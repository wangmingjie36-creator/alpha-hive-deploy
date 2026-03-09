"""
🐝 Alpha Hive - Thesis Breaks 监控系统
优化 5：明确定义和监控失效条件

配置数据已外提至 thesis_breaks_config.json（方案五 Step 1）。
ThesisBreakConfig 延迟加载 JSON，API 签名完全不变。
"""

import json
import os
import logging as _logging
from datetime import datetime
from typing import Dict, List, Optional

_log = _logging.getLogger("alpha_hive.thesis_breaks")

_CONFIG_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thesis_breaks_config.json")


class ThesisBreakConfig:
    """针对不同标的的失效条件配置（延迟加载 JSON）"""

    _data: Optional[Dict] = None

    @classmethod
    def _load(cls) -> Dict:
        if cls._data is None:
            with open(_CONFIG_JSON_PATH, encoding="utf-8") as f:
                cls._data = json.load(f)
        return cls._data

    @classmethod
    def get_breaks_config(cls, ticker: str) -> Dict:
        """获取特定标的的失效条件"""
        return cls._load().get(ticker, {})

    @classmethod
    def get_coverage_info(cls) -> Dict:
        """返回失效条件覆盖统计"""
        data = cls._load()
        all_tickers = data.get("_all_tickers", [])
        covered = [t for t in all_tickers if cls.get_breaks_config(t)]
        return {
            "total": len(all_tickers),
            "covered": len(covered),
            "coverage_pct": round(len(covered) / len(all_tickers) * 100),
            "covered_tickers": covered,
            "missing_tickers": [t for t in all_tickers if t not in covered],
        }

    # 向后兼容: 允许测试重置缓存
    @classmethod
    def _reset_cache(cls):
        cls._data = None


