"""新闻通道：主源 Massive（v0.45.445）+ 降级可见化（v0.45.444）

背景
----
2026-10-06 起 Alpha Vantage 服务端日额度连续全拒，30 只的新闻全部降级到 Yahoo。降级本身不报错，
Buzz 的 `data_quality.news` 两种源都写 `keyword`；而 Yahoo 关键词打标与主源逐文章标签是两个分类器，
同票 news_signal 系统性低约 21–29 分。v0.45.444 让降级可见，v0.45.445 把主源换成 Massive
（原 Polygon.io，免费 5 次/分钟、无日上限；10-08 校准与 AV 时代水平相当）。

本文件钉住
----------
1. Massive 主源：insights 逐票标签映射、BRK-B → BRK.B、key 只走请求头、各失败出口降级 Yahoo 并记结局、
   限速名额等不到立即降级（不让 Buzz 整个超时）。
2. AV 回滚路径（`primary = "alpha_vantage"`）：444 的各出口结局照旧。
3. 只加记录、不改去向；真实 Buzz 的通道值 / DQ / 分数不受新键影响；`news_primary` 兼作世代边界印记。
4. 汇总口径（来源未知不进分母）与告警阈值（Massive 常态≈0，> 20% 报 P1）。
"""
from __future__ import annotations

import json
import re
import time
import types
from pathlib import Path

import pytest

import alert_manager as am

_KEY = "SECRETKEY1234567"
_MKEY = "MASSIVEKEY_abcdef0123456789xyzQ"
_AV_DAILY_MSG = ("Thank you for using Alpha Vantage! Our standard API rate limit is 25 requests per day. "
                 f"key={_KEY}")

_YF = {"news": [
    {"title": "NVDA stock surges on AI demand", "publisher": "Reuters",
     "summary": "NVIDIA reported strong growth", "providerPublishTime": 1709900000, "link": "https://e/1"},
    {"title": "NVDA announces new chip", "publisher": "Bloomberg",
     "summary": "New GPU architecture", "providerPublishTime": 1709890000, "link": "https://e/2"},
    {"title": "Market decline amid recession fears", "publisher": "CNBC",
     "summary": "Stocks fall", "providerPublishTime": 1709880000, "link": "https://e/3"},
]}
_AV_OK = {"feed": [
    {"title": f"NVDA item {i}", "source": "WSJ", "summary": "s", "time_published": "20261006T100000",
     "url": f"https://e/av{i}", "ticker_sentiment": [{"ticker": "NVDA", "ticker_sentiment_score": s}]}
    for i, s in enumerate(("0.35", "-0.25", "0.05"))
]}


#: 互不相似的标题——`_build_result` 按标题 Jaccard ≥ 0.5 去重，相似标题会被并掉
_TITLES = ["Chipmaker raises guidance after record quarter", "Regulators open probe into supply contracts",
           "Analysts split on valuation ahead of earnings", "New datacenter partnership announced Tuesday"]


def _massive(labels, ticker="NVDA"):
    """Massive 响应：每篇一个本票 insight（None ⇒ 只有别的票的 insight），外加一条别的票的干扰项。"""
    return {"status": "OK", "results": [
        {"title": _TITLES[i % len(_TITLES)], "publisher": {"name": "P"}, "description": "d",
         "published_utc": "2026-10-08T11:08:00Z", "article_url": f"https://e/m{i}",
         "insights": ([{"ticker": ticker, "sentiment": lab, "sentiment_reasoning": "r"}] if lab else [])
                     + [{"ticker": "OTHER", "sentiment": "negative", "sentiment_reasoning": "r"}]}
        for i, lab in enumerate(labels)]}


def _resp(data, ok=True, status_code=200):
    return types.SimpleNamespace(ok=ok, status_code=status_code, json=lambda: data,
                                 raise_for_status=lambda: None)


