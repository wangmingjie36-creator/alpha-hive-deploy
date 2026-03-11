#!/usr/bin/env python3
"""
🐝 Alpha Hive · 深度分析报告生成器

【混合模式推荐流程】
  Step 1: python3 collect_data.py NVDA   → 生成 data-NVDA-YYYY-MM-DD.json
  Step 2: 把 JSON 内容粘贴给 Claude     → Claude 做深度推理 + 生成 HTML

【本地规则引擎模式（备用）】
  python3 deep_analysis.py NVDA           → 直接运行规则引擎生成 HTML
  python3 deep_analysis.py --json data-NVDA-2026-03-11.json  → 从 JSON 渲染 HTML
"""

import sys
import os
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

# ── 切换到脚本所在目录 ─────────────────────────────────────
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

# ── ANSI 颜色 ─────────────────────────────────────────────
R = "\033[0m"; B = "\033[1m"; G = "\033[92m"; Y = "\033[93m"
C = "\033[96m"; D = "\033[90m"

def p(msg): print(msg, flush=True)

# ═══════════════════════════════════════════════════════════
# 1. 单 Agent 运行
# ═══════════════════════════════════════════════════════════

AGENTS = {
    "scout":   ("swarm_agents.scout_bee",    "ScoutBeeNova"),
    "oracle":  ("swarm_agents.oracle_bee",   "OracleBeeEcho"),
    "chronos": ("swarm_agents.chronos_bee",  "ChronosBeeHorizon"),
    "buzz":    ("swarm_agents.buzz_bee",     "BuzzBeeWhisper"),
    "rival":   ("swarm_agents.rival_bee",    "RivalBeeVanguard"),
    "guard":   ("swarm_agents.guard_bee",    "GuardBeeSentinel"),
    "bear":    ("swarm_agents.bear_bee",     "BearBeeContrarian"),
}

AGENT_META = {
    "scout":   {"emoji": "🔵", "name": "ScoutBee",   "dim": "聪明钱 / 内幕交易", "weight": "30%", "color": "#3b82f6"},
    "oracle":  {"emoji": "🟢", "name": "OracleBee",  "dim": "期权市场预期",       "weight": "20%", "color": "#22c55e"},
    "chronos": {"emoji": "🟣", "name": "ChronosBee", "dim": "催化剂 / 财报日历",  "weight": "20%", "color": "#a855f7"},
    "buzz":    {"emoji": "🟡", "name": "BuzzBee",    "dim": "市场情绪 / 社交",    "weight": "20%", "color": "#eab308"},
    "rival":   {"emoji": "🟠", "name": "RivalBee",   "dim": "ML 辅助预测",        "weight": "15%", "color": "#f97316"},
    "guard":   {"emoji": "🔷", "name": "GuardBee",   "dim": "风险调整 / 共振",    "weight": "15%", "color": "#06b6d4"},
    "bear":    {"emoji": "🔴", "name": "BearBee",    "dim": "看空对冲论点",       "weight": "≥3",  "color": "#ef4444"},
}


def run_agent(name: str, board, ticker: str) -> Dict:
    """运行单个 Agent，返回结构化结果"""
    t0 = time.time()
    try:
        mod_path, cls_name = AGENTS[name]
        import importlib
        mod = importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
        agent = cls(board)
        result = agent.analyze(ticker)
        result["_elapsed"] = round(time.time() - t0, 1)
        result["_agent_key"] = name
        return result
    except Exception as e:
        return {
            "score": 5.0, "direction": "neutral", "confidence": 0.0,
            "discovery": f"Error: {e}", "source": name,
            "dimension": "unknown", "details": {}, "error": str(e),
            "_elapsed": round(time.time() - t0, 1), "_agent_key": name,
        }


# ═══════════════════════════════════════════════════════════
# 2. 主分析流程
# ═══════════════════════════════════════════════════════════

def analyze(ticker: str) -> Dict:
    p(f"\n{C}{B}🐝 Alpha Hive 深度分析 · {ticker}{R}")
    p(f"{D}{'─'*50}{R}")

    # 禁用 LLM，纯规则引擎（更快，无 API 费用）
    import llm_service
    llm_service.disable()

    from pheromone_board import PheromoneBoard
    from swarm_agents import prefetch_shared_data, inject_prefetched

    # Phase 0: 预取共享数据
    p(f"{Y}[Phase 0] 预取市场数据...{R}")
    try:
        prefetched = prefetch_shared_data([ticker])
        inject_prefetched(prefetched)
        p(f"{G}✓ 数据预取完成{R}")
    except Exception as e:
        p(f"{D}⚠ 预取部分失败: {e}，Agent 将自行获取数据{R}")

    board = PheromoneBoard()

    # Phase 1: 并行跑 7 Agent
    p(f"\n{Y}[Phase 1] 并行运行 7 Agent...{R}")
    agent_results = {}
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(run_agent, name, board, ticker): name
                   for name in AGENTS}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                r = fut.result()
                agent_results[name] = r
                score = r.get("score", "?")
                direction = r.get("direction", "?")
                elapsed = r.get("_elapsed", "?")
                err = r.get("error", "")
                status = f"{G}✓{R}" if not err else f"{Y}⚠{R}"
                p(f"  {status} {AGENT_META[name]['emoji']} {AGENT_META[name]['name']:<12} "
                  f"{score:>4}/10  {direction:<8}  {elapsed}s"
                  + (f"  {D}{err[:50]}{R}" if err else ""))
            except Exception as e:
                agent_results[name] = {"score": 5.0, "direction": "neutral",
                                        "error": str(e), "_agent_key": name}

    # Phase 2: QueenDistiller 汇总
    p(f"\n{Y}[Phase 2] QueenDistiller 综合评分...{R}")
    try:
        from swarm_agents.queen_distiller import QueenDistiller
        queen = QueenDistiller(board)
        t0 = time.time()
        queen_result = queen.distill(ticker, list(agent_results.values()))
        elapsed_q = round(time.time() - t0, 1)

        final_score = queen_result.get("final_score", 5.0)
        direction   = queen_result.get("direction", "neutral")
        p(f"{G}{B}  👑 最终评分: {final_score}/10 · {direction} · {elapsed_q}s{R}")
    except Exception as e:
        p(f"{Y}  ⚠ QueenDistiller 失败: {e}{R}")
        queen_result = {"final_score": 5.0, "direction": "neutral",
                        "error": str(e), "dimension_scores": {},
                        "agent_breakdown": {}, "reasoning": ""}

    return {
        "ticker": ticker,
        "timestamp": datetime.now().isoformat(),
        "agent_results": agent_results,
        "queen_result": queen_result,
    }


