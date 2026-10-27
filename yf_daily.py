"""yfinance 日线的**唯一取数入口**（v0.45.652；v0.45.658 起两个 API 都收进来）：取到即去掉「公司行动空行」，并告诉调用方
「最近一场已收盘的 K 线是不是缺了」。

为什么要有它（根因）
------------------
yfinance 1.2.0 的 `Ticker.history()` 缺省 `actions=True`，把分红 / 拆股并进日线。**越界**的事件（早于首根 K 线、或不早于
末根 + 1 天）会被 `utils.safe_merge_dfs` 补成一行「开高低收全空、成交量 0、分红 / 拆股非零」：

* 只传 `period=`（`start` 为 None）⇒ 窗口外边界日的分红可能漏成**首行**（实测 META 2026-09-21，`period="1mo"`）。
  传 `start=` 时 yfinance 把事件截到首根 K 线当天起，首行不会有；自定义 period（如 `"25d"`）内部走 start，同样不会有。
* 事件日期晚于末根 K 线（当天没有 K 线 / 盘前跑）⇒ **末行**。带不带 `start` 都会，只有 `end` 截得掉。
* 窗口中间没有 K 线的除息日 ⇒ 分红并进前一根，不造空行（只是缺一根）。日内（1m）的越界事件 yfinance 直接丢。

`yf.download` 缺省 `actions=False`：yfinance 自己在 `PriceHistory.history` 的「missing rows cleanup」里把这种行删掉——首行删了无害，
**末行删了就是前一场冒充今天，而且一声不吭**（没有事件列，调用方连「删过」都看不出来）。

v0.45.645 只在 H1 行情三字段的四条取数路径上去掉了它；v0.45.652 把其余 `Ticker.history()` 读者收进本入口；v0.45.658（652 二次检查）
补上 `yf.download` 读者（预取包是 Rival / Scout 的主路径）与带日期窗口的 `history()` 读者，入口才真是「唯一」：守卫
`tests/test_yf_daily.py::TestSingleEntry` 让「在别处直调 `.history(` / `.download(`」写不出来（剩下的例外只有结构上可核的四类）。

口径
----
* 判据只有一份：`price_metrics.event_row_layout`（645 的 `event_only_positions` + 首段 / 末段），事件列用 `EVENT_COLUMNS`
  （比 H1 多 `Capital Gains`：基金 / ETF 的资本利得分配同样会单独成行）。按行号删，不按索引标签。
* **末行按「这一路要哪天」判**（v0.45.658）：末段空行只是证据，问题是「剩下的末根还是不是最近一场**已收盘**的交易日」
  （`latest_completed_session`：今天已过收盘——提前收盘日 13:00——就是今天，否则上一个交易日）。末根早于它 ⇒ 那一场的 K 线缺了
  ⇒ 第二个返回值为真，用末根当「当天的值」的调用方必须当取不到（末行策略逐点登记在 `tests/test_yf_daily.py::POLICY`，这类为 refuse）；末根就是它（盘前跑、除息日是今天，
  今天那场还没收）⇒ 剩下的末根本来就是最近一场，与没有事件的日子一样照用，只计数。此前（652）不看时刻，盘前跑除息日会白白丢数据。
  日历判不了 ⇒ 按缺了算（宁缺不错）并计 `calendar_unavailable`。非日线（`interval` ≠ 1d）不做日历判断，末段即缺。
  ⚠️ H1 三字段（645 的 `price_metrics.drop_event_only_rows`）仍是「末段即缺」——那是用户 10-25 定的、冻结层逐例对拍的口径，本模块不动它。
* `history(tk, source=, **request)` → `(日线, 最近一场缺了)`，日线保留事件列（与 `Ticker.history()` 同形）。
* `download(yf, tickers, source=, **request)` → `(帧, {ticker: 最近一场缺了})`：内部用 `actions=True` 取（看得见空行），去掉空行后
  **还原成调用方原本用 `actions=False` 会拿到的那一帧**（去掉事件列；只因空行才存在的行整行去掉；因置空被迫升成浮点的整数列还原）
  ⇒ 按日期用的调用方（POLICY 为 ignore）拿到的帧与改前逐位相同（真 yfinance 代码路径上对拍钉住；索引的 `freq` 属性是 pandas 推断的
  元数据、两边各自推，不在「相同」之内——`DataFrame.equals` 也不看它），只有 refuse 的调用方行为变。
  调用方不许传 `actions` / `keepna`（入口自己定）。
* 计数独立于 645 的 `price_metrics.event_row_stats`（评分时快照的时点不同，见 652 说明）。`stats()` ⇒ scan_timing `yf_daily` ⇒
  `alert_manager._check_yf_daily_event_rows`：「最近一场缺了」/ 中间 ⇒ P2；首段与「今天那场还没收」只计数。
* 不调限流闸门、不 import data_pipeline：调用方原来怎么排队照旧（`yf.download` 由调用方传进来，yf_gate 的补丁照样生效）。
"""
from __future__ import annotations

