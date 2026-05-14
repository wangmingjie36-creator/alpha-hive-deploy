"""
oi_wall.py
==========
NVDA 完整 OI 墙（Max Pain 图）

运行：
  cd ~/Desktop/Alpha\ Hive
  python3 oi_wall.py

依赖：yfinance pandas matplotlib（已安装）
网络：需要连接互联网
"""

import warnings, os, json
from datetime import datetime
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

warnings.filterwarnings("ignore")

TICKER   = "NVDA"
OUTPUT   = "output/oi_wall.html"
PNG_OUT  = "output/oi_wall.png"
os.makedirs("output", exist_ok=True)

print(f"▶ 下载 {TICKER} 完整期权链...")
nvda = yf.Ticker(TICKER)

try:
    current_price = nvda.fast_info["lastPrice"]
except:
    hist = nvda.history(period="1d")
    current_price = float(hist["Close"].iloc[-1]) if not hist.empty else 226.0

print(f"  当前价格: ${current_price:.2f}")

expirations = nvda.options
print(f"  可用到期日: {len(expirations)} 个")
for e in expirations[:8]:
    print(f"    {e}")
if len(expirations) > 8:
    print(f"    ... 及 {len(expirations)-8} 个更远到期日")

# 只取近期4个到期日（财报相关）做详细 OI 墙
# 全部到期日做汇总
NEAR_EXPS = expirations[:4]   # 详细图
ALL_EXPS  = expirations[:12]  # 总计

# ─── 采集全链数据 ─────────────────────────────────────────────────────────────
print("\n▶ 采集各到期日期权链...")
all_calls = []
all_puts  = []

for exp in ALL_EXPS:
    try:
        chain = nvda.option_chain(exp)
        c = chain.calls[["strike", "openInterest", "impliedVolatility"]].copy()
        p = chain.puts[["strike", "openInterest", "impliedVolatility"]].copy()
        c["expiry"] = exp
        p["expiry"] = exp
        all_calls.append(c)
        all_puts.append(p)
        total_oi = c["openInterest"].sum() + p["openInterest"].sum()
        print(f"  {exp}: C={c['openInterest'].sum():>7,.0f}  P={p['openInterest'].sum():>7,.0f}  合计={total_oi:>8,.0f}")
    except Exception as e:
        print(f"  {exp}: 失败 ({e})")

calls_df = pd.concat(all_calls, ignore_index=True) if all_calls else pd.DataFrame()
puts_df  = pd.concat(all_puts,  ignore_index=True) if all_puts  else pd.DataFrame()

if calls_df.empty:
    print("❌ 无法获取期权链数据，请检查网络连接")
    exit(1)

# ─── 聚合：所有到期日合计 OI by Strike ─────────────────────────────────────────
call_by_strike = calls_df.groupby("strike")["openInterest"].sum().reset_index()
put_by_strike  = puts_df.groupby("strike")["openInterest"].sum().reset_index()

# 只取当前价格 ±40% 范围内的行权价
lo = current_price * 0.60
hi = current_price * 1.45
call_by_strike = call_by_strike[(call_by_strike["strike"] >= lo) & (call_by_strike["strike"] <= hi)]
put_by_strike  = put_by_strike[(put_by_strike["strike"] >= lo) & (put_by_strike["strike"] <= hi)]

all_strikes = sorted(set(call_by_strike["strike"].tolist()) | set(put_by_strike["strike"].tolist()))
call_oi = {row["strike"]: row["openInterest"] for _, row in call_by_strike.iterrows()}
put_oi  = {row["strike"]: row["openInterest"] for _, row in put_by_strike.iterrows()}

strikes  = np.array(all_strikes)
c_oi_arr = np.array([call_oi.get(s, 0) for s in strikes])
p_oi_arr = np.array([put_oi.get(s, 0)  for s in strikes])

# ─── Max Pain 计算 ─────────────────────────────────────────────────────────────
def calc_max_pain(strikes, call_oi, put_oi):
    pain = []
    for exp_price in strikes:
        # 所有call到期归零的损失
        call_loss = sum(max(0, exp_price - s) * oi for s, oi in zip(strikes, call_oi))
        put_loss  = sum(max(0, s - exp_price) * oi for s, oi in zip(strikes, put_oi))
        pain.append(call_loss + put_loss)
    return strikes[np.argmin(pain)], min(pain)

max_pain_strike, _ = calc_max_pain(strikes, c_oi_arr, p_oi_arr)
print(f"\n  Max Pain（最大痛苦点）: ${max_pain_strike:.1f}")

