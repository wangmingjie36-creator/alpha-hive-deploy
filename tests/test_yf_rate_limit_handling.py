"""Yahoo 限流的失败后处理（v0.45.672）：`yf_gate`（只加观测）+ `alphabot/yf_chain.py` + `alphabot/beta_long.py` +
`AlphaBotService`（重试提示、β 按标的缓存）+ 前端文案与重试状态机（node 真跑）。

起因（2026-10-28 只读查证）：Alpha Bot 里 NVDL 的 Yahoo 备用源 10-27 失败 9 次、10-28 失败 7 次，**全是 `yf_exception:YFRateLimited`**——
每次轮询的第一个请求（`Ticker.options`）被真实的 429 拒绝，之后 `yf_gate` 冷却 180 秒；同一时段新开的进程取同一条链是通的 ⇒ 间歇限流，不是
数据不可用。被拒的那些轮 Alpha Bot 每轮只发了 1 个请求（第一个就被拒；成功的一轮最多 12 个）；清晨窗口里没有任何 launchd / 定时任务碰 yfinance。
所以这里做的是**失败后处理**，不是「抢回额度」：
  · 限流单独成一个原因码 `yf_rate_limited`（不再是笼统的 `yf_exception:<类名>`），页面据此说人话、给「约 N 秒后再试」，并**只自动重试一次**；
  · β 对照被限流后不再逐只去撞，已经取到的不重取、下次只补缺的；
  · `yf_gate` 的日志带上 pid / 命令行 / 第几次连续 429 / 距上次成功多久——下一次「谁在撞」不用靠猜。**`yf_gate` 的行为一点没改**。
每组回答一句「谁会红」，全部离线。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import json

import yf_gate
from alphabot import beta_long as BL
from alphabot import synthetic as syn
from alphabot import yf_chain as Y
from alphabot.service import BETA_FAIL_BACKOFF_SEC, BETA_TTL_SEC, YF_RETRY_MIN_SEC, AlphaBotService
from tests.test_alphabot_hedge import DIRNAME, _write
from tests.test_alphabot_yf_chain import NOW, FakeYF
from tests.test_alphabot_yf_chain import _fetch as _yf_fetch
from tests.test_beta_sensitivity import _bench_closes, _dates, _follow
from tests.test_beta_sensitivity import FakeYF as BetaFakeYF

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "alphabot" / "static"


@pytest.fixture(autouse=True)
def _clean_gate():
    yf_gate.uninstall()
    yf_gate.reset_stats()
    yield
    yf_gate.uninstall()
    yf_gate.reset_stats()


@pytest.fixture
def root_with_rows(tmp_path):
    """真账本夹具（经 `load_history` 放行闸），最近一份审计带逐行暴露与真压力表（与 test_beta_sensitivity 同形，本地定义以免 F811）。"""
    import portfolio_greeks as pg
    root = _write(tmp_path / "data")
    f = root / DIRNAME / "greeks_2026-10-09.json"
    a = json.loads(f.read_text(encoding="utf-8"))
    a["rows"] = syn.demo_beta_rows(778.57)
    a["stress"] = pg.stress_table(a["rows"], a["spy_price"])
    f.write_text(json.dumps(a), encoding="utf-8")
    return root


class _Rec:
    """替身日志：记下 info / warning 的最终文本。"""

    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(("info", msg % a if a else msg))

    def warning(self, msg, *a, **k):
        self.lines.append(("warning", msg % a if a else msg))

    def error(self, msg, *a, **k):
        self.lines.append(("error", msg % a if a else msg))


# ═══════════════════════════════ yf_gate：只加观测、不改行为 ═══════════════════════════════

class TestGateIsRateLimited:
    def test_all_the_shapes_of_a_rate_limit(self):
        assert yf_gate.is_rate_limited(yf_gate.YFRateLimited("yfinance 限流冷却中，还剩 90s"))
        assert yf_gate.is_rate_limited(yf_gate.YFRateLimited("等待 yfinance 限流令牌超过 60s"))
        assert yf_gate.is_rate_limited(Exception("Ticker.options: Too Many Requests. Rate limited. Try after a while."))

        class YFRateLimitError(Exception):
            pass
        assert yf_gate.is_rate_limited(YFRateLimitError("x"))

    @pytest.mark.parametrize("exc", [ValueError("解析失败"), TimeoutError("x"), KeyError("Close"), ConnectionError("reset")])
    def test_other_failures_are_not_rate_limits(self, exc):
        assert yf_gate.is_rate_limited(exc) is False

    def test_retry_after_is_at_least_the_minimum_and_follows_the_cooldown(self):
        assert yf_gate.retry_after_sec(60.0) == 60.0 and yf_gate.retry_after_sec() == 0.0, "没在冷却：就是下限"
        yf_gate._enter_cooldown("t")
        r = yf_gate.retry_after_sec(60.0)
        assert 170.0 < r <= yf_gate.RATE_LIMIT_COOLDOWN, r
        assert yf_gate.retry_after_sec(500.0) == 500.0, "下限永远生效"


class TestGateObservability:
    def _call(self, fn):
        return yf_gate._gated(fn, "t")()

    def test_counters_track_ok_and_consecutive_429(self):
        def ok():
            return 1

        def boom():
            raise Exception("Too Many Requests. Rate limited.")
        assert self._call(ok) == 1
        assert yf_gate.stats()["ok"] == 1 and yf_gate.stats()["consecutive_429"] == 0
        with pytest.raises(yf_gate.YFRateLimited):
            self._call(boom)
        assert yf_gate.stats()["consecutive_429"] == 1
        yf_gate._cooldown_until = 0.0                       # 冷却过了
        with pytest.raises(yf_gate.YFRateLimited):
            self._call(boom)
        assert yf_gate.stats()["consecutive_429"] == 2 and yf_gate.stats()["rate_limited"] == 2
        yf_gate._cooldown_until = 0.0
        assert self._call(ok) == 1
        assert yf_gate.stats()["consecutive_429"] == 0, "一次成功就清零：连续才有意义。变红的变异：不清零"

    def test_the_warning_says_who_how_many_and_when_it_last_worked(self, monkeypatch):
        rec = _Rec()
        monkeypatch.setattr(yf_gate, "_log", rec)

        def boom():
            raise Exception("Ticker.options: Too Many Requests.")
        with pytest.raises(yf_gate.YFRateLimited):
            self._call(boom)
        kind, msg = rec.lines[-1]
        assert kind == "warning" and "pid=" in msg and "本进程从未成功" in msg and "连续第 1 次 429" in msg and "本进程已发 1 次" in msg
        yf_gate._cooldown_until = 0.0
        self._call(lambda: 1)
        yf_gate._cooldown_until = 0.0
        with pytest.raises(yf_gate.YFRateLimited):
            self._call(boom)
        msg = rec.lines[-1][1]
        assert "s 前" in msg and "本进程从未成功" not in msg and "连续第 1 次 429" in msg and "成功 1 次" in msg

    def test_install_line_carries_the_process_identity(self, monkeypatch):
        rec = _Rec()
        monkeypatch.setattr(yf_gate, "_log", rec)
        yf_gate.install()
        assert any("闸门已安装" in m and "pid=" in m for _, m in rec.lines)

    def test_behaviour_is_unchanged_a_429_still_blocks_without_retrying(self):
        calls = []

        def boom():
            calls.append(1)
            raise Exception("Too Many Requests. Rate limited.")
        with pytest.raises(yf_gate.YFRateLimited):
            self._call(boom)
        with pytest.raises(yf_gate.YFRateLimited):
            self._call(boom)
        assert len(calls) == 1, "冷却期内不再打网——这是闸门的核心，本次没动它"


# ═══════════════════════════════ yf_chain：限流单独成一个原因码 ═══════════════════════════════

class TestChainClassification:
    @pytest.mark.parametrize("boom", [yf_gate.YFRateLimited("yfinance 限流冷却中，还剩 90s"),
                                      Exception("Ticker.options: Too Many Requests. Rate limited. Try after a while.")])
    def test_a_rate_limit_is_its_own_reason(self, boom):
        raw, why = _yf_fetch(FakeYF(boom=boom))
        assert raw is None and why == "yf_rate_limited"

    @pytest.mark.parametrize("boom,why", [(TimeoutError("x"), "yf_exception:TimeoutError"), (ValueError("x"), "yf_exception:ValueError")])
    def test_other_failures_keep_their_class_name(self, boom, why):
        """对照：不是限流的异常照旧写类名（变红的变异：把所有异常都归成限流）。"""
        assert _yf_fetch(FakeYF(boom=boom)) == (None, why)

    def test_a_rate_limit_in_the_middle_of_the_chain_is_also_classified(self):
        class _Mid(FakeYF):
            def option_chain(self, e):
                raise yf_gate.YFRateLimited("冷却中")
        assert _yf_fetch(_Mid()) == (None, "yf_rate_limited")


# ═══════════════════════════════ beta_long：限流后不再逐只去撞 ═══════════════════════════════

def _series(n=300):
    days = _dates(n)
    bench = _bench_closes(days)
    return days, {"SPY": bench, "A": _follow(bench, 1.0), "B": _follow(bench, 1.5), "C": _follow(bench, 2.0)}


class _LimitAt(BetaFakeYF):
    """取到指定标的时抛限流。"""

    def __init__(self, series, at):
        super().__init__(series)
        self.at = at

    def Ticker(self, t):                                    # noqa: N802
        if t == self.at:
            self.asked.append((t, "limited"))
            raise yf_gate.YFRateLimited("yfinance 限流冷却中，还剩 90s")
        return super().Ticker(t)


class TestBetaLongRateLimit:
    def test_after_the_first_limit_the_rest_are_not_attempted(self):
        days, series = _series()
        fy = _LimitAt(series, "B")
        betas, info = BL.fetch_long_betas(["A", "B", "C"], days[-1], yf_module=fy)
        assert betas["A"] == pytest.approx(1.0, abs=1e-6), "限流之前取到的照常返回"
        assert betas["B"] is None and betas["C"] is None
        assert info["B"]["reason"] == "rate_limited" and info["C"]["reason"] == "rate_limited"
        assert info["B"]["retry_after_sec"] >= BL.RETRY_MARGIN_SEC and info["C"]["retry_after_sec"] >= BL.RETRY_MARGIN_SEC
        assert BL.RETRY_MARGIN_SEC <= 10, "只留一点余量，不设「至少等 1 分钟」这类固定下限——别让人干等"
        assert [t for t, _ in fy.asked] == ["SPY", "A", "B"], "C 一次都没去撞。变红的变异：限流后继续逐只取"

    def test_a_limited_benchmark_marks_everything_rate_limited_without_asking_for_more(self):
        days, series = _series()
        fy = _LimitAt(series, "SPY")
        betas, info = BL.fetch_long_betas(["A", "B"], days[-1], yf_module=fy)
        assert betas == {"A": None, "B": None} and {i["reason"] for i in info.values()} == {"rate_limited"}
        assert [t for t, _ in fy.asked] == ["SPY"]

    def test_a_non_rate_limit_failure_does_not_stop_the_others(self):
        days, series = _series()
        fy = BetaFakeYF(series, boom=["B"])
        betas, info = BL.fetch_long_betas(["A", "B", "C"], days[-1], yf_module=fy)
        assert info["B"]["reason"] == "exception:TimeoutError" and betas["C"] == pytest.approx(2.0, abs=1e-6)


# ═══════════════════════════════ 服务层：重试提示 ═══════════════════════════════

class _Svc:
    @staticmethod
    def make(tmp_path, t=None):
        clock = [0.0] if t is None else t
        return AlphaBotService(state_dir=tmp_path / "st", ledger_dir=tmp_path / "led", now_fn=lambda: NOW, clock=lambda: clock[0]), clock


@pytest.fixture
def stale_cboe(monkeypatch):
    import sell_strike_report as R
    monkeypatch.setattr(R, "_invalidate_cached_payload", lambda t: None)
    monkeypatch.setattr(R.LG, "_default_fetch", lambda t, *, as_of: (None, "stale_vintage"))


class TestServiceRetryHint:
    def test_a_rate_limited_fallback_tells_the_page_when_to_retry(self, tmp_path, stale_cboe, monkeypatch):
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", lambda t, as_of=None, **k: (None, "yf_rate_limited"))
        monkeypatch.setattr(yf_gate, "retry_after_sec", lambda minimum=0.0: max(minimum, 123.0))
        svc, clock = _Svc.make(tmp_path)
        r = svc.live("NVDL")
        assert r["data_available"] is False and r["reason"] == "yf_rate_limited"
        assert r["retry_after_sec"] == 123.0 and "retry_until_clock" not in r
        assert svc.stats["yf_rate_limited"] == 1 and svc.stats["yf_fallback_failed"] == 1
        clock[0] = 10.0                                              # 15 秒内命中失败缓存：剩余时间按「现在」重算
        assert svc.live("NVDL")["retry_after_sec"] == 113.0

    def test_the_wait_is_the_real_cooldown_not_a_padded_minimum(self, tmp_path, stale_cboe, monkeypatch):
        """等多久完全由闸门冷却决定（冷却期内任何请求都会被当场拒绝），只留一点余量——不再有「至少 60 秒」。
        变红的变异：把下限改回 60。"""
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", lambda t, as_of=None, **k: (None, "yf_rate_limited"))
        svc, _ = _Svc.make(tmp_path)
        r = svc.live("NVDL")                                  # 闸门没在冷却（测试里没装）⇒ 只剩余量
        assert r["retry_after_sec"] == YF_RETRY_MIN_SEC and YF_RETRY_MIN_SEC <= 10
        monkeypatch.setattr(yf_gate, "retry_after_sec", lambda minimum=0.0: max(minimum, 37.0))
        svc2, _ = _Svc.make(tmp_path)
        assert svc2.live("NVDL")["retry_after_sec"] == 37.0, "冷却还剩 37 秒就是 37 秒"

    def test_other_failures_carry_no_retry_hint(self, tmp_path, stale_cboe, monkeypatch):
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", lambda t, as_of=None, **k: (None, "yf_oi_stale"))
        svc, _ = _Svc.make(tmp_path)
        r = svc.live("NVDL")
        assert r["reason"] == "yf_oi_stale" and "retry_after_sec" not in r and svc.stats["yf_rate_limited"] == 0

    def test_a_later_success_clears_the_hint(self, tmp_path, stale_cboe, monkeypatch):
        outcome = [(None, "yf_rate_limited")]
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", lambda t, as_of=None, **k: outcome[0])
        svc, clock = _Svc.make(tmp_path)
        svc.live("NVDL")
        assert "NVDL" not in svc._yf_retry_until, "live() 已取走（不留着给下一次无关的失败）"
        svc._yf_retry_until["NVDL"] = 999.0
        outcome[0] = ({"ticker": "NVDL", "contracts": [{"x": 1}], "data_source": "yfinance"}, None)
        svc._chain_fetch(invalidate=False)("NVDL", as_of=None)
        assert "NVDL" not in svc._yf_retry_until


# ═══════════════════════════════ 服务层：β 对照的部分结果 ═══════════════════════════════

def _svc_beta(root, fn, clock):
    return AlphaBotService(fetch_fn=syn.demo_fetch, straddle_root=root, now_fn=lambda: NOW, clock=lambda: clock[0], beta_long_fn=fn)


class TestBetaCache:
    """按标的缓存：值、取到的时刻、最近一次的明细、绝对的「最早再试时刻」。
    覆盖：失败后别干等 / 永久取不到的标的不续命 / 强制刷新失败要看得见 / 缓存命中时重试时间照「现在」重算。"""

    @staticmethod
    def _ok(**betas):
        return betas, {t: {"n": 250, "reason": None} for t in betas}

    @staticmethod
    def _limited(tickers, wait):
        return {t: None for t in tickers}, {t: {"n": 0, "reason": "rate_limited", "retry_after_sec": float(wait)} for t in tickers}

    def _run(self, root, script, clock):
        asked = []

        def fn(tickers, day):
            asked.append(list(tickers))
            return script(len(asked), list(tickers))
        return _svc_beta(root, fn, clock), asked

    def test_retry_time_is_recomputed_on_every_serve_and_the_cooldown_is_not_hammered(self, root_with_rows):
        """（问题 3）缓存命中时「约 N 秒后」按现在重算；冷却期内重新打开 / 点「重新取 β」不发请求，立刻回结果。"""
        def script(n, tickers):
            return ({"XOM": 0.2, "NVDA": None, "AMZN": 1.4}, {"NVDA": {"n": 0, "reason": "rate_limited", "retry_after_sec": 120.0}}) if n == 1 \
                else self._ok(NVDA=1.85)
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        r = svc.beta_sensitivity()
        assert r["fetch"]["rate_limited"] is True and r["fetch"]["retry_after_sec"] == 120.0 and r["partial"] is True
        clock[0] = 50.0
        r = svc.beta_sensitivity()
        assert r["fetch"]["retry_after_sec"] == 70.0 and len(asked) == 1, "第二次命中缓存：剩余时间按现在重算，且没去撞 Yahoo。变红的变异：用取数那一刻的数"
        clock[0] = 60.0
        assert svc.beta_sensitivity(force=True)["fetch"]["retry_after_sec"] == 60.0 and len(asked) == 1, "冷却中点「重新取 β」也不发请求"

    def test_retry_is_possible_the_moment_the_cooldown_ends_not_after_a_fixed_wait(self, root_with_rows):
        """（别干等）冷却 30 秒就在第 31 秒重试，不是固定 5 分钟；而且只补缺的那一只。变红的变异：恢复固定的部分结果 TTL。"""
        def script(n, tickers):
            return ({"XOM": 0.2, "NVDA": None, "AMZN": 1.4}, {"NVDA": {"n": 0, "reason": "rate_limited", "retry_after_sec": 30.0}}) if n == 1 \
                else self._ok(NVDA=1.85)
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        svc.beta_sensitivity()
        clock[0] = 31.0
        r = svc.beta_sensitivity()
        assert asked[1] == ["NVDA"] and r["partial"] is False and r["n_alt"] == 3 and "rate_limited" not in r["fetch"]

    def test_a_transient_non_rate_limit_failure_backs_off_briefly(self, root_with_rows):
        def script(n, tickers):
            return ({"XOM": 0.2, "NVDA": None, "AMZN": 1.4}, {"NVDA": {"n": 0, "reason": "exception:TimeoutError"}}) if n == 1 \
                else self._ok(NVDA=1.85)
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        svc.beta_sensitivity()
        clock[0] = BETA_FAIL_BACKOFF_SEC - 1
        svc.beta_sensitivity()
        assert len(asked) == 1, "刚失败就别再撞"
        clock[0] = BETA_FAIL_BACKOFF_SEC + 1
        svc.beta_sensitivity()
        assert asked[1] == ["NVDA"] and BETA_FAIL_BACKOFF_SEC <= 60

    def test_a_permanently_unavailable_ticker_neither_refetches_nor_keeps_good_values_alive(self, root_with_rows):
        """（问题 2 · 无限续命）XOM 历史不足、永远判不了：不再每次重取（也就不会每次多取一次 SPY）；
        其余标的的值只按**它们自己的**取数时刻过期，不被「整份条目」的时间戳续命。变红的变异：永久失败也重取 / 部分结果重置全部时间戳。"""
        def script(n, tickers):
            if n == 1:
                return ({"XOM": None, "NVDA": 1.85, "AMZN": 1.4}, {"XOM": {"n": 80, "reason": "insufficient_overlap"}})
            return self._ok(**{t: 1.0 for t in tickers})
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        r = svc.beta_sensitivity()
        assert r["missing"] == ["XOM"] and r["partial"] is True
        for at in (400.0, 4000.0, BETA_TTL_SEC - 1):
            clock[0] = at
            svc.beta_sensitivity()
        assert len(asked) == 1, "永久失败的标的不触发重取；好值没到各自的 6 小时"
        clock[0] = BETA_TTL_SEC + 1
        svc.beta_sensitivity()
        assert asked[1] == ["AMZN", "NVDA"], "6 小时一到只重取过期的好值，不再碰永久失败的 XOM"
        clock[0] = BETA_TTL_SEC + 10
        svc.beta_sensitivity(force=True)
        assert len(asked) == 3 and "XOM" not in asked[2], "用户点「重新取 β」也不去重取永久判不了的标的。变红的变异：force 时全取"

    def test_good_values_age_from_their_own_fetch_time_even_when_another_ticker_is_refetched(self, root_with_rows):
        def script(n, tickers):
            if n == 1:
                return ({"XOM": 0.2, "NVDA": None, "AMZN": 1.4}, {"NVDA": {"n": 0, "reason": "exception:TimeoutError"}})
            return self._ok(**{t: 1.5 for t in tickers})
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        svc.beta_sensitivity()
        clock[0] = BETA_FAIL_BACKOFF_SEC + 1
        r31 = svc.beta_sensitivity()                                    # 只补 NVDA，在 31 秒
        assert r31["cache_age_sec"] == BETA_FAIL_BACKOFF_SEC + 1, "年龄 = 用到的值里最老的（XOM / AMZN 是 0 秒取的），不是最新的 NVDA。变红的变异：取最新"
        clock[0] = BETA_TTL_SEC + 1
        r = svc.beta_sensitivity()
        assert asked[1] == ["NVDA"] and sorted(asked[2]) == ["AMZN", "XOM"], "XOM / AMZN 是 0 秒取的，6 小时到了就过期；NVDA 是 31 秒取的，还没到"
        assert r["cache_age_sec"] <= BETA_TTL_SEC

    def test_a_failed_forced_refresh_is_visible_and_the_old_values_keep_their_real_age(self, root_with_rows):
        """（问题 2 · 强制刷新失败被掩盖）已有完整结果、点「重新取 β」被限流：值照用，但页面必须看到「刷新失败」+「值是多久前的」，
        不能回一份「刚取的」。变红的变异：失败时用 now 重写取数时刻 / 不记刷新失败。"""
        def script(n, tickers):
            return self._ok(XOM=0.2, NVDA=1.85, AMZN=1.4) if n == 1 else self._limited(tickers, 90)
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        svc.beta_sensitivity()
        clock[0] = 100.0
        r = svc.beta_sensitivity(force=True)
        assert len(asked) == 2 and sorted(asked[1]) == ["AMZN", "NVDA", "XOM"]
        assert r["available"] is True and r["n_alt"] == 3 and r["partial"] is False, "旧值照用"
        assert r["cache_age_sec"] == 100.0, "值是 100 秒前取的——不是「刚取的」"
        assert r["fetch"]["rate_limited"] is True and r["fetch"]["retry_after_sec"] == 90.0
        assert [x["ticker"] for x in r["fetch"]["refresh_failed"]] == ["AMZN", "NVDA", "XOM"] and r["fetch"]["failed"] == []

    def test_a_later_successful_refresh_clears_the_failure(self, root_with_rows):
        def script(n, tickers):
            return self._ok(XOM=0.2, NVDA=1.85, AMZN=1.4) if n != 2 else self._limited(tickers, 20)
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        svc.beta_sensitivity()
        clock[0] = 10.0
        svc.beta_sensitivity(force=True)
        clock[0] = 31.0
        r = svc.beta_sensitivity(force=True)
        assert len(asked) == 3 and r["fetch"]["refresh_failed"] == [] and "rate_limited" not in r["fetch"] and r["cache_age_sec"] == 0.0

    def test_everything_rate_limited_on_the_first_try_is_instant_afterwards(self, root_with_rows):
        """第一次就被限流（一个值都没有）：返回不可用 + 原因 + 倒计时；冷却期内再打开不再干等取数。"""
        clock = [0.0]
        svc, asked = self._run(root_with_rows, lambda n, tickers: self._limited(tickers, 90), clock)
        r = svc.beta_sensitivity()
        assert r["available"] is False and r["reason"] == "alt_beta_unavailable" and r["fetch"]["rate_limited"] is True
        clock[0] = 30.0
        r2 = svc.beta_sensitivity()
        assert len(asked) == 1 and r2["fetch"]["retry_after_sec"] == 60.0
        clock[0] = 91.0
        svc.beta_sensitivity()
        assert len(asked) == 2

    def test_a_complete_result_lives_six_hours_and_force_refreshes_everything(self, root_with_rows):
        clock = [0.0]
        svc, asked = self._run(root_with_rows, lambda n, t: self._ok(XOM=0.2, NVDA=1.85, AMZN=1.4), clock)
        svc.beta_sensitivity()
        clock[0] = 3600.0
        svc.beta_sensitivity()
        assert len(asked) == 1
        svc.beta_sensitivity(force=True)
        assert len(asked) == 2 and sorted(asked[1]) == ["AMZN", "NVDA", "XOM"], "什么都不缺时点「重新取 β」⇒ 全部刷新"
        clock[0] = 3600.0 + BETA_TTL_SEC + 1
        svc.beta_sensitivity()
        assert len(asked) == 3

    def test_force_on_a_partial_result_fetches_only_what_is_missing(self, root_with_rows):
        def script(n, tickers):
            return ({"XOM": 0.2, "NVDA": None, "AMZN": 1.4}, {"NVDA": {"n": 0, "reason": "rate_limited", "retry_after_sec": 10.0}}) if n == 1 \
                else self._ok(NVDA=1.8)
        clock = [0.0]
        svc, asked = self._run(root_with_rows, script, clock)
        svc.beta_sensitivity()
        clock[0] = 12.0
        r = svc.beta_sensitivity(force=True)
        assert asked[1] == ["NVDA"] and r["partial"] is False


# ═══════════════════════════════ 前端 ═══════════════════════════════

LIB = (STATIC / "lib.js").read_text(encoding="utf-8")
TICKER = (STATIC / "pages" / "ticker.js").read_text(encoding="utf-8")
BETA_JS = (STATIC / "pages" / "hedge_beta.js").read_text(encoding="utf-8")
HELP = (STATIC / "pages" / "help.js").read_text(encoding="utf-8")


def _fetch_reason_keys():
    body = re.search(r"export const FETCH_REASON_LABEL = \{(.*?)\n\};", LIB, re.S).group(1)
    return set(re.findall(r"^\s*([a-z_]+):", body, re.M))


def _returned_reasons(path):
    """`return None, "<码>"` 里的字符串字面量（不含 f-string 的 `yf_exception:`）。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple) and len(n.value.elts) == 2:
            a, b = n.value.elts
            if isinstance(a, ast.Constant) and a.value is None and isinstance(b, ast.Constant) and isinstance(b.value, str):
                out.add(b.value)
    return out


