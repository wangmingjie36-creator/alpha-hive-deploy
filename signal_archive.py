#!/usr/bin/env python3
"""
🐝 Alpha Hive — 单信号 IC 档案 (v0.43.2)
==========================================
把「哪块砖在承重」从每次重新考古，变成一张随时可查的表。

为什么需要
----------
系统有 7 只蜂、60+ 个原始字段，但只有 **5 个聚合维度**进入评估。
2026-07-30 的排查暴露了这个盲区——那天临时翻 `.swarm_results_*.json` 才发现：

  • 内幕情绪 IC = **−0.132**（方向是反的）
  • 内幕披露**笔数** IC = **+0.168**（唯一正向，但那是活跃度不是方向）
  • 蜂群一致度 IC = **+0.003**（零预测力，却被三重计入综合分）
  • cluster buying 样本 = **0 条**（学术上最强的信号，数据里一次没出现）

这些都是**原始信号层**的事实，聚合后就看不见了。没有持续档案，
每次讨论"该修哪里"都要从头挖一遍，而且样本永远停在临时抽取的那几十条。

设计
----
长表 `signal_archive(date, ticker, signal, value)`：新增信号无需改 schema。
前瞻收益在分析时从 `predictions` 联表（已有 price_t7/t30），不重复存储。

用法
----
    # 从历史 .swarm_results_*.json 回填（一次性）
    /usr/local/bin/python3 signal_archive.py --backfill

    # 分析：每个信号的四口径 IC + 噪音地板对照
    /usr/local/bin/python3 signal_archive.py --analyze
    /usr/local/bin/python3 signal_archive.py --analyze --min-samples 100

    # 查看已归档的信号清单与覆盖度
    /usr/local/bin/python3 signal_archive.py --list

日常扫描会自动写入（挂在 `_post_scan_enrichment`）。
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Dict, List, Optional

DB_PATH = Path(__file__).parent / "pheromone.db"
TABLE = "signal_archive"


# ────────────────────────────────────────────────────────────────────────────
# 信号提取器（声明式：加一个信号 = 加一行）
# ────────────────────────────────────────────────────────────────────────────

def _dig(obj: Any, path: str) -> Any:
    """按点号路径取值，任一层缺失返回 None"""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _num(v: Any) -> Optional[float]:
    """转成 float；bool 与不可转换值返回 None（避免 True→1.0 混入数值信号）"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _notable_trades(tr: Dict) -> List[Dict]:
    """notable_trades 在部分历史文件里被存成 repr 字符串，需容错解析"""
    nt = _dig(tr, "agent_details.ScoutBeeNova.details.insider.notable_trades")
    if isinstance(nt, str):
        try:
            nt = ast.literal_eval(nt)
        except (ValueError, SyntaxError):
            return []
    return nt if isinstance(nt, list) else []


def _insider_buys(tr: Dict) -> List[Dict]:
    """只保留 code=P（公开市场买入）。

    SEC Form 4 交易代码：P=公开市场买入、S=卖出、A=授予、M=行权、F=代扣税。
    学术上只有 P 被认为含信息量；其余多为行权/税务/计划内减持。
    """
    return [t for t in _notable_trades(tr)
            if isinstance(t, dict) and t.get("code") == "P"]


def _distinct_buyers(tr: Dict) -> Optional[float]:
    buys = _insider_buys(tr)
    return float(len({t.get("insider") for t in buys})) if buys else 0.0


def _officer_buys(tr: Dict) -> Optional[float]:
    return float(sum(1 for t in _insider_buys(tr) if t.get("is_officer")))


def _sentiment_num(tr: Dict) -> Optional[float]:
    m = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
    return m.get(_dig(tr, "agent_details.ScoutBeeNova.details.insider.sentiment"))


def _direction_num(agent: str) -> Callable:
    m = {"bullish": 1.0, "long": 1.0, "neutral": 0.0, "bearish": -1.0, "short": -1.0}

    def f(tr: Dict) -> Optional[float]:
        d = _dig(tr, f"agent_details.{agent}.direction")
        return m.get(str(d).lower()) if d else None
    return f


def _path(p: str) -> Callable:
    return lambda tr: _num(_dig(tr, p))


