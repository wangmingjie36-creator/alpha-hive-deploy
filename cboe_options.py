#!/usr/bin/env python3
"""
CBOE 延迟报价期权链获取器 — yfinance 限流/不可用时的真实数据降级源

数据源：CBOE（芝加哥期权交易所）公开延迟报价 JSON
    https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json
特点：全链逐合约 OI/IV/greeks，15 分钟延迟（盘后=已结算 EOD），无 API key、无限流。

产出格式与 options_analyzer.OptionsAgent.fetch_options_chain 完全一致：
    {ticker, timestamp, calls:[...], puts:[...], expirations:[...], near_expiry_set:[...]}
每个 call/put 记录键：strike / openInterest / impliedVolatility / gamma / expiry /
    dte / dte_weight / bid / ask / volume / lastPrice / contractSymbol。
失败一律返回 None（调用方据此再降级到样本数据）。

设计原则：纯 urllib（零额外依赖）、镜像 yfinance 路径的后处理（到期日筛选 / ATM 过滤 /
40-strike 上限 / DTE 加权 / gamma 注入），保证下游 GEX / Max Pain / OI 墙零改动复用。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.cboe_options")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.cboe_options")

_CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
# OCC 合约符号：NVDA 260702 C 00200000 → 标的 / YYMMDD / C|P / 8 位行权价(×1000)
_OCC = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_BS_RISK_FREE = 0.045  # 与 options_analyzer 一致的参考无风险利率
_ATM_LO, _ATM_HI = 0.30, 1.70  # ATM 过滤区间（±70%），与 yfinance 路径一致
_MAX_STRIKES_PER_SIDE = 40      # 每到期日每边最多保留 40 strike（按 OI），内存保护

# 本机老 SSL 栈（LibreSSL 2.8.3）扛不住并发 HTTPS：实测 4 并发拉 CBOE 每个挂 50-70s
# 甚至 SSL EOF，而顺序拉仅 8-11s。故串行化 CBOE 网络请求（信号量限 1）。
_CBOE_SEM = threading.Semaphore(1)
# 进程内 payload 缓存：同一标的的主链(fetch_cboe_chain)与全链(fetch_cboe_full_chain_oi)
# 共享一次下载，避免每标的拉 2 次大 JSON。短 TTL 防长驻进程取到陈旧数据。
_payload_cache = {}  # ticker -> (timestamp, data)
_cache_lock = threading.Lock()
_CACHE_TTL = 120.0


def _bs_gamma(S: float, K: float, T: float, sigma: float) -> float:
    """Black-Scholes gamma — CBOE gamma 为 0（深 ITM/低流动）时兜底，与 options_analyzer 同公式"""
    if S <= 0 or K <= 0 or T <= 1e-6 or sigma < 0.01:
        return 0.0
    try:
        d1 = (math.log(S / K) + (_BS_RISK_FREE + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _parse_occ(sym: str) -> Optional[tuple]:
    """OCC 符号 → (expiry 'YYYY-MM-DD', 'C'|'P', strike float)；非法返回 None"""
    m = _OCC.match(sym or "")
    if not m:
        return None
    _tk, yy, mm, dd, cp, strike = m.groups()
    try:
        expiry = f"20{yy}-{mm}-{dd}"
        # 校验是合法日期
        datetime.strptime(expiry, "%Y-%m-%d")
        return expiry, cp, int(strike) / 1000.0
    except ValueError:
        return None


def _pdt_now() -> datetime:
    """PDT 锚定的 naive datetime。项目硬规则：绝不用裸 datetime.now()——用户本机钟
    可偏移 ~15h（UTC 绝对时间但时区 Asia/Shanghai），裸 now() 会把 DTE 算错 ±1 天、
    误判近月/远月。锚 America/Los_Angeles 得正确美股日期。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
    except Exception:  # pragma: no cover - zoneinfo 不可用时退裸 now
        return datetime.now()


def _select_expiries(by_expiry: Dict[str, dict], today: datetime, max_expiries: int = 4):
    """镜像 yfinance 路径：DTE≥3，优先 DTE≥7 前 4 + DTE 3-6 前 2，封顶 max_expiries。
    返回 (选中到期日列表, near_expiry_set=DTE<7)。"""
    dte_pairs = []
    for e in sorted(by_expiry.keys()):
        try:
            dte = (datetime.strptime(e, "%Y-%m-%d") - today).days
        except ValueError:
            continue
        if dte >= 3:
            dte_pairs.append((e, dte))
    far = [e for e, d in dte_pairs if d >= 7][:4]
    near = [e for e, d in dte_pairs if 3 <= d < 7][:2]
    chosen = (far + near)[:max_expiries] if far else [e for e, _ in dte_pairs[:max_expiries]]
    near_set = {e for e, d in dte_pairs if d < 7}
    return chosen, list(near_set)


