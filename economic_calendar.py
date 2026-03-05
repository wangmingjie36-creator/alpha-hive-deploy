"""
🐝 Alpha Hive - 经济日历模块
硬编码 2025-2026 重大宏观经济事件（FOMC / CPI / NFP / GDP）

数据来源：
- FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- CPI/NFP: https://www.bls.gov/schedule/
- GDP: https://www.bea.gov/news/schedule

无需 API、无网络请求、纯日期计算。
"""

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

_log = logging.getLogger("alpha_hive.economic_calendar")

# ── FOMC 利率决议（声明发布日 = 会议第二天）──
_FOMC = [
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

# ── CPI 数据发布（通常每月第二周）──
_CPI = [
    # 2025
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10",
    "2025-05-13", "2025-06-11", "2025-07-11", "2025-08-12",
    "2025-09-10", "2025-10-14", "2025-11-12", "2025-12-10",
    # 2026
    "2026-01-14", "2026-02-13", "2026-03-11", "2026-04-14",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-15", "2026-10-13", "2026-11-17", "2026-12-09",
]

# ── 非农就业 NFP（通常每月第一个周五）──
_NFP = [
    # 2025
    "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04",
    "2025-05-02", "2025-06-06", "2025-07-03", "2025-08-01",
    "2025-09-05", "2025-10-03", "2025-11-07", "2025-12-05",
    # 2026
    "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
    "2026-05-01", "2026-06-05", "2026-07-02", "2026-08-07",
    "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
]

# ── GDP 初值（每季度末月下旬）──
_GDP = [
    # 2025
    "2025-01-30", "2025-04-30", "2025-07-30", "2025-10-29",
    # 2026
    "2026-01-29", "2026-04-29", "2026-07-29", "2026-10-28",
]

# 事件元数据
_EVENT_META = {
    "fomc":  {"event": "FOMC 利率决议",  "type": "fomc",  "severity": "high"},
    "cpi":   {"event": "CPI 通胀数据",   "type": "cpi",   "severity": "high"},
    "nfp":   {"event": "非农就业报告",   "type": "nfp",   "severity": "high"},
    "gdp":   {"event": "GDP 初值",       "type": "gdp",   "severity": "medium"},
}


def get_upcoming_events(days: int = 30, ref_date: Optional[date] = None) -> List[Dict]:
    """
    返回未来 N 天内的宏观事件列表

    格式复用 ChronosBeeHorizon 催化剂结构：
    [{event: str, date: str, days_until: int, type: str, severity: str}]

    Args:
        days: 前瞻天数（默认 30）
        ref_date: 参考日期（默认 today，测试用可注入）
    """
    today = ref_date or date.today()
    cutoff = today + timedelta(days=days)

    events = []
    for key, dates_list in [("fomc", _FOMC), ("cpi", _CPI),
                            ("nfp", _NFP), ("gdp", _GDP)]:
        meta = _EVENT_META[key]
        for ds in dates_list:
            try:
                d = date.fromisoformat(ds)
            except (ValueError, TypeError):
                continue
            if today <= d <= cutoff:
                events.append({
                    "event": meta["event"],
                    "date": ds,
                    "days_until": (d - today).days,
                    "type": meta["type"],
                    "severity": meta["severity"],
                })
    events.sort(key=lambda e: e["days_until"])
    return events


def get_next_event(ref_date: Optional[date] = None) -> Optional[Dict]:
    """返回最近一个宏观事件（用于 Hero 倒计时）"""
    upcoming = get_upcoming_events(days=60, ref_date=ref_date)
    return upcoming[0] if upcoming else None
