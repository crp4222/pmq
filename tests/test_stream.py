"""pmq.stream: decodeur de frames et ingestion, sans reseau."""
import json
import struct
import time

from pmq.stream import PriceStream, _FrameBuf, _mask_frame


def _server_frame(op, payload, fin=True):
    b0 = (0x80 if fin else 0) | op
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, n)
    elif n < 65536:
        head = struct.pack("!BBH", b0, 126, n)
    else:
        head = struct.pack("!BBQ", b0, 127, n)
    return head + payload


def _tick_msg(sym, ts_ms, val, topic="crypto_prices_chainlink"):
    return json.dumps({"topic": topic, "payload": {
        "data": [{"symbol": sym, "timestamp": ts_ms, "value": val}]}}).encode()


def test_framebuf_single_and_split():
    fb = _FrameBuf()
    raw = _server_frame(1, b"hello")
    assert fb.feed(raw) == [(1, b"hello")]
    out = fb.feed(raw[:3])
    assert out == []
    assert fb.feed(raw[3:]) == [(1, b"hello")]


def test_framebuf_extended_length_and_fragmented():
    fb = _FrameBuf()
    big = b"x" * 300
    assert fb.feed(_server_frame(1, big)) == [(1, big)]
    frames = _server_frame(1, b"ab", fin=False) + _server_frame(0, b"cd")
    assert fb.feed(frames) == [(1, b"abcd")]
    assert fb.feed(_server_frame(9, b"ping")) == [(9, b"ping")]


def test_mask_frame_is_unmaskable():
    raw = _mask_frame(1, b"payload")
    assert raw[1] & 0x80
    mask = raw[2:6]
    body = bytes(c ^ mask[i % 4] for i, c in enumerate(raw[6:]))
    assert body == b"payload"


def test_ingest_dedup_ring_and_sources():
    seen = []
    ps = PriceStream(assets=("btc", "eth"), keep=3,
                     on_tick=lambda src, a, ts, v: seen.append((src, a, v)))
    now_ms = int(time.time() * 1000)
    assert ps._ingest(_tick_msg("btc/usd", now_ms, 100.0)) is True
    assert ps._ingest(_tick_msg("btc/usd", now_ms, 101.0)) is False
    assert ps._ingest(_tick_msg("BTCUSDT", now_ms, 102.0,
                                topic="crypto_prices")) is False
    for i in range(1, 5):
        ps._ingest(_tick_msg("btc/usd", now_ms + i, 100.0 + i))
    assert len(ps.recent("btc")) == 3
    assert ps.last("btc", "binance")[1] == 102.0
    assert ps.last("eth") is None
    assert ps.age("btc") < 5.0
    assert ("chainlink", "btc", 100.0) in seen
    assert ("binance", "btc", 102.0) in seen


def test_ingest_rejects_garbage_and_unknown():
    ps = PriceStream(assets=("btc",))
    assert ps._ingest(b"not json") is False
    assert ps._ingest(_tick_msg("sol/usd", 1, 1.0)) is False
    assert ps._ingest(json.dumps({"topic": "other"}).encode()) is False
    bad = json.dumps({"topic": "crypto_prices_chainlink",
                      "payload": {"data": [{"symbol": "btc/usd",
                                            "timestamp": "x", "value": "y"}]}})
    assert ps._ingest(bad.encode()) is False
    assert ps.recent("btc") == []


def test_callback_errors_are_counted_not_raised():
    def boom(*a):
        raise RuntimeError("cb")
    ps = PriceStream(assets=("btc",), on_tick=boom)
    ps._ingest(_tick_msg("btc/usd", int(time.time() * 1000), 1.0))
    assert ps.health()["cb_err"] == 1
