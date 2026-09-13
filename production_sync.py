"""生产 checkout 与 origin/main 的同步（v0.45.214）

两个入口，分别在一轮扫描的两头：

  扫描前 `sync_before_scan`（编排器在 Step 1 之前调本文件 CLI）：
      把生产 checkout 的本地 main **只快进**到 origin/main。
  部署时 `push_main`（`report_deployer.auto_commit_and_notify` 调）：
      推日报提交；本地 main 落后时在**对象层**合并后再推——不动工作区、不动本地 ref。

为什么要有这个文件
------------------
2026-08-14 ~ 09-13 生产推送 12 次，6 次（09-01/03/04/09/10/11）被拒
`! [rejected] main -> main (non-fast-forward)`。逐次核对 origin/main 的 tracking-ref
reflog（各 worktree 共享）与生产 main 的 reflog：被拒的 6 次，生产 main 在报告提交时
都**严格落后**（behind 1~15、ahead 0）；成功的 2 次 behind=0。8/8 对上。
成因：各 session 从 worktree 直推 origin/main，生产 checkout 从不自动 pull。

后果分三层，最显眼的那层反而最轻：
  1. 网站不受影响——gh-pages 走 plumbing + `--force`，六天全部部署成功。
  2. 日报与账本的异地副本，要靠「碰巧有 session 在生产 checkout 里 rebase/merge 再推」
     才落到 origin/main：6 次里 3 次是以 rebase 改写后的副本落地；09-11 那次滞留本地两天，
     而 `hedge_state/` 等账本的 09-11 更新**只**在那个本地提交里。
  3. **生产跑哪版代码是随机的**：取决于哪个 session 最后一次手工快进了它（09-11 当天
     手工移动 23 次）。世代边界按 `date >= boundary` 过滤，前提是「代码落地当天就在跑」。
     实测反例：v0.45.209 边界日期 2026-09-11，修复 13:37 落地，生产 13:10 同步过，
     14:00 的扫描没带上它 ⇒ 当时世代内全部 30 条样本出自旧代码。

而**没有任何东西会红**：`alert_manager` 那条「GitHub Deployment Failed」读的
`deploy_status` 全仓零写入者，结构上不可能触发。

两条设计约束（各有一次实测）
------------------------------
1. **部署时不许改工作区。** 部署之后编排器还要起约 8 个 Python 步骤（告警、Slack、
   metrics、recorder…）。部署时 `pull` 会让它们跑比扫描更新的代码 ⇒ 一轮混两个版本，
   `code_version` 记的也不再是真跑的那版。所以用 `merge-tree --write-tree` +
   `commit-tree` 在对象层造合并（第一父 = origin/main），推 `<合并>:main`。
   本地 main 不动 ⇒ 仍是 origin/main 的祖先 ⇒ 下一轮扫描前可以快进。
2. **报告提交必须在本地做。** 若只在对象层发布、本地 HEAD 不含已发布的内容，工作区相对
   HEAD 就是脏的，`merge --ff-only` 会以「would be overwritten」拒绝——即使内容与
   origin/main 逐字节相同（另一 session 用临时仓库实测）⇒ 扫描前快进从此永远卡住。
   `report_deployer` 照旧先在本地提交，本文件只负责把它送上去。

`merge-base --is-ancestor` 与 `merge-tree --write-tree` 都有**三**个退出码：
0 / 1 是两种正常答案，其他是真出错。三者诊断方向不同，不许揉成两个
（同 `git check-ignore` 0/1/128 的教训）。`merge-tree --write-tree` 需要 git ≥ 2.38，
生产 launchd PATH 下是 /usr/bin/git 2.50.1。

⚠️ 本文件永不下发 reset / checkout / rebase / stash / merge：扫描前只 `pull --ff-only
--no-rebase`，做不到快进就保持现有代码、把原因写进结果，扫描照跑。
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from hive_logger import PATHS, get_logger

_log = get_logger("production_sync")

REMOTE_REF = "refs/remotes/origin/main"

#: 扫描前同步里算「生产代码 == origin/main」的结局；其余一律要红（alert_manager）
OK_OUTCOMES = frozenset({"up_to_date", "fast_forwarded"})


# ─────────────────────────────────────────── git 小工具
def _reason(r: Dict) -> str:
    """`run_git_cmd` 两种失败形状（stderr / error）的原因；都没有就报退出码。"""
    return ((r.get("stderr") or "").strip() or (r.get("error") or "").strip()
            or f"returncode={r.get('returncode')}，git 无错误输出")


def _rev(git, ref: str) -> Optional[str]:
    r = git.run_git_cmd(f"git rev-parse --verify {ref}")
    return r["stdout"].strip() if r["success"] and r.get("stdout", "").strip() else None


def _is_ancestor(git, a: str, b: str) -> Optional[bool]:
    """a 是否为 b 的祖先（含 a == b）。退出码 0=是，1=否，其他（含白名单拒绝）⇒ None。"""
    rc = git.run_git_cmd(f"git merge-base --is-ancestor {a} {b}").get("returncode")
    return {0: True, 1: False}.get(rc)


def _count(git, rev_range: str) -> Optional[int]:
    r = git.run_git_cmd(f"git rev-list --count {rev_range}")
    try:
        return int(r["stdout"].strip()) if r["success"] else None
    except (KeyError, ValueError):
        return None


# ─────────────────────────────────────────── 部署时：推 main
def push_main(git, merge_label: str, max_attempts: int = 3) -> Dict:
    """把本地 main 推上 origin/main；本地落后时先在对象层合并再推。

    返回 `results["git_push"]` 的形状——v0.45.210 的四个键不变，另加：
      integration   "fast_forward" | "merged" | "conflict" | "error" | "unchecked"
      behind        本地 main 落后 origin/main 的提交数（取不到为 None）
      merge_commit  造出来的合并提交（仅 merged，推送失败也保留，便于排查）
      conflicts     冲突路径（仅 conflict；此时**不推送**）
      attempts      第几轮结束。推送被拒后**只有 origin/main 在这期间又动过**才重来——
                    没动就是别的原因（hook、权限），同样的推送只会同样被拒。
    """
    res = {"success": False, "remote": "origin", "output": "", "error": None,
           "integration": None, "behind": None, "merge_commit": None,
           "conflicts": None, "attempts": 0}
    last_origin: Optional[str] = None
    for attempt in range(1, max_attempts + 1):
        f = git.run_git_cmd("git fetch origin main")
        if not f["success"]:
            if last_origin is not None:
                break   # 已经推过一轮被拒，现在连 origin 都看不到：报上一轮的拒绝原因
            # 看不到 origin 就判断不了落没落后：退回 v0.45.210 的直推，由 git 自己拒非快进
            res["attempts"] = attempt
            p = git.run_git_cmd("git push origin main")
            res.update(success=p["success"], integration="unchecked",
                       output=p.get("stdout", "") or p.get("stderr", ""), error=p.get("error"),
                       fetch_error=_reason(f))
            return res
        # 先把两个 ref 解析成 SHA 再用：origin/main 的 tracking ref 被所有 worktree 共享，
        # 别的 session 一推它就会动，合并的父提交必须是刚才看过的那个。
        head, origin = _rev(git, "refs/heads/main"), _rev(git, REMOTE_REF)
        if head is None or origin is None:
            return _push_fail(res, "error", "解析不出 refs/heads/main 或 origin/main")
        if origin == last_origin:
            break       # 上一轮被拒、origin/main 没动 ⇒ 不是竞态，别重试
        res["attempts"] = attempt
        anc = _is_ancestor(git, origin, head)
        if anc is None:
            return _push_fail(res, "error",
                              f"merge-base --is-ancestor 出错（{origin[:7]} → {head[:7]}）")
        if anc:
            target, res["integration"], res["behind"] = head, "fast_forward", 0
        else:
            res["behind"] = _count(git, f"{head}..{origin}")
            mt = git.run_git_cmd(f"git merge-tree --write-tree --name-only {origin} {head}")
            rc, lines = mt.get("returncode"), (mt.get("stdout") or "").splitlines()
            if rc == 1:
                conflicts = []
                for ln in lines[1:]:          # 第 1 行是树，其后到空行为止是冲突路径
                    if not ln.strip():
                        break
                    conflicts.append(ln.strip())
                res["conflicts"] = conflicts
                return _push_fail(res, "conflict",
                                  "本地 main 与 origin/main 冲突，未推送（生产 checkout 需人工合并）："
                                  + (", ".join(conflicts) or "（git 未列出路径）"))
            if rc != 0 or not lines:
                return _push_fail(res, "error",
                                  f"git merge-tree 出错（returncode={rc}）：{_reason(mt)}")
            msg = (f"Merge origin/main into {merge_label}"
                   f"（部署时对象层合并：本地 main 落后 {res['behind']} 个提交，v0.45.214）")
            ct = git.run_git_cmd(
                f"git commit-tree {lines[0].strip()} -p {origin} -p {head} -m {shlex.quote(msg)}")
            if not ct["success"] or not ct.get("stdout", "").strip():
                return _push_fail(res, "error", f"git commit-tree 失败：{_reason(ct)}")
            target = ct["stdout"].strip()
            res["integration"], res["merge_commit"] = "merged", target

        p = git.run_git_cmd(f"git push origin {target}:refs/heads/main")
        res.update(success=p["success"], output=p.get("stdout", "") or p.get("stderr", ""),
                   error=p.get("error"))
        if p["success"]:
            return res
        last_origin = origin
    return res


def _push_fail(res: Dict, integration: str, error: str) -> Dict:
    res.update(success=False, integration=integration, error=error)
    return res


# ─────────────────────────────────────────── 扫描前：只快进
def sync_before_scan(git, today: Optional[str] = None) -> Dict:
    """把生产 checkout 的本地 main 只快进到 origin/main。

    结局 `outcome`：
      up_to_date / fast_forwarded   生产代码 == origin/main（`OK_OUTCOMES`）
      ff_refused    git 拒绝快进（多为工作区有未提交改动、恰好被 origin 改过），detail 为原话
      local_ahead   本地 main 含 origin/main 没有的提交 ⇒ 生产跑的不是 main
      diverged      两边各有对方没有的提交
      not_on_main   生产 checkout 不在 main 分支上，不动它
      fetch_failed / error
    """
    now = datetime.now()
    res = {"date": today or now.strftime("%Y-%m-%d"),
           "checked_at": now.isoformat(timespec="seconds"),
           "outcome": None, "before": None, "after": None, "origin": None,
           "behind": None, "ahead": None, "detail": None}

    br = git.run_git_cmd("git rev-parse --abbrev-ref HEAD")
    if not br["success"]:
        return _done(res, "error", f"git rev-parse 失败：{_reason(br)}")
    branch = br["stdout"].strip()
    if branch != "main":
        return _done(res, "not_on_main", f"生产 checkout 当前在 {branch!r} 上，未动它")
    f = git.run_git_cmd("git fetch origin main")
    if not f["success"]:
        return _done(res, "fetch_failed", _reason(f))

    head, origin = _rev(git, "HEAD"), _rev(git, REMOTE_REF)
    res.update(before=head, origin=origin, after=head)
    if head is None or origin is None:
        return _done(res, "error", "解析不出 HEAD 或 origin/main")
    if head == origin:
        res.update(behind=0, ahead=0)
        return _done(res, "up_to_date", None)
    res.update(behind=_count(git, f"{head}..{origin}"), ahead=_count(git, f"{origin}..{head}"))

    ff = _is_ancestor(git, head, origin)
    if ff is None:
        return _done(res, "error", "merge-base --is-ancestor 出错")
    if not ff:
        back = _is_ancestor(git, origin, head)
        if back is None:
            return _done(res, "error", "merge-base --is-ancestor 出错")
        if back:
            return _done(res, "local_ahead",
                         f"本地 main 领先 origin/main {res['ahead']} 个提交（未推送），生产跑的不是 main")
        return _done(res, "diverged",
                     f"本地 main 与 origin/main 分叉（ahead {res['ahead']} / behind {res['behind']}）")

    p = git.run_git_cmd("git pull --ff-only --no-rebase origin main")
    after = _rev(git, "HEAD")
    res["after"] = after
    if not p["success"]:
        return _done(res, "ff_refused", _reason(p))
    # pull 自己会再 fetch 一次，可能快进到比刚才更新的提交；但至少得包含刚才看到的那个
    if after is None or not _is_ancestor(git, origin, after):
        return _done(res, "error", f"pull 报成功，但 HEAD {after} 不含 origin/main {origin[:7]}")
    return _done(res, "fast_forwarded", f"{head[:7]} → {after[:7]}")


def _done(res: Dict, outcome: str, detail: Optional[str]) -> Dict:
    res.update(outcome=outcome, detail=detail)
    if outcome in OK_OUTCOMES:
        _log.info("生产代码同步：%s %s", outcome, detail or "")
    else:
        _log.warning("⚠️ 生产代码未同步到 origin/main（本轮沿用现有代码）：%s — %s",
                     outcome, detail or "（无详情）")
    return res


# ─────────────────────────────────────────── 结果落盘（→ scan_timing → status.json）
def write_result(res: Dict, path: Optional[Path] = None) -> Optional[Path]:
    """原子写。写不出来只记 warning——观测文件不能拖垮扫描，但缺失会让告警侧报「未执行」。"""
    target = Path(path) if path else PATHS.production_sync
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except OSError as e:
        _log.warning("production_sync 结果写入失败：%s", e)
        return None
    return target


def load_for_date(date_str: str, path: Optional[Path] = None) -> Optional[Dict]:
    """本轮的同步结果；文件不存在、坏了、或是别的日期的 ⇒ None（「没测到」，不是「没问题」）。"""
    target = Path(path) if path else PATHS.production_sync
    try:
        with open(target, encoding="utf-8") as fh:
            res = json.load(fh)
    except (OSError, ValueError):
        return None
    return res if isinstance(res, dict) and res.get("date") == date_str else None


def main(argv=None) -> int:
    """编排器入口。退出码 0 = 生产代码已是 origin/main；1 = 没同步上（扫描照跑，结果已落盘）。"""
    import argparse

    ap = argparse.ArgumentParser(description="扫描前把生产 checkout 快进到 origin/main")
    ap.add_argument("--date", help="本轮日期（编排器传 $DATE_STR），与 scan_timing 的日期对齐")
    args = ap.parse_args(argv)

    from agent_toolbox import GitHubTool   # 与 report_deployer 推送用的是同一个仓库路径解析
    res = sync_before_scan(GitHubTool(), today=args.date)
    written = write_result(res)
    print(f"production_sync: {res['outcome']} | before={(res['before'] or '')[:7]} "
          f"after={(res['after'] or '')[:7]} origin={(res['origin'] or '')[:7]} "
          f"behind={res['behind']} ahead={res['ahead']} | {res['detail'] or ''} | → {written}")
    return 0 if res["outcome"] in OK_OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(main())
