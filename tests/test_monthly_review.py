"""v0.45.678：月度自诊断的两节——「数据质量（本月）」与「预注册检验进度（只计数）」。

由来见 `monthly_review` 模块头（「9 月中旬大复盘」重新定义，用户 2026-10-29）。守四件事：

A. 窗口：昨天所在的自然月、截至昨天（1 日跑 = 整个上月；跨年 / 闰年）。
B. 数据质量：逐交易日汇总覆盖率闸的字段缺失、日健康判定、漏扫、429；判不了 / 未记录如实列出；
   自身失败 ⇒「不可得：原因」，不写成「无降级」。
C. 进度：一项取不到不拖垮其余、写出原因；就绪度文件取不晚于今天的最新一份、过旧标陈旧；
   **盲化**——检验已就绪时月报也不印效应量（策略层优势、LLM 影子各造一个带效应量的「已就绪」结果，断言一个数都没漏出来）。
D. 接线：两节在推理任务（四）之前；单票诊断不出；rank-IC 表带「非协议证据」提示；静态：本模块不碰收益字段。

⚠️ 不用 skip：全部合成数据、tmp 目录、离线；日期一律显式注入（不和真实钟比）。
"""
from __future__ import annotations

import ast
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path

import pytest

import monthly_review as mr
import self_analyst as sa

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

ORACLE_KEYS = ("rv_30d", "iv_rank", "iv_current", "iv_skew_ratio", "put_call_ratio", "iv_rv_spread")
YF_ORACLE = ("rv_30d", "iv_rank", "iv_rv_spread")


# ═══════════════════════════════════════════════════════════════ A. 窗口

@pytest.mark.parametrize("today,start,end", [
    ("2026-11-01", "2026-10-01", "2026-10-31"),     # 每月 1 日：整个上月
    ("2026-10-29", "2026-10-01", "2026-10-28"),     # 月中手动跑：本月到昨天
    ("2027-01-01", "2026-12-01", "2026-12-31"),     # 跨年
    ("2028-03-01", "2028-02-01", "2028-02-29"),     # 闰年二月
    ("2026-11-02", "2026-10-01", "2026-10-31"),     # v0.45.686：定时任务推迟到 2 日也报整个上月（此前是 11-01 ~ 11-01、0 个交易日）
    ("2026-11-07", "2026-10-01", "2026-10-31"),     # 前 LATE_RUN_DAYS 天都算推迟
    ("2026-11-08", "2026-11-01", "2026-11-07"),     # 之后才算月中手动跑
])
def test_report_month(today, start, end):
    s, e = mr.report_month(date.fromisoformat(today))
    assert (s.isoformat(), e.isoformat()) == (start, end)


# ═══════════════════════════════════════════════════════════════ B. 数据质量

def _ticker(*, yf_ok=True, cboe_ok=True, unusual_recorded=True):
    oracle = {k: (1.0 if ((k in YF_ORACLE and yf_ok) or (k not in YF_ORACLE and cboe_ok)) else None)
              for k in ORACLE_KEYS}
    if unusual_recorded:
        oracle["unusual_flow_ok"] = True if yf_ok else None
    chronos = {"analyst_targets": {"mean": 1.0} if yf_ok else {}, "catalysts": ["财报"] if yf_ok else []}
    return {"agent_details": {"OracleBeeEcho": {"details": oracle}, "ChronosBeeHorizon": {"details": chronos}}}


def _write_day(home: Path, d: str, tickers: dict):
    (home / f".swarm_results_{d}.json").write_text(json.dumps(tickers), encoding="utf-8")


@pytest.fixture
def month(tmp_path):
    """today = 10-08 ⇒ 窗口 10-01 ~ 10-07，交易日 10-01 / 02 / 05 / 06 / 07。"""
    home, logs = tmp_path / "home", tmp_path / "logs"
    home.mkdir()
    logs.mkdir()
    n = 12
    _write_day(home, "2026-10-01", {f"T{i}": _ticker() for i in range(n)})
    _write_day(home, "2026-10-02", {f"T{i}": _ticker(yf_ok=False) for i in range(n)})          # yfinance 整天挂
    # 10-05 没有结果文件 ⇒ 漏扫
    _write_day(home, "2026-10-06", {**{f"T{i}": _ticker() for i in range(n - 1)}, "TX": _ticker(cboe_ok=False)})
    _write_day(home, "2026-10-07", {f"T{i}": _ticker(unusual_recorded=False) for i in range(n)})  # 旧代码：异常流没记录
    (logs / "orchestrator-2026-10-01.log").write_text("14:00:01 一切正常\n", encoding="utf-8")
    (logs / "orchestrator-2026-10-02.log").write_text(
        "14:01:00 Too Many Requests\n14:02:00 x\n14:03:00 Too Many Requests\n14:04:00 Too Many Requests\n", encoding="utf-8")
    return home, logs


