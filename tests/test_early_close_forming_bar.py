"""提前收盘日：「日线末根还在不在形成」按日历的收盘时刻判（v0.45.563）

事故形状（读代码确认；2026-03-10 起的归档里还没有提前收盘日，首个是 2026-11-27）：判据是「末根日期 == 交易所当日
且 `_exchange_now` < 15:59」，`_exchange_now` = SPY 最后一根分钟线的时间戳。提前收盘日（13:00 收）收盘后 SPY 末根是
**12:59** < 15:59 ⇒ 当天那根被当成还在形成——主路径 `_fetch_history_metrics` 丢掉它，动量 / 量比 / 波动率全部止于
D−1（维度 IC H1 窗口内 11-27、12-24 两天）。同一个写死的 15:59 抄在四处：`_drop_forming_bar` /
`_completed_bar_close` / `backtester._get_price_at_date` / `backfill_dir_accuracy._close_after`。

修法：唯一阈值 `data_pipeline._session_last_minute(d)` = `is_trading_day.session_close_et(d)` − 1 分钟（平日 15:59、
提前收盘日 12:59）；四处都用它。本文件：阈值本身、两个 data_pipeline 判据、主路径端到端（H1 的三个输入）、
回测 / 回填两处、以及「不许再写死」的 AST 守卫（带正对照）。
"""

import ast
import datetime as dt
import logging
import sys
from pathlib import Path

import pandas as pd
import pytest

import data_pipeline as dp
from bars_integrity import trading_days
from tests._repo_files import own_python_files

ROOT = Path(__file__).resolve().parent.parent
EARLY = dt.date(2026, 11, 27)        # 感恩节次日，13:00 收
REGULAR = dt.date(2026, 10, 16)


def _xnow(monkeypatch, d: dt.date, hh: int, mm: int):
    """交易所时钟（SPY 末根分钟线）钉在 d 的 hh:mm 美东。"""
    t = pd.Timestamp(dt.datetime.combine(d, dt.time(hh, mm)), tz="America/New_York")
    monkeypatch.setattr(dp, "_exchange_now", lambda: t)
    return t


def _frame(last_day: dt.date, n=22):
    days = trading_days(last_day - dt.timedelta(days=60), last_day)[-n:]
    assert days[-1] == last_day and len(days) == n
    closes = [100.0 + i * 0.7 + (i % 3) * 0.9 for i in range(n)]
    vols = [1_000_000.0 + (i % 5) * 150_000 for i in range(n)]
    return pd.DataFrame({"Close": closes, "Volume": vols},
                        index=pd.DatetimeIndex(days).tz_localize("America/New_York"))


# ───────────────────────────── 1. 阈值本身
class TestSessionLastMinute:
    @pytest.mark.parametrize("d, want", [
        ("2026-10-20", (15, 59)), ("2026-11-27", (12, 59)), ("2026-12-24", (12, 59)),
        ("2026-12-31", (15, 59)), ("2025-07-03", (12, 59)),   # 2025-07-04 周五 ⇒ 7/3 提前收
        ("2027-07-02", (15, 59)),                              # 2027-07-04 周日 ⇒ 不提前收
    ])
    def test_last_minute_follows_the_calendar(self, d, want):
        assert dp._session_last_minute(dt.date.fromisoformat(d)) == dt.time(*want)

    def test_calendar_unavailable_falls_back_to_regular_and_says_so(self, monkeypatch, caplog):
        monkeypatch.setitem(sys.modules, "is_trading_day", None)
        with caplog.at_level(logging.WARNING, logger="alpha_hive.data_pipeline"):
            assert dp._session_last_minute(EARLY) == dt.time(15, 59)
        assert any("交易日历不可用" in r.getMessage() for r in caplog.records)


