"""yfinance 全局限流闸门（v0.45.56）

事故：2026-08-27 全天 **687 次** `Too Many Requests`（8/25=364、8/26=487，
逐日在爬），扫描窗口 14:00–14:29 全程被限流，结果 `rv_30d` / `iv_rank` /
`iv_rv_spread` / `catalysts` **各 0/30**，宏观整块 `data_source: "fallback"`。

护栏当时就在仓库里 —— `resilience.yfinance_limiter` / `yfinance_breaker` ——
只是**只有 6 个调用点在用，三十多个不在**。本文件锁住修复的三条：

  ① 闸门是**不可绕过**的（patch yfinance 模块本身，不靠逐处接线）
  ② 429 **不重试**（旧的 0.7s/1.4s 退避是放大器，不是保护）
  ③ 限流时**如实报不可用**，绝不兜底成 0.0 / 常量
"""

import time

import pytest

import yf_gate


@pytest.fixture(autouse=True)
def _clean():
    yf_gate.uninstall()
    yf_gate.reset_stats()
    yield
    yf_gate.uninstall()
    yf_gate.reset_stats()


class _Fake429(Exception):
    pass


# ---------------------------------------------------------------- ① 安装
class TestInstall:
    def test_install_is_idempotent(self):
        assert yf_gate.install() is True
        assert yf_gate.install() is False
        assert yf_gate.is_installed() is True

    def test_patches_both_entrypoints(self):
        import yfinance as yf
        yf_gate.install()
        assert getattr(yf.download, "_yf_gate_wrapped", False)
        assert getattr(yf.Ticker, "_yf_gate_wrapped", False)

    def test_wraps_properties_not_just_history(self):
        """`calendar` / `info` 是 **property**，不是方法。

        只包 `history()` 会漏掉催化剂 —— 8/27 掉的第四个字段正是走
        `t.calendar`（chronos_bee.py:115）。`.info` 同理（bear/scout/rival 三只蜂）。
        """
        import yfinance as yf
        yf_gate.install()
        for name in ("history", "option_chain", "get_earnings_dates",
                     "info", "calendar", "options", "news"):
            assert name in yf.Ticker.__dict__, f"Ticker.{name} 未被闸门包装"

    def test_property_cache_hit_is_not_throttled(self, monkeypatch):
        """同一实例重复读 `.info` 只算第一次。

        yfinance 把 info 缓存在实例上；每次访问都扣令牌 = 缓存命中也等 2s，
        会把限流器变成性能灾难。
        """
        import yfinance as yf
        hits = {"n": 0}

        # ⚠️ 顺序要紧：install() 在安装那一刻就**捕获**了原 property 对象，
        # 装完再 monkeypatch 是改不到它的（闸门里握的还是旧引用，会真去打网）。
        # 所以先替换、再安装。
        monkeypatch.setattr(
            yf.Ticker, "info",
            property(lambda self: hits.__setitem__("n", hits["n"] + 1) or {"x": 1}),
            raising=False,
        )
        yf_gate.install()
        yf_gate.reset_stats()

        t = yf.Ticker("NVDA")
        for _ in range(4):
            t.info
        assert hits["n"] == 4, "底层 getter 应每次都被调用（yfinance 自己缓存）"
        assert yf_gate.stats()["calls"] == 1, "缓存命中也被计入限流"

    def test_uninstall_restores(self):
        import yfinance as yf
        orig = yf.download
        yf_gate.install()
        assert yf.download is not orig
        yf_gate.uninstall()
        assert yf.download is orig

    def test_ensure_installs(self):
        assert yf_gate.is_installed() is False
        yf_gate.ensure()
        assert yf_gate.is_installed() is True

    def test_shares_the_one_bucket(self):
        """闸门必须复用 resilience.yfinance_limiter。

        自造第二个桶 = 两拨调用方各持一半配额、互相看不见，
        合起来照样超速 —— 那正是 8/27 的成因。
        """
        from resilience import yfinance_limiter
        yf_gate.install()
        assert yf_gate._limiter() is yfinance_limiter


