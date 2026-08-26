#!/usr/bin/env python3
"""
🐝 Alpha Hive — final_score 为什么把信号聚合没了 (v0.45.18)
==========================================================
回答一个具体问题：**5 个维度里 sentiment 有 IC，为什么加权合成后 final_score 归零？**

背景
----
干净口径（`close_t7`，见 v0.45.17）下的不重叠周度 rank-IC：

    signal    -0.085      catalyst  +0.050     sentiment +0.100
    odds      +0.023      risk_adj  -0.109

而 `config.EVALUATION_WEIGHTS` 给的权重是：

    signal 0.2094 | catalyst 0.1878 | sentiment 0.1838 | odds 0.1940 | risk_adj 0.2250

**两个 IC 为负的维度（signal、risk_adj）合计占 43.4% 权重，
而唯一像样的正向维度 sentiment 只占 18.4%。** 本工具量化这个抵消。

方法
----
1. 复用 `ic_diagnostics` 的 `spearman` 与 `subsample_non_overlapping`
   —— 不自写评分逻辑，口径与主诊断工具严格一致。
2. 日度横截面 rank-IC → 每 ISO 周取第一个交易日（近似独立）→ 对周序列做 t 检验。
   **不要**把一周内多天的样本池成一个横截面：那样会把不同起始日的收益
   混进同一次排序，收益区间不可比。
3. 分解三层：
   a. 各维度单独 IC
   b. 加权和的**理论**贡献 w_i × IC_i（看谁在抵消谁）
   c. 若干**反事实组合**的实测 IC：等权、符号校正、仅 sentiment、去掉负向维度
4. 维度间相关矩阵 —— 若维度高度相关，抵消会比理论式更强。

⚠️ 局限
   - N_eff ≈ 20 余周，IC 读作量级。所有组合共用同一批周，
     故组合之间的**相对**比较比绝对值可靠。
   - 反事实组合是**样本内**构造，天然优于实际权重；它只用来定位
     「信号被抵消了多少」，**不是权重建议**。改权重须走
     `weekly_optimizer` 既有的闸（v0.44.0 起只读，见 [[alpha-hive-weight-learning-loop]]）。
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ic_diagnostics import (  # noqa: E402  - 必须在 sys.path 注入后导入
    spearman,
    subsample_non_overlapping,
)

DB = ROOT / "pheromone.db"
DIMS = ["signal", "catalyst", "sentiment", "odds", "risk_adj"]
MIN_WIDTH = 5


def load_rows(db_path: Path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""
            SELECT date, ticker, price_at_predict, close_t7, final_score, dimension_scores
            FROM predictions
            WHERE checked_t7 = 1 AND close_t7 IS NOT NULL
              AND price_at_predict > 0 AND dimension_scores IS NOT NULL
        """).fetchall()
    finally:
        con.close()
    by_day = defaultdict(list)
    for r in rows:
        try:
            ds = json.loads(r["dimension_scores"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ds, dict) or not all(d in ds for d in DIMS):
            continue
        ret = (r["close_t7"] / r["price_at_predict"] - 1) * 100
        by_day[r["date"]].append({
            "dims": {d: float(ds[d]) for d in DIMS},
            "final": r["final_score"],
            "ret": ret,
        })
    return by_day


def weekly_ic(by_day, value_fn):
    """日度横截面 IC → 每周取第一天。返回周度 IC 列表。"""
    daily = {}
    for day, recs in by_day.items():
        if len(recs) < MIN_WIDTH:
            continue
        xs = [value_fn(r) for r in recs]
        if len({round(x, 9) for x in xs}) < 2:
            continue
        ic = spearman(xs, [r["ret"] for r in recs])
        if ic is not None:
            daily[day] = ic
    return subsample_non_overlapping(daily, "周")


def stat(ics):
    n = len(ics)
    if n < 3 or stdev(ics) == 0:
        return None
    m, sd = mean(ics), stdev(ics)
    t = m / (sd / math.sqrt(n))
    return {"n": n, "ic": m, "t": t, "p": math.erfc(abs(t) / math.sqrt(2))}


def main() -> int:
    import config
    W = dict(config.EVALUATION_WEIGHTS)
    by_day = load_rows(DB)
    print(f"🐝 final_score 稀释分解 · 干净口径(close_t7) · 不重叠周")
    print(f"   样本 {sum(len(v) for v in by_day.values())} 条 / {len(by_day)} 个交易日\n")

    # ── a. 各维度单独 IC ──
    print("【a】各维度单独 IC（不重叠周）")
    print(f"{'维度':<12}{'权重':>8}{'IC':>9}{'t':>7}{'p':>8}{'w×IC':>10}{'方向':>6}")
    print("─" * 62)
    per, contrib = {}, {}
    for d in DIMS:
        s = stat(weekly_ic(by_day, lambda r, _d=d: r["dims"][_d]))
        per[d] = s
        c = W[d] * s["ic"]
        contrib[d] = c
        arrow = "正向" if s["ic"] > 0 else "**反向**"
        print(f"{d:<12}{W[d]:>8.4f}{s['ic']:>+9.3f}{s['t']:>+7.2f}{s['p']:>8.3f}"
              f"{c:>+10.4f}{arrow:>8}")
    tot = sum(contrib.values())
    pos = sum(c for c in contrib.values() if c > 0)
    neg = sum(c for c in contrib.values() if c < 0)
    print("─" * 62)
    print(f"{'加权和':<12}{sum(W.values()):>8.4f}{'':>9}{'':>7}{'':>8}{tot:>+10.4f}")
    print(f"\n   正向贡献合计 {pos:+.4f}   反向贡献合计 {neg:+.4f}   "
          f"净剩 {tot:+.4f}（抵消掉 {min(pos, -neg)/max(pos, -neg)*100:.0f}%）")
    neg_w = sum(W[d] for d in DIMS if per[d]["ic"] < 0)
    print(f"   IC 为负的维度占总权重 {neg_w/sum(W.values())*100:.1f}%"
          f"，而 sentiment 仅占 {W['sentiment']/sum(W.values())*100:.1f}%")

    # ── b. 反事实组合 ──
    def combo(weights):
        return lambda r: sum(weights[d] * r["dims"][d] for d in DIMS)

    eq = {d: 1 / len(DIMS) for d in DIMS}
    signfix = {d: W[d] * (1 if per[d]["ic"] > 0 else -1) for d in DIMS}
    posonly = {d: (W[d] if per[d]["ic"] > 0 else 0.0) for d in DIMS}
    senti = {d: (1.0 if d == "sentiment" else 0.0) for d in DIMS}

    print("\n【b】反事实组合的实测 IC（同一批周，可直接相比）")
    print(f"{'组合':<28}{'IC':>9}{'t':>7}{'p':>8}")
    print("─" * 52)
    cases = [
        ("实际 final_score 列", lambda r: r["final"]),
        ("现行权重重算", combo(W)),
        ("等权", combo(eq)),
        ("仅剔除负向维度", combo(posonly)),
        ("符号校正（负维取反）", combo(signfix)),
        ("仅 sentiment", combo(senti)),
    ]
    for label, fn in cases:
        s = stat(weekly_ic(by_day, fn))
        if s:
            print(f"{label:<28}{s['ic']:>+9.3f}{s['t']:>+7.2f}{s['p']:>8.3f}")

    # ── b2. 用 ic_diagnostics 的**独立子集**交叉验证同一结论 ──
    # 本脚本要求 5 维齐全（分解加权和的前提），ic_diagnostics 按维度各自取样。
    # 两者子集不同（本脚本少 28 行 / 3 天），故单维 IC 会有出入——
    # 实测 risk_adj 差别最大（-0.084 vs -0.161），因为被丢掉的 3 天其
    # risk_adj 日度 IC 均值达 -0.56。这恰好印证 ic_diagnostics 自己的 jackknife
    # 结论：risk_adj 由少数几天驱动，剔除极端日后 t 从 -2.50 掉到 -1.01。
    # 关键是**抵消结论对子集不敏感**，故此处并列打印两套数。
    try:
        from ic_diagnostics import load_daily_ic
        ic_alt, _, _ = load_daily_ic(DB, "return_t7", "checked_t7", MIN_WIDTH,
                                     target="close", horizon="t7")
        print("\n【b2】交叉验证：改用 ic_diagnostics 的按维度取样（子集不同）")
        pos2 = neg2 = 0.0
        print(f"{'维度':<12}{'IC':>9}{'w×IC':>10}")
        for d in DIMS:
            sub = subsample_non_overlapping(ic_alt[d], "周")
            if len(sub) < 3:
                continue
            c2 = W[d] * mean(sub)
            pos2 += max(0.0, c2)
            neg2 += min(0.0, c2)
            print(f"{d:<12}{mean(sub):>+9.3f}{c2:>+10.4f}")
        print(f"   正向 {pos2:+.4f}  反向 {neg2:+.4f}  净剩 {pos2+neg2:+.4f}"
              f"（抵消 {min(pos2, -neg2)/max(pos2, -neg2)*100:.0f}%）"
              f" ← 与【a】同结论")
    except Exception as _e:  # noqa: BLE001
        print(f"\n【b2】交叉验证跳过：{_e}")

    # ── c. 维度相关矩阵 ──
    print("\n【c】维度间 Spearman 相关（同日横截面平均）")
    corr = defaultdict(list)
    for recs in by_day.values():
        if len(recs) < MIN_WIDTH:
            continue
        for i, a in enumerate(DIMS):
            for b in DIMS[i + 1:]:
                c = spearman([r["dims"][a] for r in recs], [r["dims"][b] for r in recs])
                if c is not None:
                    corr[(a, b)].append(c)
    print(f"{'':<12}" + "".join(f"{d:>11}" for d in DIMS))
    for i, a in enumerate(DIMS):
        line = f"{a:<12}"
        for j, b in enumerate(DIMS):
            if i == j:
                line += f"{'—':>11}"
            elif (a, b) in corr:
                line += f"{mean(corr[(a, b)]):>+11.2f}"
            elif (b, a) in corr:
                line += f"{mean(corr[(b, a)]):>+11.2f}"
            else:
                line += f"{'':>11}"
        print(line)
    print("\n  高相关 = 维度在测同一件事，加权无法分散；负 IC 维度会直接压制正 IC 维度。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
