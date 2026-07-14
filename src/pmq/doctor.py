"""pmq-doctor: one command that diagnoses the classic Polymarket V2 setup
failures (the ones behind a dozen open issues on the official client):
an unfunded or wrong funder address, wrong signature_type for a deposit
wallet, funds on-chain that no signature_type sees (wallet never associated
with the CLOB backend), api key confusion, drifted client surface,
per-market minimums.

Read-only: derives addresses, calls public RPC and CLOB endpoints. Never
prints or transmits the private key, and never posts an order: it verifies
the setup causes, not the order path itself. Exit code 0 when everything is
green.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any

from .data import best_bid_ask, book_meta, fee, get_book, get_market, parse_market

PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
RPCS = ["https://polygon-rpc.com", "https://polygon-bor-rpc.publicnode.com",
        "https://1rpc.io/matic", "https://polygon.drpc.org"]
GREEN, RED, WARN = "[ok]", "[!!]", "[??]"


def _rpc(method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last: Exception = RuntimeError("no RPC endpoint reachable")
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


def looks_like_minimal_proxy(bytecode: str | None) -> bool:
    """ERC-1167 minimal proxies (Polymarket deposit wallets included) are a
    tiny stub; a bare EOA has no code at all."""
    return bytecode not in (None, "", "0x") and len(bytecode or "") < 400


def advise_sig_type(funder_is_contract: bool, owner_is_eoa: bool | None,
                    funder_equals_eoa: bool) -> tuple[int | None, str]:
    """One sentence of advice from the on-chain facts. ``owner_is_eoa`` is
    tri-state: None means owner() gave no answer (some Polymarket wallet
    generations, ERC-1967 beacon proxies and ERC-1167 clones, do not expose
    it), which is unverifiable, not wrong."""
    if funder_equals_eoa:
        return 0, "funder IS the EOA: signature_type=0"
    if funder_is_contract and owner_is_eoa:
        return 3, ("funder is a contract owned by your EOA: a deposit wallet, "
                   "signature_type=3 (POLY_1271)")
    if funder_is_contract and owner_is_eoa is None:
        return None, ("owner() not readable on the funder (usual for ERC-1967 "
                      "beacon and ERC-1167 clone generations): ownership "
                      "unverifiable read-only, the CLOB collateral check below "
                      "is the authority")
    if funder_is_contract:
        return None, ("funder is a contract NOT owned by this EOA: wrong "
                      "POLY_PRIVATE_KEY, or someone else's wallet")
    return None, "funder has no code on-chain: not a deployed wallet"


def check(ok: object, label: str, detail: str = "") -> bool:
    print(f"{GREEN if ok else RED} {label}" + (f": {detail}" if detail else ""))
    return bool(ok)


def warn(label: str, detail: str = "") -> bool:
    """A [??] line: could not be verified read-only. Never fails the run."""
    print(f"{WARN} {label}" + (f": {detail}" if detail else ""))
    return True


def _check_surface() -> bool | None:
    """Installed py-clob-client-v2 vs the verified surface. None is fatal
    (client not installed at all)."""
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import (
            MarketOrderArgsV2,
            OrderArgsV2,
            OrderType,
        )

        from .executor import _surface_drifts_over
        drifts = _surface_drifts_over(
            lambda n: getattr(ClobClient, n, None),
            MarketOrderArgsV2, OrderArgsV2, OrderType)
        return check(not drifts, "installed py-clob-client-v2 matches the verified surface",
                     "drifted: " + ", ".join(drifts) if drifts else "")
    except ImportError as e:
        check(False, "py-clob-client-v2 installed", str(e))
        return None


def _check_identity() -> tuple[bool, str, str | None, str, int] | None:
    """Key presence and derived identities. None is fatal (no key). Returns
    (ok, eoa, funder, sig_str, sig_val); sig_val is -1 when non-numeric."""
    key = os.environ.get("POLY_PRIVATE_KEY")
    funder = os.environ.get("POLY_FUNDER")
    sig = os.environ.get("POLY_SIG_TYPE", "0")
    if not key:
        check(False, "POLY_PRIVATE_KEY present in the environment")
        return None
    check(True, "POLY_PRIVATE_KEY present (never printed)")
    from eth_account import Account
    eoa = Account.from_key(key).address
    print(f"     derived EOA: {eoa}")
    print(f"     POLY_FUNDER: {funder or '(unset)'}   POLY_SIG_TYPE: {sig}")
    ok = True
    try:
        sig_val = int(sig)
    except ValueError:
        ok = check(False, "POLY_SIG_TYPE is a number",
                   f"{sig!r} is not; use 0 (EOA) or 3 (deposit wallet)")
        sig_val = -1
    return ok, eoa, funder, sig, sig_val


def _owner_of(addr: str) -> str | None:
    """owner() of a contract wallet; None when it reverts or answers nothing
    (unverifiable, NOT proof of a wrong key)."""
    try:
        res = _rpc("eth_call", [{"to": addr, "data": "0x8da5cb5b"}, "latest"])
    except Exception:
        return None
    return "0x" + res[-40:] if isinstance(res, str) and len(res) >= 42 else None


def _check_funded(addr: str) -> tuple[bool, float]:
    """On-chain pUSD balance at the funding address, as its own verdict.
    Zero is the issue-#87 shape: nothing was ever deposited there."""
    raw = _rpc("eth_call", [{"to": PUSD, "data":
              "0x70a08231" + addr.lower()[2:].rjust(64, "0")}, "latest"])
    bal = int(raw, 16) / 1e6
    if bal > 0:
        return check(True, "funder holds USDC on-chain", f"{bal:.2f} pUSD"), bal
    return check(False, "funder holds no USDC on-chain",
                 "deposit first, or wrong funder address"), bal


