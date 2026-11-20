"""深度页「证伪条件」里能可靠自动判的那几条（v0.45.777）——只给深度页看。

为什么只判这几条
----------------
`thesis_breaks_config.json` 里给 13 只票写的 64 条人工条件，绝大多数是分部 / 运营指标（数据中心收入、Azure 增速、日活）
或事件（CEO 离职、出口禁令、FDA 暂停试验），免费的结构化数据源给不出来、规则也判不了。这里只收**有免费结构化数据、
判据是一个数和一个阈值**的那 12 条：季度毛利率、EPS 对预期、自由现金流、现金能撑几个月、股息、10 年期国债收益率、
USDC 价格与市场份额。其余照旧「未核对」——错的「未触发」比诚实的「未核对」更危险。

怎么对上条件
------------
规则表 `CHECKS` 按「票 + 配置里的条件 id」登记，**连同登记时的条件原文**。配置里的原文与登记的不一样
（阈值被改过、条件被换了）⇒ 这条不判，回 `trigger_changed`、页面照旧写「未核对」并说明原因：拿旧阈值去判新条件是错的。
配置是代码检出里的 `thesis_breaks_config.json`（与扫描读的同一份，经 `thesis_breaks.ThesisBreakConfig`）。

口径
----
· 「A 且 B」型条件（现金 < 6 个月**且**融资无望）只判数字那半：数字那半不满足 ⇒ 整条一定没触发（`not_fired`）；
  满足 ⇒ `partial`（「数字部分已满足，另一半要人工判断」），**不写「已触发」**；
· 拿四舍五入过的数比阈值（Yahoo 的股息只给到 3 位小数、EPS 2 位）⇒ 按舍入区间判：区间整个在阈值那一侧才下结论，
  跨过阈值 ⇒ `borderline`（判不了）；
· 最近一期太旧（`RULES["max_quarter_age_days"]`）⇒ `stale`，不拿旧季度当「最新」；
· 取到的是**此刻**的数据 ⇒ 只给最新一天的深度页用（看历史日期不判，由 service 管）。

**只展示**：不进评分、不进账本、不写配置（配置还有一个外部的每周任务在写）、不改扫描记的 `thesis_break_evaluations`。
判定函数是纯函数；取数在 `fetch_*`，取不到 / 被限流都带原因返回，永不抛。

有权威值的量用权威源（v0.45.785，用户要求）
--------------------------------------------
· 10 年期国债收益率：**美国财政部每日收益率曲线**（经仓库已有的 `treasury_yields`，与扫描的宏观 10Y 同源）——
  「10 年期国债收益率」通常指的就是这个数（FRED DGS10 转发的也是它）。此前用 Yahoo ^TNX：那是最近一只 10 年期新券的
  实时收益率，与官方值实测差 0.02～0.03 个百分点，在阈值附近会与官方结论相反。官方只给两位小数 ⇒ 按 ±0.005 舍入区间判；
· USDC 流通量：**发行方 Circle 的公开接口**（各链合计）。份额的分母没有权威定义，仍取 DeFiLlama 的美元稳定币合计，
  但把其中 DeFiLlama 自己统计的 USDC 换成 Circle 的数，分子分母里的 USDC 是同一个数；
· USDC 价格（脱钩）没有唯一真值（每个交易所不同），仍用 Yahoo 小时线。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from alphabot import beta_long as BL

#: 阈值以外的口径常量（条件里的阈值写在 CHECKS 里，与条件原文放一起）
RULES = {
    "max_quarter_age_days": 200,     # 最近一季的季末早于今天这么多天 ⇒ 数据过旧（季报约在季末后 1～2 个月出，留一个季度的余量）
    "max_yield_age_days": 7,         # 国债收益率最近一个官方值的日期早于今天这么多天 ⇒ 过旧
    "ust_round": 0.005,              # 财政部收益率只给 2 位小数 ⇒ 真值在 ±0.005 内
    "max_supply_age_days": 3,        # Circle 各链流通量的最近更新早于今天这么多天 ⇒ 过旧
    "quarter_gap_days": (60, 120),   # 相邻两季的季末相隔多少天才算「连续的两季」
    "year_gap_days": (300, 430),     # 「一年前那次股息」的日期范围
    "dividend_round": 0.0005,        # Yahoo 股息给到 3 位小数 ⇒ 真值在 ±0.0005 内
    "eps_round": 0.005,              # EPS 实际值给到 2 位小数
    "usdc_window_days": 5,           # USDC 小时线看过去几天
    "usdc_min_coverage": 0.9,        # 小时线覆盖率低于这个 ⇒ 判不了（缺的那几小时可能正是脱钩）
    "burn_quarters": 4,              # 现金消耗按最近几季自由现金流的平均算
    "cache_ttl_sec": 6 * 3600.0,     # 取到的数据在内存里留多久（季报一季才变一次）
    "fail_ttl_sec": 600.0,           # 取失败的在这么久内不重取（免得每次开页都去撞限流）
}

#: 原因码唯一表（页面翻成人话；测试双向核对）
REASONS = {
    "stale": "数据过旧",
    "insufficient": "数据不够",
    "nonconsecutive": "季度不连续，比不了",
    "borderline": "临界，判不了",
    "estimate_nonpositive": "预期 EPS ≤ 0，「低于预期多少 %」没有意义",
    "out_of_bounds": "取到的值不合常理，不用",
    "rate_limited": "Yahoo 限流",
    "fetch_error": "取数出错",
    "empty": "数据源返回了空结果（可能是限流、还没发布或临时故障，过一会儿会重取）",
    "trigger_changed": "条件原文与登记时不同（阈值可能改过），自动核对没对上",
    "not_in_config": "配置里没有这一条",
    "not_latest_date": "自动核对只对最新一天",
    "demo": "演示模式不核对",
    "no_fetcher": "这个 Alpha Bot 没接取数",
}

STATUS_ZH = {"fired": "已触发", "not_fired": "未触发", "partial": "数字部分已满足", "unjudgeable": "判不了"}

#: (票, 配置里的条件 id) → 判法。`trigger` 是登记时配置里的条件原文，与现行配置逐字比对
CHECKS: Dict[Tuple[str, str], dict] = {
    ("NVDA", "margin_compression"): {"trigger": "季度环比下降 > 200bps", "kind": "gm_qoq_drop", "pp": 2.0},
    ("NVDA", "eps_miss_severe"): {"trigger": "实际 < 预期 20%+", "kind": "eps_miss", "pct": 20.0},
    ("TSLA", "margin_drop"): {"trigger": "环比下降 > 200bps", "kind": "gm_qoq_drop", "pp": 2.0},
    ("MSFT", "msft_eps_miss"): {"trigger": "实际 < 预期 15%+", "kind": "eps_miss", "pct": 15.0},
    ("AMZN", "amzn_eps_miss"): {"trigger": "实际 EPS < 预期 20%+", "kind": "eps_miss", "pct": 20.0},
    ("BILI", "bili_cash_burn"): {"trigger": "自由现金流连续 3 季度为负且无明确改善路径", "kind": "fcf_neg_streak", "n": 3,
                                 "manual_part": "「无明确改善路径」"},
    ("RKLB", "rklb_cash_depletion"): {"trigger": "现金储备 < 6 个月运营，且融资无望", "kind": "cash_runway", "months": 6.0,
                                      "manual_part": "「融资无望」"},
    ("CRCL", "usdc_market_share"): {"trigger": "USDC 全球稳定币市场份额 < 20%", "kind": "usdc_share", "pct": 20.0},
    ("CRCL", "crcl_depeg_event"): {"trigger": "USDC 价格跌破 $0.97 超过 24 小时", "kind": "usdc_depeg", "price": 0.97, "hours": 24},
    ("JNJ", "jnj_dividend_cut"): {"trigger": "JNJ 宣布削减季度股息", "kind": "dividend_cut"},
    ("NEE", "nee_rate_risk"): {"trigger": "10 年期国债收益率 > 5.0%（公用事业估值压制）", "kind": "yield_above", "pct": 5.0},
    ("NEE", "nee_dividend_growth"): {"trigger": "宣布低于承诺 10% 年股息增速", "kind": "dividend_growth", "pct": 10.0},
}

#: 每种判法要哪份数据（`票:份` 或全局的 `份`）、数据从哪来（页面照写）
NEEDS = {
    "gm_qoq_drop": (("income",), "Yahoo 季度利润表"),
    "eps_miss": (("eps",), "Yahoo 盈利历史（实际 vs 预期）"),
    "fcf_neg_streak": (("cashflow",), "Yahoo 季度现金流量表"),
    "cash_runway": (("balance", "cashflow"), "Yahoo 季度资产负债表 + 现金流量表"),
    "dividend_cut": (("dividends",), "Yahoo 股息记录"),
    "dividend_growth": (("dividends",), "Yahoo 股息记录"),
    "yield_above": (("@ust10",), "美国财政部每日收益率曲线（10 年期，官方值）"),
    "usdc_depeg": (("@usdc",), "Yahoo USDC-USD 小时线"),
    "usdc_share": (("@circle", "@stables"), "Circle 公布的 USDC 流通量 + DeFiLlama 统计的其余美元稳定币"),
}
GLOBAL_PARTS = ("@ust10", "@usdc", "@stables", "@circle")
#: 舍入区间比阈值时的浮点余量：1.339 + 0.001 在浮点下比 1.340 小一点点，没有它会把「差额正好等于舍入范围」判成已触发
_EPS = 1e-9


# ───────────────────────────── 小工具（纯）

def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _age(s, today: date) -> Optional[int]:
    d = _d(s)
    return (today - d).days if d else None


def _ts(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace(" ", "T"))
    except ValueError:
        return None


def _pp(x: float) -> str:
    return f"{x:+.2f} 个百分点"


def _verdict(status: str, facts: str = "", *, as_of=None, reason: Optional[str] = None, detail: str = "") -> dict:
    out = {"status": status, "status_zh": STATUS_ZH[status], "facts": facts, "as_of": as_of, "reason": reason,
           "reason_zh": (REASONS[reason] + (f"：{detail}" if detail else "")) if reason else None}
    return out


def _cant(reason: str, detail: str = "", facts: str = "", as_of=None) -> dict:
    return _verdict("unjudgeable", facts, as_of=as_of, reason=reason, detail=detail)


def _fresh(end, today: date) -> Optional[dict]:
    """最近一期够不够新；不够 ⇒ 返回 `stale` 判定，够 ⇒ None。"""
    a = _age(end, today)
    if a is None:
        return _cant("insufficient", "最近一期没有日期")
    if a > RULES["max_quarter_age_days"]:
        return _cant("stale", f"最近一期截至 {str(end)[:10]}，已过 {a} 天", as_of=str(end)[:10])
    return None


def _consecutive(a, b) -> bool:
    da, db = _d(a), _d(b)
    lo, hi = RULES["quarter_gap_days"]
    return bool(da and db) and lo <= (da - db).days <= hi


# ───────────────────────────── 判定（纯函数）。输入一律是新到旧排好的行

def judge_gm_qoq_drop(income: List[dict], pp: float, today: date) -> dict:
    """毛利率环比下降超过 `pp` 个百分点。`income` = `[{end, gross_profit, revenue}]`。"""
    # 只比最近两季：缺数据的季度不跳过（跳过就会拿更早的两季冒充「最近一季」）
    ok = lambda r: (_num(r.get("revenue")) or 0) > 0 and _num(r.get("gross_profit")) is not None  # noqa: E731
    if len(income) < 2:
        return _cant("insufficient", "不到两季")
    cur, prev = income[0], income[1]
    if not (ok(cur) and ok(prev)):
        return _cant("insufficient", f"{cur.get('end')} 或 {prev.get('end')} 那季缺毛利或收入", as_of=cur.get("end"))
    bad = _fresh(cur["end"], today)
    if bad:
        return bad
    if not _consecutive(cur["end"], prev["end"]):
        return _cant("nonconsecutive", f"{cur['end']} 与 {prev['end']}")
    g1 = cur["gross_profit"] / cur["revenue"] * 100
    g0 = prev["gross_profit"] / prev["revenue"] * 100
    facts = f"截至 {cur['end']} 那季毛利率 {g1:.2f}%，上一季（{prev['end']}）{g0:.2f}%，变化 {_pp(g1 - g0)}；阈值：下降超过 {pp:g} 个百分点"
    return _verdict("fired" if g0 - g1 > pp else "not_fired", facts, as_of=cur["end"])


def judge_eps_miss(eps: List[dict], pct: float, today: date) -> dict:
    """最近一次财报 EPS 实际值低于预期 `pct`% 以上。`eps` = `[{quarter, actual, estimate}]`。"""
    # 最近一次**已公布**的季度（还没公布的季度没有实际值，跳过它是对的）；它缺预期值 ⇒ 判不了，不退回更早的季度
    rows = [r for r in eps if _num(r.get("actual")) is not None]
    if not rows:
        return _cant("insufficient", "没有已公布实际值的季度")
    r = rows[0]
    if _num(r.get("estimate")) is None:
        return _cant("insufficient", f"{r.get('quarter')} 那季没有预期值", as_of=r.get("quarter"))
    bad = _fresh(r["quarter"], today)
    if bad:
        return bad
    act, est = r["actual"], r["estimate"]
    if est <= 0:
        return _cant("estimate_nonpositive", f"预期 {est:.2f}", as_of=r["quarter"])
    line = est * (1 - pct / 100)
    gap = (act / est - 1) * 100
    facts = f"截至 {r['quarter']} 那季 EPS 实际 {act:.2f}、预期 {est:.2f}，差 {gap:+.1f}%；阈值：比预期低 {pct:g}% 以上（即低于 {line:.2f}）"
    tol = RULES["eps_round"]
    if act + tol < line - _EPS:
        return _verdict("fired", facts, as_of=r["quarter"])
    if act - tol >= line + _EPS:
        return _verdict("not_fired", facts, as_of=r["quarter"])
    return _cant("borderline", "EPS 实际值只给到两位小数，落在阈值的舍入范围内", facts, as_of=r["quarter"])


def judge_fcf_neg_streak(cashflow: List[dict], n: int, manual_part: str, today: date) -> dict:
    """自由现金流连续 `n` 季为负（「且 …」那半要人工判）。`cashflow` = `[{end, fcf}]`。"""
    if not cashflow:
        return _cant("insufficient", "没有自由现金流")
    bad = _fresh(cashflow[0]["end"], today)
    if bad:
        return bad
    last = cashflow[:n]
    if len(last) < n or any(_num(r.get("fcf")) is None for r in last):
        return _cant("insufficient", f"最近 {n} 季里有缺数的（不拿更早的季度顶替）", as_of=cashflow[0]["end"])
    rows = last
    if not all(_consecutive(a["end"], b["end"]) for a, b in zip(last, last[1:])):
        return _cant("nonconsecutive", "、".join(r["end"] for r in last), as_of=rows[0]["end"])
    vals = "、".join(f"{r['end']} {r['fcf'] / 1e6:+,.0f} 百万" for r in last)
    facts = f"最近 {n} 季自由现金流：{vals}"
    if all(r["fcf"] < 0 for r in last):
        return _verdict("partial", facts + f"——连续 {n} 季为负；条件还要求{manual_part}，那半要人工判断", as_of=rows[0]["end"])
    return _verdict("not_fired", facts + f"——不是连续 {n} 季为负，整条不成立", as_of=rows[0]["end"])


def judge_cash_runway(balance: List[dict], cashflow: List[dict], months: float, manual_part: str, today: date) -> dict:
    """现金按最近几季平均自由现金流的消耗速度能撑几个月，少于 `months` 个月（「且 …」那半要人工判）。"""
    if not balance or _num(balance[0].get("cash")) is None:
        return _cant("insufficient", "最近一季没有现金余额")
    bal = balance
    bad = _fresh(bal[0]["end"], today)
    if bad:
        return bad
    k = int(RULES["burn_quarters"])
    fl = []
    for r in cashflow[:k]:          # 从最近一季起连续取，遇到缺数就停（不跳过）
        if _num(r.get("fcf")) is None:
            break
        fl.append(r)
    if len(fl) < 2:
        return _cant("insufficient", "最近不到两季有自由现金流", as_of=bal[0]["end"])
    if fl[0]["end"] != bal[0]["end"]:
        return _cant("nonconsecutive", f"现金截至 {bal[0]['end']}，现金流最近一季是 {fl[0]['end']}，不是同一季", as_of=bal[0]["end"])
    if not all(_consecutive(a["end"], b["end"]) for a, b in zip(fl, fl[1:])):
        return _cant("nonconsecutive", "、".join(r["end"] for r in fl), as_of=bal[0]["end"])
    cash = bal[0]["cash"]
    avg = sum(r["fcf"] for r in fl) / len(fl)
    head = (f"截至 {bal[0]['end']} 现金及短期投资 {cash / 1e6:,.0f} 百万；最近 {len(fl)} 季自由现金流平均每季 {avg / 1e6:+,.0f} 百万"
            f"（「运营消耗」按自由现金流算，含资本开支）")
    if avg >= 0:
        return _verdict("not_fired", head + "——没有在消耗现金，整条不成立", as_of=bal[0]["end"])
    runway = cash / (-avg / 3)
    facts = head + f"，按这个速度约能撑 {runway:.1f} 个月；阈值：少于 {months:g} 个月"
    if runway < months:
        return _verdict("partial", facts + f"——数字那半已满足；条件还要求{manual_part}，那半要人工判断", as_of=bal[0]["end"])
    return _verdict("not_fired", facts + "——数字那半不满足，整条不成立", as_of=bal[0]["end"])


def _div_rows(divs: List[dict]) -> List[dict]:
    return [r for r in divs if (_num(r.get("amount")) or 0) > 0 and _d(r.get("date"))]


def judge_dividend_cut(divs: List[dict], today: date) -> dict:
    """最近一次股息比上一次少。`divs` = `[{date, amount}]`。"""
    rows = _div_rows(divs)
    if len(rows) < 2:
        return _cant("insufficient", "不到两次股息记录")
    cur, prev = rows[0], rows[1]
    bad = _fresh(cur["date"], today)
    if bad:
        return bad
    facts = f"最近一次 {cur['date']} 每股 {cur['amount']:.3f}，上一次 {prev['date']} {prev['amount']:.3f}"
    tol = 2 * RULES["dividend_round"]
    if cur["amount"] + tol < prev["amount"] - _EPS:
        return _verdict("fired", facts + "——比上一次少", as_of=cur["date"])
    if cur["amount"] >= prev["amount"]:
        return _verdict("not_fired", facts + "——没有减少", as_of=cur["date"])
    return _cant("borderline", "差额小于 Yahoo 股息的舍入范围", facts, as_of=cur["date"])


def judge_dividend_growth(divs: List[dict], pct: float, today: date) -> dict:
    """最近一次股息比约一年前那次的增速低于 `pct`%。按舍入区间判（Yahoo 股息只给到 3 位小数）。"""
    rows = _div_rows(divs)
    if not rows:
        return _cant("insufficient", "没有股息记录")
    cur = rows[0]
    bad = _fresh(cur["date"], today)
    if bad:
        return bad
    lo_d, hi_d = RULES["year_gap_days"]
    cd = _d(cur["date"])
    old = [r for r in rows[1:] if lo_d <= (cd - _d(r["date"])).days <= hi_d]
    if not old:
        return _cant("insufficient", "找不到约一年前的那次股息", as_of=cur["date"])
    prev = min(old, key=lambda r: abs((cd - _d(r["date"])).days - 365))
    e = RULES["dividend_round"]
    g = (cur["amount"] / prev["amount"] - 1) * 100
    g_lo = ((cur["amount"] - e) / (prev["amount"] + e) - 1) * 100
    g_hi = ((cur["amount"] + e) / (prev["amount"] - e) - 1) * 100
    facts = (f"最近一次 {cur['date']} 每股 {cur['amount']:.3f}，约一年前 {prev['date']} {prev['amount']:.3f}，"
             f"按 Yahoo 的数算增速 {g:.2f}%（三位小数的舍入下真值在 {g_lo:.2f}%～{g_hi:.2f}% 之间）；阈值：低于 {pct:g}%")
    if g_hi < pct - _EPS:
        return _verdict("fired", facts, as_of=cur["date"])
    if g_lo >= pct + _EPS:
        return _verdict("not_fired", facts, as_of=cur["date"])
    return _cant("borderline", "舍入范围跨过了阈值，要看公司公告里的精确股息", facts, as_of=cur["date"])


def judge_yield_above(ust: Optional[dict], pct: float, today: date) -> dict:
    """10 年期美债收益率（财政部每日收益率曲线的 10 年期，单位 %）高于 `pct`。`ust` = `{date, y10}`。

    官方值只给两位小数 ⇒ 真值在 ±`ust_round` 内：区间整个在阈值上方才写已触发、整个不在上方才写未触发，正好压线 ⇒ 临界。"""
    v = _num((ust or {}).get("y10"))
    if v is None:
        return _cant("insufficient", "没有收益率数据")
    d = ust.get("date")
    a = _age(d, today)
    if a is None or a > RULES["max_yield_age_days"]:
        return _cant("stale", f"最近一个官方值是 {d}", as_of=d)
    if not 0 < v < 20:
        return _cant("out_of_bounds", f"{v}", as_of=d)
    e = RULES["ust_round"]
    facts = f"财政部公布的 {d} 10 年期收益率 {v:.2f}%（官方只给两位小数）；阈值：高于 {pct:g}%"
    if v - e > pct + _EPS:
        return _verdict("fired", facts, as_of=d)
    if v + e <= pct + _EPS:
        return _verdict("not_fired", facts, as_of=d)
    return _cant("borderline", "官方值正好压在阈值上，两位小数分不出高低", facts, as_of=d)


def judge_usdc_depeg(hourly: List[dict], price: float, hours: int, today: date) -> dict:
    """过去几天里 USDC 小时收盘连续低于 `price` 的最长时长是否达到 `hours` 小时。`hourly` = `[{t: ISO, close}]`（旧到新也行，这里自己排）。"""
    rows = sorted((r for r in hourly if _num(r.get("close")) is not None and r.get("t") and _ts(r["t"])),
                  key=lambda r: _ts(r["t"]))
    days = int(RULES["usdc_window_days"])
    if not rows:
        return _cant("insufficient", "没有小时线")
    # 只看最后一根往前整 `days` 天（v0.45.785 修）：Yahoo 的 period="5d" 从 4 天前的 UTC 零点起算、不是往回整 120 小时，
    # 原先拿它直接除以 120 ⇒ 晚上打开页面覆盖率只有八成多、天天「数据不够」。现在取数多取两天，这里截出完整窗口再算覆盖率
    end = _ts(rows[-1]["t"])
    rows = [r for r in rows if _ts(r["t"]) > end - timedelta(hours=days * 24)]
    cov = len(rows) / (days * 24)
    last = str(rows[-1]["t"])
    if _age(last, today) is None or _age(last, today) > 1:
        return _cant("stale", f"最近一根是 {last[:16]}", as_of=last[:10])
    low = min(r["close"] for r in rows)
    # 两种连续时长：`best` 只数真有的、相邻的小时（中间缺一小时就断开）——够了才写已触发；
    # `best_possible` 把缺的小时都当成「可能在 price 之下」——它也不够，才能写未触发；两者之间 ⇒ 临界
    run = best = poss = best_poss = 0
    prev_t = None
    for r in rows:
        t = _ts(r["t"])
        missing = max(0, round((t - prev_t).total_seconds() / 3600) - 1) if (t and prev_t) else 0
        below = r["close"] < price
        run = (run + 1 if missing == 0 else 1) if below else 0
        poss = poss + missing + 1 if below else 0
        best, best_poss = max(best, run), max(best_poss, poss)
        prev_t = t
    facts = (f"过去 {days} 天 {len(rows)} 根小时线（覆盖 {min(cov, 1) * 100:.0f}%），最低收盘 {low:.4f}，"
             f"低于 {price:g} 最长连续 {best} 小时；阈值：连续 {hours} 小时以上")
    if best >= hours:
        return _verdict("fired", facts, as_of=last[:10])
    if cov < RULES["usdc_min_coverage"]:
        return _cant("insufficient", f"小时线只覆盖 {cov * 100:.0f}%，缺的那几小时判不了", facts, as_of=last[:10])
    if best_poss >= hours:
        return _cant("borderline", f"把缺的小时算进去最长可达 {best_poss} 小时，缺的那几小时可能正是脱钩", facts, as_of=last[:10])
    return _verdict("not_fired", facts, as_of=last[:10])


def judge_usdc_share(st: Optional[dict], circle: Optional[dict], pct: float, today: date) -> dict:
    """USDC 占全部美元稳定币流通量的份额低于 `pct`%。

    分子 = Circle 公布的 USDC 流通量（`circle` = `{usdc, chains, as_of}`）；分母 = DeFiLlama 的美元稳定币合计里，
    把 DeFiLlama 自己统计的 USDC 换成 Circle 的数（`st` = `{usdc, total, n_assets, as_of}`）——分子分母里的 USDC 是同一个数。"""
    c = _num((circle or {}).get("usdc"))
    if c is None or c <= 0:
        return _cant("insufficient", "没有取到 Circle 公布的 USDC 流通量")
    c_as_of = circle.get("as_of")
    a = _age(c_as_of, today)
    if a is None or a > RULES["max_supply_age_days"]:
        return _cant("stale", f"Circle 的数最近更新于 {c_as_of or '未知'}", as_of=c_as_of)
    if not st or (_num(st.get("usdc")) or 0) <= 0 or (_num(st.get("total")) or 0) <= 0:
        return _cant("insufficient", "没有取到 DeFiLlama 的稳定币合计（或其中没有 USDC）")
    others = st["total"] - st["usdc"]
    if others <= 0:
        return _cant("out_of_bounds", "DeFiLlama 合计里除了 USDC 没有别的")
    total = others + c
    share = c / total * 100
    if not 0 < share <= 100:
        return _cant("out_of_bounds", f"{share:.1f}%")
    n = st.get("n_assets")
    n_others = f"{n - 1} 种" if isinstance(n, int) and n > 1 else "其余"
    facts = (f"USDC 流通 {c / 1e9:,.1f} 十亿美元（Circle 公布，{circle.get('chains', '—')} 条链合计，更新于 {c_as_of}），"
             f"占美元稳定币合计 {total / 1e9:,.1f} 十亿美元的 {share:.1f}%（其余 {n_others}取 DeFiLlama 统计）；阈值：低于 {pct:g}%")
    return _verdict("fired" if share < pct else "not_fired", facts, as_of=c_as_of)


def judge(spec: dict, data: Dict[str, object], today: date) -> dict:
    """按 `spec["kind"]` 分派。`data` 的键是 `NEEDS` 里列的那几份（值缺 = 没取到，调用方先处理）。"""
    k = spec["kind"]
    if k == "gm_qoq_drop":
        return judge_gm_qoq_drop(data["income"], spec["pp"], today)
    if k == "eps_miss":
        return judge_eps_miss(data["eps"], spec["pct"], today)
    if k == "fcf_neg_streak":
        return judge_fcf_neg_streak(data["cashflow"], spec["n"], spec["manual_part"], today)
    if k == "cash_runway":
        return judge_cash_runway(data["balance"], data["cashflow"], spec["months"], spec["manual_part"], today)
    if k == "dividend_cut":
        return judge_dividend_cut(data["dividends"], today)
    if k == "dividend_growth":
        return judge_dividend_growth(data["dividends"], spec["pct"], today)
    if k == "yield_above":
        return judge_yield_above(data["@ust10"], spec["pct"], today)
    if k == "usdc_depeg":
        return judge_usdc_depeg(data["@usdc"], spec["price"], spec["hours"], today)
    if k == "usdc_share":
        return judge_usdc_share(data["@stables"], data["@circle"], spec["pct"], today)
    raise KeyError(k)


def match(ticker: str, config: dict) -> List[dict]:
    """这只票配置里的人工条件逐条对规则表。返回 `[{id, label, level, trigger, spec | None, reason | None}]`，
    只列登记过的条件（没登记的不出现——它们照旧是「需人工判断」）。"""
    out = []
    seen = set()
    for lv in ("level_1_warning", "level_2_stop_loss"):
        block = (config or {}).get(lv)
        conds = block.get("conditions") if isinstance(block, dict) else block
        for c in conds if isinstance(conds, list) else []:
            if not isinstance(c, dict) or c.get("_machine"):
                continue
            key = (ticker, str(c.get("id")))
            spec = CHECKS.get(key)
            if spec is None:
                continue
            seen.add(key)
            same = str(c.get("trigger") or "") == spec["trigger"]
            out.append({"id": key[1], "label": c.get("metric"), "level": 2 if "2" in lv else 1, "trigger": c.get("trigger"),
                        "spec": spec if same else None, "reason": None if same else "trigger_changed"})
    for key, spec in CHECKS.items():
        if key[0] == ticker and key not in seen:
            out.append({"id": key[1], "label": None, "level": None, "trigger": spec["trigger"], "spec": None, "reason": "not_in_config"})
    return out


# ───────────────────────────── 取数（打网；永不抛）

def _frame_rows(df, fields: Dict[str, Tuple[str, ...]]) -> List[dict]:
    """yfinance 财报表（行 = 科目、列 = 季末）→ `[{end, 字段…}]` 新到旧。每个字段按给的科目名依次找第一个有的。"""
    if df is None or not hasattr(df, "columns") or getattr(df, "empty", True):
        return []
    out = []
    for col in df.columns:
        row = {"end": str(col)[:10]}
        for name, labels in fields.items():
            v = None
            for lb in labels:
                if lb in df.index:
                    v = _num(df.loc[lb, col])
                    if v is not None:
                        break
            row[name] = v
        out.append(row)
    return sorted(out, key=lambda r: r["end"], reverse=True)


def _part(yf_module, gated: Callable, part: str, ticker: str):
    """取一份数据 → 规整后的行。`gated(fn, what)` 是限流闸（测试注入恒等函数）。"""
    if part == "income":
        df = gated(lambda: yf_module.Ticker(ticker).quarterly_income_stmt, "Ticker.quarterly_income_stmt")
        return _frame_rows(df, {"gross_profit": ("Gross Profit",), "revenue": ("Total Revenue", "Operating Revenue")})
    if part == "cashflow":
        df = gated(lambda: yf_module.Ticker(ticker).quarterly_cashflow, "Ticker.quarterly_cashflow")
        return _frame_rows(df, {"fcf": ("Free Cash Flow",)})
    if part == "balance":
        df = gated(lambda: yf_module.Ticker(ticker).quarterly_balance_sheet, "Ticker.quarterly_balance_sheet")
        return _frame_rows(df, {"cash": ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")})
    if part == "eps":
        df = gated(lambda: yf_module.Ticker(ticker).earnings_history, "Ticker.earnings_history")
        if df is None or getattr(df, "empty", True):
            return []
        rows = [{"quarter": str(q)[:10], "actual": _num(a), "estimate": _num(e)}
                for q, a, e in zip(df.index, df.get("epsActual", [None] * len(df)), df.get("epsEstimate", [None] * len(df)))]
        return sorted(rows, key=lambda r: r["quarter"], reverse=True)
    if part == "dividends":
        s = gated(lambda: yf_module.Ticker(ticker).dividends, "Ticker.dividends")
        if s is None or getattr(s, "empty", True):
            return []
        return sorted(({"date": str(i)[:10], "amount": _num(v)} for i, v in s.items()), key=lambda r: r["date"], reverse=True)
    raise KeyError(part)


def _closes(hist, key: str) -> List[dict]:
    if hist is None or "Close" not in getattr(hist, "columns", ()):
        return []
    return [{key: str(i) if key == "t" else str(i)[:10], "close": _num(c)} for i, c in zip(hist.index, hist["Close"])]


def _et_today() -> date:
    """美东今天（与 `treasury_yields._et_month` 同一时区口径；测试可替换）。"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()


