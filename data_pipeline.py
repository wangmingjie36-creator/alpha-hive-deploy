"""
Alpha Hive 升级 — Phase 1.1: 多源数据降级链
============================================
替换 swarm_agents/cache.py 中的 _fetch_stock_data()

问题：当前 yfinance 失败时返回 price=100.0 的硬编码默认值，
      所有下游 Agent 会基于虚假数据生成评分，且无告警标记。

解决方案：
  1. 多源降级链：yfinance → Alpha Vantage → Finnhub → 静态缓存
  2. 每条数据都带 data_source 标记（real / degraded / stale / fallback）
  3. 熔断器状态可观测（日志 + 指标暴露）
  4. 智能缓存：LRU + TTL + 分级 TTL（real=5min, degraded=2min）

用法：
  将本文件放入项目根目录，然后在 swarm_agents/cache.py 中：
  from data_pipeline import MultiSourceFetcher
  _fetcher = MultiSourceFetcher()

  替换 _fetch_stock_data:
  def _fetch_stock_data(ticker: str) -> Dict:
      return _fetcher.fetch(ticker)
"""

import os
import time
import math
import logging
import threading
import weakref
from typing import Dict, Optional, List, Tuple
from collections import deque


def _get_secret(name: str) -> str:
    """取集中管理的 key。config 不可用时退回环境变量——
    data_pipeline 设计上可独立于项目其余部分使用。"""
    try:
        from config import get_secret
        return get_secret(name) or ""
    except Exception:  # noqa: BLE001
        import os as _os
        return _os.environ.get(name, "")
from dataclasses import dataclass
from datetime import datetime, timedelta, time as _dt_time

_log = logging.getLogger("alpha_hive.data_pipeline")


# ==================== 盘中 forming-bar 护栏（v0.29.4）====================
# 本机时钟可能被设错时区（如温哥华机器误设上海 UTC+8，偏移 15h），
# 不能用 datetime.now() 判断"美股现在开没开盘"。改用 yfinance 的分钟数据
# 末时间戳（来自交易所服务器，美东 tz，真实时间），一次扫描内缓存（用 SPY 探一次）。
_EXCHANGE_NOW_CACHE = None  # None=未取 / False=取失败 / Timestamp=成功
_EXCHANGE_NOW_LOCK = threading.Lock()  # 防 ThreadPoolExecutor 并发首批各发一次 SPY 调用

def _exchange_now():
    """返回美股交易所当前时间戳（美东 tz，来自 Yahoo 服务器），失败返回 None。
    市场开/收对所有美股相同，只需探一次（SPY），整进程缓存。
    加锁 + double-checked：并发 fetch 时只有第一个线程真正打 SPY，其余复用缓存。"""
    global _EXCHANGE_NOW_CACHE
    if _EXCHANGE_NOW_CACHE is not None:  # fast path：已取到直接返回，不进锁
        return _EXCHANGE_NOW_CACHE or None
    with _EXCHANGE_NOW_LOCK:
        if _EXCHANGE_NOW_CACHE is not None:  # double-check：进锁后再判一次
            return _EXCHANGE_NOW_CACHE or None
        try:
            import yfinance as yf
            intra = yf.Ticker("SPY").history(period="1d", interval="1m")
            if not intra.empty:
                _EXCHANGE_NOW_CACHE = intra.index[-1]
                return _EXCHANGE_NOW_CACHE
        except Exception:
            pass
        _EXCHANGE_NOW_CACHE = False
        return None


def _drop_forming_bar(hist):
    """若日线末根是"当日盘中正在形成"的 bar，返回去掉末根的 hist；否则原样返回。
    判据（不依赖本机钟）：末根日期 == 交易所真实当日 且 当前时间 < 收盘 15:59 美东。
    任何异常都原样返回（绝不影响主流程）。"""
    try:
        if hist is None or len(hist) < 3:
            return hist
        xnow = _exchange_now()
        if xnow is None:
            return hist
        if hist.index[-1].date() == xnow.date() and xnow.time() < _dt_time(15, 59):
            return hist.iloc[:-1]
    except Exception:
        pass
    return hist


def _completed_bar_close(hist) -> Optional[Tuple[str, float]]:
    """日线末根**确知已收盘** → `(场次 ISO 日期, 收盘)`；判不了一律 None（v0.45.477）。

    `_drop_forming_bar` 为不影响主流程，判不了（交易所时钟取不到 / 不足 3 根）时**原样返回**——
    末根可能正是盘中在形成的那根，它的 Close 是盘中价。拿它当价格是 yfinance 源一贯的取舍；
    但当「核实过的收盘」交给自攒索引当窗口末端（`StockData.session_close`）就违反 v0.45.465 的契约
    （盘中价 / 判不了归属的价不许进窗口）。所以这里反过来：**能证明收了才给**。

    判据与 `_drop_forming_bar` 同一份：交易所时钟可得，且末根不是「交易所当日 + 15:59 美东之前」。
    （`_exchange_now` 是 SPY 最后一根分钟线的时间戳——收盘后恒为 15:59，故不能写 16:00。）
    """
    try:
        if hist is None or len(hist) < 1:
            return None
        xnow = _exchange_now()
        if xnow is None:
            return None
        d = hist.index[-1].date()
        if d > xnow.date() or (d == xnow.date() and xnow.time() < _dt_time(15, 59)):
            return None
        px = float(hist["Close"].iloc[-1])
        if not (math.isfinite(px) and px > 0):
            return None
        return d.isoformat(), px
    except Exception:  # noqa: BLE001 —— 判不了就不给，不阻断取数
        return None


# ==================== 数据质量标记 ====================

class DataQuality:
    """数据质量等级常量"""
    REAL = "real"              # API 调用成功，数据新鲜
    DEGRADED = "degraded"      # 降级源（备用 API），数据可能有延迟
    STALE = "stale"            # 缓存数据，超过 TTL 但 API 全部失败
    FALLBACK = "fallback"      # 所有源不可用，返回安全默认值
    ERROR = "error"            # 数据获取失败