class TestFrontend:
    def test_every_reason_the_yahoo_chain_can_return_has_a_label(self):
        """双向：yf_chain 能返回的每个码都登记；表里每个 `yf_*` 键也真有人写（没有死码）。
        变红的变异：给 yf_chain 加一个原因码却不登记 / 删掉 yf_chain 的某个码。"""
        keys = _fetch_reason_keys()
        written = _returned_reasons("alphabot/yf_chain.py")
        assert written <= keys | {"stale_vintage", "vintage_mismatch"}, written - keys
        assert {k for k in keys if k.startswith("yf_")} <= written, {k for k in keys if k.startswith("yf_")} - written
        assert {"stale_vintage", "vintage_mismatch", "payload_unavailable", "price_unavailable"} <= keys

    def test_the_rate_limit_reason_is_in_the_chain_and_the_label_table(self):
        assert "yf_rate_limited" in _returned_reasons("alphabot/yf_chain.py") and "yf_rate_limited" in _fetch_reason_keys()

    def test_unknown_codes_are_shown_raw_not_swallowed(self):
        assert 'if (!text) text = raw || "未知";' in LIB and 'raw.startsWith("yf_exception:")' in LIB

    def test_the_ticker_page_uses_the_shared_retry_state_machine(self):
        """页面不自己持有「已重试」状态（那个只加不减的 Set 是 bug）：全部走 retry_gate.js。行为在 `TestRetryGateInNode` 里真跑。"""
        assert 'import { createRetryGate } from "../retry_gate.js";' in TICKER and "const retry = createRetryGate();" in TICKER
        assert "retry.begin(t);" in TICKER and "retry.plan(t," in TICKER and "retry.ok(t);" in TICKER
        assert "autoRetried" not in TICKER and "setTimeout(" not in TICKER, "定时器只在 retry_gate.js 里"
        assert "RETRY_NOTE[plan]" in TICKER

    def test_the_ticker_page_no_longer_shows_the_raw_code_alone(self):
        assert "fetchReasonText(d.reason, d.retry_after_sec)" in TICKER and "`原因：${d.reason || \"未知\"}`" not in TICKER

    def test_the_beta_block_retries_through_the_same_state_machine_and_shows_failed_refreshes(self):
        assert "const retry = createRetryGate();" in BETA_JS and 'retry.begin("beta");' in BETA_JS and 'retry.plan("beta",' in BETA_JS
        assert "R.fetch?.rate_limited" in BETA_JS and "R.fetch?.retry_after_sec" in BETA_JS and "现在就可以" in BETA_JS
        assert "R.fetch?.refresh_failed" in BETA_JS and "刷新失败" in BETA_JS and "不是刚取的" in BETA_JS, "强制刷新失败不能显示成「刚取的」"
        assert "setTimeout(" not in BETA_JS

    def test_the_help_page_explains_it_and_without_literal_markdown(self):
        assert "Yahoo 正在限流" in HELP and "yf_rate_limited" in HELP and "自动重试一次" in HELP and "间歇限流" in HELP
        assert "**" not in HELP, "帮助页是纯文本：`**` 会原样显示"