# ---------------------------------------------------------------- ② 429 识别
class TestExceptionType:
    def test_is_caught_by_network_errors(self):
        """`YFRateLimited` 必须被现有的 `except NETWORK_ERRORS` 接住。

        resilience.py 已经记过这个坑：「yfinance 限流异常并不继承上述任何一类，
        若不显式纳入，Yahoo 429 会穿透 except 直接崩溃报告生成」。
        闸门新造的异常若用 RuntimeError，等于把同一个坑再挖一遍 ——
        bear/scout/rival 三只蜂的 `.info` 路径原本能优雅降级，会直接崩。
        """
        from resilience import NETWORK_ERRORS
        assert issubclass(yf_gate.YFRateLimited, NETWORK_ERRORS)
        assert issubclass(yf_gate.YFRateLimited, OSError)


class TestRateLimitDetection:
    @pytest.mark.parametrize("msg", [
        "Too Many Requests. Rate limited. Try after a while.",
        "TOO MANY REQUESTS",
        "rate limited",
    ])
    def test_detects_by_text(self, msg):
        assert yf_gate.is_rate_limit_error(_Fake429(msg)) is True

    def test_detects_by_class_name(self):
        cls = type("YFRateLimitError", (Exception,), {})
        assert yf_gate.is_rate_limit_error(cls("whatever")) is True

    @pytest.mark.parametrize("exc", [
        ValueError("boom"), ConnectionError("EOF occurred in violation of protocol"),
        TimeoutError("timed out"),
    ])
    def test_does_not_misfire(self, exc):
        assert yf_gate.is_rate_limit_error(exc) is False


# ---------------------------------------------------------------- ③ 行为
class TestGateBehaviour:
    def test_429_enters_cooldown_and_blocks_next_call(self, monkeypatch):
        import yfinance as yf
        yf_gate.install()

        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            raise _Fake429("Too Many Requests. Rate limited.")

        monkeypatch.setattr(yf_gate, "_orig_download", _boom)
        yf.download = yf_gate._gated(_boom, "yf.download")

        with pytest.raises(yf_gate.YFRateLimited):
            yf.download("NVDA")
        assert calls["n"] == 1

        # 冷却期内**不再打网**——这是「不重试」的核心断言
        with pytest.raises(yf_gate.YFRateLimited):
            yf.download("NVDA")
        assert calls["n"] == 1, "冷却期内仍然发出了请求 —— 限流被放大了"

        st = yf_gate.stats()
        assert st["rate_limited"] == 1 and st["blocked_by_cooldown"] >= 1
        assert st["in_cooldown"] > 0

    def test_non_429_passes_through_unchanged(self):
        import yfinance as yf
        yf_gate.install()

        def _boom(*a, **k):
            raise ValueError("解析失败")

        yf.download = yf_gate._gated(_boom, "yf.download")
        with pytest.raises(ValueError):
            yf.download("NVDA")
        # 普通异常不得触发冷却，否则一次解析错误会拖垮整轮扫描
        assert yf_gate.stats()["rate_limited"] == 0

    def test_throttles(self, monkeypatch):
        """限流必须真的产生等待，否则它只是个装饰。

        ⚠️ 自带慢桶，**不读全局速率**：conftest 的 `_fast_yfinance_limiter`
        把全局桶调成了 10000 req/s，若这里依赖它，下限会算成 0，
        断言恒真 —— 那正是「一个不会变红的护栏只是装饰」。
        """
        from resilience import RateLimiter
        import yfinance as yf

        slow = RateLimiter(rate=4.0, burst=1)      # 0.25s 一个令牌
        monkeypatch.setattr(yf_gate, "_bucket", slow)
        yf_gate.install()
        yf.download = yf_gate._gated(lambda *a, **k: "ok", "yf.download")

        t0 = time.monotonic()
        for _ in range(3):
            yf.download("NVDA")
        elapsed = time.monotonic() - t0

        lower = (3 - slow._burst) / slow._rate * 0.8      # ≈0.4s
        assert lower > 0, "下限必须为正，否则断言恒真"
        assert elapsed >= lower, f"{elapsed:.2f}s < 下限 {lower:.2f}s —— 限流没生效"