def _primary(monkeypatch, name):
    import newsapi_client as nc
    monkeypatch.setattr(nc, "_news_cfg", lambda: {**nc._NEWS_CFG_DEFAULT, "primary": name})


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    import newsapi_client as nc
    d = tmp_path / "news_cache"
    d.mkdir()
    monkeypatch.setattr(nc, "_CACHE_DIR", d)
    monkeypatch.setattr(nc, "_primary_run_stats", {"status": {}, "messages": {}})
    monkeypatch.setattr(nc, "_news_limiter", types.SimpleNamespace(acquire=lambda **kw: True))
    monkeypatch.setattr(nc, "_massive_limiter", types.SimpleNamespace(acquire=lambda timeout: True))
    monkeypatch.setattr(nc, "_load_av_key", lambda: _KEY)
    monkeypatch.setattr(nc, "_load_massive_key", lambda: _MKEY)
    _primary(monkeypatch, "massive")
    nc._av_daily["count"] = 0
    nc._av_daily["date"] = ""
    yield
    nc._av_daily["count"] = 0
    nc._av_daily["date"] = ""


def _route(monkeypatch, av=None, massive=None):
    """AV / Massive / Yahoo 走同一个 `get_session("newsapi")`：按 URL 分流。值是响应或可调用（可抛异常）。"""
    import newsapi_client as nc
    calls = {"av": 0, "massive": 0, "yf": 0, "massive_kw": []}

    def get(url, **kw):
        if "alphavantage" in url:
            calls["av"] += 1
            return av(url, **kw) if callable(av) else av
        if "massive.com" in url:
            calls["massive"] += 1
            calls["massive_kw"].append((url, kw))
            return massive(url, **kw) if callable(massive) else massive
        calls["yf"] += 1
        return _resp(_YF)

    session = types.SimpleNamespace(get=get)
    if nc._RESILIENCE_OK:
        monkeypatch.setattr(nc, "get_session", lambda source: session)
    else:
        monkeypatch.setattr(nc, "_req", session)
    return calls


# ══════════════════════════════════════════════════════════════════════════
# 1. Massive 主源
# ══════════════════════════════════════════════════════════════════════════

class TestMassivePrimary:

    def test_success_maps_insights_and_never_calls_av(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, av=_resp(_AV_OK), massive=_resp(_massive(["positive", "negative", "neutral"])))
        r = nc.get_ticker_news("NVDA")
        assert r["source"] == "massive"
        assert r["primary_attempt"] == {"source": "massive", "status": "ok"}
        assert (r["bullish_count"], r["bearish_count"], r["neutral_count"]) == (1, 1, 1)
        assert calls["av"] == 0 and calls["yf"] == 0
        assert nc.get_primary_run_stats()["status"] == {"ok": 1}

    def test_key_only_in_header(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, massive=_resp(_massive(["positive"] * 3)))
        nc.get_ticker_news("NVDA")
        url, kw = calls["massive_kw"][0]
        assert kw["headers"]["Authorization"] == f"Bearer {_MKEY}"
        assert _MKEY not in url and _MKEY not in json.dumps(kw.get("params"))

    def test_share_class_symbol_uses_dot(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, massive=_resp(_massive(["neutral"] * 3, ticker="BRK.B")))
        r = nc.get_ticker_news("BRK-B")
        assert calls["massive_kw"][0][1]["params"]["ticker"] == "BRK.B"
        assert r["source"] == "massive" and r["massive_no_insight"] == 0

    def test_article_without_this_tickers_insight_is_neutral_and_counted(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, massive=_resp(_massive(["positive", None, "positive"])))
        r = nc.get_ticker_news("NVDA")
        assert r["massive_no_insight"] == 1
        assert (r["bullish_count"], r["neutral_count"]) == (2, 1)

    def test_429_is_refusal_with_text_and_falls_back(self, monkeypatch):
        import newsapi_client as nc
        body = {"status": "ERROR", "error": "You've exceeded the maximum requests per minute"}
        _route(monkeypatch, massive=_resp(body, ok=False, status_code=429))
        r = nc.get_ticker_news("NVDA")
        assert r["source"] == "yahoo_finance"
        assert r["primary_attempt"] == {"source": "massive", "status": "server_refused",
                                        "message": "You've exceeded the maximum requests per minute"}

    def test_not_authorized_status_is_refusal(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, massive=_resp({"status": "NOT_AUTHORIZED", "message": "plan does not include"},
                                          ok=False, status_code=403))
        r = nc.get_ticker_news("NVDA")
        assert r["primary_attempt"]["status"] == "server_refused"
        assert r["primary_attempt"]["message"] == "plan does not include"

    def test_limiter_timeout_falls_back_without_calling(self, monkeypatch):
        import newsapi_client as nc
        monkeypatch.setattr(nc, "_massive_limiter", types.SimpleNamespace(acquire=lambda timeout: False))
        calls = _route(monkeypatch, massive=_resp(_massive(["positive"] * 3)))
        r = nc.get_ticker_news("NVDA")
        assert calls["massive"] == 0 and r["source"] == "yahoo_finance"
        assert r["primary_attempt"] == {"source": "massive", "status": "limiter_timeout"}

    def test_no_key(self, monkeypatch):
        import newsapi_client as nc
        monkeypatch.setattr(nc, "_load_massive_key", lambda: None)
        calls = _route(monkeypatch, massive=_resp(_massive(["positive"] * 3)))
        r = nc.get_ticker_news("NVDA")
        assert r["primary_attempt"] == {"source": "massive", "status": "no_key"} and calls["massive"] == 0

    def test_network_error_is_redacted(self, monkeypatch):
        import newsapi_client as nc

        def boom(url, **kw):
            raise ConnectionError(f"SSLError while talking to api.massive.com (token {_MKEY})")

        _route(monkeypatch, massive=boom)
        a = nc.get_ticker_news("NVDA")["primary_attempt"]
        assert a["status"] == "network_error" and _MKEY not in a["message"] and "***" in a["message"]

    def test_empty_results(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, massive=_resp({"status": "OK", "results": []}))
        assert nc.get_ticker_news("NVDA")["primary_attempt"] == {"source": "massive", "status": "empty_feed"}


