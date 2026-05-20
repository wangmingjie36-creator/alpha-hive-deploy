"""
NVDA 盘中期权实时快照 + 与今早对比
运行: python3 check_intraday.py
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

# ── 今早的基准数据（07:09 UTC）────────────────────────────
BASELINE = {
    "spot": 222.98,
    "time": "07:09",
    # 5/22 call OI 基准（strike: oi）
    "call_oi": {
        225: 42059, 230: 53113, 235: 60176, 240: 74066,
        250: 93839, 220: 34417, 222: 12697, 218: 9206,
        215: 40224, 238: 31221, 245: 28449,
    },
    # 5/22 put OI 基准
    "put_oi": {
        200: 32520, 205: 24397, 210: 24103, 215: 18659,
        220: 18396, 225: 10261, 208: 11635, 212: 11667,
    },
    # 5/22 call volume 基准（早上快照）
    "call_vol": {
        225: 63108, 222: 35890, 220: 42986, 230: 60170,
        235: 54720, 240: 50412,
    }
}

ticker = yf.Ticker("NVDA")
spot = ticker.fast_info.last_price
now  = datetime.now().strftime("%H:%M:%S")
print(f"\nNVDA 现价: ${spot:.2f}  ({datetime.now().strftime('%Y-%m-%d')} {now} 本地时间)")
print(f"今早基准:  ${BASELINE['spot']:.2f}  ({BASELINE['time']})")
price_chg = (spot - BASELINE['spot']) / BASELINE['spot'] * 100
print(f"盘中变化:  {price_chg:+.2f}%  (${spot - BASELINE['spot']:+.2f})")

# ── 5/22 实时数据 ─────────────────────────────────────────
opts = ticker.options
exp = "2026-05-22"
chain = ticker.option_chain(exp)
calls = chain.calls.copy()
puts  = chain.puts.copy()

total_c_vol = calls.volume.fillna(0).sum()
total_p_vol = puts.volume.fillna(0).sum()
total_vol   = total_c_vol + total_p_vol

print(f"\n{'='*65}")
print(f"  5/22 实时总量  {datetime.now().strftime('%H:%M')}")
print(f"{'='*65}")
print(f"  Call成交: {total_c_vol:>10,.0f}  ({total_c_vol/total_vol*100:.1f}%)")
print(f"  Put 成交: {total_p_vol:>10,.0f}  ({total_p_vol/total_vol*100:.1f}%)")
print(f"  Call OI : {calls.openInterest.sum():>10,.0f}")
print(f"  Put  OI : {puts.openInterest.sum():>10,.0f}  P/C={puts.openInterest.sum()/calls.openInterest.sum():.3f}")

# ── Max Pain 当前 ─────────────────────────────────────────
all_strikes = sorted(set(calls.strike) | set(puts.strike))
pain = {}
for s in all_strikes:
    cp = calls[calls.strike > s].apply(lambda r: (r.strike - s) * r.openInterest, axis=1).sum()
    pp = puts[puts.strike < s].apply(lambda r: (s - r.strike) * r.openInterest, axis=1).sum()
    pain[s] = cp + pp
mp = min(pain, key=pain.get)
print(f"\n  Max Pain 当前: ${mp:.0f}  (距现价 {(mp-spot)/spot*100:+.1f}%)")
print(f"  今早 Max Pain: $235  （+5.9%）")

# ── ATM Straddle ──────────────────────────────────────────
atm = min(calls.strike, key=lambda x: abs(x - spot))
c_row = calls[calls.strike == atm].iloc[0]
p_row = puts[puts.strike == atm].iloc[0]
c_mid = (c_row.bid + c_row.ask)/2 if c_row.bid > 0 else c_row.lastPrice
p_mid = (p_row.bid + p_row.ask)/2 if p_row.bid > 0 else p_row.lastPrice
straddle = c_mid + p_mid
move_pct  = straddle / spot * 100
print(f"\n  ATM ${atm:.0f} Straddle: ${straddle:.2f}  ±{move_pct:.1f}%")
print(f"  隐含范围: ${spot-straddle:.1f} ~ ${spot+straddle:.1f}")
print(f"  今早 Straddle: $13.77  ±6.2%")

# ── OI变化（vs今早基准）─────────────────────────────────
print(f"\n{'='*65}")
print(f"  5/22 Call OI 变化（vs 今早 {BASELINE['time']}）")
print(f"{'='*65}")
print(f"  {'Strike':>7}  {'OI早':>9}  {'OI现':>9}  {'变化':>9}  {'Vol':>9}  {'IV%':>6}  {'Mid':>7}")

# focus near ATM
focus_calls = calls[(calls.strike >= spot - 30) & (calls.strike <= spot + 35)].sort_values('strike')
for _, r in focus_calls.iterrows():
    s = r.strike
    oi_now  = int(r.openInterest) if not pd.isna(r.openInterest) else 0
    oi_base = BASELINE['call_oi'].get(s, None)
    mid = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice

    if oi_base is not None:
        delta = oi_now - oi_base
        delta_str = f"{delta:>+9,.0f}"
        flag = " ⬆️" if delta > 2000 else " ⬇️" if delta < -2000 else ""
    else:
        delta_str = f"{'(新)':>9}"
        flag = ""

    vol = int(r.volume) if not pd.isna(r.volume) else 0
    dist = (s - spot)/spot*100
    print(f"  ${s:>6.0f}  {oi_base if oi_base else '—':>9}  {oi_now:>9,.0f}  {delta_str}  {vol:>9,.0f}  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+.1f}%{flag}")

print(f"\n  5/22 Put OI 变化")
print(f"  {'Strike':>7}  {'OI早':>9}  {'OI现':>9}  {'变化':>9}  {'Vol':>9}  {'IV%':>6}  {'Mid':>7}")
focus_puts = puts[(puts.strike >= spot - 30) & (puts.strike <= spot + 20)].sort_values('strike', ascending=False)
for _, r in focus_puts.iterrows():
    s = r.strike
    oi_now  = int(r.openInterest) if not pd.isna(r.openInterest) else 0
    oi_base = BASELINE['put_oi'].get(s, None)
    mid = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice

    if oi_base is not None:
        delta = oi_now - oi_base
        delta_str = f"{delta:>+9,.0f}"
        flag = " ⬆️" if delta > 1000 else " ⬇️" if delta < -1000 else ""
    else:
        delta_str = f"{'(新)':>9}"
        flag = ""

    vol = int(r.volume) if not pd.isna(r.volume) else 0
    dist = (s - spot)/spot*100
    print(f"  ${s:>6.0f}  {oi_base if oi_base else '—':>9}  {oi_now:>9,.0f}  {delta_str}  {vol:>9,.0f}  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+.1f}%{flag}")

# ── 盘中异常成交（今日到目前为止）────────────────────────
print(f"\n{'='*65}")
print(f"  盘中新增异常成交（V/OI > 1.5x 且 Volume > 5,000）")
print(f"{'='*65}")
all_c = calls.copy(); all_c['type'] = 'C'
all_p = puts.copy();  all_p['type'] = 'P'
combined = pd.concat([all_c, all_p])
combined['vol_oi'] = combined.volume / combined.openInterest.replace(0, np.nan)
anom = combined[
    (combined.vol_oi > 1.5) &
    (combined.volume > 5000) &
    (combined.strike > 5)
].sort_values('volume', ascending=False)

if len(anom) == 0:
    print("  暂无新增异常成交")
else:
    print(f"  {'型':>3}  {'Strike':>7}  {'Volume':>9}  {'OI':>9}  {'V/OI':>5}  {'IV%':>6}  {'Mid':>7}  {'距现价':>8}  {'名义$M':>8}")
    for _, r in anom.head(12).iterrows():
        mid  = (r.bid + r.ask)/2 if r.bid > 0 else r.lastPrice
        dist = (r.strike - spot)/spot*100
        nom  = mid * r.volume * 100 / 1e6
        print(f"  {r['type']:>3}  ${r.strike:>6.0f}  {r.volume:>9,.0f}  {r.openInterest:>9,.0f}  {r.vol_oi:>4.1f}x  {r.impliedVolatility*100:>5.1f}%  ${mid:>6.2f}  {dist:>+7.1f}%  ${nom:>7.2f}M")

# ── IV 变化监测（gamma/vega关键位）───────────────────────
print(f"\n{'='*65}")
print(f"  关键行权价 IV 快照（财报前 IV 变化监测）")
print(f"{'='*65}")
key_strikes = [215, 220, 222, 225, 230, 235, 240]
print(f"  {'Strike':>7}  {'Call IV':>8}  {'Call Mid':>9}  {'Put IV':>8}  {'Put Mid':>9}  {'Skew':>7}")
for s in key_strikes:
    c_r = calls[calls.strike == s]
    p_r = puts[puts.strike == s]
    if len(c_r) > 0 and len(p_r) > 0:
        c = c_r.iloc[0]
        p = p_r.iloc[0]
        c_mid = (c.bid + c.ask)/2 if c.bid > 0 else c.lastPrice
        p_mid = (p.bid + p.ask)/2 if p.bid > 0 else p.lastPrice
        c_iv = c.impliedVolatility*100
        p_iv = p.impliedVolatility*100
        skew = p_iv - c_iv
        print(f"  ${s:>6.0f}  {c_iv:>7.1f}%  ${c_mid:>8.2f}  {p_iv:>7.1f}%  ${p_mid:>8.2f}  {skew:>+6.1f}%")

# ── 财报隐含幅度 vs 历史 ──────────────────────────────────
print(f"\n{'='*65}")
print(f"  财报隐含幅度汇总")
print(f"{'='*65}")
print(f"  当前 ATM Straddle: ${straddle:.2f}  →  ±{move_pct:.1f}%")
print(f"  上方突破目标:      ${spot + straddle:.1f}")
print(f"  下方跌破目标:      ${spot - straddle:.1f}")
print(f"  NVDA 历史财报单日振幅: ~±8-12%（平均±9.6%）")
if move_pct > 9.6:
    print(f"  当前定价 {move_pct:.1f}% > 历史均值，期权偏贵")
elif move_pct < 7:
    print(f"  当前定价 {move_pct:.1f}% < 历史均值，期权偏便宜")
else:
    print(f"  当前定价在历史合理区间内")

print(f"\n完成。\n")
