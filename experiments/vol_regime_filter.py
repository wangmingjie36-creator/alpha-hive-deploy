#!/usr/bin/env python3
"""
🐝 Alpha Hive — 「中性」标签作为卖权风险过滤器的可交易性检验 (v0.45.13)
=====================================================================
回答一个具体问题：**蜂群的 `direction='neutral'` 标签，能不能用来决定「今天这只
标的不要卖期权」？**

缘起（含一次自我更正）
----------------------
2026-08-25 的会话里，我先报出一个结论：中性预测的「平静命中率」54.0% 远高于
方向单的 34.8%，z=5.43、**p=5.6e-08**，并据此建议「把这个波动率判别力做成
可交易的东西」。那个 p 值被两处高估，本工具的存在就是为了不再犯：

  A. **收益口径混淆（小偏差）**。`predictions.return_t7` 对走 SL/TP 的方向单存的是
     **钳位后的离场收益**（−10.04 / +9.95 这类值在库里反复出现），对中性单存的是
     原始收益。方向单 46% 走了 SL/TP，等于拿「被截断的收益」跟「原始收益」比平静度。
     ⚠️ 第一版脚本改用 `price_t7` 还原，**那也是错的**——`price_t7` 自 2026-05 起
     100% 等于 `exit_price`，同样被路径截断。库里当时**根本没有存方向单的真实
     T+7 收盘价**。v0.45.17 新增 `close_t7` 列（由 `backfill_dir_accuracy.py`
     重新取数回填）才真正解决，本脚本现已改读 `close_t7`。
     修正后差值 19.3pp → 18.2pp —— 偏差存在但不致命。

  B. **重叠窗口（致命）**。30 只标的每日滚动预测、持有期 T+7，820 条记录里
     绝大部分在时间上互相重叠。按不重叠 ISO 周聚合后 **N_eff = 21 周**，
     名义 N 高估约 39×。p=5.6e-08 按 N=919 算出，除以 √39 的尺度后不再成立。
     这正是 MEMORY「统计功效与扩池收益」条目警告过的陷阱。

结论不是「原结论作废」，而是「效应方向对，强度被夸大了约一个数量级」。

真正的发现：不是波动率预测，是双峰性标记
----------------------------------------
用原始收益重做，中性组呈**双峰/肥尾**，而非单纯「更安静」：

    |ret|<5%   中性 55.1% vs 方向单 36.9%   (+18.2pp)  ← 更常安静
    |ret|>10%  中性 25.7% vs 方向单 15.8%   (+9.9pp)   ← 也更常爆炸

所以「中性」的语义更接近**「蜂群没形成共识」**（弃权），而不是「预测低波动」。
弃权发生在信息冲突处，而信息冲突处的结果分布本就是双峰的。

这决定了它的交易用法：**不能在中性标的上卖权**（肥尾会吃掉全部权利金），
它是个**风险过滤器**，不是独立的多/空波动率策略。

方法
----
1. 用 `close_t7`（未截断的真实 T+7 收盘价）算收益，绕开 A 的双重钳位陷阱。
2. 期权损益用 **Black-Scholes** 实价，不用倍数近似；IV 取当日
   `signal_archive.options.iv_current`。卖方按 `0.95×IV` 成交以计入点差劣势。
3. 主结构：卖 ±1σ 宽跨式（iron-condor 的裸腿版），持有至 T+7 到期。
4. 三道对照，排除「中性只是别的东西的代号」：
   - 静态高波动标的黑名单
   - IV 水平过滤
   - 标的固定效应（同一标的内部比）
5. 功效按**不重叠 ISO 周**计，并给出达到显著所需周数（就绪度闸）。

⚠️ 已知局限（勿在报告里省略）
   - IV 用的是单一 `iv_current`，非真实期权链报价；未建模 skew、真实点差、
     早行权、保证金占用。结论应读作**相对排序**（过滤 vs 不过滤），
     不是可直接下单的绝对收益。
   - 到期损益忽略路径依赖：真实卖权会因保证金被迫中途平仓。
   - N_eff=21 周，样本方差自身相对标准误 ≈ √(2/20) ≈ 32%。
   - 与 `experiments/ticker_winrate_persistence.py` 的关系：那份证否了
     「标的历史胜率预测前向胜率」；本份检验的是**当日横截面标签**，不是标的属性，
     两者不冲突。
"""
from __future__ import annotations

import math
import sqlite3
import statistics as st
import datetime as dt
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "pheromone.db"

HOLD_TRADING_DAYS = 7
T = HOLD_TRADING_DAYS / 252
RISK_FREE = 0.045
SELLER_IV_HAIRCUT = 0.95      # 卖方成交价 = 0.95×IV，模拟点差劣势
STRIKE_SIGMA = 1.0            # 卖 ±1σ 宽跨式
IV_MIN, IV_MAX = 1.0, 300.0   # 哨兵过滤：IV=0 是缺失标记，非真实观测
BLACKLIST = {"VKTX", "RKLB", "CRCL", "BILI"}   # 对照用的静态高波动名单


