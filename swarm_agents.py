#!/usr/bin/env python3
"""
🐝 Alpha Hive 蜂群 Agent 系统 - 7 个自治工蜂（6 核心 + BearBeeContrarian）+ QueenDistiller
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
import logging as _logging
import math
import re as _re

_log = _logging.getLogger("alpha_hive.swarm")

# 预编译正则表达式（#32 性能优化）
_RE_TICKER = _re.compile(r'^[A-Z]{1,5}$')
_RE_INSIDER_SELL = _re.compile(r'内幕卖出\s*\$?([\d,]+)')
_RE_INSIDER_BUY = _re.compile(r'内幕买入\s*\$?([\d,]+)')
_RE_PC_RATIO = _re.compile(r'P/C[:\s]*Ratio[:\s]*([\d.]+)')
_RE_PC_SHORT = _re.compile(r'P/C[:\s]*([\d.]+)')
_RE_IV_RANK = _re.compile(r'IV[:\s]*(?:Rank)?[:\s]*([\d.]+)')
_RE_SENTIMENT = _re.compile(r'情绪\s*(\d+)%')


# ==================== 工具函数 ====================

# yfinance 数据缓存（同一次扫描内共享，避免重复请求）
import time as _time
import threading as _threading

from resilience import yfinance_limiter, yfinance_breaker
from models import DataQualityChecker as _DQChecker

# ── Agent 评分配置（从 config.py 读取，消除 magic numbers）──
try:
    from config import AGENT_SCORING as _AS
except ImportError:
    _AS = {}

def _safe_score(value, default: float = 5.0, lo: float = 0.0, hi: float = 100.0, label: str = "") -> float:
    """
    验证数值安全性：非 None、非 NaN、在 [lo, hi] 范围内。
    不满足条件时返回 default 并记录 debug 日志。
    """
    if value is None:
        if label:
            _log.debug("_safe_score: %s 为 None，使用默认值 %s", label, default)
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        if label:
            _log.debug("_safe_score: %s=%r 无法转为 float，使用默认值 %s", label, value, default)
        return default
    if math.isnan(f) or math.isinf(f):
        if label:
            _log.debug("_safe_score: %s=%r 为 NaN/Inf，使用默认值 %s", label, f, default)
        return default
    return max(lo, min(hi, f))


_yf_cache: Dict[str, Dict] = {}
_yf_cache_ts: Dict[str, float] = {}
_yf_lock = _threading.Lock()
_YF_CACHE_TTL = _AS.get("yfinance_cache_ttl", 120)
_YF_MAX_RETRIES = 2

# ── Ticker 有效性缓存（退市/拆股检测，#18）──
_ticker_validity: Dict[str, Dict] = {}
_TICKER_VALIDITY_TTL = _AS.get("ticker_validity_ttl", 3600)


def check_ticker_validity(ticker: str) -> Dict:
    """
    检测 ticker 是否存在退市/停牌/拆股风险

    返回:
        {
            "valid": bool,          # False = 退市/停牌，应跳过扫描
            "warning": str | None,  # 告警信息（分割/低价等）
            "split_ratio": float | None,  # 近期拆股比例
        }
    """
    now = _time.time()
    cached = _ticker_validity.get(ticker)
    if cached and (now - cached.get("_checked_at", 0)) < _TICKER_VALIDITY_TTL:
        return {k: v for k, v in cached.items() if not k.startswith("_")}

    result: Dict = {"valid": True, "warning": None, "split_ratio": None}

    try:
        import yfinance as _yf
        t = _yf.Ticker(ticker)
        hist = t.history(period="5d")

        # 无交易数据 → 退市/停牌
        if hist.empty:
            result["valid"] = False
            result["warning"] = f"{ticker} 无交易数据（可能已退市或停牌），已跳过扫描"
            _log.warning("⚠️ %s", result["warning"])
            _ticker_validity[ticker] = {**result, "_checked_at": now}
            return result

        # 价格极低 → 退市风险
        price = float(hist["Close"].iloc[-1])
        if price < 0.10:
            result["warning"] = f"{ticker} 价格极低 (${price:.4f})，存在退市风险"
            _log.warning("⚠️ %s", result["warning"])

        # 近30天拆股检测
        try:
            splits = t.splits
            if len(splits) > 0:
                cutoff_ts = now - 30 * 86400
                recent = [
                    (str(idx)[:10], float(ratio))
                    for idx, ratio in splits.items()
                    if hasattr(idx, "timestamp") and idx.timestamp() > cutoff_ts
                ]
                if recent:
                    date_str, ratio = recent[-1]
                    result["split_ratio"] = ratio
                    msg = f"{ticker} 近30天股票分割 ({ratio:.2f}x on {date_str})"
                    # 不覆盖退市告警
                    if not result["warning"]:
                        result["warning"] = msg
                    _log.warning("⚠️ %s", msg)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            pass

    except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
        _log.debug("ticker validity check failed for %s: %s", ticker, e)

    _ticker_validity[ticker] = {**result, "_checked_at": now}
    return result


def _fetch_stock_data(ticker: str) -> Dict:
    """
    从 yfinance 拉取股票实时数据（价格、动量、成交量等）
    内置缓存（2 分钟 TTL）+ RateLimiter + CircuitBreaker + 指数退避重试
    失败时返回默认值，不会抛出异常
    """
    # 检查缓存（持有锁返回副本，防止外部修改）
    with _yf_lock:
        cached = _yf_cache.get(ticker)
        if cached and (_time.time() - _yf_cache_ts.get(ticker, 0)) < _YF_CACHE_TTL:
            return dict(cached)

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

            if len(hist) >= 1:
                data["price"] = float(hist["Close"].iloc[-1])

            if len(hist) >= 5:
                data["momentum_5d"] = (hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100

            if len(hist) >= 2:
                recent_vol = float(hist["Volume"].iloc[-1])
                avg_vol = float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20 else float(hist["Volume"].mean())
                if math.isnan(avg_vol) or avg_vol <= 0:
                    avg_vol = 1.0
                data["avg_volume"] = int(avg_vol)
                data["volume_ratio"] = recent_vol / avg_vol if avg_vol > 0 else 1.0

            if len(hist) >= 20:
                returns = hist["Close"].pct_change().dropna()
                data["volatility_20d"] = float(returns.std() * (252 ** 0.5) * 100)

            # 写入缓存
            with _yf_lock:
                _yf_cache[ticker] = data
                _yf_cache_ts[ticker] = _time.time()
            yfinance_breaker.record_success()
            break

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.warning("yfinance fetch %s attempt %d failed: %s", ticker, attempt, e)
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
        except (AttributeError, TypeError, ValueError) as e:
            _log.debug("History context unavailable for %s: %s", ticker, e)
            return ""

    def _validate_ticker(self, ticker: str) -> Optional[Dict]:
        """验证 ticker 格式（1~5 大写字母，无特殊字符）；无效时返回标准错误结构"""
        if not ticker or not _RE_TICKER.match(str(ticker).strip()):
            _log.warning("%s.analyze() 收到无效 ticker: %r", self.__class__.__name__, ticker)
            return {
                "error": "invalid_ticker",
                "source": self.__class__.__name__,
                "score": 5.0,
                "direction": "neutral",
                "confidence": 0.0,
                "discovery": f"无效 ticker 格式: {ticker!r}（需 1~5 位大写字母）",
                "dimension": "validation",
            }
        return None


def prefetch_shared_data(tickers: list, retriever=None) -> Dict:
    """
    批量预取所有 ticker 的共享数据（yfinance + VectorMemory），
    避免 6 个 Agent 各自重复请求。

    返回: {"stock_data": {ticker: data}, "contexts": {ticker: str}}
    """
    stock_data = {}
    contexts = {}

    # 1+2. 并行预取 yfinance + VectorMemory（I/O bound，并行比串行快 N 倍）
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    _max_w = min(len(tickers), 8)
    if _max_w > 0:
        with ThreadPoolExecutor(max_workers=_max_w, thread_name_prefix="prefetch") as _pex:
            # yfinance 并行
            _yf_futs = {_pex.submit(_fetch_stock_data, t): t for t in tickers}
            for fut in _as_completed(_yf_futs):
                t = _yf_futs[fut]
                try:
                    stock_data[t] = fut.result(timeout=30)
                except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
                    _log.debug("Prefetch yfinance failed for %s: %s", t, e)
                    stock_data[t] = _fetch_stock_data(t)

            # VectorMemory 并行
            if retriever and hasattr(retriever, 'get_context_for_agent'):
                _vm_futs = {_pex.submit(retriever.get_context_for_agent, t, "BeeAgent"): t for t in tickers}
                for fut in _as_completed(_vm_futs):
                    t = _vm_futs[fut]
                    try:
                        contexts[t] = fut.result(timeout=10)
                    except (AttributeError, TypeError, ValueError, Exception) as e:
                        _log.debug("Prefetch context failed for %s: %s", t, e)
                        contexts[t] = ""

    # 3. P5: 批量预取历史预测准确率（给所有 Agent 注入反馈上下文）
    try:
        from backtester import Backtester
        _bt = Backtester()
        _bt_stats = _bt.store.get_accuracy_stats("t7", days=90)
        _by_ticker = _bt_stats.get("by_ticker", {})
        for t in tickers:
            if t in _by_ticker and _by_ticker[t].get("total", 0) >= 2:
                info = _by_ticker[t]
                acc_ctx = (
                    f"|历史T+7准确率{info['accuracy']*100:.0f}%"
                    f"({info['total']}次,均收益{info['avg_return']:+.2f}%)"
                )
                contexts[t] = (contexts.get(t, "") + acc_ctx).strip("|")
    except (ImportError, OSError, ValueError, KeyError, TypeError) as e:
        _log.debug("Prefetch backtest context failed: %s", e)

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
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)

            # ---- 1. SEC EDGAR 内幕交易数据 ----
            insider_data = None
            insider_score = 5.0
            insider_summary = ""
            try:
                from sec_edgar import get_insider_trades
                insider_data = get_insider_trades(ticker, days=90)
                insider_score = _safe_score(insider_data.get("sentiment_score"), 5.0, 0, 10, "insider_score")
                insider_summary = insider_data.get("summary", "")
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.warning("ScoutBeeNova SEC data unavailable for %s: %s", ticker, e)
                insider_summary = f"SEC 数据不可用: {e}"

            # ---- 1b. P2: EDGAR RSS 实时流（当日新鲜 Form 4，先于 REST API 反应）----
            rss_fresh_today = 0
            rss_summary_text = ""
            try:
                from edgar_rss import get_today_form4_alerts
                from sec_edgar import SECEdgarClient as _SEC
                _cik = str(_SEC()._cik_map.get(ticker.upper(), "")) or None
                rss_alerts = get_today_form4_alerts(ticker, cik=_cik)
                if rss_alerts.get("has_fresh_filings"):
                    fresh_n = rss_alerts["fresh_filings_count"]
                    rss_fresh_today = fresh_n
                    rss_summary_text = rss_alerts.get("summary", "")
                    # 当日新鲜申报信号：提升 insider_score 并在 summary 前注明
                    insider_score = min(10.0, insider_score + 0.5 * fresh_n)
                    rss_note = f"[今日{fresh_n}份实时Form4] "
                    insider_summary = rss_note + insider_summary
            except (ImportError, ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
                _log.debug("ScoutBeeNova RSS check skipped for %s: %s", ticker, e)

            # ---- 2. 拥挤度分析（真实数据源）----
            stock = self._get_stock_data(ticker)

            from crowding_detector import CrowdingDetector
            detector = CrowdingDetector(ticker)

            from real_data_sources import get_real_crowding_metrics
            metrics = get_real_crowding_metrics(ticker, stock, self.board)

            crowding_score, component_scores = detector.calculate_crowding_score(metrics)
            crowding_signal = max(1.0, 10.0 - crowding_score / 10.0)

            # ---- 3. 综合评分：内幕交易 + 拥挤度 ----
            _iw = _AS.get("scout_insider_weight", 0.6)
            _cw = _AS.get("scout_crowding_weight", 0.4)
            score = insider_score * _iw + crowding_signal * _cw
            score = max(_AS.get("score_min", 1.0), min(_AS.get("score_max", 10.0), score))

            # 方向判断
            _ch = _AS.get("crowding_high", 70)
            _cl = _AS.get("crowding_low", 30)
            _cn = _AS.get("crowding_sell_neutral", 50)
            if insider_data and insider_data.get("insider_sentiment") == "bullish":
                direction = "bullish"
            elif insider_data and insider_data.get("insider_sentiment") == "bearish":
                if crowding_score > _cn:
                    direction = "bearish"
                else:
                    direction = "neutral"  # 卖出但不拥挤，可能只是计划性减持
            elif crowding_score > _ch:
                direction = "bearish"
            elif crowding_score < _cl:
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

            # ── P1: LLM 内幕交易意图解读（规则引擎无法区分计划性卖出 vs 信心丧失）──
            llm_intent = None
            try:
                import llm_service
                if llm_service.is_available() and insider_data and insider_data.get("total_filings", 0) > 0:
                    llm_intent = llm_service.interpret_insider_trades(ticker, insider_data, stock)
                    if llm_intent:
                        llm_score = llm_intent.get("intent_score", score)
                        # 混合：规则 55% + LLM 意图解读 45%
                        score = round(score * 0.55 + float(llm_score) * 0.45, 2)
                        score = max(1.0, min(10.0, score))
                        intent_label = llm_intent.get("intent_label", "")
                        intent_reason = llm_intent.get("intent_reasoning", "")
                        if intent_reason:
                            discovery = f"{discovery} | LLM意图:{intent_reason}"
                        # LLM 识别到计划性卖出时修正方向
                        if intent_label == "planned_exit" and direction == "bearish":
                            direction = "neutral"
                        elif intent_label == "accumulation" and direction != "bullish":
                            direction = "bullish"
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.debug("ScoutBeeNova LLM unavailable for %s: %s", ticker, e)

            self._publish(ticker, discovery, "sec_edgar+crowding", score, direction)

            # Phase 2: confidence = 数据完整度（内幕数据可用 + 拥挤度可用 + LLM 加成）
            confidence = 0.5
            if insider_data and insider_data.get("total_filings", 0) > 0:
                confidence += 0.3
            dq = metrics.get("data_quality", {})
            real_fields = sum(1 for v in dq.values() if v == "real")
            confidence += min(0.1, real_fields * 0.02)
            if llm_intent:
                confidence += 0.1
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
                        "rss_fresh_today": rss_fresh_today,
                        "rss_summary": rss_summary_text,
                    },
                    "crowding_score": crowding_score,
                    "crowding_signal": round(crowding_signal, 2),
                    "components": component_scores,
                    "adjustment_factor": adj_factor,
                    "momentum_5d": stock["momentum_5d"],
                    "price": stock["price"],
                }
            }

        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("ScoutBeeNova failed for %s: %s", ticker, e, exc_info=True)
            return {"error": str(e), "source": "ScoutBeeNova", "score": 5.0, "dimension": "signal"}


# ==================== OracleBeeEcho (Odds 维度) ====================

class OracleBeeEcho(BeeAgent):
    """市场预期蜂 - 期权分析 + Polymarket 预测市场赔率
    对应维度：Odds (权重 0.15)
    融合：期权信号 60% + Polymarket 赔率 40%
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
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
                options_score = _safe_score(result.get("options_score"), 5.0, 0, 10, "options_score")
                signal_summary = result.get("signal_summary", "平衡")
            except (ImportError, ConnectionError, ValueError, KeyError, TypeError) as e:
                _log.warning("OracleBeeEcho options unavailable for %s: %s", ticker, e)
                result = {}

            # ---- Polymarket 赔率（40%）----
            poly_score = 5.0
            poly_signal = ""
            try:
                from polymarket_client import get_polymarket_odds
                poly = get_polymarket_odds(ticker)
                poly_score = _safe_score(poly.get("odds_score"), 5.0, 0, 10, "poly_score")
                poly_signal = poly.get("odds_signal", "")
                poly_markets = poly.get("markets_found", 0)
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.warning("OracleBeeEcho Polymarket unavailable for %s: %s", ticker, e)
                poly_markets = 0

            # ---- P2: 异常期权流检测（大单 OTM 买入 / 短期扫单）----
            unusual_flow = {}
            unusual_score_adj = 0.0
            try:
                from unusual_options import detect_unusual_flow
                unusual_flow = detect_unusual_flow(ticker, stock_price=current_price)
                if unusual_flow.get("data_source") != "fallback":
                    uf_score = unusual_flow.get("unusual_score", 5.0)
                    uf_dir = unusual_flow.get("unusual_direction", "neutral")
                    # 异常流作为额外调整项（±1.5 分最大影响）
                    unusual_score_adj = (uf_score - 5.0) * 0.3
                    if unusual_flow.get("signals"):
                        top_sig = unusual_flow["signals"][0]
                        signal_summary = f"{signal_summary} | 异常流:{unusual_flow['summary']}"
            except (ImportError, ConnectionError, ValueError, KeyError, TypeError) as e:
                _log.debug("P2 unusual_options 不可用 %s: %s", ticker, e)

            # ---- 融合评分（期权 + Polymarket + 异常流）----
            _ow = _AS.get("oracle_options_weight", 0.55)
            _pw = _AS.get("oracle_poly_weight", 0.35)
            _uw = _AS.get("oracle_unusual_weight", 0.10)
            if poly_markets > 0:
                score = options_score * _ow + poly_score * _pw + 5.0 * _uw
            else:
                score = options_score
            # 叠加异常流调整
            score = max(1.0, min(10.0, score + unusual_score_adj))

            # 从 signal_summary 推断方向（异常流可覆盖）
            if unusual_flow.get("unusual_direction") in ("bullish", "bearish"):
                direction = unusual_flow["unusual_direction"]
            elif "多" in signal_summary or "增强" in signal_summary or "看涨" in signal_summary:
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

            # ── P1: LLM 期权流结构解读（识别聪明钱意图，超越阈值规则）──
            llm_options = None
            try:
                import llm_service
                if llm_service.is_available() and result:
                    llm_options = llm_service.interpret_options_flow(ticker, result, stock)
                    if llm_options:
                        llm_score = llm_options.get("smart_money_score", score)
                        llm_dir = llm_options.get("smart_money_direction", direction)
                        # 混合：规则 60% + LLM 聪明钱解读 40%
                        score = round(score * 0.6 + float(llm_score) * 0.4, 2)
                        score = max(1.0, min(10.0, score))
                        if llm_dir in ("bullish", "bearish", "neutral"):
                            direction = llm_dir
                        flow_reason = llm_options.get("flow_reasoning", "")
                        signal_type = llm_options.get("signal_type", "")
                        if flow_reason:
                            discovery = f"{discovery} | LLM期权:{flow_reason}"
                        if signal_type:
                            discovery = f"{discovery}[{signal_type}]"
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.debug("OracleBeeEcho LLM unavailable for %s: %s", ticker, e)

            self._publish(ticker, discovery, "options+polymarket", score, direction)

            # Phase 2: confidence = 期权数据可用 + Polymarket 可用 + LLM 加成
            confidence = 0.4
            if result:
                confidence += 0.3
            if poly_markets > 0:
                confidence += 0.1
            if llm_options:
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

        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("OracleBeeEcho failed for %s: %s", ticker, e, exc_info=True)
            return {"error": str(e), "source": "OracleBeeEcho", "score": 5.0, "dimension": "odds"}


