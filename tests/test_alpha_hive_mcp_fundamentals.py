"""v0.45.709 alpha_hive MCP 的四个基本面类工具（财报 / 分析师 / 持仓 / 分红拆股）+ `yf_gate.gated_call`。

全部离线：假 `yf.Ticker`（monkeypatch 到 `alpha_hive_mcp.yf`），读钟只有 `alpha_hive_mcp._today`，钉住。
管四件事：
1. 形状与取值：最新期在前、条数上限、NaN / NaT → None、全空行丢掉并计数、窗口按钉住的今天算；
2. 「Yahoo 给了空」不许说成「没有」：返回 None + `unavailable` 里写明分不清；
3. 限流：429 ⇒ `rate_limited` + `retry_after_sec`，**且每个工具都真的走闸门**——冷却中再调，假 Ticker 一次都不被碰；
4. 接线：四个工具已注册、模块 docstring 列了它们、真实入口先装闸门再 `mcp.run()`。
"""
from __future__ import annotations

import ast
import asyncio
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import alpha_hive_mcp as m
import yf_gate

TODAY = date(2026, 11, 3)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """闸门的冷却与计数是模块级的：一条测试造的 429 不许让同会话里别的测试（扫描路径）进冷却。"""
    monkeypatch.setattr(yf_gate, "_cooldown_until", 0.0)
    monkeypatch.setattr(yf_gate, "_stats", dict.fromkeys(yf_gate._stats, 0))
    monkeypatch.setattr(yf_gate, "_last_ok_at", 0.0)
    monkeypatch.setattr(m, "_today", lambda: TODAY)


class YFRateLimitError(Exception):
    """与 yfinance 同名——`yf_gate.is_rate_limit_error` 按类名认。"""


class FakeTicker:
    """属性按名字从 `data` 取；值是异常就抛；每次访问记进 `seen`（证明「冷却中一次都没碰」）。"""

    data: dict = {}
    seen: list = []

    def __init__(self, ticker):
        self.ticker = ticker

    def __getattr__(self, name):
        if name not in type(self).data:
            raise AttributeError(name)
        type(self).seen.append(name)
        v = type(self).data[name]
        if isinstance(v, BaseException):
            raise v
        return v


@pytest.fixture
def fake(monkeypatch):
    FakeTicker.data, FakeTicker.seen = {}, []
    monkeypatch.setattr(m.yf, "Ticker", FakeTicker)
    return FakeTicker


def _strict_json(s: str) -> dict:
    """NaN / Infinity 不是合法 JSON：Claude 端解析会炸，这里先炸。"""
    def _bad(c):
        raise AssertionError(f"非法 JSON 常量 {c}")
    return json.loads(s, parse_constant=_bad)


def _run(fn, model):
    return _strict_json(asyncio.run(fn(model)))


def _income(n_periods=5):
    cols = [pd.Timestamp(f"{2025 - i}-09-30") for i in range(n_periods)]
    cols = cols[::-1]                                      # 故意乱序：输出必须自己排成最新在前
    return pd.DataFrame(
        {c: [100.0 + i, np.nan, np.float64(7.5)] for i, c in enumerate(cols)},
        index=["Total Revenue", "Always Empty", "EPS"],
    )


# ═════════════════════════════════════════════════════════ 财报

