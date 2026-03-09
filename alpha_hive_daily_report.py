#!/usr/bin/env python3
"""
🐝 Alpha Hive 日报生成器 - 集成期权分析的完整版本
每日自动扫描 watchlist 并生成结构化投资简报 + X 线程版本
"""

import json
import argparse
import os
import time
from datetime import datetime
from typing import Dict, List, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# 导入现有模块
from config import WATCHLIST
from hive_logger import get_logger, PATHS, set_correlation_id, SafeJSONEncoder, optional_import

_log = get_logger("daily_report")

# 可选模块（optional_import 优雅降级）
MetricsCollector = optional_import("metrics_collector", "MetricsCollector")
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

MemoryStore = optional_import("memory_store", "MemoryStore")
CalendarIntegrator = optional_import("calendar_integrator", "CalendarIntegrator")

# Phase 3 P4: Code Execution Agent（含 fallback dict，保留 try/except）
try:
    from code_executor_agent import CodeExecutorAgent
    from config import CODE_EXECUTION_CONFIG
except ImportError:
    CodeExecutorAgent = None
    CODE_EXECUTION_CONFIG = {"enabled": False}

# Phase 3 P5: CrewAI 多 Agent 框架（含错误日志，保留 try/except）
try:
    from crewai_adapter import AlphaHiveCrew
    from config import CREWAI_CONFIG
except (ImportError, TypeError) as e:
    AlphaHiveCrew = None
    CREWAI_CONFIG = {"enabled": False}
    _log.info("CrewAI 模块导入失败: %s (降级到原始蜂群)", type(e).__name__)

SlackReportNotifier = optional_import("slack_report_notifier", "SlackReportNotifier")
EarningsWatcher = optional_import("earnings_watcher", "EarningsWatcher")

# Phase 3 内存优化: 向量记忆层（含 fallback dict，保留 try/except）
try:
    from vector_memory import VectorMemory
    from config import VECTOR_MEMORY_CONFIG
except ImportError:
    VectorMemory = None
    VECTOR_MEMORY_CONFIG = {"enabled": False}

Backtester = optional_import("backtester", "Backtester")
run_full_backtest = optional_import("backtester", "run_full_backtest")


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


