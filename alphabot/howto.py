"""Alpha Bot 卖权页「怎么卖」（v0.45.515）：六种结构各自的下单步骤、风险与**推荐价位**。纯函数。

用户问的是「表里这行我该怎么真的下出去、挂什么价、什么时候收」——表只给了权利金 / 最大亏损 / 盈亏平衡。
本模块把同一份报价（`structure_quote` 的腿与金额，一个字不改）翻译成可执行的价位：

  · **下单腿**：每条腿的买卖方向、合约、行权价、距现价 %、Δ、σ 距离、P(ITM)、bid / ask / 中价；
  · **限价**：起挂价 = 组合中价（卖腿取 mid、买腿取 mid 之和）；底线价 = 自然价（卖 bid / 买 ask，即表里的权利金）
    与中价之间 `FLOOR_SHARE` 处——到底线仍不成交就今天不做，不追到自然价；
  · **买回**：按起挂价成交为例的止盈买回价（`PROFIT_TAKE`）、止损买回价（`STOP_MULT`×权利金），
    价差类止损价 ≥ 宽度时止损线形同虚设，如实写明、改用标的价位触发；
  · **标的价位**：短腿行权价、盈亏平衡、±1σ 边界、下方 / 上方最近 Zero Gamma、净 Major，各带距现价 %；
  · **管理日**：到期前 `MANAGE_DTE[期限]` 天；
  · **提示**：到期前有财报 / 财报日未知、现价处负 gamma 时的裸卖结构、点差过宽。

⚠️ **这些数字是业内通行的经验规则，不是本系统前向检验过的结论**：50% 止盈、2× 止损、一半价差让价、21 天管理都没有
进预注册检验，页面与帮助页都这样写。它们只回答「怎么执行」，不回答「该不该卖」——后者是路由规则与检验的事。
全部常量在 `RULES`，经 `/api/method` 给前端 / 帮助页，不在前端抄第二份。

本模块不读写文件、不联网、不 import 卖权模块：`structure_quote` 与基线档位 `BASE_RUNG` 由 `alphabot/service.py` 注入（火墙见
tests/test_alphabot.py::TestFirewall）。算不出来的一律是 None（页面写「不可得」），不是 0。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional

STRUCTURES = ("short_put", "short_call", "bull_put_spread", "bear_call_spread", "strangle", "iron_condor")
#: 结构 → 用哪一侧的档位（与 `sell_strike_report.structures_for_row` 同一张映射；测试逐例核对金额一致）
_PUT_SIDE = ("short_put", "bull_put_spread")
_CALL_SIDE = ("short_call", "bear_call_spread")
DEFINED_RISK = ("bull_put_spread", "bear_call_spread", "iron_condor")
UNBOUNDED_CALL = ("short_call", "strangle")
CONTRACT = 100

#: 全部可调的执行规则。**经验规则，未经本系统前向检验。**
RULES = {
    "start": "combo_mid",          # 起挂价 = 组合中价
    "floor_share": 0.5,            # 底线 = 自然价 + 0.5 × (中价 − 自然价)
    "profit_take": 0.5,            # 止盈：买回净价 = 成交权利金 × 0.5
    "stop_mult": 2.0,              # 止损：买回净价 ≥ 成交权利金 × 2.0（亏损约等于一份权利金）
    "manage_dte": {"monthly": 21, "weekly": 3},   # 到期前这么多天起评估平仓 / 换仓
    "wide_spread_pct": 0.10,       # 任一腿点差 / 中价超过它 ⇒ 提示难以按中价成交
    "loss_budget_pct": (1, 2),     # 单笔最大可承受亏损占账户的通行上限（%）；不是个性化建议
    "tick": 0.01,
}

DISCLAIMER = ("以下是执行层面的通行做法与算术，不构成投资建议；其中的 50% 止盈、2 倍止损、让价幅度、管理日都是业内经验规则，"
              "**没有被本系统前向检验过**——它们回答「怎么执行」，不回答「该不该卖」。价位取自 CBOE 延迟报价，"
              "下单前以券商实时 bid / ask 重算中价，不要照抄本页数字。")

# ─────────────────────────────── 静态说明（每种结构一份）

GUIDE: Dict[str, Dict[str, Any]] = {
    "short_put": {
        "name": "卖 put（现金担保）",
        "oneline": "卖出一张价外 put，先收权利金；到期价外全拿，价内则以行权价接货。",
        "stance": "中性偏多——不看跌，或本来就愿意在行权价附近买这只票。",
        "order": [
            "单腿限价单：卖出开仓（Sell to Open）1 张该 put，挂「净权利金 ≥ 起挂价」。",
            "券商要有「现金担保 put」权限（通常最低一档期权权限）；账户里留足 行权价 × 100 的现金。",
            "成交后立刻挂一张「买入平仓、止盈价」的 GTC 单，别等盯盘。",
        ],
        "pnl": [
            "最大盈利 = 权利金（到期收盘 ≥ 行权价，put 作废）。",
            "最大亏损 = (行权价 − 权利金) × 100 / 张（标的跌到 0 的极端情形，有限但可观）。",
            "盈亏平衡 = 行权价 − 权利金；到期收盘低于它才净亏。",
        ],
        "manage": [
            "收到权利金的 50% 就买回平仓，不拿到最后一天——尾部风险与剩余收益不对等。",
            "标的收盘跌破短腿行权价：要么买回，要么「向下向后」换仓（买回旧 put、卖更低行权价更远到期日，且换仓净收权利金）；"
            "别把一次下破当成「反正愿意接货」就放着不管。",
            "不想接货就别拿到到期：价内 put 随时可能被提前行权。",
        ],
        "risks": [
            "单边下跌时亏损放大，且权利金只是缓冲；跳空（财报、利空）没法止损。",
            "美式期权可提前行权：价内越深、越临近除息日越容易。",
            "被指派后你持有 100 股——要有这份资金与心理准备。",
        ],
        "avoid": "你不愿意持有这只票；到期前有财报；现价处 gamma 为负且贴着下方 Zero Gamma。",
    },
    "short_call": {
        "name": "卖 call（备兑）",
        "oneline": "卖出一张价外 call，先收权利金；到期价外全拿，价内则以行权价交出股票。",
        "stance": "中性偏空或温和看涨但不超过行权价——通常用于已持有 100 股的备兑。",
        "order": [
            "单腿限价单：卖出开仓 1 张该 call，挂「净权利金 ≥ 起挂价」。",
            "备兑：账户里要有 100 股对应正股（本页收益按备兑口径，占用资金取现价 S）。",
            "没有股票就是「裸卖 call」：需要最高一档期权权限与大额保证金，上方亏损无上限——本页不把它当作可以随手做的结构。",
        ],
        "pnl": [
            "最大盈利 = 权利金（备兑还多赚正股涨到行权价的那部分）。",
            "最大亏损：裸卖无上限；备兑时是正股下跌的损失，权利金只做缓冲。",
            "盈亏平衡（裸卖口径）= 行权价 + 权利金。",
        ],
        "manage": [
            "权利金的 50% 买回平仓。",
            "标的收盘涨破短腿行权价：买回，或「向上向后」换仓；备兑要想清楚是否愿意在这个价卖掉正股。",
            "有股息的票：价内 call 在除息日前夜最容易被提前行权，除息前一天前要处理掉。",
        ],
        "risks": [
            "裸卖 call 的亏损理论上无限；单日跳空涨 30% 的票不能裸卖。",
            "备兑 call 把上涨空间卖给了对手——强势上涨时你会被「卖飞」。",
            "被指派 = 股票按行权价被拿走。",
        ],
        "avoid": "你没有正股又没有高级权限；到期前有财报或并购传闻；现价处 gamma 为负（见提示）。",
    },
    "bull_put_spread": {
        "name": "牛市 put 价差",
        "oneline": "卖近端 put、同时买更远端 put 作保护：收净权利金，最大亏损被封顶。",
        "stance": "中性偏多，且想把最大亏损锁死。",
        "order": [
            "用「垂直价差 / Credit Spread」组合单一次报：卖出开仓短腿 put + 买入开仓保护 put，挂「净信用 ≥ 起挂价」。",
            "不要拆成两笔分别挂——先成交一条腿再等另一条，期间等于裸奔（腿间风险）。",
            "券商要有价差权限（通常中间一档）；占用保证金 = (宽度 − 权利金) × 100 / 张。",
        ],
        "pnl": [
            "最大盈利 = 净权利金（到期收盘 ≥ 短腿行权价）。",
            "最大亏损 = (宽度 − 净权利金) × 100 / 张（到期收盘 ≤ 保护腿行权价）。",
            "盈亏平衡 = 短腿行权价 − 净权利金。",
        ],
        "manage": [
            "净权利金的 50% 买回整个价差。",
            "标的触及短腿：整体平仓，或把整个价差向下 / 向后换；别只平一条腿，留着另一条会变成裸头寸。",
            "到期当天别留在短腿附近（钉住风险：不确定会不会被指派）——到期前一个交易日平仓更省心。",
        ],
        "risks": [
            "本页的保护腿离短腿只有约半个 σ，所以价差类「单笔回报」看着很高——代价是亏到最大值的概率也高很多，"
            "不能和卖 put 的回报直接比。",
            "短腿仍可能被提前指派；保护腿能在次日对冲，但有一晚的跳空敞口。",
            "两条腿各付一次点差，点差宽的标的成交价离中价很远。",
        ],
        "avoid": "点差很宽的标的；到期前有财报；你想要的是收取尽量多的权利金而不是封顶亏损（那就是卖 put）。",
    },
    "bear_call_spread": {
        "name": "熊市 call 价差",
        "oneline": "卖近端 call、同时买更远端 call 作保护：收净权利金，最大亏损被封顶。",
        "stance": "中性偏空，且想把上方风险锁死（这是不持股时「卖 call」的有限风险版本）。",
        "order": [
            "用「垂直价差 / Credit Spread」组合单一次报：卖出开仓短腿 call + 买入开仓保护 call，挂「净信用 ≥ 起挂价」。",
            "不要拆两笔分别挂（腿间风险）。",
            "券商要有价差权限；占用保证金 = (宽度 − 权利金) × 100 / 张。",
        ],
        "pnl": [
            "最大盈利 = 净权利金（到期收盘 ≤ 短腿行权价）。",
            "最大亏损 = (宽度 − 净权利金) × 100 / 张（到期收盘 ≥ 保护腿行权价）。",
            "盈亏平衡 = 短腿行权价 + 净权利金。",
        ],
        "manage": [
            "净权利金的 50% 买回整个价差。",
            "标的触及短腿：整体平仓或整体换仓，不要只平一条腿。",
            "有股息的票在除息日前处理价内的短腿 call（提前行权风险）。",
        ],
        "risks": [
            "保护腿离短腿很近，同样「回报高、亏满的概率也高」。",
            "上涨跳空（并购、财报）会直接把整个宽度亏掉。",
            "两条腿各付一次点差。",
        ],
        "avoid": "到期前有财报或并购传闻；点差很宽的标的；强势上涨趋势里逆势卖 call。",
    },
    "strangle": {
        "name": "宽跨（卖出）",
        "oneline": "同时卖出价外 put 与价外 call：赚的是标的待在两个行权价之间的时间价值。",
        "stance": "区间中性——预期不会大涨也不会大跌，赚时间价值与 IV 回落。",
        "order": [
            "可用「Strangle」组合单一次报，或两条单腿都卖出开仓；挂「合计净权利金 ≥ 起挂价」。",
            "需要最高一档期权权限与保证金（call 侧是裸卖）；本页「占用资金」只计 put 侧现金担保，真实保证金通常更高，以券商为准。",
            "成交后立刻为整个宽跨挂止盈买回单，并另设标的价位提醒（见「标的价位」）。",
        ],
        "pnl": [
            "最大盈利 = 合计权利金（到期收盘落在两个行权价之间）。",
            "下方最大亏损 = (put 行权价 − 权利金) × 100 / 张；上方亏损无上限。",
            "盈亏平衡有两个：put 行权价 − 权利金、call 行权价 + 权利金。",
        ],
        "manage": [
            "合计权利金的 50% 买回。",
            "一侧被测试（标的逼近某一短腿）：买回被测试的那一腿，或把没被测试的一腿向现价靠拢收更多权利金——别两腿一起放着。",
            "买回净价到 2× 权利金（见止损价）就整体平仓；不要「再等等」。",
        ],
        "risks": [
            "上方无上限、下方近乎 K×100：两头都有尾部，是六种里风险最大的。",
            "财报、利空、并购会同时顶穿一侧；IV 上升也会让浮亏放大。",
            "负 gamma 环境下波动倾向放大，宽跨尤其不利。",
        ],
        "avoid": "到期前有财报；现价处 gamma 为负；账户权限 / 保证金不够；你没法盯着它。",
    },
    "iron_condor": {
        "name": "铁鹰",
        "oneline": "牛市 put 价差 + 熊市 call 价差：区间中性，两头亏损都被封顶。",
        "stance": "区间中性，且要把两头的最大亏损都锁死（宽跨的有限风险版本）。",
        "order": [
            "用「Iron Condor」四腿组合单一次报：卖 put、买更低 put、卖 call、买更高 call，挂「净信用 ≥ 起挂价」。",
            "不要拆成四笔；至少也要按「put 价差一笔、call 价差一笔」下，且每笔都用组合单。",
            "券商要有价差权限（部分要最高一档）；占用保证金 = (较宽一翼宽度 − 净权利金) × 100 / 张——两侧不会同时亏满。",
        ],
        "pnl": [
            "最大盈利 = 净权利金（到期收盘落在两个短腿行权价之间）。",
            "最大亏损 = (较宽一翼的宽度 − 净权利金) × 100 / 张。",
            "盈亏平衡有两个：put 短腿 − 净权利金、call 短腿 + 净权利金。",
        ],
        "manage": [
            "净权利金的 50% 买回整个铁鹰。",
            "一侧被测试：平掉被测试的那一侧价差，或把整个铁鹰向被测试方向移；不要只留下没被测试的那一侧。",
            "到期前一个交易日平仓，避开钉住与指派的不确定。",
        ],
        "risks": [
            "四条腿各付一次点差：点差一宽，成交价离中价很远、回报被吞掉一截。",
            "保护腿离短腿只有约半个 σ，所以「单笔回报」高；亏满的概率也远高于看上去的数字。",
            "财报、跳空同样会把一侧打满。",
        ],
        "avoid": "点差很宽的标的；到期前有财报；你只想要权利金而不想有封顶的亏损（那就是宽跨 / 卖 put）。",
    },
}

#: 通用步骤：「下单前 / 成交后」，与结构无关
COMMON_BEFORE = [
    "用券商的期权链核对每条腿的合约（标的、到期日、行权价、Call / Put）与本页完全一致。",
    "用券商实时 bid / ask 重算「组合中价」，以它为起点——本页价格来自 CBOE 延迟报价，最早 t+1 才能真实成交。",
    "限价单 + 当日有效；从起挂价开始，每隔 1–2 分钟让一个最小报价单位，最低到底线价；到底线仍不成交就今天不做。",
]
COMMON_AFTER = [
    "成交后立刻挂「买入平仓」的 GTC 止盈单（价位见下）。",
    "给标的价位设提醒（短腿行权价、盈亏平衡、Zero Gamma），而不是只盯期权价。",
    "把管理日写进日历：到那天无论盈亏都评估平仓或换仓。",
]


def public_rules() -> dict:
    """规则常量（帮助页 / 前端经 `/api/method` 读，不在前端抄第二份）。"""
    return {"rules": {**RULES, "manage_dte": dict(RULES["manage_dte"]), "loss_budget_pct": list(RULES["loss_budget_pct"])},
            "disclaimer": DISCLAIMER, "guide": GUIDE, "before": list(COMMON_BEFORE), "after": list(COMMON_AFTER)}


# ─────────────────────────────── 小工具

def _num(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _tick(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(round(x / RULES["tick"]) * RULES["tick"], 2) + 0.0


def _per_contract(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x * CONTRACT, 2) + 0.0


def _dist_pct(level, spot) -> Optional[float]:
    lv, s = _num(level), _num(spot)
    return None if lv is None or not s or s <= 0 else round((lv / s - 1.0) * 100.0, 2)


def _rung_for(structure: str, rungs: Mapping[str, Any]):
    """结构 → 档位。put 侧 / call 侧结构各用本侧的档，宽跨与铁鹰要两侧都有。"""
    if structure in _PUT_SIDE:
        return rungs.get("put")
    if structure in _CALL_SIDE:
        return rungs.get("call")
    return dict(rungs) if all(rungs.get(s) is not None for s in ("put", "call")) else None


def _leg_view(leg: dict, ladder: Mapping[str, Any], spot) -> dict:
    side, role, rung = leg.get("side"), leg.get("role"), leg.get("rung")
    full = (((ladder or {}).get(side) or {}).get(rung) or {}).get(role) or {}
    return {
        "action": leg.get("action"), "side": side, "role": role, "cp": leg.get("cp"), "symbol": leg.get("symbol"),
        "strike": leg.get("strike"), "dist_pct": _dist_pct(leg.get("strike"), spot),
        "delta": full.get("delta"), "sigma_distance": full.get("sigma_distance"), "itm_prob": full.get("itm_prob"),
        "bid": full.get("bid"), "ask": full.get("ask"), "mid": full.get("mid"), "spread_pct": full.get("spread_pct"),
        "fill": leg.get("fill"),
    }


def _combo_mid(legs: List[dict]) -> Optional[float]:
    total = 0.0
    for lg in legs:
        m = _num(lg.get("mid"))
        if m is None:
            return None
        total += m if lg["action"] == "sell" else -m
    return total


def _manage_date(expiry: Optional[str], tenor: str) -> Optional[dict]:
    try:
        d = date.fromisoformat(str(expiry))
    except ValueError:
        return None
    n = RULES["manage_dte"].get(tenor)
    if n is None:
        return None
    return {"dte": n, "date": (d - timedelta(days=n)).isoformat()}


def _levels(structure: str, legs: List[dict], breakevens: List[float], spot, em: Mapping[str, Any],
            zg: Mapping[str, Any], majors: Mapping[str, Any]) -> List[dict]:
    """标的价位清单：每行 {kind, side, price, dist_pct, label}。按价格从低到高排。"""
    out: List[dict] = []
    puts = structure in _PUT_SIDE or structure in ("strangle", "iron_condor")
    calls = structure in _CALL_SIDE or structure in ("strangle", "iron_condor")

    def add(kind, side, price, label):
        p = _num(price)
        if p is not None:
            out.append({"kind": kind, "side": side, "price": p, "dist_pct": _dist_pct(p, spot), "label": label})

    for lg in legs:
        if lg["role"] == "short":
            add("short_strike", lg["side"], lg["strike"],
                f"短腿 {lg['cp'] == 'P' and 'put' or 'call'} 行权价——触及后进入被指派 / 到期价内的区域，按「怎么管理」处理")
        elif lg["role"] == "wing":
            add("wing_strike", lg["side"], lg["strike"], "保护腿行权价——收盘越过它即亏满最大亏损")
    for b in breakevens or []:
        add("breakeven", "put" if (_num(b) or 0) < (_num(spot) or 0) else "call", b, "盈亏平衡（到期口径，按自然价算；起挂价成交会略宽松）——收盘越过它开始净亏")
    if puts:
        add("em_edge", "put", em.get("lo"), "−1σ 期望波动边界（到期口径）")
        add("zero_gamma", "put", zg.get("nearest_below"), "下方最近 Zero Gamma——跌破后现价处 gamma 环境可能翻转（仅环境参考）")
        m = _num(majors.get("net_major_neg_strike"))
        if m is not None and (_num(spot) is None or m < spot):
            add("major", "put", m, "净 Major−（负净 GEX 最大的行权价，低于现价）——环境参考")
    if calls:
        add("em_edge", "call", em.get("hi"), "+1σ 期望波动边界（到期口径）")
        add("zero_gamma", "call", zg.get("nearest_above"), "上方最近 Zero Gamma——涨破后现价处 gamma 环境可能翻转（仅环境参考）")
        m = _num(majors.get("net_major_pos_strike"))
        if m is not None and (_num(spot) is None or m > spot):
            add("major", "call", m, "净 Major+（正净 GEX 最大的行权价，高于现价）——环境参考")
    out.sort(key=lambda r: (r["price"], r["kind"]))
    return out


def _cautions(structure: str, legs: List[dict], row: Mapping[str, Any]) -> List[dict]:
    out: List[dict] = []
    es = row.get("earnings_status")
    if es == "before_expiry":
        out.append({"level": "warn", "code": "earnings_before_expiry",
                    "text": "到期前有财报：IV 含事件溢价，但财报跳空是卖方最典型的一次性亏损来源。"
                            "要做就在财报前平仓，或只做带保护腿的结构。"})
    elif es == "unknown":
        # 现算不查财报日（`compute_live_detail` 没传 upcoming_fn），所以现算行恒为 unknown——
        # 恒亮的黄色警告没有信息量，降成说明；真要判断财报风险得自己去核实
        out.append({"level": "info", "code": "earnings_unknown",
                    "text": "本页的现算不查财报日（ETF 没有财报；个股请先去券商 / 公司公告核实到期前有没有财报，再决定做不做）。"})
    rt = row.get("route") or {}
    neg = any(str(r).startswith("negative_gamma_at_spot") for r in (rt.get("reasons") or []))
    if neg and structure in UNBOUNDED_CALL:
        out.append({"level": "warn", "code": "negative_gamma_naked",
                    "text": "路由判现价处 gamma 为负（系统已把两侧退到远档）：波动倾向放大，上方又是无上限的裸 call 风险——"
                            "同样想卖，优先看带保护腿的价差 / 铁鹰。"})
    elif neg:
        out.append({"level": "info", "code": "negative_gamma",
                    "text": "路由判现价处 gamma 为负：波动倾向放大，价位已按路由退到远档；仓位宜更小、管理更勤。"})
    wide = [lg for lg in legs if (_num(lg.get("spread_pct")) or 0) > RULES["wide_spread_pct"]]
    if wide:
        worst = max(_num(lg["spread_pct"]) for lg in wide)
        out.append({"level": "info", "code": "wide_spread",
                    "text": f"有 {len(wide)} 条腿点差超过 {RULES['wide_spread_pct'] * 100:.0f}%（最宽 {worst * 100:.0f}%）："
                            "按中价很难成交，实际成交价会往自然价靠，回报比表里低。"})
    if row.get("session_live"):
        out.append({"level": "info", "code": "intraday_quotes",
                    "text": "当前是盘中报价（15 分钟延迟）：下单前用券商实时价重算。"})
    return out


# ─────────────────────────────── 主入口

def _tier(structure: str, rungs: Mapping[str, Any], row: Mapping[str, Any], tenor: str, spot, em, zg, majors,
          quote_fn: Callable[[dict, str, Any], dict]) -> Optional[dict]:
    rung = _rung_for(structure, rungs)
    ladder = row.get("ladder")
    if rung is None or not isinstance(ladder, dict):
        return None
    q = quote_fn(ladder, structure, rung)
    legs = [_leg_view(lg, ladder, spot) for lg in (q.get("legs") or [])]
    base = {"rungs": {k: rungs.get(k) for k in ("put", "call")}, "quotable": bool(q.get("quotable")),
            "reason": q.get("reason"), "legs": legs, "cautions": _cautions(structure, legs, row)}
    if not q.get("quotable"):
        return base
    natural = _num(q.get("credit"))
    mid = _combo_mid(legs)
    if natural is None or mid is None:
        return {**base, "quotable": False, "reason": "mid_unavailable"}
    top = max(mid, natural)
    # 底线不许低于自然价（取整到最小报价单位时向上取）：低于它等于主动送价，立刻挂 bid / ask 就能成交
    nat_up = math.ceil(round(natural / RULES["tick"], 6)) * RULES["tick"]
    floor = max(_tick(natural + RULES["floor_share"] * (top - natural)), round(nat_up, 2))
    start = max(_tick(top), floor)
    collateral = _num(q.get("collateral"))
    max_loss = _num(q.get("max_loss"))
    # 价差 / 铁鹰的占用资金 = 宽度 − 权利金：按哪个价成交就用哪个价算（与 `structure_quote` 同一口径），
    # 否则「起挂价权利金 ÷ 自然价口径的占用资金」比表里同定义的数偏低
    width = (max_loss + natural) if (structure in DEFINED_RISK and max_loss is not None) else None

    def yld(x):
        coll = (width - x) if width is not None else collateral
        return None if (coll is None or coll <= 0 or x is None) else round(x / coll, 6)

    tp = max(RULES["tick"], _tick(start * RULES["profit_take"]))
    stop = _tick(start * RULES["stop_mult"])
    stop_note = None
    if width is not None:                   # 价差整体到期最多值 width
        if stop >= width:
            stop, stop_note = None, "价差最大价值（宽度）不超过 2 倍权利金：买回价止损形同虚设，改用标的价位触发。"
    limit = {"start": start, "floor": floor, "natural": round(natural, 4),
             "start_per_contract": _per_contract(start), "floor_per_contract": _per_contract(floor),
             "natural_per_contract": _per_contract(natural),
             "yield_start": yld(start), "yield_floor": yld(floor)}
    exit_ = {"take_profit_debit": tp, "take_profit_per_contract": _per_contract(tp),
             "stop_debit": stop, "stop_per_contract": _per_contract(stop), "stop_note": stop_note,
             "stop_loss_per_contract": None if stop is None else _per_contract(stop - start),
             "manage": _manage_date(row.get("expiry"), tenor)}
    payout = {"max_profit_per_contract": _per_contract(start),
              "max_loss_per_contract": _per_contract(max_loss), "max_loss_unbounded": max_loss is None,
              "collateral_per_contract": _per_contract(collateral),
              "breakevens": list(q.get("breakevens") or []),
              "breakeven_pct": [_dist_pct(b, spot) for b in (q.get("breakevens") or [])]}
    return {**base, "limit": limit, "exit": exit_, "payout": payout,
            "levels": _levels(structure, legs, list(q.get("breakevens") or []), spot, em, zg, majors)}


def build(detail: Mapping[str, Any], quote_fn: Callable[[dict, str, Any], dict], base_rung: float) -> dict:
    """`compute_live_detail` 的结果 → `{tenor: {structure: {route, base}}}`。

    `route` 档是系统路由选的那一档（**推荐**）；`base` 是固定 0.20Δ 的对照。某一档路由判不可用 ⇒ `route` 为 None。
    整个函数不抛：调用方（service）另有 try/except 与日志，这里只保证输入缺字段时给 None 而非 KeyError。"""
    spot = _num(detail.get("underlying_price"))
    views = detail.get("views") or {}
    rv = views.get("le_45dte") or {}
    zg, majors = rv.get("zero_gamma") or {}, rv.get("majors") or {}
    # 说明文字与规则常量随载荷一起给（同一个 `public_rules()`，前端不抄、也不另取一次）
    out: Dict[str, Any] = {"available": True, "tenors": {}, **public_rules()}
    for tenor, row in (detail.get("tenors") or {}).items():
        if not isinstance(row, dict) or row.get("status") != "recorded":
            out["tenors"][tenor] = None
            continue
        sts = row.get("structures") or {}
        route_rungs = sts.get("route_rungs") or {}
        em = (detail.get("expected_move") or {}).get(tenor) or {}
        base_rungs = {"put": base_rung, "call": base_rung}
        per: Dict[str, Any] = {}
        for st in STRUCTURES:
            per[st] = {
                "route": _tier(st, route_rungs, row, tenor, spot, em, zg, majors, quote_fn),
                "base": _tier(st, base_rungs, row, tenor, spot, em, zg, majors, quote_fn),
            }
        out["tenors"][tenor] = per
    return out