# ═══════════════════════════════════════════════════════════
# 3. HTML 报告生成
# ═══════════════════════════════════════════════════════════

def _score_color(score):
    if score >= 7.5: return "#16a34a"   # 绿
    if score >= 6.0: return "#ca8a04"   # 黄
    if score >= 4.5: return "#9a3412"   # 橙
    return "#dc2626"                     # 红

def _direction_badge(direction):
    d = (direction or "neutral").lower()
    if "bull" in d: return '<span class="badge bull">📈 看多</span>'
    if "bear" in d: return '<span class="badge bear">📉 看空</span>'
    return '<span class="badge neut">➖ 中性</span>'

def _score_ring(score, color):
    """SVG 圆环评分"""
    try: score = float(score)
    except: score = 5.0
    pct = score / 10
    r = 28; circ = 2 * 3.14159 * r
    dash = pct * circ
    return f'''<svg width="80" height="80" viewBox="0 0 80 80">
      <circle cx="40" cy="40" r="{r}" fill="none" stroke="#e5e7eb" stroke-width="8"/>
      <circle cx="40" cy="40" r="{r}" fill="none" stroke="{color}" stroke-width="8"
              stroke-dasharray="{dash:.1f} {circ:.1f}" stroke-linecap="round"
              transform="rotate(-90 40 40)"/>
      <text x="40" y="44" text-anchor="middle" font-size="16" font-weight="bold"
            fill="{color}">{score:.1f}</text>
    </svg>'''


def _render_details(name: str, result: Dict) -> str:
    """渲染各 Agent 的原始数据区块"""
    details = result.get("details", {}) or {}
    extras  = result.get("extras",  {}) or {}
    rows = []

    if name == "scout":
        insider = details.get("insider", {})
        if insider:
            rows.append(("内幕情绪", insider.get("sentiment", "—")))
            b = insider.get("dollar_bought", 0)
            s = insider.get("dollar_sold", 0)
            if b: rows.append(("内幕买入", f"${b:,.0f}"))
            if s: rows.append(("内幕卖出", f"${s:,.0f}"))
            rows.append(("申报数量", f"{insider.get('filings', 0)} 份"))
            trades = insider.get("notable_trades", [])
            if trades:
                t = trades[0]
                rows.append(("最大交易", f"{t.get('name','?')} {t.get('transaction_type','?')} ${t.get('total_value',0):,.0f}"))
        crd = details.get("crowding_score", details.get("crowding", None))
        if crd is not None:
            rows.append(("拥挤度", f"{crd}/100"))
        rss = details.get("rss_summary", "")
        if rss:
            rows.append(("最新公告", rss[:80]))

    elif name == "oracle":
        for k, v in list(details.items())[:8]:
            if v is not None and v != "":
                rows.append((k, str(v)[:60]))

    elif name == "chronos":
        cats = details.get("catalysts", [])
        if cats:
            rows.append(("近期催化剂数", str(len(cats))))
            c0 = cats[0]
            rows.append(("最近事件", f"{c0.get('type','?')} · {c0.get('days_until','?')}天后"))
        at = details.get("analyst_targets", {})
        if at:
            target = at.get("target_mean") or at.get("mean_target")
            upside = at.get("upside_pct")
            if target: rows.append(("分析师目标价", f"${target:.2f}"))
            if upside: rows.append(("上行空间", f"{upside:.1f}%"))

    elif name == "buzz":
        pct = details.get("sentiment_pct")
        if pct is not None: rows.append(("看多情绪", f"{pct:.1f}%"))
        mom = details.get("momentum_5d")
        if mom is not None: rows.append(("5日动量", f"{mom:+.2f}%"))
        vol = details.get("volume_ratio")
        if vol is not None: rows.append(("成交量比", f"{vol:.2f}x"))
        reddit = details.get("reddit", {})
        if reddit:
            rows.append(("Reddit 排名", f"#{reddit.get('rank', '—')}"))
            rows.append(("Reddit 提及", f"{reddit.get('mentions', 0)} 次"))
            rows.append(("Reddit 情绪", str(reddit.get("buzz", "—"))))
        fg = details.get("components", {}).get("fear_greed_signal")
        if fg is not None: rows.append(("恐惧贪婪", f"{fg:.1f}"))

    elif name == "rival":
        pred = details.get("predicted_30d") or details.get("expected_30d")
        if pred is not None: rows.append(("ML 30日预测", f"{pred:+.1f}%"))
        mom = details.get("momentum_5d")
        if mom is not None: rows.append(("5日动量", f"{mom:+.2f}%"))
        for k, v in list(details.items())[:6]:
            if k not in ("momentum_5d", "predicted_30d", "expected_30d") and v is not None:
                rows.append((k[:20], str(v)[:40]))

    elif name == "guard":
        res = details.get("resonance", {})
        if isinstance(res, dict):
            rows.append(("共振检测", "✓ 检测到" if res.get("resonance_detected") else "✗ 无共振"))
            if res.get("resonance_detected"):
                rows.append(("共振方向", res.get("direction", "—")))
                rows.append(("共振 Agent 数", str(res.get("supporting_agents", 0))))
        rows.append(("一致性", f"{details.get('consistency', 0):.0f}%"))
        rows.append(("调整系数", f"{details.get('adjustment_factor', 1.0):.2f}"))

    elif name == "bear":
        rows.append(("看空总分", f"{details.get('bear_score', 0):.1f}/10"))
        sigs = details.get("bearish_signals", [])
        if sigs:
            for i, s in enumerate(sigs[:3], 1):
                rows.append((f"风险 {i}", str(s)[:70]))
        for sub in ["insider_bear", "overval_bear", "options_bear", "momentum_bear", "news_bear"]:
            v = details.get(sub)
            labels = {"insider_bear":"内幕风险","overval_bear":"估值风险",
                      "options_bear":"期权风险","momentum_bear":"动量风险","news_bear":"新闻风险"}
            if v: rows.append((labels.get(sub, sub), f"{v:.1f}"))
        # extras
        thesis = extras.get("llm_thesis", "")
        if thesis:
            rows.append(("AI 论点", thesis[:120]))

    if not rows:
        # 通用降级：显示 details 的前几个字段
        for k, v in list(details.items())[:6]:
            if v is not None:
                rows.append((str(k)[:20], str(v)[:60]))

    if not rows:
        return '<p class="no-data">暂无原始数据（Agent 可能降级运行）</p>'

    html = '<table class="data-table"><tbody>'
    for label, val in rows:
        html += f'<tr><td class="dt-label">{label}</td><td class="dt-val">{val}</td></tr>'
    html += '</tbody></table>'
    return html


