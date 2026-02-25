#!/usr/bin/env python3
"""
🔧 Alpha Hive 代码生成器 - Phase 3 P1
自动生成数据爬取、分析、可视化代码
"""

from typing import Dict, Any, List, Optional


class CodeGenerator:
    """代码生成助手"""

    @staticmethod
    def generate_data_fetch(source: str, params: Dict[str, Any]) -> str:
        """
        生成数据爬取脚本

        Args:
            source: 数据源（"yfinance", "sec", "polymarket", "stocktwits"）
            params: 参数字典

        Returns:
            Python 代码字符串
        """
        if source == "yfinance":
            return CodeGenerator._generate_yfinance(params)
        elif source == "sec":
            return CodeGenerator._generate_sec_fetch(params)
        elif source == "polymarket":
            return CodeGenerator._generate_polymarket(params)
        elif source == "stocktwits":
            return CodeGenerator._generate_stocktwits(params)
        else:
            raise ValueError(f"不支持的数据源: {source}")

    @staticmethod
    def _generate_yfinance(params: Dict) -> str:
        """生成 yfinance 数据爬取代码"""
        ticker = params.get("ticker", "NVDA")
        period = params.get("period", "1mo")
        interval = params.get("interval", "1d")

        code = f'''
import yfinance as yf
import json

# 获取股票数据
ticker = "{ticker}"
data = yf.download(ticker, period="{period}", interval="{interval}")

# 获取股票信息
stock = yf.Ticker(ticker)
info = stock.info

# 构建输出
result = {{
    "ticker": ticker,
    "current_price": info.get("currentPrice", "N/A"),
    "52_week_high": info.get("fiftyTwoWeekHigh", "N/A"),
    "52_week_low": info.get("fiftyTwoWeekLow", "N/A"),
    "market_cap": info.get("marketCap", "N/A"),
    "pe_ratio": info.get("trailingPE", "N/A"),
    "volume": info.get("volume", "N/A"),
    "avg_volume": info.get("averageVolume", "N/A"),
    "recent_close": float(data["Close"].iloc[-1]) if len(data) > 0 else None,
    "recent_volume": int(data["Volume"].iloc[-1]) if len(data) > 0 else None
}}

print(json.dumps(result, indent=2))
'''
        return code.strip()

    @staticmethod
    def _generate_sec_fetch(params: Dict) -> str:
        """生成 SEC Form 4/13F 爬取代码"""
        ticker = params.get("ticker", "NVDA")
        form_type = params.get("form_type", "4")  # 4 或 13F

        code = f'''
import requests
import json
from datetime import datetime, timedelta

ticker = "{ticker}"
form_type = "{form_type}"

# SEC EDGAR API 端点
url = f"https://data.sec.gov/api/xquery"

params = {{
    "action": "getcompany",
    "CIK": ticker,
    "type": form_type,
    "dateb": "",
    "owner": "exclude",
    "count": "40",
    "search_text": ""
}}

try:
    response = requests.get(url, params=params, headers={{"User-Agent": "Mozilla/5.0"}})

    if response.status_code == 200:
        data = response.json()
        print(json.dumps({{
            "ticker": ticker,
            "form_type": form_type,
            "filings": data.get("filings", [])[:5],  # 最近 5 条
            "last_updated": datetime.now().isoformat()
        }}, indent=2))
    else:
        print(json.dumps({{"error": f"HTTP {{response.status_code}}"}}))
except (ConnectionError, TimeoutError, OSError, ValueError) as e:
    print(json.dumps({{"error": str(e)}}))
'''
        return code.strip()

    @staticmethod
    def _generate_polymarket(params: Dict) -> str:
        """生成 Polymarket 赔率爬取代码"""
        keyword = params.get("keyword", "NVDA earnings")

        code = f'''
import requests
import json

# Polymarket 公开 API（无需认证）
url = "https://clob.polymarket.com/markets"

params = {{
    "closed": False,
    "limit": 100
}}

try:
    response = requests.get(url, params=params)

    if response.status_code == 200:
        markets = response.json()

        # 过滤相关市场
        keyword = "{keyword}".lower()
        filtered = [m for m in markets if keyword in m.get("question", "").lower()]

        print(json.dumps({{
            "keyword": "{keyword}",
            "total_markets": len(markets),
            "filtered_count": len(filtered),
            "markets": filtered[:5]
        }}, indent=2))
    else:
        print(json.dumps({{"error": f"HTTP {{response.status_code}}"}}))
except (ConnectionError, TimeoutError, OSError, ValueError) as e:
    print(json.dumps({{"error": str(e)}}))
'''
        return code.strip()

    @staticmethod
    def _generate_stocktwits(params: Dict) -> str:
        """生成 StockTwits 情绪爬取代码"""
        ticker = params.get("ticker", "NVDA")

        code = f'''
import requests
import json

ticker = "{ticker}"

# StockTwits API
url = f"https://api.stocktwits.com/api/2/streams/symbols/{{ticker}}.json"

try:
    response = requests.get(url, headers={{"User-Agent": "Mozilla/5.0"}})

    if response.status_code == 200:
        data = response.json()
        messages = data.get("messages", [])

        # 简单情绪分析
        bullish_count = sum(1 for m in messages if "bullish" in m.get("body", "").lower())
        bearish_count = sum(1 for m in messages if "bearish" in m.get("body", "").lower())

        print(json.dumps({{
            "ticker": ticker,
            "total_messages": len(messages),
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "bullish_ratio": round(bullish_count / len(messages) * 100, 2) if messages else 0
        }}, indent=2))
    else:
        print(json.dumps({{"error": f"HTTP {{response.status_code}}"}}))
except (ConnectionError, TimeoutError, OSError, ValueError) as e:
    print(json.dumps({{"error": str(e)}}))
'''
        return code.strip()

    @staticmethod
    def generate_analysis(analysis_type: str, params: Dict) -> str:
        """
        生成数据分析脚本

        Args:
            analysis_type: 分析类型（"technical", "sentiment", "momentum"）
            params: 参数字典

        Returns:
            Python 代码字符串
        """
        if analysis_type == "technical":
            return CodeGenerator._generate_technical_analysis(params)
        elif analysis_type == "sentiment":
            return CodeGenerator._generate_sentiment_analysis(params)
        elif analysis_type == "momentum":
            return CodeGenerator._generate_momentum_analysis(params)
        else:
            raise ValueError(f"不支持的分析类型: {analysis_type}")

    @staticmethod
    def _generate_technical_analysis(params: Dict) -> str:
        """生成技术分析代码"""
        ticker = params.get("ticker", "NVDA")
        period = params.get("period", "1mo")

        code = f'''
import yfinance as yf
import pandas as pd
import json

ticker = "{ticker}"
period = "{period}"

# 下载数据
data = yf.download(ticker, period=period)

# 计算技术指标
df = data.copy()
df["SMA_20"] = df["Close"].rolling(20).mean()
df["SMA_50"] = df["Close"].rolling(50).mean()
df["RSI"] = 100 - (100 / (1 + (df["Close"].diff().apply(lambda x: x if x > 0 else 0).rolling(14).mean() /
                                df["Close"].diff().apply(lambda x: -x if x < 0 else 0).rolling(14).mean())))

# 获取最新指标
latest = df.iloc[-1]

result = {{
    "ticker": ticker,
    "price": float(latest["Close"]),
    "sma_20": float(latest["SMA_20"]) if pd.notna(latest["SMA_20"]) else None,
    "sma_50": float(latest["SMA_50"]) if pd.notna(latest["SMA_50"]) else None,
    "rsi": float(latest["RSI"]) if pd.notna(latest["RSI"]) else None,
    "signal": "超买" if latest["RSI"] > 70 else ("超卖" if latest["RSI"] < 30 else "中性")
}}

print(json.dumps(result, indent=2))
'''
        return code.strip()

    @staticmethod
    def _generate_sentiment_analysis(params: Dict) -> str:
        """生成情绪分析代码"""
        ticker = params.get("ticker", "NVDA")

        code = f'''
import requests
import json
from collections import Counter

ticker = "{ticker}"

try:
    # 从 StockTwits 获取情绪
    url = f"https://api.stocktwits.com/api/2/streams/symbols/{{ticker}}.json"
    response = requests.get(url)
    data = response.json()

    messages = data.get("messages", [])

    # 提取情绪关键词
    sentiments = []
    for msg in messages[:100]:
        body = msg.get("body", "").lower()
        if any(word in body for word in ["bullish", "moon", "pump", "buy", "long"]):
            sentiments.append("bullish")
        elif any(word in body for word in ["bearish", "dump", "sell", "short", "crash"]):
            sentiments.append("bearish")
        else:
            sentiments.append("neutral")

    sentiment_counts = Counter(sentiments)
    total = len(sentiments)

    result = {{
        "ticker": ticker,
        "total_messages": total,
        "bullish": sentiment_counts.get("bullish", 0),
        "bearish": sentiment_counts.get("bearish", 0),
        "neutral": sentiment_counts.get("neutral", 0),
        "bullish_ratio": round(sentiment_counts.get("bullish", 0) / total * 100, 2) if total > 0 else 0,
        "sentiment_score": round((sentiment_counts.get("bullish", 0) - sentiment_counts.get("bearish", 0)) / total * 100, 2) if total > 0 else 0
    }}

    print(json.dumps(result, indent=2))
except (ConnectionError, TimeoutError, OSError, ValueError) as e:
    print(json.dumps({{"error": str(e)}}))
'''
        return code.strip()

    @staticmethod
    def _generate_momentum_analysis(params: Dict) -> str:
        """生成动量分析代码"""
        ticker = params.get("ticker", "NVDA")

        code = f'''
import yfinance as yf
import pandas as pd
import json

ticker = "{ticker}"

# 下载最近 3 个月数据
data = yf.download(ticker, period="3mo")

# 计算动量指标
df = data.copy()
df["Daily_Return"] = df["Close"].pct_change()
df["Momentum"] = df["Close"] - df["Close"].shift(10)

# 计算统计指标
recent_momentum = df["Momentum"].iloc[-1]
avg_momentum = df["Momentum"].mean()
momentum_std = df["Momentum"].std()

result = {{
    "ticker": ticker,
    "current_price": float(df["Close"].iloc[-1]),
    "momentum": float(recent_momentum),
    "avg_momentum": float(avg_momentum),
    "momentum_std": float(momentum_std),
    "z_score": float((recent_momentum - avg_momentum) / momentum_std) if momentum_std > 0 else 0,
    "trend": "加速上升" if recent_momentum > avg_momentum + momentum_std else (
             "加速下降" if recent_momentum < avg_momentum - momentum_std else "平稳"
    )
}}

print(json.dumps(result, indent=2))
'''
        return code.strip()

    @staticmethod
    def generate_visualization(chart_type: str, params: Dict) -> str:
        """
        生成可视化代码

        Args:
            chart_type: 图表类型（"line", "candlestick", "heatmap"）
            params: 参数字典

        Returns:
            Python 代码字符串
        """
        if chart_type == "line":
            return CodeGenerator._generate_line_chart(params)
        elif chart_type == "candlestick":
            return CodeGenerator._generate_candlestick_chart(params)
        elif chart_type == "heatmap":
            return CodeGenerator._generate_heatmap_chart(params)
        else:
            raise ValueError(f"不支持的图表类型: {chart_type}")

    @staticmethod
    def _generate_line_chart(params: Dict) -> str:
        """生成折线图代码"""
        ticker = params.get("ticker", "NVDA")
        period = params.get("period", "1mo")

        code = f'''
import yfinance as yf
import matplotlib.pyplot as plt

ticker = "{ticker}"
period = "{period}"

# 下载数据
data = yf.download(ticker, period=period)

# 创建图表
plt.figure(figsize=(14, 7))
plt.plot(data.index, data["Close"], label="Close Price", linewidth=2, color="blue")
plt.plot(data.index, data["Close"].rolling(20).mean(), label="SMA 20", linestyle="--", color="orange")

plt.title(f"{{ticker}} Price Trend")
plt.xlabel("Date")
plt.ylabel("Price (USD)")
plt.legend()
plt.grid(True, alpha=0.3)
plt.tight_layout()

# 保存
output_path = "/tmp/alpha_hive_sandbox/output/{{ticker}}_line_chart.png"
plt.savefig(output_path, dpi=150)
print(f"Chart saved to {{output_path}}")
'''
        return code.strip()

    @staticmethod
    def _generate_candlestick_chart(params: Dict) -> str:
        """生成蜡烛图代码"""
        ticker = params.get("ticker", "NVDA")

        code = f'''
import yfinance as yf
import plotly.graph_objects as go

ticker = "{ticker}"

# 下载数据
data = yf.download(ticker, period="1mo")

# 创建蜡烛图
fig = go.Figure(data=[go.Candlestick(
    x=data.index,
    open=data["Open"],
    high=data["High"],
    low=data["Low"],
    close=data["Close"]
)])

fig.update_layout(
    title=f"{{ticker}} Candlestick Chart",
    yaxis_title="Stock Price (USD)",
    xaxis_title="Date",
    template="plotly_white",
    xaxis_rangeslider_visible=False
)

# 保存
output_path = "/tmp/alpha_hive_sandbox/output/{{ticker}}_candlestick.html"
fig.write_html(output_path)
print(f"Chart saved to {{output_path}}")
'''
        return code.strip()

    @staticmethod
    def _generate_heatmap_chart(params: Dict) -> str:
        """生成热力图代码"""
        tickers = params.get("tickers", ["NVDA", "TSLA", "AMD", "MSFT"])

        code = f'''
import yfinance as yf
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

tickers = {tickers}

# 下载收益率数据
returns = pd.DataFrame()
for ticker in tickers:
    data = yf.download(ticker, period="1mo", progress=False)
    returns[ticker] = data["Close"].pct_change() * 100

# 计算相关矩阵
corr_matrix = returns.corr()

# 创建热力图
plt.figure(figsize=(10, 8))
sns.heatmap(corr_matrix, annot=True, cmap="coolwarm", center=0, vmin=-1, vmax=1)
plt.title("Stock Returns Correlation Heatmap")
plt.tight_layout()

# 保存
output_path = "/tmp/alpha_hive_sandbox/output/correlation_heatmap.png"
plt.savefig(output_path, dpi=150)
print(f"Chart saved to {{output_path}}")
'''
        return code.strip()