class ThesisBreakMonitor:
    """实时监控失效条件"""

    def __init__(self, ticker: str, initial_score: float):
        self.ticker = ticker
        self.initial_score = initial_score
        self.config = ThesisBreakConfig.get_breaks_config(ticker)
        self.alerts = []
        self.adjusted_score = initial_score

    def check_all_conditions(self, metric_data: Dict) -> Dict:
        """检查所有失效条件"""

        result = {
            "ticker": self.ticker,
            "timestamp": datetime.now().isoformat(),
            "level_1_warnings": [],
            "level_2_stops": [],
            "score_adjustment": 0,
            "final_score": self.initial_score
        }

        # 检查 Level 1 预警
        if "level_1_warning" in self.config:
            for condition in self.config["level_1_warning"]["conditions"]:
                if self._check_condition(condition, metric_data):
                    result["level_1_warnings"].append({
                        "condition_id": condition["id"],
                        "metric": condition["metric"],
                        "trigger": condition["trigger"],
                        "current_value": metric_data.get(condition["id"]),
                        "severity": condition.get("severity", "MEDIUM"),
                        "timestamp": datetime.now().isoformat()
                    })
                    result["score_adjustment"] -= 0.15

        # 检查 Level 2 认输
        if "level_2_stop_loss" in self.config:
            for condition in self.config["level_2_stop_loss"]["conditions"]:
                if self._check_condition(condition, metric_data):
                    result["level_2_stops"].append({
                        "condition_id": condition["id"],
                        "metric": condition["metric"],
                        "trigger": condition["trigger"],
                        "current_value": metric_data.get(condition["id"]),
                        "action": condition.get("action"),
                        "timestamp": datetime.now().isoformat()
                    })
                    result["score_adjustment"] -= 0.30  # Level 2 更严重

        # 计算最终评分
        result["final_score"] = max(0, min(10, self.initial_score + result["score_adjustment"]))
        result["score_adjusted"] = result["final_score"] != self.initial_score

        return result

    def check_with_llm(
        self,
        original_thesis: Dict,
        recent_news: Optional[List[str]] = None,
        current_metrics: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """LLM 增强论文失效检测：分析新闻和指标变化，判断论文是否仍然有效

        与规则引擎互补——规则引擎检测已定义的硬性条件，LLM 检测未预见的叙事变化。
        仅在 LLM 可用时运行，不可用时静默降级。

        Returns:
            {thesis_intact, break_severity, break_reason, new_risk_factors,
             recommended_action} 或 None
        """
        try:
            import llm_service
            if not llm_service.is_available():
                return None
            result = llm_service.detect_thesis_breaks(
                self.ticker, original_thesis,
                recent_news or [], current_metrics or {},
            )
            if result:
                # LLM 发现 critical 级别失效 → 额外扣分
                severity = result.get("break_severity", "none")
                if severity == "critical":
                    self.adjusted_score = max(0, self.adjusted_score - 2.0)
                    _log.warning(
                        "ThesisBreak LLM: %s critical break — %s",
                        self.ticker, result.get("break_reason", ""),
                    )
                elif severity == "warning":
                    self.adjusted_score = max(0, self.adjusted_score - 0.5)
            return result
        except (ImportError, ConnectionError, TimeoutError, ValueError) as e:
            _log.debug("ThesisBreak LLM unavailable for %s: %s", self.ticker, e)
            return None

    def _check_condition(self, condition: Dict, metric_data: Dict) -> bool:
        """检查单个条件是否触发"""
        condition_id = condition["id"]

        # 模拟数据查询（实际应从数据源获取）
        if condition_id not in metric_data:
            return False

        current_value = metric_data[condition_id]
        trigger = condition["trigger"]

        # 简单的触发逻辑（实际应更复杂）
        if "%" in trigger and ">" in trigger:
            threshold = float(trigger.split(">")[1].strip().rstrip("%"))
            return current_value > threshold

        return False

    def generate_html_section(self) -> str:
        """生成 HTML 报告段落"""

        html = f"""
        <section id="thesis-breaks-{self.ticker}" class="report-section">
            <h2>🚨 失效条件监控 (Thesis Breaks) - {self.ticker}</h2>

            <!-- Level 1 预警 -->
            <div class="thesis-break-container level-1">
                <h3 class="level-label">⚠️ Level 1: 预警条件（降低评分 -15%）</h3>
                <div class="conditions-grid">
        """

        for condition in self.config["level_1_warning"]["conditions"]:
            html += f"""
                    <div class="break-condition">
                        <div class="break-metric">{condition['metric']}</div>
                        <div class="break-details">
                            <p><span class="label">触发条件：</span>{condition['trigger']}</p>
                            <p><span class="label">数据来源：</span>{condition['data_source']}</p>
                            <p><span class="label">当前状态：</span>{condition['current_status']}</p>
                            <p><span class="label">严重程度：</span>{condition.get('severity', 'MEDIUM')}</p>
                        </div>
                    </div>
            """

        html += """
                </div>
            </div>

            <!-- Level 2 认输 -->
            <div class="thesis-break-container level-2">
                <h3 class="level-label">🛑 Level 2: 认输条件（反转推荐）</h3>
                <div class="conditions-grid">
        """

        for condition in self.config["level_2_stop_loss"]["conditions"]:
            html += f"""
                    <div class="break-condition">
                        <div class="break-metric">{condition['metric']}</div>
                        <div class="break-details">
                            <p><span class="label">触发条件：</span>{condition['trigger']}</p>
                            <p><span class="label">数据来源：</span>{condition['data_source']}</p>
                            <p><span class="label">当前状态：</span>{condition['current_status']}</p>
                            <p><span class="label">后续行动：</span>{condition.get('action', 'N/A')}</p>
                        </div>
                    </div>
            """

        html += """
                </div>
            </div>

            <!-- 监控仪表板 -->
            <div class="monitoring-dashboard">
                <h3>📊 实时监控状态</h3>
                <table class="monitoring-table">
                    <thead>
                        <tr>
                            <th>条件</th>
                            <th>触发阈值</th>
                            <th>当前值</th>
                            <th>状态</th>
                        </tr>
                    </thead>
                    <tbody>
        """

        # 添加监控行（示例）
        conditions = self.config.get("level_1_warning", {}).get("conditions", [])
        for condition in conditions:
            html += f"""
                        <tr>
                            <td>{condition['metric']}</td>
                            <td>{condition['trigger']}</td>
                            <td>{condition['current_status']}</td>
                            <td>✅ 安全</td>
                        </tr>
            """

        html += """
                    </tbody>
                </table>
            </div>
        </section>

        <style>
            #thesis-breaks-{ticker} {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 8px;
                margin: 30px 0;
            }}

            .thesis-break-container {{
                margin: 20px 0;
                background: white;
                padding: 15px;
                border-radius: 6px;
            }}

            .level-1 {{
                border-left: 4px solid #ff9800;
            }}

            .level-2 {{
                border-left: 4px solid #f44336;
            }}

            .level-label {{
                margin-bottom: 15px;
                font-size: 16px;
                font-weight: 600;
            }}

            .conditions-grid {{
                display: grid;
                gap: 12px;
            }}

            .break-condition {{
                background: #fafafa;
                padding: 12px;
                border-radius: 4px;
                border-left: 3px solid #e0e0e0;
            }}

            .break-metric {{
                font-weight: 600;
                margin-bottom: 8px;
                color: #333;
            }}

            .break-details p {{
                margin: 4px 0;
                font-size: 13px;
            }}

            .label {{
                font-weight: 600;
                color: #666;
            }}

            .monitoring-table {{
                width: 100%;
                border-collapse: collapse;
                margin: 15px 0;
            }}

            .monitoring-table th {{
                background: #f5f5f5;
                padding: 10px;
                text-align: left;
                font-weight: 600;
                border-bottom: 2px solid #ddd;
            }}

            .monitoring-table td {{
                padding: 10px;
                border-bottom: 1px solid #eee;
            }}

            .monitoring-table tr:hover {{
                background: #f9f9f9;
            }}
        </style>
        """

        return html

    def save_to_json(self, filename: str = None) -> str:
        """保存监控配置到 JSON 文件"""

        if filename is None:
            filename = f"thesis_breaks_{self.ticker}.json"

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump({
                "ticker": self.ticker,
                "config": self.config,
                "created_at": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)

        return filename


# 使用示例
if __name__ == "__main__":
    # NVDA 监控
    nvda_monitor = ThesisBreakMonitor("NVDA", initial_score=8.52)

    # 模拟数据
    test_metrics = {
        "datacenter_revenue_decline": 2.5,  # 2.5% 增长（< 5% 下滑阈值，不触发）
        "competitor_threat": 0,
        "china_ban_risk": 35  # Polymarket 禁令概率 35%（< 60% 阈值，不触发）
    }

    result = nvda_monitor.check_all_conditions(test_metrics)
    print(f"✅ {result['ticker']} 检查完成")
    print(f"初始评分: {result['initial_score']}")
    print(f"最终评分: {result['final_score']}")
    print(f"警告数: {len(result['level_1_warnings'])}")
    print(f"认输数: {len(result['level_2_stops'])}")

    # 生成 HTML
    html = nvda_monitor.generate_html_section()
    print("\n✅ HTML 已生成")

    # 保存配置
    nvda_monitor.save_to_json()
    print("✅ 配置已保存")
