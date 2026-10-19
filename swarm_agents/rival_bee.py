"""RivalBeeVanguard - 竞争分析与预期收益蜂（ml_auxiliary 维度；v0.45.538 起无 ML 模型、不进分）"""

from typing import Dict
from swarm_agents._config import _log, _AS
from swarm_agents.base import BeeAgent
from models import AgentResult
from swarm_agents.utils import (
    clamp_score, append_context, make_error_result, AGENT_ERRORS,
)



class RivalBeeVanguard(BeeAgent):
    """竞争分析蜂 - 预期收益（动量 × 催化剂）+ 技术指标 + 分析师 EPS 修正

    不参与 5 维公式，v0.45.538 起也不再作为 final_score 的调整项（勿接回）；
    它的方向参与投票，分数经 GuardBee 的普查均分间接起作用。
    """

    def analyze(self, ticker: str) -> Dict:
        _err = self._validate_ticker(ticker)
        if _err:
            return _err
        try:
            ctx = self._get_history_context(ticker)

            # 预期收益（动量 × 催化剂档）—— v0.45.538 起**不再有 ML 模型**。
            #
            # 此前这里每只标的都 new 一个 `ml_predictor_extended.MLPredictionService`，在 30 条
            # 2023 年硬编码样本上重训 HGB、写生产 `ml_model.json`，拿它的概率当 Rival 的分数
            # （经 `QueenDistiller` 的 ml_adjustment 进 final_score）。那个概率被小样本校准压在
            # 0.54–0.58，是个只向上的近似常数。整条已退役，**勿接回**：要让 ML 进分，先预注册前瞻检验。
            # 方向本来就只取 `expected_returns` 的符号（与模型无关），所以本段只保留它需要的两个输入：
            # 5 日动量与 ChronosBee 的催化剂档。守卫：tests/test_rival_ml_adjustment_retired.py
            expectation = {}
            tech = {"tech_score_adj": 0.0, "tech_direction": "neutral", "summary": ""}
            try:
                import types as _types
                from ml_predictor import expected_returns, catalyst_quality_from_score

                stock = self._get_stock_data(ticker)
                # P0-2: momentum_5d 可为 None（真实 5 日动量不可得）→ expected_returns 内按 0（无观点）处理
                _mom_ml = stock.get("momentum_5d")

                # v0.44.3：从信息素板读**真实**的催化剂等级。
                #
                # 前提是本蜂已从 Phase-1 移到 **Phase-1.5**（见
                # `alpha_hive_daily_report` 的分阶段调用）——Phase-1 内部并行，
                # 同批蜂之间读不到彼此，排在后面才读得到。
                #
                # v0.45.151：读不到时给 **None**，不给 "B"。
                #
                # "B" 是生产**众数**（461/803 = 57.4%，五档里最常见的一档），
                # 拿它当缺失哨兵 ⇒「读不到」与「质量正好是 B」完全同形。
                # 当前世代 188 份生产 JSON 反解实测：真值≠"B" 的 39 份里 27 份
                # 被记成了 "B"，流向**全部**是 C→B，即 magnitude 0.7 当成 0.9、
                # `expected_7d/30d` 幅度系统性高估 28.6%。
                #
                # None 在 `expected_returns` 里落到 `_CATALYST_MAGNITUDE_DEFAULT`。
                #
                # ⚠️ `catalyst_quality_from_score` 的契约**不变**（它对 None 仍返回
                # "B"，理由被推翻的证据见其 docstring）—— 改的是本调用方：分数不可用
                # 时**根本不调它**。
                _cat_quality = None
                _cat_source = "unreadable"
                _chronos = self._read_peer(ticker, "ChronosBeeHorizon")
                _cat_raw = getattr(_chronos, "self_score", None) if _chronos else None
                # 类型闸照抄本仓既有那一句（advanced_analyzer.py:1207 等 5 处）：
                # `bool` 是 `int` 子类，漏掉它会让 True 变成 1.0 分的"真实观测"。
                if (isinstance(_cat_raw, (int, float))
                        and not isinstance(_cat_raw, bool)
                        and _cat_raw == _cat_raw):
                    _cat_quality = catalyst_quality_from_score(_cat_raw)
                    _cat_source = "peer_read"
                elif _chronos is None:
                    _log.info("RivalBeeVanguard %s: 板上无 ChronosBee 条目，"
                              "catalyst_quality 记为缺失（不再伪装成 \"B\"）", ticker)
                else:
                    _log.warning("RivalBeeVanguard %s: ChronosBee 条目分数不可用"
                                 "（%r），catalyst_quality 记为缺失", ticker, _cat_raw)

                # `expected_returns` 只读这两个属性（getattr），不需要整份 TrainingData。
                _er = expected_returns(_types.SimpleNamespace(
                    catalyst_quality=_cat_quality, momentum_5d=_mom_ml))
                expectation = {k: round(_er[k], 2)
                               for k in ("expected_3d", "expected_7d", "expected_30d")}
            except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
                _log.warning("RivalBeeVanguard 预期收益不可用 %s: %s", ticker, e)

            if expectation:
                ret_7d = expectation["expected_7d"]
                ret_30d = expectation["expected_30d"]
                avg_ret = (ret_7d + ret_30d) / 2

                direction = "bullish" if avg_ret > 0 else ("bearish" if avg_ret < 0 else "neutral")

                discovery = f"预期 7d {ret_7d:+.2f}% | 30d {ret_30d:+.2f}%"

                # 技术指标辅助（权重减半，仅作微调）。基线 5.0 = 原先「概率 0.5」的分数；
                # v0.45.538 起本分**不进 final_score**，只经 Guard 的普查均分间接起作用。
                tech = self._calc_technical_indicators(ticker)
                score = clamp_score(5.0 + tech["tech_score_adj"] * 0.5)
                if tech["summary"]:
                    discovery = f"{discovery} | {tech['summary']}"
            else:
                # 预期收益不可用 → 技术指标增强的动量评分
                stock = self._get_stock_data(ticker)
                # P0-2: momentum 为 None 时按 0（中性）参与评分，方向只看技术指标
                mom = stock.get("momentum_5d")
                _mom_missing = mom is None
                if _mom_missing:
                    mom = 0.0
                tech = self._calc_technical_indicators(ticker)
                score = clamp_score(5.0 + mom * 0.3 + tech["tech_score_adj"])
                _rival_bear_m = _AS.get("rival_bearish_momentum", -1.5)
                _rival_bull_m = _AS.get("rival_bullish_momentum", 2.0)
                if tech["tech_direction"] == "bullish" and mom > -1.0:
                    direction = "bullish"
                elif tech["tech_direction"] == "bearish" and mom < 1.0:
                    direction = "bearish"
                elif not _mom_missing and mom > _rival_bull_m:
                    direction = "bullish"
                elif not _mom_missing and mom < _rival_bear_m:
                    direction = "bearish"
                else:
                    direction = "neutral"
                _mom_txt = f"动量 {mom:+.1f}%" if not _mom_missing else "动量 N/A"
                # v0.45.3: volatility_20d 可为 None，format(None, '.0f') 直接抛
                _v20 = stock.get("volatility_20d")
                _v20_txt = f"波动率 {_v20:.0f}%" if _v20 is not None else "波动率 N/A"
                discovery = f"{_mom_txt} | {tech['summary']} | {_v20_txt}"

            # ---- EPS Revision Momentum（分析师共识 proxy）----
            eps_rev = self._assess_eps_revision(ticker)
            eps_sig = eps_rev.get("revision_signal", "unknown")
            if eps_sig not in ("unknown",):
                rev_summary = eps_rev.get("revision_summary", "")
                if rev_summary:
                    discovery = f"{discovery} | {rev_summary}"
                # 正面修正微升分，负面修正微降分（权重 4%，避免喧宾夺主）
                #
                # v0.44.1 修正单向棘轮：旧实现里 positive 会把 neutral 翻成
                # bullish，而 negative **没有对应的 neutral → bearish 分支**。
                # 与 ChronosBee「三分支全非负增量、无减分通路」是同族错误。
                #
                # 此前影响被掩盖：ML 路径占约 99%（1094/1101 有 ml.probability），
                # 而旧的 expected_7d 结构性恒正 ⇒ direction 进入本块时几乎从不为
                # neutral，棘轮无从触发。v0.44.1 把 expected_returns 改为居中后，
                # 无动量/无信号时 direction **会**是 neutral（动量为 0 的样本在近
                # 12 个扫描日占 63.7%），这个棘轮就会变成主要偏斜源。
                # **必须与 expected_returns 的修复同时落地。**
                if eps_sig == "positive":
                    score = min(10.0, score * 1.04)
                    if direction == "neutral":
                        direction = "bullish"
                elif eps_sig == "negative":
                    score = max(0.0, score * 0.96)
                    if direction == "neutral":
                        direction = "bearish"

            discovery = append_context(discovery, ctx)

            self._publish(ticker, discovery, "ml_predictor", round(score, 2), direction,
                         details={
                             "expected_7d": expectation.get("expected_7d") if expectation else None,
                             "expected_30d": expectation.get("expected_30d") if expectation else None,
                             "momentum_5d": stock.get("momentum_5d") if stock else None,
                         })

            # confidence = 预期收益路径可用性（0.8 / 0.3）。v0.45.538 拿掉模型时**刻意不动**：
            # 它决定 Rival 方向票的票重，改它会改 rule_direction，要另登世代边界。
            confidence = 0.3 if not expectation else 0.8
            confidence = min(1.0, confidence)

            return AgentResult(
                score=round(score, 2),
                direction=direction,
                confidence=round(confidence, 2),
                discovery=discovery,
                source="RivalBeeVanguard",
                dimension="ml_auxiliary",
                data_quality={
                    # 键名与取值沿用（Queen 的 DQ 登记表认这两个值）；"real" 现在指
                    # 「预期收益由真实动量 + 催化剂算出」，不再指有模型。
                    "ml_prediction": "real" if expectation else "fallback_momentum",
                    # CLAUDE.md 硬检查项「这个失败，下游怎么知道？」——此前读不到
                    # 只有一行 _log.debug，生产日志级别之下等于没有观测点，
                    # 而回落值 "B" 又与真值同形 ⇒ 失败被改写成「没发生过」。
                    "catalyst_quality": _cat_source,
                },
                details={
                    **(expectation if expectation else {"momentum_5d": stock.get("momentum_5d")}),
                    "eps_revision": eps_rev,
                    "technical_indicators": tech,
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
            info = self._yf_info(ticker)          # v0.45.122：走预取包

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

    # ---------- technical indicators helper ----------

    def _calc_technical_indicators(self, ticker: str) -> dict:
        """
        计算 RSI-14 / MACD Histogram / Bollinger Band% 三个核心技术指标

        用处：
        1. 预期收益不可用时，替代简单动量做评分（比单纯 mom_5d 有区分度）
        2. 预期收益可用时，作为辅助信号微调评分（±0.5）

        零新依赖：只用 yfinance history + math

        返回:
            {
                "rsi_14": float,          # 0~100, >70 超买, <30 超卖
                "macd_histogram": float,  # 正=多头, 负=空头
                "macd_cross": str,        # "golden_cross" / "death_cross" / "none"
                "bb_pct": float,          # 0~1, >0.8 超买, <0.2 超卖
                "tech_score_adj": float,  # -0.5 ~ +0.5 的评分调整量
                "tech_direction": str,    # bullish / bearish / neutral
                "summary": str,           # 一行摘要
            }
        """
        result = {
            "rsi_14": 50.0, "macd_histogram": 0.0, "macd_cross": "none",
            "bb_pct": 0.5, "tech_score_adj": 0.0, "tech_direction": "neutral",
            "summary": "",
        }
        try:
            hist = self._yf_history(ticker, period="3mo")   # v0.45.122：走预取包
            if hist.empty or len(hist) < 26:
                return result

            close = hist["Close"]

            # ===== RSI-14 =====
            deltas = close.diff().dropna()
            gain = deltas.clip(lower=0)
            loss = -deltas.clip(upper=0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            if avg_loss.iloc[-1] and avg_loss.iloc[-1] > 0:
                rs = avg_gain.iloc[-1] / avg_loss.iloc[-1]
                rsi = 100 - 100 / (1 + rs)
            else:
                rsi = 100.0 if avg_gain.iloc[-1] > 0 else 50.0
            result["rsi_14"] = round(float(rsi), 1)

            # ===== MACD (12/26/9) =====
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            histogram = float(macd_line.iloc[-1] - signal_line.iloc[-1])
            result["macd_histogram"] = round(histogram, 4)

            # 金叉/死叉检测（最近 2 根 K 线）
            if len(macd_line) >= 2 and len(signal_line) >= 2:
                prev_above = macd_line.iloc[-2] > signal_line.iloc[-2]
                curr_above = macd_line.iloc[-1] > signal_line.iloc[-1]
                if curr_above and not prev_above:
                    result["macd_cross"] = "golden_cross"
                elif not curr_above and prev_above:
                    result["macd_cross"] = "death_cross"

            # ===== Bollinger Band % =====
            if len(close) >= 20:
                sma20 = close.rolling(20).mean()
                std20 = close.rolling(20).std()
                upper = sma20.iloc[-1] + 2 * std20.iloc[-1]
                lower = sma20.iloc[-1] - 2 * std20.iloc[-1]
                bb_range = upper - lower
                if bb_range > 0:
                    bb_pct = (float(close.iloc[-1]) - lower) / bb_range
                    result["bb_pct"] = round(max(0.0, min(1.0, bb_pct)), 3)

            # ===== 综合评分调整 =====
            bull_signals = 0
            bear_signals = 0

            # RSI
            if rsi < 30:
                bull_signals += 1  # 超卖反弹信号
            elif rsi > 70:
                bear_signals += 1  # 超买回调信号

            # MACD
            if histogram > 0:
                bull_signals += 1
            elif histogram < 0:
                bear_signals += 1
            if result["macd_cross"] == "golden_cross":
                bull_signals += 1
            elif result["macd_cross"] == "death_cross":
                bear_signals += 1

            # BB%
            if result["bb_pct"] < 0.2:
                bull_signals += 1  # 下轨附近，超卖
            elif result["bb_pct"] > 0.8:
                bear_signals += 1  # 上轨附近，超买

            # 调整量（最大 ±0.5，保守）
            net = bull_signals - bear_signals
            if net >= 2:
                result["tech_score_adj"] = +0.5
                result["tech_direction"] = "bullish"
            elif net == 1:
                result["tech_score_adj"] = +0.2
                result["tech_direction"] = "bullish"
            elif net <= -2:
                result["tech_score_adj"] = -0.5
                result["tech_direction"] = "bearish"
            elif net == -1:
                result["tech_score_adj"] = -0.2
                result["tech_direction"] = "bearish"

            # 摘要
            parts = [f"RSI:{rsi:.0f}"]
            if result["macd_cross"] != "none":
                parts.append(f"MACD:{'金叉' if result['macd_cross'] == 'golden_cross' else '死叉'}")
            else:
                parts.append(f"MACD:{'多' if histogram > 0 else '空'}")
            parts.append(f"BB:{result['bb_pct']:.0%}")
            result["summary"] = " | ".join(parts)

        except Exception as e:
            _log.debug("RivalBee tech indicators failed for %s: %s", ticker, e)

        return result
