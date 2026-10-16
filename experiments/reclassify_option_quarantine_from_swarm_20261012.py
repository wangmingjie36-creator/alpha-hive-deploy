#!/usr/bin/env python3
"""按归档的**真实来源**重判期权观测该不该隔离（v0.45.479；取代 v0.45.472 按 cache 快照做的判定）。

**为什么重判。** `signal_archive` 的期权值抽自 `.swarm_results_<日期>.json` 的 `OracleBeeEcho.details`
（实测 5343 个值与之逐位相同、0 个不同）；v0.45.472 却拿 `cache/options_snapshot_*.json` 判——同一天常是
另一次运行（之后重跑 / 测试改写过快照），于是有坏行没挡住、也有好行被删（v0.45.476 二次检查审阅发现）。

**判据（逐（日期, 标的），只读 swarm_results 的 Oracle details）：**
- 整份是假的（五类市场观测全隔离）：
  - 样本链：`total_oi == 85200` 且 `put_call_ratio == 0.54`（硬编码样本链）；
  - 空链：`total_oi == 0`（Yahoo 限流时返回的 OI 全 0 的链，P/C / IV / GEX 无意义）；
  - 测试夹具：key_levels 前三档 OI == [600, 400, 200]（两天逐位相同的那份夹具）；
  - 现价是假的（`_snapshot_stock_price` 对照 Twelve Data 收盘——非交易日取前一交易日，另看前后各一天）：
    以下任一成立、且**不等于蜂群当天用的价**（predictions.price_at_predict_raw，没有就 price_at_predict；相等且偏离 ≤ 20% ⇒
    是调用方给的真价——偏离更大时蜂群自己的价也可能是假的，如 06-09 VKTX 的 145）、且**不是拆股调整差**（见下；Twelve Data 历史是拆股调整后的）：
    · 常数 145.0 / 100.0；
    · 与当天及前后一天都偏离 > 20%（拆股调整差除外：同一标的 ≥ 3 条记录落在同一整数比上，±0.5%）；
    · 0.5 整数倍、偏离 > 1%、**且落在该快照行权价网格的格点或两格中点上**（不在网格上就不可能是行权价中位数）。
  - 偏离 1–20% 的非整数价（盘中 / 盘后价）不算假价。
- 只隔离 GEX：日期 ≥ 06-29（CBOE 主源起，见 `_COHORT_HISTORY` 的 v0.45.479 更正）、key_levels 的 IV 带 Yahoo 长浮点指纹
  （> 4 位小数；CBOE 给 4 位）。
输出 JSON：{"full": [[date, ticker, why]...], "gex": [[date, ticker, why]...], "stats": {...}}。只读。

    python3 experiments/reclassify_option_quarantine_from_swarm_20261012.py --closes <td_closes.json> --out <out.json>
"""
import argparse
import bisect
import glob
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CBOE_FROM = "2026-06-29"


def _decs(x) -> int:
    s = repr(float(x))
    return len(s.split(".")[1]) if "." in s and "e" not in s else 99


def _levels(det):
    kl = det.get("key_levels") or {}
    return (kl.get("support") or []) + (kl.get("resistance") or [])


def _strikes(det):
    ks = {float(lv["strike"]) for lv in _levels(det) if isinstance(lv.get("strike"), (int, float))}
    ks |= {float(u["strike"]) for u in (det.get("unusual_activity") or []) if isinstance(u.get("strike"), (int, float))}
    return sorted(ks)


def _on_strike_grid(px, det) -> bool:
    ks = _strikes(det)
    steps = [round(b - a, 4) for a, b in zip(ks, ks[1:]) if b - a > 0]
    if not steps:
        return True   # 判不了就按「可能是」——保守，不放过
    step = min(steps)
    return abs(px * 2 / step - round(px * 2 / step)) < 1e-6


def _split_factor(r):
    """r 贴近某个整数 2–10（或其倒数）±0.5% ⇒ 返回该因子（带方向：n 或 1/n），否则 None。"""
    for n in range(2, 11):
        if abs(r / n - 1) < 0.005:
            return n
        if abs(r * n - 1) < 0.005:
            return 1 / n
    return None


def split_factors(pairs):
    """pairs: {ticker: [ratio, ...]}。同一标的 ≥ 3 条记录落在同一个整数比上才认拆股（单日一个比值是巧合：
    06-09 VKTX 的假价 145 / 真收盘 29.23 = 4.96，曾被 ±1% 的单日判据当成 5:1 拆股放过——v0.45.479）。"""
    out = {}
    for t, rs in pairs.items():
        c = Counter(f for f in (_split_factor(r) for r in rs) if f)
        if c:
            f, n = c.most_common(1)[0]
            if n >= 3:
                out[t] = f
    return out


