#!/usr/bin/env python3
"""bot_dash: read-only web dashboard for the pmq bot template.

Design constraints:
  - stdlib only (http.server), one process, a few MB of RSS: safe on the Pi
  - READ ONLY: parses bot_runs CSVs, systemctl show and the bot's journal;
    never reads live.env, never talks to any exchange API, no secrets
  - the phone does the rendering; this process only ships small JSON + one
    static HTML file (cached in memory, CSVs re-parsed only on mtime change)
"""
import csv
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DASH_PORT", "8080"))
RUNS = os.path.abspath(os.environ.get("BOT_OUT_DIR", "/home/bot/pmq-bot/bot_runs"))
SERVICE = os.environ.get("DASH_SERVICE", "pmq-bot")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_AGE_S = 45 * 86400          # ship at most 45 days of rows
SVC_CACHE_S = 20                # systemd/journal probe cache

_lock = threading.Lock()
_cache = {}


def _floats(row, keys):
    for k in keys:
        try:
            row[k] = float(row[k])
        except (KeyError, TypeError, ValueError):
            row[k] = None
    return row


def load_csv(name, numeric):
    """Rows of bot_runs/<name>.csv, cached by mtime, oldest first."""
    path = os.path.join(RUNS, f"{name}.csv")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return []
    with _lock:
        cached = _cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        with open(path, newline="") as f:
            rows = [_floats(r, numeric) for r in csv.DictReader(f)]
    except OSError:
        rows = []
    with _lock:
        _cache[name] = (mtime, rows)
    return rows


def _run(cmd):
    env = dict(os.environ, LC_ALL="C")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=env)
        return r.stdout
    except Exception:
        return ""


def service_state():
    """bot systemd state + last collateral line from its journal."""
    with _lock:
        cached = _cache.get("svc")
        if cached and time.time() - cached[0] < SVC_CACHE_S:
            return cached[1]
    out = _run(["systemctl", "show", SERVICE, "-p", "ActiveState",
                "-p", "SubState", "-p", "ActiveEnterTimestamp",
                "-p", "Environment"])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    envtok = {}
    for tok in props.get("Environment", "").split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            envtok[k] = v            # later tokens win, like systemd does
    start_epoch = None
    ts = props.get("ActiveEnterTimestamp", "")
    if ts:
        try:
            start_epoch = time.mktime(time.strptime(" ".join(ts.split()[:3]),
                                                    "%a %Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass
    collateral = collateral_ts = None
    jout = _run(["journalctl", "-u", SERVICE, "-o", "short-unix",
                 "-n", "1500", "--no-pager", "-q"])
    lines = jout.splitlines()
    for line in reversed(lines):
        # matches both the startup line and the per-scoring republication
        if "collateral" in line:
            try:
                collateral = float(line.split("collateral", 1)[1].split()[0])
                collateral_ts = float(line.split(" ", 1)[0])
            except (ValueError, IndexError):
                pass
            break
    last_log_ts = None
    if lines:
        try:
            last_log_ts = float(lines[-1].split(" ", 1)[0])
        except (ValueError, IndexError):
            pass
    svc = {
        "active": props.get("ActiveState", "unknown"),
        "sub": props.get("SubState", ""),
        "start_epoch": start_epoch,
        "mode": envtok.get("BOT_MODE", "?"),
        "stake": envtok.get("BOT_STAKE", "?"),
        "markets": envtok.get("BOT_FAMILIES", ""),
        "collateral": collateral,
        "collateral_ts": collateral_ts,
        "last_log_ts": last_log_ts,
    }
    with _lock:
        _cache["svc"] = (time.time(), svc)
    return svc


def api_payload():
    now = time.time()
    horizon = now - MAX_AGE_S
    windows = [w for w in load_csv("windows", ["window", "n_fills", "avg_price",
               "shares", "spent", "gross_pnl", "fee", "net_pnl", "scored_ts"])
               if (w["scored_ts"] or 0) >= horizon][-2000:]
    fills = [f for f in load_csv("fills", ["ts", "window", "price", "shares",
             "notional", "secs_left"])
             if (f["ts"] or 0) >= horizon][-2000:]
    fills = [{k: f.get(k) for k in ("ts", "family", "window", "mode", "side",
              "price", "shares", "notional", "secs_left", "order_id")}
             for f in fills]
    return {"now": now, "service": service_state(),
            "windows": windows, "fills": fills}


class Handler(BaseHTTPRequestHandler):
    server_version = "pmq-bot-dash"

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/data":
            body = json.dumps(api_payload(), separators=(",", ":"),
                              allow_nan=False, default=lambda o: None).encode()
            self._send(200, "application/json", body)
        elif path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "dash.html"), "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except OSError:
                self._send(500, "text/plain", b"dash.html missing")
        else:
            self._send(404, "text/plain", b"not found")

    def log_message(self, fmt, *args):
        pass                       # keep the journal quiet


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"pmq-bot-dash listening on :{PORT}", flush=True)
    srv.serve_forever()
