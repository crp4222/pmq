import pytest

pytest.importorskip("py_clob_client_v2")

from py_clob_client_v2.exceptions import PolyApiException  # noqa: E402

from pmq.exceptions import IntrospectionMismatch, OrderUncertain  # noqa: E402
from pmq.executor import DEFAULT_BUILDER_CODE, PolymarketExecutor  # noqa: E402


class FakeClient:
    """Signature-compatible stand-in for ClobClient (introspection must pass)."""

    def __init__(self, market_resp=None, market_exc=None, trades=None,
                 balance=None, open_orders=None, cancel_resp=None,
                 order_resp=None):
        self.market_resp, self.market_exc = market_resp, market_exc
        self.trades, self.balance = trades, balance
        self.open = open_orders or []
        self.cancel_resp, self.order_resp = cancel_resp, order_resp
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

    def cancel_orders(self, order_hashes):
        self.calls.append(("cancel_orders", order_hashes))
        return self.cancel_resp

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return self.order_resp

    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None):
        return self.open

    def get_trades(self, params=None, only_first_page=False, next_cursor=None):
        self.calls.append(("get_trades", params))
        return self.trades

    def get_balance_allowance(self, params=None):
        return self.balance

    def get_clob_market_info(self, condition_id):
        raise RuntimeError("market info unavailable")


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


def _spy_clob(monkeypatch, captured):
    """Monkeypatch the real ClobClient with a signature-compatible spy that
    records construction kwargs (same pattern as the BuilderConfig test)."""
    import py_clob_client_v2.client as real

    class SpyClob(FakeClient):
        def __init__(self, host, chain_id=None, key=None, signature_type=None,
                     funder=None, builder_config=None, use_server_time=True,
                     retry_on_error=True):
            super().__init__()
            captured.update(host=host, chain_id=chain_id, key=key,
                            signature_type=signature_type, funder=funder,
                            builder_config=builder_config)

    monkeypatch.setattr(real, "ClobClient", SpyClob)


def test_real_construction_reads_sig_type_and_funder_from_env(monkeypatch):
    captured = {}
    _spy_clob(monkeypatch, captured)
    monkeypatch.setenv("POLY_SIG_TYPE", "3")
    monkeypatch.setenv("POLY_FUNDER", "0x" + "f" * 40)
    ex = PolymarketExecutor(key="0x" + "1" * 64, derive_creds=False,
                            builder_code=None)
    assert captured["signature_type"] == 3          # env string parsed to int
    assert isinstance(captured["signature_type"], int)
    assert captured["funder"] == "0x" + "f" * 40
    assert ex.funder == "0x" + "f" * 40             # stored on the real branch


def test_real_construction_defaults_sig_type_zero_and_no_funder(monkeypatch):
    captured = {}
    _spy_clob(monkeypatch, captured)
    monkeypatch.delenv("POLY_SIG_TYPE", raising=False)
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    ex = PolymarketExecutor(key="0x" + "1" * 64, derive_creds=False,
                            builder_code=None)
    assert captured["signature_type"] == 0 and captured["funder"] is None
    assert ex.funder is None


def test_sig_type_above_zero_without_funder_is_refused(monkeypatch):
    captured = {}
    _spy_clob(monkeypatch, captured)
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    with pytest.raises(ValueError, match="funder"):
        PolymarketExecutor(key="0x" + "1" * 64, signature_type=3,
                           derive_creds=False, builder_code=None)
    assert not captured                 # refused before any client was built


def test_missing_key_is_refused(monkeypatch):
    captured = {}
    _spy_clob(monkeypatch, captured)
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    with pytest.raises(ValueError, match="POLY_PRIVATE_KEY"):
        PolymarketExecutor(builder_code=None)
    assert not captured


def test_funder_stored_on_injected_client_branch(monkeypatch):
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    assert make(FakeClient(), funder="0x" + "a" * 40).funder == "0x" + "a" * 40
    monkeypatch.setenv("POLY_FUNDER", "0x" + "b" * 40)
    assert make(FakeClient()).funder == "0x" + "b" * 40


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


