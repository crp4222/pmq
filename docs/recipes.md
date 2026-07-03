# Recipes

Copy-paste starting points. Everything below is Python against `pip install
pmquant` (`import pmq`); execution snippets read `POLY_PRIVATE_KEY`,
`POLY_FUNDER`, `POLY_SIG_TYPE` from the environment and never print them.

## Trade a political market in 20 lines

```python
import pmq

# discover: any gamma slug works (politics, sports, crypto)
pm = pmq.parse_market(pmq.get_market("mississippi-gubernatorial-election-presley-d-vs-reeves-r"))
book = pmq.get_book(pm["token_a"])                    # real-time, trustable
bid, bid_sz, ask, ask_sz = pmq.best_bid_ask(book)
rules = pmq.book_meta(book)                            # min_order_size, tick_size

ex = pmq.PolymarketExecutor()                          # sig_type 3 = app wallet
ex.require_collateral(10)
if ask and ask * rules["min_order_size"] <= 10:
    fill = ex.buy_fak(pm["token_a"], price_cap=ask, usd=10.0)
    if fill:                                           # book ONLY what matched
        print(f"bought {fill.matched_shares} {pm['outcome_a']} at {fill.price:.3f}")
```

## Scan a multi-outcome event (election, tournament)

```python
import pmq

total_asks = 0.0
for pm in pmq.event_markets("world-cup-winner"):
    _, _, ask, _ = pmq.best_bid_ask(pmq.get_book(pm["token_a"]))
    if ask is None:
        print(f"{pm['outcome_a']:24s} unquoted (basket incomplete)")
        continue
    total_asks += ask
    print(f"{pm['outcome_a']:24s} ask {ask}")
print("sum of asks:", round(total_asks, 4), "(a full basket below 1 - fees pays 1)")
```

## What do I hold?

```python
import os
import pmq

for p in pmq.positions(os.environ["POLY_FUNDER"]):
    print(p["slug"], p["outcome"], p["size"], "avg", p["avgPrice"],
          "now", p.get("curPrice"), "value", p.get("currentValue"))
```

## Paper-test a strategy on any market

Use [bot-template/](../bot-template/): implement `watchlist()` and
`decide()` in `strategy.py`, run `python bot.py 24`, read
`bot_runs/windows.csv`. Paper fills execute against the REAL ask, capped by
displayed size and the per-market exchange minimum, and are scored with the
real fee at resolution: if it does not survive paper, it will not survive
live.

## Budget with the real fee

```python
import pmq

ex = pmq.PolymarketExecutor()
rate = ex.fee_rate(pm["condition_id"])       # authoritative, from the exchange
cost_per_share = ask + pmq.fee(ask, 1.0, rate)
shares_affordable = budget_usd / cost_per_share
```

## Verify builder attribution on-chain

```python
import pmq

sh, usd, fees = ex.trades_totals(pm["condition_id"])   # your fills, exchange truth
# every matched order settles on the CTF Exchange V2; the bytes32 builder
# code rides in the calldata. Grep any of your settlement transactions:
# https://polygonscan.com/tx/<transactionHash from get_trades>
# and search the input data for your builder code (without the 0x prefix).
```

## After a timeout or 5xx

```python
try:
    fill = ex.buy_fak(token, cap, usd)
except pmq.OrderUncertain:
    sh, usd_spent, fees = ex.reconcile(pm["condition_id"], token)
    # book sh/usd_spent, nothing else, and only now place new orders
```
