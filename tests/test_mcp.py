import importlib

import pytest

pytest.importorskip("mcp")


def load_mcp(monkeypatch, live=False, max_usd=None, daily_usd=None):
    monkeypatch.delenv("PMQ_MCP_LIVE", raising=False)
    monkeypatch.delenv("PMQ_MCP_MAX_USD", raising=False)
    monkeypatch.delenv("PMQ_MCP_DAILY_USD", raising=False)
    if live:
        monkeypatch.setenv("PMQ_MCP_LIVE", "1")
    if max_usd is not None:
        monkeypatch.setenv("PMQ_MCP_MAX_USD", str(max_usd))
    if daily_usd is not None:
        monkeypatch.setenv("PMQ_MCP_DAILY_USD", str(daily_usd))
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
        return 87.65

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
    assert m.account_collateral() == {"collateral_usd": 87.65}
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
    assert "outcome unknown" in m.fak_sell(token_id="111", price_floor=0.95,
                                           shares=5.0)["error"]
    m._executor = FakeExecutor()
    out = m.cancel_and_reconcile(condition_id="0xc")
    assert out["cancelled"] and out["shares"] == 2.0
    m._executor = FakeExecutor(trades=None)
    assert "error" in m.cancel_and_reconcile(condition_id="0xc")


def test_daily_budget_blocks_and_counts_only_real_spend(monkeypatch):
    from pmq.executor import Fill
    m = load_mcp(monkeypatch, live=True, max_usd=10, daily_usd=8)
    m._executor = FakeExecutor(fill=Fill(order_id="0x1", matched_shares=5.1,
                                         matched_usd=4.9))
    assert m.fak_buy(token_id="t", price_cap=0.97, usd=5.0)["booked"]
    out = m.fak_buy(token_id="t", price_cap=0.97, usd=5.0)
    assert "daily buy budget" in out.get("error", "")      # 4.9 + 5 > 8
    # 3.05 fits ONLY if the confirmed 4.9 was counted (left 3.1); counting
    # the requested 5.0 would leave 3.0 and refuse this order.
    assert m.fak_buy(token_id="t", price_cap=0.97, usd=3.05)["booked"]


def test_daily_budget_rejected_orders_cost_nothing(monkeypatch):
    from pmq.executor import Fill
    m = load_mcp(monkeypatch, live=True, max_usd=10, daily_usd=6)
    m._executor = FakeExecutor(fill=Fill(rejected=True, error="no match"))
    for _ in range(4):                       # rejections never eat the budget
        assert not m.fak_buy(token_id="t", price_cap=0.97, usd=5.0)["booked"]
    m._executor = FakeExecutor(fill=Fill(order_id="0x1", matched_shares=5.1,
                                         matched_usd=4.9))
    assert m.fak_buy(token_id="t", price_cap=0.97, usd=5.0)["booked"]


def test_daily_budget_uncertain_consumes_requested(monkeypatch):
    m = load_mcp(monkeypatch, live=True, max_usd=10, daily_usd=8)
    m._executor = FakeExecutor(uncertain=True)
    assert "outcome unknown" in m.fak_buy(token_id="t", price_cap=0.97,
                                          usd=5.0)["error"]
    out = m.fak_buy(token_id="t", price_cap=0.97, usd=5.0)
    assert "daily buy budget" in out.get("error", "")

    m2 = load_mcp(monkeypatch, live=True, max_usd=10)   # cap absent = illimite
    m2._executor = FakeExecutor(uncertain=True)
    for _ in range(3):
        assert "outcome unknown" in m2.fak_buy(token_id="t", price_cap=0.97,
                                               usd=9.0)["error"]


def load_paper(monkeypatch, paper_usd=None, **kw):
    monkeypatch.setenv("PMQ_MCP_PAPER", "1")
    if paper_usd is not None:
        monkeypatch.setenv("PMQ_MCP_PAPER_USD", str(paper_usd))
    else:
        monkeypatch.delenv("PMQ_MCP_PAPER_USD", raising=False)
    return load_mcp(monkeypatch, **kw)


BOOK = {"bids": [{"price": "0.55", "size": "40"}],
        "asks": [{"price": "0.60", "size": "30"}],
        "min_order_size": "5", "tick_size": "0.01"}


def test_paper_mode_needs_no_keys_and_wins_over_live(monkeypatch):
    m = load_paper(monkeypatch, live=True)          # both set: paper wins
    assert m.PAPER_ENABLED and not m.LIVE_ENABLED
    monkeypatch.setattr(m.data, "get_book", lambda t, logger=None: dict(BOOK))
    out = m.fak_buy(token_id="111", price_cap=0.62, usd=6.0)
    assert out["booked"] and out["paper"] and out["price"] == 0.60
    assert m._executor is None                       # no executor ever built


def test_paper_fills_at_real_ask_and_respects_min_and_cap(monkeypatch):
    m = load_paper(monkeypatch)
    monkeypatch.setattr(m.data, "get_book", lambda t, logger=None: dict(BOOK))
    assert m.fak_buy(token_id="1", price_cap=0.55, usd=6.0)["rejected"]
    out = m.fak_buy(token_id="1", price_cap=0.62, usd=2.0)   # 3.33sh < min 5
    assert out["rejected"] and "minimum" in out["error"]
    ok = m.fak_buy(token_id="1", price_cap=0.62, usd=6.0)
    assert ok["booked"] and abs(ok["matched_shares"] - 10.0) < 0.01
    assert ok["cash_left"] < 1000 - 5.99


def test_paper_sell_needs_position_and_updates_cash(monkeypatch):
    m = load_paper(monkeypatch, paper_usd=100)
    monkeypatch.setattr(m.data, "get_book", lambda t, logger=None: dict(BOOK))
    assert m.fak_sell(token_id="1", price_floor=0.5, shares=5.0)["rejected"]
    m.fak_buy(token_id="1", price_cap=0.62, usd=6.0)
    out = m.fak_sell(token_id="1", price_floor=0.5, shares=10.0)
    assert out["booked"] and out["price"] == 0.55
    assert m.account_collateral()["paper"]
    tot = m.account_trades(condition_id="0xc", token_id="1")
    assert tot["paper"] and abs(tot["shares"]) < 0.01   # flat after round trip


def test_paper_cancel_and_reconcile_never_builds_an_executor(monkeypatch):
    m = load_paper(monkeypatch)
    out = m.cancel_and_reconcile(condition_id="0xc")
    assert out["cancelled"] and out["paper"]
    assert m._executor is None                  # the exchange stays out of reach


def test_paper_sell_fails_closed_on_missing_book_or_bid(monkeypatch):
    m = load_paper(monkeypatch)
    monkeypatch.setattr(m.data, "get_book", lambda t, logger=None: dict(BOOK))
    m.fak_buy(token_id="1", price_cap=0.62, usd=6.0)
    monkeypatch.setattr(m.data, "get_book", lambda t, logger=None: None)
    assert "error" in m.fak_sell(token_id="1", price_floor=0.5, shares=5.0)
    monkeypatch.setattr(m.data, "get_book",
                        lambda t, logger=None: {"bids": [], "asks": []})
    out = m.fak_sell(token_id="1", price_floor=0.5, shares=5.0)
    assert out["rejected"] and "no bid" in out["error"]
