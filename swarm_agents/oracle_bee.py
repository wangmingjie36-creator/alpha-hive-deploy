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

    def _analyze_term_structure(self, ticker: str, stock_price: float) -> dict:
        """IV 期限结构分析（近月 vs 远月 ATM IV）
        Contango（远月高）= 市场平静 → 偏多
        Backwardation（近月高）= 近期恐慌/催化剂 → 偏空
        """
        result = {"structure": "unknown", "spread": 0.0, "term_score_adj": 0.0, "summary": ""}
        try:
            import yfinance as yf
            from datetime import datetime
            t = yf.Ticker(ticker)
            expirations = list(t.options) if hasattr(t, "options") else []
            if len(expirations) < 2:
                return result
            ivs = []
            for exp in expirations[:3]:
                try:
                    chain = t.option_chain(exp)
                    calls = chain.calls
                    if calls.empty:
                        continue
                    atm_idx = (calls["strike"] - stock_price).abs().idxmin()
                    atm_iv = float(calls.loc[atm_idx, "impliedVolatility"])
                    if atm_iv > 0:
                        exp_date = datetime.strptime(exp, "%Y-%m-%d")
                        dte = max(1, (exp_date - datetime.now()).days)
                        ivs.append({"expiry": exp, "dte": dte, "atm_iv": round(atm_iv * 100, 2)})
                except (KeyError, ValueError, IndexError):
                    continue
            if len(ivs) < 2:
                return result
            near_iv = ivs[0]["atm_iv"]
            far_iv = ivs[-1]["atm_iv"]
            spread = far_iv - near_iv
            if spread > 2.0:
                structure, adj = "contango", +0.3
            elif spread > -2.0:
                structure, adj = "flat", 0.0
            elif spread > -5.0:
                structure, adj = "backwardation", -0.4
            else:
                structure, adj = "severe_backwardation", -0.8
            result = {
                "structure": structure, "spread": round(spread, 2),
                "near_iv": near_iv, "far_iv": far_iv,
                "term_score_adj": adj,
                "summary": f"TermStr:{structure}({near_iv:.0f}/{far_iv:.0f})",
            }
        except Exception as e:
            _log.debug("OracleBee term structure failed for %s: %s", ticker, e)
        return result

    def _analyze_deep_skew(self, ticker: str, stock_price: float) -> dict:
        """25-delta Skew 分析（OTM Put IV vs OTM Call IV）
        Skew > 1.3 → 机构恐慌对冲 → 偏空；Skew < 0.8 → Call 投机过热 → 偏多
        """
        result = {"skew_25d": None, "skew_score_adj": 0.0, "summary": ""}
        try:
            import yfinance as yf, math
            t = yf.Ticker(ticker)
            expirations = list(t.options) if hasattr(t, "options") else []
            if not expirations:
                return result
            chain = t.option_chain(expirations[0])
            calls, puts = chain.calls, chain.puts
            if calls.empty or puts.empty:
                return result
            otm_put_ivs = [row["impliedVolatility"] for _, row in puts.iterrows()
                           if -20 < (row["strike"] / stock_price - 1) * 100 < -5
                           and row.get("impliedVolatility", 0) > 0
                           and not math.isnan(row["impliedVolatility"])]
            otm_call_ivs = [row["impliedVolatility"] for _, row in calls.iterrows()
                            if 5 < (row["strike"] / stock_price - 1) * 100 < 20
                            and row.get("impliedVolatility", 0) > 0
                            and not math.isnan(row["impliedVolatility"])]
            if not otm_put_ivs or not otm_call_ivs:
                return result
            avg_put = sum(otm_put_ivs) / len(otm_put_ivs)
            avg_call = sum(otm_call_ivs) / len(otm_call_ivs)
            skew = avg_put / avg_call if avg_call > 0 else 1.0
            if skew > 1.3:   adj = -0.6
            elif skew > 1.15: adj = -0.3
            elif skew < 0.7:  adj = +0.4
            elif skew < 0.85: adj = +0.2
            else:             adj = 0.0
            result = {
                "skew_25d": round(skew, 3),
                "otm_put_iv": round(avg_put * 100, 1),
                "otm_call_iv": round(avg_call * 100, 1),
                "skew_score_adj": adj,
                "summary": f"Skew25d:{skew:.2f}" + ("(恐慌对冲)" if skew > 1.3 else ""),
            }
        except Exception as e:
            _log.debug("OracleBee deep skew failed for %s: %s", ticker, e)
        return result

    def _calc_max_pain(self, ticker: str, stock_price: float) -> dict:
        """Max Pain 计算（期权到期时令所有持仓亏损最大的价位）"""
        result = {"max_pain": None, "distance_pct": None, "summary": ""}
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            expirations = list(t.options) if hasattr(t, "options") else []
            if not expirations:
                return result
            chain = t.option_chain(expirations[0])
            calls, puts = chain.calls, chain.puts
            if calls.empty or puts.empty:
                return result
            call_oi = dict(zip(calls["strike"], calls["openInterest"].fillna(0).astype(int)))
            put_oi  = dict(zip(puts["strike"],  puts["openInterest"].fillna(0).astype(int)))
            all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
            if not all_strikes:
                return result
            min_pain, mp_strike = float("inf"), stock_price
            for test_price in all_strikes:
                total_pain = (sum((test_price - s) * oi * 100 for s, oi in call_oi.items() if test_price > s)
                              + sum((s - test_price) * oi * 100 for s, oi in put_oi.items() if test_price < s))
                if total_pain < min_pain:
                    min_pain, mp_strike = total_pain, test_price
            dist = (stock_price / mp_strike - 1) * 100 if mp_strike > 0 else 0
            result = {
                "max_pain": mp_strike, "distance_pct": round(dist, 2),
                "summary": f"MaxPain:${mp_strike:.0f}({dist:+.1f}%)" if mp_strike else "",
            }
        except Exception as e:
            _log.debug("OracleBee max pain failed for %s: %s", ticker, e)
        return result

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

            # ---- Phase 2: 期权深度分析（term structure / 25d skew / max pain）----
            term_structure = self._analyze_term_structure(ticker, current_price)
            deep_skew      = self._analyze_deep_skew(ticker, current_price)
            max_pain       = self._calc_max_pain(ticker, current_price)
            _deep_adj = term_structure.get("term_score_adj", 0) + deep_skew.get("skew_score_adj", 0)
            options_score = max(0.0, min(10.0, options_score + _deep_adj))
            _deep_parts = [s for s in [
                term_structure.get("summary", ""), deep_skew.get("summary", ""),
                max_pain.get("summary", ""),
            ] if s]
            if _deep_parts:
                signal_summary = signal_summary + " | " + " | ".join(_deep_parts)

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
                if term_structure.get("structure") != "unknown":
                    _pub_details["term_structure"] = term_structure["structure"]
                    _pub_details["term_spread"]    = term_structure.get("spread", 0)
                if deep_skew.get("skew_25d") is not None:
                    _pub_details["skew_25d"] = deep_skew["skew_25d"]
                if max_pain.get("max_pain") is not None:
                    _pub_details["max_pain"]          = max_pain["max_pain"]
                    _pub_details["max_pain_dist_pct"] = max_pain.get("distance_pct", 0)
                # 期权大单/异动信号（合并 OptionsAgent + unusual_options 两源）
                _ua = list(result.get("unusual_activity", []))
                if unusual_flow.get("signals"):
                    for _uf_sig in unusual_flow["signals"][:10]:
                        _ua.append({
                            "type": _uf_sig.get("type", "unusual_flow"),
                            "strike": _uf_sig.get("strike"),
                            "volume": _uf_sig.get("volume") or _uf_sig.get("size"),
                            "premium": _uf_sig.get("premium"),
                            "bullish": _uf_sig.get("sentiment", "") == "bullish",
                            "source": "unusual_options",
                        })
                if _ua:
                    _pub_details["unusual_activity"] = _ua[:15]  # 最多保留15条
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
                details={**(result or {}), "term_structure": term_structure,
                         "deep_skew": deep_skew, "max_pain": max_pain},
                extras={
                    "polymarket_score": poly_score,
                    "polymarket_markets": poly_markets,
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("OracleBeeEcho failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("OracleBeeEcho", "odds", e)
