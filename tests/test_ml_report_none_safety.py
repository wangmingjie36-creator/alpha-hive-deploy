"""
ML 报告对 None 字段的容错（v0.43.23 回归）

背景：BuzzBee 对缺价格/成交量的降级源刻意写入 None（buzz_bee.py P0-2：不拿 0
冒充"无动量"、不拿 1 冒充"正常量"）。但 generate_ml_report 用
`det.get("momentum_5d", 0)` 读取——`.get(k, 默认)` 只在**键缺失**时用默认值，
键存在且为 None 时默认值形同虚设 ⇒ `momentum > 0` 与 `f"{momentum:+.2f}"` 均抛
TypeError。

后果：2026-07-15 ~ 08-14 连续一个月，每日自动扫描的 ML 报告成功率 0~1/12，
而异常处理只记 `str(e)[:100]`（丢弃调用栈）、编排器只看退出码，于是天天报
"所有步骤成功"。实测 2026-08-14 快照 27/28 只该字段为 None。
"""

import pytest

from generate_ml_report import MLEnhancedReportGenerator


def _gen():
    """跳过重量级 __init__，只测纯渲染方法"""
    return MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)


def _buzz(details):
    return {"BuzzBeeWhisper": {"score": 6.0, "direction": "bullish",
                               "discovery": "测试叙事", "details": details}}


class TestBuzzNoneSafety:
    def test_none_momentum_and_volume_do_not_crash(self):
        """键存在且为 None —— 正是 .get 默认值救不了的那种"""
        html = _gen()._ch3_buzz(_buzz({
            "sentiment_pct": 48, "momentum_5d": None, "volume_ratio": None,
        }))
        assert html, "不应返回空"

    def test_missing_data_renders_dash_not_zero(self):
        """缺数显示「—」，绝不用 0 / 1 顶替（项目硬规则：不编数据）"""
        html = _gen()._ch3_buzz(_buzz({
            "sentiment_pct": 48, "momentum_5d": None, "volume_ratio": None,
        }))
        assert ">—</div><div class=\"lbl\">5日动量</div>" in html
        assert ">—</div><div class=\"lbl\">成交量比</div>" in html
        assert "+0.00%</div><div class=\"lbl\">5日动量" not in html
        assert "0.00×" not in html

    def test_real_values_still_render(self):
        """有数时照常渲染，别把守卫写成永远走缺失分支"""
        html = _gen()._ch3_buzz(_buzz({
            "sentiment_pct": 62, "momentum_5d": 3.25, "volume_ratio": 1.8,
        }))
        assert "+3.25%" in html
        assert "1.80×" in html
        assert ">—</div><div class=\"lbl\">5日动量</div>" not in html

    @pytest.mark.parametrize("momentum,expect_color", [
        (3.25, "#28a745"),   # 正动量 → 绿
        (-2.10, "#dc3545"),  # 负动量 → 红
        (None, "#6c757d"),   # 缺数 → 中性灰，不能被涂成"看跌"
    ])
    def test_color_reflects_state_including_missing(self, momentum, expect_color):
        html = _gen()._ch3_buzz(_buzz({
            "sentiment_pct": 50, "momentum_5d": momentum, "volume_ratio": 1.0,
        }))
        assert f'color:{expect_color}' in html


class TestScoutNoneSafety:
    def test_scout_momentum_none_does_not_crash(self):
        """ScoutBee 当前该字段健康（2026-08-14 快照 0/27 为 None），
        加守卫是防同源降级把崩溃点从 BuzzBee 平移过来"""
        html = _gen()._ch3_scout({"ScoutBeeNova": {
            "score": 7.0, "direction": "bullish", "discovery": "测试",
            "details": {"momentum_5d": None, "crowding_score": 40, "insider": {}},
        }})
        assert ">—</div><div class=\"lbl\">5日动量</div>" in html


class TestRiskRadarNoneSafety:
    """v0.43.27 回归：2026-08-24 真实事故。

    期权链因 SSL 风暴降级为样本数据 → options_analyzer 诚实返回 iv_rank=None
    （v0.43.19 起）→ `f"{iv_rank:.1f}"` 抛 TypeError，12 只标的 ML 报告全废。

    ⚠️ 这个坑我在 v0.43.23 就**看到过**这一行，但当时用 2026-08-14 的数据实测，
    该键显示为「缺失」而非 None，判定不会命中就跳过了——**用健康数据验证一个
    只在降级时触发的分支**。所以下面每条测试都必须显式喂 None。
    """

    @staticmethod
    def _radar(**over):
        g = _gen()
        opts = {"gamma_squeeze_risk": "low", "iv_rank": 55.0}
        opts.update(over.pop("options", {}))
        ad = {
            "BearBeeContrarian": {"details": {"bear_score": over.pop("bear_score", 6.0)}},
            "ScoutBeeNova": {"details": {"crowding_score": over.pop("crowding", 40)}},
            "ChronosBeeHorizon": {"details": {"catalysts": []}},
        }
        return g._ch6_risk_radar({"conflict_info": {"conflict_level": "low"}}, ad, opts)

    def test_none_iv_rank_does_not_crash(self):
        assert self._radar(options={"iv_rank": None})

    def test_none_iv_rank_renders_dash(self):
        html = self._radar(options={"iv_rank": None})
        assert "IV Rank —" in html
        assert "IV Rank 0.0%" not in html

    @pytest.mark.parametrize("field", ["bear_score", "crowding"])
    def test_other_none_fields_do_not_crash(self, field):
        assert self._radar(**{field: None})

    def test_missing_data_is_not_rendered_as_low_risk(self):
        """最关键的一条：None 当 0 处理会让 risk_level 输出「🟢 低」，
        把「没数据」渲染成「低风险」——比崩溃更危险，因为它不会报错"""
        html = self._radar(bear_score=None, crowding=None)
        assert "⚪ 数据缺失" in html
        # 两个缺数维度都不该出现绿灯
        import re
        rows = [r for r in re.findall(r"<tr>.*?</tr>", html) if "情绪风险" in r or "估值压缩" in r]
        assert rows, "未取到目标行"
        for r in rows:
            assert "🟢" not in r, f"缺数据却渲染成低风险: {r[:120]}"

    def test_real_values_still_render(self):
        """守卫不能写成永远走缺失分支"""
        html = self._radar()
        assert "IV Rank 55.0%" in html
        assert "⚪ 数据缺失" not in html
