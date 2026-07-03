#!/usr/bin/env python3
"""Read-only tour: resolve a market, read its real-time book, price a fill.
Needs no credentials. Usage: python read_market.py <gamma-slug>"""
import sys

import pmq

slug = sys.argv[1] if len(sys.argv) > 1 else "btc-updown-15m"
m = pmq.parse_market(pmq.get_market(slug))
if not m:
    raise SystemExit(f"no market for slug {slug!r}")

book = pmq.get_book(m["token_a"])
bid, bid_sz, ask, ask_sz = pmq.best_bid_ask(book)
print(f"condition {m['condition_id']}")
print(f"{m['outcome_a']}: bid {bid} ({bid_sz} sh), ask {ask} ({ask_sz} sh)")
print(f"ask depth in [0.90, 0.97]: ${pmq.band_ask_depth_usd(book, 0.90, 0.97)}")
if ask:
    print(f"taker fee for 10 shares at {ask}: ${pmq.fee(ask, 10):.4f}")
print(f"settled winner: {pmq.resolved_winner(m) or 'not settled yet'}")
