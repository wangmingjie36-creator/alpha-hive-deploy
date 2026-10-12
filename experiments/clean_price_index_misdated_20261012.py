#!/usr/bin/env python3
"""一次性清理（v0.45.472，用户批准）：价格索引 `cache/price_history_{T}.jsonl` 里「不是那天收盘价」的剩余行。

v0.45.464 已删掉偏离 > 5% 与整数行权价形状的假价（`clean_price_index_fake_rows_20261012.py`），
当时保留并只报告了两类：76 行「与前后一天的收盘价吻合（≤ 1%）」、15 行「偏离当天收盘 1–5%」。
细看（对照 Twelve Data 日线收盘）：
- 76 行里只有 39 行与相邻那天的收盘价**逐位吻合**（≤ 0.05%）——真收盘、日期记错一天
  （快照会话错位，v0.45.238 修过的那类）；其余 37 行只是落在相邻收盘附近的盘前 / 盘中价；
- 15 行是盘中 / 盘后价。
索引的语义是「那天的收盘价」，这些行在自己的日期上都是错的。

**规则：**
- 与相邻交易日收盘逐位吻合（≤ 0.05%）⇒ 是那天的收盘：那天在索引里还没有行就**挪过去**，已有行就删掉本行；
- 其余「当天偏离 > 1%」的行 ⇒ 删；
- 与当天收盘 ≤ 1% 的行不动。

**安全。** 默认 dry-run；`--apply` 先逐字节备份原文件 + 核对用收盘价 + 清单，再经 `ledger_io.write_jsonl` 原子重写。
索引没有生产写入方（`append_observation` 零调用方），读时合并的 DB 权威价照旧覆盖同日值。
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAME_PCT = 0.01
EXACT_PCT = 0.0005


def plan_for(rows, bars):
    """返回 (新行列表, 动作清单)。纯函数。"""
    dates = sorted(bars)
    # v0.45.479：「目标日已有行」只算**清理后还留着**的行——原写法用清理前的日期集合，目标日那行若自己也要被删，
    # 挪过去就被拒、两行一起没了（审阅实测丢了 7 个真收盘：BILI 07-23 / 08-13，META / QCOM / TSLA / VKTX 07-23，NVDA 07-22）。
    have = {r["date"] for r in rows if r["date"] not in bars or abs(float(r["close"]) / bars[r["date"]] - 1) <= SAME_PCT}
    keep, moved_in, actions = [], {}, []
    for r in rows:
        d, px = r["date"], float(r["close"])
        if d not in bars or abs(px / bars[d] - 1) <= SAME_PCT:
            keep.append(r)
            continue
        i = dates.index(d)
        exact = [dates[j] for j in (i - 1, i + 1)
                 if 0 <= j < len(dates) and abs(px / bars[dates[j]] - 1) <= EXACT_PCT]
        if exact and exact[0] not in have and exact[0] not in moved_in:
            moved_in[exact[0]] = {**r, "date": exact[0]}
            actions.append({"action": "move", "from": d, "to": exact[0], "close": px, "close_that_day": bars[exact[0]]})
        else:
            actions.append({"action": "drop", "date": d, "close": px, "close_that_day": bars[d],
                            "why": "相邻那天已有行（本行是它的重复）" if exact else "不是当天收盘（盘前 / 盘中 / 盘后价）"})
    new = sorted(keep + list(moved_in.values()), key=lambda x: x["date"])
    return new, actions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--closes", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    from hive_logger import PATHS
    cache = Path(a.cache_dir) if a.cache_dir else Path(PATHS.cache_dir)
    closes = json.loads(Path(a.closes).read_text())
    plan, n_move, n_drop = {}, 0, 0
    for f in sorted(cache.glob("price_history_*.jsonl")):
        t = f.name[len("price_history_"):-len(".jsonl")]
        rows = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        if not rows:
            continue
        if t not in closes:
            raise SystemExit(f"{t} 没有核对用的收盘价——不猜，停下")
        new, actions = plan_for(rows, closes[t])
        if actions:
            plan[t] = {"file": str(f), "new": new, "actions": actions}
            n_move += sum(1 for x in actions if x["action"] == "move")
            n_drop += sum(1 for x in actions if x["action"] == "drop")
    print(f"涉及 {len(plan)} 个文件：挪到正确日期 {n_move} 行，删 {n_drop} 行")
    for t, p in plan.items():
        print(f"  {t}: " + ", ".join(f"{x['from']}→{x['to']}" if x["action"] == "move" else f"删{x['date']}"
                                     for x in p["actions"]))
    if not a.apply:
        print("dry-run：未写任何文件（加 --apply 执行）")
        return
    import ledger_io
    if not a.backup_dir:
        raise SystemExit("--apply 必须给 --backup-dir")
    bk = Path(a.backup_dir)
    bk.mkdir(parents=True, exist_ok=False)
    for p in plan.values():
        shutil.copy2(p["file"], bk / Path(p["file"]).name)
    shutil.copy2(a.closes, bk / "verified_closes_twelve_data.json")
    (bk / "manifest.json").write_text(json.dumps(
        {t: p["actions"] for t, p in plan.items()}, ensure_ascii=False, indent=1))
    for p in plan.values():
        ledger_io.write_jsonl(p["file"], p["new"])
    print(f"已执行；原文件与清单在 {bk}")


if __name__ == "__main__":
    main()