def test_introspection_guard_covers_every_member_the_executor_calls():
    """0.4.10: the expected-surface table also covers cancel_orders (the
    cancel_order safety path), post_only on create_and_post_order and
    get_clob_market_info (fee_rate). A client missing any of them is
    refused at startup, not discovered on the first failing call."""
    class NoCancelOrders(FakeClient):
        cancel_orders = None
    with pytest.raises(IntrospectionMismatch, match="cancel_orders"):
        make(NoCancelOrders())

    class NoPostOnly(FakeClient):
        def create_and_post_order(self, order_args, options=None,
                                  order_type="GTC", defer_exec=False):
            return None
    with pytest.raises(IntrospectionMismatch, match="post_only"):
        make(NoPostOnly())

    class NoMarketInfo(FakeClient):
        get_clob_market_info = None
    with pytest.raises(IntrospectionMismatch, match="get_clob_market_info"):
        make(NoMarketInfo())

    class NoGetOrder(FakeClient):
        get_order = None
    with pytest.raises(IntrospectionMismatch, match="method get_order missing"):
        make(NoGetOrder())


def test_reconcile_cancels_then_reports_truth():
    fc = FakeClient(trades=[{"side": "BUY", "size": "2", "price": "0.95"}],
                    open_orders=[])
    sh, usd, fees = make(fc).reconcile("0xc")
    assert sh == 2.0 and abs(usd - 1.9) < 1e-9
    assert fc.calls and fc.calls[0][0] == "cancel"


def test_reconcile_retries_cancel_when_orders_stay_open():
    fc = FakeClient(trades=[], open_orders=[{"id": "0x1"}])
    out = make(fc).reconcile("0xc")
    assert [c[0] for c in fc.calls].count("cancel") == 2
    assert out == (0.0, 0.0, 0.0)


def test_fee_rate_authoritative_with_fallback():
    class WithInfo(FakeClient):
        def get_clob_market_info(self, condition_id):
            return {"fd": {"r": 0.03, "e": 1}}
    assert make(WithInfo()).fee_rate("0xc") == 0.03
    assert make(FakeClient()).fee_rate("0xc") == 0.07  # fallback: endpoint fails


def test_cancel_order_true_only_on_confirmed_cancel():
    """The CLOB can decline a cancel INSIDE an HTTP 200 while the order is
    mid match (measured live 2026-07-06: a blindly trusted 200 freed
    budget while the order was still resting). True requires the order id
    under ``canceled``; not_canceled and any unexpected body read as NOT
    canceled."""
    ok = FakeClient(cancel_resp={"canceled": ["0xdead"], "not_canceled": None})
    assert make(ok).cancel_order("0xdead") is True
    assert ("cancel_orders", ["0xdead"]) in ok.calls

    declined = FakeClient(cancel_resp={
        "canceled": [], "not_canceled": {"0xdead": "order is being matched"}})
    assert make(declined).cancel_order("0xdead") is False


def test_cancel_order_fails_closed_on_unexpected_bodies_and_errors():
    for body in (None, "OK", 200, [], {}, {"canceled": ["0xother"]},
                 {"canceled": "0xdead"}, {"not_canceled": ["0xdead"]}):
        assert make(FakeClient(cancel_resp=body)).cancel_order("0xdead") is False

    class CancelBoom(FakeClient):
        def cancel_orders(self, order_hashes):
            raise RuntimeError("down")
    assert make(CancelBoom()).cancel_order("0xdead") is False


def test_get_order_returns_dict_or_none():
    rec = {"id": "0xdead", "status": "MATCHED", "size_matched": "4.2"}
    ex = make(FakeClient(order_resp=rec))
    assert ex.get_order("0xdead") == rec
    assert make(FakeClient(order_resp="OK")).get_order("0xdead") is None

    class OrderBoom(FakeClient):
        def get_order(self, order_id):
            raise RuntimeError("down")
    assert make(OrderBoom()).get_order("0xdead") is None


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


MAKER_RECORD = {
    "side": "BUY", "size": "26.461537", "price": "0.64",
    "trader_side": "MAKER", "status": "CONFIRMED",
    "transaction_hash": "0x1b60f19a6f089624f27babb58bf82538c49f044ee83778783195e26a33c35d09",
    "maker_orders": [
        {"maker_address": "0x76cD962FC8C5f5E5a0CBE14C74339AA78268dA58",
         "matched_amount": "5", "price": "0.39", "order_id": "0x35fb607f67ef"},
        {"maker_address": "0x51DBDd2b190a49c1D6fA6df84c1F4A079bC1De76",
         "matched_amount": "21.461537", "price": "0.3500000023297493",
         "order_id": "0x868caa9688b9"}]}


