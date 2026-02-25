"""
🐝 Alpha Hive - Feedback Loop 系统
优化 7：准确度回溯、权重自动优化
"""

import logging as _logging
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from statistics import mean, stdev

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
        self.agent_votes = {}  # {"Scout": 8.2, "SentimentBee": 7.5, ...}

        # 使用的权重
        self.weights_used = {
            "signal": 0.30,
            "catalyst": 0.20,
            "sentiment": 0.20,
            "odds": 0.15,
            "risk_adj": 0.15
        }

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

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump({
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
            }, f, ensure_ascii=False, indent=2)

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

        for snapshot in self.snapshots:
            # 方向准确性
            direction_accuracy = snapshot.check_direction_accuracy()
            if timeframe in direction_accuracy and direction_accuracy[timeframe] is not None:
                accuracies.append(1 if direction_accuracy[timeframe] else 0)
                total_count += 1
                if direction_accuracy[timeframe]:
                    win_count += 1

            # 收益
            returns = snapshot.calculate_returns()
            if timeframe in returns:
                total_return += returns[timeframe]

        if not accuracies:
            return {}

        accuracy_pct = (sum(accuracies) / len(accuracies)) * 100
        avg_return = total_return / len(accuracies)

        return {
            "direction_accuracy": accuracy_pct,
            "win_rate": (win_count / total_count) * 100 if total_count > 0 else 0,
            "avg_return": avg_return,
            "total_trades": len(accuracies),
            "sharpe_ratio": self._calculate_sharpe(accuracies, avg_return)
        }

    def _calculate_sharpe(self, accuracies: List, avg_return: float) -> float:
        """计算 Sharpe 比率（简化版）"""
        if len(accuracies) < 2:
            return 0.0

        # 将准确性转换为收益率
        returns = [r if acc else -r for acc, r in zip(accuracies, [avg_return] * len(accuracies))]

        if len(set(returns)) == 1:
            return 0.0

        std_dev = stdev(returns)
        if std_dev == 0:
            return 0.0

        # 简化 Sharpe（年化）
        return (mean(returns) / std_dev) * (252 ** 0.5)

    def calculate_agent_contribution(self) -> Dict:
        """计算每个 Agent 的准确度贡献"""

        agent_scores = {
            "Scout": [],
            "SentimentBee": [],
            "OddsBee": [],
            "CatalystBee": [],
            "CrossBee": [],
            "ValidatorBee": []
        }

        for snapshot in self.snapshots:
            direction_accuracy = snapshot.check_direction_accuracy()

            if "t7" in direction_accuracy:
                is_correct = direction_accuracy["t7"]

                for agent_name, agent_score in snapshot.agent_votes.items():
                    if agent_name in agent_scores:
                        # 如果 Agent 评分高且预测正确，记 1；否则记 0
                        score_correct = 1 if (agent_score > 5 and is_correct) or (agent_score <= 5 and not is_correct) else 0
                        agent_scores[agent_name].append(score_correct)

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

        # 标准化为 0-1
        total_accuracy = sum(agent_accuracy.values())
        if total_accuracy == 0:
            return {}

        normalized_accuracy = {
            agent: score / total_accuracy
            for agent, score in agent_accuracy.items()
        }

        # 分配新权重
        new_weights = {}
        weight_mapping = {
            "signal": ["Scout", "CrossBee"],
            "sentiment": ["SentimentBee"],
            "odds": ["OddsBee"],
            "catalyst": ["CatalystBee"],
            "risk_adj": ["ValidatorBee"]
        }

        for category, agents in weight_mapping.items():
            category_accuracy = sum(normalized_accuracy.get(agent, 0) for agent in agents)
            new_weights[category] = min(0.35, max(0.10, category_accuracy))

        # 归一化使总和 = 1
        total = sum(new_weights.values())
        new_weights = {k: v / total for k, v in new_weights.items()}

        # 对比旧权重
        old_weights = {
            "signal": 0.30,
            "catalyst": 0.20,
            "sentiment": 0.20,
            "odds": 0.15,
            "risk_adj": 0.15
        }

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

    def generate_accuracy_dashboard_html(self) -> str:
        """生成准确度看板 HTML"""

        accuracy_t1 = self.calculate_accuracy("t1")
        accuracy_t7 = self.calculate_accuracy("t7")
        accuracy_t30 = self.calculate_accuracy("t30")
        weight_adjustments = self.suggest_weight_adjustments()
        agent_accuracy = weight_adjustments.get("agent_accuracy", {})

        html = """
        <html>
        <head>
            <title>Alpha Hive 准确度看板</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                    background: #f5f5f5;
                    padding: 20px;
                }
                .container {
                    max-width: 1400px;
                    margin: 0 auto;
                }
                .metric-card {
                    background: white;
                    border-radius: 8px;
                    padding: 20px;
                    margin: 20px 0;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }
                .metric-grid {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                    gap: 20px;
                }
                .metric-box {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 20px;
                    border-radius: 8px;
                    text-align: center;
                }
                .metric-value {
                    font-size: 32px;
                    font-weight: 700;
                    margin: 10px 0;
                }
                .metric-label {
                    font-size: 14px;
                    opacity: 0.9;
                }
                table {
                    width: 100%;
                    border-collapse: collapse;
                    margin: 15px 0;
                }
                th {
                    background: #f5f5f5;
                    padding: 12px;
                    text-align: left;
                    font-weight: 600;
                    border-bottom: 2px solid #ddd;
                }
                td {
                    padding: 12px;
                    border-bottom: 1px solid #eee;
                }
                .up { color: #27ae60; }
                .down { color: #e74c3c; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>📊 Alpha Hive 准确度看板</h1>

                <!-- 综合指标 -->
                <div class="metric-card">
                    <h2>🎯 综合准确度指标</h2>
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
                            <div class="metric-value">{sharpe:.2f}</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">平均收益 (T+7)</div>
                            <div class="metric-value">{avg_return:+.1f}%</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-label">胜率</div>
                            <div class="metric-value">{win_rate:.0f}%</div>
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

        old_weights = {
            "Scout": 0.30,
            "SentimentBee": 0.20,
            "OddsBee": 0.15,
            "CatalystBee": 0.20,
            "CrossBee": 0.10,
            "ValidatorBee": 0.05
        }

        for agent, accuracy in agent_accuracy.items():
            adjustment = weight_adjustments.get("weight_adjustments", {})
            change = ""
            if adjustment:
                for key, val in adjustment.items():
                    if agent in key or key in agent.lower():
                        change = f"{val['direction']} {val['change']}"
                        break

            html += f"""
                            <tr>
                                <td>{agent}</td>
                                <td>{accuracy:.0f}%</td>
                                <td>{old_weights.get(agent, 0):.0%}</td>
                                <td>{old_weights.get(agent, 0):.0%}</td>
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
        """.format(
            accuracy_t1_dir=accuracy_t1.get("direction_accuracy", 0),
            accuracy_t7_dir=accuracy_t7.get("direction_accuracy", 0),
            accuracy_t30_dir=accuracy_t30.get("direction_accuracy", 0),
            sharpe=accuracy_t7.get("sharpe_ratio", 0),
            avg_return=accuracy_t7.get("avg_return", 0),
            win_rate=accuracy_t7.get("win_rate", 0)
        )

        return html

    def save_accuracy_dashboard(self, filename: str = "accuracy_dashboard.html") -> str:
        """保存准确度看板"""
        html = self.generate_accuracy_dashboard_html()

        with open(filename, 'w', encoding='utf-8') as f:
            f.write(html)

        return filename


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
        "Scout": 8.5,
        "SentimentBee": 8.2,
        "OddsBee": 8.8,
        "CatalystBee": 8.7,
        "CrossBee": 8.6,
        "ValidatorBee": 8.3
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