class TestDataQuality:

    def test_aggregates(self, month):
        home, logs = month
        dq = mr.data_quality(date(2026, 10, 8), home=home, log_dir=logs)
        assert dq["available"], dq
        assert (dq["start"], dq["end"], dq["trading_days"], dq["scanned"]) == ("2026-10-01", "2026-10-07", 5, 4)
        assert dq["missing_scan"] == ["2026-10-05"]
        assert [o["date"] for o in dq["outage"]] == ["2026-10-02"]
        assert set(dq["outage"][0]["sources"]) == {"yfinance"}
        by = {f["field"]: f for f in dq["fields"]}
        assert by["rv_30d"]["missing"] == 12 and by["rv_30d"]["degraded_days"] == ["2026-10-02"]
        assert by["rv_30d"]["worst"] == {"date": "2026-10-02", "have": 0, "total": 12, "missing": 12}
        assert by["iv_current"]["missing"] == 1 and by["iv_current"]["degraded_days"] == []
        assert by["iv_current"]["worst"]["date"] == "2026-10-06"
        assert by["unusual_flow"]["not_recorded_days"] == 1 and by["unusual_flow"]["days"] == 3
        missing = [f["missing"] for f in dq["fields"]]
        assert missing == sorted(missing, reverse=True), "表要按缺失（日·票）降序"
        assert dq["rate_limit"] == {"total": 3, "days": [("2026-10-02", 3)],
                                    "unknown_days": ["2026-10-05", "2026-10-06", "2026-10-07"]}

    def test_rate_limit_counted_on_missed_scan_days(self, month):
        """v0.45.686：漏扫的日子最可能正是被限流打挂的那天——此前它直接跳过、429 不进合计也不进「没日志可数」。"""
        home, logs = month
        (logs / "orchestrator-2026-10-05.log").write_text("14:01:00 Too Many Requests\n" * 500, encoding="utf-8")
        dq = mr.data_quality(date(2026, 10, 8), home=home, log_dir=logs)
        assert dq["missing_scan"] == ["2026-10-05"]
        assert dq["rate_limit"]["total"] == 503 and ("2026-10-05", 500) in dq["rate_limit"]["days"]

    def test_render(self, month):
        home, logs = month
        txt = "\n".join(mr.render_data_quality(mr.data_quality(date(2026, 10, 8), home=home, log_dir=logs)))
        assert "## 三.五、数据质量" in txt
        assert "漏扫 1 天**（10-05）" in txt
        assert "数据源故障日 1 天：10-02（yfinance×" in txt
        assert "合计 3 次" in txt and "3 天没有日志可数" in txt
        assert "## 三.五、数据质量（2026-10-01 ~ 2026-10-07，不看收益）" in txt
        assert "| rv_30d | yfinance 日K | 12 | 1（10-02） | 10-02（0/12） |" in txt
        assert "1 天未记录" in txt

    def test_undeterminable_day_is_listed(self, month):
        home, logs = month
        (home / ".swarm_results_2026-10-05.json").write_text("{}", encoding="utf-8")      # 有文件但空
        dq = mr.data_quality(date(2026, 10, 8), home=home, log_dir=logs)
        assert dq["missing_scan"] == [] and [u["date"] for u in dq["undetermined"]] == ["2026-10-05"]
        assert "判不了 1 天" in "\n".join(mr.render_data_quality(dq))

    def test_small_sample_days_named_and_kept_out_of_field_table(self, month):
        """v0.45.686：标的不足日健康下限的日子判不了 ⇒ 不进字段表（小样本的「降级」没意义），且每一天都点名。"""
        home, logs = month
        _write_day(home, "2026-10-05", {"A": _ticker(yf_ok=False), "B": _ticker(yf_ok=False)})   # 2 只，全缺
        (home / ".swarm_results_2026-10-07.json").write_text("{坏", encoding="utf-8")
        dq = mr.data_quality(date(2026, 10, 8), home=home, log_dir=logs)
        assert [u["date"] for u in dq["undetermined"]] == ["2026-10-05", "2026-10-07"]
        by = {f["field"]: f for f in dq["fields"]}
        assert "2026-10-05" not in by["rv_30d"]["degraded_days"] and by["rv_30d"]["missing"] == 12
        txt = "\n".join(mr.render_data_quality(dq))
        assert "10-05：" in txt and "10-07：" in txt and "不计入下表" in txt

    def test_zero_trading_days_warns(self, tmp_path):
        """窗口里一个交易日都没有 ⇒ 写明，不渲染成「无漏扫」的健康样子。"""
        dq = {"available": True, "start": "2026-12-25", "end": "2026-12-25", "trading_days": 0, "scanned": 0,
              "missing_scan": [], "outage": [], "degraded_days": [], "undetermined": [], "fields": [],
              "rate_limit": {"total": 0, "days": [], "unknown_days": []}}
        assert "一个交易日都没有" in "\n".join(mr.render_data_quality(dq))

    def test_own_failure_is_visible(self, month, monkeypatch):
        import scan_coverage_gate

        def boom(*a, **k):
            raise OSError("盘坏了")
        monkeypatch.setattr(scan_coverage_gate, "check", boom)
        home, logs = month
        dq = mr.data_quality(date(2026, 10, 8), home=home, log_dir=logs)
        assert dq == {"available": False, "reason": "OSError: 盘坏了"}
        txt = "\n".join(mr.render_data_quality(dq))
        assert "不可得" in txt and "不代表这个月没有降级" in txt


