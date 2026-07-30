"""
ParallelAgentRunner 挂死防护测试（v0.42.8）

固化一次真实事故：2026-05-28 一次 10 标的扫描跑了 **24.1 小时**
（metrics.db 记录 duration_seconds=86899，而 prefetch 仅 17s），
另有 06-27(0.8h)、06-30(2.2h)、07-17(3.1h) 三次。

根因：`with ThreadPoolExecutor(...)` 退出时执行 `shutdown(wait=True)`，
会一直阻塞到所有工作线程返回。而 `future.result(timeout=)` /
`as_completed(timeout=)` 只让主线程**停止等待**，并不能杀死卡在网络调用里的
工作线程（Python 无法强制中断线程）。于是超时逻辑正确地填了默认值，
却在退出 with 块时被无限期挂住。

这些测试确保修复不被改回去。
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parallel_agent_runner import ParallelAgentRunner


class _FastAgent:
    def analyze(self, ticker):
        return {"score": 7.0, "direction": "bullish", "confidence": 0.8,
                "discovery": "ok", "source": "Fast", "dimension": "signal"}


class _StuckAgent:
    """模拟挂死的网络调用（无超时的 socket 读）

    用 Event.wait 而非 time.sleep：测试结束时可由 fixture 释放，
    否则这些线程会残留到 pytest 进程退出，被 atexit join 住——
    正是本文件要防的那个问题，测试自身不能再犯一遍。
    """

    _live: list = []

    def __init__(self, seconds=60.0):
        self.seconds = seconds
        self.started = threading.Event()
        self._release = threading.Event()
        _StuckAgent._live.append(self)

    def analyze(self, ticker):
        self.started.set()
        self._release.wait(timeout=self.seconds)
        return {"score": 5.0, "direction": "neutral", "confidence": 0.5,
                "discovery": "late", "source": "Stuck", "dimension": "odds"}

    @classmethod
    def release_all(cls):
        for a in cls._live:
            a._release.set()
        cls._live.clear()


@pytest.fixture(autouse=True)
def _release_stuck_agents():
    """每个用例结束后释放卡住的线程，避免残留到 pytest 退出"""
    yield
    _StuckAgent.release_all()
    time.sleep(0.05)


class TestStuckAgentDoesNotBlockScan:

    def test_run_phase_returns_promptly_despite_stuck_agent(self):
        """核心回归：一个卡 60s 的 Agent 不得把整个 phase 拖住。

        修复前：`with` 块退出时 join 卡住的线程 → 本用例会跑满 60s。
        修复后：shutdown(wait=False) 立即返回 → 应在 timeout 量级内结束。
        """
        stuck = _StuckAgent(seconds=60.0)
        runner = ParallelAgentRunner(timeout_per_agent=2.0)

        t0 = time.time()
        results = runner._run_phase(
            1, "TEST", {"Fast": _FastAgent(), "Stuck": stuck}, max_workers=2)
        elapsed = time.time() - t0

        assert stuck.started.wait(timeout=5), "卡住的 Agent 未被真正调度"
        assert elapsed < 20, f"主流程被卡死线程阻塞了 {elapsed:.0f}s（修复前为 60s）"
        assert len(results) == 2, "超时的 Agent 也必须返回占位结果"

    def test_stuck_agent_gets_placeholder_result(self):
        """超时的 Agent 必须产出结构完整的占位结果，不能让下游拿到 None"""
        runner = ParallelAgentRunner(timeout_per_agent=1.0)
        results = runner._run_phase(
            1, "TEST", {"Stuck": _StuckAgent(seconds=30.0)}, max_workers=1)

        assert len(results) == 1
        r = results[0]
        for field in ("score", "direction", "confidence", "source", "dimension"):
            assert field in r, f"占位结果缺字段 {field}"
        assert r.get("error"), "占位结果应带 error 标记"
        assert r["direction"] == "neutral"

    def test_all_agents_stuck_still_returns(self):
        """全部 Agent 卡住时也必须返回，而不是永久挂起"""
        runner = ParallelAgentRunner(timeout_per_agent=1.0)
        t0 = time.time()
        results = runner._run_phase(
            1, "TEST",
            {"A": _StuckAgent(seconds=30.0), "B": _StuckAgent(seconds=30.0)},
            max_workers=2)
        elapsed = time.time() - t0

        assert elapsed < 20, f"全卡住时被阻塞 {elapsed:.0f}s"
        assert len(results) == 2

    def test_normal_agents_unaffected(self):
        """没有卡死时行为不变（修复不得引入回归）"""
        runner = ParallelAgentRunner(timeout_per_agent=5.0)
        results = runner._run_phase(
            1, "TEST", {"A": _FastAgent(), "B": _FastAgent()}, max_workers=2)
        assert len(results) == 2
        assert all(r["score"] == 7.0 for r in results)
        assert not any(r.get("error") for r in results)


class TestSourceGuardAgainstRegression:
    """源码级护栏：防止有人把 `with ThreadPoolExecutor` 改回来"""

    def _source(self, code_only=False):
        import parallel_agent_runner as m
        with open(m.__file__, encoding="utf-8") as f:
            lines = f.read().splitlines()
        if code_only:
            # 只看代码行——解释该 bug 的注释里必然出现这个字符串
            lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
        return "\n".join(lines)

    def test_does_not_use_with_threadpoolexecutor(self):
        src = self._source(code_only=True)
        assert "with ThreadPoolExecutor(" not in src, (
            "禁止用 `with ThreadPoolExecutor(...)` —— 退出时 shutdown(wait=True) "
            "会阻塞到卡死线程返回（实测造成 24 小时挂起）。"
            "请用手动 executor + try/finally + shutdown(wait=False, cancel_futures=True)"
        )

    def test_shutdown_is_non_blocking(self):
        src = self._source()
        assert "shutdown(wait=False" in src, "必须以 wait=False 关闭线程池"
        assert "cancel_futures=True" in src, "应取消尚未开始的任务以释放网络配额"


class TestForceExitSafetyNet:
    """`_force_exit_if_threads_stuck` —— 退出路径的兜底

    即便 shutdown(wait=False) 已让主流程继续，`concurrent.futures` 注册的
    atexit 钩子仍会 join 工作线程，导致进程在**所有产出落盘之后**静默挂住。
    """

    def test_returns_immediately_when_no_lingering_threads(self):
        import alpha_hive_daily_report as m
        t0 = time.time()
        m._force_exit_if_threads_stuck(grace_seconds=5.0)
        assert time.time() - t0 < 1.0, "无卡住线程时不应等待"

    def test_function_exists_and_is_wired(self):
        """必须在 __main__ 里被调用，否则安全网形同虚设"""
        import alpha_hive_daily_report as m
        assert callable(getattr(m, "_force_exit_if_threads_stuck", None))
        with open(m.__file__, encoding="utf-8") as f:
            src = f.read()
        tail = src[src.rindex('if __name__ == "__main__":'):]
        assert "_force_exit_if_threads_stuck()" in tail, \
            "_force_exit_if_threads_stuck 未在 __main__ 中调用"

    def test_daemon_threads_are_ignored(self):
        """守护线程不阻止进程退出，不该被计入"""
        import alpha_hive_daily_report as m
        stop = threading.Event()
        d = threading.Thread(target=stop.wait, daemon=True, name="daemon-probe")
        d.start()
        try:
            t0 = time.time()
            m._force_exit_if_threads_stuck(grace_seconds=3.0)
            assert time.time() - t0 < 1.0, "守护线程不应触发等待"
        finally:
            stop.set()
            d.join(timeout=2)


class TestMLReportThrottle:
    """v0.42.9：ML 报告限流 + 第二处挂死模式

    标的池 10 → 30 后，若仍为每只标的生成 ML 报告，CBOE/yfinance 取价调用量
    直接翻 3 倍 —— 2026-07-23 深夜限流连锁崩溃（10/10 标的 ML 报告在
    Step 2 与 Step 3 同时失败）正是调用量堆叠所致。
    """

    def _source(self, code_only=False):
        import alpha_hive_daily_report as m
        with open(m.__file__, encoding="utf-8") as f:
            lines = f.read().splitlines()
        if code_only:
            lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
        return "\n".join(lines)

    def test_ml_report_generation_is_capped(self):
        src = self._source(code_only=True)
        assert "ALPHA_HIVE_ML_REPORT_MAX" in src, \
            "ML 报告必须有数量上限，否则扩池会让取价调用量线性膨胀"

    def test_ml_report_pool_is_non_blocking(self):
        """第二处 ThreadPoolExecutor 也必须非阻塞关闭。

        原实现 `with ThreadPoolExecutor(...)` + 无超时的 as_completed/result，
        比 parallel_agent_runner 那处更危险（那处至少有 timeout）。
        """
        src = self._source(code_only=True)
        idx = src.find("def _generate_ml_reports")
        assert idx > 0, "找不到 _generate_ml_reports"
        # 截到下一个同级方法定义为止（而非固定字符窗口——函数比想象中长）
        nxt = src.find("\n    def ", idx + 10)
        body = src[idx:nxt if nxt > 0 else len(src)]
        assert "with ThreadPoolExecutor(" not in body, \
            "_generate_ml_reports 禁止用 with ThreadPoolExecutor（退出时会 join 卡死线程）"
        assert "shutdown(wait=False" in body, "必须非阻塞关闭线程池"
        assert "as_completed(futures, timeout=" in body, "as_completed 必须带超时"

    def test_default_ticker_pool_expanded_and_diverse(self):
        """扩池不变式：≥25 只、≥8 个行业、全部在 watchlist 配置内、无重复。

        N_eff（有效独立标的数）对平均相关极度敏感：10 只科技股 N_eff=3.25，
        加入低相关行业后 30 只 → N_eff=13.8。若有人把池改回全科技股，
        标的数不变但统计功效会塌掉，故断言行业数而非仅标的数。
        """
        import argparse
        import collections
        import contextlib
        import io

        import alpha_hive_daily_report as m
        import config

        captured = {}
        orig = argparse.ArgumentParser.parse_args

        def spy(self, *a, **k):
            for act in self._actions:
                if act.dest == "tickers":
                    captured["t"] = act.default
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = spy
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.main()
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = orig

        tickers = captured.get("t")
        assert tickers, "未取到 --tickers 默认值"
        assert len(tickers) >= 25, f"默认标的仅 {len(tickers)} 只，扩池被回退了"
        assert len(set(tickers)) == len(tickers), "默认标的有重复"

        ext = config.get_extended_watchlist()
        unknown = [t for t in tickers if t not in ext]
        assert not unknown, f"这些标的不在 watchlist 配置里: {unknown}"

        sectors = collections.Counter(ext[t].get("sector", "?") for t in tickers)
        assert len(sectors) >= 8, f"行业数仅 {len(sectors)}，分散度不足: {dict(sectors)}"
        top_share = sectors.most_common(1)[0][1] / len(tickers)
        assert top_share <= 0.5, f"单一行业占比 {top_share:.0%}，过度集中"
