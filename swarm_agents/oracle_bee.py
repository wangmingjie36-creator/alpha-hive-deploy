"""OracleBeeEcho - 市场预期蜂 (Odds 维度, 权重 0.15)"""

from typing import Any, Dict, List, Optional
from swarm_agents._config import _log, _AS
from swarm_agents.cache import _safe_score
from swarm_agents.base import BeeAgent
from models import AgentResult
from swarm_agents.utils import (
    clamp_score, append_context, blend_with_llm, build_confidence,
    make_error_result, AGENT_ERRORS, LLM_ERRORS,
)


class OracleBeeEcho(BeeAgent):
    """市场预期蜂 - 期权分析 + Polymarket 预测市场赔率
    对应维度：Odds (权重 0.15)
    融合：期权信号 60% + Polymarket 赔率 40%
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)

            # 获取真实股价
            stock = self._get_stock_data(ticker)
            current_price = stock["price"]

            # ---- 期权分析（60%）----
            options_score = 5.0
            signal_summary = "期权数据不可用"
            try:
                from options_analyzer import OptionsAgent
                agent = OptionsAgent()
                result = agent.analyze(ticker, stock_price=current_price)
                options_score = _safe_score(result.get("options_score"), 5.0, 0, 10, "options_score")
                signal_summary = result.get("signal_summary", "平衡")
            except (ImportError, ConnectionError, ValueError, KeyError, TypeError) as e:
                _log.warning("OracleBeeEcho options unavailable for %s: %s", ticker, e)
                result = {}

            # ---- Polymarket 赔率（40%）----
            poly_score = 5.0
            poly_signal = ""
            try:
                from polymarket_client import get_polymarket_odds
                poly = get_polymarket_odds(ticker)
                poly_score = _safe_score(poly.get("odds_score"), 5.0, 0, 10, "poly_score")
                poly_signal = poly.get("odds_signal", "")
                poly_markets = poly.get("markets_found", 0)
            except LLM_ERRORS as e:
                _log.warning("OracleBeeEcho Polymarket unavailable for %s: %s", ticker, e)
                poly_markets = 0

            # ---- P2: 异常期权流检测（大单 OTM 买入 / 短期扫单）----
            unusual_flow = {}
            unusual_score_adj = 0.0
            try:
                from unusual_options import detect_unusual_flow
                unusual_flow = detect_unusual_flow(ticker, stock_price=current_price)
                if unusual_flow.get("data_source") != "fallback":
                    uf_score = unusual_flow.get("unusual_score", 5.0)
                    uf_dir = unusual_flow.get("unusual_direction", "neutral")
                    # 异常流作为额外调整项（±1.5 分最大影响）
                    unusual_score_adj = (uf_score - 5.0) * 0.3
                    if unusual_flow.get("signals"):
                        top_sig = unusual_flow["signals"][0]
                        signal_summary = f"{signal_summary} | 异常流:{unusual_flow['summary']}"
            except (ImportError, ConnectionError, ValueError, KeyError, TypeError) as e:
                _log.debug("P2 unusual_options 不可用 %s: %s", ticker, e)

            # ---- 融合评分（期权 + Polymarket + 异常流）----
            _ow = _AS.get("oracle_options_weight", 0.55)
            _pw = _AS.get("oracle_poly_weight", 0.35)
            _uw = _AS.get("oracle_unusual_weight", 0.10)
            if poly_markets > 0:
                # BUG FIX: 原来 5.0 硬编码为 unusual_flow 的占位分，
                # 改为使用实际 unusual_score（如无则 5.0 = 中性）
                _unusual_base = unusual_flow.get("unusual_score", 5.0) if unusual_flow else 5.0
                score = options_score * _ow + poly_score * _pw + _unusual_base * _uw
            else:
                # BUG FIX: poly_markets=0 时原来直接用 options_score（weight=1.0），
                # 完全忽略了 unusual_flow 权重。
                # 修复：将 _ow + _uw 重新归一化，按比例混合 options + unusual，
                # 如果 unusual 也不可用则退回纯 options_score。
                _unusual_base = unusual_flow.get("unusual_score", None) if unusual_flow else None
                if _unusual_base is not None:
                    _total_w = _ow + _uw  # 无 poly 时只用这两个权重
                    if _total_w > 0:
                        score = (options_score * _ow + _unusual_base * _uw) / _total_w
                    else:
                        score = options_score
                else:
                    score = options_score  # 无 poly 也无 unusual，直接用 options
            # 叠加异常流调整
            score = clamp_score(score + unusual_score_adj)

            # 从 signal_summary 推断方向（异常流可覆盖）
            if unusual_flow.get("unusual_direction") in ("bullish", "bearish"):
                direction = unusual_flow["unusual_direction"]
            elif "多" in signal_summary or "增强" in signal_summary or "看涨" in signal_summary:
                direction = "bullish"
            elif "空" in signal_summary or "看跌" in signal_summary:
                direction = "bearish"
            elif score < _AS.get("oracle_bearish_score_threshold", 4.0):
                direction = "bearish"
            elif score > _AS.get("oracle_bullish_score_threshold", 6.5):
                direction = "bullish"
            else:
                direction = "neutral"

            discovery = f"{signal_summary} | ${current_price:.1f}"
            if poly_signal:
                discovery += f" | {poly_signal}"
            discovery = append_context(discovery, ctx)

            # ── P1: LLM 期权流结构解读（识别聪明钱意图，超越阈值规则）──
            llm_options = None
            try:
                import llm_service
                if llm_service.is_available() and result:
                    llm_options = llm_service.interpret_options_flow(ticker, result, stock)
                    if llm_options:
                        score = blend_with_llm(score, llm_options, "smart_money_score", 0.6, "OracleBee_smart_money")
                        llm_dir = llm_options.get("smart_money_direction", direction)
                        if llm_dir in ("bullish", "bearish", "neutral"):
                            direction = llm_dir
                        flow_reason = llm_options.get("flow_reasoning", "")
                        signal_type = llm_options.get("signal_type", "")
                        if flow_reason:
                            discovery = f"{discovery} | LLM期权:{flow_reason}"
                        if signal_type:
                            discovery = f"{discovery}[{signal_type}]"
            except LLM_ERRORS as e:
                _log.debug("OracleBeeEcho LLM unavailable for %s: %s", ticker, e)

            # S14: IV Skew 信号融入评分
            _skew_ratio = None
            if result:
                _skew_ratio = result.get("iv_skew_ratio")
                _skew_sig = result.get("iv_skew_signal", "")
                if _skew_ratio is not None:
                    if _skew_ratio > 1.3:
                        # 机构恐慌对冲 → bearish 渐进式惩罚（-0.25~-1.0）
                        score = clamp_score(score - min(1.0, (_skew_ratio - 1.3) * 2.5))
                        discovery = f"{discovery} | Skew {_skew_ratio:.2f}({_skew_sig})"
                    elif _skew_ratio < 0.8:
                        # call 投机过热 → bullish 渐进式加分（+0.1~+0.5）
                        _bull_adj = min(0.5, (0.8 - _skew_ratio) * 1.5)
                        score = clamp_score(score + _bull_adj)
                        discovery = f"{discovery} | Skew {_skew_ratio:.2f}({_skew_sig})"

            # S3: 结构化数据交换（BearBee 可直接读取，替代正则解析）
            _pub_details = {}
            if result:
                _pub_details["pc_ratio"] = result.get("put_call_ratio")
                _pub_details["iv_rank"] = result.get("iv_rank")
                _pub_details["gex"] = result.get("gamma_exposure")  # A2: OptionsAgent 返回 "gamma_exposure"
                if _skew_ratio is not None:
                    _pub_details["iv_skew"] = _skew_ratio  # S14: 仅有值时设置
            self._publish(ticker, discovery, "options+polymarket", score, direction, details=_pub_details)

            # Phase 2: confidence = 期权数据可用 + Polymarket 可用 + LLM 加成
            confidence = build_confidence(0.4, [
                (bool(result), 0.3),
                (poly_markets > 0, 0.1),
                (bool(llm_options), 0.2),
            ])

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="OracleBeeEcho",
                dimension="odds",
                data_quality={
                    "options": "real" if result else "fallback",
                    "polymarket": "real" if poly_markets > 0 else "unavailable",
                },
                details=result,
                extras={
                    "polymarket_score": poly_score,
                    "polymarket_markets": poly_markets,
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("OracleBeeEcho failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("OracleBeeEcho", "odds", e)
