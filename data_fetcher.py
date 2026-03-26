"""
🐝 Alpha Hive - 实时数据获取系统
支持多源数据采集：StockTwits、Polymarket、Yahoo Finance、Google Trends 等
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from hive_logger import PATHS, get_logger, atomic_json_write, SafeJSONEncoder

_log = get_logger("data_fetcher")

# TTL 来源：config.py CACHE_CONFIG（统一管理，避免硬编码散落）
try:
    from config import get_cache_ttl as _ttl
except ImportError:
    def _ttl(source: str) -> int:  # type: ignore[misc]
        """降级: config 不可用时使用保守默认值"""
        return {"stocktwits_legacy": 3600, "polymarket": 300, "yahoo_finance": 300,
                "google_trends": 86400, "sec_edgar": 604800, "seeking_alpha": 86400,
                }.get(source, 300)


class CacheManager:
    """缓存管理器 - 避免重复请求"""

    def __init__(self, cache_dir: str = str(PATHS.cache_dir)):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def get_cache_key(self, source: str, ticker: str) -> str:
        """生成缓存键"""
        return f"{source}#{ticker}".lower()

    def load(self, key: str, ttl: int = 3600) -> Optional[Dict]:
        """
        从缓存加载数据

        Args:
            key: 缓存键
            ttl: 过期时间（秒）

        Returns:
            缓存数据或 None
        """
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        if not os.path.exists(cache_file):
            return None

        # 检查过期时间
        mod_time = os.path.getmtime(cache_file)
        if time.time() - mod_time > ttl:
            try:
                os.remove(cache_file)
            except OSError as e:
                _log.debug("缓存文件清理失败 %s: %s", cache_file, e)
            return None

        try:
            with open(cache_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("❌ 缓存加载失败 %s: %s", key, e)
            return None

    def save(self, key: str, data: Dict) -> bool:
        """保存数据到缓存"""
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        try:
            atomic_json_write(cache_file, data, indent=2)
            return True
        except (OSError, TypeError) as e:
            _log.error("❌ 缓存保存失败 %s: %s", key, e)
            return False


class DataFetcher:
    """核心数据获取类"""

    def __init__(self):
        self.cache = CacheManager()
        self.session_start = datetime.now()
        # ⭐ 优化 2：添加 24 小时 TTL 缓存（节省数据采集 token）
        self.api_cache_ttl = 24 * 3600  # 24 小时
        self.cache_hits = 0
        self.cache_misses = 0

    # ==================== StockTwits 数据 ====================

    def get_stocktwits_metrics(self, ticker: str) -> Dict:
        """
        获取 StockTwits 数据

        Returns:
            {
                "messages_per_day": int,
                "bullish_ratio": float (0-1),
                "sentiment_trend": str,
                "last_updated": str,
            }
        """
        cache_key = self.cache.get_cache_key("stocktwits", ticker)
        cached = self.cache.load(cache_key, ttl=_ttl("stocktwits_legacy"))
        if cached:
            _log.info("📦 使用 StockTwits 缓存: %s", ticker)
            return cached

        try:
            # 实际实现：调用 StockTwits API
            # 这里提供示例实现
            _log.info("🔄 获取 StockTwits 数据: %s", ticker)

            # StockTwits API 实时路径已迁移到 stocktwits_sentiment.py

            # 暂时返回合理的示例数据
            metrics = {
                "messages_per_day": self._estimate_stocktwits_volume(ticker),
                "bullish_ratio": self._estimate_bullish_ratio(ticker),
                "sentiment_trend": "positive",
                "last_updated": datetime.now().isoformat(),
            }

            self.cache.save(cache_key, metrics)
            return metrics

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.error("❌ StockTwits 获取失败 %s: %s", ticker, e)
            return {"messages_per_day": 0, "bullish_ratio": 0.5}

    # ==================== Polymarket 赔率 ====================

    def get_polymarket_odds(self, ticker: str) -> Dict:
        """
        获取 Polymarket 预测市场赔率

        Returns:
            {
                "event": str,
                "yes_odds": float (0-1),
                "no_odds": float (0-1),
                "volume_24h": float,
                "odds_change_24h": float (%),
            }
        """
        cache_key = self.cache.get_cache_key("polymarket", ticker)
        cached = self.cache.load(cache_key, ttl=_ttl("polymarket"))
        if cached:
            _log.info("📦 使用 Polymarket 缓存: %s", ticker)
            return cached

        try:
            _log.info("🔄 获取 Polymarket 赔率: %s", ticker)

            # Polymarket 实时路径已迁移到 polymarket_client.py

            # 示例数据
            odds_data = {
                "event": f"{ticker} Q1 2026 Earnings Beat",
                "yes_odds": self._estimate_yes_odds(ticker),
                "no_odds": 0.0,  # 自动计算
                "volume_24h": self._estimate_volume(ticker),
                "odds_change_24h": self._estimate_odds_change(ticker),
                "last_updated": datetime.now().isoformat(),
            }
            odds_data["no_odds"] = 1.0 - odds_data["yes_odds"]

            self.cache.save(cache_key, odds_data)
            return odds_data

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.error("❌ Polymarket 获取失败 %s: %s", ticker, e)
            return {"yes_odds": 0.5, "no_odds": 0.5}

    # ==================== Yahoo Finance 数据 ====================

    def get_yahoo_finance_metrics(self, ticker: str) -> Dict:
        """
        获取 Yahoo Finance 股票数据

        Returns:
            {
                "current_price": float,
                "price_change_5d": float (%),
                "short_float_ratio": float,
                "market_cap": float,
                "volume": int,
            }
        """
        cache_key = self.cache.get_cache_key("yahoo", ticker)
        cached = self.cache.load(cache_key, ttl=_ttl("yahoo_finance"))
        if cached:
            _log.info("📦 使用 Yahoo Finance 缓存: %s", ticker)
            return cached

        try:
            _log.info("🔄 获取 Yahoo Finance 数据: %s", ticker)

            # 尝试使用 yfinance 库
            try:
                import yfinance as yf
                stock = yf.Ticker(ticker)
                info = stock.info

                # currentPrice 盘后/盘前常为 None，多级 fallback 防止 None 写入缓存
                _price = (
                    info.get("currentPrice")
                    or info.get("regularMarketPrice")
                    or info.get("previousClose")
                    or info.get("open")
                )
                if not _price or _price <= 0:
                    try:
                        _hist = stock.history(period="2d")
                        _price = float(_hist["Close"].iloc[-1]) if not _hist.empty else 0
                    except Exception:
                        _price = 0

                metrics = {
                    "current_price": float(_price) if _price else 0,
                    "price_change_5d": self._calculate_5d_change(stock),
                    "short_float_ratio": info.get("shortPercentOfFloat", 0),
                    "market_cap": info.get("marketCap", 0),
                    "volume": info.get("volume", 0),
                    "last_updated": datetime.now().isoformat(),
                }

                # 仅在价格有效时写缓存，避免 None 污染缓存
                if metrics["current_price"] > 0:
                    self.cache.save(cache_key, metrics)
                return metrics

            except ImportError:
                _log.warning("⚠️ yfinance 未安装，使用示例数据")
                return self._get_sample_yahoo_data(ticker)

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
            _log.error("❌ Yahoo Finance 获取失败 %s: %s", ticker, e)
            return self._get_sample_yahoo_data(ticker)

    # ==================== Google Trends ====================

    def get_google_trends(self, ticker: str) -> Dict:
        """
        获取 Google Trends 搜索热度

        Returns:
            {
                "search_interest_percentile": float (0-100),
                "trend_direction": str ('up', 'down', 'stable'),
                "related_keywords": list,
            }
        """
        cache_key = self.cache.get_cache_key("gtrends", ticker)
        cached = self.cache.load(cache_key, ttl=_ttl("google_trends"))
        if cached:
            _log.info("📦 使用 Google Trends 缓存: %s", ticker)
            return cached

        try:
            _log.info("🔄 获取 Google Trends: %s", ticker)

            # 尝试使用 pytrends 库
            try:
                from pytrends.request import TrendReq
                pytrends = TrendReq(hl='en-US', tz=360)
                pytrends.build_payload([ticker], cat=0, timeframe='today 1m', geo='')

                trends_data = {
                    "search_interest_percentile": pytrends.interest_over_time()[ticker].iloc[-1] * 100 / 100,
                    "trend_direction": "up",
                    "related_keywords": [ticker],
                    "last_updated": datetime.now().isoformat(),
                }

                self.cache.save(cache_key, trends_data)
                return trends_data

            except ImportError:
                _log.warning("⚠️ pytrends 未安装，使用示例数据")
                return self._get_sample_trends(ticker)

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.error("❌ Google Trends 获取失败: %s", e)
            return self._get_sample_trends(ticker)

    # ==================== SEC EDGAR 文件 ====================

    def get_sec_filings(self, ticker: str, form_type: str = "4") -> List[Dict]:
        """
        获取 SEC 文件（Form 4 内幕交易）

        使用 sec_edgar.py 的真实 SEC EDGAR API 实现。
        包含内幕交易摘要：买入/卖出金额、情绪判断、重要交易明细。

        Args:
            ticker: 股票代码
            form_type: "4" 或 "13F"

        Returns:
            [{
                "filing_date": str,
                "form_type": str,
                "url": str,
                "title": str,
                "insider_sentiment": str,
                "sentiment_score": float,
                "notable_trades": list,
                "summary": str,
            }]
        """
        cache_key = self.cache.get_cache_key(f"sec_form{form_type}", ticker)
        cached = self.cache.load(cache_key, ttl=_ttl("sec_edgar"))
        if cached:
            _log.info("📦 使用 SEC 缓存: %s Form %s", ticker, form_type)
            return cached

        try:
            _log.info("🔄 获取 SEC Form %s: %s", form_type, ticker)

            # 使用 sec_edgar.py 的真实 API 实现
            from sec_edgar import SECEdgarClient
            client = SECEdgarClient()

            if form_type == "4":
                # 获取完整的内幕交易分析
                insider_data = client.get_insider_trades(ticker, days=30)

                if insider_data and insider_data.get("total_filings", 0) > 0:
                    # 同时获取原始 filing 列表用于构建文件链接
                    raw_filings = client.get_recent_form4_filings(ticker, limit=10)

                    filings = []
                    for f in raw_filings[:10]:
                        acc = f.get("accessionNumber", "").replace("-", "")
                        cik = f.get("cik", "")
                        filings.append({
                            "filing_date": f.get("filingDate", ""),
                            "form_type": "4",
                            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/" if cik else "",
                            "title": f"Form 4 - {ticker} Insider Transaction",
                        })

                    # 附加内幕交易分析摘要
                    if filings:
                        filings[0]["insider_sentiment"] = insider_data.get("insider_sentiment", "neutral")
                        filings[0]["sentiment_score"] = insider_data.get("sentiment_score", 5.0)
                        filings[0]["notable_trades"] = insider_data.get("notable_trades", [])[:5]
                        filings[0]["summary"] = insider_data.get("summary", "")
                        filings[0]["net_dollar_value"] = insider_data.get("net_dollar_value", 0)

                    self.cache.save(cache_key, filings)
                    return filings

            # 无数据或非 Form 4，使用样本数据
            _log.info("SEC EDGAR 无 %s Form %s 数据，使用样本", ticker, form_type)
            filings = self._get_sample_sec_filings(ticker, form_type)
            self.cache.save(cache_key, filings)
            return filings

        except (ImportError, ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.warning("SEC EDGAR 实时获取失败 %s: %s，降级为样本数据", ticker, e)
            return self._get_sample_sec_filings(ticker, form_type)

    # ==================== Seeking Alpha ====================

    def get_seeking_alpha_mentions(self, ticker: str) -> Dict:
        """
        获取 Seeking Alpha 页面数据

        Returns:
            {
                "page_views_week": int,
                "article_count_week": int,
                "rating": str,
            }
        """
        cache_key = self.cache.get_cache_key("seekingalpha", ticker)
        cached = self.cache.load(cache_key, ttl=_ttl("seeking_alpha"))
        if cached:
            _log.info("📦 使用 Seeking Alpha 缓存: %s", ticker)
            return cached

        try:
            _log.info("🔄 获取 Seeking Alpha: %s", ticker)

            # Seeking Alpha 爬取路径待实现（需要认证）

            data = self._get_sample_seeking_alpha(ticker)
            self.cache.save(cache_key, data)
            return data

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.error("❌ Seeking Alpha 获取失败: %s", e)
            return {"page_views_week": 0, "article_count_week": 0}

    # ==================== 辅助方法 ====================

    def _estimate_stocktwits_volume(self, ticker: str) -> int:
        """估计 StockTwits 消息量"""
        base_volumes = {
            "NVDA": 45000,
            "TSLA": 38000,
            "VKTX": 8000,
        }
        return base_volumes.get(ticker, 15000)

    def _estimate_bullish_ratio(self, ticker: str) -> float:
        """估计看多比例"""
        base_ratios = {"NVDA": 0.75, "TSLA": 0.68, "VKTX": 0.60}
        return base_ratios.get(ticker, 0.55)

    def _estimate_yes_odds(self, ticker: str) -> float:
        """估计 Polymarket YES 赔率"""
        base_odds = {"NVDA": 0.65, "TSLA": 0.55, "VKTX": 0.48}
        return base_odds.get(ticker, 0.50)

    def _estimate_volume(self, ticker: str) -> float:
        """估计 Polymarket 交易量"""
        base_volumes = {"NVDA": 8200000, "TSLA": 5500000, "VKTX": 1200000}
        return base_volumes.get(ticker, 1000000)

    def _estimate_odds_change(self, ticker: str) -> float:
        """估计 24h 赔率变化"""
        base_changes = {"NVDA": 8.2, "TSLA": 5.5, "VKTX": 3.2}
        return base_changes.get(ticker, 2.0)

    def _calculate_5d_change(self, stock) -> float:
        """计算 5 天价格变化"""
        try:
            hist = stock.history(period="5d")
            if len(hist) > 1:
                return ((hist['Close'].iloc[-1] - hist['Close'].iloc[0]) / hist['Close'].iloc[0]) * 100
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            _log.warning("5 日价格变化计算失败: %s", e)
        return 0

    def _get_sample_yahoo_data(self, ticker: str) -> Dict:
        """示例 Yahoo Finance 数据"""
        sample_data = {
            "NVDA": {
                "current_price": 145.32,
                "price_change_5d": 6.8,
                "short_float_ratio": 0.025,
                "market_cap": 3.6e12,
                "volume": 52000000,
            },
            "TSLA": {
                "current_price": 189.45,
                "price_change_5d": 2.3,
                "short_float_ratio": 0.032,
                "market_cap": 6.0e11,
                "volume": 148000000,
            },
            "VKTX": {
                "current_price": 7.82,
                "price_change_5d": -1.2,
                "short_float_ratio": 0.18,
                "market_cap": 1.2e9,
                "volume": 1500000,
            },
        }
        data = sample_data.get(ticker, {})
        data["last_updated"] = datetime.now().isoformat()
        return data

    def _get_sample_trends(self, ticker: str) -> Dict:
        """示例 Google Trends 数据"""
        return {
            "search_interest_percentile": 84.0,
            "trend_direction": "up",
            "related_keywords": [ticker, f"{ticker} stock", f"{ticker} earnings"],
            "last_updated": datetime.now().isoformat(),
        }

    def _get_sample_sec_filings(self, ticker: str, form_type: str) -> List[Dict]:
        """示例 SEC 文件"""
        return [
            {
                "filing_date": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
                "form_type": form_type,
                "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}",
                "title": f"Form {form_type} Filing",
            }
        ]

    def _get_sample_seeking_alpha(self, ticker: str) -> Dict:
        """示例 Seeking Alpha 数据"""
        sample_data = {
            "NVDA": {"page_views_week": 85000, "article_count_week": 47},
            "TSLA": {"page_views_week": 125000, "article_count_week": 63},
            "VKTX": {"page_views_week": 12000, "article_count_week": 8},
        }
        data = sample_data.get(ticker, {"page_views_week": 10000, "article_count_week": 5})
        data["last_updated"] = datetime.now().isoformat()
        return data

    # ==================== 综合数据收集 ====================

    def collect_all_metrics(self, ticker: str) -> Dict:
        """
        采集单个标的的所有指标

        Returns: 完整的指标字典，可直接用于拥挤度检测和评分
        """
        # ⭐ 优化 2：检查缓存（24 小时 TTL）
        cache_key = f"metrics_{ticker}_{datetime.now().strftime('%Y-%m-%d')}"
        cached_data = self.cache.load(cache_key, ttl=self.api_cache_ttl)
        if cached_data:
            self.cache_hits += 1
            _log.info("✅ %s 缓存命中（节省数据采集）", ticker)
            return cached_data

        self.cache_misses += 1
        _log.info("📊 开始采集 %s 的所有数据...", ticker)
        start_time = time.time()

        metrics = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "sources": {},
        }

        # H2: 真正并行采集各数据源（ThreadPoolExecutor）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _source_tasks = {
            "stocktwits": self.get_stocktwits_metrics,
            "polymarket": self.get_polymarket_odds,
            "yahoo_finance": self.get_yahoo_finance_metrics,
            "google_trends": self.get_google_trends,
            "sec_filings": self.get_sec_filings,
            "seeking_alpha": self.get_seeking_alpha_mentions,
        }
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="data_fetch") as _pool:
            _futures = {_pool.submit(fn, ticker): name for name, fn in _source_tasks.items()}
            for fut in as_completed(_futures):
                _name = _futures[fut]
                try:
                    metrics["sources"][_name] = fut.result(timeout=30)
                except Exception as _e:
                    _log.warning("collect_all_metrics %s/%s failed: %s", ticker, _name, _e)
                    metrics["sources"][_name] = {}

        # 转换为拥挤度检测需要的格式
        metrics["crowding_input"] = {
            "stocktwits_messages_per_day": metrics["sources"]["stocktwits"].get("messages_per_day", 0),
            "google_trends_percentile": metrics["sources"]["google_trends"].get("search_interest_percentile", 0),
            "bullish_agents": int(metrics["sources"]["stocktwits"].get("bullish_ratio", 0.5) * 6),
            "polymarket_odds_change_24h": metrics["sources"]["polymarket"].get("odds_change_24h", 0),
            "seeking_alpha_page_views": metrics["sources"]["seeking_alpha"].get("page_views_week", 0),
            "short_float_ratio": metrics["sources"]["yahoo_finance"].get("short_float_ratio", 0),
            "price_momentum_5d": metrics["sources"]["yahoo_finance"].get("price_change_5d", 0),
        }

        elapsed = time.time() - start_time
        _log.info("✅ 数据采集完成 %s (%.2f秒)", ticker, elapsed)

        # ⭐ 优化 2：保存到缓存（24 小时）
        self.cache.save(cache_key, metrics)

        return metrics


# ==================== 脚本示例 ====================
if __name__ == "__main__":
    _log.info("🚀 启动实时数据采集系统")

    fetcher = DataFetcher()

    # 采集多个标的的数据
    tickers = ["NVDA", "VKTX", "TSLA"]
    all_metrics = {}

    for ticker in tickers:
        metrics = fetcher.collect_all_metrics(ticker)
        all_metrics[ticker] = metrics

    # 保存汇总数据
    with open(str(PATHS.home / "realtime_metrics.json"), "w") as f:
        json.dump(all_metrics, f, indent=2, cls=SafeJSONEncoder)

    _log.info("数据采集完成！已保存到 realtime_metrics.json")
    _log.debug(json.dumps(all_metrics, indent=2, cls=SafeJSONEncoder))
