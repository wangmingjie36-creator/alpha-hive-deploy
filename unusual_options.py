"""
🐝 Alpha Hive - 异常期权流检测 (P2)
基于 yfinance 期权链实时检测大单 OTM 买入信号

检测维度：
1. 量/持仓比 (Vol/OI) > 3 → 新建仓（非滚动），方向性押注
2. OTM 偏移 > 5% 且成交量异常 → 投机性定向赌注
3. 短期到期（≤14天）OTM 大量买入 → 紧迫性定向赌注（经典扫单特征）
4. 单一行权价溢价总额 > $500K → 机构级大单

免费数据源：yfinance 期权链（无需额外 API Key）
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

_log = logging.getLogger("alpha_hive.unusual_options")

_CACHE: Dict[str, Dict] = {}
_CACHE_TS: Dict[str, float] = {}
try:
    from config import CACHE_CONFIG as _CC
    _CACHE_TTL = _CC["ttl"].get("unusual_options", 300)
except (ImportError, KeyError):
    _CACHE_TTL = 300

import time as _time


def _is_cached(ticker: str) -> bool:
    return ticker in _CACHE and (_time.time() - _CACHE_TS.get(ticker, 0)) < _CACHE_TTL


def detect_unusual_flow(ticker: str, stock_price: float = 0.0) -> Dict:
    """
    检测异常期权流

    Returns:
        {
            "unusual_score": float (0-10),
            "unusual_direction": "bullish"/"bearish"/"neutral",
            "signals": list[dict],       # 发现的异常信号列表
            "summary": str,              # 一句话摘要
            "data_source": str,
        }
    """
    if _is_cached(ticker):
        return _CACHE[ticker]

    result = {
        "unusual_score": 5.0,
        "unusual_direction": "neutral",
        "signals": [],
        "summary": "期权流数据不可用",
        "data_source": "fallback",
    }

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)

        # 获取所有到期日的期权链
        expirations = t.options
        if not expirations:
            result["summary"] = "无期权链数据"
            return result

        if not stock_price or stock_price <= 0:
            try:
                info = t.fast_info
                stock_price = getattr(info, "last_price", 0) or 100.0
            except (AttributeError, Exception):
                stock_price = 100.0

        unusual_calls = []
        unusual_puts = []
        total_call_premium = 0.0
        total_put_premium = 0.0

        now = datetime.now()
        # 只看最近 60 天内到期的合约（更有信号价值）
        near_expirations = []
        for exp in expirations[:6]:  # 最多取前 6 个到期日
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d")
                days_to_exp = (exp_date - now).days
                if days_to_exp <= 60:
                    near_expirations.append((exp, days_to_exp))
            except ValueError:
                continue

        if not near_expirations:
            near_expirations = [(expirations[0], 30)]

        for exp, days_to_exp in near_expirations[:4]:
            try:
                chain = t.option_chain(exp)
            except Exception as e:
                _log.debug("期权链获取失败 %s %s: %s", ticker, exp, e)
                continue

            calls = chain.calls
            puts = chain.puts

            if calls is None or calls.empty:
                continue

            # --- 扫描 CALL ---
            for _, row in calls.iterrows():
                try:
                    strike = float(row.get("strike", 0))
                    volume = int(row.get("volume") or 0)
                    oi = int(row.get("openInterest") or 1)
                    last_price = float(row.get("lastPrice") or 0)
                    implied_vol = float(row.get("impliedVolatility") or 0)

                    if volume < 50 or strike <= 0:
                        continue

                    otm_pct = (strike - stock_price) / stock_price * 100 if stock_price > 0 else 0
                    vol_oi_ratio = volume / max(oi, 1)
                    dollar_premium = volume * last_price * 100  # 每份合约 100 股

                    total_call_premium += dollar_premium

                    is_unusual = False
                    reasons = []

                    # 判断条件
                    if vol_oi_ratio >= 5 and volume >= 200:
                        is_unusual = True
                        reasons.append(f"Vol/OI={vol_oi_ratio:.1f}x（新建仓）")

                    if otm_pct >= 5 and volume >= 100 and vol_oi_ratio >= 2:
                        is_unusual = True
                        reasons.append(f"OTM+{otm_pct:.1f}%投机买入")

                    if days_to_exp <= 14 and otm_pct >= 3 and volume >= 100:
                        is_unusual = True
                        reasons.append(f"短期{days_to_exp}天OTM急单")

                    if dollar_premium >= 500_000:
                        is_unusual = True
                        reasons.append(f"大单溢价${dollar_premium/1e6:.2f}M")

                    if is_unusual:
                        unusual_calls.append({
                            "type": "call",
                            "strike": strike,
                            "expiry": exp,
                            "days_to_exp": days_to_exp,
                            "volume": volume,
                            "oi": oi,
                            "vol_oi_ratio": round(vol_oi_ratio, 1),
                            "otm_pct": round(otm_pct, 1),
                            "dollar_premium": round(dollar_premium),
                            "reasons": reasons,
                        })
                except (TypeError, ValueError, ZeroDivisionError):
                    continue

            # --- 扫描 PUT ---
            if puts is not None and not puts.empty:
                for _, row in puts.iterrows():
                    try:
                        strike = float(row.get("strike", 0))
                        volume = int(row.get("volume") or 0)
                        oi = int(row.get("openInterest") or 1)
                        last_price = float(row.get("lastPrice") or 0)

                        if volume < 50 or strike <= 0:
                            continue

                        otm_pct = (stock_price - strike) / stock_price * 100 if stock_price > 0 else 0
                        vol_oi_ratio = volume / max(oi, 1)
                        dollar_premium = volume * last_price * 100

                        total_put_premium += dollar_premium

                        is_unusual = False
                        reasons = []

                        if vol_oi_ratio >= 5 and volume >= 200:
                            is_unusual = True
                            reasons.append(f"Vol/OI={vol_oi_ratio:.1f}x（新建空仓）")

                        if otm_pct >= 5 and volume >= 100 and vol_oi_ratio >= 2:
                            is_unusual = True
                            reasons.append(f"OTM保护Put+{otm_pct:.1f}%")

                        if days_to_exp <= 14 and otm_pct >= 3 and volume >= 100:
                            is_unusual = True
                            reasons.append(f"短期{days_to_exp}天保护单")

                        if dollar_premium >= 500_000:
                            is_unusual = True
                            reasons.append(f"大单溢价${dollar_premium/1e6:.2f}M")

                        if is_unusual:
                            unusual_puts.append({
                                "type": "put",
                                "strike": strike,
                                "expiry": exp,
                                "days_to_exp": days_to_exp,
                                "volume": volume,
                                "oi": oi,
                                "vol_oi_ratio": round(vol_oi_ratio, 1),
                                "otm_pct": round(otm_pct, 1),
                                "dollar_premium": round(dollar_premium),
                                "reasons": reasons,
                            })
                    except (TypeError, ValueError, ZeroDivisionError):
                        continue

        # --- 综合评分 ---
        all_unusual = unusual_calls + unusual_puts

        # 按溢价排序取最重要的
        all_unusual.sort(key=lambda x: x["dollar_premium"], reverse=True)
        top_signals = all_unusual[:5]

        call_count = len(unusual_calls)
        put_count = len(unusual_puts)

        # 方向判断
        call_premium = sum(s["dollar_premium"] for s in unusual_calls)
        put_premium = sum(s["dollar_premium"] for s in unusual_puts)

        if call_count == 0 and put_count == 0:
            score = 5.0
            direction = "neutral"
            summary = "无异常期权流信号"
        else:
            # 看多信号
            bull_points = call_count * 1.5 + (call_premium / 1e6) * 0.5
            # 看空信号
            bear_points = put_count * 1.5 + (put_premium / 1e6) * 0.5

            if bull_points > bear_points * 1.5:
                direction = "bullish"
                score = min(10.0, 5.5 + bull_points * 0.3)
                summary = f"异常Call流 {call_count}个信号 溢价${call_premium/1e6:.1f}M"
            elif bear_points > bull_points * 1.5:
                direction = "bearish"
                score = max(1.0, 4.5 - bear_points * 0.3)
                summary = f"异常Put流 {put_count}个信号 溢价${put_premium/1e6:.1f}M"
            else:
                direction = "neutral"
                score = 5.0 + (bull_points - bear_points) * 0.2
                summary = f"混合期权流 Call:{call_count} Put:{put_count}"

            score = max(1.0, min(10.0, score))

        result = {
            "unusual_score": round(score, 2),
            "unusual_direction": direction,
            "signals": top_signals,
            "call_signals": unusual_calls[:3],
            "put_signals": unusual_puts[:3],
            "call_premium_total": round(total_call_premium),
            "put_premium_total": round(total_put_premium),
            "summary": summary,
            "data_source": "yfinance_chain",
        }

        _CACHE[ticker] = result
        _CACHE_TS[ticker] = _time.time()
        return result

    except ImportError:
        _log.warning("yfinance 不可用，无法检测异常期权流")
        result["summary"] = "yfinance 不可用"
        return result
    except Exception as e:
        _log.warning("unusual_options 检测失败 %s: %s", ticker, e)
        result["summary"] = f"检测失败: {str(e)[:50]}"
        return result
