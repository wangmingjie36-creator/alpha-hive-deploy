"""
🐝 Alpha Hive - ML 增强报告生成
将机器学习预测集成到高级分析报告
"""

import json
import argparse
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
from advanced_analyzer import AdvancedAnalyzer
from ml_predictor import (
    MLPredictionService,
    TrainingData,
    HistoricalDataBuilder,
)
from config import WATCHLIST
from hive_logger import PATHS, get_logger

_log = get_logger("ml_report")


class MLEnhancedReportGenerator:
    """ML 增强的报告生成器"""

    # ⭐ Task 2: 全局模型缓存（类级别，跨实例共享 + 磁盘持久化）
    _model_cache = {}          # 内存缓存（同一进程内）
    _cache_date = None         # 缓存日期
    _training_lock = Lock()    # 防止并发重复训练
    _model_file = PATHS.home / "ml_model_cache.pkl"  # 磁盘缓存文件

    # ⭐ Task 3: 异步 HTML 生成（后台文件写入）
    _file_writer_pool = None   # 异步文件写入线程池
    _writer_lock = Lock()      # 文件写入锁（防止并发冲突）

    def __init__(self):
        self.analyzer = AdvancedAnalyzer()
        self.ml_service = MLPredictionService()
        self.timestamp = datetime.now()

        # ⭐ Task 3: 初始化异步文件写入线程池（全局单例）
        if MLEnhancedReportGenerator._file_writer_pool is None:
            MLEnhancedReportGenerator._file_writer_pool = ThreadPoolExecutor(max_workers=3)
            # print("🚀 异步文件写入线程池已初始化（3 workers）")

        # ⭐ Task 2: 智能缓存策略（内存 + 磁盘）
        today = datetime.now().strftime("%Y-%m-%d")

        # 策略 1：检查内存缓存（同一进程内的快速复用）
        if today in self._model_cache:
            _log.info("复用内存缓存 ML 模型（无需重新训练）")
            self.ml_service.model = self._model_cache[today]

        # 策略 2：检查磁盘缓存（跨进程的缓存）
        elif self._check_disk_cache(today):
            _log.info("复用磁盘缓存 ML 模型（昨日已训练）")
            self._load_model_from_disk()
            # 同时更新内存缓存
            self._model_cache[today] = self.ml_service.model
            self._cache_date = today

        # 策略 3：需要训练
        else:
            with self._training_lock:
                # 双重检查（防止并发重复训练）
                if today not in self._model_cache and not self._check_disk_cache(today):
                    _log.info("初始化 ML 模型（首次训练）...")
                    self.ml_service.train_model()
                    # 缓存到内存
                    self._model_cache[today] = self.ml_service.model
                    self._cache_date = today
                    # 缓存到磁盘（供后续进程使用）
                    self._save_model_to_disk()
                else:
                    # 另一个线程已经训练，从缓存中恢复
                    if today in self._model_cache:
                        self.ml_service.model = self._model_cache[today]
                    else:
                        self._load_model_from_disk()
                        self._model_cache[today] = self.ml_service.model

    def _check_disk_cache(self, today: str) -> bool:
        """检查磁盘缓存是否存在且有效"""
        try:
            if not self._model_file.exists():
                return False

            # 检查文件修改时间是否是今天
            import os
            mtime = os.path.getmtime(str(self._model_file))
            file_date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            return file_date == today
        except Exception as e:
            # 缓存检查失败，重新训练
            return False

    def _load_model_from_disk(self):
        """从磁盘加载模型"""
        try:
            import pickle
            with open(self._model_file, "rb") as f:
                self.ml_service.model = pickle.load(f)
        except Exception as e:
            _log.warning("磁盘缓存加载失败：%s，将重新训练", e)
            self.ml_service.train_model()

    def _save_model_to_disk(self):
        """保存模型到磁盘"""
        try:
            import pickle
            with open(self._model_file, "wb") as f:
                pickle.dump(self.ml_service.model, f)
        except Exception as e:
            _log.warning("磁盘缓存保存失败：%s", e)

    # ⭐ Task 3: 异步文件写入方法
    def _write_file_async(self, filepath: Path, content: str, is_json: bool = False) -> None:
        """异步写入文件到磁盘（后台线程）"""
        try:
            with self._writer_lock:
                if is_json:
                    # JSON 内容：先对象再转 JSON
                    with open(filepath, "w") as f:
                        json.dump(content, f, indent=2, default=str)
                else:
                    # 文本内容：直接写入
                    with open(filepath, "w") as f:
                        f.write(content)
        except Exception as e:
            _log.warning("文件写入失败 %s: %s", filepath.name, str(e)[:50])

    def save_html_and_json_async(
        self,
        ticker: str,
        html_content: str,
        json_data: dict,
        report_dir: Path,
        timestamp: datetime,
    ) -> None:
        """
        异步保存 HTML 和 JSON 文件（后台线程）
        不阻塞主流程
        """
        # 生成文件名
        html_filename = f"alpha-hive-{ticker}-ml-enhanced-{timestamp.strftime('%Y-%m-%d')}.html"
        json_filename = f"analysis-{ticker}-ml-{timestamp.strftime('%Y-%m-%d')}.json"

        html_path = report_dir / html_filename
        json_path = report_dir / json_filename

        # 提交异步写入任务
        self._file_writer_pool.submit(self._write_file_async, html_path, html_content, False)
        self._file_writer_pool.submit(self._write_file_async, json_path, json_data, True)

    def generate_ml_enhanced_report(
        self, ticker: str, realtime_metrics: dict
    ) -> dict:
        """生成 ML 增强的分析报告"""

        # 获取高级分析
        advanced_analysis = self.analyzer.generate_comprehensive_analysis(
            ticker, realtime_metrics
        )

        # 构建 ML 输入数据
        ml_input = self._prepare_ml_input(ticker, realtime_metrics, advanced_analysis)

        # 获取 ML 预测
        ml_prediction = self.ml_service.predict_for_opportunity(ml_input)

        # 合并分析
        enhanced_report = {
            "ticker": ticker,
            "timestamp": self.timestamp.isoformat(),
            "advanced_analysis": advanced_analysis,
            "ml_prediction": ml_prediction,
            "combined_recommendation": self._combine_recommendations(
                advanced_analysis, ml_prediction
            ),
        }

        return enhanced_report

    def _prepare_ml_input(
        self, ticker: str, metrics: dict, analysis: dict
    ) -> TrainingData:
        """为 ML 模型准备输入数据"""

        # 从实时数据中提取特征
        crowding_score = 63.5  # 示例，可以从 metrics 中获取
        catalyst_quality = analysis.get("recommendation", {}).get("rating", "B")
        momentum_5d = metrics.get("sources", {}).get("yahoo_finance", {}).get(
            "price_change_5d", 0
        )
        volatility = 5.0  # 示例波动率
        market_sentiment = 45  # 示例情绪值

        # 映射评级到催化剂质量
        rating_to_quality = {
            "STRONG BUY": "A+",
            "BUY": "A",
            "HOLD": "B+",
            "AVOID": "C",
        }
        catalyst_quality = rating_to_quality.get(
            analysis.get("recommendation", {}).get("rating", "B"), "B"
        )

        return TrainingData(
            ticker=ticker,
            date=datetime.now().isoformat(),
            crowding_score=crowding_score,
            catalyst_quality=catalyst_quality,
            momentum_5d=momentum_5d,
            volatility=volatility,
            market_sentiment=market_sentiment,
            actual_return_3d=0,
            actual_return_7d=0,
            actual_return_30d=0,
            win_3d=False,
            win_7d=False,
            win_30d=False,
        )

    def _generate_options_section_html(self, options: dict) -> str:
        """生成期权分析 HTML 部分"""
        if not options:
            return ""

        iv_rank = options.get("iv_rank", 50)
        iv_percentile = options.get("iv_percentile", 50)
        iv_current = options.get("iv_current", 25)
        put_call_ratio = options.get("put_call_ratio", 1.0)
        gamma_squeeze_risk = options.get("gamma_squeeze_risk", "medium")
        flow_direction = options.get("flow_direction", "neutral")
        options_score = options.get("options_score", 5.0)
        signal_summary = options.get("signal_summary", "信号平衡")
        unusual_activity = options.get("unusual_activity", [])
        key_levels = options.get("key_levels", {})

        # 判断 IV Rank 颜色
        if iv_rank < 30:
            iv_color = "#28a745"  # 绿色，低 IV
            iv_label = "低 IV"
        elif iv_rank > 70:
            iv_color = "#dc3545"  # 红色，高 IV
            iv_label = "高 IV"
        else:
            iv_color = "#ffc107"  # 黄色，中等 IV
            iv_label = "中等 IV"

        # 判断流向颜色
        if flow_direction == "bullish":
            flow_color = "#28a745"
        elif flow_direction == "bearish":
            flow_color = "#dc3545"
        else:
            flow_color = "#ffc107"

        # 生成异动信号 HTML
        unusual_html = ""
        if unusual_activity:
            unusual_html = "<div style='margin-top: 15px;'><strong>异动信号：</strong><ul style='margin: 10px 0; padding-left: 20px;'>"
            for activity in unusual_activity[:5]:  # 只显示前 5 个
                activity_type = activity.get("type", "unknown")
                strike = activity.get("strike", "N/A")
                volume = activity.get("volume", 0)
                unusual_html += f"<li>{activity_type} @ ${strike} (成交量: {volume:,})</li>"
            unusual_html += "</ul></div>"

        # 生成关键位置 HTML
        support_html = ""
        resistance_html = ""

        if key_levels.get("support"):
            support_html = "<div style='margin-top: 15px;'><strong>支撑位：</strong><ul style='margin: 10px 0; padding-left: 20px;'>"
            for level in key_levels.get("support", []):
                strike = level.get("strike", "N/A")
                oi = level.get("oi", 0)
                support_html += f"<li>${strike} (OI: {oi:,})</li>"
            support_html += "</ul></div>"

        if key_levels.get("resistance"):
            resistance_html = "<div style='margin-top: 15px;'><strong>阻力位：</strong><ul style='margin: 10px 0; padding-left: 20px;'>"
            for level in key_levels.get("resistance", []):
                strike = level.get("strike", "N/A")
                oi = level.get("oi", 0)
                resistance_html += f"<li>${strike} (OI: {oi:,})</li>"
            resistance_html += "</ul></div>"

        return f"""
            <div class="section">
                <h2>📈 期权信号分析</h2>

                <div class="ml-section">
                    <h3 style="color: #667eea; margin-bottom: 15px;">⚡ 核心指标</h3>

                    <div class="metric">
                        <span class="metric-label">IV Rank</span>
                        <span class="metric-value" style="color: {iv_color};">
                            {iv_rank:.1f} ({iv_label})
                        </span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">当前 IV</span>
                        <span class="metric-value">{iv_current:.2f}%</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">IV 百分位数</span>
                        <span class="metric-value">{iv_percentile:.1f}%</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">Put/Call Ratio</span>
                        <span class="metric-value">{put_call_ratio:.2f}</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">流向</span>
                        <span class="metric-value" style="color: {flow_color};">
                            {flow_direction.upper()}
                        </span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">Gamma Squeeze 风险</span>
                        <span class="metric-value">{gamma_squeeze_risk.upper()}</span>
                    </div>

                    <h3 style="color: #667eea; margin-top: 20px; margin-bottom: 15px;">📊 期权综合评分</h3>

                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px;">
                        <div style="font-size: 3.5em; font-weight: bold; color: #667eea; margin-bottom: 10px;">
                            {options_score:.1f}
                        </div>
                        <div style="font-size: 1.2em; color: #333; margin-bottom: 10px;">/ 10.0</div>
                        <div style="color: #666; font-size: 0.95em;">{signal_summary}</div>
                    </div>

                    {unusual_html}
                    {support_html}
                    {resistance_html}
                </div>
            </div>
"""

    def _combine_recommendations(
        self, advanced_analysis: dict, ml_prediction: dict
    ) -> dict:
        """合并人工和 ML 推荐"""

        human_prob = advanced_analysis.get("probability_analysis", {}).get(
            "win_probability_pct", 50
        )
        ml_prob = ml_prediction.get("prediction", {}).get("probability", 0.5) * 100

        # 加权平均（70% 高级分析 + 30% ML）
        combined_prob = human_prob * 0.7 + ml_prob * 0.3

        # 生成最终建议
        if combined_prob >= 75:
            rating = "STRONG BUY"
            action = "积极布局"
        elif combined_prob >= 65:
            rating = "BUY"
            action = "分批建仓"
        elif combined_prob >= 50:
            rating = "HOLD"
            action = "观察等待"
        else:
            rating = "AVOID"
            action = "回避或减仓"

        return {
            "human_probability": round(human_prob, 1),
            "ml_probability": round(ml_prob, 1),
            "combined_probability": round(combined_prob, 1),
            "rating": rating,
            "action": action,
            "confidence": f"{combined_prob:.1f}%",
            "reasoning": f"人工分析 {human_prob:.1f}% + ML 预测 {ml_prob:.1f}% = 综合 {combined_prob:.1f}%",
        }

    def generate_html_report(
        self, ticker: str, enhanced_report: dict
    ) -> str:
        """生成 ML 增强的 HTML 报告（完整版）"""
        combined = enhanced_report['combined_recommendation']
        analysis = enhanced_report.get('advanced_analysis', {})
        ml_pred = enhanced_report.get('ml_prediction', {})
        options = analysis.get('options_analysis') or {}
        recommendation = analysis.get('recommendation', {})
        prob = analysis.get('probability_analysis', {})

        # 评级颜色
        rating = combined.get('rating', 'HOLD')
        if rating == 'STRONG BUY':
            rating_color = '#28a745'
        elif rating == 'BUY':
            rating_color = '#17a2b8'
        elif rating == 'AVOID':
            rating_color = '#dc3545'
        else:
            rating_color = '#ffc107'

        # 期权部分
        options_html = self._generate_options_section_html(options) if options else ""

        # ML 预测部分
        pred = ml_pred.get('prediction', {})
        ml_prob_val = pred.get('probability', 0.5) * 100
        ml_features = ml_pred.get('feature_importance', {})
        ml_html = ""
        if ml_features:
            feat_rows = "".join(
                f"<tr><td>{k}</td><td>{v:.3f}</td></tr>"
                for k, v in sorted(ml_features.items(), key=lambda x: -abs(x[1]))[:8]
            )
            ml_html = f"""
            <div class="section">
                <h2>ML 特征重要度</h2>
                <table><tr><th>特征</th><th>权重</th></tr>{feat_rows}</table>
            </div>"""

        # 概率与风控
        win_prob = prob.get('win_probability_pct', 50)
        risk_reward = prob.get('risk_reward_ratio', 1.0)
        position = analysis.get('position_management', {})
        stop_loss = position.get('stop_loss', {})
        take_profit = position.get('take_profit', {})
        holding = position.get('optimal_holding_time', '')

        # 止损止盈 HTML
        position_html = ""
        if stop_loss or take_profit:
            sl_rows = ""
            if isinstance(stop_loss, dict):
                for k, v in stop_loss.items():
                    sl_rows += f"<tr><td>{k}</td><td>${v:.2f}</td></tr>" if isinstance(v, (int, float)) else f"<tr><td>{k}</td><td>{v}</td></tr>"
            elif isinstance(stop_loss, list):
                for item in stop_loss:
                    if isinstance(item, dict):
                        sl_rows += f"<tr><td>{item.get('level','')}</td><td>${item.get('price',0):.2f}</td></tr>"
            tp_rows = ""
            if isinstance(take_profit, dict):
                for k, v in take_profit.items():
                    tp_rows += f"<tr><td>{k}</td><td>${v:.2f}</td></tr>" if isinstance(v, (int, float)) else f"<tr><td>{k}</td><td>{v}</td></tr>"
            elif isinstance(take_profit, list):
                for item in take_profit:
                    if isinstance(item, dict):
                        tp_rows += f"<tr><td>{item.get('level','')}</td><td>${item.get('price',0):.2f}</td></tr>"
            position_html = f"""
            <div class="section">
                <h2>止损 / 止盈位</h2>
                <div class="grid-2">
                    <div>
                        <h3 style="color:#dc3545;">止损位</h3>
                        <table>{sl_rows}</table>
                    </div>
                    <div>
                        <h3 style="color:#28a745;">止盈位</h3>
                        <table>{tp_rows}</table>
                    </div>
                </div>
                {f'<p style="margin-top:15px;">最佳持仓周期：<strong>{holding}</strong></p>' if holding else ''}
            </div>"""

        # 投资建议详情
        rec_reasoning = recommendation.get('reasoning', '')
        rec_risks = recommendation.get('risks', [])
        rec_catalysts = recommendation.get('catalysts', [])
        rec_html = ""
        if rec_reasoning or rec_risks or rec_catalysts:
            risks_li = "".join(f"<li>{r}</li>" for r in rec_risks[:5]) if isinstance(rec_risks, list) else ""
            cats_li = "".join(f"<li>{c}</li>" for c in rec_catalysts[:5]) if isinstance(rec_catalysts, list) else ""
            rec_html = f"""
            <div class="section">
                <h2>投资建议详情</h2>
                {f'<p>{rec_reasoning}</p>' if isinstance(rec_reasoning, str) and rec_reasoning else ''}
                {f'<h3>催化剂</h3><ul>{cats_li}</ul>' if cats_li else ''}
                {f'<h3>风险因素</h3><ul>{risks_li}</ul>' if risks_li else ''}
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{ticker} ML 增强分析 - Alpha Hive</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh; padding: 20px;
        }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        .header {{
            background: white; border-radius: 15px; padding: 35px;
            margin-bottom: 25px; box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .header h1 {{ font-size: 2.2em; color: #667eea; margin-bottom: 8px; }}
        .header .rating {{
            display: inline-block; padding: 8px 25px; border-radius: 25px;
            color: white; font-size: 1.3em; font-weight: bold;
            background: {rating_color}; margin: 10px 0;
        }}
        .section {{
            background: white; border-radius: 12px; padding: 25px;
            margin-bottom: 20px; box-shadow: 0 5px 20px rgba(0,0,0,0.08);
        }}
        .section h2 {{
            color: #667eea; font-size: 1.4em; margin-bottom: 18px;
            padding-bottom: 10px; border-bottom: 2px solid #f0f0f0;
        }}
        .section h3 {{ color: #555; margin: 15px 0 10px; font-size: 1.1em; }}
        .grid-4 {{
            display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px;
        }}
        .grid-2 {{
            display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
        }}
        .stat {{
            text-align: center; padding: 15px; border-radius: 10px;
            background: linear-gradient(135deg, #f8f9fa, #fff);
            border: 1px solid #e8e8e8;
        }}
        .stat .num {{ font-size: 1.8em; font-weight: bold; color: #667eea; }}
        .stat .lbl {{ font-size: 0.85em; color: #888; margin-top: 5px; }}
        .metric {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 10px 0; border-bottom: 1px solid #f5f5f5;
        }}
        .metric-label {{ color: #666; font-weight: 500; }}
        .metric-value {{ font-weight: bold; color: #333; }}
        table {{
            width: 100%; border-collapse: collapse; margin-top: 10px;
        }}
        th, td {{
            padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee;
        }}
        th {{
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white; font-weight: 600; font-size: 0.9em;
        }}
        ul {{ padding-left: 20px; margin: 10px 0; }}
        li {{ margin: 6px 0; color: #444; line-height: 1.6; }}
        .footer {{
            text-align: center; color: rgba(255,255,255,0.85);
            margin-top: 20px; font-size: 0.9em;
        }}
        @media (max-width: 600px) {{
            .grid-4 {{ grid-template-columns: repeat(2, 1fr); }}
            .grid-2 {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
<div class="container">
    <!-- 头部 -->
    <div class="header">
        <h1>{ticker} ML 增强分析</h1>
        <div class="rating">{rating} - {combined['action']}</div>
        <p style="color:#888; margin-top:10px;">
            {self.timestamp.strftime('%Y-%m-%d %H:%M')} | Alpha Hive
        </p>
    </div>

    <!-- 核心指标 -->
    <div class="section">
        <h2>核心指标</h2>
        <div class="grid-4">
            <div class="stat">
                <div class="num">{combined['combined_probability']:.1f}%</div>
                <div class="lbl">综合胜率</div>
            </div>
            <div class="stat">
                <div class="num">{win_prob:.1f}%</div>
                <div class="lbl">人工分析</div>
            </div>
            <div class="stat">
                <div class="num">{ml_prob_val:.1f}%</div>
                <div class="lbl">ML 预测</div>
            </div>
            <div class="stat">
                <div class="num">{risk_reward:.2f}</div>
                <div class="lbl">风险回报比</div>
            </div>
        </div>
    </div>

    <!-- 期权信号 -->
    {options_html}

    <!-- 止损止盈 -->
    {position_html}

    <!-- 投资建议 -->
    {rec_html}

    <!-- ML 特征 -->
    {ml_html}

    <!-- 免责声明 -->
    <div class="section" style="background:#fff3cd; border:1px solid #ffc107;">
        <p style="color:#856404; font-size:0.9em;">
            <strong>免责声明</strong>：本报告为 AI 自动生成，不构成投资建议。
            所有交易决策需自行判断和风控。预测存在误差，过往表现不代表未来收益。
        </p>
    </div>

    <div class="footer">
        <p><a href="index.html" style="color:white;">返回仪表板</a></p>
    </div>
</div>
</body>
</html>"""
        return html


