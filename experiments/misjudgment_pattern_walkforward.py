#!/usr/bin/env python3
"""
误判模式预警的前向检验（v1.0, 2026-09-13）—— v0.45.215 撤掉 P2-⑧ 的决策依据

问题
────
P2-⑧（v0.20.0 ~ v0.45.214）把历史误判按 (方向, 首要归因, 前 3 个触发信号) 聚成
「误判模式」；同一模式累计 ≥3 次误判即激活，新预测命中该模式时在深度报告里发预警。
它的前提是：**过去反复误判的模式，会让命中它的新预测更容易误判**。本脚本检验该前提。

撤掉的实现本身还有两个与本检验无关、但让它无法自证的缺陷：写入端每周对全部历史
非幂等重放（hits 随运行次数膨胀），且服务端只造 9 个硬编码期权信号、训练端造 19 个
分位数信号（按服务端词表回放 638 条只触发 10 次）。本脚本检验的是**修好这两处之后**
的最优形态——这个形态都没证据，才构成撤掉而不是修的理由。

方法（杜绝前视）
──────────────
对每条已验证的方向预测（日期 t）：
  1. 模式库**全量重建**，只用 date ≤ t − gap（默认 14 日历日，任何假日组合下 ≥7 交易日）
     的已判定误判；模式按**不同误判日**计数，而不是按调用次数
  2. 信号阈值（final_score 三分位、蜂群原生信号 p75、每标的 IV 分位）只用 date < t 的数据
     —— 生产 pheromone_source 用全样本分位数，那是前视
  3. 命中规则与撤掉的 check_misjudgment_warnings 相同：同标的、同方向、
     模式信号与当前信号交集 ≥ 50%
标签用 dir_correct_t7（v0.45.17，基于**未截断**的 close_t7），剔除 dir_ambiguous_t7。
**不用** correct_t7 / return_t7：那是路径依赖、被 SL/TP 钳位的交易结果。

基线：同窗口、同标签下「该标的×方向的历史误判率」（Laplace 平滑）。一行统计量都打不过，
模式库就没有存在理由。独立旁证见 ticker_winrate_persistence.py（标的历史胜率不预测前向胜率）。

预注册闸门（只有一个，故不做多重比较校正）
──────────────────────────────────────
  Δresid = 预警组 (miss − prior) 均值 − 未预警组同量
  按 ISO 周整簇 bootstrap（同日横截面相关，计价单位是周不是行）。
    退出码 0：95% CI 下界 > 0   —— 有增量预测力，可以考虑以「读时计算」形式接回
    退出码 1：95% CI 上界 < 0   —— 显著反向，预警组反而更少误判
    退出码 3：CI 跨 0，或样本不足 —— 与零无法区分，勿据此改动任何行为
  误判率原始差、方向调整收益差只报告，不入闸。

用法
────
    /usr/local/bin/python3 experiments/misjudgment_pattern_walkforward.py
    /usr/local/bin/python3 experiments/misjudgment_pattern_walkforward.py --gap-days 21
    /usr/local/bin/python3 experiments/misjudgment_pattern_walkforward.py --report experiments/misjudgment_pattern_walkforward_report.md

2026-09-13 结论（默认参数复现：gap=14，方向单 638 条 / 20 个 ISO 周，预警 62 条 / 17 周）
────────────────────────────────────────────────────────────────────────────────
  误判率  预警 53.2% vs 未预警 46.0%   Δ +7.2pp  [−13.7, +26.0]
  Δresid（闸门）                       −3.6pp  [−24.5, +14.7]   → 退出码 3
  基线自身（历史误判率高于中位 vs 其余） +1.9pp  [−13.7, +14.9]
  收益差 −3.64% [−7.22, −0.04] 只在默认种子下不跨 0，属边缘：换种子 [−7.30, +0.14]、
  5/95 缩尾 [−6.19, +0.10] 均跨 0；按标的去均值减半到 −1.85% 且跨 0；
  62 条预警里 84% 来自 BILI/VKTX/NVDA —— 主要在编码「这只票常错」。gap=21 结论不变。
  功效：测 +10pp 真实差异约需 161 个 ISO 周（≈3 年，按 1/√周 粗估）。
  ⇒ **未证实（功效不足），不是证伪**。

  本脚本与撤掉前的生产代码（weekly_analyzer.classify_misjudgments + 原
  check_misjudgment_warnings + pheromone_source 信号，经 AST 原样抽取调用）逐行对账：
  gap=14/21 各 638 行，warned/miss/prior/ret **0 处不一致**；正对照变异
  OVERLAP_MIN=0.30 → 43 处、ACTIVE_MIN_DISTINCT=2 → 77 处（注意 0.34 是等价变异：
  模式至多 3 个信号键，交集比只取 0/⅓/½/⅔/1）。
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NEUTRAL_BAND_T7 = 2.5          # 与 pheromone_source / weekly_analyzer 的 T+7 口径一致
ACTIVE_MIN_DISTINCT = 3        # 撤掉的实现：hits_threshold=3
OVERLAP_MIN = 0.5              # 撤掉的实现：交集 ≥ 50% 视为同模式
DIR_ZH = {"bullish": "看多", "bearish": "看空"}

# 蜂群原生信号：值 ≥ 该键历史 p75 即为真（与 pheromone_source 相同）
SWARM_KEYS = [
    ("composite.swarm_agreement", "swarm_agreement_high"),
    ("guard.consistency", "guard_consistent"),
    ("bear.score", "bear_warning"),
    ("insider.score", "insider_buying"),
    ("ml.expected_7d", "ml_bullish"),
    ("crowding.score", "crowded"),
    ("price.momentum_5d", "momentum_up"),
    ("sentiment.pct", "sentiment_hot"),
]


# ── 取数 ─────────────────────────────────────────────────────────────────────
def load(db_path: str):
    """只读打开 pheromone.db，返回 (预测行, {(date,ticker): {signal: value}})。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
        need = {"dir_correct_t7", "dir_ambiguous_t7", "close_t7", "price_at_predict", "final_score"}
        if not need <= cols:
            return None, None
        preds = [dict(r) for r in con.execute(
            "SELECT ticker, date, direction, final_score, price_at_predict, close_t7, "
            "dir_correct_t7, dir_ambiguous_t7 FROM predictions ORDER BY date")]
        sig_map: dict = defaultdict(dict)
        for r in con.execute("SELECT date, ticker, signal, value FROM signal_archive WHERE value IS NOT NULL"):
            sig_map[(r["date"], r["ticker"])][r["signal"]] = r["value"]
    finally:
        con.close()
    return preds, sig_map


