#!/usr/bin/env python3
"""
🐝 Alpha Hive 蜂群 Agent 系统 - 6 个自治工蜂 + QueenDistiller
实现真正的多 Agent 并行协作与信息素驱动决策

5 维加权评分公式（CLAUDE.md）：
  Opportunity Score = 0.30×Signal + 0.20×Catalyst + 0.20×Sentiment + 0.15×Odds + 0.15×RiskAdj

Agent → 维度映射：
  Signal   (0.30) = ScoutBeeNova     (SEC Form4/13F + 拥挤度)
  Catalyst (0.20) = ChronosBeeHorizon (财报/事件催化剂)
  Sentiment(0.20) = BuzzBeeWhisper   (yfinance 动量 + 成交量情绪)
  Odds     (0.15) = OracleBeeEcho    (期权 IV/P-C Ratio)
  RiskAdj  (0.15) = GuardBeeSentinel (交叉验证 + 拥挤度折扣)
  ML 辅助          = RivalBeeVanguard (ML 预测，不直接参与 5 维公式，作为额外加减分)
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from pheromone_board import PheromoneBoard, PheromoneEntry
import json


# ==================== 工具函数 ====================

# yfinance 数据缓存（同一次扫描内共享，避免重复请求）
import time as _time
import threading as _threading

from resilience import yfinance_limiter, yfinance_breaker
from models import DataQualityChecker as _DQChecker

_yf_cache: Dict[str, Dict] = {}
_yf_cache_ts: Dict[str, float] = {}
_yf_lock = _threading.Lock()
_YF_CACHE_TTL = 120  # 缓存 2 分钟
_YF_MAX_RETRIES = 2


def _fetch_stock_data(ticker: str) -> Dict:
    """
    从 yfinance 拉取股票实时数据（价格、动量、成交量等）
    内置缓存（2 分钟 TTL）+ RateLimiter + CircuitBreaker + 指数退避重试
    失败时返回默认值，不会抛出异常
    """
    # 检查缓存
    cached = _yf_cache.get(ticker)
    if cached and (_time.time() - _yf_cache_ts.get(ticker, 0)) < _YF_CACHE_TTL:
        return cached

    data = {
        "price": 100.0,
        "momentum_5d": 0.0,
        "avg_volume": 0,
        "volume_ratio": 1.0,
        "volatility_20d": 0.0,
    }

    if not yfinance_breaker.allow_request():
        return data

    for attempt in range(_YF_MAX_RETRIES + 1):
        try:
            yfinance_limiter.acquire()
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo")
            if hist.empty:
                if attempt < _YF_MAX_RETRIES:
                    _time.sleep(1.0 * (2 ** attempt))
                    continue
                return data

            data["price"] = float(hist["Close"].iloc[-1])

            if len(hist) >= 5:
                data["momentum_5d"] = (hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100

            if len(hist) >= 2:
                recent_vol = float(hist["Volume"].iloc[-1])
                avg_vol = float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20 else float(hist["Volume"].mean())
                data["avg_volume"] = int(avg_vol)
                data["volume_ratio"] = recent_vol / avg_vol if avg_vol > 0 else 1.0

            if len(hist) >= 20:
                returns = hist["Close"].pct_change().dropna()
                data["volatility_20d"] = float(returns.std() * (252 ** 0.5) * 100)

            # 写入缓存
            _yf_cache[ticker] = data
            _yf_cache_ts[ticker] = _time.time()
            yfinance_breaker.record_success()
            break

        except Exception as e:
            if attempt < _YF_MAX_RETRIES:
                _time.sleep(1.0 * (2 ** attempt))
            else:
                yfinance_breaker.record_failure()

    return data


# ==================== Agent 基类 ====================

class BeeAgent(ABC):
    """Agent 基类：所有 Agent 必须继承此类"""

    def __init__(self, board: PheromoneBoard, retriever=None):
        self.board = board
        self.retriever = retriever
        # 预注入的共享数据（由外部批量预取后注入，避免重复 API 调用）
        self._prefetched_stock: Dict[str, Dict] = {}
        self._prefetched_context: Dict[str, str] = {}

    @abstractmethod
    def analyze(self, ticker: str) -> Dict:
        """
        分析单个标的

        Returns:
            - score: 0-10 的评分
            - direction: "bullish" / "bearish" / "neutral"
            - discovery: 一句话摘要
            - source: 数据来源
            - dimension: 对应的 5 维维度名 ("signal"/"catalyst"/"sentiment"/"odds"/"risk_adj")
        """
        pass

    def _publish(self, ticker: str, discovery: str, source: str, score: float, direction: str):
        """发布发现到信息素板"""
        entry = PheromoneEntry(
            agent_id=self.__class__.__name__,
            ticker=ticker,
            discovery=discovery,
            source=source,
            self_score=score,
            direction=direction
        )
        self.board.publish(entry)

    def _get_stock_data(self, ticker: str) -> Dict:
        """获取股票数据（优先使用预取缓存，回退到直接请求）"""
        if ticker in self._prefetched_stock:
            return self._prefetched_stock[ticker]
        return _fetch_stock_data(ticker)

    def _get_history_context(self, ticker: str) -> str:
        """获取历史上下文（优先预取缓存，回退到实时查询）"""
        if ticker in self._prefetched_context:
            return self._prefetched_context[ticker]
        if not self.retriever:
            return ""
        try:
            if hasattr(self.retriever, 'get_context_for_agent'):
                return self.retriever.get_context_for_agent(
                    ticker, self.__class__.__name__
                )
            from datetime import datetime
            return self.retriever.get_context_summary(ticker, datetime.now().strftime("%Y-%m-%d"))
        except Exception:
            return ""


def prefetch_shared_data(tickers: list, retriever=None) -> Dict:
    """
    批量预取所有 ticker 的共享数据（yfinance + VectorMemory），
    避免 6 个 Agent 各自重复请求。

    返回: {"stock_data": {ticker: data}, "contexts": {ticker: str}}
    """
    stock_data = {}
    contexts = {}

    # 1. 批量预取 yfinance（串行但有全局缓存，只请求一次/ticker）
    for t in tickers:
        stock_data[t] = _fetch_stock_data(t)

    # 2. 批量预取 VectorMemory 上下文（一次查询/ticker，而非 6 次）
    if retriever and hasattr(retriever, 'get_context_for_agent'):
        for t in tickers:
            try:
                contexts[t] = retriever.get_context_for_agent(t, "BeeAgent")
            except Exception:
                contexts[t] = ""

    return {"stock_data": stock_data, "contexts": contexts}


def inject_prefetched(agents: list, prefetched: Dict):
    """将预取数据注入所有 Agent"""
    for agent in agents:
        agent._prefetched_stock = prefetched.get("stock_data", {})
        agent._prefetched_context = prefetched.get("contexts", {})


# ==================== ScoutBeeNova (Signal 维度) ====================

class ScoutBeeNova(BeeAgent):
    """聪明钱侦察蜂 - SEC Form4/13F 内幕交易 + 拥挤度分析
    对应维度：Signal (权重 0.30)

    数据源：
    - SEC EDGAR Form 4（内幕买卖记录，免费 API）
    - CrowdingDetector（拥挤度评估）
    - yfinance（动量/成交量）

    评分逻辑：
    - 内幕买入权重 60% + 拥挤度权重 40%
    - 高管主动买入 → 强烈看多信号
    - 大规模内幕卖出 → 看空信号
    """

    def analyze(self, ticker: str) -> Dict:
        try:
            ctx = self._get_history_context(ticker)

            # ---- 1. SEC EDGAR 内幕交易数据 ----
            insider_data = None
            insider_score = 5.0
            insider_summary = ""
            try:
                from sec_edgar import get_insider_trades
                insider_data = get_insider_trades(ticker, days=90)
                insider_score = insider_data.get("sentiment_score", 5.0)
                insider_summary = insider_data.get("summary", "")
            except Exception as e:
                insider_summary = f"SEC 数据不可用: {e}"

            # ---- 2. 拥挤度分析（真实数据源）----
            stock = self._get_stock_data(ticker)

            from crowding_detector import CrowdingDetector
            detector = CrowdingDetector(ticker)

            from real_data_sources import get_real_crowding_metrics
            metrics = get_real_crowding_metrics(ticker, stock, self.board)

            crowding_score, component_scores = detector.calculate_crowding_score(metrics)
            crowding_signal = max(1.0, 10.0 - crowding_score / 10.0)

            # ---- 3. 综合评分：内幕交易 60% + 拥挤度 40% ----
            score = insider_score * 0.6 + crowding_signal * 0.4
            score = max(1.0, min(10.0, score))

            # 方向判断
            if insider_data and insider_data.get("insider_sentiment") == "bullish":
                direction = "bullish"
            elif insider_data and insider_data.get("insider_sentiment") == "bearish":
                if crowding_score > 50:
                    direction = "bearish"
                else:
                    direction = "neutral"  # 卖出但不拥挤，可能只是计划性减持
            elif crowding_score > 70:
                direction = "bearish"
            elif crowding_score < 30:
                direction = "bullish"
            else:
                direction = "neutral"

            category, _ = detector.get_crowding_category(crowding_score)
            adj_factor = detector.get_adjustment_factor(crowding_score)

            # 构建发现摘要
            parts = []
            if insider_data and insider_data.get("total_filings", 0) > 0:
                dollar_sold = insider_data.get("dollar_sold", 0)
                dollar_bought = insider_data.get("dollar_bought", 0)
                if dollar_bought > 0:
                    parts.append(f"内幕买入 ${dollar_bought:,.0f}")
                if dollar_sold > 0:
                    parts.append(f"内幕卖出 ${dollar_sold:,.0f}")
                # 标注重要交易
                notable = insider_data.get("notable_trades", [])
                if notable:
                    top = notable[0]
                    parts.append(f"{top['insider']} {top['code_desc']} {top['shares']:,.0f}股")
            else:
                parts.append("无近期内幕交易")

            parts.append(f"拥挤度 {crowding_score:.0f}/100（{category}）")
            parts.append(f"动量 {stock['momentum_5d']:+.1f}%")

            discovery = " | ".join(parts)
            if ctx:
                discovery = f"{discovery} | {ctx}"

            self._publish(ticker, discovery, "sec_edgar+crowding", score, direction)

            # Phase 2: confidence = 数据完整度（内幕数据可用 + 拥挤度可用）
            confidence = 0.5
            if insider_data and insider_data.get("total_filings", 0) > 0:
                confidence += 0.3
            dq = metrics.get("data_quality", {})
            real_fields = sum(1 for v in dq.values() if v == "real")
            confidence += min(0.2, real_fields * 0.04)
            confidence = min(1.0, confidence)

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "ScoutBeeNova",
                "dimension": "signal",
                "data_quality": metrics.get("data_quality", {}),
                "details": {
                    "insider": {
                        "sentiment": insider_data.get("insider_sentiment", "neutral") if insider_data else "unknown",
                        "score": insider_score,
                        "filings": insider_data.get("total_filings", 0) if insider_data else 0,
                        "dollar_bought": insider_data.get("dollar_bought", 0) if insider_data else 0,
                        "dollar_sold": insider_data.get("dollar_sold", 0) if insider_data else 0,
                        "notable_trades": (insider_data.get("notable_trades", [])[:3]) if insider_data else [],
                    },
                    "crowding_score": crowding_score,
                    "crowding_signal": round(crowding_signal, 2),
                    "components": component_scores,
                    "adjustment_factor": adj_factor,
                    "momentum_5d": stock["momentum_5d"],
                    "price": stock["price"],
                }
            }

        except Exception as e:
            return {"error": str(e), "source": "ScoutBeeNova", "score": 5.0, "dimension": "signal"}


# ==================== OracleBeeEcho (Odds 维度) ====================

class OracleBeeEcho(BeeAgent):
    """市场预期蜂 - 期权分析 + Polymarket 预测市场赔率
    对应维度：Odds (权重 0.15)
    融合：期权信号 60% + Polymarket 赔率 40%
    """

    def analyze(self, ticker: str) -> Dict:
        try:
            ctx = self._get_history_context(ticker)

            # 获取真实股价
            stock = self._get_stock_data(ticker)
            current_price = stock["price"]

            # ---- 期权分析（60%）----
            options_score = 5.0
            signal_summary = "期权数据不可用"
            try:
                from options_analyzer import OptionsAgent
                agent = OptionsAgent()
                result = agent.analyze(ticker, stock_price=current_price)
                options_score = result.get("options_score", 5.0)
                signal_summary = result.get("signal_summary", "平衡")
            except Exception:
                result = {}

            # ---- Polymarket 赔率（40%）----
            poly_score = 5.0
            poly_signal = ""
            try:
                from polymarket_client import get_polymarket_odds
                poly = get_polymarket_odds(ticker)
                poly_score = poly.get("odds_score", 5.0)
                poly_signal = poly.get("odds_signal", "")
                poly_markets = poly.get("markets_found", 0)
            except Exception:
                poly_markets = 0

            # ---- 融合评分 ----
            if poly_markets > 0:
                score = options_score * 0.6 + poly_score * 0.4
            else:
                score = options_score  # 无 Polymarket 数据时完全依赖期权

            # 从 signal_summary 推断方向
            if "多" in signal_summary or "增强" in signal_summary or "看涨" in signal_summary:
                direction = "bullish"
            elif "空" in signal_summary or "看跌" in signal_summary:
                direction = "bearish"
            else:
                direction = "neutral"

            discovery = f"{signal_summary} | ${current_price:.1f}"
            if poly_signal:
                discovery += f" | {poly_signal}"
            if ctx:
                discovery += f" | {ctx}"

            self._publish(ticker, discovery, "options+polymarket", score, direction)

            # Phase 2: confidence = 期权数据可用 + Polymarket 可用
            confidence = 0.4
            if result:
                confidence += 0.4
            if poly_markets > 0:
                confidence += 0.2
            confidence = min(1.0, confidence)

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "OracleBeeEcho",
                "dimension": "odds",
                "data_quality": {
                    "options": "real" if result else "fallback",
                    "polymarket": "real" if poly_markets > 0 else "unavailable",
                },
                "details": result,
                "polymarket_score": poly_score,
                "polymarket_markets": poly_markets,
            }

        except Exception as e:
            return {"error": str(e), "source": "OracleBeeEcho", "score": 5.0, "dimension": "odds"}


# ==================== BuzzBeeWhisper (Sentiment 维度) ====================

class BuzzBeeWhisper(BeeAgent):
    """情绪分析蜂 - 多源市场情绪量化
    对应维度：Sentiment (权重 0.20)

    情绪信号来源（5 通道加权）：
    1. 价格动量（5日/20日）→ 市场参与者实际行为（20%）
    2. 成交量异动（今日 vs 20日均量）→ 关注度（15%）
    3. 波动率水平 → 恐惧/贪婪指标（10%）
    4. Reddit 社交情绪（ApeWisdom）→ 散户关注度和动量（25%）
    5. Finviz 新闻情绪 → 媒体叙事方向（30%）
    """

    def analyze(self, ticker: str) -> Dict:
        try:
            ctx = self._get_history_context(ticker)
            stock = self._get_stock_data(ticker)

            # 1. 动量信号（-10% ~ +10% 映射到 0~100）
            momentum_pct = max(-10, min(10, stock["momentum_5d"]))
            momentum_sentiment = (momentum_pct + 10) / 20 * 100  # 0~100

            # 2. 成交量异动（>1.5 倍 = 高关注）
            vol_ratio = stock["volume_ratio"]
            if vol_ratio > 2.0:
                volume_signal = 80
            elif vol_ratio > 1.5:
                volume_signal = 65
            elif vol_ratio > 1.0:
                volume_signal = 50
            elif vol_ratio > 0.5:
                volume_signal = 35
            else:
                volume_signal = 20

            # 3. 波动率信号（高波动 = 恐惧，低波动 = 贪婪/稳定）
            vol20 = stock["volatility_20d"]
            if vol20 > 60:
                vol_sentiment = 25
            elif vol20 > 40:
                vol_sentiment = 40
            elif vol20 > 20:
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
                reddit_signal = reddit_data["sentiment_score"] * 10
                buzz = reddit_data.get("reddit_buzz", "quiet")
                mentions = reddit_data.get("mentions", 0)
                rank = reddit_data.get("rank")
                if rank:
                    reddit_desc = f"Reddit #{rank}({buzz},{mentions}提及)"
                else:
                    reddit_desc = f"Reddit 无热度"
            except Exception:
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
                    except Exception:
                        pass

                if headlines:
                    try:
                        import llm_service
                        if llm_service.is_available():
                            llm_news = llm_service.analyze_news_sentiment(ticker, headlines)
                            if llm_news:
                                # LLM 分析成功：混合关键词 50% + LLM 50%
                                llm_news_score = llm_news.get("sentiment_score", 5.0) * 10
                                news_signal = news_signal * 0.5 + llm_news_score * 0.5
                                news_desc = llm_news.get("key_theme", news_desc)
                                news_reasoning = llm_news.get("reasoning", "")
                                news_mode = "llm_enhanced"
                    except Exception:
                        pass
            except Exception:
                news_desc = "新闻不可用"

            # 5 通道加权综合（新闻情绪权重最高）
            sentiment_composite = (
                momentum_sentiment * 0.20 +
                volume_signal * 0.15 +
                vol_sentiment * 0.10 +
                reddit_signal * 0.25 +
                news_signal * 0.30
            )

            # 转换为 0-10 分
            score = sentiment_composite / 10.0
            score = max(1.0, min(10.0, score))

            # 方向判定
            bullish_pct = int(sentiment_composite)
            if sentiment_composite > 60:
                direction = "bullish"
            elif sentiment_composite < 40:
                direction = "bearish"
            else:
                direction = "neutral"

            discovery_parts = [
                f"情绪 {bullish_pct}%",
                f"动量 {stock['momentum_5d']:+.1f}%",
                f"量比 {vol_ratio:.1f}x",
                reddit_desc,
                news_desc,
            ]
            if news_reasoning:
                discovery_parts.append(news_reasoning)
            discovery = " | ".join(p for p in discovery_parts if p)

            if ctx:
                discovery = f"{discovery} | {ctx}"

            self._publish(ticker, discovery, "market_sentiment+reddit", round(score, 2), direction)

            # Phase 2: confidence = 基础 0.5（yfinance）+ Reddit + Finviz + LLM
            confidence = 0.5  # yfinance momentum/volume always available
            if reddit_data and reddit_data.get("rank"):
                confidence += 0.2
            if news_desc and "不可用" not in news_desc:
                confidence += 0.2
            if news_mode == "llm_enhanced":
                confidence += 0.1
            confidence = min(1.0, confidence)

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "BuzzBeeWhisper",
                "dimension": "sentiment",
                "data_quality": {
                    "momentum": "real",
                    "volume": "real",
                    "volatility": "real",
                    "reddit": "real" if (reddit_data and reddit_data.get("rank")) else "fallback",
                    "finviz_news": news_mode if news_desc and "不可用" not in news_desc else "fallback",
                },
                "details": {
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
                    }
                }
            }

        except Exception as e:
            return {"error": str(e), "source": "BuzzBeeWhisper", "score": 5.0, "dimension": "sentiment"}


# ==================== ChronosBeeHorizon (Catalyst 维度) ====================

class ChronosBeeHorizon(BeeAgent):
    """催化剂追踪蜂 - 财报、事件、时间线（yfinance 真实日历）
    对应维度：Catalyst (权重 0.20)
    """

    def analyze(self, ticker: str) -> Dict:
        try:
            ctx = self._get_history_context(ticker)

            catalysts_found = []
            score = 5.0
            direction = "neutral"

            # 1. 从 yfinance 获取真实财报日期
            try:
                import yfinance as yf
                t = yf.Ticker(ticker)
                cal = t.calendar
                if cal is not None:
                    # cal 可能是 DataFrame 或 dict
                    if hasattr(cal, 'to_dict'):
                        cal_dict = cal.to_dict()
                    elif isinstance(cal, dict):
                        cal_dict = cal
                    else:
                        cal_dict = {}

                    # 提取财报日期
                    earnings_date = cal_dict.get("Earnings Date", [])
                    if isinstance(earnings_date, list) and earnings_date:
                        from datetime import datetime
                        for ed in earnings_date:
                            if hasattr(ed, 'strftime'):
                                date_str = ed.strftime("%Y-%m-%d")
                            else:
                                date_str = str(ed)[:10]
                            days_until = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.now()).days
                            if days_until >= 0:
                                catalysts_found.append({
                                    "event": f"财报发布",
                                    "date": date_str,
                                    "days_until": days_until,
                                    "type": "earnings",
                                    "severity": "critical" if days_until <= 14 else "high",
                                })
                    elif isinstance(earnings_date, dict):
                        for key, val in earnings_date.items():
                            if hasattr(val, 'strftime'):
                                date_str = val.strftime("%Y-%m-%d")
                                from datetime import datetime
                                days_until = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.now()).days
                                if days_until >= 0:
                                    catalysts_found.append({
                                        "event": f"财报发布",
                                        "date": date_str,
                                        "days_until": days_until,
                                        "type": "earnings",
                                        "severity": "critical" if days_until <= 14 else "high",
                                    })

                    # 提取其他事件
                    for key in ["Ex-Dividend Date", "Dividend Date"]:
                        val = cal_dict.get(key)
                        if val:
                            if isinstance(val, dict):
                                for k, v in val.items():
                                    if hasattr(v, 'strftime'):
                                        catalysts_found.append({
                                            "event": key,
                                            "date": v.strftime("%Y-%m-%d"),
                                            "days_until": 0,
                                            "type": "dividend",
                                            "severity": "medium",
                                        })
                            elif hasattr(val, 'strftime'):
                                catalysts_found.append({
                                    "event": key,
                                    "date": val.strftime("%Y-%m-%d"),
                                    "days_until": 0,
                                    "type": "dividend",
                                    "severity": "medium",
                                })
            except Exception:
                pass

            # 2. 补充 CatalystTimeline（已有的硬编码催化剂）
            try:
                from catalyst_refinement import CatalystTimeline, create_nvda_catalysts, create_vktx_catalysts
                if ticker == "NVDA":
                    timeline = create_nvda_catalysts()
                elif ticker == "VKTX":
                    timeline = create_vktx_catalysts()
                else:
                    timeline = None

                if timeline:
                    for cat in timeline.get_upcoming_catalysts(days_ahead=30):
                        catalysts_found.append({
                            "event": cat.event_name,
                            "date": cat.scheduled_date or "TBD",
                            "days_until": cat.get_days_until_event(),
                            "type": cat.catalyst_type.value,
                            "severity": cat.severity.value,
                        })
            except Exception:
                pass

            # 评分逻辑
            if catalysts_found:
                # 按天数排序
                catalysts_found.sort(key=lambda c: c.get("days_until", 999))

                # 基础分 + 催化剂数量加成
                base = 6.0
                # 近期催化剂（7天内）额外加分
                imminent = [c for c in catalysts_found if c.get("days_until", 999) <= 7]
                medium = [c for c in catalysts_found if 7 < c.get("days_until", 999) <= 30]

                score = base + len(imminent) * 1.5 + len(medium) * 0.5
                score = min(10.0, score)

                nearest = catalysts_found[0]
                discovery = f"催化剂 {len(catalysts_found)} 个 | 最近：{nearest['event']}（{nearest.get('days_until', '?')}天后）"
                direction = "bullish"
            else:
                score = 4.0
                discovery = "无近期催化剂"
                direction = "neutral"

            if ctx:
                discovery = f"{discovery} | {ctx}"

            self._publish(ticker, discovery, "catalyst_timeline", score, direction)

            # Phase 2: confidence = 催化剂数量和来源多样性
            confidence = 0.3  # baseline
            if catalysts_found:
                confidence += min(0.4, len(catalysts_found) * 0.1)
                # 有 yfinance 实时日历数据加分
                has_yf = any(c.get("type") == "earnings" for c in catalysts_found)
                if has_yf:
                    confidence += 0.2
            confidence = min(1.0, confidence)

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "ChronosBeeHorizon",
                "dimension": "catalyst",
                "data_quality": {
                    "yfinance_calendar": "real" if catalysts_found else "empty",
                    "catalyst_refinement": "real",
                },
                "details": {"catalysts": catalysts_found[:5]}
            }

        except Exception as e:
            return {"error": str(e), "source": "ChronosBeeHorizon", "score": 5.0, "dimension": "catalyst"}


# ==================== RivalBeeVanguard (ML 辅助) ====================

class RivalBeeVanguard(BeeAgent):
    """竞争分析与 ML 预测蜂 - 概率预测 + 行业动量对标
    不直接参与 5 维公式，作为额外调整项
    """

    def analyze(self, ticker: str) -> Dict:
        try:
            ctx = self._get_history_context(ticker)

            # 尝试 ML 预测
            prediction = {}
            try:
                from ml_predictor_extended import MLPredictionService, TrainingData
                from datetime import datetime
                service = MLPredictionService()

                stock = self._get_stock_data(ticker)
                opportunity = TrainingData(
                    ticker=ticker,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    crowding_score=50.0,
                    catalyst_quality="B+",
                    momentum_5d=stock["momentum_5d"],
                    volatility=stock["volatility_20d"],
                    market_sentiment=stock["momentum_5d"] * 5,
                    iv_rank=50.0,
                    put_call_ratio=1.0,
                    actual_return_3d=0.0,
                    actual_return_7d=0.0,
                    actual_return_30d=0.0,
                    win_3d=False,
                    win_7d=False,
                    win_30d=False,
                )
                prediction = service.predict_for_opportunity(opportunity)
            except Exception:
                pass

            if prediction:
                prob = prediction.get("probability", 0.5)
                ret_7d = prediction.get("expected_7d", 0.0)
                ret_30d = prediction.get("expected_30d", 0.0)
                avg_ret = (ret_7d + ret_30d) / 2

                score = prob * 10  # 胜率 → 0-10
                score = max(1.0, min(10.0, score))

                direction = "bullish" if avg_ret > 0 else ("bearish" if avg_ret < 0 else "neutral")

                discovery = f"ML 胜率 {prob*100:.0f}% | 7d {ret_7d:+.2f}% | 30d {ret_30d:+.2f}%"
            else:
                # ML 不可用，用简单动量对标
                stock = self._get_stock_data(ticker)
                mom = stock["momentum_5d"]
                score = max(1.0, min(10.0, 5.0 + mom * 0.3))
                direction = "bullish" if mom > 2 else ("bearish" if mom < -2 else "neutral")
                discovery = f"动量对标 {mom:+.1f}% | 波动率 {stock['volatility_20d']:.0f}%"

            if ctx:
                discovery = f"{discovery} | {ctx}"

            self._publish(ticker, discovery, "ml_predictor", round(score, 2), direction)

            # Phase 2: confidence = ML 模型可用性
            confidence = 0.3 if not prediction else 0.8
            confidence = min(1.0, confidence)

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "RivalBeeVanguard",
                "dimension": "ml_auxiliary",
                "data_quality": {
                    "ml_prediction": "real" if prediction else "fallback_momentum",
                },
                "details": prediction if prediction else {"momentum_5d": stock["momentum_5d"]}
            }

        except Exception as e:
            return {"error": str(e), "source": "RivalBeeVanguard", "score": 5.0, "dimension": "ml_auxiliary"}


# ==================== GuardBeeSentinel (RiskAdj 维度) ====================

class GuardBeeSentinel(BeeAgent):
    """交叉验证与风险评估蜂 - 共振检测 + 拥挤度折扣 + 风险调整
    对应维度：RiskAdj (权重 0.15)
    """

    def analyze(self, ticker: str) -> Dict:
        try:
            ctx = self._get_history_context(ticker)

            # 1. 检测信息素板共振
            resonance = self.board.detect_resonance(ticker)
            top_signals = self.board.get_top_signals(ticker, n=5)

            # 2. 从信息素板读取已有 Agent 分数
            avg_score = sum(e.self_score for e in top_signals) / len(top_signals) if top_signals else 5.0

            # 3. 评估信号一致性
            if top_signals:
                directions = [e.direction for e in top_signals]
                bull = directions.count("bullish")
                bear = directions.count("bearish")
                total = len(directions)
                consistency = max(bull, bear) / total if total > 0 else 0
            else:
                consistency = 0
                bull = bear = 0

            # 4. 拥挤度风险折扣（使用真实数据源）
            adj_factor = 1.0
            try:
                from crowding_detector import CrowdingDetector
                from real_data_sources import get_real_crowding_metrics
                stock = self._get_stock_data(ticker)
                detector = CrowdingDetector(ticker)
                real_metrics = get_real_crowding_metrics(ticker, stock, self.board)
                # 覆盖 bullish_agents 为实际信息素板数据
                real_metrics["bullish_agents"] = bull
                crowd, _ = detector.calculate_crowding_score(real_metrics)
                adj_factor = detector.get_adjustment_factor(crowd)
            except Exception:
                pass

            # 5. 综合评分
            if resonance["resonance_detected"]:
                # 共振 + 一致性高 → 高分，但受拥挤度调整
                raw_score = 7.0 + consistency * 2.0  # 7.0 ~ 9.0
                score = raw_score * adj_factor
                direction = resonance["direction"]
                discovery = (
                    f"共振✅ {resonance['supporting_agents']} Agent 同向 | "
                    f"一致性 {consistency:.0%} | "
                    f"风险调整 {adj_factor:.2f}"
                )
            else:
                # 无共振 → 保守，打折
                score = avg_score * 0.8 * adj_factor
                direction = "neutral"
                discovery = (
                    f"信号分散 | 均分 {avg_score:.1f} | "
                    f"一致性 {consistency:.0%} | "
                    f"风险调整 {adj_factor:.2f}"
                )

            score = max(1.0, min(10.0, score))

            if ctx:
                discovery = f"{discovery} | {ctx}"

            self._publish(ticker, discovery, "guard_bee_sentinel", round(score, 2), direction)

            # Phase 2: confidence = 信号板有数据 + 一致性高
            confidence = 0.4
            if top_signals:
                confidence += 0.3
            if consistency >= 0.7:
                confidence += 0.2
            if resonance["resonance_detected"]:
                confidence += 0.1
            confidence = min(1.0, confidence)

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "GuardBeeSentinel",
                "dimension": "risk_adj",
                "data_quality": {
                    "pheromone_board": "real",
                    "crowding": "real",
                },
                "details": {
                    "resonance": resonance,
                    "top_signals_count": len(top_signals),
                    "consistency": consistency,
                    "adjustment_factor": adj_factor,
                }
            }

        except Exception as e:
            return {"error": str(e), "source": "GuardBeeSentinel", "score": 5.0, "dimension": "risk_adj"}


# ==================== QueenDistiller (5 维加权公式 + LLM 蒸馏) ====================

class QueenDistiller:
    """
    王后蒸馏蜂 - 5 维加权评分 + 共振增强 + 多数投票 + LLM 推理

    双引擎架构：
    1. 规则引擎（始终运行）：加权评分 + 共振 + 投票 → base_score
    2. LLM 引擎（有 API Key 时启用）：Claude 分析推理 → 调整评分 + 生成推理链

    Opportunity Score = 0.30×Signal + 0.20×Catalyst + 0.20×Sentiment + 0.15×Odds + 0.15×RiskAdj
    """

    DEFAULT_WEIGHTS = {
        "signal":    0.30,
        "catalyst":  0.20,
        "sentiment": 0.20,
        "odds":      0.15,
        "risk_adj":  0.15,
    }

    def __init__(self, board: PheromoneBoard, weight_manager=None, adapted_weights: Dict = None,
                 enable_llm: bool = True):
        self.board = board
        self.weight_manager = weight_manager
        self.enable_llm = enable_llm
        if adapted_weights:
            self.DIMENSION_WEIGHTS = adapted_weights
        else:
            self.DIMENSION_WEIGHTS = dict(self.DEFAULT_WEIGHTS)

    def distill(self, ticker: str, agent_results: List[Dict]) -> Dict:
        """
        5 维加权评分 + 共振增强 + 多数投票 + LLM 推理蒸馏

        双引擎：规则引擎始终运行作为基础，LLM 引擎在可用时叠加推理。
        """
        # ===== 规则引擎（始终运行）=====

        # 1. 过滤有效结果（含数据质量清洗）
        _dq = _DQChecker()
        cleaned_results = _dq.clean_results_batch(agent_results)
        valid_results = [r for r in cleaned_results if "error" not in r]
        all_results = cleaned_results

        # 2. 按 dimension 分组（含 confidence）
        dim_scores = {}
        dim_confidence = {}
        for r in valid_results:
            dim = r.get("dimension", "")
            if dim in self.DIMENSION_WEIGHTS:
                dim_scores[dim] = r.get("score", 5.0)
                dim_confidence[dim] = r.get("confidence", 0.5)

        # 3. ML 辅助分（按 confidence 缩放影响力）
        ml_adjustment = 0.0
        for r in valid_results:
            if r.get("dimension") == "ml_auxiliary":
                ml_score = r.get("score", 5.0)
                ml_conf = r.get("confidence", 0.5)
                ml_adjustment = (ml_score - 5.0) * 0.1 * ml_conf

        # 4. 5 维 confidence-weighted 评分
        # 低 confidence Agent 的评分向 5.0（中性）收缩
        weighted_sum = 0.0
        weight_total = 0.0
        for dim, weight in self.DIMENSION_WEIGHTS.items():
            if dim in dim_scores:
                conf = dim_confidence.get(dim, 0.5)
                # 按 confidence 混合：高 confidence 用原始分，低 confidence 拉向 5.0
                effective_score = dim_scores[dim] * conf + 5.0 * (1.0 - conf)
                weighted_sum += effective_score * weight
                weight_total += weight
            else:
                weighted_sum += 5.0 * weight
                weight_total += weight

        base_score = weighted_sum / weight_total if weight_total > 0 else 5.0

        # 5. ML 调整
        adjusted_score = base_score + ml_adjustment

        # 6. 共振增强
        resonance = self.board.detect_resonance(ticker)
        if resonance["resonance_detected"]:
            boost_pct = resonance["confidence_boost"]
            rule_score = adjusted_score * (1.0 + boost_pct / 100.0)
        else:
            rule_score = adjusted_score

        rule_score = round(max(0.0, min(10.0, rule_score)), 2)

        # 7. 多数投票
        directions = [r.get("direction", "neutral") for r in valid_results]
        bullish_count = directions.count("bullish")
        bearish_count = directions.count("bearish")
        neutral_count = directions.count("neutral")

        if bullish_count > bearish_count:
            rule_direction = "bullish"
        elif bearish_count > bullish_count:
            rule_direction = "bearish"
        else:
            rule_direction = "neutral"

        # 8. Agent 方向
        per_agent_directions = {}
        for r in all_results:
            src = r.get("source", "")
            if src:
                per_agent_directions[src] = r.get("direction", "neutral")

        # 9. data_quality 汇总
        data_quality_summary = {}
        real_count = 0
        total_fields = 0
        for r in valid_results:
            dq = r.get("data_quality", {})
            if isinstance(dq, dict):
                src = r.get("source", "unknown")
                data_quality_summary[src] = dq
                for v in dq.values():
                    total_fields += 1
                    if v == "real":
                        real_count += 1

        data_real_pct = round(real_count / total_fields * 100, 1) if total_fields > 0 else 0.0

        # ===== LLM 引擎（可用时叠加）=====
        llm_result = None
        reasoning = ""
        key_insight = ""
        risk_flag = ""
        llm_confidence = 0.0
        final_score = rule_score
        final_direction = rule_direction
        distill_mode = "rule_engine"

        if self.enable_llm:
            try:
                import llm_service
                if llm_service.is_available():
                    llm_result = llm_service.distill_with_reasoning(
                        ticker=ticker,
                        agent_results=valid_results,
                        dim_scores=dim_scores,
                        resonance=resonance,
                        rule_score=rule_score,
                        rule_direction=rule_direction,
                    )
            except Exception:
                pass

        if llm_result:
            distill_mode = "llm_enhanced"
            reasoning = llm_result.get("reasoning", "")
            key_insight = llm_result.get("key_insight", "")
            risk_flag = llm_result.get("risk_flag", "")
            llm_confidence = llm_result.get("confidence", 0.5)

            llm_score = llm_result.get("final_score")
            llm_direction = llm_result.get("direction")

            if llm_score is not None and isinstance(llm_score, (int, float)):
                # 混合策略：规则引擎 60% + LLM 40%（LLM 不完全替代规则引擎）
                final_score = round(rule_score * 0.6 + float(llm_score) * 0.4, 2)
                final_score = max(0.0, min(10.0, final_score))

            if llm_direction in ("bullish", "bearish", "neutral"):
                # LLM 方向与规则引擎一致时采用，不一致时保持规则引擎
                if llm_direction == rule_direction:
                    final_direction = llm_direction
                elif llm_confidence >= 0.7:
                    # LLM 高置信度时覆盖规则引擎方向
                    final_direction = llm_direction

        return {
            "ticker": ticker,
            "final_score": final_score,
            "direction": final_direction,
            "resonance": resonance,
            "supporting_agents": len(valid_results),
            "agent_breakdown": {
                "bullish": bullish_count,
                "bearish": bearish_count,
                "neutral": neutral_count,
            },
            "agent_directions": per_agent_directions,
            "dimension_scores": dim_scores,
            "dimension_confidence": dim_confidence,
            "dimension_weights": dict(self.DIMENSION_WEIGHTS),
            "ml_adjustment": round(ml_adjustment, 3),
            "base_score_before_resonance": round(adjusted_score, 2),
            "pheromone_compact": self.board.compact_snapshot(ticker),
            "data_quality": data_quality_summary,
            "data_real_pct": data_real_pct,
            # Phase 1: LLM 推理增强
            "distill_mode": distill_mode,
            "reasoning": reasoning,
            "key_insight": key_insight,
            "risk_flag": risk_flag,
            "llm_confidence": llm_confidence,
            "rule_score": rule_score,
            "rule_direction": rule_direction,
        }