# ==================== BuzzBeeWhisper (Sentiment 维度) ====================

# ── 情绪基线 SQLite 存储（#13）──
def _sentiment_db_path():
    from pathlib import Path
    from hive_logger import PATHS
    return Path(PATHS.home) / "sentiment_baseline.db"


def _init_sentiment_db():
    """初始化情绪基线 DB（幂等）"""
    import sqlite3 as _sq
    db = _sentiment_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = _sq.connect(str(db))
    conn.execute("""CREATE TABLE IF NOT EXISTS sentiment_baseline (
        ticker TEXT NOT NULL,
        date   TEXT NOT NULL,
        sentiment_pct INTEGER NOT NULL,
        PRIMARY KEY (ticker, date)
    )""")
    conn.commit()
    conn.close()


def _upsert_sentiment(ticker: str, date_str: str, pct: int):
    """写入或更新当日情绪值"""
    import sqlite3 as _sq
    try:
        _init_sentiment_db()
        with _sq.connect(str(_sentiment_db_path())) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sentiment_baseline (ticker, date, sentiment_pct) VALUES (?,?,?)",
                (ticker, date_str, pct),
            )
            conn.execute(
                "DELETE FROM sentiment_baseline WHERE date < date('now', '-60 days')"
            )
    except (OSError, ValueError, TypeError) as _e:
        _log.debug("sentiment_baseline upsert error: %s", _e)


