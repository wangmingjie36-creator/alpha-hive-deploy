"""阶段 3.5 原型：编排导出 → 密钥扫描 → 提交 → 推送，落地 `status.json`。

v0.45.264 起已接入生产编排器 `alpha-hive-orchestrator.sh` Step 14
（转发调用见 `main()`）。

失败路径设计（对应硬检查项「这个失败，下游怎么知道？」）：
- git 仓库初始化失败（`backup_dir` 无 `.git` 时的 `git init` 返回非 0，或调用
  本身抛异常如超时）→ 未导出、未提交，`status.json` 记 `stage: "init"`,
  `ok: false`, 退出码 2；**必须在这里就中止**，不能带着未初始化的仓库继续往
  下走——`git diff --cached --quiet` 在非 git 仓库里返回 128（不是"无变化"的
  0），旧代码会把 128 误判成"有变化"进而尝试 commit，commit 失败后把"仓库
  根本没初始化成功"误标成 `stage: "commit"`（v0.45.278 修）。
- 导出失败（如并发写检测命中、源库不存在）→ 未提交，`status.json` 记
  `stage: "export"`, `ok: false`, 退出码 2。
- 密钥扫描命中 → 不提交，`status.json` 记 `stage: "secret_scan"`, `ok: false`，
  命中文件数与凭据来源文件名（不含值），退出码 1。
- **密钥扫描守卫未通过**（v0.45.307 补，二次检查发现）→ 同样 `stage: "secret_scan"`,
  `ok: false`, 退出码 1，但含义不同："扫了，没扫到密钥字面量"不等于"确认无密钥"——
  可能是本机一个真实密钥文件都没读到（`load_known_secrets_with_diagnostics` 返回
  空列表），也可能是某个磁盘上确实存在的凭据文件读取/解析失败（非 UTF-8/非法
  JSON）或读出来但过滤后一个字面量都没贡献。这几种情况此前会被当成"扫描通过、
  未命中"直接放行提交——等于"扫了"退化成"没扫但假装扫了"，本模块之前只有
  `scan_secrets.py::main()` CLI 入口有这道守卫，生产实际路径 `run()` 完全没有。
  `status["secret_scan"]` 记 `files_unreadable`/`files_with_zero_secrets`（只报
  文件名，不含值），供人工定位是哪个凭据文件出了问题。
- **未分类的顶层异常**（v0.45.307 补）→ `main()` 现在包一层顶层 try/except，
  兜住 `run()` 自身逻辑之外、这里没预料到的异常（比如 `_write_status` 写盘失败
  ——已经单独加了 OSError 保护，但仍防御性兜一层；或未来新代码路径漏了
  try/except）。这类异常映射到 `stage: "crash"`、**退出码 3**（不与 0/1/2/124
  中任何一个冲突——0/1/2 已被 done/secret_scan/其余五种占用，124 是编排器
  `run_step --timeout` 的超时码）。刻意不让它们以 Python 默认的退出码 1 崩溃
  退出：1 是专属 `secret_scan` 的语义，编排器 Step 14 会把任何 rc==1 无条件
  报成"🚨 密钥扫描命中，已拒绝提交"——一次纯粹的写盘异常/未知 bug 被误报成
  疑似密钥泄露，而此时数据其实可能已经提交推送成功（崩溃发生在 `_write_status`
  这类收尾步骤，不代表提交/推送失败）。rc=3 落进编排器 Step 14 现成的
  `else` 兜底分支（`⚠️ Step 14 失败（rc=X），继续进行`），不会触发误报。
- git 调用异常（`add`/`diff`/`commit`/`rev-parse`/`push` 任一步抛出 Python 异常，
  典型是 `subprocess.TimeoutExpired`——`_run_git` 传了 `timeout=60`，网络抖动
  或别的进程占着 `index.lock` 都会撞上）→ `status.json` 记 `stage: "git_error"`,
  `ok: false`, 退出码 2。**这条是 v0.45.278 新补的**：这些调用此前完全没有异常
  保护，未捕获异常会让 Python 以默认退出码 1 崩溃退出，而退出码 1 是专门留给
  `stage == "secret_scan"` 的——编排器 Step 14 会把纯粹的网络超时误报成"密钥
  扫描命中，已拒绝提交"，且这次崩溃根本没来得及写 `status.json`。
- git commit 失败（返回码非 0，不是异常）→ `status.json` 记 `stage: "commit"`,
  `ok: false`, 退出码 2。
- **`git add -A` 返回非 0（v0.45.307 修，二次检查发现）** → 同样 `stage: "git_error"`,
  `ok: false`, 退出码 2。旧代码不看 `add` 的返回码——`add` 半路被杀死（比如
  `_run_git` 的 `timeout=60` 撞上，或别的进程占着 `.git/index.lock`）会留下
  `index.lock`，之后每次调用 `git add -A` 都以 128 失败但被忽略，紧接着
  `git diff --cached --quiet` 因为什么都没暂存而返回 0（"无变化"），于是
  `git push` 空转成功，`status.json` 记 `ok: true, stage: "done"`——工作树
  实际一直带着未提交的改动，备份"永久成功"地什么都没提交。
- **提交后置条件（同上一条同一次修）**：无论走"无变化"还是"已提交"分支，
  紧接着都要 `git status --porcelain` 核实工作树确实干净；不干净说明
  add/diff/commit 某一步"看似成功但没真正生效"，同样归 `stage: "git_error"`
  而非悄悄放行——这是防御 `add` 返回码本身骗人的最后一道闸（例如 `add`
  返回 0 但因为其它原因没有真正暂存所有改动）。⚠️ 刻意不自动删
  `index.lock`——可能有另一个 git 进程真的在用它，删掉会造成更难排查的损坏。
- 推送失败（返回码非 0，不是异常；网络/权限/远端不可写）→ 已经提交到本地工作区
  （数据没丢），但 `status.json` 记 `stage: "push"`, `ok: false`, 退出码 2；
  下一轮跑仍会带着未推送的提交重试。
- 全部成功 → `status.json` 记 `ok: true`，含 commit sha、各库行数、耗时。

⚠️ 退出码 2 是 init/export/git_error/commit/push 五种失败共用的（`main()` 里
只把 `stage == "secret_scan"` 单独映射成退出码 1、`stage == "crash"` 映射成
退出码 3，其余一律 2）——下游要分辨具体是哪一种，必须读 `status.json` 的
`stage` 字段，不能只看退出码。
v0.45.269 之前编排器 Step 14 曾把 rc==2 硬解读为「已提交但推送失败」，
导致 export/commit 失败（根本没提交成功）也被日志误报成「已提交」；
Step 14 的分支现改为读 `stage` 字段而非只猜 rc；v0.45.278 起 `case` 语句
再加 `init` / `git_error` 两支，否则会落进未识别分支只报 WARN。

⚠️ 光读 `stage` 仍不够：`run_step()`（编排器里 Step 14 的启动函数）自己
也把 rc=2 当"脚本不存在，跳过"的哨兵值，跟本模块的 rc=2 撞车——若脚本
本轮根本没被调用，`status.json` 会是上一轮遗留的陈旧文件，`stage` 就是
上一轮的答案而非这一轮的。`status` 字典因此带一个 `date`（`YYYY-MM-DD`，
照抄 `scan_timing.json` 的既有约定），下游必须先核对它等于当天日期，
再信这份文件的 `stage`——v0.45.273 起编排器 Step 14 就是这么做的。

Slack 通知：按项目 CLAUDE.md「Slack 通知精简规则」，扫描失败/权重更新/数据质量
类事件本就禁止发 DM——本模块同理，失败只写 `status.json`，不发通知
（3.5 设计里写清楚了为什么）。

连续失败检测（v0.45.284）：`status.json` 每次调用整份覆盖，只反映"最近一次"，
单看它发现不了"连续多天卡在同一种失败/陈旧状态"。`run()` 因此在每条退出路径上
都额外追加一行到 `history_file`（JSONL，默认 `~/alpha-hive-data/logs/
backup_status_history.jsonl`），供 `backup_continuity.py`（照抄 `scan_continuity.py`
判 Step 10 连续性的模式）判定 Step 14 的连续性——同 `write_status`，不发 Slack、
不动 `OVERALL_STATUS`，只做本地聚合判定。
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from . import export as export_mod
from .scan_secrets import load_known_secrets_with_diagnostics, scan_directory


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60)


def run(src: Path, backup_dir: Path, remote: str = "origin", branch: str = "main",
        status_file: Path | None = None, commit_message: str | None = None,
        history_file: Path | None = None) -> dict:
    src = Path(src)
    backup_dir = Path(backup_dir)
    t0 = dt.datetime.now()
    status: dict = {"date": t0.strftime("%Y-%m-%d"), "started_at": t0.isoformat(timespec="seconds"),
                     "src": str(src), "backup_dir": str(backup_dir), "remote": remote, "branch": branch}

    if not (backup_dir / ".git").is_dir():
        try:
            init = _run_git(["init", "-b", branch], backup_dir)
        except Exception as e:  # noqa: BLE001 —— git init 异常（如超时）同样必须中止，不能带着未初始化的仓库往下走
            status.update(stage="init", ok=False, error=f"git init 异常：{e}")
            return _finish(status_file, history_file, status)
        status["git_init"] = init.returncode == 0
        if init.returncode != 0:
            status.update(stage="init", ok=False, error=init.stderr[-2000:])
            return _finish(status_file, history_file, status)

    # ── 1. 导出 ──────────────────────────────────────────────────────
    try:
        manifest = export_mod.run_export(src, backup_dir)
        export_mod.write_manifest_and_sums(backup_dir, manifest)
    except Exception as e:  # noqa: BLE001 —— 导出失败必须可见，不吞
        status.update(stage="export", ok=False, error=str(e))
        return _finish(status_file, history_file, status)
    status["manifest_summary"] = {
        db: {"tables": len(info["tables"]), "rows": sum(info["row_counts"].values())}
        for db, info in manifest["databases"].items()
    }

    # ── 2. 密钥扫描（提交前拦截，不是推送前）─────────────────────────
    secrets, secret_diag = load_known_secrets_with_diagnostics()
    guard_failed = list(secret_diag["files_unreadable"]) + list(secret_diag["files_with_zero_secrets"])
    if not secrets or guard_failed:
        # "扫描等于没扫就拒绝"守卫（v0.45.307 补，二次检查发现）：本机一个真实
        # 密钥文件都没读到，或磁盘上确实存在的某个凭据文件读取/解析失败/过滤后
        # 零贡献——这几种情况此前会被当成"扫了、没扫到"直接放行提交，实际上
        # 根本没有可比对的密钥字面量，等于压根没扫。只报文件名，不含值。
        status["secret_scan"] = {
            "known_secret_sources": sorted({n for n, _ in secrets}),
            "existing_key_files": secret_diag["existing_files"],
            "files_unreadable": secret_diag["files_unreadable"],
            "files_with_zero_secrets": secret_diag["files_with_zero_secrets"],
        }
        if not secrets:
            reason = "一个真实密钥文件都没读到，扫描等于没扫"
        else:
            reason = f"以下已存在的凭据文件未能贡献可比对的密钥字面量：{guard_failed}"
        status.update(stage="secret_scan", ok=False, error=f"密钥扫描守卫未通过：{reason}")
        return _finish(status_file, history_file, status)

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
        return _finish(status_file, history_file, status)

    # ── 3. 提交 ──────────────────────────────────────────────────────
    try:
        add = _run_git(["add", "-A"], backup_dir)
        if add.returncode != 0:
            # v0.45.307 修：旧代码不检查这个返回码——`add` 半路失败（典型是
            # 残留的 `.git/index.lock`）会让后面的 `git diff --cached --quiet`
            # 因为什么都没暂存而返回 0（"无变化"），进而整个流程"成功"地
            # 什么都没提交。必须在这里就中止，不能带着一次失败的 add 继续走
            # diff/commit（同 init 失败必须立刻中止的道理）。
            status.update(stage="git_error", ok=False,
                           error=f"git add -A 失败（returncode={add.returncode}）：{add.stderr[-2000:]}")
            return _finish(status_file, history_file, status)

        diff = _run_git(["diff", "--cached", "--quiet"], backup_dir)
        if diff.returncode == 0:
            status["commit"] = {"made": False, "reason": "无变化"}
        else:
            msg = commit_message or f"data backup {t0.strftime('%Y-%m-%d %H:%M:%S')}"
            commit = _run_git(["commit", "-m", msg], backup_dir)
            if commit.returncode != 0:
                status.update(stage="commit", ok=False, error=commit.stderr[-2000:])
                return _finish(status_file, history_file, status)
            sha = _run_git(["rev-parse", "HEAD"], backup_dir).stdout.strip()
            status["commit"] = {"made": True, "sha": sha, "message": msg}

        # 提交后置条件（v0.45.307 补）：无论走"无变化"还是"已提交"分支，工作树
        # 此刻都必须是干净的——add 返回码检查是第一道闸，这里是防御"add 返回 0
        # 但其实没把改动真正暂存全"这类更隐蔽情形的最后一道闸，不依赖任何一步
        # 具体返回码本身。
        status_check = _run_git(["status", "--porcelain"], backup_dir)
        if status_check.stdout.strip():
            dirty_n = len(status_check.stdout.strip().splitlines())
            status.update(
                stage="git_error", ok=False,
                error=f"提交（或判定无变化）之后工作树仍有 {dirty_n} 处未提交改动——"
                      f"add/diff/commit 某一步可能被吞：{status_check.stdout[:2000]}",
            )
            return _finish(status_file, history_file, status)
    except Exception as e:  # noqa: BLE001 —— add/diff/commit/rev-parse 异常（如超时）不能撞上 secret_scan 的退出码 1
        status.update(stage="git_error", ok=False, error=f"add/diff/commit 阶段异常：{e}")
        return _finish(status_file, history_file, status)

    # ── 4. 推送（本地裸仓库路径——不是 GitHub）───────────────────────
    try:
        push = _run_git(["push", remote, branch], backup_dir)
    except Exception as e:  # noqa: BLE001 —— push 异常（如超时）同上
        status.update(stage="git_error", ok=False, error=f"push 阶段异常：{e}")
        return _finish(status_file, history_file, status)
    if push.returncode != 0:
        status.update(stage="push", ok=False, error=push.stderr[-2000:])
        return _finish(status_file, history_file, status)

    status.update(stage="done", ok=True,
                   duration_seconds=round((dt.datetime.now() - t0).total_seconds(), 1))
    return _finish(status_file, history_file, status)


def _write_status(status_file: Path | None, status: dict) -> None:
    """写 `status.json`。判定（`status` dict）此刻已经算完——写盘失败不能让
    整个 `run()` 以未分类异常向上爆炸（v0.45.307 修，二次检查发现）：旧代码
    这里没有异常保护，`status_file` 指向一个不合法路径（比如父目录其实是个
    文件）时 `mkdir`/`write_text` 会抛 `NotADirectoryError`，一路不捕获地让
    `main()` 以 Python 默认退出码 1 崩溃退出——而退出码 1 是专属 `secret_scan`
    的，编排器 Step 14 会把这种纯粹的写盘异常误报成"🚨 密钥扫描命中，已拒绝
    提交"，此时数据可能其实早已提交推送成功（崩溃发生在收尾这一步，不代表
    提交/推送失败）。同 `_append_history` 已有的纪律：写不进去不改变判定结果，
    只打印到 stderr 让人能看见。
    """
    if status_file is None:
        return
    status_file = Path(status_file)
    try:
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"⚠️ 无法写入 status_file {status_file}: {e}", file=sys.stderr)


def _append_history(history_file: Path | None, status: dict) -> None:
    """把本次结果追加一行到 JSONL 历史日志（v0.45.284）。

    `status_file` 每次调用整份覆盖，只反映"最近一次"——`backup_continuity.py`
    判定"连续 N 天未识别/陈旧"需要跨天累积的记录，同 `weekly_optimizer.py` 的
    `weight_history.jsonl` 审计日志同一模式：只追加，不覆盖，写不进去不影响
    本次结果（判定早已完成）。
    """
    if history_file is None:
        return
    record = {"date": status.get("date"), "stage": status.get("stage"), "ok": bool(status.get("ok"))}
    history_file = Path(history_file)
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 追加失败不改变本次结果——判定在写盘之前就已完成


def _finish(status_file: Path | None, history_file: Path | None, status: dict) -> dict:
    _write_status(status_file, status)
    _append_history(history_file, status)
    return status


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="阶段 3.5 原型：导出→扫描→提交→推送")
    ap.add_argument("--src", default="/Users/igg/Desktop/Alpha Hive")
    ap.add_argument("--backup-dir", default=str(Path.home() / "alpha-hive-data" / "_git_backup"))
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--status-file", default=str(Path.home() / "alpha-hive-data" / "logs" / "backup_status.json"))
    ap.add_argument("--history-file",
                    default=str(Path.home() / "alpha-hive-data" / "logs" / "backup_status_history.jsonl"))
    ap.add_argument("--message", default=None)
    args = ap.parse_args(argv)

    try:
        status = run(Path(args.src), Path(args.backup_dir), args.remote, args.branch,
                     Path(args.status_file), args.message, Path(args.history_file))
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status.get("ok") else (1 if status.get("stage") == "secret_scan" else 2)
    except Exception as e:  # noqa: BLE001 —— 顶层兜底（v0.45.307 补，二次检查发现）：
        # run() 内部各阶段早已各自兜过 Exception；这里只剩两类会漏到此处——
        # ① run() 自身逻辑有 bug、漏了某条新代码路径的 try/except；
        # ② 参数构造阶段本身出意外（理论上不会，防御性写）。
        # 不能让它们以 Python 默认退出码 1 崩溃退出——1 是专属 stage="secret_scan"
        # 的语义，编排器 Step 14 会把任意 rc==1 无条件报成"🚨 密钥扫描命中，
        # 已拒绝提交"，而此时数据实际状态未知（可能早已提交推送成功）。
        # 用 3：不与 0/1/2（done/secret_scan/其余五种）、124（run_step 超时）
        # 中任何一个冲突，落进编排器 Step 14 现成的通用 else 分支
        # （"⚠️ Step 14 失败（rc=X），继续进行"），不会触发误报。
        crash_status = {
            "date": dt.datetime.now().strftime("%Y-%m-%d"),
            "stage": "crash", "ok": False,
            "error": f"main() 顶层捕获未分类异常：{type(e).__name__}: {e}",
        }
        _write_status(Path(args.status_file), crash_status)  # 内部已有 OSError 保护，不会再抛
        print(json.dumps(crash_status, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