def main():
    """主程序"""

    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="Alpha Hive ML 增强报告生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  python3 generate_ml_report.py
  python3 generate_ml_report.py --tickers NVDA TSLA VKTX
  python3 generate_ml_report.py --all-watchlist
        """
    )
    parser.add_argument(
        '--tickers',
        nargs='+',
        default=["NVDA", "TSLA", "VKTX"],
        help='要分析的股票代码列表（空格分隔，默认：NVDA TSLA VKTX）'
    )
    parser.add_argument(
        '--all-watchlist',
        action='store_true',
        help='分析配置中的全部监控列表'
    )

    args = parser.parse_args()

    # 确定要分析的标的
    if args.all_watchlist:
        tickers = list(WATCHLIST.keys())[:10]  # 默认最多10个
        _log.info("分析全部监控列表（最多10个）: %s", tickers)
    else:
        tickers = args.tickers
        _log.info("分析指定标的: %s", tickers)

    # 加载实时数据（如果存在）
    report_dir = PATHS.home
    realtime_file = report_dir / "realtime_metrics.json"

    metrics = {}
    if realtime_file.exists():
        try:
            with open(realtime_file) as f:
                metrics = json.load(f)
        except Exception as e:
            _log.warning("加载实时数据失败: %s，继续使用空数据", e)
    else:
        _log.warning("未找到 realtime_metrics.json，将使用样本数据")

    # 创建生成器
    report_gen = MLEnhancedReportGenerator()

    _log.info("生成 ML 增强报告...")
    _log.info("=" * 60)

    # 为每个标的生成报告
    successful_count = 0
    for ticker in tickers:
        try:
            _log.info("生成 %s ML 增强报告...", ticker)

            # 获取该标的的数据（如果没有则使用样本）
            ticker_data = metrics.get(ticker, {
                "ticker": ticker,
                "sources": {
                    "yahoo_finance": {
                        "current_price": 100.0,
                        "change_pct": 2.5
                    }
                }
            })

            # 生成分析
            enhanced_report = report_gen.generate_ml_enhanced_report(
                ticker, ticker_data
            )

            # 生成 HTML
            html = report_gen.generate_html_report(ticker, enhanced_report)

            # ⭐ Task 3: 异步保存文件（不阻塞主流程）
            filename = f"alpha-hive-{ticker}-ml-enhanced-{report_gen.timestamp.strftime('%Y-%m-%d')}.html"
            json_filename = f"analysis-{ticker}-ml-{report_gen.timestamp.strftime('%Y-%m-%d')}.json"

            # 提交异步写入任务（立即返回，不等待完成）
            report_gen.save_html_and_json_async(
                ticker,
                html,
                enhanced_report,
                report_dir,
                report_gen.timestamp
            )

            _log.info("报告已提交异步生成：%s", filename)
            _log.info("数据已提交异步保存：%s", json_filename)
            successful_count += 1

        except Exception as e:
            _log.warning("%s 分析失败: %s", ticker, str(e)[:100])

    # ⭐ Task 3: 等待所有异步文件写入完成
    if MLEnhancedReportGenerator._file_writer_pool:
        MLEnhancedReportGenerator._file_writer_pool.shutdown(wait=True)

    _log.info("=" * 60)
    _log.info("ML 增强报告生成完毕！成功: %d/%d", successful_count, len(tickers))
    _log.info("所有文件已完成写入")
    _log.info("=" * 60)


if __name__ == "__main__":
    main()
