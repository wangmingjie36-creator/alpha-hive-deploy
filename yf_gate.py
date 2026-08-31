#!/usr/bin/env python3
"""
yfinance 全局限流闸门（v0.45.56）
=================================
2026-08-27 事故：全天 **687 次 `Too Many Requests`**，从 14:00:05 持续到
14:29:40 —— 覆盖整个扫描窗口。后果是 `rv_30d` / `iv_rank` / `iv_rv_spread` /
`catalysts` **各 0/30**，宏观整块降级为 `data_source: "fallback"`。

这不是突发。RateLimit 次数逐日在爬：**8/25 = 364 → 8/26 = 487 → 8/27 = 687**。
8/25 的爆发集中在 5 分钟内、之后放行，所以数据还是全的；8/27 持续了 29 分钟，
一根 K 线都没进来。

为什么护栏没拦住
----------------
`resilience.py` 里**早就有** `yfinance_limiter` 和 `yfinance_breaker`。问题是
没人用：

| 模块 | yf 调用点 | 走限流器 |
|---|---|---|
| earnings_watcher | — | ✅ 5 处 |
| swarm_agents/cache | — | ✅ 1 处 |
| market_intelligence（RV30/IV-RV） | 3 | ❌ |
| fred_macro（宏观） | 2 | ❌ |
| risk_engine | 5 | ❌ |
| options_analyzer | 5 | ⚠️ 只用熔断器，`_opt_rl` 导入后从未调用 |
| backtester / cboe_fetcher / data_pipeline | 12 | ❌ |
| 四只蜂 / pead / paper_portfolio / dashboard / outcomes | 11 | ❌ |

**六个受治理的调用点，三十多个不受治理的。** 这与 `http_gate` docstring 里
记过的教训逐字相同：「那把信号量只锁了 CBOE。yfinance / Finnhub /
AlphaVantage 各走各的……**加源之前先确认闸门覆盖它**」。同一个错误，换了一层。

设计：不可绕过的扼流点
----------------------
逐个改 40 处调用点 = 每加一处新代码就要记得接线，而这正是上面那张表的成因。
所以本模块 **patch `yfinance.download` / `yfinance.Ticker` 本身**：接线一次，
新调用点自动受治理，不存在"忘了接"。

    import yf_gate; yf_gate.install()      # 幂等，入口处调一次

429 与瞬时故障必须分开处理
--------------------------
`market_intelligence` 原先的退避是 `time.sleep(0.7 * (attempt + 1))` ——
0.7s / 1.4s，按「瞬时 SSL/超时故障、**错开即恢复**」那类问题设计的。
（⚠️ 当时写下的理由是「本机 OpenSSL 1.1.1q 并发 HTTPS 抛 SSLEOFError」，
该归因 2026-08-25 已被重测证伪，见 `http_gate` docstring；但退避本身对
瞬时故障仍然成立，所以下面保留了它——变的是分类依据，不是策略。）
对上限流器它是**反向**的：把 1 次请求变成 3 次、间隔不到 2 秒，
在被拒绝时加倍施压。

所以：
- **SSL/超时等瞬时故障** → 短退避重试（保留原语义）
- **429** → 立即熔断，不重试。一次 429 的含义是「现在就停」，不是「再试试」。
"""

from __future__ import annotations

import threading
import time

try:
    from hive_logger import get_logger
    _log = get_logger("yf_gate")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.yf_gate")


# ---------------------------------------------------------------- 限流参数
# 速率本身定义在 `resilience.yfinance_limiter`（全进程唯一的桶），这里不另设，
# 免得两个数字各说各话。改速率去改那一处。

# 收到 429 后的冷却时长。实测 8/27 14:29 的限流到 8/28 01:38（约 11 小时后）
# 已解除，但**短窗突发限流**只需数十秒即可恢复——两者是不同机制，这里治后者。
RATE_LIMIT_COOLDOWN = 180.0

_lock = threading.Lock()
_installed = False
_orig_download = None
_orig_ticker = None

# 观测计数：供覆盖率闸/日志核对「跑完了吗」，永远不看退出标语。
_stats = {"calls": 0, "rate_limited": 0, "blocked_by_cooldown": 0}
_cooldown_until = 0.0


