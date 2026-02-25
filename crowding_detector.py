"""
🐝 Alpha Hive - Crowding Detection 系统
优化 4：检测市场拥挤度，识别过度定价
"""

import logging as _logging
import json
from datetime import datetime
from typing import Dict, Tuple, List

_log = _logging.getLogger("alpha_hive.crowding_detector")


class CrowdingDetector:
    """拥挤度评估系统"""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.weights = {
            "stocktwits_volume": 0.25,
            "google_trends": 0.15,
            "consensus_strength": 0.25,
            "polymarket_volatility": 0.15,
            "seeking_alpha_views": 0.10,
            "short_squeeze_risk": 0.10
        }

    def calculate_crowding_score(self, metrics: Dict) -> float:
        """
        计算 0-100 的拥挤度评分

        Args:
            metrics: {
                "stocktwits_messages_per_day": int,
                "google_trends_percentile": float (0-100),
                "bullish_agents": int,  # 6 个中有几个看多
                "polymarket_odds_change_24h": float (%)
                "seeking_alpha_page_views": int,
                "short_float_ratio": float,
                "price_momentum_5d": float (%)
            }
        """

        scores = {}

        # 1. StockTwits 消息量
        messages = metrics.get("stocktwits_messages_per_day", 0)
        if messages < 10000:
            scores["stocktwits_volume"] = (messages / 10000) * 30
        elif messages < 50000:
            scores["stocktwits_volume"] = 30 + ((messages - 10000) / 40000) * 40
        else:
            scores["stocktwits_volume"] = 70 + min(30, (messages - 50000) / 10000)

        # 2. Google Trends 热度
        scores["google_trends"] = metrics.get("google_trends_percentile", 0)

        # 3. Agent 共识强度
        bullish_agents = metrics.get("bullish_agents", 3)
        consensus_pct = (bullish_agents / 6) * 100
        scores["consensus_strength"] = consensus_pct

        # 4. Polymarket 赔率变化速度
        odds_change = abs(metrics.get("polymarket_odds_change_24h", 0))
        if odds_change > 10:
            scores["polymarket_volatility"] = 80
        elif odds_change > 5:
            scores["polymarket_volatility"] = 60
        elif odds_change > 2:
            scores["polymarket_volatility"] = 40
        else:
            scores["polymarket_volatility"] = 20

        # 5. Seeking Alpha 页面浏览
        page_views = metrics.get("seeking_alpha_page_views", 0)
        if page_views > 100000:
            scores["seeking_alpha_views"] = 80
        elif page_views > 50000:
            scores["seeking_alpha_views"] = 60
        elif page_views > 10000:
            scores["seeking_alpha_views"] = 40
        else:
            scores["seeking_alpha_views"] = 20

        # 6. 短期内急速上升 + 高做空比例
        short_ratio = metrics.get("short_float_ratio", 0.0)
        price_momentum = metrics.get("price_momentum_5d", 0.0)

        if short_ratio > 0.3 and price_momentum > 15:
            scores["short_squeeze_risk"] = 90
        elif short_ratio > 0.2 or price_momentum > 20:
            scores["short_squeeze_risk"] = 70
        else:
            scores["short_squeeze_risk"] = 30

        # 加权合成
        crowding_score = sum(
            self.weights[key] * scores.get(key, 0)
            for key in self.weights
        )

        return min(100, max(0, crowding_score)), scores

    def get_crowding_category(self, score: float) -> Tuple[str, str]:
        """
        根据评分返回拥挤度分类
        返回 (分类, 颜色)
        """

        if score < 30:
            return "低拥挤度", "green"
        elif score < 60:
            return "中等拥挤度", "yellow"
        else:
            return "高拥挤度", "red"

    def get_adjustment_factor(self, score: float) -> float:
        """基于拥挤度调整综合评分的因子"""

        # 拥挤度越高，评分折扣越大
        if score < 30:
            return 1.2  # 低拥挤度，加权 +20%
        elif score < 60:
            return 0.95  # 轻微折扣
        else:
            return 0.70  # 重大折扣（30% 打折）

    def get_hedge_recommendations(self, crowding_score: float) -> List[Dict]:
        """
        基于拥挤度提供对冲建议
        """

        recommendations = []

        if crowding_score > 60:  # 高拥挤
            recommendations.append({
                "strategy": "看涨期权价差（Bull Call Spread）",
                "description": "买入 ATM 看涨 + 卖出 OTM 看涨",
                "benefit": "降低成本，限制上升空间但有期权费收益",
                "suitable_for": "看好但希望降低成本"
            })

            recommendations.append({
                "strategy": "看跌期权保护（Protective Put）",
                "description": "买入 OTM 看跌期权",
                "benefit": "完全下行保护",
                "suitable_for": "已持有长仓，寻求风险管理"
            })

            recommendations.append({
                "strategy": "等待回调进场（Wait & See）",
                "description": "等待股价下跌 5-8% 后再建仓",
                "benefit": "更好的价格",
                "suitable_for": "耐心的长期投资者"
            })

        elif crowding_score > 30:  # 中等拥挤
            recommendations.append({
                "strategy": "部分止盈",
                "description": "卖出 50% 头寸锁定利润",
                "benefit": "降低风险暴露",
                "suitable_for": "有盈利头寸需要风险管理"
            })

        else:  # 低拥挤
            recommendations.append({
                "strategy": "增加头寸（Add Position）",
                "description": "可以考虑增加投资规模",
                "benefit": "低定价，信息不对称大",
                "suitable_for": "基本面看好，低拥挤的标的"
            })

        return recommendations

    def generate_html_section(self, metrics: Dict, initial_score: float) -> str:
        """生成 HTML 报告段落"""

        crowding_score, component_scores = self.calculate_crowding_score(metrics)
        category, color = self.get_crowding_category(crowding_score)
        adjustment_factor = self.get_adjustment_factor(crowding_score)
        final_score = initial_score * adjustment_factor

        html = f"""
        <section id="crowding-analysis-{self.ticker}" class="report-section">
            <div class="card-header">
                <h2>🗣️ 市场热度 & 拥挤度分析 - {self.ticker}</h2>
                <div class="crowding-badge {color}">⚠️ {category}</div>
            </div>

            <div class="card-body">
                <!-- 拥挤度仪表板 -->
                <div class="crowding-dashboard">
                    <div class="crowding-meter">
                        <div class="meter-label">拥挤度评分</div>
                        <div class="meter-bar">
                            <div class="meter-fill" style="width: {crowding_score}%"></div>
                            <span class="meter-value">{crowding_score:.0f}/100</span>
                        </div>
                        <p class="meter-interpretation">
                            {self._get_interpretation(crowding_score)}
                        </p>
                    </div>

                    <!-- 拥挤度指标分解 -->
                    <div class="crowding-breakdown">
                        <h3>拥挤度指标分解</h3>
        """

        # 添加每个指标
        indicator_labels = {
            "stocktwits_volume": ("StockTwits 48h 消息量", "25%"),
            "google_trends": ("Google Trends 热度", "15%"),
            "consensus_strength": ("6 个 Agent 共识强度", "25%"),
            "polymarket_volatility": ("Polymarket 赔率变化速度", "15%"),
            "seeking_alpha_views": ("Seeking Alpha 页面浏览", "10%"),
            "short_squeeze_risk": ("短期价格动量", "10%")
        }

        for key, (label, weight) in indicator_labels.items():
            score = component_scores.get(key, 0)
            html += f"""
                        <div class="indicator">
                            <div class="indicator-label">
                                <span>{label}</span>
                                <span class="weight">(权重 {weight})</span>
                            </div>
                            <div class="indicator-bar">
                                <div class="indicator-fill" style="width: {score}%"></div>
                            </div>
                            <div class="indicator-value">
                                <strong>{self._get_metric_display(key, metrics)}</strong>
                                <span class="interpretation">{self._get_metric_interpretation(key, score)}</span>
                            </div>
                        </div>
            """

        html += f"""
                    </div>
                </div>

                <!-- 拥挤度对评分的影响 -->
                <div class="crowding-impact">
                    <h3>📊 拥挤度对评分的影响</h3>
                    <table class="impact-table">
                        <tr>
                            <td><strong>基础综合评分</strong></td>
                            <td>{initial_score:.2f}/10</td>
                        </tr>
                        <tr>
                            <td><strong>拥挤度折扣因子</strong></td>
                            <td>{adjustment_factor:.2f}x ({('加权' if adjustment_factor > 1 else '打折')} {abs((adjustment_factor - 1) * 100):.0f}%)</td>
                        </tr>
                        <tr class="highlight">
                            <td><strong>调整后评分</strong></td>
                            <td>{final_score:.2f}/10</td>
                        </tr>
                    </table>
                    <p class="impact-interpretation">
                        {self._get_score_adjustment_interpretation(crowding_score, initial_score, final_score)}
                    </p>
                </div>

                <!-- 对冲建议 -->
                <div class="hedge-recommendations">
                    <h3>🛡️ 推荐对冲策略</h3>
        """

        for i, hedge in enumerate(self.get_hedge_recommendations(crowding_score), 1):
            html += f"""
                    <div class="hedge-option">
                        <h4>选项 {i}：{hedge['strategy']}</h4>
                        <p>
                            <strong>策略：</strong> {hedge['description']}<br>
                            <strong>优势：</strong> {hedge['benefit']}<br>
                            <strong>适合：</strong> {hedge['suitable_for']}
                        </p>
                    </div>
            """

        html += """
                </div>
            </div>
        </section>

        <style>
            #crowding-analysis-{ticker} {{
                background: linear-gradient(135deg, #fff5e6 0%, #ffe6e6 100%);
                border: 2px solid #ff9800;
                border-radius: 12px;
                padding: 20px;
                margin: 30px 0;
            }}

            .crowding-badge {{
                display: inline-block;
                padding: 8px 16px;
                border-radius: 20px;
                font-weight: 600;
                font-size: 14px;
            }}

            .crowding-badge.red {{
                background: #ffebee;
                color: #c62828;
            }}

            .crowding-badge.yellow {{
                background: #fff3e0;
                color: #e65100;
            }}

            .crowding-badge.green {{
                background: #e8f5e9;
                color: #1b5e20;
            }}

            .meter-bar {{
                position: relative;
                background: #e0e0e0;
                height: 30px;
                border-radius: 15px;
                overflow: hidden;
                margin: 10px 0;
            }}

            .meter-fill {{
                background: linear-gradient(90deg, #ff9800 0%, #f44336 100%);
                height: 100%;
                border-radius: 15px;
                transition: width 0.3s ease;
            }}

            .meter-value {{
                position: absolute;
                top: 50%;
                right: 10px;
                transform: translateY(-50%);
                color: white;
                font-weight: 600;
                font-size: 14px;
            }}

            .indicator {{
                background: white;
                padding: 12px;
                margin: 10px 0;
                border-radius: 6px;
                border-left: 3px solid #ff9800;
            }}

            .indicator-bar {{
                background: #f0f0f0;
                height: 20px;
                border-radius: 10px;
                overflow: hidden;
                margin: 8px 0;
            }}

            .indicator-fill {{
                background: linear-gradient(90deg, #ff9800, #f44336);
                height: 100%;
                border-radius: 10px;
            }}

            .impact-table {{
                width: 100%;
                margin: 15px 0;
                border-collapse: collapse;
                background: white;
                border-radius: 6px;
                overflow: hidden;
            }}

            .impact-table td {{
                padding: 12px;
                border-bottom: 1px solid #eee;
            }}

            .impact-table .highlight {{
                background: #fff3e0;
                font-weight: 600;
            }}

            .hedge-option {{
                padding: 12px;
                margin: 10px 0;
                border-left: 3px solid #2196f3;
                background: #e3f2fd;
                border-radius: 4px;
            }}
        </style>
        """

        return html

    def _get_interpretation(self, score: float) -> str:
        """获取拥挤度的文字解释"""
        if score > 70:
            return "⚠️ <strong>极度拥挤</strong><br>该想法已被广泛发现和定价。预期上升空间有限，下跌风险较高。"
        elif score > 50:
            return "🟡 <strong>中等拥挤</strong><br>信号已被部分市场参与者发现。继续上升需要新的催化剂。"
        else:
            return "🟢 <strong>低拥挤度</strong><br>信息相对不为人知。存在信息不对称的机会。"

    def _get_metric_display(self, key: str, metrics: Dict) -> str:
        """获取指标的显示值"""
        displays = {
            "stocktwits_volume": f"{metrics.get('stocktwits_messages_per_day', 0):,} 条/天",
            "google_trends": f"{metrics.get('google_trends_percentile', 0):.0f} 百分位",
            "consensus_strength": f"{metrics.get('bullish_agents', 0)}/6 看多",
            "polymarket_volatility": f"24h {metrics.get('polymarket_odds_change_24h', 0):.1f}% 变化",
            "seeking_alpha_views": f"{metrics.get('seeking_alpha_page_views', 0):,} 次/周",
            "short_squeeze_risk": f"+{metrics.get('price_momentum_5d', 0):.1f}% (5d)"
        }
        return displays.get(key, "N/A")

    def _get_metric_interpretation(self, key: str, score: float) -> str:
        """获取指标的解释"""
        if score > 70:
            return "(极度拥挤)"
        elif score > 50:
            return "(拥挤)"
        else:
            return "(正常)"

    def _get_score_adjustment_interpretation(self, crowding_score: float, initial: float, final: float) -> str:
        """获取评分调整的解释"""
        if crowding_score > 60:
            return f"虽然 {self.ticker} 在基本面和情绪上都看好，但由于高度拥挤，上升空间有限。相对收益风险比可能不如其他低拥挤度的标的。"
        elif crowding_score > 30:
            return f"{self.ticker} 的拥挤度处于中等水平。信号已被部分市场参与者发现，但仍有增长空间。"
        else:
            return f"{self.ticker} 拥挤度低，信息相对不为人知。该标的存在更大的非共识空间和上升潜力。"

    def save_to_json(self, metrics: Dict, initial_score: float, filename: str = None) -> str:
        """保存拥挤度分析到 JSON"""

        if filename is None:
            filename = f"crowding_{self.ticker}.json"

        crowding_score, component_scores = self.calculate_crowding_score(metrics)
        category, color = self.get_crowding_category(crowding_score)

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump({
                "ticker": self.ticker,
                "crowding_score": crowding_score,
                "category": category,
                "component_scores": component_scores,
                "adjustment_factor": self.get_adjustment_factor(crowding_score),
                "final_score": initial_score * self.get_adjustment_factor(crowding_score),
                "metrics": metrics,
                "created_at": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)

        return filename


def get_crowding_metrics(ticker: str, board=None) -> Dict:
    """
    获取指定标的的真实拥挤度指标

    Args:
        ticker: 股票代码
        board: PheromoneBoard 实例（可选）

    Returns:
        真实拥挤度指标字典
    """
    from real_data_sources import get_real_crowding_metrics

    # 获取 yfinance 基础数据
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="1mo")
        stock_data = {
            "price": float(hist["Close"].iloc[-1]) if not hist.empty else 100.0,
            "momentum_5d": (float(hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100) if len(hist) >= 5 else 0.0,
            "avg_volume": int(hist["Volume"].mean()) if not hist.empty else 0,
            "volume_ratio": float(hist["Volume"].iloc[-1] / hist["Volume"].mean()) if not hist.empty and hist["Volume"].mean() > 0 else 1.0,
            "volatility_20d": float(hist["Close"].pct_change().std() * (252 ** 0.5) * 100) if len(hist) >= 20 else 0.0,
        }
    except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as exc:
        _log.debug("yfinance data fetch failed for %s: %s", ticker, exc)
        stock_data = {"price": 100.0, "momentum_5d": 0.0, "avg_volume": 0, "volume_ratio": 1.0, "volatility_20d": 0.0}

    return get_real_crowding_metrics(ticker, stock_data, board)


# 使用示例
if __name__ == "__main__":
    metrics = get_crowding_metrics("NVDA")
    detector = CrowdingDetector("NVDA")
    score, components = detector.calculate_crowding_score(metrics)
    dq = metrics.get("data_quality", {})
    print(f"NVDA 拥挤度: {score:.1f}/100")
    print(f"数据质量: {dq}")
