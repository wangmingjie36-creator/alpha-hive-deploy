"""CBOE 盘中价的两个没被 v0.45.234 盖住的消费者（v0.45.243）

v0.45.234 让 `official_price` 认出「收盘后拿到盘中生成的 payload」，数据管道据此拒收。
但另有两处**不经 official_price**、自己推断「这是哪一场的收盘」：

1. 补跑兜底 `data_pipeline._fetch_historical_stock_data`：yfinance 缺目标日收盘时
   直接取云端快照 `price_at_fetch`，不看 last_trade。pheromone.db 2026-08-28 的
   DE 624.85（官方 630.33）/ TMO 626.325（622.18）/ CVX 201.44（201.86）/ VZ 49.985（50.10）
   就是这么进的库。
2. `close_correction` 的 CBOE 交叉印证：按 CDN timestamp 把 `close` 归到「最近一个已收盘
   交易日」。盘中 `close` 是实时价 ⇒ 盘中跑会报一片假的「两源分歧拒改」；收盘后拿到的
   盘中文件同样被归到当天。

数据来源（除注明外均为实测原值）：
- 云端快照：`origin/cloud-snapshots` 分支 `cloud_snapshots/<日期>/<标的>.json` 的
  price_at_fetch / last_trade_time_et / fetched_at_utc / prev_day_close
- 官方收盘：yfinance 日线（2026-09-14 批量下载）
- 盘中实拉：2026-09-14 10:55 ET，T close 26.2299 = current_price、prev_day_close 26.06；
  NVDA close 210.47、prev_day_close 218.29。⚠️ 这两份的 `last_trade_time` 是**按 15 分钟
  延迟重建**的（当时没存），close / prev_day_close 是实拉值。
- 语料级：290 份快照里 prev_day_close 对前一交易日官方收盘 289 份 ≤0.01%、1 份 <0.05%
  （BILI 16.76 vs 16.765，舍入），含全部 71 份盘中陈旧文件。
"""

from datetime import date, datetime

import pandas as pd
import pytest

import cboe_options as co
import close_correction as cc
import cloud_snapshot_loader as csl
import data_pipeline as dp


def _snap(ticker, px, last_trade, fetched_utc, prev):
    return {"ticker": ticker, "price_at_fetch": px, "price_source": "cboe_close",
            "last_trade_time_et": last_trade, "fetched_at_utc": fetched_utc,
            "vintage_date": last_trade[:10], "prev_day_close": prev}


# 云端快照原值（label 全是 cboe_close —— v0.45.234 之前产出，陈旧也这么标）
SNAPS = {
    "2026-08-28": {
        "DE":  _snap("DE", 624.85, "2026-08-28T14:55:46", "2026-08-28T21:02:23.708314+00:00", 622.66),
        "TMO": _snap("TMO", 626.325, "2026-08-28T09:45:27", "2026-08-28T21:02:35.424372+00:00", 630.7),
        "CVX": _snap("CVX", 201.44, "2026-08-28T14:58:31", "2026-08-28T21:02:22.933218+00:00", 199.77),
        "VZ":  _snap("VZ", 49.985, "2026-08-28T15:31:50", "2026-08-28T21:02:38.656386+00:00", 49.43),
    },
    "2026-08-31": {   # 当天 TMO 不在快照里
        "DE":  _snap("DE", 650.2, "2026-08-31T14:20:20", "2026-08-31T21:02:26.064592+00:00", 630.33),
        "CVX": _snap("CVX", 205.32, "2026-08-31T14:30:58", "2026-08-31T21:02:25.291336+00:00", 201.86),
        "VZ":  _snap("VZ", 50.02, "2026-08-31T15:59:59", "2026-08-31T21:02:41.139882+00:00", 50.1),
    },
    "2026-09-01": {
        "TMO": _snap("TMO", 601.44, "2026-09-01T14:39:53", "2026-09-01T21:02:58.358954+00:00", 617.1),
    },
    "2026-09-04": {   # 周五；09-07 劳动节
        "DE":  _snap("DE", 695.115, "2026-09-04T15:35:43", "2026-09-04T21:02:28.251445+00:00", 0.0),
    },
    "2026-09-08": {
        "DE":  _snap("DE", 0.0, "2026-09-08T15:48:26", "2026-09-08T21:00:00+00:00", 693.53),
    },
    "2026-09-10": {
        "T":   _snap("T", 25.55, "2026-09-10T15:59:59", "2026-09-10T21:03:45.249662+00:00", 25.15),
    },
    "2026-09-11": {
        "T":   _snap("T", 26.225, "2026-09-11T11:49:07", "2026-09-11T21:03:06.288864+00:00", 25.55),
    },
}
OFFICIAL = {("2026-08-28", "DE"): 630.33, ("2026-08-28", "TMO"): 622.18,
            ("2026-08-28", "CVX"): 201.86, ("2026-08-28", "VZ"): 50.10,
            ("2026-08-31", "VZ"): 50.02, ("2026-09-04", "DE"): 693.53,
            ("2026-09-10", "T"): 25.55, ("2026-09-11", "T"): 26.06,
            ("2026-08-27", "TMO"): 630.70, ("2026-09-11", "NVDA"): 218.29}


