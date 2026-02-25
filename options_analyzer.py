"""
🐝 Alpha Hive - 期权分析 Agent (OptionsBee)
智能期权信号提取：IV Rank、Put/Call Ratio、Gamma Exposure、异动检测
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import statistics

from hive_logger import PATHS, get_logger

_log = get_logger("options")

try:
    import yfinance as yf
except ImportError:
    yf = None


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
        except Exception:
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

            with open(cache_path, "w") as f:
                json.dump(cache_data, f, default=_json_default)
        except Exception as e:
            _log.warning("缓存写入失败：%s", e)

    def fetch_options_chain(self, ticker: str) -> Dict:
        """获取期权链数据 - 支持多源降级（yfinance > 样本数据）"""
        # 尝试读取缓存
        cached = self._read_cache(ticker, "chain")
        if cached:
            pass  # {ticker} 期权链数据来自缓存")
            return cached

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

            # 获取最近的两个到期日
            expirations = list(stock.options)[:3]  # 前 3 个到期日

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
                except Exception as e:
                    _log.warning("获取 %s %s 期权链失败：%s", ticker, expiry, e)
                    continue

            if not calls_list or not puts_list:
                _log.warning("%s 期权数据不足，使用样本数据", ticker)
                return self._get_sample_options_chain(ticker)

            # 合并所有到期日的数据
            import pandas as pd

            calls_df = pd.concat(calls_list, ignore_index=True) if calls_list else None
            puts_df = pd.concat(puts_list, ignore_index=True) if puts_list else None

            # NaN → 0 以保证 JSON 序列化 + 下游计算不出错
            if calls_df is not None:
                calls_df = calls_df.fillna(0)
            if puts_df is not None:
                puts_df = puts_df.fillna(0)

            result = {
                "ticker": ticker,
                "timestamp": datetime.now().isoformat(),
                "calls": calls_df.to_dict(orient="records") if calls_df is not None else [],
                "puts": puts_df.to_dict(orient="records") if puts_df is not None else [],
                "expirations": expirations,
            }

            self._write_cache(ticker, "chain", result)
            pass  # {ticker} 期权链数据来自 yfinance")
            return result

        except Exception as e:
            _log.warning("获取 %s 期权数据失败：%s，使用样本数据", ticker, e)
            return self._get_sample_options_chain(ticker)

    def fetch_historical_iv(self, ticker: str, days: int = 252) -> List[float]:
        """获取历史 IV 数据 - 用历史波动率代替"""
        cached = self._read_cache(ticker, "hist_iv")
        if cached:
            pass  # {ticker} 历史 IV 来自缓存")
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

            # 计算历史波动率（近端期权的隐含波动率代理）
            returns = hist["Close"].pct_change().dropna()
            rolling_vol = returns.rolling(window=20).std() * 100 * (252 ** 0.5)

            # 转换为 IV（假设 IV ≈ Historical Vol）
            iv_list = rolling_vol.dropna().tolist()

            # 保留最后 252 个数据点
            iv_list = iv_list[-days:]

            self._write_cache(ticker, "hist_iv", iv_list)
            pass  # {ticker} 历史 IV 来自 yfinance")
            return iv_list

        except Exception as e:
            _log.warning("获取 %s 历史 IV 失败：%s，使用样本数据", ticker, e)
            return self._get_sample_historical_iv(ticker)

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

        except Exception as e:
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

        # 优先使用 openInterest
        total_call_oi = _safe_sum(calls_df, "openInterest")
        total_put_oi = _safe_sum(puts_df, "openInterest")

        # OI 全零时降级为 volume
        if total_call_oi == 0 and total_put_oi == 0:
            total_call_oi = _safe_sum(calls_df, "volume")
            total_put_oi = _safe_sum(puts_df, "volume")

        if total_call_oi == 0:
            return 1.0  # 无数据时返回中立而非 0

        ratio = total_put_oi / total_call_oi
        return round(ratio, 2)

    def calculate_gamma_exposure(
        self, calls_df: List[Dict], puts_df: List[Dict], stock_price: float
    ) -> float:
        """
        计算 Gamma Exposure
        正 GEX：做市商对冲压制波动（对多头有利）
        负 GEX：做市商放大波动（对趋势跟踪有利）
        """
        if not calls_df or not puts_df:
            return 0.0

        # 简化版：用 OI * gamma 计算
        call_gamma = sum(
            c.get("openInterest", 0) * c.get("gamma", 0) for c in calls_df
        )
        put_gamma = sum(
            p.get("openInterest", 0) * p.get("gamma", 0) for p in puts_df
        )

        # 正数 = 看多，负数 = 看空
        gex = (call_gamma - put_gamma) / 1000000 if (call_gamma + put_gamma) > 0 else 0.0

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
        # yfinance 返回小数格式 (0.285 = 28.5%)，需要转换为百分比格式以匹配历史 IV
        # 过滤掉深度 ITM/OTM 的 IV ≈ 0 噪音（< 0.005 = 0.5%）
        raw_ivs = [
            c.get("impliedVolatility") for c in calls_df
            if c.get("impliedVolatility") and c.get("impliedVolatility") > 0.005
        ]
        if raw_ivs:
            current_iv = statistics.mean(raw_ivs)
            # 自动检测并统一为百分比格式（>1 已是百分比，<1 是小数需×100）
            if current_iv < 1.0:
                current_iv *= 100
        else:
            current_iv = 25.0

        # 3. 计算各项指标
        iv_rank, iv_current = self.analyzer.calculate_iv_rank(current_iv, hist_iv)
        iv_percentile = self.analyzer.calculate_iv_percentile(current_iv, hist_iv)
        put_call_ratio = self.analyzer.calculate_put_call_ratio(calls_df, puts_df)
        gex = self.analyzer.calculate_gamma_exposure(
            calls_df, puts_df, stock_price or 145.0
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
