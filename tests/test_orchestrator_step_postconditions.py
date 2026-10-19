"""编排器步骤产出后置条件（v0.45.543）：退出码 0 ≠ 干成了。

墓碑：为什么有这个文件
--------------------
v0.45.534 删掉的 Step 8（`metrics_collector.py --record`）没有 `__main__`，每天 0 秒退出 0，
`step8_metrics_collection: success` 记了几个月。`run_step` 只看退出码 ⇒「脚本根本没干活」与「干成了」同为 0，
**没有任何东西会红**。逐个删死步骤治不了根：下一个「跑了但什么都没写」的步骤照样记 success。

现在每个 `run_step` 调用都必须声明「凭什么说它干成了」（`STEP_OUTPUTS`，按脚本名登记，双向核对）：

  · INTERPRETER  —— 结局经 `_apply_step_interp` 交给 `orchestrator_steps.py`，它核本轮 `--out` JSON 的新鲜度
                    （`stale_json` / `missing_json` 早已会红）。
  · POSTCONDITION —— 跑之前 `$(_step_marker)`，退出码 0 后 `_no_output <key> …产出路径`：产出不在、或比标记旧
                    ⇒ `{"status": "failed", "reason": "no_output", "missing": […]}` + ERROR 日志；
                    整体状态同该步 rc≠0 支（Step 3 ⇒ partial；Step 6 不升级）。alert_manager 对 `failed` 出 P1。
  · READ_BACK    —— 调用点之后自己回读本轮记录（先删后写的 `--out` / 按日期核的 status 文件）；登记它读的那个变量名，
                    段里没有就红。
  · EXEMPT       —— 退出码 0 在生产路径上不可达等，写明理由。

本文件的断言分三层：
  1. 真跑：从仓库编排器（`tests/_orchestrator.py`，CI 上也跑）抽出三个函数与 Step 3 / 6 整块，
     配假 `run_step`（退出 0、写或不写产出）在 bash 里跑，看 `STEPS_RESULT` / 整体状态。
  2. 登记表：每个 `run_step` 调用归类、归类的证据在段里；登记了却不再调用也红（怕它变大，也怕它变小）。
  3. 有牙：把函数源码做变异（`-ot` 判据拿掉 / 永远返回「有产出」/ 不写片段）后再跑同一个用例，必须变结果。

范围外：Step 7（`push_report_to_slack.py`）生产恒退出 2（`skipped_manual`）——它不冒充成功，但每天跑一次只为退出 2，
本身是另一条死步骤，归死链清理队列（auto-memory `alpha-hive-dead-field.md`），不在本守卫里删。
需要 `/bin/bash` 与 `jq`（编排器本身就依赖二者；CI 的 ubuntu-latest 自带）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from tests._orchestrator import extract_function, repo_orchestrator_text

INTERPRETER = "解释器核本轮 --out"
POSTCONDITION = "后置条件"
READ_BACK = "调用点自己回读本轮记录"
EXEMPT = "豁免"
KINDS = {INTERPRETER, POSTCONDITION, READ_BACK, EXEMPT}

#: 脚本名 → (类别, 证据)。POSTCONDITION 的证据 = `_no_output` 的 key；READ_BACK 的证据 = 段里必须出现的变量名。
STEP_OUTPUTS = {
    "deploy_orchestrator.py": (READ_BACK, "_rec"),            # 部署记录先删后写，`_orchestrator_autodeploy` 读回
    "run_data_migrations.py": (READ_BACK, "_rec"),            # 迁移记录先删后写
    "alpha_hive_daily_report.py": (INTERPRETER, "2"),
    "generate_ml_report.py": (POSTCONDITION, "step3_ml_report"),
    "alert_manager.py": (POSTCONDITION, "step6_alert_analysis"),
    "push_report_to_slack.py": (EXEMPT, "无 --force 恒 exit 2（skipped_manual）；编排器从不传 --force，退出码 0 不可达"),
    "scan_continuity.py": (INTERPRETER, "10"),
    "ic_rerun_readiness.py": (INTERPRETER, "11"),
    "scan_coverage_gate.py": (INTERPRETER, "12"),
    "economic_calendar_watch.py": (INTERPRETER, "13"),
    "run_data_backup.py": (READ_BACK, "BACKUP_STATUS_JSON"),  # backup_status.json 按 stage + 今天的日期核
    "backup_continuity.py": (INTERPRETER, "15"),
    "orchestrator_steps.py": (READ_BACK, "_line"),            # 解释器自己：输出经 jq 形状校验，不合格退回按退出码
}

_RUN_STEP = re.compile(r'^[ \t]*run_step[ \t]+(?:--timeout[ \t]+\S+[ \t]+)?"?\$\{?PROJECT_DIR\}?/([\w.]+)"?', re.M)
_SEGMENT_LINES = 60


def run_step_calls(text: str) -> list:
    """[(脚本名, 行号)]：非注释的 `run_step` 调用（不含函数定义本身）。"""
    out = []
    for m in _RUN_STEP.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if text[line_start:m.start()].lstrip().startswith("#"):
            continue
        out.append((m.group(1), text.count("\n", 0, m.start()) + 1))
    return out


def classify_problems(text: str, registry: dict) -> list:
    """登记表与编排器原文对账；返回问题列表（空 = 通过）。"""
    problems = []
    lines = text.splitlines()
    calls = run_step_calls(text)
    called = {s for s, _ in calls}
    for script in sorted(called - set(registry)):
        problems.append(f"未登记的 run_step：{script}——声明它的产出（POSTCONDITION / INTERPRETER / READ_BACK）或写明豁免理由")
    for script in sorted(set(registry) - called):
        problems.append(f"登记了却不再被 run_step 调用：{script}（删条目）")
    for script, lineno in calls:
        if script not in registry:
            continue
        kind, evidence = registry[script]
        after = "\n".join(lines[lineno - 1:lineno - 1 + _SEGMENT_LINES])
        before = "\n".join(lines[max(0, lineno - 4):lineno - 1])
        if kind == INTERPRETER and not re.search(rf"_apply_step_interp {re.escape(evidence)} ", after):
            problems.append(f"{script}（第 {lineno} 行）登记为 INTERPRETER，其后 {_SEGMENT_LINES} 行没有 `_apply_step_interp {evidence}`")
        elif kind == POSTCONDITION:
            if "$(_step_marker)" not in before:
                problems.append(f"{script}（第 {lineno} 行）登记为 POSTCONDITION，前 3 行没有 `$(_step_marker)`")
            if not re.search(rf"_no_output {re.escape(evidence)} ", after):
                problems.append(f"{script}（第 {lineno} 行）登记为 POSTCONDITION，其后没有 `_no_output {evidence}`")
        elif kind == READ_BACK and evidence not in after:
            problems.append(f"{script}（第 {lineno} 行）登记为 READ_BACK，其后没有读回 `{evidence}`")
    return problems


def _block(text: str, header: str, next_header: str) -> str:
    start = text.index(header)
    end = text.index(next_header, start)
    return text[start:end]


_FUNCS = ("_step_marker", "_postcondition", "_no_output")


def _funcs(text: str) -> str:
    return "\n".join(extract_function(text, f) for f in _FUNCS)


def _bash(body: str, *, tmp: Path, locale: str = "") -> dict:
    harness = f"""set -uo pipefail