class TestSlidingWindowLimiter:

    def test_window_blocks_then_frees(self):
        import newsapi_client as nc
        lim = nc._SlidingWindowLimiter(2, period=0.3)
        assert lim.acquire(0) and lim.acquire(0)
        t0 = time.monotonic()
        assert lim.acquire(0.05) is False                 # 0.05s 内不可能空出名额 ⇒ 立即放弃
        assert time.monotonic() - t0 < 0.05
        assert lim.acquire(1.0) is True                   # 等到最早那次滑出窗口
        assert time.monotonic() - t0 >= 0.25

    def test_acquire_timeout_is_below_the_phase1_wait_for_buzz(self):
        """等名额的上限必须小于 Phase-1 等 Buzz 的超时——否则 Buzz 整个超时，连 Yahoo 都拿不到。"""
        import config
        src = (Path(__file__).resolve().parent.parent / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        m = re.search(r"phase1_agents\)\) as executor:.*?future\.result\(timeout=(\d+)\)", src, re.S)
        assert m, "找不到 Phase-1 等各蜂结果的超时——改了那段代码就要同步改本守卫"
        assert config.NEWS_SOURCE_CONFIG["massive_acquire_timeout_s"] < int(m.group(1))

    def test_production_config_is_massive_with_free_tier_rate(self):
        import config
        assert config.NEWS_SOURCE_CONFIG["primary"] == "massive"
        assert config.NEWS_SOURCE_CONFIG["massive_calls_per_minute"] <= 5
        assert config._SECRET_REGISTRY["MASSIVE_API_KEY"] == "~/.alpha_hive_massive_key"


# ══════════════════════════════════════════════════════════════════════════
# 2. AV 回滚路径（primary = "alpha_vantage"）
# ══════════════════════════════════════════════════════════════════════════

class TestAvRollbackPath:

    @pytest.fixture(autouse=True)
    def _av_primary(self, monkeypatch):
        _primary(monkeypatch, "alpha_vantage")

    def test_server_refusal_keeps_text_without_key(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, av=_resp({"Information": _AV_DAILY_MSG}), massive=_resp(_massive(["positive"] * 3)))
        r = nc.get_ticker_news("NVDA")
        assert r["source"] == "yahoo_finance" and calls["massive"] == 0
        assert r["primary_attempt"]["source"] == "alpha_vantage"
        assert r["primary_attempt"]["status"] == "server_refused"
        assert "25 requests per day" in r["primary_attempt"]["message"]
        assert _KEY not in json.dumps(r, ensure_ascii=False)
        st = nc.get_primary_run_stats()
        assert st["status"] == {"server_refused": 1}
        assert st["messages"][0]["count"] == 1 and _KEY not in st["messages"][0]["text"]

    def test_error_message_key_counts_as_refusal(self, monkeypatch):
        """AV 报错用 `Error Message` 键；原先落进空 feed 分支（去向同样是降级，只是记错了类）。"""
        import newsapi_client as nc
        _route(monkeypatch, av=_resp({"Error Message": "Invalid API call."}))
        assert nc.get_ticker_news("NVDA")["primary_attempt"] == {
            "source": "alpha_vantage", "status": "server_refused", "message": "Invalid API call."}

    def test_local_quota_does_not_call_av(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, av=_resp(_AV_OK))
        nc._av_daily["date"] = nc.pdt_today()
        nc._av_daily["count"] = nc._AV_DAILY_LIMIT
        r = nc.get_ticker_news("NVDA")
        assert r["primary_attempt"] == {"source": "alpha_vantage", "status": "local_quota"}
        assert calls["av"] == 0 and r["source"] == "yahoo_finance"

    def test_success_is_recorded_as_ok(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, av=_resp(_AV_OK))
        r = nc.get_ticker_news("NVDA")
        assert r["source"] == "alpha_vantage"
        assert r["primary_attempt"] == {"source": "alpha_vantage", "status": "ok"}

    def test_network_error_text_is_redacted(self, monkeypatch):
        """异常文本里常带完整 URL（含 apikey 参数）。"""
        import newsapi_client as nc

        def boom(url, **kw):
            raise ConnectionError(f"Max retries exceeded with url: /query?function=NEWS_SENTIMENT&apikey={_KEY}")

        _route(monkeypatch, av=boom)
        a = nc.get_ticker_news("NVDA")["primary_attempt"]
        assert a["status"] == "network_error" and _KEY not in a["message"] and "***" in a["message"]

    def test_http_error(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, av=_resp({}, ok=False, status_code=503))
        assert nc.get_ticker_news("NVDA")["primary_attempt"] == {
            "source": "alpha_vantage", "status": "http_error", "message": "HTTP 503"}

    def test_cache_hit_is_not_counted_twice(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, av=_resp({"Information": _AV_DAILY_MSG}))
        nc.get_ticker_news("NVDA")
        r2 = nc.get_ticker_news("NVDA")
        assert calls["av"] == 1
        assert r2["primary_attempt"]["status"] == "server_refused"   # 缓存里带着当时的结局
        assert nc.get_primary_run_stats()["status"] == {"server_refused": 1}


# ══════════════════════════════════════════════════════════════════════════
# 3. 只加记录、不改去向；Buzz 记录字段与世代边界印记
# ══════════════════════════════════════════════════════════════════════════

class TestRecordingDoesNotChangeTheChannel:

    def test_degraded_result_is_the_yahoo_result_plus_one_key(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, massive=_resp({"status": "ERROR", "error": "x"}, ok=False, status_code=429))
        sentinel = {"ticker": "NVDA", "source": "yahoo_finance", "is_real_data": True,
                    "sentiment_score": 3.7, "total_articles": 3, "articles": []}
        monkeypatch.setattr(nc, "_fetch_yf_news", lambda t, m=10: dict(sentinel))
        r = nc.get_ticker_news("NVDA")
        assert set(r) - set(sentinel) == {"primary_attempt"}
        assert {k: r[k] for k in sentinel} == sentinel


_CNN_FG = {"value": 47, "classification": "Neutral", "sentiment_score": 4.7,
           "is_real_data": True, "source": "cnn", "timestamp": "2026-10-06T14:15:27"}


@pytest.fixture
def buzz_env(monkeypatch, stub_reddit):
    """真实 BuzzBee 的外部依赖钉在源头（同 tests/test_bee_details_contract.py 的 buzz_sources）；
    新闻由 state["news"] 决定。"""
    import fear_greed
    import newsapi_client as nc
    import yahoo_trending
    from swarm_agents import cache as _swarm_cache

    stock = {"price": 238.9, "momentum_5d": 1.2, "avg_volume": 180_000_000,
             "volume_ratio": 1.1, "volatility_20d": 41.0}
    monkeypatch.setattr(_swarm_cache, "_fetch_stock_data", lambda ticker, target_date=None: dict(stock))
    monkeypatch.setattr(yahoo_trending, "get_ticker_attention",
                        lambda ticker: yahoo_trending._default_result(ticker))
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: dict(_CNN_FG))
    state = {"news": None}
    monkeypatch.setattr(nc, "get_ticker_news", lambda ticker, max_articles=10: dict(state["news"]))
    return state


