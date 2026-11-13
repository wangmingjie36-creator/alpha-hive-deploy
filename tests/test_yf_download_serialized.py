"""v0.45.773：`yf.download` 串行化（根因修复）——多线程并发调 yfinance.download 会互相串数据。

事故（2026-10-13 起、每个扫描日）：`market_intelligence._get_ma` 的身份校验日志「yfinance 请求 VZ 却返回了 SOXX 的数据」；
10-29 一天 25 次、15 条 10-30、当天 `429×0`。原以为是「限流时返回缓存帧」——不是。成因在 yfinance 1.2.0 的 `multi.download`：
它把结果攒在**模块级全局** `yfinance.shared._DFS`，每次调用开头 `shared._DFS = {}` 重建，工作线程往里写、调用方轮询
`len(_DFS) < len(tickers)` 后 concat 整个字典。扫描的 30 只票并行线程各发约 9 次单票 download ⇒ 互相清空 / 写进对方的字典 ⇒
拿到别家的帧（或抛 `dictionary changed size during iteration`，被调用方吞成「不可用」）。
更糟的是只有 `_get_ma` 有身份校验：`supply_chain`（每次扫描 150 次）的 `returns[sym]` 是静默污染（11-11：120 格里 9 格与同一标的在
其他票里的值不一致；10-29：59 格）。

闸门 `yf_gate` 原来只限速率（令牌桶）、不限同时在途个数。修复：`install()` 给 `yf.download` 套进程级串行锁。

本文件锁住：
  ① 正对照——没装闸门时这个桩场景必然串数据（证明下面的断言有牙；yfinance 升级修了内部实现时它会红，提示锁可重新评估）；
  ② 装了闸门就不串、不丢调用；等锁有上限、超时不执行下载且不释放别人的锁；下载抛异常也释放锁；拿到锁后复查冷却；
  ③ 只锁 `download`，`Ticker.history` 等仍并行。
全部离线：把 `yfinance.multi.Ticker` 换成桩，竞态本身发生在 yfinance 自己的 `download` 代码里。
"""

import random
import threading
import time

import pytest

import yf_gate


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """⚠️ 必须把 `yfinance.download` / `yfinance.Ticker` 换回**真实实现**再开测：整套里别的测试（`stub_yfinance`）把它们钉成离线桩，
    而 `yf_gate` 的模块状态会把那个桩当「原函数」记下来（`_orig_download`），`uninstall()` 再把桩原样还回去——单文件跑看不出，
    整套跑时本文件拿到的 `yf.download` 就是 `_OfflineInTests` 桩（v0.45.773 首次整套实测翻车）。真实实现取自 `yfinance.multi.download`
    （conftest 只改包级属性 `yfinance.download`）。"""
    import yfinance
    import yfinance.multi
    import yfinance.ticker
    yf_gate.uninstall()
    monkeypatch.setattr(yfinance, "download", yfinance.multi.download)
    monkeypatch.setattr(yfinance, "Ticker", yfinance.ticker.Ticker)
    yf_gate.reset_stats()
    yield
    yf_gate.uninstall()
    yf_gate.reset_stats()


class _HoldTicker:
    """`yfinance.multi.Ticker` 的离线桩：`history()` 返回一份以票名为标记的日线；`HOLD` 里登记的票先卡住一段时间
    （模拟网络耗时），其余票随机 0~JITTER 秒。不联网。"""

    HOLD: dict = {}
    JITTER = 0.0

    def __init__(self, sym):
        self.sym = sym

    def history(self, **kw):
        import pandas as pd
        hold = self.HOLD.get(self.sym, 0.0)
        time.sleep(hold if hold else random.uniform(0.0, self.JITTER))
        idx = pd.date_range("2026-11-02", periods=5, freq="B")
        return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": float(sum(map(ord, self.sym))),
                             "Volume": 1}, index=idx)


def _tickers_of(df):
    return {str(x).upper() for x in df.columns.get_level_values(-1)} if getattr(df.columns, "nlevels", 1) > 1 else set()


def _two_overlapping_downloads(yf):
    """A 先发、被桩卡住 0.4s；A 已在 yfinance 的等待循环里时 B 才发（0.15s 后）。返回 (A 拿到的票集, B 拿到的票集, 异常们)。"""
    out, errs = {}, []

    def _go(key, sym):
        try:
            out[key] = _tickers_of(yf.download(sym, period="5d", interval="1d", progress=False, auto_adjust=True))
        except BaseException as e:           # noqa: BLE001 — 线程里的异常要带回主线程断言
            errs.append((key, repr(e)))

    ta = threading.Thread(target=_go, args=("A", "AAA"))
    tb = threading.Thread(target=_go, args=("B", "BBB"))
    ta.start()
    time.sleep(0.15)
    tb.start()
    ta.join(10)
    tb.join(10)
    return out.get("A"), out.get("B"), errs


