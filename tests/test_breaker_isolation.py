"""熔断器状态不许跨测试泄漏 —— conftest `_reset_circuit_breakers` 的自证（v0.45.344）。

没有本文件，那个 fixture 在不在工作没人知道：v0.45.343 实测全套里**零条**测试消费
泄漏的熔断状态，删掉 fixture 全套照样绿（潜伏的隔离缺口本来就不会自己变红）。

四组，各防一种失效：

* `TestStateDoesNotLeakAcrossTests` —— 顺序成对的两条：第一条弄脏三类熔断器，
  第二条断言全是干净的。防 fixture 被删 / 被改成只 reset 一部分。
* `TestRegistryIsDerived` —— 登记表确实含模块级实例、单例数据源里的实例，
  且不延长任何实例的寿命。防 `__init__` 里的登记被删。
* `TestResetRestoresConstructorState` —— reset 后与新构造的实例逐字段相等。
  防「构造里加了字段、reset 里忘了」。
* `TestResetNeverBlocks` —— 锁被攥着时 reset 与 conftest 的重置都限时返回并点名。
  防「阻塞 reset」：`TestBreakerDoesNotDeadlock` 的变异会留下一个永久攥着锁的
  守护线程，阻塞的逐测试重置会把之后每一条测试都卡死。
"""
import gc
import threading
import time
import weakref

import pytest

# 顺序成对的两条测试之间传话：第一条弄脏后记一笔，第二条先核这一笔。
# 没有这一笔，第二条的「全是干净的」在「第一条没跑 / 没弄脏」时照样成立 —— 证明不了任何事。
_RAN: list = []
_KEPT: dict = {}


class TestStateDoesNotLeakAcrossTests:
    """⚠️ 两条必须按定义顺序跑、且都跑：单独 `-k test_2_...` 会红，这是故意的。"""

    def test_1_dirty_every_kind_of_breaker(self):
        import resilience
        import data_pipeline as dp
        from swarm_agents import cache

        # ① 模块级 CircuitBreaker，经 `from resilience import yfinance_breaker` 的**那份绑定**弄脏
        yb = cache.yfinance_breaker
        for _ in range(yb._failure_threshold):
            yb.record_failure()
        # ② slack 熔断器（此前两个文件靠「换新实例」隔离它）
        sb = resilience.slack_breaker
        for _ in range(sb._failure_threshold):
            sb.record_failure()
        # ③ 独立的 ObservableCircuitBreaker，靠模块级引用活到下一条测试
        ob = dp.ObservableCircuitBreaker("isolation_probe", failure_threshold=2)
        ob.record_failure("probe")
        ob.record_failure("probe")
        _KEPT["observable"] = ob

        # 先证弄脏确实落地，否则下一条的「干净」可能只是因为这里什么都没做
        assert yb.state == resilience.CircuitBreaker.OPEN
        assert sb.state == resilience.CircuitBreaker.OPEN
        assert ob.get_metrics()["state"] == "open" and ob.get_metrics()["trip_count"] == 1
        _RAN.append("dirtied")

    def test_2_next_test_sees_constructor_fresh_breakers(self):
        assert _RAN == ["dirtied"], (
            f"_RAN={_RAN!r}：上一条没在本条之前跑（或没弄脏）⇒ 本条的断言证明不了隔离。"
            "别单独跑本条，也别调换两条的定义顺序。")
        import resilience
        import data_pipeline as dp
        from swarm_agents import cache

        # 被 from-import 的那份绑定与 resilience 上的是同一个对象 —— 所以只能原地 reset
        assert cache.yfinance_breaker is resilience.yfinance_breaker
        for br in (resilience.yfinance_breaker, resilience.slack_breaker):
            assert br.state == resilience.CircuitBreaker.CLOSED, f"{br.name} 漏过来了：{br.state}"
            assert br._failure_count == 0, f"{br.name} 失败计数漏过来了：{br._failure_count}"
        ob = _KEPT.pop("observable")
        fresh = dp.ObservableCircuitBreaker("isolation_probe", failure_threshold=2)
        assert ob.get_metrics() == fresh.get_metrics()


class TestRegistryIsDerived:

    def test_registry_holds_every_module_level_breaker(self):
        import resilience
        import fred_macro
        import newsapi_client

        live = resilience.CircuitBreaker.live_instances()
        module_level = {n: v for n, v in vars(resilience).items()
                        if isinstance(v, resilience.CircuitBreaker)}
        # 正对照：推导出来的名单不能是空的（否则下面的循环恒真）
        assert {"sec_breaker", "yfinance_breaker", "reddit_breaker",
                "slack_breaker"} <= set(module_level), sorted(module_level)
        module_level["fred_macro._fred_breaker"] = fred_macro._fred_breaker
        module_level["newsapi_client._news_breaker"] = newsapi_client._news_breaker
        for name, br in module_level.items():
            assert br is not None, name
            assert any(b is br for b in live), f"{name} 不在登记表里 —— conftest 重置不到它"

    def test_registry_holds_fetcher_source_breakers(self):
        """`get_fetcher()` 单例里各数据源各持一个 ObservableCircuitBreaker。"""
        import data_pipeline as dp
        sources = (dp.CBOESource(), dp.YFinanceSource())
        live = dp.ObservableCircuitBreaker.live_instances()
        for src in sources:
            assert any(b is src.breaker for b in live), f"{src.name} 的熔断器不在登记表里"

    @pytest.mark.parametrize("modname,clsname", [("resilience", "CircuitBreaker"),
                                                  ("data_pipeline", "ObservableCircuitBreaker")])
    def test_registry_does_not_keep_instances_alive(self, modname, clsname):
        import importlib
        cls = getattr(importlib.import_module(modname), clsname)
        br = cls("gc_probe")
        ref = weakref.ref(br)
        assert any(b is br for b in cls.live_instances())
        del br
        gc.collect()
        assert ref() is None, "登记表把实例留住了 —— 应当是 WeakSet"
        assert not any(b.name == "gc_probe" for b in cls.live_instances())


