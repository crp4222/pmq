"""Market data layer for Polymarket, with the operational knowledge encoded.

Facts this module encodes (verified live, July 2026):

* The CLOB ``/book`` endpoint is REAL TIME (served by the matching engine,
  includes ``last_trade_price``). The data-api ``/trades`` indexer LAGS
  matching by 1 to 3 minutes (measured: freshest visible trade 120s old).
  Therefore: books drive live decisions; the trade tape is for OFFLINE
  scoring only, at least 5 minutes after the window closes.
* Gamma ``/markets?slug=`` returns ``[]`` for EXPIRED short-lived markets;
  ``/events?slug=`` still resolves them. :func:`get_market` does the
  fallback for you.
* Gamma settlement (outcomePrices pinned to 0.99+) can lag the market close
  by more than 15 minutes; the last pre-close book identifies the winner
  immediately (a side bid pinned at 0.90+ means that side won). See
  :func:`book_inferred_winner`.
* Taker fees (since 2026-03-30, decided at match time under CLOB V2):
  ``fee = rate * price * (1 - price) * shares`` with a per-category rate,
  see :data:`FEE_RATES`. Makers always pay zero. The ``maker/taker_base_fee``
  of 1000 bps seen in API responses is an on-chain CAP, never the charge.
"""
from __future__ import annotations

import calendar
import json
import logging
import math
import time
import urllib.request
from typing import Any, Callable, TypedDict

Logger = Callable[[str], None]

log = logging.getLogger("pmq")

UA = {"User-Agent": "Mozilla/5.0"}
#: Hard cap on HTTP response bodies (books can be big; a healthy one stays
#: far under this). An oversized body reads as a failed GET, never a partial
#: or unbounded read.
_MAX_BODY = 8 * 1024 * 1024
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

#: Official taker fee rates per market category (docs.polymarket.com/trading/fees,
#: fetched 2026-07-03). Fee in $ = rate * p * (1 - p) * shares. Makers pay 0.
FEE_RATES: dict[str, float] = {
    "crypto": 0.07,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "mentions": 0.04,
    "tech": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "geopolitics": 0.0,
}


class ParsedMarket(TypedDict):
    """Normalized view of a Gamma market object, see :func:`parse_market`."""
    condition_id: str | None
    slug: str | None
    token_a: str
    token_b: str
    outcome_a: str
    outcome_b: str
    outcome_prices_raw: Any
    idx_a: int
    end_ts: int | None


class BookMeta(TypedDict):
    """Exchange metadata riding on a book response, see :func:`book_meta`."""
    min_order_size: float | None
    tick_size: float | None
    neg_risk: bool | None
    last_trade_price: float | None


def fee(price: float, shares: float, rate: float = FEE_RATES["crypto"]) -> float:
    """Taker fee in $ under the current schedule. Makers pay zero.

    The fee peaks at price 0.50 and vanishes toward 0 and 1: a taker fill at
    0.95 costs about a third of one at 0.50 for the same share count.
    """
    return rate * price * (1.0 - price) * shares


def http_get_json(url: str, retries: int = 3, timeout: float = 10,
                  logger: Logger | None = None) -> Any:
    """GET a JSON document with linear backoff. Returns None on final failure.
    Bodies above 8 MB read as failure too (no unbounded read)."""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(_MAX_BODY + 1)
                if len(body) > _MAX_BODY:
                    raise ValueError(f"response body exceeds {_MAX_BODY} bytes")
                return json.loads(body.decode())
        except Exception as e:
            if i == retries - 1:
                if logger:
                    logger(f"GET failed permanently: {url} ({e})")
                return None
            time.sleep(1.5 * (i + 1))
    return None


def get_market(slug: str, logger: Logger | None = None) -> dict[str, Any] | None:
    """Gamma market object for a slug, falling back to /events for expired ones."""
    data = http_get_json(f"{GAMMA}/markets?slug={slug}", logger=logger)
    if data:
        first: dict[str, Any] = data[0]
        return first
    ev = http_get_json(f"{GAMMA}/events?slug={slug}", logger=logger)
    if ev and ev[0].get("markets"):
        fallback: dict[str, Any] = ev[0]["markets"][0]
        return fallback
    return None


