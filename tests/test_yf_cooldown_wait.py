"""标的开跑前等 yfinance 冷却结束（v0.45.716）

事故：2026-11-03 扫描第 282 次请求撞上**一次** 429，之后 180s 冷却里仍在开新标的 ⇒
二十几只的 HV / 异常流秒回失败（iv_rank 10/30、unusual_flow 9/30，判数据源故障日），
失败秒回又让处理节奏暴涨、把 Massive 新闻 5 次/分钟名额挤爆（9 只降级 Yahoo）。

本文件锁住：
  ① `yf_cooldown_wait.CooldownWaitBudget` 只睡、不发请求；按冷却段扣预算（并行等同一段只扣一次）；
     超单次上限 / 超总预算 ⇒ 不等，照旧失败，且计数可见（谁会红：scan_timing 的 yfinance 一节）
  ② 扫描在**有效性检测之前**等（它也取 yfinance 日线），上下文缺省带预算
全部用假时钟，不睡真时间、不出网。
"""

import pytest

import yf_cooldown_wait
import yf_gate


@pytest.fixture(autouse=True)
def _clean():
    yf_gate.reset_stats()
    yield
    yf_gate.reset_stats()


class _Clock:
    """假单调钟：sleep 推进时间；`during_sleep` 可在睡眠中插入动作（模拟别的线程）。"""

    def __init__(self, t=1000.0):
        self.t = t
        self.slept = []
        self.during_sleep = None

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        hook, self.during_sleep = self.during_sleep, None
        if hook:
            hook()
        self.t += s


def _cooldown(clock, remaining):
    yf_gate._cooldown_until = clock.t + remaining


def _budget(clock, total=600.0, per=185.0):
    return yf_cooldown_wait.CooldownWaitBudget(total, per, sleep=clock.sleep, now=clock.now)


class TestCooldownWaitBudget:
    def test_not_in_cooldown_returns_immediately(self):
        c = _Clock()
        assert _budget(c).wait("X") is True
        assert c.slept == []
        st = yf_gate.stats()
        assert st["cooldown_waits"] == 0 and st["cooldown_wait_refused"] == 0

    def test_waits_out_remaining_then_true(self):
        # 11-03 纸面组合撞上时冷却还剩 86s —— 这个量级必须等
        c = _Clock()
        _cooldown(c, 86.0)
        b = _budget(c)
        assert b.wait("CVX") is True
        assert c.slept == [86.0]
        st = yf_gate.stats()
        assert st["cooldown_waits"] == 1 and st["cooldown_wait_s"] == 86.0
        assert b.spent_s == pytest.approx(86.0)

    def test_over_per_wait_max_refuses_without_sleeping(self):
        c = _Clock()
        _cooldown(c, 400.0)
        assert _budget(c, per=185.0).wait("X") is False
        assert c.slept == []
        assert yf_gate.stats()["cooldown_wait_refused"] == 1

    def test_total_budget_exhausted_refuses(self):
        # 429 风暴日：第二段冷却超出剩余预算 ⇒ 不等（退回秒回失败），别把扫描拖进 Step 2 超时
        c = _Clock()
        b = _budget(c, total=300.0)
        _cooldown(c, 180.0)
        assert b.wait("A") is True
        _cooldown(c, 180.0)
        assert b.wait("B") is False
        assert c.slept == [180.0]
        st = yf_gate.stats()
        assert st["cooldown_waits"] == 1 and st["cooldown_wait_refused"] == 1

    def test_same_cooldown_segment_charged_once(self):
        # 4 个并行标的同时等同一段冷却：墙钟只过去一次，只扣一次
        c = _Clock()
        b = _budget(c, total=200.0)
        _cooldown(c, 180.0)
        inner = []
        c.during_sleep = lambda: inner.append(b.wait("T2"))   # 第一位睡着时第二位到来
        assert b.wait("T1") is True
        assert inner == [True]
        assert b.spent_s == pytest.approx(180.0)
        assert yf_gate.stats()["cooldown_waits"] == 1

    def test_new_cooldown_during_wait_is_judged_again(self):
        # 等的时候别的线程又撞一次 429 ⇒ 新的一段，按同样规则再判、再扣
        c = _Clock()
        b = _budget(c, total=600.0)
        _cooldown(c, 100.0)
        c.during_sleep = lambda: setattr(yf_gate, "_cooldown_until", c.t + 100.0 + 180.0)
        assert b.wait("X") is True
        assert c.slept == [100.0, 180.0]
        assert yf_gate.stats()["cooldown_waits"] == 2
        assert b.spent_s == pytest.approx(280.0)

    def test_never_issues_requests(self, monkeypatch):
        # 等待不得经闸门发任何 yfinance 调用（不给 Yahoo 加压）
        c = _Clock()
        _cooldown(c, 50.0)
        before = yf_gate.stats()["calls"]
        _budget(c).wait("X")
        assert yf_gate.stats()["calls"] == before

    def test_stats_keys_present_for_scan_timing(self):
        st = yf_gate.stats()
        for k in ("cooldown_waits", "cooldown_wait_s", "cooldown_wait_refused"):
            assert k in st


class TestScanWiring:
    def test_context_factory_builds_budget(self):
        import alpha_hive_daily_report as r
        b = r._make_yf_wait_budget()
        assert isinstance(b, yf_cooldown_wait.CooldownWaitBudget)
        assert b.total_s == r.YF_COOLDOWN_WAIT_BUDGET_S

    def test_waits_before_validity_check(self, monkeypatch):
        import alpha_hive_daily_report as r
        import swarm_agents

        order = []

        class _B:
            def wait(self, label=""):
                order.append(("wait", label))
                return True

        def _validity(t):
            order.append(("validity", t))
            return {"valid": False, "warning": "test"}   # 提前返回，不跑 Phase-1

        monkeypatch.setattr(swarm_agents, "check_ticker_validity", _validity)
        ctx = r._SwarmContext(targets=["ZZZ"], board=None, phase1_agents=[], rival_agent=None,
                              guard_agent=None, bear_agent=None, queen=None, all_agents=[],
                              prefetch_elapsed=0.0, start_time=0.0, yf_wait_budget=_B())
        rep = object.__new__(r.AlphaHiveDailyReporter)
        assert rep._analyze_single_ticker(ctx, "ZZZ", 1, 1) is None
        assert order == [("wait", "ZZZ"), ("validity", "ZZZ")]

    def test_no_budget_means_no_wait(self, monkeypatch):
        import alpha_hive_daily_report as r
        import swarm_agents
        monkeypatch.setattr(swarm_agents, "check_ticker_validity",
                            lambda t: {"valid": False, "warning": "test"})
        ctx = r._SwarmContext(targets=["ZZZ"], board=None, phase1_agents=[], rival_agent=None,
                              guard_agent=None, bear_agent=None, queen=None, all_agents=[],
                              prefetch_elapsed=0.0, start_time=0.0)
        rep = object.__new__(r.AlphaHiveDailyReporter)
        assert rep._analyze_single_ticker(ctx, "ZZZ", 1, 1) is None
