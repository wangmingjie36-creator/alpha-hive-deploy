"""
report_formatters - 报告格式化与展示模块

从 AlphaHiveDailyReporter 提取的格式化方法。
每个函数接收 reporter 实例（原 self）作为第一个参数。
"""

from typing import Dict, List
from hive_logger import get_logger

_log = get_logger("report_formatters")

# 免责声明常量（与主模块保持一致）
DISCLAIMER_FULL = (
    "本报告为蜂群 AI 分析，不构成投资建议，不替代持牌投顾。"
    "预测存在误差，所有交易决策需自行判断和风控。"
)
DISCLAIMER_SHORT = "非投资建议，仅数据分析与情景推演。"


def format_score_adjustments(data: Dict) -> str:
    """
    NA2：将 distill() 返回的调整字段格式化为人类可读注释。
    返回空字符串表示无调整发生。

    示例输出：
      "⚠️ 反对蜂看空 8.5 → 封顶 9.25 | ⚠️ 数据质量 60% (×0.875) | 🤖 LLM蒸馏(0.8) 基础分8.3 | ❌ 维度覆盖64%"
    """
    parts = []

    # BearBee 封顶
    if data.get("bear_cap_applied"):
        bs = data.get("bear_strength", 0.0)
        rs = data.get("rule_score", data.get("final_score", 0.0))
        parts.append(f"⚠️ 反对蜂看空强度{bs:.1f} → 封顶{rs:.2f}")

    # GuardBee 风险折扣
    if data.get("guard_penalty_applied"):
        gp = data.get("guard_penalty", 0.0)
        parts.append(f"🛡️ 风控折扣-{gp:.2f}")

    # 数据质量折扣
    if data.get("dq_penalty_applied"):
        rp = data.get("data_real_pct", 0.0)
        qf = data.get("dq_quality_factor", 1.0)
        parts.append(f"⚠️ 数据质量{rp:.0f}%(×{qf:.3f})")

    # LLM 蒸馏
    if data.get("distill_mode") == "llm_enhanced":
        lc = data.get("llm_confidence", 0.0)
        rs = data.get("rule_score", data.get("final_score", 0.0))
        parts.append(f"🤖 LLM蒸馏(置信{lc:.1f}) 基础分{rs:.1f}")

    # 维度覆盖率不足
    cov = data.get("dimension_coverage_pct", 100.0)
    if cov < 80.0:
        dim_status = data.get("dimension_status", {})
        missing = (
            [dim for dim, st in dim_status.items() if st != "present"]
            if isinstance(dim_status, dict) else []
        )
        missing_str = "/".join(missing) if missing else ""
        parts.append(f"❌ 维度覆盖{cov:.0f}%({missing_str})")

    return " | ".join(parts)


# ---------- section builders (return List[str]) ----------

def _build_summary(sorted_results: list, total_count: int) -> List[str]:
    """版块 1：今日摘要（全部标的按综合分降序）"""
    md: List[str] = []
    resonances = sum(1 for _, r in sorted_results if r["resonance"]["resonance_detected"])
    md.append("## 1) 今日摘要")
    md.append("")
    md.append(f"- 扫描标的：{total_count} 个 | 共振信号：{resonances}/{total_count}")
    md.append("")
    # 所有标的按综合分降序展示，≥7.5 高优先级，6.0~7.4 观察名单，<6.0 不行动
    high, watch, low = [], [], []
    for ticker, data in sorted_results:
        score = data["final_score"]
        res = "共振✅" if data["resonance"]["resonance_detected"] else ""
        line = f"  - **{ticker}** {data['direction'].upper()} {score:.1f}/10 {res}"
        if score >= 7.5:
            high.append(line)
        elif score >= 6.0:
            watch.append(line)
        else:
            low.append(line)
    if high:
        md.append("- 🟢 **高优先级**（≥7.5）：")
        md.extend(high)
    if watch:
        md.append("- 🟡 **观察名单**（6.0~7.4）：")
        md.extend(watch)
    if low:
        md.append("- ⚫ **暂不行动**（<6.0）：")
        md.extend(low)
    md.append("")
    return md


