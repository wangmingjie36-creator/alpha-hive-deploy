"""
🐝 Alpha Hive - Catalyst Refinement 系统
优化 3：精细化催化剂信息（时间、预期、历史对标）
"""

import json
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
from enum import Enum


class CatalystType(Enum):
    """催化剂类型"""
    EARNINGS = "earnings"
    FDA_APPROVAL = "fda_approval"
    PRODUCT_LAUNCH = "product_launch"
    MERGER = "merger"
    ECONOMIC_EVENT = "economic_event"


class CatalystSeverity(Enum):
    """催化剂严重程度"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Catalyst:
    """精细化的催化剂数据模型"""

    def __init__(self, ticker: str, catalyst_type: CatalystType):
        self.ticker = ticker
        self.catalyst_type = catalyst_type
        self.event_name = ""
        self.severity = CatalystSeverity.MEDIUM

        # 时间信息
        self.scheduled_date = None  # YYYY-MM-DD
        self.scheduled_time = None  # HH:MM (美东时间)
        self.time_zone = "America/New_York"
        self.time_window_days = 0  # ±多少天可能延期
        self.is_confirmed = False

        # 历史数据
        self.historical_beat_miss_ratio = {}  # {"beat": 0.65, "miss": 0.25, "inline": 0.10}
        self.average_move_magnitude = 0.0  # 平均波动 %
        self.upside_downside_ratio = 1.0  # 上行/下行比例

        # 市场预期
        self.analyst_consensus = "Unknown"  # "Beat", "Miss", "In-line"
        self.consensus_confidence = 0.0  # 0-100%
        self.iv_implied = 0.0  # 期权隐含波动率 %
        self.polymarket_odds = {}  # {"beat": 0.65, "miss": 0.35}

        # 关键指标
        self.key_metrics = {}
        self.break_conditions = []

        # 后续事件
        self.subsequent_events = []

        # 风险因素
        self.risk_factors = []

    def add_historical_data(self, beat_pct: float, miss_pct: float, inline_pct: float,
                            avg_move: float, upside_ratio: float):
        """添加历史对标数据"""
        self.historical_beat_miss_ratio = {
            "beat": beat_pct,
            "miss": miss_pct,
            "inline": inline_pct
        }
        self.average_move_magnitude = avg_move
        self.upside_downside_ratio = upside_ratio

    def add_market_expectation(self, consensus: str, confidence: float,
                               iv_implied: float, polymarket_odds: Dict):
        """添加市场预期数据"""
        self.analyst_consensus = consensus
        self.consensus_confidence = confidence
        self.iv_implied = iv_implied
        self.polymarket_odds = polymarket_odds

    def add_key_metric(self, metric_name: str, current_value: float,
                       estimate: float, threshold: float, importance: str):
        """添加关键指标"""
        self.key_metrics[metric_name] = {
            "current_value": current_value,
            "estimate": estimate,
            "threshold": threshold,
            "importance": importance  # "CRITICAL", "HIGH", "MEDIUM"
        }

    def add_subsequent_event(self, event_name: str, date: str, time: str,
                             description: str, probability: float = 1.0):
        """添加后续事件"""
        self.subsequent_events.append({
            "event_name": event_name,
            "date": date,
            "time": time,
            "description": description,
            "probability": probability
        })

    def add_break_condition(self, condition: str):
        """添加失效条件"""
        self.break_conditions.append(condition)

    def add_risk_factor(self, risk: str):
        """添加风险因素"""
        self.risk_factors.append(risk)

    def get_days_until_event(self) -> int:
        """计算距离事件的天数"""
        event_date = datetime.strptime(self.scheduled_date, "%Y-%m-%d")
        return (event_date - datetime.now()).days

    def get_reliability_grade(self) -> Tuple[str, str]:
        """
        根据确认程度和时间窗口获取可靠性等级
        返回 (等级, 颜色)
        """
        risk_score = 0

        if not self.is_confirmed:
            risk_score += 20
        if self.time_window_days > 5:
            risk_score += 15
        if self.get_days_until_event() < 3:
            risk_score -= 5

        if risk_score < 20:
            return "A+ 极高可靠性", "green"
        elif risk_score < 40:
            return "A 高可靠性", "green"
        elif risk_score < 60:
            return "B 中等可靠性", "yellow"
        else:
            return "C 低可靠性", "red"

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            "ticker": self.ticker,
            "catalyst_type": self.catalyst_type.value,
            "event_name": self.event_name,
            "scheduled_date": self.scheduled_date,
            "scheduled_time": self.scheduled_time,
            "is_confirmed": self.is_confirmed,
            "historical_data": self.historical_beat_miss_ratio,
            "average_move_magnitude": self.average_move_magnitude,
            "analyst_consensus": self.analyst_consensus,
            "polymarket_odds": self.polymarket_odds,
            "key_metrics": self.key_metrics,
            "subsequent_events": self.subsequent_events,
            "risk_factors": self.risk_factors
        }


class CatalystTimeline:
    """催化剂时间线管理"""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.catalysts: List[Catalyst] = []

    def add_catalyst(self, catalyst: Catalyst):
        """添加催化剂"""
        self.catalysts.append(catalyst)
        # 按日期排序
        self.catalysts.sort(key=lambda c: c.scheduled_date if c.scheduled_date else "9999-12-31")

    def get_upcoming_catalysts(self, days_ahead: int = 90) -> List[Catalyst]:
        """获取未来 N 天内的催化剂"""
        cutoff_date = datetime.now() + timedelta(days=days_ahead)
        return [c for c in self.catalysts
                if c.scheduled_date and datetime.strptime(c.scheduled_date, "%Y-%m-%d") <= cutoff_date]

    def generate_timeline_html(self) -> str:
        """生成时间线 HTML"""

        html = f"""
        <section id="catalysts-timeline-{self.ticker}" class="report-section">
            <h2>🎯 催化剂日期 & 时间线（精细化）- {self.ticker}</h2>

            <div class="timeline-container">
        """

        for catalyst in self.get_upcoming_catalysts():
            reliability, color = catalyst.get_reliability_grade()
            days_until = catalyst.get_days_until_event()

            html += f"""
                <div class="catalyst-card">
                    <div class="catalyst-header">
                        <h3>{catalyst.event_name}</h3>
                        <div class="reliability-badge {color}">
                            {reliability}
                        </div>
                    </div>

                    <div class="catalyst-body">
                        <!-- 时间精细化 -->
                        <div class="catalyst-section">
                            <h4>📅 时间精细化</h4>
                            <table class="timing-table">
                                <tr>
                                    <td><strong>确切日期</strong></td>
                                    <td>{catalyst.scheduled_date}</td>
                                </tr>
                                <tr>
                                    <td><strong>发布时间</strong></td>
                                    <td>{catalyst.scheduled_time or 'TBD'}</td>
                                </tr>
                                <tr>
                                    <td><strong>时间确定性</strong></td>
                                    <td>{"✅ 官方确认" if catalyst.is_confirmed else "❌ 未确认"}</td>
                                </tr>
                                <tr>
                                    <td><strong>延期风险</strong></td>
                                    <td>±{catalyst.time_window_days} 天</td>
                                </tr>
                                <tr>
                                    <td><strong>距离现在</strong></td>
                                    <td>{days_until} 天</td>
                                </tr>
                            </table>
                        </div>

                        <!-- 历史对标 -->
                        <div class="catalyst-section">
                            <h4>📈 历史表现对标</h4>
                            <table class="historical-table">
                                <thead>
                                    <tr>
                                        <th>结果类型</th>
                                        <th>历史占比</th>
                                        <th>平均波动</th>
                                        <th>上下行比</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr>
                                        <td>Beat (超预期)</td>
                                        <td>{catalyst.historical_beat_miss_ratio.get('beat', 0):.1%}</td>
                                        <td rowspan="3" style="text-align:center">
                                            <strong>{catalyst.average_move_magnitude:.1f}%</strong>
                                        </td>
                                        <td rowspan="3" style="text-align:center">
                                            <strong>{catalyst.upside_downside_ratio:.2f}x</strong>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td>In-line (符合预期)</td>
                                        <td>{catalyst.historical_beat_miss_ratio.get('inline', 0):.1%}</td>
                                    </tr>
                                    <tr>
                                        <td>Miss (低于预期)</td>
                                        <td>{catalyst.historical_beat_miss_ratio.get('miss', 0):.1%}</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>

                        <!-- 市场预期 -->
                        <div class="catalyst-section">
                            <h4>🎯 市场预期 vs 隐含信息</h4>
                            <table class="expectation-table">
                                <tr>
                                    <td><strong>分析师共识</strong></td>
                                    <td>{catalyst.analyst_consensus} ({catalyst.consensus_confidence:.0f}% 置信)</td>
                                </tr>
                                <tr>
                                    <td><strong>Polymarket 赔率</strong></td>
                                    <td>
                                        {"".join([f"{k.upper()}: {v:.0%} | " for k, v in catalyst.polymarket_odds.items()]).rstrip(" | ")}
                                    </td>
                                </tr>
                                <tr>
                                    <td><strong>期权隐含波动率</strong></td>
                                    <td><strong style="color: #ff9800">{catalyst.iv_implied:.1f}%</strong></td>
                                </tr>
                            </table>
                        </div>

                        <!-- 关键指标 -->
                        <div class="catalyst-section">
                            <h4>🔑 市场最关心的指标</h4>
                            <div class="key-metrics">
        """

            for metric_name, metric_data in catalyst.key_metrics.items():
                importance = metric_data.get("importance", "MEDIUM")
                stars = "⭐" * (3 if importance == "CRITICAL" else 2 if importance == "HIGH" else 1)

                html += f"""
                                <div class="metric-item">
                                    <span class="importance">{stars} {importance}</span>
                                    <strong>{metric_name}</strong>
                                    <p>预期: {metric_data.get('estimate')} | 阈值: {metric_data.get('threshold')}</p>
                                </div>
                """

            html += """
                            </div>
                        </div>

                        <!-- 后续事件 -->
                        <div class="catalyst-section">
                            <h4>📅 后续事件</h4>
                            <div class="subsequent-events">
            """

            for event in catalyst.subsequent_events:
                html += f"""
                                <div class="event-item">
                                    <strong>{event['event_name']}</strong> - {event['date']} {event['time']}<br>
                                    <p>{event['description']}</p>
                                    <small>概率: {event['probability']:.0%}</small>
                                </div>
                """

            html += """
                            </div>
                        </div>

                        <!-- 风险因素 -->
                        <div class="catalyst-section">
                            <h4>⚠️ 风险因素</h4>
                            <ul>
            """

            for risk in catalyst.risk_factors:
                html += f"<li>{risk}</li>"

            html += """
                            </ul>
                        </div>

                        <!-- 失效条件 -->
                        <div class="catalyst-section highlight">
                            <h4>🚨 失效条件</h4>
                            <ul>
            """

            for condition in catalyst.break_conditions:
                html += f"<li>❌ {condition}</li>"

            html += """
                            </ul>
                        </div>
                    </div>
                </div>
            """

        html += """
            </div>

            <style>
                .timeline-container {
                    display: flex;
                    flex-direction: column;
                    gap: 20px;
                }

                .catalyst-card {
                    background: white;
                    border: 2px solid #667eea;
                    border-radius: 8px;
                    padding: 20px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }

                .catalyst-header {
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 20px;
                    border-bottom: 2px solid #eee;
                    padding-bottom: 10px;
                }

                .reliability-badge {
                    padding: 6px 12px;
                    border-radius: 12px;
                    font-size: 12px;
                    font-weight: 600;
                }

                .reliability-badge.green {
                    background: #e8f5e9;
                    color: #27ae60;
                }

                .reliability-badge.yellow {
                    background: #fff3e0;
                    color: #ff9800;
                }

                .reliability-badge.red {
                    background: #ffebee;
                    color: #f44336;
                }

                .catalyst-section {
                    margin: 20px 0;
                    padding: 15px;
                    background: #fafafa;
                    border-radius: 6px;
                    border-left: 3px solid #667eea;
                }

                .catalyst-section.highlight {
                    background: #fff3cd;
                    border-left-color: #ff9800;
                }

                .timing-table, .historical-table, .expectation-table {
                    width: 100%;
                    border-collapse: collapse;
                    margin: 10px 0;
                }

                .timing-table td, .historical-table td, .expectation-table td {
                    padding: 10px;
                    border-bottom: 1px solid #eee;
                }

                .historical-table th {
                    background: #f5f5f5;
                    font-weight: 600;
                    padding: 10px;
                }

                .key-metrics {
                    display: grid;
                    gap: 10px;
                }

                .metric-item {
                    background: white;
                    padding: 12px;
                    border-left: 3px solid #2196f3;
                    border-radius: 4px;
                }

                .importance {
                    color: #f44336;
                    font-weight: 600;
                }

                .subsequent-events {
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                }

                .event-item {
                    background: white;
                    padding: 10px;
                    border-left: 3px solid #4caf50;
                    border-radius: 4px;
                }

                .event-item p {
                    margin: 5px 0;
                    font-size: 13px;
                    color: #666;
                }

                .event-item small {
                    color: #999;
                }
            </style>
        </section>
        """

        return html

    def save_to_json(self, filename: str = None) -> str:
        """保存时间线到 JSON"""

        if filename is None:
            filename = f"catalyst_timeline_{self.ticker}.json"

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump({
                "ticker": self.ticker,
                "catalysts": [c.to_dict() for c in self.catalysts],
                "created_at": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)

        return filename


# 创建具体的催化剂实例
def create_nvda_catalysts() -> CatalystTimeline:
    """创建 NVDA 的催化剂"""

    timeline = CatalystTimeline("NVDA")

    # 财报催化剂
    earnings = Catalyst("NVDA", CatalystType.EARNINGS)
    earnings.event_name = "Q4 FY2026 财报发布"
    earnings.scheduled_date = "2026-03-15"
    earnings.scheduled_time = "16:00"  # NYSE 收盘后
    earnings.is_confirmed = True
    earnings.time_window_days = 0

    # 历史数据
    earnings.add_historical_data(
        beat_pct=0.65,
        miss_pct=0.15,
        inline_pct=0.20,
        avg_move=7.5,
        upside_ratio=1.8
    )

    # 市场预期
    earnings.add_market_expectation(
        consensus="Beat",
        confidence=68,
        iv_implied=15.2,
        polymarket_odds={"beat": 0.65, "miss": 0.22, "inline": 0.13}
    )

    # 关键指标
    earnings.add_key_metric("DataCenter Revenue", 28.5, 28.5, 28.0, "CRITICAL")
    earnings.add_key_metric("Gross Margin", 70.5, 70.5, 68.0, "CRITICAL")
    earnings.add_key_metric("中国市场展望", None, None, None, "CRITICAL")

    # 后续事件
    earnings.add_subsequent_event(
        "Earnings Call",
        "2026-03-15",
        "17:00",
        "CEO 讨论关键指标和中国前景"
    )

    # 风险因素
    earnings.add_risk_factor("宏观经济衰退可能导致 AI 芯片需求下滑")
    earnings.add_risk_factor("竞争对手 AMD 发布更强产品可能压低价格")
    earnings.add_risk_factor("中国禁令风险可能突然升级")

    # 失效条件
    earnings.add_break_condition("财报被延期 > 1 周")
    earnings.add_break_condition("CEO 宣布离职或重大丑闻")
    earnings.add_break_condition("美国芯片出口禁令突然升级")

    earnings.severity = CatalystSeverity.CRITICAL

    timeline.add_catalyst(earnings)

    return timeline


def create_vktx_catalysts() -> CatalystTimeline:
    """创建 VKTX 的催化剂"""

    timeline = CatalystTimeline("VKTX")

    # 临床试验结果
    trial = Catalyst("VKTX", CatalystType.FDA_APPROVAL)
    trial.event_name = "Phase 3 临床试验结果发布"
    trial.scheduled_date = "2026-08-15"  # 预计 Q3（约 8 月中旬）
    trial.is_confirmed = False
    trial.time_window_days = 45

    # 历史数据（生物制药行业）
    trial.add_historical_data(
        beat_pct=0.40,  # 生物制药成功率较低
        miss_pct=0.45,
        inline_pct=0.15,
        avg_move=25.0,  # 波动更大
        upside_ratio=3.5  # 成功时大幅上升
    )

    trial.add_market_expectation(
        consensus="Uncertain",
        confidence=40,
        iv_implied=45.0,  # 高隐含波动率
        polymarket_odds={"success": 0.55, "failure": 0.45}
    )

    trial.add_key_metric("Primary Endpoint", None, None, None, "CRITICAL")
    trial.add_key_metric("Safety Profile", None, None, None, "CRITICAL")

    trial.add_break_condition("FDA 试验暂停（IND Hold）")
    trial.add_break_condition("关键患者脱落 > 15%")

    trial.severity = CatalystSeverity.CRITICAL

    timeline.add_catalyst(trial)

    return timeline


# 使用示例
if __name__ == "__main__":
    # 创建 NVDA 催化剂
    nvda_catalysts = create_nvda_catalysts()
    print(f"✅ NVDA 催化剂已创建，共 {len(nvda_catalysts.catalysts)} 个")

    # 生成 HTML
    html = nvda_catalysts.generate_timeline_html()
    print("✅ HTML 已生成")

    # 保存到 JSON
    nvda_catalysts.save_to_json()
    print("✅ 催化剂已保存到 JSON")

    # 打印即将发生的催化剂
    upcoming = nvda_catalysts.get_upcoming_catalysts(days_ahead=30)
    print(f"\n📅 未来 30 天的催化剂：{len(upcoming)} 个")
    for catalyst in upcoming:
        print(f"  - {catalyst.event_name} ({catalyst.scheduled_date})")
