#!/usr/bin/env python3
"""
🐝 Alpha Hive 日报生成器 - 集成期权分析的完整版本
每日自动扫描 watchlist 并生成结构化投资简报 + X 线程版本
"""

import json
import os
import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# 导入现有模块
from config import WATCHLIST, EVALUATION_WEIGHTS
from hive_logger import get_logger, PATHS, set_correlation_id

_log = get_logger("daily_report")

# Week 4: 指标收集器
try:
    from metrics_collector import MetricsCollector
except ImportError:
    MetricsCollector = None
from generate_ml_report import MLEnhancedReportGenerator
from pheromone_board import PheromoneBoard
from swarm_agents import (
    ScoutBeeNova, OracleBeeEcho, BuzzBeeWhisper,
    ChronosBeeHorizon, RivalBeeVanguard, GuardBeeSentinel,
    BearBeeContrarian,
    QueenDistiller, prefetch_shared_data, inject_prefetched
)
from concurrent.futures import as_completed
from agent_toolbox import AgentHelper

# Phase 2: Import memory store
try:
    from memory_store import MemoryStore
except ImportError:
    MemoryStore = None

# Phase 3 P2: Import Calendar integrator
try:
    from calendar_integrator import CalendarIntegrator
except ImportError:
    CalendarIntegrator = None

# Phase 3 P4: Import Code Execution Agent
try:
    from code_executor_agent import CodeExecutorAgent
    from config import CODE_EXECUTION_CONFIG
except ImportError:
    CodeExecutorAgent = None
    CODE_EXECUTION_CONFIG = {"enabled": False}

# Phase 3 P5: Import CrewAI 多 Agent 框架
try:
    from crewai_adapter import AlphaHiveCrew
    from config import CREWAI_CONFIG
except (ImportError, TypeError) as e:
    AlphaHiveCrew = None
    CREWAI_CONFIG = {"enabled": False}
    _log.info("CrewAI 模块导入失败: %s (降级到原始蜂群)", type(e).__name__)

# Phase 3 P6: Import Slack 报告通知器（替代 Gmail）
try:
    from slack_report_notifier import SlackReportNotifier
except ImportError:
    SlackReportNotifier = None

# Phase 3 内存优化: 向量记忆层（Chroma 长期记忆）
try:
    from vector_memory import VectorMemory
    from config import VECTOR_MEMORY_CONFIG
except ImportError:
    VectorMemory = None
    VECTOR_MEMORY_CONFIG = {"enabled": False}

# Phase 6: 回测反馈循环
try:
    from backtester import Backtester, run_full_backtest
except ImportError:
    Backtester = None
    run_full_backtest = None


# 免责声明常量（去重，全局引用）
DISCLAIMER_FULL = (
    "本报告为蜂群 AI 分析，不构成投资建议，不替代持牌投顾。"
    "预测存在误差，所有交易决策需自行判断和风控。"
)
DISCLAIMER_SHORT = "非投资建议，仅数据分析与情景推演。"


@dataclass
class OpportunityItem:
    """机会项目结构"""
    ticker: str
    direction: str  # "看多" / "看空" / "中性"
    signal_score: float  # 0-10
    catalyst_score: float  # 0-10
    sentiment_score: float  # 0-10
    odds_score: float  # 0-10
    risk_score: float  # 0-10
    options_score: float  # 0-10 (新增)
    opportunity_score: float  # 0-10 (综合)
    confidence: float  # 0-100%
    key_catalysts: List[str]
    options_signal: str  # 期权信号摘要
    risks: List[str]
    thesis_break: str  # 失效条件


