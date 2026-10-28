"""
🐝 Alpha Hive - FRED 宏观经济层 (P5)
为 GuardBeeSentinel 提供宏观环境过滤器

数据源（免费，无需 API Key）：
- ^VIX → CBOE 恐慌指数（yfinance）
- ^TNX → 10年期美债收益率（yfinance）
- ^FVX → 5年期美债收益率（yfinance，收益率曲线 fallback）
- ^DXY / DX-Y.NYB → 美元指数（yfinance）
- ^GSPC → 标普 500（yfinance，判断大盘环境）
- TLT → 长债 ETF（债市情绪）
- GLD → SPDR 黄金 ETF（避险情绪指标）
- 11 SPDR 板块 ETF → 板块轮动跟踪

可选 FRED API（免费注册 fred.stlouisfed.org 获取 key）：
- 设置环境变量 FRED_API_KEY 可解锁 CPI、PMI、2Y 国债收益率等
"""

import datetime as _dt
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Tuple

from bars_integrity import session_change
from hive_logger import get_logger

_log = get_logger("fred_macro")

# 弹性层：熔断器 + 连接池
try:
    from resilience import get_session, CircuitBreaker
    _fred_breaker = CircuitBreaker("fred", failure_threshold=5, recovery_timeout=180.0)
    _RESILIENCE_OK = True
except ImportError:
    _fred_breaker = None
    _RESILIENCE_OK = False


def _load_fred_key() -> str:
    """加载 FRED API Key：环境变量 > ~/.alpha_hive_fred_key 文件"""
    # 1. 环境变量
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        return key
    # 2. 文件
    key_file = os.path.expanduser("~/.alpha_hive_fred_key")
    try:
        with open(key_file) as f:
            key = f.read().strip()
            if key:
                return key
    except (OSError, UnicodeDecodeError):
        pass
    return ""

_CACHE: Dict = {}
_CACHE_TS: float = 0.0
try:
    from config import CACHE_CONFIG as _CC
    _CACHE_TTL = _CC["ttl"].get("fred_macro", 1800)
except (ImportError, KeyError):
    _CACHE_TTL = 1800
_lock = threading.Lock()

# 板块 ETF 单独缓存（复用 _CACHE_TTL 30min）
_etf_cache: Dict[tuple, tuple] = {}   # (etf, 那一场) -> (timestamp, (name, chg))；v0.45.663 起按场次存
_etf_lock = threading.Lock()


def get_macro_context() -> Dict:
    """
    获取当前宏观经济环境快照

    Returns:
        {
            # v0.45.669：没观测到就是 None / "unknown"——报价全部没取到、取数出错也一样（旧兜底 VIX 20.0 / 10Y 4.5 /
            # 宏观分 5.0 neutral，只靠 data_source == "fallback" 告诉读者别信）。按字段判，别按标记整块判。
            "macro_regime": "risk_on"/"risk_off"/"neutral"/"unknown",
            "macro_score": float | None (0-10, 10=极度风险偏好；只由观测到的分项算，展示用),
            "macro_score_inputs" / "macro_score_missing": list[str],   # MACRO_SCORE_PARTS 里算进去 / 没观测到的
            "vix": float | None,
            "vix_regime": "low"/"moderate"/"elevated"/"high"/"spike"/"unknown",
            "treasury_10y": float | None,                       # v0.45.663：缺就是 None（旧兜底 4.5）
            "rate_environment": "low"/"moderate"/"high"/"unknown",
            "dollar_trend": "strong"/"weak"/"neutral"/"unknown",
            "market_trend": "bull"/"bear"/"neutral"/"unknown",  # 较前一交易日（1 日）
            "quote_gaps": {符号: 原因},                         # v0.45.663：Yahoo 日线按日期没配上的
            "macro_headwinds": list[str],
            "macro_tailwinds": list[str],
            "summary": str,
            "data_source": str,                                 # 一个来源都没供上 ⇒ "fallback"（只是标签）
            "error": str,                                       # 只在取数代码自己出错的整份出口出现
        }
    """
    global _CACHE, _CACHE_TS

    with _lock:
        if _CACHE and (time.time() - _CACHE_TS) < _CACHE_TTL:
            return _CACHE

    result = _fetch_macro_data()

    with _lock:
        _CACHE = result
        _CACHE_TS = time.time()

    return result


# ══════════════════════════════════════════════════════════════════
# 云端快照供给（v0.45.59）
# ══════════════════════════════════════════════════════════════════
# 补跑（`--date`）时宏观必须是**目标日**的，而不是运行当天的。
# 旧实现两处都错位：
#   · VIX/F&G —— market.json 里存着目标日的真实观测，但 `load_market()`
#     在生产代码里**一个调用者都没有**（典型的「死字段：算了没人读」）
#   · 国债/SPX/美元/黄金 —— `yf.Ticker(sym).history(period="5d")` 永远取
#     最近 5 天，与目标日无关
# 结果：8/27 的报告里宏观是 8/28 的，且没有任何标记说明这一点。
#
# 现在：`as_of` 一旦设定，两条路径同时对齐到该日。
_MACRO_SNAPSHOT: Optional[Dict] = None


def set_macro_snapshot(date: Optional[str], market: Optional[Dict] = None) -> None:
    """装载某日的 market.json 作为宏观取数基准。`date=None` 卸载。

    由 `cloud_snapshot_loader.snapshot_mode` 调用，与期权链供给器同进同出 ——
    只装一半会让报告里期权是目标日的、宏观是今天的，且无从分辨。
    """
    global _MACRO_SNAPSHOT, _CACHE, _CACHE_TS
    _MACRO_SNAPSHOT = None if not date else {"date": date, "market": market or {}}
    _CACHE, _CACHE_TS = None, 0.0        # 口径变了，旧缓存必须作废


def get_macro_snapshot() -> Optional[Dict]:
    return _MACRO_SNAPSHOT


def _asof_history(yf, sym: str, as_of: str):
    """取 `as_of` 当日及其前一交易日的收盘。

    用 start/end 而不是 `period="5d"` —— 后者永远是**最近** 5 天。
    多要 12 个自然日的余量以跨过周末与假期。

    v0.45.652：经 `yf_daily` 取。末行是公司行动空行 = 有事件、却没有那天的 K 线（D 那根缺了）⇒ 返回 None：
    剩下的末根是更早的一场，拿它当 D 就是冒充（此前末行收盘是 NaN，两处调用方也都落到「没取到」，结局相同，现在多一行日志）。

    ⚠️ 只保证「到 D 为止」，**不保证末根是 D**：Yahoo 整根缺 D 又没有事件时照样返回止于更早一场的日线。
    调用方按日期配对（v0.45.663 起两处都用 `bars_integrity.session_change`：缺 D ⇒ anchor_missing，点名进 `quote_gaps` / `gaps`
    与 654 的 `not_on_as_of`），不按位置取。
    """
    import datetime as _dt
    import yf_daily
    d = _dt.date.fromisoformat(as_of)
    hist, last_blank = yf_daily.history(yf.Ticker(sym), source="macro_asof",
                                        start=(d - _dt.timedelta(days=12)).isoformat(),
                                        end=(d + _dt.timedelta(days=1)).isoformat(),
                                        interval="1d")
    if last_blank:
        _log.warning("补跑宏观 %s @ %s：末行是公司行动空行（那天没有 K 线），按没取到处理", sym, as_of)
        return None
    if hist is None or hist.empty:
        return None
    # end 是开区间，但时区/夏令时偶尔会多带一根；显式截断到 as_of 当日为止
    hist = hist[hist.index.date <= d]
    return hist if not hist.empty else None


# ══════════════════════════════════════════════════════════════════
# 当日宏观：脱离 yfinance（v0.45.60）
# ══════════════════════════════════════════════════════════════════
# 2026-08-27：yfinance 全天 687 次 429，这 7 个符号一起归零，整块宏观降级为
# `data_source: "fallback"`，报告里的 `treasury_10y: 4.5` 是兜底常量。
#
# 定时任务在**当日** 17:00 ET 跑，所以替代源必须当天就能出数：
#   ^VIX          → CBOE（早已在用，8/27 限流全程 30/30 完好）
#   ^TNX / ^FVX   → 美国财政部日度曲线（免 key；FRED 的 DGS10 转发的正是它，晚一天）
#   ^GSPC / GLD / DX-Y.NYB / TLT → Finnhub /quote 的 ETF 代理
#
# 为什么用 ETF 代理而不是指数代码：实测 Finnhub 免费档对 `^GSPC` / `^VIX`
# 返回 "Market data subscription required for CFD indices"，而 SPY / GLD /
# UUP / TLT 全通（2026-08-27 实测 771.10 / 422.60 / 28.02 / 83.13）。
# 涨跌幅用 `c` 与 `pc`，与原 yfinance 的 last/prev 口径一致。
#
# yfinance 保留为**最后一环**，不再是第一环。
_ETF_PROXY = {
    "SPX":  ("SPY", "标普 500"),
    "GLD":  ("GLD", "黄金"),
    "DXY":  ("UUP", "美元"),
    "TLT":  ("TLT", "长债"),
}