def _news(source, status=None, score=3.0):
    r = {"ticker": "NVDA", "articles": [{"title": f"h{i}"} for i in range(4)], "total_articles": 4,
         "bullish_count": 1, "bearish_count": 1, "neutral_count": 2, "sentiment_score": score,
         "dominant_theme": "t", "source": source, "is_real_data": True,
         "data_quality": {"issues": [], "passed": True}}
    if status:
        r["primary_attempt"] = {"source": "massive", "status": status}
    return r


def _run_buzz(ticker="NVDA"):
    from pheromone_board import PheromoneBoard
    from swarm_agents import BuzzBeeWhisper
    r = BuzzBeeWhisper(PheromoneBoard()).analyze(ticker)
    assert "error" not in r, r.get("error")
    return r


class TestBuzzRecordsSourceOnly:

    def test_source_fields_present(self, buzz_env):
        buzz_env["news"] = _news("yahoo_finance", "server_refused")
        det = _run_buzz()["details"]
        assert det["news_source"] == "yahoo_finance"
        assert det["news_primary_status"] == "server_refused"
        assert det["news_primary"] == "massive"          # 主源失败降级时照写（印记靠它）

    def test_channel_values_unchanged_by_the_new_keys(self, buzz_env):
        """同一份新闻内容，带 / 不带 `source`+`primary_attempt`：通道值、DQ、分数、方向逐项相同。"""
        buzz_env["news"] = _news("massive", "ok")
        a = _run_buzz()
        bare = _news("massive")
        del bare["source"]
        buzz_env["news"] = bare
        b = _run_buzz()
        assert a["details"]["components"] == b["details"]["components"]
        assert a["data_quality"] == b["data_quality"]
        assert (a["score"], a["direction"]) == (b["score"], b["direction"])
        assert b["details"]["news_source"] is None and b["details"]["news_primary_status"] is None

    def test_dq_news_label_still_keyword_for_every_source(self, buzz_env):
        """不往 data_quality 加新取值（新 DQ 值要进 Queen 登记表，否则静默记 0）。"""
        for src, st in (("massive", "ok"), ("alpha_vantage", "ok"), ("yahoo_finance", "server_refused")):
            buzz_env["news"] = _news(src, st)
            assert _run_buzz()["data_quality"]["news"] == "keyword"

    def test_real_buzz_output_carries_the_boundary_marker(self, buzz_env):
        """生产者（真实 Buzz）↔ 读者（ic_rerun_readiness 印记）同一形状；旧记录没有这个键 ⇒ 不算新代码。"""
        import ic_rerun_readiness as rr
        buzz_env["news"] = _news("yahoo_finance", "network_error")
        rec = {"swarm_results": {"agent_details": {"BuzzBeeWhisper": _run_buzz()}}}
        assert rr._marker_buzz_news_primary_massive(rec) is True
        assert rr._BOUNDARY_MARKERS["v0.45.445"][1] is rr._marker_buzz_news_primary_massive
        old = {"swarm_results": {"agent_details": {"BuzzBeeWhisper": {"details": {"news_source": "alpha_vantage"}}}}}
        assert rr._marker_buzz_news_primary_massive(old) is False

    def test_boundary_is_registered_before_the_dim_ic_window(self):
        """换主源 = 换 news 通道：必须登世代边界，且日期早于维度 IC 协议窗口（否则终止 H1）。"""
        import ic_rerun_readiness as rr
        import signal_archive as sa
        sys_path_exp = Path(__file__).resolve().parent.parent / "experiments"
        import sys
        sys.path.insert(0, str(sys_path_exp))
        import dim_ic_protocol as P
        hit = [(d, v) for d, v, _ in rr._COHORT_HISTORY if v == "v0.45.445"]
        assert len(hit) == 1 and hit[0][0] < P.FORWARD_START
        assert set(sa.COHORT_SIGNAL_SCOPE["v0.45.445"]) == {"buzz.comp.news_signal", "agent.BuzzBeeWhisper.*"}