import datetime as _dt
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

import price_metrics

#: 公司行动空行的事件列（H1 那组 + 资本利得）
EVENT_COLUMNS: Tuple[str, ...] = ("Dividends", "Stock Splits", "Capital Gains")

#: 调用点 → 说明。`history` / `download` 的 `source=` 只认这里的名字（告警按它点名）；守卫核对代码里用到的名字与本表**双向**一致。
SOURCES: Dict[str, str] = {
    # ── Ticker.history()：用末根 / 近几根当「现在」的 ──
    "options_hv": "options_analyzer.fetch_historical_hv：1y 已实现波动率 → iv_rank（Oracle 期权分）",
    "rival_fallback": "swarm_agents.base._yf_history：预取包缺票时的直连回退（Rival RSI / MACD / BB）",
    "macro_quotes": "fred_macro._fetch_macro_data 实时分支：非 yfinance 源没供上的宏观符号（GuardBee 宏观票）",
    "macro_asof": "fred_macro._asof_history：补跑对齐目标日的宏观 / 板块 ETF 日线",
    "sector_rotation": "fred_macro._fetch_sector_rotation 实时分支：11 只板块 ETF 5 日涨跌（GuardBee 宏观票 → 权重路由）",
    "snapshot_entry_price": "alpha_hive_daily_report._post_scan_notify：快照入场价的 yfinance 回退",
    "synthetic_swarm": "alpha_hive_daily_report._generate_synthetic_swarm_results：合成蜂群结果的价格 / 5 日动量",
    "dashboard_price": "dashboard_renderer._detail：价格 / 5 日动量的最后一道回退（展示）",
    "mcp_price_history": "alpha_hive_mcp.alphahive_get_price_history：MCP 蜡烛（展示）",
    "ticker_validity": "swarm_agents.cache.check_ticker_validity：退市 / 低价检查",
    "spot_vix": "vix_term_structure._get_spot_vix：^VIX 现价兜底（指数无分红，入口照过）",
    "iv_crush": "iv_crush_scraper.get_iv_crush_data：历史财报涨跌（深度报告）",
    "crowding_cli": "crowding_detector.get_crowding_metrics：拥挤度示例（生产只被它自己的 __main__ 调用）",
    "risk_engine_cli": "risk_engine：收益序列 / 历史 VaR / 全量分析 / 多票收益 / β（生产只被它自己的 CLI 调用）",
    # ── Ticker.history()：带日期窗口、按日期用（v0.45.658 迁入；此前是「dated_window」例外）──
    "backtest_engine": "backtest_engine.PriceBackfiller._get_price_on_date：回测按日取价",
    "backtester_history": "backtester.Backtester._history：回测缓存未命中时的逐票取价",
    "dynamic_exit": "dynamic_exit_backtest._fetch_price_series：动态离场回测的价格序列",
    "factor_attribution": "factor_attribution._get_stock_returns：因子归因的收益序列",
    "outcomes": "outcomes_fetcher.OutcomesFetcher._fetch_remote：T+N 结果回填的逐日 OHLC",
    "paper_prefetch": "paper_portfolio.prefetch_ohlc：纸面组合批量预取 OHLC",
    "paper_replay": "paper_portfolio._ReplayOhlcWindow._download：纸面组合回放窗口",
    "paper_direct": "paper_portfolio._fetch_ohlc_direct：纸面组合逐票直连 OHLC",
    "portfolio_backtest_preds": "portfolio_backtest.load_verified_predictions：组合回测的 SPY 基准",
    "portfolio_backtest_spy": "portfolio_backtest._fetch_spy_prices：组合回测的 SPY 价格",
    "etf_factors": "portfolio_factor_attribution._load_etf_factors：组合因子归因的 ETF 收益",
    # ── yf.download()（v0.45.658 迁入）──
    "prefetch_bundle": "swarm_agents.base.prefetch_market_bundle：一轮扫描的日线预取包（Rival / Scout 主路径）",
    "close_panel": "swarm_agents.base._yf_close_panel：预取包缺列时的收盘价面板直连（Scout 相对强度）",
    "market_regime_ma": "market_intelligence.detect_market_regime._get_ma：现价与均线（GuardBee 市场政体）",
    "supply_chain": "market_intelligence.get_supply_chain_signals：供应链 5 日涨跌（ScoutBee）",
    "iv_rv_spread": "market_intelligence.calculate_iv_rv_spread：已实现波动率窗口（Oracle IV-RV，维度 IC H2 的输入，按日期用）",
    "index_level": "cboe_fetcher.CBOEDailyFetcher._fetch_index_level：SKEW / VVIX 现值的 yfinance 兜底（指数无分红）",
    "concentration": "portfolio_concentration.analyze_concentration：一个月收益相关性",
    "backtest_prefetch": "backtester.Backtester._prefetch_backtest_prices：回测批量预取",
    "dir_accuracy_backfill": "backfill_dir_accuracy._fetch_closes：方向准确率回填的收盘价",
    "close_correction": "close_correction.official_closes：按日核实官方收盘",
    "ic_diagnostics": "ic_diagnostics._close_panel：IC 诊断的收盘价面板",
    "pead": "pead_analyzer._compute_post_earnings_drift：财报后漂移",
    "coverage_gate": "scan_coverage_gate.check_prices：扫描覆盖率闸门的价格核对",
    "forward_vol": "signal_archive._forward_realized_vol：前瞻已实现波动率",
}

