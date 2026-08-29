"""
🐝 Alpha Hive - 经济日历模块
硬编码美国重大宏观经济事件（FOMC / CPI / NFP / GDP）

数据来源（**逐条抄自官方已发布日程**）：
- FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- CPI:  https://www.bls.gov/schedule/news_release/cpi.htm
- NFP:  https://www.bls.gov/schedule/news_release/empsit.htm
- GDP:  https://www.bea.gov/news/schedule

无需 API、无网络请求、纯日期计算。

⚠️ 两条硬规矩（v0.45.65 立，因为两条都被破过）
────────────────────────────────────────────────
**一、禁止按规律推算日期。**
「CPI 在每月第二周」「非农在每月第一个周五」是**近似**，不是日程。官方每年都会
偏离若干次。v0.45.65 拿官方日程逐条核对，发现旧表 2026 年有 9 个日期是错的
（CPI 6 个、NFP 2 个、GDP 1 个），错的位置**恰好全是官方偏离规律的那几个月**——
说明旧表是按规律推出来的，却挂着官方 URL 当来源。最狠的一处：2026-11 的 CPI
被推成 11-17，官方是 11-10，**差 7 天**。
官方没发布的年份，就在 `_TABLE_SPECS[...]["pending"]` 里标「待验证」，
**空着比填错好**——空着会被下面的地平线告警吼出来，填错不会。

**二、本模块是会过期的硬编码资产，必须自己喊过期。**
表被日历走完时 `get_upcoming_events()` 返回 `[]`，而「未来 30 天真的没有宏观事件」
也返回 `[]`——两者同形，这正是本项目反复踩的静默降级。所以 `get_calendar_health()`
逐表算覆盖地平线，低于阈值 WARNING、走完 ERROR，并由
`tests/test_economic_calendar.py::TestCoverageHorizon` 在**真实 today**（不注入
ref_date）上守着，把「以后某天悄悄变哑」提前变成「今天单测变红」。
"""

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

_log = logging.getLogger("alpha_hive.economic_calendar")

# ── FOMC 利率决议（声明发布日 = 两日会议的第二天）──
# 核对时间：2026-08-29，对照 federalreserve.gov FOMC calendars（页面 Last Update 2026-08-19）
_FOMC = [
    # 2025（已核对，与官方一致；2025-08-22 的 notation vote 非例会，不计入）
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026（已核对，与官方一致）
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    # 2027（v0.45.65 新增；官方 2027 全年八次已发布）
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
    # 2028（官方目前只发布了一次：脚注「A two-day meeting is scheduled for
    # January 25-26, 2028」；其余 2028 会期未发布，不推算）
    "2028-01-26",
]

# ── CPI 数据发布（BLS Consumer Price Index，08:30 ET）──
# 核对时间：2026-08-29，逐条对照 bls.gov/schedule/news_release/cpi.htm
_CPI = [
    # 2025：⚠️ 未与官方逐条核对（BLS 当前页只回溯到 2025-11 参考月）。
    # 全部是过去日期，get_upcoming_events 不会返回，仅留档；
    # 若将来需要历史回放，先去 BLS「Prior Years」核对再用。
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10",
    "2025-05-13", "2025-06-11", "2025-07-11", "2025-08-12",
    "2025-09-10", "2025-10-14", "2025-11-12", "2025-12-10",
    # 2026：已逐条核对。以下 6 个由 v0.45.65 修正（旧值 → 官方值）：
    #   01-14→01-13  04-14→04-10  09-15→09-11  10-13→10-14  11-17→11-10  12-09→12-10
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
    # 2027：BLS 尚未发布 → 待验证，见 _TABLE_SPECS["cpi"]["pending"]。禁止推算填空。
]

# ── 非农就业 NFP（BLS Employment Situation，08:30 ET）──
# 核对时间：2026-08-29，逐条对照 bls.gov/schedule/news_release/empsit.htm
_NFP = [
    # 2025：⚠️ 同 CPI，未与官方逐条核对，全部为过去日期，仅留档。
    "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04",
    "2025-05-02", "2025-06-06", "2025-07-03", "2025-08-01",
    "2025-09-05", "2025-10-03", "2025-11-07", "2025-12-05",
    # 2026：已逐条核对。以下 2 个由 v0.45.65 修正（旧值 → 官方值）：
    #   02-06→02-11  05-01→05-08   ——正是官方偏离「每月第一个周五」的两个月
    "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03",
    "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
    # 2027：BLS 尚未发布 → 待验证。禁止推算填空。
]

# ── GDP 初值 / Advance Estimate（BEA，08:30 ET）──
# 核对时间：2026-08-29，对照 bea.gov/news/schedule
_GDP = [
    # 2025：⚠️ 未与官方逐条核对，全部为过去日期，仅留档。
    "2025-01-30", "2025-04-30", "2025-07-30", "2025-10-29",
    # 2026：前三条为过去日期（未逐条核对）；
    # 第四条由 v0.45.65 修正：10-28 → 10-29（官方「GDP (Advance Estimate),
    # 3rd Quarter 2026 — October 29」）
    "2026-01-29", "2026-04-29", "2026-07-29", "2026-10-29",
    # 2027：BEA 当前日程只排到 2026-12-23，2027 未发布 → 待验证。禁止推算填空。
]

