"""v0.45.544：本会话 504 ~ 542 二次检查里修掉的「None 往下游传」与展示类问题。

P/C / total_oi 自 v0.45.512 / 516 / 522 起会是 None（取不到），下游有几处没接住：
- 深度报告：`.get("total_oi", 0)` 在键存在、值为 None 时不生效 ⇒ `:,.0f` TypeError（整份报告失败；09-24 / 09-25
  那种全断日 30 只都会撞上）；论点失效评估把 None 兜成 1.0 冒充「评估过、没触发」；叙述写成「多空均衡」。
- 期权图表：None 兜成 0.00 ⇒ 染绿、标「Call偏多 →看涨」。
- RivalBee：None 兜成 1.0 当 ML 特征、不进 imputed_features。
- 网站 / ML 报告：流向 "unknown" 原样贴出；样本链早退仍写 "neutral"。
- 回测报告：窗口内 P/C 全 NULL ⇒ `AVG` 为 None ⇒ `:.2f` TypeError。
"""
from __future__ import annotations

import generate_deep_v2 as g


def _deep(pc=None, oi=None):
    det = {"put_call_ratio": pc, "total_oi": oi, "flow_direction": "unknown", "key_levels": {},
           "iv_current": 50.0, "iv_rank": 40.0, "unusual_activity": []}
    return {"ticker": "SYN", "timestamp": "2026-10-19",
            "swarm_results": {"final_score": 5.0, "direction": "neutral",
                              "agent_details": {"OracleBeeEcho": {"score": 5.0, "details": det}}}}


class TestDeepReport:
    def test_oi_fmt(self):
        assert g._oi_fmt(None) == "—" and g._oi_fmt("x") == "—" and g._oi_fmt(1234.0) == "1,234"

    def test_extract_keeps_none_and_shows_dash(self):
        ctx = g.extract(_deep())
        assert ctx["total_oi"] is None and ctx["put_call_ratio"] == "—"

    def test_narratives_do_not_crash_on_none(self):
        """变红的变异：`_build_swarm_narrative` 里的 `{ctx["total_oi"]:,.0f}` 写回去（TypeError）。"""
        ctx = g.extract(_deep())
        html = g._build_swarm_narrative(ctx)
        assert "总OI —" in html
        opt = g._build_options_narrative(ctx)
        assert "近端P/C=—（不可得）" in opt and "多空均衡" not in opt.split("近端P/C=")[1][:20]

    def test_real_values_unchanged(self):
        ctx = g.extract(_deep(pc=0.8, oi=123456))
        assert ctx["put_call_ratio"] == 0.8 and "总OI 123,456" in g._build_swarm_narrative(ctx)


def test_thesis_break_gets_none_not_one():
    """论点失效评估：P/C 不可得 ⇒ 传 None（market_intelligence 记「未评估」），不兜成 1.0 冒充「评估过、没触发」。
    这段在 `generate_deep_v2.main()` 里、没有单独的接缝 ⇒ 源码断言（变红的变异：兜底改回 1.0）。"""
    import inspect
    src = inspect.getsource(g.main)
    assert "_tb_pcr = None   # v0.45.544" in src and "_tb_pcr = 1.0" not in src


class TestChart:
    def _texts(self, monkeypatch, pc, oi):
        import chart_engine as ce
        plt, _, _ = ce._get_mpl()
        figs = []
        orig_close = plt.close
        monkeypatch.setattr(plt, "close", lambda fig=None, *a, **k: (figs.append(fig), orig_close(fig))[1])
        data = {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": {
            "put_call_ratio": pc, "total_oi": oi, "iv_rank": 40.0, "iv_current": 50.0,
            "key_levels": {"support": [{"strike": 95.0, "oi": 1000}], "resistance": [{"strike": 105.0, "oi": 900}]},
            "unusual_activity": []}}}}}
        out = ce.render_options_chart(data, "SYN", "2026-10-19", current_price=100.0)
        assert out, "图表应照常生成"
        from matplotlib.text import Text
        return [t.get_text() for f in figs if f is not None for t in f.findobj(Text)]

    def test_none_pc_is_dash_not_green_bullish(self, monkeypatch):
        """06-17 VKTX / CRCL 形状（call OI 0 ⇒ P/C None，但有关键价位）。变红的变异：P/C 兜回 0.0。"""
        texts = self._texts(monkeypatch, None, None)
        assert "Call偏多 →看涨" not in texts and "不可得" in texts
        assert not any("0.00M" in t for t in texts)

    def test_real_pc_unchanged(self, monkeypatch):
        texts = self._texts(monkeypatch, 0.6, 2_000_000)
        assert "Call偏多 →看涨" in texts and "0.60" in texts and "2.00M" in texts


def test_rival_passes_none_pc_as_missing():
    """板上 OracleBee 的 P/C 取不到 ⇒ RivalBee 的 ML 特征传 None，交给 ml_predictor 列进 imputed_features。
    变红的变异：回落改回 1.0 / 不判 NaN。"""
    import inspect

    from ml_predictor import TrainingData, _feature_quality
    from swarm_agents import rival_bee
    assert rival_bee._usable_pc(None) is None and rival_bee._usable_pc(float("nan")) is None
    assert rival_bee._usable_pc(True) is None and rival_bee._usable_pc(0.8) == 0.8
    src = inspect.getsource(rival_bee)
    assert "_iv_rank, _pc_ratio = 50.0, None" in src and "_pc_ratio = _usable_pc(_pc_raw)" in src
    q = _feature_quality(TrainingData(ticker="SYN", date="2026-10-19", crowding_score=50.0, catalyst_quality="B",
                                      momentum_5d=0.0, volatility=0.3, market_sentiment=0.0, iv_rank=50.0,
                                      put_call_ratio=None, actual_return_3d=0.0, actual_return_7d=0.0,
                                      actual_return_30d=0.0, win_3d=False, win_7d=False, win_30d=False))
    assert "put_call_ratio" in q["imputed_features"]


def test_sample_chain_flow_is_unknown_and_site_hides_it():
    import inspect

    import dashboard_renderer as dr
    import options_analyzer as oa
    src = inspect.getsource(oa.OptionsAgent.analyze)
    assert '"flow_direction": "unknown",' in src and '"flow_direction": "neutral",' not in src
    sd = {"SYN": {"agent_details": {"ScoutBeeNova": {"details": {"price": 10.0}},
                                    "OracleBeeEcho": {"details": {"flow_direction": "unknown"}}}}}
    assert dr._detail("SYN", sd)["flow_dir"] == "-"
    sd["SYN"]["agent_details"]["OracleBeeEcho"]["details"]["flow_direction"] = "bullish"
    assert dr._detail("SYN", sd)["flow_dir"] == "bullish"


def test_backtester_source_guards_null_avgs():
    """`print_report` 的期权段直接查生产库、没有接缝 ⇒ 源码断言。变红的变异：`{…avg_pc_ratio']:.2f}` 写回去。"""
    import inspect

    import backtester as bt
    src = inspect.getsource(bt.Backtester.print_report)
    assert "{opts_row['avg_pc_ratio']:.2f}" not in src and "平均 P/C Ratio: —" in src
