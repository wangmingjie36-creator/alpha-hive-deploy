"""退避接缝在**生产配置**下仍然真睡、且节奏没变（v0.45.524）。

`tests/_retry_seams.py` 登记的每个 `_retry_sleep` 接缝，在别的测试里都被换成了记录器
（`stub_yfinance` 自动换、`test_pipeline` 等点名换）——也就是说，**除了本文件，没有任何
测试会因为接缝被改成空函数而变红**。本文件把两半各钉一次：

① 接缝 → `time.sleep`：秒数原样传过去，并且确实耗掉了墙钟时间（不打桩的真睡）；
② 重试循环 → 接缝：失败时按原来的退避节奏调用接缝（0.7/1.4、1/2、2/4/8 秒）。

两半合起来才等于「生产的退避还在」：只有①，循环可能不再调接缝；只有②，接缝可能不再睡。
"""
from __future__ import annotations

import importlib
import time
from datetime import date, timedelta

import pytest

from tests._retry_seams import ALL_RETRY_SLEEP_SEAMS, YF_RETRY_SLEEP_SEAMS


# ════════════════════════════ ① 接缝真的在睡 ════════════════════════════

@pytest.mark.parametrize("module", ALL_RETRY_SLEEP_SEAMS)
def test_seam_delegates_to_time_sleep(module, monkeypatch):
    mod = importlib.import_module(module)
    seen = []
    monkeypatch.setattr(time, "sleep", seen.append)
    mod._retry_sleep(1.4)
    assert seen == [1.4], f"{module}._retry_sleep 没把秒数原样交给 time.sleep：{seen}"


@pytest.mark.parametrize("module", ALL_RETRY_SLEEP_SEAMS)
def test_seam_really_waits(module):
    """不打桩：接缝真的耗掉墙钟。防「接缝改成 `pass` 后①仍可能被别的写法骗过」。"""
    mod = importlib.import_module(module)
    t0 = time.monotonic()
    mod._retry_sleep(0.05)
    assert time.monotonic() - t0 >= 0.045


def test_registry_lists_every_module_that_defines_the_seam():
    """登记表不许漏：仓库里定义了 `_retry_sleep` 的生产模块都必须在表里（否则①②都管不到它）。"""
    import ast

    from tests import _ast_cache
    from tests._repo_files import own_python_files
    defined = set()
    files, _ = own_python_files(_ast_cache.REPO)
    for p in files:
        rel = p.relative_to(_ast_cache.REPO)
        if rel.parts[0] in ("tests", "experiments"):
            continue
        tree = _ast_cache.parse_file(p)
        if any(isinstance(n, ast.FunctionDef) and n.name == "_retry_sleep" for n in tree.body):
            defined.add(".".join(rel.with_suffix("").parts))
    assert defined == set(ALL_RETRY_SLEEP_SEAMS), defined


# ════════════════════════════ ② 循环按原节奏调接缝 ════════════════════════════

def test_stub_yfinance_replaces_exactly_the_registered_seams(stub_yfinance):
    for module in YF_RETRY_SLEEP_SEAMS:
        mod = importlib.import_module(module)
        mod._retry_sleep(9.9)
    assert stub_yfinance == [(m, 9.9) for m in YF_RETRY_SLEEP_SEAMS]


def test_options_iv_term_yfinance_backoff(stub_yfinance):
    """`_gated`：失败后退避 0.7、1.4 秒再放弃（retries=2）。"""
    from options_analyzer import OptionsAnalyzer
    pts, errors = OptionsAnalyzer._iv_term_points_yfinance("NVDA", 100.0)
    assert pts == [] and errors
    assert [s for m, s in stub_yfinance if m == "options_analyzer"] == [0.7, 1.4]


def test_options_chain_yfinance_backoff(monkeypatch, tmp_path, stub_yfinance):
    """yfinance 期权链降级路径：每个到期日 `option_chain` 失败后退避 1、2 秒。"""
    import options_analyzer as oa

    exps = [(date.today() + timedelta(days=d)).isoformat() for d in (10, 17)]

    class _Ticker:
        def __init__(self, *_a, **_k):
            pass
        options = exps
        fast_info = {"lastPrice": 100.0}

        def option_chain(self, _expiry):
            raise ConnectionError("down")

    agent = oa.OptionsAgent()
    monkeypatch.setattr(agent.fetcher, "cache_dir", str(tmp_path))
    monkeypatch.setattr(agent.fetcher, "_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(agent.fetcher, "_try_cboe", lambda *a, **k: None)
    monkeypatch.setattr(oa, "_opt_cb", None)
    monkeypatch.setattr(oa.yf, "Ticker", _Ticker)
    agent.fetcher.fetch_options_chain("NVDA", 100.0)
    assert [s for m, s in stub_yfinance if m == "options_analyzer"] == [1.0, 2.0] * len(exps)


def test_market_intelligence_rv_download_backoff(stub_yfinance, monkeypatch):
    """`calculate_iv_rv_spread` 的 yfinance 日K：瞬时失败后退避 0.7、1.4 秒（限流则不重试）。"""
    import market_intelligence as mi
    import twelve_data
    monkeypatch.setattr(twelve_data, "is_configured", lambda: False)
    out = mi.calculate_iv_rv_spread("NVDA", 30.0)
    assert out.get("rv_30d") in (None, 0, 0.0) or "error" in out
    assert [s for m, s in stub_yfinance if m == "market_intelligence"] == [0.7, 1.4]