def _build_smart_money(sorted_results: list) -> List[str]:
    """版块 2：今日聪明钱动向（ScoutBeeNova）"""
    md: List[str] = []
    md.append("## 2) 今日聪明钱动向")
    md.append("")
    for ticker, data in sorted_results:
        agent = data.get("agent_details", {}).get("ScoutBeeNova", {})
        discovery = agent.get("discovery", "")
        details = agent.get("details", {})
        insider = details.get("insider", {})
        md.append(f"### {ticker}")
        if discovery:
            md.append(f"- {discovery}")
        if insider:
            sentiment = insider.get("sentiment", "unknown")
            bought = insider.get("dollar_bought", 0)
            sold = insider.get("dollar_sold", 0)
            filings = insider.get("filings", 0)
            md.append(f"- 内幕交易情绪：**{sentiment}** | 申报数：{filings}")
            if bought > 0:
                md.append(f"- 内幕买入金额：${bought:,.0f}")
            if sold > 0:
                md.append(f"- 内幕卖出金额：${sold:,.0f}")
            notable = insider.get("notable_trades", [])
            for t in notable[:2]:
                if isinstance(t, dict):
                    md.append(f"  - {t.get('insider', '?')}：{t.get('code_desc', '?')} {t.get('shares', 0):,.0f} 股")
        crowding = details.get("crowding_score", "")
        if crowding:
            md.append(f"- 拥挤度：{crowding:.0f}/100")
        md.append("")
    return md


def _build_market_expectations(sorted_results: list) -> List[str]:
    """版块 3：市场隐含预期（OracleBeeEcho）"""
    md: List[str] = []
    md.append("## 3) 市场隐含预期")
    md.append("")
    for ticker, data in sorted_results:
        agent = data.get("agent_details", {}).get("OracleBeeEcho", {})
        discovery = agent.get("discovery", "")
        details = agent.get("details", {})
        md.append(f"### {ticker}")
        if discovery:
            md.append(f"- {discovery}")
        if isinstance(details, dict) and details:
            iv = details.get("iv_rank")
            pc = details.get("put_call_ratio")
            gamma = details.get("gamma_exposure")
            if iv is not None:
                md.append(f"- IV Rank：{iv}")
            if pc is not None:
                pc_val = pc if isinstance(pc, (int, float)) else pc
                md.append(f"- Put/Call Ratio：{pc_val}")
            if gamma is not None:
                md.append(f"- Gamma Exposure：{gamma}")
            unusual = details.get("unusual_activity", [])
            if unusual:
                md.append(f"- 异常活动：{len(unusual)} 个信号")
                for u in unusual[:3]:
                    if isinstance(u, dict):
                        utype = u.get("type", "unknown").replace("_", " ")
                        strike = u.get("strike", "")
                        vol = u.get("volume", 0)
                        bull = "看涨" if u.get("bullish") else "看跌"
                        md.append(f"  - {bull} {utype} ${strike} ({vol:,.0f}手)")
                    elif isinstance(u, str):
                        md.append(f"  - {u}")
        md.append("")
    return md


def _build_sentiment(sorted_results: list) -> List[str]:
    """版块 4：X 情绪汇总（BuzzBeeWhisper）"""
    md: List[str] = []
    md.append("## 4) X 情绪汇总")
    md.append("")
    for ticker, data in sorted_results:
        agent = data.get("agent_details", {}).get("BuzzBeeWhisper", {})
        discovery = agent.get("discovery", "")
        details = agent.get("details", {})
        md.append(f"### {ticker}")
        if discovery:
            md.append(f"- {discovery}")
        if isinstance(details, dict) and details:
            sent_pct = details.get("sentiment_pct")
            mom = details.get("momentum_5d")
            vol = details.get("volume_ratio")
            if sent_pct is not None:
                md.append(f"- 看多情绪：{sent_pct}%")
            if mom is not None:
                md.append(f"- 5 日动量：{mom:+.1f}%")
            if vol is not None:
                md.append(f"- 量比：{vol:.1f}x")
            reddit = details.get("reddit_mentions") or details.get("reddit_rank")
            if reddit:
                md.append(f"- Reddit 热度：{reddit}")
        md.append("")
    return md