class YFRateLimited(ConnectionError):
    """yfinance 正处于限流冷却期。调用方应视同该数据源不可用，走降级链下一环。

    **绝不能被兜底成 0.0 / 5.0 / 常量** —— 那是静默降级，比抛错晚爆炸得多。

    为什么继承 `ConnectionError` 而不是 `RuntimeError`：`resilience.py` 里已经
    记过这个坑 ——「yfinance 限流异常（YFRateLimitError）并不继承上述任何一类，
    若不显式纳入，Yahoo 429 会穿透 except 直接崩溃报告生成」。本类是闸门**新造**
    的异常，若用 `RuntimeError` 就等于把那个坑原样再挖一遍：三只蜂
    （bear/scout/rival）与各取数点的 `except NETWORK_ERRORS` 都接不住它，
    原本能优雅降级的路径会直接崩。
    `ConnectionError ⊂ OSError ⊂ NETWORK_ERRORS`，全线自动接住。
    """


def _limiter():
    """复用 `resilience.yfinance_limiter` —— **全进程唯一那一个桶**。

    刻意不自造：`swarm_agents/cache.py` 和 `earnings_watcher.py` 已经在用它，
    再建一个桶意味着两拨调用方各持一半配额、谁也不知道对方在打，
    合起来照样超速。桶必须是共享的才叫限流。
    """
    global _bucket
    if _bucket is None:
        try:
            from resilience import yfinance_limiter
            _bucket = yfinance_limiter
        except Exception:  # pragma: no cover - resilience 不可得时退化为不限流
            return None
    return _bucket


_bucket = None


def is_rate_limit_error(exc: BaseException) -> bool:
    """判定一个异常是否是 yfinance 限流。

    不能只 isinstance —— `YFRateLimitError` 只继承 `YFException`，而部分路径
    （yf.download 内部吞掉异常后重抛、或旧版本）只留下文本。两条都认。
    """
    if type(exc).__name__ == "YFRateLimitError":
        return True
    try:
        from yfinance.exceptions import YFRateLimitError
        if isinstance(exc, YFRateLimitError):
            return True
    except Exception:  # pragma: no cover
        pass
    return "too many requests" in str(exc).lower() or "rate limited" in str(exc).lower()


def _enter_cooldown(reason: str) -> None:
    global _cooldown_until
    with _lock:
        _cooldown_until = time.monotonic() + RATE_LIMIT_COOLDOWN
        _stats["rate_limited"] += 1
    _log.warning("yfinance 限流，进入 %.0fs 冷却（%s）；期间调用一律抛 YFRateLimited",
                 RATE_LIMIT_COOLDOWN, reason)


def _check_cooldown() -> None:
    with _lock:
        remaining = _cooldown_until - time.monotonic()
        if remaining > 0:
            _stats["blocked_by_cooldown"] += 1
    if remaining > 0:
        raise YFRateLimited(f"yfinance 限流冷却中，还剩 {remaining:.0f}s")


def _gated(fn, what: str):
    """把一次 yfinance 调用套上「冷却检查 → 限流令牌 → 429 识别」。"""
    def wrapper(*args, **kwargs):
        _check_cooldown()
        rl = _limiter()
        if rl is not None and not rl.acquire(timeout=60.0):
            raise YFRateLimited(f"等待 yfinance 限流令牌超过 60s（{what}）")
        with _lock:
            _stats["calls"] += 1
        try:
            return fn(*args, **kwargs)
        except BaseException as e:
            if is_rate_limit_error(e):
                _enter_cooldown(f"{what}: {e}")
                raise YFRateLimited(str(e)) from e
            raise
    wrapper.__name__ = getattr(fn, "__name__", what)
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    wrapper._yf_gate_wrapped = True          # 供测试断言「确实接上了」
    return wrapper


# ---------------------------------------------------------------- Ticker 包装
# 会打网的方法与 property。分开列是因为包法不同（property 要重建 property）。
_TICKER_METHODS = ("history", "option_chain", "get_earnings_dates")
_TICKER_PROPS = ("info", "calendar", "options", "news")


