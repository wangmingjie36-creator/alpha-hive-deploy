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

        ⚠️ **本条没有它原先声称的那颗牙**（2026-09-11 三档实测，v0.45.200 更正）。
        原 docstring 写「把正则的 `^` 去掉 ⇒ 命中正文里引用的版本号 ⇒ 红」，实跑是绿：
        `^` 对 `re.match()` **恒冗余**——`.match` 本就只从串首匹配，而
        `_CHANGELOG_RE` 全部走逐行 `.match`。护栏是 `^` 与 `.match` 的**合取**，
        二者互为冗余，去掉任一单独都不会红。

        且本条用的是**真** CHANGELOG，所以连「去 `^` 且换 `.search`」也照样绿：
        文件里确有非行首的 `## [x.y.z]` 散文引用，但都排在第一条真标题**之后**，
        而 `changelog_version()` 取首个匹配即 return ⇒ 位置巧合掩盖了变异。
        ⭐ 「X 会让它红」必须分清红的原因是**结构**还是**当前内容**：后者随文件漂，
        而 CHANGELOG 每天被十几个 session 追加。

        本条守的是「读出来的 == 文件顶部那条」这件事本身；牙在
        `TestHeadingMustBeLineAnchored` 的合成夹具里（把散文引用放到第一条真标题**之前**）。
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


class TestHeadingMustBeLineAnchored:
    """给上面那条补牙：**非行首**的 `## [x.y.z]` 不许被当成标题。

    真 CHANGELOG 验不了这件事——它的第一条行首标题排在所有散文引用之前，
    `changelog_version()` 取首个匹配即 return，于是变异被位置掩盖（见上）。
    本类用合成夹具把散文引用挪到第一条真标题**之前**，让护栏可观测。
    """

    #: 第 3 行在**非行首**位置写了 `## [9.9.9]`，且排在真标题 `## [1.2.3]` 之前。
    #: 缩进两格是有意的——顶格写会被 CHANGELOG 结构守卫
    #: （`tests/test_changelog_entry_integrity.py`，v0.45.195）算成真标题。
    _FIXTURE = (
        "# Alpha Hive · 版本变更历史\n"
        "\n"
        "> 举例说明：一条叫 `## [9.9.9]` 的标题——这是散文，不是标题。\n"
        "\n"
        "---\n"
        "\n"
        "## [1.2.3] — 2026-01-01 — 真标题\n"
        "\n"
        "正文\n"
    )

    def test_prose_mention_before_first_heading_is_not_taken(self, tmp_path, monkeypatch):
        """变异（**必须同时改两处**）：`_CHANGELOG_RE` 去掉 `^` **且**
        `changelog_version()` 里 `.match(line)` 换成 `.search(line)`
        ⇒ 命中第 3 行散文的 `9.9.9` ⇒ 红。

        ⭐ 单改其一恒绿，因为 `^` 与 `.match` 互为冗余——这正是原 docstring
        错在哪。合取型护栏无法用单点变异证伪，只能靠这样的夹具正面钉住行为。
        """
        import code_version as cv

        (tmp_path / "CHANGELOG.md").write_text(self._FIXTURE, encoding="utf-8")
        monkeypatch.setattr(cv, "_repo_dir", lambda: tmp_path)

        got = cv.changelog_version()
        assert got == "1.2.3", (
            f"取到 {got!r}——非行首的 `## [9.9.9]` 被当成了标题；"
            "护栏是「逐行 `.match`」这个遍历形状，不是 `^`")

    def test_fixture_really_contains_the_trap(self):
        """夹具自检：没有这条，上面那条可能是在一份**没有陷阱**的文件上绿的。

        （同 v0.45.195 `TestBothAssertionsAreNeeded` 的用意：证明样本真有牙。）
        变异：把 `_FIXTURE` 第 3 行的 `## [9.9.9]` 删掉 ⇒ 红。
        """
        import re

        from code_version import _CHANGELOG_RE

        lines = self._FIXTURE.split("\n")
        trap = [i for i, l in enumerate(lines, 1)
                if re.search(r"##\s*\[[0-9]", l) and not _CHANGELOG_RE.match(l)]
        heads = [i for i, l in enumerate(lines, 1) if _CHANGELOG_RE.match(l)]
        assert trap, "夹具里没有「非行首的 `## [x.y.z]`」，陷阱不存在"
        assert heads, "夹具里没有真标题"
        assert min(trap) < min(heads), (
            f"陷阱在第 {min(trap)} 行、真标题在第 {min(heads)} 行——"
            "陷阱必须排在真标题**之前**，否则 `changelog_version()` 先 return 就测不到")


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

        monkeypatch.setattr(cv, "_git", lambda *a, **k: None)
        assert cv.resolve()["dirty_tracked"] is None
        monkeypatch.setattr(cv, "_git", lambda *a, **k: "")
        assert cv.resolve()["dirty_tracked"] is False
        monkeypatch.setattr(cv, "_git", lambda *a, **k: " M foo.py")
        assert cv.resolve()["dirty_tracked"] is True


