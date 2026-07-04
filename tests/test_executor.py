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


def test_cent_rounding_has_no_binary_drift():
    from pmq.executor import _floor_cents
    # int(x*100)/100 would return one cent low here; _floor_cents must not.
    assert _floor_cents(16.90) == 16.90
    assert _floor_cents(33.30) == 33.30
    assert _floor_cents(66.60) == 66.60
    # genuine sub-cent values still floor
    assert _floor_cents(5.007) == 5.00
    assert _floor_cents(4.999) == 4.99


def test_buy_fak_sends_intended_cents_not_drifted():
    fc = FakeClient(market_resp={"orderID": "0x1", "makingAmount": "0", "takingAmount": "0"})
    make(fc).buy_fak("tok", 0.97, 16.90)
    assert fc.calls[0][1].amount == 16.90  # not 16.89


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


def test_builder_code_rides_inside_every_order(monkeypatch):
    """Attribution is part of the signed order args on BOTH order paths."""
    monkeypatch.delenv("POLY_BUILDER_CODE", raising=False)
    ok = {"orderID": "0x1", "makingAmount": "1", "takingAmount": "1"}
    fc = FakeClient(market_resp=ok)
    ex = PolymarketExecutor(client=fc)
    ex.buy_fak("tok", 0.97, 5.0)
    ex.sell_fak("tok", 0.95, 5.0)
    ex.limit_gtc("tok", 0.50, 10.0, "BUY")
    assert [c[1].builder_code for c in fc.calls] == [DEFAULT_BUILDER_CODE] * 3


def test_builder_code_env_override_and_optout_reach_the_order(monkeypatch):
    code = "0x" + "ab" * 32
    monkeypatch.setenv("POLY_BUILDER_CODE", code)
    ok = {"orderID": "0x1", "makingAmount": "1", "takingAmount": "1"}
    fc = FakeClient(market_resp=ok)
    PolymarketExecutor(client=fc).buy_fak("tok", 0.97, 5.0)
    assert fc.calls[0][1].builder_code == code
    fc2 = FakeClient(market_resp=ok)
    make(fc2).buy_fak("tok", 0.97, 5.0)  # opt-out: client default (zero) stands
    assert "ab" not in fc2.calls[0][1].builder_code


def test_builder_config_reaches_the_real_client(monkeypatch):
    """The real ClobClient must be constructed with the BuilderConfig too."""
    import py_clob_client_v2.client as real

    captured = {}

    class SpyClob(FakeClient):
        def __init__(self, host, chain_id=None, key=None, signature_type=None,
                     funder=None, builder_config=None, use_server_time=True,
                     retry_on_error=True):
            super().__init__()
            captured["builder_config"] = builder_config

    monkeypatch.setattr(real, "ClobClient", SpyClob)
    monkeypatch.delenv("POLY_BUILDER_CODE", raising=False)
    PolymarketExecutor(key="0x" + "1" * 64, derive_creds=False)
    assert captured["builder_config"].builder_code == DEFAULT_BUILDER_CODE


def test_installed_client_injects_builder_config_on_both_paths():
    """Regression canary for the dependency itself: py-clob-client-v2 must
    keep injecting the client-level BuilderConfig into limit AND market
    orders. If this fails after a bump, re-verify attribution end-to-end."""
    import inspect

    from py_clob_client_v2.client import ClobClient
    for method in (ClobClient.create_order, ClobClient.create_market_order):
        assert "builder_config.builder_code" in inspect.getsource(method)


# Executable specification of CLAUDE.md invariant 1 (the fail-closed fill
# contract). One row per possible exchange outcome; weakening any expectation
# here IS the regression, whatever it fixes elsewhere.
FAIL_CLOSED_CONTRACT = [
    # exchange outcome (response or raised exception)         -> expectation
    ({"orderID": "0x1", "success": True,
      "makingAmount": "4.98", "takingAmount": "5.1"},            "booked"),
    ({"orderID": "0x1",
      "makingAmount": "4.98", "takingAmount": "5.1"},            "booked"),   # success absent
    ({"orderID": "0x1", "success": True},                        "zero"),     # no amounts
    ({"orderID": "0x1", "makingAmount": "x", "takingAmount": "y"}, "zero"),   # unparseable
    ({"orderID": "0x1", "makingAmount": None},                   "zero"),
    ({"orderID": "0x1", "success": False},                       "rejected"), # flagged failed
    ({"error": "insufficient balance"},                          "rejected"), # no orderID
    ({},                                                         "rejected"),
    ("OK",                                                       "rejected"), # non-dict
    (None,                                                       "rejected"),
    (api_error(400, "no orders found to match"),                 "rejected"), # clean 4xx
    (api_error(404),                                             "rejected"),
    (api_error(429),                                             "rejected"),
    (api_error(500),                                             "uncertain"), # 5xx MAY exist
    (api_error(502),                                             "uncertain"),
    (api_error(None),                                            "uncertain"), # status unknown
    (RuntimeError("socket timeout"),                             "uncertain"), # transport
]


