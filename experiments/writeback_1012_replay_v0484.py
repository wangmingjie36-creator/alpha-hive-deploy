"""10-12 整日重放结果写回生产（v0.45.484）。两段式，照 v0.45.464 的 patch 脚本：

  plan （只读生产）：python3 experiments/writeback_1012_replay_v0484.py plan  <沙箱> <prod> <plan_dir> [--scope …]
  apply（写生产）  ：python3 experiments/writeback_1012_replay_v0484.py apply <plan_dir> <prod> <backup_dir> [--dry-run]

沙箱 = `replay_1012_full_day_v0484.py corrected … union` 跑完的数据根（control 已 29/30 逐位复现生产存档，见 CHANGELOG）。

scope（逗号分隔，缺省 = core）：
  core      predictions 10-12 的 30 行（整行换成沙箱行，id 不动）、signal_archive 10-12（整日删除再插入）、
            `.swarm_results_2026-10-12.json`、`report_snapshots/{T}_2026-10-12.json`
  memory    agent_memory / reasoning_sessions / adapted_weights 的 10-12 行、sentiment_baseline.db 的 10-12 行
  options   `cache/options_snapshot_{T}_2026-10-12.json`（生产当天 0 份）、`cache/iv_history_{T}.jsonl` 的 10-12 行（生产当天 0 行）
  options_files  只换期权快照文件、不碰 iv_history（v0.45.484 第二次写回用：IV 索引 10-12 行第一次已写、29/30 逐字相同，
            BILI 那行是 Yahoo 占位 IV 6.25（v0.45.482 在源头剔除的那种），不写进索引）
  report    日报 `alpha-hive-daily-2026-10-12.md` / `.json` 与 X 线程 `alpha-hive-thread-2026-10-12-N.txt`，内容取重放
            `run_swarm_scan` 返回的 report（重放钟钉在 10-12 14:11:56，「📌 关注事项」的距今天数本来就对，不用换回原文）；
            写法同 `save_report`（md = markdown_report、json 用 SafeJSONEncoder indent=2、线程逐条一个文件）。需 `--report`。

写前：plan 记下每一处的写前值（行内容哈希 / 文件 sha256）；apply 先逐项核对没被别人改过（否则整体中止、什么都不写），
再整库备份（sqlite backup API）+ 逐文件原件备份，DB 一个事务，JSON 走 ledger_io / atomic_json_write。
不动：纸面组合 / 跨式 / VRP / 对冲 / 卖权账本、probability_scorecard、12 份 ML 深度报告与 analysis-*-ml JSON、网站（下次扫描的 gh-pages 同步自然带上新日报）。
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# 模块层注入仓库根（experiments 脚本约定，守卫 tests/test_experiments_script_bootstrap.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

D = "2026-10-12"
TABLES_CORE = ("predictions", "signal_archive")
TABLES_MEMORY = ("agent_memory", "reasoning_sessions", "adapted_weights")


def _sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _rows(db, table, where="substr(date,1,10)=?", args=(D,)):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        rows = [dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id", args)]
    finally:
        c.close()
    return cols, rows


def _h(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def plan(sandbox, prod, plan_dir, scope, report_path=None):
    os.makedirs(plan_dir, exist_ok=True)
    P = {"date": D, "scope": sorted(scope), "db": {}, "sentiment_baseline": None, "files": {}, "jsonl_rows": {}}
    pdb, sdb = os.path.join(prod, "pheromone.db"), os.path.join(sandbox, "pheromone.db")
    tables = (TABLES_CORE if "core" in scope else ()) + (TABLES_MEMORY if "memory" in scope else ())
    for t in tables:
        cols, old = _rows(pdb, t)
        _, new = _rows(sdb, t)
        P["db"][t] = {"cols": cols, "old_hash": _h(old), "old_n": len(old), "old": old, "new": new}
    if "core" in scope:
        o = {r["ticker"]: r for r in P["db"]["predictions"]["old"]}
        n = {r["ticker"]: r for r in P["db"]["predictions"]["new"]}
        assert sorted(o) == sorted(n) and len(n) == 30, "predictions 标的集合不一致"
        assert all((r["price_at_predict"] or 0) > 0 for r in n.values()), "重放行仍有入场价不可用"
        rel = [f".swarm_results_{D}.json"] + [f"report_snapshots/{t}_{D}.json" for t in sorted(n)]
    else:
        rel = []
    if "options" in scope or "options_files" in scope:
        rel += sorted(os.path.relpath(p, sandbox) for p in Path(sandbox, "cache").glob(f"options_snapshot_*_{D}.json"))
    if "options" in scope:
        for p in sorted(Path(sandbox, "cache").glob("iv_history_*.jsonl")):
            new_lines = [ln for ln in p.read_text().splitlines() if f'"{D}"' in ln]
            if not new_lines:
                continue
            r = os.path.relpath(p, sandbox)
            pp = os.path.join(prod, r)
            P["jsonl_rows"][r] = {"sha_before": _sha(pp) if os.path.exists(pp) else None,
                                  "prod_has_date": os.path.exists(pp) and any(f'"{D}"' in ln for ln in open(pp)),
                                  "add": [json.loads(x) for x in new_lines]}
    for r in rel:
        pp, sp = os.path.join(prod, r), os.path.join(sandbox, r)
        P["files"][r] = {"sha_before": _sha(pp) if os.path.exists(pp) else None, "new": json.load(open(sp))}
    if "memory" in scope:
        def sb(db):
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                return [list(r) for r in c.execute("SELECT ticker, date, sentiment_pct FROM sentiment_baseline WHERE date=? ORDER BY ticker", (D,))]
            finally:
                c.close()
        old = sb(os.path.join(prod, "sentiment_baseline.db"))
        P["sentiment_baseline"] = {"old": old, "old_hash": _h(old), "new": sb(os.path.join(sandbox, "sentiment_baseline.db"))}
    if "report" in scope:
        from alpha_hive_daily_report import SafeJSONEncoder
        rep = json.load(open(report_path))
        P["text_files"] = {f"alpha-hive-daily-{D}.md": rep["markdown_report"],
                           f"alpha-hive-daily-{D}.json": json.dumps(rep, ensure_ascii=False, indent=2, cls=SafeJSONEncoder)}
        for i, th in enumerate(rep["twitter_threads"], 1):
            P["text_files"][f"alpha-hive-thread-{D}-{i}.txt"] = th
        P["text_sha_before"] = {r: (_sha(os.path.join(prod, r)) if os.path.exists(os.path.join(prod, r)) else None)
                                for r in P["text_files"]}
        P["thread_files_before"] = sorted(x.name for x in Path(prod).glob(f"alpha-hive-thread-{D}-*.txt"))
    with open(os.path.join(plan_dir, "plan.json"), "w") as f:
        json.dump(P, f, ensure_ascii=False, default=str)
    summ = {t: f'{v["old_n"]} → {len(v["new"])}' for t, v in P["db"].items()}
    summ["text_files"] = len(P.get("text_files") or {})
    print("plan:", json.dumps({"db": summ, "files": len(P["files"]), "jsonl_files": len(P["jsonl_rows"]),
                               "sentiment_baseline": None if not P["sentiment_baseline"] else
                               f'{len(P["sentiment_baseline"]["old"])} → {len(P["sentiment_baseline"]["new"])}'},
                              ensure_ascii=False))


def apply(plan_dir, prod, backup_dir, dry_run):
    import ledger_io
    from hive_logger import atomic_json_write
    P = json.load(open(os.path.join(plan_dir, "plan.json")))
    pdb = os.path.join(prod, "pheromone.db")
    # 1) 写前值核对：任何一处被别人改过 ⇒ 整体中止
    bad = []
    for t, v in P["db"].items():
        _, cur = _rows(pdb, t)
        if _h(cur) != v["old_hash"]:
            bad.append(f"db:{t}")
    for r, v in P["files"].items():
        pp = os.path.join(prod, r)
        if (_sha(pp) if os.path.exists(pp) else None) != v["sha_before"]:
            bad.append(f"file:{r}")
    for r, v in P["jsonl_rows"].items():
        pp = os.path.join(prod, r)
        if (_sha(pp) if os.path.exists(pp) else None) != v["sha_before"] or v["prod_has_date"]:
            bad.append(f"jsonl:{r}")
    for r, v in (P.get("text_sha_before") or {}).items():
        pp = os.path.join(prod, r)
        if (_sha(pp) if os.path.exists(pp) else None) != v:
            bad.append(f"text:{r}")
    if "text_files" in P and sorted(x.name for x in Path(prod).glob(f"alpha-hive-thread-{D}-*.txt")) != P["thread_files_before"]:
        bad.append("thread_files")
    if P.get("sentiment_baseline"):
        c = sqlite3.connect(f"file:{os.path.join(prod, 'sentiment_baseline.db')}?mode=ro", uri=True)
        cur = [list(r) for r in c.execute("SELECT ticker, date, sentiment_pct FROM sentiment_baseline WHERE date=? ORDER BY ticker", (D,))]
        c.close()
        if _h(cur) != P["sentiment_baseline"]["old_hash"]:
            bad.append("sentiment_baseline")
    if bad:
        sys.exit(f"写前值不一致，整体中止、什么都没写：{bad}")
    print("写前值核对通过：", len(P["db"]), "张表 /", len(P["files"]), "个文件 /", len(P["jsonl_rows"]), "个 jsonl")
    if dry_run:
        print("dry-run：不写")
        return
    # 2) 备份
    os.makedirs(backup_dir, exist_ok=False)
    for name in ("pheromone.db", "sentiment_baseline.db"):
        src = os.path.join(prod, name)
        if name == "sentiment_baseline.db" and not P.get("sentiment_baseline"):
            continue
        s = sqlite3.connect(src)
        d = sqlite3.connect(os.path.join(backup_dir, name))
        s.backup(d)
        d.close()
        s.close()
    for r in list(P["files"]) + list(P["jsonl_rows"]) + list(P.get("text_files") or {}) + list(P.get("thread_files_before") or []):
        pp = os.path.join(prod, r)
        if os.path.exists(pp):
            os.makedirs(os.path.dirname(os.path.join(backup_dir, "files", r)), exist_ok=True)
            shutil.copy2(pp, os.path.join(backup_dir, "files", r))
    shutil.copy2(os.path.join(plan_dir, "plan.json"), os.path.join(backup_dir, "plan.json"))
    # 3) DB：一个事务
    c = sqlite3.connect(pdb, timeout=60)
    try:
        c.execute("BEGIN IMMEDIATE")
        for t, v in P["db"].items():
            cols = [x for x in v["cols"] if x != "id"]
            if t == "predictions":
                for row in v["new"]:
                    n = c.execute(f"UPDATE predictions SET {', '.join(f'{x}=?' for x in cols if x not in ('date', 'ticker'))} "
                                  "WHERE date=? AND ticker=?",
                                  [row[x] for x in cols if x not in ("date", "ticker")] + [row["date"], row["ticker"]]).rowcount
                    assert n == 1, (t, row["ticker"], n)
            else:
                c.execute(f"DELETE FROM {t} WHERE substr(date,1,10)=?", (D,))
                for row in v["new"]:
                    c.execute(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [row[x] for x in cols])
        for t, v in P["db"].items():
            n = c.execute(f"SELECT count(*) FROM {t} WHERE substr(date,1,10)=?", (D,)).fetchone()[0]
            assert n == len(v["new"]), (t, n, len(v["new"]))
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
    if P.get("sentiment_baseline"):
        s = sqlite3.connect(os.path.join(prod, "sentiment_baseline.db"), timeout=60)
        try:
            s.execute("BEGIN IMMEDIATE")
            s.execute("DELETE FROM sentiment_baseline WHERE date=?", (D,))
            s.executemany("INSERT INTO sentiment_baseline (ticker, date, sentiment_pct) VALUES (?,?,?)", P["sentiment_baseline"]["new"])
            s.commit()
        finally:
            s.close()
    # 4) 文件
    for r, v in P["files"].items():
        atomic_json_write(os.path.join(prod, r), v["new"])
    for r, v in P["jsonl_rows"].items():
        pp = os.path.join(prod, r)
        recs = ledger_io.load_jsonl(pp) if os.path.exists(pp) else []
        ledger_io.write_jsonl(pp, recs + v["add"])
    if "text_files" in P:
        for name in P["thread_files_before"]:
            if name not in P["text_files"]:
                os.remove(os.path.join(prod, name))
        for r, txt in P["text_files"].items():
            ledger_io.atomic_write_text(os.path.join(prod, r), txt)
    print("已写回；备份在", backup_dir)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("plan", "apply"))
    ap.add_argument("a")
    ap.add_argument("prod")
    ap.add_argument("c")
    ap.add_argument("--scope", default="core")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default=None, help="scope 含 report 时：重放 run_swarm_scan 返回的 report JSON")
    a = ap.parse_args()
    if a.mode == "plan":
        plan(a.a, a.prod, a.c, set(a.scope.split(",")), a.report)
    else:
        apply(a.a, a.prod, a.c, a.dry_run)
