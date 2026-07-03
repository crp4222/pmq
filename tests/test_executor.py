import pytest

pytest.importorskip("py_clob_client_v2")

from py_clob_client_v2.exceptions import PolyApiException  # noqa: E402

from pmq.exceptions import IntrospectionMismatch, OrderUncertain  # noqa: E402
from pmq.executor import DEFAULT_BUILDER_CODE, PolymarketExecutor  # noqa: E402


class FakeClient:
    """Signature-compatible stand-in for ClobClient (introspection must pass)."""

    def __init__(self, market_resp=None, market_exc=None, trades=None,
                 balance=None, open_orders=None):
        self.market_resp, self.market_exc = market_resp, market_exc
        self.trades, self.balance = trades, balance
        self.open = open_orders or []
        self.calls = []

    def create_and_post_market_order(self, order_args, options=None,
                                     order_type="FOK", defer_exec=False):
        self.calls.append(("market", order_args, order_type))
        if self.market_exc:
            raise self.market_exc
        return self.market_resp

    def create_and_post_order(self, order_args, options=None, order_type="GTC",
                              post_only=False, defer_exec=False):
        self.calls.append(("limit", order_args, order_type))
        if self.market_exc:
            raise self.market_exc
        return self.market_resp

    def cancel_market_orders(self, payload):
        self.calls.append(("cancel", payload))

    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None):
        return self.open

    def get_trades(self, params=None, only_first_page=False, next_cursor=None):
        return self.trades

    def get_balance_allowance(self, params=None):
        return self.balance


def make(client, **kw):
    kw.setdefault("builder_code", None)
    return PolymarketExecutor(client=client, **kw)


def api_error(status, msg="boom"):
    e = PolyApiException(error_msg=msg)
    e.status_code = status
    return e


def test_confirmed_buy_fill_books_matched_amounts():
    ex = make(FakeClient(market_resp={"orderID": "0xabc", "success": True,
                                      "makingAmount": "4.98", "takingAmount": "5.134"}))
    f = ex.buy_fak("tok", 0.97, 5.0)
    assert f and f.matched_shares == 5.134 and f.matched_usd == 4.98
    assert abs(f.price - 0.9700) < 1e-3
    assert not f.rejected


def test_error_dict_on_200_books_nothing():
    ex = make(FakeClient(market_resp={"error": "insufficient balance"}))
    f = ex.buy_fak("tok", 0.97, 5.0)
    assert not f and f.rejected


def test_success_false_books_nothing():
    ex = make(FakeClient(market_resp={"orderID": "0xabc", "success": False}))
    assert not ex.buy_fak("tok", 0.97, 5.0)


def test_non_dict_response_books_nothing():
    ex = make(FakeClient(market_resp="OK"))
    f = ex.buy_fak("tok", 0.97, 5.0)
    assert not f and f.rejected


def test_unparseable_amounts_book_zero():
    ex = make(FakeClient(market_resp={"orderID": "0xabc", "makingAmount": "x"}))
    f = ex.buy_fak("tok", 0.97, 5.0)
    assert f.order_id == "0xabc" and not f


def test_4xx_is_clean_rejection():
    ex = make(FakeClient(market_exc=api_error(400, "no orders found to match")))
    f = ex.buy_fak("tok", 0.97, 5.0)
    assert f.rejected and "no orders found" in f.error


def test_5xx_and_unknown_raise_order_uncertain():
    for exc in (api_error(502), api_error(None), RuntimeError("socket timeout")):
        ex = make(FakeClient(market_exc=exc))
        with pytest.raises(OrderUncertain):
            ex.buy_fak("tok", 0.97, 5.0)


def test_sell_mirrors_amounts():
    ex = make(FakeClient(market_resp={"orderID": "0xs", "makingAmount": "5.134",
                                      "takingAmount": "4.98"}))
    f = ex.sell_fak("tok", 0.95, 5.134)
    assert f.matched_shares == 5.134 and f.matched_usd == 4.98


def test_buy_amount_rounds_down_to_cent():
    fc = FakeClient(market_resp={"orderID": "0x1", "makingAmount": "0", "takingAmount": "0"})
    make(fc).buy_fak("tok", 0.97, 4.999)
    assert fc.calls[0][1].amount == 4.99


def test_zero_amount_never_reaches_the_wire():
    fc = FakeClient()
    f = make(fc).buy_fak("tok", 0.97, 0.004)
    assert f.rejected and not fc.calls


def test_builder_code_default_and_optout(monkeypatch):
    monkeypatch.delenv("POLY_BUILDER_CODE", raising=False)
    assert PolymarketExecutor(client=FakeClient()).builder_code == DEFAULT_BUILDER_CODE
    assert make(FakeClient()).builder_code is None
    with pytest.raises(ValueError):
        PolymarketExecutor(client=FakeClient(), builder_code="0xnotbytes32")


def test_trades_totals_filters_and_fees():
    trades = [
        {"side": "BUY", "size": "5", "price": "0.9", "trader_side": "TAKER"},
        {"side": "BUY", "size": "5", "price": "0.9", "status": "FAILED"},
        {"side": "SELL", "size": "9", "price": "0.5"},
        {"side": "BUY", "size": "10", "price": "0.8", "trader_side": "MAKER"},
    ]
    sh, usd, fees = make(FakeClient(trades=trades)).trades_totals("0xc")
    assert (sh, usd) == (15.0, 0.9 * 5 + 0.8 * 10)
    assert abs(fees - 0.07 * 0.9 * 0.1 * 5) < 1e-9  # maker leg pays zero


def test_collateral_parses_raw_units_and_fails_closed():
    assert make(FakeClient(balance={"balance": "34070070"})).collateral() == 34.07007
    assert make(FakeClient(balance="oops")).collateral() == 0.0
    with pytest.raises(RuntimeError):
        make(FakeClient(balance={"balance": "1000000"})).require_collateral(5.0)


def test_introspection_guard_refuses_drifted_client():
    class Drifted(FakeClient):
        create_and_post_market_order = None
    with pytest.raises(IntrospectionMismatch):
        make(Drifted())


def test_reconcile_cancels_then_reports_truth():
    fc = FakeClient(trades=[{"side": "BUY", "size": "2", "price": "0.95"}],
                    open_orders=[])
    sh, usd, fees = make(fc).reconcile("0xc")
    assert sh == 2.0 and abs(usd - 1.9) < 1e-9
    assert fc.calls and fc.calls[0][0] == "cancel"
