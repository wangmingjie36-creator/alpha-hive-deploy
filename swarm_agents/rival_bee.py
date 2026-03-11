"""RivalBeeVanguard - 竞争分析与 ML 预测蜂 (ML 辅助维度)"""

from typing import Any, Dict, List, Optional
from swarm_agents._config import _log, _AS
from swarm_agents.cache import _safe_score
from swarm_agents.base import BeeAgent
from models import AgentResult
from swarm_agents.utils import (
    clamp_score, append_context, make_error_result, AGENT_ERRORS,
)


class RivalBeeVanguard(BeeAgent):
    """竞争分析与 ML 预测蜂 - 概率预测 + 行业动量对标
    不直接参与 5 维公式，作为额外调整项
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)

            # 尝试 ML 预测
            prediction = {}
            try:
                from ml_predictor_extended import MLPredictionService, TrainingData
                from datetime import datetime
                service = MLPredictionService()

                stock = self._get_stock_data(ticker)
                opportunity = TrainingData(
                    ticker=ticker,
                    date=datetime.now().strftime("%Y-%m-%d"),
                    crowding_score=50.0,
                    catalyst_quality="B+",
                    momentum_5d=stock["momentum_5d"],
                    volatility=stock["volatility_20d"],
                    market_sentiment=stock["momentum_5d"] * 5,
                    iv_rank=50.0,
                    put_call_ratio=1.0,
                    actual_return_3d=0.0,
                    actual_return_7d=0.0,
                    actual_return_30d=0.0,
                    win_3d=False,
                    win_7d=False,
                    win_30d=False,
                )
                prediction = service.predict_for_opportunity(opportunity)
            except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
                _log.warning("RivalBeeVanguard ML prediction unavailable for %s: %s", ticker, e)

            if prediction:
                prob = prediction.get("probability", 0.5)
                # 硬限制：ML 模型过度自信时（小样本场景），概率不应超过 95%
                prob = min(prob, 0.95)
                prediction["probability"] = prob   # 回写，保持 details 一致
                ret_7d = prediction.get("expected_7d", 0.0)
                ret_30d = prediction.get("expected_30d", 0.0)
                avg_ret = (ret_7d + ret_30d) / 2

                score = prob * 10  # 胜率 → 0-10
                score = clamp_score(score)

                direction = "bullish" if avg_ret > 0 else ("bearish" if avg_ret < 0 else "neutral")

                discovery = f"ML 胜率 {prob*100:.0f}% | 7d {ret_7d:+.2f}% | 30d {ret_30d:+.2f}%"
            else:
                # ML 不可用，用简单动量对标
                stock = self._get_stock_data(ticker)
                mom = stock["momentum_5d"]
                score = clamp_score(5.0 + mom * 0.3)
                _rival_bear_m = _AS.get("rival_bearish_momentum", -1.5)
                _rival_bull_m = _AS.get("rival_bullish_momentum", 2.0)
                direction = "bullish" if mom > _rival_bull_m else ("bearish" if mom < _rival_bear_m else "neutral")
                discovery = f"动量对标 {mom:+.1f}% | 波动率 {stock['volatility_20d']:.0f}%"

            discovery = append_context(discovery, ctx)

            self._publish(ticker, discovery, "ml_predictor", round(score, 2), direction,
                         details={
                             "ml_probability": prediction.get("probability") if prediction else None,
                             "expected_7d": prediction.get("expected_7d") if prediction else None,
                             "expected_30d": prediction.get("expected_30d") if prediction else None,
                             "momentum_5d": stock.get("momentum_5d") if stock else None,
                         })

            # Phase 2: confidence = ML 模型可用性
            confidence = 0.3 if not prediction else 0.8
            confidence = min(1.0, confidence)

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="RivalBeeVanguard",
                dimension="ml_auxiliary",
                data_quality={
                    "ml_prediction": "real" if prediction else "fallback_momentum",
                },
                details=prediction if prediction else {"momentum_5d": stock["momentum_5d"]},
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("RivalBeeVanguard failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("RivalBeeVanguard", "ml_auxiliary", e)