def _agent_score(agent: str) -> Callable:
    return lambda tr: _num(_dig(tr, f"agent_details.{agent}.score"))


def _swarm_agreement(tr: Dict) -> Optional[float]:
    """蜂群方向一致度 = 多数派占比。2026-07-30 实测 IC=+0.003（零预测力），
    却经 GuardBee 基础分 + queen 共振加成被三重计入综合分。"""
    dirs = []
    for _ag, d in (tr.get("agent_details") or {}).items():
        v = (d or {}).get("direction")
        if v:
            dirs.append(str(v).lower())
    if len(dirs) < 3:
        return None
    from collections import Counter
    c = Counter(dirs)
    return c.most_common(1)[0][1] / len(dirs)


#: 信号名 → 提取函数。命名约定 `来源.字段`，便于按前缀筛选。
SIGNAL_EXTRACTORS: Dict[str, Callable[[Dict], Optional[float]]] = {
    # ── 聚合层（对照基准）────────────────────────────────────────
    "composite.final_score": lambda tr: _num(tr.get("final_score")),
    "composite.swarm_agreement": _swarm_agreement,

    # ── 内幕 / 聪明钱（系统立身之本）──────────────────────────────
    "insider.sentiment": _sentiment_num,
    "insider.score": _path("agent_details.ScoutBeeNova.details.insider.score"),
    "insider.filings": _path("agent_details.ScoutBeeNova.details.insider.filings"),
    "insider.dollar_bought": _path("agent_details.ScoutBeeNova.details.insider.dollar_bought"),
    "insider.dollar_sold": _path("agent_details.ScoutBeeNova.details.insider.dollar_sold"),
    "insider.distinct_buyers": _distinct_buyers,      # cluster buying 的分子
    "insider.officer_buys": _officer_buys,
    "congress.score": _path("agent_details.ScoutBeeNova.details.congress.congress_score"),

    # ── 拥挤度 / 关注度 ─────────────────────────────────────────
    "crowding.score": _path("agent_details.ScoutBeeNova.details.crowding_score"),
    "crowding.signal": _path("agent_details.ScoutBeeNova.details.crowding_signal"),
    "crowding.adj_factor": _path("agent_details.ScoutBeeNova.details.adjustment_factor"),

    # ── 价格 / 动量 ────────────────────────────────────────────
    "price.momentum_5d": _path("agent_details.ScoutBeeNova.details.momentum_5d"),
    "price.volatility_20d": _path("agent_details.BuzzBeeWhisper.details.volatility_20d"),
    "price.volume_ratio": _path("agent_details.BuzzBeeWhisper.details.volume_ratio"),

    # ── 期权 ──────────────────────────────────────────────────
    "options.iv_current": _path("agent_details.OracleBeeEcho.details.iv_current"),
    "options.put_call_ratio": _path("agent_details.OracleBeeEcho.details.put_call_ratio"),
    "options.gamma_exposure": _path("agent_details.OracleBeeEcho.details.gamma_exposure"),
    "options.total_oi": _path("agent_details.OracleBeeEcho.details.total_oi"),

    # ── 情绪 ──────────────────────────────────────────────────
    "sentiment.pct": _path("agent_details.BuzzBeeWhisper.details.sentiment_pct"),

    # ── 看空侧（唯一勉强显著的方向）──────────────────────────────
    "bear.score": _path("agent_details.BearBeeContrarian.details.bear_score"),
    "bear.insider_bear": _path("agent_details.BearBeeContrarian.details.insider_bear"),
    "bear.overval_bear": _path("agent_details.BearBeeContrarian.details.overval_bear"),
    "bear.options_bear": _path("agent_details.BearBeeContrarian.details.options_bear"),
    "bear.short_int_bear": _path("agent_details.BearBeeContrarian.details.short_int_bear"),

    # ── 共振 / 一致性（回声源头）─────────────────────────────────
    "guard.consistency": _path("agent_details.GuardBeeSentinel.details.consistency"),
    "guard.adj_factor": _path("agent_details.GuardBeeSentinel.details.adjustment_factor"),
    "guard.macro_adj": _path("agent_details.GuardBeeSentinel.details.macro_adj"),
    "guard.top_signals_count": _path("agent_details.GuardBeeSentinel.details.top_signals_count"),

    # ── ML 预测 ───────────────────────────────────────────────
    "ml.probability": _path("agent_details.RivalBeeVanguard.details.probability"),
    "ml.expected_7d": _path("agent_details.RivalBeeVanguard.details.expected_7d"),
    "ml.expected_30d": _path("agent_details.RivalBeeVanguard.details.expected_30d"),

    # ── 基本面快照 ─────────────────────────────────────────────
    "fund.pe_ratio": _path("agent_details.CodeExecutorAgent.details.pe_ratio"),
    "fund.market_cap": _path("agent_details.CodeExecutorAgent.details.market_cap"),

    # ── 各蜂原始分与方向 ───────────────────────────────────────
    **{f"agent.{a}.score": _agent_score(a) for a in (
        "ScoutBeeNova", "BuzzBeeWhisper", "OracleBeeEcho",
        "ChronosBeeHorizon", "RivalBeeVanguard", "GuardBeeSentinel",
        "BearBeeContrarian")},
    **{f"agent.{a}.direction": _direction_num(a) for a in (
        "ScoutBeeNova", "BuzzBeeWhisper", "OracleBeeEcho",
        "ChronosBeeHorizon", "RivalBeeVanguard", "GuardBeeSentinel",
        "BearBeeContrarian")},
}


