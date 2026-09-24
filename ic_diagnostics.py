#!/usr/bin/env python3
"""
🐝 Alpha Hive — 维度 IC 体检 (v0.42.6)
========================================
每次讨论「要不要调权重」之前**先跑这个**。

为什么需要多口径而不是一个 t 值
--------------------------------
T+7 前瞻收益在相邻交易日之间**高度重叠**（连续两天共享 5/7 的收益区间），
日度 IC 序列因此远非独立观测，朴素 t 检验会系统性高估显著性。实测：
risk_adj 日度 t=-3.72 → 不重叠周度 t=-2.56 → 剔除最极端 10 天后 t=-1.89。
T+30 更夸张：日度 -2.91 → 周度 -3.28（看似更强！）→ **真不重叠的月度只剩 -1.09**。
那个"周度 T+30"是最危险的读数——保留了大部分重叠，却披着"已做非重叠处理"的外衣。

所以本工具对每个维度同时给出四个口径，只有**多口径一致**才值得采信：

  1. 日度（重叠）      —— 与历史报告口径一致，t 偏高，仅作参照
  2. Newey-West(HAC)   —— 对自相关做 Bartlett 核调整
  3. 不重叠周度        —— 每 ISO 周取一个横截面，近似独立
  4. Jackknife         —— 剔除最极端 10 天，检验是否被少数日子驱动

外加 regime 分段（上涨期 vs 下跌震荡期）——catalyst 的全样本 IC≈0 其实是
"上涨期 +0.094 / 下跌期 −0.236"两个相反效应相消的假象。

用法
----
    /usr/local/bin/python3 ic_diagnostics.py                 # T+7 + T+30 全套
    /usr/local/bin/python3 ic_diagnostics.py --horizon t7    # 只看 T+7
    /usr/local/bin/python3 ic_diagnostics.py --json          # 机器可读
    /usr/local/bin/python3 ic_diagnostics.py --min-width 8   # 提高每日最少标的数

⚠️ 目标变量用 `return_t7`（毛收益、带符号价格变动）而**不是** `net_return_t7`。
后者是**方向调整后**的损益（看空标的下跌 → net 为正），而 dimension_scores 是
"看多度"分数，两者配对在语义上是错的。实测 RKLB bearish：return_t7=+10.05
而 net_return_t7=−10.37。
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev
from typing import Dict, List, Optional, Tuple

# v0.45.160：`DB_PATH` 现在是**覆盖钩子**，默认 `None` ⇒ 运行时解析 `PATHS.db`。
# 保留这个名字是因为 `tests/` 有多处 `monkeypatch.setattr(<mod>, "DB_PATH", ...)`
# 依赖它做隔离；把它删掉会把那些测试打红。
DB_PATH = None

def _db_path() -> Path:
    """本模块用的 `pheromone.db` 路径。**调用时求值。**

    v0.45.160：这里原本是 `Path(__file__).parent / "pheromone.db"`。
    那个写法比「模块级常量冻在 import 期」更彻底——它**压根不读任何环境变量**：
    `ALPHA_HIVE_DB_PATH` / `ALPHA_HIVE_HOME` 设成什么都无效，连改成懒求值都救不了，
    只能显式改去读 `PATHS.db`。于是 `tests/conftest.py::_isolate_env` 对它完全无效。

    ⚠️ 下面所有默认参数一律写 `None`，**不要**写 `= DB_PATH` 或 `= _db_path()`——
    默认参数在 `def` 执行时（＝import 期）求值，等于换个地方冻同一个值
    （同型 v0.45.37 / v0.45.150）。
    """
    if DB_PATH is not None:
        return Path(DB_PATH)
    from hive_logger import PATHS
    return Path(PATHS.db)
DIMS = ["signal", "catalyst", "sentiment", "odds", "risk_adj"]

# regime 分段（按 SPY 月度 T+7 收益方向划分，见 CHANGELOG v0.42.3）
UP_MONTHS = {"2026-04", "2026-05", "2026-06"}
DOWN_MONTHS = {"2026-02", "2026-03", "2026-07"}

HORIZONS = {
    "t7": ("return_t7", "checked_t7", 7, "周"),
    "t30": ("return_t30", "checked_t30", 30, "月"),
}

# ⚠️ 目标变量口径（v0.42.7 新增，默认 price）
#
# `return_t7` **不是**纯 T+7 前瞻收益，而是 `backtester._simulate_trade_path()`
# 产出的**路径依赖模拟交易收益**：逐日扫 OHLC，触及止损/止盈即提前出场。
# 实测 776 条 checked_t7 里 SL 195 条 + TP 135 条 = **330 条（42.5%）提前出场**，
# 其收益被钉在出场档位而非真实价格变动：
#     +9.945 出现 93 次、−5.048 出现 61 次、−10.045 出现 21 次…
# 与真实价格变动的一致率仅 **88.9%**。
#
# 对 rank-IC 的影响：Spearman 只看序，而档位截断会制造大量**并列值**，
# 破坏尾部排序信息——而尾部恰是 IC 信息量最集中的地方。
#
# 因此默认改用 `price`：直接由 `(price_t7 − price_at_predict) / price_at_predict`
# 计算，无截断、无并列人为聚集。两种口径的实测差异见 CHANGELOG v0.42.7
# （结论未被推翻，但 risk_adj 在干净口径下从 3/4 升到 **4/4**）。
#
# 何时该用 path：想问"这个维度能否预测我的**实际交易盈亏**"（含止损止盈规则）。
# 何时该用 close：想问"这个维度能否预测**前瞻收益**"——IC 框架的标准问题。
#
# ⚠️⚠️ v0.45.19 更正：**`price` 口径并不干净**。
# 上面 v0.42.7 那段推理是对的，但选错了列——`price_t7` 存的是
# `path["exit_price"]`，也就是**离场价**。实测自 2026-05 起它 100% 等于
# `exit_price`（`SELECT COUNT(*) FROM predictions WHERE checked_t7=1
#  AND ABS(price_t7-exit_price)<0.01` 可自查）。所以 `price` 口径与 `path`
# 一样带 SL/TP 截断，"无截断、无并列人为聚集"这句话对 5 月后的样本是错的。
#
# 真正未截断的 T+7 收盘价是 v0.45.19 新增的 **`close_t7`**（由
# `backfill_dir_accuracy.py` 重新取数回填，927 条全覆盖），故默认改为 `close`。
# `price` 保留仅为复现历史报告，**不要用它出新结论**。
TARGETS = ("close", "price", "path")

#: 「未截断收盘价」口径下每个 horizon 的**终点价列**（v0.45.321 抽成唯一真相，
#: 本模块 `load_daily_ic` 与 `signal_archive.load_panel` 共用）。
#:
#: · t7 → `close_t7`：见上一段。`price_t7` 是离场价，**不能**按 `f"price_{h}"` 拼列名——
#:   `signal_archive.load_panel` 正是这么拼的，v0.45.19 更正了这里却没更正那里，
#:   它的 `--analyze` 一直对着截断收益算 IC 直到 v0.45.321。
#: · t30 → `price_t30`：这一列**本来就是收盘价**，不是回退。`backtester` 的 SL/TP
#:   路径模拟只包在 t7 分支里；T+1 / T+30 走「沿用旧逻辑」分支，用
#:   `_get_price_at_date`（与 close_t7 同一个取价函数）。生产库实测（2026-09-23 快照）
#:   price_t30 == exit_price 0/837，SL/TP 行 0/360。没有 `close_t30` 列，也不需要。
#:
#: ⚠️ 新增 horizon 必须在这里显式登记，并先核对那一列存的是收盘价还是离场价。
FORWARD_CLOSE_COL = {"t7": "close_t7", "t30": "price_t30"}


def forward_return_sql(target: str, target_col: str,
                       horizon: str) -> Tuple[str, str, Optional[str]]:
    """前瞻收益口径 → `(SELECT 片段, WHERE 片段, 终点价列)`。**本模块读前瞻收益的唯一入口。**

    v0.45.328：`load_daily_ic` 与 `build_benchmark_panel` 以前各拼各的 SQL。v0.45.19 把前者
    改成 close_t7，后者照旧 `f"price_{horizon}"` —— `--benchmark` 在 t7 上一直对着 SL/TP
    离场价算，也从没读过传进来的 `target_col`（`--target` 对它无效），于是同一次默认运行里
    维度表与基准表对同一个维度印出两个不同的 IC。两个读者共用这一处，才不会再分叉。

    终点价列为 None ⇒ path 口径（直接读 `target_col`，不经价格）。
    列名在**调用时**查 `FORWARD_CLOSE_COL`，不冻结。
    """
    if target == "close":
        if horizon not in FORWARD_CLOSE_COL:
            # 不猜：按 f"price_{horizon}" 拼列名正是 v0.45.19 / v0.45.321 / v0.45.328 修掉的那个 bug
            raise ValueError(f"horizon={horizon!r} 未在 FORWARD_CLOSE_COL 登记"
                             f"（已登记：{sorted(FORWARD_CLOSE_COL)}）")
        end = FORWARD_CLOSE_COL[horizon]
    elif target == "price":
        end = f"price_{horizon}"          # 仅复现历史：t7 上是 SL/TP 离场价
    elif target == "path":
        return f"{target_col} AS ret", f"{target_col} IS NOT NULL", None
    else:
        raise ValueError(f"target={target!r} 不在 {TARGETS}")
    return (f"price_at_predict, {end} AS p_end",
            f"{end} IS NOT NULL AND price_at_predict > 0", end)


def row_forward_return(r, end_col: Optional[str]) -> Optional[float]:
    """按 `forward_return_sql` 取出的行算前瞻收益（%）。取不到（价为空/非正）返回 None。"""
    if end_col is None:
        return r["ret"]
    p0, p1 = r["price_at_predict"], r["p_end"]
    if not p0 or not p1 or p0 <= 0:
        return None
    return (p1 - p0) / p0 * 100.0


#: 终点价列里「恰好等于 SL/TP 离场价」的行占 SL/TP 行的比例超过它 ⇒ 告警。
#: 2026-09-23 快照实测：price_t7 87.6%（540 行）、close_t7 0.2%、price_t30 0% —— 两端相距甚远。
#: v0.45.328 从 signal_archive 搬来（v0.45.321 首设），两边共用这一个阈值。
TRUNCATION_ALARM = 0.5


def truncation_share(con, end_col: str, checked_col: str) -> Tuple[int, float]:
    """终点价列的**截断指纹**：SL/TP 行里它有多大比例恰好等于 `exit_price`。

    读对列只是代码层；这里管数据层 —— 哪天有人把离场价写进 close_t7、
    或给 t30 也套上路径模拟，列名没变、测试不红，只有这个比例会跳。
    库里没有 exit_* 列（旧库 / 测试夹具）⇒ (0, 0.0)，无从判断即不告警。
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
    if not {"exit_price", "exit_reason"} <= cols:
        return 0, 0.0
    n, hit = con.execute(
        f"SELECT COUNT(*), SUM(ABS({end_col} - exit_price) < 0.01) FROM predictions "
        f"WHERE {checked_col}=1 AND {end_col} IS NOT NULL AND exit_price IS NOT NULL "
        f"  AND exit_reason IN ('SL', 'TP')").fetchone()
    return n, ((hit or 0) / n if n else 0.0)


