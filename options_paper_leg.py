#!/usr/bin/env python3
"""
期权纸面腿：财报跨式账本（v0.45.101，期权路线图第 3 步）
========================================================
做什么
------
一本与股票纸面组合（paper_portfolio）**完全分开**的账本，只交易一种东西：
跨过财报日的 ATM 跨式。信号来自 earnings_vol_signal：
    rich  → 卖跨式（short）      cheap → 买跨式（long）      fair / untradeable → 不动
目的是**测量**「隐含事件波动 vs 历史实际波动」这个信号在真实报价、真实点差下
值不值钱，不是给任何人下单用的。

成交约定（诚实，付点差）
------------------------
    入场：long 两腿都按 **ask** 买入；short 两腿都按 **bid** 卖出
    出场：镜像——long 按 bid 卖出；short 按 ask 买回
    盯市：mid
用 mid 当成交价等于假装自己是做市商。点差是这个策略最大的确定性成本，
不付它测出来的收益是假的。

仓位
----
    risk_budget = NAV × risk_per_trade_pct / 100
    contracts   = max(1, floor(risk_budget / (premium × 100)))
    一张合约就超预算 → 跳过并记录原因（不会为了"总得开一张"突破风险预算）
两侧都由同一个 floor 封顶（floor(budget/per_contract) 本身就保证 per_contract×n ≤
budget，不需要额外的循环）。short 侧用权利金当上限只是因为收到的权利金是
**保证金的代理**，卖跨式的真实风险无上界（标的跳 30% 时亏的是数倍权利金）。
这本账用它只是为了让 long/short 两侧的名义规模可比，**不是**实盘仓位规则。

盯市与出场
----------
逐日用 `cboe_options.quote_contracts` 按 OCC 符号重新报价两腿：
    两腿 quote_ok           → mark = mid 之和，mark_source="cboe_mid"，stale_days=0
    否则                    → 沿用 last_mark，mark_source="stale"，
                              stale_days = as_of − last_mark_date 的**真实日历天数**
出场触发：
    as_of > earnings_date（exit_after_event） → post_event
    expiry − as_of ≤ expiry_buffer_days       → expiry_buffer
    到期后 write_off_days_after_expiry 天仍拿不到任何价格 → written_off（核销）
出场价：报价 ok → 镜像成交价；报价连续 stale 超过 fallback_stale_max_days（或已到期）
→ 用 **min(as_of, expiry) 那天**的标的收盘算内在价值 |S − K|，mark_source="intrinsic"。
必须取到期日（或更早）的收盘：合约到期后标的还在走，拿今天的收盘算等于让一张早就
作废的合约继续跟涨（K=100、到期日 S=101 的真实结算是 $1，两个半月后按 S=160 会算成 $60）。
取不到收盘就继续持有并告警——**绝不**编一个价把仓位关掉；但也不能让它永远挂着：
到期后 `quote_contracts` 对不在链里的符号**永远**返回 None（`to_expiry ≤ 0` 是永久状态
不是暂时状态），所以"等报价回来"是等不到的。过了核销期限仍无任何价格 → 按最后已知
mark 平账、mark_source="written_off"、error 级告警点名——账本不能挂着一个拿入场日
冻结价计进 NAV 的幽灵仓。stale_days 也因此必须是真实日历天数而不是运行次数计数器：
漏跑一年的计数器只会加十来次，读出来像"才 stale 10 天"。

诚实降级
--------
- 报价里任何 NaN/Inf 进不了状态：入口 `_num()` 过滤，落盘前 `_scrub()` 再扫一遍。
  `bool(nan) is True`、`nan <= 0` 是 False，比较守卫全挡不住，paper_portfolio
  在 v0.45.97 为此烂了四天净值。
- NAV 非有限 → 当日不开仓并 error（不把 NaN 传给新仓）。
- 同日重跑幂等：净值按日期去重、已有该票持仓不重复开、stale 计数按日只加一次。

没做的事（已知局限）
--------------------
- **没有 delta 对冲**：ATM 跨式入场时近似 delta 中性，之后标的一漂就带方向。
  v1 不处理，把它当成本策略噪音的一部分记录下来。
- 不分腿平仓、不滚仓、不做 IV crush 的逐腿归因。
- 没有保证金利息、没有指派/行权模拟（到期前 2 天就关掉了）。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from hive_logger import PATHS, get_logger

_log = get_logger("options_paper_leg")

CONFIG = {
    # 规模校准（2026-09-03）：NVDA $224 / rv 45% 的 30 DTE ATM 跨式 ≈ $28/股 = $2,800/张，
    # COST ≈ $53/股 = $5,300/张。20k×2%=$400 的预算连一张都开不出，账本会永远是空的。
    # 这是测量账本，规模只为让主流名单的一张合约进得来：100k × 6% = $6,000。
    "starting_capital": 100_000.0,
    "risk_per_trade_pct": 6.0,        # 每笔权利金（long 最大亏损 / short 名义）占 NAV 上限
    "max_open": 6,
    "trade_rich": True,               # rich → 卖跨式
    "trade_cheap": True,              # cheap → 买跨式
    "exit_after_event": True,         # 财报过后第一个有报价的日子平仓
    "expiry_buffer_days": 2,          # 到期前 2 天强平（避开到期日 gamma 与指派）
    "fallback_stale_max_days": 3,     # 事件后报价连续 stale 超过 3 天 → 内在价值平仓
    # 到期这么多天后仍拿不到任何价格（报价没了、到期日收盘也取不到）→ 按最后已知
    # mark 核销。30 天足够覆盖一次长假 + 数据源短期故障；再长就是幽灵仓了。
    "write_off_days_after_expiry": 30,
}

BASE_DIR = PATHS.home
STATE_DIR = BASE_DIR / "options_paper_state"
POSITIONS_FILE = STATE_DIR / "positions.jsonl"
CLOSED_FILE = STATE_DIR / "closed_trades.jsonl"
EQUITY_FILE = STATE_DIR / "equity_curve.jsonl"
META_FILE = STATE_DIR / "meta.json"

_VERSION = "0.45.101"


# ══════════════════════════════════════════════════════════════════════════════
# 数值守卫
# ══════════════════════════════════════════════════════════════════════════════

def _num(v) -> Optional[float]:
    """float 且有限 → float；否则 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> Optional[float]:
    """有限且 > 0 → float；否则 None。"""
    f = _num(v)
    return f if f is not None and f > 0 else None


