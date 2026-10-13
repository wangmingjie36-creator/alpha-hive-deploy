"""Alpha Bot「溢价」页（v0.45.492）：单票全链，按 **IV ÷ 期限匹配的 RV** 找卖方溢价高的合约。纯函数。

**它是筛选工具，不是已验证的优势。** 卖方长期赚的是方差风险溢价（IV 平均高于随后的 RV），但本仓库从没验证过
「按本页挑出来的那张，结局更好」；个股层面「IV 比 RV 高」本身也不是信号（见 `vrp_signal` 文档）——所以页头
另给一行 `vrp_signal` 的「这只票今天的 IV−RV 在它自己历史里的分位」（攒够样本前如实写「样本不足」）。

口径（每一条都在帮助页，常量经 `/api/method` 出自 `public_rules`）：

  · **RV 期限匹配**：到期日 e 的 RV 取最近 N 个日对数收益，N = clamp(到期前交易日数, RV_N_MIN, RV_N_MAX)。
    ⚠️ 同一到期日内所有合约共用一个 RV ⇒ 到期日内按 IV/RV 排 ≡ 按 IV 排（排出来的是偏度）——
    所以另给逐到期日的平值 IV / RV，先挑期限、再挑行权价。
  · **RV 窗口里缺一根日线 ⇒ 该 N 不可得**（不照算）：按位置取 N+1 根时缺一根，窗口就无声地多跨一天，
    这是仓库里反复出过的那类错（auto-memory「位置窗口统计盘点」）。窗内有 |ln r| ≥ JUMP_ABS 的跳变
    （拆股量级）同样不可得：`realized_vol_from_closes` 会把它静默剔除，窗口就悄悄变短。
  · **偏度溢价** = 合约 IV − 同到期同方向、对数价内程度上左右两个邻居的线性插值（自身与同行权价排除；
    邻居远于 SKEW_MAX_LOG_DIST 就不算）。凸的微笑上弦在曲线之上 ⇒ 这个数偏小（对卖方保守）。
  · **疑似事件**（不依赖财报日）：平值 IV 的总方差 V = σ²T 逐段求前向方差，某段比相邻段多出的方差
    超过报价噪声（每个平值 IV 按 KINK["iv_tol"] 的误差算）的 noise_mult 倍、且折合的一次性波动 ≥ min_move
    ⇒ 该段疑似含事件。三态向后传播（见 `_propagate`）。这是启发式，检出下限见 `kink_detect_limit`。
  · **年化收益按 bid**（mid 往往成交不了）：put 抵押 = 行权价（现金担保）、call 抵押 = 现价
    （备兑名义）——与卖权页 `structure_quote` 同一口径，测试逐例对拍。
  · **IV Rank** 拿 IV 历史**自己的**最新一条去排（与历史同一个量），不拿 CBOE 的 iv30：
    那是另一个口径，拿它排出来的位置可能是假的。iv30 只并排显示、不排名。
  · **财报日**取最近一次有效扫描写进卖权账本的 Chronos 日期，不读兜底缓存（理由见 `earnings_state`）。
  · 判不了的一律是 None（页面写「不可得 / 原因」），不是 0、不是 False。
  · **筛选、排序、按到期日只看一个、截断都在这里按这个顺序做**（`select`）——排序若放到截断之后的前端，
    「按年化排」排的就只是「按 IV/RV 选出的前 MAX_ROWS 个」。
  · **原因码只有一张表** `REASONS`：每个写进 `reason` 的码都经 `_why` 产出、都登记在表里
    （tests/test_alphabot_premium.py 用 AST 双向核对），前端只查这张表、查不到就标「未登记」。

本模块不读写文件、不联网、不 import 卖权模块：所有外部函数经 `Deps` 由 `alphabot/service.py` 注入。
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

DISCLAIMER = ("公开信息研究与筛选，**不构成投资建议**。溢价高 ≠ 已证明对卖方有利：本页的排序从未被前向检验过；"
              "CBOE 延迟报价，按 bid 计收入。")
SORTS = ("iv_over_rv", "yield_ann", "skew_pts")
DEFAULTS = {"dte_min": 7, "dte_max": 60, "delta_min": 0.10, "delta_max": 0.35, "oi_min": 100, "side": "both",
            "sort": "iv_over_rv"}
#: 不记在本机、只对这一次请求有效的参数（选中的到期日随链变，不能当偏好记）
TRANSIENT = ("expiry",)
DTE_BOUNDS = (0, 365)
DELTA_BOUNDS = (0.0, 1.0)
OI_BOUNDS = (0, 10_000_000)
SIDES = ("put", "call", "both")
RV_N_MIN, RV_N_MAX = 10, 60
HEADLINE_RV = (20, 30)
#: 与 `realized_vol_from_closes` 的剔除阈值同一个数——它会静默丢掉这种收益，这里改成整窗不可得
JUMP_ABS = 0.5
#: 日线末端最多落后这么多个交易日（Twelve Data 收盘后仍丢当天那根，正常落后 0）；超过 ⇒ RV 全部不可得
BARS_MAX_LAG_SESSIONS = 2
KINK = {"min_dte": 3, "min_gap_days": 5, "iv_tol": 0.01, "noise_mult": 2.0, "min_move": 0.02}
#: 帮助页「检出下限」的示例口径（平坦期限结构、这个 IV 水平下；IV 越高下限越高）
KINK_EXAMPLE = {"iv": 0.30, "weekly": (7, 14), "monthly": (30, 60)}
SKEW_MAX_LOG_DIST = 0.05
LOW_DTE = 2
#: 财报日取自卖权账本里最近一行 recorded 的扫描（往回最多看这么多个账本日期；扫描那天失败的行是 unavailable）
EARNINGS_SCAN_LOOKBACK = 10
#: 那次扫描落后超过这么多个交易日 ⇒ 财报日按「未知」（Chronos 的预估日期会被改，旧的不可信）
EARNINGS_MAX_SCAN_LAG_SESSIONS = 5
IV_RANK_MAX_LAG_SESSIONS = 3
MAX_ROWS = 300
SPREAD_EPS = 1e-9            # 同 structure_quote 的 _TOL_EPS：价差恰好等于上限算通过
YIELD_DP = 6

#: 原因码 → 中文（`{}` 处填冒号后的明细）。前缀即码。本模块与 service 产出的每个码都必须在这里。
REASONS = {
    # 日线 / RV
    "bars_unavailable": "日线不可得", "no_bars": "日线为空", "duplicate_dates": "日线有重复日期",
    "bars_stale": "日线落后 {} 个交易日", "insufficient_bars": "日线根数不够（有 / 需 {}）",
    "bad_close": "{} 的收盘价不合法", "gap_in_window": "窗口内缺日线 {}", "suspect_jump": "{} 有拆股量级的跳变",
    "rv_rejected": "RV 算出来不在合理区间",
    # 偏度
    "no_bracketing_neighbors": "两侧没有邻居行权价", "neighbors_too_far": "邻居行权价太远",
    # 事件扭结
    "dte_too_short": "天数太短不参与", "no_atm_iv": "没有平值 IV", "too_close_to_previous": "离上一个参与者太近",
    "insufficient_expiries": "可判定的到期日不足两个", "inverted_term_structure": "期限结构倒挂（前向方差 ≤ 0）",
    "no_reference_segment": "相邻段都判不了，没有参照",
    # 财报日（扫描写进卖权账本的 Chronos 日期）
    "not_in_recent_scans": "最近的扫描里没有这只票（不在扫描名单，或那几天扫描都失败）",
    "scan_had_no_earnings_info": "那次扫描没拿到财报信息", "unparseable_date": "扫描记下的日期解析不了（{}）",
    "scan_too_old": "最近一次有效扫描落后 {} 个交易日", "scan_ledger_unreadable": "卖权账本读不了（{}）",
    # IV 历史 / VRP
    "no_index_file": "没有 IV 历史", "empty_index": "IV 历史为空", "insufficient_samples": "样本不足",
    "stale_observation": "最新观测落后 {} 个交易日", "flat_history": "历史无波动，排不了",
    "no_rows_for_ticker": "VRP 账本里没有这只票", "ledger_unreadable": "VRP 账本读不了（{}）",
    # 整页
    "underlying_price_unavailable": "现价不可得", "as_of_unavailable": "链的日期不可得", "fetch_returned_none": "取链没有返回",
    "exception": "计算出错（{}）", "demo_mode": "演示模式不读本地数据",
}
#: 由别的模块产出、经本页透传的码 → 产出它的源文件（测试核对该文件里真有这个字面量）
EXTERNAL_REASONS = {
    "twelve_data_not_configured": ("alphabot/service.py", "没配 Twelve Data key"),
    "twelve_data_returned_nothing": ("alphabot/service.py", "Twelve Data 没返回日线"),
    "bars_fetch_exception": ("alphabot/service.py", "取日线出错（{}）"),
    "stale_vintage": ("cboe_options.py", "CBOE 文件还停在上一交易日"),
    "payload_unavailable": ("cboe_options.py", "CBOE 文件拿不到"),
    "vintage_unverifiable": ("cboe_options.py", "CBOE 文件日期判不了"),
    "vintage_mismatch": ("cboe_options.py", "CBOE 文件日期不符"),
    "snapshot_mode_no_raw_chain": ("cboe_options.py", "补跑模式没有原始链"),
    "price_unavailable": ("cboe_options.py", "CBOE 文件里没有可用的现价"),
    "no_parseable_contracts": ("cboe_options.py", "CBOE 文件里没有能解析的合约"),
    "iv_and_rv_missing": ("vrp_signal.py", "那天 IV 与 RV 都缺"),
    "iv_missing": ("vrp_signal.py", "那天 IV 缺"),
    "rv_30d_missing": ("vrp_signal.py", "那天 RV30 缺"),
}
#: 漏斗各道（顺序即判定顺序）→ 中文
STAGES = {"chain": "全链", "valid_expiry": "到期日可解析", "within_max_dte": "期限内", "priced": "有 IV 与 Δ",
          "quote_gate": "有买价", "spread_gate": "价差闸", "dte": "天数范围", "side": "方向", "delta": "|Δ| 范围",
          "oi": "OI 下限", "expiry": "选中的到期日", "no_contracts": "链上没有合约"}


def _why(code: str, detail: Any = None) -> str:
    """原因码的唯一出口（AST 测试只认经它产出的字面量）。"""
    return code if detail is None else f"{code}:{detail}"


class ParamError(ValueError):
    """查询参数非法（服务层映射成 400；`param` = 出错的那个参数名，前端据此只撤销那一个）。"""

    def __init__(self, msg: str, param: Optional[str] = None):
        super().__init__(msg)
        self.param = param


@dataclass(frozen=True)
class Deps:
    """外部函数与常量（service 注入；测试可换）。"""
    leg: Callable[[dict, float], dict]                    # 卖权页的单腿视图：Δ（CBOE 优先、BS 兜底）、价差、σ 距离
    atm_iv: Callable[[List[dict], float, str], Optional[float]]   # 期限页的平值 IV
    classify_earnings: Callable[[Any, str, str], str]
    year_fraction: Callable[[Any], Optional[float]]
    rv_from_closes: Callable[[List[float]], Optional[float]]     # 百分数
    trading_days: Callable[[date, date], List[date]]
    find_gaps: Callable[..., Dict[str, List[str]]]
    rank_fn: Callable[[float, List[float]], Tuple[Optional[float], Optional[float]]]
    max_spread_pct: float
    iv_rank_min_days: int
    iv_rank_window: int
    vrp_min_obs: int


# ─────────────────────────────── 小工具

def _num(x) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pos(x) -> Optional[float]:
    v = _num(x)
    return v if (v is not None and v > 0) else None


def _date(s) -> Optional[date]:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def pct_to_dec(x) -> Optional[float]:
    """唯一的「百分数 → 小数」边界（RV、IV 历史、iv30 都是百分数进来）。"""
    v = _num(x)
    return None if v is None else v / 100.0


def _lag_sessions(d, as_of, deps: Deps) -> Optional[int]:
    """d 之后、as_of 为止（含）的交易日数 = 这份观测落后几个交易日。任一日期不合法 ⇒ None。"""
    a, b = _date(as_of), _date(d)
    if a is None or b is None:
        return None
    return len(deps.trading_days(b + timedelta(days=1), a)) if a > b else 0


def num_s(x) -> str:
    v = _num(x)
    return "—" if v is None else (str(int(v)) if v == int(v) else f"{v:g}")


def chain_as_of(raw: dict) -> str:
    return str((raw or {}).get("as_of") or (raw or {}).get("vintage_date") or "")


# ─────────────────────────────── 参数

def parse_params(q: Mapping[str, Any]) -> dict:
    """查询参数 → 已校验的参数（缺省取 DEFAULTS）。未知键 / 类型不对 / 越界 / 下限大于上限 ⇒ ParamError(param=…)。
    `expiry` 只校验格式；它是不是链上的到期日由 `select` 判（要先有链）。"""
    q = dict(q or {})
    unknown = sorted(set(q) - set(DEFAULTS) - set(TRANSIENT))
    if unknown:
        raise ParamError(f"未知参数：{', '.join(unknown)}", unknown[0])
    out = dict(DEFAULTS, expiry=None)

    def as_int(k, lo, hi):
        try:
            v = int(str(q[k]).strip())
        except ValueError as exc:
            raise ParamError(f"{k} 须为整数：{q[k]!r}", k) from exc
        if not lo <= v <= hi:
            raise ParamError(f"{k} 须在 {lo}–{hi}：{v}", k)
        return v

    def as_float(k, lo, hi):
        v = _num(str(q[k]).strip())
        if v is None:
            raise ParamError(f"{k} 须为数字：{q[k]!r}", k)
        if not lo <= v <= hi:
            raise ParamError(f"{k} 须在 {lo}–{hi}：{v}", k)
        return v

    def as_choice(k, choices):
        s = str(q[k]).strip().lower()
        if s not in choices:
            raise ParamError(f"{k} 须为 {'/'.join(choices)}：{q[k]!r}", k)
        return s

    for k in ("dte_min", "dte_max"):
        if k in q:
            out[k] = as_int(k, *DTE_BOUNDS)
    for k in ("delta_min", "delta_max"):
        if k in q:
            out[k] = as_float(k, *DELTA_BOUNDS)
    if "oi_min" in q:
        out["oi_min"] = as_int("oi_min", *OI_BOUNDS)
    if "side" in q:
        out["side"] = as_choice("side", SIDES)
    if "sort" in q:
        out["sort"] = as_choice("sort", SORTS)
    if "expiry" in q and str(q["expiry"]).strip():
        d = _date(str(q["expiry"]).strip())
        if d is None or len(str(q["expiry"]).strip()) != 10:
            raise ParamError(f"expiry 应为 YYYY-MM-DD：{q['expiry']!r}", "expiry")
        out["expiry"] = d.isoformat()
    # 范围颠倒怪到「刚改的那一头」上：只有一头出现在查询里时就是它，两头都在时报下限
    if out["dte_min"] > out["dte_max"]:
        raise ParamError(f"dte_min（{out['dte_min']}）大于 dte_max（{out['dte_max']}）",
                         "dte_max" if "dte_max" in q and "dte_min" not in q else "dte_min")
    if out["delta_min"] > out["delta_max"]:
        raise ParamError(f"delta_min（{out['delta_min']}）大于 delta_max（{out['delta_max']}）",
                         "delta_max" if "delta_max" in q and "delta_min" not in q else "delta_min")
    return out


def kink_detect_limit(iv: float, dte_prev: float, dte: float, deps: Deps) -> float:
    """平坦期限结构（两端平值 IV 都是 iv）时，(dte_prev, dte] 这一段能判出的最小一次性波动（小数）。
    与 `forward_kinks` 同一条判据：excess ≥ noise_mult·noise 且 move ≥ min_move，excess = move²。"""
    T1, T2 = deps.year_fraction(dte_prev), deps.year_fraction(dte)
    noise = 2.0 * KINK["iv_tol"] * math.hypot(iv * T2, iv * T1)
    return max(KINK["min_move"], math.sqrt(KINK["noise_mult"] * noise))


def public_rules(deps: Deps) -> dict:
    """帮助页 / 响应回显用的规则常量与文案表（唯一出处）。"""
    ex = KINK_EXAMPLE
    return {"defaults": dict(DEFAULTS), "sorts": list(SORTS), "dte_bounds": list(DTE_BOUNDS),
            "delta_bounds": list(DELTA_BOUNDS), "oi_bounds": list(OI_BOUNDS), "max_spread_pct": deps.max_spread_pct,
            "rv_n_min": RV_N_MIN, "rv_n_max": RV_N_MAX, "headline_rv": list(HEADLINE_RV), "jump_abs": JUMP_ABS,
            "bars_max_lag_sessions": BARS_MAX_LAG_SESSIONS,
            "kink_min_dte": KINK["min_dte"], "kink_min_gap_days": KINK["min_gap_days"],
            "kink_iv_tol_pts": KINK["iv_tol"] * 100, "kink_noise_mult": KINK["noise_mult"],
            "kink_min_move_pct": KINK["min_move"] * 100, "kink_example_iv_pct": ex["iv"] * 100,
            "kink_limit_weekly_pct": kink_detect_limit(ex["iv"], *ex["weekly"], deps) * 100,
            "kink_limit_monthly_pct": kink_detect_limit(ex["iv"], *ex["monthly"], deps) * 100,
            "skew_max_log_dist": SKEW_MAX_LOG_DIST, "low_dte": LOW_DTE,
            "earnings_scan_lookback": EARNINGS_SCAN_LOOKBACK,
            "earnings_max_scan_lag_sessions": EARNINGS_MAX_SCAN_LAG_SESSIONS, "iv_rank_min_days": deps.iv_rank_min_days,
            "iv_rank_window": deps.iv_rank_window, "iv_rank_max_lag_sessions": IV_RANK_MAX_LAG_SESSIONS,
            "vrp_min_obs": deps.vrp_min_obs, "max_rows": MAX_ROWS,
            "reasons": dict(REASONS) | {k: v[1] for k, v in EXTERNAL_REASONS.items()}, "stages": dict(STAGES)}


# ─────────────────────────────── RV

def matched_n(n_trading_days: int) -> int:
    return min(RV_N_MAX, max(RV_N_MIN, int(n_trading_days)))


def _rv_unavailable(n: int, reason: str) -> dict:
    return {"n": n, "value": None, "reason": reason, "start": None, "end": None}


def clean_bars(bars: Sequence[dict]) -> Tuple[List[Tuple[str, Optional[float]]], Optional[str]]:
    """日线 → 按日期升序的 (date, close)。重复日期 ⇒ 原因 duplicate_dates（不猜留哪根）。"""
    rows = []
    for b in bars or []:
        d = _date((b or {}).get("date"))
        if d is not None:
            rows.append((d.isoformat(), _num(b.get("close"))))
    rows.sort(key=lambda r: r[0])
    dates = [r[0] for r in rows]
    if len(set(dates)) != len(dates):
        return rows, _why("duplicate_dates")
    return rows, None


def rv_window(rows: Sequence[Tuple[str, Optional[float]]], n: int, *, missing: frozenset,
              rv_fn: Callable[[List[float]], Optional[float]]) -> dict:
    """最近 n 个日收益的 RV（小数）。不可得 ⇒ value None + reason。"""
    if len(rows) < n + 1:
        return _rv_unavailable(n, _why("insufficient_bars", f"{len(rows)}/{n + 1}"))
    win = list(rows[-(n + 1):])
    out = _rv_unavailable(n, None) | {"start": win[0][0], "end": win[-1][0]}
    bad = [d for d, c in win if c is None or c <= 0]
    if bad:
        return out | {"reason": _why("bad_close", bad[0])}
    # 端点是有 bar 的日期，不可能在 missing 里 ⇒ 严格不等就是「窗内」
    gaps = sorted(d for d in missing if win[0][0] < d < win[-1][0])
    if gaps:
        return out | {"reason": _why("gap_in_window", ",".join(gaps))}
    for (_d0, c0), (d1, c1) in zip(win[:-1], win[1:]):
        if abs(math.log(c1 / c0)) >= JUMP_ABS:
            return out | {"reason": _why("suspect_jump", d1)}
    v = rv_fn([c for _d, c in win])
    if v is None:
        return out | {"reason": _why("rv_rejected")}
    return out | {"value": pct_to_dec(v)}


def bars_context(bars: dict, *, today: date, deps: Deps) -> Tuple[dict, List[Tuple[str, Optional[float]]], frozenset]:
    """日线 → (整体状态, 可用于取窗的 (date, close), 缺失的交易日)。不可用时后两项为空。"""
    ctx = {"available": False, "reason": None, "source": (bars or {}).get("source"), "last_date": None,
           "gaps_in_windows": [], "lag_sessions": None}
    if not (bars or {}).get("available"):
        return ctx | {"reason": (bars or {}).get("reason") or _why("bars_unavailable")}, [], frozenset()
    rows, why = clean_bars(bars.get("bars") or [])
    if why or not rows:
        return ctx | {"reason": why or _why("no_bars")}, [], frozenset()
    last = rows[-1][0]
    gaps = deps.find_gaps([d for d, _c in rows], today=today)
    missing = frozenset(gaps.get("missing") or [])
    # 两件不同的事分开报（v0.45.492 二次检查）：find_gaps 的 critical 把二者混在一起——
    #   · 末根之后缺的交易日（find_gaps 把末端补到「上一个已完成交易日」）= 日线**落后**几个交易日：窗口照算、但数据旧；
    #   · 末根之前、最长 RV 窗口覆盖范围内缺的 = **窗内缺口**：跨过它的窗口不可得。
    lag = sum(1 for d in missing if d > last)
    oldest = rows[-(RV_N_MAX + 1)][0] if len(rows) > RV_N_MAX else rows[0][0]
    ctx |= {"last_date": last, "lag_sessions": lag,
            "gaps_in_windows": sorted(d for d in missing if oldest < d < last)}
    if lag > BARS_MAX_LAG_SESSIONS:
        return ctx | {"reason": _why("bars_stale", lag)}, [], missing
    return ctx | {"available": True}, rows, missing


# ─────────────────────────────── 偏度 / 事件

def smile_reference(points: Sequence[Tuple[float, float, float]], strike: float, S: float) -> dict:
    """points：同到期同方向的 (行权价, 对数价内程度 k, IV)，按行权价升序。插值在 k 上做，排除自身行权价。"""
    out = {"iv_ref": None, "lo": None, "hi": None, "reason": None}
    k = math.log(strike / S)
    strikes = [p[0] for p in points]
    i = bisect.bisect_left(strikes, strike - 1e-9)
    j = bisect.bisect_right(strikes, strike + 1e-9)
    if i == 0 or j >= len(points):
        return out | {"reason": _why("no_bracketing_neighbors")}
    lo, hi = points[i - 1], points[j]
    if (k - lo[1]) > SKEW_MAX_LOG_DIST or (hi[1] - k) > SKEW_MAX_LOG_DIST:
        return out | {"reason": _why("neighbors_too_far")}
    return out | {"lo": lo[0], "hi": hi[0], "iv_ref": lo[2] + (hi[2] - lo[2]) * (k - lo[1]) / (hi[1] - lo[1])}


def _propagate(out: Dict[str, dict], segs: List[Tuple[str, str, Optional[bool], Optional[float]]]) -> None:
    """三态「含事件」：段 = (起, 止, 判定, 波动)，事件落在 (起, 止] 里。对每个到期日 e：
       · 有疑似段止于 e 之前（含）⇒ True（event_move = 这些段里最大的波动）；
       · 否则，有判不了的段止于 e 之前（含）、或有任一（疑似 / 判不了）段把 e 夹在段内、或 e 在最后一段之后 ⇒ None；
       · 否则 False（e 之前的每一段都判过且干净）。
    「判不了」与「疑似」同样向后传播：e 之后的到期日都含着那一段。"""
    last_end = segs[-1][1]
    for exp, rec in out.items():
        before = [s for s in segs if s[1] <= exp]
        around = [s for s in segs if s[0] < exp < s[1]]
        hits = [m for _st, _en, state, m in before if state is True]
        if hits:
            rec["event_inside"], rec["event_move"] = True, max(hits)
        elif (any(state is None for _st, _en, state, _m in before)
              or any(state is not False for _st, _en, state, _m in around) or exp > last_end):
            rec["event_inside"] = None
        else:
            rec["event_inside"] = False


def forward_kinks(atm: Sequence[dict], *, as_of: str, deps: Deps) -> Dict[str, dict]:
    """atm：按到期升序的 {"expiry","dte","atm_iv"}。返回 {expiry: 事件判定}。

    status：evaluated（参与了判定）/ not_evaluated（DTE 太短、无平值 IV、离上一个参与者太近）/
    insufficient_expiries（可判定的到期日不足两个，整条期限结构判不了）。
    suspect：True 疑似 / False 判过、不疑似 / None 判不了（倒挂、没有参照段）。
    """
    out: Dict[str, dict] = {}
    kept: List[dict] = []
    for e in atm:
        rec = {"status": "not_evaluated", "reason": None, "fwd_vol": None, "implied_event_move": None,
               "suspect": None, "event_inside": None, "event_move": None}
        out[e["expiry"]] = rec
        if _num(e.get("dte")) is None or e["dte"] < KINK["min_dte"]:
            rec["reason"] = _why("dte_too_short")
        elif _pos(e.get("atm_iv")) is None:
            rec["reason"] = _why("no_atm_iv")
        elif kept and (e["dte"] - kept[-1]["dte"]) < KINK["min_gap_days"]:
            rec["reason"] = _why("too_close_to_previous")
        else:
            kept.append(e)
    if len(kept) < 2:
        for rec in out.values():
            rec["status"], rec["reason"] = "insufficient_expiries", rec["reason"] or _why("insufficient_expiries")
        return out

    # 逐段前向方差（min_gap_days > 0 ⇒ 相邻参与者的 T 严格递增，ΔT > 0）
    seg = []
    prev_V, prev_T, prev_sig = 0.0, 0.0, 0.0
    for e in kept:
        T, sig = deps.year_fraction(e["dte"]), e["atm_iv"]
        V = sig * sig * T
        v = (V - prev_V) / (T - prev_T)
        seg.append({"e": e, "T": T, "sig": sig, "dT": T - prev_T, "v": v, "prev_T": prev_T, "prev_sig": prev_sig,
                    "f": math.sqrt(v) if v > 0 else None})
        prev_V, prev_T, prev_sig = V, T, sig

    for i, s in enumerate(seg):
        rec = out[s["e"]["expiry"]]
        rec["status"], rec["fwd_vol"] = "evaluated", s["f"]
        if s["f"] is None:
            rec["reason"] = _why("inverted_term_structure")         # 数据问题，不是「判过、干净」
            continue
        nb = [seg[j]["f"] for j in (i - 1, i + 1) if 0 <= j < len(seg) and seg[j]["f"] is not None]
        if not nb:
            rec["reason"] = _why("no_reference_segment")
            continue
        ref = sum(nb) / len(nb)
        excess = (s["v"] - ref * ref) * s["dT"]
        # 误差传播：d(σ²T)/dσ = 2σT；两端平值 IV 各有 iv_tol 的误差、彼此独立 ⇒ hypot
        noise = 2.0 * KINK["iv_tol"] * math.hypot(s["sig"] * s["T"], s["prev_sig"] * s["prev_T"])
        move = math.sqrt(excess) if excess > 0 else 0.0
        rec["implied_event_move"] = move
        rec["suspect"] = bool(excess >= KINK["noise_mult"] * noise and move >= KINK["min_move"])

    ends = [s["e"]["expiry"] for s in seg]
    starts = [as_of] + ends[:-1]
    _propagate(out, [(st, en, out[en]["suspect"], out[en]["implied_event_move"]) for st, en in zip(starts, ends)])
    return out


# ─────────────────────────────── 账外的三份本地数据（service 读、这里只判）

def earnings_state(entry: Optional[dict], err: Optional[str], *, as_of: str, deps: Deps) -> dict:
    """财报日 ← 最近一次有效扫描写进卖权账本的 Chronos 日期（与卖权页、日报同一个来源）。

    entry = {"scan_date", "earnings_date", "earnings_status", "source"}（那天该票任一 recorded 行；None = 最近的扫描里没有）。
    **不读** `earnings_cache/{T}_date.json`：那是兜底专用缓存、扫描几乎不写，2026-10-13 实测 NEE / CRCL 的缓存日期
    比扫描的晚一周以上——「财报在到期前」会被判成「到期后」，正是卖方最怕的那个方向。
    status：known（有日期）/ none（扫描视野内没有财报，或记下的那次已过）/ unknown（判不了）。
    none 只是给页头的说明：扫描只知道「下一次」，none 时下一次在哪不知道 ⇒ 到期日一律判不了（见 `_expiry_rows`）。"""
    out = {"status": "unknown", "date": None, "scan_date": None, "lag_sessions": None, "source": None, "reason": None}
    if err:
        return out | {"reason": err}
    if entry is None:
        return out | {"reason": _why("not_in_recent_scans")}
    out |= {"scan_date": entry.get("scan_date"), "source": entry.get("source"),
            "lag_sessions": _lag_sessions(entry.get("scan_date"), as_of, deps)}
    if out["lag_sessions"] is not None and out["lag_sessions"] > EARNINGS_MAX_SCAN_LAG_SESSIONS:
        return out | {"reason": _why("scan_too_old", out["lag_sessions"])}
    if entry.get("earnings_status") in (None, "unknown"):
        return out | {"reason": _why("scan_had_no_earnings_info")}
    raw_d = entry.get("earnings_date")
    if raw_d is None:
        return out | {"status": "none"}
    d = _date(raw_d) if isinstance(raw_d, str) else None
    if d is None:
        return out | {"reason": _why("unparseable_date", repr(raw_d))}
    out["date"] = d.isoformat()
    a = _date(as_of)
    return out | {"status": "none" if (a is not None and d < a) else "known"}


def earnings_observation(rows: Sequence[dict], *, ticker: str, lookback: int) -> Optional[dict]:
    """卖权账本行（两个 tenor 混在一起）→ 该票最近一次**有财报信息**的扫描观测（喂 `earnings_state`）。

    只看 status == recorded 的行（扫描失败那天是 unavailable）；在该票最近 `lookback` 个有记录的日期里，
    跳过 earnings_status 为 None / unknown 的日子（那天 Chronos 与兜底都失败）往回找——最新一天失败不该
    盖掉前一天拿到的日期。都没有信息 ⇒ 返回最新那天（earnings_state 判「那次扫描没拿到财报信息」）；
    一行都没有 ⇒ None（「最近的扫描里没有这只票」）。"""
    mine = [r for r in rows if isinstance(r, dict) and str(r.get("ticker") or "").upper() == ticker
            and r.get("status") == "recorded" and _date(r.get("date")) is not None]
    if not mine:
        return None
    dates = sorted({str(r["date"])[:10] for r in mine}, reverse=True)[:lookback]

    def entry(r):
        return {"scan_date": str(r["date"])[:10], "earnings_date": r.get("earnings_date"),
                "earnings_status": r.get("earnings_status"), "source": r.get("earnings_source")}
    for d in dates:
        good = [r for r in mine if str(r["date"])[:10] == d and r.get("earnings_status") not in (None, "unknown")]
        if good:
            return entry(good[0])
    return entry(next(r for r in mine if str(r["date"])[:10] == dates[0]))


def iv_rank_block(index: Optional[Mapping[str, float]], err: Optional[str], *, as_of: str, deps: Deps) -> dict:
    """IV 历史（百分数，{date: iv}）→ IV Rank。拿历史**自己的**最新一条排，不拿 CBOE iv30。"""
    out = {"state": "unavailable", "reason": err, "n": 0, "min_days": deps.iv_rank_min_days,
           "window": deps.iv_rank_window, "current": None, "current_date": None, "lag_sessions": None,
           "lo": None, "hi": None, "rank": None, "percentile": None}
    if err or index is None:
        return out | {"reason": err or _why("no_index_file")}
    dates = sorted(d for d in index if _date(d) is not None)[-deps.iv_rank_window:]
    vals = [float(index[d]) for d in dates]
    out["n"] = len(vals)
    if not vals:
        return out | {"reason": _why("empty_index")}
    out |= {"current": pct_to_dec(vals[-1]), "current_date": dates[-1], "lo": pct_to_dec(min(vals)),
            "hi": pct_to_dec(max(vals)), "lag_sessions": _lag_sessions(dates[-1], as_of, deps)}
    if len(vals) < deps.iv_rank_min_days:
        return out | {"state": "insufficient", "reason": _why("insufficient_samples")}
    if out["lag_sessions"] is not None and out["lag_sessions"] > IV_RANK_MAX_LAG_SESSIONS:
        return out | {"state": "stale", "reason": _why("stale_observation", out["lag_sessions"])}
    rank, pctile = deps.rank_fn(vals[-1], vals)
    if rank is None:
        return out | {"state": "degenerate", "reason": _why("flat_history")}
    return out | {"state": "ready", "reason": None, "rank": rank, "percentile": pctile}


def vrp_block(rows: Optional[List[dict]], err: Optional[str], *, ticker: str, as_of: str, deps: Deps) -> dict:
    """`vrp_signal` 账本里这只票最新一行（IV−RV 在它自己历史里的分位；攒够 vrp_min_obs 才有分位）。"""
    out = {"state": "unavailable", "reason": err, "date": None, "iv_pct": None, "rv30_pct": None, "vrp_pts": None,
           "n_obs": None, "min_obs": deps.vrp_min_obs, "ts_pct": None, "label": None, "lag_sessions": None}
    if err or rows is None:
        return out | {"reason": err or _why("ledger_unreadable", "no_rows")}
    mine = [r for r in rows if isinstance(r, dict) and str(r.get("ticker") or "").upper() == ticker
            and _date(r.get("date")) is not None]
    if not mine:
        return out | {"reason": _why("no_rows_for_ticker")}
    r = max(mine, key=lambda x: str(x.get("date")))
    out |= {"date": str(r.get("date"))[:10], "iv_pct": _num(r.get("iv")), "rv30_pct": _num(r.get("rv_30d")),
            "vrp_pts": _num(r.get("vrp_ex_ante")), "n_obs": r.get("n_obs"), "ts_pct": _num(r.get("ts_pct")),
            "label": r.get("label"), "lag_sessions": _lag_sessions(r.get("date"), as_of, deps)}
    if r.get("reason"):
        return out | {"state": "missing_inputs", "reason": r.get("reason")}      # vrp_signal 自己的码（EXTERNAL_REASONS）
    if r.get("ready") and out["ts_pct"] is not None:
        return out | {"state": "ready", "reason": None}
    return out | {"state": "insufficient", "reason": _why("insufficient_samples")}


def unavailable_blocks(reason: str, *, ticker: str, as_of: str, deps: Deps) -> dict:
    """三份本地数据都不读时（演示模式 / 测试）的同形占位。"""
    return {"earnings": earnings_state(None, reason, as_of=as_of, deps=deps),
            "iv_index": iv_rank_block(None, reason, as_of=as_of, deps=deps),
            "vrp": vrp_block(None, reason, ticker=ticker, as_of=as_of, deps=deps)}


# ─────────────────────────────── 组装

def _stale_last_trade(contract_ltt, payload_ltt) -> Optional[bool]:
    """末笔成交早于最新场次？（bid/ask 是即时报价，这只说明这张最近一场没成交）缺一边 ⇒ None。"""
    a, b = _date(contract_ltt), _date(payload_ltt)
    if a is None or b is None:
        return None
    return a < b


_SIDE = {"P": "put", "C": "call"}


def _fixed_gates(contracts: List[dict], S: float, deps: Deps) -> Tuple[List[Tuple[dict, dict, str, float]], List[dict], Dict[str, float]]:
    """一次遍历判完全部固定闸（与查询参数无关）。返回 (通过者 [(合约, 单腿, 到期日, dte)], 漏斗, {到期日: dte})。
    到期日 / DTE 的合法性与「进不进到期日表」是**同一次**判定——通过闸的合约必然在到期日表里。"""
    n = {k: 0 for k in ("valid_expiry", "within_max_dte", "priced", "quote_gate", "spread_gate")}
    passed, dte_of = [], {}
    for c in contracts:
        e, dte = str(c.get("expiry")), _num(c.get("dte"))
        if _date(e) is None or len(e) != 10 or dte is None:
            continue
        n["valid_expiry"] += 1
        if not DTE_BOUNDS[0] <= dte <= DTE_BOUNDS[1]:
            continue
        n["within_max_dte"] += 1
        dte_of.setdefault(e, dte)
        leg = deps.leg(c, S)
        if leg.get("strike") is None or leg.get("iv") is None or leg.get("delta") is None or leg.get("cp") not in _SIDE:
            continue
        n["priced"] += 1
        if not leg.get("quote_ok"):
            continue
        n["quote_gate"] += 1
        if (leg.get("spread_pct") or 0.0) - deps.max_spread_pct > SPREAD_EPS:
            continue
        n["spread_gate"] += 1
        passed.append((c, leg, e, dte))
    funnel = [{"stage": "chain", "n": len(contracts)}] + [{"stage": k, "n": v} for k, v in n.items()]
    return passed, funnel, dte_of


def _expiry_rows(contracts, S, as_of, dte_of, *, bars: dict, earnings: dict, today: date, deps: Deps):
    """逐到期日：交易日数 / 匹配 RV / 平值 IV / 财报 / 事件。返回 ({到期日: 行}, 页头 RV 与日线状态)。"""
    expiries = sorted(dte_of)
    td_iso = [d.isoformat() for d in deps.trading_days(_date(as_of) + timedelta(days=1), _date(expiries[-1]))] \
        if expiries else []
    bctx, rows, missing = bars_context(bars, today=today, deps=deps)
    rv_cache: Dict[int, dict] = {}

    def rv_of(n: int) -> dict:
        if n not in rv_cache:
            rv_cache[n] = (rv_window(rows, n, missing=missing, rv_fn=deps.rv_from_closes) if bctx["available"]
                           else _rv_unavailable(n, bctx["reason"]))
        return rv_cache[n]

    atm = [{"expiry": e, "dte": dte_of[e], "atm_iv": deps.atm_iv(contracts, S, e)} for e in expiries]
    kinks = forward_kinks(atm, as_of=as_of, deps=deps)
    # 扫描只知道**下一次**财报日（ChronosBee 只取 days_until ≥ 0 的那一个）。known ⇒ 按它分（之前 False、之后 True）；
    # 其余——包括「视野内没有」与「记下的那次已过」——都不知道下一次在哪，**每个到期日都判不了（None）**。
    # 不能像卖权账本那样把「none」当成「到期前没有财报」：本页 DTE 可到 365 天，120 天的到期日必然含下一次财报。
    earn_info = {"earnings_date": earnings.get("date")} if earnings.get("status") == "known" else None
    out = {}
    for x in atm:
        e = x["expiry"]
        n_td = bisect.bisect_right(td_iso, e)
        rv = rv_of(matched_n(n_td))
        k = kinks[e]
        out[e] = {"expiry": e, "dte": x["dte"], "n_td": n_td, "rv_n": rv["n"], "rv": rv["value"],
                  "rv_reason": rv["reason"], "rv_start": rv["start"], "rv_end": rv["end"], "atm_iv": x["atm_iv"],
                  "atm_iv_over_rv": (x["atm_iv"] / rv["value"]) if (x["atm_iv"] and rv["value"]) else None,
                  "kink": {key: k[key] for key in ("status", "reason", "fwd_vol", "implied_event_move", "suspect")},
                  "event_inside": k["event_inside"], "event_move": k["event_move"],
                  "earnings_before_expiry": {"before_expiry": True, "after_expiry": False, "none": False}.get(
                      deps.classify_earnings(earn_info, as_of, e)),
                  "dte_le_2": x["dte"] <= LOW_DTE, "tenor_exceeds_rv_window": n_td > RV_N_MAX}
    r30 = rv_of(HEADLINE_RV[1])
    return out, {"rv20": rv_of(HEADLINE_RV[0]), "rv30": r30}, bctx


def _contract_row(c: dict, leg: dict, e: str, dte: float, er: dict, ref: dict, S: float, payload_ltt) -> dict:
    K, iv, side = leg["strike"], leg["iv"], _SIDE[leg["cp"]]
    collateral = K if side == "put" else S              # 同 structure_quote：put 现金担保 K、call 备兑名义 S
    y_raw = leg["bid"] / collateral
    rv = er["rv"]
    return {
        "symbol": leg.get("symbol"), "expiry": e, "dte": dte, "side": side, "strike": K,
        "delta": leg["delta"], "abs_delta": abs(leg["delta"]), "bid": leg["bid"], "ask": leg["ask"],
        "spread_pct": leg.get("spread_pct"), "iv": iv, "rv": rv, "rv_n": er["rv_n"],
        "iv_over_rv": (iv / rv) if rv else None, "iv_minus_rv_pts": ((iv - rv) * 100) if rv else None,
        "skew_pts": ((iv - ref["iv_ref"]) * 100) if ref["iv_ref"] is not None else None,
        "skew_reason": ref["reason"], "ref_strikes": [ref["lo"], ref["hi"]] if ref["iv_ref"] is not None else None,
        "yield_raw": round(y_raw, YIELD_DP), "yield_ann": round(y_raw * 365.0 / dte, YIELD_DP) if dte > 0 else None,
        "collateral": collateral, "sigma_distance": leg.get("sigma_distance"),
        "oi": _num(c.get("oi")), "volume": _num(c.get("volume")), "event_move": er["event_move"],
        "flags": {"earnings_before_expiry": er["earnings_before_expiry"], "event_inside": er["event_inside"],
                  "stale_last_trade": _stale_last_trade(c.get("last_trade_time"), payload_ltt),
                  "dte_le_2": er["dte_le_2"], "tenor_exceeds_rv_window": er["tenor_exceeds_rv_window"]},
    }


def build(raw: dict, *, bars: dict, earnings: dict, iv_index: dict, vrp: dict, today: date, deps: Deps) -> dict:
    """与查询参数无关的全部计算（可缓存）。raw = 原始链 payload；earnings / iv_index / vrp 已由各 *_block 判好。"""
    S = _pos(raw.get("underlying_price"))
    as_of = chain_as_of(raw)
    if S is None:
        return {"data_available": False, "reason": _why("underlying_price_unavailable")}
    if _date(as_of) is None:
        return {"data_available": False, "reason": _why("as_of_unavailable")}
    contracts = [c for c in raw.get("contracts") or [] if isinstance(c, dict)]
    payload_ltt = raw.get("payload_last_trade_time")

    passed, funnel, dte_of = _fixed_gates(contracts, S, deps)
    exp_rows, headline, bctx = _expiry_rows(contracts, S, as_of, dte_of, bars=bars, earnings=earnings, today=today,
                                            deps=deps)

    smiles: Dict[Tuple[str, str], List[Tuple[float, float, float]]] = {}
    for _c, leg, e, _dte in passed:
        smiles.setdefault((e, leg["cp"]), []).append((leg["strike"], math.log(leg["strike"] / S), leg["iv"]))
    for v in smiles.values():
        v.sort(key=lambda p: p[0])
    rows = [_contract_row(c, leg, e, dte, exp_rows[e], smile_reference(smiles[(e, leg["cp"])], leg["strike"], S), S,
                          payload_ltt) for c, leg, e, dte in passed]

    iv30, r30 = pct_to_dec(raw.get("iv30")), headline["rv30"]
    return {
        "data_available": True, "reason": None,
        "chain": {"as_of": as_of, "underlying_price": S, "underlying_price_source": raw.get("underlying_price_source"),
                  "payload_last_trade_time": payload_ltt, "session_live": raw.get("session_live")},
        "headline": headline | {"iv30": iv30, "iv30_over_rv30": (iv30 / r30["value"]) if (iv30 and r30["value"]) else None},
        "bars": bctx, "earnings": earnings, "history": {"iv_rank": iv_index, "vrp": vrp},
        "funnel_fixed": funnel, "expiries": [exp_rows[e] for e in sorted(exp_rows)], "rows": rows,
    }


def _sort_key(sort: str):
    def key(r: dict):
        v = r.get(sort)
        return (v is None, -(v or 0.0), -(r.get("iv") or 0.0), r.get("expiry") or "", r.get("strike") or 0.0)
    return key


def select(full: dict, p: dict) -> dict:
    """按查询参数：过滤 → 排序 → 截断（这个顺序是本函数存在的理由）+ 漏斗。不改 `full`。
    p["expiry"] 不是链上的到期日 ⇒ ParamError(param="expiry")。"""
    if not full.get("data_available"):
        return dict(full)
    exps = {e["expiry"] for e in full["expiries"]}
    if p.get("expiry") and p["expiry"] not in exps:
        raise ParamError(f"链上没有到期日 {p['expiry']}", "expiry")
    if p.get("expiry"):
        dte = next(e["dte"] for e in full["expiries"] if e["expiry"] == p["expiry"])
        if not p["dte_min"] <= dte <= p["dte_max"]:
            raise ParamError(f"到期日 {p['expiry']}（{num_s(dte)} 天）不在天数范围 {p['dte_min']}–{p['dte_max']} 内", "expiry")
    funnel = [dict(s) for s in full["funnel_fixed"]]
    rows = full["rows"]
    stages = [("dte", lambda r: p["dte_min"] <= r["dte"] <= p["dte_max"]),
              ("side", lambda r: p["side"] == "both" or r["side"] == p["side"]),
              ("delta", lambda r: p["delta_min"] <= r["abs_delta"] <= p["delta_max"]),
              ("oi", lambda r: (r["oi"] or 0.0) >= p["oi_min"])]
    for name, keep in stages:
        rows = [r for r in rows if keep(r)]
        funnel.append({"stage": name, "n": len(rows)})
    per_exp: Dict[str, int] = {}
    for r in rows:                                   # 到期日表的计数在「只看一个到期日」之前数
        per_exp[r["expiry"]] = per_exp.get(r["expiry"], 0) + 1
    if p.get("expiry"):
        rows = [r for r in rows if r["expiry"] == p["expiry"]]
        funnel.append({"stage": "expiry", "n": len(rows)})
    first_zero = next((s["stage"] for s in funnel if s["n"] == 0), None)
    rows = sorted(rows, key=_sort_key(p["sort"]))
    expiries = [{**e, "in_window": p["dte_min"] <= e["dte"] <= p["dte_max"], "n_rows": per_exp.get(e["expiry"], 0)}
                for e in full["expiries"]]
    out = {k: v for k, v in full.items() if k not in ("rows", "expiries", "funnel_fixed")}
    out.update({"params": dict(p), "funnel": funnel,
                "empty_reason": "no_contracts" if first_zero == "chain" else first_zero, "expiries": expiries,
                "rows": rows[:MAX_ROWS], "rows_total": len(rows), "truncated": len(rows) > MAX_ROWS,
                "max_rows": MAX_ROWS})
    return out