def _get_sentiment_baseline(ticker: str, days: int = 30) -> Optional[float]:
    """获取过去 N 天的平均情绪值（排除今日），无数据返回 None"""
    import sqlite3 as _sq
    try:
        with _sq.connect(str(_sentiment_db_path())) as conn:
            row = conn.execute(
                f"SELECT AVG(sentiment_pct) FROM sentiment_baseline "
                f"WHERE ticker=? AND date < date('now') AND date >= date('now', '-{days} days')",
                (ticker,),
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0])
    except (OSError, ValueError, TypeError) as _e:
        _log.debug("sentiment_baseline query error: %s", _e)
    return None


_SENTIMENT_SPIKE_THRESHOLD = 20   # 偏差超过 20 个百分点触发告警
_SENTIMENT_MIN_DAYS = 5            # 至少 5 天基线才触发告警


def _check_sentiment_spike(ticker: str, current_pct: int, today: str) -> Optional[str]:
    """
    对比当日情绪与 30 天基线，偏差 >THRESHOLD 时触发 Slack 告警。
    返回告警描述字符串（无告警时返回 None）。
    """
    baseline = _get_sentiment_baseline(ticker, days=30)
    if baseline is None:
        return None
    delta = current_pct - baseline
    if abs(delta) < _SENTIMENT_SPIKE_THRESHOLD:
        return None

    direction_str = "看多骤升" if delta > 0 else "看空骤降"
    msg = (
        f"{ticker} 情绪突变 [{direction_str}]：当日 {current_pct}%，"
        f"30日均值 {baseline:.1f}%，偏差 {delta:+.1f}ppt"
    )
    _log.warning("📡 情绪突变告警 %s", msg)
    try:
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        if getattr(n, "enabled", False):
            n.send_risk_alert(
                alert_title=f"{ticker} 情绪突变告警",
                alert_message=msg,
                severity="HIGH" if abs(delta) >= 30 else "MEDIUM",
            )
    except Exception as _se:
        _log.debug("Slack 情绪突变告警发送失败: %s", _se)
    return msg


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
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
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
                                llm_news_score = llm_news.get("sentiment_score", 5.0) * 10
                                news_signal = news_signal * 0.30 + llm_news_score * 0.70
                                news_desc = llm_news.get("key_theme", news_desc)
                                news_reasoning = llm_news.get("reasoning", "")
                                news_mode = "llm_enhanced"
                    except (ImportError, ConnectionError, ValueError, KeyError) as e:
                        _log.debug("LLM news analysis unavailable for %s: %s", ticker, e)
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.warning("BuzzBeeWhisper Finviz news unavailable for %s: %s", ticker, e)
                news_desc = "新闻不可用"

            # 5b. P4: Yahoo Finance + AV 新闻摘要（增强新闻面，与 Finviz 加权融合）
            try:
                from newsapi_client import get_ticker_news
                news_ext = get_ticker_news(ticker, max_articles=8)
                if news_ext.get("is_real_data") and news_ext.get("total_articles", 0) >= 3:
                    ext_signal = news_ext["sentiment_score"] * 10
                    # 融合：Finviz 60% + 扩展新闻 40%（扩展新闻覆盖更广）
                    news_signal = news_signal * 0.60 + ext_signal * 0.40
                    if not news_desc or "不可用" in news_desc:
                        news_desc = news_ext.get("dominant_theme", "")
                    _log.debug("BuzzBeeWhisper news extended for %s: src=%s articles=%d",
                               ticker, news_ext.get("source"), news_ext["total_articles"])
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
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
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
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
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
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
            score = max(_AS.get("score_min", 1.0), min(_AS.get("score_max", 10.0), score))

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
            discovery = " | ".join(p for p in discovery_parts if p)

            if ctx:
                discovery = f"{discovery} | {ctx}"

            self._publish(ticker, discovery, "market_sentiment+reddit", round(score, 2), direction)

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

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "BuzzBeeWhisper",
                "dimension": "sentiment",
                "sentinel_spike": _spike_msg,   # None 或突变告警描述
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

        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("BuzzBeeWhisper failed for %s: %s", ticker, e, exc_info=True)
            return {"error": str(e), "source": "BuzzBeeWhisper", "score": 5.0, "dimension": "sentiment"}


# ==================== ChronosBeeHorizon (Catalyst 维度) ====================

