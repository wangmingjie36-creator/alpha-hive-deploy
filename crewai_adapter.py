#!/usr/bin/env python3
"""
🤖 Alpha Hive CrewAI 多 Agent 框架适配层 - Phase 3 P5
将自研 BeeAgent 包装为 CrewAI Agent，通过 Process.hierarchical 实现主-子递归调度
与现有 PheromoneBoard 深度集成
"""

import json
import logging as _logging
import time
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

_log = _logging.getLogger("alpha_hive.crewai_adapter")

CREWAI_AVAILABLE = False

# 尝试导入 CrewAI（可能因版本不兼容而失败）
try:
    from crewai import Agent, Task, Crew, Process
    from crewai.tools import BaseTool
    CREWAI_AVAILABLE = True
except (ImportError, TypeError) as e:
    CREWAI_AVAILABLE = False
    print(f"⚠️ CrewAI 导入失败 (可能因 Python 版本不兼容): {type(e).__name__}")
    # 定义虚拟基类，使得代码不会因为缺少依赖而崩溃
    class BaseTool:
        pass

from pheromone_board import PheromoneBoard

# 每个 Agent 的精准功能描述（替代泛泛的"分析投资机会"）
TOOL_DESCRIPTIONS = {
    "ScoutBeeNova": "SEC Form4/13F机构持仓分析+拥挤度评估",
    "OracleBeeEcho": "期权IV Rank+Put/Call Ratio+隐含概率",
    "BuzzBeeWhisper": "X/社交媒体情绪量化+叙事主题提取",
    "ChronosBeeHorizon": "催化剂事件日历+时间线追踪",
    "RivalBeeVanguard": "竞争格局对标+ML预测信号",
    "GuardBeeSentinel": "多源交叉验证+共振检测+风险评估",
}


@dataclass
class CrewAIToolResult:
    """CrewAI Tool 执行结果"""
    success: bool
    data: Dict[str, Any]
    error: Optional[str] = None


class BeeAgentTool(BaseTool):
    """
    将 BeeAgent.analyze() 包装为 CrewAI Tool
    允许 CrewAI ManagerAgent 调用独立的蜂群 Agent
    """

    name: str = "BeeAgent"
    description: str = "Alpha Hive BeeAgent 分析工具"
    bee_agent: Any = None

    def _run(self, ticker: str) -> str:
        """
        执行蜂群 Agent 分析

        Args:
            ticker: 股票代码（格式化字符串）

        Returns:
            JSON 格式的分析结果
        """
        if not self.bee_agent:
            return json.dumps({
                "success": False,
                "error": "BeeAgent 未绑定"
            }, ensure_ascii=False)

        try:
            result = self.bee_agent.analyze(ticker)

            # 如果结果是字符串（JSON），尝试解析；否则转换为 JSON
            if isinstance(result, str):
                try:
                    data = json.loads(result)
                except json.JSONDecodeError:
                    data = {"raw": result}
            else:
                data = result

            return json.dumps({
                "success": True,
                "ticker": ticker,
                "agent_name": self.bee_agent.__class__.__name__,
                "data": data
            }, ensure_ascii=False, default=str)

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.error("BeeAgentTool._run failed for %s: %s", ticker, e, exc_info=True)
            return json.dumps({
                "success": False,
                "ticker": ticker,
                "agent_name": self.bee_agent.__class__.__name__,
                "error": str(e)
            }, ensure_ascii=False)