# ────────────────────────────────────────────────────────────────────────────
# 存储
# ────────────────────────────────────────────────────────────────────────────

def ensure_schema(db_path: Path = DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal TEXT NOT NULL,
                value REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, ticker, signal)
            )
        """)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_sigarch_signal "
                     f"ON {TABLE}(signal, date)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_sigarch_date "
                     f"ON {TABLE}(date)")
        conn.commit()


def extract(ticker_result: Dict) -> Dict[str, float]:
    """从单个标的的 swarm 结果里抽出全部数值信号"""
    out = {}
    for name, fn in SIGNAL_EXTRACTORS.items():
        try:
            v = fn(ticker_result)
        except Exception:  # noqa: BLE001 - 单个信号失败不应影响其余
            v = None
        if v is not None and math.isfinite(v):
            out[name] = float(v)
    return out


def archive(swarm_results: Dict, date: str, db_path: Path = DB_PATH) -> int:
    """把一次扫描的全部原始信号写入档案。

    Args:
        date: **业务日期**（YYYY-MM-DD）。与 predictions 表同键，供联表。

    Returns:
        写入的 (ticker, signal) 行数
    """
    if not swarm_results:
        return 0
    ensure_schema(db_path)
    rows = []
    for ticker, tr in swarm_results.items():
        if not isinstance(tr, dict):
            continue
        for sig, val in extract(tr).items():
            rows.append((date, ticker, sig, val))
    if not rows:
        return 0
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO {TABLE} (date,ticker,signal,value) "
            f"VALUES (?,?,?,?)", rows)
        conn.commit()
    return len(rows)


def backfill(pattern: str = ".swarm_results_*.json",
             db_path: Path = DB_PATH) -> Dict[str, int]:
    """从历史 .swarm_results_*.json 回填。幂等（UNIQUE + REPLACE）。"""
    base = Path(db_path).parent
    files = sorted(glob.glob(str(base / pattern)))
    stats = {"files": 0, "rows": 0, "skipped": 0}
    for f in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
        if not m:
            stats["skipped"] += 1
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            stats["skipped"] += 1
            continue
        n = archive(data, m.group(1), db_path)
        stats["files"] += 1
        stats["rows"] += n
    return stats


# ────────────────────────────────────────────────────────────────────────────
# 分析
# ────────────────────────────────────────────────────────────────────────────

def decompose_fixed_vs_timevarying(by_day: Dict[str, List]) -> Dict:
    """把信号拆成「标的固定效应」与「票内时变」两部分，分别算 IC。

    ## 为什么这个分解是必需的

    常规 IC 无法区分两种**性质完全不同**的信号，而它们长得一模一样：

    - **固定效应**：某些标的长期取值就高/低 —— 这是**选股标签**，
      不是时变信号。塞进每日评分等于每天重新发现"MSFT 是只大盘科技股"，
      并把这个身份当成新信息。正确用法是**筛选池**，不是打分。
    - **票内时变**：同一只标的的取值相对自身均值的波动 —— 这才是**择时信号**，
      适合做成每日评分。

    2026-07-30 实测的两个反例：
      • `risk_adj`：固定效应 IC=+0.006(t=0.19)、票内时变 −0.161(t=−3.75)
        ⇒ 100% 择时，用法正确
      • `crowding_score`：全样本看驼峰形显著（30~50 组超额 +1.17%，
        且样本外延续），但**票内去均值后效应完全消失**
        ⇒ 100% 固定效应，把它当每日信号用是错的

    Args:
        by_day: {date: [(value, ret, ticker), ...]}

    Returns:
        {ic_fixed, ic_within, ic_raw, nature} —— nature ∈
        {"择时", "选股标签", "混合", "无"}
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd

    vals_by_ticker: Dict[str, List[float]] = defaultdict(list)
    for rows in by_day.values():
        for v, _r, tk in rows:
            vals_by_ticker[tk].append(v)
    if len(vals_by_ticker) < 3:
        return {}
    tmean = {tk: mean(vs) for tk, vs in vals_by_ticker.items()}

    raw = {d: [(v, r) for v, r, _ in rows] for d, rows in by_day.items()}
    fixed = {d: [(tmean[tk], r) for _v, r, tk in rows] for d, rows in by_day.items()}
    within = {d: [(v - tmean[tk], r) for v, r, tk in rows] for d, rows in by_day.items()}

    def _mean_ic(panel):
        s = icd._ic_series_from_pairs(panel)
        return mean(s.values()) if len(s) >= 10 else float("nan")

    ic_raw, ic_f, ic_w = _mean_ic(raw), _mean_ic(fixed), _mean_ic(within)

    # 票内方差为 0 ⇒ 该信号在每只标的内部完全不变，是**纯标签**。
    # 此时 ic_within 必为 nan（常数无法算秩相关），但语义不是"未知"而是"无时变信息"，
    # 需与"天数不足导致的 nan"区分开。
    within_spread = max(
        (abs(v) for rows in within.values() for v, _ in rows), default=0.0)
    no_within_variation = within_spread < 1e-12

    af = abs(ic_f) if math.isfinite(ic_f) else 0.0
    aw = 0.0 if no_within_variation else (abs(ic_w) if math.isfinite(ic_w) else 0.0)

    # 判定门槛不能用固定绝对值 —— 票均值本身有抽样波动，标的越少波动越大。
    # 实测：8 只票 × 30 天的**纯随机**面板，ic_fixed 可达 0.037，
    # 若用固定 0.03 会把噪音判成"选股标签"。
    # 改用 1/√(有效横截面宽度) 标定：这正是零假设下单日 IC 的标准差量级
    # （与 ic_diagnostics 噪音地板同源的原理）。
    widths = [len(rows) for rows in by_day.values()]
    avg_w = mean(widths) if widths else 5.0
    n_days_eff = len(by_day)
    floor = (1.0 / math.sqrt(max(avg_w - 1, 1))) / math.sqrt(max(n_days_eff, 1)) * 2.0

    if max(af, aw) < floor:
        nature = "无"
    elif aw >= af * 1.5:
        nature = "择时"
    elif af >= aw * 1.5:
        nature = "选股标签"
    else:
        nature = "混合"
    return {"ic_raw": ic_raw, "ic_fixed": ic_f,
            "ic_within": 0.0 if no_within_variation else ic_w,
            "no_within_variation": no_within_variation,
            "nature": nature}


