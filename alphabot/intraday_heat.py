"""盘中 GEX 热力图（行权价 × 时间）叠现价 / Zero Gamma 路径——纯函数，只排已拍好的快照。

为什么放服务端：格子取哪个值、逐拍差怎么配对、颜色截断在哪都是口径，放在一处、能被测试钉住；
前端只排版（与 `howto` / `premium` 同一分工）。

口径（常量随返回值的 `rules` 给页面，页面说明只引用它们）：
- 格子 = 某一拍里某个行权价的净 GEX（美元 / 现价每变动 1%），`basis` 选 OI 口径或成交量口径。
- `mode="delta"` = 这一拍减**上一拍同一个行权价**；任一侧缺值 ⇒ 没有格子（不当 0）。首拍没有上一拍 ⇒ 整列空。
  OI 盘中不变 ⇒ OI 口径的逐拍差只来自现价 / IV 的变化；成交量口径的逐拍差 ≈ 这段时间新增成交带来的 GEX
  （**没有方向**：CBOE 延迟快照分不出主动买卖，这不是 GEXBot 的 State / Orderflow）。
- 快照只存现价 ±`service.SNAPSHOT_STRIKE_BAND` 内的行权价 ⇒ 现价移动时边缘行权价会进出窗口，缺的格子留空。
- 颜色按 |值| 的 `CLIP_QUANTILE` 分位截断（最近秩），超出的格子按截断值上色、提示框给真值——
  否则当天最大的一两个行权价会把其余格子全压成灰。
- 行权价太密（SPX 在 ±12% 内约 450 个）⇒ 超过 `MAX_ROWS` 行时把相邻行权价按固定宽度分桶**求和**
  （GEX 按行权价可加，桶和就是这一段价格区间的净 GEX）；逐拍差先逐行权价算、再求和。每格带桶里有几个行权价。
- x 轴按拍排列，不是等距时间；相邻两拍间隔 > 中位间隔 `GAP_FACTOR` 倍的地方记在 `gaps`。

只展示：不进评分、不写任何文件、不 import 卖权模块（数据只来自 `service.intraday` 已落盘的快照）。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

CLIP_QUANTILE = 0.95
GAP_FACTOR = 2.0
MAX_ROWS = 80
VIEWS = ("le_45dte", "next_expiry")
BASES = {"oi": 1, "volume": 2}          # 快照 strikes 行 = [strike, net_gex_oi, net_gex_volume]
MODES = ("level", "delta")

REASONS = {
    "no_snapshots": "这一天没有快照",
    "need_two_snapshots": "逐拍变化至少要两张快照",
    "no_values": "快照里没有可画的行权价数值",
}


def _num(x) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def _quantile_nearest_rank(vals: list, q: float) -> float:
    s = sorted(vals)
    k = max(1, math.ceil(q * len(s)))
    return s[k - 1]


def _parse_ts(ts) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _gaps(snaps: list) -> list:
    """相邻两拍间隔 > 中位间隔 × GAP_FACTOR 的位置（下标 i 表示第 i−1 拍到第 i 拍之间）。读不出时刻的拍不参与。"""
    pts = [(i, _parse_ts(s.get("ts"))) for i, s in enumerate(snaps)]
    diffs = [(i, (b - a).total_seconds()) for (_, a), (i, b) in zip(pts, pts[1:]) if a and b]
    pos = sorted(d for _, d in diffs if d > 0)
    if len(pos) < 2:
        return []
    med = pos[(len(pos) - 1) // 2]
    return [{"index": i, "minutes": round(d / 60.0, 1)} for i, d in diffs if d > med * GAP_FACTOR]


def build(snaps: list, *, view: str = "le_45dte", basis: str = "oi", mode: str = "level") -> dict:
    if view not in VIEWS or basis not in BASES or mode not in MODES:
        raise ValueError(f"view/basis/mode 不认识：{view}/{basis}/{mode}")
    col = BASES[basis]
    out = {"view": view, "basis": basis, "mode": mode, "available": False, "reason": None,
           "times": [], "ts": [], "strikes": [], "step": None, "cells": [], "price": [], "zero_gamma": [],
           "clip": None, "max_abs": None, "n_clipped": 0, "gaps": [], "n_snapshots": len(snaps),
           "bucket": None, "n_strikes_raw": 0,
           "rules": {"clip_quantile": CLIP_QUANTILE, "gap_factor": GAP_FACTOR, "max_rows": MAX_ROWS}}
    if not snaps:
        out["reason"] = "no_snapshots"
        return out
    if mode == "delta" and len(snaps) < 2:
        out["reason"] = "need_two_snapshots"
        return out

    rows = []  # 每拍 {strike: value}
    for s in snaps:
        v = (s.get("views") or {}).get(view) or {}
        m = {}
        for r in v.get("strikes") or []:
            k = _num(r[0]) if isinstance(r, (list, tuple)) and len(r) > col else None
            if k is not None:
                m[k] = _num(r[col])
        rows.append(m)

    cells = []
    for ti, m in enumerate(rows):
        for k, val in m.items():
            if mode == "delta":
                prev = rows[ti - 1].get(k) if ti > 0 else None
                val = None if val is None or prev is None else val - prev
            if val is not None:
                cells.append([ti, k, val])

    strikes = sorted({k for m in rows for k in m})
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    step = min(diffs) if diffs else None
    bucket = None
    if step and len(strikes) > MAX_ROWS:
        bucket = step * math.ceil(((strikes[-1] - strikes[0]) / step + 1) / MAX_ROWS)
        lo = strikes[0] - step / 2
        center = lambda k: lo + (math.floor((k - lo) / bucket) + 0.5) * bucket  # noqa: E731
        agg = {}
        for ti, k, v in cells:
            key = (ti, center(k))
            tot, n = agg.get(key, (0.0, 0))
            agg[key] = (tot + v, n + 1)
        cells = [[ti, c, tot, n] for (ti, c), (tot, n) in sorted(agg.items())]
        strikes = sorted({c for _, c, _, _ in cells})
        step = bucket
    else:
        cells = [[ti, k, v, 1] for ti, k, v in cells]
    out.update({
        "times": [(s.get("ts") or "")[11:16] for s in snaps],
        "ts": [s.get("ts") for s in snaps],
        "strikes": strikes,
        "step": step,
        "bucket": bucket,
        "n_strikes_raw": len({k for m in rows for k in m}),
        "price": [_num(s.get("underlying_price")) for s in snaps],
        "zero_gamma": [_num((((s.get("views") or {}).get(view) or {}).get("zero_gamma") or {}).get("nearest"))
                       for s in snaps],
        "gaps": _gaps(snaps),
    })
    absvals = [abs(c[2]) for c in cells if c[2] != 0]
    if not absvals:
        out["reason"] = "no_values"
        return out
    clip = _quantile_nearest_rank(absvals, CLIP_QUANTILE)
    # 列：[拍, 行权价（分桶时 = 桶中心）, 真值（提示框用）, 上色用的截断值, 桶里有几个行权价]
    out["cells"] = [[ti, k, v, max(-clip, min(clip, v)), n] for ti, k, v, n in cells]
    out.update({"available": True, "clip": clip, "max_abs": max(absvals),
                "n_clipped": sum(1 for c in cells if abs(c[2]) > clip)})
    return out
