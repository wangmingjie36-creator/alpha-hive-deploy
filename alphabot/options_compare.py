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
  · **全链** P/C（`full_pc_ratio`）与**全链** OI（call + put 合计）是总览列、变化与提示的口径：覆盖全部到期日，日间可比；
  · **近端窗口** P/C（`put_call_ratio`）与窗口 OI（`total_oi`）只在详情页并排摆出、**不挂提示**：它们只数扫描主链选中的
    那 4 个到期日（8 个日历日之后最近的 4 个）。月度到期日（第三个周五，OI 是周度的好几倍）进出这个窗口时，窗口 OI 会
    跳 3～5 倍、窗口 P/C 也跟着变，那是窗口换了、不是持仓变了（2026-11-11 实测：NVDA 10-08 → 10-09 窗口 OI 171 万 → 47 万，
    同两天全链 OI 1081 万 → 1078 万；10 / 11 月全部标的 836 个相邻扫描日里，窗口 OI 变化 ≥ 15% 的有 184 个，全链的 34 个）；
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
    "pc_jump": 0.25,              # 全链 P/C 单日变化 ≥ 此值
    "iv_rank_jump": 20.0,         # IV Rank 单日变化 ≥ 此值（百分位点）；两天口径不同（source 变了）不比
    "skew_jump": 0.10,            # IV Skew 比值单日变化 ≥ 此值
    "oi_jump_pct": 15.0,          # 全链 OI 单日变化 ≥ 此百分比
    "centroid_shift_pct": 2.5,    # Call 成交量重心迁移 ≥ 现价的此百分比
    "flows_shown": 12,            # 详情页列出的异动流条数（按权利金金额排）
    "levels_shown": 3,            # 支撑 / 阻力各列几档
    "history_days": 30,           # 趋势线回看的扫描日个数
    # ── 异动流按到期日的逐层说明（v0.45.769）
    "expiry_flows_shown": 5,      # 每个到期日列出几笔（按权利金金额排）
    "flow_lean_strong": 3.0,      # 一侧权利金 ≥ 另一侧的此倍数 ⇒「以 X 为主」
    "flow_lean_mild": 1.5,        # ≥ 此倍数 ⇒「偏 X」；不到 ⇒「两边接近」
    "flow_single_share": 40.0,    # 单笔占本到期日权利金 ≥ 此百分比 ⇒ 点名
    "flow_avg_shift_pct": 1.0,    # Call 加权行权价与上一个到期日相差 ≥ 此百分比才提差多少
}

#: 异动类型 → 展示名。判据是扫描记的 `type`；**它只说是 Call 还是 Put、成交方式，不知道是买入还是卖出**
FLOW_TYPE_ZH = {"large_call_volume": "大额 Call 成交", "large_put_volume": "大额 Put 成交",
                "call_sweep": "Call 扫单", "put_sweep": "Put 扫单"}

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


def _lean(call_p: float, put_p: float) -> Tuple[str, Optional[float]]:
    """两侧权利金 → (倾向标签, 大的一侧是小的一侧的几倍)。标签只说哪一侧的钱多，不说看涨看跌。"""
    if call_p <= 0 and put_p <= 0:
        return "none", None
    if put_p <= 0:
        return "call_only", None
    if call_p <= 0:
        return "put_only", None
    big, side = (call_p / put_p, "call") if call_p >= put_p else (put_p / call_p, "put")
    if big >= RULES["flow_lean_strong"]:
        return f"{side}_strong", round(big, 1)
    if big >= RULES["flow_lean_mild"]:
        return f"{side}_mild", round(big, 1)
    return "balanced", round(big, 1)


LEAN_ZH = {"call_only": "只有 Call", "put_only": "只有 Put", "call_strong": "以 Call 为主", "put_strong": "以 Put 为主",
           "call_mild": "偏 Call", "put_mild": "偏 Put", "balanced": "两边接近", "none": "—"}


