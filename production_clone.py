"""生产代码独立克隆 `~/alpha-hive-prod` 与它的守卫（数据根迁移阶段 8，v0.45.431）

为什么有这个文件
----------------
生产检出 `~/Desktop/Alpha Hive` 同时是开发主检出：所有 worktree 挂在它的 `.git` 下，Claude 会话
默认也在这里启动。于是会话**直接在生产 main 上提交**、不推送：

  10-04 06:37  fb0bb88d（Alpha Bot 帮助页）      ⇒ 10-05 扫描 diverged（ahead 1 / behind 19）
  10-06 06:21  68efa970（Alpha Bot 文案）        ⇒ ahead 1 / behind 14
  10-08 02:22  53df066c（Oracle 异常期权流限速）  ⇒ ahead 1 / behind 1

v0.45.402 退役 `push_main` 之后分叉**不会自愈**：`production_sync` 只快进，遇分叉记 `diverged`、照跑旧代码，
直到有人把那个提交合进 origin/main。P1 告警只进本地 alerts 文件（Slack 精简规则不推送）⇒ 无人看见。

治法不是再加一条提醒，是让「生产代码」与「开发检出」**物理上不是同一个仓库**：

* 生产代码 = 一份**独立克隆**（不是 worktree：worktree 共享 refs / hooks / index 锁 / stash，
  开发仓库里任何一个会话移动 main，生产都跟着动）。唯一的写入者是扫描前的 `production_sync`（只快进）。
* 克隆自己的 `.git/hooks` 装守卫（开发仓库的钩子不受影响）：

  reference-transaction   `refs/heads/main` 只许指向 `refs/remotes/origin/main` 的祖先（含相等）。
                          git 在**每一次** ref 写入的 prepared 阶段调用它、非零即中止事务——
                          `commit` / `commit --no-verify` / `merge` / `rebase` / `cherry-pick` / `reset` /
                          `update-ref` 全都绕不过；`pull --ff-only` 先在**单独事务**里更新 origin/main、
                          再把 main 移到它（2026-10-08 实测），所以快进照常放行。
  pre-commit / pre-merge-commit / pre-rebase
                          早一步、说人话地拒绝（不然要等 ref 事务失败，索引里已经暂存了改动）。
  pre-push                只许推 `refs/heads/gh-pages`（网站部署，`report_deployer`）。

谁装、谁核
----------
编排器调 `production_sync.py` 时设 `ALPHA_HIVE_PRODUCTION_CLONE=1`（**期望从外部声明**：重新 clone 会丢掉
克隆内的任何标记，只有编排器知道「这里应该是生产克隆」）⇒ `production_sync` 在快进**之前**调
`ensure_guard`：缺的钩子补上、旧版本的刷新，再核一遍现状 ⇒ 结果进 `production_sync.json` 的
`clone_guard` ⇒ `scan_timing` ⇒ status.json ⇒ `alert_manager` P1。环境变量用不认识它的旧代码会忽略
（不像新增 CLI 参数会让旧 argparse 退出）⇒ 克隆停在旧提交上也能先快进、下一轮再上守卫，不会互锁。

`ok` 要同时满足：HEAD 解析得出提交（v0.45.432）、独立仓库、origin 是 GitHub 上的 `alpha-hive-deploy`、
没有 `core.hooksPath` 改道、五个钩子都是本文件当前版本、被跟踪文件没有未提交改动。任何一条不满足都
**不拦扫描**（扫描照跑），只红。

三条拒绝：
* 不是独立仓库（`.git` 是文件 = worktree；或挂着 linked worktree = 开发仓库）⇒ **不写钩子**。
  装进开发仓库会拦住所有 worktree 的提交——有人手动跑 production_sync 时也不能造成这个后果。
* origin 不是 `alpha-hive-deploy`（v0.45.432）⇒ **不写钩子**：那不是我们的克隆（如 `setup --dest` 指错到数据备份仓，
  装上之后备份仓自己的提交全被拒）。
* 同名钩子不是本文件生成的（没有标记行）⇒ 不覆盖、报 `foreign`（不删别人的东西，但要红）。

手动入口（cutover 与排查）：
  /usr/local/bin/python3 production_clone.py setup            # 克隆到 ~/alpha-hive-prod（已存在则只补守卫）
  /usr/local/bin/python3 production_clone.py check            # 只读体检，不写任何东西
退出码：0 ok / 1 守卫或状态不合格（含拒绝） / 2 克隆失败。stdout 是一份 JSON。

⚠️ 钩子是减速带不是安全边界：`core.hooksPath` 改道、直接改 `.git/refs`，都能绕过——所以 `ok` 同时核
`core.hooksPath` 与工作区，绕过之后的下一轮扫描会红。真正的根治是会话不在这里启动（开发在
`~/Desktop/Alpha Hive` 的 worktree 里做）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from hive_logger import get_logger

_log = get_logger("production_clone")

#: 生产克隆应当从这里拉代码（与开发检出的 origin 同一个 URL）。调用时读模块属性，测试可 monkeypatch。
DEFAULT_ORIGIN = "git@github.com:wangmingjie36-creator/alpha-hive-deploy.git"
#: 编排器设它为 "1" ⇒ `production_sync` 把本轮所在仓库当生产克隆核对（见模块 docstring）
PRODUCTION_CLONE_ENV = "ALPHA_HIVE_PRODUCTION_CLONE"

GUARD_TAG = "alpha-hive-production-clone-guard"
GUARD_VERSION = 1
_SUBPROC_TIMEOUT = 20
_CLONE_TIMEOUT = 600


def default_dest() -> Path:
    """生产克隆的位置。**调用时**求值（HOME 由调用方决定），不做模块级常量。

    指向代码（不是数据）：数据根是 `~/alpha-hive-data`，与它无关。放在 `~` 下而不是 `~/Desktop`：
    不受 TCC 桌面保护、不在 iCloud「桌面与文稿」同步里、路径无空格。
    """
    return Path.home() / "alpha-hive-prod"


# ─────────────────────────────────────────── 钩子正文
_WHY = ("这里是 Alpha Hive 生产克隆：代码只由扫描前的 production_sync 快进到 origin/main。\n"
        "   改代码请在开发检出（~/Desktop/Alpha Hive）开 worktree、推 origin/main——下一个扫描日自动进生产。")


def _header(name: str) -> str:
    return (f"#!/bin/sh\n"
            f"# {GUARD_TAG} v{GUARD_VERSION} {name}\n"
            f"# 由 production_clone.py 生成，勿手改：每轮扫描前 production_sync 会核对并刷新（手改的会被报 stale 并覆盖）。\n")


def _refuse(name: str, what: str) -> str:
    return _header(name) + f'cat >&2 <<\'__ALPHA_HIVE_EOF__\'\n⛔ 拒绝{what}。{_WHY}\n__ALPHA_HIVE_EOF__\nexit 1\n'


HOOKS: Dict[str, str] = {
    "pre-commit": _refuse("pre-commit", "在生产克隆里提交"),
    "pre-merge-commit": _refuse("pre-merge-commit", "在生产克隆里产生合并提交"),
    "pre-rebase": _refuse("pre-rebase", "在生产克隆里变基"),
    "pre-push": _header("pre-push") + """\