def warn_if_truncated(con, end_col: str, checked_col: str) -> Tuple[int, float]:
    """截断指纹超阈值 ⇒ stderr 告警。只该在**自称未截断**的口径（close）上调用：
    price / path 口径本来就标明含截断，在那里告警是恒真的噪音，会把告警养成被无视的东西。"""
    n_sltp, share = truncation_share(con, end_col, checked_col)
    if share > TRUNCATION_ALARM:
        print(f"⚠️  {end_col}：SL/TP 行里 {share:.0%}（{n_sltp} 行）恰好等于 exit_price"
              f" —— 这一列看起来是离场价而不是收盘价，下面的 IC 是对着截断收益算的，"
              f"勿据此下结论（见 ic_diagnostics.FORWARD_CLOSE_COL）", file=sys.stderr)
    return n_sltp, share


# ────────────────────────────────────────────────────────────────────────────
# 统计工具
# ────────────────────────────────────────────────────────────────────────────

def spearman(x: List[float], y: List[float]) -> Optional[float]:
    """Spearman 秩相关（含并列值的平均秩处理），纯 stdlib 实现。"""
    n = len(x)
    if n < 2:
        return None

    def rank(v: List[float]) -> List[float]:
        order = sorted(range(n), key=lambda i: v[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx, ry = rank(x), rank(y)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def basic_stats(vals: List[float]) -> Tuple[float, float, float, int]:
    """返回 (mean, SE, t, n)。"""
    n = len(vals)
    if n < 2:
        return (float("nan"),) * 3 + (n,)
    m = mean(vals)
    se = stdev(vals) / math.sqrt(n)
    return m, se, (m / se if se else float("nan")), n


def newey_west_t(vals: List[float], lag: int) -> float:
    """HAC 调整的 t 统计量（Bartlett 核）——对 IC 序列自相关做修正。"""
    n = len(vals)
    if n < 3:
        return float("nan")
    m = mean(vals)
    d = [v - m for v in vals]
    var = sum(v * v for v in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        gL = sum(d[i] * d[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lag + 1)) * gL
    if var <= 0:
        return float("nan")
    return m / math.sqrt(var / n)


def normal_two_sided_p(t: float) -> float:
    if not math.isfinite(t):
        return float("nan")
    return 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))


def subsample_non_overlapping(ic_by_day: Dict[str, float], period: str) -> List[float]:
    """每个周期取第一个可用交易日，近似消除前瞻收益重叠。

    period="周" → 按 ISO 周；period="月" → 按年月。
    T+30 必须用月度才算真不重叠——周度取样仍重叠约 4 倍。
    """
    seen: Dict = {}
    for d in sorted(ic_by_day):
        if period == "月":
            key = d[:7]
        else:
            key = datetime.date.fromisoformat(d).isocalendar()[:2]
        if key not in seen:
            seen[key] = ic_by_day[d]
    return list(seen.values())


# ────────────────────────────────────────────────────────────────────────────
# 数据加载与 IC 计算
# ────────────────────────────────────────────────────────────────────────────

def load_daily_ic(db_path: Path, target_col: str, checked_col: str,
                  min_width: int = 5, target: str = "close",
                  horizon: str = "t7") -> Tuple[Dict[str, Dict[str, float]], int, List[int]]:
    """按日横截面 Spearman rank-IC。

    Args:
        target: "close" = 由 close_t7 与 price_at_predict 算（**推荐，真正无截断**）；
                "price" = 由 price_{h} 算——⚠️ price_t7 实为离场价，同样被截断，
                          仅为复现历史报告保留；
                "path"  = 直接用 return_{h} 列（含 SL/TP 提前出场）
                详见模块顶部 TARGETS 说明；SQL 由 `forward_return_sql` 统一生成。

    Returns:
        (ic[dim][date], 样本行数, 每日横截面宽度列表)
    """
    sel, where, end_col = forward_return_sql(target, target_col, horizon)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT date, dimension_scores, {sel} FROM predictions "
            f"WHERE {checked_col}=1 AND {where} AND dimension_scores IS NOT NULL"
        ).fetchall()
        if target == "close":
            warn_if_truncated(con, end_col, checked_col)
    finally:
        con.close()

    by_day: Dict[str, List] = defaultdict(list)
    for r in rows:
        try:
            ds = json.loads(r["dimension_scores"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not (isinstance(ds, dict) and ds):
            continue
        ret = row_forward_return(r, end_col)
        if ret is None:
            continue
        by_day[r["date"][:10]].append((ds, ret))

    ic: Dict[str, Dict[str, float]] = {d: {} for d in DIMS}
    widths: List[int] = []
    for day, items in sorted(by_day.items()):
        if len(items) < min_width:
            continue
        widths.append(len(items))
        for dim in DIMS:
            pairs = [(i[0].get(dim), i[1]) for i in items
                     if i[0].get(dim) is not None and i[1] is not None]
            if len(pairs) < min_width or len(set(a for a, _ in pairs)) < 2:
                continue
            c = spearman([a for a, _ in pairs], [b for _, b in pairs])
            if c is not None:
                ic[dim][day] = c
    return ic, len(rows), widths


def diagnose(ic_by_day: Dict[str, float], lag: int, period: str) -> Dict:
    """对单个维度的日度 IC 序列跑四口径 + regime 分段。"""
    days = sorted(ic_by_day)
    vals = [ic_by_day[d] for d in days]
    if len(vals) < 5:
        return {}

    m, se, t, n = basic_stats(vals)
    nw_t = newey_west_t(vals, lag)

    sub = subsample_non_overlapping(ic_by_day, period)
    sm, sse, st_, sn = basic_stats(sub)
    ci = (sm - 1.96 * sse, sm + 1.96 * sse) if sn > 1 else (float("nan"),) * 2

    # jackknife：朝 0 方向剔除最极端的 10 天
    ordered = sorted(vals)
    keep = ordered[10:] if m < 0 else ordered[:-10]
    jm, _, jt, jn = basic_stats(keep) if len(keep) > 1 else ((float("nan"),) * 3 + (0,))

    up = [v for d, v in ic_by_day.items() if d[:7] in UP_MONTHS]
    dn = [v for d, v in ic_by_day.items() if d[:7] in DOWN_MONTHS]
    um, _, ut, un = basic_stats(up) if len(up) > 1 else ((float("nan"),) * 3 + (0,))
    dm, _, dt, dn_n = basic_stats(dn) if len(dn) > 1 else ((float("nan"),) * 3 + (0,))

    passed = sum(1 for x in (t, nw_t, st_, jt) if math.isfinite(x) and abs(x) >= 2.0)

    return {
        "daily_ic": m, "daily_t": t, "n_days": n,
        "nw_t": nw_t,
        "sub_ic": sm, "sub_t": st_, "sub_n": sn, "sub_ci": ci,
        "sub_p_bonferroni": min(normal_two_sided_p(st_) * len(DIMS), 1.0),
        "jack_ic": jm, "jack_t": jt, "jack_n": jn,
        "up_ic": um, "up_t": ut, "up_n": un,
        "down_ic": dm, "down_t": dt, "down_n": dn_n,
        "passed_methods": passed,
    }


# ────────────────────────────────────────────────────────────────────────────
# 基准套件（v0.43.1）
# ────────────────────────────────────────────────────────────────────────────
#
# 为什么必须有基准
# ----------------
# 单看"综合分 IC = −0.09, t = −2.8"无法回答唯一重要的问题：
# **这比什么都不做强吗？** 2026-07-30 的排查里，正是因为临时加了一个
# 20 日动量对照，才发现连这个被验证几十年的经典因子在同一批数据上
# 也只过 1/4 —— 结论从"蜂群设计有问题"变成"这个任务在这个尺度上极难"，
# 指向完全不同的行动。没有基准，那次会给出方向正确但代价高昂的错误建议。
#
# 本套件提供三类参照：
#   1. **噪音地板**：随机排序重复 N 次 → |IC| 的 95 分位。任何低于此带的
#      "信号"都不可信。这把"过 1/4 口径"这种模糊说法变成一个具体数字。
#   2. **经典因子**：20日动量 / 5日反转 / 已实现波动。它们是"别人早就
#      做出来的东西"，打不过它们就没有理由用复杂系统。
#   3. **系统自身**：综合分 + 5 个维度。
#
# 使用规则（写给未来的自己）
# ------------------------
# **任何评分/权重改动上线前，必须先跑 `--benchmark` 并证明它相对基准有改善。**
# 只比"改动前的自己"好是不够的 —— 那是在噪音里挑选。
#
# ⚠️ v0.45.328 之前 t7 的 `--benchmark` 是对着 SL/TP 离场价（price_t7）算的，
# 与维度表不同口径、也不认 `--target`。引用旧基准数字前先看 CHANGELOG v0.45.328。

RANDOM_DRAWS = 200          # 噪音地板的重采样次数
_PRICE_CACHE: Dict = {}


def _close_panel(yf, tickers: List[str], start: str, end: str):
    """一次 `yf.download` → 收盘价面板（列 = ticker）。

    多票时 `["Close"]` 本就是「列 = ticker」；单票在旧版 yfinance 上是 Series、新版是单列
    DataFrame（memory `alpha-hive-yfinance-multiindex`）—— 重试路径逐只下载两种都会遇到，
    拍成同一形状。
    """
    px = yf.download(list(tickers), start=start, end=end,
                     progress=False, auto_adjust=True)["Close"]
    if not hasattr(px, "columns"):
        px = px.to_frame(name=tickers[0])
    return px


def missing_tickers(px, tickers: List[str]) -> List[str]:
    """请求了、但面板里一个有效收盘价都没有的标的（缺列，或整列 NaN）。

    yfinance 批量下载的部分失败两种形状都有：失败的票要么整列 NaN，要么根本没有列。
    只判「列在不在」会把整列 NaN 的票当成拿到了。
    """
    cols = set(px.columns) if px is not None else set()
    return sorted(t for t in set(tickers)
                  if t not in cols or not px[t].notna().any())


def _load_prices(tickers: List[str], start: str, end: str):
    """拉取收盘价（yfinance）。整批失败返回 None —— 基准降级而非整个工具崩掉。

    v0.45.332：批量下载的**部分**失败不抛异常。2026-09-23 连跑 5 次，4 次各随机丢 1–2 只
    （AVGO+RKLB / T+MSTR / CVX / ADBE），唯一痕迹是 yfinance 自己的一行 stderr
    `N Failed download(s)`；旧实现只在异常时降级 ⇒ 残缺面板照常返回，`--benchmark` 的
    经典因子行少算几只票、表上看不出来（20日动量 IC 0.0955–0.1142 随丢哪只而变）。
    现在：缺的票逐只重试一轮（实测一轮通常补齐）；仍缺的写 stderr 点名。下游要看覆盖率就对
    返回的面板调 `missing_tickers` —— 覆盖率从数据本身读，不另存一份（缓存命中时也成立）。

    **为什么不接 Twelve Data 兜底**（`close_correction` v0.45.257 接了）：那边要的是官方
    **原始**收盘，与数据源无关；这里是 `auto_adjust=True` 的**复权**价，而
    `twelve_data._fetch_rows` 不传 `adjust`，其默认复权口径未核实（待验证）。同一个横截面里
    混两个源，每只票的因子值就取决于它碰巧由谁供数 —— 正是本次要消灭的「随丢哪只而变」。
    缺口改为如实报告，不用别的口径填。

    一只都没拿到（空表）按整批失败处理，不逐只重试：v0.45.257 实测的 52/52 限流就是这个
    形状（不抛、返回空），逐只重试只会往同一个限流里再撞 52 次。
    """
    key = (tuple(sorted(tickers)), start, end)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]
    want = sorted(set(tickers))
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
        px = _close_panel(yf, want, start, end)
    except Exception as e:  # noqa: BLE001 - 基准是可选功能，任何失败都降级
        print(f"⚠️  行情拉取失败，价格类基准跳过：{e}", file=sys.stderr)
        return None

    missing = missing_tickers(px, want)
    if len(missing) == len(want):
        print(f"⚠️  行情批量下载 0/{len(want)} 只有数据（多半是整批限流），价格类基准跳过",
              file=sys.stderr)
        return None
    if missing:
        import pandas as pd
        recovered = []
        for t in missing:
            try:
                one = _close_panel(yf, [t], start, end)
            except Exception:  # noqa: BLE001 - 单只重试失败 = 该票仍缺，下面照实报告
                continue
            if t in one.columns and one[t].notna().any():
                px = pd.concat([px.drop(columns=[t], errors="ignore"), one[[t]]],
                               axis=1).sort_index()
                recovered.append(t)
        still = [t for t in missing if t not in recovered]
        if still:
            print(f"⚠️  行情缺 {len(still)}/{len(want)} 只（批量下载漏掉、逐只重试 1 轮仍无数据）："
                  f"{', '.join(still)} —— 下游只在其余 {len(want) - len(still)} 只上计算",
                  file=sys.stderr)
        else:
            print(f"ℹ️  行情批量下载漏了 {len(missing)}/{len(want)} 只"
                  f"（{', '.join(missing)}），逐只重试已补齐", file=sys.stderr)
    _PRICE_CACHE[key] = px
    return px


def build_benchmark_panel(db_path: Path, target_col: str, checked_col: str,
                          horizon: str, min_width: int = 5,
                          target: str = "close",
                          check_truncation: bool = True) -> Tuple[Dict[str, Dict], Dict]:
    """构造 ({因子名: {date: [(值, 前瞻收益), ...]}} 面板, 经典因子行情覆盖率)。

    前瞻收益口径与 `load_daily_ic` 同源（`forward_return_sql`），`target` 语义相同。

    check_truncation: close 口径下是否查截断指纹（默认查）。v0.45.336：只有**同一次运行里
    已经对同一库、同一 target/horizon 调过 `load_daily_ic`** 的调用方才该传 False（`main()`）——
    指纹只取决于 (终点列, checked 列)，与两边各自的行集无关，第二次查只会把同一行告警再印一遍。

    v0.45.332：第二个返回值是经典因子（📈📉🌪）的覆盖率 —— `status` ok / partial /
    unavailable、`n_priced`/`n_tickers`、`missing`、`n_factor_records`/`n_records`。
    此前行情缺票时这三行只在其余标的上算、与综合分不是同一样本，输出里无从得知
    （见 `_load_prices`）。**返回形状有意改成二元组**：旧调用方直接拿它当面板用会当场报错，
    而不是静默丢掉覆盖率。

    ⚠️ v0.45.328 更正：此前这里写死 `f"price_{horizon}"`、不读 `target_col`、不收 `target`
    ⇒ t7 的 `--benchmark`（综合分 / 5 维 / 经典因子 / 噪音地板）全部对着 SL/TP 离场价算，
    且 `--target` 对它无效；同一次默认运行里维度表（close_t7）与基准表（price_t7）
    对同一维度印出两个 IC。**v0.45.328 之前的 t7 基准输出都是截断口径**，t30 不受影响
    （price_t30 本来就是收盘价，见 FORWARD_CLOSE_COL）。
    """
    sel, where, end_col = forward_return_sql(target, target_col, horizon)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT date, ticker, final_score, dimension_scores, {sel} "
            f"FROM predictions WHERE {checked_col}=1 AND {where}"
        ).fetchall()
        if target == "close" and check_truncation:
            warn_if_truncated(con, end_col, checked_col)
    finally:
        con.close()

    recs = []
    for r in rows:
        ret = row_forward_return(r, end_col)
        if ret is None:
            continue
        try:
            ds = json.loads(r["dimension_scores"]) if r["dimension_scores"] else {}
        except (json.JSONDecodeError, TypeError):
            ds = {}
        recs.append({
            "date": r["date"][:10], "ticker": r["ticker"],
            "score": r["final_score"], "ds": ds,
            "ret": ret,
        })
    if not recs:
        return {}, {}

    # 价格类基准（可选，失败则只保留系统自身）
    tickers = sorted({r["ticker"] for r in recs})
    dates = sorted({r["date"] for r in recs})
    px = _load_prices(tickers, "2025-11-01", dates[-1])
    missing = missing_tickers(px, tickers)
    mom20 = mom5 = vol20 = None
    if px is not None:
        try:
            import pandas as pd
            mom20, mom5, vol20 = {}, {}, {}
            for r in recs:
                t, d = r["ticker"], pd.Timestamp(r["date"])
                if t not in px.columns:
                    continue
                s = px[t].loc[:d].dropna()
                if len(s) < 26:
                    continue
                k = (t, r["date"])
                mom20[k] = (s.iloc[-1] / s.iloc[-21] - 1) * 100
                mom5[k] = (s.iloc[-1] / s.iloc[-6] - 1) * 100
                vol20[k] = s.pct_change().tail(20).std() * 100
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  价格因子构造失败，跳过：{e}", file=sys.stderr)
            mom20 = mom5 = vol20 = None

    panel: Dict[str, Dict] = defaultdict(lambda: defaultdict(list))
    rng = random.Random(20260730)
    for r in recs:
        d, k = r["date"], (r["ticker"], r["date"])
        panel["🐝 综合分 final_score"][d].append((r["score"], r["ret"]))
        for dim in DIMS:
            v = r["ds"].get(dim)
            if v is not None:
                panel[f"   └ {dim}"][d].append((v, r["ret"]))
        if mom20 is not None and k in mom20:
            panel["📈 20日动量"][d].append((mom20[k], r["ret"]))
            panel["📉 5日反转"][d].append((-mom5[k], r["ret"]))
            panel["🌪 低波动(1/vol20)"][d].append((-vol20[k], r["ret"]))
        panel["🎲 随机(单次抽样)"][d].append((rng.random(), r["ret"]))

    # 只保留满足最小宽度的日期
    out = {}
    for name, by_d in panel.items():
        out[name] = {d: v for d, v in by_d.items() if len(v) >= min_width}

    # 记录数也要比：缺票之外，`len(s) < 26` 等过滤同样会让经典因子少算一截
    n_factor = len(mom20) if mom20 is not None else 0
    if px is None or mom20 is None:
        status = "unavailable"
    elif missing or n_factor < len(recs):
        status = "partial"
    else:
        status = "ok"
    coverage = {
        "status": status,
        "n_tickers": len(tickers), "n_priced": len(tickers) - len(missing),
        "missing": missing,
        "n_records": len(recs), "n_factor_records": n_factor,
    }
    return out, coverage


