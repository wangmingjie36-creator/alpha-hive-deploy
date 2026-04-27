"""
🐝 Alpha Hive - Feedback Loop 系统
优化 7：准确度回溯、权重自动优化
"""

import logging as _logging
import json
import os
from datetime import datetime
from typing import Dict, List
from statistics import mean, stdev
from hive_logger import SafeJSONEncoder, atomic_json_write

_log = _logging.getLogger("alpha_hive.feedback_loop")


class ReportSnapshot:
    """报告快照 - 保存生成报告时的完整信息"""

    def __init__(self, ticker: str, date: str):
        self.ticker = ticker
        self.date = date
        self.report_id = f"{ticker}_{date}"

        # 输出数据
        self.composite_score = 0.0
        self.direction = "Neutral"  # "Long", "Short", "Neutral"
        self.price_target = 0.0
        self.stop_loss = 0.0
        self.entry_price = 0.0

        # Agent 评分
        self.agent_votes = {}  # {"ScoutBeeNova": 8.2, "BuzzBeeWhisper": 7.5, ...}

        # 使用的权重（从 config 读取，带兜底）
        _fallback_w = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15}
        try:
            from config import EVALUATION_WEIGHTS as _EW
            self.weights_used = {k: _EW.get(k, _fallback_w[k]) for k in _fallback_w}
        except (ImportError, AttributeError):
            self.weights_used = dict(_fallback_w)

        # 实际结果（后续填充）
        self.actual_price_t1 = None  # T+1 的价格
        self.actual_price_t7 = None  # T+7 的价格
        self.actual_price_t30 = None  # T+30 的价格

    def calculate_returns(self) -> Dict:
        """计算各时间段的实际收益"""
        returns = {}

        if self.actual_price_t1 and self.entry_price:
            returns["t1"] = ((self.actual_price_t1 - self.entry_price) / self.entry_price) * 100
        if self.actual_price_t7 and self.entry_price:
            returns["t7"] = ((self.actual_price_t7 - self.entry_price) / self.entry_price) * 100
        if self.actual_price_t30 and self.entry_price:
            returns["t30"] = ((self.actual_price_t30 - self.entry_price) / self.entry_price) * 100

        return returns

    def check_direction_accuracy(self) -> Dict:
        """检查方向预测准确性"""
        returns = self.calculate_returns()
        accuracy = {}

        for timeframe, ret in returns.items():
            if self.direction == "Long":
                accuracy[timeframe] = ret > 0
            elif self.direction == "Short":
                accuracy[timeframe] = ret < 0
            else:
                accuracy[timeframe] = None

        return accuracy

    def save_to_json(self, directory: str = "report_snapshots") -> str:
        """保存快照到 JSON 文件"""

        os.makedirs(directory, exist_ok=True)
        filename = os.path.join(directory, f"{self.report_id}.json")

        atomic_json_write(filename, {
            "ticker": self.ticker,
            "date": self.date,
            "composite_score": self.composite_score,
            "direction": self.direction,
            "price_target": self.price_target,
            "stop_loss": self.stop_loss,
            "entry_price": self.entry_price,
            "agent_votes": self.agent_votes,
            "weights_used": self.weights_used,
            "actual_prices": {
                "t1": self.actual_price_t1,
                "t7": self.actual_price_t7,
                "t30": self.actual_price_t30
            },
            "created_at": datetime.now().isoformat()
        }, indent=2, default=lambda o: SafeJSONEncoder().default(o))

        return filename

    @classmethod
    def load_from_json(cls, filename: str) -> "ReportSnapshot":
        """从 JSON 文件加载快照"""

        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)

        snapshot = cls(data["ticker"], data["date"])
        snapshot.composite_score = data.get("composite_score", 0.0)
        snapshot.direction = data.get("direction", "Neutral")
        snapshot.price_target = data.get("price_target", 0.0)
        snapshot.stop_loss = data.get("stop_loss", 0.0)
        snapshot.entry_price = data.get("entry_price", 0.0)
        snapshot.agent_votes = data.get("agent_votes", {})
        snapshot.weights_used = data.get("weights_used", {})

        actual_prices = data.get("actual_prices", {})
        snapshot.actual_price_t1 = actual_prices.get("t1")
        snapshot.actual_price_t7 = actual_prices.get("t7")
        snapshot.actual_price_t30 = actual_prices.get("t30")

        return snapshot


