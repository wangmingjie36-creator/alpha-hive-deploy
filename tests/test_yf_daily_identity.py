"""v0.45.773：`yf_daily.download` 的身份校验——返回里混进了没请求的票就丢掉、计数、标缺。

背景见 `tests/test_yf_download_serialized.py`（根因：yfinance 并发 download 串数据，修复是 `yf_gate` 的串行锁）。
这里是第二层 + 观测点：串了的帧是**完整的别家数据**（长度、列、新鲜度都合格），下游无从分辨；此前只有 `_get_ma` 一处校验，
`supply_chain`（每次扫描 150 次）把别家的 5 日涨跌当成自己的一路写进评分（11-11：120 格里 9 格、10-29：59 格与同一标的在别的票里的值不一致）。
装了串行锁后 `identity_mismatch` 应恒为 0——非 0 就是锁被绕过或 yfinance 行为变了，`alert_manager` 要红。
"""

import types

import numpy as np
import pandas as pd

import yf_daily

SRC = "prefetch_bundle"          # SOURCES 里登记过的调用点名


def _frame(syms, n=10, seed=0, nan_syms=()):
    """yfinance 默认形状（group_by=column）：第 0 层价格、第 1 层票。`nan_syms` 里的票给全空占位列（单票取失败时 yfinance 的样子）。"""
    idx = pd.bdate_range(end="2026-10-27", periods=n)
    parts = {}
    for i, s in enumerate(syms):
        base = 100.0 + 10 * i + seed
        close = np.linspace(base, base + n - 1, n)
        d = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close,
                          "Volume": np.arange(1000, 1000 + n, dtype="int64"),
                          "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
        if s in nan_syms:
            d = d.astype(float) * np.nan
        parts[s] = d
    raw = pd.concat(parts, axis=1, names=["Ticker", "Price"])
    return raw.swaplevel(0, 1, axis=1).sort_index(axis=1, level=0)


def _yf(raw):
    return types.SimpleNamespace(download=lambda *a, **k: raw)


def _close(frame, sym):
    return frame[("Close", sym)].tolist()


class TestIdentity:
    def test_matching_request_untouched(self):
        raw = _frame(["VZ"])
        out, flags = yf_daily.download(_yf(raw), "VZ", source=SRC, period="1mo", interval="1d")
        assert _close(out, "VZ") == _close(raw, "VZ")
        assert flags == {"VZ": False}
        st = yf_daily.stats()
        assert st["identity_mismatch"] == 0 and st["identity_foreign"] == 0 and st["identity_items"] == []

    def test_single_request_gets_other_ticker_is_emptied_and_flagged(self):
        """请求 VZ、返回 SOXX 的完整帧（2026-11-11 15:13:45 的样子）。"""
        out, flags = yf_daily.download(_yf(_frame(["SOXX"])), "VZ", source=SRC, period="1mo", interval="1d")
        assert out.empty, "别家的数据不能当 VZ 的用"
        assert flags == {"VZ": True}
        st = yf_daily.stats()
        assert st["identity_mismatch"] == 1 and st["identity_foreign"] == 1
        assert st["by_source"][SRC]["identity_mismatch"] == 1
        assert st["identity_items"] == [{"source": SRC, "asked": ["VZ"], "got": ["SOXX"]}]

    def test_extra_ticker_dropped_requested_ones_unchanged(self):
        raw = _frame(["AAA", "BBB", "CCC"])
        out, flags = yf_daily.download(_yf(raw), ["AAA", "BBB"], source=SRC, period="1mo", interval="1d")
        assert sorted(set(out.columns.get_level_values(1))) == ["AAA", "BBB"]
        for s in ("AAA", "BBB"):
            assert _close(out, s) == _close(raw, s)
        assert "CCC" not in flags and flags == {"AAA": False, "BBB": False}
        st = yf_daily.stats()
        assert st["identity_mismatch"] == 1 and st["identity_foreign"] == 1

    def test_requested_missing_and_foreign_present(self):
        out, flags = yf_daily.download(_yf(_frame(["AAA", "CCC"])), ["AAA", "BBB"], source=SRC, period="1mo", interval="1d")
        assert sorted(set(out.columns.get_level_values(1))) == ["AAA"]
        assert flags == {"AAA": False, "BBB": True}, "请求了却没拿到的票要标成缺了（refuse 的调用方据此当取不到）"

    def test_case_and_separator_variants_are_not_mismatches(self):
        raw = _frame(["AAA", "BBB"])
        for req in ("aaa bbb", "AAA,BBB", ["aaa", "BBB"]):
            yf_daily._reset_stats()
            out, _ = yf_daily.download(_yf(raw), req, source=SRC, period="1mo", interval="1d")
            assert _close(out, "AAA") == _close(raw, "AAA"), req
            assert yf_daily.stats()["identity_mismatch"] == 0, req

    def test_failed_ticker_placeholder_is_not_a_mismatch(self):
        """多票请求里个别票取失败：yfinance 给一列全空占位。这不是串数据，不能被误杀。"""
        raw = _frame(["AAA", "BBB"], nan_syms=("BBB",))
        out, _ = yf_daily.download(_yf(raw), ["AAA", "BBB"], source=SRC, period="1mo", interval="1d")
        assert set(out.columns.get_level_values(1)) == {"AAA", "BBB"}
        assert yf_daily.stats()["identity_mismatch"] == 0

    def test_non_frame_and_flat_columns_pass_through(self):
        sentinel = object()
        out, flags = yf_daily.download(types.SimpleNamespace(download=lambda *a, **k: sentinel), "AAA", source=SRC, period="1mo")
        assert out is sentinel and flags == {}
        flat = _frame(["AAA"]).xs("AAA", axis=1, level=1)            # 单层列：没有票名可对
        out, _ = yf_daily.download(_yf(flat), "ZZZ", source=SRC, period="1mo", interval="1d")
        assert yf_daily.stats()["identity_mismatch"] == 0
        assert sorted(out.columns) == ["Close", "High", "Low", "Open", "Volume"]

    def test_unrelated_stats_keys_unchanged_by_clean_calls(self):
        """干净的调用不改任何既有计数的形状（by_source 条目不凭空多出 identity_mismatch 键）。"""
        yf_daily.download(_yf(_frame(["AAA"])), "AAA", source=SRC, period="1mo", interval="1d")
        assert "identity_mismatch" not in yf_daily.stats()["by_source"][SRC]
