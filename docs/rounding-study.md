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
3. Limit-path fills never exceeded the signed size in these tests. The
   overfill reports (filled 5.051 on a size-5 order) are consistent with the
   MARKET-order path instead, where the contract is "spend this amount" and
   the share count is the division remainder of amount by price.
4. The per-market minimum size is enforced server-side with a clean 400, and
   is readable in advance from the book response (`min_order_size`, exposed
   by `pmq.book_meta`).
