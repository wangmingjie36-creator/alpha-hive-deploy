"""新闻通道降级可见化（v0.45.444）——AV 拒绝原文、逐票实际来源、P1

背景
----
2026-10-06 Alpha Vantage 服务端日额度耗尽，30 只的新闻全部降级到 Yahoo。降级本身不报错，
Buzz 的 `data_quality.news` 两种源都写 `keyword`；而 Yahoo 关键词打标与 AV 逐文章模型分是两个
分类器，同一只票的 news_signal 比它自己的 AV 日均值平均低 21 分（25 只里 21 只更低）。
「谁会红？」——原来没人。拒绝原文也被丢了，只记了键名 `['Information']`，分不清撞的是
每日 25 次还是每分钟 5 次。

本文件钉住四件事
----------------
1. AV 每个没拿到数据的出口都带结局，拒绝原文去 key 后留下（`newsapi_client`）。
2. **只加记录、不改去向**：降级结果除多一个 `av_attempt` 键外不变；真实 Buzz 的通道值 /
   data_quality / 分数与没有这两个新键时逐项相同（维度 IC 协议：改 Buzz 通道 = 终止 H1）。
3. `summarize_news_sources` 的口径：来源未知的标的不进分子分母。
4. `alert_manager`：常态（0.20–0.27，09-28~10-02 实测）不报、降级报 P1、观测点缺失报 P2、
   扫描没真跑完不报。最后一组从真实 Buzz 输出一路接到告警，防「夹具让生产值不可达」。
"""
from __future__ import annotations

import json
import types

import pytest

import alert_manager as am

_KEY = "SECRETKEY1234567"
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


def _resp(data, ok=True, status_code=200):
    return types.SimpleNamespace(ok=ok, status_code=status_code, json=lambda: data,
                                 raise_for_status=lambda: None)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    import newsapi_client as nc
    d = tmp_path / "news_cache"
    d.mkdir()
    monkeypatch.setattr(nc, "_CACHE_DIR", d)
    monkeypatch.setattr(nc, "_av_run_stats", {"status": {}, "messages": {}})
    monkeypatch.setattr(nc, "_news_limiter", types.SimpleNamespace(acquire=lambda **kw: True))
    monkeypatch.setattr(nc, "_load_av_key", lambda: _KEY)
    nc._av_daily["count"] = 0
    nc._av_daily["date"] = ""
    yield
    nc._av_daily["count"] = 0
    nc._av_daily["date"] = ""


def _route(monkeypatch, av):
    """AV 与 Yahoo 走同一个 `get_session("newsapi")`：按 URL 分流。`av` 是响应或可调用（可抛异常）。"""
    import newsapi_client as nc
    calls = {"av": 0, "yf": 0}

    def get(url, **kw):
        if "alphavantage" in url:
            calls["av"] += 1
            return av(url, **kw) if callable(av) else av
        calls["yf"] += 1
        return _resp(_YF)

    session = types.SimpleNamespace(get=get)
    if nc._RESILIENCE_OK:
        monkeypatch.setattr(nc, "get_session", lambda source: session)
    else:
        monkeypatch.setattr(nc, "_req", session)
    return calls


# ══════════════════════════════════════════════════════════════════════════
# 1. AV 每个出口都带结局
# ══════════════════════════════════════════════════════════════════════════