# ── Black-Scholes ────────────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def bs_price(S: float, K: float, sigma: float, t: float,
             r: float = RISK_FREE, call: bool = True) -> float:
    if sigma <= 0 or t <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if call:
        return S * _norm_cdf(d1) - K * math.exp(-r * t) * _norm_cdf(d2)
    return K * math.exp(-r * t) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def short_strangle_pnl(S0: float, S_exp: float, iv_pct: float,
                       k_sigma: float = STRIKE_SIGMA,
                       haircut: float = SELLER_IV_HAIRCUT) -> float:
    """卖 ±k_sigma 宽跨式，持有到期。返回占标的价的百分比损益。"""
    sigma = iv_pct / 100.0
    move = sigma * math.sqrt(T)
    k_call, k_put = S0 * (1 + k_sigma * move), S0 * (1 - k_sigma * move)
    premium = (bs_price(S0, k_call, sigma * haircut, T, call=True)
               + bs_price(S0, k_put, sigma * haircut, T, call=False))
    payout = max(0.0, S_exp - k_call) + max(0.0, k_put - S_exp)
    return (premium - payout) / S0 * 100


# ── 取数 ─────────────────────────────────────────────────────────────────────
def load_records(db_path: Path = DB) -> list[dict]:
    """
    从 pheromone.db 取「预测 × 当日 IV × 原始 T+7 收益」。

    收益一律走 `close_t7`（v0.45.17 新增，未截断的真实 T+7 收盘价）。
    **既不用 `return_t7` 也不用 `price_t7`**——前者是钳位离场收益，后者自
    2026-05 起等同 `exit_price`，两者都被路径截断（见模块 docstring A）。
    `close_t7` 为空的行直接跳过，不做兜底还原。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("""
            SELECT p.direction, p.ticker, p.date,
                   p.price_at_predict, p.close_t7, CAST(s.value AS REAL)
            FROM predictions p
            JOIN signal_archive s
              ON s.date = p.date AND s.ticker = p.ticker
             AND s.signal = 'options.iv_current'
            WHERE p.checked_t7 = 1
              AND p.close_t7 IS NOT NULL
              AND p.price_at_predict > 0
        """).fetchall()
    finally:
        conn.close()

    out = []
    for direction, ticker, date, p0, p7, iv in rows:
        if iv is None or not (IV_MIN < iv <= IV_MAX):
            continue
        iso_y, iso_w, _ = dt.date.fromisoformat(date).isocalendar()
        out.append({
            "direction": direction,
            "is_neutral": direction == "neutral",
            "ticker": ticker,
            "date": date,
            "week": f"{iso_y}-W{iso_w:02d}",
            "iv": iv,
            "raw_ret": (p7 / p0 - 1) * 100,
            "pnl": short_strangle_pnl(p0, p7, iv),
        })
    return out


# ── 统计工具 ─────────────────────────────────────────────────────────────────
def _sharpe(vals: list[float], periods: int = 36) -> float:
    if len(vals) < 2 or st.stdev(vals) == 0:
        return 0.0
    return st.mean(vals) / st.stdev(vals) * math.sqrt(periods)


def _describe(vals: list[float]) -> dict:
    s = sorted(vals)
    n = len(s)
    return {
        "n": n,
        "mean": st.mean(s),
        "win": sum(1 for x in s if x > 0) / n * 100,
        "p05": s[int(n * 0.05)],
        "worst": s[0],
        "sharpe": _sharpe(s),
    }


def _sign_test(diffs: list[float]) -> float:
    n = len(diffs)
    k = sum(1 for d in diffs if d > 0)
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n


# ── 各节分析 ─────────────────────────────────────────────────────────────────
def section_filter(recs: list[dict]) -> None:
    print("── 1. 中性过滤对卖 1σ 宽跨式的影响 ──")
    print(f"{'组合':<24}{'n':>5}{'胜率':>8}{'均损益%':>10}{'p05%':>9}{'最差%':>9}{'SR':>7}")
    for label, subset in (
        ("全部（无过滤）", recs),
        ("剔除中性", [r for r in recs if not r["is_neutral"]]),
        ("仅中性（被剔除）", [r for r in recs if r["is_neutral"]]),
    ):
        d = _describe([r["pnl"] for r in subset])
        print(f"{label:<24}{d['n']:>5}{d['win']:>7.1f}%{d['mean']:>+10.2f}"
              f"{d['p05']:>+9.2f}{d['worst']:>+9.2f}{d['sharpe']:>7.2f}")


def section_controls(recs: list[dict]) -> None:
    print("\n── 2. 对照：中性标签是否只是别的规则的代号 ──")
    print(f"{'规则':<28}{'n':>5}{'均损益%':>10}{'SR':>7}{'最差%':>9}")
    for label, subset in (
        ("无过滤", recs),
        ("剔除中性（蜂群信号）", [r for r in recs if not r["is_neutral"]]),
        ("剔除高波标的（黑名单）", [r for r in recs if r["ticker"] not in BLACKLIST]),
        ("剔除 IV>73", [r for r in recs if r["iv"] <= 73]),
    ):
        p = [r["pnl"] for r in subset]
        print(f"{label:<28}{len(p):>5}{st.mean(p):>+10.2f}{_sharpe(p):>7.2f}{min(p):>+9.2f}")

    print("\n   标的固定效应（同标的内 中性均值 − 方向单均值）:")
    by = defaultdict(lambda: defaultdict(list))
    for r in recs:
        by[r["ticker"]]["neu" if r["is_neutral"] else "dir"].append(r["pnl"])
    diffs = []
    for tk in sorted(by):
        neu, dirs = by[tk]["neu"], by[tk]["dir"]
        if len(neu) >= 5 and len(dirs) >= 5:
            diffs.append(st.mean(neu) - st.mean(dirs))
    worse = sum(1 for d in diffs if d < 0)
    print(f"   可比标的 {len(diffs)} 只，中性更差 {worse} 只，"
          f"均值 {st.mean(diffs):+.2f}pp，符号检验 p={_sign_test([-d for d in diffs]):.4f}")


def section_power(recs: list[dict]) -> dict:
    print("\n── 3. 功效：按不重叠 ISO 周计（唯一诚实的 N）──")
    wk_all, wk_flt = defaultdict(list), defaultdict(list)
    for r in recs:
        wk_all[r["week"]].append(r["pnl"])
        if not r["is_neutral"]:
            wk_flt[r["week"]].append(r["pnl"])
    weeks = sorted(w for w in wk_all
                   if len(wk_all[w]) >= 5 and len(wk_flt.get(w, [])) >= 3)
    base = [st.mean(wk_all[w]) for w in weeks]
    filt = [st.mean(wk_flt[w]) for w in weeks]
    diffs = [f - b for f, b in zip(filt, base)]
    n = len(weeks)

    print(f"   N_eff = {n} 周 ({weeks[0]} → {weeks[-1]})；"
          f"名义 N={len(recs)} 高估约 {len(recs)/n:.0f}×")
    mean_d, sd_d = st.mean(diffs), st.stdev(diffs)
    t_stat = mean_d / (sd_d / math.sqrt(n))
    p_val = math.erfc(abs(t_stat) / math.sqrt(2))
    need = math.ceil((2.8 / (mean_d / sd_d)) ** 2) if mean_d > 0 else -1
    print(f"   过滤增量 {mean_d:+.3f}pp/周  t={t_stat:.2f}  p≈{p_val:.4f}  "
          f"正增量周 {sum(1 for d in diffs if d > 0)}/{n}")
    print(f"   符号检验 p={_sign_test(diffs):.4f}")
    verdict = "已显著" if p_val < 0.05 else f"未显著 — 约需 {need} 周（还差 {max(0, need-n)} 周）"
    print(f"   就绪度：{verdict}")
    return {"n_eff": n, "mean_delta": mean_d, "p": p_val, "weeks_needed": need}


def main() -> int:
    recs = load_records()
    if not recs:
        print("无可用样本（pheromone.db 缺 close_t7 或 options.iv_current，先跑 backfill_dir_accuracy.py）")
        return 3
    print(f"🐝 中性标签 · 卖权风险过滤器检验   样本 {len(recs)} 条\n")
    section_filter(recs)
    section_controls(recs)
    res = section_power(recs)
    # 结论一律由数据生成，**不写死**。第一版在这里硬编码了「中性是卖权风险
    # 过滤器」，等到换成未截断的 close_t7、效应归零之后，那句话还留在输出里
    # 自说自话——写死的结论就是这么变成谎话的。
    if res["p"] < 0.05 and res["mean_delta"] > 0:
        print(f"\n结论：过滤增量 {res['mean_delta']:+.3f}pp/周，p={res['p']:.4f} —— "
              "中性标签对卖权组合有统计显著的正贡献。")
        return 0
    if res["p"] < 0.05:
        print(f"\n结论：过滤增量 {res['mean_delta']:+.3f}pp/周（**为负**），"
              f"p={res['p']:.4f} —— 剔除中性反而更差，勿采用。")
        return 1
    print(f"\n结论：过滤增量 {res['mean_delta']:+.3f}pp/周，p={res['p']:.4f} —— "
          "**与零无法区分**。中性标签目前不构成可用的卖权过滤器；\n"
          "      在功效达标前不得据此改动交易行为。")
    return 3   # 3 = 未达显著，勿据此行动


if __name__ == "__main__":
    raise SystemExit(main())
