"""
🐝 Alpha Hive - 期权分析 Agent (OptionsBee)
智能期权信号提取：IV Rank、Put/Call Ratio、Gamma Exposure、异动检测
"""

import json
import math as _math
import os
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import statistics

from hive_logger import PATHS, get_logger, atomic_json_write

_log = get_logger("options")

try:
    import yfinance as yf
except ImportError:
    yf = None

# ── 期权数据断路器（#9）──
try:
    from resilience import yfinance_limiter as _opt_rl, yfinance_breaker as _opt_cb, NETWORK_ERRORS
except ImportError:
    _opt_rl = None
    _opt_cb = None
    NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError, ValueError, KeyError)

try:
    from hive_logger import FeatureRegistry
    FeatureRegistry.register("yfinance_options", yf is not None,
                              "期权分析不可用" if yf is None else "")
except ImportError:
    pass


class OptionsDataFetcher:
    """期权数据采集器 - 支持多源降级策略"""

    def __init__(self, cache_dir: str = str(PATHS.cache_dir)):
        self.cache_dir = cache_dir
        self.cache_ttl = 300  # 5 分钟缓存
        os.makedirs(cache_dir, exist_ok=True)

    def _get_cache_path(self, ticker: str, data_type: str) -> str:
        """获取缓存文件路径"""
        return os.path.join(self.cache_dir, f"options_{ticker}_{data_type}.json")

    def _read_cache(self, ticker: str, data_type: str) -> Optional[Dict]:
        """读取缓存数据"""
        cache_path = self._get_cache_path(ticker, data_type)
        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, "r") as f:
                data = json.load(f)

            # 检查缓存是否过期
            timestamp = data.get("timestamp")
            if timestamp:
                cached_time = datetime.fromisoformat(timestamp)
                if (datetime.now() - cached_time).total_seconds() > self.cache_ttl:
                    return None

            return data.get("data")
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
            _log.debug("options cache read failed: %s", e)
            return None

    def _write_cache(self, ticker: str, data_type: str, data: Dict) -> None:
        """写入缓存数据"""
        try:
            cache_path = self._get_cache_path(ticker, data_type)
            cache_data = {
                "timestamp": datetime.now().isoformat(),
                "data": data,
            }

            def _json_default(obj):
                """处理 pandas Timestamp 等不可序列化类型"""
                if hasattr(obj, "isoformat"):
                    return obj.isoformat()
                if hasattr(obj, "item"):  # numpy scalar
                    return obj.item()
                return str(obj)

            atomic_json_write(cache_path, cache_data, default=_json_default)
        except (OSError, TypeError, ValueError) as e:
            _log.warning("缓存写入失败：%s", e)

    # BUG FIX 根因②: 原 172800s(48h) 不覆盖美国 3 天长周末（周五收→周二开≈88h）
    # 改为 432000s(120h/5天)，确保整个长周末期间缓存有效
    _LAST_VALID_IV_TTL = 432000  # 120 小时（5 天）

    def _read_last_valid_iv(self, ticker: str) -> Optional[float]:
        """读取上次有效 IV（48 小时内），用于收市/数据缺失时降级"""
        cache_path = self._get_cache_path(ticker, "last_valid_iv")
        try:
            if not os.path.exists(cache_path):
                return None
            with open(cache_path) as f:
                data = json.load(f)
            ts = data.get("timestamp", "")
            if ts:
                age = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
                if age > self._LAST_VALID_IV_TTL:
                    return None
            return float(data["iv"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            return None

    def _save_last_valid_iv(self, ticker: str, iv: float) -> None:
        """保存当前有效 IV，供收市后降级使用"""
        try:
            cache_path = self._get_cache_path(ticker, "last_valid_iv")
            atomic_json_write(cache_path, {"iv": iv, "timestamp": datetime.now().isoformat()})
        except (OSError, TypeError, ValueError):
            pass

    def fetch_options_chain(self, ticker: str, stock_price: float = 0.0) -> Dict:
        """获取期权链数据 - 支持多源降级（yfinance > 样本数据）"""
        # 尝试读取缓存
        cached = self._read_cache(ticker, "chain")
        if cached:
            pass  # {ticker} 期权链数据来自缓存")
            return cached

        # 断路器检查（#9）：yfinance 最近连续失败时快速降级
        if _opt_cb and not _opt_cb.allow_request():
            _log.warning("%s 期权链跳过：yfinance 断路器开路（近期连续失败）", ticker)
            return self._get_sample_options_chain(ticker)

        # 主来源：yfinance
        if yf is None:
            _log.warning("yfinance 未安装，使用样本数据")
            return self._get_sample_options_chain(ticker)

        try:
            stock = yf.Ticker(ticker)

            # 获取最近的到期日
            if not hasattr(stock, "options") or not stock.options:
                _log.warning("%s 期权数据不可用，使用样本数据", ticker)
                return self._get_sample_options_chain(ticker)

            # v0.17.0: 到期日选择策略优化 — 防止到期周 OI 跳变
            # 策略：取 DTE ≥ 3 的前 4 个（扩大覆盖面），DTE < 7 的到期日标记为
            # "near_expiry" 供下游做 OI 权重衰减（避免 total_oi 因到期结算骤降）
            # 旧策略：DTE ≥ 7 的前 3 个 → Opex 周 OI 可能骤降 60%+
            all_expirations = list(stock.options)
            today_dt = datetime.now()
            _expiry_with_dte = []
            for _e in all_expirations:
                try:
                    _dte = (datetime.strptime(_e, "%Y-%m-%d") - today_dt).days
                    if _dte >= 3:  # 排除 DTE<3 的超临近到期（已进入结算态）
                        _expiry_with_dte.append((_e, _dte))
                except ValueError:
                    continue
            # 优先取 DTE ≥ 7 的前 4 个，若不足 3 个则补入 DTE 3-6 的
            _far = [e for e, d in _expiry_with_dte if d >= 7][:4]
            _near = [e for e, d in _expiry_with_dte if 3 <= d < 7][:2]
            expirations = (_far + _near)[:4] if _far else [e for e, _ in _expiry_with_dte[:4]]
            if not expirations:
                expirations = all_expirations[:3]  # 终极降级
            # 记录哪些到期日是近期的（DTE < 7），供 total_oi 计算做权重衰减
            _near_expiry_set = {e for e, d in _expiry_with_dte if d < 7}

            # 获取当前股价，用于 ATM 过滤（避免深度遗留低价合约污染 key levels）
            _current_price = 0.0
            try:
                _fi = stock.fast_info
                _current_price = float(
                    _fi.get("lastPrice") or _fi.get("previousClose") or 0
                )
            except (AttributeError, TypeError, KeyError, RuntimeError):
                pass

            calls_list = []
            puts_list = []

            for expiry in expirations:
                for _retry in range(3):  # 最多重试 2 次（U3: 期权链重试）
                    try:
                        chain = stock.option_chain(expiry)
                        calls = chain.calls
                        puts = chain.puts

                        # 过滤无效数据（保留 OI >= 0，不再要求 > 100）
                        calls = calls[calls["openInterest"] >= 0]
                        puts = puts[puts["openInterest"] >= 0]

                        # ATM 过滤：剔除偏离当前价 >70% 的深度 OTM/ITM 遗留合约
                        # 防止十年前低价时留下的 $5/$10 行权价污染 key levels
                        if _current_price > 0:
                            _lo = _current_price * 0.30
                            _hi = _current_price * 1.70
                            calls = calls[(calls["strike"] >= _lo) & (calls["strike"] <= _hi)]
                            puts = puts[(puts["strike"] >= _lo) & (puts["strike"] <= _hi)]

                        # U4: 内存保护 — 每个到期日最多保留 top 40 strikes（按 OI）
                        if len(calls) > 40:
                            calls = calls.nlargest(40, "openInterest")
                        if len(puts) > 40:
                            puts = puts.nlargest(40, "openInterest")

                        calls["expiry"] = expiry
                        puts["expiry"] = expiry

                        calls_list.append(calls)
                        puts_list.append(puts)
                        break  # 成功则跳出重试
                    except (*NETWORK_ERRORS, TypeError) as e:
                        if _retry < 2:
                            import time as _time
                            _time.sleep(1.0 * (2 ** _retry))  # 1s, 2s 指数退避
                            continue
                        _log.warning("获取 %s %s 期权链失败（重试耗尽）：%s", ticker, expiry, e)
                        break

            if not calls_list or not puts_list:
                _log.warning("%s 期权数据不足，降级为样本数据（yfinance 返回空链）", ticker)
                if _opt_cb:
                    _opt_cb.record_failure()
                return self._get_sample_options_chain(ticker)

            # 合并所有到期日的数据，并按 DTE 加权
            import pandas as pd

            calls_df = pd.concat(calls_list, ignore_index=True) if calls_list else None
            puts_df = pd.concat(puts_list, ignore_index=True) if puts_list else None

            # NaN → 0 以保证 JSON 序列化 + 下游计算不出错
            if calls_df is not None:
                calls_df = calls_df.fillna(0)
            if puts_df is not None:
                puts_df = puts_df.fillna(0)

            # DTE 加权：近期到期的期权权重更高（1/sqrt(DTE)）
            # 用于下游 P/C ratio、GEX 等聚合计算
            today = datetime.now()
            for df in [calls_df, puts_df]:
                if df is not None and not df.empty and "expiry" in df.columns:
                    dte_values = []
                    for exp_str in df["expiry"]:
                        try:
                            exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d")
                            dte = max(1, (exp_date - today).days)
                        except (ValueError, TypeError):
                            dte = 30  # 默认
                        dte_values.append(dte)
                    df["dte"] = dte_values
                    # 权重 = 1/sqrt(DTE)，归一化使最大权重=1.0
                    raw_weights = [1.0 / (d ** 0.5) for d in dte_values]
                    max_w = max(raw_weights) if raw_weights else 1.0
                    df["dte_weight"] = [w / max_w for w in raw_weights]

            # ── 注入 BS gamma（yfinance 不返回 Greeks）──────────────
            # 用 Black-Scholes 为每份合约计算 gamma，填入 "gamma" 字段
            # 供下游 calculate_gamma_exposure() 使用
            _BS_RISK_FREE = 0.045  # 参考无风险利率

            def _bs_gamma_inline(S: float, K: float, T: float, sigma: float) -> float:
                if S <= 0 or K <= 0 or T <= 1e-6 or sigma < 0.01:
                    return 0.0
                try:
                    d1 = (_math.log(S / K) + (_BS_RISK_FREE + 0.5 * sigma ** 2) * T) / (sigma * _math.sqrt(T))
                    return _math.exp(-0.5 * d1 * d1) / (_math.sqrt(2 * _math.pi) * S * sigma * _math.sqrt(T))
                except (ValueError, ZeroDivisionError):
                    return 0.0

            # 估算股价：先用传入参数，无则从最大 OI 行权价中值推断
            _S = stock_price or 0.0
            if _S <= 0 and calls_df is not None and not calls_df.empty:
                _atm_candidates = calls_df[calls_df["openInterest"] > 50]["strike"].tolist() if "openInterest" in calls_df.columns else []
                _S = float(statistics.median(_atm_candidates)) if _atm_candidates else 0.0
            for df in [calls_df, puts_df]:
                if df is None or df.empty or _S <= 0:
                    continue
                gammas = []
                for _, row in df.iterrows():
                    raw_g = row.get("gamma", 0.0) if "gamma" in df.columns else 0.0
                    if raw_g and raw_g != 0.0:
                        gammas.append(float(raw_g))
                        continue
                    K     = float(row.get("strike", 0) or 0)
                    dte   = float(row.get("dte", 30) or 30)
                    sigma = float(row.get("impliedVolatility", 0) or 0)
                    T     = max(dte, 0.5) / 365.0
                    gammas.append(_bs_gamma_inline(_S, K, T, sigma))
                df["gamma"] = gammas
            # ── /BS gamma 注入 ────────────────────────────────────

            result = {
                "ticker": ticker,
                "timestamp": datetime.now().isoformat(),
                "calls": calls_df.to_dict(orient="records") if calls_df is not None else [],
                "puts": puts_df.to_dict(orient="records") if puts_df is not None else [],
                "expirations": expirations,
                # v0.17.0: 近期到期集合，供下游 total_oi 计算做权重衰减
                "near_expiry_set": list(_near_expiry_set),
            }

            self._write_cache(ticker, "chain", result)
            if _opt_cb:
                _opt_cb.record_success()
            return result

        except (*NETWORK_ERRORS, TypeError, AttributeError) as e:
            _log.warning("获取 %s 期权链失败：%s，降级为样本数据", ticker, e)
            if _opt_cb:
                _opt_cb.record_failure()
            return self._get_sample_options_chain(ticker)

    def fetch_historical_iv(self, ticker: str, days: int = 252) -> List[float]:
        """获取历史 IV 数据 - 用历史已实现波动率 + 动态 IV 溢价估算

        方法：
        1. 获取当前期权链中的实际隐含波动率（ATM 中位数）
        2. 计算当前 20 日已实现波动率
        3. 算出动态 IV/HV 比率（典型范围 1.05-1.60）
        4. 用该比率 × 历史 HV 滚动序列 = 更准确的历史 IV 代理

        相比固定 HV × 1.25 的优势：
        - 高波动期（如财报季前），IV premium 可能高达 1.6+
        - 低波动期，IV premium 可能低至 1.05
        - 动态比率让 IV Rank 更贴合实际市场状态
        """
        cached = self._read_cache(ticker, "hist_iv_v3")
        if cached:
            return cached

        if yf is None:
            _log.warning("yfinance 未安装，使用样本 IV 数据")
            return self._get_sample_historical_iv(ticker)

        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1y")

            if hist.empty:
                _log.warning("%s 历史数据不可用，使用样本数据", ticker)
                return self._get_sample_historical_iv(ticker)

            # 计算历史已实现波动率（20日滚动）
            returns = hist["Close"].pct_change().dropna()
            rolling_vol = returns.rolling(window=20).std() * 100 * (252 ** 0.5)
            hv_values = rolling_vol.dropna().tolist()

            if not hv_values:
                return self._get_sample_historical_iv(ticker)

            # 动态 IV premium：从当前期权链获取实际 IV，与当前 HV 对比
            current_hv = hv_values[-1] if hv_values else 25.0
            iv_premium = self._estimate_iv_premium(stock, current_hv)

            iv_list = [v * iv_premium for v in hv_values]

            # 保留最后 252 个数据点
            iv_list = iv_list[-days:]

            self._write_cache(ticker, "hist_iv_v3", iv_list)
            return iv_list

        except (*NETWORK_ERRORS, TypeError) as e:
            _log.warning("获取 %s 历史 IV 失败：%s，使用样本数据", ticker, e)
            return self._get_sample_historical_iv(ticker)

    def _estimate_iv_premium(self, stock, current_hv: float) -> float:
        """
        从当前期权链估算 IV/HV 比率（动态 IV premium）

        - 取 ATM ±20% 范围内的 call IV 中位数
        - 计算 IV / HV 比率，clamp 到 [1.05, 2.0]
        - 无法获取时降级为 1.25
        """
        try:
            if not hasattr(stock, "options") or not stock.options:
                return 1.25

            # 跳过 DTE<7 的近期到期日（近到期期权 IV 因 Gamma 效应被人为抬高）
            today_dt = datetime.now()
            expiry = None
            for _e in stock.options:
                try:
                    if (datetime.strptime(_e, "%Y-%m-%d") - today_dt).days >= 7:
                        expiry = _e
                        break
                except (ValueError, TypeError):
                    continue
            if expiry is None:
                expiry = stock.options[0]  # 降级：无 DTE≥7 则取最近的
            chain = stock.option_chain(expiry)
            calls = chain.calls

            # 获取当前股价
            try:
                price = stock.fast_info.get("lastPrice", 0) or stock.fast_info.get("previousClose", 0)
            except (AttributeError, TypeError, KeyError, RuntimeError):
                price = 0

            if not price:
                all_strikes = calls["strike"].tolist()
                price = statistics.median(all_strikes) if all_strikes else 100.0

            # ATM ±20% 范围
            atm_lower = price * 0.80
            atm_upper = price * 1.20

            atm_calls = calls[
                (calls["strike"] >= atm_lower) &
                (calls["strike"] <= atm_upper) &
                (calls["impliedVolatility"] > 0.005)
            ]

            if atm_calls.empty:
                return 1.25

            # 中位数 IV（yfinance 返回小数，×100 转百分比）
            current_iv = float(atm_calls["impliedVolatility"].median()) * 100

            if current_hv <= 0:
                return 1.25

            ratio = current_iv / current_hv
            # clamp 到合理范围
            return max(1.05, min(2.0, ratio))

        except (*NETWORK_ERRORS, TypeError, AttributeError, IndexError) as e:
            _log.debug("IV premium 估算降级: %s", e)
            return 1.25

    def fetch_expirations(self, ticker: str) -> List[str]:
        """获取期权到期日列表"""
        if yf is None:
            _log.warning("yfinance 未安装，使用样本到期日")
            return self._get_sample_expirations(ticker)

        try:
            stock = yf.Ticker(ticker)

            if not hasattr(stock, "options") or not stock.options:
                _log.warning("%s 期权到期日不可用", ticker)
                return self._get_sample_expirations(ticker)

            expirations = list(stock.options)[:5]  # 返回前 5 个到期日
            pass  # {ticker} 期权到期日来自 yfinance")
            return expirations

        except (*NETWORK_ERRORS, AttributeError) as e:
            _log.warning("获取 %s 期权到期日失败：%s", ticker, e)
            return self._get_sample_expirations(ticker)

    # ==================== 样本数据降级策略 ====================

    def _get_sample_options_chain(self, ticker: str) -> Dict:
        """样本期权链数据"""
        return {
            "ticker": ticker,
            "source": "sample",
            "timestamp": datetime.now().isoformat(),
            "calls": [
                {
                    "strike": 140.0,
                    "openInterest": 15000,
                    "volume": 8500,
                    "bid": 8.5,
                    "ask": 9.2,
                    "gamma": 0.0082,
                    "vega": 42.5,
                    "theta": -3.2,
                    "impliedVolatility": 0.285,
                    "expiry": "2026-03-21",
                },
                {
                    "strike": 145.0,
                    "openInterest": 22000,
                    "volume": 12000,
                    "bid": 5.2,
                    "ask": 5.9,
                    "gamma": 0.0095,
                    "vega": 38.2,
                    "theta": -2.8,
                    "impliedVolatility": 0.278,
                    "expiry": "2026-03-21",
                },
                {
                    "strike": 150.0,
                    "openInterest": 18500,
                    "volume": 6200,
                    "bid": 2.8,
                    "ask": 3.4,
                    "gamma": 0.0078,
                    "vega": 32.1,
                    "theta": -2.2,
                    "impliedVolatility": 0.272,
                    "expiry": "2026-03-21",
                },
            ],
            "puts": [
                {
                    "strike": 140.0,
                    "openInterest": 12000,
                    "volume": 5800,
                    "bid": 7.2,
                    "ask": 7.9,
                    "gamma": 0.0081,
                    "vega": 41.2,
                    "theta": -2.5,
                    "impliedVolatility": 0.282,
                    "expiry": "2026-03-21",
                },
                {
                    "strike": 145.0,
                    "openInterest": 9500,
                    "volume": 3200,
                    "bid": 4.8,
                    "ask": 5.4,
                    "gamma": 0.0092,
                    "vega": 36.8,
                    "theta": -2.0,
                    "impliedVolatility": 0.275,
                    "expiry": "2026-03-21",
                },
                {
                    "strike": 135.0,
                    "openInterest": 8200,
                    "volume": 2100,
                    "bid": 12.5,
                    "ask": 13.2,
                    "gamma": 0.0065,
                    "vega": 38.5,
                    "theta": -3.1,
                    "impliedVolatility": 0.291,
                    "expiry": "2026-03-21",
                },
            ],
            "expirations": ["2026-03-21", "2026-04-18", "2026-05-16"],
        }

    def _get_sample_historical_iv(self, ticker: str) -> List[float]:
        """样本历史 IV 数据"""
        # 生成 252 个 IV 值（1 年），范围 20-40
        base_iv = {
            "NVDA": 28.5,
            "TSLA": 45.2,
            "VKTX": 52.8,
        }.get(ticker, 30.0)

        # 添加随机波动（±10%）
        iv_list = [
            base_iv + (i % 10 - 5) * 0.8 for i in range(252)
        ]
        return iv_list

    def _get_sample_expirations(self, ticker: str) -> List[str]:
        """样本到期日列表"""
        today = datetime.now()
        expirations = []

        # 生成后续 5 个到期日（假设周二和第三个周五）
        for weeks in [1, 2, 4, 8, 16]:
            exp_date = today + timedelta(weeks=weeks)
            # 调整到下一个周五
            days_to_friday = (4 - exp_date.weekday()) % 7
            exp_date = exp_date + timedelta(days=days_to_friday)
            expirations.append(exp_date.strftime("%Y-%m-%d"))

        return expirations



class OptionsAnalyzer:
    """期权信号分析器"""

    def __init__(self):
        self.fetcher = OptionsDataFetcher()

    def calculate_iv_rank(
        self, current_iv: float, hist_iv_list: List[float]
    ) -> Tuple[float, float]:
        """
        计算 IV Rank (0-100)
        IV Rank = (current_iv - min_52w) / (max_52w - min_52w) * 100
        """
        if not hist_iv_list or len(hist_iv_list) < 10:
            # 数据不足，返回中立值
            return 50.0, current_iv

        min_iv = min(hist_iv_list)
        max_iv = max(hist_iv_list)

        if max_iv == min_iv:
            iv_rank = 50.0
        else:
            iv_rank = ((current_iv - min_iv) / (max_iv - min_iv)) * 100
            iv_rank = max(0, min(100, iv_rank))  # 约束在 0-100

        return round(iv_rank, 2), round(current_iv, 2)

    def calculate_iv_percentile(self, current_iv: float, hist_iv_list: List[float]) -> float:
        """计算 IV 百分位数（当前 IV 排名）"""
        if not hist_iv_list or len(hist_iv_list) < 10:
            return 50.0

        # 计算有多少个历史 IV 低于当前 IV
        count_below = sum(1 for iv in hist_iv_list if iv < current_iv)
        percentile = (count_below / len(hist_iv_list)) * 100

        return round(percentile, 2)

    def calculate_put_call_ratio(
        self, calls_df: List[Dict], puts_df: List[Dict]
    ) -> float:
        """
        计算 Put/Call Ratio (开仓量权重，OI 优先，OI 全零时用 volume)
        P/C < 0.7 → 强多头信号
        0.7-1.5 → 中立
        > 1.5 → 强空头信号
        """
        if not calls_df or not puts_df:
            return 1.0  # 默认中立

        import math

        def _safe_sum(data, key):
            return sum(
                v for v in (d.get(key, 0) for d in data)
                if v and not (isinstance(v, float) and math.isnan(v))
            )

        # DTE 加权 OI（近期到期权重更高）
        def _weighted_sum(data, key):
            return sum(
                v * d.get("dte_weight", 1.0)
                for d in data
                for v in [d.get(key, 0)]
                if v and not (isinstance(v, float) and math.isnan(v))
            )

        # 优先使用 DTE 加权 openInterest
        total_call_oi = _weighted_sum(calls_df, "openInterest")
        total_put_oi = _weighted_sum(puts_df, "openInterest")

        # OI 全零时降级为 volume
        if total_call_oi == 0 and total_put_oi == 0:
            total_call_oi = _weighted_sum(calls_df, "volume")
            total_put_oi = _weighted_sum(puts_df, "volume")

        if total_call_oi == 0:
            return 1.0  # 无数据时返回中立而非 0

        ratio = total_put_oi / total_call_oi
        return round(ratio, 2)

    def calculate_gamma_exposure(
        self, calls_df: List[Dict], puts_df: List[Dict], stock_price: float
    ) -> float:
        """
        计算 Notional Gamma Exposure（标准做市商 delta-hedge 模型）

        公式：GEX = Σ(stock_price × 100 × gamma × OI × dte_weight)
        - stock_price: 标的股票当前价格
        - 100: 每份合约对应 100 股
        - gamma: 该行权价的 gamma
        - OI: 未平仓合约数
        - dte_weight: DTE 权重（近期期权权重更大）

        做市商在 call 上做多 gamma（买入 call → long gamma），
        在 put 上做空 gamma（卖出 put → short gamma），
        因此 net GEX = call_gamma - put_gamma

        正 GEX：做市商对冲压制波动（稳定市场）
        负 GEX：做市商放大波动（利于趋势跟踪）

        返回值单位：百万美元 notional gamma
        """
        if not calls_df or not puts_df:
            return 0.0

        if stock_price <= 0:
            return 0.0

        # 标准 notional GEX 计算
        call_gamma = sum(
            stock_price * 100 * c.get("gamma", 0) * c.get("openInterest", 0)
            * c.get("dte_weight", 1.0)
            for c in calls_df
        )
        put_gamma = sum(
            stock_price * 100 * p.get("gamma", 0) * p.get("openInterest", 0)
            * p.get("dte_weight", 1.0)
            for p in puts_df
        )

        # 正数 = net long gamma（压制波动），负数 = net short gamma（放大波动）
        # 除以 1e6 转为百万美元
        total = call_gamma + put_gamma
        gex = (call_gamma - put_gamma) / 1e6 if total > 0 else 0.0

        return round(gex, 4)

    def calculate_iv_skew(
        self, calls_df: List[Dict], puts_df: List[Dict], stock_price: float
    ) -> Dict:
        """
        S14：计算 IV Skew（25-delta put vs 25-delta call 的隐含波动率差）

        Skew > 1.3 → 机构大量买保护性 put → bearish 信号（恐慌溢价）
        Skew < 0.8 → call 端投机过热 → 可能过度乐观
        0.8~1.3   → 正常范围

        近似 25-delta：OTM ~5% 的行权价（put: stock_price * 0.95, call: stock_price * 1.05）
        """
        if not calls_df or not puts_df or stock_price <= 0:
            return {"skew_ratio": None, "skew_signal": "数据不足"}

        import math

        # 近似 25-delta 行权价范围
        put_target = stock_price * 0.95   # OTM put ~5% below
        call_target = stock_price * 1.05  # OTM call ~5% above
        tolerance = stock_price * 0.03    # ±3% 容差

        # 找 OTM put IV（行权价 ≈ stock_price * 0.95）
        put_ivs = []
        for p in puts_df:
            strike = p.get("strike", 0)
            iv = p.get("impliedVolatility", 0)
            try:
                if iv and math.isfinite(iv) and iv > 0.005 and abs(strike - put_target) <= tolerance:
                    put_ivs.append(float(iv))
            except (TypeError, ValueError):
                continue

        # 找 OTM call IV（行权价 ≈ stock_price * 1.05）
        call_ivs = []
        for c in calls_df:
            strike = c.get("strike", 0)
            iv = c.get("impliedVolatility", 0)
            try:
                if iv and math.isfinite(iv) and iv > 0.005 and abs(strike - call_target) <= tolerance:
                    call_ivs.append(float(iv))
            except (TypeError, ValueError):
                continue

        if not put_ivs or not call_ivs:
            return {"skew_ratio": None, "skew_signal": "OTM 期权数据不足"}

        avg_put_iv = sum(put_ivs) / len(put_ivs)
        avg_call_iv = sum(call_ivs) / len(call_ivs)

        if avg_call_iv <= 0:
            return {"skew_ratio": None, "skew_signal": "call IV 为零"}

        skew_ratio = round(avg_put_iv / avg_call_iv, 3)

        if skew_ratio > 1.3:
            signal = "bearish（机构恐慌对冲）"
        elif skew_ratio < 0.8:
            signal = "bullish（call 投机过热）"
        else:
            signal = "neutral"

        return {
            "skew_ratio": skew_ratio,
            "skew_signal": signal,
            "otm_put_iv": round(avg_put_iv * 100, 2),
            "otm_call_iv": round(avg_call_iv * 100, 2),
        }

    def calculate_iv_term_structure(
        self, ticker: str, stock_price: float
    ) -> Dict:
        """计算个股 IV 期限结构：逐到期日取 ATM IV，判断 Contango/Backwardation。

        Contango  (正向期限结构): 近期 IV < 远期 IV → 正常，市场无即时恐慌
        Backwardation (倒挂):    近期 IV > 远期 IV → 市场担忧短期事件（财报/催化剂）

        Returns:
            {
              "term_structure": [{"expiry": str, "dte": int, "atm_iv": float}, ...],
              "shape": "contango"|"backwardation"|"flat"|"unknown",
              "front_iv": float|None,    # 最近 expiry ATM IV (%)
              "back_iv": float|None,     # 较远 expiry ATM IV (%)
              "iv_spread": float|None,   # back_iv - front_iv (pp), >0 = contango
              "signal": str,
            }
        """
        result: Dict = {"shape": "unknown", "term_structure": [], "signal": "IV期限结构数据不足"}
        if yf is None or stock_price <= 0:
            return result
        try:
            import math
            stock_obj = yf.Ticker(ticker)
            if not hasattr(stock_obj, "options") or not stock_obj.options:
                return result

            today_dt = datetime.now()
            # 取 4 个跨度不同的到期日（尽量覆盖 30/60/90/180 DTE）
            all_exps = list(stock_obj.options)
            selected: List[str] = []
            targets = [25, 55, 85, 150]   # 目标 DTE
            for tgt in targets:
                best = min(
                    (e for e in all_exps if (datetime.strptime(e, "%Y-%m-%d") - today_dt).days >= tgt - 10),
                    key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d") - today_dt).days - tgt),
                    default=None
                )
                if best and best not in selected:
                    selected.append(best)
            if not selected:
                selected = all_exps[:4]

            term_pts: List[Dict] = []
            atm_tol = stock_price * 0.04   # ±4% ATM 容差

            for exp in selected:
                try:
                    chain = stock_obj.option_chain(exp)
                    dte = (datetime.strptime(exp, "%Y-%m-%d") - today_dt).days
                    # ATM call IV（最接近股价的行权价）
                    calls_list = chain.calls.to_dict("records") if chain.calls is not None else []
                    ivs = []
                    for c in calls_list:
                        k = c.get("strike", 0)
                        iv_raw = c.get("impliedVolatility", 0)
                        if (abs(k - stock_price) <= atm_tol
                                and iv_raw and math.isfinite(iv_raw) and 0.02 < iv_raw < 2.0):
                            ivs.append(float(iv_raw) * 100)
                    if ivs:
                        term_pts.append({
                            "expiry": exp,
                            "dte":    dte,
                            "atm_iv": round(sum(ivs) / len(ivs), 1),
                        })
                except Exception:
                    continue

            if len(term_pts) < 2:
                result["term_structure"] = term_pts
                return result

            result["term_structure"] = term_pts
            front_iv = term_pts[0]["atm_iv"]
            back_iv  = term_pts[-1]["atm_iv"]
            iv_spread = round(back_iv - front_iv, 1)
            result["front_iv"]  = front_iv
            result["back_iv"]   = back_iv
            result["iv_spread"] = iv_spread

            if iv_spread >= 3.0:
                result["shape"] = "contango"
                result["signal"] = (
                    f"IV期限结构正向（前端{front_iv:.1f}% → 远端{back_iv:.1f}%，"
                    f"+{iv_spread:.1f}pp），市场无短期恐慌，IV低廉适合方向性买权"
                )
            elif iv_spread <= -3.0:
                result["shape"] = "backwardation"
                result["signal"] = (
                    f"IV期限结构倒挂（前端{front_iv:.1f}% > 远端{back_iv:.1f}%，"
                    f"{iv_spread:.1f}pp），短期事件溢价偏高（财报/催化剂），卖短期权占优"
                )
            else:
                result["shape"] = "flat"
                result["signal"] = (
                    f"IV期限结构平坦（前端{front_iv:.1f}% / 远端{back_iv:.1f}%，"
                    f"利差{iv_spread:+.1f}pp），无明显方向性时间价值信号"
                )

        except Exception as e:
            _log.debug("IV term structure unavailable for %s: %s", ticker, e)
        return result

    def detect_unusual_activity(
        self, calls_df: List[Dict], puts_df: List[Dict],
        stock_price: float = 0.0
    ) -> List[Dict]:
        """
        检测异动信号（v0.16.0 重写：对齐 unusual_options.py 多维检测逻辑）

        触发条件（满足任一即标记异常）：
        1. Vol/OI Sweep: vol/oi >= 5 且 vol >= 200（新建仓信号）
        2. 大单成交: vol > 10000
        3. OTM 投机买入: OTM >= 5% 且 vol >= 100 且 vol/oi >= 2
        4. 短期急单: 到期 <= 14 天 且 OTM >= 3% 且 vol >= 100
        5. 大额溢价: dollar_premium >= $500K
        """
        unusual = []
        _sp = stock_price if stock_price > 0 else 0

        def _scan(contracts: List[Dict], is_call: bool):
            for c in contracts:
                volume = int(c.get("volume", 0) or 0)
                oi = int(c.get("openInterest", 0) or 1)
                strike = float(c.get("strike", 0) or 0)
                last_price = float(c.get("lastPrice", 0) or 0)
                expiry = c.get("expiry", "")

                if volume < 50 or strike <= 0:
                    continue

                vol_oi = volume / max(oi, 1)
                dollar_premium = volume * last_price * 100
                otm_pct = 0.0
                if _sp > 0:
                    otm_pct = ((strike - _sp) / _sp * 100) if is_call else ((_sp - strike) / _sp * 100)

                # 计算到期天数
                days_to_exp = 30  # 默认
                if expiry:
                    try:
                        from datetime import datetime as _dt
                        exp_date = _dt.strptime(str(expiry)[:10], "%Y-%m-%d")
                        days_to_exp = (exp_date - _dt.now()).days
                    except (ValueError, TypeError):
                        pass

                is_unusual = False
                reasons = []

                # 条件 1: Vol/OI Sweep
                if vol_oi >= 5 and volume >= 200:
                    is_unusual = True
                    reasons.append(f"Vol/OI={vol_oi:.1f}x")

                # 条件 2: 大单成交
                if volume > 10000:
                    is_unusual = True
                    reasons.append(f"大单{volume:,}手")

                # 条件 3: OTM 投机
                if otm_pct >= 5 and volume >= 100 and vol_oi >= 2:
                    is_unusual = True
                    reasons.append(f"OTM+{otm_pct:.1f}%投机")

                # 条件 4: 短期急单
                if days_to_exp <= 14 and otm_pct >= 3 and volume >= 100:
                    is_unusual = True
                    reasons.append(f"短期{days_to_exp}天急单")

                # 条件 5: 大额溢价
                if dollar_premium >= 500_000:
                    is_unusual = True
                    reasons.append(f"溢价${dollar_premium/1e6:.2f}M")

                if is_unusual:
                    _type_prefix = "call" if is_call else "put"
                    _type = f"{_type_prefix}_sweep" if vol_oi >= 5 else f"large_{_type_prefix}_volume"
                    entry = {
                        "type": _type,
                        "strike": strike,
                        "volume": volume,
                        "oi": oi,
                        "ratio": round(vol_oi, 2),
                        "bullish": is_call,
                        "otm_pct": round(otm_pct, 1),
                        "dollar_premium": round(dollar_premium),
                        "days_to_exp": days_to_exp,
                        "reasons": reasons,
                    }
                    if expiry:
                        entry["expiry"] = str(expiry)[:10]
                    unusual.append(entry)

        _scan(calls_df, is_call=True)
        _scan(puts_df, is_call=False)

        # v0.16.0: 不合并，保留每条原始明细（渲染层按到期日分组展示）
        unusual.sort(key=lambda x: x.get("dollar_premium", 0), reverse=True)
        return unusual

    def find_key_levels(
        self, calls_df: List[Dict], puts_df: List[Dict], stock_price: float = 0.0
    ) -> Dict:
        """
        找出高 OI 的关键行权价（支撑/阻力）

        stock_price: 当前股价，用于 ATM 过滤（剔除偏离 >70% 的遗留低价合约）
        """
        key_levels = {"support": [], "resistance": []}

        # ATM 过滤函数：偏离当前价 >70% 的行权价视为无效（历史遗留合约）
        def _atm_ok(item: Dict) -> bool:
            if stock_price <= 0:
                return True
            s = item.get("strike", 0)
            return stock_price * 0.30 <= s <= stock_price * 1.70

        if calls_df:
            # 看涨的高 OI 是阻力：先 ATM 过滤，再按 OI 排序
            calls_filtered = [c for c in calls_df if _atm_ok(c)]
            calls_sorted = sorted(
                calls_filtered if calls_filtered else calls_df,
                key=lambda x: x.get("openInterest", 0),
                reverse=True,
            )
            for call in calls_sorted[:3]:
                key_levels["resistance"].append(
                    {
                        "strike": call.get("strike"),
                        "oi": call.get("openInterest"),
                        "iv": call.get("impliedVolatility"),
                    }
                )

        if puts_df:
            # 看跌的高 OI 是支撑：先 ATM 过滤，再按 OI 排序
            puts_filtered = [p for p in puts_df if _atm_ok(p)]
            puts_sorted = sorted(
                puts_filtered if puts_filtered else puts_df,
                key=lambda x: x.get("openInterest", 0),
                reverse=True,
            )
            for put in puts_sorted[:3]:
                key_levels["support"].append(
                    {
                        "strike": put.get("strike"),
                        "oi": put.get("openInterest"),
                        "iv": put.get("impliedVolatility"),
                    }
                )

        return key_levels

    def generate_options_score(
        self,
        iv_rank: float,
        put_call_ratio: float,
        gex: float,
        unusual: List[Dict],
    ) -> Tuple[float, str]:
        """
        生成期权综合评分 (0-10)

        公式：
        iv_signal (0-3): IV 在 30-70 最高，极端高低扣分
        flow_signal (0-3): P/C 越低（多头）得分越高
        gex_signal (0-2): 负 GEX 加分（波动放大利于趋势）
        unusual_signal (0-2): 每 1 个多头大单 +1，上限 2
        """

        # IV Signal (0-3)：IV Rank 在 40-70 得分最高
        if iv_rank < 20:
            iv_signal = 1.0  # 极低 IV
        elif iv_rank < 40:
            iv_signal = 2.0  # 低 IV
        elif iv_rank <= 70:
            iv_signal = 3.0  # 理想范围
        elif iv_rank <= 85:
            iv_signal = 2.0  # 偏高
        else:
            iv_signal = 1.0  # 极高 IV

        # Flow Signal (0-3)：P/C 越低越多头
        if put_call_ratio < 0.7:
            flow_signal = 3.0
        elif put_call_ratio < 1.0:
            flow_signal = 2.0
        elif put_call_ratio < 1.5:
            flow_signal = 1.0
        else:
            flow_signal = 0.0

        # GEX Signal (0-2)：负 GEX 有利趋势跟踪
        gex_signal = 2.0 if gex < -0.001 else 1.0

        # Unusual Signal (0-2)：多头异动加分
        bullish_unusual = sum(1 for u in unusual if u.get("bullish", False))
        unusual_signal = min(2.0, bullish_unusual * 0.5)

        total_score = iv_signal + flow_signal + gex_signal + unusual_signal
        total_score = round(total_score, 2)

        # 生成信号总结
        signals = []
        if iv_signal >= 3.0:
            signals.append("IV 处于理想水位")
        if flow_signal >= 3.0:
            signals.append("做多气氛浓厚（P/C低）")
        if gex < -0.001:
            signals.append("负 GEX 利于趋势")
        if bullish_unusual > 0:
            signals.append(f"检测到 {bullish_unusual} 个看涨异动")

        summary = " | ".join(signals) if signals else "信号平衡"

        return total_score, summary


def _sanitize_result(result: Dict) -> None:
    """方案21: 遍历结果 dict，将 NaN/Inf float 替换为 0.0（就地修改）"""
    for key, val in result.items():
        if isinstance(val, float) and (_math.isnan(val) or _math.isinf(val)):
            result[key] = 0.0
        elif isinstance(val, dict):
            _sanitize_result(val)  # 递归处理嵌套 dict（如 iv_skew_detail）
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, float) and (_math.isnan(item) or _math.isinf(item)):
                    val[i] = 0.0
                elif isinstance(item, dict):
                    _sanitize_result(item)  # 递归处理 list 内嵌套 dict（如 unusual_activity）


class OptionsAgent:
    """期权分析 Agent - 统一接口"""

    def __init__(self):
        self.analyzer = OptionsAnalyzer()
        self.fetcher = OptionsDataFetcher()

    # ── v0.17.0: OI 稳定性口径 ──────────────────────────────────────
    @staticmethod
    def _calc_total_oi(calls_df: list, puts_df: list, options_chain: dict) -> int:
        """计算 total_oi（稳定口径）：排除 DTE < 7 的近到期合约。

        目的：Opex 周到期日脱落会导致 OI 日环比骤降 50-80%，产生虚假异常告警。
        稳定口径只统计 DTE ≥ 7 的合约 OI，使日环比对比更平滑。
        当所有合约都是 DTE < 7 时，退化为原始总和（避免返回 0）。
        """
        near_set = set(options_chain.get("near_expiry_set", []))
        if not near_set:
            # 无近期标记，退化为原始求和
            return (sum(c.get("openInterest", 0) for c in calls_df)
                    + sum(p.get("openInterest", 0) for p in puts_df))

        stable_oi = 0
        for c in calls_df:
            exp = str(c.get("expiry", ""))[:10]
            if exp not in near_set:
                stable_oi += int(c.get("openInterest", 0) or 0)
        for p in puts_df:
            exp = str(p.get("expiry", ""))[:10]
            if exp not in near_set:
                stable_oi += int(p.get("openInterest", 0) or 0)

        # 如果排除后为 0（全是近期合约），退化为原始总和
        if stable_oi == 0:
            return (sum(c.get("openInterest", 0) for c in calls_df)
                    + sum(p.get("openInterest", 0) for p in puts_df))
        return stable_oi

    def analyze(self, ticker: str, stock_price: Optional[float] = None,
                force_refresh: bool = False) -> Dict:
        """
        执行完整期权分析
        返回标准化分析结果字典

        v0.15.2: 跨进程 per-ticker-per-date snapshot 缓存
        - 同一交易日内，任何模块（OracleBee / advanced_analyzer / BearBee 等）
          调用本方法都会返回**同一份冻结快照**，避免 yfinance 多次独立拉取
          产生数据分裂（iv_rank / pc_ratio / GEX 等不一致）。
        - 首次调用执行完整计算并写入 cache/options_snapshot_{TICKER}_{YYYY-MM-DD}.json
        - 后续调用直接读取该文件，无需重新计算
        - 旁路方式：
            1) 传 force_refresh=True 强制重算
            2) 环境变量 OPTIONS_SNAPSHOT_DISABLE=1 全局禁用
        """
        # ===== Snapshot cache 入口 =====
        _snap_disabled = os.environ.get("OPTIONS_SNAPSHOT_DISABLE", "").lower() in ("1", "true", "yes")
        _snap_date = datetime.now().strftime("%Y-%m-%d")
        _snap_path = os.path.join(self.fetcher.cache_dir,
                                  f"options_snapshot_{ticker}_{_snap_date}.json")
        if not _snap_disabled and not force_refresh and os.path.exists(_snap_path):
            try:
                with open(_snap_path, "r") as _f:
                    _cached = json.load(_f)
                # 校验快照日期仍是今天，防止跨午夜脏数据
                _cached_ts = _cached.get("_snapshot_timestamp", "")
                if _cached_ts.startswith(_snap_date):
                    _log.info("[%s] 期权快照命中: %s (冻结于 %s)",
                              ticker, os.path.basename(_snap_path), _cached_ts[:19])
                    return _cached
                else:
                    _log.warning("[%s] 期权快照日期不匹配 (%s vs %s)，忽略",
                                 ticker, _cached_ts[:10], _snap_date)
            except (json.JSONDecodeError, OSError) as _e:
                _log.warning("[%s] 期权快照读取失败，重新计算: %s", ticker, _e)
        # 期权分析

        # 1. 获取期权链数据
        options_chain = self.fetcher.fetch_options_chain(ticker)
        calls_df = options_chain.get("calls", [])
        puts_df = options_chain.get("puts", [])

        # 2. 获取历史 IV
        hist_iv = self.fetcher.fetch_historical_iv(ticker)

        # 计算当前 IV（从期权链中获取）
        # 关键修复：
        # 1. 只用 ATM 附近（±20%）的期权
        # 2. 过滤 <7 天到期的期权（临近到期 IV 被 Theta 衰减人为放大）
        # 3. 用中位数代替均值，抗极端值
        atm_price = stock_price
        if not atm_price:
            all_strikes = [c.get("strike", 0) for c in calls_df if c.get("openInterest", 0) > 100]
            atm_price = statistics.median(all_strikes) if all_strikes else 145.0
        atm_lower = atm_price * 0.80
        atm_upper = atm_price * 1.20

        # 判断到期日是否 >= 7 天
        min_expiry_days = 7
        today = datetime.now()
        def _expiry_ok(expiry_str):
            """过滤 <7 天到期的期权"""
            if not expiry_str:
                return True  # 无到期日信息时不过滤
            try:
                exp_date = datetime.strptime(str(expiry_str)[:10], "%Y-%m-%d")
                return (exp_date - today).days >= min_expiry_days
            except (ValueError, TypeError):
                return True

        # ── 根因③ BUG FIX: 加上 IV 上限过滤，防止盘后宽 spread 极端值污染 median ──
        # 盘后 ATM 期权 bid/ask 暴宽，impliedVolatility 常飙到 100%-300%，
        # 必须在计算 median 之前剔除，否则均值严重偏高
        _IV_UPPER_RAW = 2.0   # 盘后 raw 过滤上限（小数，= 200%），去除极端噪声
        raw_ivs = []
        for c in calls_df:
            iv = c.get("impliedVolatility")
            strike = c.get("strike", 0)
            expiry = c.get("expiry", "")
            if iv and 0.005 < iv < _IV_UPPER_RAW and atm_lower <= strike <= atm_upper and _expiry_ok(expiry):
                raw_ivs.append(iv)

        # 如果过滤后无数据，放宽到包含短期到期（保留上限过滤）
        if not raw_ivs:
            for c in calls_df:
                iv = c.get("impliedVolatility")
                strike = c.get("strike", 0)
                if iv and 0.005 < iv < _IV_UPPER_RAW and atm_lower <= strike <= atm_upper:
                    raw_ivs.append(iv)

        _MIN_VALID_IV = 5.0    # IV < 5% 视为无效
        _MAX_VALID_IV = 150.0  # IV > 150% 视为异常（保留极高波动标的的合理空间）

        if raw_ivs:
            current_iv = statistics.median(raw_ivs) * 100  # 小数 → 百分比
        else:
            current_iv = 0.0

        # ── 根因④ BUG FIX: 使用精确 DST 判断，避免 3/11 月换时误判 ──
        from datetime import timezone, timedelta as _td, time as _dtime
        _utc = datetime.now(timezone.utc)
        try:
            # 优先使用 zoneinfo（Python 3.9+）或 pytz 精确处理夏令时
            try:
                from zoneinfo import ZoneInfo as _ZI
                _et = _utc.astimezone(_ZI("America/New_York"))
            except ImportError:
                import pytz as _pytz
                _et = _utc.astimezone(_pytz.timezone("America/New_York"))
        except Exception:
            # 最终降级到粗略近似（保留原逻辑兜底）
            _et = _utc + _td(hours=-4 if 3 <= _utc.month <= 11 else -5)
        _market_open = (_et.weekday() < 5 and
                        _dtime(9, 30) <= _et.time() < _dtime(16, 0))

        # ── 根因①②联合 BUG FIX ──
        # 原逻辑：_save_last_valid_iv 只在 market_open=True 时执行，
        # 但报告通常盘后运行，缓存永远不被写入，导致每次都用 stale raw IV 或硬编码 25.0。
        # 原 TTL=48h，不覆盖美国长周末（周五收→周二开 = ~88h）。
        # 修复策略：
        #   A. 盘中有效 IV → 立即保存（原逻辑保留）
        #   B. 盘后 raw IV 在合理区间 (5%~150%) → 作为"次优缓存"保存并使用，
        #      避免直接使用极端 stale 值或硬编码 25.0
        #   C. 缓存 TTL 从 48h 延长到 120h（覆盖 3 天长周末 + 缓冲）
        if not _market_open or current_iv < _MIN_VALID_IV:
            _raw_iv = current_iv  # 保留原始值用于对比
            last_valid = self.fetcher._read_last_valid_iv(ticker)
            if last_valid:
                # 合理性校验：缓存值不应低于历史 IV 最低点的 70%
                # 防止某次错误采集（如 yfinance 返回极低 IV）污染后续扫描
                _hist_min = min(hist_iv) if hist_iv else 0.0
                _cache_suspicious = _hist_min > 0 and last_valid < _hist_min * 0.7
                if _cache_suspicious:
                    _log.warning(
                        "%s 缓存 IV %.2f%% 低于历史最低 %.2f%% × 70%% → 丢弃坏缓存；"
                        "raw_iv=%.2f%% %s",
                        ticker, last_valid, _hist_min, _raw_iv,
                        "可用，采用" if _MIN_VALID_IV <= _raw_iv <= _MAX_VALID_IV else "同样异常，使用兜底 25.0%%"
                    )
                    # 坏缓存不使用；current_iv 保持 raw_iv 进入下方 elif/else 兜底
                else:
                    _log.info(
                        "%s IV 降级→缓存 %.2f%% (市场%s, raw_iv=%.2f%%)",
                        ticker, last_valid,
                        "已关闭" if not _market_open else "异常数据", _raw_iv
                    )
                    current_iv = last_valid
            if last_valid is None or _cache_suspicious if last_valid else False:
                pass  # 继续往下走 elif/else
            elif _MIN_VALID_IV <= current_iv <= _MAX_VALID_IV:
                # 盘后无缓存，但 raw IV 在合理范围内 → 作为次优缓存保存并使用
                # （优于硬编码 25.0，下次运行直接从缓存读取）
                _log.info(
                    "%s 盘后无缓存，raw_iv=%.2f%% 在合理范围，保存为次优缓存", ticker, current_iv
                )
                self.fetcher._save_last_valid_iv(ticker, current_iv)
                # current_iv 保持不变，直接使用
            elif current_iv < _MIN_VALID_IV:
                current_iv = 25.0  # 完全无效，最后兜底
            # else: current_iv > MAX_VALID_IV → 极端异常，也用 25.0
            else:
                _log.warning("%s raw_iv=%.2f%% 超出合理上限，使用兜底 25.0%%", ticker, current_iv)
                current_iv = 25.0
        else:
            # 开市且 IV 有效 → 保存供收市后使用
            self.fetcher._save_last_valid_iv(ticker, current_iv)

        # 3. 计算各项指标
        iv_rank, iv_current = self.analyzer.calculate_iv_rank(current_iv, hist_iv)
        iv_percentile = self.analyzer.calculate_iv_percentile(current_iv, hist_iv)
        put_call_ratio = self.analyzer.calculate_put_call_ratio(calls_df, puts_df)
        # 估算股价（如果未提供，从期权链 ATM strike 推测）
        if not stock_price:
            all_strikes = [c.get("strike", 0) for c in calls_df if c.get("openInterest", 0) > 100]
            stock_price = statistics.median(all_strikes) if all_strikes else 145.0
        gex = self.analyzer.calculate_gamma_exposure(
            calls_df, puts_df, stock_price
        )
        unusual_activity = self.analyzer.detect_unusual_activity(calls_df, puts_df, stock_price)
        key_levels = self.analyzer.find_key_levels(calls_df, puts_df, stock_price or 0.0)
        # S14: IV Skew 分析
        iv_skew = self.analyzer.calculate_iv_skew(calls_df, puts_df, stock_price)

        # S15: IV 期限结构（个股 term structure）
        iv_term_struct = self.analyzer.calculate_iv_term_structure(ticker, stock_price)

        # ① IV-RV Spread（隐含波动率 vs 已实现波动率价差）
        iv_rv_data: Dict = {}
        try:
            from market_intelligence import calculate_iv_rv_spread
            iv_rv_data = calculate_iv_rv_spread(ticker, current_iv)
        except Exception as _e_ivr:
            _log.debug("IV-RV Spread 计算失败 %s: %s", ticker, _e_ivr)

        # ⑤ Gamma 到期日历（按到期日拆分 OI 集中度 + Pin Risk + Charm 方向）
        gamma_calendar: Dict = {}
        try:
            from market_intelligence import calculate_gamma_expiry_calendar
            gamma_calendar = calculate_gamma_expiry_calendar(
                calls_df, puts_df, stock_price or atm_price
            )
        except Exception as _e_gc:
            _log.debug("Gamma 到期日历计算失败 %s: %s", ticker, _e_gc)

        # 4. 生成综合评分
        options_score, signal_summary = self.analyzer.generate_options_score(
            iv_rank, put_call_ratio, gex, unusual_activity
        )

        # 5. 判断 Gamma Squeeze 风险
        if gex > 0.001:
            gamma_squeeze_risk = "high"  # 正 GEX 压制波动
        elif gex < -0.001:
            gamma_squeeze_risk = "low"  # 负 GEX 放大波动
        else:
            gamma_squeeze_risk = "medium"

        # 6. 判断流向
        if put_call_ratio < 0.85:
            flow_direction = "bullish"
        elif put_call_ratio > 1.2:
            flow_direction = "bearish"
        else:
            flow_direction = "neutral"

        # 7. 数据质量判定
        _is_sample = options_chain.get("source") == "sample"
        _has_real_iv = bool(raw_ivs)
        if _is_sample:
            data_quality = "unavailable"
        elif not _has_real_iv or current_iv < _MIN_VALID_IV:
            data_quality = "degraded"
        else:
            data_quality = "real"

        # 8. 汇总结果
        result = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "data_quality": data_quality,  # "real" | "degraded" | "unavailable"
            "iv_rank": iv_rank,  # 0-100
            "iv_percentile": iv_percentile,  # 0-100
            "iv_current": iv_current,  # 当前 IV
            "put_call_ratio": put_call_ratio,
            # v0.17.0: total_oi 双口径 — raw（原始总和）+ stable（排除 DTE<7 近到期）
            # stable 口径用于日环比对比，避免 Opex 周到期日脱落导致 OI 跳变
            "total_oi": self._calc_total_oi(calls_df, puts_df, options_chain),
            "total_oi_raw": (sum(c.get("openInterest", 0) for c in calls_df)
                             + sum(p.get("openInterest", 0) for p in puts_df)),
            "gamma_exposure": gex,
            "gamma_squeeze_risk": gamma_squeeze_risk,
            "unusual_activity": unusual_activity,
            "key_levels": key_levels,
            "flow_direction": flow_direction,
            "options_score": options_score,  # 0-10
            "signal_summary": signal_summary,
            "expiration_dates": options_chain.get("expirations", [])[:3],
            # S14: IV Skew
            "iv_skew_ratio": iv_skew.get("skew_ratio"),
            "iv_skew_signal": iv_skew.get("skew_signal", ""),
            "iv_skew_detail": iv_skew,
            # S15: IV 期限结构
            "iv_term_structure": iv_term_struct,
            # ① IV-RV Spread
            "rv_30d": iv_rv_data.get("rv_30d", 0.0),
            "iv_rv_spread": iv_rv_data.get("iv_rv_spread", 0.0),
            "iv_rv_signal": iv_rv_data.get("iv_rv_signal", ""),
            "iv_rv_detail": iv_rv_data,
            # ⑤ Gamma 到期日历
            "gamma_calendar": gamma_calendar,
        }

        # 方案21: 出口消毒 — 遍历结果 dict，NaN/Inf → 安全默认值
        _sanitize_result(result)

        # ===== Snapshot cache 出口：写入 per-ticker-per-date 冻结快照 =====
        if not _snap_disabled:
            try:
                result["_snapshot_timestamp"] = datetime.now().isoformat()
                result["_snapshot_ticker"] = ticker
                result["_snapshot_stock_price"] = stock_price
                with open(_snap_path, "w") as _f:
                    json.dump(result, _f, default=str, indent=2)
                _log.info("[%s] 期权快照写入: %s", ticker, os.path.basename(_snap_path))
            except OSError as _e:
                _log.warning("[%s] 期权快照写入失败: %s", ticker, _e)

        return result


# ==================== 脚本示例 ====================
if __name__ == "__main__":
    agent = OptionsAgent()

    # 测试单个标的
    result = agent.analyze("NVDA", stock_price=145.0)

    _log.info("=" * 60)
    _log.info("期权分析结果")
    _log.info("=" * 60)
    _log.info(json.dumps(result, indent=2, ensure_ascii=False))
