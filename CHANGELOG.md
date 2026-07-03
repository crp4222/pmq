# Changelog

## 0.3.0 (2026-07-03)

* New: `pmq-doctor`, a read-only diagnosis command for Polymarket V2 setups.
  Checks the installed py-clob-client-v2 against the verified surface,
  derives the EOA from `POLY_PRIVATE_KEY` (never printed), reads the funder
  on-chain (contract vs EOA, `owner()`), advises the right `POLY_SIG_TYPE`,
  verifies the CLOB sees collateral with the configured identity (and probes
  the other signature types when it does not), and optionally checks one
  market (`--market <slug>`: book, min_order_size, tick, taker fee).
* Docs: docs/recipes.md cookbook (trade a market, paper-test a strategy,
  read positions, verify builder attribution on-chain), demo card in the
  README.
* CI: the publish workflow refuses to upload when the release tag does not
  match the version in pyproject.toml (the failure mode that blocked the
  first 0.3.0 release).

## 0.2.0 (2026-07-03)

* New: `positions(user)` and `event_markets(slug)` in the data layer; `event`
  tool in the MCP server; `fee_rate(condition_id)` (authoritative per-market
  taker rate from the exchange) and `cancel_order(order_id)` on the executor.
* Trust: weekly live canary workflow (real-endpoint checks, auto-opens an
  issue on drift), SECURITY.md, production receipt in the README,
  docs/rounding-study.md (measured V2 rounding behavior).
* Quality: ruff in CI, tests for the bot-template engine (34 tests total).

## 0.1.0 (2026-07-03)

First release.

* `pmq.data`: real-time books (with per-market exchange rules via
  `book_meta`), gamma slug resolution with expired-market fallback,
  market-agnostic `parse_market` (any binary outcomes, close time), settled
  and book-inferred winners, offline trade tape, official per-category taker
  fee formula.
* `pmq.executor.PolymarketExecutor`: fail-closed CLOB V2 execution. FAK
  buys/sells through the market-order path, exchange-confirmed fills only,
  `OrderUncertain` + `reconcile()` from get_trades, deposit-wallet
  (POLY_1271) support, collateral fail-fast, builder-code default with
  disclosure and opt-out, startup introspection of the installed
  py-clob-client-v2.
* `pmq.mcp` (`pmq-mcp`): MCP server. Read tools always available
  (find_markets, market, book, taker_fee, account tools); trading tools only
  registered when the operator sets `PMQ_MCP_LIVE=1`, per-order cap via
  `PMQ_MCP_MAX_USD`.
* `bot-template/`: market-agnostic bot engine (strategy owns `watchlist()`
  and `decide()`), honest paper mode against real books, risk rails (budget
  with fee headroom, poisoning, halts with exit code 42, disk-persisted
  daily halt), systemd unit, phone dashboard.
