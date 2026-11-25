"""Alpha Bot · Zero Gamma 日间拆解（v0.45.816）。纯函数。

要回答的问题：「两个存档日之间，Zero Gamma 为什么动了——是哪一项推的？」
2026-11-24 手工拆 NVDA 时只能用排除法（昨天的逐合约 OI 没存）；v0.45.815 起每天存一份当日原始链，
本模块拿两份存档链做**逐项替换**。

五项（`FACTORS`）：
  · set  合约集合：这个视图里收哪些合约（到期的走了、新挂牌的来了、≤45 天窗口边缘进出）；
  · oi   同一张合约的持仓；
  · iv   同一张合约的隐含波动率；
  · t    同一张合约的剩余天数（dte）；
  · spot 现价——曲线本身不含现价，它只决定 ±20% 扫描窗口放在哪、「最近零点」从哪儿量。

合约按 OCC 代码对上（没有代码时用 到期日 / 行权价 / call·put）。混合链：先按 `set` 那一天的链与视图定下收哪些合约，
每张合约的 oi / iv / dte 各取所选那一天的值；那一天没有这张合约（新挂牌 / 已到期）⇒ 用 `set` 那一天自己的值——
这种合约的影响整个归给「合约集合」。

**两个方向各换一遍**（one-at-a-time）：
  · 正向：从上一份全取昨天出发，每次只把一项换成今天的；
  · 反向：从今天出发，每次只把一项换回昨天的。
两边都是同一项把 Zero Gamma 推到（拉回）位，才说「主因是它」；两边不一致 ⇒ 照实报「交互、分不开」。
不用 Shapley 平均分摊：Zero Gamma 可以**不存在**（全段同号），没有一个能被平均的数——每一步记的是**状态**
（在 X 过零 / 全段为正 / 全段为负）。

状态之间的距离 `_dist`（单位：现价的 %）：状态不同 ⇒ `STATE_GAP`（比 ±20% 窗口内任何零点移动都大）；
都过零 ⇒ 最近零点之差 / 今天现价；同为全正或全负 ⇒ 0。

另给逐行权价的 OI 归因：今天视图内两天都有的合约，只把 OI 换回昨天，按行权价差出净 GEX 变化。

⚠️ 这是模型对自身的归因（它假设做市商卖空全部 put，Zero Gamma 证据等级 C），不是因果；
高亮阈值是拍的、未经检验，只决定显示，不进任何决策。

本模块不读写文件、不联网、不 import 卖权模块：`view_fn`（`view_contracts`）、`sweep_fn`（`zero_gamma_sweep`）、
`profile_fn`（`strike_profile`）由 `alphabot/service.py` 注入（火墙见 tests/test_alphabot.py::TestFirewall）。
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Mapping, Optional, Tuple

FACTORS = ("set", "oi", "iv", "t", "spot")
FACTOR_LABELS = {"set": "合约集合", "oi": "OI", "iv": "IV", "t": "剩余时间", "spot": "现价（扫描窗口）"}
VIEWS = ("le_45dte", "next_expiry", "full")
STATE_GAP = 100.0

#: 全部可调规则（经 `/api/method` 给页面与帮助页，前端不抄第二份）。阈值是拍的、未经检验，只控制显示。
RULES = {
    "move_flag_pct": 2.0,       # 两天都过零且最近零点移动 ≥ 现价的 2% ⇒ 高亮
    "flat_pct": 0.25,           # 总移动 < 现价的 0.25% ⇒「基本没动」，不做归因
    "dominant_share": 0.5,      # 单换一项就走完总距离的一半以上（两个方向都是）⇒「主因」
    "top_strikes": 6,           # 逐行权价 OI 归因列几档
    "strike_band": 0.12,        # 只看今天现价 ±12% 内的行权价
    "state_gap": STATE_GAP,
}

VERDICT_LABELS = {
    "flat": "基本没动",
    "dominant": "主因明确",
    "leading": "有主导项，但不独占",
    "interaction": "各项交互，分不开",
}

READ_ME = ("先看「结论」：两个方向（从昨天单换一项 / 从今天单换回一项）指向同一项才叫主因。"
           "下一到期视图在到期日换了的那天，比的是两张不同的合约，合约集合一项按构造必然占主导——优先看 ≤45 天视图。"
           "这是模型对自身的归因（假设做市商卖空全部 put，证据 C），不是因果。")


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def contract_key(c) -> Optional[tuple]:
    """合约身份：OCC 代码优先；没有就用 (到期日, 行权价, C/P)。认不出 ⇒ None（不参与对齐）。"""
    if not isinstance(c, dict):
        return None
    sym = c.get("symbol")
    if isinstance(sym, str) and sym.strip():
        return ("sym", sym.strip())
    K, cp, exp = _num(c.get("strike")), str(c.get("cp") or "").strip().upper()[:1], c.get("expiry")
    if K is None or cp not in ("C", "P") or not exp:
        return None
    return ("kce", str(exp), K, cp)


class _Day:
    """一天的链：现价 + 按合约身份索引的合约。"""

    def __init__(self, raw: Mapping):
        self.S = _num(raw.get("underlying_price"))
        self.contracts = [c for c in (raw.get("contracts") or []) if isinstance(c, dict)]
        self.idx: Dict[tuple, dict] = {}
        self.n_unkeyed = 0
        for c in self.contracts:
            k = contract_key(c)
            if k is None:
                self.n_unkeyed += 1
            else:
                self.idx.setdefault(k, c)


def _view_keys(day: _Day, view: str, view_fn) -> List[tuple]:
    out, seen = [], set()
    for c in view_fn(day.contracts, view):
        k = contract_key(c)
        if k is not None and k not in seen:
            seen.add(k)
            out.append(k)
    return out


_FIELD_OF = {"oi": "oi", "iv": "iv", "t": "dte"}


def _hybrid(days: Mapping[str, _Day], keys: Mapping[str, List[tuple]], choice: Mapping[str, str]) -> Tuple[List[dict], float]:
    """按 choice（每项取 'prev' / 'cur'）拼一条链与现价。"""
    base_day = days[choice["set"]]
    out = []
    for k in keys[choice["set"]]:
        rec = dict(base_day.idx[k])
        for fac, field in _FIELD_OF.items():
            src = days[choice[fac]].idx.get(k)
            if src is not None:
                rec[field] = src.get(field)
        if choice["oi"] != choice["set"] or choice["iv"] != choice["set"] or choice["t"] != choice["set"]:
            rec.pop("gamma", None)      # 混了别天的 IV / T，CBOE 给的那天 gamma 不再对应；扫描本来就不用它
        out.append(rec)
    return out, days[choice["spot"]].S


def _state(z: Mapping, S: Optional[float]) -> dict:
    return {
        "curve_state": z.get("curve_state"),
        "nearest": z.get("nearest"),
        "crossings": [round(float(x), 2) for x in (z.get("crossings") or [])],
        "sign_at_spot": z.get("sign_at_spot"),
        "total_at_spot": z.get("total_at_spot"),
        "spot": S,
        "n_contracts": z.get("n_contracts"),
        "excluded_no_iv": z.get("excluded_no_iv"),
        "excluded_no_oi": z.get("excluded_no_oi"),
    }


def _dist(a: Mapping, b: Mapping, ref: float) -> float:
    if a["curve_state"] != b["curve_state"]:
        return STATE_GAP
    if a["curve_state"] == "crosses":
        na, nb = _num(a.get("nearest")), _num(b.get("nearest"))
        if na is None or nb is None:
            return STATE_GAP
        return abs(na - nb) / ref * 100.0
    return 0.0


def state_text(s: Mapping) -> str:
    st = s.get("curve_state")
    if st == "crosses":
        n = _num(s.get("nearest"))
        return f"最近零点 {n:.2f}" if n is not None else "过零"
    return {"all_positive": "全段为正（无零点）", "all_negative": "全段为负（无零点）",
            "insufficient_contracts": "没有可用合约"}.get(st, str(st))


def _verdict(total: float, fwd: Dict[str, float], bwd: Dict[str, float], rules: Mapping) -> dict:
    if total < rules["flat_pct"]:
        return {"verdict": "flat", "factor": None}
    top_f = max(FACTORS, key=lambda f: (fwd[f], -FACTORS.index(f)))
    top_b = max(FACTORS, key=lambda f: (bwd[f], -FACTORS.index(f)))
    if top_f != top_b or fwd[top_f] <= 0 or bwd[top_b] <= 0:
        return {"verdict": "interaction", "factor": None, "top_forward": top_f, "top_backward": top_b}
    share = rules["dominant_share"] * total
    v = "dominant" if fwd[top_f] >= share and bwd[top_b] >= share else "leading"
    return {"verdict": v, "factor": top_f}


def _headline(view_res: Mapping) -> str:
    y, d = view_res["prev"], view_res["cur"]
    v = view_res["verdict"]
    move = f"{state_text(y)} → {state_text(d)}"
    if v["verdict"] == "flat":
        return f"基本没动（{move}）。"
    if v["verdict"] == "interaction":
        return (f"{move}。各项交互，分不开：从昨天单换最接近今天的是「{FACTOR_LABELS[v['top_forward']]}」，"
                f"从今天单换回最接近昨天的是「{FACTOR_LABELS[v['top_backward']]}」。")
    f = v["factor"]
    fw = view_res["forward"][f]["state"]
    bw = view_res["backward"][f]["state"]
    lead = "主因" if v["verdict"] == "dominant" else "主导项（不独占）"
    return (f"{move}。{lead}：{FACTOR_LABELS[f]}——从昨天单换它就到 {state_text(fw)}；"
            f"从今天单换回它就回到 {state_text(bw)}。")


def _strike_oi_attribution(days, keys, view_fn, profile_fn, rules) -> List[dict]:
    """今天视图内两天都有的合约，只把 OI 换回昨天，按行权价差出净 GEX（美元 / 1%）。"""
    cur, prev = days["cur"], days["prev"]
    common = [k for k in keys["cur"] if k in prev.idx]
    if not common or cur.S is None:
        return []
    now_rows = [cur.idx[k] for k in common]
    was_rows = [dict(cur.idx[k], oi=prev.idx[k].get("oi")) for k in common]

    def by_strike(rows):
        return {r.get("strike"): _num(r.get("net_gex_usd_per_1pct")) or 0.0
                for r in profile_fn(rows, cur.S).get("rows") or []}
    now_g, was_g = by_strike(now_rows), by_strike(was_rows)
    d_oi: Dict[float, Dict[str, float]] = {}
    for k in common:
        c = cur.idx[k]
        K = _num(c.get("strike"))
        cp = str(c.get("cp") or "").upper()[:1]
        if K is None or cp not in ("C", "P"):
            continue
        delta = (_num(c.get("oi")) or 0.0) - (_num(prev.idx[k].get("oi")) or 0.0)
        slot = d_oi.setdefault(K, {"C": 0.0, "P": 0.0})
        slot[cp] += delta
    band = rules["strike_band"]
    out = []
    for K in set(now_g) | set(was_g):
        if K is None or abs(K / cur.S - 1.0) > band:
            continue
        dg = now_g.get(K, 0.0) - was_g.get(K, 0.0)
        if dg == 0.0:
            continue
        o = d_oi.get(K, {"C": 0.0, "P": 0.0})
        out.append({"strike": K, "delta_net_gex_usd_per_1pct": dg,
                    "delta_call_oi": o["C"], "delta_put_oi": o["P"]})
    out.sort(key=lambda r: -abs(r["delta_net_gex_usd_per_1pct"]))
    return out[: rules["top_strikes"]]


def decompose_view(days: Mapping[str, _Day], view: str, *, view_fn, sweep_fn, profile_fn,
                   rules: Mapping = RULES) -> dict:
    keys = {d: _view_keys(days[d], view, view_fn) for d in ("prev", "cur")}
    ref = days["cur"].S

    def run(choice):
        chain, S = _hybrid(days, keys, choice)
        return _state(sweep_fn(chain, S), S)

    all_prev = {f: "prev" for f in FACTORS}
    all_cur = {f: "cur" for f in FACTORS}
    y, d = run(all_prev), run(all_cur)
    total = _dist(y, d, ref)
    forward, backward = {}, {}
    fwd_gain, bwd_gain = {}, {}
    for f in FACTORS:
        s = run(dict(all_prev, **{f: "cur"}))
        fwd_gain[f] = total - _dist(s, d, ref)
        forward[f] = {"state": s, "gain_pct": fwd_gain[f], "text": state_text(s)}
        s = run(dict(all_cur, **{f: "prev"}))
        bwd_gain[f] = total - _dist(s, y, ref)
        backward[f] = {"state": s, "gain_pct": bwd_gain[f], "text": state_text(s)}

    set_prev, set_cur = set(keys["prev"]), set(keys["cur"])
    common = set_prev & set_cur
    iv_missing = sum(1 for k in common
                     if _num(days["prev"].idx[k].get("iv")) is None or _num(days["cur"].idx[k].get("iv")) is None)
    exp_prev = sorted({str(days["prev"].idx[k].get("expiry")) for k in set_prev})
    exp_cur = sorted({str(days["cur"].idx[k].get("expiry")) for k in set_cur})
    moved = None
    if y["curve_state"] == d["curve_state"] == "crosses" and _num(y["nearest"]) is not None \
            and _num(d["nearest"]) is not None:
        moved = (d["nearest"] - y["nearest"]) / ref * 100.0
    flags = {
        "state_changed": y["curve_state"] != d["curve_state"],
        "sign_at_spot_flipped": (y["sign_at_spot"] != d["sign_at_spot"]
                                 and None not in (y["sign_at_spot"], d["sign_at_spot"])),
        "moved_big": moved is not None and abs(moved) >= rules["move_flag_pct"],
        "expiry_rolled": exp_prev != exp_cur,
    }
    res = {
        "view": view,
        "prev": y, "cur": d,
        "total_dist_pct": total,
        "nearest_move_pct": moved,
        "forward": forward, "backward": backward,
        "verdict": _verdict(total, fwd_gain, bwd_gain, rules),
        "flags": flags,
        "highlight": flags["state_changed"] or flags["sign_at_spot_flipped"] or flags["moved_big"],
        "matching": {"n_prev": len(set_prev), "n_cur": len(set_cur), "n_common": len(common),
                     "n_only_prev": len(set_prev - set_cur), "n_only_cur": len(set_cur - set_prev),
                     "n_common_iv_missing": iv_missing,
                     "expiries_prev": exp_prev if len(exp_prev) <= 3 else None,
                     "expiries_cur": exp_cur if len(exp_cur) <= 3 else None},
        "strike_oi": _strike_oi_attribution(days, keys, view_fn, profile_fn, rules),
    }
    res["headline"] = _headline(res)
    return res


def build(prev_raw: Mapping, cur_raw: Mapping, *, view_fn: Callable, sweep_fn: Callable,
          profile_fn: Callable, views=VIEWS, rules: Mapping = RULES) -> dict:
    """两份原始链（上一份存档 / 当前存档）⇒ 各视图的拆解。现价缺失 ⇒ `available=False`，不硬算。"""
    days = {"prev": _Day(prev_raw), "cur": _Day(cur_raw)}
    if days["prev"].S is None or days["cur"].S is None:
        return {"available": False, "reason": "underlying_price_unavailable", "views": {}}
    out = {"available": True, "reason": None,
           "spot_prev": days["prev"].S, "spot_cur": days["cur"].S,
           "n_unkeyed": {"prev": days["prev"].n_unkeyed, "cur": days["cur"].n_unkeyed},
           "views": {}}
    for v in views:
        out["views"][v] = decompose_view(days, v, view_fn=view_fn, sweep_fn=sweep_fn,
                                         profile_fn=profile_fn, rules=rules)
    out["highlight"] = any(r["highlight"] for r in out["views"].values())
    return out


def public_rules() -> dict:
    return {"rules": dict(RULES), "factors": list(FACTORS), "factor_labels": dict(FACTOR_LABELS),
            "verdict_labels": dict(VERDICT_LABELS), "read_me": READ_ME}
