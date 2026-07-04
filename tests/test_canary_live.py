"""Live canary: exercises the REAL Polymarket endpoints and the installed
py-clob-client-v2 surface. Runs only when PMQ_CANARY=1 (weekly scheduled
workflow); regular CI stays offline. A failure here means Polymarket or the
client drifted, not that pmq broke."""
import os

import pytest

import pmq

pytestmark = pytest.mark.skipif(os.environ.get("PMQ_CANARY") != "1",
                                reason="live canary runs on schedule only")


def _top_market():
    evs = pmq.http_get_json(
        f"{pmq.data.GAMMA}/events?closed=false&order=volume24hr"
        f"&ascending=false&limit=1")
    assert evs, "gamma events endpoint returned nothing"
    pm = pmq.parse_market(evs[0]["markets"][0])
    assert pm and pm["token_a"] and pm["condition_id"]
    return pm


def test_gamma_and_slug_resolution():
    pm = _top_market()
    again = pmq.parse_market(pmq.get_market(pm["slug"]))
    assert again and again["condition_id"] == pm["condition_id"]


def test_book_shape_and_exchange_rules():
    pm = _top_market()
    book = pmq.get_book(pm["token_a"])
    assert book, "CLOB book endpoint returned nothing"
    bid, bid_sz, ask, ask_sz = pmq.best_bid_ask(book)
    assert (bid is not None) or (ask is not None), "book has no quotes at all"
    meta = pmq.book_meta(book)
    assert meta["min_order_size"] and meta["tick_size"], \
        "book no longer carries min_order_size/tick_size"


def test_positions_endpoint_shape():
    rows = pmq.positions("0x0000000000000000000000000000000000000001")
    assert isinstance(rows, list)


def test_installed_client_surface_still_matches():
    import inspect

    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType

    from pmq.executor import _EXPECTED_MARKET_ARGS, _EXPECTED_METHODS
    for name, params in _EXPECTED_METHODS.items():
        fn = getattr(ClobClient, name, None)
        assert fn is not None, f"client lost method {name}"
        have = set(inspect.signature(fn).parameters)
        for p in params:
            assert p in have, f"{name}() lost parameter {p}"
    have = set(inspect.signature(MarketOrderArgsV2).parameters)
    for p in _EXPECTED_MARKET_ARGS:
        assert p in have, f"MarketOrderArgsV2 lost field {p}"
    assert hasattr(OrderType, "FAK")


def test_market_info_fee_field():
    pm = _top_market()
    from py_clob_client_v2.client import ClobClient
    c = ClobClient("https://clob.polymarket.com", chain_id=137)
    mi = c.get_clob_market_info(pm["condition_id"])
    assert isinstance(mi, dict) and "fd" in mi and "r" in mi["fd"], \
        "get_clob_market_info no longer exposes fd.r (authoritative fee rate)"


def test_egress_only_polymarket_hosts(monkeypatch):
    """THE claim, made executable: keys cannot leak to a third party because
    no third party is ever contacted. Records every DNS resolution during a
    full session (market data, auth derivation, one signed order sent with a
    throwaway zero-fund key) and fails on any host outside polymarket.com.
    The weekly CI run prints the observed list in public logs."""
    import socket

    from eth_account import Account

    hosts: set[str] = set()
    real = socket.getaddrinfo

    def spy(host, *args, **kwargs):
        hosts.add(str(host))
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    pm = _top_market()
    pmq.get_book(pm["token_a"])
    ex = pmq.PolymarketExecutor(key=Account.create().key.hex(),
                                signature_type=0, funder=None)
    assert ex.collateral() == 0.0            # throwaway key, zero funds
    try:
        fill = ex.buy_fak(pm["token_a"], price_cap=0.99, usd=1.0)
        assert fill.rejected, "a zero-fund order must be cleanly rejected"
    except pmq.OrderUncertain:
        pass                                  # 5xx path; egress is the point
    print("hosts contacted:", sorted(hosts))
    foreign = {h for h in hosts
               if h != "polymarket.com" and not h.endswith(".polymarket.com")}
    assert not foreign, f"unexpected egress: {sorted(foreign)}"