def test_trades_totals_maker_record_counts_only_our_slice(monkeypatch):
    """Real settlement record 2026-07-04: the taker's aggregate size sits at
    top level; our fill is the maker_orders slice matched by funder."""
    monkeypatch.setenv("POLY_FUNDER", "0x76cD962FC8C5f5E5a0CBE14C74339AA78268dA58")
    ex = make(FakeClient(trades=[MAKER_RECORD]))
    sh, usd, fees = ex.trades_totals("0xc")
    assert sh == 5.0 and abs(usd - 1.95) < 1e-9 and fees == 0.0


def test_trades_totals_taker_records_unchanged(monkeypatch):
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    taker = {"side": "BUY", "size": "5.2", "price": "0.95",
             "trader_side": "TAKER", "status": "CONFIRMED"}
    ex = make(FakeClient(trades=[taker, {"side": "BUY", "size": "9",
                                         "price": "0.9", "status": "FAILED"}]))
    sh, usd, fees = ex.trades_totals("0xc")
    assert sh == 5.2 and abs(usd - 4.94) < 1e-9 and fees > 0


def test_trades_totals_without_funder_counts_every_slice(monkeypatch):
    """No POLY_FUNDER set: every slice of a bundled maker record is
    attributed to us (the documented reason to set POLY_FUNDER even on
    sig 0 accounts that post resting orders)."""
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    sh, usd, fees = make(FakeClient(trades=[MAKER_RECORD])).trades_totals("0xc")
    assert abs(sh - (5 + 21.461537)) < 1e-9
    assert abs(usd - (5 * 0.39 + 21.461537 * 0.3500000023297493)) < 1e-9
    assert fees == 0.0


def test_trades_totals_funder_match_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(
        "POLY_FUNDER", "0x76CD962FC8C5F5E5A0CBE14C74339AA78268DA58")
    sh, usd, fees = make(FakeClient(trades=[MAKER_RECORD])).trades_totals("0xc")
    assert sh == 5.0 and abs(usd - 1.95) < 1e-9 and fees == 0.0


def test_trades_totals_sliceless_maker_records_use_top_level_size(monkeypatch):
    """maker_orders missing, empty or non-list: the top-level size is ours
    (minimal records), still at zero fee."""
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    for mos in ({}, {"maker_orders": []}, {"maker_orders": "not-a-list"}):
        rec = {"side": "BUY", "size": "3", "price": "0.5",
               "trader_side": "MAKER", **mos}
        sh, usd, fees = make(FakeClient(trades=[rec])).trades_totals("0xc")
        assert (sh, usd, fees) == (3.0, 1.5, 0.0)


def test_trades_totals_skips_malformed_rows_and_slices(monkeypatch):
    """Non-dict rows, unparseable amounts and non-dict slices are skipped;
    well-formed records still count (fail closed, never raise)."""
    monkeypatch.setenv("POLY_FUNDER", "0x76cD962FC8C5f5E5a0CBE14C74339AA78268dA58")
    me = "0x76cD962FC8C5f5E5a0CBE14C74339AA78268dA58"
    trades = [
        "garbage", None, 42,
        {"side": "BUY", "size": "x", "price": "0.9", "trader_side": "TAKER"},
        {"side": "BUY", "size": "3", "price": "y", "trader_side": "MAKER"},
        {"side": "BUY", "trader_side": "MAKER", "maker_orders": [
            "not-a-dict",
            {"maker_address": me, "matched_amount": "bad", "price": "1"},
            {"maker_address": me, "matched_amount": "2", "price": "0.5"}]},
        {"side": "BUY", "size": "1", "price": "0.6", "trader_side": "TAKER"},
    ]
    sh, usd, fees = make(FakeClient(trades=trades)).trades_totals("0xc")
    assert abs(sh - 3.0) < 1e-9 and abs(usd - 1.6) < 1e-9
    assert abs(fees - 0.07 * 0.6 * 0.4 * 1) < 1e-9  # only the taker row


