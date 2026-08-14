#!/usr/bin/env python3
"""
自攒 IV 历史（v0.43.18）
========================
真 IV Rank 需要"过去 N 天期权市场每天的隐含波动率"。免费数据源（yfinance /
CBOE 延迟报价）只暴露**当前**期权链，无法回查历史 IV，付费源（ORATS /
IVolatility / CBOE DataShop）才有。因此唯一可行路径是**自己每天记一笔**。

本模块从每日期权快照 `cache/options_snapshot_{TICKER}_{DATE}.json` 读取
`iv_raw_observed`（v0.43.18 起写入，是降级前的当日真实观测 IV），拼成真实
IV 时间序列。

⚠️ 为什么不用快照里的 `iv_current`：
扫描固定在收盘后运行 → `_market_open` 恒 False → options_analyzer 的降级
分支每次都用 `last_valid_iv` 缓存（TTL 120h），当日真实值被丢弃。实测
NVDA 76 份快照里 `iv_current` 只有 15 个不同值（≈76/5，与 120h TTL 精确
吻合）——那是一条"5 天一阶的阶梯"，拿它算 min/max 会严重失真。
`iv_raw_observed` 才是每日独立观测。

数据从 v0.43.18 上线日开始积累，此前的快照没有该字段（会被自动跳过）。
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import List, Optional, Tuple

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.iv_history")
except ImportError:  # pragma: no cover - 独立运行兜底
    import logging
    _log = logging.getLogger("alpha_hive.iv_history")

# 标准 IV Rank 用 252 个交易日（1 年）。样本不足时退回 HV 代理，绝不假装。
IV_RANK_FULL_WINDOW = 252
# 最低可用样本：63 个交易日 ≈ 3 个月，是业界公认的"季度 IV Rank"变体下限。
# 低于此值 rank 的分辨率太粗（1/N），宁可诚实降级也不出一个看着像真的假数。
IV_RANK_MIN_DAYS = 63

_SNAP_RE = re.compile(r"options_snapshot_(.+)_(\d{4}-\d{2}-\d{2})\.json$")
# IV 合理区间，与 options_analyzer 的 _MIN/_MAX_VALID_IV 保持一致
_MIN_VALID_IV = 5.0
_MAX_VALID_IV = 150.0


def load_iv_history(
    ticker: str,
    cache_dir: str,
    max_days: int = IV_RANK_FULL_WINDOW,
) -> Tuple[List[float], int]:
    """读取该标的的真实观测 IV 序列（按日期升序）。

    Args:
        ticker: 标的代码
        cache_dir: 期权快照目录（通常是 OptionsDataFetcher.cache_dir）
        max_days: 最多回看多少个交易日（默认 252）

    Returns:
        (iv_list, n_days) —— iv_list 按日期升序，每个交易日至多一条；
        n_days 是实际可用的独立观测天数。数据不足时返回 ([], 0)。
    """
    pattern = os.path.join(cache_dir, f"options_snapshot_{ticker}_*.json")
    by_date: dict = {}

    for path in glob.glob(pattern):
        m = _SNAP_RE.search(os.path.basename(path))
        if not m or m.group(1) != ticker:
            continue
        date_str = m.group(2)
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        iv = snap.get("iv_raw_observed")
        if iv is None:
            continue  # v0.43.18 之前的快照没有此字段，跳过（不退回 iv_current）
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            continue
        if not (_MIN_VALID_IV <= iv <= _MAX_VALID_IV):
            continue
        by_date[date_str] = iv  # 同日多次扫描：后写覆盖，保证一天一条

    if not by_date:
        return [], 0

    ordered = [by_date[d] for d in sorted(by_date)][-max_days:]
    return ordered, len(ordered)


def iv_rank_from_history(
    current_iv: float,
    iv_history: List[float],
) -> Tuple[Optional[float], Optional[float]]:
    """用真实 IV 历史算 IV Rank / IV Percentile。

    与 options_analyzer.calculate_iv_rank 同口径（min-max 归一），
    但分母是**真实观测 IV**而非 HV 代理。

    Returns:
        (iv_rank, iv_percentile)；样本不足或退化时返回 (None, None)，
        由调用方决定是否降级——本函数不编造中性值。
    """
    if not iv_history or len(iv_history) < 2 or current_iv is None:
        return None, None

    lo, hi = min(iv_history), max(iv_history)
    if hi <= lo:
        return None, None  # 全部相同 → 无法排名，诚实返回 None

    iv_rank = max(0.0, min(100.0, (current_iv - lo) / (hi - lo) * 100))
    below = sum(1 for v in iv_history if v < current_iv)
    iv_percentile = below / len(iv_history) * 100
    return round(iv_rank, 2), round(iv_percentile, 2)


def coverage_report(cache_dir: str) -> dict:
    """自攒进度报告：各标的已积累多少个交易日的真实 IV 观测。"""
    pattern = os.path.join(cache_dir, "options_snapshot_*.json")
    tickers: dict = {}
    for path in glob.glob(pattern):
        m = _SNAP_RE.search(os.path.basename(path))
        if not m:
            continue
        tickers.setdefault(m.group(1), None)

    out = {}
    for tk in tickers:
        _, n = load_iv_history(tk, cache_dir)
        out[tk] = n
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":  # pragma: no cover
    import sys
    _dir = sys.argv[1] if len(sys.argv) > 1 else "cache"
    rep = coverage_report(_dir)
    ready = {k: v for k, v in rep.items() if v >= IV_RANK_MIN_DAYS}
    print(f"自攒 IV 历史进度（阈值 {IV_RANK_MIN_DAYS} 个交易日）")
    print(f"  已达标: {len(ready)}/{len(rep)} 只")
    for tk, n in list(rep.items())[:15]:
        flag = "✅" if n >= IV_RANK_MIN_DAYS else f"({IV_RANK_MIN_DAYS - n} 天后可用)"
        print(f"  {tk:6s} {n:4d} 天 {flag}")