class TestAvAttemptOutcome:

    def test_server_refusal_keeps_text_without_key(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, _resp({"Information": _AV_DAILY_MSG}))
        r = nc.get_ticker_news("NVDA")
        assert r["source"] == "yahoo_finance"
        assert r["av_attempt"]["status"] == "server_refused"
        assert "25 requests per day" in r["av_attempt"]["message"]
        assert _KEY not in json.dumps(r, ensure_ascii=False)
        st = nc.get_av_run_stats()
        assert st["status"] == {"server_refused": 1}
        assert st["messages"][0]["count"] == 1 and _KEY not in st["messages"][0]["text"]

    def test_error_message_key_counts_as_refusal(self, monkeypatch):
        """AV 报错用 `Error Message` 键；原先落进空 feed 分支（去向同样是降级，只是记错了类）。"""
        import newsapi_client as nc
        _route(monkeypatch, _resp({"Error Message": "Invalid API call."}))
        r = nc.get_ticker_news("NVDA")
        assert r["av_attempt"] == {"status": "server_refused", "message": "Invalid API call."}

    def test_local_quota_does_not_call_av(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, _resp(_AV_OK))
        nc._av_daily["date"] = nc.pdt_today()
        nc._av_daily["count"] = nc._AV_DAILY_LIMIT
        r = nc.get_ticker_news("NVDA")
        assert r["av_attempt"] == {"status": "local_quota"}
        assert calls["av"] == 0 and r["source"] == "yahoo_finance"

    def test_success_is_recorded_as_ok(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, _resp(_AV_OK))
        r = nc.get_ticker_news("NVDA")
        assert r["source"] == "alpha_vantage" and r["av_attempt"] == {"status": "ok"}
        assert nc.get_av_run_stats()["status"] == {"ok": 1}

    def test_no_key(self, monkeypatch):
        import newsapi_client as nc
        monkeypatch.setattr(nc, "_load_av_key", lambda: None)
        calls = _route(monkeypatch, _resp(_AV_OK))
        r = nc.get_ticker_news("NVDA")
        assert r["av_attempt"] == {"status": "no_key"} and calls["av"] == 0
        assert nc.get_av_run_stats()["status"] == {"no_key": 1}

    def test_network_error_text_is_redacted(self, monkeypatch):
        """异常文本里常带完整 URL（含 apikey 参数）。"""
        import newsapi_client as nc

        def boom(url, **kw):
            raise ConnectionError(f"Max retries exceeded with url: /query?function=NEWS_SENTIMENT&apikey={_KEY}")

        _route(monkeypatch, boom)
        r = nc.get_ticker_news("NVDA")
        assert r["av_attempt"]["status"] == "network_error"
        assert _KEY not in r["av_attempt"]["message"] and "***" in r["av_attempt"]["message"]

    def test_http_error(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, _resp({}, ok=False, status_code=503))
        assert nc.get_ticker_news("NVDA")["av_attempt"] == {"status": "http_error", "message": "HTTP 503"}

    def test_cache_hit_is_not_counted_twice(self, monkeypatch):
        import newsapi_client as nc
        calls = _route(monkeypatch, _resp({"Information": _AV_DAILY_MSG}))
        nc.get_ticker_news("NVDA")
        r2 = nc.get_ticker_news("NVDA")
        assert calls["av"] == 1
        assert r2["av_attempt"]["status"] == "server_refused"   # 缓存里带着当时的结局
        assert nc.get_av_run_stats()["status"] == {"server_refused": 1}


# ══════════════════════════════════════════════════════════════════════════
# 2. 只加记录、不改去向
# ══════════════════════════════════════════════════════════════════════════

class TestRecordingDoesNotChangeTheChannel:

    def test_degraded_result_is_the_yahoo_result_plus_one_key(self, monkeypatch):
        import newsapi_client as nc
        _route(monkeypatch, _resp({"Information": _AV_DAILY_MSG}))
        sentinel = {"ticker": "NVDA", "source": "yahoo_finance", "is_real_data": True,
                    "sentiment_score": 3.7, "total_articles": 3, "articles": []}
        monkeypatch.setattr(nc, "_fetch_yf_news", lambda t, m=10: dict(sentinel))
        r = nc.get_ticker_news("NVDA")
        assert set(r) - set(sentinel) == {"av_attempt"}
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


def _news(source, av_status=None, score=3.0):
    r = {"ticker": "NVDA", "articles": [{"title": f"h{i}"} for i in range(4)], "total_articles": 4,
         "bullish_count": 1, "bearish_count": 1, "neutral_count": 2, "sentiment_score": score,
         "dominant_theme": "t", "source": source, "is_real_data": True,
         "data_quality": {"issues": [], "passed": True}}
    if av_status:
        r["av_attempt"] = {"status": av_status}
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
        assert det["news_av_status"] == "server_refused"

    def test_channel_values_unchanged_by_the_new_keys(self, buzz_env):
        """同一份新闻内容，带 / 不带 `source`+`av_attempt`：通道值、DQ、分数、方向逐项相同。"""
        buzz_env["news"] = _news("yahoo_finance", "server_refused")
        a = _run_buzz()
        bare = _news("yahoo_finance")
        del bare["source"]
        buzz_env["news"] = bare
        b = _run_buzz()
        assert a["details"]["components"] == b["details"]["components"]
        assert a["data_quality"] == b["data_quality"]
        assert (a["score"], a["direction"]) == (b["score"], b["direction"])
        assert b["details"]["news_source"] is None and b["details"]["news_av_status"] is None

    def test_dq_news_label_still_keyword_for_both_sources(self, buzz_env):
        """不往 data_quality 加新取值（新 DQ 值要进 Queen 登记表，否则静默记 0）。"""
        for src in ("alpha_vantage", "yahoo_finance"):
            buzz_env["news"] = _news(src, "ok" if src == "alpha_vantage" else "local_quota")
            assert _run_buzz()["data_quality"]["news"] == "keyword"