def test_trades_totals_none_when_api_unreachable_but_zeros_on_empty():
    class Boom(FakeClient):
        def get_trades(self, params=None, only_first_page=False, next_cursor=None):
            raise RuntimeError("down")
    assert make(Boom()).trades_totals("0xc") is None
    assert make(FakeClient(trades=None)).trades_totals("0xc") == (0.0, 0.0, 0.0)
    assert make(FakeClient(trades=[])).trades_totals("0xc") == (0.0, 0.0, 0.0)


def test_trades_totals_non_finite_or_negative_amounts_book_zero(monkeypatch):
    """json.loads accepts NaN/Infinity; one hostile or corrupt tape record
    must not poison exchange-truth totals (NaN compares false everywhere,
    the exact re-buy failure mode reconcile exists to prevent). Same
    finite non-negative contract as the fill parser since 0.4.4."""
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    good = {"side": "BUY", "size": "2", "price": "0.5", "trader_side": "TAKER"}
    for bad in ("NaN", "Infinity", "-3", float("nan"), float("inf"), -1.0):
        rows = [{"side": "BUY", "size": bad, "price": "0.5",
                 "trader_side": "TAKER"},
                {"side": "BUY", "size": bad, "price": "0.5",
                 "trader_side": "MAKER"},
                {"side": "BUY", "trader_side": "MAKER", "maker_orders": [
                    {"maker_address": "", "matched_amount": bad, "price": "0.5"}]},
                {"side": "BUY", "size": "2", "price": bad,
                 "trader_side": "TAKER"},
                dict(good)]
        sh, usd, fees = make(FakeClient(trades=rows)).trades_totals("0xc")
        assert sh == 2.0 and abs(usd - 1.0) < 1e-9
        assert abs(fees - 0.07 * 0.5 * 0.5 * 2) < 1e-9


def test_trades_totals_non_list_body_is_not_truth():
    """A drifted /trades body shape (dict, string, number) must read as
    'truth unavailable' (None), never as zeros that would trigger a
    re-buy, and never raise."""
    for body in ({"error": "nope"}, "FAIL", 7, True):
        assert make(FakeClient(trades=body)).trades_totals("0xc") is None


def _attr_client(taker_id="0xT1", maker_id="0xM1"):
    class C(FakeClient):
        def get_trades(self, params=None, only_first_page=False, next_cursor=None):
            return [
                {"side": "BUY", "status": "CONFIRMED", "trader_side": "TAKER",
                 "taker_order_id": taker_id, "size": "5.0", "price": "0.9"},
                {"side": "BUY", "status": "CONFIRMED", "trader_side": "TAKER",
                 "taker_order_id": "0xOTHER", "size": "7.0", "price": "0.8"},
                {"side": "BUY", "status": "CONFIRMED", "trader_side": "MAKER",
                 "size": "99", "price": "0.5",
                 "maker_orders": [
                     {"order_id": maker_id, "matched_amount": "3.0", "price": "0.6"},
                     {"order_id": "0xTHEIRS", "matched_amount": "4.0", "price": "0.6"},
                 ]},
            ]
    return C()


def test_order_registry_filters_totals(tmp_path):
    mine = tmp_path / "mine.ids"
    theirs = tmp_path / "theirs.ids"
    mine.write_text("0xT1\n0xM1\n")
    theirs.write_text("0xOTHER\n0xTHEIRS\n")
    ex = PolymarketExecutor(client=_attr_client(), builder_code=None,
                            order_log=str(mine),
                            foreign_order_logs=[str(theirs)])
    sh, usd, fees = ex.trades_totals("0xc")
    assert sh == 5.0 + 3.0 and abs(usd - (4.5 + 1.8)) < 1e-9
    # legacy mode (no registry): everything counts, maker slices by funder
    ex2 = make(FakeClient())
    ex2.client = _attr_client()
    sh2, usd2, _ = ex2.trades_totals("0xc")
    assert sh2 == 5.0 + 7.0 + 3.0 + 4.0


def test_claim_unknown_recovers_uncertain_orders(tmp_path):
    mine = tmp_path / "mine.ids"
    theirs = tmp_path / "theirs.ids"
    mine.write_text("")                      # rien a nous dans le registre
    theirs.write_text("0xOTHER\n0xTHEIRS\n") # l autre bot revendique les siens
    ex = PolymarketExecutor(client=_attr_client(taker_id="0xUNKNOWN",
                                                maker_id="0xUNKNOWN2"),
                            builder_code=None, order_log=str(mine),
                            foreign_order_logs=[str(theirs)])
    assert ex.trades_totals("0xc")[0] == 0.0                 # strict: exclu
    sh, _, _ = ex.trades_totals("0xc", claim_unknown=True)   # reconcile: reclame
    assert sh == 5.0 + 3.0