# stdin 每行：<本地 ref> <本地 sha> <远端 ref> <远端 sha>。只许推 gh-pages（report_deployer 的网站部署），且不许删除它。
rc=0
while read -r lref lsha rref rsha; do
    case "${rref}" in
        refs/heads/gh-pages)
            case "${lsha}" in
                *[!0]*) ;;
                *) echo "⛔ 生产克隆不许删除远端 gh-pages" >&2; rc=1 ;;
            esac ;;
        *) echo "⛔ 生产克隆只许推 refs/heads/gh-pages（网站部署），拒绝推 ${rref}" >&2; rc=1 ;;
    esac
done
exit ${rc}
""",
    "reference-transaction": _header("reference-transaction") + """\
# $1 = prepared / committed / aborted；只有 prepared 阶段的退出码会中止事务。
# stdin 每行：<旧值> <新值> <ref>。旧值不可信（update-ref 不带旧值时是全零，实测），只看新值。
# 新值必须是 origin/main 的祖先（含相等）。merge-base 退出码三态：0 是 / 1 否 / 其他出错——后两种都拒绝（失败即关），
# 指向符号引用（ref:...）会让 merge-base 出错，同样拒绝。
# 例外：新值全零（删除）放行。pack-refs / gc 先写 packed-refs 再删 loose ref，钩子看到的就是「main → 全零」
# （实测：不放行则 gc 永远失败）。不变式是「main 永不指向 origin/main 以外的提交」，删除不违反它；
# 真把 main 删了 ⇒ HEAD 悬空 ⇒ 下一轮 production_sync 报 error ⇒ P1。
if [ "$1" != "prepared" ]; then
    cat >/dev/null
    exit 0