def classify(det, closes, swarm_px, date, split=None):
    """返回 ("full" | "gex" | None, 原因)。纯函数。"""
    if det.get("total_oi") == 85200 and det.get("put_call_ratio") == 0.54:
        return "full", "sample_chain"
    lv = _levels(det)
    if [x.get("oi") for x in lv][:3] == [600, 400, 200]:
        return "full", "test_fixture"
    if det.get("total_oi") in (0, 0.0) and det.get("data_quality") != "unavailable":
        # 空链：Yahoo 限流时返回的 OI 全 0 的链（v0.34.0 换 CBOE 主源要治的正是它）——P/C / IV / GEX 都无意义
        return "full", "empty_chain"
    sp = det.get("_snapshot_stock_price")
    if isinstance(sp, (int, float)) and sp > 0 and closes:
        ds = sorted(closes)
        i = bisect.bisect_left(ds, date)
        same = closes.get(date) if date in closes else (closes[ds[i - 1]] if i > 0 else None)
        nb = [closes[ds[j]] for j in (i - 1, i + 1 if date in closes else i) if 0 <= j < len(ds)]
        if same:
            dev = abs(sp / same - 1)
            ndev = min([abs(sp / x - 1) for x in nb] or [9.0])
            # 「等于蜂群当天用的价 ⇒ 调用方给的真价」只在偏离不大（≤ 20%）时作数：
            # 06-09 VKTX 蜂群自己记的价就是假的 145（v0.45.479 审 06-09 时发现），大偏离时它证明不了真。
            real = (dev <= 0.01 or ndev <= 0.01
                    or (swarm_px is not None and abs(swarm_px - sp) < 1e-6 and dev <= 0.20)
                    or (split is not None and _split_factor(sp / same) == split))
            if not real:
                if sp in (145.0, 100.0):
                    return "full", "constant_price"
                if dev > 0.20 and ndev > 0.20:
                    return "full", "price_off_20pct"
                if abs(sp * 2 - round(sp * 2)) < 1e-9 and _on_strike_grid(sp, det):
                    return "full", "strike_median_price"
    if date >= CBOE_FROM:
        ivs = [x.get("iv") for x in lv if isinstance(x.get("iv"), (int, float)) and x.get("iv")]
        if ivs and max(_decs(v) for v in ivs) > 4:
            return "gex", "yfinance_chain_fingerprint"
    return None, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--closes", required=True)
    ap.add_argument("--home", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from hive_logger import PATHS
    home = Path(a.home) if a.home else Path(PATHS.home)
    td = json.loads(Path(a.closes).read_text())
    con = sqlite3.connect(f"file:{home / 'pheromone.db'}?mode=ro", uri=True)
    px = {(d, t): (raw if raw is not None else p) for d, t, p, raw in
          con.execute("SELECT date, ticker, price_at_predict, price_at_predict_raw FROM predictions")}
    out, stats = {"full": [], "gex": []}, Counter()
    files = sorted(glob.glob(str(home / ".swarm_results_*.json")))
    ratios = {}
    for f in files:   # 第一遍：逐标的收集「现价 / 参照收盘」比值，认出真正的拆股
        date = os.path.basename(f)[len(".swarm_results_"):-len(".json")]
        if date >= "2026-09-04":
            continue
        for t, row in json.load(open(f)).items():
            det = ((row.get("agent_details") or {}).get("OracleBeeEcho") or {}).get("details") or {}
            sp, cl = det.get("_snapshot_stock_price"), td.get(t) or {}
            prev = [d for d in cl if d <= date]
            if isinstance(sp, (int, float)) and sp > 0 and prev:
                ratios.setdefault(t, []).append(sp / cl[max(prev)])
    splits = split_factors(ratios)
    out["splits"] = splits
    for f in files:
        date = os.path.basename(f)[len(".swarm_results_"):-len(".json")]
        for t, row in json.load(open(f)).items():
            det = ((row.get("agent_details") or {}).get("OracleBeeEcho") or {}).get("details") or {}
            if not det:
                stats["no_oracle_details"] += 1
                continue
            closes = td.get(t) if date < "2026-09-04" else None
            if date < "2026-09-04" and not closes:
                stats["no_reference_close"] += 1
            kind, why = classify(det, closes, px.get((date, t)), date, splits.get(t))
            stats[why or "clean"] += 1
            if kind:
                out[kind].append([date, t, why])
    out["stats"] = dict(stats)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out["stats"], ensure_ascii=False))
    print(f"full {len(out['full'])}，gex {len(out['gex'])}")


if __name__ == "__main__":
    main()
