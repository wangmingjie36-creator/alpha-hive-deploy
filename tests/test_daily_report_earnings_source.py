"""日报的财报日只来自本轮 ChronosBee 的按日观测，经 `earnings_calendar`，**任何情况下都不打网**（v0.45.500）。

沿革：v0.45.104 改从本轮 ChronosBee 催化剂取（此前 `earnings_vol_signal.scan` 每天对每只票真打 yfinance），
蜂没产出催化剂时再构造 `EarningsWatcher` 兜底。2026-10-13 调查
（`experiments/earnings_date_cache_staleness_report.md`）：那条兜底与主路径同源（yfinance calendar），
主路径失败的那几天期权取数也一起挂了，兜底一次都没被调到；唯一一次被调到是 ChronosBee 把**当天**的财报丢了。
现在判不了就是判不了（None ⇒ 卖权 unknown），不再打网。逐情形的语义在 `tests/test_earnings_calendar.py`。

本文件只钉日报这一侧的接线：
  1. `_earnings_date_from_swarm` 委托给 `earnings_calendar`，蜂失败 / 票缺失时也**绝不**构造 EarningsWatcher；
  2. 它用的是调用方给的 as_of（当天的财报算「当天」），不是 `datetime.now()`；
  3. `_post_scan_notify` 里两处钩子（跨式 `scan`、卖权 `run_for_date`）都传了 `upcoming_fn`，且带上 `self.date_str`。
"""

import ast
import inspect
import sys
import types

import pytest

import alpha_hive_daily_report as ahdr
import earnings_calendar as EC

MAKE = ahdr.AlphaHiveDailyReporter._earnings_date_from_swarm
AS_OF = "2026-10-13"


def _entry(dates=None, *, error=None, legacy=None):
    if error:
        return {"agent_details": {"ChronosBeeHorizon": {"details": {}, "error": error}}}
    det = {}
    if dates is not None:
        det[EC.FIELD] = {"schema": EC.SCHEMA, "available": True, "dates": dates, "fetched_at": "2026-10-13T14:00:00"}
    if legacy is not None:
        det["catalysts"] = legacy
    return {"agent_details": {"ChronosBeeHorizon": {"details": det}}}


@pytest.fixture
def no_watcher(monkeypatch):
    """EarningsWatcher 一被构造就红：日报这一侧不许再打 yfinance 取财报日。"""
    built = []

    class _Boom:
        def __init__(self):
            built.append(1)
            raise AssertionError("日报不许构造 EarningsWatcher（v0.45.500 起财报日只经 earnings_calendar）")

    monkeypatch.setitem(sys.modules, "earnings_watcher", types.SimpleNamespace(EarningsWatcher=_Boom))
    return built


class TestHookSide:

    def test_dated_observation_is_used(self, no_watcher):
        fn = MAKE({"COST": _entry(["2026-12-10"])}, AS_OF)
        got = fn("COST")
        assert got["earnings_date"] == "2026-12-10" and got["source"] == EC.SOURCE_CALENDAR
        assert got["observed_on"] == AS_OF

    def test_earnings_today_counts_as_today(self, no_watcher):
        """ChronosBee 的分数口径把当天丢掉（`datetime.now()` 得 −1），读取端按 as_of 做日期算术不丢。
        变异：resolve 用 `d > as_of` ⇒ 返回下一季或 None。"""
        fn = MAKE({"JNJ": _entry(["2026-10-13", "2027-01-20"])}, AS_OF)
        assert fn("JNJ")["earnings_date"] == AS_OF

    @pytest.mark.parametrize("swarm", [
        {"WMT": _entry(error="catalyst_sources_unavailable")},     # 蜂失败
        {"WMT": {"agent_details": {}}},                             # 蜂没跑
        {},                                                         # 票缺失
    ])
    def test_cannot_judge_returns_none_and_never_hits_network(self, no_watcher, swarm):
        assert MAKE(swarm, AS_OF)("WMT") is None
        assert no_watcher == [], "判不了不许打网兜底（与主路径同源，需要它的那天它也够不着）"

    def test_as_of_comes_from_caller(self, no_watcher):
        """同一份观测，as_of 不同结果不同——证明用的是参数不是墙钟。"""
        sw = {"MU": _entry(["2026-09-30"])}
        assert MAKE(sw, "2026-09-30")("MU")["earnings_date"] == "2026-09-30"
        later = MAKE(sw, "2026-10-01")("MU")
        assert later["earnings_date"] is None and later["reason"] == "only_past_dates"


class TestHooksPassTheInjectedSource:

    @staticmethod
    def _calls(attr):
        src = inspect.getsource(ahdr.AlphaHiveDailyReporter._post_scan_notify)
        tree = ast.parse(src.lstrip())
        return [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]

    @pytest.mark.parametrize("attr", ["scan", "run_for_date"])
    def test_hook_passes_upcoming_fn_built_for_the_scan_date(self, attr):
        """两处钩子都必须传 `upcoming_fn=self._earnings_date_from_swarm(swarm_results, self.date_str)`。
        变异：去掉 upcoming_fn（退回读盘缺省，扫描当时文件还没写完）/ as_of 写成别的。"""
        calls = [c for c in self._calls(attr) if any(k.arg == "upcoming_fn" for k in c.keywords)]
        assert calls, f"_post_scan_notify 里的 {attr}() 没收到 upcoming_fn"
        for c in calls:
            kw = next(k for k in c.keywords if k.arg == "upcoming_fn").value
            assert isinstance(kw, ast.Call) and getattr(kw.func, "attr", None) == "_earnings_date_from_swarm"
            assert [ast.unparse(a) for a in kw.args] == ["swarm_results", "self.date_str"], ast.unparse(kw)

    def test_probe_sees_the_real_calls(self):
        """自证：探针在真源码上看得见两处调用（方法改名 / 源码取不到时上面那条会对空集恒绿）。"""
        assert self._calls("scan") and self._calls("run_for_date")
