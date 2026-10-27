#!/usr/bin/env python3
"""v0.45.658 世代边界的等价证据：某个扫描日，`yf_daily` 末段判据会不会让「用末根当今天」的读者改口。

为什么需要这份证据
------------------
v0.45.658（652 二次检查）把 `yf.download` 读者与带日期窗口的 `history()` 读者也收进 `yf_daily`，并把末段空行按「最近一场
**已收盘**的交易日是不是缺了」判。按日期用的读者（ignore）拿到的帧与改前逐位相同（`yf_daily.download` 还原成 actions=False 的
形状，真 yfinance 代码路径对拍钉住），只有末行策略为 refuse 的读者在「最近一场缺了」时改口（当取不到）：

  · 预取包（`prefetch_market_bundle`）→ Rival 技术面 / Scout 相对强度；`_yf_close_panel` 直连 → Scout；
  · `market_intelligence._get_ma`（GuardBee 市场政体）、`get_supply_chain_signals`（Scout）；
  · `cboe_fetcher._fetch_index_level`（指数，无公司行动 ⇒ 不可能改口，列出来只为完整）。

「最近一场缺了」= 末段有公司行动空行、且剩下的末根早于最近一场已收盘的交易日。生产扫描在收盘后（14:00 PDT）跑，最近一场就是
扫描日 D；末段空行要求某个事件（分红 / 拆股 / 资本利得）日期**晚于**该票的末根 K 线。D 那根在，D 当天的事件就并进 D 那根，
不会成空行；更早的事件并进更早的 K 线。⇒ **扫描日 D 当天没有任何读者宇宙里的标的有事件 ⇒ 新旧代码在 D 输出相同**。
（还剩一种：事件日 E < D 而 E 到 D 的 K 线在 D 扫描时**全都**缺——要 Yahoo 对一只大盘股连缺多日，生产里会先在别处红；
这里按「不可能」处理并写明。）

旧代码的逐票记录里没有「这只票的预取日线末根是哪天」⇒ 证据按事件日历从外部读出来、落盘冻结（同 v0.45.652 的
`macro_rotation_equivalence_audit.py`）。

判据（每个扫描日 D）
--------------------
    equivalent                 读者宇宙里没有任何标的在 (D 的上一个交易日, D] 内有事件
    equivalent_bar_present     有事件的每只标的，当天扫描记下的现价（ScoutBee `details.price`，同一轮从 Yahoo 取）**等于 D 的收盘**
                               且不等于上一个交易日的收盘 ⇒ 扫描那一刻 D 那根 K 线在 ⇒ 事件并进了它、不成空行
                               （2026-10-20 DELL 除息：记下 461.56 = 10-20 收盘，10-19 是 455.44）
    not_proven                 其余（列出 (标的, 事件日)）——**证不出等价就不算等价**

用法（只读数据根；`--fetch` 联网取事件日历，yfinance 免费、不花钱；只写 --out）
----
    /usr/local/bin/python3 experiments/yf_daily_download_equivalence_audit.py --home ~/alpha-hive-data \\
        --from 2026-10-19 --to 2026-10-27 --fetch --out experiments/yf_daily_download_equivalence_20261019.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Dict, List

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

EVENT_COLUMNS = ("Dividends", "Stock Splits", "Capital Gains")


def universe() -> Dict[str, str]:
    """refuse 读者会碰到的全部标的 → 来自哪个读者。"""
    from config import WATCHLIST
    from fred_macro import _SECTOR_ETFS
    from market_intelligence import _SUPPLY_CHAIN
    out: Dict[str, str] = {}
    for t in WATCHLIST:
        out[t] = "watchlist（预取包 Rival / Scout、政体均线、供应链本票）"
    for e in _SECTOR_ETFS:
        out.setdefault(e, "板块 ETF（预取包 Scout 相对强度）")
    for s in _SUPPLY_CHAIN:
        out.setdefault(s, "供应链同行（Scout）")
    for s in ("^GSPC", "SOXX"):
        out.setdefault(s, "市场政体均线（Guard）")
    return dict(sorted(out.items()))


def fetch_events(symbols: List[str], start: str, pause: float = 1.0) -> Dict[str, Dict]:
    """每只标的 `history(start=, actions=True)` 里事件列非零的日期。取不到 ⇒ 记 error（该标的证不出）。"""
    import yfinance as yf
    out: Dict[str, Dict] = {}
    for i, s in enumerate(symbols):
        if i:
            time.sleep(pause)
        try:
            # 不复权：要与扫描记下的现价（原始成交价）比
            h = yf.Ticker(s).history(start=start, actions=True, auto_adjust=False)
            cols = [c for c in EVENT_COLUMNS if c in h.columns]
            ev = sorted({str(ix)[:10] for ix, row in h[cols].iterrows() if any(float(row[c] or 0) != 0 for c in cols)})
            closes = {str(ix)[:10]: round(float(c), 4) for ix, c in h["Close"].items() if c == c}
            out[s] = {"events": ev, "bars": len(h), "last_bar": str(h.index[-1])[:10] if len(h) else None,
                      "closes": closes}
        except Exception as e:  # noqa: BLE001
            out[s] = {"error": f"{type(e).__name__}: {e}"}
    return out


def scan_days(home: Path, lo: str, hi: str) -> List[str]:
    return sorted(p.name[len(".swarm_results_"):-len(".json")] for p in home.glob(".swarm_results_*.json")
                  if lo <= p.name[len(".swarm_results_"):-len(".json")] <= hi)


def _scan_price(home: Path, day: str, sym: str):
    try:
        with open(home / f".swarm_results_{day}.json", encoding="utf-8") as fh:
            row = json.load(fh).get(sym) or {}
        return ((row.get("agent_details") or {}).get("ScoutBeeNova") or {}).get("details", {}).get("price")
    except (OSError, ValueError, AttributeError):
        return None


def judge(home: Path, days: List[str], events: Dict[str, Dict]) -> Dict[str, Dict]:
    from bars_integrity import previous_completed_session
    out: Dict[str, Dict] = {}
    for d in days:
        prev = previous_completed_session(dt.date.fromisoformat(d)).isoformat()
        pairs = [(s, e) for s, rec in events.items() for e in rec.get("events", []) if prev < e <= d]
        unknown = sorted(s for s, rec in events.items() if "error" in rec)
        checks = {}
        for s, _e in pairs:
            closes = events[s].get("closes") or {}
            px, c_d, c_prev = _scan_price(home, d, s), closes.get(d), closes.get(prev)
            checks[s] = {"scan_price": px, "close_D": c_d, "close_prev": c_prev,
                         "bar_present": (isinstance(px, (int, float)) and c_d is not None and c_prev is not None
                                         and abs(px - c_d) < 0.011 and abs(c_d - c_prev) >= 0.011)}
        if unknown:
            status = "not_proven"
        elif not pairs:
            status = "equivalent"
        elif all(c["bar_present"] for c in checks.values()):
            status = "equivalent_bar_present"
        else:
            status = "not_proven"
        out[d] = {"status": status, "window": [prev, d], "event_pairs": pairs, "bar_checks": checks,
                  "unreadable_symbols": unknown}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", required=True)
    ap.add_argument("--from", dest="lo", required=True)
    ap.add_argument("--to", dest="hi", required=True)
    ap.add_argument("--fetch", action="store_true", help="联网取事件日历（不带则读 --out 里已有的 events 重判）")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    uni = universe()
    if a.fetch:
        start = (dt.date.fromisoformat(a.lo) - dt.timedelta(days=21)).isoformat()
        events = fetch_events(list(uni), start)
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    else:
        prev = json.loads(out.read_text(encoding="utf-8"))
        events, fetched_at = prev["events"], prev["events_fetched_at"]
    home = Path(a.home).expanduser()
    days = scan_days(home, a.lo, a.hi)
    doc = {"generator": "experiments/yf_daily_download_equivalence_audit.py", "version": "v0.45.658",
           "events_fetched_at": fetched_at, "universe": uni, "events": events, "days": judge(home, days, events)}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for d, rec in doc["days"].items():
        print(d, rec["status"], rec["event_pairs"] or "", rec["unreadable_symbols"] or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