# ══════════════════════════════════════════════════════════════════════════
# 3. 汇总口径
# ══════════════════════════════════════════════════════════════════════════

def _sr(src, av):
    return {"agent_details": {"BuzzBeeWhisper": {"details": {"news_source": src, "news_av_status": av}}}}


class TestSummarize:

    def test_normal_day_shape(self):
        import newsapi_client as nc
        sr = {f"A{i}": _sr("alpha_vantage", "ok") for i in range(24)}
        sr.update({f"L{i}": _sr("yahoo_finance", "local_quota") for i in range(5)})
        sr["R0"] = _sr("yahoo_finance", "server_refused")
        s = nc.summarize_news_sources(sr)
        assert s["available"] and s["n_known"] == 30 and s["n_unknown"] == 0
        assert s["non_av_share"] == 0.2
        assert s["by_source"] == {"alpha_vantage": 24, "yahoo_finance": 6}
        assert s["av_status"] == {"ok": 24, "local_quota": 5, "server_refused": 1}

    def test_unknown_is_outside_the_ratio(self):
        """Buzz 报错（无 details）不能被算成「新闻降级」。"""
        import newsapi_client as nc
        sr = {f"A{i}": _sr("alpha_vantage", "ok") for i in range(10)}
        sr.update({f"E{i}": {"agent_details": {"BuzzBeeWhisper": {"error": "boom"}}} for i in range(20)})
        s = nc.summarize_news_sources(sr)
        assert s["n_known"] == 10 and s["n_unknown"] == 20 and s["non_av_share"] == 0.0

    def test_empty_is_unavailable(self):
        import newsapi_client as nc
        s = nc.summarize_news_sources({})
        assert s["available"] is False and s["non_av_share"] is None


# ══════════════════════════════════════════════════════════════════════════
# 4. 告警
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


def _ns(n_av, n_known=30, msgs=None):
    return {"available": True, "n_known": n_known, "n_unknown": 0,
            "by_source": {"alpha_vantage": n_av, "yahoo_finance": n_known - n_av},
            "av_status": {}, "non_av_share": round((n_known - n_av) / n_known, 4),
            "refusal_messages": msgs or []}


class TestAlerts:

    def test_degraded_day_is_p1_with_refusal_text(self, tmp_path):
        ns = _ns(0, msgs=[{"text": "standard API rate limit is 25 requests per day", "count": 25}])
        _, hits = _news_alerts(tmp_path, _status(ns))
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.HIGH
        assert "30/30" in hits[0].message
        assert "25 requests per day" in hits[0].details["AV 拒绝原文"][0]

    @pytest.mark.parametrize("n_av", [24, 22])   # 09-28~10-02 实测常态：6 只与 8 只不走 AV
    def test_normal_days_are_silent(self, tmp_path, n_av):
        _, hits = _news_alerts(tmp_path, _status(_ns(n_av)))
        assert hits == []

    def test_threshold_is_strict(self, tmp_path):
        _, hits = _news_alerts(tmp_path, _status(_ns(18)))   # 12/30 = 0.40
        assert hits == []
        _, hits = _news_alerts(tmp_path, _status(_ns(17)))   # 13/30
        assert [h.level for h in hits] == [am.AlertLevel.HIGH]

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
        for tk, (src, av) in plan.items():
            buzz_env["news"] = _news(src, av)
            sr[tk] = {"agent_details": {"BuzzBeeWhisper": _run_buzz(tk)}}
        return sr

    def test_all_yahoo_day_raises_p1(self, buzz_env, tmp_path):
        import newsapi_client as nc
        sr = self._swarm(buzz_env, {_tk("Y", i): ("yahoo_finance", "server_refused") for i in range(12)})
        _, hits = _news_alerts(tmp_path, _status(nc.summarize_news_sources(sr)))
        assert [h.level for h in hits] == [am.AlertLevel.HIGH]

    def test_mostly_av_day_is_silent(self, buzz_env, tmp_path):
        import newsapi_client as nc
        plan = {_tk("A", i): ("alpha_vantage", "ok") for i in range(10)}
        plan.update({_tk("L", i): ("yahoo_finance", "local_quota") for i in range(2)})
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
