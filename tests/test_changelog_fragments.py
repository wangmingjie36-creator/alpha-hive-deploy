"""版本碎片 `changelog.d/` 的判据与占号工具（v0.45.728）

判据与工具的唯一真相在 `changelog_fragments.py`（设计与实测见它的模块 docstring）。本文件：

* `TestRealTree`：对**本 worktree 真实的** `changelog.d/` 求值——全量测试里的那道检查
  （提交 / 推送时钩子读 git 对象另查，管道在 `tests/test_changelog_guard_hook.py::TestFragments`）。
* `TestLintTeeth` / `TestProtect`：每条判据各喂一个**只违反它**的样本（断言恰好报一条、报的就是它），
  健康样本不误报——误报的守卫会被整体关掉，而「只验一个方向」证明不了检测器在工作。
* `TestClaimTool`：在临时仓库（bare origin + 克隆）里走真实 git：取号、在 origin/main 上另建提交、
  推送后以 ls-remote 核实、被抢号时重取、带进当前分支（快进 / cherry-pick）、以后 rebase 自动跳过重复提交。
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import changelog_fragments as cf  # noqa: E402
import changelog_guard as cg  # noqa: E402

TOP = "0.45.728"
CLAIM = "<!-- 认领：claude/x 2026-11-05T09:12:33-08:00 9f3a1c07 -->"
PLACEHOLDER = f"## [0.45.730] — 2026-11-05 — 占位（进行中：某件事）\n{CLAIM}\n"
DONE = f"## [0.45.730] — 2026-11-05 — 正式标题\n{CLAIM}\n\n### Fixed\n- `x.py`：修了点东西。\n"


def lint1(text, name="0.45.730.md", top=TOP):
    return cf.lint({f"changelog.d/{name}": text}, top)


def only(problems, needle):
    """恰好一条问题、且是它——样本只该违反一条判据，多报说明判据互相遮盖或样本选错了。"""
    assert len(problems) == 1 and needle in problems[0], problems
    return problems[0]


# ═════════════════════════════ 真实的 changelog.d/ ═════════════════════════════

class TestRealTree:

    def test_reads_this_worktree(self):
        """锚点先对：`_repo_dir()` 必须是本 worktree，否则下面在审别的 checkout。"""
        assert cf._repo_dir() == _ROOT

    def test_fragments_in_this_worktree_pass(self):
        files = cf.worktree_files(_ROOT)
        top = cf.archive_top((_ROOT / cf.ARCHIVE).read_text(encoding="utf-8"))
        assert top, "读不到档案顶号——下面「碎片号 > 档案最大号」那条会恒真"
        problems = cf.lint(files, top)
        assert not problems, ("changelog.d/ 里有不合规的碎片：\n  " + "\n  ".join(problems)
                              + "\n格式与占号流程见 changelog.d/README.md")

    def test_readme_exists_and_points_to_claim(self):
        """README 是新会话读到迁移规则的入口之一（另两处是报错文字与 CHANGELOG.md 文件头）。"""
        text = (_ROOT / "changelog.d" / "README.md").read_text(encoding="utf-8")
        assert "changelog_fragments.py claim" in text and "认领" in text

    def test_archive_header_points_to_the_new_protocol(self):
        """冻结说明写在档案文件头：往旧文件顶部插条目的人第一眼就看得到。"""
        head = (_ROOT / cf.ARCHIVE).read_text(encoding="utf-8").split("\n## [", 1)[0]
        # 认「已冻结」这句陈述本身，不认「冻结」二字：「冻结前已在本文件里占的号」也含它，
        # 删掉冻结声明照样绿（变异实测 A2）。
        assert "本文件已冻结" in head, head
        assert "changelog.d/" in head and "changelog_fragments.py claim" in head, head


# ═════════════════════════════ lint 的牙齿 ═════════════════════════════

class TestLintTeeth:

    def test_healthy_samples_pass(self):
        assert lint1(PLACEHOLDER) == []
        assert lint1(DONE) == []
        assert lint1(DONE.encode("utf-8")) == [], "git blob 是 bytes，要按 UTF-8 解"
        assert cf.lint({"changelog.d/README.md": "任意说明\n"}, TOP) == []
        assert lint1("\n\n" + DONE) == [], "开头的空行不算「首行不是标题」"

    @pytest.mark.parametrize("name", ["0.45.0730.md", "0.45.730.MD", "0.45.730 2.md", "v0.45.730.md",
                                      "0.45.730.md.bak", "notes.txt", "0.45.md"])
    def test_noncanonical_file_names(self, name):
        """前导零 / 大写扩展名 / iCloud 副本都会让**一个号有两个文件**——文件名唯一这条结构保证就破了。"""
        only(lint1(DONE, name=name), "文件名不合规")

    def test_subdirectory(self):
        only(cf.lint({"changelog.d/old/0.45.730.md": DONE}, TOP), "子目录")

    def test_not_a_regular_file(self):
        only(cf.lint({"changelog.d/0.45.730.md": cf.NotRegular("120000")}, TOP), "不是普通文件")

    def test_not_utf8(self):
        only(lint1(b"\xff\xfe\x00"), "不是 UTF-8")

    def test_conflict_markers(self):
        conflicted = DONE.replace("### Fixed\n", "<<<<<<< HEAD\n### Fixed\n=======\n### Added\n>>>>>>> theirs\n")
        only(lint1(conflicted), "冲突标记")

    @pytest.mark.parametrize("text", ["", "  \n\n\t\n"])
    def test_empty(self, text):
        only(lint1(text), "空文件")

    def test_first_line_is_not_a_heading(self):
        only(lint1("先写了一句话\n" + DONE), "首个非空行不是标题")

    def test_heading_format(self):
        """连字符代替破折号——一样会让「首行是标题」不成立。"""
        only(lint1(DONE.replace(" — ", " - ")), "首个非空行不是标题")

    def test_heading_version_differs_from_file_name(self):
        only(lint1(DONE, name="0.45.731.md"), "≠ 文件名")

    def test_bad_date(self):
        only(lint1(DONE.replace("2026-11-05 —", "2026-13-05 —")), "日期不合法")

    def test_two_headings(self):
        """一个碎片只放一个版本（第二个号得不到「一号一文件」的结构保证）。"""
        only(lint1(DONE + "\n## [0.45.731] — 2026-11-05 — 顺手写的另一条\n\n正文\n"), "2 个行首")

    def test_missing_claim(self):
        only(lint1(DONE.replace(CLAIM + "\n", "")), "实有 0 行")

    def test_two_claims(self):
        """两行认领 = 两份碎片被拼到了一起（解冲突时两边都留了）。"""
        only(lint1(DONE.replace(CLAIM, CLAIM + "\n" + CLAIM.replace("9f3a1c07", "0badcafe"))), "实有 2 行")

    def test_malformed_claim(self):
        bad = DONE.replace(CLAIM, "<!-- 认领：claude/x 2026-11-05 -->")
        problems = lint1(bad)
        assert len(problems) == 2 and "格式不对" in problems[0] and "实有 0 行" in problems[1], problems

    def test_bodyless_non_placeholder(self):
        only(lint1(DONE.split("\n\n### Fixed")[0] + "\n"), "没有正文")

    def test_horizontal_rule_is_not_body(self):
        only(lint1(DONE.split("\n\n### Fixed")[0] + "\n\n---\n"), "没有正文")

    def test_closing_an_abandoned_number_needs_a_reason(self):
        """「占位关闭：…」不是占位（PLACEHOLDER_RE 只认「占位（进行中：…）」），要写一句为什么。"""
        closed = PLACEHOLDER.replace("占位（进行中：某件事）", "占位关闭：改由 0.45.731 做")
        only(lint1(closed), "没有正文")
        assert lint1(closed + "\n撞号后让给了别的会话。\n") == []

    @pytest.mark.parametrize("version", ["0.45.728", "0.45.700", "0.44.999"])
    def test_number_not_above_archive(self, version):
        text = DONE.replace("0.45.730", version)
        only(lint1(text, name=f"{version}.md"), "≤ 档案")

    def test_number_compared_numerically(self):
        """0.45.1000 > 0.45.728（按字符串比较会反过来）。"""
        text = DONE.replace("0.45.730", "0.45.1000")
        assert lint1(text, name="0.45.1000.md") == []


class TestProtect:
    BASE = {"changelog.d/0.45.729.md": PLACEHOLDER.replace("0.45.730", "0.45.729"),
            "changelog.d/README.md": "说明\n"}

    def test_completing_a_placeholder_keeps_the_claim_and_passes(self):
        done = DONE.replace("0.45.730", "0.45.729")
        assert cf.protect(self.BASE, {**self.BASE, "changelog.d/0.45.729.md": done.encode()}) == []

    def test_adding_a_fragment_passes(self):
        assert cf.protect(self.BASE, {**self.BASE, "changelog.d/0.45.730.md": DONE}) == []

    def test_deleting_a_fragment_on_main(self):
        only(cf.protect(self.BASE, {"changelog.d/README.md": "说明\n"}), "被删掉")

    def test_renaming_counts_as_deleting(self):
        moved = {"changelog.d/0.45.731.md": self.BASE["changelog.d/0.45.729.md"], "changelog.d/README.md": "说明\n"}
        only(cf.protect(self.BASE, moved), "被删掉")

    def test_changing_the_claim_line(self):
        """撞号后解冲突选错边：别人的碎片被自己的盖掉，认领注释随之变了。"""
        mine = DONE.replace("0.45.730", "0.45.729").replace("9f3a1c07", "0badcafe")
        only(cf.protect(self.BASE, {**self.BASE, "changelog.d/0.45.729.md": mine}), "认领注释变了")

    def test_removing_the_claim_line(self):
        gone = DONE.replace("0.45.730", "0.45.729").replace(CLAIM + "\n", "")
        only(cf.protect(self.BASE, {**self.BASE, "changelog.d/0.45.729.md": gone}), "认领注释变了")


class TestHelpers:

    def test_rendered_placeholder_passes_lint(self):
        text = cf.render_placeholder("0.45.730", "一句话", who="claude/x", at="2026-11-05T09:12:33-08:00",
                                     nonce=secrets.token_hex(4), date="2026-11-05")
        assert lint1(text) == []
        assert cf.is_placeholder(text) and cf.claim_of(text)

    def test_two_claims_of_the_same_number_never_have_the_same_content(self):
        """内容相同的同号碎片在 rebase 时被**静默合掉**（实测）——随机串保证一定撞得出声。"""
        texts = {cf.render_placeholder("0.45.730", "同一句", who="main", at="t", nonce=secrets.token_hex(4),
                                       date="2026-11-05") for _ in range(20)}
        assert len(texts) == 20

    def test_version_order_and_next(self):
        assert cf.version_key("0.45.99") < cf.version_key("0.45.100")
        assert cf.next_version(["0.45.99", "0.45.100", "0.45.728"]) == "0.45.729"
        with pytest.raises(ValueError):
            cf.next_version(["2"])

    def test_render_puts_fragments_first_newest_first_without_claims(self):
        files = {"changelog.d/0.45.729.md": DONE.replace("0.45.730", "0.45.729"), "changelog.d/0.45.730.md": DONE,
                 "changelog.d/README.md": "说明\n"}
        archive = f"# log\n\n---\n\n## [{TOP}] — 2026-11-05 — 档案顶\n\n档案正文\n\n---\n\n## [0.45.727] — d — 次\n\n正文\n"
        out = cf.render(files, archive)
        order = re.findall(r"^## \[([0-9.]+)\]", out, re.M)
        assert order == ["0.45.730", "0.45.729", TOP, "0.45.727"]
        assert "认领" not in out and "勿提交" in out
        assert re.findall(r"^## \[([0-9.]+)\]", cf.render(files, archive, limit=3), re.M) == order[:3]

    def test_entry_text_finds_fragments_then_archive(self, tmp_path):
        (tmp_path / "changelog.d").mkdir()
        (tmp_path / "changelog.d" / "0.45.730.md").write_text(DONE, encoding="utf-8")
        (tmp_path / cf.ARCHIVE).write_text(f"# log\n\n## [{TOP}] — 2026-11-05 — 档案顶\n\n正文\n\n---\n", encoding="utf-8")
        assert cf.entry_text("0.45.730", tmp_path).startswith("## [0.45.730] — 2026-11-05 — 正式标题")
        assert "认领" not in cf.entry_text("0.45.730", tmp_path)
        assert cf.entry_text(TOP, tmp_path) == f"## [{TOP}] — 2026-11-05 — 档案顶\n\n正文"
        assert cf.entry_text("0.45.1", tmp_path) is None

    def test_entry_text_refuses_to_pick_among_duplicates(self, tmp_path):
        (tmp_path / cf.ARCHIVE).write_text("## [0.1.0] — d — 甲\n\n正文\n\n## [0.1.0] — d — 乙\n\n正文\n", encoding="utf-8")
        with pytest.raises(ValueError):
            cf.entry_text("0.1.0", tmp_path)


# ═════════════════════════════ 占号工具（真实 git） ═════════════════════════════

ARCHIVE = f"# log\n\n---\n\n## [{TOP}] — 2026-11-05 — 占位（进行中：档案顶）\n\n---\n\n## [0.45.727] — d — 次\n\n正文\n"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """bare origin（main 上已有碎片协议：档案 + changelog.d/README.md）+ 按需克隆。不装钩子（钩子管道另测）。"""
    for var in cg._GIT_LOCATION_VARS:     # pytest 若从 git hook 里被拉起，继承的定位变量会打到真仓库上
        monkeypatch.delenv(var, raising=False)
    origin = tmp_path / "origin.git"

    def git(*args, cwd, check=True):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{p.stdout}{p.stderr}")
        return p

    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    def clone(name):
        d = tmp_path / name
        git("clone", "-q", str(origin), str(d), cwd=tmp_path)
        git("config", "user.email", f"{name}@t", cwd=d)
        git("config", "user.name", name, cwd=d)
        return d

    seed = tmp_path / "seed"
    git("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
    git("config", "user.email", "s@t", cwd=seed)
    git("config", "user.name", "s", cwd=seed)
    (seed / cf.ARCHIVE).write_text(ARCHIVE, encoding="utf-8")
    (seed / "changelog.d").mkdir()
    (seed / "changelog.d" / "README.md").write_text("说明\n", encoding="utf-8")
    (seed / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "base", cwd=seed)
    git("push", "-q", str(origin), "main", cwd=seed)

    def origin_main():
        return git("--git-dir", str(origin), "rev-parse", "main", cwd=tmp_path).stdout.strip()

    def origin_files(rev="main"):
        out = git("--git-dir", str(origin), "ls-tree", "-r", "--name-only", rev, cwd=tmp_path).stdout
        return sorted(out.split())

    return type("Remote", (), dict(origin=origin, tmp=tmp_path, git=staticmethod(git), clone=staticmethod(clone),
                                   origin_main=staticmethod(origin_main), origin_files=staticmethod(origin_files)))


def _sha_from(out: str) -> str:
    m = re.search(r"已建占号提交 ([0-9a-f]{40})", out)
    assert m, out
    return m.group(1)


class TestClaimTool:

    def test_without_push_builds_a_commit_on_origin_main_and_touches_nothing_local(self, remote, capsys):
        a = remote.clone("a")
        remote.git("checkout", "-qb", "feat", cwd=a)
        (a / "app.py").write_text("x = 2\n", encoding="utf-8")
        remote.git("commit", "-qam", "本地进度", cwd=a)
        (a / "app.py").write_text("x = 3  # 没提交\n", encoding="utf-8")
        head, main = remote.git("rev-parse", "HEAD", cwd=a).stdout.strip(), remote.origin_main()

        assert cf.claim(a, "一句话说明", push=False) == 0
        out = capsys.readouterr().out
        sha = _sha_from(out)
        assert "还没推送，号还没占住" in out and f"{sha}:refs/heads/main" in out and f"adopt {sha}" in out
        # adopt 作用于**当前目录**所在的 checkout：交给人的命令不带 cd，在别的目录里跑就接进了别人的分支
        assert f"cd '{a}' && " in out, out

        parent = remote.git("rev-parse", f"{sha}^", cwd=a).stdout.strip()
        assert parent == main, "占号提交必须建在 origin/main 上，而不是当前分支上"
        changed = remote.git("diff-tree", "--no-commit-id", "--name-only", "-r", f"{sha}^", sha, cwd=a).stdout.split()
        assert changed == ["changelog.d/0.45.729.md"]
        text = remote.git("show", f"{sha}:changelog.d/0.45.729.md", cwd=a).stdout
        assert cf.lint({"changelog.d/0.45.729.md": text}, TOP) == []
        assert "占位（进行中：一句话说明）" in text and cf.claim_of(text).startswith("<!-- 认领：feat ")

        assert remote.origin_main() == main, "没带 --push 就不许推"
        assert remote.git("rev-parse", "HEAD", cwd=a).stdout.strip() == head
        assert (a / "app.py").read_text(encoding="utf-8") == "x = 3  # 没提交\n"
        assert not (a / "changelog.d" / "0.45.729.md").exists()

    def test_push_is_verified_and_the_claim_lands_in_a_diverged_dirty_branch(self, remote, capsys):
        """会话做到一半（分支有自己的提交 + 未提交改动）也能占号；以后 rebase 到 main 时 git 自动跳过重复提交。"""
        a = remote.clone("a")
        remote.git("checkout", "-qb", "feat", cwd=a)
        (a / "app.py").write_text("x = 2\n", encoding="utf-8")
        remote.git("commit", "-qam", "本地进度", cwd=a)
        (a / "notes.txt").write_text("草稿\n", encoding="utf-8")                  # 未跟踪
        (a / "app.py").write_text("x = 3  # 没提交\n", encoding="utf-8")         # 未暂存

        assert cf.claim(a, "做到一半", push=True, trailers=["Co-Authored-By: t <t@t>"]) == 0
        out = capsys.readouterr().out
        assert "✅ 已占 0.45.729" in out and "cherry-pick" in out, out
        assert "changelog.d/0.45.729.md" in remote.origin_files()
        claim_sha = remote.origin_main()
        assert "Co-Authored-By: t <t@t>" in remote.git("log", "-1", "--format=%B", claim_sha, cwd=a).stdout

        assert (a / "changelog.d" / "0.45.729.md").is_file(), "占位文件没带进当前分支"
        assert (a / "app.py").read_text(encoding="utf-8") == "x = 3  # 没提交\n"
        assert (a / "notes.txt").is_file()
        assert remote.git("log", "-1", "--format=%s", "HEAD~1", cwd=a).stdout.strip() == "本地进度"

        # 完工：补正文、提交，rebase 到 main ⇒ cherry-pick 来的重复提交被跳过，无冲突
        remote.git("checkout", "--", "app.py", cwd=a)
        frag = a / "changelog.d" / "0.45.729.md"
        frag.write_text(frag.read_text(encoding="utf-8").replace("占位（进行中：做到一半）", "做完了") + "\n正文。\n",
                        encoding="utf-8")
        remote.git("commit", "-qam", "完工", cwd=a)
        remote.git("fetch", "-q", "origin", cwd=a)
        rb = remote.git("rebase", "origin/main", cwd=a, check=False)
        assert rb.returncode == 0, rb.stderr
        mine = remote.git("log", "--format=%s", "origin/main..HEAD", cwd=a).stdout.split("\n")
        assert [s for s in mine if s] == ["完工", "本地进度"], mine

    def test_branch_behind_main_is_fast_forwarded(self, remote, capsys):
        a, b = remote.clone("a"), remote.clone("b")
        (b / "app.py").write_text("x = 9\n", encoding="utf-8")
        remote.git("commit", "-qam", "别人推的", cwd=b)
        remote.git("push", "-q", "origin", "HEAD:main", cwd=b)

        assert cf.claim(a, "落后时占号", push=True) == 0
        assert "快进" in capsys.readouterr().out
        assert remote.git("rev-parse", "HEAD", cwd=a).stdout.strip() == remote.origin_main()

    def test_losing_the_race_takes_the_next_number(self, remote, monkeypatch, capsys):
        """号被别人抢先推上 main ⇒ 推送被拒 ⇒ 重新取号重推。两份碎片各有各的号，谁也没盖掉谁。"""
        a, b = remote.clone("a"), remote.clone("b")
        real_build = cf.build_claim_commit
        calls = []

        def racing_build(root, base, version, content, message):
            calls.append(version)
            if len(calls) == 1:          # 我们取完号、还没推的那一刻，别人抢先推了同一个号
                other = cf.render_placeholder(version, "别人的", who="b", at="t", nonce="0badcafe", date="2026-11-05")
                (b / "changelog.d" / f"{version}.md").write_text(other, encoding="utf-8")
                remote.git("add", "-A", cwd=b)
                remote.git("commit", "-qm", "别人占号", cwd=b)
                remote.git("push", "-q", "origin", "HEAD:main", cwd=b)
            return real_build(root, base, version, content, message)

        monkeypatch.setattr(cf, "build_claim_commit", racing_build)
        assert cf.claim(a, "我的", push=True) == 0
        out = capsys.readouterr().out
        assert calls == ["0.45.729", "0.45.730"], calls
        assert "被拒" in out and "✅ 已占 0.45.730" in out, out
        files = remote.origin_files()
        assert "changelog.d/0.45.729.md" in files and "changelog.d/0.45.730.md" in files
        theirs = remote.git("--git-dir", str(remote.origin), "show", "main:changelog.d/0.45.729.md",
                            cwd=remote.tmp).stdout
        assert "0badcafe" in theirs, "别人的碎片被盖掉了"

    def test_push_refused_without_main_moving_does_not_retry(self, remote, monkeypatch, capsys):
        """推送被拒、远端 main 却没动 ⇒ 不是被抢号（钩子 / 权限）⇒ 重试也没用，停下并把原因带出来。"""
        hook = remote.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 远端拒收：测试用 >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        a = remote.clone("a")
        calls = []
        real_build = cf.build_claim_commit
        monkeypatch.setattr(cf, "build_claim_commit", lambda *a_, **k: calls.append(1) or real_build(*a_, **k))
        main = remote.origin_main()

        assert cf.claim(a, "x", push=True) == 1
        err = capsys.readouterr().err
        assert "不是被抢号" in err and "远端拒收" in err, err
        assert len(calls) == 1 and remote.origin_main() == main
        assert not (a / "changelog.d" / "0.45.729.md").exists()

    def test_claims_of_the_same_number_in_the_same_second_still_differ(self, remote, capsys):
        """两个会话同一秒、同一句说明占同一个号：内容若相同，rebase 会把后来者的占号**静默合掉**（实测）。
        随机串必须来自 claim 本身——变异：把 `secrets.token_hex(4)` 换成常量 ⇒ 两份相同 ⇒ 红。"""
        import datetime as dt

        a = remote.clone("a")
        now = dt.datetime(2026, 11, 5, 9, 0, 0, tzinfo=dt.timezone.utc)
        blobs = []
        for _ in range(2):
            assert cf.claim(a, "同一句", push=False, now=now) == 0
            sha = _sha_from(capsys.readouterr().out)
            blobs.append(remote.git("rev-parse", f"{sha}:changelog.d/0.45.729.md", cwd=a).stdout.strip())
        assert blobs[0] != blobs[1]

    def test_adopt_refuses_a_commit_that_is_not_on_main(self, remote, capsys):
        a = remote.clone("a")
        assert cf.claim(a, "x", push=False) == 0
        sha = _sha_from(capsys.readouterr().out)
        assert cf.adopt(a, sha) == 1
        assert "没占住" in capsys.readouterr().err
        assert not (a / "changelog.d" / "0.45.729.md").exists()

    def test_adopt_after_a_manual_push_and_its_staged_changes_guard(self, remote, capsys):
        """推不了 main 的会话：照打印的命令推送，再 adopt。暂存区有改动时先说清楚（git 自己会建议 stash——本仓禁裸 stash）。"""
        a = remote.clone("a")
        remote.git("checkout", "-qb", "feat", cwd=a)
        (a / "app.py").write_text("x = 2\n", encoding="utf-8")
        remote.git("commit", "-qam", "本地进度", cwd=a)
        assert cf.claim(a, "x", push=False) == 0
        sha = _sha_from(capsys.readouterr().out)
        remote.git("push", "-q", "origin", f"{sha}:refs/heads/main", cwd=a)

        (a / "app.py").write_text("x = 3\n", encoding="utf-8")
        remote.git("add", "app.py", cwd=a)
        assert cf.adopt(a, sha) == 1
        err = capsys.readouterr().err
        assert "暂存区有未提交的改动" in err and "stash" in err, err
        assert remote.git("diff", "--cached", "--name-only", cwd=a).stdout.split() == ["app.py"], "暂存的改动被动了"

        remote.git("commit", "-qm", "提交掉", cwd=a)
        assert cf.adopt(a, sha) == 0
        assert (a / "changelog.d" / "0.45.729.md").is_file()
        assert cf.adopt(a, sha) == 0 and "无需带入" in capsys.readouterr().out, "第二次 adopt 应当是无事可做"

    def test_claim_refuses_before_the_protocol_is_on_main(self, remote, capsys):
        a = remote.clone("a")
        remote.git("rm", "-q", "changelog.d/README.md", cwd=a)
        remote.git("commit", "-qm", "去掉协议", cwd=a)
        remote.git("push", "-q", "origin", "HEAD:main", cwd=a)
        assert cf.claim(a, "x", push=True) == 1
        assert "还没合入 main" in capsys.readouterr().err

    def test_cli_acts_on_the_checkout_it_is_run_from(self, remote):
        """拿别处的脚本副本在自己的 checkout 里跑：占号带进的是**当前目录**那个 checkout，不是脚本所在的仓库。"""
        a = remote.clone("a")
        env = {k: v for k, v in os.environ.items() if k not in cg._GIT_LOCATION_VARS}
        run = subprocess.run([sys.executable, str(_ROOT / "changelog_fragments.py"), "claim", "命令行", "--push"],
                             cwd=a / "changelog.d", env=env, capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
        assert (a / "changelog.d" / "0.45.729.md").is_file()
        assert not (_ROOT / "changelog.d" / "0.45.729.md").exists() or "命令行" not in \
            (_ROOT / "changelog.d" / "0.45.729.md").read_text(encoding="utf-8")
        st = subprocess.run([sys.executable, str(_ROOT / "changelog_fragments.py"), "status", "--no-fetch"],
                            cwd=a, env=env, capture_output=True, text=True)
        assert st.returncode == 0, st.stderr
        assert "下一个号 0.45.730" in st.stdout and "0.45.729  碎片" in st.stdout and "档案（冻结前占的号" in st.stdout
