"""Property fuzz of the book parsers, the read-side twin of
test_fill_fuzz.py: whatever shape /book returns (malformed levels,
NaN/inf/negative/out-of-range prices and sizes, wrong container types),
best_bid_ask, band_ask_depth_usd and book_meta never raise, never surface
a non-finite number, and never surface an out-of-range price."""
import math

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from pmq.data import band_ask_depth_usd, best_bid_ask, book_meta  # noqa: E402

jsonish = st.recursive(
    st.none() | st.booleans() | st.integers(-10**9, 10**9)
    | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=12),
    lambda c: st.lists(c, max_size=3)
    | st.dictionaries(st.text(max_size=8), c, max_size=3),
    max_leaves=8)

numish = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(allow_nan=True, allow_infinity=True).map(str),
    st.sampled_from(["NaN", "Infinity", "-Infinity", "0.5", "0.93", "1.7",
                     "-0.2", "0", "1", "", "x", None]),
    jsonish)

valid_level = st.fixed_dictionaries(
    {"price": st.floats(min_value=0.0, max_value=1.0),
     "size": st.floats(min_value=0.0, max_value=1e9)})
level = st.one_of(valid_level, jsonish, st.dictionaries(
    st.one_of(st.sampled_from(["price", "size"]), st.text(max_size=6)),
    numish, max_size=4))
side = st.one_of(jsonish, st.lists(level, max_size=6))
book = st.one_of(jsonish, st.fixed_dictionaries({}, optional={
    "bids": side, "asks": side, "min_order_size": numish,
    "tick_size": numish, "neg_risk": jsonish, "last_trade_price": numish}))


@given(book=book,
       lo=st.floats(allow_nan=True, allow_infinity=True),
       hi=st.floats(allow_nan=True, allow_infinity=True))
@settings(max_examples=300, deadline=None)
def test_book_parsers_never_raise_never_surface_invalid(book, lo, hi):
    bb, bb_sz, ba, ba_sz = best_bid_ask(book)
    for p in (bb, ba):
        assert p is None or (math.isfinite(p) and 0.0 <= p <= 1.0)
    for s in (bb_sz, ba_sz):
        assert s is None or (math.isfinite(s) and s >= 0.0)
    if bb is None:
        assert bb_sz is None    # a size can never exist without its quote
    if ba is None:
        assert ba_sz is None

    depth = band_ask_depth_usd(book, lo, hi)
    assert math.isfinite(depth) and depth >= 0.0

    meta = book_meta(book)
    for key, hi_bound in (("min_order_size", math.inf),
                          ("tick_size", 1.0), ("last_trade_price", 1.0)):
        v = meta[key]
        assert v is None or (math.isfinite(v) and 0.0 <= v <= hi_bound)
    assert meta["neg_risk"] is None or isinstance(meta["neg_risk"], bool)
