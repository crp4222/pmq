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
    """CLOB stand-in: collateral visible only under the right sig_type."""
    balances = {}

    def __init__(self, signature_type=None, **kw):
        if signature_type is None:
            try:
                signature_type = int(os.environ.get("POLY_SIG_TYPE", "0"))
            except ValueError:
                signature_type = -1
        self.st = signature_type

    def collateral(self):
        return type(self).balances.get(self.st, 0.0)


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
        monkeypatch.setattr("pmq.executor.PolymarketExecutor", FakeExecutor)
    return _setup


def test_advise_sig_type_matrix():
    assert doctor.advise_sig_type(False, False, True)[0] == 0
    assert doctor.advise_sig_type(True, True, False)[0] == 3
    assert doctor.advise_sig_type(True, False, False)[0] is None
    assert doctor.advise_sig_type(False, False, False)[0] is None


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
