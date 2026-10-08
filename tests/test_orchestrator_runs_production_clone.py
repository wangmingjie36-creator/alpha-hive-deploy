"""编排器必须跑生产独立克隆（数据根迁移阶段 8，v0.45.431；v0.45.432 二次检查补闸前失败落盘）

守卫本体在 `tests/test_production_clone.py`；本文件只钉编排器（唯一真相 = 仓库 scripts/ 那份）的接线，
与编排器的切换同一提交进出——切换未合入时，守卫本体照样能单独合入。
闸前失败的真跑（沙箱里删掉 / 掏空代码目录）在 `tests/test_scan_catchup.py::TestGateBranchesLive`（integration）。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import production_clone as pc  # noqa: E402
from tests._orchestrator import extract_function, orch_literal, repo_orchestrator_text  # noqa: E402

#: 闸前三个「代码目录出问题」分支 → 各自必须落盘的 status 值
_EARLY_EXITS = {
    'if [ ! -d "$PROJECT_DIR" ]': "failed_project_dir_missing",
    'if [ ! -f "$PROJECT_DIR/alpha_hive_daily_report.py" ]': "failed_core_script_missing",
    'if ! cd "$PROJECT_DIR"': "failed_project_dir_unreadable",
}


@pytest.fixture(scope="module")
def orch():
    return repo_orchestrator_text()


@pytest.fixture(scope="module")
def code(orch):
    return "\n".join(ln for ln in orch.splitlines() if not ln.lstrip().startswith("#"))


class TestOrchestratorPointsAtTheClone:
    """编排器必须跑生产克隆、并声明它。两者是同一版的两半：
    只改 PROJECT_DIR 不声明 ⇒ 守卫永远不装、没人红；只声明不改 ⇒ 开发仓库天天红（不装钩子）。"""

    def test_project_dir_is_the_standalone_clone(self, orch):
        pdir = orch_literal(orch, "PROJECT_DIR")
        assert pdir, "PROJECT_DIR 不是唯一一处纯字面赋值"
        assert "/Desktop/" not in pdir, f"生产代码又指回了开发检出：{pdir}"
        assert Path(pdir).name == pc.default_dest().name, (pdir, pc.default_dest())

    def test_sync_call_declares_the_clone_for_that_process_only(self, code):
        lines = code.splitlines()
        calls = [ln for ln in lines if 'production_sync.py" --date' in ln]
        assert len(calls) == 1, calls
        assert f"{pc.PRODUCTION_CLONE_ENV}=1 " in calls[0], calls[0]
        assert not any(f"export {pc.PRODUCTION_CLONE_ENV}" in ln for ln in lines), "只给 production_sync 设，别 export 给所有步骤"

    def test_steps_run_with_cwd_inside_the_clone(self, code):
        """run_step 不 cd、launchd 的 WorkingDirectory 仍是开发检出 ⇒ 编排器自己必须在任何 Python 步骤之前切进 PROJECT_DIR。"""
        # 精确找那条顶层 cd：第 93 行附近 `_WL_RAW="$(cd "$PROJECT_DIR" && …)"` 是子 shell 里的，不改变脚本 cwd
        cd = code.find('if ! cd "$PROJECT_DIR"')
        assert cd != -1, "编排器没有切进生产克隆"
        assert code.index('if [ ! -d "$PROJECT_DIR" ]') < cd < code.index('production_sync.py" --date')
        assert cd < code.index("STEP1_START")


class TestEarlyExitsAreWrittenToStatus:
    """v0.45.432：代码目录出问题时编排器在闸前 exit 1。alert_manager 就在那个目录里、跑不起来，
    status.json 不写就停在上一次成功 ⇒ 整天静默。每个这样的分支都必须在 exit 之前落盘。"""

    @pytest.mark.parametrize("cond,status", sorted(_EARLY_EXITS.items()))
    def test_branch_writes_its_status_before_exiting(self, code, cond, status):
        import re
        start = code.index(cond)
        body = code[start:code.index("\nfi", start)]
        # 行首锚定（缩进之后紧跟调用）：只查子串时 `: _early_fail_status …`（空命令）这种失效写法照样过（变异实测）
        call = re.search(rf'^[ \t]*_early_fail_status "{status}" ', body, flags=re.M)
        assert call, body
        assert call.start() < body.index("exit 1"), body

    def test_helper_writes_valid_json(self, orch, tmp_path):
        """heredoc 拼 JSON：换了文案带进引号 / 反斜杠就会写出坏文件——真跑一次 /bin/bash 读回来。"""
        fn = extract_function(orch, "_early_fail_status")
        rep, log = tmp_path / "reports", tmp_path / "orch.log"
        script = (f'REPORTDIR="{rep}"\nLOGFILE="{log}"\n{fn}\n'
                  '_early_fail_status "failed_project_dir_missing" "PROJECT_DIR /Users/x/alpha-hive-prod does not exist"\n')
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=20)
        assert r.returncode == 0, r.stderr
        got = json.loads((rep / "status.json").read_text(encoding="utf-8"))
        assert got["status"] == "failed_project_dir_missing"
        assert got["error"].endswith("does not exist") and got["logfile"] == str(log)
        assert got["last_run"].endswith("Z")

    def test_every_early_message_is_json_safe(self, code):
        """传给 heredoc 的第 2 个参数只许是本脚本写死的文案：不能含双引号 / 反斜杠 / 命令替换。"""
        import re
        calls = re.findall(r'_early_fail_status "([^"]+)" "([^"]*)"', code)
        assert len(calls) >= len(_EARLY_EXITS), calls
        for status, msg in calls:
            assert "\\" not in msg and "$(" not in msg and "`" not in msg, (status, msg)
