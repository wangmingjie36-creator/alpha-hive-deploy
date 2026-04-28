"""
ff6_cycle_history.py
=====================
过去 20 年 NVDA FF6 因子周期比对分析

功能：
  1. 下载 NVDA 20 年日线价格（yfinance）
  2. 下载 Fama-French 6 因子日频数据（French 数据库）
  3. 滚动 252 日 OLS 回归 → 每日因子载荷 + Jensen Alpha + IR
  4. 找出与今日因子读数相似的历史窗口
  5. 统计每个匹配窗口后续 1 个月（22 交易日）的价格表现
  6. 生成 HTML 报告 → output/ff6_history.html

运行方式（在用户 Mac 终端）：
  cd ~/Desktop/Alpha\ Hive
  python3 ff6_cycle_history.py
"""

import os, io, zipfile, warnings
import numpy as np
import pandas as pd
import yfinance as yf
import urllib.request
from scipy import stats
from datetime import datetime

warnings.filterwarnings("ignore")

# ─── 目标因子（今日读数）───────────────────────────────────────────────────────
TARGET = {
    "beta_mkt":  1.637,
    "beta_hml": -0.980,
    "beta_mom":  0.470,
    "alpha_ann": 14.46,   # %
    "ir":         0.64,
}

# 相似度匹配容差（±）
TOL = {
    "beta_mkt":  0.25,
    "beta_hml":  0.35,
    "beta_mom":  0.20,
}

