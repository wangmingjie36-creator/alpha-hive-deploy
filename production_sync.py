"""生产 checkout 与 origin/main 的同步（v0.45.214；v0.45.402 起只剩扫描前快进）

入口：`sync_before_scan`（编排器在 Step 1 之前调本文件 CLI）——把生产 checkout 的本地 main
**只快进**到 origin/main。

为什么要有这个文件
------------------
2026-08-14 ~ 09-13 生产推送 12 次，6 次被拒 `non-fast-forward`：各 session 从 worktree 直推
origin/main，生产 checkout 从不自动 pull。更要紧的是**生产跑哪版代码是随机的**——取决于哪个 session
最后一次手工快进了它（09-11 当天手工移动 23 次）。世代边界按 `date >= boundary` 过滤，前提是
「代码落地当天就在跑」；实测反例：v0.45.209 边界日期 2026-09-11，修复 13:37 落地，生产 13:10
同步过，14:00 的扫描没带上它 ⇒ 世代内全部 30 条样本出自旧代码。
而没有任何东西会红（旧告警读的 `deploy_status` 全仓零写入者）。

**v0.45.402 退役了 `push_main`**（部署时把日报提交在对象层合并后推 main）：数据根迁移阶段 6 起
代码仓库不再跟踪生产数据，日报不再提交 / 推送，那条推送链连同它的两条设计约束（部署时不许改工作区、
报告提交必须在本地做）一起消失。⚠️ 勿重建。快进本身的约束仍然成立：

⚠️ 本文件永不下发 reset / checkout / rebase / stash / merge：扫描前只 `pull --ff-only
--no-rebase`，做不到快进就保持现有代码、把原因写进结果，扫描照跑。
阶段 6 的提示：解除跟踪的提交快进进来时，git 会把那些文件从工作区**删掉**；工作区里有「被跟踪且已
修改」的文件则快进**中止**（outcome 非 OK，`alert_manager` 会红）——不要在生产 checkout 里手改被跟踪文件。

数据根迁移阶段 8（v0.45.431）：生产代码换成独立克隆 `~/alpha-hive-prod`。编排器设 `ALPHA_HIVE_PRODUCTION_CLONE=1`
时，`main()` 在快进**之前**调 `production_clone.ensure_guard`（钩子拒绝一切让 main 偏离 origin/main 的写入），
结局写进结果的 `clone_guard`。没设就与此前完全相同（切换前 / 回退后）。设计与两条拒绝见 `production_clone` docstring。

`merge-base --is-ancestor` 的退出码有**三**个含义：0 / 1 是两种正常答案，其他是真出错。
不许揉成两个（同 `git check-ignore` 0/1/128 的教训）。
"""

from __future__ import annotations

import json
import os
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
    git = GitHubTool()
    # 阶段 8（v0.45.431）：编排器声明「这里应是生产克隆」⇒ 快进**之前**补齐 / 核对守卫（钩子先于本轮 pull 生效，
    # pull 本身就是对 reference-transaction 钩子的一次正对照：钩子误拦快进 ⇒ ff_refused ⇒ 既有 P1）。
    # 用环境变量不用 CLI 参数：不认识它的旧代码会忽略、照常快进，不会因 argparse 报错卡在旧提交上互锁。
    # ensure_guard 从不抛；结局与快进互不影响（守卫不合格也照常同步、照常扫描），各自红各自的。
    guard = None
    import production_clone
    if os.environ.get(production_clone.PRODUCTION_CLONE_ENV) == "1":
        guard = production_clone.ensure_guard(git.repo_path)
    res = sync_before_scan(git, today=args.date)
    if guard is not None:
        res["clone_guard"] = guard
    written = write_result(res)
    print(f"production_sync: {res['outcome']} | before={(res['before'] or '')[:7]} "
          f"after={(res['after'] or '')[:7]} origin={(res['origin'] or '')[:7]} "
          f"behind={res['behind']} ahead={res['ahead']} | {res['detail'] or ''} | → {written}")
    if guard is not None:
        print(f"production_clone: {'ok' if guard.get('ok') else 'NOT OK'} | actions={guard.get('actions') or {}} | "
              f"{'；'.join(guard.get('problems') or []) or '—'}")
    return 0 if res["outcome"] in OK_OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(main())
