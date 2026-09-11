"""数据质量行：拿不到的维度要显示「—」，不能静默省略（v0.45.114 回归）

背景
----
`_build_dim_dq_html` 原本对拿不到的维度 `continue` 掉，造成两种静默：

    部分缺失（生产 1401 条里 52 条，3.7%）
        → 那几维从行里**消失**，剩下的 bar 照常铺满整行，看起来像一行完整数据。
    全缺（7 条，0.5%）
        → `items` 为空 ⇒ 返回**空串**，整行在卡片上直接不存在。

后者是 v0.45.113 记的「三条报警通道全哑」的第 ② 条：那 7 张 BRK-B 卡片
同时拿到一个伪造的正常五边形雷达图和一行「不存在」的数据质量。

两种情况出自**同一个 `if pct is None: continue`**，故一并修。只特判全缺
就正好重犯 v0.45.54 那个「修一支漏一支」的错——v0.45.113 刚为此付过代价。

渲染约定（零 CSS 改动，复用既有类）
--------------------------------
    有数值 → 彩色 `dq-fill` + `NN%`
    拿不到 → 空 `dq-bar`（`background:var(--border)` 本身是灰底轨道）+ `—` + title「无数据」

`—` 与 `0%` 的区别在读数与 tooltip：`0%` = 测过、质量为零；`—` = 没测到。

边界证据
--------
拿全部生产 `dim_data_quality` 逐条跑新旧实现 diff：
**五项齐全的 1342 条逐字节不变**，有差异的 59 条全部是缺失维度
（52 条部分 None + 7 条全 None）。
"""

import pytest

from dashboard_renderer import _DIM_DQ_LABELS, _build_dim_dq_html

FULL = {"signal": 87.1, "catalyst": 100.0, "sentiment": 100.0,
        "odds": 85.0, "risk_adj": 100.0}
# 生产真实形态（.swarm_results_2026-03-16 QCOM，catalyst 缺）
PARTIAL = {"signal": 87.1, "catalyst": None, "sentiment": 94.0,
           "odds": 85.0, "risk_adj": 100.0}
# 生产真实形态（.swarm_results_2026-08-04 BRK-B）
ALL_NONE = dict.fromkeys(_DIM_DQ_LABELS, None)


class TestNeverSilentlyOmits:
    """行里永远是 5 个 dq-item —— 少一个就是有维度被静默省略了。"""

    @pytest.mark.parametrize("dq", [FULL, PARTIAL, ALL_NONE, {}, None],
                             ids=["齐全", "部分缺", "全缺", "空dict", "None"])
    def test_always_five_items(self, dq):
        assert _build_dim_dq_html(dq).count('class="dq-item"') == len(_DIM_DQ_LABELS) == 5

    @pytest.mark.parametrize("dq", [FULL, PARTIAL, ALL_NONE, {}, None],
                             ids=["齐全", "部分缺", "全缺", "空dict", "None"])
    def test_never_returns_empty_string(self, dq):
        assert _build_dim_dq_html(dq).startswith('<div class="dim-dq-row">')

    @pytest.mark.parametrize("dq", [FULL, PARTIAL, ALL_NONE, {}, None],
                             ids=["齐全", "部分缺", "全缺", "空dict", "None"])
    def test_every_label_present(self, dq):
        html = _build_dim_dq_html(dq)
        for label in _DIM_DQ_LABELS.values():
            assert label in html


class TestMissingRendersDash:
    def test_all_none_gives_five_dashes(self):
        html = _build_dim_dq_html(ALL_NONE)
        assert html.count("—") == 5 and "无数据" in html
        assert "dq-fill" not in html          # 一条彩色条都不该有

    def test_partial_marks_only_the_missing_one(self):
        html = _build_dim_dq_html(PARTIAL)
        assert html.count("—") == 1           # 只有 catalyst
        assert html.count("dq-fill") == 4
        assert "催化 数据质量 无数据" in html
        assert "信号 数据质量 87%" in html

    @pytest.mark.parametrize("bad", [None, "N/A", "", [], {}, True, False],
                             ids=["None", "字符串", "空串", "list", "dict", "True", "False"])
    def test_non_numeric_treated_as_missing(self, bad):
        """bool 是 int 子类，必须当缺失（同 _radar_data 的护栏）"""
        html = _build_dim_dq_html({**FULL, "odds": bad})
        assert "赔率 数据质量 无数据" in html
        assert html.count("—") == 1


class TestRealDataUnchanged:
    """成对的另一半：只断言「缺失要显示 —」不够，
    一个「所有维度都渲染成 —」的粗暴实现也能让上面全绿。"""

    def test_full_row_is_byte_identical_to_legacy(self):
        """五项齐全时的输出必须与 v0.45.113 之前逐字节一致。
        生产 1342 条齐全条目实测 0 处变化。"""
        expected = (
            '<div class="dim-dq-row">'
            '<span class="dq-item" title="信号 数据质量 87%"><span class="dq-lbl">信号</span>'
            '<span class="dq-bar"><span class="dq-fill" style="width:87%;background:#28a745;"></span></span>'
            '<span class="dq-val">87%</span></span>'
            '<span class="dq-item" title="催化 数据质量 100%"><span class="dq-lbl">催化</span>'
            '<span class="dq-bar"><span class="dq-fill" style="width:100%;background:#28a745;"></span></span>'
            '<span class="dq-val">100%</span></span>'
            '<span class="dq-item" title="情绪 数据质量 100%"><span class="dq-lbl">情绪</span>'
            '<span class="dq-bar"><span class="dq-fill" style="width:100%;background:#28a745;"></span></span>'
            '<span class="dq-val">100%</span></span>'
            '<span class="dq-item" title="赔率 数据质量 85%"><span class="dq-lbl">赔率</span>'
            '<span class="dq-bar"><span class="dq-fill" style="width:85%;background:#28a745;"></span></span>'
            '<span class="dq-val">85%</span></span>'
            '<span class="dq-item" title="风险 数据质量 100%"><span class="dq-lbl">风险</span>'
            '<span class="dq-bar"><span class="dq-fill" style="width:100%;background:#28a745;"></span></span>'
            '<span class="dq-val">100%</span></span>'
            '</div>'
        )
        assert _build_dim_dq_html(FULL) == expected

    @pytest.mark.parametrize("pct,color", [(90.0, "#28a745"), (80.0, "#28a745"),
                                           (79.9, "#ffc107"), (50.0, "#ffc107"),
                                           (49.9, "#dc3545"), (0.0, "#dc3545")])
    def test_color_thresholds_unchanged(self, pct, color):
        assert color in _build_dim_dq_html({**FULL, "signal": pct})

    def test_zero_is_not_a_dash(self):
        """0% 是「测过、质量为零」，不是「没测到」——两者必须可区分。"""
        html = _build_dim_dq_html({**FULL, "signal": 0.0})
        assert "信号 数据质量 0%" in html
        assert "信号 数据质量 无数据" not in html
        assert html.count("—") == 0
