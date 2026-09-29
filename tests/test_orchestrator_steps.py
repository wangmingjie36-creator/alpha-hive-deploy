"""`orchestrator_steps.py`（编排器步骤解释器，v0.45.355）的表驱动测试。

分组：
  · TestGoldenLegacy —— 旧格式 JSON（无 `schema_version`）：片段去掉 `contract` 后与**现行 bash 逐字相同**、
    级别相同。期望值写成常量表 `GOLDEN`（好读），**且**这张表本身由 `test_row_is_what_the_repo_orchestrator_does`
    对照 B 之前的编排器：按**模式**（shell 变量名 / 步骤标题，不按行号）
    把各步的分支链抽出来，在 bash 里配同一批夹具**真跑**，逐条比片段、级别与日志原文。
    B（v0.45.385）起编排器经 `_apply_step_interp` 调解释器、内联分支链已删 ⇒ 对照已钉到 B 之前的冻结副本
    `tests/fixtures/orchestrator_pre_b.sh.frozen`（逐字节、摘要钉在 `test_orchestrator_step_interp.py`；
    不用 `git show <sha>`：CI 是浅克隆）。副本缺失就**红**（不是 skip：它随仓库发布，拿不到就是真缺）。
    新接线的守卫在 `tests/test_orchestrator_step_interp.py`。需要 `/bin/bash` 与 `jq`（编排器本身就依赖二者）。
  · TestVariants —— 每个工具步骤 × 八种 JSON 形态：v1 ok / attention / legacy / stale / 契约不符 / 崩溃 / 缺失 / 不可解析
  · TestDateRollover —— 跨午夜：外壳日期恰是次日且确是本轮写的 ⇒ 采用并标 `date_rollover`；其余日期不符照旧
  · TestAttention —— 外壳 status=attention ⇒ 全部条目进 message（封顶 + 「…(+N)」）；alarm ⇒ warn + 🚨
  · TestJsonProblemOnlyWhereRead —— JSON 问题只在 bash 真读 JSON 的那几支覆盖判定，其余记 `json_problem`
  · TestHardening —— FIFO / 字符设备 / 过大 / 过深：恒一行、恒 rc 0、不阻塞、按「不可解析」记
  · TestSweep —— 全组合（步骤 × rc × 形态）的不变式（含「error 只留给那几类」）
  · TestStep2 / TestStep4 —— Step 2 rc=1 的「跑完了但 ML 退化」判定与 Step 4 的联动
    （Step 5 自 v0.45.351 起按部署日志的实际结局判，不归本解释器，见 orchestrator_steps 模块 docstring）
  · TestCli / TestRenderError —— 子进程恒一行 JSON、恒退出 0；内部抛错 ⇒ render_error
  · TestDocPointers —— 文档只存指针：不许再出现编排器行号 / sha 快照；`date_flag` 在生产方真实存在

时间全部是**写死的本地时刻**（mtime 用 os.utime 钉住），不读「现在」——不会随日历过期而变红。
"""
from __future__ import annotations

import ast
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

import orchestrator_steps as ost
import step_contract as sc

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**（子进程要跑 orchestrator_steps.py），故用 __file__

DATE = "2026-09-28"
NEXT = "2026-09-29"                                               # DATE 的次日（跨午夜）
FRESH_TS = datetime(2026, 9, 28, 14, 50, 0).timestamp()          # 本轮工具写 JSON 的时刻（本地）
RUN_START = int(datetime(2026, 9, 28, 14, 40, 0).timestamp())    # 本步开始（早于 FRESH）
LATE_START = int(datetime(2026, 9, 28, 15, 0, 0).timestamp())    # 本步开始（晚于 FRESH ⇒ 那份 JSON 是上一次的）
YESTERDAY_TS = datetime(2026, 9, 27, 14, 50, 0).timestamp()
FRESH_ISO = datetime.fromtimestamp(FRESH_TS).astimezone().isoformat(timespec="seconds")
EVE_START = int(datetime(2026, 9, 28, 23, 58, 0).timestamp())    # 本步开始于午夜前
ROLL_TS = datetime(2026, 9, 29, 0, 27, 57).timestamp()           # 工具在午夜后写出（2026-09-08 实况的形状）
ROLL_ISO = datetime.fromtimestamp(ROLL_TS).astimezone().isoformat(timespec="seconds")
DUR = 7

STEPS = ("10", "11", "12", "13", "15")
KEY = {"10": "step10_scan_continuity", "11": "step11_ic_rerun_readiness", "12": "step12_scan_coverage",
       "13": "step13_calendar_watch", "15": "step15_backup_continuity"}
TOOL = {"10": "scan_continuity", "11": "ic_rerun_readiness", "12": "scan_coverage_gate",
        "13": "economic_calendar_watch", "15": "backup_continuity"}

# ─────────────────────────────── 旧格式 payload（照各工具现行 --out 的键）───────────────────────────────
BEV = {"version": "v0.45.349", "boundary": DATE, "marker": "gex_signal_neutralized",
       "marker_first_seen": None, "verdict": "no_evidence_yet", "unmarked_after_boundary": [],
       "alarm": False, "line": "⏳ 归档里还没有该口径的印记（世代边界 v0.45.349 / 2026-09-28）",
       "per_version": [{"version": "v0.45.334"}, {"version": "v0.45.349"}]}
#: BOUNDARY_JSON 内联 python 的 keep 过滤后的样子（没有 marker / per_version；error 不在原 dict 里就不出现）
BEV_KEPT = {"version": "v0.45.349", "boundary": DATE, "verdict": "no_evidence_yet", "marker_first_seen": None,
            "unmarked_after_boundary": [], "alarm": False,
            "line": "⏳ 归档里还没有该口径的印记（世代边界 v0.45.349 / 2026-09-28）"}
BEV_ALARM = {"version": "v0.45.349", "boundary": DATE, "marker_first_seen": "2026-09-25",
             "verdict": "boundary_too_early", "unmarked_after_boundary": [], "alarm": True,
             "line": "🚨 世代边界 v0.45.349 / 2026-09-28 数据核对未通过，印记首见 2026-09-25：边界写早了",
             "per_version": []}
BEV_ALARM_KEPT = {k: BEV_ALARM[k] for k in ("version", "boundary", "verdict", "marker_first_seen",
                                            "unmarked_after_boundary", "alarm", "line")}

LEGACY = {
    "10": {"window": {"start": "2026-08-17", "end": DATE, "trading_days": 30}, "scanned_days": 23,
           "coverage": 23 / 30, "gaps": [], "longest_gap": 5, "missing_days": [], "weeks_total": 7,
           "weeks_covered": 6, "weeks_missed": ["2026-W34"], "week_coverage": 6 / 7,
           "thresholds": {"min_coverage": 0.9, "max_gap": 2}, "healthy": False,
           "consistency": {"db_only": [], "snapshot_only": []}, "per_day_tickers": {}},
    "11": {"cohort": {"date": "2026-09-18", "version": "v0.45.315", "reason": "…", "n_generations": 9},
           "target_ic": 0.09, "weeks_required": 25, "weeks_accrued": 0, "weeks_remaining": 25,
           "n_ripe_samples": 0, "n_all_samples": 240, "eta_date": "2026-12-21", "pool_drift": 0.0,
           "pool_note": None, "ready": False, "next_step": "…", "cohort_boundary_evidence": BEV},
    "12": {"date": DATE, "determinable": True, "tickers": 30, "healthy": False,
           "fields": [{"field": "rv_30d", "source": "yfinance 历史价", "have": 0, "total": 30, "degraded": True},
                      {"field": "iv_rank", "source": "CBOE", "have": 30, "total": 30, "degraded": False},
                      {"field": "catalysts", "source": "Finnhub 日历", "have": 0, "total": 30, "degraded": True}],
           "degraded_fields": ["rv_30d", "catalysts"], "likely_network_layer": True,
           "label_honesty": {"determinable": True, "healthy": True, "contradictions": []}},
    "13": {"checked_at": DATE, "throttled": True, "days_since_last_check": 3, "max_age_days": 7,
           "calendar_health": {"status": "stale", "binding_table": "nfp", "binding_last_date": "2026-12-04",
                               "binding_horizon_days": 67},
           "upstream": {}, "new_schedule_tables": [], "undeterminable_tables": [],
           "upstream_conclusive": True, "action_required": False},
    "15": {"window": {"start": "2026-08-17", "end": DATE, "trading_days": 30}, "backed_up_days": 20,
           "coverage": 2 / 3, "gaps": [], "longest_gap": 4, "weeks_total": 7, "weeks_covered": 6,
           "weeks_missed": ["2026-W37"], "healthy": False},
}
#: rc=1 那一支的摘要（逐字照内联 python；原来是两行的，这里各段分别断言）
SUMMARY = {
    "10": ["过去 30 个交易日跑了 23 次（覆盖率 77%），ISO 周覆盖 6/7，最长空档 5 个交易日",
           "完全无扫描的周: 2026-W34 ← 每个都是一个永久丢失的 T+7 观测"],
    "11": ["⏳ Step 11：世代自 2026-09-18（v0.45.315）起，已攒 0/25 个不重叠周（0 条已回填样本），预计 ≈2026-12-21 到位",
           "Step 11 世代边界核对：⏳ 归档里还没有该口径的印记（世代边界 v0.45.349 / 2026-09-28）"],
    "12": ["⚠️ Step 12：检出字段降级或来源标签矛盾 — rv_30d 0/30; catalysts 0/30  ⚠️ 多个不同数据源同时降级，疑为网络/闸门层"],
    "13": ["⚠️ Step 13：本地日历 stale：nfp 表只到 2026-12-04（剩 67 天），上游暂无新日程可抄"],
    "15": ["过去 30 个交易日只成功了 20 次（覆盖率 67%），最长空档 4 个交易日", "完全无成功备份的周: 2026-W37"],
}


# ─────────────────────────────────────────── 夹具 ───────────────────────────────────────────

def _write(path: Path, content, mtime: float = FRESH_TS) -> Path:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def legacy_json(tmp_path: Path, step: str, payload=None, mtime: float = FRESH_TS) -> Path:
    return _write(tmp_path / f"legacy-{step}.json", LEGACY[step] if payload is None else payload, mtime)


def _v1_payload(step: str, payload=None) -> dict:
    body = dict(LEGACY[step] if payload is None else payload)
    body.pop("date", None)     # scan_coverage_gate 的 date 由外壳承载（保留键撞名即抛）
    return body


def v1_json(tmp_path: Path, step: str, *, status: Optional[str] = None, attention=(), payload=None,
            date: str = DATE, generated_at: str = FRESH_ISO, tool=None, mtime: float = FRESH_TS,
            name: str = "v1") -> Path:
    """外壳 status 缺省跟生产方一样：有条目 ⇒ attention，没有 ⇒ ok。"""
    attention = list(attention)
    status = status or ("attention" if attention else "ok")
    env = sc.envelope(tool or TOOL[step], date, status, attention=attention,
                      payload=_v1_payload(step, payload), generated_at=generated_at)
    return _write(tmp_path / f"{name}-{step}.json", env, mtime)