def _end_ts(m: dict[str, Any]) -> int | None:
    """Market close time as unix epoch, or None. Gamma sends UTC ISO 8601."""
    raw = m.get("endDate") or m.get("endDateIso") or ""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return calendar.timegm(time.strptime(raw, fmt))
        except (ValueError, OverflowError):
            continue
    return None


def parse_market(m: dict[str, Any] | None, outcome_a: str | None = None,
                 outcome_b: str | None = None) -> ParsedMarket | None:
    """Extract condition id and outcome token ids from a Gamma market object.

    Works on ANY binary market: politics, sports, crypto, whatever the
    outcome names are (Yes/No, Up/Down, team names). By default the two
    outcomes are taken in the order the market declares them; pass
    ``outcome_a``/``outcome_b`` to pin a specific one to the ``a`` slot.
    Returns None on any shape surprise (fail closed).
    """
    if not m:
        return None
    try:
        outcomes: Any = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m["outcomes"]
        token_ids: Any = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m["clobTokenIds"]
        a = outcomes.index(outcome_a) if outcome_a else 0
        b = outcomes.index(outcome_b) if outcome_b else (1 if a == 0 else 0)
        return ParsedMarket(
            condition_id=m.get("conditionId"),
            slug=m.get("slug"),
            token_a=token_ids[a],
            token_b=token_ids[b],
            outcome_a=outcomes[a],
            outcome_b=outcomes[b],
            outcome_prices_raw=m.get("outcomePrices"),
            idx_a=int(a),
            end_ts=_end_ts(m),
        )
    except Exception:
        return None


def resolved_winner(pm: ParsedMarket | None) -> str | None:
    """Winning outcome name from settled Gamma outcomePrices; None if unsettled.
    Prices must be finite and within [0, 1]: json.loads accepts NaN, and a
    hostile or drifted price must never surface a winner (fail closed)."""
    if not pm:
        return None
    try:
        op = pm["outcome_prices_raw"]
        op = json.loads(op) if isinstance(op, str) else op
        op = [float(x) for x in op]
        if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in op) or max(op) < 0.99:
            return None
        ia = pm.get("idx_a", 0)
        return pm["outcome_a"] if op[ia] > op[1 - ia] else pm["outcome_b"]
    except Exception:
        return None


def get_book(token_id: str, logger: Logger | None = None) -> dict[str, Any] | None:
    """Real-time CLOB book. THE live data source; never the trade tape.
    A non-dict body reads as no book (fail closed)."""
    book = http_get_json(f"{CLOB}/book?token_id={token_id}", logger=logger)
    return book if isinstance(book, dict) else None


def _levels(side: Any) -> tuple[list[tuple[float, float]], int]:
    """Valid (price, size) pairs of one book side plus the count of levels
    excluded. A level counts ONLY if it parses to a finite price within
    [0, 1] and a finite size >= 0: json.loads accepts NaN/Infinity, and a
    malformed or hostile level must never reach a quote, a depth sum or a
    paper fill. A side that is not a list reads as empty."""
    if not isinstance(side, list):
        return [], 0 if not side else 1
    out: list[tuple[float, float]] = []
    excluded = 0
    for lvl in side:
        try:
            p, s = float(lvl["price"]), float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            excluded += 1
            continue
        if math.isfinite(p) and math.isfinite(s) and 0.0 <= p <= 1.0 and s >= 0.0:
            out.append((p, s))
        else:
            excluded += 1
    return out, excluded


def _finite_or_none(x: float) -> float | None:
    return x if math.isfinite(x) else None


def best_bid_ask(book: dict[str, Any] | None,
                 ) -> tuple[float | None, float | None, float | None, float | None]:
    """(best_bid, bid_size, best_ask, ask_size), sizes summed at the level.

    Invalid levels (non-finite or out-of-range price or size, wrong shape)
    are EXCLUDED, with one warning per call counting what was skipped. A
    side with no valid level reads as empty (None quote), exactly like a
    genuinely empty book."""
    b = book if isinstance(book, dict) else {}
    bids, x_bid = _levels(b.get("bids"))
    asks, x_ask = _levels(b.get("asks"))
    if x_bid or x_ask:
        log.warning("best_bid_ask: excluded %d invalid book levels", x_bid + x_ask)
    bb = max((p for p, _ in bids), default=None)
    ba = min((p for p, _ in asks), default=None)
    bb_sz = _finite_or_none(sum(s for p, s in bids if p == bb)) if bb is not None else None
    ba_sz = _finite_or_none(sum(s for p, s in asks if p == ba)) if ba is not None else None
    return bb, bb_sz, ba, ba_sz


