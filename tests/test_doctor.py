import os

import pytest

pytest.importorskip("py_clob_client_v2")

import pmq.doctor as doctor  # noqa: E402

# Well-known throwaway: private key 0x...01 derives this EOA. Never funded.
TEST_KEY = "0x" + "1".rjust(64, "0")
EOA = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
FUNDER = "0x" + "2" * 40
OTHER = "0x" + "9" * 40
PROXY_CODE = "0x363d3d373d3d3d363d73" + "ab" * 30


def rpc_mock(code=PROXY_CODE, owner=EOA, balance_usd=50.0):
    """Canned Polygon RPC: bytecode, owner() and pUSD balanceOf answers."""
    def _rpc(method, params):
        if method == "eth_getCode":
            return code
        data = params[0]["data"]
        if data == "0x8da5cb5b":
            if owner is None:
                raise RuntimeError("execution reverted")
            return "0x" + "0" * 24 + owner[2:].lower()
        if data.startswith("0x70a08231"):
            return hex(int(balance_usd * 1e6))
        raise AssertionError(f"unexpected rpc {method} {params}")
    return _rpc


class FakeExecutor:
    """CLOB stand-in: collateral visible only under the right sig_type; the
    api key surface (get_address, get_api_keys) answers like a live client,
    so ``self.client is self``. Set ``api_keys`` to an Exception to model a
    broken L2 auth."""
    balances = {}
    api_keys: object = {"apiKeys": ["key-1"]}
    raise_for: frozenset = frozenset()

    def __init__(self, signature_type=None, **kw):
        if signature_type is None:
            try:
                signature_type = int(os.environ.get("POLY_SIG_TYPE", "0"))
            except ValueError:
                signature_type = -1
        self.st = signature_type
        if self.st in type(self).raise_for:
            raise RuntimeError("no executor for this sig type")
        self.client = self

    def collateral(self):
        return type(self).balances.get(self.st, 0.0)

    def get_address(self):
        return EOA

    def get_api_keys(self):
        keys = type(self).api_keys
        if isinstance(keys, Exception):
            raise keys
        return keys


@pytest.fixture
def setup(monkeypatch):
    def _setup(sig="0", funder=None, rpc=None, balances=None):
        monkeypatch.setenv("POLY_PRIVATE_KEY", TEST_KEY)
        monkeypatch.setenv("POLY_SIG_TYPE", sig)
        if funder:
            monkeypatch.setenv("POLY_FUNDER", funder)
        else:
            monkeypatch.delenv("POLY_FUNDER", raising=False)
        monkeypatch.setattr(doctor, "_rpc", rpc or rpc_mock())
        FakeExecutor.balances = balances or {}
        FakeExecutor.api_keys = {"apiKeys": ["key-1"]}
        FakeExecutor.raise_for = frozenset()
        monkeypatch.setattr("pmq.executor.PolymarketExecutor", FakeExecutor)
    return _setup


def test_advise_sig_type_matrix():
    assert doctor.advise_sig_type(False, False, True)[0] == 0
    assert doctor.advise_sig_type(True, True, False)[0] == 3
    assert doctor.advise_sig_type(True, False, False)[0] is None
    assert doctor.advise_sig_type(False, False, False)[0] is None
    sig, msg = doctor.advise_sig_type(True, None, False)   # owner() unreadable
    assert sig is None and "owner() not readable" in msg
    assert "NOT owned" not in msg


def test_owner_of_tristate(monkeypatch):
    """_owner_of: an address answer, an empty answer (0x), a revert."""
    monkeypatch.setattr(doctor, "_rpc",
                        lambda m, p: "0x" + "0" * 24 + OTHER[2:])
    assert doctor._owner_of(FUNDER).lower() == OTHER.lower()
    monkeypatch.setattr(doctor, "_rpc", lambda m, p: "0x")
    assert doctor._owner_of(FUNDER) is None

    def revert(m, p):
        raise RuntimeError("execution reverted")
    monkeypatch.setattr(doctor, "_rpc", revert)
    assert doctor._owner_of(FUNDER) is None


def test_minimal_proxy_detector():
    assert doctor.looks_like_minimal_proxy("0x363d3d373d3d363d6020" + "ab" * 60)
    assert not doctor.looks_like_minimal_proxy("0x")
    assert not doctor.looks_like_minimal_proxy(None)
    assert not doctor.looks_like_minimal_proxy("0x" + "ab" * 300)


def test_main_without_key_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    assert doctor.main([]) == 1
    assert "POLY_PRIVATE_KEY" in capsys.readouterr().out


def test_main_market_flag_without_value_exits_2(capsys):
    assert doctor.main(["--market"]) == 2
    assert "usage" in capsys.readouterr().out


def test_deposit_wallet_all_green(setup, capsys):
    setup(sig="3", funder=FUNDER, balances={3: 87.65})
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "signature_type=3" in out
    assert "87.65 USDC" in out
    assert "everything green" in out