class BacktestAnalyzer:
    """回溯测试分析器"""

    def __init__(self, directory: str = "report_snapshots"):
        self.directory = directory
        self.snapshots = self._load_all_snapshots()

    def _load_all_snapshots(self) -> List[ReportSnapshot]:
        """加载所有快照"""
        snapshots = []

        if not os.path.exists(self.directory):
            return snapshots

        for filename in os.listdir(self.directory):
            if filename.endswith(".json"):
                try:
                    snapshot = ReportSnapshot.load_from_json(
                        os.path.join(self.directory, filename)
                    )
                    snapshots.append(snapshot)
                except (json.JSONDecodeError, OSError, KeyError, ValueError) as e:
                    _log.error("加载 %s 失败: %s", filename, e, exc_info=True)

        return snapshots

    def get_snapshots_by_ticker(self, ticker: str) -> List[ReportSnapshot]:
        """按标的获取快照"""
        return [s for s in self.snapshots if s.ticker == ticker]

    def calculate_accuracy(self, timeframe: str = "t7") -> Dict:
        """
        计算准确度指标

        Args:
            timeframe: "t1", "t7", or "t30"
        """

        if not self.snapshots:
            return {}

        accuracies = []
        total_return = 0.0
        win_count = 0
        total_count = 0
        direction_adjusted_returns = []

        for snapshot in self.snapshots:
            # 方向准确性
            direction_accuracy = snapshot.check_direction_accuracy()
            if timeframe in direction_accuracy and direction_accuracy[timeframe] is not None:
                accuracies.append(1 if direction_accuracy[timeframe] else 0)
                total_count += 1
                if direction_accuracy[timeframe]:
                    win_count += 1

            # 收益（方向调整：做空时股价下跌 = 正收益）
            returns = snapshot.calculate_returns()
            if timeframe in returns:
                _dir = snapshot.direction.lower()
                if _dir in ("short", "bearish"):
                    total_return += -returns[timeframe]
                    # 收集实际每笔收益率用于 Sharpe 计算
                    direction_adjusted_returns.append(-returns[timeframe])
                elif _dir in ("long", "bullish"):
                    total_return += returns[timeframe]
                    # 收集实际每笔收益率用于 Sharpe 计算
                    direction_adjusted_returns.append(returns[timeframe])
                # neutral: 不计入方向性收益

        if not accuracies:
            return {}

        accuracy_pct = (sum(accuracies) / len(accuracies)) * 100
        avg_return = total_return / len(accuracies)

        return {
            "direction_accuracy": accuracy_pct,
            "win_rate": (win_count / total_count) * 100 if total_count > 0 else 0,
            "avg_return": avg_return,
            "total_trades": len(accuracies),
            "sharpe_ratio": self._calculate_sharpe(direction_adjusted_returns),
            "profit_factor": self._calculate_profit_factor(direction_adjusted_returns),
            "information_ratio": self._calculate_information_ratio(direction_adjusted_returns),
            "max_consecutive_losses": self._calculate_max_consecutive_losses(accuracies)
        }

    def _calculate_sharpe(self, direction_adjusted_returns: List[float],
                          risk_free_annual: float = 0.05,
                          period_days: int = 7) -> float:
        """计算 Sharpe 比率（使用实际每笔收益率，非平均值）

        Args:
            direction_adjusted_returns: 方向调整后的实际收益率列表（百分比）
            risk_free_annual: 年化无风险利率
            period_days: 持有期天数
        """
        if len(direction_adjusted_returns) < 2:
            return 0.0

        periods_per_year = 252 / period_days
        rf_per_period = risk_free_annual / periods_per_year
        excess = [r / 100.0 - rf_per_period for r in direction_adjusted_returns]

        mean_excess = sum(excess) / len(excess)
        variance = sum((x - mean_excess) ** 2 for x in excess) / (len(excess) - 1)
        std_dev = variance ** 0.5

        if std_dev == 0:
            return 0.0

        return round((mean_excess / std_dev) * (periods_per_year ** 0.5), 3)

    def _calculate_profit_factor(self, direction_adjusted_returns: List[float]) -> float:
        """Profit Factor = 赢钱总和 / 亏钱总和，目标 >2.0"""
        if not direction_adjusted_returns:
            return 0.0
        gross_profit = sum(r for r in direction_adjusted_returns if r > 0)
        gross_loss = abs(sum(r for r in direction_adjusted_returns if r < 0))
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        return round(gross_profit / gross_loss, 3)

    def _calculate_information_ratio(self, direction_adjusted_returns: List[float],
                                      benchmark_annual: float = 0.10) -> float:
        """Information Ratio = (年化收益 - 基准) / 跟踪误差

        Args:
            direction_adjusted_returns: 方向调整后的收益率列表（百分比）
            benchmark_annual: 年化基准收益率（默认 SPY ~10%）
        """
        if len(direction_adjusted_returns) < 5:
            return 0.0
        periods_per_year = 252 / 7  # T+7 周期
        benchmark_per_period = benchmark_annual / periods_per_year

        excess = [r / 100.0 - benchmark_per_period for r in direction_adjusted_returns]
        mean_excess = sum(excess) / len(excess)
        variance = sum((x - mean_excess) ** 2 for x in excess) / (len(excess) - 1)
        tracking_error = variance ** 0.5

        if tracking_error == 0:
            return 0.0
        return round((mean_excess / tracking_error) * (periods_per_year ** 0.5), 3)

    def _calculate_max_consecutive_losses(self, accuracies: List[int]) -> int:
        """最大连续亏损次数"""
        if not accuracies:
            return 0
        max_streak = 0
        current_streak = 0
        for acc in accuracies:
            if acc == 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        return max_streak

    # 旧名称 → 新名称兼容映射（旧快照可能用旧名存储 agent_votes）
    _LEGACY_AGENT_MAP = {
        "Scout": "ScoutBeeNova",
        "SentimentBee": "BuzzBeeWhisper",
        "OddsBee": "OracleBeeEcho",
        "CatalystBee": "ChronosBeeHorizon",
        "CrossBee": "RivalBeeVanguard",
        "ValidatorBee": "GuardBeeSentinel",
    }

    def calculate_agent_contribution(self) -> Dict:
        """计算每个 Agent 的准确度贡献"""

        # 修复 Bug #6：BearBeeContrarian 纳入学习闭环（risk_adj 维度）
        # 旧实现排除 BearBee 导致"BearBee 预警正确时其他蜂错被惩罚，系统越学越不看空"
        agent_scores = {
            "ScoutBeeNova": [],
            "BuzzBeeWhisper": [],
            "OracleBeeEcho": [],
            "ChronosBeeHorizon": [],
            "RivalBeeVanguard": [],
            "GuardBeeSentinel": [],
            "BearBeeContrarian": [],
        }

        for snapshot in self.snapshots:
            direction_accuracy = snapshot.check_direction_accuracy()

            if "t7" in direction_accuracy:
                is_correct = direction_accuracy["t7"]

                for agent_name, agent_score in snapshot.agent_votes.items():
                    # 兼容旧名称：将旧 key 映射到新 Agent ID
                    canonical = self._LEGACY_AGENT_MAP.get(agent_name, agent_name)
                    if canonical in agent_scores:
                        # 如果 Agent 评分高且预测正确，记 1；否则记 0
                        score_correct = 1 if (agent_score > 5 and is_correct) or (agent_score <= 5 and not is_correct) else 0
                        agent_scores[canonical].append(score_correct)

        # 计算平均准确度
        agent_accuracy = {}
        for agent, scores in agent_scores.items():
            if scores:
                agent_accuracy[agent] = (sum(scores) / len(scores)) * 100
            else:
                agent_accuracy[agent] = 0.0

        return agent_accuracy

    def suggest_weight_adjustments(self) -> Dict:
        """建议权重调整"""

        agent_accuracy = self.calculate_agent_contribution()

        if not agent_accuracy or sum(agent_accuracy.values()) == 0:
            return {}

        # 修复 Bug #7：按"维度内 Agent 准确度均值"计算权重，而非"几只蜂映射到该维度就加几次"
        # 旧实现：signal 维度 = Scout + Rival 两蜂准确率相加 → 结构性高于单蜂维度
        # 新实现：signal 维度 = avg(Scout, Rival)，与其他单蜂维度口径一致
        weight_mapping = {
            "signal": ["ScoutBeeNova", "RivalBeeVanguard"],
            "sentiment": ["BuzzBeeWhisper"],
            "odds": ["OracleBeeEcho"],
            "catalyst": ["ChronosBeeHorizon"],
            "risk_adj": ["GuardBeeSentinel", "BearBeeContrarian"],  # 修复 #6
        }

        # 每个维度先算"维度内准确度均值"（0~100），再对维度间归一化
        dim_avg_accuracy = {}
        for category, agents in weight_mapping.items():
            vals = [agent_accuracy.get(a, 0.0) for a in agents if agent_accuracy.get(a, 0.0) > 0]
            dim_avg_accuracy[category] = (sum(vals) / len(vals)) if vals else 0.0

        total_dim_acc = sum(dim_avg_accuracy.values())
        if total_dim_acc <= 0:
            return {}

        new_weights = {}
        for category in weight_mapping:
            raw = dim_avg_accuracy[category] / total_dim_acc  # 维度间归一化
            new_weights[category] = min(0.35, max(0.10, raw))

        # 归一化使总和 = 1（方案19: 除零守卫）
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}
        else:
            _log.warning("feedback_loop: 权重总和为零，回退默认权重")
            new_weights = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15}

        # 对比旧权重（从 config 读取，带兜底）
        _fallback_w = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15}
        try:
            from config import EVALUATION_WEIGHTS as _EW
            old_weights = {k: _EW.get(k, _fallback_w[k]) for k in _fallback_w}
        except (ImportError, AttributeError):
            old_weights = dict(_fallback_w)

        comparison = {}
        for key in old_weights:
            change_pct = (new_weights[key] - old_weights[key]) * 100
            comparison[key] = {
                "old": old_weights[key],
                "new": new_weights[key],
                "change": f"{change_pct:+.1f}%",
                "direction": "↑" if change_pct > 0 else "↓" if change_pct < 0 else "→"
            }

        return {
            "agent_accuracy": agent_accuracy,
            "weight_adjustments": comparison,
            "new_weights": new_weights
        }

    def analyze_misses_with_llm(self, ticker: str, timeframe: str = "t7") -> list:
        """LLM 增强预测复盘：对方向判断错误的预测做根因分析

        仅在 LLM 可用时运行，不可用时返回空列表。
        每次最多分析 3 条最近的失败预测（控制 API 成本）。

        Returns:
            [{miss_category, root_cause, agent_blame, lesson_learned,
              weight_suggestion}, ...]
        """
        try:
            import llm_service
            if not llm_service.is_available():
                return []
        except ImportError:
            return []

        ticker_snaps = self.get_snapshots_by_ticker(ticker)
        if not ticker_snaps:
            return []

        misses = []
        for snap in ticker_snaps[-10:]:  # 只看最近 10 条
            direction_acc = snap.check_direction_accuracy()
            tf_correct = direction_acc.get(timeframe)
            if tf_correct is False:  # 明确方向错误
                returns = snap.calculate_returns()
                prediction = {
                    "date": snap.date,
                    "direction": snap.direction,
                    "score": getattr(snap, "final_score", None),
                    "narrative": getattr(snap, "narrative", ""),
                }
                actual = {
                    f"return_{timeframe}": returns.get(timeframe),
                    "direction_correct": False,
                }
                result = llm_service.analyze_prediction_miss(ticker, prediction, actual)
                if result:
                    result["prediction_date"] = snap.date
                    misses.append(result)
                if len(misses) >= 3:  # 最多 3 条，控制 API 成本
                    break

        return misses

    def generate_accuracy_dashboard_html(self) -> str:
        """生成准确度看板 HTML"""

        accuracy_t1 = self.calculate_accuracy("t1")
        accuracy_t7 = self.calculate_accuracy("t7")
        accuracy_t30 = self.calculate_accuracy("t30")
        weight_adjustments = self.suggest_weight_adjustments()
        agent_accuracy = weight_adjustments.get("agent_accuracy", {})

        # 提取指标值
        accuracy_t1_dir = accuracy_t1.get("direction_accuracy", 0)
        accuracy_t7_dir = accuracy_t7.get("direction_accuracy", 0)
        accuracy_t30_dir = accuracy_t30.get("direction_accuracy", 0)
        sharpe = accuracy_t7.get("sharpe_ratio", 0)
        avg_return = accuracy_t7.get("avg_return", 0)
        win_rate = accuracy_t7.get("win_rate", 0)
        profit_factor = accuracy_t7.get("profit_factor", 0)
        max_consecutive_losses = accuracy_t7.get("max_consecutive_losses", 0)

        html = f"""
        <html>
        <head>
            <title>Alpha Hive 准确度看板</title>
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    background: #f5f5f5;
                    padding: 20px;
                }}
                .container {{
                    max-width: 1400px;
                    margin: 0 auto;
                }}
                .metric-card {{
                    background: white;
                    border-radius: 8px;
                    padding: 20px;
                    margin: 20px 0;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }}
                .metric-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                    gap: 20px;
                }}
                .metric-box {{
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 20px;
                    border-radius: 8px;
                    text-align: center;
                }}
                .metric-value {{
                    font-size: 32px;
                    font-weight: 700;
                    margin: 10px 0;
                }}
                .metric-label {{
                    font-size: 14px;
                    opacity: 0.9;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin: 15px 0;
                }}
                th {{
                    background: #f5f5f5;
                    padding: 12px;
                    text-align: left;
                    font-weight: 600;
                    border-bottom: 2px solid #ddd;
                }}
                td {{
                    padding: 12px;
                    border-bottom: 1px solid #eee;
                }}
                .up {{ color: #27ae60; }}
                .down {{ color: #e74c3c; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Alpha Hive 准确度看板</h1>

                <!-- 综合指标 -->
                <div class="metric-card">
                    <h2>综合准确度指标</h2>
                    <div class="metric-grid">
                        <div class="metric-box">
                            <div class="metric-label">T+1 方向准确度</div>
                            <div class="metric-value">{accuracy_t1_dir:.0f}%</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">T+7 方向准确度</div>
                            <div class="metric-value">{accuracy_t7_dir:.0f}%</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">T+30 方向准确度</div>
                            <div class="metric-value">{accuracy_t30_dir:.0f}%</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">Sharpe 比率 (T+7)</div>
                            <div class="metric-value">{sharpe:.3f}</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">平均收益 (T+7)</div>
                            <div class="metric-value">{avg_return:+.2f}%</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">胜率</div>
                            <div class="metric-value">{win_rate:.0f}%</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">Profit Factor</div>
                            <div class="metric-value">{profit_factor}</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">最大连续亏损</div>
                            <div class="metric-value">{max_consecutive_losses}</div>
                        </div>
                    </div>
                </div>

                <!-- Agent 贡献度 -->
                <div class="metric-card">
                    <h2>🐝 Agent 贡献度分析</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Agent</th>
                                <th>准确度</th>
                                <th>当前权重</th>
                                <th>建议权重</th>
                                <th>变更</th>
                            </tr>
                        </thead>
                        <tbody>
        """

        # Agent → 维度 → 显示权重（用于 HTML 报表）
        _AGENT_TO_DIM = {
            "ScoutBeeNova": "signal",
            "BuzzBeeWhisper": "sentiment",
            "OracleBeeEcho": "odds",
            "ChronosBeeHorizon": "catalyst",
            "RivalBeeVanguard": "signal",  # 辅助 signal 维度
            "GuardBeeSentinel": "risk_adj",
        }
        _fallback_dw = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15}
        try:
            from config import EVALUATION_WEIGHTS as _EW
            _DIM_WEIGHTS = {k: _EW.get(k, _fallback_dw[k]) for k in _fallback_dw}
        except (ImportError, AttributeError):
            _DIM_WEIGHTS = dict(_fallback_dw)

        for agent, accuracy in agent_accuracy.items():
            dim = _AGENT_TO_DIM.get(agent, "")
            adj_detail = weight_adjustments.get("weight_adjustments", {}).get(dim, {})
            change = f"{adj_detail['direction']} {adj_detail['change']}" if adj_detail else ""

            old_w = _DIM_WEIGHTS.get(dim, 0)
            new_w = weight_adjustments.get("new_weights", {}).get(dim, old_w)
            html += f"""
                            <tr>
                                <td>{agent}</td>
                                <td>{accuracy:.0f}%</td>
                                <td>{old_w:.0%}</td>
                                <td>{new_w:.0%}</td>
                                <td>{change}</td>
                            </tr>
            """

        html += f"""
                        </tbody>
                    </table>
                </div>

                <!-- 权重建议 -->
                <div class="metric-card">
                    <h2>⚙️ 建议的权重调整</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>维度</th>
                                <th>当前权重</th>
                                <th>建议权重</th>
                                <th>变更</th>
                                <th>建议</th>
                            </tr>
                        </thead>
                        <tbody>
        """

        for dimension, values in weight_adjustments.get("weight_adjustments", {}).items():
            change_class = "up" if values['direction'] == "↑" else "down" if values['direction'] == "↓" else ""
            html += f"""
                            <tr>
                                <td><strong>{dimension.upper()}</strong></td>
                                <td>{values['old']:.1%}</td>
                                <td>{values['new']:.1%}</td>
                                <td class="{change_class}"><strong>{values['direction']} {values['change']}</strong></td>
                                <td>
        """

            if values['direction'] == "↑":
                html += "该维度表现优于平均，建议提高权重"
            elif values['direction'] == "↓":
                html += "该维度表现低于平均，建议降低权重"
            else:
                html += "维度表现符合预期，保持不变"

            html += """
                                </td>
                            </tr>
            """

        html += """
                        </tbody>
                    </table>
                </div>
            </div>
        </body>
        </html>
        """

        return html

    def save_accuracy_dashboard(self, filename: str = "accuracy_dashboard.html") -> str:
        """保存准确度看板"""
        html = self.generate_accuracy_dashboard_html()

        with open(filename, 'w', encoding='utf-8') as f:
            f.write(html)

        return filename


