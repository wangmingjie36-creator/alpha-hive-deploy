"""盘中成交活跃度（不分正负）——纯函数：相邻两拍之间每张合约新增的成交量 × 当前 gamma，按行权价求和。

为什么不能拿快照里的「成交量口径净 GEX」逐拍相减（v0.45.684 热力图的「按成交量 × 逐拍变化」）：
1. 那是 call − put 的**净**值：同一行权价 call、put 同时放量，增量互相抵掉，最活跃的格子反而发灰；
2. 那是「全天累计成交量 × 当前 gamma」之差：现价一动 gamma 就变，早上的成交量乘上新 gamma 也会出差值，
   一个整天没成交的行权价只因现价移过来就会亮。
⇒ 这里**先逐张合约减成交量、再乘这一拍的 gamma**，call 与 put 都按正值相加：

    活跃度(行权价 K) = Σ_{K 上的合约} (本拍成交量 − 上一拍成交量) × gamma × 100 × S² × 1%

单位与净 GEX 相同（美元 / 现价每变动 1%），读作「这段时间成交了多少 gamma 名义额」；另给不乘 gamma 的
纯成交量增量（张）做对照——gamma 加权天然让临近到期的平值合约压过其余行权价。

⚠️ 只是活跃度：分不出买卖方向、分不出开仓还是平仓（CBOE 延迟快照没有这些信息），价差 / 跨式的每条腿都算；
不是 GEXBot 的 State / Orderflow，不能据此推断做市商仓位朝哪边。只展示，不进评分、不写账本、未经前向检验。

算不出就留空、不当 0（原因码唯一表 `REASONS`，页面只翻译它）：
- 上一拍的逐合约成交量只在 Alpha Bot 进程内存里（快照只存聚合值、不存原始链）⇒ 进程启动后第一拍没有；
- 成交量按交易日累计 ⇒ 上一拍是别的日子不比；取数源变了（CBOE ↔ yfinance）不比；
- 逐合约：上一拍没有这张合约、成交量变小（数据源修正）、缺成交量、要用 gamma 时没有 gamma、身份重复 ⇒ 这张不进，计数。
  某个行权价上一张都没进 ⇒ 那格留空（有合约进了、增量为 0 才是真 0）。

本模块不 import 卖权模块（只有 `alphabot/service.py` 可以）：合约子集与 gamma 由 service 注入。
"""
from __future__ import annotations

import math
from typing import Callable, Dict, Optional

CONTRACT_MULT = 100.0
PCT = 0.01

REASONS = {
    "no_previous_in_process": "Alpha Bot 启动后的第一拍：上一拍的逐合约成交量只存在内存里，没有可比的",
    "previous_other_day": "上一拍是别的交易日（成交量按日累计，不能相减）",
    "previous_not_last_snapshot": "内存里的上一拍不是快照文件里的最后一拍，不比",
    "source_changed": "上一拍与这一拍的取数源不同（CBOE / yfinance 成交量不可比）",
    "previous_not_computed": "上一拍的活跃度没算出来（取不到原始链或出错），这一拍没有可比的",
    "no_raw_chain": "这一拍拿不到原始链（逐合约成交量）",
    "exception": "计算出错（见 Alpha Bot 日志）",
    "first_of_day": "当天第一拍，没有上一拍",
}

COUNT_KEYS = ("n_used", "n_no_previous", "n_volume_decreased", "n_no_volume", "n_no_gamma",
              "n_bad_contract", "n_duplicate_key")


def _num(x) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def contract_key(c) -> Optional[str]:
    """合约身份：有 OCC 代码就用代码（SPX 与 SPXW 同日同价同方向是两张合约，只有代码分得开），
    否则 到期日|行权价|方向。形状不对 ⇒ None。"""
    if not isinstance(c, dict):
        return None
    sym = c.get("symbol")
    if isinstance(sym, str) and sym.strip():
        return sym.strip()
    k, cp, exp = _num(c.get("strike")), c.get("cp"), c.get("expiry")
    if k is None or k <= 0 or cp not in ("C", "P") or not isinstance(exp, str) or not exp:
        return None
    return f"{exp}|{k:g}|{cp}"


