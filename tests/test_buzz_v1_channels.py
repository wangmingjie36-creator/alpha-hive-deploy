"""buzz_v1 冻结通道层与桥接核对（v0.45.583，根治阶段 2）

背景
----
buzz_v1 冻结了「通道值 → 分」，通道值仍是生产算的。阶段 2 把「原始观测 → 7 个通道」也冻结（`buzz_v1_channels`，逐字抄窗口起点
10-12 的代码 + 已确认的测量修正），`buzz_v1.bridge()` 逐日核对冻结通道与入档通道逐位相等。两组各 ≥ 10 个通过日、0 个失败日
才进阶段 4（用户 10-20 定）。

本文件钉住
----------
1. 纯度：冻结层不 import 任何生产代码、不读 config。
2. 照抄的常数与生产一致；生产先改了却没登记 ⇒ 开发期红（阶段 4 之前；之后生产与冻结版按设计分开）。
3. 数值复刻：numpy 成对求和 / pandas mean / std 逐位相同。
4. **差分对拍**：同一批随机原始输入，生产代码（经观测层落档）与冻结层（从档案读回）逐位相同——Reddit / 新闻三源 / 热搜 / F&G /
   行情（实时含末根判断、补跑）/ BuzzBee 的通道映射。
5. 桥接：通过 / 失败 / 不全 / 未存 / 整分对不上的判定；满 10 天才 ready；接进维度 IC 进度行与 Step 11 外壳。
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import math
import random
import sqlite3
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import buzz_v1 as B
import buzz_v1_channels as fc
import observation_archive as oa
import observation_layer as ol
from bars_integrity import trading_days

_ROOT = Path(__file__).resolve().parent.parent
D = "2026-10-21"


@pytest.fixture
def on():
    oa.enable(D, "live")


def _resp(data, ok=True, status_code=200):
    return types.SimpleNamespace(ok=ok, status_code=status_code, json=lambda: data, raise_for_status=lambda: None)


def _load(sha):
    return oa.load(sha)


# ══════════════════════════════════════════════════════════════════════════
# 1. 纯度
# ══════════════════════════════════════════════════════════════════════════

class TestPurity:
    def test_frozen_layer_imports_nothing_from_production(self):
        tree = ast.parse((_ROOT / "buzz_v1_channels.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert mods <= {"__future__", "datetime", "math", "typing"}, (
            f"冻结通道层不许依赖生产代码 / config / pandas / numpy：{sorted(mods - {'__future__', 'datetime', 'math', 'typing'})}")


# ══════════════════════════════════════════════════════════════════════════
# 2. 照抄的常数（阶段 4 前：与生产不一致 = 生产改了却没登记）
# ══════════════════════════════════════════════════════════════════════════

class TestConstantsMatchProduction:
    def test_keywords_and_dedup(self):
        import config
        assert set(config.SENTIMENT_KEYWORDS["bullish"]) == fc.BULLISH_KWS
        assert set(config.SENTIMENT_KEYWORDS["bearish"]) == fc.BEARISH_KWS
        assert config.NEWS_FILTER_CONFIG["dedup_jaccard_threshold"] == fc.DEDUP_JACCARD

    def test_channel_thresholds(self):
        import config
        vt, lt = config.AGENT_SCORING["volume_thresholds"], config.AGENT_SCORING["volatility_thresholds"]
        assert tuple(th for th, _v in fc.VOLUME_STEPS) == (vt["very_high"], vt["high"], vt["normal"], vt["low"])
        assert tuple(th for th, _v in fc.VOLATILITY_STEPS) == (lt["extreme"], lt["high"], lt["moderate"])

    def test_news_source_constants(self):
        import newsapi_client as nc
        assert (nc._AV_BULL_THRESHOLD, nc._AV_BEAR_THRESHOLD) == (fc.AV_BULL_THRESHOLD, fc.AV_BEAR_THRESHOLD)
        assert nc._MASSIVE_LABELS == fc.MASSIVE_LABELS and nc._VALID_LABELS == set(fc.VALID_LABELS)

    def test_session_last_minute_every_day_2020_2035(self):
        import data_pipeline as dp
        d = dt.date(2020, 1, 1)
        while d <= dt.date(2035, 12, 31):
            assert fc.session_last_minute(d) == dp._session_last_minute(d), d
            d += dt.timedelta(days=1)

    def test_window_lo_matches(self):
        import data_pipeline as dp
        d = dt.date(2024, 1, 1)
        while d <= dt.date(2028, 12, 31):
            assert fc._window_lo(d) == dp._volatility_20d_window_lo(d), d
            d += dt.timedelta(days=1)

    def test_event_row_columns(self):
        """v0.45.645 公司行动空行的判据列（逐例行为由下面的差分对拍与 tests/test_event_only_rows.py 钉住）。"""
        import price_metrics as pm
        assert (fc.EVENT_ROW_PRICE_COLUMNS, fc.EVENT_ROW_EVENT_COLUMNS) == (pm.EVENT_ROW_PRICE_COLUMNS,
                                                                            pm.EVENT_ROW_EVENT_COLUMNS)


# ══════════════════════════════════════════════════════════════════════════
# 3. 数值复刻
# ══════════════════════════════════════════════════════════════════════════

class TestNumerics:
    def test_pairwise_sum_is_numpy_add_reduce(self):
        R = random.Random(1)
        for _ in range(1500):
            a = [R.uniform(-1, 1) * R.choice([1e-3, 1.0, 1e6, 1e9]) for _ in range(R.randint(1, 300))]
            assert fc._np_sum(a) == float(np.add.reduce(np.array(a)))

    def test_mean_and_std_are_pandas(self):
        R = random.Random(2)
        for _ in range(1500):
            n = R.randint(2, 45)
            vols = [R.uniform(1e5, 9e7) if R.random() > 0.15 else None for _ in range(n)]
            s = pd.Series([np.nan if v is None else v for v in vols])
            m = fc._nanmean(vols)
            assert (math.isnan(m) and math.isnan(s.mean())) or m == s.mean()
            rs = [R.uniform(-0.08, 0.08) for _ in range(n)]
            assert fc._std(rs) == pd.Series(rs).std()
            ints = [R.randint(0, 900_000_000) for _ in range(n)]
            assert fc._nanmean(ints) == pd.Series(np.array(ints, dtype="int64")).mean()


# ══════════════════════════════════════════════════════════════════════════
# 4. 差分对拍：生产代码（经观测层落档） vs 冻结层（从档案读回）
# ══════════════════════════════════════════════════════════════════════════

POOL = ["NVDA", "TSLA", "AMD", "PLTR", "AAPL", "MSFT", "META", "AMZN", "GME", "AMC", "SOFI", "RKLB", "BRK-B"]
WEIRD = [None, 0, -3, 1.5, "abc", True, 7]
WORDS = ["surge", "beat", "record", "growth", "rally", "decline", "fall", "miss", "probe", "lawsuit", "upgrade",
         "downgrade", "chip", "ai", "demand", "guidance", "the", "a", "on", "after", "high", "strong", "weak", "cut",
         "raise", "bullish", "bearish", "crash", "soar", "record high", "risk", "gains"]


def _num(R, lo, hi):
    c = R.random()
    if c < 0.08:
        return R.choice(WEIRD)
    return R.randint(lo, hi) if c < 0.5 else round(R.uniform(lo, hi), R.choice([0, 1, 3]))


def _rows(R):
    out = []
    for i, t in enumerate(R.sample(POOL, R.randint(0, len(POOL)))):
        row = {"ticker": t if R.random() > 0.05 else t.lower(), "rank": i + 1 if R.random() > 0.1 else R.choice(WEIRD),
               "mentions": _num(R, 0, 400), "upvotes": _num(R, 0, 3000)}
        if R.random() > 0.1:
            row["mentions_24h_ago"] = _num(R, 0, 400)
        out.append(row)
    return out


@pytest.fixture
def reddit(monkeypatch, tmp_path):
    import resilience
    import reddit_sentiment as rs
    monkeypatch.setattr(rs, "reddit_limiter", types.SimpleNamespace(acquire=lambda *a, **k: True))
    monkeypatch.setattr(resilience, "reddit_breaker", types.SimpleNamespace(
        allow_request=lambda: True, record_success=lambda: None, record_failure=lambda: None))
    box = {"lists": {}}

    def get(url, timeout=None):
        v = box["lists"][url.split("/filter/")[1].split("/")[0]]
        if isinstance(v, Exception):
            raise v
        return _resp({"results": v})
    monkeypatch.setattr(rs, "get_session", lambda src: types.SimpleNamespace(get=get))
    box["mod"], box["tmp"] = rs, tmp_path
    return box


class TestDifferentialReddit:
    def test_random_lists(self, reddit, on, monkeypatch):
        from swarm_agents.cache import _safe_score
        R = random.Random(3)
        n = 0
        for k in range(120):
            reddit["lists"] = {"all-stocks": _rows(R) if R.random() > 0.08 else ConnectionError("x"),
                               "wallstreetbets": _rows(R) if R.random() > 0.08 else ConnectionError("y")}
            monkeypatch.setattr(reddit["mod"], "CACHE_DIR", reddit["tmp"] / f"rc{k}")
            c = reddit["mod"].RedditSentimentClient()
            for t in POOL + ["ZZZZ"]:
                r = c.get_ticker_sentiment(t)
                prod = _safe_score(r.get("sentiment_score"), 5.0, 0, 10, "reddit_score") * 10
                blk = oa.buzz_refs(reddit=r, news=None, yahoo=None, fear_greed=None, price=None)
                lists = {f: _load(s)["payload"]["results"] for f, s in blk["reddit"]["ref"].items()}
                assert fc.reddit_signal(t, lists["all-stocks"], lists["wallstreetbets"]) == prod, (t, lists)
                n += 1
        assert n > 1500


def _title(R):
    return " ".join(R.choice(WORDS) for _ in range(R.randint(1, 9))) if R.random() > 0.05 else ""


@pytest.fixture
def news(monkeypatch, tmp_path):
    import newsapi_client as nc
    monkeypatch.setattr(nc, "_primary_run_stats", {"status": {}, "messages": {}})
    monkeypatch.setattr(nc, "_news_limiter", types.SimpleNamespace(acquire=lambda **kw: True))
    monkeypatch.setattr(nc, "_massive_limiter", types.SimpleNamespace(acquire=lambda timeout: True))
    monkeypatch.setattr(nc, "_load_av_key", lambda: "AVKEY")
    monkeypatch.setattr(nc, "_load_massive_key", lambda: "MKEY")
    monkeypatch.setattr(nc, "_news_breaker", None)
    box = {"bodies": {}}

    def get(url, **kw):
        return box["bodies"]["massive" if "massive.com" in url else "av" if "alphavantage" in url else "yf"]
    monkeypatch.setattr(nc, "get_session", lambda src: types.SimpleNamespace(get=get))
    box["mod"], box["tmp"], box["mp"] = nc, tmp_path, monkeypatch
    yield box
    nc._av_daily.update(date="", count=0)


def _massive_body(R, t):
    res = []
    for i in range(R.randint(1, 10)):
        ins = ([{"ticker": t if R.random() > 0.1 else t.replace("-", "."),
                 "sentiment": R.choice(["positive", "negative", "neutral", "mixed", None])}] if R.random() > 0.15 else [])
        res.append({"title": _title(R), "publisher": R.choice([{"name": "P"}, None]),
                    "description": R.choice([_title(R), None, ""]), "published_utc": f"2026-10-20T11:0{i % 10}Z",
                    "article_url": f"https://e/{i}", "insights": ins + [{"ticker": "OTHER", "sentiment": "negative"}]})
    return {"status": R.choice(["OK", "DELAYED"]), "results": res}


def _av_body(R, t):
    return {"feed": [{"title": _title(R), "source": "WSJ", "summary": _title(R), "time_published": "20261020T100000",
                      "url": f"https://e/av{i}",
                      "ticker_sentiment": [{"ticker": t, "ticker_sentiment_score": R.choice(
                          [str(round(R.uniform(-0.6, 0.6), 4)), "nan", "abc", "0.15", "-0.15"])}]}
                     for i in range(R.randint(1, 10))]}


def _yf_body(R):
    return {"news": [{"title": _title(R), "publisher": "X", "summary": _title(R),
                      "providerPublishTime": 1709900000 + i, "link": f"https://e/{i}"} for i in range(R.randint(1, 10))]}


class TestDifferentialNews:
    @pytest.mark.parametrize("primary", ["massive", "alpha_vantage", "none"])
    def test_random_payloads(self, news, on, primary):
        from swarm_agents.cache import _safe_score
        nc = news["mod"]
        news["mp"].setattr(nc, "_news_cfg", lambda: {**nc._NEWS_CFG_DEFAULT, "primary": primary})
        R = random.Random({"massive": 4, "alpha_vantage": 5, "none": 6}[primary])
        n = 0
        for k in range(250):
            t = R.choice(POOL)
            news["bodies"] = {"massive": _resp(_massive_body(R, t)), "av": _resp(_av_body(R, t)),
                              "yf": _resp(_yf_body(R))}
            news["mp"].setattr(nc, "_CACHE_DIR", news["tmp"] / f"nc{k}")
            nc._av_daily.update(date="", count=0)
            r = nc.get_ticker_news(t, max_articles=R.choice([8, 10]))
            prod = (_safe_score(r.get("sentiment_score"), default=5.0, lo=0.0, hi=10.0, label="x") * 10
                    if r.get("is_real_data") and r.get("total_articles", 0) >= 3 else 50)
            if not r.get("is_real_data"):
                continue
            doc = _load(r[oa.REF_KEY])
            assert fc.news_signal(doc["kind"], doc["payload"]) == prod, (doc["kind"], doc["payload"])
            n += 1
        assert n > 150


class TestNewsEdgeCases:
    """随机用例碰不到的两处：类份额票的 insight 写法、标题截断对去重的影响。生产与冻结层同一份 payload 对拍。"""

    def _both(self, news, t, results):
        from swarm_agents.cache import _safe_score
        nc = news["mod"]
        news["mp"].setattr(nc, "_news_cfg", lambda: {**nc._NEWS_CFG_DEFAULT, "primary": "massive"})
        news["mp"].setattr(nc, "_CACHE_DIR", news["tmp"] / f"edge_{t}_{len(results)}")
        news["bodies"] = {"massive": _resp({"status": "OK", "results": results}), "av": _resp({}), "yf": _resp({})}
        r = nc.get_ticker_news(t, max_articles=10)
        prod = _safe_score(r.get("sentiment_score"), default=5.0, lo=0.0, hi=10.0, label="x") * 10 \
            if r.get("is_real_data") and r.get("total_articles", 0) >= 3 else 50
        doc = _load(r[oa.REF_KEY])
        return prod, fc.news_signal(doc["kind"], doc["payload"])

    def test_class_share_insight_written_with_a_dot(self, news, on):
        """BRK-B 的 insight 写 BRK.B（Massive 的写法）：要按请求用的写法也认。"""
        res = [{"title": f"Distinct headline number {i} {w}", "description": "d",
                "insights": [{"ticker": "BRK.B", "sentiment": "positive"}]}
               for i, w in enumerate(("alpha", "beta", "gamma", "delta"))]
        prod, frozen = self._both(news, "BRK-B", res)
        assert prod == frozen and prod == 100.0, "前提：四篇都按 BRK.B 认成看多"

    def test_long_titles_dedupe_on_the_first_200_chars(self, news, on):
        """前 100 字相同、100–200 字不同的两个标题：按 200 字截断不算重复，按 100 字截断就算——计数跟着变。"""
        common, i = "", 0
        while len(common) <= 100:                                     # 公共前缀 > 100 字
            common, i = (common + " " if common else "") + f"common{i}", i + 1
        a = common + " " + " ".join(f"alpha{i}" for i in range(12))
        b = common + " " + " ".join(f"bravo{i}" for i in range(12))
        assert len(a) > 150 and a[:100] == b[:100] and a[:200] != b[:200]
        res = [{"title": a, "description": "", "insights": [{"ticker": "NVDA", "sentiment": "positive"}]},
               {"title": b, "description": "", "insights": [{"ticker": "NVDA", "sentiment": "positive"}]},
               {"title": "Unrelated story one", "description": "", "insights": [{"ticker": "NVDA", "sentiment": "negative"}]},
               {"title": "Unrelated story two here", "description": "",
                "insights": [{"ticker": "NVDA", "sentiment": "negative"}]}]
        prod, frozen = self._both(news, "NVDA", res)
        assert prod == frozen == 55.0, "前提：两篇长标题都保留（2 看多 / 4 篇）"


class TestDifferentialTrendingAndFearGreed:
    def test_trending(self, on, monkeypatch, tmp_path):
        import yahoo_trending as yt
        R = random.Random(7)
        for k in range(200):
            syms = R.sample(POOL, R.randint(1, len(POOL)))
            monkeypatch.setattr(yt, "_CACHE_PATH", tmp_path / f"yt{k}.json")
            monkeypatch.setattr(yt, "get_session", lambda src, _s=syms: types.SimpleNamespace(
                get=lambda url, **kw: _resp({"finance": {"result": [{"quotes": [{"symbol": s} for s in _s]}]}})))
            for t in POOL + ["ZZZZ"]:
                r = yt.get_ticker_attention(t)
                assert fc.yahoo_signal(t, _load(r[oa.REF_KEY])["payload"]["symbols"]) == r["attention_score"] * 10

    def test_fear_greed(self, on, monkeypatch, tmp_path):
        import fear_greed as fgm
        R = random.Random(8)
        for k in range(400):
            cnn_up = R.random() > 0.2
            score = R.choice([R.uniform(0, 100), R.randint(0, 100) + 0.5, 50.0])
            cnn = _resp({"fear_and_greed": {"score": score, "rating": R.choice(["fear", "greed", "x"])}}) if cnn_up \
                else _resp({}, ok=False, status_code=500)
            alt = _resp({"data": [{"value": str(R.randint(0, 100)), "value_classification": "Greed"}]})
            monkeypatch.setattr(fgm, "_CACHE_PATH", tmp_path / f"fg{k}.json")
            monkeypatch.setattr(fgm, "get_session", lambda src, _c=cnn, _a=alt: types.SimpleNamespace(
                get=lambda url, **kw: _c if "cnn.io" in url else _a))
            r = fgm.get_fear_greed()
            doc = _load(r[oa.REF_KEY])
            assert fc.fear_greed_signal(doc["kind"], doc["payload"]) == r["sentiment_score"] * 10


def _frame(R, last, n):
    days = trading_days(last - dt.timedelta(days=70), last)[-n:] if n else []
    closes = [R.uniform(20, 400) for _ in days]
    df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes,
                       "Volume": np.array([R.randint(0, 5_000_000) for _ in days], dtype=R.choice(["int64", "float64"])),
                       "Dividends": [0.0] * len(days), "Stock Splits": [0.0] * len(days)},
                      index=pd.DatetimeIndex(days, name="Date").tz_localize("America/New_York"))
    if len(df) and R.random() < 0.15:
        df.iloc[R.randrange(len(df)), 3] = np.nan
    if len(df) and R.random() < 0.08:
        df["Volume"] = df["Volume"].astype("float64")
        df.iloc[R.randrange(len(df)), 4] = np.nan
    return _with_event_rows(R, df)


def _with_event_rows(R, df):
    """v0.45.645：随机掺进 yfinance 公司行动空行（开高低收全空、量 0、分红 / 拆股非零）——首行外的边界日、窗口中间 / 末根把真 K 线
    换掉——以及形似而不是的行（没有事件 / 量不为 0 / 只缺收盘），生产与冻结层必须逐例同判。"""
    if not len(df):
        return df
    px = ["Open", "High", "Low", "Close"]
    if R.random() < 0.15:          # 窗口外边界日的分红漏进来（实测 META 09-21 的形状）
        row = pd.DataFrame({c: [np.nan] for c in px} | {"Volume": [0], "Dividends": [R.choice([0.21, 0.525, 1.05])],
                                                         "Stock Splits": [0.0]},
                           index=pd.DatetimeIndex([df.index[0] - pd.Timedelta(days=1)], name="Date"))
        df = pd.concat([row.astype(df.dtypes.to_dict()), df])
    for _ in range(R.choice([0, 0, 0, 1, 2])):
        i = R.randrange(len(df))
        kind = R.choice(["div", "div", "split", "no_event", "volume", "close_only", "div_nan"])
        df.iloc[i, [df.columns.get_loc(c) for c in (px if kind != "close_only" else ["Close"])]] = np.nan
        df.iloc[i, df.columns.get_loc("Volume")] = R.randint(1, 9) if kind == "volume" else 0
        if kind in ("div", "volume", "close_only"):
            df.iloc[i, df.columns.get_loc("Dividends")] = R.choice([0.21, 0.525, 1.05])
        elif kind == "split":
            df.iloc[i, df.columns.get_loc("Stock Splits")] = R.choice([2.0, 0.5])
        elif kind == "div_nan":     # 事件列是 NaN：不算事件（两层都按「有限非零」判）
            df.iloc[i, df.columns.get_loc("Dividends")] = np.nan
    return df


@pytest.fixture
def yfstub(monkeypatch):
    import yfinance
    box = {"frame": None}

    class _T:
        def __init__(self, *_a, **_k):
            pass

        def history(self, **_k):
            return box["frame"]
    monkeypatch.setattr(yfinance, "Ticker", _T)
    return box


class TestDifferentialPrice:
    def test_live_paths(self, yfstub, on, monkeypatch):
        import data_pipeline as dp
        import price_metrics as pm
        R = random.Random(9)
        n = 0
        cover = {"event_first": 0, "event_tail": 0, "lookalike": 0}   # v0.45.645：公司行动空行真的被对拍到了
        for _ in range(500):
            last = R.choice(trading_days(dt.date(2026, 6, 1), dt.date(2026, 12, 31)))
            yfstub["frame"] = _frame(R, last, R.choice([3, 4, 5, 6, 18, 19, 20, 21, 22, 23]))
            _f = yfstub["frame"]
            _pos = pm.event_only_positions(_f)
            cover["event_first"] += 0 in _pos
            cover["event_tail"] += any(p >= len(_f) - 20 and p > 0 for p in _pos)
            cover["lookalike"] += int(_f["Close"].isna().sum()) > len(_pos)
            d = last if R.random() < 0.8 else last - dt.timedelta(days=1)
            clock = R.choice([None, pd.Timestamp(dt.datetime.combine(d, dt.time(*R.choice(
                [(9, 45), (11, 30), (12, 59), (13, 0), (15, 30), (15, 59)]))), tz="America/New_York")])
            monkeypatch.setattr(dp, "_exchange_now", lambda _c=clock: _c)
            for prod in (dp._fetch_history_metrics("NVDA"), dp.YFinanceSource().fetch("NVDA")):
                if prod is None:
                    continue
                if isinstance(prod, dict):
                    refs, vals = prod["_observations"], prod
                else:
                    refs = prod.observations
                    vals = {k: getattr(prod, k) for k in ("momentum_5d", "volume_ratio", "volatility_20d")}
                got = fc.price_inputs({"price": {"expected": True, "ref": refs}}, _load)
                for k in ("momentum_5d", "volume_ratio", "volatility_20d"):
                    a, b = got[k], vals.get(k)
                    assert (a is None and b is None) or (a is not None and b is not None and (
                        (math.isnan(a) and math.isnan(b)) or a == b)), (k, a, b)
                n += 1
        assert n > 600
        assert min(cover.values()) >= 30, cover

    def test_backfill_path(self, yfstub, on, monkeypatch):
        import data_pipeline as dp
        R = random.Random(10)
        n = 0
        for _ in range(200):
            as_of = R.choice(trading_days(dt.date(2026, 6, 1), dt.date(2026, 10, 16)))
            df = _frame(R, as_of, R.choice([30, 44]))
            yfstub["frame"] = df
            out = dp._fetch_historical_stock_data("NVDA", as_of.isoformat())
            if out.get("_data_unavailable") or "snapshot_close" in out["observations"]:
                continue
            got = fc.price_inputs({"price": {"expected": True, "ref": out["observations"]}}, _load)
            for k in ("momentum_5d", "volume_ratio", "volatility_20d"):
                a, b = got[k], out[k]
                assert (a is None and b is None) or (math.isnan(a) and math.isnan(b)) or a == b, (k, a, b)
            n += 1
        assert n > 100


@pytest.fixture
def buzz(all_agents, monkeypatch):
    import fear_greed
    import newsapi_client
    import reddit_sentiment
    import yahoo_trending
    from swarm_agents import cache as _cache
    box = {"stock": {}}
    monkeypatch.setattr(reddit_sentiment, "get_reddit_sentiment", lambda t: {"sentiment_score": 6.2, "rank": 7})
    monkeypatch.setattr(newsapi_client, "get_ticker_news", lambda t, max_articles=10: {
        "is_real_data": True, "total_articles": 5, "sentiment_score": 6.43, "source": "massive"})
    monkeypatch.setattr(yahoo_trending, "get_ticker_attention", lambda t: {"is_real_data": True, "attention_score": 4.5})
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: {"is_real_data": True, "sentiment_score": 3.2,
                                                               "value": 32, "source": "cnn"})
    monkeypatch.setattr(_cache, "_fetch_stock_data", lambda t, target_date=None: {"price": 100.0, **box["stock"]})
    b = all_agents["buzz"]
    b._prefetched_stock = {}
    b._target_date = D
    box["bee"] = b
    return box


class TestDifferentialChannelMapping:
    @pytest.mark.parametrize("m, vr, v20", [
        (None, None, None), (1.2345, 1.2, 30.0), (12.0, 2.5, 70.0), (-14.0, 0.3, 10.0), (float("nan"), float("nan"), 40.0),
        (10.0, 2.0, 60.0), (-10.0, 1.5, 20.0), (0.0, 1.0, 40.0000001), (3.3, 0.5, 19.999), (-3.1, 0.5000001, 60.0001),
    ])
    def test_buzz_components_equal_frozen_mapping(self, buzz, m, vr, v20):
        buzz["stock"] = {"momentum_5d": m, "volume_ratio": vr, "volatility_20d": v20}
        comp = buzz["bee"].analyze("NVDA")["details"]["components"]
        assert comp["momentum_signal"] == fc.momentum_channel(m)
        assert comp["volume_signal"] == fc.volume_channel(vr)
        assert comp["volatility_signal"] == fc.volatility_channel(v20)


# ══════════════════════════════════════════════════════════════════════════
# 5. 桥接核对
# ══════════════════════════════════════════════════════════════════════════

def _write_obs():
    """一只票一天的全套原始观测（直接落档）→ 溯源块。"""
    lists = {"all-stocks": [{"ticker": "NVDA", "rank": 2, "mentions": 120, "upvotes": 700, "mentions_24h_ago": 80}],
             "wallstreetbets": [{"ticker": "NVDA", "rank": 4, "mentions": 50, "upvotes": 300}]}
    reddit = {f: oa.reuse("apewisdom.ranking", {"filter": f, "results": v}) for f, v in lists.items()}
    news = oa.reuse("news.massive", {"ticker": "NVDA", "symbol": "NVDA", "max_articles": 10, "results": [
        {"title": t, "description": "d", "insights": [{"ticker": "NVDA", "sentiment": s}]}
        for t, s in (("Chipmaker raises guidance", "positive"), ("Regulators open probe", "negative"),
                     ("Analysts split on valuation", "neutral"), ("New datacenter partnership", "positive"))]})
    yahoo = oa.reuse("yahoo.trending", {"symbols": ["TSLA", "NVDA"]})
    fgr = oa.reuse("fear_greed.cnn", {"fear_and_greed": {"score": 32.4, "rating": "fear"}})
    R = random.Random(11)
    frame = _frame(R, dt.date(2026, 10, 20), 22).assign(Close=lambda f: [100 + i for i in range(len(f))])
    daily = oa.reuse(ol.KIND_DAILY, ol.frame_payload("NVDA", {"period": "1mo"}, frame))
    clock = oa.reuse(ol.KIND_CLOCK, {"spy_last_minute": "2026-10-20T15:59:00-04:00"})
    return {"_layer": oa.LAYER_VERSION,
            "reddit": {"expected": True, "ref": reddit}, "news": {"expected": True, "ref": news},
            "yahoo": {"expected": True, "ref": yahoo}, "fear_greed": {"expected": True, "ref": fgr},
            "price": {"expected": True, "ref": {"yf_daily": daily, "exchange_clock": clock}}}


def _seed_day(db, date, block, tweak=None, score_tweak=0.0):
    """按冻结层的结果造一天的入档（= 生产与冻结版一致的情形），可选改坏某个通道 / 整分。"""
    rec = fc.channels_from_observations("NVDA", block, _load)
    s, p = rec["sentiment"], rec["price"]
    comps = {} if "incomplete" in s else {c: s[c] for c in fc.SENTIMENT_CHANNELS}
    if "incomplete" not in p:
        comps.update({c: p[c] for c in fc.PRICE_CHANNELS})
    if tweak:
        comps.update(tweak)
    details = {"components": comps, "observations": block,
               "volume_ratio": p.get("volume_ratio"), "volatility_20d": p.get("volatility_20d")}
    import signal_archive
    signal_archive.archive({"NVDA": {"agent_details": {"BuzzBeeWhisper": {"details": details},
                                                       "ScoutBeeNova": {"details": {"momentum_5d": p.get("momentum_5d")}}}}},
                           date, db_path=db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS predictions (date TEXT, ticker TEXT, dimension_scores TEXT)")
    if len(comps) == 7:
        sc = B.score(comps, p["momentum_5d"], None)["score"] + score_tweak
        con.execute("INSERT INTO predictions VALUES (?,?,?)", (date, "NVDA", json.dumps({"sentiment": sc})))
    con.commit()
    con.close()


class TestBridge:
    def test_all_equal_passes_both_groups_and_the_score(self, on, tmp_path):
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        r = B.bridge(str(db), end="2026-10-21")
        assert r["ok"] and [g["days"][0]["status"] for g in r["groups"].values()] == ["pass", "pass"]
        assert r["score"] == {"n": 1, "n_match": 1, "n_skipped": 0}

    @pytest.mark.parametrize("tweak, group", [({"news_signal": 61.0}, "sentiment"), ({"volatility_signal": 40}, "price")])
    def test_a_different_channel_fails_its_group(self, on, tmp_path, tweak, group):
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs(), tweak=tweak)
        r = B.bridge(str(db), end="2026-10-21")
        assert not r["ok"] and r["groups"][group]["n_fail"] == 1 and r["groups"][group]["first_fail"] == "2026-10-21"
        (m,) = [x for x in r["mismatches"] if x["group"] == group]
        assert m["diffs"][0]["field"] == next(iter(tweak))

    def test_a_different_raw_volatility_fails_price(self, on, tmp_path, monkeypatch):
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        con = sqlite3.connect(db)
        con.execute("UPDATE signal_archive SET value = value + 1e-12 WHERE signal = 'price.volatility_20d'")
        con.commit()
        con.close()
        r = B.bridge(str(db), end="2026-10-21")
        assert r["groups"]["price"]["n_fail"] == 1, "原始字段也逐位核（不是只核分档后的通道）"

    def test_score_mismatch_is_not_ok(self, on, tmp_path):
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs(), score_tweak=0.01)
        r = B.bridge(str(db), end="2026-10-21")
        assert not r["ok"] and r["score"] == {"n": 1, "n_match": 0, "n_skipped": 0}

    def test_missing_payload_is_incomplete_not_fail(self, on, tmp_path):
        db = tmp_path / "p.db"
        blk = _write_obs()
        _seed_day(db, "2026-10-21", blk)
        oa.payload_path(blk["news"]["ref"]).unlink()
        r = B.bridge(str(db), end="2026-10-21")
        assert r["ok"] and r["groups"]["sentiment"]["days"][0]["status"] == "incomplete"
        assert r["groups"]["price"]["days"][0]["status"] == "pass"
        assert "不在档" in r["incomplete_examples"][0]["why"]

    def test_price_gap_is_incomplete(self, on, tmp_path):
        db = tmp_path / "p.db"
        blk = _write_obs()
        blk["price"] = {"expected": True, "ref": None, "gaps": ["td_adjusted"]}
        _seed_day(db, "2026-10-21", blk)
        r = B.bridge(str(db), end="2026-10-21")
        assert r["groups"]["price"]["days"][0]["status"] == "incomplete"
        assert "td_adjusted" in r["incomplete_examples"][0]["why"]

    def test_scan_before_price_observations_is_not_archived(self, on, tmp_path):
        """10-20 那种：v0.45.580 之前的扫描没有 price 溯源块 ⇒ 不算不全、不计数。"""
        db = tmp_path / "p.db"
        blk = _write_obs()
        del blk["price"]
        _seed_day(db, "2026-10-20", blk)
        r = B.bridge(str(db), end="2026-10-20")
        assert r["groups"]["price"]["days"][0]["status"] == "not_archived"
        assert r["groups"]["price"]["n_incomplete"] == 0 and r["groups"]["sentiment"]["n_pass"] == 1

    def test_ready_after_ten_passing_days(self, on, tmp_path):
        db = tmp_path / "p.db"
        days = [d.isoformat() for d in trading_days(dt.date(2026, 10, 21), dt.date(2026, 11, 30))[:10]]
        for d in days[:9]:
            _seed_day(db, d, _write_obs())
        assert not B.bridge(str(db), end=days[-1])["ready"]
        _seed_day(db, days[-1], _write_obs())
        r = B.bridge(str(db), end=days[-1])
        assert r["ready"] and all(g["n_pass"] == 10 for g in r["groups"].values())

    def test_ten_passes_with_one_failure_is_not_ready(self, on, tmp_path):
        """满 10 个通过日但有过一个失败日 ⇒ 不 ready：失败必须查清，不能被后来的通过日「稀释」掉。"""
        db = tmp_path / "p.db"
        days = [d.isoformat() for d in trading_days(dt.date(2026, 10, 21), dt.date(2026, 11, 30))[:11]]
        _seed_day(db, days[0], _write_obs(), tweak={"news_signal": 61.0})
        for d in days[1:]:
            _seed_day(db, d, _write_obs())
        r = B.bridge(str(db), end=days[-1])
        g = r["groups"]["sentiment"]
        assert g["n_pass"] == 10 and g["n_fail"] == 1 and not g["ready"] and not r["ready"]

    def test_no_links_yet_is_a_note(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(db).close()
        r = B.bridge(str(db), end=D)
        assert r["ok"] and not r.get("ready") and r["note"]

    def test_frozen_layer_raising_is_a_failure_not_silence(self, on, tmp_path, monkeypatch):
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())

        def boom(*a, **k):
            raise AttributeError("'NoneType' object has no attribute 'upper'")
        monkeypatch.setattr(fc, "channels_from_observations", boom)
        r = B.bridge(str(db), end="2026-10-21")
        assert not r["ok"] and r["errors"] and r["groups"]["sentiment"]["n_fail"] == 1


class TestBridgeSecondCheck:
    """v0.45.588 二次检查补的四处：覆盖自己核、空转不算通过、入档缺项按隔离分、读错误不当「没有数据」。"""

    def _prod_only(self, db, date, ticker, sentiment=5.0):
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE IF NOT EXISTS predictions (date TEXT, ticker TEXT, dimension_scores TEXT)")
        con.execute("INSERT INTO predictions VALUES (?,?,?)", (date, ticker, json.dumps({"sentiment": sentiment})))
        con.commit()
        con.close()

    def test_scored_ticker_without_links_makes_the_day_incomplete(self, on, tmp_path):
        """NVDA 有溯源、AMD 生产有分却没写进溯源表 ⇒ 那天两组都「不全」，不能只看 NVDA 就判通过。"""
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        self._prod_only(db, "2026-10-21", "AMD")
        r = B.bridge(str(db), end="2026-10-21")
        assert [g["days"][0]["status"] for g in r["groups"].values()] == ["incomplete", "incomplete"]
        assert any(x["ticker"] == "AMD" and "没有溯源块" in x["why"] for x in r["incomplete_examples"])

    def test_archived_channels_without_links_also_count(self, on, tmp_path):
        """AMD 入档了通道（`buzz.comp.*`）却没有溯源块、也没有生产分（如预测写库失败那天）⇒ 照样记「不全」。"""
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        con = sqlite3.connect(db)
        con.execute("INSERT INTO signal_archive (date, ticker, signal, value) VALUES ('2026-10-21', 'AMD', "
                    "'buzz.comp.news_signal', 55.0)")
        con.commit()
        con.close()
        r = B.bridge(str(db), end="2026-10-21")
        assert r["groups"]["sentiment"]["days"][0]["status"] == "incomplete"
        assert any(x["ticker"] == "AMD" for x in r["incomplete_examples"])

    def test_a_scan_day_with_no_links_at_all_shows_up(self, on, tmp_path):
        """第一个有溯源的扫描日之后，某天生产有分、溯源表一行都没有（如开档没接上）⇒ 那天照样出现、记「不全」，不是消失。"""
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        self._prod_only(db, "2026-10-22", "NVDA")
        r = B.bridge(str(db), end="2026-10-22")
        days = r["groups"]["sentiment"]["days"]
        assert [x["date"] for x in days] == ["2026-10-21", "2026-10-22"] and days[1]["status"] == "incomplete"

    def test_a_day_with_nothing_observed_is_vacuous(self, on, tmp_path):
        """整天数据源全挂：四个情绪通道都不该有观测、都回中性 ⇒ 冻结层与生产都是 50、全等，但什么也没核到 ⇒「空转」不计入通过日。"""
        db = tmp_path / "p.db"
        blk = _write_obs()
        for ch in ("reddit", "news", "yahoo", "fear_greed"):
            blk[ch] = {"expected": False, "ref": None}
        _seed_day(db, "2026-10-21", blk)
        r = B.bridge(str(db), end="2026-10-21")
        g = r["groups"]["sentiment"]
        assert g["days"][0]["status"] == "vacuous" and g["n_pass"] == 0 and g["n_vacuous"] == 1
        assert r["groups"]["price"]["days"][0]["status"] == "pass", "前提：行情组照常有观测可核"

    def test_archived_scalar_missing_without_quarantine_is_a_mismatch(self, on, tmp_path):
        """入档没有量比、冻结层算出了一个值：没登记隔离 ⇒ 不等（此前只在入档有值时才比，量比同档时漏）。"""
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        con = sqlite3.connect(db)
        con.execute("DELETE FROM signal_archive WHERE signal = 'price.volume_ratio'")
        con.commit()
        con.close()
        r = B.bridge(str(db), end="2026-10-21")
        assert r["groups"]["price"]["n_fail"] == 1
        assert r["mismatches"][0]["diffs"][0]["field"] == "price.volume_ratio"

    def test_quarantined_cells_are_skipped_not_failed(self, on, tmp_path, monkeypatch):
        import signal_archive
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        con = sqlite3.connect(db)
        con.execute("DELETE FROM signal_archive WHERE signal IN ('price.volume_ratio', 'buzz.comp.news_signal')")
        con.commit()
        con.close()
        monkeypatch.setattr(signal_archive, "QUARANTINE", [
            {"date": "2026-10-21", "signals": ["price.volume_ratio", "buzz.comp.news_signal"], "tickers": ["NVDA"],
             "reason": "test"}])
        r = B.bridge(str(db), end="2026-10-21")
        assert r["groups"]["price"]["days"][0]["status"] == "pass", "隔离的原始字段跳过"
        assert r["groups"]["sentiment"]["days"][0]["status"] == "incomplete", "隔离的通道没法对 ⇒ 不全，不算失败"
        assert "已登记隔离" in r["incomplete_examples"][0]["why"]

    def test_read_error_is_an_error_not_no_data(self, on, tmp_path):
        """溯源表在、但读不出（这里用缺列模拟坏库）⇒ 报 error；只有「表还没建」才是「还没有带溯源的扫描日」。"""
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute(f"CREATE TABLE {oa.LINKS_TABLE} (date TEXT, ticker TEXT, consumer TEXT)")
        con.commit()
        con.close()
        r = B.bridge(str(db), end=D)
        assert r["ok"] is False and "refs" in r["error"] and "note" not in r
        with pytest.raises(sqlite3.OperationalError):
            oa.load_links(db, D)

    def test_corrupt_link_json_is_an_error(self, on, tmp_path):
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        con = sqlite3.connect(db)
        con.execute(f"UPDATE {oa.LINKS_TABLE} SET refs = '{{not json'")
        con.commit()
        con.close()
        r = B.bridge(str(db), end="2026-10-21")
        assert r["ok"] is False and r["error"].startswith("JSONDecodeError")

    def test_score_rows_skipped_for_a_missing_reference_are_counted(self, on, tmp_path):
        """参照日（≥ 锚点）有 sentiment.pct 却没有通道 ⇒ 整分对不了；照实计 `n_skipped`，不静默少一行。"""
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs())
        con = sqlite3.connect(db)
        con.execute("INSERT INTO signal_archive (date, ticker, signal, value) VALUES ('2026-10-16', 'NVDA', 'sentiment.pct', 55)")
        con.commit()
        con.close()
        r = B.bridge(str(db), end="2026-10-21")
        assert r["score"] == {"n": 0, "n_match": 0, "n_skipped": 1}


class TestBridgeWiring:
    def test_executor_summary_has_only_counts(self, on, tmp_path):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dimfx", _ROOT / "experiments" / "dim_ic_forward_test.py")
        fx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fx)
        db = tmp_path / "p.db"
        _seed_day(db, "2026-10-21", _write_obs(), tweak={"news_signal": 61.0})
        br = fx.buzz_v1_bridge(db, "2026-10-21")
        assert set(br) <= set(fx._BRIDGE_KEYS) | {"groups", "score", "n_errors"}
        assert br["ok"] is False and br["groups"]["sentiment"]["n_fail"] == 1
        line = fx.status_line({"status": "not_ready", "truncation": {}, "buzz_v1_bridge": br, "h1_weeks": 0,
                               "next_look_at": 26, "forward_start": "2026-10-12"})
        assert line.startswith("⚠️") and "桥接核对" in line and "首次 2026-10-21" in line
        ok = fx.status_line({"status": "not_ready", "truncation": {}, "h1_weeks": 0, "next_look_at": 26,
                             "forward_start": "2026-10-12",
                             "buzz_v1_bridge": {**br, "ok": True, "groups": {
                                 "sentiment": {"n_pass": 1, "n_fail": 0}, "price": {"n_pass": 0, "n_fail": 0}}}})
        assert ok.startswith("⏳") and "情绪 1/10" in ok

    def test_readiness_lists_a_failed_bridge(self):
        import ic_rerun_readiness as rr
        assert "buzz_v1_bridge" in rr._DIM_DETAIL_KEYS
        d = {"status": "not_ready", "forward_start": "2026-10-12", "h1_anchor": {"state": "ok"},
             "input_code_guard": {"ok": True}, "buzz_v1_self_proof": {"ok": True},
             "buzz_v1_bridge": {"ok": False, "groups": {"price": {"n_fail": 2, "first_fail": "2026-10-22"}},
                                "score": {"n": 3, "n_match": 3}, "n_errors": 0}}
        (it,) = [a for a in rr._dim_ic_attention(d) if a["id"].endswith("buzz_v1_bridge")]
        assert it["level"] == "warn" and "price 失败 2 天" in it["message"]