def _funder_wallet_status(eoa: str, funder: str) -> tuple[bool, int | None]:
    """One verdict line about the funder contract: green when owned by this
    EOA, warn when owner() is unreadable (proxy generations differ), red only
    when owner() names a DIFFERENT address or the funder has no code."""
    code = _rpc("eth_getCode", [funder, "latest"])
    is_contract = code not in (None, "", "0x")
    owner = _owner_of(funder) if is_contract else None
    owner_is_eoa = None if owner is None else owner.lower() == eoa.lower()
    expected_sig, advice = advise_sig_type(is_contract, owner_is_eoa, False)
    if is_contract and owner_is_eoa is None:
        ok = warn("funder wallet on-chain", advice)
        print("     bytecode is " + ("an ERC-1167 minimal proxy, the deposit wallet shape"
              if looks_like_minimal_proxy(code) else "a full contract, not a minimal proxy"))
        return ok, expected_sig
    return check(expected_sig is not None, "funder wallet on-chain", advice), expected_sig


def _check_funder(eoa: str, funder: str | None) -> tuple[bool, int | None, float | None]:
    """On-chain truth about the funder. Returns (ok, expected_sig, onchain_usd);
    expected_sig None means no on-chain advice possible (wrong wallet,
    unverifiable owner, RPC down); onchain_usd None means balance unknown."""
    self_funded = not funder or funder.lower() == eoa.lower()
    try:
        if self_funded:
            expected_sig, advice = advise_sig_type(False, False, True)
            ok = check(True, "funder", advice)
        else:
            ok, expected_sig = _funder_wallet_status(eoa, funder or "")
        funded, bal = _check_funded(funder or eoa)
        return ok and funded, expected_sig, bal
    except Exception as e:
        return (check(False, "on-chain funder checks (RPC)", str(e)[:120]),
                0 if self_funded else None, None)


def _check_api_key(ex: Any) -> bool:
    """The api key handshake, made explicit: create returns 400 when a key
    already exists, and create_or_derive_api_key then derives that existing
    key. Non fatal by construction, consistent with a key that already
    exists. The key belongs to the EOA (shown so it can be eyeballed against
    the derived EOA above), whatever wallet signs the orders."""
    try:
        addr = ex.client.get_address()
        raw = ex.client.get_api_keys()
        keys = raw.get("apiKeys") if isinstance(raw, dict) else raw
        n = len(keys) if isinstance(keys, list) else 0
        return check(n > 0, f"api key registered for EOA {addr}",
                     f"{n} key(s); a 400 from create is non fatal by "
                     "construction: the key already exists and derive returns it")
    except Exception as e:
        return check(False, "api key listing (L2 auth)", str(e)[:120])


def _probe_sig_types(sig_val: int) -> int | None:
    """Try the other signature types read-only; name the one that sees funds."""
    from .executor import PolymarketExecutor
    for st in (0, 1, 2, 3):
        if st == sig_val:
            continue
        try:
            alt = PolymarketExecutor(signature_type=st).collateral()
        except Exception:
            continue
        if alt > 0:
            print(f"     but sig_type={st} sees {alt:.2f} USDC: "
                  f"set POLY_SIG_TYPE={st}")
            return st
    return None