def _ascii_timeline(catalysts: list, ticker: str) -> str:
    """ChronosBee ASCII 时间轴"""
    if not catalysts:
        return '<pre class="timeline">（无近期催化剂数据）</pre>'
    lines = [f"{'─'*48}", f"  {ticker} 催化剂时间轴", f"{'─'*48}"]
    today_label = "TODAY"
    lines.append(f"  {today_label:<12} ◄ 当前")
    for c in catalysts[:6]:
        days  = c.get("days_until", "?")
        ctype = c.get("type", "event")
        name  = c.get("name", c.get("title", ctype))[:28]
        bar   = "·" * min(int(days) if isinstance(days, (int,float)) else 0, 20)
        lines.append(f"  +{str(days)+'d':<11} {bar}▶ {name}")
    lines.append(f"{'─'*48}")
    return '<pre class="timeline">' + "\n".join(lines) + "</pre>"


def _resonance_matrix(guard_r: Dict, all_agents: Dict) -> str:
    """GuardBee 信号共振矩阵"""
    res = (guard_r.get("details") or {}).get("resonance", {})
    if not isinstance(res, dict):
        return '<p class="dim-text">共振数据不可用</p>'
    detected  = res.get("resonance_detected", False)
    direction = res.get("direction", "neutral")
    dims      = res.get("resonant_dimensions", [])
    count     = res.get("supporting_agents", 0)
    boost     = res.get("confidence_boost", 0)
    consistency = (guard_r.get("details") or {}).get("consistency", 0)

    dim_map = {"signal":"聪明钱","catalyst":"催化剂","sentiment":"情绪",
               "odds":"赔率","risk_adj":"风险调整"}
    rows = ""
    for dim_key, dim_label in dim_map.items():
        agent_scores = [v.get("score",5) for k,v in all_agents.items()
                        if (v.get("dimension","") or "").startswith(dim_key[:4])]
        avg = sum(agent_scores)/len(agent_scores) if agent_scores else None
        in_res = dim_key in (dims or [])
        icon = "⚡" if in_res else ("✓" if avg and avg>=6 else "·")
        color = "#fbbf24" if in_res else ("#86efac" if avg and avg>=6 else "#475569")
        avg_str = f"{avg:.1f}" if avg else "—"
        rows += f'<tr><td style="color:{color}">{icon} {dim_label}</td><td style="color:{color}">{avg_str}</td><td>{"共振" if in_res else ""}</td></tr>'

    status_color = "#fbbf24" if detected else "#475569"
    status_text  = f"⚡ 共振检测到 · {direction} · {count} Agent · +{boost:.0%} 置信" if detected else "无共振（信号分散）"
    return f'''
    <div style="margin-bottom:10px;color:{status_color};font-weight:600">{status_text}</div>
    <div style="margin-bottom:6px;color:#64748b;font-size:0.82em">一致性指数: {consistency:.0f}%</div>
    <table class="mini-table"><thead><tr><th>维度</th><th>均分</th><th>状态</th></tr></thead>
    <tbody>{rows}</tbody></table>'''


