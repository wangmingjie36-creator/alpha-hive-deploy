"""v0.45.57 —— 失效条件必须真的进报告。

守的是三件事，每一件都对应一个已经发生过的真实故障：

1. **顺序**：计算必须在 `_build_swarm_report` 之前。
   v0.45.55 修好了 KeyError，但计算住在 `_post_scan_notify` 里，
   而报告在它前一行就渲染完了 —— 修对了一个没有读者的字段。
2. **列名不撒谎**：7 节表格最后一列曾标题写「失效条件」、内容是
   GuardBee 的「共振✅ 5 Agent 同向」，即论点**成立**的理由。
3. **全灭要吵**：0 覆盖必须留 ERROR，不能只是安静地渲染一片空白。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import report_formatters as rf  # noqa: E402


def _row(ticker, l1=None, l2=None, discovery="共振✅ 5 Agent 同向 | 一致性 60%"):
    return (ticker, {
        "resonance": {"resonance_detected": True},
        "agent_breakdown": {"bullish": 5, "bearish": 3, "neutral": 0},
        "data_real_pct": 96,
        "final_score": 6.8,
        "direction": "bullish",
        "agent_details": {"GuardBeeSentinel": {"discovery": discovery}},
        **({"thesis_break_l1": l1} if l1 is not None else {}),
        **({"thesis_break_l2": l2} if l2 is not None else {}),
    })


# ── 1. 顺序不变式 ────────────────────────────────────────────────
def test_attach_runs_before_report_is_built():
    """扫描主流程里，_attach_thesis_breaks 必须早于 _build_swarm_report。

    这是 v0.45.57 的核心。若有人把 attach 调用挪回 _post_scan_notify，
    报告会重新变成一片空白而单测其余部分照样全绿 —— 所以要直接盯源码顺序。
    """
    import alpha_hive_daily_report as ahdr
    src = inspect.getsource(ahdr.AlphaHiveDailyReporter.run_swarm_scan)
    i_attach = src.find("_attach_thesis_breaks")
    i_build = src.find("_build_swarm_report")
    assert i_attach != -1, "run_swarm_scan 不再调用 _attach_thesis_breaks"
    assert i_build != -1
    assert i_attach < i_build, (
        "失效条件计算跑在建报告之后 —— 渲染层看不到它，"
        "这正是 v0.45.55 修完仍然 0/30 的原因"
    )


def test_attach_runs_before_swarm_results_dumped():
    """也必须早于 _post_scan_enrichment —— 它会把 .swarm_results_*.json 落盘。"""
    import alpha_hive_daily_report as ahdr
    src = inspect.getsource(ahdr.AlphaHiveDailyReporter.run_swarm_scan)
    assert src.find("_attach_thesis_breaks") < src.find("_post_scan_enrichment")


def test_attach_writes_both_levels(monkeypatch):
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig

    monkeypatch.setattr(ThesisBreakConfig, "_data", {
        "AAA": {
            "level_1_warning": {"conditions": [{"metric": "M", "trigger": "T"}]},
            "level_2_stop_loss": {"conditions": [
                {"field": "score", "op": "<", "value": 4.0, "_machine": True}]},
        }
    })
    rep = ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)
    sr = {"AAA": {}, "BBB": {}}
    rep._attach_thesis_breaks(sr)
    assert sr["AAA"]["thesis_break_l1"] == ["M：T"]
    assert sr["AAA"]["thesis_break_l2"] == ["score：score < 4.0（自动）"]
    assert "thesis_break_l1" not in sr["BBB"], "无配置的标的不应被塞空列表冒充有覆盖"


def test_attach_one_bad_condition_does_not_kill_the_rest(monkeypatch):
    """逐条隔离：一条读不懂不得连坐同一标的的其余条件。"""
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig

    monkeypatch.setattr(ThesisBreakConfig, "_data", {
        "AAA": {"level_1_warning": {"conditions": [
            {"metric": "M1", "trigger": "T1"},
            {"完全陌生的第三种": "schema"},
            {"metric": "M2", "trigger": "T2"},
        ]}}
    })
    rep = ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)
    sr = {"AAA": {}}
    rep._attach_thesis_breaks(sr)
    assert sr["AAA"]["thesis_break_l1"] == ["M1：T1", "M2：T2"]


def test_attach_screams_when_coverage_is_zero(monkeypatch, caplog):
    import logging
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig

    monkeypatch.setattr(ThesisBreakConfig, "_data", {})
    rep = ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)
    with caplog.at_level(logging.ERROR, logger="alpha_hive.daily_report"):
        rep._attach_thesis_breaks({"AAA": {}, "BBB": {}})
    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "失效条件全灭必须留 ERROR —— 8/27 那次只有一行 WARNING，没人看见"


# ── 2. 渲染 ──────────────────────────────────────────────────────
def test_column_no_longer_mislabels_resonance_as_thesis_break():
    md = "\n".join(rf._build_composite_judgment([_row("META", ["A：a"], ["B：b"])]))
    header = [ln for ln in md.splitlines() if ln.startswith("| 标的")][0]
    assert "共振判定" in header
    data_line = [ln for ln in md.splitlines() if ln.startswith("| **META**")][0]
    cells = [c.strip() for c in data_line.split("|")]
    # 「共振✅ 5 Agent 同向」必须落在共振判定列，不能落在失效条件列
    cols = [c.strip() for c in header.split("|")]
    assert cells[cols.index("失效条件")].startswith("L1×")
    assert "共振" in cells[cols.index("共振判定")]


def test_real_conditions_are_rendered():
    md = "\n".join(rf._build_thesis_breaks(
        [_row("META", ["DAU 增速：QoQ < 1%"], ["广告收入：YoY < 0%"])]))
    assert "DAU 增速：QoQ < 1%" in md
    assert "广告收入：YoY < 0%" in md
    assert "L1 预警" in md and "L2 止损" in md


def test_missing_config_is_named_not_hidden():
    md = "\n".join(rf._build_thesis_breaks([_row("AMC", [], [])]))
    assert "AMC" in md and "无失效条件配置" in md


def test_l1_only_ticker_still_renders():
    md = "\n".join(rf._build_thesis_breaks([_row("T", ["只有一级"], [])]))
    assert "只有一级" in md
    assert "L2 止损" not in md
    assert "无失效条件配置" not in md


def test_section_survives_absent_keys():
    """老的 .swarm_results 里根本没有这两个 key —— 重渲染历史报告不能崩。"""
    md = "\n".join(rf._build_composite_judgment([_row("NVDA")]))
    assert "⚠️ 无" in md


# ── 3. 真配置端到端 ──────────────────────────────────────────────
def test_real_config_renders_for_every_watchlist_ticker():
    """真配置 + 真 WATCHLIST：30 只必须全部拿到条件。

    出现第四种 schema、或有人删了标的条目时，这条先红。
    """
    import alpha_hive_daily_report as ahdr
    import config
    from thesis_breaks import ThesisBreakConfig

    ThesisBreakConfig._reset_cache()
    rep = ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)
    sr = {t: {} for t in config.WATCHLIST}
    rep._attach_thesis_breaks(sr)
    empty = [t for t, v in sr.items()
             if not (v.get("thesis_break_l1") or v.get("thesis_break_l2"))]
    assert not empty, f"这些标的没有可渲染的失效条件：{empty}"


# ── v0.45.62：阈值旁边必须有当前值与判定 ──────────────────────────
def _row_with_data(ticker, iv=None, pcr=None, score=5.0, bear=0):
    return (ticker, {
        "final_score": score,
        "agent_details": {
            "OracleBeeEcho": {"details": {
                "iv_current": iv, "put_call_ratio": pcr,
                "_snapshot_stock_price": 100.0}},
            "BearBeeContrarian": {"details": {"bearish_signals": ["x"] * bear}},
        },
    })


def _attach(monkeypatch, cfg, rows):
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig
    import market_intelligence as mi
    monkeypatch.setattr(ThesisBreakConfig, "_data", cfg)
    monkeypatch.setattr(mi, "_BASE", __import__("pathlib").Path(
        __import__("alpha_hive_daily_report").__file__).parent)
    rep = ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)
    rep._attach_thesis_breaks(rows)
    return rows


_CFG_ONE = {"AAA": {"level_1_warning": {"conditions": [
    {"field": "put_call_ratio", "op": ">", "value": 1.59,
     "_machine": True, "_note": "看跌持仓进入 p98"}]}}}


def test_verdict_shows_current_value_and_not_fired(monkeypatch, tmp_path):
    """静态阈值表 → 可判读的核对结果。缺了当前值，读者分不清
    「没触发」和「不可能触发」——这正是 8/27 网页上的样子。"""
    import json
    import market_intelligence as mi
    (tmp_path / "thesis_breaks_config.json").write_text(
        json.dumps(_CFG_ONE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mi, "_BASE", tmp_path)
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig
    monkeypatch.setattr(ThesisBreakConfig, "_data", _CFG_ONE)
    rows = dict([_row_with_data("AAA", pcr=1.09)])
    ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)._attach_thesis_breaks(rows)
    line = rows["AAA"]["thesis_break_l1"][0]
    assert "1.59" in line, "阈值必须还在"
    assert "当前 1.09" in line, "当前值必须打出来"
    assert "未触发" in line


def test_verdict_marks_fired(monkeypatch, tmp_path):
    import json
    import market_intelligence as mi
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig
    (tmp_path / "thesis_breaks_config.json").write_text(
        json.dumps(_CFG_ONE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mi, "_BASE", tmp_path)
    monkeypatch.setattr(ThesisBreakConfig, "_data", _CFG_ONE)
    rows = dict([_row_with_data("AAA", pcr=2.4)])
    ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)._attach_thesis_breaks(rows)
    assert "✅ 已触发" in rows["AAA"]["thesis_break_l1"][0]


def test_missing_value_is_not_reported_as_safe(monkeypatch, tmp_path):
    """当前值缺失必须说「未核对」，**不能**渲染成「未触发」。

    这是本仓库最常见的故障形状：缺数据被读成「没事」。"""
    import json
    import market_intelligence as mi
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig
    (tmp_path / "thesis_breaks_config.json").write_text(
        json.dumps(_CFG_ONE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mi, "_BASE", tmp_path)
    monkeypatch.setattr(ThesisBreakConfig, "_data", _CFG_ONE)
    rows = dict([_row_with_data("AAA", pcr=None)])
    ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)._attach_thesis_breaks(rows)
    line = rows["AAA"]["thesis_break_l1"][0]
    assert "未核对" in line and "未触发" not in line


def test_evaluation_order_matches_config_order():
    """求值明细必须与配置逐位同序 —— 渲染层按位置贴当前值。

    顺序一旦漂了，A 条件的阈值会配上 B 条件的当前值，
    而两条都「看起来正常」。所以直接盯契约。
    """
    import json
    import market_intelligence as mi
    cfg = json.loads((__import__("pathlib").Path(mi.__file__).parent
                      / "thesis_breaks_config.json").read_text(encoding="utf-8"))
    tk = "NVDA"
    res = mi.check_thesis_breaks(tk, 100.0, 30.0, 1.0, [], 5.0)
    evs = res["evaluations"]
    expected = []
    for lk in ("level_1_warning", "level_2_stop_loss"):
        for c in (cfg[tk].get(lk, {}) or {}).get("conditions", []) or []:
            expected.append((lk, c.get("field"), c.get("value")))
    assert [(e["level"], e["field"], e["value"]) for e in evs] == expected


def test_evaluations_present_on_every_return_path():
    """三条 return 路径都要带 evaluations —— 不留给调用方 .get 兜默认值。"""
    import market_intelligence as mi
    for args in (("ZZZ_NOT_IN_CONFIG", 100.0, 30.0, 1.0, [], 5.0),
                 ("NVDA", 100.0, 30.0, 1.0, [], 5.0),
                 ("NVDA", 100.0, 30.0, 9.9, [], 0.1)):
        assert "evaluations" in mi.check_thesis_breaks(*args)


def test_misaligned_evaluations_degrade_instead_of_mislabel(monkeypatch, tmp_path):
    """长度对不上时退回「只有阈值」，绝不把 A 的当前值贴到 B 上。"""
    import json
    import market_intelligence as mi
    import alpha_hive_daily_report as ahdr
    from thesis_breaks import ThesisBreakConfig
    cfg = {"AAA": {"level_1_warning": {"conditions": [
        {"field": "put_call_ratio", "op": ">", "value": 1.59, "_machine": True},
        {"field": "score", "op": "<", "value": 4.0, "_machine": True}]}}}
    (tmp_path / "thesis_breaks_config.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(mi, "_BASE", tmp_path)
    monkeypatch.setattr(ThesisBreakConfig, "_data", cfg)
    monkeypatch.setattr(ahdr.AlphaHiveDailyReporter, "_evaluate_thesis_breaks",
                        staticmethod(lambda t, r: [{"level": "level_1_warning",
                                                    "field": "score", "op": "<",
                                                    "value": 4.0, "actual": 9.9,
                                                    "fired": False, "machine": True}]))
    rows = dict([_row_with_data("AAA", pcr=1.09, score=9.9)])
    ahdr.AlphaHiveDailyReporter.__new__(ahdr.AlphaHiveDailyReporter)._attach_thesis_breaks(rows)
    for line in rows["AAA"]["thesis_break_l1"]:
        assert "当前" not in line, f"对不齐时不该贴当前值：{line}"


def test_section_summary_counts_fired():
    md = "\n".join(rf._build_thesis_breaks([
        _row("AAA", ["a：x（当前 1，✅ 已触发）", "b：y（当前 2，未触发）"], []),
        _row("BBB", ["c：z（当前值缺失，未核对）"], []),
    ]))
    assert "共 **3** 条" in md and "**1** 条当前满足" in md
    assert "**1** 条因缺当前值未核对" in md


def test_prose_conditions_are_not_counted_as_safe():
    """全是人工条件的日子（如 8/26 口径），汇总不能读成「都没事」。"""
    md = "\n".join(rf._build_thesis_breaks([
        _row("META", ["DAU：QoQ < 1%（人工条件，未自动核对）"], [])]))
    assert "人工条件" in md and "需人工判读" in md
    assert "可自动核对" not in md, "没有自动条件时不该报可自动核对条数"


def test_uncalibrated_caveat_only_when_auto_conditions_exist():
    prose_only = "\n".join(rf._build_thesis_breaks([
        _row("META", ["DAU：QoQ < 1%（人工条件，未自动核对）"], [])]))
    assert "阈值未校准" not in prose_only
    with_auto = "\n".join(rf._build_thesis_breaks([
        _row("AMC", ["IV 进入 p98：iv > 105.7（自动）（当前 83.42，未触发）"], [])]))
    assert "阈值未校准" in with_auto
    assert "你是哪只票" in with_auto, "要说清阈值测的是什么，不能只说「未校准」"