def _check_clob(sig: str, sig_val: int, onchain_usd: float | None) -> bool:
    """Does the CLOB see collateral with the configured identity? On zero,
    probe the other signature types and name the one that works; funds
    sitting on-chain that NO signature type sees mean the wallet was never
    associated with the CLOB backend."""
    try:
        from .executor import PolymarketExecutor
        ex = PolymarketExecutor()
        ok = _check_api_key(ex)
        usdc = ex.collateral()
        if usdc > 0:
            return check(True, f"CLOB sees collateral with sig_type={sig}",
                         f"{usdc:.2f} USDC") and ok
        detail = ("funds on-chain but CLOB sees 0: wrong signature_type or "
                  "wallet not associated" if onchain_usd else "0.00 USDC")
        check(False, f"CLOB sees collateral with sig_type={sig}", detail)
        if _probe_sig_types(sig_val) is None and onchain_usd:
            print("     no signature_type sees the on-chain funds: the wallet "
                  "was never associated with the CLOB backend; create it in "
                  "the Polymarket app, or deploy it via the relayer "
                  "(deploy_deposit_wallet) or polymarket-client "
                  "AsyncSecureClient.create")
        return False
    except Exception as e:
        return check(False, "CLOB auth/collateral", str(e)[:160])


def _signer_note(sig_val: int) -> None:
    """Printed under signature_type=3 only: what this doctor can and cannot
    prove. Order signing uses the deposit wallet while the api key stays the
    EOA's; only the backend association makes the pair valid."""
    if sig_val != 3:
        return
    print('     note: with sig_type=3 the order signer is the deposit wallet '
          'and the api key belongs to the EOA, by design; orders authorize '
          'only when the CLOB backend links the two. This doctor is read-only '
          'and never posts an order: if every line above is green and orders '
          'still fail with "signer must be the address of the API KEY", the '
          'missing link is that backend association (Polymarket app, relayer '
          'deploy_deposit_wallet, or polymarket-client AsyncSecureClient.create)')


def _check_market(market_arg: str) -> bool:
    """Optional per-market exchange rules: book, minimum size, tick, fee."""
    pm = parse_market(get_market(market_arg))
    if not pm:
        return check(False, f"market {market_arg} resolvable",
                     "expired or wrong slug; recurring families need the window "
                     "start suffix, e.g. btc-updown-15m-<unix_ts>")
    b = get_book(pm["token_a"])
    meta = book_meta(b)
    bid, _, ask, _ = best_bid_ask(b)
    print(f"{GREEN} market {market_arg}: bid={bid} ask={ask} "
          f"min_order_size={meta['min_order_size']} tick={meta['tick_size']}")
    if ask is not None:
        print(f"     taker fee at ask: {fee(ask, 1.0):.4f}$/share (crypto table; "
              f"authoritative per-market rate via executor.fee_rate)")
    if meta["min_order_size"] and ask:
        print(f"     smallest possible order here: about "
              f"{meta['min_order_size'] * ask:.2f}$")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    market_arg = None
    if "--market" in argv:
        i = argv.index("--market")
        if i + 1 >= len(argv):
            print("usage: pmq-doctor [--market <gamma-slug>]")
            return 2
        market_arg = argv[i + 1]
    print("pmq-doctor: Polymarket V2 setup diagnosis (read-only, no key ever printed)\n")

    surface_ok = _check_surface()
    if surface_ok is None:
        print("\nverdict: pip install py-clob-client-v2")
        return 1
    all_ok = surface_ok

    identity = _check_identity()
    if identity is None:
        print("\nverdict: export POLY_PRIVATE_KEY (data-layer usage needs no key)")
        return 1
    id_ok, eoa, funder, sig, sig_val = identity
    all_ok &= id_ok

    funder_ok, expected_sig, onchain_usd = _check_funder(eoa, funder)
    all_ok &= funder_ok
    if expected_sig is not None:
        good = sig_val == expected_sig
        all_ok &= check(good, f"POLY_SIG_TYPE={sig} matches the wallet type",
                        "" if good else f"set POLY_SIG_TYPE={expected_sig}")

    all_ok &= _check_clob(sig, sig_val, onchain_usd)
    _signer_note(sig_val)

    if market_arg:
        all_ok &= _check_market(market_arg)

    print("\nverdict:", "everything green, orders should work" if all_ok else
          "fix the [!!] lines above, in order")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