def _payload(s):
    return {"last_trade_time": s["last_trade_time_et"], "prev_day_close": s["prev_day_close"],
            "close": s["price_at_fetch"]}


def _fetched(s):
    return datetime.fromisoformat(s["fetched_at_utc"])


# ═════════════════════════════ cboe_options：唯一的判据
class TestCloseVerdict:
    @pytest.mark.parametrize("d,t", [("2026-08-28", "DE"), ("2026-08-28", "TMO"),
                                     ("2026-08-28", "CVX"), ("2026-08-28", "VZ"),
                                     ("2026-09-11", "T")])
    def test_real_stale_snapshots(self, d, t):
        s = SNAPS[d][t]
        assert co.close_verdict(_payload(s), _fetched(s)) == (co.CLOSE_STALE_INTRADAY, None)

    @pytest.mark.parametrize("d,t", [("2026-08-31", "VZ"), ("2026-09-10", "T")])
    def test_real_fresh_snapshots(self, d, t):
        s = SNAPS[d][t]
        verdict, session = co.close_verdict(_payload(s), _fetched(s))
        assert verdict == co.CLOSE_OFFICIAL and session == date.fromisoformat(d)
        assert s["price_at_fetch"] == pytest.approx(OFFICIAL[(d, t)], abs=0.006)

    def test_intraday_file_is_session_open_not_previous_close(self):
        """09-14 10:55 ET 实拉：close 就是实时价（旧 close_correction 把它归到 09-11）"""
        p = {"close": 26.2299, "current_price": 26.2299, "prev_day_close": 26.06,
             "last_trade_time": "2026-09-14T10:40:00"}      # last_trade 为重建值
        assert co.close_verdict(p, datetime(2026, 9, 14, 10, 55)) == (co.CLOSE_SESSION_OPEN, None)

    def test_premarket_next_day_fresh_file_belongs_to_last_session(self):
        p = {"close": 25.55, "last_trade_time": "2026-09-10T15:59:59"}
        assert co.close_verdict(p, datetime(2026, 9, 11, 8, 31)) == (co.CLOSE_OFFICIAL, date(2026, 9, 10))

    def test_unverifiable(self):
        assert co.close_verdict({"close": 1.0}, datetime(2026, 9, 11, 17, 0)) == (co.CLOSE_UNVERIFIABLE, None)

    def test_half_day(self):
        p = {"last_trade_time": "2026-11-27T12:59:58"}
        assert co.close_verdict(p, datetime(2026, 11, 27, 12, 59, 59))[0] == co.CLOSE_SESSION_OPEN
        assert co.close_verdict(p, datetime(2026, 11, 27, 13, 5)) == (co.CLOSE_OFFICIAL, date(2026, 11, 27))

    def test_agrees_with_official_price_on_every_real_snapshot(self):
        """不许长出第二条规则：同一份 payload 两个入口对「盘中陈旧」的判断必须一致"""
        for d, by_t in SNAPS.items():
            for t, s in by_t.items():
                if not s["price_at_fetch"]:
                    continue
                p = dict(_payload(s), symbol=t)
                stale_a = co.official_price(p, _fetched(s))[1] == co.STALE_INTRADAY_SOURCE
                stale_b = co.close_verdict(p, _fetched(s))[0] == co.CLOSE_STALE_INTRADAY
                assert stale_a == stale_b, (d, t)