class ChronosBeeHorizon(BeeAgent):
    """催化剂追踪蜂 - 财报、事件、时间线（yfinance 真实日历）
    对应维度：Catalyst (权重 0.20)
    """

    # 催化剂类型基础权重（1.0 = 原始近期事件加分 +1.0 的标准）
    CATALYST_TYPE_WEIGHTS: Dict[str, float] = {
        "earnings":        1.5,   # 财报（价格波动最大，±5~15%）
        "fda_approval":    1.4,   # FDA 批准/拒绝（二元事件，影响极大）
        "merger":          1.3,   # 并购
        "product_launch":  1.2,   # 重大产品发布
        "regulatory":      1.1,   # 监管决定
        "guidance":        1.0,   # 业绩指引更新
        "economic_event":  0.9,   # 宏观经济事件
        "investor_day":    0.7,   # 投资者日/分析师日
        "analyst_day":     0.7,   # 同上
        "conference":      0.5,   # 行业会议（信息量有限）
        "split":           0.8,   # 股票拆分
        "dividend":        0.4,   # 股息（稳定性高，但价格冲击小）
        "exDividendDate":  0.4,   # 除息日
        "dividendDate":    0.3,   # 分红到账日
    }
    _CATALYST_TYPE_DEFAULT = 0.7   # 未知类型默认权重

    # 催化剂严重程度乘数（与 CatalystSeverity 对应）
    CATALYST_SEVERITY_MULT: Dict[str, float] = {
        "critical": 1.3,
        "high":     1.1,
        "medium":   1.0,
        "low":      0.8,
    }

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
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
            except (ConnectionError, TimeoutError, ValueError, KeyError, AttributeError, OSError) as e:
                _log.warning("ChronosBeeHorizon yfinance calendar unavailable for %s: %s", ticker, e)

            # 2. 补充 CatalystTimeline（已有的硬编码催化剂）
            try:
                from catalyst_refinement import create_nvda_catalysts, create_vktx_catalysts
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
            except (ImportError, ValueError, AttributeError) as e:
                _log.debug("CatalystTimeline unavailable for %s: %s", ticker, e)

            # 评分逻辑
            if catalysts_found:
                # 按天数排序
                catalysts_found.sort(key=lambda c: c.get("days_until", 999))

                # 基础分 + 按事件类型 × 严重程度的加权加分
                # 近期（7天内）× 1.0；中期（8~30天）× 0.3；超出 30 天不计分
                base = 5.5
                score = base
                imminent = []
                for c in catalysts_found:
                    days = c.get("days_until", 999)
                    event_type = c.get("type", "")
                    severity = c.get("severity", "medium")
                    type_w = self.CATALYST_TYPE_WEIGHTS.get(event_type, self._CATALYST_TYPE_DEFAULT)
                    sev_m = self.CATALYST_SEVERITY_MULT.get(severity, 1.0)
                    if days <= 7:
                        score += 1.0 * type_w * sev_m
                        imminent.append(c)
                    elif days <= 30:
                        score += 0.3 * type_w * sev_m
                score = min(10.0, score)

                nearest = catalysts_found[0]
                discovery = f"催化剂 {len(catalysts_found)} 个 | 最近：{nearest['event']}（{nearest.get('days_until', '?')}天后）"

                # 方向：需要有高影响力的近期催化剂（type_w >= 1.2）才 bullish
                # 普通事件（会议/股息）不足以推断看多方向
                high_impact_imminent = [
                    c for c in imminent
                    if self.CATALYST_TYPE_WEIGHTS.get(c.get("type", ""), self._CATALYST_TYPE_DEFAULT) >= 1.2
                ]
                if score >= 7.5 and high_impact_imminent:
                    direction = "bullish"
                elif score <= 4.5:
                    direction = "bearish"
                else:
                    direction = "neutral"
            else:
                score = 4.0
                discovery = "无近期催化剂"
                direction = "neutral"

            if ctx:
                discovery = f"{discovery} | {ctx}"

            # ── P1: LLM 催化剂影响力解读（规则引擎不知道财报方向是利多还是利空）──
            llm_catalyst = None
            try:
                import llm_service
                stock_for_llm = self._get_stock_data(ticker)
                if llm_service.is_available() and catalysts_found:
                    llm_catalyst = llm_service.interpret_catalyst_impact(
                        ticker, catalysts_found, stock_for_llm
                    )
                    if llm_catalyst:
                        llm_score = llm_catalyst.get("impact_score", score)
                        llm_dir = llm_catalyst.get("impact_direction", direction)
                        # 混合：规则 35% + LLM 催化剂解读 65%（催化剂判断最依赖语义理解）
                        score = round(score * 0.35 + float(llm_score) * 0.65, 2)
                        score = max(1.0, min(10.0, score))
                        if llm_dir in ("bullish", "bearish", "neutral"):
                            direction = llm_dir
                        impact_reason = llm_catalyst.get("impact_reasoning", "")
                        key_cat = llm_catalyst.get("key_catalyst", "")
                        if impact_reason:
                            discovery = f"{discovery} | LLM催化剂:{impact_reason}"
                        if key_cat:
                            discovery = f"{discovery}[关注:{key_cat}]"
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.debug("ChronosBeeHorizon LLM unavailable for %s: %s", ticker, e)

            self._publish(ticker, discovery, "catalyst_timeline", score, direction)

            # Phase 2: confidence = 催化剂数量和来源多样性 + LLM 加成
            confidence = 0.3  # baseline
            if catalysts_found:
                confidence += min(0.3, len(catalysts_found) * 0.08)
                has_yf = any(c.get("type") == "earnings" for c in catalysts_found)
                if has_yf:
                    confidence += 0.15
            if llm_catalyst:
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
                    "llm_impact": "llm_enhanced" if llm_catalyst else "rule_only",
                },
                "details": {"catalysts": catalysts_found[:5]}
            }

        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("ChronosBeeHorizon failed for %s: %s", ticker, e, exc_info=True)
            return {"error": str(e), "source": "ChronosBeeHorizon", "score": 5.0, "dimension": "catalyst"}


# ==================== RivalBeeVanguard (ML 辅助) ====================

class RivalBeeVanguard(BeeAgent):
    """竞争分析与 ML 预测蜂 - 概率预测 + 行业动量对标
    不直接参与 5 维公式，作为额外调整项
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
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
            except (ImportError, ValueError, KeyError, TypeError) as e:
                _log.warning("RivalBeeVanguard ML prediction unavailable for %s: %s", ticker, e)

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

        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("RivalBeeVanguard failed for %s: %s", ticker, e, exc_info=True)
            return {"error": str(e), "source": "RivalBeeVanguard", "score": 5.0, "dimension": "ml_auxiliary"}


# ==================== GuardBeeSentinel (RiskAdj 维度) ====================

class GuardBeeSentinel(BeeAgent):
    """交叉验证与风险评估蜂 - 共振检测 + 拥挤度折扣 + 风险调整
    对应维度：RiskAdj (权重 0.15)
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
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
            except (ImportError, ValueError, KeyError, TypeError) as e:
                _log.warning("GuardBeeSentinel crowding analysis unavailable for %s: %s", ticker, e)

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

            # ── P5: FRED 宏观环境过滤（risk_off 时主动降权，risk_on 时小幅增强）──
            macro_adj = 0.0
            macro_desc = ""
            try:
                from fred_macro import get_macro_context, get_macro_risk_adjustment
                macro = get_macro_context()
                macro_adj, macro_desc = get_macro_risk_adjustment(macro)
                if macro_adj != 0.0:
                    score = max(1.0, min(10.0, score + macro_adj))
                    discovery = f"{discovery} | 宏观:{macro.get('summary', '')}"
                    if macro_desc:
                        discovery = f"{discovery}({macro_desc[:40]})"
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.debug("P5 fred_macro 不可用 %s: %s", ticker, e)

            # ── P1: LLM 冲突合成（识别哪种矛盾更危险，规则引擎只看一致性百分比）──
            llm_guard = None
            try:
                import llm_service
                if llm_service.is_available() and top_signals:
                    pheromone_snap = self.board.snapshot()
                    ticker_snap = [e for e in pheromone_snap if e.get("ticker") == ticker]
                    if ticker_snap:
                        llm_guard = llm_service.synthesize_agent_conflicts(
                            ticker, ticker_snap, resonance
                        )
                        if llm_guard:
                            llm_risk = llm_guard.get("risk_score", 5.0)
                            conflict_type = llm_guard.get("conflict_type", "coherent")
                            guard_reason = llm_guard.get("guard_reasoning", "")
                            rec_action = llm_guard.get("recommended_action", "proceed")
                            # risk_score 高 → 降低 guard 分（对蜂群总分施加保守修正）
                            if conflict_type == "major_conflict":
                                score = max(1.0, score * 0.75)
                                direction = "neutral"
                            elif conflict_type == "minor_divergence":
                                score = max(1.0, score * 0.9)
                            if guard_reason:
                                discovery = f"{discovery} | LLM冲突检测:{guard_reason}"
                            if rec_action == "avoid":
                                discovery = f"{discovery}[⚠建议回避]"
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.debug("GuardBeeSentinel LLM unavailable for %s: %s", ticker, e)

            self._publish(ticker, discovery, "guard_bee_sentinel", round(score, 2), direction)

            # Phase 2: confidence = 信号板有数据 + 一致性高 + LLM 冲突评估
            confidence = 0.4
            if top_signals:
                confidence += 0.25
            if consistency >= 0.7:
                confidence += 0.15
            if resonance["resonance_detected"]:
                confidence += 0.1
            if llm_guard:
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
                    "llm_conflict": "llm_enhanced" if llm_guard else "rule_only",
                },
                "details": {
                    "resonance": resonance,
                    "top_signals_count": len(top_signals),
                    "consistency": consistency,
                    "adjustment_factor": adj_factor,
                    "llm_conflict_type": llm_guard.get("conflict_type", "") if llm_guard else "",
                    "llm_recommended_action": llm_guard.get("recommended_action", "") if llm_guard else "",
                }
            }

        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("GuardBeeSentinel failed for %s: %s", ticker, e, exc_info=True)
            return {"error": str(e), "source": "GuardBeeSentinel", "score": 5.0, "dimension": "risk_adj"}


# ==================== BearBeeContrarian (看空对冲蜂) ====================