# ───────────────────────────── 2. 两个判据
class TestDropFormingBar:
    @pytest.mark.parametrize("d, hhmm, dropped", [
        (EARLY, (12, 59), False),     # 提前收盘日收盘后（17:00 ET 那一跑，SPY 末根 12:59）：当天那根已收完
        (EARLY, (12, 58), True),      # 收盘前一分钟：还在形成
        (EARLY, (11, 0), True),
        (REGULAR, (15, 59), False),   # 平日语义不变
        (REGULAR, (15, 58), True),
        (REGULAR, (12, 59), True),    # 平日 12:59 仍在盘中——阈值不是「一律 12:59」
    ])
    def test_today_bar(self, monkeypatch, d, hhmm, dropped):
        _xnow(monkeypatch, d, *hhmm)
        hist = _frame(d)
        assert len(dp._drop_forming_bar(hist)) == len(hist) - (1 if dropped else 0)

    def test_previous_day_bar_is_never_dropped(self, monkeypatch):
        _xnow(monkeypatch, EARLY, 9, 45)
        hist = _frame(dt.date(2026, 11, 25))
        assert len(dp._drop_forming_bar(hist)) == len(hist)

    def test_calendar_unavailable_keeps_the_old_behaviour(self, monkeypatch):
        """判不了日历 ⇒ 退回改前的 15:59（提前收盘日照旧误丢——不更糟，且 WARNING 看得见）。"""
        _xnow(monkeypatch, EARLY, 12, 59)
        hist = _frame(EARLY)                                   # 夹具自己要用日历，先造好再摘掉
        monkeypatch.setitem(sys.modules, "is_trading_day", None)
        assert len(dp._drop_forming_bar(hist)) == len(hist) - 1


class TestCompletedBarClose:
    @pytest.mark.parametrize("d, hhmm, done", [
        (EARLY, (12, 59), True), (EARLY, (12, 30), False),
        (REGULAR, (15, 59), True), (REGULAR, (15, 58), False),
    ])
    def test_session_close_is_given_only_when_the_bar_is_done(self, monkeypatch, d, hhmm, done):
        _xnow(monkeypatch, d, *hhmm)
        hist = _frame(d)
        want = (d.isoformat(), float(hist["Close"].iloc[-1])) if done else None
        assert dp._completed_bar_close(hist) == want


# ───────────────────────────── 3. 主路径端到端：H1 的三个行情输入止于 D
@pytest.fixture
def yf_frame(monkeypatch):
    import yfinance
    holder = {"frame": None}

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **_k):
            return holder["frame"]

    monkeypatch.setattr(yfinance, "Ticker", _T)
    return holder


class TestMainPathOnEarlyCloseDay:
    def test_history_metrics_end_at_d_after_the_early_close(self, monkeypatch, yf_frame):
        """11-27 17:00 ET 的生产扫描（SPY 末根 12:59）：动量 / 量比 / 波动率都含 11-27 那根。
        判别：改前丢掉 11-27，三个值都是止于 11-25 的版本，与下面的期望不等。"""
        _xnow(monkeypatch, EARLY, 12, 59)
        hist = _frame(EARLY)
        yf_frame["frame"] = hist
        m = dp._fetch_history_metrics("NVDA")
        c, v = hist["Close"], hist["Volume"]
        assert m["momentum_5d"] == pytest.approx((c.iloc[-1] / c.iloc[-5] - 1) * 100)
        assert m["volume_ratio"] == pytest.approx(v.iloc[-1] / v.iloc[-20:].mean())
        assert m["volatility_20d"] == pytest.approx(float(c.pct_change().dropna().std() * 252 ** 0.5 * 100))
        lagged = (c.iloc[-2] / c.iloc[-6] - 1) * 100
        assert abs(m["momentum_5d"] - lagged) > 1e-6, "前提：止于 D 与止于 D−1 的动量确实不同"

    def test_yfinance_source_price_and_session_close_are_d(self, monkeypatch, yf_frame):
        _xnow(monkeypatch, EARLY, 12, 59)
        hist = _frame(EARLY)
        yf_frame["frame"] = hist
        data = dp.YFinanceSource().fetch("NVDA")
        last = float(hist["Close"].iloc[-1])
        assert data.price == last and data.session_close == (EARLY.isoformat(), last)


