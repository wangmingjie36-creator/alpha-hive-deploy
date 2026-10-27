"""yfinance 期权链 → 与 CBOE 原始合约取数函数（`cboe_options` 里给卖权页用的那个）同形的原始合约（Alpha Bot 的备用取数源，v0.45.603）。

起因（2026-10-22）：NVDL（NVDA 的 2 倍杠杆 ETF）盘中拉取一直 `stale_vintage`——CBOE 给它的文件整个上午停在昨天
收盘（15:59:59，今天一张合约都没成交过），同一轮里 NVDA / TSLA / META / AMZN 都是新的；yfinance 同一时刻有 10:50 ET
的成交。用户要求「换 yfinance 试一下」。

用法（`alphabot/service.py::_chain_fetch`）：**只有** CBOE 报 `stale_vintage`、**且**该票在 `service.YF_FALLBACK_TICKERS`
里才走这里——CBOE 一恢复就自动改回 CBOE，不是「永久换源」。⚠️ **只给 Alpha Bot 的现算页（盘中 / 标的 / 总览）**；
溢价页、卖权账本（`sell_strike_ledger._default_fetch`，预注册检验）与日报仍只认 CBOE，不经过这里。

口径差异（页面如实写，不装作等同 CBOE）：
  · 没有 delta / gamma / vega / theta / theo：置 None，水平引擎（`sell_strike_levels`）对 None 本来就按 IV 用 BS 现算；
  · IV 是 Yahoo 自己求解的，失败留下的「减半阶梯 / 1e-05 地板」不是观测（`yahoo_iv.is_placeholder_iv`）⇒ 置 None；
  · 没有 `iv30`（None）；标的价取最近一根 1 分钟线的收盘（约 15 分钟延迟），来源标签 `yfinance_intraday`；
  · 一次要发「1 + 每个到期日 1」个请求（只取 ≤ 120 天、最多 10 个到期日），限流由 `yf_gate` 管。
陈旧判据与 CBOE 同一条：最新成交的美东日期 < 此刻应有的交易日 ⇒ `stale_vintage`，不拿旧数据冒充盘中。

v0.45.662 加 BILI 时补的两道护栏（BILI 的 CBOE 文件盘中不更新、夜里才刷新一次；yfinance 一侧它却是本仓出过坏数据的票）：
  · **OI 是不是这一场的**——过全仓唯一判据 `oi_staleness`（v0.45.512 / 542 / 544：2026-10-12 Yahoo 全市场 OI 几乎清零，
    BILI 当天 put/call 算成 298.46）。判陈旧 ⇒ `yf_oi_stale`；这条链最近有成交的合约不足以判 ⇒ `yf_oi_unjudgeable`。
    两者都**拒绝整份链**而不是「OI 当 0 照用」：水平引擎吃 OI（GEX / 主要水平），OI 成片为 0 时它不报错、只会给出
    一张看起来正常的错图。备用源本来就是兜底，宁可页面写「现算不可得：<原因>」也不拿坏数凑数。判据吃的是**未过滤**的
    全部到期日的原始表（单个到期日会误判，见 `oi_staleness` docstring；收盘后丢掉的 DTE=0 到期日也算在内——
    `test_the_oi_judge_sees_the_unfiltered_expiries` 钉住这一点）。
  · Yahoo 的 IV 求解失败值（`yahoo_iv.is_placeholder_iv`）早就置 None；现在把置 None 的合约数记进 `n_iv_missing`
    （BILI 实测一条链里约 18% 是占位值）。`n_iv_missing` 与 `oi_check` **页面不显示**，只进
    `service._chain_fetch` 的 info 日志（OI 占比、无可用 IV 的合约数）和返回值，供排查。
  实测（2026-10-27 盘中，BILI）：判据 66 个近期成交合约、OI>0 占 0.98、可判、不陈旧；与 CBOE 同合约 OI 对账 172 / 202
  一致，其余多为小幅差异，另有几个 Yahoo 记 0 而 CBOE 记 13 / 57（单合约漏记，判据是链级的，抓不到这种，页面口径里如实写）。
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Callable, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SOURCE = "yfinance_intraday"
MAX_DTE = 120
MAX_EXPIRIES = 10


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _et_naive_iso(ts) -> Optional[str]:
    """pandas / datetime 时间戳 → 美东墙钟的 ISO 字符串（同 CBOE `last_trade_time` 的写法，不带时区）。

    缺失（None / NaT / NaN）⇒ None。⚠️ `pd.NaT.isoformat()` 返回字符串 `"NaT"`，而 `"NaT" > "2026-…"`（N 比数字大）：
    一个从没成交过的合约就能把 `newest` 顶成 `"NaT"`、让 `vintage < expected` 恒为假——一条昨天的链被当成新鲜放行
    （v0.45.662 二次检查实测，自 v0.45.603 就在）。"""
    if ts is None or ts != ts:                       # NaT / NaN 都不等于自己
        return None
    try:
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if dt.tzinfo is not None:
            dt = dt.astimezone(ET)
        return dt.replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 - 判不了就当没有这个时间
        return None


def _rows(df, expiry: str, cp: str, dte: int, is_placeholder: Callable) -> List[dict]:
    out = []
    for r in df.to_dict("records"):
        K = _num(r.get("strike"))
        sym = r.get("contractSymbol")
        if K is None or K <= 0 or not sym:
            continue
        iv = _num(r.get("impliedVolatility"))
        if iv is not None and (iv <= 0 or is_placeholder(iv)):
            iv = None
        oi = _num(r.get("openInterest"))
        out.append({"symbol": sym, "expiry": expiry, "cp": cp, "strike": K, "dte": dte, "iv": iv,
                    "delta": None, "gamma": None, "vega": None, "theta": None, "theo": None,
                    "oi": oi if oi is not None else 0.0, "volume": _num(r.get("volume")),
                    "bid": _num(r.get("bid")), "ask": _num(r.get("ask")),
                    "last_trade_time": _et_naive_iso(r.get("lastTradeDate"))})
    return out


def fetch_yf_raw_contracts(ticker: str, *, as_of: Optional[str] = None, now_et: Optional[datetime] = None,
                           yf_module=None) -> Tuple[Optional[dict], Optional[str]]:
    """返回 `(结果, None)` 或 `(None, 原因)`——与 CBOE 那个取数函数同一契约，永不抛。
    原因：`stale_vintage` / `vintage_mismatch` / `yf_no_expiries` / `yf_price_unavailable` / `yf_no_contracts` /
    `yf_oi_stale` / `yf_oi_unjudgeable` / `yf_exception:<类名>`。`yf_module` 只给测试注入。"""
    try:
        import cboe_options as CB
        import oi_staleness
        from yahoo_iv import is_placeholder_iv
        _ensure = lambda: None  # noqa: E731
        if yf_module is None:                      # 真取数才装限流闸门（测试注入假模块时不碰全局）
            import yfinance as yf_module
            try:
                from yf_gate import ensure as _ensure
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断
                pass

        now = (now_et or datetime.now(ET)).astimezone(ET)
        _ensure()
        tk = yf_module.Ticker(ticker)
        expiries = list(tk.options or [])
        if not expiries:
            return None, "yf_no_expiries"
        bars = tk.history(period="1d", interval="1m")
        S = _num(bars["Close"].dropna().iloc[-1]) if bars is not None and len(bars) and "Close" in bars else None
        if not S or S <= 0:
            return None, "yf_price_unavailable"

        today = now.date()
        chosen = [e for e in expiries if 0 <= (date.fromisoformat(e) - today).days <= MAX_DTE][:MAX_EXPIRIES]
        frames = []
        for e in chosen:
            _ensure()
            ch = tk.option_chain(e)
            frames.append((e, ch.calls, ch.puts))

        contracts: List[dict] = []
        newest = None
        for e, calls, puts in frames:
            dte = (date.fromisoformat(e) - today).days
            for df, cp in ((calls, "C"), (puts, "P")):
                rows = _rows(df, e, cp, dte, is_placeholder_iv)
                contracts.extend(rows)
                for r in rows:
                    lt = r["last_trade_time"]
                    if lt and (newest is None or lt > newest):
                        newest = lt
        if not contracts or newest is None:
            return None, "yf_no_contracts"

        vintage = newest[:10]                       # 最新一笔成交的美东日期 = 这份链的 vintage
        expected = CB.session_date_at(now)
        if expected and vintage < expected:
            return None, "stale_vintage"
        if as_of is not None and vintage != as_of:
            return None, "vintage_mismatch"
        oi_check = oi_staleness.assess_frames([df for _, calls, puts in frames for df in (calls, puts)])
        if not oi_check["judgeable"]:
            return None, "yf_oi_unjudgeable"
        if oi_check["stale"]:
            return None, "yf_oi_stale"
        session_live = CB._raw_session_live(vintage, now)
        kept = [c for c in contracts if not (c["dte"] == 0 and not session_live)]
        if not kept:
            return None, "yf_no_contracts"
        return {
            "ticker": ticker, "as_of": as_of or vintage, "vintage_date": vintage, "underlying_price": S,
            "underlying_price_source": SOURCE, "session_live": session_live, "fetched_at": now.isoformat(),
            "payload_last_trade_time": newest, "iv30": None, "contracts": kept, "n_raw": len(contracts),
            "n_dropped_unparseable": 0, "n_expired_excluded": 0, "n_expiring_today_excluded": len(contracts) - len(kept),
            "data_source": "yfinance", "oi_check": oi_check,
            "n_iv_missing": sum(1 for c in kept if c["iv"] is None),
        }, None
    except Exception as exc:  # noqa: BLE001 - 取数层的任何异常都是「这次取不到」，如实报原因
        return None, f"yf_exception:{type(exc).__name__}"