def _fetch_cboe_payload(ticker: str, timeout: int, *, retries: int = 3) -> Optional[dict]:
    """拉取 CBOE 延迟报价 JSON，返回 data 段（含 options / current_price / close）；失败返回 None。

    串行化（`_CBOE_SEM` 限 1）：本机老 SSL 栈扛不住并发 HTTPS（实测 4 并发挂 50-70s/SSL EOF），
    顺序拉仅 8-11s。进程缓存：同标的主链+全链共享一次下载。重试退避：瞬时 SSL EOF 错开即恢复。
    """
    key = ticker.upper()
    now = time.time()
    with _cache_lock:
        hit = _payload_cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    url = _CBOE_URL.format(key)
    last_err = None
    for attempt in range(retries):
        try:
            with _CBOE_SEM:  # 串行化：避免并发压垮本机 SSL 栈
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=timeout).read()
            data = (json.loads(raw) or {}).get("data") or {}
            if not data.get("options"):
                _log.warning("CBOE %s 返回空期权链", ticker)
                return None
            with _cache_lock:
                _payload_cache[key] = (now, data)
            return data
        except Exception as e:  # 网络/SSL/解析失败 → 退避重试
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.7 * (attempt + 1))
    _log.warning("CBOE 拉取 %s 失败（重试 %d 次耗尽）：%s", ticker, retries, last_err)
    return None


def fetch_cboe_chain(
    ticker: str,
    stock_price: float = 0.0,
    *,
    timeout: int = 15,
    max_expiries: int = 4,
) -> Optional[Dict]:
    """拉取并解析 CBOE 期权链 → options_analyzer 兼容 result dict；任何失败返回 None。"""
    data = _fetch_cboe_payload(ticker, timeout)
    if not data:
        return None
    options = data["options"]

    # 现价：优先入参 → CBOE current_price → close
    S = float(stock_price or 0.0) or float(data.get("current_price") or data.get("close") or 0.0)

    # ── 解析全部合约，按到期日分组 ─────────────────────────────
    # CBOE 每合约 iv 已是小数（实测 ATM ~0.18-0.34，与 yfinance impliedVolatility 同尺度），
    # 直接用；深 ITM/低流动合约 iv=0 由下方 BS gamma 兜底。不做百分数/小数启发式检测，
    # 避免对高 IV 标的（biotech 催化剂期 >300%）误判压缩。
    by_expiry: Dict[str, dict] = {}
    dropped = 0
    for o in options:
        parsed = _parse_occ(o.get("option", ""))
        if not parsed:
            dropped += 1
            continue
        expiry, cp, strike = parsed
        bucket = by_expiry.setdefault(expiry, {"C": [], "P": []})
        bucket[cp].append({
            "strike": strike,
            "openInterest": float(o.get("open_interest") or 0.0),
            "impliedVolatility": float(o.get("iv") or 0.0),
            "_cboe_gamma": float(o.get("gamma") or 0.0),
            "bid": float(o.get("bid") or 0.0),
            "ask": float(o.get("ask") or 0.0),
            "volume": float(o.get("volume") or 0.0),
            "lastPrice": float(o.get("last_trade_price") or 0.0),
            "contractSymbol": o.get("option", ""),
        })

    # 解析丢弃率告警：CBOE 格式变更 / 调整后符号（如拆股 AAPL1）会令 OCC 正则失配，
    # 静默跳过在多标的批量里不可见 → 丢弃 >5% 时告警，及时暴露格式回归。
    if options and dropped > len(options) * 0.05:
        _log.warning("CBOE %s：%d/%d 合约 OCC 符号解析失败（疑格式变更）", ticker, dropped, len(options))

    if not by_expiry:
        _log.warning("CBOE %s 无可解析合约", ticker)
        return None

    today = _pdt_now()
    expirations, near_expiry_set = _select_expiries(by_expiry, today, max_expiries)
    if not expirations:
        return None

    def _finalize_side(rows: List[dict], expiry: str) -> List[dict]:
        """单到期日单边：ATM 过滤 → 40-cap → DTE/gamma 注入。"""
        # ATM 过滤（与 yfinance 路径一致，仅当有现价时）
        if S > 0:
            rows = [r for r in rows if _ATM_LO * S <= r["strike"] <= _ATM_HI * S]
        # 每边最多 40 strike（按 OI 降序）
        rows = sorted(rows, key=lambda r: r["openInterest"], reverse=True)[:_MAX_STRIKES_PER_SIDE]
        try:
            dte = max(1, (datetime.strptime(expiry, "%Y-%m-%d") - today).days)
        except ValueError:
            dte = 30
        for r in rows:
            r["expiry"] = expiry
            r["dte"] = dte
            # gamma：优先 CBOE，缺失（0）时 BS 兜底
            g = r.pop("_cboe_gamma", 0.0)
            if not g:
                T = max(dte, 0.5) / 365.0
                g = _bs_gamma(S, r["strike"], T, r["impliedVolatility"])
            r["gamma"] = g
        return rows

    calls: List[dict] = []
    puts: List[dict] = []
    for e in expirations:
        b = by_expiry.get(e, {"C": [], "P": []})
        calls.extend(_finalize_side(list(b["C"]), e))
        puts.extend(_finalize_side(list(b["P"]), e))

    if not calls and not puts:
        return None

    # DTE 加权（1/sqrt(DTE) 归一化，与 yfinance 路径一致），跨整个 calls/puts
    def _apply_dte_weight(rows: List[dict]):
        if not rows:
            return
        raw_w = [1.0 / (r["dte"] ** 0.5) for r in rows]
        max_w = max(raw_w) if raw_w else 1.0
        for r, w in zip(rows, raw_w):
            r["dte_weight"] = w / max_w

    _apply_dte_weight(calls)
    _apply_dte_weight(puts)

    total_oi = sum(r["openInterest"] for r in calls) + sum(r["openInterest"] for r in puts)
    _log.info(
        "CBOE %s 期权链：%d 到期日，%d calls + %d puts，总 OI %s，现价 $%.2f",
        ticker, len(expirations), len(calls), len(puts), f"{int(total_oi):,}", S,
    )

    return {
        "ticker": ticker,
        "timestamp": _pdt_now().isoformat(),
        "calls": calls,
        "puts": puts,
        "expirations": expirations,
        "near_expiry_set": near_expiry_set,
        "_source": "cboe",  # 数据来源标记，供下游/调试识别
    }


