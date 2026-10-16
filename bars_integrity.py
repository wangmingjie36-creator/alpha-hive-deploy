#!/usr/bin/env python3
"""
日线完整性校验（v0.45.383）
==========================
回答一个此前**没人问过**的问题：拿到的日线序列，**少没少交易日**？

为什么需要
----------
2026-09-23 扫描时 yfinance 的 1y 日线缺了 09-22 那一根（今天再取已被补上，事后看不见，
靠「删掉哪根能复现原值」反推才确认）。`fetch_historical_hv` 只返回一串**没有日期**的 HV 数值，
少一根 bar 让 20 日滚动窗口整体后移一天——产出的仍是「合理的数字」，没有任何报错：

    BRK-B / DE / XOM / COST / CVX / ABBV / BILI / CRM 共 8 只 iv_rank 静默错位（DE 差近 10 点），
    BRK-B 的 rv_30d 也是这根（14.52，正确 12.55）。

覆盖率闸门 `scan_coverage_gate` 只判「字段是否非空」，缺口日它也是绿的。

设计
----
· **纯函数、不联网**：输入日期序列，输出缺了哪些交易日。交易日历复用 `is_trading_day`
  （NYSE 假日**按规则**算，不是查表，不会过期）。
· **只区分两档，且理由来自依赖关系，不是拍的阈值**：
    critical  缺口落在**最后 `critical_bars` 根**之内（或序列末端落后于上一个已完成交易日）。
              20 日滚动 HV 的**当前值**只依赖最后 21 根，这里少一根，当前 HV 就是错的——09-23 就是这种。
    minor     更早的缺口。只会挪动窗口里 ≤20 个历史 HV 点，只有恰好造出新的最大/最小值才影响 rank，
              不值得为它把当天的指标置空；但要**计数**，让人看得见。
· **不要求「今天那根」存在**：末端只要求覆盖到「上一个已完成的交易日」。收盘后几分钟 Yahoo 还没发当日 bar
  是常态（`data_pipeline._drop_forming_bar` 同源顾虑），拿它当缺口会制造假降级——降级会改分数，代价不对称。
· 交易所**临时休市**（国葬日等）不在 `is_trading_day` 的规则历里，按规则会被判成缺口。
  这不是假设：写本模块时用 29 只标的的真实日线核对，**28 只在 2025-01-09 缺一根**（卡特国葬日休市，
  第 29 只 CRCL 那时还没上市）。所以本模块带一张 `AD_HOC_CLOSURES` 小表；**不改 `is_trading_day`**——
  它还管着扫描调度，改它的语义超出本次范围（它对 2025-01-09 仍答「交易日」，待验证是否要单独处理）。
  表里没有的临时休市会让当天全部标的同时报缺口，`iv_rank` 覆盖率闸门（0.70）随即变红——
  这比悄悄用错数据好；出现了往表里补一行，并写明日期与来源。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Sequence

# 20 日滚动窗口的当前值依赖「最后 21 根」（21 根 ⇒ 20 个日收益）
DEFAULT_CRITICAL_BARS = 21

# 规则历不知道的交易所临时休市：{日期: 原因}。加一行前先用真实日线核对「大多数标的确实没有这天」。
AD_HOC_CLOSURES: Dict[dt.date, str] = {
    dt.date(2025, 1, 9): "全国哀悼日（卡特国葬），NYSE/Nasdaq 休市；2026-09-28 实测 28/29 只标的缺这天",
}


def _et_today() -> Optional[dt.date]:
    """美东当日。取不到（无 zoneinfo/tzdata）返回 None，由调用方退回本机日期。"""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # pragma: no cover - 无 tzdata
        return None


def previous_completed_session(today: Optional[dt.date] = None) -> dt.date:
    """严格早于 `today` 的最近一个交易日（`today` 本身不算——它的 bar 可能还没发出来）。"""
    from is_trading_day import is_trading_day
    d = (today or _et_today() or dt.date.today()) - dt.timedelta(days=1)
    for _ in range(15):                      # 最长连休不会超过 ~5 天，15 天兜底
        if d not in AD_HOC_CLOSURES and is_trading_day(d)[0]:
            return d
        d -= dt.timedelta(days=1)
    return d                                 # pragma: no cover - 不可达


def trading_days(start: dt.date, end: dt.date) -> List[dt.date]:
    """[start, end] 闭区间内的美股交易日。"""
    from is_trading_day import is_trading_day
    out: List[dt.date] = []
    d = start
    while d <= end:
        if d not in AD_HOC_CLOSURES and is_trading_day(d)[0]:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def find_gaps(dates: Sequence[str], *, today: Optional[dt.date] = None,
              critical_bars: int = DEFAULT_CRITICAL_BARS) -> Dict[str, List[str]]:
    """找出日线序列里缺失的交易日。

    Parameters
    ----------
    dates : 升序的 ``YYYY-MM-DD`` 日期（每根 bar 一个）。
    today : 「今天」（美东）。默认取当下；测试注入用。只用来确定末端应覆盖到哪天。
    critical_bars : 最后多少根 bar 内的缺口算 critical，见模块文档。

    Returns
    -------
    ``{"missing": [...], "critical": [...], "minor": [...]}``，都是升序 ISO 日期。
    序列为空返回三个空列表（**不是**「没有缺口」——空序列由调用方另判，本函数不替它说话）。
    """
    if not dates:
        return {"missing": [], "critical": [], "minor": []}
    have = {dt.date.fromisoformat(d) for d in dates}
    first, last = min(have), max(have)
    # 末端：至少要覆盖到上一个已完成交易日；若序列已含更晚的 bar（今天那根），就到它为止
    expected_last = max(last, previous_completed_session(today))
    expected = trading_days(first, expected_last)
    missing = [d for d in expected if d not in have]

    # critical 起点：倒数第 critical_bars 根 bar 的日期（序列短于此则从头算）
    ordered = sorted(have)
    cut = ordered[-critical_bars] if len(ordered) >= critical_bars else ordered[0]
    critical = [d for d in missing if d >= cut]
    minor = [d for d in missing if d < cut]
    iso = lambda xs: [x.isoformat() for x in xs]  # noqa: E731
    return {"missing": iso(missing), "critical": iso(critical), "minor": iso(minor)}


# ─────────────────────────────────────────────────────────────────────────────
# 阶段 2（v0.45.387）：缺口时向第二源借「缺的那一根」
# ─────────────────────────────────────────────────────────────────────────────
# 为什么不整段换成 Twelve Data：yfinance 给**复权**收盘、Twelve Data 给**未复权**收盘。
# 分红标的上 HV Rank 会差 1~3 点（2026-09-30 实测 ABBV：复权 23.42 / 未复权 21.65）。
# 所以只借缺的那一根，并按**复权因子换算回 yfinance 口径**：
#   · 复权因子在两次除息日之间是常数 ⇒ 缺失日的复权价 = 第二源收盘 × 前一根的（复权价 / 第二源收盘）；
#   · 后一根也存在时，要求它算出的因子与前一根一致——不一致说明缺口落在除息日上，
#     换算没有根据，**放弃修复**（退回置空，宁可缺数也不编一根）；
#   · 缺口在序列末端（后面没有 bar）：末根复权价 ≡ 未复权价，因子取 1；末端缺多根时因子归属分不清，
#     只在「前一根因子 ≈ 1（没有待处理的分红调整）」时才补。
# 两侧因子允许的相对差 = **可接受的补数误差上界**（补出的那根最多偏这么多）。
# 定值依据（2026-09-30，30 只标的 × 最后 21 根，逐根删除再补回，共 630 次）：两源收盘的因子噪声中位数 2.6e-8、
# 620 次成功补回的相对误差最大 9.8e-8，最终 iv_rank 偏差最大 0.01（=四舍五入）、percentile 偏差 0；
# 10 次拒补全部落在除息日（5 只标的各一次除息，每次拒相邻两天）。1e-4 比实测噪声宽 3 个数量级、不会误拒，
# 又比任何常规分红比例（≥ 数个 1e-4）窄，除息日一定被拒。
FILL_RATIO_TOL = 1e-4
FILL_MAX_NEIGHBOR_DAYS = 7     # 借因子的邻居 bar 离缺失日不能超过这么多日历日


def fill_gaps_from_reference(dates: Sequence[str], closes: Sequence[float],
                             ref_rows: Sequence[Dict], targets: Sequence[str], *,
                             tol: float = FILL_RATIO_TOL) -> Dict:
    """用第二源 `ref_rows`（``[{date, close, ...}]``，未复权）补 `targets` 里缺的交易日。

    纯函数、不联网。`dates`/`closes` 是主源（复权）序列，升序、等长。

    Returns
    -------
    ``{"dates", "closes", "filled": [iso...], "unfilled": {iso: 原因}}``——`dates`/`closes` 是插入
    补出的 bar 后的新序列（仍升序）；补不出的日期原样留在 `unfilled` 里，**不静默丢掉**。
    """
    have = {d: float(c) for d, c in zip(dates, closes)}
    ref: Dict[str, float] = {}
    for r in ref_rows or []:
        try:
            v = float(r["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if v > 0 and v == v:
            ref[str(r["date"])] = v
    orig = sorted(have)
    filled: List[str] = []
    unfilled: Dict[str, str] = {}
    tail_targets = [t for t in sorted(set(targets)) if orig and t > orig[-1]]
    day = dt.date.fromisoformat

    def _near(x: str, d: str) -> bool:
        return abs((day(d) - day(x)).days) <= FILL_MAX_NEIGHBOR_DAYS

    for d in sorted(set(targets)):
        if d in have:
            continue
        if d not in ref:
            unfilled[d] = "ref_missing"
            continue
        prev = next((x for x in reversed(orig) if x < d and x in ref and _near(x, d)), None)
        nxt = next((x for x in orig if x > d and x in ref and _near(x, d)), None)
        has_later_bar = any(x > d for x in orig)
        if prev is None:
            unfilled[d] = "no_prev_neighbor"
            continue
        ratio_p = have[prev] / ref[prev]
        if nxt is not None:
            ratio_n = have[nxt] / ref[nxt]
            if abs(ratio_n / ratio_p - 1) > tol:
                unfilled[d] = "ratio_mismatch"      # 缺口夹着除息日（或两源在邻居上不一致）
                continue
            factor = ratio_p
        elif has_later_bar:
            unfilled[d] = "no_next_neighbor"        # 后面有 bar 但第二源没有它——没法验因子
            continue
        else:                                        # 序列末端
            if len(tail_targets) == 1:
                factor = 1.0                         # 末根复权价 ≡ 未复权价
            elif abs(ratio_p - 1) <= tol:
                factor = 1.0
            else:
                unfilled[d] = "tail_ambiguous"
                continue
        have[d] = ref[d] * factor
        filled.append(d)
    out_dates = sorted(have)
    return {"dates": out_dates, "closes": [have[x] for x in out_dates],
            "filled": filled, "unfilled": unfilled}