def flow_by_expiry(raw, spot: Optional[float] = None, resistances: Optional[List[dict]] = None) -> dict:
    """异动流按到期日分层（v0.45.769，纯函数）：每层给两侧笔数 / 权利金、占全部异动权利金的比例、哪一侧的钱多，
    以及几条**能从这批记录直接算出来**的说明（`facts`）。

    ⚠️ 只陈述算得出的量：倍数、占比、行权价范围、与上一层的差。**不写**「鲸鱼式定向押注」「市场信念高度集中」这类判断，
    也不把 Call 多写成看涨——记录不分买卖方向（`bullish` 字段只是按 Call / Put 贴的），买 Call 与卖 Call 在这里长得一样。
    没有权利金金额的记录不参与金额类的量（计入 `missing_premium`），判不出 Call / Put 的不计入两侧（`unclassified`）。"""
    rows, unknown, no_prem = [], 0, 0
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        typ = str(f.get("type") or "")
        side = "call" if "call" in typ else ("put" if "put" in typ else None)
        k, exp = _num(f.get("strike")), f.get("expiry")
        if side is None or k is None or not isinstance(exp, str) or not exp:
            unknown += 1
            continue
        prem = _num(f.get("dollar_premium"))
        if prem is None or prem < 0:
            no_prem += 1
            prem = None
        rows.append({"side": side, "type": typ, "type_zh": FLOW_TYPE_ZH.get(typ, typ), "strike": k, "expiry": exp,
                     "volume": _num(f.get("volume")), "premium_usd": prem, "otm_pct": _num(f.get("otm_pct")),
                     "days_to_exp": _num(f.get("days_to_exp"))})
    total = sum(r["premium_usd"] or 0.0 for r in rows)
    res = sorted(x["strike"] for x in (resistances or []) if isinstance(x, dict) and _num(x.get("strike")) is not None
                 and (spot is None or x["strike"] > spot))
    near_res = res[0] if res else None
    layers: List[dict] = []
    prev: Optional[dict] = None
    for exp in sorted({r["expiry"] for r in rows}):
        grp = [r for r in rows if r["expiry"] == exp]
        calls, puts = [r for r in grp if r["side"] == "call"], [r for r in grp if r["side"] == "put"]
        cp, pp = sum(r["premium_usd"] or 0.0 for r in calls), sum(r["premium_usd"] or 0.0 for r in puts)
        lean, times = _lean(cp, pp)
        ranked = sorted(grp, key=lambda r: -(r["premium_usd"] or 0.0))
        cw = sum((r["premium_usd"] or 0.0) for r in calls)
        call_avg = round(sum(r["strike"] * (r["premium_usd"] or 0.0) for r in calls) / cw, 2) if cw > 0 else None
        dte = next((r["days_to_exp"] for r in grp if r["days_to_exp"] is not None), None)
        layer = {"expiry": exp, "days_to_exp": dte, "n_call": len(calls), "n_put": len(puts),
                 "call_premium": cp, "put_premium": pp, "lean": lean, "lean_zh": LEAN_ZH[lean], "times": times,
                 "share_pct": round((cp + pp) / total * 100, 1) if total > 0 else None,
                 "call_strike_lo": min((r["strike"] for r in calls), default=None),
                 "call_strike_hi": max((r["strike"] for r in calls), default=None),
                 "call_strike_avg": call_avg,
                 "top": ranked[: int(RULES["expiry_flows_shown"])],
                 "n_more": max(0, len(grp) - int(RULES["expiry_flows_shown"]))}
        facts: List[str] = []
        if layer["share_pct"] is not None:
            facts.append(f"占全部异动权利金的 {layer['share_pct']:.0f}%" if layer["share_pct"] >= 1 else "占全部异动权利金不到 1%")
        if lean in ("call_only", "put_only"):
            facts.append(f"这个到期日的异动{LEAN_ZH[lean]}")
        elif times is not None:
            a, b = ("Call", "Put") if cp >= pp else ("Put", "Call")
            facts.append(f"{a} 权利金是 {b} 的 {times:.1f} 倍" if lean != "balanced" else f"{a} 权利金是 {b} 的 {times:.1f} 倍，两边接近")
        if prev is not None and cp > 0 and pp > 0 and prev["call_premium"] > 0 and prev["put_premium"] > 0:
            r0, r1 = prev["call_premium"] / prev["put_premium"], cp / pp
            facts.append(f"Call 对 Put 的倍数：上一个到期日 {r0:.1f}，这里 {r1:.1f}")
        if ranked and (cp + pp) > 0 and (ranked[0]["premium_usd"] or 0.0) / (cp + pp) * 100 >= RULES["flow_single_share"] and len(grp) > 1:
            t0 = ranked[0]
            facts.append(f"单笔 {t0['type_zh']} ${t0['strike']:g} 占本到期日权利金 "
                         f"{(t0['premium_usd'] or 0.0) / (cp + pp) * 100:.0f}%，其余 {len(grp) - 1} 笔合计 "
                         f"{100 - (t0['premium_usd'] or 0.0) / (cp + pp) * 100:.0f}%")
        if calls:
            lo, hi = layer["call_strike_lo"], layer["call_strike_hi"]
            rng = f"Call 异动的行权价在 ${lo:g}" + (f"–${hi:g}" if hi != lo else "")
            if near_res is not None:
                above = sum(1 for r in calls if r["strike"] >= near_res)
                rng += f"；{len(calls)} 笔里 {above} 笔在最近的阻力位 ${near_res:g} 或之上"
            facts.append(rng)
        if call_avg is not None:
            line = f"Call 按权利金加权的行权价 ${call_avg:,.2f}"
            pa = prev["call_strike_avg"] if prev is not None else None
            # 差不到 1% 的不提（几毛钱的差是噪声，写出来像是有含义）
            if pa and abs(call_avg / pa - 1) * 100 >= RULES["flow_avg_shift_pct"]:
                line += f"，比上一个到期日{'高' if call_avg > pa else '低'} ${abs(call_avg - pa):,.2f}"
            facts.append(line)
        if prev is not None and (prev["call_premium"] + prev["put_premium"]) > 0 and (cp + pp) > 0:
            k = (cp + pp) / (prev["call_premium"] + prev["put_premium"])
            facts.append(f"权利金合计是上一个到期日的 {k:.1f} 倍" if k >= 1 else
                         f"权利金合计是上一个到期日的 {k * 100:.0f}%" if k >= 0.01 else "权利金合计不到上一个到期日的 1%")
        layer["facts"] = facts
        layers.append(layer)
        prev = layer
    tc, tp = sum(x["call_premium"] for x in layers), sum(x["put_premium"] for x in layers)
    lean, times = _lean(tc, tp)
    return {"count": len(rows), "unclassified": unknown, "missing_premium": no_prem,
            "n_call": sum(x["n_call"] for x in layers), "n_put": sum(x["n_put"] for x in layers),
            "call_premium": tc, "put_premium": tp, "lean": lean, "lean_zh": LEAN_ZH[lean], "times": times,
            "nearest_resistance": near_res, "layers": layers}


