"""pmq-doctor: one command that diagnoses the classic Polymarket V2 setup
failures (the ones behind a dozen open issues on the official client):
wrong signature_type for a deposit wallet, api key confusion, CLOB balance 0
while funds sit on-chain, drifted client surface, per-market minimums.

Read-only: derives addresses, calls public RPC and CLOB endpoints. Never
prints or transmits the private key. Exit code 0 when everything is green.
"""
import inspect
import json
import os
import sys
import urllib.request

from .data import best_bid_ask, book_meta, fee, get_book, get_market, parse_market

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
RPCS = ["https://polygon-rpc.com", "https://polygon-bor-rpc.publicnode.com",
        "https://1rpc.io/matic", "https://polygon.drpc.org"]
GREEN, RED, WARN = "[ok]", "[!!]", "[??]"


def _rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last = None
    for rpc in RPCS:
        try:
            req = urllib.request.Request(rpc, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                out = json.loads(r.read().decode())
            if "error" in out:
                raise RuntimeError(out["error"])
            return out["result"]
        except Exception as e:
            last = e
    raise last


def looks_like_minimal_proxy(bytecode):
    """ERC-1167 minimal proxies (Polymarket deposit wallets included) are a
    tiny stub; a bare EOA has no code at all."""
    return bytecode not in (None, "", "0x") and len(bytecode) < 400


def advise_sig_type(funder_is_contract, owner_is_eoa, funder_equals_eoa):
    """One sentence of advice from the on-chain facts."""
    if funder_equals_eoa:
        return 0, "funder IS the EOA: signature_type=0"
    if funder_is_contract and owner_is_eoa:
        return 3, ("funder is a contract owned by your EOA: a deposit wallet, "
                   "signature_type=3 (POLY_1271)")
    if funder_is_contract:
        return None, ("funder is a contract NOT owned by this EOA: wrong "
                      "POLY_PRIVATE_KEY, or someone else's wallet")
    return None, "funder has no code on-chain: not a deployed wallet"


def check(ok, label, detail=""):
    print(f"{GREEN if ok else RED} {label}" + (f": {detail}" if detail else ""))
    return bool(ok)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    market_arg = argv[argv.index("--market") + 1] if "--market" in argv else None
    all_ok = True
    print("pmq-doctor: Polymarket V2 setup diagnosis (read-only, no key ever printed)\n")

    # 1. installed surface
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType

        from .executor import _EXPECTED_MARKET_ARGS, _EXPECTED_METHODS
        drifts = []
        for name, params in _EXPECTED_METHODS.items():
            fn = getattr(ClobClient, name, None)
            have = set(inspect.signature(fn).parameters) if fn else set()
            drifts += [f"{name}.{p}" for p in params if p not in have]
        have = set(inspect.signature(MarketOrderArgsV2).parameters)
        drifts += [f"MarketOrderArgsV2.{p}" for p in _EXPECTED_MARKET_ARGS if p not in have]
        if not hasattr(OrderType, "FAK"):
            drifts.append("OrderType.FAK")
        all_ok &= check(not drifts, "installed py-clob-client-v2 matches the verified surface",
                        "drifted: " + ", ".join(drifts) if drifts else "")
    except ImportError as e:
        all_ok &= check(False, "py-clob-client-v2 installed", str(e))
        print("\nverdict: pip install py-clob-client-v2")
        return 1

    # 2. environment and identities
    key = os.environ.get("POLY_PRIVATE_KEY")
    funder = os.environ.get("POLY_FUNDER")
    sig = os.environ.get("POLY_SIG_TYPE", "0")
    if not key:
        check(False, "POLY_PRIVATE_KEY present in the environment")
        print("\nverdict: export POLY_PRIVATE_KEY (data-layer usage needs no key)")
        return 1
    check(True, "POLY_PRIVATE_KEY present (never printed)")
    from eth_account import Account
    eoa = Account.from_key(key).address
    print(f"     derived EOA: {eoa}")
    print(f"     POLY_FUNDER: {funder or '(unset)'}   POLY_SIG_TYPE: {sig}")

    # 3. on-chain truth about the funder
    expected_sig = 0
    if funder and funder.lower() != eoa.lower():
        try:
            code = _rpc("eth_getCode", [funder, "latest"])
            is_contract = looks_like_minimal_proxy(code) or len(code or "0x") > 2
            owner = None
            try:
                res = _rpc("eth_call", [{"to": funder, "data": "0x8da5cb5b"}, "latest"])
                owner = "0x" + res[-40:]
            except Exception:
                pass
            owner_is_eoa = bool(owner) and owner.lower() == eoa.lower()
            expected_sig, advice = advise_sig_type(is_contract, owner_is_eoa, False)
            all_ok &= check(expected_sig is not None, "funder wallet on-chain", advice)
            bal = int(_rpc("eth_call", [{"to": PUSD, "data":
                      "0x70a08231" + funder.lower()[2:].rjust(64, "0")}, "latest"]), 16) / 1e6
            print(f"     on-chain pUSD at funder: {bal:.2f}")
        except Exception as e:
            check(False, "on-chain funder checks (RPC)", str(e)[:120])
    else:
        expected_sig, advice = advise_sig_type(False, False, True)
        check(True, "funder", advice)

    if expected_sig is not None:
        good = int(sig) == expected_sig
        all_ok &= check(good, f"POLY_SIG_TYPE={sig} matches the wallet type",
                        "" if good else f"set POLY_SIG_TYPE={expected_sig}")

    # 4. CLOB view with the configured identity
    try:
        from .executor import PolymarketExecutor
        ex = PolymarketExecutor()
        usdc = ex.collateral()
        seen = check(usdc > 0, f"CLOB sees collateral with sig_type={sig}", f"{usdc:.2f} USDC")
        all_ok &= seen
        if not seen:
            for st in (0, 1, 2, 3):
                if st == int(sig):
                    continue
                try:
                    alt = PolymarketExecutor(signature_type=st).collateral()
                    if alt > 0:
                        print(f"     but sig_type={st} sees {alt:.2f} USDC: "
                              f"set POLY_SIG_TYPE={st}")
                        break
                except Exception:
                    continue
    except Exception as e:
        all_ok &= check(False, "CLOB auth/collateral", str(e)[:160])

    # 5. optional market checks
    if market_arg:
        pm = parse_market(get_market(market_arg))
        if pm:
            b = get_book(pm["token_a"])
            meta = book_meta(b)
            bid, _, ask, _ = best_bid_ask(b)
            print(f"{GREEN} market {market_arg}: bid={bid} ask={ask} "
                  f"min_order_size={meta['min_order_size']} tick={meta['tick_size']}")
            print(f"     taker fee at ask: {fee(ask or 0.5, 1.0):.4f}$/share (crypto table; "
                  f"authoritative per-market rate via executor.fee_rate)")
        else:
            all_ok &= check(False, f"market {market_arg} resolvable",
                            "expired or wrong slug; recurring families need the window "
                            "start suffix, e.g. btc-updown-15m-<unix_ts>")

    print("\nverdict:", "everything green, orders should work" if all_ok else
          "fix the [!!] lines above, in order")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
