"""阶段 3.5 原型：编排导出 → 密钥扫描 → 提交 → 推送，落地 `status.json`。

v0.45.264 起已接入生产编排器 `alpha-hive-orchestrator.sh` Step 14
（转发调用见 `main()`）。

失败路径设计（对应硬检查项「这个失败，下游怎么知道？」）：
- 密钥扫描命中 → 不提交，`status.json` 记 `stage: "secret_scan"`, `ok: false`，
  命中文件数与凭据来源文件名（不含值），退出码 1。
- 导出失败（如并发写检测命中、源库不存在）→ 未提交，`status.json` 记
  `stage: "export"`, `ok: false`, 退出码 2。
- git commit 失败 → `status.json` 记 `stage: "commit"`, `ok: false`, 退出码 2。
- 推送失败（网络/权限/远端不可写）→ 已经提交到本地工作区（数据没丢），
  但 `status.json` 记 `stage: "push"`, `ok: false`, 退出码 2；
  下一轮跑仍会带着未推送的提交重试。
- 全部成功 → `status.json` 记 `ok: true`，含 commit sha、各库行数、耗时。

⚠️ 退出码 2 是 export/commit/push 三种失败共用的（`main()` 里只把
`stage == "secret_scan"` 单独映射成退出码 1，其余一律 2）——下游要分辨
具体是哪一种，必须读 `status.json` 的 `stage` 字段，不能只看退出码。
v0.45.269 之前编排器 Step 14 曾把 rc==2 硬解读为「已提交但推送失败」，
导致 export/commit 失败（根本没提交成功）也被日志误报成「已提交」；
Step 14 的分支现改为读 `stage` 字段而非只猜 rc。

⚠️ 光读 `stage` 仍不够：`run_step()`（编排器里 Step 14 的启动函数）自己
也把 rc=2 当"脚本不存在，跳过"的哨兵值，跟本模块的 rc=2 撞车——若脚本
本轮根本没被调用，`status.json` 会是上一轮遗留的陈旧文件，`stage` 就是
上一轮的答案而非这一轮的。`status` 字典因此带一个 `date`（`YYYY-MM-DD`，
照抄 `scan_timing.json` 的既有约定），下游必须先核对它等于当天日期，
再信这份文件的 `stage`——v0.45.273 起编排器 Step 14 就是这么做的。

Slack 通知：按项目 CLAUDE.md「Slack 通知精简规则」，扫描失败/权重更新/数据质量
类事件本就禁止发 DM——本模块同理，失败只写 `status.json`，不发通知
（3.5 设计里写清楚了为什么）。
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from . import export as export_mod
from .scan_secrets import load_known_secrets, scan_directory


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


def run(src: Path, backup_dir: Path, remote: str = "origin", branch: str = "main",
        status_file: Path | None = None, commit_message: str | None = None) -> dict:
    src = Path(src)
    backup_dir = Path(backup_dir)
    t0 = dt.datetime.now()
    status: dict = {"date": t0.strftime("%Y-%m-%d"), "started_at": t0.isoformat(timespec="seconds"),
                     "src": str(src), "backup_dir": str(backup_dir), "remote": remote, "branch": branch}

    if not (backup_dir / ".git").is_dir():
        init = _run_git(["init", "-b", branch], backup_dir)
        status["git_init"] = init.returncode == 0

    # ── 1. 导出 ──────────────────────────────────────────────────────
    try:
        manifest = export_mod.run_export(src, backup_dir)
        export_mod.write_manifest_and_sums(backup_dir, manifest)
    except Exception as e:  # noqa: BLE001 —— 导出失败必须可见，不吞
        status.update(stage="export", ok=False, error=str(e))
        _write_status(status_file, status)
        return status
    status["manifest_summary"] = {
        db: {"tables": len(info["tables"]), "rows": sum(info["row_counts"].values())}
        for db, info in manifest["databases"].items()
    }

    # ── 2. 密钥扫描（提交前拦截，不是推送前）─────────────────────────
    secrets = load_known_secrets()
    scan = scan_directory(backup_dir, secrets)
    status["secret_scan"] = {
        "files_scanned": scan["files_scanned"],
        "known_secret_sources": sorted({n for n, _ in secrets}),
        "hit_count": len(scan["hits"]),
        "hit_files": [h["file"] for h in scan["hits"]],  # 只报文件名，不含值
    }
    if scan["hits"]:
        status.update(stage="secret_scan", ok=False,
                       error=f"命中 {len(scan['hits'])} 处，已拒绝提交")
        _write_status(status_file, status)
        return status

    # ── 3. 提交 ──────────────────────────────────────────────────────
    _run_git(["add", "-A"], backup_dir)
    diff = _run_git(["diff", "--cached", "--quiet"], backup_dir)
    if diff.returncode == 0:
        status["commit"] = {"made": False, "reason": "无变化"}
    else:
        msg = commit_message or f"data backup {t0.strftime('%Y-%m-%d %H:%M:%S')}"
        commit = _run_git(["commit", "-m", msg], backup_dir)
        if commit.returncode != 0:
            status.update(stage="commit", ok=False, error=commit.stderr[-2000:])
            _write_status(status_file, status)
            return status
        sha = _run_git(["rev-parse", "HEAD"], backup_dir).stdout.strip()
        status["commit"] = {"made": True, "sha": sha, "message": msg}

    # ── 4. 推送（本地裸仓库路径——不是 GitHub）───────────────────────
    push = _run_git(["push", remote, branch], backup_dir)
    if push.returncode != 0:
        status.update(stage="push", ok=False, error=push.stderr[-2000:])
        _write_status(status_file, status)
        return status

    status.update(stage="done", ok=True,
                   duration_seconds=round((dt.datetime.now() - t0).total_seconds(), 1))
    _write_status(status_file, status)
    return status


def _write_status(status_file: Path | None, status: dict) -> None:
    if status_file is None:
        return
    status_file = Path(status_file)
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="阶段 3.5 原型：导出→扫描→提交→推送")
    ap.add_argument("--src", default="/Users/igg/Desktop/Alpha Hive")
    ap.add_argument("--backup-dir", default=str(Path.home() / "alpha-hive-data" / "_git_backup"))
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--status-file", default=str(Path.home() / "alpha-hive-data" / "logs" / "backup_status.json"))
    ap.add_argument("--message", default=None)
    args = ap.parse_args(argv)

    status = run(Path(args.src), Path(args.backup_dir), args.remote, args.branch,
                 Path(args.status_file), args.message)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status.get("ok") else (1 if status.get("stage") == "secret_scan" else 2)


if __name__ == "__main__":
    sys.exit(main())
