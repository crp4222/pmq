# What actually breaks when your Polymarket bot meets CLOB V2

*Field notes from taking a small bot live on Polymarket's CLOB V2 in July
2026. Every error message below is verbatim from production. If you landed
here from a search engine with one of them, jump to its section; the fix is
included. The lessons are encoded in [pmq](https://github.com/crp4222/pmq).*

Polymarket switched its exchange to CLOB V2 on 2026-04-28. V1-signed orders
are rejected outright, the order struct changed (no more `nonce`,
`feeRateBps`, `taker`; new `timestamp`, `metadata`, `builder`), fees are
decided at match time, and most tutorials, bots and MCP servers you will find
online still speak V1. Here is what that migration looks like from the
trenches, error by error.

## 1. `invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals, taker amount a max of 4 decimals`

**HTTP 400 on `POST /order`.** You built a perfectly reasonable FAK limit
order: price rounded to the tick, size rounded to two decimals. The exchange
still rejects it.

The trap: the CLOB treats FAK and FOK **buys as market orders**. For a market
buy, the maker amount is the USDC you spend, and it must have at most 2
decimals. A limit-style `price * size` multiplication produces up to 5
decimals of maker amount, so the order is structurally invalid no matter how
you round price and size individually.

The fix: do not build FAK buys with the limit-order path
(`create_and_post_order`). Use the market-order path:

```python
from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType

client.create_and_post_market_order(
    MarketOrderArgsV2(token_id=tok, amount=4.99, side="BUY", price=0.95),
    order_type=OrderType.FAK)
```

`amount` is dollars for a BUY (shares for a SELL), `price` is your cap. The
builder inside the client rounds the maker amount down to the cent and brings
the taker amount to the allowed accuracy. Round your dollar amount DOWN
yourself if you manage a budget: `usd = int(usd * 100) / 100`.

## 2. `no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.`

**HTTP 400, and yet the body contains an `orderID`.** This one misleads
people into retry loops. It is not an error state: your FAK order was
accepted, found nothing to match against (the ask you saw two seconds ago is
gone), and was killed. Nothing rests, nothing filled, nothing to cancel.

Treat it as a clean no-fill and move on. In a poll loop, do not book
anything, do not panic-cancel, and count it toward whatever failure budget
halts your bot if the book keeps evaporating in front of you.

## 3. The CLOB says `balance: 0` while your money sits on-chain

You funded the account, the app shows the balance, a manual trade works, and
still `get_balance_allowance(...COLLATERAL)` returns zero. You double-check
your `funder` parameter. Still zero. For every `signature_type` you try
except one.

Two facts explain it:

* The balance endpoint **never sends your `funder`** upstream. Look at the
  client source: the query carries `signature_type`, an asset type and a
  token id. The server derives the funding wallet itself from your
  authenticated EOA and the `signature_type`.
* The Polymarket app's default wallet is a **deposit wallet**: an ERC-1167
  minimal proxy validating signatures via ERC-1271, controlled by your EOA.
  That is `signature_type=3` (`POLY_1271`), added in V2 alongside 0 (EOA),
  1 (`POLY_PROXY`) and 2 (`POLY_GNOSIS_SAFE`).

So if your funds live in the app's wallet and you authenticate with
`signature_type=1`, the server derives a proxy address that holds nothing,
and truthfully reports zero.

Debugging trick that settles it in one RPC call: `eth_call` `owner()`
(selector `0x8da5cb5b`) on your funder address. If it returns your EOA, the
wallet is yours, and if its bytecode is a 45-byte-ish ERC-1167 proxy, it is a
deposit wallet: use `signature_type=3` and keep `funder` set to that address
for order building.

We verified the full chain on-chain afterward: orders signed with sig type 3
from a deposit wallet match, settle to the CTF Exchange V2 contract, and
carry the builder code in calldata.

## 4. Trust the installed package, not the docs

The V2 examples floating around (and some official snippets) use parameter
names that the installed `py-clob-client-v2` does not have. The reliable
workflow is to introspect what you actually installed:

```python
import inspect
from py_clob_client_v2.client import ClobClient
print(inspect.signature(ClobClient.create_and_post_market_order))
```

Everything pmq does was written against introspected signatures, and its
executor re-checks that surface at startup, refusing to trade if the
installed client drifted. A changed default is invisible in a diff of your
own code and can turn a guard into a no-op.

## 5. Fees are not what the API response suggests

The `maker_base_fee` and `taker_base_fee` of 1000 bps you see in market
objects are on-chain caps, never the charge. The actual schedule (official
docs, 2026-07-03): `fee = rate * price * (1 - price) * shares`, with rate
0.07 for crypto up/down, 0.03 sports, 0.04 finance and politics, 0.05
economics and culture and weather, 0 geopolitics. Makers pay zero. The
parabola matters for strategy math: a taker fill at 0.95 costs about a third
of one at 0.50 per share.

Since V2, the fee is decided at match time and no longer rides in the signed
order, so your budget math must reserve headroom: at price `p` a share
costs you `p + rate * p * (1 - p)`, not `p`.

## 6. Assorted sharp edges

* The data-api trade tape lags matching by 1 to 3 minutes. Score offline
  with it, never trade off it; the `/book` endpoint is the real-time source.
* Gamma `/markets?slug=` returns an empty list for expired short-lived
  markets; `/events?slug=` still resolves them.
* Gamma settlement can lag a close by 15+ minutes. The last pre-close book
  identifies the winner immediately: whichever side's bid is pinned at 0.90+.
* Builder attribution changed with V2: the old header trio attributes
  nothing. The bytes32 builder code rides inside the signed order. You can
  verify any fill's attribution yourself by grepping the settlement
  transaction calldata for the code.

## The shape of a bot that survives

The unifying principle behind all of the above: **fail closed**. Book a fill
only when the exchange said so and only for the size it said. Treat unknown
outcomes (timeouts, 5xx) as radioactive: cancel, reconcile from `get_trades`,
and only then resume. Halt after consecutive failures. Check collateral
before starting. Persist risk halts to disk so a restart does not amnesia
your way back into the market.

That is exactly the layer [pmq](https://github.com/crp4222/pmq) provides,
with the strategy left to you.