@pytest.fixture
def _hold_ticker(monkeypatch):
    monkeypatch.setattr("yfinance.multi.Ticker", _HoldTicker)
    monkeypatch.setattr(_HoldTicker, "HOLD", {"AAA": 0.6})      # 比 B 的启动延迟（0.15s）长得多：负载高时线程启动抖动也不会让 B 晚于 A 结束
    monkeypatch.setattr(_HoldTicker, "JITTER", 0.0)
    return _HoldTicker


class TestDownloadSerialized:
    def test_install_marks_only_download_serialized(self):
        import yfinance as yf
        yf_gate.install()
        assert yf.download._yf_gate_serialized is True
        assert yf_gate._gated(lambda: None, "x")._yf_gate_serialized is False, "默认不串行：别把 Ticker.history 一并锁了"

    def test_positive_control_unserialized_download_really_crosses(self, _hold_ticker):
        """正对照：没装闸门时，这个桩场景必然串数据。
        若这条红了：说明 yfinance 的 `download` 不再共享全局字典（升级修了它），这把锁可以重新评估。"""
        import yfinance as yf
        try:
            a, b, errs = _two_overlapping_downloads(yf)
        finally:
            # 没锁时 A 的工作线程在 A 返回之后（0.4s）还会往 yfinance 的全局字典里写——不等它写完，会污染下一条测试的 download
            # （这正是竞态的另一副面孔：迟到的写入落进下一次调用的字典）。有锁时调用返回前所有工作线程都已写完，没有这个问题。
            time.sleep(0.7)
        assert not errs, errs
        assert b == {"BBB"}
        assert a == {"BBB"}, f"A 请求的是 AAA，实际拿到 {a}——竞态没复现（yfinance 升级改了内部实现？）"

    def test_gate_prevents_the_crossing(self, _hold_ticker):
        import yfinance as yf
        yf_gate.install()
        a, b, errs = _two_overlapping_downloads(yf)
        assert not errs, errs
        assert a == {"AAA"} and b == {"BBB"}, f"装了闸门仍串数据：A={a} B={b}"
        st = yf_gate.stats()
        assert st["download_serialized"] == 2
        assert st["download_lock_wait_s"] > 0.1, f"B 排队等了 A 的 ~0.25s——等待时间要记下来（扫描变慢多少看它）：{st}"
        assert st["download_lock_timeouts"] == 0

    def test_many_threads_no_crossing_and_no_lost_calls(self, monkeypatch):
        import yfinance as yf
        monkeypatch.setattr("yfinance.multi.Ticker", _HoldTicker)
        monkeypatch.setattr(_HoldTicker, "HOLD", {})
        monkeypatch.setattr(_HoldTicker, "JITTER", 0.02)
        yf_gate.install()
        syms = ["SOXX", "^GSPC", "NVDA", "TSLA", "MSFT", "VZ"]
        rounds, bad, errs, done = 6, [], [], []
        lock = threading.Lock()

        def _w(sym):
            for _ in range(rounds):
                try:
                    got = _tickers_of(yf.download(sym, period="5d", progress=False))
                except BaseException as e:       # noqa: BLE001
                    with lock:
                        errs.append(repr(e))
                    continue
                with lock:
                    done.append(sym)
                    if got != {sym.upper()}:
                        bad.append((sym, got))

        ts = [threading.Thread(target=_w, args=(s,)) for s in syms]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        assert not errs, errs[:3]
        assert not bad, f"串数据 {len(bad)} 次：{bad[:3]}"
        assert len(done) == len(syms) * rounds, "有调用丢了（竞态的另一种表现：抛 dictionary changed size…）"

    def test_lock_timeout_raises_without_running_download(self, monkeypatch):
        import yfinance as yf
        yf_gate.install()
        monkeypatch.setattr(yf_gate, "DOWNLOAD_LOCK_TIMEOUT", 0.05)
        calls = {"n": 0}
        yf.download = yf_gate._gated(lambda *a, **k: calls.__setitem__("n", calls["n"] + 1), "yf.download", serialize=True)
        assert yf_gate._download_lock.acquire(timeout=1)          # 模拟持锁的下载卡住
        try:
            with pytest.raises(yf_gate.YFDownloadLockTimeout) as ei:
                yf.download("NVDA")
            assert isinstance(ei.value, yf_gate.YFRateLimited) and isinstance(ei.value, ConnectionError)
            assert calls["n"] == 0, "没拿到锁就发了下载"
            assert yf_gate._download_lock.locked(), "超时的那个线程不该释放不属于它的锁"
            assert yf_gate.stats()["download_lock_timeouts"] == 1
        finally:
            yf_gate._download_lock.release()
        yf.download("NVDA")                                        # 锁放开后恢复正常
        assert calls["n"] == 1

    def test_exception_inside_download_releases_the_lock(self):
        import yfinance as yf
        yf_gate.install()

        def _boom(*a, **k):
            raise ValueError("解析失败")

        yf.download = yf_gate._gated(_boom, "yf.download", serialize=True)
        with pytest.raises(ValueError):
            yf.download("NVDA")
        assert not yf_gate._download_lock.locked(), "下载抛异常后锁没释放——下一个调用会等到超时"

    def test_cooldown_entered_while_queued_blocks_the_call(self):
        """排队期间别的线程踩了 429：拿到锁后必须复查冷却，别在冷却里发请求。"""
        import yfinance as yf
        yf_gate.install()
        calls = {"n": 0}
        yf.download = yf_gate._gated(lambda *a, **k: calls.__setitem__("n", calls["n"] + 1), "yf.download", serialize=True)
        assert yf_gate._download_lock.acquire(timeout=1)
        res = {}

        def _w():
            try:
                yf.download("NVDA")
                res["r"] = "ok"
            except BaseException as e:               # noqa: BLE001
                res["r"] = e

        t = threading.Thread(target=_w)
        t.start()
        time.sleep(0.1)                                # 让它过了第一道冷却检查、卡在等锁
        yf_gate._enter_cooldown("test: 排队期间别人踩了 429")
        yf_gate._download_lock.release()
        t.join(5)
        assert isinstance(res.get("r"), yf_gate.YFRateLimited), res
        assert calls["n"] == 0, "冷却期内仍然发出了请求"
        assert not yf_gate._download_lock.locked()

    def test_double_wrapped_call_does_not_deadlock_on_itself(self):
        """v0.45.774（二次检查探针）：`install()` 并发 / 重复安装留下双层包装时，同一线程会连取两次锁——不可重入就等满上限再超时。"""
        calls = {"n": 0}
        inner = yf_gate._gated(lambda: calls.__setitem__("n", calls["n"] + 1) or "ok", "yf.download", serialize=True)
        outer = yf_gate._gated(inner, "yf.download", serialize=True)
        t0 = time.monotonic()
        assert outer() == "ok"
        assert time.monotonic() - t0 < 1.0 and calls["n"] == 1
        assert yf_gate.stats()["download_lock_timeouts"] == 0
        assert not yf_gate._download_lock.locked() and yf_gate._dl_holder_since == 0.0

    def test_nested_call_in_same_thread_is_reentrant(self):
        calls = []

        def _outer_fn():
            calls.append("outer")
            return inner()                              # 被包装的函数里又调了一个 serialize 包装

        inner = yf_gate._gated(lambda: calls.append("inner") or "ok", "yf.download", serialize=True)
        outer = yf_gate._gated(_outer_fn, "yf.download", serialize=True)
        assert outer() == "ok" and calls == ["outer", "inner"]
        assert not yf_gate._download_lock.locked()

    def test_stuck_holder_fails_fast_after_the_first_waiter_times_out(self, monkeypatch):
        """v0.45.774（二次检查探针）：持锁者卡死时，只有第一个等锁的线程等满上限；之后到的调用立刻失败，
        不是每个线程每次调用都再等满（30 线程 × 9 次 × 300s 会把一次挂死放大成整轮扫描超时）。"""
        monkeypatch.setattr(yf_gate, "DOWNLOAD_LOCK_TIMEOUT", 0.2)
        stuck = threading.Event()
        release = threading.Event()

        def _holder():
            yf_gate._acquire_download_lock("holder")
            stuck.set()
            release.wait(10)
            yf_gate._release_download_lock()

        th = threading.Thread(target=_holder)
        th.start()
        assert stuck.wait(2)
        fn = yf_gate._gated(lambda: "ok", "yf.download", serialize=True)
        try:
            t0 = time.monotonic()
            with pytest.raises(yf_gate.YFDownloadLockTimeout):
                fn()
            first = time.monotonic() - t0
            assert first >= 0.18, "第一个等锁的线程应该等满上限"
            time.sleep(0.05)                           # 持锁时长已超过上限
            t1 = time.monotonic()
            for _ in range(5):
                with pytest.raises(yf_gate.YFDownloadLockTimeout):
                    fn()
            assert time.monotonic() - t1 < 0.15, "后来者仍在逐个等满上限——卡死会被放大成整轮超时"
            assert yf_gate.stats()["download_lock_timeouts"] == 6
        finally:
            release.set()
            th.join(5)
        assert fn() == "ok", "持锁者放开后恢复正常（不能留下永久的快速失败状态）"
        assert yf_gate._dl_holder_since == 0.0

    def test_non_download_calls_stay_parallel(self):
        """只锁 download：普通被闸门包装的调用（Ticker.history 等）仍可并行——两个线程必须同时在 fn 里。"""
        barrier = threading.Barrier(2, timeout=3)
        fn = yf_gate._gated(lambda: barrier.wait(), "Ticker.history")
        errs = []

        def _w():
            try:
                fn()
            except BaseException as e:               # noqa: BLE001
                errs.append(repr(e))

        ts = [threading.Thread(target=_w) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(5) for t in ts]
        assert not errs, f"两个非 download 调用没能同时在飞（被串行了？）：{errs}"