def test_posted_orders_land_in_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("POLY_ORDER_LOG", raising=False)
    reg = tmp_path / "bot.ids"
    fc = FakeClient(market_resp={"orderID": "0xNEW", "success": True,
                                 "makingAmount": "4.9", "takingAmount": "5.1"})
    ex = PolymarketExecutor(client=fc, builder_code=None, order_log=str(reg))
    ex.buy_fak("tok", 0.97, 5.0)
    assert "0xNEW" in reg.read_text()
    assert "0xNEW" in ex._own_ids


# ---- 0.6.1: maker fills under the counterparty's top-level side/asset ----
# A MAKER-role record reports the TAKER at top level. Measured live
# 2026-07-06: 6 CONFIRMED maker fills (16.97 shares, 16.19 USD) were
# invisible to trades_totals because their records carried the taker's
# SELL (or the complementary asset) at top level.

ME = "0x76cD962FC8C5f5E5a0CBE14C74339AA78268dA58"
UP = "111"
DOWN = "222"


def _mslice(amount, price, side=None, asset=None, oid="0xM1", addr=ME):
    mo = {"order_id": oid, "maker_address": addr,
          "matched_amount": amount, "price": price}
    if side is not None:
        mo["side"] = side
    if asset is not None:
        mo["asset_id"] = asset
        mo["outcome"] = "Up" if asset == UP else "Down"
    return mo


def _mrec(taker_side, taker_asset=None, slices=None):
    rec = {"side": taker_side, "size": "40", "price": "0.5",
           "trader_side": "MAKER", "status": "CONFIRMED"}
    if taker_asset is not None:
        rec["asset_id"] = taker_asset
        rec["outcome"] = "Up" if taker_asset == UP else "Down"
    if slices is not None:
        rec["maker_orders"] = slices
    return rec


def test_trades_totals_counts_maker_buy_under_taker_sell_record(monkeypatch):
    """THE 2026-07-06 bug: our BUY bid lifted by a selling taker lives in
    a record whose top-level side is SELL. It books from the slice."""
    monkeypatch.setenv("POLY_FUNDER", ME)
    rec = _mrec("SELL", UP, [_mslice("16.97", "0.954", side="BUY", asset=UP)])
    sh, usd, fees = make(FakeClient(trades=[rec])).trades_totals("0xc", UP)
    assert abs(sh - 16.97) < 1e-9 and abs(usd - 16.97 * 0.954) < 1e-9
    assert fees == 0.0


def test_trades_totals_counts_maker_fill_under_complementary_record(monkeypatch):
    """Mint match: the taker bought the OTHER outcome, so even the record
    asset is not ours. The slice carries our token and side and books."""
    monkeypatch.setenv("POLY_FUNDER", ME)
    rec = _mrec("BUY", DOWN, [_mslice("5", "0.39", side="BUY", asset=UP)])
    sh, usd, fees = make(FakeClient(trades=[rec])).trades_totals("0xc", UP)
    assert sh == 5.0 and abs(usd - 1.95) < 1e-9 and fees == 0.0


def test_trades_totals_queries_by_condition_alone():
    """The venue-side asset filter is gone (it can hide maker fills whose
    record carries the complementary token): side and token now apply
    in-process."""
    fc = FakeClient(trades=[])
    make(fc).trades_totals("0xc", UP)
    params = [c[1] for c in fc.calls if c[0] == "get_trades"][0]
    assert params.market == "0xc" and params.asset_id is None


def test_trades_totals_requested_side_selects_maker_slices(monkeypatch):
    """side='BUY' books only our BUY slices and side='SELL' only our SELL
    slices, whatever the records' top-level sides say."""
    monkeypatch.setenv("POLY_FUNDER", ME)
    rows = [_mrec("SELL", UP, [_mslice("3", "0.5", side="BUY", asset=UP)]),
            _mrec("BUY", UP, [_mslice("7", "0.6", side="SELL", asset=UP)])]
    ex = make(FakeClient(trades=rows))
    b = ex.trades_totals("0xc", UP, side="BUY")
    s = ex.trades_totals("0xc", UP, side="SELL")
    assert b[0] == 3.0 and abs(b[1] - 1.5) < 1e-9
    assert s[0] == 7.0 and abs(s[1] - 4.2) < 1e-9