class TestPrevCloseSession:
    @pytest.mark.parametrize("d,t,belongs", [
        ("2026-08-31", "DE", "2026-08-28"),    # 陈旧文件（14:20:20）的 prev_day_close 照样准
        ("2026-08-31", "CVX", "2026-08-28"),
        ("2026-08-31", "VZ", "2026-08-28"),
        ("2026-08-28", "TMO", "2026-08-27"),   # 09:45 的陈旧文件
        ("2026-09-08", "DE", "2026-09-04"),    # 跨劳动节
        ("2026-09-11", "T", "2026-09-10"),
    ])
    def test_real_values_match_official_close(self, d, t, belongs):
        got = co.prev_close_session(_payload(SNAPS[d][t]))
        assert got is not None and got[0] == date.fromisoformat(belongs)
        assert got[1] == pytest.approx(OFFICIAL[(belongs, t)], abs=0.006)

    def test_intraday_live_pull(self):
        p = {"close": 210.47, "prev_day_close": 218.29, "last_trade_time": "2026-09-14T10:40:00"}
        assert co.prev_close_session(p) == (date(2026, 9, 11), 218.29)

    @pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"), "x"])
    def test_invalid_price(self, bad):
        assert co.prev_close_session({"prev_day_close": bad, "last_trade_time": "2026-09-11T15:59:59"}) is None

    def test_no_last_trade_means_no_attribution(self):
        assert co.prev_close_session({"prev_day_close": 26.06}) is None


# ═════════════════════════════ 消费者 1：补跑兜底
@pytest.fixture
def snapshots(monkeypatch):
    def install(by_date):
        monkeypatch.setattr(csl, "load_ticker", lambda d, t, **k: (by_date.get(d) or {}).get(t))
        monkeypatch.setattr(csl, "available_dates", lambda *a, **k: sorted(by_date))
    install(SNAPS)
    return install


class TestLoadOfficialClose:
    @pytest.mark.parametrize("t,expect", [("DE", 630.33), ("CVX", 201.86), ("VZ", 50.10)])
    def test_stale_0828_recovered_from_next_snapshot_prev_close(self, snapshots, t, expect):
        px, src = csl.load_official_close("2026-08-28", t)
        assert src == csl.SNAP_NEXT_PREV_CLOSE
        assert px == pytest.approx(expect, abs=0.006), "入库的 624.85/201.44/49.985 不许再出现"

    def test_tmo_no_next_snapshot_is_unavailable(self, snapshots):
        """08-31 快照没有 TMO；09-01 的 prev_day_close 属 08-31 —— 不许拿来冒充 08-28"""
        assert csl.load_official_close("2026-08-28", "TMO") == (None, "snapshot_stale_intraday")

    def test_across_holiday(self, snapshots):
        assert csl.load_official_close("2026-09-04", "DE") == (693.53, csl.SNAP_NEXT_PREV_CLOSE)

    @pytest.mark.parametrize("d,t,px", [("2026-08-31", "VZ", 50.02), ("2026-09-10", "T", 25.55)])
    def test_fresh_snapshot_uses_own_price(self, snapshots, d, t, px):
        assert csl.load_official_close(d, t) == (px, csl.SNAP_CLOSE)

    def test_latest_stale_with_no_later_snapshot(self, snapshots):
        assert csl.load_official_close("2026-09-11", "T") == (None, "snapshot_stale_intraday")

    def test_gap_in_snapshots_does_not_misattribute(self, snapshots):
        """隔了一个交易日的下一份快照：prev_day_close 属于它的前一天，不是目标日"""
        snapshots({"2026-08-28": SNAPS["2026-08-28"], "2026-09-01": {
            "DE": _snap("DE", 1.0, "2026-09-01T15:59:59", "2026-09-01T21:00:00+00:00", 650.0)}})
        assert csl.load_official_close("2026-08-28", "DE") == (None, "snapshot_stale_intraday")

    def test_intraday_fetched_snapshot_rejected(self, snapshots):
        """抓的时候那场还没收（cboe_intraday）——价是盘中价"""
        s = _snap("X", 100.0, "2026-08-28T14:45:00", "2026-08-28T19:00:00+00:00", 99.0)
        s["price_source"] = "cboe_intraday"
        snapshots({"2026-08-28": {"X": s}})
        assert csl.load_official_close("2026-08-28", "X") == (None, "snapshot_session_open")

    def test_label_is_not_trusted(self, snapshots):
        """标签写 cboe_close 也照判；标签写 stale 但 last_trade 贴收盘也照判"""
        s = dict(SNAPS["2026-08-31"]["VZ"], price_source="cboe_stale_intraday")
        snapshots({"2026-08-31": {"VZ": s}})
        assert csl.load_official_close("2026-08-31", "VZ") == (50.02, csl.SNAP_CLOSE)

    def test_no_snapshot_at_all(self, snapshots):
        snapshots({})
        assert csl.load_official_close("2026-08-28", "DE") == (None, "snapshot_no_snapshot")


