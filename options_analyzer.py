"""
🐝 Alpha Hive - 期权分析 Agent (OptionsBee)
智能期权信号提取：IV Rank、Put/Call Ratio、Gamma Exposure、异动检测
"""

import json
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
    from resilience import yfinance_limiter as _opt_rl, yfinance_breaker as _opt_cb
except ImportError:
    _opt_rl = None
    _opt_cb = None

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

    _LAST_VALID_IV_TTL = 172800  # 48 小时：覆盖周末 + 收市后整晚

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
        except Exception:
            return None

    def _save_last_valid_iv(self, ticker: str, iv: float) -> None:
        """保存当前有效 IV，供收市后降级使用"""
        try:
            cache_path = self._get_cache_path(ticker, "last_valid_iv")
            atomic_json_write(cache_path, {"iv": iv, "timestamp": datetime.now().isoformat()})
        except Exception:
            pass

    def fetch_options_chain(self, ticker: str) -> Dict:
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

            # 获取 DTE ≥ 7 的前 3 个到期日（避免 gamma 膨胀的超短期 IV 干扰 IV Rank）
            # 若不足则降级为最近的 3 个（保证至少有数据可用）
            all_expirations = list(stock.options)
            today_dt = datetime.now()
            expirations = [
                e for e in all_expirations
                if (datetime.strptime(e, "%Y-%m-%d") - today_dt).days >= 7
            ][:3]
            if not expirations:
                expirations = all_expirations[:3]

            calls_list = []
            puts_list = []

            for expiry in expirations:
                try:
                    chain = stock.option_chain(expiry)
                    calls = chain.calls
                    puts = chain.puts

                    # 过滤无效数据（保留 OI >= 0，不再要求 > 100）
                    calls = calls[calls["openInterest"] >= 0]
                    puts = puts[puts["openInterest"] >= 0]

                    calls["expiry"] = expiry
                    puts["expiry"] = expiry

                    calls_list.append(calls)
                    puts_list.append(puts)
                except (ConnectionError, TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
                    _log.warning("获取 %s %s 期权链失败：%s", ticker, expiry, e)
                    continue

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

            result = {
                "ticker": ticker,
                "timestamp": datetime.now().isoformat(),
                "calls": calls_df.to_dict(orient="records") if calls_df is not None else [],
                "puts": puts_df.to_dict(orient="records") if puts_df is not None else [],
                "expirations": expirations,
            }

            self._write_cache(ticker, "chain", result)
            if _opt_cb:
                _opt_cb.record_success()
            return result

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError) as e:
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

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
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
            except (AttributeError, TypeError):
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

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError,
                TypeError, AttributeError, IndexError) as e:
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

        except (ConnectionError, TimeoutError, OSError, ValueError, AttributeError) as e:
            _log.warning("获取 %s 期权到期日失败：%s", ticker, e)
            return self._get_sample_expirations(ticker)

    # ==================== 样本数据降级策略 ====================

    def _get_sample_options_chain(self, ticker: str) -> Dict:
        """样本期权链数据"""
        return {
            "ticker": ticker,
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

    def detect_unusual_activity(
        self, calls_df: List[Dict], puts_df: List[Dict]
    ) -> List[Dict]:
        """
        检测异动信号
        - 成交量 / 开仓量 > 5
        - 单笔成交量 > 10000
        """
        unusual = []

        # 检测看涨扫货（Call Sweep）
        for call in calls_df:
            volume = call.get("volume", 0)
            oi = call.get("openInterest", 1)

            if oi > 0 and volume / oi > 5:
                unusual.append(
                    {
                        "type": "call_sweep",
                        "strike": call.get("strike"),
                        "volume": volume,
                        "oi": oi,
                        "ratio": round(volume / oi, 2),
                        "bullish": True,
                    }
                )
            elif volume > 10000:
                unusual.append(
                    {
                        "type": "large_call_volume",
                        "strike": call.get("strike"),
                        "volume": volume,
                        "bullish": True,
                    }
                )

        # 检测看跌扫货（Put Sweep）
        for put in puts_df:
            volume = put.get("volume", 0)
            oi = put.get("openInterest", 1)

            if oi > 0 and volume / oi > 5:
                unusual.append(
                    {
                        "type": "put_sweep",
                        "strike": put.get("strike"),
                        "volume": volume,
                        "oi": oi,
                        "ratio": round(volume / oi, 2),
                        "bullish": False,
                    }
                )
            elif volume > 10000:
                unusual.append(
                    {
                        "type": "large_put_volume",
                        "strike": put.get("strike"),
                        "volume": volume,
                        "bullish": False,
                    }
                )

        # 按成交量排序，返回前 10 个
        unusual.sort(key=lambda x: x.get("volume", 0), reverse=True)
        return unusual[:10]

    def find_key_levels(
        self, calls_df: List[Dict], puts_df: List[Dict]
    ) -> Dict:
        """
        找出高 OI 的关键行权价（支撑/阻力）
        """
        key_levels = {"support": [], "resistance": []}

        if calls_df:
            # 看涨的高 OI 是阻力
            calls_sorted = sorted(
                calls_df, key=lambda x: x.get("openInterest", 0), reverse=True
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
            # 看跌的高 OI 是支撑
            puts_sorted = sorted(
                puts_df, key=lambda x: x.get("openInterest", 0), reverse=True
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


class OptionsAgent:
    """期权分析 Agent - 统一接口"""

    def __init__(self):
        self.analyzer = OptionsAnalyzer()
        self.fetcher = OptionsDataFetcher()

    def analyze(self, ticker: str, stock_price: Optional[float] = None) -> Dict:
        """
        执行完整期权分析
        返回标准化分析结果字典
        """
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

        raw_ivs = []
        for c in calls_df:
            iv = c.get("impliedVolatility")
            strike = c.get("strike", 0)
            expiry = c.get("expiry", "")
            if iv and iv > 0.005 and atm_lower <= strike <= atm_upper and _expiry_ok(expiry):
                raw_ivs.append(iv)

        # 如果过滤后无数据，放宽到包含短期到期
        if not raw_ivs:
            for c in calls_df:
                iv = c.get("impliedVolatility")
                strike = c.get("strike", 0)
                if iv and iv > 0.005 and atm_lower <= strike <= atm_upper:
                    raw_ivs.append(iv)

        _MIN_VALID_IV = 5.0  # IV < 5% 视为无效

        if raw_ivs:
            current_iv = statistics.median(raw_ivs) * 100  # 小数 → 百分比
        else:
            current_iv = 0.0

        # 判断当前是否在美股交易时段（ET 9:30-16:00，周一到周五）
        from datetime import timezone, timedelta as _td, time as _dtime
        _utc = datetime.now(timezone.utc)
        # 夏令时：3月第二个周日 ~ 11月第一个周日（粗略：3-11月 ET=UTC-4，其余 UTC-5）
        _et = _utc + _td(hours=-4 if 3 <= _utc.month <= 11 else -5)
        _market_open = (_et.weekday() < 5 and
                        _dtime(9, 30) <= _et.time() < _dtime(16, 0))

        # 两种情况使用缓存（48 小时内的上次有效值）：
        #   1. 非交易时段 —— yfinance IV 不可信（stale quotes / near-zero）
        #   2. IV 过低 —— 即使在开市时段也视为异常数据
        if not _market_open or current_iv < _MIN_VALID_IV:
            last_valid = self.fetcher._read_last_valid_iv(ticker)
            if last_valid:
                _log.info(
                    "%s IV 降级→缓存 %.2f%% (市场%s, raw_iv=%.2f%%)",
                    ticker, last_valid,
                    "已关闭" if not _market_open else "异常数据", current_iv
                )
                current_iv = last_valid
            elif current_iv < _MIN_VALID_IV:
                current_iv = 25.0  # 无缓存兜底
            # else: 收市但无缓存，保留 raw data（优于硬编码）
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
        unusual_activity = self.analyzer.detect_unusual_activity(calls_df, puts_df)
        key_levels = self.analyzer.find_key_levels(calls_df, puts_df)

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

        # 7. 汇总结果
        result = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "iv_rank": iv_rank,  # 0-100
            "iv_percentile": iv_percentile,  # 0-100
            "iv_current": iv_current,  # 当前 IV
            "put_call_ratio": put_call_ratio,
            "total_oi": sum(c.get("openInterest", 0) for c in calls_df)
            + sum(p.get("openInterest", 0) for p in puts_df),
            "gamma_exposure": gex,
            "gamma_squeeze_risk": gamma_squeeze_risk,
            "unusual_activity": unusual_activity,
            "key_levels": key_levels,
            "flow_direction": flow_direction,
            "options_score": options_score,  # 0-10
            "signal_summary": signal_summary,
            "expiration_dates": options_chain.get("expirations", [])[:3],
        }

        # 分析完成

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