def format_price_coverage(cov: Dict) -> str:
    """经典因子覆盖率的一行说明（`--benchmark` 表头用）。"""
    if not cov:
        return ""
    if cov["status"] == "unavailable":
        return "⚠️ 行情不可用 —— 经典因子（📈📉🌪）三行缺席，下表只有系统自身与随机"
    frac = (f"{cov['n_priced']}/{cov['n_tickers']} 只"
            f"（记录 {cov['n_factor_records']}/{cov['n_records']}）")
    if cov["status"] == "ok":
        return f"经典因子行情覆盖 {frac} ✅"
    miss = f"，缺 {', '.join(cov['missing'])}" if cov["missing"] else ""
    return (f"⚠️ 经典因子行情覆盖 {frac}{miss} —— 📈📉🌪 三行与综合分**不是同一样本**，"
            f"两者的比较打折扣")


def _ic_series_from_pairs(by_day: Dict[str, List]) -> Dict[str, float]:
    s = {}
    for d, pairs in by_day.items():
        xs = [a for a, _ in pairs]
        if len(set(xs)) < 2:
            continue
        c = spearman(xs, [b for _, b in pairs])
        if c is not None:
            s[d] = c
    return s


def noise_floor(panel: Dict[str, Dict], lag: int, period: str,
                draws: int = RANDOM_DRAWS, base_key: Optional[str] = None) -> Dict:
    """随机排序重复 draws 次 → |日度IC| 与 |周度t| 的 95 分位。

    这是"什么都不知道"长什么样。任何未超过此带的因子都不应被采信 ——
    包括系统自己的综合分。

    Args:
        base_key: 用哪个因子的**日期/宽度骨架**做随机化模板。
            必须显式指定或能匹配到默认值 —— 地板对骨架高度敏感：
            实测同一份 signal_archive 面板，基准取 `composite.final_score`
            （64 天 / 日均宽 10.4）得地板 0.076，取 `fund.pe_ratio`
            （49 天 / 日均宽 8.0）得 **0.116**，相差 52%，足以翻转多个信号的判定。
            旧实现写死匹配 `"🐝 综合分 final_score"`（只有 build_benchmark_panel
            会产生该键），在 signal_archive 面板上必然落空并静默 fallback 到
            `next(iter(...))` —— 即依赖 dict 迭代顺序（源于 SQL 行序），
            不是一个可复现的选择。
    """
    base = None
    for k in (base_key, "🐝 综合分 final_score", "composite.final_score"):
        if k and k in panel:
            base = panel[k]
            break
    if base is None:
        # 兜底：取覆盖天数最多的因子，而非 dict 首项 —— 至少是确定性的
        base = max(panel.values(), key=len, default={})
    if not base:
        return {}
    daily_abs, sub_abs, passed = [], [], []
    for i in range(draws):
        rng = random.Random(1000 + i)
        fake = {d: [(rng.random(), ret) for _, ret in pairs]
                for d, pairs in base.items()}
        s = _ic_series_from_pairs(fake)
        if len(s) < 10:
            continue
        r = diagnose(s, lag, period)
        if not r:
            continue
        daily_abs.append(abs(r["daily_ic"]))
        if math.isfinite(r["sub_t"]):
            sub_abs.append(abs(r["sub_t"]))
        passed.append(r["passed_methods"])

    def pct(v, q):
        if not v:
            return float("nan")
        v = sorted(v)
        return v[min(len(v) - 1, int(q * len(v)))]

    return {
        "n_draws": len(daily_abs),
        "ic_p95": pct(daily_abs, 0.95),
        "ic_p50": pct(daily_abs, 0.50),
        "sub_t_p95": pct(sub_abs, 0.95),
        "passed_mean": mean(passed) if passed else float("nan"),
        "passed_p95": pct(passed, 0.95),
    }


