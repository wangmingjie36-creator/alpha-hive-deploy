"""BeeAgent 基类 + prefetch_shared_data + inject_prefetched"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pheromone_board import PheromoneBoard, PheromoneEntry
from resilience import NETWORK_ERRORS
from swarm_agents._config import _log, _RE_TICKER, _AS
from swarm_agents import cache as _cache  # Module reference for monkeypatch compatibility!


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

    def _publish(self, ticker: str, discovery: str, source: str, score: float, direction: str, details: Dict = None):
        """发布发现到信息素板（S3: 支持结构化 details 数据交换）"""
        entry = PheromoneEntry(
            agent_id=self.__class__.__name__,
            ticker=ticker,
            discovery=discovery,
            source=source,
            self_score=score,
            direction=direction,
            details=details or {},
        )
        self.board.publish(entry)

    def _get_stock_data(self, ticker: str) -> Dict:
        """获取股票数据（优先使用预取缓存，回退到直接请求）

        增加 price<=0 保护（WARN-3）：当所有数据源不可用时，返回带
        _data_unavailable=True 标记，下游 Agent 应检查并提前返回安全结果。
        """
        if ticker in self._prefetched_stock:
            data = self._prefetched_stock[ticker]
        else:
            data = _cache._fetch_stock_data(ticker)

        # WARN-3 保护：price<=0 说明所有数据源不可用
        price = data.get("price", 0)
        if not price or price <= 0:
            _log.warning(
                "%s._get_stock_data(%s): price=%s, 标记数据不可用",
                self.__class__.__name__, ticker, price
            )
            data["_data_unavailable"] = True

        return data

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
    _max_w = min(len(tickers), 4)  # 限制并发避免 yfinance 429 限流
    if _max_w > 0:
        with ThreadPoolExecutor(max_workers=_max_w, thread_name_prefix="prefetch") as _pex:
            # yfinance 并行
            _yf_futs = {_pex.submit(_cache._fetch_stock_data, t): t for t in tickers}
            for fut in _as_completed(_yf_futs):
                t = _yf_futs[fut]
                try:
                    stock_data[t] = fut.result(timeout=30)
                except NETWORK_ERRORS as e:
                    _log.debug("Prefetch yfinance failed for %s: %s", t, e)
                    stock_data[t] = _cache._fetch_stock_data(t)

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
