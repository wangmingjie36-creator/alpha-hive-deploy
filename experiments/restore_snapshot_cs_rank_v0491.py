#!/usr/bin/env python3
"""把被往返写抹掉的 report_snapshots `cs_rank` / `created_at` 从 git 历史找回（v0.45.491）。

    /usr/local/bin/python3 experiments/restore_snapshot_cs_rank_v0491.py            # dry-run：只报计划
    （加 --apply --backup <不存在的目录> --expect-cs N --expect-created M 才写）

病因：v0.45.491 之前 `ReportSnapshot.load_from_json` 漏读 cs_rank、`save_to_json` 恒写 created_at=now()，
`outcomes_fetcher` 每次回填 T+1/T+7/T+30 都「读 → 填一格 → 存」⇒ cs_rank 被抹成 null、created_at 改成回填时刻。

来源：同一份文件在两个 git 仓库里的全部历史版本——
  · 代码仓库 `origin/main`（数据根迁移阶段 6 之前，日报提交跟踪 report_snapshots/，止于 2026-10-03）；
  · 数据备份仓库 `PATHS.data_backup_repo` 的 HEAD（2026-09-24 起每个扫描日一次）。
**按「同一次扫描」认版本**：身份 = (composite_score, direction, entry_price, agent_votes)。回填不改这四个，
同日重扫会改——08-25 / 08-26 一份文件有 2~5 个版本、各带各的 cs_rank，取「最早的非空版本」会拿到被后一次
扫描取代的那次的值；10-05 现行文件是 run-1（v0.45.4xx 恢复），而 `.swarm_results_2026-10-05.json` 是 run-2。
⇒ 不用 `.swarm_results_*.json` 当来源（07-08 / 07-09 / 08-12 / 10-05 共 33 份与快照自己的历史不一致）。

规则：
  · cs_rank：只补现值为 null 的；同身份版本里恰好一个不同的非空值才补，出现两个 ⇒ ambiguous，不补、报出来。
  · created_at：取同身份版本里最早的 created_at；与现值不同才改（往返写只会把它往后推）。
  · 其余任何字段不碰；写入前重读文件，现值已不是计划时看到的值 ⇒ 跳过（并发写者赢）。
  · --apply 前提：生产克隆的 feedback_loop.py 已含本版修复（否则下一次扫描的回填会再抹一次）；
    整个目录先复制到 --backup 并逐文件比对；条数恰为 --expect-* 才写；写完调 `record_tool_run` 登记。
幂等：写完再跑一遍，计划为空。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOOL = "restore_snapshot_cs_rank_v0491"
CODE_REPO = Path(__file__).resolve().parent.parent   # 代码仓库本身（__file__ 锚点：要的就是「代码在哪」）
FIX_MARKER = "_merge_onto_loaded"


def _git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git -C {repo} {' '.join(a)} 失败：{r.stderr.strip()[:300]}")
    return r.stdout


def _versions(repo, ref, rel):
    """(提交时间, 仓库标签, sha8, 文件内容 dict)，按提交顺序。"""
    out = []
    log = _git(repo, "log", "--reverse", "--format=%H %cI", ref, "--", rel)
    for line in log.splitlines():
        sha, when = line.split()
        r = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{rel}"], capture_output=True, text=True)
        if r.returncode != 0:      # 该提交删除了它
            continue
        try:
            out.append((when, str(repo), sha[:8], json.loads(r.stdout)))
        except ValueError:
            continue
    return out


def _ident(d):
    return json.dumps([d.get("composite_score"), d.get("direction"), d.get("entry_price"),
                       d.get("agent_votes")], sort_keys=True)


def plan(snap_dir, sources):
    """sources = [(repo, ref, 仓库内 report_snapshots 相对目录)]。返回 (逐文件计划, 统计)。"""
    rows, stat = [], Counter()
    for fname in sorted(os.listdir(snap_dir)):
        if not fname.endswith(".json"):
            continue
        cur = json.loads(Path(snap_dir, fname).read_text(encoding="utf-8"))
        vs = []
        for repo, ref, sub in sources:
            vs += _versions(repo, ref, f"{sub}/{fname}")
        vs.sort(key=lambda v: v[0])
        same = [v for v in vs if _ident(v[3]) == _ident(cur)]
        row = {"file": fname, "date": cur.get("date"), "cs_rank": None, "created_at": None,
               "cur_cs_rank": cur.get("cs_rank"), "cur_created_at": cur.get("created_at")}

        if cur.get("cs_rank") is None:
            nonnull = [v for v in same if v[3].get("cs_rank") is not None]
            distinct = {json.dumps(v[3]["cs_rank"], sort_keys=True) for v in nonnull}
            if not vs:
                stat["cs_rank:no_history"] += 1
            elif not same:
                stat["cs_rank:no_same_scan_version"] += 1
            elif not nonnull:
                stat["cs_rank:never_had_one"] += 1
            elif len(distinct) > 1:
                stat["cs_rank:AMBIGUOUS"] += 1
                row["ambiguous"] = sorted(distinct)
            else:
                row["cs_rank"] = nonnull[-1][3]["cs_rank"]
                row["cs_rank_from"] = nonnull[-1][1:3]
                stat["cs_rank:restore"] += 1
        else:
            stat["cs_rank:intact"] += 1

        created = sorted(v[3]["created_at"] for v in same if v[3].get("created_at"))
        if created and created[0] != cur.get("created_at"):
            row["created_at"] = created[0]
            stat["created_at:restore"] += 1
        rows.append(row)
    return rows, stat


def _todo(rows):
    return [r for r in rows if r["cs_rank"] is not None or r["created_at"] is not None]


def _copy_and_verify(src, dst):
    shutil.copytree(src, dst)
    a = {p.name: p.read_bytes() for p in Path(src).glob("*.json")}
    b = {p.name: p.read_bytes() for p in Path(dst).glob("*.json")}
    if a != b:
        raise SystemExit(f"备份 {dst} 与源目录逐文件比对不一致——停下（备份留在原地供检查）")
    return len(b)


def apply(snap_dir, rows):
    """逐文件写回；写前重读，现值已变就跳过。返回 (写入文件数, 其中 cs_rank 数, 其中 created_at 数, 跳过数)。"""
    from hive_logger import atomic_json_write
    n_files = n_cs = n_ca = skipped = 0
    for r in _todo(rows):
        p = Path(snap_dir, r["file"])
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("cs_rank") != r["cur_cs_rank"] or d.get("created_at") != r["cur_created_at"]:
            skipped += 1
            continue
        if r["cs_rank"] is not None:
            d["cs_rank"] = r["cs_rank"]
            n_cs += 1
        if r["created_at"] is not None:
            d["created_at"] = r["created_at"]
            n_ca += 1
        atomic_json_write(str(p), d, indent=2)
        n_files += 1
    return n_files, n_cs, n_ca, skipped


def main(argv=None):
    from hive_logger import PATHS
    ap = argparse.ArgumentParser()
    ap.add_argument("--snap-dir", default=None, help="缺省 PATHS.home/report_snapshots")
    ap.add_argument("--code-repo", default=str(CODE_REPO))
    ap.add_argument("--code-ref", default="origin/main")
    ap.add_argument("--backup-repo", default=None, help="缺省 PATHS.data_backup_repo")
    ap.add_argument("--backup-ref", default="HEAD")
    ap.add_argument("--plan-out", default=None, help="逐文件计划写到这里（JSON）")
    ap.add_argument("--prod-clone", default=os.path.expanduser("~/alpha-hive-prod"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=None, help="--apply 必填：一个还不存在的目录，整份 report_snapshots 复制到这里")
    ap.add_argument("--expect-cs", type=int, default=None)
    ap.add_argument("--expect-created", type=int, default=None)
    a = ap.parse_args(argv)

    snap_dir = Path(a.snap_dir) if a.snap_dir else Path(PATHS.home) / "report_snapshots"
    backup_repo = Path(a.backup_repo) if a.backup_repo else Path(PATHS.data_backup_repo)
    sources = [(Path(a.code_repo), a.code_ref, "report_snapshots"),
               (backup_repo, a.backup_ref, "report_snapshots")]
    rows, stat = plan(snap_dir, sources)
    todo = _todo(rows)

    print(f"快照目录 {snap_dir}：{len(rows)} 份")
    for k, v in sorted(stat.items()):
        print(f"  {k}: {v}")
    by_date = defaultdict(Counter)
    for r in todo:
        by_date[r["date"]]["cs_rank" if r["cs_rank"] is not None else "created_at_only"] += 1
    for dt in sorted(by_date):
        print(f"  {dt} {dict(by_date[dt])}")
    if a.plan_out:
        Path(a.plan_out).write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"逐文件计划 → {a.plan_out}")
    n_cs = sum(1 for r in todo if r["cs_rank"] is not None)
    n_ca = sum(1 for r in todo if r["created_at"] is not None)
    print(f"计划：cs_rank 补 {n_cs} 份，created_at 改回 {n_ca} 份")

    if not a.apply:
        print("dry-run：未写")
        return 0
    if stat.get("cs_rank:AMBIGUOUS"):
        raise SystemExit("有 ambiguous 的文件——先人工看 --plan-out，停下")
    if a.expect_cs != n_cs or a.expect_created != n_ca:
        raise SystemExit(f"--expect-cs/--expect-created（{a.expect_cs}/{a.expect_created}）与计划（{n_cs}/{n_ca}）不符——停下")
    fl = Path(a.prod_clone) / "feedback_loop.py"
    if not fl.exists() or FIX_MARKER not in fl.read_text(encoding="utf-8"):
        raise SystemExit(f"{fl} 还没有 v0.45.491 的修复——现在写回，下一次扫描的回填会再抹一次。先等生产克隆快进")
    if not a.backup or Path(a.backup).exists():
        raise SystemExit("--apply 必须给一个还不存在的 --backup 目录")
    n_files = _copy_and_verify(snap_dir, a.backup)
    print(f"已备份 {n_files} 份 → {a.backup}（逐文件比对一致）")

    w_files, w_cs, w_ca, skipped = apply(snap_dir, rows)
    print(f"已写 {w_files} 份（cs_rank {w_cs}，created_at {w_ca}）；写前现值已变而跳过 {skipped} 份")
    from data_migrations.runner import record_tool_run
    record_tool_run(TOOL, rows_affected=w_files, backup=str(a.backup),
                    args=" ".join(argv if argv is not None else sys.argv[1:]),
                    ledger_dir=Path(PATHS.migrations_state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
