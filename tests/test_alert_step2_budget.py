"""性能告警 = Step 2 用掉它自己超时预算的比例（v0.45.569）——取代恒真的 5 秒常数基线。

事故：`AlertAnalyzer.__init__(perf_baseline_seconds=5.0)`，生产 `alert_manager.main()` 不传参 ⇒ 缺省值就是生产值；
判据 `total_duration_seconds > 5.0 × 1.5`，而真实扫描 1200–3800 秒 ⇒ 09-22 起每个跑完的扫描日都出
「⚠️ 【P1 高】性能异常」（10-05…10-20 的 alerts-*.json 全有，「性能下降」20000–76000%）。天天红等于没人红：
恒真闸把人训练成忽略 P1，真出事那天的 P1 也会被一起忽略。

本文件四组，各堵一个方向：
  1. 真实日回放：20 个真实扫描日只有 10-12（唯一擦边日）响；同一份夹具对旧闸**全部**为真
     ⇒ 尺子自己有牙，谁再把秒数常数基线接回来（比总耗时或比 Step 2 耗时都一样）这里立刻红。
  2. 正对照：真慢的日子照样响（09-23 外推、边界、超时没落 P0、跑到一半崩掉、退出码 1 跑完）。
  3. 没查成 ≠ 没问题：预算字段缺 / null / 非数 ⇒ checks_skipped；Step 2 根本没跑 ⇒ 不记。
  4. 告警线与编排器真实预算两头焊住（离正常日太近 ⇒ 恒真；不低于超时 ⇒ 恒假，被杀前永远不响），
     并用真 `write_status` heredoc 写出 status.json 再读（写入者与读者同形）。需要 `/bin/bash` 与 `jq`。
"""
import inspect
import json
import os
import re
import subprocess

import pytest

import alert_manager as am
from tests._orchestrator import repo_orchestrator_text, step2_budget, step2_budget_formula

#: 真实 30 只扫描日：(日期, Step 6 时的 total_duration_seconds, Step 2 耗时, 当天 Step 2 超时预算, 新闸是否该响)。
#: 出处：`~/.claude/logs/orchestrator-<日期>.log` 的「Step 2 预算余量：H s / T s」（耗时 = T − H）与
#: `alerts-<日期>.json` 旧 P1 的「实际耗时」（= Step 6 读到的总耗时）。09-21 / 09-23 两天超时被杀、status=failed
#: 走 P0，不在此列；10-16 起不再有 Slack 确认的 ~600 秒空等（总耗时 ≈ Step 2）；10-19 起预算 120 → 180 秒/只。
REAL_DAYS = [
    ("2026-09-22", 3059, 2435, 3600, False),
    ("2026-09-24", 3376, 2661, 3600, False),   # yfinance 429 风暴（78 行），74%——慢，但没逼近预算
    ("2026-09-25", 2810, 2198, 3600, False),
    ("2026-09-28", 2856, 2232, 3600, False),
    ("2026-09-29", 2940, 2305, 3600, False),
    ("2026-09-30", 2699, 2061, 3600, False),
    ("2026-10-01", 2859, 2225, 3600, False),
    ("2026-10-02", 2735, 2097, 3600, False),
    ("2026-10-05", 2900, 2284, 3600, False),
    ("2026-10-06", 2702, 2080, 3600, False),
    ("2026-10-07", 2691, 2064, 3600, False),
    ("2026-10-08", 2627, 1995, 3600, False),
    ("2026-10-09", 2728, 2097, 3600, False),
    ("2026-10-12", 3820, 3122, 3600, True),    # 余量 13%：唯一擦边日（429 风暴 + 收尾段 7×）——新闸的真实正对照
    ("2026-10-13", 3017, 2354, 3600, False),
    ("2026-10-14", 1228, 609, 3600, False),
    ("2026-10-15", 2798, 2168, 3600, False),
    ("2026-10-16", 2216, 2203, 3600, False),
    ("2026-10-19", 1942, 1940, 5400, False),
    ("2026-10-20", 1966, 1965, 5400, False),
]

