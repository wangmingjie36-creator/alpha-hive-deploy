"""财报日必须复用本轮扫描已有的 ChronosBee 催化剂，不再重打 yfinance（v0.45.104）。

二次复查实测的问题：`earnings_vol_signal.scan` 对每只通过报价预检的票各调一次
`EarningsWatcher.get_earnings_date`（内部走 `stock.calendar`），而 ChronosBee 在
**同一轮扫描里**早就取过同一个数据（2026-09-02 的 `.swarm_results` 里 28/30 只票
带 `type == "earnings"` 的催化剂）。`earnings_cache/*_date.json` 的 TTL 是 12 小时、
扫描每天跑一次，所以那些调用每天都是真打网——实测该目录 24 个文件最新停在 8/9，
当天一个都没刷新。yfinance 429 是本项目头号数据丢失原因。

本测试钉住三件事：
  1. 蜂里有财报日 → 用它，**绝不**构造 EarningsWatcher；
  2. 蜂里有催化剂但没有财报条目 → 视野内确实没有财报，返回 None 且仍不打网；
  3. 蜂**完全没跑出催化剂** → 必须自己兜底打一次网（不能静默当成「没有财报」）。

第 3 条是重点：`scan` 的 `upcoming_fn = upcoming_fn or _upcoming` 只在**参数**为
None 时替换整个函数，不会因为某只票返回 None 就回退。兜底若不写在注入函数自己
里就不存在——本文件第一版注释正是这么写错的。
"""

import sys
import types

import pytest

import alpha_hive_daily_report as ahdr

MAKE = ahdr.AlphaHiveDailyReporter._earnings_date_from_swarm


def _swarm(catalysts):
    """{ticker: catalysts | None} → swarm_results 形状。None = 蜂没产出。"""
    out = {}
    for tk, cats in catalysts.items():
        details = {} if cats is None else {"catalysts": cats}
        out[tk] = {"agent_details": {"ChronosBeeHorizon": {"details": details}}}
    return out


@pytest.fixture
def watcher_spy(monkeypatch):
    """把 EarningsWatcher 换成计数假货；构造次数与调用参数都记下来。"""
    calls = {"built": 0, "asked": []}

    class _Fake:
        def __init__(self):
            calls["built"] += 1

        def get_earnings_date(self, ticker):
            calls["asked"].append(ticker)
            return {"earnings_date": "2026-11-11", "earnings_time": "AMC",
                    "source": "yfinance"}

    monkeypatch.setitem(sys.modules, "earnings_watcher",
                        types.SimpleNamespace(EarningsWatcher=_Fake))
    return calls


class TestEarningsDateComesFromSwarm:

    def test_uses_chronos_catalyst_and_never_builds_a_watcher(self, watcher_spy):
        fn = MAKE(_swarm({"COST": [
            {"type": "dividend", "date": "2026-09-23"},
            {"type": "earnings", "date": "2026-09-24", "days_until": 21},
        ]}))
        got = fn("COST")
        assert got["earnings_date"] == "2026-09-24"
        assert got["source"] == "chronos_bee_catalyst"
        assert watcher_spy["built"] == 0, "蜂里已有财报日，不得再打 yfinance"

    def test_catalysts_without_earnings_means_no_earnings_and_no_network(self, watcher_spy):
        fn = MAKE(_swarm({"KO": [{"type": "dividend", "date": "2026-09-23"}]}))
        got = fn("KO")
        assert got["earnings_date"] is None
        assert watcher_spy["built"] == 0, "视野内没有财报是结论，不是取数失败"

    def test_agent_failure_falls_back_to_the_watcher(self, watcher_spy):
        """蜂完全没产出催化剂（9/2 的 WMT/SNOW）必须兜底，不能静默当成没有财报。"""
        fn = MAKE(_swarm({"WMT": None}))
        got = fn("WMT")
        assert got is not None and got["earnings_date"] == "2026-11-11"
        assert watcher_spy["built"] == 1 and watcher_spy["asked"] == ["WMT"]

    def test_watcher_is_built_at_most_once_across_tickers(self, watcher_spy):
        fn = MAKE(_swarm({"WMT": None, "SNOW": None, "MU": None}))
        for tk in ("WMT", "SNOW", "MU"):
            fn(tk)
        assert watcher_spy["built"] == 1, "兜底要延迟构造并复用，不能每票新建"
        assert watcher_spy["asked"] == ["WMT", "SNOW", "MU"]

    def test_fallback_failure_is_swallowed(self, monkeypatch):
        class _Boom:
            def __init__(self):
                raise RuntimeError("yfinance down")

        monkeypatch.setitem(sys.modules, "earnings_watcher",
                            types.SimpleNamespace(EarningsWatcher=_Boom))
        fn = MAKE(_swarm({"WMT": None}))
        assert fn("WMT") is None, "兜底失败按「没有财报日」处理，不得抛穿主流程"

    def test_missing_ticker_is_treated_as_agent_failure(self, watcher_spy):
        """扫描结果里根本没有这只票 → 当作蜂失败兜底，而不是当作没有财报。"""
        fn = MAKE(_swarm({"COST": [{"type": "earnings", "date": "2026-09-24"}]}))
        assert fn("NVDA")["earnings_date"] == "2026-11-11"
        assert watcher_spy["built"] == 1


class TestScanIsCalledWithTheInjectedSource:

    def test_hook_passes_upcoming_fn(self):
        """日报钩子必须把注入函数传给 scan——否则上面这些语义一条都用不上。"""
        import ast
        import inspect
        src = inspect.getsource(ahdr.AlphaHiveDailyReporter._post_scan_notify)
        tree = ast.parse(src.lstrip())
        found = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "scan"):
                kwargs = {k.arg for k in node.keywords}
                assert "upcoming_fn" in kwargs, "scan() 没有收到 upcoming_fn，仍会自己打网"
                found = True
        assert found, "_post_scan_notify 里找不到 scan() 调用"
