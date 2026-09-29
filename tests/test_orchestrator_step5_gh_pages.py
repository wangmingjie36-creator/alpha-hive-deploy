"""编排器 Step 5 按 gh-pages 部署的**实际结局**判红（v0.45.351）。

v0.45.351 把 Step 5 的判定写成编排器里的函数 `_step5_gh_pages_verdict`，判据本身在
`report_deployer.gh_pages_step_status`（见 `test_gh_pages_unverified_parent.py`）。

本文件把那个函数**从仓库里的编排器（`tests/_orchestrator.py::REPO_ORCH`，v0.45.353 起
受版本控制的唯一真相）抽出来**，在 bash 里配真 `report_deployer.py` 跑：
它接没接上 helper、映射得对不对、helper 不可用时退回得对不对——只有真跑才看得出来
（这类「编排器读的字段/键名与 Python 侧对不上」本仓已经栽过：v0.45.255 的
`step2_swarm_analysis` 夹具键名）。

为什么不在 scan_timing 那条路上判：`write_status()` 把 `scan_timing.json` 并进
status.json 的那一步，09-14~09-25 每个扫描日都没生效（alert_manager 日志「status.json
无 scan_timing」），网站是否更新不能只押在它上面。

读仓库副本而不是 `~/.claude/scripts/` 下的部署副本：后者只在一台 Mac 上存在，按它
skip 的测试在 CI 上恒 skip（v0.45.334 的 bug 就是这样藏了一天，见 `tests/_orchestrator.py`）。
部署副本与仓库是否一致由 `test_orchestrator_deployed_matches_repo.py` 单独管。
需要 `/bin/bash` 与 `jq`（编排器本身就依赖二者；CI 的 ubuntu-latest 自带）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._orchestrator import extract_function, repo_orchestrator_text

_ROOT = Path(__file__).resolve().parent.parent
_FUNC = "_step5_gh_pages_verdict"


def _run(func_src: str, *, project_dir: Path, logs_dir: Path, step2_rc: int,
         since: float, locale: str) -> dict:
    harness = f"""set -uo pipefail