# ══════════════════════════════════════════════════════════════════════════
# 4. 汇总口径
# ══════════════════════════════════════════════════════════════════════════

def _sr(src, st):
    return {"agent_details": {"BuzzBeeWhisper": {"details": {"news_source": src, "news_primary_status": st}}}}


class TestSummarize:

    def test_normal_massive_day(self):
        import newsapi_client as nc
        sr = {f"M{i}": _sr("massive", "ok") for i in range(29)}
        sr["N0"] = _sr("yahoo_finance", "network_error")
        s = nc.summarize_news_sources(sr)
        assert s["available"] and s["primary_source"] == "massive"
        assert s["n_known"] == 30 and s["n_unknown"] == 0
        assert s["non_primary_share"] == round(1 / 30, 4)
        assert s["by_source"] == {"massive": 29, "yahoo_finance": 1}
        assert s["primary_status"] == {"ok": 29, "network_error": 1}

    def test_ratio_follows_the_configured_primary(self, monkeypatch):
        import newsapi_client as nc
        _primary(monkeypatch, "alpha_vantage")
        sr = {f"A{i}": _sr("alpha_vantage", "ok") for i in range(24)}
        sr.update({f"L{i}": _sr("yahoo_finance", "local_quota") for i in range(6)})
        s = nc.summarize_news_sources(sr)
        assert s["primary_source"] == "alpha_vantage" and s["non_primary_share"] == 0.2

    def test_unknown_is_outside_the_ratio(self):
        """Buzz 报错（无 details）不能被算成「新闻降级」。"""
        import newsapi_client as nc
        sr = {f"M{i}": _sr("massive", "ok") for i in range(10)}
        sr.update({f"E{i}": {"agent_details": {"BuzzBeeWhisper": {"error": "boom"}}} for i in range(20)})
        s = nc.summarize_news_sources(sr)
        assert s["n_known"] == 10 and s["n_unknown"] == 20 and s["non_primary_share"] == 0.0

    def test_empty_is_unavailable(self):
        import newsapi_client as nc
        s = nc.summarize_news_sources({})
        assert s["available"] is False and s["non_primary_share"] is None