_ACTION_COLUMNS = frozenset(EVENT_COLUMNS)
_lock = threading.Lock()
_MAX_ITEMS = 20
_stats: Dict[str, Any] = {}


def _zero() -> Dict[str, Any]:
    return {"calls": 0, "frames": 0, "dropped": 0, "first_row": 0, "last_row": 0, "last_row_pending": 0, "middle": 0,
            "calendar_unavailable": 0, "uninspectable": 0, "items_total": 0, "items": [], "by_source": {}}


_stats.update(_zero())


# ── 「最近一场已收盘」──────────────────────────────────────────────────────────

def _now_et() -> _dt.datetime:
    """美东此刻。本模块读钟只有这一处（测试经 monkeypatch 钉住）。"""
    from zoneinfo import ZoneInfo
    return _dt.datetime.now(ZoneInfo("America/New_York"))


def latest_completed_session(now: Optional[_dt.datetime] = None) -> Optional[_dt.date]:
    """`now`（美东，缺省此刻）时最近一个**已收盘**的交易日：今天是交易日且已过收盘（`is_trading_day.session_close_et`，
    提前收盘日 13:00）⇒ 今天；否则 `bars_integrity.previous_completed_session`（含临时休市表）。判不了 ⇒ None。"""
    try:
        now = now or _now_et()
        from bars_integrity import AD_HOC_CLOSURES, previous_completed_session
        from is_trading_day import is_trading_day, session_close_et
        d = now.date()
        if d not in AD_HOC_CLOSURES and is_trading_day(d)[0] and now.time() >= session_close_et(d):
            return d
        return previous_completed_session(d)
    except Exception:  # noqa: BLE001 —— 判不了由调用方按「缺了」处理并计数
        return None


def _bar_date(x) -> Optional[_dt.date]:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _trail_verdict(kept, trail: int, daily: bool) -> Tuple[bool, str]:
    """末段空行意味着什么：``(最近一场缺了, 分类)``，分类 ∈ ``""`` / ``"missing"`` / ``"missing_no_calendar"`` / ``"pending"``。"""
    if not trail:
        return False, ""
    if not daily or len(kept) == 0:
        return True, "missing"
    lcs = latest_completed_session()
    if lcs is None:
        return True, "missing_no_calendar"
    last = _bar_date(kept.index[-1])
    if last is None or last < lcs:
        return True, "missing"
    return False, "pending"                     # 剩下的末根就是最近一场已收盘的：今天那场还没收


def _is_daily(request: Dict[str, Any]) -> bool:
    return request.get("interval", "1d") == "1d"


def _is_frame(x) -> bool:
    return hasattr(x, "columns") and hasattr(x, "iloc") and hasattr(x, "__len__")


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"yf_daily: 未登记的 source {source!r}（先登记到 yf_daily.SOURCES）")


# ── 入口 ────────────────────────────────────────────────────────────────────