def labelled(p: dict) -> bool:
    return (p["direction"] in DIR_ZH and p["dir_correct_t7"] is not None
            and not (p["dir_ambiguous_t7"] or 0) and bool(p["close_t7"]) and bool(p["price_at_predict"]))


def raw_return_pct(p: dict) -> float:
    return (p["close_t7"] / p["price_at_predict"] - 1) * 100


# ── 无前视阈值 ────────────────────────────────────────────────────────────────
def _quantile(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(int(len(s) * q), len(s) - 1)]


def _before(series, cutoff):
    """series 为按日期排序的 (date, value)；取 date < cutoff 的值。"""
    return [v for _, v in series[:bisect.bisect_left(series, (cutoff,))]]


def _percentile_rank(sorted_vals, v):
    if not sorted_vals:
        return 50.0
    return round(bisect.bisect_left(sorted_vals, v) / len(sorted_vals) * 100, 1)


class Thresholds:
    """某个截止日之前可见的全部阈值。构造一次 series，按截止日反复切片。"""

    def __init__(self, preds, sig_map):
        self.score = sorted((p["date"], p["final_score"]) for p in preds if p["final_score"] is not None)
        self.keys = {k: sorted((d, s[k]) for (d, _t), s in sig_map.items() if s.get(k) is not None)
                     for k, _ in SWARM_KEYS}
        iv = defaultdict(list)
        for (d, t), s in sig_map.items():
            v = s.get("options.iv_current")
            if v is not None and v > 0:
                iv[t].append((d, v))
        self.iv = {t: sorted(v) for t, v in iv.items()}

    def at(self, cutoff: str) -> dict:
        sc = _before(self.score, cutoff)
        return {
            "score_hi": _quantile(sc, 0.75) or 6.5,
            "score_lo": _quantile(sc, 0.25) or 3.5,
            "swarm": {k: _quantile(_before(self.keys[k], cutoff), 0.75) for k, _ in SWARM_KEYS},
            "iv": {t: sorted(_before(s, cutoff)) for t, s in self.iv.items()},
        }