def load_panel(db_path: Path = DB_PATH, horizon: str = "t7",
               min_width: int = 5,
               with_ticker: bool = False) -> Dict[str, Dict[str, List]]:
    """联表取 {signal: {date: [(值, 前瞻收益)]}}。

    前瞻收益用**纯价格变动**（price_{h} / price_at_predict），而非 return_t7 列
    ——后者是 `_simulate_trade_path` 的路径依赖收益，42.5% 的行被 SL/TP 档位截断，
    会制造大量并列值破坏 rank-IC 的尾部排序（详见 ic_diagnostics 模块注释）。
    """
    price_col, checked_col = f"price_{horizon}", f"checked_{horizon}"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rets = {}
        for r in con.execute(
            f"SELECT date, ticker, price_at_predict, {price_col} AS p1 "
            f"FROM predictions WHERE {checked_col}=1 AND {price_col} IS NOT NULL "
            f"  AND price_at_predict > 0"
        ):
            rets[(r["ticker"], r["date"][:10])] = (
                (r["p1"] - r["price_at_predict"]) / r["price_at_predict"] * 100.0)
        if not rets:
            return {}
        panel: Dict[str, Dict[str, List]] = defaultdict(lambda: defaultdict(list))
        for r in con.execute(f"SELECT date, ticker, signal, value FROM {TABLE}"):
            key = (r["ticker"], r["date"][:10])
            if key not in rets or r["value"] is None:
                continue
            row = ((r["value"], rets[key], r["ticker"]) if with_ticker
                   else (r["value"], rets[key]))
            panel[r["signal"]][r["date"][:10]].append(row)
    finally:
        con.close()
    return {s: {d: v for d, v in bd.items() if len(v) >= min_width}
            for s, bd in panel.items()}


