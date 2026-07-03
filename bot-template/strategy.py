"""Your strategy lives here. The engine imports NAME, watchlist() and
decide(); everything else in this repo is plumbing that works on any
Polymarket market (politics, sports, crypto, culture).

watchlist() -> iterable of gamma slugs to track right now. A static list
    from the environment, an API discovery call, or generated names for
    recurring markets: your call. It is polled continuously, so the list
    can change over time.

decide(pm, book_a, book_b, remaining_usd, side_held, state)
    -> None, or (side_key, price_cap, usd)
    Called on every poll for every tracked market that is still open.
    side_key is 'a' or 'b' (see pm['outcome_a']/pm['outcome_b'] for what
    they mean on THIS market), price_cap the worst price you accept, usd
    how much to spend. The engine enforces the per-market budget with fee
    headroom, one side per market, halts and reconciliation; you only
    express intent. pm['end_ts'] gives the close time when the market has
    one. state is a dict private to this market, persist what you want.

The demo below is an API ILLUSTRATION, deliberately naive, negative EV
after adverse selection. It exists so paper mode shows the plumbing
working. Replace it with your own research; that part is on you.
"""
import os

import pmq

NAME = "demo-do-not-trade-this"

SLUGS = [s.strip() for s in os.environ.get("BOT_SLUGS", "").split(",") if s.strip()]


def watchlist():
    return SLUGS


def decide(pm, book_a, book_b, remaining_usd, side_held, state):
    # Demo: once per market, buy 5$ of whichever outcome is quoted at or
    # above 0.98 (a near-settled favorite). Textbook carry, usually a bad
    # trade. Delete me. The 5$ keeps the demo above the typical per-market
    # exchange minimum (min_order_size shares, read from the live book by
    # the engine).
    if state.get("done"):
        return None
    for side_key, book in (("a", book_a), ("b", book_b)):
        _, _, ask, ask_sz = pmq.best_bid_ask(book)
        if ask is not None and 0.98 <= ask <= 0.99 and ask_sz and ask_sz * ask >= 5:
            state["done"] = True
            return (side_key, ask, min(5.0, remaining_usd))
    return None
