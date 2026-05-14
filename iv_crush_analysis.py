"""
iv_crush_analysis.py
=====================
NVDA 财报 IV Crush 历史分析（完全离线版）

方法：
  pre_IV    = 从 ATM 隐含涨跌幅反推年化 IV
              公式：implied_pct / (0.8 × √(DTE/365)) × 100
  post_HV30 = 财报后 30 日已实现 HV 估算
              公式：√[ (earnings_move² + 29 × daily_base_var) / 30 × 252 ] × 100
              base_var 用 NVDA 非财报期 HV≈45%/√252 推算
  crush_pp  = pre_IV − post_HV30
  crush_pct = crush_pp / pre_IV

运行（无需联网）：
  cd ~/Desktop/Alpha\ Hive
  python3 iv_crush_analysis.py
"""

import os, math, base64
from datetime import datetime
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUTPUT_HTML = "output/iv_crush_analysis.html"
OUTPUT_PNG  = "output/iv_crush_analysis.png"
os.makedirs("output", exist_ok=True)

# ── 当前环境数据（来自 5/13 快照 + 5/14 oi_wall.py 实测） ─────────────────────
CURRENT_IV      = 42.69   # 5/13 快照 iv_current
CURRENT_IV_RANK = 12.28   # 5/13 快照 iv_rank
CURRENT_PRICE   = 235.08  # 5/14 实测（oi_wall.py）

# NVDA 非财报期基准 HV（45% 年化，即每日 2.83% 波动）
BASE_HV_ANNUAL  = 45.0
BASE_DAILY_VAR  = (BASE_HV_ANNUAL / 100 / math.sqrt(252)) ** 2

# ── 财报历史数据 ────────────────────────────────────────────────────────────────
# implied_move: 财报当周 ATM straddle 隐含涨跌幅（%）
# actual_move : 财报日收盘 vs 前日收盘涨跌（%）
# dte         : 用于 straddle 的期权到期日（天）
EARNINGS = [
    # date          implied  actual    dte   label
    ("2026-02-25",    8.0,   +8.8,    14,  "Q4 FY26  2/25/26"),
    ("2025-11-19",    8.5,   +4.9,    14,  "Q3 FY26 11/19/25"),
    ("2025-08-27",    9.5,   +6.7,    14,  "Q2 FY26  8/27/25"),
    ("2025-05-28",    8.8,  +16.1,    14,  "Q1 FY26  5/28/25"),
    ("2025-02-26",    8.2,   -8.5,    14,  "Q4 FY25  2/26/25"),
    ("2024-11-20",    9.0,   +2.0,    14,  "Q3 FY25 11/20/24"),
    ("2024-08-28",   10.5,   +9.3,    14,  "Q2 FY25  8/28/24"),
    ("2024-05-22",   10.0,  +21.8,    14,  "Q1 FY25  5/22/24"),
]

UPCOMING = ("2026-05-28", None, None, 14, "Q1 FY27  5/20/26 ▶UPCOMING")

# ── 核心计算函数 ────────────────────────────────────────────────────────────────
def calc_pre_iv(implied_pct: float, dte: int) -> float:
    """隐含涨跌幅 → 年化 IV（straddle 近似）"""
    return (implied_pct / 100.0) / (0.8 * math.sqrt(dte / 365.0)) * 100.0

def calc_post_hv30(actual_pct: float) -> float:
    """实际涨跌幅 + 基准 HV → 财报后 30日 HV 估算"""
    earnings_var = (actual_pct / 100.0) ** 2
    avg_var = (earnings_var + 29.0 * BASE_DAILY_VAR) / 30.0
    return math.sqrt(avg_var * 252) * 100.0

# ── 逐期计算 ────────────────────────────────────────────────────────────────────
records = []
for date, implied, actual, dte, label in EARNINGS:
    pre_iv    = calc_pre_iv(implied, dte)
    post_hv30 = calc_post_hv30(actual)
    crush_pp  = pre_iv - post_hv30
    crush_pct = crush_pp / pre_iv * 100.0
    records.append({
        "date": date, "label": label,
        "implied": implied, "actual": actual,
        "pre_iv": pre_iv, "post_hv30": post_hv30,
        "crush_pp": crush_pp, "crush_pct": crush_pct,
    })

# ── 统计摘要 ────────────────────────────────────────────────────────────────────
avg_pre_iv    = np.mean([r["pre_iv"]    for r in records])
avg_post_hv   = np.mean([r["post_hv30"] for r in records])
avg_crush_pp  = np.mean([r["crush_pp"]  for r in records])
avg_crush_pct = np.mean([r["crush_pct"] for r in records])
seller_wins   = sum(1 for r in records if abs(r["actual"]) <= r["implied"])

