"""
Alpha Hive - 数据模型层

使用 dataclass 定义所有核心数据结构，提供验证和清洗工具。
不依赖 Pydantic（保持零外部依赖），使用 dataclass + __post_init__ 验证。
"""

import logging as _logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any
import math

_log = _logging.getLogger("alpha_hive.models")


# ==================== 数据清洗工具 ====================

def clamp(value: float, lo: float, hi: float) -> float:
    """将值限制在 [lo, hi] 范围内"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return (lo + hi) / 2.0
    return max(lo, min(hi, float(value)))


def clean_score(value) -> float:
    """清洗评分值：处理 None, NaN, 越界"""
    if value is None:
        return 5.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 5.0
    if math.isnan(v) or math.isinf(v):
        return 5.0
    return clamp(v, 0.0, 10.0)


def clean_confidence(value) -> float:
    """清洗 confidence 值"""
    if value is None:
        return 0.5
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.5
    if math.isnan(v) or math.isinf(v):
        return 0.5
    return clamp(v, 0.0, 1.0)


def clean_direction(value) -> str:
    """清洗方向字段"""
    valid = {"bullish", "bearish", "neutral"}
    if isinstance(value, str) and value.lower() in valid:
        return value.lower()
    return "neutral"


def clean_string(value, default: str = "", max_len: int = 500) -> str:
    """清洗字符串：None -> default, 截断超长"""
    if value is None:
        return default
    s = str(value).strip()
    return s[:max_len] if len(s) > max_len else s


# ==================== Agent 结果模型 ====================

@dataclass
class AgentResult:
    """单个 Agent 的分析结果（标准化输出格式）"""

    score: float
    direction: str
    confidence: float
    discovery: str
    source: str
    dimension: str
    data_quality: Dict[str, str] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def __post_init__(self):
        self.score = clean_score(self.score)
        self.direction = clean_direction(self.direction)
        self.confidence = clean_confidence(self.confidence)
        self.discovery = clean_string(self.discovery)
        self.source = clean_string(self.source, default="Unknown")
        self.dimension = clean_string(self.dimension, default="unknown")

    @classmethod
    def from_dict(cls, d: Dict) -> Optional["AgentResult"]:
        """从 Agent 返回的 dict 构造 AgentResult，无效数据返回 None"""
        if not d or not isinstance(d, dict):
            return None
        if "error" in d and "score" not in d:
            return None
        try:
            return cls(
                score=d.get("score"),
                direction=d.get("direction"),
                confidence=d.get("confidence", 0.5),
                discovery=d.get("discovery", ""),
                source=d.get("source", "Unknown"),
                dimension=d.get("dimension", "unknown"),
                data_quality=d.get("data_quality", {}),
                details=d.get("details", {}),
                error=d.get("error"),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            _log.debug("AgentResult.from_dict 失败: %s", exc)
            return None

    def to_dict(self) -> Dict:
        return asdict(self)

    @property
    def is_valid(self) -> bool:
        """是否为有效结果（无 error 且有必要字段）"""
        return self.error is None and self.dimension != "unknown"


# ==================== 蒸馏输出模型 ====================

@dataclass
class DistillOutput:
    """QueenDistiller.distill() 的输出（标准化格式）"""

    ticker: str
    final_score: float
    direction: str
    resonance: Dict = field(default_factory=dict)
    supporting_agents: int = 0
    agent_breakdown: Dict[str, int] = field(default_factory=dict)
    dimension_scores: Dict[str, float] = field(default_factory=dict)
    dimension_confidence: Dict[str, float] = field(default_factory=dict)
    dimension_weights: Dict[str, float] = field(default_factory=dict)
    data_quality: Dict[str, Any] = field(default_factory=dict)
    data_real_pct: float = 0.0
    distill_mode: str = "rule_engine"

    def __post_init__(self):
        self.ticker = clean_string(self.ticker, default="UNKNOWN", max_len=10).upper()
        self.final_score = clean_score(self.final_score)
        self.direction = clean_direction(self.direction)
        self.data_real_pct = clamp(self.data_real_pct, 0.0, 100.0)

    def to_dict(self) -> Dict:
        return asdict(self)


# ==================== 数据质量检测器 ====================

class DataQualityChecker:
    """
    数据质量自动检测：NaN / null / outlier / 缺失字段

    用法：
        checker = DataQualityChecker()
        issues = checker.check_agent_result(result_dict)
        cleaned = checker.clean_agent_result(result_dict)
    """

    # 各维度的评分合理区间（超出视为 outlier）
    SCORE_RANGES = {
        "signal":       (0.0, 10.0),
        "catalyst":     (0.0, 10.0),
        "sentiment":    (0.0, 10.0),
        "odds":         (0.0, 10.0),
        "risk_adj":     (0.0, 10.0),
        "ml_auxiliary":  (0.0, 10.0),
    }

    REQUIRED_FIELDS = {"score", "direction", "source", "dimension"}

    def check_agent_result(self, result: Dict) -> List[str]:
        """
        检查单个 Agent 结果的数据质量

        Returns:
            问题列表（空 = 无问题）
        """
        if not result or not isinstance(result, dict):
            return ["result is None or not a dict"]

        issues = []

        # 必要字段检查
        for f in self.REQUIRED_FIELDS:
            if f not in result:
                issues.append(f"missing required field: {f}")

        # score 检查
        score = result.get("score")
        if score is None:
            issues.append("score is None")
        elif isinstance(score, float) and (math.isnan(score) or math.isinf(score)):
            issues.append(f"score is {score}")
        elif not (0.0 <= float(score) <= 10.0):
            issues.append(f"score out of range: {score}")

        # confidence 检查
        conf = result.get("confidence")
        if conf is not None:
            try:
                cv = float(conf)
                if math.isnan(cv) or math.isinf(cv):
                    issues.append(f"confidence is {conf}")
                elif not (0.0 <= cv <= 1.0):
                    issues.append(f"confidence out of range: {conf}")
            except (TypeError, ValueError):
                issues.append(f"confidence not numeric: {conf}")

        # direction 检查
        direction = result.get("direction")
        if direction and direction not in ("bullish", "bearish", "neutral"):
            issues.append(f"invalid direction: {direction}")

        return issues

    def clean_agent_result(self, result: Dict) -> Optional[Dict]:
        """
        清洗单个 Agent 结果：修复可修复的问题，不可修复返回 None

        Returns:
            清洗后的 dict，或 None（数据不可用）
        """
        if not result or not isinstance(result, dict):
            return None

        # error 结果直接过滤
        if "error" in result and "score" not in result:
            return None

        cleaned = dict(result)

        # 清洗各字段
        cleaned["score"] = clean_score(cleaned.get("score"))
        cleaned["direction"] = clean_direction(cleaned.get("direction"))
        cleaned["confidence"] = clean_confidence(cleaned.get("confidence"))
        cleaned["discovery"] = clean_string(cleaned.get("discovery"))
        cleaned["source"] = clean_string(cleaned.get("source"), default="Unknown")
        cleaned["dimension"] = clean_string(cleaned.get("dimension"), default="unknown")

        return cleaned

    def clean_results_batch(self, results: List) -> List[Dict]:
        """
        批量清洗 Agent 结果列表

        - 过滤 None
        - 过滤 error 结果
        - 清洗有效结果
        """
        if not results:
            return []

        cleaned = []
        for r in results:
            if r is None:
                continue
            c = self.clean_agent_result(r)
            if c is not None:
                cleaned.append(c)
        return cleaned
