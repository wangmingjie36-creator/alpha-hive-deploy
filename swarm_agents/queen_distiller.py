"""QueenDistiller - 王后蒸馏蜂（5 维加权评分 + LLM 蒸馏）"""

from collections import defaultdict
from typing import Any, Dict, List, Optional
from pheromone_board import PheromoneBoard
from models import DataQualityChecker as _DQChecker
from swarm_agents._config import _log
from swarm_agents.cache import _safe_score
from swarm_agents.utils import LLM_ERRORS

# ML 特征 → 蜂群维度映射（Enhancement C: ML 反馈权重）
FEATURE_TO_DIMENSION: Dict[str, Optional[str]] = {
    "crowding": "signal",
    "catalyst": "catalyst",
    "momentum": "sentiment",
    "sentiment": "sentiment",
    "volatility": "risk_adj",
    "iv_rank": "odds",
    "put_call_ratio": "odds",
    "odds_score": "odds",
    "risk_adj_score": "risk_adj",
    "final_score": None,        # 元特征，不映射
    "agent_agreement": None,    # 元特征，不映射
    "direction_encoded": None,  # 元特征，不映射
}



class QueenDistiller:
    """
    王后蒸馏蜂 - 5 维加权评分 + 共振增强 + 多数投票 + LLM 推理

    双引擎架构：
    1. 规则引擎（始终运行）：加权评分 + 共振 + 投票 → base_score
    2. LLM 引擎（有 API Key 时启用）：Claude 分析推理 → 调整评分 + 生成推理链

    Opportunity Score = 0.30×Signal + 0.20×Catalyst + 0.20×Sentiment + 0.15×Odds + 0.15×RiskAdj
    """

    DEFAULT_WEIGHTS = {
        "signal":    0.30,
        "catalyst":  0.20,
        "sentiment": 0.20,
        "odds":      0.15,
        "risk_adj":  0.15,
    }

    # 数据质量源分类契约（_apply_triple_penalty 评分用）
    # REAL_SOURCES (1.0)：API 调用成功 / 文件加载成功 / 真实数据
    # PROXY_SOURCES (0.7)：降级/代理数据，仍有参考价值
    # 其他 (0.0)：未分类 — 表示遗漏了分类，应视为 Bug
    # → 新增 agent data_quality 值时，务必加入对应集合
    REAL_SOURCES = {
        "real", "yfinance", "finviz_api", "options_api",
        "keyword", "llm_enhanced", "reddit_apewisdom",
        "rule_only", "sec_api", "SEC直查", "Finviz", "finviz",
        "loaded", "empty",  # ChronosBee: catalysts.json 加载成功 / 日历查询成功但无事件
    }
    PROXY_SOURCES = {
        "proxy_volume", "proxy_momentum", "proxy_social",
        "pheromone_board", "unavailable",
        "fallback", "fallback_momentum", "default", "missing",  # 降级回退仍有参考价值
    }

    def __init__(self, board: PheromoneBoard, weight_manager=None, adapted_weights: Dict = None,
                 enable_llm: bool = True, ml_model=None):
        self.board = board
        self.weight_manager = weight_manager
        self.enable_llm = enable_llm
        self.ml_model = ml_model
        if adapted_weights:
            self.DIMENSION_WEIGHTS = adapted_weights
        else:
            # 优先从 config.EVALUATION_WEIGHTS 读取，确保权重配置单一入口
            try:
                from config import EVALUATION_WEIGHTS
                valid_dims = set(self.DEFAULT_WEIGHTS.keys())
                # 只保留已实现的维度（防止 config 中存在未对应 Agent 的维度）
                cfg_weights = {k: v for k, v in EVALUATION_WEIGHTS.items() if k in valid_dims}
                # 用 DEFAULT_WEIGHTS 补全缺失维度
                merged = dict(self.DEFAULT_WEIGHTS)
                merged.update(cfg_weights)
                self.DIMENSION_WEIGHTS = merged
            except (ImportError, AttributeError):
                self.DIMENSION_WEIGHTS = dict(self.DEFAULT_WEIGHTS)

        # Enhancement C: ML 反馈权重调整
        self.ml_adjustments: Dict[str, float] = {}
        self.ml_feedback_enabled = False
        if ml_model:
            self.ml_adjustments = self._compute_ml_weight_adjustments()
            if self.ml_adjustments:
                self.ml_feedback_enabled = True
                try:
                    from config import ML_FEEDBACK_CONFIG as _MFC
                except ImportError:
                    _MFC = {}
                if _MFC.get("enable_dimension_weighting", True):
                    for dim, factor in self.ml_adjustments.items():
                        if dim in self.DIMENSION_WEIGHTS:
                            self.DIMENSION_WEIGHTS[dim] *= factor
                    # 归一化使权重总和 = 1.0
                    _total = sum(self.DIMENSION_WEIGHTS.values())
                    if _total > 0:
                        self.DIMENSION_WEIGHTS = {
                            k: round(v / _total, 4)
                            for k, v in self.DIMENSION_WEIGHTS.items()
                        }
                    _log.info(
                        "[ML-Feedback] 维度权重已调整: %s",
                        {k: round(v, 3) for k, v in self.DIMENSION_WEIGHTS.items()},
                    )

    def _compute_ml_weight_adjustments(self) -> Dict[str, float]:
        """Enhancement C: 从 ML 模型特征重要性计算维度调整因子。

        Returns:
            {dimension: adjustment_factor} 例如 {"signal": 1.3, "odds": 0.8}
            空 dict 表示无 ML 模型或模型未训练。
        """
        if not self.ml_model or not hasattr(self.ml_model, "get_feature_importance"):
            return {}
        importance = self.ml_model.get_feature_importance()
        if not importance:
            return {}

        try:
            from config import ML_FEEDBACK_CONFIG as _MFC
        except ImportError:
            _MFC = {}
        _min_adj = _MFC.get("min_adjustment", 0.5)
        _max_adj = _MFC.get("max_adjustment", 2.0)

        # 1. 聚合特征重要性到维度
        dim_importance: Dict[str, List[float]] = defaultdict(list)
        for feat, info in importance.items():
            dim = FEATURE_TO_DIMENSION.get(feat)
            if dim:
                dim_importance[dim].append(info["weight"])

        if not dim_importance:
            return {}

        # 2. 维度平均重要性 → 调整因子
        # 基准：如果 5 个维度权重均等，则每个维度的特征重要性应约为 1/5 = 0.20
        _baseline = 1.0 / len(self.DEFAULT_WEIGHTS)  # 0.20
        dim_adjustments: Dict[str, float] = {}
        for dim, weights in dim_importance.items():
            avg = sum(weights) / len(weights)
            adjustment = avg / _baseline if _baseline > 0 else 1.0
            adjustment = max(_min_adj, min(_max_adj, adjustment))
            dim_adjustments[dim] = round(adjustment, 3)

        return dim_adjustments

    @staticmethod
    def _polish_narrative(ticker: str, raw_narrative: str, score: float, direction: str) -> str:
        """P3: 用 LLM 润色叙事文本（失败时返回原文）"""
        if not raw_narrative:
            return raw_narrative
        try:
            import llm_service
            if llm_service.is_available():
                polished = llm_service.polish_briefing_narrative(
                    ticker, raw_narrative, score, direction,
                )
                if polished and len(polished) >= 10:
                    return polished
        except LLM_ERRORS:
            pass
        return raw_narrative

    # ---------- distill helper methods ----------

    def _prepare_dimension_data(self, agent_results: List[Dict]) -> Dict:
        """过滤有效结果、按维度分组、覆盖度计算。返回 dict。"""
        _dq = _DQChecker()
        cleaned_results = _dq.clean_results_batch(agent_results)
        valid_results = [r for r in cleaned_results if "error" not in r]
        all_results = cleaned_results

        dim_scores = {}
        dim_confidence = {}
        for r in valid_results:
            dim = r.get("dimension", "")
            if dim in self.DIMENSION_WEIGHTS:
                dim_scores[dim] = _safe_score(r.get("score"), 5.0, 0, 10, f"dim_{dim}")
                dim_confidence[dim] = _safe_score(r.get("confidence"), 0.5, 0, 1.0, f"conf_{dim}")

        dim_status: Dict[str, str] = {}
        dim_missing_reason: Dict[str, str] = {}
        for dim in self.DIMENSION_WEIGHTS:
            if dim in dim_scores:
                dim_status[dim] = "present"
            else:
                error_result = next(
                    (r for r in all_results if r.get("dimension") == dim and "error" in r), None
                )
                if error_result:
                    dim_status[dim] = "error"
                    dim_missing_reason[dim] = str(error_result["error"])[:80]
                else:
                    dim_status[dim] = "absent"
                    dim_missing_reason[dim] = "Agent 未返回该维度"
        present_count = sum(1 for s in dim_status.values() if s == "present")
        _n_dims = len(self.DIMENSION_WEIGHTS)
        dimension_coverage_pct = round(present_count / _n_dims * 100, 1) if _n_dims else 100.0

        return {
            "valid_results": valid_results,
            "all_results": all_results,
            "dim_scores": dim_scores,
            "dim_confidence": dim_confidence,
            "dim_status": dim_status,
            "dim_missing_reason": dim_missing_reason,
            "dimension_coverage_pct": dimension_coverage_pct,
            "present_count": present_count,
        }

    def _compute_weighted_score(self, ticker: str, dim_scores: Dict,
                                dim_confidence: Dict,
                                dimension_coverage_pct: float,
                                present_count: int,
                                valid_results: List[Dict]) -> Dict:
        """ML 调整 + 5D 加权 + 覆盖度压缩 + 共振增强。返回 dict。"""
        ml_adjustment = 0.0
        for r in valid_results:
            if r.get("dimension") == "ml_auxiliary":
                ml_score = _safe_score(r.get("score", 5.0), default=5.0, lo=0.0, hi=10.0, label="ml_score")
                ml_conf = _safe_score(r.get("confidence", 0.5), default=0.5, lo=0.0, hi=1.0, label="ml_conf")
                ml_adjustment = (ml_score - 5.0) * 0.1 * ml_conf

        _n_dims = len(self.DIMENSION_WEIGHTS)
        _missing_dim_fill = 4.7 if dimension_coverage_pct < 80.0 else 5.0

        weighted_sum = 0.0
        weight_total = 0.0
        for dim, weight in self.DIMENSION_WEIGHTS.items():
            if dim in dim_scores:
                conf = dim_confidence.get(dim, 0.5)
                effective_weight = weight * min(1.0, conf * 2)
                weighted_sum += dim_scores[dim] * effective_weight
                weight_total += effective_weight
            else:
                weighted_sum += _missing_dim_fill * weight
                weight_total += weight

        base_score = weighted_sum / weight_total if weight_total > 0 else 5.0

        coverage_warning = ""
        if dimension_coverage_pct < 60.0:
            _pre_compress = base_score
            base_score = round(5.0 + (base_score - 5.0) * 0.1, 2)
            coverage_warning = (
                f"仅 {present_count}/{_n_dims} 维度可用，"
                f"分数已压缩至中性区间（{_pre_compress:.2f}→{base_score:.2f}）"
            )
            _log.warning("%s %s", ticker, coverage_warning)
        elif dimension_coverage_pct < 80.0:
            coverage_warning = f"仅 {present_count}/{_n_dims} 维度可用，缺失维度施加 -0.3 惩罚"

        adjusted_score = base_score + ml_adjustment

        resonance = self.board.detect_resonance(ticker)
        if resonance["resonance_detected"]:
            boost_pct = _safe_score(resonance.get("confidence_boost"), 0.0, -50, 50, "resonance_boost")
            rule_score = adjusted_score * (1.0 + boost_pct / 100.0)
        else:
            rule_score = adjusted_score

        rule_score = round(max(0.0, min(10.0, rule_score)), 2)

        return {
            "base_score": base_score,
            "adjusted_score": adjusted_score,
            "rule_score": rule_score,
            "ml_adjustment": ml_adjustment,
            "resonance": resonance,
            "coverage_warning": coverage_warning,
        }

    def _apply_triple_penalty(self, ticker: str, rule_score: float,
                              valid_results: List[Dict]) -> Dict:
        """DQ 压缩 → Guard 关门 → Bear 上限 → 组合帽。返回 dict。"""
        pre_penalty_score = rule_score

        _qs_early = 0.0
        _tf_early = 0
        for r in valid_results:
            _dq_e = r.get("data_quality", {})
            if isinstance(_dq_e, dict):
                for v in _dq_e.values():
                    _tf_early += 1
                    if v in self.REAL_SOURCES:
                        _qs_early += 1.0
                    elif v in self.PROXY_SOURCES:
                        _qs_early += 0.7
        data_real_pct = round(_qs_early / _tf_early * 100, 1) if _tf_early > 0 else 0.0
        data_real_pct = _safe_score(data_real_pct, 50.0, 0, 100, "data_real_pct")

        # 步骤 1/3: 数据质量压缩
        dq_penalty_applied = False
        quality_factor = 1.0
        if data_real_pct < 80.0:
            quality_factor = round(0.5 + 0.5 * (data_real_pct / 80.0), 3)
            pre_dq = rule_score
            rule_score = round(5.0 + (rule_score - 5.0) * quality_factor, 2)
            rule_score = max(0.0, min(10.0, rule_score))
            if abs(rule_score - pre_dq) >= 0.05:
                dq_penalty_applied = True
                _log.info(
                    "%s [S1-1/3 DQ] real_pct=%.1f%% factor=%.3f %.2f→%.2f",
                    ticker, data_real_pct, quality_factor, pre_dq, rule_score,
                )
        score_after_dq = rule_score

        # 步骤 2/3: GuardBee 风险关门
        guard_result = next(
            (r for r in valid_results if r.get("dimension") == "risk_adj"), None
        )
        guard_penalty = 0.0
        guard_penalty_applied = False
        if guard_result is not None:
            guard_score = _safe_score(guard_result.get("score"), 5.0, 0, 10, "guard_score")
            if guard_score < 4.0:
                guard_penalty = round((4.0 - guard_score) / 4.0 * 0.8, 3)
                pre_guard = rule_score
                rule_score = round(max(rule_score - guard_penalty, 2.0), 2)
                if rule_score < pre_guard:
                    guard_penalty_applied = True
                    _log.info(
                        "%s [S1-2/3 Guard] guard=%.1f penalty=%.3f %.2f→%.2f",
                        ticker, guard_score, guard_penalty, pre_guard, rule_score,
                    )
        score_after_guard = rule_score

        # 步骤 3/3: BearBee 看空上限
        try:
            from config import BEAR_SCORING_CONFIG as _BSC3
        except ImportError:
            _BSC3 = {}
        _bear_cap_thresh = _BSC3.get("bear_cap_trigger_threshold", 5.0)
        _bear_cap_slope = _BSC3.get("bear_cap_slope", 0.5)
        contrarian_result = next(
            (r for r in valid_results if r.get("dimension") == "contrarian"), None
        )
        bear_strength = 0.0
        bear_cap_applied = False
        if contrarian_result is not None:
            bear_strength = round(10.0 - contrarian_result.get("score", 5.0), 2)
            if bear_strength > _bear_cap_thresh:
                bear_cap = round(10.0 - (bear_strength - _bear_cap_thresh) * _bear_cap_slope, 2)
                if rule_score > bear_cap:
                    _log.info(
                        "%s [S1-3/3 Bear] strength=%.1f cap=%.2f（原 %.2f）",
                        ticker, bear_strength, bear_cap, rule_score,
                    )
                    rule_score = bear_cap
                    bear_cap_applied = True
        score_after_bear = rule_score

        # 组合惩罚上限
        total_penalty = round(pre_penalty_score - rule_score, 2)
        combo_cap_applied = False
        if total_penalty > 2.0:
            rule_score = round(max(pre_penalty_score - 2.0, 2.0), 2)
            combo_cap_applied = True
            _log.info(
                "%s [S1-Combo] 总惩罚 %.2f 超限 → 截断至 -2.0，%.2f→%.2f",
                ticker, total_penalty, pre_penalty_score, rule_score,
            )

        return {
            "rule_score": rule_score,
            "pre_penalty_score": pre_penalty_score,
            "score_after_dq": score_after_dq,
            "score_after_guard": score_after_guard,
            "score_after_bear": score_after_bear,
            "total_penalty": total_penalty,
            "combo_cap_applied": combo_cap_applied,
            "dq_penalty_applied": dq_penalty_applied,
            "quality_factor": quality_factor,
            "data_real_pct": data_real_pct,
            "guard_penalty": guard_penalty,
            "guard_penalty_applied": guard_penalty_applied,
            "bear_strength": bear_strength,
            "bear_cap_applied": bear_cap_applied,
            "contrarian_result": contrarian_result,
        }

    def _compute_confidence_calibration(self, final_score: float,
                                        dim_scores: Dict,
                                        vote_result: Dict,
                                        present_count: int) -> Dict:
        """Enhancement B: 置信度校准 — 基于维度分散度计算置信区间。

        Returns:
            dict with confidence_band, band_width, discrimination, dimension_std
        """
        import statistics as _stats

        try:
            from config import CONFIDENCE_CALIBRATION_CONFIG as _CCC
        except ImportError:
            _CCC = {}
        _std_mult = _CCC.get("std_multiplier", 0.3)
        _low_cov_thresh = _CCC.get("low_coverage_threshold", 3)
        _cov_amp = _CCC.get("coverage_amplifier", 1.5)
        _conf_amp = _CCC.get("conflict_amplifier", 1.3)
        _max_band = _CCC.get("max_band", 2.0)

        values = list(dim_scores.values())
        dim_std = _stats.stdev(values) if len(values) > 1 else 0.0

        band_width = dim_std * _std_mult

        # 放大因子
        if present_count < _low_cov_thresh:
            band_width *= _cov_amp
            # 当维度 ≤ 1 时 stdev=0（乘法放大无效），但缺乏数据本身意味着高不确定性
            if len(values) <= 1:
                band_width = max(band_width, 1.0)
        if vote_result.get("conflict_info", {}).get("conflict_discount", 0) > 0:
            band_width *= _conf_amp

        band_width = min(band_width, _max_band)

        # 置信区间
        confidence_band = (
            round(max(0.0, final_score - band_width), 2),
            round(min(10.0, final_score + band_width), 2),
        )

        # 区分度标签
        if band_width < 0.5:
            discrimination = "high"
        elif band_width < 1.2:
            discrimination = "medium"
        else:
            discrimination = "low"

        return {
            "confidence_band": confidence_band,
            "band_width": round(band_width, 2),
            "discrimination": discrimination,
            "dimension_std": round(dim_std, 2),
        }

    def _compute_direction_vote(self, ticker: str, valid_results: List[Dict],
                                all_results: List[Dict],
                                rule_score: float) -> Dict:
        """S4 反博弈 + S5 冲突再投票 + data_quality 汇总。返回 dict。"""
        directions = [r.get("direction", "neutral") for r in valid_results]
        bullish_count = directions.count("bullish")
        bearish_count = directions.count("bearish")
        neutral_count = directions.count("neutral")

        _all_conf = [r.get("confidence", 0.5) for r in valid_results]
        _total_w_raw = sum(_all_conf) or 1.0
        _weight_cap = _total_w_raw * 0.4

        # Enhancement C: ML 反馈 → Agent 投票置信度调整
        try:
            from config import ML_FEEDBACK_CONFIG as _MFC_vote
        except ImportError:
            _MFC_vote = {}
        _ml_vote_boost_enabled = (
            self.ml_feedback_enabled
            and _MFC_vote.get("enable_vote_boosting", True)
            and self.ml_adjustments
        )

        def _effective_conf(r):
            conf = min(r.get("confidence", 0.5), _weight_cap)
            if _ml_vote_boost_enabled:
                from pheromone_board import PheromoneBoard as _PB
                _agent_dim = _PB.AGENT_DIMENSIONS.get(r.get("source", ""))
                if _agent_dim:
                    conf *= self.ml_adjustments.get(_agent_dim, 1.0)
            return conf

        bullish_w = sum(_effective_conf(r) for r in valid_results if r.get("direction") == "bullish")
        bearish_w = sum(_effective_conf(r) for r in valid_results if r.get("direction") == "bearish")
        neutral_w = sum(_effective_conf(r) for r in valid_results if r.get("direction") == "neutral")
        total_w = bullish_w + bearish_w + neutral_w or 1.0

        try:
            from config import BEAR_SCORING_CONFIG as _BSC4
        except ImportError:
            _BSC4 = {}
        _bear_min_agents = _BSC4.get("voting_bearish_min_agents", 1)
        _bear_min_wpct = _BSC4.get("voting_bearish_min_weight_pct", 0.25)

        if bullish_w > bearish_w and bullish_w / total_w >= 0.4 and bullish_count >= 2:
            rule_direction = "bullish"
        elif bearish_w > bullish_w and bearish_w / total_w >= _bear_min_wpct and bearish_count >= _bear_min_agents:
            rule_direction = "bearish"
        else:
            rule_direction = "neutral"

        # S4.5: 冲突仲裁 — 票差过小时提升 GuardBee/BearBee 异议权重
        try:
            from config import CONFLICT_ARBITRATION_CONFIG as _CAC
        except ImportError:
            _CAC = {}
        _close_vote_thresh = _CAC.get("close_vote_threshold", 0.15)
        _dissent_boost = _CAC.get("dissent_boost", 1.5)
        _dissent_agents = set(_CAC.get("dissent_agents", ["GuardBeeSentinel", "BearBeeContrarian"]))

        _pre_arb_margin = abs(bullish_w - bearish_w) / total_w if total_w > 0 else 0.0
        _arb_triggered = False
        _arb_flipped = False

        if _pre_arb_margin < _close_vote_thresh and (bullish_count >= 1 and bearish_count >= 1):
            _arb_triggered = True
            _pre_arb_direction = rule_direction
            # 重新计算加权票，基于 ML 提升后的基础置信度 + 异议方 dissent_agents 额外 boost
            _arb_bull_w = 0.0
            _arb_bear_w = 0.0
            _arb_neut_w = 0.0
            for r in valid_results:
                _dir = r.get("direction", "neutral")
                _conf = _effective_conf(r)  # 使用 ML 提升后的置信度作为基础
                _src = r.get("source", "")
                # 如果该 Agent 是 dissent_agent 且投的方向与当前多数方向相反，提升其权重
                if _src in _dissent_agents:
                    if (rule_direction == "bullish" and _dir == "bearish") or \
                       (rule_direction == "bearish" and _dir == "bullish") or \
                       (rule_direction == "neutral"):
                        _conf = _conf * _dissent_boost
                if _dir == "bullish":
                    _arb_bull_w += _conf
                elif _dir == "bearish":
                    _arb_bear_w += _conf
                else:
                    _arb_neut_w += _conf

            # 重新判定方向
            _arb_total = _arb_bull_w + _arb_bear_w + _arb_neut_w or 1.0
            if _arb_bull_w > _arb_bear_w and _arb_bull_w / _arb_total >= 0.4 and bullish_count >= 2:
                rule_direction = "bullish"
            elif _arb_bear_w > _arb_bull_w and _arb_bear_w / _arb_total >= _bear_min_wpct and bearish_count >= _bear_min_agents:
                rule_direction = "bearish"
            else:
                rule_direction = "neutral"

            if rule_direction != _pre_arb_direction:
                _arb_flipped = True
                _log.info(
                    "%s [S4.5-Arb] 仲裁翻转: %s→%s (margin %.3f < %.2f)",
                    ticker, _pre_arb_direction, rule_direction, _pre_arb_margin, _close_vote_thresh,
                )
            # 更新投票权重（用于返回值）
            bullish_w = _arb_bull_w
            bearish_w = _arb_bear_w
            neutral_w = _arb_neut_w
            total_w = _arb_total

        # S5: 冲突驱动增强
        from config import SENTIMENT_MOMENTUM_CONFIG as _SMC5
        _conflict_min = _SMC5.get("conflict_heavy_min_agents", 2)
        _conflict_resolve = _SMC5.get("conflict_dq_resolve_threshold", 0.55)
        _conflict_factor = _SMC5.get("conflict_discount_factor", 0.3)

        conflict_level = "none"
        conflict_info: Dict[str, Any] = {}

        if bullish_count >= _conflict_min and bearish_count >= _conflict_min:
            conflict_level = "heavy"
            dq_bull_w = 0.0
            dq_bear_w = 0.0
            for r in valid_results:
                _dir = r.get("direction", "neutral")
                if _dir not in ("bullish", "bearish"):
                    continue
                _dq = r.get("data_quality", {})
                _real = sum(1 for v in _dq.values() if v in self.REAL_SOURCES) if isinstance(_dq, dict) else 0
                _tf = max(1, len(_dq) if isinstance(_dq, dict) else 1)
                _dq_ratio = _real / _tf
                _conf = min(r.get("confidence", 0.5), _weight_cap)
                _combined = _conf * (0.5 + 0.5 * _dq_ratio)
                if _dir == "bullish":
                    dq_bull_w += _combined
                else:
                    dq_bear_w += _combined

            dq_total = dq_bull_w + dq_bear_w or 1.0
            if dq_bull_w / dq_total >= _conflict_resolve:
                rule_direction = "bullish"
            elif dq_bear_w / dq_total >= _conflict_resolve:
                rule_direction = "bearish"

            _conflict_ratio = (bullish_count + bearish_count) / max(1, len(valid_results))
            conflict_discount = round(_conflict_factor * min(1.0, _conflict_ratio), 2)
            rule_score = round(max(1.0, rule_score - conflict_discount), 2)

            conflict_info = {
                "conflict_level": "heavy",
                "bullish_agents": bullish_count,
                "bearish_agents": bearish_count,
                "dq_bull_weight": round(dq_bull_w, 3),
                "dq_bear_weight": round(dq_bear_w, 3),
                "resolved_direction": rule_direction,
                "conflict_discount": conflict_discount,
            }
            _log.info(
                "%s [S5-Conflict] 重度冲突 (%d多 vs %d空)，DQ加权→%s，折扣 %.2f",
                ticker, bullish_count, bearish_count, rule_direction, conflict_discount,
            )
        elif bullish_count >= 1 and bearish_count >= 1:
            conflict_level = "moderate"
            conflict_info = {
                "conflict_level": "moderate",
                "bullish_agents": bullish_count,
                "bearish_agents": bearish_count,
            }

        per_agent_directions = {}
        for r in all_results:
            src = r.get("source", "")
            if src:
                per_agent_directions[src] = r.get("direction", "neutral")

        data_quality_summary = {}
        for r in valid_results:
            dq = r.get("data_quality", {})
            if isinstance(dq, dict):
                src = r.get("source", "unknown")
                data_quality_summary[src] = dq

        _DIM_SOURCES = {
            "signal":    "ScoutBeeNova",
            "catalyst":  "ChronosBeeHorizon",
            "sentiment": "BuzzBeeWhisper",
            "odds":      "OracleBeeEcho",
            "risk_adj":  "GuardBeeSentinel",
        }
        dim_data_quality: Dict[str, Optional[float]] = {}
        for _dim, _src in _DIM_SOURCES.items():
            _qs = 0.0
            _tf = 0
            for r in valid_results:
                if r.get("source") == _src:
                    _dq = r.get("data_quality", {})
                    if isinstance(_dq, dict):
                        for v in _dq.values():
                            _tf += 1
                            if v in self.REAL_SOURCES:
                                _qs += 1.0
                            elif v in self.PROXY_SOURCES:
                                _qs += 0.7
            dim_data_quality[_dim] = round(_qs / _tf * 100, 1) if _tf > 0 else None

        return {
            "rule_direction": rule_direction,
            "rule_score": rule_score,
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "neutral_count": neutral_count,
            "direction_vote_weights": {
                "bullish": round(bullish_w, 3),
                "bearish": round(bearish_w, 3),
                "neutral": round(neutral_w, 3),
            },
            "per_agent_directions": per_agent_directions,
            "conflict_level": conflict_level,
            "conflict_info": conflict_info,
            "data_quality_summary": data_quality_summary,
            "dim_data_quality": dim_data_quality,
            # S4.5 冲突仲裁
            "arbitration_triggered": _arb_triggered,
            "arbitration_flipped": _arb_flipped,
            "pre_arbitration_margin": round(_pre_arb_margin, 4),
        }

    def _run_llm_engine(self, ticker: str, valid_results: List[Dict],
                        all_results: List[Dict], dim_scores: Dict,
                        resonance: Dict, rule_score: float,
                        rule_direction: str, contrarian_result: Any,
                        conflict_level: str, conflict_info: Dict) -> Dict:
        """LLM 调用 + 分数混合 + 叙事生成 + agent_details 收集。返回 dict。"""
        llm_result = None
        reasoning = ""
        key_insight = ""
        risk_flag = ""
        llm_confidence = 0.0
        final_score = rule_score
        final_direction = rule_direction
        distill_mode = "rule_engine"

        if self.enable_llm:
            try:
                import llm_service
                if llm_service.is_available():
                    _buzz_details = {}
                    for _r in valid_results:
                        if _r.get("source") == "BuzzBeeWhisper":
                            _buzz_details = (_r.get("details") or {})
                            break
                    _sent_ctx = None
                    _sm = _buzz_details.get("sentiment_momentum")
                    _sd = _buzz_details.get("sentiment_divergence")
                    if _sm or _sd or conflict_level != "none":
                        _sent_ctx = {
                            "momentum_3d": (_sm or {}).get("delta_3d"),
                            "momentum_regime": (_sm or {}).get("momentum_regime", "unknown"),
                            "divergence_type": (_sd or {}).get("divergence_type", "none"),
                            "divergence_severity": (_sd or {}).get("severity", 0),
                            "conflict_level": conflict_level,
                            "conflict_info": conflict_info if conflict_info else None,
                        }

                    llm_result = llm_service.distill_with_reasoning(
                        ticker=ticker,
                        agent_results=valid_results,
                        dim_scores=dim_scores,
                        resonance=resonance,
                        rule_score=rule_score,
                        rule_direction=rule_direction,
                        bear_result=contrarian_result,
                        sentiment_context=_sent_ctx,
                    )
            except LLM_ERRORS as e:
                _log.warning("QueenDistiller LLM service unavailable: %s", e)

        narrative = ""
        bull_bear_synthesis = ""
        contrarian_view = ""

        if llm_result:
            distill_mode = "llm_enhanced"
            reasoning = llm_result.get("reasoning", "")
            key_insight = llm_result.get("key_insight", "")
            risk_flag = llm_result.get("risk_flag", "")
            llm_confidence = llm_result.get("confidence", 0.5)
            narrative = llm_result.get("narrative", "")
            bull_bear_synthesis = llm_result.get("bull_bear_synthesis", "")
            contrarian_view = llm_result.get("contrarian_view", "")

            llm_score = llm_result.get("final_score")
            llm_direction = llm_result.get("direction")

            if llm_score is not None and isinstance(llm_score, (int, float)):
                llm_weight = min(0.6, max(0.2, llm_confidence))
                if llm_direction != rule_direction and llm_confidence < 0.7:
                    llm_weight *= 0.5
                rule_weight = 1.0 - llm_weight
                final_score = round(rule_score * rule_weight + float(llm_score) * llm_weight, 2)
                final_score = max(0.0, min(10.0, final_score))

            if llm_direction in ("bullish", "bearish", "neutral"):
                if llm_direction == rule_direction:
                    final_direction = llm_direction
                elif llm_confidence >= 0.7:
                    final_direction = llm_direction

        agent_details = {}
        for r in all_results:
            src = r.get("source", "unknown")
            agent_details[src] = {
                "discovery": r.get("discovery", ""),
                "score": r.get("score", 5.0),
                "direction": r.get("direction", "neutral"),
                "confidence": r.get("confidence", 0.5),
                "dimension": r.get("dimension", ""),
                "details": r.get("details") or {},
            }
            if src == "BearBeeContrarian":
                agent_details[src]["llm_thesis"] = r.get("llm_thesis", "")
                agent_details[src]["llm_key_risks"] = r.get("llm_key_risks", [])
                agent_details[src]["llm_contrarian_insight"] = r.get("llm_contrarian_insight", "")
                agent_details[src]["llm_thesis_break"] = r.get("llm_thesis_break", "")

        return {
            "final_score": final_score,
            "final_direction": final_direction,
            "distill_mode": distill_mode,
            "reasoning": reasoning,
            "key_insight": key_insight,
            "risk_flag": risk_flag,
            "llm_confidence": llm_confidence,
            "narrative": narrative,
            "bull_bear_synthesis": bull_bear_synthesis,
            "contrarian_view": contrarian_view,
            "agent_details": agent_details,
        }

    def distill(self, ticker: str, agent_results: List[Dict]) -> Dict:
        """
        5 维加权评分 + 共振增强 + 多数投票 + LLM 推理蒸馏

        双引擎：规则引擎始终运行作为基础，LLM 引擎在可用时叠加推理。
        """
        # ===== 1. 维度数据准备 =====
        prep = self._prepare_dimension_data(agent_results)
        valid_results = prep["valid_results"]
        all_results = prep["all_results"]
        dim_scores = prep["dim_scores"]
        dim_confidence = prep["dim_confidence"]
        dim_status = prep["dim_status"]
        dim_missing_reason = prep["dim_missing_reason"]
        dimension_coverage_pct = prep["dimension_coverage_pct"]
        present_count = prep["present_count"]

        # ===== 2. 加权评分 + 共振 =====
        ws = self._compute_weighted_score(
            ticker, dim_scores, dim_confidence,
            dimension_coverage_pct, present_count, valid_results)
        adjusted_score = ws["adjusted_score"]
        rule_score = ws["rule_score"]
        ml_adjustment = ws["ml_adjustment"]
        resonance = ws["resonance"]
        coverage_warning = ws["coverage_warning"]

        # ===== 3. 三重惩罚 =====
        tp = self._apply_triple_penalty(ticker, rule_score, valid_results)
        rule_score = tp["rule_score"]
        contrarian_result = tp["contrarian_result"]

        # ===== 4. 方向投票 + 冲突 =====
        dv = self._compute_direction_vote(
            ticker, valid_results, all_results, rule_score)
        rule_direction = dv["rule_direction"]
        rule_score = dv["rule_score"]

        # ===== 5. LLM 引擎 =====
        llm = self._run_llm_engine(
            ticker, valid_results, all_results, dim_scores,
            resonance, rule_score, rule_direction,
            contrarian_result, dv["conflict_level"], dv["conflict_info"])

        final_score = llm["final_score"]
        final_direction = llm["final_direction"]

        # ===== 6. 置信度校准 =====
        confidence_calibration = self._compute_confidence_calibration(
            final_score, dim_scores, dv, present_count)

        return {
            "ticker": ticker,
            "final_score": final_score,
            "direction": final_direction,
            "resonance": resonance,
            "supporting_agents": len(valid_results),
            "agent_breakdown": {
                "bullish": dv["bullish_count"],
                "bearish": dv["bearish_count"],
                "neutral": dv["neutral_count"],
            },
            "agent_directions": dv["per_agent_directions"],
            "agent_details": llm["agent_details"],
            "dimension_scores": dim_scores,
            "dimension_confidence": dim_confidence,
            "dimension_weights": dict(self.DIMENSION_WEIGHTS),
            "ml_adjustment": round(ml_adjustment, 3),
            "ml_contribution_pct": round(abs(ml_adjustment) / max(abs(final_score), 0.01) * 100, 1),
            "base_score_before_resonance": round(adjusted_score, 2),
            "pheromone_compact": self.board.compact_snapshot(ticker),
            "data_quality": dv["data_quality_summary"],
            "data_real_pct": tp["data_real_pct"],
            "dim_data_quality": dv["dim_data_quality"],
            # Phase 1: LLM 推理增强
            "distill_mode": llm["distill_mode"],
            "reasoning": llm["reasoning"],
            "key_insight": llm["key_insight"],
            "risk_flag": llm["risk_flag"],
            "llm_confidence": llm["llm_confidence"],
            # Phase 2: 叙事增强（P3: LLM 润色）
            "narrative": self._polish_narrative(
                ticker, llm["narrative"], final_score, final_direction),
            "bull_bear_synthesis": llm["bull_bear_synthesis"],
            "contrarian_view": llm["contrarian_view"],
            "rule_score": rule_score,
            "rule_direction": rule_direction,
            "bear_strength": tp["bear_strength"],
            "bear_cap_applied": tp["bear_cap_applied"],
            "guard_penalty": tp["guard_penalty"],
            "guard_penalty_applied": tp["guard_penalty_applied"],
            "direction_vote_weights": dv["direction_vote_weights"],
            "dq_quality_factor": tp["quality_factor"],
            "dq_penalty_applied": tp["dq_penalty_applied"],
            # NA1: 维度状态可视化
            "dimension_status": dim_status,
            "dimension_missing_reason": dim_missing_reason,
            "dimension_coverage_pct": dimension_coverage_pct,
            # S1: 三重惩罚中间值追踪
            "pre_penalty_score": tp["pre_penalty_score"],
            "score_after_dq": tp["score_after_dq"],
            "score_after_guard": tp["score_after_guard"],
            "score_after_bear": tp["score_after_bear"],
            "total_penalty": tp["total_penalty"],
            "combo_cap_applied": tp["combo_cap_applied"],
            # S2: 维度覆盖度警告
            "coverage_warning": coverage_warning,
            # 方案9: 数据质量关卡（Phase-Level Circuit Breaker）
            "data_quality_grade": (
                "critical" if dimension_coverage_pct < 40.0 else
                "degraded" if dimension_coverage_pct < 60.0 else
                "normal"
            ),
            # S5: 冲突驱动增强
            "conflict_level": dv["conflict_level"],
            "conflict_info": dv["conflict_info"],
            # S4.5: 冲突仲裁
            "arbitration_triggered": dv["arbitration_triggered"],
            "arbitration_flipped": dv["arbitration_flipped"],
            "pre_arbitration_margin": dv["pre_arbitration_margin"],
            # Enhancement B: 置信度校准
            "confidence_calibration": confidence_calibration,
            # Enhancement C: ML 反馈权重
            "ml_weight_adjustments": dict(self.ml_adjustments),
            "ml_feedback_enabled": self.ml_feedback_enabled,
        }

    # ==================== Phase 2: 历史类比推理 ====================

    def enrich_with_historical_analogy(
        self,
        ticker: str,
        distilled: dict,
        vector_memory,
        memory_store,
    ) -> dict:
        """
        用历史类比推理丰富 QueenDistiller 输出。
        仅在 LLM 模式 + 有足够历史记忆时调用。

        Args:
            ticker: 股票代码
            distilled: distill() 的返回结果（会被就地修改）
            vector_memory: VectorMemory 实例
            memory_store: MemoryStore 实例

        Returns:
            修改后的 distilled dict（新增 historical_analogy 字段）
        """
        if not self.enable_llm:
            return distilled

        try:
            # 1. 构建当前信号查询
            direction = distilled.get("direction", "neutral")
            key_insight = distilled.get("key_insight", "")
            narrative = distilled.get("narrative", "")
            final_score = distilled.get("final_score", 5.0)

            query = f"{ticker} {direction} {key_insight}"

            # 2. 从 VectorMemory 检索语义相似历史
            vm_results = []
            if vector_memory and hasattr(vector_memory, "search") and vector_memory.enabled:
                vm_results = vector_memory.search(
                    query=query,
                    ticker=ticker,
                    top_k=8,
                    days=90,
                )

            # 最低门槛：需 >=5 条历史记忆才值得做类比
            if len(vm_results) < 5:
                distilled["historical_analogy"] = None
                return distilled

            # 3. 从 MemoryStore 获取含实际回报的历史记忆
            ms_results = []
            if memory_store and hasattr(memory_store, "get_recent_memories"):
                ms_results = memory_store.get_recent_memories(
                    ticker=ticker,
                    days=90,
                    limit=50,
                )

            # 4. 构建当前信号摘要
            current_signals = {
                "direction": direction,
                "final_score": final_score,
                "key_insight": key_insight,
                "narrative": narrative[:200] if narrative else "",
                "bear_strength": distilled.get("bear_strength", 0),
            }

            # 5. 调用 LLM 历史类比
            import llm_service
            analogy = llm_service.find_historical_analogy(
                ticker=ticker,
                current_signals=current_signals,
                historical_memories=vm_results,
                historical_outcomes=ms_results,
            )

            if analogy and analogy.get("analogy_found"):
                distilled["historical_analogy"] = analogy

                # 6. 应用 confidence_adjustment 微调 final_score（±0.5 上限）
                adj = analogy.get("confidence_adjustment", 0)
                if isinstance(adj, (int, float)):
                    adj = max(-0.1, min(0.1, adj))
                    score_adj = adj * 5  # 映射 ±0.1 → ±0.5 分
                    score_adj = max(-0.5, min(0.5, score_adj))
                    old_score = distilled["final_score"]
                    distilled["final_score"] = round(
                        max(0, min(10, old_score + score_adj)), 2
                    )
                    distilled["historical_analogy"]["score_adjustment_applied"] = round(score_adj, 2)
            else:
                distilled["historical_analogy"] = analogy  # 保留 analogy_found=false 记录

        except Exception as e:
            _log.warning("enrich_with_historical_analogy 失败 (%s): %s", ticker, e)
            distilled["historical_analogy"] = None

        return distilled