@dataclass
class StockData:
    """标准化股票数据结构（所有数据源统一输出格式）

    P0-2 (v0.38.0): momentum_5d 可为 None（真实 5 日动量不可得时），
    不再用当日涨跌幅冒充——下游背离检测/情绪映射按 5 日语义消费，
    1 日近似会触发错误的 bull trap / 空头阈值判定。
    ⚠️ 名字叫 5 日，全仓口径实为 **4 个交易日**收益（`iloc[-1] / iloc[-5]`；
    自攒索引回落 `price_history.MOMENTUM_LOOKBACK` 对齐此口径，v0.45.469）。
    momentum_source: "5d_real"（yfinance 历史K线计算）| "price_index"（自攒索引回落）| "unavailable"
    momentum_as_of: 自攒索引回落时窗口末端那一天（v0.45.465）；其余来源不填
    volume_source: "yfinance" | "twelvedata"（回落）| None（没有值）（v0.45.473）
    volume_as_of: Twelve Data 回落时比值末根那一天（v0.45.505）；其余来源不填

    数据源只填**自己取到的**历史字段；缺的那几项由 `MultiSourceFetcher` 唯一出口
    `_fill_history_fields` 统一回落（v0.45.473），不归哪个源管。
    """
    price: float = 0.0
    # v0.45.3: 类型早已是 Optional，默认值却还是 0.0 / 1.0——构造 StockData 时
    # 不显式赋值就又变回伪造（531/582 行显式传 None 正说明意图是 None）。
    # 默认改为 None：**没测量过就是未知**，而不是"持平/正常量"。
    momentum_5d: Optional[float] = None
    # v0.45.54：由 int 改 Optional[int] —— 20 日均量不可得时置 None，
    # 而不是 0（读作「零成交」）或 1（读作「日均成交 1 股」）。
    avg_volume: Optional[int] = None
    volume_ratio: Optional[float] = None
    # v0.45.3: 原 `float = 0.0`。momentum_5d/volume_ratio 早已是 Optional（P0-2,
    # v0.38.0），只有 volatility 还留着 0.0——而它是三者里后果最重的：
    # σ=0 ⇒ VaR 恒为 0 ⇒ 风险面板显示"🟢 低"。只改 cache.py 的兜底堵不住，
    # 因为那条分支要 data_pipeline 导入失败才走得到，主路径在这里。
    volatility_20d: Optional[float] = None
    # 元数据
    data_source: str = DataQuality.FALLBACK
    source_name: str = "none"
    momentum_source: str = "none"
    # v0.45.465：自攒索引回落的窗口末端日期。拿到当场官方收盘时 = 场次日期；
    # 拿不到时是库里最新一天（上一个扫描日），比 yfinance 收盘后的窗口滞后一个扫描日。
    momentum_as_of: Optional[str] = None
    volume_source: Optional[str] = None
    # v0.45.505：Twelve Data 回落的比值末根日期。只在它 == 此刻最近一场已收盘时才收（见
    # `_fill_volume_from_twelvedata`），所以有值时它就是 yfinance 主路径会结束的那一天。
    volume_as_of: Optional[str] = None
    fetch_timestamp: float = 0.0
    is_market_hours: bool = False
    # v0.45.234：price 取的是哪个字段（目前只有 CBOE 源填，见 cboe_options.official_price）。
    # 进 to_dict —— 「这个入场价是不是官方收盘」得让下游看得见，不能只在日志里。
    price_source: str = ""
    # v0.45.234：此源拿到了数，但**不该优先采用**（CBOE 盘中陈旧价）。
    # MultiSourceFetcher 先试后面的源，全失败才退回用它。不进 to_dict（内部调度用）。
    defer: bool = False
    # v0.45.473：此源核实过的某一场官方收盘 `(ISO 日期, 价)`，给出口的 momentum 回落当窗口末端
    # （CBOE 取自 `_cboe_session_close`、yfinance 取自末根已收盘日线）。不进 to_dict。
    session_close: Optional[Tuple[str, float]] = None
    # v0.45.473：已过出口回落（`_fill_history_fields`）。缓存存的是回落后的对象，命中时不重跑。不进 to_dict。
    history_checked: bool = False

    def to_dict(self) -> Dict:
        return {
            "price": self.price,
            "momentum_5d": self.momentum_5d,
            "avg_volume": self.avg_volume,
            "volume_ratio": self.volume_ratio,
            "volatility_20d": self.volatility_20d,
            "data_source": self.data_source,
            "source_name": self.source_name,
            "momentum_source": self.momentum_source,
            "momentum_as_of": self.momentum_as_of,
            "volume_source": self.volume_source,
            "volume_as_of": self.volume_as_of,
            "fetch_timestamp": self.fetch_timestamp,
            "is_market_hours": self.is_market_hours,
            "price_source": self.price_source,
        }


# ==================== 熔断器（可观测版）====================

class ObservableCircuitBreaker:
    """带指标暴露的熔断器"""

    # v0.45.344：全部活着的实例（WeakSet，不延长寿命）。`get_fetcher()` 单例的各数据源
    # 各持一个，状态跨测试存活；唯一读者是 tests/conftest.py `_reset_circuit_breakers`。
    # 与 resilience.CircuitBreaker._live 同形，两个类互不继承，所以各自一份。
    _live: "weakref.WeakSet[ObservableCircuitBreaker]" = weakref.WeakSet()
    _live_lock = threading.Lock()

    def __init__(self, name: str, failure_threshold: int = 3,
                 recovery_timeout: float = 60.0, half_open_max: int = 1):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max = half_open_max

        self._init_state()
        self._lock = threading.Lock()
        with ObservableCircuitBreaker._live_lock:
            ObservableCircuitBreaker._live.add(self)

    def _init_state(self):
        """运行态与指标字段的唯一出处：构造与 reset() 共用，新增字段不会漏重置。"""
        self._failures = 0
        self._successes = 0
        self._total_calls = 0
        self._state = "closed"  # closed / open / half_open
        self._last_failure_time = 0.0
        self._half_open_calls = 0

        # 指标
        self._consecutive_failures = 0
        self._last_error: str = ""
        self._trip_count = 0

    @classmethod
    def live_instances(cls) -> list:
        """当前所有活着的实例（快照）。"""
        with ObservableCircuitBreaker._live_lock:
            return list(ObservableCircuitBreaker._live)

    def reset(self, timeout: Optional[float] = None):
        """重置成刚构造时的状态（含 trip_count 等指标；阈值等配置不动）。

        timeout 语义同 `resilience.CircuitBreaker.reset`：None 阻塞等锁；给了就在
        时限内拿不到锁时抛 RuntimeError（不抛 TimeoutError 的理由也见那里）。
        """
        got = self._lock.acquire() if timeout is None else self._lock.acquire(timeout=timeout)
        if not got:
            raise RuntimeError(
                f"ObservableCircuitBreaker[{self.name}] 的锁 {timeout}s 内拿不到（别的线程一直攥着）")
        try:
            self._init_state()
        finally:
            self._lock.release()

    def allow_request(self) -> bool:
        with self._lock:
            now = time.time()
            if self._state == "closed":
                return True
            elif self._state == "open":
                if now - self._last_failure_time >= self.recovery_timeout:
                    self._state = "half_open"
                    self._half_open_calls = 0
                    _log.info("[CB-%s] open → half_open (recovery timeout)", self.name)
                    return True
                return False
            else:  # half_open
                if self._half_open_calls < self.half_open_max:
                    self._half_open_calls += 1
                    return True
                return False

    def record_success(self):
        with self._lock:
            self._successes += 1
            self._total_calls += 1
            self._consecutive_failures = 0
            if self._state == "half_open":
                self._state = "closed"
                self._failures = 0
                _log.info("[CB-%s] half_open → closed (success)", self.name)

    def record_failure(self, error: str = ""):
        with self._lock:
            self._failures += 1
            self._total_calls += 1
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            self._last_error = str(error)[:200]

            if self._state == "half_open":
                self._state = "open"
                self._trip_count += 1
                _log.warning("[CB-%s] half_open → open (failure: %s)", self.name, error)
            elif self._failures >= self.failure_threshold:
                self._state = "open"
                self._trip_count += 1
                _log.warning("[CB-%s] closed → open (threshold %d, error: %s)",
                             self.name, self.failure_threshold, error)

    def get_metrics(self) -> Dict:
        """暴露熔断器指标（供监控/日志使用）"""
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "successes": self._successes,
                "total_calls": self._total_calls,
                "consecutive_failures": self._consecutive_failures,
                "trip_count": self._trip_count,
                "last_error": self._last_error,
                "success_rate": (
                    round(self._successes / self._total_calls * 100, 1)
                    if self._total_calls > 0 else 0.0
                ),
            }


# ==================== 数据源适配器 ====================

