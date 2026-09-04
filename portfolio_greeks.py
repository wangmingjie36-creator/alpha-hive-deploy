#!/usr/bin/env python3
"""
组合 Greeks 聚合 + β·Delta 带状对冲 + 现货×IV 联合压力测试（v0.45.103，期权路线图第 5 步 / 收尾）
=====================================================================================
做什么
------
把三本账**读到一张风险表上**，然后只做一件动作——用 SPY 股票把组合的 β·$Delta 拉回带内：
    ① 股票纸面组合   paper_portfolio.POSITIONS_FILE      （只读，不改它）
    ② 财报跨式腿     options_paper_leg.POSITIONS_FILE    （只读，不改它；两腿用 CBOE 重新报价）
    ③ 本模块自己的 SPY 覆盖账本  hedge_state/            （唯一会写的账本）
逐行算 $Delta / β·$Delta / $Gamma(1% 移动) / $Vega(每 vol 点) / $Theta(每日)，按合并 NAV 折成百分比，
给出 β·Delta 带状对冲建议，并把 现货冲击 × IV 冲击 的联合压力网格一起落盘审计。

为什么是带状（band）而不是连续对冲
------------------------------------
连续 delta 对冲在纸面上"最干净"，实盘里是**换手机器**：每天按残余 delta 交易，成本
与噪音成正比、与信息无关。这里的 β·$Delta 里装着股票纸面组合十几条持仓的方向观点（那是这本账
存在的意义），只有当组合整体的市场暴露超过 ±band（默认 NAV 的 15%）时才动手，
且只拉回到**带边**（`rebalance_to="edge"`，可切 "center"）——保留观点、砍掉尾巴。
带的宽度是 v1 的主观常数，不是校准结果；等 hedge_state/ 攒够天数再看该不该改。

为什么 v1 用 SPY 股票而不是指数认沽
------------------------------------
SPY 股票的对冲比 = β·$Delta / SPY 价，一行算式，无到期、无 IV 敞口、无滚仓，
点差 ~1 bp 可以忽略（本模块**没有点差模型**，成交价=当日收盘，下面明说）。
指数认沽会引入第二组 Greeks（对冲工具自己的 vega/theta），把「测量组合暴露」这件
事和「押 IV 方向」混在一起；等 vrp_signal（v0.45.102）证明卖/买 vol 有边际再谈。

CBOE Greeks 单位（2026-09-03 用 NVDA 实盘报价对 greeks_engine 校核，结论写死在这里）
-----------------------------------------------------------------------------------
    NVDA S=227.19，225C 2026-09-25（22 DTE）iv=0.3162：
        CBOE  delta 0.5736  gamma 0.0221  vega 0.2192  theta −0.1543
        BS    delta 0.5787  gamma 0.0222  vega 0.2182  theta −0.1720   （T=22/365, r=4.5%）
    225C 2026-10-02（29 DTE）iv=0.3207：CBOE vega 0.2517 / BS 0.2508；theta −0.1346 / BS −0.1536
  → **vega 是「每 1 个 vol 点（IV 变 0.01）」的每股价格变化**（若按每 1.00 vol 算应是 ~22，
    一张 $8 的期权不可能）；**theta 是「每日历日」的每股价格变化，多头为负**（CBOE 比 BS
    约小 10%，是日算法/股息约定差异，不是单位差异）；delta/gamma 每股，与 BS 差 <1%。
  所以：$Vega/pt = qty × vega；$Theta/日 = qty × theta；$Gamma(1%) = ½ · qty · gamma · (0.01·S)²。

诚实降级
--------
- β **不用** risk_engine._estimate_beta（失败时静默返回 1.0，与真实 β=1.0 同形——本项目
  明令禁止的「安全默认值」）。自己用 60 个交易日对 SPY 的对数收益 OLS 算，算不出就是
  `(None, None)`，聚合结果标 `partial=True`、`band_status="unknown"`，**不在部分数据上对冲**。
- 价格/报价/β 缺一行就少一行，`coverage` 里逐项计数；所有数值过 `_num()`（`bool(nan) is True`，
  真值判断挡不住 NaN），落盘前再 `_scrub()` 一遍。
- 合并 NAV 三个分量（股票账 / 跨式账 / SPY 覆盖）任一缺失 → None 并说出缺哪个。
- 压力网格里 β 缺失的行**剔除**并把该格标 `partial`，不用 1.0 顶上；剔除行的毛 |$Delta|
  记在 `excluded_dollar_delta` 并挂到 `worst_cell` 上——最差格那个数字会被单独引用，
  必须自带「少算了多少」。已到期（dte<=0）的合约同样剔除，不按 T=1/365 冒充活合约。
- IV 轴被 0 地板托住的格标 `iv_clamped` + 实际施加的 `iv_pts_effective`（8% IV 的票吃不下 −10pt）。
- 「从未启动」必须由净值文件与持仓文件**双双为空**证明；有持仓没净值行 = 数据缺失 → None。
- 账本里 shares 为 null/NaN 的行标成残行、不丢弃——丢掉之后 band_status 会报 "empty"。

没做的事（已知局限）
--------------------
- 没有 SPY 点差/冲击成本模型（~1 bp，明说不建模）；没有融资利息（做空 SPY 的现金记正）。
- 对冲工具只有 SPY 股票；不做指数认沽、不做逐票 delta 对冲、不对冲 vega/gamma（只告警）。
- 期权重定价用 BS 平价面（每张合约各用自己的 IV 平移），不建模 skew 变化、不建模股息。
- 股票价用 Twelve Data 收盘（与 paper_portfolio 的 yfinance 收盘可能差几分钱，口径不同）。
- 压力网格里期权的 (0,0) 格不是 0：BS 价 − 市场 mid 的模型基差按合约单列在 `bs_vs_mid_gap`，
  不强行归零——那是模型诊断，不是 P&L。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from hive_logger import PATHS, get_logger

_log = get_logger("portfolio_greeks")

CONFIG = {
    "beta_delta_band_pct": 15.0,     # β·$Delta 允许偏离 target 的带宽（% NAV）
    "beta_delta_target_pct": 0.0,    # 带中心（% NAV）；0 = 市场中性
    "vega_alert_pct": 1.0,           # |$Vega/pt| 超过 NAV 的 1% → 告警（不对冲）
    "gamma_alert_pct": 0.5,          # |$Gamma(1%)| 超过 NAV 的 0.5% → 告警（不对冲）
    "hedge_instrument": "SPY",
    "rebalance_to": "edge",          # edge = 拉回最近带边 | center = 拉回带中心
    "stress_spot_pct": [-10, -5, 0, 5, 10],
    "stress_iv_pts": [-10, 0, 10, 20],
    "risk_free": 0.045,
    "beta_window": 60,               # OLS 用的交易日数
    "beta_cache_trading_days": 5,    # β 缓存有效期（交易日）
}

BASE_DIR = PATHS.home
STATE_DIR = BASE_DIR / "hedge_state"
POSITIONS_FILE = STATE_DIR / "positions.jsonl"
TRADES_FILE = STATE_DIR / "trades.jsonl"
EQUITY_FILE = STATE_DIR / "equity_curve.jsonl"
META_FILE = STATE_DIR / "meta.json"
BETA_CACHE_FILE = STATE_DIR / "beta_cache.json"

_VERSION = "0.45.103"
_GREEK_KEYS = ("dollar_delta", "beta_dollar_delta", "gamma_dollar_per_1pct",
               "vega_dollar_per_pt", "theta_dollar_per_day")


# ══════════════════════════════════════════════════════════════════════════════
# 数值守卫 / 小工具
# ══════════════════════════════════════════════════════════════════════════════

def _num(v) -> Optional[float]:
    """float 且有限 → float；否则 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v) -> Optional[float]:
    f = _num(v)
    return f if f is not None and f > 0 else None


def _scrub(obj, _path: str = ""):
    """落盘前最后一道闸：任何非有限 float 变 None 并 error 日志。"""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _log.error("[PortfolioGreeks] 非有限值被拦在落盘前：%s=%r → None", _path, obj)
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, f"{_path}.{k}") for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, f"{_path}[{i}]") for i, v in enumerate(obj)]
    return obj


