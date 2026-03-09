"""Agent 公共工具函数

提取自 7 个 BeeAgent 子类中的重复模式（方案七）：
- clamp_score / clamp_score_cfg: 分数边界钳制（25 处 → 1 函数）
- make_error_result: 标准错误返回结构（7 处 → 1 函数）
- build_confidence: 置信度增量构建器（7 处 → 1 函数）
- append_context: 历史上下文拼接（7 处 → 1 函数）
- blend_with_llm: 规则+LLM 加权混合（6 处 → 1 函数）
- AGENT_ERRORS / LLM_ERRORS: 标准异常元组常量
"""

from typing import Dict, Optional

from swarm_agents._config import _log, _AS
from swarm_agents.cache import _safe_score

# ── 标准异常元组（所有 Agent 外层 try/except 统一引用）──
AGENT_ERRORS = (ImportError, ValueError, KeyError, TypeError, AttributeError)
LLM_ERRORS = (ImportError, ConnectionError, TimeoutError, ValueError, KeyError)


def clamp_score(score: float, lo: float = 1.0, hi: float = 10.0) -> float:
    """分数边界钳制（替代 max(lo, min(hi, score))）"""
    return max(lo, min(hi, score))


def clamp_score_cfg(score: float) -> float:
    """使用 AGENT_SCORING 配置的分数钳制"""
    return max(_AS.get("score_min", 1.0), min(_AS.get("score_max", 10.0), score))


def make_error_result(source: str, dimension: str, error) -> Dict:
    """标准错误返回结构（使用 AgentResult dataclass 保证结构一致性）"""
    from models import AgentResult
    return AgentResult(
        score=5.0, direction="neutral", confidence=0.0,
        discovery=f"Error: {error}", source=source, dimension=dimension,
        data_quality={"source": "error", "freshness": "N/A"},
        error=str(error),
    ).to_dict()


def build_confidence(base: float, increments: list) -> float:
    """置信度增量构建器

    Args:
        base: 基础置信度（0.0~1.0）
        increments: [(condition_bool, increment_float), ...] 条件增量列表

    Returns:
        clamped to [0.0, 1.0]
    """
    c = base
    for cond, inc in increments:
        if cond:
            c += inc
    return min(1.0, c)


def append_context(discovery: str, ctx: str) -> str:
    """附加历史上下文到发现摘要"""
    if ctx:
        return f"{discovery} | {ctx}"
    return discovery


def blend_with_llm(
    rule_score: float,
    llm_result: Optional[Dict],
    score_key: str,
    rule_weight: float,
    label: str = "",
) -> float:
    """规则引擎分数 + LLM 分数加权混合

    Args:
        rule_score: 规则引擎计算的分数
        llm_result: LLM 返回的 dict（可为 None）
        score_key: 从 llm_result 中取分数的 key
        rule_weight: 规则引擎权重（0.0~1.0），LLM 权重 = 1 - rule_weight
        label: 日志标签

    Returns:
        混合后的 clamped score（若 llm_result 为 None 则返回原分）
    """
    if not llm_result:
        return rule_score
    llm_score = _safe_score(
        llm_result.get(score_key),
        default=rule_score,
        lo=1.0,
        hi=10.0,
        label=label,
    )
    llm_weight = round(1.0 - rule_weight, 4)
    blended = round(rule_score * rule_weight + llm_score * llm_weight, 2)
    return clamp_score(blended)
