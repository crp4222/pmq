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
import time
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
PAPER_ENABLED = os.environ.get("PMQ_MCP_PAPER") == "1"
if PAPER_ENABLED:
    LIVE_ENABLED = False          # explicit paper always wins over live
MAX_USD = float(os.environ.get("PMQ_MCP_MAX_USD", "10"))
PAPER_START_USD = float(os.environ.get("PMQ_MCP_PAPER_USD", "1000"))
MAX_DAILY_USD = float(os.environ.get("PMQ_MCP_DAILY_USD", "0"))
_budget_day = [""]
_budget_spent = [0.0]


def _budget_left() -> float | None:
    """Remaining daily BUY budget, None when disabled (PMQ_MCP_DAILY_USD
    unset or <= 0). Resets at the UTC day change. Per process: a server
    restart resets it; this is a runaway-session limiter, not accounting."""
    if MAX_DAILY_USD <= 0:
        return None
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if _budget_day[0] != day:
        _budget_day[0], _budget_spent[0] = day, 0.0
    return MAX_DAILY_USD - _budget_spent[0]


def _budget_consume(usd: float) -> None:
    if MAX_DAILY_USD > 0:
        _budget_spent[0] += usd

mcp = FastMCP(
    "pmq",
    instructions=(
        "Polymarket CLOB V2 data and fail-closed execution. Books are real "
        "time; the trade tape lags 1 to 3 minutes, never trade off it. "
        + ("PAPER trading tools are enabled: fills are SIMULATED against "
           "the real live order books (real asks, real exchange minimums, "
           "real fees); no keys are involved and no order ever reaches the "
           f"exchange. Starting balance {PAPER_START_USD:.2f} USD."
           if PAPER_ENABLED else
           "Trading tools are ENABLED; every order is capped at "
           f"{MAX_USD:.2f} USD per call"
           + (f" and buys at {MAX_DAILY_USD:.2f} USD per UTC day."
              if MAX_DAILY_USD > 0 else ".") if LIVE_ENABLED else
           "Trading tools are DISABLED (operator did not set PMQ_MCP_LIVE=1 "
           "or PMQ_MCP_PAPER=1); this server is read-only.")),
)

_executor: PolymarketExecutor | None = None


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


_paper: dict[str, Any] = {"cash": PAPER_START_USD, "positions": {}, "fills": []}


def _paper_reset() -> None:
    _paper["cash"] = PAPER_START_USD
    _paper["positions"] = {}
    _paper["fills"] = []


@mcp.tool()
def account_collateral() -> dict[str, Any]:
    """Collateral (pUSD, $) the CLOB sees for the configured account. If this
    is 0 while funds are on-chain, the operator's POLY_SIG_TYPE is wrong
    (the Polymarket app's deposit wallet needs 3). In paper mode: the
    simulated cash balance."""
    if PAPER_ENABLED:
        return {"collateral_usd": round(_paper["cash"], 2), "paper": True}
    return {"collateral_usd": _ex().collateral()}


@mcp.tool()
def account_trades(condition_id: str, token_id: str = "") -> dict[str, Any]:
    """Exchange-truth totals of OUR account's BUY trades on one market:
    (shares, usd, fee estimate). This is the reconciliation source, use it
    after any uncertainty instead of trusting local bookkeeping."""
    if PAPER_ENABLED:
        fills = [f for f in _paper["fills"]
                 if not token_id or f["token_id"] == token_id]
        return {"shares": round(sum(f["shares"] for f in fills if f["side"] == "BUY")
                                - sum(f["shares"] for f in fills if f["side"] == "SELL"), 4),
                "usd": round(sum(f["usd"] for f in fills if f["side"] == "BUY"), 4),
                "fee_estimate": round(sum(f["fee"] for f in fills), 4),
                "paper": True}
    totals = _ex().trades_totals(condition_id, token_id or None)
    if totals is None:
        return {"error": "trades endpoint unreachable"}
    sh, usd, fees = totals
    return {"shares": sh, "usd": usd, "fee_estimate": fees}