def _state_fields(br, config):
    return {k: v for k, v in vars(br).items() if k not in config and k != "_lock"}


class TestResetRestoresConstructorState:
    """字段清单从 `vars()` 推导，不手抄：构造里新加一个运行态字段，
    「弄脏」这一步会先红（逼你把它弄脏），接着 reset 的比对会红（逼你让 reset 覆盖它）。"""

    def test_circuit_breaker(self):
        from resilience import CircuitBreaker
        config = {"name", "_failure_threshold", "_recovery_timeout"}
        fresh = CircuitBreaker("p", failure_threshold=1, recovery_timeout=60.0)
        br = CircuitBreaker("p", failure_threshold=1, recovery_timeout=60.0)
        br.record_failure()

        before, after_fresh = _state_fields(br, config), _state_fields(fresh, config)
        untouched = {k for k in after_fresh if before[k] == after_fresh[k]}
        assert not untouched, f"这些运行态字段没被弄脏，reset 覆盖不覆盖它们测不出来：{untouched}"
        br.reset()
        assert _state_fields(br, config) == _state_fields(fresh, config)

    def test_observable_circuit_breaker(self):
        from data_pipeline import ObservableCircuitBreaker as OCB
        config = {"name", "failure_threshold", "recovery_timeout", "half_open_max"}
        fresh = OCB("p", failure_threshold=1, recovery_timeout=0, half_open_max=1)
        br = OCB("p", failure_threshold=1, recovery_timeout=0, half_open_max=1)
        br.record_failure("e1")         # closed → open（trip 1）
        assert br.allow_request()       # open → half_open（recovery_timeout=0）
        assert br.allow_request()       # 用掉 half_open 名额
        br.record_success()             # half_open → closed
        br.record_failure("e2")         # closed → open（trip 2），计数全非零

        before, after_fresh = _state_fields(br, config), _state_fields(fresh, config)
        untouched = {k for k in after_fresh if before[k] == after_fresh[k]}
        assert not untouched, f"这些运行态字段没被弄脏，reset 覆盖不覆盖它们测不出来：{untouched}"
        br.reset()
        assert _state_fields(br, config) == _state_fields(fresh, config)


class _LockHolder:
    """另起一个线程攥住熔断器的锁，直到 release()。模拟死锁变异留下的那个线程。"""

    def __init__(self, br):
        self._go, self._held = threading.Event(), threading.Event()
        self._t = threading.Thread(target=self._run, args=(br,), daemon=True,
                                   name=f"breaker-lock-holder-{br.name}")
        self._t.start()
        assert self._held.wait(5), "持锁线程没拿到锁"

    def _run(self, br):
        with br._lock:
            self._held.set()
            self._go.wait(30)

    def release(self):
        self._go.set()
        self._t.join(5)


class TestResetNeverBlocks:

    @pytest.mark.parametrize("modname,clsname", [("resilience", "CircuitBreaker"),
                                                  ("data_pipeline", "ObservableCircuitBreaker")])
    def test_reset_with_timeout_raises_naming_the_breaker(self, modname, clsname):
        import importlib
        br = getattr(importlib.import_module(modname), clsname)("held_probe")
        holder = _LockHolder(br)
        try:
            t0 = time.monotonic()
            with pytest.raises(RuntimeError, match="held_probe"):
                br.reset(timeout=0.2)
            assert time.monotonic() - t0 < 2.0
        finally:
            holder.release()
        br.reset(timeout=0.2)           # 锁放开后照常可重置

    def test_conftest_reset_returns_and_names_a_held_breaker(self, reset_live_breakers):
        """conftest 的逐测试重置遇到被攥着的锁：限时返回、点名，第二次不再等。

        在工作线程里调，免得「阻塞 reset」的变异把这条连同整套一起卡住 ——
        那样它就不是红，是挂。
        """
        from resilience import CircuitBreaker
        br = CircuitBreaker("held_probe")
        holder = _LockHolder(br)
        results, elapsed = [], []

        def _call():
            t0 = time.monotonic()
            results.append(reset_live_breakers("self-test", timeout=1.0))
            elapsed.append(time.monotonic() - t0)

        try:
            for _ in range(2):
                w = threading.Thread(target=_call, daemon=True)
                w.start()
                w.join(5)
                assert not w.is_alive(), (
                    "conftest 的重置在被攥着的锁上阻塞了 —— 死锁变异留下的线程会让之后每条测试都挂住")
        finally:
            holder.release()
        first, second = results
        assert any("held_probe" in s for s in first), first
        assert any("held_probe" in s and "self-test" in s for s in second), second
        assert elapsed[1] < 0.5, f"已知卡死的熔断器第二次又等了 {elapsed[1]:.2f}s —— 每条测试都等会把整套拖垮"