def _make_gated_method(base, name: str):
    _orig = getattr(base, name)

    def _m(self, *a, **k):
        return _gated(lambda *aa, **kk: _orig(self, *aa, **kk), f"Ticker.{name}")(*a, **k)

    _m.__name__ = name
    return _m


def _make_gated_property(base, name: str):
    """只对**首次**访问计限流。

    yfinance 把 `.info` / `.calendar` 缓存在实例上，调用方常反复读同一个属性。
    每次都扣一个令牌 = 缓存命中也要等 2s，把限流器变成性能灾难。
    这里只闸住真正发生网络请求的那一次。
    """
    _orig = getattr(base, name)

    def _get(self):
        try:
            _seen = self.__dict__.setdefault("_yf_gate_fetched", set())
        except Exception:  # pragma: no cover - __slots__ 之类
            return _gated(lambda: _orig.fget(self), f"Ticker.{name}")()
        if name in _seen:
            return _orig.fget(self)          # 已取过，走 yfinance 自己的缓存
        _r = _gated(lambda: _orig.fget(self), f"Ticker.{name}")()
        _seen.add(name)                      # 失败时不记，重试仍受闸
        return _r

    return property(_get)


def install() -> bool:
    """给 `yfinance.download` / `yfinance.Ticker` 接上闸门。幂等。

    Returns: 本次是否真的完成了安装（已装过返回 False）
    """
    global _installed, _orig_download, _orig_ticker
    with _lock:
        if _installed:
            return False
    try:
        import yfinance as yf
    except ImportError:
        _log.warning("yfinance 不可得，闸门未安装")
        return False

    _orig_download = yf.download
    _orig_ticker = yf.Ticker
    yf.download = _gated(_orig_download, "yf.download")

    # Ticker 的网络发生在**属性/方法访问**时，不是构造时，所以不能只包 __init__。
    # 且两种形态都要治：`history()` 是方法，`calendar` / `info` 是 property。
    # 只包 history 会漏掉催化剂 —— 8/27 掉的第四个字段正是走 `t.calendar`
    # （chronos_bee.py:115），它是 property。
    _OrigTicker = _orig_ticker
    _ns = {}
    for _name in _TICKER_METHODS:
        if hasattr(_OrigTicker, _name):
            _ns[_name] = _make_gated_method(_OrigTicker, _name)
    for _name in _TICKER_PROPS:
        if isinstance(getattr(_OrigTicker, _name, None), property):
            _ns[_name] = _make_gated_property(_OrigTicker, _name)
    _ns["_yf_gate_wrapped"] = True
    _GatedTicker = type("Ticker", (_OrigTicker,), _ns)
    yf.Ticker = _GatedTicker

    with _lock:
        _installed = True
    _b = _limiter()
    _log.info("yfinance 闸门已安装（%s req/s, burst %s, 429 冷却 %.0fs）",
              getattr(_b, "_rate", "?"), getattr(_b, "_burst", "?"), RATE_LIMIT_COOLDOWN)
    return True


def uninstall() -> None:
    """还原 —— 仅供测试使用。"""
    global _installed, _cooldown_until
    if not _installed:
        return
    import yfinance as yf
    yf.download = _orig_download
    yf.Ticker = _orig_ticker
    with _lock:
        _installed = False
        _cooldown_until = 0.0


def is_installed() -> bool:
    return _installed


def ensure() -> None:
    """幂等、极廉价（一个 bool）。给热点取数函数在入口处调。

    为什么不只在 `main()` 里 install 一次就够：本项目有十几个入口
    （日报 / ML 报告 / 仪表板 / 云端快照 / 各种补跑脚本），而**漏接一个入口**
    正是 `resilience.yfinance_limiter` 六年只覆盖 6/40 个调用点的成因。
    热点函数自己 `ensure()`，覆盖率就不依赖「记得在新入口接线」。
    """
    if not _installed:
        install()


def stats() -> dict:
    """观测数据。`rate_limited > 0` 即当轮扫描踩过限流，应进覆盖率报告。"""
    with _lock:
        d = dict(_stats)
        d["in_cooldown"] = max(0.0, _cooldown_until - time.monotonic())
    return d


def reset_stats() -> None:
    global _cooldown_until
    with _lock:
        for k in _stats:
            _stats[k] = 0
        _cooldown_until = 0.0
