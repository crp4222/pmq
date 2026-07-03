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


GAMMA_MARKET = {"conditionId": "0xc0nd", "slug": "btc-updown-15m-1",
                "outcomes": '["Up", "Down"]', "clobTokenIds": '["111", "222"]',
                "outcomePrices": '["0.6", "0.4"]'}


def test_find_markets_search_and_default(monkeypatch):
    m = load_mcp(monkeypatch)
    ev = {"title": "BTC 15m", "volume24hr": 1000.0, "markets": [GAMMA_MARKET]}
    monkeypatch.setattr(m.data, "http_get_json",
                        lambda url, logger=None: {"events": [ev]} if "public-search" in url else [ev])
    hit = m.find_markets(query="btc")[0]
    assert hit["market_slug"] == "btc-updown-15m-1" and hit["outcomes"] == ["Up", "Down"]
    assert m.find_markets()[0]["event"] == "BTC 15m"
    monkeypatch.setattr(m.data, "http_get_json", lambda url, logger=None: None)
    assert "error" in m.find_markets(query="void")[0]


def test_market_and_event_tools(monkeypatch):
    m = load_mcp(monkeypatch)
    monkeypatch.setattr(m.data, "get_market", lambda slug, logger=None: GAMMA_MARKET)
    out = m.market(slug="btc-updown-15m-1")
    assert out["condition_id"] == "0xc0nd" and out["outcomes"]["Up"] == "111"
    monkeypatch.setattr(m.data, "get_market", lambda slug, logger=None: None)
    assert "error" in m.market(slug="nope")
    monkeypatch.setattr(m.data, "http_get_json",
                        lambda url, logger=None: [{"markets": [GAMMA_MARKET]}])
    assert m.event(slug="ev")[0]["market_slug"] == "btc-updown-15m-1"
    monkeypatch.setattr(m.data, "http_get_json", lambda url, logger=None: None)
    assert "error" in m.event(slug="nope")[0]


def test_book_tool_summarizes_live_book(monkeypatch):
    m = load_mcp(monkeypatch)
    book = {"bids": [{"price": "0.94", "size": "10"}],
            "asks": [{"price": "0.96", "size": "20"}],
            "min_order_size": "5", "tick_size": "0.01"}
    monkeypatch.setattr(m.data, "get_book", lambda token, logger=None: book)
    out = m.book(token_id="111", depth_lo=0.9, depth_hi=0.97)
    assert out["bid"] == 0.94 and out["ask"] == 0.96
    assert out["ask_depth_usd_in_range"] == 19.2 and out["min_order_size"] == 5.0
    monkeypatch.setattr(m.data, "get_book", lambda token, logger=None: None)
    assert "error" in m.book(token_id="111")


class FakeExecutor:
    def __init__(self, fill=None, uncertain=False, trades=(2.0, 1.9, 0.01)):
        self.fill, self.uncertain, self.trades = fill, uncertain, trades

    def collateral(self):
        return 39.42

    def buy_fak(self, token_id, price_cap, usd):
        from pmq.exceptions import OrderUncertain
        if self.uncertain:
            raise OrderUncertain("502")
        return self.fill

    sell_fak = buy_fak

    def trades_totals(self, condition_id, token_id=None):
        return self.trades

    def reconcile(self, condition_id, token_id=None):
        return self.trades


def test_account_tools_report_executor_truth(monkeypatch):
    m = load_mcp(monkeypatch)
    m._executor = FakeExecutor()
    assert m.account_collateral() == {"collateral_usd": 39.42}
    out = m.account_trades(condition_id="0xc")
    assert (out["shares"], out["usd"]) == (2.0, 1.9)
    m._executor = FakeExecutor(trades=None)
    assert "error" in m.account_trades(condition_id="0xc")


def test_live_tools_book_only_confirmed_fills(monkeypatch):
    from pmq.executor import Fill
    m = load_mcp(monkeypatch, live=True, max_usd=10)
    m._executor = FakeExecutor(fill=Fill(order_id="0x1", matched_shares=5.1,
                                         matched_usd=4.9))
    out = m.fak_buy(token_id="111", price_cap=0.97, usd=5.0)
    assert out["booked"] and out["matched_shares"] == 5.1
    out = m.fak_sell(token_id="111", price_floor=0.95, shares=5.1)
    assert out["booked"]
    m._executor = FakeExecutor(uncertain=True)
    assert "outcome unknown" in m.fak_buy(token_id="111", price_cap=0.97,
                                          usd=5.0)["error"]
    m._executor = FakeExecutor()
    out = m.cancel_and_reconcile(condition_id="0xc")
    assert out["cancelled"] and out["shares"] == 2.0
    m._executor = FakeExecutor(trades=None)
    assert "error" in m.cancel_and_reconcile(condition_id="0xc")