def _fetch_historical_stock_data(ticker: str, as_of_date: str) -> Dict:
    """v0.41.6: `--date` 补跑历史交易日时，锚定该日期的真实收盘价。

    根因：CBOE/AlphaVantage/Finnhub 全是"当前实时报价"源，没有免费的历史
    快照能力；补跑过去某天的报告时，若仍走 MultiSourceFetcher.fetch()，
    拿到的其实是"脚本运行那一刻"的实时价——同一天补跑多次，价格会跟着
    市场实时波动而变，跟 as_of_date 的真实收盘价毫无关系（曾出现 7/21
    的报告里现价先后显示 $206.34 / $205.10，两个都不是 7/21 收盘价 $207.29）。

    只能用 yfinance 历史K线实现——这是唯一有 start=/end= 历史区间能力的源。
    """
    try:
        import yfinance as yf
        as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        from datetime import timedelta
        # 往前多拉 60 天，留够 20 日均量/5 日动量/20 日波动率的计算窗口
        start = as_of - timedelta(days=60)
        end = as_of + timedelta(days=1)  # yfinance end 是开区间，+1 天才含 as_of 当天
        hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat())
        if hist.empty:
            fb = StockData(data_source=DataQuality.FALLBACK).to_dict()
            fb["_data_unavailable"] = True
            return fb

        # 只保留 <= as_of 的行（防止误传未来日期，或 yfinance 返回多余数据）
        _idx_dates = hist.index.tz_localize(None) if hist.index.tz is not None else hist.index
        hist = hist[_idx_dates.date <= as_of]
        # v0.45.69：**先丢掉 Close 为 NaN 的行，再判空。**
        # 原写法 `hist["Close"].iloc[-1] <= 0` 挡不住 NaN —— NaN 的任何比较
        # 都返回 False，于是 `price=float(NaN)` 一路流到下游。
        # 实测 2026-08-29 补跑 8/28（周六跑周五）：yfinance 尾行 Close 是 NaN，
        # 30 只标的全部拿到 price=NaN。而 NaN 还是 truthy，
        # `base._get_stock_data` 的 `if not price or price <= 0` 也拦不住它，
        # 最终 OptionsAgent 收到 stock_price=NaN → ATM 带 [NaN, NaN] 恒不匹配
        # → raw_ivs 全空 → data_quality 判 degraded → iv_current 回落陈缓存。
        # 一个 NaN，穿过四道各自写着「<= 0」的守卫。
        _closes = hist["Close"].dropna()
        if hist.empty or _closes.empty or _closes.iloc[-1] <= 0:
            fb = StockData(data_source=DataQuality.FALLBACK).to_dict()
            fb["_data_unavailable"] = True
            return fb
        hist = hist.loc[_closes.index]      # 后续窗口一并用清洗过的行，口径一致

        # v0.45.69 ⚠️ 丢完 NaN 之后必须校验**剩下的最后一行确实是 as_of 当天**。
        # 否则「丢掉 NaN 行」会静默变成「用前一交易日的收盘价冒充目标日」——
        # 那正是 2026-08-24 被写进 signal_archive 隔离名单的那种口径污染，
        # 只不过换了个更隐蔽的入口。
        # 实测 2026-08-29：yfinance 的 2026-08-28 行 Close=NaN 而 Volume=1.94 亿
        # （当天确实交易了，只是收盘价拿不到），丢掉后末行变成 8/27 的 227.98，
        # 而 8/28 的真实收盘按云端 CBOE 快照是 217.55 —— 差 4.8%。
        _last_dt = hist.index[-1]
        _last_date = (_last_dt.tz_localize(None) if getattr(_last_dt, "tzinfo", None) else _last_dt).date()
        if _last_date != as_of:
            # v0.45.70：yfinance 拿不到目标日收盘时，改问云端快照。
            # v0.45.243：**不再直接取 `price_at_fetch`**。那是抓取那一刻 official_price
            # 的取值，CDN 发盘中生成的文件时它是中午的成交价、且 v0.45.234 之前的快照
            # 照样标 cboe_close（08-28 DE 624.85 vs 官方 630.33 就是这么进的库）。
            # 判不判得出「官方收盘」全在 `load_official_close` 里，这里只认它的结论。
            _snap_px, _snap_src = None, "snapshot_not_consulted"
            try:
                import cloud_snapshot_loader as _csl
                _snap_px, _snap_src = _csl.load_official_close(as_of_date, ticker)
            except Exception as _e_snap:  # noqa: BLE001 —— 兜底失败不得掩盖主诊断
                _snap_src = f"snapshot_error:{type(_e_snap).__name__}"
                _log.warning("[历史补跑] %s @ %s 云端快照取价失败: %s", ticker, as_of_date, _e_snap)

            if _snap_px is None:
                # 谁会红？—— 「快照有、但不是官方收盘」与「快照没有」分开报：前者是
                # v0.45.243 主动拒收的（`_reason` 带判决），不能读成「兜底坏了」。
                _log.warning(
                    "[历史补跑] %s @ %s：yfinance 无该日有效收盘价（最近有效行是 %s），"
                    "云端快照也给不出官方收盘（%s）—— **不以前一交易日 / 盘中价冒充**，标记不可用",
                    ticker, as_of_date, _last_date, _snap_src)
                fb = StockData(data_source=DataQuality.FALLBACK).to_dict()
                fb["_data_unavailable"] = True
                fb["_reason"] = f"no_close_on_{as_of_date}"
                fb["_snapshot_verdict"] = _snap_src
                return fb

            _log.info("[历史补跑] %s @ %s：yfinance 无该日收盘（末行 %s），"
                      "改用云端快照 %.4f（来源 %s）",
                      ticker, as_of_date, _last_date, _snap_px, _snap_src)
            _d2 = StockData(
                price=_snap_px,
                data_source=DataQuality.REAL,
                source_name=f"cloud_snapshot:{_snap_src}",
                fetch_timestamp=time.time(),
                price_source=_snap_src,
            )
            # 动量用快照价当最新一档、yfinance 历史当基准 —— 两端都是收盘价，
            # 口径可比；但要标明它是拼出来的，别当成单一来源的读数。
            _cl = hist["Close"]
            if len(_cl) >= 4:
                _base = float(_cl.iloc[-4])
                if _base > 0:
                    _d2.momentum_5d = float((_snap_px / _base - 1) * 100)
                    _d2.momentum_source = "5d_snapshot_spliced"
            else:
                _d2.momentum_5d = None
                _d2.momentum_source = "unavailable"
            # ⚠️ volume_ratio 不给：它要拿**目标日**成交量比均量，
            # 而这条路径下目标日的量本来就没取到。用前一日的量冒充会静默失真。
            _d2.volume_ratio = None
            _r2 = _d2.to_dict()
            _r2["_as_of_date"] = as_of_date
            _r2["_price_from_cloud_snapshot"] = True
            return _r2

        data = StockData(
            price=float(hist["Close"].iloc[-1]),
            data_source=DataQuality.REAL,
            source_name="yfinance_historical",
            fetch_timestamp=time.time(),
        )
        if len(hist) >= 5:
            data.momentum_5d = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100)
            data.momentum_source = "5d_real"
        else:
            data.momentum_5d = None
            data.momentum_source = "unavailable"

        recent_vol = float(hist["Volume"].iloc[-1])
        avg_vol = (float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20
                   else float(hist["Volume"].mean()))
        if avg_vol and avg_vol > 0 and not math.isnan(avg_vol):
            data.avg_volume = int(avg_vol)
            data.volume_ratio = recent_vol / avg_vol
        else:
            data.volume_ratio = None

        if len(hist) >= 20:
            returns = hist["Close"].pct_change().dropna()
            if len(returns) >= 2:
                vol = float(returns.std() * (252 ** 0.5) * 100)
                if not (math.isnan(vol) or math.isinf(vol)):
                    data.volatility_20d = vol

        result = data.to_dict()
        result["_as_of_date"] = as_of_date
        return result
    except Exception as e:
        _log.warning("[历史补跑] %s @ %s 取历史收盘价失败: %s", ticker, as_of_date, e)
        fallback = StockData(data_source=DataQuality.FALLBACK).to_dict()
        fallback["_data_unavailable"] = True
        return fallback


