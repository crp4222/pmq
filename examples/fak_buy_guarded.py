#!/usr/bin/env python3
"""Minimal guarded live FAK buy. Reads POLY_PRIVATE_KEY, POLY_FUNDER,
POLY_SIG_TYPE from the environment and refuses to run without LIVE=1.

Usage: LIVE=1 python fak_buy_guarded.py <token_id> <price_cap> <usd>"""
import os
import sys

from pmq import OrderUncertain, PolymarketExecutor

if os.environ.get("LIVE") != "1":
    raise SystemExit("refusing: set LIVE=1 to place a real order")
token_id, price_cap, usd = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])

ex = PolymarketExecutor()
print(f"collateral: {ex.require_collateral(usd):.2f} USDC")

try:
    fill = ex.buy_fak(token_id, price_cap, usd)
except OrderUncertain as e:
    print(f"outcome UNKNOWN ({e}); reconciling before anything else")
    raise SystemExit(1)

if fill:
    print(f"matched {fill.matched_shares} sh at {fill.price:.4f}, order {fill.order_id}")
elif fill.rejected:
    print(f"clean rejection, nothing booked: {fill.error}")
else:
    print(f"accepted but zero matched (order {fill.order_id}), nothing booked")