def _scrub(obj, _path: str = ""):
    """落盘前最后一道闸：任何非有限 float 变 None 并 error 日志。"""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _log.error("[OptionsPaperLeg] 非有限值被拦在落盘前：%s=%r → None", _path, obj)
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, f"{_path}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, f"{_path}[{i}]") for i, v in enumerate(obj)]
    return obj


def _days_between(d1: str, d2: str) -> Optional[int]:
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StraddlePosition:
    ticker: str
    side: str                       # "long" | "short"
    entry_date: str
    expiry: str
    strike: float
    call_symbol: str
    put_symbol: str
    contracts: int
    entry_call: float               # 成交价（long=ask / short=bid）
    entry_put: float
    entry_premium: float            # 每股 = call + put
    entry_underlying: Optional[float]
    earnings_date: Optional[str]
    signal_ratio: Optional[float]
    label: Optional[str]
    size_usd: float                 # premium × 100 × contracts
    last_mark: float                # 每股 mid 之和
    last_mark_date: str
    mark_source: str                # cboe_mid | stale | intrinsic
    rationale: str = ""
    # stale_days = as_of − last_mark_date 的日历天数（H3a 起不再是运行次数计数器）。
    # 一并删掉了 last_quote_date：它唯一的用途是"同日重跑不重复加 stale"，而日历
    # 天数天然幂等，用不着它了；留着就是一个只写不读的死字段（本项目专门有这条教训），
    # 而"上次跑到哪天"meta.last_run_date 已经记了。
    stale_days: int = 0

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_record(cls, d: Dict) -> "StraddlePosition":
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class ClosedStraddle:
    ticker: str
    side: str
    entry_date: str
    exit_date: str
    expiry: str
    strike: float
    call_symbol: str
    put_symbol: str
    contracts: int
    entry_call: float
    entry_put: float
    entry_premium: float
    exit_call: Optional[float]
    exit_put: Optional[float]
    exit_premium: float
    entry_underlying: Optional[float]
    exit_underlying: Optional[float]
    earnings_date: Optional[str]
    signal_ratio: Optional[float]
    label: Optional[str]
    size_usd: float
    pnl_usd: float
    pnl_pct: float
    exit_reason: str                # post_event | expiry_buffer | written_off | manual
    mark_source: str                # cboe_mid | intrinsic | written_off
    holding_days: Optional[int]     # 已按 expiry 钳过：一张合约不可能被持有到它到期之后
    rationale: str = ""
    settled_late_days: Optional[int] = None   # 平仓日晚于到期日的天数（正常为 None）

    def to_dict(self) -> Dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════════════
# 状态文件（与 paper_portfolio 同形，路径为模块全局，测试可重定向）
# ══════════════════════════════════════════════════════════════════════════════

def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_jsonl(path: Path, records: List[Dict]) -> None:
    _atomic_write_text(path, "".join(json.dumps(_scrub(r), ensure_ascii=False) + "\n" for r in records))


def _append_jsonl(path: Path, record: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_scrub(record), ensure_ascii=False) + "\n")


def _load_meta() -> Dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except ValueError:
            _log.error("[OptionsPaperLeg] meta.json 损坏，按新账本处理")
    return {
        "version": _VERSION,
        "starting_capital": CONFIG["starting_capital"],
        "starting_date": None,
        "cash": CONFIG["starting_capital"],
        "last_run_date": None,
        "config_snapshot": dict(CONFIG),
        "skipped_entries": [],
    }