def _r(v, nd=2) -> Optional[float]:
    f = _num(v)
    return round(f, nd) if f is not None else None


def _days_between(d1: str, d2: str) -> Optional[int]:
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


def _weekdays_between(d0: str, d1: str) -> Optional[int]:
    """(d0, d1] 内的工作日数；d1 < d0 → 负数（调用方据此拒绝「来自未来」的缓存）。"""
    try:
        a = datetime.strptime(d0, "%Y-%m-%d").date()
        b = datetime.strptime(d1, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    if b < a:
        return -1
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


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
            _log.error("[PortfolioGreeks] meta.json 损坏，按新账本处理")
    return {"version": _VERSION, "starting_date": None, "cash": 0.0,
            "last_run_date": None, "config_snapshot": dict(CONFIG)}


def _save_meta(meta: Dict) -> None:
    _atomic_write_text(META_FILE, json.dumps(_scrub(meta), ensure_ascii=False, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# 默认数据源（测试一律注入；生产走 Twelve Data，兜底本地价格索引）
# ══════════════════════════════════════════════════════════════════════════════

_BARS_CACHE: Dict[Tuple[str, str], Optional[List[dict]]] = {}
# v0.45.105：100 → 120，与 `twelve_data.SHARED_BARS_WINDOW` 对齐。β 仍然只用最后
# 61 根（`beta_window`+1），多出来的历史一根都用不上——提到 120 纯粹是为了让本模块、
# vrp_signal（它的 `settle_window_bars` 真的要 120 根）、options_paper_leg 三方
# **请求同一个窗口**，从而共享 `twelve_data` 的进程内缓存：窗口不齐的话，先跑的
# 小窗口喂不饱后跑的大窗口，缓存等于白设。多取 20 根不多花配额（同一次请求的
# outputsize），Twelve Data 按调用次数计费、不按行数。
_BARS_WINDOW = 120


def _fetch_bars_uncached(ticker: str, as_of: str) -> Optional[List[dict]]:
    """真去取数的那一层（Twelve Data → 本地价格索引），**不碰缓存**。
    单独拆出来，是为了让「同一 (ticker, as_of) 只取一次」这件事能被测试直接数到——
    否则缓存命中与否只能靠计时猜，而本项目的教训是「看着成功其实早废了」。"""
    rows: Optional[List[dict]] = None
    try:
        import twelve_data
        if twelve_data.is_configured():
            # v0.45.105：网络层的去重已经下沉到 twelve_data.fetch_bars
            #（按 (ticker, end_date) 记忆，三个消费方共享），本函数名里的
            # "uncached" 说的是**本模块这一层**不记忆，仍然成立。
            rows = twelve_data.fetch_bars(ticker, _BARS_WINDOW, end_date=as_of)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 取 K 线失败: %s", ticker, exc)
        rows = None
    if not rows:
        try:
            from price_history import load_price_history
            hist = load_price_history(ticker, str(PATHS.cache_dir))
            if hist:
                rows = [{"date": d, "close": c} for d, c in hist]
                _log.info("[%s] 用本地价格索引代替 Twelve Data 日线（%d 根）", ticker, len(rows))
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 本地价格索引读取失败: %s", ticker, exc)
    clean = []
    for b in rows or []:
        d = str(b.get("date") or "")[:10]
        c = _pos(b.get("close"))
        if d and c is not None and d <= as_of:
            clean.append({"date": d, "close": c})
    clean.sort(key=lambda b: b["date"])
    return clean or None


def daily_bars(ticker: str, as_of: str) -> Optional[List[dict]]:
    """截至 as_of（含）的日线 `[{date, close}]` 升序，进程内按 (ticker, as_of) 记忆一次。
    **本模块对外的取 K 线入口**（二次复查新增）：收盘价与 β 共用同一次 Twelve Data 调用
    （30 只票 + SPY ≈ 31 次/天，预算 800，且 Twelve Data 是串行 7 次/分钟）。
    Twelve Data 未配置/拿不到 → 本地价格索引（快照价，口径略不同，日志里说）。

    v0.45.105：这里的 `_BARS_CACHE` 记的是**归一化之后**的结果——按 as_of 截断、
    去掉坏行、排好序，还可能来自本地价格索引而不是 Twelve Data。它和网络层
    去重是两件事，所以两层都留着，但**不再各管各的**：
      · 本层（`_BARS_CACHE`）省的是重复的归一化 + 兜底分支，并且给 `_bars_in_memory`
        提供「重算 β 要不要网络」的判据；
      · 网络层（`twelve_data.fetch_bars`，按 `(ticker, end_date)` 记忆）省的是
        真正贵的东西——串行 7 次/分钟的 API 调用，且**跨模块共享**。
    从前 vrp_signal 与 options_paper_leg 各自裸调 `twelve_data._fetch_rows`，
    同一只票的日线一次扫描最多被取 3 遍；三方现在都走 `fetch_bars` 且都请求
    `SHARED_BARS_WINDOW`(=120) 根，只剩 1 遍。缓存没放在本模块，是因为
    本模块已经 import 了 options_paper_leg，反向 import 会成环；`twelve_data`
    是三方共同的叶子依赖，放那里谁都不欠谁。"""
    key = (ticker, as_of)
    if key in _BARS_CACHE:
        return _BARS_CACHE[key]
    _BARS_CACHE[key] = _fetch_bars_uncached(ticker, as_of)
    return _BARS_CACHE[key]


def _default_bars(ticker: str, as_of: str) -> Optional[List[dict]]:
    """内部别名 = daily_bars（测试注入的挂钩点，历史名字，别删）。"""
    return daily_bars(ticker, as_of)


def _bars_in_memory(ticker: str, as_of: str) -> bool:
    """这只票与基准的日线是否**已经在本进程缓存里**（即：重算 β 不需要任何网络调用）。"""
    return bool(_BARS_CACHE.get((ticker, as_of))) and bool(_BARS_CACHE.get((CONFIG["hedge_instrument"], as_of)))


def _default_close(ticker: str, as_of: str) -> Optional[float]:
    """as_of 当日（或 5 个日历日内最后一根）收盘；取不到 None。"""
    bars = _default_bars(ticker, as_of)
    if not bars:
        return None
    last = bars[-1]
    gap = _days_between(last["date"], as_of)
    if gap is None or gap > 5:
        return None
    return last["close"]


def _ols_beta(stock: List[dict], bench: List[dict], as_of: str, window: int) -> Optional[Tuple[float, int]]:
    """按日期对齐后取最后 window+1 根共同日线 → window 个对数收益 → 斜率 cov/var。
    共同日线不足 window+1 根 → None（不降级到更短窗口冒充 60 日 β）。"""
    b_by = {b["date"]: b["close"] for b in bench if b["date"] <= as_of}
    pairs = [(s["close"], b_by[s["date"]]) for s in stock if s["date"] <= as_of and s["date"] in b_by]
    pairs = pairs[-(window + 1):]
    if len(pairs) < window + 1:
        return None
    rs, rb = [], []
    for (s0, b0), (s1, b1) in zip(pairs[:-1], pairs[1:]):
        if min(s0, b0, s1, b1) <= 0:
            return None
        rs.append(math.log(s1 / s0))
        rb.append(math.log(b1 / b0))
    n = len(rb)
    mb, ms = sum(rb) / n, sum(rs) / n
    var = sum((x - mb) ** 2 for x in rb)
    if var <= 0:
        return None
    cov = sum((x - mb) * (y - ms) for x, y in zip(rb, rs))
    beta = cov / var
    return (beta, n) if math.isfinite(beta) else None


def _read_beta_cache() -> Dict:
    if BETA_CACHE_FILE.exists():
        try:
            d = json.loads(BETA_CACHE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


def _fresh_cached_beta(ticker: str, as_of: str) -> Optional[float]:
    """磁盘缓存里对 as_of 仍然有效的 β：计算日在 (as_of − 5 个交易日, as_of] 内。
    计算日晚于 as_of（补跑历史）一律不用——那是未来数据。"""
    ent = _read_beta_cache().get(ticker)
    if not isinstance(ent, dict):
        return None
    wd = _weekdays_between(str(ent.get("as_of") or ""), as_of)
    b = _num(ent.get("beta"))
    if b is not None and wd is not None and 0 <= wd < CONFIG["beta_cache_trading_days"]:
        return b
    return None


def _default_beta(ticker: str, as_of: str) -> Tuple[Optional[float], Optional[str]]:
    """60 日 OLS β 对 SPY。返回 (beta, source)，source ∈ {"ols60", "cache", None}。
    算不出 → (None, None)。

    磁盘缓存只在**日线拿不到时**兜底，不再抢在重算前面（二次复查修正）：走到这里时
    _default_close 早已把同一 (ticker, as_of) 的日线放进 _BARS_CACHE，OLS 不过是 60 次
    乘加——用缓存**一次网络调用都省不下**，却可能端上 4 个交易日前的 β。省错东西了。
    所以：日线已在内存 → 一律重算；日线取不到（限流/断网）→ 才退回缓存并标 source="cache"。"""
    if ticker == CONFIG["hedge_instrument"]:
        return 1.0, "benchmark"
    if not _bars_in_memory(ticker, as_of):
        b = _fresh_cached_beta(ticker, as_of)
        if b is not None:
            return b, "cache"
    stock = _default_bars(ticker, as_of)
    bench = _default_bars(CONFIG["hedge_instrument"], as_of)
    res = _ols_beta(stock, bench, as_of, CONFIG["beta_window"]) if (stock and bench) else None
    if res is None:
        b = _fresh_cached_beta(ticker, as_of)
        if b is not None:
            _log.warning("[%s] 日线不可得/不足，退回 %d 个交易日内的缓存 β", ticker, CONFIG["beta_cache_trading_days"])
            return b, "cache"
        if stock and bench:
            _log.warning("[%s] β 不可得：与 SPY 对齐的日线不足 %d 根", ticker, CONFIG["beta_window"] + 1)
        return None, None
    beta, n = res
    beta = round(beta, 4)
    cache = _read_beta_cache()          # 写回时才读盘：缓存的用途已只剩「日线断供时的兜底」
    cache[ticker] = {"as_of": as_of, "beta": beta, "n": n, "computed_at": as_of}
    try:
        _atomic_write_text(BETA_CACHE_FILE, json.dumps(_scrub(cache), ensure_ascii=False, indent=1))
    except OSError as exc:
        _log.warning("β 缓存写入失败（不影响本次结果）: %s", exc)
    return beta, f"ols{CONFIG['beta_window']}"


def _default_quotes(ticker: str, symbols: List[str]) -> Dict[str, Optional[dict]]:
    from cboe_options import quote_contracts
    return quote_contracts(ticker, symbols)


# ══════════════════════════════════════════════════════════════════════════════
# 逐行暴露
# ══════════════════════════════════════════════════════════════════════════════

def _blank_row(ticker: str, kind: str) -> Dict:
    return {"ticker": ticker, "kind": kind, "qty": None, "price": None,
            "dollar_delta": None, "beta": None, "beta_source": None, "beta_dollar_delta": None,
            "gamma_dollar_per_1pct": None, "vega_dollar_per_pt": None, "theta_dollar_per_day": None,
            "price_missing": False, "quote_missing": False, "beta_missing": False}


def _apply_beta(row: Dict, beta_fn, as_of: str) -> None:
    try:
        beta, src = beta_fn(row["ticker"], as_of)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[%s] β 计算异常: %s", row["ticker"], exc)
        beta, src = None, None
    beta = _num(beta)
    row["beta"], row["beta_source"] = beta, (src if beta is not None else None)
    row["beta_missing"] = beta is None
    dd = _num(row.get("dollar_delta"))
    row["beta_dollar_delta"] = round(dd * beta, 2) if (dd is not None and beta is not None) else None


def stock_exposures(as_of: str, positions: Optional[List[Dict]] = None,
                    closes_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                    beta_fn: Optional[Callable[[str, str], Tuple[Optional[float], Optional[str]]]] = None
                    ) -> List[Dict]:
    """股票纸面组合每条持仓一行：qty = +shares（bullish）/ −shares（bearish）。
    股票没有 gamma/vega/theta，那三项是真实的 0.0 而不是「未知」。"""
    closes_fn = closes_fn or _default_close
    beta_fn = beta_fn or _default_beta
    if positions is None:
        import paper_portfolio as pp
        positions = _load_jsonl(pp.POSITIONS_FILE)
    rows: List[Dict] = []
    for p in positions or []:
        tk = str(p.get("ticker") or "")
        row = _blank_row(tk, "stock")
        row.update({"direction": p.get("direction"), "entry_date": p.get("entry_date"),
                    "gamma_dollar_per_1pct": 0.0, "vega_dollar_per_pt": 0.0, "theta_dollar_per_day": 0.0})
        shares = _num(p.get("shares"))
        if not tk or shares is None:
            _log.warning("[PortfolioGreeks] 股票仓记录不完整（ticker=%r shares=%r），按缺价处理", tk, p.get("shares"))
            row["price_missing"] = True
            row["beta_missing"] = True
            rows.append(row)
            continue
        sign = -1.0 if str(p.get("direction")) == "bearish" else 1.0
        row["qty"] = round(sign * shares, 6)
        try:
            px = _pos(closes_fn(tk, as_of))
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 收盘价获取失败: %s", tk, exc)
            px = None
        row["price"] = px
        row["price_missing"] = px is None
        row["dollar_delta"] = round(row["qty"] * px, 2) if px is not None else None
        _apply_beta(row, beta_fn, as_of)
        rows.append(row)
    return rows


def option_exposures(as_of: str, positions: Optional[List[Dict]] = None,
                     quotes_fn: Optional[Callable[[str, List[str]], Dict[str, Optional[dict]]]] = None,
                     beta_fn: Optional[Callable[[str, str], Tuple[Optional[float], Optional[str]]]] = None,
                     closes_fn: Optional[Callable[[str, str], Optional[float]]] = None) -> List[Dict]:
    """跨式腿每张合约一行（一笔跨式两行）。qty = ±contracts×100（long +，short −）。
    Greeks 来自 CBOE 重新报价（单位见模块头）：
        $Delta      = qty × delta × S
        $Gamma(1%)  = ½ × qty × gamma × (0.01·S)²        —— 1% 移动的凸性 P&L
        $Vega/pt    = qty × vega                          —— CBOE vega 已是每 vol 点
        $Theta/日   = qty × theta                         —— CBOE theta 每日历日、多头为负
    报价缺失 → 该行 quote_missing、Greeks None（不沿用 last_mark 的任何东西）。
    S 用 closes_fn 的标的收盘（quote_contracts 不带标的价）；缺 S → price_missing。"""
    quotes_fn = quotes_fn or _default_quotes
    closes_fn = closes_fn or _default_close
    beta_fn = beta_fn or _default_beta
    if positions is None:
        import options_paper_leg as opl
        positions = _load_jsonl(opl.POSITIONS_FILE)
    rows: List[Dict] = []
    for p in positions or []:
        tk = str(p.get("ticker") or "")
        side = str(p.get("side") or "")
        n = _num(p.get("contracts"))
        legs = [("call", p.get("call_symbol")), ("put", p.get("put_symbol"))]
        try:
            quotes = quotes_fn(tk, [s for _, s in legs if s]) or {}
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 持仓重新报价失败: %s", tk, exc)
            quotes = {}
        try:
            S = _pos(closes_fn(tk, as_of))
        except Exception as exc:  # noqa: BLE001
            _log.warning("[%s] 标的收盘获取失败: %s", tk, exc)
            S = None
        sign = 1.0 if side == "long" else -1.0
        for cp, sym in legs:
            row = _blank_row(tk, "option")
            row.update({"symbol": sym, "cp": cp, "side": side, "strike": _num(p.get("strike")),
                        "expiry": p.get("expiry"), "dte": _days_between(as_of, str(p.get("expiry") or "")),
                        "mid": None, "iv": None, "delta": None, "gamma": None, "vega": None, "theta": None})
            row["price"], row["price_missing"] = S, S is None
            if n is None or n <= 0 or not sym:
                row["quote_missing"] = True
                _apply_beta(row, beta_fn, as_of)
                rows.append(row)
                continue
            row["qty"] = sign * n * 100.0
            q = quotes.get(sym)
            ok = isinstance(q, dict) and bool(q.get("quote_ok"))
            g = {k: _num(q.get(k)) for k in ("mid", "iv", "delta", "gamma", "vega", "theta")} if ok else {}
            if not ok or any(g.get(k) is None for k in ("mid", "delta", "gamma", "vega", "theta")):
                row["quote_missing"] = True
                _apply_beta(row, beta_fn, as_of)
                rows.append(row)
                continue
            row.update(g)
            qty = row["qty"]
            row["vega_dollar_per_pt"] = round(qty * g["vega"], 2)
            row["theta_dollar_per_day"] = round(qty * g["theta"], 2)
            if S is not None:
                row["dollar_delta"] = round(qty * g["delta"] * S, 2)
                row["gamma_dollar_per_1pct"] = round(0.5 * qty * g["gamma"] * (0.01 * S) ** 2, 2)
            _apply_beta(row, beta_fn, as_of)
            rows.append(row)
    return rows


def _hedge_positions() -> List[Dict]:
    return _load_jsonl(POSITIONS_FILE)


def hedge_exposures(as_of: str, closes_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                    positions: Optional[List[Dict]] = None, spy_price: Optional[float] = None) -> List[Dict]:
    """SPY 覆盖账本的持仓行。β=1.0 是定义（对冲工具就是基准），source="benchmark"。"""
    closes_fn = closes_fn or _default_close
    if positions is None:
        positions = _hedge_positions()
    rows: List[Dict] = []
    for p in positions or []:
        tk = str(p.get("ticker") or CONFIG["hedge_instrument"])
        row = _blank_row(tk, "hedge")
        row.update({"gamma_dollar_per_1pct": 0.0, "vega_dollar_per_pt": 0.0, "theta_dollar_per_day": 0.0,
                    "beta": 1.0, "beta_source": "benchmark", "avg_price": _num(p.get("avg_price"))})
        shares = _num(p.get("shares"))
        if shares is None:
            # shares 是 null/NaN（_scrub 落盘时把 NaN 写成 null，坏行会原样读回来）：
            # 这行的 $ 暴露**未知**，不是 0。原来 `continue` 把它和「已平掉的 0 股行」
            # 一起丢掉，结果 hedge_exposures 返回空表、band_status 报 "empty"、
            # n_price_missing=0——自信且错误。照 stock_exposures 的老规矩标一行残行
            # （那边 :385 一直是这么处理的），让 β·$Delta 缺一项 → unknown → 不对冲。
            # 标 price_missing 而不是新造一个 shares_missing：口径与股票行一致，
            # 覆盖率计数与 hedge_recommendation 的 reason 都不用改就能说出「缺了东西」。
            _log.warning("[PortfolioGreeks] 对冲账本仓位 shares 非有限（%r），按缺数据处理不跳过", p.get("shares"))
            row["price_missing"] = True
            row["beta_missing"] = True
            row["beta"] = row["beta_source"] = None
            rows.append(row)
            continue
        if shares == 0:
            continue                      # 真实的 0 股（平仓留痕）：跳过是对的，它确实没有暴露
        row["qty"] = shares
        px = _pos(spy_price)
        if px is None:
            try:
                px = _pos(closes_fn(tk, as_of))
            except Exception as exc:  # noqa: BLE001
                _log.warning("[%s] 对冲工具收盘获取失败: %s", tk, exc)
                px = None
        row["price"], row["price_missing"] = px, px is None
        if px is not None:
            row["dollar_delta"] = round(shares * px, 2)
            row["beta_dollar_delta"] = row["dollar_delta"]
        rows.append(row)
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 聚合 / 合并 NAV / 对冲建议
# ══════════════════════════════════════════════════════════════════════════════

def aggregate(rows: List[Dict], nav: Optional[float]) -> Dict:
    """求和 + 折 % NAV + 覆盖率 + β·Delta 带状判定。
    band_status："unknown" 只要有任何一行的 β·$Delta 算不出来（缺价/缺报价/缺 β）——
    缺的那部分可能大到翻转结论，所以宁可说不知道。"""
    nav = _pos(nav)
    sums = {k: 0.0 for k in _GREEK_KEYS}
    n_price = n_quote = n_beta = n_incomplete_bdd = 0
    n_with_beta = 0
    for r in rows:
        for k in _GREEK_KEYS:
            v = _num(r.get(k))
            if v is not None:
                sums[k] += v
        n_price += bool(r.get("price_missing"))
        n_quote += bool(r.get("quote_missing"))
        n_beta += bool(r.get("beta_missing"))
        n_with_beta += _num(r.get("beta")) is not None
        n_incomplete_bdd += _num(r.get("beta_dollar_delta")) is None
    n_rows = len(rows)
    pct = {k: (round(sums[k] / nav * 100.0, 4) if nav else None) for k in _GREEK_KEYS}
    coverage = {"n_rows": n_rows, "n_price_missing": n_price, "n_quote_missing": n_quote,
                "n_beta_missing": n_beta, "n_beta_dd_incomplete": n_incomplete_bdd,
                "beta_coverage": (round(n_with_beta / n_rows, 4) if n_rows else None)}
    partial = bool(n_price or n_quote or n_beta)
    target, band = CONFIG["beta_delta_target_pct"], CONFIG["beta_delta_band_pct"]
    if nav is None or n_incomplete_bdd > 0 or n_rows == 0:
        status = "unknown" if n_rows else "empty"
    else:
        p = pct["beta_dollar_delta"]
        status = "above" if p > target + band else ("below" if p < target - band else "inside")
    vega_alert = (nav is not None and pct["vega_dollar_per_pt"] is not None
                  and abs(pct["vega_dollar_per_pt"]) > CONFIG["vega_alert_pct"])
    gamma_alert = (nav is not None and pct["gamma_dollar_per_1pct"] is not None
                   and abs(pct["gamma_dollar_per_1pct"]) > CONFIG["gamma_alert_pct"])
    return {
        "nav": nav,
        "sums": {k: round(v, 2) for k, v in sums.items()},
        "pct_nav": pct,
        "coverage": coverage,
        "partial": partial,
        "band_status": status,
        "band": {"target_pct": target, "band_pct": band,
                 "lower_pct": target - band, "upper_pct": target + band},
        "vega_alert": bool(vega_alert),
        "gamma_alert": bool(gamma_alert),
        "by_kind": {kind: sum(1 for r in rows if r.get("kind") == kind) for kind in ("stock", "option", "hedge")},
    }


def _latest_nav(path: Path, as_of: str) -> Optional[float]:
    best = None
    for e in _load_jsonl(path):
        d = str(e.get("date") or "")
        if d and d <= as_of and (best is None or d > best[0]):
            best = (d, _num(e.get("nav")))
    return best[1] if best else None


def _book_never_started(equity_file, positions_file) -> bool:
    """这本账**从未启动**：净值文件与持仓文件都不存在，或存在但一条可解析记录都没有。
    两个都要看——单看净值文件的话，「文件被删」与「从未开张」输出完全一样。"""
    for f in (equity_file, positions_file):
        try:
            if Path(f).exists() and _load_jsonl(Path(f)):
                return False
        except OSError as exc:
            _log.warning("[PortfolioGreeks] %s 不可读，不敢当作「从未启动」: %s", f, exc)
            return False
    return True


def hedge_overlay_value(as_of: str, spy_price: Optional[float]) -> Optional[float]:
    """覆盖账本净值 = cash + shares × SPY 价。没开过仓 → 0.0；有仓但没价 → None。"""
    meta = _load_meta()
    cash = _num(meta.get("cash"))
    if cash is None:
        return None
    raw = [_num(p.get("shares")) for p in _hedge_positions()]
    if any(v is None for v in raw):
        # 有一行 shares 读不出来 → 覆盖账本市值未知。`or 0.0` 会把它算成 0 股，
        # 于是净值看着完好、合并 NAV 少一块、Greeks 分子却还在——分母被做小、比率被放大。
        _log.error("[PortfolioGreeks] 对冲账本有 shares 非有限的仓位，覆盖账本净值不可得")
        return None
    shares = sum(raw)
    if shares == 0:
        return cash
    px = _pos(spy_price)
    return None if px is None else cash + shares * px


def combined_nav_detail(as_of: str, closes_fn: Optional[Callable[[str, str], Optional[float]]] = None,
                        spy_price: Optional[float] = None) -> Dict:
    """三分量合并 NAV；任一缺失 → nav=None 且 `missing` 列出是哪一个。
    跨式腿的 10 万起始资本是名义的（测量账本），合并进来只为让百分比有一个分母。"""
    import paper_portfolio as pp
    import options_paper_leg as opl
    closes_fn = closes_fn or _default_close
    comps: Dict[str, Optional[float]] = {
        "stock_book": _latest_nav(pp.EQUITY_FILE, as_of),
        "straddle_leg": _latest_nav(opl.EQUITY_FILE, as_of),
    }
    # 跨式腿**从未启动** ≠ 「今天缺一行」。前者是合法的零状态：没有期权仓，对合并 NAV
    # 与 Greeks 的贡献确实是 0，按 0 计并标注 not_started，否则对冲在腿建账之前永远 unknown。
    # 但「从未启动」要由**净值文件与持仓文件双双空**来证明，只看净值文件不够（二次复查修正）：
    #   ① 净值文件被删/状态目录指错 → 输出与「从未启动」逐字节相同，$98,000 的账凭空消失；
    #   ② 更糟的是持仓还在、净值文件没了：这些仓的 Greeks 进了分子、NAV 却按 0 进分母，
    #      partial=False、band_status="above"，照样下单——分母被做小的比率是要成交的。
    # 有持仓却没净值行，那是数据缺失，只能 None 并点名。
    not_started: List[str] = []
    if comps["straddle_leg"] is None and _book_never_started(opl.EQUITY_FILE, opl.POSITIONS_FILE):
        comps["straddle_leg"] = 0.0
        not_started.append("straddle_leg")
    if spy_price is None:
        try:
            spy_price = _pos(closes_fn(CONFIG["hedge_instrument"], as_of))
        except Exception as exc:  # noqa: BLE001
            _log.warning("[SPY] 收盘获取失败: %s", exc)
            spy_price = None
    comps["hedge_overlay"] = hedge_overlay_value(as_of, spy_price)
    missing = [k for k, v in comps.items() if v is None]
    nav = None if missing else sum(comps.values())  # type: ignore[arg-type]
    return {"nav": (round(nav, 2) if nav is not None else None), "components": comps,
            "missing": missing, "not_started": not_started, "spy_price": spy_price}


def combined_nav(as_of: str, closes_fn: Optional[Callable[[str, str], Optional[float]]] = None) -> Optional[float]:
    d = combined_nav_detail(as_of, closes_fn)
    if d["nav"] is None:
        _log.warning("[PortfolioGreeks] %s 合并 NAV 不可得，缺：%s", as_of, ", ".join(d["missing"]))
    return d["nav"]


def hedge_recommendation(agg: Dict, spy_price: Optional[float], nav: Optional[float]) -> Dict:
    """带外 → 用 SPY 股票拉回（edge：最近带边；center：带中心）。
    spy_shares 负 = 卖出 SPY（组合太多头），正 = 买入。edge 模式股数向外取整（ceil），
    否则四舍五入会留半股在带外、第二天再来一笔 1 股的小交易。
    inside / unknown / empty 一律 hold，reason 说清楚缺什么——**不在部分数据上对冲**。"""
    status = agg.get("band_status")
    nav = _pos(nav)
    base = {"action": "hold", "spy_shares": 0, "excess_usd": None, "target_usd": None,
            "band_status": status, "rebalance_to": CONFIG["rebalance_to"],
            "beta_dd_usd": (agg.get("sums") or {}).get("beta_dollar_delta"),
            "beta_dd_pct": (agg.get("pct_nav") or {}).get("beta_dollar_delta"),
            "spy_price": _pos(spy_price)}
    if status == "unknown":
        cov = agg.get("coverage") or {}
        why = []
        if nav is None:
            why.append("nav unavailable")
        if cov.get("n_price_missing"):
            why.append(f"{cov['n_price_missing']} price missing")
        if cov.get("n_quote_missing"):
            why.append(f"{cov['n_quote_missing']} quote missing")
        if cov.get("n_beta_missing"):
            why.append(f"{cov['n_beta_missing']} beta missing")
        base["reason"] = "partial data — no hedge on partial data (" + ", ".join(why or ["unknown"]) + ")"
        return base
    if status == "empty":
        base["reason"] = "no positions"
        return base
    if status == "inside":
        base["reason"] = (f"β·Δ {base['beta_dd_pct']:+.2f}% NAV inside band "
                          f"[{agg['band']['lower_pct']:+.0f}%, {agg['band']['upper_pct']:+.0f}%]")
        return base
    if nav is None or base["spy_price"] is None:
        base["reason"] = f"band {status} but " + ("nav unavailable" if nav is None else "SPY price unavailable")
        return base
    band = agg["band"]
    if CONFIG["rebalance_to"] == "center":
        target_pct = band["target_pct"]
    else:
        target_pct = band["upper_pct"] if status == "above" else band["lower_pct"]
    target_usd = target_pct / 100.0 * nav
    excess = base["beta_dd_usd"] - target_usd
    mag = abs(excess) / base["spy_price"]
    shares = int(math.ceil(mag - 1e-9)) if CONFIG["rebalance_to"] != "center" else int(round(mag))
    spy_shares = -shares if excess > 0 else shares
    action = "sell_spy" if spy_shares < 0 else ("buy_spy" if spy_shares > 0 else "hold")
    base.update({"action": action, "spy_shares": spy_shares, "excess_usd": round(excess, 2),
                 "target_usd": round(target_usd, 2), "target_pct": target_pct,
                 "reason": (f"β·Δ {base['beta_dd_pct']:+.2f}% NAV {status} band → rebalance to "
                            f"{CONFIG['rebalance_to']} ({target_pct:+.0f}% = ${target_usd:,.0f}); "
                            f"excess ${excess:,.0f} / SPY ${base['spy_price']:.2f} = {spy_shares:+d} sh")})
    return base


# ══════════════════════════════════════════════════════════════════════════════
# 压力测试：现货 × IV 联合网格
# ══════════════════════════════════════════════════════════════════════════════

def stress_table(rows: List[Dict], spy_price: Optional[float] = None) -> Dict:
    """网格 stress_spot_pct × stress_iv_pts → 组合 P&L。
        股票：dollar_delta × β × shock            （β 缺 → 剔除，该格 partial）
        期权：qty × [BS(S·(1+β·shock), K, T, r, iv + pts/100) − mid]，T=dte/365
        SPY：dollar_delta × shock
    (0,0) 格股票恒为 0；期权在 (0,0) 是 BS 价 − mid 的模型基差，逐合约列在 bs_vs_mid_gap。
    剔除的行进 `excluded`，其毛 |$Delta| 进 `excluded_dollar_delta`（也挂到 worst_cell 上）；
    IV 轴被 0 地板托住的格标 `iv_clamped` + 实际施加的 `iv_pts_effective`。
    已到期（dte<=0）的合约剔除，不重定价——理由写在下面剔除处。"""
    from greeks_engine import bs_price
    IV_FLOOR = 0.0001
    spots = list(CONFIG["stress_spot_pct"])
    ivs = list(CONFIG["stress_iv_pts"])
    r = CONFIG["risk_free"]
    cells: List[Dict] = []
    gaps: List[Dict] = []
    excluded: List[str] = []
    usable: List[Tuple[str, Dict]] = []
    excl_dd = 0.0            # 被剔除行的 |$Delta| 合计（**毛额**：正负互抵会把规模抹平）
    excl_dd_unknown = 0      # 连 $Delta 都算不出来的剔除行数（缺价/缺报价）

    def _exclude(row: Dict, label: str) -> None:
        """剔一行的同时把它的规模记下来。只记 label 的话，worst_cell 里那个温和的数字
        就没人对得上账了——$100 万无 β 的股票被剔掉后，最差格能小 100 倍。"""
        nonlocal excl_dd, excl_dd_unknown
        excluded.append(label)
        dd = _num(row.get("dollar_delta"))
        if dd is None:
            excl_dd_unknown += 1
        else:
            excl_dd += abs(dd)

    for row in rows:
        kind = row.get("kind")
        if kind == "hedge":
            dd = _num(row.get("dollar_delta"))
            if dd is None:
                dd = (_num(row.get("qty")) or 0.0) * (_pos(spy_price) or 0.0) if _pos(spy_price) else None
            if dd is None:
                _exclude(row, f"{row.get('ticker')}(hedge:no price)")
                continue
            usable.append(("hedge", {"dd": dd}))
        elif kind == "stock":
            dd, beta = _num(row.get("dollar_delta")), _num(row.get("beta"))
            if dd is None or beta is None:
                _exclude(row, f"{row.get('ticker')}(stock:{'no price' if dd is None else 'no beta'})")
                continue
            usable.append(("stock", {"dd": dd, "beta": beta}))
        elif kind == "option":
            need = {k: _num(row.get(k)) for k in ("qty", "price", "strike", "iv", "mid", "beta")}
            dte = _num(row.get("dte"))     # 过 _num：NaN 的 dte 躲得过 `is None`，却会让 int() 直接崩
            if any(v is None for v in need.values()) or dte is None or row.get("cp") not in ("call", "put"):
                miss = [k for k, v in need.items() if v is None] + (["dte"] if dte is None else [])
                _exclude(row, f"{row.get('symbol') or row.get('ticker')}(option:{','.join(miss) or 'cp'})")
                continue
            if dte <= 0:
                # 已到期的合约剔除，**不**重新定价。原来 T=max(int(dte),1)/365 会把一张
                # 过期 5 天的合约当成「还剩 1 天」的活合约报价，凭空发明时间价值，
                # 且不剔除、不标记——网格看着完整。
                # 为什么是剔除而不是按内在价值 max(S−K,0)·qty 重估：过期腿的真实结果
                # 取决于结算与平仓约定（有没有被行权、跨式腿账本何时把它移出持仓），
                # 那是 options_paper_leg 的信息，本模块既不管也拿不到；在这里编一个
                # 内在价值只是用一个猜测换另一个猜测，而且更像真的。剔除会让整张网格
                # partial、行名进 excluded、规模进 excluded_dollar_delta——大声说不知道。
                _exclude(row, f"{row.get('symbol') or row.get('ticker')}(option:expired {int(dte)}d)")
                continue
            T = int(dte) / 365.0
            base_bs = bs_price(need["price"], need["strike"], T, r, need["iv"], row["cp"])
            gaps.append({"symbol": row.get("symbol"), "bs": round(base_bs, 4), "mid": need["mid"],
                         "gap": round(base_bs - need["mid"], 4), "T_days": int(dte)})
            usable.append(("option", {**need, "T": T, "cp": row["cp"]}))
    partial = bool(excluded)
    worst = None
    for sp in spots:
        for ivp in ivs:
            pnl = 0.0
            clamped: List[float] = []
            for kind, u in usable:
                shock = sp / 100.0
                if kind == "hedge":
                    pnl += u["dd"] * shock
                elif kind == "stock":
                    pnl += u["dd"] * u["beta"] * shock
                else:
                    S1 = u["price"] * (1.0 + u["beta"] * shock)
                    iv_shocked = u["iv"] + ivp / 100.0      # ivp 是 vol 点：−10pt = IV −0.10
                    iv1 = max(iv_shocked, IV_FLOOR)
                    if iv1 > iv_shocked:
                        # 地板托住了：这张合约实际吃到的冲击比列标签小（IV 8% 的票吃不下
                        # −10pt）。原来这里静默截断，格子照样标 −10pt——标签在说谎。
                        clamped.append(round((iv1 - u["iv"]) * 100.0, 4))
                    pnl += u["qty"] * (bs_price(S1, u["strike"], u["T"], r, iv1, u["cp"]) - u["mid"])
            cell = {"spot_pct": sp, "iv_pts": ivp, "pnl": round(pnl, 2), "partial": partial,
                    "iv_clamped": bool(clamped)}
            if clamped:
                cell["iv_pts_effective"] = min(clamped, key=abs)   # 截得最狠的那张（|冲击| 最小）
                cell["n_iv_clamped"] = len(clamped)
            cells.append(cell)
            if worst is None or pnl < worst["pnl"]:
                worst = dict(cell)
    zero = next((c["pnl"] for c in cells if c["spot_pct"] == 0 and c["iv_pts"] == 0), None)
    out = {"spot_pct": spots, "iv_pts": ivs, "cells": cells, "worst_cell": worst,
           "pnl_at_zero": zero, "bs_vs_mid_gap": gaps, "n_used": len(usable),
           "excluded": excluded, "partial": partial,
           "excluded_dollar_delta": round(excl_dd, 2), "excluded_dd_unknown": excl_dd_unknown,
           "n_iv_clamped_cells": sum(1 for c in cells if c.get("iv_clamped"))}
    if worst is not None and partial:
        # 最差格那个金额会被单独引用（日报里就是一行字），所以把「它少算了多少敞口」
        # 贴在它自己身上，而不是指望读者去翻 excluded 列表。
        worst["excluded_dollar_delta"] = round(excl_dd, 2)
        worst["excluded_rows"] = len(excluded)
        worst["excluded_dd_unknown"] = excl_dd_unknown
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 计算一天（纯读）→ 执行（写账本）
# ══════════════════════════════════════════════════════════════════════════════

def _hedge_rows_before_today(as_of: str, closes_fn, spy_price: Optional[float]) -> Tuple[List[Dict], Optional[Dict]]:
    """今天已经成交过的话，把那笔从持仓里减掉，重建「交易前」视角——同日重跑才能算出
    与第一次一模一样的建议与审计文件（幂等）。返回 (rows_before, today_trade | None)。"""
    trades = [t for t in _load_jsonl(TRADES_FILE) if t.get("date") == as_of]
    today = trades[-1] if trades else None
    positions = _hedge_positions()
    if today is not None:
        traded = _num(today.get("shares")) or 0.0
        tk = today.get("ticker") or CONFIG["hedge_instrument"]
        found = False
        adj = []
        for p in positions:
            if p.get("ticker") == tk:
                found = True
                adj.append({**p, "shares": (_num(p.get("shares")) or 0.0) - traded})
            else:
                adj.append(p)
        if not found:
            adj.append({"ticker": tk, "shares": -traded, "avg_price": _num(today.get("price"))})
        positions = adj
    return hedge_exposures(as_of, closes_fn, positions=positions, spy_price=spy_price), today


def compute_day(as_of: str, closes_fn=None, quotes_fn=None, beta_fn=None) -> Dict:
    """只读：暴露行 + 聚合 + 建议 + 压力表。不写任何账本文件（β 缓存除外——那是缓存）。"""
    closes_fn = closes_fn or _default_close
    quotes_fn = quotes_fn or _default_quotes
    beta_fn = beta_fn or _default_beta
    try:
        spy_price = _pos(closes_fn(CONFIG["hedge_instrument"], as_of))
    except Exception as exc:  # noqa: BLE001
        _log.warning("[SPY] 收盘获取失败: %s", exc)
        spy_price = None
    stock_rows = stock_exposures(as_of, closes_fn=closes_fn, beta_fn=beta_fn)
    option_rows = option_exposures(as_of, quotes_fn=quotes_fn, beta_fn=beta_fn, closes_fn=closes_fn)
    hedge_rows, today_trade = _hedge_rows_before_today(as_of, closes_fn, spy_price)
    rows = stock_rows + option_rows + hedge_rows
    nav_d = combined_nav_detail(as_of, closes_fn, spy_price=spy_price)
    # NAV 不分「交易前/后」：成交价=收盘价，−shares×px 的现金腿与 +shares×px 的市值腿
    # 恰好抵消，覆盖净值在交易瞬间不变——所以同日重跑用当前状态算 NAV 也与第一次一致。
    agg = aggregate(rows, nav_d["nav"])
    rec = hedge_recommendation(agg, spy_price, nav_d["nav"])
    stress = stress_table(rows, spy_price)
    return {"as_of": as_of, "version": _VERSION, "spy_price": spy_price, "nav": nav_d,
            "rows": rows, "aggregate": agg, "recommendation": rec, "stress": stress,
            "today_trade": today_trade}


def _execute_trade(as_of: str, rec: Dict, agg: Dict, nav: Optional[float]) -> Optional[Dict]:
    """按当日收盘成交 SPY（无点差模型：SPY 点差 ~1 bp，明说不建模）。返回成交记录。"""
    shares = int(rec.get("spy_shares") or 0)
    px = _pos(rec.get("spy_price"))
    if shares == 0 or px is None:
        return None
    meta = _load_meta()
    cash = _num(meta.get("cash"))
    if cash is None:
        _log.error("[PortfolioGreeks] meta.cash 非有限（%r）——拒绝成交", meta.get("cash"))
        return None
    positions = _hedge_positions()
    tk = CONFIG["hedge_instrument"]
    cur = next((p for p in positions if p.get("ticker") == tk), None)
    old = _num(cur.get("shares")) if cur else 0.0
    old = old or 0.0
    new = old + shares
    cash -= shares * px
    if not math.isfinite(cash) or not math.isfinite(new):
        _log.error("[PortfolioGreeks] 成交算出非有限值（cash=%r shares=%r），拒绝入账", cash, new)
        return None
    rest = [p for p in positions if p.get("ticker") != tk]
    if new != 0:
        old_avg = _num((cur or {}).get("avg_price")) or px
        same_side = old != 0 and (old > 0) == (new > 0)
        if same_side and abs(new) > abs(old):        # 加仓：数量加权均价
            avg = (abs(old) * old_avg + abs(shares) * px) / abs(new)
        elif same_side:                               # 减仓：均价不变
            avg = old_avg
        else:                                         # 翻向：新仓从成交价起算
            avg = px
        rest.append({"ticker": tk, "shares": new, "avg_price": round(avg, 4),
                     "entry_date": (cur or {}).get("entry_date") or as_of, "last_price": px,
                     "last_mark_date": as_of})
    _write_jsonl(POSITIONS_FILE, rest)
    trade = {"date": as_of, "ticker": tk, "shares": shares, "price": px, "action": rec.get("action"),
             "fill": "close", "spread_model": "none (SPY ~1bp, not modelled)",
             "excess_usd": rec.get("excess_usd"), "target_usd": rec.get("target_usd"),
             "beta_dd_usd_before": rec.get("beta_dd_usd"), "beta_dd_pct_before": rec.get("beta_dd_pct"),
             "nav": nav, "shares_after": new, "reason": rec.get("reason")}
    _append_jsonl(TRADES_FILE, trade)
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of
    meta["cash"] = cash
    _save_meta(meta)
    _log.info("[PortfolioGreeks] → %s %+d SPY @ %.2f（%s）", as_of, shares, px, rec.get("reason"))
    return trade


def run_for_date(as_of: str, closes_fn=None, quotes_fn=None, beta_fn=None, execute: bool = True) -> Dict:
    """算暴露 → 聚合 → 建议 →（execute 且带外且今天还没交易过）成交 → 覆盖账本盯市 →
    净值快照（按日期去重）→ 审计文件 hedge_state/greeks_{as_of}.json。同日重跑幂等。
    execute=False：只算，不写任何账本文件（β 缓存除外）。"""
    res = compute_day(as_of, closes_fn=closes_fn, quotes_fn=quotes_fn, beta_fn=beta_fn)
    rec = res["recommendation"]
    nav = res["nav"]["nav"]
    executed = res.get("today_trade")
    if not execute:
        res["executed_trade"] = executed
        res["executed"] = False
        res["aggregate_after"] = None
        return res
    if executed is None and rec.get("action") in ("sell_spy", "buy_spy"):
        executed = _execute_trade(as_of, rec, res["aggregate"], nav)
    elif executed is not None and rec.get("action") in ("sell_spy", "buy_spy"):
        _log.info("[PortfolioGreeks] %s 今天已成交过（%+d SPY），重跑不再交易", as_of, int(executed.get("shares") or 0))
    res["executed_trade"] = executed
    res["executed"] = executed is not None
    res["today_trade"] = executed      # 第一次跑与重跑写出同一份审计文件（幂等）

    # ── 覆盖账本盯市 + 净值快照 ──
    meta = _load_meta()
    spy_price = res["spy_price"]
    positions = _hedge_positions()
    shares = 0.0
    for p in positions:
        s = _num(p.get("shares")) or 0.0
        shares += s
        if spy_price is not None:
            p["last_price"] = spy_price
            p["last_mark_date"] = as_of
    if positions:
        _write_jsonl(POSITIONS_FILE, positions)
    cash = _num(meta.get("cash"))
    mv = (shares * spy_price) if (spy_price is not None) else (None if shares else 0.0)
    overlay_nav = (cash + mv) if (cash is not None and mv is not None) else None
    snapshot = {"date": as_of, "spy_shares": shares, "spy_price": spy_price, "cash": _r(cash),
                "market_value": _r(mv), "nav": _r(overlay_nav),
                "trades_today": sum(1 for t in _load_jsonl(TRADES_FILE) if t.get("date") == as_of),
                "band_status": res["aggregate"]["band_status"]}
    equity = [e for e in _load_jsonl(EQUITY_FILE) if e.get("date") != as_of]
    equity.append(snapshot)
    equity.sort(key=lambda e: e["date"])
    _write_jsonl(EQUITY_FILE, equity)
    if not meta.get("starting_date"):
        meta["starting_date"] = as_of
    meta["last_run_date"] = as_of
    meta["version"] = _VERSION
    meta["config_snapshot"] = dict(CONFIG)
    meta.setdefault("cash", 0.0)
    _save_meta(meta)

    # ── 交易后的聚合（含 SPY 覆盖现状）──
    hedge_rows_after = hedge_exposures(as_of, closes_fn or _default_close, positions=_hedge_positions(),
                                       spy_price=spy_price)
    rows_after = [r for r in res["rows"] if r.get("kind") != "hedge"] + hedge_rows_after
    res["aggregate_after"] = aggregate(rows_after, nav)
    res["equity_snapshot"] = snapshot

    audit = {k: v for k, v in res.items() if k != "rows"}
    audit["rows"] = res["rows"]
    _atomic_write_text(STATE_DIR / f"greeks_{as_of}.json",
                       json.dumps(_scrub(audit), ensure_ascii=False, indent=1, sort_keys=True))
    return res


# ══════════════════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_usd(v) -> str:
    f = _num(v)
    return f"${f:+,.0f}" if f is not None else "—"


def _fmt_pct(v, nd=2) -> str:
    f = _num(v)
    return f"{f:+.{nd}f}%" if f is not None else "—"


def _load_audit(as_of: str) -> Optional[Dict]:
    p = STATE_DIR / f"greeks_{as_of}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def render_markdown(as_of: str, result: Optional[Dict] = None) -> str:
    """日报小节。优先读当日审计文件（run_for_date 已跑过）；否则只读计算、不落盘。
    三本账都没有任何持仓 → 空串。"""
    res = result or _load_audit(as_of)
    if res is None:
        try:
            res = run_for_date(as_of, execute=False)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[PortfolioGreeks] 只读计算失败: %s", exc)
            return ""
    rows = res.get("rows") or []
    if not rows:
        return ""
    agg = res.get("aggregate") or {}
    after = res.get("aggregate_after") or agg
    rec = res.get("recommendation") or {}
    nav_d = res.get("nav") or {}
    nav = _num(nav_d.get("nav"))
    kinds = agg.get("by_kind") or {}
    trade = res.get("executed_trade")
    stress = res.get("stress") or {}

    L = ["", "## 组合 Greeks 与对冲（观察项 + 纸面 SPY 覆盖）", ""]
    hedge_sh = sum(_num(r.get("qty")) or 0.0 for r in rows if r.get("kind") == "hedge")
    L.append(f"截至 {as_of}：股票仓 {kinds.get('stock', 0)} 行 / 跨式腿合约 {kinds.get('option', 0)} 行 / "
             f"SPY 覆盖 {hedge_sh:+,.0f} 股（交易前）；合并 NAV "
             + (f"${nav:,.0f}" if nav is not None else f"不可得（缺：{', '.join(nav_d.get('missing') or ['?'])}）")
             + (f"；SPY ${_num(res.get('spy_price')):.2f}" if _num(res.get("spy_price")) is not None else "；SPY 价不可得")
             + "。")
    L += ["", "| 指标 | 数值 | % NAV | 备注 |", "|------|------|-------|------|"]
    s, p = agg.get("sums") or {}, agg.get("pct_nav") or {}
    band = agg.get("band") or {}
    L.append(f"| $Delta | {_fmt_usd(s.get('dollar_delta'))} | {_fmt_pct(p.get('dollar_delta'))} | 名义方向暴露 |")
    L.append(f"| β·$Delta | {_fmt_usd(s.get('beta_dollar_delta'))} | {_fmt_pct(p.get('beta_dollar_delta'))} | "
             f"带 [{band.get('lower_pct', 0):+.0f}%, {band.get('upper_pct', 0):+.0f}%] → **{agg.get('band_status')}** |")
    L.append(f"| $Gamma (1% 移动) | {_fmt_usd(s.get('gamma_dollar_per_1pct'))} | {_fmt_pct(p.get('gamma_dollar_per_1pct'), 3)} | "
             f"{'⚠️ 超 ' + str(CONFIG['gamma_alert_pct']) + '%' if agg.get('gamma_alert') else '凸性项'} |")
    L.append(f"| $Vega / vol 点 | {_fmt_usd(s.get('vega_dollar_per_pt'))} | {_fmt_pct(p.get('vega_dollar_per_pt'), 3)} | "
             f"{'⚠️ 超 ' + str(CONFIG['vega_alert_pct']) + '%' if agg.get('vega_alert') else 'IV 每变 1 点'} |")
    L.append(f"| $Theta / 日 | {_fmt_usd(s.get('theta_dollar_per_day'))} | {_fmt_pct(p.get('theta_dollar_per_day'), 3)} | 日历日 |")
    cov = agg.get("coverage") or {}
    cov_line = (f"覆盖：{cov.get('n_rows', 0)} 行，缺价 {cov.get('n_price_missing', 0)} / 缺报价 {cov.get('n_quote_missing', 0)} / "
                f"缺 β {cov.get('n_beta_missing', 0)}，β 覆盖率 "
                + (f"{(cov.get('beta_coverage') or 0) * 100:.0f}%" if cov.get("beta_coverage") is not None else "—"))
    if agg.get("partial"):
        cov_line += " —— **部分数据，结论不完整，不据此对冲**"
    L += ["", cov_line]
    L.append(f"**今日建议**：`{rec.get('action')}` {int(rec.get('spy_shares') or 0):+d} SPY —— {rec.get('reason')}")
    if trade:
        L.append(f"**已执行**（纸面）：{int(trade.get('shares') or 0):+d} SPY @ ${_num(trade.get('price')) or 0:.2f}，"
                 f"成交=收盘、无点差模型；交易后 β·$Delta {_fmt_pct((after.get('pct_nav') or {}).get('beta_dollar_delta'))} NAV"
                 f"（{after.get('band_status')}）")
    elif res.get("executed") is False and rec.get("action") in ("sell_spy", "buy_spy"):
        L.append("**未执行**（dry-run / 只读渲染）")
    if stress.get("cells"):
        spots, ivs = stress["spot_pct"], stress["iv_pts"]
        L += ["", "**压力网格**（行：β 调整后现货冲击；列：IV 平移 vol 点；单位 $）", "",
              "| spot \\ IV | " + " | ".join(f"{v:+d}pt" for v in ivs) + " |",
              "|---|" + "---|" * len(ivs)]
        by = {(c["spot_pct"], c["iv_pts"]): c for c in stress["cells"]}
        for sp in spots:
            vals = []
            for iv in ivs:
                c = by.get((sp, iv)) or {}
                # `*` = 这一格的 IV 冲击被 0 地板截断了，实际没打满标签上的点数
                vals.append(f"{_num(c.get('pnl')) or 0:+,.0f}" + ("*" if c.get("iv_clamped") else ""))
            L.append(f"| {sp:+d}% | " + " | ".join(vals) + " |")
        w = stress.get("worst_cell") or {}
        L.append("")
        # 「网格不完整，已排除 $X 敞口」必须**紧贴**最差格那个金额：读者带走的是那个数字，
        # 不是下一行的免责声明（二次复查：$100 万无 β 的行被剔掉后，最差格温和了 100 倍）。
        worst_line = (f"最差格：现货 {w.get('spot_pct', 0):+d}% / IV {w.get('iv_pts', 0):+d}pt → {_fmt_usd(w.get('pnl'))}"
                      + (f"（{_num(w.get('pnl')) / nav * 100:+.2f}% NAV）" if (nav and _num(w.get('pnl')) is not None) else ""))
        if stress.get("partial"):
            exd = _num(w.get("excluded_dollar_delta"))
            unk = int(w.get("excluded_dd_unknown") or 0)
            worst_line += ("　⚠️ **网格不完整，已排除 " + (f"${exd:,.0f}" if exd is not None else "未知规模")
                           + (f" + {unk} 行规模未知" if unk else "") + " 敞口，真实最差比这个数字更差**"
                           + f"；剔除 {len(stress.get('excluded') or [])} 行（{', '.join(stress['excluded'][:4])}）")
        L.append(worst_line)
        if stress.get("n_iv_clamped_cells"):
            cl = [c for c in stress["cells"] if c.get("iv_clamped")]
            L.append(f"⚠️ IV 轴截断（表中标 `*`）：{stress['n_iv_clamped_cells']} 格的标称冲击打不满"
                     f"（合约 IV 不够减，IV 不能为负）——"
                     + "；".join(f"{c['spot_pct']:+d}%/{c['iv_pts']:+d}pt 实际 {_num(c.get('iv_pts_effective')) or 0:+.1f}pt"
                                 for c in cl[:4])
                     + "。这些格施加的冲击比标签小，别按标签读。")
        if stress.get("bs_vs_mid_gap"):
            g = stress["bs_vs_mid_gap"]
            L.append(f"模型基差（BS − mid，每股）：" + "；".join(f"{x.get('symbol')} {x.get('gap'):+.2f}" for x in g[:6])
                     + f"；(0,0) 格合计 {_fmt_usd(stress.get('pnl_at_zero'))}（不是 P&L，是模型与市场的差）")
    L += ["",
          "> CBOE Greeks 单位（NVDA 实盘对 BS 校核）：vega 每 vol 点、theta 每日历日、delta/gamma 每股。",
          f"> 对冲规则：只在 β·$Delta 出 ±{CONFIG['beta_delta_band_pct']:.0f}% NAV 带时用 SPY 股票拉回{'带边' if CONFIG['rebalance_to'] == 'edge' else '带中心'}；"
          "vega/gamma 只告警不对冲；缺 β/缺价一律不对冲。",
          "> **观察项：不进评分、不进股票纸面组合、不构成任何建议。** SPY 覆盖是独立纸面账本，起始现金 0。", ""]
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main(argv: Optional[List[str]] = None) -> int:
    from hive_logger import pdt_today
    ap = argparse.ArgumentParser(description="组合 Greeks 聚合 + β·Delta 带状对冲（纸面 SPY 覆盖）")
    ap.add_argument("--date", default=None, help="业务日 YYYY-MM-DD（默认 PDT 今天）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写账本（β 缓存除外）")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON（不含逐行）")
    args = ap.parse_args(argv)
    as_of = args.date or pdt_today()
    res = run_for_date(as_of, execute=not args.dry_run)
    if args.json:
        out = {k: v for k, v in res.items() if k != "rows"}
        out["n_rows"] = len(res.get("rows") or [])
        print(json.dumps(_scrub(out), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(as_of, result=res) or f"{as_of}: 三本账均无持仓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