def run(step, rc, json_path=None, *, date=DATE, run_start=None, duration=DUR, timeout=None,
        data_dir=None, report_dir=None) -> dict:
    argv = ["--step", str(step), "--rc", str(rc), "--date", date]
    if json_path is not None:
        argv += ["--json", str(json_path)]
    for flag, val in (("--run-start", run_start), ("--duration", duration), ("--timeout-seconds", timeout),
                      ("--data-dir", data_dir), ("--report-dir", report_dir)):
        if val is not None:
            argv += [flag, str(val)]
    out = ost.render(argv)
    assert set(out) == {"level", "message", "steps_fragment"}, out
    assert out["level"] in ost.LEVELS and "\n" not in out["message"], out
    return out


def frag(out: dict, step: str) -> dict:
    assert list(out["steps_fragment"]) == [KEY[step]], out
    return out["steps_fragment"][KEY[step]]


# ─────────────────── B 之前的编排器（冻结副本）：按模式抽出分支链，在 bash 里真跑 ───────────────────
#: B 之前最后一版编排器的逐字节副本。**永远不许**从 B 之后的编排器重新生成——摘要钉在
#: `tests/test_orchestrator_step_interp.py::test_frozen_fixture_pinned`，改一个字节就红。
PRE_B_REL = "tests/fixtures/orchestrator_pre_b.sh.frozen"


@functools.lru_cache(maxsize=1)
def _orchestrator_text_cached() -> str:
    p = REPO_ROOT / PRE_B_REL
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def orchestrator_text() -> str:
    text = _orchestrator_text_cached()
    if not text:
        pytest.fail(f"拿不到 B 之前的编排器冻结副本 {REPO_ROOT / PRE_B_REL}。它随仓库发布，"
                    "拿不到就是真缺——不是 skip 的理由")
    return text


class _BashStep:
    """一步分支链在 B 之前的冻结副本里的位置（全按模式）与它读的 shell 变量。

    anchor   该步之后第一处 `start` 才是它的分支链（Step 4 与 Step 2 的链都以 `if [ $STEP2_RC -eq 0 ]` 开头）
    start    抽取的第一行
    through  抽取到它之后的第一个顶格 `fi`（缺省 = 从 start 算起）——Step 11 要带上 BOUNDARY_JSON 与 `_S11_NEW` 段
    """

    def __init__(self, key: str, rc_var: str, anchor: str, start: str, *, through: Optional[str] = None,
                 json_var: Optional[str] = None, dur_var: Optional[str] = None,
                 timeout_var: Optional[str] = None):
        self.key, self.rc_var, self.anchor, self.start, self.through = key, rc_var, anchor, start, through
        self.json_var, self.dur_var, self.timeout_var = json_var, dur_var, timeout_var


def _chain_if(var: str) -> str:
    return rf"^if \[ \${var} -eq 0 \]; then$"


BASH: Dict[str, _BashStep] = {
    "2": _BashStep("step2_hive_analysis", "STEP2_RC", r"^STEP2_RC=\$\?$", _chain_if("STEP2_RC"),
                   dur_var="STEP2_DURATION", timeout_var="STEP2_TIMEOUT"),
    "4": _BashStep("step4_dashboard", "STEP2_RC", r"【Step 4/5】", _chain_if("STEP2_RC")),
    "10": _BashStep(KEY["10"], "STEP10_RC", r"^STEP10_RC=\$\?$", _chain_if("STEP10_RC"),
                    json_var="CONTINUITY_JSON", dur_var="STEP10_DURATION"),
    "11": _BashStep(KEY["11"], "STEP11_RC", r"^STEP11_RC=\$\?$", r"^READINESS_LINE=\$\(",
                    through=r"^_S11_NEW=\$\(", json_var="READINESS_JSON"),
    "12": _BashStep(KEY["12"], "STEP12_RC", r"^STEP12_RC=\$\?$", _chain_if("STEP12_RC"), json_var="COVERAGE_JSON"),
    "13": _BashStep(KEY["13"], "STEP13_RC", r"^STEP13_RC=\$\?$", _chain_if("STEP13_RC"), json_var="CALWATCH_JSON"),
    "15": _BashStep(KEY["15"], "STEP15_RC", r"^STEP15_RC=\$\?$", _chain_if("STEP15_RC"),
                    json_var="BACKUP_CONTINUITY_JSON", dur_var="STEP15_DURATION"),
}


def bash_block(step: str) -> str:
    text, spec = orchestrator_text(), BASH[step]
    a = re.search(spec.anchor, text, re.M)
    assert a, f"B 之前的编排器冻结副本里找不到 Step {step} 的锚点 {spec.anchor!r}（副本被改过？摘要守卫应已先红）"
    s = re.compile(spec.start, re.M).search(text, a.end())
    assert s, f"冻结副本里 Step {step} 的锚点之后找不到分支链起点 {spec.start!r}"
    from_ = s.start()
    if spec.through:
        t = re.compile(spec.through, re.M).search(text, s.end())
        assert t, f"编排器里 Step {step} 找不到 {spec.through!r}"
        from_end = t.end()
    else:
        from_end = s.end()
    f = re.compile(r"^fi$", re.M).search(text, from_end)
    assert f, f"编排器里 Step {step} 的分支链没有顶格 fi 收尾"
    return text[from_:f.end()]


def _bash_env() -> dict:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    assert shutil.which("jq", path=env["PATH"]), "找不到 jq：编排器本身依赖它，这组对照没有它就是假绿"
    return env


_LEVEL_OF = {"INFO": "info", "WARN": "warn", "ERROR": "error"}


def run_bash(step: str, rc: int, json_path=None, *, timeout_seconds: int = 3600
             ) -> Tuple[str, List[str], dict]:
    """在 bash 里跑 B 之前的冻结副本里该步的分支链。返回 (最高日志级别, 各条 log 原文, 该步的 STEPS_RESULT 条目)。"""
    spec = BASH[step]
    assigns = {spec.rc_var: rc}
    if spec.json_var:
        assigns[spec.json_var] = str(json_path)
    if spec.dur_var:
        assigns[spec.dur_var] = DUR
    if spec.timeout_var:
        assigns[spec.timeout_var] = timeout_seconds
    script = "\n".join([
        "set -uo pipefail",
        r"""log() { printf '\036LOG\037%s\037%s' "$1" "$2"; }""",
        "set_status() { :; }",
        f"PYTHON3={shlex.quote(sys.executable)}",
        "STEPS_RESULT='{}'",
        *(f"{k}={shlex.quote(str(v))}" for k, v in assigns.items()),
        bash_block(step),
        r"""printf '\036RESULT\037%s' "$(printf '%s' "$STEPS_RESULT" | jq -c .)" """,
    ]) + "\n"
    p = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=_bash_env(),
                       timeout=60, cwd=str(Path(json_path).parent) if json_path else None)
    assert p.returncode == 0, f"bash rc={p.returncode}\nSTDERR:{p.stderr}\nSTDOUT:{p.stdout}"
    levels, texts, result = [], [], None
    for rec in p.stdout.split("\x1e"):
        f = rec.split("\x1f")
        if f[0] == "LOG" and len(f) == 3:
            levels.append(_LEVEL_OF[f[1]])
            texts.append(f[2])
        elif f[0] == "RESULT":
            result = json.loads(f[1])
    assert result is not None and levels, p.stdout
    return max(levels, key=ost.LEVELS.index), texts, result[spec.key]


def assert_message_carries_bash_lines(message: str, bash_texts: List[str]) -> None:
    """bash 的每条 log（多行的逐行、去掉缩进）都原样出现在解释器的一行 message 里。"""
    for t in bash_texts:
        for piece in t.split("\n"):
            piece = piece.strip()
            if piece:
                assert piece in message, (piece, message)


def bash_branches(block: str, rc_var: str) -> Tuple[str, Dict[Optional[int], str]]:
    """(分支链之前的部分, {rc: 该支的正文, None: else 支})。"""
    head_re = re.compile(rf"^(?:if|elif) \[ \${rc_var} -eq (\d+) \]; then$|^else$", re.M)
    heads = list(head_re.finditer(block))
    assert heads, f"找不到 {rc_var} 的分支链"
    chain_end = re.compile(r"^fi$", re.M).search(block, heads[-1].end())
    assert chain_end, f"{rc_var} 的分支链没有顶格 fi 收尾"
    branches: Dict[Optional[int], str] = {}
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else chain_end.start()
        branches[int(h.group(1)) if h.group(1) else None] = block[h.end():end]
    return block[:heads[0].start()], branches


# ═══════════════════════════════ 1. 旧格式：与现行 bash 逐字相同 ═══════════════════════════════
# (step, rc, 有无 JSON, 期望级别, 期望片段（不含 contract）)——ids 里的 `STEPn_RC=k` 就是 bash 里的那一支
# （rc=99 = else 支；Step 11/12/13 没有 124 支，124 也落进 else）。级别 = 该支里 bash 打出的各行日志级别的最高者。
# P = --json 路径（detail_json 原样抄它）。
P = "<P>"
GOLDEN = [
    ("10", 0, True, "info", {"status": "healthy", "duration_seconds": DUR}),
    ("10", 1, True, "warn", {"status": "degraded", "duration_seconds": DUR, "detail_json": P}),
    ("10", 3, True, "warn", {"status": "undetermined"}),
    ("10", 2, False, "warn", {"status": "skipped"}),
    ("10", 124, True, "error", {"status": "timeout"}),
    ("10", 99, True, "warn", {"status": "error", "rc": 99}),
    # Step 11：cohort_boundary 恒在（`_S11_NEW` 的 jq 合并），取自 JSON 的 cohort_boundary_evidence 经 keep 过滤
    ("11", 0, True, "info", {"status": "ready", "cohort_boundary": BEV_KEPT}),
    ("11", 1, True, "info", {"status": "accruing", "cohort_boundary": BEV_KEPT}),
    ("11", 3, True, "warn", {"status": "undetermined", "cohort_boundary": BEV_KEPT}),
    ("11", 2, False, "warn", {"status": "skipped", "cohort_boundary": None}),
    # Step 11 **没有** 124 分支：超时落进 else——照抄，不顺手「修」
    ("11", 124, True, "warn", {"status": "error", "rc": 124, "cohort_boundary": BEV_KEPT}),
    ("11", 99, True, "warn", {"status": "error", "rc": 99, "cohort_boundary": BEV_KEPT}),
    ("12", 0, True, "info", {"status": "healthy"}),
    ("12", 1, True, "warn", {"status": "degraded"}),
    ("12", 3, True, "warn", {"status": "undetermined"}),
    ("12", 2, False, "warn", {"status": "skipped"}),
    ("12", 124, True, "warn", {"status": "error", "rc": 124}),
    ("12", 99, True, "warn", {"status": "error", "rc": 99}),
    ("13", 0, True, "info", {"status": "healthy"}),
    ("13", 1, True, "warn", {"status": "action_required", "detail_json": P}),
    ("13", 3, True, "warn", {"status": "undetermined"}),
    ("13", 2, False, "warn", {"status": "skipped"}),
    ("13", 124, True, "warn", {"status": "error", "rc": 124}),
    ("13", 99, True, "warn", {"status": "error", "rc": 99}),
    ("15", 0, True, "info", {"status": "healthy", "duration_seconds": DUR}),
    ("15", 1, True, "warn", {"status": "degraded", "duration_seconds": DUR, "detail_json": P}),
    ("15", 3, True, "warn", {"status": "undetermined"}),
    ("15", 2, False, "warn", {"status": "skipped"}),
    ("15", 124, True, "error", {"status": "timeout"}),
    ("15", 99, True, "warn", {"status": "error", "rc": 99}),
]
GOLDEN_IDS = [f"STEP{g[0]}_RC={g[1]}" for g in GOLDEN]
BASE_LEVEL = {(g[0], g[1]): g[3] for g in GOLDEN}
BASE_STATUS = {(g[0], g[1]): g[4]["status"] for g in GOLDEN}


