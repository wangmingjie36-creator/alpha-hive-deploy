"""
Quiver Quant integration — congressional trading signals and government contract analysis.

Quiver Quant (quiverquant.com) provides free/cheap access to:
- Congressional stock trading (Form 4D / periodic transaction reports)
- Government contract awards
- Lobbying data
- Political beta signals

This module fetches and analyzes congressional trading behavior to compute
political alpha signals for Alpha Hive tickers.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
import hashlib

import requests

from hive_logger import get_logger

logger = get_logger(__name__)

# Congressional trader weighting (committee relevance, historical alpha)
POLITICIAN_WEIGHTS = {
    "Nancy Pelosi": 2.0,
    "Kevin McCarthy": 1.8,
    "Mitch McConnell": 1.8,
    "Chuck Schumer": 1.8,
    "Paul Ryan": 1.6,
    "Donald Trump": 1.5,
    "Dianne Feinstein": 1.5,
    "Richard Burr": 1.5,
    "Kelly Loeffler": 1.4,
}

# Transaction size ranges (midpoint estimates)
TRANSACTION_RANGE_MIDPOINTS = {
    "$1,001 - $15,000": 8000,
    "$15,001 - $50,000": 32500,
    "$50,001 - $100,000": 75000,
    "$100,001 - $250,000": 175000,
    "$250,001 - $500,000": 375000,
    "$500,001 - $1,000,000": 750000,
    "$1,000,001 - $5,000,000": 3000000,
    "$5,000,001 - $25,000,000": 15000000,
    "$25,000,001 - $50,000,000": 37500000,
    "$50,000,001 - $100,000,000": 75000000,
    "Over $100,000,000": 150000000,
}


class QuiverFetcher:
    """
    Quiver Quant API 集成，获取国会交易信号和政策 alpha 指标。

    主要功能：
    - 获取国会议员股票交易记录（Form 4D）
    - 计算政治 beta 信号（买卖情绪、关键交易者）
    - 获取政府合同数据
    - 计算政策 alpha（国会交易 + 合同 + 游说综合信号）
    """

    def __init__(self):
        """初始化 Quiver API 客户端和缓存目录。"""
        self.api_token = self._load_api_token()
        self.base_url = "https://api.quiverquant.com/beta/"
        self.cache_dir = Path("cache/quiver")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.available = self.api_token is not None

        if not self.available:
            logger.warning(
                "Quiver API token not found (env var QUIVER_API_TOKEN or ~/.alpha_hive_quiver_key). "
                "Congressional trading signals will be unavailable."
            )

    def _load_api_token(self) -> Optional[str]:
        """
        从环境变量或文件加载 API Token。

        优先级：
        1. 环境变量 QUIVER_API_TOKEN
        2. 文件 ~/.alpha_hive_quiver_key
        3. None（不可用）
        """
        # Try environment variable first
        token = os.environ.get("QUIVER_API_TOKEN")
        if token:
            return token.strip()

        # Try file in home directory
        key_file = Path.home() / ".alpha_hive_quiver_key"
        if key_file.exists():
            try:
                with open(key_file, "r") as f:
                    token = f.read().strip()
                    if token:
                        return token
            except Exception as e:
                logger.debug(f"Failed to read Quiver key file: {e}")

        return None

    def _get_cache_path(self, endpoint: str, ticker: Optional[str] = None) -> Path:
        """生成缓存文件路径。"""
        if ticker:
            cache_key = f"{endpoint}_{ticker}".lower()
        else:
            cache_key = endpoint.lower()

        # Hash for long filenames
        cache_hash = hashlib.md5(cache_key.encode()).hexdigest()[:8]
        return self.cache_dir / f"{cache_key}_{cache_hash}.json"

    def _load_cache(
        self, cache_path: Path, ttl_seconds: int = 3600
    ) -> Optional[Dict]:
        """
        从缓存加载数据（如果未过期）。

        Args:
            cache_path: 缓存文件路径
            ttl_seconds: 缓存有效期（秒）

        Returns:
            缓存的数据或 None（如果缺失或过期）
        """
        if not cache_path.exists():
            return None

        try:
            mtime = cache_path.stat().st_mtime
            age_seconds = time.time() - mtime
            if age_seconds > ttl_seconds:
                return None

            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Failed to load cache {cache_path}: {e}")
            return None

    def _save_cache(self, cache_path: Path, data: Any) -> None:
        """原子写入缓存文件。"""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(cache_path)
        except Exception as e:
            logger.warning(f"Failed to save cache {cache_path}: {e}")

    def _api_request(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
        timeout: int = 10,
    ) -> Optional[Dict]:
        """
        发送 Quiver API 请求。

        Args:
            endpoint: API 端点（相对于 base_url）
            params: 查询参数
            timeout: 请求超时（秒）

        Returns:
            API 响应 JSON 或 None（如果失败）
        """
        if not self.available:
            return None

        url = self.base_url + endpoint
        headers = {"Authorization": f"Bearer {self.api_token}"}
        params = params or {}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Quiver API request failed: {endpoint} - {e}")
            return None

    def fetch_congressional_trades(
        self, ticker: Optional[str] = None, days: int = 90
    ) -> List[Dict]:
        """
        获取国会议员股票交易记录。

        Args:
            ticker: 股票代码（可选，留空则获取所有交易）
            days: 回溯天数（默认 90 天）

        Returns:
            交易列表，每个元素格式：
            {
                "politician": str,
                "party": str ("D" / "R" / "I"),
                "transaction_type": str ("Buy" / "Sell"),
                "amount_range": str,
                "date": str (YYYY-MM-DD),
                "ticker": str,
                "disclosure_date": str (YYYY-MM-DD),
                "amount_estimate": float (范围中点),
            }
        """
        cache_path = self._get_cache_path("congressional_trades", ticker)
        cached = self._load_cache(cache_path, ttl_seconds=4 * 3600)
        if cached is not None:
            return cached

        # API endpoint: /congressmen/stock-trades
        params = {
            "days": days,
        }
        if ticker:
            params["ticker"] = ticker.upper()

        result = self._api_request("congressmen/stock-trades", params=params)
        if result is None:
            return []

        trades = []
        for trade in result:
            amount_estimate = TRANSACTION_RANGE_MIDPOINTS.get(
                trade.get("amount_range", ""), 50000
            )
            trades.append(
                {
                    "politician": trade.get("politician", ""),
                    "party": trade.get("party", ""),
                    "transaction_type": trade.get("transaction_type", ""),
                    "amount_range": trade.get("amount_range", ""),
                    "amount_estimate": amount_estimate,
                    "date": trade.get("date", ""),
                    "ticker": trade.get("ticker", "").upper(),
                    "disclosure_date": trade.get("disclosure_date", ""),
                }
            )

        self._save_cache(cache_path, trades)
        return trades

    def fetch_recent_trades(self, days: int = 30) -> List[Dict]:
        """
        获取最近 N 天的所有国会交易。

        Args:
            days: 回溯天数

        Returns:
            交易列表
        """
        return self.fetch_congressional_trades(ticker=None, days=days)

    def calculate_congressional_signal(self, ticker: str) -> Dict:
        """
        计算单只股票的国会交易信号。

        Args:
            ticker: 股票代码

        Returns:
            {
                "net_sentiment": float (-1.0 to +1.0, 负=卖, 正=买),
                "buy_count": int,
                "sell_count": int,
                "notable_traders": list[str],
                "total_estimated_value": float,
                "signal": str ("strong_buy" / "buy" / "neutral" / "sell" / "strong_sell"),
                "confidence": float (0-1, 基于交易数和新近度),
                "_unavailable": bool (如果无法获取数据),
            }
        """
        ticker = ticker.upper()

        # Fetch trades from past 90 days
        trades = self.fetch_congressional_trades(ticker=ticker, days=90)

        if not trades:
            return {
                "net_sentiment": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "notable_traders": [],
                "total_estimated_value": 0.0,
                "signal": "neutral",
                "confidence": 0.0,
                "_unavailable": True,
            }

        buy_trades = []
        sell_trades = []
        notable = set()
        total_value = 0.0

        now = datetime.now()

        for trade in trades:
            is_buy = trade["transaction_type"].lower() == "buy"
            amount = trade["amount_estimate"]
            total_value += amount

            # Recency weighting: trades within 7 days get higher weight
            trade_date = datetime.fromisoformat(trade["date"])
            days_ago = (now - trade_date).days
            recency_weight = max(0.5, 2.0 - (days_ago / 7.0))

            # Politician weight
            pol_weight = POLITICIAN_WEIGHTS.get(trade["politician"], 1.0)

            weighted_amount = amount * recency_weight * pol_weight

            if is_buy:
                buy_trades.append((trade, weighted_amount))
            else:
                sell_trades.append((trade, weighted_amount))

            notable.add(trade["politician"])

        buy_value = sum(t[1] for t in buy_trades)
        sell_value = sum(t[1] for t in sell_trades)
        total_weighted = buy_value + sell_value

        if total_weighted > 0:
            net_sentiment = (buy_value - sell_value) / total_weighted
        else:
            net_sentiment = 0.0

        # Confidence: based on trade count and recency
        trade_count = len(trades)
        confidence = min(
            1.0, (trade_count / 10.0) * 0.8 + 0.2
        )  # Cap at 1.0, floor at 0.2

        # Signal classification
        if net_sentiment > 0.5:
            signal = "strong_buy"
        elif net_sentiment > 0.1:
            signal = "buy"
        elif net_sentiment < -0.5:
            signal = "strong_sell"
        elif net_sentiment < -0.1:
            signal = "sell"
        else:
            signal = "neutral"

        # Get top traders (by total value)
        top_traders = []
        trader_totals = {}
        for trade in trades:
            pol = trade["politician"]
            trader_totals[pol] = trader_totals.get(pol, 0) + trade["amount_estimate"]

        for pol, _ in sorted(trader_totals.items(), key=lambda x: x[1], reverse=True)[
            :5
        ]:
            top_traders.append(pol)

        return {
            "net_sentiment": round(net_sentiment, 3),
            "buy_count": len(buy_trades),
            "sell_count": len(sell_trades),
            "notable_traders": top_traders,
            "total_estimated_value": round(total_value, 2),
            "signal": signal,
            "confidence": round(confidence, 3),
        }

    def fetch_government_contracts(
        self, ticker: Optional[str] = None, days: int = 180
    ) -> List[Dict]:
        """
        获取政府合同数据。

        Args:
            ticker: 公司股票代码（可选）
            days: 回溯天数（默认 180 天）

        Returns:
            合同列表，每个元素格式：
            {
                "company": str,
                "ticker": str,
                "contract_value": float,
                "agency": str,
                "contract_date": str (YYYY-MM-DD),
                "description": str,
            }
        """
        cache_path = self._get_cache_path("government_contracts", ticker)
        cached = self._load_cache(cache_path, ttl_seconds=24 * 3600)
        if cached is not None:
            return cached

        params = {"days": days}
        if ticker:
            params["ticker"] = ticker.upper()

        result = self._api_request("government/contracts", params=params)
        if result is None:
            return []

        contracts = []
        for contract in result:
            contracts.append(
                {
                    "company": contract.get("company", ""),
                    "ticker": contract.get("ticker", "").upper(),
                    "contract_value": float(contract.get("contract_value", 0)),
                    "agency": contract.get("agency", ""),
                    "contract_date": contract.get("contract_date", ""),
                    "description": contract.get("description", ""),
                }
            )

        self._save_cache(cache_path, contracts)
        return contracts

    def calculate_policy_alpha(self, ticker: str) -> Dict:
        """
        计算综合政策 alpha 信号（国会交易 + 政府合同 + 游说）。

        Args:
            ticker: 股票代码

        Returns:
            {
                "congressional_signal": str,
                "congressional_sentiment": float,
                "congressional_confidence": float,
                "contract_signal": str,
                "recent_contract_count": int,
                "recent_contract_value": float,
                "policy_alpha_score": float (-1.0 to +1.0),
                "policy_signal": str ("strong_positive" / "positive" / "neutral" / "negative" / "strong_negative"),
                "_unavailable": bool,
            }
        """
        ticker = ticker.upper()

        # Get congressional signal
        cong_sig = self.calculate_congressional_signal(ticker)
        cong_sentiment = cong_sig.get("net_sentiment", 0.0)
        cong_conf = cong_sig.get("confidence", 0.0)
        cong_signal = cong_sig.get("signal", "neutral")

        # Get government contracts
        contracts = self.fetch_government_contracts(ticker=ticker, days=180)
        recent_contracts = contracts[:5] if contracts else []
        contract_count = len(contracts)
        contract_value = sum(c["contract_value"] for c in contracts)

        # Contracts signal: significant contracts are positive
        contract_signal = "neutral"
        if contract_value > 100_000_000:
            contract_signal = "strong_positive"
        elif contract_value > 50_000_000:
            contract_signal = "positive"
        elif contract_value > 0:
            contract_signal = "positive"

        # Composite policy alpha score
        # Weight: 60% congressional, 40% contracts
        alpha_score = 0.6 * cong_sentiment + 0.4 * (
            1.0 if contract_signal == "strong_positive" else
            0.5 if contract_signal == "positive" else
            0.0
        ) - 0.4

        alpha_score = max(-1.0, min(1.0, alpha_score))

        # Policy signal
        if alpha_score > 0.5:
            policy_signal = "strong_positive"
        elif alpha_score > 0.1:
            policy_signal = "positive"
        elif alpha_score < -0.5:
            policy_signal = "strong_negative"
        elif alpha_score < -0.1:
            policy_signal = "negative"
        else:
            policy_signal = "neutral"

        return {
            "congressional_signal": cong_signal,
            "congressional_sentiment": cong_sentiment,
            "congressional_confidence": cong_conf,
            "contract_signal": contract_signal,
            "recent_contract_count": contract_count,
            "recent_contract_value": round(contract_value, 2),
            "policy_alpha_score": round(alpha_score, 3),
            "policy_signal": policy_signal,
        }

    def format_for_scout_discovery(self, ticker: str) -> Optional[str]:
        """
        格式化国会交易信息供 Scout Bee discovery 使用（一行文本）。

        Args:
            ticker: 股票代码

        Returns:
            单行发现文本，例如：
            "🏛️ 3 国会议员近30天买入 NVDA（Nancy Pelosi +$500K-1M, Dan Crenshaw +$50K-100K）— 政策Alpha信号: 看多"
            或 None（如果无可用数据）
        """
        ticker = ticker.upper()
        trades = self.fetch_congressional_trades(ticker=ticker, days=30)

        if not trades:
            return None

        # Get top 3 most recent/largest trades
        trades_sorted = sorted(
            trades,
            key=lambda x: (
                1 if x["transaction_type"].lower() == "buy" else -1,
                x["amount_estimate"],
                x["date"],
            ),
            reverse=True,
        )[:3]

        # Format trader details
        trader_details = []
        for trade in trades_sorted:
            pol = trade["politician"]
            action = "↑" if trade["transaction_type"].lower() == "buy" else "↓"
            amount = trade["amount_range"]
            trader_details.append(f"{pol} {action} {amount}")

        traders_text = ", ".join(trader_details)

        # Get signal
        sig = self.calculate_congressional_signal(ticker)
        signal_text = "看多" if sig["signal"] in ["buy", "strong_buy"] else "看空" if sig["signal"] in ["sell", "strong_sell"] else "中性"

        return f"🏛️ {len(trades)} 国会议员近30天交易 {ticker}（{traders_text}）— 政治Alpha: {signal_text}"

    def format_congressional_card_html(self, ticker: str) -> Optional[str]:
        """
        生成国会交易卡片 HTML，供报告嵌入。

        Args:
            ticker: 股票代码

        Returns:
            HTML 字符串或 None（如果无可用数据）
        """
        ticker = ticker.upper()
        sig = self.calculate_congressional_signal(ticker)

        if sig.get("_unavailable"):
            return None

        trades = self.fetch_congressional_trades(ticker=ticker, days=30)
        if not trades:
            return None

        # Build trader rows
        trader_rows = []
        for trade in trades[:10]:  # Top 10 trades
            pol = trade["politician"]
            action = "📈 BUY" if trade["transaction_type"].lower() == "buy" else "📉 SELL"
            amount = trade["amount_range"]
            date_str = trade["date"]
            party = "🔴 R" if trade["party"] == "R" else "🔵 D" if trade["party"] == "D" else "⚪ I"

            trader_rows.append(
                f'<tr><td>{pol}</td><td>{party}</td><td>{action}</td><td>{amount}</td><td>{date_str}</td></tr>'
            )

        trader_table = "\n".join(trader_rows)

        # Signal color
        signal = sig["signal"]
        signal_color = (
            "#4CAF50"
            if signal in ["buy", "strong_buy"]
            else "#FF6B6B"
            if signal in ["sell", "strong_sell"]
            else "#FFC107"
        )
        signal_text = "看多" if signal in ["buy", "strong_buy"] else "看空" if signal in ["sell", "strong_sell"] else "中性"

        html = f"""
