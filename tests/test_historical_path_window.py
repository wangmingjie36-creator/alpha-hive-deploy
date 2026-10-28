"""`--date` 补跑路径的行情字段与实时扫描同一个窗口（v0.45.566）

事故形状（v0.45.564 查明）：`data_pipeline._fetch_historical_stock_data` 为保证根数够，向 yfinance 要 as_of 前 60 天，
然后**直接拿这 60 天（约 40 根）算波动率**——实时扫描（`period="1mo"`）是一个月（约 21 根），不是同一个量。生产归档里
2026-08-27 的 `price.volatility_20d` 30/30 符合 60 天窗口：那天是次日 `--date` 补跑的（这就是「08-27 复现不出」）。
一个月只有 19 根的天，量比的均量还多算进窗口外的一根（实时路径不足 20 根时取全部）。

修法：先把历史截到 (as_of − 1 个日历月, as_of]（与实时路径、Twelve Data 回落同一个实现 `_volatility_20d_window_lo`），
再按实时路径的写法算动量 / 量比 / 波动率。用户 10-20：「现在修补跑路径的 60 天窗口」。
"""

import ast
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

import data_pipeline as dp
from bars_integrity import trading_days

#: 真实金值：NVDA 2026-10-16（yfinance 复权收盘，10-19 取；as_of 前 60 天 = 08-17 起共 44 根）。生产归档 `price.volatility_20d`
#: = 24.65248730557474——那天是实时扫描出的值（一个月窗口 (09-16, 10-16] 22 根）。窗口外 09-10 除息（复权只影响窗口外）。
NVDA_ITEMS = [
    ("2026-08-17", 224.758499), ("2026-08-18", 219.4944), ("2026-08-19", 217.316833), ("2026-08-20", 216.607635),
    ("2026-08-21", 214.480011), ("2026-08-24", 208.246979), ("2026-08-25", 212.811874), ("2026-08-26", 209.425659),
    ("2026-08-27", 227.725174), ("2026-08-28", 217.306839), ("2026-08-31", 220.533234), ("2026-09-01", 217.196976),
    ("2026-09-02", 224.15918), ("2026-09-03", 228.194656), ("2026-09-04", 230.102524), ("2026-09-08", 225.477692),
    ("2026-09-09", 223.419998), ("2026-09-10", 218.360001), ("2026-09-11", 218.289993), ("2026-09-14", 210.960007),
    ("2026-09-15", 212.169998), ("2026-09-16", 213.899994), ("2026-09-17", 219.339996), ("2026-09-18", 222.270004),
    ("2026-09-21", 227.380005), ("2026-09-22", 228.869995), ("2026-09-23", 225.509995), ("2026-09-24", 224.580002),
    ("2026-09-25", 225.070007), ("2026-09-28", 228.860001), ("2026-09-29", 227.210007), ("2026-09-30", 228.380005),
    ("2026-10-01", 230.860001), ("2026-10-02", 233.949997), ("2026-10-05", 238.899994), ("2026-10-06", 239.240005),
    ("2026-10-07", 237.470001), ("2026-10-08", 230.479996), ("2026-10-09", 229.279999), ("2026-10-12", 226.320007),
    ("2026-10-13", 228.520004), ("2026-10-14", 221.300003), ("2026-10-15", 217.229996), ("2026-10-16", 214.130005),
]
NVDA_ARCHIVED = 24.65248730557474


def _frame(items, vols=None):
    vols = vols or [1_000_000.0 + (i % 7) * 120_000 for i in range(len(items))]
    return pd.DataFrame({"Close": [c for _, c in items], "Volume": vols},
                        index=pd.DatetimeIndex([d for d, _ in items]).tz_localize("America/New_York"))


@pytest.fixture
def yf(monkeypatch):
    """yfinance.Ticker(...).history：`period="1mo"`（实时路径）回一个月那截，`start=/end=`（补跑路径）回 60 天全量。"""
    import yfinance
    holder = {"full": None, "month": None, "calls": []}

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **kw):
            holder["calls"].append(kw)
            return holder["month"] if kw.get("period") == "1mo" else holder["full"]

    monkeypatch.setattr(yfinance, "Ticker", _T)
    monkeypatch.setattr(dp, "_exchange_now", lambda: None)
    return holder