def _finnhub_quote(symbol: str) -> Optional[Dict]:
    """Finnhub /quote。→ {"last","prev","change_pct","ts"} 或 None。

    拿不到就 None —— 绝不返回 0.0。0.0 会被下游当成「持平」，
    与「没数据」不可区分（MEMORY 静默降级三件套）。
    """
    try:
        from data_pipeline import _get_secret
        key = _get_secret("FINNHUB_API_KEY")
    except Exception:  # noqa: BLE001
        key = ""
    if not key:
        return None
    try:
        import json as _json
        import urllib.request as _ur

        from http_gate import urlopen_gated
        req = _ur.Request(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={key}",
                          headers={"User-Agent": "alpha-hive/1.0"})
        q = _json.loads(urlopen_gated(req, timeout=15))
    except Exception as e:  # noqa: BLE001
        _log.debug("Finnhub quote 失败 %s: %s", symbol, e)
        return None
    try:
        last = float(q.get("c") or 0)
        prev = float(q.get("pc") or 0)
    except (TypeError, ValueError):
        return None
    if last <= 0:
        return None
    # v0.45.663：没有前收盘 ⇒ 涨跌未知（None），不再拿 last 当 prev、给 0.0 冒充「持平」
    return {
        "last": last,
        "prev": prev if prev > 0 else None,
        "change_pct": ((last / prev) - 1) * 100 if prev > 0 else None,
        "ts": q.get("t"),
    }


def _same_day_macro_data(as_of: Optional[str] = None) -> Tuple[Dict, Dict[str, str]]:
    """当日口径取数：财政部 + Finnhub ETF。→ (data, sources)

    `data` 与原 yfinance 分支同形（{name: {last, prev, change_pct}}），
    这样下游分析逻辑一行都不用改。
    `sources` 逐字段记录来源，供 `data_source` 如实汇总 ——
    不同源的值同形，不标源就无从分辨（MEMORY「读 vix 前先看 vix_source」）。
    """
    data: Dict[str, Dict] = {}
    sources: Dict[str, str] = {}

    # ① 国债：一次请求拿到 2Y/5Y/10Y
    #
    # v0.45.61：补跑时按**目标日**取。财政部一次返回整月，指定日期不额外发请求；
    # 该日无数据（周末/假日/未发布）时 `get_yield_curve` 返回 None，**不会**
    # 悄悄给前一交易日。补跑走 FRED 也对，但 FRED 转发的就是这份数据且晚一天
    # ——实测 8/28 查询时 FRED 的 2Y 是 4.19@08-26，财政部已有 4.20@08-27。
    try:
        from treasury_yields import get_yield_curve
        cur = get_yield_curve(as_of)
        if cur:
            for name, k in (("TNX", "y10"), ("FVX", "y5"), ("TWO", "y2")):
                v = cur.get(k)
                if v is not None:
                    # 口径注意：财政部给的是 par yield 本身，**不是** ^TNX 的 ×10。
                    # v0.45.663：只取了这一天 ⇒ 涨跌未知（None）；旧写 prev=v、0.0 冒充「持平」，
                    # 让「10Y 持续走高」那条逆风在实时日永远不可能触发
                    data[name] = {"last": v, "prev": None, "change_pct": None}
                    sources[name] = f"treasury_gov@{cur['date']}"
    except Exception as e:  # noqa: BLE001
        _log.debug("财政部曲线不可用: %s", e)

    # ② 大盘 / 黄金 / 美元 / 长债：ETF 代理。
    # **仅当日口径**：Finnhub `/quote` 只给最新报价，给不了历史某日 ——
    # 补跑时拿今天的 SPY 冒充目标日比缺失更坏（补跑那条路由 `_asof_history`
    # 按目标日取 yfinance 日K）。
    for name, (sym, _label) in ({} if as_of else _ETF_PROXY).items():
        q = _finnhub_quote(sym)
        if q:
            data[name] = {"last": q["last"], "prev": q["prev"],
                          "change_pct": q["change_pct"]}
            sources[name] = f"finnhub:{sym}"

    return data, sources


def _realtime_as_of() -> Optional[str]:
    """实时口径下，这批宏观数据实际代表**哪一个交易日**。

    v0.45.92：此前实时路径的 `as_of` 恒为 `None`（哨兵，表示"非补跑"），
    于是日报里的 `macro_context` 没有任何日期戳 —— 读者无法判断它是哪天的。

    ⚠️ 不能直接填"今天"。盘前跑时拿到的实时报价是**上一个交易日的收盘**
    （2026-09-01 06:49 ET 手动跑过一次，正是这个情形），写成今天就是本项目
    一路在治的 vintage 污染，比留空更坏。故：16:00 ET 收盘后才把当天算作
    as_of，否则回退到最近一个已收盘的交易日。

    复用已装好的 `cboe_options._et_now`（ZoneInfo，DST 正确）与
    `is_trading_day`（含美股假日表），不自己算时区和节假日。
    推算不出来时返回 `None` —— 诚实留空，不猜。
    """
    try:
        from datetime import timedelta
        from is_trading_day import is_trading_day
        from cboe_options import _et_now, _ET_CLOSE

        et = _et_now()
        d = et.date()
        # 当天尚未收盘（盘前/盘中），手里的报价还属于上一个交易日
        if not is_trading_day(d)[0] or et.time() < _ET_CLOSE:
            d -= timedelta(days=1)
        for _ in range(10):
            if is_trading_day(d)[0]:
                return d.isoformat()
            d -= timedelta(days=1)
    except Exception as e:  # noqa: BLE001
        _log.debug("实时 as_of 推算失败，留空：%s", e)
    return None


def _compose_data_source(as_of, src_map: Dict[str, str], data: Dict,
                         has_fred: bool) -> str:
    """如实汇总本次宏观实际用到的源。

    不再无条件写 "yfinance"：8/27 那天 yfinance 一个字段都没供上，
    标签却仍是 `yfinance+fred`，排查时会把人引向一个没被调用的源。
    """
    parts = []
    if as_of:
        parts.append("cloud_snapshot")
    # v0.45.61 二次检查：补跑分支原先**无条件**写 `cloud_snapshot+yfinance@日期`，
    # 完全不看 `src_map`。而自 v0.45.61 起补跑的国债已改走财政部 ——
    # 于是标签说 yfinance、实际是 treasury。
    # 这正是本 session 一路在修的那类标签问题（8/27 那天 yfinance 一个字段
    # 都没供上、标签却仍写 yfinance），我在同一个函数里又犯了一次。
    if any(v.startswith("treasury_gov") for v in src_map.values()):
        parts.append("treasury")
    if any(v.startswith("finnhub") for v in src_map.values()):
        parts.append("finnhub")
    if len(data) > len(src_map):        # 有字段是 yfinance 兜上来的
        parts.append("yfinance")
    if has_fred:
        parts.append("fred")
    if not parts:
        return "fallback"
    label = "+".join(parts)
    return f"{label}@{as_of}" if as_of else label


#: 宏观分（`macro_score`，只展示：Guard / Queen 不读它）的分项。v0.45.669 起只由**观测到的**分项算，
#: 结果带 `macro_score_inputs` / `macro_score_missing`；一项都没观测到 ⇒ 分数 None。前六项定 `macro_regime`
#: （一项都没有 ⇒ "unknown"），cpi / hy 在定完政体之后才加减分（沿用旧次序，未改）。
MACRO_SCORE_PARTS = ("vix", "rate", "market", "dollar", "gold", "curve", "cpi", "hy")


def _classify_vix(vix: Optional[float]) -> str:
    """VIX 绝对水平分档（没有 VIX ⇒ "unknown"）。v0.43.24 时抽成函数是因为有两条产出路径要同口径；
    v0.45.669 起只剩一条，仍留作唯一的分档处。与 cboe_vix.get_vix_regime 保持一致。"""
    if vix is None:
        return "unknown"
    if vix < 15:
        return "low"
    if vix < 20:
        return "moderate"
    if vix < 30:
        return "elevated"
    if vix < 40:
        return "high"
    return "spike"


