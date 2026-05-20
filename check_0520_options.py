"""
NVDA 今日期权异常检测 + 5/22 磁吸位
运行: python3 check_0520_options.py
"""
import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime, date

ticker = yf.Ticker("NVDA")
spot = ticker.fast_info.last_price
print(f"\nNVDA 现价: ${spot:.2f}  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 本地时间)")

# 找 5/22 到期日
opts = ticker.options
print(f"可用到期日前8个: {opts[:8]}")
target = date(2026, 5, 22)
exp = min(opts, key=lambda x: abs((datetime.strptime(x, "%Y-%m-%d").date() - target).days))
print(f"使用到期日: {exp}\n")

chain = ticker.option_chain(exp)
calls = chain.calls.copy()
puts  = chain.puts.copy()

# ── 整体统计 ──────────────────────────────────────────────
total_c_vol = calls.volume.fillna(0).sum()
total_p_vol = puts.volume.fillna(0).sum()
total_vol   = total_c_vol + total_p_vol
total_c_oi  = calls.openInterest.fillna(0).sum()
total_p_oi  = puts.openInterest.fillna(0).sum()

print("=" * 60)
print(f"  {exp} 期权总体")
print("=" * 60)
print(f"  Call成交: {total_c_vol:,.0f}  ({total_c_vol/total_vol*100:.1f}%)")
print(f"  Put 成交: {total_p_vol:,.0f}  ({total_p_vol/total_vol*100:.1f}%)")
print(f"  Call OI : {total_c_oi:,.0f}")
print(f"  Put  OI : {total_p_oi:,.0f}  P/C={total_p_oi/total_c_oi:.3f}")

# ── Max Pain ──────────────────────────────────────────────
all_strikes = sorted(set(calls.strike) | set(puts.strike))
pain = {}
for s in all_strikes:
    cp = calls[calls.strike > s].apply(lambda r: (r.strike - s) * r.openInterest, axis=1).sum()
    pp = puts[puts.strike < s].apply(lambda r: (s - r.strike) * r.openInterest, axis=1).sum()
    pain[s] = cp + pp
mp = min(pain, key=pain.get)
print(f"\n  Max Pain: ${mp:.0f}  (距现价 {(mp-spot)/spot*100:+.1f}%)")

# ── ATM Straddle ──────────────────────────────────────────
atm = min(calls.strike, key=lambda x: abs(x - spot))
c_row = calls[calls.strike == atm].iloc[0]
p_row = puts[puts.strike == atm].iloc[0]
c_mid = (c_row.bid + c_row.ask)/2 if c_row.bid > 0 else c_row.lastPrice
p_mid = (p_row.bid + p_row.ask)/2 if p_row.bid > 0 else p_row.lastPrice
straddle = c_mid + p_mid
move_pct  = straddle / spot * 100
print(f"  ATM ${atm:.0f} Straddle = ${straddle:.2f}  ±{move_pct:.1f}%")
print(f"  隐含范围: ${spot-straddle:.1f} ~ ${spot+straddle:.1f}")

# ── 近ATM Call (±40) ─────────────────────────────────────
print(f"\n{'='*60}")
print(f"  近ATM Call (${spot-40:.0f}-${spot+45:.0f})")
print(f"{'='*60}")
print(f"  {'Strike':>7}  {'OI':>8}  {'Volume':>8}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>7}")
calls_near = calls[(calls.strike >= spot-40) & (calls.strike <= spot+45)].sort_values('strike')
for _, r in calls_near.iterrows():
    mid   = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    voi   = r.volume / r.openInterest if r.openInterest > 0 else 0
    dist  = (r.strike - spot)/spot*100
    flag  = "  ⚡异常" if voi > 1.5 and r.volume > 3000 else ""
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>8,.0f}  {r.volume:>8,.0f}  {voi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+6.1f}%{flag}")

# ── 近ATM Put ─────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  近ATM Put (${spot-40:.0f}-${spot+30:.0f})")
print(f"{'='*60}")
print(f"  {'Strike':>7}  {'OI':>8}  {'Volume':>8}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>7}")
puts_near = puts[(puts.strike >= spot-40) & (puts.strike <= spot+30)].sort_values('strike', ascending=False)
for _, r in puts_near.iterrows():
    mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    voi  = r.volume / r.openInterest if r.openInterest > 0 else 0
    dist = (r.strike - spot)/spot*100
    flag = "  ⚡异常" if voi > 1.5 and r.volume > 3000 else ""
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>8,.0f}  {r.volume:>8,.0f}  {voi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+6.1f}%{flag}")

# ── 全链异常成交（所有到期，今日V/OI异常）────────────────
print(f"\n{'='*60}")
print(f"  全链异常成交（V/OI > 2x 且 Volume > 8,000）")
print(f"{'='*60}")
all_calls, all_puts = [], []
for e in opts[:6]:  # 前6个到期日
    try:
        ch = ticker.option_chain(e)
        c = ch.calls.copy(); c['expiry'] = e; c['type'] = 'C'
        p = ch.puts.copy();  p['expiry'] = e; p['type'] = 'P'
        all_calls.append(c); all_puts.append(p)
    except:
        pass

combined = pd.concat(all_calls + all_puts, ignore_index=True)
combined['vol_oi'] = combined.volume / combined.openInterest.replace(0, np.nan)
anom = combined[
    (combined.vol_oi > 2) &
    (combined.volume > 8000) &
    (combined.strike > 5)         # 过滤$0伪影
].sort_values('volume', ascending=False).head(15)

if len(anom) == 0:
    print("  今日无明显异常大单")
else:
    print(f"  {'类型':>4}  {'Strike':>7}  {'到期':>10}  {'Volume':>8}  {'OI':>8}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>7}  {'名义($M)':>9}")
    for _, r in anom.iterrows():
        mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
        dist = (r.strike - spot)/spot*100
        nom  = mid * r.volume * 100 / 1e6
        print(f"  {r['type']:>4}  ${r.strike:>6.0f}  {r.expiry:>10}  {r.volume:>8,.0f}  {r.openInterest:>8,.0f}  {r.vol_oi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+6.1f}%  ${nom:>8.2f}M")

# ── OI最大的Call/Put（全5/22）───────────────────────────
print(f"\n{'='*60}")
print(f"  {exp} Top OI Call（5/22磁吸确认）")
print(f"{'='*60}")
top_c = calls[calls.openInterest > 10000].sort_values('openInterest', ascending=False).head(10)
for _, r in top_c.iterrows():
    mid = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    print(f"  ${r.strike:>6.0f}  OI={r.openInterest:>8,.0f}  Vol={r.volume:>8,.0f}  mid=${mid:.2f}  {dist:+.1f}%")

print(f"\n  {exp} Top OI Put")
top_p = puts[puts.openInterest > 10000].sort_values('openInterest', ascending=False).head(8)
for _, r in top_p.iterrows():
    mid = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
    dist = (r.strike - spot)/spot*100
    print(f"  ${r.strike:>6.0f}  OI={r.openInterest:>8,.0f}  Vol={r.volume:>8,.0f}  mid=${mid:.2f}  {dist:+.1f}%")

print("\n完成。\n")
