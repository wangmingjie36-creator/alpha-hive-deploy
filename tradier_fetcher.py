"""
Tradier API 集成 — IV 数据交叉验证

功能：
1. 实时期权链数据获取（Call/Put 含 Greeks）
2. IV 隐含波动率交叉验证（vs yfinance）
3. 历史 IV 数据回溯（日线级别）
4. Greeks 深度分析（Delta/Gamma/Theta/Vega）
5. 自动缓存管理（5 分钟实时，24 小时历史）

Tradier 免费层优势：
- 无需信用卡（开发者账户免费注册）
- 实时期权 Greeks（美式行权计算）
- 完整期权链数据
- 沙箱环境用于测试

缓存路径：cache/tradier/
API 端点：https://sandbox.tradier.com/v1/（沙箱）或 https://api.tradier.com/v1/（生产）
"""

import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

try:
    import requests
except ImportError:
    requests = None

try:
    from hive_logger import get_logger, atomic_json_write
except ImportError:
    def get_logger(name):
        import logging
        return logging.getLogger(name)

    def atomic_json_write(path: str, data: Dict[str, Any]) -> None:
        """简单原子写入（备选）"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


logger = get_logger(__name__)

# 缓存配置
_CACHE_DIR = Path.home() / "cache" / "tradier"
_LIVE_CACHE_TTL = 300      # 实时数据 5 分钟
_HIST_CACHE_TTL = 86400    # 历史数据 24 小时

# API 配置
_SANDBOX_BASE = "https://sandbox.tradier.com/v1"
_PROD_BASE = "https://api.tradier.com/v1"
_DEFAULT_USE_SANDBOX = True  # 默认使用沙箱


def _load_api_token() -> Optional[str]:
    """
    从环境变量或文件加载 Tradier API Token

    优先级：
    1. 环境变量 TRADIER_API_TOKEN
    2. 文件 ~/.alpha_hive_tradier_key
    3. None（无 token，功能降级）
    """
    # 环境变量
    token = os.environ.get("TRADIER_API_TOKEN", "").strip()
    if token:
        return token

    # 文件
    key_path = Path.home() / ".alpha_hive_tradier_key"
    if key_path.exists():
        try:
            with open(key_path) as f:
                token = f.read().strip()
            if token:
                return token
        except Exception as e:
            logger.warning(f"Failed to read Tradier key from {key_path}: {e}")

    return None


class TradierFetcher:
    """
    Tradier API 集成器

    提供期权链、IV、Greeks 数据获取及交叉验证能力
    """

    def __init__(self, use_sandbox: bool = _DEFAULT_USE_SANDBOX):
        """
        初始化 Tradier 获取器

        Args:
            use_sandbox: 是否使用沙箱环境（True=沙箱，False=生产）
        """
        self.api_token = _load_api_token()
        self.use_sandbox = use_sandbox
        self.base_url = _SANDBOX_BASE if use_sandbox else _PROD_BASE

        if not self.api_token:
            logger.warning(
                "Tradier API token not found. Set TRADIER_API_TOKEN env var or "
                "create ~/.alpha_hive_tradier_key. Tradier features will be disabled."
            )

        # 初始化缓存目录
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _is_token_valid(self) -> bool:
        """检查 API token 是否可用"""
        return bool(self.api_token)

    def _get_cache_path(self, cache_key: str) -> Path:
        """生成缓存文件路径"""
        return _CACHE_DIR / f"{cache_key}.json"

    def _read_cache(self, cache_key: str, ttl: int = _LIVE_CACHE_TTL) -> Optional[Dict[str, Any]]:
        """
        读取缓存数据

        Args:
            cache_key: 缓存键名
            ttl: 缓存有效期（秒）

        Returns:
            缓存数据或 None（过期或不存在）
        """
        cache_path = self._get_cache_path(cache_key)
        if not cache_path.exists():
            return None

        try:
            mtime = cache_path.stat().st_mtime
            age = time.time() - mtime
            if age > ttl:
                return None

            with open(cache_path) as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to read cache {cache_key}: {e}")
            return None

    def _write_cache(self, cache_key: str, data: Dict[str, Any]) -> None:
        """写入缓存数据"""
        try:
            cache_path = self._get_cache_path(cache_key)
            atomic_json_write(str(cache_path), data)
        except Exception as e:
            logger.debug(f"Failed to write cache {cache_key}: {e}")

    def _make_request(
        self, endpoint: str, params: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        发起 Tradier API 请求

        Args:
            endpoint: API 路径（无前缀）
            params: 查询参数

        Returns:
            JSON 响应或 None（失败）
        """
        if not self._is_token_valid():
            logger.warning("Tradier API token not available, skipping request")
            return None

        if requests is None:
            logger.warning("requests library not available")
            return None

        url = f"{self.base_url}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Tradier API request failed: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"Tradier API response decode failed: {e}")
            return None

    def fetch_options_chain(
        self, ticker: str, expiration: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取期权链数据（Call/Put 含 Greeks）

        Args:
            ticker: 股票代码（如 'NVDA'）
            expiration: 到期日期（格式 'YYYY-MM-DD'，省略则返回所有到期日）

        Returns:
            {
                "expirations": ["2026-04-17", ...],
                "quotes": {
                    "2026-04-17": {
                        "calls": [
                            {
                                "symbol": "NVDA260417C00180000",
                                "strike": 180.0,
                                "last": 5.5,
                                "bid": 5.4,
                                "ask": 5.6,
                                "volume": 1000,
                                "delta": 0.65,
                                "gamma": 0.012,
                                "theta": -0.08,
                                "vega": 0.25,
                                "iv": 0.35
                            },
                            ...
                        ],
                        "puts": [...]
                    }
                }
            }
        """
        cache_key = f"options_chain_{ticker}_{expiration or 'all'}"
        cached = self._read_cache(cache_key, ttl=_LIVE_CACHE_TTL)
        if cached:
            return cached

        endpoint = "markets/options/chains"
        params = {"symbol": ticker}
        if expiration:
            params["expiration"] = expiration

        data = self._make_request(endpoint, params)
        if data:
            self._write_cache(cache_key, data)
            return data

        return None

    def fetch_iv_for_strike(
        self,
        ticker: str,
        strike: float,
        expiration: str,
        option_type: str,  # 'call' or 'put'
    ) -> Optional[float]:
        """
        获取特定行权价的 IV

        Args:
            ticker: 股票代码
            strike: 行权价
            expiration: 到期日期（'YYYY-MM-DD'）
            option_type: 'call' 或 'put'

        Returns:
            隐含波动率（如 0.35）或 None
        """
        chain = self.fetch_options_chain(ticker, expiration)
        if not chain or "quotes" not in chain:
            return None

        quotes = chain["quotes"].get(expiration, {})
        option_list = quotes.get("calls" if option_type.lower() == "call" else "puts", [])

        for option in option_list:
            if option.get("strike") == strike:
                return option.get("iv")

        return None

    def fetch_historical_iv(
        self, ticker: str, start_date: str, end_date: str
    ) -> Optional[List[Dict[str, Any]]]:
        """
        获取历史 IV 数据（日线级别）

        Args:
            ticker: 股票代码
            start_date: 开始日期（'YYYY-MM-DD'）
            end_date: 结束日期（'YYYY-MM-DD'）

        Returns:
            [
                {
                    "date": "2026-03-27",
                    "iv_30d": 0.32,
                    "iv_60d": 0.30,
                    "iv_90d": 0.29,
                    "close": 185.50
                },
                ...
            ]
        """
        cache_key = f"historical_iv_{ticker}_{start_date}_{end_date}"
        cached = self._read_cache(cache_key, ttl=_HIST_CACHE_TTL)
        if cached:
            return cached

        endpoint = "markets/options/historical"
        params = {
            "symbol": ticker,
            "start": start_date,
            "end": end_date,
        }

        data = self._make_request(endpoint, params)
        if data:
            self._write_cache(cache_key, data)
            return data

        return None

    def fetch_greeks(
        self, ticker: str, expiration: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取完整 Greeks 数据（Delta/Gamma/Theta/Vega）

        Args:
            ticker: 股票代码
            expiration: 到期日期（可选）

        Returns:
            enriched chain dict with Greeks populated
        """
        chain = self.fetch_options_chain(ticker, expiration)
        if not chain:
            return None

        # Greeks 已在 fetch_options_chain 的响应中包含
        # 此方法用于确保完整性和便利调用
        return chain

    def cross_validate_iv(
        self, ticker: str, yf_iv: Optional[float], tradier_iv: Optional[float]
    ) -> Dict[str, Any]:
        """
        交叉验证 yfinance 和 Tradier IV 数据

        Args:
            ticker: 股票代码
            yf_iv: yfinance 隐含波动率
            tradier_iv: Tradier 隐含波动率

        Returns:
            {
                "yf_iv": 0.35,
                "tradier_iv": 0.34,
                "diff_abs": 0.01,
                "diff_pct": 2.9,  # 百分比差异
                "reliable_source": "tradier",  # 推荐使用的数据源
                "confidence": 0.95,  # 置信度 0-1
                "status": "consistent"  # "consistent", "divergent", "single_source"
            }
        """
        result = {
            "yf_iv": yf_iv,
            "tradier_iv": tradier_iv,
            "diff_abs": None,
            "diff_pct": None,
            "reliable_source": None,
            "confidence": None,
            "status": None,
        }

        # 单一数据源
        if yf_iv is None and tradier_iv is None:
            result["status"] = "no_data"
            result["confidence"] = 0.0
            return result

        if yf_iv is None:
            result["reliable_source"] = "tradier"
            result["confidence"] = 0.8
            result["status"] = "single_source"
            return result

        if tradier_iv is None:
            result["reliable_source"] = "yfinance"
            result["confidence"] = 0.75
            result["status"] = "single_source"
            return result

        # 两个数据源都可用
        diff_abs = abs(yf_iv - tradier_iv)
        diff_pct = (diff_abs / max(yf_iv, tradier_iv)) * 100 if max(yf_iv, tradier_iv) > 0 else 0

        result["diff_abs"] = round(diff_abs, 4)
        result["diff_pct"] = round(diff_pct, 2)

        # 置信度评估：差异 < 5% 视为一致
        if diff_pct < 5.0:
            result["status"] = "consistent"
            result["confidence"] = 0.95
            # Tradier Greeks 更准确（美式行权计算），优先使用
            result["reliable_source"] = "tradier"
        elif diff_pct < 15.0:
            result["status"] = "divergent"
            result["confidence"] = 0.70
            result["reliable_source"] = "tradier"  # 仍优先使用
        else:
            result["status"] = "highly_divergent"
            result["confidence"] = 0.50
            result["reliable_source"] = "tradier"

        return result

    def validate_against_yfinance(
        self, ticker: str, yf_options_data: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        验证 Tradier 数据与 yfinance 的一致性

        Args:
            ticker: 股票代码
            yf_options_data: yfinance 期权数据（格式见下）

        Returns:
            {
                "ticker": "NVDA",
                "validation_date": "2026-03-27",
                "expirations": [
                    {
                        "expiration": "2026-04-17",
                        "yf_iv_30d": 0.35,
                        "tradier_iv_30d": 0.34,
                        "iv_diff_pct": 2.9,
                        "delta_corr": 0.98,  # Delta 相关系数
                        "gamma_corr": 0.97,
                        "assessment": "reliable"
                    }
                ],
                "overall_correlation": 0.96,
                "recommendation": "use_tradier"  # or "use_yfinance", "blend"
            }
        """
        if not yf_options_data:
            logger.warning(f"No yfinance data provided for {ticker}")
            return None

        tradier_chain = self.fetch_options_chain(ticker)
        if not tradier_chain:
            logger.warning(f"Failed to fetch Tradier chain for {ticker}")
            return None

        expirations = yf_options_data.get("expirations", [])
        expiration_results = []

        for exp in expirations[:3]:  # 仅验证前 3 个到期日
            yf_iv = yf_options_data.get(f"iv_{exp}") or yf_options_data.get("iv_30d")
            tradier_iv = self.fetch_iv_for_strike(
                ticker,
                yf_options_data.get("atm_strike", 0),
                exp,
                "call"
            )

            validation = self.cross_validate_iv(ticker, yf_iv, tradier_iv)
            validation["expiration"] = exp
            expiration_results.append(validation)

        # 总体相关性评估
        consistent_count = sum(
            1 for r in expiration_results if r["status"] == "consistent"
        )
        overall_correlation = consistent_count / len(expiration_results) if expiration_results else 0

        # 推荐
        if overall_correlation > 0.9:
            recommendation = "use_tradier"
        elif overall_correlation > 0.7:
            recommendation = "blend"
        else:
            recommendation = "use_yfinance"

        return {
            "ticker": ticker,
            "validation_date": datetime.now().strftime("%Y-%m-%d"),
            "expirations": expiration_results,
            "overall_correlation": round(overall_correlation, 3),
            "recommendation": recommendation,
        }

    def health_check(self) -> bool:
        """
        检查 API 连接状态

        Returns:
            True 如果 API 可用，False 否则
        """
        if not self._is_token_valid():
            logger.warning("Tradier API token not available")
            return False

        # 尝试获取一个简单的 quote
        endpoint = "markets/quotes"
        params = {"symbols": "SPY", "greeks": "true"}

        data = self._make_request(endpoint, params)
        return data is not None


# ==================== 演示 ====================

if __name__ == "__main__":
    import sys

    # 初始化获取器
    fetcher = TradierFetcher(use_sandbox=True)

    print("=" * 60)
    print("Tradier API 集成演示")
    print("=" * 60)

    # 1. 检查 API 连接
    print("\n[1] 检查 API 连接...")
    if fetcher.health_check():
        print("✓ Tradier API 连接正常")
    else:
        print("✗ Tradier API 连接失败")
        print("   提示：设置 TRADIER_API_TOKEN 环境变量或 ~/.alpha_hive_tradier_key")
        sys.exit(1)

    # 2. 获取期权链
    ticker = "SPY"
    print(f"\n[2] 获取 {ticker} 期权链...")
    chain = fetcher.fetch_options_chain(ticker)
    if chain:
        expirations = chain.get("expirations", [])
        print(f"✓ 找到 {len(expirations)} 个到期日")
        if expirations:
            first_exp = expirations[0]
            print(f"  最近到期日：{first_exp}")

            # 3. 获取特定到期日的 Greeks
            print(f"\n[3] 获取 {first_exp} 的 Greeks...")
            chain_detail = fetcher.fetch_options_chain(ticker, first_exp)
            if chain_detail and "quotes" in chain_detail:
                calls = chain_detail["quotes"].get(first_exp, {}).get("calls", [])
                if calls:
                    sample_call = calls[0]
                    print(f"✓ 获取 {len(calls)} 个 Call 合约")
                    print(f"  样本：{sample_call.get('symbol')}")
                    print(f"    - Strike: ${sample_call.get('strike')}")
                    print(f"    - IV: {sample_call.get('iv', 'N/A'):.2%}")
                    print(f"    - Delta: {sample_call.get('delta', 'N/A')}")
                    print(f"    - Gamma: {sample_call.get('gamma', 'N/A')}")
                    print(f"    - Theta: {sample_call.get('theta', 'N/A')}")
                    print(f"    - Vega: {sample_call.get('vega', 'N/A')}")

    # 4. 交叉验证演示
    print(f"\n[4] IV 交叉验证演示...")
    yf_iv = 0.35
    tradier_iv = 0.34
    validation = fetcher.cross_validate_iv(ticker, yf_iv, tradier_iv)
    print(f"✓ 验证结果：")
    print(f"  - yfinance IV: {validation['yf_iv']:.2%}")
    print(f"  - Tradier IV: {validation['tradier_iv']:.2%}")
    print(f"  - 差异率: {validation['diff_pct']:.2f}%")
    print(f"  - 状态: {validation['status']}")
    print(f"  - 推荐数据源: {validation['reliable_source']}")
    print(f"  - 置信度: {validation['confidence']:.0%}")

    print("\n" + "=" * 60)
    print("演示完成")
    print("=" * 60)
