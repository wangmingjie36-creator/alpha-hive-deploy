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

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Tuple

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
_etf_cache: Dict[str, tuple] = {}   # etf -> (timestamp, (name, chg))
_etf_lock = threading.Lock()


def get_macro_context() -> Dict:
    """
    获取当前宏观经济环境快照

    Returns:
        {
            "macro_regime": "risk_on"/"risk_off"/"neutral",
            "macro_score": float (0-10, 10=极度风险偏好),
            "vix": float,
            "vix_regime": "low"/"elevated"/"high"/"spike",
            "treasury_10y": float,
            "rate_environment": "low"/"rising"/"high",
            "dollar_trend": "strong"/"weak"/"neutral",
            "market_trend": "bull"/"bear"/"neutral",
            "macro_headwinds": list[str],
            "macro_tailwinds": list[str],
            "summary": str,
            "data_source": str,
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
    """
    import datetime as _dt
    d = _dt.date.fromisoformat(as_of)
    hist = yf.Ticker(sym).history(start=(d - _dt.timedelta(days=12)).isoformat(),
                                  end=(d + _dt.timedelta(days=1)).isoformat(),
                                  interval="1d")
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
    return {
        "last": last,
        "prev": prev if prev > 0 else last,
        "change_pct": ((last / prev) - 1) * 100 if prev > 0 else 0.0,
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
                    # 口径注意：财政部给的是 par yield 本身，**不是** ^TNX 的 ×10
                    data[name] = {"last": v, "prev": v, "change_pct": 0.0}
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


def _classify_vix(vix: float) -> str:
    """VIX 绝对水平分档。抽成函数是因为 v0.43.24 后有两条产出路径
    （正常路径 / yfinance 全灭但 CBOE 可用的部分降级路径），
    两边必须同口径，否则同一个 VIX 会得到不同 regime 标签。
    与 cboe_vix.get_vix_regime 保持一致。"""
    if vix < 15:
        return "low"
    if vix < 20:
        return "moderate"
    if vix < 30:
        return "elevated"
    if vix < 40:
        return "high"
    return "spike"


def _fetch_macro_data() -> Dict:
    """内部：实际拉取宏观数据"""

    base = {
        "macro_regime": "neutral",
        "macro_score": 5.0,
        "vix": 20.0,
        "vix_regime": "elevated",
        "treasury_10y": 4.5,
        "rate_environment": "high",
        "dollar_trend": "neutral",
        "market_trend": "neutral",
        "gold_price": None,
        "gold_change_pct": 0.0,
        "gold_trend": "stable",
        "macro_headwinds": [],
        "macro_tailwinds": [],
        "summary": "宏观数据不可用（降级到默认值）",
        "data_source": "fallback",
        # v0.43.24: VIX 单独标源。它可以在 yfinance 全灭时仍由 CBOE 供上，
        # 此时 data_source 仍是 fallback（其余字段确实降级了），但 VIX 是真的。
        "vix_source": "fallback",
    }

    try:
        try:                                    # v0.45.56 限流闸门
            from yf_gate import ensure as _yf_ensure
            _yf_ensure()
        except Exception:                       # pragma: no cover - 闸门不可得不阻断
            pass

        import yfinance as yf

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

        for name, sym in symbols.items():
            if name in data:          # 已由当日源供上，不再打 yfinance
                continue
            try:
                if _as_of:
                    hist = _asof_history(yf, sym, _as_of)
                    if hist is None:
                        continue
                else:
                    t = yf.Ticker(sym)
                    hist = t.history(period="5d", interval="1d")
                if hist is not None and not hist.empty:
                    data[name] = {
                        "last": float(hist["Close"].iloc[-1]),
                        "prev": float(hist["Close"].iloc[-2]) if len(hist) >= 2 else float(hist["Close"].iloc[-1]),
                        "change_pct": 0.0,
                    }
                    if data[name]["prev"] != 0:
                        data[name]["change_pct"] = (data[name]["last"] / data[name]["prev"] - 1) * 100
            except Exception as e:
                _log.debug("宏观数据获取失败 %s: %s", sym, e)

        # ---- VIX：CBOE 优先（v0.43.24 Step 2）----
        # 原先直接用 yfinance 的 ^VIX，绕过了项目既定的 CBOE 优先链。宏观数据在
        # 30 只标的扫完之后才抓，配额已耗尽（2026-08-14 全天 363 条 429），7 个
        # 标的全灭 → 整体降级到 base 的 vix=20.0，被当作"偏高恐慌"写进报告，
        # 而当天真实 VIX 是 14.25。CBOE 的 VIX_History.csv 无 key、无限流。
        _cboe_vix = None
        _vix_src_override = ""
        # v0.45.59：补跑时 VIX 取目标日快照里的观测值。
        # 实时问 CBOE 拿到的是**今天**的 VIX —— 对 8/27 的报告是错的，
        # 而 market.json 里正躺着当天 17:05 ET 抓下的真值。
        if _snap:
            try:
                _vt = ((_snap.get("market") or {}).get("cboe") or {}).get("vix_term") or {}
                _sv = _vt.get("vix_spot")
                if isinstance(_sv, (int, float)) and _sv > 0:
                    _cboe_vix = float(_sv)
                    _vix_src_override = "cloud_snapshot_cboe"
            except Exception as _e_sv:  # noqa: BLE001
                _log.debug("快照 VIX 读取失败: %s", _e_sv)
        if _cboe_vix is None:
            try:
                from cboe_vix import get_vix_spot as _cboe_vix_spot
                _spot = _cboe_vix_spot()
                if _spot:
                    _cboe_vix = _spot[0]
            except Exception as _e_cv:  # noqa: BLE001
                _log.debug("CBOE VIX 不可用，回落 yfinance: %s", _e_cv)

        if not data:
            # yfinance 全灭。但 CBOE 若拿到真实 VIX，就不该让它跟着变成 20.0 ——
            # 其余字段照旧降级，只把 VIX 这一项换成观测值并如实标源。
            if _cboe_vix is not None:
                _partial = dict(base)
                _partial["vix"] = _cboe_vix
                _partial["vix_regime"] = _classify_vix(_cboe_vix)
                _partial["vix_source"] = "cboe"
                _partial["summary"] = f"宏观数据不可用（VIX {_cboe_vix:.1f} 来自 CBOE，其余降级）"
                return _partial
            return base

        # ---- VIX 分析 ----
        if _cboe_vix is not None:
            vix = _cboe_vix
            # v0.45.59：补跑时必须区分「今天问 CBOE 拿的」与「目标日快照里的」。
            # 两者同形（都是一个 float），标成同一个 "cboe" 就无从分辨报告里的
            # VIX 究竟属于哪一天 —— 正是 MEMORY 里「读 vix 前先看 vix_source」
            # 那条要防的事。
            _vix_source = _vix_src_override or "cboe"
        else:
            vix = data.get("VIX", {}).get("last", 20.0)
            _vix_source = "yfinance" if "VIX" in data else "fallback"
        vix_change = data.get("VIX", {}).get("change_pct", 0.0)

        vix_regime = _classify_vix(vix)

        # ---- 10Y 利率分析 ----
        tnx = data.get("TNX", {}).get("last", 4.5)
        tnx_change = data.get("TNX", {}).get("change_pct", 0.0)

        if tnx < 3.0:
            rate_env = "low"
        elif tnx < 4.5:
            rate_env = "moderate"
        else:
            rate_env = "high"

        # ---- 美元趋势 ----
        dxy_change = data.get("DXY", {}).get("change_pct", 0.0)
        if dxy_change > 0.3:
            dollar_trend = "strong"
        elif dxy_change < -0.3:
            dollar_trend = "weak"
        else:
            dollar_trend = "neutral"

        # ---- 大盘趋势（5日）----
        spx_change = data.get("SPX", {}).get("change_pct", 0.0)
        if spx_change > 0.5:
            market_trend = "bull"
        elif spx_change < -0.5:
            market_trend = "bear"
        else:
            market_trend = "neutral"

        # ---- 黄金趋势（避险指标）----
        gold_price = data.get("GLD", {}).get("last")
        gold_change = data.get("GLD", {}).get("change_pct", 0.0)
        if gold_change > 1.0:
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

        def _set_curve(y2: float, src: str):
            """同一段判定逻辑此前抄了三遍（近似 / FRED / 本次新增财政部）。
            抄三遍就有三处会漂移 —— 收敛成一处。"""
            nonlocal treasury_2y, yield_spread, yield_curve, _2y_source
            treasury_2y = round(y2, 3)
            yield_spread = round((tnx - y2) * 100, 1)     # bp
            yield_curve = ("inverted" if yield_spread < -10
                           else "flat" if yield_spread < 20 else "normal")
            _2y_source = src

        # v0.45.60：财政部日度曲线直接给真 2Y —— 优先于下面的 5Y 近似。
        #
        # 那个近似（`5Y + 0.15`）是拿不到 2Y 时代的产物，而它会**扭曲曲线判定**：
        # 2026-08-27 实测 5Y=4.38 → 近似 2Y=4.53 → 10Y−2Y=+14bp 判成 "flat"；
        # 真 2Y=4.20 → 利差 +47bp，实际是 "normal"。差了一个档位。
        _tsy_2y = data.get("TWO", {}).get("last")
        if _tsy_2y is not None and tnx > 0:
            _set_curve(_tsy_2y, "treasury_gov")
        else:
            # 退而求其次：5Y 近似。保留是因为财政部不可得时它总比 unknown 强，
            # 但必须标出来，否则读者无从知道这个 2Y 是推算的。
            fvx = data.get("FVX", {}).get("last")
            if fvx is not None and tnx > 0:
                _set_curve(fvx + 0.15, "approx_from_5y")

        # ---- FRED API（提前获取以修正 2Y 曲线数据）----
        fred_data = {}
        fred_key = _load_fred_key()
        if fred_key:
            fred_data = _fetch_fred_series(fred_key)
            # FRED 精确 2Y 覆盖 5Y 近似 —— 但**不覆盖财政部**：
            # FRED 转发的就是财政部的数，且晚一天（实测 08-26 vs 08-27）。
            # 用它盖掉更新的同源数据是纯粹的倒退。
            if (fred_data.get("treasury_2y") is not None
                    and _2y_source != "treasury_gov"):
                _set_curve(fred_data["treasury_2y"], "fred")

        # ---- 板块轮动 ----
        sector_rotation = _fetch_sector_rotation(yf)

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

        # ---- 宏观政体判断 ----
        if score >= 7:
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
        if vix_change > 10:
            headwinds.append(f"VIX 单日飙升 {vix_change:+.1f}%（波动率冲击）")
        if rate_env == "high" and tnx_change > 2:
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
            f"VIX {vix:.1f}({vix_regime})",
            f"10Y {tnx:.2f}%",
            f"大盘{spx_change:+.1f}%",
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
            "macro_score": round(score, 1),
            "vix": round(vix, 2),
            "vix_change_pct": round(vix_change, 2),
            "vix_regime": vix_regime,
            "treasury_10y": round(tnx, 3),
            "treasury_2y": treasury_2y,
            "yield_spread": yield_spread,
            "yield_curve": yield_curve,
            "rate_environment": rate_env,
            "dollar_trend": dollar_trend,
            "market_trend": market_trend,
            "spx_change_pct": round(spx_change, 2),
            "gold_price": round(gold_price, 2) if gold_price else None,
            "gold_change_pct": round(gold_change, 2),
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
            "data_source": _compose_data_source(_as_of, _src_map, data, bool(fred_data)),
            "field_sources": dict(_src_map),   # 逐字段来源，空 = 该项走了 yfinance
            "treasury_2y_source": _2y_source,  # treasury_gov / fred / approx_from_5y
            "vix_source": _vix_source,
            "as_of": _as_of,          # None = 实时口径
        }

    except ImportError:
        _log.warning("yfinance 不可用，宏观数据降级")
        return base
    except Exception as e:
        _log.warning("宏观数据获取失败: %s", e)
        return base