log() {{ printf 'LOG[%s] %s\\n' "$1" "$2"; }}
OVERALL_STATUS=success
set_status() {{ OVERALL_STATUS="$1"; }}
PROJECT_DIR={json.dumps(str(project_dir))}
PYTHON3={json.dumps(sys.executable)}
LOGFILE=/dev/null
STEP2_START={since}
STEP2_RC={step2_rc}
STEPS_RESULT='{{"step2_hive_analysis": {{"status": "success"}}}}'
{func_src}
{_FUNC}
printf 'RESULT=%s\\n' "$(echo "$STEPS_RESULT" | jq -c .)"
printf 'STATUS=%s\\n' "$OVERALL_STATUS"
"""
    env = {k: v for k, v in os.environ.items() if k not in ("LC_ALL", "LANG", "LC_CTYPE")}
    env.update({"ALPHA_HIVE_HOME": str(logs_dir.parent), "ALPHA_HIVE_LOGS_DIR": str(logs_dir),
                "PATH": "/usr/local/bin:/usr/bin:/bin"})
    if locale:
        env["LC_ALL"] = locale
    p = subprocess.run(["/bin/bash", "-c", harness], capture_output=True, text=True,
                       env=env, timeout=120)
    assert p.returncode == 0, f"rc={p.returncode}\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}"
    out = dict(ln.split("=", 1) for ln in p.stdout.splitlines() if ln.startswith(("RESULT=", "STATUS=")))
    return {"steps": json.loads(out["RESULT"]), "status": out["STATUS"], "log": p.stdout}


def _write_log(logs_dir: Path, *records):
    logs_dir.mkdir(parents=True, exist_ok=True)
    with open(logs_dir / ".gh_pages_deploy_log.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.mark.parametrize("locale", ["", "en_US.UTF-8"])
class TestRealOrchestratorStep5:
    """抽真函数、配真 helper 跑。两种 locale 都跑：launchd 是 C locale，手工 / Python 拉起是
    UTF-8（`$VAR` 紧跟全角字符在 UTF-8 下会被 set -u 判未定义，见 test_orchestrator_braced_vars）。"""

    @pytest.fixture
    def func(self):
        return extract_function(repo_orchestrator_text(), _FUNC)

    def test_failed_deploy_goes_red_even_when_step2_rc_is_0(self, func, tmp_path, locale):
        """本版要治的形状：Step 2 退出 0、推送却失败 ⇒ 旧逻辑恒 skipped_builtin（零告警）。"""
        logs = tmp_path / "home" / "logs"
        _write_log(logs, {"timestamp": "2026-09-25T21:46:43.738300Z", "status": "failed",
                          "last_error": "ssh: connect to host github.com port 22", "attempts": 4})
        r = _run(func, project_dir=_ROOT, logs_dir=logs, step2_rc=0, since=0, locale=locale)
        s5 = r["steps"]["step5_github_deploy"]
        assert s5["status"] == "failed" and s5["reason"] == "gh_pages_deploy_failed", r
        assert r["status"] == "partial", "网站没更新，整轮不该是 success"
        assert "本轮网站没有更新" in r["log"] and "port 22" in r["log"]

    def test_success_is_confirmed_from_the_log(self, func, tmp_path, locale):
        logs = tmp_path / "home" / "logs"
        _write_log(logs, {"timestamp": "2026-09-28T21:46:43Z", "status": "success",
                          "action": "pushed_new_commit", "attempts": 1})
        r = _run(func, project_dir=_ROOT, logs_dir=logs, step2_rc=0, since=0, locale=locale)
        assert r["steps"]["step5_github_deploy"]["status"] == "success", r
        assert r["status"] == "success"

    def test_rc1_after_a_real_deploy_is_not_reported_as_site_not_updated(self, func, tmp_path, locale):
        """09-24：RC=1 是 ML 常数闸，部署其实成功——旧逻辑报「本轮网站不会更新」是误报。"""
        logs = tmp_path / "home" / "logs"
        _write_log(logs, {"timestamp": "2026-09-24T21:53:23Z", "status": "success", "attempts": 1})
        r = _run(func, project_dir=_ROOT, logs_dir=logs, step2_rc=1, since=0, locale=locale)
        assert r["steps"]["step5_github_deploy"]["status"] == "success", r
        assert "不会更新" not in r["log"]

    def test_no_record_this_round_is_red(self, func, tmp_path, locale):
        """Step 2 之后没有任何部署记录（部署没跑到 / 抛了）⇒ 红，不是「跳过」。"""
        logs = tmp_path / "home" / "logs"
        _write_log(logs, {"timestamp": "2026-09-24T21:53:23Z", "status": "success"})
        r = _run(func, project_dir=_ROOT, logs_dir=logs, step2_rc=0, since=4_000_000_000,
                 locale=locale)
        s5 = r["steps"]["step5_github_deploy"]
        assert s5["status"] == "failed" and s5["reason"] == "no_deploy_record", r

    @pytest.mark.parametrize("rc,expect", [(0, "skipped_builtin"), (1, "failed")])
    def test_helper_unavailable_falls_back_to_rc_logic(self, func, tmp_path, locale, rc, expect):
        """生产代码早于 v0.45.351（没有 helper）⇒ 退回旧逻辑，且 RC=0 那支标 unverified 不装成功。"""
        old_checkout = tmp_path / "old_checkout"
        old_checkout.mkdir()
        logs = tmp_path / "home" / "logs"
        _write_log(logs)
        r = _run(func, project_dir=old_checkout, logs_dir=logs, step2_rc=rc, since=0, locale=locale)
        s5 = r["steps"]["step5_github_deploy"]
        assert s5["status"] == expect, r
        if rc == 0:
            assert s5.get("unverified") is True and "未核实" in r["log"]
        else:
            assert s5["reason"] == "step2_did_not_complete"


class TestExtractorHasTeeth:
    """抽取器自证（纯合成文本，任何机器都跑）：抽不到时要抛，不能返回空串让上面的类「测了个寂寞」。"""

    def test_extracts_only_the_named_function(self):
        text = "a() {\n    x\n}\n_step5_gh_pages_verdict() {\n    if true; then\n        y\n    fi\n}\nb\n"
        got = extract_function(text, _FUNC)
        assert got.startswith(f"{_FUNC}() {{") and got.rstrip().endswith("}") and "b\n" not in got

    def test_missing_function_raises(self):
        with pytest.raises(LookupError):
            extract_function("nothing here\n", _FUNC)
