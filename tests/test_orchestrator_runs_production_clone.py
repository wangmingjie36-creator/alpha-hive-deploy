"""编排器必须跑生产独立克隆（数据根迁移阶段 8，v0.45.431）

守卫本体在 `tests/test_production_clone.py`；本文件只钉编排器（唯一真相 = 仓库 scripts/ 那份）的三处接线，
与编排器的切换同一提交进出——切换未合入时，守卫本体照样能单独合入。
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import production_clone as pc  # noqa: E402


class TestOrchestratorPointsAtTheClone:
    """编排器（唯一真相 = 仓库 scripts/ 那份）必须跑生产克隆、并声明它。两者是同一版的两半：
    只改 PROJECT_DIR 不声明 ⇒ 守卫永远不装、没人红；只声明不改 ⇒ 开发仓库天天红（不装钩子）。"""

    @pytest.fixture(scope="class")
    def orch(self):
        from tests._orchestrator import repo_orchestrator_text
        return repo_orchestrator_text()

    def test_project_dir_is_the_standalone_clone(self, orch):
        from tests.test_scan_catchup import _orch_literal
        pdir = _orch_literal(orch, "PROJECT_DIR")
        assert pdir, "PROJECT_DIR 不是唯一一处纯字面赋值"
        assert "/Desktop/" not in pdir, f"生产代码又指回了开发检出：{pdir}"
        assert Path(pdir).name == pc.default_dest().name, (pdir, pc.default_dest())

    def test_sync_call_declares_the_clone_for_that_process_only(self, orch):
        code = [ln for ln in orch.splitlines() if not ln.lstrip().startswith("#")]
        calls = [ln for ln in code if 'production_sync.py" --date' in ln]
        assert len(calls) == 1, calls
        assert f"{pc.PRODUCTION_CLONE_ENV}=1 " in calls[0], calls[0]
        assert not any(f"export {pc.PRODUCTION_CLONE_ENV}" in ln for ln in code), "只给 production_sync 设，别 export 给所有步骤"

    def test_steps_run_with_cwd_inside_the_clone(self, orch):
        """run_step 不 cd、launchd 的 WorkingDirectory 仍是开发检出 ⇒ 编排器自己必须在任何 Python 步骤之前切进 PROJECT_DIR。"""
        code = "\n".join(ln for ln in orch.splitlines() if not ln.lstrip().startswith("#"))
        cd = code.find('cd "$PROJECT_DIR" ||')
        assert cd != -1, "编排器没有切进生产克隆"
        assert code.index('if [ ! -d "$PROJECT_DIR" ]') < cd < code.index('production_sync.py" --date')
        assert cd < code.index("STEP1_START")
