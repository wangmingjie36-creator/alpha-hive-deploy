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
import threading as _threading
from datetime import datetime
from typing import Dict

_log = logging.getLogger("alpha_hive.unusual_options")

_CACHE: Dict[str, Dict] = {}
_CACHE_TS: Dict[str, float] = {}
_cache_lock = _threading.Lock()
try:
    from config import CACHE_CONFIG as _CC
    _CACHE_TTL = _CC["ttl"].get("unusual_options", 300)
except (ImportError, KeyError):
    _CACHE_TTL = 300

import time as _time

# ── 取数重试与退避（只针对「令牌等待超时」与瞬时网络错误）──────────────────────────
# 2026-10-05 重跑 28/30 只丢了异常流，日志里能对上的失败有两类：yf_gate 的共享令牌桶（0.5 req/s）排队超过 60s
# ⇒ `YFRateLimited("等待 yfinance 限流令牌超过 60s（Ticker.options）")`（14:28~14:29 的 options_analyzer / unusual_options
# 各有记录），以及 `curl: (35) TLS connect error`。两者都是「错开即恢复」型——前者是自己人抢令牌，后者是瞬时握手。
# ⚠️ 与 yf_gate 的设计一致，**不对 429 / 冷却重试**：「一次 429 的含义是现在就停，不是再试试」；
#    冷却中（`限流冷却中`）、识别出的 429、以及其它异常一律不重试、直接失败（由 fetch_status 可见）。
# 预算：每只标的共用 2 次（10s、20s）；进程内累计 RETRY_CIRCUIT_TRIP 只标的把预算用光后不再重试——
# 持续饥饿时继续等只会把整轮扫描拖长，也加重对共享桶的占用。
RETRY_BACKOFF_S = (10.0, 20.0)
RETRY_CIRCUIT_TRIP = 5
_retry_exhausted = 0
_sleep = _time.sleep            # 测试可替换，避免真睡


def _failure_kind(exc: BaseException) -> str:
    """cooldown / rate_limited（不重试）；token_wait / transient（重试）；other（不重试）。"""
    msg = str(exc)
    if "限流冷却中" in msg:
        return "cooldown"
    if "等待 yfinance 限流令牌" in msg:
        return "token_wait"
    if type(exc).__name__ == "YFRateLimited":
        return "rate_limited"
    try:
        from yf_gate import is_rate_limit_error
        if is_rate_limit_error(exc):
            return "rate_limited"
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return "transient"
    return "other"


_RETRYABLE_KINDS = ("token_wait", "transient")


class _RetryBudget:
    """一只标的一次检测的重试预算（`.options` 与各 `option_chain` 共用）。"""

    def __init__(self):
        self.used = 0
        self.kinds = []
        self._counted = False

    def call(self, fn, ticker: str, what: str):
        global _retry_exhausted
        while True:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001
                kind = _failure_kind(e)
                budget_left = self.used < len(RETRY_BACKOFF_S)
                with _cache_lock:
                    tripped = _retry_exhausted >= RETRY_CIRCUIT_TRIP
                    if kind in _RETRYABLE_KINDS and not budget_left and not self._counted:
                        self._counted = True
                        _retry_exhausted += 1
                if kind not in _RETRYABLE_KINDS or not budget_left or tripped:
                    e.args = (f"{e} [{kind}]",) + tuple(e.args[1:]) if e.args else (f"[{kind}]",)
                    raise
                delay = RETRY_BACKOFF_S[self.used]
                self.used += 1
                self.kinds.append(kind)
                _log.warning("unusual_options %s %s 取数失败（%s），%.0fs 后重试（%d/%d）：%s",
                             ticker, what, kind, delay, self.used, len(RETRY_BACKOFF_S), str(e)[:80])
                _sleep(delay)