# 事件元数据
_EVENT_META = {
    "fomc":  {"event": "FOMC 利率决议",  "type": "fomc",  "severity": "high"},
    "cpi":   {"event": "CPI 通胀数据",   "type": "cpi",   "severity": "high"},
    "nfp":   {"event": "非农就业报告",   "type": "nfp",   "severity": "high"},
    "gdp":   {"event": "GDP 初值",       "type": "gdp",   "severity": "medium"},
}

# ── 覆盖地平线阈值（天）──
#
# 阈值**逐表设定**，因为它衡量的是「该来源提前多久发布日程」，不是「我们想要多少」。
# 一刀切 90 天会让 GDP 永久变红（BEA 根本不提前 90 天排下一次初值），
# 而永久变红的告警等于没有告警——很快就会被人关掉。
_MIN_HORIZON_DAYS = {
    # 美联储提前 1.5~2.5 年发布全年会期。地平线掉到半年以内，只可能是
    # 「官方早就发了、我们没抄」，不可能是官方没发。
    "fomc": 180,
    # BLS 提前约一年发布，每年秋季刷新下一年。留 90 天 = 发现后有整整一个季度
    # 去核对补录。
    "cpi": 90,
    "nfp": 90,
    # BEA 只维护约 4 个月的滚动日程，而 GDP 初值是季度频率（间隔 ~91 天），
    # 所以「到下一次初值的天数」天然在 0~91 之间摆动。30 天是「下一个季度确实
    # 还没进日程」的水位，再高就是在惩罚 BEA 的正常节奏。
    "gdp": 30,
}
_DEFAULT_MIN_HORIZON_DAYS = 90

# ── 各表的来源与已核对边界 ──
# `verified_through`：官方已发布且已逐条核对到这一天。往表里加超过它的日期时，
# **必须同时上移这个值**——`test_no_dates_beyond_verified_through` 会逼你回到源站看一眼，
# 这正是为了防止「按规律推算」重演。
_TABLE_SPECS: Dict[str, Dict[str, Any]] = {
    "fomc": {
        "dates": _FOMC,
        "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "verified_through": "2028-01-26",
        "pending": "2028 年 1 月之后的会期官方未发布（每个日期在前一次会议确认前均为 tentative）→ 待验证",
    },
    "cpi": {
        "dates": _CPI,
        "source": "https://www.bls.gov/schedule/news_release/cpi.htm",
        "verified_through": "2026-12-10",
        "pending": "2027 全年日程 BLS 尚未发布（2026-08-29 核对，站点只到 2026-12）→ 待验证",
    },
    "nfp": {
        "dates": _NFP,
        "source": "https://www.bls.gov/schedule/news_release/empsit.htm",
        "verified_through": "2026-12-04",
        "pending": "2027 全年日程 BLS 尚未发布（2026-08-29 核对，站点只到 2026-12）→ 待验证",
    },
    "gdp": {
        "dates": _GDP,
        "source": "https://www.bea.gov/news/schedule",
        "verified_through": "2026-10-29",
        "pending": "BEA 滚动日程当前只到 2026-12-23，2027 初值日期未发布 → 待验证",
    },
}

# 告警节流：同一状态在一个进程里只吼一次（日报一次扫 30 只标的，
# GuardBee 每只都会调一次日历，不节流会刷 30 条同样的 WARNING）。
_WARNED_KEYS: set = set()


def _reset_warning_throttle() -> None:
    """清空告警节流状态（仅供测试使用）"""
    _WARNED_KEYS.clear()


def _parse_dates(raw: List[str]) -> List[date]:
    out = []
    for ds in raw:
        try:
            out.append(date.fromisoformat(ds))
        except (ValueError, TypeError):
            _log.warning("经济日历中存在无法解析的日期，已跳过: %r", ds)
    return out


