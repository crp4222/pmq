# pmq

<!-- mcp-name: io.github.crp4222/pmq -->

[![PyPI](https://img.shields.io/pypi/v/pmquant)](https://pypi.org/project/pmquant/)
[![tests](https://github.com/crp4222/pmq/actions/workflows/test.yml/badge.svg)](https://github.com/crp4222/pmq/actions/workflows/test.yml)
[![canary](https://github.com/crp4222/pmq/actions/workflows/canary.yml/badge.svg)](https://github.com/crp4222/pmq/actions/workflows/canary.yml)
[![coverage gate](https://img.shields.io/badge/coverage-%E2%89%A585%25%20enforced%20in%20CI-blue)](.github/workflows/test.yml)
[![typed](https://img.shields.io/badge/types-mypy%20strict-blue)](pyproject.toml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/crp4222/pmq/badge)](https://scorecard.dev/viewer/?uri=github.com/crp4222/pmq)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Fail-closed execution and market data for **Polymarket CLOB V2**, in Python,
built agent-first. Local signing (your keys never leave your process),
exchange-confirmed fills only, fee-correct math, deposit-wallet
(`POLY_1271`) support that actually works in production, order-attribution
registries (several bots can share one wallet, each with its own
exchange-truth accounting), and a bundled
**MCP server**: plug any LLM or agent framework that speaks MCP (Claude,
ChatGPT, LangChain, your own loop) on top and it can read every market and,
if and only if the operator enables it, trade under hard rails: tools that
do not exist until you create them, a cap per order, a daily buy budget.
The model cannot widen any of this from inside a session.

```bash
pip install pmquant        # Python >= 3.10; distribution pmquant, import pmq
```

(PyPI's similarity check reserves the bare name; the module you import is
`pmq`, same pattern as beautifulsoup4/bs4.)

## Try it in 30 seconds, no keys

Point any MCP client at `uvx`; it installs the MCP extra in an isolated
environment and starts `pmq-mcp` with one environment variable:

```json
{
  "mcpServers": {
    "pmq": {
      "command": "uvx",
      "args": ["--from", "pmquant[mcp]", "pmq-mcp"],
      "env": { "PMQ_MCP_PAPER": "1" }
    }
  }
}
```

`PMQ_MCP_PAPER=1` registers the same trading tools as live, but fills are
**simulated against the real live order books** using the displayed best
quote, venue minimums, and a documented crypto-rate fee estimate. The first
paper ledger starts at 1000 USD, configurable with `PMQ_MCP_PAPER_USD`; it
then persists locally across server restarts. No keys are needed and no order
can reach the exchange. A real session, captured 2026-07-04, quoted verbatim:

```text
> find_markets(query="fed decision july")
    12 markets, among them "How many dissent at the July Fed meeting?"
> market(slug="will-no-one-dissent-the-july-fed-decision-20260616001928666")
    condition_id 0x50ba...7967, token ids for Yes and No, closes 2026-07-29
> book(token_id=<Yes>)
    bid 0.54 x 592.75 | ask 0.56 x 21 | min order 5 shares | tick 0.01
> fak_buy(token_id=<Yes>, price_cap=0.58, usd=10)
    paper fill: 17.8571 shares at 0.56 (the real ask, not the cap),
    fee 0.308, cash left 989.69
> account_collateral()
    989.69 paper USD
```

Five calls: discover, resolve, read the live book, buy with simulated
money at the real ask, check the balance. The same session rendered as a
step-by-step page: [docs/demo.html](docs/demo.html) (one self-contained
HTML file, no JavaScript, no external requests; download and open it).
Trading real money additionally requires keys and an explicit
`PMQ_MCP_LIVE=1`, under the rails in
[the agents section](#agents-the-mcp-server).

As of 2026-07-03 this is, to our knowledge, the **only maintained Python
layer combining local CLOB V2 signing, an exchange-confirmed fill contract,
and working deposit-wallet (POLY_1271) auth**. That claim is dated and
falsifiable: [docs/comparison.md](docs/comparison.md) names the
alternatives and what each does instead; open an issue if it goes stale.

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

`sell_fak` and `limit_gtc` follow the same contract, and all three paths
have carried production volume: a FAK round trip (buy 5.149 @ 0.94, sell
back 5.14 @ 0.94, cross-checked via `get_trades`, 2026-07-03) and a GTC
maker fill (posted above the bid, matched as MAKER at zero fee,
2026-07-04, settlement tx in the production section below).

## Scope, latency, requirements

Python 3.10 to 3.14 (the CI matrix runs all five). Plain REST round trips,
measured 2026-07-04 (medians of 5, residential fiber, Western Europe):
resolve a market 76 ms, fetch a book 85 ms, sign + POST an order and get
the exchange's answer 73 ms. Sub-second everywhere, built for second-scale
strategies (the maintainer's bot polls 15-minute windows every 2.5 s); it
is not a microsecond market-making stack: no websockets, no co-location,
one HTTP call per action.

## Why this exists

Polymarket cut over to CLOB V2 on 2026-04-28. V1-signed orders are rejected in
production, the fee schedule is decided at match time, and the official client
examples leave several traps undocumented. Every line of pmq was paid for with
a real error in live trading:

* `invalid amounts, the market buy orders maker amount supports a max accuracy
  of 2 decimals, taker amount a max of 4 decimals`: the CLOB treats FAK/FOK
  buys as **market orders** and caps their signed amounts at 2 decimals
  (maker) / 4 decimals (taker) whatever the tick size. The official client's
  rounding table allows 5-6 taker decimals on markets whose tick is finer
  than 0.01 (any book trading past 0.96 or under 0.04), so market orders
  there are rejected wholesale (reported upstream:
  [py-clob-client-v2#99](https://github.com/Polymarket/py-clob-client-v2/issues/99)).
  pmq clamps the signed
  pair to the exchange caps before signing and refuses at startup any
  client build that would still sign a rejectable pair, so the trap cannot
  reach your orders. Measurements in
  [docs/rounding-study.md](docs/rounding-study.md).
* `no orders found to match with FAK order` (HTTP 400, yet with an `orderID`):
  a clean no-fill, not an error. pmq returns an empty `Fill` instead of crashing
  or, worse, retrying blindly.
* CLOB shows `balance: 0` while your pUSD sits on-chain: the balance endpoint
  ignores your `funder` parameter and derives the wallet from your EOA and
  `signature_type`. Funds in the Polymarket app's default wallet (an ERC-1271
  deposit wallet) are only visible with `signature_type=3`.

The full write-up with reproduction details: [docs/war-story.md](docs/war-story.md).

## Runs in production: my own money, daily

I built pmq for my own trading. It executes real volume with my funds every
day, and it has never booked a fill the exchange did not confirm. If you
want to see it on-chain, here is a settlement from one of my wallets
(2026-07-03):
[`0x387f5f09...100d88a8`](https://polygonscan.com/tx/0x387f5f09c031bb36a71c54adc978b1ed4d50c67f6dd3f0c2c8068391100d88a8)
on the CTF Exchange V2: a FAK market buy built by this library, matched and
settled, with the builder code visible in the calldata. The maker path has
its own receipt (2026-07-04): a `limit_gtc` posted one tick above the bid,
matched as MAKER at zero fee and settled in
[`0x1b60f19a...c35d09`](https://polygonscan.com/tx/0x1b60f19a6f089624f27babb58bf82538c49f044ee83778783195e26a33c35d09),
where the `maker_orders` slice accounting that release 0.4.6 encodes is
visible in the raw trade record. A weekly
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
open, and returns `(shares, usd, fees)` from `get_trades`: the exchange truth.

At startup pmq **introspects the installed py-clob-client-v2** against the API
surface it was verified on, and refuses to trade on drift instead of sending
orders through changed semantics. The whole table is pinned by an executable
test per row plus a hypothesis fuzz suite (hundreds of generated adversarial
responses per run, including NaN/Infinity and negative amounts, which book
zero).

## Several bots, one wallet

`get_trades` is account-level: run two bots on the same wallet and each
one's exchange-truth totals silently include the other's fills. Since
0.5.0 every order-sender can keep an **attribution registry**: an
append-only file of its own order ids, written on every confirmed post.

```python
ex = PolymarketExecutor(order_log="botA.orders",
                        foreign_order_logs=["botB.orders"])
# or per process: POLY_ORDER_LOG=botA.orders POLY_FOREIGN_ORDER_LOGS=botB.orders
```

With a registry configured, `trades_totals()` counts only trades whose
`taker_order_id` (taker role) or `maker_orders[].order_id` slice (maker
role) belongs to OUR registry (both fields verified present and populated
on real V2 trade records), and `reconcile()` additionally claims trades
unknown to EVERY registry, so a fill posted during an uncertainty window
is recovered by the bot that was uncertain and by nobody else. Sound only
if every sender on the wallet keeps a registry
(`POLY_FOREIGN_ORDER_LOGS` is colon-separated). The MCP server inherits
the registries through the same environment variables. Fully opt-in:
without `POLY_ORDER_LOG` the behavior is unchanged.

## Streaming the resolution prices

The updown markets resolve on the Chainlink stream, and
`wss://ws-live-data.polymarket.com` republishes that exact stream (plus a
Binance spot mirror). `pmq.stream.PriceStream` consumes it with the
standard library only:

```python
from pmq.stream import PriceStream

ps = PriceStream(assets=("btc", "eth")).start()
ps.last("btc")               # (unix_seconds, value) from the Chainlink feed
ps.age("btc")                # seconds since the freshest tick
ps.last("btc", "binance")    # the spot mirror, for comparison
```

Design note, measured 2026-07 from two unrelated egresses: the edge serves
the sustained push only to browser connections; a plain client gets the
initial tick batch after subscribing, then silence. `PriceStream` therefore
re-polls short connections (about one per second); the freshest tick is
typically 1.2 to 2.8 seconds old. Treat the feed as advisory and fail
closed on `age()`: the exchange resolves with its own copy.

## The signature_type decoder table

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

## Alternatives

NautilusTrader if you want a full backtesting and trading framework; pmxt
if you accept routing writes through a hosted backend; raw
py-clob-client-v2 if you want no opinion layered on the official client.
The dated feature-by-feature table (written by an interested party, every
row checkable) lives in [docs/comparison.md](docs/comparison.md).

## Builder code disclosure

pmq ships with the maintainer's public Polymarket **builder code** as default
attribution inside signed orders (`pmq.executor.DEFAULT_BUILDER_CODE`). Its
commission is set to **0/0: it never adds any fee to your orders**. Attribution
feeds Polymarket's builder program and funds this project at zero cost to you.

## Agents: the MCP server

For an installed server, run `pip install "pmquant[mcp]"` then `pmq-mcp`
(stdio). For a clean MCP-client configuration, use
`uvx --from "pmquant[mcp]" pmq-mcp` as in the paper example above. Listed in
the [official MCP registry](https://registry.modelcontextprotocol.io) as
`io.github.crp4222/pmq`, it works with Claude Desktop or Code, ChatGPT,
LangChain, and a bare SDK loop.

**What an agent can do, exactly:**

| Tool | Needs | What it does |
|---|---|---|
| `pmq_status` | nothing | mode, registered trading surface, caps, daily headroom, and durable-state health without constructing a signer |
| `find_markets` | nothing | discover active markets, any category, full-text search |
| `event` | nothing | all binary markets of a multi-outcome event (elections, tournaments) |
| `market` | nothing | slug to condition id, outcome names, token ids, close time, winner |
| `market_snapshot` | nothing | resolve a market and read a top-of-book summary for every outcome in one call |
| `book` | nothing | real-time bid/ask with sizes, depth in a price range, exchange minimums |
| `order_preview` | nothing | non-mutating top-of-book FAK estimate with rails and a crypto-rate fee estimate; it never creates a signer, submits an order, or reserves budget |
| `taker_fee` | nothing | official fee formula per category, cost per share including fee |
| `account_collateral` | paper mode, or keys | paper cash or the CLOB-visible live balance with a sig_type diagnostic |
| `account_trades` | paper mode, or keys | paper totals or exchange-truth BUY totals on one market |
| `account_portfolio` | paper mode, or public wallet | durable paper positions, or public Data API positions for `wallet` or `POLY_FUNDER` |
| `fak_buy` | `PMQ_MCP_PAPER=1`, or keys plus `PMQ_MCP_LIVE=1` | open a position with a fill-and-kill buy; nothing rests |
| `fak_sell` | `PMQ_MCP_PAPER=1`, or keys plus `PMQ_MCP_LIVE=1` | close a position with a fill-and-kill sell under the same contract |
| `cancel_and_reconcile` | `PMQ_MCP_PAPER=1`, or keys plus `PMQ_MCP_LIVE=1` | cancel resting orders and return reconciliation truth; paper has nothing resting |

With `PMQ_MCP_PAPER=1` (the [30-second demo](#try-it-in-30-seconds-no-keys)
above) the same trading and account tools are registered **keyless**:
fills are simulated at the displayed best quote, capped by the displayed
size, refused under the exchange minimum, and the account tools report the
durable paper ledger. `order_preview` remains read-only in every mode.
Paper responses are flagged `paper: true`, and no order reaches the exchange.

**The rails, all operator-set (server environment, invisible to and
untouchable by the model):**

| Variable | Effect | Default |
|---|---|---|
| `PMQ_MCP_LIVE` | unset: the three trading tools are never REGISTERED; an agent cannot call a tool that does not exist | read-only |
| `PMQ_MCP_PAPER` | trading tools simulate fills against the real live books, keyless, nothing sent to the exchange; wins over `PMQ_MCP_LIVE` when both are set | off |
| `PMQ_MCP_PAPER_USD` | initial paper balance when a new state file is created | 1000 |
| `PMQ_MCP_MAX_USD` | hard cap per single order, live and paper alike | 10 |
| `PMQ_MCP_DAILY_USD` | durable cumulative BUY budget per UTC day; unknown live results retain their requested reservation through that UTC day | off |
| `PMQ_MCP_STATE_FILE` | local file for the durable paper ledger and daily budget | `$XDG_STATE_HOME/pmq/mcp-state.json`, otherwise `~/.local/state/pmq/mcp-state.json` |
| `POLY_*` keys | omit them entirely for a data-only server | absent |

Structural rails on top: only FAK orders exist (nothing rests unattended on
the book), every uncertain outcome is surfaced for reconciliation, and fills
are booked only from exchange confirmations, never from optimism.

The state file contains paper cash, positions, fills, and the daily budget,
never key material. It is atomically replaced on update. Use a distinct state
file for each concurrently running server. A live buy reserves its requested
amount before the client call, then settles that reservation to an
exchange-confirmed amount. A clean rejection releases it; an unknown outcome
keeps the full reservation through the UTC day. `pmq_status` exposes state
health without exposing secrets. If a required durable write fails, the
affected buy is refused rather than proceeding without its rail.

```json
{
  "mcpServers": {
    "pmq": {
      "command": "uvx",
      "args": ["--from", "pmquant[mcp]", "pmq-mcp"],
      "env": {
        "PMQ_MCP_LIVE": "1",
        "PMQ_MCP_MAX_USD": "10",
        "PMQ_MCP_DAILY_USD": "25",
        "POLY_PRIVATE_KEY": "...",
        "POLY_FUNDER": "0x...",
        "POLY_SIG_TYPE": "3"
      }
    }
  }
}
```

Remove `PMQ_MCP_LIVE` and the `POLY_*` variables entirely for a read-only
market-data server.

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
* A documented wave of fake "polymarket bot" repositories steals private
  keys; pmq is deliberately small so the entire execution path stays
  readable in minutes by anyone who wants to look.
* Fund the trading wallet with what you can afford to lose. Nothing here is
  financial advice; prediction-market access is restricted in some
  jurisdictions and compliance is on you.

## If you feel like checking any of it

None of the claims above require taking my word; each one comes with a
handle you can pull, whenever you care to:

* **Egress.** `PMQ_CANARY=1 pytest tests/test_canary_live.py -k egress -s`
  records every DNS resolution during a full session (market data, auth
  derivation, one signed order) and fails on any host outside
  `polymarket.com`. Last observed list: `clob.polymarket.com`,
  `gamma-api.polymarket.com`, nothing else. The weekly
  [canary](../../actions/workflows/canary.yml) prints that list in public
  CI logs. One designed exception: `pmq-doctor`'s optional on-chain checks
  use the public Polygon RPCs named in its source.
* **Provenance.** Releases carry a signed PEP 740 attestation (Sigstore,
  via PyPI trusted publishing): click "provenance" next to any file on the
  [PyPI files page](https://pypi.org/project/pmquant/#files), or fetch it
  raw from PyPI's integrity API. The signing identity is this repository's
  `publish.yml` workflow.
* **Dependencies.** Dependabot files weekly bump PRs (Python and
  SHA-pinned GitHub Actions), and the weekly canary runs `pip-audit`; a
  hit opens an issue by itself.
* **The source.** Five small modules; the whole execution path reads in
  minutes. The grep targets that answer the important questions fastest
  are listed in [SECURITY.md](SECURITY.md).

## Stability and maintenance

Pre-1.0 SemVer with a written deprecation window and a stated bar for 1.0;
one maintainer, trading his own money through this exact code daily. The
operational rule worth knowing: if the canary badge goes red and stays
red, treat the project as unmaintained and pin your last known-good
version. Full policy and the precisely scoped help-wanted:
[docs/stability.md](docs/stability.md).

## License

MIT
