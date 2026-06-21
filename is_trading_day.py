#!/usr/bin/env python3
"""
美股交易日检测工具

检查给定日期是否为美股交易日（NYSE/NASDAQ 开盘日）。
跳过周末和所有美股官方假日。

用法:
    python3 is_trading_day.py           # 检查今天
    python3 is_trading_day.py 2026-03-07  # 检查指定日期

退出码:
    0  = 是交易日（可以运行扫描）
    10 = 非交易日（应跳过扫描）
    1  = 脚本错误（应继续运行扫描，防止误跳过）
"""

import sys
from datetime import date, timedelta


def _easter(year: int) -> date:
    """Anonymous Gregorian Easter algorithm (Meeus)"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    """
    联邦假日 'observed' 规则：
    周六 → 周五观察，周日 → 周一观察
    """
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """第 n 个星期几（weekday: 0=Mon ... 6=Sun）"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """该月最后一个星期几"""
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def us_market_holidays(year: int) -> dict[date, str]:
    """
    返回指定年份所有 NYSE/NASDAQ 休市日。
    包含 10 个联邦 / 交易所假日。

    参考: https://www.nyse.com/markets/hours-calendars
    """
    holidays = {}

    # 1. New Year's Day (Jan 1) — NYSE 特例：落周六【不】回滚到前一周五（12/31 照常开盘，
    #    史实如 2021-12-31 正常交易）；落周日才顺延到周一（1/2）；周一~周五当天休市。
    _ny = date(year, 1, 1)
    if _ny.weekday() == 6:        # 周日 → 观察到 1/2（周一）
        holidays[date(year, 1, 2)] = "元旦 New Year's Day"
    elif _ny.weekday() < 5:       # 周一~周五 → 当天休市
        holidays[_ny] = "元旦 New Year's Day"
    # 周六：NYSE 不回滚，不加休市日

    # 2. MLK Day (3rd Monday in January)
    holidays[_nth_weekday(year, 1, 0, 3)] = "马丁·路德·金纪念日 MLK Day"

    # 3. Presidents' Day (3rd Monday in February)
    holidays[_nth_weekday(year, 2, 0, 3)] = "总统日 Presidents' Day"

    # 4. Good Friday (Friday before Easter)
    easter_date = _easter(year)
    holidays[easter_date - timedelta(days=2)] = "耶稣受难日 Good Friday"

    # 5. Memorial Day (last Monday in May)
    holidays[_last_weekday(year, 5, 0)] = "阵亡将士纪念日 Memorial Day"

    # 6. Juneteenth (June 19) — NYSE 自 2022 年起休市
    holidays[_observed(date(year, 6, 19))] = "六月节 Juneteenth"

    # 7. Independence Day (July 4)
    holidays[_observed(date(year, 7, 4))] = "独立日 Independence Day"

    # 8. Labor Day (1st Monday in September)
    holidays[_nth_weekday(year, 9, 0, 1)] = "劳动节 Labor Day"

    # 9. Thanksgiving (4th Thursday in November)
    holidays[_nth_weekday(year, 11, 3, 4)] = "感恩节 Thanksgiving"

    # 10. Christmas (Dec 25)
    holidays[_observed(date(year, 12, 25))] = "圣诞节 Christmas"

    return holidays


def is_trading_day(d: date | None = None) -> tuple[bool, str]:
    """
    检查给定日期是否为美股交易日。

    返回:
        (True, "交易日") 或 (False, "原因说明")
    """
    if d is None:
        d = date.today()

    # 周末
    if d.weekday() >= 5:
        day_name = "周六" if d.weekday() == 5 else "周日"
        return False, f"{d} 是{day_name}，非交易日"

    # 假日
    holidays = us_market_holidays(d.year)
    # 元旦可能是上一年的 observed（12/31 周五），也检查前一年
    if d.month == 12 and d.day == 31:
        next_year_holidays = us_market_holidays(d.year + 1)
        holidays.update(next_year_holidays)
    if d.month == 1 and d.day <= 3:
        prev_year_holidays = us_market_holidays(d.year - 1)
        holidays.update(prev_year_holidays)

    if d in holidays:
        return False, f"{d} 是美股假日：{holidays[d]}"

    return True, f"{d} 是交易日"


def filename_is_nontrading_day(name: str) -> bool:
    """从文件名/日期串中提取 YYYY-MM-DD，判断该日是否美股非交易日（周末/假日）。

    用于部署 glob 与 dashboard 历史序列过滤时区漂移产生的非交易日「幽灵」文件
    （如周日标签的 ml-enhanced / daily 报告）。
    **fail-safe**：提取不出日期、或交易日检查抛异常 → 返回 False（视为可保留），
    宁可漏滤一个幽灵，也绝不误删/误滤任何合法交易日文件。
    """
    import re as _re
    m = _re.search(r"(\d{4}-\d{2}-\d{2})", name or "")
    if not m:
        return False
    try:
        trading, _ = is_trading_day(date.fromisoformat(m.group(1)))
        return not trading
    except Exception:
        return False


def main():
    if len(sys.argv) > 1 and sys.argv[1] not in ("-h", "--help"):
        try:
            check_date = date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"日期格式错误：{sys.argv[1]}（应为 YYYY-MM-DD）", file=sys.stderr)
            sys.exit(1)
    else:
        if len(sys.argv) > 1:
            print(__doc__)
            sys.exit(0)
        check_date = date.today()

    trading, reason = is_trading_day(check_date)
    print(reason)
    sys.exit(0 if trading else 10)


if __name__ == "__main__":
    main()