def volume_map(contracts) -> Dict[str, float]:
    """{合约身份: 当日累计成交量}。身份重复的合约整组不收（不知道该信哪一行）。"""
    out: Dict[str, float] = {}
    dup = set()
    for c in contracts or []:
        key = contract_key(c)
        v = _num(c.get("volume")) if key else None
        if key is None or v is None or v < 0:
            continue
        if key in out or key in dup:
            dup.add(key)
            out.pop(key, None)
            continue
        out[key] = v
    return out


def build_view(prev_vols: Dict[str, float], contracts, S, *, gamma_fn: Callable, band: float) -> dict:
    """一个期限视图的逐行权价活跃度。`contracts` 已是该视图的子集；`gamma_fn(c, S)` → gamma 或 None。
    只收现价 ±band 内的行权价（与快照其余数据同一窗口）。

    返回 {"strikes": [[行权价, gamma 加权活跃度 | None, 成交量增量 | None, 未进的合约数]], "totals": {...}, "counts": {...}}。
    """
    Sv = _num(S)
    counts = {k: 0 for k in COUNT_KEYS}
    seen: Dict[str, int] = {}
    for c in contracts or []:
        key = contract_key(c)
        if key is not None:
            seen[key] = seen.get(key, 0) + 1
    agg: Dict[float, list] = {}          # 行权价 → [加权和, 成交量增量和, 进了几张, 没进几张]
    for c in contracts or []:
        key = contract_key(c)
        k = _num(c.get("strike")) if isinstance(c, dict) else None
        if key is None or k is None or k <= 0 or Sv is None or Sv <= 0:
            counts["n_bad_contract"] += 1
            continue
        if abs(k / Sv - 1.0) > band:
            continue
        row = agg.setdefault(k, [0.0, 0.0, 0, 0])
        why = None
        v, p = _num(c.get("volume")), prev_vols.get(key)
        if seen.get(key, 0) > 1:
            why = "n_duplicate_key"
        elif v is None or v < 0:
            why = "n_no_volume"
        elif p is None:
            why = "n_no_previous"
        elif v < p:
            why = "n_volume_decreased"
        dv = (v - p) if why is None else None
        g = None
        if why is None and dv > 0:
            g = _num(gamma_fn(c, Sv))
            if g is None or g <= 0:
                why = "n_no_gamma"
        if why is not None:
            counts[why] += 1
            row[3] += 1
            continue
        counts["n_used"] += 1
        row[2] += 1
        row[1] += dv
        if dv > 0:
            row[0] += dv * g * CONTRACT_MULT * Sv * Sv * PCT
    strikes = [[k, (w if n else None), (dv if n else None), miss]
               for k, (w, dv, n, miss) in sorted(agg.items())]
    used = [r for r in strikes if r[1] is not None]
    return {"strikes": strikes, "counts": counts,
            "totals": {"activity_usd_per_1pct": sum(r[1] for r in used) if used else None,
                       "volume_added": sum(r[2] for r in used) if used else None}}


def unavailable(reason: str, *, since_ts=None) -> dict:
    if reason not in REASONS:
        raise ValueError(f"未登记的原因码：{reason}")
    return {"available": False, "reason": reason, "since_ts": since_ts}


def compare_state(prev: Optional[dict], *, date_et: str, source: str, last_ts: Optional[str]) -> Optional[str]:
    """上一拍能不能拿来比：能 ⇒ None，不能 ⇒ 原因码。`prev` = service 内存里记的
    {"date": 美东日, "source": 取数源, "ts": 那一拍的时刻, "vols": {...}}；`last_ts` = 快照文件最后一拍的时刻。"""
    if not prev:
        return "no_previous_in_process"
    if prev.get("date") != date_et:
        return "previous_other_day"
    if prev.get("ts") != last_ts:
        return "previous_not_last_snapshot"
    if prev.get("source") != source:
        return "source_changed"
    if prev.get("vols") is None:
        return "previous_not_computed"
    return None
