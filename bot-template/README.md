# pmq bot template

A complete Polymarket bot minus the strategy, for ANY market: politics,
sports, crypto, culture. The engine (`bot.py`) is market-agnostic: it tracks
whatever slugs your `strategy.py` returns, executes your intents through the
pmq fail-closed layer, scores at resolution, and enforces the risk rails.
You write two small functions.

## What is deliberately NOT here

A trading edge, or even a direction to look for one. The shipped demo
strategy buys 2$ of any 0.98+ favorite once per market: an API illustration
with negative expectancy, present only so paper mode has something to show.
The engine is the part that is the same for everyone; the strategy is the
part that is not.

## Quickstart (paper, safe anywhere)

```bash
pip install pmq
BOT_SLUGS="<any-gamma-market-slug>" python bot.py 24
```

Fills land in `bot_runs/fills.csv`, resolved markets in
`bot_runs/windows.csv`, and `python dash/bot_dash.py` serves a phone
dashboard on port 8080 reading those files.

## The two functions you own (`strategy.py`)

* `watchlist()` returns the gamma slugs to track right now. Static from an
  env var, discovered via the gamma API, or generated for recurring
  markets.
* `decide(pm, book_a, book_b, remaining_usd, side_held, state)` returns
  `None` or `(side_key, price_cap, usd)`. You see real-time books and the
  market metadata (`pm['outcome_a']`, `pm['end_ts']`, ...); the engine
  enforces budget with fee headroom, one side per market, poisoning,
  reconciliation and halts. A buggy strategy can lose its stake; it cannot
  un-guard the execution.

## The rails (engine-enforced, whatever your strategy does)

* Paper mode by default; live requires `BOT_MODE=live` AND `LIVE=1`.
* Nothing booked without exchange confirmation; unknown outcomes poison the
  market until reconciled from `get_trades`.
* `BOT_MAX_CONSEC_FAILS` failed buys in a row exit with code 42, and the
  systemd unit's `RestartPreventExitStatus=42` keeps that halt halted.
* Daily realized loss under `BOT_DAILY_HALT` stops entries; in live it is
  persisted to disk so a same-day restart stays stopped.
* Collateral checked before the first live order (deposit wallets need
  `POLY_SIG_TYPE=3`).

## Env reference

| Var | Default | Meaning |
|---|---|---|
| BOT_MODE | paper | paper or live (live also needs LIVE=1) |
| BOT_SLUGS | empty | comma-separated slugs for the demo watchlist |
| BOT_STAKE | 5 | $ budget per market, fees included |
| BOT_DAILY_HALT | -25 | UTC-day realized loss that halts |
| BOT_POLL | 2.5 | seconds between polls |
| BOT_SCORE_POLL | 60 | seconds between resolution checks |
| BOT_FEE_CATEGORY | crypto | fee schedule for budgeting and scoring |
| BOT_OUT_DIR | ./bot_runs | CSV output directory |
| BOT_MAX_CONSEC_FAILS | 10 | failed buys in a row before exit 42 |
| BOT_MAX_TRACK_HOURS | 48 | drop unresolved markets after this long |
