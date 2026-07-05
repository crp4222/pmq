"""Property fuzz of the fail-closed fill contract: whatever shape the
exchange response takes (including adversarial NaN/inf/negative amounts,
arbitrary nesting, wrong types), the executor books only confirmed,
finite, non-negative matched amounts, and the only exception that can
escape an order call is OrderUncertain."""
import math

import pytest

pytest.importorskip("py_clob_client_v2")
hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from py_clob_client_v2.exceptions import PolyApiException  # noqa: E402

from pmq.exceptions import OrderUncertain  # noqa: E402
from pmq.executor import Fill, PolymarketExecutor  # noqa: E402


class FuzzClient:
    """Signature-compatible ClobClient stand-in (introspection must pass)."""

    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def create_and_post_market_order(self, order_args, options=None,
                                     order_type="FOK", defer_exec=False):
        if self.exc:
            raise self.exc
        return self.resp

    def create_and_post_order(self, order_args, options=None, order_type="GTC",
                              post_only=False, defer_exec=False):
        if self.exc:
            raise self.exc
        return self.resp

    def cancel_market_orders(self, payload):
        pass

    def cancel_orders(self, order_hashes):
        pass

    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None):
        return []

    def get_trades(self, params=None, only_first_page=False, next_cursor=None):
        return []

    def get_balance_allowance(self, params=None):
        return {}

    def get_clob_market_info(self, condition_id):
        return {}


def make(resp=None, exc=None):
    return PolymarketExecutor(client=FuzzClient(resp, exc), builder_code=None)


jsonish = st.recursive(
    st.none() | st.booleans() | st.integers(-10**9, 10**9)
    | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=12),
    lambda c: st.lists(c, max_size=3)
    | st.dictionaries(st.text(max_size=8), c, max_size=3),
    max_leaves=8)
resp_dicts = st.dictionaries(
    st.one_of(st.sampled_from(["orderID", "success", "makingAmount",
                               "takingAmount", "error", "errorMsg", "status"]),
              st.text(max_size=8)),
    jsonish, max_size=6)
responses = st.one_of(jsonish, resp_dicts)


def assert_fail_closed(fill, resp):
    assert isinstance(fill, Fill)
    assert math.isfinite(fill.matched_shares) and fill.matched_shares >= 0.0
    assert math.isfinite(fill.matched_usd) and fill.matched_usd >= 0.0
    if fill:  # something was booked: the response must have earned it
        assert isinstance(resp, dict)
        assert resp.get("orderID") and resp.get("success") is not False


@given(resp=responses)
@settings(max_examples=250, deadline=None)
def test_market_path_books_only_confirmed_finite_amounts(resp):
    assert_fail_closed(make(resp).buy_fak("tok", 0.97, 5.0), resp)
    assert_fail_closed(make(resp).sell_fak("tok", 0.95, 5.0), resp)


@given(resp=responses)
@settings(max_examples=150, deadline=None)
def test_limit_path_books_only_confirmed_finite_amounts(resp):
    assert_fail_closed(make(resp).limit_gtc("tok", 0.50, 10.0, "BUY"), resp)


@given(status=st.one_of(st.none(), st.integers(min_value=100, max_value=599)),
       msg=st.text(max_size=30))
@settings(max_examples=150, deadline=None)
def test_api_error_partition_is_total(status, msg):
    """4xx is a clean rejection; everything else is OrderUncertain. No
    status code, message or None may produce a third behavior."""
    e = PolyApiException(error_msg=msg)
    e.status_code = status
    ex = make(exc=e)
    if status is not None and 400 <= status < 500:
        f = ex.buy_fak("tok", 0.97, 5.0)
        assert f.rejected and not f
    else:
        with pytest.raises(OrderUncertain):
            ex.buy_fak("tok", 0.97, 5.0)


@given(exc_type=st.sampled_from([RuntimeError, ValueError, OSError,
                                 KeyError, ConnectionError, TimeoutError]),
       msg=st.text(max_size=20))
@settings(max_examples=80, deadline=None)
def test_any_transport_exception_becomes_order_uncertain(exc_type, msg):
    ex = make(exc=exc_type(msg))
    with pytest.raises(OrderUncertain):
        ex.buy_fak("tok", 0.97, 5.0)
    with pytest.raises(OrderUncertain):
        ex.limit_gtc("tok", 0.50, 10.0, "BUY")


trade_rows = st.one_of(jsonish, st.dictionaries(
    st.one_of(st.sampled_from(["side", "size", "price", "status",
                               "trader_side", "maker_orders",
                               "maker_address", "matched_amount"]),
              st.text(max_size=8)),
    jsonish, max_size=6))


@given(trades=st.one_of(jsonish, st.lists(trade_rows, max_size=5)))
@settings(max_examples=250, deadline=None)
def test_trades_totals_is_finite_non_negative_or_none(trades):
    """Reconciliation truth under an adversarial tape: whatever get_trades
    returns, trades_totals yields finite non-negative totals or None
    (truth unavailable), and never raises."""
    class TapeClient(FuzzClient):
        def get_trades(self, params=None, only_first_page=False,
                       next_cursor=None):
            return trades
    out = PolymarketExecutor(client=TapeClient(),
                             builder_code=None).trades_totals("0xc")
    if out is not None:
        assert all(math.isfinite(v) and v >= 0.0 for v in out)
