import json

from pmq import (
    band_ask_depth_usd,
    best_bid_ask,
    book_inferred_winner,
    fee,
    parse_market,
    resolved_winner,
)


def test_fee_matches_official_formula():
    assert fee(0.50, 100) == 0.07 * 0.5 * 0.5 * 100
    assert abs(fee(0.95, 100) - 0.3325) < 1e-9
    assert fee(0.50, 100, rate=0.0) == 0.0
    assert fee(0.50, 100, rate=0.03) == 0.03 * 25


def test_best_bid_ask_sums_sizes_at_level():
    book = {"bids": [{"price": "0.90", "size": "10"}, {"price": "0.90", "size": "5"},
                     {"price": "0.85", "size": "99"}],
            "asks": [{"price": "0.93", "size": "7"}, {"price": "0.95", "size": "3"}]}
    bb, bbs, ba, bas = best_bid_ask(book)
    assert (bb, bbs, ba, bas) == (0.90, 15.0, 0.93, 7.0)


def test_best_bid_ask_empty_book():
    assert best_bid_ask(None) == (None, None, None, None)
    assert best_bid_ask({"bids": [], "asks": []}) == (None, None, None, None)


def test_band_ask_depth_usd():
    book = {"asks": [{"price": "0.92", "size": "10"}, {"price": "0.98", "size": "100"}]}
    assert band_ask_depth_usd(book, 0.90, 0.97) == 9.2
    assert band_ask_depth_usd(None, 0, 1) == 0


def _gamma_market(outcomes=("Up", "Down"), prices=("0.999", "0.001")):
    return {"conditionId": "0xc0nd", "outcomes": json.dumps(list(outcomes)),
            "clobTokenIds": json.dumps(["111", "222"]),
            "outcomePrices": json.dumps(list(prices))}


def test_parse_market_updown_and_yesno():
    pm = parse_market(_gamma_market())
    assert pm["condition_id"] == "0xc0nd"
    assert pm["token_a"] == "111" and pm["token_b"] == "222"
    pm2 = parse_market(_gamma_market(outcomes=("No", "Yes")), "Yes", "No")
    assert pm2["token_a"] == "222" and pm2["token_b"] == "111"


def test_parse_market_generic_outcomes_and_end_ts():
    m = _gamma_market(outcomes=("G2 Esports", "Top Esports"))
    m["endDate"] = "2026-07-04T12:00:00Z"
    pm = parse_market(m)
    assert pm["outcome_a"] == "G2 Esports" and pm["outcome_b"] == "Top Esports"
    assert pm["end_ts"] == 1783166400
    assert resolved_winner(pm) == "G2 Esports"


def test_parse_market_fails_closed():
    assert parse_market(None) is None
    assert parse_market({"outcomes": "not json"}) is None


def test_resolved_winner():
    assert resolved_winner(parse_market(_gamma_market())) == "Up"
    assert resolved_winner(parse_market(_gamma_market(prices=("0.6", "0.4")))) is None
    assert resolved_winner(None) is None


def test_book_meta_reads_exchange_rules_from_the_book():
    from pmq import book_meta
    meta = book_meta({"min_order_size": "5", "tick_size": "0.001",
                      "neg_risk": True, "last_trade_price": "0.123"})
    assert meta == {"min_order_size": 5.0, "tick_size": 0.001,
                    "neg_risk": True, "last_trade_price": 0.123}
    assert book_meta(None)["min_order_size"] is None


def test_book_inferred_winner():
    assert book_inferred_winner(0.95, 0.02) == "a"
    assert book_inferred_winner(0.02, 0.91) == "b"
    assert book_inferred_winner(0.60, 0.35) is None
    assert book_inferred_winner(None, None) is None


def test_http_get_json_success_and_permanent_failure(monkeypatch):
    import io
    import urllib.request

    from pmq import data as d

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=10: FakeResp(b'{"a": 1}'))
    assert d.http_get_json("http://x") == {"a": 1}

    def boom(req, timeout=10):
        raise OSError("down")
    logged = []
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    assert d.http_get_json("http://x", logger=logged.append) is None
    assert logged and "permanently" in logged[0]


def test_get_market_falls_back_to_events(monkeypatch):
    from pmq import data as d
    calls = []

    def fake_get(url, logger=None):
        calls.append(url)
        if "/markets" in url:
            return []
        return [{"markets": [{"conditionId": "0xevt"}]}]
    monkeypatch.setattr(d, "http_get_json", fake_get)
    assert d.get_market("expired-slug")["conditionId"] == "0xevt"
    assert len(calls) == 2

    monkeypatch.setattr(d, "http_get_json", lambda url, logger=None: None)
    assert d.get_market("nope") is None


def test_event_markets_skips_unparseable_members(monkeypatch):
    from pmq import data as d
    ev = [{"markets": [_gamma_market(), {"outcomes": "not json"}]}]
    monkeypatch.setattr(d, "http_get_json", lambda url, logger=None: ev)
    out = d.event_markets("some-event")
    assert len(out) == 1 and out[0]["condition_id"] == "0xc0nd"
    monkeypatch.setattr(d, "http_get_json", lambda url, logger=None: None)
    assert d.event_markets("nope") == []


def test_positions_empty_on_unreachable_api(monkeypatch):
    from pmq import data as d
    monkeypatch.setattr(d, "http_get_json", lambda url, logger=None: None)
    assert d.positions("0x" + "a" * 40) == []


def test_get_tape_paginates_until_since_ts(monkeypatch):
    from pmq import data as d
    pages = [[{"timestamp": 200}, {"timestamp": 150}],
             [{"timestamp": 90}],
             [{"timestamp": 10}]]

    def fake_get(url, logger=None):
        return pages.pop(0) if pages else []
    monkeypatch.setattr(d, "http_get_json", fake_get)
    tape = d.get_tape("0xc", since_ts=100)
    # stops after the page whose oldest trade predates since_ts
    assert [t["timestamp"] for t in tape] == [200, 150, 90]


def test_package_lazy_exports():
    import pmq
    assert pmq.Fill is not None  # lazy, pulls the executor module
    assert pmq.DEFAULT_BUILDER_CODE.startswith("0x")
    import pytest as _pytest
    with _pytest.raises(AttributeError):
        pmq.does_not_exist


def test_doctor_pure_logic():
    from pmq.doctor import advise_sig_type, looks_like_minimal_proxy
    assert looks_like_minimal_proxy("0x363d3d373d3d363d6020366004")
    assert not looks_like_minimal_proxy("0x")
    assert not looks_like_minimal_proxy(None)
    assert advise_sig_type(False, False, True)[0] == 0
    assert advise_sig_type(True, True, False)[0] == 3
    assert advise_sig_type(True, False, False)[0] is None
    assert advise_sig_type(False, False, False)[0] is None