def _build_catalysts(sorted_results: list) -> List[str]:
    """版块 5：财报/事件催化剂（ChronosBeeHorizon）"""
    md: List[str] = []
    md.append("## 5) 财报/事件催化剂")
    md.append("")
    for ticker, data in sorted_results:
        agent = data.get("agent_details", {}).get("ChronosBeeHorizon", {})
        discovery = agent.get("discovery", "")
        details = agent.get("details", {})
        md.append(f"### {ticker}")
        if discovery:
            md.append(f"- {discovery}")
        if isinstance(details, dict) and details:
            earnings = details.get("next_earnings") or details.get("earnings_date")
            if earnings:
                md.append(f"- 下次财报：{earnings}")
            events = details.get("upcoming_events") or details.get("catalysts", [])
            if isinstance(events, list):
                for ev in events[:3]:
                    if isinstance(ev, dict):
                        md.append(f"  - {ev.get('date', '?')}：{ev.get('event', ev.get('description', '?'))}")
                    elif isinstance(ev, str):
                        md.append(f"  - {ev}")
            past = details.get("recent_events", [])
            if isinstance(past, list):
                for ev in past[:2]:
                    if isinstance(ev, dict):
                        md.append(f"  - [已发生] {ev.get('description', ev)}")
        md.append("")
    return md


def _build_competitive(sorted_results: list) -> List[str]:
    """版块 6：竞争格局分析（RivalBeeVanguard）"""
    md: List[str] = []
    md.append("## 6) 竞争格局分析")
    md.append("")
    for ticker, data in sorted_results:
        agent = data.get("agent_details", {}).get("RivalBeeVanguard", {})
        discovery = agent.get("discovery", "")
        details = agent.get("details", {})
        md.append(f"### {ticker}")
        if discovery:
            md.append(f"- {discovery}")
        if isinstance(details, dict) and details:
            ml_pred = details.get("ml_prediction") or details.get("prediction")
            if isinstance(ml_pred, dict):
                md.append(f"- ML 预测方向：{ml_pred.get('direction', '?')}")
                md.append(f"- ML 置信度：{ml_pred.get('confidence', '?')}")
            peers = details.get("peer_comparison") or details.get("peers", [])
            if isinstance(peers, list) and peers:
                md.append(f"- 同业对标：{', '.join(str(p) for p in peers[:5])}")
        md.append("")
    return md


def _build_bear_contrarian(sorted_results: list) -> List[str]:
    """版块 6.5：看空对冲观点（BearBeeContrarian）"""
    md: List[str] = []
    md.append("## 6.5) 看空对冲观点")
    md.append("")
    md.append("> BearBeeContrarian 专门寻找看空信号，平衡蜂群系统性看多偏差")
    md.append("")
    for ticker, data in sorted_results:
        agent = data.get("agent_details", {}).get("BearBeeContrarian", {})
        discovery = agent.get("discovery", "")
        details = agent.get("details", {})
        bear_score = details.get("bear_score", 0)
        signals = details.get("bearish_signals", [])
        direction = agent.get("direction", "neutral")

        if direction == "bearish":
            severity = "**看空警告**"
        elif direction == "neutral":
            severity = "需关注风险点"
        elif signals:
            severity = "风险提示"
        else:
            severity = "暂无看空信号"

        md.append(f"### {ticker} ({severity} | 看空强度 {bear_score:.1f}/10)")
        if signals:
            for sig in signals:
                md.append(f"- {sig}")
        elif discovery:
            md.append(f"- {discovery}")
        else:
            md.append("- 未发现显著看空信号")
        # LLM 看空论点
        llm_thesis = agent.get("llm_thesis", "")
        llm_risks = agent.get("llm_key_risks", [])
        llm_ci = agent.get("llm_contrarian_insight", "")
        if llm_thesis:
            md.append(f"- 🤖 **AI看空论点**：{llm_thesis}")
        if llm_risks:
            for risk in llm_risks[:3]:
                md.append(f"  - ⚠️ {risk}")
        if llm_ci:
            md.append(f"- 💡 **反对洞察**：{llm_ci}")
        # 数据来源标注
        sources = details.get("data_sources", {})
        if sources:
            src_labels = {"pheromone_board": "蜂群共享", "sec_api": "SEC直查",
                          "options_api": "期权直查", "finviz_api": "Finviz",
                          "yfinance": "yfinance", "unavailable": "不可用",
                          "llm_enhanced": "LLM增强"}
            src_parts = [f"{k}={src_labels.get(v, v)}" for k, v in sources.items()]
            md.append(f"- *数据来源*：{' | '.join(src_parts)}")
        md.append("")
    return md