def _fetch_fred_series(api_key: str) -> Dict:
    """从 FRED API 获取 CPI（同比）、失业率等月度数据（使用 requests 解决 macOS SSL 问题）

    已集成弹性层：CircuitBreaker 熔断 + get_session 连接池复用。
    """
    # 熔断检查
    if _fred_breaker and not _fred_breaker.allow_request():
        _log.warning("FRED API 熔断中，跳过请求")
        return {}

    result = {}
    try:
        import requests as _req
        _session = get_session("fred") if _RESILIENCE_OK else _req
        base = "https://api.stlouisfed.org/fred/series/observations"

        # CPI 同比：取最近 13 个月做真正 YoY（而非月环比年化）
        r = _session.get(base, params={
            "series_id": "CPIAUCSL", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "13"
        }, timeout=8)
        if r.ok:
            obs = r.json().get("observations", [])
            if len(obs) >= 13:
                v_now = float(obs[0]["value"])
                v_year_ago = float(obs[12]["value"])
                result["cpi_yoy"] = round((v_now / v_year_ago - 1) * 100, 2)
                result["cpi_date"] = obs[0]["date"]

        # 失业率：最新值
        r2 = _session.get(base, params={
            "series_id": "UNRATE", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "1"
        }, timeout=8)
        if r2.ok:
            obs2 = r2.json().get("observations", [])
            if obs2:
                result["unemployment"] = float(obs2[0]["value"])
                result["unemployment_date"] = obs2[0]["date"]

        # 联邦基金利率（实际有效利率）
        r3 = _session.get(base, params={
            "series_id": "DFF", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "1"
        }, timeout=8)
        if r3.ok:
            obs3 = r3.json().get("observations", [])
            if obs3:
                result["fed_funds_rate"] = float(obs3[0]["value"])

        # 2Y 国债收益率（收益率曲线精确数据）
        r4 = _session.get(base, params={
            "series_id": "DGS2", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "1"
        }, timeout=8)
        if r4.ok:
            obs4 = r4.json().get("observations", [])
            if obs4 and obs4[0].get("value", ".") != ".":
                result["treasury_2y"] = float(obs4[0]["value"])

        # HY 信用利差（ICE BofA US HY OAS，单位: 百分点 → 转换为 bp）
        r5 = _session.get(base, params={
            "series_id": "BAMLH0A0HYM2", "api_key": api_key,
            "file_type": "json", "sort_order": "desc", "limit": "2"
        }, timeout=8)
        if r5.ok:
            obs5 = r5.json().get("observations", [])
            if obs5 and obs5[0].get("value", ".") != ".":
                # FRED 返回百分点（e.g. 3.50），乘以 100 → bp（350bp）
                result["hy_spread_bp"] = round(float(obs5[0]["value"]) * 100, 0)
                result["hy_spread_date"] = obs5[0]["date"]
                # 日环比变动（bp）
                if len(obs5) >= 2 and obs5[1].get("value", ".") != ".":
                    prev_bp = float(obs5[1]["value"]) * 100
                    result["hy_spread_chg_bp"] = round(
                        result["hy_spread_bp"] - prev_bp, 1)

        # 至少有一个成功响应 → 记录成功
        if _fred_breaker and result:
            _fred_breaker.record_success()

    except Exception as e:
        _log.debug("FRED API 调用失败: %s", e)
        if _fred_breaker:
            _fred_breaker.record_failure()
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