# v0.45.357：`cboe_vix.get_vix_observation` 带出的元数据 → 宏观输出里的键名。
# 降级路径（base）也带齐这些键、值为 None —— 同 v0.45.92 的理由：「键不存在」
# 与「键在、值为 None」不可区分，就是本项目治过多次的静默降级形态。
_VIX_OBS_KEYS = {
    "vix_feed": "feed",                  # delayed_quote（当日收盘）| history_csv（CSV 最后一行）
    "vix_feed_note": "quote_reason",     # 报价没被采用的原因（before_close / quote_unavailable …）
    "vix_history_fetch": "history_fetch",  # download | cache_fresh | cache_stale（下载失败）…
    "vix_lag_sessions": "lag_sessions",  # 落后最新一场已收完的 VIX 几个交易日
    "vix_stale": "stale",                # as_of 早于扫描日的前一交易日 ⇒ GuardBee 不计这票
    "vix_quote_check": "quote_check",    # 报价收盘 vs 次日 CSV 官方收盘的核对账本摘要
}


def _vix_obs_fields(obs: Dict) -> Dict:
    return {k: (obs or {}).get(src) for k, src in _VIX_OBS_KEYS.items()}


def _vix_change_pct(vix, prev) -> Optional[float]:
    """同一对观测（当前值与**它自己的**前一收盘）的变动百分比；缺一个就是 None。"""
    if isinstance(vix, (int, float)) and isinstance(prev, (int, float)) and prev > 0:
        return round((vix / prev - 1) * 100, 2)
    return None