def test_wrong_sig_type_for_deposit_wallet(setup, capsys):
    setup(sig="0", funder=FUNDER, balances={0: 0.0, 3: 87.65})
    assert doctor.main([]) == 1
    assert "set POLY_SIG_TYPE=3" in capsys.readouterr().out


def test_funder_owned_by_someone_else(setup, capsys):
    setup(sig="3", funder=FUNDER, rpc=rpc_mock(owner=OTHER), balances={3: 5.0})
    assert doctor.main([]) == 1
    assert "NOT owned" in capsys.readouterr().out


def test_funder_is_the_eoa(setup, capsys):
    setup(sig="0", balances={0: 10.0})
    assert doctor.main([]) == 0
    assert "funder IS the EOA" in capsys.readouterr().out


def test_zero_collateral_probes_other_sig_types(setup, capsys):
    setup(sig="3", funder=FUNDER, balances={3: 0.0, 0: 12.5})
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "sig_type=0 sees 12.50 USDC" in out
    assert "set POLY_SIG_TYPE=0" in out


def test_rpc_failure_is_never_green(setup, capsys):
    def down(method, params):
        raise RuntimeError("all RPCs unreachable")
    setup(sig="0", funder=FUNDER, rpc=down, balances={0: 10.0})
    assert doctor.main([]) == 1
    assert "RPC" in capsys.readouterr().out


def test_garbage_sig_type_is_diagnosed_not_crashed(setup, capsys):
    setup(sig="three", balances={0: 10.0})
    assert doctor.main([]) == 1
    assert "is not" in capsys.readouterr().out


def test_unfunded_funder_is_a_clear_red(setup, capsys):
    """Issue-#87 shape: the funder was never deposited to. The verdict names
    the deposit, not a signature_type or association problem."""
    setup(sig="3", funder=FUNDER, rpc=rpc_mock(balance_usd=0.0), balances={})
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "funder holds no USDC on-chain" in out
    assert "deposit first, or wrong funder address" in out
    assert "not associated" not in out          # zero on-chain is NOT that case


def test_funds_onchain_but_no_sig_type_sees_them_names_association(setup, capsys):
    """pUSD sits at the funder but the CLOB reports 0 under every
    signature_type: the wallet was never associated with the backend."""
    setup(sig="3", funder=FUNDER, balances={})
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert ("funds on-chain but CLOB sees 0: wrong signature_type or "
            "wallet not associated") in out
    assert "never associated with the CLOB backend" in out
    assert "deploy_deposit_wallet" in out


def test_funded_and_clob_visible_is_green(setup, capsys):
    """Both sides positive: on-chain pUSD and CLOB collateral."""
    setup(sig="3", funder=FUNDER, balances={3: 87.65})
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "funder holds USDC on-chain: 50.00 pUSD" in out
    assert "87.65 USDC" in out


def test_owner_revert_is_warn_not_red(setup, capsys):
    """A funder whose owner() reverts (ERC-1167/ERC-1967 generations) must
    not fail as 'wrong key'; the CLOB collateral check stays the authority."""
    setup(sig="3", funder=FUNDER, rpc=rpc_mock(owner=None), balances={3: 87.65})
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "[??] funder wallet on-chain" in out
    assert "owner() not readable" in out
    assert "ERC-1167 minimal proxy, the deposit wallet shape" in out
    assert "NOT owned" not in out
    assert "matches the wallet type" not in out  # no sig advice without owner


def test_owner_revert_on_fat_contract_is_still_warn(setup, capsys):
    setup(sig="3", funder=FUNDER, rpc=rpc_mock(code="0x" + "ab" * 300, owner=None),
          balances={3: 5.0})
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "[??] funder wallet on-chain" in out
    assert "a full contract, not a minimal proxy" in out


def test_owner_someone_else_stays_red(setup, capsys):
    """Only a DIFFERENT owner() answer is a red; pinned next to the warn
    cases so the tri-state cannot regress."""
    setup(sig="3", funder=FUNDER, rpc=rpc_mock(owner=OTHER), balances={3: 5.0})
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "NOT owned" in out
    assert "[??] funder wallet on-chain" not in out


def test_api_key_line_names_the_eoa_and_the_400(setup, capsys):
    setup(sig="3", funder=FUNDER, balances={3: 87.65})
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert f"api key registered for EOA {EOA}" in out
    assert "400 from create is non fatal by construction" in out


def test_api_key_listing_failure_is_red(setup, capsys):
    setup(sig="3", funder=FUNDER, balances={3: 87.65})
    FakeExecutor.api_keys = RuntimeError("401 Unauthorized")
    assert doctor.main([]) == 1
    assert "api key listing (L2 auth)" in capsys.readouterr().out


def test_signer_note_printed_under_sig3_only(setup, capsys):
    setup(sig="3", funder=FUNDER, balances={3: 87.65})
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert 'signer must be the address of the API KEY' in out
    assert "never posts an order" in out
    setup(sig="0", balances={0: 10.0})
    assert doctor.main([]) == 0
    assert "signer must be the address" not in capsys.readouterr().out


