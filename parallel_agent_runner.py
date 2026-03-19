"""
Alpha Hive 升级 — Phase 1.2: Agent 并行化执行引擎
===================================================
替换 AlphaHiveDailyReporter.run_swarm_scan() 中的串行 Agent 调用

问题：当前 7 个 Agent 串行执行 analyze()，10标的耗时 ~133s
解决：分两阶段并行 —
  Phase A: Scout/Oracle/Buzz/Chronos/Rival 完全并行（无依赖）
  Phase B: Guard + Bear 读信息素板（依赖 Phase A）
  预估提速：133s → ~45s（3x）

用法：
  from parallel_agent_runner import ParallelAgentRunner
  runner = ParallelAgentRunner(max_workers_p1=5, max_workers_p2=2)
  results = runner.run_all(ticker, agents_dict)
"""

import time
import logging
from typing import Dict, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from dataclasses import dataclass, field

_log = logging.getLogger("alpha_hive.parallel_runner")


@dataclass
class AgentTiming:
    """单个 Agent 的执行计时"""
    agent_name: str
    ticker: str
    phase: int  # 1 or 2
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    success: bool = True
    error: str = ""


class ParallelAgentRunner:
    """
    两阶段并行 Agent 执行引擎
    
    Phase 1 (并行): ScoutBee, OracleBee, BuzzBee, ChronosBee, RivalBee, CodeExecutor
                    这些 Agent 互不依赖，可以安全并行
    Phase 2 (并行): GuardBee, BearBee
                    依赖 Phase 1 的信息素板数据
    
    线程安全：
    - PheromoneBoard.publish() 已有锁保护
    - 每个 Agent 的 _prefetched_stock / _prefetched_context 是只读的
    - yfinance 缓存有 _yf_lock 保护
    """

    # Phase 1 Agents（无依赖，完全并行）
    PHASE_1_AGENTS = {
        "ScoutBeeNova",
        "OracleBeeEcho",
        "BuzzBeeWhisper",
        "ChronosBeeHorizon",
        "RivalBeeVanguard",
        "CodeExecutorAgent",
    }

    # Phase 2 Agents（依赖信息素板，Phase 1 完成后执行）
    PHASE_2_AGENTS = {
        "GuardBeeSentinel",
        "BearBeeContrarian",
    }

    def __init__(self, max_workers_p1: int = 5, max_workers_p2: int = 2,
                 timeout_per_agent: float = 60.0):
        """
        Args:
            max_workers_p1: Phase 1 最大并行线程数（默认 5，避免 API 限流）
            max_workers_p2: Phase 2 最大并行线程数（默认 2）
            timeout_per_agent: 单个 Agent 的超时秒数
        """
        self.max_workers_p1 = max_workers_p1
        self.max_workers_p2 = max_workers_p2
        self.timeout_per_agent = timeout_per_agent
        self._timings: List[AgentTiming] = []

    def run_all(self, ticker: str, agents: Dict,
                on_agent_done: Optional[Callable] = None) -> List[Dict]:
        """
        对单个 ticker 运行所有 Agent（两阶段并行）
        
        Args:
            ticker: 股票代码
            agents: {agent_name: agent_instance} 字典
            on_agent_done: 可选回调，每个 Agent 完成时调用 (agent_name, result, timing)
        
        Returns:
            所有 Agent 结果的列表（保持与原始串行接口兼容）
        """
        all_results = []
        
        # ===== Phase 1: 并行执行独立 Agent =====
        phase1_agents = {
            name: agent for name, agent in agents.items()
            if name in self.PHASE_1_AGENTS
        }
        
        if phase1_agents:
            p1_results = self._run_phase(
                phase=1,
                ticker=ticker,
                agents=phase1_agents,
                max_workers=self.max_workers_p1,
                on_done=on_agent_done,
            )
            all_results.extend(p1_results)

        # ===== Phase 2: 并行执行依赖 Agent（Guard + Bear）=====
        phase2_agents = {
            name: agent for name, agent in agents.items()
            if name in self.PHASE_2_AGENTS
        }

        if phase2_agents:
            p2_results = self._run_phase(
                phase=2,
                ticker=ticker,
                agents=phase2_agents,
                max_workers=self.max_workers_p2,
                on_done=on_agent_done,
            )
            all_results.extend(p2_results)

        return all_results

    def run_batch(self, tickers: List[str], agents_factory: Callable,
                  on_ticker_done: Optional[Callable] = None,
                  on_agent_done: Optional[Callable] = None) -> Dict[str, List[Dict]]:
        """
        批量扫描多个 ticker（ticker 之间串行，Agent 之间并行）
        
        Args:
            tickers: 标的列表
            agents_factory: 工厂函数，返回 {agent_name: agent_instance}
            on_ticker_done: 可选回调 (idx, total, ticker, results)
            on_agent_done: 可选回调 (agent_name, result, timing)
        
        Returns:
            {ticker: [agent_results]}
        """
        all_results = {}
        total = len(tickers)

        for idx, ticker in enumerate(tickers, 1):
            _log.info("[ParallelRunner] 开始 %s (%d/%d)", ticker, idx, total)
            start = time.time()

            agents = agents_factory()
            results = self.run_all(ticker, agents, on_agent_done)
            all_results[ticker] = results

            elapsed = time.time() - start
            _log.info("[ParallelRunner] %s 完成 (%.1fs, %d results)",
                      ticker, elapsed, len(results))

            if on_ticker_done:
                on_ticker_done(idx, total, ticker, results)

        return all_results

    def _run_phase(self, phase: int, ticker: str, agents: Dict,
                   max_workers: int,
                   on_done: Optional[Callable] = None) -> List[Dict]:
        """运行一个阶段的所有 Agent（并行）"""
        results = []
        
        if not agents:
            return results

        with ThreadPoolExecutor(
            max_workers=min(max_workers, len(agents)),
            thread_name_prefix=f"agent-p{phase}"
        ) as executor:
            # 提交所有任务
            future_to_name: Dict[Future, str] = {}
            for name, agent in agents.items():
                timing = AgentTiming(
                    agent_name=name,
                    ticker=ticker,
                    phase=phase,
                    start_time=time.time(),
                )
                future = executor.submit(self._safe_analyze, agent, ticker, timing)
                future_to_name[future] = name

            # 收集结果（按完成顺序）
            try:
                completed_futures = as_completed(future_to_name, timeout=self.timeout_per_agent * 2)
                for future in completed_futures:
                    name = future_to_name[future]
                    try:
                        result, timing = future.result(timeout=self.timeout_per_agent)
                        self._timings.append(timing)
                        results.append(result)

                        if on_done:
                            try:
                                on_done(name, result, timing)
                            except Exception as e:
                                _log.warning("Agent callback failed for %s: %s", name, e)

                    except Exception as e:
                        _log.error("[Phase%d] %s 超时或异常: %s", phase, name, e)
                        # 返回安全的默认结果
                        results.append({
                            "score": 5.0,
                            "direction": "neutral",
                            "confidence": 0.0,
                            "discovery": f"Agent 执行超时: {str(e)[:80]}",
                            "source": name,
                            "dimension": self._guess_dimension(name),
                            "error": str(e),
                            "data_quality": {"source": "timeout"},
                        })
            except TimeoutError:
                # as_completed 全局超时 — 收集剩余未完成的 Agent
                for future, name in future_to_name.items():
                    if not future.done():
                        _log.error("[Phase%d] %s 全局超时未完成", phase, name)
                        results.append({
                            "score": 5.0,
                            "direction": "neutral",
                            "confidence": 0.0,
                            "discovery": f"Agent 全局超时",
                            "source": name,
                            "dimension": self._guess_dimension(name),
                            "error": "global_timeout",
                            "data_quality": {"source": "timeout"},
                        })

        return results

    @staticmethod
    def _safe_analyze(agent, ticker: str, timing: AgentTiming):
        """安全执行 Agent.analyze()，捕获所有异常"""
        try:
            result = agent.analyze(ticker)
            timing.end_time = time.time()
            timing.duration_ms = (timing.end_time - timing.start_time) * 1000
            timing.success = "error" not in result
            return result, timing
        except Exception as e:
            timing.end_time = time.time()
            timing.duration_ms = (timing.end_time - timing.start_time) * 1000
            timing.success = False
            timing.error = str(e)
            _log.error("Agent %s failed for %s: %s",
                       timing.agent_name, ticker, e, exc_info=True)
            return {
                "score": 5.0,
                "direction": "neutral",
                "confidence": 0.0,
                "discovery": f"Error: {str(e)[:100]}",
                "source": timing.agent_name,
                "dimension": "unknown",
                "error": str(e),
                "data_quality": {"source": "error"},
            }, timing

    @staticmethod
    def _guess_dimension(agent_name: str) -> str:
        """从 Agent 名称推断维度"""
        dim_map = {
            "ScoutBeeNova": "signal",
            "OracleBeeEcho": "odds",
            "BuzzBeeWhisper": "sentiment",
            "ChronosBeeHorizon": "catalyst",
            "RivalBeeVanguard": "ml_auxiliary",
            "GuardBeeSentinel": "risk_adj",
            "BearBeeContrarian": "contrarian",
            "CodeExecutorAgent": "ml_auxiliary",
        }
        return dim_map.get(agent_name, "unknown")

    def get_timing_report(self) -> Dict:
        """生成执行耗时报告"""
        if not self._timings:
            return {"total_agents": 0}

        by_phase = {1: [], 2: []}
        for t in self._timings:
            by_phase[t.phase].append(t)

        report = {
            "total_agents": len(self._timings),
            "total_duration_ms": sum(t.duration_ms for t in self._timings),
            "success_count": sum(1 for t in self._timings if t.success),
            "failure_count": sum(1 for t in self._timings if not t.success),
            "phases": {},
        }

        for phase_num, timings in by_phase.items():
            if not timings:
                continue
            durations = [t.duration_ms for t in timings]
            # 并行执行：实际耗时 ≈ 最慢的 Agent
            report["phases"][f"phase_{phase_num}"] = {
                "agent_count": len(timings),
                "wall_clock_ms": max(durations),  # 并行的实际耗时
                "sum_ms": sum(durations),          # 串行的总耗时（对比用）
                "speedup": round(sum(durations) / max(durations), 1) if max(durations) > 0 else 1.0,
                "slowest": max(timings, key=lambda t: t.duration_ms).agent_name,
                "fastest": min(timings, key=lambda t: t.duration_ms).agent_name,
                "agents": [
                    {
                        "name": t.agent_name,
                        "duration_ms": round(t.duration_ms, 1),
                        "success": t.success,
                        "error": t.error if t.error else None,
                    }
                    for t in sorted(timings, key=lambda t: t.duration_ms, reverse=True)
                ],
            }

        # 总加速比
        serial_total = sum(t.duration_ms for t in self._timings)
        parallel_total = sum(
            max(t.duration_ms for t in phase_timings) if phase_timings else 0
            for phase_timings in by_phase.values()
        )
        report["serial_estimate_ms"] = round(serial_total, 1)
        report["parallel_actual_ms"] = round(parallel_total, 1)
        report["overall_speedup"] = round(serial_total / parallel_total, 1) if parallel_total > 0 else 1.0

        return report

    def reset_timings(self):
        """重置计时数据（每次扫描前调用）"""
        self._timings.clear()