def fetch_cboe_full_chain_oi(
    ticker: str,
    stock_price: float,
    max_expirations: int = 24,
    *,
    timeout: int = 15,
) -> Optional[Dict]:
    """全链 OI 聚合（供 options_analyzer._fetch_full_chain_oi 限流兜底）。

    返回与该方法 yfinance 路径相同的中间结构，复用其 Max Pain / OI 墙计算（零重复）：
        {call_oi:{strike:oi}, put_oi:{strike:oi},
         call_exp_oi:{strike:{exp:oi}}, put_exp_oi:{strike:{exp:oi}},
         expiry_breakdown:[{expiry,call_oi,put_oi,total}], used_exps:int}
    行权价过滤区间 [0.60×S, 1.45×S]，与 yfinance 路径一致。失败返回 None。
    """
    if not stock_price or stock_price <= 0:
        return None
    data = _fetch_cboe_payload(ticker, timeout)
    if not data:
        return None
    lo, hi = stock_price * 0.60, stock_price * 1.45

    # 按到期日分组（仅 strike + OI，过滤价格区间 + 正 OI）
    by_exp: Dict[str, Dict[str, Dict[float, int]]] = {}
    for o in data["options"]:
        parsed = _parse_occ(o.get("option", ""))
        if not parsed:
            continue
        expiry, cp, strike = parsed
        if not (lo <= strike <= hi):
            continue
        oi = int(o.get("open_interest") or 0)
        if oi <= 0:
            continue
        b = by_exp.setdefault(expiry, {"C": {}, "P": {}})
        b[cp][strike] = b[cp].get(strike, 0) + oi

    if not by_exp:
        return None

    call_oi: Dict[float, int] = {}
    put_oi: Dict[float, int] = {}
    call_exp_oi: Dict[float, Dict[str, int]] = {}
    put_exp_oi: Dict[float, Dict[str, int]] = {}
    expiry_breakdown: List[dict] = []
    used = 0
    for exp in sorted(by_exp.keys())[:max_expirations]:
        b = by_exp[exp]
        c_sum, p_sum = sum(b["C"].values()), sum(b["P"].values())
        expiry_breakdown.append({"expiry": exp, "call_oi": c_sum, "put_oi": p_sum, "total": c_sum + p_sum})
        for s, oi in b["C"].items():
            call_oi[s] = call_oi.get(s, 0) + oi
            d = call_exp_oi.setdefault(s, {})
            d[exp] = d.get(exp, 0) + oi
        for s, oi in b["P"].items():
            put_oi[s] = put_oi.get(s, 0) + oi
            d = put_exp_oi.setdefault(s, {})
            d[exp] = d.get(exp, 0) + oi
        used += 1

    if not call_oi and not put_oi:
        return None
    _log.info("CBOE %s 全链 OI 聚合：%d 到期日，总 OI %s",
              ticker, used, f"{sum(call_oi.values()) + sum(put_oi.values()):,}")
    return {
        "call_oi": call_oi, "put_oi": put_oi,
        "call_exp_oi": call_exp_oi, "put_exp_oi": put_exp_oi,
        "expiry_breakdown": expiry_breakdown, "used_exps": used,
    }


if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    r = fetch_cboe_chain(tk)
    if not r:
        print(f"{tk}: CBOE 获取失败")
        sys.exit(1)
    toi = sum(c["openInterest"] for c in r["calls"]) + sum(p["openInterest"] for p in r["puts"])
    print(f"{tk}: {len(r['expirations'])} 到期日 {r['expirations']}")
    print(f"  calls={len(r['calls'])} puts={len(r['puts'])} 总OI={int(toi):,} source={r['_source']}")
    if r["calls"]:
        c = r["calls"][0]
        print(f"  样例 call: strike={c['strike']} OI={c['openInterest']} IV={c['impliedVolatility']:.3f} gamma={c['gamma']:.5f} dte={c['dte']}")
