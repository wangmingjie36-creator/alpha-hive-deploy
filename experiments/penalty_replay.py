"""P2-2 (v0.38.0): 罚分叠加压缩离线回放实验 — 只出报告，不改生产代码

背景（评分链路审计发现）：
queen_distiller 对 rule_score 顺序施加多层罚分——DQ 压缩（向 5.0 收缩）→
Guard 罚分 → Bear cap → combo cap（总罚分 >2.0 时硬顶）→ 冲突折扣 →
准确率折扣。审计推断顺序叠加把高分逐层压回 5.x，评分聚堆在 5.0-5.8
区间，final_score 失去区分度（近30天 composite vs T+7 的 rho ≈ -0.05）。

实验：用 .swarm_results_*.json 存的中间值（pre_penalty_score / score_after_*
/ conflict_info.conflict_discount）离线重放两种合成：
  A. 现行叠加式：final = 顺序压缩后的分（即历史 final_score 轨迹）
  B. 统一风险面：unified = clamp(pre_penalty_score - min(2.0, 各罚分之和), 2.0, 10.0)
     （罚分只加总一次、单次扣减、上限 2.0——不再层层压缩）

对比两种口径：
  - final_score 与 T+7 收益的 Spearman rho（区分度）
  - score≥6 方向单（看多）+ 全部看空的命中率 / 均 PnL（actionable 口径）
  - 分数分布（是否解除 5.0-5.8 聚堆）

运行：/usr/local/bin/python3 experiments/penalty_replay.py
输出：experiments/penalty_replay_report.md
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SNAP_DIR = ROOT / "report_snapshots"
OUT = Path(__file__).parent / "penalty_replay_report.md"


def t7_map():
    """(ticker, date) -> T+7 收益%"""
    out = {}
    for f in SNAP_DIR.glob("*.json"):
        try:
            s = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ap = s.get("actual_prices") or {}
        t7, ep = ap.get("t7"), s.get("entry_price")
        if t7 and ep:
            out[(s["ticker"], s["date"])] = (t7 / ep - 1) * 100
    return out


def load_swarm_rows():
    rets = t7_map()
    rows = []
    for f in sorted(ROOT.glob(".swarm_results_*.json")):
        date = f.name.replace(".swarm_results_", "").replace(".json", "")
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for tk, r in d.items():
            if not isinstance(r, dict):
                continue
            pre = r.get("pre_penalty_score")
            fin = r.get("final_score")
            if pre is None or fin is None:
                continue
            ret = rets.get((tk, date))
            if ret is None:
                continue
            after_dq = r.get("score_after_dq", pre)
            after_guard = r.get("score_after_guard", after_dq)
            after_bear = r.get("score_after_bear", after_guard)
            conflict = (r.get("conflict_info") or {}).get("conflict_discount", 0.0) or 0.0
            rows.append({
                "ticker": tk, "date": date, "ret": ret,
                "dir": (r.get("direction") or "neutral").lower(),
                "pre": float(pre),
                "dq_pen": max(0.0, float(pre) - float(after_dq)),
                "guard_pen": max(0.0, float(after_dq) - float(after_guard)),
                "bear_pen": max(0.0, float(after_guard) - float(after_bear)),
                "conflict_pen": float(conflict),
                "final_current": float(fin),
            })
    return rows


def spearman(xs, ys):
    def rank(v):
        s = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for i, idx in enumerate(s):
            r[idx] = i
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def actionable_stats(rows, score_key):
    """可执行方向单口径：看多 score≥6 + 全部看空"""
    seg = [r for r in rows
           if (r["dir"] == "bullish" and r[score_key] >= 6.0) or r["dir"] == "bearish"]
    if not seg:
        return {"n": 0}
    hits = sum((r["ret"] > 0) if r["dir"] == "bullish" else (r["ret"] < 0) for r in seg)
    pnl = [r["ret"] if r["dir"] == "bullish" else -r["ret"] for r in seg]
    return {"n": len(seg), "acc": hits / len(seg) * 100, "pnl": sum(pnl) / len(pnl)}


def hist(rows, key):
    c = Counter()
    for r in rows:
        b = min(9.5, max(2.0, round(r[key] * 2) / 2))  # 0.5 分桶
        c[b] += 1
    return c


def main():
    rows = load_swarm_rows()
    if len(rows) < 30:
        print(f"样本不足（{len(rows)}），中止")
        return

    # B 口径：统一风险面
    for r in rows:
        total_pen = min(2.0, r["dq_pen"] + r["guard_pen"] + r["bear_pen"] + r["conflict_pen"])
        r["final_unified"] = max(2.0, min(10.0, r["pre"] - total_pen))

    md = ["# 罚分叠加压缩回放实验（P2-2 / v0.38.0）", ""]
    md.append(f"样本：{len(rows)} 条（.swarm_results 历史 × T+7 已回填快照 join）。")
    md.append("")

    # 区分度
    rets = [r["ret"] for r in rows]
    rho_a = spearman([r["final_current"] for r in rows], rets)
    rho_b = spearman([r["final_unified"] for r in rows], rets)
    rho_pre = spearman([r["pre"] for r in rows], rets)
    md.append("## 区分度（Spearman rho，score vs T+7 收益）")
    md.append("")
    md.append("| 口径 | rho |")
    md.append("|------|-----|")
    md.append(f"| 罚分前原始分（pre_penalty_score） | {rho_pre:+.3f} |")
    md.append(f"| A. 现行叠加式（final_score） | {rho_a:+.3f} |")
    md.append(f"| B. 统一风险面 | {rho_b:+.3f} |")
    md.append("")

    # actionable
    sa = actionable_stats(rows, "final_current")
    sb = actionable_stats(rows, "final_unified")
    md.append("## 可执行方向单口径（看多 score≥6 + 全部看空）")
    md.append("")
    md.append("| 口径 | 方向单数 | 命中率 | 均 PnL |")
    md.append("|------|---------|--------|--------|")
    md.append(f"| A. 现行 | {sa['n']} | {sa.get('acc', 0):.0f}% | {sa.get('pnl', 0):+.2f}% |")
    md.append(f"| B. 统一风险面 | {sb['n']} | {sb.get('acc', 0):.0f}% | {sb.get('pnl', 0):+.2f}% |")
    md.append("")

    # 分布
    md.append("## 分数分布（0.5 分桶）")
    md.append("")
    ha, hb = hist(rows, "final_current"), hist(rows, "final_unified")
    buckets = sorted(set(ha) | set(hb))
    md.append("| 分桶 | A 现行 | B 统一 |")
    md.append("|------|--------|--------|")
    for b in buckets:
        md.append(f"| {b:.1f} | {ha.get(b, 0)} | {hb.get(b, 0)} |")
    mid_a = sum(v for k, v in ha.items() if 5.0 <= k <= 6.0) / len(rows) * 100
    mid_b = sum(v for k, v in hb.items() if 5.0 <= k <= 6.0) / len(rows) * 100
    md.append("")
    md.append(f"5.0-6.0 聚堆占比：A 现行 {mid_a:.0f}% vs B 统一 {mid_b:.0f}%。")
    md.append("")

    # 罚分构成
    md.append("## 罚分构成（均值）")
    md.append("")
    md.append("| 罚分层 | 均值 | 非零占比 |")
    md.append("|--------|------|---------|")
    for k, label in [("dq_pen", "DQ 压缩"), ("guard_pen", "Guard 罚分"),
                     ("bear_pen", "Bear cap"), ("conflict_pen", "冲突折扣")]:
        vals = [r[k] for r in rows]
        nz = sum(1 for v in vals if v > 0.01) / len(rows) * 100
        md.append(f"| {label} | {statistics.mean(vals):.3f} | {nz:.0f}% |")
    md.append("")

    # 结论
    md.append("## 结论")
    md.append("")
    verdict = []
    if rho_b > rho_a + 0.03 and sb.get("acc", 0) >= sa.get("acc", 0):
        verdict.append("统一风险面在区分度与方向单质量上均不劣于现行——**支持进一步验证后上线**。")
    elif abs(rho_b - rho_a) <= 0.03:
        verdict.append("两种口径区分度差异 <0.03——罚分叠加**不是**区分度弱的主因，"
                       "证据不足，**不建议改动**（真正瓶颈更可能在上游各蜂原始分本身，"
                       "见 pre_penalty_score 的 rho）。")
    else:
        verdict.append("现行叠加式反而更好——**证伪统一风险面假设，勿改**。")
    md.extend([f"- {v}" for v in verdict])
    md.append(f"- 罚分前原始分 rho={rho_pre:+.3f}：这是评分体系区分度的上限——"
              "若接近 0，说明改罚分结构治标不治本。")
    md.append("- 本实验为离线回放，改不改生产等用户看完数据决定。")
    md.append("")

    OUT.write_text("\n".join(md), encoding="utf-8")
    print(f"报告已写入 {OUT}")
    print(f"rho: pre={rho_pre:+.3f} A现行={rho_a:+.3f} B统一={rho_b:+.3f}")
    print(f"actionable: A n={sa['n']} acc={sa.get('acc', 0):.0f}% | B n={sb['n']} acc={sb.get('acc', 0):.0f}%")


if __name__ == "__main__":
    main()
