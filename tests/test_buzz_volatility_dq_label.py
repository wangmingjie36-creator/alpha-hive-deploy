"""v0.45.559：BuzzBee `data_quality["volatility"]` 不再硬编码 "real"。

`volatility_20d` 为 None 时波动率通道回中性 50，标签却照报 "real" ⇒ Queen 的 `data_real_pct` 与 S5 冲突仲裁的
DQ 票重都按满分计（同 v0.45.209 GuardBee crowding 那个形状）。生产史 153 行（08-26 起 7 个扫描日）全是这样。

守三件事：
1. 三个行情通道（动量 / 量比 / 波动率）同一规则：取不到 ⇒ 通道 50 且标签 "unavailable"；取到 ⇒ "real"。
   参数化而不是只测波动率——三条里任何一条退回硬编码都会红。
2. 标签只是标签：同一份输入下，Buzz 的分数 / 方向 / 置信度 / 通道值都不读它（改标签不该动 H1 的输入层或冻结层）。
3. "unavailable" 在 Queen 的 PROXY_SOURCES 里（0.7），不是未分类（0 分 + dq_unclassified）。

⚠️ 全部离线、不用 skip：外部依赖钉在源头（同 tests/test_news_source_visibility.py 的 buzz_env）。
"""
from __future__ import annotations

import pytest

_CNN_FG = {"value": 47, "classification": "Neutral", "sentiment_score": 4.7,
           "is_real_data": True, "source": "cnn", "timestamp": "2026-10-06T14:15:27"}
_STOCK = {"price": 238.9, "momentum_5d": 1.2, "avg_volume": 180_000_000,
          "volume_ratio": 1.1, "volatility_20d": 41.0}
_NEWS = {"ticker": "NVDA", "articles": [{"title": f"h{i}"} for i in range(4)], "total_articles": 4,
         "bullish_count": 1, "bearish_count": 1, "neutral_count": 2, "sentiment_score": 6.0,
         "dominant_theme": "t", "source": "massive", "is_real_data": True,
         "data_quality": {"issues": [], "passed": True}}

# 行情字段 → (DQ 标签键, details.components 通道键)
_CHANNELS = {
    "momentum_5d": ("momentum", "momentum_signal"),
    "volume_ratio": ("volume", "volume_signal"),
    "volatility_20d": ("volatility", "volatility_signal"),
}


@pytest.fixture
def buzz(monkeypatch, stub_reddit):
    """返回 run(**行情覆盖) -> BuzzBee AgentResult dict。"""
    import fear_greed
    import llm_service
    import newsapi_client as nc
    import yahoo_trending
    from pheromone_board import PheromoneBoard
    from swarm_agents import BuzzBeeWhisper
    from swarm_agents import cache as _swarm_cache

    monkeypatch.setattr(llm_service, "is_available", lambda: False)
    monkeypatch.setattr(yahoo_trending, "get_ticker_attention",
                        lambda ticker: yahoo_trending._default_result(ticker))
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: dict(_CNN_FG))
    monkeypatch.setattr(nc, "get_ticker_news", lambda ticker, max_articles=10: dict(_NEWS))

    def run(**overrides):
        stock = {**_STOCK, **overrides}
        monkeypatch.setattr(_swarm_cache, "_fetch_stock_data",
                            lambda ticker, target_date=None: dict(stock))
        r = BuzzBeeWhisper(PheromoneBoard()).analyze("NVDA")
        assert "error" not in r, r.get("error")
        return r
    return run


class TestLabelFollowsAvailability:

    @pytest.mark.parametrize("field", sorted(_CHANNELS))
    def test_missing_field_is_unavailable_and_neutral(self, buzz, field):
        """变红的变异：把任一标签改回 `"real"` 硬编码（v0.45.559 之前波动率就是这样）。"""
        dq_key, comp_key = _CHANNELS[field]
        r = buzz(**{field: None})
        assert r["details"]["components"][comp_key] == 50, "缺数据时通道应回中性 50"
        assert r["data_quality"][dq_key] == "unavailable", (
            f"{field}=None 时通道已是中性 50，DQ 标签却报 {r['data_quality'][dq_key]!r}")
        others = {k for k in _CHANNELS.values() if k[0] != dq_key}
        assert all(r["data_quality"][k] == "real" for k, _ in others), "只该降级缺的那一条"

    def test_all_present_all_real(self, buzz):
        r = buzz()
        assert {r["data_quality"][k] for k, _ in _CHANNELS.values()} == {"real"}

    def test_zero_volatility_is_a_value_not_missing(self, buzz):
        """0.0 是取到的数（is not None），不是缺失——别把判据写成真值判断。"""
        assert buzz(volatility_20d=0.0)["data_quality"]["volatility"] == "real"


class TestLabelIsOnlyALabel:

    def test_buzz_outputs_do_not_read_the_label(self, buzz, monkeypatch):
        """同一份输入（volatility_20d=None），强行把标签改回旧值后重跑：分 / 方向 / 置信度 / 通道逐项相同。

        做法：把 AgentResult 的构造截住、把 volatility 标签换成 "real" 再交回——若 Buzz 别处读了这个标签，
        两次输出会在标签以外的地方不同。"""
        import models
        new = buzz(volatility_20d=None)

        orig_init = models.AgentResult.__init__

        def _old_label(self, *a, **kw):
            dq = dict(kw.get("data_quality") or {})
            dq["volatility"] = "real"
            kw["data_quality"] = dq
            orig_init(self, *a, **kw)
        monkeypatch.setattr(models.AgentResult, "__init__", _old_label)
        old = buzz(volatility_20d=None)

        assert old["data_quality"]["volatility"] == "real"      # 截住生效（否则本测试恒真）
        for k in ("score", "direction", "confidence"):
            assert new[k] == old[k], k
        assert new["details"]["components"] == old["details"]["components"]
        assert {k: v for k, v in new["data_quality"].items() if k != "volatility"} == \
               {k: v for k, v in old["data_quality"].items() if k != "volatility"}


class TestQueenClassifiesTheLabel:

    def test_unavailable_is_a_registered_proxy_label(self):
        from swarm_agents.queen_distiller import QueenDistiller as Q
        assert "unavailable" in Q.PROXY_SOURCES and "unavailable" not in Q.REAL_SOURCES

    def test_triple_penalty_counts_it_at_0_7_not_0(self, buzz):
        """经真实 `_apply_triple_penalty`：同一条 Buzz 记录，volatility 由 "real" 换成 "unavailable" ⇒
        data_real_pct 恰好少 0.3 / 通道数（记 0.7，不是 0），且不进 dq_unclassified。"""
        from swarm_agents.queen_distiller import QueenDistiller
        new = buzz(volatility_20d=None)
        assert new["data_quality"]["volatility"] == "unavailable"
        old = {**new, "data_quality": {**new["data_quality"], "volatility": "real"}}
        q = QueenDistiller.__new__(QueenDistiller)        # 只用实例方法读类属性，不需要 board / 配置
        tp_new = q._apply_triple_penalty("NVDA", 5.0, [new])
        tp_old = q._apply_triple_penalty("NVDA", 5.0, [old])
        assert tp_new["dq_unclassified"] == []
        n = len(new["data_quality"])
        assert tp_old["data_real_pct"] - tp_new["data_real_pct"] == pytest.approx(30.0 / n, abs=0.11)