#: 对比页每个读数「怎么读」（v0.45.769）。只讲这个数是什么、高低各意味着什么量变，不讲该怎么交易
METRIC_READS = {
    "score": "综合分 0–10，由各维度分数按权重合成。它没有被证实有预测力，只用来在名单里排先后。",
    "spot": "现价取自期权快照那一刻；涨跌幅是对上一次扫描的。",
    "full_pc_ratio": "全链 P/C = 全部到期日的 Put 未平仓 ÷ Call 未平仓。大于 1 是 Put 更多。Put 多可能是买保护，也可能是卖 Put 收权利金，单看这个数分不出方向。",
    "full_oi": "全链 OI = 全部到期日的未平仓合约数。增加是新开的仓多于平掉的；月度到期日当天会因合约到期而明显减少。",
    "iv_rank": "IV Rank = 当前波动率在过去一年最低到最高之间的位置（0–100）。标着「HV 代」时用的是已实现波动率的排名，真 IV 历史攒够 63 个交易日后自动切换。",
    "iv_rv": "IV 是期权隐含的年化波动率，RV30 是过去 30 天实际走出来的。IV 高于 RV 是期权定价比近期实际波动贵，差得越多越贵。",
    "skew": "IV Skew = 虚值 Put 的隐含波动率 ÷ 虚值 Call 的。大于 1 是下行保护比上行期权贵，是多数股票的常态；数值升高是这个差距在拉大。",
    "max_pain": "Max Pain = 让近 7 天到期的期权买方合计亏得最多的那个价位。它没有可靠的预测力，只当作未平仓集中在哪里的一个参照。",
    "window": "「近端窗口」的两项只数扫描选中的 4 个到期日，月度到期日进出窗口时会大幅跳变，所以不算变化。",
}