def _build_composite_judgment(sorted_results: list) -> List[str]:
    """版块 7：综合判断 & 信号强度（含 AI 叙事、历史类比、评分调整、交叉验证）"""
    md: List[str] = []
    # 主表
    md.append("## 7) 综合判断 & 信号强度")
    md.append("")
    md.append("| 标的 | 方向 | 综合分 | 共振 | 投票(多/空/中) | 数据% | 失效条件 |")
    md.append("|------|------|--------|------|---------------|-------|---------|")
    for ticker, data in sorted_results:
        res = "Y" if data["resonance"]["resonance_detected"] else "N"
        ab = data["agent_breakdown"]
        data_pct = data.get("data_real_pct", 0)
        guard = data.get("agent_details", {}).get("GuardBeeSentinel", {})
        guard_discovery = guard.get("discovery", "")
        thesis_break = "信号分散" if not guard_discovery else guard_discovery.split("|")[0].strip()[:30]
        md.append(
            f"| **{ticker}** | {data['direction'].upper()} | "
            f"{data['final_score']:.1f} | {res} | "
            f"{ab['bullish']}/{ab['bearish']}/{ab['neutral']} | "
            f"{data_pct:.0f}% | {thesis_break} |"
        )
    md.append("")

    # LLM 多空综合叙事
    synthesis_lines = []
    for ticker, data in sorted_results:
        bbs = data.get("bull_bear_synthesis", "")
        cv = data.get("contrarian_view", "")
        if bbs or cv:
            parts = [f"- **{ticker}**"]
            if bbs:
                parts.append(f"  - 多空综合：{bbs}")
            if cv:
                parts.append(f"  - 少数意见：{cv}")
            synthesis_lines.append("\n".join(parts))
    if synthesis_lines:
        md.append("### AI 多空综合叙事")
        md.append("")
        md.extend(synthesis_lines)
        md.append("")

    # 历史类比推理
    analogy_lines = []
    for ticker, data in sorted_results:
        ha = data.get("historical_analogy")
        if ha and ha.get("analogy_found"):
            analogy_lines.append(f"**{ticker}** — {ha.get('analogy_summary', '')}")
            outcome = ha.get("historical_outcome", {})
            t1 = outcome.get("t1", "N/A")
            t7 = outcome.get("t7", "N/A")
            t30 = outcome.get("t30", "N/A")
            analogy_lines.append(f"- 历史结果：T+1 {t1} | T+7 {t7} | T+30 {t30}")
            sim = ha.get("similarity_score", 0)
            analogy_lines.append(f"- 相似度：{sim:.0%} | 置信调整：{ha.get('score_adjustment_applied', 0):+.1f}分")
            diff = ha.get("key_differences", "")
            if diff:
                analogy_lines.append(f"- 关键差异：{diff}")
            warning = ha.get("warning", "")
            if warning:
                analogy_lines.append(f"- ⚠️ {warning}")
            analogy_lines.append("")
    if analogy_lines:
        md.append("### 📜 历史类比推理")
        md.append("")
        md.extend(analogy_lines)

    # 评分调整注释
    adj_lines = []
    for ticker, data in sorted_results:
        adj = format_score_adjustments(data)
        if adj:
            adj_lines.append(f"- **{ticker}**：{adj}")
    if adj_lines:
        md.append("### 评分调整说明")
        md.append("")
        md.extend(adj_lines)
        md.append("")

    # GuardBeeSentinel 详细交叉验证
    md.append("### 交叉验证详情")
    md.append("")
    for ticker, data in sorted_results:
        guard = data.get("agent_details", {}).get("GuardBeeSentinel", {})
        discovery = guard.get("discovery", "")
        if discovery:
            md.append(f"- **{ticker}**：{discovery}")
    md.append("")
    return md


