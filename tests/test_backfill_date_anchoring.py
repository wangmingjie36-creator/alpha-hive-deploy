"""
`--date` 补跑时价格必须锚定目标交易日（v0.43.28 回归）

v0.41.6 已建好历史通道：`fetch_stock_data(t, as_of_date=...)` →
`_fetch_historical_stock_data`，返回该日真实收盘价（`source_name="yfinance_historical"`）。
建它的起因就是 2026-07-21 补跑时"网站上的价格不是当日收盘价"。

但通道建好了不等于处处接通——本次查出 **3 条旁路**在补跑时仍取今天的实时价：

| 位置 | 问题 |
|---|---|
| `swarm_agents/base.py` `_get_stock_data` | prefetch 落空分支不传 date |
| `alpha_hive_daily_report._generate_ml_reports` | `_fsd(ticker)` 不传 as_of_date |
| `alpha_hive_daily_report._analyze_ticker_safe` | `_dr_fetch_stock(ticker)` 不传 date |

危险在于**静默**：同一份报告里，prefetch 命中的标的是历史价、落空的是实时价，
两种口径混在一起，没有任何报错。
"""

import inspect

import pytest


def _concrete_agent():
    """BeeAgent 是抽象类（analyze 未实现），造个最小具体实例来测基类方法"""
    import swarm_agents.base as base

    class _Concrete(base.BeeAgent):
        def analyze(self, ticker):  # pragma: no cover - 仅为满足抽象接口
            return {}

    return _Concrete.__new__(_Concrete)


class TestPrefetchCarriesDate:
    def test_prefetch_returns_target_date(self):
        """inject 要能拿到日期，prefetch 就必须把它带出来"""
        import swarm_agents.base as base
        src = inspect.getsource(base.prefetch_shared_data)
        assert '"target_date": target_date' in src

    def test_inject_sets_target_date_on_agents(self):
        import swarm_agents.base as base

        class _A:
            pass

        a = _A()
        base.inject_prefetched([a], {"stock_data": {}, "contexts": {},
                                     "target_date": "2026-08-24"})
        assert a._target_date == "2026-08-24"


class TestMissBranchPassesDate:
    def test_get_stock_data_miss_uses_target_date(self, monkeypatch):
        """prefetch 落空时也必须走历史通道，否则同一份报告两种价格口径"""
        import swarm_agents.base as base

        seen = {}

        def _fake(ticker, target_date=None):
            seen["ticker"] = ticker
            seen["date"] = target_date
            return {"price": 208.48}

        monkeypatch.setattr(base._cache, "_fetch_stock_data", _fake)

        agent = _concrete_agent()
        agent._prefetched_stock = {}          # 强制 miss
        agent._target_date = "2026-08-24"
        agent._get_stock_data("NVDA")

        assert seen["date"] == "2026-08-24", "落空分支丢了目标日期 → 会取到今天的实时价"

    def test_no_target_date_attribute_is_safe(self, monkeypatch):
        """老对象没有 _target_date 属性时不能崩（getattr 默认 None = 实时口径）"""
        import swarm_agents.base as base

        monkeypatch.setattr(base._cache, "_fetch_stock_data",
                            lambda t, d=None: {"price": 100.0})
        agent = _concrete_agent()
        agent._prefetched_stock = {}
        agent._get_stock_data("NVDA")  # 不应抛 AttributeError


class TestDailyReportSitesAnchored:
    @pytest.mark.parametrize("method,needle", [
        ("_generate_ml_reports", "as_of_date=self.date_str"),
        ("_analyze_ticker_safe", "_dr_fetch_stock(ticker, self.date_str)"),
    ])
    def test_site_passes_report_date(self, method, needle):
        from alpha_hive_daily_report import AlphaHiveDailyReporter
        src = inspect.getsource(getattr(AlphaHiveDailyReporter, method))
        assert needle in src, f"{method} 未锚定报告日期，补跑时会取今天实时价"


class TestHistoricalPathContract:
    def test_historical_branch_marks_its_source(self):
        """历史价必须自带来源标记，读数时才能分辨口径
        （同 vix_source / iv_rank_source / momentum_source 的思路）"""
        import data_pipeline
        src = inspect.getsource(data_pipeline._fetch_historical_stock_data)
        assert "yfinance_historical" in src
        assert '_as_of_date' in src
