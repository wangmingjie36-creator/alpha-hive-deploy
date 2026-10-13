"""在 10-13 开盘前抓取会随时间漂移的原始输入（yfinance 期权链成交量会在开盘后重置；ApeWisdom 是滚动 24h）。
只读外部、只写 OUT 目录。抓取时刻逐项记录。"""
import json
import os
import pickle
import sys
import time
from datetime import datetime, timezone
import requests
import yfinance as yf
OUT = sys.argv[1]
TICKERS = sys.argv[2].split(",")
os.makedirs(OUT, exist_ok=True)
log = []
def stamp(): return datetime.now(timezone.utc).isoformat()
def pace(): time.sleep(1.2)
for f in ("all-stocks", "wallstreetbets", "stocks", "investing", "options"):
    try:
        r = requests.get(f"https://apewisdom.io/api/v1.0/filter/{f}/page/1", timeout=20)
        json.dump({"fetched_at": stamp(), "status": r.status_code, "body": r.json()}, open(f"{OUT}/apewisdom_{f}.json", "w"))
        log.append((f, "ok", r.status_code))
    except Exception as e:
        log.append((f, "err", repr(e)))
for tk in TICKERS:
    rec = {"ticker": tk, "fetched_at": stamp(), "errors": {}}
    t = yf.Ticker(tk)
    try:
        rec["options"] = list(t.options); pace()
    except Exception as e:
        rec["errors"]["options"] = repr(e); rec["options"] = []
    rec["chains"] = {}
    for exp in rec["options"][:6]:
        try:
            ch = t.option_chain(exp)
            rec["chains"][exp] = {"calls": ch.calls, "puts": ch.puts, "fetched_at": stamp()}
        except Exception as e:
            rec["errors"][f"chain {exp}"] = repr(e)
        pace()
    for name, fn in (("hist_raw", lambda: t.history(start="2026-04-01", end="2026-10-14", auto_adjust=False)),
                     ("analyst_price_targets", lambda: t.analyst_price_targets),
                     ("calendar", lambda: t.calendar),
                     ("fast_info", lambda: {k: getattr(t.fast_info, k, None) for k in ("last_price", "previous_close", "regular_market_previous_close")})):
        try:
            rec[name] = fn()
        except Exception as e:
            rec["errors"][name] = repr(e)
        pace()
    pickle.dump(rec, open(f"{OUT}/yf_{tk}.pkl", "wb"))
    log.append((tk, len(rec["chains"]), list(rec["errors"])))
    print(tk, len(rec["chains"]), rec["errors"], flush=True)
json.dump(log, open(f"{OUT}/_log.json", "w"), ensure_ascii=False, indent=1, default=str)
print("DONE", stamp())
