"""Alpha Bot「深度」页的数据面（v0.45.760）：单票深度报告——把每日扫描对一只标的记下的东西排成八个章节。

来历
----
原来的深度报告由 `generate_deep_v2.py` 生成（要人在终端手动跑、可选调 Claude API 写推理段落），v0.45.724 连同依赖整条退役，
最后一份 NVDA 报告停在 2026-05-29。用户 2026-11-11 定：**不恢复那条链**，在 Alpha Bot 里新做一页，直接读每日扫描落盘的
`.swarm_results_<日期>.json`——扫描名单里每只标的、每个扫描日都有，扫描跑完自动就是最新，不用手动跑命令、不调 LLM、不花钱。

板块（v0.45.760 同日按「取舍 + 去套话」重排；依据是 2026-10-28～11-10 共 300 份扫描结果的实测）：
摘要（含共振与冲突）/ 风险 / 催化剂 / 期权要点 / 人气与板块 / 参考价位 / 各蜂明细。

读什么（**本模块不读文件**）
---------------------------
输入是 `alphabot/service.py` 读好的「某天某票的扫描结果」（dict）。本模块只做挑字段、换算距离、排顺序；纯函数，不联网、不写任何东西。
期权读数复用 `options_compare.day_view`（同一份口径，不另抄）。

⚠️ 它是**扫描记录的排版，不是新的分析**：
  · 取不到的字段一律 None / 空表，页面显示「—」，**不补默认值、不编数**；
  · **只搬数字与事实，不搬扫描时模板拼出来的推断句**——「上游需求强劲，有望传导至订单端」「当前分析具备较高独立 alpha 价值」
    「信号解读可按常规权重处理」这类句子、数字后面挂的情绪词（「IV Rank 100（恐慌高位）」的括号）、✅ ⚠️ 一类符号都不上页面；
    也**不润色它们**（润色 = 替规则引擎编一套更像人话的理由）。清洗只做三件机械的事：拆竖线、去掉不含数字的括号、丢掉引用历史准确率的片段
    （那个口径有已知问题），见 `clean_text` / `findings`；
  · 300 份里恒定不变的内容不上页面：拥挤度说明（300 份同一句）、周期说明（两种模板，240 份是「普通交易窗口」，改为只列真命中的
    日历窗口）、「持有期提示」（规则推出的操作建议）、期限结构「信号」与 charm 说明（含操作建议 / 远离现价的「Pin 目标价」）；
  · 不出概率、不出情景目标价、不出模型预期收益（未证实有预测力）；参考价位只列离现价多远；
  · 综合分与方向未被证实有预测力（`final_score` 的 IC ≈ 0），页面顶部说一次；
  · 证伪条件里标「人工」的扫描**没有自动核对**（`fired` 为 None），页面写「未核对」，不当成「未触发」。
"""
from __future__ import annotations

import math
import re
from typing import List, Optional

from alphabot import options_compare as OC

#: 维度与蜂的展示名、顺序（唯一表；页面只排版）
DIMENSIONS = (("signal", "信号（聪明钱）"), ("catalyst", "催化剂"), ("sentiment", "情绪"), ("odds", "赔率（期权）"),
              ("risk_adj", "风险调整"))
AGENTS = (("ScoutBeeNova", "Scout · 聪明钱侦察"), ("OracleBeeEcho", "Oracle · 期权市场"), ("ChronosBeeHorizon", "Chronos · 催化剂"),
          ("BuzzBeeWhisper", "Buzz · 情绪与叙事"), ("RivalBeeVanguard", "Rival · 模型与技术面"),
          ("GuardBeeSentinel", "Guard · 交叉验证"), ("BearBeeContrarian", "Bear · 看空对冲"))
#: 已退役、但老日子的记录里还在的蜂（展示名带「已退役」）。CodeExecutorAgent：v0.45.750 退役，记录里出现到 2026-10 下旬
RETIRED_AGENTS = {"CodeExecutorAgent": "CodeExecutor（已退役）"}
BEAR_PARTS = (("insider_bear", "内幕交易"), ("options_bear", "期权"), ("momentum_bear", "动量"), ("news_bear", "新闻"),
              ("overval_bear", "估值"), ("short_int_bear", "做空"))
