"""yfinance 期权链的 OI 是不是这一场的——全仓唯一判据（v0.45.512 起于 options_analyzer，v0.45.542 抽出共用）。

**事故。** 2026-10-12（哥伦布日）Yahoo 全市场的 openInterest 几乎清零，只剩几个月没成交的旧合约上冻结的旧数：
30 只标的「最近有成交的合约里 OI>0 的占比」全部 ≤ 0.04（9,300 个里 16 个）；同一指标在正常日（10-16 实测 30 只）是
0.81–1.00。同一天 CBOE 链的 OI 连续正常（COST / TMO / CRM / MU / WMT / XOM / NVDA 与前后几天同量级）⇒ 只是 Yahoo 一侧。

**判据。** 本链（全部到期日、**过滤之前**）以最新一笔成交为参照、往前 `RECENT_DAYS` 天内有成交的合约里，OI>0 的占比。
刚成交过的合约几乎一定有未平仓量；它们的 OI 成片是 0 ⇒ 这一场的 OI 没发布，不是市场没仓位。< `MAX_RATIO` 判陈旧；
这类合约少于 `MIN_RECENT` 个 ⇒ 判不了（`judgeable=False`，调用方照旧）。参照点是链自己的最新成交，**不读钟**。

**样本要是几个到期日合起来（v0.45.544 二次检查）。** 单个到期日会误判：10-19 正常日 SNOW 六个到期日里三个只有
0.12–0.19（新挂的周权 / 个别到期日 OI 发布不全，原因待验证），MCP 原先只看最近一个到期日 ⇒ 0.18 ⇒ 正常日判陈旧。
前 4 个到期日合起来：正常日最低 0.575（SNOW，60 天内前 4 个）、主链（DTE≥8 的 4 个）最低 0.81；10-12 故障日最高 0.04。
「任一到期日正常就不算陈旧」也不行——10-12 TMO 有一个到期日是 1.0。阈值相应从 0.5 降到 0.25（两侧余量都约 10 倍于
故障日最高值 / 约一半正常日最低值），调用方一律传多个到期日。

**读者。** 凡是从 yfinance 取期权链、又读 OI 的入口都先过这里（v0.45.542 盘点：主链 `fetch_options_chain`、全链 OI 的
yfinance 降级、`unusual_options.detect_unusual_flow`、MCP `alphahive_get_options_snapshot` 的 yfinance 路径——它取前 4 个
到期日来判，展示仍只用最近一个）。陈旧 ⇒ 由
OI 派生的量一律记不可得；只读成交量 / 价格 / IV 的部分照用。守卫 `tests/test_oi_staleness_consumers.py`。
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple

RECENT_DAYS = 4
MIN_RECENT = 20       # 最近有成交的合约少于这个数 ⇒ 判不了
MAX_RATIO = 0.25      # 其中 OI>0 的占比低于它 ⇒ 本链 OI 陈旧（v0.45.544：0.5 离正常日最低 0.575 太近）

Row = Tuple[Optional[float], float]


def rows_from_frame(df) -> List[Row]:
    """一张 yfinance 期权表（DataFrame）⇒ `[(最后成交的 epoch 秒 | None, OI), …]`。列缺失 / 空表 ⇒ []。"""
    if df is None or getattr(df, "empty", True):
        return []
    cols = getattr(df, "columns", ())
    if "openInterest" not in cols or "lastTradeDate" not in cols:
        return []
    out: List[Row] = []
    for ts, oi in zip(df["lastTradeDate"], df["openInterest"]):
        t: Optional[float] = None
        try:
            if isinstance(ts, (int, float)):
                t = float(ts) if math.isfinite(float(ts)) else None
            elif ts is not None and hasattr(ts, "timestamp"):
                t = float(ts.timestamp())
                if not math.isfinite(t):
                    t = None
        except (TypeError, ValueError, OverflowError):
            t = None   # NaT 等
        try:
            o = float(oi)
        except (TypeError, ValueError):
            o = 0.0
        out.append((t, o if math.isfinite(o) else 0.0))
    return out


def assess(rows: Iterable[Row]) -> Dict:
    """`rows` = 本链每个合约的（最后成交时刻，OI）。返回 `{recent_contracts, recent_with_oi, ratio, judgeable, stale}`；
    `stale` 只在 `judgeable` 时可能为真。"""
    rows = list(rows)
    ts = [t for t, _ in rows if t is not None]
    out = {"recent_contracts": 0, "recent_with_oi": 0, "ratio": None, "judgeable": False, "stale": False}
    if not ts:
        return out
    cut = max(ts) - RECENT_DAYS * 86400
    recent = [oi for t, oi in rows if t is not None and t >= cut]
    n, k = len(recent), sum(1 for oi in recent if oi > 0)
    out.update(recent_contracts=n, recent_with_oi=k, ratio=round(k / n, 4) if n else None)
    if n >= MIN_RECENT:
        out["judgeable"] = True
        out["stale"] = (k / n) < MAX_RATIO
    return out


def assess_frames(frames: Iterable) -> Dict:
    """多张表（各到期日的 calls / puts，过滤之前）合起来判。"""
    rows: List[Row] = []
    for df in frames:
        rows.extend(rows_from_frame(df))
    return assess(rows)