def _phase1_block(key: str, r: Dict) -> str:
    """Phase 1 固定格式：原始数据→异常识别→维度结论→置信度"""
    meta    = AGENT_META[key]
    details = r.get("details", {}) or {}
    extras  = r.get("extras",  {}) or {}
    score   = float(r.get("score", 5.0))
    conf    = float(r.get("confidence", 0.0))
    disc    = r.get("discovery", "")
    err     = r.get("error", "")
    elapsed = r.get("_elapsed", "?")
    sc_color = _score_color(score)
    dq      = r.get("data_quality", {}) or {}

    # ── 原始数据行 ────────────────────────────────────────
    raw_lines = []
    if key == "scout":
        ins = details.get("insider", {})
        if ins:
            raw_lines.append(f"内幕买入 ${ins.get('dollar_bought',0):,.0f}  卖出 ${ins.get('dollar_sold',0):,.0f}  申报 {ins.get('filings',0)} 份")
            trades = ins.get("notable_trades", [])
            for t in trades[:2]:
                raw_lines.append(f"  └ {t.get('name','?')} {t.get('transaction_type','?')} ${t.get('total_value',0):,.0f} [{t.get('filing_date','?')}]")
        crd = details.get("crowding_score")
        if crd: raw_lines.append(f"拥挤度 {crd}/100")
        rss = details.get("rss_summary","")
        if rss: raw_lines.append(f"最新公告: {rss[:80]}")

    elif key == "oracle":
        for k,v in list(details.items())[:6]:
            if v: raw_lines.append(f"{k}: {v}")

    elif key == "chronos":
        cats = details.get("catalysts",[])
        if cats:
            raw_lines.append(f"近期催化剂 {len(cats)} 个:")
            for c in cats[:3]:
                raw_lines.append(f"  └ {c.get('type','?')} · +{c.get('days_until','?')}天 · {c.get('name',c.get('title',''))[:30]}")
        at = details.get("analyst_targets",{})
        if at:
            raw_lines.append(f"分析师目标价 ${at.get('target_mean',at.get('mean_target','—'))}  上行 {at.get('upside_pct','—')}%")

    elif key == "buzz":
        pct = details.get("sentiment_pct")
        mom = details.get("momentum_5d")
        vol = details.get("volume_ratio")
        if pct is not None: raw_lines.append(f"看多情绪 {pct:.1f}%")
        if mom is not None: raw_lines.append(f"5日动量 {mom:+.2f}%")
        if vol is not None: raw_lines.append(f"成交量比 {vol:.2f}x")
        rd = details.get("reddit",{})
        if rd: raw_lines.append(f"Reddit #{rd.get('rank','—')}  提及 {rd.get('mentions',0)} 次  情绪 {rd.get('buzz','—')}")
        fg = (details.get("components") or {}).get("fear_greed_signal")
        if fg: raw_lines.append(f"恐惧贪婪指数 {fg:.0f}")

    elif key == "rival":
        p30 = details.get("predicted_30d") or details.get("expected_30d")
        m5  = details.get("momentum_5d")
        if p30: raw_lines.append(f"ML 30日预测 {p30:+.1f}%")
        if m5:  raw_lines.append(f"5日动量 {m5:+.2f}%")

    elif key == "guard":
        res = details.get("resonance",{})
        if isinstance(res,dict):
            raw_lines.append(f"共振: {'✓ 检测到 ' + str(res.get('direction','')) if res.get('resonance_detected') else '✗ 无共振'}")
        raw_lines.append(f"一致性 {details.get('consistency',0):.0f}%  调整系数 {details.get('adjustment_factor',1.0):.2f}")

    elif key == "bear":
        raw_lines.append(f"看空综合分 {details.get('bear_score',0):.1f}/10")
        subs = [("insider_bear","内幕"),("overval_bear","估值"),("options_bear","期权"),
                ("momentum_bear","动量"),("news_bear","新闻")]
        parts = [f"{lbl} {details[k]:.1f}" for k,lbl in subs if details.get(k)]
        if parts: raw_lines.append("  └ " + "  ".join(parts))
        for i,s in enumerate(details.get("bearish_signals",[])[:3],1):
            raw_lines.append(f"  {i}. {s[:70]}")

    if not raw_lines and not err:
        raw_lines = [f"{k}: {v}" for k,v in list(details.items())[:4] if v]
    if err and not raw_lines:
        raw_lines = [f"错误: {err[:80]}"]

    raw_html = "\n".join(raw_lines) if raw_lines else "（无数据）"

    # ── 异常识别 ──────────────────────────────────────────
    anomaly = ""
    if key == "scout":
        ins = details.get("insider",{})
        b = ins.get("dollar_bought",0); s = ins.get("dollar_sold",0)
        if b > s*2 and b > 0:       anomaly = f"⚡ 内幕净买入显著超过卖出（买 ${b:,.0f} vs 卖 ${s:,.0f}）"
        elif s > b*2 and s > 0:     anomaly = f"⚠ 内幕净卖出显著超过买入（卖 ${s:,.0f} vs 买 ${b:,.0f}）"
        else:                        anomaly = "— 内幕交易无明显异常"
    elif key == "chronos":
        cats = details.get("catalysts",[])
        near = [c for c in cats if isinstance(c.get("days_until"),int) and c["days_until"] <= 14]
        anomaly = f"⚡ {len(near)} 个催化剂在14天内" if near else "— 近期无高影响催化剂"
    elif key == "buzz":
        pct = details.get("sentiment_pct",50)
        if pct > 70:   anomaly = f"⚡ 看多情绪过热 {pct:.0f}%（可能反转预警）"
        elif pct < 30: anomaly = f"⚡ 看多情绪极低 {pct:.0f}%（潜在超卖）"
        else:           anomaly = f"— 情绪中性区间 {pct:.0f}%"
    elif key == "bear":
        sigs = details.get("bearish_signals",[])
        anomaly = f"⚡ {len(sigs)} 条独立看空信号{'（超过门控3条）' if len(sigs)>=3 else '（未达门控3条）'}"
    elif key == "guard":
        res = details.get("resonance",{})
        anomaly = "⚡ 检测到跨维度信号共振" if (isinstance(res,dict) and res.get("resonance_detected")) else "— 无共振，信号分散"
    else:
        anomaly = f"— {disc[:60]}" if disc and not disc.startswith("Error") else "— 无异常"

    # ── 数据质量徽章 ─────────────────────────────────────
    dq_html = ""
    for src, quality in list(dq.items())[:4]:
        cls = "dq-real" if quality in ("real","loaded","llm_enhanced") else "dq-fallback"
        dq_html += f'<span class="dq-badge {cls}">{src}:{quality}</span> '

    ring_sm = _score_ring(score, sc_color)
    dir_badge = _direction_badge(r.get("direction","neutral"))

    return f'''
    <div class="p1-block" style="border-left:3px solid {meta["color"]}">
      <div class="p1-header">
        <div style="flex-shrink:0">{ring_sm}</div>
        <div style="flex:1">
          <div class="p1-name">{meta["emoji"]} {meta["name"]} <span class="p1-dim">{meta["dim"]}</span></div>
          <div style="margin-top:4px">{dir_badge} <span class="p1-conf">置信度 {conf*100:.0f}%</span> <span class="p1-time">{elapsed}s</span></div>
        </div>
      </div>
      <div class="p1-row"><span class="p1-label">原始数据</span><pre class="p1-pre">{raw_html}</pre></div>
      <div class="p1-row"><span class="p1-label">异常识别</span><span class="p1-val">{anomaly}</span></div>
      <div class="p1-row"><span class="p1-label">维度结论</span>
        <span class="p1-val" style="color:{sc_color};font-weight:700">{meta["dim"]}: {score:.1f}/10</span>
        <span class="p1-explain">← {disc[:80] if disc and not disc.startswith("Error") else "规则引擎计算"}</span>
      </div>
      {f'<div class="p1-dq">{dq_html}</div>' if dq_html else ""}
    </div>'''


