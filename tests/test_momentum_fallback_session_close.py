"""momentum_5d 自攒索引回落要以扫描日当场收盘为窗口末端（v0.45.465 回归）

事故形状：`data_pipeline._fill_momentum_from_index`（yfinance 失败时的回落）的序列 =
冻在 08-14 的价格索引（`append_observation` 从无生产调用方）+ `pheromone.db` 的
`price_at_predict`。扫描日当天那一行要到取数**之后**才写 ⇒ 取数时序列最新一点是上一个
扫描日，而 yfinance 收盘后的日线含当天那根——同名「5 日动量」，窗口差一个扫描日。
09-28~10-09 回放 300 对：平均 |Δ| 3.79pp、符号一致 66%；含当天收盘 1.91pp / 83%。
VZ 10-09 当天跌约 10%：yfinance −9.16%，回落 +0.80%。

修法：CBOE 取数时若 payload 的 `close` 经 `cboe_options.close_verdict` 判为**官方收盘**，
把 `(场次日期, close)` 交给回落在内存里并进序列（不写索引）。盘中 / 盘中陈旧 / 判不了归属的
价不许进。

本文件的夹具（下面 `_CLOSES`）就照 VZ 那天的形状造：前一天之前小涨、当天大跌——滞后窗口
与正确窗口**符号相反**，所以任何一条退回滞后的路径都会红，不是差一点点。
"""

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import cboe_options as co
import data_pipeline as dp
import price_history as ph
from hive_logger import PATHS

# 09-28(一) … 10-08(四) 九个交易日；D = 10-09(五)
_DATES = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
          "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
_CLOSES = [40.0, 40.0, 40.0, 40.0, 40.0, 41.0, 41.0, 41.0, 40.32]
D = "2026-10-09"
D_CLOSE = 36.24
# 窗口 4 个交易日（与主路径同口径，v0.45.469）。滞后窗口：10-08 / 10-02 = +0.80%；正确窗口：10-09 / 10-05 = −11.61%
LAGGED = round((40.32 / 40.0 - 1) * 100, 4)
SAME_DAY = round((D_CLOSE / 41.0 - 1) * 100, 4)
SCAN_ET = datetime(2026, 10, 9, 17, 30)        # 生产扫描时刻（14:30 PDT）


@pytest.fixture
def db(tmp_path):
    """沙箱权威价库，只有 D 之前的行（= 取数那一刻生产库的样子）。

    目标路径按沙箱自己算、写前断言 `PATHS.db` 指向它——不经被测属性写（CLAUDE.md 数据根节）。
    """
    target = tmp_path / "test.db"
    assert Path(PATHS.db) == target, "conftest 的 _isolate_env 没生效，本文件作废"
    con = sqlite3.connect(target)
    con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT, price_at_predict REAL)")
    con.executemany("INSERT INTO predictions VALUES (?,?,?)",
                    [(d, "VZ", c) for d, c in zip(_DATES, _CLOSES)])
    con.commit()
    con.close()
    cache = tmp_path / "cache"
    assert Path(PATHS.cache_dir) == cache
    return target


def _payload(close, last_trade, current=None):
    return {"symbol": "VZ", "close": close, "current_price": current if current is not None else close + 0.5,
            "last_trade_time": last_trade}


# ── price_history：latest_close 的语义 ──────────────────────────────────────────

class TestLatestCloseInPriceHistory:
    def test_without_it_window_ends_previous_scan_day(self, db):
        """记录现状：不传就是滞后一个扫描日（这条是回落要补的那个缺口的定义）。"""
        r = ph.momentum_5d_detail("VZ")
        assert r == {"momentum_5d": LAGGED, "as_of": "2026-10-08", "anchor": "2026-10-02"}

    def test_session_close_becomes_window_end(self, db):
        r = ph.momentum_5d_detail("VZ", latest_close=(D, D_CLOSE))
        assert r == {"momentum_5d": SAME_DAY, "as_of": D, "anchor": "2026-10-05"}
        assert (SAME_DAY < 0) != (LAGGED < 0), "夹具失效：两个窗口必须符号相反"

    def test_float_wrapper_passes_it_through(self, db):
        assert ph.momentum_5d("VZ", latest_close=(D, D_CLOSE)) == SAME_DAY

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), None, "x"])
    def test_invalid_close_ignored(self, db, bad):
        r = ph.momentum_5d_detail("VZ", latest_close=(D, bad))
        assert r["as_of"] == "2026-10-08" and r["momentum_5d"] == LAGGED

    def test_non_trading_day_close_filtered(self, db):
        r = ph.momentum_5d_detail("VZ", latest_close=("2026-10-10", 30.0))   # 周六
        assert r["as_of"] == "2026-10-08"

    def test_overrides_db_same_day_row(self, db):
        """同日重跑：DB 已有当天行（可能出自盘中陈旧价），核过的收盘优先。"""
        con = sqlite3.connect(db)
        con.execute("INSERT INTO predictions VALUES (?,?,?)", (D, "VZ", 50.0))
        con.commit()
        con.close()
        assert ph.momentum_5d_detail("VZ")["momentum_5d"] == round((50.0 / 41.0 - 1) * 100, 4)
        assert ph.momentum_5d_detail("VZ", latest_close=(D, D_CLOSE))["momentum_5d"] == SAME_DAY

    def test_never_written_to_disk(self, db, tmp_path):
        """只在内存里合并：索引 / 缓存目录 / 库都不因它改变。"""
        cache = tmp_path / "cache"
        (cache / "price_history_VZ.jsonl").write_text(
            json.dumps({"date": "2026-08-14", "close": 39.0}) + "\n", encoding="utf-8")
        ph.momentum_5d_detail("VZ")          # 先让一次性迁移标记落地
        before = {p: p.read_bytes() for p in cache.rglob("*") if p.is_file()}
        db_before = db.read_bytes()
        ph.momentum_5d_detail("VZ", latest_close=(D, D_CLOSE))
        after = {p: p.read_bytes() for p in cache.rglob("*") if p.is_file()}
        assert after == before and db.read_bytes() == db_before