def _hist_nan_on(target):
    idx = pd.to_datetime(["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", target])
    return pd.DataFrame({"Close": [600.0, 610.0, 615.0, 620.0, float("nan")],
                         "Volume": [1e6] * 5}, index=idx)


@pytest.fixture
def yf_nan(monkeypatch):
    calls = {"n": 0}

    class _T:
        def __init__(self, t):
            pass

        def history(self, **kw):
            calls["n"] += 1
            return _hist_nan_on("2026-08-28")

    monkeypatch.setattr("yfinance.Ticker", _T)
    yield
    assert calls["n"] > 0, "yfinance 桩没被调用 —— 打了真网"


class TestBackfillPipeline:
    def test_de_0828_gets_official_close_and_label(self, snapshots, yf_nan):
        r = dp._fetch_historical_stock_data("DE", "2026-08-28")
        assert r["price"] == pytest.approx(630.33)
        assert r["price_source"] == csl.SNAP_NEXT_PREV_CLOSE
        assert r["source_name"] == f"cloud_snapshot:{csl.SNAP_NEXT_PREV_CLOSE}"
        assert r["_price_from_cloud_snapshot"] is True

    def test_tmo_0828_unavailable_with_verdict(self, snapshots, yf_nan):
        """谁会红？—— 不可用时说得出是「快照是盘中价」而不是「没有快照」"""
        r = dp._fetch_historical_stock_data("TMO", "2026-08-28")
        assert r["_data_unavailable"] is True and not r["price"]
        assert r["_reason"] == "no_close_on_2026-08-28"
        assert r["_snapshot_verdict"] == "snapshot_stale_intraday"

    def test_loader_exception_is_visible(self, monkeypatch, yf_nan):
        def boom(*a, **k):
            raise RuntimeError("git 挂了")
        monkeypatch.setattr(csl, "load_official_close", boom)
        r = dp._fetch_historical_stock_data("DE", "2026-08-28")
        assert r["_data_unavailable"] is True
        assert r["_snapshot_verdict"] == "snapshot_error:RuntimeError"


# ═════════════════════════════ 消费者 2：close_correction
class TestCboeClosesFromPayload:
    def test_intraday_uses_prev_day_close_not_live_price(self):
        now = datetime(2026, 9, 14, 10, 55)
        t = {"close": 26.2299, "current_price": 26.2299, "prev_day_close": 26.06,
             "last_trade_time": "2026-09-14T10:40:00"}
        nvda = {"close": 210.47, "prev_day_close": 218.29, "last_trade_time": "2026-09-14T10:40:00"}
        tally = {}
        assert cc.cboe_closes_from_payload(t, now, tally) == {"2026-09-11": 26.06}
        assert cc.cboe_closes_from_payload(nvda, now, tally) == {"2026-09-11": 218.29}
        assert tally == {co.CLOSE_SESSION_OPEN: 2}

    def test_stale_after_close_drops_close_keeps_prev(self):
        s = SNAPS["2026-08-28"]["DE"]
        assert cc.cboe_closes_from_payload(_payload(s), _fetched(s)) == {"2026-08-27": 622.66}

    def test_fresh_after_close_gives_both(self):
        s = SNAPS["2026-08-31"]["VZ"]
        assert cc.cboe_closes_from_payload(_payload(s), _fetched(s)) == {
            "2026-08-31": 50.02, "2026-08-28": 50.1}


class _Resp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b