def print_benchmark(panel: Dict[str, Dict], lag: int, period: str,
                    floor: Dict, target_label: Optional[str] = None,
                    coverage: Optional[Dict] = None) -> None:
    rows = []
    for name, by_day in panel.items():
        s = _ic_series_from_pairs(by_day)
        if len(s) < 10:
            continue
        r = diagnose(s, lag, period)
        if r:
            rows.append((name, r))
    rows.sort(key=lambda x: -abs(x[1]["daily_ic"]))

    print("\n" + "=" * 104)
    print("【基准对照】任何改动上线前必须证明相对这组基准有改善"
          " —— 只比「改动前的自己」好，是在噪音里挑选")
    print("=" * 104)
    if target_label:
        # v0.45.328：点名终点列 —— 此前基准表静默用 price_t7，与上方维度表不是同一口径
        print(f"  目标={target_label}（与上方维度表同一口径）")
    if coverage:
        # v0.45.332：此前行情缺票时经典因子行静默少算几只，表上看不出来
        print(f"  {format_price_coverage(coverage)}")
    if floor:
        print(f"  🎯 噪音地板（随机排序 ×{floor['n_draws']} 次）："
              f"|日度IC| 中位 {floor['ic_p50']:.3f}、**95分位 {floor['ic_p95']:.3f}**"
              f" ｜ 通过口径数 均值 {floor['passed_mean']:.2f}、95分位 {floor['passed_p95']:.0f}/4")
        print("     ↑ 低于 95 分位的一律视为无信号，无论 t 值多好看")
    print("-" * 104)
    print(f"{'因子':<26}{'日度IC':>9}{'t':>8}{'NW t':>8}{'不重叠'+period+'t':>10}"
          f"{'通过':>6}   {'是否超出噪音地板':<16}")
    print("-" * 104)
    thr = floor.get("ic_p95", float("nan"))
    for name, r in rows:
        beats = (math.isfinite(thr) and abs(r["daily_ic"]) > thr)
        verdict = "✅ 超出" if beats else "❌ 噪音带内"
        print(f"{name:<26}{_fmt(r['daily_ic']):>9}{_fmt(r['daily_t'],'+.2f'):>8}"
              f"{_fmt(r['nw_t'],'+.2f'):>8}{_fmt(r['sub_t'],'+.2f'):>10}"
              f"{r['passed_methods']:>4}/4   {verdict:<16}")
    print()
    sysrow = next((r for n, r in rows if n.startswith("🐝")), None)
    best_ext = max((r for n, r in rows if n[0] in "📈📉🌪"),
                   key=lambda r: abs(r["daily_ic"]), default=None)
    if sysrow and best_ext:
        better = abs(sysrow["daily_ic"]) > abs(best_ext["daily_ic"])
        caveat = ""
        if coverage and coverage.get("status") == "partial":
            caveat = (f"（⚠️ 经典因子只覆盖 {coverage['n_factor_records']}/"
                      f"{coverage['n_records']} 条记录，不是同一样本）")
        print(f"  判定：综合分 |IC|={abs(sysrow['daily_ic']):.4f} vs "
              f"最佳经典因子 |IC|={abs(best_ext['daily_ic']):.4f} → "
              f"{'系统占优' if better else '⚠️ 系统未能超过经典因子'}{caveat}")
    if sysrow and math.isfinite(thr) and abs(sysrow["daily_ic"]) <= thr:
        print("  ⚠️ 综合分未超出噪音地板 —— 当前证据不支持任何基于它的选股决策")


