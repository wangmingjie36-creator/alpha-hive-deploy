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

    Opportunity Score = Σ wᵢ×维度分，权重唯一真相见 config.EVALUATION_WEIGHTS
    （此处硬编码的 0.30/0.20/0.20/0.15/0.15 是 v0.45.172 之前的旧值快照，早已
    与实际配置不符——本文件不再抄写数值，见下方 DEFAULT_WEIGHTS 的定义与注释）。
    """

    # 硬编码备份 —— 仅在 config.EVALUATION_WEIGHTS 导入失败时由 __init__ 使用。
    # 正常运行时权重以 config.EVALUATION_WEIGHTS 为准（单一入口）。
    # v0.45.172：随 config.py 同步更新（此前长期未同步，ImportError 时会静默
    # 退回已被实测判定净拖累的旧权重方案——signal/risk_adj 各占权重却拖累 IC）。
    DEFAULT_WEIGHTS = {
        "signal":    0.0000,
        "catalyst":  0.3320,
        "sentiment": 0.3250,
        "odds":      0.3430,
        "risk_adj":  0.0000,
    }

    # 数据质量源分类契约（_apply_triple_penalty 评分用）
    # REAL_SOURCES (1.0)：API 调用成功 / 文件加载成功 / 真实数据
    # PROXY_SOURCES (0.7)：降级/代理数据，仍有参考价值
    # 其他 (0.0)：未分类 — 表示遗漏了分类，应视为 Bug
    # → 新增 agent data_quality 值时，务必加入对应集合
    # v0.45.314：上面这句契约此前没有任何东西在执行 —— `peer_read`（v0.45.151）与
    # `quiet`（P1-1）两个**成功**标签漏登记，被记 0 分，比 API 挂掉的 `fallback`
    # （0.7）还低，白扣网站「数据真实度」约 5.3pp。现在两道防线：
    #   · 扫描期：`_apply_triple_penalty` 遇未分类标签打 warning 并写入 `dq_unclassified`
    #   · 测试期：`tests/test_dq_label_classification.py` AST 枚举全部生产者的标签
    REAL_SOURCES = {
        "real", "yfinance", "options_api",
        "keyword", "llm_enhanced", "reddit_apewisdom",
        "newsapi",  # v0.40.0: Yahoo/AV 新闻主源（Finviz 已删除）
        "rule_only", "sec_api", "SEC直查",
        "loaded", "empty",  # ChronosBee: 日历查询成功但无事件（v0.45.32: catalysts.json 通道已移除）
        # v0.45.314：RivalBee 成功读到 ChronosBee 的真实分数（v0.45.151 引入）
        "peer_read",
        # v0.45.314：BuzzBee —— ApeWisdom 榜单正常返回、该票不在前 100 = 真实低热度
        # （与 "empty" 同理：查询成功、结果为空，是观测不是降级）
        "quiet",
    }
    PROXY_SOURCES = {
        "proxy_volume", "proxy_momentum", "proxy_social",
        "pheromone_board", "unavailable",
        "fallback", "fallback_momentum", "default", "missing",  # 降级回退仍有参考价值
        # v0.45.314：两个降级标签此前同样未分类（记 0 = 把「这一项降级」升格成
        # 「这一项全废」，同 v0.45.191/209 的判据），与 "unavailable" 同档。
        # 全史 1696 行台账里两者出现 0 次 ⇒ 对历史分数无影响。
        "unreadable",  # RivalBee：ChronosBee 条目缺失或分数不可用
        "failed",      # ChronosBee：yfinance 日历查询失败
    }

    # 不进方向**计票**的蜂（v0.45.212）。只拿掉计票里那一票：报告的 agent_breakdown、
    # 逐蜂方向、data_quality 汇总、BullVeto 读 BearBee 分数、bear_cap —— 照旧看全体。
    #
    # BearBeeContrarian：本职是反方陈述，不是陪审员。实测（623 条有 T+7 超额收益的记录）
    #   90.5% 看空、看空命中 50.5%（19 周 p=0.63）；confidence = 0.3 + 0.1×信号数
    #   + 0.1×**读到的 real 源数**（可得性当置信度），451 条 conf≥0.95 的看空命中 49.9%；
    #   且 `contrarian` 不在 ml_adjustments 表里 ⇒ 豁免了核心维度那 0.5–0.6 的票重缩放。
    #   GuardBee 的复述票（v0.45.209）起决定作用的行里 96% 正是在抵消它。
    # ⚠️ 结果证据是零效应不是改善（预注册两语料三指标 p 0.12–0.59），理由是结构性的。
    #
    # GuardBeeSentinel：方向是其余六只多数的复述（v0.45.209：普查口径后 90/90、打乱对照
    #   38.9%），在这里被当第 7 张独立票再数一遍。它此前「有用」只是在抵消 BearBee；
    #   BearBee 退出后再摘它，预注册比较两语料三指标 p 0.38–0.99 —— 零效应。
    #   它的**分数**通道（风险关门）、宏观政体、共振维度（另一条复述通道，未测未动）都不在此列。
    # 守卫：tests/test_non_voting_agents.py
    NON_VOTING_AGENTS = frozenset({"BearBeeContrarian", "GuardBeeSentinel"})

    # 本类从各蜂 `details` 读的键 —— **唯一登记处**（v0.45.247）。读一律走 `_read_bee_detail`。
    #
    # 为什么要登记：裸 `(r.get("details") or {}).get(k)` 把「键不存在」（接口断了）与
    # 「值为 None」（数据暂时拿不到）渲染成同一个 None。2026-03-30 起这里读 BuzzBee 的
    # `fear_greed_value`、Oracle 的 `gex`、Guard 的 `market_regime.dealer_gex`，三个键
    # 生产者**从未产出过**（前者与 Oracle 那个只进了发信息素板的 `_pub_details`），
    # F&G 政体调整与 F&G>75 看多门槛因此半年里一次都没执行，没有任何东西变红。
    # 守卫：tests/test_bee_details_contract.py（登记表 ↔ 调用点 ↔ 生产者 AST 键树三方核对）。
    DETAIL_READS = frozenset({
        ("GuardBeeSentinel", "macro_regime"),
        ("OracleBeeEcho", "iv_rank"),
        ("ScoutBeeNova", "price"),
        ("BuzzBeeWhisper", "sentiment_momentum"),
        ("BuzzBeeWhisper", "sentiment_divergence"),
    })

    def _read_bee_detail(self, results: List[Dict], source: str, key: str,
                         misses: List[str], default: Any = None) -> Any:
        """读 `source` 这只蜂的 `details[key]`，语义同 `details.get(key, default)`，
        但把「键不存在」记进 `misses`（进 `swarm_results.details_contract_misses`）。

        不算契约断裂、只返回 `default` 的三种情形：这只蜂不在结果里；它报了 `error`
        （details 为空是已声明的失败）；键在但值为 None（照常返回 None）。

        ⚠️ 只记账不抛：调用点多在 `try/except Exception` 里，抛了会被吞成 debug 日志。
        记账后由 `distill` 在 try 之外统一打 error。
        """
        if (source, key) not in self.DETAIL_READS:
            misses.append(f"undeclared:{source}.{key}")
        for r in results:
            if r.get("source") != source:
                continue
            if r.get("error"):
                return default
            det = r.get("details")
            if not isinstance(det, dict) or key not in det:
                misses.append(f"{source}.{key}")
                return default
            return det[key]
        return default

    def __init__(self, board: PheromoneBoard, weight_manager=None, adapted_weights: Dict = None,
                 enable_llm: bool = True, ml_model=None):
        self.board = board
        self.weight_manager = weight_manager
        self.enable_llm = enable_llm
        self.ml_model = ml_model
        if adapted_weights:
            # ⚠️ v0.45.176 起**生产不再走这条分支**：`alpha_hive_daily_report` 不再传
            # `adapted_weights=`（理由见 `Backtester.adapt_weights` 的 docstring —— 它学的是
            # 「谁更爱说中性」而不是准头）。参数保留仅供测试注入自定义权重。
            # **不要在生产代码里传它。** 传了就整体顶掉 config，且下方 config 热加载
            # （Bug #18 的修复）会被恒真短路掉——那正是它六个月来的真实状态。
            self.DIMENSION_WEIGHTS = adapted_weights
        else:
            # 修复 Bug #18：config 热加载 — 旧实现权重在 __init__ 时快照，
            # weekly_optimizer 写入 config.py 后需重启才生效。
            # 新实现：每次实例化都 importlib.reload，让长驻进程也能拿到最新权重
            try:
                import importlib
                import config as _cfg
                importlib.reload(_cfg)
                EVALUATION_WEIGHTS = _cfg.EVALUATION_WEIGHTS
                valid_dims = set(self.DEFAULT_WEIGHTS.keys())
                cfg_weights = {k: v for k, v in EVALUATION_WEIGHTS.items() if k in valid_dims}
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

    def _assert_regime_preserved_zeros(self, ticker: str, regime_weights: Dict) -> bool:
        """政体层保零守卫（v0.45.176）：喂进评分的权重里，零维必须仍为零。

        **这是「谁会红？」在运行期的落点。** 姊妹守卫
        `alpha_hive_daily_report._assert_config_zeros_survive` 检的是本对象的
        `DIMENSION_WEIGHTS`（蜂后基准权重，即 09-09 adapted_weights 事故那一层），
        但**真正乘进 `_compute_weighted_score` 的是这里的 `regime_weights`**。
        实测：把 `gex_regime` 的 `max(0.02, ·)` 地板 bug 放回去，姊妹守卫
        **仍返回 True**，而逐标的权重已经是 signal=0.0192 —— 探针放在它要防的
        那个 bug 的上游，等于没放。两层缺一不可。

        不抛异常：无人值守的定时扫描里，为一条权重不变式炸掉整轮扫描
        得不偿失；打 error 日志（编排器与日志检查会看到）+ 每个实例只报一次，
        避免 30 只标的刷 30 条同样的错。
        """
        zeroed = [d for d, v in self.DIMENSION_WEIGHTS.items() if v == 0]
        if not zeroed:
            return True
        bad = {d: regime_weights.get(d) for d in zeroed
               if isinstance(regime_weights.get(d), (int, float)) and regime_weights[d] != 0}
        if not bad:
            return True
        if not getattr(self, "_regime_zero_violation_logged", False):
            self._regime_zero_violation_logged = True
            _log.error(
                "[%s] 政体层保零违反：基准权重已归零 %s，政体调整后却是 %s"
                " —— 评分实际用的是后者。历史成因是 `gex_regime.RegimeWeightAdjuster`"
                " 的 `max(0.02, ·)` 地板把显式零复活成 2%%（v0.45.176 已修）。",
                ticker, zeroed, {k: round(v, 4) for k, v in bad.items()},
            )
        return False

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
        _baseline = 1.0 / len(self.DIMENSION_WEIGHTS)  # 当前激活维度数的均等基准（通常 = 0.20）
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

    _OOS_TRUST_CACHE: tuple = ()  # (mtime, factor) 类级缓存，避免每票读盘

    @classmethod
    def _ml_oos_trust_factor(cls) -> float:
        """v0.40.0: 从 ml_model_cache.json 读 OOS 精度 → ML 调整信任系数。

        OOS >= half_threshold: 1.0 | >= zero_threshold: 0.5 | 低于: 0.0
        OOS 缺失（旧模型/样本<60）: 1.0（不惩罚，待下次训练产生）。
        """
        try:
            from config import ML_FEEDBACK_CONFIG as _MFC
        except (ImportError, AttributeError):
            _MFC = {}
        half_th = float(_MFC.get("oos_trust_half_threshold", 55.0))
        zero_th = float(_MFC.get("oos_trust_zero_threshold", 50.0))
        try:
            import os as _os
            import json as _json
            from hive_logger import PATHS as _PATHS
            # v0.45.149：兜底曾是相对路径 `"ml_model_cache.json"`，会读到
            # 当前工作目录里的野文件。绝对路径取不到就应当放弃，不该改读 cwd。
            _path = str(_PATHS.ml_model_cache)
            _mtime = _os.path.getmtime(_path)
            if cls._OOS_TRUST_CACHE and cls._OOS_TRUST_CACHE[0] == _mtime:
                return cls._OOS_TRUST_CACHE[1]
            with open(_path) as _f:
                _oos = _json.load(_f).get("oos_accuracy")
            if _oos is None:
                factor = 1.0
            elif _oos >= half_th:
                factor = 1.0
            elif _oos >= zero_th:
                factor = 0.5
            else:
                factor = 0.0
            cls._OOS_TRUST_CACHE = (_mtime, factor)
            return factor
        except Exception:
            return 1.0  # 读不到时不惩罚（fail-open）

    def _compute_weighted_score(self, ticker: str, dim_scores: Dict,
                                dim_confidence: Dict,
                                dimension_coverage_pct: float,
                                present_count: int,
                                valid_results: List[Dict],
                                override_weights: Dict = None) -> Dict:
        """ML 调整 + 5D 加权 + 覆盖度压缩 + 共振增强。返回 dict。"""
        ml_adjustment = 0.0
        for r in valid_results:
            if r.get("dimension") == "ml_auxiliary":
                ml_score = _safe_score(r.get("score", 5.0), default=5.0, lo=0.0, hi=10.0, label="ml_score")
                ml_conf = _safe_score(r.get("confidence", 0.5), default=0.5, lo=0.0, hi=1.0, label="ml_conf")
                ml_adjustment = (ml_score - 5.0) * 0.1 * ml_conf
                # v0.40.0: 按 OOS（时序外样本）精度缩放 ML 信任度——
                # in-sample 精度是自考自评；OOS <55% 减半、<50%（不如抛硬币）置零。
                # 阈值见 config.ML_FEEDBACK_CONFIG（oos_trust_*）。
                ml_adjustment *= self._ml_oos_trust_factor()

        _weights = override_weights if override_weights else self.DIMENSION_WEIGHTS
        _n_dims = len(_weights)

        # 升级 1: 缺失维度动态填充（消除系统性偏差）
        # 旧：固定 4.7 → 无论市场状态都偏空。新：用已有维度均值与 5.0 的中值
        if dim_scores:
            _available_avg = sum(dim_scores.values()) / len(dim_scores)
            _missing_dim_fill = round((_available_avg + 5.0) / 2, 2)
        else:
            _missing_dim_fill = 5.0

        # 升级 2: 置信度权重配置
        try:
            from config import CONFIDENCE_WEIGHTING as _CONF_CFG
        except (ImportError, AttributeError):
            _CONF_CFG = {"exponent": 1.5, "floor": 0.3}
        _conf_exp = _CONF_CFG.get("exponent", 1.5)
        _conf_floor = _CONF_CFG.get("floor", 0.3)

        # 修复 Bug #19：缺失维度重新归一化（而非注入中性假值）
        # 旧实现：missing dim 加 _missing_dim_fill × weight 到和里 → 即使其他 4 维一致 8.0，
        # 缺 signal 维度会被拉回 (8.0×0.7 + 5.0×0.3)≈6.8，错失高分标的
        # 新实现：只用"已覆盖维度"加权平均，权重重新归一化到 1.0（对应 coverage<80% 的 warning 已独立告知）
        weighted_sum = 0.0
        weight_total = 0.0
        missing_penalty_weight = 0.0  # 追踪缺失权重占比
        for dim, weight in _weights.items():
            if dim in dim_scores:
                conf = dim_confidence.get(dim, 0.5)
                effective_weight = weight * max(_conf_floor, conf ** _conf_exp)
                weighted_sum += dim_scores[dim] * effective_weight
                weight_total += effective_weight
            else:
                missing_penalty_weight += weight  # 仅用于外层 coverage 惩罚，不再注入中性假值

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

        # ⚠️ 共振加成正在做预注册前瞻检验（v0.45.242，`experiments/resonance_boost_forward_test.py`）：
        # 样本内它是评分链里唯一测得出损失排序信息的一步，但证据是事后分析，用户定的是
        # 「前瞻确认后再删」。**结论出来之前勿删改、勿改成分方向**——那会让检验的自证失败
        # （B0 重放复现不了生产），样本内理由与数字见 experiments/resonance_boost_insample_report.md。
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
        dq_unclassified: List[str] = []
        for r in valid_results:
            _dq_e = r.get("data_quality", {})
            if isinstance(_dq_e, dict):
                for _ch_e, v in _dq_e.items():
                    _tf_early += 1
                    if v in self.REAL_SOURCES:
                        _qs_early += 1.0
                    elif v in self.PROXY_SOURCES:
                        _qs_early += 0.7
                    else:
                        # v0.45.314：未分类 = 契约写明的 Bug，此前静默记 0 分
                        dq_unclassified.append(f"{r.get('source', '?')}.{_ch_e}={v!r}")
        if dq_unclassified:
            _log.warning("%s data_quality 未分类标签（按 0 分计入 data_real_pct，"
                         "应登记进 REAL_SOURCES/PROXY_SOURCES）: %s",
                         ticker, ", ".join(dq_unclassified))
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
            "dq_unclassified": dq_unclassified,
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
        # 计票口径（v0.45.212）：本函数里凡是门槛、票重、仲裁、冲突再投票，一律只看 _voters。
        _voters = [r for r in valid_results if r.get("source") not in self.NON_VOTING_AGENTS]
        _vote_excluded = sorted({r.get("source") for r in valid_results} & self.NON_VOTING_AGENTS)
        directions = [r.get("direction", "neutral") for r in _voters]
        bullish_count = directions.count("bullish")
        bearish_count = directions.count("bearish")
        neutral_count = directions.count("neutral")

        _all_conf = [r.get("confidence", 0.5) for r in _voters]
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

        # 没有 ML 乘数的投票蜂取「当期平均乘数」，不取 1.0（v0.45.228）。
        # 乘数被下限 0.5 主导（793 份 JSON 中位 0.500），「1.0 = 平均」这个隐含假设不成立 ——
        # RivalBee（ml_auxiliary 不在表里）与 CodeExec（没有维度）曾因此拿到 1.5–2.0 倍相对票重。
        # 取均值 ⇒ 只保留乘数之间的相对信息；全部压在下限时 ≡ 不缩放。
        # 维度权重那个消费者乘完会归一化，水平本来就不起作用，所以只改这里。
        # 守卫：tests/test_ml_vote_scaling_default.py
        _ml_neutral = (sum(self.ml_adjustments.values()) / len(self.ml_adjustments)
                       if self.ml_adjustments else 1.0)

        def _effective_conf(r):
            conf = min(r.get("confidence", 0.5), _weight_cap)
            if _ml_vote_boost_enabled:
                from pheromone_board import PheromoneBoard as _PB
                _agent_dim = _PB.AGENT_DIMENSIONS.get(r.get("source", ""))
                conf *= self.ml_adjustments.get(_agent_dim, _ml_neutral)
            return conf

        bullish_w = sum(_effective_conf(r) for r in _voters if r.get("direction") == "bullish")
        bearish_w = sum(_effective_conf(r) for r in _voters if r.get("direction") == "bearish")
        neutral_w = sum(_effective_conf(r) for r in _voters if r.get("direction") == "neutral")
        total_w = bullish_w + bearish_w + neutral_w or 1.0

        try:
            from config import BEAR_SCORING_CONFIG as _BSC4
        except ImportError:
            _BSC4 = {}
        _bear_min_agents = _BSC4.get("voting_bearish_min_agents", 1)
        _bear_min_wpct = _BSC4.get("voting_bearish_min_weight_pct", 0.25)

        # 升级 4: 看多不对称门槛（看多需要更强共识）
        try:
            from config import BULLISH_GATE_CONFIG as _BGC
        except (ImportError, AttributeError):
            _BGC = {"min_weight_pct": 0.50, "min_agents": 3}
        _bull_min_wpct = _BGC.get("min_weight_pct", 0.50)
        _bull_min_agents = _BGC.get("min_agents", 3)
        # v0.45.247 删：「F&G>75 时看多门槛抬到 60%」。fg_value 恒为 None，从未触发
        # （且 2026-02-20 起 CNN F&G 最高 71，即便接上历史上也是 0 次）。

        # 升级 6: 方向惯性平滑（窄边际时倾向维持昨日方向）
        try:
            from config import DIRECTION_STABILITY as _DS_CFG
        except (ImportError, AttributeError):
            _DS_CFG = {"enabled": True, "inertia_bonus": 0.1, "narrow_margin": 0.15}
        if _DS_CFG.get("enabled", True):
            _vote_margin = abs(bullish_w - bearish_w) / total_w if total_w > 0 else 1.0
            if _vote_margin < _DS_CFG.get("narrow_margin", 0.15):
                _prev_entries = [e for e in self.board._entries
                                 if e.ticker == ticker and e.agent_id == "QueenDistiller"]
                if _prev_entries:
                    _prev_dir = _prev_entries[-1].direction
                    _inertia = _DS_CFG.get("inertia_bonus", 0.1)
                    if _prev_dir == "bullish":
                        bullish_w += _inertia
                    elif _prev_dir == "bearish":
                        bearish_w += _inertia
                    total_w = bullish_w + bearish_w + neutral_w or 1.0

        if bullish_w > bearish_w and bullish_w / total_w >= _bull_min_wpct and bullish_count >= _bull_min_agents:
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
        _dissent_agents = set(_CAC.get("dissent_agents", []))

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
            for r in _voters:
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
            for r in _voters:
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

            _conflict_ratio = (bullish_count + bearish_count) / max(1, len(_voters))
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

        # v0.37.0: Bear 强信号压制看多 —— BearBee score >= 阈值时 bullish 降级 neutral（不翻转）
        # 回测（536 样本）：被拦截的 13 单看多均收益 -1.70% vs 全体看多 +3.09%
        if (rule_direction == "bullish"
                and _BSC4.get("bull_veto_enabled", False)):
            _veto_th = _BSC4.get("bull_veto_bear_score", 7.0)
            _bear_r = next((r for r in valid_results
                            if r.get("source") == "BearBeeContrarian"), None)
            if _bear_r is not None and (_bear_r.get("score") or 0) >= _veto_th:
                rule_direction = "neutral"
                conflict_info["bull_veto"] = {
                    "bear_score": round(_bear_r.get("score", 0), 1),
                    "threshold": _veto_th,
                }
                _log.info(
                    "%s [BullVeto] BearBee %.1f >= %.1f，bullish 降级 neutral",
                    ticker, _bear_r.get("score", 0), _veto_th,
                )

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

        # agent_breakdown 的展示口径保持「全体」（报告里渲染成「Agent 投票：看多N vs 看空M」，
        # 改口径会让历史对比失真）；真正参与计票的在 voting_counts 与 vote_excluded_agents。
        _all_dirs = [r.get("direction", "neutral") for r in valid_results]
        return {
            "rule_direction": rule_direction,
            "rule_score": rule_score,
            "bullish_count": _all_dirs.count("bullish"),
            "bearish_count": _all_dirs.count("bearish"),
            "neutral_count": _all_dirs.count("neutral"),
            "voting_counts": {"bullish": bullish_count, "bearish": bearish_count,
                              "neutral": neutral_count},
            "vote_excluded_agents": _vote_excluded,
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
                        conflict_level: str, conflict_info: Dict,
                        detail_misses: Optional[List[str]] = None) -> Dict:
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
                    _misses = detail_misses if detail_misses is not None else []
                    _sent_ctx = None
                    _sm = self._read_bee_detail(
                        valid_results, "BuzzBeeWhisper", "sentiment_momentum", _misses)
                    _sd = self._read_bee_detail(
                        valid_results, "BuzzBeeWhisper", "sentiment_divergence", _misses)
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
                # v0.45.182：把失败标记透出来。本白名单此前只抄 6 个键，
                # `error` 被丢在外面 ⇒ 下游看到的只有 `make_error_result` 的
                # `score=5.0`，与「这只蜂真的打了 5.0」**逐字节同形**。
                # 生产里合法打 5.0 的有 38 例、崩掉的有 13 例，靠取值分不开。
                # 旧快照没有这个键，`.get("error")` 取到 None ⇒ 按「没失败」读，
                # 与历史语义一致。
                "error": r.get("error"),
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

    def distill(self, ticker: str, agent_results: List[Dict],
                dealer_gex: Dict = None) -> Dict:
        """
        5 维加权评分 + 共振增强 + 多数投票 + LLM 推理蒸馏

        双引擎：规则引擎始终运行作为基础，LLM 引擎在可用时叠加推理。

        升级 #1: GEX 政体联动评分 —— v0.45.334 起**只算不加**（诊断值，见步骤 4.5）
        升级 #4: 政体条件权重（根据宏观/GEX/IV 动态调整 5 维权重）—— GEX 仍进评分的两条通道之一
                 （另一条是 OracleBee `options_score` 里的 `gex_signal`，v0.45.334 未动；见步骤 4.5 注释）
        """
        # 降级护栏：蜂 future 超时 / 抛异常时 alpha_hive_daily_report 会向 agent_results
        # append(None)，而下方 GEX/F&G 预处理循环（line ~874/883/897/924）直接 _r.get(...)，
        # 遇 None 抛 AttributeError（F&G 循环无 try 保护 → 整个 ticker 蒸馏崩溃）。
        # 入口统一滤 None，与 _prepare_dimension_data 的 clean_results_batch 同口径，
        # 不影响维度覆盖度 / 评分（None 本就不是任何蜂结果）。
        agent_results = [_r for _r in agent_results if _r is not None]

        # 各蜂 details 的读取契约账本（见 DETAIL_READS）。本次蒸馏内的所有读取往这里记。
        _detail_misses: List[str] = []

        # ===== 0. GEX 政体 + 政体权重预计算 =====
        _gex_data = {}
        _gex_mod_result = {"gex_adjustment": 0.0, "gex_regime": "unknown",
                           "flip_proximity_pct": None, "can_flip_vanna": False,
                           "regime_description": "未计算", "confidence_modifier": 1.0}
        _regime_weights_desc = ""
        _regime_weights_used = dict(self.DIMENSION_WEIGHTS)
        _macro_regime = "neutral"
        _iv_rank_val = None

        try:
            from gex_regime import GexRegimeModifier, RegimeWeightAdjuster

            # 优先使用传入的 dealer_gex 参数
            _gex_data = dealer_gex or {}

            _macro_regime = self._read_bee_detail(
                agent_results, "GuardBeeSentinel", "macro_regime", _detail_misses, default="neutral")
            _iv_rank_val = self._read_bee_detail(
                agent_results, "OracleBeeEcho", "iv_rank", _detail_misses)

            # v0.45.247 删：这里原先还依次试 Guard `market_regime.dealer_gex` 与 Oracle `gex`。
            # 两者生产者都**从不产出**（794/794 份非 error 结果为空；Oracle 的 gex 只在
            # `_pub_details`，且是 gamma_exposure 标量而非本函数要的 dict）⇒ 生产 GEX 一直
            # 落在下面的按需计算。删掉不改任何行为，只是不再假装有前两条来源。
            if not _gex_data:
                try:
                    from advanced_analyzer import DealerGEXAnalyzer
                    _scout_price = self._read_bee_detail(
                        agent_results, "ScoutBeeNova", "price", _detail_misses)
                    if _scout_price and float(_scout_price) > 0:
                        _gex_analyzer = DealerGEXAnalyzer()
                        _gex_data = _gex_analyzer.analyze(ticker, float(_scout_price))
                except Exception as _e_gex_lazy:
                    _log.debug("按需 GEX 计算失败 (%s): %s", ticker, _e_gex_lazy)

            # 升级 #4: 根据政体调整权重
            _rwa = RegimeWeightAdjuster()
            _gex_regime_str = _gex_data.get("regime", "unknown") if _gex_data else "unknown"
            _regime_weights_used, _regime_weights_desc = _rwa.adjust_weights(
                base_weights=dict(self.DIMENSION_WEIGHTS),
                macro_regime=_macro_regime,
                gex_regime=_gex_regime_str,
                iv_rank=_iv_rank_val,
            )
            if _regime_weights_desc != "权重未调整（中性环境）":
                _log.info("[%s] 政体权重调整: %s | %s", ticker, _regime_weights_desc,
                          {k: f"{v:.3f}" for k, v in _regime_weights_used.items()})
        except Exception as _e_regime:
            _log.debug("政体权重/GEX 预计算失败 (%s): %s", ticker, _e_regime)

        # v0.45.176：政体层保零守卫。**刻意放在 try/except 之外** ——
        # 上面那个 except 吞一切到 debug，检查写在里面会被静默吃掉
        # （见 MEMORY `alpha-hive-failure-propagation`：容错把失败改写成「没发生过」）。
        # 放在外面还能同时覆盖降级路径（异常时 _regime_weights_used 退回
        # dict(self.DIMENSION_WEIGHTS)，那条路也必须保零）。
        self._assert_regime_preserved_zeros(ticker, _regime_weights_used)

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

        # ===== 2. 加权评分 + 共振（使用政体调整后的权重）=====
        ws = self._compute_weighted_score(
            ticker, dim_scores, dim_confidence,
            dimension_coverage_pct, present_count, valid_results,
            override_weights=_regime_weights_used)
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

        # ===== 4.5 GEX 政体诊断（需要 direction）—— v0.45.334 起只算、不加进 rule_score =====
        # 此前这里把 `GexRegimeModifier` 的 ±0.8 直接加到 rule_score 上，是 GEX 进评分的三条通道之一。
        # 另两条：① 步骤 0 的 RegimeWeightAdjuster 三值 regime 偏移权重；② OracleBee `options_score`
        # 里的 `gex_signal`（`options_analyzer.py`：主链 `gamma_exposure < -0.001` 得 2.0、否则 1.0，
        # None 也按 1.0 ⇒ 经 odds 维进加权分）。v0.45.197 的世代边界只记了 ①。
        # ⚠️ **本版只断开这一条，② 未动**（改它是评分口径变更，须用户决定）。评审只读重放
        # 09-11~09-22：去掉 ② 那 +1，210 行里 47 行 final_score 变，中位 0.31、最大 0.38
        # （47/210 已只读复核；Δ 幅度为评审实测、未复算）。② 同样挂着数据可得性：09-24/25 两天
        # Oracle `gamma_exposure` 60/60 为 None ⇒ 负 gamma 标的少了那 +1。断开这一条的理由：
        #   · 它的非零值 67% 来自「正 GEX 且距 flip<2%」分支，而那个 flip 是逐行权价看净 GEX
        #     变号，结构上贴着现价 ⇒ 「近 flip」大多是定义的产物，不是环境信号；
        #   · 与数据可得性挂钩：GEX 取不到的日子（09-17 全天 30/30 unknown）没有这笔调整，
        #     全池分数系统性偏高 ~0.11 ⇒ 分数里混进了「那天 CBOE 好不好用」；
        #   · 唯一一次量测（`experiments/resonance_boost_insample_report.md` §3.1 逐级 IC）里
        #     GEX 这一步 +0.003 / +0.005，不显著；没有任何证据说它改善排序。
        # **继续计算并落盘**（审计轨迹 + 共振前瞻检验 `_REQUIRED` 需要 `gex_regime_mod` 这个键）；
        # `gex_adjustment` 保留原名、原值，语义变成「算了但没施加」，由 `applied=False` 标明。
        # 守卫：`tests/test_gex_modifier_disconnected.py`（行为枚举 + AST：生产代码里
        # gex_adj* 不得进 +/- 运算）。⚠️ 不要接回来——要接先过前瞻检验，并登记世代边界。
        try:
            from gex_regime import GexRegimeModifier
            _gex_mod_result = GexRegimeModifier().compute(_gex_data, direction=rule_direction)
            _gex_adj = _gex_mod_result["gex_adjustment"]
            if abs(_gex_adj) > 0.01:
                _log.info("[%s] GEX政体诊断值 %+.2f（诊断值，未施加；rule_score 仍为 %.2f）| %s",
                          ticker, _gex_adj, rule_score, _gex_mod_result["regime_description"])
        except Exception as _e_gex:
            _log.debug("GEX 政体诊断计算失败 (%s): %s", ticker, _e_gex)
        # 刻意放在 try 之外：compute 抛异常时 `_gex_mod_result` 是步骤 0 的默认值，
        # 那条路落盘的也必须带上标记 —— 下游（共振检验 replay、深度报告徽章、
        # `ic_rerun_readiness.cohort_boundary_evidence`）靠「键缺失 ⇒ 旧记录、当时施加过」
        # 区分新旧两代，新记录缺键会被误认成旧记录。
        _gex_mod_result["applied"] = False

        # v0.45.247 删：步骤 4.6「Fear & Greed 政体调整」（极度恐惧+看空 +0.3 / +看多 −0.4 等）。
        # 它读的 BuzzBee `fear_greed_value` 从未出现在 AgentResult.details 里 ⇒ 自 2026-03-30
        # 引入起**一次都没执行过**，删除不改任何生产分数。未接线的理由（按看多度轴读，看空 +0.3
        # 是把空单推离空头闸；配对重放 + 负对照显示 IC「改善」是压缩负 IC 分差的机械效应）
        # 见 MEMORY `alpha-hive-fear-greed-dead-wire`。**要重建先读那篇**；它是聚合层函数，
        # 可用 signal_archive 的 `market.fear_greed` 离线重放，不必上线实验。

        # ===== 5. LLM 引擎 =====
        llm = self._run_llm_engine(
            ticker, valid_results, all_results, dim_scores,
            resonance, rule_score, rule_direction,
            contrarian_result, dv["conflict_level"], dv["conflict_info"],
            detail_misses=_detail_misses)

        final_score = llm["final_score"]
        final_direction = llm["final_direction"]

        # ===== 5.5 历史胜率反馈折扣 =====
        _ticker_acc_discount = 0.0
        try:
            from config import TICKER_ACCURACY_FEEDBACK as _TAF
        except (ImportError, AttributeError):
            # v0.45.23: fallback 必须与 config 的深思熟虑值同向（v0.45.12 已定 enabled=False）。
            # 若这里是 True，config 导入失败会静默重开被走查检验否决的胜率反馈，
            # 且与真实开启同形，无从分辨。
            _TAF = {"enabled": False, "min_samples": 5, "discount_threshold": 0.50, "min_reliability": 0.5}
        if _TAF.get("enabled", False):
            try:
                from feedback_loop import BacktestAnalyzer as _BA_ta
                from hive_logger import PATHS as _PATHS_ta
                # v0.45.98 原版：这里独立算一份 `_project_root_ta =
                # Path(__file__).resolve().parent.parent`，同时喂给 _snap_dir
                # 与 close_t7_db_path——理由是怕两者落到 feedback_loop.py 自己
                # 的 `__file__` 相对缺省值会不一致（那时 feedback_loop 的默认
                # 值确实是 `Path(__file__).parent`）。
                # v0.45.260（数据根迁移阶段 2）：那份顾虑已不成立——
                # feedback_loop._db_path() 早在 v0.45.160/171 就改成读
                # `PATHS.db`，不再是 `__file__` 派生。继续在这里独立冻结
                # `_project_root_ta` 只是把「两份独立冻结的默认值必须巧合
                # 一致」这个脆弱耦合原样保留（且换成了 `PATHS.home`/`PATHS.db`
                # 也是各自独立解析）。现在**不传** `close_t7_db_path`，让
                # `BacktestAnalyzer` 走它自己对 `feedback_loop._db_path()` 的
                # 默认解析；`_snap_dir` 也改用 `PATHS.home`——两处现在结构性地
                # 读同一个真相源（`PATHS.home`/`PATHS.db` 共享同一个
                # `ALPHA_HIVE_HOME`），不再需要"必须巧合一致"。
                # 生产今天不设 `ALPHA_HIVE_HOME` 时，`PATHS.home` 与旧的
                # `Path(__file__).resolve().parent.parent`（本文件在
                # `swarm_agents/` 下，上跳两级）落在同一个仓库根，行为不变。
                _snap_dir = str(_PATHS_ta.home / "report_snapshots")
                # 缓存 BacktestAnalyzer 实例（避免每标的都重新扫描文件系统）
                # v0.45.87：接入 close_t7 干净口径（此前用只有约1/3 可信的
                # actual_prices.t7），与 weekly_optimizer.py 共用同一份实现。
                if not hasattr(self, "_ba_cache"):
                    self._ba_cache = _BA_ta(directory=_snap_dir, clean_t7=True)
                _snaps = self._ba_cache.get_snapshots_by_ticker(ticker)
                _t7 = [s for s in (_snaps or []) if s.actual_price_t7 is not None and s.entry_price]
                if len(_t7) >= _TAF.get("min_samples", 5):
                    _wins = sum(1 for s in _t7
                                if (s.direction == "Long" and (s.actual_price_t7 - s.entry_price) > 0) or
                                   (s.direction == "Short" and (s.actual_price_t7 - s.entry_price) < 0))
                    _win_rate = _wins / len(_t7)
                    _threshold = _TAF.get("discount_threshold", 0.50)
                    if _win_rate < _threshold:
                        _reliability = max(_TAF.get("min_reliability", 0.5), _win_rate / _threshold)
                        _pre_ta = final_score
                        # 压缩偏离中性的幅度，但不改变方向
                        # 即 score > 5 时向下压缩，score < 5 时向上压缩
                        final_score = round(5.0 + (final_score - 5.0) * _reliability, 2)
                        _ticker_acc_discount = round(abs(_pre_ta - final_score), 2)
                        _log.info("[%s] 历史胜率折扣: winrate=%.1f%% (%d/%d), reliability=%.2f, "
                                  "score %.2f→%.2f",
                                  ticker, _win_rate * 100, _wins, len(_t7),
                                  _reliability, _pre_ta, final_score)
            except Exception as _e_ta:
                _log.debug("历史胜率折扣失败 (%s): %s", ticker, _e_ta)

        # ===== 6. 置信度校准（含 GEX confidence_modifier）=====
        confidence_calibration = self._compute_confidence_calibration(
            final_score, dim_scores, dv, present_count)
        # GEX 政体修正置信带宽
        # v0.45.334 刻意**未动**这一支（它不是评分输入，只改 band_width）。已知既有不一致：
        # `confidence_band` / `discrimination` 在上面那行就算好了、这里只乘 band_width ⇒
        # 两者对不上（09-11 起 119 行里 113 行半宽 ≠ band_width）。另开任务处理，别顺手改。
        _gex_conf_mod = _gex_mod_result.get("confidence_modifier", 1.0)
        if _gex_conf_mod > 1.0 and "band_width" in confidence_calibration:
            confidence_calibration["band_width"] = round(
                confidence_calibration["band_width"] * _gex_conf_mod, 2)
            confidence_calibration["gex_confidence_modifier"] = _gex_conf_mod

        _result = {
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
            "dimension_weights": dict(_regime_weights_used),
            "dimension_weights_base": dict(self.DIMENSION_WEIGHTS),
            "ml_adjustment": round(ml_adjustment, 3),
            "ml_contribution_pct": round(abs(ml_adjustment) / max(abs(final_score), 0.01) * 100, 1),
            "base_score_before_resonance": round(adjusted_score, 2),
            "pheromone_compact": self.board.compact_snapshot(ticker),
            "data_quality": dv["data_quality_summary"],
            "data_real_pct": tp["data_real_pct"],
            "dq_unclassified": tp["dq_unclassified"],
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
            "vote_excluded_agents": dv["vote_excluded_agents"],
            # Enhancement B: 置信度校准
            "confidence_calibration": confidence_calibration,
            # Enhancement C: ML 反馈权重
            "ml_weight_adjustments": dict(self.ml_adjustments),
            "ml_feedback_enabled": self.ml_feedback_enabled,
            # 升级 #1: GEX 政体诊断（v0.45.334 起 applied=False：gex_adjustment 算了但没施加）
            "gex_regime_mod": _gex_mod_result,
            # 升级 #4: 政体条件权重
            "regime_weights_description": _regime_weights_desc,
            "macro_regime": _macro_regime,
            # 升级 5: 历史胜率折扣
            "ticker_accuracy_discount": _ticker_acc_discount,
            # v0.45.247：原有 `fear_greed_value`（恒为 None，已删）。F&G 原值现在在
            # agent_details.BuzzBeeWhisper.details.fear_greed（带 source）。
            # 读各蜂 details 时「键不存在」的记账；正常为空列表，非空即接口断了。
            "details_contract_misses": sorted(set(_detail_misses)),
        }

        # 刻意放在所有 try/except 之外（同 _assert_regime_preserved_zeros 的理由）。
        if _detail_misses:
            _log.error("[%s] 蜂 details 读取契约断裂（生产者没产出这些键，读到的是默认值）：%s",
                       ticker, sorted(set(_detail_misses)))

        # 升级 6: 发布 Queen 方向到信息素板（供下次运行的惯性计算）
        try:
            from pheromone_board import PheromoneEntry
            self.board.publish(PheromoneEntry(
                agent_id="QueenDistiller",
                ticker=ticker,
                discovery=f"Queen决策: {final_direction} {final_score:.1f}/10",
                source="queen_distiller",
                self_score=final_score,
                direction=final_direction,
            ))
        except Exception as _e_pub:
            _log.debug("Queen 方向发布失败 (%s): %s", ticker, _e_pub)

        return _result

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