# ══════════════════════════════════════════════════════════════════════════
# 5. 告警
# ══════════════════════════════════════════════════════════════════════════

def _status(ns="__absent__", step2="success"):
    extra = {"gh_pages": {"success": True}}
    if ns != "__absent__":
        extra["news_sources"] = ns
    return {"status": "success", "total_duration_seconds": 2000,
            "steps_result": {"step2_hive_analysis": {"status": step2}},
            "scan_timing": {"extra": extra, "production_sync": {"outcome": "up_to_date"}}}


def _news_alerts(tmp_path, status):
    a = am.AlertAnalyzer(report_dir=tmp_path)
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False))
    a.analyze(p)
    return a, [x for x in a.alerts if "新闻通道" in x.message]


def _ns(n_primary, n_known=30, primary="massive", msgs=None):
    return {"available": True, "primary_source": primary, "n_known": n_known, "n_unknown": 0,
            "by_source": {primary: n_primary, "yahoo_finance": n_known - n_primary},
            "primary_status": {}, "non_primary_share": round((n_known - n_primary) / n_known, 4),
            "refusal_messages": msgs or []}


class TestAlerts:

    def test_degraded_day_is_p1_with_refusal_text(self, tmp_path):
        ns = _ns(0, msgs=[{"text": "You've exceeded the maximum requests per minute", "count": 30}])
        _, hits = _news_alerts(tmp_path, _status(ns))
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.HIGH
        assert "30/30" in hits[0].message and "massive" in hits[0].message
        assert "maximum requests per minute" in hits[0].details["主源拒绝原文"][0]

    @pytest.mark.parametrize("n_primary", [30, 29, 27])
    def test_normal_massive_days_are_silent(self, tmp_path, n_primary):
        _, hits = _news_alerts(tmp_path, _status(_ns(n_primary)))
        assert hits == []

    def test_threshold_is_strict(self, tmp_path):
        assert am.NEWS_NON_PRIMARY_P1_SHARE == 0.20
        _, hits = _news_alerts(tmp_path, _status(_ns(24)))   # 6/30 = 0.20
        assert hits == []
        _, hits = _news_alerts(tmp_path, _status(_ns(23)))   # 7/30
        assert [h.level for h in hits] == [am.AlertLevel.HIGH]

    def test_av_era_normal_would_now_be_flagged(self, tmp_path):
        """AV 时代的「常态」（8/30 走 Yahoo）在 Massive 下就是降级——两种量纲混算，应当报。"""
        _, hits = _news_alerts(tmp_path, _status(_ns(22, primary="alpha_vantage")))
        assert [h.level for h in hits] == [am.AlertLevel.HIGH] and "alpha_vantage" in hits[0].message

    def test_missing_observation_after_real_scan_is_p2(self, tmp_path):
        a, hits = _news_alerts(tmp_path, _status())
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM]
        assert any("新闻通道" in s for s in a.checks_skipped)

    def test_unavailable_carries_reason(self, tmp_path):
        _, hits = _news_alerts(tmp_path, _status({"available": False, "reason": "FileNotFoundError: x"}))
        assert hits[0].level == am.AlertLevel.MEDIUM and "FileNotFoundError" in hits[0].details["原因"]

    def test_too_few_known_is_undetermined_not_p1(self, tmp_path):
        _, hits = _news_alerts(tmp_path, _status(_ns(0, n_known=5)))
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "无法判定" in hits[0].message

    def test_no_real_scan_no_news_alert(self, tmp_path):
        _, hits = _news_alerts(tmp_path, _status(step2="skipped"))
        assert hits == []


