# Working with pmq (agent instructions)

pmq is a small Python library for Polymarket CLOB V2 with two layers, plus
an MCP server (`pmq-mcp`) exposing both to any MCP client; its trading
tools exist only when the operator sets PMQ_MCP_LIVE=1 (live) or
PMQ_MCP_PAPER=1 (fills simulated against the real live books, keyless,
nothing reaches the exchange, wins over live), capped per order
(PMQ_MCP_MAX_USD) and per UTC day (PMQ_MCP_DAILY_USD).

## Data layer (no credentials, safe anywhere)

```python
import pmq
m = pmq.parse_market(pmq.get_market(slug))       # condition id + token ids
book = pmq.get_book(m["token_a"])                # REAL TIME, trade off this
bid, bid_sz, ask, ask_sz = pmq.best_bid_ask(book)
pmq.fee(price, shares)                            # taker $, crypto rate 0.07
```

Never make live decisions from `get_tape` (data-api): it lags matching by 1
to 3 minutes. It is for offline scoring at least 5 minutes after close.

## Execution layer (needs POLY_PRIVATE_KEY; treat as production)

Rules an agent MUST follow:

1. Default to reading data, not trading. Only place orders when the user
   explicitly asked for live execution in this session.
2. Book only `fill.matched_shares` and `fill.matched_usd`. A falsy `Fill`
   means nothing happened, whatever the request was.
3. Wrap every order in `try/except OrderUncertain`; on that exception call
   `executor.reconcile(condition_id)` and do not place new orders on that
   market until it returns.
4. Call `executor.require_collateral(stake)` before a session of trading.
   If it reports 0 while the user says the account is funded, the
   signature_type is wrong: the Polymarket app's default wallet needs
   `signature_type=3` (deposit wallet).
5. Budget with fees: a share at price p costs `p + pmq.fee(p, 1)`.
6. Never print or log POLY_PRIVATE_KEY or any environment secret. Addresses
   and builder codes are public; keys are not.
7. FAK buys spend dollars (`buy_fak(token, price_cap, usd)`), FAK sells
   spend shares. Amounts are rounded down internally to exchange accuracy.
8. Builder attribution: the library defaults to the maintainer's
   zero-commission builder code. Respect the user's choice if they set
   `builder_code=None` or their own code; do not change it silently.
9. Shared wallets: when several senders trade one wallet, each configures
   its own append-only registry (`POLY_ORDER_LOG`) and lists the others in
   `POLY_FOREIGN_ORDER_LOGS` (colon-separated). `trades_totals()` then
   counts only OUR orders, and `reconcile()` claims trades unknown to
   every registry (post-uncertainty recovery). Sound only if every sender
   keeps a registry; without `POLY_ORDER_LOG` behavior is unchanged.