class AlphaHiveCrew:
    """
    Alpha Hive CrewAI 编排系统

    使用 Process.hierarchical 实现：
    - ManagerAgent（QueenDistiller 角色）协调所有分析 Agent
    - 每个 BeeAgent 对应一个 Tool
    - 信息素板（PheromoneBoard）作为共享状态
    """

    def __init__(self, board: Optional[PheromoneBoard] = None, memory_store=None):
        """
        初始化 CrewAI 编排系统

        Args:
            board: PheromoneBoard 实例（共享信息素）
            memory_store: 持久化记忆存储
        """
        self.board = board or PheromoneBoard(memory_store=memory_store)
        self.memory_store = memory_store
        self.agents_list = []  # 原始 BeeAgent 列表
        self.crew = None
        self.tools = []

    def build(self, tickers: List[str]) -> "AlphaHiveCrew":
        """
        构建 CrewAI Crew 架构

        需要先初始化蜂群 Agent，然后创建 CrewAI 框架

        Args:
            tickers: 要分析的股票代码列表

        Returns:
            self（支持链式调用）
        """
        if not CREWAI_AVAILABLE:
            raise RuntimeError("CrewAI 未安装，请运行: pip install crewai crewai-tools")

        from swarm_agents import (
            ScoutBeeNova, OracleBeeEcho, BuzzBeeWhisper,
            ChronosBeeHorizon, RivalBeeVanguard, GuardBeeSentinel
        )

        # 实例化 6 个 BeeAgent（共享信息素板）
        bee_agents = [
            ScoutBeeNova(self.board),
            OracleBeeEcho(self.board),
            BuzzBeeWhisper(self.board),
            ChronosBeeHorizon(self.board),
            RivalBeeVanguard(self.board),
            GuardBeeSentinel(self.board),
        ]
        self.agents_list = bee_agents

        # 将每个 BeeAgent 包装为 CrewAI Tool（精准描述提升 LLM 调用准确性）
        self.tools = []
        for agent in bee_agents:
            agent_name = agent.__class__.__name__
            tool = BeeAgentTool(
                name=agent_name,
                description=TOOL_DESCRIPTIONS.get(agent_name, "投资分析工具"),
                bee_agent=agent
            )
            self.tools.append(tool)

        # ManagerAgent（精简 prompt，减少 ~47% token）
        manager = Agent(
            role="投资分析总监",
            goal="调用全部6个工具分析{ticker}，输出JSON：score(0-10)、direction、discovery、risks",
            backstory="综合多源信号（SEC披露、期权、情绪、催化剂、竞争格局、风险），加权评分，保留少数意见。",
            tools=self.tools,
            allow_delegation=True,
            verbose=True,
        )

        # 分析任务（压缩 description，强制 JSON schema）
        task = Task(
            description="分析{ticker}投资机会。调用所有工具，综合评分。",
            agent=manager,
            expected_output='{"score":0.0,"direction":"bullish|bearish|neutral","discovery":"一句话摘要","reasoning":"推理过程","signals":{},"risks":[]}',
        )

        # 构建 Crew（使用 hierarchical 过程）
        self.crew = Crew(
            agents=[manager],  # 仅包含 ManagerAgent，其他 Agent 通过 Tool 调用
            tasks=[task],
            process=Process.hierarchical,
            manager_agent=manager,
            verbose=True,
        )

        return self

    def analyze(self, ticker: str) -> Dict:
        """
        运行 CrewAI 分析单个标的

        Args:
            ticker: 股票代码

        Returns:
            标准化的分析结果字典
        """
        if not self.crew:
            raise RuntimeError("先调用 build() 构建 Crew")

        try:
            # 运行 CrewAI workflow
            result = self.crew.kickoff(inputs={"ticker": ticker})

            # 将 CrewAI 结果转换为蜂群标准格式
            return self._normalize_result(ticker, result)

        except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError) as e:
            _log.error("CrewAI analysis failed for %s: %s", ticker, e, exc_info=True)
            return {
                "ticker": ticker,
                "final_score": 0.0,
                "direction": "neutral",
                "discovery": f"分析失败: {str(e)[:100]}",
                "error": str(e),
                "mode": "crewai"
            }

    def _normalize_result(self, ticker: str, crew_result) -> Dict:
        """
        将 CrewAI 输出转换为与 QueenDistiller.distill() 相同的格式

        Args:
            ticker: 股票代码
            crew_result: CrewAI Crew.kickoff() 的结果

        Returns:
            标准化的结果字典
        """
        try:
            # CrewAI 返回一个对象，raw 属性包含最终输出
            output_text = str(crew_result.raw) if hasattr(crew_result, 'raw') else str(crew_result)

            # 尝试解析为 JSON
            try:
                data = json.loads(output_text)
            except json.JSONDecodeError:
                # 如果不是 JSON，包装为字典
                data = {"raw_output": output_text[:500]}

            # 提取关键字段，使用默认值
            final_score = float(data.get("score", data.get("final_score", 5.0)))
            direction = data.get("direction", "neutral").lower()

            # 确保分数在 0-10 范围内
            final_score = max(0.0, min(10.0, final_score))

            return {
                "ticker": ticker,
                "final_score": final_score,
                "direction": direction,
                "discovery": data.get("discovery", "")[:500],
                "reasoning": data.get("reasoning", "")[:500],
                "signals": data.get("signals", {}),
                "risks": data.get("risks", []),
                "resonance": {"resonance_detected": False, "boost": 0},
                "agent_breakdown": data,
                "mode": "crewai"
            }

        except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as e:
            _log.error("Result normalization failed: %s", e, exc_info=True)
            return {
                "ticker": ticker,
                "final_score": 5.0,
                "direction": "neutral",
                "discovery": str(crew_result)[:200] if crew_result else "",
                "mode": "crewai",
                "error": str(e)
            }

    def get_pheromone_board(self) -> PheromoneBoard:
        """获取共享的信息素板"""
        return self.board

    def get_agents_count(self) -> int:
        """获取蜂群 Agent 数量"""
        return len(self.agents_list)


def test_crewai_adapter():
    """
    简单的测试函数，验证 CrewAI 适配层是否就绪
    """
    print("\n🧪 CrewAI 适配层测试\n")

    if not CREWAI_AVAILABLE:
        print("❌ CrewAI 未安装，跳过测试")
        print("📝 安装命令: pip install crewai crewai-tools --user")
        return False

    # 先检查 API Key
    try:
        # 创建 PheromoneBoard
        board = PheromoneBoard()
        print("✅ PheromoneBoard 创建成功")

        # 创建 AlphaHiveCrew
        crew = AlphaHiveCrew(board=board)
        print("✅ AlphaHiveCrew 创建成功")

        # 构建 Crew
        crew.build(["NVDA"])
        print(f"✅ Crew 构建成功（{crew.get_agents_count()} 个蜂群 Agent）")

        # 验证 tools
        print(f"✅ CrewAI Tools: {len(crew.tools)} 个")
        for tool in crew.tools:
            print(f"   - {tool.name}: {tool.description[:50]}")

        return True

    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError, ImportError) as e:
        _log.error("CrewAI adapter test failed: %s", e, exc_info=True)
        return False


if __name__ == "__main__":
    success = test_crewai_adapter()
    print(f"\n{'✅ 所有测试通过' if success else '❌ 测试失败'}\n")