def analyze(db_path: Path = DB_PATH, horizon: str = "t7",
            min_samples: int = 50, min_width: int = 5,
            draws: int = 200) -> List[Dict]:
    """对每个归档信号跑四口径 IC + 噪音地板对照。"""
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd

    # 带 ticker 取一次，供固定效应/时变分解；IC 计算仍用 (值, 收益) 二元组
    panel_t = load_panel(db_path, horizon, min_width, with_ticker=True)
    if not panel_t:
        return []
    panel = {s: {d: [(v, r) for v, r, _ in rows] for d, rows in bd.items()}
             for s, bd in panel_t.items()}
    lag, period = (7, "周") if horizon == "t7" else (30, "月")
    # 显式指定地板基准：`composite.final_score` 覆盖最全（每个标的每天都有）。
    # 不能依赖默认 fallback —— 地板对基准的日期覆盖高度敏感（实测 0.076 vs 0.116）。
    floor = icd.noise_floor(panel, lag, period, draws=draws,
                            base_key="composite.final_score")
    thr = floor.get("ic_p95", float("nan"))

    out = []
    for sig, by_day in sorted(panel.items()):
        n = sum(len(v) for v in by_day.values())
        if n < min_samples:
            continue
        s = icd._ic_series_from_pairs(by_day)
        if len(s) < 10:
            continue
        r = icd.diagnose(s, lag, period)
        if not r:
            continue
        # 覆盖度：非并列值的比例——稀疏事件型信号（如 cluster buying）
        # 大量并列会让 rank-IC 失真，必须显式暴露
        vals = [a for v in by_day.values() for a, _ in v]
        distinct_ratio = len(set(vals)) / len(vals) if vals else 0.0
        r.update({
            "signal": sig, "n_samples": n, "n_days": len(by_day),
            "distinct_ratio": distinct_ratio,
            "beats_noise": bool(math.isfinite(thr) and abs(r["daily_ic"]) > thr),
        })
        # 固定效应 vs 时变分解 —— 区分「选股标签」与「择时信号」
        r.update(decompose_fixed_vs_timevarying(panel_t[sig]) or
                 {"nature": "?", "ic_fixed": float("nan"), "ic_within": float("nan")})
        out.append(r)
    out.sort(key=lambda x: -abs(x["daily_ic"]))
    return out, floor