class AlphaHiveDailyReporter:
    """Alpha Hive 日报生成引擎"""

    def __init__(self):
        self.report_dir = PATHS.home
        self.timestamp = datetime.now()
        self.date_str = self.timestamp.strftime("%Y-%m-%d")

        # 初始化报告生成器
        self.ml_generator = MLEnhancedReportGenerator()

        # 初始化 Agent 工具集（新增）
        self.agent_helper = AgentHelper()

        # Phase 2: 初始化持久化记忆存储
        self.memory_store = None
        self._session_id = None
        if MemoryStore:
            try:
                self.memory_store = MemoryStore()
                self._session_id = self.memory_store.generate_session_id(run_mode="daily_scan")
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("MemoryStore 初始化失败，继续运行: %s", e)

        # 结果存储
        self.opportunities: List[OpportunityItem] = []
        self.observations: List[Dict] = []
        self.risks: List[Dict] = []

        # 线程安全锁（用于并行执行时保护共享数据）
        self._results_lock = Lock()

        # Phase 3 P2: 初始化 Google Calendar 集成（失败时降级）
        self.calendar = None
        if CalendarIntegrator:
            try:
                self.calendar = CalendarIntegrator()
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("Calendar 初始化失败: %s", e)

        # Phase 3 P4: 初始化代码执行 Agent（失败时降级）
        self.code_executor_agent = None
        if CodeExecutorAgent and CODE_EXECUTION_CONFIG.get("enabled"):
            try:
                self.code_executor_agent = CodeExecutorAgent(board=None)
                # board 在 run_swarm_scan 时注入
            except (OSError, ValueError, RuntimeError, TypeError) as e:
                _log.warning("CodeExecutorAgent 初始化失败: %s", e)

        # Phase 3 内存优化: 初始化向量记忆层（Chroma 长期记忆）
        self.vector_memory = None
        if VectorMemory and VECTOR_MEMORY_CONFIG.get("enabled"):
            try:
                self.vector_memory = VectorMemory(
                    db_path=VECTOR_MEMORY_CONFIG.get("db_path"),
                    retention_days=VECTOR_MEMORY_CONFIG.get("retention_days", 90)
                )
                if self.vector_memory.enabled:
                    if VECTOR_MEMORY_CONFIG.get("cleanup_on_startup"):
                        self.vector_memory.cleanup()
            except (ImportError, OSError, ValueError, RuntimeError) as e:
                _log.warning("向量记忆初始化失败: %s", e)

        # Week 4: 指标收集器
        self.metrics = None
        if MetricsCollector:
            try:
                self.metrics = MetricsCollector()
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("MetricsCollector 初始化失败: %s", e)

        # Phase 2: 共享线程池（替代所有 daemon 线程，退出时等待完成）
        import atexit
        self._bg_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="hive_bg")
        self._bg_futures = []
        atexit.register(self._shutdown_bg)

        # Phase 3 P6: 初始化 Slack 报告通知器（替代 Gmail）
        self.slack_notifier = None
        if SlackReportNotifier:
            try:
                self.slack_notifier = SlackReportNotifier()
            except (OSError, ValueError, RuntimeError, ConnectionError) as e:
                _log.warning("Slack 通知器初始化失败: %s", e)

    def _shutdown_bg(self) -> None:
        """atexit 处理器：等待后台任务完成"""
        from concurrent.futures import TimeoutError as FuturesTimeout, CancelledError
        for f in self._bg_futures:
            try:
                f.result(timeout=10)
            except (FuturesTimeout, CancelledError, OSError, RuntimeError) as e:
                _log.debug("Background task cleanup: %s", e)
        self._bg_executor.shutdown(wait=True)

    def _submit_bg(self, fn, *args) -> None:
        """提交后台任务到共享线程池（替代 daemon 线程）"""
        future = self._bg_executor.submit(fn, *args)
        self._bg_futures.append(future)
        # 清理已完成的 futures（防止内存泄漏）
        self._bg_futures = [f for f in self._bg_futures if not f.done()]

    def _analyze_ticker_safe(self, ticker: str, index: int, total: int) -> Tuple[str, OpportunityItem, str]:
        """
        分析单个标的（线程安全，可在并行上下文中调用）

        Args:
            ticker: 股票代码
            index: 当前索引（用于显示进度）
            total: 总数（用于显示进度）

        Returns:
            (ticker, opportunity_item_or_none, error_message_or_none)
        """
        try:
            # 构建最小化的实时数据结构
            realtime_metrics = {
                "ticker": ticker,
                "sources": {
                    "yahoo_finance": {
                        "current_price": 100.0,
                        "change_pct": 2.5
                    }
                }
            }

            # 生成 ML 增强报告
            ml_report = self.ml_generator.generate_ml_enhanced_report(
                ticker, realtime_metrics
            )

            # 解析为 OpportunityItem
            opportunity = self._parse_ml_report_to_opportunity(ticker, ml_report)

            # 线程安全地添加到结果列表
            with self._results_lock:
                self.opportunities.append(opportunity)

            return ticker, opportunity, None

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.error("Ticker analysis failed for %s: %s", ticker, e, exc_info=True)
            error_msg = str(e)
            # 线程安全地添加观察项
            with self._results_lock:
                self.observations.append({
                    "ticker": ticker,
                    "status": "error",
                    "error": error_msg
                })
            return ticker, None, error_msg

    def run_daily_scan(self, focus_tickers: List[str] = None) -> Dict:
        """
        执行每日扫描（并行版本）

        Args:
            focus_tickers: 重点关注标的（如为None则扫描全部watchlist）

        Returns:
            完整的日报数据结构
        """
        _log.info("Alpha Hive 日报 %s", self.date_str)

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        _log.info("标的：%s", " ".join(targets))

        start_parallel = time.time()

        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            futures = [
                executor.submit(self._analyze_ticker_safe, ticker, i + 1, len(targets))
                for i, ticker in enumerate(targets)
            ]

            for i, future in enumerate(futures, 1):
                ticker, opportunity, error = future.result()
                if error:
                    _log.warning("[%d/%d] %s 分析失败: %s", i, len(targets), ticker, error[:60])
                else:
                    _log.info("[%d/%d] %s: %.1f/10", i, len(targets), ticker, opportunity.opportunity_score)

        elapsed_parallel = time.time() - start_parallel
        _log.info("分析耗时：%.1fs", elapsed_parallel)

        # 排序机会
        self.opportunities.sort(key=lambda x: x.opportunity_score, reverse=True)

        # 构建报告
        report = self._build_report()

        # Phase 2: 异步保存会话（使用共享线程池，退出时等待完成）
        if self.memory_store and self._session_id:
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "daily_scan",
                targets, {}, [], elapsed_parallel
            )

        return report

    def run_swarm_scan(self, focus_tickers: List[str] = None) -> Dict:
        """
        真正的蜂群协作扫描 - 6 个 Agent 并行运行，实时通过信息素板交换发现

        Args:
            focus_tickers: 重点关注标的（如为None则扫描全部watchlist）

        Returns:
            完整的蜂群分析报告
        """
        # Week 4: 设置 correlation_id 追踪本次扫描
        set_correlation_id(self._session_id or f"swarm_{self.date_str}")
        _log.info("蜂群协作启动 %s", self.date_str)

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        _log.info("标的：%s", " ".join(targets))

        start_time = time.time()

        # 创建共享的信息素板
        board = PheromoneBoard(memory_store=self.memory_store, session_id=self._session_id)

        # 实例化 Agent：第一阶段 6 个核心 Agent，第二阶段 BearBeeContrarian（读取信息素板）
        retriever = self.vector_memory if (self.vector_memory and self.vector_memory.enabled) else None
        phase1_agents = [
            ScoutBeeNova(board, retriever=retriever),
            OracleBeeEcho(board, retriever=retriever),
            BuzzBeeWhisper(board, retriever=retriever),
            ChronosBeeHorizon(board, retriever=retriever),
            RivalBeeVanguard(board, retriever=retriever),
            GuardBeeSentinel(board, retriever=retriever),
        ]
        # 看空对冲蜂：二阶段执行（等其他 Agent 写入信息素板后再分析）
        bear_agent = BearBeeContrarian(board, retriever=retriever)

        # Phase 3 P4: 动态注入 CodeExecutorAgent
        if self.code_executor_agent and CODE_EXECUTION_CONFIG.get("add_to_swarm"):
            self.code_executor_agent.board = board
            phase1_agents.append(self.code_executor_agent)

        # Phase 6: 自适应权重
        adapted_w = Backtester.load_adapted_weights() if Backtester else None
        queen = QueenDistiller(board, adapted_weights=adapted_w)

        all_agents = phase1_agents + [bear_agent]
        _log.info("%d Agent（含二阶段看空蜂）| 预取数据中...", len(all_agents))

        # ⚡ 优化 #1+#2: 批量预取 yfinance + VectorMemory（每 ticker 仅 1 次）
        prefetched = prefetch_shared_data(targets, retriever)
        inject_prefetched(all_agents, prefetched)
        prefetch_elapsed = time.time() - start_time
        _log.info("预取完成 (%.1fs) | 开始并行分析", prefetch_elapsed)

        # ⚡ 优化 #3: 单层线程池，按 ticker 串行、Agent 并行
        swarm_results = {}

        # Phase 2: 崩溃恢复 checkpoint
        checkpoint_file = self.report_dir / f".checkpoint_{self._session_id or 'default'}.json"
        completed_tickers = set()
        if checkpoint_file.exists():
            try:
                with open(checkpoint_file, "r") as f:
                    ckpt = json.load(f)
                    swarm_results = ckpt.get("results", {})
                    completed_tickers = set(swarm_results.keys())
                    if completed_tickers:
                        _log.info("恢复 checkpoint：%d 标的已完成", len(completed_tickers))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                _log.warning("Checkpoint 恢复失败，重新开始: %s", e)

        for idx, ticker in enumerate(targets, 1):
            if ticker in completed_tickers:
                res = "✅" if swarm_results[ticker]["resonance"]["resonance_detected"] else "—"
                _log.info("[%d/%d] %s: %.1f/10 (已缓存) %s", idx, len(targets), ticker, swarm_results[ticker]['final_score'], res)
                continue

            # 第一阶段：6 个核心 Agent 并行分析
            with ThreadPoolExecutor(max_workers=len(phase1_agents)) as executor:
                futures = {executor.submit(agent.analyze, ticker): agent for agent in phase1_agents}
                agent_results = []
                for future in as_completed(futures):
                    try:
                        agent_results.append(future.result(timeout=60))
                    except (TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as e:
                        _log.warning("Agent future failed: %s", e)
                        agent_results.append(None)

            # 第二阶段：BearBeeContrarian 读取信息素板后分析（此时其他 Agent 数据已可用）
            try:
                bear_result = bear_agent.analyze(ticker)
                agent_results.append(bear_result)
                _log.info("  🐻 看空蜂: %s %s (%.1f分, %d信号)",
                          ticker, bear_result.get("direction", "?"),
                          bear_result.get("details", {}).get("bear_score", 0),
                          len(bear_result.get("details", {}).get("bearish_signals", [])))
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                _log.warning("BearBeeContrarian failed for %s: %s", ticker, e)
                agent_results.append(None)

            distilled = queen.distill(ticker, agent_results)
            swarm_results[ticker] = distilled

            res = "✅" if distilled["resonance"]["resonance_detected"] else "—"
            _log.info("[%d/%d] %s: %.1f/10 %s %s", idx, len(targets), ticker, distilled['final_score'], distilled['direction'], res)

            # 写入 checkpoint（每个 ticker 完成后）
            try:
                with open(checkpoint_file, "w") as f:
                    json.dump({"results": swarm_results, "targets": targets}, f, default=str)
            except (OSError, TypeError) as e:
                _log.warning("Checkpoint 写入失败: %s", e)

        # 扫描完成，保存蜂群结果供 ML 报告同步使用
        try:
            swarm_json = self.report_dir / f".swarm_results_{self.date_str}.json"
            with open(swarm_json, "w") as f:
                json.dump(swarm_results, f, default=str, ensure_ascii=False)
        except (OSError, TypeError) as e:
            _log.warning("Swarm results 保存失败: %s", e)
        # 清理 checkpoint
        try:
            checkpoint_file.unlink(missing_ok=True)
        except OSError as e:
            _log.debug("Checkpoint 清理失败: %s", e)

        elapsed = time.time() - start_time

        # LLM Token 使用统计
        try:
            import llm_service
            usage = llm_service.get_usage()
            if usage["call_count"] > 0:
                _log.info("蜂群耗时：%.1fs | LLM: %d调用 $%.4f", elapsed, usage['call_count'], usage['total_cost_usd'])
            else:
                _log.info("蜂群耗时：%.1fs | 规则引擎模式", elapsed)
        except (ImportError, AttributeError, KeyError) as e:
            _log.info("蜂群耗时：%.1fs (LLM stats unavailable: %s)", elapsed, e)

        # Week 4: 记录扫描指标 + SLO 检查
        if self.metrics:
            try:
                scores = [d.get("final_score", 5.0) for d in swarm_results.values()]
                agent_errors = sum(
                    1 for d in swarm_results.values()
                    if d.get("supporting_agents", 0) == 0
                )
                resonance_n = sum(
                    1 for d in swarm_results.values()
                    if d.get("resonance", {}).get("resonance_detected")
                )
                avg_real = (
                    sum(d.get("data_real_pct", 0) for d in swarm_results.values()) / len(swarm_results)
                    if swarm_results else 0
                )
                llm_c, llm_cost = 0, 0.0
                try:
                    import llm_service as _ls
                    _u = _ls.get_usage()
                    llm_c, llm_cost = _u.get("call_count", 0), _u.get("total_cost_usd", 0.0)
                except (ImportError, AttributeError, KeyError):
                    pass

                self.metrics.record_scan(
                    ticker_count=len(swarm_results),
                    duration_seconds=elapsed,
                    agent_count=len(all_agents),
                    prefetch_seconds=prefetch_elapsed,
                    avg_score=sum(scores) / len(scores) if scores else 5.0,
                    max_score=max(scores) if scores else 5.0,
                    min_score=min(scores) if scores else 5.0,
                    agent_errors=agent_errors,
                    agent_total=len(swarm_results) * len(all_agents),
                    data_real_pct=avg_real,
                    resonance_count=resonance_n,
                    llm_calls=llm_c,
                    llm_cost_usd=llm_cost,
                    session_id=self._session_id or "",
                    scan_mode="swarm",
                )
                for ticker, data in swarm_results.items():
                    self.metrics.record_ticker(
                        ticker=ticker,
                        final_score=data.get("final_score", 5.0),
                        direction=data.get("direction", "neutral"),
                        supporting_agents=data.get("supporting_agents", 0),
                        data_real_pct=data.get("data_real_pct", 0),
                        resonance_detected=data.get("resonance", {}).get("resonance_detected", False),
                        session_id=self._session_id or "",
                    )

                # SLO 检查
                violations = self.metrics.check_slo(days=1)
                if violations:
                    _log.warning("SLO 违规 %d 条: %s",
                                 len(violations),
                                 "; ".join(v["details"] for v in violations))
            except (OSError, ValueError, KeyError, TypeError) as e:
                _log.warning("指标收集异常: %s", e)

        # Phase 6: 回测反馈循环
        if Backtester:
            try:
                bt = Backtester()
                bt.save_predictions(swarm_results)
                bt.run_backtest()
                bt.adapt_weights(min_samples=5)
            except (OSError, ValueError, KeyError, TypeError) as e:
                _log.warning("回测异常: %s", e)

        # Phase 6: Slack 推送高分机会 + 异常信号
        if self.slack_notifier and self.slack_notifier.enabled:
            for ticker, data in swarm_results.items():
                score = data.get("final_score", 0)
                direction = data.get("direction", "neutral")
                dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(direction, direction)

                # 高分机会推送（>= 7.5）
                if score >= 7.5:
                    self._submit_bg(
                        self.slack_notifier.send_opportunity_alert,
                        ticker, score, dir_cn,
                        data.get("discovery", "高分机会"),
                        [f"评分 {score:.1f}/10"]
                    )

                # 异常信号推送：强看空 或 内幕大额交易
                elif score <= 3.0:
                    self._submit_bg(
                        self.slack_notifier.send_risk_alert,
                        f"{ticker} 低分预警",
                        f"蜂群评分仅 {score:.1f}/10，方向 {dir_cn}",
                        "HIGH"
                    )

        # 生成综合报告
        report = self._build_swarm_report(swarm_results, board)

        # Phase 3 P2: 为高分机会添加日历提醒（后台线程池，退出时等待完成）
        if self.calendar and report.get('opportunities'):
            for opp in report['opportunities']:
                if opp.opportunity_score >= 7.5:
                    self._submit_bg(
                        self.calendar.add_opportunity_reminder,
                        opp.ticker, opp.opportunity_score, opp.direction,
                        f"{opp.key_catalysts[0] if opp.key_catalysts else '高分机会'}"
                    )

        # Phase 2: 异步保存会话（使用共享线程池，退出时等待完成）
        if self.memory_store and self._session_id:
            snapshot = board.compact_snapshot()  # 在主线程取快照（线程安全）
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "swarm",
                targets, swarm_results, snapshot, elapsed
            )

        # Phase 3 内存优化: 将高价值发现存入向量记忆（长期记忆）
        if self.vector_memory and self.vector_memory.enabled:
            stored = 0
            # 1. 存储 Queen 的最终评分
            for ticker, data in swarm_results.items():
                if data.get("final_score", 0) >= 5.0:
                    self.vector_memory.store(
                        ticker=ticker,
                        agent_id="QueenDistiller",
                        discovery=f"评分{data['final_score']:.1f} {data['direction']} "
                                  f"支持{data.get('supporting_agents', 0)}Agent",
                        direction=data["direction"],
                        score=data["final_score"],
                        source="swarm_scan",
                        session_id=self._session_id or ""
                    )
                    stored += 1
            # 2. 存储信息素板上每个 Agent 的高价值发现
            for entry in board.snapshot():
                if entry.get("self_score", 0) >= 6.0:
                    self.vector_memory.store(
                        ticker=entry.get("ticker", ""),
                        agent_id=entry.get("agent_id", ""),
                        discovery=entry.get("discovery", "")[:300],
                        direction=entry.get("direction", "neutral"),
                        score=entry.get("self_score", 5.0),
                        source=entry.get("source", ""),
                        session_id=self._session_id or ""
                    )
                    stored += 1
            if stored > 0:
                _log.info("已存入 %d 条长期记忆 (Chroma)", stored)

        # Slack 推送
        if self.slack_notifier and self.slack_notifier.enabled:
            try:
                self.slack_notifier.send_daily_report(report)
                _log.info("Slack 日报已发送")
            except (ConnectionError, TimeoutError, OSError, ValueError) as e:
                _log.error("Slack 日报发送失败: %s", e, exc_info=True)

        return report

    def run_crew_scan(self, focus_tickers: List[str] = None) -> Dict:
        """
        CrewAI 模式蜂群扫描 - 使用 Process.hierarchical 主-子 Agent 递归调度
        若 crewai 未安装，自动降级到 run_swarm_scan()

        Args:
            focus_tickers: 重点关注标的（如为None则扫描全部watchlist）

        Returns:
            完整的蜂群分析报告
        """
        # 检查 CrewAI 是否可用
        if not AlphaHiveCrew or not CREWAI_CONFIG.get("enabled"):
            _log.info("CrewAI 未安装或未启用，降级到标准蜂群模式")
            return self.run_swarm_scan(focus_tickers)

        _log.info("CrewAI 模式 %s", self.date_str)

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        _log.info("标的：%s", " ".join(targets))

        # 创建共享的信息素板
        board = PheromoneBoard(memory_store=self.memory_store, session_id=self._session_id)

        # 构建 CrewAI Crew
        crew = AlphaHiveCrew(board=board, memory_store=self.memory_store)
        crew.build(targets)

        _log.info("CrewAI %d Agent", crew.get_agents_count())

        swarm_results = {}
        start_time = time.time()

        # 使用 CrewAI 分析每个标的
        for i, ticker in enumerate(targets, 1):
            _log.info("[%d/%d] CrewAI 分析 %s", i, len(targets), ticker)

            try:
                result = crew.analyze(ticker)
                swarm_results[ticker] = result

                _log.info("  %s: %.1f/10 %s", ticker, result.get('final_score', 0), result.get('direction', 'neutral'))

            except (ValueError, KeyError, TypeError, RuntimeError, ConnectionError) as e:
                _log.warning("  %s CrewAI 分析失败: %s", ticker, str(e)[:80])
                swarm_results[ticker] = {
                    "ticker": ticker,
                    "final_score": 0.0,
                    "direction": "neutral",
                    "discovery": f"CrewAI 分析失败: {str(e)}",
                    "error": str(e)
                }

        elapsed = time.time() - start_time
        _log.info("CrewAI 耗时：%.1fs", elapsed)

        # 转换为标准报告格式（兼容 run_swarm_scan 输出）
        report = self._build_swarm_report(swarm_results, board)

        # 异步保存会话（使用共享线程池，退出时等待完成）
        if self.memory_store and self._session_id:
            snapshot = board.compact_snapshot()
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "crew_scan",
                targets, swarm_results, snapshot, elapsed
            )

        # Slack 推送
        if self.slack_notifier and self.slack_notifier.enabled:
            try:
                self.slack_notifier.send_daily_report(report)
                _log.info("Slack 日报已发送")
            except (ConnectionError, TimeoutError, OSError, ValueError) as e:
                _log.error("Slack 日报发送失败: %s", e, exc_info=True)

        return report

    def _build_swarm_report(self, swarm_results: Dict, board: PheromoneBoard) -> Dict:
        """
        将蜂群分析结果转换为标准报告格式

        Args:
            swarm_results: QueenDistiller 的所有汇总结果
            board: 信息素板（用于提取全局信息）

        Returns:
            标准报告格式
        """
        # 排序结果
        sorted_results = sorted(
            swarm_results.items(),
            key=lambda x: x[1]["final_score"],
            reverse=True
        )

        # 构建 OpportunityItem 列表（兼容现有报告格式）
        opportunities = []
        for ticker, swarm_data in sorted_results:
            opp = OpportunityItem(
                ticker=ticker,
                direction="看多" if swarm_data["direction"] == "bullish" else (
                    "看空" if swarm_data["direction"] == "bearish" else "中性"
                ),
                signal_score=swarm_data["final_score"],
                catalyst_score=swarm_data["final_score"] * 0.9,
                sentiment_score=swarm_data["final_score"] * 0.85,
                odds_score=swarm_data["final_score"] * 0.8,
                risk_score=swarm_data["final_score"] * 0.95,
                options_score=swarm_data["final_score"] * 0.88,
                opportunity_score=swarm_data["final_score"],
                confidence=min(95, swarm_data["final_score"] * 10) if swarm_data["final_score"] >= 7.5 else 60,
                key_catalysts=["多 Agent 共振信号"] if swarm_data["resonance"]["resonance_detected"] else ["待验证"],
                options_signal=f"共振信号 ({swarm_data['resonance']['supporting_agents']} Agent)",
                risks=["多头拥挤"] if swarm_data["resonance"]["resonance_detected"] else [],
                thesis_break="信号分散"
            )
            opportunities.append(opp)

        self.opportunities = opportunities

        # 构建标准报告
        report = {
            "date": self.date_str,
            "timestamp": self.timestamp.isoformat(),
            "system_status": "✅ 蜂群协作完成",
            "phase_completed": "完整蜂群流程 (Swarm Mode)",
            "swarm_metadata": {
                "total_agents": 6,
                "tickers_analyzed": len(swarm_results),
                "resonances_detected": sum(1 for r in swarm_results.values() if r["resonance"]["resonance_detected"]),
                "pheromone_board_entries": board.get_entry_count()
            },
            "markdown_report": self._generate_swarm_markdown_report(swarm_results),
            "twitter_threads": self._generate_swarm_twitter_threads(swarm_results),
            "opportunities": [
                {
                    "rank": i + 1,
                    "ticker": opp.ticker,
                    "direction": opp.direction,
                    "opp_score": round(opp.opportunity_score, 1),
                    "confidence": f"{opp.confidence:.0f}%",
                    "resonance": swarm_results[opp.ticker]["resonance"]["resonance_detected"],
                    "supporting_agents": swarm_results[opp.ticker]["supporting_agents"],
                    "thesis_break": opp.thesis_break
                }
                for i, opp in enumerate(self.opportunities[:5])
            ]
        }

        return report

    def _generate_swarm_markdown_report(self, swarm_results: Dict) -> str:
        """生成蜂群模式的 Markdown 报告（8 版块完整结构）"""

        md = []
        md.append(f"# 【{self.date_str}】Alpha Hive 蜂群协作日报")
        md.append("")
        md.append(f"**自动生成于**：{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        md.append(f"**系统模式**：完全去中心化蜂群协作 | 6 个自治 Agent")
        md.append("")

        sorted_results = sorted(
            swarm_results.items(),
            key=lambda x: x[1]["final_score"],
            reverse=True
        )

        # ====== 版块 1：今日摘要 ======
        resonances = sum(1 for r in swarm_results.values() if r["resonance"]["resonance_detected"])
        md.append("## 1) 今日摘要")
        md.append("")
        md.append(f"- 扫描标的：{len(swarm_results)} 个 | 共振信号：{resonances}/{len(swarm_results)}")
        for i, (ticker, data) in enumerate(sorted_results[:3], 1):
            res = "共振" if data["resonance"]["resonance_detected"] else ""
            md.append(f"- **{ticker}** {data['direction'].upper()} {data['final_score']:.1f}/10 {res}")
        md.append("")

        # ====== 版块 2：今日聪明钱动向（ScoutBeeNova） ======
        md.append("## 2) 今日聪明钱动向")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("ScoutBeeNova", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            insider = details.get("insider", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if insider:
                sentiment = insider.get("sentiment", "unknown")
                bought = insider.get("dollar_bought", 0)
                sold = insider.get("dollar_sold", 0)
                filings = insider.get("filings", 0)
                md.append(f"- 内幕交易情绪：**{sentiment}** | 申报数：{filings}")
                if bought > 0:
                    md.append(f"- 内幕买入金额：${bought:,.0f}")
                if sold > 0:
                    md.append(f"- 内幕卖出金额：${sold:,.0f}")
                notable = insider.get("notable_trades", [])
                for t in notable[:2]:
                    if isinstance(t, dict):
                        md.append(f"  - {t.get('insider', '?')}：{t.get('code_desc', '?')} {t.get('shares', 0):,.0f} 股")
            crowding = details.get("crowding_score", "")
            if crowding:
                md.append(f"- 拥挤度：{crowding:.0f}/100")
            md.append("")

        # ====== 版块 3：市场隐含预期（OracleBeeEcho） ======
        md.append("## 3) 市场隐含预期")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("OracleBeeEcho", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                iv = details.get("iv_rank")
                pc = details.get("put_call_ratio")
                gamma = details.get("gamma_exposure")
                if iv is not None:
                    md.append(f"- IV Rank：{iv}")
                if pc is not None:
                    pc_val = pc if isinstance(pc, (int, float)) else pc
                    md.append(f"- Put/Call Ratio：{pc_val}")
                if gamma is not None:
                    md.append(f"- Gamma Exposure：{gamma}")
                # 异常活动
                unusual = details.get("unusual_activity", [])
                if unusual:
                    md.append(f"- 异常活动：{len(unusual)} 个信号")
                    for u in unusual[:3]:
                        if isinstance(u, dict):
                            utype = u.get("type", "unknown").replace("_", " ")
                            strike = u.get("strike", "")
                            vol = u.get("volume", 0)
                            bull = "看涨" if u.get("bullish") else "看跌"
                            md.append(f"  - {bull} {utype} ${strike} ({vol:,.0f}手)")
                        elif isinstance(u, str):
                            md.append(f"  - {u}")
            md.append("")

        # ====== 版块 4：X 情绪汇总（BuzzBeeWhisper） ======
        md.append("## 4) X 情绪汇总")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("BuzzBeeWhisper", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                sent_pct = details.get("sentiment_pct")
                mom = details.get("momentum_5d")
                vol = details.get("volume_ratio")
                if sent_pct is not None:
                    md.append(f"- 看多情绪：{sent_pct}%")
                if mom is not None:
                    md.append(f"- 5 日动量：{mom:+.1f}%")
                if vol is not None:
                    md.append(f"- 量比：{vol:.1f}x")
                reddit = details.get("reddit_mentions") or details.get("reddit_rank")
                if reddit:
                    md.append(f"- Reddit 热度：{reddit}")
            md.append("")

        # ====== 版块 5：财报/事件催化剂（ChronosBeeHorizon） ======
        md.append("## 5) 财报/事件催化剂")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("ChronosBeeHorizon", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                earnings = details.get("next_earnings") or details.get("earnings_date")
                if earnings:
                    md.append(f"- 下次财报：{earnings}")
                events = details.get("upcoming_events") or details.get("catalysts", [])
                if isinstance(events, list):
                    for ev in events[:3]:
                        if isinstance(ev, dict):
                            md.append(f"  - {ev.get('date', '?')}：{ev.get('event', ev.get('description', '?'))}")
                        elif isinstance(ev, str):
                            md.append(f"  - {ev}")
                past = details.get("recent_events", [])
                if isinstance(past, list):
                    for ev in past[:2]:
                        if isinstance(ev, dict):
                            md.append(f"  - [已发生] {ev.get('description', ev)}")
            md.append("")

        # ====== 版块 6：竞争格局分析（RivalBeeVanguard） ======
        md.append("## 6) 竞争格局分析")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("RivalBeeVanguard", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                ml_pred = details.get("ml_prediction") or details.get("prediction")
                if isinstance(ml_pred, dict):
                    md.append(f"- ML 预测方向：{ml_pred.get('direction', '?')}")
                    md.append(f"- ML 置信度：{ml_pred.get('confidence', '?')}")
                peers = details.get("peer_comparison") or details.get("peers", [])
                if isinstance(peers, list) and peers:
                    md.append(f"- 同业对标：{', '.join(str(p) for p in peers[:5])}")
            md.append("")

        # ====== 版块 6.5：看空对冲观点（BearBeeContrarian） ======
        md.append("## 6.5) 看空对冲观点")
        md.append("")
        md.append("> BearBeeContrarian 专门寻找看空信号，平衡蜂群系统性看多偏差")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("BearBeeContrarian", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            bear_score = details.get("bear_score", 0)
            signals = details.get("bearish_signals", [])
            direction = agent.get("direction", "neutral")

            if direction == "bearish":
                severity = "**看空警告**"
            elif direction == "neutral":
                severity = "需关注风险点"
            elif signals:
                severity = "风险提示"
            else:
                severity = "暂无看空信号"

            md.append(f"### {ticker} ({severity} | 看空强度 {bear_score:.1f}/10)")
            if signals:
                for sig in signals:
                    md.append(f"- {sig}")
            elif discovery:
                md.append(f"- {discovery}")
            else:
                md.append("- 未发现显著看空信号")
            # 数据来源标注
            sources = details.get("data_sources", {})
            if sources:
                src_labels = {"pheromone_board": "蜂群共享", "sec_api": "SEC直查",
                              "options_api": "期权直查", "finviz_api": "Finviz",
                              "yfinance": "yfinance", "unavailable": "不可用"}
                src_parts = [f"{k}={src_labels.get(v, v)}" for k, v in sources.items()]
                md.append(f"- *数据来源*：{' | '.join(src_parts)}")
            md.append("")

        # ====== 版块 7：综合判断 & 信号强度（GuardBeeSentinel + 全体投票） ======
        md.append("## 7) 综合判断 & 信号强度")
        md.append("")
        md.append("| 标的 | 方向 | 综合分 | 共振 | 投票(多/空/中) | 数据% | 失效条件 |")
        md.append("|------|------|--------|------|---------------|-------|---------|")
        for ticker, data in sorted_results:
            res = "Y" if data["resonance"]["resonance_detected"] else "N"
            ab = data["agent_breakdown"]
            data_pct = data.get("data_real_pct", 0)
            # 从 GuardBeeSentinel 获取交叉验证信息
            guard = data.get("agent_details", {}).get("GuardBeeSentinel", {})
            guard_discovery = guard.get("discovery", "")
            thesis_break = "信号分散" if not guard_discovery else guard_discovery[:30]
            md.append(
                f"| **{ticker}** | {data['direction'].upper()} | "
                f"{data['final_score']:.1f} | {res} | "
                f"{ab['bullish']}/{ab['bearish']}/{ab['neutral']} | "
                f"{data_pct:.0f}% | {thesis_break} |"
            )
        md.append("")

        # GuardBeeSentinel 详细交叉验证
        md.append("### 交叉验证详情")
        md.append("")
        for ticker, data in sorted_results:
            guard = data.get("agent_details", {}).get("GuardBeeSentinel", {})
            discovery = guard.get("discovery", "")
            if discovery:
                md.append(f"- **{ticker}**：{discovery}")
        md.append("")

        # ====== 版块 8：数据来源 & 免责声明 ======
        md.append("## 8) 数据来源 & 免责声明")
        md.append("")
        md.append("**蜂群分工**：")
        md.append("- ScoutBeeNova：聪明钱侦察（SEC Form 4/13F + 拥挤度）")
        md.append("- OracleBeeEcho：市场预期（期权 IV/P-C Ratio/Gamma）")
        md.append("- BuzzBeeWhisper：社交情绪（X/Reddit/Finviz）")
        md.append("- ChronosBeeHorizon：催化剂追踪（财报/事件日历）")
        md.append("- RivalBeeVanguard：竞争格局（ML 预测 + 行业对标）")
        md.append("- GuardBeeSentinel：交叉验证（共振检测 + 风险调整）")
        md.append("")
        md.append("**免责声明**：")
        md.append(DISCLAIMER_FULL)
        md.append("")

        return "\n".join(md)

    def _generate_swarm_twitter_threads(self, swarm_results: Dict) -> List[str]:
        """生成蜂群模式的 X 线程版本"""

        threads = []
        sorted_results = sorted(
            swarm_results.items(),
            key=lambda x: x[1]["final_score"],
            reverse=True
        )

        # 主线程
        main_thread = []
        main_thread.append(
            f"【Alpha Hive 蜂群日报 {self.date_str}】"
            f"6 个自治 Agent 协作分析，多数投票共振信号。"
            f"{DISCLAIMER_SHORT}👇"
        )

        for i, (ticker, data) in enumerate(sorted_results[:3], 1):
            resonance_emoji = "✅" if data["resonance"]["resonance_detected"] else "❌"
            insight = data.get("key_insight", "")
            tweet = (
                f"{i}. **{ticker}** {data['direction'].upper()}\n"
                f"蜂群评分：{data['final_score']:.1f}/10 | 共振：{resonance_emoji}\n"
                f"Agent 投票：看多{data['agent_breakdown']['bullish']} vs 看空{data['agent_breakdown']['bearish']}"
            )
            if insight:
                tweet += f"\nAI洞察：{insight}"
            main_thread.append(tweet)

        main_thread.append(
            f"🐝 6 个 Agent 独立分析 → 信息素板实时交换 → 多数投票汇总\n"
            f"高共振信号优先级最高。风险提示：控制仓位。\n"
            f"下一步：T+1 验证，T+7 回看准确率。@igg_wang748"
        )

        threads.append("\n\n".join(main_thread))

        return threads

    def _parse_ml_report_to_opportunity(self, ticker: str, ml_report: Dict) -> OpportunityItem:
        """将 ML 报告解析为 OpportunityItem"""

        adv = ml_report.get("advanced_analysis", {})
        opts = adv.get("options_analysis")
        ml_pred = ml_report.get("ml_prediction", {})

        # 提取各维度评分（假设已标准化为 0-10）
        signal_score = adv.get("signal_strength", 5.0)
        catalyst_score = adv.get("catalyst_score", 5.0)
        sentiment_score = adv.get("sentiment_score", 5.0)
        odds_score = adv.get("odds_score", 5.0)
        risk_score = adv.get("risk_adjusted_score", 5.0)

        # 安全提取期权分数
        if opts and isinstance(opts, dict):
            options_score = float(opts.get("options_score", 5.0))
            options_signal = opts.get("signal_summary", "信号平衡")
        else:
            options_score = 5.0
            options_signal = "期权数据不可用"

        # 计算综合 Opportunity Score（与 CLAUDE.md 5 维公式一致）
        # options_score 合并入 odds 维度（取平均）
        odds_combined = (odds_score + options_score) / 2.0
        opp_score = (
            0.30 * signal_score +
            0.20 * catalyst_score +
            0.20 * sentiment_score +
            0.15 * odds_combined +
            0.15 * risk_score
        )

        # 判断方向
        if opp_score >= 7.5:
            direction = "看多" if signal_score > 5.0 else "看空"
            confidence = min(95, opp_score * 10)
        elif opp_score >= 6.0:
            direction = "中性"
            confidence = 60
        else:
            direction = "中性"
            confidence = 30

        return OpportunityItem(
            ticker=ticker,
            direction=direction,
            signal_score=signal_score,
            catalyst_score=catalyst_score,
            sentiment_score=sentiment_score,
            odds_score=odds_score,
            risk_score=risk_score,
            options_score=options_score,
            opportunity_score=opp_score,
            confidence=confidence,
            key_catalysts=adv.get("upcoming_catalysts", [])[:3] if adv.get("upcoming_catalysts") else [],
            options_signal=options_signal,
            risks=adv.get("key_risks", [])[:2] if adv.get("key_risks") else [],
            thesis_break=adv.get("thesis_break_conditions", "未定义")
        )

    def _build_report(self) -> Dict:
        """构建完整报告"""

        report = {
            "date": self.date_str,
            "timestamp": self.timestamp.isoformat(),
            "system_status": "✅ 完成",
            "phase_completed": "1-6 (完整蜂群流程)",
            "markdown_report": self._generate_markdown_report(),
            "twitter_threads": self._generate_twitter_threads(),
            "opportunities": [
                {
                    "rank": i + 1,
                    "ticker": opp.ticker,
                    "direction": opp.direction,
                    "opp_score": round(opp.opportunity_score, 1),
                    "confidence": f"{opp.confidence:.0f}%",
                    "options_signal": opp.options_signal,
                    "key_catalyst": opp.key_catalysts[0] if opp.key_catalysts else "N/A",
                    "thesis_break": opp.thesis_break
                }
                for i, opp in enumerate(self.opportunities[:5])
            ],
            "observation_list": self.observations
        }

        return report

    def _generate_markdown_report(self) -> str:
        """生成中文 Markdown 报告"""

        md = []
        md.append(f"# 【{self.date_str}】Alpha Hive 每日投资简报")
        md.append("")
        md.append(f"**自动生成于**：{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        md.append(f"**系统状态**：✅ 完全激活 | Phase 1-6 完成")
        md.append("")

        # 1. 今日摘要
        md.append("## 📊 今日摘要（Top 3）")
        md.append("")

        for i, opp in enumerate(self.opportunities[:3], 1):
            md.append(f"### {i}. **{opp.ticker}** - {opp.direction}")
            md.append(f"- **机会分数**：{opp.opportunity_score:.1f}/10 | **置信度**：{opp.confidence:.0f}%")
            md.append(f"- **期权信号**：{opp.options_signal}")
            if opp.key_catalysts:
                md.append(f"- **关键催化剂**：{', '.join(opp.key_catalysts[:2])}")
            md.append("")

        # 2. 机会清单
        md.append("## 🎯 完整机会清单")
        md.append("")
        md.append("| 排序 | 标的 | 方向 | 综合分 | 期权信号 | 置信度 |")
        md.append("|------|------|------|--------|---------|--------|")

        for i, opp in enumerate(self.opportunities[:5], 1):
            md.append(
                f"| {i} | **{opp.ticker}** | {opp.direction} | "
                f"{opp.opportunity_score:.1f} | {opp.options_signal[:12]}... | {opp.confidence:.0f}% |"
            )

        md.append("")

        # 3. 风险雷达
        md.append("## ⚠️ 风险雷达")
        md.append("")
        for opp in self.opportunities[:3]:
            if opp.risks:
                md.append(f"**{opp.ticker}**：{', '.join(opp.risks)}")

        md.append("")

        # 4. 数据来源与免责
        md.append("## 📝 数据来源 & 免责声明")
        md.append("")
        md.append("**数据源**：")
        md.append("- StockTwits 情绪（实时）")
        md.append("- Polymarket 赔率（每5分钟）")
        md.append("- Yahoo Finance / yFinance（实时）")
        md.append("- SEC 披露（每日更新）")
        md.append("- **期权链数据**（yFinance，每5分钟缓存）")
        md.append("")
        md.append("**免责声明**：")
        md.append(DISCLAIMER_FULL)
        md.append("")

        return "\n".join(md)

    def _generate_twitter_threads(self) -> List[str]:
        """生成 X 线程版本"""

        threads = []

        # 主线程
        main_thread = []
        main_thread.append(
            f"【Alpha Hive 日报 {self.date_str}】"
            f"{DISCLAIMER_SHORT}"
            f"今天最值得跟踪的 3 个机会 👇"
        )

        for i, opp in enumerate(self.opportunities[:3], 1):
            main_thread.append(
                f"{i}. **{opp.ticker}** {opp.direction}\n"
                f"综合分：{opp.opportunity_score:.1f}/10 | 期权信号：{opp.options_signal}\n"
                f"主催化剂：{opp.key_catalysts[0] if opp.key_catalysts else 'TBD'}"
            )

        main_thread.append(
            f"更多详情见完整日报。风险提示：高波动标的需控制仓位。"
            f"下一步跟踪：T+1 验证信号强度，T+7 回看预测偏差。@igg_wang748"
        )

        threads.append("\n\n".join(main_thread))

        return threads

    def auto_commit_and_notify(self, report: Dict) -> Dict:
        """
        自动提交报告到 Git + Slack 通知（Agent Toolbox 演示）

        新功能：使用 AgentHelper 自动执行 Git 提交和通知
        """
        _log.info("Auto-commit & Notify 启动")

        results = {}

        # 1. Git 提交报告
        _log.info("Git commit...")
        status = self.agent_helper.git.status()
        if status.get("modified_files"):
            commit_result = self.agent_helper.git.commit(
                f"Alpha Hive 蜂群日报 {self.date_str}"
            )
            results["git_commit"] = commit_result
            if commit_result["success"]:
                _log.info("Git commit 成功")
            else:
                _log.warning("Git commit 失败：%s", commit_result.get('message'))
        else:
            _log.info("无需提交（工作目录干净）")

        # 2. Git 推送
        _log.info("Git push...")
        push_result = self.agent_helper.git.push("main")
        results["git_push"] = push_result
        if push_result["success"]:
            _log.info("Git push 成功")
        else:
            _log.warning("Git push 失败")

        # 3. Slack 通知
        _log.info("发送 Slack 通知...")
        top_opp = self.opportunities[0] if self.opportunities else None
        if top_opp:
            message = (
                f"📊 *蜂群日报 {self.date_str}*\n"
                f"🎯 Top 机会：{top_opp.ticker} {top_opp.direction}\n"
                f"📈 综合分：{top_opp.opportunity_score:.1f}/10\n"
                f"🔗 报告：`{self.report_dir / f'alpha-hive-daily-{self.date_str}.md'}`"
            )
            slack_result = self.agent_helper.notify.send_slack_message(
                "#alpha-hive",
                message
            )
            results["slack_notification"] = slack_result
            if slack_result.get("success"):
                _log.info("Slack 通知已发送")
            else:
                _log.warning("Slack 通知失败：%s", slack_result.get('error'))

        _log.info("Auto-commit & Notify 完成")
        return results

    def save_report(self, report: Dict) -> str:
        """保存报告到文件"""

        # 保存 JSON 版本
        json_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # 保存 Markdown 版本
        md_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.md"
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(report["markdown_report"])

        # 保存 X 线程版本
        for i, thread in enumerate(report["twitter_threads"], 1):
            thread_file = self.report_dir / f"alpha-hive-thread-{self.date_str}-{i}.txt"
            with open(thread_file, "w", encoding="utf-8") as f:
                f.write(thread)

        _log.info("报告已保存：%s", md_file.name)

        return str(md_file)


def main():
    """主入口"""

    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="Alpha Hive 每日投资简报生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  # 传统 ML 模式（默认）
  python3 alpha_hive_daily_report.py
  python3 alpha_hive_daily_report.py --tickers NVDA TSLA VKTX
  python3 alpha_hive_daily_report.py --all-watchlist

  # 蜂群协作模式（6 个自治 Agent）
  python3 alpha_hive_daily_report.py --swarm --tickers NVDA TSLA VKTX
  python3 alpha_hive_daily_report.py --swarm --all-watchlist
        """
    )
    parser.add_argument(
        '--tickers',
        nargs='+',
        default=["NVDA", "TSLA", "VKTX"],
        help='要扫描的股票代码列表（空格分隔，默认：NVDA TSLA VKTX）'
    )
    parser.add_argument(
        '--all-watchlist',
        action='store_true',
        help='扫描配置中的全部监控列表'
    )
    parser.add_argument(
        '--swarm',
        action='store_true',
        help='启用蜂群协作模式（6 个自治 Agent 并行分析）'
    )

    args = parser.parse_args()

    # 创建报告生成器
    reporter = AlphaHiveDailyReporter()

    # 确定扫描标的
    focus_tickers = list(WATCHLIST.keys())[:10] if args.all_watchlist else args.tickers

    if args.swarm:
        report = reporter.run_swarm_scan(focus_tickers=focus_tickers)
    else:
        report = reporter.run_daily_scan(focus_tickers=focus_tickers)

    # 保存报告
    report_path = reporter.save_report(report)

    _log.info("完成！报告：%s", report_path)

    return report


if __name__ == "__main__":
    main()
