"""
NVDA 空头全景扫描
1. 股票融券做空（Short Interest）
2. 期权全链 Put OI 分布（各到期日）
3. 最大Put仓位 + 真实溢价筛选（排除彩票）
4. Put/Call 比值热力图

运行: python3 check_short_positions.py
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, date

ticker = yf.Ticker("NVDA")
spot   = ticker.fast_info.last_price
info   = ticker.info
print(f"\nNVDA 现价: ${spot:.2f}  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")

# ══════════════════════════════════════════════════════════
# 1. 股票做空数据（融券卖空）
# ══════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print(f"  1. 融券做空数据（Stock Short Interest）")
print(f"{'='*62}")

shares_short      = info.get('sharesShort', 0)
shares_short_prev = info.get('sharesShortPriorMonth', 0)
float_shares      = info.get('floatShares', 1)
short_ratio       = info.get('shortRatio', 0)      # 做空比率（天数平仓）
short_pct_float   = info.get('shortPercentOfFloat', 0)
shares_outstanding= info.get('sharesOutstanding', 1)

si_pct = short_pct_float * 100 if short_pct_float <= 1.0 else short_pct_float
si_change = (shares_short - shares_short_prev) / shares_short_prev * 100 if shares_short_prev else 0

print(f"  融券股数:         {shares_short:>15,.0f} 股")
print(f"  上月融券股数:     {shares_short_prev:>15,.0f} 股")
print(f"  环比变化:         {si_change:>+14.1f}%  {'↑ 空头增加' if si_change > 5 else '↓ 空头减少' if si_change < -5 else '→ 基本不变'}")
print(f"  占流通股比例:     {si_pct:>14.2f}%")
print(f"  Short Ratio:      {short_ratio:>14.1f} 天（空头平仓需要的交易日）")
print(f"  做空名义市值:     ${shares_short * spot / 1e9:>13.2f}B")

if si_pct < 1.5:
    si_level = "极低（机构不看空）"
elif si_pct < 3:
    si_level = "偏低（少量对冲）"
elif si_pct < 6:
    si_level = "中等（有一定空头）"
else:
    si_level = "偏高（显著空头压力）"
print(f"  空头强度评级:     {si_level}")

# ══════════════════════════════════════════════════════════
# 2. 期权全链 Put 分布（各到期日）
# ══════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print(f"  2. 全链 Put OI 分布（各到期日）")
print(f"{'='*62}")

opts = ticker.options[:10]  # 前10个到期日
exp_summary = []

for exp in opts:
    try:
        ch = ticker.option_chain(exp)
        c  = ch.calls[ch.calls.strike > 5]
        p  = ch.puts[ch.puts.strike > 5]
        c_oi = c.openInterest.fillna(0).sum()
        p_oi = p.openInterest.fillna(0).sum()
        c_vol= c.volume.fillna(0).sum()
        p_vol= p.volume.fillna(0).sum()
        pc_oi= p_oi / c_oi if c_oi > 0 else 0

        # 最大Put OI行权价
        top_put = p.sort_values('openInterest', ascending=False).iloc[0] if len(p) > 0 else None
        top_put_str = f"${top_put.strike:.0f}(OI={top_put.openInterest:,.0f})" if top_put is not None else "—"

        exp_summary.append({
            'exp': exp, 'c_oi': c_oi, 'p_oi': p_oi,
            'pc_oi': pc_oi, 'c_vol': c_vol, 'p_vol': p_vol,
            'top_put': top_put_str
        })
    except Exception as e:
        pass

print(f"  {'到期日':>12}  {'Call OI':>10}  {'Put OI':>10}  {'P/C':>5}  {'最大Put行权价'}")
for row in exp_summary:
    bar = "█" * int(row['pc_oi'] * 5)
    alert = " ⚠️" if row['pc_oi'] > 1.0 else ""
    print(f"  {row['exp']:>12}  {row['c_oi']:>10,.0f}  {row['p_oi']:>10,.0f}  {row['pc_oi']:>4.2f}  {row['top_put']}{alert}")

# ══════════════════════════════════════════════════════════
# 3. 全链最大 Put OI（含真实溢价筛选）
# ══════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print(f"  3. 全链最大 Put 仓位（按OI排序，过滤超深OTM彩票）")
print(f"{'='*62}")

all_puts = []
for exp in opts[:8]:
    try:
        ch = ticker.option_chain(exp)
        p  = ch.puts[ch.puts.strike > 5].copy()
        p['expiry'] = exp
        all_puts.append(p)
    except:
        pass

puts_all = pd.concat(all_puts, ignore_index=True)

# 过滤：行权价 > 现价*0.55（排除彩票式超深OTM）
puts_real = puts_all[puts_all.strike >= spot * 0.55].copy()
puts_real['mid'] = (puts_real.bid + puts_real.ask) / 2
puts_real.loc[puts_real.mid <= 0, 'mid'] = puts_real['lastPrice']
puts_real['notional_m'] = puts_real.mid * puts_real.openInterest * 100 / 1e6
puts_real['dist'] = (puts_real.strike - spot) / spot * 100
puts_real['vol_oi'] = puts_real.volume / puts_real.openInterest.replace(0, np.nan)

# 按OI排序
top_puts = puts_real.sort_values('openInterest', ascending=False).head(20)

print(f"  {'到期':>12}  {'Strike':>7}  {'OI':>9}  {'Volume':>8}  {'V/OI':>5}  {'mid':>7}  {'名义$M':>8}  {'距现价':>8}  性质")
for _, r in top_puts.iterrows():
    # 判断性质
    if r.strike < spot * 0.80:
        note = "尾部对冲"
    elif r.strike < spot * 0.92:
        note = "下行保护"
    elif r.strike >= spot * 0.98:
        note = "近ATM做空"
    else:
        note = "定向看跌"
    flag = " ⚡" if (r.vol_oi or 0) > 1.5 and r.volume > 3000 else ""
    print(f"  {r.expiry:>12}  ${r.strike:>6.0f}  {r.openInterest:>9,.0f}  {r.volume:>8,.0f}  {r.vol_oi:>4.1f}x  ${r.mid:>6.2f}  ${r.notional_m:>7.1f}M  {r.dist:>+7.1f}%  {note}{flag}")

# ══════════════════════════════════════════════════════════
# 4. 真实空头：有价值的 Put（mid > $1，strike > 现价*0.85）
# ══════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print(f"  4. 有效空头仓位（mid > $1 且 strike > 现价×85%）")
print(f"     这类 Put 有真实 delta，是真正的方向性看跌押注")
print(f"{'='*62}")

real_shorts = puts_real[
    (puts_real.mid > 1.0) &
    (puts_real.strike >= spot * 0.85)
].sort_values('openInterest', ascending=False).head(15)

total_real_notional = real_shorts.notional_m.sum()
print(f"  总名义规模: ${total_real_notional:.1f}M\n")
print(f"  {'到期':>12}  {'Strike':>7}  {'OI':>9}  {'Volume':>8}  {'mid':>7}  {'名义$M':>8}  {'距现价':>8}  {'IV%':>6}")
for _, r in real_shorts.iterrows():
    print(f"  {r.expiry:>12}  ${r.strike:>6.0f}  {r.openInterest:>9,.0f}  {r.volume:>8,.0f}  ${r.mid:>6.2f}  ${r.notional_m:>7.1f}M  {r.dist:>+7.1f}%  {r.impliedVolatility*100:>5.1f}%")

# ══════════════════════════════════════════════════════════
# 5. 近ATM Put vs Call OI 对比（±$20）
# ══════════════════════════════════════════════════════════
print(f"\n{'='*62}")
print(f"  5. 近ATM Put vs Call OI 对比（±$20，5/22 + 6/18）")
print(f"{'='*62}")

for exp_check in [opts[0], "2026-06-18"]:
    try:
        ch = ticker.option_chain(exp_check)
        c = ch.calls[(ch.calls.strike >= spot-20) & (ch.calls.strike <= spot+20)]
        p = ch.puts[(ch.puts.strike >= spot-20) & (ch.puts.strike <= spot+20)]
        c_oi_sum = c.openInterest.sum()
        p_oi_sum = p.openInterest.sum()
        print(f"\n  [{exp_check}]  近ATM Call OI={c_oi_sum:,.0f}  Put OI={p_oi_sum:,.0f}  P/C={p_oi_sum/c_oi_sum:.2f}")
        strikes = sorted(set(c.strike) | set(p.strike))
        print(f"  {'Strike':>7}  {'Call OI':>9}  {'Put OI':>9}  {'偏向'}")
        for s in strikes:
            c_row = c[c.strike == s]
            p_row = p[p.strike == s]
            c_oi_val = c_row.openInterest.values[0] if len(c_row) > 0 else 0
            p_oi_val = p_row.openInterest.values[0] if len(p_row) > 0 else 0
            bias = "←多" if c_oi_val > p_oi_val * 1.5 else "→空" if p_oi_val > c_oi_val * 1.5 else "均衡"
            print(f"  ${s:>6.0f}  {c_oi_val:>9,.0f}  {p_oi_val:>9,.0f}  {bias}")
    except Exception as e:
        print(f"  [{exp_check}] 获取失败: {e}")

print(f"\n完成。\n")