@dataclass
class _SwarmContext:
    """run_swarm_scan 的跨阶段共享状态"""
    targets: List[str]
    board: object  # PheromoneBoard
    phase1_agents: list
    guard_agent: object
    bear_agent: object
    queen: object
    all_agents: list
    prefetch_elapsed: float
    start_time: float
    checkpoint_file: object = None  # Path


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

        # Phase 2: 共享线程池（替代所有 daemon 线程，退出时等待完成）
        import atexit
        self._bg_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="hive_bg")
        self._bg_futures = []
        atexit.register(self._shutdown_bg)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._shutdown_bg()

    def _shutdown_bg(self) -> None:
        """atexit 处理器：等待后台任务完成"""
        from concurrent.futures import TimeoutError as FuturesTimeout, CancelledError
        for f in self._bg_futures:
            try:
                f.result(timeout=10)
            except (FuturesTimeout, CancelledError, OSError, RuntimeError) as e:
                _log.debug("Background task cleanup: %s", e)
        self._bg_executor.shutdown(wait=True)

    def _submit_bg(self, fn, *args, **kwargs) -> None:
        """提交后台任务到共享线程池（替代 daemon 线程）"""
        # 清理已完成的 futures（防止内存泄漏）
        self._bg_futures = [f for f in self._bg_futures if not f.done()]
        # U6: 超限保护 — 阻塞等待最旧 future 完成
        if len(self._bg_futures) >= 20:
            try:
                self._bg_futures[0].result(timeout=15)
            except Exception as _e_bg:
                self.logger.debug("后台任务等待超时: %s", _e_bg)
            self._bg_futures = [f for f in self._bg_futures if not f.done()]
        future = self._bg_executor.submit(fn, *args, **kwargs)
        self._bg_futures.append(future)

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

        with ThreadPoolExecutor(max_workers=min(len(targets), 6)) as executor:
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

    # ── D4: 部署后 CDN 验证 ──
    _DEPLOY_BASE_URL = "https://wangmingjie36-creator.github.io/alpha-hive-deploy"

    def _verify_cdn_deployment(self, *args, **kwargs):
        """验证 CDN 部署（委托 report_deployer）"""
        from report_deployer import verify_cdn_deployment
        return verify_cdn_deployment(self, *args, **kwargs)

    def _deploy_static_to_ghpages(self, *args, **kwargs):
        """部署静态文件到 gh-pages（委托 report_deployer）"""
        from report_deployer import deploy_static_to_ghpages
        return deploy_static_to_ghpages(self, *args, **kwargs)

    # ── Step 4: run_swarm_scan 拆分方法 ──

    def _init_scan_context(self, focus_tickers) -> '_SwarmContext':
        """初始化蜂群扫描上下文：Board + Agents + 预取数据"""
        set_correlation_id(self._session_id or f"swarm_{self.date_str}")
        _log.info("蜂群协作启动 %s", self.date_str)
        try:
            from hive_logger import FeatureRegistry
            FeatureRegistry.log_status()
        except ImportError:
            pass

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        _log.info("标的：%s", " ".join(targets))
        start_time = time.time()

        board = PheromoneBoard(memory_store=self.memory_store, session_id=self._session_id)
        retriever = self.vector_memory if (self.vector_memory and self.vector_memory.enabled) else None
        phase1_agents = [
            ScoutBeeNova(board, retriever=retriever),
            OracleBeeEcho(board, retriever=retriever),
            BuzzBeeWhisper(board, retriever=retriever),
            ChronosBeeHorizon(board, retriever=retriever),
            RivalBeeVanguard(board, retriever=retriever),
        ]
        guard_agent = GuardBeeSentinel(board, retriever=retriever)
        bear_agent = BearBeeContrarian(board, retriever=retriever)

        if self.code_executor_agent and CODE_EXECUTION_CONFIG.get("add_to_swarm"):
            self.code_executor_agent.board = board
            phase1_agents.append(self.code_executor_agent)

        adapted_w = Backtester.load_adapted_weights() if Backtester else None
        import llm_service as _llm_check_q
        queen = QueenDistiller(board, adapted_weights=adapted_w, enable_llm=_llm_check_q.is_available())

        all_agents = phase1_agents + [guard_agent, bear_agent]
        _log.info("%d Agent（Phase1 %d + Guard + Bear）| 预取数据中...", len(all_agents), len(phase1_agents))

        prefetched = prefetch_shared_data(targets, retriever)
        inject_prefetched(all_agents, prefetched)
        prefetch_elapsed = time.time() - start_time
        _log.info("预取完成 (%.1fs) | 开始并行分析", prefetch_elapsed)

        checkpoint_file = self.report_dir / f".checkpoint_{self._session_id or 'default'}.json"

        return _SwarmContext(
            targets=targets, board=board,
            phase1_agents=phase1_agents, guard_agent=guard_agent,
            bear_agent=bear_agent, queen=queen, all_agents=all_agents,
            prefetch_elapsed=prefetch_elapsed, start_time=start_time,
            checkpoint_file=checkpoint_file,
        )

    def _load_checkpoint(self, ctx: '_SwarmContext'):
        """加载崩溃恢复 checkpoint，返回 (swarm_results, completed_tickers)"""
        swarm_results = {}
        completed_tickers = set()
        if ctx.checkpoint_file and ctx.checkpoint_file.exists():
            try:
                with open(ctx.checkpoint_file, "r") as f:
                    ckpt = json.load(f)
                    swarm_results = ckpt.get("results", {})
                    saved_date = ckpt.get("saved_at", "")
                    today_date = datetime.now().strftime("%Y-%m-%d")
                    if saved_date and saved_date != today_date:
                        _log.warning("Checkpoint 已过期 (saved: %s, today: %s)，重新开始",
                                     saved_date, today_date)
                        swarm_results = {}
                    else:
                        completed_tickers = set(swarm_results.keys())
                        if completed_tickers:
                            _log.info("恢复 checkpoint：%d 标的已完成", len(completed_tickers))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                _log.warning("Checkpoint 恢复失败，重新开始: %s", e)
                swarm_results = {}
                completed_tickers = set()
        return swarm_results, completed_tickers

    def _analyze_single_ticker(self, ctx: '_SwarmContext', ticker: str,
                               idx: int, total: int, progress_callback=None):
        """单标的全流程分析：有效性检测 → Phase1 并行 → Guard → Bear → Queen distill"""
        # Ticker 有效性检测
        try:
            from swarm_agents import check_ticker_validity
            _validity = check_ticker_validity(ticker)
            if not _validity["valid"]:
                _log.warning("[%d/%d] ⏭️ 跳过 %s（%s）", idx, total, ticker, _validity["warning"])
                return None
            if _validity.get("warning"):
                _log.warning("[%d/%d] ⚠️ %s 异常：%s", idx, total, ticker, _validity["warning"])
        except Exception as _ve:
            _log.debug("ticker validity check error for %s: %s", ticker, _ve)

        # Phase 1: 并行分析
        with ThreadPoolExecutor(max_workers=len(ctx.phase1_agents)) as executor:
            futures = {executor.submit(agent.analyze, ticker): agent for agent in ctx.phase1_agents}
            agent_results = []
            for future in as_completed(futures):
                try:
                    agent_results.append(future.result(timeout=60))
                except (TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as e:
                    _log.warning("Agent future failed: %s", e)
                    agent_results.append(None)

        # Phase 1.5: GuardBeeSentinel 交叉验证
        try:
            guard_result = ctx.guard_agent.analyze(ticker)
            agent_results.append(guard_result)
            _log.info("  🛡️ 验证蜂: %s %s (%.1f分)",
                      ticker, guard_result.get("direction", "?"),
                      guard_result.get("score", 5.0))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            _log.warning("GuardBeeSentinel failed for %s: %s", ticker, e)
            agent_results.append(None)

        # Phase 2: BearBeeContrarian 看空对冲
        try:
            bear_result = ctx.bear_agent.analyze(ticker)
            agent_results.append(bear_result)
            _log.info("  🐻 看空蜂: %s %s (%.1f分, %d信号)",
                      ticker, bear_result.get("direction", "?"),
                      bear_result.get("details", {}).get("bear_score", 0),
                      len(bear_result.get("details", {}).get("bearish_signals", [])))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            _log.warning("BearBeeContrarian failed for %s: %s", ticker, e)
            agent_results.append(None)

        distilled = ctx.queen.distill(ticker, agent_results)

        res = "✅" if distilled["resonance"]["resonance_detected"] else "—"
        _log.info("[%d/%d] %s: %.1f/10 %s %s", idx, total, ticker, distilled['final_score'], distilled['direction'], res)

        # 进度回调
        if progress_callback:
            try:
                progress_callback(idx, total, ticker, distilled)
            except Exception as _cb_err:
                _log.debug("Progress callback error: %s", _cb_err)

        # 写入 checkpoint
        # Note: swarm_results 在编排器中更新后才写 checkpoint，这里仅返回 distilled
        return distilled

    def _post_scan_enrichment(self, ctx: '_SwarmContext', swarm_results: Dict) -> float:
        """扫描后增强：历史类比 + 保存蜂群结果 + 清理 checkpoint，返回 elapsed"""
        # 历史类比推理（top-3 ticker）
        try:
            if ctx.queen.enable_llm and self.vector_memory and self.memory_store:
                sorted_tickers = sorted(
                    swarm_results.keys(),
                    key=lambda t: swarm_results[t].get("final_score", 0),
                    reverse=True,
                )[:3]
                for tk in sorted_tickers:
                    try:
                        ctx.queen.enrich_with_historical_analogy(
                            ticker=tk, distilled=swarm_results[tk],
                            vector_memory=self.vector_memory, memory_store=self.memory_store,
                        )
                        ha = swarm_results[tk].get("historical_analogy")
                        if ha and ha.get("analogy_found"):
                            _log.info("历史类比 %s: %s (相似度 %.2f)",
                                      tk, ha.get("analogy_summary", "")[:60],
                                      ha.get("similarity_score", 0))
                    except Exception as _ha_err:
                        _log.warning("历史类比 enrichment 失败 (%s): %s", tk, _ha_err)
        except Exception as _ha_outer:
            _log.warning("历史类比整体跳过: %s", _ha_outer)

        # 保存蜂群结果（合并当日已有结果）
        try:
            swarm_json = self.report_dir / f".swarm_results_{self.date_str}.json"
            merged_swarm = {}
            if swarm_json.exists():
                try:
                    with open(swarm_json, encoding="utf-8") as _f:
                        merged_swarm = json.load(_f)
                except (OSError, json.JSONDecodeError):
                    pass
            merged_swarm.update(swarm_results)
            with open(swarm_json, "w") as f:
                json.dump(merged_swarm, f, cls=SafeJSONEncoder, ensure_ascii=False)
        except (OSError, TypeError) as e:
            _log.warning("Swarm results 保存失败: %s", e)

        # 清理 checkpoint
        try:
            ctx.checkpoint_file.unlink(missing_ok=True)
        except OSError as e:
            _log.debug("Checkpoint 清理失败: %s", e)

        return time.time() - ctx.start_time

    def _post_scan_metrics(self, ctx: '_SwarmContext', swarm_results: Dict, elapsed: float) -> None:
        """扫描后指标：LLM 统计 + MetricsCollector + SLO 检查 + 回测 + 权重自适应 + DB 清理"""
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

        # MetricsCollector 记录 + SLO
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
                    agent_count=len(ctx.all_agents),
                    prefetch_seconds=ctx.prefetch_elapsed,
                    avg_score=sum(scores) / len(scores) if scores else 5.0,
                    max_score=max(scores) if scores else 5.0,
                    min_score=min(scores) if scores else 5.0,
                    agent_errors=agent_errors,
                    agent_total=len(swarm_results) * len(ctx.all_agents),
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

                violations = self.metrics.check_slo(days=1)
                if violations:
                    _vio_text = "; ".join(v["details"] for v in violations)
                    _log.warning("SLO 违规 %d 条: %s", len(violations), _vio_text)
                    if self.slack_notifier:
                        try:
                            _n = len(violations)
                            _sev = "CRITICAL" if _n > 3 else ("HIGH" if _n > 1 else "MEDIUM")
                            _body = f"最近 24h 检测到 {_n} 条 SLO 违规：\n"
                            for _v in violations:
                                _body += f"  - [{_v['slo_name']}] {_v['details']}\n"
                            self.slack_notifier.send_risk_alert(
                                alert_title=f"SLO 违规告警（{_n} 条）",
                                alert_message=_body,
                                severity=_sev,
                            )
                        except (OSError, ValueError, ConnectionError) as _se:
                            _log.warning("SLO Slack 告警发送失败: %s", _se)
            except (OSError, ValueError, KeyError, TypeError) as e:
                _log.warning("指标收集异常: %s", e)

        # 回测反馈循环
        adapted = None
        if Backtester:
            try:
                bt = Backtester()
                bt.save_predictions(swarm_results)
                bt.run_backtest()
                adapted = bt.adapt_weights(min_samples=10, period="t7")
                if adapted is None:
                    adapted = bt.adapt_weights(min_samples=5, period="t1")
                    if adapted:
                        _log.info("自适应权重：T+7 样本不足，使用 T+1 数据（保守调整）")
            except (OSError, ValueError, KeyError, TypeError) as e:
                _log.warning("回测异常: %s", e)

        if adapted and self.slack_notifier and self.slack_notifier.enabled:
            try:
                weight_lines = " | ".join(f"{k}: {v:.3f}" for k, v in adapted.items())
                self._submit_bg(
                    self.slack_notifier.send_risk_alert,
                    alert_title="权重自适应更新",
                    alert_message=f"回测反馈已更新 5 维权重：\n{weight_lines}",
                    severity="info",
                )
            except (OSError, ValueError, ConnectionError) as e:
                _log.debug("权重通知发送失败: %s", e)

        # 数据库清理
        if self.memory_store:
            try:
                self.memory_store.cleanup_old_data(180)
            except Exception as e:
                _log.debug("记忆清理失败: %s", e)
        if self.vector_memory and getattr(self.vector_memory, "enabled", False):
            try:
                self.vector_memory.cleanup(days=90)
            except Exception as e:
                _log.debug("向量记忆清理失败: %s", e)
        if Backtester:
            try:
                Backtester().cleanup_old_predictions(180)
            except Exception as e:
                _log.debug("预测清理失败: %s", e)

    def _post_scan_notify(self, ctx: '_SwarmContext', swarm_results: Dict,
                          report: Dict, elapsed: float) -> None:
        """扫描后通知：Slack推送 + 失效条件 + 日历 + 会话存储 + 向量记忆 + 反馈循环"""
        # Slack 推送高分机会 + 异常信号
        if self.slack_notifier and self.slack_notifier.enabled:
            for ticker, data in swarm_results.items():
                score = data.get("final_score", 0)
                direction = data.get("direction", "neutral")
                dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(direction, direction)
                adj_note = self._format_score_adjustments(data)
                details_list = [f"评分 {score:.1f}/10"]
                if adj_note:
                    details_list.append(adj_note)
                cov = data.get("dimension_coverage_pct", 100.0)
                if cov < 100.0:
                    details_list.append(f"维度覆盖 {cov:.0f}%")
                if score >= 7.5:
                    self._submit_bg(
                        self.slack_notifier.send_opportunity_alert,
                        ticker, score, dir_cn,
                        data.get("discovery", "高分机会"), details_list
                    )
                elif score <= 3.0:
                    self._submit_bg(
                        self.slack_notifier.send_risk_alert,
                        f"{ticker} 低分预警",
                        f"蜂群评分仅 {score:.1f}/10，方向 {dir_cn}" + (f" | {adj_note}" if adj_note else ""),
                        "HIGH"
                    )

        # 失效条件快照
        try:
            from thesis_breaks import ThesisBreakConfig
            for _opp in report.get("opportunities", []):
                _tk = _opp.get("ticker", "")
                _tb_cfg = ThesisBreakConfig.get_breaks_config(_tk)
                if _tb_cfg:
                    _l1 = [c["metric"] + "：" + c["trigger"]
                           for c in _tb_cfg.get("level_1_warning", {}).get("conditions", [])]
                    _l2 = [c["metric"] + "：" + c["trigger"]
                           for c in _tb_cfg.get("level_2_stop_loss", {}).get("conditions", [])]
                    _opp["thesis_break_l1"] = _l1
                    _opp["thesis_break_l2"] = _l2
                    if _tk in swarm_results:
                        swarm_results[_tk]["thesis_break_l1"] = _l1
                        swarm_results[_tk]["thesis_break_l2"] = _l2
        except Exception as _tbe:
            _log.warning("thesis_break 配置加载失败: %s", _tbe)

        # 日历提醒
        if self.calendar and report.get('opportunities'):
            for opp in report['opportunities']:
                _opp_score = opp.get("opp_score", 0) if isinstance(opp, dict) else getattr(opp, "opportunity_score", 0)
                if _opp_score >= 7.5:
                    _tk = opp.get("ticker", "") if isinstance(opp, dict) else getattr(opp, "ticker", "")
                    _dir = opp.get("direction", "") if isinstance(opp, dict) else getattr(opp, "direction", "")
                    self._submit_bg(self.calendar.add_opportunity_reminder, _tk, _opp_score, _dir, "高分机会")

        # 保存会话
        if self.memory_store and self._session_id:
            snapshot = ctx.board.compact_snapshot()
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "swarm",
                ctx.targets, swarm_results, snapshot, elapsed
            )

        # 向量记忆存储
        if self.vector_memory and self.vector_memory.enabled:
            stored = 0
            for ticker, data in swarm_results.items():
                if data.get("final_score", 0) >= 5.0:
                    self.vector_memory.store(
                        ticker=ticker, agent_id="QueenDistiller",
                        discovery=f"评分{data['final_score']:.1f} {data['direction']} "
                                  f"支持{data.get('supporting_agents', 0)}Agent",
                        direction=data["direction"], score=data["final_score"],
                        source="swarm_scan", session_id=self._session_id or ""
                    )
                    stored += 1
            for entry in ctx.board.snapshot():
                if entry.get("self_score", 0) >= 6.0:
                    self.vector_memory.store(
                        ticker=entry.get("ticker", ""), agent_id=entry.get("agent_id", ""),
                        discovery=entry.get("discovery", "")[:300],
                        direction=entry.get("direction", "neutral"),
                        score=entry.get("self_score", 5.0),
                        source=entry.get("source", ""), session_id=self._session_id or ""
                    )
                    stored += 1
            if stored > 0:
                _log.info("已存入 %d 条长期记忆 (Chroma)", stored)

        # Slack 重试
        if self.slack_notifier and self.slack_notifier.enabled:
            try:
                retried = self.slack_notifier.retry_failed()
                if retried:
                    _log.info("Slack 重试成功 %d 条", retried)
            except Exception as e:
                _log.debug("Slack 重试失败: %s", e)

        # 反馈循环快照
        try:
            from feedback_loop import ReportSnapshot
            import yfinance as _yf_fb
            _snap_dir = os.path.join(str(self.report_dir), "report_snapshots")
            _snap_count = 0
            for _tk, _data in swarm_results.items():
                if _data.get("final_score", 0) >= 5.0:
                    _snap = ReportSnapshot(_tk, self.date_str)
                    _snap.composite_score = _data.get("final_score", 0.0)
                    _snap.direction = _data.get("direction", "Neutral")
                    _snap.agent_votes = {
                        e.get("agent_id", ""): e.get("self_score", 5.0)
                        for e in ctx.board.snapshot()
                        if e.get("ticker") == _tk
                    }
                    try:
                        _hist = _yf_fb.Ticker(_tk).history(period="1d")
                        if not _hist.empty:
                            _snap.entry_price = float(_hist["Close"].iloc[-1])
                    except Exception as _e_price:
                        self.logger.debug("Snapshot 入场价获取失败 (%s): %s", _tk, _e_price)
                    _snap.save_to_json(_snap_dir)
                    _snap_count += 1
            if _snap_count:
                _log.info("反馈循环: 已保存 %d 个标的快照", _snap_count)
        except Exception as e:
            _log.debug("反馈循环保存失败(非致命): %s", e)

    def run_swarm_scan(self, focus_tickers: List[str] = None, progress_callback=None) -> Dict:
        """
        真正的蜂群协作扫描 - 7 个自治工蜂并行运行（6 核心 + BearBeeContrarian），实时通过信息素板交换发现

        Args:
            focus_tickers: 重点关注标的（如为None则扫描全部watchlist）

        Returns:
            完整的蜂群分析报告
        """
        ctx = self._init_scan_context(focus_tickers)
        swarm_results, completed_tickers = self._load_checkpoint(ctx)

        for idx, ticker in enumerate(ctx.targets, 1):
            if ticker in completed_tickers:
                res = "✅" if swarm_results[ticker]["resonance"]["resonance_detected"] else "—"
                _log.info("[%d/%d] %s: %.1f/10 (已缓存) %s",
                          idx, len(ctx.targets), ticker, swarm_results[ticker]['final_score'], res)
                continue

            distilled = self._analyze_single_ticker(ctx, ticker, idx, len(ctx.targets), progress_callback)
            if distilled:
                swarm_results[ticker] = distilled
                # 写入 checkpoint（每个 ticker 完成后）
                try:
                    with open(ctx.checkpoint_file, "w") as f:
                        json.dump({"results": swarm_results, "targets": ctx.targets,
                                  "saved_at": datetime.now().strftime("%Y-%m-%d")}, f, cls=SafeJSONEncoder)
                except (OSError, TypeError) as e:
                    _log.warning("Checkpoint 写入失败: %s", e)

        elapsed = self._post_scan_enrichment(ctx, swarm_results)
        self._post_scan_metrics(ctx, swarm_results, elapsed)
        report = self._build_swarm_report(swarm_results, ctx.board, agent_count=len(ctx.all_agents))
        self._post_scan_notify(ctx, swarm_results, report, elapsed)
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

    # ── _build_swarm_report helper methods ──────────────────────────

    def _build_opportunity_items(self, sorted_results):
        """构建 OpportunityItem 列表（使用 QueenDistiller 真实维度分数）"""
        opportunities = []
        for ticker, swarm_data in sorted_results:
            _dim = swarm_data.get("dimension_scores", {})
            _final = swarm_data["final_score"]

            # 从 agent_details 提取真实催化剂、风险、thesis_break
            _details = swarm_data.get("agent_details", {})
            _catalysts = []
            _risks = []
            _thesis_break = ""
            for _agent_id, _ad in _details.items():
                if "Chronos" in _agent_id and isinstance(_ad, dict):
                    _cat_disc = _ad.get("discovery", "")
                    if _cat_disc and _cat_disc != "未发现显著看空信号":
                        _catalysts.append(_cat_disc[:60])
                if "Bear" in _agent_id and isinstance(_ad, dict):
                    _bear_disc = _ad.get("discovery", "")
                    if _bear_disc and _bear_disc != "未发现显著看空信号":
                        for _seg in _bear_disc.split(" | ")[:3]:
                            if _seg.strip():
                                _risks.append(_seg.strip()[:50])
                if "Guard" in _agent_id and isinstance(_ad, dict):
                    _guard_disc = _ad.get("discovery", "")
                    if _guard_disc:
                        _thesis_break = _guard_disc[:80]

            if not _catalysts:
                _catalysts = ["多 Agent 共振信号"] if swarm_data["resonance"]["resonance_detected"] else ["待验证"]
            if not _risks:
                _risks = ["多头拥挤"] if swarm_data["resonance"]["resonance_detected"] else []
            if not _thesis_break:
                _thesis_break = "信号分散"

            # 真实置信度：从 dimension_confidence 平均值计算
            _dim_conf = swarm_data.get("dimension_confidence", {})
            if _dim_conf:
                _avg_conf = sum(_dim_conf.values()) / len(_dim_conf)
                _confidence = min(95, max(30, round(_avg_conf * 100)))
            else:
                _confidence = min(95, _final * 10) if _final >= 7.5 else 60

            opp = OpportunityItem(
                ticker=ticker,
                direction="看多" if swarm_data["direction"] == "bullish" else (
                    "看空" if swarm_data["direction"] == "bearish" else "中性"
                ),
                signal_score=round(_dim.get("signal", _final), 2),
                catalyst_score=round(_dim.get("catalyst", _final * 0.9), 2),
                sentiment_score=round(_dim.get("sentiment", _final * 0.85), 2),
                odds_score=round(_dim.get("odds", _final * 0.8), 2),
                risk_score=round(_dim.get("risk_adj", _final * 0.95), 2),
                options_score=round(_dim.get("odds", _final * 0.88), 2),
                opportunity_score=_final,
                confidence=_confidence,
                key_catalysts=_catalysts,
                options_signal=f"共振信号 ({swarm_data['resonance']['supporting_agents']} Agent)",
                risks=_risks,
                thesis_break=_thesis_break,
            )
            opportunities.append(opp)
        return opportunities

    def _compute_sector_sentiment(self, swarm_results):
        """P4a: 跨标的情绪传染网络（纯规则引擎，零 API 费用）— mutates swarm_results"""
        sector_sentiment_summary = {}
        try:
            from config import WATCHLIST, SENTIMENT_MOMENTUM_CONFIG as _SMC
            _deviation_high = _SMC.get("sector_deviation_high", 15)
            _deviation_mid = _SMC.get("sector_deviation_mid", 8)

            # Step 1: 提取每个 ticker 的 BuzzBeeWhisper 情绪百分比
            _sector_sentiments: dict[str, list[tuple[str, float]]] = {}
            for _tk, _sd in swarm_results.items():
                _wl = WATCHLIST.get(_tk, {})
                _sector = _wl.get("sector", "Other") if isinstance(_wl, dict) else "Other"
                _buzz_pct = None
                for _aid, _ad in (_sd.get("agent_details") or {}).items():
                    if "Buzz" in _aid and isinstance(_ad, dict):
                        _det = _ad.get("details", {})
                        if isinstance(_det, dict):
                            _buzz_pct = _det.get("sentiment_pct")
                        break
                if _buzz_pct is None:
                    _dim_s = _sd.get("dimension_scores", {}).get("sentiment")
                    if _dim_s is not None:
                        _buzz_pct = _dim_s * 10.0
                if _buzz_pct is not None:
                    _sector_sentiments.setdefault(_sector, []).append((_tk, float(_buzz_pct)))

            # Step 2: 板块平均情绪（≥2 个标的才有意义）
            _sector_avgs: dict[str, float] = {}
            for _sec, _items in _sector_sentiments.items():
                if len(_items) >= 2:
                    _avg = sum(_p for _, _p in _items) / len(_items)
                    _sector_avgs[_sec] = round(_avg, 1)

            # Step 3: 计算个股偏离 & 注入 swarm_results
            for _tk, _sd in swarm_results.items():
                _wl = WATCHLIST.get(_tk, {})
                _sector = _wl.get("sector", "Other") if isinstance(_wl, dict) else "Other"
                if _sector not in _sector_avgs:
                    continue
                _tk_pct = None
                for _t, _p in _sector_sentiments.get(_sector, []):
                    if _t == _tk:
                        _tk_pct = _p
                        break
                if _tk_pct is None:
                    continue
                _dev = round(_tk_pct - _sector_avgs[_sector], 1)
                if _dev > _deviation_high:
                    _sig = "overheating"
                elif _dev > _deviation_mid:
                    _sig = "above_sector"
                elif _dev < -_deviation_high:
                    _sig = "undervalued"
                elif _dev < -_deviation_mid:
                    _sig = "below_sector"
                else:
                    _sig = "in_line"
                _sd["sector_sentiment"] = {
                    "sector": _sector,
                    "sector_avg_pct": _sector_avgs[_sector],
                    "ticker_pct": round(_tk_pct, 1),
                    "deviation_ppt": _dev,
                    "signal": _sig,
                    "peers": [_t for _t, _ in _sector_sentiments[_sector] if _t != _tk],
                }

            sector_sentiment_summary = {
                sec: {"avg_pct": avg, "count": len(_sector_sentiments.get(sec, []))}
                for sec, avg in _sector_avgs.items()
            }
            if _sector_avgs:
                _log.info("P4a 跨标的情绪传染：板块均值 %s", _sector_avgs)
        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.debug("P4a 情绪传染网络不可用: %s", e)
        return sector_sentiment_summary

    def _compute_cross_ticker(self, swarm_results, board):
        """P4b: 跨标的关联分析（LLM）— mutates swarm_results"""
        cross_ticker_analysis = {}
        try:
            from config import WATCHLIST
            import llm_service as _llm_ct
            use_llm_flag = _llm_ct.is_available() or any(
                r.get("distill_mode") == "llm_enhanced" for r in swarm_results.values()
            )
            if use_llm_flag and len(swarm_results) >= 2:
                import llm_service
                sector_map = {}
                for tk in swarm_results:
                    wl_entry = WATCHLIST.get(tk, {})
                    sector_map[tk] = wl_entry.get("sector", "Other") if isinstance(wl_entry, dict) else "Other"
                distilled_scores = {}
                for tk, data in swarm_results.items():
                    distilled_scores[tk] = {
                        "final_score": data.get("final_score", 5.0),
                        "direction": data.get("direction", "neutral"),
                    }
                board_snap = board.compact_snapshot() if board else []
                cross_ticker_analysis = llm_service.analyze_cross_ticker_patterns(
                    board_snapshot=board_snap,
                    distilled_scores=distilled_scores,
                    sector_map=sector_map,
                ) or {}
                if cross_ticker_analysis:
                    _log.info("P4b 跨标的关联分析：%s",
                              cross_ticker_analysis.get("sector_rotation_signal", "N/A"))
                    for insight in cross_ticker_analysis.get("cross_ticker_insights", []):
                        for tk in insight.get("tickers", []):
                            if tk in swarm_results:
                                existing = swarm_results[tk].get("cross_ticker_insights", [])
                                existing.append(insight)
                                swarm_results[tk]["cross_ticker_insights"] = existing
                    for tk in swarm_results:
                        sec = sector_map.get(tk, "Other")
                        mom = cross_ticker_analysis.get("sector_momentum", {}).get(sec)
                        if mom:
                            swarm_results[tk]["sector_momentum"] = mom
        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.debug("P4b 跨标的关联分析不可用: %s", e)
        return cross_ticker_analysis

    def _fetch_report_context(self, swarm_results):
        """P4 集中度 + P5 宏观 + P3 回测 — 三个独立数据源"""
        concentration = {}
        try:
            from portfolio_concentration import analyze_concentration
            from config import WATCHLIST
            concentration = analyze_concentration(swarm_results, WATCHLIST)
            _log.info("P4 集中度分析：%s（风险=%s）",
                      concentration.get("summary", ""), concentration.get("concentration_risk", ""))
        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.debug("P4 portfolio_concentration 不可用: %s", e)

        macro_snapshot = {}
        try:
            from fred_macro import get_macro_context
            macro_snapshot = get_macro_context()
            _log.info("P5 宏观环境：%s", macro_snapshot.get("summary", ""))
        except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
            _log.debug("P5 fred_macro 不可用: %s", e)

        backtest_stats = {}
        try:
            if Backtester:
                _bt = Backtester()
                backtest_stats = _bt.store.get_accuracy_stats("t7", days=30)
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log.debug("Backtest stats unavailable: %s", e)

        return concentration, macro_snapshot, backtest_stats

    def _build_swarm_report(self, swarm_results: Dict, board: PheromoneBoard,
                            agent_count: int = 7) -> Dict:
        """将蜂群分析结果转换为标准报告格式"""
        sorted_results = sorted(
            swarm_results.items(),
            key=lambda x: x[1]["final_score"],
            reverse=True
        )

        self.opportunities = self._build_opportunity_items(sorted_results)
        sector_sentiment_summary = self._compute_sector_sentiment(swarm_results)
        cross_ticker_analysis = self._compute_cross_ticker(swarm_results, board)
        concentration, macro_snapshot, backtest_stats = self._fetch_report_context(swarm_results)

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
            "cross_ticker_analysis": cross_ticker_analysis,
            "sector_sentiment_contagion": sector_sentiment_summary,
            "macro_context": macro_snapshot,
            "backtest_stats": backtest_stats,
            "markdown_report": self._generate_swarm_markdown_report(swarm_results, concentration, macro_snapshot, backtest_stats, agent_count=agent_count, cross_ticker=cross_ticker_analysis),
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
                for i, opp in enumerate(self.opportunities)
            ]
        }

        return report

    @staticmethod
    def _format_score_adjustments(*args, **kwargs):
        """NA2：格式化调整字段（委托 report_formatters）"""
        from report_formatters import format_score_adjustments
        return format_score_adjustments(*args, **kwargs)

    def _generate_swarm_markdown_report(self, *args, **kwargs):
        """生成蜂群 Markdown 报告（委托 report_formatters）"""
        from report_formatters import generate_swarm_markdown_report
        return generate_swarm_markdown_report(self, *args, **kwargs)

    def _generate_swarm_twitter_threads(self, *args, **kwargs):
        """生成蜂群 X 线程版本（委托 report_formatters）"""
        from report_formatters import generate_swarm_twitter_threads
        return generate_swarm_twitter_threads(self, *args, **kwargs)

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
                for i, opp in enumerate(self.opportunities)
            ],
            "observation_list": self.observations
        }

        return report

    def _generate_markdown_report(self, *args, **kwargs):
        """生成中文 Markdown 报告（委托 report_formatters）"""
        from report_formatters import generate_markdown_report
        return generate_markdown_report(self, *args, **kwargs)

    def _generate_twitter_threads(self, *args, **kwargs):
        """生成 X 线程版本（委托 report_formatters）"""
        from report_formatters import generate_twitter_threads
        return generate_twitter_threads(self, *args, **kwargs)

    def auto_commit_and_notify(self, *args, **kwargs):
        """自动提交报告 + 通知（委托 report_deployer）"""
        from report_deployer import auto_commit_and_notify
        return auto_commit_and_notify(self, *args, **kwargs)

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
        # 加载蜂群详细数据（save_report 已写入 .swarm_results_*.json）
        swarm_data: Dict = {}
        sr_path = self.report_dir / f".swarm_results_{self.date_str}.json"
        if sr_path.exists():
            try:
                with open(sr_path) as f:
                    swarm_data = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

        # 用 swarm_data 所有标的（而非仅 opportunities 前几名），确保每个扫描标的都有 ML 报告
        opps = report.get("opportunities", [])
        opp_tickers = [o.get("ticker") for o in opps if o.get("ticker")]
        extra = [t for t in swarm_data if t not in opp_tickers]
        tickers = opp_tickers + extra
        if not tickers:
            return []

        def _gen_one(ticker: str) -> str | None:
            """生成单个标的的 ML 报告（线程安全）"""
            try:
                real_price, real_change = 100.0, 0.0
                try:
                    import yfinance as _yf
                    _hist = _yf.Ticker(ticker).history(period="5d")
                    if not _hist.empty:
                        real_price = float(_hist["Close"].iloc[-1])
                        if len(_hist) >= 2:
                            real_change = (_hist["Close"].iloc[-1] / _hist["Close"].iloc[-2] - 1) * 100
                except Exception as _yfe:
                    _log.debug("yfinance 价格获取失败 %s: %s", ticker, _yfe)

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

                enhanced = self.ml_generator.generate_ml_enhanced_report(ticker, ticker_data)

                if ticker in swarm_data:
                    enhanced["swarm_results"] = swarm_data[ticker]

                html = self.ml_generator.generate_html_report(ticker, enhanced)
                html_path = self.report_dir / f"alpha-hive-{ticker}-ml-enhanced-{self.date_str}.html"
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)

                _log.info("ML 增强报告已生成：%s", html_path.name)
                return ticker
            except Exception as e:
                _log.warning("ML 报告生成失败 %s: %s", ticker, e)
                return None

        # 并行生成 ML 报告（yfinance + HTML 渲染，I/O 密集）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        generated = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_gen_one, t): t for t in tickers}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    generated.append(result)

        return generated

    # ── save_report helper methods ──────────────────────────────────

    def _merge_existing_report(self, report, json_file):
        """合并今日已有报告的 opportunities（支持分批运行）— mutates report"""
        if not json_file.exists():
            return
        try:
            with open(json_file, encoding="utf-8") as _f:
                existing = json.load(_f)
            existing_by_ticker = {o.get("ticker"): o for o in existing.get("opportunities", [])}
            new_by_ticker = {o.get("ticker"): o for o in report.get("opportunities", [])}
            existing_by_ticker.update(new_by_ticker)  # 新批次结果优先
            merged_opps = sorted(existing_by_ticker.values(),
                                 key=lambda x: x.get("opp_score", 0), reverse=True)
            report["opportunities"] = merged_opps
            if "swarm_metadata" in report:
                report["swarm_metadata"]["tickers_analyzed"] = len(merged_opps)
            _log.info("合并今日已有报告：共 %d 标的", len(merged_opps))
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            _log.warning("合并已有报告失败，使用新报告: %s", e)

    def _generate_synthetic_swarm_results(self, report):
        """确保 .swarm_results 存在（回退路径：从 opportunities 合成）"""
        swarm_json = self.report_dir / f".swarm_results_{self.date_str}.json"
        if swarm_json.exists():
            return
        _sr_data = {}
        # ── 获取 F&G 指数（全局，仅查一次）──
        _fg_value, _fg_class = None, ""
        try:
            from fear_greed import get_fear_greed
            _fg = get_fear_greed()
            _fg_value = _fg.get("value")
            _fg_class = _fg.get("classification", "")
        except Exception as _e_fg:
            logging.getLogger("alpha_hive").debug("Fear & Greed 指数获取失败: %s", _e_fg)
        # ── 初始化期权分析器（复用缓存，开销极低）──
        _opts_agent = None
        try:
            from options_analyzer import OptionsAgent
            _opts_agent = OptionsAgent()
        except ImportError:
            pass
        for _opp in report.get("opportunities", []):
            _tk = _opp.get("ticker")
            if not _tk:
                continue
            _dir_raw = _opp.get("direction", "中性")
            _dir_en = "bullish" if "多" in _dir_raw else ("bearish" if "空" in _dir_raw else "neutral")
            # ── 期权数据（IV Rank / P/C Ratio / GEX 等）──
            _oracle_details = {}
            _opts_signal = _opp.get("options_signal", "")
            if _opts_agent:
                try:
                    _or = _opts_agent.analyze(_tk)
                    _oracle_details = {
                        "iv_rank": _or.get("iv_rank"),
                        "iv_current": _or.get("iv_current"),
                        "put_call_ratio": _or.get("put_call_ratio"),
                        "gamma_exposure": _or.get("gamma_exposure"),
                        "gamma_squeeze_risk": _or.get("gamma_squeeze_risk"),
                        "flow_direction": _or.get("flow_direction"),
                        "signal_summary": _or.get("signal_summary", _opts_signal),
                    }
                    _opts_signal = _or.get("signal_summary", _opts_signal)
                except Exception as _oe:
                    _log.debug("期权数据获取失败 %s: %s", _tk, _oe)
            # ── BuzzBee discovery（含 F&G）──
            _buzz_disc = ""
            if _fg_value is not None:
                _buzz_disc = f"F&G {_fg_value} ({_fg_class})"
            # ── 价格数据 ──
            _scout_details = {}
            try:
                import yfinance as _yf_sr
                _h_sr = _yf_sr.Ticker(_tk).history(period="5d")
                if not _h_sr.empty:
                    _scout_details["price"] = float(_h_sr["Close"].iloc[-1])
                    if len(_h_sr) >= 2:
                        _scout_details["momentum_5d"] = round(
                            (_h_sr["Close"].iloc[-1] / _h_sr["Close"].iloc[0] - 1) * 100, 2
                        )
            except Exception as _e_sr:
                logging.getLogger("alpha_hive").debug("闪电模式价格获取失败 (%s): %s", _tk, _e_sr)
            _sr_data[_tk] = {
                "ticker": _tk,
                "final_score": _opp.get("opp_score", 5.0),
                "direction": _dir_en,
                "supporting_agents": _opp.get("supporting_agents", 0),
                "resonance": {
                    "resonance_detected": bool(_opp.get("resonance")),
                    "supporting_agents": _opp.get("supporting_agents", 0)
                },
                "dimension_scores": {},
                "agent_details": {
                    "OracleBeeEcho": {
                        "score": _opp.get("opp_score", 5.0),
                        "details": _oracle_details,
                        "discovery": _opts_signal,
                    },
                    "BuzzBeeWhisper": {
                        "score": 5.0,
                        "discovery": _buzz_disc,
                    },
                    "ScoutBeeNova": {
                        "score": _opp.get("opp_score", 5.0),
                        "discovery": "",
                        "details": _scout_details,
                    },
                    "BearBeeContrarian": {
                        "score": 5.0,
                        "discovery": "",
                    },
                },
                "data_real_pct": 0,
            }
        if _sr_data:
            try:
                with open(swarm_json, "w", encoding="utf-8") as _sf:
                    json.dump(_sr_data, _sf, ensure_ascii=False, cls=SafeJSONEncoder)
                _log.info("已回写 .swarm_results（%d 标的，增强版）", len(_sr_data))
            except (OSError, TypeError) as _sre:
                _log.debug("swarm_results 回写失败: %s", _sre)

    def _save_output_files(self, report, md_file):
        """保存 MD + X 线程 + ML HTML + index.html + PWA + RSS"""
        # Markdown
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(report["markdown_report"])

        # 清理当天旧的 X 线程文件
        for old in self.report_dir.glob(f"alpha-hive-thread-{self.date_str}-*.txt"):
            old.unlink()

        # X 线程
        for i, thread in enumerate(report["twitter_threads"], 1):
            thread_file = self.report_dir / f"alpha-hive-thread-{self.date_str}-{i}.txt"
            with open(thread_file, "w", encoding="utf-8") as f:
                f.write(thread)

        # ML 增强 HTML 报告（必须在 index.html 前完成）
        try:
            ml_tickers = self._generate_ml_reports(report)
            if ml_tickers:
                _log.info("ML 增强报告完成：%s", ml_tickers)
                _log.info("ML 报告: %s", ", ".join(ml_tickers))
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log.warning("ML 报告批量生成出错: %s", e)

        # GitHub Pages 仪表板
        try:
            html = self._generate_index_html(report)
            index_file = self.report_dir / "index.html"
            with open(index_file, "w", encoding="utf-8") as f:
                f.write(html)
            _log.info("index.html 已更新（GitHub Pages）")
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log.warning("index.html 生成失败: %s", e)

        # PWA 文件
        try:
            self._write_pwa_files()
        except (OSError, ValueError) as e:
            _log.warning("PWA 文件生成失败: %s", e)

        # RSS
        try:
            rss_xml = self._generate_rss_xml(report)
            with open(self.report_dir / "rss.xml", "w", encoding="utf-8") as f:
                f.write(rss_xml)
            _log.info("rss.xml 已更新")
        except (OSError, ValueError) as e:
            _log.warning("rss.xml 生成失败: %s", e)

    def save_report(self, report: Dict) -> str:
        """保存报告到文件（MD / JSON / X线程 / index.html GitHub Pages）"""
        json_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.json"
        md_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.md"

        self._merge_existing_report(report, json_file)

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, cls=SafeJSONEncoder)

        self._generate_synthetic_swarm_results(report)
        self._save_output_files(report, md_file)

        _log.info("报告已保存：%s", md_file.name)
        return str(md_file)

    def _write_pwa_files(self, *args, **kwargs):
        """生成 PWA 文件（委托 report_web_assets）"""
        from report_web_assets import write_pwa_files
        return write_pwa_files(self, *args, **kwargs)

    def _generate_rss_xml(self, *args, **kwargs):
        """生成 RSS XML（委托 report_web_assets）"""
        from report_web_assets import generate_rss_xml
        return generate_rss_xml(self, *args, **kwargs)

    def _generate_index_html(self, *args, **kwargs):
        """生成 index.html（委托 report_web_assets）"""
        from report_web_assets import generate_index_html
        return generate_index_html(self, *args, **kwargs)

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
        default=["NVDA", "TSLA", "VKTX", "META", "MSFT", "RKLB", "BILI", "AMZN", "CRCL"],
        help='要扫描的股票代码列表（空格分隔，默认：NVDA TSLA VKTX META MSFT RKLB BILI AMZN CRCL）'
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
    parser.add_argument(
        '--no-llm',
        action='store_true',
        help='跳过询问，直接使用规则引擎模式（不调用 Claude API）'
    )
    parser.add_argument(
        '--use-llm',
        action='store_true',
        help='跳过询问，直接使用 LLM 混合模式'
    )

    args = parser.parse_args()

    # ── LLM 模式选择（每次跑简报前询问）──
    import llm_service as _llm_svc
    _llm_key_exists = bool(_llm_svc._load_api_key())

    if args.no_llm:
        use_llm = False
    elif args.use_llm:
        use_llm = True
    elif _llm_key_exists:
        print("\n┌─────────────────────────────────────────┐")
        print("│        Alpha Hive — 分析模式选择        │")
        print("├─────────────────────────────────────────┤")
        print("│  [1] LLM 混合模式  Claude API（推荐）   │")
        print("│      QueenDistiller + BuzzBee 语义增强  │")
        print("│      耗时 ~100s / 9 标的，约 $0.10      │")
        print("│                                         │")
        print("│  [2] 规则引擎模式  纯规则（测试迭代）   │")
        print("│      耗时 ~26s，$0 API 费用             │")
        print("└─────────────────────────────────────────┘")
        choice = input("请选择 [1/2，默认 1]：").strip()
        use_llm = (choice != "2")
    else:
        use_llm = False
        print("⚠️  未检测到 API Key，使用规则引擎模式")

    if not use_llm:
        _llm_svc.disable()
        print("🔧 规则引擎模式\n")
    else:
        print("🧠 LLM 混合模式（Claude API）\n")

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

    # 三端同步：GitHub 提交推送 + Hive App + Slack
    print("\n📡 同步三端：GitHub / Hive App / Slack...")
    try:
        sync_results = reporter.auto_commit_and_notify(report)
        git_ok = sync_results.get("git_push", {}).get("success", False)
        deploy_env = sync_results.get("deploy_env", "production")
        remote_label = sync_results.get("git_push", {}).get("remote", "origin")
        if deploy_env == "test":
            print(f"   GitHub push : {'✅' if git_ok else '⚠️  失败'} → 🔧 测试环境 https://wangmingjie36-creator.github.io/alpha-hive-test/")
        else:
            print(f"   GitHub push : {'✅' if git_ok else '⚠️  失败'} → 🧠 生产环境 https://wangmingjie36-creator.github.io/alpha-hive-deploy/")
        print(f"   Hive App    : ✅ .swarm_results 已落盘，下次启动自动加载")
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        _log.warning("三端同步部分失败: %s", e)
        print(f"   ⚠️  三端同步出错：{e}")

    return report


if __name__ == "__main__":
    main()
