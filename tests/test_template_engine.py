"""Tests for the bot-template engine (loaded from bot-template/bot.py)."""
import csv
import importlib.util
import os
import sys

import pytest

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "bot-template", "bot.py")


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_MODE", "paper")
    for mod in ("bot", "strategy"):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location("bot", os.path.abspath(TEMPLATE))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["bot"] = bot
    spec.loader.exec_module(bot)
    return bot


def _write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_recover_orphans_rebuilds_unscored_markets(engine, monkeypatch):
    _write_csv(engine.FILLS_CSV, engine.FILLS_HEADER, [
        {"ts": 1, "family": "some-market", "window": 100, "mode": "paper",
         "side": "Yes", "price": 0.5, "shares": 10, "notional": 5,
         "ask_size_at_fill": "", "band_depth_usd": "", "secs_left": "",
         "order_id": ""},
        {"ts": 2, "family": "scored-market", "window": 100, "mode": "paper",
         "side": "Yes", "price": 0.5, "shares": 10, "notional": 5,
         "ask_size_at_fill": "", "band_depth_usd": "", "secs_left": "",
         "order_id": ""},
    ])
    _write_csv(engine.WINDOWS_CSV, engine.WINDOWS_HEADER, [
        {"family": "scored-market", "window": 100, "mode": "paper",
         "side": "Yes", "n_fills": 1, "avg_price": 0.5, "shares": 10,
         "spent": 5, "winner": "Yes", "winner_source": "gamma",
         "gross_pnl": 5, "fee": 0, "net_pnl": 5, "scored_ts": 3}])
    fake_pm = {"condition_id": "0xc", "slug": "some-market", "token_a": "1",
               "token_b": "2", "outcome_a": "Yes", "outcome_b": "No",
               "outcome_prices_raw": None, "idx_a": 0, "end_ts": None}
    monkeypatch.setattr(engine.pmq, "get_market", lambda s, logger=None: {"slug": s})
    monkeypatch.setattr(engine.pmq, "parse_market",
                        lambda m, *a, **k: dict(fake_pm) if m else None)
    tracked = {}
    engine.recover_orphans(tracked)
    assert "some-market" in tracked and "scored-market" not in tracked
    st = tracked["some-market"]
    assert st["side"] == "Yes" and st["spent"] == 5.0 and not st["resolved"]


def test_recover_orphans_survives_missing_files(engine):
    tracked = {}
    engine.recover_orphans(tracked)
    assert tracked == {}


def test_halt_flag_roundtrip(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "STATE_DIR", str(tmp_path / "state"))
    day = "2026-07-03"
    engine.write_halt_flag(day, -25.5)
    assert os.path.exists(engine.halt_flag_path(day))
    content = open(engine.halt_flag_path(day)).read()
    assert "day_pnl=-25.50" in content


def test_append_csv_writes_header_once(engine, tmp_path):
    p = str(tmp_path / "x.csv")
    engine.append_csv(p, {"a": 1, "b": 2}, ["a", "b"])
    engine.append_csv(p, {"a": 3, "b": 4}, ["a", "b"])
    rows = list(csv.DictReader(open(p)))
    assert len(rows) == 2 and rows[1]["a"] == "3"