fi
rc=0
while read -r old new ref; do
    [ "${ref}" = "refs/heads/main" ] || continue
    case "${new}" in
        *[!0]*) ;;
        *) continue ;;
    esac
    if git merge-base --is-ancestor "${new}" refs/remotes/origin/main 2>/dev/null; then
        continue
    fi
    echo "⛔ 拒绝把 main 移到 ${new}：它不在 origin/main 的历史里。" >&2
    echo "   生产克隆的 main 只能被快进到 origin/main（production_sync）；改代码请在开发检出的 worktree 里做。" >&2
    rc=1
done
exit ${rc}
""",
}


# ─────────────────────────────────────────── 只读体检
def _git(repo: Path, *args: str, timeout: int = _SUBPROC_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


def _config_get(repo: Path, key: str) -> Optional[str]:
    """`git config --get`：未设 ⇒ None（退出码 1）；其他失败 ⇒ 抛（别把「读不了」当「没设」）。"""
    r = _git(repo, "config", "--get", key)
    if r.returncode == 0:
        return r.stdout.strip()
    if r.returncode == 1:
        return None
    raise RuntimeError(f"git config --get {key} 失败（rc={r.returncode}）：{r.stderr.strip() or '无错误输出'}")


def _hook_state(path: Path, want: str) -> str:
    """missing / current / stale（我们生成的旧版或被手改 / 不可执行）/ foreign（不是我们生成的）。"""
    if not path.exists() and not path.is_symlink():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "foreign"
    if GUARD_TAG not in text:
        return "foreign"
    if text == want and os.access(path, os.X_OK) and not path.is_symlink():
        return "current"
    return "stale"


def _dirty_tracked(repo: Path) -> List[str]:
    """被跟踪文件的未提交改动（不含未跟踪文件）。`--no-optional-locks`：体检不许去抢索引锁。"""
    r = _git(repo, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=no")
    if r.returncode != 0:
        raise RuntimeError(f"git status 失败（rc={r.returncode}）：{r.stderr.strip() or '无错误输出'}")
    names: List[str] = []
    entries = iter(r.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        names.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            next(entries, None)   # -z 下改名 / 复制条目后面紧跟一个不带状态前缀的旧名字段
    return names


def inspect(repo, expected_origin: Optional[str] = None) -> Dict:
    """只读：这个仓库作为生产克隆合不合格。**不写任何东西**（`check` 子命令与 `ensure_guard` 共用）。"""
    repo = Path(repo)
    want_origin = expected_origin or DEFAULT_ORIGIN
    r: Dict = {"ok": False, "repo": str(repo), "standalone": None, "origin": None, "origin_ok": None,
               "hooks_path_override": None, "hooks": {}, "dirty_tracked": None, "problems": []}
    dotgit = repo / ".git"
    if not dotgit.exists():
        r["problems"].append(f"not_a_repo：{repo} 下没有 .git")
        return r
    if not dotgit.is_dir():
        r["standalone"] = False
        r["problems"].append("is_worktree：.git 是文件（这是 worktree，与开发仓库共享 refs / hooks / 索引锁）")
    else:
        wt = dotgit / "worktrees"
        linked = sorted(p.name for p in wt.iterdir()) if wt.is_dir() else []
        r["standalone"] = not linked
        if linked:
            r["problems"].append(f"has_linked_worktrees：挂着 {len(linked)} 个 worktree（这是开发仓库，不是生产克隆）")
    try:
        # v0.45.432：HEAD 必须解析成提交。空仓库 / 克隆被 SIGKILL 中断（setup 超时）留下的半截目录：
        # 没有提交、没有被跟踪文件 ⇒ 下面的工作区检查恒为空 ⇒ 此前判 ok=True（实测），次日编排器在
        # 「核心脚本不存在」处 exit 1。索引没写完的半截克隆则会在工作区检查里显成大批删除，同样红。
        h = _git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}")
        if h.returncode != 0:
            r["problems"].append("no_head：HEAD 解析不出提交（空仓库，或克隆中途被打断）")
        r["origin"] = _config_get(repo, "remote.origin.url")
        r["origin_ok"] = r["origin"] == want_origin
        if not r["origin_ok"]:
            r["problems"].append(f"origin_mismatch：origin = {r['origin']!r}，应为 {want_origin!r}")
        r["hooks_path_override"] = _config_get(repo, "core.hooksPath")
        if r["hooks_path_override"] is not None:
            r["problems"].append(f"hooks_path_override：core.hooksPath = {r['hooks_path_override']!r}，"
                                 f".git/hooks 里的守卫不会被执行")
        if r["standalone"]:
            for name, text in HOOKS.items():
                st = _hook_state(dotgit / "hooks" / name, text)
                r["hooks"][name] = st
                if st != "current":
                    r["problems"].append(f"hook_{st}：{name}")
        r["dirty_tracked"] = _dirty_tracked(repo)
        if r["dirty_tracked"]:
            r["problems"].append(f"dirty_tracked：{len(r['dirty_tracked'])} 个被跟踪文件有未提交改动"
                                 f"（扫描跑的是工作区不是 commit）：{', '.join(r['dirty_tracked'][:10])}")
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        r["problems"].append(f"error：{e}")
    r["ok"] = not r["problems"]
    return r


# ─────────────────────────────────────────── 装 / 刷新
def _write_hook(path: Path, text: str) -> None:
    """同目录临时文件 → chmod 755 → os.replace：中途失败不会留下半个钩子。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o755)
        if path.is_symlink():
            path.unlink()
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_guard(repo, expected_origin: Optional[str] = None) -> Dict:
    """补齐 / 刷新守卫钩子，返回装完之后的 `inspect` 结果 + `actions`。

    不是独立仓库、或 origin 不是 alpha-hive-deploy ⇒ 一个字节都不写（见模块 docstring「三条拒绝」）；foreign 钩子不覆盖。
    **从不抛**：任何异常都折成 `ok: False` + `problems`——调用方（扫描前同步）不能被它拖垮，但必须红。
    """
    try:
        before = inspect(repo, expected_origin)
        actions: Dict[str, str] = {}
        # v0.45.432：origin 不对也不写。此前只看 standalone ⇒ `setup --dest` 指错到别的独立仓库（如数据备份仓
        # `~/alpha-hive-data/_git_backup`）会装上「main 只许指向 origin/main 祖先」的钩子，那个仓库自己的提交从此全被拒。
        if before["standalone"] and before["origin_ok"]:
            hooks_dir = Path(repo) / ".git" / "hooks"
            for name, text in HOOKS.items():
                st = before["hooks"].get(name)
                if st in ("missing", "stale"):
                    _write_hook(hooks_dir / name, text)
                    actions[name] = "installed" if st == "missing" else "refreshed"
        after = inspect(repo, expected_origin) if actions else before
        after["actions"] = actions
        if actions:
            _log.info("生产克隆守卫：%s", ", ".join(f"{k} {v}" for k, v in actions.items()))
        if not after["ok"]:
            _log.warning("⚠️ 生产克隆不合格：%s", "；".join(after["problems"]))
        return after
    except Exception as e:  # noqa: BLE001 —— 折成 ok=False 让告警红，见 docstring
        _log.error("生产克隆守卫核对失败：%s", e)
        return {"ok": False, "repo": str(repo), "actions": {}, "problems": [f"error：{type(e).__name__}: {e}"]}


