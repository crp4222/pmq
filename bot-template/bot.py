#!/usr/bin/env python3
"""pmq bot template: all the plumbing, none of the strategy, ANY market.

The engine knows nothing about market families, categories or timing. Your
``strategy.py`` provides two functions:

* ``watchlist()``: the gamma slugs to track right now (politics, sports,
  crypto, anything). Static list, or generated dynamically for recurring
  markets.
* ``decide(...)``: called on every poll for every tracked market, returns
  None or an intent ``(side_key, price_cap, usd)``.

The engine supplies the rails around your intent:

* paper mode by default: fills simulated at the real ask, scored with the
  real fee at resolution; live requires BOT_MODE=live AND LIVE=1
* per-market budget with taker-fee headroom, never exceeded, one side only
* fail-closed execution via pmq: nothing booked without exchange
  confirmation; an unknown outcome poisons the market (cancel, reconcile
  from get_trades, no further entries there)
* consecutive failed buys halt the process with exit code 42; pair with
  systemd RestartPreventExitStatus=42 so voluntary halts STAY halted
* daily loss halt, persisted to disk in live mode
* collateral checked before the first live order

Outputs fills.csv and windows.csv under BOT_OUT_DIR, read as is by the
bundled dashboard.
"""
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy  # noqa: E402  (your code)

import pmq  # noqa: E402
from pmq import OrderUncertain  # noqa: E402

MODE = os.environ.get("BOT_MODE", "paper")                  # paper | live
STAKE_USD = float(os.environ.get("BOT_STAKE", "5"))          # per market, fees incl.
POLL_S = float(os.environ.get("BOT_POLL", "2.5"))
SCORE_POLL_S = float(os.environ.get("BOT_SCORE_POLL", "60"))
DAILY_HALT_USD = float(os.environ.get("BOT_DAILY_HALT", "-25"))
FEE_RATE = pmq.FEE_RATES.get(os.environ.get("BOT_FEE_CATEGORY", "crypto"), 0.07)
MAX_CONSEC_FAILS = int(os.environ.get("BOT_MAX_CONSEC_FAILS", "10"))
MAX_TRACK_H = float(os.environ.get("BOT_MAX_TRACK_HOURS", "48"))
HALT_EXIT_CODE = 42

OUT_DIR = os.path.abspath(os.environ.get("BOT_OUT_DIR", "./bot_runs"))
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
os.makedirs(OUT_DIR, exist_ok=True)
FILLS_CSV = os.path.join(OUT_DIR, "fills.csv")
WINDOWS_CSV = os.path.join(OUT_DIR, "windows.csv")
FILLS_HEADER = ["ts", "family", "window", "mode", "side", "price", "shares", "notional",
                "ask_size_at_fill", "band_depth_usd", "secs_left", "order_id"]
WINDOWS_HEADER = ["family", "window", "mode", "side", "n_fills", "avg_price", "shares",
                  "spent", "winner", "winner_source", "gross_pnl", "fee",
                  "net_pnl", "scored_ts"]


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def append_csv(path, row, header):
    exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not exists:
            w.writeheader()
        w.writerow(row)


def halt_flag_path(utc_day):
    return os.path.join(STATE_DIR, f"halt-{utc_day}.flag")