def _serve(monkeypatch, payloads):
    import json
    import urllib.request

    def fake(req, timeout=15):
        sym = req.full_url.rsplit("/", 1)[-1].split(".")[0]
        if sym not in payloads:
            raise OSError("404")
        return _Resp(json.dumps({"timestamp": "2026-09-14 14:55:00", "data": payloads[sym]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)


class TestCorrectEndToEnd:
    """把旧 bug 的两个真实场景完整跑一遍 correct()"""

    @pytest.fixture
    def db(self, tmp_path):
        import sqlite3

        def make(rows):
            p = str(tmp_path / "t.db")
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, date TEXT, "
                        "ticker TEXT, price_at_predict REAL)")
            con.executemany("INSERT INTO predictions (date,ticker,price_at_predict) VALUES (?,?,?)", rows)
            con.commit()
            return con
        return make

    def test_intraday_run_no_false_dispute(self, db, monkeypatch):
        """09-14 盘中跑：T 09-11 行 26.225（盘中陈旧价入库）→ 官方 26.06。
        旧代码拿 close 26.2299 当 09-11 收盘印证 ⇒ 分歧 0.65% > 0.2% ⇒ 拒改。"""
        con = db([("2026-09-11", "T", 26.225), ("2026-09-11", "NVDA", 218.0)])
        monkeypatch.setattr(cc, "official_closes", lambda t, lo, hi: {
            ("2026-09-11", "T"): 26.06, ("2026-09-11", "NVDA"): 218.29})
        monkeypatch.setattr(cc, "_prev_trading_day", lambda: "2026-09-11")
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 9, 14, 10, 55))
        _serve(monkeypatch, {
            "T": {"close": 26.2299, "current_price": 26.2299, "prev_day_close": 26.06,
                  "last_trade_time": "2026-09-14T10:40:00"},
            "NVDA": {"close": 210.47, "current_price": 210.47, "prev_day_close": 218.29,
                     "last_trade_time": "2026-09-14T10:40:00"}})
        st = cc.correct(con, apply=False)
        assert st["disputed"] == 0, "盘中实时价被当成上一场收盘去印证了"
        assert st["corrected"] == 2 and st["cross_checked"] == 2
        assert st["cboe_verdicts"] == {co.CLOSE_SESSION_OPEN: 2}

    def test_stale_file_after_close_no_false_dispute(self, db, monkeypatch):
        """08-28 17:02 ET 跑：DE 文件 last_trade 14:55:46、close 624.85。
        旧代码按 timestamp 归到 08-28 ⇒ 与 yfinance 630.33 分歧 0.87% ⇒ 拒改。"""
        con = db([("2026-08-28", "DE", 624.85)])
        monkeypatch.setattr(cc, "official_closes", lambda t, lo, hi: {("2026-08-28", "DE"): 630.33})
        monkeypatch.setattr(cc, "_prev_trading_day", lambda: "2026-08-28")
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 8, 28, 17, 2))
        s = SNAPS["2026-08-28"]["DE"]
        _serve(monkeypatch, {"DE": _payload(s)})
        st = cc.correct(con, apply=False)
        assert st["disputed"] == 0
        assert st["corrected"] == 1 and st["cross_checked"] == 0, "08-28 没有 CBOE 官方收盘可印证"
        assert st["cboe_verdicts"] == {co.CLOSE_STALE_INTRADAY: 1}

    def test_real_dispute_still_refused(self, db, monkeypatch):
        """防过度修复：CBOE 真给出该日官方收盘且与 yfinance 分歧时，仍然拒改"""
        con = db([("2026-08-31", "VZ", 49.0)])
        monkeypatch.setattr(cc, "official_closes", lambda t, lo, hi: {("2026-08-31", "VZ"): 51.0})
        monkeypatch.setattr(cc, "_prev_trading_day", lambda: "2026-08-31")
        monkeypatch.setattr(co, "_et_now", lambda: datetime(2026, 8, 31, 17, 2))
        _serve(monkeypatch, {"VZ": _payload(SNAPS["2026-08-31"]["VZ"])})
        st = cc.correct(con, apply=False)
        assert st["disputed"] == 1

    def test_fetch_failure_counted(self, db, monkeypatch):
        con = db([("2026-08-31", "VZ", 49.0)])
        monkeypatch.setattr(cc, "official_closes", lambda t, lo, hi: {("2026-08-31", "VZ"): 50.02})
        monkeypatch.setattr(cc, "_prev_trading_day", lambda: "2026-08-31")
        _serve(monkeypatch, {})
        st = cc.correct(con, apply=False)
        assert st["cboe_verdicts"] == {"fetch_failed": 1}
        assert st["corrected"] == 1 and st["cross_checked"] == 0
