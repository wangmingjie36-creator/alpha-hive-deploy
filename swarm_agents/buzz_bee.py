"""BuzzBeeWhisper - 情绪分析蜂 (Sentiment 维度, 权重 0.20)"""

from typing import Any, Dict, List, Optional
from swarm_agents._config import _log, _AS
from swarm_agents.cache import _safe_score
from swarm_agents.base import BeeAgent
from models import AgentResult
from swarm_agents.utils import (
    clamp_score_cfg, append_context, make_error_result,
    AGENT_ERRORS, LLM_ERRORS,
)
from swarm_agents.sentiment import (
    _get_sentiment_momentum, _detect_sentiment_price_divergence,
    _upsert_sentiment, _check_sentiment_spike,
)


class BuzzBeeWhisper(BeeAgent):
    """情绪分析蜂 - 多源市场情绪量化
    对应维度：Sentiment (权重 0.20)

    情绪信号来源（7 通道加权）：
    1. 价格动量（5日/20日）→ 市场参与者实际行为（20%）
    2. 成交量异动（今日 vs 20日均量）→ 关注度（10%）
    3. 波动率水平 → 恐惧/贪婪指标（5%）
    4. Reddit 社交情绪（ApeWisdom）→ 散户关注度和动量（25%）
    5. Finviz 新闻情绪 → 媒体叙事方向（25%）
    6. Yahoo Finance 热搜榜 → 市场关注度（5%，免费实时）
    7. Fear & Greed Index → 市场整体贪婪度（10%，免费实时）
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)
            stock = self._get_stock_data(ticker)

            # 1. 动量信号（-10% ~ +10% 映射到 0~100）
            momentum_pct = max(-10, min(10, stock["momentum_5d"]))
            momentum_sentiment = (momentum_pct + 10) / 20 * 100  # 0~100

            # 2. 成交量异动（阈值从 config AGENT_SCORING 读取）
            vol_ratio = stock["volume_ratio"]
            _vt = _AS.get("volume_thresholds", {})
            if vol_ratio > _vt.get("very_high", 2.0):
                volume_signal = 80
            elif vol_ratio > _vt.get("high", 1.5):
                volume_signal = 65
            elif vol_ratio > _vt.get("normal", 1.0):
                volume_signal = 50
            elif vol_ratio > _vt.get("low", 0.5):
                volume_signal = 35
            else:
                volume_signal = 20

            # 3. 波动率信号（高波动 = 恐惧，低波动 = 贪婪/稳定）
            vol20 = stock["volatility_20d"]
            _vlt = _AS.get("volatility_thresholds", {})
            if vol20 > _vlt.get("extreme", 60):
                vol_sentiment = 25
            elif vol20 > _vlt.get("high", 40):
                vol_sentiment = 40
            elif vol20 > _vlt.get("moderate", 20):
                vol_sentiment = 60
            else:
                vol_sentiment = 75

            # 4. Reddit 社交情绪
            reddit_signal = 50  # 默认中性
            reddit_data = None
            reddit_desc = ""
            try:
                from reddit_sentiment import get_reddit_sentiment
                reddit_data = get_reddit_sentiment(ticker)
                # 将 sentiment_score (1-10) 转为 0-100
                reddit_signal = _safe_score(reddit_data.get("sentiment_score"), 5.0, 0, 10, "reddit_score") * 10
                buzz = reddit_data.get("reddit_buzz", "quiet")
                mentions = reddit_data.get("mentions", 0)
                rank = reddit_data.get("rank")
                if rank:
                    reddit_desc = f"Reddit #{rank}({buzz},{mentions}提及)"
                else:
                    reddit_desc = f"Reddit 无热度"
            except LLM_ERRORS as e:
                _log.warning("BuzzBeeWhisper Reddit unavailable for %s: %s", ticker, e)
                reddit_desc = "Reddit 不可用"

            # 5. Finviz 新闻情绪（关键词基础 + LLM 语义增强）
            news_signal = 50  # 默认中性
            news_desc = ""
            news_reasoning = ""
            news_mode = "keyword"
            try:
                from finviz_sentiment import get_finviz_sentiment
                finviz = get_finviz_sentiment(ticker)
                news_signal = finviz["news_score"] * 10  # 0-10 → 0-100
                news_desc = finviz.get("news_signal", "")

                # LLM 语义分析（有 API Key 时自动启用）
                headlines = finviz.get("top_bullish", []) + finviz.get("top_bearish", [])
                if not headlines:
                    # 尝试获取原始标题
                    try:
                        from finviz_sentiment import _client as fv_client
                        if fv_client:
                            headlines = fv_client.get_news_titles(ticker, max_titles=10)
                    except (ImportError, AttributeError, ConnectionError) as e:
                        _log.debug("Finviz client headlines fallback for %s: %s", ticker, e)

                if headlines:
                    try:
                        import llm_service
                        if llm_service.is_available():
                            llm_news = llm_service.analyze_news_sentiment(ticker, headlines)
                            if llm_news:
                                # LLM 分析成功：混合关键词 30% + LLM 70%（LLM 情绪理解更强）
                                llm_news_score = _safe_score(
                                    llm_news.get("sentiment_score"), default=5.0,
                                    lo=0.0, hi=10.0, label="BuzzBee_llm_sentiment",
                                ) * 10
                                news_signal = news_signal * 0.30 + llm_news_score * 0.70
                                news_desc = llm_news.get("key_theme", news_desc)
                                news_reasoning = llm_news.get("reasoning", "")
                                news_mode = "llm_enhanced"
                    except (ImportError, ConnectionError, ValueError, KeyError) as e:
                        _log.debug("LLM news analysis unavailable for %s: %s", ticker, e)
            except LLM_ERRORS as e:
                _log.warning("BuzzBeeWhisper Finviz news unavailable for %s: %s", ticker, e)
                news_desc = "新闻不可用"

            # 5b. P4: Yahoo Finance + AV 新闻摘要（增强新闻面，与 Finviz 加权融合）
            try:
                from newsapi_client import get_ticker_news
                news_ext = get_ticker_news(ticker, max_articles=8)
                if news_ext.get("is_real_data") and news_ext.get("total_articles", 0) >= 3:
                    ext_signal = _safe_score(
                        news_ext.get("sentiment_score"), default=5.0,
                        lo=0.0, hi=10.0, label="BuzzBee_ext_sentiment",
                    ) * 10
                    # 融合：Finviz 60% + 扩展新闻 40%（扩展新闻覆盖更广）
                    news_signal = news_signal * 0.60 + ext_signal * 0.40
                    if not news_desc or "不可用" in news_desc:
                        news_desc = news_ext.get("dominant_theme", "")
                    _log.debug("BuzzBeeWhisper news extended for %s: src=%s articles=%d",
                               ticker, news_ext.get("source"), news_ext["total_articles"])
            except LLM_ERRORS as e:
                _log.debug("BuzzBeeWhisper extended news unavailable for %s: %s", ticker, e)

            # 6. Yahoo Finance 热搜榜（散户关注度，免费无需注册）
            yahoo_signal = 50.0
            yahoo_desc = ""
            try:
                from yahoo_trending import get_ticker_attention
                yt = get_ticker_attention(ticker)
                if yt.get("is_real_data"):
                    yahoo_signal = yt["attention_score"] * 10
                    yahoo_desc = yt.get("description", "")
            except LLM_ERRORS as e:
                _log.debug("Yahoo Trending unavailable for %s: %s", ticker, e)

            # 7. Fear & Greed Index（市场整体情绪背景，免费无需 Key）
            fg_signal = 50.0
            fg_desc = ""
            try:
                from fear_greed import get_fear_greed
                fg = get_fear_greed()
                if fg.get("is_real_data"):
                    fg_signal = fg["sentiment_score"] * 10
                    fg_desc = f"F&G {fg['value']}({fg['classification']})"
            except LLM_ERRORS as e:
                _log.debug("Fear & Greed unavailable: %s", e)

            # 7 通道加权综合（权重从 config AGENT_SCORING 读取）
            _bw = _AS.get("buzz_weights", {})
            sentiment_composite = (
                momentum_sentiment * _bw.get("momentum", 0.20) +
                volume_signal      * _bw.get("volume", 0.10) +
                vol_sentiment      * _bw.get("volatility", 0.05) +
                reddit_signal      * _bw.get("reddit", 0.25) +
                news_signal        * _bw.get("news", 0.25) +
                yahoo_signal       * _bw.get("yahoo", 0.05) +
                fg_signal          * _bw.get("fear_greed", 0.10)
            )

            # 转换为 0-10 分
            score = sentiment_composite / 10.0
            score = clamp_score_cfg(score)

            # ── 情绪动量调整（时序信号）──
            sent_momentum = _get_sentiment_momentum(ticker, int(sentiment_composite))
            score += sent_momentum["momentum_score_adj"]

            # ── 情绪-价格背离检测 ──
            sent_divergence = _detect_sentiment_price_divergence(
                int(sentiment_composite), stock.get("momentum_5d", 0.0), ticker
            )
            score += sent_divergence["score_adj"]
            score = clamp_score_cfg(score)

            # 方向判定
            bullish_pct = int(sentiment_composite)
            if sentiment_composite > _AS.get("direction_bullish_min", 60):
                direction = "bullish"
            elif sentiment_composite < _AS.get("direction_bearish_max", 40):
                direction = "bearish"
            else:
                direction = "neutral"

            discovery_parts = [
                f"情绪 {bullish_pct}%",
                f"动量 {stock['momentum_5d']:+.1f}%",
                f"量比 {vol_ratio:.1f}x",
                reddit_desc,
                news_desc,
                yahoo_desc,
                fg_desc,
            ]
            if news_reasoning:
                discovery_parts.append(news_reasoning)
            # 情绪动量/背离信号加入摘要
            if sent_momentum["momentum_regime"] not in ("stable", "unknown"):
                discovery_parts.append(f"情绪动量:{sent_momentum['momentum_regime']}")
            if sent_divergence["divergence_type"] != "none":
                discovery_parts.append(sent_divergence["description"])
            discovery = " | ".join(p for p in discovery_parts if p)

            discovery = append_context(discovery, ctx)

            # S3: 结构化数据交换（BearBee 可直接读取，替代正则解析）
            _pub_details = {"sentiment_score": bullish_pct}
            if reddit_data:
                _pub_details["reddit_momentum"] = reddit_data.get("momentum", 0)
            self._publish(ticker, discovery, "market_sentiment+reddit", round(score, 2), direction, details=_pub_details)

            # confidence = 基础 0.5（yfinance）+ Reddit + Finviz + Yahoo + F&G + LLM
            confidence = 0.5
            if reddit_data and reddit_data.get("rank"):
                confidence += 0.15
            if news_desc and "不可用" not in news_desc:
                confidence += 0.15
            if yahoo_desc and "不可用" not in yahoo_desc:
                confidence += 0.05
            if fg_desc:
                confidence += 0.05
            if news_mode == "llm_enhanced":
                confidence += 0.10
            confidence = min(1.0, confidence)

            # ── 情绪基线更新 + 突变检测（#13）──
            from datetime import datetime as _dt
            _today_str = _dt.now().strftime("%Y-%m-%d")
            _upsert_sentiment(ticker, _today_str, bullish_pct)
            _spike_msg = _check_sentiment_spike(ticker, bullish_pct, _today_str)

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="BuzzBeeWhisper",
                dimension="sentiment",
                data_quality={
                    "momentum": "real",
                    "volume": "real",
                    "volatility": "real",
                    "reddit": "real" if (reddit_data and reddit_data.get("rank")) else "fallback",
                    "finviz_news": news_mode if news_desc and "不可用" not in news_desc else "fallback",
                },
                details={
                    "sentiment_pct": bullish_pct,
                    "momentum_5d": stock["momentum_5d"],
                    "volume_ratio": vol_ratio,
                    "volatility_20d": vol20,
                    "reddit": {
                        "rank": reddit_data.get("rank") if reddit_data else None,
                        "mentions": reddit_data.get("mentions", 0) if reddit_data else 0,
                        "mention_delta": reddit_data.get("mention_delta", 0) if reddit_data else 0,
                        "buzz": reddit_data.get("reddit_buzz", "quiet") if reddit_data else "unknown",
                        "score": reddit_data.get("sentiment_score", 5.0) if reddit_data else 5.0,
                    },
                    "components": {
                        "momentum_signal": round(momentum_sentiment, 1),
                        "volume_signal": volume_signal,
                        "volatility_signal": vol_sentiment,
                        "reddit_signal": round(reddit_signal, 1),
                    },
                    "sentiment_momentum": sent_momentum,
                    "sentiment_divergence": sent_divergence,
                },
                extras={"sentinel_spike": _spike_msg},
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("BuzzBeeWhisper failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("BuzzBeeWhisper", "sentiment", e)
