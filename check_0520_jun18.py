"""
NVDA 6/18 期权全链深度分析（更新版）
运行: python3 check_0520_jun18.py
"""
import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime, date

ticker = yf.Ticker("NVDA")
spot = ticker.fast_info.last_price
print(f"\nNVDA 现价: ${spot:.2f}  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")

exp = "2026-06-18"
chain = ticker.option_chain(exp)
calls = chain.calls.copy()
puts  = chain.puts.copy()

# 过滤 $0 伪影
calls = calls[calls.strike > 5]
puts  = puts[puts.strike > 5]

total_c_vol = calls.volume.fillna(0).sum()
total_p_vol = puts.volume.fillna(0).sum()
total_vol   = total_c_vol + total_p_vol
total_c_oi  = calls.openInterest.fillna(0).sum()
total_p_oi  = puts.openInterest.fillna(0).sum()

print(f"\n{'='*62}")
print(f"  6/18 整体统计")
print(f"{'='*62}")
print(f"  Call OI  : {total_c_oi:>12,.0f}")
print(f"  Put  OI  : {total_p_oi:>12,.0f}  P/C OI = {total_p_oi/total_c_oi:.3f}")
print(f"  Call Vol : {total_c_vol:>12,.0f}  ({total_c_vol/total_vol*100:.1f}%)")
print(f"  Put  Vol : {total_p_vol:>12,.0f}  ({total_p_vol/total_vol*100:.1f}%)")

# ── Max Pain ──────────────────────────────────────────────
print(f"\n  计算 Max Pain...")
all_strikes = sorted(set(calls.strike) | set(puts.strike))
pain = {}
for s in all_strikes:
    cp = calls[calls.strike > s].apply(lambda r: (r.strike - s) * r.openInterest, axis=1).sum()
    pp = puts[puts.strike < s].apply(lambda r: (s - r.strike) * r.openInterest, axis=1).sum()
    pain[s] = cp + pp
mp = min(pain, key=pain.get)

# top 5 min pain zone
pain_sorted = sorted(pain.items(), key=lambda x: x[1])
print(f"  Max Pain: ${mp:.0f}  (距现价 {(mp-spot)/spot*100:+.1f}%)")
print(f"  最低疼痛区前5: {[f'${s:.0f}' for s,_ in pain_sorted[:5]]}")

# ── ATM Straddle ──────────────────────────────────────────
atm = min(calls.strike, key=lambda x: abs(x - spot))
c_row = calls[calls.strike == atm].iloc[0]
p_row = puts[puts.strike == atm].iloc[0]
c_mid = (c_row.bid + c_row.ask)/2 if c_row.bid > 0 else c_row.lastPrice
p_mid = (p_row.bid + p_row.ask)/2 if p_row.bid > 0 else p_row.lastPrice
straddle = c_mid + p_mid
move_pct  = straddle / spot * 100
print(f"\n  ATM ${atm:.0f} Straddle = ${straddle:.2f}  ±{move_pct:.1f}%")
print(f"  隐含范围: ${spot-straddle:.1f} ~ ${spot+straddle:.1f}")

# ── Call Wall / Put Wall ──────────────────────────────────
top_call_oi = calls.sort_values('openInterest', ascending=False).head(1).iloc[0]
# Put wall: max OI where strike < spot (meaningful puts)
put_candidates = puts[puts.strike < spot + 10].sort_values('openInterest', ascending=False).head(1).iloc[0]
print(f"\n  Call Wall: ${top_call_oi.strike:.0f}  OI={top_call_oi.openInterest:,.0f}")
print(f"  Put  Wall: ${put_candidates.strike:.0f}  OI={put_candidates.openInterest:,.0f}")

# GEX proxy: largest OI near spot (±30)
calls_near = calls[(calls.strike >= spot-30) & (calls.strike <= spot+30)]
gex_zone = calls_near.sort_values('openInterest', ascending=False).head(3)
print(f"\n  近ATM Gamma密集区 (±$30):")
for _, r in gex_zone.iterrows():
    mid = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    print(f"    Call ${r.strike:.0f}  OI={r.openInterest:,.0f}  Vol={r.volume:,.0f}  mid=${mid:.2f}  IV={r.impliedVolatility*100:.1f}%")

