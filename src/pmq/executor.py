"""Fail-closed execution layer for Polymarket CLOB V2.

Design contract, in one paragraph: nothing is ever booked unless the exchange
confirmed it. A fill exists only if the order response is a dict bearing an
orderID and not flagged failed, and only for the matched size the response
reports. A 4xx is a clean rejection (the order does not exist). A timeout or
5xx raises :class:`~pmq.exceptions.OrderUncertain` and the caller must
reconcile via :meth:`PolymarketExecutor.reconcile` before trading that market
again. Unparseable responses count as zero fill. This is the difference
between a bot that survives a flaky network and one that buys its stake again
on every poll.

Production notes baked in (all verified live on CLOB V2, July 2026):

* FAK/FOK BUY orders are treated by the CLOB as MARKET orders: the maker
  amount is USDC with at most 2 decimals. Posting them as plain limit orders
  fails with ``invalid amounts, the market buy orders maker amount supports a
  max accuracy of 2 decimals``. pmq routes them through the market-order
  builder, which applies the correct rounding.
* A 400 ``no orders found to match with FAK order`` WITH an orderID in the
  body is a clean no-fill (the ask vanished between your poll and your send),
  not an error state.
* The balance endpoint derives the funding wallet server-side from the
  authenticated EOA and ``signature_type``; the ``funder`` parameter is never
  sent. Funds held by a deposit wallet (ERC-1271) are only visible with
  ``signature_type=3`` (POLY_1271).
* Builder attribution rides INSIDE the signed order (bytes32 code); HTTP
  headers attribute nothing on V2. pmq sets the code in BOTH places that
  py-clob-client-v2 honors: per order args and in the client BuilderConfig,
  so attribution survives whichever path a client version prefers.
"""
from __future__ import annotations

import inspect
import logging
import math
import os
import re
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from .data import FEE_RATES, fee
from .exceptions import IntrospectionMismatch, OrderUncertain

log = logging.getLogger("pmq")

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
BYTES32_RE = re.compile(r"0x[0-9a-fA-F]{64}")

# Builder attribution default. DISCLOSURE: this is the pmq maintainer's public
# builder code (commission 0/0: it never adds fees to your orders). It funds
# the project through Polymarket's builder program at zero cost to you.
# Opt out with PolymarketExecutor(builder_code=None) or use your own code
# from https://polymarket.com/settings?tab=builder
DEFAULT_BUILDER_CODE = "0x4b22812cf929165a247b575eb417a3b6c9e3c12e96f0159c4d0ad39f78d17371"
_UNSET: Any = object()

_CENT = Decimal("0.01")


def _patch_market_taker_precision() -> None:
    """py-clob-client-v2 1.0.2 reuses its LIMIT-order rounding table for
    MARKET orders. For ticks finer than 0.01 that table allows taker amounts
    with 5-6 decimals, which the V2 exchange rejects outright ("invalid
    amounts ... taker amount a max of 4 decimals"), so every FAK on such a
    market fails. Until upstream fixes the table, clamp the market path to 4
    decimals: round-down semantics keep the never-exceed-budget contract and
    the dust given up is under 0.0001 share per order."""
    from py_clob_client_v2.order_builder import builder as _builder
    original = _builder.OrderBuilder.get_market_order_amounts
    if getattr(original, "_pmq_taker4", False):
        return

    def clamped(self: Any, side: Any, amount: float, price: float,
                round_config: Any) -> Any:
        if round_config.amount > 4:
            round_config = _builder.RoundConfig(
                price=round_config.price, size=round_config.size, amount=4)
        return original(self, side, amount, price, round_config)

    clamped._pmq_taker4 = True  # type: ignore[attr-defined]
    _builder.OrderBuilder.get_market_order_amounts = clamped


def _floor_cents(x: float) -> float:
    """Round DOWN to the cent without the binary-float drift that makes
    ``int(16.90 * 100) / 100`` return ``16.89``. ``str(x)`` yields the shortest
    decimal repr, so ``16.90`` stays ``16.90`` while a genuine ``16.907`` still
    floors to ``16.90``. Rounding down preserves the never-exceed-budget
    contract for buys and the never-oversell contract for sells."""
    return float(Decimal(str(x)).quantize(_CENT, rounding=ROUND_DOWN))


