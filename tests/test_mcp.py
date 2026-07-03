import importlib

import pytest

pytest.importorskip("mcp")


def load_mcp(monkeypatch, live=False, max_usd=None):
    monkeypatch.delenv("PMQ_MCP_LIVE", raising=False)
    monkeypatch.delenv("PMQ_MCP_MAX_USD", raising=False)
    if live:
        monkeypatch.setenv("PMQ_MCP_LIVE", "1")
    if max_usd is not None:
        monkeypatch.setenv("PMQ_MCP_MAX_USD", str(max_usd))
    import pmq.mcp
    return importlib.reload(pmq.mcp)


def test_trading_tools_do_not_exist_without_operator_optin(monkeypatch):
    m = load_mcp(monkeypatch, live=False)
    assert not hasattr(m, "fak_buy")
    assert not hasattr(m, "fak_sell")
    assert not hasattr(m, "cancel_and_reconcile")
    assert hasattr(m, "book") and hasattr(m, "market")


def test_trading_tools_exist_with_optin_and_enforce_cap(monkeypatch):
    m = load_mcp(monkeypatch, live=True, max_usd=10)
    out = m.fak_buy(token_id="tok", price_cap=0.95, usd=11.0)
    assert "refused" in out.get("error", "")


def test_taker_fee_tool_matches_formula(monkeypatch):
    m = load_mcp(monkeypatch)
    out = m.taker_fee(price=0.5, shares=100, category="crypto")
    assert abs(out["fee_usd"] - 1.75) < 1e-9
    assert out["rate"] == 0.07
    assert "error" in m.taker_fee(price=0.5, shares=1, category="astrology")