def _fetch_macro_data() -> Dict:
    """内部：实际拉取宏观数据"""

    # v0.45.669：只剩取数出错（下面的 `except`）这一个出口用它，且**每个观测量都是缺失**（None / "unknown"）。
    # 旧版这里是一份看起来正常的数——VIX 20.0（elevated）、10Y 4.5（恰好 high ⇒ 宏观分 −1）、美元 / 大盘 neutral、
    # 黄金 stable / 涨跌 0.0、宏观分 5.0 / neutral——只靠 `data_source == "fallback"` 告诉读者「别信」；Guard、日报、仪表板
    # 各自出过事才补上查这个标记（v0.43.24 / v0.45.54 / v0.45.43）。现在值本身就说「没有」，标记只是标签。
    # 报价全部没取到不再走这里：照常走下面那一条路径（板块轮动、FRED 照取，宏观分只算观测到的分项）。
    base = {
        "macro_regime": "unknown",
        "macro_score": None,
        "macro_score_inputs": [],
        "macro_score_missing": list(MACRO_SCORE_PARTS),
        "vix": None,
        "vix_regime": "unknown",
        "treasury_10y": None,
        "rate_environment": "unknown",
        "dollar_trend": "unknown",
        "market_trend": "unknown",
        "spx_change_pct": None,
        "yield_curve": "unknown",
        "gold_price": None,
        "gold_change_pct": None,
        "gold_trend": "unknown",
        "macro_headwinds": [],
        "macro_tailwinds": [],
        "summary": "宏观数据不可用（取数出错，见 error）",
        "data_source": "fallback",
        # v0.43.24: VIX 单独标源（CBOE 能在别的源全灭时供上）。「fallback」= 没有观测到的来源。
        "vix_source": "fallback",
        # v0.45.92：降级路径也要带这两个键，否则消费者拿到"键不存在"，
        # 与实时路径的"键在、值为 None"无法区分 —— 那正是本项目治过多次的
        # 静默降级形态。这里的值是兜底常量，不属于任何一天，故 as_of 留 None
        # 且 mode 明写 fallback，不冒充 realtime。
        "as_of": None,
        "as_of_mode": "fallback",
        # v0.45.352：VIX 的 CBOE 观测日与前一收盘（仅 CBOE 路径有值，其余 None；
        # v0.45.366 起补跑取 CSV 目标日那一行时也有值）。
        "vix_as_of": None,
        "vix_prev_close": None,
        "vix_prev_as_of": None,
        # v0.45.357：与 `vix` 同一对观测算出的变动；取不到就是 None（旧兜底 0.0 冒充「持平」）。
        # 以及 VIX 怎么拿到的、新不新鲜（`_vix_obs_fields`）。
        "vix_change_pct": None,
        **{k: None for k in _VIX_OBS_KEYS},
    }

    try:
        try:                                    # v0.45.56 限流闸门
            from yf_gate import ensure as _yf_ensure
            _yf_ensure()
        except Exception:                       # pragma: no cover - 闸门不可得不阻断
            pass

        # v0.45.669：yfinance 导入失败只让 Yahoo 那几个符号算没取到（`quote_gaps` 记 `yfinance_unavailable`），
        # 财政部 / Finnhub / CBOE / FRED 照用、板块轮动自己报整段失败——旧代码在这里整份换成兜底常量。
        try:
            import yfinance as yf
        except ImportError as _e_yf:
            yf = None
            _log.warning("宏观：yfinance 不可导入（%s），Yahoo 那几个符号记为没取到，其余来源照用", _e_yf)

        symbols = {
            "VIX":    "^VIX",
            "TNX":    "^TNX",
            "FVX":    "^FVX",      # 5Y Treasury（收益率曲线 fallback）
            "DXY":    "DX-Y.NYB",
            "SPX":    "^GSPC",
            "TLT":    "TLT",
            "GLD":    "GLD",       # SPDR 黄金 ETF（避险指标）
        }

        # v0.45.59：补跑时对齐到目标日。`_snap` 为 None 时行为与旧版完全一致。
        _snap = _MACRO_SNAPSHOT
        _as_of = _snap.get("date") if _snap else None

        # v0.45.60：**当日**口径先走非 yfinance 源（财政部 + Finnhub ETF）。
        # 补跑口径不走这条 —— 那两个源只给"最新"，给不了历史某日，
        # 拿今天的值冒充目标日比缺失更坏。补跑仍由 `_asof_history` 对齐。
        data: Dict = {}
        _src_map: Dict[str, str] = {}
        # 补跑也调 —— 只为拿目标日的国债曲线（ETF 报价那半段会自动跳过）。
        data, _src_map = _same_day_macro_data(_as_of)
        if data:
            _log.info("宏观非 yfinance 源命中 %d 项（as_of=%s）：%s",
                      len(data), _as_of or "当日",
                      ", ".join(f"{k}={v}" for k, v in sorted(_src_map.items())))

        # v0.45.663：这批宏观代表**哪一场**——补跑是 D，实时是 `_realtime_as_of()`（收盘后是今天，否则上一场）。
        # 只算一次：输出的 `as_of` 与下面按日期配对的锚定日是同一天。
        _session = _as_of or _realtime_as_of()
        _session_d = _dt.date.fromisoformat(_session) if _session else None

        # v0.45.663：按**日期**配对（`bars_integrity.session_change`），不再数第几根。
        # 旧代码末根当这一场、倒数第二根当前一场：Yahoo 缺一根就整体错位一格、不报错——08-31 补跑的大盘涨跌
        # −0.58% 就是 08-31 / 08-27（缺 08-28），按日期是 −0.33%。v0.45.654 只核了「末根是 D」，前一根照旧数位置；
        # 实时那支连末根是不是今天都不核（Yahoo 还没出今天那根 ⇒ 昨天的收盘当今天）。现在：
        #   锚定日那根缺 ⇒ 该符号当没取到；只缺前一场那根 ⇒ 水平照用（10Y 判利率环境 / 算曲线、黄金价格），涨跌为 None。
        # 每个没配上的都点名进 `quote_gaps`（两种口径都带）+ 一条 WARNING；补跑另带 654 的 `not_on_as_of`（由它派生）。
        # NaN 收盘（2026-08-28 那种：成交了、Yahoo 还没填收盘）在 `closes_by_session` 里同「没有那根」。
        _quote_gaps: Dict[str, str] = {}
        for name, sym in symbols.items():
            if name in data:          # 已由当日源供上，不再打 yfinance
                continue
            try:
                if yf is None:
                    _quote_gaps[name] = "yfinance_unavailable"
                    continue
                if _session_d is None:
                    _quote_gaps[name] = "no_session"        # 判不出这批代表哪天 ⇒ 不按位置猜
                    continue
                if _as_of:
                    hist = _asof_history(yf, sym, _as_of)
                else:
                    import yf_daily
                    # v0.45.652：经唯一入口取。末行空行 = 最近那根缺了 ⇒ 当没取到（剩下的末根是前一场）
                    hist, _last_blank = yf_daily.history(yf.Ticker(sym), source="macro_quotes",
                                                         period="5d", interval="1d")
                    if _last_blank:
                        _quote_gaps[name] = "last_row_blank"
                        continue
                if hist is None or len(hist) == 0:
                    _quote_gaps[name] = "no_data"
                    continue
                q = session_change(hist, _session_d, 1)
                if q["last"] is None:
                    _quote_gaps[name] = q["missing"]
                    continue
                if q["missing"]:
                    _quote_gaps[name] = q["missing"]        # 只缺前一场：水平照用、涨跌未知
                data[name] = {"last": q["last"], "prev": q["prev"], "change_pct": q["change_pct"]}
            except Exception as e:
                _quote_gaps[name] = f"error:{type(e).__name__}"
                _log.debug("宏观数据获取失败 %s: %s", sym, e)
        _gap_keys: Dict = {"quote_gaps": dict(sorted(_quote_gaps.items()))}
        if _as_of:
            _gap_keys["not_on_as_of"] = sorted(n for n, r in _quote_gaps.items() if r.startswith("anchor_missing:"))
        if _quote_gaps:
            _log.warning("宏观报价 %s（%s）：%d 个符号按日期没配上——缺这一场的当没取到、只缺前一场的只用水平（%s）",
                         _session, "补跑" if _as_of else "实时", len(_quote_gaps),
                         ", ".join(f"{n}={symbols[n]}:{r}" for n, r in sorted(_quote_gaps.items())))

        # ---- VIX：CBOE 优先（v0.43.24 Step 2）----
        # 原先直接用 yfinance 的 ^VIX，绕过了项目既定的 CBOE 优先链。宏观数据在
        # 30 只标的扫完之后才抓，配额已耗尽（2026-08-14 全天 363 条 429），7 个
        # 标的全灭 → 整体降级到 base 的 vix=20.0，被当作"偏高恐慌"写进报告，
        # 而当天真实 VIX 是 14.25。CBOE 的 VIX_History.csv 无 key、无限流。
        _cboe_vix = None
        _vix_src_override = ""
        _vix_as_of = _vix_prev_close = _vix_prev_as_of = None
        _vix_obs: Dict = {}
        # v0.45.59：补跑时 VIX 必须是**目标日**的 —— 实时问 CBOE 拿到的是今天的。
        #
        # v0.45.366：目标日的值改取 CBOE CSV 里 **D 那一行**（官方收盘），不再先信快照的 `vix_spot`：
        # 云端 17:05 ET 抓它时 CSV 还没追加 D 行 ⇒ 08-27/28/31 三份补跑日报与 08-26~09-11 那 12 份
        # market.json 全落后一场；且快照不记观测日，分辨不出哪天是对的。补跑是事后跑，D 行早已在 CSV 里。
        # 取到 ⇒ 标 `cboe`（与实时同一种观测：CBOE 某场的官方收盘，观测日见 `vix_as_of`，GuardBee 照常计票，
        # 用户 2026-09-28 决定）；取不到 ⇒ 退回快照值、标 `cloud_snapshot_cboe`（无观测日，Guard 不计票）。
        #
        # ⚠️ 快照模式下**绝不**落到下面的实时 `get_vix_observation()`：快照缺 `vix_term`（`load_market`
        # 剔除了兜底段）或整份 market.json 缺失时，旧代码会把**运行当天**的 VIX 贴到 D 上、标 `cboe`、
        # `vix_stale=False` ⇒ Guard 拿今天的数给过去某天投票（2026-09-28 离线复现；历史上 0 次触发）。
        if _snap:
            try:
                from cboe_vix import get_vix_observation_asof as _cboe_vix_asof
                _vix_obs = _cboe_vix_asof(_as_of) or {}
            except Exception as _e_asof:  # noqa: BLE001 - 取不到就走快照退路，但要看得见
                _log.warning("补跑 VIX：CBOE CSV 取目标日 %s 失败，退回快照值: %s", _as_of, _e_asof)
                _vix_obs = {"quote_reason": f"asof_error:{type(_e_asof).__name__}"}
            if _vix_obs.get("vix") is not None:
                _cboe_vix = _vix_obs["vix"]
                _vix_as_of = _vix_obs.get("as_of")
                _vix_prev_close = _vix_obs.get("prev_close")
                _vix_prev_as_of = _vix_obs.get("prev_as_of")
            else:
                try:
                    _vt = ((_snap.get("market") or {}).get("cboe") or {}).get("vix_term") or {}
                    _sv = _vt.get("vix_spot")
                    # 只判 `> 0` 挡不住兜底常量 15.0（`cboe_fetcher` 拿不到期货时整组落
                    # 15.0/15.75/16.5、标 `default_fallback`）。生产路径上 `load_market` 已先剔除该段，
                    # 这里再判一次，不让直接喂未过滤 market 的调用方把兜底值当观测。
                    if (_vt.get("source") != "default_fallback"
                            and isinstance(_sv, (int, float)) and 5.0 <= _sv <= 150.0):
                        _cboe_vix = float(_sv)
                        _vix_src_override = "cloud_snapshot_cboe"
                        _vix_obs["feed"] = "cloud_snapshot"
                except Exception as _e_sv:  # noqa: BLE001
                    _log.debug("快照 VIX 读取失败: %s", _e_sv)
                if _cboe_vix is None:
                    _log.warning("补跑 %s：CBOE CSV 与快照都给不出 VIX（%s）",
                                 _as_of, _vix_obs.get("quote_reason"))
        # v0.45.352：同一份 CBOE 历史里取最后两行 —— 当前值（= get_vix_spot 的口径）
        # 连同**它自己的观测日**，外加前一收盘。仪表板宏观条的 VIX 涨跌只用这对：
        #   · `vix_change_pct` 对不上 `vix`：它来自 yfinance 那条腿。CBOE 的 CSV 在
        #     17:00 ET 扫描时通常还没更新当日，于是 `vix` 是**上一交易日**收盘，而
        #     `vix_change_pct` 是当日的（09-11 实测：vix=17.84=CBOE 09-10 收盘，
        #     vix_change_pct=−11.21%=09-11 当日 15.84/17.84−1）。两者拼起来就错位一天。
        #   · 拿两份日报的 `vix` 相减也不行：没有日期，CBOE 缓存陈旧时两天会读到同一个
        #     收盘（09-24 / 09-25 都是 09-22 的 14.21），相减得一个假的「+0.0」。
        #
        # v0.45.357：改走 `cboe_vix.get_vix_observation` —— 17:00 ET 扫描时 CSV 还没有当日行，
        # 先试收盘后的延迟报价（时间戳必须证明是这一场收盘），拿不到才退回 CSV 最后一行；
        # 连同取数方式、落后几场、是否陈旧一起带出（见 `_VIX_OBS_KEYS`）。
        # v0.45.366：只在实时口径走这里（快照模式见上，绝不回落实时 VIX）。
        if _cboe_vix is None and not _snap:
            try:
                from cboe_vix import get_vix_observation as _cboe_vix_obs
                _vix_obs = _cboe_vix_obs() or {}
                if _vix_obs.get("vix") is not None:
                    _cboe_vix = _vix_obs["vix"]
                    _vix_as_of = _vix_obs.get("as_of")
                    _vix_prev_close = _vix_obs.get("prev_close")
                    _vix_prev_as_of = _vix_obs.get("prev_as_of")
            except Exception as _e_cv:  # noqa: BLE001
                _log.debug("CBOE VIX 不可用，回落 yfinance: %s", _e_cv)

        # v0.45.669：报价一项都没取到（财政部 / Finnhub / Yahoo 全空）也**不提前返回**，照常走下面同一条路径——
        # 旧代码在这里整份换成兜底常量（或只把 VIX 换成 CBOE 的真值、其余照抄兜底），于是：
        #   · 板块轮动（另一次独立的 Yahoo 取数）与 FRED 都不取 ⇒ 这些天 Guard 的板块票必然缺（经 `RegimeWeightAdjuster` ⇒ final_score）；
        #   · 宏观分恒 5.0 / neutral，连那个真 VIX 都没算进去（09-24 / 25：VIX 14.21 < 15 是 low，本该 +2 ⇒ 7.0 risk_on——那个 VIX 是 09-22 的陈旧收盘，Guard 不计票，宏观分只展示）。
        # 这一次失败原先只有仪表板打一条 WARNING（09-24 / 25 实况），在这里补上源头那条。
        _no_quotes = not data
        if _no_quotes:
            _log.warning("宏观报价 %s（%s）：财政部 / Finnhub / Yahoo 一项都没供上——VIX %s；板块轮动与 FRED 照取",
                         _session, "补跑" if _as_of else "实时",
                         f"{_cboe_vix:.2f}（{_vix_src_override or 'cboe'}）" if _cboe_vix is not None else "也没有")

        # ---- VIX 分析 ----
        if _cboe_vix is not None:
            vix = _cboe_vix
            # v0.45.59：补跑时必须区分「今天问 CBOE 拿的」与「目标日快照里的」。
            # 两者同形（都是一个 float），标成同一个 "cboe" 就无从分辨报告里的
            # VIX 究竟属于哪一天 —— 正是 MEMORY 里「读 vix 前先看 vix_source」
            # 那条要防的事。
            _vix_source = _vix_src_override or "cboe"
        elif "VIX" in data:
            vix = data["VIX"]["last"]
            _vix_source = "yfinance"
        else:
            # v0.45.669：CBOE 与 Yahoo 都没给 ⇒ 没有 VIX（旧兜底 20.0 / elevated，`vix_source=fallback` 时 Guard 本就不计票）
            vix = None
            _vix_source = "fallback"
        # v0.45.357：变动必须与 `vix` 出自**同一对**观测。旧写法恒取 yfinance 那条腿，
        # 而 `vix` 是 CBOE 的上一场收盘 ⇒ 错位一天（09-11：vix=17.84 是 09-10 收盘，
        # vix_change_pct=−11.21% 却是 09-11 当日），喂给下面的「VIX 单日飙升」逆风晚报一天。
        # 快照 VIX 没有配对的前一收盘 ⇒ None；yfinance 路径本就同出一份日 K ⇒ 照用；
        # 缺 VIX 时旧默认 0.0（冒充「持平」）改为 None。
        if _cboe_vix is not None:
            vix_change = _vix_change_pct(vix, _vix_prev_close)
        else:
            vix_change = data["VIX"].get("change_pct") if "VIX" in data else None

        vix_regime = _classify_vix(vix)

        # v0.45.663：下面四项缺了就是缺（None / "unknown"，不加不减分），不再用看起来正常的数顶上——
        # 旧缺 10Y 填 4.5（恰好落进 high ⇒ 宏观分 −1、报告印「10Y 4.50%」），缺涨跌填 0.0（报告印「持平」）。
        # 2026-09 的静默降级审计确认过这条（`experiments/silent_degradation_audit_verified.json`，fred_macro / report_formatters 两项）。
        # Guard 只读 `gold_trend`：「unknown」与「stable」一样不投票。

        # ---- 10Y 利率分析 ----
        tnx = data.get("TNX", {}).get("last")
        tnx_change = data.get("TNX", {}).get("change_pct")

        if tnx is None:
            rate_env = "unknown"
        elif tnx < 3.0:
            rate_env = "low"
        elif tnx < 4.5:
            rate_env = "moderate"
        else:
            rate_env = "high"

        # ---- 美元趋势 ----
        dxy_change = data.get("DXY", {}).get("change_pct")
        if dxy_change is None:
            dollar_trend = "unknown"
        elif dxy_change > 0.3:
            dollar_trend = "strong"
        elif dxy_change < -0.3:
            dollar_trend = "weak"
        else:
            dollar_trend = "neutral"

        # ---- 大盘趋势（较前一交易日；旧注释与报告标签写「5日」，从来都是 1 日）----
        spx_change = data.get("SPX", {}).get("change_pct")
        if spx_change is None:
            market_trend = "unknown"
        elif spx_change > 0.5:
            market_trend = "bull"
        elif spx_change < -0.5:
            market_trend = "bear"
        else:
            market_trend = "neutral"

        # ---- 黄金趋势（避险指标）----
        gold_price = data.get("GLD", {}).get("last")
        gold_change = data.get("GLD", {}).get("change_pct")
        if gold_change is None:
            gold_trend = "unknown"
        elif gold_change > 1.0:
            gold_trend = "surging"    # 避险需求强烈
        elif gold_change > 0.3:
            gold_trend = "rising"
        elif gold_change < -1.0:
            gold_trend = "falling"
        else:
            gold_trend = "stable"

        # ---- 收益率曲线（2Y-10Y 利差）----
        # 优先 FRED 2Y（精确），fallback 用 5Y 推算
        treasury_2y = None
        yield_spread = None
        yield_curve = "unknown"
        _2y_source = ""
        # v0.45.654：利差要一个**观测到的** 10Y。缺 TNX 时上面的 `tnx` 是兜底常量 4.5，拿它减真 2Y 会造出假倒挂 / 假趋平
        # （10-12 补跑实况 10Y 5.24、2Y 4.75 ⇒ +49bp normal；换成 4.5 是 −25bp inverted ⇒ Guard risk_off +2）。
        # 135 份日报里只在整份降级（fallback，Guard 本就不看曲线）时缺过 TNX；补跑「末根不是 D ⇒ 当没取到」让它够得着了。
        _tnx_seen = "TNX" in data

        def _set_curve(y2: float, src: str):
            """同一段判定逻辑此前抄了三遍（近似 / FRED / 本次新增财政部）。
            抄三遍就有三处会漂移 —— 收敛成一处。"""
            nonlocal treasury_2y, yield_spread, yield_curve, _2y_source
            treasury_2y = round(y2, 3)
            _2y_source = src
            if not _tnx_seen:                               # 2Y 照记；利差 / 曲线留 None / unknown
                return
            yield_spread = round((tnx - y2) * 100, 1)     # bp
            yield_curve = ("inverted" if yield_spread < -10
                           else "flat" if yield_spread < 20 else "normal")

        # v0.45.60：财政部日度曲线直接给真 2Y —— 优先于下面的 5Y 近似。
        #
        # 那个近似（`5Y + 0.15`）是拿不到 2Y 时代的产物，而它会**扭曲曲线判定**：
        # 2026-08-27 实测 5Y=4.38 → 近似 2Y=4.53 → 10Y−2Y=+14bp 判成 "flat"；
        # 真 2Y=4.20 → 利差 +47bp，实际是 "normal"。差了一个档位。
        _tsy_2y = data.get("TWO", {}).get("last")
        # v0.45.663：缺 10Y 时 `tnx` 是 None（旧为 4.5、恒过 `> 0`）——2Y 照记、利差留空（`_set_curve` 内判），行为不变
        _tnx_ok = tnx is None or tnx > 0
        if _tsy_2y is not None and _tnx_ok:
            _set_curve(_tsy_2y, "treasury_gov")
        else:
            # 退而求其次：5Y 近似。保留是因为财政部不可得时它总比 unknown 强，
            # 但必须标出来，否则读者无从知道这个 2Y 是推算的。
            fvx = data.get("FVX", {}).get("last")
            if fvx is not None and _tnx_ok:
                _set_curve(fvx + 0.15, "approx_from_5y")

        # ---- FRED API（提前获取以修正 2Y 曲线数据）----
        fred_data = {}
        fred_key = _load_fred_key()
        if fred_key:
            # v0.45.657：补跑按 D 的时点取（见 `_fetch_fred_series`）；旧代码补跑也取运行当天的最新值
            fred_data = _fetch_fred_series(fred_key, vintage=_as_of)
            # FRED 精确 2Y 覆盖 5Y 近似 —— 但**不覆盖财政部**：
            # FRED 转发的就是财政部的数，且晚一天（实测 08-26 vs 08-27）。
            # 用它盖掉更新的同源数据是纯粹的倒退。
            if (fred_data.get("treasury_2y") is not None
                    and _2y_source != "treasury_gov"):
                _set_curve(fred_data["treasury_2y"], "fred")

        # ---- 板块轮动 ----
        # v0.45.366：补跑对齐目标日（旧：`period="5d"` 永远取最近 5 天，GuardBee 的板块票是运行当天的）
        sector_rotation = _fetch_sector_rotation(yf, as_of=_as_of)

        # ---- 宏观综合评分（0-10）----
        # 越多顺风 → 分越高
        score = 5.0

        # VIX 贡献
        if vix_regime == "low":
            score += 2.0
        elif vix_regime == "moderate":
            score += 1.0
        elif vix_regime == "high":
            score -= 1.5
        elif vix_regime == "spike":
            score -= 3.0

        # 利率贡献（高利率压制成长股）
        if rate_env == "low":
            score += 1.0
        elif rate_env == "high":
            score -= 1.0

        # 大盘贡献
        if market_trend == "bull":
            score += 1.0
        elif market_trend == "bear":
            score -= 1.5

        # 美元贡献（强美元压制非美收益）
        if dollar_trend == "strong":
            score -= 0.5
        elif dollar_trend == "weak":
            score += 0.5

        # 黄金贡献（上涨=避险=股市逆风）
        if gold_trend == "surging":
            score -= 0.8
        elif gold_trend == "rising":
            score -= 0.3
        elif gold_trend == "falling":
            score += 0.3

        # 收益率曲线贡献
        if yield_curve == "inverted":
            score -= 1.5
        elif yield_curve == "flat":
            score -= 0.5

        score = max(1.0, min(10.0, score))

        # v0.45.669：哪些分项是**观测到的**。没观测到的分项上面本就不加不减（"unknown" 不进任何一档）——
        # 但一项都没有时 5.0 / neutral 是凭空的「中性」（旧整份兜底恒为它），所以改成 None / "unknown"。
        _score_inputs = [p for p, state in (("vix", vix_regime), ("rate", rate_env), ("market", market_trend),
                                            ("dollar", dollar_trend), ("gold", gold_trend), ("curve", yield_curve))
                         if state != "unknown"]

        # ---- 宏观政体判断 ----
        if not _score_inputs:
            macro_regime = "unknown"
        elif score >= 7:
            macro_regime = "risk_on"
        elif score <= 4:
            macro_regime = "risk_off"
        else:
            macro_regime = "neutral"

        # ---- 顺逆风汇总 ----
        headwinds = []
        tailwinds = []

        if vix_regime in ("high", "spike"):
            headwinds.append(f"VIX {vix:.1f}（市场恐慌{'' if vix_regime=='high' else '极度'}升温）")
        if vix_change is not None and vix_change > 10:
            _vspan = f"{_vix_prev_as_of}→{_vix_as_of}，" if _vix_as_of and _vix_prev_as_of else ""
            headwinds.append(f"VIX 单日飙升 {vix_change:+.1f}%（{_vspan}波动率冲击）")
        if rate_env == "high" and tnx_change is not None and tnx_change > 2:
            headwinds.append(f"10Y 利率 {tnx:.2f}% 持续走高（压制成长估值）")
        if dollar_trend == "strong":
            headwinds.append(f"美元走强（新兴市场 + 大宗商品承压）")
        if market_trend == "bear":
            headwinds.append(f"大盘下行（{spx_change:+.1f}%，贝塔风险放大）")

        if yield_curve == "inverted":
            headwinds.append(f"收益率曲线倒挂（2Y-10Y利差{yield_spread:+.0f}bp，衰退信号）")
        elif yield_curve == "flat":
            headwinds.append(f"收益率曲线趋平（2Y-10Y利差{yield_spread:+.0f}bp）")

        if vix_regime in ("low", "moderate"):
            tailwinds.append(f"VIX {vix:.1f}（低波动，风险偏好良好）")
        if market_trend == "bull":
            tailwinds.append(f"大盘上行（{spx_change:+.1f}%，贝塔顺风）")
        if dollar_trend == "weak":
            tailwinds.append("美元偏弱（出口商 + 大宗商品受益）")
        if rate_env == "low":
            tailwinds.append(f"低利率环境（成长股估值友好）")

        if gold_trend == "surging":
            headwinds.append(f"黄金飙升{gold_change:+.1f}%（避险需求强烈，资金撤离风险资产）")
        elif gold_trend == "rising":
            headwinds.append(f"黄金走强{gold_change:+.1f}%（避险情绪升温）")
        elif gold_trend == "falling":
            tailwinds.append(f"黄金回落{gold_change:+.1f}%（风险偏好回升）")

        # ---- FRED CPI/FFR 补充评分（FRED 数据已在 score 之前获取）----
        if fred_data:
            if fred_data.get("cpi_yoy") is not None:
                _score_inputs.append("cpi")
                cpi = fred_data["cpi_yoy"]
                if cpi > 4.0:
                    headwinds.append(f"CPI同比 {cpi:.1f}%（通胀偏高，加息压力仍存）")
                    score -= 0.5
                elif cpi > 2.5:
                    headwinds.append(f"CPI同比 {cpi:.1f}%（通胀略偏高）")
                else:
                    tailwinds.append(f"CPI同比 {cpi:.1f}%（通胀受控，货币政策友好）")
                    score += 0.3
                score = max(1.0, min(10.0, score))
            if fred_data.get("fed_funds_rate") is not None:
                ffr = fred_data["fed_funds_rate"]
                if ffr >= 5.0:
                    headwinds.append(f"联邦基金利率 {ffr:.2f}%（高利率压制成长估值）")
                elif ffr <= 2.0:
                    tailwinds.append(f"联邦基金利率 {ffr:.2f}%（宽松环境）")

            # ---- HY 信用利差评分 ----
            if fred_data.get("hy_spread_bp") is not None:
                _score_inputs.append("hy")
                hy_bp = fred_data["hy_spread_bp"]
                hy_chg = fred_data.get("hy_spread_chg_bp")
                chg_str = f"({hy_chg:+.0f}bp日变)" if hy_chg is not None else ""
                if hy_bp >= 600:
                    headwinds.append(
                        f"HY信用利差 {hy_bp:.0f}bp{chg_str}（系统性风险信号，信贷市场承压）")
                    score -= 2.0
                elif hy_bp >= 400:
                    headwinds.append(
                        f"HY信用利差 {hy_bp:.0f}bp{chg_str}（信用风险偏高，风险资产受压）")
                    score -= 1.0
                elif hy_bp >= 300:
                    headwinds.append(
                        f"HY信用利差 {hy_bp:.0f}bp{chg_str}（信用利差偏宽，注意边际走阔）")
                    score -= 0.3
                else:
                    tailwinds.append(
                        f"HY信用利差 {hy_bp:.0f}bp{chg_str}（信用市场平稳，风险偏好良好）")
                    score += 0.3
                score = max(1.0, min(10.0, score))

        summary_parts = [
            "宏观报价全部没取到",
        ] if _no_quotes else []
        summary_parts += [
            f"VIX {vix:.1f}({vix_regime})" if vix is not None else "VIX —",
            f"10Y {tnx:.2f}%" if tnx is not None else "10Y —",
            f"大盘{spx_change:+.1f}%" if spx_change is not None else "大盘—",
            f"宏观:{macro_regime}",
        ]
        if yield_curve != "unknown":
            yc_label = {"normal": "正常", "flat": "趋平", "inverted": "倒挂"}
            summary_parts.append(f"曲线:{yc_label.get(yield_curve, yield_curve)}")
        if fred_data.get("cpi_yoy") is not None:
            summary_parts.append(f"CPI同比{fred_data['cpi_yoy']:.1f}%")
        if fred_data.get("hy_spread_bp") is not None:
            summary_parts.append(f"HY利差{fred_data['hy_spread_bp']:.0f}bp")
        if gold_trend in ("surging", "rising", "falling"):
            _gl = {"surging": "飙升", "rising": "走强", "falling": "回落"}
            summary_parts.append(f"黄金{_gl[gold_trend]}{gold_change:+.1f}%")

        return {
            "macro_regime": macro_regime,
            "macro_score": round(score, 1) if _score_inputs else None,
            "macro_score_inputs": [p for p in MACRO_SCORE_PARTS if p in _score_inputs],
            "macro_score_missing": [p for p in MACRO_SCORE_PARTS if p not in _score_inputs],
            "vix": round(vix, 2) if vix is not None else None,
            "vix_change_pct": round(vix_change, 2) if vix_change is not None else None,
            "vix_regime": vix_regime,
            "treasury_10y": round(tnx, 3) if tnx is not None else None,
            "treasury_2y": treasury_2y,
            "yield_spread": yield_spread,
            "yield_curve": yield_curve,
            "rate_environment": rate_env,
            "dollar_trend": dollar_trend,
            "market_trend": market_trend,
            "spx_change_pct": round(spx_change, 2) if spx_change is not None else None,
            "gold_price": round(gold_price, 2) if gold_price else None,
            "gold_change_pct": round(gold_change, 2) if gold_change is not None else None,
            "gold_trend": gold_trend,
            "sector_rotation": sector_rotation,
            "macro_headwinds": headwinds,
            "macro_tailwinds": tailwinds,
            "fred_extras": fred_data,
            "cpi_yoy": fred_data.get("cpi_yoy"),
            "unemployment": fred_data.get("unemployment"),
            "fed_funds_rate": fred_data.get("fed_funds_rate"),
            "hy_spread_bp": fred_data.get("hy_spread_bp"),
            "hy_spread_chg_bp": fred_data.get("hy_spread_chg_bp"),
            "summary": " | ".join(summary_parts),
            # v0.45.59：`as_of` 生效时如实写明这是哪一天的口径。
            # 「yfinance+fred」用在补跑上会让读者以为是运行当天的实时数据。
            #
            # v0.45.60：当日口径下 `data_source` 必须反映**实际**供数的源。
            # 财政部与 Finnhub 供上后仍写「yfinance」是假标签 —— 排查限流时
            # 会让人去查一个根本没被调用的源。
            # v0.45.669：报价一项都没有 ⇒ 仍标 "fallback"（与旧整份降级同一个标签，只是不再承担「别信这些值」——值本身已是缺失）。
            # 不写成 `cloud_snapshot@D` / `fred`：Guard 据它不记曲线 / 黄金（这些天两者必为 unknown，票不变），
            # `ic_rerun_readiness` 旧边界的判据也按它认整份降级；FRED 供没供上看 `fred_extras`。
            "data_source": ("fallback" if _no_quotes
                            else _compose_data_source(_as_of, _src_map, data, bool(fred_data))),
            "field_sources": dict(_src_map),   # 逐字段来源，空 = 该项走了 yfinance
            "treasury_2y_source": _2y_source,  # treasury_gov / fred / approx_from_5y
            "vix_source": _vix_source,
            # v0.45.352：只在 CBOE 路径有值（快照退路 / yfinance / 兜底都是 None）。
            # v0.45.366：补跑取 CSV 目标日那一行（`vix_feed=history_csv_asof`）也算 CBOE 路径。
            "vix_as_of": _vix_as_of,
            "vix_prev_close": _vix_prev_close,
            "vix_prev_as_of": _vix_prev_as_of,
            # v0.45.357：取数方式 / 新鲜度 / 报价核对（同上；补跑时 `vix_quote_check` 恒 None）
            **_vix_obs_fields(_vix_obs),
            # v0.45.92：两条路径都给日期戳。
            # `_as_of` 本身**不能动** —— 它是控制流：非 None 会让上面走
            # `_asof_history` 对齐历史日，实时路径填了它会改变取数行为。
            # 所以只改输出，并用 `as_of_mode` 显式承载原先靠 None 表达的区分。
            "as_of": _session,               # v0.45.663：与按日期配对的锚定日同一次求值
            "as_of_mode": "backfill" if _as_of else "realtime",
            **_gap_keys,
        }

    except Exception as e:
        # v0.45.669：只剩这一个整份出口（取数代码自己出错）；值全是缺失，并写明是什么错。
        # yfinance 导入失败不再走这里（见上：只让 Yahoo 那几个符号算没取到）。
        _log.warning("宏观数据获取失败（整份为缺失）: %s: %s", type(e).__name__, e)
        return {**base, "error": f"{type(e).__name__}: {e}"}