def compute_signals(p: dict, sigs: dict, thr: dict) -> dict:
    """与 pheromone_source._build_entry 的 19 个信号同义，阈值换成无前视版本。"""
    score = float(p["final_score"] or 0)
    pc = sigs.get("options.put_call_ratio")
    pc = float(pc) if pc not in (None, 0) else None
    call_pct = round(100.0 / (1.0 + pc), 1) if pc else None
    iv_cur = sigs.get("options.iv_current")
    iv_rank = (_percentile_rank(thr["iv"].get(p["ticker"], []), iv_cur)
               if iv_cur not in (None, 0) else None)
    agreement = sigs.get("composite.swarm_agreement")
    out = {
        "score_high": score >= thr["score_hi"],
        "score_low": score <= thr["score_lo"],
        "pc_bearish": bool(pc and pc >= 1.2),
        "pc_bullish": bool(pc and pc <= 0.7),
        "iv_elevated": bool(iv_rank is not None and iv_rank >= 60),
        "iv_suppressed": bool(iv_rank is not None and iv_rank <= 30),
        "call_dominant": bool(call_pct is not None and call_pct >= 65),
        "put_dominant": bool(call_pct is not None and call_pct <= 35),
        "resonance_active": bool(agreement is not None and agreement >= 0.75),
        "covered_call_pattern": bool(call_pct is not None and call_pct >= 65
                                     and iv_rank is not None and iv_rank >= 60),
        "call_otm_bias": False,
    }
    for key, name in SWARM_KEYS:
        v, t = sigs.get(key), thr["swarm"].get(key)
        out[name] = bool(v is not None and t is not None and v >= t)
    return out


# ── 模式库（与撤掉的实现同义，计数口径改为不同误判日）──────────────────────────
def primary_reason(direction_zh: str, chg: float, score: float, signals: dict) -> str:
    """weekly_analyzer.classify_misjudgments 的首条归因（pheromone 口径下 hedge_type 恒空）。"""
    if abs(chg) < NEUTRAL_BAND_T7:
        return f"中性区间（变动<{NEUTRAL_BAND_T7}%，方向不明确）"
    if "多" in direction_zh and chg < -NEUTRAL_BAND_T7:
        return "看多但大跌（可能被宏观/趋势压制）"
    if "空" in direction_zh and chg > NEUTRAL_BAND_T7:
        return "看空但大涨（可能有催化剂反弹）"
    if score >= 7.0:
        return "高评分(≥7.0)过度乐观"
    if signals.get("resonance_active"):
        return "共振信号触发但方向反转（price-in 风险）"
    if signals.get("call_dominant") and "多" in direction_zh:
        return "Call主导+看多但实际下跌（iv_suppressed环境，方向性误判）"
    return "未归类"


def pattern_key(direction_zh: str, reason: str, signals: dict) -> tuple[str, list[str]]:
    keys = sorted(k for k, v in signals.items() if v)[:3]
    return f"{direction_zh}|{reason[:20]}|{','.join(keys)}", keys


def build_library(preds, sig_map, thr: dict, lib_cutoff: str):
    """date ≤ lib_cutoff 的已判定方向单 → (模式库, 标的×方向 [误判数, 样本数])。"""
    lib: dict = defaultdict(dict)
    base: dict = defaultdict(lambda: [0, 0])
    for p in preds:
        if p["date"] > lib_cutoff or not labelled(p):
            continue
        dz = DIR_ZH[p["direction"]]
        miss = not p["dir_correct_t7"]
        b = base[(p["ticker"], dz)]
        b[0] += miss
        b[1] += 1
        if not miss:
            continue
        s = compute_signals(p, sig_map.get((p["date"], p["ticker"]), {}), thr)
        reason = primary_reason(dz, round(raw_return_pct(p), 2), round(float(p["final_score"] or 0), 2), s)
        key, sig_keys = pattern_key(dz, reason, s)
        rec = lib[p["ticker"]].setdefault(key, {"direction": dz, "signal_keys": set(sig_keys), "dates": set()})
        rec["dates"].add(p["date"])
    return lib, base


def is_warned(lib, ticker: str, direction_zh: str, signals: dict) -> bool:
    live = {k for k, v in signals.items() if v}
    for rec in lib.get(ticker, {}).values():
        if len(rec["dates"]) < ACTIVE_MIN_DISTINCT or rec["direction"] != direction_zh:
            continue
        if rec["signal_keys"] and len(rec["signal_keys"] & live) / len(rec["signal_keys"]) >= OVERLAP_MIN:
            return True
    return False


