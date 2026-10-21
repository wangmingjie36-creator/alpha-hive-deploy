"""H1 原始观测档案（v0.45.568，buzz_v1 根治阶段 1）

背景
----
buzz_v1 冻结的只是「通道值 → 分」；通道由生产代码从外部数据算出，而生产把取数与解释写在一起、只存解释后的数字 ⇒
「这条边界只动了冻结层」只能靠人声明。根治（用户 2026-10-20 定）：原始观测在取数那一刻原样落档，buzz_v1 向下冻结到
通道层、从档案重算。本版只做落档 + 溯源，**不改任何值**。

本文件钉住
----------
1. 档案本身：内容寻址（同内容同地址、写过不重写）、只在扫描进程落盘、请求参数去密钥、读回校验地址、绝不抛进取数代码。
2. 四个取数模块：结果带所用观测的地址（缓存命中也带）、失败记事件；**存下的内容足够重算**——拿档案里的 payload
   当网络响应重跑同一个模块，结果逐字段相同（阶段 2 冻结通道层的前提）。
3. BuzzBee：`details["observations"]` 四个通道的 expected / ref；有没有地址都不改分数与通道值。
4. 链接表与 buzz.comp.* 同一事务、同日重扫后者覆盖前者；定向回填不碰。
5. 完整性统计（缺链接 / payload 不在档 / 没开档 / 落盘失败）与 P2 告警；`main()` 开扫前开档、收尾写 scan_timing。
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import types

import pytest

import alert_manager as am
import observation_archive as oa

D = "2026-10-21"
_AV_KEY = "AVSECRETKEY_0123456789"
_M_KEY = "MASSIVEKEY_abcdef0123456789xyzQ"


def _resp(data, ok=True, status_code=200):
    return types.SimpleNamespace(ok=ok, status_code=status_code, json=lambda: data, raise_for_status=lambda: None)


def _files(base):
    return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()) if base.exists() else []


def _events():
    p = oa.events_path(D)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _strip(d):
    """比较结果时去掉每次都变的时间戳与本版新增的溯源键。"""
    return {k: v for k, v in d.items() if k not in ("timestamp", oa.REF_KEY)}


@pytest.fixture
def on():
    oa.enable(D, "live")


# ══════════════════════════════════════════════════════════════════════════
# 1. 档案本身
# ══════════════════════════════════════════════════════════════════════════

class TestAddress:
    def test_content_addressed_and_key_order_free(self):
        a = oa.address("yahoo.trending", {"symbols": ["NVDA", "TSLA"]})
        assert a == oa.address("yahoo.trending", {"symbols": ["NVDA", "TSLA"]})
        assert oa.address("fear_greed.cnn", {"fear_and_greed": {"score": 1, "rating": "x"}}) == \
            oa.address("fear_greed.cnn", {"fear_and_greed": {"rating": "x", "score": 1}})
        assert a != oa.address("yahoo.trending", {"symbols": ["TSLA", "NVDA"]}), "名次是内容的一部分"
        assert oa.is_address(a)

    def test_kind_is_part_of_the_address(self):
        p = {"filter": "all-stocks", "results": []}
        with pytest.raises(ValueError):
            oa.address("apewisdom.nope", p)

    def test_kinds_registry_covers_every_kind_the_modules_use(self):
        """模块里写到的每个 kind 都在登记处；登记处里没有没人用的 kind（死登记会让阶段 2 白写一种解析）。"""
        import re
        from pathlib import Path
        root = Path(oa.__file__).parent
        used = set()
        for fn in ("reddit_sentiment.py", "newsapi_client.py", "yahoo_trending.py", "fear_greed.py"):
            src = (root / fn).read_text(encoding="utf-8")
            used |= set(re.findall(r'_obs\.(?:observe|reuse|fetch_failed)\(\s*"([a-z_.]+)"', src))
            if 'f"news.{primary}"' in src:
                used |= {"news.massive", "news.alpha_vantage"}
        # v0.45.580：观测层的取数用常量 KIND_* 写 kind
        import observation_layer as ol
        src = (root / "observation_layer.py").read_text(encoding="utf-8")
        consts = set(re.findall(r'_archive\.(?:observe|reuse|fetch_failed)\(\s*(KIND_[A-Z]+)', src))
        assert consts, "前提：观测层确实经 KIND_* 常量调档案"
        used |= {getattr(ol, c) for c in consts}
        assert used == set(oa.KINDS), (used ^ set(oa.KINDS))


class TestWriting:
    def test_disabled_process_computes_the_address_but_writes_nothing(self):
        sha = oa.observe("yahoo.trending", {"symbols": ["NVDA"]}, request={"url": "u"})
        assert oa.is_address(sha)
        assert not oa.root().exists(), "非扫描进程（MCP / Alpha Bot / 测试）不落盘"

    def test_enabled_writes_payload_once_and_an_event(self, on):
        payload = {"symbols": ["NVDA", "AAPL"]}
        sha = oa.observe("yahoo.trending", payload, request={"url": "u", "params": {"count": 25}})
        sha2 = oa.observe("yahoo.trending", {"symbols": ["NVDA", "AAPL"]}, request={"url": "u"})
        assert sha == sha2
        assert oa.load(sha) == {"kind": "yahoo.trending", "payload": payload}
        assert _files(oa.root()) == [f"events/{D}.jsonl", f"payloads/{sha[:2]}/{sha}.json"]
        st = oa.stats()
        assert (st["payloads_written"], st["events"], st["failures"]) == (1, 2, 0)
        ev = _events()
        assert [e["status"] for e in ev] == ["ok", "ok"] and ev[0]["sha"] == sha
        assert ev[0]["request"] == {"url": "u", "params": {"count": 25}} and ev[0]["mode"] == "live"

    def test_reuse_archives_a_payload_another_process_cached(self, on):
        """缓存是别的进程填的（当时没开档）：扫描里命中它时补档，不记事件（不是这次取的）。"""
        sha = oa.reuse("apewisdom.ranking", {"filter": "all-stocks", "results": [{"ticker": "NVDA"}]})
        assert oa.load(sha)["payload"]["results"] == [{"ticker": "NVDA"}]
        assert _events() == []

    def test_existing_file_is_not_rewritten(self, on):
        payload = {"symbols": ["X"]}
        sha = oa.address("yahoo.trending", payload)
        path = oa.payload_path(sha)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"kind": "yahoo.trending", "payload": payload}), encoding="utf-8")
        before = path.stat().st_mtime_ns
        oa.reuse("yahoo.trending", payload)
        assert path.stat().st_mtime_ns == before
        assert oa.stats()["payloads_present"] == 1 and oa.stats()["payloads_written"] == 0

    def test_failed_fetch_is_an_event_without_payload(self, on):
        oa.fetch_failed("news.massive", "server_refused", request={"url": "u"}, message="x" * 999)
        (ev,) = _events()
        assert ev["sha"] is None and ev["status"] == "server_refused" and len(ev["message"]) == 300
        assert not (oa.root() / "payloads").exists()

    def test_secrets_never_reach_the_archive(self, on):
        oa.observe("news.alpha_vantage", {"ticker": "NVDA", "max_articles": 10, "feed": []},
                   request={"url": "u", "params": {"apikey": _AV_KEY, "tickers": "NVDA"},
                            "headers": {"Authorization": f"Bearer {_M_KEY}"}, "api_token": _M_KEY})
        blob = "".join(p.read_text(encoding="utf-8") for p in oa.root().rglob("*") if p.is_file())
        assert _AV_KEY not in blob and _M_KEY not in blob
        assert _events()[0]["request"] == {"url": "u", "params": {"tickers": "NVDA"}, "headers": {}}

    def test_tampered_payload_is_refused_on_read(self, on):
        import ledger_io
        sha = oa.observe("yahoo.trending", {"symbols": ["NVDA"]})
        p = oa.payload_path(sha)
        p.write_text(json.dumps({"kind": "yahoo.trending", "payload": {"symbols": ["TSLA"]}}), encoding="utf-8")
        with pytest.raises(ledger_io.LedgerCorrupt):
            oa.load(sha)
        assert oa.load("0" * 64) is None

    def test_enable_rejects_bad_date_and_mode(self):
        with pytest.raises(ValueError):
            oa.enable("2026-13-01", "live")
        with pytest.raises(ValueError):
            oa.enable(D, "replay")
        assert not oa.is_enabled()


class TestNeverRaises:
    def test_unserializable_payload_returns_none_and_counts(self, on):
        assert oa.observe("fear_greed.cnn", {"fear_and_greed": {"score": float("nan")}}) is None
        assert oa.stats()["failures"] == 1 and "address:fear_greed.cnn" in oa.stats()["failure_samples"][0]

    def test_unknown_kind_on_failure_path_counts(self, on):
        oa.fetch_failed("news.unknown_primary", "unknown_primary")
        assert oa.stats()["failures"] == 1 and _events() == []

    def test_write_failure_is_counted_not_raised(self, on, monkeypatch):
        import ledger_io

        def boom(*a, **k):
            raise ledger_io.LedgerWriteError("disk full")
        monkeypatch.setattr(ledger_io, "write_json", boom)
        sha = oa.observe("yahoo.trending", {"symbols": ["NVDA"]})
        assert oa.is_address(sha), "地址照给：链接在、payload 不在 ⇒ summarize 数成 missing_payloads"
        assert oa.stats()["failures"] == 1


# ══════════════════════════════════════════════════════════════════════════
# 2. 四个取数模块：溯源 + 存下的内容足够重算
# ══════════════════════════════════════════════════════════════════════════

_ALL = [{"ticker": "TSLA", "rank": 1, "mentions": 300, "upvotes": 2000, "mentions_24h_ago": 250},
        {"ticker": "NVDA", "rank": 2, "mentions": 120, "upvotes": 700, "mentions_24h_ago": 80},
        {"ticker": "AMD", "rank": 9, "mentions": 40, "upvotes": 30, "mentions_24h_ago": None}]
_WSB = [{"ticker": "NVDA", "rank": 4, "mentions": 50, "upvotes": 300, "mentions_24h_ago": 40}]


@pytest.fixture
def reddit(monkeypatch, tmp_path):
    import resilience
    import reddit_sentiment as rs
    monkeypatch.setattr(rs, "CACHE_DIR", tmp_path / "reddit_cache")
    monkeypatch.setattr(rs, "reddit_limiter", types.SimpleNamespace(acquire=lambda *a, **k: True))
    monkeypatch.setattr(resilience, "reddit_breaker", types.SimpleNamespace(
        allow_request=lambda: True, record_success=lambda: None, record_failure=lambda: None))
    lists = {"all-stocks": _ALL, "wallstreetbets": _WSB}
    calls = []

    def get(url, timeout=None):
        name = url.split("/filter/")[1].split("/")[0]
        calls.append(name)
        v = lists[name]
        if isinstance(v, Exception):
            raise v
        return _resp({"results": v})
    monkeypatch.setattr(rs, "get_session", lambda src: types.SimpleNamespace(get=get))
    return types.SimpleNamespace(mod=rs, lists=lists, calls=calls, cache=tmp_path / "reddit_cache")


class TestReddit:
    def test_result_carries_both_ranking_addresses(self, reddit, on):
        r = reddit.mod.RedditSentimentClient().get_ticker_sentiment("NVDA")
        refs = r[oa.REF_KEY]
        assert refs == {f: oa.address("apewisdom.ranking", {"filter": f, "results": reddit.lists[f]})
                        for f in oa.REDDIT_FILTERS}
        assert oa.load(refs["all-stocks"])["payload"]["results"] == _ALL
        assert [e["status"] for e in _events()] == ["ok", "ok"]

    def test_quiet_and_failed_lists_still_have_addresses(self, reddit, on):
        """没上榜（安静）与整张榜失败（空榜）都由那两张榜决定 ⇒ 都有地址；失败另记事件。"""
        reddit.lists["wallstreetbets"] = ConnectionError("boom")
        r = reddit.mod.RedditSentimentClient().get_ticker_sentiment("ZZZZ")
        assert r["rank"] is None and r["sentiment_score"] == 5.0
        assert r[oa.REF_KEY]["wallstreetbets"] == oa.address("apewisdom.ranking",
                                                             {"filter": "wallstreetbets", "results": []})
        assert sorted(e["status"] for e in _events()) == ["network_error", "ok"]

    def test_cache_filled_by_a_non_scan_process_is_archived_on_use(self, reddit):
        """MCP 等进程（没开档）先填了榜单缓存；随后扫描进程命中缓存 ⇒ 地址相同、payload 补进档案。"""
        c = reddit.mod.RedditSentimentClient()
        c.get_ticker_sentiment("NVDA")
        assert not oa.root().exists()
        for f in reddit.cache.glob("*_sentiment.json"):
            f.unlink()                      # 只留榜单缓存，逼它重算这只票
        oa.enable(D, "live")
        n_calls = len(reddit.calls)
        r = c.get_ticker_sentiment("NVDA")
        assert len(reddit.calls) == n_calls, "前提：命中缓存，没再取数"
        for sha in r[oa.REF_KEY].values():
            assert oa.load(sha) is not None
        assert _events() == []

    def test_per_ticker_cache_hit_returns_the_same_addresses(self, reddit, on):
        c = reddit.mod.RedditSentimentClient()
        assert c.get_ticker_sentiment("NVDA")[oa.REF_KEY] == c.get_ticker_sentiment("NVDA")[oa.REF_KEY]

    @pytest.mark.parametrize("ticker", ["NVDA", "TSLA", "AMD", "ZZZZ"])
    def test_archived_payloads_suffice_to_recompute(self, reddit, on, tmp_path, monkeypatch, ticker):
        first = reddit.mod.RedditSentimentClient().get_ticker_sentiment(ticker)
        replay = {f: oa.load(sha)["payload"]["results"] for f, sha in first[oa.REF_KEY].items()}
        monkeypatch.setattr(reddit.mod, "CACHE_DIR", tmp_path / "replay_cache")
        reddit.lists.update(replay)
        again = reddit.mod.RedditSentimentClient().get_ticker_sentiment(ticker)
        assert _strip(again) == _strip(first) and again[oa.REF_KEY] == first[oa.REF_KEY]


def _massive_body(n=4):
    titles = ["Chipmaker raises guidance after record quarter", "Regulators open probe into supply contracts",
              "Analysts split on valuation ahead of earnings", "New datacenter partnership announced Tuesday"]
    labs = ["positive", "negative", "neutral", None]
    return {"status": "OK", "request_id": "r1", "results": [
        {"title": titles[i], "publisher": {"name": "P"}, "description": "d", "published_utc": "2026-10-20T11:08:00Z",
         "article_url": f"https://e/m{i}",
         "insights": ([{"ticker": "NVDA", "sentiment": labs[i], "sentiment_reasoning": "r"}] if labs[i] else [])
                     + [{"ticker": "OTHER", "sentiment": "negative", "sentiment_reasoning": "r"}]}
        for i in range(n)]}


_YF_NEWS = {"news": [
    {"title": "NVDA stock surges on AI demand", "publisher": "Reuters", "summary": "NVIDIA reported strong growth",
     "providerPublishTime": 1709900000, "link": "https://e/1"},
    {"title": "NVDA announces new chip", "publisher": "Bloomberg", "summary": "New GPU architecture",
     "providerPublishTime": 1709890000, "link": "https://e/2"},
    {"title": "Market decline amid recession fears", "publisher": "CNBC", "summary": "Stocks fall",
     "providerPublishTime": 1709880000, "link": "https://e/3"}]}
_AV_FEED = {"feed": [
    {"title": f"NVDA item {i} {w}", "source": "WSJ", "summary": "s", "time_published": "20261020T100000",
     "url": f"https://e/av{i}", "ticker_sentiment": [{"ticker": "NVDA", "ticker_sentiment_score": s}]}
    for i, (s, w) in enumerate((("0.35", "alpha"), ("-0.25", "beta gamma"), ("0.05", "delta epsilon zeta")))]}


@pytest.fixture
def news(monkeypatch, tmp_path):
    import newsapi_client as nc
    monkeypatch.setattr(nc, "_CACHE_DIR", tmp_path / "news_cache")
    monkeypatch.setattr(nc, "_primary_run_stats", {"status": {}, "messages": {}})
    monkeypatch.setattr(nc, "_news_limiter", types.SimpleNamespace(acquire=lambda **kw: True))
    monkeypatch.setattr(nc, "_massive_limiter", types.SimpleNamespace(acquire=lambda timeout: True))
    monkeypatch.setattr(nc, "_load_av_key", lambda: _AV_KEY)
    monkeypatch.setattr(nc, "_load_massive_key", lambda: _M_KEY)
    monkeypatch.setattr(nc, "_news_breaker", None)
    nc._av_daily.update(date="", count=0)
    bodies = {"massive": _resp(_massive_body()), "av": _resp(_AV_FEED), "yf": _resp(_YF_NEWS)}

    def get(url, **kw):
        key = "massive" if "massive.com" in url else "av" if "alphavantage" in url else "yf"
        return bodies[key]
    monkeypatch.setattr(nc, "get_session", lambda src: types.SimpleNamespace(get=get))

    def primary(name):
        monkeypatch.setattr(nc, "_news_cfg", lambda: {**nc._NEWS_CFG_DEFAULT, "primary": name})
    primary("massive")
    yield types.SimpleNamespace(mod=nc, bodies=bodies, primary=primary, cache=tmp_path / "news_cache")
    nc._av_daily.update(date="", count=0)


class TestNews:
    def test_massive_result_points_at_the_raw_results(self, news, on):
        r = news.mod.get_ticker_news("NVDA", max_articles=10)
        assert r["source"] == "massive"
        doc = oa.load(r[oa.REF_KEY])
        assert doc["kind"] == "news.massive"
        assert doc["payload"] == {"ticker": "NVDA", "symbol": "NVDA", "max_articles": 10,
                                  "results": _massive_body()["results"]}
        (ev,) = _events()
        assert ev["request"]["params"] == {"ticker": "NVDA", "limit": 10, "order": "desc", "sort": "published_utc"}

    def test_cache_hit_keeps_the_address(self, news, on):
        a = news.mod.get_ticker_news("NVDA")
        assert news.mod.get_ticker_news("NVDA")[oa.REF_KEY] == a[oa.REF_KEY]
        assert len(_events()) == 1, "缓存命中不是一次取数"

    def test_primary_refused_falls_back_to_yahoo_and_both_are_recorded(self, news, on):
        news.bodies["massive"] = _resp({"status": "ERROR", "error": "plan"}, ok=False, status_code=429)
        r = news.mod.get_ticker_news("NVDA")
        assert r["source"] == "yahoo_finance"
        assert oa.load(r[oa.REF_KEY])["kind"] == "news.yahoo_finance"
        assert [(e["kind"], e["status"]) for e in _events()] == [("news.massive", "server_refused"),
                                                                ("news.yahoo_finance", "ok")]

    def test_nothing_anywhere_is_two_failure_events_and_no_address(self, news, on):
        news.bodies["massive"] = _resp({"status": "OK", "results": []})
        news.bodies["yf"] = _resp({"news": []})
        r = news.mod.get_ticker_news("NVDA")
        assert not r["is_real_data"] and oa.REF_KEY not in r
        assert [(e["kind"], e["status"]) for e in _events()] == [("news.massive", "empty_feed"),
                                                                ("news.yahoo_finance", "no_data")]

    def test_av_rollback_path_never_archives_the_key(self, news, on):
        news.primary("alpha_vantage")
        r = news.mod.get_ticker_news("NVDA")
        assert r["source"] == "alpha_vantage" and oa.load(r[oa.REF_KEY])["kind"] == "news.alpha_vantage"
        blob = "".join(p.read_text(encoding="utf-8") for p in oa.root().rglob("*") if p.is_file())
        assert _AV_KEY not in blob and _M_KEY not in blob
        assert "apikey" not in _events()[0]["request"]["params"]

    def test_av_module_strips_the_key_itself(self, news, on, monkeypatch):
        """去密钥有两道：模块自己不传 apikey、档案再按键名兜底。只靠第二道 = 哪天改了兜底规则就漏——第一道单独钉住。"""
        seen = []
        real = oa.observe
        monkeypatch.setattr(news.mod._obs, "observe", lambda kind, payload, request=None: (
            seen.append(request), real(kind, payload, request=request))[1])
        news.primary("alpha_vantage")
        news.mod.get_ticker_news("NVDA")
        assert seen and _AV_KEY not in json.dumps(seen)

    @pytest.mark.parametrize("primary, body_key, kind, wrap", [
        ("massive", "massive", "news.massive", lambda p: {"status": "OK", "results": p["results"]}),
        ("alpha_vantage", "av", "news.alpha_vantage", lambda p: {"feed": p["feed"]}),
        ("yahoo_only", "yf", "news.yahoo_finance", lambda p: {"news": p["news"]}),
    ])
    def test_archived_payloads_suffice_to_recompute(self, news, on, tmp_path, monkeypatch,
                                                    primary, body_key, kind, wrap):
        news.primary(primary)
        first = news.mod.get_ticker_news("NVDA")
        doc = oa.load(first[oa.REF_KEY])
        assert doc["kind"] == kind
        monkeypatch.setattr(news.mod, "_CACHE_DIR", tmp_path / "replay_cache")
        news.bodies[body_key] = _resp(wrap(doc["payload"]))
        again = news.mod.get_ticker_news("NVDA")
        assert _strip(again) == _strip(first) and again[oa.REF_KEY] == first[oa.REF_KEY]


@pytest.fixture
def trending(monkeypatch, tmp_path):
    import yahoo_trending as yt
    monkeypatch.setattr(yt, "_CACHE_PATH", tmp_path / "yahoo_trending.json")
    box = {"resp": _resp({"finance": {"result": [{"quotes": [{"symbol": s} for s in
                                                              ("TSLA", "NVDA", "AMD", "PLTR")]}]}})}
    monkeypatch.setattr(yt, "get_session", lambda src: types.SimpleNamespace(get=lambda url, **kw: box["resp"]))
    return types.SimpleNamespace(mod=yt, box=box, cache=tmp_path / "yahoo_trending.json")


class TestYahooTrending:
    @pytest.mark.parametrize("ticker", ["NVDA", "ZZZZ"])
    def test_listed_and_unlisted_both_point_at_the_list(self, trending, on, ticker):
        r = trending.mod.get_ticker_attention(ticker)
        assert oa.load(r[oa.REF_KEY])["payload"] == {"symbols": ["TSLA", "NVDA", "AMD", "PLTR"]}

    def test_unavailable_has_no_address_and_a_failure_event(self, trending, on):
        trending.box["resp"] = _resp({}, ok=False, status_code=503)
        r = trending.mod.get_ticker_attention("NVDA")
        assert not r["is_real_data"] and oa.REF_KEY not in r
        assert [(e["status"], e["message"]) for e in _events()] == [("http_error", "HTTP 503")]

    def test_cache_hit_from_another_process_is_archived(self, trending):
        trending.mod.get_ticker_attention("NVDA")           # 没开档：只填缓存
        assert not oa.root().exists()
        oa.enable(D, "live")
        trending.box["resp"] = None                          # 再取就炸：证明下面是缓存命中
        r = trending.mod.get_ticker_attention("NVDA")
        assert oa.load(r[oa.REF_KEY]) is not None and _events() == []

    def test_archived_payload_suffices_to_recompute(self, trending, on, tmp_path, monkeypatch):
        first = trending.mod.get_ticker_attention("AMD")
        syms = oa.load(first[oa.REF_KEY])["payload"]["symbols"]
        monkeypatch.setattr(trending.mod, "_CACHE_PATH", tmp_path / "replay.json")
        trending.box["resp"] = _resp({"finance": {"result": [{"quotes": [{"symbol": s} for s in syms]}]}})
        again = trending.mod.get_ticker_attention("AMD")
        assert _strip(again) == _strip(first)


_CNN = {"fear_and_greed": {"score": 32.4571, "rating": "fear", "timestamp": "2026-10-20T20:59:58+00:00",
                           "previous_close": 35.1}, "fear_and_greed_historical": {"data": [1, 2, 3]}}
_ALT = {"data": [{"value": "61", "value_classification": "Greed", "timestamp": "1760918400"}]}


@pytest.fixture
def fg(monkeypatch, tmp_path):
    import fear_greed as m
    monkeypatch.setattr(m, "_CACHE_PATH", tmp_path / "fear_greed.json")
    box = {"cnn": _resp(_CNN), "alt": _resp(_ALT)}
    monkeypatch.setattr(m, "get_session", lambda src: types.SimpleNamespace(
        get=lambda url, **kw: box["cnn"] if "cnn.io" in url else box["alt"]))
    return types.SimpleNamespace(mod=m, box=box)


class TestFearGreed:
    def test_cnn_object_is_archived_without_the_history_series(self, fg, on):
        r = fg.mod.get_fear_greed()
        assert r["source"] == "cnn"
        assert oa.load(r[oa.REF_KEY])["payload"] == {"fear_and_greed": _CNN["fear_and_greed"]}

    def test_cnn_down_crypto_fallback_is_its_own_kind(self, fg, on):
        fg.box["cnn"] = _resp({}, ok=False, status_code=500)
        r = fg.mod.get_fear_greed()
        assert r["source"] == "alternative_me"
        assert oa.load(r[oa.REF_KEY])["kind"] == "fear_greed.alternative_me"
        assert [(e["kind"], e["status"]) for e in _events()] == [("fear_greed.cnn", "unavailable"),
                                                                ("fear_greed.alternative_me", "ok")]

    def test_both_down_is_default_without_address(self, fg, on):
        fg.box["cnn"] = fg.box["alt"] = _resp({}, ok=False, status_code=500)
        r = fg.mod.get_fear_greed()
        assert not r["is_real_data"] and oa.REF_KEY not in r

    @pytest.mark.parametrize("cnn_up", [True, False])
    def test_archived_payload_suffices_to_recompute(self, fg, on, tmp_path, monkeypatch, cnn_up):
        if not cnn_up:
            fg.box["cnn"] = _resp({}, ok=False, status_code=500)
        first = fg.mod.get_fear_greed()
        doc = oa.load(first[oa.REF_KEY])
        monkeypatch.setattr(fg.mod, "_CACHE_PATH", tmp_path / "replay.json")
        if cnn_up:
            fg.box["cnn"] = _resp({"fear_and_greed": doc["payload"]["fear_and_greed"]})
        else:
            fg.box["alt"] = _resp({"data": [doc["payload"]["data"]]})
        again = fg.mod.get_fear_greed()
        assert _strip(again) == _strip(first)


# ══════════════════════════════════════════════════════════════════════════
# 3. BuzzBee 溯源块
# ══════════════════════════════════════════════════════════════════════════

_SHA = {k: oa.address("yahoo.trending", {"symbols": [k]}) for k in ("a", "w", "n", "y", "f", "d", "c")}


@pytest.fixture
def buzz(all_agents, monkeypatch):
    import fear_greed
    import newsapi_client
    import reddit_sentiment
    import yahoo_trending
    from swarm_agents import cache as _cache
    box = {"with_refs": True, "news_raises": False}

    def ref(d, v):
        return {**d, oa.REF_KEY: v} if box["with_refs"] else d

    def news(t, max_articles=10):
        if box["news_raises"]:
            raise ConnectionError("news down")
        return ref({"is_real_data": True, "total_articles": 5, "sentiment_score": 6.4321, "dominant_theme": "t",
                    "articles": [], "source": "massive"}, _SHA["n"])
    monkeypatch.setattr(reddit_sentiment, "get_reddit_sentiment", lambda t: ref(
        {"sentiment_score": 6.2371, "reddit_buzz": "hot", "mentions": 10, "rank": 7},
        {"all-stocks": _SHA["a"], "wallstreetbets": _SHA["w"]}))
    monkeypatch.setattr(newsapi_client, "get_ticker_news", news)
    monkeypatch.setattr(yahoo_trending, "get_ticker_attention", lambda t: ref(
        {"is_real_data": True, "attention_score": 4.5, "description": "d"}, _SHA["y"]))
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: ref(
        {"is_real_data": True, "sentiment_score": 3.2, "value": 32, "classification": "Fear", "source": "cnn"},
        _SHA["f"]))
    stock = {"price": 100.0, "momentum_5d": 1.2345, "volume_ratio": 1.2, "volatility_20d": 30.0}

    def fetch_stock(t, target_date=None):
        out = dict(stock)
        if box["with_refs"]:
            out["observations"] = {"yf_daily": _SHA["d"], "exchange_clock": _SHA["c"]}
        return out
    monkeypatch.setattr(_cache, "_fetch_stock_data", fetch_stock)
    b = all_agents["buzz"]
    b._prefetched_stock = {}
    b._target_date = D
    return types.SimpleNamespace(bee=b, box=box)


class TestBuzzDetails:
    def test_observations_block(self, buzz):
        obs = buzz.bee.analyze("NVDA")["details"]["observations"]
        assert obs == {"_layer": oa.LAYER_VERSION,
                       "reddit": {"expected": True, "ref": {"all-stocks": _SHA["a"], "wallstreetbets": _SHA["w"]}},
                       "news": {"expected": True, "ref": _SHA["n"]},
                       "yahoo": {"expected": True, "ref": _SHA["y"]},
                       "fear_greed": {"expected": True, "ref": _SHA["f"]},
                       "price": {"expected": True, "ref": {"yf_daily": _SHA["d"], "exchange_clock": _SHA["c"]}}}

    def test_addresses_do_not_change_any_value(self, buzz):
        """有没有溯源键，分数 / 方向 / 通道值 / 其余 details 逐位相同——本版只多存、不改值。"""
        a = buzz.bee.analyze("NVDA")
        buzz.box["with_refs"] = False
        b = buzz.bee.analyze("NVDA")
        assert (a["score"], a["direction"]) == (b["score"], b["direction"])
        da, db_ = dict(a["details"]), dict(b["details"])
        oa_, ob_ = da.pop("observations"), db_.pop("observations")
        assert da == db_
        ob_.pop("_layer")
        assert all(v["ref"] is None and v["expected"] for v in ob_.values()), "没地址 ⇒ 记成缺失，不是不该有"
        assert ob_["price"]["gaps"] == ["<无观测登记>"]

    def test_reddit_needs_both_list_addresses(self):
        """只有一张榜有地址 ⇒ 记成缺失（重算 Reddit 通道两张榜都要），不是「有一张就够」。"""
        r = oa.buzz_refs(reddit={oa.REF_KEY: {"all-stocks": _SHA["a"], "wallstreetbets": None}},
                         news=None, yahoo=None, fear_greed=None, price=None)
        assert r["reddit"] == {"expected": True, "ref": None}

    @pytest.mark.parametrize("ch", ["news", "yahoo", "fear_greed"])
    def test_fallback_result_is_not_expected_even_with_an_address(self, ch):
        """不是真实数据 ⇒ 通道就是中性 50，重算用不着观测：expected=False、也不抄地址（兜底结果里的地址不是它用的）。"""
        kw = {"reddit": None, "news": None, "yahoo": None, "fear_greed": None, "price": None,
              ch: {"is_real_data": False, oa.REF_KEY: _SHA["n"]}}
        assert oa.buzz_refs(**kw)[ch] == {"expected": False, "ref": None}

    def test_news_fetch_raising_means_not_expected(self, buzz):
        """取数抛异常时 news_ext 未绑定：通道是中性 50，不需要观测，也不能让 Buzz 因为溯源块崩掉。"""
        buzz.box["news_raises"] = True
        r = buzz.bee.analyze("NVDA")
        assert "error" not in r, r
        assert r["details"]["observations"]["news"] == {"expected": False, "ref": None}
        assert r["details"]["components"]["news_signal"] == 50


# ══════════════════════════════════════════════════════════════════════════
# 4. 链接表
# ══════════════════════════════════════════════════════════════════════════

def _tr(obs, comp=55.0):
    details = {"components": {c: comp for c in ("momentum_signal", "volume_signal", "volatility_signal",
                                                 "reddit_signal", "news_signal", "yahoo_signal",
                                                 "fear_greed_signal")}}
    if obs is not None:
        details["observations"] = obs
    return {"agent_details": {"BuzzBeeWhisper": {"details": details}}}


def _obs_block(tag="1"):
    s = oa.address("yahoo.trending", {"symbols": [tag]})
    return {"reddit": {"expected": True, "ref": {"all-stocks": s, "wallstreetbets": s}},
            "news": {"expected": True, "ref": s}, "yahoo": {"expected": True, "ref": s},
            "fear_greed": {"expected": False, "ref": None}}


class TestLinksTable:
    def test_written_with_the_channels_and_replaced_on_rescan(self, tmp_path):
        import signal_archive as sa
        db = tmp_path / "p.db"
        sa.archive({"NVDA": _tr(_obs_block("1"), 55.0), "OLD": _tr(None)}, D, db_path=db)
        assert oa.load_links(db, D) == {"NVDA": _obs_block("1")}, "没有溯源块的票（旧代码 / Buzz 出错）不写"
        sa.archive({"NVDA": _tr(_obs_block("2"), 61.0)}, D, db_path=db)
        assert oa.load_links(db, D) == {"NVDA": _obs_block("2")}
        con = sqlite3.connect(db)
        v = con.execute("SELECT value FROM signal_archive WHERE ticker='NVDA' AND signal='buzz.comp.news_signal'"
                        " AND date=?", (D,)).fetchone()[0]
        con.close()
        assert v == 61.0, "前提：通道值也是后者覆盖前者——链接与它同一轮"

    def test_targeted_backfill_leaves_links_alone(self, tmp_path):
        import signal_archive as sa
        db = tmp_path / "p.db"
        sa.archive({"NVDA": _tr(_obs_block("1"))}, D, db_path=db)
        sa.archive({"NVDA": _tr(_obs_block("2"))}, D, db_path=db, only=["buzz.comp.news_signal"])
        assert oa.load_links(db, D) == {"NVDA": _obs_block("1")}

    def test_missing_table_reads_empty(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(db).close()
        assert oa.load_links(db, D) == {}


# ══════════════════════════════════════════════════════════════════════════
# 5. 完整性统计、告警、接线
# ══════════════════════════════════════════════════════════════════════════

def _archived_block(on_disk=True):
    payload = {"symbols": ["NVDA"]}
    s = oa.reuse("yahoo.trending", payload) if on_disk else oa.address("yahoo.trending", {"symbols": ["GONE"]})
    return {"reddit": {"expected": True, "ref": {"all-stocks": s, "wallstreetbets": s}},
            "news": {"expected": True, "ref": s}, "yahoo": {"expected": True, "ref": s},
            "fear_greed": {"expected": False, "ref": None}}


class TestSummarize:
    def test_complete_day_is_ok(self, on):
        s = oa.summarize({"NVDA": _tr(_archived_block()), "ERR": {"agent_details": {"BuzzBeeWhisper": {"details": {}}}}})
        assert s["ok"] and s["n_buzz_error"] == 1 and s["expected"]["news"] == 1

    def test_missing_ref_is_not_ok(self, on):
        blk = _archived_block()
        blk["news"]["ref"] = None
        s = oa.summarize({"NVDA": _tr(blk)})
        assert not s["ok"] and s["missing_refs"]["news"] == 1 and "NVDA: news 无地址" in s["examples"]

    def test_payload_not_on_disk_is_not_ok(self, on):
        s = oa.summarize({"NVDA": _tr(_archived_block(on_disk=False))})
        assert not s["ok"] and s["missing_payloads"] == 4

    def test_buzz_without_block_is_not_ok(self, on):
        s = oa.summarize({"NVDA": _tr(None)})
        assert not s["ok"] and s["n_without_block"] == 1

    def test_not_enabled_is_not_ok(self):
        assert not oa.summarize({})["ok"]

    def test_reddit_needs_both_lists(self, on):
        blk = _archived_block()
        blk["reddit"]["ref"] = {"all-stocks": blk["news"]["ref"]}
        assert oa.summarize({"NVDA": _tr(blk)})["missing_refs"]["reddit"] == 1


def _status(oa_extra="__absent__", step2="success"):
    extra = {"gh_pages": {"success": True}}
    if oa_extra != "__absent__":
        extra["observation_archive"] = oa_extra
    return {"status": "success", "total_duration_seconds": 2000,
            "steps_result": {"step2_hive_analysis": {"status": step2}},
            "scan_timing": {"extra": extra, "production_sync": {"outcome": "up_to_date"}}}


def _alerts(tmp_path, status):
    a = am.AlertAnalyzer(report_dir=tmp_path)
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False))
    a.analyze(p)
    return [x for x in a.alerts if "原始观测档案" in x.message]


class TestAlert:
    def test_complete_is_silent(self, tmp_path):
        assert _alerts(tmp_path, _status({"available": True, "ok": True})) == []

    def test_incomplete_is_p2(self, tmp_path):
        (hit,) = _alerts(tmp_path, _status({"available": True, "ok": False, "enabled": True,
                                            "missing_refs": {"news": 2, "reddit": 0}, "missing_payloads": 1}))
        assert hit.level == am.AlertLevel.MEDIUM and hit.details["缺链接（通道: 只数）"] == {"news": 2}

    def test_absent_is_p2_not_silence(self, tmp_path):
        (hit,) = _alerts(tmp_path, _status())
        assert "未统计" in hit.message

    def test_early_exit_scan_is_not_judged(self, tmp_path):
        assert _alerts(tmp_path, _status(step2="failed")) == []


class TestMainWiring:
    def test_enabled_before_the_scan_and_reported_after(self):
        import alpha_hive_daily_report as m
        src = inspect.getsource(m.main)
        i_on = src.index("_obs_archive.enable(reporter.date_str")
        i_scan = src.index("reporter.run_swarm_scan(")
        assert i_on < i_scan, "开扫之后才开档 ⇒ 预取阶段的取数不落档"
        assert '"observation_archive": _observation_archive' in src
        assert src.index("_obs_archive.summarize(") > i_scan
