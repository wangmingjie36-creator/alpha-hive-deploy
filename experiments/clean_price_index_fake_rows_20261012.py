#!/usr/bin/env python3
"""一次性清理：价格索引 `cache/price_history_{T}.jsonl` 里「不是那天收盘价」的行（v0.45.464，2026-10-12，用户批准）。

**来源。** 索引全部 782 行来自 08-15/16 的一次性快照迁移（`price_history.merge_snapshots_into_index`），
值是快照的 `_snapshot_stock_price`。v0.45.462 前 analyze() 取不到现价会把它写成行权价中位数或 145.0；
6 月还有测试把 `analyze("NVDA", stock_price=145.0)` 的快照写进了生产 cache（`TEST` 票的快照与索引至今还在）。

**判据（对照 Twelve Data 日线收盘，`--closes` 给的 JSON：{ticker: {date: close}}）：**
- 删：与**当天**及**前后各一个交易日**的收盘价都偏离 > 5%（不是那天、也不是错一天的真收盘）
- 删：值是 0.5 的整数倍（行权价形状）、与当天收盘偏离 > 1%、且不与前后一天吻合（≤ 1%）——行权价中位数兜底
- 删：`TEST` 整个索引文件（不是真实标的，测试写穿生产的残留）
- 不动：与当天 ≤ 1%；与前后一天 ≤ 1%（真收盘、日期错一天——另一类问题，只报告）；其余 1–5% 的非整数值（盘中 / 盘后价，只报告）

**安全。** 默认 dry-run 只打印；`--apply` 先把原文件逐字节拷进 `--backup-dir`（与清单一起），再经 `ledger_io.write_jsonl` 原子重写。
索引没有任何生产写入方（`append_observation` 零调用方，迁移标记全在），清理期间无并发写。
读者影响（只读审计）：五个读者都没把这些行用进落盘结果（见 CHANGELOG v0.45.462 / 464）。
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAKE_PCT = 0.05
MATCH_PCT = 0.01


def classify(ticker, rows, bars):
    """返回 (删的行, 报告用的分类计数, 灰区明细)。纯函数。"""
    dates = sorted(bars)
    drop, counts, notes = [], {"same": 0, "adjacent": 0, "grey": 0, "no_bar": 0, "drop": 0}, []
    for r in rows:
        d, px = r["date"], float(r["close"])
        if d not in bars:
            counts["no_bar"] += 1
            notes.append((ticker, d, px, "那天没有收盘价可核"))
            continue
        i = dates.index(d)
        same = abs(px / bars[d] - 1)
        nb = [abs(px / bars[dates[j]] - 1) for j in (i - 1, i + 1) if 0 <= j < len(dates)]
        near_nb = bool(nb) and min(nb) <= MATCH_PCT
        strike_like = abs(px * 2 - round(px * 2)) < 1e-9
        if same <= MATCH_PCT:
            counts["same"] += 1
        elif near_nb:
            counts["adjacent"] += 1
        elif same > FAKE_PCT and (not nb or min(nb) > FAKE_PCT):
            drop.append({**r, "reason": "far_from_close", "close_that_day": bars[d]})
        elif strike_like:
            drop.append({**r, "reason": "strike_shaped", "close_that_day": bars[d]})
        else:
            counts["grey"] += 1
            notes.append((ticker, d, px, f"偏离当天收盘 {same * 100:.1f}%"))
    counts["drop"] = len(drop)
    return drop, counts, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--closes", required=True, help="Twelve Data 收盘价 JSON {ticker: {date: close}}")
    ap.add_argument("--cache-dir", default=None, help="缺省 PATHS.cache_dir")
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from hive_logger import PATHS
    cache = Path(a.cache_dir) if a.cache_dir else Path(PATHS.cache_dir)
    closes = json.loads(Path(a.closes).read_text())
    plan, totals, all_notes = {}, {}, []
    for f in sorted(cache.glob("price_history_*.jsonl")):
        t = f.name[len("price_history_"):-len(".jsonl")]
        rows = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        if t == "TEST":
            plan[t] = {"file": str(f), "drop_file": True, "drop": rows, "keep": []}
            continue
        if t not in closes:
            raise SystemExit(f"{t} 没有核对用的收盘价——不猜，停下")
        drop, counts, notes = classify(t, rows, closes[t])
        totals[t] = counts
        all_notes += notes
        if drop:
            dropped = {(d["date"], d["close"]) for d in drop}
            plan[t] = {"file": str(f), "drop_file": False, "drop": drop,
                       "keep": [r for r in rows if (r["date"], r["close"]) not in dropped]}

    n_drop = sum(len(p["drop"]) for p in plan.values())
    print(f"索引文件 {len(totals) + ('TEST' in plan)} 个；要删 {n_drop} 行，涉及 {len(plan)} 个文件")
    for t, p in plan.items():
        print(f"  {t}: 删 {len(p['drop'])} 行" + ("（整个文件）" if p["drop_file"] else f"，留 {len(p['keep'])} 行"))
    agg = {k: sum(c[k] for c in totals.values()) for k in ("same", "adjacent", "grey", "no_bar")}
    print(f"保留行：当天吻合 {agg['same']} / 错一天的真收盘 {agg['adjacent']} / 1–5% 灰区 {agg['grey']} / 无可核 {agg['no_bar']}")
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
        {"removed": {t: p["drop"] for t, p in plan.items()}, "kept_counts": totals,
         "grey_or_unverifiable_kept": all_notes, "rule": {"fake_pct": FAKE_PCT, "match_pct": MATCH_PCT}},
        ensure_ascii=False, indent=1))
    for p in plan.values():
        if p["drop_file"]:
            os.remove(p["file"])
        else:
            ledger_io.write_jsonl(p["file"], p["keep"])
    print(f"已执行；原文件与清单在 {bk}")


if __name__ == "__main__":
    main()
