"""
ic_diagnostics `_load_prices` 的部分下载失败必须可见（v0.45.332）

背景：`yf.download(52 只)` 部分失败时**不抛异常**——失败的票整列 NaN 或干脆没有列，
唯一痕迹是 yfinance 自己的一行 stderr。2026-09-23 连跑 5 次有 4 次各随机丢 1–2 只
（AVGO+RKLB / T+MSTR / CVX / ADBE）。旧 `_load_prices` 只在异常时降级 ⇒ 残缺面板照常返回，
`build_benchmark_panel` 用 `if t not in px.columns` / `len(s) < 26` 静默跳过缺的票 ⇒
`--benchmark` 的经典因子行（20日动量 / 5日反转 / 低波动）少算几只、输出里无从得知
（20日动量 IC 0.0955–0.1142 随丢哪只而变）。

本文件全离线：`yfinance.download` 被替换成按剧本返回的假函数；返回形状照 yfinance 1.2
（多票与单票都是 `(Price, Ticker)` 两层列名），另测旧版单票的平列名形状
（memory `alpha-hive-yfinance-multiindex`：重试路径逐只下载，两种形状都会遇到）。
"""

import datetime
import json
import os
import random
import sqlite3
import sys

import pandas as pd
import pytest
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ic_diagnostics as icd

START, END = "2025-11-01", "2026-07-03"
LATE_BARS = 35
TRIO = ["T0", "T1", "T2"]


def _closes(tickers, start=START, end=END) -> pd.DataFrame:
    """每只票一条确定性的随机游走（种子只依赖票名 ⇒ 批量与单只下载给同一条）。"""
    idx = pd.bdate_range(start, end, inclusive="left")
    cols = {}
    for t in tickers:
        rng = random.Random(t)
        p, path = 100.0, []
        for _ in idx:
            p *= 1 + rng.gauss(0, 0.02)
            path.append(p)
        cols[t] = path
    return pd.DataFrame(cols, index=idx)


def _as_download(close: pd.DataFrame, shape: str = "multi") -> pd.DataFrame:
    if shape == "flat":
        # 旧版 yfinance 单票：平列名，`["Close"]` 是 Series
        return pd.DataFrame({"Close": close.iloc[:, 0], "Open": close.iloc[:, 0]})
    return pd.concat({"Close": close, "Open": close}, axis=1)   # 列 = (Price, Ticker)


class FakeDownload:
    """第一次调用 = 批量下载，之后 = 逐只重试。记录每次请求了哪些票。

    batch_nan    批量里整列 NaN 的票
    batch_absent 批量里干脆没有列的票
    batch_late   批量里只有最后 LATE_BARS 根有价的票（新上市形状：列在、票在，早期记录攒不够 26 根）
    retry_nan    重试时仍整列 NaN 的票
    retry_raises 重试时抛异常的票
    """

    def __init__(self, batch_nan=(), batch_absent=(), batch_late=(), retry_nan=(),
                 retry_raises=(), retry_shape="multi"):
        self.batch_nan, self.batch_absent = set(batch_nan), set(batch_absent)
        self.batch_late = set(batch_late)
        self.retry_nan, self.retry_raises = set(retry_nan), set(retry_raises)
        self.retry_shape = retry_shape
        self.calls = []

    def __call__(self, tickers, start=None, end=None, **kw):
        tickers = [tickers] if isinstance(tickers, str) else list(tickers)
        self.calls.append(tickers)
        close = _closes(tickers, start, end)
        if len(self.calls) == 1:
            for t in self.batch_nan:
                close[t] = float("nan")
            for t in self.batch_late:
                close.iloc[:-LATE_BARS, close.columns.get_loc(t)] = float("nan")
            close = close.drop(columns=[t for t in self.batch_absent if t in close])
            return _as_download(close)
        (t,) = tickers
        if t in self.retry_raises:
            raise RuntimeError(f"simulated retry failure for {t}")
        if t in self.retry_nan:
            close[t] = float("nan")
        return _as_download(close, self.retry_shape)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(icd, "_PRICE_CACHE", {})


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = FakeDownload(**kw)
        monkeypatch.setattr(yf, "download", f)
        return f
    return install


# ────────────────────────────────────────────────────────────────────────────
# missing_tickers：两种缺失形状都要认
# ────────────────────────────────────────────────────────────────────────────