<div style="border: 2px solid {signal_color}; border-radius: 8px; padding: 12px; margin: 10px 0; background: #f9f9f9;">
    <div style="font-weight: bold; color: {signal_color}; margin-bottom: 8px; font-size: 13px;">
        🏛️ 国会交易信号 — {signal_text.upper()}
    </div>
    <div style="font-size: 12px; margin-bottom: 8px; color: #555;">
        近30天: {sig['buy_count']} 买 / {sig['sell_count']} 卖 | 信心: {sig['confidence']:.0%} | 净情绪: {sig['net_sentiment']:+.2f}
    </div>
    <table style="width: 100%; font-size: 11px; border-collapse: collapse; margin-top: 8px;">
        <thead>
            <tr style="border-bottom: 1px solid #ddd; background: #f0f0f0;">
                <th style="text-align: left; padding: 4px;">议员</th>
                <th style="text-align: center; padding: 4px;">党派</th>
                <th style="text-align: center; padding: 4px;">行动</th>
                <th style="text-align: right; padding: 4px;">金额范围</th>
                <th style="text-align: right; padding: 4px;">日期</th>
            </tr>
        </thead>
        <tbody>
            {trader_table}
        </tbody>
    </table>
</div>
"""
        return html


def demo():
    """演示 QuiverFetcher 功能。"""
    fetcher = QuiverFetcher()

    if not fetcher.available:
        print("⚠️  Quiver API token not available. Skipping demo.")
        return

    test_tickers = ["NVDA", "TSLA", "META"]

    for ticker in test_tickers:
        print(f"\n{'='*60}")
        print(f"Ticker: {ticker}")
        print(f"{'='*60}")

        # Congressional signal
        sig = fetcher.calculate_congressional_signal(ticker)
        print(f"\nCongressional Signal:")
        print(f"  Signal: {sig['signal']}")
        print(f"  Net Sentiment: {sig['net_sentiment']:.3f}")
        print(f"  Buy/Sell: {sig['buy_count']}/{sig['sell_count']}")
        print(f"  Confidence: {sig['confidence']:.1%}")
        print(f"  Notable Traders: {', '.join(sig['notable_traders'][:3])}")

        # Policy alpha
        policy = fetcher.calculate_policy_alpha(ticker)
        print(f"\nPolicy Alpha:")
        print(f"  Score: {policy['policy_alpha_score']:.3f}")
        print(f"  Signal: {policy['policy_signal']}")
        print(f"  Contract Value (180d): ${policy['recent_contract_value']:,.0f}")

        # Scout discovery
        discovery = fetcher.format_for_scout_discovery(ticker)
        if discovery:
            print(f"\nScout Discovery:")
            print(f"  {discovery}")

        # Recent trades
        trades = fetcher.fetch_congressional_trades(ticker=ticker, days=30)
        print(f"\nRecent Trades (last 30 days): {len(trades)}")
        for trade in trades[:3]:
            print(
                f"  {trade['politician']} ({trade['party']}) "
                f"{trade['transaction_type']} {trade['ticker']} "
                f"{trade['amount_range']} on {trade['date']}"
            )


if __name__ == "__main__":
    demo()