def _fetch_sector_rotation(yf_module=None) -> Dict:
    """获取 11 个 SPDR 板块 ETF 的 5 日表现，返回板块轮动数据（并行 + 单 ETF 缓存）"""
    result = {"hot": [], "cold": [], "full": {}}
    try:
        yf = yf_module
        if yf is None:
            import yfinance as yf
        tickers = list(_SECTOR_ETFS.keys())
        now = time.time()

        # 分离：已缓存 vs 需拉取
        to_fetch = []
        performances = []
        with _etf_lock:
            for etf in tickers:
                cached = _etf_cache.get(etf)
                if cached and (now - cached[0]) < _CACHE_TTL:
                    name, chg = cached[1]
                    performances.append((etf, name, chg))
                    result["full"][etf] = (name, chg)
                else:
                    to_fetch.append(etf)

        # 并行拉取缺失 ETF
        def _fetch_one(etf):
            try:
                t = yf.Ticker(etf)
                hist = t.history(period="5d", interval="1d")
                if hist is not None and len(hist) >= 2:
                    first_close = float(hist["Close"].iloc[0])
                    last_close = float(hist["Close"].iloc[-1])
                    # < 5 防 yfinance sample data ~1.0 哨兵值（ETF 真实价格均 > $5）
                    if first_close >= 5:
                        chg = round((last_close / first_close - 1) * 100, 2)
                        # 5 日涨跌 ±50% 以上为数据异常，归零保守处理
                        if abs(chg) > 50:
                            chg = 0.0
                        name = _SECTOR_ETFS[etf]
                        return (etf, name, chg)
            except Exception as e:
                _log.debug("Sector ETF %s fetch failed: %s", etf, e)
            return None

        if to_fetch:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="etf") as pool:
                futures = {pool.submit(_fetch_one, etf): etf for etf in to_fetch}
                for fut in as_completed(futures):
                    fetched = fut.result()
                    if fetched:
                        etf, name, chg = fetched
                        performances.append(fetched)
                        result["full"][etf] = (name, chg)
                        with _etf_lock:
                            _etf_cache[etf] = (time.time(), (name, chg))

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
        _log.debug("板块轮动数据获取失败: %s", e)
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
