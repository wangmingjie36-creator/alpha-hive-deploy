"""
🐝 Alpha Hive — GEX 政体联动评分 + 政体条件权重调节器

升级 #1: GexRegimeModifier
  - 读取 DealerGEXAnalyzer 输出（total_gex, gex_flip, regime, vanna_stress）
  - 当 GEX 负值 + flip 临近 → 抑制看多信号，增强看空信号
  - 当 GEX 正值 + 远离 flip → 信号正常

升级 #4: RegimeWeightAdjuster
  - 根据宏观政体（risk_on / risk_off / neutral）+ GEX 政体
  - 动态调整 5 维评分权重（±15% 幅度，保持总和 = 1.0）
"""

import logging
from typing import Dict, Optional, Tuple

_log = logging.getLogger("alpha_hive.gex_regime")


# ═══════════════════════════════════════════════════════════════════════════
#  升级 #1: GEX 政体联动评分
# ═══════════════════════════════════════════════════════════════════════════

class GexRegimeModifier:
    """
    根据 Dealer GEX 状态对蜂群 final_score 施加调整。

    核心逻辑：
    - GEX 负值（dealer 做空 gamma）→ 市场波动放大，看多信号可靠性下降
    - GEX 正值（dealer 做多 gamma）→ 市场趋于稳定，信号可靠性正常
    - flip 距离越近（<3%），GEX 翻转概率越高，不确定性越大
    - vanna_stress.can_flip_gex = True → 极端情况，信号强烈衰减
    """

    # 调整上下界（防止单一因子过度影响）
    MAX_ADJUSTMENT = 0.8
    MIN_ADJUSTMENT = -0.8

    def compute(self, dealer_gex: Dict, direction: str = "neutral") -> Dict:
        """
        计算 GEX 政体调整值。

        Args:
            dealer_gex: DealerGEXAnalyzer.analyze() 输出
            direction:  当前蜂群投票方向 ("bullish"/"bearish"/"neutral")

        Returns:
            {
                "gex_adjustment": float,     # 对 final_score 的调整（-0.8 ~ +0.8）
                "gex_regime": str,           # "positive_gex" / "negative_gex" / "unknown"
                "flip_proximity_pct": float, # 当前价距 flip 的百分比
                "can_flip_vanna": bool,      # vanna 压力是否可翻转 GEX
                "regime_description": str,   # 人类可读描述
                "confidence_modifier": float # 对置信带宽的修正因子（1.0=不变，>1=加宽）
            }
        """
        if not dealer_gex or dealer_gex.get("error"):
            return self._neutral_result("GEX 数据不可用")

        total_gex = dealer_gex.get("total_gex", 0.0)
        regime = dealer_gex.get("regime", "unknown")
        gex_flip = dealer_gex.get("gex_flip")
        stock_price = dealer_gex.get("stock_price", 0.0)
        vanna = dealer_gex.get("vanna_stress", {})
        can_flip = vanna.get("can_flip_gex", False)

        # ── 1. flip 距离百分比 ──
        flip_pct = 999.0
        if gex_flip and stock_price and stock_price > 0:
            flip_pct = abs(stock_price - gex_flip) / stock_price * 100

        # ── 2. GEX 方向调整 ──
        adjustment = 0.0
        conf_modifier = 1.0  # 置信带宽修正因子
        desc_parts = []

        if regime == "negative_gex":
            # 基础惩罚：GEX 负值 → 波动放大环境
            adjustment -= 0.3
            conf_modifier += 0.15  # 加宽置信带
            desc_parts.append("Dealer γ-短仓(波动放大)")

            # 对看多信号额外惩罚
            if direction == "bullish":
                adjustment -= 0.2
                desc_parts.append("看多信号在负GEX下可靠性降低")

            # flip 临近（<3%）→ 额外不确定性
            if flip_pct < 3.0:
                adjustment -= 0.2
                conf_modifier += 0.10
                desc_parts.append(f"距GEX翻转仅{flip_pct:.1f}%")
            elif flip_pct < 5.0:
                adjustment -= 0.1
                desc_parts.append(f"距GEX翻转{flip_pct:.1f}%")

            # vanna 可翻转 → 极端情况
            if can_flip:
                adjustment -= 0.1
                conf_modifier += 0.10
                desc_parts.append("Vanna压力可触发GEX翻转")

        elif regime == "positive_gex":
            # GEX 正值 → 稳定环境
            desc_parts.append("Dealer γ-多仓(波动压缩)")

            # 对看空信号小幅抑制（稳定环境下空头更难盈利）
            if direction == "bearish":
                adjustment += 0.1
                desc_parts.append("正GEX环境下空头面临gamma压缩")

            # 但如果 flip 很近（<2%），即使正 GEX 也要小心
            if flip_pct < 2.0:
                adjustment -= 0.15
                conf_modifier += 0.10
                desc_parts.append(f"⚠️ 接近GEX翻转点({flip_pct:.1f}%)")

        # ── 3. clamp ──
        adjustment = max(self.MIN_ADJUSTMENT, min(self.MAX_ADJUSTMENT, adjustment))
        conf_modifier = max(1.0, min(1.5, conf_modifier))

        return {
            "gex_adjustment": round(adjustment, 2),
            "gex_regime": regime,
            "flip_proximity_pct": round(flip_pct, 2) if flip_pct < 999 else None,
            "can_flip_vanna": can_flip,
            "regime_description": " | ".join(desc_parts) if desc_parts else "GEX 数据正常",
            "confidence_modifier": round(conf_modifier, 2),
        }

    @staticmethod
    def _neutral_result(reason: str) -> Dict:
        return {
            "gex_adjustment": 0.0,
            "gex_regime": "unknown",
            "flip_proximity_pct": None,
            "can_flip_vanna": False,
            "regime_description": reason,
            "confidence_modifier": 1.0,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  升级 #4: 政体条件权重调节器
# ═══════════════════════════════════════════════════════════════════════════

class RegimeWeightAdjuster:
    """
    根据市场政体动态调整 5 维评分权重。

    设计原则：
    - Risk-off → 加权 Guard/Bear（risk_adj）, 降 Buzz（sentiment）
    - Risk-on  → 加权 Scout（signal）, 降 Guard
    - Negative GEX → 加权 Oracle（odds/期权信号更重要）, 降 Scout
    - 高 IV 环境 → 加权 Oracle, 降 Buzz（情绪在高 IV 下是噪音）
    - 调整幅度 ±15%（相对于基准权重），总和始终归一化到 1.0
    """

    # 最大偏移百分比（相对于基准权重）
    MAX_SHIFT_PCT = 0.15

    def adjust_weights(
        self,
        base_weights: Dict[str, float],
        macro_regime: str = "neutral",
        gex_regime: str = "unknown",
        iv_rank: Optional[float] = None,
    ) -> Tuple[Dict[str, float], str]:
        """
        返回调整后的权重 dict 和描述字符串。

        Args:
            base_weights: 原始 EVALUATION_WEIGHTS（如 {"signal": 0.30, ...}）
            macro_regime: "risk_on" / "risk_off" / "neutral"
            gex_regime:   "positive_gex" / "negative_gex" / "unknown"
            iv_rank:      当前标的 IV Rank（0-100），None 则不调整

        Returns:
            (adjusted_weights, description)
        """
        # 复制基准权重
        w = {k: v for k, v in base_weights.items()}
        shifts = {k: 0.0 for k in w}
        reasons = []

        def _shift(dim: str, val: float) -> None:
            """安全累加偏移量（忽略不存在的维度）"""
            if dim in shifts:
                shifts[dim] += val

        # ── 宏观政体调整 ──
        if macro_regime == "risk_off":
            _shift("risk_adj", 0.12)     # Guard 加权
            _shift("sentiment", -0.08)   # 情绪信号降级（恐慌噪音多）
            _shift("signal", -0.04)      # 聪明钱信号略降（risk_off 时趋势不明）
            reasons.append("Risk-off:加权风控/降情绪")
        elif macro_regime == "risk_on":
            _shift("signal", 0.08)       # 聪明钱信号加权
            _shift("catalyst", 0.04)     # 催化剂更可靠
            _shift("risk_adj", -0.12)    # Guard 降权（低风险环境）
            reasons.append("Risk-on:加权信号/降风控")

        # ── GEX 政体调整 ──
        if gex_regime == "negative_gex":
            _shift("odds", 0.08)         # 期权信号在负 GEX 下更有价值
            _shift("signal", -0.04)      # 聪明钱信号可靠性降
            _shift("sentiment", -0.04)   # 情绪更嘈杂
            reasons.append("负GEX:加权期权/降情绪")
        elif gex_regime == "positive_gex":
            _shift("signal", 0.03)       # 正 GEX 下趋势更明确
            _shift("odds", -0.03)        # 期权信号占比不需要那么高
            reasons.append("正GEX:信号环境稳定")

        # ── 高 IV 环境调整 ──
        if iv_rank is not None:
            if iv_rank >= 70:
                _shift("odds", 0.06)         # 期权定价信号更丰富
                _shift("sentiment", -0.06)   # 高 IV 下情绪是纯噪音
                reasons.append(f"高IV({iv_rank:.0f}):加权期权/降情绪")
            elif iv_rank <= 20:
                _shift("sentiment", 0.04)    # 低 IV 下情绪信号相对可靠
                _shift("odds", -0.04)        # 期权信号在低 IV 下价值低
                reasons.append(f"低IV({iv_rank:.0f}):加权情绪/降期权")

        # ── 施加偏移（clamp 到 ±MAX_SHIFT_PCT）──
        for k in w:
            shift = max(-self.MAX_SHIFT_PCT, min(self.MAX_SHIFT_PCT, shifts[k]))
            w[k] = max(0.02, w[k] + shift * w[k])  # 相对偏移，最小 2%

        # ── 归一化到 1.0 ──
        total = sum(w.values())
        if total > 0:
            w = {k: round(v / total, 4) for k, v in w.items()}

        desc = " | ".join(reasons) if reasons else "权重未调整（中性环境）"
        return w, desc