def _scenarios(final_score: float, direction: str, ticker: str, agents: Dict) -> str:
    """第5章：4情景推演表格 + 概率加权期望收益"""
    d = direction.lower()
    # 根据方向和评分设定情景
    if "bull" in d and final_score >= 7:
        rows = [
            ("🐂 强烈看多", "25%", f"+18~+25%", "信号持续共振，机构跟进建仓，催化剂提前兑现"),
            ("📈 温和看多（基准）", "40%", f"+8~+15%", "当前趋势延续，数据如期改善"),
            ("➖ 震荡整理", "25%", f"-3~+5%", "催化剂落地不及预期，市场观望"),
            ("🐻 下行风险", "10%", f"-12~-20%", "宏观转向、内幕大额卖出、BearBee信号兑现"),
        ]
    elif "bear" in d:
        rows = [
            ("🐻 强烈看空", "25%", f"-18~-28%", "看空信号全面兑现，机构减仓加速"),
            ("📉 温和看空（基准）", "40%", f"-8~-15%", "当前压力延续，支撑位失守"),
            ("➖ 震荡整理", "25%", f"-3~+3%", "多空博弈平衡，方向待定"),
            ("🐂 反弹风险", "10%", f"+8~+15%", "超卖反弹、利好催化剂突发"),
        ]
    else:
        rows = [
            ("📈 上行情景", "20%", f"+10~+18%", "催化剂触发、情绪转好、资金流入"),
            ("➖ 中性基准", "45%", f"-3~+5%", "数据维持现状，无重大变化"),
            ("📉 下行情景", "25%", f"-8~-15%", "宏观压力、行业逆风、信号转空"),
            ("🐻 尾部风险", "10%", f"-18~-28%", "黑天鹅事件、流动性冲击"),
        ]

    # 概率加权期望收益估算
    def mid(s):
        try:
            parts = s.replace("%","").replace("+","").split("~")
            return (float(parts[0]) + float(parts[1])) / 2
        except: return 0
    ev = sum(float(r[1].replace("%",""))/100 * mid(r[2]) for r in rows)

    tr = ""
    for scenario, prob, ret, desc in rows:
        ret_color = "#86efac" if "+" in ret else "#fca5a5" if "-" in ret[:2] else "#94a3b8"
        tr += f'<tr><td>{scenario}</td><td class="tc">{prob}</td><td class="tc" style="color:{ret_color};font-weight:600">{ret}</td><td class="small">{desc}</td></tr>'

    ev_color = "#86efac" if ev > 0 else "#fca5a5"
    return f'''
    <div style="overflow-x:auto">
    <table class="full-table">
      <thead><tr><th>情景</th><th>概率</th><th>预期收益</th><th>触发条件</th></tr></thead>
      <tbody>{tr}</tbody>
    </table>
    </div>
    <div class="ev-box">概率加权期望收益 <span style="color:{ev_color};font-weight:700;font-size:1.1em">{ev:+.1f}%</span></div>'''


def _invalidation_table(final_score: float, direction: str, agents: Dict) -> str:
    """第4章：投资假设 + 失效条件（具体阈值）"""
    d = direction.lower()
    bear_r = agents.get("bear", {})
    bear_details = bear_r.get("details", {}) or {}
    bear_sigs = bear_details.get("bearish_signals", [])
    chronos_r = agents.get("chronos", {})
    cats = (chronos_r.get("details") or {}).get("catalysts", [])
    nearest_days = cats[0].get("days_until","?") if cats else "?"

    if "bull" in d:
        rows = [
            ("✅ 假设 1", "内幕净买入维持正值", f"ScoutBee 内幕卖出超过买入 2 倍"),
            ("✅ 假设 2", "催化剂按期兑现", f"ChronosBee 催化剂推迟超过 7 天"),
            ("✅ 假设 3", f"综合评分维持 ≥ {max(final_score-1.5, 5.0):.1f}", f"QueenDistiller 评分跌破 {max(final_score-1.5, 5.0):.1f}"),
            ("✅ 假设 4", "BearBee 看空论点未超过 3 条", f"BearBee 新增独立看空信号 ≥ 3 条（当前 {len(bear_sigs)} 条）"),
            ("✅ 假设 5", f"无重大宏观黑天鹅", "VIX > 35 或单日大盘跌幅 > 3%"),
        ]
    elif "bear" in d:
        rows = [
            ("⚠ 假设 1", "下行趋势持续", f"5日动量转正且 > +3%"),
            ("⚠ 假设 2", f"综合评分维持 ≤ {min(final_score+1.5, 7.0):.1f}", f"QueenDistiller 评分突破 {min(final_score+1.5, 7.0):.1f}"),
            ("⚠ 假设 3", "无正向催化剂", f"ChronosBee 检测到 {nearest_days} 天内正向催化剂"),
            ("⚠ 假设 4", "机构持续减仓", "ScoutBee 检测到内幕大额买入"),
            ("⚠ 假设 5", "BearBee 信号保持强度", "BearBee 看空分低于 4.0"),
        ]
    else:
        rows = [
            ("❓ 假设 1", "当前震荡区间持续", "突破区间上沿（评分 > 7.5）或下沿（评分 < 4.0）"),
            ("❓ 假设 2", "无重大催化剂", f"ChronosBee 检测到 {nearest_days} 天内高冲击催化剂"),
            ("❓ 假设 3", "情绪中性区间", "BuzzBee 看多情绪突破 70% 或跌破 30%"),
            ("❓ 假设 4", "成交量正常", "成交量比 > 2.5x（异常资金流入/流出）"),
        ]

    tr = "".join(f'<tr><td class="tag">{tag}</td><td>{assume}</td><td class="invalidate">{fail}</td></tr>'
                 for tag, assume, fail in rows)
    return f'''
    <table class="full-table">
      <thead><tr><th style="width:10%">标记</th><th>当前假设</th><th>失效条件（触发即重新评估）</th></tr></thead>
      <tbody>{tr}</tbody>
    </table>'''


def _risk_radar(agents: Dict, qr: Dict) -> str:
    """第6章：风险雷达"""
    bear_r = agents.get("bear", {})
    bear_d = bear_r.get("details", {}) or {}
    guard_r = agents.get("guard", {})
    guard_d = guard_r.get("details", {}) or {}
    chronos_d = (agents.get("chronos",{}).get("details") or {})
    buzz_d = (agents.get("buzz",{}).get("details") or {})

    risks = []

    # 看空强度
    bs = bear_d.get("bear_score", 0)
    if bs >= 7:   risks.append(("🔴 极高", "看空信号强度", f"BearBee 综合看空分 {bs:.1f}/10"))
    elif bs >= 5: risks.append(("🟡 中等", "看空信号强度", f"BearBee 综合看空分 {bs:.1f}/10"))
    else:         risks.append(("🟢 低",   "看空信号强度", f"BearBee 综合看空分 {bs:.1f}/10"))

    # 催化剂风险
    cats = chronos_d.get("catalysts",[])
    near = [c for c in cats if isinstance(c.get("days_until"),int) and c["days_until"] <= 14]
    if near: risks.append(("🟡 关注", "近期催化剂", f"{len(near)} 个事件在 14 天内"))
    else:    risks.append(("🟢 低",   "近期催化剂", "无近期高影响催化剂"))

    # 情绪风险
    pct = buzz_d.get("sentiment_pct", 50)
    if pct > 75:   risks.append(("🔴 过热", "市场情绪", f"看多情绪 {pct:.0f}%（反转预警）"))
    elif pct < 25: risks.append(("🔴 恐慌", "市场情绪", f"看多情绪仅 {pct:.0f}%（极度悲观）"))
    elif pct > 60: risks.append(("🟡 偏热", "市场情绪", f"看多情绪 {pct:.0f}%"))
    else:          risks.append(("🟢 中性", "市场情绪", f"看多情绪 {pct:.0f}%"))

    # 共振状态
    res = guard_d.get("resonance",{})
    if isinstance(res,dict) and res.get("resonance_detected"):
        risks.append(("⚡ 共振", "信号共振", f"跨维度共振检测到 · {res.get('direction','')} · 置信度+{res.get('confidence_boost',0):.0%}"))
    else:
        risks.append(("🟡 分散", "信号共振", "无共振，各维度信号分散"))

    # 内幕交易风险
    scout_d = (agents.get("scout",{}).get("details") or {})
    ins = scout_d.get("insider",{})
    if ins:
        s = ins.get("dollar_sold",0); b = ins.get("dollar_bought",0)
        if s > b*3 and s > 0: risks.append(("🔴 高", "内幕交易", f"内幕净卖出 ${s-b:,.0f}（警惕！）"))
        elif b > s*2 and b > 0: risks.append(("🟢 正面", "内幕交易", f"内幕净买入 ${b-s:,.0f}（积极信号）"))
        else: risks.append(("🟢 中性", "内幕交易", "内幕交易无异常"))

    tr = "".join(f'<tr><td class="risk-level">{level}</td><td class="risk-cat">{cat}</td><td class="small">{desc}</td></tr>'
                 for level, cat, desc in risks)
    return f'''
    <table class="full-table">
      <thead><tr><th>风险等级</th><th>风险类型</th><th>具体描述</th></tr></thead>
      <tbody>{tr}</tbody>
    </table>'''


