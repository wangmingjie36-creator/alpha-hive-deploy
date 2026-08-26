"""
方案12: 共享正确性判定逻辑 — OutcomesFetcher 与 Backtester 统一标准

问题：两个系统对"预测正确"的定义不一致：
  - OutcomesFetcher: return > 0 → correct (严格零容差)
  - Backtester: return > -1% → correct (含 1% 容差)

统一后：使用可配置容差。

────────────────────────────────────────────────────────────────
v0.45.9 (2026-08-25) P0 修复：单边亏损豁免 → 双边模糊带
────────────────────────────────────────────────────────────────
旧实现是**单边容差**：
    看多 correct if return > -1.0   ← 亏 0.9% 记为「预测正确」
    看空 correct if return < +1.0   ← 逆向涨 0.9% 记为「预测正确」
这不是中性带，是给亏损单发免罪符。实测影响（pheromone.db 全量）：
    T+1 方向样本 679 条，175 条(25.8%)「判对」实为亏损单，
        其中 173 条恰落在 ±1% 带内；准确率 72.9% → 真实符号 47.4%
    T+7 方向样本 647 条，20 条虚高；55.6% → 52.6%
虚高指标被 backtester.adapt_weights / weekly_optimizer 权重自适应 /
ml_predictor 训练标签共同消费，等于全系统在优化一个假目标。

新实现是**三态双边**：
    |return| <= tolerance          → "ambiguous"（噪音，从准确率统计中剔除）
    超出容差且方向一致              → "correct"
    超出容差且方向相反              → "incorrect"
ambiguous 既不算对也不算错，落库到 ambiguous_{period} 列，所有准确率
查询需带 `AND ambiguous_{period} = 0`。

注意「中性」方向的判定**不变**：中性预测的语义就是「不会大幅波动」，
|return| < neutral_tolerance 判对是名副其实的，不是亏损豁免。
"""

from typing import Optional, Tuple

# 方向容差（百分比形式）：|return| <= 该值视为噪音（ambiguous），不计入准确率
DEFAULT_TOLERANCE_PCT = 1.0
# 中性方向容差：实际收益在 ±5% 内视为"中性正确"
# v0.38.1: 3.0 → 5.0。全样本回放（experiments/neutral_band_replay.py，164 条中性）：
# ±3% 命中仅 36%——高波动政体下 61-77% 样本 |T+7|>3%，"中性但 |ret|>3%"本质是
# 该标的的正常波动而非预测错误；±5% 命中 52%，与波动率缩放口径（53%）几乎等效
# 但实现简单（±5% ≈ 0.674×σ7 在本组合典型 σ7≈7.4% 下的近似）。
# 仅影响准确率记账（backtester 统计 / outcomes_fetcher 回填标签），不影响交易行为。
DEFAULT_NEUTRAL_TOLERANCE_PCT = 5.0


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
        "correct"   方向判对（幅度超出容差）
        "incorrect" 方向判错（幅度超出容差）
        "ambiguous" |return| 在容差内，噪音，不计入准确率（v0.45.9 新增）
        "neutral"   无收益数据或方向无法识别
    """
    if return_pct is None:
        return "neutral"

    # 统一方向名称
    _dir = direction.strip().lower()
    if _dir in ("long", "bullish"):
        if abs(return_pct) <= tolerance_pct:
            return "ambiguous"
        return "correct" if return_pct > 0 else "incorrect"
    elif _dir in ("short", "bearish"):
        if abs(return_pct) <= tolerance_pct:
            return "ambiguous"
        return "correct" if return_pct < 0 else "incorrect"
    elif _dir in ("neutral",):
        # 中性预测的语义是「不会大幅波动」，此处的带宽是名副其实的判定标准，
        # 不是亏损豁免，故保持原逻辑
        return "correct" if abs(return_pct) < neutral_tolerance_pct else "incorrect"
    else:
        return "neutral"


def determine_outcome_triplet(
    direction: str,
    return_pct: Optional[float],
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    neutral_tolerance_pct: float = DEFAULT_NEUTRAL_TOLERANCE_PCT,
) -> Tuple[bool, bool]:
    """
    落库友好版本 —— 返回 (correct, ambiguous)。

    写 DB 时应同时写 correct_{period} 与 ambiguous_{period}；
    读准确率时必须带 `AND ambiguous_{period} = 0`。

    Returns:
        (True,  False) 判对
        (False, False) 判错
        (False, True )  模糊（噪音，不计入分子也不计入分母）
    """
    result = determine_correctness(direction, return_pct,
                                   tolerance_pct, neutral_tolerance_pct)
    return (result == "correct", result == "ambiguous")


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
        True = 方向正确, False = 方向错误或模糊

    ⚠️ v0.45.9 起 ambiguous 会返回 False。若调用方需要把模糊样本从统计中
    剔除（而不是记为错误），请改用 determine_outcome_triplet()。
    """
    result = determine_correctness(direction, return_pct, tolerance_pct, neutral_tolerance_pct)
    return result == "correct"