ROLL = 252          # 滚动窗口（交易日）
FWD  = 22           # 前瞻窗口（~1 个月）
START_DATE = "2004-01-01"
END_DATE   = datetime.today().strftime("%Y-%m-%d")
TICKER = "NVDA"

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─── Step 1: 下载 NVDA 价格 ──────────────────────────────────────────────────
print("▶ Step 1: 下载 NVDA 20 年日线价格...")
raw = yf.download(TICKER, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
if raw.empty:
    raise RuntimeError("yfinance 下载失败，请检查网络")

nvda = raw["Close"].squeeze()
nvda.name = "NVDA"
nvda_ret = nvda.pct_change().dropna()
print(f"   NVDA 数据：{nvda.index[0].date()} ~ {nvda.index[-1].date()}，{len(nvda)} 行")

# ─── Step 2: 下载 French 因子数据 ────────────────────────────────────────────
def _fetch_french_zip(url):
    """下载 French zip 文件，返回第一个 CSV 的 DataFrame"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, timeout=30)
    buf = io.BytesIO(resp.read())
    with zipfile.ZipFile(buf) as z:
        name = [n for n in z.namelist() if n.endswith(".CSV") or n.endswith(".csv")][0]
        with z.open(name) as f:
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    # 跳过注释行，找到数据起始行
    start_row = 0
    for i, l in enumerate(lines):
        if l.strip().startswith("2") or l.strip().startswith("1"):
            start_row = i
            break
    data_str = "\n".join(lines[start_row:])
    df = pd.read_csv(io.StringIO(data_str), header=None)
    return df

print("▶ Step 2: 下载 Fama-French 因子数据...")

# FF5 日频（含 Mkt-RF, SMB, HML, RMW, CMA, RF）
FF5_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
# Momentum 日频（WML）
MOM_URL  = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip"

def load_french_daily(url, col_names):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urllib.request.urlopen(req, timeout=60)
    buf = io.BytesIO(resp.read())
    with zipfile.ZipFile(buf) as z:
        csv_name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
        raw_bytes = z.read(csv_name).decode("utf-8", errors="ignore")
    lines = raw_bytes.splitlines()
    data_lines = []
    for l in lines:
        stripped = l.strip()
        if stripped and stripped[0].isdigit() and len(stripped) >= 8:
            data_lines.append(stripped)
    df = pd.read_csv(io.StringIO("\n".join(data_lines)), header=None)
    df.columns = col_names[:df.shape[1]]
    df["Date"] = pd.to_datetime(df["Date"].astype(str), format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["Date"]).set_index("Date")
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce") / 100.0  # pct → decimal
    return df

ff5  = load_french_daily(FF5_URL, ["Date","Mkt_RF","SMB","HML","RMW","CMA","RF"])
mom  = load_french_daily(MOM_URL,  ["Date","Mom"])
print(f"   FF5 数据：{ff5.index[0].date()} ~ {ff5.index[-1].date()}")
print(f"   Mom 数据：{mom.index[0].date()} ~ {mom.index[-1].date()}")

# ─── Step 3: 合并 & 计算超额收益 ─────────────────────────────────────────────
print("▶ Step 3: 合并数据...")
factors = ff5.join(mom, how="inner")
factors["NVDA_ex"] = nvda_ret.reindex(factors.index) - factors["RF"]
factors = factors.dropna(subset=["NVDA_ex","Mkt_RF","SMB","HML","RMW","CMA","Mom"])

print(f"   合并后数据：{factors.index[0].date()} ~ {factors.index[-1].date()}，{len(factors)} 行")

# ─── Step 4: 滚动 FF6 回归 ───────────────────────────────────────────────────
print(f"▶ Step 4: 滚动 {ROLL} 日 FF6 OLS 回归（约需 10-30 秒）...")

factor_cols = ["Mkt_RF","SMB","HML","RMW","CMA","Mom"]
X_all = factors[factor_cols].values
y_all = factors["NVDA_ex"].values
dates = factors.index

results = []
n = len(factors)

for i in range(ROLL, n):
    X = X_all[i-ROLL:i]
    y = y_all[i-ROLL:i]
    Xc = np.column_stack([np.ones(ROLL), X])
    try:
        coef, res, _, _ = np.linalg.lstsq(Xc, y, rcond=None)
    except Exception:
        continue
    alpha_daily = coef[0]
    betas = coef[1:]  # Mkt,SMB,HML,RMW,CMA,Mom
    y_hat = Xc @ coef
    ss_res = np.sum((y - y_hat)**2)
    ss_tot = np.sum((y - y.mean())**2)
    r2 = 1 - ss_res/ss_tot if ss_tot > 0 else 0
    resid = y - y_hat
    te = resid.std() * np.sqrt(252)
    alpha_ann = alpha_daily * 252
    ir = (alpha_daily / resid.std()) * np.sqrt(252) if resid.std() > 0 else 0
    results.append({
        "date": dates[i],
        "beta_mkt": betas[0],
        "beta_smb": betas[1],
        "beta_hml": betas[2],
        "beta_rmw": betas[3],
        "beta_cma": betas[4],
        "beta_mom": betas[5],
        "alpha_ann": alpha_ann * 100,  # 转为 %
        "ir": ir,
        "r2": r2,
        "te": te * 100,
    })

reg = pd.DataFrame(results).set_index("date")
reg["price"] = nvda.reindex(reg.index, method="ffill")
print(f"   回归完成，{len(reg)} 个窗口")

# ─── Step 5: 找相似窗口 ──────────────────────────────────────────────────────
print("▶ Step 5: 筛选相似因子窗口...")
mask = (
    (reg["beta_mkt"].between(TARGET["beta_mkt"] - TOL["beta_mkt"], TARGET["beta_mkt"] + TOL["beta_mkt"])) &
    (reg["beta_hml"].between(TARGET["beta_hml"] - TOL["beta_hml"], TARGET["beta_hml"] + TOL["beta_hml"])) &
    (reg["beta_mom"].between(TARGET["beta_mom"] - TOL["beta_mom"], TARGET["beta_mom"] + TOL["beta_mom"]))
)
similar = reg[mask].copy()

# 去重：相邻 22 天只取一个
similar = similar[~(similar.index.to_series().diff().dt.days.lt(22).fillna(False))]

print(f"   找到 {len(similar)} 个相似窗口")

# ─── Step 6: 计算前瞻收益 ────────────────────────────────────────────────────
print("▶ Step 6: 计算后续 1 个月收益...")
fwd_data = []
for dt in similar.index:
    future_idx = nvda.index[nvda.index > dt]
    if len(future_idx) >= FWD:
        p0   = nvda.loc[dt]
        p_t1 = nvda.iloc[nvda.index.get_loc(dt) + 1] if nvda.index.get_loc(dt)+1 < len(nvda) else None
        p_1m = nvda.loc[future_idx[FWD-1]]
        ret_1m = (p_1m / p0 - 1) * 100
        ret_1d = (p_t1 / p0 - 1) * 100 if p_t1 else None
        # max drawdown in window
        window_prices = nvda.loc[future_idx[:FWD]]
        peak = p0
        mdd = 0
        for p in window_prices:
            if p > peak: peak = p
            dd = (p - peak) / peak * 100
            if dd < mdd: mdd = dd
        fwd_data.append({
            "date": dt,
            "year": dt.year,
            "price": similar.loc[dt, "price"],
            "beta_mkt": similar.loc[dt, "beta_mkt"],
            "beta_hml": similar.loc[dt, "beta_hml"],
            "beta_mom": similar.loc[dt, "beta_mom"],
            "alpha_ann": similar.loc[dt, "alpha_ann"],
            "ir": similar.loc[dt, "ir"],
            "ret_1d_pct": ret_1d,
            "ret_1m_pct": ret_1m,
            "p_1m": p_1m,
            "mdd_1m": mdd,
        })
    else:
        # 最近的日期（未来数据不足）
        fwd_data.append({
            "date": dt,
            "year": dt.year,
            "price": similar.loc[dt, "price"],
            "beta_mkt": similar.loc[dt, "beta_mkt"],
            "beta_hml": similar.loc[dt, "beta_hml"],
            "beta_mom": similar.loc[dt, "beta_mom"],
            "alpha_ann": similar.loc[dt, "alpha_ann"],
            "ir": similar.loc[dt, "ir"],
            "ret_1d_pct": None,
            "ret_1m_pct": None,
            "p_1m": None,
            "mdd_1m": None,
        })

fwd = pd.DataFrame(fwd_data)
has_fwd = fwd[fwd["ret_1m_pct"].notna()]
print(f"   有前瞻数据：{len(has_fwd)} 个窗口")

# ─── Step 7: 统计摘要 ────────────────────────────────────────────────────────
up_count   = (has_fwd["ret_1m_pct"] > 0).sum()
down_count = (has_fwd["ret_1m_pct"] <= 0).sum()
win_rate   = up_count / len(has_fwd) * 100 if len(has_fwd) > 0 else 0
avg_ret    = has_fwd["ret_1m_pct"].mean()
med_ret    = has_fwd["ret_1m_pct"].median()
avg_mdd    = has_fwd["mdd_1m"].mean()
best_ret   = has_fwd["ret_1m_pct"].max()
worst_ret  = has_fwd["ret_1m_pct"].min()

print(f"\n──── 统计摘要 ────")
print(f"相似窗口数量    : {len(has_fwd)}")
print(f"上涨次数        : {up_count}  下跌次数: {down_count}")
print(f"1个月胜率       : {win_rate:.1f}%")
print(f"平均1月收益     : {avg_ret:+.2f}%")
print(f"中位1月收益     : {med_ret:+.2f}%")
print(f"最佳收益        : {best_ret:+.2f}%")
print(f"最差收益        : {worst_ret:+.2f}%")
print(f"平均最大回撤    : {avg_mdd:.2f}%")

# ─── Step 8: 生成 HTML ───────────────────────────────────────────────────────
print("\n▶ Step 8: 生成 HTML 报告...")

# 准备折线图数据（全部窗口的 1M 收益，按年分布）
chart_years  = has_fwd["year"].tolist()
chart_rets   = has_fwd["ret_1m_pct"].round(2).tolist()
chart_dates  = has_fwd["date"].dt.strftime("%Y-%m-%d").tolist()
chart_prices = has_fwd["price"].round(2).tolist()
chart_mdd    = has_fwd["mdd_1m"].round(2).tolist()
chart_ir     = has_fwd["ir"].round(3).tolist()
chart_bm     = has_fwd["beta_mkt"].round(3).tolist()
chart_bh     = has_fwd["beta_hml"].round(3).tolist()
chart_bmo    = has_fwd["beta_mom"].round(3).tolist()

def ret_color(v):
    if v > 15:  return "#3B6D11"
    if v > 5:   return "#639922"
    if v > 0:   return "#97C459"
    if v > -5:  return "#E24B4A"
    return "#A32D2D"

rows_html = ""
for _, r in has_fwd.sort_values("date", ascending=False).iterrows():
    rv = r["ret_1m_pct"]
    arrow = "▲" if rv > 0 else "▼"
    color = ret_color(rv)
    mdd_v = r["mdd_1m"]
    rows_html += f"""
    <tr>
      <td style="padding:7px 10px;color:var(--text)">{r['date'].strftime('%Y-%m-%d')}</td>
      <td style="padding:7px 10px;text-align:right;">$ {r['price']:.2f}</td>
      <td style="padding:7px 10px;text-align:right;">{r['beta_mkt']:.3f}</td>
      <td style="padding:7px 10px;text-align:right;">{r['beta_hml']:.3f}</td>
      <td style="padding:7px 10px;text-align:right;">{r['beta_mom']:.3f}</td>
      <td style="padding:7px 10px;text-align:right;">{r['ir']:.2f}</td>
      <td style="padding:7px 10px;text-align:right;font-weight:500;color:{color};">{arrow} {rv:+.1f}%</td>
      <td style="padding:7px 10px;text-align:right;color:#A32D2D;">{mdd_v:.1f}%</td>
    </tr>"""

html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>NVDA FF6 20年周期比对</title>
<style>
  :root {{ --bg:#fff; --bg2:#f5f4f0; --text:#1a1a1a; --muted:#666; --border:rgba(0,0,0,0.1); }}
  * {{ box-sizing:border-box; margin:0; padding:0; font-family: -apple-system, sans-serif; }}
  body {{ background:var(--bg2); color:var(--text); padding:32px 24px; }}
  .card {{ background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:20px 24px; margin-bottom:20px; }}
  h1 {{ font-size:20px; font-weight:500; margin-bottom:4px; }}
  h2 {{ font-size:15px; font-weight:500; margin-bottom:14px; color:var(--muted); letter-spacing:.03em; text-transform:uppercase; font-size:11px; }}
  .grid4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }}
  .metric {{ background:var(--bg2); border-radius:8px; padding:14px 16px; }}
  .m-label {{ font-size:12px; color:var(--muted); margin-bottom:4px; }}
  .m-val {{ font-size:22px; font-weight:500; }}
  .up {{ color:#3B6D11; }} .down {{ color:#A32D2D; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  thead tr {{ border-bottom:1px solid var(--border); }}
  th {{ padding:8px 10px; text-align:right; font-weight:500; color:var(--muted); font-size:11px; }}
  th:first-child {{ text-align:left; }}
  tbody tr {{ border-bottom:0.5px solid var(--border); }}
  tbody tr:hover {{ background:var(--bg2); }}
  .chart-wrap {{ position:relative; height:300px; }}
  .target-box {{ background:#EEEDFE; border-radius:8px; padding:12px 16px; margin-bottom:16px; font-size:13px; color:#3C3489; line-height:1.7; }}
</style>
</head>
<body>
<div class="card">
  <h1>NVDA · Fama-French 6因子 · 20年历史周期比对</h1>
  <p style="font-size:13px;color:var(--muted);margin-top:6px;">筛选标准：β_mkt ∈ [{TARGET["beta_mkt"]-TOL["beta_mkt"]:.2f}, {TARGET["beta_mkt"]+TOL["beta_mkt"]:.2f}] · β_hml ∈ [{TARGET["beta_hml"]-TOL["beta_hml"]:.2f}, {TARGET["beta_hml"]+TOL["beta_hml"]:.2f}] · β_mom ∈ [{TARGET["beta_mom"]-TOL["beta_mom"]:.2f}, {TARGET["beta_mom"]+TOL["beta_mom"]:.2f}] · 滚动窗口 {ROLL}日 · 生成时间 {datetime.today().strftime('%Y-%m-%d %H:%M')}</p>
</div>

<div class="card">
  <div class="target-box">
    今日因子基准（2026-04-27）：β_mkt = {TARGET["beta_mkt"]} · β_hml = {TARGET["beta_hml"]} · β_mom = {TARGET["beta_mom"]} · Alpha = {TARGET["alpha_ann"]}% · IR = {TARGET["ir"]}
  </div>
  <div class="grid4">
    <div class="metric"><div class="m-label">历史匹配窗口数</div><div class="m-val">{len(has_fwd)}</div></div>
    <div class="metric"><div class="m-label">1个月胜率</div><div class="m-val {'up' if win_rate>=50 else 'down'}">{win_rate:.1f}%</div></div>
    <div class="metric"><div class="m-label">平均1月收益</div><div class="m-val {'up' if avg_ret>0 else 'down'}">{avg_ret:+.1f}%</div></div>
    <div class="metric"><div class="m-label">中位1月收益</div><div class="m-val {'up' if med_ret>0 else 'down'}">{med_ret:+.1f}%</div></div>
    <div class="metric"><div class="m-label">最佳1月收益</div><div class="m-val up">{best_ret:+.1f}%</div></div>
    <div class="metric"><div class="m-label">最差1月收益</div><div class="m-val down">{worst_ret:+.1f}%</div></div>
    <div class="metric"><div class="m-label">平均最大回撤</div><div class="m-val down">{avg_mdd:.1f}%</div></div>
    <div class="metric"><div class="m-label">上涨/下跌</div><div class="m-val">{up_count} / {down_count}</div></div>
  </div>
</div>

<div class="card">
  <h2>各窗口后续1月收益分布</h2>
  <div class="chart-wrap"><canvas id="barChart" role="img" aria-label="Bar chart of 1-month forward returns for each similar FF6 factor window over 20 years"></canvas></div>
</div>

<div class="card">
  <h2>历史匹配窗口明细（最新优先）</h2>
  <div style="overflow-x:auto;">
  <table>
    <thead><tr>
      <th style="text-align:left;">日期</th>
      <th>入场价</th>
      <th>β_mkt</th>
      <th>β_hml</th>
      <th>β_mom</th>
      <th>IR</th>
      <th>后续1月</th>
      <th>期间MDD</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const dates   = {chart_dates};
const rets    = {chart_rets};
const prices  = {chart_prices};
const mdds    = {chart_mdd};
const irs     = {chart_ir};

const bgColors = rets.map(r => r > 15 ? '#3B6D11' : r > 5 ? '#639922' : r > 0 ? '#97C459' : r > -5 ? '#F09595' : '#E24B4A');

new Chart(document.getElementById('barChart'), {{
  type: 'bar',
  data: {{
    labels: dates,
    datasets: [{{
      label: '后续1月收益 %',
      data: rets,
      backgroundColor: bgColors,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        callbacks: {{
          title: ctx => dates[ctx[0].dataIndex],
          label: ctx => [
            ' 1月收益: ' + rets[ctx.dataIndex].toFixed(2) + '%',
            ' 入场价: $' + prices[ctx.dataIndex].toFixed(2),
            ' 期间MDD: ' + mdds[ctx.dataIndex].toFixed(2) + '%',
            ' IR: ' + irs[ctx.dataIndex].toFixed(3),
          ]
        }}
      }}
    }},
    scales: {{
      x: {{ ticks: {{ maxRotation: 45, font: {{ size: 10 }} }}, grid: {{ display: false }} }},
      y: {{
        title: {{ display: true, text: '1月收益 %', font: {{ size: 11 }} }},
        ticks: {{ callback: v => v.toFixed(0) + '%' }},
        grid: {{ color: 'rgba(0,0,0,0.06)' }}
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

out_path = os.path.join(OUTPUT_DIR, "ff6_history.html")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"\n✅ 报告已生成: {os.path.abspath(out_path)}")
print("   在浏览器中打开即可查看完整分析")
print(f"\n── 快速摘要 ──────────────────────────────────")
print(f"匹配窗口: {len(has_fwd)} 个   胜率: {win_rate:.1f}%   均值: {avg_ret:+.2f}%   中位: {med_ret:+.2f}%")
print(f"最佳: {best_ret:+.2f}%   最差: {worst_ret:+.2f}%   平均MDD: {avg_mdd:.2f}%")