def history(tk, *, source: str, **request) -> Tuple[Any, bool]:
    """`tk.history(**request)`（`tk` = `yf.Ticker(...)` 或同形对象）去掉公司行动空行后返回 `(日线, 最近一场缺了)`。

    没有空行时第一项就是 yfinance 返回的同一个对象。`source` 必须是 `SOURCES` 里的名字。取数异常原样抛（调用方原有的
    except 照旧接）。返回的不是 DataFrame（只在测试桩里见过）⇒ 原样返回、计 `uninspectable`，不猜。"""
    _check_source(source)
    hist = tk.history(**request)
    _count_call(source)
    if not _is_frame(hist):
        _count_uninspectable()
        return hist, False
    return _strip(source, str(getattr(tk, "ticker", "?")), hist, _is_daily(request))


def download(yf_module, *args, source: str, **request) -> Tuple[Any, Dict[str, bool]]:
    """`yf_module.download(*args, **request)` 的替身：返回 `(调用方原本用 actions=False 会拿到的那一帧, {ticker: 最近一场缺了})`。

    `yf_module` 由调用方传（它手里的 `yfinance`，带 yf_gate 补丁 / 测试替身）。其余参数**原样转交**（tickers 按位置还是按关键字给，
    就怎么传下去——只多一个 `actions=True`）。内部 `actions=True` 才看得见空行；去掉空行后还原成 actions=False 的形状（见模块说明）。
    不许传 `actions` / `keepna`。返回的不是 DataFrame ⇒ 原样返回、空字典。"""
    _check_source(source)
    bad = sorted(k for k in ("actions", "keepna") if k in request)
    if bad:
        raise ValueError(f"yf_daily.download: 别传 {bad}（入口自己用 actions=True 取、再还原成 actions=False 的形状）")
    tickers = args[0] if args else request.get("tickers")
    raw = yf_module.download(*args, actions=True, **request)
    _count_call(source)
    if not _is_frame(raw):
        _count_uninspectable()
        return raw, {}
    return _strip_download(source, raw, tickers, _is_daily(request))


# ── 内部 ────────────────────────────────────────────────────────────────────

def _strip(source: str, ticker: str, frame, daily: bool):
    pos, lead, trail = price_metrics.event_row_layout(frame, EVENT_COLUMNS)
    kept = price_metrics.drop_positions(frame, pos)
    missing, kind = _trail_verdict(kept, trail, daily)
    _record(source, ticker, frame, pos, lead, trail, kind)
    return kept, missing


def _drop_action_columns(frame, price_level: Optional[int]):
    if price_level is None:
        gone = [c for c in frame.columns if c in _ACTION_COLUMNS]
        return frame.drop(columns=gone) if gone else frame
    gone = [c for c in frame.columns if c[price_level] in _ACTION_COLUMNS]
    if not gone:
        return frame
    out = frame.drop(columns=gone)
    out.columns = out.columns.remove_unused_levels()
    return out


def _strip_download(source: str, raw, tickers, daily: bool):
    cols = raw.columns
    nlev = getattr(cols, "nlevels", 1)
    names = [tickers] if isinstance(tickers, str) else list(tickers)
    if len(raw) == 0:
        return _drop_action_columns(raw, None if nlev == 1 else _price_level(cols)), {}
    if nlev == 1:                                   # 单票平列（multi_level_index=False）：整帧就是这一只
        sym = names[0] if len(names) == 1 else "?"
        kept, missing = _strip(source, sym, raw, daily)
        return _drop_action_columns(kept, None), {sym: missing}
    price_level = _price_level(cols)
    if nlev != 2 or price_level is None:            # 认不出的形状：只去事件列（认得出价格层时），不猜哪行是空行
        _count_uninspectable()
        return (raw if price_level is None else _drop_action_columns(raw, price_level)), {}
    tick_level = 1 - price_level
    flags: Dict[str, bool] = {}
    null_rows: Dict[Any, List[int]] = {}
    for sym in dict.fromkeys(cols.get_level_values(tick_level)):
        sub = raw.xs(sym, axis=1, level=tick_level)
        own = [i for i, m in enumerate(sub.notna().any(axis=1).tolist()) if m]   # 这只票自己的行（其余是对齐出来的全空行）
        own_sub = sub.iloc[own]
        pos, lead, trail = price_metrics.event_row_layout(own_sub, EVENT_COLUMNS)
        missing, kind = _trail_verdict(price_metrics.drop_positions(own_sub, pos), trail, daily)
        _record(source, str(sym), own_sub, pos, lead, trail, kind)
        flags[str(sym)] = missing
        if pos:
            null_rows[sym] = [own[i] for i in pos]
    out = raw
    if null_rows:
        import numpy as np
        out = raw.copy()
        touched: set = set()
        for sym, rows in null_rows.items():
            mask = np.zeros(len(out), dtype=bool)
            mask[rows] = True
            for c in out.columns:
                if c[tick_level] == sym:
                    out[c] = out[c].where(~mask)          # 置空（整数列随之升浮点，下面还原）
            touched.update(rows)
        out = _drop_action_columns(out, price_level)
        # 只因空行才存在的行：置空后整行全空 ⇒ 去掉（actions=False 时 yfinance 根本不会产出这一行）
        rows = sorted(touched)
        empty = [r for r, e in zip(rows, out.iloc[rows].isna().all(axis=1).tolist()) if e]
        out = price_metrics.drop_positions(out, empty)
        # 成交量：yfinance 每只票都给 int64，只有对齐出空格才升浮点。去掉上面的行 / 置空后，没有空格的那几列还原成 int64
        # （别的票因为这些行被对齐出 NaN、行去掉后也该回到 int64——actions=False 时这些行根本不存在）
        for c in out.columns:
            if c[price_level] == "Volume" and out[c].dtype.kind == "f" and not out[c].isna().any():
                out[c] = out[c].astype("int64")
        return out, flags
    return _drop_action_columns(out, price_level), flags