# ─────────────────────────────────────────── cutover：建克隆
def _discard_partial(dest: Path) -> str:
    """克隆失败后删掉**本次新建**的目标目录（调用方只在 dest 原本不存在时调）。

    超时走 SIGKILL，git 来不及清理 ⇒ 留下 .git 已建、HEAD 未诞生的半截目录；不删的话重跑 setup 会把它当
    已有克隆。返回拼进 detail 的说明（删不掉也要说出来，不吞）。
    """
    if not dest.exists():
        return ""
    try:
        shutil.rmtree(dest)
        return f"（已删除残留的半截目录 {dest}）"
    except OSError as e:
        return f"（⚠️ 残留的半截目录 {dest} 删不掉：{e}——重跑前手动删除）"


def setup(dest=None, origin: Optional[str] = None) -> Dict:
    """克隆到 `dest`（缺省 `~/alpha-hive-prod`）并装守卫。已存在 ⇒ 不重新克隆，只补守卫并体检。

    完整克隆（不加 `--single-branch`）：gh-pages 部署要 `refs/remotes/origin/gh-pages`。
    """
    dest = Path(dest).expanduser() if dest else default_dest()
    url = origin or DEFAULT_ORIGIN
    res: Dict = {"outcome": None, "dest": str(dest), "origin": url, "cloned": False, "detail": None, "guard": None}
    if dest.exists():
        if not (dest / ".git").is_dir():
            res.update(outcome="refused", detail=f"{dest} 已存在且不是独立 git 仓库（没有 .git 目录），不动它")
            return res
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run(["git", "clone", "--origin", "origin", "--", url, str(dest)],
                               capture_output=True, text=True, timeout=_CLONE_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            res.update(outcome="clone_failed", detail=f"{type(e).__name__}: {e}{_discard_partial(dest)}")
            return res
        if r.returncode != 0:
            res.update(outcome="clone_failed",
                       detail=(r.stderr.strip() or f"returncode={r.returncode}") + _discard_partial(dest))
            return res
        res["cloned"] = True
    res["guard"] = ensure_guard(dest, url)
    res["outcome"] = "ok" if res["guard"]["ok"] else "guard_not_ok"
    return res


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="生产代码独立克隆（阶段 8）：建克隆 / 体检")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="克隆（已存在则跳过）并装守卫钩子")
    s.add_argument("--dest", help="克隆位置（缺省 ~/alpha-hive-prod）")
    s.add_argument("--origin", help=f"远端 URL（缺省 {DEFAULT_ORIGIN}）")
    c = sub.add_parser("check", help="只读体检：独立仓库 / origin / 钩子 / 工作区")
    c.add_argument("--repo", help="仓库位置（缺省 ~/alpha-hive-prod）")
    c.add_argument("--origin", help=f"期望的远端 URL（缺省 {DEFAULT_ORIGIN}）")
    args = ap.parse_args(argv)

    if args.cmd == "setup":
        res = setup(args.dest, args.origin)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return {"ok": 0, "clone_failed": 2}.get(res["outcome"], 1)
    res = inspect(Path(args.repo).expanduser() if args.repo else default_dest(), args.origin)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
