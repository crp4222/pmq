# CLOB V2 order rounding, measured

*Controlled experiment against production, 2026-07-03, py-clob-client-v2
1.0.2, total risk budget 10 USD (net result of the run: +0.39). Raw data
posted on the client's issues
[#89](https://github.com/Polymarket/py-clob-client-v2/issues/89#issuecomment-4875871905)
and [#66](https://github.com/Polymarket/py-clob-client-v2/issues/66#issuecomment-4875871996).*

## Method

Phase A: resting GTC bids far below the touch (cannot fill, cancelled after
each case, zero cost), submitted with adversarial sizes and prices, then read
back through `get_open_orders` to see what the server actually recorded.
Phase B: two marketable limit buys on a liquid market to observe matched
amounts against requested size, cross-checked with `get_trades`.

## Results

| Case | Requested | Server-side view |
|---|---|---|
| Baseline 2-decimal size | 5.25 @ 0.05 | 5.25, exact |
| 4-decimal size | 5.2537 | **5.25, silently rounded down** |
| Float drift | 5.100000000000001 | 5.1, normalized |
| Price finer than tick | 0.0515 (tick 0.01) | **0.05, silently rounded down** |
| Sub-cent notional | 5.007 @ 0.03 | accepted, size recorded as 5.00 |
| Below minimum size | size 4 (min 5) | HTTP 400: `Size (4) lower than the minimum: 5` |
| Marketable fill, odd size | 5.1234 @ 0.96 | signed as 5.12; matched **exactly 5.12** shares for 4.9152 USDC (0.96 x 5.12 to the cent) |
| Marketable fill, control | 5.0 @ 0.96 | matched exactly 5.00 for 4.80 USDC |

## What this means

1. On 1.0.2, every input is normalized CLIENT-side (rounded DOWN to the
   allowed decimals per the tick's RoundConfig) before signing. Float-drift
   artifacts never reach the wire.
2. The normalization is **silent**. If your own accounting keeps the
   unrounded size or price, it diverges from what the exchange signed. Book
   from the response's matched amounts (what pmq's `Fill` does), never from
   your request.
3. Limit-path fills never exceeded the signed size in these tests because
   they matched AT the limit price. The mechanism behind the overfill
   reports (credit to gmoutsin in
   [#89](https://github.com/Polymarket/py-clob-client-v2/issues/89)): the
   signed V2 order is an amounts PAIR (makerAmount USDC, takerAmount
   tokens), a ratio with a worst-case bound, not a (price, size) tuple.
   Under price improvement the engine preserves your dollars and returns
   proportionally MORE tokens (4.95 at a 0.98 ask = 5.051 tokens on a
   "size 5" order at 0.99). Strictly favorable, but it breaks size-based
   accounting: book from the matched amounts, and never rely on a
   marketable limit to cap token count exactly.
4. The per-market minimum size is enforced server-side with a clean 400, and
   is readable in advance from the book response (`min_order_size`, exposed
   by `pmq.book_meta`).

## Addendum (2026-07-04): fine-tick market orders

The study above ran on a 0.01-tick market and point 1 turns out to hold
only there. On 2026-07-04 a 0.001-tick market rejected every market buy
with `invalid amounts ... taker amount a max of 4 decimals`: the client's
ROUNDING_CONFIG allows amount decimals = price decimals + 2 (5 for tick
0.001, 6 for 0.0025 and 0.0001). That is right for LIMIT orders, whose
amounts are exact price×size products, but MARKET-order takers are capped
by the server at a flat 4 decimals whatever the tick, so on fine ticks the
maker/price division leaves a 5th decimal and the order can never be
accepted. Normalization still happens client-side; it just normalizes to a
precision the server refuses.

pmq 0.4.3 clamps the market path to 4 decimals (round-down: the budget
contract is intact, the dust given up is under 0.0001 share). pmq 0.4.5
also refuses at startup any client build that would still sign such a
pair, so this class of failure stops at deploy time, not at trade time.