def _build_concentration(concentration) -> List[str]:
    """版块 P4：投资组合集中度风险（可选）"""
    md: List[str] = []
    if not concentration or not concentration.get("sector_breakdown"):
        return md
    risk_level = concentration.get("concentration_risk", "low")
    risk_emoji = {"low": "✅", "medium": "⚠️", "high": "🚨"}.get(risk_level, "")
    md.append(f"## 📊 投资组合集中度分析 {risk_emoji}")
    md.append("")
    md.append(f"**集中度风险**：{risk_level.upper()} | **综合评分**：{concentration.get('risk_score', 0):.1f}/10")
    md.append("")
    md.append("**板块分布**：")
    for sector, info in concentration.get("sector_breakdown", {}).items():
        tickers_str = " / ".join(info.get("tickers", []))
        md.append(f"- {sector}：{info.get('pct', 0):.0f}%（{tickers_str}）")
    md.append("")
    corr_warns = concentration.get("correlation_warnings", [])
    if corr_warns:
        md.append("**高相关对（≥0.70）**：")
        for w in corr_warns[:4]:
            md.append(f"- {w['pair']}：相关系数 {w['correlation']:.2f} [{w['risk'].upper()}]")
        md.append("")
    md.append("**分散化建议**：")
    for rec in concentration.get("recommendations", []):
        md.append(f"- {rec}")
    md.append("")
    return md


def _build_cross_ticker(cross_ticker) -> List[str]:
    """版块 P4b：跨标的关联分析（可选）"""
    md: List[str] = []
    if not cross_ticker or not isinstance(cross_ticker, dict):
        return md
    md.append("## 🔗 跨标的关联分析")
    md.append("")
    sector_mom = cross_ticker.get("sector_momentum", {})
    if sector_mom:
        mom_parts = []
        for sec, trend in sector_mom.items():
            emoji = {"leading": "🟢", "lagging": "🔴", "neutral": "🟡"}.get(trend, "")
            mom_parts.append(f"{sec}: {trend} {emoji}")
        md.append("**板块动量**：" + " | ".join(mom_parts))
        md.append("")
    insights = cross_ticker.get("cross_ticker_insights", [])
    if insights:
        md.append("**关联洞察**：")
        for ins in insights[:5]:
            tks = " & ".join(ins.get("tickers", []))
            md.append(f"- [{ins.get('type', '')}] {tks}：{ins.get('insight', '')}")
        md.append("")
    corr_warnings = cross_ticker.get("correlation_warnings", [])
    if corr_warnings:
        md.append("**关联风险⚠️**：")
        for w in corr_warnings[:4]:
            md.append(f"- {w}")
        md.append("")
    rotation = cross_ticker.get("sector_rotation_signal", "")
    if rotation:
        md.append(f"**轮动信号**：{rotation}")
        md.append("")
    hints = cross_ticker.get("portfolio_adjustment_hints", [])
    if hints:
        md.append("**组合调整建议**：")
        for h in hints[:3]:
            md.append(f"- {h}")
        md.append("")
    return md


