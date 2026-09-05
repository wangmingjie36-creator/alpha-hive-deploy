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


def _finite_pos(x) -> bool:
    """有限、正、非 bool 的数。`bool` 是 `int` 子类，True 会被当 1.0——仓库同类守卫一律显式排除。"""
    import math as _m
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and _m.isfinite(x) and x > 0)


class OracleBeeEcho(BeeAgent):
    """市场预期蜂 - 期权分析 + Polymarket 预测市场赔率
    对应维度：Odds (权重 0.15)
    融合：期权信号 60% + Polymarket 赔率 40%
    """

    # ---------- v0.45.128：期限结构 / 25Δ skew 改从 OptionsAgent 的 CBOE 结果派生 ----------
    #
    # 此前这两项各自拉 yfinance 期权链（v0.45.122 把三处收成一份 memo）。2026-09-04
    # 全量核对发现那条路「活着」但输出的是垃圾：近月 ATM IV 中位 15.6%、9/29 只 <8%
    # （NVDA 4.44%、COST 4.16%，CBOE 同日前端 IV 33.4% / 23.8%）——yfinance 把 0~2 DTE
    # 的到期日排最前、IV 字段在那儿常是残值，而 options_analyzer 的 CBOE 路径只选 DTE≥3。
    # skew 同样系统性偏高（中位 ~1.2 vs CBOE ~1.0），把 -0.3 / -0.6 的「恐慌对冲」扣分
    # 发给了大半个名单。
    #
    # 换源后**档位与分值一字不改**（只换 IV 来源）。09-04 实测 options_score
    # Δ 中位 +0.30、范围 [-0.5, +1.7]、21/30 非零——属评分输入变更，已追加世代边界
    # （ic_rerun_readiness._COHORT_HISTORY，v0.45.128）。
    #
    # CBOE 结果缺席（data_available=False / skew_ratio=None）→ unknown、adj=0，
    # **不再回退 yfinance**：回退到一个已证明输出垃圾的源，比诚实的 0 更糟。
    # 本蜂自此不再直接碰 yfinance（静态守卫 tests/test_prefetch_market_bundle.py）。

    @staticmethod
    def _term_structure_adj(options_result: dict) -> dict:
        """IV 期限结构（前端 vs 远端 ATM IV，单位 %）→ options_score 调整。
        Contango（远端高）= 市场平静 → 偏多；Backwardation（前端高）= 近期恐慌/催化剂 → 偏空。"""
        result = {"structure": "unknown", "spread": 0.0, "term_score_adj": 0.0,
                  "summary": "", "source": "none"}
        ts = (options_result or {}).get("iv_term_structure") or {}
        if not isinstance(ts, dict) or not ts.get("data_available"):
            return result
        front, back = ts.get("front_iv"), ts.get("back_iv")
        if not _finite_pos(front) or not _finite_pos(back):
            return result
        front, back = float(front), float(back)
        spread = back - front
        if spread > 2.0:
            structure, adj = "contango", +0.3
        elif spread > -2.0:
            structure, adj = "flat", 0.0
        elif spread > -5.0:
            structure, adj = "backwardation", -0.4
        else:
            structure, adj = "severe_backwardation", -0.8
        return {
            "structure": structure, "spread": round(spread, 2),
            "near_iv": round(front, 2), "far_iv": round(back, 2),
            "term_score_adj": adj,
            "summary": f"TermStr:{structure}({front:.0f}/{back:.0f})",
            "source": ts.get("source") or "options_agent",
        }

    @staticmethod
    def _skew_adj(options_result: dict) -> dict:
        """25Δ 近似 skew（OTM put IV / OTM call IV）→ options_score 调整。
        Skew > 1.3 → 机构恐慌对冲 → 偏空；Skew < 0.8 → Call 投机过热 → 偏多。"""
        result = {"skew_25d": None, "skew_score_adj": 0.0, "summary": "", "source": "none"}
        r = options_result or {}
        detail = r.get("iv_skew_detail") if isinstance(r.get("iv_skew_detail"), dict) else {}
        skew = detail.get("skew_ratio", r.get("iv_skew_ratio"))
        if not _finite_pos(skew):
            return result
        skew = float(skew)
        if skew > 1.3:
            adj = -0.6
        elif skew > 1.15:
            adj = -0.3
        elif skew < 0.7:
            adj = +0.4
        elif skew < 0.85:
            adj = +0.2
        else:
            adj = 0.0
        return {
            "skew_25d": round(skew, 3),
            "otm_put_iv": detail.get("otm_put_iv"), "otm_call_iv": detail.get("otm_call_iv"),
            "skew_score_adj": adj,
            "summary": f"Skew25d:{skew:.2f}" + ("(恐慌对冲)" if skew > 1.3 else ""),
            "source": "options_agent",
        }

    @staticmethod
    def _max_pain_from_oi(call_oi: dict, put_oi: dict, stock_price: float):
        """纯计算：给定 {strike: oi} 求 Max Pain。

        v0.41.2 退化保护：① 全零/近零 OI（深夜限流常态）时每个行权价
        痛苦值恒为 0，旧实现会取到链内最低行权价（NVDA 曾算出 $50，
        现价 $203）——总 OI < 500 直接返回 None；② 结果偏离现价 >50%
        视为数据垃圾同样返回 None（近端磁吸不可能离谱到这种程度）。
        """
        all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
        if not all_strikes:
            return None
        total_oi = sum(call_oi.values()) + sum(put_oi.values())
        if total_oi < 500:
            return None  # OI 太薄（多为限流空链），结果无意义
        min_pain, mp_strike = float("inf"), None
        for test_price in all_strikes:
            total_pain = (sum((test_price - s) * oi * 100 for s, oi in call_oi.items() if test_price > s)
                          + sum((s - test_price) * oi * 100 for s, oi in put_oi.items() if test_price < s))
            if total_pain < min_pain:
                min_pain, mp_strike = total_pain, test_price
        if mp_strike is None or mp_strike <= 0:
            return None
        if stock_price > 0 and abs(mp_strike - stock_price) / stock_price > 0.50:
            return None  # 偏离现价 >50% = 数据垃圾
        return mp_strike

    def _calc_max_pain(self, ticker: str, stock_price: float) -> dict:
        """Max Pain 计算（期权到期时令所有持仓亏损最大的价位）。

        v0.41.2: 主源改 CBOE（fetch_cboe_chain，与期权链同源）；旧实现裸调 yfinance
        最近到期日，深夜限流全零 OI 时退化取最低行权价（v40.1 同款反模式的期权版漏网）。
        v0.45.128: 去掉 yfinance 兜底，CBOE 取不到即 None。
        """
        result = {"max_pain": None, "distance_pct": None, "summary": ""}
        call_oi, put_oi = {}, {}
        # ── 主源：CBOE（取返回链中最近到期日）──
        try:
            from cboe_options import fetch_cboe_chain
            cb = fetch_cboe_chain(ticker, stock_price)
            if cb and (cb.get("calls") or cb.get("puts")):
                _exps = sorted({c.get("expiry") for c in (cb.get("calls") or []) if c.get("expiry")})
                if _exps:
                    _near = _exps[0]
                    for c in cb.get("calls") or []:
                        if c.get("expiry") == _near:
                            call_oi[float(c.get("strike", 0))] = call_oi.get(float(c.get("strike", 0)), 0) + int(c.get("openInterest") or 0)
                    for p in cb.get("puts") or []:
                        if p.get("expiry") == _near:
                            put_oi[float(p.get("strike", 0))] = put_oi.get(float(p.get("strike", 0)), 0) + int(p.get("openInterest") or 0)
        except Exception as e:
            _log.debug("OracleBee max pain CBOE 主源失败 %s: %s", ticker, e)
        # v0.45.128：不再回退 yfinance——CBOE 取不到就诚实返回 None（见 _term_structure_adj 注释）。

        try:
            mp_strike = self._max_pain_from_oi(call_oi, put_oi, stock_price)
            if mp_strike is not None:
                dist = (stock_price / mp_strike - 1) * 100 if mp_strike > 0 else 0
                result = {
                    "max_pain": mp_strike, "distance_pct": round(dist, 2),
                    "summary": f"MaxPain:${mp_strike:.0f}({dist:+.1f}%)",
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
            result = {}  # 修复 Bug #9：前置初始化，防止 except 路径下 NameError
            try:
                from options_analyzer import OptionsAgent
                agent = OptionsAgent()
                result = agent.analyze(ticker, stock_price=current_price)
                options_score = _safe_score(result.get("options_score"), 5.0, 0, 10, "options_score")
                signal_summary = result.get("signal_summary", "平衡")
            except (ImportError, ConnectionError, ValueError, KeyError, TypeError,
                    OSError, AttributeError) as e:  # 修复 #9：扩展 except 元组，覆盖 yfinance 实际抛的 OSError/URLError 族
                _log.warning("OracleBeeEcho options unavailable for %s: %s", ticker, e)
                result = {}

            # ---- Polymarket 赔率（权重 35%）----
            # v0.45.30: 默认关闭（config.POLYMARKET_ENABLED=False）。本名单是大盘股，
            # Polymarket 无对应个股预测市场，实测从未成功返回过赔率，只白耗
            # 30 次 × 最多 3 重试 × 15s 超时。关闭后走下方 poly_markets==0 的
            # 权重重归一化分支，评分口径不变。
            # ⚠️ fallback 默认值必须与 config 同为 False（v0.45.23 教训）。
            poly_score = 5.0
            poly_signal = ""
            poly_markets = 0
            try:
                from config import POLYMARKET_ENABLED as _POLY_ON
            except (ImportError, AttributeError):
                _POLY_ON = False
            if _POLY_ON:
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
            # v0.45.128：期限结构 / skew 从上面 OptionsAgent 的 CBOE 结果派生，不再碰 yfinance
            term_structure = self._term_structure_adj(result)
            deep_skew      = self._skew_adj(result)
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
            # 修复 Bug #11：用具体词组而非子串 "多"/"空"（旧实现命中"多头空头很多"等混合词歧义）
            _ss = signal_summary or ""
            _bull_keywords = ("看多", "看涨", "多头", "走高", "上行")
            _bear_keywords = ("看空", "看跌", "空头", "下行", "走低")
            _bull_count = sum(1 for kw in _bull_keywords if kw in _ss)
            _bear_count = sum(1 for kw in _bear_keywords if kw in _ss)
            if unusual_flow.get("unusual_direction") in ("bullish", "bearish"):
                direction = unusual_flow["unusual_direction"]
            elif _bull_count > _bear_count:
                direction = "bullish"
            elif _bear_count > _bull_count:
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
