"""ChronosBeeHorizon - 催化剂追踪蜂 (Catalyst 维度, 权重 0.20)"""

from typing import Any, Dict, List, Optional
from swarm_agents._config import _log, _AS
from swarm_agents.cache import _safe_score
from swarm_agents.base import BeeAgent
from resilience import NETWORK_ERRORS
from models import AgentResult
from swarm_agents.utils import (
    clamp_score, append_context, blend_with_llm, build_confidence,
    make_error_result, AGENT_ERRORS, LLM_ERRORS,
)


class ChronosBeeHorizon(BeeAgent):
    """催化剂追踪蜂 - 财报、事件、时间线（yfinance 真实日历）
    对应维度：Catalyst (权重 0.20)
    """

    # 催化剂类型基础权重（1.0 = 原始近期事件加分 +1.0 的标准）
    CATALYST_TYPE_WEIGHTS: Dict[str, float] = {
        "earnings":        1.5,   # 财报（价格波动最大，±5~15%）
        "fda_approval":    1.4,   # FDA 批准/拒绝（二元事件，影响极大）
        "merger":          1.3,   # 并购
        "product_launch":  1.2,   # 重大产品发布
        "regulatory":      1.1,   # 监管决定
        "guidance":        1.0,   # 业绩指引更新
        "economic_event":  0.9,   # 宏观经济事件
        "investor_day":    0.7,   # 投资者日/分析师日
        "analyst_day":     0.7,   # 同上
        "conference":      0.5,   # 行业会议（信息量有限）
        "split":           0.8,   # 股票拆分
        "dividend":        0.4,   # 股息（稳定性高，但价格冲击小）
        "exDividendDate":  0.4,   # 除息日
        "dividendDate":    0.3,   # 分红到账日
    }
    _CATALYST_TYPE_DEFAULT = 0.7   # 未知类型默认权重

    # 催化剂严重程度乘数（与 CatalystSeverity 对应）
    CATALYST_SEVERITY_MULT: Dict[str, float] = {
        "critical": 1.3,
        "high":     1.1,
        "medium":   1.0,
        "low":      0.8,
    }

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)

            catalysts_found = []
            score = 5.0
            direction = "neutral"
            t = None  # yfinance Ticker，步骤 1b 分析师目标价复用

            # 1. 从 yfinance 获取真实财报日期
            try:
                import yfinance as yf
                t = yf.Ticker(ticker)
                cal = t.calendar
                if cal is not None:
                    # cal 可能是 DataFrame 或 dict
                    if hasattr(cal, 'to_dict'):
                        cal_dict = cal.to_dict()
                    elif isinstance(cal, dict):
                        cal_dict = cal
                    else:
                        cal_dict = {}

                    # 提取财报日期
                    earnings_date = cal_dict.get("Earnings Date", [])
                    if isinstance(earnings_date, list) and earnings_date:
                        from datetime import datetime
                        for ed in earnings_date:
                            if hasattr(ed, 'strftime'):
                                date_str = ed.strftime("%Y-%m-%d")
                            else:
                                date_str = str(ed)[:10]
                            days_until = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.now()).days
                            if days_until >= 0:
                                catalysts_found.append({
                                    "event": f"财报发布",
                                    "date": date_str,
                                    "days_until": days_until,
                                    "type": "earnings",
                                    "severity": "critical" if days_until <= 14 else "high",
                                })
                    elif isinstance(earnings_date, dict):
                        for key, val in earnings_date.items():
                            if hasattr(val, 'strftime'):
                                date_str = val.strftime("%Y-%m-%d")
                                from datetime import datetime
                                days_until = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.now()).days
                                if days_until >= 0:
                                    catalysts_found.append({
                                        "event": f"财报发布",
                                        "date": date_str,
                                        "days_until": days_until,
                                        "type": "earnings",
                                        "severity": "critical" if days_until <= 14 else "high",
                                    })

                    # 提取其他事件
                    for key in ["Ex-Dividend Date", "Dividend Date"]:
                        val = cal_dict.get(key)
                        if val:
                            if isinstance(val, dict):
                                for k, v in val.items():
                                    if hasattr(v, 'strftime'):
                                        catalysts_found.append({
                                            "event": key,
                                            "date": v.strftime("%Y-%m-%d"),
                                            "days_until": 0,
                                            "type": "dividend",
                                            "severity": "medium",
                                        })
                            elif hasattr(val, 'strftime'):
                                catalysts_found.append({
                                    "event": key,
                                    "date": val.strftime("%Y-%m-%d"),
                                    "days_until": 0,
                                    "type": "dividend",
                                    "severity": "medium",
                                })
            except (*NETWORK_ERRORS, AttributeError) as e:
                _log.warning("ChronosBeeHorizon yfinance calendar unavailable for %s: %s", ticker, e)

            # 1b. 分析师目标价（补强2：yfinance analyst_price_targets）
            _analyst_info: Dict = {}
            try:
                if t is not None:  # reuse yfinance Ticker from step 1
                    _apt = getattr(t, "analyst_price_targets", None)
                    if _apt is not None and hasattr(_apt, "get"):
                        _current = _apt.get("current", 0) or 0
                        _low = _apt.get("low", 0) or 0
                        _high = _apt.get("high", 0) or 0
                        _mean = _apt.get("mean", 0) or 0
                        _median = _apt.get("median", 0) or 0
                        if _mean > 0:
                            _analyst_info = {
                                "target_mean": round(_mean, 2),
                                "target_low": round(_low, 2),
                                "target_high": round(_high, 2),
                                "target_median": round(_median, 2),
                                "current_price": round(_current, 2),
                            }
                            # 计算 upside/downside 百分比
                            if _current > 0:
                                _analyst_info["upside_pct"] = round(
                                    (_mean / _current - 1) * 100, 1
                                )
            except (*NETWORK_ERRORS, AttributeError) as e:
                _log.debug("ChronosBeeHorizon analyst targets unavailable for %s: %s", ticker, e)

            # 1c. IV Crush 历史数据（财报期权定价核心参考）
            try:
                import sys as _sys
                import os as _os_iv
                _hive_dir = _os_iv.path.dirname(_os_iv.path.dirname(__file__))
                if _hive_dir not in _sys.path:
                    _sys.path.insert(0, _hive_dir)
                from iv_crush_scraper import get_iv_crush_data, format_iv_crush_summary
                _iv_data = get_iv_crush_data(ticker)
                ctx["iv_crush"] = _iv_data
                ctx["iv_crush_summary"] = format_iv_crush_summary(_iv_data)
                # 把 IV Crush 信息附加到财报催化剂（如已存在）
                if _iv_data.get("avg_abs_move") and catalysts_found:
                    for _c in catalysts_found:
                        if _c.get("type") == "earnings":
                            _c["iv_crush_avg_move"] = _iv_data["avg_abs_move"]
                            _c["implied_move"] = _iv_data.get("current_implied_move")
                            _c["beat_rate"] = _iv_data.get("historical_beat_rate")
                            break
            except Exception as _e_iv:
                _log.debug("IV Crush unavailable for %s: %s", ticker, _e_iv)
                ctx.setdefault("iv_crush", {})
                ctx.setdefault("iv_crush_summary", "")

            # 2. 加载外部 catalysts.json（S13：覆盖全部 WATCHLIST 标的）
            _catalysts_json_loaded = False
            try:
                import json as _json_cat
                import os as _os_cat
                _cat_path = _os_cat.path.join(_os_cat.path.dirname(_os_cat.path.dirname(__file__)), "catalysts.json")
                if _os_cat.path.isfile(_cat_path):
                    with open(_cat_path, "r", encoding="utf-8") as _cf:
                        _all_cats = _json_cat.load(_cf)
                    _catalysts_json_loaded = True
                    for entry in _all_cats.get(ticker, []):
                        cat_date = entry.get("date", "")
                        if cat_date:
                            from datetime import datetime as _dt_cat
                            try:
                                days_until = (_dt_cat.strptime(cat_date, "%Y-%m-%d") - _dt_cat.now()).days
                            except ValueError:
                                days_until = 999
                            if days_until >= 0:
                                catalysts_found.append({
                                    "event": entry.get("event", "Unknown"),
                                    "date": cat_date,
                                    "days_until": days_until,
                                    "type": entry.get("type", "economic_event"),
                                    "severity": entry.get("severity", "medium"),
                                })
            except (OSError, ValueError, KeyError) as e:
                _log.debug("catalysts.json unavailable for %s: %s", ticker, e)

            # 2b. 回退：CatalystTimeline 硬编码（向后兼容 NVDA/VKTX 详细催化剂）
            try:
                from catalyst_refinement import create_nvda_catalysts, create_vktx_catalysts
                if ticker == "NVDA":
                    timeline = create_nvda_catalysts()
                elif ticker == "VKTX":
                    timeline = create_vktx_catalysts()
                else:
                    timeline = None

                if timeline:
                    # 去重：如果 catalysts.json 已有同名事件就跳过
                    existing_events = {c["event"] for c in catalysts_found}
                    for cat in timeline.get_upcoming_catalysts(days_ahead=30):
                        if cat.event_name not in existing_events:
                            catalysts_found.append({
                                "event": cat.event_name,
                                "date": cat.scheduled_date or "TBD",
                                "days_until": cat.get_days_until_event(),
                                "type": cat.catalyst_type.value,
                                "severity": cat.severity.value,
                            })
            except (ImportError, ValueError, AttributeError) as e:
                _log.debug("CatalystTimeline unavailable for %s: %s", ticker, e)

            # 评分逻辑
            if catalysts_found:
                # 按天数排序
                catalysts_found.sort(key=lambda c: c.get("days_until", 999))

                # 基础分 + 按事件类型 × 严重程度的加权加分
                # 近期（7天内）× 1.0；中期（8~30天）× 0.3；超出 30 天不计分
                base = 5.5
                score = base
                imminent = []
                for c in catalysts_found:
                    days = c.get("days_until", 999)
                    event_type = c.get("type", "")
                    severity = c.get("severity", "medium")
                    type_w = self.CATALYST_TYPE_WEIGHTS.get(event_type, self._CATALYST_TYPE_DEFAULT)
                    sev_m = self.CATALYST_SEVERITY_MULT.get(severity, 1.0)
                    if days <= 7:
                        score += 1.0 * type_w * sev_m
                        imminent.append(c)
                    elif days <= 30:
                        score += 0.3 * type_w * sev_m
                score = clamp_score(score)

                nearest = catalysts_found[0]
                discovery = f"催化剂 {len(catalysts_found)} 个 | 最近：{nearest['event']}（{nearest.get('days_until', '?')}天后）"

                # 方向：需要有高影响力的近期催化剂（type_w >= 1.2）才 bullish
                # 普通事件（会议/股息）不足以推断看多方向
                high_impact_imminent = [
                    c for c in imminent
                    if self.CATALYST_TYPE_WEIGHTS.get(c.get("type", ""), self._CATALYST_TYPE_DEFAULT) >= 1.2
                ]
                if score >= 7.5 and high_impact_imminent:
                    direction = "bullish"
                elif score <= 4.5:
                    direction = "bearish"
                else:
                    direction = "neutral"
            else:
                score = 4.0
                discovery = "无近期催化剂"
                direction = "neutral"

            # 分析师目标价注入 discovery
            if _analyst_info and _analyst_info.get("upside_pct") is not None:
                _up = _analyst_info["upside_pct"]
                _tgt = _analyst_info["target_mean"]
                discovery = f"{discovery} | 分析师目标${_tgt}({'↑' if _up > 0 else '↓'}{abs(_up):.1f}%)"

            discovery = append_context(discovery, ctx)

            # ── P1: LLM 催化剂影响力解读（规则引擎不知道财报方向是利多还是利空）──
            llm_catalyst = None
            try:
                import llm_service
                stock_for_llm = self._get_stock_data(ticker)
                if llm_service.is_available() and catalysts_found:
                    llm_catalyst = llm_service.interpret_catalyst_impact(
                        ticker, catalysts_found, stock_for_llm
                    )
                    if llm_catalyst:
                        score = blend_with_llm(score, llm_catalyst, "impact_score", 0.35, "ChronosBee_impact")
                        llm_dir = llm_catalyst.get("impact_direction", direction)
                        if llm_dir in ("bullish", "bearish", "neutral"):
                            direction = llm_dir
                        impact_reason = llm_catalyst.get("impact_reasoning", "")
                        key_cat = llm_catalyst.get("key_catalyst", "")
                        if impact_reason:
                            discovery = f"{discovery} | LLM催化剂:{impact_reason}"
                        if key_cat:
                            discovery = f"{discovery}[关注:{key_cat}]"
            except LLM_ERRORS as e:
                _log.debug("ChronosBeeHorizon LLM unavailable for %s: %s", ticker, e)

            self._publish(ticker, discovery, "catalyst_timeline", score, direction,
                         details={
                             "catalyst_count": len(catalysts_found),
                             "nearest_days": catalysts_found[0].get("days_until") if catalysts_found else None,
                             "catalyst_types": list({c.get("type", "") for c in catalysts_found}),
                             "analyst_upside_pct": _analyst_info.get("upside_pct") if _analyst_info else None,
                             "analyst_mean_target": _analyst_info.get("target_mean") if _analyst_info else None,
                         })

            # Phase 2: confidence = 催化剂数量和来源多样性 + LLM 加成 + 分析师数据
            _cat_boost = min(0.3, len(catalysts_found) * 0.08) if catalysts_found else 0
            _has_yf = any(c.get("type") == "earnings" for c in catalysts_found) if catalysts_found else False
            confidence = build_confidence(0.3, [
                (catalysts_found, _cat_boost),
                (_has_yf, 0.15),
                (bool(llm_catalyst), 0.2),
                (bool(_analyst_info), 0.1),
            ])

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="ChronosBeeHorizon",
                dimension="catalyst",
                data_quality={
                    "yfinance_calendar": "real" if catalysts_found else "empty",
                    "catalysts_json": "loaded" if _catalysts_json_loaded else "missing",
                    "analyst_targets": "real" if _analyst_info else "unavailable",
                    "llm_impact": "llm_enhanced" if llm_catalyst else "rule_only",
                },
                details={
                    "catalysts": catalysts_found[:5],
                    "analyst_targets": _analyst_info or {},
                    "iv_crush": ctx.get("iv_crush", {}),
                    "iv_crush_summary": ctx.get("iv_crush_summary", ""),
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("ChronosBeeHorizon failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("ChronosBeeHorizon", "catalyst", e)