def _subst(d, path):
    return {k: (str(path) if v == P else v) for k, v in d.items()}


class TestGoldenLegacy:
    @pytest.mark.parametrize("step,rc,with_json,level,expected", GOLDEN, ids=GOLDEN_IDS)
    def test_fragment_and_level_match_todays_bash(self, tmp_path, step, rc, with_json, level, expected):
        p = legacy_json(tmp_path, step) if with_json else tmp_path / "absent.json"
        out = run(step, rc, p)
        f = dict(frag(out, step))
        contract = f.pop("contract", None)
        assert f == _subst(expected, p)
        assert out["level"] == level
        # (e) 读到了旧格式 JSON ⇒ 标 legacy；rc=2 不读 JSON ⇒ 不标
        assert contract == ("legacy" if with_json and rc != 2 else None)

    @pytest.mark.parametrize("step,rc,with_json,level,expected", GOLDEN, ids=GOLDEN_IDS)
    def test_row_is_what_the_repo_orchestrator_does(self, tmp_path, step, rc, with_json, level, expected):
        """上面那张表不是凭记忆写的：把 B 之前的冻结副本里该步的分支链抽出来、同一夹具在 bash 里真跑，逐条相等。"""
        p = legacy_json(tmp_path, step) if with_json else tmp_path / "absent.json"
        b_level, b_texts, b_frag = run_bash(step, rc, p)
        assert b_frag == _subst(expected, p)
        assert b_level == level
        assert_message_carries_bash_lines(run(step, rc, p)["message"], b_texts)

    @pytest.mark.parametrize("step", STEPS)
    def test_json_read_branches_match_pre_b_orchestrator(self, step):
        """(a') 「哪几支读 JSON」不是凭记忆写的：从 B 之前的冻结副本的分支链里数 `open('$<JSON 变量>')` 出现在哪。
        rc=2 除外——解释器在那一支刻意不读（(a)：脚本不存在，路径上的 JSON 不可能是本轮的）。"""
        spec = BASH[step]
        pre, branches = bash_branches(bash_block(step), spec.rc_var)
        needle = f"open('${spec.json_var}')"
        for rc in (0, 1, 3, 124, 99):
            body = branches.get(rc, branches.get(None, ""))
            bash_reads = needle in pre or needle in body
            assert ost._TOOL_STEPS[step].reads_json(rc) == bash_reads, (step, rc)
        assert ost._TOOL_STEPS[step].reads_json(2) is False

    @pytest.mark.parametrize("step", STEPS)
    def test_rc1_summary_text_matches_inline_python(self, tmp_path, step):
        """rc=1 的摘要是内联 python 逐字搬过来的（原来多行的，现在各段落在同一行里）。"""
        out = run(step, 1, legacy_json(tmp_path, step))
        for seg in SUMMARY[step]:
            assert seg in out["message"], (seg, out["message"])

    def test_step11_boundary_alarm_is_warn_and_kept_keys_only(self, tmp_path):
        """边界段：alarm 为真打 WARN；keep 过滤：只抄那几个键（per_version 不进 status.json）。"""
        payload = dict(LEGACY["11"], cohort_boundary_evidence=BEV_ALARM)
        p = legacy_json(tmp_path, "11", payload)
        out = run("11", 1, p)
        assert frag(out, "11") == {"status": "accruing", "cohort_boundary": BEV_ALARM_KEPT, "contract": "legacy"}
        assert out["level"] == "warn"
        assert "🚨 Step 11：世代边界核对未通过 — 🚨 世代边界 v0.45.349" in out["message"]
        assert "追加**一条更正" in out["message"]
        b_level, b_texts, b_frag = run_bash("11", 1, p)
        assert (b_level, b_frag) == ("warn", {"status": "accruing", "cohort_boundary": BEV_ALARM_KEPT})
        assert_message_carries_bash_lines(out["message"], b_texts)

    def test_step11_legacy_without_boundary_key_is_null(self, tmp_path):
        """v0.45.334 之前的就绪度 JSON 没有 cohort_boundary_evidence ⇒ 记 null，不假装正常。"""
        payload = {k: v for k, v in LEGACY["11"].items() if k != "cohort_boundary_evidence"}
        p = legacy_json(tmp_path, "11", payload)
        out = run("11", 1, p)
        assert frag(out, "11") == {"status": "accruing", "cohort_boundary": None, "contract": "legacy"}
        assert "本轮未核对" in out["message"] and out["level"] == "info"
        assert run_bash("11", 1, p)[2] == {"status": "accruing", "cohort_boundary": None}

    def test_step11_jq_alternative_falls_back_to_verdict(self, tmp_path):
        """`jq -r '.line // .verdict'`：line 为 null 时退到 verdict。"""
        bev = dict(BEV, line=None)
        p = legacy_json(tmp_path, "11", dict(LEGACY["11"], cohort_boundary_evidence=bev))
        out = run("11", 1, p)
        assert "Step 11 世代边界核对：no_evidence_yet" in out["message"]
        assert_message_carries_bash_lines(out["message"], run_bash("11", 1, p)[1])

    def test_step11_non_finite_numbers_are_rewritten_like_jq(self, tmp_path):
        """内联 python 把 NaN/Infinity 裸写进 BOUNDARY_JSON；其后的 `jq -e .` **吃得下**（jq 1.7，实测
        jq-1.7.1-apple）⇒ 不退成 null，而是经 `_S11_NEW` 的 `--argjson` 变成 NaN→null、±Infinity→±DBL_MAX。
        `.line // .verdict` 读的是未改写的文本：NaN 不算缺，打成 `null`（不退到 verdict）。两段都与 bash 真跑对照。"""
        p = tmp_path / "nan.json"
        bev = dict(BEV, error=float("nan"), marker_first_seen=float("inf"), unmarked_after_boundary=[float("-inf")])
        _write(p, json.dumps(dict(LEGACY["11"], cohort_boundary_evidence=bev)))
        expected = dict(BEV_KEPT, error=None, marker_first_seen=1.7976931348623157e+308,
                        unmarked_after_boundary=[-1.7976931348623157e+308])
        assert frag(run("11", 1, p), "11")["cohort_boundary"] == expected
        jq_ver = subprocess.run(["jq", "--version"], capture_output=True, text=True, env=_bash_env()).stdout.strip()
        assert run_bash("11", 1, p)[2]["cohort_boundary"] == expected, f"jq 版本 {jq_ver}：口径随 jq 版本变了？"
        _write(p, json.dumps(dict(LEGACY["11"], cohort_boundary_evidence=dict(BEV, line=float("nan")))))
        out = run("11", 1, p)
        assert frag(out, "11")["cohort_boundary"]["line"] is None
        assert "Step 11 世代边界核对：null" in out["message"]
        assert_message_carries_bash_lines(out["message"], run_bash("11", 1, p)[1])

    def test_step13_new_tables_summary(self, tmp_path):
        payload = dict(LEGACY["13"], new_schedule_tables=["cpi"], action_required=True,
                       upstream={"cpi": {"new_items": ["2027-01-13", "2027-02-11"],
                                         "source": "https://www.bls.gov/schedule/news_release/cpi.htm"}})
        p = legacy_json(tmp_path, "13", payload)
        out = run("13", 1, p)
        assert ("⚠️ Step 13：上游已发布新日程：cpi —— 待人工抄录；cpi 新增 2 条"
                "（https://www.bls.gov/schedule/news_release/cpi.htm）") in out["message"]
        assert_message_carries_bash_lines(out["message"], run_bash("13", 1, p)[1])

    def test_step13_inconclusive_upstream_is_not_reported_as_no_news(self, tmp_path):
        payload = dict(LEGACY["13"], upstream_conclusive=False, undeterminable_tables=["cpi", "gdp"])
        p = legacy_json(tmp_path, "13", payload)
        out = run("13", 1, p)
        assert "⚠️ 上游 2 个源无法判定（cpi、gdp），本次无法确认上游有无新日程" in out["message"]
        assert "上游暂无新日程可抄" not in out["message"]
        assert_message_carries_bash_lines(out["message"], run_bash("13", 1, p)[1])

    def test_duration_is_omitted_not_invented(self, tmp_path):
        """没传 --duration ⇒ 不带 duration_seconds（不编一个 0 出来冒充测过）。"""
        out = run("10", 0, legacy_json(tmp_path, "10"), duration=None)
        assert frag(out, "10") == {"status": "healthy", "contract": "legacy"}
        assert "耗时 ?s" in out["message"]

    def test_timeout_text_uses_given_seconds(self, tmp_path):
        assert "（>120s）" in run("10", 124, None)["message"]          # bash 写死的常数（run_step --timeout 120）
        assert "（>60s）" in run("15", 124, None)["message"]           # 同上（--timeout 60）
        assert "（>90s）" in run("15", 124, None, timeout=90)["message"]


# ═══════════════════════════════ 2. 每步 × 八种 JSON 形态 ═══════════════════════════════
#: rc=1（五步都读 JSON 的那一支）的现行片段，形态测试以它为基准
RC1 = {"10": {"status": "degraded", "duration_seconds": DUR, "detail_json": P},
       "11": {"status": "accruing", "cohort_boundary": BEV_KEPT},
       "12": {"status": "degraded"},
       "13": {"status": "action_required", "detail_json": P},
       "15": {"status": "degraded", "duration_seconds": DUR, "detail_json": P}}
RC1_LEVEL = {"10": "warn", "11": "info", "12": "warn", "13": "warn", "15": "warn"}


def _problem_frag(step: str, status: str, rc: int, rc_status: str, **extra) -> dict:
    """问题形态（在读 JSON 的那一支）的片段：原判定进 rc_status，duration 照带，Step 11 的 cohort_boundary 记 null。"""
    f = {"status": status, "rc": rc, "rc_status": rc_status}
    if step in ("10", "15") and rc in (0, 1):
        f["duration_seconds"] = DUR
    if step == "11":
        f["cohort_boundary"] = None
    f.update(extra)
    return f


def _reads(step: str, rc: int) -> bool:
    """bash 在哪几支读 JSON（`test_json_read_branches_match_pre_b_orchestrator` 从冻结副本核对过）。"""
    return rc != 2 and (step == "11" or rc == 1)


ATTN = [sc.attention_item("x.dim_ic.h1_anchor_pending", "warn",
                          "维度 IC 协议：H1 锚点待登记 —— 须早于 2026-10-12 进边界表", deadline="2026-10-12"),
        sc.attention_item("x.calendar", "info", "nfp 表只到 2026-12-04", deadline="2026-12-04"),
        sc.attention_item("x.ready", "info", "已就绪（知会：外壳不说要人看时不刷屏）")]