def _price_level(cols) -> Optional[int]:
    for i in range(getattr(cols, "nlevels", 1)):
        if "Close" in set(cols.get_level_values(i)):
            return i
    return None


def _count_call(source: str) -> None:
    with _lock:
        _stats["calls"] += 1
        _by(source)["calls"] += 1


def _count_uninspectable() -> None:
    with _lock:
        _stats["uninspectable"] += 1


def _by(source: str) -> Dict[str, int]:
    return _stats["by_source"].setdefault(source, {"calls": 0, "frames": 0, "first_row": 0, "last_row": 0,
                                                   "last_row_pending": 0, "middle": 0})


def _record(source: str, ticker: str, frame, pos: Sequence[int], lead: int, trail: int, kind: str) -> None:
    n = len(frame)
    middle = list(pos[lead:len(pos) - trail])
    flagged = [(i, False) for i in middle]
    with _lock:
        by = _by(source)
        _stats["frames"] += 1
        by["frames"] += 1
        _stats["dropped"] += len(pos)
        _stats["first_row"] += lead                 # 窗口外的边界日漏进来的事件：正常，只计数
        by["first_row"] += lead
        _stats["middle"] += len(middle)             # yfinance 1.2.0 不会产生：出现 = yfinance 行为变了
        by["middle"] += len(middle)
        if trail:
            key = "last_row_pending" if kind == "pending" else "last_row"
            _stats[key] += 1
            by[key] += 1
            if kind == "missing_no_calendar":
                _stats["calendar_unavailable"] += 1
            if kind != "pending":
                flagged.append((pos[-1], True))
        for i, last in flagged:
            _stats["items_total"] += 1
            if len(_stats["items"]) < _MAX_ITEMS:
                _stats["items"].append({"source": source, "ticker": ticker, "date": str(frame.index[i])[:10],
                                        "pos": i, "n": n, "last": last})


def stats() -> Dict[str, Any]:
    """计数副本：``calls`` 入口被调几次；``frames`` 看过几份单票日线（download 一次多票算多份）；``dropped`` 去掉几行，其中
    ``first_row`` 首段（正常）、``middle`` 窗口中间（yfinance 1.2.0 不会产生，出现 = yfinance 行为变了）；末段按份数分两种：
    ``last_row`` = 最近一场已收盘的 K 线缺了（refuse 的调用方已当取不到），``last_row_pending`` = 今天那场还没收（盘前跑，照用）；
    ``calendar_unavailable`` = 交易日历判不了、按缺了算的份数；``items_total`` = 「缺了」的份数 + 中间行数，``items`` 是前 20 条明细；
    ``uninspectable`` = 返回的不是 DataFrame / 认不出的多层列；``by_source`` 逐调用点。"""
    with _lock:
        return {**_stats, "items": [dict(x) for x in _stats["items"]],
                "by_source": {k: dict(v) for k, v in _stats["by_source"].items()}}


def _reset_stats() -> None:
    """测试用：清零（conftest 每条测试前后都清）。"""
    with _lock:
        _stats.clear()
        _stats.update(_zero())
