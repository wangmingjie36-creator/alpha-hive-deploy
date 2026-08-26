#!/usr/bin/env python3
"""
🐝 Alpha Hive — 云端当日快照抓取 (v0.45.26)
=============================================
在 Claude cloud routine 里每个交易日收盘后运行，把**过时不候**的当日数据
（期权链 / ATM IV 期限结构 / 全链 OI / VIX 期限结构 / P/C / SKEW / VVIX / F&G）
落盘为 JSON，由 routine 提交到 `cloud-snapshots` 分支。

为什么存在
----------
扫描连续性实测覆盖率 35%（v0.45.25），断档根因是主机关机。股价有历史 API
可补，**当日期权链没有**——所以把「抓数」从「分析」里拆出来单独保鲜：
云端只负责存住原始数据，蜂群评分照旧在本机跑（补跑时消费这些快照）。

设计约束
--------
- **零 LLM 调用、零付费 API**：只访问 CBOE / CNN / alternative.me 公开端点。
- **复用主仓库抓取层**（cboe_options / cboe_fetcher / fear_greed），不自造轮子；
  每标的网络请求 = 1 次（`_fetch_cboe_payload` 进程缓存，chain/term/oi 三个
  解析共享同一份 payload。用私有函数是刻意的：它就是缓存层的入口）。
- **产出必须可数**：manifest.json 记录成功/失败清单——「跑完了吗」永远
  核对数量，不看退出标语（见 MEMORY 静默降级三件套）。
- 幂等：同日重跑整目录覆盖。

用法
----
    python3 cloud_snapshot_fetch.py                      # 今天（PDT 业务日）
    python3 cloud_snapshot_fetch.py --out cloud_snapshots
    python3 cloud_snapshot_fetch.py --tickers NVDA,AMD   # 调试子集

退出码：0=全部成功；1=部分失败（详见 manifest）；2=完全失败/无产出。
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCHEMA_VERSION = 1


def _business_date() -> str:
    # 业务日口径与扫描一致：PDT/PST 日历日
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _degradation_check(cboe: dict) -> dict:
    """标记 market.cboe 里疑似兜底降级的段。消费端规则：**列出的段不可信**。

    判据两层：① source == 'default_fallback'（v0.45.29 起 cboe_fetcher 全部
    兜底路径都标注，权威判据）；② 等值匹配已知兜底常量（保底启发式，兜住
    旧缓存/旧版数据没有 source 键的情形；真值恰等于常量会误报，可接受）。
    背景：云沙箱里 yfinance 域名被重置，skew/vvix 必然降级——首跑实测
    vix_term=15.0/15.75/16.5、skew=120.0 与真实观测（15.70/143.27）完全不符。
    """
    sus = {}
    c = cboe or {}
    for sec in ("vix_term", "skew", "vvix", "pcce"):
        if (c.get(sec) or {}).get("source") == "default_fallback":
            sus[sec] = "explicit_default_fallback"
    vt = c.get("vix_term") or {}
    if "vix_term" not in sus and (
            vt.get("vix_spot"), vt.get("vix_1m"), vt.get("vix_3m")) == (15.0, 15.75, 16.5):
        sus["vix_term"] = "matches_known_fallback_15.0/15.75/16.5"
    if "skew" not in sus and (c.get("skew") or {}).get("skew_value") == 120.0:
        sus["skew"] = "matches_known_fallback_120.0"
    if "vvix" not in sus and (c.get("vvix") or {}).get("vvix_value") == 85.0:
        sus["vvix"] = "matches_known_fallback_85.0"
    pc = c.get("pcce") or {}
    if "pcce" not in sus and pc and pc.get("call_volume") == 0 and pc.get("put_volume") == 0:
        sus["pcce"] = "zero_volume_heuristic"
    return sus


def _fetch_one_ticker(ticker: str) -> dict:
    """单标的三件套：精选链 / ATM IV 期限结构 / 全链 OI。共享一次网络请求。"""
    import cboe_options as co

    payload = co._fetch_cboe_payload(ticker, 15)  # noqa: SLF001 —— 进程缓存入口，见模块 docstring
    if not payload:
        raise RuntimeError("CBOE payload 为空（网络/403/符号问题）")
    price = float(payload.get("current_price") or payload.get("close") or 0.0)

    out = {
        "ticker": ticker,
        "schema_version": SCHEMA_VERSION,
        "fetched_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "price_at_fetch": price,
        "price_source": "cboe_delayed",
    }
    # 三个解析各自独立失败，不连坐；哪个为 None 就如实存 None + 原因键
    chain = co.fetch_cboe_chain(ticker, price)
    out["chain"] = chain
    out["iv_term_structure"] = co.fetch_cboe_iv_term_structure(ticker, price) if price > 0 else None
    out["full_chain_oi"] = co.fetch_cboe_full_chain_oi(ticker, price) if price > 0 else None
    if chain is None:
        raise RuntimeError("chain 解析为空（payload 有但无有效合约）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="cloud_snapshots")
    ap.add_argument("--tickers", default="", help="逗号分隔子集，默认 config.WATCHLIST 全量")
    args = ap.parse_args()

    from config import WATCHLIST
    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               or sorted(WATCHLIST.keys()))

    date = _business_date()
    day_dir = os.path.join(args.out, date)
    os.makedirs(day_dir, exist_ok=True)

    t0 = time.time()
    ok, failed = [], {}

    # ── 每标的期权快照 ────────────────────────────────────────────
    for i, t in enumerate(tickers, 1):
        try:
            data = _fetch_one_ticker(t)
            path = os.path.join(day_dir, f"{t}.json")
            with open(path + ".tmp", "w") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(path + ".tmp", path)
            ok.append(t)
            print(f"  [{i:2d}/{len(tickers)}] {t} ✓  (${data['price_at_fetch']:.2f})")
        except Exception as e:  # noqa: BLE001 —— per-ticker 隔离，manifest 记明细
            failed[t] = f"{type(e).__name__}: {e}"
            print(f"  [{i:2d}/{len(tickers)}] {t} ✗  {failed[t]}", file=sys.stderr)
        time.sleep(0.5)  # 温和限速；CBOE 无认证端点，别做坏邻居

    # ── 大盘指标（VIX 期限结构 / PCCE / SKEW / VVIX / F&G）────────
    market: dict = {"schema_version": SCHEMA_VERSION,
                    "fetched_at_utc": datetime.now(ZoneInfo("UTC")).isoformat()}
    try:
        from cboe_fetcher import CBOEDailyFetcher
        market["cboe"] = CBOEDailyFetcher().fetch_all()
    except Exception as e:  # noqa: BLE001
        market["cboe"] = None
        market["cboe_error"] = f"{type(e).__name__}: {e}"
    try:
        from fear_greed import get_fear_greed
        market["fear_greed"] = get_fear_greed()
    except Exception as e:  # noqa: BLE001
        market["fear_greed"] = None
        market["fear_greed_error"] = f"{type(e).__name__}: {e}"
    market["degraded_sections"] = _degradation_check(market.get("cboe"))
    with open(os.path.join(day_dir, "market.json"), "w") as f:
        json.dump(market, f, ensure_ascii=False, indent=1)

    # ── manifest：产出数量是唯一的成功判据 ───────────────────────
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "tickers_requested": len(tickers),
        "tickers_ok": len(ok),
        "ok": ok,
        "failed": failed,
        "market_cboe_ok": market.get("cboe") is not None,
        "market_degraded_sections": sorted(market["degraded_sections"].keys()),
        "fear_greed_ok": market.get("fear_greed") is not None,
    }
    with open(os.path.join(day_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    deg = manifest["market_degraded_sections"]
    print(f"\n📦 {date}: {len(ok)}/{len(tickers)} 标的成功，"
          f"market={'✓' if manifest['market_cboe_ok'] else '✗'} "
          f"F&G={'✓' if manifest['fear_greed_ok'] else '✗'}，"
          f"{manifest['elapsed_sec']}s → {day_dir}/")
    if deg:
        print(f"   ⚠️ market 疑似兜底段（不可信）：{', '.join(deg)}")
    if failed:
        print(f"   失败：{', '.join(failed)}")

    if not ok:
        return 2
    return 1 if (failed or not manifest["market_cboe_ok"]) else 0


if __name__ == "__main__":
    sys.exit(main())