class TestFinancials:
    def test_newest_first_capped_and_json_native(self, fake):
        fake.data = {"income_stmt": _income(5)}
        out = _run(m.alphahive_get_financials, m.FinancialsInput(ticker="nvda", max_periods=3))
        assert out["ticker"] == "NVDA"
        assert out["periods"] == ["2025-09-30", "2024-09-30", "2023-09-30"]
        assert out["items"]["Total Revenue"] == [104.0, 103.0, 102.0]
        assert out["items"]["EPS"] == [7.5, 7.5, 7.5]

    def test_all_empty_line_items_are_dropped_and_counted(self, fake):
        fake.data = {"income_stmt": _income(2)}
        out = _run(m.alphahive_get_financials, m.FinancialsInput(ticker="NVDA"))
        assert "Always Empty" not in out["items"] and out["empty_items_dropped"] == 1

    def test_partly_missing_values_stay_as_none(self, fake):
        df = _income(2)
        df.iloc[0, 0] = np.nan
        fake.data = {"income_stmt": df}
        out = _run(m.alphahive_get_financials, m.FinancialsInput(ticker="NVDA"))
        assert out["items"]["Total Revenue"] == [101.0, None]

    @pytest.mark.parametrize("statement, frequency, attr", [
        ("income", "annual", "income_stmt"), ("balance", "quarterly", "quarterly_balance_sheet"),
        ("cashflow", "annual", "cashflow"), ("cashflow", "quarterly", "quarterly_cashflow"),
    ])
    def test_reads_the_right_yahoo_attribute(self, fake, statement, frequency, attr):
        fake.data = {attr: _income(2)}
        out = _run(m.alphahive_get_financials,
                   m.FinancialsInput(ticker="NVDA", statement=statement, frequency=frequency))
        assert fake.seen == [attr] and out["statement"] == statement and out["frequency"] == frequency

    def test_empty_is_an_error_that_says_it_cannot_tell_none_from_failure(self, fake):
        fake.data = {"income_stmt": pd.DataFrame()}
        out = _run(m.alphahive_get_financials, m.FinancialsInput(ticker="NVDA"))
        assert "error" in out and "not confirmed" in out["error"]

    def test_bad_statement_is_rejected_by_the_model(self):
        with pytest.raises(Exception):
            m.FinancialsInput(ticker="NVDA", statement="ebitda")


# ═════════════════════════════════════════════════════════ 分析师

def _upgrades(dates):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="GradeDate")
    return pd.DataFrame({
        "Firm": [f"F{i}" for i in range(len(dates))], "ToGrade": "Buy", "FromGrade": "Hold", "Action": "up",
        "priceTargetAction": "Raises", "currentPriceTarget": np.float64(200.0), "priorPriceTarget": np.nan,
    }, index=idx)


class TestAnalystRatings:
    def test_window_is_counted_from_the_pinned_today_newest_first(self, fake):
        fake.data = {
            "recommendations": pd.DataFrame({"period": ["0m", "-1m"], "strongBuy": [5, 4], "buy": [10, 9],
                                             "hold": [3, 3], "sell": [0, 1], "strongSell": [0, 0]}),
            "upgrades_downgrades": _upgrades(["2026-08-01", "2026-10-30", "2026-09-10", "2026-08-05"]),
        }
        out = _run(m.alphahive_get_analyst_ratings, m.AnalystInput(ticker="NVDA", days_back=90))
        # 11-03 往前 90 天 = 08-05（含）
        assert [c["date"] for c in out["changes"]] == ["2026-10-30", "2026-09-10", "2026-08-05"]
        assert out["changes_in_window"] == 3
        first = out["changes"][0]
        assert first["firm"] == "F1" and first["to_grade"] == "Buy" and first["price_target"] == 200.0
        assert first["prior_price_target"] is None
        assert out["rating_counts"][0] == {"period": "0m", "strongBuy": 5, "buy": 10, "hold": 3,
                                           "sell": 0, "strongSell": 0}
        assert out["unavailable"] == {}

    def test_changes_are_capped_at_30_but_the_count_is_not(self, fake):
        fake.data = {"recommendations": pd.DataFrame(),
                     "upgrades_downgrades": _upgrades([f"2026-10-{d:02d}" for d in range(1, 31)] + ["2026-11-01"] * 5)}
        out = _run(m.alphahive_get_analyst_ratings, m.AnalystInput(ticker="NVDA"))
        assert len(out["changes"]) == 30 and out["changes_in_window"] == 35

    def test_empty_sections_are_none_with_a_reason_not_empty_lists(self, fake):
        fake.data = {"recommendations": pd.DataFrame(), "upgrades_downgrades": pd.DataFrame()}
        out = _run(m.alphahive_get_analyst_ratings, m.AnalystInput(ticker="NVDA"))
        assert out["rating_counts"] is None and out["changes"] is None
        assert set(out["unavailable"]) == {"rating_counts", "changes"}