def test_probe_skips_sig_types_whose_executor_fails(setup, capsys):
    """A signature type whose executor cannot even be built is skipped, and
    the probe still names the one that sees the funds."""
    setup(sig="0", funder=FUNDER, balances={3: 9.5})
    FakeExecutor.raise_for = frozenset({1, 2})
    assert doctor.main([]) == 1
    assert "but sig_type=3 sees 9.50 USDC" in capsys.readouterr().out


def test_executor_constructor_failure_is_a_clob_red(setup, capsys):
    setup(sig="3", funder=FUNDER, balances={3: 50.0})
    FakeExecutor.raise_for = frozenset({3})
    assert doctor.main([]) == 1
    assert "CLOB auth/collateral" in capsys.readouterr().out


def test_rpc_failure_without_funder_is_never_green_either(setup, capsys):
    """The self-funded (EOA) path now reads the on-chain balance too, and a
    dead RPC must fail the run, same as the funder-contract path."""
    def down(method, params):
        raise RuntimeError("all RPCs unreachable")
    setup(sig="0", rpc=down, balances={0: 10.0})
    assert doctor.main([]) == 1
    assert "RPC" in capsys.readouterr().out


GAMMA_MARKET = {"conditionId": "0xc0nd", "slug": "btc-updown-15m-1751550300",
                "outcomes": '["Up", "Down"]', "clobTokenIds": '["111", "222"]',
                "endDate": "2026-07-03T14:45:00Z", "outcomePrices": '["1", "0"]'}
BOOK = {"bids": [{"price": "0.94", "size": "10"}],
        "asks": [{"price": "0.96", "size": "20"}],
        "min_order_size": "5", "tick_size": "0.01"}


def test_market_section_reports_exchange_rules(setup, capsys, monkeypatch):
    setup(sig="0", balances={0: 10.0})
    monkeypatch.setattr(doctor, "get_market", lambda slug: GAMMA_MARKET)
    monkeypatch.setattr(doctor, "get_book", lambda token: BOOK)
    assert doctor.main(["--market", "btc-updown-15m-1751550300"]) == 0
    out = capsys.readouterr().out
    assert "min_order_size=5.0" in out
    assert "smallest possible order here: about 4.80$" in out


def test_unresolvable_market_fails_the_run(setup, capsys, monkeypatch):
    setup(sig="0", balances={0: 10.0})
    monkeypatch.setattr(doctor, "get_market", lambda slug: None)
    assert doctor.main(["--market", "nope"]) == 1
    assert "resolvable" in capsys.readouterr().out


def test_rpc_walks_the_endpoint_list_and_rejects_error_bodies(monkeypatch):
    """The real _rpc: a dead endpoint fails over to the next one; a JSON-RPC
    error body raises instead of being returned as a result."""
    import io
    import json as js

    calls = []

    def flaky_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise OSError("connect timeout")
        return io.BytesIO(js.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": "0x2a"}).encode())

    monkeypatch.setattr(doctor.urllib.request, "urlopen", flaky_urlopen)
    assert doctor._rpc("eth_getCode", ["0x0", "latest"]) == "0x2a"
    assert calls[0] != calls[1]                 # second endpoint answered

    def error_body(req, timeout=0):
        return io.BytesIO(js.dumps(
            {"jsonrpc": "2.0", "id": 1,
             "error": {"code": -32000, "message": "nope"}}).encode())

    monkeypatch.setattr(doctor.urllib.request, "urlopen", error_body)
    with pytest.raises(RuntimeError):
        doctor._rpc("eth_getCode", ["0x0", "latest"])


def test_check_surface_green_on_installed_client(capsys):
    """The installed py-clob-client-v2 matches the verified surface, so the
    executor would start clean. If this fails the client drifted and the
    introspection tables need re-verifying (invariant #2)."""
    assert doctor._check_surface() is True
    assert "matches the verified surface" in capsys.readouterr().out


def test_check_surface_reports_missing_method(monkeypatch, capsys):
    """A drifted client (get_order gone) is caught, with the shared
    IntrospectionMismatch wording 'method get_order missing', not the old
    per-parameter 'get_order.order_id' form. Pins the doctor side of the
    executor/doctor shared drift helper."""
    from py_clob_client_v2.client import ClobClient
    monkeypatch.setattr(ClobClient, "get_order", None)
    assert doctor._check_surface() is False
    out = capsys.readouterr().out
    assert "method get_order missing" in out
    assert "get_order.order_id" not in out


def test_check_surface_missing_client_is_fatal(monkeypatch):
    """No py-clob-client-v2 installed at all is fatal (None), not a drift."""
    import builtins
    real_import = builtins.__import__

    def no_client(name, *a, **k):
        if name.startswith("py_clob_client_v2"):
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_client)
    assert doctor._check_surface() is None