#: `_fetch_fred_series` 产出的观测键（补跑时缺哪个就点名哪个）
_FRED_FIELDS = ("cpi_yoy", "unemployment", "fed_funds_rate", "treasury_2y", "hy_spread_bp")


def _fred_value(o: Dict) -> float:
    """一条 FRED 观测的数值。`"."` 是 FRED 的缺值（假日、停摆没发布的月份）⇒ 抛 ValueError 并写明是哪天。"""
    v = o["value"]
    if v == ".":
        raise ValueError(f"{o['date']} 的值是 \".\"（FRED 缺值）")
    return float(v)


def _parse_cpi(obs: list) -> Dict:
    """CPI 同比：最新一个月 ÷ 一年前**同一月份**（按日期配对，不按位置）。

    v0.45.660：2025-10 因美国政府停摆没有发布（FRED 那一行的值是 "."），2026-10 那期（11-10 发布）起的一个月里
    一年前同月正是它 ⇒ 算不出同比 ⇒ 抛出、本项缺失（不拿相邻月份顶替）。"""
    if not obs:
        return {}
    d = obs[0]["date"]
    year_ago = f"{int(d[:4]) - 1}{d[4:]}"
    prev = next((o for o in obs if o.get("date") == year_ago), None)
    if prev is None:
        raise ValueError(f"CPI {d} 的一年前同月 {year_ago} 不在返回的观测里")
    return {"cpi_yoy": round((_fred_value(obs[0]) / _fred_value(prev) - 1) * 100, 2), "cpi_date": d}