class TestVariants:
    @pytest.mark.parametrize("step", STEPS)
    def test_v1_ok_is_legacy_fragment_plus_contract(self, tmp_path, step):
        p = v1_json(tmp_path, step)
        out = run(step, 1, p)
        assert frag(out, step) == dict(_subst(RC1[step], p), contract="v1", envelope_status="ok")
        assert out["level"] == RC1_LEVEL[step]
        assert "需人看" not in out["message"]

    @pytest.mark.parametrize("step", STEPS)
    def test_v1_non_attention_status_reports_only_warn_and_deadline_items(self, tmp_path, step):
        """(d) 外壳不说「要人看」时（这里 undetermined）：warn 条目与带 deadline 的条目进 message 与片段；
        不带 deadline 的 info 不报。"""
        p = v1_json(tmp_path, step, status="undetermined", attention=ATTN)
        written = json.loads(p.read_text(encoding="utf-8"))["attention"]
        out = run(step, 1, p)
        assert frag(out, step) == dict(_subst(RC1[step], p), contract="v1", envelope_status="undetermined",
                                       attention=written[:2])
        assert out["level"] == "warn"                      # warn 条目把 Step 11 的 info 抬到 warn
        m = out["message"]
        assert "需人看：[warn] 维度 IC 协议：H1 锚点待登记 —— 须早于 2026-10-12 进边界表（截止 2026-10-12）" in m
        assert "[info] nfp 表只到 2026-12-04（截止 2026-12-04）" in m
        assert "不刷屏" not in m

    def test_v1_overdue_deadline_raises_info_to_warn(self, tmp_path):
        p = v1_json(tmp_path, "11", status="ok",
                    attention=[sc.attention_item("x.late", "info", "锚点登记", deadline="2026-09-27")])
        out = run("11", 1, p)
        assert out["level"] == "warn" and "（截止 2026-09-27，已过期）" in out["message"]

    def test_attention_is_compacted_into_one_line(self, tmp_path):
        """(d) 压缩：条目里的换行压成空格、超长截断——bash 侧 `log` 一次只打一行。"""
        long_msg = "第一行\n  第二行\r\n第三行" + "长" * 400
        p = v1_json(tmp_path, "12", attention=[sc.attention_item("x.nl", "warn", long_msg)])
        m = run("12", 0, p)["message"]
        assert "[warn] 第一行 第二行 第三行长" in m and "\n" not in m and "\r" not in m
        assert "长" * 250 not in m and "…" in m

    def test_v1_attention_is_reported_even_on_rc0(self, tmp_path):
        """attention 与退出码分开（外壳语义）：rc=0 也照报。"""
        p = v1_json(tmp_path, "11", attention=ATTN[:1])
        out = run("11", 0, p)
        assert frag(out, "11")["status"] == "ready" and frag(out, "11")["attention"][0]["deadline"] == "2026-10-12"
        assert out["level"] == "warn"

    @pytest.mark.parametrize("step", STEPS)
    def test_legacy_is_accepted_and_marked(self, tmp_path, step):
        p = legacy_json(tmp_path, step)
        out = run(step, 1, p)
        assert frag(out, step) == dict(_subst(RC1[step], p), contract="legacy")
        assert out["level"] == RC1_LEVEL[step]

    # ── (a) stale ──
    @pytest.mark.parametrize("step", STEPS)
    def test_stale_v1_date_mismatch(self, tmp_path, step):
        p = v1_json(tmp_path, step, date="2026-09-27")
        out = run(step, 1, p)
        assert frag(out, step) == _problem_frag(
            step, "stale_json", 1, RC1[step]["status"], contract="v1",
            stale={"reason": "date_mismatch", "json_date": "2026-09-27", "expected_date": DATE,
                   "written_at": FRESH_ISO})
        assert out["level"] == "error"                     # rc=1 与崩溃同码：本轮那份等于缺失 ⇒ error
        assert "不是本轮写的（外壳 date=2026-09-27，本轮是 2026-09-28）" in out["message"]

    @pytest.mark.parametrize("step", STEPS)
    def test_stale_v1_written_before_run(self, tmp_path, step):
        p = v1_json(tmp_path, step)
        f = frag(run(step, 1, p, run_start=LATE_START), step)
        assert f["status"] == "stale_json" and f["stale"]["reason"] == "written_before_run"
        # 同一份文件、本步在它之后才开始 ⇒ 新鲜
        assert frag(run(step, 1, p, run_start=RUN_START), step)["status"] == RC1[step]["status"]

    @pytest.mark.parametrize("step", STEPS)
    def test_stale_legacy_by_mtime(self, tmp_path, step):
        p = legacy_json(tmp_path, step, mtime=YESTERDAY_TS)
        f = frag(run(step, 1, p), step)
        assert f["status"] == "stale_json" and f["rc_status"] == RC1[step]["status"]
        assert f["contract"] == "legacy" and f["stale"]["reason"] == "mtime_before_date"
        p2 = legacy_json(tmp_path, step)
        f2 = frag(run(step, 1, p2, run_start=LATE_START), step)
        assert f2["status"] == "stale_json" and f2["stale"]["reason"] == "written_before_run"

    def test_rerun_timeout_does_not_read_previous_file(self, tmp_path):
        """根因场景：同一 DATE_STR 重跑、Step 11 超时 ⇒ 现行 bash 把上一次文件里的边界核对抄进片段。
        （Step 11 的 BOUNDARY_JSON 在分支链之前无条件读 ⇒ 这一支也「读」JSON ⇒ 覆盖。）"""
        p = legacy_json(tmp_path, "11")                    # 上一次（14:50）留下的
        out = run("11", 124, p, run_start=LATE_START)      # 本步 15:00 才开始
        assert frag(out, "11") == _problem_frag(
            "11", "stale_json", 124, "error", contract="legacy",
            stale={"reason": "written_before_run", "written_at": ost._iso_local(FRESH_TS),
                   "run_start": ost._iso_local(LATE_START)})
        assert out["level"] == "warn"

    def test_rc2_never_reads_leftover_json(self, tmp_path):
        """(a) rc=2（脚本不存在）⇒ 路径上的 JSON 不可能是本轮的：不读，Step 11 边界记 null。"""
        p = legacy_json(tmp_path, "11")
        out = run("11", 2, p)
        assert frag(out, "11") == {"status": "skipped", "cohort_boundary": None}
        assert "先前留下的" in out["message"]

    # ── (b) 契约不符 ──
    @pytest.mark.parametrize("step", STEPS)
    def test_wrong_schema_version(self, tmp_path, step):
        env = json.loads(v1_json(tmp_path, step).read_text(encoding="utf-8"))
        env["schema_version"] = 2
        out = run(step, 1, _write(tmp_path / "v2.json", env))
        f = frag(out, step)
        assert f == _problem_frag(step, "contract_error", 1, RC1[step]["status"], problems=f["problems"])
        assert any("schema_version=2" in x for x in f["problems"]) and out["level"] == "error"

    @pytest.mark.parametrize("mutate,needle", [
        (lambda e: e.pop("tool"), "缺外壳键 tool"),
        (lambda e: e.update(status="ready"), "status='ready'"),
        (lambda e: e.update(date="2026-9-28"), "YYYY-MM-DD"),
        (lambda e: e.update(tool="scan_continuity"), "本步期望 'ic_rerun_readiness'"),
    ], ids=["missing-key", "bad-status", "bad-date", "wrong-tool"])
    def test_contract_problems_are_listed(self, tmp_path, mutate, needle):
        env = json.loads(v1_json(tmp_path, "11").read_text(encoding="utf-8"))
        mutate(env)
        out = run("11", 1, _write(tmp_path / "bad.json", env))
        f = frag(out, "11")
        assert f["status"] == "contract_error" and any(needle in x for x in f["problems"]), f
        assert f["cohort_boundary"] is None and out["level"] == "error"

    def test_non_object_json_is_contract_error(self, tmp_path):
        f = frag(run("10", 1, _write(tmp_path / "list.json", [1, 2])), "10")
        assert f["status"] == "contract_error" and "顶层必须是对象" in f["problems"][0]

    # ── (c) 崩溃 ──
    @pytest.mark.parametrize("step", STEPS)
    def test_crashed_envelope_is_error_not_undetermined(self, tmp_path, step, capsys):
        """外壳由真正的生产方路径 `step_contract.run_tool` 写出：未捕获异常 ⇒ 退出码 3 + status=error。"""
        p = tmp_path / "crash.json"

        def boom():
            raise RuntimeError("boom")
        rc = sc.run_tool(TOOL[step], boom, argv=["--quiet", "--out", str(p), "--date", DATE])
        assert rc == 3
        env = json.loads(p.read_text(encoding="utf-8"))
        out = run(step, rc, p)
        assert frag(out, step) == _problem_frag(step, "error", 3, "undetermined", contract="v1",
                                                envelope_status="error", error="RuntimeError: boom",
                                                attention=env["attention"])
        assert out["level"] == "error"
        assert "运行时崩溃（exit=3）" in out["message"] and "RuntimeError: boom" in out["message"]
        assert "无法判定" not in out["message"]          # 不再落进「3 = 无法判定」那句话

    def test_stale_crash_envelope_is_not_this_runs_crash(self, tmp_path, capsys):
        p = tmp_path / "crash.json"
        sc.run_tool("ic_rerun_readiness", lambda: 1 / 0, argv=["--out", str(p), "--date", "2026-09-27"])
        f = frag(run("11", 3, p), "11")
        assert f["status"] == "stale_json" and f["rc_status"] == "undetermined"

    # ── (f) 缺失 / 不可解析 ──
    @pytest.mark.parametrize("step", STEPS)
    def test_missing_json_on_rc1_is_not_recorded_as_normal(self, tmp_path, step):
        """根因：旧代码崩溃 = rc 1 且不写 --out，现行 bash 记成 accruing/degraded（「正常」）。"""
        out = run(step, 1, tmp_path / "absent.json")
        assert frag(out, step) == _problem_frag(step, "missing_json", 1, RC1[step]["status"])
        assert out["level"] == "error" and "分不开" in out["message"]
        assert f"「{RC1[step]['status']}」与「工具崩了」" in out["message"]

    @pytest.mark.parametrize("step,rc", [(s, r) for s in STEPS for r in (3, 124, 99)])
    def test_missing_json_where_expected_is_unchanged(self, tmp_path, step, rc):
        """rc=3（早退）/124（被杀）/其他：没有 JSON 是预期的 ⇒ 与现行逐字相同，不加任何键。"""
        expected = next(g for g in GOLDEN if g[0] == step and g[1] == rc)
        f = frag(run(step, rc, tmp_path / "absent.json"), step)
        exp = dict(expected[4])
        if step == "11":
            exp["cohort_boundary"] = None
        assert f == exp

    @pytest.mark.parametrize("step", STEPS)
    def test_unparsable_json(self, tmp_path, step):
        out = run(step, 1, _write(tmp_path / "bad.json", "{not json"))
        f = frag(out, step)
        assert f == _problem_frag(step, "unparsable_json", 1, RC1[step]["status"], error=f["error"])
        assert f["error"].startswith("JSONDecodeError") and out["level"] == "error"

    def test_binary_garbage_is_unparsable_not_crash(self, tmp_path):
        p = tmp_path / "bin.json"
        p.write_bytes(b"\xff\xfe\x00garbage")
        assert frag(run("12", 1, p), "12")["status"] == "unparsable_json"


