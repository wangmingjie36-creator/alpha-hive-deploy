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

            # v0.41.5: 生成代码里的 yfinance 抓价与 Scout/Oracle 走的 CBOE
            # 快照价各查各的，曾导致同一次扫描出现两个不同现价。用共享快照价
            # 覆盖沙盒脚本抓到的 current_price，SMA/RSI 等技术指标不受影响
            # （仍用沙盒脚本自己拉的历史K线计算）。
            _snapshot_price = self._get_stock_data(ticker).get("price")
            if _snapshot_price:
                data["current_price"] = round(float(_snapshot_price), 2)

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

            # 6. 技术分析不可用 → 只报「取到了什么数据」，**不报方向**
            #
            # ⚠️ v0.45.191：这条兜底原本是
            #     if price and market_cap: score = 6.0; direction = "bullish"
            # 「价格与市值都拿到了」⇒「看多」是**范畴错误** —— 数据可用性不含
            # 任何方向信息。`agent_memory` 台账实测（2026-04-06~09-10）：
            # `code_executor_data` 共 870 条，**870 条（100%）是 `6.0/bullish`**，
            # 下面那条 else 五个月一次没走过 ⇒ 它是一张恒定的看多票，不是观测。
            #
            # v0.43.10（2026-08-12）修的是**上游**（技术分析脚本撞 yfinance
            # MultiIndex 崩溃，使本兜底被 100% 走到），台账确认那次修复属实
            # （恒定占比 100% → 3.1%）；**兜底本身的错原样留到了这里**。
            #
            # 现在：拿没拿到数据只决定**说辞**，方向一律 neutral、分数落量表中性点。
            price = data.get("current_price")
            market_cap = data.get("market_cap")

            if price and market_cap:
                discovery = (f"📊 技术分析不可用，仅取到价格 ${price:.2f}"
                             f"、市值 ${market_cap:,.0f}")
            else:
                discovery = "📊 技术分析不可用，仅取到部分市场数据"
            score = 5.0
            direction = "neutral"

            self._publish(ticker, discovery, "code_executor_data", score, direction)

            return {
                "score": score,
                "direction": direction,
                "discovery": discovery,
                "source": "CodeExecutorAgent",
                "dimension": _DIMENSION,   # BUG FIX
                # 机读标记：没有它，「兜底的中性」与「真的中性」完全同形 ——
                # 即 v0.45.151「缺失哨兵选中众数」的形状。
                # 值必须落在 `QueenDistiller.PROXY_SOURCES`（"fallback" 在内），
                # 否则 DQ 汇总按 0 质量计，等于把「降级」悄悄升格成「数据全废」。
                "data_quality": {"technical": "fallback"},
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

    # ── v0.45.191：此处删掉五个零调用点的方法 ───────────────────────────
    # `generate_data_fetch_code` / `generate_analysis_code` /
    # `generate_visualization_code` / `execute_and_analyze` / `auto_debug`。
    #
    # 判据两条独立、各带正对照：
    # ① 静态：AST 全仓零调用点（同类 `analyze` 有 17 个生产调用点，证明扫描器
    #    有效）；字符串引用 / 仓库外调用者 / 动态派发三个盲区均已查空。
    # ② 运行时：`pheromone.db::agent_memory` 台账（2026-04-06~09-10，18,120 行）
    #    里 `execute_and_analyze` 独有的三个 source —— `code_executor_success` /
    #    `_fixed` / `_error` —— **各 0 行**；而同表同 agent 的 `analyze` 三个
    #    source 共 3,884 行，证明台账记得住这只蜂。五个月零执行。
    #
    # 顺带记：它们内含 `成功→8.0/bullish`、`修好→7.0/bullish`、
    # `报错→2.0/bearish` —— 与上面那条兜底是**同一个范畴错误**（「跑通了 ⇒ 看多」）。
    # 先修兜底、后删它们，就是为了不把线索一起丢掉。
    # 设计存档见 `PHASE3_P1_CODE_EXECUTION_PLAN.md`。
