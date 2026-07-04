"""pmq-mcp: MCP server exposing pmq to LLM agents, safety rails included.

Run with ``pmq-mcp`` (stdio transport). Design rules:

* Read tools work with no credentials and never touch keys.
* Trading tools exist ONLY when the operator sets ``PMQ_MCP_LIVE=1`` in the
  server's environment. An agent cannot talk its way past a tool that was
  never registered.
* Every order is capped by ``PMQ_MCP_MAX_USD`` (default 10) per call.
* On an unknown outcome (timeout, 5xx) the server reconciles automatically
  and reports exchange truth instead of guessing.
* POLY_PRIVATE_KEY is read by the executor, used to sign, never returned.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from typing import TYPE_CHECKING, Any

from . import data
from .exceptions import OrderUncertain

if TYPE_CHECKING:
    from .executor import Fill, PolymarketExecutor

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit("pmq-mcp needs the optional dependency: pip install 'pmq[mcp]'") from e

LIVE_ENABLED = os.environ.get("PMQ_MCP_LIVE") == "1"
MAX_USD = float(os.environ.get("PMQ_MCP_MAX_USD", "10"))

def _attribution_state() -> dict[str, Any]:
    """Which builder code would ride inside signed orders, and where it
    came from. Pure env inspection: safe without credentials."""
    from .executor import DEFAULT_BUILDER_CODE
    env = os.environ.get("POLY_BUILDER_CODE")
    code = DEFAULT_BUILDER_CODE if env is None else env
    source = ("maintainer default (disclosed in README)" if env is None
              else "operator override via POLY_BUILDER_CODE" if env
              else "opted out via empty POLY_BUILDER_CODE")
    return {"builder_code": code or None, "source": source,
            "commission": "0/0: attribution never adds fees to orders",
            "opt_out": "set POLY_BUILDER_CODE= (empty) in the server env"}


mcp = FastMCP(
    "pmq",
    instructions=(
        "Polymarket CLOB V2 data and fail-closed execution. Books are real "
        "time; the trade tape lags 1 to 3 minutes, never trade off it. "
        + ("Trading tools are ENABLED; every order is capped at "
           f"{MAX_USD:.2f} USD per call. Orders carry builder attribution "
           "(zero commission): call the attribution tool for the exact "
           "state and the one-line opt-out." if LIVE_ENABLED else
           "Trading tools are DISABLED (operator did not set PMQ_MCP_LIVE=1); "
           "this server is read-only.")),
)

_executor: PolymarketExecutor | None = None


@mcp.tool()
def attribution() -> dict[str, Any]:
    """Full transparency on builder attribution: the exact builder code that
    rides inside signed orders, where it comes from (maintainer default,
    operator override, or opted out), its commission (always 0/0), and how
    to opt out. Call this whenever the user asks what is attached to their
    orders."""
    return _attribution_state()


def _ex() -> PolymarketExecutor:
    global _executor
    if _executor is None:
        from .executor import PolymarketExecutor
        _executor = PolymarketExecutor()
    return _executor


def _fill_dict(fill: Fill) -> dict[str, Any]:
    return {"order_id": fill.order_id, "matched_shares": fill.matched_shares,
            "matched_usd": fill.matched_usd, "price": fill.price,
            "rejected": fill.rejected, "error": fill.error,
            "booked": bool(fill)}


@mcp.tool()
def find_markets(query: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Discover tradeable Polymarket markets of ANY kind (politics, sports,
    crypto, culture). With a query, full-text search; without, the most
    active events by 24h volume. Returns event title plus, per market, the
    slug to pass to the `market` tool and the outcome names."""
    if query:
        res = data.http_get_json(
            f"{data.GAMMA}/public-search?q={urllib.parse.quote(query)}"
            f"&limit_per_type={min(limit, 20)}") or {}
        events = res.get("events") or []
    else:
        events = data.http_get_json(
            f"{data.GAMMA}/events?closed=false&order=volume24hr&ascending=false"
            f"&limit={min(limit, 20)}") or []
    out = []
    for ev in events[:limit]:
        for m in (ev.get("markets") or [])[:4]:
            pm = data.parse_market(m)
            if pm:
                out.append({"event": ev.get("title"), "market_slug": pm["slug"],
                            "outcomes": [pm["outcome_a"], pm["outcome_b"]],
                            "volume24hr": ev.get("volume24hr")})
    return out or [{"error": "nothing found"}]


@mcp.tool()
def event(slug: str) -> list[dict[str, Any]]:
    """All binary markets of one multi-outcome EVENT (an election, a
    tournament: one market per candidate). Use the event slug from
    find_markets. Returns per market: slug, outcome names with token ids,
    close time, settled winner if any."""
    out = [{"market_slug": pm["slug"],
            "outcomes": {pm["outcome_a"]: pm["token_a"], pm["outcome_b"]: pm["token_b"]},
            "end_ts": pm["end_ts"], "settled_winner": data.resolved_winner(pm)}
           for pm in data.event_markets(slug)]
    return out or [{"error": f"no event found for slug {slug!r}"}]


