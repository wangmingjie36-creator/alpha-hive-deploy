#!/usr/bin/env python3
"""v0.45.383 世代边界的等价证据：某天某标的的日线序列有没有「最后 21 根内缺交易日」。

为什么需要这份证据
------------------
v0.45.383 的新代码只在「检测到日线缺口」时才改变输出（`iv_rank` 置空、Oracle 期权分走中性）。
边界想登记在 2026-09-28（新代码 09-29 才首跑）而不作废 09-28 那 30 条样本，前提是**证明 09-28 那天新旧代码
输出逐项相同 = 那天 30 只标的都没有缺口**（照 v0.45.369 先例，见 `ic_rerun_readiness._BOUNDARY_EQUIVALENCE`）。

难点：旧代码**不记日期**——归档的 Oracle 记录里没有任何字段能直接说「当时的日线缺没缺」。
所以只能**用外部数据重放**：拿当天存下的 `iv_rank` / `iv_percentile`，看「完整序列」能不能精确复现它，
以及「缺了某一根的序列」能不能同样复现它。

判据（每个 日期×标的）
--------------------
    stored   = 归档里 OracleBeeEcho.details 的 (iv_rank, iv_percentile)            ← 旧代码当天的输出
    complete = 完整日线序列按生产公式算出的 (rank, percentile)
    ambiguous = 「删掉最后 21 根里的某一根」后仍能同时复现 stored 的那些日期

    verified            complete 复现 stored（两个量都在 TOL 内），且没有任何单根缺失能同样复现它
                        ⇒ 唯一解释是「序列完整」
    day_level_inference complete 复现 stored，但**至少一个**单根缺失也能复现（典型：VKTX 的 HV 是全年最高，
                        rank=100 / percentile=(n-1)/n，删哪根都不变）⇒ 单看这只标的分辨不出；
                        **同一天**没有任何标的 MISMATCH、且 ≥ MIN_DAY_VERIFIED 只被唯一证明 ⇒ 按天推断：
                        缺口是按天发生的（2026-09-23 一天 8/22 只同时中招），这么多只都干净而唯独它有缺口的概率可忽略。
                        ⚠️ 这是**概率推断，不是直接证明**，登记时单独标出，不与 verified 混为一谈。
    mismatch            complete 复现不了 stored ⇒ 不能证明等价（可能正是缺口）
    not_applicable      iv_rank_source 不是 hv_proxy（真实 IV 历史口径不经过 fetch_historical_hv）⇒ 新代码是空操作

⚠️ 数据窗口：yfinance 的 `period="1y"` 以「运行当下」为终点。证据只在**目标日之后几天内**生成才可信（窗口起点漂移 ≤ 几根，
远早于最后 21 根，对结果无影响）；隔太久重跑会漂移，这也是证据要**落盘冻结**、不在判定时现算的原因。

用法
----
    /usr/local/bin/python3 experiments/hv_gap_equivalence_audit.py --home ~/alpha-hive-data --date 2026-09-28 \\
        --out experiments/hv_gap_equivalence_20260928.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOL = 0.011               # 两位小数四舍五入的容差
MIN_DAY_VERIFIED = 20     # 按天推断要求当天至少这么多只被唯一证明
CRITICAL_BARS = 21        # 与 bars_integrity.DEFAULT_CRITICAL_BARS 同一口径


def _hv_series(closes: List[float]) -> List[float]:
    """生产口径：20 日滚动样本标准差（ddof=1）×100×√252，取尾 252。"""
    pc = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    out = []
    for i in range(19, len(pc)):
        x = pc[i - 19:i + 1]
        m = sum(x) / 20
        out.append((sum((y - m) ** 2 for y in x) / 19) ** 0.5 * 100 * 252 ** 0.5)
    return out[-252:]


def rank_pct(closes: List[float]) -> Optional[Tuple[float, float]]:
    s = _hv_series(closes)
    if len(s) < 10:
        return None
    lo, hi, cur = min(s), max(s), s[-1]
    if hi == lo:
        return None
    return (round((cur - lo) / (hi - lo) * 100, 2),
            round(sum(1 for v in s if v < cur) / len(s) * 100, 2))


def classify(stored: Tuple[float, float], closes: List[float], dates: List[str]) -> Dict:
    """对一个标的给出 verified / ambiguous / mismatch（尚未做按天推断）。"""
    complete = rank_pct(closes)
    if complete is None:
        return {"status": "mismatch", "reason": "序列太短算不出", "stored": list(stored)}
    match = lambda v: v is not None and abs(v[0] - stored[0]) <= TOL and abs(v[1] - stored[1]) <= TOL  # noqa: E731
    if not match(complete):
        return {"status": "mismatch", "stored": list(stored), "complete": list(complete)}
    n = len(closes)
    amb = [dates[k] for k in range(n - CRITICAL_BARS, n)
           if match(rank_pct(closes[:k] + closes[k + 1:]))]
    return {"status": "ambiguous" if amb else "verified", "stored": list(stored),
            "complete": list(complete), "ambiguous_drops": amb}


def audit_day(home: Path, day: str, sleep: float = 0.15) -> Dict:
    import yfinance as yf
    sr = json.load(open(home / f".swarm_results_{day}.json", encoding="utf-8"))
    tickers: Dict[str, Dict] = {}
    for t, r in sr.items():
        det = ((r.get("agent_details") or {}).get("OracleBeeEcho") or {}).get("details") or {}
        if det.get("iv_rank_source") != "hv_proxy":
            tickers[t] = {"status": "not_applicable", "iv_rank_source": det.get("iv_rank_source")}
            continue
        sk, sp = det.get("iv_rank"), det.get("iv_percentile")
        if sk is None or sp is None:
            tickers[t] = {"status": "mismatch", "reason": "归档里 iv_rank/iv_percentile 为空"}
            continue
        h = yf.Ticker(t).history(period="1y")
        dates = [d.strftime("%Y-%m-%d") for d in h.index]
        if day not in dates:
            tickers[t] = {"status": "mismatch", "reason": f"日线里没有 {day}"}
            continue
        i = dates.index(day)
        tickers[t] = classify((sk, sp), h["Close"].tolist()[:i + 1], dates[:i + 1])
        time.sleep(sleep)
    n_ver = sum(1 for v in tickers.values() if v["status"] == "verified")
    n_mis = sum(1 for v in tickers.values() if v["status"] == "mismatch")
    for v in tickers.values():
        if v["status"] == "ambiguous":
            if n_mis == 0 and n_ver >= MIN_DAY_VERIFIED:
                v["status"] = "day_level_inference"
                v["basis"] = (f"同日 {n_ver} 只被唯一证明无缺口、0 只不一致；缺口按天发生，"
                              "此标的输出对单根缺失不敏感，单看分辨不出")
            else:
                v["status"] = "mismatch"
                v["reason"] = "输出对缺口不敏感，且当天证据不足以按天推断"
    summary: Dict[str, int] = {}
    for v in tickers.values():
        summary[v["status"]] = summary.get(v["status"], 0) + 1
    return {"tickers": tickers, "summary": summary, "n_tickers": len(tickers)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", required=True, help="含 .swarm_results_<日期>.json 的数据目录")
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    day = audit_day(Path(os.path.expanduser(a.home)), a.date)
    doc = {
        "version": "v0.45.383",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "generator": "experiments/hv_gap_equivalence_audit.py",
        "tolerance": TOL, "min_day_verified": MIN_DAY_VERIFIED, "critical_bars": CRITICAL_BARS,
        "days": {a.date: day},
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(a.date, day["summary"], "→", a.out)
    return 0 if not day["summary"].get("mismatch") else 1


if __name__ == "__main__":
    raise SystemExit(main())
