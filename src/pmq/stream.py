"""Short-poll client for Polymarket's resolution price feed.

``wss://ws-live-data.polymarket.com`` republishes the exact price streams
that resolve the updown markets: the Chainlink stream (the referee) and a
Binance spot mirror. Measured behavior (2026-07, identical from two
unrelated egresses): the edge serves the SUSTAINED push only to
browser-fingerprinted connections; a plain client receives the initial
~60-tick batch after subscribing, then silence, regardless of keepalive
pings. The reliable pattern is therefore to RE-POLL short connections and
drain the batch each time: one poll takes about a second and the freshest
tick in a batch is 1.2 to 2.8 seconds old (p50 1.8s measured over 150
back-to-back polls, zero dead).

:class:`PriceStream` implements that pattern with the standard library
only. Strategies should treat the feed as advisory and fail closed on
:meth:`PriceStream.age` (a 3 second guard works well in practice).

Both sources arrive under the same topic; the discriminant is the symbol
FORMAT: ``btc/usd`` (slash) is Chainlink, ``BTCUSDT`` is Binance.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from typing import Any, Callable

log = logging.getLogger("pmq.stream")

WS_HOST = "ws-live-data.polymarket.com"
WS_PORT = 443
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
_SOURCES = {"chainlink": "cl", "binance": "bn"}

OnTick = Callable[[str, str, float, float], None]


def _mask_frame(op: int, payload: bytes) -> bytes:
    """One masked client frame (RFC 6455 requires masking client->server)."""
    b0 = 0x80 | op
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, 0x80 | n)
    elif n < 65536:
        head = struct.pack("!BBH", b0, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", b0, 0x80 | 127, n)
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


class _FrameBuf:
    """Incremental server-frame decoder; pure so it is testable offline.
    ``feed`` returns complete (opcode, payload) messages, reassembling
    fragmented text frames; control frames pass through unmerged."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._frag = bytearray()
        self._frag_op: int | None = None

    def feed(self, chunk: bytes) -> list[tuple[int, bytes]]:
        self._buf.extend(chunk)
        out: list[tuple[int, bytes]] = []
        while len(self._buf) >= 2:
            b0, b1 = self._buf[0], self._buf[1]
            ln = b1 & 0x7F
            off = 2
            if ln == 126:
                if len(self._buf) < 4:
                    break
                ln = struct.unpack("!H", self._buf[2:4])[0]
                off = 4
            elif ln == 127:
                if len(self._buf) < 10:
                    break
                ln = struct.unpack("!Q", self._buf[2:10])[0]
                off = 10
            if b1 & 0x80:                # masked server frame (not expected)
                off += 4
            if len(self._buf) < off + ln:
                break
            payload = bytes(self._buf[off:off + ln])
            del self._buf[:off + ln]
            fin, op = b0 >> 7, b0 & 0x0F
            if op == 0:                  # continuation
                self._frag.extend(payload)
                if fin and self._frag_op is not None:
                    out.append((self._frag_op, bytes(self._frag)))
                    self._frag = bytearray()
                    self._frag_op = None
            elif op == 1 and not fin:    # fragmented text starts
                self._frag_op = 1
                self._frag = bytearray(payload)
            else:
                out.append((op, payload))
        return out


