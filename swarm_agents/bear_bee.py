"""BearBeeContrarian - 看空对冲蜂 (contrarian 维度)"""

from typing import Any, Dict, List, Optional
from pheromone_board import PheromoneEntry
from swarm_agents._config import (
    _log, _AS,
    _RE_INSIDER_SELL, _RE_INSIDER_BUY,
    _RE_PC_RATIO, _RE_PC_SHORT, _RE_IV_RANK, _RE_SENTIMENT,
)
from swarm_agents.cache import _safe_score
from swarm_agents.base import BeeAgent
from resilience import NETWORK_ERRORS
from models import AgentResult
from swarm_agents.utils import (
    clamp_score, append_context, make_error_result,
    AGENT_ERRORS, LLM_ERRORS,
)


class BearBeeContrarian(BeeAgent):
    """看空对冲蜂 - 专门寻找看空信号，平衡蜂群的系统性看多偏差
    独立维度：contrarian（不参与 5 维评分，但影响方向投票）

    **二阶段执行**：在其他 6 个 Agent 完成后运行，从信息素板读取已有数据，
    避免重复 API 调用导致限流失败。

    分析维度：
    1. 内幕卖出强度（从 ScoutBeeNova 信息素板读取，回退 SEC 直查）
    2. 估值泡沫（P/E 过高、涨幅过大 — 使用预取 yfinance 数据）
    3. 期权看跌信号（从 OracleBeeEcho 信息素板读取，回退期权模块）
    4. 动量衰减（使用预取 yfinance 数据）
    5. 新闻看空信号（从 BuzzBeeWhisper 信息素板读取，回退 Finviz）
    """

    def _read_board_entry(self, ticker: str, agent_id_prefix: str) -> Optional[PheromoneEntry]:
        """从信息素板读取指定 Agent 对指定 ticker 的最新条目"""
        if not self.board:
            return None
        entries = self.board.get_top_signals(ticker=ticker, n=20)
        for e in entries:
            if e.agent_id.startswith(agent_id_prefix):
                return e
        return None

    # ---------- signal assessment helpers ----------

    def _assess_insider_selling(self, ticker: str,
                                bearish_signals: List[str],
                                data_sources: Dict[str, str],
                                ) -> tuple:
        """评估内幕卖出强度。返回 (insider_bear, insider_data)。"""
        insider_bear = 0.0
        insider_data = None

        scout_entry = self._read_board_entry(ticker, "ScoutBee")
        if scout_entry and scout_entry.discovery:
            disc = scout_entry.discovery
            data_sources["insider"] = "real"
            _sd = getattr(scout_entry, 'details', {}) or {}
            if "insider_sold_usd" in _sd:
                sold = int(_sd.get("insider_sold_usd", 0))
                bought = int(_sd.get("insider_bought_usd", 0))
            else:
                sell_match = _RE_INSIDER_SELL.search(disc)
                buy_match = _RE_INSIDER_BUY.search(disc)
                sold = int(sell_match.group(1).replace(',', '')) if sell_match else 0
                bought = int(buy_match.group(1).replace(',', '')) if buy_match else 0

            if sold > 0 or bought > 0:
                insider_data = {"dollar_sold": sold, "dollar_bought": bought}
                if sold > bought * 3 and sold > 1_000_000:
                    insider_bear = 8.0
                    bearish_signals.append(f"内幕大额抛售 ${sold:,.0f}（买入仅 ${bought:,.0f}）")
                elif sold > bought * 2 and sold > 500_000:
                    insider_bear = 6.5
                    bearish_signals.append(f"内幕卖多买少 卖${sold:,.0f}/买${bought:,.0f}")
                elif sold > bought and sold > 100_000:
                    insider_bear = 5.0
                    bearish_signals.append(f"内幕净卖出 ${sold:,.0f}")

            if scout_entry.direction == "bearish" and insider_bear < 6.0:
                insider_bear = max(insider_bear, 6.0)
                if not any("内幕" in s for s in bearish_signals):
                    bearish_signals.append(f"Scout 内幕信号看空（{scout_entry.self_score:.1f}分）")

        if not insider_data:
            try:
                from sec_edgar import get_insider_trades
                insider_data = get_insider_trades(ticker, days=90)
                if insider_data:
                    data_sources["insider"] = "sec_api"
                    sold = insider_data.get("dollar_sold", 0)
                    bought = insider_data.get("dollar_bought", 0)
                    sentiment = insider_data.get("insider_sentiment", "neutral")
                    if sentiment == "bearish":
                        insider_bear = 7.0
                        bearish_signals.append(f"内幕人净卖出 ${sold:,.0f}")
                    elif sold > bought * 3 and sold > 1_000_000:
                        insider_bear = 8.0
                        bearish_signals.append(f"内幕大额抛售 ${sold:,.0f}（买入仅 ${bought:,.0f}）")
                    elif sold > bought * 2:
                        insider_bear = 5.5
                        bearish_signals.append(f"内幕卖多买少 卖${sold:,.0f}/买${bought:,.0f}")
            except LLM_ERRORS as e:
                _log.warning("BearBeeContrarian SEC fallback failed for %s: %s", ticker, e)
                data_sources["insider"] = "unavailable"

        return insider_bear, insider_data

    def _assess_options_puts(self, ticker: str, price: float,
                             bearish_signals: List[str],
                             data_sources: Dict[str, str],
                             ) -> tuple:
        """评估期权看跌信号。返回 (options_bear, options_data)。"""
        options_bear = 0.0
        options_data = None

        oracle_entry = self._read_board_entry(ticker, "OracleBee")
        if oracle_entry and oracle_entry.discovery:
            disc = oracle_entry.discovery
            data_sources["options"] = "real"
            _od = getattr(oracle_entry, 'details', {}) or {}
            if "pc_ratio" in _od:
                pc_ratio = _od.get("pc_ratio")
                iv_rank = _od.get("iv_rank")
            else:
                pc_match = _RE_PC_RATIO.search(disc)
                if not pc_match:
                    pc_match = _RE_PC_SHORT.search(disc)
                iv_match = _RE_IV_RANK.search(disc)
                pc_ratio = float(pc_match.group(1)) if pc_match else None
                iv_rank = float(iv_match.group(1)) if iv_match else None

            if pc_ratio and pc_ratio > 1.5:
                options_bear = 8.0
                bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（强看跌信号）")
            elif pc_ratio and pc_ratio > 1.2:
                options_bear = 6.0
                bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（偏看跌）")
            elif pc_ratio and pc_ratio > 1.0:
                options_bear = 4.0
                bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（略偏空）")

            if iv_rank and iv_rank > 80:
                options_bear = max(options_bear, 7.0)
                bearish_signals.append(f"IV Rank {iv_rank:.0f}（恐慌高位）")
            elif iv_rank and iv_rank > 60:
                options_bear = max(options_bear, 5.0)
                bearish_signals.append(f"IV Rank {iv_rank:.0f}（波动偏高）")

            iv_skew = _od.get("iv_skew")
            if iv_skew is not None and iv_skew > 1.15:
                options_bear = max(options_bear, 6.5)
                bearish_signals.append(f"IV Skew {iv_skew:.2f}（看跌期权溢价偏高）")
            gex = _od.get("gex")
            if gex is not None and gex < 0:
                options_bear = max(options_bear, 5.0)
                bearish_signals.append(f"GEX 负值 {gex:,.0f}（做市商助跌）")

            if oracle_entry.direction == "bearish" and options_bear < 5.0:
                options_bear = max(options_bear, 5.5)
                if not any("P/C" in s for s in bearish_signals):
                    bearish_signals.append(f"Oracle 期权信号看空（{oracle_entry.self_score:.1f}分）")

            options_data = {"pc_ratio": pc_ratio, "iv_rank": iv_rank, "iv_skew": iv_skew, "gex": gex}

        if not options_data:
            try:
                from options_analyzer import OptionsAgent
                opt = OptionsAgent()
                result = opt.analyze(ticker, stock_price=price if price > 0 else None)
                if result:
                    data_sources["options"] = "options_api"
                    pc_ratio = result.get("put_call_ratio", 1.0)
                    iv_rank = result.get("iv_rank", 50)
                    if pc_ratio > 1.5:
                        options_bear = 8.0
                        bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（强看跌）")
                    elif pc_ratio > 1.2:
                        options_bear = 6.0
                        bearish_signals.append(f"P/C Ratio {pc_ratio:.2f}（偏看跌）")
                    if iv_rank > 80:
                        options_bear = max(options_bear, 7.0)
                        bearish_signals.append(f"IV Rank {iv_rank:.0f}（恐慌高位）")
            except (ImportError, ConnectionError, ValueError, KeyError, TypeError, AttributeError) as e:
                _log.warning("BearBeeContrarian options fallback failed for %s: %s", ticker, e)
                data_sources["options"] = "unavailable"

        return options_bear, options_data

    def _assess_news_sentiment(self, ticker: str,
                               bearish_signals: List[str],
                               data_sources: Dict[str, str],
                               ) -> tuple:
        """评估新闻看空信号。返回 (news_bear, buzz_entry)。"""
        news_bear = 0.0

        buzz_entry = self._read_board_entry(ticker, "BuzzBee")
        if buzz_entry and buzz_entry.discovery:
            disc = buzz_entry.discovery
            data_sources["news"] = "real"
            _bd = getattr(buzz_entry, 'details', {}) or {}
            sentiment_pct = _bd.get("sentiment_score") if "sentiment_score" in _bd else None
            if sentiment_pct is None:
                sent_match = _RE_SENTIMENT.search(disc)
                sentiment_pct = int(sent_match.group(1)) if sent_match else None
            if sentiment_pct is not None:
                if sentiment_pct < 30:
                    news_bear = 7.5
                    bearish_signals.append(f"市场情绪极度悲观 {sentiment_pct}%")
                elif sentiment_pct < 40:
                    news_bear = 6.0
                    bearish_signals.append(f"市场情绪偏空 {sentiment_pct}%")
                elif sentiment_pct < 45:
                    news_bear = 4.0
                    bearish_signals.append(f"市场情绪略偏谨慎 {sentiment_pct}%")

            reddit_mom = _bd.get("reddit_momentum")
            if reddit_mom is not None and reddit_mom < -2:
                news_bear = max(news_bear, 6.0)
                bearish_signals.append(f"Reddit 动量急跌 {reddit_mom:+.1f}（社区看空转向）")
            elif reddit_mom is not None and reddit_mom < -0.5:
                news_bear = max(news_bear, 4.5)
                bearish_signals.append(f"Reddit 动量走弱 {reddit_mom:+.1f}")

            if buzz_entry.direction == "bearish" and news_bear < 5.0:
                news_bear = max(news_bear, 5.5)
                bearish_signals.append(f"Buzz 情绪分析看空（{buzz_entry.self_score:.1f}分）")

        if news_bear == 0.0:
            try:
                from finviz_sentiment import get_finviz_sentiment
                finviz = get_finviz_sentiment(ticker)
                if finviz and isinstance(finviz, dict):
                    data_sources["news"] = "finviz_api"
                    news_score = finviz.get("news_score", 5.0)
                    neg = len(finviz.get("top_bearish", []))
                    pos = len(finviz.get("top_bullish", []))
                    if news_score < 3.5:
                        news_bear = 7.0
                        bearish_signals.append(f"新闻情绪偏空（评分 {news_score:.1f}/10）")
                    elif news_score < 4.5:
                        news_bear = 5.0
                        bearish_signals.append(f"新闻略偏空（评分 {news_score:.1f}/10）")
                    if neg > pos * 2 and neg >= 3:
                        news_bear = max(news_bear, 6.5)
                        bearish_signals.append(f"负面新闻主导（{neg}空 vs {pos}多）")
            except LLM_ERRORS as e:
                _log.warning("BearBeeContrarian Finviz news fallback failed for %s: %s", ticker, e)
                if "news" not in data_sources:
                    data_sources["news"] = "unavailable"

        return news_bear, buzz_entry

    def _assess_valuation(self, ticker: str, stock: Dict, mom_5d: float,
                          price: float, bearish_signals: List[str],
                          data_sources: Dict[str, str]) -> float:
        """评估估值/涨幅过热。返回 overval_bear。"""
        overval_bear = 0.0

        pe = 0
        if price > 0:
            try:
                import yfinance as yf
                info = yf.Ticker(ticker).fast_info
                pe = getattr(info, 'pe_ratio', 0) or 0
            except (*NETWORK_ERRORS, AttributeError) as e:
                _log.debug("BearBeeContrarian PE ratio unavailable for %s: %s", ticker, e)
                pe = 0

        if mom_5d > 15:
            overval_bear = 8.0
            bearish_signals.append(f"5日暴涨 {mom_5d:+.1f}%（超买）")
        elif mom_5d > 8:
            overval_bear = 6.0
            bearish_signals.append(f"5日涨幅过大 {mom_5d:+.1f}%")
        elif mom_5d > 5:
            overval_bear = 4.0
            bearish_signals.append(f"5日涨幅 {mom_5d:+.1f}%（关注回调风险）")

        if pe and pe > 80:
            overval_bear = max(overval_bear, 7.0)
            bearish_signals.append(f"P/E 极高 {pe:.1f}（估值泡沫风险）")
        elif pe and pe > 50:
            overval_bear = max(overval_bear, 5.0)
            bearish_signals.append(f"P/E 偏高 {pe:.1f}")
        elif pe and pe > 35:
            overval_bear = max(overval_bear, 3.5)
            bearish_signals.append(f"P/E {pe:.1f}（高于市场中位数）")

        data_sources["valuation"] = "yfinance"
        return overval_bear

    def _assess_momentum_decay(self, stock: Dict, mom_5d: float,
                               bearish_signals: List[str],
                               data_sources: Dict[str, str]) -> float:
        """评估动量衰减/量能萎缩。返回 momentum_bear。"""
        momentum_bear = 0.0
        vol_ratio = stock.get("volume_ratio", 1.0)
        volatility = stock.get("volatility_20d", 0)

        if mom_5d < -5:
            momentum_bear = 7.5
            bearish_signals.append(f"5日下跌 {mom_5d:+.1f}%")
        elif mom_5d < -2:
            momentum_bear = 5.5
            bearish_signals.append(f"动量转弱 {mom_5d:+.1f}%")
        elif mom_5d < 0:
            momentum_bear = 3.0
            bearish_signals.append(f"近期小幅回调 {mom_5d:+.1f}%")

        if 0.01 < vol_ratio < 0.5:
            momentum_bear = max(momentum_bear, 5.0)
            bearish_signals.append(f"量能萎缩 {vol_ratio:.1f}x（参与度下降）")
        elif vol_ratio > 3.0 and mom_5d < 0:
            momentum_bear = max(momentum_bear, 7.0)
            bearish_signals.append(f"放量下跌 {vol_ratio:.1f}x | {mom_5d:+.1f}%")
        elif vol_ratio > 2.0 and mom_5d < 0:
            momentum_bear = max(momentum_bear, 5.5)
            bearish_signals.append(f"量增价跌 {vol_ratio:.1f}x | {mom_5d:+.1f}%")

        if volatility > 50:
            momentum_bear = max(momentum_bear, 5.5)
            bearish_signals.append(f"高波动率 {volatility:.0f}%（年化）")

        data_sources["momentum"] = "yfinance"
        return momentum_bear

    def _assess_catalyst_risk(self, ticker: str,
                              bearish_signals: List[str],
                              data_sources: Dict[str, str]) -> float:
        """评估催化剂风险。返回 chronos_bear。"""
        chronos_bear = 0.0
        chronos_entry = self._read_board_entry(ticker, "Chronos")
        if chronos_entry and chronos_entry.discovery:
            data_sources["catalyst"] = "real"
            _cd = getattr(chronos_entry, 'details', {}) or {}
            nearest = _cd.get("nearest_days")
            if nearest is not None and nearest <= 7:
                chronos_bear = 5.0
                bearish_signals.append(f"催化剂{nearest}天内到来，波动性风险↑")
            analyst_upside = _cd.get("analyst_upside_pct")
            if analyst_upside is not None and analyst_upside < -10:
                chronos_bear = max(chronos_bear, 6.0)
                bearish_signals.append(f"分析师目标价下方 {abs(analyst_upside):.0f}%")
        return chronos_bear

    def _assess_ml_prediction(self, ticker: str,
                              bearish_signals: List[str],
                              data_sources: Dict[str, str]) -> float:
        """评估 ML 预测看空信号。返回 ml_bear。"""
        ml_bear = 0.0
        rival_entry = self._read_board_entry(ticker, "RivalBee")
        if rival_entry and rival_entry.discovery:
            data_sources["ml"] = "real"
            _rd = getattr(rival_entry, 'details', {}) or {}
            ml_prob = _rd.get("ml_probability")
            if ml_prob is not None and ml_prob < 0.45:
                ml_bear = 6.0 + (0.45 - ml_prob) * 10
                ml_bear = min(8.5, ml_bear)
                bearish_signals.append(f"ML模型看空(概率{ml_prob:.0%})")
            exp_7d = _rd.get("expected_7d")
            if exp_7d is not None and exp_7d < -3:
                ml_bear = max(ml_bear, 5.5)
                bearish_signals.append(f"ML预测7日回报{exp_7d:+.1f}%")
        return ml_bear

    def _assess_signal_consistency(self, ticker: str,
                                   bearish_signals: List[str],
                                   data_sources: Dict[str, str]) -> float:
        """评估信号一致性风险。返回 guard_bear。"""
        guard_bear = 0.0
        guard_entry = self._read_board_entry(ticker, "GuardBee")
        if guard_entry and guard_entry.discovery:
            data_sources["guard"] = "real"
            _gd = getattr(guard_entry, 'details', {}) or {}
            consist = _gd.get("consistency")
            if consist is not None and consist < 0.4:
                guard_bear = 5.5
                bearish_signals.append(f"信号一致性极低({consist:.0%})，方向不确定")
            conflict = _gd.get("conflict_type", "")
            if conflict in ("major_conflict", "direction_conflict"):
                guard_bear = max(guard_bear, 6.0)
                bearish_signals.append(f"GuardBee检测到重大冲突: {conflict}")
        return guard_bear

    def _assess_short_interest(self, ticker: str,
                               bearish_signals: List[str],
                               data_sources: Dict[str, str]) -> tuple:
        """评估空头仓位（Short Interest % of Float + Days to Cover）。
        返回 (short_bear, si_data)。

        高空头仓位 + 负动量 = 机构做空确认
        高空头仓位 + 正动量 = 轧空风险（在 bearish_signals 中注明）
        """
        short_bear = 0.0
        si_data: Dict[str, Any] = {}
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info
            si_raw = info.get("shortPercentOfFloat")   # yfinance 返回 0-1 的小数
            dtc    = info.get("shortRatio")             # Days to Cover (float)

            if si_raw is not None:
                # 统一转为百分比
                si_pct = si_raw * 100.0 if si_raw <= 1.0 else float(si_raw)
                si_data["short_pct_float"] = round(si_pct, 1)
                si_data["days_to_cover"]   = round(dtc, 1) if dtc else None
                data_sources["short_interest"] = "yfinance"

                if si_pct >= 20:
                    short_bear = 7.5
                    bearish_signals.append(
                        f"空头仓位极高 {si_pct:.1f}%浮筹（机构重仓做空，做空压力大）")
                elif si_pct >= 15:
                    short_bear = 6.0
                    bearish_signals.append(
                        f"空头仓位偏高 {si_pct:.1f}%浮筹")
                elif si_pct >= 10:
                    short_bear = 4.0
                    bearish_signals.append(
                        f"空头仓位 {si_pct:.1f}%浮筹（有一定做空压力）")

                if dtc and dtc > 10:
                    short_bear = max(short_bear, 6.0)
                    bearish_signals.append(f"回补天数 {dtc:.1f}天（做空拥挤）")
                elif dtc and dtc > 5:
                    short_bear = max(short_bear, 4.5)
                    bearish_signals.append(f"回补天数 {dtc:.1f}天")

        except (*NETWORK_ERRORS, AttributeError, KeyError, TypeError, ValueError) as e:
            _log.debug("BearBeeContrarian short interest unavailable for %s: %s", ticker, e)
            data_sources["short_interest"] = "unavailable"
        return short_bear, si_data

    # ---------- scoring + LLM helpers ----------

    def _compute_bear_score(self, dim_scores: Dict[str, float],
                            bearish_signals: List[str],
                            price: float, mom_5d: float) -> float:
        """综合看空评分。返回 rule_bear_score。"""
        try:
            from config import BEAR_SCORING_CONFIG as _BSC
        except ImportError:
            _BSC = {}

        _weights = {"insider": 0.25, "valuation": 0.20, "options": 0.25,
                    "momentum": 0.15, "news": 0.15, "chronos": 0.10,
                    "ml": 0.08, "guard": 0.07, "short_int": 0.18}
        _active_dims = [(k, v, _weights[k]) for k, v in dim_scores.items()
                        if v > 0 and k in _weights]

        if _active_dims:
            _act_num = sum(s * w for _, s, w in _active_dims)
            _act_den = sum(w for _, _, w in _active_dims)
            _wavg = _act_num / _act_den
            _max_sig = max(s for _, s, _ in _active_dims)
            _boost_w = _BSC.get("max_signal_boost_weight", 0.3)
            rule_bear_score = _wavg * (1.0 - _boost_w) + _max_sig * _boost_w
            _breadth = min(len(_active_dims) - 1, 3) * _BSC.get("breadth_bonus_per_dim", 0.3)
            rule_bear_score = clamp_score(rule_bear_score + _breadth)
        else:
            rule_bear_score = 5.0

        if not bearish_signals:
            if price > 0 and mom_5d >= 0:
                bearish_signals.append(f"当前价 ${price:.2f} | 暂无明显看空信号，但建议设置止损")
                rule_bear_score = _BSC.get("no_signal_positive_mom", 3.0)
            else:
                rule_bear_score = _BSC.get("no_signal_negative_mom", 2.0)

        return rule_bear_score

    def _generate_llm_bear_thesis(self, ticker: str, rule_bear_score: float,
                                  bearish_signals: List[str],
                                  insider_data: Any, options_data: Any,
                                  buzz_entry: Any,
                                  data_sources: Dict[str, str]) -> Dict:
        """LLM 看空论点生成。返回 dict(final_bear_score, llm_thesis, ...)。"""
        result = {
            "final_bear_score": rule_bear_score,
            "llm_thesis": "",
            "llm_key_risks": [],
            "llm_contrarian_insight": "",
            "llm_thesis_break": "",
        }
        try:
            import llm_service
            if llm_service.is_available():
                bull_signals = []
                if self.board:
                    for e in self.board.get_top_signals(ticker=ticker, n=20):
                        if e.direction == "bullish" and not e.agent_id.startswith("BearBee"):
                            bull_signals.append({
                                "agent": e.agent_id,
                                "score": e.self_score,
                                "discovery": e.discovery[:120],
                            })

                llm_bear = llm_service.generate_bear_thesis(
                    ticker=ticker,
                    bull_signals=bull_signals,
                    bear_signals=bearish_signals,
                    insider_data=insider_data,
                    options_data=options_data,
                    news_data={"buzz_entry": buzz_entry.discovery[:200] if buzz_entry else None},
                )
                if llm_bear:
                    llm_bear_score = llm_bear.get("bear_score")
                    if llm_bear_score is not None and isinstance(llm_bear_score, (int, float)):
                        result["final_bear_score"] = round(
                            rule_bear_score * 0.55 + float(llm_bear_score) * 0.45, 2)
                        result["final_bear_score"] = clamp_score(
                            result["final_bear_score"], lo=0.0)
                    result["llm_thesis"] = llm_bear.get("thesis", "")
                    result["llm_key_risks"] = llm_bear.get("key_risks", [])
                    result["llm_contrarian_insight"] = llm_bear.get("contrarian_insight", "")
                    result["llm_thesis_break"] = llm_bear.get("thesis_break", "")
                    data_sources["llm_bear"] = "llm_enhanced"
                    if result["llm_thesis"]:
                        bearish_signals.append(f"LLM看空论点: {result['llm_thesis'][:80]}")
        except LLM_ERRORS as e:
            _log.debug("BearBeeContrarian LLM unavailable for %s: %s", ticker, e)
        return result

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)
            stock = self._get_stock_data(ticker)
            bearish_signals = []
            data_sources = {}  # 跟踪数据来源

            # ===== 1. 内幕卖出强度 =====
            insider_bear, insider_data = self._assess_insider_selling(
                ticker, bearish_signals, data_sources)

            # ===== 2. 估值/涨幅过热 =====
            mom_5d = stock.get("momentum_5d", 0)
            price = stock.get("price", 0) or stock.get("current_price", 0)
            overval_bear = self._assess_valuation(
                ticker, stock, mom_5d, price, bearish_signals, data_sources)

            # ===== 3. 期权看跌信号 =====
            options_bear, options_data = self._assess_options_puts(
                ticker, price, bearish_signals, data_sources)

            # ===== 4. 动量衰减 =====
            momentum_bear = self._assess_momentum_decay(
                stock, mom_5d, bearish_signals, data_sources)

            # ===== 5. 新闻看空信号 =====
            news_bear, buzz_entry = self._assess_news_sentiment(
                ticker, bearish_signals, data_sources)

            # ===== 6. 催化剂风险 =====
            chronos_bear = self._assess_catalyst_risk(
                ticker, bearish_signals, data_sources)

            # ===== 7. ML 预测看空 =====
            ml_bear = self._assess_ml_prediction(
                ticker, bearish_signals, data_sources)

            # ===== 8. 信号一致性风险 =====
            guard_bear = self._assess_signal_consistency(
                ticker, bearish_signals, data_sources)

            # ===== 9. 空头仓位分析 =====
            short_bear, si_data = self._assess_short_interest(
                ticker, bearish_signals, data_sources)

            # ===== 综合看空评分 =====
            dim_scores = {
                "insider": insider_bear, "valuation": overval_bear,
                "options": options_bear, "momentum": momentum_bear,
                "news": news_bear, "chronos": chronos_bear,
                "ml": ml_bear, "guard": guard_bear, "short_int": short_bear,
            }
            rule_bear_score = self._compute_bear_score(
                dim_scores, bearish_signals, price, mom_5d)

            # ===== LLM 看空论点生成 =====
            llm_out = self._generate_llm_bear_thesis(
                ticker, rule_bear_score, bearish_signals,
                insider_data, options_data, buzz_entry, data_sources)
            final_bear_score = llm_out["final_bear_score"]
            llm_thesis = llm_out["llm_thesis"]
            llm_key_risks = llm_out["llm_key_risks"]
            llm_contrarian_insight = llm_out["llm_contrarian_insight"]
            llm_thesis_break = llm_out["llm_thesis_break"]

            # 反转为看空分：bear_score 越高 → 越看空 → 给蜂群一个低分
            # score 代表"该标的的吸引力"：看空信号强 = 低分
            score = clamp_score(10.0 - final_bear_score)

            try:
                from config import BEAR_SCORING_CONFIG as _BSC
            except ImportError:
                _BSC = {}
            _bear_thresh = _BSC.get("direction_bearish_min", 5.5)
            _neut_thresh = _BSC.get("direction_neutral_min", 3.5)
            if final_bear_score >= _bear_thresh:
                direction = "bearish"
            elif final_bear_score >= _neut_thresh:
                direction = "neutral"
            else:
                direction = "bullish"  # 找不到看空理由 = 确认看多

            if bearish_signals:
                discovery = " | ".join(bearish_signals[:6])
            else:
                discovery = "未发现显著看空信号"

            discovery = append_context(discovery, ctx)

            self._publish(ticker, discovery, "bear_contrarian", round(score, 2), direction,
                         details={
                             "bear_score": round(final_bear_score, 2),
                             "signal_count": len(bearish_signals),
                             "top_risk": bearish_signals[0][:60] if bearish_signals else "",
                         })

            confidence = min(1.0, 0.3 + len(bearish_signals) * 0.1)
            # M5: 信息素板数据可用时增加置信度
            # BearBee 的数据源全部经由信息素板中转，成功读到时标记为 "real"
            # 统计有多少个数据维度成功从信息素板获取了真实数据
            board_sources = sum(1 for v in data_sources.values() if v == "real")
            confidence = min(1.0, confidence + board_sources * 0.1)
            # LLM 可用时额外增加置信度
            if llm_thesis:
                confidence = min(1.0, confidence + 0.1)

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="BearBeeContrarian",
                dimension="contrarian",
                data_quality=data_sources,
                details={
                    "bear_score": round(final_bear_score, 2),
                    "rule_bear_score": round(rule_bear_score, 2),
                    "bearish_signals": bearish_signals,
                    "insider_bear": round(insider_bear, 1),
                    "overval_bear": round(overval_bear, 1),
                    "options_bear": round(options_bear, 1),
                    "momentum_bear": round(momentum_bear, 1),
                    "news_bear": round(news_bear, 1),
                    "short_int_bear": round(short_bear, 1),
                    "short_interest": si_data,
                    "data_sources": data_sources,
                },
                extras={
                    "llm_thesis": llm_thesis,
                    "llm_key_risks": llm_key_risks,
                    "llm_contrarian_insight": llm_contrarian_insight,
                    "llm_thesis_break": llm_thesis_break,
                },
            ).to_dict()

        except AGENT_ERRORS as e:
            _log.error("BearBeeContrarian failed for %s: %s", ticker, e, exc_info=True)
            return make_error_result("BearBeeContrarian", "contrarian", e)