def readings(today: dict, prev: Optional[dict], d: dict) -> List[dict]:
    """对比页的逐项解读（纯函数）：`[{key, label, now, change, read}]`。`now` / `change` 只陈述这次的数和它相对上一次的变化，
    取不到就是 None（页面写「—」）；`read` 是固定的「怎么读」。不把任何变化解读成看涨 / 看跌。"""
    prev = prev or {}
    ok = bool(d.get("available"))

    def chg(val, fmt, up, down, flat="与上一次相同"):
        if not ok or val is None:
            return None
        if val == 0:
            return flat
        return (up if val > 0 else down).format(fmt.format(abs(val)))
    out = []

    def add(key, label, now, change):
        out.append({"key": key, "label": label, "now": now, "change": change, "read": METRIC_READS[key]})
    sc = today.get("score")
    add("score", "综合分", f"{sc:.2f}" if sc is not None else None, chg(d.get("score"), "{:.2f}", "比上一次高 {}", "比上一次低 {}"))
    sp = today.get("spot")
    add("spot", "现价", f"{sp:,.2f}" if sp is not None else None, chg(d.get("spot_pct"), "{:.2f}%", "比上一次涨 {}", "比上一次跌 {}"))
    pc = today.get("full_pc_ratio")
    add("full_pc_ratio", "P/C（全链）", f"{pc:.2f}（Put 未平仓{'多于' if pc > 1 else '少于' if pc < 1 else '等于'} Call）" if pc is not None else None,
        chg(d.get("full_pc_ratio"), "{:.2f}", "比上一次高 {}：Put 相对 Call 变多了", "比上一次低 {}：Put 相对 Call 变少了"))
    oi = today.get("full_oi")
    add("full_oi", "全链 OI", f"{oi / 1e6:.2f}M 张" if oi is not None else None,
        chg(d.get("full_oi_pct"), "{:.1f}%", "比上一次多 {}", "比上一次少 {}"))
    ivr = today.get("iv_rank")
    src = "（已实现波动率的排名）" if today.get("iv_rank_source") == "hv_proxy" else ""
    ivr_now = None if ivr is None else (f"{ivr:.0f}{src}" + ("：等于过去一年的最高值" if ivr >= 100 else "：等于过去一年的最低值" if ivr <= 0 else ""))
    add("iv_rank", "IV Rank", ivr_now,
        "两次的口径不同，不比" if ok and d.get("iv_rank_comparable") is False
        else chg(d.get("iv_rank"), "{:.0f} 点", "比上一次高 {}", "比上一次低 {}"))
    iv, rv = today.get("iv_current"), today.get("rv_30d")
    add("iv_rv", "IV / RV30",
        f"{iv:.1f} / {rv:.1f}：IV 是 RV 的 {iv / rv:.2f} 倍" if iv is not None and rv else (f"{iv:.1f} / —" if iv is not None else None),
        chg(d.get("iv_current"), "{:.2f}", "IV 比上一次高 {}", "IV 比上一次低 {}", "IV 与上一次相同"))
    sk = today.get("skew")
    add("skew", "IV Skew", f"{sk:.3f}（虚值 Put 的 IV {'高于' if sk > 1 else '低于' if sk < 1 else '等于'}虚值 Call）" if sk is not None else None,
        chg(d.get("skew"), "{:.3f}", "比上一次高 {}：差距拉大", "比上一次低 {}：差距缩小"))
    mp = today.get("max_pain")
    mp_now = None
    if mp is not None:
        mp_now = f"{mp:,.2f}" + (f"，在现价{'上方' if mp > sp else '下方'} {abs(mp / sp - 1) * 100:.1f}%" if sp else "")
    add("max_pain", "Max Pain", mp_now, chg(d.get("max_pain"), "{:.2f}", "比上一次高 {}", "比上一次低 {}"))
    add("window", "近端窗口两项", None, None)
    return out


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
    fc_call, fc_put = _num(_dig(o, "full_chain_oi", "total_call_oi")), _num(_dig(o, "full_chain_oi", "total_put_oi"))
    full_oi = fc_call + fc_put if fc_call is not None and fc_put is not None else None      # 缺一侧不拿另一侧顶替
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
        "full_oi": full_oi,
        "max_pain": _num(_dig(o, "max_pain", "max_pain")),
        "term_shape": ts.get("shape"), "front_iv": _num(ts.get("front_iv")), "back_iv": _num(ts.get("back_iv")),
        "supports": _levels(_dig(o, "key_levels", "support"), spot),
        "resistances": _levels(_dig(o, "key_levels", "resistance"), spot),
        "flows": flows[: int(RULES["flows_shown"])], "flow_summary": flow_sum,
        "flow_expiries": flow_by_expiry(o.get("unusual_activity"), spot, _levels(_dig(o, "key_levels", "resistance"), spot)),
        "unusual_flow_ok": o.get("unusual_flow_ok"),
    }