# ── 走查 ─────────────────────────────────────────────────────────────────────
def walk_forward(preds, sig_map, gap_days: int = 14, start: str = "2026-04-01") -> list[dict]:
    th = Thresholds(preds, sig_map)
    test = [p for p in preds if p["date"] >= start and labelled(p)]
    by_date = defaultdict(list)
    for p in test:
        by_date[p["date"]].append(p)
    rows = []
    for d in sorted(by_date):
        thr = th.at(d)
        lib_cut = (dt.date.fromisoformat(d) - dt.timedelta(days=gap_days)).isoformat()
        lib, base = build_library(preds, sig_map, thr, lib_cut)
        for p in by_date[d]:
            dz = DIR_ZH[p["direction"]]
            misses, n = base[(p["ticker"], dz)]
            s = compute_signals(p, sig_map.get((d, p["ticker"]), {}), thr)
            rows.append({
                "date": d, "ticker": p["ticker"],
                "warned": is_warned(lib, p["ticker"], dz, s),
                "miss": int(not p["dir_correct_t7"]),
                "prior": (misses + 1) / (n + 2), "prior_n": n,
                "ret": raw_return_pct(p) * (1 if p["direction"] == "bullish" else -1),
            })
    return rows


# ── 统计 ─────────────────────────────────────────────────────────────────────
def iso_week(d: str) -> str:
    y, w, _ = dt.date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def group_diff(rows, f, flag="warned"):
    a = [f(r) for r in rows if r[flag]]
    b = [f(r) for r in rows if not r[flag]]
    if not a or not b:
        return None
    return sum(a) / len(a) - sum(b) / len(b)


def week_bootstrap(rows, stat, n_boot: int, seed: int):
    """按 ISO 周整簇重抽；返回 (点估计, CI 下界, CI 上界)。"""
    by_week = defaultdict(list)
    for r in rows:
        by_week[iso_week(r["date"])].append(r)
    weeks = list(by_week)
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        sample = [r for w in (rng.choice(weeks) for _ in weeks) for r in by_week[w]]
        v = stat(sample)
        if v is not None:
            vals.append(v)
    if not vals:
        return stat(rows), None, None
    vals.sort()
    return stat(rows), vals[int(0.025 * len(vals))], vals[min(int(0.975 * len(vals)), len(vals) - 1)]


def gate(ci_lo, ci_hi) -> int:
    """预注册闸门 → 退出码（0 有增量预测力 / 1 显著反向 / 3 无法区分）。"""
    if ci_lo is None or ci_hi is None:
        return 3
    if ci_lo > 0:
        return 0
    if ci_hi < 0:
        return 1
    return 3


MIN_WEEKS, MIN_WARNED = 8, 20