# ═══════════════════════════════ 3. 跨午夜：date_rollover ═══════════════════════════════

class TestDateRollover:
    """(a) 生产方不给日期参数时按**自己运行那一刻**取日期；本轮跑过午夜 ⇒ 外壳是 DATE_STR 的次日。
    2026-09-08 实况：Step 10–13 在 09-09 00:27 才跑，`scan_coverage-2026-09-08.json` 里 `"date": "2026-09-09"`。"""

    @pytest.mark.parametrize("step", STEPS)
    def test_next_day_envelope_written_this_run_is_used(self, tmp_path, step):
        p = v1_json(tmp_path, step, date=NEXT, generated_at=ROLL_ISO, mtime=ROLL_TS)
        out = run(step, 1, p, run_start=EVE_START)
        assert frag(out, step) == dict(_subst(RC1[step], p), contract="v1", envelope_status="ok",
                                       date_rollover=True)
        assert out["level"] == RC1_LEVEL[step]            # 兜底已处理，不另抬级别
        m = out["message"]
        assert f"外壳 date={NEXT} 是本轮 {DATE} 的次日" in m and "按本轮结果采用" in m
        assert ost._TOOL_STEPS[step].date_flag in m        # 指明 B 该用哪个参数把日期传进去
        for seg in SUMMARY[step]:
            assert seg in m                                # 内容照用

    def test_incident_shape_2026_09_08(self, tmp_path):
        """Step 12 实况的形状：DATE_STR=09-08，本步 09-09 00:27:50 开始，外壳 date=09-09、00:27:57 写出。"""
        written = datetime(2026, 9, 9, 0, 27, 57).timestamp()
        p = v1_json(tmp_path, "12", date="2026-09-09", mtime=written,
                    generated_at=datetime.fromtimestamp(written).astimezone().isoformat(timespec="seconds"))
        out = run("12", 1, p, date="2026-09-08", run_start=int(datetime(2026, 9, 9, 0, 27, 50).timestamp()))
        f = frag(out, "12")
        assert f["status"] == "degraded" and f["date_rollover"] is True and "rc_status" not in f

    def test_next_day_but_written_before_step_start_is_stale(self, tmp_path):
        p = v1_json(tmp_path, "11", date=NEXT, generated_at=ROLL_ISO, mtime=ROLL_TS)
        f = frag(run("11", 1, p, run_start=int(datetime(2026, 9, 29, 0, 30, 0).timestamp())), "11")
        assert f["status"] == "stale_json" and f["stale"]["reason"] == "date_mismatch" and "date_rollover" not in f

    @pytest.mark.parametrize("env_date", ["2026-09-27", "2026-09-30"], ids=["day-before", "two-days-after"])
    def test_other_date_mismatches_stay_stale(self, tmp_path, env_date):
        """只开「次日」一个方向。前一天（2026-11-01 起温哥华 00:00–01:00 那一小时就是这个形状）与上一轮
        留下的文件分不开 ⇒ 照旧 stale——那一种只能靠 B 显式传日期治。"""
        p = v1_json(tmp_path, "11", date=env_date, generated_at=ROLL_ISO, mtime=ROLL_TS)
        f = frag(run("11", 1, p, run_start=EVE_START), "11")
        assert f["status"] == "stale_json" and "date_rollover" not in f

    def test_without_run_start_uses_generated_at_date_and_mtime(self, tmp_path):
        p = v1_json(tmp_path, "12", date=NEXT, generated_at=ROLL_ISO, mtime=ROLL_TS)
        f = frag(run("12", 1, p), "12")
        assert f["status"] == "degraded" and f["date_rollover"] is True
        # mtime 早于 --date 当天 0 点 ⇒ 不是本轮
        p2 = v1_json(tmp_path, "12", date=NEXT, generated_at=ROLL_ISO, mtime=YESTERDAY_TS, name="old")
        assert frag(run("12", 1, p2), "12")["status"] == "stale_json"
        # generated_at 的日期 ≠ 外壳 date ⇒ 自相矛盾，不放行
        p3 = v1_json(tmp_path, "12", date=NEXT, generated_at=FRESH_ISO, mtime=ROLL_TS, name="odd")
        assert frag(run("12", 1, p3), "12")["status"] == "stale_json"

    def test_unparsable_generated_at_is_not_rollover(self, tmp_path):
        p = v1_json(tmp_path, "12", date=NEXT, generated_at="半夜", mtime=ROLL_TS)
        assert frag(run("12", 1, p, run_start=EVE_START), "12")["status"] == "stale_json"
        assert frag(run("12", 1, p), "12")["status"] == "stale_json"

    def test_rollover_crash_envelope_is_this_runs_crash(self, tmp_path):
        p = tmp_path / "crash.json"
        _write(p, sc.envelope("scan_coverage_gate", NEXT, "error", payload={"error": "X: y"}, generated_at=ROLL_ISO,
                              attention=[sc.attention_item("scan_coverage_gate.crashed", "alarm", "崩了")]),
               mtime=ROLL_TS)
        out = run("12", 3, p, run_start=EVE_START)
        f = frag(out, "12")
        assert f["status"] == "error" and f["date_rollover"] is True and out["level"] == "error"


# ═══════════════════════════════ 4. attention：全部上报 / 封顶 / alarm ⇒ warn ═══════════════════════════════

class TestAttention:
    def test_status_attention_reports_info_items_too(self, tmp_path):
        """(d) 外壳 status=attention ⇒ 生产方说要人看 ⇒ info 条目也报。此前 `.checkpoint` / `.ready`
        这类「该人去跑一下」的 info 条目没有 deadline，被过滤得一条不剩。"""
        items = [sc.attention_item("ic_rerun.resonance_forward.checkpoint", "info",
                                   "共振加成前瞻检验已到T+30检视点 —— 手动跑 `/usr/local/bin/python3 x.py` 看结论"),
                 sc.attention_item("ic_rerun.ready", "info", "IC 重跑已就绪 —— 该跑（需人看结果）")]
        p = v1_json(tmp_path, "11", attention=items)
        out = run("11", 1, p)
        f = frag(out, "11")
        assert f["envelope_status"] == "attention"
        assert [a["id"] for a in f["attention"]] == ["ic_rerun.resonance_forward.checkpoint", "ic_rerun.ready"]
        assert "[info] 共振加成前瞻检验已到T+30检视点" in out["message"]
        assert "[info] IC 重跑已就绪" in out["message"]
        assert out["level"] == "info"                     # info 条目不抬级别，只是不再被藏起来

    def test_all_items_ordered_by_severity(self, tmp_path):
        items = [sc.attention_item("a.info", "info", "甲-知会"),
                 sc.attention_item("a.warn", "warn", "乙-处理"),
                 sc.attention_item("a.alarm", "alarm", "丙-口径")]
        m = run("13", 1, v1_json(tmp_path, "13", attention=items))["message"]
        assert m.index("丙-口径") < m.index("乙-处理") < m.index("甲-知会")

    def test_attention_message_is_capped_with_count(self, tmp_path):
        items = [sc.attention_item(f"x.{i}", "warn", f"第{i:02d}条 " + "描述" * 60) for i in range(30)]
        out = run("12", 1, v1_json(tmp_path, "12", attention=items))
        m = out["message"]
        shown = len(re.findall(r"\[warn\] 第\d\d条", m))
        assert 1 <= shown < 30 and m.endswith(f"…(+{30 - shown})"), m[-40:]
        assert len(m) < 1500
        assert len(frag(out, "12")["attention"]) == 30     # 片段不截：截的只是那一行日志

    def test_status_attention_without_items_is_flagged(self, tmp_path):
        """外壳说要人看却一条没列 ⇒ 生产方前后不一致；照报并抬到 warn，不让它因为形状不对而消失。"""
        out = run("12", 0, v1_json(tmp_path, "12", status="attention"))
        assert "却没有列出任何条目" in out["message"] and out["level"] == "warn"
        assert frag(out, "12")["envelope_status"] == "attention"

    @pytest.mark.parametrize("step", STEPS)
    def test_v1_alarm_item_is_warn_with_siren(self, tmp_path, step):
        """alarm ⇒ warn + 🚨：现行 bash 对同类事件刻意打 WARN（不计入 OVERALL_STATUS），error 留给崩溃类。"""
        p = v1_json(tmp_path, step, attention=[sc.attention_item("x.a", "alarm", "口径可能已经错了")])
        out = run(step, 1, p)
        assert out["level"] == "warn" and "🚨 [alarm] 口径可能已经错了" in out["message"]
        assert frag(out, step)["attention"][0]["id"] == "x.a"

    def test_step12_label_contradiction_alarm_is_warn_like_bash(self, tmp_path):
        """scan_coverage_gate 把来源标签矛盾列成 alarm；bash 在 STEP12_RC=1 支打的是 WARN。"""
        item = sc.attention_item("scan_coverage_gate.label_contradictions", "alarm", "iv_rank 标签说成功、值却为空")
        p = v1_json(tmp_path, "12", attention=[item])
        out = run("12", 1, p)
        assert out["level"] == "warn" == run_bash("12", 1, legacy_json(tmp_path, "12"))[0]
        assert "🚨 [alarm] iv_rank 标签说成功" in out["message"]

    def test_step11_boundary_alarm_text_is_not_duplicated(self, tmp_path):
        """生产方已把世代边界告警列成 alarm 条目（原文 + 处理方式）⇒ 基础段只留一句指针，不再重复一遍。
        条目用真生产方 `ic_rerun_readiness.build_attention` 造，与生产同形。"""
        import ic_rerun_readiness as irr
        items = irr.build_attention({}, BEV_ALARM, {"status": "not_ready"}, {"status": "not_ready"},
                                    {"status": "not_ready", "h1_anchor": {"state": "ok"}})
        assert len(items) == 1 and items[0]["id"].startswith(ost._BOUNDARY_ATTENTION_ID + "."), items
        p = v1_json(tmp_path, "11", attention=items,
                    payload=dict(LEGACY["11"], cohort_boundary_evidence=BEV_ALARM))
        out = run("11", 1, p)
        m = out["message"]
        assert m.count(BEV_ALARM["line"]) == 1, m
        assert m.count("追加**一条更正") == 1, m
        assert "世代边界核对未通过（逐条原文与处理方式见本行「需人看」" in m and f"完整判别在 {p}" in m
        assert out["level"] == "warn"
        assert frag(out, "11")["cohort_boundary"] == BEV_ALARM_KEPT


# ═══════════════════════════════ 5. JSON 问题只在 bash 读 JSON 的那几支覆盖判定 ═══════════════════════════════

