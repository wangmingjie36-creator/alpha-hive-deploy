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
                # 双向守卫：类型检查 + NaN 检查 + 双侧边界（统一上界来自 config）
                if not isinstance(prob, (int, float)) or prob != prob:  # NaN guard
                    _log.warning("RivalBeeVanguard: ML prob 异常值 %r，回退 0.5", prob)
                    prob = 0.5
                try:
                    from config import ML_PROB_CAP as _ML_CAP
                except ImportError:
                    _ML_CAP = 0.95
                prob = max(0.0, min(float(prob), _ML_CAP))
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

            # ---- EPS Revision Momentum（分析师共识 proxy）----
            eps_rev = self._assess_eps_revision(ticker)
            eps_sig = eps_rev.get("revision_signal", "unknown")
            if eps_sig not in ("unknown",):
                rev_summary = eps_rev.get("revision_summary", "")
                if rev_summary:
                    discovery = f"{discovery} | {rev_summary}"
                # 正面修正微升分，负面修正微降分（权重 4%，避免喧宾夺主）
                if eps_sig == "positive":
                    score = min(10.0, score * 1.04)
                    if direction == "neutral":
                        direction = "bullish"
                elif eps_sig == "negative":
                    score = max(0.0, score * 0.96)

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
                details={
                    **(prediction if prediction else {"momentum_5d": stock["momentum_5d"]}),
                    "eps_revision": eps_rev,
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("RivalBeeVanguard failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("RivalBeeVanguard", "ml_auxiliary", e)

    # ---------- analyst EPS revision helpers ----------

    def _assess_eps_revision(self, ticker: str) -> Dict:
        """用 yfinance.info 提取分析师 EPS/目标价共识，作为 EPS Revision 动量代理指标。

        免费数据局限：yfinance 无法取得历史 consensus 序列，
        故以当前共识偏差（上行空间 + 分析师方向 + 远期EPS增速）作为 proxy。

        Returns:
            {
              "forward_eps": float|None,
              "trailing_eps": float|None,
              "eps_growth_proj": float|None,      # (forward/trailing - 1) * 100
              "target_mean_price": float|None,
              "current_price": float|None,
              "upside_to_target": float|None,     # (target/price - 1) * 100
              "recommendation_mean": float|None,  # 1=strong buy … 5=strong sell
              "num_analyst_opinions": int|None,
              "revision_signal": "positive"|"negative"|"neutral"|"unknown",
              "revision_summary": str,
            }
        """
        result: Dict = {"revision_signal": "unknown", "revision_summary": ""}
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info

            fwd_eps  = info.get("forwardEps")
            trail_eps = info.get("trailingEps")
            target   = info.get("targetMeanPrice")
            price    = info.get("currentPrice") or info.get("regularMarketPrice")
            rec_mean = info.get("recommendationMean")   # 1-5 float
            n_ana    = info.get("numberOfAnalystOpinions")

            # EPS 增速代理
            eps_growth = None
            if fwd_eps and trail_eps and trail_eps != 0:
                eps_growth = round((fwd_eps / trail_eps - 1) * 100, 1)

            # 上行空间
            upside = None
            if target and price and price > 0:
                upside = round((target / price - 1) * 100, 1)

            result.update({
                "forward_eps":         fwd_eps,
                "trailing_eps":        trail_eps,
                "eps_growth_proj":     eps_growth,
                "target_mean_price":   target,
                "current_price":       price,
                "upside_to_target":    upside,
                "recommendation_mean": rec_mean,
                "num_analyst_opinions": n_ana,
            })

            # ---- 综合打分为修正信号 ----
            pos_pts = 0; neg_pts = 0
            summary_parts = []

            # 上行空间
            if upside is not None:
                if upside >= 20:
                    pos_pts += 2
                    summary_parts.append(f"目标价上行 {upside:+.1f}%（分析师看多）")
                elif upside >= 8:
                    pos_pts += 1
                    summary_parts.append(f"目标价上行 {upside:+.1f}%")
                elif upside <= -5:
                    neg_pts += 2
                    summary_parts.append(f"目标价低于现价 {upside:.1f}%（分析师下调）")

            # 分析师评级方向
            if rec_mean is not None:
                if rec_mean <= 1.8:
                    pos_pts += 2
                    summary_parts.append(f"分析师共识{rec_mean:.1f}/5（强烈看多）")
                elif rec_mean <= 2.5:
                    pos_pts += 1
                    summary_parts.append(f"分析师共识{rec_mean:.1f}/5（偏多）")
                elif rec_mean >= 4.2:
                    neg_pts += 2
                    summary_parts.append(f"分析师共识{rec_mean:.1f}/5（强烈卖出）")
                elif rec_mean >= 3.5:
                    neg_pts += 1
                    summary_parts.append(f"分析师共识{rec_mean:.1f}/5（偏空/持有）")

            # 远期 EPS 增速
            if eps_growth is not None:
                if eps_growth >= 20:
                    pos_pts += 1
                    summary_parts.append(f"远期EPS增速 {eps_growth:+.1f}%（盈利高增长）")
                elif eps_growth <= -10:
                    neg_pts += 1
                    summary_parts.append(f"远期EPS增速 {eps_growth:+.1f}%（盈利下修预警）")

            if pos_pts > neg_pts + 1:
                result["revision_signal"] = "positive"
            elif neg_pts > pos_pts + 1:
                result["revision_signal"] = "negative"
            elif pos_pts > 0 or neg_pts > 0:
                result["revision_signal"] = "neutral"

            result["revision_summary"] = " | ".join(summary_parts) if summary_parts else "分析师数据不足"

        except Exception as e:
            _log.debug("RivalBeeVanguard EPS revision unavailable for %s: %s", ticker, e)
        return result
