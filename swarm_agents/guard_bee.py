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

            # ── 宏观环境统一调整（替代 P5a~P5f 零散 if-else，有 ±1.5 上限保护）──
            macro_result = self._calc_macro_adjustment(ticker)
            macro_adj = macro_result["score_adj"]
            macro_desc = macro_result["macro_summary"]
            if macro_adj != 0.0:
                score = clamp_score(score + macro_adj)
            if macro_result["signals"]:
                discovery = f"{discovery} | {macro_desc}"
            vix_term = macro_result["details"]  # 兼容后续 details 字段

            # ── P6: 时间周期意识 + 市场政体识别（③④）──────────────────────────────────
            cycle_ctx: dict = {}
            regime_ctx: dict = {}
            try:
                from market_intelligence import get_cycle_context, detect_market_regime
                from datetime import date as _date_mi
                cycle_ctx = get_cycle_context(_date_mi.today(), earnings_date=None, fomc_dates=[])
                regime_ctx = detect_market_regime(ticker)
                # Regime 评分修正
                _regime = regime_ctx.get("overall_regime", "")
                if _regime == "risk_off":
                    score = clamp_score(score - 0.5)
                    discovery = f"{discovery} | ⚠️ Regime:风险规避"
                elif _regime == "risk_on":
                    score = clamp_score(score + 0.2)
                    discovery = f"{discovery} | Regime:风险偏好"
                # 时间周期注记（Opex/FOMC/财报后窗口）
                _cycle_label = cycle_ctx.get("cycle_label", "")
                if _cycle_label and _cycle_label != "normal":
                    discovery = f"{discovery} | 周期:{_cycle_label}"
                if cycle_ctx.get("is_opex_week"):
                    score = clamp_score(score - 0.3)
                    discovery = f"{discovery} | ⚠️ Opex周(Charm/Vanna压力)"
            except Exception as _e_p6:
                _log.debug("P6 market_intelligence cycle/regime 不可用: %s", _e_p6)

            # ── P7: 信号拥挤度元指数（⑦）────────────────────────────────────────────
            signal_crowding_data: dict = {}
            try:
                from market_intelligence import calculate_signal_crowding
                # 用蜂群方向近似 options_flow（bull→低P/C, bear→高P/C）
                _flow_approx = 1.0 + (-0.2 if direction == "bullish" else 0.2 if direction == "bearish" else 0.0)
                signal_crowding_data = calculate_signal_crowding(
                    reddit_rank=50,           # 默认中性，无直接来源
                    analyst_consensus=3.5,    # 默认中性
                    options_flow=_flow_approx,
                    swarm_direction=direction,
                    scout_discovery=discovery,
                )
                _alpha_decay = signal_crowding_data.get("alpha_decay_factor", 1.0)
                if _alpha_decay < 0.85:
                    score = clamp_score(score * _alpha_decay)
                    discovery = f"{discovery} | ⚠️ 拥挤衰减×{_alpha_decay:.2f}"
            except Exception as _e_p7:
                _log.debug("P7 signal_crowding 不可用: %s", _e_p7)

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
                    "vix_term_structure": vix_term,
                    "macro_adj": macro_adj,
                    "macro_desc": macro_desc,
                    "macro_regime": macro_result.get("regime", "unknown"),
                    "macro_signals": macro_result.get("signals", []),
                    "macro_regime_votes": macro_result.get("regime_votes", {}),
                    # ③ 时间周期
                    "cycle_context": cycle_ctx,
                    # ④ 市场政体
                    "market_regime": regime_ctx,
                    # ⑦ 信号拥挤度
                    "signal_crowding": signal_crowding_data,
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("GuardBeeSentinel failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("GuardBeeSentinel", "risk_adj", e)

    # ---------- macro adjustment helper ----------

    def _calc_macro_adjustment(self, ticker: str) -> dict:
        """
        统一宏观环境调整（替代 P5a~P5f 的零散 if-else）

        流程：
        1. 收集所有宏观信号（VIX / 收益率曲线 / 黄金 / FOMC / VIX期限结构 / 板块轮动）
        2. 用信号投票判定 regime（risk_on / neutral / risk_off）
        3. 输出一个统一的评分调整量（有上限保护）

        与原来的区别：
        - 原来：6 个独立 if-else 各自 ±0.3~0.8，叠加无上限 → 最坏 -3.1
        - 现在：先合成 regime，再一次性调整 → 最大 ±1.5

        返回:
            {
                "regime": str,           # "risk_on" / "neutral" / "risk_off"
                "score_adj": float,      # -1.5 ~ +0.5
                "signals": list[str],    # 触发的信号描述
                "macro_summary": str,    # 一行摘要
                "details": dict,         # 原始指标值
                "regime_votes": dict,    # 投票明细
            }
        """
        signals = []
        regime_votes = {"risk_on": 0, "neutral": 0, "risk_off": 0}
        details = {}

        # ===== 1. FRED 宏观数据 =====
        macro = {}
        try:
            from fred_macro import get_macro_context, get_sector_etf_for_ticker
            macro = get_macro_context()
        except (ImportError, Exception) as e:
            _log.debug("fred_macro 不可用: %s", e)

        # VIX
        vix = macro.get("vix")
        if vix is not None:
            details["vix"] = vix
            if vix > 35:
                regime_votes["risk_off"] += 3
                signals.append(f"VIX {vix:.1f}（恐慌）")
            elif vix > 25:
                regime_votes["risk_off"] += 1
                signals.append(f"VIX {vix:.1f}（偏高）")
            elif vix < 15:
                regime_votes["risk_on"] += 1

        # 收益率曲线
        yc = macro.get("yield_curve", "unknown")
        details["yield_curve"] = yc
        if yc == "inverted":
            regime_votes["risk_off"] += 2
            signals.append("收益率曲线倒挂")
        elif yc == "flat":
            regime_votes["risk_off"] += 1

        # 黄金避险
        gld = macro.get("gold_trend", "stable")
        details["gold_trend"] = gld
        if gld == "surging":
            regime_votes["risk_off"] += 2
            signals.append("黄金飙升（避险需求强）")
        elif gld == "rising":
            regime_votes["risk_off"] += 1
            signals.append("黄金走强")

        # ===== 2. FOMC 临近 =====
        try:
            from economic_calendar import get_next_event
            nxt = get_next_event()
            if nxt and nxt.get("days_until", 99) <= 3 and nxt.get("type") == "fomc":
                regime_votes["risk_off"] += 1
                signals.append(f"FOMC {nxt['days_until']}天后")
                details["fomc_days"] = nxt["days_until"]
        except (ImportError, Exception):
            pass

        # ===== 3. VIX 期限结构 =====
        try:
            from vix_term_structure import get_vix_term_structure
            vix_term = get_vix_term_structure()
            structure = vix_term.get("structure", "")
            details["vix_term"] = structure
            if structure == "backwardation":
                regime_votes["risk_off"] += 2
                signals.append("VIX Backwardation（恐慌结构）")
            elif structure == "steep_contango":
                regime_votes["risk_on"] += 1
        except (ImportError, Exception):
            pass

        # ===== 4. 板块轮动 =====
        try:
            from fred_macro import get_sector_etf_for_ticker
            sr = macro.get("sector_rotation", {})
            t_etf = get_sector_etf_for_ticker(ticker)
            if t_etf and sr:
                hot_etfs = [h[0] for h in sr.get("hot", [])]
                cold_etfs = [c[0] for c in sr.get("cold", [])]
                if t_etf in hot_etfs:
                    regime_votes["risk_on"] += 1
                    signals.append("板块顺风")
                    details["sector_rotation"] = "hot"
                elif t_etf in cold_etfs:
                    regime_votes["risk_off"] += 1
                    signals.append("板块逆风")
                    details["sector_rotation"] = "cold"
        except (ImportError, Exception):
            pass

        # ===== 5. 判定 regime =====
        regime = max(regime_votes, key=regime_votes.get)
        # 平局时偏保守（neutral 而非 risk_on）
        if regime_votes["risk_on"] == regime_votes["risk_off"]:
            regime = "neutral"
        # risk_off 需要至少 2 票才成立（避免单信号误判）
        if regime == "risk_off" and regime_votes["risk_off"] < 2:
            regime = "neutral"

        # ===== 6. 统一评分调整（有上限保护）=====
        REGIME_ADJ = {
            "risk_on": +0.5,
            "neutral": 0.0,
            "risk_off": -1.0,
        }
        score_adj = REGIME_ADJ.get(regime, 0.0)

        # risk_off 信号越多，惩罚越重（但有上限 -1.5）
        if regime == "risk_off":
            extra = min(0.5, (regime_votes["risk_off"] - 2) * 0.25)
            score_adj = max(-1.5, score_adj - extra)

        macro_summary = f"Macro:{regime}"
        if signals:
            macro_summary += f"({'; '.join(signals[:2])})"

        return {
            "regime": regime,
            "score_adj": round(score_adj, 2),
            "signals": signals,
            "macro_summary": macro_summary,
            "details": details,
            "regime_votes": dict(regime_votes),
        }