log() {{ printf 'LOG[%s] %s\\n' "$1" "$2"; }}
OVERALL_STATUS=success
set_status() {{ OVERALL_STATUS="$1"; }}
LOGFILE=/dev/null
LOGDIR={json.dumps(str(tmp / "logs"))}
DATA_DIR={json.dumps(str(tmp / "data"))}
PROJECT_DIR={json.dumps(str(tmp / "proj"))}
STEPS_RESULT='{{}}'
mkdir -p "$LOGDIR" "$DATA_DIR"
{body}
printf 'RESULT=%s\\n' "$(echo "$STEPS_RESULT" | jq -c .)"
printf 'STATUS=%s\\n' "$OVERALL_STATUS"
"""
    env = {k: v for k, v in os.environ.items() if k not in ("LC_ALL", "LANG", "LC_CTYPE")}
    env.update({"PATH": "/usr/local/bin:/usr/bin:/bin", "TMPDIR": str(tmp)})
    if locale:
        env["LC_ALL"] = locale
    p = subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, f"rc={p.returncode}\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}"
    out = dict(ln.split("=", 1) for ln in p.stdout.splitlines() if ln.startswith(("RESULT=", "STATUS=", "RC=")))
    return {"steps": json.loads(out["RESULT"]), "status": out["STATUS"], "rc": out.get("RC"), "log": p.stdout}


def _no_output_case(funcs: str, tmp: Path, *, rc: int, prepare: str, mark: bool = True, locale: str = "") -> dict:
    """prepare 在建标记**之后**执行（写产出 = 本步写的）；`OLD` 前缀的文件在标记**之前**写（上一轮留下的）。"""
    body = f"""{funcs}