class BearBeeContrarian(BeeAgent):
    """看空对冲蜂 - 专门寻找看空信号，平衡蜂群的系统性看多偏差
    独立维度：contrarian（不参与 5 维评分，但影响方向投票）

    **二阶段执行**：在其他 6 个 Agent 完成后运行，从信息素板读取已有数据，
    避免重复 API 调用导致限流失败。

    分析维度：
    1. 内幕卖出强度（从 ScoutBeeNova 信息素板读取，回退 SEC 直查）
    2. 估值泡沫（P/E 过高、涨幅过大 — 使用预取 yfinance 数据）
    3. 期权看跌信号（从 OracleBeeEcho 信息素板读取，回退期权模块）
    4. 动量衰减（使用预取 yfinance 数据）
    5. 新闻看空信号（从 BuzzBeeWhisper 信息素板读取，回退 Finviz）
    """

    def _read_board_entry(self, ticker: str, agent_id_prefix: str) -> Optional[PheromoneEntry]:
        """从信息素板读取指定 Agent 对指定 ticker 的最新条目"""
        if not self.board:
            return None
        entries = self.board.get_top_signals(ticker=ticker, n=20)
        for e in entries:
            if e.agent_id.startswith(agent_id_prefix):
                return e
        return None

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)
            stock = self._get_stock_data(ticker)
            bearish_signals = []
            bearish_score = 0.0  # 看空严重程度 0-10
            total_weight = 0.0
            data_sources = {}  # 跟踪数据来源

            # ===== 1. 内幕卖出强度（优先从 ScoutBeeNova 信息素板读取）=====
            insider_bear = 0.0
            insider_data = None

            # 先尝试从信息素板读取 ScoutBeeNova 已发布的内幕数据
            scout_entry = self._read_board_entry(ticker, "ScoutBee")
            if scout_entry and scout_entry.discovery:
                disc = scout_entry.discovery
                data_sources["insider"] = "real"  # ScoutBee 真实 SEC 数据（经信息素板中转）
                # 解析 ScoutBeeNova 的 discovery 文本提取内幕数据
                sell_match = _RE_INSIDER_SELL.search(disc)
                buy_match = _RE_INSIDER_BUY.search(disc)
                sold = int(sell_match.group(1).replace(',', '')) if sell_match else 0
                bought = int(buy_match.group(1).replace(',', '')) if buy_match else 0

                if sold > 0 or bought > 0:
                    insider_data = {"dollar_sold": sold, "dollar_bought": bought}
                    if sold > bought * 3 and sold > 1_000_000:
                        insider_bear = 8.0
                        bearish_signals.append(f"内幕大额抛售 ${sold:,.0f}（买入仅 ${bought:,.0f}）")
                    elif sold > bought * 2 and sold > 500_000:
                        insider_bear = 6.5
                        bearish_signals.append(f"内幕卖多买少 卖${sold:,.0f}/买${bought:,.0f}")
                    elif sold > bought and sold > 100_000:
                        insider_bear = 5.0
                        bearish_signals.append(f"内幕净卖出 ${sold:,.0f}")

                # 也检查 ScoutBeeNova 方向（bearish = 内幕看空信号强）
                if scout_entry.direction == "bearish" and insider_bear < 6.0:
                    insider_bear = max(insider_bear, 6.0)
                    if not any("内幕" in s for s in bearish_signals):
                        bearish_signals.append(f"Scout 内幕信号看空（{scout_entry.self_score:.1f}分）")

            # 回退：直接调用 SEC API
            if not insider_data:
                try:
                    from sec_edgar import get_insider_trades
                    insider_data = get_insider_trades(ticker, days=90)
                    if insider_data:
                        data_sources["insider"] = "sec_api"
                        sold = insider_data.get("dollar_sold", 0)
                        bought = insider_data.get("dollar_bought", 0)
                        sentiment = insider_data.get("insider_sentiment", "neutral")
                        if sentiment == "bearish":
                            insider_bear = 7.0
                            bearish_signals.append(f"内幕人净卖出 ${sold:,.0f}")
                        elif sold > bought * 3 and sold > 1_000_000:
                            insider_bear = 8.0
                            bearish_signals.append(f"内幕大额抛售 ${sold:,.0f}（买入仅 ${bought:,.0f}）")
                        elif sold > bought * 2:
                            insider_bear = 5.5
                            bearish_signals.append(f"内幕卖多买少 卖${sold:,.0f}/买${bought:,.0f}")
                except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                    _log.warning("BearBeeContrarian SEC fallback failed for %s: %s", ticker, e)
                    data_sources["insider"] = "unavailable"

            bearish_score += insider_bear * 0.25
            total_weight += 0.25

            # ===== 2. 估值/涨幅过热（使用预取 yfinance 数据）=====
            overval_bear = 0.0
            mom_5d = stock.get("momentum_5d", 0)
            price = stock.get("price", 0) or stock.get("current_price", 0)

            # 获取 P/E（从 yfinance 缓存）
            pe = stock.get("pe_ratio", 0)
            if not pe and price > 0:
                try:
                    import yfinance as yf
                    info = yf.Ticker(ticker).fast_info
                    pe = getattr(info, 'pe_ratio', 0) or 0
                except (ConnectionError, TimeoutError, ValueError, KeyError, AttributeError, OSError) as e:
                    _log.debug("BearBeeContrarian PE ratio unavailable for %s: %s", ticker, e)
                    pe = 0

            if mom_5d > 15:
                overval_bear = 8.0
                bearish_signals.append(f"5日暴涨 {mom_5d:+.1f}%（超买）")
            elif mom_5d > 8:
                overval_bear = 6.0
                bearish_signals.append(f"5日涨幅过大 {mom_5d:+.1f}%")
            elif mom_5d > 5:
                overval_bear = 4.0
                bearish_signals.append(f"5日涨幅 {mom_5d:+.1f}%（关注回调风险）")

            if pe and pe > 80:
                overval_bear = max(overval_bear, 7.0)
                bearish_signals.append(f"P/E 极高 {pe:.1f}（估值泡沫风险）")
            elif pe and pe > 50:
                overval_bear = max(overval_bear, 5.0)
                bearish_signals.append(f"P/E 偏高 {pe:.1f}")
            elif pe and pe > 35:
                overval_bear = max(overval_bear, 3.5)
                bearish_signals.append(f"P/E {pe:.1f}（高于市场中位数）")

            data_sources["valuation"] = "yfinance"
            bearish_score += overval_bear * 0.20
            total_weight += 0.20

            # ===== 3. 期权看跌信号（优先从 OracleBeeEcho 信息素板读取）=====
            options_bear = 0.0
            options_data = None

            # 先尝试从信息素板读取 OracleBeeEcho 已发布的期权数据
            oracle_entry = self._read_board_entry(ticker, "OracleBee")
            if oracle_entry and oracle_entry.discovery:
                disc = oracle_entry.discovery
                data_sources["options"] = "real"  # OracleBee 真实期权数据（经信息素板中转）
                pc_match = _RE_PC_RATIO.search(disc)
                if not pc_match:
                    pc_match = _RE_PC_SHORT.search(disc)
                iv_match = _RE_IV_RANK.search(disc)

                pc_ratio = float(pc_match.group(1)) if pc_match else None
                iv_rank = float(iv_match.group(1)) if iv_match else None

                if pc_ratio and pc_ratio > 1.5:
                    options_bear = 8.0
                    bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（强看跌信号）")
                elif pc_ratio and pc_ratio > 1.2:
                    options_bear = 6.0
                    bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（偏看跌）")
                elif pc_ratio and pc_ratio > 1.0:
                    options_bear = 4.0
                    bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（略偏空）")

                if iv_rank and iv_rank > 80:
                    options_bear = max(options_bear, 7.0)
                    bearish_signals.append(f"IV Rank {iv_rank:.0f}（恐慌高位）")
                elif iv_rank and iv_rank > 60:
                    options_bear = max(options_bear, 5.0)
                    bearish_signals.append(f"IV Rank {iv_rank:.0f}（波动偏高）")

                # 检查 OracleBeeEcho 的方向
                if oracle_entry.direction == "bearish" and options_bear < 5.0:
                    options_bear = max(options_bear, 5.5)
                    if not any("P/C" in s for s in bearish_signals):
                        bearish_signals.append(f"Oracle 期权信号看空（{oracle_entry.self_score:.1f}分）")

                options_data = {"pc_ratio": pc_ratio, "iv_rank": iv_rank}

            # 回退：直接调用期权分析模块
            if not options_data:
                try:
                    from options_analyzer import OptionsAnalyzer
                    opt = OptionsAnalyzer()
                    result = opt.analyze(ticker, stock_price=price if price > 0 else None)
                    if result:
                        data_sources["options"] = "options_api"
                        pc_ratio = result.get("put_call_ratio", 1.0)
                        iv_rank = result.get("iv_rank", 50)
                        if pc_ratio > 1.5:
                            options_bear = 8.0
                            bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（强看跌）")
                        elif pc_ratio > 1.2:
                            options_bear = 6.0
                            bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（偏看跌）")
                        if iv_rank > 80:
                            options_bear = max(options_bear, 7.0)
                            bearish_signals.append(f"IV Rank {iv_rank:.0f}（恐慌高位）")
                except (ImportError, ConnectionError, ValueError, KeyError, TypeError) as e:
                    _log.warning("BearBeeContrarian options fallback failed for %s: %s", ticker, e)
                    data_sources["options"] = "unavailable"

            bearish_score += options_bear * 0.25
            total_weight += 0.25

            # ===== 4. 动量衰减 / 量能萎缩（使用预取 yfinance 数据）=====
            momentum_bear = 0.0
            vol_ratio = stock.get("volume_ratio", 1.0)
            volatility = stock.get("volatility_20d", 0)

            if mom_5d < -5:
                momentum_bear = 7.5
                bearish_signals.append(f"5日下跌 {mom_5d:+.1f}%")
            elif mom_5d < -2:
                momentum_bear = 5.5
                bearish_signals.append(f"动量转弱 {mom_5d:+.1f}%")
            elif mom_5d < 0:
                momentum_bear = 3.0
                bearish_signals.append(f"近期小幅回调 {mom_5d:+.1f}%")

            if 0.01 < vol_ratio < 0.5:
                momentum_bear = max(momentum_bear, 5.0)
                bearish_signals.append(f"量能萎缩 {vol_ratio:.1f}x（参与度下降）")
            elif vol_ratio > 3.0 and mom_5d < 0:
                momentum_bear = max(momentum_bear, 7.0)
                bearish_signals.append(f"放量下跌 {vol_ratio:.1f}x | {mom_5d:+.1f}%")
            elif vol_ratio > 2.0 and mom_5d < 0:
                momentum_bear = max(momentum_bear, 5.5)
                bearish_signals.append(f"量增价跌 {vol_ratio:.1f}x | {mom_5d:+.1f}%")

            if volatility > 50:
                momentum_bear = max(momentum_bear, 5.5)
                bearish_signals.append(f"高波动率 {volatility:.0f}%（年化）")

            data_sources["momentum"] = "yfinance"
            bearish_score += momentum_bear * 0.15
            total_weight += 0.15

            # ===== 5. 新闻看空信号（优先从 BuzzBeeWhisper 信息素板读取）=====
            news_bear = 0.0

            # 先尝试从信息素板读取 BuzzBeeWhisper 的情绪数据
            buzz_entry = self._read_board_entry(ticker, "BuzzBee")
            if buzz_entry and buzz_entry.discovery:
                disc = buzz_entry.discovery
                data_sources["news"] = "real"  # BuzzBee 真实情绪数据（经信息素板中转）
                sent_match = _RE_SENTIMENT.search(disc)
                if sent_match:
                    sentiment_pct = int(sent_match.group(1))
                    if sentiment_pct < 30:
                        news_bear = 7.5
                        bearish_signals.append(f"市场情绪极度悲观 {sentiment_pct}%")
                    elif sentiment_pct < 40:
                        news_bear = 6.0
                        bearish_signals.append(f"市场情绪偏空 {sentiment_pct}%")
                    elif sentiment_pct < 45:
                        news_bear = 4.0
                        bearish_signals.append(f"市场情绪略偏谨慎 {sentiment_pct}%")

                # 检查 BuzzBeeWhisper 的方向
                if buzz_entry.direction == "bearish" and news_bear < 5.0:
                    news_bear = max(news_bear, 5.5)
                    bearish_signals.append(f"Buzz 情绪分析看空（{buzz_entry.self_score:.1f}分）")

            # 回退：直接调用 Finviz
            if news_bear == 0.0:
                try:
                    from finviz_sentiment import get_finviz_sentiment
                    finviz = get_finviz_sentiment(ticker)
                    if finviz and isinstance(finviz, dict):
                        data_sources["news"] = "finviz_api"
                        news_score = finviz.get("news_score", 5.0)
                        neg = len(finviz.get("top_bearish", []))
                        pos = len(finviz.get("top_bullish", []))
                        if news_score < 3.5:
                            news_bear = 7.0
                            bearish_signals.append(f"新闻情绪偏空（评分 {news_score:.1f}/10）")
                        elif news_score < 4.5:
                            news_bear = 5.0
                            bearish_signals.append(f"新闻略偏空（评分 {news_score:.1f}/10）")
                        if neg > pos * 2 and neg >= 3:
                            news_bear = max(news_bear, 6.5)
                            bearish_signals.append(f"负面新闻主导（{neg}空 vs {pos}多）")
                except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                    _log.warning("BearBeeContrarian Finviz news fallback failed for %s: %s", ticker, e)
                    if "news" not in data_sources:
                        data_sources["news"] = "unavailable"

            bearish_score += news_bear * 0.15
            total_weight += 0.15

            # ===== 综合看空评分 =====
            if total_weight > 0:
                rule_bear_score = bearish_score / total_weight
            else:
                rule_bear_score = 5.0

            # 若完全无数据但其他 Agent 都看多，给出温和的"谨慎提醒"
            if not bearish_signals:
                # 检查价格本身是否存在过热风险
                if price > 0 and mom_5d >= 0:
                    bearish_signals.append(f"当前价 ${price:.2f} | 暂无明显看空信号，但建议设置止损")
                    rule_bear_score = 3.0
                else:
                    rule_bear_score = 2.0

            # ===== LLM 看空论点生成 =====
            llm_thesis = ""
            llm_key_risks = []
            llm_contrarian_insight = ""
            llm_thesis_break = ""
            final_bear_score = rule_bear_score

            try:
                import llm_service
                if llm_service.is_available():
                    # 收集 bull_signals（从信息素板读取其他 Agent 的看多信号）
                    bull_signals = []
                    if self.board:
                        for e in self.board.get_top_signals(ticker=ticker, n=20):
                            if e.direction == "bullish" and not e.agent_id.startswith("BearBee"):
                                bull_signals.append({
                                    "agent": e.agent_id,
                                    "score": e.self_score,
                                    "discovery": e.discovery[:120],
                                })

                    llm_bear = llm_service.generate_bear_thesis(
                        ticker=ticker,
                        bull_signals=bull_signals,
                        bear_signals=bearish_signals,
                        insider_data=insider_data,
                        options_data=options_data,
                        news_data={"buzz_entry": buzz_entry.discovery[:200] if buzz_entry else None},
                    )
                    if llm_bear:
                        llm_bear_score = llm_bear.get("bear_score")
                        if llm_bear_score is not None and isinstance(llm_bear_score, (int, float)):
                            # 混合：规则 55% + LLM 45%
                            final_bear_score = round(rule_bear_score * 0.55 + float(llm_bear_score) * 0.45, 2)
                            final_bear_score = max(0.0, min(10.0, final_bear_score))
                        llm_thesis = llm_bear.get("thesis", "")
                        llm_key_risks = llm_bear.get("key_risks", [])
                        llm_contrarian_insight = llm_bear.get("contrarian_insight", "")
                        llm_thesis_break = llm_bear.get("thesis_break", "")
                        data_sources["llm_bear"] = "llm_enhanced"
                        if llm_thesis:
                            bearish_signals.append(f"LLM看空论点: {llm_thesis[:80]}")
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.debug("BearBeeContrarian LLM unavailable for %s: %s", ticker, e)

            # 反转为看空分：bear_score 越高 → 越看空 → 给蜂群一个低分
            # score 代表"该标的的吸引力"：看空信号强 = 低分
            score = max(1.0, min(10.0, 10.0 - final_bear_score))

            if final_bear_score >= 6.5:
                direction = "bearish"
            elif final_bear_score >= 4.5:
                direction = "neutral"
            else:
                direction = "bullish"  # 找不到看空理由 = 确认看多

            if bearish_signals:
                discovery = " | ".join(bearish_signals[:6])
            else:
                discovery = "未发现显著看空信号"

            if ctx:
                discovery = f"{discovery} | {ctx}"

            self._publish(ticker, discovery, "bear_contrarian", round(score, 2), direction)

            confidence = min(1.0, 0.3 + len(bearish_signals) * 0.1)
            # 信息素板数据可用时增加置信度
            board_sources = sum(1 for v in data_sources.values() if v == "pheromone_board")
            confidence = min(1.0, confidence + board_sources * 0.1)
            # LLM 可用时额外增加置信度
            if llm_thesis:
                confidence = min(1.0, confidence + 0.1)

            return {
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 2),
                "discovery": discovery,
                "source": "BearBeeContrarian",
                "dimension": "contrarian",
                "data_quality": data_sources,
                "llm_thesis": llm_thesis,
                "llm_key_risks": llm_key_risks,
                "llm_contrarian_insight": llm_contrarian_insight,
                "llm_thesis_break": llm_thesis_break,
                "details": {
                    "bear_score": round(final_bear_score, 2),
                    "rule_bear_score": round(rule_bear_score, 2),
                    "bearish_signals": bearish_signals,
                    "insider_bear": round(insider_bear, 1),
                    "overval_bear": round(overval_bear, 1),
                    "options_bear": round(options_bear, 1),
                    "momentum_bear": round(momentum_bear, 1),
                    "news_bear": round(news_bear, 1),
                    "data_sources": data_sources,
                }
            }

        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("BearBeeContrarian failed for %s: %s", ticker, e, exc_info=True)
            return {"error": str(e), "source": "BearBeeContrarian", "score": 5.0, "dimension": "contrarian"}


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
            # 优先从 config.EVALUATION_WEIGHTS 读取，确保权重配置单一入口
            try:
                from config import EVALUATION_WEIGHTS
                valid_dims = set(self.DEFAULT_WEIGHTS.keys())
                # 只保留已实现的维度（防止 config 中存在未对应 Agent 的维度）
                cfg_weights = {k: v for k, v in EVALUATION_WEIGHTS.items() if k in valid_dims}
                # 用 DEFAULT_WEIGHTS 补全缺失维度
                merged = dict(self.DEFAULT_WEIGHTS)
                merged.update(cfg_weights)
                self.DIMENSION_WEIGHTS = merged
            except (ImportError, AttributeError):
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
                dim_scores[dim] = _safe_score(r.get("score"), 5.0, 0, 10, f"dim_{dim}")
                dim_confidence[dim] = _safe_score(r.get("confidence"), 0.5, 0, 1.0, f"conf_{dim}")

        # 2.5 维度状态追踪（NA1：可视化哪些维度缺失及原因）
        dim_status: Dict[str, str] = {}    # present / absent / error
        dim_missing_reason: Dict[str, str] = {}
        for dim in self.DIMENSION_WEIGHTS:
            if dim in dim_scores:
                dim_status[dim] = "present"
            else:
                # 区分"Agent 报错"还是"完全没返回"
                error_result = next(
                    (r for r in all_results if r.get("dimension") == dim and "error" in r), None
                )
                if error_result:
                    dim_status[dim] = "error"
                    dim_missing_reason[dim] = str(error_result["error"])[:80]
                else:
                    dim_status[dim] = "absent"
                    dim_missing_reason[dim] = "Agent 未返回该维度"
        present_count = sum(1 for s in dim_status.values() if s == "present")
        _n_dims = len(self.DIMENSION_WEIGHTS)
        dimension_coverage_pct = round(present_count / _n_dims * 100, 1) if _n_dims else 100.0

        # 3. ML 辅助分（按 confidence 缩放影响力）
        ml_adjustment = 0.0
        for r in valid_results:
            if r.get("dimension") == "ml_auxiliary":
                ml_score = r.get("score", 5.0)
                ml_conf = r.get("confidence", 0.5)
                ml_adjustment = (ml_score - 5.0) * 0.1 * ml_conf

        # 4. 5 维 confidence-weighted 评分
        # 新逻辑：低 confidence 缩小该维度的有效权重，而非把分拉向 5.0
        # conf >= 0.5 → 全权重（不干预）
        # conf <  0.5 → 线性缩小（conf=0.5 全权重；conf=0 权重=0）
        # 优势：保留维度的实际观点（8.0 就是 8.0），高分+低置信度不会被错误地拉到 6 分
        weighted_sum = 0.0
        weight_total = 0.0
        for dim, weight in self.DIMENSION_WEIGHTS.items():
            if dim in dim_scores:
                conf = dim_confidence.get(dim, 0.5)
                # conf < 0.5 时线性缩小权重；conf >= 0.5 保持全权重
                effective_weight = weight * min(1.0, conf * 2)
                weighted_sum += dim_scores[dim] * effective_weight
                weight_total += effective_weight
            else:
                # 缺失维度：5.0 中性分，全权重锚定（与旧逻辑一致）
                weighted_sum += 5.0 * weight
                weight_total += weight

        base_score = weighted_sum / weight_total if weight_total > 0 else 5.0

        # 5. ML 调整
        adjusted_score = base_score + ml_adjustment

        # 6. 共振增强
        resonance = self.board.detect_resonance(ticker)
        if resonance["resonance_detected"]:
            boost_pct = _safe_score(resonance.get("confidence_boost"), 0.0, -50, 50, "resonance_boost")
            rule_score = adjusted_score * (1.0 + boost_pct / 100.0)
        else:
            rule_score = adjusted_score

        rule_score = round(max(0.0, min(10.0, rule_score)), 2)

        # 6.5. BearBeeContrarian 看空强度上限
        # 反对蜂的 score = 10 - bear_strength（反向映射），bear_strength 越高看空越强
        # bear_strength > 7.0 时对 rule_score 施加软上限，防止强看空信号下评分虚高
        contrarian_result = next(
            (r for r in valid_results if r.get("dimension") == "contrarian"), None
        )
        bear_strength = 0.0
        bear_cap_applied = False
        if contrarian_result is not None:
            bear_strength = round(10.0 - contrarian_result.get("score", 5.0), 2)
            if bear_strength > 7.0:
                # bear=7.5 → cap=9.75; bear=8.0 → cap=9.5; bear=9.0 → cap=9.0; bear=10.0 → cap=8.5
                bear_cap = round(10.0 - (bear_strength - 7.0) * 0.5, 2)
                if rule_score > bear_cap:
                    _log.info(
                        "%s BearBee 看空强度 %.1f → 上限 %.2f（原 %.2f）",
                        ticker, bear_strength, bear_cap, rule_score,
                    )
                    rule_score = bear_cap
                    bear_cap_applied = True

        # 6.7. GuardBeeSentinel 风险关门（NA4）
        # risk_adj < 4.0 时施加额外折扣，防止高风险标的被虚高评分淹没
        # 折扣公式：penalty = (4.0 - guard_score) / 4.0 * 0.8，最大 -0.8 分
        # guard=3.9 → -0.02（可忽略）；guard=2.0 → -0.40；guard=0.0 → -0.80
        guard_result = next(
            (r for r in valid_results if r.get("dimension") == "risk_adj"), None
        )
        guard_penalty = 0.0
        guard_penalty_applied = False
        if guard_result is not None:
            guard_score = _safe_score(guard_result.get("score"), 5.0, 0, 10, "guard_score")
            if guard_score < 4.0:
                guard_penalty = round((4.0 - guard_score) / 4.0 * 0.8, 3)
                pre_guard = rule_score
                rule_score = round(max(rule_score - guard_penalty, 2.0), 2)
                if rule_score < pre_guard:
                    guard_penalty_applied = True
                    _log.info(
                        "%s GuardBee 风险关门: guard_score=%.1f penalty=%.3f %.2f→%.2f",
                        ticker, guard_score, guard_penalty, pre_guard, rule_score,
                    )

        # 7. 置信度加权多数投票（NA3）
        # 旧逻辑：原始计数，2个高置信度看多会被5个低置信度看空淹没
        # 新逻辑：各 Agent 票重 = 其 confidence，高置信度 Agent 影响力更大
        directions = [r.get("direction", "neutral") for r in valid_results]
        bullish_count = directions.count("bullish")
        bearish_count = directions.count("bearish")
        neutral_count = directions.count("neutral")

        bullish_w = sum(r.get("confidence", 0.5) for r in valid_results if r.get("direction") == "bullish")
        bearish_w = sum(r.get("confidence", 0.5) for r in valid_results if r.get("direction") == "bearish")
        neutral_w = sum(r.get("confidence", 0.5) for r in valid_results if r.get("direction") == "neutral")
        total_w = bullish_w + bearish_w + neutral_w or 1.0

        if bullish_w > bearish_w and bullish_w / total_w >= 0.4:
            rule_direction = "bullish"
        elif bearish_w > bullish_w and bearish_w / total_w >= 0.4:
            rule_direction = "bearish"
        else:
            rule_direction = "neutral"

        # 8. Agent 方向
        per_agent_directions = {}
        for r in all_results:
            src = r.get("source", "")
            if src:
                per_agent_directions[src] = r.get("direction", "neutral")

        # 9. data_quality 汇总（三级评分：real=1.0, proxy=0.7, fallback=0）
        REAL_SOURCES = {
            "real", "yfinance", "finviz_api", "options_api",
            "keyword", "llm_enhanced", "reddit_apewisdom",
            "rule_only",   # 规则引擎是系统设计行为，非降级
            "sec_api",     # SEC 直查 API
            "SEC直查", "Finviz", "finviz",
        }
        PROXY_SOURCES = {
            "proxy_volume", "proxy_momentum", "proxy_social",
            "pheromone_board",
            "unavailable",  # API 未接入是结构设计，非运行失败，给 0.7 分
        }
        data_quality_summary = {}
        quality_score = 0.0
        total_fields = 0
        for r in valid_results:
            dq = r.get("data_quality", {})
            if isinstance(dq, dict):
                src = r.get("source", "unknown")
                data_quality_summary[src] = dq
                for v in dq.values():
                    total_fields += 1
                    if v in REAL_SOURCES:
                        quality_score += 1.0
                    elif v in PROXY_SOURCES:
                        quality_score += 0.7

        data_real_pct = round(quality_score / total_fields * 100, 1) if total_fields > 0 else 0.0

        # ── 按维度分拆数据质量（#3）──
        _DIM_SOURCES = {
            "signal":    "ScoutBeeNova",
            "catalyst":  "ChronosBeeHorizon",
            "sentiment": "BuzzBeeWhisper",
            "odds":      "OracleBeeEcho",
            "risk_adj":  "GuardBeeSentinel",
        }
        dim_data_quality: Dict[str, Optional[float]] = {}
        for _dim, _src in _DIM_SOURCES.items():
            _qs = 0.0
            _tf = 0
            for r in valid_results:
                if r.get("source") == _src:
                    _dq = r.get("data_quality", {})
                    if isinstance(_dq, dict):
                        for v in _dq.values():
                            _tf += 1
                            if v in REAL_SOURCES:
                                _qs += 1.0
                            elif v in PROXY_SOURCES:
                                _qs += 0.7
            dim_data_quality[_dim] = round(_qs / _tf * 100, 1) if _tf > 0 else None

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
                        bear_result=contrarian_result,
                    )
            except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
                _log.warning("QueenDistiller LLM service unavailable: %s", e)

        narrative = ""
        bull_bear_synthesis = ""
        contrarian_view = ""

        if llm_result:
            distill_mode = "llm_enhanced"
            reasoning = llm_result.get("reasoning", "")
            key_insight = llm_result.get("key_insight", "")
            risk_flag = llm_result.get("risk_flag", "")
            llm_confidence = llm_result.get("confidence", 0.5)
            narrative = llm_result.get("narrative", "")
            bull_bear_synthesis = llm_result.get("bull_bear_synthesis", "")
            contrarian_view = llm_result.get("contrarian_view", "")

            llm_score = llm_result.get("final_score")
            llm_direction = llm_result.get("direction")

            if llm_score is not None and isinstance(llm_score, (int, float)):
                # 混合策略：规则引擎 50% + LLM 50%（升级后权重对等）
                final_score = round(rule_score * 0.5 + float(llm_score) * 0.5, 2)
                final_score = max(0.0, min(10.0, final_score))

            if llm_direction in ("bullish", "bearish", "neutral"):
                # LLM 方向与规则引擎一致时采用，不一致时保持规则引擎
                if llm_direction == rule_direction:
                    final_direction = llm_direction
                elif llm_confidence >= 0.7:
                    # LLM 高置信度时覆盖规则引擎方向
                    final_direction = llm_direction

        # 10. 数据质量折扣（P4 门控）
        # 数据真实度不足时，将 final_score 向中性值 5.0 压缩，防止低质数据产生高置信结论
        # ≥ 80%: quality_factor = 1.0（无折扣）
        # 60–80%: 线性从 1.0 降至 0.875
        # 40%:    factor = 0.75；0%: factor = 0.5（最大压缩，偏差减半）
        dq_penalty_applied = False
        quality_factor = 1.0
        data_real_pct = _safe_score(data_real_pct, 50.0, 0, 100, "data_real_pct")
        if data_real_pct < 80.0:
            quality_factor = round(0.5 + 0.5 * (data_real_pct / 80.0), 3)
            pre_dq = final_score
            final_score = round(5.0 + (final_score - 5.0) * quality_factor, 2)
            final_score = max(0.0, min(10.0, final_score))
            if abs(final_score - pre_dq) >= 0.05:
                dq_penalty_applied = True
                _log.info(
                    "%s 数据质量折扣: real_pct=%.1f%% factor=%.3f %.2f→%.2f",
                    ticker, data_real_pct, quality_factor, pre_dq, final_score,
                )

        # 保留各 Agent 的原始分析内容（discovery + details）
        agent_details = {}
        for r in all_results:
            src = r.get("source", "unknown")
            agent_details[src] = {
                "discovery": r.get("discovery", ""),
                "score": r.get("score", 5.0),
                "direction": r.get("direction", "neutral"),
                "confidence": r.get("confidence", 0.5),
                "dimension": r.get("dimension", ""),
                "details": r.get("details") or {},
            }
            # BearBee LLM 看空论点（升级后新增字段）
            if src == "BearBeeContrarian":
                agent_details[src]["llm_thesis"] = r.get("llm_thesis", "")
                agent_details[src]["llm_key_risks"] = r.get("llm_key_risks", [])
                agent_details[src]["llm_contrarian_insight"] = r.get("llm_contrarian_insight", "")
                agent_details[src]["llm_thesis_break"] = r.get("llm_thesis_break", "")

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
            "agent_details": agent_details,
            "dimension_scores": dim_scores,
            "dimension_confidence": dim_confidence,
            "dimension_weights": dict(self.DIMENSION_WEIGHTS),
            "ml_adjustment": round(ml_adjustment, 3),
            "ml_contribution_pct": round(abs(ml_adjustment) / max(abs(final_score), 0.01) * 100, 1),
            "base_score_before_resonance": round(adjusted_score, 2),
            "pheromone_compact": self.board.compact_snapshot(ticker),
            "data_quality": data_quality_summary,
            "data_real_pct": data_real_pct,
            "dim_data_quality": dim_data_quality,
            # Phase 1: LLM 推理增强
            "distill_mode": distill_mode,
            "reasoning": reasoning,
            "key_insight": key_insight,
            "risk_flag": risk_flag,
            "llm_confidence": llm_confidence,
            # Phase 2: 叙事增强
            "narrative": narrative,
            "bull_bear_synthesis": bull_bear_synthesis,
            "contrarian_view": contrarian_view,
            "rule_score": rule_score,
            "rule_direction": rule_direction,
            "bear_strength": bear_strength,
            "bear_cap_applied": bear_cap_applied,
            "guard_penalty": guard_penalty,
            "guard_penalty_applied": guard_penalty_applied,
            "direction_vote_weights": {
                "bullish": round(bullish_w, 3),
                "bearish": round(bearish_w, 3),
                "neutral": round(neutral_w, 3),
            },
            "dq_quality_factor": quality_factor,
            "dq_penalty_applied": dq_penalty_applied,
            # NA1: 维度状态可视化
            "dimension_status": dim_status,
            "dimension_missing_reason": dim_missing_reason,
            "dimension_coverage_pct": dimension_coverage_pct,
        }

    # ==================== Phase 2: 历史类比推理 ====================

    def enrich_with_historical_analogy(
        self,
        ticker: str,
        distilled: dict,
        vector_memory,
        memory_store,
    ) -> dict:
        """
        用历史类比推理丰富 QueenDistiller 输出。
        仅在 LLM 模式 + 有足够历史记忆时调用。

        Args:
            ticker: 股票代码
            distilled: distill() 的返回结果（会被就地修改）
            vector_memory: VectorMemory 实例
            memory_store: MemoryStore 实例

        Returns:
            修改后的 distilled dict（新增 historical_analogy 字段）
        """
        if not self.use_llm:
            return distilled

        try:
            # 1. 构建当前信号查询
            direction = distilled.get("direction", "neutral")
            key_insight = distilled.get("key_insight", "")
            narrative = distilled.get("narrative", "")
            final_score = distilled.get("final_score", 5.0)

            query = f"{ticker} {direction} {key_insight}"

            # 2. 从 VectorMemory 检索语义相似历史
            vm_results = []
            if vector_memory and hasattr(vector_memory, "search") and vector_memory.enabled:
                vm_results = vector_memory.search(
                    query=query,
                    ticker=ticker,
                    top_k=8,
                    days=90,
                )

            # 最低门槛：需 ≥5 条历史记忆才值得做类比
            if len(vm_results) < 5:
                distilled["historical_analogy"] = None
                return distilled

            # 3. 从 MemoryStore 获取含实际回报的历史记忆
            ms_results = []
            if memory_store and hasattr(memory_store, "get_recent_memories"):
                ms_results = memory_store.get_recent_memories(
                    ticker=ticker,
                    days=90,
                    limit=50,
                )

            # 4. 构建当前信号摘要
            current_signals = {
                "direction": direction,
                "final_score": final_score,
                "key_insight": key_insight,
                "narrative": narrative[:200] if narrative else "",
                "bear_strength": distilled.get("bear_strength", 0),
            }

            # 5. 调用 LLM 历史类比
            import llm_service
            analogy = llm_service.find_historical_analogy(
                ticker=ticker,
                current_signals=current_signals,
                historical_memories=vm_results,
                historical_outcomes=ms_results,
            )

            if analogy and analogy.get("analogy_found"):
                distilled["historical_analogy"] = analogy

                # 6. 应用 confidence_adjustment 微调 final_score（±0.5 上限）
                adj = analogy.get("confidence_adjustment", 0)
                if isinstance(adj, (int, float)):
                    adj = max(-0.1, min(0.1, adj))
                    score_adj = adj * 5  # 映射 ±0.1 → ±0.5 分
                    score_adj = max(-0.5, min(0.5, score_adj))
                    old_score = distilled["final_score"]
                    distilled["final_score"] = round(
                        max(0, min(10, old_score + score_adj)), 2
                    )
                    distilled["historical_analogy"]["score_adjustment_applied"] = round(score_adj, 2)
            else:
                distilled["historical_analogy"] = analogy  # 保留 analogy_found=false 记录

        except Exception as e:
            _log.warning("enrich_with_historical_analogy 失败 (%s): %s", ticker, e)
            distilled["historical_analogy"] = None

        return distilled