class TestJsonProblemOnlyWhereRead:
    def test_step10_timeout_with_garbage_json_stays_timeout(self, tmp_path):
        """Step 10 rc=124：bash 这一支根本不读 JSON（被杀了，留下什么都与判定无关）⇒ 仍是 timeout。"""
        out = run("10", 124, _write(tmp_path / "bad.json", "{not json"))
        f = frag(out, "10")
        assert f["status"] == "timeout" and "rc_status" not in f
        assert f["json_problem"]["status"] == "unparsable_json"
        assert f["json_problem"]["error"].startswith("JSONDecodeError")
        assert out["level"] == "error"                     # bash 自己的超时 ERROR，不是 JSON 抬的

    @pytest.mark.parametrize("step", ("10", "12", "13", "15"))
    def test_rc0_with_stale_json_stays_healthy(self, tmp_path, step):
        out = run(step, 0, v1_json(tmp_path, step, date="2026-09-27"))
        f = frag(out, step)
        assert f["status"] == "healthy" and "rc_status" not in f
        assert f["json_problem"]["status"] == "stale_json"
        assert f["json_problem"]["stale"]["reason"] == "date_mismatch"
        assert out["level"] == "warn"                      # 工具说写了、写的却不是本轮 ⇒ 要人看，但不是 error
        assert "不读该 JSON" in out["message"]

    def test_rc0_missing_json_keeps_verdict(self, tmp_path):
        out = run("15", 0, tmp_path / "absent.json")
        assert frag(out, "15") == {"status": "healthy", "duration_seconds": DUR,
                                   "json_problem": {"status": "missing_json"}}
        assert out["level"] == "warn"

    def test_step11_rc0_missing_json_is_overridden_because_bash_reads_it(self, tmp_path):
        out = run("11", 0, tmp_path / "absent.json")
        assert frag(out, "11") == _problem_frag("11", "missing_json", 0, "ready")
        assert out["level"] == "warn"

    @pytest.mark.parametrize("step", STEPS)
    def test_rc3_unparsable(self, tmp_path, step):
        out = run(step, 3, _write(tmp_path / "bad.json", "{"))
        f = frag(out, step)
        if step == "11":                                   # BOUNDARY_JSON 无条件读 ⇒ 覆盖
            assert f == _problem_frag("11", "unparsable_json", 3, "undetermined", error=f["error"])
        else:
            assert f == {"status": "undetermined", "json_problem": {"status": "unparsable_json", "error": f["json_problem"]["error"]}}
        assert out["level"] == "warn"

    def test_contract_error_in_unread_branch_keeps_status_but_is_error(self, tmp_path):
        """契约不符是生产方 / 接线的 bug（例：--json 指错了文件）：判定仍按退出码，但级别 error。"""
        out = run("10", 0, v1_json(tmp_path, "10", tool="someone_else"))
        f = frag(out, "10")
        assert f["status"] == "healthy" and f["json_problem"]["status"] == "contract_error"
        assert out["level"] == "error"

    def test_crash_envelope_overrides_even_in_unread_branch(self, tmp_path):
        """崩溃是工具的问题不是 JSON 的问题 ⇒ 不受「只在读 JSON 的支覆盖」限制（Step 10 rc=3 bash 不读）。"""
        p = tmp_path / "crash.json"
        sc.write_out(p, sc.envelope("scan_continuity", DATE, "error", payload={"error": "X: y"}, generated_at=FRESH_ISO))
        assert frag(run("10", 3, p), "10")["status"] == "error"


# ═══════════════════════════════ 6. 读文件的硬化 ═══════════════════════════════

def _deep(depth: int) -> str:
    return "[" * depth + "]" * depth


class TestHardening:
    def test_fifo_does_not_block_cli(self, tmp_path):
        """FIFO 没有写端时 `open` 会一直等 ⇒ 整个编排器挂住。必须不读、立刻给出一行。"""
        fifo = tmp_path / "ic.json"
        os.mkfifo(fifo)
        r = _cli(["--step", "11", "--rc", "1", "--date", DATE, "--json", str(fifo)], tmp_path, timeout=20)
        d = _one_json_line(r)
        f = d["steps_fragment"][KEY["11"]]
        assert f["status"] == "unparsable_json" and "FIFO" in f["error"], f

    def test_dev_zero_is_not_read(self, tmp_path):
        """字符设备：`/dev/zero` 会一直读到内存耗尽。不是普通文件一个字节都不读。"""
        r = _cli(["--step", "12", "--rc", "1", "--date", DATE, "--json", "/dev/zero"], tmp_path, timeout=20)
        f = _one_json_line(r)["steps_fragment"][KEY["12"]]
        assert f["status"] == "unparsable_json" and "字符设备" in f["error"], f

    def test_directory_is_unparsable(self, tmp_path):
        f = frag(run("10", 1, tmp_path), "10")
        assert f["status"] == "unparsable_json" and "目录" in f["error"]

    def test_oversized_file_is_unparsable(self, tmp_path):
        p = tmp_path / "big.json"
        p.write_text('{"pad": "' + "x" * (ost.MAX_JSON_BYTES + 10) + '"}', encoding="utf-8")
        f = frag(run("13", 1, p), "13")
        assert f["status"] == "unparsable_json" and "过大" in f["error"]

    def test_deep_but_valid_json_is_a_problem_not_a_render_error(self, tmp_path):
        """500 层：`json.loads` 解析得了，但本模块的递归遍历 / `json.dumps` 撞 RecursionError（此前落成 render_error，
        说不出是 JSON 的问题）。现在按「不可解析」记、原因写明。"""
        p = tmp_path / "deep.json"
        body = json.dumps(dict(LEGACY["11"], cohort_boundary_evidence=dict(BEV, unmarked_after_boundary="@@")))
        _write(p, body.replace('"@@"', _deep(500)))
        out = run("11", 1, p)
        f = frag(out, "11")
        assert f["status"] == "unparsable_json" and "嵌套" in f["error"], f
        assert out["level"] == "error"                     # rc=1 且这一支读 JSON：本轮那份等于没有

    def test_depth_2000_via_cli_is_one_line(self, tmp_path):
        p = _write(tmp_path / "deep.json", _deep(2000))
        d = _one_json_line(_cli(["--step", "15", "--rc", "1", "--date", DATE, "--json", str(p)], tmp_path, timeout=20))
        f = d["steps_fragment"][KEY["15"]]
        assert f["status"] == "unparsable_json" and "嵌套" in f["error"], f

    def test_step2_evidence_fifos_do_not_block(self, tmp_path):
        """Step 2 核对的 `.swarm_results` 与 `scan_timing.json` 走同一套安全读。"""
        d = make_day(tmp_path / "d", timing=None, swarm=False)
        (d / "logs").mkdir(exist_ok=True)
        os.mkfifo(d / "logs" / "scan_timing.json")
        os.mkfifo(d / f".swarm_results_{DATE}.json")
        r = _cli(["--step", "2", "--rc", "1", "--date", DATE, "--data-dir", str(d), "--run-start", str(RUN_START)],
                 tmp_path, timeout=20)
        f = _one_json_line(r)["steps_fragment"][S2]
        assert f["status"] == "failed"
        assert sum("FIFO" in x for x in f["rc1_unverified"]) == 2, f

    def test_git_push_success_of_odd_shape_is_unknown_not_render_error(self, tmp_path):
        d = make_day(tmp_path / "d")
        t = d / "logs" / "scan_timing.json"
        doc = json.loads(t.read_text())
        doc["extra"]["git_push"]["success"] = [1]
        t.write_text(json.dumps(doc))
        out = run("2", 1, data_dir=d, run_start=RUN_START)
        f = out["steps_fragment"][S2]
        assert f["status"] == "success_with_warning" and f["git_push_success"] is None
        assert "推送结果未知" in out["message"]


# ═══════════════════════════════ 7. 全组合不变式 ═══════════════════════════════

def _make_variant(tmp_path: Path, step: str, variant: str):
    if variant == "v1":
        return v1_json(tmp_path, step)
    if variant == "attention":
        return v1_json(tmp_path, step, attention=ATTN)
    if variant == "legacy":
        return legacy_json(tmp_path, step)
    if variant == "stale":
        return v1_json(tmp_path, step, date="2026-09-27")
    if variant == "contract":
        return v1_json(tmp_path, step, tool="someone_else")
    if variant == "crashed":
        p = tmp_path / "crash.json"
        sc.write_out(p, sc.envelope(TOOL[step], DATE, "error", payload={"error": "X: y"}, attention=[
            sc.attention_item(f"{TOOL[step]}.crashed", "alarm", "崩了")]))
        return p
    if variant == "missing":
        return tmp_path / "absent.json"
    if variant == "unparsable":
        return _write(tmp_path / "bad.json", "{")
    raise AssertionError(variant)


VARIANTS = ("v1", "attention", "legacy", "stale", "contract", "crashed", "missing", "unparsable")
RCS = (0, 1, 2, 3, 124, 99)
PROBLEM_OF = {"stale": "stale_json", "contract": "contract_error", "unparsable": "unparsable_json"}


class TestSweep:
    @pytest.mark.parametrize("variant", VARIANTS)
    @pytest.mark.parametrize("rc", RCS)
    @pytest.mark.parametrize("step", STEPS)
    def test_invariants(self, tmp_path, step, rc, variant, capsys):
        out = run(step, rc, _make_variant(tmp_path, step, variant))
        f = frag(out, step)
        json.dumps(out, allow_nan=False)                               # jq 吃得下
        base, base_level = BASE_STATUS[(step, rc)], BASE_LEVEL[(step, rc)]
        if rc == 2:
            assert f["status"] == "skipped" and "json_problem" not in f   # 不读 JSON
            return
        problem = PROBLEM_OF.get(variant)
        if variant == "missing" and rc in (0, 1):
            problem = "missing_json"
        reads = _reads(step, rc)
        if variant == "crashed":
            expected_status = "error"                                  # 工具的问题，哪一支都覆盖
        elif problem and reads:
            expected_status = problem
        else:
            expected_status = base
        assert f["status"] == expected_status, (variant, f)
        # (a') 不读 JSON 的那几支：判定按退出码，问题只进 json_problem
        if problem and not reads:
            assert f["json_problem"]["status"] == problem and "rc_status" not in f, f
        else:
            assert "json_problem" not in f, f
        if f["status"] in ("stale_json", "contract_error", "missing_json", "unparsable_json") or variant == "crashed":
            assert f["rc"] == rc and f["rc_status"] == base
            if step == "11":
                assert f["cohort_boundary"] is None                   # 不可用 JSON 的内容一个字都不用
        # 级别
        if variant in ("contract", "crashed"):
            assert out["level"] == "error"
        elif problem and reads and rc == 1:
            assert out["level"] == "error"
        elif problem:
            assert out["level"] == ost._max_level(base_level, "warn")
        elif variant == "attention":
            assert out["level"] == ost._max_level(base_level, "warn")
        else:
            assert out["level"] == base_level
        # (d) error 只留给：崩溃 / 契约不符 / 读 JSON 的 rc=1 支里 JSON 不可用 / bash 自己的 ERROR 支
        if out["level"] == "error":
            assert (variant in ("contract", "crashed") or (problem and reads and rc == 1)
                    or base_level == "error"), (variant, out)
        if variant in ("v1", "attention"):
            assert f["contract"] == "v1" and f["envelope_status"] == ("attention" if variant == "attention" else "ok")
        if variant == "legacy":
            assert f["contract"] == "legacy" and "envelope_status" not in f


