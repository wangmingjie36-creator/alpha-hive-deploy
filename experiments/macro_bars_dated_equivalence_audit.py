#!/usr/bin/env python3
"""v0.45.663 世代边界的等价证据：某天 Guard 读到的宏观量，换成「按日期配对、缺值不顶数」的新代码会不会变。

为什么需要这份证据
------------------
v0.45.663 把 `fred_macro` 读 Yahoo 日线从「数第几根」改成按日期配对（`bars_integrity.session_change`），缺值不再顶数
（10Y 4.5 / 涨跌 0.0 ⇒ None / "unknown"）。经 GuardBee 宏观票（→ `RegimeWeightAdjuster` → final_score）的有三样：
  · 板块轮动的冷热三只（每天都走 Yahoo：11 只 SPDR ETF「这一场对往前第 4 个交易日」）；
  · 黄金趋势（GLD 涨跌）——实时口径通常由 Finnhub 供，不走 Yahoo；
  · 收益率曲线要的 10Y 水平——实时口径通常由财政部供；VIX 由 CBOE 供（Guard 只认 CBOE 的票）。
旧代码的逐票记录里没有「每根日线是哪天的」，所以证据按天从外部读、落盘冻结（照 v0.45.652 先例）。

判据（每个日期，只读当天日报 `macro_context` + 今天重拉的 Yahoo 日线）
--------------------------------------------------------------------
    equivalent                    ① 黄金来自 Finnhub、10Y 来自财政部（当天）、VIX 来自 CBOE、实时口径 ⇒ 这三样不经 Yahoo 日线；
                                  ② 轮动表 11 只齐全，且每只存档涨跌 = 按日期重算（今天的 Yahoo：这一场 / 往前第 4 场，
                                  `round(…, 2)` 差 ≤ 0.011），冷热三只集合相同 ⇒ 当时的 5 根窗口没有错位、按日期配对得出同一组数
                                  （当时若缺过一根，旧代码按位置会配到别的日子，存档就对不上今天按日期算的值）
    equivalent_no_yfinance_data   ① 同上；轮动表为空，且当天整轮 yfinance 0 次成功响应（`yf_gate` 计数 `calls == rate_limited`）
                                  ⇒ 一根日线都没到手，新旧代码走同一条失败路径（10-26 限流故障日；计数沿用 v0.45.652 冻结的那份，
                                  `scan_timing.json` 每天被覆盖）
    not_proven                    其余——**证不出等价就不算等价**

⚠️ 只能代表**今天**看到的 Yahoo：当时缺、事后被补齐的那根，表现为「存档对不上今天按日期算的值」⇒ not_proven（宁严）。

用法（只读数据根，联网拉 Yahoo，只写 --out）
----
    /usr/local/bin/python3 experiments/macro_bars_dated_equivalence_audit.py --home ~/alpha-hive-data \\
        --from 2026-10-19 --to 2026-10-27 --prior-gates experiments/macro_rotation_equivalence_20261019.json \\
        --out experiments/macro_bars_dated_equivalence_20261019.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bars_integrity  # noqa: E402

#: 与 `fred_macro._SECTOR_ETFS` / `SECTOR_ROTATION_SESSIONS_BACK` 相同（测试钉住）
SECTOR_ETFS = ("XLK", "XLV", "XLE", "XLF", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLC", "XLB")
SESSIONS_BACK = 4
#: 存档是 round(…, 2)；同一对收盘新旧逐位相同 ⇒ 差 ≥ 0.01 就是配了别的日子
TOL = 0.011
EQUIV_STATUSES = ("equivalent", "equivalent_no_yfinance_data")


def _hot_cold(values: Dict[str, float]):
    """与 `fred_macro._fetch_sector_rotation` 同一排法：≥ 6 只取首尾各 3。"""
    perf = sorted(values.items(), key=lambda x: x[1], reverse=True)
    if len(perf) < 6:
        return None, None
    return {e for e, _v in perf[:3]}, {e for e, _v in perf[-3:]}


def _quote_problems(day: str, mc: dict) -> List[str]:
    fs = mc.get("field_sources") or {}
    out = []
    if mc.get("as_of_mode") != "realtime":
        out.append(f"as_of_mode={mc.get('as_of_mode')!r}（不是实时口径）")
    if not str(fs.get("GLD") or "").startswith("finnhub:"):
        out.append(f"GLD 来源 {fs.get('GLD')!r}（不是 Finnhub ⇒ 黄金走了 Yahoo 日线）")
    if fs.get("TNX") != f"treasury_gov@{day}":
        out.append(f"TNX 来源 {fs.get('TNX')!r}（不是当天财政部 ⇒ 10Y 走了 Yahoo 或缺）")
    if mc.get("vix_source") != "cboe":
        out.append(f"vix_source={mc.get('vix_source')!r}（不是 CBOE）")
    return out


def judge(day: str, mc: dict, closes: Dict[str, Dict[str, float]], yf_gate: Optional[dict]) -> Dict:
    """纯函数。`mc` = 当天日报的 `macro_context`；`closes` = {ETF: {ISO 日期: 收盘}}（今天重拉的 Yahoo）；
    `yf_gate` = 当天那轮扫描的 yfinance 计数（没有就 None）。"""
    problems = _quote_problems(day, mc)
    sr = mc.get("sector_rotation") or {}
    full = sr.get("full") or {}
    base = bars_integrity.sessions_before(dt.date.fromisoformat(day), SESSIONS_BACK).isoformat()
    rec: Dict = {"sector_rotation_full": len(full), "base_session": base, "mismatches": {}, "yfinance_gate": yf_gate,
                 "data_source": mc.get("data_source"),       # 等价判据逐行核对 Guard 的 `macro_data_source` 与它一致
                 "quote_sources": {k: (mc.get("field_sources") or {}).get(k) for k in ("GLD", "TNX")}
                 | {"VIX": mc.get("vix_source")}}
    if len(full) == len(SECTOR_ETFS):
        dated = {}
        for etf, (_name, stored) in full.items():
            c = closes.get(etf) or {}
            a, b = c.get(day), c.get(base)
            if a is None or b is None:
                rec["mismatches"][etf] = f"今天的 Yahoo 缺 {day if a is None else base}"
                continue
            dated[etf] = round((a / b - 1) * 100, 2)
            if abs(stored - dated[etf]) > TOL:
                rec["mismatches"][etf] = f"存档 {stored} ≠ 按日期 {dated[etf]}"
        hot, cold = _hot_cold(dated)
        same = (hot == {h[0] for h in sr.get("hot") or []} and cold == {c[0] for c in sr.get("cold") or []})
        if rec["mismatches"] or not same:
            status, reason = "not_proven", ("逐只对不上" if rec["mismatches"] else "冷热三只不同")
        else:
            status, reason = "equivalent", ""
    elif not full and isinstance(yf_gate, dict) and isinstance(yf_gate.get("calls"), int) \
            and yf_gate.get("calls") == yf_gate.get("rate_limited"):
        status, reason = ("equivalent_no_yfinance_data",
                          f"轮动表为空且 yfinance 整轮 0 次成功响应（calls={yf_gate['calls']} = rate_limited）")
    else:
        status, reason = "not_proven", f"轮动表 {len(full)} 只（不是 {len(SECTOR_ETFS)}，也证不出整轮 yfinance 没有响应）"
    if problems:
        status, reason = "not_proven", "；".join(problems + ([reason] if reason else []))
    rec.update(status=status, reason=reason)
    return rec


def _fetch_closes(start: dt.date, end: dt.date) -> Dict[str, Dict[str, float]]:
    import yfinance as yf
    out: Dict[str, Dict[str, float]] = {}
    for etf in SECTOR_ETFS:
        h = yf.Ticker(etf).history(start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(), interval="1d")
        out[etf] = {d.isoformat(): float(v) for d, v in zip(h.index.date, h["Close"]) if math.isfinite(float(v))}
    return out


def _gate_today(home: Path, day: str) -> Optional[dict]:
    try:
        with open(home / "logs" / "scan_timing.json", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    yf = (st.get("counters") or {}).get("yfinance") if isinstance(st, dict) and st.get("date") == day else None
    return dict(yf) if isinstance(yf, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", required=True)
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--prior-gates", help="v0.45.652 冻结证据（沿用其中逐日的 yfinance_gate）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    home = Path(os.path.expanduser(a.home))
    prior = {}
    if a.prior_gates:
        with open(a.prior_gates, encoding="utf-8") as fh:
            prior = {d: v.get("yfinance_gate") for d, v in (json.load(fh).get("days") or {}).items()}
    start, end = dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end)
    closes = _fetch_closes(start - dt.timedelta(days=14), end)
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")
    days = {}
    for day in [d.isoformat() for d in bars_integrity.trading_days(start, end)]:
        p = home / f"alpha-hive-daily-{day}.json"
        if not p.exists():
            continue
        mc = (json.loads(p.read_text(encoding="utf-8")) or {}).get("macro_context") or {}
        days[day] = judge(day, mc, closes, _gate_today(home, day) or prior.get(day))
    doc = {"version": "v0.45.663", "generated_at": fetched_at, "yahoo_fetched_at": fetched_at,
           "generator": "experiments/macro_bars_dated_equivalence_audit.py", "sessions_back": SESSIONS_BACK,
           "sector_etfs": list(SECTOR_ETFS), "days": days}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    for d, v in days.items():
        print(d, v["status"], v["sector_rotation_full"], v["reason"], v["mismatches"] or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