def views_of(results) -> dict:
    """一天的全部扫描结果 → `{标的（大写）: day_view 读数}`。服务端缓存的就是它（原始文件近 2 MB，读数只有几十 KB）。
    不是对象的条目丢掉；顶层不是对象由调用方先判。"""
    out = {}
    for t, r in (results or {}).items():
        v = day_view(r)
        if v is not None:
            out[str(t).upper()] = v
    return out


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
        "full_pc_ratio": _delta(today.get("full_pc_ratio"), prev.get("full_pc_ratio"), 3),
        "window_pc_ratio": _delta(today.get("pc_ratio"), prev.get("pc_ratio")),
        "iv_rank": _delta(today.get("iv_rank"), prev.get("iv_rank"), 1) if same_ivr else None,
        "iv_rank_comparable": same_ivr,
        "iv_current": _delta(today.get("iv_current"), prev.get("iv_current")),
        "skew": _delta(today.get("skew"), prev.get("skew"), 3),
        "full_oi_pct": _pct(today.get("full_oi"), prev.get("full_oi")),
        "window_oi_pct": _pct(today.get("total_oi"), prev.get("total_oi")),
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
    move("full_pc_ratio", d["full_pc_ratio"], RULES["pc_jump"],
         f"全链 P/C 单日 {d['full_pc_ratio']:+.2f}（{prev.get('full_pc_ratio')} → {today.get('full_pc_ratio')}）"
         if d["full_pc_ratio"] is not None else "")
    move("iv_rank", d["iv_rank"], RULES["iv_rank_jump"],
         f"IV Rank 单日 {d['iv_rank']:+.0f}（{prev.get('iv_rank')} → {today.get('iv_rank')}）" if d["iv_rank"] is not None else "")
    move("skew", d["skew"], RULES["skew_jump"],
         f"IV Skew 单日 {d['skew']:+.3f}（{prev.get('skew')} → {today.get('skew')}）" if d["skew"] is not None else "")
    move("full_oi", d["full_oi_pct"], RULES["oi_jump_pct"],
         f"全链 OI 单日 {d['full_oi_pct']:+.1f}%" if d["full_oi_pct"] is not None else "")
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
            "full_pc_ratio": today["full_pc_ratio"], "iv_rank": today["iv_rank"], "iv_rank_source": today["iv_rank_source"],
            "skew": today["skew"], "full_oi": today["full_oi"],
            "call_pct": today["flow_summary"]["call_pct"],
            "delta": d, "alerts": al, "alert_count": len(al), "flipped": bool(d.get("direction_flipped"))}


def _prev_for(t: str, days: List[Tuple[str, dict]], idx: int) -> Tuple[Optional[str], Optional[dict], int]:
    """`days` = [(日期, {标的: `day_view` 读数})]，按日期升序、`idx` 是今天。往前找这只票**有记录**的最近一个扫描日；返回 (日期, 读数, 中间跳过了几个扫描日)。"""
    for j in range(idx - 1, -1, -1):
        v = (days[j][1] or {}).get(t)
        if v is not None:
            return days[j][0], v, idx - 1 - j
    return None, None, 0


def build_overview(days: List[Tuple[str, dict]], date: str) -> dict:
    """`days` = [(日期, `views_of(那天的全部扫描结果)`)]，升序。每只票一行：今天的读数 + 相对上一个有记录的扫描日的变化。"""
    idx = next((i for i, (d, _) in enumerate(days) if d == date), None)
    if idx is None:
        return {"available": False, "reason": "no_such_date", "reason_zh": reason_zh("no_such_date"), "date": date, "rows": []}
    rows = []
    for t in sorted((days[idx][1] or {})):
        today = days[idx][1][t]
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
    today = (days[idx][1] or {}).get(t)
    if today is None:
        return base | {"reason": "ticker_not_scanned", "reason_zh": reason_zh("ticker_not_scanned")}
    pd_, pv, gap = _prev_for(t, days, idx)
    d = compare(today, pv)
    hist = []
    for dt, res in days[: idx + 1]:
        v = (res or {}).get(t)
        if v is None:
            continue
        hist.append({"date": dt, "score": v["score"], "direction": v["direction"], "spot": v["spot"],
                     "full_pc_ratio": v["full_pc_ratio"], "pc_ratio": v["pc_ratio"], "full_oi": v["full_oi"], "iv_rank": v["iv_rank"], "iv_rank_source": v["iv_rank_source"],
                     "iv_current": v["iv_current"], "skew": v["skew"], "total_oi": v["total_oi"],
                     "call_pct": v["flow_summary"]["call_pct"]})
    return base | {"available": True, "reason": None if pv else "no_prev", "reason_zh": None if pv else reason_zh("no_prev"),
                   "prev_date": pd_, "scans_between": gap, "today": today, "prev": pv, "delta": d,
                   "alerts": alerts(today, pv, d), "readings": readings(today, pv, d),
                   "history": hist[-int(RULES["history_days"]):]}