def _fetch_ust10(treasury_get: Optional[Callable[[Optional[str]], Optional[dict]]], today: date) -> Optional[dict]:
    """财政部最新的 10 年期官方值 `{date, y10}`；取不到 → None。`treasury_get(date|None)` 只给测试注入。"""
    if treasury_get is None:
        import treasury_yields  # noqa: PLC0415 - 与扫描的宏观 10Y 同一个入口（按月整份取、自带缓存与重试）
        treasury_get = treasury_yields.get_yield_curve
    r = treasury_get(None)
    if not r or _num(r.get("y10")) is None:
        # 月初本月还没有官方值（1 号落在周末、或第一个交易日还没发布）时本月那份是空的 ⇒ 补问一次上月最后一个工作日。
        # 只问一次：取不到时 treasury_yields 不缓存失败，逐日回找会把一次断网放大成好几次 30 秒超时。
        # 「本月」按**美东**算（v0.45.790 修）：`get_yield_curve(None)` 取的是美东当月；本机在太平洋，月末那天 21 点后美东已进下个月，
        # 按本机日期算「上月」会问到上上个月（超过 7 天 ⇒ 根本不问），每月末晚上 3 小时取不到
        ref = max(today, _et_today())
        d = ref.replace(day=1) - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        r = treasury_get(d.isoformat()) if (today - d).days <= RULES["max_yield_age_days"] else None
    if not r or _num(r.get("y10")) is None:
        return None
    return {"date": str(r.get("date"))[:10], "y10": float(r["y10"])}


