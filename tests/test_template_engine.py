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


# ---------------- main-loop tests (fake clock, stubbed strategy and pmq) ----
import time as real_time  # noqa: E402
import types  # noqa: E402

from pmq import OrderUncertain  # noqa: E402
from pmq.executor import Fill  # noqa: E402

T0 = 1_780_000_000.0
RUN_H = 6 / 3600.0            # 6 fake seconds, about 12 iterations


class Clock:
    """time-module stand-in: sleep() advances a virtual clock, so main()
    terminates deterministically without waiting."""

    def __init__(self, t0=T0):
        self.t = t0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.5)

    def gmtime(self, x=None):
        return real_time.gmtime(self.t if x is None else x)

    def strftime(self, fmt, tt=None):
        return real_time.strftime(fmt, self.gmtime() if tt is None else tt)


@pytest.fixture()
def load_engine(tmp_path, monkeypatch):
    """Factory: fresh engine module with chosen env, plus the stub wiring."""

    def _load(mode="paper", live_env=None):
        monkeypatch.setenv("BOT_OUT_DIR", str(tmp_path))
        monkeypatch.setenv("BOT_MODE", mode)
        monkeypatch.delenv("BOT_SLUGS", raising=False)
        if live_env is None:
            monkeypatch.delenv("LIVE", raising=False)
        else:
            monkeypatch.setenv("LIVE", live_env)
        for mod in ("bot", "strategy"):
            sys.modules.pop(mod, None)
        spec = importlib.util.spec_from_file_location("bot", os.path.abspath(TEMPLATE))
        bot = importlib.util.module_from_spec(spec)
        sys.modules["bot"] = bot
        spec.loader.exec_module(bot)
        return bot

    return _load


def wire(engine, monkeypatch, *, watchlist, decide, winner=lambda: None,
         books=None, executor=None, end_in=9999.0):
    """Deterministic world: every pmq call the loop makes is stubbed."""
    clock = Clock()
    monkeypatch.setattr(engine, "time", clock)
    monkeypatch.setattr(engine, "SCORE_POLL_S", 0.0)
    monkeypatch.setattr(engine, "POLL_S", 0.0)

    def pm_for(slug):
        return {"condition_id": "0xc-" + slug, "slug": slug,
                "token_a": slug + "-A", "token_b": slug + "-B",
                "outcome_a": "Yes", "outcome_b": "No",
                "outcome_prices_raw": None, "idx_a": 0,
                "end_ts": T0 + end_in}

    default_book = {"bid": 0.93, "bidsz": 50.0, "ask": 0.97, "asksz": 100.0,
                    "min": 5.0}
    monkeypatch.setattr(engine.pmq, "get_market", lambda s, logger=None: {"slug": s})
    monkeypatch.setattr(engine.pmq, "parse_market",
                        lambda m, *a, **k: pm_for(m["slug"]) if m else None)
    monkeypatch.setattr(engine.pmq, "get_book",
                        lambda tok, logger=None: dict((books or {}).get(tok, default_book)))
    monkeypatch.setattr(engine.pmq, "best_bid_ask",
                        lambda b: (b["bid"], b["bidsz"], b["ask"], b["asksz"]))
    monkeypatch.setattr(engine.pmq, "book_meta",
                        lambda b: {"min_order_size": b["min"], "tick_size": 0.01})
    monkeypatch.setattr(engine.pmq, "resolved_winner", lambda pm: winner())
    if executor is not None:
        monkeypatch.setattr(engine.pmq, "PolymarketExecutor",
                            lambda *a, **k: executor)
    monkeypatch.setattr(
        engine, "strategy",
        types.SimpleNamespace(NAME="test", watchlist=watchlist, decide=decide))
    return clock


def read_rows(path):
    return list(csv.DictReader(open(path))) if os.path.exists(path) else []


class FakeLiveEx:
    def __init__(self, fill=None, exc=None, collateral_usd=100.0,
                 totals=(5.15, 5.0, 0.01), reconcile_totals=(0.0, 0.0, 0.0)):
        self.fill, self.exc = fill, exc
        self.collateral_usd, self.totals = collateral_usd, totals
        self.reconcile_totals = reconcile_totals
        self.buy_calls = 0

    def require_collateral(self, need):
        if self.collateral_usd < need:
            raise RuntimeError(f"collateral {self.collateral_usd:.2f} below {need}")
        return self.collateral_usd

    def buy_fak(self, token, cap, usd):
        self.buy_calls += 1
        if self.exc:
            raise self.exc
        return self.fill

    def reconcile(self, condition_id, token_id=None):
        return self.reconcile_totals

    def trades_totals(self, condition_id, token_id=None, fee_rate=None):
        return self.totals

    def collateral(self):
        return self.collateral_usd


def one_shot_decide(cap=0.98, usd=5.0):
    def decide(pm, book_a, book_b, remaining_usd, side_held, state):
        return None if side_held else ("a", cap, usd)
    return decide


