"""GuardBeeSentinel - 交叉验证与风险评估蜂 (RiskAdj 维度, 权重 0.15)"""

from typing import Any, Dict, List, Optional
from swarm_agents._config import _log, _AS
from swarm_agents.cache import _safe_score
from swarm_agents.base import BeeAgent
from models import AgentResult
from swarm_agents.utils import (
    clamp_score, append_context, build_confidence,
    make_error_result, AGENT_ERRORS, LLM_ERRORS,
)


class GuardBeeSentinel(BeeAgent):
    """交叉验证与风险评估蜂 - 共振检测 + 拥挤度折扣 + 风险调整
    对应维度：RiskAdj (权重 0.15)
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)

            # 1. 检测信息素板共振
            resonance = self.board.detect_resonance(ticker)
            top_signals = self.board.get_top_signals(ticker, n=5)

            # 2. 从信息素板读取已有 Agent 分数
            avg_score = sum(e.self_score for e in top_signals) / len(top_signals) if top_signals else 5.0

            # 3. 评估信号一致性
            if top_signals:
                directions = [e.direction for e in top_signals]
                bull = directions.count("bullish")
                bear = directions.count("bearish")
                total = len(directions)
                consistency = max(bull, bear) / total if total > 0 else 0
            else:
                consistency = 0
                bull = bear = 0

            # 4. 拥挤度风险折扣（使用真实数据源）
            adj_factor = 1.0
            try:
                from crowding_detector import CrowdingDetector
                from real_data_sources import get_real_crowding_metrics
                stock = self._get_stock_data(ticker)
                detector = CrowdingDetector(ticker)
                real_metrics = get_real_crowding_metrics(ticker, stock, self.board)
                # 覆盖 bullish_agents 为实际信息素板数据
                real_metrics["bullish_agents"] = bull
                crowd, _ = detector.calculate_crowding_score(real_metrics)
                adj_factor = detector.get_adjustment_factor(crowd)
            except (ImportError, ValueError, KeyError, TypeError) as e:
                _log.warning("GuardBeeSentinel crowding analysis unavailable for %s: %s", ticker, e)

            # 5. 综合评分
            if resonance["resonance_detected"]:
                # 共振 + 一致性高 → 高分，但受拥挤度调整
                raw_score = 7.0 + consistency * 2.0  # 7.0 ~ 9.0
                score = raw_score * adj_factor
                direction = resonance["direction"]
                discovery = (
                    f"共振✅ {resonance['supporting_agents']} Agent 同向 | "
                    f"一致性 {consistency:.0%} | "
                    f"风险调整 {adj_factor:.2f}"
                )
            else:
                # 无共振 → 保守打折，但保留多数方向而非强制中性
                score = avg_score * 0.8 * adj_factor
                if bull > bear:
                    direction = "bullish"
                elif bear > bull:
                    direction = "bearish"
                else:
                    direction = "neutral"
                discovery = (
                    f"信号分散 | 均分 {avg_score:.1f} | "
                    f"一致性 {consistency:.0%} | "
                    f"风险调整 {adj_factor:.2f}"
                )

            score = clamp_score(score)

            discovery = append_context(discovery, ctx)

            # ── P5: FRED 宏观环境过滤（risk_off 时主动降权，risk_on 时小幅增强）──
            macro_adj = 0.0
            macro_desc = ""
            try:
                from fred_macro import get_macro_context, get_macro_risk_adjustment, get_sector_etf_for_ticker
                macro = get_macro_context()
                macro_adj, macro_desc = get_macro_risk_adjustment(macro)
                if macro_adj != 0.0:
                    score = clamp_score(score + macro_adj)
                    discovery = f"{discovery} | 宏观:{macro.get('summary', '')}"
                    if macro_desc:
                        discovery = f"{discovery}({macro_desc[:40]})"

                # P5b: 精细化宏观调整 — 经济事件临近
                try:
                    from economic_calendar import get_next_event
                    _nxt = get_next_event()
                    if _nxt and _nxt.get("days_until", 99) <= 3 and _nxt.get("type") == "fomc":
                        score = clamp_score(score - 0.5)
                        discovery = f"{discovery} | ⚠️ FOMC {_nxt['days_until']}天后"
                except ImportError:
                    pass
                except Exception as e:
                    _log.debug("P5b economic_calendar 失败: %s", e)

                # P5c: 收益率曲线调整
                _yc = macro.get("yield_curve", "unknown")
                if _yc == "inverted":
                    score = clamp_score(score - 0.8)
                    discovery = f"{discovery} | ⚠️ 收益率曲线倒挂"
                elif _yc == "flat":
                    score = clamp_score(score - 0.3)

                # P5d: 板块轮动调整
                _sr = macro.get("sector_rotation", {})
                _t_etf = get_sector_etf_for_ticker(ticker)
                if _t_etf and _sr:
                    _hot_etfs = [h[0] for h in _sr.get("hot", [])]
                    _cold_etfs = [c[0] for c in _sr.get("cold", [])]
                    if _t_etf in _hot_etfs:
                        score = clamp_score(score + 0.3)
                        discovery = f"{discovery} | 板块顺风"
                    elif _t_etf in _cold_etfs:
                        score = clamp_score(score - 0.3)
                        discovery = f"{discovery} | 板块逆风"

                # P5e: 黄金避险信号
                _gld = macro.get("gold_trend", "stable")
                if _gld == "surging":
                    score = clamp_score(score - 0.4)
                    discovery = f"{discovery} | ⚠️ 黄金避险飙升"
                elif _gld == "rising":
                    score = clamp_score(score - 0.2)
                    discovery = f"{discovery} | 黄金走强"
            except LLM_ERRORS as e:
                _log.debug("P5 fred_macro 不可用 %s: %s", ticker, e)

            # ── P1: LLM 冲突合成（识别哪种矛盾更危险，规则引擎只看一致性百分比）──
            llm_guard = None
            try:
                import llm_service
                if llm_service.is_available() and top_signals:
                    pheromone_snap = self.board.snapshot()
                    ticker_snap = [e for e in pheromone_snap if e.get("ticker") == ticker]
                    if ticker_snap:
                        llm_guard = llm_service.synthesize_agent_conflicts(
                            ticker, ticker_snap, resonance
                        )
                        if llm_guard:
                            conflict_type = llm_guard.get("conflict_type", "coherent")
                            _VALID_CONFLICT = {"coherent", "minor_divergence", "major_conflict", "data_quality_issue"}
                            if conflict_type not in _VALID_CONFLICT:
                                _log.warning("GuardBee: unknown conflict_type '%s', fallback to 'coherent'", conflict_type)
                                conflict_type = "coherent"
                            guard_reason = llm_guard.get("guard_reasoning", "")
                            rec_action = llm_guard.get("recommended_action", "proceed")
                            if rec_action not in ("proceed", "caution", "avoid"):
                                rec_action = "proceed"
                            # risk_score 高 → 降低 guard 分（对蜂群总分施加保守修正）
                            if conflict_type == "major_conflict":
                                score = clamp_score(score * 0.75)
                                direction = "neutral"
                            elif conflict_type == "minor_divergence":
                                score = clamp_score(score * 0.9)
                            if guard_reason:
                                discovery = f"{discovery} | LLM冲突检测:{guard_reason}"
                            if rec_action == "avoid":
                                discovery = f"{discovery}[⚠建议回避]"
            except LLM_ERRORS as e:
                _log.debug("GuardBeeSentinel LLM unavailable for %s: %s", ticker, e)

            self._publish(ticker, discovery, "guard_bee_sentinel", round(score, 2), direction,
                         details={
                             "consistency": consistency,
                             "crowding_adj": adj_factor,
                             "conflict_type": llm_guard.get("conflict_type", "") if llm_guard else "",
                             "resonance_detected": resonance.get("resonance_detected", False),
                         })

            # Phase 2: confidence = 信号板有数据 + 一致性高 + LLM 冲突评估
            confidence = build_confidence(0.4, [
                (bool(top_signals), 0.25),
                (consistency >= 0.7, 0.15),
                (resonance["resonance_detected"], 0.1),
                (bool(llm_guard), 0.1),
            ])

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="GuardBeeSentinel",
                dimension="risk_adj",
                data_quality={
                    "pheromone_board": "real",
                    "crowding": "real",
                    "llm_conflict": "llm_enhanced" if llm_guard else "rule_only",
                },
                details={
                    "resonance": resonance,
                    "top_signals_count": len(top_signals),
                    "consistency": consistency,
                    "adjustment_factor": adj_factor,
                    "llm_conflict_type": llm_guard.get("conflict_type", "") if llm_guard else "",
                    "llm_recommended_action": llm_guard.get("recommended_action", "") if llm_guard else "",
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("GuardBeeSentinel failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("GuardBeeSentinel", "risk_adj", e)