def _tracking_tasks(ticker: str, agents: Dict, qr: Dict) -> str:
    """第7章：明日追踪任务"""
    from datetime import timedelta
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%m/%d")
    in7d = (datetime.now() + timedelta(days=7)).strftime("%m/%d")

    bear_d = (agents.get("bear",{}).get("details") or {})
    chronos_d = (agents.get("chronos",{}).get("details") or {})
    guard_d = (agents.get("guard",{}).get("details") or {})
    cats = chronos_d.get("catalysts",[])
    final_score = float(qr.get("final_score", 5.0))

    tasks = [
        f"☐ [{tomorrow}] 检查 SEC EDGAR {ticker} 是否有新 Form 4 大额内部人交易申报",
        f"☐ [{tomorrow}] 监控 QueenDistiller 综合评分是否偏离当前 {final_score:.1f}（±1.5 触发重评）",
        f"☐ [{tomorrow}] 追踪 BearBee 看空信号数量（当前 {len(bear_d.get('bearish_signals',[]))} 条，≥5 条升级警戒）",
    ]
    if cats:
        c0 = cats[0]
        tasks.append(f"☐ [{c0.get('days_until','?')}天后] 关注 {c0.get('type','催化剂')} 事件: {c0.get('name',c0.get('title',''))[:30]}")
    res = guard_d.get("resonance",{})
    if isinstance(res,dict) and res.get("resonance_detected"):
        tasks.append(f"☐ [{in7d}] 跟踪信号共振是否持续（当前 {res.get('direction','')}）")
    else:
        tasks.append(f"☐ [{in7d}] 监控信号共振是否形成（GuardBee 一致性需 > 60%）")
    tasks.append(f"☐ [{in7d}] 重跑 deep_analysis.py {ticker} 对比评分变化")
    tasks.append(f"☐ [持续] 关注 BuzzBee 情绪是否突破 70% 或跌破 30%（情绪极端反转预警）")

    return "<ul class='task-list'>" + "".join(f"<li>{t}</li>" for t in tasks) + "</ul>"


def generate_html(data: Dict) -> str:
    ticker      = data["ticker"]
    ts          = data["timestamp"][:16].replace("T", " ")
    agents      = data["agent_results"]
    qr          = data["queen_result"]

    final_score = float(qr.get("final_score", 5.0))
    direction   = qr.get("direction", "neutral")
    reasoning   = qr.get("reasoning", "")
    key_insight = qr.get("key_insight", "")
    dim_scores  = qr.get("dimension_scores", {})
    breakdown   = qr.get("agent_breakdown", {})
    llm_conf    = qr.get("llm_confidence", 0.0)
    resonance   = qr.get("resonance", {})

    score_color = _score_color(final_score)
    dir_badge   = _direction_badge(direction)
    ring        = _score_ring(final_score, score_color)
    bull_c = breakdown.get("bullish", 0)
    bear_c = breakdown.get("bearish", 0)
    neut_c = breakdown.get("neutral", 0)
    conf_pct = f"{llm_conf*100:.0f}%" if llm_conf else "规则引擎"

    # ── 第2章：五维评分条 ────────────────────────────────
    DIM_MAP = [
        ("signal",   "Signal 聪明钱",    0.30),
        ("catalyst", "Catalyst 催化剂",  0.20),
        ("sentiment","Sentiment 情绪",   0.20),
        ("odds",     "Odds 赔率",        0.15),
        ("risk_adj", "RiskAdj 风险调整", 0.15),
    ]
    dim_rows = ""
    weighted_sum = 0.0
    for dk, dlabel, w in DIM_MAP:
        sc = dim_scores.get(dk)
        if sc is None:
            dim_rows += f'<tr><td>{dlabel}</td><td class="tc">×{int(w*100)}%</td><td>—</td><td class="tc">—</td><td class="small dim-text">数据不可用</td></tr>'
            continue
        sc = float(sc)
        weighted_sum += sc * w
        bar_color = _score_color(sc)
        bar = f'<div style="background:#0f172a;border-radius:4px;height:8px;width:100%"><div style="background:{bar_color};height:8px;border-radius:4px;width:{sc*10}%"></div></div>'
        contrib = sc * w
        dim_rows += f'<tr><td>{dlabel}</td><td class="tc">×{int(w*100)}%</td><td style="width:120px">{bar}</td><td class="tc" style="color:{bar_color};font-weight:700">{sc:.1f}</td><td class="tc small">{contrib:.2f}</td></tr>'

    # ── 第3章：Phase 1 Agent 块 ──────────────────────────
    phase1_blocks = "".join(_phase1_block(k, agents.get(k, {})) for k in
                            ["scout","oracle","chronos","buzz","rival","guard","bear"])

    # ── ChronosBee ASCII 时间轴 ──────────────────────────
    cats = (agents.get("chronos",{}).get("details") or {}).get("catalysts", [])
    timeline_html = _ascii_timeline(cats, ticker)

    # ── GuardBee 信号共振矩阵 ────────────────────────────
    resonance_matrix_html = _resonance_matrix(agents.get("guard",{}), agents)

    # ── 第1章摘要 ────────────────────────────────────────
    summary_lines = []
    if reasoning:
        for l in str(reasoning).split("\n"):
            if l.strip(): summary_lines.append(l.strip())
    summary_text = " ".join(summary_lines[:3]) if summary_lines else f"基于7 Agent并行分析，综合评分 {final_score:.1f}/10，方向 {direction}。"

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{ticker} 深度研究 · Alpha Hive · {ts[:10]}</title>
<style>
*, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        background:#0f172a; color:#e2e8f0; min-height:100vh; padding:24px 16px; }}
.container {{ max-width:1000px; margin:0 auto; }}