def test_paper_flow_fills_at_real_ask_and_scores(load_engine, monkeypatch):
    engine = load_engine()
    won = {"w": None}

    def decide(pm, book_a, book_b, remaining_usd, side_held, state):
        if side_held:                     # entered: let the market resolve
            won["w"] = "Yes"
            return None
        return ("a", 0.98, 5.0)           # cap ABOVE the 0.97 ask on purpose

    wire(engine, monkeypatch, watchlist=lambda: ["mkt-a"], decide=decide,
         winner=lambda: won["w"])
    engine.main(RUN_H)
    fills = read_rows(engine.FILLS_CSV)
    assert len(fills) == 1
    assert float(fills[0]["price"]) == 0.97          # real ask, not the cap
    assert fills[0]["mode"] == "paper" and fills[0]["side"] == "Yes"
    wins = read_rows(engine.WINDOWS_CSV)
    assert len(wins) == 1 and wins[0]["winner"] == "Yes"
    shares = float(fills[0]["shares"])
    expected_net = shares * (1 - 0.97) - engine.pmq.fee(0.97, shares, engine.FEE_RATE)
    assert abs(float(wins[0]["net_pnl"]) - round(expected_net, 2)) < 0.011


def test_no_fill_when_ask_above_cap_or_below_min_size(load_engine, monkeypatch):
    engine = load_engine()
    thin = {"bid": 0.93, "bidsz": 50.0, "ask": 0.97, "asksz": 2.0, "min": 5.0}
    wire(engine, monkeypatch, watchlist=lambda: ["capped", "thin"],
         decide=lambda pm, book_a, book_b, remaining_usd, side_held, state:
             None if side_held else ("a", 0.90 if pm["slug"] == "capped" else 0.98, 5.0),
         books={"thin-A": thin})
    engine.main(RUN_H)
    assert read_rows(engine.FILLS_CSV) == []   # 0.90 cap under ask; 2sh depth under 5sh min


def test_one_side_per_market_is_enforced(load_engine, monkeypatch):
    engine = load_engine()

    def decide(pm, book_a, book_b, remaining_usd, side_held, state):
        return ("b", 0.98, 5.0) if side_held else ("a", 0.98, 5.0)

    wire(engine, monkeypatch, watchlist=lambda: ["mkt-a"], decide=decide)
    engine.main(RUN_H)
    fills = read_rows(engine.FILLS_CSV)
    assert len(fills) == 1 and fills[0]["side"] == "Yes"


def test_budget_cap_with_fee_headroom_never_exceeded(load_engine, monkeypatch):
    engine = load_engine()
    wire(engine, monkeypatch, watchlist=lambda: ["mkt-a"],
         decide=lambda pm, book_a, book_b, remaining_usd, side_held, state:
             ("a", 0.98, 99.0))            # asks for far more than the budget
    engine.main(RUN_H)
    fills = read_rows(engine.FILLS_CSV)
    assert len(fills) == 1                 # headroom leaves no room for a 2nd
    notional = float(fills[0]["notional"])
    fee = engine.pmq.fee(0.97, float(fills[0]["shares"]), engine.FEE_RATE)
    assert notional <= engine.STAKE_USD and notional + fee <= engine.STAKE_USD + 0.005


def test_daily_halt_paper_blocks_entries_without_exit(load_engine, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "DAILY_HALT_USD", -2.0)
    n = {"i": 0}

    def watchlist():
        n["i"] += 1
        return ["mkt-a", "mkt-b", "mkt-c"] if n["i"] >= 6 else ["mkt-a", "mkt-b"]

    wire(engine, monkeypatch, watchlist=watchlist, decide=one_shot_decide(),
         winner=lambda: "No" if n["i"] >= 2 else None)   # both entries LOSE
    engine.main(RUN_H)                    # must return, not SystemExit
    fills = read_rows(engine.FILLS_CSV)
    assert sorted(f["family"] for f in fills) == ["mkt-a", "mkt-b"]
    assert all(f["family"] != "mkt-c" for f in fills)    # halted before c
    assert len(read_rows(engine.WINDOWS_CSV)) == 2