def _is_cached(ticker: str) -> bool:
    with _cache_lock:
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
    with _cache_lock:
        if ticker in _CACHE and (_time.time() - _CACHE_TS.get(ticker, 0)) < _CACHE_TTL:
            return dict(_CACHE[ticker])  # 返回副本，防止外部修改

    result = {
        "unusual_score": 5.0,
        "unusual_direction": "neutral",
        "signals": [],
        "summary": "期权流数据不可用",
        "data_source": "fallback",
        # v0.45.421：取数状态与「无异常」分开。此前 yfinance 把所有到期日的期权链都取失败，也返回
        # 「无异常期权流信号 / neutral / 5.0 / data_source=yfinance_chain」——与真的没有异动不可区分，
        # 且被缓存 300 秒（2026-10-05 重跑 28/30 只丢了这一路，日志里只有 2 条 warning）。
        #   fetch_status: "ok" 全部到期日取到 / "partial" 部分取到 / "failed" 一个都没取到或根本没进循环
        "fetch_status": "failed", "failure_reason": "", "chains_total": 0, "chains_failed": 0,
        "retries": 0,
    }

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        retry = _RetryBudget()

        # 获取所有到期日的期权链
        expirations = retry.call(lambda: t.options, ticker, "options")
        if not expirations:
            result["summary"] = "无期权链数据"
            # yfinance 默认 hide_exceptions=True：取数失败与「该标的没有期权」在返回值里无法区分
            result["failure_reason"] = "无到期日列表（取数失败，或该标的无期权——yfinance 无法区分）"
            return result

        if not stock_price or stock_price <= 0:
            try:
                info = t.fast_info
                stock_price = getattr(info, "last_price", 0) or 0.0
            except (AttributeError, Exception):
                stock_price = 0.0
            if not stock_price or stock_price <= 0:
                # v0.40.1: 拿不到真实现价时诚实跳过——假价 100 会把 OTM 距离
                # 全算错，产出假异动信号（同 v38.0 期权样本链教训）
                try:
                    from data_pipeline import fetch_stock_data as _fsd_uo
                    stock_price = float(_fsd_uo(ticker).get("price") or 0.0)
                except Exception:
                    stock_price = 0.0
            if not stock_price or stock_price <= 0:
                result["summary"] = "现价不可得，跳过异动检测"
                result["failure_reason"] = "现价不可得"
                return result

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

        chains_total = chains_failed = 0
        last_chain_error = ""
        for exp, days_to_exp in near_expirations[:4]:
            chains_total += 1
            try:
                chain = retry.call(lambda: t.option_chain(exp), ticker, f"option_chain {exp}")
            except Exception as e:
                chains_failed += 1
                last_chain_error = f"{type(e).__name__}: {str(e)[:80]}"
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
                    # v0.45.54：`or 1` 让 vol_oi_ratio = volume / 1 = volume ——
                    # 任何有成交的合约都会被判成「异动」。真实 OI=0 是「新开合约」，
                    # 与「OI 字段缺失」都不该被当成 1 张持仓。
                    _oi_raw = row.get("openInterest")
                    try:
                        oi = int(_oi_raw) if _oi_raw is not None else 0
                    except (TypeError, ValueError):
                        oi = 0
                    if oi <= 0:
                        continue      # 无持仓基数 ⇒ 比率无意义，不判异动
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
                        # v0.45.54：`or 1` 让 vol_oi_ratio = volume / 1 = volume ——
                        # 任何有成交的合约都会被判成「异动」。真实 OI=0 是「新开合约」，
                        # 与「OI 字段缺失」都不该被当成 1 张持仓。
                        _oi_raw = row.get("openInterest")
                        try:
                            oi = int(_oi_raw) if _oi_raw is not None else 0
                        except (TypeError, ValueError):
                            oi = 0
                        if oi <= 0:
                            continue      # 无持仓基数 ⇒ 比率无意义，不判异动
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

        # --- 取数状态（v0.45.421）：每个到期日都失败 ⇒ 这不是「无异常」，是没数据 ---
        if chains_total and chains_failed == chains_total:
            _log.warning("unusual_options 全部 %d 个到期日期权链取数失败 %s：%s",
                         chains_total, ticker, last_chain_error)
            result["summary"] = "期权链全部取数失败"
            result["failure_reason"] = f"全部 {chains_total} 个到期日取数失败：{last_chain_error}"
            result["chains_total"], result["chains_failed"] = chains_total, chains_failed
            result["retries"] = retry.used
            return result      # fallback 形状（5.0 / neutral / 无信号），**不缓存**
        if chains_failed:
            _log.warning("unusual_options 部分期权链取数失败 %s：%d/%d（%s）——结果只覆盖取到的到期日",
                         ticker, chains_failed, chains_total, last_chain_error)

        # --- 综合评分 ---
        all_unusual = unusual_calls + unusual_puts

        # 按溢价排序，返回全部（v0.16.0: 移除 [:5] 截断）
        all_unusual.sort(key=lambda x: x["dollar_premium"], reverse=True)
        top_signals = all_unusual

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
            "fetch_status": "partial" if chains_failed else "ok",
            "failure_reason": f"{chains_failed}/{chains_total} 个到期日取数失败：{last_chain_error}" if chains_failed else "",
            "chains_total": chains_total, "chains_failed": chains_failed,
            "retries": retry.used,
        }

        with _cache_lock:
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
        result["failure_reason"] = f"{type(e).__name__}: {str(e)[:80]}"
        return result
