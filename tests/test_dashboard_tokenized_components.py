"""仪表板组件层走站点令牌（v0.45.352，重做 v0.45.77 的意图）

v0.45.77（`3581d8d`，08-30）在分支上把组件层迁到令牌系统，从未合并；一个月里 main 上又有
15 个提交动过这几个文件。本版在现 main 上重做，这里锁住两件「退回去不会有任何东西红」的事：

1. **个股深度卡头不再是「整栏方向色 + 白字」通栏。** 那种结构不能简单接令牌——暗色主题下
   `--bull` 切到高亮绿，白字压亮绿对比度更差。v0.45.77 的解法是拆结构：中性底 + `.sdir-*`
   徽标 + 公司名/板块（取自 config，查不到就不渲染该行，不编）。
2. **renderer 里不再有硬编码的涨跌 / 中性十六进制色。** 它们在浅色主题下对比度不够
   （`#ffc107` 1.53:1、`#28a745` 2.93:1），且不随暗色切换。`rgba(r,g,b,α)` 淡染底色是站点惯例、
   不在此列；`#94a3b8` / `#666` / `#fff` 这类「无数据 / 纯装饰」的灰白也不在此列
   （套 `--neut` 会把「没有这项数据」误读成「中性信号」）。
"""

import re
from pathlib import Path

import pytest

import dashboard_renderer as dr

ROOT = Path(__file__).resolve().parent.parent

# 涨跌 / 中性语义的旧硬编码色（含 Bootstrap 与 Tailwind 两套来源）
_DIRECTIONAL_HEX = re.compile(
    r"#(22c55e|ef4444|28a745|dc3545|f59e0b|ffc107|16a34a|dc2626|d97706|4ade80|f87171|fbbf24)\b", re.I)


def test_renderer_has_no_directional_hex():
    src = (ROOT / "dashboard_renderer.py").read_text(encoding="utf-8")
    hits = [f"{src.count(chr(10), 0, m.start()) + 1}:{m.group(0)}" for m in _DIRECTIONAL_HEX.finditer(src)]
    assert not hits, f"硬编码涨跌色回来了，改用 var(--bull/--bear/--neut)：{hits}"


def test_directional_hex_pattern_has_teeth():
    assert _DIRECTIONAL_HEX.search('color = "#28a745" if pct >= 80')
    assert not _DIRECTIONAL_HEX.search('background:rgba(34,197,94,.10)')


class TestCompanyCardHeader:

    @pytest.fixture(autouse=True)
    def _offline(self, stub_yfinance):
        """`_detail` 会逐票补价，钉成离线（取价失败本就不阻断渲染）。"""

    def _card(self, ticker, direction, tmp_path):
        sd = {ticker: {"direction": direction, "final_score": 7.8, "agent_details": {}}}
        return dr._build_deep_analysis_html([ticker], {}, sd, tmp_path, "2026-09-11", {})

    def test_no_colored_banner(self, tmp_path):
        html = self._card("NVDA", "bullish", tmp_path)
        assert '<div class="cc-header">' in html
        assert not re.search(r'class="cc-header"[^>]*style=', html), "卡头又挂回了内联背景色"

    @pytest.mark.parametrize("direction,cls", [
        ("bullish", "sdir-bull"), ("bearish", "sdir-bear"), ("neutral", "sdir-neut"), ("看多", "sdir-bull"),
    ])
    def test_direction_badge(self, tmp_path, direction, cls):
        assert f'class="sdir {cls}"' in self._card("NVDA", direction, tmp_path)

    def test_name_and_sector_from_config(self, tmp_path):
        from config import WATCHLIST
        info = WATCHLIST["NVDA"]
        html = self._card("NVDA", "bullish", tmp_path)
        assert f'<span class="cc-name">{info["name"]} · {info["sector"]}</span>' in html

    def test_unknown_ticker_renders_no_name_line(self, tmp_path):
        """查不到就不渲染，不拿 ticker 或空串占位。"""
        assert "cc-name" not in self._card("ZZZZ", "neutral", tmp_path)


def test_conflict_icon_is_a_typographic_mark():
    """emoji 不能着色、不随主题变；站点信号标记统一用 ● ▲ ▼。"""
    import inspect
    src = inspect.getsource(dr._signal_conflicts)
    assert "⚠" not in src and "\\u26a0" not in src
    assert '<span class="cw-icon">▲</span>' in src