def _build_macro(macro_context) -> List[str]:
    """版块 P5：宏观环境（可选）"""
    md: List[str] = []
    if not macro_context or macro_context.get("data_source") == "fallback":
        return md
    regime = macro_context.get("macro_regime", "neutral")
    regime_emoji = {"risk_on": "🟢", "risk_off": "🔴", "neutral": "🟡"}.get(regime, "")
    md.append(f"## 🌐 宏观环境 {regime_emoji}")
    md.append("")
    md.append(f"**宏观政体**：{regime.upper()} | **评分**：{macro_context.get('macro_score', 5):.1f}/10")
    md.append("")
    md.append("| 指标 | 数值 | 状态 |")
    md.append("|------|------|------|")
    md.append(f"| VIX | {macro_context.get('vix', 0):.1f} | {macro_context.get('vix_regime', '')} |")
    md.append(f"| 10Y利率 | {macro_context.get('treasury_10y', 0):.2f}% | {macro_context.get('rate_environment', '')} |")
    md.append(f"| 大盘(5日) | {macro_context.get('spx_change_pct', 0):+.2f}% | {macro_context.get('market_trend', '')} |")
    md.append(f"| 美元 | — | {macro_context.get('dollar_trend', '')} |")
    md.append("")
    headwinds = macro_context.get("macro_headwinds", [])
    tailwinds = macro_context.get("macro_tailwinds", [])
    if headwinds:
        md.append("**逆风**：" + " | ".join(headwinds[:3]))
        md.append("")
    if tailwinds:
        md.append("**顺风**：" + " | ".join(tailwinds[:3]))
        md.append("")
    return md


def _build_backtest(backtest_stats) -> List[str]:
    """版块 P3：历史预测准确率（可选）"""
    md: List[str] = []
    if not backtest_stats or backtest_stats.get("total_checked", 0) <= 0:
        return md
    acc = backtest_stats["overall_accuracy"]
    total = backtest_stats["total_checked"]
    correct = backtest_stats["correct_count"]
    avg_ret = backtest_stats["avg_return"]
    md.append("## 📈 历史预测准确率（T+7，近30天）")
    md.append("")
    md.append(
        f"**样本**：{total} 条 | "
        f"**准确率**：{acc * 100:.1f}% ({correct}/{total}) | "
        f"**平均收益**：{avg_ret:+.2f}%"
    )
    md.append("")
    by_ticker = backtest_stats.get("by_ticker", {})
    if by_ticker:
        md.append("| 标的 | 方向准确率 | 预测次数 | 平均收益 |")
        md.append("|------|-----------|---------|---------|")
        for t, info in sorted(
            by_ticker.items(), key=lambda x: x[1]["total"], reverse=True
        )[:6]:
            md.append(
                f"| {t} | {info['accuracy'] * 100:.0f}% "
                f"| {info['total']} | {info['avg_return']:+.2f}% |"
            )
        md.append("")
    by_dir = backtest_stats.get("by_direction", {})
    if by_dir:
        parts = []
        for d, label in [("bullish", "看多"), ("bearish", "看空"), ("neutral", "中性")]:
            info = by_dir.get(d, {})
            if info.get("total", 0) > 0:
                parts.append(
                    f"{label}:{info['accuracy']*100:.0f}%({info['total']}次)"
                )
        if parts:
            md.append("**按方向**：" + " | ".join(parts))
            md.append("")
    return md