def _fetch_usdc(yf_module, ensure: Callable[[], None]) -> List[dict]:
    import yf_daily
    ensure()
    hist, _ = yf_daily.history(yf_module.Ticker("USDC-USD"), source="alphabot_thesis_usdc",
                               period=f"{int(RULES['usdc_window_days']) + 2}d", interval="1h")   # 多取两天，窗口由 judge 截
    return _closes(hist, "t")


STABLES_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=false"
#: 发行方 Circle 的公开接口：各链 USDC 流通量与合计（免 key）
CIRCLE_URL = "https://api.circle.com/v1/stablecoins"


def _default_http_get(url: str) -> dict:
    import json
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "alpha-hive/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:   # noqa: S310 - 固定的公开 https 地址
        return json.load(r)


def _circle(http_get: Optional[Callable[[str], dict]]) -> Optional[dict]:
    """Circle 公布的 USDC 流通量 `{usdc, chains, as_of}`（`as_of` = 各链里最近一次更新的日期，UTC）；没有 USDC 这一项 → None。"""
    d = (http_get or _default_http_get)(CIRCLE_URL)
    for a in (d or {}).get("data") or []:
        if (a or {}).get("symbol") != "USDC":
            continue
        v = _num(a.get("totalAmount"))
        chains = [c for c in (a.get("chains") or []) if isinstance(c, dict)]
        upd = max((str(c.get("updateDate") or "") for c in chains), default="")
        return {"usdc": v, "chains": len(chains), "as_of": upd[:10] or None} if v and v > 0 else None
    return None