# ── data_pipeline：只有核实过的官方收盘能进来 ──────────────────────────────────

class TestCboeSessionClose:
    def test_official_close_after_session(self, monkeypatch):
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        assert dp._cboe_session_close(_payload(D_CLOSE, "2026-10-09T15:59:58")) == (D, D_CLOSE)

    def test_intraday_rejected(self, monkeypatch):
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 10, 9, 11, 0))
        assert dp._cboe_session_close(_payload(37.0, "2026-10-09T10:44:00")) is None

    def test_stale_intraday_file_after_close_rejected(self, monkeypatch):
        """收盘后拿到盘中生成的文件：close 是那一刻的成交价（T 09-11 12:10 那类）。"""
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        assert dp._cboe_session_close(_payload(37.0, "2026-10-09T12:10:00")) is None

    def test_unverifiable_rejected(self, monkeypatch):
        """缺 last_trade_time：official_price fail-open 仍标 cboe_close，这里不收。"""
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        p = _payload(D_CLOSE, None)
        assert co.official_price(p, SCAN_ET)[1] == "cboe_close", "前提：official_price 会放行它"
        assert dp._cboe_session_close(p) is None

    def test_weekend_run_dates_to_last_session(self, monkeypatch):
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 10, 10, 9, 0))   # 周六补跑
        assert dp._cboe_session_close(_payload(D_CLOSE, "2026-10-09T16:00:00")) == (D, D_CLOSE)

    def test_half_day_reads_close_not_after_hours(self, monkeypatch):
        """感恩节次日 13:00 收盘；14:00 时 official_price 按本机钟仍判盘中、给 current_price（盘后价）。
        当场收盘必须是 close。"""
        now = datetime(2026, 11, 27, 14, 0)
        monkeypatch.setattr(co, "_et_now", lambda: now)
        p = _payload(40.0, "2026-11-27T13:00:00", current=40.9)
        assert co.official_price(p, now) == (40.9, "cboe_intraday"), "前提：钟口径给的是盘后价"
        assert dp._cboe_session_close(p) == ("2026-11-27", 40.0)

    @pytest.mark.parametrize("bad", [0, -3, "nan", None])
    def test_bad_close_rejected(self, monkeypatch, bad):
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        assert dp._cboe_session_close(_payload(bad, "2026-10-09T15:59:58", current=1.0)) is None


# ── 扫描真正走的那条：CBOESource.fetch → yfinance 失败 → 自攒索引回落 ──────────

@pytest.fixture
def yf_down(monkeypatch):
    import yfinance

    def _boom(*a, **k):
        raise RuntimeError("Too Many Requests")
    monkeypatch.setattr(yfinance, "Ticker", _boom)
    monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda t, out: out)


def _fetch_via_cboe(ticker):
    """v0.45.473：回落在 MultiSourceFetcher 的唯一出口，不在 CBOESource 里 ⇒ 经真 fetcher（只挂 CBOE）取。"""
    f = dp.MultiSourceFetcher()
    f._sources = [dp.CBOESource()]
    return f.fetch(ticker)