def _tk(prefix: str, i: int) -> str:
    """BuzzBee 只收 1~5 位大写字母的 ticker。"""
    return prefix + "ABCDEFGHIJKLMNOP"[i]


class TestEndToEndFromRealBuzz:
    """真实 Buzz 输出 → summarize → alert_manager。夹具只钉外部源，不手写 details。"""

    def _swarm(self, buzz_env, plan):
        sr = {}
        for tk, (src, st) in plan.items():
            buzz_env["news"] = _news(src, st)
            sr[tk] = {"agent_details": {"BuzzBeeWhisper": _run_buzz(tk)}}
        return sr

    def test_all_yahoo_day_raises_p1(self, buzz_env, tmp_path):
        import newsapi_client as nc
        sr = self._swarm(buzz_env, {_tk("Y", i): ("yahoo_finance", "server_refused") for i in range(12)})
        _, hits = _news_alerts(tmp_path, _status(nc.summarize_news_sources(sr)))
        assert [h.level for h in hits] == [am.AlertLevel.HIGH]

    def test_mostly_massive_day_is_silent(self, buzz_env, tmp_path):
        import newsapi_client as nc
        plan = {_tk("M", i): ("massive", "ok") for i in range(11)}
        plan[_tk("N", 0)] = ("yahoo_finance", "network_error")
        _, hits = _news_alerts(tmp_path, _status(nc.summarize_news_sources(self._swarm(buzz_env, plan))))
        assert hits == []


class TestMainWiring:
    """真 `main()` 的蜂群路径：读不到本轮 `.swarm_results` 时照样写一条带原因的记录（不能「没记录」）。
    读得到时的正路径见 tests/test_production_sync.py::test_main_writes_the_gh_pages_result_into_the_timing_snapshot。"""

    def test_missing_swarm_file_is_recorded_with_reason(self, monkeypatch, tmp_path):
        import sys
        import alpha_hive_daily_report as adr
        import yf_gate

        class SwarmReporter:
            date_str = "2026-09-14"
            report_dir = tmp_path          # 目录里没有 .swarm_results_2026-09-14.json

            def __init__(self, date_override=None):
                pass

            def run_swarm_scan(self, focus_tickers=None):
                return {"system_status": "✅ 蜂群协作完成", "swarm_metadata": {"tickers_analyzed": 1},
                        "opportunities": [{"ticker": "NVDA"}]}

            def save_report(self, report):
                return "/dev/null"

            def deploy_and_notify(self, report):
                return {"gh_pages": {"success": True}, "deploy_env": "production"}

        written = []
        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SwarmReporter)
        monkeypatch.setattr(yf_gate, "install", lambda: False)
        monkeypatch.setattr(adr._timing, "write", lambda d, extra=None, **k: written.append((d, extra)))
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--swarm", "--no-llm", "--force"])
        adr.main()

        ns = written[0][1]["news_sources"]
        assert ns["available"] is False and "FileNotFoundError" in ns["reason"], ns
