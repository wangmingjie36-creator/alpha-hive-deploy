"""把仓库里的编排器部署到 launchd 执行的位置（v0.45.356，编排器纳入版本控制·阶段 2）

  仓库 scripts/alpha-hive-orchestrator.sh（唯一真相） ──本工具──▶ ~/.claude/scripts/alpha-hive-orchestrator.sh

为什么有这个文件
----------------
v0.45.353 起编排器受版本控制，但 launchd 仍执行 `~/.claude/scripts/` 下的部署副本
（不用软链接：launchd 下 bash 对 `~/Desktop` 有 TCC 限制，见编排器 production_sync 段注释）。
部署这一步是**唯一能拦住坏版本的时刻**——软链接没有这个时刻，一保存就生效。
阶段 2 只提供工具与手动入口；阶段 3 才把它接进扫描前同步之后。

部署什么：**永远是 git 里的一个 blob**（`--ref` 指定，必填），从不读工作区文件
⇒ 未提交的改动、生产 checkout 里的脏文件，都到不了 launchd。

关卡（全部作用在**将要写入的那几个字节**上，任一不过 ⇒ 不部署、保留现有副本）：
  1. 来源在 main 上：该 blob 必须出现在 `--main-ref`（默认 origin/main）的该文件历史里。
     按 blob 判而不按提交判：生产 checkout 可能带着一个没推上去的日报提交（09-25 分叉），
     那时 HEAD 不是 origin/main 的祖先，但编排器内容仍是 main 上的某一版。
  2. 形状：含 `set -uo pipefail`、行数 > 500（空文件 / 别的脚本不许冒充）。
  3. `bash -n` 语法检查（用 `/bin/bash`，即 launchd 实际用的 3.2）。
  4. 裸 `$VAR` 紧跟非 ASCII（`orchestrator_lint.find_unbraced`，v0.45.348 那类）。

漂移（现有部署副本不是 main 历史里的任何一版 = 有人绕过仓库直接改了生产）：
  默认**拒绝覆盖**（退出码 2）——覆盖会静默冲掉那次热修复。先把改动搬进仓库、合入，再部署；
  确认要丢弃时加 `--accept-drift`，此时**先备份**再覆盖。
  不需要状态文件：「在不在 git 历史里」本身就是判据。同理，非漂移时**不写 .bak**——
  被覆盖的那版就在 git 里，git 就是备份。

写入：同目录临时文件 → 关卡 → chmod 755 → fsync → `os.replace`（新 inode）。
正在运行的编排器握着旧 inode，读完旧版不受影响（2026-09-27 临时仓库实测 git ff 同理）。

退出码（写 `except` 前先问「谁会红」：每种失败都有自己的码与 outcome，不揉）：
  0  deployed / already_current（dry-run 时为 would_deploy）
  1  refused_gate   —— 候选版本没过关卡（不在 main 上 / 形状 / 语法 / 裸变量）
  2  refused_drift  —— 现有部署副本被手改过
  3  error          —— 判定不了（git 失败、ref 不存在、读写异常）

结果：stdout 一行 JSON；`--out` 另写一份（**无默认路径**：调用方显式给，
免得凭空多一个要登记进 `PATHS` 的产物）。

手动部署（阶段 3 之前，从任一 worktree）：
  /usr/local/bin/python3 deploy_orchestrator.py --ref origin/main --dry-run
  /usr/local/bin/python3 deploy_orchestrator.py --ref origin/main
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from orchestrator_lint import find_unbraced

REL_PATH = "scripts/alpha-hive-orchestrator.sh"
DEFAULT_MAIN_REF = "origin/main"
BASH = "/bin/bash"          # launchd plist 用的就是它（3.2）；关卡要测真执行者
_NULL_BLOB = "0" * 40

OK_OUTCOMES = frozenset({"deployed", "already_current", "would_deploy"})
_EXIT = {"deployed": 0, "already_current": 0, "would_deploy": 0,
         "refused_gate": 1, "refused_drift": 2, "error": 3}


class _GitError(RuntimeError):
    pass


def _repo_root() -> Path:
    """代码锚点：本文件所在的仓库检出（CLAUDE.md「指向代码还是数据」——代码，用 `__file__`）。"""
    return Path(__file__).resolve().parent


def default_dest() -> Path:
    """launchd 执行的部署副本。**调用时**求值，不做模块级常量。"""
    return Path.home() / ".claude" / "scripts" / "alpha-hive-orchestrator.sh"


def _git(repo: Path, *args: str, stdin: Optional[bytes] = None) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), *args], input=stdin,
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        raise _GitError(f"git {' '.join(args)} 失败（rc={r.returncode}）："
                        f"{r.stderr.decode(errors='replace').strip() or '无错误输出'}")
    return r.stdout


def blob_of(repo: Path, data: bytes) -> str:
    """内容的 git blob id（`hash-object --stdin`，不加 -w：不往对象库写任何东西）。"""
    return _git(repo, "hash-object", "--stdin", stdin=data).decode().strip()


def main_history_blobs(repo: Path, main_ref: str) -> set:
    raw = _git(repo, "log", "--no-abbrev", "--raw", "--format=", main_ref, "--", REL_PATH).decode()
    blobs = {ln.split()[3] for ln in raw.splitlines() if ln.startswith(":")}
    blobs.discard(_NULL_BLOB)
    return blobs


def gate_failures(data: bytes, tmp_file: Path) -> List[str]:
    """候选内容的形状 / 语法 / 裸变量关卡。`tmp_file` 必须已写入 `data`（`bash -n` 测的是那个文件）。"""
    fails: List[str] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"不是合法 UTF-8：{e}"]
    if "set -uo pipefail" not in text or text.count("\n") <= 500:
        fails.append("形状不像编排器（缺 `set -uo pipefail` 或不足 500 行）")
    r = subprocess.run([BASH, "-n", str(tmp_file)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        fails.append(f"bash -n 失败：{r.stderr.strip()[:500]}")
    for no, name, line in find_unbraced(text):
        fails.append(f"第 {no} 行裸变量 ${name} 紧跟非 ASCII（改成 ${{{name}}}）：{line.strip()[:120]}")
    return fails


def deploy(ref: str, *, repo: Optional[Path] = None, dest: Optional[Path] = None,
           main_ref: str = DEFAULT_MAIN_REF, dry_run: bool = False,
           accept_drift: bool = False) -> Dict:
    repo = repo or _repo_root()
    dest = dest or default_dest()
    res: Dict = {"outcome": None, "ref": ref, "main_ref": main_ref, "dest": str(dest),
                 "candidate_blob": None, "previous_blob": None, "commit": None,
                 "gate_failures": [], "drift": None, "backup": None,
                 "dry_run": dry_run, "detail": None,
                 "at": datetime.now().isoformat(timespec="seconds")}

    def done(outcome: str, detail: Optional[str] = None) -> Dict:
        res["outcome"], res["detail"] = outcome, detail
        return res

    tmp: Optional[Path] = None
    try:
        res["commit"] = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        data = _git(repo, "cat-file", "blob", f"{res['commit']}:{REL_PATH}")
        cand = res["candidate_blob"] = blob_of(repo, data)
        history = main_history_blobs(repo, main_ref)
        if not history:
            return done("error", f"{main_ref} 的历史里没有 {REL_PATH}——ref 或路径不对，无法判定")

        prev = None
        if dest.exists():
            prev = res["previous_blob"] = blob_of(repo, dest.read_bytes())
            res["drift"] = prev not in history
        if prev == cand:
            return done("already_current")

        if cand not in history:
            res["gate_failures"] = [f"{ref} 上的编排器（blob {cand[:12]}）不在 {main_ref} 的历史里——"
                                    "只部署已合入 main 的版本"]
            return done("refused_gate", res["gate_failures"][0])

        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{dest.name}.deploy-", dir=str(dest.parent))
        tmp = Path(name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        res["gate_failures"] = gate_failures(data, tmp)
        if res["gate_failures"]:
            return done("refused_gate", f"{len(res['gate_failures'])} 项关卡未过")

        if res["drift"] and not accept_drift:
            return done("refused_drift",
                        f"部署副本（blob {prev[:12]}）不是 {main_ref} 上的任何一版——有人直接改过生产。"
                        f"先 `diff` 看改了什么、把改动搬进仓库 {REL_PATH} 合入后再部署；"
                        "确认丢弃才加 --accept-drift（会先备份）")
        if dry_run:
            return done("would_deploy")

        if res["drift"]:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = dest.with_name(f"{dest.name}.bak-{stamp}_drift-{prev[:8]}")
            backup.write_bytes(dest.read_bytes())
            os.chmod(backup, 0o755)
            res["backup"] = str(backup)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        tmp = None
        if blob_of(repo, dest.read_bytes()) != cand:
            return done("error", "写入后回读的内容与候选 blob 不一致")
        return done("deployed")
    except (_GitError, OSError, subprocess.SubprocessError) as e:
        return done("error", f"{type(e).__name__}: {e}")
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="把仓库里的编排器部署到 launchd 执行的位置")
    ap.add_argument("--ref", required=True,
                    help="部署哪个提交里的编排器（必填：手动用 origin/main；阶段 3 生产 checkout 用 HEAD）")
    ap.add_argument("--main-ref", default=DEFAULT_MAIN_REF, help="「已合入」的判据 ref（默认 origin/main）")
    ap.add_argument("--dest", type=Path, help="部署位置（默认 ~/.claude/scripts/alpha-hive-orchestrator.sh）")
    ap.add_argument("--out", type=Path, help="另把结果 JSON 写到这里（无默认）")
    ap.add_argument("--dry-run", action="store_true", help="跑完全部关卡但不写入")
    ap.add_argument("--accept-drift", action="store_true", help="部署副本被手改过时仍覆盖（先备份）")
    args = ap.parse_args(argv)

    res = deploy(args.ref, dest=args.dest, main_ref=args.main_ref,
                 dry_run=args.dry_run, accept_drift=args.accept_drift)
    line = json.dumps(res, ensure_ascii=False)
    print(line)
    if args.out:
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(line + "\n", encoding="utf-8")
        except OSError as e:
            print(f"deploy_orchestrator: 结果写不进 {args.out}：{e}", file=sys.stderr)
            return 3
    return _EXIT[res["outcome"]]


if __name__ == "__main__":
    sys.exit(main())
