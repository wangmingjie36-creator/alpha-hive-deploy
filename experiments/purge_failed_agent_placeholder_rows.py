#!/usr/bin/env python3
"""清掉 `signal_archive` 里出错蜂的占位行（v0.45.768）。

v0.45.768 起抽取器不再把出错蜂（`swarm_agents.utils.make_error_result` 的 5.0 / 中性）写进档案，
判据唯一实现 `models.agent_record_failed`。但库里已有的历史行 **`backfill` 清不掉**：它是 `INSERT OR REPLACE`，
抽取器现在返回 None 的格子它不会删。本工具做两件事，只碰「当天记录里那只蜂确实出错」的格子：

  ① 删：`agent.<蜂>.score` / `agent.<蜂>.direction` —— 那只蜂出错的 (日期, 标的)；
  ② 改：`composite.swarm_agreement` —— 那一行有任何出错的蜂（含已退役的 CodeExecutorAgent）⇒ 按新抽取器重算，
     新值为 None 就删。只改有出错蜂的行：没出错蜂的行新旧抽取器逐位相同，碰都不碰（顺手「修」别的漂移不是本工具的事）。

**先对账再动**：每个目标格子在库里的值必须等于「旧抽取器对同一份 `.swarm_results_<日期>.json` 的输出」
（即确实是当时从这份记录写进去的占位）。对不上的格子不动、单独报出来，有一个就停（`--allow-mismatch` 才跳过它们继续）。

    python3 experiments/purge_failed_agent_placeholder_rows.py --expect-delete 336 --expect-update 439            # dry-run
    python3 experiments/purge_failed_agent_placeholder_rows.py --expect-delete 336 --expect-update 439 --apply --backup <file.db>

安全照 `purge_quarantined_signal_rows.py`：`--apply` 先 `BEGIN IMMEDIATE` 持写锁，再用 sqlite 在线备份 API 整库备份并
`integrity_check`；事务内**现读现判**、命中数必须恰好等于两个 `--expect`；改完核对、`integrity_check` 为 ok 才 COMMIT。
守卫 tests/test_failed_agent_placeholders.py::TestPurgeTool
"""
import argparse
import glob
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DIR_MAP = {"bullish": 1.0, "long": 1.0, "neutral": 0.0, "bearish": -1.0, "short": -1.0}


def _old_agent_cell(rec, field):
    """v0.45.768 之前的抽取器对这一格的输出（`_agent_score` / `_direction_num` 不问出错）。"""
    import signal_archive as sa
    if field == "score":
        return sa._num((rec or {}).get("score"))
    d = (rec or {}).get("direction")
    return _DIR_MAP.get(str(d).lower()) if d else None


def _old_agreement(tr):
    """v0.45.768 之前的 `_swarm_agreement`（不跳过出错的蜂）。"""
    from collections import Counter
    dirs = [str(v).lower() for v in ((d or {}).get("direction") for d in (tr.get("agent_details") or {}).values()) if v]
    if len(dirs) < 3:
        return None
    return Counter(dirs).most_common(1)[0][1] / len(dirs)


def plan(db, pattern=".swarm_results_*.json"):
    """返回 (删的格子 {(date,ticker,signal): 旧值}, 改的格子 {(date,ticker): (旧值, 新值或 None)})——都是「库里该是什么、要变成什么」。"""
    import signal_archive as sa
    from models import agent_record_failed
    archived = {s.split(".")[1] for s in sa.SIGNAL_EXTRACTORS if s.startswith("agent.") and s.endswith(".score")}
    deletes, updates = {}, {}
    for f in sorted(glob.glob(str(Path(db).parent / pattern))):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
        if not m:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        date = m.group(1)
        for ticker, tr in (data or {}).items():
            if not isinstance(tr, dict):
                continue
            ad = tr.get("agent_details") or {}
            failed = [a for a, rec in ad.items() if agent_record_failed(rec)]
            if not failed:
                continue
            for a in failed:
                if a not in archived:
                    continue
                for field in ("score", "direction"):
                    old = _old_agent_cell(ad[a], field)
                    if old is not None:
                        deletes[(date, ticker, f"agent.{a}.{field}")] = old
            old = _old_agreement(tr)
            if old is not None:
                updates[(date, ticker)] = (old, sa._swarm_agreement(tr))
    return deletes, updates