# ═══════════════════════════════════════════════════════════════ C. 进度

def _ok(line, as_of=None):
    return lambda log_dir, today: {"line": line, **({"as_of": as_of} if as_of else {})}


def _boom(log_dir, today):
    raise RuntimeError("boom")


class TestProgress:

    def test_one_failure_does_not_hide_the_rest(self):
        rows = mr.prereg_progress(date(2026, 10, 8), sources=[("甲", _ok("3/10 周")), ("乙", _boom), ("丙", _ok("a|b"))])
        assert [r["error"] for r in rows] == [None, "RuntimeError: boom", None]
        txt = "\n".join(mr.render_progress(rows))
        assert "⚠️ 1 项取不到进度" in txt
        assert "| 乙 | ⚠️ 不可得：RuntimeError: boom |" in txt
        assert "| 丙 | a／b |" in txt, "表格里的 | 要换掉，否则 Markdown 表错位"

    def test_newline_in_error_stays_on_one_row(self):
        def nl(log_dir, today):
            raise RuntimeError("第一行\n第二行")
        txt = "\n".join(mr.render_progress(mr.prereg_progress(date(2026, 10, 8), sources=[("甲", nl)])))
        assert "| 甲 | ⚠️ 不可得：RuntimeError: 第一行 第二行 |" in txt

    def test_stale_readiness_marked(self):
        rows = mr.prereg_progress(date(2026, 10, 8), sources=[("新", _ok("x", "2026-10-07")), ("旧", _ok("y", "2026-10-01"))])
        assert [r["stale"] for r in rows] == [False, True]
        txt = "\n".join(mr.render_progress(rows))
        assert "x（10-07 的就绪度）" in txt and "陈旧：最新就绪度停在 2026-10-01" in txt

    def test_readiness_json_latest_not_after_today(self, tmp_path):
        for d, line in (("2026-10-05", "旧"), ("2026-10-06", "用这份"), ("2026-10-09", "未来")):
            (tmp_path / f"ic_rerun_readiness-{d}.json").write_text(json.dumps({
                "dim_ic_forward_test": {"status": "not_ready", "line": line}, "weeks_accrued": 2, "weeks_required": 25,
                "eta_date": "2027-01-20", "ready": False, "status": "ok"}), encoding="utf-8")
        r = mr._from_readiness("dim_ic_forward_test", "x.py")(tmp_path, date(2026, 10, 8))
        assert r == {"line": "用这份", "as_of": "2026-10-06"}
        assert mr._ic_rerun(tmp_path, date(2026, 10, 8))["line"] == "2/25 个合格周，预计 2027-01-20"
        with pytest.raises(KeyError):
            mr._from_readiness("resonance_forward_test", "x.py")(tmp_path, date(2026, 10, 8))
        with pytest.raises(FileNotFoundError):
            mr._from_readiness("dim_ic_forward_test", "x.py")(tmp_path / "空", date(2026, 10, 8))

    @staticmethod
    def _readiness(tmp_path, seg=None, **top):
        doc = {"weeks_accrued": 2, "weeks_required": 25, "status": "ok", **top}
        if seg is not None:
            doc["dim_ic_forward_test"] = seg
        (tmp_path / "ic_rerun_readiness-2026-10-07.json").write_text(json.dumps(doc), encoding="utf-8")

    @pytest.mark.parametrize("status,line", [
        ("in_progress", "🔔 维度 IC：已过 中期 检视（H1=significant_negative）；H1 合格周 13/26"),
        ("concluded", "✅ 维度 IC：已出结论：H1=significant_negative，H2=not_significant"),
        ("confirmed", "✅ 共振加成：确认（Δ=+0.031，p=0.004）"),
        ("not_confirmed", "共振加成：未确认（p=0.21）"),
        ("insample", "样本内 Δ=+0.05"),
        ("some_future_status", "H1=significant_positive"),
    ])
    def test_readiness_verdicts_never_copied(self, tmp_path, status, line):
        """v0.45.686：上游进度行过了检视点就写结论 ⇒ 只有 not_ready 原样抄，其余（含没见过的状态）一律改写成指路。"""
        self._readiness(tmp_path, {"status": status, "line": line})
        r = mr._from_readiness("dim_ic_forward_test", "experiments/dim_ic_forward_test.py")(tmp_path, date(2026, 10, 8))
        for leak in ("significant", "Δ", "p=", "确认", "结论：", "13/26"):
            assert leak not in r["line"], f"月报转述了 {status} 的结论 {leak!r}：{r['line']}"
        assert status in r["line"] and "experiments/dim_ic_forward_test.py" in r["line"]

    def test_readiness_cannot_judge_is_an_error_row(self, tmp_path):
        self._readiness(tmp_path, {"status": "cannot_judge", "line": "⚠️ F&G：进程在 F&G 重放结束前终止"})
        rows = mr.prereg_progress(date(2026, 10, 8), log_dir=tmp_path,
                                  sources=[("甲", mr._from_readiness("dim_ic_forward_test", "x.py"))])
        assert rows[0]["error"] and "判不了（cannot_judge）" in rows[0]["error"]

    @pytest.mark.parametrize("top", [
        {"status": "undetermined", "reason": "找不到 pheromone.db", "weeks_accrued": None, "weeks_required": None},
        {"status": "error", "reason": "崩了"},
        {"weeks_accrued": None},
        {"weeks_required": True},
    ])
    def test_ic_rerun_without_numbers_is_an_error(self, tmp_path, top):
        """v0.45.686：外壳判不了 / 合格周数缺失时此前渲染成「None/None 个合格周」、不计入取不到。"""
        self._readiness(tmp_path, None, **top)
        rows = mr.prereg_progress(date(2026, 10, 8), log_dir=tmp_path, sources=[("IC 重跑", mr._ic_rerun)])
        assert rows[0]["error"] and rows[0]["line"] is None

    def test_strategy_edge_ready_does_not_leak_effect(self, monkeypatch):
        from experiments import strategy_edge_test as se
        monkeypatch.setattr(se, "assess", lambda as_of=None: {
            "status": "ready", "cohort": {"date": "2026-10-19"}, "n_units": 52, "n_units_required": 52,
            "n_trades": 300, "mean_pct": 0.4321, "t": 2.718, "p": 0.0042, "decision": "检出优势：放大仓位"})
        line = mr._strategy_edge(None, date(2027, 10, 20))["line"]
        assert "已就绪" in line and "52/52 周" in line
        for leak in ("0.4321", "2.718", "0.0042", "检出优势", "放大"):
            assert leak not in line, f"月报漏出了效应量 {leak!r}：{line}"

    def test_strategy_edge_undetermined_is_an_error_row(self, monkeypatch):
        from experiments import strategy_edge_test as se
        monkeypatch.setattr(se, "assess", lambda as_of=None: {"status": "undetermined", "reason": "无已验证预测数据"})
        rows = mr.prereg_progress(date(2026, 10, 8), sources=[("策略层优势", mr._strategy_edge)])
        assert rows[0]["error"] == "RuntimeError: 无法判定：无已验证预测数据"

    def test_llm_shadow_ready_does_not_leak_result(self, monkeypatch, tmp_path):
        import llm_shadow_report as lr
        import llm_shadow_scan
        (tmp_path / lr.STATUS_DIRNAME).mkdir()
        monkeypatch.setattr(llm_shadow_scan, "default_shadow_root", lambda home: tmp_path)
        monkeypatch.setattr(lr, "build_report", lambda root, db, today=None: {
            "describe": {"generation": "g2", "healthy_days": 25, "spend_usd": 70.0, "missing_run_days": [],
                         "mean_score_diff": 0.777, "direction_flip_rate": 0.3141},
            "discordant_decided": 64, "thresholds": {"min_healthy_days": 20, "min_discordant": 60},
            "ready": True, "result": {"llm_only_correct": 37, "rule_only_correct": 19, "mcnemar_p": 0.0123}})
        line = mr._llm_shadow(None, date(2026, 12, 1))["line"]
        assert "已就绪" in line and "健康日 25/20" in line and "64/60" in line
        for leak in ("37", "19", "0.0123", "0.777", "0.3141"):
            assert leak not in line, f"月报漏出了 LLM 影子的结果 {leak!r}：{line}"

    def test_sell_strike_row_only_points_elsewhere(self):
        """卖权火墙（`tests/test_sell_strike_integration.py`）：月报既不 import 卖权模块、也不碰它的状态目录——
        这一行只指路，不取数、不报错（那两条火墙测试会在本模块越界时变红）。"""
        r = mr._sell_strike(None, date(2026, 10, 8))
        assert "火墙" in r["line"] and "账本就绪度" in r["line"] and "as_of" not in r

    def test_missing_ledger_dirs_are_errors_not_zeros(self, monkeypatch, tmp_path):
        """v0.45.686：数据根配错时三个进度函数都返回空列表 ⇒ 此前渲染成「0/60」「0/30」「健康日 0/20」。"""
        import llm_shadow_scan
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "空根"))
        monkeypatch.setattr(llm_shadow_scan, "default_shadow_root", lambda home: tmp_path / "空影子根")
        rows = mr.prereg_progress(date(2026, 10, 8), sources=[
            ("跨式 × GEX", mr._straddle_gex), ("宽跨影子", mr._strangle_shadow), ("LLM 影子", mr._llm_shadow)])
        assert all(r["error"] and "不存在" in r["error"] for r in rows), rows

    def test_count_only_formatters(self, monkeypatch, tmp_path):
        import straddle_gex_prereg as sg
        import straddle_strangle_shadow as ss
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
        (tmp_path / "options_paper_state").mkdir()
        monkeypatch.setattr(sg, "_load_rows", lambda: [])
        monkeypatch.setattr(sg, "progress", lambda rows: {
            "n_events": 32, "n_with_gex": 8, "n_eligible_settled": 1, "ready": False, "frozen": False,
            "n_informative": {"positive_gex": 0, "negative_gex": 0, "blocks": 0},
            "need": {"min_units": 60, "min_per_group": 20, "min_informative_blocks": 8}})
        assert "已结算合格 1/60" in mr._straddle_gex(None, date(2026, 10, 8))["line"]
        monkeypatch.setattr(ss, "_load", lambda root, name: [])
        monkeypatch.setattr(ss, "progress", lambda closed, positions: {
            "n_units": 30, "n_blocks": 8, "n_closed": 40, "n_open_recorded": 2, "ready": True, "frozen": True,
            "need": {"min_units": 30, "min_blocks": 8}})
        line = mr._strangle_shadow(None, date(2026, 10, 8))["line"]
        assert "合格单位 30/30" in line and "已就绪" in line and "已冻结" in line

    def test_every_registered_source_is_called(self):
        """登记表里每一项都得真被调用（没有一项悄悄不出现）：全部换成会抛的桩，行数 = 登记数、每行都有原因。"""
        srcs = [(name, _boom) for name, _fn in mr.PROGRESS_SOURCES]
        rows = mr.prereg_progress(date(2026, 10, 8), sources=srcs)
        assert [r["name"] for r in rows] == [n for n, _ in mr.PROGRESS_SOURCES]
        assert all(r["error"] for r in rows)
        assert len(mr.PROGRESS_SOURCES) >= 8


