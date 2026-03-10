"""
方案12: 共享正确性判定逻辑 — OutcomesFetcher 与 Backtester 统一标准

问题：两个系统对"预测正确"的定义不一致：
  - OutcomesFetcher: return > 0 → correct (严格零容差)
  - Backtester: return > -1% → correct (含 1% 容差)

统一后：使用可配置容差，默认 -1%（允许小幅逆向波动不视为方向错误）
"""

from typing import Optional

# 默认容差（百分比形式）：允许 ±1% 的逆向波动仍视为方向正确
DEFAULT_TOLERANCE_PCT = 1.0
# 中性方向容差：实际收益在 ±3% 内视为"中性正确"
DEFAULT_NEUTRAL_TOLERANCE_PCT = 3.0


def determine_correctness(
    direction: str,
    return_pct: Optional[float],
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    neutral_tolerance_pct: float = DEFAULT_NEUTRAL_TOLERANCE_PCT,
) -> str:
    """
    判断预测方向是否正确（统一标准）

    Args:
        direction: 预测方向，支持多种格式:
            - "Long" / "bullish" → 看多
            - "Short" / "bearish" → 看空
            - "Neutral" / "neutral" → 中性
        return_pct: 实际收益率（百分比，如 5.0 = +5%，-3.2 = -3.2%）
        tolerance_pct: 看多/看空容差百分比（默认 1.0%）
        neutral_tolerance_pct: 中性容差百分比（默认 3.0%）

    Returns:
        "correct" / "incorrect" / "neutral"（Neutral 无实际收益时返回 "neutral"）
    """
    if return_pct is None:
        return "neutral"

    # 统一方向名称
    _dir = direction.strip().lower()
    if _dir in ("long", "bullish"):
        # 看多：实际收益 > -tolerance 即为正确
        return "correct" if return_pct > -tolerance_pct else "incorrect"
    elif _dir in ("short", "bearish"):
        # 看空：实际收益 < +tolerance 即为正确
        return "correct" if return_pct < tolerance_pct else "incorrect"
    elif _dir in ("neutral",):
        # 中性：收益在 ±neutral_tolerance 内为正确
        return "correct" if abs(return_pct) < neutral_tolerance_pct else "incorrect"
    else:
        return "neutral"


def determine_correctness_bool(
    direction: str,
    return_pct: float,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    neutral_tolerance_pct: float = DEFAULT_NEUTRAL_TOLERANCE_PCT,
) -> bool:
    """
    布尔版本（供 Backtester 使用）

    Args:
        direction: "bullish" / "bearish" / "neutral"
        return_pct: 实际收益率（百分比）
        tolerance_pct: 看多/看空容差百分比
        neutral_tolerance_pct: 中性容差百分比

    Returns:
        True = 方向正确, False = 方向错误
    """
    result = determine_correctness(direction, return_pct, tolerance_pct, neutral_tolerance_pct)
    return result == "correct"
