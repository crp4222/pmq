"""pmq-mcp: MCP server exposing pmq to LLM agents, safety rails included.

Run with ``pmq-mcp`` (stdio transport). Design rules:

* Read tools work with no credentials and never touch keys.
* Trading tools exist ONLY when the operator sets ``PMQ_MCP_LIVE=1`` in the
  server's environment. An agent cannot talk its way past a tool that was
  never registered.
* Every order is capped by ``PMQ_MCP_MAX_USD`` (default 10) per call.
* An unknown outcome (timeout, 5xx) keeps its full daily-budget reservation
  through the UTC-day reset. Reconcile before deciding what happened.
* POLY_PRIVATE_KEY is read by the executor, used to sign, never returned.
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import data
from .exceptions import OrderUncertain

if TYPE_CHECKING:
    from .executor import Fill, PolymarketExecutor

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit("pmq-mcp needs the optional dependency: pip install 'pmquant[mcp]'") from e


def _finite_nonnegative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _env_nonnegative(name: str, default: str) -> float:
    value = _finite_nonnegative(os.environ.get(name, default))
    if value is None:
        raise SystemExit(f"{name} must be a finite non-negative number")
    return value


LIVE_ENABLED = os.environ.get("PMQ_MCP_LIVE") == "1"
PAPER_ENABLED = os.environ.get("PMQ_MCP_PAPER") == "1"
if PAPER_ENABLED:
    LIVE_ENABLED = False          # explicit paper always wins over live
MAX_USD = _env_nonnegative("PMQ_MCP_MAX_USD", "10")
PAPER_START_USD = _env_nonnegative("PMQ_MCP_PAPER_USD", "1000")
MAX_DAILY_USD = _env_nonnegative("PMQ_MCP_DAILY_USD", "0")
_STATE_SCHEMA = 1


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _state_path() -> Path:
    raw = os.environ.get("PMQ_MCP_STATE_FILE")
    if raw:
        return Path(raw).expanduser()
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "pmq" / "mcp-state.json"


def _fresh_state(paper_start_usd: float) -> dict[str, Any]:
    return {"schema": _STATE_SCHEMA,
            "paper": {"cash": paper_start_usd, "positions": {}, "fills": []},
            "budget": {"utc_day": "", "spent_usd": 0.0}}


def _valid_paper_fill(fill: Any) -> bool:
    if not isinstance(fill, dict) or fill.get("side") not in ("BUY", "SELL"):
        return False
    if not isinstance(fill.get("token_id"), str):
        return False
    price = _finite_nonnegative(fill.get("price"))
    shares = _finite_nonnegative(fill.get("shares"))
    return (price is not None and price > 0 and shares is not None and shares > 0
            and all(_finite_nonnegative(fill.get(field)) is not None
                    for field in ("usd", "fee")))


def _valid_state(state: Any) -> bool:
    if not isinstance(state, dict) or state.get("schema") != _STATE_SCHEMA:
        return False
    paper, budget = state.get("paper"), state.get("budget")
    if not isinstance(paper, dict) or not isinstance(budget, dict):
        return False
    positions, fills = paper.get("positions"), paper.get("fills")
    if _finite_nonnegative(paper.get("cash")) is None or not isinstance(positions, dict):
        return False
    if not all(isinstance(token, str) and _finite_nonnegative(shares) is not None
               for token, shares in positions.items()):
        return False
    if not isinstance(fills, list) or not all(_valid_paper_fill(fill) for fill in fills):
        return False
    return isinstance(budget.get("utc_day"), str) and _finite_nonnegative(
        budget.get("spent_usd")) is not None


def _write_state(path: Path, state: dict[str, Any]) -> str | None:
    temp_path: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.chmod(temp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, allow_nan=False, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        return None
    except (OSError, TypeError, ValueError) as e:
        return str(e)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


class _McpState:
    """Atomic local ledger for paper fills and durable daily buy rails."""

    def __init__(self, path: Path, paper_start_usd: float) -> None:
        self.path = path
        self.data = _fresh_state(paper_start_usd)
        self.error: str | None = None
        self._lock = threading.RLock()
        if path.exists():
            self._load()

    def _load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as e:
            self.error = f"state unreadable: {e}"
            return
        if not _valid_state(loaded):
            self.error = "state has an invalid schema or non-finite values"
            return
        self.data = loaded

    def _commit(self, candidate: dict[str, Any]) -> bool:
        with self._lock:
            if self.error or not _valid_state(candidate):
                self.error = self.error or "state update rejected"
                return False
            error = _write_state(self.path, candidate)
            if error:
                self.error = f"state write failed: {error}"
                return False
            self.data = candidate
            return True

    def activate(self) -> bool:
        with self._lock:
            if self.error:
                return False
            return self.path.exists() or self._commit(self.data)

    @staticmethod
    def _today_budget(state: dict[str, Any]) -> dict[str, Any]:
        budget = state["budget"]
        return budget if budget["utc_day"] == _utc_day() else {
            "utc_day": _utc_day(), "spent_usd": 0.0}

    def budget_left(self, maximum: float) -> float | None:
        with self._lock:
            if maximum <= 0 or self.error:
                return None
            budget = self._today_budget(self.data)
            return max(0.0, maximum - float(budget["spent_usd"]))

    def reserve_budget(self, maximum: float, usd: float) -> bool:
        if _finite_nonnegative(usd) is None or usd <= 0:
            return False
        with self._lock:
            if self.error:
                return False
            candidate = copy.deepcopy(self.data)
            budget = self._today_budget(candidate)
            if float(budget["spent_usd"]) + usd > maximum + 1e-9:
                return False
            candidate["budget"] = budget
            candidate["budget"]["spent_usd"] += usd
            return self._commit(candidate)

    def settle_budget(self, reserved_usd: float, actual_usd: float) -> bool:
        if (_finite_nonnegative(reserved_usd) is None
                or _finite_nonnegative(actual_usd) is None):
            return False
        with self._lock:
            candidate = copy.deepcopy(self.data)
            candidate["budget"] = self._today_budget(candidate)
            spent = float(candidate["budget"]["spent_usd"])
            candidate["budget"]["spent_usd"] = max(0.0, spent - reserved_usd + actual_usd)
            return self._commit(candidate)

    def paper_cash(self) -> float:
        with self._lock:
            return float(self.data["paper"]["cash"])

    def paper_position(self, token_id: str) -> float:
        with self._lock:
            return float(self.data["paper"]["positions"].get(token_id, 0.0))

    def record_paper_fill(self, token_id: str, side: str, price: float,
                          shares: float, fee_usd: float,
                          budget_usd: float | None = None,
                          budget_limit: float | None = None) -> bool:
        values = (_finite_nonnegative(price), _finite_nonnegative(shares),
                  _finite_nonnegative(fee_usd), _finite_nonnegative(budget_usd)
                  if budget_usd is not None else 0.0)
        if (side not in ("BUY", "SELL") or not token_id or values[0] is None
                or values[0] <= 0 or values[1] is None or values[1] <= 0
                or values[2] is None or values[3] is None):
            return False
        with self._lock:
            candidate = copy.deepcopy(self.data)
            paper = candidate["paper"]
            held = float(paper["positions"].get(token_id, 0.0))
            notional = price * shares
            cash = float(paper["cash"])
            if side == "BUY":
                cash -= notional + fee_usd
                held += shares
            elif shares <= held + 1e-9:
                cash += notional - fee_usd
                held -= shares
            else:
                return False
            if cash < -1e-9:
                return False
            if budget_usd is not None:
                budget = self._today_budget(candidate)
                if budget_limit is not None and (
                        float(budget["spent_usd"]) + budget_usd > budget_limit + 1e-9):
                    return False
                candidate["budget"] = budget
                candidate["budget"]["spent_usd"] += budget_usd
            paper["cash"] = max(0.0, cash)
            if held <= 1e-9:
                paper["positions"].pop(token_id, None)
            else:
                paper["positions"][token_id] = held
            paper["fills"].append({"token_id": token_id, "side": side, "price": price,
                                   "shares": shares, "usd": round(notional, 4),
                                   "fee": round(fee_usd, 4)})
            return self._commit(candidate)

    def paper_totals(self, token_id: str) -> dict[str, float]:
        with self._lock:
            fills = [fill for fill in self.data["paper"]["fills"]
                     if not token_id or fill["token_id"] == token_id]
            return {"shares": round(sum(fill["shares"] for fill in fills if fill["side"] == "BUY")
                                    - sum(fill["shares"] for fill in fills if fill["side"] == "SELL"), 4),
                    "usd": round(sum(fill["usd"] for fill in fills if fill["side"] == "BUY"), 4),
                    "fee_estimate": round(sum(fill["fee"] for fill in fills
                                              if fill["side"] == "BUY"), 4)}

    def paper_portfolio(self) -> dict[str, Any]:
        with self._lock:
            positions = [{"token_id": token, "shares": round(shares, 4)}
                         for token, shares in sorted(self.data["paper"]["positions"].items())]
            return {"cash_usd": round(self.paper_cash(), 2), "positions": positions,
                    "fills": len(self.data["paper"]["fills"]), "paper": True}

    def status(self, active: bool) -> dict[str, Any]:
        return {"persistent": active and self.error is None and self.path.exists(),
                "healthy": self.error is None, "error": self.error}


_state = _McpState(_state_path(), PAPER_START_USD)
if PAPER_ENABLED or MAX_DAILY_USD > 0:
    _state.activate()

mcp = FastMCP(
    "pmq",
    instructions=(
        "Polymarket CLOB V2 data and fail-closed execution. Books are real "
        "time; the trade tape lags 1 to 3 minutes, never trade off it. "
        + ("PAPER trading tools are enabled: fills are SIMULATED against "
           "the real live top of book (real exchange minimums, estimated "
           "fees); no keys are involved and no order ever reaches the "
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


def _order_values(side: str, price_limit: Any,
                  amount: Any) -> tuple[str, float, float] | str:
    if not isinstance(side, str):
        return "side must be BUY or SELL"
    normalized = side.upper()
    if normalized not in ("BUY", "SELL"):
        return "side must be BUY or SELL"
    try:
        limit, size = float(price_limit), float(amount)
    except (TypeError, ValueError, OverflowError):
        return "price_limit and amount must be numbers"
    if not (math.isfinite(limit) and 0.0 < limit <= 1.0):
        return "price_limit must be finite and within (0, 1]"
    if not math.isfinite(size) or size <= 0:
        return "amount must be finite and positive"
    return normalized, limit, size


def _floor_shares(shares: float) -> float:
    return math.floor(shares * 10_000) / 10_000


def _preview_error(error: str) -> dict[str, Any]:
    return {"would_fill": False, "rejected": True, "error": error,
            "top_of_book_only": True, "fee_source": "crypto_table_estimate"}


def _top_of_book_level(raw_book: dict[str, Any], side: str,
                       price_limit: float) -> tuple[float, float, Any] | str:
    bid, bid_size, ask, ask_size = data.best_bid_ask(raw_book)
    price, displayed = (ask, ask_size) if side == "BUY" else (bid, bid_size)
    direction, comparison = ("ask", "under") if side == "BUY" else ("bid", "above")
    price_value = _finite_nonnegative(price)
    displayed_value = _finite_nonnegative(displayed)
    if price_value is None or price_value <= 0 or displayed_value is None or displayed_value <= 0:
        return f"best {direction} cannot support a fill"
    if ((side == "BUY" and price_value > price_limit)
            or (side == "SELL" and price_value < price_limit)):
        return f"no {direction} at or {comparison} {price_limit} (best {direction}: {price_value})"
    return price_value, displayed_value, data.book_meta(raw_book)


def _top_of_book_preview(token_id: str, side: str, price_limit: float,
                         amount: float, available: float | None = None,
                         ) -> dict[str, Any]:
    """Top-of-book simulation shared by preview and keyless paper fills."""
    raw_book = data.get_book(token_id)
    if not raw_book:
        return _preview_error("book unavailable")
    level = _top_of_book_level(raw_book, side, price_limit)
    if isinstance(level, str):
        return _preview_error(level)
    price_value, displayed_value, meta = level
    fee_per_share = _finite_nonnegative(data.fee(price_value, 1.0))
    if fee_per_share is None:
        return _preview_error("fee estimate unavailable")
    unit_cost = price_value + fee_per_share
    capacity = min(displayed_value, amount if side == "SELL" else amount / price_value)
    available_value = _finite_nonnegative(available) if available is not None else None
    if available is not None and available_value is None:
        return _preview_error("available balance is invalid")
    if side == "BUY" and available_value is not None:
        capacity = min(capacity, available_value / unit_cost)
    if side == "SELL" and available_value is not None:
        capacity = min(capacity, available_value)
    shares = _floor_shares(capacity)
    minimum = _finite_nonnegative(meta["min_order_size"])
    if minimum is None:
        return _preview_error("exchange minimum is invalid")
    if shares < minimum:
        return _preview_error(f"{shares} shares under the exchange minimum {minimum}")
    notional = round(price_value * shares, 4)
    fee_usd = _finite_nonnegative(data.fee(price_value, shares))
    if fee_usd is None:
        return _preview_error("fee estimate unavailable")
    return {"would_fill": True, "rejected": False, "side": side, "price": price_value,
            "displayed_shares": displayed_value, "matched_shares": shares,
            "matched_usd": notional, "fee_estimate_usd": fee_usd,
            "top_of_book_only": True, "fee_source": "crypto_table_estimate",
            **meta}


def _buy_rail_error(usd: float) -> str | None:
    if usd > MAX_USD:
        return f"refused: {usd} exceeds the {MAX_USD} USD per-order cap"
    left = _state.budget_left(MAX_DAILY_USD)
    if MAX_DAILY_USD > 0 and _state.error:
        return f"refused: durable budget state unavailable ({_state.error})"
    if left is not None and usd > left:
        return (f"refused: daily buy budget ({MAX_DAILY_USD:.2f} USD) leaves only "
                f"{max(left, 0.0):.2f} USD today")
    return None


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
def market_snapshot(slug: str) -> dict[str, Any]:
    """One read-only decision snapshot: resolve a market and return the live
    top-of-book summary for each outcome. This does not place or prepare an
    order, and a missing book is reported only for that outcome."""
    resolved = market(slug)
    if "error" in resolved:
        return {"error": str(resolved["error"])}
    outcomes = {name: {"token_id": token_id, "book": book(token_id)}
                for name, token_id in resolved["outcomes"].items()}
    return {"condition_id": resolved["condition_id"], "end_ts": resolved["end_ts"],
            "settled_winner": resolved["settled_winner"], "outcomes": outcomes}


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
def order_preview(token_id: str, side: str, price_limit: float,
                  amount: float) -> dict[str, Any]:
    """Read-only top-of-book FAK preview. For BUY, `amount` is USD and
    `price_limit` is the highest acceptable price. For SELL, `amount` is
    shares and `price_limit` is the lowest acceptable price. It never creates
    an executor, sends an order, or reserves budget. Fees are an estimate
    using the documented crypto-table rate until the exchange confirms a fill."""
    values = _order_values(side, price_limit, amount)
    if isinstance(values, str):
        return _preview_error(values)
    normalized, limit, size = values
    rail_error = _buy_rail_error(size) if normalized == "BUY" else None
    if rail_error:
        return _preview_error(rail_error)
    available = None
    if PAPER_ENABLED:
        paper_available = _paper_available(token_id, normalized, size)
        if isinstance(paper_available, str):
            return _preview_error(paper_available)
        available = paper_available
    out = _top_of_book_preview(token_id, normalized, limit, size, available)
    out["does_not_execute"] = True
    out["does_not_reserve_budget"] = True
    return out


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
def pmq_status() -> dict[str, Any]:
    """Operator-visible mode and safety-rail status. This is keyless: it
    never constructs a signer, checks a live balance, or changes state."""
    mode = "paper" if PAPER_ENABLED else "live" if LIVE_ENABLED else "read_only"
    state = _state.status(PAPER_ENABLED or MAX_DAILY_USD > 0)
    return {"mode": mode, "trading_tools_registered": LIVE_ENABLED or PAPER_ENABLED,
            "per_order_cap_usd": MAX_USD,
            "daily_buy_budget_usd": MAX_DAILY_USD if MAX_DAILY_USD > 0 else None,
            "daily_buy_budget_left_usd": _state.budget_left(MAX_DAILY_USD),
            "utc_day": _utc_day(), "state": state,
            "paper": _state.paper_portfolio() if PAPER_ENABLED else {"enabled": False}}


def _totals_dict(totals: tuple[float, float, float] | None) -> dict[str, float] | None:
    if totals is None:
        return None
    shares, usd, fees = totals
    return {"shares": shares, "usd": usd, "fee_estimate": fees}


@mcp.tool()
def account_collateral() -> dict[str, Any]:
    """Collateral (pUSD, $) the CLOB sees for the configured account. If this
    is 0 while funds are on-chain, the operator's POLY_SIG_TYPE is wrong
    (the Polymarket app's deposit wallet needs 3). In paper mode: the
    simulated cash balance."""
    if PAPER_ENABLED:
        return {"collateral_usd": round(_state.paper_cash(), 2), "paper": True}
    return {"collateral_usd": _ex().collateral()}


@mcp.tool()
def account_trades(condition_id: str, token_id: str = "") -> dict[str, Any]:
    """BUY-side totals of OUR account on one market: (shares, usd,
    fee_estimate), usd and fees counting BUY fills only. This is the
    reconciliation source, use it after any uncertainty instead of
    trusting local bookkeeping. Paper mode: same semantics over the
    simulated fills, except shares are net of paper sells (position,
    not gross buys)."""
    if PAPER_ENABLED:
        return {**_state.paper_totals(token_id), "paper": True}
    totals = _totals_dict(_ex().trades_totals(condition_id, token_id or None))
    return totals if totals is not None else {"error": "trades endpoint unreachable"}


def _public_portfolio(wallet: str, limit: int) -> dict[str, Any]:
    address = wallet or os.environ.get("POLY_FUNDER", "")
    if not address:
        return {"error": "provide wallet or set POLY_FUNDER to read a public portfolio"}
    try:
        safe_limit = min(max(int(limit), 1), 200)
    except (TypeError, ValueError, OverflowError):
        return {"error": "limit must be an integer"}
    return {"wallet": address, "positions": data.positions(address, limit=safe_limit),
            "source": "public_data_api_lagged"}


def _paper_available(token_id: str, side: str, amount: float) -> float | str:
    available = _state.paper_cash() if side == "BUY" else _state.paper_position(token_id)
    if side == "SELL" and amount > available + 1e-9:
        return f"position is {available} shares, cannot sell {amount}"
    return available


@mcp.tool()
def account_portfolio(wallet: str = "", limit: int = 200) -> dict[str, Any]:
    """Current portfolio without placing an order. Paper mode returns the
    local durable ledger. Otherwise pass a public wallet address, or configure
    POLY_FUNDER, to read its public Data API positions. Data API values lag the
    matching engine and are not exchange reconciliation truth."""
    if PAPER_ENABLED:
        return _state.paper_portfolio()
    return _public_portfolio(wallet, limit)


def _paper_order(token_id: str, side: str, price_limit: float,
                 amount: float) -> dict[str, Any]:
    available = _paper_available(token_id, side, amount)
    if isinstance(available, str):
        return {"booked": False, "rejected": True, "paper": True,
                "error": available}
    preview = _top_of_book_preview(token_id, side, price_limit, amount, available)
    if not preview["would_fill"]:
        return {"booked": False, "rejected": True, "paper": True,
                "error": preview["error"]}
    budget = preview["matched_usd"] if side == "BUY" and MAX_DAILY_USD > 0 else None
    if not _state.record_paper_fill(token_id, side, preview["price"],
                                    preview["matched_shares"],
                                    preview["fee_estimate_usd"], budget,
                                    MAX_DAILY_USD if budget is not None else None):
        reason = "balance" if side == "BUY" else "position"
        return {"error": f"paper state unavailable ({_state.error or reason + ' changed'})",
                "paper": True}
    return {"booked": True, "paper": True,
            "matched_shares": preview["matched_shares"],
            "matched_usd": preview["matched_usd"], "price": preview["price"],
            "fee_usd": preview["fee_estimate_usd"],
            "fee_source": preview["fee_source"],
            "top_of_book_only": True, "cash_left": round(_state.paper_cash(), 2)}


def _live_buy(token_id: str, price_cap: float, usd: float) -> dict[str, Any]:
    """Execute a buy only after its daily limit has been durably reserved."""
    executor = _ex()
    if MAX_DAILY_USD > 0 and not _state.reserve_budget(MAX_DAILY_USD, usd):
        return {"error": _buy_rail_error(usd) or "refused: unable to reserve daily buy budget"}
    try:
        fill = executor.buy_fak(token_id, price_cap, usd)
    except OrderUncertain as e:
        return {"error": f"outcome unknown ({e}); the full daily-budget reservation "
                         "remains through the UTC reset. Call cancel_and_reconcile "
                         "to inspect exchange truth."}
    out = _fill_dict(fill)
    if MAX_DAILY_USD <= 0:
        return out
    actual_usd = _finite_nonnegative(fill.matched_usd) if fill else 0.0
    if actual_usd is None or not _state.settle_budget(usd, actual_usd):
        out["warning"] = ("daily-budget reservation retained because confirmed spend "
                          "could not be persisted")
    return out


def _trade_order(side: str, token_id: str, price_limit: float,
                 amount: float) -> dict[str, Any]:
    values = _order_values(side, price_limit, amount)
    if isinstance(values, str):
        return {"error": values}
    normalized, limit, size = values
    if normalized == "BUY":
        rail_error = _buy_rail_error(size)
        if rail_error:
            return {"error": rail_error}
    if PAPER_ENABLED:
        return _paper_order(token_id, normalized, limit, size)
    if normalized == "BUY":
        return _live_buy(token_id, limit, size)
    try:
        return _fill_dict(_ex().sell_fak(token_id, limit, size))
    except OrderUncertain as e:
        return {"error": f"outcome unknown ({e}); call account_trades before any new order"}


if LIVE_ENABLED or PAPER_ENABLED:
    @mcp.tool()
    def fak_buy(token_id: str, price_cap: float, usd: float) -> dict[str, Any]:
        """Place a fill-and-kill market BUY: spend up to `usd` at prices no
        worse than `price_cap`. Nothing rests on the book. Book ONLY what
        `matched_shares`/`matched_usd` report; `booked: false` means nothing
        happened. Hard-capped per call by the operator's PMQ_MCP_MAX_USD and
        per UTC day by PMQ_MCP_DAILY_USD when set. A confirmed rejection costs
        nothing. An unknown live result keeps the full requested reservation
        through the UTC-day reset."""
        return _trade_order("BUY", token_id, price_cap, usd)

    @mcp.tool()
    def fak_sell(token_id: str, price_floor: float, shares: float) -> dict[str, Any]:
        """Fill-and-kill market SELL of `shares` at prices no worse than
        `price_floor`. Same confirmation contract as fak_buy."""
        return _trade_order(side="SELL", token_id=token_id,
                            price_limit=price_floor, amount=shares)

    @mcp.tool()
    def cancel_and_reconcile(condition_id: str, token_id: str = "") -> dict[str, Any]:
        """Cancel every resting order of ours on one market, verify none
        stayed open, and return exchange-truth totals. Call this after any
        'outcome unknown' before placing new orders on that market. It does
        not release an unknown BUY's daily-budget reservation."""
        if PAPER_ENABLED:
            return {"cancelled": True, "paper": True,
                    "note": "paper mode: FAK only, nothing ever rests"}
        totals = _totals_dict(_ex().reconcile(condition_id, token_id or None))
        if totals is None:
            return {"cancelled": True, "error": "trades endpoint unreachable"}
        return {"cancelled": True, **totals}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
