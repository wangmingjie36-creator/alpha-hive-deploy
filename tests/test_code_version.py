"""「在跑的是哪一版代码」观测点（v0.45.182）。

治的形状：2026-09-10 事故（CHANGELOG v0.45.181）——一个修复推到了 `main`，
生产 checkout 不自动 pull、落后六个提交，当天扫描照旧跑旧代码，把刚回灌的
125 条样本又删了一遍。能查出来纯属数据消失得够显眼；换成一个不改变行数的
行为差异就无从发现，因为「生产在跑的是哪一版」在日志与 `status.json` 里都没有记录。

本文件守两件事：
  A. `code_version.resolve()` 算得对，且**失败时不编值**；
  B. 它**真的被接上了**——扫描开始打日志、结果进 `scan_timing` 快照
     （编排器已把该文件 jq 进 `status.json`）。
     B 比 A 重要：一个没人调用的观测点等于没有（v0.45.180 那条
     「本次不可用」提示就是这么成为死代码的）。

每条断言都附了能让它变红的变异。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDERS = {"unknown", "UNKNOWN", "n/a", "N/A", "0000000", "none", "-", ""}


class TestResolvesRealValues:
    def test_sha_and_version_are_real(self):
        """在仓库里跑，sha / branch / changelog_version 必须是真值。

        变异：把 `_repo_dir()` 改成 `PATHS.home`（测试里被隔离到 tmp）⇒ 全 None ⇒ 红。
        """
        import code_version as cv

        i = cv.resolve()
        assert i["sha"] and re.fullmatch(r"[0-9a-f]{7,40}", i["sha"]), i
        assert i["branch"], i
        assert i["changelog_version"] and re.fullmatch(r"\d+(\.\d+)*", i["changelog_version"]), i
        assert Path(i["repo_dir"]) == _ROOT, (
            f"repo_dir={i['repo_dir']} 不是仓库根 —— 它要的是「代码在哪」，"
            "不是 PATHS.home（那会跟着 ALPHA_HIVE_HOME 跑到 tmp 去）")
        assert (Path(i["repo_dir"]) / ".git").exists(), "repo_dir 下没有 .git"

    def test_changelog_version_matches_file_top(self):
        """读出来的版本号必须等于 CHANGELOG.md 行首第一条 `## [x.y.z]`。

        变异：把正则的 `^` 去掉（不锚定行首）⇒ 可能命中正文里引用的版本号 ⇒ 红。
        """
        import code_version as cv

        want = None
        with open(_ROOT / "CHANGELOG.md", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^##\s*\[([0-9]+(?:\.[0-9]+)*)\]", line)
                if m:
                    want = m.group(1)
                    break
        assert want, "CHANGELOG.md 里没有行首版本号，本条断言的锚点失效了"
        assert cv.changelog_version() == want


class TestFailureNeverFabricates:
    """失败一律 None —— `"unknown"` / `"0000000"` 会被读成「有个版本，只是长这样」。"""

    def test_non_repo_yields_all_none(self, tmp_path, monkeypatch):
        """变异：任一字段的兜底改成 `"unknown"` 或 `""` ⇒ 红。"""
        import code_version as cv

        monkeypatch.setattr(cv, "_repo_dir", lambda: tmp_path)
        i = cv.resolve()
        for k in ("sha", "branch", "dirty_tracked", "changelog_version"):
            assert i[k] is None, f"{k}={i[k]!r} —— 取不到就该是 None"
            assert i[k] not in _PLACEHOLDERS or i[k] is None

    def test_summary_line_shows_dashes_not_placeholders(self, tmp_path, monkeypatch):
        import code_version as cv

        monkeypatch.setattr(cv, "_repo_dir", lambda: tmp_path)
        line = cv.summary_line()
        assert "—" in line
        for ghost in ("unknown", "0000000", "None", "null"):
            assert ghost not in line, f"摘要里出现了占位值 {ghost}"

    def test_not_a_repo_is_reported_as_such(self, tmp_path, monkeypatch, caplog):
        """git 退出码 **128 = 不是 git 仓库**，与普通失败要分开报。

        CLAUDE.md 就 `git check-ignore` 的三义退出码记过一次：
        把 128 揉进别的失败就是「把一种失败误报成另一种」。
        变异：删掉 `if r.returncode == 128:` 那一支 ⇒ 日志不再点明 ⇒ 红。
        """
        import code_version as cv

        monkeypatch.setattr(cv, "_repo_dir", lambda: tmp_path)
        with caplog.at_level("WARNING"):
            cv.resolve()
        assert any("不是 git 仓库" in r.getMessage() for r in caplog.records), (
            "非 git 目录没有被明确报成「不是 git 仓库」")


class TestCleanTreeIsNotConfusedWithFailure:
    """⚠️ 这条抓的是我自己差点发出去的 bug。

    `git status --porcelain` 在**干净工作区**输出为空。若 `_git` 写成
    `return r.stdout.strip() or None`，「成功但没输出」就与「命令失败」
    变成同一个 `None`，`dirty_tracked` 再也分不出「干净」和「没测到」。
    """

    def test_git_returns_empty_string_not_none_on_success(self, monkeypatch):
        """变异：`_git` 末行改回 `return r.stdout.strip() or None` ⇒ 红。"""
        import subprocess

        import code_version as cv

        class _R:
            returncode = 0
            stdout = "\n"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
        out = cv._git("status", "--porcelain")
        assert out == "", f"成功但空输出应返回空串，实得 {out!r} —— 与失败混为一谈了"

    def test_dirty_tracked_is_tri_state(self, monkeypatch):
        """None / False / True 三态必须都能出现。

        变异：把 `dirty_tracked` 写成 `bool(porcelain)` ⇒ 失败时变 False ⇒ 红。
        """
        import code_version as cv

        monkeypatch.setattr(cv, "_git", lambda *a: None)
        assert cv.resolve()["dirty_tracked"] is None
        monkeypatch.setattr(cv, "_git", lambda *a: "")
        assert cv.resolve()["dirty_tracked"] is False
        monkeypatch.setattr(cv, "_git", lambda *a: " M foo.py")
        assert cv.resolve()["dirty_tracked"] is True


class TestActuallyWired:
    """B 组：观测点必须**真的被调用**。没有这一组，A 组全绿也证明不了什么。"""

    def test_scan_startup_calls_it(self):
        """扫描启动处必须调用 `code_version.log_startup()`。

        变异：删掉 `alpha_hive_daily_report._init_scan_context` 里那次调用 ⇒ 红。
        """
        src = (_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_init_scan_context":
                target = node
                break
        assert target is not None, (
            "找不到 _init_scan_context —— 扫描启动点被改名了？"
            "改名就把本条的锚点一起改，别让它空转变绿。")
        called = [n for n in ast.walk(target)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "log_startup"]
        assert called, "扫描启动处没有调用 code_version.log_startup() —— 观测点没接上"

    def test_snapshot_carries_code_version(self):
        """`scan_timing.snapshot()` 必须带上真实版本 —— 这是进 status.json 的那条路。

        变异：从 snapshot 里删掉 `code_version` 键 ⇒ 红；
        改成返回 `{}` ⇒ 下面的 sha 断言 ⇒ 红。
        """
        import scan_timing as st

        snap = st.snapshot("2026-09-11")
        assert "code_version" in snap, "快照里没有 code_version —— status.json 也就不会有"
        cvv = snap["code_version"]
        assert cvv is not None and cvv.get("sha"), (
            f"code_version={cvv!r} —— 接上了但没解析出真值")
        assert cvv.get("changelog_version")

    @pytest.mark.integration  # 编排器在仓库外（~/.claude/scripts），干净检出与 CI 上不存在
    def test_orchestrator_merges_timing_into_status(self):
        """本模块挂在 `scan_timing` 上，前提是编排器确实把它并进 `status.json`。
        若那行 jq 被改掉，版本就进不了 status.json，而本文件其余断言照样全绿。

        ⚠️ 条件性写在 **marker** 上，不写进 `skip`：marker 在默认摘要里是
        `N deselected`（看得见），`skip` 平时和 PASSED 一样是一个点。
        本条第一版就写成了 `pytest.skip`，docstring 却写着「用 marker 不用 skip」
        ——原则写对了、做的相反，正是 CLAUDE.md 那节要治的形状。
        """
        orch = Path.home() / ".claude" / "scripts" / "alpha-hive-orchestrator.sh"
        assert orch.exists(), (
            f"编排器不在 {orch} —— 本条已标 @pytest.mark.integration，"
            "只在装了定时任务的机器上跑")
        text = orch.read_text(encoding="utf-8", errors="replace")
        assert "scan_timing.json" in text and "scan_timing:" in text, (
            "编排器不再把 scan_timing.json 并进 status.json —— "
            "code_version 也就进不了 status.json")