def _save_meta(meta: Dict) -> None:
    _atomic_write_text(META_FILE, json.dumps(_scrub(meta), ensure_ascii=False, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# 默认数据源（测试一律注入）
# ══════════════════════════════════════════════════════════════════════════════

def _default_quotes(ticker: str, symbols: List[str]) -> Dict[str, Optional[dict]]:
    from cboe_options import quote_contracts
    return quote_contracts(ticker, symbols)


def _default_close(ticker: str, as_of: str) -> Optional[float]:
    """as_of 当日（或最近 5 个日历日内最后一根）收盘；取不到 None。"""
    def _pick(rows: List[Tuple[str, float]]) -> Optional[float]:
        best = None
        for d, c in rows:
            if d <= as_of and (best is None or d > best[0]):
                best = (d, c)
        if best is None:
            return None
        gap = _days_between(best[0], as_of)
        if gap is None or gap > 5:
            return None
        return _pos(best[1])

    try:
        import twelve_data
        # v0.45.105：走共享缓存入口，并且**要 120 根而不是 10 根**。
        # 本模块是日报里最先跑的那个消费方；只要 10 根的话，随后 vrp_signal /
        # portfolio_greeks 各要 120 根，缓存里那条 10 根的条目不够用，会各自
        # 重取一次——三次调用只省下零次。窗口要大不多花配额（同一次请求的
        # outputsize），下面 `_pick` 取的是「≤ as_of 的最后一根」，
        # 多给几十根旧数据一个字都不影响它的答案。
        if twelve_data.is_configured():
            rows = twelve_data.fetch_bars(
                ticker, twelve_data.SHARED_BARS_WINDOW, end_date=as_of)
            if rows:
                px = _pick([(str(r.get("date"))[:10], r.get("close")) for r in rows])
                if px is not None:
                    return px
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 收盘获取失败: %s", ticker, exc)
    try:
        from price_history import load_price_history
        hist = load_price_history(ticker, str(PATHS.cache_dir))
        return _pick(list(hist)) if hist else None
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] 本地价格索引读取失败: %s", ticker, exc)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# 仓位 / 成交
# ══════════════════════════════════════════════════════════════════════════════

def size_contracts(side: str, premium: Optional[float], nav: Optional[float]) -> Tuple[int, Optional[str]]:
    """合约张数；0 张时给原因。long/short 同一套封顶：floor(budget/per_contract)。"""
    premium = _pos(premium)
    nav = _pos(nav)
    if premium is None:
        return 0, "premium unavailable"
    if nav is None:
        return 0, "nav unavailable"
    budget = nav * CONFIG["risk_per_trade_pct"] / 100.0
    per_contract = premium * 100.0
    if per_contract > budget:
        return 0, f"1 contract ${per_contract:,.0f} exceeds risk budget ${budget:,.0f}"
    # L1（v0.45.104）：这里原本还有一段 `if side == "short": while ... n -= 1` 的"封顶循环"。
    # 上面 `per_contract > budget` 已经早退，floor(budget/per_contract) 本身就保证
    # per_contract × n ≤ budget——循环体在 180 万次随机预算内输入里一次都没执行过。
    # 一个永远不可能触发、却在文档里被当成承重墙介绍的守卫，删掉比留着安全。
    n = max(1, int(math.floor(budget / per_contract)))
    return n, None


def entry_fills(side: str, call: Optional[dict], put: Optional[dict]) -> Tuple[Optional[float], Optional[float]]:
    """long 按 ask 买入两腿；short 按 bid 卖出两腿。任一缺失 → (None, None)。"""
    if not isinstance(call, dict) or not isinstance(put, dict):
        return None, None
    key = "ask" if side == "long" else "bid"
    c, p = _pos(call.get(key)), _pos(put.get(key))
    if c is None or p is None:
        return None, None
    return c, p


def exit_fills(side: str, call: Optional[dict], put: Optional[dict]) -> Tuple[Optional[float], Optional[float]]:
    """镜像：long 按 bid 卖出；short 按 ask 买回。"""
    if not isinstance(call, dict) or not isinstance(put, dict):
        return None, None
    key = "bid" if side == "long" else "ask"
    c, p = _pos(call.get(key)), _pos(put.get(key))
    if c is None or p is None:
        return None, None
    return c, p


def intrinsic_value(strike: Optional[float], underlying: Optional[float]) -> Optional[float]:
    """同一行权价的 call+put 内在价值 = |S − K|。"""
    k, s = _pos(strike), _pos(underlying)
    if k is None or s is None:
        return None
    return abs(s - k)


def _quote_ok(q: Optional[dict]) -> bool:
    return (isinstance(q, dict) and bool(q.get("quote_ok"))
            and _pos(q.get("mid")) is not None
            and _pos(q.get("bid")) is not None and _pos(q.get("ask")) is not None)


def _mark_value(pos: StraddlePosition) -> float:
    """持仓对 NAV 的贡献：long 为 +市值、short 为 −市值。"""
    v = pos.last_mark * 100.0 * pos.contracts
    return v if pos.side == "long" else -v


def _unrealized(pos: StraddlePosition) -> float:
    d = pos.last_mark - pos.entry_premium
    if pos.side == "short":
        d = -d
    return d * 100.0 * pos.contracts


def _open_from_signal(sig: Dict, nav: float, as_of: str) -> Tuple[Optional[StraddlePosition], Optional[str]]:
    label = sig.get("label")
    if label == "rich":
        side = "short"
    elif label == "cheap":
        side = "long"
    else:
        return None, f"label {label!r} not tradeable"
    q = sig.get("quote") or {}
    call, put = q.get("call"), q.get("put")
    ec, ep = entry_fills(side, call, put)
    if ec is None:
        return None, "fill price unavailable"
    premium = ec + ep
    n, why = size_contracts(side, premium, nav)
    if n < 1:
        return None, why
    strike = _pos(call.get("strike")) if isinstance(call, dict) else None
    expiry = (call or {}).get("expiry") or sig.get("selected_expiry")
    if strike is None or not expiry or not (call or {}).get("symbol") or not (put or {}).get("symbol"):
        return None, "contract identity incomplete"
    mid_c, mid_p = _pos(call.get("mid")), _pos(put.get("mid"))
    mark = (mid_c + mid_p) if (mid_c is not None and mid_p is not None) else premium
    ratio = _num(sig.get("ratio"))
    basis = sig.get("event_move_basis")
    rationale = (f"{label}: implied event {sig.get('implied_event_move_pct')}% vs hist median "
                 f"{sig.get('hist_median_abs_move_pct')}% (n={sig.get('hist_n')}), ratio={ratio}, "
                 f"basis={basis}, spread≤{sig.get('max_leg_spread_pct')}, earnings {sig.get('earnings_date')}")
    return StraddlePosition(
        ticker=sig["ticker"], side=side, entry_date=as_of, expiry=expiry, strike=strike,
        call_symbol=call["symbol"], put_symbol=put["symbol"], contracts=n,
        entry_call=round(ec, 4), entry_put=round(ep, 4), entry_premium=round(premium, 4),
        entry_underlying=_pos(sig.get("underlying_price")),
        earnings_date=sig.get("earnings_date"), signal_ratio=ratio, label=label,
        size_usd=round(premium * 100.0 * n, 2),
        last_mark=round(mark, 4), last_mark_date=as_of, mark_source="cboe_mid",
        rationale=rationale, stale_days=0,
    ), None


def _close(pos: StraddlePosition, exit_call: Optional[float], exit_put: Optional[float],
           exit_premium: float, as_of: str, reason: str, mark_source: str,
           exit_underlying: Optional[float], note: str = "") -> Tuple[ClosedStraddle, float]:
    """返回 (记录, cash 变动)。long 平仓收回权利金；short 平仓付出权利金。"""
    n = pos.contracts
    gross = exit_premium * 100.0 * n
    cash_delta = gross if pos.side == "long" else -gross
    pnl = (exit_premium - pos.entry_premium) * 100.0 * n
    if pos.side == "short":
        pnl = -pnl
    pnl_pct = pnl / pos.size_usd * 100.0 if pos.size_usd > 0 else 0.0
    # H2 不变式（v0.45.104，免费的一道交叉校验）：一张合约不可能被持有到超过自己的
    # 到期日，所以 holding_days 上限就是 expiry − entry_date。多出来的天数是"账本晚了
    # 几天才了结"，单列到 settled_late_days，不许塞进 holding_days 里冒充持有期。
    raw_days = _days_between(pos.entry_date, as_of)
    max_days = _days_between(pos.entry_date, pos.expiry)
    hold, late = raw_days, None
    if raw_days is not None and max_days is not None and raw_days > max_days:
        hold, late = max_days, raw_days - max_days
        _log.error("[OptionsPaperLeg] %s 平仓日 %s 比到期日 %s 晚 %d 天："
                   "holding_days 钳到 %d，超出部分记 settled_late_days",
                   pos.ticker, as_of, pos.expiry, late, max_days)
    rec = ClosedStraddle(
        ticker=pos.ticker, side=pos.side, entry_date=pos.entry_date, exit_date=as_of,
        expiry=pos.expiry, strike=pos.strike, call_symbol=pos.call_symbol, put_symbol=pos.put_symbol,
        contracts=n, entry_call=pos.entry_call, entry_put=pos.entry_put, entry_premium=pos.entry_premium,
        exit_call=round(exit_call, 4) if exit_call is not None else None,
        exit_put=round(exit_put, 4) if exit_put is not None else None,
        exit_premium=round(exit_premium, 4),
        entry_underlying=pos.entry_underlying, exit_underlying=exit_underlying,
        earnings_date=pos.earnings_date, signal_ratio=pos.signal_ratio, label=pos.label,
        size_usd=pos.size_usd, pnl_usd=round(pnl, 2), pnl_pct=round(pnl_pct, 4),
        exit_reason=reason, mark_source=mark_source,
        holding_days=hold, settled_late_days=late,
        rationale=(pos.rationale + (" | " + note if note else "")),
    )
    return rec, cash_delta


# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════

def run_for_date(as_of: str,
                 quotes_fn: Optional[Callable[[str, List[str]], Dict[str, Optional[dict]]]] = None,
                 signals: Optional[List[Dict]] = None,
                 closes_fn: Optional[Callable[[str, str], Optional[float]]] = None) -> Dict:
    """(a) 盯市 → (b) 出场 → (c) 从信号开仓 → (d) 净值快照。同日重跑幂等。"""
    quotes_fn = quotes_fn or _default_quotes
    closes_fn = closes_fn or _default_close
    meta = _load_meta()
    positions = [StraddlePosition.from_record(p) for p in _load_jsonl(POSITIONS_FILE)]
    closed = _load_jsonl(CLOSED_FILE)
    cash = _num(meta.get("cash"))
    if cash is None:
        _log.error("[OptionsPaperLeg] meta.cash 非有限（%r）——本日不动账，只写告警", meta.get("cash"))
        return {"as_of": as_of, "error": "cash not finite"}
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of

    # ── (a)+(b) 盯市与出场 ──
    remaining: List[StraddlePosition] = []
    closed_today: List[Dict] = []
    for pos in positions:
        try:
            quotes = quotes_fn(pos.ticker, [pos.call_symbol, pos.put_symbol]) or {}
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 持仓重新报价失败: %s", pos.ticker, exc)
            quotes = {}
        qc, qp = quotes.get(pos.call_symbol), quotes.get(pos.put_symbol)
        ok = _quote_ok(qc) and _quote_ok(qp)
        if ok:
            pos.last_mark = round(qc["mid"] + qp["mid"], 4)
            pos.last_mark_date = as_of
            pos.mark_source = "cboe_mid"
            pos.stale_days = 0
        elif pos.last_mark_date == as_of:
            # 今天已经有一个好 mark（入场日的 mid，或同日早先一次成功盯市）：
            # 重跑时报价拿不到不算 stale，否则同日重跑会把状态改掉（幂等性）。
            pass
        else:
            # H3(a)（v0.45.104）：stale_days 以前是**运行次数**计数器（每跑一次 +1）。
            # 漏跑（周末、机器没开、任务失败）不会计数，于是一个到期一年的幽灵仓
            # 读出来是"stale 10 天"，看着像暂时性抖动。改成距最后一次成功盯市的
            # 真实日历天数：同日重跑天然幂等，不需要再靠 last_quote_date 去重
            # （那个字段因此也删了，见 StraddlePosition）。
            pos.stale_days = _days_between(pos.last_mark_date, as_of) or 0
            pos.mark_source = "stale"
            _log.warning("[OptionsPaperLeg] %s %s 两腿报价不可用（距上次成功盯市 %d 天），沿用 %s 的 mark %.4f",
                         pos.ticker, pos.side, pos.stale_days, pos.last_mark_date, pos.last_mark)

        post_event = bool(CONFIG["exit_after_event"] and pos.earnings_date and as_of > pos.earnings_date)
        to_expiry = _days_between(as_of, pos.expiry)
        near_expiry = to_expiry is not None and to_expiry <= CONFIG["expiry_buffer_days"]
        if not (post_event or near_expiry):
            remaining.append(pos)
            continue
        reason = "post_event" if post_event else "expiry_buffer"

        rec = None
        if ok:
            xc, xp = exit_fills(pos.side, qc, qp)
            if xc is not None:
                rec, delta = _close(pos, xc, xp, xc + xp, as_of, reason, "cboe_mid", None)
        if rec is None:
            expired = to_expiry is not None and to_expiry <= 0
            if pos.stale_days > CONFIG["fallback_stale_max_days"] or expired:
                # H2（v0.45.104）：内在价值必须用**到期日（或更早的 as_of）**那天的收盘。
                # 以前直接用 as_of 的收盘，等于让一张早就作废的合约继续跟着标的涨：
                # K=100、2026-10-02 到期、到期日 S=101（真实结算 $1.00/股），
                # 12-20 才结算时 S=160 → 记成 $60.00/股，一笔 $220 的仓位凭空 +$5,780。
                settle_date = min(as_of, pos.expiry) if pos.expiry else as_of
                try:
                    S = _pos(closes_fn(pos.ticker, settle_date))
                except Exception as exc:  # noqa: BLE001
                    _log.warning("[%s] 收盘价获取失败: %s", pos.ticker, exc)
                    S = None
                iv = intrinsic_value(pos.strike, S)
                past_expiry = _days_between(pos.expiry, as_of) if pos.expiry else None
                if iv is not None:
                    note = (f"INTRINSIC fallback: quotes stale {pos.stale_days}d"
                            f"{' / expired' if expired else ''}, settle_date={settle_date}, "
                            f"|S−K|=|{S}−{pos.strike}|")
                    rec, delta = _close(pos, None, None, iv, as_of, reason, "intrinsic", S, note)
                elif past_expiry is not None and past_expiry > CONFIG["write_off_days_after_expiry"]:
                    # H3(b)（v0.45.104）：到期后 cboe_options.quote_contracts 对不在链里的
                    # 符号**永远**返回 None——`to_expiry ≤ 0` 是永久状态，不是暂时状态。
                    # 所以"等报价回来再平"是等不到的：不核销就永远挂着，而它冻结的入场日
                    # mark 还在 NAV 里当活价用。按最后已知 mark 平账并 error 级点名。
                    note = (f"WRITTEN OFF: {past_expiry}d past expiry {pos.expiry}, no quote and "
                            f"no close at/before expiry; P&L from last known mark "
                            f"{pos.last_mark} @ {pos.last_mark_date} (NOT a traded price)")
                    _log.error("[OptionsPaperLeg] ⚠️ 核销幽灵仓 %s %s ×%d K=%s exp=%s：到期 %d 天"
                               "仍取不到任何价格，按最后已知 mark %.4f（%s）平账，非成交价",
                               pos.ticker, pos.side, pos.contracts, pos.strike, pos.expiry,
                               past_expiry, pos.last_mark, pos.last_mark_date)
                    rec, delta = _close(pos, None, None, pos.last_mark, as_of, "written_off",
                                        "written_off", None, note)
                else:
                    _log.warning("[OptionsPaperLeg] %s 该平仓（%s）但既无报价也无收盘价——继续持有",
                                 pos.ticker, reason)
            else:
                _log.info("[OptionsPaperLeg] %s 该平仓（%s）但报价不可用，等待（stale %d/%d）",
                          pos.ticker, reason, pos.stale_days, CONFIG["fallback_stale_max_days"])
        if rec is None:
            remaining.append(pos)
            continue
        if not math.isfinite(delta) or not math.isfinite(rec.pnl_usd):
            _log.error("[OptionsPaperLeg] %s 平仓算出非有限值（delta=%r pnl=%r），拒绝入账、继续持有",
                       pos.ticker, delta, rec.pnl_usd)
            remaining.append(pos)
            continue
        cash += delta
        d = rec.to_dict()
        _append_jsonl(CLOSED_FILE, d)
        closed.append(d)
        closed_today.append(d)
        _log.info("[OptionsPaperLeg] ← %s %s %s  %.2f→%.2f ×%d  PnL $%+.2f (%s)",
                  reason, pos.ticker, pos.side, pos.entry_premium, rec.exit_premium,
                  pos.contracts, rec.pnl_usd, rec.mark_source)
    positions = remaining

    # ── (c) 开仓 ──
    if signals is None:
        try:
            import earnings_vol_signal as _evs
            signals = _evs.signals_for_date(as_of) or _evs.scan(as_of)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[OptionsPaperLeg] 信号读取失败，本日不开仓: %s", exc)
            signals = []
    nav_for_sizing = cash + sum(_mark_value(p) for p in positions)
    opened: List[Dict] = []
    skipped: List[Dict] = []
    if not math.isfinite(nav_for_sizing):
        _log.error("[OptionsPaperLeg] %s NAV 非有限（cash=%r，持仓 %d 条）——本日不开新仓",
                   as_of, cash, len(positions))
        signals = []
    open_tix = {p.ticker for p in positions}
    cands = [s for s in (signals or []) if s.get("eligible") and s.get("tradeable")
             and s.get("as_of", as_of) == as_of]
    cands.sort(key=lambda s: abs((_num(s.get("ratio")) or 1.0) - 1.0), reverse=True)
    for sig in cands:
        tk = sig.get("ticker")
        label = sig.get("label")
        if (label == "rich" and not CONFIG["trade_rich"]) or (label == "cheap" and not CONFIG["trade_cheap"]):
            continue
        if label not in ("rich", "cheap"):
            continue
        if tk in open_tix:
            continue
        if len(positions) >= CONFIG["max_open"]:
            skipped.append({"ticker": tk, "reason": "max_open reached"})
            break
        pos, why = _open_from_signal(sig, nav_for_sizing, as_of)
        if pos is None:
            skipped.append({"ticker": tk, "reason": why})
            _log.info("[OptionsPaperLeg] %s %s 跳过：%s", tk, label, why)
            continue
        if pos.side == "long" and pos.size_usd > cash:
            skipped.append({"ticker": tk, "reason": "insufficient cash"})
            continue
        if not all(math.isfinite(x) for x in (pos.size_usd, pos.last_mark, pos.entry_premium)):
            skipped.append({"ticker": tk, "reason": "non-finite fill"})
            continue
        cash += -pos.size_usd if pos.side == "long" else pos.size_usd
        positions.append(pos)
        open_tix.add(tk)
        opened.append(pos.to_dict())
        _log.info("[OptionsPaperLeg] → %s %s K=%s exp=%s  premium %.2f ×%d  size=$%.0f",
                  pos.ticker, pos.side, pos.strike, pos.expiry, pos.entry_premium,
                  pos.contracts, pos.size_usd)

    # ── (d) 净值快照 ──
    unreal = sum(_unrealized(p) for p in positions)
    at_risk = sum(p.size_usd for p in positions)
    nav = cash + sum(_mark_value(p) for p in positions)
    n_stale = sum(1 for p in positions if p.mark_source != "cboe_mid")
    snapshot = {
        "date": as_of,
        "cash": round(cash, 2),
        "open_premium_at_risk": round(at_risk, 2),
        "unrealized": round(unreal, 2),
        "nav": round(nav, 2),
        "positions": len(positions),
        "stale_positions": n_stale,
        # 从状态推导而非用本次运行的计数器：同日重跑必须写出逐字节相同的快照
        "closed_today": sum(1 for c in closed if c.get("exit_date") == as_of),
        "realized_pnl_today": round(sum(_num(c.get("pnl_usd")) or 0.0 for c in closed
                                        if c.get("exit_date") == as_of), 2),
        "opened_today": sum(1 for p in positions if p.entry_date == as_of),
    }
    equity = [e for e in _load_jsonl(EQUITY_FILE) if e.get("date") != as_of]
    equity.append(snapshot)
    equity.sort(key=lambda e: e["date"])
    _write_jsonl(EQUITY_FILE, equity)
    _write_jsonl(POSITIONS_FILE, [p.to_dict() for p in positions])

    meta["cash"] = cash
    meta["last_run_date"] = as_of
    meta["config_snapshot"] = dict(CONFIG)
    meta["version"] = _VERSION
    meta["skipped_entries"] = [{"date": as_of, **s} for s in skipped]
    _save_meta(meta)

    return {
        "as_of": as_of, "nav": nav, "cash": cash,
        "positions": [p.to_dict() for p in positions],
        "opened_today": opened, "closed_today": closed_today,
        "skipped": skipped, "equity_snapshot": snapshot,
    }


# ══════════════════════════════════════════════════════════════════════════════
# KPI / 报告
# ══════════════════════════════════════════════════════════════════════════════

def _kpi_block(trades: List[Dict]) -> Dict:
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_pnl_pct": None, "total_pnl_usd": 0.0}
    wins = sum(1 for t in trades if (_num(t.get("pnl_usd")) or 0.0) > 0)
    pcts = [_num(t.get("pnl_pct")) for t in trades]
    pcts = [p for p in pcts if p is not None]
    return {
        "n": n,
        "win_rate": round(wins / n * 100.0, 2),
        "avg_pnl_pct": round(sum(pcts) / len(pcts), 4) if pcts else None,
        "total_pnl_usd": round(sum(_num(t.get("pnl_usd")) or 0.0 for t in trades), 2),
    }


def compute_kpis() -> Dict:
    closed = _load_jsonl(CLOSED_FILE)
    positions = _load_jsonl(POSITIONS_FILE)
    equity = _load_jsonl(EQUITY_FILE)
    by_side = defaultdict(list)
    by_label = defaultdict(list)
    for t in closed:
        by_side[t.get("side") or "?"].append(t)
        by_label[t.get("label") or "?"].append(t)
    out = _kpi_block(closed)
    out.update({
        "by_side": {k: _kpi_block(v) for k, v in by_side.items()},
        "by_label": {k: _kpi_block(v) for k, v in by_label.items()},
        "intrinsic_exits": sum(1 for t in closed if t.get("mark_source") == "intrinsic"),
        # H3(c)（v0.45.104）：核销与冻结 mark 必须单独可见，否则读者会把一个早就
        # 死掉的仓位的入场日价格当成活报价读。
        "written_off_exits": sum(1 for t in closed if t.get("mark_source") == "written_off"),
        "stale_positions": sum(1 for p in positions if p.get("mark_source") != "cboe_mid"),
        "open_positions": len(positions),
        "nav": equity[-1].get("nav") if equity else CONFIG["starting_capital"],
        "starting_capital": CONFIG["starting_capital"],
        "as_of": equity[-1].get("date") if equity else None,
    })
    return out


def _fmt(v, nd=2, suffix="") -> str:
    f = _num(v)
    return f"{f:.{nd}f}{suffix}" if f is not None else "—"


def render_markdown(as_of: str) -> str:
    """日报小节。没有信号也没有持仓/历史 → 空串（不占版面）。"""
    try:
        import earnings_vol_signal as _evs
        sigs = _evs.signals_for_date(as_of)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[OptionsPaperLeg] 读取信号失败: %s", exc)
        sigs = []
    positions = [StraddlePosition.from_record(p) for p in _load_jsonl(POSITIONS_FILE)]
    kpis = compute_kpis()
    if not sigs and not positions and kpis["n"] == 0:
        return ""

    lines = ["", "## 期权纸面腿：财报跨式（观察项）", ""]
    eligible = [s for s in sigs if s.get("eligible")]
    lines.append(f"扫描 {len(sigs)} 个快照，跨过财报的合格信号 {len(eligible)} 个"
                 f"（rich {sum(1 for s in eligible if s.get('label') == 'rich')} / "
                 f"cheap {sum(1 for s in eligible if s.get('label') == 'cheap')} / "
                 f"fair {sum(1 for s in eligible if s.get('label') == 'fair')} / "
                 f"untradeable {sum(1 for s in eligible if s.get('label') == 'untradeable')}）。")
    if eligible:
        lines += ["", "| 标的 | 标签 | 比值 | 隐含事件波动 | 历史中位 (n) | 最大腿点差 | 财报日 | 口径 |",
                  "|------|------|------|--------------|--------------|------------|--------|------|"]
        for s in sorted(eligible, key=lambda x: -abs((_num(x.get("ratio")) or 1.0) - 1.0)):
            lines.append(f"| {s['ticker']} | {s.get('label')} | {_fmt(s.get('ratio'))} | "
                         f"{_fmt(s.get('implied_event_move_pct'), 2, '%')} | "
                         f"{_fmt(s.get('hist_median_abs_move_pct'), 2, '%')} ({s.get('hist_n')}) | "
                         f"{_fmt((_num(s.get('max_leg_spread_pct')) or 0) * 100, 1, '%')} | "
                         f"{s.get('earnings_date')} | {s.get('event_move_basis')} |")
    if positions:
        lines += ["", "**持仓**", "",
                  "| 标的 | 方向 | 张数 | 入场权利金 | 当前 mark | 浮动盈亏 | mark 来源 | 财报日 | 到期 |",
                  "|------|------|------|------------|-----------|----------|-----------|--------|------|"]
        for p in positions:
            # H3(c)：冻结 mark 必须一眼可辨，否则读者会把上次活报价的价格当成今天的市价。
            src = (f"⚠️ {p.mark_source} ({p.stale_days}d @ {p.last_mark_date})"
                   if p.mark_source != "cboe_mid" else p.mark_source)
            lines.append(f"| {p.ticker} | {p.side} | {p.contracts} | {_fmt(p.entry_premium)} | "
                         f"{_fmt(p.last_mark)} | ${_unrealized(p):+,.0f} | {src} | "
                         f"{p.earnings_date} | {p.expiry} |")
        n_frozen = sum(1 for p in positions if p.mark_source != "cboe_mid")
        if n_frozen:
            lines += ["", f"> ⚠️ 其中 {n_frozen} 个持仓用的是**冻结 mark**（上次拿到活报价那天的价格），"
                          "不是今天的市价；这部分 NAV 只是账面占位，不可当成可成交的估值。"]
    tail = ""
    if kpis.get("intrinsic_exits"):
        tail += f"，内在价值平仓 {kpis['intrinsic_exits']} 笔"
    if kpis.get("written_off_exits"):
        tail += (f"，**核销（written_off）{kpis['written_off_exits']} 笔**"
                 "（到期后再也拿不到任何价格，按最后已知 mark 记账，不是成交价）")
    lines += ["", f"**账本**：NAV ${_num(kpis.get('nav')) or 0:,.0f}（起始 ${CONFIG['starting_capital']:,.0f}），"
                  f"已平 {kpis['n']} 笔，胜率 {_fmt(kpis.get('win_rate'), 1, '%')}，"
                  f"均值 {_fmt(kpis.get('avg_pnl_pct'), 2, '%')}，累计 ${kpis.get('total_pnl_usd', 0):+,.0f}"
                  f"{tail}。"]
    for k, v in sorted(kpis.get("by_side", {}).items()):
        lines.append(f"- {k}: {v['n']} 笔，胜率 {_fmt(v['win_rate'], 1, '%')}，均值 {_fmt(v['avg_pnl_pct'], 2, '%')}")
    lines += ["",
              "> 成交约定：long 按 ask 买 / bid 卖，short 按 bid 卖 / ask 买回（付点差）；盯市用 CBOE mid。",
              "> 隐含事件波动 = √(跨式² − 0.8σ√T 扩散²)，`raw_straddle` 口径未扣扩散、偏高。",
              "> ⚠️ 无 delta 对冲；short 跨式风险无上界，权利金封顶只是名义可比性，**不是仓位规则**。",
              "> **观察项：不进评分、不进股票纸面组合、不构成任何建议。**", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from hive_logger import pdt_today
    d = sys.argv[1] if len(sys.argv) > 1 else pdt_today()
    r = run_for_date(d)
    print(json.dumps({k: v for k, v in r.items() if k != "positions"}, ensure_ascii=False, indent=2))
    print(render_markdown(d))
