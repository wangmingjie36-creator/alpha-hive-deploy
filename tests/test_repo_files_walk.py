"""v0.45.642 `tests/_repo_files.py::walk_own_python_files`——全仓守卫共用的「本仓自己的 .py」剪枝遍历。

为什么单独有这份测试：这个遍历是好几条全仓守卫（`test_earnings_calendar`、`test_portfolio_greeks_bid_zero` …）的**视野**。
视野错了，守卫照样绿：多扫了第三方 → 慢到超时（v0.45.636）；少扫了第一方 → 守卫在没人看见的地方失效；`os.walk` 吞掉读不了的
目录 → 覆盖悄悄缩水。所以这里管三件事：

1. 合成树上的规则（排除什么、记录什么、错误怎么出来）；
2. 剪枝发生在**遍历时**（被剪的目录一个都不许走进去）；
3. 对真实仓库的 **git 清单**对账——清单是「第一方」的独立定义：被名字规则剪掉的目录里不许有已跟踪的 .py，
   已跟踪的第一方 .py 一个都不许从遍历结果里掉。
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests import _repo_files as RF

_ROOT = Path(RF.__file__).resolve().parent.parent


def _tree(tmp_path):
    for rel in ("a.py", "pkg/b.py", "pkg/deep/c.py", "pkg/b 2.py", "c 13.py", "alphabot 2/straddle.py",
                "pkg/sub 13/m.py", ".git/e.py", ".claude/worktrees/w/d.py", "mcp-servers/m/.venv/lib/dep.py",
                "mcp-servers/m/server.py", "libs/site-packages/lib.py", "node_modules/n.py", "vendor/v.py",
                "third_party/t.py", "tests/t.py", "experiments/x.py", "pkg/.hidden/h.py", ".hidden_top.py",
                "pkg/.hidden_file.py", "pkg/notes.txt"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _rels(res, root):
    return [p.relative_to(root).as_posix() for p in res.files]


class TestSyntheticTree:
    def test_only_first_party_python_files_survive(self, tmp_path):
        root = _tree(tmp_path)
        got = _rels(RF.walk_own_python_files(root, skip_dirs={"tests", "experiments"}), root)
        assert got == ["a.py", "mcp-servers/m/server.py", "pkg/b.py", "pkg/deep/c.py"]

    def test_skip_dirs_is_the_callers_choice(self, tmp_path):
        root = _tree(tmp_path)
        got = _rels(RF.walk_own_python_files(root), root)
        assert "tests/t.py" in got and "experiments/x.py" in got

    def test_icloud_copies_are_skipped_for_files_and_for_directories(self, tmp_path):
        root = _tree(tmp_path)
        got = _rels(RF.walk_own_python_files(root), root)
        assert not [g for g in got if " 2" in g or " 13" in g], got

    def test_every_pruned_directory_is_recorded_with_its_reason(self, tmp_path):
        root = _tree(tmp_path)
        res = RF.walk_own_python_files(root, skip_dirs={"tests", "experiments"})
        reasons = dict(res.pruned)
        assert reasons[".git"] == "hidden" and reasons["mcp-servers/m/.venv"] == "hidden"
        assert reasons["libs/site-packages"] == "vendored" and reasons["node_modules"] == "vendored"
        assert reasons["vendor"] == "vendored" and reasons["third_party"] == "vendored"
        assert reasons["alphabot 2"] == "icloud" and reasons["pkg/sub 13"] == "icloud"
        assert reasons["tests"] == "skip" and reasons["experiments"] == "skip"
        assert res.pruned == sorted(res.pruned)

    def test_result_is_sorted_and_files_are_absolute_under_root(self, tmp_path):
        root = _tree(tmp_path)
        res = RF.walk_own_python_files(root)
        assert res.files == sorted(res.files) and all(p.is_absolute() for p in res.files)

    def test_pruned_directories_are_never_entered(self, tmp_path, monkeypatch):
        """剪枝要发生在遍历时：先全量枚举再丢掉，就是 v0.45.636 之前那条守卫解析 1.5 万个第三方文件的写法。"""
        root = _tree(tmp_path)
        visited, real_walk = [], os.walk

        def spy(top, *a, **k):
            for dp, dn, fn in real_walk(top, *a, **k):
                visited.append(Path(dp).relative_to(top).as_posix())
                yield dp, dn, fn

        monkeypatch.setattr(os, "walk", spy)
        RF.walk_own_python_files(root, skip_dirs={"tests", "experiments"})
        bad = [v for v in visited if set(v.split("/")) & {".git", ".claude", ".venv", "site-packages", "node_modules",
                                                           "vendor", "third_party", "tests", "experiments", ".hidden",
                                                           "alphabot 2", "sub 13"}]
        assert not bad, bad

    def test_unreadable_directories_come_out_as_errors_instead_of_vanishing(self, tmp_path, monkeypatch):
        """`os.walk` 默认吞掉读不了的目录（onerror=None）。遍历必须把 onerror 接上，错误原样给调用方。
        用间谍直接触发 onerror，不靠 chmod（root 用户 / 某些文件系统上 chmod 000 读得了，测试就成了恒绿）。"""
        root = _tree(tmp_path)
        real_walk, seen_onerror = os.walk, []

        def spy(top, *a, **k):
            seen_onerror.append(k.get("onerror"))
            if k.get("onerror"):
                k["onerror"](PermissionError(13, "Permission denied", str(Path(top) / "locked")))
            yield from real_walk(top, *a, **k)

        monkeypatch.setattr(os, "walk", spy)
        res = RF.walk_own_python_files(root)
        assert seen_onerror and seen_onerror[0] is not None, "遍历没有接 onerror：读不了的目录会被无声吞掉"
        assert len(res.errors) == 1 and "locked" in res.errors[0] and "Permission denied" in res.errors[0]

    def test_clean_tree_has_no_errors(self, tmp_path):
        assert RF.walk_own_python_files(_tree(tmp_path)).errors == []


def _tracked_py():
    """git 清单里所有被跟踪的 .py（相对仓库根）。清单是「第一方」的独立定义——不依赖遍历自己的名字规则。
    取不到清单就是红，不 skip：开发检出 / worktree / CI 都在 git 仓库里。"""
    r = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=str(_ROOT), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"git ls-files 失败（{r.returncode}）：{r.stderr[:200]}"
    return [x for x in r.stdout.split("\x00") if x]


class TestAgainstTheGitManifest:
    """对真实仓库、对独立的 git 清单对账。"""

    def test_manifest_itself_was_read(self):
        assert len(_tracked_py()) >= 300, "git 清单读出来太少——对账的另一端是空的，下面的断言都不作数"

    def test_the_name_rules_hide_no_tracked_source(self):
        """点号目录 / vendored 目录名 / iCloud 副本名这些**按名字**的剪枝规则，不许把已跟踪的第一方 .py 剪掉
        （例如哪天有人在 `vendor/`、`.hooks/` 下提交了自己的 .py，这条守卫视野里就悄悄少了一块）。"""
        hidden = [x for x in _tracked_py()
                  if not RF._is_ours(Path(x)) or any(RF.ICLOUD_COPY.search(p) for p in Path(x).parts)]
        assert hidden == [], (f"这些已跟踪的 .py 会被遍历的名字规则排除：{hidden[:5]}——要么改名，要么在这里登记并说明理由")

    def test_pruned_directories_contain_no_tracked_source(self):
        res = RF.walk_own_python_files(_ROOT)
        tracked = _tracked_py()
        inside = {p: [t for t in tracked if t.startswith(p + "/")] for p, why in res.pruned if why != "skip"}
        assert {p: t for p, t in inside.items() if t} == {}

    def test_every_tracked_first_party_file_is_walked(self):
        """遍历结果必须是 git 清单（第一方）的超集——掉一个文件，依赖它的守卫就在那个文件里失明。
        超出的部分是已创建但未提交的新文件，允许（遍历读工作树，这正是它不用 `git ls-files` 的原因）。"""
        walked = {p.relative_to(_ROOT).as_posix() for p in RF.walk_own_python_files(_ROOT).files}
        missing = sorted(set(_tracked_py()) - walked)
        assert missing == [], missing[:10]

    def test_real_repo_walk_reads_everything_and_enters_no_third_party_tree(self):
        # 点号目录的快照取在遍历**之前**，对账时只认「遍历后仍存在」的：别的进程在两者之间新建 / 删掉
        # `.ruff_cache` 之类的缓存目录，不该让这条偶发红。
        before = sorted(e.name for e in os.scandir(_ROOT) if e.is_dir(follow_symlinks=False) and e.name.startswith("."))
        res = RF.walk_own_python_files(_ROOT)
        assert res.errors == [], res.errors[:5]
        # 「点号目录确实被剪了」的自证，按**这个环境里实际存在**的点号目录算：`.git` 在开发检出 / 干净克隆里是目录、
        # 在 git worktree 里是一个文件（`gitdir: …`，不在 `os.walk` 的 dirnames 里），写死 `.git` 在 worktree 里恒红
        # （v0.45.646）。`.github` / `.claude` 是被跟踪的目录，任何环境都有，所以集合不会是空的——空集合会让
        # 下面的包含断言恒真，故先断言非空。
        assert before, "仓库根下一个点号目录都没有——剪枝自证没有可对照的样本（.github / .claude 是被跟踪的）"
        missing = [d for d in before if (_ROOT / d).is_dir() and (d, "hidden") not in res.pruned]
        assert not missing, f"这些点号目录存在却没被剪——剪枝规则没生效：{missing}"
        assert all(why in {"skip", "vendored", "hidden", "icloud"} for _, why in res.pruned)
        bad = [p.relative_to(_ROOT).as_posix() for p in res.files
               if {".venv", "site-packages", "node_modules"} & set(p.relative_to(_ROOT).parts)]
        assert not bad, bad[:5]


class TestGuardsUseTheSharedWalk:
    """别让两条守卫哪天又各自长出一份手写遍历。"""

    @pytest.mark.parametrize("fname, needle", [("test_earnings_calendar.py", "walk_own_python_files"),
                                              ("test_portfolio_greeks_bid_zero.py", "walk_own_python_files")])
    def test_call_the_shared_walker_and_have_no_walk_of_their_own(self, fname, needle):
        src = (_ROOT / "tests" / fname).read_text(encoding="utf-8")
        assert needle in src
        # 守卫自己的 os.walk 只允许出现在「间谍」测试里（monkeypatch 它来证明剪枝），不许有真遍历
        real_calls = [ln for ln in src.splitlines() if "os.walk(" in ln and "real_walk" not in ln]
        assert real_calls == [], real_calls
