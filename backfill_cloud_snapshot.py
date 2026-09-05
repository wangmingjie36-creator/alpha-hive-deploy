#!/usr/bin/env python3
"""补跑单个标的的云端快照到指定业务日（在 Mac 上跑，yfinance 降级链可用）。

存在理由：`cloud_snapshot_fetch._business_date()` 取墙上时钟，事后补跑会解析成
「今天」——补 09-04 时若今天是 09-05（周六），既会建出周六目录，又会要求
vintage=09-05 这个永不成立的条件。本脚本把业务日作为参数钉死。

安全边界（三条都不可放宽）：
  1. vintage 必须等于目标业务日，否则拒绝落盘 —— 与主脚本同一判据；
  2. 只写 --ticker 指定的标的，不碰同目录下其它 29 份；
  3. price_source 如实记录实际来源（yfinance 就写 yfinance，不冒充 cboe）。

用法（务必用 3.11）：
    /usr/local/bin/python3 backfill_cloud_snapshot.py \
        --ticker TMUS --date 2026-09-04 --out cloud_snapshots

加 --dry-run 只探测 vintage、不落盘、不改 manifest。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def _load_manifest(day_dir: str) -> dict:
    with open(os.path.join(day_dir, "manifest.json")) as f:
        return json.load(f)


def _atomic_write(path: str, payload: dict, indent=None) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=indent,
                  separators=(",", ":") if indent is None else None)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--date", required=True, help="目标业务日 YYYY-MM-DD")
    ap.add_argument("--out", default="cloud_snapshots")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ticker = args.ticker.strip().upper()
    date = args.date.strip()
    day_dir = os.path.join(args.out, date)

    if not os.path.isdir(day_dir):
        print(f"✗ 目录不存在：{day_dir} —— 补跑只补已有的一天，不新建", file=sys.stderr)
        return 2

    dest = os.path.join(day_dir, f"{ticker}.json")
    if os.path.exists(dest):
        print(f"✗ {dest} 已存在 —— 拒绝覆盖已有快照", file=sys.stderr)
        return 2

    import cloud_snapshot_fetch as csf
    import cboe_options as co

    # 不清缓存会原样拿回那份陈旧 payload（同主脚本补抓 pass）
    co.invalidate_payload_cache(ticker)

    try:
        data = csf._fetch_one_ticker(ticker, date)
    except csf.StaleVintageError as e:
        print(f"✗ vintage 仍不符，拒绝落盘：{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"✗ 抓取失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # 二次确认：_fetch_one_ticker 已校验，这里防的是它日后被改松
    if data.get("vintage_date") != date:
        print(f"✗ vintage={data.get('vintage_date')} != 业务日 {date}，拒绝落盘",
              file=sys.stderr)
        return 1

    src = data.get("price_source")
    print(f"✓ {ticker} vintage={data['vintage_date']} "
          f"price={data['price_at_fetch']} source={src}")

    if args.dry_run:
        print("  (--dry-run：未落盘，未改 manifest)")
        return 0

    # 补跑的溯源信息：消费端能一眼看出这格不是当日主跑写的
    data["backfilled_at_utc"] = datetime.now(ZoneInfo("UTC")).isoformat()
    _atomic_write(dest, data)

    m = _load_manifest(day_dir)
    if ticker not in m.get("ok", []):
        m["ok"] = sorted(m.get("ok", []) + [ticker])
    m["tickers_ok"] = len(m["ok"])
    (m.get("failed") or {}).pop(ticker, None)
    m["vintage_stale"] = [t for t in m.get("vintage_stale", []) if t != ticker]
    if data.get("vintage_status") == "ok":
        m["vintage_ok"] = m.get("vintage_ok", 0) + 1
    m.setdefault("backfilled", []).append(
        {"ticker": ticker, "at_utc": data["backfilled_at_utc"], "source": src})
    _atomic_write(os.path.join(day_dir, "manifest.json"), m, indent=1)

    print(f"  已写入 {dest}")
    print(f"  manifest 更新：tickers_ok={m['tickers_ok']}/{m['tickers_requested']}，"
          f"failed={list((m.get('failed') or {}).keys()) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