def _stables(http_get: Optional[Callable[[str], dict]], today: date) -> dict:
    d = (http_get or _default_http_get)(STABLES_URL)
    total, usdc, n = 0.0, None, 0
    for a in (d or {}).get("peggedAssets") or []:
        v = _num(((a or {}).get("circulating") or {}).get("peggedUSD"))
        if v is None or v <= 0:
            continue
        total += v
        n += 1
        if a.get("symbol") == "USDC":
            usdc = v
    return {"usdc": usdc, "total": total, "n_assets": n, "as_of": today.isoformat()}


def fetch_part(part: str, ticker: str, *, today: date, yf_module=None, http_get=None,
               treasury_get=None) -> Tuple[object, dict]:
    """`(数据, {reason, retry_after_sec?, detail?})`；`reason` 为 None = 取到了。永不抛。
    `yf_module` / `http_get` / `treasury_get` 只给测试注入（注入 `yf_module` 时不装限流闸）。"""
    gated: Callable = lambda fn, what: fn()   # noqa: E731
    ensure: Callable[[], None] = lambda: None  # noqa: E731
    try:
        if part == "@stables":
            return _stables(http_get, today), {"reason": None}
        if part in ("@circle", "@ust10"):
            got = _circle(http_get) if part == "@circle" else _fetch_ust10(treasury_get, today)
            return (got, {"reason": None}) if got else (None, {"reason": "empty"})
        if yf_module is None:
            import yfinance as yf_module  # noqa: PLC0415
            try:
                import yf_gate  # noqa: PLC0415
                gated, ensure = yf_gate.gated_call, yf_gate.ensure
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断（与 price_ohlc / beta_long 同）
                pass
        if part == "@usdc":
            rows = _fetch_usdc(yf_module, ensure)
        else:
            rows = _part(yf_module, gated, part, ticker)
        # 空表不当「取到了」：yfinance 被限流时有的成员不抛、只给空表——当成功缓存 6 小时，页面就一直「数据不够」
        return (rows, {"reason": None}) if rows else (None, {"reason": "empty"})
    except Exception as exc:  # noqa: BLE001 - 取不到只是这几条判不了，页面照实写
        if BL._rate_limited(exc):
            return None, {"reason": "rate_limited", "retry_after_sec": BL._retry_after()}
        return None, {"reason": "fetch_error", "detail": type(exc).__name__}


def public_rules() -> dict:
    return {"reasons": dict(REASONS), "status": dict(STATUS_ZH), "max_quarter_age_days": RULES["max_quarter_age_days"],
            "sources": {k: v[1] for k, v in NEEDS.items()}}
