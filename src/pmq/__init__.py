"""pmq: fail-closed execution and market data for Polymarket CLOB V2.

Data layer (no keys needed): get_market, parse_market, get_book,
best_bid_ask, band_ask_depth_usd, resolved_winner, book_inferred_winner,
get_tape, fee, FEE_RATES.

Execution layer (keys stay local, nothing booked without exchange
confirmation): PolymarketExecutor, Fill, OrderUncertain.
"""
from .data import (
    FEE_RATES,
    band_ask_depth_usd,
    best_bid_ask,
    book_inferred_winner,
    book_meta,
    event_markets,
    fee,
    get_book,
    get_market,
    get_tape,
    http_get_json,
    parse_market,
    positions,
    resolved_winner,
)
from .exceptions import IntrospectionMismatch, OrderUncertain, PmqError

__version__ = "0.5.0"
__all__ = [
    "FEE_RATES", "band_ask_depth_usd", "best_bid_ask", "book_inferred_winner",
    "book_meta", "event_markets", "fee", "get_book", "get_market", "get_tape",
    "http_get_json", "parse_market", "positions", "resolved_winner",
    "PolymarketExecutor", "Fill",
    "PmqError", "OrderUncertain", "IntrospectionMismatch",
    "__version__",
]


def __getattr__(name: str) -> object:
    # Lazy import: the data layer must stay usable without py-clob-client-v2.
    if name in ("PolymarketExecutor", "Fill", "DEFAULT_BUILDER_CODE"):
        from . import executor
        return getattr(executor, name)
    raise AttributeError(name)