RULES = {"insider_trades_shown": 5, "peers_shown": 6, "min_bear_points": 3, "flows_shown": 5}
#: 命中才列出来的日历窗口（Guard 的 `cycle_context` 布尔位 → 展示名）
CALENDAR_FLAGS = (("is_opex_week", "月度期权到期周"), ("is_weekly_opex_day", "周度期权到期日"), ("is_fomc_week", "FOMC 周"),
                  ("is_month_end_window", "月末再平衡窗口"), ("is_post_earnings_window", "财报后窗口"))
_NO_DIGIT_PAREN = re.compile(r"[（(]([^（()）\d]*)[）)]")
#: 不含数字、但是事实的括号——留着。全量 2721 份实测（二次检查）：「高波动率 53%（年化）」「期权数据不可用（真实链获取失败）」
#: 「F&G 33(Fear)」（指数自己的分档名）、报错原文里的 `type(s)` 都曾被当成情绪词删掉
_KEEP_PAREN = re.compile(r"年化|失败|不可用|陈旧|缺失|过期|Fear|Greed|Neutral|^s$")
_SYMBOLS = re.compile("[\u2600-\u27bf\U0001f300-\U0001faff\ufe0f]")
_ACCURACY = re.compile(r"准确率|历史\s*\d+\s*条")
#: 模型预期收益的片段（「预期 7d -12.49%」「30d -18.73%」「ML预测7日回报-12.5%」）：未证实有预测力，不上页面
_MODEL = re.compile(r"ML\s*预测|^\s*预期\s*\d+d|^\s*\d+d\s*[-+−]")

REASONS = {
    "demo": "演示模式不读扫描结果",
    "no_scan_files": "数据根下没有任何扫描结果文件",
    "no_such_date": "没有这一天的扫描结果",
    "file_unreadable": "这一天的扫描结果文件读不了",
    "ticker_not_scanned": "这一天的扫描里没有这只标的",
}


def public_rules() -> dict:
    return dict(RULES)


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


def _text(v) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _texts(v) -> List[str]:
    return [s.strip() for s in (v if isinstance(v, list) else []) if isinstance(s, str) and s.strip()]