# ── Top OI Call / Put ─────────────────────────────────────
print(f"\n{'='*62}")
print(f"  6/18 Top 15 Call OI")
print(f"{'='*62}")
print(f"  {'Strike':>7}  {'OI':>9}  {'Volume':>8}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>8}  {'性质'}")
top_calls = calls.sort_values('openInterest', ascending=False).head(15)
for _, r in top_calls.iterrows():
    mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    voi  = r.volume/r.openInterest if r.openInterest > 0 else 0
    # 判断性质
    if r.strike < spot * 0.92:
        note = "深ITM/备兑"
    elif r.strike > spot * 1.15:
        note = "彩票押注"
    elif abs(dist) < 3:
        note = "ATM核心"
    else:
        note = "OTM定向"
    flag = " ⚡" if voi > 1.5 and r.volume > 5000 else ""
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>9,.0f}  {r.volume:>8,.0f}  {voi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+7.1f}%  {note}{flag}")

print(f"\n{'='*62}")
print(f"  6/18 Top 12 Put OI（过滤超深OTM）")
print(f"{'='*62}")
print(f"  {'Strike':>7}  {'OI':>9}  {'Volume':>8}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>8}")
# filter meaningful puts (strike > spot*0.5)
puts_meaningful = puts[puts.strike > spot * 0.5].sort_values('openInterest', ascending=False).head(12)
for _, r in puts_meaningful.iterrows():
    mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    voi  = r.volume/r.openInterest if r.openInterest > 0 else 0
    flag = " ⚡" if voi > 1.5 and r.volume > 5000 else ""
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>9,.0f}  {r.volume:>8,.0f}  {voi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+7.1f}%{flag}")

# ── 今日异常成交 ──────────────────────────────────────────
print(f"\n{'='*62}")
print(f"  今日异常成交（V/OI > 1.8x 且 Volume > 3,000）")
print(f"{'='*62}")
combined = pd.concat([
    calls.assign(type='C'),
    puts.assign(type='P')
], ignore_index=True)
combined['vol_oi'] = combined.volume / combined.openInterest.replace(0, np.nan)
anom = combined[
    (combined.vol_oi > 1.8) &
    (combined.volume > 3000) &
    (combined.strike > 5)
].sort_values('volume', ascending=False)

if len(anom) == 0:
    print("  今日无异常成交")
else:
    print(f"  {'型':>3}  {'Strike':>7}  {'Volume':>8}  {'OI':>9}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>7}  {'名义($M)':>9}")
    for _, r in anom.iterrows():
        mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
        dist = (r.strike - spot)/spot*100
        nom  = mid * r.volume * 100 / 1e6
        print(f"  {r['type']:>3}  ${r.strike:>6.0f}  {r.volume:>8,.0f}  {r.openInterest:>9,.0f}  {r.vol_oi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+6.1f}%  ${nom:>8.2f}M")

# ── OI变化快照：和昨日比较 ────────────────────────────────
print(f"\n{'='*62}")
print(f"  OI最大Call行权价前10（磁吸结构确认）")
print(f"{'='*62}")
# Near spot focus ($150-$300)
calls_focus = calls[(calls.strike >= 150) & (calls.strike <= 320)].sort_values('openInterest', ascending=False).head(10)
for _, r in calls_focus.iterrows():
    mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    bar  = "█" * int(r.openInterest / 5000)
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>8,.0f}  {dist:>+7.1f}%  {bar}")

print(f"\n  OI最大Put行权价前8（支撑结构）")
puts_focus = puts[(puts.strike >= spot*0.7) & (puts.strike <= spot+10)].sort_values('openInterest', ascending=False).head(8)
for _, r in puts_focus.iterrows():
    mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    bar  = "█" * int(r.openInterest / 3000)
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>8,.0f}  {dist:>+7.1f}%  ${mid:.2f}  {bar}")

print(f"\n完成。\n")
