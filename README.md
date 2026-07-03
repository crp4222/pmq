# pmq

<!-- mcp-name: io.github.crp4222/pmq -->

[![PyPI](https://img.shields.io/pypi/v/pmquant)](https://pypi.org/project/pmquant/)
[![tests](https://github.com/crp4222/pmq/actions/workflows/test.yml/badge.svg)](https://github.com/crp4222/pmq/actions/workflows/test.yml)
[![canary](https://github.com/crp4222/pmq/actions/workflows/canary.yml/badge.svg)](https://github.com/crp4222/pmq/actions/workflows/canary.yml)
[![coverage gate](https://img.shields.io/badge/coverage-%E2%89%A585%25%20enforced%20in%20CI-blue)](.github/workflows/test.yml)
[![typed](https://img.shields.io/badge/types-mypy%20strict-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Fail-closed execution and market data for **Polymarket CLOB V2**, in Python.
Local signing (your keys never leave your process), exchange-confirmed fills
only, fee-correct math, and deposit-wallet (`POLY_1271`) support that actually
works in production.

```bash
pip install pmquant        # distribution name pmquant, import name pmq
```

(PyPI's similarity check reserves the bare name; the module you import is
`pmq`, same pattern as beautifulsoup4/bs4.)

As of 2026-07-03 this is, to our knowledge, the **only maintained Python
layer combining local CLOB V2 signing, an exchange-confirmed fill contract,
and working deposit-wallet (POLY_1271) auth**. That claim is dated and
falsifiable: the comparison table below names the alternatives and what each
does instead; open an issue if it goes stale.

## Why this exists

Polymarket cut over to CLOB V2 on 2026-04-28. V1-signed orders are rejected in
production, the fee schedule is decided at match time, and the official client
examples leave several traps undocumented. Every line of pmq was paid for with
a real error in live trading:

* `invalid amounts, the market buy orders maker amount supports a max accuracy
  of 2 decimals`: the CLOB treats FAK/FOK buys as **market orders**. pmq routes
  them through the market-order builder with the correct rounding.
* `no orders found to match with FAK order` (HTTP 400, yet with an `orderID`):
  a clean no-fill, not an error. pmq returns an empty `Fill` instead of crashing
  or, worse, retrying blindly.
* CLOB shows `balance: 0` while your pUSD sits on-chain: the balance endpoint
  ignores your `funder` parameter and derives the wallet from your EOA and
  `signature_type`. Funds in the Polymarket app's default wallet (an ERC-1271
  deposit wallet) are only visible with `signature_type=3`.

The full write-up with reproduction details: [docs/war-story.md](docs/war-story.md).

## Runs in production

The maintainer's own bot trades through this exact executor 24/7 with real
money. Example receipt (2026-07-03): settlement transaction
[`0x387f5f09...100d88a8`](https://polygonscan.com/tx/0x387f5f09c031bb36a71c54adc978b1ed4d50c67f6dd3f0c2c8068391100d88a8)
on the CTF Exchange V2: a FAK market buy built by this library, matched and
settled, with the builder code visible in the calldata. Additionally, a weekly
[canary workflow](.github/workflows/canary.yml) exercises the real endpoints
and the installed client surface, and opens an issue by itself if Polymarket
drifts.

## pmq-doctor: diagnose your setup in one command

```bash
pip install pmquant && pmq-doctor --market <slug>
```

It checks, in order: the installed client surface (introspection), your
derived EOA, the funder wallet on-chain (`owner()` and bytecode: is it a
deposit wallet?), whether `POLY_SIG_TYPE` matches the wallet type, whether
the CLOB actually sees your collateral (and if not, WHICH sig_type does),
and the target market's minimum size and tick. Real output on a real
deposit-wallet account:

![pmq-doctor output](docs/assets/pmq-doctor.svg)

If you landed here from "the order signer address has to be the address of
the API KEY" or a CLOB balance of 0 with funds on-chain: this is the tool.

## The contract: nothing is booked without exchange confirmation

| Situation | What pmq does |
|---|---|
| Response is a dict with `orderID`, not flagged failed | `Fill` with the **matched** size read from the response |
| Error dict on HTTP 200, string body, `success: false` | `Fill(rejected=True)`, zero booked |
| HTTP 4xx (incl. FAK no-match) | `Fill(rejected=True)`, zero booked |
| Timeout, 5xx, exception after send | raises `OrderUncertain`: the order MAY exist. Call `reconcile()` before trading that market again |
| Unparseable matched amounts | zero booked (fail closed) |

`reconcile(condition_id)` cancels anything resting, verifies nothing stayed
open, and returns `(shares, usd, fees)` from `get_trades`: the exchange truth,
not your hopes.

At startup pmq **introspects the installed py-clob-client-v2** against the API
surface it was verified on, and refuses to trade on drift instead of sending
orders through changed semantics.

## Quickstart

Market data needs no keys:

```python
import pmq

m = pmq.parse_market(pmq.get_market("btc-updown-15m-1783062000"))
book = pmq.get_book(m["token_a"])
bid, bid_sz, ask, ask_sz = pmq.best_bid_ask(book)
print(ask, pmq.band_ask_depth_usd(book, 0.90, 0.97))
print(pmq.fee(price=0.95, shares=100))          # taker fee in $, crypto rate
```

Execution (reads `POLY_PRIVATE_KEY`, `POLY_FUNDER`, `POLY_SIG_TYPE` from the
environment):

```python
from pmq import PolymarketExecutor, OrderUncertain

ex = PolymarketExecutor()                        # signature_type=3 for the app's deposit wallet
ex.require_collateral(5.0)                       # fail fast, with a diagnostic that names sig_type

try:
    fill = ex.buy_fak(token_id=m["token_a"], price_cap=0.95, usd=5.00)
except OrderUncertain:
    ex.reconcile(m["condition_id"], m["token_a"])   # exchange truth before anything else
else:
    if fill:                                     # book ONLY what matched
        print(fill.matched_shares, "shares at", fill.price, "order", fill.order_id)
```

`sell_fak` and `limit_gtc` follow the same contract. The buy path has carried
live volume; treat the sell path as following the same documented semantics
with less battle time.

## The signature_type table nobody gives you

| `signature_type` | Wallet | When it is yours |
|---|---|---|
| 0 | the EOA itself | you trade from a bare private key |
| 1 | `POLY_PROXY` | email/Magic accounts (legacy) |
| 2 | `POLY_GNOSIS_SAFE` | browser-wallet proxy |
| 3 | `POLY_1271` deposit wallet | **the Polymarket app's default wallet** |

If `collateral()` returns 0 while the funds are visible on-chain on your funder
address, your `signature_type` is wrong. Debug trick: `eth_call` `owner()`
(`0x8da5cb5b`) on the funder; if it returns your EOA and the wallet bytecode is
an ERC-1167 proxy, you want `signature_type=3`.

## Comparison (2026-07-03, factual)

| | pmq | py-clob-client-v2 (official) | pmxt | NautilusTrader | caiovicentino MCP |
|---|---|---|---|---|---|
| CLOB V2 signing | yes, local | yes, local | writes via its hosted backend | yes, local | V1 only (rejected in prod since 2026-04-28) |
| Confirmed-fill contract | yes (core design) | no (raw responses) | n/a | engine-level | no |
| Deposit wallet / POLY_1271 | yes, production-proven | open issues (#70 and others) | n/a | untested claim | no |
| Fee math | official per-category formula | fee at match, no helper | via backend | fee model | fee-blind |
| Reconciliation helper | yes | no | n/a | engine-level | no |
| Footprint | one small lib | one small lib | multi-venue platform | full trading framework | MCP server |

NautilusTrader is excellent if you want a full framework; pmq is the small
library you embed in your own bot. pmxt is convenient if you accept routing
writes through their backend; pmq exists for self-custody.

## Builder code disclosure

pmq ships with the maintainer's public Polymarket **builder code** as default
attribution inside signed orders (`pmq.executor.DEFAULT_BUILDER_CODE`). Its
commission is set to **0/0: it never adds any fee to your orders**. Attribution
feeds Polymarket's builder program and funds this project at zero cost to you.

Opt out or replace it, one line either way:

```python
PolymarketExecutor(builder_code=None)            # no attribution
PolymarketExecutor(builder_code="0xYOURS...")    # your own code
```

or set the `POLY_BUILDER_CODE` environment variable. (Same model as
JKorf/Polymarket.Net; the official client defaults to zero attribution.)

## MCP server (agents)

`pip install "pmquant[mcp]"` then run `pmq-mcp` (stdio). Read tools (market,
book, taker_fee, account_collateral, account_trades) always exist. Trading
tools (`fak_buy`, `fak_sell`, `cancel_and_reconcile`) are **only registered
when the operator sets `PMQ_MCP_LIVE=1`** in the server environment: an
agent cannot talk its way past a tool that was never created. Every order is
capped per call by `PMQ_MCP_MAX_USD` (default 10).

```json
{
  "mcpServers": {
    "pmq": {
      "command": "pmq-mcp",
      "env": { "POLY_PRIVATE_KEY": "...", "POLY_FUNDER": "0x...", "POLY_SIG_TYPE": "3" }
    }
  }
}
```

Leave the `POLY_*` variables out entirely for a read-only market-data server.

## Bot template

[bot-template/](bot-template/) is a complete bot minus the strategy, for ANY
market (politics, sports, crypto, culture): paper mode against real books
with real fees, per-market budgets with fee headroom, poisoned-market
reconciliation, consecutive-failure halt, disk-persisted daily loss halt, a
systemd unit with `RestartPreventExitStatus=42` so halts stay halted, and a
lightweight phone dashboard. You implement `watchlist()` and `decide()`; the
shipped demo strategy is an API illustration meant to be replaced.

## Security posture

* Keys are read from the environment, used to instantiate the signer, and
  never logged. No custody, no backend, no telemetry, zero network calls
  besides Polymarket endpoints.
* Beware of the documented wave of fake "polymarket bot" repositories that
  steal private keys. Read the source: pmq is small on purpose.
* Fund the trading wallet with what you can afford to lose. Nothing here is
  financial advice; prediction-market access is restricted in some
  jurisdictions and compliance is on you.

## License

MIT