class TestCboeSourceEndToEnd:
    def test_scan_after_close_uses_same_day_window(self, db, yf_down, monkeypatch):
        monkeypatch.setattr(co, "_fetch_cboe_payload",
                            lambda t, timeout=15: _payload(D_CLOSE, "2026-10-09T15:59:58", current=36.9))
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        d = _fetch_via_cboe("VZ")
        assert d["price"] == D_CLOSE and d["price_source"] == "cboe_close"
        assert d["momentum_5d"] == SAME_DAY, f"窗口滞后了一个扫描日（滞后值 {LAGGED}）"
        assert d["momentum_source"] == "price_index", "回落值不许标成 yfinance 实算的 5d_real"
        assert d["momentum_as_of"] == D

    def test_source_itself_only_carries_the_close(self, db, yf_down, monkeypatch):
        """v0.45.473：CBOESource 只报自己取到的——momentum 留 None，核实过的当场收盘随数据带出去。"""
        monkeypatch.setattr(co, "_fetch_cboe_payload",
                            lambda t, timeout=15: _payload(D_CLOSE, "2026-10-09T15:59:58", current=36.9))
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        d = dp.CBOESource().fetch("VZ")
        assert d.momentum_5d is None and d.momentum_source == "unavailable"
        assert d.session_close == (D, D_CLOSE)
        assert "session_close" not in d.to_dict(), "内部调度字段不进下游字典"

    def test_intraday_scan_keeps_previous_close_window(self, db, yf_down, monkeypatch):
        """盘中跑：当天没收盘，盘中价不许进；窗口末端是上一场（与 yfinance 丢掉成形中那根同口径）。"""
        monkeypatch.setattr(co, "_fetch_cboe_payload",
                            lambda t, timeout=15: _payload(36.5, "2026-10-09T11:44:00"))
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 10, 9, 12, 0))
        d = _fetch_via_cboe("VZ")
        assert d["price_source"] == "cboe_intraday"
        assert d["momentum_5d"] == LAGGED and d["momentum_as_of"] == "2026-10-08"

    def test_yfinance_short_history_branch_also_gets_session_close(self, db, monkeypatch):
        """yfinance 应答了但不足 5 根（没算出动量）：当场收盘同样要传到出口的回落，
        且 yfinance 算出的 volume_ratio 不许被一并丢掉（v0.45.473 前会丢）。"""
        import pandas as pd
        import yfinance

        class _T:
            def __init__(self, *_a, **_k):
                pass

            def history(self, **_k):
                idx = pd.to_datetime(["2026-10-07", "2026-10-08", "2026-10-09"])
                return pd.DataFrame({"Close": [41.0, 40.32, D_CLOSE], "Volume": [1e6, 1e6, 3e6]}, index=idx)
        monkeypatch.setattr(yfinance, "Ticker", _T)
        monkeypatch.setattr(dp, "_exchange_now", lambda: None)
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda t, out: out)
        monkeypatch.setattr(co, "_fetch_cboe_payload",
                            lambda t, timeout=15: _payload(D_CLOSE, "2026-10-09T15:59:58"))
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        d = _fetch_via_cboe("VZ")
        assert (d["momentum_5d"], d["momentum_as_of"], d["momentum_source"]) == (SAME_DAY, D, "price_index")
        assert d["volume_ratio"] is not None and d["volume_source"] == "yfinance", \
            "前提：确实走的是 yfinance 有应答的分支，且它算出的量比保住了"

    def test_yfinance_success_untouched(self, db, monkeypatch):
        """yfinance 有值时回落不跑：标签仍是 5d_real、没有 as_of。"""
        monkeypatch.setattr(co, "_fetch_cboe_payload",
                            lambda t, timeout=15: _payload(D_CLOSE, "2026-10-09T15:59:58"))
        monkeypatch.setattr(co, "_et_now", lambda: SCAN_ET)
        monkeypatch.setattr(dp, "_fetch_history_metrics",
                            lambda t: {"momentum_5d": -9.16, "momentum_source": "yfinance",
                                       "volume_ratio": 1.0, "avg_volume": 10})
        d = _fetch_via_cboe("VZ")
        assert (d["momentum_5d"], d["momentum_source"], d["momentum_as_of"]) == (-9.16, "5d_real", None)


class TestRejectedSessionCloseIsLoud:
    def test_warns_when_supplied_close_misses_window(self, db, caplog):
        caplog.set_level("WARNING", logger="alpha_hive.data_pipeline")
        out = dp._fill_momentum_from_index("VZ", None, ("2026-10-10", 30.0))
        assert out["momentum_as_of"] == "2026-10-08"
        assert any("没进自攒索引窗口" in r.getMessage() for r in caplog.records)

    def test_no_warning_without_session_close(self, db, caplog):
        caplog.set_level("WARNING", logger="alpha_hive.data_pipeline")
        out = dp._fill_momentum_from_index("VZ", None)
        assert out["momentum_as_of"] == "2026-10-08"
        assert not any("没进自攒索引窗口" in r.getMessage() for r in caplog.records)


class TestTerminalFallbackCarriesAsOf:
    def test_all_sources_dead(self, db, monkeypatch):
        class _Dead:
            def fetch(self, t):
                return None
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda t, out: out)
        f = dp.MultiSourceFetcher()
        f._sources = [_Dead(), _Dead()]
        r = f.fetch("VZ")
        assert r["_data_unavailable"] is True
        assert r["momentum_5d"] == LAGGED and r["momentum_as_of"] == "2026-10-08", \
            "全源失败时没有当场收盘：值照给，但末端日期必须看得见"
        assert not math.isnan(r["momentum_5d"])