# ---------------------------------------------------------------- ④ 覆盖率
class TestCoverageGuards:
    def test_no_from_yfinance_import(self):
        """`from yfinance import download` 会绕过闸门（拿的是函数不是模块）。

        闸门的全部价值在于「不可绕过」；这条断言守住它。
        """
        import glob
        import os
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bad = []
        pat = re.compile(r"^\s*from\s+yfinance\s+import\s+(?!.*exceptions)", re.M)
        for f in glob.glob(os.path.join(root, "*.py")) + \
                 glob.glob(os.path.join(root, "swarm_agents", "*.py")):
            src = open(f, encoding="utf-8", errors="ignore").read()
            if pat.search(src):
                bad.append(os.path.basename(f))
        assert not bad, f"这些模块绕过了闸门（改用 `import yfinance as yf`）：{bad}"

    def test_hot_paths_call_ensure(self):
        """8/27 挂掉的四个字段，其取数函数必须自己 ensure()。

        只在 main() 里 install 不够——本项目有十几个入口，
        漏接一个正是限流器长期只覆盖 6/40 个调用点的成因。
        """
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for mod in ("market_intelligence.py", "fred_macro.py", "options_analyzer.py",
                    "dashboard_renderer.py"):
            src = open(os.path.join(root, mod), encoding="utf-8").read()
            assert "from yf_gate import ensure" in src, f"{mod} 未接闸门"


# ---------------------------------------------------------------- ⑤ 诚实降级
class TestHonestDegradation:
    def test_iv_rv_reports_unavailable_not_zero(self, monkeypatch):
        """限流时 `calculate_iv_rv_spread` 必须如实说不可用。

        `rv_30d` 兜成 0.0 与「真的没波动」不可区分 —— 这是本项目
        最常见的故障形态（见 MEMORY 静默降级三件套）。
        """
        import market_intelligence as mi

        def _boom(*a, **k):
            raise _Fake429("Too Many Requests. Rate limited.")

        monkeypatch.setattr("yfinance.download", _boom)
        d = mi.calculate_iv_rv_spread("NVDA", 48.6)

        assert d["rv_30d"] is None, "限流时 rv_30d 必须是 None，不能是 0.0"
        assert d["data_available"] is False
        assert d["error"], "必须留下可读的失败原因"

    def test_iv_rv_does_not_retry_on_429_without_the_gate(self, monkeypatch):
        """429 只准打一次网 —— 且这条必须在**闸门缺席时**依然成立。

        ⚠️ 这个测试第一版是假的：它没停用闸门，于是第 2/3 次调用被冷却挡下，
        `calls == 1` 无论 market_intelligence 有没有早退都成立。把修复注释掉
        它照样绿 —— 一个不会变红的护栏只是装饰。

        现在停用闸门，单独隔离出 `calculate_iv_rv_spread` 自己的行为：
        旧实现对 429 也走 `time.sleep(0.7*(n+1))` 重试 3 次，是限流的放大器。
        """
        import market_intelligence as mi
        import yf_gate as _g

        monkeypatch.setattr(_g, "ensure", lambda: None)   # 隔离：本测试不装闸门
        assert not _g.is_installed()

        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            raise _Fake429("Too Many Requests. Rate limited.")

        monkeypatch.setattr("yfinance.download", _boom)
        mi.calculate_iv_rv_spread("NVDA", 48.6)
        assert calls["n"] == 1, f"429 后仍重试了 {calls['n']-1} 次 —— 退避变成了施压"

    def test_gate_alone_also_caps_it_at_one(self, monkeypatch):
        """第二层：即便调用方忘了早退，闸门自己也把 429 后的重试挡在冷却里。

        与上一条是**两层独立的保护**，故分开断言 —— 合成一条就会像第一版那样
        互相遮掩。
        """
        import yfinance as yf
        yf_gate.install()
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            raise _Fake429("Too Many Requests. Rate limited.")

        yf.download = yf_gate._gated(_boom, "yf.download")
        for _ in range(3):                       # 模拟"调用方照旧重试 3 次"
            with pytest.raises(yf_gate.YFRateLimited):
                yf.download("NVDA")
        assert calls["n"] == 1, "闸门没能把重试挡在冷却期外"