# ═══════════════════════════════════════════════════════════════ D. 接线 / 静态

def _brief(**kw):
    stats = {"total": 0, "correct": 0, "wrong": 0, "win_rate": 0.0, "avg_ret_7d": 0.0}
    return sa.format_briefing(stats, {}, [], 3, None, **kw)


class TestWiring:

    def test_sections_before_tasks(self):
        dq = {"available": False, "reason": "测试"}
        txt = _brief(data_quality=dq, prereg_progress=[{"name": "甲", "line": "1/2", "as_of": None, "stale": False, "error": None}])
        i, j, k = txt.index("## 三.五、数据质量"), txt.index("## 三.六、预注册检验进度"), txt.index("## 四、分析任务")
        assert i < j < k
        assert "### 任务 5：数据质量 → 修复待办" in txt and "不要从中推断任何效应" in txt

    def test_sections_absent_when_not_given(self):
        txt = _brief()
        assert "三.五" not in txt and "三.六" not in txt

    def test_rank_ic_table_carries_protocol_note(self):
        from experiments.dim_ic_protocol import FORWARD_START
        txt = _brief(dimension_ic={"ScoutBeeNova": {"ic": 0.01, "n": 50, "ic_recent": None, "n_recent": 5}})
        assert f"窗口含 {FORWARD_START} 之后" in txt and "不是协议证据" in txt

    def _snap(self, home):
        snaps = home / "report_snapshots"
        snaps.mkdir()
        now = datetime.now()
        (snaps / "T0.json").write_text(json.dumps({
            "ticker": "T0", "date": now.strftime("%Y-%m-%d"), "created_at": now.isoformat(),
            "direction": "bullish", "entry_price": 100.0, "actual_prices": {"t7": 101.0}}), encoding="utf-8")

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        h = tmp_path / "data_root"
        h.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(h))
        for name in ("ALPHAHIVE_DIR", "SNAPSHOTS_DIR", "BRIEFS_DIR"):
            monkeypatch.setattr(sa, name, None)
        monkeypatch.setattr(mr, "_log_dir", lambda log_dir=None: tmp_path / "logs")   # 不读本机真实的编排器日志
        return h

    def test_main_writes_both_sections(self, home, monkeypatch):
        self._snap(home)
        monkeypatch.setattr(mr, "PROGRESS_SOURCES", [("甲", _ok("5/10 周")), ("乙", _boom)])
        monkeypatch.setattr(sys, "argv", ["self_analyst.py"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            sa.main()
        out = buf.getvalue()
        assert "预注册进度: 2 项，⚠️ 1 项取不到" in out, out
        brief = next((home / "self_analysis_briefs").glob("self_analysis_*.md")).read_text(encoding="utf-8")
        assert "## 三.五、数据质量" in brief and "| 甲 | 5/10 周 |" in brief and "不可得：RuntimeError: boom" in brief

    def test_single_ticker_run_skips_both(self, home, monkeypatch):
        self._snap(home)

        def must_not_run(*a, **k):
            raise AssertionError("单票诊断不该出月度两节")
        monkeypatch.setattr(mr, "data_quality", must_not_run)
        monkeypatch.setattr(mr, "prereg_progress", must_not_run)
        monkeypatch.setattr(sys, "argv", ["self_analyst.py", "--ticker", "T0"])
        with redirect_stdout(io.StringIO()):
            sa.main()


#: 收益 / 结果类字段与「算效应量」的入口：本模块一个都不许碰（数据质量不看收益，进度只取计数）
OUTCOME_TOKENS = ("close_t7", "return_t7", "price_t7", "price_t30", "correct_t", "actual_prices", "report_snapshots",
                  "mean_pct", "score_discordant", "mcnemar", "evaluate", "run_once")
#: 只按整词认的键（子串会误伤 `.swarm_results_` 文件名）：LLM 影子报告里装结果的那一格
OUTCOME_EXACT = ("result",)


def _outcome_hits(tree: ast.AST) -> list:
    doc_ids = {id(n.body[0].value) for n in ast.walk(tree)
               if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
               and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    hits = []
    for n in ast.walk(tree):
        texts = []
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            texts.append(n.value)
        elif isinstance(n, ast.Name):
            texts.append(n.id)
        elif isinstance(n, ast.Attribute):
            texts.append(n.attr)
        hits += [f"L{getattr(n, 'lineno', '?')}:{t[:40]!r}" for t in texts
                 if any(tok in t for tok in OUTCOME_TOKENS) or t in OUTCOME_EXACT]
    return hits


class TestNoOutcomes:

    def test_module_never_touches_outcomes(self):
        tree = ast.parse((REPO_ROOT / "monthly_review.py").read_text(encoding="utf-8"))
        assert not _outcome_hits(tree), _outcome_hits(tree)

    @pytest.mark.parametrize("lesion", [
        'x = row["close_t7"]\n', 'x = rep["result"]\n', 'x = se.evaluate(units)\n', 'x = r.get("mean_pct")\n',
    ])
    def test_detector_has_teeth(self, lesion):
        assert _outcome_hits(ast.parse(lesion))
