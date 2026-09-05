"""BeeAgent 基类 + prefetch_shared_data + inject_prefetched"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from hive_logger import pdt_today  # v0.28.0: 美股交易日工具
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
        # v0.45.122：一轮扫描的 yfinance 市场数据包（history / info / calendar /
        # analyst_targets），由 prefetch_market_bundle 一次取齐后注入。
        # 各蜂通过 _yf_* 访问器读，**不再自己 yf.Ticker / yf.download**。
        self._prefetched_market: Dict[str, Dict] = {}

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

    # 一轮里 Phase-1 有 5~6 只蜂发布，`get_top_signals` 的默认 n=5 会截断。
    # 取 24 条（80 条上限的 30%）足够覆盖单标的一轮的全部条目。
    _PEER_LOOKUP_N = 24

    def _read_peer(self, ticker: str, agent_id: str):
        """读取**同一轮里已发布**的其他蜂的信息素条目。

        返回 `PheromoneEntry` 或 `None`（对方还没跑 / 没发布 / 板上已衰减淘汰）。

        ⚠️ **调用方必须能接受 None。** 能不能读到完全取决于**执行阶段顺序**：
        Phase-1 内部是并行的，同批蜂之间读不到彼此；只有排在后面的阶段
        （GuardBee 所在的 Phase-1.5、BearBee 所在的 Phase-2，以及 v0.44.3 起
        被移到 Phase-1.5 的 RivalBee）才读得到 Phase-1 的产出。
        **想读某只蜂就必须排在它后面** —— 别指望靠重试或等待绕过这一点。

        为什么走信息素板而不是把上游结果当参数传进 `analyze()`：
        `analyze(ticker)` 的签名是所有蜂共用的契约，改它会牵动全部蜂与测试；
        而板上的 `details` 字段本来就是为结构化数据交换设计的（S3）。
        """
        try:
            entries = self.board.get_top_signals(ticker, n=self._PEER_LOOKUP_N)
        except (AttributeError, TypeError) as e:      # board 不可用/替身对象
            _log.debug("_read_peer(%s, %s) 板读取失败: %s", ticker, agent_id, e)
            return None
        for e in entries or []:
            if getattr(e, "agent_id", None) == agent_id:
                return e
        return None

    def _get_stock_data(self, ticker: str) -> Dict:
        """获取股票数据（优先使用预取缓存，回退到直接请求）

        增加 price<=0 保护（WARN-3）：当所有数据源不可用时，返回带
        _data_unavailable=True 标记，下游 Agent 应检查并提前返回安全结果。
        """
        if ticker in self._prefetched_stock:
            data = self._prefetched_stock[ticker]
        else:
            # v0.43.28: 必须透传目标日期。prefetch 走的是
            # `_fetch_stock_data(t, target_date)`（v0.41.6 的历史补跑通道），
            # 但这条落空分支此前不传 date —— `--date` 补跑历史交易日时，
            # 任何 prefetch miss 的标的会**静默拿到今天的实时价**，
            # 与同一份报告里其余标的的历史收盘价口径不一致。
            data = _cache._fetch_stock_data(ticker, getattr(self, "_target_date", None))

        # WARN-3 保护：price<=0 说明所有数据源不可用
        price = data.get("price", 0)
        # v0.45.69：NaN 必须显式挡。`not price` 拦不住（NaN 是 truthy），
        # `price <= 0` 也拦不住（NaN 比较恒 False）—— 2026-08-28 补跑就是
        # 从这里漏过去的，下游一路当成真价格用。
        import math as _m
        if (price is None or not isinstance(price, (int, float))
                or (isinstance(price, float) and not _m.isfinite(price))
                or price <= 0):
            _log.warning(
                "%s._get_stock_data(%s): price=%s, 标记数据不可用",
                self.__class__.__name__, ticker, price
            )
            data["_data_unavailable"] = True

        return data

    # ---------- v0.45.122：yfinance 访问器（预取优先，落空回退到直连） ----------
    #
    # 为什么是「访问器」而不是直接让蜂读 self._prefetched_market：
    #   · 回退语义要唯一。没注入（单票 CLI / 测试 / 补跑脚本）时，行为必须**逐字**
    #     等于改动前那只蜂自己写的 `yf.Ticker(ticker).info`，而不是另一套逻辑；
    #   · 数据源不许换：预取拿的就是 `.info` / `.calendar` / `.history` 本身，
    #     蜂拿到的对象与它自己去取一模一样，只是少了 N-1 次闸门排队。
    #
    # 2026-09-04 实测：同一只票的 `.info` 被 Scout / Rival / Bear 各取一次，
    # `.history` 被 Rival 取一次、Scout 又 download 一次；每次都过 0.5 req/s 的闸。

    def _market(self) -> Dict[str, Dict]:
        return getattr(self, "_prefetched_market", None) or {}

    def _yf_info(self, ticker: str) -> Dict:
        """`yf.Ticker(ticker).info`。"""
        hit = self._market().get("info", {}).get(ticker)
        if hit is not None:
            return hit
        import yfinance as yf
        return yf.Ticker(ticker).info

    def _yf_calendar(self, ticker: str):
        """`yf.Ticker(ticker).calendar`（DataFrame 或 dict，随 yfinance 版本）。"""
        cal = self._market().get("calendar", {})
        if ticker in cal:
            return cal[ticker]
        import yfinance as yf
        return yf.Ticker(ticker).calendar

    def _yf_analyst_targets(self, ticker: str):
        """`yf.Ticker(ticker).analyst_price_targets`。"""
        apt = self._market().get("analyst_targets", {})
        if ticker in apt:
            return apt[ticker]
        import yfinance as yf
        return getattr(yf.Ticker(ticker), "analyst_price_targets", None)

    def _yf_history(self, ticker: str, period: str = "3mo"):
        """`yf.Ticker(ticker).history(period=)`。预取包按 `MARKET_HISTORY_PERIOD`
        下载，请求更短的 period 时按日历日切尾巴（yfinance 的 `period="Nd"` 就是
        从现在往回数 N 个日历日）。请求比预取包更长 → 不猜，回退直连。"""
        hist = self._market().get("history", {})
        df = hist.get(ticker)
        if df is not None:
            sliced = _slice_history_period(df, period, self._market().get("history_period"))
            if sliced is not None:
                return sliced
        import yfinance as yf
        return yf.Ticker(ticker).history(period=period)

    def _yf_close_panel(self, symbols: List[str], period: str):
        """`yf.download(symbols, period=)["Close"]` 的等价物：列 = symbol 的收盘价面板。
        任一 symbol 不在预取包里 → 整个面板回退直连（不拼一半预取一半实时）。"""
        hist = self._market().get("history", {})
        frames = {}
        for s in symbols:
            df = hist.get(s)
            sliced = (_slice_history_period(df, period, self._market().get("history_period"))
                      if df is not None else None)
            if sliced is None:
                frames = None
                break
            frames[s] = sliced["Close"]
        if frames:
            import pandas as _pd
            return _pd.DataFrame(frames)
        import yfinance as yf
        return yf.download(list(symbols), period=period, interval="1d",
                           progress=False, auto_adjust=True)["Close"]

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
            return self.retriever.get_context_summary(ticker, pdt_today())  # v0.28.0: 美股交易日召回
        except (AttributeError, TypeError, ValueError) as e:
            _log.debug("History context unavailable for %s: %s", ticker, e)
            return ""

    def _validate_ticker(self, ticker: str) -> Optional[Dict]:
        """验证 ticker 格式（1~5 大写字母 + 可选 -X/.X 类份额后缀）；无效时返回标准错误结构"""
        if not ticker or not _RE_TICKER.match(str(ticker).strip()):
            _log.warning("%s.analyze() 收到无效 ticker: %r", self.__class__.__name__, ticker)
            return {
                "error": "invalid_ticker",
                "source": self.__class__.__name__,
                "score": 5.0,
                "direction": "neutral",
                "confidence": 0.0,
                "discovery": f"无效 ticker 格式: {ticker!r}"
                             "（需 1~5 位大写字母，可带 -X/.X 类份额后缀）",
                "dimension": "validation",
            }
        return None


def prefetch_shared_data(tickers: list, retriever=None, target_date: Optional[str] = None) -> Dict:
    """
    批量预取所有 ticker 的共享数据（yfinance + VectorMemory），
    避免 6 个 Agent 各自重复请求。

    v0.41.6: target_date 是当前报告的目标交易日（通常=调用方的 self.date_str）。
    非当日实时扫描时（`--date` 补跑历史交易日），透传给 _fetch_stock_data 让
    价格锚定该日期真实收盘价，而非"脚本运行那一刻"的实时报价——所有走
    _get_stock_data()/共享快照价的 Agent（Scout/Oracle/Chronos/CodeExecutor 等）
    自动获得一致、正确的历史价格。

    返回: {"stock_data": {ticker: data}, "contexts": {ticker: str}, "target_date": str|None}
    """
    stock_data = {}
    contexts = {}

    # 1+2. 并行预取 yfinance + VectorMemory（I/O bound，并行比串行快 N 倍）
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    _max_w = min(len(tickers), 4)  # 限制并发避免 yfinance 429 限流
    if _max_w > 0:
        with ThreadPoolExecutor(max_workers=_max_w, thread_name_prefix="prefetch") as _pex:
            # yfinance 并行
            _yf_futs = {_pex.submit(_cache._fetch_stock_data, t, target_date): t for t in tickers}
            for fut in _as_completed(_yf_futs):
                t = _yf_futs[fut]
                try:
                    stock_data[t] = fut.result(timeout=30)
                except NETWORK_ERRORS as e:
                    _log.debug("Prefetch yfinance failed for %s: %s", t, e)
                    stock_data[t] = _cache._fetch_stock_data(t, target_date)

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

    # 4. v0.45.122：yfinance 市场数据包（history / info / calendar / analyst_targets）
    market: Dict = {}
    try:
        market = prefetch_market_bundle(tickers)
    except Exception as e:  # noqa: BLE001 - 预取是优化不是前提，失败各蜂自行回退直连
        _log.warning("市场数据包预取失败（各蜂回退直连）: %s", e)

    return {"stock_data": stock_data, "contexts": contexts, "target_date": target_date,
            "market": market}


# ---------- v0.45.122：yfinance 市场数据包 ----------

#: 预取包的日线窗口。Rival 技术指标要 3mo（≥26 根），Scout 相对强度要 25d，
#: 取两者的最大值；短窗口由访问器切尾巴得到。
MARKET_HISTORY_PERIOD = "3mo"

_PERIOD_DAYS = {"d": 1, "wk": 7, "mo": 30, "y": 365}


def _period_to_days(period: Optional[str]) -> Optional[int]:
    """"25d" → 25、"3mo" → 90、"1y" → 365；认不出返回 None。"""
    if not period:
        return None
    p = str(period).strip().lower()
    for unit in ("mo", "wk", "d", "y"):     # "mo" 要先于 "d"/"y" 匹配
        if p.endswith(unit):
            try:
                return int(p[: -len(unit)]) * _PERIOD_DAYS[unit]
            except ValueError:
                return None
    return None


def _slice_history_period(df, period: str, bundle_period: Optional[str]):
    """把按 `bundle_period` 下载的帧切成 `period` 的尾巴。

    返回 None 表示「不能用预取包」：请求比包长、或 period 认不出——调用方回退直连。
    请求与包同长直接返回整帧（逐字节等于 `history(period=bundle_period)`）。
    """
    want = _period_to_days(period)
    have = _period_to_days(bundle_period)
    if want is None or have is None or want > have:
        return None
    if want == have:
        return df
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=want)).date()
    try:
        return df[df.index.date >= cutoff]
    except (AttributeError, TypeError):
        return None


def _sector_etfs_for(tickers: list) -> List[str]:
    """Scout 相对强度要的板块 ETF：与 scout_bee 同一张映射表。"""
    out = set()
    try:
        from config import WATCHLIST
        from fred_macro import _SECTOR_TO_ETF
    except ImportError:
        return []
    for t in tickers:
        etf = _SECTOR_TO_ETF.get(WATCHLIST.get(t, {}).get("sector", ""), "")
        if etf:
            out.add(etf)
    return sorted(out)


def prefetch_market_bundle(tickers: list) -> Dict[str, Dict]:
    """一轮扫描只取一次的 yfinance 数据：

        history          ticker/ETF → OHLC 帧（一次 yf.download 全覆盖）
        info             ticker → `.info` dict          （Scout 板块兜底 / Rival 分析师 / Bear 空头仓位）
        calendar         ticker → `.calendar`           （Chronos 财报日）
        analyst_targets  ticker → `.analyst_price_targets`（Chronos 目标价）

    每一项**失败就缺席**，不放占位值：蜂的访问器发现缺席会回退到它原来的直连，
    行为与没预取时完全一致。返回的 `history_period` 告诉访问器帧有多长。
    """
    bundle: Dict[str, Dict] = {"history": {}, "info": {}, "calendar": {},
                               "analyst_targets": {}, "history_period": MARKET_HISTORY_PERIOD}
    if not tickers:
        return bundle
    try:
        import yfinance as yf
    except ImportError:
        return bundle

    # ── 日线：一次 download 覆盖全部标的 + 板块 ETF ──
    symbols = sorted(set(tickers) | set(_sector_etfs_for(tickers)))
    try:
        from backtester import _split_download_frame
        raw = yf.download(tickers=symbols, period=MARKET_HISTORY_PERIOD, interval="1d",
                          group_by="ticker", auto_adjust=True, progress=False, threads=False)
        bundle["history"] = _split_download_frame(raw, symbols)
    except Exception as e:  # noqa: BLE001
        _log.warning("市场数据包：日线批量下载失败（各蜂回退直连）: %s", e)

    # ── 逐票属性：info / calendar / analyst_price_targets ──
    # info 与 calendar 走闸门（0.5 req/s），并发也排队；这里只是把「每只票各蜂
    # 各取一次」压成「每只票取一次」，不试图绕闸。
    def _one(t: str):
        got: Dict = {}
        try:
            tk = yf.Ticker(t)
        except Exception as e:  # noqa: BLE001
            _log.debug("市场数据包：Ticker(%s) 构造失败: %s", t, e)
            return t, got
        for key, attr in (("info", "info"), ("calendar", "calendar"),
                          ("analyst_targets", "analyst_price_targets")):
            try:
                val = getattr(tk, attr)
                if val is not None:
                    got[key] = val
            except Exception as e:  # noqa: BLE001 - 缺席即可，蜂会回退
                _log.debug("市场数据包：%s.%s 不可得: %s", t, attr, e)
        return t, got

    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
    with ThreadPoolExecutor(max_workers=min(len(tickers), 4), thread_name_prefix="prefetch-mkt") as ex:
        for fut in _as_completed([ex.submit(_one, t) for t in tickers]):
            try:
                t, got = fut.result(timeout=120)
            except Exception as e:  # noqa: BLE001
                _log.debug("市场数据包：单票预取异常: %s", e)
                continue
            for key, val in got.items():
                bundle[key][t] = val

    _log.info("市场数据包：日线 %d/%d 只 | info %d | calendar %d | analyst_targets %d",
              len(bundle["history"]), len(symbols), len(bundle["info"]),
              len(bundle["calendar"]), len(bundle["analyst_targets"]))
    return bundle


def inject_prefetched(agents: list, prefetched: Dict):
    """将预取数据注入所有 Agent"""
    for agent in agents:
        agent._prefetched_stock = prefetched.get("stock_data", {})
        agent._prefetched_context = prefetched.get("contexts", {})
        # v0.43.28: 目标日期随预取一起注入，供 _get_stock_data 的落空分支使用
        agent._target_date = prefetched.get("target_date")
        # v0.45.122: yfinance 市场数据包
        agent._prefetched_market = prefetched.get("market", {}) or {}
