"""T+N 交易日算术只有一份实现，而且是 NYSE 日历（v0.45.520）。

**背景。** `outcomes_fetcher` / `backtester` / `backfill_dir_accuracy` 此前用 pandas
`CustomBusinessDay(USFederalHolidayCalendar)`——那是联邦假日表：哥伦布日、退伍军人日 NYSE 开市却被判休，
耶稣受难日 NYSE 休市却被判开（2025–2027 共 10 天相反）。取价取的是「联邦目标日当天或之后第一根 bar」，
于是窗口跨这些日子的 T+1/T+7/T+30 标签差一个交易日（受难日那批早一天、哥伦布那批晚一天）。
生产库实测已回填 413 行受影响、存值 413/413 等于错位那天的收盘，按 NYSE 重算方向判定翻转 48 行。
拨钟 CI（`tools/clockshift`）10-14 的 +7 档把它当日期炸弹抓到：`test_outcomes_backlog_starvation` 只在
「今天」= 2026-10-21 红（夹具两天恰跨 10-12 哥伦布日）。

**纪律。** 每条断言的 docstring 写明什么变异会让它变红。离线、零外部依赖。
"""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

import backtester as B
import outcomes_fetcher as OF
from is_trading_day import add_trading_days, is_trading_day

_ROOT = Path(__file__).resolve().parent.parent


class TestAddTradingDays:

    @pytest.mark.parametrize("start, n, want", [
        (date(2026, 10, 9), 1, date(2026, 10, 12)),    # 哥伦布日 NYSE 开市（联邦表给 10-13）
        (date(2026, 10, 13), -1, date(2026, 10, 12)),
        (date(2026, 11, 10), 1, date(2026, 11, 11)),   # 退伍军人日 NYSE 开市
        (date(2026, 4, 2), 1, date(2026, 4, 6)),       # 耶稣受难日 NYSE 休市（联邦表给 04-03）
        (date(2026, 3, 26), 7, date(2026, 4, 7)),      # 跨受难日的 T+7（联邦表给 04-06 = 实为 T+6）
        (date(2027, 12, 30), 1, date(2027, 12, 31)),   # 2028 元旦落周六，NYSE 不提前到 12-31 休市
        (date(2026, 10, 10), 1, date(2026, 10, 12)),   # 周六起算：次一交易日记 1
        (date(2026, 10, 10), -1, date(2026, 10, 9)),
        (date(2026, 10, 10), 0, date(2026, 10, 12)),   # n=0：休市顺延（同 pandas rollforward）
        (date(2026, 10, 12), 0, date(2026, 10, 12)),
        (datetime(2026, 10, 9, 13, 30), 1, date(2026, 10, 12)),
    ])
    def test_known_cases(self, start, n, want):
        """变红的变异：退回联邦假日表；或步进时把起始日本身计入。"""
        assert add_trading_days(start, n) == want

    def test_matches_pandas_except_where_the_calendars_disagree(self):
        """2025-02 ~ 2027-11 每天 × n∈{±1, ±7, 30}：与 pandas 联邦版**只**在窗口跨过两表相反的那几天时不同——
        即除了日历本身，语义（起算方式、正负方向、周末起算）与改之前完全一样。
        变红的变异：改步进语义（如从 d 当天起算）；或 `is_trading_day` 的假日表变了却没人知道。"""
        fed = CustomBusinessDay(calendar=USFederalHolidayCalendar())
        fed_closed = set(USFederalHolidayCalendar().holidays("2024-12-01", "2028-02-01").date)
        disagree = {d for d in (date(2025, 1, 1) + timedelta(i) for i in range(1100))
                    if d.weekday() < 5 and is_trading_day(d)[0] == (d in fed_closed)}
        assert len(disagree) == 10, sorted(disagree)
        unexplained, explained = [], 0
        d = date(2025, 2, 1)
        while d < date(2027, 11, 1):
            for n in (1, 7, 30, -1, -7):
                a, b = add_trading_days(d, n), (pd.Timestamp(d) + n * fed).date()
                if a == b:
                    continue
                lo, hi = (d, max(a, b)) if n > 0 else (min(a, b), d)
                if any(lo <= x <= hi for x in disagree):
                    explained += 1
                else:
                    unexplained.append((d, n, a, b))
            d += timedelta(days=1)
        assert unexplained == []
        assert explained > 0, "对照失效：一处分歧都没有，说明两边算的是同一个日历"


class TestCallSitesUseNyse:

    def test_outcomes_fetcher_target_window(self, tmp_path):
        """报告快照 actual_prices 的目标日。变红的变异：`_target_window` 退回联邦假日表（给 10-13）。"""
        f = OF.OutcomesFetcher(str(tmp_path))
        target, end = f._target_window("NVDA", "2026-10-09", 1)
        assert target == date(2026, 10, 12) and end == date(2026, 10, 22)
        assert f._target_window("NVDA", "2026-04-02", 1)[0] == date(2026, 4, 6)

    def test_backtester_price_lookup_starts_at_nyse_target(self, monkeypatch):
        """predictions.price_t1 / close_t7 / price_t30 / SPY 基准的取价起点。
        变红的变异：`_get_price_at_date` 退回联邦假日表（起点 10-13）。"""
        import data_pipeline
        seen = []
        bt = B.Backtester.__new__(B.Backtester)
        monkeypatch.setattr(B, "yf", object())
        monkeypatch.setattr(data_pipeline, "_exchange_now", lambda: None)

        def _history(ticker, start, end):
            seen.append(start)
            return pd.DataFrame({"Close": [101.0]}, index=pd.DatetimeIndex([start]))
        monkeypatch.setattr(bt, "_history", _history)
        assert bt._get_price_at_date("NVDA", "2026-10-09", 1) == 101.0
        assert bt._get_price_at_date("NVDA", "2026-03-26", 7) == 101.0
        assert seen == ["2026-10-12", "2026-04-07"]


def test_no_other_trading_day_arithmetic():
    """全仓（测试除外）不许再出现 pandas 的联邦假日表 / CustomBusinessDay——T+N 只经 `add_trading_days`。
    变红的变异：任一生产模块重新 `from pandas.tseries.holiday import USFederalHolidayCalendar`（或用
    `CustomBusinessDay` 自建日历）。按 AST 认 import 与名字，不认注释 / 字符串里的提及。"""
    banned = {"USFederalHolidayCalendar", "CustomBusinessDay", "AbstractHolidayCalendar"}
    hits = []
    for p in sorted(_ROOT.rglob("*.py")):
        rel = p.relative_to(_ROOT)
        if rel.parts[0] in {"tests", ".claude", ".git", "node_modules"} or "site-packages" in rel.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            hits += [f"{rel}:{node.lineno} {n}" for n in names if n in banned]
    assert hits == [], hits