# ─────────────────────────────────────────────
# P2-⑧ 误判 → thesis_breaks 自动回写（v0.20.0）
# 把事后归因转化为事前预警：每条误判映射到 thesis_breaks_config 的
# auto_misjudgment_patterns 节点，下次出现同模式时主动告警。
# ─────────────────────────────────────────────

import os as _os
import json as _json
from datetime import datetime as _dt


def register_misjudgment_pattern(
    ticker: str,
    miss_date: str,
    direction: str,
    price_chg_pct: float,
    primary_reason: str,
    signals: dict,
    thesis_config_path: str = None,
) -> dict:
    """
    把一条误判模式注册到 thesis_breaks_config.json:auto_misjudgment_patterns。

    pattern_key 由 (direction + primary_reason 关键词) 哈希得到，相同条件命中时计数+1。
    达到 hits_threshold (默认 3) 后自动激活为 active warning。
    """
    if thesis_config_path is None:
        thesis_config_path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)),
            'thesis_breaks_config.json'
        )

    # 提取触发信号的关键 key（仅 True 项）
    sig_keys = sorted([k for k, v in (signals or {}).items() if v])
    pattern_key = f"{direction}|{primary_reason[:20]}|{','.join(sig_keys[:3])}"

    try:
        with open(thesis_config_path, 'r', encoding='utf-8') as f:
            cfg = _json.load(f)
    except Exception as e:
        _log.warning(f"thesis_config 读取失败: {e}")
        return {'registered': False, 'error': str(e)}

    if 'auto_misjudgment_patterns' not in cfg:
        cfg['auto_misjudgment_patterns'] = {}

    bucket = cfg['auto_misjudgment_patterns'].setdefault(ticker, {})
    rec = bucket.setdefault(pattern_key, {
        'pattern_key': pattern_key,
        'direction': direction,
        'reason': primary_reason,
        'signal_keys': sig_keys[:3],
        'hits': 0,
        'last_hit_date': miss_date,
        'first_hit_date': miss_date,
        'recent_drawdowns': [],
        'active_warning': False,
        'hits_threshold': 3,
    })
    rec['hits'] = rec.get('hits', 0) + 1
    rec['last_hit_date'] = miss_date
    drawdowns = rec.get('recent_drawdowns', [])
    drawdowns.append({'date': miss_date, 'price_chg_pct': price_chg_pct})
    rec['recent_drawdowns'] = drawdowns[-10:]  # 保留最近 10 次
    rec['avg_drawdown'] = round(
        sum(d['price_chg_pct'] for d in rec['recent_drawdowns']) / len(rec['recent_drawdowns']), 2
    )
    if rec['hits'] >= rec.get('hits_threshold', 3):
        rec['active_warning'] = True

    cfg['_meta'] = cfg.get('_meta', {})
    cfg['_meta']['auto_patterns_updated_at'] = _dt.now().isoformat()

    try:
        with open(thesis_config_path, 'w', encoding='utf-8') as f:
            _json.dump(cfg, f, ensure_ascii=False, indent=2)
        _log.info(f"误判模式已注册 {ticker}/{pattern_key} hits={rec['hits']} active={rec['active_warning']}")
        return {'registered': True, 'pattern': rec}
    except Exception as e:
        _log.warning(f"thesis_config 写入失败: {e}")
        return {'registered': False, 'error': str(e)}