# ────────────────────────────────────────────────────────────────────────────
# 输出
# ────────────────────────────────────────────────────────────────────────────

def _fmt(x: float, spec: str = "+.4f") -> str:
    return format(x, spec) if math.isfinite(x) else "  n/a"


def print_horizon(name: str, res: Dict, meta: Dict) -> None:
    period = meta["period"]
    print("=" * 100)
    print(f"【{name}】目标={meta['target']}")
    print(f"  样本行数={meta['rows']}  有效交易日={meta['n_days']}  "
          f"日均横截面宽度={meta['avg_width']:.1f} "
          f"(min {meta['min_width']} / max {meta['max_width']})")
    print("=" * 100)
    print(f"{'维度':<11}{'日度IC':>9}{'t(重叠)':>9}{'NW t':>8}"
          f"{'不重叠'+period+'IC':>12}{period+'t':>7}{'95%CI':>21}{'Bonf':>7}")
    print("-" * 100)
    for dim in DIMS:
        r = res.get(dim)
        if not r:
            continue
        lo, hi = r["sub_ci"]
        mark = "*" if math.isfinite(lo) and lo * hi > 0 else "0"
        ci = f"[{_fmt(lo,'+.3f')},{_fmt(hi,'+.3f')}]{mark}"
        print(f"{dim:<11}{_fmt(r['daily_ic']):>9}{_fmt(r['daily_t'],'+.2f'):>9}"
              f"{_fmt(r['nw_t'],'+.2f'):>8}{_fmt(r['sub_ic']):>12}"
              f"{_fmt(r['sub_t'],'+.2f'):>7}{ci:>21}"
              f"{_fmt(r['sub_p_bonferroni'],'.3f'):>7}")
    print(f"\n  CI 尾标 * = 不含 0（显著）｜0 = 含 0。Bonf = 不重叠{period}度 p 值 ×{len(DIMS)} 校正")