def book_meta(book: dict[str, Any] | None) -> BookMeta:
    """Exchange metadata riding on the book response: per-market minimum
    order size (shares), tick size, neg_risk flag, last trade price. Read
    these from the live book instead of hardcoding exchange rules.
    Non-finite or out-of-range values fall back to None, never NaN."""
    b: dict[str, Any] = book if isinstance(book, dict) else {}

    def _f(k: str, hi: float = math.inf) -> float | None:
        try:
            v = float(b[k])
        except (KeyError, TypeError, ValueError):
            return None
        return v if math.isfinite(v) and 0.0 <= v <= hi else None

    nr = b.get("neg_risk")
    return {"min_order_size": _f("min_order_size"),
            "tick_size": _f("tick_size", hi=1.0),
            "neg_risk": nr if isinstance(nr, bool) else None,
            "last_trade_price": _f("last_trade_price", hi=1.0)}


def band_ask_depth_usd(book: dict[str, Any] | None, lo: float, hi: float) -> float:
    """Total $ notional of asks resting within [lo, hi]. Invalid levels are
    EXCLUDED (one warning per call with the count); a non-finite total
    fails closed to 0.0."""
    asks, excluded = _levels(book.get("asks") if isinstance(book, dict) else None)
    if excluded:
        log.warning("band_ask_depth_usd: excluded %d invalid book levels", excluded)
    total = sum(p * s for p, s in asks if lo <= p <= hi)
    return round(total, 2) if math.isfinite(total) else 0.0


def book_inferred_winner(bid_a: float | None, bid_b: float | None,
                         threshold: float = 0.90) -> str | None:
    """Winner from a last pre-close book snapshot; None if ambiguous.

    Use when Gamma settlement lags: a side whose BID is pinned at or above
    ``threshold`` at close identifies the winner immediately.
    """
    if bid_a is not None and bid_a >= threshold:
        return "a"
    if bid_b is not None and bid_b >= threshold:
        return "b"
    return None


def event_markets(slug: str, logger: Logger | None = None) -> list[ParsedMarket]:
    """All binary markets of one event (multi-outcome events like elections
    or tournaments are one binary market per candidate). Returns a list of
    :func:`parse_market` dicts; unparseable members are skipped."""
    ev = http_get_json(f"{GAMMA}/events?slug={slug}", logger=logger)
    if not ev:
        return []
    out: list[ParsedMarket] = []
    for m in ev[0].get("markets") or []:
        pm = parse_market(m)
        if pm:
            out.append(pm)
    return out


def positions(user_address: str, logger: Logger | None = None,
              limit: int = 200) -> list[dict[str, Any]]:
    """Current holdings of a wallet per the data-api (public, ~1 min lag).
    Answers "what do I hold?" after fills: list of dicts with asset,
    conditionId, size, avgPrice, currentValue and friends."""
    return http_get_json(f"{DATA}/positions?user={user_address}&limit={limit}",
                         logger=logger) or []


def get_tape(condition_id: str, since_ts: float, max_pages: int = 4,
             logger: Logger | None = None) -> list[dict[str, Any]]:
    """Complete trade tape for a closed market (paginated, newest first).

    OFFLINE USE ONLY: the indexer lags matching by 1 to 3 minutes; call at
    least 5 minutes after close or you will score against missing fills.
    """
    out: list[dict[str, Any]] = []
    for page in range(max_pages):
        batch = http_get_json(
            f"{DATA}/trades?market={condition_id}&limit=500&offset={page*500}",
            logger=logger) or []
        out.extend(batch)
        if not batch or min(int(t.get("timestamp", 0)) for t in batch) < since_ts:
            break
    return out