/* Header */
.header {{ background:linear-gradient(135deg,#1e293b,#0f172a); border:1px solid #334155;
           border-radius:16px; padding:28px 32px; margin-bottom:20px;
           display:flex; gap:24px; align-items:center; flex-wrap:wrap; }}
.h-ticker {{ font-size:2em; font-weight:800; color:#f8fafc; letter-spacing:-1px; }}
.h-sub {{ color:#64748b; font-size:0.88em; margin-top:3px; }}
.h-stats {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:10px; }}
.hstat {{ background:#0f172a; border:1px solid #334155; border-radius:8px; padding:8px 14px; text-align:center; }}
.hstat-v {{ font-size:1.3em; font-weight:700; }}
.hstat-l {{ font-size:0.72em; color:#475569; margin-top:2px; }}

/* Section */
.section {{ background:#1e293b; border:1px solid #334155; border-radius:12px;
            padding:22px 24px; margin-bottom:18px; }}
.ch-title {{ font-size:1em; font-weight:700; color:#f1f5f9; margin-bottom:16px;
             padding-bottom:10px; border-bottom:1px solid #334155; }}

/* Badge */
.badge {{ display:inline-block; padding:3px 10px; border-radius:20px; font-size:0.82em; font-weight:600; }}
.bull {{ background:#14532d; color:#86efac; }}
.bear {{ background:#7f1d1d; color:#fca5a5; }}
.neut {{ background:#1e293b; color:#94a3b8; border:1px solid #475569; }}

/* Summary box */
.summary-box {{ background:#172554; border-left:3px solid #3b82f6; padding:12px 16px;
                border-radius:0 8px 8px 0; color:#bfdbfe; line-height:1.7; font-size:0.95em; }}
.insight-box {{ background:#1e3a2f; border-left:3px solid #16a34a; padding:10px 16px;
                border-radius:0 8px 8px 0; color:#86efac; margin-top:10px; font-size:0.9em; }}

/* Tables */
.full-table {{ width:100%; border-collapse:collapse; font-size:0.88em; }}
.full-table th {{ background:#0f172a; color:#64748b; padding:8px 10px; text-align:left; font-size:0.8em; text-transform:uppercase; letter-spacing:0.04em; }}
.full-table td {{ padding:9px 10px; border-bottom:1px solid #0f172a; }}
.full-table tr:hover td {{ background:#1e293b44; }}
.tc {{ text-align:center; }}
.small {{ font-size:0.82em; color:#94a3b8; }}
.mini-table {{ width:100%; border-collapse:collapse; font-size:0.85em; }}
.mini-table th {{ color:#475569; padding:5px 8px; text-align:left; border-bottom:1px solid #334155; }}
.mini-table td {{ padding:5px 8px; border-bottom:1px solid #0f172a; }}

/* Phase 1 blocks */
.p1-block {{ background:#0f172a; border-radius:10px; padding:16px; margin-bottom:14px;
             padding-left:18px; }}
.p1-header {{ display:flex; gap:12px; align-items:flex-start; margin-bottom:12px; }}
.p1-name {{ font-size:0.95em; font-weight:700; color:#f1f5f9; }}
.p1-dim {{ font-size:0.8em; color:#475569; font-weight:400; margin-left:6px; }}
.p1-conf {{ font-size:0.78em; color:#475569; margin-left:6px; }}
.p1-time {{ font-size:0.75em; color:#334155; margin-left:6px; }}
.p1-row {{ display:flex; gap:12px; margin-bottom:8px; align-items:baseline; }}
.p1-label {{ font-size:0.75em; font-weight:600; color:#475569; text-transform:uppercase;
             letter-spacing:0.05em; min-width:60px; flex-shrink:0; }}
.p1-val {{ font-size:0.88em; color:#e2e8f0; }}
.p1-explain {{ font-size:0.8em; color:#64748b; margin-left:8px; }}
.p1-pre {{ font-size:0.82em; color:#94a3b8; line-height:1.6; white-space:pre-wrap;
           font-family:ui-monospace,"SF Mono",monospace; }}
.p1-dq {{ margin-top:8px; display:flex; flex-wrap:wrap; gap:4px; }}

/* Timeline */
.timeline {{ font-size:0.82em; color:#64748b; line-height:1.6; background:#0f172a;
             padding:14px 16px; border-radius:8px; font-family:ui-monospace,"SF Mono",monospace; }}

/* Risk radar */
.risk-level {{ font-weight:600; }}
.risk-cat {{ color:#94a3b8; }}

/* Scenarios */
.ev-box {{ margin-top:12px; padding:10px 16px; background:#0f172a; border-radius:8px;
           font-size:0.9em; color:#94a3b8; }}

/* Tasks */
.task-list {{ list-style:none; padding:0; }}
.task-list li {{ padding:7px 0; border-bottom:1px solid #0f172a;
                 font-size:0.88em; color:#94a3b8; font-family:ui-monospace,"SF Mono",monospace; }}

/* Invalidation */
.tag {{ color:#fbbf24; font-weight:600; font-size:0.85em; }}
.invalidate {{ color:#fca5a5; font-size:0.85em; }}

/* DQ badges */
.dq-badge {{ font-size:0.72em; padding:2px 7px; border-radius:10px; }}
.dq-real {{ background:#14532d40; color:#86efac; border:1px solid #14532d; }}
.dq-fallback {{ background:#78350f40; color:#fcd34d; border:1px solid #78350f; }}

.dim-text {{ color:#475569; font-style:italic; }}
.footer {{ text-align:center; margin-top:28px; color:#334155; font-size:0.8em; }}

@media(max-width:600px) {{
  .header {{ flex-direction:column; }}
  .p1-row {{ flex-direction:column; gap:4px; }}
}}
</style>
</head>
<body>
<div class="container">

  <!-- ── Header ── -->
  <div class="header">
    <div style="flex-shrink:0">{ring}</div>
    <div style="flex:1">
      <div class="h-ticker">🐝 {ticker}</div>
      <div class="h-sub">Alpha Hive 深度研究 · {ts} · 7 Agent 并行 · 规则引擎</div>
      <div class="h-stats">
        <div class="hstat"><div class="hstat-v" style="color:{score_color}">{final_score:.1f}</div><div class="hstat-l">综合评分</div></div>
        <div class="hstat"><div class="hstat-v">{dir_badge}</div><div class="hstat-l">方向</div></div>
        <div class="hstat"><div class="hstat-v" style="color:#a78bfa">{conf_pct}</div><div class="hstat-l">置信度</div></div>
        <div class="hstat"><div class="hstat-v">📈{bull_c} 📉{bear_c} ➖{neut_c}</div><div class="hstat-l">Agent 投票</div></div>
      </div>
    </div>
  </div>

  <!-- ── 第1章：核心结论 ── -->
  <div class="section">
    <div class="ch-title">第 1 章 · 核心结论</div>
    <div class="summary-box">{summary_text}</div>
    {f'<div class="insight-box">💡 {key_insight}</div>' if key_insight else ""}
  </div>

  <!-- ── 第2章：五维评分表 ── -->
  <div class="section">
    <div class="ch-title">第 2 章 · 五维加权评分 &nbsp;<span style="font-size:0.85em;color:#64748b">（加权合计 = 综合分基础）</span></div>
    <table class="full-table">
      <thead><tr><th>维度</th><th>权重</th><th style="width:140px">分布</th><th>分数</th><th>加权贡献</th></tr></thead>
      <tbody>{dim_rows}</tbody>
      <tfoot><tr style="border-top:2px solid #334155">
        <td colspan="3" style="text-align:right;color:#64748b;font-size:0.85em">加权合计 →</td>
        <td colspan="2" style="color:{score_color};font-weight:700;font-size:1.1em">{weighted_sum:.2f}</td>
      </tr></tfoot>
    </table>
  </div>

  <!-- ── 第3章：7 Agent Phase 1 推理链 ── -->
  <div class="section">
    <div class="ch-title">第 3 章 · 各维度深度分析 &nbsp;<span style="font-size:0.85em;color:#64748b">（原始数据 → 异常识别 → 维度结论 → 置信度）</span></div>
    {phase1_blocks}

    <!-- ChronosBee ASCII 时间轴 -->
    <div style="margin-top:18px">
      <div style="font-size:0.82em;font-weight:600;color:#475569;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">ChronosBee · 催化剂时间轴</div>
      {timeline_html}
    </div>

    <!-- GuardBee 信号共振矩阵 -->
    <div style="margin-top:18px">
      <div style="font-size:0.82em;font-weight:600;color:#475569;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">GuardBee · 信号共振矩阵</div>
      {resonance_matrix_html}
    </div>
  </div>

  <!-- ── 第4章：投资假设 + 失效条件 ── -->
  <div class="section">
    <div class="ch-title">第 4 章 · 投资假设 + 失效条件</div>
    {_invalidation_table(final_score, direction, agents)}
  </div>

  <!-- ── 第5章：4情景推演 ── -->
  <div class="section">
    <div class="ch-title">第 5 章 · 情景推演 + 概率加权期望收益</div>
    {_scenarios(final_score, direction, ticker, agents)}
  </div>

  <!-- ── 第6章：风险雷达 ── -->
  <div class="section">
    <div class="ch-title">第 6 章 · 风险雷达</div>
    {_risk_radar(agents, qr)}
  </div>

  <!-- ── 第7章：明日追踪任务 ── -->
  <div class="section">
    <div class="ch-title">第 7 章 · 明日追踪任务</div>
    {_tracking_tasks(ticker, agents, qr)}
  </div>

  <div class="footer">Alpha Hive · {ts} · 非投资建议，仅量化规则引擎分析</div>
</div>
</body>
</html>'''
    return html


# ═══════════════════════════════════════════════════════════
# 4. 主入口
# ═══════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]

    # ── 模式1：从 JSON 文件直接渲染 HTML ──────────────────
    if args and args[0] == "--json":
        if len(args) < 2:
            p(f"{Y}用法: python3 deep_analysis.py --json data-NVDA-2026-03-11.json{R}")
            sys.exit(1)
        json_path = args[1]
        p(f"{C}{B}🐝 从 JSON 文件渲染 HTML 报告...{R}")
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ticker = data.get("ticker", "UNKNOWN").upper()
        p(f"{Y}[渲染] 生成 HTML 报告 · {ticker}{R}")
        html = generate_html(data)
        date_str = datetime.now().strftime("%Y-%m-%d")
        fname = f"deep-{ticker}-{date_str}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        p(f"{G}{B}✅ 报告已生成: {fname}{R}")
        p(f"{D}   open ~/Desktop/Alpha\\ Hive/{fname}{R}\n")
        p(f"\n{'─'*50}")
        p(f"{D}💡 提示：此报告由规则引擎生成。{R}")
        p(f"{D}   推荐方式：把 JSON 内容粘贴给 Claude 获取深度推理版本。{R}")
        return

    # ── 模式2：直接运行规则引擎 ───────────────────────────
    tickers = args if args else ["NVDA"]
    for ticker in tickers:
        ticker = ticker.upper()
        t_start = time.time()

        data = analyze(ticker)

        # 生成 HTML
        p(f"\n{Y}[Phase 3] 生成 HTML 报告...{R}")
        html = generate_html(data)

        # 保存文件
        date_str = datetime.now().strftime("%Y-%m-%d")
        fname = f"deep-{ticker}-{date_str}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)

        total = round(time.time() - t_start, 1)
        p(f"{G}{B}✅ 报告已生成: {fname}  ({total}s){R}")
        p(f"{D}   open ~/Desktop/Alpha\\ Hive/{fname}{R}")
        p(f"\n{'─'*50}")
        p(f"{D}💡 提示：此报告由规则引擎生成（无 Claude 推理）。")
        p(f"   混合模式：python3 collect_data.py {ticker} → 粘贴 JSON 给 Claude{R}\n")


if __name__ == "__main__":
    main()