def _paper_buy(token_id: str, price_cap: float, usd: float) -> dict[str, Any]:
    """Simulated FAK buy against the REAL book: fills at the real best ask
    (never at the wished cap), capped by displayed size, refused under the
    per-market exchange minimum, real fee formula. Same shape as live."""
    book = data.get_book(token_id)
    if not book:
        return {"error": "book unavailable"}
    _, _, ask, ask_sz = data.best_bid_ask(book)
    if ask is None or ask > price_cap:
        return {"booked": False, "rejected": True, "paper": True,
                "error": f"no ask at or under {price_cap} (best ask: {ask})"}
    min_sh = data.book_meta(book)["min_order_size"] or 0.0
    usd_eff = min(usd, ask * (ask_sz or 0.0), _paper["cash"])
    shares = round(usd_eff / ask, 4)
    if shares < (min_sh or 0):
        return {"booked": False, "rejected": True, "paper": True,
                "error": f"{shares} shares under the exchange minimum {min_sh}"}
    fee_usd = data.fee(ask, shares)
    _paper["cash"] -= ask * shares + fee_usd
    _paper["positions"][token_id] = _paper["positions"].get(token_id, 0.0) + shares
    fill = {"token_id": token_id, "side": "BUY", "price": ask,
            "shares": shares, "usd": round(ask * shares, 4),
            "fee": round(fee_usd, 4)}
    _paper["fills"].append(fill)
    return {"booked": True, "paper": True, "matched_shares": shares,
            "matched_usd": fill["usd"], "price": ask, "fee_usd": fill["fee"],
            "cash_left": round(_paper["cash"], 2)}


def _paper_sell(token_id: str, price_floor: float, shares: float) -> dict[str, Any]:
    held = _paper["positions"].get(token_id, 0.0)
    if shares > held + 1e-9:
        return {"booked": False, "rejected": True, "paper": True,
                "error": f"position is {held} shares, cannot sell {shares}"}
    book = data.get_book(token_id)
    if not book:
        return {"error": "book unavailable"}
    bid, bid_sz, _, _ = data.best_bid_ask(book)
    if bid is None or bid < price_floor:
        return {"booked": False, "rejected": True, "paper": True,
                "error": f"no bid at or above {price_floor} (best bid: {bid})"}
    sh = round(min(shares, bid_sz or shares), 4)
    fee_usd = data.fee(bid, sh)
    _paper["cash"] += bid * sh - fee_usd
    _paper["positions"][token_id] = held - sh
    fill = {"token_id": token_id, "side": "SELL", "price": bid, "shares": sh,
            "usd": round(bid * sh, 4), "fee": round(fee_usd, 4)}
    _paper["fills"].append(fill)
    return {"booked": True, "paper": True, "matched_shares": sh,
            "matched_usd": fill["usd"], "price": bid, "fee_usd": fill["fee"],
            "cash_left": round(_paper["cash"], 2)}


if LIVE_ENABLED or PAPER_ENABLED:
    @mcp.tool()
    def fak_buy(token_id: str, price_cap: float, usd: float) -> dict[str, Any]:
        """Place a fill-and-kill market BUY: spend up to `usd` at prices no
        worse than `price_cap`. Nothing rests on the book. Book ONLY what
        `matched_shares`/`matched_usd` report; `booked: false` means nothing
        happened. Hard-capped per call by the operator's PMQ_MCP_MAX_USD and
        per UTC day by PMQ_MCP_DAILY_USD when set (confirmed spend counts;
        an unknown outcome conservatively consumes the full requested
        amount until reconciled)."""
        if usd > MAX_USD:
            return {"error": f"refused: {usd} exceeds the {MAX_USD} USD per-order cap"}
        left = _budget_left()
        if left is not None and usd > left:
            return {"error": f"refused: daily buy budget "
                             f"({MAX_DAILY_USD:.2f} USD) leaves only "
                             f"{max(left, 0.0):.2f} USD today"}
        if PAPER_ENABLED:
            out = _paper_buy(token_id, price_cap, usd)
            if out.get("booked"):
                _budget_consume(float(out.get("matched_usd") or 0.0))
            return out
        try:
            fill = _ex().buy_fak(token_id, price_cap, usd)
        except OrderUncertain as e:
            _budget_consume(usd)
            return {"error": f"outcome unknown ({e}); call cancel_and_reconcile "
                             "on this market before any new order"}
        if fill:
            _budget_consume(fill.matched_usd)
        return _fill_dict(fill)

    @mcp.tool()
    def fak_sell(token_id: str, price_floor: float, shares: float) -> dict[str, Any]:
        """Fill-and-kill market SELL of `shares` at prices no worse than
        `price_floor`. Same confirmation contract as fak_buy."""
        if PAPER_ENABLED:
            return _paper_sell(token_id, price_floor, shares)
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
        if PAPER_ENABLED:
            return {"cancelled": True, "paper": True,
                    "note": "paper mode: FAK only, nothing ever rests"}
        totals = _ex().reconcile(condition_id, token_id or None)
        if totals is None:
            return {"cancelled": True, "error": "trades endpoint unreachable"}
        sh, usd, fees = totals
        return {"cancelled": True, "shares": sh, "usd": usd, "fee_estimate": fees}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
