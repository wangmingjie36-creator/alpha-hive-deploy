"""ScoutBeeNova - 聪明钱侦察蜂 (Signal 维度, 权重 0.30)"""

from typing import Any, Dict, List, Optional
from swarm_agents._config import _log, _AS
from swarm_agents.cache import _safe_score
from swarm_agents.base import BeeAgent
from models import AgentResult
from swarm_agents.utils import (
    clamp_score_cfg, append_context, blend_with_llm,
    make_error_result, AGENT_ERRORS, LLM_ERRORS,
)


class ScoutBeeNova(BeeAgent):
    """聪明钱侦察蜂 - SEC Form4/13F 内幕交易 + 拥挤度分析
    对应维度：Signal (权重 0.30)

    数据源：
    - SEC EDGAR Form 4（内幕买卖记录，免费 API）
    - CrowdingDetector（拥挤度评估）
    - yfinance（动量/成交量）

    评分逻辑：
    - 内幕买入权重 60% + 拥挤度权重 40%
    - 高管主动买入 → 强烈看多信号
    - 大规模内幕卖出 → 看空信号
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)

            # ---- 1. SEC EDGAR 内幕交易数据 ----
            insider_data = None
            insider_score = 5.0
            try:
                from sec_edgar import get_insider_trades
                insider_data = get_insider_trades(ticker, days=90)
                insider_score = _safe_score(insider_data.get("sentiment_score"), 5.0, 0, 10, "insider_score")
            except LLM_ERRORS as e:
                _log.warning("ScoutBeeNova SEC data unavailable for %s: %s", ticker, e)
                pass  # insider_score 保持默认 5.0

            # ---- 1b. P2: EDGAR RSS 实时流（当日新鲜 Form 4，先于 REST API 反应）----
            rss_fresh_today = 0
            rss_summary_text = ""
            try:
                from edgar_rss import get_today_form4_alerts
                from sec_edgar import SECEdgarClient as _SEC
                _cik = str(_SEC()._cik_map.get(ticker.upper(), "")) or None
                rss_alerts = get_today_form4_alerts(ticker, cik=_cik)
                if rss_alerts.get("has_fresh_filings"):
                    fresh_n = rss_alerts["fresh_filings_count"]
                    rss_fresh_today = fresh_n
                    rss_summary_text = rss_alerts.get("summary", "")
                    # 当日新鲜申报信号：提升 insider_score
                    insider_score = min(10.0, insider_score + 0.5 * fresh_n)
            except (*LLM_ERRORS, OSError) as e:
                _log.debug("ScoutBeeNova RSS check skipped for %s: %s", ticker, e)

            # ---- 2. 拥挤度分析（真实数据源）----
            stock = self._get_stock_data(ticker)

            from crowding_detector import CrowdingDetector
            detector = CrowdingDetector(ticker)

            from real_data_sources import get_real_crowding_metrics
            metrics = get_real_crowding_metrics(ticker, stock, self.board)

            crowding_score, component_scores = detector.calculate_crowding_score(metrics)
            crowding_signal = max(1.0, 10.0 - crowding_score / 10.0)

            # ---- 3. 综合评分：内幕交易 + 拥挤度 ----
            _iw = _AS.get("scout_insider_weight", 0.6)
            _cw = _AS.get("scout_crowding_weight", 0.4)
            score = insider_score * _iw + crowding_signal * _cw
            score = clamp_score_cfg(score)

            # 方向判断
            _ch = _AS.get("crowding_high", 70)
            _cl = _AS.get("crowding_low", 30)
            _cn = _AS.get("crowding_sell_neutral", 50)
            if insider_data and insider_data.get("insider_sentiment") == "bullish":
                direction = "bullish"
            elif insider_data and insider_data.get("insider_sentiment") == "bearish":
                if crowding_score > _cn:
                    direction = "bearish"
                else:
                    direction = "neutral"  # 卖出但不拥挤，可能只是计划性减持
            elif crowding_score > _ch:
                direction = "bearish"
            elif crowding_score < _cl:
                direction = "bullish"
            else:
                direction = "neutral"

            category, _ = detector.get_crowding_category(crowding_score)
            adj_factor = detector.get_adjustment_factor(crowding_score)

            # 构建发现摘要
            parts = []
            if insider_data and insider_data.get("total_filings", 0) > 0:
                dollar_sold = insider_data.get("dollar_sold", 0)
                dollar_bought = insider_data.get("dollar_bought", 0)
                if dollar_bought > 0:
                    parts.append(f"内幕买入 ${dollar_bought:,.0f}")
                if dollar_sold > 0:
                    parts.append(f"内幕卖出 ${dollar_sold:,.0f}")
                # 标注重要交易
                notable = insider_data.get("notable_trades", [])
                if notable:
                    top = notable[0]
                    parts.append(f"{top.get('insider', '?')} {top.get('code_desc', '?')} {top.get('shares', 0):,.0f}股")
            else:
                parts.append("无近期内幕交易")

            parts.append(f"拥挤度 {crowding_score:.0f}/100（{category}）")
            parts.append(f"动量 {stock['momentum_5d']:+.1f}%")

            discovery = append_context(" | ".join(parts), ctx)

            # ── P1: LLM 内幕交易意图解读（规则引擎无法区分计划性卖出 vs 信心丧失）──
            llm_intent = None
            try:
                import llm_service
                if llm_service.is_available() and insider_data and insider_data.get("total_filings", 0) > 0:
                    llm_intent = llm_service.interpret_insider_trades(ticker, insider_data, stock)
                    if llm_intent:
                        score = blend_with_llm(score, llm_intent, "intent_score", 0.55, "ScoutBee_intent")
                        intent_label = llm_intent.get("intent_label", "")
                        intent_reason = llm_intent.get("intent_reasoning", "")
                        if intent_reason:
                            discovery = f"{discovery} | LLM意图:{intent_reason}"
                        # LLM 识别到计划性卖出时修正方向
                        if intent_label == "planned_exit" and direction == "bearish":
                            direction = "neutral"
                        elif intent_label == "accumulation" and direction != "bullish":
                            direction = "bullish"
            except LLM_ERRORS as e:
                _log.debug("ScoutBeeNova LLM unavailable for %s: %s", ticker, e)

            # ---- 2b. 国会议员交易（Quiver Quant 免费端点）----
            congress_data = {}
            try:
                import sys as _sys_ct
                import os as _os_ct
                _hive_dir_ct = _os_ct.path.dirname(_os_ct.path.dirname(__file__))
                if _hive_dir_ct not in _sys_ct.path:
                    _sys_ct.path.insert(0, _hive_dir_ct)
                from congress_trades_scraper import get_congress_trades_for_ticker
                congress_data = get_congress_trades_for_ticker(ticker, days_back=90)
                if congress_data.get("trades"):
                    c_score = congress_data.get("congress_score", 0)
                    net = congress_data.get("net_amount_est", 0)
                    net_label = f"净买入 ${net:,.0f}" if net > 0 else f"净卖出 ${abs(net):,.0f}"
                    parts.append(f"国会交易: {congress_data['buy_count']}买/{congress_data['sell_count']}卖 {net_label} (信号:{c_score}/10)")
                    # 强国会买入信号可微调综合分（权重 5%）
                    if c_score >= 7.0:
                        score = min(10.0, score * 1.05)
                    elif c_score >= 5.0 and net > 100_000:
                        score = min(10.0, score * 1.02)
            except Exception as _e_ct:
                _log.debug("Congress trades unavailable for %s: %s", ticker, _e_ct)

            # ---- 2c. 板块相对强弱（20日回报 vs 板块 ETF）----
            sector_rs = self._assess_sector_relative_strength(ticker)
            if sector_rs.get("rs_signal") not in ("unknown", None):
                rs_val  = sector_rs["relative_strength"]
                rs_sig  = sector_rs["rs_signal"]
                etf_lbl = sector_rs.get("sector_name") or sector_rs.get("sector_etf", "")
                rs_text = (
                    f"板块相对强度 {rs_val:+.1f}pp({etf_lbl} 20D)"
                    f"{'【跑赢】' if rs_sig=='outperform' else '【跑输】' if rs_sig=='underperform' else ''}"
                )
                # discovery 在 line 123 已冻结，直接拼接（与 LLM意图 块保持一致）
                discovery = f"{discovery} | {rs_text}"
                # 跑赢板块 → 上调综合分（权重 3%）
                if rs_sig == "outperform":
                    score = min(10.0, score * 1.03)
                elif rs_sig == "underperform":
                    score = max(0.0, score * 0.97)

            # ── 2d: 跨资产供应链信号（⑥）— TSM/AMAT/ASML/SOXX 相对强弱 ──────────────
            supply_chain_data: dict = {}
            try:
                from market_intelligence import get_supply_chain_signals
                supply_chain_data = get_supply_chain_signals(ticker)
                _sc_summary = supply_chain_data.get("supply_chain_note", "")
                if _sc_summary:
                    discovery = f"{discovery} | {_sc_summary}"
                _sc_signal = supply_chain_data.get("supply_chain_signal", "neutral")
                if _sc_signal == "positive":
                    score = min(10.0, score * 1.03)
                elif _sc_signal == "negative":
                    score = max(0.0, score * 0.97)
            except Exception as _e_sc:
                _log.debug("Supply chain signals unavailable for %s: %s", ticker, _e_sc)

            # S3: 结构化数据交换（BearBee 可直接读取，替代正则解析）
            _pub_details = {"crowding_score": crowding_score}
            if insider_data:
                _pub_details["insider_sold_usd"] = insider_data.get("dollar_sold", 0) or 0
                _pub_details["insider_bought_usd"] = insider_data.get("dollar_bought", 0) or 0
                _pub_details["insider_sentiment"] = insider_data.get("sentiment_score", 5.0)
            self._publish(ticker, discovery, "sec_edgar+crowding", score, direction, details=_pub_details)

            # Phase 2: confidence = 数据完整度（内幕数据可用 + 拥挤度可用 + LLM 加成）
            confidence = 0.5
            if insider_data and insider_data.get("total_filings", 0) > 0:
                confidence += 0.3
            dq = metrics.get("data_quality", {})
            real_fields = sum(1 for v in dq.values() if v == "real")
            confidence += min(0.1, real_fields * 0.02)
            if llm_intent:
                confidence += 0.1
            confidence = min(1.0, confidence)

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="ScoutBeeNova",
                dimension="signal",
                data_quality=metrics.get("data_quality", {}),
                details={
                    "insider": {
                        "sentiment": insider_data.get("insider_sentiment", "neutral") if insider_data else "unknown",
                        "score": insider_score,
                        "filings": insider_data.get("total_filings", 0) if insider_data else 0,
                        "dollar_bought": insider_data.get("dollar_bought", 0) if insider_data else 0,
                        "dollar_sold": insider_data.get("dollar_sold", 0) if insider_data else 0,
                        "notable_trades": (sorted(
                            insider_data.get("notable_trades", []),
                            key=lambda t: (t.get("total_value", 0) if t.get("total_value", 0) > 0
                                          else t.get("shares", 0)),
                            reverse=True
                        )[:5]) if insider_data else [],
                        "rss_fresh_today": rss_fresh_today,
                        "rss_summary": rss_summary_text,
                    },
                    "crowding_score": crowding_score,
                    "crowding_signal": round(crowding_signal, 2),
                    "components": component_scores,
                    "adjustment_factor": adj_factor,
                    "momentum_5d": stock["momentum_5d"],
                    "price": stock["price"],
                    "congress": {
                        "buy_count": congress_data.get("buy_count", 0),
                        "sell_count": congress_data.get("sell_count", 0),
                        "net_amount_est": congress_data.get("net_amount_est", 0),
                        "congress_score": congress_data.get("congress_score", 0),
                        "top_signal": congress_data.get("top_signal", ""),
                        "summary": congress_data.get("summary", ""),
                    },
                    "sector_relative_strength": sector_rs,
                    # ⑥ 跨资产供应链信号
                    "supply_chain": supply_chain_data,
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("ScoutBeeNova failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("ScoutBeeNova", "signal", e)

    # ---------- sector relative strength ----------

    def _assess_sector_relative_strength(self, ticker: str) -> Dict:
        """计算个股 20 日回报 vs 板块 ETF 20 日回报，返回相对强弱数据。

        Returns:
            {
              "stock_ret_20d": float,
              "sector_ret_20d": float,
              "relative_strength": float,   # stock - sector (pp)
              "sector_etf": str,
              "sector_name": str,
              "rs_signal": "outperform"|"underperform"|"neutral"|"unknown",
            }
        """
        result: Dict = {"rs_signal": "unknown"}
        try:
            import yfinance as yf

            # 1. 找板块 ETF
            sector_etf = ""
            sector_name = ""
            try:
                from config import WATCHLIST
                _sector_str = WATCHLIST.get(ticker, {}).get("sector", "")
                from fred_macro import _SECTOR_TO_ETF, _SECTOR_ETFS
                sector_etf  = _SECTOR_TO_ETF.get(_sector_str, "")
                sector_name = _SECTOR_ETFS.get(sector_etf, "")
            except (ImportError, KeyError):
                pass

            # 2. 若 WATCHLIST 未给出板块，用 yfinance info fallback
            if not sector_etf:
                try:
                    info = yf.Ticker(ticker).info
                    _yf_sector = info.get("sector", "")
                    from fred_macro import _SECTOR_TO_ETF, _SECTOR_ETFS
                    sector_etf  = _SECTOR_TO_ETF.get(_yf_sector, "")
                    sector_name = _SECTOR_ETFS.get(sector_etf, _yf_sector or "Unknown")
                except Exception:
                    pass

            if not sector_etf:
                result["rs_signal"] = "unknown"
                return result

            # 3. 拉取 20 日收盘价
            tickers_to_fetch = [ticker, sector_etf]
            hist = yf.download(
                tickers_to_fetch, period="25d", interval="1d",
                progress=False, auto_adjust=True
            )["Close"]

            if hist is None or len(hist) < 5:
                return result

            # 确保两列都存在
            if ticker not in hist.columns or sector_etf not in hist.columns:
                return result

            _stk  = hist[ticker].dropna()
            _etf  = hist[sector_etf].dropna()

            if len(_stk) < 5 or len(_etf) < 5:
                return result

            stock_ret  = round((_stk.iloc[-1] / _stk.iloc[0] - 1) * 100, 2)
            sector_ret = round((_etf.iloc[-1] / _etf.iloc[0] - 1) * 100, 2)
            rs = round(stock_ret - sector_ret, 2)

            if rs >= 5.0:
                rs_signal = "outperform"
            elif rs <= -5.0:
                rs_signal = "underperform"
            else:
                rs_signal = "neutral"

            result.update({
                "stock_ret_20d":  stock_ret,
                "sector_ret_20d": sector_ret,
                "relative_strength": rs,
                "sector_etf":    sector_etf,
                "sector_name":   sector_name,
                "rs_signal":     rs_signal,
            })

        except Exception as e:
            _log.debug("ScoutBeeNova sector RS unavailable for %s: %s", ticker, e)
        return result