class TestDirtyWorktreeIsWarned:
    """v0.45.185：工作区脏必须打 **warning**，不是 info。

    `~/Desktop/Alpha Hive` 既是生产目录**又**被当成开发工作目录（2026-09-11
    实测一次有另一个 session 未提交的 10 个文件），而定时扫描跑的就是那份工作区
    ⇒ 一个写到一半的编辑会被定时任务捡去执行，此时 `sha` **不能唯一确定**
    实际跑了什么代码。这是 v0.45.181 事故的镜像（那次生产太旧，这次太新且未完成）。
    """

    _DIRTY = {"sha": "abc1234", "branch": "main", "dirty_tracked": True,
              "dirty_count": 10, "dirty_files": ["models.py", "signal_archive.py"],
              "changelog_version": "0.45.185"}
    _CLEAN = dict(_DIRTY, dirty_tracked=False, dirty_count=0, dirty_files=[])

    def test_dirty_logs_warning(self, caplog):
        """变异：把 `if i.get("dirty_tracked") is True:` 那支删掉（一律 info）⇒ 红。"""
        import code_version as cv

        with caplog.at_level("DEBUG"):
            cv.log_startup(dict(self._DIRTY))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warns, "工作区脏却没有 warning —— 这条提醒等于没有"
        msg = warns[0].getMessage()
        assert "工作区" in msg and "abc1234" in msg
        assert "models.py" in msg, "warning 里没列脏文件 —— 事后没法用"

    def test_clean_does_not_warn(self, caplog):
        """反向自证：干净时不许 warning，否则上一条在任何输入下都绿。

        变异：把判据改成 `is not None`（干净也警告）⇒ 红。
        """
        import code_version as cv

        with caplog.at_level("DEBUG"):
            cv.log_startup(dict(self._CLEAN))
        assert not [r for r in caplog.records if r.levelname == "WARNING"], (
            "工作区干净却打了 warning —— 警告会被当噪音忽略")

    def test_unmeasured_does_not_warn_as_dirty(self, caplog):
        """没测到（None）不是脏。变异：判据写成 `if i.get("dirty_tracked"):`
        对 None 仍为假、这条绿；但写成 `is not False` ⇒ 红。"""
        import code_version as cv

        unknown = {k: None for k in self._DIRTY}
        with caplog.at_level("DEBUG"):
            cv.log_startup(unknown)
        assert not [r for r in caplog.records
                    if r.levelname == "WARNING" and "未提交改动" in r.getMessage()]

    def test_dirty_files_is_tri_state(self, monkeypatch):
        """`dirty_files` / `dirty_count` 同样三态：None 没测到 / [] 干净 / 非空。

        变异：`dirty_files` 失败兜底写成 `[]` ⇒ 红（[] 读作「测了、干净」）。
        """
        import code_version as cv

        monkeypatch.setattr(cv, "_git", lambda *a, **k: None)
        i = cv.resolve()
        assert i["dirty_files"] is None and i["dirty_count"] is None
        monkeypatch.setattr(cv, "_git", lambda *a, **k: "")
        i = cv.resolve()
        assert i["dirty_files"] == [] and i["dirty_count"] == 0
        monkeypatch.setattr(cv, "_git", lambda *a, **k: " M a.py\nM  b.py\nR  old.py -> new.py")
        i = cv.resolve()
        assert i["dirty_count"] == 3
        assert i["dirty_files"] == ["a.py", "b.py", "new.py"], (
            f"解析错了：{i['dirty_files']} —— 重命名行应取目标路径")

    def test_dirty_files_capped(self, monkeypatch):
        """脏文件很多时只带样本，别把 status.json 撑爆；但 count 要给全量。

        变异：去掉 `[:_DIRTY_SAMPLE]` 切片 ⇒ 红。
        """
        import code_version as cv

        monkeypatch.setattr(cv, "_git",
                            lambda *a, **k: "\n".join(f" M f{n}.py" for n in range(50)))
        i = cv.resolve()
        assert i["dirty_count"] == 50, "count 应是全量"
        assert len(i["dirty_files"]) == cv._DIRTY_SAMPLE, "样本没有截断"