OUT="$DATA_DIR/out.json"
OLD="$DATA_DIR/old.json"
: > "$OLD"; touch -t 200001010000 "$OLD"
{'M="$(_step_marker)"' if mark else 'M=""'}
{prepare}
if _no_output stepX_test {rc} 7 "$M" "$@"; then printf 'RC=0\\n'; else printf 'RC=1\\n'; fi
"""
    return _bash(body, tmp=tmp, locale=locale)


@pytest.fixture(scope="module")
def orch() -> str:
    return repo_orchestrator_text()


@pytest.mark.parametrize("locale", ["", "en_US.UTF-8"])
class TestNoOutputFunction:
    """抽真函数跑。两种 locale：launchd 是 C，手工 / Python 拉起是 UTF-8（`$VAR` 紧跟全角字符在 UTF-8 + set -u 下会炸）。"""

    def _run(self, orch, tmp_path, locale, prepare, *, rc=0, mark=True, args='"$OUT"'):
        funcs = _funcs(orch)
        return _no_output_case(funcs, tmp_path, rc=rc, mark=mark, locale=locale,
                               prepare=prepare + f'\nset -- {args}')

    def test_fresh_output_passes_untouched(self, orch, tmp_path, locale):
        r = self._run(orch, tmp_path, locale, ': > "$OUT"')
        assert r["rc"] == "1" and r["steps"] == {}, r

    def test_missing_output_is_failed_no_output(self, orch, tmp_path, locale):
        r = self._run(orch, tmp_path, locale, ":")
        frag = r["steps"]["stepX_test"]
        assert r["rc"] == "0", r
        assert frag == {"status": "failed", "reason": "no_output", "missing": ["out.json"], "duration_seconds": 7}, r
        assert "LOG[ERROR]" in r["log"] and "out.json" in r["log"]

    def test_file_left_by_an_earlier_run_is_not_this_steps_output(self, orch, tmp_path, locale):
        """Step 8 的变体：文件在，但是上一轮写的——退出 0 照样没干活。"""
        r = self._run(orch, tmp_path, locale, ":", args='"$OLD"')
        assert r["rc"] == "0", r
        assert r["steps"]["stepX_test"]["missing"] == ["old.json（旧文件，不是本步写的）"], r

    def test_every_declared_output_is_checked(self, orch, tmp_path, locale):
        r = self._run(orch, tmp_path, locale, ': > "$OUT"', args='"$OUT" "$DATA_DIR/b.html" "$DATA_DIR/c.html"')
        assert r["steps"]["stepX_test"]["missing"] == ["b.html", "c.html"], r

    def test_nonzero_rc_is_not_its_business(self, orch, tmp_path, locale):
        r = self._run(orch, tmp_path, locale, ":", rc=1)
        assert r["rc"] == "1" and r["steps"] == {}, r

    def test_without_a_marker_it_still_checks_existence(self, orch, tmp_path, locale):
        r = self._run(orch, tmp_path, locale, ":", mark=False)
        assert r["rc"] == "0" and r["steps"]["stepX_test"]["reason"] == "no_output", r
        r = self._run(orch, tmp_path, locale, ':', mark=False, args='"$OLD"')
        assert r["rc"] == "1" and "未核是不是本步写的" in r["log"], r

    def test_marker_is_cleaned_up(self, orch, tmp_path, locale):
        self._run(orch, tmp_path, locale, ': > "$OUT"')
        self._run(orch, tmp_path, locale, ":")
        assert not list(tmp_path.glob("alpha_hive_stepmark.*")), list(tmp_path.iterdir())


def _stub_run_step(write: str) -> str:
    """假 run_step：退出码取 $STUB_RC（缺省 0），并按 write 写产出（空串 = 什么都不写）。"""
    return f'run_step() {{ {write or ":"}; return ${{STUB_RC:-0}}; }}\n'


class TestRealStepBlocks:
    """Step 3 / 6 整块从仓库编排器抽出来真跑：接没接上、整体状态口径对不对，只有真跑看得出。"""

    def _step3(self, orch, tmp, write, rc=0):
        block = _block(orch, "# Step 3: ML 增强报告生成", "# Step 4/5:")
        pre = 'TICKERS=(AAA BBB)\nDATE_STR="$(date +%Y-%m-%d)"\nALPHA_HIVE_ML_REPORT_MAX=12\n'
        return _bash(_funcs(orch) + _stub_run_step(write) + f"STUB_RC={rc}\n" + pre + block, tmp=tmp)

    def test_step3_exit0_with_a_ticker_still_missing_is_partial(self, orch, tmp_path):
        r = self._step3(orch, tmp_path, ': > "$DATA_DIR/alpha-hive-AAA-ml-enhanced-${DATE_STR}.html"')
        s3 = r["steps"]["step3_ml_report"]
        today = date.today().isoformat()
        assert s3["status"] == "failed" and s3["missing"] == [f"alpha-hive-BBB-ml-enhanced-{today}.html"], r
        assert r["status"] == "partial", "Step 3 的 rc≠0 支整体 partial，no_output 同口径"

    def test_step3_all_written_is_success(self, orch, tmp_path):
        w = 'for t in AAA BBB; do : > "$DATA_DIR/alpha-hive-${t}-ml-enhanced-${DATE_STR}.html"; done'
        r = self._step3(orch, tmp_path, w)
        assert r["steps"]["step3_ml_report"]["status"] == "success" and r["status"] == "success", r

    def _step6(self, orch, tmp, write, rc=0):
        block = _block(orch, "# Step 6: 智能告警分析", "# Step 7: 推送")
        pre = 'REPORTDIR="$DATA_DIR"\nDATE_STR="$(date +%Y-%m-%d)"\n'
        return _bash(_funcs(orch) + _stub_run_step(write) + f"STUB_RC={rc}\n" + pre + block, tmp=tmp)

    def test_step6_exit0_without_alerts_file_is_red(self, orch, tmp_path):
        r = self._step6(orch, tmp_path, "")
        s6 = r["steps"]["step6_alert_analysis"]
        assert s6["status"] == "failed" and s6["reason"] == "no_output", r
        assert r["status"] == "success", "Step 6 的 rc≠0 支不升级整体状态"

    def test_step6_alerts_file_written_is_success(self, orch, tmp_path):
        r = self._step6(orch, tmp_path, ': > "$LOGDIR/alerts-${DATE_STR}.json"')
        assert r["steps"]["step6_alert_analysis"]["status"] == "success", r

    def test_step6_rollover_after_an_earlier_same_day_run_is_success(self, orch, tmp_path):
        """v0.45.549：DATE_STR 名下是上一轮留下的旧文件，本轮跨零点写的是次日名 ⇒ 认次日名，不误判 no_output。
        （543 版只在 DATE_STR 文件「不存在」时才看次日名 ⇒ 这里会红。）"""
        block = _block(orch, "# Step 6: 智能告警分析", "# Step 7: 推送")
        pre = ('REPORTDIR="$DATA_DIR"\nDATE_STR="2000-01-01"\n'
               ': > "$LOGDIR/alerts-2000-01-01.json"; touch -t 200001010000 "$LOGDIR/alerts-2000-01-01.json"\n')
        write = ': > "$LOGDIR/alerts-$(date +%Y-%m-%d).json"'
        r = _bash(_funcs(orch) + _stub_run_step(write) + "STUB_RC=0\n" + pre + block, tmp=tmp_path)
        assert r["steps"]["step6_alert_analysis"]["status"] == "success", r

    def test_step6_stale_files_under_both_names_is_red(self, orch, tmp_path):
        block = _block(orch, "# Step 6: 智能告警分析", "# Step 7: 推送")
        pre = ('REPORTDIR="$DATA_DIR"\nDATE_STR="2000-01-01"\n'
               'for f in "$LOGDIR/alerts-2000-01-01.json" "$LOGDIR/alerts-$(date +%Y-%m-%d).json"; do '
               ': > "$f"; touch -t 200001010000 "$f"; done\n')
        r = _bash(_funcs(orch) + _stub_run_step("") + "STUB_RC=0\n" + pre + block, tmp=tmp_path)
        assert r["steps"]["step6_alert_analysis"]["reason"] == "no_output", r


class TestEveryRunStepDeclaresItsEvidence:

    def test_registry_matches_orchestrator(self, orch):
        problems = classify_problems(orch, STEP_OUTPUTS)
        assert not problems, "\n".join(problems)

    def test_every_entry_is_well_formed(self):
        bad = {k: v for k, v in STEP_OUTPUTS.items()
               if not (isinstance(v, tuple) and len(v) == 2 and v[0] in KINDS and str(v[1]).strip())}
        assert not bad, bad

    def test_scanner_sees_every_real_call(self, orch):
        """正对照：扫描器认得出真编排器里的调用形状（带 / 不带 --timeout、${PROJECT_DIR} / $PROJECT_DIR）。"""
        names = {s for s, _ in run_step_calls(orch)}
        assert {"generate_ml_report.py", "alert_manager.py", "orchestrator_steps.py", "ic_rerun_readiness.py"} <= names, names
        raw = len(re.findall(r"^[ \t]*run_step[ \t]", orch, re.M))
        assert raw == len(run_step_calls(orch)), "有 run_step 调用的写法扫描器没认出来——扩 _RUN_STEP，别让它漏登记"


class TestGuardsHaveTeeth:
    """反向自证：上面全绿不能是因为判据是瞎的。每条变异都必须改变同一用例的结果。"""

    def test_new_unregistered_step_goes_red(self, orch):
        mutated = orch + '\nrun_step --timeout 60 "$PROJECT_DIR/new_tool.py" >> "$LOGFILE" 2>&1\n'
        assert any("new_tool.py" in p for p in classify_problems(mutated, STEP_OUTPUTS))

    def test_dropping_the_postcondition_call_goes_red(self, orch):
        mutated = orch.replace("_no_output step3_ml_report ", "true ", 1)
        assert any("step3_ml_report" in p for p in classify_problems(mutated, STEP_OUTPUTS))
        mutated = orch.replace('_STEP6_MARK="$(_step_marker)"', '_STEP6_MARK=""', 1)
        assert any("_step_marker" in p for p in classify_problems(mutated, STEP_OUTPUTS))

    def test_stale_registry_entry_goes_red(self, orch):
        assert any("ghost.py" in p for p in classify_problems(orch, {**STEP_OUTPUTS, "ghost.py": (EXEMPT, "x")}))

    def test_interpreter_claim_needs_the_call(self, orch):
        reg = {**STEP_OUTPUTS, "alert_manager.py": (INTERPRETER, "6")}
        assert any("alert_manager.py" in p for p in classify_problems(orch, reg))

    @pytest.mark.parametrize("mutation, case", [
        (("-ot", "-nt"), "old"),                                      # 新鲜度判据反了
        (('[ -z "${_PC_MISSING}" ]\n}', "true\n}"), "missing"),        # 永远说「有产出」
        (("'. + {($k): {\"status\": \"failed\", \"reason\": \"no_output\", \"missing\": $m, \"duration_seconds\": $d}}')\n    return 0\n}",
          "'. + {($k): {\"status\": \"failed\", \"reason\": \"no_output\", \"missing\": $m, \"duration_seconds\": $d}}')\n    return 1\n}"), "missing"),  # 记了片段却告诉调用方「不归我管」
        (('STEPS_RESULT=$(echo "$STEPS_RESULT" | jq --arg k', '_DROPPED=$(echo "$STEPS_RESULT" | jq --arg k'), "missing"),  # 不写片段
    ])
    def test_function_mutations_change_the_outcome(self, orch, tmp_path, mutation, case):
        funcs = _funcs(orch)
        old, new = mutation
        assert old in funcs, f"变异锚点没找到（函数改过了？）：{old!r}"
        args = '"$OLD"' if case == "old" else '"$OUT"'
        good = _no_output_case(funcs, tmp_path / "g", rc=0, prepare=f"set -- {args}")
        bad = _no_output_case(funcs.replace(old, new, 1), tmp_path / "b", rc=0, prepare=f"set -- {args}")
        assert good["rc"] == "0", good
        assert (bad["rc"], bad["steps"]) != (good["rc"], good["steps"]), f"变异 {mutation} 没改变结果——用例没牙"


class TestAlertManagerAlwaysWritesItsFile:
    """Step 6 的后置条件依赖这一点：无告警也写 alerts-<日期>.json（v0.45.543 前不写）。"""

    def test_no_alerts_still_writes_file(self, tmp_path, monkeypatch):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import alert_manager

        monkeypatch.setattr(alert_manager.AlertAnalyzer, "analyze", lambda self, p: [])
        monkeypatch.setattr(sys, "argv", ["alert_manager.py", "--status-json", str(tmp_path / "status.json"),
                                          "--output-dir", str(tmp_path)])
        alert_manager.main()
        files = list(tmp_path.glob("alerts-*.json"))
        assert len(files) == 1, list(tmp_path.iterdir())
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["total_alerts"] == 0 and data["alerts"] == [] and data["checks_skipped"] == []