# ───────────────────────────── 4. 回测 / 回填两处同一个阈值
class TestBacktesterAndBackfill:
    def test_backtester_scores_the_early_close_day_after_the_close(self, monkeypatch):
        from tests.test_backtest_forming_bar import _run
        after = dt.datetime.combine(EARLY, dt.time(12, 59))
        during = dt.datetime.combine(EARLY, dt.time(12, 30))
        assert _run(monkeypatch, EARLY, after, 55.5) == 55.5
        assert _run(monkeypatch, EARLY, during, 55.5) is None

    def test_backfill_takes_the_early_close_day_after_the_close(self, monkeypatch):
        import backfill_dir_accuracy as bda
        hist = _frame(EARLY)
        series = hist["Close"]
        _xnow(monkeypatch, EARLY, 12, 59)
        assert bda._close_after(series, EARLY.isoformat()) == (float(series.iloc[-1]), EARLY.isoformat())
        _xnow(monkeypatch, EARLY, 12, 30)
        assert bda._close_after(series, EARLY.isoformat()) == (None, None)


# ───────────────────────────── 5. 不许再写死（唯一实现）
def hardcoded_last_minute_sites(sources: dict) -> list:
    """生产代码里 `…time(15, 59)` 的调用点；只许出现在 `data_pipeline._session_last_minute`（日历判不了时的回落）。"""
    hits = []
    for rel, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        owner = {}

        def _walk(node, fn):
            for ch in ast.iter_child_nodes(node):
                nxt = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                owner[id(ch)] = nxt
                _walk(ch, nxt)
        _walk(tree, None)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
            args = [a.value for a in n.args if isinstance(a, ast.Constant)]
            if name.endswith("time") and args[:2] == [15, 59]:
                if not (rel == "data_pipeline.py" and owner.get(id(n)) == "_session_last_minute"):
                    hits.append(f"{rel}:{n.lineno}（{owner.get(id(n)) or '模块层'}）")
    return hits


def _production_sources() -> dict:
    files, _how = own_python_files(ROOT)
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in files if p.relative_to(ROOT).parts[0] not in {"tests", "experiments"}}


class TestSingleThreshold:
    def test_no_hardcoded_last_minute_outside_the_helper(self):
        srcs = _production_sources()
        assert "data_pipeline.py" in srcs and "backtester.py" in srcs and len(srcs) > 50, "文件枚举没接上"
        assert hardcoded_last_minute_sites(srcs) == []
        assert "_dt_time(15, 59)" in srcs["data_pipeline.py"], "正面核对：回落那一处确实在（守卫认得出它）"

    def test_guard_has_teeth(self):
        src = (ROOT / "backtester.py").read_text(encoding="utf-8")
        old = "_xnow.time() < _session_last_minute(_xnow.date())"
        assert src.count(old) == 1, "变异锚点失效"
        mutated = src.replace(old, "_xnow.time() < _dt_time(15, 59)")
        line = next(i for i, ln in enumerate(mutated.splitlines(), 1) if "_dt_time(15, 59)" in ln)
        hits = hardcoded_last_minute_sites({"backtester.py": mutated})
        assert len(hits) == 1 and hits[0].startswith(f"backtester.py:{line}（"), hits
        # 回落那一处挪出 `_session_last_minute` 也要红
        dp_src = (ROOT / "data_pipeline.py").read_text(encoding="utf-8")
        moved = dp_src.replace("def _session_last_minute(d)", "def _renamed_helper(d)")
        assert hardcoded_last_minute_sites({"data_pipeline.py": moved}), "白名单按函数名认，改名后应红"