@mcp.tool()
def market(slug: str) -> dict[str, Any]:
    """Resolve one Polymarket market by its gamma slug (any category, works
    for expired short-lived markets too). Returns condition_id, the outcome
    names mapped to their token ids (use those token ids with `book` and the
    trading tools), the close time and the settled winner if resolution
    already happened."""
    pm = data.parse_market(data.get_market(slug))
    if not pm:
        return {"error": f"no market found for slug {slug!r}"}
    return {"condition_id": pm["condition_id"],
            "outcomes": {pm["outcome_a"]: pm["token_a"], pm["outcome_b"]: pm["token_b"]},
            "end_ts": pm["end_ts"],
            "settled_winner": data.resolved_winner(pm)}


@mcp.tool()
def book(token_id: str, depth_lo: float = 0.0, depth_hi: float = 1.0) -> dict[str, Any]:
    """REAL-TIME order book summary for one outcome token: best bid/ask with
    sizes, plus the $ notional of asks resting inside [depth_lo, depth_hi].
    This endpoint is served by the matching engine; trust it over the trade
    tape for any live decision."""
    b = data.get_book(token_id)
    if not b:
        return {"error": "book unavailable"}
    bid, bid_sz, ask, ask_sz = data.best_bid_ask(b)
    return {"bid": bid, "bid_size": bid_sz, "ask": ask, "ask_size": ask_sz,
            "ask_depth_usd_in_range": data.band_ask_depth_usd(b, depth_lo, depth_hi),
            **data.book_meta(b)}


@mcp.tool()
def taker_fee(price: float, shares: float, category: str = "crypto") -> dict[str, Any]:
    """Official Polymarket taker fee in $ (fee = rate * p * (1-p) * shares).
    Categories and rates: crypto 0.07, sports 0.03, finance/politics/mentions/
    tech 0.04, economics/culture/weather 0.05, geopolitics 0. Makers pay 0."""
    rate = data.FEE_RATES.get(category)
    if rate is None:
        return {"error": f"unknown category, pick one of {sorted(data.FEE_RATES)}"}
    return {"fee_usd": data.fee(price, shares, rate), "rate": rate,
            "cost_per_share_incl_fee": price + data.fee(price, 1.0, rate)}


@mcp.tool()
def account_collateral() -> dict[str, Any]:
    """Collateral (pUSD, $) the CLOB sees for the configured account. If this
    is 0 while funds are on-chain, the operator's POLY_SIG_TYPE is wrong
    (the Polymarket app's deposit wallet needs 3)."""
    return {"collateral_usd": _ex().collateral()}


@mcp.tool()
def account_trades(condition_id: str, token_id: str = "") -> dict[str, Any]:
    """Exchange-truth totals of OUR account's BUY trades on one market:
    (shares, usd, fee estimate). This is the reconciliation source, use it
    after any uncertainty instead of trusting local bookkeeping."""
    totals = _ex().trades_totals(condition_id, token_id or None)
    if totals is None:
        return {"error": "trades endpoint unreachable"}
    sh, usd, fees = totals
    return {"shares": sh, "usd": usd, "fee_estimate": fees}


if LIVE_ENABLED:
    @mcp.tool()
    def fak_buy(token_id: str, price_cap: float, usd: float) -> dict[str, Any]:
        """Place a fill-and-kill market BUY: spend up to `usd` at prices no
        worse than `price_cap`. Nothing rests on the book. Book ONLY what
        `matched_shares`/`matched_usd` report; `booked: false` means nothing
        happened. Hard-capped per call by the operator's PMQ_MCP_MAX_USD."""
        if usd > MAX_USD:
            return {"error": f"refused: {usd} exceeds the {MAX_USD} USD per-order cap"}
        try:
            return _fill_dict(_ex().buy_fak(token_id, price_cap, usd))
        except OrderUncertain as e:
            return {"error": f"outcome unknown ({e}); call cancel_and_reconcile "
                             "on this market before any new order"}

    @mcp.tool()
    def fak_sell(token_id: str, price_floor: float, shares: float) -> dict[str, Any]:
        """Fill-and-kill market SELL of `shares` at prices no worse than
        `price_floor`. Same confirmation contract as fak_buy."""
        try:
            return _fill_dict(_ex().sell_fak(token_id, price_floor, shares))
        except OrderUncertain as e:
            return {"error": f"outcome unknown ({e}); "
                             "call account_trades before any new order"}

    @mcp.tool()
    def cancel_and_reconcile(condition_id: str, token_id: str = "") -> dict[str, Any]:
        """Cancel every resting order of ours on one market, verify none
        stayed open, and return exchange-truth totals. Call this after any
        'outcome unknown' before placing new orders on that market."""
        totals = _ex().reconcile(condition_id, token_id or None)
        if totals is None:
            return {"cancelled": True, "error": "trades endpoint unreachable"}
        sh, usd, fees = totals
        return {"cancelled": True, "shares": sh, "usd": usd, "fee_estimate": fees}


def main() -> None:
    # Runtime disclosure: the operator sees the attribution state in the
    # server log even if they never read the README. stderr, never stdout
    # (stdout is the MCP stdio transport).
    a = _attribution_state()
    code = a["builder_code"]
    print(f"pmq-mcp: builder attribution "
          f"{'off' if code is None else code[:10] + '...'} "
          f"({a['source']}; commission 0/0; opt out: POLY_BUILDER_CODE=)",
          file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
