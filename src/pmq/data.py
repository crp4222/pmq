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
import calendar
import json
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

#: Official taker fee rates per market category (docs.polymarket.com/trading/fees,
#: fetched 2026-07-03). Fee in $ = rate * p * (1 - p) * shares. Makers pay 0.
FEE_RATES = {
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


def fee(price, shares, rate=FEE_RATES["crypto"]):
    """Taker fee in $ under the current schedule. Makers pay zero.

    The fee peaks at price 0.50 and vanishes toward 0 and 1: a taker fill at
    0.95 costs about a third of one at 0.50 for the same share count.
    """
    return rate * price * (1.0 - price) * shares


def http_get_json(url, retries=3, timeout=10, logger=None):
    """GET a JSON document with linear backoff. Returns None on final failure."""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == retries - 1:
                if logger:
                    logger(f"GET failed permanently: {url} ({e})")
                return None
            time.sleep(1.5 * (i + 1))
    return None


def get_market(slug, logger=None):
    """Gamma market object for a slug, falling back to /events for expired ones."""
    data = http_get_json(f"{GAMMA}/markets?slug={slug}", logger=logger)
    if data:
        return data[0]
    ev = http_get_json(f"{GAMMA}/events?slug={slug}", logger=logger)
    if ev and ev[0].get("markets"):
        return ev[0]["markets"][0]
    return None


def _end_ts(m):
    """Market close time as unix epoch, or None. Gamma sends UTC ISO 8601."""
    raw = m.get("endDate") or m.get("endDateIso") or ""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return calendar.timegm(time.strptime(raw, fmt))
        except (ValueError, OverflowError):
            continue
    return None


def parse_market(m, outcome_a=None, outcome_b=None):
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
        outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
        token_ids = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
        a = outcomes.index(outcome_a) if outcome_a else 0
        b = outcomes.index(outcome_b) if outcome_b else (1 if a == 0 else 0)
        return {
            "condition_id": m.get("conditionId"),
            "slug": m.get("slug"),
            "token_a": token_ids[a],
            "token_b": token_ids[b],
            "outcome_a": outcomes[a],
            "outcome_b": outcomes[b],
            "outcome_prices_raw": m.get("outcomePrices"),
            "idx_a": a,
            "end_ts": _end_ts(m),
        }
    except Exception:
        return None


def resolved_winner(pm):
    """Winning outcome name from settled Gamma outcomePrices; None if unsettled."""
    if not pm:
        return None
    try:
        op = pm["outcome_prices_raw"]
        op = json.loads(op) if isinstance(op, str) else op
        op = [float(x) for x in op]
        if max(op) < 0.99:
            return None
        ia = pm.get("idx_a", 0)
        return pm["outcome_a"] if op[ia] > op[1 - ia] else pm["outcome_b"]
    except Exception:
        return None


def get_book(token_id, logger=None):
    """Real-time CLOB book. THE live data source; never the trade tape."""
    return http_get_json(f"{CLOB}/book?token_id={token_id}", logger=logger)


def best_bid_ask(book):
    """(best_bid, bid_size, best_ask, ask_size), sizes summed at the level."""
    if not book:
        return None, None, None, None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bb = max((float(b["price"]) for b in bids), default=None)
    ba = min((float(a["price"]) for a in asks), default=None)
    bb_sz = sum(float(b["size"]) for b in bids if float(b["price"]) == bb) if bb is not None else None
    ba_sz = sum(float(a["size"]) for a in asks if float(a["price"]) == ba) if ba is not None else None
    return bb, bb_sz, ba, ba_sz


def book_meta(book):
    """Exchange metadata riding on the book response: per-market minimum
    order size (shares), tick size, neg_risk flag, last trade price. Read
    these from the live book instead of hardcoding exchange rules."""
    b = book or {}
    def _f(k):
        try:
            return float(b[k])
        except (KeyError, TypeError, ValueError):
            return None
    return {"min_order_size": _f("min_order_size"), "tick_size": _f("tick_size"),
            "neg_risk": b.get("neg_risk"), "last_trade_price": _f("last_trade_price")}


def band_ask_depth_usd(book, lo, hi):
    """Total $ notional of asks resting within [lo, hi]."""
    asks = (book or {}).get("asks") or []
    return round(sum(float(a["price"]) * float(a["size"]) for a in asks
                     if lo <= float(a["price"]) <= hi), 2)


def book_inferred_winner(bid_a, bid_b, threshold=0.90):
    """Winner from a last pre-close book snapshot; None if ambiguous.

    Use when Gamma settlement lags: a side whose BID is pinned at or above
    ``threshold`` at close identifies the winner immediately.
    """
    if bid_a is not None and bid_a >= threshold:
        return "a"
    if bid_b is not None and bid_b >= threshold:
        return "b"
    return None


def get_tape(condition_id, since_ts, max_pages=4, logger=None):
    """Complete trade tape for a closed market (paginated, newest first).

    OFFLINE USE ONLY: the indexer lags matching by 1 to 3 minutes; call at
    least 5 minutes after close or you will score against missing fills.
    """
    out = []
    for page in range(max_pages):
        batch = http_get_json(
            f"{DATA}/trades?market={condition_id}&limit=500&offset={page*500}",
            logger=logger) or []
        out.extend(batch)
        if not batch or min(int(t.get("timestamp", 0)) for t in batch) < since_ts:
            break
    return out