def check_misjudgment_warnings(ticker: str, direction: str, signals: dict,
                               thesis_config_path: str = None) -> list:
    """
    在生成报告时调用：基于当前方向和信号，从 thesis_breaks_config 中查找
    已激活的误判模式并返回警告列表。
    """
    if thesis_config_path is None:
        thesis_config_path = _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)),
            'thesis_breaks_config.json'
        )
    warnings = []
    try:
        with open(thesis_config_path, 'r', encoding='utf-8') as f:
            cfg = _json.load(f)
    except Exception:
        return warnings

    bucket = (cfg.get('auto_misjudgment_patterns') or {}).get(ticker, {})
    sig_keys = set(k for k, v in (signals or {}).items() if v)
    for pattern_key, rec in bucket.items():
        if not rec.get('active_warning'):
            continue
        if rec.get('direction') != direction:
            continue
        # 信号交集 ≥ 50% 视为同模式
        rec_sigs = set(rec.get('signal_keys', []))
        if not rec_sigs:
            continue
        overlap = len(rec_sigs & sig_keys)
        if overlap / max(len(rec_sigs), 1) >= 0.5:
            warnings.append({
                'pattern_key': pattern_key,
                'reason': rec.get('reason', ''),
                'hits': rec.get('hits', 0),
                'avg_drawdown': rec.get('avg_drawdown', 0),
                'last_hit_date': rec.get('last_hit_date', ''),
                'severity': 'HIGH' if rec['hits'] >= 5 else 'MEDIUM',
            })
    return warnings


# 使用示例
if __name__ == "__main__":
    # 创建快照
    snapshot = ReportSnapshot("NVDA", "2026-02-23")
    snapshot.composite_score = 8.52
    snapshot.direction = "Long"
    snapshot.price_target = 650
    snapshot.stop_loss = 580
    snapshot.entry_price = 640

    snapshot.agent_votes = {
        "ScoutBeeNova": 8.5,
        "BuzzBeeWhisper": 8.2,
        "OracleBeeEcho": 8.8,
        "ChronosBeeHorizon": 8.7,
        "RivalBeeVanguard": 8.6,
        "GuardBeeSentinel": 8.3,
    }

    # 保存
    snapshot.save_to_json()
    print("✅ 快照已保存")

    # 模拟后续价格（实际应从数据源获取）
    snapshot.actual_price_t1 = 648
    snapshot.actual_price_t7 = 655
    snapshot.actual_price_t30 = 620

    # 回溯分析
    analyzer = BacktestAnalyzer()
    accuracy = analyzer.calculate_accuracy("t7")
    print(f"✅ T+7 准确度: {accuracy}")

    # 保存仪表板
    dashboard_path = analyzer.save_accuracy_dashboard()
    print(f"✅ 准确度仪表板已保存到 {dashboard_path}")