def print_report(rows: List[Dict], floor: Dict, horizon: str) -> None:
    print("=" * 112)
    print(f"【单信号 IC 档案】horizon={horizon}   共 {len(rows)} 个信号达到最小样本量")
    print("=" * 112)
    if floor:
        print(f"  🎯 噪音地板（随机 ×{floor['n_draws']}）：|日度IC| 95分位 = "
              f"{floor['ic_p95']:.3f} ｜ 通过口径数 95分位 = {floor['passed_p95']:.0f}/4")
        print("     判定为真信号需同时满足：|IC| > 地板 **且** 通过 ≥3/4")
    print("-" * 126)
    print(f"{'信号':<32}{'样本':>6}{'天':>5}{'日度IC':>9}{'t':>7}{'NW':>7}"
          f"{'周t':>7}{'通过':>6}{'离散':>6}{'固定效应':>10}{'票内时变':>10}{'性质':>8}  判定")
    print("-" * 126)

    def _f(x):
        return f"{x:+.4f}" if isinstance(x, float) and math.isfinite(x) else "   n/a"

    for r in rows:
        real = r["beats_noise"] and r["passed_methods"] >= 3
        mark = "🟢 候选" if real else ("🟡 口径不足" if r["beats_noise"] else "⚪ 噪音带内")
        warn = " ⚠️稀疏" if r["distinct_ratio"] < 0.25 else ""
        print(f"{r['signal']:<32}{r['n_samples']:>6}{r['n_days']:>5}"
              f"{r['daily_ic']:>+9.4f}{r['daily_t']:>+7.2f}{r['nw_t']:>+7.2f}"
              f"{r['sub_t']:>+7.2f}{r['passed_methods']:>4}/4"
              f"{r['distinct_ratio']:>6.2f}"
              f"{_f(r.get('ic_fixed')):>10}{_f(r.get('ic_within')):>10}"
              f"{r.get('nature','?'):>8}  {mark}{warn}")
    print()
    print("  ⚠️稀疏 = 取值离散度 <25%，多为事件型信号（大量并列），rank-IC 会失真，")
    print("     应改用事件研究（对比有/无事件两组的超额收益）而非 IC。")
    print()
    print("  【性质】区分两种在常规 IC 里长得一样、但用法完全不同的信号：")
    print("    择时     = 票内时变主导 → 同一标的相对自身均值的波动有预测力，")
    print("               **适合做每日评分**（例：risk_adj 固定+0.006 / 时变 −0.161）")
    print("    选股标签 = 固定效应主导 → 只是某类标的的身份标记，不随时间提供新信息，")
    print("               **只能做筛选池，塞进每日评分等于每天重新发现「MSFT 是大盘股」**")
    print("               （例：crowding_score 全样本看似显著且样本外延续，")
    print("                 但票内去均值后效应完全消失）")


def main() -> int:
    ap = argparse.ArgumentParser(description="Alpha Hive 单信号 IC 档案")
    ap.add_argument("--backfill", action="store_true", help="从历史 .swarm_results 回填")
    ap.add_argument("--analyze", action="store_true", help="分析每个信号的 IC")
    ap.add_argument("--list", action="store_true", help="列出已归档信号与覆盖度")
    ap.add_argument("--horizon", choices=["t7", "t30"], default="t7")
    ap.add_argument("--min-samples", type=int, default=50)
    ap.add_argument("--min-width", type=int, default=5)
    ap.add_argument("--draws", type=int, default=200)
    ap.add_argument("--db", type=str, default=str(DB_PATH))
    args = ap.parse_args()
    db = Path(args.db)

    if args.backfill:
        st = backfill(db_path=db)
        print(f"✅ 回填完成：{st['files']} 个文件 → {st['rows']} 行"
              f"（跳过 {st['skipped']}）")

    if args.list:
        ensure_schema(db)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            f"SELECT signal, COUNT(*) n, COUNT(DISTINCT date) d, "
            f"       MIN(date) mn, MAX(date) mx FROM {TABLE} "
            f"GROUP BY signal ORDER BY n DESC").fetchall()
        con.close()
        print(f"{'信号':<34}{'行数':>7}{'天数':>6}  范围")
        for s, n, d, mn, mx in rows:
            print(f"{s:<34}{n:>7}{d:>6}  {mn[:10]} ~ {mx[:10]}")

    if args.analyze:
        res = analyze(db, args.horizon, args.min_samples, args.min_width, args.draws)
        if not res:
            print("⏭  无足够数据，请先 --backfill")
            return 1
        rows, floor = res
        print_report(rows, floor, args.horizon)

    if not (args.backfill or args.analyze or args.list):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
