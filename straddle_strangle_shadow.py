#!/usr/bin/env python3
"""影子宽跨 vs 账本跨式：只记录 + 预注册检验（v0.45.586）。协议全文：experiments/straddle_strangle_shadow_prereg.md。

问的唯一问题
------------
在账本自己的成交与定仓约定下，**同一事件、同一入场日、同一到期日**，卖 25Δ 宽跨的盈亏与卖 ATM 跨式的盈亏有没有系统差异？
（不预设方向，双侧；不检验任何「按标的选结构」的规则——协议 §9。）

本模块分两半
------------
**记录（纯函数，由三处只记录的挂接点调用）**
  · `quote_record`：信号行的 `strangle_quote`——当日快照 `quote_set.contracts` 的 `c25` / `p25`（与跨式同一到期日）+ 点时点的历史涨跌；
    由 `earnings_vol_signal._scan_with_info` 在 `compute_signal` 返回**之后**挂上（结构上进不了任何判断）；
  · `entry_record`：账本真正开仓时，把它抄成持仓的 `shadow_strangle`，并按账本自己的 `size_contracts` 与当时的 `nav_for_sizing`
    算出假想的张数（`options_paper_leg._attach_shadow_entry`）；
  · `exit_record`：账本平仓的**同一天**，对两个符号重新报价（或沿用账本的内在价值结算），算出假想盈亏，并到平仓记录的
    `shadow_strangle` 里（`options_paper_leg._attach_shadow_exit`）。
  任何一步失败都**不影响账本**，但在源头登记（`note` → `stats()` → `scan_timing.counters()["strangle_shadow"]` → alert_manager P2）。

**检验（只读平仓记录，唯一的写是冻结结果）**
  · `units(closed)` / `progress(closed, positions)`：就绪度——**只有计数**，不返回任何盈亏、不算任何效应量；
  · `run_once(closed)`：就绪后跑**一次**分块符号翻转检验并冻结到 `PATHS.straddle_strangle_shadow_result`（`os.link` 先到者赢）。

盲期：冻结之前，任何代码路径都不把同一笔的影子宽跨盈亏与跨式盈亏放进同一个返回值（`BLINDED_KEYS` 不出现在 `progress` 里，
测试钉住）。信息论上盲不了（两个盈亏都在平仓记录里）——自己拼起来看 = 偷看 = 协议变更。

⚠️ 本模块**不 import** `options_paper_leg` / `earnings_vol_signal` / `alphabot`：定仓函数由调用方注入（`size_fn`），
这样记录函数是纯的，依赖只有一个方向（账本 → 本模块）。
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import ledger_io
from hive_logger import PATHS, get_logger

_log = get_logger("straddle_strangle_shadow")

#: 协议常量。本文件与 experiments/straddle_strangle_shadow_prereg.md 不一致时以这里为准，并按协议 §8 记修订。
PREREG = {
    "version": 1,
    "registered": "2026-10-21",
    "doc": "experiments/straddle_strangle_shadow_prereg.md",
    #: 只有 `entry_date >= start_date` 且入场当天带着影子记录的单位才进检验（协议 §0：登记前入场的 8 笔一律排除）
    "start_date": "2026-10-21",
    "unit": "closed_short_straddle_with_shadow_strangle",
    "quote_schema": 1,
    "shadow_schema": 1,
    "strangle_roles": ("c25", "p25"),
    "metric": "d = (strangle_pnl_usd - straddle_pnl_usd) / nav_for_sizing",
    "statistic": "mean(d)",
    "test": "two_sided_block_sign_flip",
    "block": "earnings_iso_week",
    "min_units": 30,
    "min_blocks": 8,
    "alpha": 0.05,
    "enum_max_blocks": 16,           # 块数 ≤ 16 ⇒ 枚举全部 2^块数；否则随机
    "n_perm": 20000,
    "seed": 20261021,
}
#: 冻结前不许出现在 `progress` 返回值里的键（任何一笔的盈亏 / 差值 / 效应量 / p 值 / 判定）
BLINDED_KEYS = frozenset({"observed", "p_value", "decision", "mean_d", "d", "pnl", "pnl_usd", "shadow_pnl",
                          "pnl_per_share", "exit_premium", "paired", "group_means"})
RESULT_NAME = "straddle_strangle_shadow_result.json"
LEG_FIELDS = ("symbol", "strike", "expiry", "bid", "ask", "mid", "spread_pct", "delta", "oi", "quote_ok")


def public_constants() -> dict:
    """给页面 / 帮助页显示的协议常量（规则只在这里维护一份）。"""
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in PREREG.items()}


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> Optional[float]:
    f = _num(v)
    return f if (f is not None and f > 0) else None


# ═══════════════════════════════ 失败在源头登记（谁会红？）

_LOCK = threading.Lock()
_ITEMS_MAX = 20
_ZERO = {"signal_recorded": 0, "signal_error": 0, "entry_ok": 0, "entry_unquotable": 0, "entry_no_signal_record": 0,
         "entry_no_size": 0, "entry_error": 0, "exit_ok": 0, "exit_unpriced": 0, "exit_error": 0}
_STATS: Dict = {**_ZERO, "items": []}
#: 这几类是**故障**（记录没写成），alert_manager 报 P2；其余是数据（不可成交 / 没有信号记录 / 平仓无价）——如实计数、不报警
ERROR_KINDS = ("signal_error", "entry_error", "exit_error")


def note(kind: str, ticker: Optional[str] = None, detail: Optional[str] = None) -> None:
    """登记一次事件。`kind` 必须是 `_ZERO` 里的键（拼错就抛，不静默记到别处）。"""
    if kind not in _ZERO:
        raise KeyError(f"未登记的影子记录事件 {kind!r}")
    with _LOCK:
        _STATS[kind] += 1
        if kind in ERROR_KINDS and len(_STATS["items"]) < _ITEMS_MAX:
            _STATS["items"].append({"kind": kind, "ticker": ticker, "detail": detail})


def stats() -> dict:
    """进程内计数（`scan_timing.counters()["strangle_shadow"]` 读它）。`errors` = 记录没写成的次数。"""
    with _LOCK:
        out = {k: _STATS[k] for k in _ZERO}
        out["items"] = list(_STATS["items"])
    out["errors"] = sum(out[k] for k in ERROR_KINDS)
    return out


def reset_stats() -> None:
    with _LOCK:
        _STATS.clear()
        _STATS.update({**_ZERO, "items": []})


# ═══════════════════════════════ 记录（纯函数）

def leg_brief(c) -> Optional[dict]:
    return {k: c.get(k) for k in LEG_FIELDS} if isinstance(c, dict) else None


def prior_moves(raw_moves, as_of: str) -> List[dict]:
    """历史财报涨跌（带符号，百分点），只取财报日**早于** as_of 的，新 → 旧；坏行丢弃（点时点：事后才发生的那次不混进事前的历史）。
    与 `alphabot.strangle_compare.prior_moves` 同一规则（测试对拍）。"""
    out = []
    for m in raw_moves or []:
        if not isinstance(m, dict):
            continue
        ed, mv = str(m.get("earnings_date") or ""), _num(m.get("move_pct"))
        if mv is None or not ed or ed >= as_of:
            continue
        out.append({"earnings_date": ed, "move_pct": mv})
    return sorted(out, key=lambda x: x["earnings_date"], reverse=True)


def quote_record(qs, as_of: str, raw_moves=None) -> dict:
    """信号行的 `strangle_quote`：当日快照里的 25Δ 两腿（与跨式同一到期日）+ 点时点的历史涨跌。取不到 ⇒ `available=False` + 原因。"""
    base = {"schema_version": PREREG["quote_schema"], "as_of": as_of, "available": False, "reason": None,
            "selected_expiry": None, "fetched_at": None, "market_open": None, "underlying_price": None,
            "call": None, "put": None, "hist_moves": prior_moves(raw_moves, as_of)}
    if not isinstance(qs, dict):
        return {**base, "reason": "no_quote_set"}
    base.update({"selected_expiry": qs.get("selected_expiry"), "fetched_at": qs.get("fetched_at"),
                 "market_open": qs.get("market_open"), "underlying_price": _pos(qs.get("underlying_price"))})
    if not qs.get("data_available"):
        return {**base, "reason": "quote_set_unavailable"}
    cs = qs.get("contracts") or {}
    call, put = leg_brief(cs.get(PREREG["strangle_roles"][0])), leg_brief(cs.get(PREREG["strangle_roles"][1]))
    base.update({"call": call, "put": put})
    if call is None or put is None:
        return {**base, "reason": "missing_leg:" + ("call" if call is None else "put")}
    return {**base, "available": True}


def _leg_fill(side: str, q, *, closing: bool) -> Optional[float]:
    """单腿成交价（账本约定）：入场 long 按 ask 买、short 按 bid 卖；出场镜像（long 按 bid 卖、short 按 ask 买回）。
    没有合格的价（缺 / ≤ 0）⇒ None。"""
    if not isinstance(q, dict):
        return None
    buy = (side == "long") != closing            # 入场 long 买、出场 short 买回
    return _pos(q.get("ask" if buy else "bid"))


def entry_fills(side: str, call, put) -> Tuple[Optional[float], Optional[float]]:
    """账本的入场成交约定（`options_paper_leg.entry_fills`，测试对拍）。任一腿缺 ⇒ (None, None)。"""
    c, p = _leg_fill(side, call, closing=False), _leg_fill(side, put, closing=False)
    return (c, p) if (c is not None and p is not None) else (None, None)


def exit_fills(side: str, call, put) -> Tuple[Optional[float], Optional[float]]:
    """账本的出场成交约定（`options_paper_leg.exit_fills`，测试对拍）。任一腿缺 ⇒ (None, None)。"""
    c, p = _leg_fill(side, call, closing=True), _leg_fill(side, put, closing=True)
    return (c, p) if (c is not None and p is not None) else (None, None)


def _band_features(S, call: dict, put: dict, premium: float, moves: List[dict]) -> dict:
    """到期盈亏平衡带相对现价的位置、历史涨跌落在带外的次数、最大腿点差（入场时点特征；日后做特征分析只读它）。"""
    lo, hi = float(put["strike"]) - premium, float(call["strike"]) + premium
    lo_pct, hi_pct = (lo / S - 1.0) * 100.0, (hi / S - 1.0) * 100.0
    spreads = [x for x in (_num(call.get("spread_pct")), _num(put.get("spread_pct"))) if x is not None]
    return {"premium": premium, "band_lo_pct": lo_pct, "band_hi_pct": hi_pct,
            "k_outside": sum(1 for m in moves if m["move_pct"] <= lo_pct or m["move_pct"] >= hi_pct), "n_hist": len(moves),
            "max_spread_frac": max(spreads) if spreads else None}


def entry_record(qrec, side: str, nav, size_fn: Callable, straddle: dict, *, as_of: Optional[str] = None) -> dict:
    """持仓的 `shadow_strangle`（入场时点）。`qrec` = 信号行的 `strangle_quote`；`size_fn(side, premium, nav) -> (张数, 原因)`
    由调用方注入账本自己的 `size_contracts`；`straddle` = 这笔跨式的入场事实
    `{"call": 腿报价, "put": 腿报价, "premium", "contracts", "underlying"}`（算点时点特征用）。
    `status`：`ok`（两腿都能按账本约定成交）/ `unquotable`（不能；原因在 `reason`，**不往后找别的行权价顶替**）/
    `no_signal_record`（信号行没带 `strangle_quote`，如部署当天先于新代码落盘的信号）/ `no_size`（定仓给不出张数）。"""
    base = {"schema_version": PREREG["shadow_schema"], "recorded_on": as_of, "side": side,
            "status": None, "reason": None, "call": None, "put": None, "premium": None,
            "nav_for_sizing": _pos(nav), "contracts": None, "contracts_reason": None, "features": None, "exit": None}
    if not isinstance(qrec, dict) or qrec.get("schema_version") != PREREG["quote_schema"]:
        return {**base, "status": "no_signal_record", "reason": "no_strangle_quote"}
    base.update({"call": qrec.get("call"), "put": qrec.get("put")})
    if not qrec.get("available"):
        return {**base, "status": "unquotable", "reason": qrec.get("reason") or "unavailable"}
    for name, leg in (("call", qrec.get("call")), ("put", qrec.get("put"))):
        if not leg.get("quote_ok"):
            return {**base, "status": "unquotable", "reason": f"quote_not_ok:{name}"}
    fc, fp = entry_fills(side, qrec["call"], qrec["put"])
    if fc is None:
        return {**base, "status": "unquotable", "reason": "no_fill_price"}
    premium = fc + fp
    n, why = size_fn(side, premium, nav)
    S = _pos(straddle.get("underlying")) or _pos(qrec.get("underlying_price"))
    moves = qrec.get("hist_moves") or []
    feats = None
    if S is not None:
        feats = {"hist_moves": moves, "strangle": _band_features(S, qrec["call"], qrec["put"], premium, moves)}
        sc, sp = straddle.get("call"), straddle.get("put")
        if isinstance(sc, dict) and isinstance(sp, dict) and _pos(straddle.get("premium")) is not None:
            feats["straddle"] = {**_band_features(S, sc, sp, float(straddle["premium"]), moves),
                                 "contracts": straddle.get("contracts")}
        feats["premium_ratio"] = premium / float(straddle["premium"]) if _pos(straddle.get("premium")) else None
    return {**base, "status": "ok" if n >= 1 else "no_size", "reason": None if n >= 1 else why,
            "premium": premium, "contracts": n if n >= 1 else None, "contracts_reason": why, "features": feats}


def exit_record(entry: dict, *, exit_date: str, basis: str, quotes: Optional[dict] = None, underlying=None) -> dict:
    """平仓记录的 `shadow_strangle`：入场记录 + `exit`（平仓当天、同一约定）。`basis` = 账本这次平仓用的价格来源：
      · `cboe_mid`：对两个符号重新报价（`quotes` = 调用方用账本同一个 `quotes_fn` 取的），long 按 bid 卖、short 按 ask 买回；
        任一腿没有合格的价 ⇒ `unpriced`（**不用内在价值顶替**：这一步手上没有平仓日的标的价；协议 §2 的实现层调整，见 v1 修订说明）；
      · `intrinsic`：账本在到期那场的收盘 S 上按内在价值结算 ⇒ 两条腿同样按 S 结算；
      · 其他（核销等）⇒ `unpriced`。
    入场不是 `ok` ⇒ 不算盈亏（`exit.status = "not_applicable"`）。"""
    out = {**entry, "exit": {"status": None, "reason": None, "exit_date": exit_date, "basis": None,
                             "exit_premium": None, "pnl_per_share": None, "pnl_usd": None}}
    ex = out["exit"]
    if entry.get("status") != "ok":
        ex.update(status="not_applicable", reason=f"entry_{entry.get('status')}")
        return out
    side, n = entry["side"], entry.get("contracts")
    call, put = entry["call"], entry["put"]
    if basis == "intrinsic":
        S = _pos(underlying)
        if S is None:
            ex.update(status="unpriced", reason="no_underlying")
            return out
        xc, xp = max(S - float(call["strike"]), 0.0), max(float(put["strike"]) - S, 0.0)
    elif basis == "cboe_mid":
        qs = quotes or {}
        xc = _leg_fill(side, qs.get(call["symbol"]), closing=True)
        xp = _leg_fill(side, qs.get(put["symbol"]), closing=True)
        if xc is None or xp is None:
            ex.update(status="unpriced", reason="no_exit_quote:" + "+".join(
                n for n, v in (("call", xc), ("put", xp)) if v is None))
            return out
    else:
        ex.update(status="unpriced", reason=f"ledger_{basis}")
        return out
    prem_out = xc + xp
    per_share = (entry["premium"] - prem_out) if side == "short" else (prem_out - entry["premium"])
    ex.update(status="ok", basis=basis, exit_premium=prem_out, pnl_per_share=per_share, pnl_usd=per_share * 100.0 * n)
    return out


# ═══════════════════════════════ 检验（只读平仓记录）

def _block(earnings_date) -> Optional[str]:
    try:
        y, w, _ = date.fromisoformat(str(earnings_date)[:10]).isocalendar()
    except (TypeError, ValueError):
        return None
    return f"{y}-W{w:02d}"


def units(closed: List[dict]) -> List[dict]:
    """协议 §4：每笔平仓一个单位，`status` 说明为什么进 / 不进检验。`d` 是结果——调用方在冻结前只许数个数。
    进检验 = `ok`：入场日 ≥ `start_date`、带着 schema 对的影子记录、空头、入场可成交、定得出张数、平仓有价。"""
    out = []
    for c in closed or []:
        sh = c.get("shadow_strangle")
        u = {"ticker": c.get("ticker"), "earnings_date": c.get("earnings_date"), "entry_date": c.get("entry_date"),
             "side": c.get("side"), "block": _block(c.get("earnings_date")), "d": None}
        if str(c.get("entry_date") or "") < PREREG["start_date"]:
            u["status"] = "before_start"
        elif not isinstance(sh, dict):
            u["status"] = "no_record"
        elif sh.get("schema_version") != PREREG["shadow_schema"]:
            u["status"] = f"schema:{sh.get('schema_version')}"
        elif c.get("side") != "short":
            u["status"] = "long_side"
        elif sh.get("status") != "ok":
            u["status"] = f"entry_{sh.get('status')}"
        else:
            ex = sh.get("exit") or {}
            nav = _pos(sh.get("nav_for_sizing"))
            a, b = _num(ex.get("pnl_usd")), _num(c.get("pnl_usd"))
            if ex.get("status") != "ok":
                u["status"] = f"exit_{ex.get('status') or 'missing'}"
            elif None in (a, b) or nav is None or u["block"] is None:
                u["status"] = "unusable"
            else:
                u["status"], u["d"] = "ok", (a - b) / nav
        out.append(u)
    return out


def progress(closed: List[dict], positions: Optional[List[dict]] = None, *, root=None) -> dict:
    """就绪度：**只有计数**。不返回 `d`、不返回任何一笔的盈亏（盲期）。"""
    us = units(closed)
    ok = [u for u in us if u["status"] == "ok"]
    blocks = {u["block"] for u in ok}
    by_status: Dict[str, int] = {}
    for u in us:
        by_status[u["status"]] = by_status.get(u["status"], 0) + 1
    open_rec = [p for p in positions or [] if isinstance(p.get("shadow_strangle"), dict)
                and str(p.get("entry_date") or "") >= PREREG["start_date"]]
    open_by: Dict[str, int] = {}
    for p in open_rec:
        k = p["shadow_strangle"].get("status") or "—"
        open_by[k] = open_by.get(k, 0) + 1
    need = {"min_units": PREREG["min_units"], "min_blocks": PREREG["min_blocks"]}
    frozen = read_result(root)
    return {"available": True, "prereg_version": PREREG["version"], "start_date": PREREG["start_date"],
            "ready": len(ok) >= need["min_units"] and len(blocks) >= need["min_blocks"], "frozen": frozen is not None,
            "n_closed": len(us), "by_status": by_status, "n_open_recorded": len(open_rec), "open_by_status": open_by,
            "n_units": len(ok), "n_blocks": len(blocks), "need": need}


def block_signflip_test(us: List[dict], *, n_perm: int, seed: int, enum_max_blocks: int) -> dict:
    """D = mean(d)；H0：d 的分布关于 0 对称。**块内所有单位共用一个符号**（同周事件被同一轮行情推着走）。
    块数 ≤ enum_max_blocks ⇒ 枚举全部 2^块数（p = #{|D_perm| ≥ |D_obs|} ÷ 2^块数，含自身）；否则随机 n_perm 次
    （p = (1 + #{|D_perm| ≥ |D_obs|}) ÷ (1 + n_perm)）。并列算「≥」（保守）。"""
    ok = [u for u in us if u.get("status") == "ok" and u.get("d") is not None and u.get("block")]
    if not ok:
        return {"testable": False, "reason": "no_units"}
    sums: Dict[str, float] = {}
    for u in ok:
        sums[u["block"]] = sums.get(u["block"], 0.0) + u["d"]
    n, order = len(ok), sorted(sums)
    S = [sums[b] for b in order]
    obs = sum(S) / n
    tol = 1e-12
    if len(S) <= enum_max_blocks:
        total, hits = 1 << len(S), 0
        for mask in range(total):
            stat = sum((-s if (mask >> i) & 1 else s) for i, s in enumerate(S)) / n
            if abs(stat) >= abs(obs) - tol:
                hits += 1
        p, mode = hits / total, "enumerate"
    else:
        rng = random.Random(seed)
        hits = 0
        for _ in range(n_perm):
            stat = sum((-s if rng.random() < 0.5 else s) for s in S) / n
            if abs(stat) >= abs(obs) - tol:
                hits += 1
        p, mode = (1 + hits) / (1 + n_perm), "random"
    return {"testable": True, "mode": mode, "observed": obs, "p_value": p, "n_units": n, "n_blocks": len(S),
            "decision": "reject_h0" if p <= PREREG["alpha"] else "fail_to_reject_h0",
            "direction": ("strangle_higher" if obs > 0 else "straddle_higher" if obs < 0 else "none")}


def _result_path(root=None) -> Path:
    """缺省 = `PATHS.straddle_strangle_shadow_result`（调用时求值）；`root` 只给测试 / 指定数据根用。"""
    if root is None:
        return PATHS.straddle_strangle_shadow_result
    return Path(root) / "options_paper_state" / RESULT_NAME


def read_result(root=None) -> Optional[dict]:
    p = _result_path(root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log.error("影子宽跨冻结结果文件读不了（不重算、不覆盖，须人工处理）：%s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_once(closed: List[dict], positions: Optional[List[dict]] = None, *, root=None, today: Optional[str] = None) -> dict:
    """就绪后跑一次并冻结；已冻结 ⇒ 原样返回那份；未就绪 ⇒ 拒绝（不算任何效应量）。"""
    done = read_result(root)
    if done is not None:
        return {**done, "already_frozen": True}
    pr = progress(closed, positions, root=root)
    if not pr["ready"]:
        return {"ran": False, "reason": "not_ready", "progress": pr}
    us = units(closed)
    res = block_signflip_test(us, n_perm=PREREG["n_perm"], seed=PREREG["seed"], enum_max_blocks=PREREG["enum_max_blocks"])
    out = {"prereg_version": PREREG["version"], "ready_date": today, "progress": pr, **res,
           "unit_keys": [[u["ticker"], u["earnings_date"], u["entry_date"]] for u in us if u["status"] == "ok"]}
    path = _result_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{RESULT_NAME}.tmp.{os.getpid()}")
    try:
        ledger_io.write_json(tmp, out)            # 原子 + 严格序列化（拒 NaN）+ 写后回读：新写入一律走 ledger_io
        os.link(tmp, path)                        # 先到者赢：已存在就抛 FileExistsError，不覆盖
    except FileExistsError:
        return {**(read_result(root) or {}), "already_frozen": True}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _log.info("影子宽跨预注册检验已冻结：%s", {k: out[k] for k in ("decision", "p_value", "direction") if k in out})
    return out


def _load(root, name: str) -> List[dict]:
    home = Path(root) if root is not None else PATHS.home
    return ledger_io.load_jsonl(home / "options_paper_state" / name)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="影子宽跨 vs 账本跨式预注册检验：缺省只看就绪度；--run-once 就绪后跑一次并冻结")
    ap.add_argument("--run-once", action="store_true")
    args = ap.parse_args(argv)
    closed, positions = _load(None, "closed_trades.jsonl"), _load(None, "positions.jsonl")
    if not args.run_once:
        print(json.dumps(progress(closed, positions), ensure_ascii=False, indent=1))
        return 0
    from hive_logger import pdt_today
    res = run_once(closed, positions, today=pdt_today())
    print(json.dumps({k: v for k, v in res.items() if k != "unit_keys"}, ensure_ascii=False, indent=1))
    return 0 if res.get("ran", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