# ═════════════════════════════════════════════════════════ 持仓

class TestHolders:
    def test_breakdown_and_top_ten(self, fake):
        inst = pd.DataFrame({"Date Reported": [pd.Timestamp("2026-06-30")] * 12, "Holder": [f"H{i}" for i in range(12)],
                             "pctHeld": np.linspace(0.09, 0.01, 12), "Shares": np.arange(12, dtype="int64"),
                             "Value": np.nan})
        fake.data = {
            "major_holders": pd.DataFrame({"Value": [0.04, 0.68, 5321.0]},
                                          index=pd.Index(["insidersPercentHeld", "institutionsPercentHeld",
                                                          "institutionsCount"], name="Breakdown")),
            "institutional_holders": inst, "mutualfund_holders": pd.DataFrame(),
        }
        out = _run(m.alphahive_get_holders, m.TickerInput(ticker="NVDA"))
        assert out["breakdown"] == {"insidersPercentHeld": 0.04, "institutionsPercentHeld": 0.68,
                                    "institutionsCount": 5321.0}
        assert len(out["top_institutions"]) == 10
        row = out["top_institutions"][0]
        assert row["Holder"] == "H0" and row["Date Reported"] == "2026-06-30" and row["Value"] is None
        assert isinstance(row["Shares"], int)
        assert out["top_funds"] is None and "top_funds" in out["unavailable"]

    def test_everything_empty(self, fake):
        fake.data = {"major_holders": pd.DataFrame(), "institutional_holders": None,
                     "mutualfund_holders": pd.DataFrame()}
        out = _run(m.alphahive_get_holders, m.TickerInput(ticker="NVDA"))
        assert set(out["unavailable"]) == {"breakdown", "top_institutions", "top_funds"}


# ═════════════════════════════════════════════════════════ 分红拆股

def _series(pairs, tz="America/New_York"):
    return pd.Series([v for _, v in pairs],
                     index=pd.DatetimeIndex([pd.Timestamp(d) for d, _ in pairs]).tz_localize(tz))


class TestCorporateActions:
    def test_dividend_window_and_full_split_history(self, fake):
        fake.data = {
            "dividends": _series([("2020-03-01", 0.04), ("2024-06-10", 0.01), ("2026-09-11", np.float64(0.01))]),
            "splits": _series([("2021-07-20", 4.0), ("2024-06-10", 10.0)]),
        }
        out = _run(m.alphahive_get_corporate_actions, m.CorporateActionsInput(ticker="NVDA", years=5))
        assert out["dividends"] == [{"date": "2026-09-11", "amount": 0.01}, {"date": "2024-06-10", "amount": 0.01}]
        assert out["splits"] == [{"date": "2024-06-10", "ratio": 10.0}, {"date": "2021-07-20", "ratio": 4.0}]
        assert out["unavailable"] == {}

    def test_empty_series_is_none_not_never_paid(self, fake):
        """`paper_portfolio` 记过：Yahoo 取数失败时 `splits` 也是空 Series。空 ≠ 从没分红 / 拆股。"""
        fake.data = {"dividends": pd.Series(dtype=float), "splits": pd.Series(dtype=float)}
        out = _run(m.alphahive_get_corporate_actions, m.CorporateActionsInput(ticker="NVDA"))
        assert out["dividends"] is None and out["splits"] is None
        assert "not confirmed" in out["unavailable"]["dividends"]


# ═════════════════════════════════════════════════════════ 限流与闸门

_CALLS = {
    "financials": (m.alphahive_get_financials, lambda: m.FinancialsInput(ticker="NVDA"), "income_stmt"),
    "analyst": (m.alphahive_get_analyst_ratings, lambda: m.AnalystInput(ticker="NVDA"), "recommendations"),
    "holders": (m.alphahive_get_holders, lambda: m.TickerInput(ticker="NVDA"), "major_holders"),
    "actions": (m.alphahive_get_corporate_actions, lambda: m.CorporateActionsInput(ticker="NVDA"), "dividends"),
}


