#!/usr/bin/env python3
"""
回填 signal_archive 历史 Fear & Greed 数据（v0.45.267）

`market.fear_greed` / `market.fear_greed_is_cnn` 是 v0.45.247 才加的抽取器，读的是
`agent_details.BuzzBeeWhisper.details.fear_greed.value` —— 这个结构化字段在此之前的
`.swarm_results_*.json` 里从不存在，`signal_archive.backfill()` 现有的抽取器重跑一遍
历史文件也抽不出东西（抽取器读的字段压根没写过，不是"口径变了"，是"以前没有"）。

真值另有出处：老版本 Buzz 把 F&G 写进了人类可读的 `discovery` 文本字段
（形如 "... F&G 21(Extreme Fear) ..."），是当天扫描真实用过的值——这比事后拉 CNN
官方历史序列端点更准：实测比对过，同一天两者经常相差 1~10 点（当天扫描时刻的
intraday 快照 vs CNN 历史接口的官方历史点，口径不同），直接拿 CNN 历史序列回填
会让同一个信号列前后两段用不同口径喂同一个槽位；CNN 历史序列自己还有缺口
（如 2026-03-12 当天没有历史点，但 discovery 里有真实记录）。

来源标签留白是有意的：`signal_archive._fear_greed_is_cnn` 的 docstring 已经证实
2026-03-10~13 那几天 discovery 记的其实是 Alternative.me 加密 F&G（CNN 当日
17-22，记录值却是 13/15）——直接沿用这个已核实的结论标 is_cnn=0.0。除此之外的
历史区间，老代码没留 source 字段，无法可靠判定当天走的是 CNN 还是加密备用源；
不编数据，宁可对应 is_cnn 一行不写，也不用"数值接近就当 CNN"这类猜测——
下游任何分析要用这段历史必须先接受"这段 is_cnn 未知"，不能假装知道。

一次性脚本，不进 `signal_archive.SIGNAL_EXTRACTORS`：把 legacy 文本解析塞进正式
抽取器会让它在每次全历史 `--backfill` 重跑时都被动触发，把这里的判断悄悄变成
"当前口径"的一部分。回填只做一次，写完的行此后由 `INSERT OR REPLACE` 的幂等语义
保护——现役抽取器读的是结构化字段，永远抽不到这些旧文件，不会覆盖本脚本写的值。

用法:
    python3 backfill_fear_greed_legacy.py            # dry-run，只打印
    python3 backfill_fear_greed_legacy.py --apply    # 实际写入
"""

import argparse
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("backfill_fg_legacy")

_FG_PATTERN = re.compile(r"F&G (\d+)\(([^)]+)\)")

# 已核实（见 signal_archive._fear_greed_is_cnn docstring）：这几天 discovery 记的是
# Alternative.me 加密 F&G，不是 CNN——沿用既有结论，不是本脚本新猜的。
KNOWN_CRYPTO_FALLBACK_DATES = frozenset({
    "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13",
})


def _extract_legacy_fg(snapshot: Dict) -> Optional[Tuple[int, str]]:
    """从一天的 swarm_results 快照里找任意一个标的的 discovery 文本，抽出 F&G 值。

    F&G 是当天全池同一个市场级常量，任选一个能匹配上的标的即可，不需要每个都试。
    """
    for _ticker, tr in (snapshot or {}).items():
        if not isinstance(tr, dict):
            continue
        discovery = tr.get("agent_details", {}).get("BuzzBeeWhisper", {}).get("discovery", "")
        if not isinstance(discovery, str):
            continue
        m = _FG_PATTERN.search(discovery)
        if m:
            return int(m.group(1)), m.group(2)
    return None


def find_backfill_targets(swarm_results_dir: Path, db_path: Path) -> Dict[str, Dict[str, Any]]:
    """返回 `{date: {"value", "classification", "tickers", "is_cnn"}}`。

    只包含 `signal_archive` 里已有扫描记录、但还没有 `market.fear_greed` 行、且当天
    `.swarm_results_<date>.json` 里能解析出真实 F&G 值的日期——三个条件缺一个都跳过，
    不拿别的日子顶替、不编数据。
    """
    swarm_results_dir = Path(swarm_results_dir)
    with sqlite3.connect(str(db_path)) as conn:
        existing_fg_dates = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM signal_archive WHERE signal='market.fear_greed'")
        }
        all_dates_tickers: Dict[str, list] = {}
        for date, ticker in conn.execute("SELECT DISTINCT date, ticker FROM signal_archive"):
            all_dates_tickers.setdefault(date, []).append(ticker)

    targets: Dict[str, Dict[str, Any]] = {}
    for date, tickers in all_dates_tickers.items():
        if date in existing_fg_dates:
            continue
        fp = swarm_results_dir / f".swarm_results_{date}.json"
        if not fp.exists():
            continue
        try:
            snapshot = json.loads(fp.read_text())
        except (OSError, json.JSONDecodeError) as e:
            _log.warning("跳过 %s：快照文件读取/解析失败 %s", date, e)
            continue
        found = _extract_legacy_fg(snapshot)
        if found is None:
            continue
        value, classification = found
        targets[date] = {
            "value": value,
            "classification": classification,
            "tickers": sorted(tickers),
            "is_cnn": 0.0 if date in KNOWN_CRYPTO_FALLBACK_DATES else None,
        }
    return targets


def run(swarm_results_dir: Path, db_path: Path, apply_changes: bool = False) -> Dict[str, Any]:
    targets = find_backfill_targets(swarm_results_dir, db_path)

    rows = []
    for date, info in sorted(targets.items()):
        for ticker in info["tickers"]:
            rows.append((date, ticker, "market.fear_greed", float(info["value"])))
            if info["is_cnn"] is not None:
                rows.append((date, ticker, "market.fear_greed_is_cnn", info["is_cnn"]))

    _log.info("待回填天数: %d，行数: %d", len(targets), len(rows))
    for date, info in sorted(targets.items()):
        _log.info("  %s  value=%s (%s)  is_cnn=%s  tickers=%d",
                   date, info["value"], info["classification"], info["is_cnn"],
                   len(info["tickers"]))

    if apply_changes and rows:
        with sqlite3.connect(str(db_path)) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO signal_archive (date,ticker,signal,value) "
                "VALUES (?,?,?,?)", rows)
            conn.commit()
        _log.info("已写入 %d 行", len(rows))

    return {"dates": len(targets), "rows": len(rows), "targets": targets}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run，只打印）")
    ap.add_argument("--swarm-results-dir", default=".",
                     help="`.swarm_results_*.json` 所在目录（默认当前目录）")
    ap.add_argument("--db", default=None, help="pheromone.db 路径（默认 PATHS.db）")
    args = ap.parse_args()

    from hive_logger import PATHS
    db_path = Path(args.db) if args.db else Path(PATHS.db)
    swarm_results_dir = Path(args.swarm_results_dir)

    run(swarm_results_dir, db_path, apply_changes=args.apply)
    if not args.apply:
        _log.info("dry-run 完成，确认无误后加 --apply 实际写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