# ─── Top OI 行权价汇总 ─────────────────────────────────────────────────────────
print("\n── Top 10 Call OI（全到期日合计）──────────────")
top_calls = sorted(zip(strikes, c_oi_arr), key=lambda x: -x[1])[:10]
for s, oi in top_calls:
    otm = (s - current_price) / current_price * 100
    tag = "ITM" if s < current_price else f"OTM+{otm:.1f}%"
    print(f"  ${s:>6.1f}  {oi:>8,.0f} 手  {tag}")

print("\n── Top 10 Put OI（全到期日合计）──────────────")
top_puts = sorted(zip(strikes, p_oi_arr), key=lambda x: -x[1])[:10]
for s, oi in top_puts:
    otm = (current_price - s) / current_price * 100
    tag = "ITM" if s > current_price else f"OTM+{otm:.1f}%"
    print(f"  ${s:>6.1f}  {oi:>8,.0f} 手  {tag}")

# ─── 绘图 ─────────────────────────────────────────────────────────────────────
print("\n▶ 绘图...")

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), facecolor="#F2F1ED")
fig.suptitle(f"NVDA 期权 OI 墙  |  当前价 ${current_price:.2f}  |  {datetime.today().strftime('%Y-%m-%d')}",
             fontsize=13, color="#1a1a1a", y=0.98)

# 图1：OI 墙（Call/Put 镜像）
ax1.set_facecolor("#fff")
ax1.bar(strikes, c_oi_arr, width=1.8, color="#639922", alpha=0.85, label="Call OI")
ax1.bar(strikes, -p_oi_arr, width=1.8, color="#E24B4A", alpha=0.85, label="Put OI")
ax1.axvline(current_price, color="#534AB7", linewidth=2, linestyle="--", label=f"当前价 ${current_price:.2f}")
ax1.axvline(max_pain_strike, color="#BA7517", linewidth=1.5, linestyle=":", label=f"Max Pain ${max_pain_strike:.0f}")
ax1.axhline(0, color="#ccc", linewidth=0.5)
ymax = max(c_oi_arr.max(), p_oi_arr.max()) * 1.1
ax1.set_ylim(-ymax, ymax)
ax1.set_ylabel("OI（手）", fontsize=10, color="#555")
ax1.set_title("全到期日合计 OI 墙（上=Call  下=Put）", fontsize=11, color="#555", pad=8)
ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{abs(v)/1000:.0f}K"))
ax1.set_xlim(lo, min(hi, current_price * 1.35))
ax1.legend(fontsize=9, loc="upper left")
ax1.spines["top"].set_visible(False)
ax1.spines["right"].set_visible(False)
ax1.set_facecolor("#fafaf8")
ax1.grid(axis="y", color="#eee", linewidth=0.5)

# 图2：Net OI（Call - Put）热力图
net_oi = c_oi_arr - p_oi_arr
colors = ["#3B6D11" if v >= 0 else "#E24B4A" for v in net_oi]
ax2.bar(strikes, net_oi, width=1.8, color=colors, alpha=0.8)
ax2.axvline(current_price, color="#534AB7", linewidth=2, linestyle="--")
ax2.axvline(max_pain_strike, color="#BA7517", linewidth=1.5, linestyle=":")
ax2.axhline(0, color="#bbb", linewidth=0.8)
ax2.set_ylabel("净 OI (Call−Put)", fontsize=10, color="#555")
ax2.set_xlabel("行权价 $", fontsize=10, color="#555")
ax2.set_title("净 OI（绿=Call主导  红=Put主导）", fontsize=11, color="#555", pad=8)
ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1000:.0f}K"))
ax2.set_xlim(lo, min(hi, current_price * 1.35))
ax2.spines["top"].set_visible(False)
ax2.spines["right"].set_visible(False)
ax2.set_facecolor("#fafaf8")
ax2.grid(axis="y", color="#eee", linewidth=0.5)

plt.tight_layout(rect=[0, 0, 1, 0.97])
plt.savefig(PNG_OUT, dpi=150, bbox_inches="tight")
plt.close()

print(f"\n✅ PNG 图表已保存: {os.path.abspath(PNG_OUT)}")