def test_trades_totals_token_filter_applies_per_slice(monkeypatch):
    """One trade can mix same-asset makers (opposite side of the taker)
    and complementary-asset makers (same side, mint): every slice books
    to its OWN token, never to the record's top-level asset."""
    monkeypatch.setenv("POLY_FUNDER", ME)
    rec = _mrec("BUY", UP, [_mslice("4", "0.55", side="SELL", asset=UP),
                            _mslice("6", "0.44", side="BUY", asset=DOWN,
                                    oid="0xM2")])
    ex = make(FakeClient(trades=[rec]))
    assert ex.trades_totals("0xc", UP, side="SELL")[0] == 4.0
    assert ex.trades_totals("0xc", DOWN, side="BUY")[0] == 6.0
    assert ex.trades_totals("0xc", UP, side="BUY")[0] == 0.0
    assert ex.trades_totals("0xc", DOWN, side="SELL")[0] == 0.0


def test_trades_totals_derives_slice_side_when_absent(monkeypatch):
    """Slices without a side field: the same asset as the taker means we
    faced it (opposite side), the complementary asset means the same
    side. Outcome labels carry the same information when asset ids are
    missing too."""
    monkeypatch.setenv("POLY_FUNDER", ME)
    faced = _mrec("SELL", UP, [_mslice("3", "0.5", asset=UP)])
    minted = _mrec("BUY", DOWN, [_mslice("5", "0.39", asset=UP)])
    ex = make(FakeClient(trades=[faced, minted]))
    sh, usd, _ = ex.trades_totals("0xc", UP)
    assert sh == 8.0 and abs(usd - (1.5 + 1.95)) < 1e-9
    assert ex.trades_totals("0xc", UP, side="SELL")[0] == 0.0

    by_outcome = {"side": "SELL", "size": "9", "price": "0.5",
                  "outcome": "Up", "trader_side": "MAKER",
                  "maker_orders": [{"order_id": "0x1", "maker_address": ME,
                                    "matched_amount": "2", "price": "0.5",
                                    "outcome": "Up"}]}
    assert make(FakeClient(trades=[by_outcome])).trades_totals("0xc")[0] == 2.0


def test_trades_totals_sliceless_maker_record_keeps_record_side(monkeypatch):
    """No slices and no per-slice information: our side is unknowable, so
    the record side keeps standing for it (trimmed archival records; real
    MAKER records always carry maker_orders). Documented limit."""
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    rec = {"side": "SELL", "size": "3", "price": "0.5", "trader_side": "MAKER"}
    assert make(FakeClient(trades=[rec])).trades_totals("0xc")[0] == 0.0
    assert make(FakeClient(trades=[rec])).trades_totals(
        "0xc", side="SELL")[0] == 3.0


def test_trades_totals_only_mine_gates_recovered_maker_records(tmp_path,
                                                               monkeypatch):
    """The records the old side pre-filter skipped now flow through the
    registry gate like any other: a slice outside OUR registry books
    nothing (the attribution invariant does not weaken with the fix)."""
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    mine = tmp_path / "mine.ids"
    mine.write_text("0xMINE\n")
    rec = _mrec("SELL", UP,
                [_mslice("3", "0.5", side="BUY", asset=UP, oid="0xMINE"),
                 _mslice("9", "0.5", side="BUY", asset=UP, oid="0xFOREIGN")])
    ex = PolymarketExecutor(client=FakeClient(trades=[rec]),
                            builder_code=None, order_log=str(mine))
    sh, usd, _ = ex.trades_totals("0xc", UP)
    assert sh == 3.0 and abs(usd - 1.5) < 1e-9


