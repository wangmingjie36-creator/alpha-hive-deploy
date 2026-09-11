"""ML 深度报告第 5 章情景推演改读 pheromone.db 真实 T+7 分布（v0.45.132）

固化的事实（2026-09-06 查明）：`advanced_analyzer.HistoricalAnalyzer` 此前是 6 条手写
记录（NVDA 3 / VKTX 2 / TSLA 1，2023 年），按「拥挤度 ±10」匹配——27/30 只标的结构上
永远缺失；能命中的两只靠的是「消息数 > 1000 → 63.5」这个常数去撞 2023 年的记录。
同期 pheromone.db 已有 900+ 条核对过 T+7 收盘的真实预测。

这里守六条：
  1. 读库：只读连接；库缺失 / 坏行 → 状态可见（db_status / skipped_rows + warning）
  2. 口径：收益 = close_t7 / price_at_predict − 1；**不读 return_t7**（钳位陷阱）
  3. 分层：同标的+同方向 ≥ MIN_SAMPLE → same_direction；否则不分方向；再不足 → 无 expected_7d
  4. 分位数：线性插值，与 numpy 默认一致；方向口径的命中率与风险收益比只在同方向时有定义
  5. 渲染：五行 = 五个分位点，期望价 = 均值；没有写死的 25/45/20/10 概率；不可用时把样本数说出来
  6. 接线：generate_comprehensive_analysis(direction=…) 真把方向传进历史回溯，rr 闸读的是同一份
全文件只碰临时 sqlite 文件，不出网。
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import advanced_analyzer as AA
from advanced_analyzer import HistoricalAnalyzer, _percentile

_SCHEMA = """CREATE TABLE predictions (
    id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, direction TEXT, final_score REAL,
    price_at_predict REAL, close_t7, return_t7 REAL)"""


def _mkdb(tmp_path, rows):
    """rows: (date, ticker, direction, score, price_at_predict, close_t7[, return_t7])"""
    p = tmp_path / "pheromone.db"
    con = sqlite3.connect(p)
    con.execute(_SCHEMA)
    for r in rows:
        r = tuple(r) + ((None,) if len(r) == 6 else ())
        con.execute("INSERT INTO predictions(date,ticker,direction,final_score,price_at_predict,close_t7,return_t7) VALUES (?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()
    return p


def _rows(ticker, direction, rets, start_day=1, score=6.0):
    """收益（%）序列 → 行；日期递增保证 date_range 可核。"""
    out = []
    for i, r in enumerate(rets):
        d = f"2026-06-{(start_day + i) % 28 + 1:02d}" if i < 28 else f"2026-07-{(i - 28) % 28 + 1:02d}"
        out.append((d, ticker, direction, score, 100.0, 100.0 * (1 + r / 100.0)))
    return out


# ───────────────────────────────────────────── 1+2. 读库与口径
class TestLoad:
    def test_missing_db_is_visible_not_fatal(self, tmp_path):
        h = HistoricalAnalyzer(tmp_path / "nope.db")
        er = h.calculate_expected_returns("NVDA", "bullish")
        assert h.db_status == "missing" and er["db_status"] == "missing"
        assert "expected_7d" not in er and er["sample_size"] == 0

    def test_return_is_close_t7_over_price_at_predict(self, tmp_path):
        p = _mkdb(tmp_path, [("2026-06-01", "NVDA", "bullish", 6.0, 200.0, 220.0, 99.0)])
        h = HistoricalAnalyzer(p)
        sim = h.find_similar_opportunities("NVDA")
        assert sim[0]["gain_7d_pct"] == pytest.approx(10.0)
        assert sim[0]["gain_7d_pct"] != 99.0, "读了 return_t7（钳位离场收益）而不是干净收盘口径"

    def test_bad_rows_are_counted_and_dropped(self, tmp_path, caplog):
        p = _mkdb(tmp_path, [
            ("2026-06-01", "NVDA", "bullish", 6.0, 200.0, 220.0),
            ("2026-06-02", "NVDA", "bullish", 6.0, 200.0, "abc"),
            ("2026-06-03", "NVDA", "bullish", 6.0, 200.0, ""),        # sqlite 会把 NaN 存成 NULL，被 SQL 过滤；空串才到 Python 层
        ])
        h = HistoricalAnalyzer(p)
        with caplog.at_level("WARNING"):
            sim = h.find_similar_opportunities("NVDA")
        assert len(sim) == 1 and h.skipped_rows == 2 and h.db_status == "ok"
        assert any("非法" in m for m in caplog.messages), "坏行被静默丢弃，没人会红"

    def test_readonly_connection_does_not_create_db(self, tmp_path):
        target = tmp_path / "absent.db"
        HistoricalAnalyzer(target).find_similar_opportunities("X")
        assert not target.exists()

    def test_direction_case_insensitive_and_other_tickers_excluded(self, tmp_path):
        p = _mkdb(tmp_path, [("2026-06-01", "NVDA", "Bullish", 6.0, 100.0, 101.0),
                             ("2026-06-01", "TSLA", "bullish", 6.0, 100.0, 150.0)])
        h = HistoricalAnalyzer(p)
        assert [s["gain_7d_pct"] for s in h.find_similar_opportunities("NVDA", "BULLISH")] == [pytest.approx(1.0)]


# ───────────────────────────────────────────── 3. 分层
class TestTiers:
    MIN = HistoricalAnalyzer.MIN_SAMPLE

    def test_same_direction_when_enough(self, tmp_path):
        p = _mkdb(tmp_path, _rows("NVDA", "bullish", [1.0] * self.MIN) + _rows("NVDA", "bearish", [-1.0] * 5))
        er = HistoricalAnalyzer(p).calculate_expected_returns("NVDA", "bullish")
        assert er["basis"] == "same_direction" and er["sample_size"] == self.MIN
        assert er["same_direction_n"] == self.MIN and er["any_direction_n"] == self.MIN + 5

    def test_one_short_falls_back_to_any_direction(self, tmp_path):
        p = _mkdb(tmp_path, _rows("NVDA", "bullish", [1.0] * (self.MIN - 1)) + _rows("NVDA", "bearish", [-1.0] * 5))
        er = HistoricalAnalyzer(p).calculate_expected_returns("NVDA", "bullish")
        assert er["basis"] == "any_direction" and er["sample_size"] == self.MIN + 4
        assert "hit_rate_pct" not in er and "risk_reward" not in er, "不分方向时方向口径无定义"

    def test_insufficient_has_no_distribution_but_says_how_many(self, tmp_path):
        p = _mkdb(tmp_path, _rows("NVDA", "bullish", [1.0] * 5) + _rows("NVDA", "neutral", [0.0] * 3))
        er = HistoricalAnalyzer(p).calculate_expected_returns("NVDA", "bullish")
        assert "expected_7d" not in er and er["basis"] is None
        assert er["sample_size"] == 8 and er["same_direction_n"] == 5
        assert "样本不足" in er["note"] and "5" in er["note"] and "8" in er["note"]

    def test_no_direction_uses_any_direction_only(self, tmp_path):
        p = _mkdb(tmp_path, _rows("NVDA", "bullish", [1.0] * self.MIN))
        er = HistoricalAnalyzer(p).calculate_expected_returns("NVDA", None)
        assert er["basis"] == "any_direction" and er["same_direction_n"] == 0


# ───────────────────────────────────────────── 4. 分位数与方向口径
class TestQuantiles:
    def test_percentile_linear_interpolation(self):
        s = [float(i) for i in range(1, 26)]
        assert _percentile(s, 10) == pytest.approx(3.4)
        assert _percentile(s, 25) == pytest.approx(7.0)
        assert _percentile(s, 50) == pytest.approx(13.0)
        assert _percentile(s, 90) == pytest.approx(22.6)
        assert _percentile(s, 0) == 1.0 and _percentile(s, 100) == 25.0
        with pytest.raises(ValueError):
            _percentile([], 50)

    def test_distribution_fields(self, tmp_path):
        rets = [float(i) for i in range(1, 26)]
        p = _mkdb(tmp_path, _rows("NVDA", "bullish", rets))
        er = HistoricalAnalyzer(p).calculate_expected_returns("NVDA", "bullish")
        e7 = er["expected_7d"]
        assert e7["mean"] == 13.0 and e7["median"] == 13.0
        assert e7["p10"] == 3.4 and e7["p90"] == 22.6 and e7["min"] == 1.0 and e7["max"] == 25.0
        assert er["date_range"][0] < er["date_range"][1]
        assert er["hit_rate_pct"] == 100.0
        assert er["risk_reward"]["ratio"] is None, "没有亏损单时不该冒出一个比率"

    def test_bearish_direction_flips_sign_for_hit_and_rr(self, tmp_path):
        rets = [-4.0] * 15 + [2.0] * 5      # 看空：跌是命中
        p = _mkdb(tmp_path, _rows("NVDA", "bearish", rets))
        er = HistoricalAnalyzer(p).calculate_expected_returns("NVDA", "bearish")
        assert er["hit_rate_pct"] == 75.0
        assert er["risk_reward"]["avg_gain_pct"] == 4.0 and er["risk_reward"]["avg_loss_pct"] == -2.0
        assert er["risk_reward"]["ratio"] == 2.0

    def test_rr_gate_reads_the_same_number(self, tmp_path):
        rets = [3.0] * 10 + [-1.0] * 10
        p = _mkdb(tmp_path, _rows("NVDA", "bullish", rets))
        a = AA.AdvancedAnalyzer()
        er = HistoricalAnalyzer(p).calculate_expected_returns("NVDA", "bullish")
        assert a._calculate_risk_reward_ratio("NVDA", er) == 3.0
        assert a._calculate_risk_reward_ratio("NVDA", {"basis": "any_direction"}) is None
        assert a._calculate_risk_reward_ratio("NVDA", {}) is None
        # 仓库同类守卫那一句：bool 是 int 子类、NaN 能穿过 `is None`——两条闸都要在
        for bad in (True, float("nan"), float("inf"), "3.0"):
            assert a._calculate_risk_reward_ratio("NVDA", {"risk_reward": {"ratio": bad}}) is None, bad


# ───────────────────────────────────────────── 5. 渲染
def _render(analysis, swarm=None):
    from generate_ml_report import MLEnhancedReportGenerator as G
    return G._ch5_scenarios(G.__new__(G), analysis, swarm or {})


def _exp(**over):
    exp = {"sample_size": 40, "basis": "same_direction", "direction": "bullish",
           "same_direction_n": 40, "any_direction_n": 70, "min_sample": 20,
           "date_range": ["2026-03-09", "2026-08-27"], "db_status": "ok",
           "return_basis": "close_t7 / price_at_predict − 1", "hit_rate_pct": 57.5,
           "expected_7d": {"mean": 1.2, "median": 0.8, "min": -9.0, "max": 12.0,
                           "p10": -5.0, "p25": -2.0, "p75": 3.5, "p90": 7.0, "std": 4.0}}
    exp.update(over)
    return exp


class TestRender:
    SWARM = {"direction": "bullish",
             "agent_details": {"ScoutBeeNova": {"details": {"price": 100.0}}}}

    def test_five_quantile_rows_and_mean_price(self):
        html = _render({"historical_analysis": {"expected_returns": _exp()}}, self.SWARM)
        for price, pct in (("$95.00", "-5.0%"), ("$98.00", "-2.0%"), ("$100.80", "+0.8%"),
                           ("$103.50", "+3.5%"), ("$107.00", "+7.0%")):
            assert price in html and pct in html, (price, pct)
        assert "$101.20" in html and "样本均值期望价" in html
        assert "同标的、同方向（bullish）" in html and "40 次" in html
        assert "2026-03-09 ~ 2026-08-27" in html and "命中率 57.5%" in html

    def test_no_hardcoded_probabilities_anywhere(self):
        import inspect
        from generate_ml_report import MLEnhancedReportGenerator as G
        src = inspect.getsource(G._ch5_scenarios)
        for fake in ('25, curr_price', '45, curr_price', 'or 20', 'or 5', 'or -10', 'Σ(概率'):
            assert fake not in src, fake
        html = _render({"historical_analysis": {"expected_returns": _exp()}}, self.SWARM)
        assert "概率加权期望价格" not in html and "25%" not in html and "45%" not in html

    def test_any_direction_basis_is_labelled(self):
        html = _render({"historical_analysis": {"expected_returns": _exp(basis="any_direction", same_direction_n=7)}}, self.SWARM)
        assert "不分方向" in html and "仅 7 条" in html
        assert "命中率" not in html

    def test_insufficient_says_counts_and_reason(self):
        er = {"sample_size": 7, "basis": None, "same_direction_n": 5, "any_direction_n": 7,
              "min_sample": 20, "db_status": "ok", "note": "样本不足：CRM 同方向 5 / 不分方向 7，低于 20"}
        html = _render({"historical_analysis": {"expected_returns": er}}, self.SWARM)
        assert "情景推演不可用" in html and "样本不足：CRM 同方向 5 / 不分方向 7" in html
        assert "同方向样本 5" in html and "不分方向 7" in html and "最低 20" in html

    def test_missing_db_reason_is_shown(self):
        er = {"sample_size": 0, "basis": None, "same_direction_n": 0, "any_direction_n": 0,
              "min_sample": 20, "db_status": "missing", "note": "样本不足：X 同方向 0 / 不分方向 0，低于 20"}
        html = _render({"historical_analysis": {"expected_returns": er}}, self.SWARM)
        assert "情景推演不可用" in html and "样本不足" in html

    def test_missing_price_is_named(self):
        html = _render({"historical_analysis": {"expected_returns": _exp()}}, {})
        assert "情景推演不可用" in html and "现价" in html

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, None, "5"])
    def test_non_finite_quantile_is_unavailable(self, bad):
        e = _exp(); e["expected_7d"]["p10"] = bad
        html = _render({"historical_analysis": {"expected_returns": e}}, self.SWARM)
        assert "情景推演不可用" in html

    def test_hand_written_history_is_gone(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "advanced_analyzer.py"), encoding="utf-8").read()
        for token in ("HistoricalOpportunity(", "initial_crowding", "Q1 2024 Earnings", "Trial Results"):
            assert token not in src, f"手写历史库残留：{token}"


# ───────────────────────────────────────────── 6. 接线
class TestWiring:
    def test_direction_reaches_history_and_rr_gate(self, tmp_path, monkeypatch):
        rets = [3.0] * 15 + [-1.0] * 10
        p = _mkdb(tmp_path, _rows("NVDA", "bullish", rets))
        a = AA.AdvancedAnalyzer()
        a.history = HistoricalAnalyzer(p)
        monkeypatch.setattr(a.dealer_gex, "analyze", lambda *x, **k: {})
        # 第 5 步会 new 一个 OptionsAgent 真打 CBOE（socket 探针实测 4 次出网）——本测试只验接线
        monkeypatch.setattr(AA, "OPTIONS_AGENT_AVAILABLE", False)
        metrics = {"crowding_input": {"social_messages_per_day": 50},
                   "sources": {"yahoo_finance": {"current_price": 100.0}}}
        out = a.generate_comprehensive_analysis("NVDA", metrics, direction="bullish")
        er = out["historical_analysis"]["expected_returns"]
        assert er["basis"] == "same_direction" and er["sample_size"] == 25
        assert out["probability_analysis"]["risk_reward_ratio"] == er["risk_reward"]["ratio"] == 3.0
        assert out["position_management"]["optimal_holding_time"]["note"].startswith("历史样本只有 T+7")
        assert len(out["historical_analysis"]["similar_opportunities"]) == 3

    def test_ml_report_passes_swarm_direction(self, monkeypatch):
        """generate_ml_enhanced_report 必须把方向交给 generate_comprehensive_analysis"""
        import ast
        import inspect
        import generate_ml_report as M
        src = inspect.getsource(M.MLEnhancedReportGenerator.generate_ml_enhanced_report)
        tree = ast.parse(src.lstrip() if not src.startswith("    ") else "\n".join(l[4:] for l in src.splitlines()))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "generate_comprehensive_analysis"]
        assert calls and any(k.arg == "direction" for k in calls[0].keywords)

        # v0.45.135：原断言比对的是 main() 里的**字面量**
        #   'swarm_direction=(swarm_data.get(ticker) or {}).get("direction")'
        # 那条断言分不出「接线断了」与「变量名改了」——本次把该行重构成
        # `_sr = swarm_data.get(ticker) or {}` 后它就红了，而接线其实更全了
        # （v0.45.132 只接了 CLI 路径，生产日扫 `--swarm` 那条一直没传）。
        # 改判 AST：调用点确实传了 swarm_direction 关键字即可。
        # 更完整的「两个蜂群参数必须成对出现在每个调用点」守卫见
        # tests/test_ml_catalyst_quality_source.py::TestProductionWiring
        main_tree = ast.parse(open(M.__file__, encoding="utf-8").read())
        wired = [n for n in ast.walk(main_tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "generate_ml_enhanced_report"
                 and any(k.arg == "swarm_direction" for k in n.keywords)]
        assert wired, "generate_ml_report.main() 必须把蜂群方向传给报告生成器"