@pytest.mark.parametrize("outcome,expected", FAIL_CLOSED_CONTRACT)
@pytest.mark.parametrize("path", ["market", "limit"])
def test_fail_closed_fill_contract(outcome, expected, path):
    kw = {"market_exc": outcome} if isinstance(outcome, Exception) else {"market_resp": outcome}
    ex = make(FakeClient(**kw))
    place = (lambda: ex.buy_fak("tok", 0.97, 5.0)) if path == "market" else \
            (lambda: ex.limit_gtc("tok", 0.97, 5.0, "BUY"))
    if expected == "uncertain":
        with pytest.raises(OrderUncertain):
            place()
        return
    f = place()
    if expected == "booked":
        assert f and f.matched_shares > 0 and not f.rejected
    elif expected == "zero":
        assert not f and not f.rejected and f.order_id  # order exists, books nothing
    else:
        assert not f and f.rejected and f.matched_shares == 0


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


def test_introspection_guard_covers_order_args_builder_code(monkeypatch):
    # limit_gtc now depends on OrderArgsV2.builder_code; the guard must refuse
    # a client whose OrderArgsV2 dropped it, not crash later at call time.
    import py_clob_client_v2.clob_types as ct

    class NoBuilderOrderArgs:
        def __init__(self, token_id, price, size, side):
            self.token_id, self.price, self.size, self.side = token_id, price, size, side
    monkeypatch.setattr(ct, "OrderArgsV2", NoBuilderOrderArgs)
    with pytest.raises(IntrospectionMismatch, match="OrderArgsV2 lost field builder_code"):
        make(FakeClient())


def test_reconcile_cancels_then_reports_truth():
    fc = FakeClient(trades=[{"side": "BUY", "size": "2", "price": "0.95"}],
                    open_orders=[])
    sh, usd, fees = make(fc).reconcile("0xc")
    assert sh == 2.0 and abs(usd - 1.9) < 1e-9
    assert fc.calls and fc.calls[0][0] == "cancel"


def test_fee_rate_authoritative_with_fallback():
    class WithInfo(FakeClient):
        def get_clob_market_info(self, condition_id):
            return {"fd": {"r": 0.03, "e": 1}}
    assert make(WithInfo()).fee_rate("0xc") == 0.03
    assert make(FakeClient()).fee_rate("0xc") == 0.07  # fallback: method missing


def test_cancel_order_single():
    class WithCancel(FakeClient):
        def cancel_orders(self, order_hashes):
            self.calls.append(("cancel_orders", order_hashes))
    fc = WithCancel()
    assert make(fc).cancel_order("0xdead") is True
    assert ("cancel_orders", ["0xdead"]) in fc.calls
    assert make(FakeClient()).cancel_order("0xdead") is False


def test_private_key_never_appears_in_logs(monkeypatch, caplog):
    import logging

    import py_clob_client_v2.client as real

    class SpyClob(FakeClient):
        def __init__(self, host, chain_id=None, key=None, signature_type=None,
                     funder=None, builder_config=None, use_server_time=True,
                     retry_on_error=True):
            super().__init__()

    monkeypatch.setattr(real, "ClobClient", SpyClob)
    secret = "0x" + "7" * 64
    with caplog.at_level(logging.DEBUG):
        PolymarketExecutor(key=secret, derive_creds=False)
    assert secret not in caplog.text and "7" * 64 not in caplog.text


def test_nan_inf_negative_amounts_book_zero():
    """json.loads accepts NaN/Infinity; a drifted or hostile response must
    still book nothing (finite non-negative amounts only)."""
    for bad in ("NaN", "Infinity", "-5", float("nan"), float("inf"), -3.0):
        ex = make(FakeClient(market_resp={"orderID": "0x1", "success": True,
                                          "makingAmount": bad,
                                          "takingAmount": "5.0"}))
        f = ex.buy_fak("tok", 0.97, 5.0)
        assert not f and f.matched_usd == 0.0 and f.matched_shares == 0.0


def test_market_taker_amounts_clamped_to_4dp_on_fine_ticks():
    """1.0.2's rounding table allows 5-6dp market takers on ticks finer than
    0.01; the V2 exchange caps market takers at 4dp and rejects the order.
    Constructing an executor must clamp the market path (and stay idempotent)
    so signed pairs are always exchange-acceptable."""
    from py_clob_client_v2.order_builder.builder import (
        ROUNDING_CONFIG,
        OrderBuilder,
    )
    from py_clob_client_v2.order_builder.constants import BUY, SELL

    make(FakeClient())
    make(FakeClient())          # second init must not double-wrap
    fn = OrderBuilder.get_market_order_amounts
    assert getattr(fn, "_pmq_taker4", False)
    b = object.__new__(OrderBuilder)
    for tick in ("0.01", "0.001", "0.0001"):
        _, mk, tk = fn(b, BUY, 9.98, 0.985, ROUNDING_CONFIG[tick])
        assert int(mk) % 10**4 == 0, f"maker >2dp at tick {tick}"
        assert int(tk) % 10**2 == 0, f"taker >4dp at tick {tick}"
        _, mk_s, tk_s = fn(b, SELL, 10.13, 0.985, ROUNDING_CONFIG[tick])
        assert int(mk_s) % 10**4 == 0 and int(tk_s) % 10**2 == 0


def test_startup_refuses_client_signing_dirty_market_amounts(monkeypatch):
    """If a client build slips past the 4dp clamp (new code path, future
    regression), the startup introspection must refuse to trade at all."""
    from py_clob_client_v2.order_builder import builder as b

    def dirty(self, side, amount, price, round_config):
        return side, 9980000, 10131970          # taker 10.13197: 5 decimals

    dirty._pmq_taker4 = True                    # defeat the pmq wrapper
    monkeypatch.setattr(b.OrderBuilder, "get_market_order_amounts", dirty)
    with pytest.raises(IntrospectionMismatch):
        make(FakeClient())