def _parse_latest(value_key: str, date_key: str):
    def _parse(obs: list) -> Dict:
        return {value_key: _fred_value(obs[0]), date_key: obs[0]["date"]} if obs else {}
    return _parse


def _parse_dgs2(obs: list) -> Dict:
    """债市假日那一行是 "."：照旧不取（不报警，那是常态）。"""
    if not obs or obs[0].get("value", ".") == ".":
        return {}
    return {"treasury_2y": _fred_value(obs[0]), "treasury_2y_date": obs[0]["date"]}


def _parse_hy(obs: list) -> Dict:
    """ICE BofA US HY OAS，FRED 给百分点（3.50）⇒ ×100 成 bp；带日环比。假日 "." 照旧不取。"""
    if not obs or obs[0].get("value", ".") == ".":
        return {}
    out = {"hy_spread_bp": round(_fred_value(obs[0]) * 100, 0), "hy_spread_date": obs[0]["date"]}
    if len(obs) >= 2 and obs[1].get("value", ".") != ".":
        out["hy_spread_chg_bp"] = round(out["hy_spread_bp"] - _fred_value(obs[1]) * 100, 1)
    return out


#: (序列, 取几条, 解析) —— 逐个解析、互不连带（v0.45.660）
_FRED_PARSERS = (
    ("CPIAUCSL", 13, _parse_cpi),
    ("UNRATE", 1, _parse_latest("unemployment", "unemployment_date")),
    ("DFF", 1, _parse_latest("fed_funds_rate", "fed_funds_rate_date")),
    ("DGS2", 1, _parse_dgs2),
    ("BAMLH0A0HYM2", 2, _parse_hy),
)


