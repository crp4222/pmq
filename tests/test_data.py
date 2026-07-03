import json

from pmq import (band_ask_depth_usd, best_bid_ask, book_inferred_winner, fee,
                 parse_market, resolved_winner)


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