def _fetch_history_metrics(ticker: str) -> Optional[Dict]:
    """P0-2 (v0.38.0): 从 yfinance 历史K线独立计算 momentum_5d / volume_ratio /
    avg_volume / volatility_20d（含 forming-bar 护栏）。

    供 CBOESource（无历史K线）用。失败返回 None——调用方必须把 momentum_5d 置 None
    （诚实缺数据），禁止用 1 日涨跌幅冒充。

    **只算 yfinance**（v0.45.473）：此前这里在成功 / 异常两条分支各自挂了一次自攒索引 /
    Twelve Data 回落，空历史、均量 NaN 两条分支却一条都没挂；且只有 CBOE 胜出时才走得到。
    回落现在只在 `MultiSourceFetcher` 的唯一出口 `_fill_history_fields` 里做。
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="1mo")
        if hist.empty or len(hist) < 2:
            return None
        hist = _drop_forming_bar(hist)
        if hist.empty or len(hist) < 2:
            return None

        out: Dict = {}
        if len(hist) >= 5:
            out["momentum_5d"] = float(
                (hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100
            )

        recent_vol = float(hist["Volume"].iloc[-1])
        avg_vol = (float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20
                   else float(hist["Volume"].mean()))
        # v0.45.54：兜底 1.0 会让 avg_volume = **1 股**（任何流动性检查都判
        # 极端不可交易），且 volume_ratio = recent_vol/1 变成千万量级
        # （读作「成交量是均量的 5000 万倍」）。不可得就置 None。
        if math.isnan(avg_vol) or avg_vol <= 0:
            _log.warning("%s 20 日均量不可得，avg_volume/volume_ratio 置 None", ticker)
            out["avg_volume"] = None
            out["volume_ratio"] = None
        else:
            out["avg_volume"] = int(avg_vol)
            out["volume_ratio"] = recent_vol / avg_vol

        if len(hist) >= 20:
            returns = hist["Close"].pct_change().dropna()
            if len(returns) >= 2:
                vol = float(returns.std() * (252 ** 0.5) * 100)
                if not (math.isnan(vol) or math.isinf(vol)):
                    out["volatility_20d"] = vol

        out["_price_from_hist"] = float(hist["Close"].iloc[-1])
        out["momentum_source"] = "yfinance"
        return out
    except Exception as e:
        _log.debug("_fetch_history_metrics %s failed: %s", ticker, e)
        return None


def _fill_momentum_from_index(ticker: str, out: Optional[Dict],
                              session_close: Optional[Tuple[str, float]] = None) -> Optional[Dict]:
    """yfinance 拿不到 momentum_5d 时，回落自攒收盘价索引（v0.43.25）。

    索引来源是 pheromone.db 的权威价 + 期权快照，**完全不经外部接口**，
    因此不受 yfinance 限流影响——而限流恰恰是本函数长期返回 None 的唯一原因
    （2026-08-14 全天 363 条 429）。

    volume_ratio 不由本函数回落：自攒序列只有收盘价、没有成交量，硬造一个
    比值等于编数据。它的回落见下面 `_fill_volume_from_twelvedata`——
    独立数据源，不是拿这份收盘价索引硬凑。

    缓存目录与权威价库都由 `price_history` 在调用时经 `PATHS` 解析（v0.45.463）。
    此前这里传 `os.environ.get("ALPHA_HIVE_CACHE_DIR", "cache")`：生产不设该变量，
    cwd 又是代码克隆（09-26 数据根迁移 / 10-09 生产克隆之后），回落恒读空。

    `session_close=(ISO 日期, 价)`（v0.45.465）：**已核实**的当场官方收盘（来源见
    `_cboe_session_close`）。扫描日当天的 DB 行要到取数之后才写，索引自 08-15/16 起
    不再增长 ⇒ 不传它时窗口末端是上一个扫描日，比 yfinance（收盘后含当天那根）
    滞后一个扫描日。只在内存里并进序列，不写索引。结果带 `momentum_as_of`
    （窗口末端）；传了却没进窗口（值被拒 / 非交易日）打 warning——那一天的值是滞后的。
    盘中价 / 盘中陈旧文件 / 判不了归属的价**不许**从这里进：它们不是收盘。

    生产唯一调用点：`_fill_history_fields`（v0.45.473；AST 守卫
    `tests/test_fetcher_single_exit.py`）——别再挂到某个源或某条分支里。
    """
    if out is not None and out.get("momentum_5d") is not None:
        return out
    try:
        from price_history import momentum_5d_detail as _idx_momentum
        r = _idx_momentum(ticker, latest_close=session_close)
    except Exception as e:  # noqa: BLE001
        # v0.45.470：原为 debug。「索引里没数据」走的是 `r is None`（下面那支）；走到这里是
        # 代码 / 环境坏了（如路径解析抛错），与「没数据」不是一回事，必须看得见。
        _log.warning("[%s] 自攒动量索引回落出错: %s: %s", ticker, type(e).__name__, e)
        return out
    if r is None:
        return out  # 索引也没有 → 保持 None，诚实缺数据
    m, as_of = r["momentum_5d"], r["as_of"]
    out = dict(out) if out else {}
    out["momentum_5d"] = m
    out["momentum_source"] = "price_index"
    out["momentum_as_of"] = as_of
    if session_close is not None and as_of != session_close[0]:
        _log.warning("[%s] 当场收盘 %s@%s 没进自攒索引窗口（末端 %s）—— momentum_5d 滞后",
                     ticker, session_close[1], session_close[0], as_of)
    _log.info("[%s] momentum_5d 由自攒索引补上: %+.2f%%（截至 %s；yfinance 不可用）",
              ticker, m, as_of)
    return out


def _cboe_session_close(payload: dict) -> Optional[Tuple[str, float]]:
    """CBOE payload 的 `close` 若是**已核实**的官方收盘 → `(场次 ISO 日期, 价)`，否则 None（v0.45.465）。

    判据只用 `cboe_options.close_verdict`（「这个 close 是不是官方收盘、属于哪一场」的唯一实现），
    不另起规则：盘中（那一场还没收）、盘中生成的陈旧文件、缺 `last_trade_time` 判不了归属，
    一律不收。场次日期取 `last_trade_time` 那一场——周末 / 盘前补跑拿到的是上一场，不是今天。

    读的是 `payload["close"]`，**不是**扫描采用的 `official_price`：后者按本机钟选字段，
    半日市收盘后、16:00 前会给 `current_price`（盘后价）；`close_verdict` 判的正是 `close`。
    """
    try:
        from cboe_options import close_verdict, CLOSE_OFFICIAL
        verdict, session = close_verdict(payload)
        px = float((payload or {}).get("close"))
    except Exception as e:  # noqa: BLE001 —— 判不了就不收，不阻断取数
        _log.debug("CBOE 当场收盘判不了: %s", e)
        return None
    if verdict != CLOSE_OFFICIAL or session is None or not (math.isfinite(px) and px > 0):
        return None
    return session.isoformat(), px


def _latest_closed_session() -> Optional[str]:
    """此刻「最近一场已收盘」的交易日（ISO），即 yfinance 主路径的日线窗口会结束的那一天（v0.45.505）。

    交易日且已过该场收盘（提前收盘日 13:00）⇒ 当天；交易日盘前 / 盘中 ⇒ 前一交易日；周末 / 假日 ⇒ 之前
    最近一个交易日。规则全部复用 `portfolio_greeks`（`_expected_session` 判「这天用哪一场」、`_session_closed`
    判「收了没」、`_et_now` 是唯一一只钟），不另抄一份交易日算术。
    判不了（日历 / 时区不可用）⇒ None：调用方据此**拒收**，不猜。
    """
    try:
        import is_trading_day as _cal  # noqa: F401 —— 先证日历可用：下面两个 helper 判不了时会静默给值
        from portfolio_greeks import _et_now, _expected_session, _session_closed
        now = _et_now()
        today = now.date()
        s = _expected_session(today.isoformat())
        if s == today.isoformat() and not _session_closed(s, now):
            s = _expected_session((today - timedelta(days=1)).isoformat())
        return s
    except Exception as e:  # noqa: BLE001
        _log.warning("判不了最近一场已收盘的交易日: %s: %s", type(e).__name__, e)
        return None


def _fill_volume_from_twelvedata(ticker: str, out: Optional[Dict]) -> Optional[Dict]:
    """yfinance 拿不到 volume_ratio 时，回落 Twelve Data 日K（v0.45.81）。

    独立数据源（800次/天、8次/分免费档，见 `twelve_data.py`），不经 yfinance，
    不受同一限流影响。未配置 key 或接口失败时安静返回 None，不阻断降级链——
    与 `_fill_momentum_from_index` 对 momentum_5d 的处理对称。

    不去动 momentum_5d：那条已经有自己的回落路径（自攒价格索引），
    这里只补 volume_ratio 这一项缺口。

    **只收「末根 == 此刻最近一场已收盘」的比值**（v0.45.505，`_latest_closed_session`）：Twelve Data 当日那根
    当晚是临时值、被 `twelve_data._drop_forming_bar` 规则 ① 丢掉（**那条规则是对的，勿改**），所以收盘后的生产
    扫描（~17:15 ET）里它的比值止于 D-1，而 yfinance 主路径含 D。旧行为把 D-1 的比值当 D 的收下：2026-09-15
    NVDA 报 1.01（D-1 1.0132），当天实际 0.676——一个 0.68× 的缩量日被说成量能正常。对不上 ⇒ 拒收（WARNING
    点名标的 / 末根 / 应有场次），volume_ratio 留 None ⇒ `still_missing` ⇒ P2。盘中 / 盘前 / 周末跑时应有场次是
    前一交易日，与 Twelve Data 末根一致，照收。收下时结果带 `volume_as_of`。
    没有免费源能在扫描时给出 D 的定稿成交量（CBOE 收盘后偏低中位 19%、Massive 免费档 19:00 ET 前 403、
    Tiingo 21:00 ET 前仍在改），所以这里是「宁缺」，不是去别处补。

    生产唯一调用点：`_fill_history_fields`（v0.45.473），同上。
    """
    if out is not None and out.get("volume_ratio") is not None:
        return out
    try:
        from twelve_data import fetch_volume_ratio as _td_volume
        v = _td_volume(ticker)
    except Exception as e:  # noqa: BLE001
        _log.debug("[%s] Twelve Data 成交量回落不可用: %s", ticker, e)
        return out
    if v is None:
        return out  # Twelve Data 也没有 → 保持 None，诚实缺数据
    as_of, want = v.get("as_of"), _latest_closed_session()
    if as_of is None or want is None or as_of != want:
        _log.warning("[%s] Twelve Data 量比 %.2fx 末根 %s ≠ 应有场次 %s（最近一场已收盘）—— 拒收，volume_ratio 留 None",
                     ticker, v["volume_ratio"], as_of, want)
        return out
    out = dict(out) if out else {}
    out["volume_ratio"] = v["volume_ratio"]
    out["avg_volume"] = v["avg_volume"]
    out["volume_source"] = "twelvedata"
    out["volume_as_of"] = as_of
    _log.info("[%s] volume_ratio 由 Twelve Data 补上: %.2fx（截至 %s；yfinance 不可用）",
              ticker, v["volume_ratio"], as_of)
    return out


class CBOESource:
    """主数据源：CBOE 延迟报价（options 端点自带现价，稳定无限流；用户定调"股价也走 CBOE"）

    价格来自 CBOE；momentum_5d/volume_ratio 从 yfinance 历史K线独立获取
    （P0-2：CBOE 只有当日涨跌幅，1 日近似冒充 5 日动量会污染背离检测，
    历史K线不可得时诚实置 None；自攒索引 / Twelve Data 回落在 MultiSourceFetcher 出口，v0.45.473）。
    """

    def __init__(self):
        self.breaker = ObservableCircuitBreaker("cboe", failure_threshold=3, recovery_timeout=60)
        self.name = "cboe"

    def fetch(self, ticker: str) -> Optional[StockData]:
        if not self.breaker.allow_request():
            return None
        try:
            from cboe_options import _fetch_cboe_payload
            payload = _fetch_cboe_payload(ticker, timeout=15)
            if not payload:
                self.breaker.record_failure("no_data")
                return None
            # v0.45.46：按交易时段选字段。旧写法 `current_price or close` 在
            # 收盘后拿到的是**盘后价**——全部定时扫描都在 17:00 ET 跑。
            # 实测 CRM 2026-08-26（财报日）盘后 232.32 vs 官方收盘 205.62。
            from cboe_options import official_price, STALE_INTRADAY_SOURCE
            price, _px_src = official_price(payload)
            if price <= 0:
                self.breaker.record_failure("zero_price")
                return None

            # v0.45.234：收盘后拿到的是盘中生成的 payload（CDN 没刷新）——
            # close 是那一刻的成交价，当入场价会错 ~1%（T 09-11 +0.63%、TMUS 09-10 −1.12%）。
            # 交给后面的源取官方收盘；全失败时 MultiSourceFetcher 才退回用它。
            # ⚠️ **不记熔断失败**：CBOE 应答正常，只是这只票的文件旧。记了的话
            # 一天 3~8 只里连撞 3 只就会熔断，把其余健康标的一起推去挤 yfinance 限流。
            # 也**不拉**历史K线：多半用不上，白耗一次 yfinance 配额。
            if _px_src == STALE_INTRADAY_SOURCE:
                self.breaker.record_success()
                return StockData(
                    price=price,
                    data_source=DataQuality.DEGRADED,
                    source_name=self.name,
                    price_source=_px_src,
                    momentum_source="unavailable",
                    fetch_timestamp=time.time(),
                    defer=True,
                    # 盘中陈旧文件的 close 不是官方收盘 ⇒ 这里恒为 None；照同一规则取，不另写判据
                    session_close=_cboe_session_close(payload),
                )

            data = StockData(
                price=price,
                data_source=DataQuality.REAL,
                source_name=self.name,
                price_source=_px_src,
                fetch_timestamp=time.time(),
                # v0.45.465 / 473：已核实的当场官方收盘，随数据带出去，由出口的
                # momentum 回落当窗口末端（不在本源里回落）。
                session_close=_cboe_session_close(payload),
            )
            # P0-2: 历史指标独立获取；取不到的项留 None（不再 1 日近似），
            # 由 MultiSourceFetcher 出口统一回落（v0.45.473）。
            metrics = _fetch_history_metrics(ticker)
            if metrics:
                # v0.45.473：逐项取。此前要求 "momentum_5d" 在才收，yfinance 不足 5 根时
                # 连算出来的 volume_ratio 也一并丢掉。
                # v0.45.54：`.get(k, default)` 挡不住「键存在但值为 None」——
                # _fetch_history_metrics 会把不可得的均量置 None。
                data.momentum_5d = metrics.get("momentum_5d")
                data.momentum_source = "5d_real" if data.momentum_5d is not None else "unavailable"
                data.avg_volume = metrics.get("avg_volume")
                data.volume_ratio = metrics.get("volume_ratio")
                data.volume_source = "yfinance" if data.volume_ratio is not None else None
                data.volatility_20d = metrics.get("volatility_20d")   # v0.45.3: 不再补 0.0
            else:
                data.momentum_source = "unavailable"
                _log.warning("[CBOE] %s 历史K线不可得，momentum_5d/volume_ratio 待出口回落", ticker)
            self.breaker.record_success()
            return data

        except Exception as e:
            self.breaker.record_failure(str(e))
            _log.warning("[CBOE] fetch %s failed: %s", ticker, e)
            return None


class YFinanceSource:
    """降级源 0：yfinance（CBOE 无数据/失败时的备用，免费，实时性好）"""

    def __init__(self):
        self.breaker = ObservableCircuitBreaker("yfinance", failure_threshold=3, recovery_timeout=60)
        self.name = "yfinance"

    def fetch(self, ticker: str) -> Optional[StockData]:
        if not self.breaker.allow_request():
            return None
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo")
            if hist.empty or len(hist) < 2:
                self.breaker.record_failure("empty_history")
                return None

            # v0.29.4 盘中护栏：丢弃"当日正在形成"的盘中 bar，确保下游
            # 价格/动量/成交量全用已收盘日线（盘中跑扫描时取的是收盘价而非盘中价）
            hist = _drop_forming_bar(hist)
            if hist.empty or len(hist) < 2:
                self.breaker.record_failure("empty_history")
                return None

            data = StockData(
                data_source=DataQuality.REAL,
                source_name=self.name,
                fetch_timestamp=time.time(),
            )
            data.price = float(hist["Close"].iloc[-1])
            data.price_source = "yfinance_daily_close"
            # v0.45.473：末根**确知已收盘**时就是一场官方收盘——不足 5 根时出口的 momentum 回落拿它当窗口末端。
            # v0.45.477：判不了就不给（见 `_completed_bar_close`），盘中价不许进索引窗口。
            data.session_close = _completed_bar_close(hist)

            if len(hist) >= 5:
                data.momentum_5d = (hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100
                data.momentum_source = "5d_real"
            else:
                data.momentum_source = "unavailable"

            recent_vol = float(hist["Volume"].iloc[-1])
            avg_vol = float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20 else float(hist["Volume"].mean())
            # v0.45.473：原兜底 avg_vol=1.0 ⇒ avg_volume=1 股、volume_ratio=当日成交量本身（千万量级）——
            # `_fetch_history_metrics` 在 v0.45.54 已改成 None，这里一直漏着。置 None，交给出口回落。
            if not (math.isnan(avg_vol) or avg_vol <= 0):
                data.avg_volume = int(avg_vol)
                data.volume_ratio = recent_vol / avg_vol
                data.volume_source = "yfinance"

            if len(hist) >= 20:
                returns = hist["Close"].pct_change().dropna()
                if len(returns) >= 2:
                    vol = float(returns.std() * (252 ** 0.5) * 100)
                    if not (math.isnan(vol) or math.isinf(vol)):
                        data.volatility_20d = vol

            self.breaker.record_success()
            return data

        except Exception as e:
            self.breaker.record_failure(str(e))
            _log.warning("[yfinance] fetch %s failed: %s", ticker, e)
            return None


class AlphaVantageSource:
    """降级源 1：Alpha Vantage（免费 25次/天，需 API Key）"""

    def __init__(self):
        # v0.43.26: 原为 os.environ.get("ALPHA_VANTAGE_API_KEY")——环境变量未设，
        # 而 key 实际在 ~/.alpha_hive_av_key。改用集中的 config.get_secret
        # （环境变量优先 → 降级文件，且校验文件权限）。
        # 兼容旧环境变量名：注册表登记的是 AV_API_KEY，与此处历史用名不同。
        self.api_key = _get_secret("AV_API_KEY") or os.environ.get("ALPHA_VANTAGE_API_KEY", "")
        self.breaker = ObservableCircuitBreaker("alpha_vantage", failure_threshold=5, recovery_timeout=120)
        self.name = "alpha_vantage"

    def fetch(self, ticker: str) -> Optional[StockData]:
        if not self.api_key or not self.breaker.allow_request():
            return None
        try:
            import json

            url = (
                f"https://www.alphavantage.co/query?"
                f"function=GLOBAL_QUOTE&symbol={ticker}&apikey={self.api_key}"
            )
            # v0.43.27: 同 Finnhub，走全局 HTTPS 闸门
            from http_gate import urlopen_gated
            result = json.loads(urlopen_gated(url, timeout=10).decode())

            quote = result.get("Global Quote", {})
            price = float(quote.get("05. price", 0))
            if price <= 0:
                self.breaker.record_failure("zero_price")
                return None

            change_pct = float(quote.get("10. change percent", "0").rstrip("%"))
            volume = int(quote.get("06. volume", 0))

            data = StockData(
                price=price,
                # P0-2: 不再用日涨跌幅冒充 5 日动量——诚实置 None，
                # 下游（背离检测/情绪映射）按 5 日语义消费，1 日近似会误判
                momentum_5d=None,
                avg_volume=volume,
                volume_ratio=None,  # AV 免费版无法计算量比（原 1.0 会被当"正常量"消费）
                data_source=DataQuality.DEGRADED,
                source_name=self.name,
                momentum_source="unavailable",
                fetch_timestamp=time.time(),
            )
            self.breaker.record_success()
            return data

        except Exception as e:
            self.breaker.record_failure(str(e))
            _log.warning("[AlphaVantage] fetch %s failed: %s", ticker, e)
            return None


class FinnhubSource:
    """降级源 2：Finnhub（免费 60次/分钟，需 API Key）"""

    def __init__(self):
        # v0.43.26: 同上。key 在 ~/.alpha_hive_finnhub_key，环境变量从未设过，
        # 所以这条降级链的最后一环一直是死的。
        self.api_key = _get_secret("FINNHUB_API_KEY")
        self.breaker = ObservableCircuitBreaker("finnhub", failure_threshold=5, recovery_timeout=90)
        self.name = "finnhub"

    def fetch(self, ticker: str) -> Optional[StockData]:
        if not self.api_key or not self.breaker.allow_request():
            return None
        try:
            import json

            url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={self.api_key}"
            # v0.43.27: 必须走全局 HTTPS 闸门。本源自 v0.43.26 接通后才真正开始
            # 发请求（此前拿不到 key、一个请求都不发），是个不受闸门约束的新调用方。
            # 走闸门的理由是不给 Finnhub 的配额/限流器加压。
            # ⚠️ 原注称它「正是 8/24 EOF 风暴的新增变量」并归因于 OpenSSL 1.1.1q：
            # 该归因 2026-08-25 已证伪，8/24 根因未定（见 http_gate docstring）。
            from http_gate import urlopen_gated
            quote = json.loads(urlopen_gated(url, timeout=10).decode())

            current = float(quote.get("c", 0))
            prev_close = float(quote.get("pc", 0))

            if current <= 0:
                self.breaker.record_failure("zero_price")
                return None

            change_pct = ((current / prev_close) - 1) * 100 if prev_close > 0 else 0.0

            data = StockData(
                price=current,
                momentum_5d=None,  # P0-2: 不再用日涨跌幅冒充 5 日动量
                volume_ratio=None,  # Finnhub quote 无成交量数据
                data_source=DataQuality.DEGRADED,
                source_name=self.name,
                momentum_source="unavailable",
                fetch_timestamp=time.time(),
            )
            self.breaker.record_success()
            return data

        except Exception as e:
            self.breaker.record_failure(str(e))
            _log.warning("[Finnhub] fetch %s failed: %s", ticker, e)
            return None


# ==================== 历史字段出口回落（v0.45.473）====================
#
# momentum_5d / volume_ratio 各有一条独立于主价源的回落（自攒价格索引 / Twelve Data）。
# 回落是**出口的属性**，不是某个源的：不管哪个源胜出、走哪个出口，缺的项都在这里补一次。
# 此前挂在 CBOESource 内部 + 「全源失败」一支（v0.45.270）——Finnhub / AV 胜出、过期缓存、
# CBOE 盘中陈旧价三个出口，以及 CBOE 内部「yfinance 空历史 / 均量 NaN」两条分支都够不着
# （auto-memory `alpha-hive-fallback-not-reached.md` 的第四次）。

_HISTORY_FIELDS = ("momentum", "volume")
_history_lock = threading.Lock()
# 本进程还没解析过任何标的 ⇒ None（scan_timing 约定：None 是「没跑」，不是「零次」）
_history_stats: Optional[Dict] = None
_history_last: Dict[str, Dict[str, bool]] = {}   # ticker → {字段: 最近一次出口后仍缺}


def _record_history_fill(ticker: str, path: str, outcome: Dict[str, str],
                         window_behind: bool) -> None:
    global _history_stats
    with _history_lock:
        if _history_stats is None:
            _history_stats = {"resolutions": 0, "by_path": {}, "window_behind_session": 0}
        _history_stats["resolutions"] += 1
        p = _history_stats["by_path"].setdefault(
            path, {"n": 0, **{f: {"needed": 0, "filled": 0, "errors": 0} for f in _HISTORY_FIELDS}})
        p["n"] += 1
        for f in _HISTORY_FIELDS:
            o = outcome.get(f, "present")
            if o == "present":
                continue
            p[f]["needed"] += 1
            if o == "filled":
                p[f]["filled"] += 1
            elif o == "error":
                p[f]["errors"] += 1
        if window_behind:
            _history_stats["window_behind_session"] += 1
        _history_last[ticker] = {f: outcome.get(f) in ("missing", "error") for f in _HISTORY_FIELDS}


def history_fill_stats() -> Optional[Dict]:
    """本进程出口回落计数（v0.45.473）→ `scan_timing.counters()["history_fill"]` ⇒ status.json。

    - `by_path[出口]`：`n` 次解析；每个字段 `needed`（出口时缺）/ `filled`（回落补上）/ `errors`（回落抛错）。
      出口名：`source:<源名>` / `deferred:cboe` / `all_sources_failed`。
      缓存出口（新鲜 / 过期）带的是**存入时**那次出口的结果，不重跑回落、不计——生产里缓存对象都已
      过出口，所以 `cache_expired` 只会在有人绕过 `_emit` 写缓存时出现（v0.45.477 更正：473 写成会计）。
      「这一轮用了几次过期缓存」看 `[DataPipeline] … 使用过期缓存` 日志与 `get_all_metrics()["fetch_stats"]["stale"]`。
    - `window_behind_session`：回落给了 momentum，但窗口末端早于该源核实的那一场收盘（值滞后）。
    - `still_missing`：各字段在**最近一次**出口后仍为 None 的标的 ⇒ alert_manager P2。
    本进程没解析过任何标的 ⇒ None。
    """
    with _history_lock:
        if _history_stats is None:
            return None
        out = {
            "resolutions": _history_stats["resolutions"],
            "window_behind_session": _history_stats["window_behind_session"],
            "by_path": {k: {kk: (dict(vv) if isinstance(vv, dict) else vv) for kk, vv in v.items()}
                        for k, v in _history_stats["by_path"].items()},
        }
        out["still_missing"] = {f: sorted(t for t, m in _history_last.items() if m.get(f))
                                for f in _HISTORY_FIELDS}
        return out


def reset_history_fill_stats() -> None:
    global _history_stats
    with _history_lock:
        _history_stats = None
        _history_last.clear()


def _fill_history_fields(ticker: str, data: StockData, path: str) -> None:
    """出口唯一的历史字段回落（v0.45.473）：`data` 缺 momentum_5d / volume_ratio 就各补一次，就地改。

    标签与值同一处定：源自己算的值由源标（`5d_real` / `yfinance`），这里补上的标
    `price_index`（+ `momentum_as_of`）/ `twelvedata`；补不上的标 `unavailable`。
    `data.session_close`（源核实过的某一场官方收盘）交给 momentum 回落当窗口末端（v0.45.465）。
    已补过（`history_checked`，缓存命中）不重跑——Twelve Data 有日配额。
    """
    if data.history_checked:
        return
    outcome: Dict[str, str] = {}
    window_behind = False

    if data.momentum_5d is None:
        try:
            r = _fill_momentum_from_index(ticker, None, data.session_close)
        except Exception as e:  # noqa: BLE001 —— 回落自身出错不得拖垮取数，但要看得见（计 errors）
            _log.warning("[%s] momentum_5d 出口回落出错（%s）: %s: %s",
                         ticker, path, type(e).__name__, e)
            outcome["momentum"] = "error"
        else:
            if r and r.get("momentum_5d") is not None:
                data.momentum_5d = r["momentum_5d"]
                data.momentum_source = "price_index"
                data.momentum_as_of = r.get("momentum_as_of")
                outcome["momentum"] = "filled"
                window_behind = (data.session_close is not None
                                 and data.momentum_as_of != data.session_close[0])
            else:
                outcome["momentum"] = "missing"
        if data.momentum_5d is None:
            data.momentum_source = "unavailable"

    if data.volume_ratio is None:
        try:
            r = _fill_volume_from_twelvedata(ticker, None)
        except Exception as e:  # noqa: BLE001
            _log.warning("[%s] volume_ratio 出口回落出错（%s）: %s: %s",
                         ticker, path, type(e).__name__, e)
            outcome["volume"] = "error"
        else:
            if r and r.get("volume_ratio") is not None:
                data.volume_ratio = r["volume_ratio"]
                data.avg_volume = r.get("avg_volume")
                data.volume_source = "twelvedata"
                data.volume_as_of = r.get("volume_as_of")
                outcome["volume"] = "filled"
            else:
                outcome["volume"] = "missing"

    data.history_checked = True
    _record_history_fill(ticker, path, outcome, window_behind)


# ==================== 多源聚合器 ====================

class MultiSourceFetcher:
    """
    多源数据降级链：CBOE → yfinance → Alpha Vantage → Finnhub → 陈旧缓存 → 安全默认值

    核心原则：
    1. 永远不返回 price=100 的虚假数据
    2. 每条数据都带质量标记（real/degraded/stale/fallback）
    3. 降级时自动告警
    4. 所有熔断器指标可查询
    """

    # 缓存配置
    CACHE_TTL_REAL = 300       # 5分钟（real 数据）
    CACHE_TTL_DEGRADED = 120   # 2分钟（降级数据，更快刷新）
    CACHE_TTL_STALE = 3600     # 1小时（过期数据作为最后防线）
    MAX_CACHE_SIZE = 500

    def __init__(self):
        self._sources: List = [
            CBOESource(),
            YFinanceSource(),
            # v0.43.26: Finnhub 提到 AV 之前。额度差一个量级——Finnhub 60 次/分钟，
            # AV 只有 25 次/天。而需要降级的场景恰恰是"30 只标的批量抓取失败"，
            # 把日额度只有 25 的源排在前面，等于第 26 只起必然再降一级。
            FinnhubSource(),
            AlphaVantageSource(),
        ]
        self._cache: Dict[str, StockData] = {}
        self._cache_ts: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._insert_order: deque = deque()

        # 统计
        self._fetch_stats = {
            "total": 0, "real": 0, "degraded": 0,
            "stale": 0, "fallback": 0,
        }

    def fetch(self, ticker: str) -> Dict:
        """
        获取股票数据（自动降级链 + 缓存）
        返回字典格式，兼容现有 _fetch_stock_data 接口

        v0.45.473：**唯一出口**。分支全在 `_resolve`（只返回 StockData，不出字典），
        这里只有一个 return，经 `_emit` → `_fill_history_fields` 统一补历史字段。
        此前回落只挂在 CBOE 源内部与「全源失败」那一支：Finnhub / AV 胜出、
        过期缓存、CBOE 盘中陈旧价三个出口都把 momentum_5d / volume_ratio 原样 None 交出去。
        守卫：`tests/test_fetcher_single_exit.py`（AST：一个 return、回落函数唯一调用点）。
        """
        data, path, cacheable = self._resolve(ticker)
        return self._emit(ticker, data, path, cacheable)

    def _emit(self, ticker: str, data: StockData, path: str, cacheable: bool) -> Dict:
        """出口：补历史字段 → 入缓存（缓存存补过的对象）→ 字典。"""
        _fill_history_fields(ticker, data, path)
        if cacheable:
            self._set_cache(ticker, data)
        out = data.to_dict()
        if data.data_source == DataQuality.FALLBACK:
            # 标记数据不可用，下游 Agent 应跳过分析
            out["_data_unavailable"] = True
        return out

    def _resolve(self, ticker: str) -> Tuple[StockData, str, bool]:
        """降级链本体 → `(数据, 出口名, 是否入缓存)`。**只返回 StockData**——
        出字典是 `_emit` 的事（结构守卫钉住本函数不许调 `.to_dict()`）。"""
        with self._lock:
            self._fetch_stats["total"] += 1

        # 1. 检查新鲜缓存
        cached = self._get_cache(ticker)
        if cached and cached.data_source in (DataQuality.REAL, DataQuality.DEGRADED):
            ttl = (self.CACHE_TTL_REAL if cached.data_source == DataQuality.REAL
                   else self.CACHE_TTL_DEGRADED)
            if time.time() - cached.fetch_timestamp < ttl:
                return cached, "cache_fresh", False

        # 2. 尝试降级链
        deferred: Optional[StockData] = None
        for source in self._sources:
            data = source.fetch(ticker)
            if data and data.price > 0 and data.defer:
                deferred = deferred or data
                continue
            if data and data.price > 0:
                with self._lock:
                    self._fetch_stats[data.data_source] += 1
                if data.data_source != DataQuality.REAL:
                    _log.warning(
                        "[DataPipeline] %s 降级到 %s（源：%s）",
                        ticker, data.data_source, data.source_name
                    )
                return data, f"source:{data.source_name}", True

        # 3. 使用过期缓存（stale）— cached 已是副本，修改不影响缓存
        if cached and cached.price > 0:
            stale_age = time.time() - cached.fetch_timestamp
            if stale_age < self.CACHE_TTL_STALE:
                cached.data_source = DataQuality.STALE  # 安全：修改的是副本
                with self._lock:
                    self._fetch_stats["stale"] += 1
                _log.warning(
                    "[DataPipeline] %s 使用过期缓存（%.0f秒前，源：%s）",
                    ticker, stale_age, cached.source_name
                )
                return cached, "cache_expired", False

        # 3.5 (v0.45.234) 被推迟的数（CBOE 盘中陈旧价）：比 price=0 让整只标的跳过强，
        # 但必须带着 degraded + price_source 标签出去，不能冒充官方收盘。
        if deferred is not None:
            with self._lock:
                self._fetch_stats[deferred.data_source] += 1
            _log.warning(
                "[DataPipeline] %s 其余源全部失败，退用 %s 的 %s 价 %.4f（非官方收盘）",
                ticker, deferred.source_name, deferred.price_source, deferred.price
            )
            return deferred, f"deferred:{deferred.source_name}", True

        # 4. 最后防线：安全默认值（明确标记为 fallback；`_emit` 加 `_data_unavailable`）
        # v0.45.270 起这一支也补 momentum_5d / volume_ratio（2026-09-17 CBOE 熔断 + yfinance
        # 同时中断，独立回落链连尝试的机会都没有）；v0.45.473 起和其余出口一样由 `_emit` 统一补——
        # BuzzBee/ChronosBee 读这两项时不检查 `_data_unavailable`（设计成部分降级）。
        with self._lock:
            self._fetch_stats["fallback"] += 1
        _log.error(
            "[DataPipeline] %s 所有数据源不可用！返回 FALLBACK 默认值（不可用于交易决策）",
            ticker
        )
        return StockData(
            price=0.0,
            data_source=DataQuality.FALLBACK,
            source_name="none",
            fetch_timestamp=time.time(),
        ), "all_sources_failed", False

    def _get_cache(self, ticker: str) -> Optional[StockData]:
        with self._lock:
            cached = self._cache.get(ticker)
            if cached is None:
                return None
            # 返回副本，防止 stale 路径修改原缓存对象
            import copy
            return copy.copy(cached)

    def _set_cache(self, ticker: str, data: StockData):
        with self._lock:
            if ticker not in self._cache:
                self._insert_order.append(ticker)
            self._cache[ticker] = data
            self._cache_ts[ticker] = time.time()
            # LRU 淘汰
            while len(self._cache) > self.MAX_CACHE_SIZE and self._insert_order:
                oldest = self._insert_order.popleft()
                self._cache.pop(oldest, None)
                self._cache_ts.pop(oldest, None)

    def get_all_metrics(self) -> Dict:
        """获取所有数据源的熔断器指标 + 缓存统计"""
        metrics = {
            "fetch_stats": dict(self._fetch_stats),
            "cache_size": len(self._cache),
            "sources": {},
        }
        for source in self._sources:
            metrics["sources"][source.name] = source.breaker.get_metrics()
        return metrics

    def get_health_summary(self) -> str:
        """一行健康摘要（供日志/Slack 告警使用）"""
        m = self.get_all_metrics()
        parts = []
        for name, sm in m["sources"].items():
            state = sm["state"]
            rate = sm["success_rate"]
            emoji = "🟢" if state == "closed" else ("🟡" if state == "half_open" else "🔴")
            parts.append(f"{emoji}{name}({state},{rate}%)")
        fs = m["fetch_stats"]
        total = fs["total"] or 1
        real_pct = round(fs["real"] / total * 100, 1)
        return f"数据管道: {' | '.join(parts)} | 真实数据率:{real_pct}% | 缓存:{m['cache_size']}"


# ==================== 集成接口 ====================

# 全局单例
_fetcher: Optional[MultiSourceFetcher] = None
_fetcher_lock = threading.Lock()


def get_fetcher() -> MultiSourceFetcher:
    """获取全局 MultiSourceFetcher 单例"""
    global _fetcher
    if _fetcher is None:
        with _fetcher_lock:
            if _fetcher is None:
                _fetcher = MultiSourceFetcher()
    return _fetcher


def fetch_stock_data(ticker: str, as_of_date: Optional[str] = None) -> Dict:
    """
    直接替换 swarm_agents/cache.py 中的 _fetch_stock_data()

    用法（在 swarm_agents/cache.py 中）：
        from data_pipeline import fetch_stock_data as _fetch_stock_data

    v0.41.6: as_of_date 非空且不等于今天（真实 PDT 当日）时，视为 `--date`
    补跑历史交易日——改走 yfinance 历史K线锚定该日期真实收盘价，不再命中
    CBOE/AV/Finnhub 的"当前实时报价"缓存链。as_of_date 为 None 或等于今天时
    行为与之前完全一致（当日实时扫描不受影响）。
    """
    if as_of_date:
        try:
            from hive_logger import pdt_today
            _today = pdt_today()
        except ImportError:
            _today = None
        if _today is None or as_of_date != _today:
            return _fetch_historical_stock_data(ticker, as_of_date)
    return get_fetcher().fetch(ticker)
