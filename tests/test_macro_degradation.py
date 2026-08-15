"""
宏观数据降级不得冒充观测值（v0.43.24 回归）

背景：`fred_macro._fetch_macro_data` 在 7 个宏观标的（^VIX/^TNX/^FVX/DXY/^GSPC/
TLT/GLD）**全部**抓取失败时返回 `base` 常量字典——`vix=20.0`、
`yield_curve="unknown"`、`gold_trend="stable"`——并老实标注
`data_source="fallback"`。

事故：GuardBee 此前不看这个标记，把 20.0 写进 `details["vix"]`，深度报告渲染成
"VIX 20（偏高恐慌）"。而当天真实 VIX 是 14.6（极度平静）——**方向相反的信号**。
实测 88 个扫描日里 13 天如此，2026 年 8 月的 9 个扫描日里占 5 天，且在恶化。

触发原因是 yfinance 限流：宏观数据在 30 只标的扫完之后才抓，配额已耗尽
（2026-08-14 全天 363 条 Too Many Requests）。

注意 `dashboard_renderer` 早就在判 `data_source != "fallback"`，本测试守的是
GuardBee 这条此前缺失的路径。
"""

from unittest.mock import patch

import pytest

from swarm_agents.guard_bee import GuardBeeSentinel

_FALLBACK = {
    "macro_regime": "neutral", "macro_score": 5.0,
    "vix": 20.0, "vix_regime": "elevated",
    "yield_curve": "unknown", "gold_trend": "stable",
    "summary": "宏观数据不可用（降级到默认值）",
    "data_source": "fallback",
}

_REAL = {
    "macro_regime": "neutral", "macro_score": 5.0,
    "vix": 14.25, "vix_regime": "low",
    "yield_curve": "normal", "gold_trend": "rising",
    "summary": "VIX 14.2(low) | 10Y 4.70%",
    "data_source": "yfinance+fred",
}


def _details(macro):
    g = GuardBeeSentinel.__new__(GuardBeeSentinel)  # 跳过重量级 __init__
    with patch("fred_macro.get_macro_context", return_value=macro):
        return g._calc_macro_adjustment("NVDA")["details"]


class TestMacroFallbackNotRecorded:
    @pytest.mark.parametrize("field", ["vix", "yield_curve", "gold_trend"])
    def test_fallback_constants_are_not_recorded(self, field):
        """降级时这些 base 常量不得进入 details——否则会被当成当天的观测值"""
        assert field not in _details(_FALLBACK)

    def test_fallback_is_labeled(self):
        """必须留下可查的标记，而不是静悄悄少几个字段"""
        assert _details(_FALLBACK)["macro_data_source"] == "fallback"

    def test_vix_20_never_reaches_report(self):
        """20.0 是 fred_macro base 的字面值。它一旦进 details，
        深度报告就会渲染成『VIX 20（偏高恐慌）』——与真实的 14.6 方向相反"""
        assert _details(_FALLBACK).get("vix") != 20.0


class TestHealthyMacroUnaffected:
    def test_real_values_still_recorded(self):
        """守卫不能写成永远走降级分支"""
        d = _details(_REAL)
        assert d["vix"] == 14.25
        assert d["yield_curve"] == "normal"
        assert d["gold_trend"] == "rising"
        assert d["macro_data_source"] == "yfinance+fred"

    def test_unknown_source_treated_as_usable(self):
        """只有显式 'fallback' 才算降级；缺少该字段的旧数据不应被误杀"""
        macro = {k: v for k, v in _REAL.items() if k != "data_source"}
        d = _details(macro)
        assert d["vix"] == 14.25
        assert d["macro_data_source"] == "unknown"
