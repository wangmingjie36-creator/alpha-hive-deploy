"""Alpha Bot「对比」页的数据面（v0.45.759）：期权结构的跨日对比——今天这次扫描 vs 上一次扫描。

来历
----
接替 Cowork 定时任务「Options compare daily」：它跑 `compare_engine_v2.py`，用正则从**深度报告 HTML**里抠数，
所以要先手动跑完 NVDA 深度报告才有输入；深度报告生成器 v0.45.724 退役后它再没有输入（最后一份产物 2026-05-20）。
同一批数的源头是每日扫描落盘的 `.swarm_results_<日期>.json`（OracleBee 的 `details`），这里直接读它——
不依赖深度报告、不止 NVDA、不用另起定时任务：扫描跑完，页面上就是最新一天。

读什么（**本模块不读文件**）
---------------------------
输入是 `alphabot/service.py` 读好的「某天某票的扫描结果」（dict）。本模块只做：抽出期权结构的读数（`day_view`）、
与上一个扫描日相减（`compare`）、按阈值挂提示（`alerts`）、排成总览行与单票详情。纯函数，不联网、不写任何东西。

没搬过来的（旧引擎里有、这里**故意不做**）
---------------------------------------
  · 「自学习准确率」「自适应权重」「置信度」「次日预测归档」——那是另一套方向预测，旧口径的准确率有已知污染；
    本页只摆读数与变化，不出方向判断、不出分数；
  · Gmail 草稿——Alpha Bot 只绑回环、不对外发东西；
  · 周度仪表盘——趋势线在单票详情里。

⚠️ 提示阈值（`RULES`）沿用旧引擎的经验值（综合分 ±1.5、P/C ±0.25、Call 重心迁移——旧版写死 $5、只对 NVDA 的价位
有意义，这里改成现价的百分比），**未经前向检验**；它们只决定「哪一行挂个提示」，不进评分、不是信号。
综合分（`final_score`）本身也没有被证实有预测力，这里照实摆出来只为看它怎么变。

口径（照扫描记录的原样，不重算）
  · P/C = `put_call_ratio`：按到期远近加权的未平仓量比（近端权重大）；`full_pc_ratio` 是全链不加权的那个，两个都给；
  · 总 OI = `total_oi`：只数 7 天以后到期的合约（避开到期日脱落造成的假骤降）；
  · IV Rank 带 `iv_rank_source`——`hv_proxy` 是拿历史波动率顶替的，不是真 IV Rank，页面照实标出；
  · 异动流 = `unusual_activity`，是 CBOE 延迟快照里「量大 / 量 OI 比高」的合约，**分不出买卖方向与开平仓**。
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

ORACLE = "OracleBeeEcho"
SCOUT = "ScoutBeeNova"

#: 提示阈值与展示条数。页面与帮助页只经 `public_rules()` 取，不另抄。
RULES = {
    "score_jump": 1.5,            # 综合分单日变化 ≥ 此值
    "pc_jump": 0.25,              # P/C 单日变化 ≥ 此值
    "iv_rank_jump": 20.0,         # IV Rank 单日变化 ≥ 此值（百分位点）；两天口径不同（source 变了）不比
    "skew_jump": 0.10,            # IV Skew 比值单日变化 ≥ 此值
    "oi_jump_pct": 15.0,          # 总 OI 单日变化 ≥ 此百分比
    "centroid_shift_pct": 2.5,    # Call 成交量重心迁移 ≥ 现价的此百分比
    "flows_shown": 12,            # 详情页列出的异动流条数（按权利金金额排）
    "levels_shown": 3,            # 支撑 / 阻力各列几档
    "history_days": 30,           # 趋势线回看的扫描日个数
}

DIRECTION_ZH = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
#: 不可得原因码 → 人话（唯一表；页面直接用 `reason_zh`）
REASONS = {
    "demo": "演示模式不读扫描结果",
    "no_scan_files": "数据根下没有任何扫描结果文件",
    "no_such_date": "没有这一天的扫描结果",
    "file_unreadable": "这一天的扫描结果文件读不了",
    "ticker_not_scanned": "这一天的扫描里没有这只标的",
    "no_options_details": "这一天这只标的没有期权分析记录",
    "no_prev": "之前没有这只标的的扫描记录，没东西可比",
}


def public_rules() -> dict:
    return dict(RULES) | {"direction_zh": dict(DIRECTION_ZH)}


def reason_zh(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    head, _, tail = str(code).partition(":")
    base = REASONS.get(head, head)
    return f"{base}（{tail}）" if tail else base


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _dig(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _levels(raw, spot: Optional[float]) -> List[dict]:
    out = []
    for lv in raw if isinstance(raw, list) else []:
        k = _num(_dig(lv, "strike"))
        if k is None:
            continue
        out.append({"strike": k, "oi": _num(lv.get("oi")),
                    "dist_pct": round((k / spot - 1) * 100, 2) if spot else None})
    return out[: int(RULES["levels_shown"])]


def _flows(raw) -> Tuple[List[dict], dict]:
    """异动流 → (按权利金金额排的前 N 条, 汇总)。side 取自记录里的 type（call_* / *_put_*），判不了的不猜、不计入两侧。"""
    rows, call_vol, put_vol, call_kv, unknown = [], 0.0, 0.0, 0.0, 0
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        typ = str(f.get("type") or "")
        side = "call" if "call" in typ else ("put" if "put" in typ else None)
        k, vol = _num(f.get("strike")), _num(f.get("volume"))
        if side is None or k is None or vol is None:
            unknown += 1
            continue
        if side == "call":
            call_vol += vol
            call_kv += k * vol
        else:
            put_vol += vol
        rows.append({"side": side, "type": typ, "strike": k, "expiry": f.get("expiry"), "volume": vol,
                     "oi": _num(f.get("oi")), "vol_oi": _num(f.get("ratio")), "otm_pct": _num(f.get("otm_pct")),
                     "premium_usd": _num(f.get("dollar_premium")), "days_to_exp": _num(f.get("days_to_exp"))})
    rows.sort(key=lambda r: -(r["premium_usd"] or 0.0))
    total = call_vol + put_vol
    summary = {"count": len(rows), "unclassified": unknown, "call_volume": call_vol, "put_volume": put_vol,
               "call_pct": round(call_vol / total * 100, 1) if total > 0 else None,
               "call_centroid": round(call_kv / call_vol, 2) if call_vol > 0 else None}
    return rows, summary


#: `day_view` 从 OracleBee details 里读的键（`slim` 只留这些：一天的结果文件近 2 MB，逐行权价 OI 表占大头，缓存时不留）
_ORACLE_KEYS = ("_snapshot_stock_price", "unusual_activity", "iv_term_structure", "data_quality", "put_call_ratio",
                "iv_rank", "iv_rank_source", "iv_current", "rv_30d", "iv_rv_spread", "iv_skew_ratio", "iv_skew_detail",
                "total_oi", "max_pain", "key_levels", "unusual_flow_ok")
_FULL_CHAIN_KEYS = ("full_pc_ratio", "total_call_oi", "total_put_oi")


def slim(result) -> Optional[dict]:
    """一只票的扫描结果 → 只留 `day_view` 要读的部分（给服务端缓存用）。`day_view(slim(x)) == day_view(x)`（测试钉住）。"""
    if not isinstance(result, dict):
        return None
    o = _dig(result, "agent_details", ORACLE, "details")
    details = {}
    if isinstance(o, dict) and o:
        details = {k: o[k] for k in _ORACLE_KEYS if k in o}
        fc = o.get("full_chain_oi")
        if isinstance(fc, dict):
            details["full_chain_oi"] = {k: fc[k] for k in _FULL_CHAIN_KEYS if k in fc}
    return {"final_score": result.get("final_score"), "direction": result.get("direction"),
            "resonance": {"resonance_detected": _dig(result, "resonance", "resonance_detected")},
            "agent_details": {ORACLE: {"details": details},
                              SCOUT: {"details": {"price": _dig(result, "agent_details", SCOUT, "details", "price")}}}}


def day_view(result) -> Optional[dict]:
    """一只标的某天的扫描结果 → 期权结构读数。没有 OracleBee 的 details ⇒ `options_available: False`（分数 / 方向仍给）。
    取不到的字段一律 None，不填默认值。"""
    if not isinstance(result, dict):
        return None
    o = _dig(result, "agent_details", ORACLE, "details")
    has = isinstance(o, dict) and bool(o)
    o = o if has else {}
    spot = _num(o.get("_snapshot_stock_price"))
    spot_source = "options_snapshot" if spot is not None else None
    if spot is None:
        spot = _num(_dig(result, "agent_details", SCOUT, "details", "price"))
        spot_source = "scout" if spot is not None else None
    flows, flow_sum = _flows(o.get("unusual_activity"))
    ts = o.get("iv_term_structure") if isinstance(o.get("iv_term_structure"), dict) else {}
    return {
        "score": _num(result.get("final_score")), "direction": result.get("direction"),
        "resonance": bool(_dig(result, "resonance", "resonance_detected")),
        "options_available": has, "options_data_quality": o.get("data_quality"),
        "spot": spot, "spot_source": spot_source,
        "pc_ratio": _num(o.get("put_call_ratio")), "full_pc_ratio": _num(_dig(o, "full_chain_oi", "full_pc_ratio")),
        "iv_rank": _num(o.get("iv_rank")), "iv_rank_source": o.get("iv_rank_source"),
        "iv_current": _num(o.get("iv_current")), "rv_30d": _num(o.get("rv_30d")),
        "iv_rv_spread": _num(o.get("iv_rv_spread")),
        "skew": _num(o.get("iv_skew_ratio")),
        "otm_put_iv": _num(_dig(o, "iv_skew_detail", "otm_put_iv")),
        "otm_call_iv": _num(_dig(o, "iv_skew_detail", "otm_call_iv")),
        "total_oi": _num(o.get("total_oi")),
        "full_call_oi": _num(_dig(o, "full_chain_oi", "total_call_oi")),
        "full_put_oi": _num(_dig(o, "full_chain_oi", "total_put_oi")),
        "max_pain": _num(_dig(o, "max_pain", "max_pain")),
        "term_shape": ts.get("shape"), "front_iv": _num(ts.get("front_iv")), "back_iv": _num(ts.get("back_iv")),
        "supports": _levels(_dig(o, "key_levels", "support"), spot),
        "resistances": _levels(_dig(o, "key_levels", "resistance"), spot),
        "flows": flows[: int(RULES["flows_shown"])], "flow_summary": flow_sum,
        "unusual_flow_ok": o.get("unusual_flow_ok"),
    }


def _delta(a: Optional[float], b: Optional[float], dp: int = 2) -> Optional[float]:
    return round(a - b, dp) if a is not None and b is not None else None


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round((a / b - 1) * 100, 2) if a is not None and b else None


def compare(today: dict, prev: Optional[dict]) -> dict:
    """两天的读数相减。任何一边缺 ⇒ 那一项 None（不当成 0 变化）。IV Rank 两天口径不同（source 不一样）⇒ 不比、
    写明 `iv_rank_comparable: False`。"""
    if not prev:
        return {"available": False}
    same_ivr = today.get("iv_rank_source") == prev.get("iv_rank_source")
    tc, pc = _dig(today, "flow_summary", "call_centroid"), _dig(prev, "flow_summary", "call_centroid")
    spot = today.get("spot")
    return {
        "available": True,
        "score": _delta(today.get("score"), prev.get("score")),
        "direction_flipped": bool(today.get("direction") and prev.get("direction")
                                  and today.get("direction") != prev.get("direction")),
        "spot_pct": _pct(today.get("spot"), prev.get("spot")),
        "pc_ratio": _delta(today.get("pc_ratio"), prev.get("pc_ratio")),
        "iv_rank": _delta(today.get("iv_rank"), prev.get("iv_rank"), 1) if same_ivr else None,
        "iv_rank_comparable": same_ivr,
        "iv_current": _delta(today.get("iv_current"), prev.get("iv_current")),
        "skew": _delta(today.get("skew"), prev.get("skew"), 3),
        "total_oi_pct": _pct(today.get("total_oi"), prev.get("total_oi")),
        "max_pain": _delta(today.get("max_pain"), prev.get("max_pain")),
        "call_pct": _delta(_dig(today, "flow_summary", "call_pct"), _dig(prev, "flow_summary", "call_pct"), 1),
        "call_centroid": _delta(tc, pc),
        "call_centroid_pct_of_spot": round((tc - pc) / spot * 100, 2) if tc is not None and pc is not None and spot else None,
    }


def alerts(today: dict, prev: Optional[dict], d: dict) -> List[dict]:
    """按 `RULES` 挂提示：`{"level": "flip" | "move", "key", "text"}`。只陈述变化，不解读成看涨 / 看跌。"""
    if not d.get("available"):
        return []
    out: List[dict] = []
    zh = lambda x: DIRECTION_ZH.get(x, x or "—")  # noqa: E731
    if d["direction_flipped"]:
        out.append({"level": "flip", "key": "direction",
                    "text": f"方向翻转：{zh(prev.get('direction'))} → {zh(today.get('direction'))}"})

    def move(key, val, limit, text):
        if val is not None and abs(val) >= limit:
            out.append({"level": "move", "key": key, "text": text})
    move("score", d["score"], RULES["score_jump"],
         f"综合分单日 {d['score']:+.2f}（{prev.get('score')} → {today.get('score')}）" if d["score"] is not None else "")
    move("pc_ratio", d["pc_ratio"], RULES["pc_jump"],
         f"P/C 单日 {d['pc_ratio']:+.2f}（{prev.get('pc_ratio')} → {today.get('pc_ratio')}）" if d["pc_ratio"] is not None else "")
    move("iv_rank", d["iv_rank"], RULES["iv_rank_jump"],
         f"IV Rank 单日 {d['iv_rank']:+.0f}（{prev.get('iv_rank')} → {today.get('iv_rank')}）" if d["iv_rank"] is not None else "")
    move("skew", d["skew"], RULES["skew_jump"],
         f"IV Skew 单日 {d['skew']:+.3f}（{prev.get('skew')} → {today.get('skew')}）" if d["skew"] is not None else "")
    move("total_oi", d["total_oi_pct"], RULES["oi_jump_pct"],
         f"总 OI 单日 {d['total_oi_pct']:+.1f}%" if d["total_oi_pct"] is not None else "")
    move("call_centroid", d["call_centroid_pct_of_spot"], RULES["centroid_shift_pct"],
         (f"Call 成交重心{'上移' if (d['call_centroid'] or 0) > 0 else '下移'} {abs(d['call_centroid'] or 0):.2f}"
          f"（现价的 {abs(d['call_centroid_pct_of_spot']):.1f}%）") if d["call_centroid_pct_of_spot"] is not None else "")
    return out


def _brief(t: str, date: str, prev_date: Optional[str], today: dict, prev: Optional[dict], gap: int) -> dict:
    d = compare(today, prev)
    al = alerts(today, prev, d)
    return {"ticker": t, "date": date, "prev_date": prev_date, "scans_between": gap,
            "score": today["score"], "direction": today["direction"], "spot": today["spot"],
            "options_available": today["options_available"],
            "pc_ratio": today["pc_ratio"], "iv_rank": today["iv_rank"], "iv_rank_source": today["iv_rank_source"],
            "skew": today["skew"], "total_oi": today["total_oi"],
            "call_pct": today["flow_summary"]["call_pct"],
            "delta": d, "alerts": al, "alert_count": len(al), "flipped": bool(d.get("direction_flipped"))}


def _prev_for(t: str, days: List[Tuple[str, dict]], idx: int) -> Tuple[Optional[str], Optional[dict], int]:
    """`days` 按日期升序、`idx` 是今天。往前找这只票**有记录**的最近一个扫描日；返回 (日期, 读数, 中间跳过了几个扫描日)。"""
    for j in range(idx - 1, -1, -1):
        v = day_view((days[j][1] or {}).get(t))
        if v is not None:
            return days[j][0], v, idx - 1 - j
    return None, None, 0


def build_overview(days: List[Tuple[str, dict]], date: str) -> dict:
    """`days` = [(日期, 那天的全部扫描结果)]，升序，最后一个是 `date`。每只票一行：今天的读数 + 相对上一个有记录的扫描日的变化。"""
    idx = next((i for i, (d, _) in enumerate(days) if d == date), None)
    if idx is None:
        return {"available": False, "reason": "no_such_date", "reason_zh": reason_zh("no_such_date"), "date": date, "rows": []}
    rows = []
    for t in sorted((days[idx][1] or {})):
        today = day_view(days[idx][1][t])
        if today is None:
            continue
        pd_, pv, gap = _prev_for(t, days, idx)
        rows.append(_brief(t, date, pd_, today, pv, gap))
    rows.sort(key=lambda r: (-int(r["flipped"]), -r["alert_count"], r["ticker"]))
    return {"available": bool(rows), "reason": None if rows else "ticker_not_scanned",
            "reason_zh": None if rows else "这一天的扫描结果里没有任何标的", "date": date,
            "prev_scan_date": days[idx - 1][0] if idx > 0 else None, "rows": rows, "rules": public_rules()}


def build_ticker(t: str, days: List[Tuple[str, dict]], date: str) -> dict:
    """单票详情：今天 / 上一次的完整读数、变化、提示、趋势（最近 `history_days` 个有这只票的扫描日）。"""
    base = {"available": False, "ticker": t, "date": date, "rules": public_rules()}
    idx = next((i for i, (d, _) in enumerate(days) if d == date), None)
    if idx is None:
        return base | {"reason": "no_such_date", "reason_zh": reason_zh("no_such_date")}
    today = day_view((days[idx][1] or {}).get(t))
    if today is None:
        return base | {"reason": "ticker_not_scanned", "reason_zh": reason_zh("ticker_not_scanned")}
    pd_, pv, gap = _prev_for(t, days, idx)
    d = compare(today, pv)
    hist = []
    for dt, res in days[: idx + 1]:
        v = day_view((res or {}).get(t))
        if v is None:
            continue
        hist.append({"date": dt, "score": v["score"], "direction": v["direction"], "spot": v["spot"],
                     "pc_ratio": v["pc_ratio"], "iv_rank": v["iv_rank"], "iv_rank_source": v["iv_rank_source"],
                     "iv_current": v["iv_current"], "skew": v["skew"], "total_oi": v["total_oi"],
                     "call_pct": v["flow_summary"]["call_pct"]})
    return base | {"available": True, "reason": None if pv else "no_prev", "reason_zh": None if pv else reason_zh("no_prev"),
                   "prev_date": pd_, "scans_between": gap, "today": today, "prev": pv, "delta": d,
                   "alerts": alerts(today, pv, d), "history": hist[-int(RULES["history_days"]):]}