expected_post_iv  = CURRENT_IV - avg_crush_pp
expected_post_hv  = calc_post_hv30(10.0)   # 假设财报当天 ±10%（中位场景）

print("── NVDA 财报 IV Crush 历史分析 ─────────────────────────────────────────")
print(f"{'财报日':<12} {'隐含涨跌':>8} {'实际涨跌':>8} {'vs预期':>7} {'pre_IV':>8} {'post_HV30':>10} {'Crush(pp)':>10} {'Crush%':>8}")
print("─" * 80)
for r in records:
    beat = "✓未超" if abs(r["actual"]) <= r["implied"] else "✗超出"
    print(f"{r['date']:<12} {r['implied']:>7.1f}%  {r['actual']:>+7.1f}%  {beat:>5}  "
          f"{r['pre_iv']:>7.1f}%  {r['post_hv30']:>9.1f}%  "
          f"-{r['crush_pp']:>8.1f}pp  {r['crush_pct']:>6.1f}%")

print(f"\n── 均值（n={len(records)}）──────────────────────────────────────────────")
print(f"  pre_IV:    {avg_pre_iv:.1f}%")
print(f"  post_HV30: {avg_post_hv:.1f}%")
print(f"  IV Crush:  -{avg_crush_pp:.1f}pp  （{avg_crush_pct:.0f}% 压缩）")
print(f"  卖方胜率:  {seller_wins}/{len(records)} = {seller_wins/len(records)*100:.0f}%")
print(f"\n── 5/20/26 财报前预测 ──────────────────────────────────────────────────")
print(f"  当前 IV:         {CURRENT_IV:.1f}%  (IV Rank {CURRENT_IV_RANK:.0f})")
print(f"  历史平均 Crush:  -{avg_crush_pp:.1f}pp")
print(f"  财报后预计 IV:   ~{expected_post_iv:.1f}%")
print(f"  ⚠ 当前 IV 已处历史低位，Crush 绝对幅度有限，但比例仍约 {avg_crush_pct:.0f}%")

# ── 绘图 ────────────────────────────────────────────────────────────────────────
print("\n▶ 绘图...")

PURPLE  = "#534AB7"
GREEN   = "#639922"
DKGREEN = "#3B6D11"
RED     = "#E24B4A"
AMBER   = "#BA7517"
BG      = "#F2F1ED"
CARD    = "#fafaf8"

fig = plt.figure(figsize=(15, 12), facecolor=BG)
gs  = fig.add_gridspec(3, 2, hspace=0.48, wspace=0.32,
                       left=0.07, right=0.97, top=0.93, bottom=0.06)
ax1 = fig.add_subplot(gs[0, :])
ax2 = fig.add_subplot(gs[1, 0])
ax3 = fig.add_subplot(gs[1, 1])
ax4 = fig.add_subplot(gs[2, :])

fig.suptitle(
    f"NVDA 财报 IV Crush 历史分析  |  {len(records)} 个历史财报期  |  {datetime.today().strftime('%Y-%m-%d')}",
    fontsize=13, color="#1a1a1a", y=0.97
)

for ax in [ax1, ax2, ax3, ax4]:
    ax.set_facecolor(CARD)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#eee", linewidth=0.5)

labels = [r["date"][2:7] for r in records]
x = np.arange(len(records))
w = 0.36

# ── 图1：pre_IV vs post_HV30 ──────────────────────────────────────────────────
pre_ivs  = [r["pre_iv"]    for r in records]
post_hvs = [r["post_hv30"] for r in records]

ax1.bar(x - w/2, pre_ivs,  width=w, color=PURPLE, alpha=0.85, label="Pre-earnings ATM IV（隐含涨跌幅反推）")
ax1.bar(x + w/2, post_hvs, width=w, color=GREEN,  alpha=0.85, label="Post-earnings HV30（实际涨跌推算）")

# 当前 IV 水位线
ax1.axhline(CURRENT_IV, color=PURPLE, linewidth=1.5, linestyle=":", alpha=0.7)
ax1.text(len(records) - 0.5, CURRENT_IV + 1.5,
         f"5/20/26 当前 IV={CURRENT_IV:.1f}%\n(IV Rank={CURRENT_IV_RANK:.0f}, 历史最低分位)",
         fontsize=8, color=PURPLE, ha="right")