def test_trades_totals_taker_path_locked(monkeypatch):
    """favbot and chainlive score real money through the taker path: the
    maker fix must not move it. Side selection, FAILED exclusion and the
    fee estimate behave exactly as 0.6.0."""
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    rows = [
        {"side": "BUY", "size": "5", "price": "0.9", "trader_side": "TAKER",
         "asset_id": UP, "status": "CONFIRMED"},
        {"side": "SELL", "size": "9", "price": "0.5", "trader_side": "TAKER",
         "asset_id": UP, "status": "CONFIRMED"},
        {"side": "BUY", "size": "7", "price": "0.8", "trader_side": "TAKER",
         "asset_id": UP, "status": "FAILED"},
    ]
    sh, usd, fees = make(FakeClient(trades=rows)).trades_totals("0xc", UP)
    assert (sh, usd) == (5.0, 4.5)
    assert abs(fees - 0.07 * 0.9 * 0.1 * 5) < 1e-9


def test_trades_totals_taker_token_filter_in_process(monkeypatch):
    """token_id now filters taker records in-process on their top-level
    asset (the field the venue filtered on): the other token of the
    market is excluded, records without an asset keep counting."""
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    rows = [
        {"side": "BUY", "size": "5", "price": "0.9", "trader_side": "TAKER",
         "asset_id": UP},
        {"side": "BUY", "size": "7", "price": "0.8", "trader_side": "TAKER",
         "asset_id": DOWN},
        {"side": "BUY", "size": "2", "price": "0.5", "trader_side": "TAKER"},
    ]
    ex = make(FakeClient(trades=rows))
    assert ex.trades_totals("0xc", UP)[0] == 7.0
    assert ex.trades_totals("0xc")[0] == 14.0


MAKER_RECORD_FULL = {
    # The MAKER_RECORD settlement in the COMPLETE shape get_trades returns
    # (captured 2026-07-04; owner/counterparty ids scrubbed): the top
    # level is the TAKER, a BUY of the opposite outcome minted against
    # us, and our fill is the funder-matched slice, itself side BUY with
    # its own asset_id. Note the slices' empty fee_rate_bps strings.
    "id": "8489e058-a38e-431d-8d7d-c74103c4a689",
    "taker_order_id": "0xtaker",
    "market": "0x7c2d64edce3705436bfb026276fc0454d97f55ea2d336931537caf90d5c01d59",
    "asset_id": "13462038953389429583043236757434556877510677005296"
                "543188461193393177949107848",
    "side": "BUY", "size": "26.461537", "fee_rate_bps": "0",
    "price": "0.64", "status": "CONFIRMED",
    "match_time": "1783168449", "last_update": "1783168459",
    "outcome": "EDward Gaming", "bucket_index": 0, "owner": "scrubbed",
    "maker_address": "0xa50A47f0443E7Af89f18cAAc78BB7eF388b54E2e",
    "transaction_hash": "0x1b60f19a6f089624f27babb58bf82538c49f044ee83778"
                        "783195e26a33c35d09",
    "maker_orders": [
        {"order_id": "0x35fb607f67ef", "owner": "scrubbed",
         "maker_address": "0x76cD962FC8c5f5E5A0CBE14c74339aA78268da58",
         "matched_amount": "5", "price": "0.39", "fee_rate_bps": "",
         "asset_id": "24190104720077035941619094576418214448508678280027"
                     "219867188476329765265887856",
         "outcome": "Rex Regum Qeon", "side": "BUY"},
        {"order_id": "0x868caa9688b9", "owner": "scrubbed",
         "maker_address": "0x51DBDd2b190a49c1D6fA6df84c1F4A079bC1De76",
         "matched_amount": "21.461537", "price": "0.3500000023297493",
         "fee_rate_bps": "",
         "asset_id": "24190104720077035941619094576418214448508678280027"
                     "219867188476329765265887856",
         "outcome": "Rex Regum Qeon", "side": "BUY"}],
    "trader_side": "MAKER"}


def test_trades_totals_real_full_record(monkeypatch):
    """Full-fidelity real record: requested BUY on our token books our
    slice; requested SELL books nothing; the taker's token books nothing
    of ours."""
    monkeypatch.setenv("POLY_FUNDER", ME)
    tok = MAKER_RECORD_FULL["maker_orders"][0]["asset_id"]
    other = MAKER_RECORD_FULL["asset_id"]
    ex = make(FakeClient(trades=[MAKER_RECORD_FULL]))
    sh, usd, fees = ex.trades_totals("0xc", tok)
    assert sh == 5.0 and abs(usd - 1.95) < 1e-9 and fees == 0.0
    assert ex.trades_totals("0xc", tok, side="SELL")[0] == 0.0
    assert ex.trades_totals("0xc", other)[0] == 0.0