class TestMissingTickers:
    def test_all_nan_column_counts_as_missing(self):
        px = _closes(TRIO)
        px["T1"] = float("nan")
        assert icd.missing_tickers(px, TRIO) == ["T1"]

    def test_absent_column_counts_as_missing(self):
        assert icd.missing_tickers(_closes(["T0", "T2"]), TRIO) == ["T1"]

    def test_partially_nan_column_is_not_missing(self):
        """有一个有效价就算拿到了 —— 本函数只管「整只票没来」，不管单日缺价。"""
        px = _closes(TRIO)
        px.iloc[:-1, 1] = float("nan")
        assert icd.missing_tickers(px, TRIO) == []

    def test_none_means_everything_missing(self):
        assert icd.missing_tickers(None, TRIO) == TRIO


# ────────────────────────────────────────────────────────────────────────────
# _load_prices：缺的逐只重试一轮；仍缺的点名
# ────────────────────────────────────────────────────────────────────────────

class TestLoadPricesRetry:
    @pytest.mark.parametrize("gap", ["batch_nan", "batch_absent"])
    @pytest.mark.parametrize("retry_shape", ["multi", "flat"])
    def test_missing_ticker_is_refetched_and_recovered(self, fake, capsys, gap, retry_shape):
        f = fake(**{gap: ["T1"]}, retry_shape=retry_shape)
        px = icd._load_prices(TRIO, START, END)
        assert f.calls == [TRIO, ["T1"]], "只该对缺的那只逐只重试一次"
        assert icd.missing_tickers(px, TRIO) == []
        pd.testing.assert_series_equal(px["T1"], _closes(["T1"])["T1"], check_names=False)
        err = capsys.readouterr().err
        assert "1/3" in err and "T1" in err and "补齐" in err

    @pytest.mark.parametrize("how", ["retry_nan", "retry_raises"])
    def test_still_missing_after_retry_is_named_on_stderr(self, fake, capsys, how):
        f = fake(batch_nan=["T1"], **{how: ["T1"]})
        px = icd._load_prices(TRIO, START, END)
        assert px is not None, "只缺一只不是整批失败，其余的票照常返回"
        assert f.calls == [TRIO, ["T1"]], "重试恰好一轮"
        assert icd.missing_tickers(px, TRIO) == ["T1"]
        err = capsys.readouterr().err
        assert "缺 1/3" in err and "T1" in err and "其余 2 只" in err

    def test_complete_frame_no_retry_no_warning(self, fake, capsys):
        """负对照：不缺就不重试、不出声 —— 否则「有警告」证明不了任何事。"""
        f = fake()
        px = icd._load_prices(TRIO, START, END)
        assert f.calls == [TRIO]
        assert icd.missing_tickers(px, TRIO) == []
        assert capsys.readouterr().err == ""

    def test_nothing_came_back_degrades_without_per_ticker_retry(self, fake, capsys):
        """一只都没有 = 整批失败（多半限流），逐只重试只会再撞同一个限流 N 次。"""
        f = fake(batch_nan=TRIO)
        assert icd._load_prices(TRIO, START, END) is None
        assert f.calls == [TRIO]
        assert "0/3" in capsys.readouterr().err

    def test_batch_exception_still_degrades_to_none(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(yf, "download", boom)
        assert icd._load_prices(TRIO, START, END) is None
        assert "价格类基准跳过" in capsys.readouterr().err


# ────────────────────────────────────────────────────────────────────────────
# --benchmark：覆盖率进返回值、进表头、进 JSON
# ────────────────────────────────────────────────────────────────────────────

TICKERS = [f"T{i}" for i in range(8)]


def _bdays(start: str, n: int):
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


DAYS = _bdays("2026-06-01", 24)   # ≥10 天才进 --benchmark 的因子表
N_REC = len(DAYS) * len(TICKERS)


def _build_db(tmp_path):
    rng = random.Random(332)
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        final_score REAL, dimension_scores TEXT,
        price_at_predict REAL, price_t7 REAL, return_t7 REAL, close_t7 REAL,
        exit_price REAL, exit_reason TEXT, checked_t7 INTEGER DEFAULT 0)""")
    rows = []
    for d in DAYS:
        for tk in TICKERS:
            x = rng.gauss(0, 1)
            close = 100.0 * (1 + (x + rng.gauss(0, 1)) / 100.0)
            rows.append((d, tk, x, json.dumps({k: x for k in icd.DIMS}),
                         100.0, close, close - 100.0, close, None, None))
    con.executemany(
        "INSERT INTO predictions (date,ticker,final_score,dimension_scores,price_at_predict,"
        "price_t7,return_t7,close_t7,exit_price,exit_reason,checked_t7) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1)", rows)
    con.commit()
    con.close()
    return db


def _panel(db):
    return icd.build_benchmark_panel(db, "return_t7", "checked_t7", "t7")


MOM = "📈 20日动量"


class TestBenchmarkCoverage:
    def test_partial_coverage_is_reported_and_matches_the_panel(self, tmp_path, fake):
        fake(batch_nan=["T3"], retry_nan=["T3"])
        panel, cov = _panel(_build_db(tmp_path))
        assert cov["status"] == "partial"
        assert (cov["n_priced"], cov["n_tickers"], cov["missing"]) == (7, 8, ["T3"])
        assert (cov["n_factor_records"], cov["n_records"]) == (7 * len(DAYS), N_REC)
        # 覆盖率说的必须是面板的实情：经典因子行每天确实只有 7 只，综合分行 8 只
        assert {len(v) for v in panel[MOM].values()} == {7}
        assert {len(v) for v in panel["🐝 综合分 final_score"].values()} == {8}

    def test_complete_coverage_negative_control(self, tmp_path, fake):
        fake()
        panel, cov = _panel(_build_db(tmp_path))
        assert cov == {"status": "ok", "n_tickers": 8, "n_priced": 8, "missing": [],
                       "n_records": N_REC, "n_factor_records": N_REC}
        assert {len(v) for v in panel[MOM].values()} == {8}

    def test_all_tickers_priced_but_records_short_is_still_partial(self, tmp_path, fake):
        """票一只不缺、但早期记录攒不够 26 根被 `len(s) < 26` 跳过 —— 同样不是同一样本。"""
        fake(batch_late=["T5"])
        panel, cov = _panel(_build_db(tmp_path))
        assert cov["missing"] == [] and cov["n_priced"] == 8
        assert 0 < N_REC - cov["n_factor_records"] < len(DAYS), "夹具要让 T5 恰好丢掉一部分天"
        assert cov["status"] == "partial"
        assert "缺 " not in icd.format_price_coverage(cov)

    def test_recovered_by_retry_counts_as_complete(self, tmp_path, fake):
        fake(batch_nan=["T3"])
        assert _panel(_build_db(tmp_path))[1]["status"] == "ok"

    def test_unavailable_prices(self, tmp_path, monkeypatch):
        monkeypatch.setattr(icd, "_load_prices", lambda *a, **k: None)
        panel, cov = _panel(_build_db(tmp_path))
        assert cov["status"] == "unavailable" and cov["n_priced"] == 0
        assert MOM not in panel


def _run_main(monkeypatch, db, *extra):
    monkeypatch.setattr(sys, "argv", ["ic_diagnostics.py", "--db", str(db), "--horizon", "t7",
                                      "--benchmark", "--draws", "5", *extra])
    assert icd.main() == 0


class TestBenchmarkOutput:
    def test_text_header_names_the_gap(self, tmp_path, fake, monkeypatch, capsys):
        fake(batch_nan=["T3"], retry_nan=["T3"])
        _run_main(monkeypatch, _build_db(tmp_path))
        out = capsys.readouterr().out
        assert "经典因子行情覆盖 7/8 只" in out and "缺 T3" in out
        assert "不是同一样本" in out
        # 判定行自己也要带上（表头那句不能代它 —— 两处都有「不是同一样本」）
        verdict = next(ln for ln in out.splitlines() if ln.strip().startswith("判定："))
        assert f"只覆盖 {7 * len(DAYS)}/{N_REC} 条记录" in verdict

    def test_text_header_complete_negative_control(self, tmp_path, fake, monkeypatch, capsys):
        fake()
        _run_main(monkeypatch, _build_db(tmp_path))
        out = capsys.readouterr().out
        assert "经典因子行情覆盖 8/8 只" in out
        assert "不是同一样本" not in out
        assert any(ln.strip().startswith("判定：") for ln in out.splitlines()), \
            "判定行没印出来 —— 上一条的「不含」就是空转"

    def test_json_carries_price_coverage(self, tmp_path, fake, monkeypatch, capsys):
        fake(batch_nan=["T3"], retry_nan=["T3"])
        _run_main(monkeypatch, _build_db(tmp_path), "--json")
        out = json.loads(capsys.readouterr().out)
        cov = out["t7"]["benchmark"]["price_coverage"]
        assert cov["status"] == "partial" and cov["missing"] == ["T3"]