def _same(a, b):
    return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def resolve(conn, deletes, updates):
    """对账：只留库里确实存着旧值的格子。返回 (删的 id 列表, 改的 [(id, 新值或 None)], 对不上的说明列表)。"""
    import signal_archive as sa
    del_ids, upd, mismatch = [], [], []
    for (d, t, s), old in sorted(deletes.items()):
        row = conn.execute(f"SELECT id, value FROM {sa.TABLE} WHERE date=? AND ticker=? AND signal=?", (d, t, s)).fetchone()
        if row is None:
            continue                       # 当时就没入档（隔离 / 档案晚于该日建立），无事可做
        if _same(row[1], old):
            del_ids.append(row[0])
        else:
            mismatch.append(f"{d} {t} {s}: 库 {row[1]} ≠ 记录 {old}")
    for (d, t), (old, new) in sorted(updates.items()):
        row = conn.execute(f"SELECT id, value FROM {sa.TABLE} WHERE date=? AND ticker=? AND signal=?",
                           (d, t, "composite.swarm_agreement")).fetchone()
        if row is None:
            continue
        if new is not None and _same(row[1], new):
            continue                       # 已是新值（本工具跑过 / 新旧相同）——幂等
        if not _same(row[1], old):
            mismatch.append(f"{d} {t} composite.swarm_agreement: 库 {row[1]} ≠ 记录 {old}")
        else:
            upd.append((row[0], new))
    return del_ids, upd, mismatch


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="缺省 PATHS.db")
    ap.add_argument("--expect-delete", type=int, required=True, help="预期删除的 agent.* 行数（不等就中止）")
    ap.add_argument("--expect-update", type=int, required=True, help="预期改 / 删的 swarm_agreement 行数（不等就中止）")
    ap.add_argument("--allow-mismatch", action="store_true", help="对不上的格子跳过、不中止（缺省有一个就停）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=None)
    a = ap.parse_args(argv)
    import signal_archive as sa
    from hive_logger import PATHS
    db = Path(a.db) if a.db else Path(PATHS.db)
    deletes, updates = plan(db)

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as ro:
        del_ids, upd, mismatch = resolve(ro, deletes, updates)
    print(f"{db}: 出错蜂占位 agent.* 删 {len(del_ids)} 行（预期 {a.expect_delete}）；"
          f"swarm_agreement 改 {len(upd)} 行（预期 {a.expect_update}，其中删 {sum(1 for _i, v in upd if v is None)}）")
    for line in mismatch[:20]:
        print("  对不上（不动）：" + line)
    if mismatch:
        print(f"  对不上共 {len(mismatch)} 格")
        if not a.allow_mismatch:
            raise SystemExit("有对不上的格子——不动，停下（确认是记录被重扫覆盖等已知原因后才加 --allow-mismatch）")
    if (len(del_ids), len(upd)) != (a.expect_delete, a.expect_update):
        raise SystemExit("命中数与预期不符——不动，停下")
    if not a.apply:
        print("dry-run：未改任何行")
        return 0
    if not a.backup:
        raise SystemExit("--apply 必须给 --backup")
    bk = Path(a.backup)
    if bk.exists():
        raise SystemExit(f"{bk} 已存在，不覆盖")
    src = sqlite3.connect(db)
    src.isolation_level = None
    src.execute("BEGIN IMMEDIATE")
    try:
        # 持写锁之后再备份（同 purge_quarantined_signal_rows v0.45.479）：此刻起别人提交不了，备份就是即将被改的那个状态
        rd = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(bk)
        rd.backup(dst)
        rd.close()
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份库 integrity_check 未通过")
        dst.close()
        d_ids, u_rows, mm = resolve(src, deletes, updates)        # 事务内现读现判
        if (mm and not a.allow_mismatch) or (len(d_ids), len(u_rows)) != (a.expect_delete, a.expect_update):
            raise RuntimeError(f"事务内命中 删 {len(d_ids)} / 改 {len(u_rows)} / 对不上 {len(mm)}，与 dry-run 不同")
        before = src.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        src.executemany(f"DELETE FROM {sa.TABLE} WHERE id=?", [(i,) for i in d_ids])
        src.executemany(f"DELETE FROM {sa.TABLE} WHERE id=?", [(i,) for i, v in u_rows if v is None])
        src.executemany(f"UPDATE {sa.TABLE} SET value=? WHERE id=?", [(v, i) for i, v in u_rows if v is not None])
        after = src.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        n_del = len(d_ids) + sum(1 for _i, v in u_rows if v is None)
        left_d, left_u, _ = resolve(src, deletes, updates)
        if before - after != n_del or left_d or left_u:
            raise RuntimeError(f"核对失败：{before}→{after}（应删 {n_del}），残留 删 {len(left_d)} / 改 {len(left_u)}")
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity_check 未通过")
        src.execute("COMMIT")
    except BaseException:
        src.execute("ROLLBACK")
        raise
    print(f"已删 agent.* {len(d_ids)} 行、改 swarm_agreement {len(u_rows)} 行（{before} → {after}）；备份 {bk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