def _fetch_fred_series(api_key: str, vintage: Optional[str] = None) -> Dict:
    """从 FRED API 获取 CPI（同比）、失业率等月度数据（使用 requests 解决 macOS SSL 问题）

    已集成弹性层：CircuitBreaker 熔断 + get_session 连接池复用。

    v0.45.657：`vintage=D`（补跑）⇒ 每条请求带 `realtime_start=realtime_end=D`，取 FRED **在 D 那天的样子**
    （ALFRED 时点数据）。旧代码补跑也取「运行当天的最新值」：补跑晚几周跑，2Y 就是几周后的（Treasury 缺 D 行时
    经曲线进 Guard 宏观票），CPI 还会拿到 D 之后才发布的月份与修订值。不用 `observation_end=D`：CPI 的观测日是
    月初、发布在次月中，按观测日截照样看得到未来。时点口径下 2Y / 联邦基金利率是 D 当天已发布的最新一场
    （通常 D−1，与 D 那天实时扫描从 FRED 拿到的相同）；HY 利差可能已含 D 当晚发布的 D 值（≤ D，只进展示）。
    时点请求失败 ⇒ 该项没取到、WARNING 点名，**不回落最新值**；结果带 `vintage` 键（全缺时不带，免得空结果被当成「有 FRED」）。
    """
    # 熔断检查
    if _fred_breaker and not _fred_breaker.allow_request():
        _log.warning("FRED API 熔断中，跳过请求")
        return {}

    result = {}
    _pit = {"realtime_start": vintage, "realtime_end": vintage} if vintage else {}
    try:
        import requests as _req
        _session = get_session("fred") if _RESILIENCE_OK else _req
        base = "https://api.stlouisfed.org/fred/series/observations"

        # v0.45.660：逐个序列解析、互不连带。旧代码五段解析写在同一个 try 里，任何一个值是 "."（FRED 的缺值）
        # 都抛 ValueError、被外层 except 以 debug 吞掉，排在它后面的序列（含进曲线的 2Y）一起丢、熔断器还记一次失败。
        # 取数失败（网络 / 超时）照旧整段中止并记熔断——那是 FRED 不通，逐个重试只会把超时乘五。
        for series_id, limit, parse in _FRED_PARSERS:
            try:
                r = _session.get(base, params={
                    "series_id": series_id, "api_key": api_key,
                    "file_type": "json", "sort_order": "desc", "limit": str(limit), **_pit
                }, timeout=8)
                if not r.ok:
                    continue
                result.update(parse(r.json().get("observations", [])))
            except _req.RequestException:
                # 取数失败照旧整段中止、记熔断。requests 里有几种同时继承 ValueError（代理 / URL 配置错、FRED 回了非 JSON 的页面），
                # 不先放走会被下面当成「解析失败」逐个吞掉，熔断器永远不跳（v0.45.660 二次检查，独立审查揪出）
                raise
            except (ValueError, KeyError, TypeError, ZeroDivisionError) as e:
                _log.warning("FRED %s 解析失败，本项按没取到处理（其余序列照常）：%s", series_id, e)

        # 至少有一个成功响应 → 记录成功
        if _fred_breaker and result:
            _fred_breaker.record_success()

    except Exception as e:
        _log.debug("FRED API 调用失败: %s", e)
        if _fred_breaker:
            _fred_breaker.record_failure()
    if vintage:
        _missing = [k for k in _FRED_FIELDS if result.get(k) is None]
        if _missing:
            _log.warning("补跑 FRED 时点数据 %s：%d 项没取到，按没取到处理、不回落最新值（%s）",
                         vintage, len(_missing), ", ".join(_missing))
        if result:
            result["vintage"] = vintage
    return result


