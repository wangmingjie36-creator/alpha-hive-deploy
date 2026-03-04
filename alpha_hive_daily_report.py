#!/usr/bin/env python3
"""
🐝 Alpha Hive 日报生成器 - 集成期权分析的完整版本
每日自动扫描 watchlist 并生成结构化投资简报 + X 线程版本
"""

import json
import argparse
import time
from datetime import datetime
from typing import Dict, List, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# 导入现有模块
from config import WATCHLIST
from hive_logger import get_logger, PATHS, set_correlation_id, SafeJSONEncoder

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
        future = self._bg_executor.submit(fn, *args, **kwargs)
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

    def _deploy_static_to_ghpages(self):
        """用 git plumbing 构建仅含静态文件的 gh-pages 提交并推送。"""
        import subprocess
        import os
        repo = self.agent_helper.git.repo_path or "."
        idx = os.path.join(repo, ".git", "gh-pages-index")
        if os.path.exists(idx):
            os.remove(idx)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = idx
        static_exts = (".html", ".json", ".xml", ".js")
        files = [f for f in os.listdir(repo)
                 if f.endswith(static_exts) or f == ".nojekyll"]
        if not files:
            _log.warning("无静态文件可部署")
            return
        for f in sorted(files):
            blob = subprocess.check_output(
                ["git", "hash-object", "-w", f], cwd=repo
            ).decode().strip()
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", "100644", blob, f],
                env=env, cwd=repo, check=True
            )
        tree = subprocess.check_output(
            ["git", "write-tree"], env=env, cwd=repo
        ).decode().strip()
        # 获取 gh-pages 父提交（若存在）
        parent_args = []
        try:
            parent = subprocess.check_output(
                ["git", "rev-parse", "gh-pages"], cwd=repo, stderr=subprocess.DEVNULL
            ).decode().strip()
            parent_args = ["-p", parent]
        except subprocess.CalledProcessError:
            pass
        commit = subprocess.check_output(
            ["git", "commit-tree", tree] + parent_args +
            ["-m", f"Deploy: Alpha Hive static {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=repo
        ).decode().strip()
        subprocess.run(
            ["git", "update-ref", "refs/heads/gh-pages", commit],
            cwd=repo, check=True
        )
        r = subprocess.run(
            ["git", "push", "origin", "gh-pages", "--force"],
            cwd=repo, capture_output=True, text=True
        )
        if os.path.exists(idx):
            os.remove(idx)
        if r.returncode == 0:
            _log.info("gh-pages 部署成功 (%d 静态文件)", len(files))
        else:
            _log.warning("gh-pages push 失败: %s", r.stderr)

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
        # 汇报可选模块降级状态
        try:
            from hive_logger import FeatureRegistry
            FeatureRegistry.log_status()
        except ImportError:
            pass

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
        import llm_service as _llm_check_q
        queen = QueenDistiller(board, adapted_weights=adapted_w, enable_llm=_llm_check_q.is_available())

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

            # ── #18: Ticker 有效性检测（退市/停牌/拆股）──
            try:
                from swarm_agents import check_ticker_validity
                _validity = check_ticker_validity(ticker)
                if not _validity["valid"]:
                    _log.warning("[%d/%d] ⏭️ 跳过 %s（%s）", idx, len(targets), ticker, _validity["warning"])
                    continue
                if _validity.get("warning"):
                    _log.warning("[%d/%d] ⚠️ %s 异常：%s", idx, len(targets), ticker, _validity["warning"])
            except Exception as _ve:
                _log.debug("ticker validity check error for %s: %s", ticker, _ve)

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
                    json.dump({"results": swarm_results, "targets": targets}, f, cls=SafeJSONEncoder)
            except (OSError, TypeError) as e:
                _log.warning("Checkpoint 写入失败: %s", e)

        # ==================== Phase 2: 历史类比推理（top-3 ticker）====================
        try:
            if queen.enable_llm and self.vector_memory and self.memory_store:
                # 按 final_score 降序取 top-3
                sorted_tickers = sorted(
                    swarm_results.keys(),
                    key=lambda t: swarm_results[t].get("final_score", 0),
                    reverse=True,
                )[:3]
                for tk in sorted_tickers:
                    try:
                        queen.enrich_with_historical_analogy(
                            ticker=tk,
                            distilled=swarm_results[tk],
                            vector_memory=self.vector_memory,
                            memory_store=self.memory_store,
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

        # 扫描完成，保存蜂群结果（合并当日已有结果，支持分批运行）
        try:
            swarm_json = self.report_dir / f".swarm_results_{self.date_str}.json"
            merged_swarm = {}
            if swarm_json.exists():
                try:
                    with open(swarm_json, encoding="utf-8") as _f:
                        merged_swarm = json.load(_f)
                except (OSError, json.JSONDecodeError):
                    pass
            merged_swarm.update(swarm_results)  # 新批次覆盖同名标的
            with open(swarm_json, "w") as f:
                json.dump(merged_swarm, f, cls=SafeJSONEncoder, ensure_ascii=False)
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

                # SLO 检查 + 自动 Slack 告警
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

        # Phase 6: 回测反馈循环
        adapted = None
        if Backtester:
            try:
                bt = Backtester()
                bt.save_predictions(swarm_results)
                bt.run_backtest()
                # 优先 T+7（更可靠，需 10 条样本），不足时降级到 T+1（需 5 条）
                adapted = bt.adapt_weights(min_samples=10, period="t7")
                if adapted is None:
                    adapted = bt.adapt_weights(min_samples=5, period="t1")
                    if adapted:
                        _log.info("自适应权重：T+7 样本不足，使用 T+1 数据（保守调整）")
            except (OSError, ValueError, KeyError, TypeError) as e:
                _log.warning("回测异常: %s", e)

        # 权重自适应 Slack 通知
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

        # 数据库清理（每日扫描末尾，保留 180 天数据）
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

        # Phase 6: Slack 推送高分机会 + 异常信号
        if self.slack_notifier and self.slack_notifier.enabled:
            for ticker, data in swarm_results.items():
                score = data.get("final_score", 0)
                direction = data.get("direction", "neutral")
                dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(direction, direction)

                # NA2：调整说明（附加到 Slack 推送）
                adj_note = self._format_score_adjustments(data)
                details_list = [f"评分 {score:.1f}/10"]
                if adj_note:
                    details_list.append(adj_note)
                cov = data.get("dimension_coverage_pct", 100.0)
                if cov < 100.0:
                    details_list.append(f"维度覆盖 {cov:.0f}%")

                # 高分机会推送（>= 7.5）
                if score >= 7.5:
                    self._submit_bg(
                        self.slack_notifier.send_opportunity_alert,
                        ticker, score, dir_cn,
                        data.get("discovery", "高分机会"),
                        details_list
                    )

                # 异常信号推送：强看空 或 内幕大额交易
                elif score <= 3.0:
                    self._submit_bg(
                        self.slack_notifier.send_risk_alert,
                        f"{ticker} 低分预警",
                        f"蜂群评分仅 {score:.1f}/10，方向 {dir_cn}" + (f" | {adj_note}" if adj_note else ""),
                        "HIGH"
                    )

        # 生成综合报告
        report = self._build_swarm_report(swarm_results, board, agent_count=len(all_agents))

        # ── 失效条件快照（仅有配置的标的：NVDA/VKTX/TSLA）──
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
                    # 同步写入 swarm_results 供 .swarm_results_*.json 持久化
                    if _tk in swarm_results:
                        swarm_results[_tk]["thesis_break_l1"] = _l1
                        swarm_results[_tk]["thesis_break_l2"] = _l2
        except Exception as _tbe:
            _log.warning("thesis_break 配置加载失败: %s", _tbe)

        # Phase 3 P2: 为高分机会添加日历提醒（后台线程池，退出时等待完成）
        if self.calendar and report.get('opportunities'):
            for opp in report['opportunities']:
                _opp_score = opp.get("opp_score", 0) if isinstance(opp, dict) else getattr(opp, "opportunity_score", 0)
                if _opp_score >= 7.5:
                    _tk = opp.get("ticker", "") if isinstance(opp, dict) else getattr(opp, "ticker", "")
                    _dir = opp.get("direction", "") if isinstance(opp, dict) else getattr(opp, "direction", "")
                    self._submit_bg(
                        self.calendar.add_opportunity_reminder,
                        _tk, _opp_score, _dir, "高分机会"
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

        # 重试失败的 Slack 消息
        if self.slack_notifier and self.slack_notifier.enabled:
            try:
                retried = self.slack_notifier.retry_failed()
                if retried:
                    _log.info("Slack 重试成功 %d 条", retried)
            except Exception as e:
                _log.debug("Slack 重试失败: %s", e)

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

        # ── P4b: 跨标的关联分析（LLM）──
        cross_ticker_analysis = {}
        try:
            from config import WATCHLIST
            import llm_service as _llm_ct
            use_llm_flag = _llm_ct.is_available() or any(
                r.get("distill_mode") == "llm_enhanced" for r in swarm_results.values()
            )
            if use_llm_flag and len(swarm_results) >= 2:
                import llm_service
                # 构建 sector_map
                sector_map = {}
                for tk in swarm_results:
                    wl_entry = WATCHLIST.get(tk, {})
                    sector_map[tk] = wl_entry.get("sector", "Other") if isinstance(wl_entry, dict) else "Other"
                # 构建 distilled_scores
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
                    # 将 cross_ticker_insights 注入各 ticker 的 swarm_results
                    for insight in cross_ticker_analysis.get("cross_ticker_insights", []):
                        for tk in insight.get("tickers", []):
                            if tk in swarm_results:
                                existing = swarm_results[tk].get("cross_ticker_insights", [])
                                existing.append(insight)
                                swarm_results[tk]["cross_ticker_insights"] = existing
                    # 将 sector_momentum 注入各 ticker
                    for tk in swarm_results:
                        sec = sector_map.get(tk, "Other")
                        mom = cross_ticker_analysis.get("sector_momentum", {}).get(sec)
                        if mom:
                            swarm_results[tk]["sector_momentum"] = mom
        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.debug("P4b 跨标的关联分析不可用: %s", e)

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
            "cross_ticker_analysis": cross_ticker_analysis,
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
                for i, opp in enumerate(self.opportunities[:5])
            ]
        }

        return report

    @staticmethod
    def _format_score_adjustments(data: Dict) -> str:
        """
        NA2：将 distill() 返回的调整字段格式化为人类可读注释。
        返回空字符串表示无调整发生。

        示例输出：
          "⚠️ 反对蜂看空 8.5 → 封顶 9.25 | ⚠️ 数据质量 60% (×0.875) | 🤖 LLM蒸馏(0.8) 基础分8.3 | ❌ 维度覆盖64%"
        """
        parts = []

        # BearBee 封顶
        if data.get("bear_cap_applied"):
            bs = data.get("bear_strength", 0.0)
            rs = data.get("rule_score", data.get("final_score", 0.0))
            parts.append(f"⚠️ 反对蜂看空强度{bs:.1f} → 封顶{rs:.2f}")

        # GuardBee 风险折扣
        if data.get("guard_penalty_applied"):
            gp = data.get("guard_penalty", 0.0)
            parts.append(f"🛡️ 风控折扣-{gp:.2f}")

        # 数据质量折扣
        if data.get("dq_penalty_applied"):
            rp = data.get("data_real_pct", 0.0)
            qf = data.get("dq_quality_factor", 1.0)
            parts.append(f"⚠️ 数据质量{rp:.0f}%(×{qf:.3f})")

        # LLM 蒸馏
        if data.get("distill_mode") == "llm_enhanced":
            lc = data.get("llm_confidence", 0.0)
            rs = data.get("rule_score", data.get("final_score", 0.0))
            parts.append(f"🤖 LLM蒸馏(置信{lc:.1f}) 基础分{rs:.1f}")

        # 维度覆盖率不足
        cov = data.get("dimension_coverage_pct", 100.0)
        if cov < 80.0:
            dim_status = data.get("dimension_status", {})
            missing = (
                [dim for dim, st in dim_status.items() if st != "present"]
                if isinstance(dim_status, dict) else []
            )
            missing_str = "/".join(missing) if missing else ""
            parts.append(f"❌ 维度覆盖{cov:.0f}%({missing_str})")

        return " | ".join(parts)

    def _generate_swarm_markdown_report(self, swarm_results: Dict,
                                         concentration: Dict = None,
                                         macro_context: Dict = None,
                                         backtest_stats: Dict = None,
                                         agent_count: int = 7,
                                         cross_ticker: Dict = None) -> str:
        """生成蜂群模式的 Markdown 报告（8 版块 + P4集中度 + P4b跨标的 + P5宏观 + P3回测）"""

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
            narrative = data.get("narrative", "")
            summary_line = f"- **{ticker}** {data['direction'].upper()} {data['final_score']:.1f}/10 {res}"
            if narrative:
                summary_line += f"\n  - 📝 {narrative}"
            md.append(summary_line)
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
            # LLM 看空论点（升级后新增）
            llm_thesis = agent.get("llm_thesis", "")
            llm_risks = agent.get("llm_key_risks", [])
            llm_ci = agent.get("llm_contrarian_insight", "")
            if llm_thesis:
                md.append(f"- 🤖 **AI看空论点**：{llm_thesis}")
            if llm_risks:
                for risk in llm_risks[:3]:
                    md.append(f"  - ⚠️ {risk}")
            if llm_ci:
                md.append(f"- 💡 **反对洞察**：{llm_ci}")
            # 数据来源标注
            sources = details.get("data_sources", {})
            if sources:
                src_labels = {"pheromone_board": "蜂群共享", "sec_api": "SEC直查",
                              "options_api": "期权直查", "finviz_api": "Finviz",
                              "yfinance": "yfinance", "unavailable": "不可用",
                              "llm_enhanced": "LLM增强"}
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

        # LLM 多空综合叙事（升级后新增）
        synthesis_lines = []
        for ticker, data in sorted_results:
            bbs = data.get("bull_bear_synthesis", "")
            cv = data.get("contrarian_view", "")
            if bbs or cv:
                parts = [f"- **{ticker}**"]
                if bbs:
                    parts.append(f"  - 多空综合：{bbs}")
                if cv:
                    parts.append(f"  - 少数意见：{cv}")
                synthesis_lines.append("\n".join(parts))
        if synthesis_lines:
            md.append("### AI 多空综合叙事")
            md.append("")
            md.extend(synthesis_lines)
            md.append("")

        # Phase 2: 历史类比推理子版块
        analogy_lines = []
        for ticker, data in sorted_results:
            ha = data.get("historical_analogy")
            if ha and ha.get("analogy_found"):
                analogy_lines.append(f"**{ticker}** — {ha.get('analogy_summary', '')}")
                outcome = ha.get("historical_outcome", {})
                t1 = outcome.get("t1", "N/A")
                t7 = outcome.get("t7", "N/A")
                t30 = outcome.get("t30", "N/A")
                analogy_lines.append(f"- 历史结果：T+1 {t1} | T+7 {t7} | T+30 {t30}")
                sim = ha.get("similarity_score", 0)
                analogy_lines.append(f"- 相似度：{sim:.0%} | 置信调整：{ha.get('score_adjustment_applied', 0):+.1f}分")
                diff = ha.get("key_differences", "")
                if diff:
                    analogy_lines.append(f"- 关键差异：{diff}")
                warning = ha.get("warning", "")
                if warning:
                    analogy_lines.append(f"- ⚠️ {warning}")
                analogy_lines.append("")
        if analogy_lines:
            md.append("### 📜 历史类比推理")
            md.append("")
            md.extend(analogy_lines)

        # NA2：评分调整注释（bear_cap / dq_penalty / llm_enhanced / 低维度覆盖）
        adj_lines = []
        for ticker, data in sorted_results:
            adj = self._format_score_adjustments(data)
            if adj:
                adj_lines.append(f"- **{ticker}**：{adj}")
        if adj_lines:
            md.append("### 评分调整说明")
            md.append("")
            md.extend(adj_lines)
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

        # ====== 版块 P4b：跨标的关联分析（LLM）======
        if cross_ticker and isinstance(cross_ticker, dict):
            md.append("## 🔗 跨标的关联分析")
            md.append("")

            # 板块动量
            sector_mom = cross_ticker.get("sector_momentum", {})
            if sector_mom:
                mom_parts = []
                for sec, trend in sector_mom.items():
                    emoji = {"leading": "🟢", "lagging": "🔴", "neutral": "🟡"}.get(trend, "")
                    mom_parts.append(f"{sec}: {trend} {emoji}")
                md.append("**板块动量**：" + " | ".join(mom_parts))
                md.append("")

            # 跨标的洞察
            insights = cross_ticker.get("cross_ticker_insights", [])
            if insights:
                md.append("**关联洞察**：")
                for ins in insights[:5]:
                    tks = " & ".join(ins.get("tickers", []))
                    md.append(f"- [{ins.get('type', '')}] {tks}：{ins.get('insight', '')}")
                md.append("")

            # 关联风险警告
            corr_warnings = cross_ticker.get("correlation_warnings", [])
            if corr_warnings:
                md.append("**关联风险⚠️**：")
                for w in corr_warnings[:4]:
                    md.append(f"- {w}")
                md.append("")

            # 轮动信号
            rotation = cross_ticker.get("sector_rotation_signal", "")
            if rotation:
                md.append(f"**轮动信号**：{rotation}")
                md.append("")

            # 组合建议
            hints = cross_ticker.get("portfolio_adjustment_hints", [])
            if hints:
                md.append("**组合调整建议**：")
                for h in hints[:3]:
                    md.append(f"- {h}")
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
            narrative = data.get("narrative", "")
            tweet = (
                f"{i}. **{ticker}** {data['direction'].upper()}\n"
                f"蜂群评分：{data['final_score']:.1f}/10 | 共振：{resonance_emoji}\n"
                f"Agent 投票：看多{data['agent_breakdown']['bullish']} vs 看空{data['agent_breakdown']['bearish']}"
            )
            if narrative:
                tweet += f"\n📝 {narrative}"
            elif insight:
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

        # 1. Git 提交报告（始终新 commit，不 amend，避免 GitHub Pages 部署冲突）
        #
        # ⚠️ 架构说明：
        #   - LLM 模式：commit 所有变更 → git push origin main → 生产页面更新
        #   - 测试模式：commit 所有变更 → 仅推 test remote（临时分支）→ git reset --hard origin/main
        #              local main 完全回滚，origin/main 不受任何影响
        #   - 禁止在测试模式外手动 `git add index.html && git push origin main`，
        #     生成物（index.html / md / json / ML html）只能通过 LLM 扫描进入 origin
        from datetime import datetime as _dt2
        import llm_service as _llm_check
        _using_llm = _llm_check.is_available()
        timestamp = _dt2.now().strftime("%H:%M")
        today_commit_msg = f"Alpha Hive 蜂群日报 {self.date_str} {timestamp}"
        _log.info("Git commit... (mode: new)")
        status = self.agent_helper.git.status()
        if status.get("modified_files"):
            commit_result = self.agent_helper.git.commit(today_commit_msg)
            results["git_commit"] = commit_result
            if commit_result["success"]:
                _log.info("Git commit 成功（new）")
            else:
                _log.warning("Git commit 失败：%s", commit_result.get('message'))
        else:
            _log.info("无需提交（工作目录干净）")

        # 2. Git 推送：LLM 模式 → 生产（origin main），规则模式 → 测试（test remote）
        #    规则模式使用临时分支，不污染本地 main，推完即删除
        env_label = "🧠 生产（LLM）" if _using_llm else "🔧 测试（规则引擎）"
        _log.info("Git push → [%s]", env_label)

        if _using_llm:
            # 生产模式：推送 origin main + gh-pages（Pages 从 gh-pages 部署）
            r = self.agent_helper.git.run_git_cmd("git push origin main")
            push_result = {"success": r["success"], "remote": "origin",
                           "output": r.get("stdout", "") or r.get("stderr", "")}
            # 同步 gh-pages 分支（仅静态文件，GitHub Pages 部署源）
            try:
                self._deploy_static_to_ghpages()
            except Exception as e:
                _log.warning("gh-pages 部署失败: %s", e)
        else:
            # 测试模式：临时分支 → test remote → 删除临时分支 → 本地 main 回滚到 origin/main
            _remote_check = self.agent_helper.git.run_git_cmd("git remote")
            if "test" not in _remote_check.get("stdout", ""):
                _log.warning("test remote 不存在，跳过推送")
                push_result = {"success": False, "error": "test remote not configured"}
            else:
                _tmp = "_test_snapshot"
                # 从当前 HEAD 创建临时分支并推送到 test:main
                self.agent_helper.git.run_git_cmd(f"git branch -D {_tmp}")
                self.agent_helper.git.run_git_cmd(f"git checkout -b {_tmp}")
                r = self.agent_helper.git.run_git_cmd(f"git push test {_tmp}:main --force")
                push_result = {"success": r["success"], "remote": "test",
                               "output": r.get("stdout", "") or r.get("stderr", "")}
                # 回到 main 并删除临时分支，本地 main 恢复干净状态
                self.agent_helper.git.run_git_cmd("git checkout main")
                self.agent_helper.git.run_git_cmd(f"git branch -D {_tmp}")
                # 重置本地 main 到 origin/main，撤销测试数据对本地 main 的污染
                self.agent_helper.git.run_git_cmd("git fetch origin")
                self.agent_helper.git.run_git_cmd("git reset --hard origin/main")
                _log.info("本地 main 已恢复至 origin/main（测试数据不污染生产）")

        results["git_push"] = push_result
        results["deploy_env"] = "production" if _using_llm else "test"
        if push_result["success"]:
            _log.info("Git push 成功 → %s", push_result.get("remote"))
        else:
            _log.warning("Git push 失败：%s", push_result.get("error") or push_result.get("output", ""))

        # 3. Slack 通知（由 Claude Code MCP 工具推送，不用 webhook bot）
        _log.info("Slack 推送由 Claude Code 负责（用户账号）")
        results["slack_notification"] = {"skipped": "handled_by_claude_mcp"}

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

        json_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.json"
        md_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.md"

        # 如果今日已有报告，合并 opportunities（支持分批运行，避免互相覆盖）
        if json_file.exists():
            try:
                with open(json_file, encoding="utf-8") as _f:
                    existing = json.load(_f)
                existing_by_ticker = {o.get("ticker"): o for o in existing.get("opportunities", [])}
                new_by_ticker = {o.get("ticker"): o for o in report.get("opportunities", [])}
                existing_by_ticker.update(new_by_ticker)  # 新批次结果优先
                merged_opps = sorted(existing_by_ticker.values(),
                                     key=lambda x: x.get("opportunity_score", 0), reverse=True)
                report["opportunities"] = merged_opps
                if "swarm_metadata" in report:
                    report["swarm_metadata"]["tickers_analyzed"] = len(merged_opps)
                _log.info("合并今日已有报告：共 %d 标的", len(merged_opps))
            except Exception as e:
                _log.warning("合并已有报告失败，使用新报告: %s", e)

        # 保存 JSON 版本
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, cls=SafeJSONEncoder)

        # 保存 Markdown 版本（新批次追加到已有文件末尾）
        if md_file.exists():
            existing_md = md_file.read_text(encoding="utf-8")
            combined_md = existing_md + "\n\n---\n\n" + report["markdown_report"]
            with open(md_file, "w", encoding="utf-8") as f:
                f.write(combined_md)
            report["markdown_report"] = combined_md
        else:
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
                _log.info("ML 报告: %s", ", ".join(ml_tickers))
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

        # 生成 PWA 文件（manifest.json + sw.js）
        try:
            self._write_pwa_files()
        except Exception as e:
            _log.warning("PWA 文件生成失败: %s", e)

        # 生成 RSS 订阅源
        try:
            rss_xml = self._generate_rss_xml(report)
            with open(self.report_dir / "rss.xml", "w", encoding="utf-8") as f:
                f.write(rss_xml)
            _log.info("rss.xml 已更新")
        except Exception as e:
            _log.warning("rss.xml 生成失败: %s", e)

        _log.info("报告已保存：%s", md_file.name)

        return str(md_file)

    def _write_pwa_files(self):
        """生成 manifest.json + sw.js"""
        import json as _json2

        # ── manifest.json ──
        icon_svg = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Cpolygon points='50,5 93,28 93,72 50,95 7,72 7,28' fill='%23F4A532'/%3E%3Ctext x='50' y='62' font-size='42' text-anchor='middle' fill='%23fff'%3E🐝%3C/text%3E%3C/svg%3E"
        manifest = {
            "name": "Alpha Hive 投资仪表板",
            "short_name": "Alpha Hive",
            "start_url": "./",
            "display": "standalone",
            "theme_color": "#F4A532",
            "background_color": "#0e1117",
            "icons": [
                {"src": icon_svg, "sizes": "any", "type": "image/svg+xml"}
            ]
        }
        manifest_path = self.report_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            _json2.dump(manifest, f, ensure_ascii=False, indent=2)

        # ── sw.js ──
        cache_name = f"alpha-hive-{self.date_str}"
        sw_content = f"""// Alpha Hive Service Worker - {self.date_str}
var CACHE_NAME='{cache_name}';
var PRECACHE_URLS=['./', 'index.html', 'manifest.json',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'];

self.addEventListener('install', function(e){{
  e.waitUntil(
    caches.open(CACHE_NAME).then(function(cache){{
      return cache.addAll(PRECACHE_URLS);
    }})
  );
}});

self.addEventListener('activate', function(e){{
  e.waitUntil(
    caches.keys().then(function(names){{
      return Promise.all(
        names.filter(function(n){{ return n!==CACHE_NAME; }})
             .map(function(n){{ return caches.delete(n); }})
      );
    }})
  );
}});

self.addEventListener('fetch', function(e){{
  var url=new URL(e.request.url);
  // JSON 数据用 network-first
  if(url.pathname.endsWith('.json')){{
    e.respondWith(
      fetch(e.request).then(function(r){{
        var rc=r.clone();
        caches.open(CACHE_NAME).then(function(c){{ c.put(e.request, rc); }});
        return r;
      }}).catch(function(){{ return caches.match(e.request); }})
    );
    return;
  }}
  // HTML/CDN 用 cache-first
  e.respondWith(
    caches.match(e.request).then(function(r){{
      return r || fetch(e.request).then(function(resp){{
        var rc=resp.clone();
        caches.open(CACHE_NAME).then(function(c){{ c.put(e.request, rc); }});
        return resp;
      }});
    }})
  );
}});
"""
        sw_path = self.report_dir / "sw.js"
        with open(sw_path, "w", encoding="utf-8") as f:
            f.write(sw_content)

        _log.info("PWA 文件已生成：manifest.json + sw.js")

    def _generate_rss_xml(self, report: Dict) -> str:
        """生成 RSS 2.0 XML 订阅源"""
        import glob as _glob
        from xml.sax.saxutils import escape as _esc

        from datetime import timezone as _tz
        now_rfc = datetime.now(_tz.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        base_url = "https://wangmingjie36-creator.github.io/alpha-hive-deploy/"

        items_xml = ""
        # 当前简报作为第一条 item
        opps = report.get("opportunities", [])
        top3 = sorted(opps, key=lambda x: float(x.get("opp_score", 0)), reverse=True)[:3]
        top3_str = ", ".join(
            f"{o.get('ticker','')} ({float(o.get('opp_score',0)):.1f})" for o in top3
        ) if top3 else "无新机会"
        desc_text = f"今日 Top 3：{top3_str}"
        items_xml += (
            f"    <item>\n"
            f"      <title>{_esc('Alpha Hive 日报 ' + self.date_str)}</title>\n"
            f"      <link>{base_url}</link>\n"
            f"      <description>{_esc(desc_text)}</description>\n"
            f"      <pubDate>{now_rfc}</pubDate>\n"
            f"      <guid>{base_url}#{self.date_str}</guid>\n"
            f"    </item>\n"
        )

        # 历史 JSON 作为 items（最多 10 条）
        hist_files = sorted(
            _glob.glob(str(self.report_dir / "alpha-hive-daily-*.json")),
            reverse=True
        )
        count = 0
        for hf in hist_files:
            from pathlib import Path as _P
            hdate = _P(hf).stem.replace("alpha-hive-daily-", "")
            if hdate == self.date_str:
                continue
            try:
                with open(hf, encoding="utf-8") as fp:
                    hrpt = json.load(fp)
                hopps = hrpt.get("opportunities", [])
                htop3 = sorted(hopps, key=lambda x: float(x.get("opp_score", 0)), reverse=True)[:3]
                htop3_str = ", ".join(
                    f"{o.get('ticker','')} ({float(o.get('opp_score',0)):.1f})" for o in htop3
                ) if htop3 else "无机会"
                items_xml += (
                    f"    <item>\n"
                    f"      <title>{_esc('Alpha Hive 日报 ' + hdate)}</title>\n"
                    f"      <link>{base_url}</link>\n"
                    f"      <description>{_esc('Top 3：' + htop3_str)}</description>\n"
                    f"      <pubDate>{hdate}</pubDate>\n"
                    f"      <guid>{base_url}#{hdate}</guid>\n"
                    f"    </item>\n"
                )
                count += 1
                if count >= 10:
                    break
            except (json.JSONDecodeError, KeyError, OSError, ValueError) as _rss_err:
                _log.debug("RSS 历史条目解析失败: %s", _rss_err)
                continue

        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0">\n'
            '  <channel>\n'
            '    <title>Alpha Hive 投资日报</title>\n'
            f'    <link>{base_url}</link>\n'
            '    <description>去中心化蜂群智能投资研究平台 — 每日投资机会扫描</description>\n'
            '    <language>zh-CN</language>\n'
            f'    <lastBuildDate>{now_rfc}</lastBuildDate>\n'
            f'{items_xml}'
            '  </channel>\n'
            '</rss>\n'
        )

    def _generate_index_html(self, report: Dict) -> str:
        """委托给 dashboard_renderer 模块生成仪表板 HTML"""
        from dashboard_renderer import render_dashboard_html
        return render_dashboard_html(
            report=report,
            date_str=self.date_str,
            report_dir=self.report_dir,
            opportunities=self.opportunities,
        )


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