class TestGitToParseSeam:
    """⚠️ 这一组补的是 `_git` 与 `_parse_dirty` **之间的接缝**。

    v0.45.185 实测：两者各自的单测全绿，组合起来却把 `code_version.py` 解析成
    `ode_version.py`。原因是 `_git` 对整段 stdout 做了 `.strip()`，削掉了
    **第一行**状态列的前导空格（porcelain 的未暂存修改 X 位就是空格），
    于是首行只剩 2 字符前缀，`line[3:]` 吃掉了文件名首字母。

    为什么原有单测抓不到：`test_dirty_files_is_tri_state` 直接 monkeypatch
    `_git` 返回 `" M a.py"` —— **夹具喂的是已经正确成形的输入**，从没经过那道
    `.strip()`。所以这里从 `subprocess` 出口注入，让两段代码真的串起来跑。
    """

    _PORCELAIN = " M code_version.py\nM  staged.py\n M tests/test_x.py\nR  old.py -> new.py\n"

    def _fake_run(self, stdout):
        class _R:
            returncode = 0
            stderr = ""
        _R.stdout = stdout
        return lambda *a, **k: _R()

    def test_first_line_leading_space_survives(self, monkeypatch):
        """从 subprocess 出口喂真实 porcelain，文件名必须一个字符都不少。

        变异：`_git` 的 `raw` 分支去掉、一律 `.strip()` ⇒ 首个文件名缺首字母 ⇒ 红。
        """
        import subprocess

        import code_version as cv

        monkeypatch.setattr(subprocess, "run", self._fake_run(self._PORCELAIN))
        i = cv.resolve()
        assert i["dirty_files"][0] == "code_version.py", (
            f"首个文件名被削了：{i['dirty_files'][0]!r} —— "
            "porcelain 第一行的前导空格是状态列，不能 strip 掉")
        assert i["dirty_files"] == ["code_version.py", "staged.py",
                                    "tests/test_x.py", "new.py"], i["dirty_files"]
        assert i["dirty_count"] == 4

    def test_rev_parse_still_stripped(self, monkeypatch):
        """反向自证：非 raw 的调用仍要 strip —— 否则 sha 会带上换行。

        变异：把 `_git` 改成一律 `rstrip("\n")`（连空格都不剥）⇒ 这里 sha 带空格 ⇒ 红。
        """
        import subprocess

        import code_version as cv

        monkeypatch.setattr(subprocess, "run", self._fake_run("  abc1234  \n"))
        assert cv._git("rev-parse", "--short", "HEAD") == "abc1234"


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