for i, (pre, post) in enumerate(zip(pre_ivs, post_hvs)):
    ax1.text(i - w/2, pre + 0.8, f"{pre:.0f}%",  ha="center", fontsize=8,
             color=PURPLE, fontweight="600")
    ax1.text(i + w/2, post + 0.8, f"{post:.0f}%", ha="center", fontsize=8,
             color=DKGREEN)

ax1.set_xticks(x); ax1.set_xticklabels(labels, fontsize=9)
ax1.set_ylabel("波动率 (%)", fontsize=10, color="#555")
ax1.set_title("财报前 ATM IV（紫色） vs 财报后 HV30（绿色）", fontsize=11, color="#555", pad=8)
ax1.legend(fontsize=9, loc="upper right")
ax1.set_ylim(0, max(pre_ivs) * 1.28)

# ── 图2：IV Crush pp ──────────────────────────────────────────────────────────
crush_pps = [r["crush_pp"] for r in records]
colors2 = [RED if c > avg_crush_pp else AMBER for c in crush_pps]
bars2 = ax2.bar(x, crush_pps, color=colors2, alpha=0.85)
ax2.axhline(avg_crush_pp, color="#222", linewidth=1.5, linestyle="--",
            label=f"均值 -{avg_crush_pp:.1f}pp")
for i, c in enumerate(crush_pps):
    ax2.text(i, c + 0.4, f"-{c:.0f}", ha="center", fontsize=9, fontweight="700",
             color=RED if c > avg_crush_pp else AMBER)
ax2.set_xticks(x); ax2.set_xticklabels(labels, fontsize=9)
ax2.set_ylabel("IV Crush（百分点 pp）", fontsize=10, color="#555")
ax2.set_title("IV Crush 幅度（pp）", fontsize=11, color="#555", pad=8)
ax2.legend(fontsize=9)

# ── 图3：IV Crush % ───────────────────────────────────────────────────────────
crush_pcts = [r["crush_pct"] for r in records]
colors3 = [RED if c > avg_crush_pct else AMBER for c in crush_pcts]
ax3.bar(x, crush_pcts, color=colors3, alpha=0.85)
ax3.axhline(avg_crush_pct, color="#222", linewidth=1.5, linestyle="--",
            label=f"均值 {avg_crush_pct:.0f}%")
for i, c in enumerate(crush_pcts):
    ax3.text(i, c + 0.5, f"{c:.0f}%", ha="center", fontsize=9, fontweight="700",
             color=RED if c > avg_crush_pct else AMBER)
ax3.set_xticks(x); ax3.set_xticklabels(labels, fontsize=9)
ax3.set_ylabel("IV 压缩比例 (%)", fontsize=10, color="#555")
ax3.set_title("IV Crush 比例（% 压缩）", fontsize=11, color="#555", pad=8)
ax3.legend(fontsize=9)

# ── 图4：实际涨跌 vs 隐含范围（时间轴）──────────────────────────────────────
actuals  = [r["actual"]  for r in records]
implieds = [r["implied"] for r in records]

for i, (act, imp) in enumerate(zip(actuals, implieds)):
    # 隐含范围灰色填充带
    ax4.fill_between([i - 0.4, i + 0.4], [-imp, -imp], [imp, imp],
                     color="#e0ddf0", alpha=0.55, zorder=1)
    within = abs(act) <= imp
    c = DKGREEN if within else RED
    mk = "o" if within else "^"
    # 涨跌线
    ax4.plot([i, i], [0, act], color=c, linewidth=2.5, zorder=2)
    ax4.scatter(i, act, color=c, s=90, zorder=3, marker=mk)
    # 涨跌标注
    offset = 1.3 if act >= 0 else -2.4
    ax4.text(i, act + offset, f"{act:+.1f}%", ha="center", fontsize=8.5,
             color=c, fontweight="600")
    # 隐含范围标注（仅正侧）
    ax4.text(i, imp + 0.6, f"±{imp:.1f}%", ha="center", fontsize=7.5,
             color="#aaa")

ax4.axhline(0, color="#999", linewidth=0.8)
ax4.set_xticks(x)
ax4.set_xticklabels([r["date"][2:7] for r in records], fontsize=9)
ax4.set_ylabel("涨跌幅 (%)", fontsize=10, color="#555")
ax4.set_title(
    "实际涨跌（点）vs 隐含涨跌范围（灰带）· 绿圈=未超预期  红三角=超预期",
    fontsize=10.5, color="#555", pad=8
)
ax4.text(0.01, 0.95,
         f"卖方胜率 {seller_wins}/{len(records)} = {seller_wins/len(records)*100:.0f}%（实际<隐含）",
         transform=ax4.transAxes, fontsize=9.5, color=PURPLE, fontweight="600",
         verticalalignment="top")

plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight")
plt.close()
print(f"✅ PNG 已保存: {os.path.abspath(OUTPUT_PNG)}")

# ── 生成 HTML ────────────────────────────────────────────────────────────────────
with open(OUTPUT_PNG, "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

table_rows = ""
for r in records:
    act_color = DKGREEN if r["actual"] > 0 else RED
    beat = "✓ 未超" if abs(r["actual"]) <= r["implied"] else "✗ 超出"
    beat_color = DKGREEN if "✓" in beat else RED
    table_rows += f"""<tr>
      <td style="color:#666;font-size:12px">{r['date']}</td>
      <td style="text-align:right">{r['implied']:.1f}%</td>
      <td style="text-align:right;color:{act_color};font-weight:500">{r['actual']:+.1f}%</td>
      <td style="text-align:right;color:{beat_color};font-size:12px">{beat}</td>
      <td style="text-align:right;color:#534AB7;font-weight:600">{r['pre_iv']:.0f}%</td>
      <td style="text-align:right;color:#3B6D11">{r['post_hv30']:.0f}%</td>
      <td style="text-align:right;color:#E24B4A;font-weight:600">-{r['crush_pp']:.0f}pp</td>
      <td style="text-align:right;color:#BA7517">{r['crush_pct']:.0f}%</td>
    </tr>"""

html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<title>NVDA IV Crush 历史分析</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}}
body{{background:#f2f1ed;color:#1a1a1a;padding:24px 20px;max-width:1100px;margin:0 auto;}}
.card{{background:#fff;border:1px solid rgba(0,0,0,0.08);border-radius:12px;padding:20px 24px;margin-bottom:16px;}}
h1{{font-size:17px;font-weight:600;margin-bottom:4px;}}
.sub{{font-size:11px;color:#999;margin-top:3px;line-height:1.6;}}
.grid5{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px;}}
.grid4{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;}}
.m{{background:#f8f7f3;border-radius:8px;padding:12px 14px;}}
.ml{{font-size:11px;color:#999;margin-bottom:4px;}}
.mv{{font-size:21px;font-weight:700;}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th{{text-align:left;padding:7px 10px;color:#aaa;font-weight:500;font-size:11px;border-bottom:1px solid #eee;}}
th.r{{text-align:right;}}
td{{padding:7px 10px;border-bottom:.5px solid #f5f5f5;}}
img{{width:100%;border-radius:8px;}}
.insight{{background:#fffbf0;border-left:3px solid #BA7517;padding:14px 18px;border-radius:0 8px 8px 0;font-size:13px;line-height:1.85;margin-top:14px;}}
.tag{{display:inline-block;padding:2px 9px;border-radius:4px;font-size:11px;font-weight:600;margin-right:5px;}}
.purple-card{{background:#f0eef8;border-color:#c5bfe8;}}
</style></head><body>

<div class="card">
  <h1>NVDA 财报 IV Crush 历史分析</h1>
  <p class="sub">
    分析最近 {len(records)} 个财报期 (2024-05 → 2026-02) &nbsp;·&nbsp;
    Pre-IV: ATM 隐含涨跌幅反推 &nbsp;·&nbsp;
    Post-HV30: 实际涨跌幅 + NVDA 基准 HV45% 合成估算 &nbsp;·&nbsp;
    生成时间: {datetime.today().strftime('%Y-%m-%d %H:%M')}
  </p>
</div>

<div class="grid5" style="margin-bottom:16px;">
  <div class="m"><div class="ml">平均 Pre-IV（财报前）</div>
    <div class="mv" style="color:#534AB7;">{avg_pre_iv:.0f}%</div></div>
  <div class="m"><div class="ml">平均 Post-HV30（财报后）</div>
    <div class="mv" style="color:#3B6D11;">{avg_post_hv:.0f}%</div></div>
  <div class="m"><div class="ml">平均 IV Crush（pp）</div>
    <div class="mv" style="color:#E24B4A;">-{avg_crush_pp:.0f}pp</div></div>
  <div class="m"><div class="ml">平均 Crush 压缩比</div>
    <div class="mv" style="color:#BA7517;">{avg_crush_pct:.0f}%</div></div>
  <div class="m"><div class="ml">卖方胜率（8期）</div>
    <div class="mv" style="color:#639922;">{seller_wins}/{len(records)} = {seller_wins/len(records)*100:.0f}%</div></div>
</div>

<div class="card purple-card">
  <p style="font-size:11px;color:#7b72c9;text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px;font-weight:600;">
    ▶ 5/20/26 Q1 FY27 财报前 — IV Crush 预测
  </p>
  <div class="grid4">
    <div><div style="font-size:11px;color:#999;">当前 IV（5/13 快照）</div>
      <div style="font-size:24px;font-weight:700;color:#534AB7;">{CURRENT_IV:.1f}%</div></div>
    <div><div style="font-size:11px;color:#999;">IV Rank（历史极低）</div>
      <div style="font-size:24px;font-weight:700;color:#BA7517;">{CURRENT_IV_RANK:.0f}</div></div>
    <div><div style="font-size:11px;color:#999;">历史平均 Crush</div>
      <div style="font-size:24px;font-weight:700;color:#E24B4A;">-{avg_crush_pp:.0f}pp</div></div>
    <div><div style="font-size:11px;color:#999;">财报后预计 IV</div>
      <div style="font-size:24px;font-weight:700;color:#3B6D11;">~{expected_post_iv:.0f}%</div></div>
  </div>
  <div class="insight">
    <strong>核心结论：</strong>历史上 NVDA 财报后 IV 从平均 <strong>{avg_pre_iv:.0f}%</strong> 压缩至 <strong>{avg_post_hv:.0f}%</strong>，
    压缩约 <strong>{avg_crush_pct:.0f}%</strong>。但本次 <em>起点极低</em>（IV Rank={CURRENT_IV_RANK:.0f}），
    Crush 的绝对损失更小（历史 Crush 平均 -{avg_crush_pp:.0f}pp，当前 IV {CURRENT_IV}% 财报后预计仅降至约 {expected_post_iv:.0f}%）。<br><br>
    <strong>策略含义：</strong><br>
    <span class="tag" style="background:#e8f5e0;color:#3B6D11;">看多方向</span>
    买 <strong>当周/次周 ATM Call</strong> — Vega 损失有限，Delta 收益为主。勿买深 OTM（IV Crush 后权利金归零快）。<br>
    <span class="tag" style="background:#fff0e0;color:#BA7517;">不确定方向</span>
    <strong>卖 Strangle / Iron Condor</strong> — 收 IV Crush，但需能承受 ±{max([r['implied'] for r in records if r['implied'] <= 10], default=10):.0f}% 以内的波动。<br>
    <span class="tag" style="background:#ffeaea;color:#E24B4A;">风险警示</span>
    卖方历史胜率 {seller_wins}/{len(records)} 但历史上有 {len(records)-seller_wins} 次超预期涨跌（如 +21.8% / +16.1%）。本次 IV 低 → 隐含涨跌幅窄 → 超出概率提升。
  </div>
</div>

<div class="card"><img src="data:image/png;base64,{img_b64}" alt="NVDA IV Crush 历史分析图表"></div>

<div class="card">
  <p style="font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.04em;margin-bottom:14px;">逐期明细</p>
  <table>
    <thead><tr>
      <th>财报日</th><th class="r">隐含涨跌</th><th class="r">实际涨跌</th><th class="r">vs 预期</th>
      <th class="r">Pre IV</th><th class="r">Post HV30*</th><th class="r">Crush(pp)</th><th class="r">Crush(%)</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
  <p style="font-size:11px;color:#bbb;margin-top:12px;">
    * Post HV30 = 基于实际涨跌幅 + NVDA 非财报期基准 HV45% 合成估算，非真实历史 IV 快照。
    真实数据需 Barchart Premier / OptionMetrics 等付费订阅。
  </p>
</div>

</body></html>"""

with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
    f.write(html)

print(f"✅ HTML 报告已保存: {os.path.abspath(OUTPUT_HTML)}")
print(f"\n── 关键洞察 ────────────────────────────────────────────────────────────")
print(f"  当前 IV {CURRENT_IV}% 处于历史 {CURRENT_IV_RANK:.0f} 分位（极低）")
print(f"  历史 IV Crush 平均 -{avg_crush_pp:.0f}pp（{avg_crush_pct:.0f}% 压缩）")
print(f"  财报后预计 IV ≈ {expected_post_iv:.0f}%")
print(f"  → 买期权成本低，Crush 损失也相对小")
print(f"  → 但隐含涨跌幅窄（约±{sum(r['implied'] for r in records)/len(records):.1f}% 均值），超出概率↑")