def generate_swarm_markdown_report(reporter, swarm_results: Dict,
                                     concentration: Dict = None,
                                     macro_context: Dict = None,
                                     backtest_stats: Dict = None,
                                     agent_count: int = 7,
                                     cross_ticker: Dict = None) -> str:
    """生成蜂群模式的 Markdown 报告（8 版块 + P4集中度 + P4b跨标的 + P5宏观 + P3回测）"""

    md = []
    md.append(f"# 【{reporter.date_str}】Alpha Hive 蜂群协作日报")
    md.append("")
    md.append(f"**自动生成于**：{reporter.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"**系统模式**：完全去中心化蜂群协作 | {agent_count} 个自治工蜂（6 核心 + BearBeeContrarian）")
    md.append("")

    # ── 方案9: 数据质量警告横幅 ──────────────────────────
    _n_tickers = len(swarm_results)
    _n_degraded = sum(
        1 for r in swarm_results.values()
        if r.get("data_quality_grade", "normal") in ("degraded", "critical")
    )
    _n_critical = sum(
        1 for r in swarm_results.values()
        if r.get("data_quality_grade", "normal") == "critical"
    )
    if _n_tickers > 0 and (_n_degraded / _n_tickers > 0.5 or _n_critical >= _n_tickers * 0.5):
        if _n_critical >= _n_tickers * 0.5:
            md.append("> 🔴 **数据严重不足警告**：多数标的仅有 1-2 个维度数据可用"
                      f"（{_n_critical}/{_n_tickers} 严重不足），报告结论可靠性极低，请勿作为决策依据。")
        else:
            md.append("> ⚠️ **数据质量降级**：超过半数标的维度覆盖不足"
                      f"（{_n_degraded}/{_n_tickers} 降级），报告结论仅供参考，请结合其他来源交叉验证。")
        md.append("")

    sorted_results = sorted(
        swarm_results.items(),
        key=lambda x: x[1]["final_score"],
        reverse=True
    )

    md.extend(_build_summary(sorted_results, len(swarm_results)))
    md.extend(_build_smart_money(sorted_results))
    md.extend(_build_market_expectations(sorted_results))
    md.extend(_build_sentiment(sorted_results))
    md.extend(_build_catalysts(sorted_results))
    md.extend(_build_competitive(sorted_results))

    md.extend(_build_bear_contrarian(sorted_results))
    md.extend(_build_composite_judgment(sorted_results))
    md.extend(_build_concentration(concentration))
    md.extend(_build_cross_ticker(cross_ticker))
    md.extend(_build_macro(macro_context))
    md.extend(_build_backtest(backtest_stats))

    # ====== 版块 8：数据来源 & 免责声明 ======
    md.append("## 8) 数据来源 & 免责声明")
    md.append("")
    md.append("**蜂群分工**：")
    md.append("- ScoutBeeNova：聪明钱侦察（SEC Form 4/13F + 拥挤度）")
    md.append("- OracleBeeEcho：市场预期（期权 IV/P-C Ratio/Gamma）")
    md.append("- BuzzBeeWhisper：社交情绪（X/Reddit/Finviz）")
    md.append("- ChronosBeeHorizon：催化剂追踪（财报/事件日历）")
    md.append("- RivalBeeVanguard：竞争格局（ML 预测 + 行业对标）")
    md.append("- GuardBeeSentinel：交叉验证（共振检测 + 风险调整）")
    md.append("")
    md.append("**免责声明**：")
    md.append(DISCLAIMER_FULL)
    md.append("")

    return "\n".join(md)


def generate_swarm_twitter_threads(reporter, swarm_results: Dict) -> List[str]:
    """生成蜂群模式的 X 线程版本"""

    threads = []
    sorted_results = sorted(
        swarm_results.items(),
        key=lambda x: x[1]["final_score"],
        reverse=True
    )

    # 主线程
    main_thread = []
    main_thread.append(
        f"【Alpha Hive 蜂群日报 {reporter.date_str}】"
        f"7 个自治工蜂协作分析，多数投票共振信号。"
        f"{DISCLAIMER_SHORT}👇"
    )

    for i, (ticker, data) in enumerate(sorted_results[:3], 1):
        resonance_emoji = "✅" if data["resonance"]["resonance_detected"] else "❌"
        insight = data.get("key_insight", "")
        narrative = data.get("narrative", "")
        tweet = (
            f"{i}. **{ticker}** {data['direction'].upper()}\n"
            f"蜂群评分：{data['final_score']:.1f}/10 | 共振：{resonance_emoji}\n"
            f"Agent 投票：看多{data['agent_breakdown']['bullish']} vs 看空{data['agent_breakdown']['bearish']}"
        )
        if narrative:
            tweet += f"\n📝 {narrative}"
        elif insight:
            tweet += f"\nAI洞察：{insight}"
        main_thread.append(tweet)

    main_thread.append(
        f"🐝 7 个工蜂独立分析（6 核心 + 看空对冲蜂）→ 信息素板实时交换 → 多数投票汇总\n"
        f"高共振信号优先级最高。风险提示：控制仓位。\n"
        f"下一步：T+1 验证，T+7 回看准确率。@igg_wang748"
    )

    threads.append("\n\n".join(main_thread))

    return threads


def generate_markdown_report(reporter) -> str:
    """生成中文 Markdown 报告"""

    md = []
    md.append(f"# 【{reporter.date_str}】Alpha Hive 每日投资简报")
    md.append("")
    md.append(f"**自动生成于**：{reporter.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append(f"**系统状态**：✅ 完全激活 | Phase 1-6 完成")
    md.append("")

    # 1. 今日摘要
    md.append("## 📊 今日摘要（Top 3）")
    md.append("")

    for i, opp in enumerate(reporter.opportunities[:3], 1):
        md.append(f"### {i}. **{opp.ticker}** - {opp.direction}")
        md.append(f"- **机会分数**：{opp.opportunity_score:.1f}/10 | **置信度**：{opp.confidence:.0f}%")
        md.append(f"- **期权信号**：{opp.options_signal}")
        if opp.key_catalysts:
            md.append(f"- **关键催化剂**：{', '.join(opp.key_catalysts[:2])}")
        md.append("")

    # 2. 机会清单
    md.append("## 🎯 完整机会清单")
    md.append("")
    md.append("| 排序 | 标的 | 方向 | 综合分 | 期权信号 | 置信度 |")
    md.append("|------|------|------|--------|---------|--------|")

    for i, opp in enumerate(reporter.opportunities, 1):
        md.append(
            f"| {i} | **{opp.ticker}** | {opp.direction} | "
            f"{opp.opportunity_score:.1f} | {opp.options_signal[:12]}... | {opp.confidence:.0f}% |"
        )

    md.append("")

    # 3. 风险雷达
    md.append("## ⚠️ 风险雷达")
    md.append("")
    for opp in reporter.opportunities[:3]:
        if opp.risks:
            md.append(f"**{opp.ticker}**：{', '.join(opp.risks)}")

    md.append("")

    # 4. 数据来源与免责
    md.append("## 📝 数据来源 & 免责声明")
    md.append("")
    md.append("**数据源**：")
    md.append("- StockTwits 情绪（实时）")
    md.append("- Polymarket 赔率（每5分钟）")
    md.append("- Yahoo Finance / yFinance（实时）")
    md.append("- SEC 披露（每日更新）")
    md.append("- **期权链数据**（yFinance，每5分钟缓存）")
    md.append("")
    md.append("**免责声明**：")
    md.append(DISCLAIMER_FULL)
    md.append("")

    return "\n".join(md)


def generate_twitter_threads(reporter) -> List[str]:
    """生成 X 线程版本"""

    threads = []

    # 主线程
    main_thread = []
    main_thread.append(
        f"【Alpha Hive 日报 {reporter.date_str}】"
        f"{DISCLAIMER_SHORT}"
        f"今天最值得跟踪的 3 个机会 👇"
    )

    for i, opp in enumerate(reporter.opportunities[:3], 1):
        main_thread.append(
            f"{i}. **{opp.ticker}** {opp.direction}\n"
            f"综合分：{opp.opportunity_score:.1f}/10 | 期权信号：{opp.options_signal}\n"
            f"主催化剂：{opp.key_catalysts[0] if opp.key_catalysts else 'TBD'}"
        )

    main_thread.append(
        f"更多详情见完整日报。风险提示：高波动标的需控制仓位。"
        f"下一步跟踪：T+1 验证信号强度，T+7 回看预测偏差。@igg_wang748"
    )

    threads.append("\n\n".join(main_thread))

    return threads

