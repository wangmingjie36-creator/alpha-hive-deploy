"""数据根迁移阶段 6 ①：代码仓库不许跟踪数据（v0.45.394）。

生产数据自阶段 5 起在 `ALPHA_HIVE_HOME`（~/alpha-hive-data），代码仓库是公开的。
任何被 git 跟踪（含已 `git add` 进暂存区）的顶层条目，只要在
`data_backup.migrate_data_root.classify` 里属于 MOVE / MOVE_DB，就是「数据又被提交了」⇒ 红。

- 判据**复用迁移工具的分类表**，不另抄一份清单：新增产物登记进 MOVE 规则的同时，这里自动覆盖。
- 看的是 `git ls-files`（索引），不是工作区：只存在于磁盘、被 .gitignore 挡住的数据不红，
  `git add -f` 硬塞进去的会红。
- 推送到 main 时由 changelog_guard 的契约闸跑（CONTRACT_REQUIRED_TESTS），不必等 CI。
- 例外：无。需要提交的是代码，不是数据；真有「随代码发布的静态资源」就登记进 SKIP_EXACT。
"""
import os
import subprocess

from data_backup import migrate_data_root as m

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def tracked_data_entries(root=_ROOT):
    """返回被跟踪的数据类顶层条目 {名字: 文件数}。git 调用失败即抛（不许把失败当「没有」）。"""
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True,
                         env=_env(), check=True).stdout.decode().split("\0")
    files = [f for f in out if f]
    assert files, "git ls-files 空输出：守卫没有看到任何文件，等于没跑"
    tops = {}
    for f in files:
        head, sep, _ = f.partition("/")
        is_dir, n = tops.get(head, (False, 0))
        tops[head] = (is_dir or bool(sep), n + 1)
    return {n: cnt for n, (is_dir, cnt) in sorted(tops.items())
            if m.classify(n, is_dir)[0] in ("MOVE", "MOVE_DB")}


def _by_rule(bad):
    """按分类表给的理由归并，失败信息不被几千行文件名淹没。"""
    groups = {}
    for n, cnt in bad.items():
        why = m.classify(n, "/" in n or n in m.MOVE_DIRS)[1]
        g = groups.setdefault(why, [0, 0, n])
        g[0] += 1
        g[1] += cnt
    return groups


def test_no_production_data_is_tracked_by_git():
    bad = tracked_data_entries()
    assert not bad, (
        f"代码仓库跟踪了 {len(bad)} 个数据类顶层条目（阶段 6：数据只在数据根，不进公开仓库）。"
        "解除跟踪用 `git rm --cached`（历史保留），并补 .gitignore。按规则归并（条目数 / 文件数 / 示例）：\n"
        + "\n".join(f"  {why}: {g[0]} / {g[1]}  e.g. {g[2]}" for why, g in sorted(_by_rule(bad).items())))


# ── 守卫自己的牙：合成仓库上反向自证（没有这组，上面那条的绿灯证明不了任何事）──────────
def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={**_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "foo.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("r\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "code only")
    return tmp_path


def test_guard_green_on_code_only_repo(tmp_path):
    assert tracked_data_entries(str(_repo(tmp_path))) == {}


def test_guard_red_when_data_file_is_staged(tmp_path):
    """往暂存区加一个数据文件（尚未提交）就红——阶段 6 验收「提交数据文件会红」。"""
    root = _repo(tmp_path)
    (root / "alpha-hive-daily-2026-10-03.json").write_text("{}")
    (root / "report_snapshots").mkdir()
    (root / "report_snapshots" / "NVDA_2026-10-03.json").write_text("{}")
    _git(root, "add", "-A")
    assert tracked_data_entries(str(root)) == {"alpha-hive-daily-2026-10-03.json": 1,
                                               "report_snapshots": 1}


def test_guard_ignores_gitignored_data_on_disk(tmp_path):
    """数据留在磁盘上、被 .gitignore 挡住 ⇒ 不红（数据根就该有这些）；`add -f` 硬塞才红。"""
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("report_snapshots/\n")
    (root / "report_snapshots").mkdir()
    (root / "report_snapshots" / "a.json").write_text("{}")
    _git(root, "add", "-A")
    assert tracked_data_entries(str(root)) == {}
    _git(root, "add", "-f", "report_snapshots/a.json")
    assert tracked_data_entries(str(root)) == {"report_snapshots": 1}
