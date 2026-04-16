"""
Alpha Hive · PaperPortfolio v0.19.0
$50,000 股票现货模拟组合，用于透明展示蜂群方向信号的真实组合级别表现。

设计原则：
- 口径与 backtester.py 一致：股票现货 (bull→买入, bear→融券卖空)
- 不含期权模拟（避免 IV crush 还原误差）
- Defined Risk：-5% SL / +10% TP / T+10 强平
- 成本复用 trading_costs.py
- 审计日志 append-only：closed_trades.jsonl
- Bootstrap：从 report_snapshots 回放历史信号重建 equity curve
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 项目内依赖
try:
    from trading_costs import apply_costs, sharpe_ratio
except Exception:
    def apply_costs(gross, direction, ticker, holding_days, override_slippage_bps=None):
        return {"net_return_pct": gross - 0.12, "cost_pct": 0.12,
                "breakdown": {"slippage_pct": 0.06, "commission_pct": 0.02, "borrow_pct": 0.04}}
    def sharpe_ratio(rets, periods_per_year=52):
        if not rets:
            return 0.0
        m = sum(rets) / len(rets)
        v = sum((r - m) ** 2 for r in rets) / max(1, len(rets) - 1)
        return (m / math.sqrt(v)) * math.sqrt(periods_per_year) if v > 0 else 0.0

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
SNAPSHOT_DIR = BASE_DIR / "report_snapshots"
STATE_DIR = BASE_DIR / "paper_portfolio_state"
STATE_DIR.mkdir(exist_ok=True)

POSITIONS_FILE = STATE_DIR / "positions.jsonl"      # 当前持仓
CLOSED_FILE = STATE_DIR / "closed_trades.jsonl"     # 平仓记录（append-only）
EQUITY_FILE = STATE_DIR / "equity_curve.jsonl"      # 每日净值快照
META_FILE = STATE_DIR / "meta.json"                 # 组合元数据（启动日、现金等）

CONFIG = {
    "starting_capital": 50_000.0,
    "bootstrap_date": "2026-03-09",  # snapshot 最早可用日期（用户请求 01-02 但数据从 03-09 起）
    "max_positions": 15,
    "max_deployed_pct": 30.0,   # 最大部署资金 30% NAV
    "size_pct_by_tier": {       # 基础仓位占 NAV %
        "high": 2.5,            # ⭐⭐⭐ 高置信
        "mid": 1.5,             # ⭐⭐ 中置信
        "low": 0.0,             # ⚠️ 低置信跳过
    },
    "win_rate_multiplier": {    # 按 ticker 历史胜率修正
        "strong": 1.2,          # ≥ 60% + 样本 ≥ 10
        "normal": 1.0,
        "weak": 0.5,            # < 45%
    },
    "sl_pct": 7.0,              # 止损 7%（v0.19.1 参数优化：5%→7% 胜率 33%→50% Sharpe 1.27→2.73）
    "tp_pct": 10.0,             # 止盈 10%
    "time_stop_days": 10,       # T+10 强平
    "entry_conf_min": "mid",    # 最低置信 mid
    "entry_score_bull": 6.5,
    "entry_score_bear": 3.5,
    "min_samples_for_win_rate": 5,  # 低于 5 样本不用胜率过滤

    # ── 两层模式（v0.19.1）──────────────────────────────────────────────────
    # bootstrap_date ～ live_start_date：回放所有 ticker（建立历史基准 + 胜率统计）
    # live_start_date 之后：只开 ticker_whitelist 里的新仓（跟实际报告对齐）
    # ticker_whitelist 留空 [] = 继续全标的
    "live_start_date": "2026-04-16",   # 今天之后进入实时追踪模式
    "ticker_whitelist": ["NVDA"],       # 实时追踪标的（只跑深度报告的 ticker）
}


# ══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    ticker: str
    direction: str          # "bullish" / "bearish"
    entry_date: str
    entry_price: float
    sl_price: float
    tp_price: float
    shares: float           # 允许小数股（按 $ 计算的模拟）
    size_usd: float         # 初始建仓市值
    time_stop_date: str     # T+10 强平日
    confidence: str         # high/mid/low
    score: float            # 蜂群评分
    rationale: str          # 入场依据简述

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ClosedTrade:
    ticker: str
    direction: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    holding_days: int
    shares: float
    gross_return_pct: float
    net_return_pct: float
    cost_pct: float
    pnl_usd: float
    exit_reason: str        # "TP" / "SL" / "TIME"
    confidence: str
    score: float

    def to_dict(self) -> Dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════════════
# JSONL 读写工具
# ══════════════════════════════════════════════════════════════════════════════

def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _write_jsonl(path: Path, records: List[Dict]) -> None:
    """完整重写（用于 positions 这种会删减的）"""
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _append_jsonl(path: Path, record: Dict) -> None:
    """追加（用于 closed_trades / equity_curve）"""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_meta() -> Dict:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    return {
        "version": "0.19.0",
        "starting_capital": CONFIG["starting_capital"],
        "starting_date": CONFIG["bootstrap_date"],
        "cash": CONFIG["starting_capital"],
        "last_run_date": None,
        "config_snapshot": dict(CONFIG),
    }


def _save_meta(meta: Dict) -> None:
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# 置信度 tier 推断（复用 generate_deep_v2 的 P0 逻辑）
# ══════════════════════════════════════════════════════════════════════════════

def _infer_confidence(snapshot: Dict) -> str:
    """根据 snapshot 推断置信度 tier (high/mid/low)。

    Snapshot 可能缺字段，尽量兜底：
    - agent_votes 分散度 → dim_std
    - 无 bear_signals 列表时视为 0
    """
    score = float(snapshot.get("composite_score") or 0)
    votes = snapshot.get("agent_votes") or {}
    if votes:
        vals = [float(v) for v in votes.values() if v is not None]
        if len(vals) >= 2:
            m = sum(vals) / len(vals)
            dim_std = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
        else:
            dim_std = 0.0
    else:
        dim_std = 0.0

    bear_sig_count = len(snapshot.get("bear_signals") or [])

    violations = 0
    if dim_std >= 1.5: violations += 1
    if bear_sig_count > 0: violations += 1

    if violations == 0:
        return "high"
    elif violations == 1:
        return "mid"
    return "low"


# ══════════════════════════════════════════════════════════════════════════════
# Ticker 历史胜率（从 closed_trades 自计算，避免依赖外部缓存）
# ══════════════════════════════════════════════════════════════════════════════

def _ticker_win_rate(ticker: str, closed: List[Dict]) -> Tuple[float, int]:
    trades = [t for t in closed if t["ticker"] == ticker]
    if not trades:
        return 0.0, 0
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    return wins / len(trades), len(trades)


def _size_multiplier(ticker: str, closed: List[Dict]) -> float:
    wr, n = _ticker_win_rate(ticker, closed)
    if n < CONFIG["min_samples_for_win_rate"]:
        return CONFIG["win_rate_multiplier"]["normal"]
    if wr >= 0.60 and n >= 10:
        return CONFIG["win_rate_multiplier"]["strong"]
    if wr < 0.45:
        return CONFIG["win_rate_multiplier"]["weak"]
    return CONFIG["win_rate_multiplier"]["normal"]


# ══════════════════════════════════════════════════════════════════════════════
# 历史价格获取（yfinance，带缓存）
# ══════════════════════════════════════════════════════════════════════════════

_PRICE_CACHE: Dict[Tuple[str, str, str], Dict] = {}


def _fetch_ohlc(ticker: str, start: str, end: str) -> Dict[str, Dict]:
    """拉 [start, end] 区间的每日 OHLC。返回 {date_str: {Open, High, Low, Close}}"""
    key = (ticker, start, end)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]

    try:
        import yfinance as yf
        tkr = yf.Ticker(ticker)
        hist = tkr.history(start=start, end=end, auto_adjust=False)
        if hist is None or len(hist) == 0:
            _PRICE_CACHE[key] = {}
            return {}
        out = {}
        for idx, row in hist.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            out[date_str] = {
                "Open": float(row["Open"]),
                "High": float(row["High"]),
                "Low": float(row["Low"]),
                "Close": float(row["Close"]),
            }
        _PRICE_CACHE[key] = out
        return out
    except Exception:
        _PRICE_CACHE[key] = {}
        return {}


def _next_trading_date(ticker: str, after: str, max_lookahead_days: int = 5) -> Optional[str]:
    """找 after 之后的下一个交易日（有 OHLC 数据的日期）"""
    dt = datetime.strptime(after, "%Y-%m-%d")
    end_dt = dt + timedelta(days=max_lookahead_days + 5)
    ohlc = _fetch_ohlc(ticker, (dt + timedelta(days=1)).strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    if not ohlc:
        return None
    sorted_dates = sorted(ohlc.keys())
    return sorted_dates[0] if sorted_dates else None


# ══════════════════════════════════════════════════════════════════════════════
# 核心逻辑：建仓 / 平仓 / mark-to-market
# ══════════════════════════════════════════════════════════════════════════════

def _should_open(snapshot: Dict, existing_tickers: set, as_of: str = "") -> Tuple[bool, str]:
    """判断是否符合开仓条件。返回 (是否开, 原因说明)"""
    ticker = snapshot.get("ticker")
    if ticker in existing_tickers:
        return False, "已有持仓"

    # ── 两层模式：live_start_date 之后只开白名单 ticker ──
    whitelist = CONFIG.get("ticker_whitelist") or []
    live_start = CONFIG.get("live_start_date") or ""
    if whitelist and live_start and as_of >= live_start:
        if ticker not in whitelist:
            return False, f"实时模式仅追踪 {whitelist}，跳过 {ticker}"
    score = float(snapshot.get("composite_score") or 0)
    direction = (snapshot.get("direction") or "").lower()
    if "bull" in direction:
        if score < CONFIG["entry_score_bull"]:
            return False, f"bull 但 score {score:.1f} < {CONFIG['entry_score_bull']}"
    elif "bear" in direction:
        if score > CONFIG["entry_score_bear"]:
            return False, f"bear 但 score {score:.1f} > {CONFIG['entry_score_bear']}"
    else:
        return False, "非 bull/bear"

    conf = _infer_confidence(snapshot)
    tier_ranks = {"high": 3, "mid": 2, "low": 1}
    if tier_ranks[conf] < tier_ranks[CONFIG["entry_conf_min"]]:
        return False, f"置信 {conf} < {CONFIG['entry_conf_min']}"
    return True, f"{direction} score={score:.1f} conf={conf}"


def _compute_position_size(nav: float, conf: str, ticker: str, closed: List[Dict]) -> float:
    base_pct = CONFIG["size_pct_by_tier"].get(conf, 0.0)
    mult = _size_multiplier(ticker, closed)
    return nav * (base_pct / 100.0) * mult


def _open_position(
    snapshot: Dict,
    nav: float,
    as_of: str,
    ticker_ohlc: Dict[str, Dict],
    closed: List[Dict],
) -> Optional[Position]:
    """用 as_of 日的 Close 作为入场价（成本模型里的滑点会再扣一次）"""
    ticker = snapshot["ticker"]
    direction = snapshot["direction"]
    conf = _infer_confidence(snapshot)
    size_usd = _compute_position_size(nav, conf, ticker, closed)
    if size_usd <= 1:
        return None

    # 入场价：用 snapshot 的 entry_price 或当日 Close
    entry_price = float(snapshot.get("entry_price") or 0)
    if entry_price <= 0 and as_of in ticker_ohlc:
        entry_price = ticker_ohlc[as_of]["Close"]
    if entry_price <= 0:
        return None

    # SL / TP 价位
    if "bull" in direction:
        sl = entry_price * (1 - CONFIG["sl_pct"] / 100.0)
        tp = entry_price * (1 + CONFIG["tp_pct"] / 100.0)
    else:  # bear
        sl = entry_price * (1 + CONFIG["sl_pct"] / 100.0)
        tp = entry_price * (1 - CONFIG["tp_pct"] / 100.0)

    shares = size_usd / entry_price

    # T+10 日期（自然日 +14 粗略覆盖 10 个交易日）
    entry_dt = datetime.strptime(as_of, "%Y-%m-%d")
    time_stop_dt = entry_dt + timedelta(days=14)

    _cs = snapshot.get("composite_score")
    _cs_txt = f"{float(_cs):.1f}" if _cs is not None else "N/A"
    rationale = f"score={_cs_txt} · {conf}"

    return Position(
        ticker=ticker,
        direction="bullish" if "bull" in direction else "bearish",
        entry_date=as_of,
        entry_price=entry_price,
        sl_price=round(sl, 4),
        tp_price=round(tp, 4),
        shares=round(shares, 4),
        size_usd=round(size_usd, 2),
        time_stop_date=time_stop_dt.strftime("%Y-%m-%d"),
        confidence=conf,
        score=float(snapshot.get("composite_score") or 0),
        rationale=rationale,
    )


def _check_exit(pos: Position, as_of: str, ohlc: Dict[str, Dict]) -> Optional[Tuple[str, float, str]]:
    """
    扫描 (entry_date, as_of] 区间的每日 OHLC，检查 SL/TP/TIME 触发。
    返回 (exit_reason, exit_price, exit_date) 或 None（仍持仓）。

    保守规则：同日同时触发 SL+TP → 先 SL（悲观）。
    """
    start = datetime.strptime(pos.entry_date, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(as_of, "%Y-%m-%d")
    time_stop = datetime.strptime(pos.time_stop_date, "%Y-%m-%d")

    dates_in_range = sorted([d for d in ohlc.keys() if
                              start <= datetime.strptime(d, "%Y-%m-%d") <= end])

    for d in dates_in_range:
        bar = ohlc[d]
        lo, hi = bar["Low"], bar["High"]
        dt = datetime.strptime(d, "%Y-%m-%d")

        if pos.direction == "bullish":
            hit_sl = lo <= pos.sl_price
            hit_tp = hi >= pos.tp_price
            if hit_sl and hit_tp:
                return ("SL", pos.sl_price, d)     # 保守
            if hit_sl:
                return ("SL", pos.sl_price, d)
            if hit_tp:
                return ("TP", pos.tp_price, d)
        else:  # bearish
            hit_sl = hi >= pos.sl_price
            hit_tp = lo <= pos.tp_price
            if hit_sl and hit_tp:
                return ("SL", pos.sl_price, d)
            if hit_sl:
                return ("SL", pos.sl_price, d)
            if hit_tp:
                return ("TP", pos.tp_price, d)

        # 时间止损：持仓到 T+10 日，无论方向强平
        if dt >= time_stop:
            return ("TIME", bar["Close"], d)

    return None


def _close_position(pos: Position, exit_reason: str, exit_price: float, exit_date: str) -> Tuple[ClosedTrade, float]:
    """平仓，返回 (ClosedTrade, pnl_usd)"""
    if pos.direction == "bullish":
        gross_pct = (exit_price - pos.entry_price) / pos.entry_price * 100.0
    else:
        gross_pct = (pos.entry_price - exit_price) / pos.entry_price * 100.0

    holding_days = (datetime.strptime(exit_date, "%Y-%m-%d") -
                    datetime.strptime(pos.entry_date, "%Y-%m-%d")).days
    holding_days = max(1, holding_days)

    # 成本：SL/TIME 触发时出场滑点额外加（默认 10bp/边；SL 触发加到 20bp/边 = 止损穿透溢价）
    # ⚠️ override_slippage_bps 传的是"单边 bp"，apply_costs 内部 ×2 变双边
    extra_slip = 20.0 if exit_reason == "SL" else None
    cost_res = apply_costs(gross_pct, pos.direction, pos.ticker, holding_days,
                           override_slippage_bps=extra_slip)
    net_pct = cost_res["net_return_pct"]
    pnl_usd = pos.size_usd * (net_pct / 100.0)

    trade = ClosedTrade(
        ticker=pos.ticker,
        direction=pos.direction,
        entry_date=pos.entry_date,
        entry_price=pos.entry_price,
        exit_date=exit_date,
        exit_price=round(exit_price, 4),
        holding_days=holding_days,
        shares=pos.shares,
        gross_return_pct=round(gross_pct, 4),
        net_return_pct=round(net_pct, 4),
        cost_pct=cost_res["cost_pct"],
        pnl_usd=round(pnl_usd, 2),
        exit_reason=exit_reason,
        confidence=pos.confidence,
        score=pos.score,
    )
    return trade, pnl_usd


def _mark_to_market(positions: List[Position], as_of: str) -> Tuple[float, List[Dict]]:
    """计算当前持仓 mark-to-market 未实现损益"""
    unrealized = 0.0
    details = []
    for pos in positions:
        ohlc = _fetch_ohlc(pos.ticker, pos.entry_date,
                            (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
        cur_price = pos.entry_price
        if as_of in ohlc:
            cur_price = ohlc[as_of]["Close"]
        elif ohlc:
            # 回退到最近一日
            latest = max(ohlc.keys())
            cur_price = ohlc[latest]["Close"]

        if pos.direction == "bullish":
            u_pct = (cur_price - pos.entry_price) / pos.entry_price * 100.0
        else:
            u_pct = (pos.entry_price - cur_price) / pos.entry_price * 100.0
        u_usd = pos.size_usd * (u_pct / 100.0)
        unrealized += u_usd
        details.append({
            "ticker": pos.ticker,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "current_price": round(cur_price, 2),
            "unreal_pct": round(u_pct, 2),
            "unreal_usd": round(u_usd, 2),
            "sl_price": pos.sl_price,
            "tp_price": pos.tp_price,
            "size_usd": pos.size_usd,
        })
    return unrealized, details


# ══════════════════════════════════════════════════════════════════════════════
# 主入口：run_daily（单日回放）/ bootstrap_from_history（首次启动）
# ══════════════════════════════════════════════════════════════════════════════

def _load_snapshots_for_date(date_str: str) -> List[Dict]:
    """读取指定日期所有 ticker snapshot"""
    out = []
    for f in SNAPSHOT_DIR.glob(f"*_{date_str}.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append(d)
        except Exception:
            continue
    return out


def _all_snapshot_dates() -> List[str]:
    dates = set()
    for f in SNAPSHOT_DIR.glob("*_*.json"):
        parts = f.stem.split("_")
        if len(parts) >= 2:
            dates.add(parts[-1])
    return sorted(dates)


def run_for_date(as_of: str, verbose: bool = False) -> Dict:
    """
    执行指定日期的 paper portfolio 操作：
    1. 扫描现有仓位 → 检查 SL/TP/TIME 触发 → 平仓
    2. 读取当日符合条件的报告 → 开新仓
    3. 更新 equity curve
    """
    meta = _load_meta()
    positions = [Position(**p) for p in _load_jsonl(POSITIONS_FILE)]
    closed = _load_jsonl(CLOSED_FILE)
    cash = float(meta["cash"])

    # ── Step 1: 检查现有仓位是否触发出场 ──
    remaining = []
    pnl_today = 0.0
    for pos in positions:
        # 拉包含 as_of 的 OHLC 段
        ohlc = _fetch_ohlc(pos.ticker, pos.entry_date,
                           (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
        exit_check = _check_exit(pos, as_of, ohlc)
        if exit_check:
            reason, ex_price, ex_date = exit_check
            trade, pnl = _close_position(pos, reason, ex_price, ex_date)
            _append_jsonl(CLOSED_FILE, trade.to_dict())
            cash += pos.size_usd + pnl   # 归还本金 + 净损益
            pnl_today += pnl
            closed.append(trade.to_dict())
            if verbose:
                print(f"  ← {reason} {pos.ticker} {pos.direction}  ${pos.entry_price:.2f}→${ex_price:.2f}  PnL ${pnl:+.2f}")
        else:
            remaining.append(pos)
    positions = remaining

    # ── Step 2: 开新仓 ──
    snapshots = _load_snapshots_for_date(as_of)
    existing_tix = {p.ticker for p in positions}
    nav_for_sizing = cash + sum(p.size_usd for p in positions)  # 简化：用 cost basis 做 NAV

    # 先按 score 排序，保证高分优先吃到资金
    snapshots.sort(key=lambda s: abs(float(s.get("composite_score") or 5) - 5), reverse=True)

    opened_count = 0
    for snap in snapshots:
        if len(positions) >= CONFIG["max_positions"]:
            break
        deployed = sum(p.size_usd for p in positions)
        if deployed / nav_for_sizing * 100 >= CONFIG["max_deployed_pct"]:
            break

        ok, reason = _should_open(snap, existing_tix, as_of=as_of)
        if not ok:
            continue

        ticker = snap["ticker"]
        ohlc = _fetch_ohlc(ticker, as_of,
                           (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d"))
        new_pos = _open_position(snap, nav_for_sizing, as_of, ohlc, closed)
        if new_pos is None:
            continue
        if new_pos.size_usd > cash:
            continue  # 现金不足
        positions.append(new_pos)
        existing_tix.add(ticker)
        cash -= new_pos.size_usd
        opened_count += 1
        if verbose:
            print(f"  → 开仓 {ticker} {new_pos.direction}  ${new_pos.entry_price:.2f}  size=${new_pos.size_usd:.0f} ({new_pos.confidence})")

    # ── Step 3: mark-to-market + 快照 ──
    unreal_usd, pos_details = _mark_to_market(positions, as_of)
    deployed_usd = sum(p.size_usd for p in positions)
    nav = cash + deployed_usd + unreal_usd

    equity_snapshot = {
        "date": as_of,
        "cash": round(cash, 2),
        "deployed": round(deployed_usd, 2),
        "unrealized": round(unreal_usd, 2),
        "nav": round(nav, 2),
        "positions_count": len(positions),
        "trades_closed_today": sum(1 for t in closed if t.get("exit_date") == as_of),
        "realized_pnl_today": round(pnl_today, 2),
    }

    # 去重：同一天多次运行时只保留最新快照
    existing_equity = _load_jsonl(EQUITY_FILE)
    existing_equity = [e for e in existing_equity if e.get("date") != as_of]
    existing_equity.append(equity_snapshot)
    existing_equity.sort(key=lambda x: x["date"])
    _write_jsonl(EQUITY_FILE, existing_equity)

    # 保存仓位快照 + meta
    _write_jsonl(POSITIONS_FILE, [p.to_dict() for p in positions])
    meta["cash"] = cash
    meta["last_run_date"] = as_of
    _save_meta(meta)

    return {
        "as_of": as_of,
        "nav": nav,
        "cash": cash,
        "positions": pos_details,
        "opened_today": opened_count,
        "realized_pnl_today": pnl_today,
        "equity_snapshot": equity_snapshot,
    }


def bootstrap_from_history(verbose: bool = False) -> None:
    """首次启动：从 bootstrap_date 开始逐日回放所有历史 snapshot"""
    meta = _load_meta()
    if meta.get("last_run_date"):
        if verbose:
            print(f"Bootstrap 已完成到 {meta['last_run_date']}，跳过")
        return

    all_dates = _all_snapshot_dates()
    start = CONFIG["bootstrap_date"]
    all_dates = [d for d in all_dates if d >= start]
    if not all_dates:
        print(f"⚠️ 找不到 {start} 之后的 snapshot，跳过 bootstrap")
        return

    print(f"🔁 Bootstrap PaperPortfolio: {all_dates[0]} → {all_dates[-1]} ({len(all_dates)} 个交易日)")
    for d in all_dates:
        res = run_for_date(d, verbose=verbose)
        if verbose:
            print(f"   {d}  NAV=${res['nav']:,.2f}  持仓={len(res['positions'])}  今日开仓={res['opened_today']}")


# ══════════════════════════════════════════════════════════════════════════════
# KPI 计算
# ══════════════════════════════════════════════════════════════════════════════

def compute_kpis(as_of: Optional[str] = None) -> Dict:
    eq = _load_jsonl(EQUITY_FILE)
    if not eq:
        return {"nav": CONFIG["starting_capital"], "total_return_pct": 0.0, "n_trades": 0}

    eq_sorted = sorted(eq, key=lambda x: x["date"])
    if as_of:
        eq_sorted = [e for e in eq_sorted if e["date"] <= as_of]
    if not eq_sorted:
        return {"nav": CONFIG["starting_capital"], "total_return_pct": 0.0, "n_trades": 0}

    latest = eq_sorted[-1]
    start_nav = CONFIG["starting_capital"]
    total_ret = (latest["nav"] - start_nav) / start_nav * 100

    # 日度回报序列
    daily_rets = []
    for i in range(1, len(eq_sorted)):
        prev = eq_sorted[i - 1]["nav"]
        cur = eq_sorted[i]["nav"]
        if prev > 0:
            daily_rets.append((cur - prev) / prev * 100.0)  # 百分比形式，匹配 sharpe_ratio 期望

    # Sharpe（按交易日 ~252/年）
    sharpe = sharpe_ratio(daily_rets, periods_per_year=252) if daily_rets else 0.0
    if sharpe is None:
        sharpe = 0.0

    # Max Drawdown
    peak = eq_sorted[0]["nav"]
    mdd = 0.0
    for e in eq_sorted:
        peak = max(peak, e["nav"])
        dd = (e["nav"] - peak) / peak * 100
        mdd = min(mdd, dd)

    # 胜率
    closed = _load_jsonl(CLOSED_FILE)
    if as_of:
        closed = [c for c in closed if c["exit_date"] <= as_of]
    wins = sum(1 for c in closed if c["pnl_usd"] > 0)
    total = len(closed)

    # SPY 基准对比
    spy_start_date = eq_sorted[0]["date"]
    spy_end_date = eq_sorted[-1]["date"]
    spy_ret = 0.0
    try:
        spy_ohlc = _fetch_ohlc("SPY", spy_start_date,
                                (datetime.strptime(spy_end_date, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
        if spy_ohlc:
            _dates = sorted(spy_ohlc.keys())
            spy_start = spy_ohlc[_dates[0]]["Close"]
            spy_end = spy_ohlc[min(spy_end_date, _dates[-1])]["Close"] if spy_end_date in spy_ohlc else spy_ohlc[_dates[-1]]["Close"]
            spy_ret = (spy_end - spy_start) / spy_start * 100
    except Exception:
        pass

    return {
        "nav": latest["nav"],
        "cash": latest["cash"],
        "deployed": latest["deployed"],
        "unrealized": latest["unrealized"],
        "total_return_pct": round(total_ret, 2),
        "spy_return_pct": round(spy_ret, 2),
        "alpha_pct": round(total_ret - spy_ret, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(mdd, 2),
        "win_rate_pct": round(wins / total * 100, 1) if total else 0.0,
        "trades_total": total,
        "trades_wins": wins,
        "positions_count": latest.get("positions_count", 0),
        "days_running": len(eq_sorted),
        "starting_date": eq_sorted[0]["date"],
        "latest_date": latest["date"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# HTML 渲染
# ══════════════════════════════════════════════════════════════════════════════

def _render_sparkline_svg(nav_series: List[Tuple[str, float]], width: int = 320, height: int = 60) -> str:
    if len(nav_series) < 2:
        return ""
    vals = [v for _, v in nav_series]
    lo, hi = min(vals), max(vals)
    rng = hi - lo if hi > lo else 1
    pts = []
    for i, (_, v) in enumerate(nav_series):
        x = i / (len(nav_series) - 1) * width
        y = height - ((v - lo) / rng) * height
        pts.append(f"{x:.1f},{y:.1f}")
    color = "#10b981" if vals[-1] >= vals[0] else "#ef4444"
    path = "M" + " L".join(pts)
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'style="display:block;margin:8px 0;">'
            f'<path d="{path}" stroke="{color}" stroke-width="2" fill="none"/>'
            f'</svg>')


def render_portfolio_card() -> str:
    """渲染 $50k PaperPortfolio 卡片 HTML（插入到 CH0 或顶层）"""
    kpi = compute_kpis()
    positions = _load_jsonl(POSITIONS_FILE)
    closed = _load_jsonl(CLOSED_FILE)
    eq = _load_jsonl(EQUITY_FILE)

    if not eq:
        return (
            '<div style="margin:16px 0;padding:16px;background:var(--bg2);border-radius:10px;'
            'border:1px solid var(--border1);color:var(--text3);">'
            '📊 PaperPortfolio 尚未启动。运行 <code>python3 paper_portfolio.py bootstrap</code> 初始化。'
            '</div>'
        )

    # Sparkline
    nav_series = [(e["date"], e["nav"]) for e in sorted(eq, key=lambda x: x["date"])]
    sparkline = _render_sparkline_svg(nav_series)

    # KPI 色
    ret_col = "#10b981" if kpi["total_return_pct"] >= 0 else "#ef4444"
    alpha_col = "#10b981" if kpi["alpha_pct"] >= 0 else "#ef4444"

    # 持仓表格
    pos_rows = ""
    if positions:
        pos_details = []
        for p in positions:
            p_obj = Position(**p)
            ohlc = _fetch_ohlc(p_obj.ticker, p_obj.entry_date,
                                (datetime.strptime(kpi["latest_date"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
            cur = p_obj.entry_price
            if kpi["latest_date"] in ohlc:
                cur = ohlc[kpi["latest_date"]]["Close"]
            elif ohlc:
                cur = ohlc[max(ohlc.keys())]["Close"]
            if p_obj.direction == "bullish":
                u_pct = (cur - p_obj.entry_price) / p_obj.entry_price * 100.0
            else:
                u_pct = (p_obj.entry_price - cur) / p_obj.entry_price * 100.0
            u_usd = p_obj.size_usd * (u_pct / 100.0)
            pos_details.append((p_obj, cur, u_pct, u_usd))

        for p_obj, cur, u_pct, u_usd in pos_details:
            pnl_col = "#10b981" if u_usd >= 0 else "#ef4444"
            dir_icon = "🟢" if p_obj.direction == "bullish" else "🔴"
            pos_rows += (
                f'<tr>'
                f'<td style="padding:4px 8px;font-weight:600;">{p_obj.ticker}</td>'
                f'<td style="padding:4px 8px;">{dir_icon} {"Long" if p_obj.direction == "bullish" else "Short"}</td>'
                f'<td style="padding:4px 8px;text-align:right;">${p_obj.entry_price:.2f}</td>'
                f'<td style="padding:4px 8px;text-align:right;">${cur:.2f}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:{pnl_col};font-weight:600;">'
                f'${u_usd:+.2f} ({u_pct:+.1f}%)</td>'
                f'<td style="padding:4px 8px;text-align:right;font-size:11px;color:var(--text3);">'
                f'${p_obj.sl_price:.2f} / ${p_obj.tp_price:.2f}</td>'
                f'<td style="padding:4px 8px;text-align:right;font-size:11px;color:var(--text3);">'
                f'{p_obj.entry_date}</td>'
                f'</tr>'
            )
    else:
        pos_rows = '<tr><td colspan="7" style="padding:10px;text-align:center;color:var(--text3);">当前无持仓</td></tr>'

    # 近 5 笔平仓
    closed_sorted = sorted(closed, key=lambda x: x["exit_date"], reverse=True)[:5]
    closed_rows = ""
    for t in closed_sorted:
        pnl_col = "#10b981" if t["pnl_usd"] >= 0 else "#ef4444"
        mark = "✅" if t["pnl_usd"] > 0 else ("❌" if t["pnl_usd"] < 0 else "➖")
        reason_label = {"TP": "止盈", "SL": "止损", "TIME": "T+10 强平"}.get(t["exit_reason"], t["exit_reason"])
        closed_rows += (
            f'<div style="padding:4px 0;font-size:12px;color:var(--text2);">'
            f'{mark} <strong>{t["ticker"]}</strong> '
            f'{"Long" if t["direction"] == "bullish" else "Short"} · {reason_label} · '
            f'${t["entry_price"]:.2f} → ${t["exit_price"]:.2f} · '
            f'<span style="color:{pnl_col};font-weight:600;">${t["pnl_usd"]:+.2f}</span> '
            f'({t["net_return_pct"]:+.1f}%) · '
            f'{t["holding_days"]}d'
            f'</div>'
        )

    deployed_pct = kpi["deployed"] / kpi["nav"] * 100 if kpi["nav"] > 0 else 0
    _closed_block = closed_rows if closed_rows else '<div style="font-size:12px;color:var(--text3);">暂无平仓记录</div>'

    return (
        '<div style="margin:16px 0;padding:20px;background:linear-gradient(135deg,rgba(34,197,94,0.06),rgba(16,185,129,0.03));'
        'border:1px solid rgba(34,197,94,0.3);border-radius:12px;">'

        # 标题行
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">'
        '<div style="font-size:14px;font-weight:700;color:#22c55e;letter-spacing:.05em;">'
        '📊 $50,000 策略模拟组合 · 透明账户</div>'
        f'<div style="font-size:10px;color:var(--text3);font-style:italic;">'
        f'自 {kpi["starting_date"]} · {kpi["days_running"]} 天 · v0.19.0</div>'
        '</div>'

        # 核心 KPI
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px;">'
        f'<div><div style="font-size:11px;color:var(--text3);">组合净值</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">${kpi["nav"]:,.2f}</div>'
        f'<div style="font-size:12px;color:{ret_col};font-weight:600;">{kpi["total_return_pct"]:+.2f}%</div></div>'

        f'<div><div style="font-size:11px;color:var(--text3);">vs SPY</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">{kpi["spy_return_pct"]:+.2f}%</div>'
        f'<div style="font-size:12px;color:{alpha_col};font-weight:600;">Alpha {kpi["alpha_pct"]:+.2f}%</div></div>'

        f'<div><div style="font-size:11px;color:var(--text3);">Sharpe / MDD</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">{kpi["sharpe"]:.2f}</div>'
        f'<div style="font-size:12px;color:#ef4444;font-weight:600;">MDD {kpi["max_drawdown_pct"]:.1f}%</div></div>'

        f'<div><div style="font-size:11px;color:var(--text3);">胜率 / 仓位</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">{kpi["win_rate_pct"]:.1f}%</div>'
        f'<div style="font-size:12px;color:var(--text3);">'
        f'{kpi["trades_wins"]}/{kpi["trades_total"]} · 持 {kpi["positions_count"]}/{CONFIG["max_positions"]}</div></div>'
        '</div>'

        # Equity curve
        f'{sparkline}'

        # 资金利用
        f'<div style="font-size:11px;color:var(--text3);margin-bottom:12px;">'
        f'💰 现金 ${kpi["cash"]:,.2f} · 已部署 ${kpi["deployed"]:,.2f} ({deployed_pct:.1f}% NAV) · '
        f'未实现 ${kpi["unrealized"]:+,.2f}</div>'

        # 当前持仓
        '<div style="margin-bottom:12px;">'
        '<div style="font-size:11px;font-weight:700;color:var(--text3);margin-bottom:4px;">当前持仓</div>'
        '<table style="width:100%;font-size:12px;border-collapse:collapse;">'
        '<thead><tr style="border-bottom:1px solid var(--border2);color:var(--text3);">'
        '<th style="padding:4px 8px;text-align:left;">标的</th>'
        '<th style="padding:4px 8px;text-align:left;">方向</th>'
        '<th style="padding:4px 8px;text-align:right;">入场</th>'
        '<th style="padding:4px 8px;text-align:right;">当前</th>'
        '<th style="padding:4px 8px;text-align:right;">未实现</th>'
        '<th style="padding:4px 8px;text-align:right;">SL / TP</th>'
        '<th style="padding:4px 8px;text-align:right;">开仓日</th>'
        '</tr></thead>'
        f'<tbody>{pos_rows}</tbody></table>'
        '</div>'

        # 近 5 笔平仓
        '<div style="margin-bottom:8px;">'
        '<div style="font-size:11px;font-weight:700;color:var(--text3);margin-bottom:4px;">近 5 笔平仓</div>'
        f'{_closed_block}'
        '</div>'

        # 规则说明
        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border2);'
        'font-size:10px;color:var(--text3);line-height:1.6;">'
        f'⚙️ 规则：高置信 {CONFIG["size_pct_by_tier"]["high"]}% NAV / 中置信 {CONFIG["size_pct_by_tier"]["mid"]}% · '
        f'−{CONFIG["sl_pct"]}% SL / +{CONFIG["tp_pct"]}% TP / T+{CONFIG["time_stop_days"]} 强平 · '
        f'最大并行 {CONFIG["max_positions"]} 仓位 · 最大部署 {CONFIG["max_deployed_pct"]}% NAV<br>'
        '📎 含 trading_costs.py 成本（滑点 + 佣金 + 借券费）· 股票现货模拟 · 不含期权'
        '</div>'
        '</div>'
    )


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser(description="Alpha Hive PaperPortfolio")
    p.add_argument("cmd", choices=["bootstrap", "run", "kpi", "card", "reset"],
                   help="bootstrap=回放历史 / run=跑单日 / kpi=打印KPI / card=渲染HTML / reset=清空")
    p.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if args.cmd == "reset":
        for f in [POSITIONS_FILE, CLOSED_FILE, EQUITY_FILE, META_FILE]:
            if f.exists():
                f.unlink()
        print("🗑  已清空 PaperPortfolio 状态")
        return

    if args.cmd == "bootstrap":
        bootstrap_from_history(verbose=args.verbose)
        kpi = compute_kpis()
        print(f"\n✅ Bootstrap 完成")
        print(f"   NAV:    ${kpi['nav']:,.2f}  ({kpi['total_return_pct']:+.2f}%)")
        print(f"   vs SPY: {kpi['spy_return_pct']:+.2f}%  Alpha: {kpi['alpha_pct']:+.2f}%")
        print(f"   胜率:   {kpi['win_rate_pct']:.1f}% ({kpi['trades_wins']}/{kpi['trades_total']})")
        print(f"   Sharpe: {kpi['sharpe']:.2f}  MDD: {kpi['max_drawdown_pct']:.1f}%")
        return

    if args.cmd == "run":
        as_of = args.date or datetime.now().strftime("%Y-%m-%d")
        res = run_for_date(as_of, verbose=args.verbose)
        print(f"\n📊 {as_of}  NAV=${res['nav']:,.2f}  持仓={len(res['positions'])}  "
              f"今日开仓={res['opened_today']}  已实现P&L=${res['realized_pnl_today']:+.2f}")
        return

    if args.cmd == "kpi":
        kpi = compute_kpis(args.date)
        print(json.dumps(kpi, ensure_ascii=False, indent=2))
        return

    if args.cmd == "card":
        html = render_portfolio_card()
        out = BASE_DIR / "paper_portfolio_card.html"
        out.write_text(f'<!DOCTYPE html><html><head><meta charset="utf-8">'
                       f'<style>:root{{--bg2:#1a1d2e;--bg3:#252840;--border1:#2e3348;--border2:#3a4055;'
                       f'--text1:#e2e8f0;--text2:#94a3b8;--text3:#64748b;--green2:#10b981;--red2:#ef4444;--gold2:#f59e0b;}}'
                       f'body{{background:#0f1119;color:#e2e8f0;font-family:system-ui,sans-serif;padding:20px;}}</style>'
                       f'</head><body>{html}</body></html>', encoding="utf-8")
        print(f"✅ 已渲染 → {out}")
        return


if __name__ == "__main__":
    main()
