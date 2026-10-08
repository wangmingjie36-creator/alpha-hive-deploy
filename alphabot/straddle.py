"""Alpha Bot「跨式账本」页的数据面（v0.45.424）：财报跨式纸面账本 + 逐仓风险 + 盘中参考报价。

读什么（全部只读；路径每次调用时现取 `PATHS.home`）
----------------------------------------------------
  · `<数据根>/options_paper_state/`：跨式账本（positions / closed_trades / equity_curve / meta）与
    财报信号 `earnings_signals.jsonl`（现价、隐含 / 实际事件波动都在这里）；
  · `<数据根>/hedge_state/greeks_<日期>.json`：portfolio_greeks 每天逐腿算好的 Greeks（CBOE 报价自带的
    per-share delta / gamma / vega / theta）。
`options_paper_leg` / `earnings_vol_signal` 的路径是模块级常量、import 时就冻住了（CLAUDE.md「新产物的默认
路径」一节），这里不复用。本模块**不写任何文件**（tests/test_alphabot_straddle.py 打遍接口前后核指纹）。

逐仓 Greeks
-----------
腿上的 per-share Greeks 取自 greeks 文件（或盘中报价），金额口径与 `portfolio_greeks.option_exposures` 逐项相同
（测试拿同一组输入对照）：
    qty = ±张数×100（买跨式 +、卖跨式 −）
    $Delta = qty·delta·S    $Gamma(1%) = ½·qty·gamma·(0.01·S)²    $Vega/点 = qty·vega    $Theta/日 = qty·theta
**S 用与账本 mark 同一份 CBOE 快照里的现价**（当天信号行的 `underlying_price`），不用 greeks 文件里的 `price`：
那个价曾比期权报价晚一天（v0.45.423）。任一腿缺任一项 ⇒ 对应合计为 None，不把缺的当 0。

盲期（检验协议：experiments/straddle_gex_prereg.md）
---------------------------------------------------
信号行从 v0.45.424 起带 `gex_ctx`（GEX 影子记录）。检验冻结之前，本模块**只数**带 GEX 的事件有多少、
各政体多少，**从不**把任何一笔的 GEX 和它之后的盈亏 / 实际波动放进同一个返回值：持仓、平仓、校准、
今日信号四处都不含 `gex_ctx`（测试钉住）。当天现算的 GEX 水平（Alpha Bot 本来就显示的那份）照常给。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hive_logger import PATHS, get_logger

_log = get_logger("alphabot.straddle")

LEDGER_DIRNAME = "options_paper_state"
GREEKS_DIRNAME = "hedge_state"
GREEK_KEYS = ("delta", "gamma", "vega", "theta")
#: 扫描 14:00 PT（= 17:00 ET）起跑、约 50 分钟；过了这个美东时刻，当天就该有账本行
LEDGER_READY_ET = dtime(19, 0)
#: 每份跨式的净 |Δ| 到这个数就在卡片上标「已明显带方向」（入场时 ≈ 0）。展示阈值，不是交易规则
DIRECTIONAL_DELTA_WARN = 0.40
#: 持仓 / 平仓 / 校准里从入场信号行带出的字段——**刻意不含 gex_ctx**（见模块头「盲期」）
ENTRY_FIELDS = ("ratio", "label", "implied_event_move_pct", "hist_median_abs_move_pct", "hist_n",
                "max_leg_spread_pct", "event_move_basis", "rv_30d", "dte", "straddle_move_pct",
                "underlying_price", "quote_fetched_at", "market_open", "straddle_net_delta")
#: 本模块的任何返回值都不许出现的键（检验冻结前）
BLINDED_KEYS = frozenset({"gex_ctx"})
SIDE_SIGN = {"long": 1.0, "short": -1.0}


# ─────────────────────────────── 小工具

def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> Optional[float]:
    f = _num(v)
    return f if (f is not None and f > 0) else None


def _date(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def days_between(a, b) -> Optional[int]:
    """日历日 b − a（两端都是 YYYY-MM-DD）；任一端解析不了 ⇒ None。"""
    da, db = _date(a), _date(b)
    return (db - da).days if (da and db) else None


def read_jsonl(path: Path) -> Tuple[List[dict], int]:
    """(行, 坏行数)。文件不存在 ⇒ ([], 0)。坏行不吞：计数随结果返回，页面上显示。"""
    rows, bad = [], 0
    if not path.is_file():
        return rows, bad
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            bad += 1
    return rows, bad


def read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    if not path.is_file():
        return None, "missing"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return (obj, None) if isinstance(obj, dict) else (None, "not_an_object")


# ─────────────────────────────── 读账本（唯一的 I/O）

def load_ledger(root=None) -> dict:
    """一次读齐页面要的全部文件。`root` = 数据根（缺省 `PATHS.home`，调用时求值）。"""
    home = Path(root) if root is not None else PATHS.home
    ld, gd = home / LEDGER_DIRNAME, home / GREEKS_DIRNAME
    out = {"ledger_dir": str(ld), "exists": ld.is_dir(), "meta": None, "meta_error": None,
           "positions": [], "closed": [], "equity": [], "signals": [], "bad_lines": {},
           "greeks": None, "greeks_error": None, "source": "ledger"}
    if not out["exists"]:
        return out
    out["meta"], out["meta_error"] = read_json(ld / "meta.json")
    for key, name in (("positions", "positions.jsonl"), ("closed", "closed_trades.jsonl"),
                      ("equity", "equity_curve.jsonl"), ("signals", "earnings_signals.jsonl")):
        rows, bad = read_jsonl(ld / name)
        out[key] = rows
        if bad:
            out["bad_lines"][name] = bad
            _log.warning("跨式账本 %s 有 %d 行解析失败（页面会显示）", name, bad)
    as_of = ledger_as_of(out)
    if as_of:
        out["greeks"], out["greeks_error"] = read_json(gd / f"greeks_{as_of}.json")
    else:
        out["greeks_error"] = "no_ledger_date"
    return out


def load_positions(root=None) -> List[dict]:
    """只读持仓文件（盘中参考报价只需要持仓的两个合约代码，不必每次重读整本账本，v0.45.428）。"""
    home = Path(root) if root is not None else PATHS.home
    rows, bad = read_jsonl(home / LEDGER_DIRNAME / "positions.jsonl")
    if bad:
        _log.warning("跨式账本 positions.jsonl 有 %d 行解析失败", bad)
    return rows


def ledger_as_of(led: dict) -> Optional[str]:
    meta = led.get("meta") or {}
    if meta.get("last_run_date"):
        return str(meta["last_run_date"])
    eq = led.get("equity") or []
    return str(eq[-1].get("date")) if eq and eq[-1].get("date") else None


def expected_ledger_date(now: datetime) -> Tuple[str, str]:
    """此刻「最近一个应当已有账本行」的美东扫描日，与所用日历。今天是交易日且已过 `LEDGER_READY_ET` ⇒ 今天；
    否则往前找最近的交易日。交易日历不可用 ⇒ 按工作日判，并如实返回 `weekday_only`。"""
    try:
        import is_trading_day as itd

        def is_td(d):
            return bool(itd.is_trading_day(d)[0])
        cal = "is_trading_day"
    except Exception:  # noqa: BLE001 - 日历坏了：按工作日判，但要说出来
        def is_td(d):
            return d.weekday() < 5
        cal = "weekday_only"
    d = now.date()
    if not (is_td(d) and now.time() >= LEDGER_READY_ET):
        d -= timedelta(days=1)
        for _ in range(14):
            if is_td(d):
                break
            d -= timedelta(days=1)
    return d.isoformat(), cal


# ─────────────────────────────── 逐仓风险（纯函数）

def position_greeks(legs, side: str, contracts, S) -> dict:
    """一笔跨式（[call 腿, put 腿]）的净 Greeks。腿 = 带 per-share delta/gamma/vega/theta 的 dict，或 None。
    金额口径同 `portfolio_greeks.option_exposures`（见模块头）。缺项 ⇒ 对应合计 None、`missing` 写明缺什么。"""
    n = _pos(contracts)
    sign = SIDE_SIGN.get(side)
    qty = sign * n * 100.0 if (n is not None and sign is not None) else None
    missing: List[str] = []
    per: Dict[str, List[float]] = {k: [] for k in GREEK_KEYS}
    legs = list(legs or [])[:2]
    legs += [None] * (2 - len(legs))
    for name, leg in zip(("call", "put"), legs):
        if not isinstance(leg, dict):
            missing.append(f"{name}:no_quote")
            continue
        for k in GREEK_KEYS:
            v = _num(leg.get(k))
            if v is None:
                missing.append(f"{name}:{k}")
            else:
                per[k].append(v)
    tot = {k: (sum(per[k]) if len(per[k]) == 2 else None) for k in GREEK_KEYS}
    S = _pos(S)
    if S is None:
        missing.append("underlying_price")

    def mul(*xs):
        return None if any(x is None for x in xs) else math.prod(xs)
    d, g, v, t = (tot[k] for k in GREEK_KEYS)
    per_straddle = mul(sign, d)                    # 这笔仓位每 1 份跨式（每股）的净 Δ
    return {
        "qty": qty,
        "net_delta_per_straddle": per_straddle,
        "directional": (abs(per_straddle) >= DIRECTIONAL_DELTA_WARN) if per_straddle is not None else None,
        "delta_shares": mul(qty, d),
        "dollar_delta": mul(qty, d, S),
        "gamma_dollar_per_1pct": None if (qty is None or g is None or S is None) else 0.5 * qty * g * (0.01 * S) ** 2,
        "vega_dollar_per_pt": mul(qty, v),
        "theta_dollar_per_day": mul(qty, t),
        "underlying_price": S,
        "complete": not missing,
        "missing": missing,
    }


def breakeven(side: str, strike, premium) -> dict:
    """到期盈亏平衡带 K ± 入场权利金。卖跨式：到期价落在带内赚；买跨式：落在带外才赚。"""
    K, P = _pos(strike), _pos(premium)
    if K is None or P is None:
        return {"available": False, "lo": None, "hi": None, "profit_inside": None}
    return {"available": True, "lo": K - P, "hi": K + P, "profit_inside": side == "short"}


def band_position(S, band: dict) -> dict:
    """现价相对盈亏平衡带的位置：在带内否、离两条边各多少 %（相对现价）、离最近一条边多少 %。"""
    S = _pos(S)
    if S is None or not band.get("available"):
        return {"available": False}
    lo, hi = band["lo"], band["hi"]
    inside = lo <= S <= hi
    to_lo, to_hi = (lo / S - 1.0) * 100.0, (hi / S - 1.0) * 100.0
    nearest = min(abs(to_lo), abs(to_hi))
    in_profit = inside if band.get("profit_inside") else not inside
    return {"available": True, "S": S, "inside": inside, "pct_to_lo": to_lo, "pct_to_hi": to_hi,
            "pct_to_nearest_edge": nearest, "at_expiry_in_profit": in_profit}


def unrealized(side: str, entry_premium, mark, contracts) -> Optional[float]:
    """同 `options_paper_leg._unrealized`：(mark − 入场) × 100 × 张数，卖跨式取反。"""
    e, m, n, sign = _num(entry_premium), _num(mark), _pos(contracts), SIDE_SIGN.get(side)
    if None in (e, m, n, sign):
        return None
    return (m - e) * 100.0 * n * sign


# ─────────────────────────────── 汇总（纯函数：吃 load_ledger 的结果）

def _kpi_block(trades: List[dict]) -> dict:
    """直接用账本自己的 `options_paper_leg._kpi_block`——同一口径只维护一份（v0.45.428 二次检查）。
    那个模块 import 时只求值数据根（0.45.422 起缺省 ~/alpha-hive-data，不会抛）；本页不读它的模块级路径。"""
    import options_paper_leg as opl
    return opl._kpi_block(trades)


def kpis(closed: List[dict]) -> dict:
    by_side: Dict[str, List[dict]] = {}
    by_label: Dict[str, List[dict]] = {}
    for t in closed:
        by_side.setdefault(t.get("side") or "?", []).append(t)
        by_label.setdefault(t.get("label") or "?", []).append(t)
    out = _kpi_block(closed)
    out.update({"by_side": {k: _kpi_block(v) for k, v in by_side.items()},
                "by_label": {k: _kpi_block(v) for k, v in by_label.items()},
                "intrinsic_exits": sum(1 for t in closed if t.get("mark_source") == "intrinsic"),
                "written_off_exits": sum(1 for t in closed if t.get("mark_source") == "written_off")})
    return out


def _signal_index(signals: List[dict]):
    """(ticker, as_of) → 行；(ticker, earnings_date) → 该事件全部合格行（按 as_of 升序）。"""
    by_day: Dict[Tuple[str, str], dict] = {}
    by_event: Dict[Tuple[str, str], List[dict]] = {}
    for s in signals:
        tk, d = str(s.get("ticker") or ""), str(s.get("as_of") or "")
        by_day[(tk, d)] = s
        if s.get("eligible") and s.get("earnings_date"):
            by_event.setdefault((tk, str(s["earnings_date"])), []).append(s)
    for rows in by_event.values():
        rows.sort(key=lambda r: str(r.get("as_of") or ""))
    return by_day, by_event


def _entry_brief(sig: Optional[dict]) -> Optional[dict]:
    return {k: sig.get(k) for k in ENTRY_FIELDS} if isinstance(sig, dict) else None


def _settled(event_rows: List[dict]) -> Optional[dict]:
    for r in event_rows or []:
        if _num(r.get("realized_abs_move_pct")) is not None:
            return r
    return None


def _greek_legs(greeks: Optional[dict]) -> Dict[str, dict]:
    """greeks 文件里的期权行，按 OCC 符号索引；只取 per-share 的四个 Greeks 与报价可得性。"""
    out = {}
    for r in (greeks or {}).get("rows") or []:
        if isinstance(r, dict) and r.get("kind") == "option" and r.get("symbol"):
            out[str(r["symbol"])] = {k: r.get(k) for k in (*GREEK_KEYS, "iv", "mid", "quote_missing")}
    return out


def position_view(p: dict, *, as_of: Optional[str], today: Optional[str], sig_today: Optional[dict],
                  sig_entry: Optional[dict], greek_legs: Dict[str, dict]) -> dict:
    side = str(p.get("side") or "")
    n = _pos(p.get("contracts"))
    pnl = unrealized(side, p.get("entry_premium"), p.get("last_mark"), n)
    size = _pos(p.get("size_usd"))
    S = _pos((sig_today or {}).get("underlying_price"))
    band = breakeven(side, p.get("strike"), p.get("entry_premium"))
    legs = [greek_legs.get(str(p.get("call_symbol"))), greek_legs.get(str(p.get("put_symbol")))]
    legs = [None if (not leg or leg.get("quote_missing")) else leg for leg in legs]
    g = position_greeks(legs, side, n, S)
    g["source"] = f"hedge_state/greeks_{as_of}.json" if (as_of and greek_legs) else None
    return {
        **{k: p.get(k) for k in ("ticker", "side", "label", "entry_date", "expiry", "strike", "call_symbol",
                                 "put_symbol", "contracts", "entry_call", "entry_put", "entry_premium",
                                 "entry_underlying", "earnings_date", "signal_ratio", "size_usd", "last_mark",
                                 "last_mark_date", "mark_source", "stale_days", "rationale")},
        "unrealized_usd": pnl,
        "unrealized_pct": (pnl / size * 100.0) if (pnl is not None and size) else None,
        "days": {"entry_to_expiry": days_between(p.get("entry_date"), p.get("expiry")),
                 "entry_to_earnings": days_between(p.get("entry_date"), p.get("earnings_date")),
                 "elapsed": days_between(p.get("entry_date"), today),
                 "to_earnings": days_between(today, p.get("earnings_date")),
                 "to_expiry": days_between(today, p.get("expiry"))},
        "underlying": {"price": S, "as_of": (sig_today or {}).get("as_of"),
                       "fetched_at": (sig_today or {}).get("quote_fetched_at"),
                       "reason": None if S else ("no_signal_row_for_ledger_date" if not sig_today
                                                 else "underlying_price_missing")},
        "breakeven": band, "band": band_position(S, band), "greeks": g,
        "entry_signal": _entry_brief(sig_entry),
    }


def closed_view(c: dict, *, sig_entry: Optional[dict], event_rows: List[dict]) -> dict:
    st = _settled(event_rows)
    implied = _num((sig_entry or {}).get("implied_event_move_pct"))
    realized = _num((st or {}).get("realized_abs_move_pct"))
    return {
        **{k: c.get(k) for k in ("ticker", "side", "label", "entry_date", "exit_date", "expiry", "strike",
                                 "contracts", "entry_premium", "exit_premium", "earnings_date", "signal_ratio",
                                 "size_usd", "pnl_usd", "pnl_pct", "exit_reason", "mark_source", "holding_days",
                                 "settled_late_days", "rationale")},
        "implied_event_move_pct": implied,
        "realized_abs_move_pct": realized,
        "realized_move_pct": _num((st or {}).get("realized_move_pct")),
        "realized_vs_implied": (realized / implied) if (realized is not None and implied) else None,
        "entry_signal": _entry_brief(sig_entry),
    }


def equity_series(equity: List[dict], closed: List[dict]) -> List[dict]:
    """净值曲线 + 截至当天的累计已实现（按平仓日累加）。"""
    real = sorted((str(c.get("exit_date") or ""), _num(c.get("pnl_usd")) or 0.0) for c in closed)
    out, acc, i = [], 0.0, 0
    for e in sorted(equity, key=lambda r: str(r.get("date") or "")):
        d = str(e.get("date") or "")
        while i < len(real) and real[i][0] <= d:
            acc += real[i][1]
            i += 1
        out.append({"date": d, "nav": _num(e.get("nav")), "cash": _num(e.get("cash")),
                    "unrealized": _num(e.get("unrealized")), "realized_cum": round(acc, 2),
                    "open_premium_at_risk": _num(e.get("open_premium_at_risk")),
                    "positions": e.get("positions"), "stale_positions": e.get("stale_positions")})
    return out


_REASON_ZH = (
    ("not within", "财报不在可交易窗口（须晚于今天、早于到期前缓冲）"),
    ("no upcoming earnings date", "查不到下次财报日"),
    ("quote_set unavailable", "当日没有报价集"),
    ("atm leg quote not ok", "ATM 腿报价不可用"),
    ("insufficient earnings history", "财报历史次数不足"),
    ("historical median move unavailable", "历史财报波动不可得"),
)


def reason_zh(r) -> str:
    s = str(r or "")
    for needle, zh in _REASON_ZH:
        if needle in s:
            return zh
    return s or "—"


def signals_today(signals: List[dict], as_of: Optional[str], open_tickers: set, skipped: List[dict]) -> dict:
    rows = [s for s in signals if as_of and s.get("as_of") == as_of]
    skip = {str(x.get("ticker")): x.get("reason") for x in skipped or [] if x.get("date") in (None, as_of)}
    out = []
    for s in sorted((s for s in rows if s.get("eligible")), key=lambda s: -abs((_num(s.get("ratio")) or 1.0) - 1.0)):
        tk = str(s.get("ticker") or "")
        if tk in open_tickers:
            status = "持仓中"
        elif tk in skip:
            status = f"跳过：{skip[tk]}"
        elif s.get("label") in ("rich", "cheap"):
            status = "未开仓（原因未记录）"
        else:
            status = "不交易"
        out.append({**{k: s.get(k) for k in ("ticker", "label", "raw_label", "ratio", "implied_event_move_pct",
                                             "hist_median_abs_move_pct", "hist_n", "max_leg_spread_pct",
                                             "earnings_date", "selected_expiry", "tradeable", "untradeable_reason",
                                             "event_move_basis")}, "status": status})
    reasons: Dict[str, int] = {}
    for s in rows:
        if not s.get("eligible"):
            k = reason_zh(s.get("reason"))
            reasons[k] = reasons.get(k, 0) + 1
    return {"as_of": as_of, "n_rows": len(rows), "eligible": out,
            "ineligible_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])}


def calibration_points(by_event: Dict[Tuple[str, str], List[dict]], traded: set) -> List[dict]:
    """每个已结算的财报事件一个点：横轴 = 首条可用信号的比值（隐含 / 历史中位），纵轴 = 实际 / 隐含。
    「首条可用」= 按日期最早、隐含事件波动 > 0 的合格行（被压成 0 的行没有比值，例：MU 09-04~11）——
    与检验协议的单位定义相同；**不含 GEX**（盲期）。"""
    pts = []
    for (tk, ed), rows in sorted(by_event.items()):
        st = _settled(rows)
        first = next((r for r in rows if (_num(r.get("implied_event_move_pct")) or 0.0) > 0
                      and _num(r.get("ratio")) is not None), None)
        if first is None:
            continue
        implied = _num(first.get("implied_event_move_pct"))
        realized = _num((st or {}).get("realized_abs_move_pct"))
        ratio = _num(first.get("ratio"))
        if realized is None:
            continue
        pts.append({"ticker": tk, "earnings_date": ed, "first_as_of": first.get("as_of"), "ratio": ratio,
                    "label": first.get("raw_label") or first.get("label"), "implied_event_move_pct": implied,
                    "realized_abs_move_pct": realized, "realized_vs_implied": realized / implied,
                    "traded": (tk, ed) in traded})
    return pts


def build_overview(led: dict, *, now_et: datetime, rules: Optional[dict] = None,
                   shadow: Optional[dict] = None) -> dict:
    """页面要的全部数据。`rules`：阈值 / 账本配置（服务层从代码里取的那一份）；`shadow`：GEX 影子记录进度
    （只有计数）。缺目录 ⇒ `data_available=False` 且说清是哪个目录，**不**当成「空账本」。"""
    base = {"source": led.get("source"), "rules": rules or {}, "now_et": now_et.isoformat(timespec="seconds"),
            "state_dir": {"path": led.get("ledger_dir"), "exists": bool(led.get("exists"))}}
    if not led.get("exists"):
        return {**base, "data_available": False, "reason": "ledger_dir_missing"}
    meta = led.get("meta") or {}
    as_of = ledger_as_of(led)
    today = now_et.date().isoformat()
    expected, cal = expected_ledger_date(now_et)
    by_day, by_event = _signal_index(led.get("signals") or [])
    legs = _greek_legs(led.get("greeks"))
    positions = [position_view(p, as_of=as_of, today=today, sig_today=by_day.get((str(p.get("ticker")), str(as_of))),
                               sig_entry=by_day.get((str(p.get("ticker")), str(p.get("entry_date")))),
                               greek_legs=legs)
                 for p in led.get("positions") or []]
    closed_raw = sorted(led.get("closed") or [], key=lambda c: str(c.get("exit_date") or ""), reverse=True)
    closed = [closed_view(c, sig_entry=by_day.get((str(c.get("ticker")), str(c.get("entry_date")))),
                          event_rows=by_event.get((str(c.get("ticker")), str(c.get("earnings_date"))), []))
              for c in closed_raw]
    equity = equity_series(led.get("equity") or [], led.get("closed") or [])
    last = equity[-1] if equity else {}
    start = _num(meta.get("starting_capital")) or _num((meta.get("config_snapshot") or {}).get("starting_capital"))
    realized = round(sum(_num(c.get("pnl_usd")) or 0.0 for c in led.get("closed") or []), 2)
    unreal = round(sum(p["unrealized_usd"] or 0.0 for p in positions), 2)
    nav = last.get("nav")
    # 账目恒等式：NAV = 起始资金 + 已实现 + 浮动（cash 记账下精确成立）。对不上 ⇒ 页面标红，别默默显示
    gap = (nav - (start + realized + unreal)) if (nav is not None and start is not None) else None
    traded = {(str(c.get("ticker")), str(c.get("earnings_date"))) for c in led.get("closed") or []} | \
             {(str(p.get("ticker")), str(p.get("earnings_date"))) for p in led.get("positions") or []}
    cfg = meta.get("config_snapshot") or {}
    return {
        **base, "data_available": True, "as_of": as_of, "today_et": today,
        "freshness": {"expected_as_of": expected, "calendar": cal,
                      "stale": bool(as_of and as_of < expected), "ledger_version": meta.get("version")},
        "account": {"starting_capital": start, "cash": _num(meta.get("cash")), "nav": nav,
                    "return_pct": ((nav / start - 1.0) * 100.0) if (nav is not None and start) else None,
                    "realized_usd": realized, "unrealized_usd": unreal,
                    "unrealized_ledger_usd": last.get("unrealized"),
                    "open_premium_at_risk": last.get("open_premium_at_risk"),
                    "positions": len(positions), "max_open": cfg.get("max_open"),
                    "risk_per_trade_pct": cfg.get("risk_per_trade_pct"),
                    "identity_gap_usd": round(gap, 2) if gap is not None else None,
                    "identity_ok": (abs(gap) <= 1.0) if gap is not None else None},
        "positions": positions,
        "greeks_file": {"date": as_of, "available": bool(legs), "error": led.get("greeks_error") if not legs else None},
        "closed": closed, "kpis": kpis(led.get("closed") or []), "equity": equity,
        "signals_today": signals_today(led.get("signals") or [], as_of, {str(p.get("ticker")) for p in positions},
                                       meta.get("skipped_entries") or []),
        "calibration": calibration_points(by_event, traded),
        "shadow": shadow or {"available": False, "reason": "not_computed"},
        "bad_lines": led.get("bad_lines") or {}, "meta_error": led.get("meta_error"),
    }


# ─────────────────────────────── 盘中参考（第二期）

def levels_brief(detail: Optional[dict], view: str = "le_45dte") -> dict:
    """Alpha Bot 现算结果里的当天水平（与标的页同一份）：现价处 gamma 符号、最近零点、净 Major、单边极值。"""
    d = detail or {}
    if not d.get("data_available"):
        return {"available": False, "reason": d.get("reason") or "unavailable"}
    v = (d.get("views") or {}).get(view) or {}
    zg, mj = v.get("zero_gamma") or {}, v.get("majors") or {}
    return {"available": True, "view": view, "underlying_price": d.get("underlying_price"),
            "payload_last_trade_time": d.get("payload_last_trade_time"),
            "sign_at_spot": zg.get("sign_at_spot"), "curve_state": zg.get("curve_state"),
            "zg_nearest": zg.get("nearest"), "zg_below": zg.get("nearest_below"), "zg_above": zg.get("nearest_above"),
            "net_major_pos": mj.get("net_major_pos_strike"), "net_major_neg": mj.get("net_major_neg_strike"),
            "call_side_extreme": mj.get("call_side_extreme_strike"), "put_side_extreme": mj.get("put_side_extreme_strike")}


def live_view(p: dict, q: dict, levels: dict, *, now_et: datetime) -> dict:
    """一笔持仓的盘中参考：两腿现报（mid 盯市 + 按可成交价立即平仓）、净 Greeks、现价相对盈亏带的位置、当天水平。
    **只读、不写账本**；报价拿不到就如实不可得，**不**拿账本 mark 顶上。"""
    side = str(p.get("side") or "")
    n = _pos(p.get("contracts"))
    quotes = (q or {}).get("quotes") or {}
    qc, qp = quotes.get(str(p.get("call_symbol"))), quotes.get(str(p.get("put_symbol")))
    ok = [isinstance(x, dict) and bool(x.get("quote_ok")) for x in (qc, qp)]
    mark = (qc["mid"] + qp["mid"]) if all(ok) else None
    # 立即平仓：买跨式按 bid 卖出、卖跨式按 ask 买回（与账本的出场成交约定相同）
    exit_px = None
    if all(ok):
        exit_px = (qc["bid"] + qp["bid"]) if side == "long" else (qc["ask"] + qp["ask"])
    S = _pos((q or {}).get("underlying_price"))
    band = breakeven(side, p.get("strike"), p.get("entry_premium"))
    legs = [qc if ok[0] else None, qp if ok[1] else None]
    pnl_mid = unrealized(side, p.get("entry_premium"), mark, n)
    size = _pos(p.get("size_usd"))
    return {
        "ticker": p.get("ticker"), "fetched_at_et": now_et.isoformat(timespec="seconds"),
        "available": bool((q or {}).get("available")), "reason": (q or {}).get("reason"),
        "session_live": (q or {}).get("session_live"), "vintage_date": (q or {}).get("vintage_date"),
        "payload_last_trade_time": (q or {}).get("payload_last_trade_time"),
        "underlying_price": S, "underlying_price_source": (q or {}).get("underlying_price_source"),
        "legs": {"call": qc, "put": qp}, "quote_ok": all(ok),
        "mark_mid": mark, "unrealized_mid_usd": pnl_mid,
        "unrealized_mid_pct": (pnl_mid / size * 100.0) if (pnl_mid is not None and size) else None,
        "exit_now_premium": exit_px, "exit_now_usd": unrealized(side, p.get("entry_premium"), exit_px, n),
        "greeks": position_greeks(legs, side, n, S), "band": band_position(S, band), "breakeven": band,
        "levels": levels,
        "same_payload": (levels.get("payload_last_trade_time") == (q or {}).get("payload_last_trade_time"))
        if levels.get("available") and (q or {}).get("available") else None,
    }