import json as _json
import shutil
import subprocess

RETRY_GATE = STATIC / "retry_gate.js"


def _run_gate(script: str):
    """在 node 里真跑 retry_gate.js：假定时器（只记录、手动触发），脚本把观察到的东西 `console.log(JSON.stringify(...))`。
    node 缺失 ⇒ **红**（不 skip：skip 会把「这条没验」渲染成「这条没问题」）。GitHub 的 ubuntu runner 自带 node。"""
    node = shutil.which("node")
    assert node, "需要 node 来真跑前端状态机（tests/test_yf_rate_limit_handling.py::TestRetryGateInNode）；本机 / CI 都应有 node"
    prelude = f"""
import {{ createRetryGate }} from {_json.dumps(RETRY_GATE.as_uri())};
const fake = {{ n: 0, timers: new Map(), cleared: [], delays: [] }};
const timers = {{
  set: (f, ms) => {{ const id = ++fake.n; fake.timers.set(id, f); fake.delays.push(ms); return id; }},
  clear: (id) => {{ fake.cleared.push(id); fake.timers.delete(id); }},
}};
const fire = () => {{ const fs = [...fake.timers.values()]; fake.timers.clear(); fs.forEach((f) => f()); return fs.length; }};
const gate = createRetryGate(timers);
let runs = 0;
const run = () => {{ runs += 1; }};
const alive = () => true;
const out = {{}};
"""
    r = subprocess.run([node, "--input-type=module", "-e", prelude + script + "\nconsole.log(JSON.stringify(out));"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:600]
    return _json.loads(r.stdout.strip().splitlines()[-1])


class TestRetryGateInNode:
    """审查员列出的三个状态 bug + 边界，在 node 里真跑（不是 grep 字符串）。"""

    def test_one_automatic_retry_per_episode_then_a_new_episode_gets_a_fresh_one(self):
        """（问题 1a）成功之后的**新**一轮限流必须还有一次自动重试，也不能说「已自动重试过」。变红的变异：成功不清记号。"""
        o = _run_gate("""
const P = { limited: true, retryAfterSec: 60, run, alive };
out.first = gate.plan("T", P);
out.fired = fire(); out.runs1 = runs;
out.again = gate.plan("T", P);                      // 重试后仍被限流 ⇒ 这一轮已经试过
out.no_new_timer = fake.timers.size;
gate.ok("T");                                       // 后来成功了
out.new_episode = gate.plan("T", P);                // 新一轮：又有一次自动重试
""")
        assert o["first"] == "scheduled" and o["fired"] == 1 and o["runs1"] == 1
        assert o["again"] == "tried" and o["no_new_timer"] == 0
        assert o["new_episode"] == "scheduled"

    def test_leaving_the_page_forgets_the_attempt_instead_of_faking_a_retry(self):
        """（问题 1b）安排了重试后用户切了子标签（旧页面 alive 变假）：定时器作废、**不留「已重试过」的假记号**，新渲染可以重新安排。"""
        o = _run_gate("""
let live = true;
out.first = gate.plan("T", { limited: true, retryAfterSec: 60, run, alive: () => live });
live = false;
out.fired = fire(); out.runs = runs;
out.after = gate.plan("T", { limited: true, retryAfterSec: 60, run, alive });
out.timers_after = fake.timers.size;                // 新渲染确实又安排了一个定时器
""")
        assert o["first"] == "scheduled" and o["fired"] == 1 and o["runs"] == 0, "页面没了：不去取数"
        assert o["after"] == "scheduled" and o["timers_after"] == 1, "没有留下假记号：新渲染真的有定时器。变红的变异：作废时不删记号（返回「已安排」却没有定时器）"

    def test_a_manual_refresh_cancels_the_pending_timer(self):
        """（问题 1c）限流后用户在定时器触发前手动刷新并成功：定时器必须被取消，否则会多发一次强制取数、冲掉当前页面。"""
        o = _run_gate("""
gate.plan("T", { limited: true, retryAfterSec: 60, run, alive });
gate.begin("T");                                    // 用户点了「重新拉取」：新渲染开始
gate.ok("T");                                       // 并且成功了
out.fired = fire(); out.runs = runs; out.cleared = fake.cleared.length;
""")
        assert o["fired"] == 0 and o["runs"] == 0 and o["cleared"] >= 1, "变红的变异：begin 不取消待触发的定时器"

    def test_a_manual_refresh_that_fails_again_gets_a_fresh_timer_but_after_an_auto_retry_it_does_not(self):
        o = _run_gate("""
const P = { limited: true, retryAfterSec: 10, run, alive };
gate.plan("T", P);
gate.begin("T");                                    // 手动刷新取消了待触发的
out.manual_fail = gate.plan("T", P);                // 又被限流：重新安排一次
fire();                                             // 自动重试触发
gate.begin("T");                                    // 之后用户再手动刷新
out.after_auto = gate.plan("T", P);                 // 仍被限流：这一轮已经自动试过了
out.pending = fake.timers.size;
""")
        assert o["manual_fail"] == "scheduled" and o["after_auto"] == "tried" and o["pending"] == 0

    def test_wait_comes_from_the_server_plus_a_small_margin_and_long_waits_are_left_to_the_user(self):
        o = _run_gate("""
out.a = gate.plan("A", { limited: true, retryAfterSec: 37, run, alive });
out.b = gate.plan("B", { limited: true, retryAfterSec: 300, run, alive });
out.c = gate.plan("C", { limited: true, retryAfterSec: 301, run, alive });
out.d = gate.plan("D", { limited: true, retryAfterSec: undefined, run, alive });
out.e = gate.plan("E", { limited: true, retryAfterSec: -5, run, alive });
out.f = gate.plan("F", { limited: true, retryAfterSec: 0, run, alive });
out.delays = fake.delays;
""")
        assert (o["a"], o["b"], o["c"], o["d"], o["e"], o["f"]) == ("scheduled", "scheduled", "manual", "manual", "manual", "scheduled")
        assert o["delays"] == [40000, 303000, 3000], "等多久 = 服务端给的冷却剩余 + 3 秒余量；不加固定下限"

    def test_a_non_limit_failure_ends_the_episode(self):
        o = _run_gate("""
gate.plan("T", { limited: true, retryAfterSec: 10, run, alive });
fire();                                             // 自动重试过一次
out.other = gate.plan("T", { limited: false, run, alive });   // 现在失败的原因变了（不是限流）
out.next = gate.plan("T", { limited: true, retryAfterSec: 10, run, alive });
""")
        assert o["other"] == "none" and o["next"] == "scheduled"

    def test_keys_are_independent(self):
        o = _run_gate("""
out.a = gate.plan("A", { limited: true, retryAfterSec: 10, run, alive });
out.b = gate.plan("B", { limited: true, retryAfterSec: 10, run, alive });
gate.begin("A");
out.fired = fire(); out.runs = runs;
""")
        assert o["a"] == o["b"] == "scheduled" and o["fired"] == 1 and o["runs"] == 1, "只取消了 A，B 的照常触发"


class TestDocsAndScope:
    def test_only_the_alpha_bot_layer_uses_the_new_gate_helpers(self):
        """`is_rate_limited` / `retry_after_sec` 是给 Alpha Bot 的失败后处理用的；生产扫描路径不依赖它们（没动它的行为）。"""
        skip = {"tests", "experiments", "_archive"}
        users = set()
        for p in ROOT.rglob("*.py"):
            rel = p.relative_to(ROOT)
            if skip & set(rel.parts) or any(part.startswith(".") for part in rel.parts):
                continue
            src = p.read_text(encoding="utf-8")
            if re.search(r"\b(is_rate_limited|retry_after_sec)\b", src) and rel.as_posix() != "yf_gate.py":
                users.add(rel.as_posix())
        assert users == {"alphabot/yf_chain.py", "alphabot/beta_long.py", "alphabot/service.py", "alphabot/hedge.py"}, users

    def test_the_gate_still_has_one_cooldown_and_no_retry_loop(self):
        """闸门的原则是「429 = 现在就停，不是再试试」：代码里（不看 docstring 的字眼）没有 sleep 调用、没有 while 循环。"""
        tree = ast.parse((ROOT / "yf_gate.py").read_text(encoding="utf-8"))
        sleeps = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sleep"]
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.While)]
        assert sleeps == [] and loops == []
        assert (ROOT / "yf_gate.py").read_text(encoding="utf-8").count("RATE_LIMIT_COOLDOWN = 180.0") == 1
