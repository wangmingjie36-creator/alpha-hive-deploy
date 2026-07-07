"""P2-1 (v0.38.0): 中性判定带宽离线回放实验 — 只出报告，不改生产代码

背景：
- 中性预测"判对"= |T+7| ≤ 3%（outcome_utils.DEFAULT_NEUTRAL_TOLERANCE_PCT 固定带宽），
  但 2026-06 单月 61% 样本 |ret|>3% → 中性在高波动政体下天然 ~40% 命中，
  拖垮 headline 准确率（中性占比 4月16% → 6月55%）。
- 且两条准确率路径口径不一致：
  * outcome_utils.determine_correctness：中性 ±3% 判对/判错
  * feedback_loop.calculate_agent_contribution：中性返回 None（不计入）

实验：全样本重放三种带宽口径下的中性命中率
  A. 固定 ±3%（现行）
  B. 固定 ±5%
  C. 波动率缩放：±0.674 × σ7(ticker)（该标的 T+7 收益的标准差 × 0.674
     = 正态假设下 50% 中央区间；σ7 从全样本估计，是回放性启发式）

运行：/usr/local/bin/python3 experiments/neutral_band_replay.py
输出：experiments/neutral_band_replay_report.md
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SNAP_DIR = ROOT / "report_snapshots"
OUT = Path(__file__).parent / "neutral_band_replay_report.md"


def t7_ret(s: dict):
    ap = s.get("actual_prices") or {}
    t7, ep = ap.get("t7"), s.get("entry_price")
    if not t7 or not ep:
        return None
    return (t7 / ep - 1) * 100


def load_rows():
    rows = []
    for f in sorted(SNAP_DIR.glob("*.json")):
        try:
            s = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        r = t7_ret(s)
        if r is None:
            continue
        rows.append({
            "ticker": s["ticker"], "date": s["date"],
            "dir": (s.get("direction") or "neutral").lower(),
            "score": float(s.get("composite_score") or 5.0),
            "ret": r,
        })
    return rows


def main():
    rows = load_rows()
    neutrals = [r for r in rows if r["dir"] == "neutral"]
    md = ["# 中性判定带宽回放实验（P2-1 / v0.38.0）", ""]
    md.append(f"全样本 {len(rows)} 条（T+7 已回填），其中中性 {len(neutrals)} 条"
              f"（{len(neutrals)/len(rows)*100:.0f}%）。")
    md.append("")

    # 各标的 σ7（T+7 收益标准差）
    by_tk = defaultdict(list)
    for r in rows:
        by_tk[r["ticker"]].append(r["ret"])
    sigma7 = {tk: (statistics.stdev(v) if len(v) >= 3 else 5.0) for tk, v in by_tk.items()}

    md.append("## 各标的 T+7 收益波动（σ7）")
    md.append("")
    md.append("| 标的 | n | σ7 | 0.674×σ7（C口径带宽） |")
    md.append("|------|---|-----|------|")
    for tk in sorted(sigma7):
        md.append(f"| {tk} | {len(by_tk[tk])} | {sigma7[tk]:.1f}% | ±{0.674*sigma7[tk]:.1f}% |")
    md.append("")

    # 三口径命中率（整体 + 按月）
    def hit(r, band):
        return abs(r["ret"]) <= band

    def run_scheme(label, band_fn):
        hits = [hit(r, band_fn(r)) for r in neutrals]
        return f"| {label} | {sum(hits)}/{len(hits)} | {sum(hits)/len(hits)*100:.0f}% |"

    md.append("## 中性命中率（三口径，全样本）")
    md.append("")
    md.append("| 口径 | 命中/总数 | 命中率 |")
    md.append("|------|----------|--------|")
    md.append(run_scheme("A. 固定 ±3%（现行）", lambda r: 3.0))
    md.append(run_scheme("B. 固定 ±5%", lambda r: 5.0))
    md.append(run_scheme("C. ±0.674×σ7(ticker)", lambda r: 0.674 * sigma7[r["ticker"]]))
    md.append("")

    md.append("## 按月分解")
    md.append("")
    md.append("| 月份 | 中性n | A ±3% | B ±5% | C 波动率缩放 | 月内中性占比 |")
    md.append("|------|-------|-------|-------|-------------|-------------|")
    by_month = defaultdict(list)
    month_all = defaultdict(int)
    for r in rows:
        month_all[r["date"][:7]] += 1
        if r["dir"] == "neutral":
            by_month[r["date"][:7]].append(r)
    for m in sorted(by_month):
        seg = by_month[m]
        a = sum(hit(r, 3.0) for r in seg) / len(seg) * 100
        b = sum(hit(r, 5.0) for r in seg) / len(seg) * 100
        c = sum(hit(r, 0.674 * sigma7[r["ticker"]]) for r in seg) / len(seg) * 100
        share = len(seg) / month_all[m] * 100
        md.append(f"| {m} | {len(seg)} | {a:.0f}% | {b:.0f}% | {c:.0f}% | {share:.0f}% |")
    md.append("")

    # 口径分裂审计
    md.append("## 两条准确率路径口径审计")
    md.append("")
    md.append("| 路径 | 中性处理 | 影响 |")
    md.append("|------|---------|------|")
    md.append("| `outcome_utils.determine_correctness` | ±3% 判对/判错 | backtester/"
              "outcomes_fetcher/日报 headline 用此口径——中性大量计入拖低 headline |")
    md.append("| `feedback_loop.calculate_agent_contribution` | 返回 None 不计入 | "
              "Agent 贡献度评估只看方向单——与 headline 口径不一致 |")
    md.append("")
    md.append("**统一建议**：headline 主口径已在 v0.37.0 切换为可执行方向单"
              "（`actionable` 块）；中性命中率单独展示，且建议采用下方推荐带宽。")
    md.append("")

    # 结论
    a_all = sum(hit(r, 3.0) for r in neutrals) / len(neutrals) * 100
    b_all = sum(hit(r, 5.0) for r in neutrals) / len(neutrals) * 100
    c_all = sum(hit(r, 0.674 * sigma7[r["ticker"]]) for r in neutrals) / len(neutrals) * 100
    md.append("## 结论")
    md.append("")
    md.append(f"- 固定 ±3% 全样本中性命中率 {a_all:.0f}%——对高波动标的（σ7>8% 的 "
              f"{sum(1 for v in sigma7.values() if v > 8)} 只）近乎抛硬币，"
              "「中性但 |ret|>3%」本质是波动，不是预测错误。")
    md.append(f"- ±5% 提升到 {b_all:.0f}%；波动率缩放口径 {c_all:.0f}%（各标的天然对齐自身波动）。")
    md.append("- 波动率缩放（C）在统计上最诚实：中性判定应回答「价格是否停在该标的正常噪音带内」。")
    md.append("- **建议**：`outcome_utils` 增加可选 `sigma7` 参数支持波动率缩放带宽；"
              "headline 继续用 actionable 口径（v37 已切）。是否上线由用户决定。")
    md.append("")

    OUT.write_text("\n".join(md), encoding="utf-8")
    print(f"报告已写入 {OUT}")
    print(f"A±3%: {a_all:.0f}% | B±5%: {b_all:.0f}% | C波动率缩放: {c_all:.0f}%")


if __name__ == "__main__":
    main()