# ═══════════════════════════════ 8. Step 2 与 Step 4/5 ═══════════════════════════════
TICKERS = ["NVDA", "TSLA", "MSFT", "QCOM", "VKTX", "META", "BILI", "AMZN"]


def make_day(root: Path, date: str = DATE, *, swarm=True, daily=("json", "md"), timing="complete",
             ml="near_constant", written_at="2026-09-28T14:46:43", git_push_success=True,
             report_root=None) -> Path:
    """造一天的 Step 2 产物。ml：near_constant = 09-24 实况（8 份里 6 份同值、共 2 个不同值）。"""
    rep = report_root or root
    root.mkdir(parents=True, exist_ok=True)
    rep.mkdir(parents=True, exist_ok=True)
    if swarm:
        (root / f".swarm_results_{date}.json").write_text(json.dumps({t: {"final_score": 5.0} for t in TICKERS}))
    for suffix in daily:
        (rep / f"alpha-hive-daily-{date}.{suffix}").write_text("{}" if suffix == "json" else "# 日报")
    if timing:
        extra = {"early_exit": "empty_scan_guard"} if timing == "early_exit" else {
            "git_push": {"success": git_push_success}, "git_commit": {"success": True}}
        (root / "logs").mkdir(exist_ok=True)
        (root / "logs" / "scan_timing.json").write_text(json.dumps(
            {"date": "2026-09-27" if timing == "wrong_date" else date, "written_at": written_at,
             "phases": {}, "counters": {}, "extra": extra}))
    probs = {"near_constant": [0.5211139065066932] * 6 + [0.48] * 2,
             "constant": [0.5899693787928219] * 8,
             "ok": [0.40 + 0.02 * i for i in range(8)]}.get(ml)
    for t, p in zip(TICKERS, probs or []):
        (rep / f"analysis-{t}-ml-{date}.json").write_text(json.dumps(
            {"ml_prediction": {"prediction": {"probability": p}, "input": {"momentum_5d": TICKERS.index(t)}},
             "swarm_results": {"x": 1}}))
    return root


S2 = "step2_hive_analysis"
#: (rc, 级别, 片段)——rc=1 这里是「无法确认跑完」的那一种（与 bash 的 else 支同判；解释器另追加 rc1_unverified）
STEP2_GOLDEN = [
    (0, "info", {"status": "success", "duration_seconds": DUR}),
    (124, "error", {"status": "timeout", "duration_seconds": DUR}),
    (2, "warn", {"status": "skipped"}),
    (1, "warn", {"status": "failed", "duration_seconds": DUR}),
    (99, "warn", {"status": "failed", "duration_seconds": DUR}),
]


class TestStep2:
    @pytest.mark.parametrize("rc,level,expected", STEP2_GOLDEN, ids=[f"STEP2_RC={g[0]}" for g in STEP2_GOLDEN])
    def test_golden_without_completion_proof(self, tmp_path, rc, level, expected):
        out = run("2", rc, data_dir=tmp_path / "empty", timeout=3600)
        f = dict(out["steps_fragment"][S2])
        unverified = f.pop("rc1_unverified", None)
        assert f == expected and out["level"] == level
        assert (unverified is not None) == (rc == 1)
        b_level, b_texts, b_frag = run_bash("2", rc)          # 冻结副本里 STEP2_RC 的分支链真跑
        assert (b_level, b_frag) == (level, expected)
        assert_message_carries_bash_lines(out["message"], b_texts)

    def test_timeout_message_carries_budget(self, tmp_path):
        assert "⏰ Step 2 超时（>3600s），蜂群分析被终止！" == run("2", 124, timeout=3600)["message"]

    def test_rc1_after_completed_scan_is_success_with_warning(self, tmp_path):
        """(g) 09-24/25 实况：ML 准常数 ⇒ exit 1，但日报与部署都做完了。"""
        d = make_day(tmp_path / "d")
        out = run("2", 1, data_dir=d, run_start=RUN_START)
        assert out["steps_fragment"] == {S2: {
            "status": "success_with_warning", "duration_seconds": DUR, "rc": 1, "warning": "ml_model_constant",
            "ml_model_guard": {"verdict": "near_constant", "n_files": 8, "n_numeric": 8, "distinct": 2},
            "git_push_success": True}}
        assert out["level"] == "warn"
        assert "ML 概率退化（near_constant：8 份里只有 2 个不同值）" in out["message"]
        assert "ml_model_guard.py --date 2026-09-28" in out["message"] and "推送成功" in out["message"]

    def test_rc1_constant_and_push_failure_are_both_visible(self, tmp_path):
        d = make_day(tmp_path / "d", ml="constant", git_push_success=False)
        out = run("2", 1, data_dir=d, run_start=RUN_START)
        f = out["steps_fragment"][S2]
        assert f["ml_model_guard"]["verdict"] == "constant" and f["git_push_success"] is False
        # 指向 scan_timing.json 本身，不指 status.json（那条 jq 合并 09-14~09-25 没生效过，见 v0.45.351）
        assert f"推送失败（见 {d / 'logs' / 'scan_timing.json'} 的 extra.git_push）" in out["message"]

    def test_rc1_completed_but_ml_not_degenerate_says_unexplained(self, tmp_path):
        """主流程跑完 ⇒ 仍是 success_with_warning（部署确已完成），但原因如实写「待查」，不编一个。"""
        out = run("2", 1, data_dir=make_day(tmp_path / "d", ml="ok"), run_start=RUN_START)
        f = out["steps_fragment"][S2]
        assert f["status"] == "success_with_warning" and f["warning"] == "rc1_after_completion_unexplained"
        assert f["ml_model_guard"]["verdict"] == "ok" and "待查" in out["message"] and out["level"] == "warn"

    @pytest.mark.parametrize("kw,needle", [
        (dict(swarm=False), ".swarm_results_2026-09-28.json 不存在"),
        (dict(daily=("json",)), "alpha-hive-daily-2026-09-28.md 不存在或为空"),
        (dict(timing=None), "scan_timing.json 不存在"),
        (dict(timing="early_exit"), "早退"),
        (dict(timing="wrong_date"), "date='2026-09-27'"),
    ], ids=["no-swarm", "no-md", "no-timing", "early-exit", "timing-wrong-date"])
    def test_rc1_without_completion_proof_stays_failed(self, tmp_path, kw, needle):
        """产物在 ≠ 跑完：`.swarm_results` 早在 enrichment 就写了、部署段只兜四种异常 ⇒ 崩在中途也 rc=1。"""
        out = run("2", 1, data_dir=make_day(tmp_path / "d", **kw))
        f = out["steps_fragment"][S2]
        assert f["status"] == "failed" and f["duration_seconds"] == DUR
        assert any(needle in x for x in f["rc1_unverified"]), f
        assert out["level"] == "warn" and "Python 未捕获异常的退出码也是 1" in out["message"]

    def test_rc1_without_run_start_is_not_upgraded(self, tmp_path):
        """同日重跑时上一轮的 scan_timing.json 日期同样对得上；`run_step` 在 TCC 拒绝时也返回 1
        （本轮根本没跑）⇒ 没有 --run-start 就分不出「本轮跑完了」，保守记 failed（与现行一致）。"""
        f = run("2", 1, data_dir=make_day(tmp_path / "d"))["steps_fragment"][S2]
        assert f["status"] == "failed" and any("--run-start" in x for x in f["rc1_unverified"]), f
        assert run("4", 1, data_dir=tmp_path / "d")["steps_fragment"]["step4_dashboard"]["status"] == "failed"

    def test_rc1_scan_timing_from_before_this_step_is_not_proof(self, tmp_path):
        d = make_day(tmp_path / "d", written_at="2026-09-28T14:46:43")
        f = run("2", 1, data_dir=d, run_start=LATE_START)["steps_fragment"][S2]
        assert f["status"] == "failed" and any("早于本步开始" in x for x in f["rc1_unverified"])

    def test_rc1_without_any_data_dir_is_failed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        f = run("2", 1)["steps_fragment"][S2]
        assert f["status"] == "failed" and "ALPHA_HIVE_HOME" in f["rc1_unverified"][0]

    def test_data_dir_defaults_to_alpha_hive_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(make_day(tmp_path / "home")))
        assert run("2", 1, run_start=RUN_START)["steps_fragment"][S2]["status"] == "success_with_warning"

    def test_report_dir_is_where_daily_and_ml_files_are(self, tmp_path):
        d = make_day(tmp_path / "data", report_root=tmp_path / "rep")
        assert run("2", 1, data_dir=d, report_dir=tmp_path / "rep", run_start=RUN_START)["steps_fragment"][S2][
            "status"] == "success_with_warning"
        assert run("2", 1, data_dir=d, run_start=RUN_START)["steps_fragment"][S2]["status"] == "failed"  # 日报不在 data-dir

    def test_reads_only(self, tmp_path):
        """核对产物是纯读：跑完前后目录树逐字相同。"""
        d = make_day(tmp_path / "d")
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in d.rglob("*")}
        run("2", 1, data_dir=d)
        run("4", 1, data_dir=d)
        assert {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in d.rglob("*")} == before


S4 = "step4_dashboard"


class TestStep4:
    def test_rc0_is_skipped_builtin(self, tmp_path):
        out = run("4", 0)
        assert out["steps_fragment"] == {S4: {"status": "skipped_builtin"}}      # 【Step 4/5】段 STEP2_RC=0
        assert out["level"] == "info" and "Step 4 跳过（仪表板已由 Step 2 pipeline 生成）" in out["message"]
        b_level, b_texts, b_frag = run_bash("4", 0)
        assert (b_level, b_frag) == ("info", {"status": "skipped_builtin"})
        assert_message_carries_bash_lines(out["message"], b_texts)

    @pytest.mark.parametrize("rc", [1, 2, 124, 99])
    def test_step2_not_complete_is_failed_error(self, tmp_path, rc):
        out = run("4", rc, data_dir=tmp_path / "empty")
        expected = {"status": "failed", "reason": "step2_did_not_complete", "step2_rc": rc}
        assert out["steps_fragment"] == {S4: expected}                           # 【Step 4/5】段 else
        assert out["level"] == "error" and f"Step 2 没跑完（RC={rc}）" in out["message"]
        b_level, b_texts, b_frag = run_bash("4", rc)
        assert (b_level, b_frag) == ("error", expected)
        assert_message_carries_bash_lines(out["message"], b_texts)

    def test_rc1_completed_scan_is_not_reported_as_not_generated(self, tmp_path):
        """(g) 09-24/25：仪表板已生成却打「Step 4 未生成仪表板！」ERROR——这里改成与 rc=0 同一支（追加 step2_status）。"""
        out = run("4", 1, data_dir=make_day(tmp_path / "d"), run_start=RUN_START)
        assert out["steps_fragment"] == {S4: {"status": "skipped_builtin", "step2_status": "success_with_warning"}}
        assert out["level"] == "info" and "未生成" not in out["message"]

    def test_step5_is_not_this_interpreters_business(self):
        """Step 5 自 v0.45.351 起由 report_deployer 按部署日志判：这里不许再长出第二份判据。"""
        assert ost._normalize_step("5") is None and ost._normalize_step("4_5") is None
        assert all("step5_github_deploy" not in keys for keys in ost.STEP_KEYS.values())


