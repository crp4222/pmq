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


def main(run_hours):
    t_end = time.time() + run_hours * 3600
    ex = None
    consec_fails = 0
    if MODE == "live":
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
    log(f"bot starting: mode={MODE} stake=${STAKE_USD}/market poll={POLL_S}s "
        f"daily_halt={DAILY_HALT_USD}$ strategy={strategy.NAME}")

    tracked, day_pnl, halted_day = {}, {}, None
    last_score_poll = 0.0

    while time.time() < t_end:
        try:
            now = time.time()
            utc_day = time.strftime("%Y-%m-%d", time.gmtime(now))

            if utc_day != halted_day and day_pnl.get(utc_day, 0.0) <= DAILY_HALT_USD:
                halted_day = utc_day
                log(f"DAILY HALT: {day_pnl[utc_day]:+.2f}$ <= {DAILY_HALT_USD}$")
                if ex:
                    write_halt_flag(utc_day, day_pnl[utc_day])
                    raise SystemExit(HALT_EXIT_CODE)

            # ---- watchlist ----
            for slug in strategy.watchlist():
                if slug not in tracked:
                    pm = pmq.parse_market(pmq.get_market(slug, log))
                    tracked[slug] = {"pm": pm, "first_seen": now, "fills": [],
                                     "spent": 0.0, "fees": 0.0, "side": None,
                                     "winner": None, "src": "", "resolved": pm is None,
                                     "poisoned": False, "sstate": {}}
                    log(f"{slug}: tracking" if pm else f"{slug}: not found, skipped")

            # ---- entries ----
            for slug, st in tracked.items():
                pm = st["pm"]
                if not pm or st["resolved"] or st["poisoned"] or halted_day == utc_day:
                    continue
                if pm["end_ts"] and now > pm["end_ts"]:
                    continue
                book_a = pmq.get_book(pm["token_a"], log)
                book_b = pmq.get_book(pm["token_b"], log)

                remaining = STAKE_USD - st["spent"] - st["fees"]
                order = strategy.decide(
                    pm=pm, book_a=book_a, book_b=book_b,
                    remaining_usd=remaining, side_held=st["side"], state=st["sstate"])
                if not order:
                    continue
                side_key, price_cap, usd = order          # ('a'|'b', float, float)
                token = pm["token_a"] if side_key == "a" else pm["token_b"]
                side_name = pm["outcome_a"] if side_key == "a" else pm["outcome_b"]
                book = book_a if side_key == "a" else book_b
                if st["side"] not in (None, side_name):
                    continue                               # one side per market
                usd = min(usd, remaining / (1 + FEE_RATE * (1 - price_cap)))
                # reality checks against the live book: an executable ask at
                # or under the cap, and the per-market exchange minimum size
                _, _, ask, ask_sz = pmq.best_bid_ask(book)
                if ask is None or ask > price_cap:
                    continue
                min_sh = pmq.book_meta(book)["min_order_size"] or 0.0
                usd = min(usd, ask * (ask_sz or 0.0))
                if usd < 1.0 or usd / ask < min_sh:
                    continue

                # paper fills happen at the REAL ask, never at the wished cap
                fill_p, fill_sh, order_id = ask, usd / ask, ""
                if ex:
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
                        continue
                    if fill.rejected or not fill:
                        consec_fails += 1
                        if consec_fails >= MAX_CONSEC_FAILS:
                            log(f"HALT: {MAX_CONSEC_FAILS} consecutive failed buys")
                            raise SystemExit(HALT_EXIT_CODE)
                        continue
                    consec_fails = 0
                    fill_p, fill_sh, order_id = fill.price, fill.matched_shares, fill.order_id
                st["side"] = side_name
                st["fills"].append((fill_p, fill_sh))
                st["spent"] += fill_p * fill_sh
                st["fees"] += pmq.fee(fill_p, fill_sh, FEE_RATE)
                _, _, _, ask_sz = pmq.best_bid_ask(book_a if side_key == "a" else book_b)
                append_csv(FILLS_CSV, {
                    "ts": round(now, 1), "family": slug,
                    "window": pm["end_ts"] or int(st["first_seen"]), "mode": MODE,
                    "side": side_name, "price": round(fill_p, 4),
                    "shares": round(fill_sh, 2), "notional": round(fill_p * fill_sh, 2),
                    "ask_size_at_fill": ask_sz, "band_depth_usd": "",
                    "secs_left": round(pm["end_ts"] - now, 1) if pm["end_ts"] else "",
                    "order_id": order_id}, FILLS_HEADER)
                log(f"{slug}: FILL {side_name} {fill_sh:.2f}sh @ {fill_p:.3f}")

            # ---- scoring (resolution poll, cheap cadence) ----
            if now - last_score_poll >= SCORE_POLL_S:
                last_score_poll = now
                for slug, st in list(tracked.items()):
                    pm = st["pm"]
                    if st["resolved"] or pm is None:
                        continue
                    if not st["fills"] and not st["poisoned"]:
                        if now - st["first_seen"] > MAX_TRACK_H * 3600 or \
                                slug not in set(strategy.watchlist()):
                            del tracked[slug]
                        continue
                    fresh = pmq.parse_market(pmq.get_market(slug, log))
                    winner = pmq.resolved_winner(fresh)
                    if winner is None:
                        if now - st["first_seen"] > MAX_TRACK_H * 3600:
                            log(f"{slug}: unresolved after {MAX_TRACK_H}h, dropping "
                                f"({len(st['fills'])} fills unscored)")
                            st["resolved"] = True
                        continue
                    shares = sum(s for _, s in st["fills"])
                    spent, fees = st["spent"], st["fees"]
                    if ex:                                # live: exchange truth
                        tok = pm["token_a"] if st["side"] == pm["outcome_a"] else pm["token_b"]
                        totals = ex.trades_totals(pm["condition_id"], tok, fee_rate=FEE_RATE)
                        if totals is None:
                            continue
                        shares, spent, fees = totals
                    if shares <= 0:
                        st["resolved"] = True
                        continue
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
                        # republish the CLOB-visible balance so the dashboard
                        # tracks exchange truth without ever holding keys
                        c = ex.collateral()
                        if c > 0:
                            log(f"collateral {c:.2f} USDC")

        except SystemExit:
            raise
        except Exception as e:
            log(f"main loop error (continuing): {e}")
        time.sleep(POLL_S)

    log("run complete")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 24.0)