def summarize(rows, n_boot: int = 5000, seed: int = 20260913) -> dict:
    weeks = {iso_week(r["date"]) for r in rows}
    warned = [r for r in rows if r["warned"]]
    res = {"n": len(rows), "weeks": len(weeks), "warned": len(warned),
           "warned_weeks": len({iso_week(r["date"]) for r in warned})}
    if len(weeks) < MIN_WEEKS or len(warned) < MIN_WARNED or len(warned) == len(rows):
        res["insufficient"] = True
        return res
    res["miss_warned"] = sum(r["miss"] for r in warned) / len(warned)
    res["miss_unwarned"] = sum(r["miss"] for r in rows if not r["warned"]) / (len(rows) - len(warned))
    res["d_miss"] = week_bootstrap(rows, lambda s: group_diff(s, lambda r: r["miss"]), n_boot, seed)
    res["d_resid"] = week_bootstrap(rows, lambda s: group_diff(s, lambda r: r["miss"] - r["prior"]), n_boot, seed)
    res["d_ret"] = week_bootstrap(rows, lambda s: group_diff(s, lambda r: r["ret"]), n_boot, seed)
    # 基线自身：历史误判率高于中位（且 ≥5 个历史样本）是否预测更多误判
    enough = [dict(r) for r in rows if r["prior_n"] >= 5]
    if len(enough) >= 2 * MIN_WARNED:
        med = sorted(r["prior"] for r in enough)[len(enough) // 2]
        for r in enough:
            r["high_prior"] = r["prior"] > med
        res["d_baseline"] = week_bootstrap(
            enough, lambda s: group_diff(s, lambda r: r["miss"], flag="high_prior"), n_boot, seed)
    lo, hi = res["d_miss"][1], res["d_miss"][2]
    if lo is not None and hi is not None and hi > lo:
        se = (hi - lo) / 3.92
        res["weeks_for_10pp"] = len(weeks) * (se / (0.10 / 2.80)) ** 2
    res["exit_code"] = gate(res["d_resid"][1], res["d_resid"][2])
    return res


def _pp(t):
    est, lo, hi = t
    return f"{100 * est:+.1f}pp [{100 * lo:+.1f}, {100 * hi:+.1f}]" if lo is not None else f"{100 * est:+.1f}pp [n/a]"


def render(res: dict, gap_days: int, start: str, db_path: str) -> str:
    lines = [
        f"# 误判模式预警 · 前向检验（{dt.date.today().isoformat()}）",
        "",
        f"- 数据：`{Path(db_path).name}`（只读）；方向单 {res['n']} 条 / {res['weeks']} 个 ISO 周；起点 {start}；gap={gap_days} 日历日",
        f"- 预警 {res['warned']} 条，分布在 {res['warned_weeks']} 个周",
        "",
    ]
    if res.get("insufficient"):
        lines += [f"**样本不足**（需 ≥{MIN_WEEKS} 周且预警 ≥{MIN_WARNED} 条）⇒ 退出码 3，无法判定。"]
        return "\n".join(lines) + "\n"
    lines += [
        "| 指标 | 点估计 [95% CI，ISO 周整簇 bootstrap] |",
        "|---|---|",
        f"| 误判率 预警 vs 未预警 | {100 * res['miss_warned']:.1f}% vs {100 * res['miss_unwarned']:.1f}% |",
        f"| Δ误判率 | {_pp(res['d_miss'])} |",
        f"| **Δresid（闸门）** | **{_pp(res['d_resid'])}** |",
        f"| Δ方向调整收益（辅助，不入闸） | {res['d_ret'][0]:+.2f}% [{res['d_ret'][1]:+.2f}, {res['d_ret'][2]:+.2f}] |",
    ]
    if "d_baseline" in res:
        lines.append(f"| 基线自身：历史误判率高于中位 | {_pp(res['d_baseline'])} |")
    lines.append("")
    verdict = {0: "**有增量预测力**（CI 下界 > 0）。可以考虑以「读时从 pheromone.db 计算」的形式接回，"
                  "**不要**再持久化进 thesis_breaks_config.json。",
               1: "**显著反向**：预警组反而更少误判。勿接回。",
               3: "**与零无法区分**。未证实，不是证伪；在功效达标前勿据此改动任何行为。"}[res["exit_code"]]
    lines.append(f"结论（由闸门生成）：{verdict}")
    if "weeks_for_10pp" in res:
        lines.append(f"\n功效粗估：测出 +10pp 真实差异（80% 功效、双侧 5%）约需 {res['weeks_for_10pp']:.0f} 个 ISO 周。")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="误判模式预警的无前视前向检验")
    ap.add_argument("--db", default=None, help="pheromone.db 路径（默认 hive_logger.PATHS.db）")
    ap.add_argument("--gap-days", type=int, default=14)
    ap.add_argument("--start", default="2026-04-01", help="测试集起点（之前为模式库预热期）")
    ap.add_argument("--bootstrap", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--report", default=None, help="把 markdown 报告写到该路径（不给则只打印）")
    args = ap.parse_args(argv)

    if args.db is None:
        from hive_logger import PATHS
        args.db = PATHS.db
    if not Path(args.db).exists():
        print(f"找不到数据库：{args.db}")
        return 3
    preds, sig_map = load(args.db)
    if preds is None:
        print("predictions 表缺 dir_correct_t7/close_t7 等列（先跑 backfill_dir_accuracy.py）")
        return 3
    rows = walk_forward(preds, sig_map, gap_days=args.gap_days, start=args.start)
    res = summarize(rows, n_boot=args.bootstrap, seed=args.seed)
    text = render(res, args.gap_days, args.start, args.db)
    print(text)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
        print(f"报告已写入 {args.report}")
    return 3 if res.get("insufficient") else res["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
