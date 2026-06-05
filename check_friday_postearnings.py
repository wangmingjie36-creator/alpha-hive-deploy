"""
NVDA 财报后 5/22 期权流快照
对比盘中基准，看IV Crush程度 + 新磁吸位
运行: python3 check_friday_postearnings.py
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

ticker = yf.Ticker("NVDA")
spot   = ticker.fast_info.last_price
now    = datetime.now().strftime("%H:%M:%S")
print(f"\nNVDA 现价: ${spot:.2f}  ({datetime.now().strftime('%Y-%m-%d')} {now})")
print(f"财报前收盘: $224.76  财报后盘后: ~平盘")
price_chg = (spot - 224.76) / 224.76 * 100
print(f"vs 盘中高点: {price_chg:+.2f}%")

# ── 5/22 期权链 ───────────────────────────────────────────
exp = "2026-05-22"
chain = ticker.option_chain(exp)
calls = chain.calls.copy()
puts  = chain.puts.copy()

c_vol = calls.volume.fillna(0).sum()
p_vol = puts.volume.fillna(0).sum()
total = c_vol + p_vol

print(f"\n{'='*62}")
print(f"  5/22 期权总体（财报后）")
print(f"{'='*62}")
print(f"  Call Vol: {c_vol:>10,.0f}  ({c_vol/total*100:.1f}%)")
print(f"  Put  Vol: {p_vol:>10,.0f}  ({p_vol/total*100:.1f}%)")
print(f"  Call OI : {calls.openInterest.sum():>10,.0f}")
print(f"  Put  OI : {puts.openInterest.sum():>10,.0f}  P/C={puts.openInterest.sum()/calls.openInterest.sum():.3f}")

# ── Max Pain ──────────────────────────────────────────────
strikes = sorted(set(calls.strike) | set(puts.strike))
pain = {}
for s in strikes:
    cp = calls[calls.strike > s].apply(lambda r: (r.strike - s) * r.openInterest, axis=1).sum()
    pp = puts[puts.strike < s].apply(lambda r: (s - r.strike) * r.openInterest, axis=1).sum()
    pain[s] = cp + pp
mp = min(pain, key=pain.get)
print(f"\n  Max Pain: ${mp:.0f}  (距现价 {(mp-spot)/spot*100:+.1f}%)")
print(f"  盘中Max Pain: $235  (+4.6%)")

# ── ATM Straddle → IV Crush 测量 ─────────────────────────
atm = min(calls.strike, key=lambda x: abs(x-spot))
c_r = calls[calls.strike == atm].iloc[0]
p_r = puts[puts.strike == atm].iloc[0]
c_mid = (c_r.bid + c_r.ask)/2 if c_r.bid > 0 else c_r.lastPrice
p_mid = (p_r.bid + p_r.ask)/2 if p_r.bid > 0 else p_r.lastPrice
straddle = c_mid + p_mid
move_pct  = straddle / spot * 100
c_iv = c_r.impliedVolatility * 100
p_iv = p_r.impliedVolatility * 100

print(f"\n  ATM ${atm:.0f} Straddle: ${straddle:.2f}  ±{move_pct:.1f}%")
print(f"  IV: Call={c_iv:.1f}%  Put={p_iv:.1f}%")
print(f"  盘中 Straddle: $12.82  ±5.7%  IV≈79%")
crush = (c_iv - 79) / 79 * 100
print(f"  IV Crush: {crush:+.1f}%  ({'已发生' if crush < -10 else '尚未充分' if crush < 0 else '未崩'})")

# ── 关键行权价价格变化（IV Crush可视化）────────────────────
print(f"\n{'='*62}")
print(f"  关键行权价 现价 vs 盘中对比（IV Crush程度）")
print(f"{'='*62}")

# 盘中价格基准
INTRADAY_CALLS = {
    220: 9.03, 222: 7.62, 225: 6.25, 228: 5.30,
    230: 4.28, 232: 3.50, 235: 2.76, 240: 1.72,
    245: 1.10, 250: 0.66, 255: 0.41
}
INTRADAY_PUTS = {
    215: 2.54, 218: 3.30, 220: 4.20, 222: 5.35,
    225: 6.57, 228: 8.07, 230: 9.45
}

print(f"\n  Call 现价 vs 盘中（10:16）:")
print(f"  {'Strike':>7}  {'盘中':>8}  {'现价':>8}  {'变化':>8}  {'IV%':>6}  {'距现价':>8}")
focus_calls = calls[(calls.strike >= spot-15) & (calls.strike <= spot+35)].sort_values('strike')
for _, r in focus_calls.iterrows():
    s = r.strike
    mid_now  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    mid_base = INTRADAY_CALLS.get(s, None)
    dist = (s - spot)/spot*100
    if mid_base:
        chg = (mid_now - mid_base)/mid_base*100
        flag = " 🔴" if chg < -30 else " 🟡" if chg < -10 else " 🟢" if chg > 10 else ""
        print(f"  ${s:>6.0f}  ${mid_base:>7.2f}  ${mid_now:>7.2f}  {chg:>+7.1f}%  {r.impliedVolatility*100:>5.1f}%  {dist:>+7.1f}%{flag}")
    else:
        print(f"  ${s:>6.0f}  {'—':>8}  ${mid_now:>7.2f}  {'—':>8}  {r.impliedVolatility*100:>5.1f}%  {dist:>+7.1f}%")

print(f"\n  Put 现价 vs 盘中:")
print(f"  {'Strike':>7}  {'盘中':>8}  {'现价':>8}  {'变化':>8}  {'IV%':>6}  {'距现价':>8}")
focus_puts = puts[(puts.strike >= spot-20) & (puts.strike <= spot+10)].sort_values('strike', ascending=False)
for _, r in focus_puts.iterrows():
    s = r.strike
    mid_now  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    mid_base = INTRADAY_PUTS.get(s, None)
    dist = (s - spot)/spot*100
    if mid_base:
        chg = (mid_now - mid_base)/mid_base*100
        flag = " 🔴" if chg < -30 else " 🟡" if chg < -10 else " 🟢" if chg > 10 else ""
        print(f"  ${s:>6.0f}  ${mid_base:>7.2f}  ${mid_now:>7.2f}  {chg:>+7.1f}%  {r.impliedVolatility*100:>5.1f}%  {dist:>+7.1f}%{flag}")
    else:
        print(f"  ${s:>6.0f}  {'—':>8}  ${mid_now:>7.2f}  {'—':>8}  {r.impliedVolatility*100:>5.1f}%  {dist:>+7.1f}%")

# ── 财报后新异常流 ────────────────────────────────────────
print(f"\n{'='*62}")
print(f"  财报后新异常成交（V/OI > 2x 且 Vol > 3,000）")
print(f"{'='*62}")
all_c = calls.assign(type='C')
all_p = puts.assign(type='P')
comb  = pd.concat([all_c, all_p])
comb['vol_oi'] = comb.volume / comb.openInterest.replace(0, np.nan)
anom = comb[(comb.vol_oi > 2) & (comb.volume > 3000) & (comb.strike > 5)].sort_values('volume', ascending=False)
if len(anom) == 0:
    print("  暂无新异常大单")
else:
    print(f"  {'型':>3}  {'Strike':>7}  {'Vol':>8}  {'OI':>8}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>7}  {'名义$M':>8}")
    for _, r in anom.head(10).iterrows():
        mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
        dist = (r.strike - spot)/spot*100
        nom  = mid * r.volume * 100 / 1e6
        print(f"  {r['type']:>3}  ${r.strike:>6.0f}  {r.volume:>8,.0f}  {r.openInterest:>8,.0f}  {r.vol_oi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+6.1f}%  ${nom:>7.2f}M")

# ── 周五到期OI TOP（磁吸确认）────────────────────────────
print(f"\n{'='*62}")
print(f"  5/22 OI Top 10 Call（周五磁吸位最终确认）")
print(f"{'='*62}")
top_c = calls[calls.openInterest > 5000].sort_values('openInterest', ascending=False).head(10)
for _, r in top_c.iterrows():
    mid = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    # 判断是否还有价值
    worth = "💀归零" if (mid < 0.05 and dist > 3) else "⚡有价值" if mid > 0.5 else "🟡边缘"
    print(f"  ${r.strike:>6.0f}  OI={r.openInterest:>8,.0f}  mid=${mid:.2f}  {dist:>+.1f}%  IV={r.impliedVolatility*100:.0f}%  {worth}")

print(f"\n  5/22 OI Top 8 Put（支撑确认）")
top_p = puts[(puts.openInterest > 5000) & (puts.strike >= spot*0.88)].sort_values('openInterest', ascending=False).head(8)
for _, r in top_p.iterrows():
    mid = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    worth = "💀归零" if (mid < 0.05 and dist < -5) else "⚡有价值" if mid > 0.5 else "🟡边缘"
    print(f"  ${r.strike:>6.0f}  OI={r.openInterest:>8,.0f}  mid=${mid:.2f}  {dist:>+.1f}%  IV={r.impliedVolatility*100:.0f}%  {worth}")

print(f"\n完成。\n")