class PriceStream:
    """Poll the resolution price feed; keep the last ticks per symbol.

    ``assets`` are lowercase tickers (``btc``, ``eth``, ...); each is
    subscribed on both sources. ``on_tick(source, asset, ts, value)`` is
    called for every NEW tick (timestamps in seconds; duplicates are
    dropped); exceptions in the callback are counted, never raised.
    """

    def __init__(self, assets: tuple[str, ...] = ("btc",),
                 poll_interval: float = 1.0, keep: int = 8,
                 on_tick: OnTick | None = None) -> None:
        self.assets = tuple(a.lower() for a in assets)
        self.poll_interval = poll_interval
        self.keep = keep
        self.on_tick = on_tick
        subs = [{"topic": "crypto_prices_chainlink", "type": "update",
                 "filters": json.dumps({"symbol": f"{a}/usd"})}
                for a in self.assets]
        subs += [{"topic": "crypto_prices", "type": "update",
                  "filters": json.dumps({"symbol": f"{a.upper()}USDT"})}
                 for a in self.assets]
        self._sub_msg = json.dumps({"action": "subscribe",
                                    "subscriptions": subs})
        self._lock = threading.Lock()
        self._recent: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self._last_ms: dict[tuple[str, str], int] = {}
        self._health = {"ok": 0, "empty": 0, "err": 0, "cb_err": 0,
                        "last_ok": 0.0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- ingest
    def _ingest(self, payload: bytes) -> bool:
        """Parse one text message; returns True when a chainlink tick for
        the FIRST asset landed (the poll's batch-complete signal)."""
        try:
            m = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(m, dict) or m.get("topic") not in (
                "crypto_prices_chainlink", "crypto_prices"):
            return False
        pl = m.get("payload") or {}
        if not isinstance(pl, dict):
            return False
        data = pl.get("data")
        rows = data if isinstance(data, list) else [pl]
        saw_first_cl = False
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                ts_ms = int(r["timestamp"])
                val = float(r["value"])
            except (KeyError, TypeError, ValueError):
                continue
            sym_raw = str(r.get("symbol") or pl.get("symbol") or "").lower()
            src = "cl" if "/" in sym_raw else "bn"
            sym = sym_raw.split("/")[0].replace("usdt", "")
            if sym not in self.assets:
                continue
            key = (src, sym)
            with self._lock:
                if ts_ms <= self._last_ms.get(key, 0):
                    continue
                self._last_ms[key] = ts_ms
                dq = self._recent.setdefault(key, [])
                dq.append((ts_ms / 1000.0, val))
                del dq[:-self.keep]
            if src == "cl" and sym == self.assets[0]:
                saw_first_cl = True
            if self.on_tick is not None:
                try:
                    self.on_tick("chainlink" if src == "cl" else "binance",
                                 sym, ts_ms / 1000.0, val)
                except Exception:
                    self._health["cb_err"] += 1
        return saw_first_cl

    # -------------------------------------------------------------- socket
    def poll_once(self, read_budget: float = 1.5) -> int:
        """One short connection: connect, subscribe, drain the batch.
        Returns the number of ws messages ingested; raises OSError on
        connect or handshake failure so callers can back off."""
        sock: socket.socket | None = None
        for af, st, pr, _, sa in socket.getaddrinfo(
                WS_HOST, WS_PORT, 0, socket.SOCK_STREAM):
            sock = socket.socket(af, st, pr)
            sock.settimeout(10)
            try:
                sock.connect(sa)
                break
            except OSError:
                sock.close()
                sock = None
        if sock is None:
            raise OSError("connect failed")
        try:
            ctx = ssl.create_default_context()
            # the edge disables ws-over-h2: pin http/1.1 so ALPN never
            # selects it
            ctx.set_alpn_protocols(["http/1.1"])
            tls = ctx.wrap_socket(sock, server_hostname=WS_HOST)
            wskey = base64.b64encode(os.urandom(16)).decode()
            req = ("GET / HTTP/1.1\r\n"
                   f"Host: {WS_HOST}\r\n"
                   "Connection: Upgrade\r\n"
                   "Upgrade: websocket\r\n"
                   f"User-Agent: {_UA}\r\n"
                   "Origin: https://polymarket.com\r\n"
                   "Sec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {wskey}\r\n\r\n")
            tls.sendall(req.encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                c = tls.recv(4096)
                if not c:
                    raise OSError("closed during handshake")
                buf += c
                if len(buf) > 65536:
                    raise OSError("oversized handshake")
            head, rest = buf.split(b"\r\n\r\n", 1)
            if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
                raise OSError("no 101 upgrade")
            tls.sendall(_mask_frame(1, self._sub_msg.encode()))
            tls.settimeout(0.5)
            frames = _FrameBuf()
            pending = frames.feed(rest)
            n = 0
            got = False
            deadline = time.time() + read_budget
            while time.time() < deadline:
                for op, payload in pending:
                    if op == 8:                       # close
                        return n
                    if op == 9:                       # ping -> pong
                        try:
                            tls.sendall(_mask_frame(10, payload))
                        except OSError:
                            pass
                        continue
                    if op == 1:
                        got = self._ingest(payload) or got
                        n += 1
                pending = []
                # the batch lands in one burst; once the first asset's
                # chainlink row is in, the rings are fresh: close early
                if got and n >= 3:
                    break
                try:
                    chunk = tls.recv(65536)
                except (TimeoutError, ssl.SSLWantReadError):
                    if got:
                        break
                    continue
                if not chunk:
                    break
                pending = frames.feed(chunk)
            return n
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # -------------------------------------------------------------- loop
    def _loop(self) -> None:
        fails = 0
        while not self._stop.is_set():
            start = time.time()
            ok = False
            try:
                if self.poll_once() > 0:
                    self._health["ok"] += 1
                    self._health["last_ok"] = time.time()
                    ok = True
                else:
                    self._health["empty"] += 1
            except Exception as e:
                self._health["err"] += 1
                log.debug("poll failed: %s", e)
            if ok:
                fails = 0
                backoff = self.poll_interval
            else:
                # isolated blips retry immediately; the backoff (cap 60s)
                # engages only when failures persist, and resets on success
                fails += 1
                backoff = (self.poll_interval if fails <= 3
                           else min(2.0 * 1.6 ** (fails - 3), 60.0))
            self._stop.wait(max(0.0, backoff - (time.time() - start)))

    def start(self) -> "PriceStream":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="pmq-price-stream")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- reads
    def _key(self, asset: str, source: str) -> tuple[str, str]:
        try:
            return (_SOURCES[source], asset.lower())
        except KeyError:
            raise ValueError(f"source must be one of {sorted(_SOURCES)}")

    def recent(self, asset: str, source: str = "chainlink",
               ) -> list[tuple[float, float]]:
        """Last ticks, oldest first, as (unix_seconds, value)."""
        with self._lock:
            return list(self._recent.get(self._key(asset, source), ()))

    def last(self, asset: str, source: str = "chainlink",
             ) -> tuple[float, float] | None:
        r = self.recent(asset, source)
        return r[-1] if r else None

    def age(self, asset: str, source: str = "chainlink") -> float | None:
        """Seconds since the freshest tick, or None if none yet. FAIL
        CLOSED on None or large ages: the feed is advisory, the exchange
        resolves with its own copy."""
        t = self.last(asset, source)
        return None if t is None else max(0.0, time.time() - t[0])

    def health(self) -> dict[str, Any]:
        return dict(self._health)