def print_robustness(res: Dict) -> None:
    print("\n" + "=" * 100)
    print("【稳健性：jackknife + regime 分段】")
    print("=" * 100)
    print(f"{'维度':<11}{'全样本IC':>10}{'剔极端10天':>12}{'t':>8}   "
          f"{'上涨期(4-6月)':>20}{'下跌震荡(2,3,7月)':>22}")
    print("-" * 100)
    for dim in DIMS:
        r = res.get(dim)
        if not r:
            continue
        flag = " ←失效" if (math.isfinite(r["jack_t"]) and abs(r["jack_t"]) < 2
                          <= abs(r["daily_t"])) else ""
        print(f"{dim:<11}{_fmt(r['daily_ic']):>10}{_fmt(r['jack_ic']):>12}"
              f"{_fmt(r['jack_t'],'+.2f'):>8}{flag:<7}"
              f"{_fmt(r['up_ic']):>9}(t{_fmt(r['up_t'],'+.2f')},n{r['up_n']})"
              f"{_fmt(r['down_ic']):>11}(t{_fmt(r['down_t'],'+.2f')},n{r['down_n']})")


def print_scoreboard(res: Dict) -> None:
    print("\n" + "=" * 100)
    print("【口径对照：同一维度在四种口径下的 t 值】")
    print("=" * 100)
    print(f"{'维度':<11}{'日度重叠':>10}{'Newey-West':>12}{'不重叠':>9}"
          f"{'剔10天':>9}{'结论':>24}")
    print("-" * 100)
    verdicts = {4: "四口径全过", 3: "三口径过", 2: "半数口径过",
                1: "仅一口径过", 0: "无口径通过"}
    for dim in DIMS:
        r = res.get(dim)
        if not r:
            continue
        print(f"{dim:<11}{_fmt(r['daily_t'],'+.2f'):>10}{_fmt(r['nw_t'],'+.2f'):>12}"
              f"{_fmt(r['sub_t'],'+.2f'):>9}{_fmt(r['jack_t'],'+.2f'):>9}"
              f"{verdicts[r['passed_methods']]:>24}")
    print("\n  ⚠️ 只有多口径一致才值得采信。单看日度 t 会因 T+N 收益重叠而系统性高估显著性。")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Alpha Hive 维度 IC 体检（调权重前必跑）")
    ap.add_argument("--horizon", choices=["t7", "t30", "both"], default="both",
                    help="前瞻期（默认 both）")
    ap.add_argument("--min-width", type=int, default=5,
                    help="每日最少标的数，低于此值的交易日不参与（默认 5）")
    # v0.45.160：argparse 的 default 在 import 期求值，不能塞冻结值
    ap.add_argument("--db", type=str, default=None, help="pheromone.db 路径（默认 PATHS.db）")
    ap.add_argument("--target", choices=TARGETS, default="close",
                    help="目标变量口径：close=未截断收盘价（默认，推荐；列见 FORWARD_CLOSE_COL）；"
                         "price=price_{h} 列（t7 上是 SL/TP 离场价，仅复现历史报告）；"
                         "path=return_{h} 列（路径依赖，含 SL/TP 截断）")
    ap.add_argument("--benchmark", action="store_true",
                    help="对照基准套件：噪音地板 + 经典因子 + 系统各维度（需联网拉行情）")
    ap.add_argument("--draws", type=int, default=RANDOM_DRAWS,
                    help=f"噪音地板的随机重采样次数（默认 {RANDOM_DRAWS}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    args = ap.parse_args()

    # v0.45.160：argparse 的 default 是 None（不能塞 import 期冻结值），此处解析
    db = Path(args.db) if args.db else _db_path()
    if not db.exists():
        print(f"❌ 找不到数据库：{db}", file=sys.stderr)
        return 1

    horizons = ["t7", "t30"] if args.horizon == "both" else [args.horizon]
    out: Dict[str, Dict] = {}

    for h in horizons:
        target, checked, lag, period = HORIZONS[h]
        if args.target == "close" and h != "t7":
            # v0.45.321 更正：旧文案说这里「回退 price 口径、仍含 SL/TP 截断」——前半对、
            # 后半错。price_t30 本来就是收盘价（见 FORWARD_CLOSE_COL），照旧点名列，但不再误报截断
            print(f"ℹ️  {h}: 终点价用 {FORWARD_CLOSE_COL[h]}（无 close_{h} 列；"
                  f"该列即 T+30 收盘价，SL/TP 路径模拟只作用于 t7，不含截断）")
        ic, n_rows, widths = load_daily_ic(db, target, checked, args.min_width,
                                           target=args.target, horizon=h)
        if not widths:
            print(f"⏭  {h}: 无满足 min-width={args.min_width} 的交易日，跳过")
            continue
        res = {dim: diagnose(ic[dim], lag, period) for dim in DIMS}
        res = {k: v for k, v in res.items() if v}
        label = {"close": f"{FORWARD_CLOSE_COL[h]} 相对 price_at_predict（未截断收盘价）",
                 "price": f"price_{h} 相对 price_at_predict（⚠️ t7 为 SL/TP 离场价，仅复现历史）",
                 }.get(args.target, f"{target}（路径依赖，含 SL/TP 截断）")
        meta = {
            "target": label, "target_mode": args.target,
            # v0.45.328：终点价列（path 口径为 None）；维度表与 --benchmark 共用
            "end_col": forward_return_sql(args.target, target, h)[2],
            "rows": n_rows, "n_days": len(widths),
            "avg_width": mean(widths), "min_width": min(widths),
            "max_width": max(widths), "period": period,
        }
        out[h] = {"meta": meta, "dims": res}

        if not args.json:
            title = f"T+{7 if h == 't7' else 30} 横截面 rank-IC"
            print()
            print_horizon(title, res, meta)
            if h == "t7":
                print_robustness(res)
                print_scoreboard(res)

        if args.benchmark:
            # v0.45.336：上面的 load_daily_ic 已对同一库、同一口径查过截断指纹（结果与行集无关），
            # 这里再查只会把同一行告警印第二遍
            panel, coverage = build_benchmark_panel(db, target, checked, h, args.min_width,
                                                    target=args.target, check_truncation=False)
            if panel:
                floor = noise_floor(panel, lag, period, draws=args.draws)
                out[h]["benchmark"] = {
                    "target_mode": args.target, "end_col": meta["end_col"],
                    "price_coverage": coverage,
                    "noise_floor": floor,
                    "factors": {
                        n: diagnose(_ic_series_from_pairs(bd), lag, period)
                        for n, bd in panel.items()
                        if len(_ic_series_from_pairs(bd)) >= 10
                    },
                }
                if not args.json:
                    print_benchmark(panel, lag, period, floor, target_label=label,
                                    coverage=coverage)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