def get_calendar_health(ref_date: Optional[date] = None) -> Dict[str, Any]:
    """
    体检硬编码日历的覆盖情况 —— **逐表**算，取最差的那张。

    为什么必须逐表：FOMC 已经排到 2028，CPI/NFP/GDP 只到 2026-12。
    若用 max(全部日期) 算总地平线，会得到「还有 500 天覆盖」的健康读数，
    而四条数据里有三条早已断供——聚合掩盖分量，正是本项目要治的那类静默降级。

    Returns:
        {
          ok: bool,                     # 全部表都在各自阈值之上
          status: "ok" | "stale" | "exhausted",
          horizon_days: int,            # 最短表的原始地平线（可为负）
          last_date: str | None,        # 最短表的最后一个日期
          binding_table: str,           # 余量最小、最先报警的那张表
          binding_margin_days: int,     # 该表距离自己阈值还剩几天
          stale_tables: [str], exhausted_tables: [str],
          per_table: {key: {...}},
          checked_at: "YYYY-MM-DD",
        }
    """
    today = ref_date or date.today()

    per_table: Dict[str, Dict[str, Any]] = {}
    for key, spec in _TABLE_SPECS.items():
        parsed = _parse_dates(spec["dates"])
        last = max(parsed) if parsed else None
        remaining = [d for d in parsed if d >= today]
        horizon = (last - today).days if last else -9999
        threshold = _MIN_HORIZON_DAYS.get(key, _DEFAULT_MIN_HORIZON_DAYS)
        per_table[key] = {
            "last_date": last.isoformat() if last else None,
            "horizon_days": horizon,
            "min_horizon_days": threshold,
            "margin_days": horizon - threshold,
            "remaining": len(remaining),
            "exhausted": not remaining,
            "stale": horizon < threshold,
            "source": spec["source"],
            "verified_through": spec["verified_through"],
            "pending": spec["pending"],
        }

    exhausted = sorted(k for k, v in per_table.items() if v["exhausted"])
    stale = sorted(k for k, v in per_table.items() if v["stale"] and not v["exhausted"])

    # 先报警的是「余量最小」的表，不是「地平线最短」的表——阈值不同，可比的是余量
    binding = min(per_table.items(), key=lambda kv: kv[1]["margin_days"])[0]
    shortest = min(per_table.items(), key=lambda kv: kv[1]["horizon_days"])[0]

    if exhausted:
        status = "exhausted"
    elif stale:
        status = "stale"
    else:
        status = "ok"

    return {
        "ok": status == "ok",
        "status": status,
        "horizon_days": per_table[shortest]["horizon_days"],
        "last_date": per_table[shortest]["last_date"],
        "shortest_table": shortest,
        "binding_table": binding,
        "binding_margin_days": per_table[binding]["margin_days"],
        "stale_tables": stale,
        "exhausted_tables": exhausted,
        "per_table": per_table,
        "checked_at": today.isoformat(),
    }


def _emit_health_warning(today: date) -> Dict[str, Any]:
    """按体检结果吼一嗓子（同状态一个进程只吼一次）"""
    health = get_calendar_health(ref_date=today)
    if health["ok"]:
        return health

    key = (health["status"], health["binding_table"], health["last_date"])
    first_time = key not in _WARNED_KEYS
    _WARNED_KEYS.add(key)

    if health["status"] == "exhausted":
        names = "/".join(health["exhausted_tables"])
        msg = ("🚨 经济日历已被日期走完：%s 表在 %s 之后没有任何条目，"
               "宏观催化剂信号已失效（GuardBee 的 FOMC 临近票、dashboard 倒计时都会静默消失）。"
               "补录办法见 economic_calendar 模块 docstring；各表来源：%s")
        args = (names, health["last_date"],
                "; ".join(f"{k}<-{v['source']}" for k, v in health["per_table"].items()
                          if v["exhausted"]))
        (_log.error if first_time else _log.debug)(msg, *args)
    else:
        b = health["per_table"][health["binding_table"]]
        msg = ("⚠️ 经济日历即将过期：%s 表只覆盖到 %s（还剩 %d 天，阈值 %d 天）。"
               "待验证事项：%s。请去 %s 抄取新日程，禁止按规律推算。")
        args = (health["binding_table"], b["last_date"], b["horizon_days"],
                b["min_horizon_days"], b["pending"], b["source"])
        (_log.warning if first_time else _log.debug)(msg, *args)

    return health


def get_upcoming_events(days: int = 30, ref_date: Optional[date] = None) -> List[Dict]:
    """
    返回未来 N 天内的宏观事件列表

    格式复用 ChronosBeeHorizon 催化剂结构：
    [{event: str, date: str, days_until: int, type: str, severity: str}]

    Args:
        days: 前瞻天数（默认 30）
        ref_date: 参考日期（默认 today，测试用可注入）

    Note:
        体检**不以「返回值是否为空」为条件**。空列表有两种成因：
        ①未来 30 天真的没有宏观事件（正常，安静）；②表被走完了（故障，要吼）。
        只在 ② 成立时告警，靠的是 `get_calendar_health()` 看表尾日期，
        而不是看这次调用返回了几条——把这两件事混为一谈，正是静默降级的入口。
    """
    today = ref_date or date.today()
    cutoff = today + timedelta(days=days)

    events = []
    for key, dates_list in [("fomc", _FOMC), ("cpi", _CPI),
                            ("nfp", _NFP), ("gdp", _GDP)]:
        meta = _EVENT_META[key]
        for d in _parse_dates(dates_list):
            if today <= d <= cutoff:
                events.append({
                    "event": meta["event"],
                    "date": d.isoformat(),
                    "days_until": (d - today).days,
                    "type": meta["type"],
                    "severity": meta["severity"],
                })
    events.sort(key=lambda e: e["days_until"])

    _emit_health_warning(today)
    return events


def get_next_event(ref_date: Optional[date] = None) -> Optional[Dict]:
    """返回最近一个宏观事件（用于 Hero 倒计时）

    返回 None 有两种含义（未来 60 天无事件 / 日历已过期），
    调用方若要区分，读 `get_calendar_health()`。
    """
    upcoming = get_upcoming_events(days=60, ref_date=ref_date)
    return upcoming[0] if upcoming else None