def _vol(closes):
    return float(pd.Series(closes).pct_change().dropna().std() * 252 ** 0.5 * 100)


class TestWindowStart:
    @pytest.mark.parametrize("as_of, lo", [
        ("2026-10-16", "2026-09-16"), ("2026-03-31", "2026-02-28"), ("2026-12-31", "2026-11-30"),
        ("2026-01-15", "2025-12-15"), ("2028-03-30", "2028-02-29"), ("2026-05-31", "2026-04-30"),
    ])
    def test_one_calendar_month_back_clamped_to_month_end(self, as_of, lo):
        assert dp._volatility_20d_window_lo(dt.date.fromisoformat(as_of)) == dt.date.fromisoformat(lo)

    def test_the_input_code_guard_sees_the_window_helper(self):
        """名字带 `volatility_20d` ⇒ 输入层指纹守卫（price.fields）把它算进单元：以后改窗口规则会红。"""
        import signal_code_fingerprints as scf
        src = Path(dp.__file__).read_text(encoding="utf-8")
        located = scf._loc_mentions(ast.parse(src), "data_pipeline.py", "")[0]   # v0.45.671 起返回 (文本, 节点, 归属名)
        assert "def _volatility_20d_window_lo" in located


class TestHistoricalPathUsesTheLiveWindow:
    def test_real_nvda_day_reproduces_the_live_archive(self, yf):
        """真实金值：补跑 10-16 现在等于那天实时扫描入档的值；改前（60 天全量）差 3 点多。"""
        yf["full"] = _frame(NVDA_ITEMS)
        out = dp._fetch_historical_stock_data("NVDA", "2026-10-16")
        assert out["volatility_20d"] == pytest.approx(NVDA_ARCHIVED, abs=1e-4)
        old = _vol([c for _, c in NVDA_ITEMS])
        assert abs(old - NVDA_ARCHIVED) > 1.0, f"前提：60 天全量（{old:.3f}）确实是另一个数"
        assert yf["calls"][0].get("start") == "2026-08-17", "前提：补跑仍多拉 60 天（只为根数够），窗口在算之前截"

    def test_same_bars_give_the_same_fields_as_the_live_path(self, yf):
        """同一份日线：实时路径（period=1mo 那截）与补跑路径（60 天再截）四个字段逐位一致。"""
        full = _frame(NVDA_ITEMS)
        yf["full"] = full
        yf["month"] = full[full.index.date > dt.date(2026, 9, 16)]
        live = dp._fetch_history_metrics("NVDA")
        replay = dp._fetch_historical_stock_data("NVDA", "2026-10-16")
        for k in ("momentum_5d", "volume_ratio", "avg_volume", "volatility_20d"):
            assert replay[k] == live[k], (k, replay[k], live[k])

    def test_19_bar_month_averages_only_the_window(self, yf):
        """(02-10, 03-10] 只有 19 根：均量取这 19 根（实时路径不足 20 根取全部），不再多算进窗口外的第 20 根。"""
        days = trading_days(dt.date(2026, 1, 9), dt.date(2026, 3, 10))
        items = [(d.isoformat(), 100.0 + i * 0.5 + (i % 4)) for i, d in enumerate(days)]
        vols = [5_000_000.0 if d.isoformat() <= "2026-02-10" else 1_000_000.0 + i * 1000 for i, d in enumerate(days)]
        yf["full"] = _frame(items, vols)
        out = dp._fetch_historical_stock_data("NVDA", "2026-03-10")
        win = [v for (d, _), v in zip(items, vols) if d > "2026-02-10"]
        assert len(win) == 19, "前提：窗口恰好 19 根"
        assert out["volume_ratio"] == pytest.approx(win[-1] / (sum(win) / 19))
        assert out["volume_ratio"] != pytest.approx(vols[-1] / (sum(vols[-20:]) / 20)), "判别：窗口外那根的量故意放大"
        assert out["volatility_20d"] == pytest.approx(_vol([c for d, c in items if d > "2026-02-10"]))
