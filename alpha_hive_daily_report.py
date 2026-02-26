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

# 财报自动监控器
try:
    from earnings_watcher import EarningsWatcher
except ImportError:
    EarningsWatcher = None

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

        # 财报自动监控器
        self.earnings_watcher = None
        if EarningsWatcher:
            try:
                self.earnings_watcher = EarningsWatcher()
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("EarningsWatcher 初始化失败: %s", e)

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

    def run_swarm_scan(self, focus_tickers: List[str] = None, progress_callback=None) -> Dict:
        """
        真正的蜂群协作扫描 - 7 个自治工蜂并行运行（6 核心 + BearBeeContrarian），实时通过信息素板交换发现

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

        # 实例化 Agent：第一阶段 6 个核心 Agent（可选+CodeExecutor），第二阶段 BearBeeContrarian（读取信息素板后分析）
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

            # 第一阶段：6 个核心 Agent 并行分析（含可选 CodeExecutorAgent）
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

            # 进度回调（供桌面 App 实时动画使用）
            if progress_callback:
                try:
                    progress_callback(idx, len(targets), ticker, distilled)
                except Exception as _cb_err:
                    _log.debug("Progress callback error: %s", _cb_err)

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
        report = self._build_swarm_report(swarm_results, board, agent_count=len(all_agents))

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
        # CrewAI 模式：6 核心 BeeAgent + BearBeeContrarian = 7
        report = self._build_swarm_report(swarm_results, board, agent_count=7)

        # 异步保存会话（使用共享线程池，退出时等待完成）
        if self.memory_store and self._session_id:
            snapshot = board.compact_snapshot()
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "crew_scan",
                targets, swarm_results, snapshot, elapsed
            )

        return report

    def _build_swarm_report(self, swarm_results: Dict, board: PheromoneBoard,
                            agent_count: int = 7) -> Dict:
        """
        将蜂群分析结果转换为标准报告格式

        Args:
            swarm_results: QueenDistiller 的所有汇总结果
            board: 信息素板（用于提取全局信息）
            agent_count: 实际运行的 Agent 总数（Phase-1 + BearBeeContrarian + 可选 CodeExecutor）

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

        # ── P4: 投资组合集中度分析（板块重叠 + 相关性矩阵）──
        concentration = {}
        try:
            from portfolio_concentration import analyze_concentration
            from config import WATCHLIST
            concentration = analyze_concentration(swarm_results, WATCHLIST)
            _log.info("P4 集中度分析：%s（风险=%s）",
                      concentration.get("summary", ""), concentration.get("concentration_risk", ""))
        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.debug("P4 portfolio_concentration 不可用: %s", e)

        # ── P5: 宏观环境快照（附加到报告元数据）──
        macro_snapshot = {}
        try:
            from fred_macro import get_macro_context
            macro_snapshot = get_macro_context()
            _log.info("P5 宏观环境：%s", macro_snapshot.get("summary", ""))
        except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
            _log.debug("P5 fred_macro 不可用: %s", e)

        # ── P3: 获取回测准确率统计（附加到报告）──
        backtest_stats = {}
        try:
            if Backtester:
                _bt = Backtester()
                backtest_stats = _bt.store.get_accuracy_stats("t7", days=30)
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log.debug("Backtest stats unavailable: %s", e)

        # 构建标准报告
        report = {
            "date": self.date_str,
            "timestamp": self.timestamp.isoformat(),
            "system_status": "✅ 蜂群协作完成",
            "phase_completed": "完整蜂群流程 (Swarm Mode)",
            "swarm_metadata": {
                "total_agents": agent_count,
                "tickers_analyzed": len(swarm_results),
                "resonances_detected": sum(1 for r in swarm_results.values() if r["resonance"]["resonance_detected"]),
                "pheromone_board_entries": board.get_entry_count()
            },
            "concentration_analysis": concentration,
            "macro_context": macro_snapshot,
            "backtest_stats": backtest_stats,
            "markdown_report": self._generate_swarm_markdown_report(swarm_results, concentration, macro_snapshot, backtest_stats, agent_count=agent_count),
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

    def _generate_swarm_markdown_report(self, swarm_results: Dict,
                                         concentration: Dict = None,
                                         macro_context: Dict = None,
                                         backtest_stats: Dict = None,
                                         agent_count: int = 7) -> str:
        """生成蜂群模式的 Markdown 报告（8 版块 + P4集中度 + P5宏观 + P3回测）"""

        md = []
        md.append(f"# 【{self.date_str}】Alpha Hive 蜂群协作日报")
        md.append("")
        md.append(f"**自动生成于**：{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        md.append(f"**系统模式**：完全去中心化蜂群协作 | {agent_count} 个自治工蜂（6 核心 + BearBeeContrarian）")
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

        # ====== 版块 P4：投资组合集中度风险 ======
        if concentration and concentration.get("sector_breakdown"):
            risk_level = concentration.get("concentration_risk", "low")
            risk_emoji = {"low": "✅", "medium": "⚠️", "high": "🚨"}.get(risk_level, "")
            md.append(f"## 📊 投资组合集中度分析 {risk_emoji}")
            md.append("")
            md.append(f"**集中度风险**：{risk_level.upper()} | **综合评分**：{concentration.get('risk_score', 0):.1f}/10")
            md.append("")

            # 板块分布
            md.append("**板块分布**：")
            for sector, info in concentration.get("sector_breakdown", {}).items():
                tickers_str = " / ".join(info.get("tickers", []))
                md.append(f"- {sector}：{info.get('pct', 0):.0f}%（{tickers_str}）")
            md.append("")

            # 相关性警告
            corr_warns = concentration.get("correlation_warnings", [])
            if corr_warns:
                md.append("**高相关对（≥0.70）**：")
                for w in corr_warns[:4]:
                    md.append(f"- {w['pair']}：相关系数 {w['correlation']:.2f} [{w['risk'].upper()}]")
                md.append("")

            # 建议
            md.append("**分散化建议**：")
            for rec in concentration.get("recommendations", []):
                md.append(f"- {rec}")
            md.append("")

        # ====== 版块 P5：宏观环境 ======
        if macro_context and macro_context.get("data_source") != "fallback":
            regime = macro_context.get("macro_regime", "neutral")
            regime_emoji = {"risk_on": "🟢", "risk_off": "🔴", "neutral": "🟡"}.get(regime, "")
            md.append(f"## 🌐 宏观环境 {regime_emoji}")
            md.append("")
            md.append(f"**宏观政体**：{regime.upper()} | **评分**：{macro_context.get('macro_score', 5):.1f}/10")
            md.append("")
            md.append(f"| 指标 | 数值 | 状态 |")
            md.append(f"|------|------|------|")
            md.append(f"| VIX | {macro_context.get('vix', 0):.1f} | {macro_context.get('vix_regime', '')} |")
            md.append(f"| 10Y利率 | {macro_context.get('treasury_10y', 0):.2f}% | {macro_context.get('rate_environment', '')} |")
            md.append(f"| 大盘(5日) | {macro_context.get('spx_change_pct', 0):+.2f}% | {macro_context.get('market_trend', '')} |")
            md.append(f"| 美元 | — | {macro_context.get('dollar_trend', '')} |")
            md.append("")
            headwinds = macro_context.get("macro_headwinds", [])
            tailwinds = macro_context.get("macro_tailwinds", [])
            if headwinds:
                md.append("**逆风**：" + " | ".join(headwinds[:3]))
                md.append("")
            if tailwinds:
                md.append("**顺风**：" + " | ".join(tailwinds[:3]))
                md.append("")

        # ====== 版块 P3：历史预测准确率（T+7 回测反馈）======
        if backtest_stats and backtest_stats.get("total_checked", 0) > 0:
            acc = backtest_stats["overall_accuracy"]
            total = backtest_stats["total_checked"]
            correct = backtest_stats["correct_count"]
            avg_ret = backtest_stats["avg_return"]
            md.append("## 📈 历史预测准确率（T+7，近30天）")
            md.append("")
            md.append(
                f"**样本**：{total} 条 | "
                f"**准确率**：{acc * 100:.1f}% ({correct}/{total}) | "
                f"**平均收益**：{avg_ret:+.2f}%"
            )
            md.append("")
            by_ticker = backtest_stats.get("by_ticker", {})
            if by_ticker:
                md.append("| 标的 | 方向准确率 | 预测次数 | 平均收益 |")
                md.append("|------|-----------|---------|---------|")
                for t, info in sorted(
                    by_ticker.items(), key=lambda x: x[1]["total"], reverse=True
                )[:6]:
                    md.append(
                        f"| {t} | {info['accuracy'] * 100:.0f}% "
                        f"| {info['total']} | {info['avg_return']:+.2f}% |"
                    )
                md.append("")
            by_dir = backtest_stats.get("by_direction", {})
            if by_dir:
                parts = []
                for d, label in [("bullish", "看多"), ("bearish", "看空"), ("neutral", "中性")]:
                    info = by_dir.get(d, {})
                    if info.get("total", 0) > 0:
                        parts.append(
                            f"{label}:{info['accuracy']*100:.0f}%({info['total']}次)"
                        )
                if parts:
                    md.append("**按方向**：" + " | ".join(parts))
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
            f"7 个自治工蜂协作分析，多数投票共振信号。"
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
            f"🐝 7 个工蜂独立分析（6 核心 + 看空对冲蜂）→ 信息素板实时交换 → 多数投票汇总\n"
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

        # 检查最近一次提交是否已是今日报告（决定 commit vs amend）
        today_commit_msg = f"Alpha Hive 蜂群日报 {self.date_str}"
        last_r = self.agent_helper.git.run_git_cmd("git log -1 --pretty=%s")
        last_msg = last_r.get("stdout", "").strip()
        is_amend = (last_msg == today_commit_msg)
        did_amend = False

        # 1. Git 提交报告
        _log.info("Git commit... (mode: %s)", "amend" if is_amend else "new")
        status = self.agent_helper.git.status()
        if status.get("modified_files"):
            if is_amend:
                # 今日已有提交 → amend 覆盖，不叠加新 commit
                self.agent_helper.git.run_git_cmd("git add -A")
                r = self.agent_helper.git.run_git_cmd(
                    f"git commit --amend -m '{today_commit_msg}'"
                )
                commit_result = {"success": r["success"], "mode": "amend",
                                 "message": r.get("stdout", "") or r.get("stderr", "")}
                did_amend = True
            else:
                commit_result = self.agent_helper.git.commit(today_commit_msg)
            results["git_commit"] = commit_result
            if commit_result["success"]:
                _log.info("Git commit 成功（%s）", "amend" if is_amend else "new")
            else:
                _log.warning("Git commit 失败：%s", commit_result.get('message'))
        else:
            _log.info("无需提交（工作目录干净）")

        # 2. Git 推送（amend 后需要 force-with-lease 强制推送）
        _log.info("Git push...")
        if did_amend:
            r = self.agent_helper.git.run_git_cmd("git push origin main --force-with-lease")
            push_result = {"success": r["success"],
                           "output": r.get("stdout", "") or r.get("stderr", ""),
                           "mode": "force-with-lease"}
        else:
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

    def check_earnings_updates(self, report_path: str = None, tickers: List[str] = None) -> Dict:
        """
        检查 watchlist 中今日是否有标的发布了财报，若有则自动抓取结果并更新简报

        Args:
            report_path: 简报文件路径（默认今日简报）
            tickers: 要检查的标的（默认 WATCHLIST 全部）

        Returns:
            {reporting_today: [], updated: [], earnings_data: {}, errors: []}
        """
        if not self.earnings_watcher:
            _log.info("EarningsWatcher 不可用，跳过财报检查")
            return {"reporting_today": [], "updated": [], "earnings_data": {}, "errors": ["EarningsWatcher not available"]}

        if tickers is None:
            tickers = list(WATCHLIST.keys())

        if report_path is None:
            # 查找今日简报
            candidates = [
                self.report_dir / "reports" / f"alpha_hive_daily_{self.date_str}.md",
                self.report_dir / f"alpha-hive-daily-{self.date_str}.md",
            ]
            for c in candidates:
                if c.exists():
                    report_path = str(c)
                    break

        if report_path is None:
            _log.warning("未找到今日简报文件，跳过财报更新")
            return {"reporting_today": [], "updated": [], "earnings_data": {}, "errors": ["no report file found"]}

        result = self.earnings_watcher.check_and_update(tickers, report_path)

        # 如果有更新，通过 Slack 发送通知
        if result.get("updated") and self.slack_notifier and self.slack_notifier.enabled:
            for ticker in result["updated"]:
                ed = result["earnings_data"].get(ticker, {})
                rev = ed.get("revenue_actual")
                eps = ed.get("eps_actual")
                yoy = ed.get("yoy_revenue_growth")

                msg_parts = [f"{ticker} 财报数据已自动更新"]
                if rev:
                    rev_str = f"${rev / 1e9:.1f}B" if abs(rev) >= 1e9 else f"${rev / 1e6:.0f}M"
                    msg_parts.append(f"营收 {rev_str}")
                if yoy is not None:
                    msg_parts.append(f"YoY {'+' if yoy > 0 else ''}{yoy * 100:.1f}%")
                if eps is not None:
                    msg_parts.append(f"EPS ${eps:.2f}")

                try:
                    self.slack_notifier.send_opportunity_alert(
                        ticker,
                        0,  # score placeholder
                        "财报更新",
                        " | ".join(msg_parts),
                        ["自动抓取", f"完整度: {ed.get('data_completeness', 'N/A')}"]
                    )
                except (OSError, ValueError, RuntimeError) as e:
                    _log.warning("Slack 财报通知发送失败: %s", e)

        # D1: 自动同步财报日期到催化剂日历
        try:
            auto_catalysts = self.earnings_watcher.get_catalysts_for_calendar(tickers)
            if auto_catalysts and hasattr(self, 'calendar') and self.calendar:
                # 合并自动获取的财报日期与 config.CATALYSTS
                from config import CATALYSTS
                merged = dict(CATALYSTS)
                for t, events in auto_catalysts.items():
                    if t in merged:
                        # 去重：只添加尚未存在的 earnings 事件
                        existing_dates = {e.get("scheduled_date") for e in merged[t]}
                        for ev in events:
                            if ev.get("scheduled_date") not in existing_dates:
                                merged[t].append(ev)
                    else:
                        merged[t] = events
                self.calendar.sync_catalysts(catalysts=merged, tickers=tickers)
                _log.info("已自动同步 %d 个标的的财报日期到催化剂日历", len(auto_catalysts))
        except (ImportError, OSError, ValueError, TypeError, AttributeError) as e:
            _log.debug("催化剂日历自动同步跳过: %s", e)

        return result

    def _generate_ml_reports(self, report: Dict) -> List[str]:
        """为扫描标的批量生成 ML 增强 HTML 报告（同步写入，供 _generate_index_html 检测到文件后添加链接）"""
        opps = report.get("opportunities", [])
        tickers = [o.get("ticker") for o in opps if o.get("ticker")]
        if not tickers:
            return []

        # 加载蜂群详细数据（save_report 已写入 .swarm_results_*.json）
        swarm_data: Dict = {}
        sr_path = self.report_dir / f".swarm_results_{self.date_str}.json"
        if sr_path.exists():
            try:
                with open(sr_path) as f:
                    swarm_data = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

        generated = []
        for ticker in tickers:
            try:
                # 从 yfinance 获取当前价格
                real_price, real_change = 100.0, 0.0
                try:
                    import yfinance as _yf
                    _hist = _yf.Ticker(ticker).history(period="5d")
                    if not _hist.empty:
                        real_price = float(_hist["Close"].iloc[-1])
                        if len(_hist) >= 2:
                            real_change = (_hist["Close"].iloc[-1] / _hist["Close"].iloc[-2] - 1) * 100
                except Exception:
                    pass

                ticker_data = {
                    "ticker": ticker,
                    "sources": {
                        "yahoo_finance": {
                            "current_price": real_price,
                            "price_change_5d": real_change,
                            "change_pct": real_change,
                        }
                    },
                }

                # 生成 ML 增强分析
                enhanced = self.ml_generator.generate_ml_enhanced_report(ticker, ticker_data)

                # 注入蜂群数据
                if ticker in swarm_data:
                    enhanced["swarm_results"] = swarm_data[ticker]

                # 同步写入 HTML（必须在 _generate_index_html 前完成，以便文件存在性检测通过）
                html = self.ml_generator.generate_html_report(ticker, enhanced)
                html_path = self.report_dir / f"alpha-hive-{ticker}-ml-enhanced-{self.date_str}.html"
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)

                generated.append(ticker)
                _log.info("ML 增强报告已生成：%s", html_path.name)

            except Exception as e:
                _log.warning("ML 报告生成失败 %s: %s", ticker, e)

        return generated

    def save_report(self, report: Dict) -> str:
        """保存报告到文件（MD / JSON / X线程 / index.html GitHub Pages）"""

        # 保存 JSON 版本
        json_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # 保存 Markdown 版本
        md_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.md"
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(report["markdown_report"])

        # 清理当天旧的 X 线程文件（防止多次运行时数量不同导致残留叠加）
        for old in self.report_dir.glob(f"alpha-hive-thread-{self.date_str}-*.txt"):
            old.unlink()

        # 保存 X 线程版本
        for i, thread in enumerate(report["twitter_threads"], 1):
            thread_file = self.report_dir / f"alpha-hive-thread-{self.date_str}-{i}.txt"
            with open(thread_file, "w", encoding="utf-8") as f:
                f.write(thread)

        # 生成 ML 增强 HTML 报告（必须在 _generate_index_html 前完成，以便 ML 链接自动出现）
        try:
            ml_tickers = self._generate_ml_reports(report)
            if ml_tickers:
                _log.info("ML 增强报告完成：%s", ml_tickers)
                print(f"   ML 报告     : ✅ {', '.join(ml_tickers)}")
        except Exception as e:
            _log.warning("ML 报告批量生成出错: %s", e)

        # 更新 GitHub Pages 仪表板
        try:
            html = self._generate_index_html(report)
            index_file = self.report_dir / "index.html"
            with open(index_file, "w", encoding="utf-8") as f:
                f.write(html)
            _log.info("index.html 已更新（GitHub Pages）")
        except Exception as e:
            _log.warning("index.html 生成失败: %s", e)

        _log.info("报告已保存：%s", md_file.name)

        return str(md_file)

    def _generate_index_html(self, report: Dict) -> str:
        """从 swarm report + .swarm_results_*.json 生成完整 GitHub Pages 仪表板"""
        from datetime import datetime as _dt
        import html as _html
        from pathlib import Path as _Path

        now_str = _dt.now().strftime("%Y-%m-%d %H:%M PST")
        date_str = self.date_str
        opps = report.get("opportunities", [])
        meta = report.get("swarm_metadata", {})
        n_tickers = meta.get("tickers_analyzed", len(opps))
        n_agents = meta.get("total_agents", 7)
        n_resonance = meta.get("resonances_detected", 0)

        # 读取详细 swarm_results（含 IV Rank、P/C Ratio、内幕信号等）
        swarm_detail: Dict = {}
        try:
            sr_path = self.report_dir / f".swarm_results_{date_str}.json"
            if sr_path.exists():
                with open(sr_path) as _f:
                    swarm_detail = json.load(_f)
        except (OSError, json.JSONDecodeError):
            pass

        # 将 opportunities 按 ticker 建立索引，并补充 swarm 详细数据
        opp_by_ticker = {o.get("ticker"): o for o in opps}
        # 若 swarm_detail 有更多 ticker（超过 opportunities 的 5 个），全部纳入
        all_tickers_sorted = [o.get("ticker") for o in opps]
        for t in swarm_detail:
            if t not in all_tickers_sorted:
                all_tickers_sorted.append(t)

        dir_map = {"bullish": ("看多", "bullish", "#28a745"),
                   "bearish": ("看空", "bearish", "#dc3545"),
                   "neutral": ("中性", "neutral", "#ffc107")}

        def sc_cls(score):
            return "sc-h" if score >= 7.0 else ("sc-m" if score >= 5.5 else "sc-l")

        def _detail(ticker):
            """提取单个 ticker 的详细指标"""
            sd = swarm_detail.get(ticker, {})
            ad = sd.get("agent_details", {})
            oracle = ad.get("OracleBeeEcho", {}).get("details", {})
            scout_disc = ad.get("ScoutBeeNova", {}).get("discovery", "")
            bear_score = ad.get("BearBeeContrarian", {}).get("score", 0.0)
            ab = sd.get("agent_breakdown", {})
            iv_rank = oracle.get("iv_rank", None)
            pc = oracle.get("put_call_ratio", None)
            real_pct = sd.get("data_real_pct", None)
            # 内幕信号：取 ScoutBeeNova discovery 第一个 | 段
            insider_hint = scout_disc.split("|")[0].strip() if scout_disc else ""
            # 是否有内幕买入/卖出
            insider_color = "#28a745" if "买入" in insider_hint else ("#dc3545" if "卖出" in insider_hint else "#666")
            return {
                "iv_rank": f"{iv_rank:.1f}" if iv_rank is not None else "-",
                "pc": f"{pc:.2f}" if pc is not None else "-",
                "bear_score": float(bear_score),
                "bullish": ab.get("bullish", 0),
                "bearish_v": ab.get("bearish", 0),
                "neutral_v": ab.get("neutral", 0),
                "insider_hint": _html.escape(insider_hint[:35]) if insider_hint else "",
                "insider_color": insider_color,
                "real_pct": f"{real_pct:.0f}%" if real_pct is not None else "-",
            }

        # 计算 avg real_pct
        real_pcts = [swarm_detail[t].get("data_real_pct", 0) for t in swarm_detail if swarm_detail[t].get("data_real_pct")]
        avg_real = f"{sum(real_pcts)/len(real_pcts):.0f}%" if real_pcts else "-"

        # ── 机会卡片（Top 6）──
        cards_html = ""
        for i, ticker in enumerate(all_tickers_sorted[:6], 1):
            opp = opp_by_ticker.get(ticker, {})
            score = float(opp.get("opp_score") or swarm_detail.get(ticker, {}).get("final_score", 0))
            direction = str(opp.get("direction") or swarm_detail.get(ticker, {}).get("direction", "neutral")).lower()
            if direction not in dir_map:
                direction = "bullish" if "多" in direction else ("bearish" if "空" in direction else "neutral")
            resonance = opp.get("resonance", swarm_detail.get(ticker, {}).get("resonance", {}).get("resonance_detected", False))
            supporting = int(opp.get("supporting_agents") or swarm_detail.get(ticker, {}).get("supporting_agents", 0))
            dir_label, dir_cls, dir_color = dir_map[direction]
            border = " style=\"border-color:#28a745;border-width:2px;\"" if i == 1 else ""
            rank_style = " style=\"background:#28a745;color:white;\"" if i == 1 else ""
            sc = sc_cls(score)
            res_badge = (f'<span class="res-badge res-y">{supporting} Agent 共振</span>'
                         if resonance else '<span class="res-badge res-n">无共振</span>')
            d = _detail(ticker)
            pc_color = ' style="color:#28a745;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) < 0.7 else (
                       ' style="color:#dc3545;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) > 1.5 else "")
            bear_pct = min(100, int(d["bear_score"] * 10))
            insider_row = (f'<div class="mr"><span class="lbl">内幕信号</span>'
                           f'<span class="val" style="color:{d["insider_color"]};">{d["insider_hint"]}</span></div>'
                           if d["insider_hint"] else "")
            ml_link = _Path(self.report_dir / f"alpha-hive-{ticker}-ml-enhanced-{date_str}.html")
            ml_row = (f'<div class="mr"><span class="lbl">ML 报告</span>'
                      f'<span class="val"><a href="alpha-hive-{ticker}-ml-enhanced-{date_str}.html" style="color:#667eea;">查看详情</a></span></div>'
                      if ml_link.exists() else "")
            cards_html += f"""
                <div class="opp-card"{border}>
                    <div class="card-rank"{rank_style}>#{i}</div>
                    <div class="card-hd">
                        <h3>{_html.escape(ticker)}</h3>
                        <div class="dir-badge dir-{dir_cls}">{dir_label}</div>
                    </div>
                    <div class="card-body">
                        <div class="mr"><span class="lbl">综合分</span><span class="val {sc}">{score:.1f}/10</span></div>
                        <div class="mr"><span class="lbl">共振信号</span>{res_badge}</div>
                        <div class="mr"><span class="lbl">投票</span><span class="val">{d['bullish']}多 / {d['bearish_v']}空 / {d['neutral_v']}中</span></div>
                        <div class="mr"><span class="lbl">IV Rank</span><span class="val">{d['iv_rank']}</span></div>
                        <div class="mr"><span class="lbl">P/C Ratio</span><span class="val"{pc_color}>{d['pc']}</span></div>
                        {insider_row}
                        <div class="mr"><span class="lbl">看空强度</span><span class="val">{d['bear_score']:.1f}/10</span></div>
                        <div class="bear-bar"><div class="bear-fill" style="width:{bear_pct}%"></div></div>
                        {ml_row}
                    </div>
                </div>"""

        # ── 完整表格（全部 ticker）──
        rows_html = ""
        for i, ticker in enumerate(all_tickers_sorted, 1):
            opp = opp_by_ticker.get(ticker, {})
            score = float(opp.get("opp_score") or swarm_detail.get(ticker, {}).get("final_score", 0))
            direction = str(opp.get("direction") or swarm_detail.get(ticker, {}).get("direction", "neutral")).lower()
            if direction not in dir_map:
                direction = "bullish" if "多" in direction else ("bearish" if "空" in direction else "neutral")
            resonance = opp.get("resonance", swarm_detail.get(ticker, {}).get("resonance", {}).get("resonance_detected", False))
            supporting = int(opp.get("supporting_agents") or swarm_detail.get(ticker, {}).get("supporting_agents", 0))
            dir_label, _, dir_color = dir_map[direction]
            sc = sc_cls(score)
            d = _detail(ticker)
            res_html = (f'<span class="res-badge res-y">{supporting} Agent</span>'
                        if resonance else '<span class="res-badge res-n">无</span>')
            row_style = " style=\"background:#f0fff0;\"" if i == 1 else ""
            ml_link = _Path(self.report_dir / f"alpha-hive-{ticker}-ml-enhanced-{date_str}.html")
            ml_td = (f'<a href="alpha-hive-{ticker}-ml-enhanced-{date_str}.html" style="color:#667eea;">查看</a>'
                     if ml_link.exists() else "-")
            pc_style = (' style="color:#28a745;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) < 0.7
                        else (' style="color:#dc3545;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) > 1.5 else ""))
            rows_html += f"""
                <tr{row_style}>
                    <td>{i}</td>
                    <td><strong>{_html.escape(ticker)}</strong></td>
                    <td style="color:{dir_color};font-weight:bold;">{dir_label}</td>
                    <td class="{sc}"><strong>{score:.1f}</strong>/10</td>
                    <td>{res_html}</td>
                    <td>{d['bullish']} / {d['bearish_v']} / {d['neutral_v']}</td>
                    <td>{d['iv_rank']}</td>
                    <td{pc_style}>{d['pc']}</td>
                    <td style="color:#fd7e14;">{d['bear_score']:.1f}/10</td>
                    <td>{ml_td}</td>
                </tr>"""

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Alpha Hive - 投资简报仪表板</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
               min-height: 100vh; padding: 20px; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .header {{ background: white; border-radius: 15px; padding: 40px;
                   margin-bottom: 30px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); text-align: center; }}
        .header h1 {{ font-size: 2.5em; color: #667eea; margin-bottom: 10px; }}
        .header p {{ color: #666; font-size: 1.1em; }}
        .header .update-time {{ display: inline-block; margin-top: 12px; padding: 6px 18px;
            background: #f0f0ff; border-radius: 20px; color: #667eea; font-size: 0.95em; font-weight: 500; }}
        .main-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 30px; margin-bottom: 30px; }}
        .section {{ background: white; border-radius: 15px; padding: 30px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }}
        .section h2 {{ color: #667eea; font-size: 1.6em; margin-bottom: 20px;
                       display: flex; align-items: center; gap: 10px; }}
        .section h2::before {{ content: ''; display: inline-block; width: 4px; height: 28px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 2px; }}
        .opp-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
        .opp-card {{ border: 1px solid #e0e0e0; border-radius: 12px; padding: 22px;
            background: linear-gradient(135deg, #f8f9fa 0%, #fff 100%); position: relative;
            transition: transform 0.3s, box-shadow 0.3s; }}
        .opp-card:hover {{ transform: translateY(-5px); box-shadow: 0 15px 35px rgba(102,126,234,0.2); }}
        .card-rank {{ position: absolute; top: 10px; right: 15px; font-size: 0.85em;
            font-weight: bold; color: #667eea; background: #f0f0f0; padding: 4px 8px; border-radius: 5px; }}
        .card-hd {{ display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid #eee; }}
        .card-hd h3 {{ font-size: 1.5em; color: #333; }}
        .dir-badge {{ padding: 4px 14px; border-radius: 20px; color: white; font-size: 0.85em; font-weight: bold; }}
        .dir-bullish {{ background: #28a745; }} .dir-neutral {{ background: #ffc107; color: #333; }}
        .dir-bearish {{ background: #dc3545; }}
        .card-body {{ display: flex; flex-direction: column; gap: 8px; }}
        .mr {{ display: flex; justify-content: space-between; align-items: center; padding: 6px 0; font-size: 0.93em; }}
        .mr .lbl {{ color: #666; font-weight: 500; }} .mr .val {{ color: #333; font-weight: bold; }}
        .sc-h {{ color: #28a745; }} .sc-m {{ color: #fd7e14; }} .sc-l {{ color: #dc3545; }}
        .res-badge {{ display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 0.8em; font-weight: bold; }}
        .res-y {{ background: #d4edda; color: #155724; }} .res-n {{ background: #f8d7da; color: #721c24; }}
        .bear-bar {{ height: 6px; border-radius: 3px; background: #eee; margin-top: 4px; }}
        .bear-fill {{ height: 100%; border-radius: 3px; background: linear-gradient(90deg, #ffc107, #dc3545); }}
        .status-card {{ border: 2px solid #28a745; border-radius: 10px; padding: 20px;
            background: linear-gradient(135deg, rgba(102,126,234,0.05), rgba(118,75,162,0.05)); }}
        .status-hd {{ display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 15px; padding-bottom: 10px; border-bottom: 1px solid #eee; }}
        .status-hd h3 {{ color: #667eea; font-size: 1.2em; }}
        .status-ind {{ display: flex; align-items: center; gap: 8px; font-size: 1.1em; font-weight: bold; color: #28a745; }}
        .status-dot {{ width: 12px; height: 12px; border-radius: 50%; background-color: #28a745; animation: pulse 2s infinite; }}
        @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.5; }} }}
        .si {{ display: flex; flex-direction: column; gap: 10px; font-size: 0.95em; }}
        .sr {{ display: flex; justify-content: space-between; }}
        .sr .sl {{ color: #666; }} .sr .sv {{ color: #333; font-weight: bold; }}
        .full-table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        .full-table th, .full-table td {{ padding: 11px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        .full-table th {{ background: linear-gradient(135deg, #667eea, #764ba2); color: white; font-weight: 600; font-size: 0.88em; }}
        .full-table tr:hover {{ background-color: #f8f9fa; }}
        .full-table td {{ font-size: 0.93em; }}
        .footer {{ text-align: center; color: white; margin-top: 30px; font-size: 0.95em; }}
        .footer p {{ margin: 5px 0; }}
        @media (max-width: 768px) {{
            .main-grid {{ grid-template-columns: 1fr; }}
            .header {{ padding: 20px; }} .header h1 {{ font-size: 1.8em; }}
            .opp-grid {{ grid-template-columns: 1fr; }}
            .full-table {{ font-size: 0.82em; }} .full-table th, .full-table td {{ padding: 8px 6px; }}
        }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Alpha Hive 每日投资简报</h1>
        <p>去中心化蜂群智能投资研究平台 | {n_agents} 个自治工蜂 + 二阶段看空对冲</p>
        <div class="update-time">{now_str} | {n_tickers} 标的扫描 | SEC 真实数据 | 数据真实度 {avg_real}</div>
    </div>
    <div class="main-grid">
        <div class="section">
            <h2>今日 Top 6 机会</h2>
            <div class="opp-grid">{cards_html}
            </div>
        </div>
        <div>
            <div class="section" style="margin-bottom: 30px;">
                <div class="status-card">
                    <div class="status-hd">
                        <h3>系统状态</h3>
                        <div class="status-ind"><div class="status-dot"></div>运行正常</div>
                    </div>
                    <div class="si">
                        <div class="sr"><span class="sl">更新日期</span><span class="sv">{date_str}</span></div>
                        <div class="sr"><span class="sl">最后更新</span><span class="sv">{now_str.split()[1]}</span></div>
                        <div class="sr"><span class="sl">扫描标的</span><span class="sv">{n_tickers} 个</span></div>
                        <div class="sr"><span class="sl">Agent 架构</span><span class="sv">{n_agents} Agent + 看空蜂</span></div>
                        <div class="sr"><span class="sl">共振检测</span><span class="sv" style="color:#28a745;">{n_resonance}/{n_tickers} 标的</span></div>
                        <div class="sr"><span class="sl">数据真实度</span><span class="sv" style="color:#28a745;">{avg_real}</span></div>
                        <div class="sr"><span class="sl">SEC 数据</span><span class="sv" style="color:#28a745;">真实 EDGAR API</span></div>
                    </div>
                </div>
            </div>
            <div class="section">
                <h2>今日报告</h2>
                <div class="reports-list">
                    <div class="report-item">
                        <div class="report-date">{now_str} - 蜂群扫描 ({n_tickers}标的)</div>
                        <div class="report-links">
                            <a href="alpha-hive-daily-{date_str}.md" class="rl md">完整简报</a>
                            <a href="alpha-hive-daily-{date_str}.json" class="rl" style="background:#764ba2;color:white;">JSON</a>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <div class="section" style="margin-bottom: 30px;">
        <h2>完整机会清单</h2>
        <table class="full-table">
            <thead>
                <tr>
                    <th>#</th><th>标的</th><th>方向</th><th>综合分</th><th>共振</th>
                    <th>投票(多/空/中)</th><th>IV Rank</th><th>P/C Ratio</th><th>看空强度</th><th>ML 详情</th>
                </tr>
            </thead>
            <tbody>{rows_html}
            </tbody>
        </table>
    </div>
    <div class="footer">
        <p>Alpha Hive - 完全自动化蜂群智能投资研究平台</p>
        <p>最后更新：{now_str} | {n_tickers} 标的蜂群扫描 | SEC 真实数据 | 数据真实度 {avg_real}</p>
        <p style="font-size:0.9em;margin-top:10px;opacity:0.8;">
            声明：本报告为 AI 蜂群自动生成，仅供参考，不构成投资建议。预测存在误差，所有交易决策需自行判断和风控。
        </p>
    </div>
</div>
</body>
</html>"""


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

  # 蜂群协作模式（7 个自治工蜂：6 核心 + BearBeeContrarian）
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
        help='启用蜂群协作模式（7 个自治工蜂：6 核心并行 + BearBeeContrarian 看空对冲）'
    )
    parser.add_argument(
        '--check-earnings',
        action='store_true',
        help='检查今日财报并自动更新简报（可单独运行，不需要重新扫描）'
    )

    args = parser.parse_args()

    # 创建报告生成器
    reporter = AlphaHiveDailyReporter()

    # 如果只是检查财报更新
    if args.check_earnings:
        focus_tickers = list(WATCHLIST.keys())[:10] if args.all_watchlist else args.tickers
        result = reporter.check_earnings_updates(tickers=focus_tickers)
        reporting = result.get("reporting_today", [])
        updated = result.get("updated", [])
        if reporting:
            _log.info("今日财报: %s | 已更新: %s", reporting, updated)
        else:
            _log.info("今日无 watchlist 标的发布财报")
        return result

    # 确定扫描标的
    focus_tickers = list(WATCHLIST.keys())[:10] if args.all_watchlist else args.tickers

    if args.swarm:
        report = reporter.run_swarm_scan(focus_tickers=focus_tickers)
    else:
        report = reporter.run_daily_scan(focus_tickers=focus_tickers)

    # 保存报告（Hive app 通过 .swarm_results_{date}.json 自动同步）
    report_path = reporter.save_report(report)
    _log.info("报告已保存：%s", report_path)

    # 三端同步：GitHub 提交推送 + Hive App + Slack 下午2点（温哥华 PST）
    print("\n📡 同步三端：GitHub / Hive App / Slack...")
    try:
        sync_results = reporter.auto_commit_and_notify(report)
        git_ok = sync_results.get("git_push", {}).get("success", False)
        print(f"   GitHub push : {'✅' if git_ok else '⚠️  失败（见日志）'}")
        print(f"   Hive App    : ✅ .swarm_results 已落盘，下次启动自动加载")
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        _log.warning("三端同步部分失败: %s", e)
        print(f"   ⚠️  三端同步出错：{e}")

    return report


if __name__ == "__main__":
    main()