# ═══════════════════════════════ 9. CLI 与 render_error ═══════════════════════════════

def _cli(argv, cwd, timeout: int = 60):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, str(REPO_ROOT / "orchestrator_steps.py"), *argv],
                       capture_output=True, cwd=str(cwd), env=env, timeout=timeout)
    return r


def _one_json_line(r) -> dict:
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    out = r.stdout.decode("ascii")                     # ensure_ascii：纯 ASCII
    assert out.endswith("\n") and out.count("\n") == 1, out
    d = json.loads(out)
    assert set(d) == {"level", "message", "steps_fragment"} and d["level"] in ost.LEVELS
    assert d["steps_fragment"] and all(isinstance(v.get("status"), str) for v in d["steps_fragment"].values())
    return d


class TestCli:
    @pytest.mark.parametrize("argv", [
        [],
        ["--help"],
        ["--step"],
        ["--step", "10"],
        ["--step", "10", "--rc", "abc", "--date", DATE],
        ["--step", "nope", "--rc", "1", "--date", DATE],
        ["--step", "10", "--rc", "1", "--date", "2026-9-28"],
        ["--step", "10", "--rc", "1", "--date", DATE, "--run-start", "yesterday"],
        ["--step", "11", "--rc", "1", "--date", DATE, "--json", "/dev/null/nope"],
        ["--step", "11", "--rc", "1", "--date", DATE, "--bogus", "x"],
        ["--step", "2", "--rc", "1", "--date", DATE, "--data-dir", "/nonexistent/dir"],
    ], ids=lambda a: " ".join(a) or "<none>")
    def test_garbage_in_one_json_line_out_rc0(self, tmp_path, argv):
        _one_json_line(_cli(argv, tmp_path))

    @pytest.mark.parametrize("argv,needle", [
        (["--step", "10", "--date", DATE], "--rc"),
        (["--step", "10", "--rc", "1", "--date", DATE, "--bogus", "x"], "--bogus"),
    ], ids=["missing-rc", "unknown-arg"])
    def test_argument_errors_say_what_was_wrong(self, argv, needle, capsys):
        """argparse 默认 `sys.exit(2)`：兜底虽接得住，但 message 只剩「SystemExit: 2」——说不出错在哪。"""
        out = ost.render(argv)
        assert out["level"] == "error" and needle in out["message"], out["message"]
        assert out["steps_fragment"] == {"step10_scan_continuity": {"status": "render_error",
                                                                    "rc": 1 if "--rc" in argv else None}}
        assert capsys.readouterr().out == ""            # 用法文本不许漏进 stdout

    def test_render_error_fragment_names_the_step_when_known(self, tmp_path):
        d = json.loads(_cli(["--step", "11", "--rc", "abc", "--date", DATE], tmp_path).stdout)
        assert d["steps_fragment"] == {"step11_ic_rerun_readiness": {"status": "render_error", "rc": "abc"}}
        assert d["level"] == "error" and d["message"].startswith("render_error: ")

    @pytest.mark.parametrize("bad", ["2026-9-28", "tomorrow", ""])
    def test_bad_date_is_render_error_not_silently_used(self, tmp_path, bad):
        """--date 是新鲜度的基准：不规范就拒绝解释（字符串比较下 "2026-9-28" 永远不等于外壳里的日期）。"""
        d = json.loads(_cli(["--step", "10", "--rc", "0", "--date", bad], tmp_path).stdout)
        assert d["level"] == "error" and "YYYY-MM-DD" in d["message"]
        assert d["steps_fragment"] == {"step10_scan_continuity": {"status": "render_error", "rc": 0}}

    def test_binary_json_file_via_cli(self, tmp_path):
        p = tmp_path / "bin.json"
        p.write_bytes(b"\x00\xff" * 50)
        d = json.loads(_cli(["--step", "12", "--rc", "1", "--date", DATE, "--json", str(p)], tmp_path).stdout)
        assert d["steps_fragment"]["step12_scan_coverage"]["status"] == "unparsable_json"

    def test_cli_equals_in_process(self, tmp_path):
        p = v1_json(tmp_path, "11", attention=ATTN)
        argv = ["--step", "step11_ic_rerun_readiness", "--rc", "1", "--date", DATE, "--json", str(p)]
        assert json.loads(_cli(argv, tmp_path).stdout) == ost.render(argv)

    def test_real_producer_crash_is_never_recorded_as_accruing(self, tmp_path):
        """实测根因：`ic_rerun_readiness --db <坏库>`。改造前 rc=1、不写 --out（本解释器记 missing_json）；
        改造后 rc=3 + 错误外壳（记 error）。两种都必须是 error 级，绝不能是 accruing / undetermined。"""
        db = tmp_path / "pheromone.db"
        db.write_text("this is not sqlite")
        out = tmp_path / "r.json"
        env = dict(os.environ, ALPHA_HIVE_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, str(REPO_ROOT / "ic_rerun_readiness.py"), "--db", str(db),
                            "--quiet", "--out", str(out), "--today", DATE],
                           capture_output=True, cwd=str(tmp_path), env=env, timeout=120)
        assert r.returncode in (1, 3), r.stderr.decode(errors="replace")
        res = run("11", r.returncode, out)
        f = frag(res, "11")
        assert f["status"] in ("missing_json", "error") and res["level"] == "error", f
        assert f["cohort_boundary"] is None


class TestRenderError:
    def test_internal_exception_becomes_render_error(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(ost, "_load_doc", boom)
        out = ost.render(["--step", "10", "--rc", "1", "--json", str(tmp_path / "x.json"), "--date", DATE])
        assert out == {"level": "error", "message": "render_error: RuntimeError: boom",
                       "steps_fragment": {"step10_scan_continuity": {"status": "render_error", "rc": 1}}}

    def test_render_error_in_step2_outcome_names_step4(self, monkeypatch):
        """Step 2 / 4 共用的判定抛错 ⇒ 两步各自 render_error，片段键是各自的键。"""
        monkeypatch.setattr(ost, "_step2_outcome", lambda ctx: 1 / 0)
        out4 = ost.render(["--step", "4", "--rc", "1", "--date", DATE])
        assert out4["steps_fragment"] == {"step4_dashboard": {"status": "render_error", "rc": 1}}
        assert out4["message"].startswith("render_error: ZeroDivisionError")
        out2 = ost.render(["--step", "2", "--rc", "1", "--date", DATE])
        assert out2["steps_fragment"] == {"step2_hive_analysis": {"status": "render_error", "rc": 1}}

    def test_bad_shape_from_internals_is_render_error(self, monkeypatch):
        monkeypatch.setattr(ost, "_interpret", lambda sid, ctx: ("fatal", ["x"], {"k": {"status": "s"}}))
        out = ost.render(["--step", "12", "--rc", "0", "--date", DATE])
        assert out["steps_fragment"] == {"step12_scan_coverage": {"status": "render_error", "rc": 0}}

    def test_stray_print_does_not_break_the_one_line(self, monkeypatch, capsys):
        real = ost._interpret

        def noisy(sid, ctx):
            print("某个被 import 的模块在 import 期打了一行")
            return real(sid, ctx)
        monkeypatch.setattr(ost, "_interpret", noisy)
        assert ost.main(["--step", "2", "--rc", "0", "--date", DATE, "--duration", "5"]) == 0
        cap = capsys.readouterr()
        assert cap.out.count("\n") == 1 and json.loads(cap.out)["steps_fragment"] == {
            "step2_hive_analysis": {"status": "success", "duration_seconds": 5}}
        assert "import 期打了一行" in cap.err

    def test_nan_in_fragment_is_sanitized_not_fatal(self, monkeypatch, capsys):
        monkeypatch.setattr(ost, "_interpret",
                            lambda sid, ctx: ("warn", ["x"], {"step10_scan_continuity": {"status": "s",
                                                                                         "v": float("nan")}}))
        assert ost.main(["--step", "10", "--rc", "1", "--date", DATE]) == 0
        assert json.loads(capsys.readouterr().out)["steps_fragment"]["step10_scan_continuity"]["v"] is None

    def test_main_never_raises_even_on_base_exceptions(self, monkeypatch, capsys):
        def interrupt(*a, **k):
            raise KeyboardInterrupt
        monkeypatch.setattr(ost, "_interpret", interrupt)
        try:
            rc = ost.main(["--step", "13", "--rc", "1", "--date", DATE])
        except KeyboardInterrupt:      # 穿透了就记成本条失败——不让它把整个 pytest 会话一起打断
            pytest.fail("KeyboardInterrupt 穿透了 main()：bash 那一行拿不到，只剩 rc")
        assert rc == 0
        d = json.loads(capsys.readouterr().out)
        assert d["steps_fragment"] == {"step13_calendar_watch": {"status": "render_error", "rc": 1}}


# ═══════════════════════════════ 10. 文档只存指针 ═══════════════════════════════
#: 编排器行号 / 行数 / sha 快照的形状（CLAUDE.md「文档分工原则」：行号随上游任何一处改动整体漂移，
#: v0.45.351 在 Step 5 插了 29 行，此前逐行抄的行号当场全部作废）
_SNAPSHOT_PATTERNS = {
    "行号区间": r"(?<![\d.])(?:[6-9]\d\d|1[0-5]\d\d)\s*[–-]\s*(?:[6-9]\d\d|1[0-5]\d\d)(?![\d.])",
    "行号并列": r"(?<![\d.])(?:[6-9]\d\d|1[0-5]\d\d)\s*/\s*(?:[6-9]\d\d|1[0-5]\d\d)(?![\d.])",
    "编排器+行号": r"编排器\s*\d{3,4}",
    "行数": r"(?<![\d.])\d{3,4}\s*行[，,）)]",
    "sha": r"sha256\s*[0-9a-f]{6}",
}


class TestDocPointers:
    @pytest.mark.parametrize("path", [REPO_ROOT / "orchestrator_steps.py", Path(__file__)],
                             ids=["orchestrator_steps.py", "test_orchestrator_steps.py"])
    def test_no_orchestrator_line_number_snapshots(self, path):
        text = path.read_text(encoding="utf-8")
        hits = [(name, m.group(0), text.count("\n", 0, m.start()) + 1)
                for name, pat in _SNAPSHOT_PATTERNS.items() for m in re.finditer(pat, text)]
        assert not hits, f"{path.name} 里又出现了编排器行号 / sha 快照（按 shell 变量名 / 步骤标题引用）：{hits}"

    @pytest.mark.parametrize("step", STEPS)
    def test_date_flag_exists_in_producer_argparse(self, step):
        """「B 必须显式传 DATE_STR」的参数名唯一真相是 `_ToolStep.date_flag`；它得在生产方 argparse 里真实存在。"""
        tool_step = ost._TOOL_STEPS[step]
        tree = ast.parse((REPO_ROOT / f"{tool_step.tool}.py").read_text(encoding="utf-8"))
        flags = {a.value for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_argument"
                 for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
        assert tool_step.date_flag in flags, (tool_step.tool, tool_step.date_flag, sorted(flags))

    def test_docstring_tells_b_to_pass_the_date(self):
        doc = ost.__doc__
        assert "B 接线须知" in doc and "date_flag" in doc
        assert "2026-11-01" in doc and "America/Vancouver" in doc and "date_rollover" in doc