#: 09-23（CBOE 陈旧级联，顺序段 ≈127 秒/只）外推 30 只需要的 Step 2 秒数（CHANGELOG v0.45.518 取证）。
EXTRAPOLATED_0923_S = 4900

#: 正常日离告警线至少这么多倍。预算被调低到让告警线贴近正常日（例如退回 120 秒/只：2700 vs 2661）⇒ 红，
#: 逼改预算的人同时重想告警线——否则「改一个数」又会悄悄造出一个恒真闸。
MIN_CLEARANCE = 1.25

_TICKERS_30 = [f"T{i:02d}" for i in range(30)]


def _status(used, timeout, *, status="success", step2_status="success", total=None, budget=True, tickers=None):
    s = {
        "status": status,
        "total_duration_seconds": used + 27 if total is None else total,
        "tickers": _TICKERS_30 if tickers is None else tickers,
        "steps_result": {"step2_hive_analysis": {"status": step2_status, "duration_seconds": used}},
    }
    if budget:
        s["step2_budget"] = {"timeout_seconds": timeout, "duration_seconds": used,
                             "headroom_seconds": timeout - used, "headroom_pct": (timeout - used) * 100 // timeout}
    return s


def _analyze(tmp_path, status):
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    alerts = a.analyze(p)
    return a, [x for x in alerts if "performance" in x.tags]


def _budget_skipped(a):
    return [s for s in a.checks_skipped if "Step 2 预算余量检查" in s]


class TestRealDaysReplay:

    @pytest.mark.parametrize("date,total,used,timeout,fires", REAL_DAYS, ids=[d[0] for d in REAL_DAYS])
    def test_only_the_genuine_near_miss_fires(self, tmp_path, date, total, used, timeout, fires):
        a, perf = _analyze(tmp_path, _status(used, timeout, total=total))
        assert bool(perf) is fires, (date, used, timeout, [x.message for x in perf])
        assert not _budget_skipped(a)
        if fires:
            assert perf[0].level == am.AlertLevel.HIGH and "【P1 高】性能异常" in perf[0].message

    def test_fixture_has_teeth_against_the_retired_constant_gate(self):
        """尺子自己有牙：被删的那道闸（总耗时 > 5.0 × 1.5）在本夹具的每一个「不该响」的日子上都为真。

        所以上一条全绿 ⇒ 判据里确实没有秒数常数基线；比 Step 2 耗时的常数同理（最小值 609 秒，任何 ≤400 秒的
        常数 × 1.5 都会在全部正常日上响）。"""
        quiet = [d for d in REAL_DAYS if not d[4]]
        assert quiet and all(total > 5.0 * 1.5 for _d, total, _u, _t, _f in quiet)
        assert min(u for _d, _t, u, _b, _f in quiet) > 400 * 1.5

    def test_total_duration_is_no_longer_the_criterion(self, tmp_path):
        """总耗时再大、Step 2 在预算内 ⇒ 不响。非 Step 2 部分 10-20 只有 27 秒，其余各步有自己的超时与失败告警。"""
        _a, perf = _analyze(tmp_path, _status(1965, 5400, total=10 ** 6))
        assert perf == []


class TestGenuinelySlowDaysStillFire:

    def test_0923_cboe_stale_cascade_extrapolated_fires(self, tmp_path):
        """正对照：09-23 那类日子放进今天的 5400 秒预算，跑得完（91%）——离被杀只差 500 秒，必须响。"""
        _a, perf = _analyze(tmp_path, _status(EXTRAPOLATED_0923_S, 5400))
        assert len(perf) == 1 and perf[0].level == am.AlertLevel.HIGH
        assert "91%" in perf[0].message
        d = perf[0].details
        assert d["Step 2 耗时"] == "4900秒" and d["超时预算"] == "5400秒（30 只）" and d["余量"] == "500秒"

    def test_boundary_is_inclusive(self, tmp_path):
        t = 5400
        edge = int(t * am.STEP2_BUDGET_USED_P1)
        assert _analyze(tmp_path, _status(edge, t))[1]
        assert not _analyze(tmp_path, _status(edge - 1, t))[1]

    def test_timeout_that_did_not_reach_p0_fires(self, tmp_path):
        """超时被杀但没被判 failed（例如零产出闸以外的路径记 partial）——余量 ≈0，这里兜住。"""
        _a, perf = _analyze(tmp_path, _status(5401, 5400, status="partial", step2_status="timeout"))
        assert perf, "超时没落 P0 时性能检查是唯一会红的"

    def test_crash_after_long_run_still_reports_the_duration(self, tmp_path):
        """跑到 85% 才崩：步骤失败 P1 之外，耗时本身也说明问题——不以 Step 2 成功为前提。"""
        _a, perf = _analyze(tmp_path, _status(4600, 5400, status="partial", step2_status="failed"))
        assert perf

    def test_rc1_completed_slow_day_fires(self, tmp_path):
        _a, perf = _analyze(tmp_path, _status(4500, 5400, status="partial", step2_status="success_with_warning"))
        assert perf

    def test_failed_pipeline_still_returns_p0_first(self, tmp_path):
        """09-21 / 09-23 的真实形状：status=failed ⇒ P0 先返回（行为不变），不叠一条性能 P1。"""
        p = tmp_path / "status.json"
        p.write_text(json.dumps(_status(3599, 3600, status="failed")), encoding="utf-8")
        alerts = am.AlertAnalyzer(report_dir=tmp_path).analyze(p)
        assert [x.level for x in alerts] == [am.AlertLevel.CRITICAL]


class TestUncheckedIsNotHealthy:

    @pytest.mark.parametrize("budget", [
        None,                                                     # 旧编排器 / 手写 status.json：没有这一段
        {"timeout_seconds": None, "duration_seconds": None},      # heredoc 里 `${STEP2_TIMEOUT:-null}` 未赋值
        {"timeout_seconds": "5400", "duration_seconds": 1965},    # 字符串
        {"timeout_seconds": 5400, "duration_seconds": True},      # bool 是 int 的子类，不算数
        {"timeout_seconds": 0, "duration_seconds": 1965},         # 预算 0：除零 / 恒真
        {"timeout_seconds": 5400, "duration_seconds": -5},
        {"timeout_seconds": 5400, "duration_seconds": float("nan")},
        "5400",                                                   # 整段不是对象
    ])
    def test_step2_ran_but_budget_unusable_is_recorded_as_skipped(self, tmp_path, budget):
        s = _status(1965, 5400, budget=False)
        if budget is not None:
            s["step2_budget"] = budget
        a, perf = _analyze(tmp_path, s)
        assert perf == []
        assert _budget_skipped(a), a.checks_skipped

    def test_step2_never_ran_records_nothing(self, tmp_path):
        """正对照：Step 2 根本没跑（steps_result 里没有它）⇒ 本来就无可判，不往 checks_skipped 里灌噪声。"""
        s = _status(0, 5400, budget=False)
        s["steps_result"] = {"db_backup": {"status": "success"}}
        a, perf = _analyze(tmp_path, s)
        assert perf == [] and not _budget_skipped(a)


class TestNoConstantBaselineEverAgain:

    def test_constructor_has_no_numeric_default(self):
        """墓碑：生产 `main()` 不传参 ⇒ 构造器的数值缺省值就是生产值。任何秒数缺省都是 5.0 那颗雷的复刻。"""
        sig = inspect.signature(am.AlertAnalyzer.__init__)
        numeric = {n: p.default for n, p in sig.parameters.items()
                   if isinstance(p.default, (int, float)) and not isinstance(p.default, bool)}
        assert not numeric, numeric
        assert not [n for n in sig.parameters if "baseline" in n]
        assert not hasattr(am.AlertAnalyzer(), "perf_baseline")

    def test_threshold_is_a_fraction(self):
        assert 0 < am.STEP2_BUDGET_USED_P1 < 1


class TestThresholdWeldedToOrchestratorBudget:

    def test_both_sides_against_the_real_budget(self):
        """两头：告警线（秒）= 比例 × 编排器真实 30 只预算。
          · 离本夹具全部「不该响」日的最大 Step 2 耗时 ≥ MIN_CLEARANCE 倍 ⇒ 不会恒真；
          · 低于预算本身 ⇒ 不会恒假（被杀之前能响），且 09-23 外推那类日子在线上。"""
        coef, floor, _ = step2_budget_formula(repo_orchestrator_text())
        budget = step2_budget(30, coef, floor)
        line = am.STEP2_BUDGET_USED_P1 * budget
        worst_quiet = max(u for _d, _t, u, _b, f in REAL_DAYS if not f)
        assert line >= MIN_CLEARANCE * worst_quiet, (
            f"告警线 {line:.0f}s 离正常日最慢 {worst_quiet}s 不足 {MIN_CLEARANCE} 倍（预算 {budget}s）——"
            f"改了预算就重新标定 STEP2_BUDGET_USED_P1，别让告警线贴着正常日")
        assert line < budget
        assert EXTRAPOLATED_0923_S >= line, f"09-23 那类日子（≈{EXTRAPOLATED_0923_S}s）在告警线 {line:.0f}s 之下——不会响"

    def test_mutant_budget_cut_back_to_120s_per_ticker_turns_red(self):
        """守卫有牙：同一判据套在 v0.45.518 之前的 120 秒/只（3600 秒）上必须不过——那时正常日用掉 55–68%，
        75% 的告警线离 09-24 只有 1.01 倍。"""
        line = am.STEP2_BUDGET_USED_P1 * step2_budget(30, 120, 1800)
        worst_quiet = max(u for _d, _t, u, _b, f in REAL_DAYS if not f)
        assert line < MIN_CLEARANCE * worst_quiet


def _write_status_src(orch_text: str) -> str:
    """抽 `write_status() {` 到 heredoc 结束符 STATUSEOF 之后的第一个顶格 `}`。

    不能用 `tests._orchestrator.extract_function`：heredoc 里 JSON 的闭合 `}` 也顶格，会把函数截在半路。"""
    m = re.search(r"^write_status\(\) \{\n.*?^STATUSEOF\n.*?^\}\n", orch_text, re.S | re.M)
    assert m, "编排器里找不到 write_status（或其 STATUSEOF heredoc）"
    return m.group(0)


class TestWriterReaderSameShape:

    @pytest.mark.parametrize("used,fires", [(EXTRAPOLATED_0923_S, True), (1965, False)])
    def test_real_write_status_heredoc_feeds_the_check(self, tmp_path, used, fires):
        """真 `write_status()`（仓库编排器原文）写出 status.json → 真 `analyze()`。哪边改了字段名 / 形状都会红。"""
        func = _write_status_src(repo_orchestrator_text())
        report = tmp_path / "reports"
        report.mkdir()
        timeout = 5400
        harness = f"""set -uo pipefail
log() {{ :; }}
REPORTDIR={json.dumps(str(report))}
DATA_DIR={json.dumps(str(tmp_path / "data"))}
DATE_STR=2026-10-20
OVERALL_STATUS=success
TICKERS_ARG={json.dumps(" ".join(_TICKERS_30))}
STEPS_RESULT='{{"step2_hive_analysis": {{"status": "success", "duration_seconds": {used}}}}}'
STEP1_START=$(( $(date +%s) - {used + 27} ))
STEP2_TIMEOUT={timeout}
STEP2_DURATION={used}
STEP2_HEADROOM=$(( STEP2_TIMEOUT - STEP2_DURATION ))
STEP2_HEADROOM_PCT=$(( STEP2_HEADROOM * 100 / STEP2_TIMEOUT ))
LOGFILE=/dev/null
{func}
write_status
"""
        env = {**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin"}
        p = subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True, env=env, timeout=60)
        assert p.returncode == 0, f"rc={p.returncode}\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}"
        status_path = report / "status.json"
        written = json.loads(status_path.read_text(encoding="utf-8"))
        assert written["step2_budget"]["timeout_seconds"] == timeout
        assert written["step2_budget"]["duration_seconds"] == used
        a = am.AlertAnalyzer(report_dir=tmp_path)
        perf = [x for x in a.analyze(status_path) if "performance" in x.tags]
        assert bool(perf) is fires, [x.message for x in perf]
        assert not _budget_skipped(a), a.checks_skipped
