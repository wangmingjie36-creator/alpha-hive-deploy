#!/usr/bin/env python3
"""
🤖 Alpha Hive 代码执行 Agent - Phase 3 P1
能够自动生成和执行代码的 Agent
"""

import logging as _logging
import json
from typing import Dict, Any, Optional
from swarm_agents import BeeAgent
from pheromone_board import PheromoneBoard
from code_executor import CodeExecutor
from code_generator import CodeGenerator
from debugger import Debugger

# BUG FIX: 导入 CODE_EXECUTION_CONFIG，确保 CodeExecutor 使用正确的配置
try:
    from config import CODE_EXECUTION_CONFIG as _CE_CFG
except ImportError:
    _CE_CFG = {}

_log = _logging.getLogger("alpha_hive.code_executor_agent")

# CodeExecutorAgent 对应的蜂群维度标签
_DIMENSION = "technical"


class CodeExecutorAgent(BeeAgent):
    """能够执行代码的智能 Agent"""

    def __init__(
        self,
        board: PheromoneBoard,
        retriever=None,
        executor: Optional[CodeExecutor] = None
    ):
        """
        初始化代码执行 Agent

        Args:
            board: 信息素板
            retriever: 记忆检索器
            executor: 代码执行器
        """
        super().__init__(board, retriever)
        # BUG FIX: 优先使用外部传入的 executor；否则从 CODE_EXECUTION_CONFIG 读取参数，
        # 避免使用硬编码默认值（原来 enable_network=False 导致永久 ConnectionError）
        self.executor = executor or CodeExecutor(
            max_timeout=_CE_CFG.get("max_timeout", 30),
            sandbox_dir=_CE_CFG.get("sandbox_dir"),
            enable_network=_CE_CFG.get("enable_network", True),
            enable_file_write=_CE_CFG.get("enable_file_write", True),
        )
        self.debugger = Debugger()

    def analyze(self, ticker: str) -> Dict[str, Any]:
        """
        通过代码执行进行分析

        流程：
        1. 生成数据爬取脚本
        2. 执行获取数据
        3. 进行分析
        4. 发布结果

        Args:
            ticker: 股票代码

        Returns:
            分析结果字典
        """
        try:
            # 1. 生成数据爬取脚本
            discovery = f"🔧 正在为 {ticker} 执行代码分析"
            self._publish(ticker, discovery, "code_executor", 5.0, "neutral")

            # 获取 yfinance 数据
            code = CodeGenerator.generate_data_fetch(
                "yfinance",
                {"ticker": ticker, "period": "1mo"}
            )

            # 2. 执行数据爬取
            fetch_result = self.executor.execute_python(code)

            if not fetch_result["success"]:
                # 尝试自动修复
                auto_retry_result = self.debugger.auto_retry(code, self.executor)

                if not auto_retry_result["success"]:
                    error = self.debugger.parse_error(fetch_result["stderr"])
                    discovery = f"❌ 数据爬取失败: {error['error_type']}"
                    self._publish(ticker, discovery, "code_executor", 2.0, "neutral")

                    return {
                        "score": 2.0,
                        "direction": "neutral",
                        "discovery": discovery,
                        "source": "CodeExecutorAgent",
                        "dimension": _DIMENSION,   # BUG FIX: 缺失导致显示 "unknown"
                        "error": error
                    }

                fetch_result = auto_retry_result["result"]

            # 3. 解析爬取结果
            try:
                data = json.loads(fetch_result["stdout"])
            except json.JSONDecodeError:
                discovery = f"❌ 数据解析失败"
                self._publish(ticker, discovery, "code_executor", 2.0, "neutral")

                return {
                    "score": 2.0,
                    "direction": "neutral",
                    "discovery": discovery,
                    "source": "CodeExecutorAgent",
                    "dimension": _DIMENSION,   # BUG FIX
                    "raw_output": fetch_result["stdout"]
                }

            # 4. 生成技术分析脚本
            analysis_code = CodeGenerator.generate_analysis(
                "technical",
                {"ticker": ticker, "period": "1mo"}
            )

            analysis_result = self.executor.execute_python(analysis_code)

            if analysis_result["success"]:
                try:
                    analysis_data = json.loads(analysis_result["stdout"])

                    # 5. 生成发现和评分
                    price = data.get("current_price", 0)
                    sma_20 = analysis_data.get("sma_20", 0)
                    signal = analysis_data.get("signal", "中性")

                    # 评分逻辑
                    if signal == "超买":
                        score = 3.0  # 看空
                        direction = "bearish"
                        discovery = f"📊 技术指标超买 (RSI > 70)，价格 ${price:.2f}"
                    elif signal == "超卖":
                        score = 7.0  # 看多
                        direction = "bullish"
                        discovery = f"📊 技术指标超卖 (RSI < 30)，价格 ${price:.2f}"
                    else:
                        if sma_20 and price > sma_20:
                            score = 6.5
                            direction = "bullish"
                            discovery = f"📊 价格高于 20 日均线，价格 ${price:.2f}"
                        else:
                            score = 4.5
                            direction = "bearish"
                            discovery = f"📊 价格低于 20 日均线，价格 ${price:.2f}"

                    # 发布发现
                    self._publish(ticker, discovery, "code_executor_analysis", score, direction)

                    return {
                        "score": score,
                        "direction": direction,
                        "discovery": discovery,
                        "source": "CodeExecutorAgent",
                        "dimension": _DIMENSION,   # BUG FIX
                        "details": {
                            "price": price,
                            "sma_20": sma_20,
                            "rsi_signal": signal,
                            "fetch_data": data,
                            "analysis_data": analysis_data
                        }
                    }

                except json.JSONDecodeError as _jde:
                    _log.warning("代码执行分析结果解析失败: %s", _jde)

            # 6. 如果分析失败，返回原始数据结果
            price = data.get("current_price")
            market_cap = data.get("market_cap")

            if price and market_cap:
                score = 6.0
                direction = "bullish"
                discovery = f"📊 价格数据可用: ${price:.2f}，市值: ${market_cap:,.0f}"
            else:
                score = 5.0
                direction = "neutral"
                discovery = f"📊 获取到部分市场数据"

            self._publish(ticker, discovery, "code_executor_data", score, direction)

            return {
                "score": score,
                "direction": direction,
                "discovery": discovery,
                "source": "CodeExecutorAgent",
                "dimension": _DIMENSION,   # BUG FIX
                "details": data
            }

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.error("CodeExecutorAgent.analyze 异常: %s", e, exc_info=True)
            discovery = f"❌ 执行异常: {str(e)[:50]}"
            self._publish(ticker, discovery, "code_executor", 1.0, "neutral")

            return {
                "error": str(e),
                "source": "CodeExecutorAgent",
                "score": 1.0,
                "dimension": _DIMENSION,   # BUG FIX
                "direction": "neutral",
                "discovery": discovery,
            }

    def generate_data_fetch_code(self, source: str, params: Dict) -> str:
        """
        生成数据爬取代码

        Args:
            source: 数据源
            params: 参数

        Returns:
            Python 代码
        """
        return CodeGenerator.generate_data_fetch(source, params)

    def generate_analysis_code(self, analysis_type: str, params: Dict) -> str:
        """生成分析代码"""
        return CodeGenerator.generate_analysis(analysis_type, params)

    def generate_visualization_code(self, chart_type: str, params: Dict) -> str:
        """生成可视化代码"""
        return CodeGenerator.generate_visualization(chart_type, params)

    def execute_and_analyze(self, code: str, ticker: str = "UNKNOWN") -> Dict[str, Any]:
        """
        执行代码并分析结果

        Args:
            code: Python 代码
            ticker: 关联的股票代码

        Returns:
            {
                "success": bool,
                "result": Dict,
                "analysis": str,
                "discovery": str
            }
        """
        # 代码验证
        is_valid, warnings = self.debugger.validate_code(code)

        if not is_valid:
            return {
                "success": False,
                "error": warnings[0],
                "discovery": f"❌ 代码验证失败: {warnings[0]}"
            }

        # 执行代码
        result = self.executor.execute_python(code)

        if result["success"]:
            discovery = f"✅ 代码执行成功（{result['execution_time']:.2f}s）"
            self._publish(ticker, discovery, "code_executor_success", 8.0, "bullish")

            return {
                "success": True,
                "result": result,
                "analysis": "代码执行成功",
                "discovery": discovery
            }
        else:
            # 自动修复
            retry_result = self.debugger.auto_retry(code, self.executor, max_attempts=2)

            if retry_result["success"]:
                discovery = f"✅ 修复后执行成功（{retry_result['attempts']} 次尝试）"
                self._publish(ticker, discovery, "code_executor_fixed", 7.0, "bullish")

                return {
                    "success": True,
                    "result": retry_result["result"],
                    "analysis": f"经过 {retry_result['attempts']} 次修复后成功",
                    "discovery": discovery,
                    "modifications": retry_result["modifications"]
                }
            else:
                # 分析错误
                error = self.debugger.parse_error(result["stderr"])
                discovery = f"❌ 执行失败: {error['error_type']} - {error['suggestion']}"
                self._publish(ticker, discovery, "code_executor_error", 2.0, "bearish")

                return {
                    "success": False,
                    "error": error,
                    "analysis": error["suggestion"],
                    "discovery": discovery,
                    "attempts": retry_result["attempts"]
                }

    def auto_debug(self, code: str) -> Dict[str, Any]:
        """
        自动调试代码

        Args:
            code: Python 代码

        Returns:
            调试结果
        """
        # 代码验证
        is_valid, warnings = self.debugger.validate_code(code)

        if warnings:
            _log.warning("代码警告: %s", "; ".join(warnings))

        # 执行代码
        result = self.executor.execute_python(code)

        if result["success"]:
            return {
                "success": True,
                "message": "代码执行成功，无错误",
                "result": result
            }

        # 解析错误
        error = self.debugger.parse_error(result["stderr"])

        # 生成修复建议
        suggested_code = self.debugger.suggest_fix(error, code)

        return {
            "success": False,
            "error": error,
            "suggested_fix": suggested_code,
            "warnings": warnings,
            "original_result": result
        }