def test_daily_halt_live_writes_flag_and_exits_42(load_engine, monkeypatch, tmp_path):
    engine = load_engine(mode="live", live_env="1")
    monkeypatch.setattr(engine, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(engine, "DAILY_HALT_USD", -2.0)
    ex = FakeLiveEx(fill=Fill(order_id="0x1", matched_shares=5.15, matched_usd=5.0),
                    totals=(5.15, 5.0, 0.01))
    n = {"i": 0}

    def watchlist():
        n["i"] += 1
        return ["mkt-a"]

    wire(engine, monkeypatch, watchlist=watchlist, decide=one_shot_decide(),
         winner=lambda: "No" if n["i"] >= 2 else None, executor=ex)
    with pytest.raises(SystemExit) as ei:
        engine.main(RUN_H)
    assert ei.value.code == engine.HALT_EXIT_CODE
    day = real_time.strftime("%Y-%m-%d", real_time.gmtime(T0))
    assert os.path.exists(engine.halt_flag_path(day))
    wins = read_rows(engine.WINDOWS_CSV)   # scored from exchange truth
    assert len(wins) == 1 and float(wins[0]["spent"]) == 5.0
    assert abs(float(wins[0]["net_pnl"]) - (-5.01)) < 1e-6


def test_live_requires_explicit_live_env(load_engine, monkeypatch):
    engine = load_engine(mode="live", live_env=None)
    called = []
    monkeypatch.setattr(engine.pmq, "PolymarketExecutor",
                        lambda *a, **k: called.append(1))
    with pytest.raises(SystemExit) as ei:
        engine.main(RUN_H)
    assert ei.value.code == engine.HALT_EXIT_CODE and not called


def test_live_halt_flag_present_blocks_start(load_engine, monkeypatch, tmp_path):
    engine = load_engine(mode="live", live_env="1")
    monkeypatch.setattr(engine, "STATE_DIR", str(tmp_path / "state"))
    clock = wire(engine, monkeypatch, watchlist=lambda: [],
                 decide=lambda *a, **k: None,
                 executor=FakeLiveEx())
    day = real_time.strftime("%Y-%m-%d", real_time.gmtime(clock.t))
    engine.write_halt_flag(day, -25.0)
    with pytest.raises(SystemExit) as ei:
        engine.main(RUN_H)
    assert ei.value.code == engine.HALT_EXIT_CODE


def test_live_collateral_failure_halts(load_engine, monkeypatch):
    engine = load_engine(mode="live", live_env="1")
    wire(engine, monkeypatch, watchlist=lambda: [], decide=lambda *a, **k: None,
         executor=FakeLiveEx(collateral_usd=0.5))
    with pytest.raises(SystemExit) as ei:
        engine.main(RUN_H)
    assert ei.value.code == engine.HALT_EXIT_CODE


def test_live_consecutive_rejections_halt(load_engine, monkeypatch, tmp_path):
    engine = load_engine(mode="live", live_env="1")
    monkeypatch.setattr(engine, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(engine, "MAX_CONSEC_FAILS", 3)
    ex = FakeLiveEx(fill=Fill(rejected=True, error="no match"))
    wire(engine, monkeypatch, watchlist=lambda: ["mkt-a"],
         decide=lambda pm, book_a, book_b, remaining_usd, side_held, state:
             ("a", 0.98, 5.0), executor=ex)
    with pytest.raises(SystemExit) as ei:
        engine.main(RUN_H)
    assert ei.value.code == engine.HALT_EXIT_CODE and ex.buy_calls == 3
    assert read_rows(engine.FILLS_CSV) == []


def test_order_uncertain_poisons_market_and_adopts_reconcile(load_engine, monkeypatch, tmp_path):
    engine = load_engine(mode="live", live_env="1")
    monkeypatch.setattr(engine, "STATE_DIR", str(tmp_path / "state"))
    ex = FakeLiveEx(exc=OrderUncertain("502"), reconcile_totals=(5.0, 4.8, 0.01),
                    totals=(5.0, 4.8, 0.01))
    wire(engine, monkeypatch, watchlist=lambda: ["mkt-a"],
         decide=lambda pm, book_a, book_b, remaining_usd, side_held, state:
             ("a", 0.98, 5.0),             # keeps asking; poison must block it
         winner=lambda: "Yes" if ex.buy_calls else None, executor=ex)
    engine.main(RUN_H)
    assert ex.buy_calls == 1               # poisoned after the first uncertain
    wins = read_rows(engine.WINDOWS_CSV)
    assert len(wins) == 1 and float(wins[0]["spent"]) == 4.8
    assert wins[0]["side"] == "Yes"


def test_loop_survives_data_layer_exceptions(load_engine, monkeypatch):
    engine = load_engine()
    wire(engine, monkeypatch, watchlist=lambda: ["mkt-a"],
         decide=lambda *a, **k: ("a", 0.98, 5.0))

    def boom(tok, logger=None):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(engine.pmq, "get_book", boom)
    engine.main(RUN_H)                     # must complete, not raise
    assert read_rows(engine.FILLS_CSV) == []


def test_unresolved_market_dropped_after_max_track_hours(load_engine, monkeypatch):
    engine = load_engine()
    monkeypatch.setattr(engine, "MAX_TRACK_H", 0.0005)   # 1.8 fake seconds
    wire(engine, monkeypatch, watchlist=lambda: ["mkt-a"],
         decide=one_shot_decide(), winner=lambda: None)
    engine.main(RUN_H)
    assert len(read_rows(engine.FILLS_CSV)) == 1
    assert read_rows(engine.WINDOWS_CSV) == []           # dropped, never scored
