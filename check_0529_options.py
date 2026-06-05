"""
NVDA 5/29 周五到期期权流全扫描
今日到期 + 下周结构预览
运行: python3 check_0529_options.py
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, date

import time, sys

# ── 现价：命令行传入 或 手动输入 ──────────────────────────
if len(sys.argv) > 1:
    spot = float(sys.argv[1])
else:
    try:
        spot = float(input("请输入 NVDA 当前股价（如 135.50）: ").strip())
    except Exception:
        spot = 0.0

now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

ticker = yf.Ticker("NVDA")
time.sleep(2)
print(f"\nNVDA 现价: ${spot:.2f}  ({now} 本地时间)")
print(f"财报日: 5/20  EPS $1.87(+140%)  营收 $81.6B(+85%)  Q2指引 $91B")
if spot <= 0:
    print("⚠️  未输入有效股价，部分距现价计算将不准确")
    spot = 135.0  # 占位

time.sleep(2)
opts = ticker.options
print(f"\n可用到期日: {opts[:10]}")

# 找 5/29
target = date(2026, 5, 29)
exp_today = min(opts, key=lambda x: abs((datetime.strptime(x, "%Y-%m-%d").date() - target).days))
print(f"今日到期: {exp_today}")

time.sleep(2)
chain = ticker.option_chain(exp_today)
calls = chain.calls.copy()
puts  = chain.puts.copy()

c_vol = calls.volume.fillna(0).sum()
p_vol = puts.volume.fillna(0).sum()
total = c_vol + p_vol
c_oi  = calls.openInterest.fillna(0).sum()
p_oi  = puts.openInterest.fillna(0).sum()

print(f"\n{'='*65}")
print(f"  {exp_today} 今日到期 总体")
print(f"{'='*65}")
print(f"  Call Vol : {c_vol:>10,.0f}  ({c_vol/total*100:.1f}%)")
print(f"  Put  Vol : {p_vol:>10,.0f}  ({p_vol/total*100:.1f}%)")
print(f"  Call OI  : {c_oi:>10,.0f}")
print(f"  Put  OI  : {p_oi:>10,.0f}  P/C OI={p_oi/c_oi:.3f}")

# ── Max Pain ──────────────────────────────────────────────
print(f"\n  计算 Max Pain...")
all_strikes = sorted(set(calls.strike) | set(puts.strike))
pain = {}
for s in all_strikes:
    cp = calls[calls.strike > s].apply(lambda r: (r.strike - s)*r.openInterest, axis=1).sum()
    pp = puts[puts.strike < s].apply(lambda r: (s - r.strike)*r.openInterest, axis=1).sum()
    pain[s] = cp + pp
mp = min(pain, key=pain.get)
pain_top5 = sorted(pain.items(), key=lambda x: x[1])[:5]
print(f"  Max Pain: ${mp:.0f}  (距现价 {(mp-spot)/spot*100:+.1f}%)")
print(f"  引力带: {[f'${s:.0f}' for s,_ in pain_top5]}")

# ── ATM Straddle ──────────────────────────────────────────
atm = min(calls.strike, key=lambda x: abs(x-spot))
c_r = calls[calls.strike==atm].iloc[0]
p_r = puts[puts.strike==atm].iloc[0]
c_mid = (c_r.bid+c_r.ask)/2 if c_r.bid > 0 else c_r.lastPrice
p_mid = (p_r.bid+p_r.ask)/2 if p_r.bid > 0 else p_r.lastPrice
straddle = c_mid + p_mid
move_pct  = straddle/spot*100
print(f"\n  ATM ${atm:.0f} Straddle: ${straddle:.2f}  ±{move_pct:.1f}%")
print(f"  今日剩余波动区间: ${spot-straddle:.1f} ~ ${spot+straddle:.1f}")

# ── 近ATM Call ────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  今日到期 近ATM Call (±$30)")
print(f"{'='*65}")
print(f"  {'Strike':>7}  {'OI':>9}  {'Vol':>9}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>8}  {'状态'}")
fc = calls[(calls.strike>=spot-30)&(calls.strike<=spot+35)].sort_values('strike')
for _, r in fc.iterrows():
    mid  = (r.bid+r.ask)/2 if r.bid>0 else r.lastPrice
    voi  = r.volume/r.openInterest if r.openInterest>0 else 0
    dist = (r.strike-spot)/spot*100
    if mid < 0.03:
        status = "💀归零"
    elif mid < 0.15:
        status = "🟡临界"
    elif dist < -2:
        status = "💰深ITM"
    elif abs(dist) <= 2:
        status = "⚡ATM"
    else:
        status = "🎯OTM"
    flag = " ← 异常" if voi > 2 and r.volume > 3000 else ""
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>9,.0f}  {r.volume:>9,.0f}  {voi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+7.1f}%  {status}{flag}")

# ── 近ATM Put ─────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  今日到期 近ATM Put (±$30)")
print(f"{'='*65}")
print(f"  {'Strike':>7}  {'OI':>9}  {'Vol':>9}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>8}  {'状态'}")
fp = puts[(puts.strike>=spot-30)&(puts.strike<=spot+15)].sort_values('strike', ascending=False)
for _, r in fp.iterrows():
    mid  = (r.bid+r.ask)/2 if r.bid>0 else r.lastPrice
    voi  = r.volume/r.openInterest if r.openInterest>0 else 0
    dist = (r.strike-spot)/spot*100
    if mid < 0.03:
        status = "💀归零"
    elif mid < 0.15:
        status = "🟡临界"
    elif dist > 2:
        status = "💰深ITM"
    elif abs(dist) <= 2:
        status = "⚡ATM"
    else:
        status = "🎯OTM"
    flag = " ← 异常" if voi > 2 and r.volume > 3000 else ""
    print(f"  ${r.strike:>6.0f}  {r.openInterest:>9,.0f}  {r.volume:>9,.0f}  {voi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+7.1f}%  {status}{flag}")

# ── 全链异常成交 ──────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  今日异常成交（V/OI > 2x 且 Vol > 3,000）")
print(f"{'='*65}")
comb = pd.concat([calls.assign(type='C'), puts.assign(type='P')])
comb['vol_oi'] = comb.volume / comb.openInterest.replace(0, np.nan)
anom = comb[(comb.vol_oi>2)&(comb.volume>3000)&(comb.strike>5)].sort_values('volume', ascending=False)
if len(anom)==0:
    print("  暂无异常成交")
else:
    print(f"  {'型':>3}  {'Strike':>7}  {'Vol':>9}  {'OI':>9}  {'V/OI':>5}  {'Mid':>7}  {'距现价':>7}  {'名义$M':>8}")
    for _, r in anom.head(12).iterrows():
        mid  = (r.bid+r.ask)/2 if r.bid>0 else r.lastPrice
        dist = (r.strike-spot)/spot*100
        nom  = mid*r.volume*100/1e6
        print(f"  {r['type']:>3}  ${r.strike:>6.0f}  {r.volume:>9,.0f}  {r.openInterest:>9,.0f}  {r.vol_oi:>4.1f}x  ${mid:>6.2f}  {dist:>+6.1f}%  ${nom:>7.2f}M")

# ── 今日最大OI ────────────────────────────────────────────
print(f"\n{'='*65}")
print(f"  今日到期 Top OI Call（引力确认）")
print(f"{'='*65}")
top_c = calls[calls.openInterest>5000].sort_values('openInterest', ascending=False).head(10)
for _, r in top_c.iterrows():
    mid  = (r.bid+r.ask)/2 if r.bid>0 else r.lastPrice
    dist = (r.strike-spot)/spot*100
    worth = "💀" if mid < 0.05 else "💰" if mid > 1 else "🟡"
    print(f"  ${r.strike:>6.0f}  OI={r.openInterest:>8,.0f}  Vol={r.volume:>8,.0f}  mid=${mid:.2f}  {dist:>+.1f}%  {worth}")

print(f"\n  今日到期 Top OI Put（支撑确认）")
top_p = puts[(puts.openInterest>5000)&(puts.strike>spot*0.85)].sort_values('openInterest', ascending=False).head(8)
for _, r in top_p.iterrows():
    mid  = (r.bid+r.ask)/2 if r.bid>0 else r.lastPrice
    dist = (r.strike-spot)/spot*100
    worth = "💀" if mid < 0.05 else "💰" if mid > 1 else "🟡"
    print(f"  ${r.strike:>6.0f}  OI={r.openInterest:>8,.0f}  Vol={r.volume:>8,.0f}  mid=${mid:.2f}  {dist:>+.1f}%  {worth}")

# ── 下周结构预览（6/05 + 6/12）────────────────────────────
print(f"\n{'='*65}")
print(f"  下周期权结构预览（6/5 + 6/12）SpaceX IPO前窗口")
print(f"{'='*65}")
for nxt in opts[1:5]:  # 跳过今天
    try:
        time.sleep(1)
        ch = ticker.option_chain(nxt)
        c  = ch.calls[ch.calls.strike>5]
        p  = ch.puts[ch.puts.strike>5]
        c_oi2 = c.openInterest.fillna(0).sum()
        p_oi2 = p.openInterest.fillna(0).sum()
        c_v2  = c.volume.fillna(0).sum()
        p_v2  = p.volume.fillna(0).sum()
        pc    = p_oi2/c_oi2 if c_oi2>0 else 0
        # top call OI
        tc = c.sort_values('openInterest', ascending=False).iloc[0] if len(c)>0 else None
        tp = p[p.strike>spot*0.85].sort_values('openInterest', ascending=False).iloc[0] if len(p[p.strike>spot*0.85])>0 else None
        tc_str = f"Call Wall ${tc.strike:.0f}(OI={tc.openInterest:,.0f})" if tc is not None else "—"
        tp_str = f"Put Wall ${tp.strike:.0f}(OI={tp.openInterest:,.0f})" if tp is not None else "—"

        # ATM straddle
        atm2 = min(c.strike, key=lambda x: abs(x-spot))
        cr2  = c[c.strike==atm2].iloc[0]
        pr2  = p[p.strike==atm2].iloc[0] if len(p[p.strike==atm2])>0 else None
        cm2  = (cr2.bid+cr2.ask)/2 if cr2.bid>0 else cr2.lastPrice
        pm2  = (pr2.bid+pr2.ask)/2 if pr2 is not None and pr2.bid>0 else (pr2.lastPrice if pr2 is not None else 0)
        strd2 = cm2 + pm2
        print(f"\n  [{nxt}]  P/C={pc:.2f}  Straddle=${strd2:.2f}(±{strd2/spot*100:.1f}%)")
        print(f"    {tc_str}")
        print(f"    {tp_str}")
        print(f"    Call Vol={c_v2:,.0f}  Put Vol={p_v2:,.0f}")
    except Exception as e:
        print(f"  [{nxt}] 获取失败")

print(f"\n完成。\n")