def write_halt_flag(utc_day, pnl):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(halt_flag_path(utc_day), "w") as f:
            f.write(f"ts={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"day_pnl={pnl:+.2f}\nthreshold={DAILY_HALT_USD}\n")
    except OSError as e:
        log(f"halt flag write failed ({e}); halt still enforced in-process")


def recover_orphans(tracked):
    """Markets that got a fill but no scoring row (a restart between fill and
    resolution wipes in-memory state) are rebuilt so the scoring loop settles
    them. Live scoring re-pulls exchange truth, so numbers cannot drift."""
    try:
        scored = set()
        if os.path.exists(WINDOWS_CSV):
            for r in csv.DictReader(open(WINDOWS_CSV)):
                scored.add((r["family"], r["mode"]))
        if not os.path.exists(FILLS_CSV):
            return
        now, orphans = time.time(), {}
        for r in csv.DictReader(open(FILLS_CSV)):
            if r["mode"] != MODE or (r["family"], MODE) in scored:
                continue
            o = orphans.setdefault(r["family"], {"side": r["side"], "fills": []})
            o["fills"].append((float(r["price"]), float(r["shares"])))
        for slug, o in orphans.items():
            pm = pmq.parse_market(pmq.get_market(slug, log))
            if not pm:
                log(f"orphan {slug}: market unresolvable, skipped")
                continue
            spent = sum(p * s for p, s in o["fills"])
            fees = sum(pmq.fee(p, s, FEE_RATE) for p, s in o["fills"])
            tracked[slug] = {"pm": pm, "first_seen": now - MAX_TRACK_H * 1800,
                             "fills": o["fills"], "spent": spent, "fees": fees,
                             "side": o["side"], "winner": None, "src": "",
                             "resolved": False, "poisoned": False, "sstate": {}}
            log(f"orphan recovered: {slug} ({len(o['fills'])} fill(s)), will be scored")
    except Exception as e:
        log(f"orphan recovery failed (continuing without): {e}")


def live_startup():
    """The three gates between paper and real money: explicit LIVE=1, no halt
    flag for today, collateral actually visible. Returns the executor."""
    if os.environ.get("LIVE") != "1":
        log("HALT: live mode requires explicit LIVE=1")
        raise SystemExit(HALT_EXIT_CODE)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if os.path.exists(halt_flag_path(today)):
        log(f"HALT: daily halt flag present for {today}; no trading until next UTC day")
        raise SystemExit(HALT_EXIT_CODE)
    ex = pmq.PolymarketExecutor()
    try:
        usdc = ex.require_collateral(STAKE_USD)
    except RuntimeError as e:
        log(f"HALT: {e}")
        raise SystemExit(HALT_EXIT_CODE)
    log(f"live executor ready, collateral {usdc:.2f} USDC")
    return ex


def apply_daily_halt(day_pnl, utc_day, halted_day, ex):
    """Paper: mark the day halted (entries stop). Live: persist the flag and
    exit 42 so systemd keeps the process down for the rest of the UTC day."""
    if utc_day == halted_day or day_pnl.get(utc_day, 0.0) > DAILY_HALT_USD:
        return halted_day
    log(f"DAILY HALT: {day_pnl[utc_day]:+.2f}$ <= {DAILY_HALT_USD}$")
    if ex:
        write_halt_flag(utc_day, day_pnl[utc_day])
        raise SystemExit(HALT_EXIT_CODE)
    return utc_day


def refresh_watchlist(tracked, now):
    for slug in strategy.watchlist():
        if slug not in tracked:
            pm = pmq.parse_market(pmq.get_market(slug, log))
            tracked[slug] = {"pm": pm, "first_seen": now, "fills": [],
                             "spent": 0.0, "fees": 0.0, "side": None,
                             "winner": None, "src": "", "resolved": pm is None,
                             "poisoned": False, "sstate": {}}
            log(f"{slug}: tracking" if pm else f"{slug}: not found, skipped")


def size_order(book, price_cap, usd, remaining):
    """Budget with taker-fee headroom, then reality checks against the live
    book: an executable ask at or under the cap, capped by displayed depth,
    above the per-market exchange minimum. Returns (usd, ask) or None."""
    usd = min(usd, remaining / (1 + FEE_RATE * (1 - price_cap)))
    _, _, ask, ask_sz = pmq.best_bid_ask(book)
    if ask is None or ask > price_cap:
        return None
    min_sh = pmq.book_meta(book)["min_order_size"] or 0.0
    usd = min(usd, ask * (ask_sz or 0.0))
    if usd < 1.0 or usd / ask < min_sh:
        return None
    return usd, ask


def execute_live(ex, slug, st, pm, token, side_name, price_cap, usd, consec_fails):
    """One live buy under the fail-closed contract. Returns
    (fill_price, fill_shares, order_id, consec_fails); fill_price is None
    when nothing was booked (clean rejection, or poisoned market)."""
    try:
        fill = ex.buy_fak(token, price_cap, usd)
    except OrderUncertain as e:
        st["poisoned"] = True
        log(f"{slug}: POISONED ({e}); reconciling")
        totals = ex.reconcile(pm["condition_id"], token)
        if totals and totals[0] > 0:
            sh, spent, fees = totals
            st.update(side=side_name, fills=[(spent / sh, sh)],
                      spent=spent, fees=fees)
        return None, None, None, consec_fails
    if fill.rejected or not fill:
        consec_fails += 1
        if consec_fails >= MAX_CONSEC_FAILS:
            log(f"HALT: {MAX_CONSEC_FAILS} consecutive failed buys")
            raise SystemExit(HALT_EXIT_CODE)
        return None, None, None, consec_fails
    return fill.price, fill.matched_shares, fill.order_id, 0


def enter_market(slug, st, ex, now, consec_fails):
    """Ask the strategy about one open market and book at most one fill."""
    pm = st["pm"]
    book_a = pmq.get_book(pm["token_a"], log)
    book_b = pmq.get_book(pm["token_b"], log)

    remaining = STAKE_USD - st["spent"] - st["fees"]
    order = strategy.decide(
        pm=pm, book_a=book_a, book_b=book_b,
        remaining_usd=remaining, side_held=st["side"], state=st["sstate"])
    if not order:
        return consec_fails
    side_key, price_cap, usd = order              # ('a'|'b', float, float)
    token = pm["token_a"] if side_key == "a" else pm["token_b"]
    side_name = pm["outcome_a"] if side_key == "a" else pm["outcome_b"]
    book = book_a if side_key == "a" else book_b
    if st["side"] not in (None, side_name):
        return consec_fails                        # one side per market
    sized = size_order(book, price_cap, usd, remaining)
    if sized is None:
        return consec_fails
    usd, ask = sized

    # paper fills happen at the REAL ask, never at the wished cap
    fill_p, fill_sh, order_id = ask, usd / ask, ""
    if ex:
        fill_p, fill_sh, order_id, consec_fails = execute_live(
            ex, slug, st, pm, token, side_name, price_cap, usd, consec_fails)
        if fill_p is None:
            return consec_fails
    st["side"] = side_name
    st["fills"].append((fill_p, fill_sh))
    st["spent"] += fill_p * fill_sh
    st["fees"] += pmq.fee(fill_p, fill_sh, FEE_RATE)
    _, _, _, ask_sz = pmq.best_bid_ask(book)
    append_csv(FILLS_CSV, {
        "ts": round(now, 1), "family": slug,
        "window": pm["end_ts"] or int(st["first_seen"]), "mode": MODE,
        "side": side_name, "price": round(fill_p, 4),
        "shares": round(fill_sh, 2), "notional": round(fill_p * fill_sh, 2),
        "ask_size_at_fill": ask_sz, "band_depth_usd": "",
        "secs_left": round(pm["end_ts"] - now, 1) if pm["end_ts"] else "",
        "order_id": order_id}, FILLS_HEADER)
    log(f"{slug}: FILL {side_name} {fill_sh:.2f}sh @ {fill_p:.3f}")
    return consec_fails


def enter_markets(tracked, ex, now, utc_day, halted_day, consec_fails):
    for slug, st in tracked.items():
        pm = st["pm"]
        if not pm or st["resolved"] or st["poisoned"] or halted_day == utc_day:
            continue
        if pm["end_ts"] and now > pm["end_ts"]:
            continue
        consec_fails = enter_market(slug, st, ex, now, consec_fails)
    return consec_fails


def score_market(slug, st, ex, day_pnl, now):
    """Settle one market against its resolved winner; in live mode the
    scored numbers come from get_trades (exchange truth), never local booking."""
    pm = st["pm"]
    fresh = pmq.parse_market(pmq.get_market(slug, log))
    winner = pmq.resolved_winner(fresh)
    if winner is None:
        if now - st["first_seen"] > MAX_TRACK_H * 3600:
            log(f"{slug}: unresolved after {MAX_TRACK_H}h, dropping "
                f"({len(st['fills'])} fills unscored)")
            st["resolved"] = True
        return
    shares = sum(s for _, s in st["fills"])
    spent, fees = st["spent"], st["fees"]
    if ex:
        tok = pm["token_a"] if st["side"] == pm["outcome_a"] else pm["token_b"]
        totals = ex.trades_totals(pm["condition_id"], tok, fee_rate=FEE_RATE)
        if totals is None:
            return
        shares, spent, fees = totals
    if shares <= 0:
        st["resolved"] = True
        return
    avg_p = spent / shares
    won = st["side"] == winner
    gross = shares * (1 - avg_p) if won else -spent
    net = gross - fees
    d = time.strftime("%Y-%m-%d", time.gmtime(now))
    day_pnl[d] = day_pnl.get(d, 0.0) + net
    append_csv(WINDOWS_CSV, {
        "family": slug, "window": pm["end_ts"] or int(st["first_seen"]),
        "mode": MODE, "side": st["side"], "n_fills": len(st["fills"]),
        "avg_price": round(avg_p, 4), "shares": round(shares, 2),
        "spent": round(spent, 2), "winner": winner,
        "winner_source": "gamma", "gross_pnl": round(gross, 2),
        "fee": round(fees, 4), "net_pnl": round(net, 2),
        "scored_ts": round(now, 1)}, WINDOWS_HEADER)
    log(f"{slug}: SCORED {st['side']} net={net:+.2f}$ "
        f"winner={winner} day={day_pnl[d]:+.2f}$")
    st["resolved"] = True
    if ex:
        # republish the CLOB-visible balance so the dashboard tracks
        # exchange truth without ever holding keys
        c = ex.collateral()
        if c > 0:
            log(f"collateral {c:.2f} USDC")


def score_markets(tracked, ex, day_pnl, now):
    for slug, st in list(tracked.items()):
        if st["resolved"] or st["pm"] is None:
            continue
        if not st["fills"] and not st["poisoned"]:
            if now - st["first_seen"] > MAX_TRACK_H * 3600 or \
                    slug not in set(strategy.watchlist()):
                del tracked[slug]
            continue
        score_market(slug, st, ex, day_pnl, now)


def main(run_hours):
    t_end = time.time() + run_hours * 3600
    ex = live_startup() if MODE == "live" else None
    log(f"bot starting: mode={MODE} stake=${STAKE_USD}/market poll={POLL_S}s "
        f"daily_halt={DAILY_HALT_USD}$ strategy={strategy.NAME}")

    tracked, day_pnl, halted_day = {}, {}, None
    consec_fails = 0
    recover_orphans(tracked)
    last_score_poll = 0.0

    while time.time() < t_end:
        try:
            now = time.time()
            utc_day = time.strftime("%Y-%m-%d", time.gmtime(now))
            halted_day = apply_daily_halt(day_pnl, utc_day, halted_day, ex)
            refresh_watchlist(tracked, now)
            consec_fails = enter_markets(tracked, ex, now, utc_day,
                                         halted_day, consec_fails)
            if now - last_score_poll >= SCORE_POLL_S:
                last_score_poll = now
                score_markets(tracked, ex, day_pnl, now)
        except SystemExit:
            raise
        except Exception as e:
            log(f"main loop error (continuing): {e}")
        time.sleep(POLL_S)

    log("run complete")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 24.0)