class TestRateLimit:
    @pytest.mark.parametrize("key", sorted(_CALLS))
    def test_429_is_reported_as_rate_limited_with_retry_time(self, fake, key):
        fn, model, attr = _CALLS[key]
        fake.data = {attr: YFRateLimitError("Too Many Requests. Rate limited. Try after a while.")}
        out = _run(fn, model())
        assert out["rate_limited"] is True and out["retry_after_sec"] >= 60
        assert yf_gate._stats["rate_limited"] == 1, "429 没进闸门的冷却——下一次调用会接着打 Yahoo"

    @pytest.mark.parametrize("key", sorted(_CALLS))
    def test_every_tool_goes_through_the_gate(self, fake, key):
        """冷却中再调：假 Ticker 一次都不许被碰。绕过闸门（直接读属性）的工具在这里必红——
        只看返回值证明不了，`_yf_err` 按异常文本也能认出 429。"""
        fake.data = {"income_stmt": YFRateLimitError("Too Many Requests")}
        _run(m.alphahive_get_financials, m.FinancialsInput(ticker="NVDA"))          # 进冷却
        fake.seen.clear()
        fn, model, _attr = _CALLS[key]
        out = _run(fn, model())
        assert out["rate_limited"] is True
        assert fake.seen == [], f"冷却中 {key} 仍读了 Yahoo：{fake.seen}"

    def test_other_errors_are_not_called_rate_limits(self, fake):
        fake.data = {"income_stmt": KeyError("quoteSummary")}
        out = _run(m.alphahive_get_financials, m.FinancialsInput(ticker="NVDA"))
        assert "rate_limited" not in out and out["error"].startswith("KeyError")


class TestGatedCall:
    def test_429_enters_cooldown_and_later_calls_are_blocked(self):
        with pytest.raises(yf_gate.YFRateLimited):
            yf_gate.gated_call(lambda: (_ for _ in ()).throw(YFRateLimitError("Too Many Requests")), "t")
        touched = []
        with pytest.raises(yf_gate.YFRateLimited):
            yf_gate.gated_call(lambda: touched.append(1), "t")
        assert touched == [] and yf_gate.retry_after_sec() > 0

    def test_success_passes_the_value_through_without_installing_the_gate(self):
        before = yf_gate.is_installed()
        assert yf_gate.gated_call(lambda: 42, "t") == 42
        assert yf_gate.is_installed() == before


# ═════════════════════════════════════════════════════════ 接线

_NEW = {"alphahive_get_financials", "alphahive_get_analyst_ratings", "alphahive_get_holders",
        "alphahive_get_corporate_actions"}


class TestWiring:
    def test_tools_are_registered(self):
        names = {t.name for t in asyncio.run(m.mcp.list_tools())}
        assert _NEW <= names

    def test_module_docstring_lists_them(self):
        assert all(n in m.__doc__ for n in _NEW)

    def test_entry_point_installs_the_gate_before_running(self):
        """真实入口（`__main__`）先 `yf_gate.install()` 再 `mcp.run()`；import 本模块不装（不给测试进程打补丁）。"""
        tree = ast.parse(Path(m.__file__).read_text(encoding="utf-8"))
        main = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
        assert len(main) == 1
        calls = [ast.unparse(c.func) for c in ast.walk(main[0]) if isinstance(c, ast.Call)]
        assert "yf_gate.install" in calls and "mcp.run" in calls
        assert calls.index("yf_gate.install") < calls.index("mcp.run")
        module_level = [ast.unparse(n) for n in tree.body if not isinstance(n, (ast.If, ast.FunctionDef,
                                                                                  ast.AsyncFunctionDef, ast.ClassDef))]
        assert not any("yf_gate.install" in s for s in module_level)