def clean_text(v) -> Optional[str]:
    """扫描生成的一小段文字 → 去掉符号、破折号之后的推断句、**不含数字**的括号（挂在数字后面的情绪词）。含数字的括号是事实，留着；
    不含数字但属于事实的（`_KEEP_PAREN`：年化、取数失败、指数分档名）也留着。只删不写：不改写、不润色。"""
    t = _text(v)
    if t is None:
        return None
    t = _SYMBOLS.sub("", t.split("——")[0])                              # 破折号后面是模板接的推断句，只留前半句事实
    t = _NO_DIGIT_PAREN.sub(lambda m: m.group(0) if _KEEP_PAREN.search(m.group(1)) else "", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ：:;；|") or None


def findings(discovery) -> List[str]:
    """一只蜂的「发现」（竖线拼接的一长串）→ 逐条。丢掉引用历史准确率的片段；其余只做 `clean_text`。"""
    out = []
    for part in str(discovery or "").split("|"):
        if _ACCURACY.search(part) or _MODEL.search(part):
            continue
        c = clean_text(part)
        if c and c not in out:
            out.append(c)
    return out


def rank_of(results, ticker: str) -> dict:
    """这只票当天在名单里按综合分排第几（1 = 最高；同分并列取靠前的名次）。分数缺失的票不参与排名。"""
    scores = {str(t).upper(): _num(r.get("final_score")) for t, r in (results or {}).items() if isinstance(r, dict)}
    scores = {t: v for t, v in scores.items() if v is not None}
    mine = scores.get(ticker)
    if mine is None:
        return {"rank": None, "of": len(scores)}
    return {"rank": 1 + sum(1 for v in scores.values() if v > mine), "of": len(scores)}


def _details(result, agent) -> dict:
    d = _dig(result, "agent_details", agent, "details")
    return d if isinstance(d, dict) else {}


def _dist(level: Optional[float], spot: Optional[float]) -> Optional[float]:
    return round((level / spot - 1) * 100, 2) if level is not None and spot else None


def _directions(r: dict) -> dict:
    """各蜂当天的方向 `{蜂: 方向}`：先取 `agent_directions`，没有就取各蜂自己记的。"""
    out = {}
    for name, a in (r.get("agent_details") if isinstance(r.get("agent_details"), dict) else {}).items():
        d = _dig(r, "agent_directions", name) or (a.get("direction") if isinstance(a, dict) else None)
        if d:
            out[name] = d
    return out


def _votes(r: dict) -> dict:
    """方向票——**只数真投票的蜂**。扫描记的 `agent_breakdown` 是「全体」口径（`queen_distiller` 为了历史可比保留的展示数），
    把不投票的蜂也数在里面：NVDA 2026-11-10 记「6 只看空、1 只中性」，其中 Bear 与 Guard 自 2026-09-14 起不投票，真投票的是 4 空 1 中性。
    不投票的名单取**那一天记录里**的 `vote_excluded_agents`（没有这个键的老记录 = 当时全体都投）。"""
    excluded = set(_texts(r.get("vote_excluded_agents")))
    dirs = _directions(r)
    voting = {n: d for n, d in dirs.items() if n not in excluded}
    return {"counts": {k: sum(1 for d in voting.values() if d == k) for k in ("bullish", "bearish", "neutral")},
            "voters": len(voting),
            "non_voting": sorted(n for n in (r.get("agent_details") if isinstance(r.get("agent_details"), dict) else {}) if n in excluded),
            "weights": {k: _num(_dig(r, "direction_vote_weights", k)) for k in ("bullish", "bearish", "neutral")}}


def _pipeline(r: dict) -> List[dict]:
    """综合分是怎么一步步来的——扫描记下的各步分数，照记录排（`queen_distiller`：五维加权 → 共振加成 → 数据质量压缩 →
    Guard 扣分 → Bear 封顶 → 其后还有别的调整 → 最终）。只列记录里有的步；每步带与上一步的差，差为 0 的步页面可以不显示。
    ⚠️ 不给「其后的调整」起名字：Bear 封顶之后到最终分之间还有若干步（2026-10 / 11 有 171 份在这一段变了），记录里没有逐步的名字。"""
    steps = [("五个维度加权", "base_score_before_resonance"), ("共振加成后", "pre_penalty_score"),
             ("数据质量压缩后", "score_after_dq"), ("Guard 扣分后", "score_after_guard"),
             ("Bear 封顶后", "score_after_bear"), ("最终综合分", "final_score")]
    out, prev = [], None
    for label, key in steps:
        v = _num(r.get(key))
        if v is None:
            continue
        out.append({"label": label, "key": key, "score": v, "change": round(v - prev, 2) if prev is not None else None})
        prev = v
    if out and out[-1]["key"] == "final_score" and len(out) >= 2 and out[-2]["key"] == "score_after_bear":
        out[-1]["label"] = "其后的调整，得最终综合分" if out[-1]["change"] else "最终综合分"
    return out


def _summary(r: dict, opt: dict) -> dict:
    band = _dig(r, "confidence_calibration", "confidence_band")
    band = [_num(x) for x in band] if isinstance(band, list) and len(band) == 2 else None
    return {
        "score": _num(r.get("final_score")), "direction": r.get("direction"), "spot": opt["spot"],
        "confidence_band": band if band and None not in band else None,
        "votes": _votes(r),
        "data_real_pct": _num(r.get("data_real_pct")), "coverage_pct": _num(r.get("dimension_coverage_pct")),
        "data_quality_grade": r.get("data_quality_grade"), "distill_mode": r.get("distill_mode"),
        "macro_regime": r.get("macro_regime"),
        "total_penalty": _num(r.get("total_penalty")), "coverage_warning": _text(r.get("coverage_warning")),
        "resonance": _resonance(r),
    }


def _dimensions(r: dict) -> List[dict]:
    """五个维度。`weight` 是当天实际用的权重（政体路由之后），`base_weight` 是路由之前的；权重为 0 ⇒ 不进综合分。"""
    return [{"key": k, "label": zh, "score": _num(_dig(r, "dimension_scores", k)),
             "weight": _num(_dig(r, "dimension_weights", k)), "base_weight": _num(_dig(r, "dimension_weights_base", k)),
             "confidence": _num(_dig(r, "dimension_confidence", k)),
             "status": _dig(r, "dimension_status", k), "data_quality": _num(_dig(r, "dim_data_quality", k)),
             "in_score": (_num(_dig(r, "dimension_weights", k)) or 0.0) > 0} for k, zh in DIMENSIONS]


def _agents(r: dict) -> List[dict]:
    """当天记录里的每只蜂——**名单跟着记录走**，不写死：评分改过几次，老日子里有后来退役的蜂（CodeExecutorAgent 记到 2026-10 下旬），
    以后加了新蜂也会自己出现。已知的蜂按固定顺序在前，其余按名字排在后面、用记录里的原名。
    `weight` = 这只蜂所在维度当天的权重（它的维度不在五维里 ⇒ None）；`in_score` = 权重 > 0。"""
    excluded = set(_texts(r.get("vote_excluded_agents")))
    recorded = r.get("agent_details") if isinstance(r.get("agent_details"), dict) else {}
    known = dict(AGENTS)
    order = [n for n, _ in AGENTS if n in recorded] + sorted(n for n in recorded if n not in known)
    dims = dict(DIMENSIONS)
    out = []
    for name in order:
        a = recorded.get(name)
        if not isinstance(a, dict):
            continue
        dim = a.get("dimension")
        weight = _num(_dig(r, "dimension_weights", dim)) if dim in dims else None
        disc = str(a.get("discovery") or "").lstrip()
        out.append({"name": name, "label": known.get(name) or RETIRED_AGENTS.get(name, name), "known": name in known,
                    "dimension": dim, "dimension_label": dims.get(dim), "weight": weight, "in_score": bool(weight and weight > 0),
                    "score": _num(a.get("score")),
                    "direction": a.get("direction") or _dig(r, "agent_directions", name),
                    "confidence": _num(a.get("confidence")), "findings": findings(a.get("discovery")),
                    # 这只蜂当天出错了（扫描把报错原文记进「发现」，以 Error 或 ❌ 开头）——它的分数与方向不是正常算出来的，
                    # 页面要在摘要里点名，不能只躺在收起的明细里
                    "failed": disc.startswith("Error") or disc.startswith("\u274c"),
                    "votes": name not in excluded})
    return out


def _side_effects(r: dict) -> List[dict]:
    """不靠权重、但当天确实动了综合分的通道，和算了却没用的诊断量——都取记录里的开关，不靠猜。"""
    gex = r.get("gex_regime_mod") if isinstance(r.get("gex_regime_mod"), dict) else {}
    out = [
        {"key": "guard_penalty", "label": "Guard 扣分（它的分数低于 4 才扣）", "applied": bool(r.get("guard_penalty_applied")),
         "value": _num(r.get("guard_penalty"))},
        {"key": "bear_cap", "label": "Bear 封顶（看空强度高时给综合分设上限）", "applied": bool(r.get("bear_cap_applied")),
         "value": _num(r.get("bear_strength"))},
        {"key": "dq", "label": "数据质量压缩", "applied": bool(r.get("dq_penalty_applied")), "value": _num(r.get("dq_quality_factor"))},
        {"key": "combo_cap", "label": "扣分合计封顶", "applied": bool(r.get("combo_cap_applied")), "value": _num(r.get("total_penalty"))},
    ]
    if "ml_auxiliary_in_score" in r:
        out.append({"key": "ml_auxiliary", "label": "Rival 的模型分", "applied": bool(r.get("ml_auxiliary_in_score")), "value": None})
    if gex:
        # 只读「用没用」这个开关，**不读那个加减分的数值**：`tests/test_gex_modifier_disconnected.py` 把读那个键的生产文件
        # 名单钉死（防它换个名字接回评分），展示层用不着数值，就不去当它的读者
        out.append({"key": "gex_direct", "label": "GEX 直接加减分", "applied": bool(gex.get("applied")), "value": None})
    return out


def _resonance(r: dict) -> dict:
    res = r.get("resonance") if isinstance(r.get("resonance"), dict) else {}
    return {"detected": bool(res.get("resonance_detected")), "direction": res.get("direction"),
            "supporting_agents": _num(res.get("supporting_agents")), "consistency": _num(res.get("consistency")),
            "dimensions": _texts(res.get("resonant_dimensions")), "boost_pct": _num(res.get("confidence_boost")),
            "conflict_level": r.get("conflict_level"), "arbitration_triggered": bool(r.get("arbitration_triggered")),
            "arbitration_flipped": bool(r.get("arbitration_flipped"))}


def _catalysts(r: dict, spot: Optional[float]) -> dict:
    c, rv = _details(r, "ChronosBeeHorizon"), _details(r, "RivalBeeVanguard")
    events = [{"event": e.get("event"), "date": e.get("date"), "days_until": _num(e.get("days_until")),
               "type": e.get("type"), "severity": e.get("severity")}
              for e in (c.get("catalysts") if isinstance(c.get("catalysts"), list) else []) if isinstance(e, dict)]
    at = c.get("analyst_targets") if isinstance(c.get("analyst_targets"), dict) else {}
    targets = {k: _num(at.get(f"target_{k}")) for k in ("mean", "median", "low", "high")}
    eps = rv.get("eps_revision") if isinstance(rv.get("eps_revision"), dict) else {}
    return {"events": events, "analyst_targets": targets if any(v is not None for v in targets.values()) else None,
            "target_upside_pct": _dist(targets["mean"], spot),
            "analysts": _num(eps.get("num_analyst_opinions")), "recommendation_mean": _num(eps.get("recommendation_mean")),
            "forward_eps": _num(eps.get("forward_eps")), "trailing_eps": _num(eps.get("trailing_eps")),
            "eps_growth_pct": _num(eps.get("eps_growth_proj"))}


def _options(r: dict, opt: dict) -> dict:
    """期权要点：读数本身在 `opt`（`options_compare.day_view`）；这里只补 gamma 环境。完整的日间对比在「对比」页，不在这里再摆一遍。"""
    gex = r.get("gex_state") if isinstance(r.get("gex_state"), dict) else {}
    read = dict(opt) | {"flows": opt["flows"][: int(RULES["flows_shown"])]}
    return {"read": read,
            "gex": {"available": bool(gex.get("available")), "reason": gex.get("reason"), "regime": gex.get("regime"),
                    "flip": _num(gex.get("gex_flip")), "call_wall": _num(gex.get("largest_call_wall")),
                    "put_wall": _num(gex.get("largest_put_wall"))}}


def _macro(r: dict) -> dict:
    g, b, s = _details(r, "GuardBeeSentinel"), _details(r, "BuzzBeeWhisper"), _details(r, "ScoutBeeNova")
    vix = g.get("vix_term_structure") if isinstance(g.get("vix_term_structure"), dict) else {}
    mr = g.get("market_regime") if isinstance(g.get("market_regime"), dict) else {}
    fg = b.get("fear_greed") if isinstance(b.get("fear_greed"), dict) else {}
    rd = b.get("reddit") if isinstance(b.get("reddit"), dict) else {}
    sm = b.get("sentiment_momentum") if isinstance(b.get("sentiment_momentum"), dict) else {}
    rs = s.get("sector_relative_strength") if isinstance(s.get("sector_relative_strength"), dict) else {}
    sc = s.get("supply_chain") if isinstance(s.get("supply_chain"), dict) else {}
    peers = [{"symbol": p.get("symbol"), "name": p.get("name"), "return_5d_pct": _num(p.get("return_5d_pct")),
              "vs_target_pct": _num(p.get("vs_target_pct"))}
             for p in (sc.get("peers") if isinstance(sc.get("peers"), list) else []) if isinstance(p, dict)]
    return {
        "regime": g.get("macro_regime") or r.get("macro_regime"),
        "signals": [c for c in (clean_text(x) for x in _texts(g.get("macro_signals"))) if c],
        "vix": _num(vix.get("vix")), "vix_source": vix.get("vix_source"), "vix_as_of": vix.get("vix_as_of"),
        "vix_stale": vix.get("vix_stale") if isinstance(vix.get("vix_stale"), bool) else None,
        "vix_term": vix.get("vix_term"), "yield_curve": vix.get("yield_curve"),
        # 依据只取第一个逗号之前的事实（「SPX $7521 高于 200MA $7331 (+2.6%)」），后半句是模板给的结论
        "layers": [{"label": zh, "regime": mr.get(f"{k}_regime"),
                    "detail": _text((_text(mr.get(f"{k}_detail")) or "").split("，")[0])}
                   for k, zh in (("macro", "大盘"), ("sector", "板块"), ("stock", "个股"))
                   if mr.get(f"{k}_regime") or mr.get(f"{k}_detail")],
        "calendar": [zh for k, zh in CALENDAR_FLAGS if _dig(g, "cycle_context", k) is True],
        "days_to_opex": _num(_dig(g, "cycle_context", "days_to_opex")),
        "sentiment_pct": _num(b.get("sentiment_pct")),
        "fear_greed": _num(fg.get("value")), "fear_greed_label": fg.get("classification"),
        "fear_greed_real": fg.get("is_real_data") if isinstance(fg.get("is_real_data"), bool) else None,
        "reddit_rank": _num(rd.get("rank")), "reddit_mentions": _num(rd.get("mentions")),
        "volume_ratio": _num(b.get("volume_ratio")), "momentum_5d": _num(b.get("momentum_5d")),
        "volatility_20d": _num(b.get("volatility_20d")),
        "sentiment_deltas": {k: _num(sm.get(k)) for k in ("delta_1d", "delta_3d", "delta_7d")},
        "sector": {"etf": rs.get("sector_etf"), "name": rs.get("sector_name"), "stock_ret_20d": _num(rs.get("stock_ret_20d")),
                   "sector_ret_20d": _num(rs.get("sector_ret_20d")), "relative": _num(rs.get("relative_strength"))} if rs else None,
        "peers": peers[: int(RULES["peers_shown"])],
    }


def _levels(r: dict, opt: dict, cat: dict, optx: dict) -> dict:
    """参考价位：不给概率、不给情景。① 期权市场隐含的波动幅度（平值跨式中间价 ÷ 现价，到所选到期日；只有幅度）；
    ② 几个价位离现价多远（都取自别的板块，不新算），按价格从高到低。分析师最高 / 最低目标价不列（常在现价两三倍之外，把近端价位挤到后面）。"""
    qs = _dig(r, "agent_details", "OracleBeeEcho", "details", "quote_set")
    qs = qs if isinstance(qs, dict) else {}
    spot = opt["spot"]
    mv = _num(qs.get("implied_move_pct")) if qs.get("data_available") else None
    implied = None
    if mv is not None and spot:
        implied = {"pct": mv, "expiry": qs.get("selected_expiry"), "dte": _num(qs.get("selected_dte")),
                   "straddle_mid": _num(qs.get("atm_straddle_mid")),
                   "up": round(spot * (1 + mv / 100), 2), "down": round(spot * (1 - mv / 100), 2)}
    raw = [("期权隐含上沿", implied and implied["up"]), ("期权隐含下沿", implied and implied["down"]),
           ("Call 墙", optx["gex"]["call_wall"]), ("Put 墙", optx["gex"]["put_wall"]),
           ("Gamma 翻转位", optx["gex"]["flip"]), ("Max Pain（近 7 天）", opt["max_pain"]),
           ("分析师目标价均值", (cat.get("analyst_targets") or {}).get("mean"))]
    rows = [{"label": lb, "price": p, "dist_pct": _dist(p, spot)} for lb, p in raw if p is not None]
    rows.sort(key=lambda x: -x["price"])
    return {"spot": spot, "implied": implied, "rows": rows}


def _risks(r: dict) -> dict:
    be, s = _details(r, "BearBeeContrarian"), _details(r, "ScoutBeeNova")
    ins = s.get("insider") if isinstance(s.get("insider"), dict) else {}
    trades = [{"insider": t.get("insider"), "action": t.get("code_desc"), "shares": _num(t.get("shares")),
               "price": _num(t.get("price")), "date": t.get("date"), "value_usd": _num(t.get("total_value")),
               "pct_of_holding": _num(t.get("pct_of_holding_sold"))}
              for t in (ins.get("notable_trades") if isinstance(ins.get("notable_trades"), list) else []) if isinstance(t, dict)]
    breaks = []
    for e in r.get("thesis_break_evaluations") if isinstance(r.get("thesis_break_evaluations"), list) else []:
        if not isinstance(e, dict):
            continue
        fired = e.get("fired") if isinstance(e.get("fired"), bool) else None
        breaks.append({"level": 2 if "2" in str(e.get("level")) else 1, "label": e.get("label"),
                       "machine": bool(e.get("machine")), "fired": fired, "field": e.get("field"), "op": e.get("op"),
                       "value": _num(e.get("value")), "actual": _num(e.get("actual"))})
    signals = [c for c in (clean_text(x) for x in _texts(be.get("bearish_signals")) if not _MODEL.search(x)) if c]
    return {
        "bear_score": _num(be.get("bear_score")), "bearish_signals": signals,
        "enough_bear_points": len(signals) >= int(RULES["min_bear_points"]),
        "bear_parts": [{"label": zh, "score": _num(be.get(k))} for k, zh in BEAR_PARTS if _num(be.get(k)) is not None],
        "short_pct_float": _num(_dig(be, "short_interest", "short_pct_float")),
        "days_to_cover": _num(_dig(be, "short_interest", "days_to_cover")),
        "insider": {"sold_usd": _num(ins.get("dollar_sold")), "bought_usd": _num(ins.get("dollar_bought")),
                    "filings": _num(ins.get("filings")), "trades": trades[: int(RULES["insider_trades_shown"])]} if ins else None,
        "crowding_score": _num(s.get("crowding_score")),
        "thesis_breaks": breaks, "thesis_fired": sum(1 for b in breaks if b["fired"] is True),
        "guard_penalty": _num(r.get("guard_penalty")), "bear_cap_applied": bool(r.get("bear_cap_applied")),
        "risk_flag": _text(r.get("risk_flag")),
    }


def build(ticker: str, date: str, result, rank: Optional[dict] = None) -> dict:
    """一只票某天的扫描结果 → 各板块。`result` 不是对象 ⇒ `ticker_not_scanned`。`rank` = `rank_of(那天全部结果, ticker)`。"""
    base = {"available": False, "ticker": ticker, "date": date, "rules": public_rules()}
    opt = OC.day_view(result)
    if opt is None:
        return base | {"reason": "ticker_not_scanned", "reason_zh": reason_zh("ticker_not_scanned")}
    cat = _catalysts(result, opt["spot"])
    optx = _options(result, opt)
    return base | {"available": True, "reason": None, "reason_zh": None,
                   "summary": _summary(result, opt) | {"rank": rank or {"rank": None, "of": None}},
                   "risks": _risks(result), "catalysts": cat, "options": optx, "macro": _macro(result),
                   "levels": _levels(result, opt, cat, optx), "dimensions": _dimensions(result), "agents": _agents(result),
                   "pipeline": _pipeline(result), "side_effects": _side_effects(result)}