# ─── 生成 HTML（内嵌 PNG + 数据表）──────────────────────────────────────────
import base64
with open(PNG_OUT, "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

total_call_oi = int(calls_df["openInterest"].sum())
total_put_oi  = int(puts_df["openInterest"].sum())
total_oi      = total_call_oi + total_put_oi
pc_ratio      = total_put_oi / total_call_oi if total_call_oi > 0 else 0

top_c_rows = "".join(
    f'<tr><td>${s:.1f}</td><td style="text-align:right;color:#3B6D11;font-weight:500;">{oi:,.0f}</td>'
    f'<td style="text-align:right;color:#888;font-size:11px;">{"ITM" if s<current_price else f"OTM+{(s-current_price)/current_price*100:.1f}%"}</td></tr>'
    for s, oi in top_calls
)
top_p_rows = "".join(
    f'<tr><td>${s:.1f}</td><td style="text-align:right;color:#E24B4A;font-weight:500;">{oi:,.0f}</td>'
    f'<td style="text-align:right;color:#888;font-size:11px;">{"ITM" if s>current_price else f"OTM+{(current_price-s)/current_price*100:.1f}%"}</td></tr>'
    for s, oi in top_puts
)

html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<title>NVDA OI 墙完整版</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,sans-serif;}}
body{{background:#f2f1ed;color:#1a1a1a;padding:24px 20px;}}
.card{{background:#fff;border:1px solid rgba(0,0,0,0.08);border-radius:12px;padding:20px 24px;margin-bottom:16px;}}
h1{{font-size:17px;font-weight:600;margin-bottom:4px;}}
.sub{{font-size:11px;color:#999;margin-top:3px;}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px;}}
.m{{background:#f8f7f3;border-radius:8px;padding:12px 14px;}}
.ml{{font-size:11px;color:#999;margin-bottom:4px;}}
.mv{{font-size:21px;font-weight:600;}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th{{text-align:left;padding:6px 10px;color:#aaa;font-weight:500;font-size:11px;border-bottom:1px solid #eee;}}
th.r{{text-align:right;}}
td{{padding:6px 10px;border-bottom:0.5px solid #f5f5f5;}}
img{{width:100%;border-radius:8px;}}
</style></head><body>
<div class="card">
  <h1>NVDA 完整 OI 墙</h1>
  <p class="sub">到期日：{", ".join(ALL_EXPS[:6])} 等 {len(ALL_EXPS)} 个 &nbsp;·&nbsp; 生成时间：{datetime.today().strftime('%Y-%m-%d %H:%M')}</p>
</div>
<div class="grid">
  <div class="m"><div class="ml">总 OI（{len(ALL_EXPS)} 个到期日）</div><div class="mv">{total_oi:,}</div></div>
  <div class="m"><div class="ml">总 Call OI</div><div class="mv" style="color:#3B6D11;">{total_call_oi:,}</div></div>
  <div class="m"><div class="ml">总 Put OI</div><div class="mv" style="color:#E24B4A;">{total_put_oi:,}</div></div>
  <div class="m"><div class="ml">P/C OI 比 · Max Pain</div>
    <div class="mv">{pc_ratio:.3f} · <span style="color:#BA7517;">${max_pain_strike:.0f}</span></div></div>
</div>
<div class="card"><img src="data:image/png;base64,{img_b64}" alt="NVDA OI 墙图表"></div>
<div class="grid2">
  <div class="card">
    <p style="font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.04em;margin-bottom:12px;">Top 10 Call OI</p>
    <table><thead><tr><th>行权价</th><th class="r">OI（手）</th><th class="r">位置</th></tr></thead>
    <tbody>{top_c_rows}</tbody></table>
  </div>
  <div class="card">
    <p style="font-size:11px;color:#aaa;text-transform:uppercase;letter-spacing:.04em;margin-bottom:12px;">Top 10 Put OI</p>
    <table><thead><tr><th>行权价</th><th class="r">OI（手）</th><th class="r">位置</th></tr></thead>
    <tbody>{top_p_rows}</tbody></table>
  </div>
</div>
</body></html>"""

with open(OUTPUT, "w", encoding="utf-8") as f:
    f.write(html)

print(f"✅ HTML 报告已保存: {os.path.abspath(OUTPUT)}")
print(f"\n── 关键数字 ─────────────────────────────────────")
print(f"  总 OI（{len(ALL_EXPS)} 个到期日）: {total_oi:,} 手")
print(f"  P/C OI 比: {pc_ratio:.3f}")
print(f"  Max Pain: ${max_pain_strike:.0f}")
print(f"  最大 Call OI: ${top_calls[0][0]:.0f}  {top_calls[0][1]:,.0f} 手")
print(f"  最大 Put OI:  ${top_puts[0][0]:.0f}  {top_puts[0][1]:,.0f} 手")