@dataclass
class Fill:
    """Exchange-confirmed execution result. ``matched_shares == 0`` means no
    fill happened and NOTHING must be booked, whatever the caller hoped."""
    order_id: str = ""
    matched_shares: float = 0.0
    matched_usd: float = 0.0
    rejected: bool = False
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def price(self) -> float | None:
        return self.matched_usd / self.matched_shares if self.matched_shares else None

    def __bool__(self) -> bool:
        return self.matched_shares > 0


# (method name, parameters that must exist) verified against py-clob-client-v2
# 1.0.2 by introspection. If the installed client drifts, we refuse to start.
_EXPECTED_METHODS: dict[str, tuple[str, ...]] = {
    "create_and_post_market_order": ("order_args", "order_type"),
    "create_and_post_order": ("order_args", "order_type"),
    "cancel_market_orders": ("payload",),
    "get_open_orders": ("params",),
    "get_trades": ("params",),
    "get_balance_allowance": ("params",),
}
_EXPECTED_MARKET_ARGS: tuple[str, ...] = (
    "token_id", "amount", "side", "price", "builder_code")
_EXPECTED_ORDER_ARGS: tuple[str, ...] = (
    "token_id", "price", "size", "side", "builder_code")


class PolymarketExecutor:
    """Thin, fail-closed wrapper around the official py-clob-client-v2.

    Reads POLY_PRIVATE_KEY, POLY_FUNDER, POLY_SIG_TYPE and POLY_BUILDER_CODE
    from the environment unless passed explicitly. The private key is used to
    instantiate the signer and is never logged.

    ``signature_type``: 0 EOA, 1 POLY_PROXY (email/Magic), 2 POLY_GNOSIS_SAFE,
    3 POLY_1271 (deposit wallet, the default wallet of the Polymarket app).
    If the CLOB shows a 0 balance while the funds sit on your funder address
    on-chain, your signature_type is wrong: only the matching type makes the
    server derive the wallet that actually holds the pUSD.
    """

    def __init__(self, key: str | None = None, funder: str | None = None,
                 signature_type: int | None = None,
                 builder_code: str | None = _UNSET,
                 host: str = HOST, chain_id: int = CHAIN_ID,
                 client: Any = None, derive_creds: bool = True) -> None:
        from py_clob_client_v2.clob_types import (
            AssetType,
            BalanceAllowanceParams,
            BuilderConfig,
            MarketOrderArgsV2,
            OpenOrderParams,
            OrderArgsV2,
            OrderMarketCancelParams,
            OrderType,
            TradeParams,
        )
        from py_clob_client_v2.exceptions import PolyApiException
        self._t: dict[str, Any] = {
            "AssetType": AssetType, "BalanceAllowanceParams": BalanceAllowanceParams,
            "MarketOrderArgs": MarketOrderArgsV2, "OpenOrderParams": OpenOrderParams,
            "OrderArgs": OrderArgsV2, "OrderMarketCancelParams": OrderMarketCancelParams,
            "OrderType": OrderType, "TradeParams": TradeParams,
        }
        self.PolyApiException: type[Exception] = PolyApiException
        _patch_market_taker_precision()

        if builder_code is _UNSET:
            builder_code = os.environ.get("POLY_BUILDER_CODE", DEFAULT_BUILDER_CODE)
        if builder_code and not BYTES32_RE.fullmatch(builder_code):
            raise ValueError("builder_code must be bytes32 hex (0x + 64 hex chars) or None")
        self.builder_code: str | None = builder_code

        if client is not None:
            self.client = client
            self.funder: str | None = funder or os.environ.get("POLY_FUNDER")
        else:
            from py_clob_client_v2.client import ClobClient
            key = key or os.environ.get("POLY_PRIVATE_KEY")
            if not key:
                raise ValueError("POLY_PRIVATE_KEY missing (env or key= argument)")
            funder = funder or os.environ.get("POLY_FUNDER")
            if signature_type is None:
                signature_type = int(os.environ.get("POLY_SIG_TYPE", "0"))
            if signature_type > 0 and not funder:
                raise ValueError("signature_type > 0 requires the funder wallet address")
            builder_config = BuilderConfig(builder_code=builder_code) if builder_code else None
            self.client = ClobClient(host, chain_id=chain_id, key=key,
                                     signature_type=signature_type, funder=funder,
                                     builder_config=builder_config,
                                     use_server_time=True, retry_on_error=True)
            self.funder = funder
            if derive_creds:
                self.client.set_api_creds(self.client.create_or_derive_api_key())
        self._verify_client_surface()
        log.info("executor ready (builder=%s)", "on" if builder_code else "off")

    # ---------------- introspection guard ----------------
    def _surface_drifts(self) -> list[str]:
        drifts: list[str] = []
        for name, params in _EXPECTED_METHODS.items():
            fn = getattr(self.client, name, None)
            if fn is None:
                drifts.append(f"method {name} missing")
                continue
            try:
                have = set(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                continue
            for p in params:
                if p not in have:
                    drifts.append(f"{name}() lost parameter {p}")
        for label, ctor, expected in (
                ("MarketOrderArgsV2", self._t["MarketOrderArgs"], _EXPECTED_MARKET_ARGS),
                ("OrderArgsV2", self._t["OrderArgs"], _EXPECTED_ORDER_ARGS)):
            have = set(inspect.signature(ctor).parameters)
            drifts += [f"{label} lost field {p}" for p in expected if p not in have]
        if not hasattr(self._t["OrderType"], "FAK"):
            drifts.append("OrderType.FAK missing")
        drifts += self._amount_precision_drifts()
        return drifts

    def _amount_precision_drifts(self) -> list[str]:
        """Behavioral guard: the exchange caps signed MARKET-order amounts at
        2 decimals (maker) and 4 (taker) whatever the tick size. Exercise the
        installed builder's arithmetic on awkward pairs across every rounding
        config; a client build that would sign a rejectable pair is refused
        here, at startup, instead of failing on the first fine-tick order."""
        try:
            from py_clob_client_v2.order_builder.builder import (
                ROUNDING_CONFIG,
                OrderBuilder,
            )
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except ImportError:
            return []
        out: set[str] = set()
        probe = object.__new__(OrderBuilder)
        cases = ((BUY, 9.98, 0.985), (BUY, 4.97, 0.983),
                 (SELL, 10.13, 0.985), (SELL, 5.35, 0.933))
        for tick, rc in ROUNDING_CONFIG.items():
            for side, amount, price in cases:
                try:
                    _, mk, tk = OrderBuilder.get_market_order_amounts(
                        probe, side, amount, price, rc)
                except Exception as e:
                    out.add(f"market amounts builder failed at tick {tick}: {e}")
                    continue
                if int(mk) % 10_000 or int(tk) % 100:
                    out.add(f"market order would sign >2dp maker or >4dp taker "
                            f"at tick {tick}")
        return sorted(out)

    def _verify_client_surface(self) -> None:
        drifts = self._surface_drifts()
        if drifts:
            raise IntrospectionMismatch(
                "installed py-clob-client-v2 drifted from the verified surface: "
                + "; ".join(drifts) + ". Refusing to trade; pin the version pmq "
                "was tested with or upgrade pmq.")

    # ---------------- balance ----------------
    def collateral(self) -> float:
        """Available pUSD collateral as seen by the CLOB for this EOA and
        signature_type. Raw 6-decimal units converted to $. Any parse failure
        returns 0.0 (fail closed)."""
        try:
            bal = self.client.get_balance_allowance(self._t["BalanceAllowanceParams"](
                asset_type=self._t["AssetType"].COLLATERAL))
            return float(bal.get("balance", 0)) / 1e6 if isinstance(bal, dict) else 0.0
        except Exception as e:
            log.warning("collateral check failed: %s", e)
            return 0.0

    def require_collateral(self, min_usd: float) -> float:
        """Raise if the CLOB-visible collateral is below ``min_usd``."""
        usdc = self.collateral()
        if usdc < min_usd:
            raise RuntimeError(
                f"collateral {usdc:.2f} USDC below required {min_usd:.2f}. If the "
                f"funds ARE on your funder address on-chain, your signature_type "
                f"is wrong (deposit wallets need 3).")
        return usdc

    # ---------------- orders ----------------
    def _builder_kwargs(self) -> dict[str, str]:
        # Per-order attribution: explicit beats relying on the client-level
        # BuilderConfig injection, and works with injected test clients too.
        return {"builder_code": self.builder_code} if self.builder_code else {}

    def _parse_fill(self, resp: Any, side: str) -> Fill:
        if not (isinstance(resp, dict) and resp.get("orderID")
                and resp.get("success") is not False):
            return Fill(rejected=True, error=repr(resp)[:300],
                        raw=resp if isinstance(resp, dict) else {})
        # For a BUY the maker amount is USDC given and the taker amount shares
        # received; a SELL mirrors it. Unparseable counts as zero (fail closed).
        try:
            making = float(resp.get("makingAmount") or 0.0)
            taking = float(resp.get("takingAmount") or 0.0)
        except (TypeError, ValueError):
            making = taking = 0.0
        if not (math.isfinite(making) and math.isfinite(taking)) \
                or making < 0 or taking < 0:
            making = taking = 0.0    # NaN/inf/negative amounts book nothing
        usd, shares = (making, taking) if side == "BUY" else (taking, making)
        return Fill(order_id=str(resp["orderID"]), matched_shares=shares,
                    matched_usd=usd, raw=resp)

    def _market_order(self, token_id: str, amount: float, side: str,
                      price: float) -> Fill:
        t = self._t
        try:
            resp = self.client.create_and_post_market_order(
                t["MarketOrderArgs"](token_id=token_id, amount=amount,
                                     side=side, price=price,
                                     **self._builder_kwargs()),
                order_type=t["OrderType"].FAK)
        except self.PolyApiException as e:
            status = getattr(e, "status_code", None)
            msg = str(getattr(e, "error_msg", e))[:300]
            if status is not None and 400 <= status < 500:
                # clean rejection, the order does not exist server-side;
                # "no orders found to match" lands here and is a no-fill
                return Fill(rejected=True, error=f"{status}: {msg}")
            raise OrderUncertain(f"status={status} {msg}")
        except Exception as e:
            raise OrderUncertain(repr(e)[:300])
        return self._parse_fill(resp, side)

    def buy_fak(self, token_id: str, price_cap: float, usd: float) -> Fill:
        """Fill-and-kill market BUY: spend up to ``usd`` (rounded DOWN to the
        cent, the exchange accuracy for market-buy maker amounts) at prices no
        worse than ``price_cap``. Whatever does not match immediately is
        killed by the exchange; nothing ever rests. Returns a :class:`Fill`;
        book ONLY ``fill.matched_shares`` and ``fill.matched_usd``.
        Raises :class:`OrderUncertain` when the outcome is unknown."""
        usd = _floor_cents(usd)
        if usd <= 0:
            return Fill(rejected=True, error="usd amount rounds to zero")
        return self._market_order(token_id, usd, "BUY", price_cap)

    def sell_fak(self, token_id: str, price_floor: float, shares: float) -> Fill:
        """Fill-and-kill market SELL of ``shares`` at prices no worse than
        ``price_floor``. Same confirmation contract as :meth:`buy_fak`.
        The buy path has carried live volume; the sell path follows the same
        documented semantics; production-verified with a live round trip
        (mirrored makingAmount/takingAmount cross-checked via get_trades)."""
        shares = _floor_cents(shares)
        if shares <= 0:
            return Fill(rejected=True, error="share amount rounds to zero")
        return self._market_order(token_id, shares, "SELL", price_floor)

    def limit_gtc(self, token_id: str, price: float, size: float, side: str,
                  post_only: bool = False) -> Fill:
        """Resting GTC limit order. Returns a :class:`Fill` whose matched
        amounts reflect only what crossed IMMEDIATELY; the rest is resting
        (track it via :meth:`open_orders`, settle via :meth:`trades_totals`).
        Maker fills cost zero fee."""
        t = self._t
        try:
            resp = self.client.create_and_post_order(
                t["OrderArgs"](token_id=token_id, price=price, size=size,
                               side=side, **self._builder_kwargs()),
                order_type=t["OrderType"].GTC, post_only=post_only)
        except self.PolyApiException as e:
            status = getattr(e, "status_code", None)
            msg = str(getattr(e, "error_msg", e))[:300]
            if status is not None and 400 <= status < 500:
                return Fill(rejected=True, error=f"{status}: {msg}")
            raise OrderUncertain(f"status={status} {msg}")
        except Exception as e:
            raise OrderUncertain(repr(e)[:300])
        return self._parse_fill(resp, side)

    # ---------------- reconciliation ----------------
    def fee_rate(self, condition_id: str) -> float:
        """Authoritative taker fee rate for one market, straight from the
        exchange (``get_clob_market_info`` field ``fd.r``). Falls back to the
        published crypto rate on failure, so treat the result as an estimate
        exactly like :data:`~pmq.data.FEE_RATES`."""
        try:
            mi = self.client.get_clob_market_info(condition_id)
            return float(mi["fd"]["r"])
        except Exception as e:
            log.warning("fee_rate(%s) fell back to static table: %s", condition_id, e)
            return FEE_RATES["crypto"]

    def cancel_order(self, order_id: str) -> bool:
        """Cancel one resting order by id. Never raises."""
        try:
            self.client.cancel_orders([order_id])
            return True
        except Exception as e:
            log.warning("cancel_order(%s) failed: %s", order_id, e)
            return False

    def cancel_market(self, condition_id: str) -> bool:
        """Cancel every resting order of ours on one market. Never raises."""
        try:
            self.client.cancel_market_orders(
                self._t["OrderMarketCancelParams"](market=condition_id))
            return True
        except Exception as e:
            log.warning("cancel_market(%s) failed: %s", condition_id, e)
            return False

    def open_orders(self, condition_id: str | None = None) -> list[dict[str, Any]] | None:
        try:
            return self.client.get_open_orders(
                self._t["OpenOrderParams"](market=condition_id)) or []
        except Exception as e:
            log.warning("get_open_orders failed: %s", e)
            return None

    @staticmethod
    def _size_price(rec: dict[str, Any], size_key: str = "size",
                    ) -> tuple[float, float] | None:
        """(size, price) of one trade record or maker slice; None when
        unparseable (a malformed record books nothing, fail closed).
        Same finiteness contract as :meth:`_parse_fill`: json.loads accepts
        NaN/Infinity, and one such record must not poison the totals."""
        try:
            s, p = float(rec.get(size_key) or 0), float(rec.get("price") or 0)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(s) and math.isfinite(p)) or s < 0 or p < 0:
            return None
        return s, p

    def _maker_slice_totals(self, t: dict[str, Any]) -> tuple[float, float]:
        """(shares, usd) WE filled in one MAKER-role trade record. The
        top-level size is the counterparty's aggregate; ours is the sum of
        the ``maker_orders`` slices matched by funder address (every slice
        when no funder is configured). Records without slices fall back to
        the top-level size."""
        mos = t.get("maker_orders")
        if not isinstance(mos, list) or not mos:
            sp = self._size_price(t)
            return (sp[0], sp[1] * sp[0]) if sp is not None else (0.0, 0.0)
        me = (self.funder or "").lower()
        sh = usd = 0.0
        for mo in mos:
            if not isinstance(mo, dict):
                continue
            if me and str(mo.get("maker_address", "")).lower() != me:
                continue
            sp = self._size_price(mo, "matched_amount")
            if sp is not None:
                sh += sp[0]
                usd += sp[1] * sp[0]
        return sh, usd

    def trades_totals(self, condition_id: str, token_id: str | None = None,
                      side: str = "BUY", fee_rate: float = FEE_RATES["crypto"],
                      ) -> tuple[float, float, float] | None:
        """Exchange truth for one market: (shares, usd, fee_estimate) actually
        traded on our account, or None if the API is unreachable. FAILED
        trades are excluded; maker fills carry zero fee.

        MAKER-role records report the counterparty's AGGREGATE size at top
        level (verified on a real settlement, 2026-07-04); our actual slice
        lives in ``maker_orders``, matched by funder address. Set
        POLY_FUNDER even for sig 0 accounts if you post resting orders,
        otherwise all slices of bundled trades are attributed to you."""
        try:
            trades = self.client.get_trades(
                self._t["TradeParams"](market=condition_id, asset_id=token_id))
        except Exception as e:
            log.warning("get_trades(%s) failed: %s", condition_id, e)
            return None
        if trades is not None and not isinstance(trades, list):
            # drifted body shape: refuse to claim truth rather than guess
            log.warning("get_trades(%s) returned a non-list body", condition_id)
            return None
        sh = usd = fees = 0.0
        for t in trades or []:
            if not isinstance(t, dict) or t.get("side") != side \
                    or t.get("status") == "FAILED":
                continue
            if t.get("trader_side") == "MAKER":
                dsh, dusd = self._maker_slice_totals(t)
                sh += dsh
                usd += dusd
                continue                       # maker fills pay zero fee
            sp = self._size_price(t)
            if sp is None:
                continue
            s, p = sp
            sh += s
            usd += p * s
            fees += fee(p, s, fee_rate)
        return sh, usd, fees

    def reconcile(self, condition_id: str, token_id: str | None = None,
                  ) -> tuple[float, float, float] | None:
        """After :class:`OrderUncertain`: cancel anything possibly resting,
        verify nothing stayed open, then return exchange-truth totals. Call
        this BEFORE placing any new order on that market."""
        self.cancel_market(condition_id)
        still = self.open_orders(condition_id)
        if still:
            log.warning("reconcile(%s): %d orders still open after cancel, retrying",
                        condition_id, len(still))
            self.cancel_market(condition_id)
        return self.trades_totals(condition_id, token_id)
