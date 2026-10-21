"""墓碑（v0.45.571）：编排器 Step 7（`push_report_to_slack.py`）连同脚本、富文本日报格式化一起删了。

Step 7 每天 `run_step push_report_to_slack.py`（不带 `--force`），脚本在推送前直接 `sys.exit(2)`，编排器记
`step7_push_report: skipped_manual`。10-01~10-20 生产日志每天都是「Step 7 跳过」，结构化日志里它从没走到推送路径；
`step7_push_report` 键零读者（Step 6 alert_manager 在它之前就跑完，网站 / MCP / 其他 Python 都不读）；没有任何
定时任务 / launchd / skill 传 `--force`。v0.45.543 的后置条件守卫把它登记为豁免（退出码 0 不可达）——不冒充成功，
但每天跑一次只为退出 2，是一条死步骤。用户 10-21 选「连脚本一起删」：`slack_report_notifier` 里只服务它的
`send_rich_daily_report` / `_format_rich_daily_mrkdwn` / `_load_json` 一并删，Slack 发送白名单变空。

兼容一轮：合入后第一个扫描日仍跑旧编排器，它照旧 `run_step push_report_to_slack.py`——脚本不在时 `run_step`
返回 2（「脚本不存在，跳过」），落到同一个 `skipped_manual` 分支，与删前同值（`TestOldOrchestratorStillFine`）。
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from tests import _ast_cache
from tests._orchestrator import extract_function, repo_orchestrator_text
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
_RETIRED_MEMBERS = {"send_rich_daily_report", "_format_rich_daily_mrkdwn", "_load_json"}


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _docstring_ids(tree) -> set[int]:
    return {id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}


def _uses_push_report(tree) -> list[int]:
    """导入它，或在非 docstring 的字符串里点名它（subprocess / run_step 式调用）。说到它不算用它。"""
    skip = _docstring_ids(tree)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name == "push_report_to_slack" for a in n.names):
            out.append(n.lineno)
        elif isinstance(n, ast.ImportFrom) and n.module == "push_report_to_slack":
            out.append(n.lineno)
        elif (isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
              and "push_report_to_slack" in n.value):
            out.append(n.lineno)
    return out


class TestStep7StaysRetired:

    def test_script_is_gone(self):
        assert not (REPO_ROOT / "push_report_to_slack.py").exists()

    def test_orchestrator_has_no_step7(self):
        orch = repo_orchestrator_text()
        run_lines = [ln for ln in orch.splitlines() if ln.strip().startswith("run_step")]
        assert not any("push_report_to_slack" in ln for ln in run_lines)
        code = "\n".join(_code_lines(orch))
        assert "step7_push_report" not in code, "step7 结局键又写回 status.json 了"
        assert not re.search(r"STEP7_", code)
        assert any("alert_manager.py" in ln for ln in run_lines)   # 正对照：同一解析认得出仍在跑的步骤

    def test_formatter_members_are_gone(self):
        tree = _ast_cache.parse_file(REPO_ROOT / "slack_report_notifier.py")
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SlackReportNotifier")
        members = {f.name for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert "send_plain_text" in members                      # 正对照：解析的确实是那个类
        assert not (_RETIRED_MEMBERS & members), sorted(_RETIRED_MEMBERS & members)

    def test_nobody_calls_the_script(self):
        files, _how = own_python_files(REPO_ROOT)
        hits = []
        for f in files:
            rel = f.relative_to(REPO_ROOT).as_posix()
            if rel.startswith("tests/"):
                continue
            try:
                tree = _ast_cache.parse_file(f, errors="replace")
            except SyntaxError:
                continue
            hits += [f"{rel}:{ln}" for ln in _uses_push_report(tree)]
        assert not hits, hits


class TestOldOrchestratorStillFine:
    """部署那一轮跑的是旧编排器：它的 `run_step` 对缺失脚本返回 2 ⇒ 落到原 `skipped_manual` 分支。"""

    def test_run_step_on_missing_script_returns_2(self, tmp_path):
        fn = extract_function(repo_orchestrator_text(), "run_step")
        script = f'log() {{ :; }}\n{fn}\nrun_step --timeout 300 "{tmp_path}/push_report_to_slack.py"\necho "rc=$?"\n'
        out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert out.stdout.strip().endswith("rc=2"), (out.stdout, out.stderr)


class TestDetectorHasTeeth:

    def test_detector(self):
        assert _uses_push_report(ast.parse("import push_report_to_slack\n"))
        assert _uses_push_report(ast.parse("from push_report_to_slack import main\n"))
        assert _uses_push_report(ast.parse('subprocess.run(["python3", "push_report_to_slack.py", "--force"])\n'))
        assert not _uses_push_report(ast.parse('def f():\n    """旧版调 push_report_to_slack.py --force。"""\n'))
        assert not _uses_push_report(ast.parse('import alert_manager\n'))