_SECTOR_ETFS = {
    "XLK": "科技", "XLV": "医疗", "XLE": "能源",
    "XLF": "金融", "XLI": "工业", "XLY": "可选消费",
    "XLP": "必需消费", "XLU": "公用事业", "XLRE": "房地产",
    "XLC": "通信", "XLB": "材料",
}

# 板块 ETF → config.WATCHLIST sector 映射
_SECTOR_TO_ETF = {
    "Technology": "XLK", "Healthcare": "XLV", "Energy": "XLE",
    "Financial": "XLF", "Industrial": "XLI", "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP", "Utilities": "XLU", "Real Estate": "XLRE",
    "Communication": "XLC", "Materials": "XLB",
    # 别名映射
    "Automotive": "XLY", "Biotech": "XLV", "Fintech": "XLK",
    "Semiconductor": "XLK", "E-Commerce": "XLY",
}


#: 板块轮动「5 日涨跌」= 这一场对往前第 4 个交易日（5 根日线首尾，与旧 `period="5d"` 的 `iloc[0]` 同一定义）
SECTOR_ROTATION_SESSIONS_BACK = 4


def _fetch_sector_rotation(yf_module=None, as_of: Optional[str] = None) -> Dict:
    """获取 11 个 SPDR 板块 ETF 的 5 日表现，返回板块轮动数据（并行 + 单 ETF 缓存）

    v0.45.366：`as_of`（补跑目标日）给定时对齐到该日 —— 此前恒 `period="5d"` 取**最近** 5 天，
    补跑时 GuardBee 的板块顺逆风票是运行当天的（同 v0.45.59 治过的国债/SPX 那类错位）。
    补跑不读也不写单 ETF 缓存（读了会把实时值混进补跑，写了会把 D 的值混进之后的实时）。

    v0.45.663：涨跌按**日期**配对（`bars_integrity.session_change`：这一场对往前第
    `SECTOR_ROTATION_SESSIONS_BACK` 个交易日），不再数第几根（旧：实时 `iloc[0]`、补跑 `iloc[-min(5, len)]`，
    Yahoo 缺一根就错位一格、不报错）。这一场 = 补跑的 D / 实时的 `_realtime_as_of()`（Yahoo 还没出今天那根 ⇒
    这只不计，不再拿昨天的窗口冒充今天）。没算进去的每一只都点名进 `gaps`（原因）+ 一条 WARNING——此前取数失败只打
    debug，10-14 / 10-26 整张表为空、10-05 少两只，日志里一个字都没有。±50% 以上不再「归零」照排（0.0 冒充持平），
    改为不计、点名 `implausible`。结果带 `as_of`（这批代表的那一场）与 `gaps` 两键（整段失败 ⇒ `gaps` 只有 `"*"` 一项）；
    补跑另带 `not_on_as_of`（由 `gaps` 派生）。
    实时缓存按（ETF, 那一场）存，跨过收盘不会把上一场的值当这一场用。
    """
    result = {"hot": [], "cold": [], "full": {}}
    _d = None
    if as_of:
        _d = _dt.date.fromisoformat(as_of)
    session = as_of or _realtime_as_of()
    session_d = _dt.date.fromisoformat(session) if session else None
    result["as_of"] = session
    gaps: Dict[str, str] = {}
    try:
        yf = yf_module
        if yf is None:
            import yfinance as yf
        tickers = list(_SECTOR_ETFS.keys())
        now = time.time()

        # 分离：已缓存 vs 需拉取（补跑口径不读缓存）
        to_fetch = []
        performances = []
        with _etf_lock:
            for etf in tickers:
                cached = None if _d else _etf_cache.get((etf, session))
                if cached and (now - cached[0]) < _CACHE_TTL:
                    name, chg = cached[1]
                    performances.append((etf, name, chg))
                    result["full"][etf] = (name, chg)
                else:
                    to_fetch.append(etf)

        # 并行拉取缺失 ETF → (etf, (etf, name, chg) 或 None, 没算进去的原因)
        def _fetch_one(etf):
            try:
                if session_d is None:
                    return etf, None, "no_session"          # 判不出这批代表哪天 ⇒ 不按位置猜
                if _d is not None:
                    hist = _asof_history(yf, etf, as_of)
                else:
                    import yf_daily
                    # v0.45.652：经唯一入口取。首行空行（窗口外边界日的分红）此前让 iloc[0] 成 NaN、整只 ETF 被丢——
                    # 11 只 SPDR 同一天除息，一漏就是整张轮动表空；末行空行 = 最近那根缺了 ⇒ 这只不计（同此前 NaN 的结局）
                    hist, _last_blank = yf_daily.history(yf.Ticker(etf), source="sector_rotation",
                                                         period="5d", interval="1d")
                    if _last_blank:
                        return etf, None, "last_row_blank"
                if hist is None or len(hist) == 0:
                    return etf, None, "no_data"
                q = session_change(hist, session_d, SECTOR_ROTATION_SESSIONS_BACK)
                if q["missing"]:
                    return etf, None, q["missing"]
                # < 5 防 yfinance sample data ~1.0 哨兵值（ETF 真实价格均 > $5）
                if q["prev"] < 5:
                    return etf, None, f"base_below_5:{q['prev']}"
                chg = round(q["change_pct"], 2)
                # 5 日涨跌 ±50% 以上为数据异常：不计、点名（旧「归零保守处理」= 拿 0.0 冒充持平照样排进冷热）
                if abs(chg) > 50:
                    return etf, None, f"implausible:{chg}"
                return etf, (etf, _SECTOR_ETFS[etf], chg), None
            except Exception as e:
                _log.debug("Sector ETF %s fetch failed: %s", etf, e)
                return etf, None, f"error:{type(e).__name__}"

        if to_fetch:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="etf") as pool:
                futures = [pool.submit(_fetch_one, etf) for etf in to_fetch]
                for fut in as_completed(futures):
                    etf, fetched, why = fut.result()
                    if fetched is None:
                        gaps[etf] = why
                        continue
                    _etf, name, chg = fetched
                    performances.append(fetched)
                    result["full"][etf] = (name, chg)
                    if _d is None:          # 补跑口径不写缓存（见 docstring）
                        with _etf_lock:
                            _etf_cache[(etf, session)] = (time.time(), (name, chg))

        if performances:
            performances.sort(key=lambda x: x[2], reverse=True)
            if len(performances) >= 6:
                result["hot"] = performances[:3]
                result["cold"] = performances[-3:]
            elif len(performances) >= 2:
                mid = len(performances) // 2
                result["hot"] = performances[:mid]
                result["cold"] = performances[mid:]
            else:
                result["hot"] = performances[:1]
                result["cold"] = []
    except Exception as e:
        _log.warning("板块轮动 %s：整段取数失败，整张表为空（%s: %s）", session, type(e).__name__, e)
        gaps["*"] = f"error:{type(e).__name__}"
    result["gaps"] = dict(sorted(gaps.items()))
    if _d is not None:
        result["not_on_as_of"] = sorted(e for e, r in gaps.items() if r.startswith("anchor_missing:"))
    if gaps and "*" not in gaps:
        _log.warning("板块轮动 %s（%s）：%d 只 ETF 没算进去（%s）", session, "补跑" if _d else "实时", len(gaps),
                     ", ".join(f"{e}:{r}" for e, r in sorted(gaps.items())))
    return result


def get_sector_etf_for_ticker(ticker: str) -> str:
    """根据 ticker 的板块返回对应的板块 ETF 代码"""
    try:
        from config import WATCHLIST
        sector = WATCHLIST.get(ticker, {}).get("sector", "")
        return _SECTOR_TO_ETF.get(sector, "")
    except (ImportError, KeyError):
        return ""


def get_macro_risk_adjustment(macro: Dict) -> Tuple:
    """
    将宏观数据转换为 GuardBeeSentinel 可用的风险调整因子

    Returns:
        (adjustment: float, description: str)
        adjustment: -2.0 ~ +1.5（对 guard score 的加减分）
    """
    regime = macro.get("macro_regime", "neutral")
    vix_regime = macro.get("vix_regime", "elevated")
    score = macro.get("macro_score", 5.0)

    if regime == "risk_off":
        if vix_regime == "spike":
            return (-2.0, f"宏观极度风险厌恶（VIX={macro.get('vix', 0):.0f}，全面减仓环境）")
        return (-1.2, f"宏观风险偏好走弱（{macro.get('summary', '')}）")
    elif regime == "risk_on":
        return (+1.0, f"宏观顺风环境（{macro.get('summary', '')}）")
    else:
        if vix_regime in ("high", "spike"):
            return (-0.5, f"宏观中性但波动偏高（VIX={macro.get('vix', 0):.0f}）")
        return (0.0, f"宏观中性（{macro.get('summary', '')}）")
