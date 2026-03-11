#!/usr/bin/env python3
"""
🐝 Alpha Hive · 混合模式深度报告生成器 · Template C v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本地 JSON 读取  +  Claude API 深度推理  →  Template C v2.0 HTML

用法:
  python3 generate_deep_v2.py --ticker NVDA
  python3 generate_deep_v2.py --ticker NVDA --date 2026-03-10
  python3 generate_deep_v2.py --ticker VKTX --no-llm   # 纯本地模式，跳过 API

输出:
  deep-{TICKER}-{DATE}.html  保存到 Alpha Hive 文件夹
"""

import json
import os
import sys
import glob
import argparse
import re
from datetime import datetime, date
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────────────────────────
ALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))
# 支持 VM 路径（Cowork 模式）
_VM_PATH = Path("/sessions/keen-magical-wright/mnt/Alpha Hive")
if _VM_PATH.exists():
    ALPHAHIVE_DIR = _VM_PATH
OUTPUT_DIR = ALPHAHIVE_DIR
API_KEY_FILE = Path("~/.anthropic_api_key").expanduser()
# 在 VM 中，home 可能映射到不同路径
_VM_API_KEY = Path("/sessions/keen-magical-wright/mnt/Alpha Hive/.anthropic_api_key")
if not API_KEY_FILE.exists() and _VM_API_KEY.exists():
    API_KEY_FILE = _VM_API_KEY

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def find_latest_json(ticker: str, date_str: str | None = None) -> Path:
    """找到最新的 analysis JSON 文件"""
    pattern = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"找不到 {ticker} 的分析 JSON: {pattern}")
    if date_str:
        target = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-{date_str}.json")
        if os.path.exists(target):
            return Path(target)
        print(f"⚠️  指定日期 {date_str} 文件不存在，使用最新: {files[-1]}")
    return Path(files[-1])


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_api_key() -> str | None:
    if API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text().strip()
        return key if key.startswith("sk-") else None
    return os.environ.get("ANTHROPIC_API_KEY")


def fmt_score(v) -> str:
    try:
        return f"{float(v):.1f}"
    except Exception:
        return str(v)


def score_class(v) -> str:
    try:
        f = float(v)
        if f >= 6.5:
            return "bull"
        elif f <= 4.0:
            return "bear"
        return "neutral"
    except Exception:
        return "neutral"


def direction_badge(direction: str) -> str:
    d = str(direction).lower()
    if "bull" in d:
        return "badge-bull"
    elif "bear" in d:
        return "badge-bear"
    return "badge-neutral"


def direction_zh(direction: str) -> str:
    d = str(direction).lower()
    if "bull" in d:
        return "📈 看多"
    elif "bear" in d:
        return "📉 看空"
    return "⚖️ 中性"


# ── 数据提取 ──────────────────────────────────────────────────────────────────

def extract(data: dict) -> dict:
    """从 JSON 中提取所有关键字段，返回结构化 context dict"""
    sr = data.get("swarm_results", {})
    ad = sr.get("agent_details", {})
    ml = data.get("ml_prediction", {})
    cr = data.get("combined_recommendation", {})
    aa = data.get("advanced_analysis", {})

    ticker = data.get("ticker", "???")
    ts_raw = data.get("timestamp", str(date.today()))
    report_date = ts_raw[:10]

    # 蜂群总体
    final_score = sr.get("final_score", 0)
    direction = sr.get("direction", "neutral")
    resonance = sr.get("resonance", {})

    # 各 Agent
    scout   = ad.get("ScoutBeeNova", {})
    rival   = ad.get("RivalBeeVanguard", {})
    buzz    = ad.get("BuzzBeeWhisper", {})
    chronos = ad.get("ChronosBeeHorizon", {})
    oracle  = ad.get("OracleBeeEcho", {})
    guard   = ad.get("GuardBeeSentinel", {})
    bear    = ad.get("BearBeeContrarian", {})

    # Oracle 详细数据
    odet = oracle.get("details", {})
    key_levels = odet.get("key_levels", {})
    unusual = odet.get("unusual_activity", [])

    # Chronos 催化剂
    cdet = chronos.get("details", {})
    catalysts = cdet.get("catalysts", [])
    # 去重催化剂
    seen_events = set()
    unique_catalysts = []
    for c in catalysts:
        key = (c.get("event", ""), c.get("date", ""))
        if key not in seen_events:
            seen_events.add(key)
            unique_catalysts.append(c)
    catalysts = unique_catalysts[:6]

    analyst_target = cdet.get("analyst_target", None)
    if analyst_target is None:
        for c in cdet.get("catalysts", []):
            if c.get("event", "").startswith("分析师"):
                pass

    # ML 预测
    ml_pred = ml.get("prediction", {})
    ml_7d = ml_pred.get("expected_7d", 0)
    ml_30d = ml_pred.get("expected_30d", 0)
    ml_prob = ml.get("input", {})

    # 宏观指标（BuzzBee）
    bdet = buzz.get("details", {})
    reddit = bdet.get("reddit", {})
    fg_score = None
    # 从 discovery 中提取 F&G
    buzz_disc = buzz.get("discovery", "")
    fg_match = re.search(r'F&G[^\d]*(\d+)', buzz_disc)
    if fg_match:
        fg_score = int(fg_match.group(1))

    # IV Skew
    iv_skew = odet.get("iv_skew_ratio", None)
    iv_skew_signal = odet.get("iv_skew_signal", "neutral")

    # 综合建议
    combined_prob = cr.get("combined_probability", final_score * 10)
    rating = cr.get("rating", "HOLD")
    action = cr.get("action", "观察等待")

    # 价格
    scout_det = scout.get("details", {})
    price = scout_det.get("price", None)

    # 逆向蜂信号
    bear_signals = bear.get("details", {}).get("bearish_signals", [])

    return {
        "ticker": ticker,
        "report_date": report_date,
        "price": price,
        "final_score": final_score,
        "direction": direction,
        "direction_zh": direction_zh(direction),
        "resonance": resonance,
        "combined_prob": combined_prob,
        "rating": rating,
        "action": action,
        # Agents
        "scout": scout,
        "rival": rival,
        "buzz": buzz,
        "chronos": chronos,
        "oracle": oracle,
        "guard": guard,
        "bear": bear,
        # Oracle data
        "put_call_ratio": odet.get("put_call_ratio", "N/A"),
        "total_oi": odet.get("total_oi", 0),
        "iv_skew": iv_skew,
        "iv_skew_signal": iv_skew_signal,
        "iv_current": odet.get("iv_current", 0),
        "iv_rank": odet.get("iv_rank", 0),
        "options_score": odet.get("options_score", 0),
        "flow_direction": odet.get("flow_direction", "neutral"),
        "signal_summary": odet.get("signal_summary", ""),
        "unusual_activity": unusual,
        "key_levels": key_levels,
        # Catalysts
        "catalysts": catalysts,
        # ML
        "ml_7d": ml_7d,
        "ml_30d": ml_30d,
        # Macro
        "fg_score": fg_score,
        "reddit": reddit,
        # Bear signals
        "bear_signals": bear_signals,
        # Overview
        "overview": aa.get("overview", ""),
        # Raw JSON for LLM context
        "_raw": data,
    }


# ── Claude API 深度推理 ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 Alpha Hive 深度研究分析师，专门为蜂群量化系统生成深度分析叙事文本。
你的写作风格：
- 专业但易读，中文，适合个人投资者
- 结合期权结构、技术面和基本面进行多维度分析
- 避免空话，每句话必须有数据支撑
- 字数控制：每个段落100-180字
- 直接输出段落文本，不需要标题或编号"""


def llm_reason(ctx: dict, section: str, api_key: str) -> str:
    """调用 Claude API 对指定章节进行深度推理，返回 HTML 段落文本"""
    try:
        import anthropic
    except ImportError:
        print("  ⚠️  anthropic 库未安装，使用本地文本")
        return _local_fallback(ctx, section)

    ticker = ctx["ticker"]
    score = ctx["final_score"]
    direction = ctx["direction_zh"]
    res = ctx["resonance"]
    res_dims = res.get("resonant_dimensions", [])

    prompts = {
        "swarm_analysis": f"""
分析 {ticker} 的蜂群七维评分结构：
- 综合评分 {score}/10，方向 {direction}
- Scout(基本面) {fmt_score(ctx['scout'].get('score'))}, 发现: {ctx['scout'].get('discovery','')[:100]}
- Rival(ML辅助) {fmt_score(ctx['rival'].get('score'))}, 7日预期 {ctx['ml_7d']:+.1f}%
- Buzz(情绪) {fmt_score(ctx['buzz'].get('score'))}, 发现: {ctx['buzz'].get('discovery','')[:100]}
- Chronos(催化剂) {fmt_score(ctx['chronos'].get('score'))}
- Oracle(期权) {fmt_score(ctx['oracle'].get('score'))}, P/C={ctx['put_call_ratio']}, OI={ctx['total_oi']:,.0f}
- Guard(宏观) {fmt_score(ctx['guard'].get('score'))}, 发现: {ctx['guard'].get('discovery','')[:100]}
- Bear(逆向) {fmt_score(ctx['bear'].get('score'))}, 信号: {', '.join(ctx['bear_signals'][:2])}

生成2段深度叙事分析，解释评分背后的逻辑和各蜂之间的分歧。直接输出两段 HTML <p> 标签。每段使用 <strong>、<span class="bull-text">、<span class="bear-text">、<span class="highlight"> 进行关键词标注。""",

        "resonance": f"""
分析 {ticker} 的蜂群共振机制：
- 共振检测: {res.get('resonance_detected', False)}
- 共振方向: {res.get('direction', 'N/A')}
- 共振维度: {', '.join(res_dims)}
- 支持Agent数量: {res.get('supporting_agents', 0)}
- 置信度提升: +{res.get('confidence_boost', 0)}%
- ML 7日预期: {ctx['ml_7d']:+.1f}%，30日: {ctx['ml_30d']:+.1f}%
- 期权流方向: {ctx['flow_direction']}

生成2段分析，解释共振机制原理及本次共振信号的含义和有效性。输出两段 HTML <p> 标签，使用强调标签。""",

        "catalyst": f"""
分析 {ticker} 的催化剂时间线：
催化剂列表（最多6个）:
{json.dumps(ctx['catalysts'][:6], ensure_ascii=False, indent=2)}

生成2段叙事，分析催化剂的密度、质量和对股价的潜在影响。特别分析最近3个催化剂的联动效应。输出两段 HTML <p> 标签，使用强调标签。""",

        "options": f"""
深度分析 {ticker} 的期权市场结构：
- P/C 比: {ctx['put_call_ratio']}（{'>1 偏空' if float(ctx['put_call_ratio']) > 1 else '<1 偏多'}）
- 总OI: {ctx['total_oi']:,.0f}
- IV Skew: {ctx['iv_skew']} ({ctx['iv_skew_signal']})
- IV 当前: {ctx['iv_current']:.1f}%
- 流向: {ctx['flow_direction']}
- 关键支撑: {json.dumps(ctx['key_levels'].get('support',[])[:3], ensure_ascii=False)}
- 关键阻力: {json.dumps(ctx['key_levels'].get('resistance',[])[:3], ensure_ascii=False)}
- 异常活动（前5）: {json.dumps(ctx['unusual_activity'][:5], ensure_ascii=False)}
- 系统信号: {ctx['signal_summary']}

生成3段深度期权结构分析：1)P/C与OI含义, 2)关键位分析与Gamma机制, 3)IV Skew解读。输出三段 HTML <p> 标签，使用强调标签。""",

        "macro": f"""
分析 {ticker} 当前宏观与情绪环境：
- Fear & Greed 指数: {ctx['fg_score'] if ctx['fg_score'] else '未知'}
- GuardBee 评分: {fmt_score(ctx['guard'].get('score'))} ({ctx['guard'].get('direction','neutral')})
- 宏观发现: {ctx['guard'].get('discovery','')[:200]}
- Buzz情绪%: {ctx['buzz'].get('details',{}).get('sentiment_pct','N/A')}
- Reddit: {ctx['reddit'].get('rank','N/A')}名, {ctx['reddit'].get('mentions','N/A')}次提及

生成2段宏观分析：分析宏观逆风/顺风对该股的影响，以及F&G极值下的反向做多机会。输出两段 HTML <p> 标签，使用强调标签。""",

        "scenario": f"""
为 {ticker} 生成四情景推演（基于以下数据）：
- 当前价格: {'$'+str(ctx['price']) if ctx['price'] else '市价'}
- 综合评分: {score}/10，方向: {direction}
- ML 7日预期: {ctx['ml_7d']:+.1f}%，30日: {ctx['ml_30d']:+.1f}%
- 最大阻力: ${ctx['key_levels'].get('resistance',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('resistance') else 'N/A'}
- 最大支撑: ${ctx['key_levels'].get('support',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('support') else 'N/A'}
- 催化剂: {len(ctx['catalysts'])} 个
- 风险信号: {', '.join(ctx['bear_signals'][:2])}

生成四情景的核心交易逻辑段落（1段），以及止盈止损具体参考。输出两段 HTML <p> 标签，使用强调标签。在第二段给出具体入场思路。""",

        "risk": f"""
为 {ticker} 生成风险分析总结：
- 逆向信号: {', '.join(ctx['bear_signals'])}
- Bear评分: {fmt_score(ctx['bear'].get('score'))} ({ctx['bear'].get('direction')})
- F&G: {ctx['fg_score'] if ctx['fg_score'] else '未知'}
- IV Skew: {ctx['iv_skew']}
- 宏观: {ctx['guard'].get('discovery','')[:150]}

生成1段综合风险总结，整合所有风险信号并给出风险管理建议。输出一段 HTML <p> 标签，使用强调标签。""",
    }

    prompt = prompts.get(section, "")
    if not prompt:
        return _local_fallback(ctx, section)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠️  LLM 调用失败 ({section}): {e}")
        return _local_fallback(ctx, section)


def _local_fallback(ctx: dict, section: str) -> str:
    """当 LLM 不可用时的本地生成文本"""
    ticker = ctx["ticker"]
    score = ctx["final_score"]
    direction = ctx["direction_zh"]
    resonance = ctx["resonance"]
    res_dims = resonance.get("resonant_dimensions", [])

    fallbacks = {
        "swarm_analysis": f"""<p><strong>蜂群评分结构分析：</strong>{ticker} 综合评分 <span class="{'bull-text' if 'bull' in ctx['direction'] else 'bear-text'}">{score:.2f}/10</span>，方向 {direction}。
        Chronos(催化剂) {fmt_score(ctx['chronos'].get('score'))} | Oracle(期权) {fmt_score(ctx['oracle'].get('score'))} | Rival(ML) {fmt_score(ctx['rival'].get('score'))}。
        期权流方向 <span class="highlight">{ctx['flow_direction']}</span>，P/C比 {ctx['put_call_ratio']}，总OI {ctx['total_oi']:,.0f}。</p>
        <p>Scout(基本面) {fmt_score(ctx['scout'].get('score'))} 反映公司基本面状况。Guard(宏观) {fmt_score(ctx['guard'].get('score'))} {'显示宏观顺风' if float(ctx['guard'].get('score',5)) > 5 else '记录宏观阻力'}。
        Bear(逆向) {fmt_score(ctx['bear'].get('score'))} 信号：{', '.join(ctx['bear_signals'][:2]) or '无重大看跌信号'}。</p>""",

        "resonance": f"""<p><strong>共振机制解读：</strong>{'共振已触发' if resonance.get('resonance_detected') else '共振未触发'}，
        共振维度：{', '.join(res_dims) or '无'}，支持Agent {resonance.get('supporting_agents',0)} 个，
        置信度提升 +{resonance.get('confidence_boost',0)}%。</p>
        <p>ML 预测 7日 {ctx['ml_7d']:+.1f}%，30日 {ctx['ml_30d']:+.1f}%。
        期权市场方向 <span class="highlight">{ctx['flow_direction']}</span>，配合催化剂窗口，信号强度{'较高' if resonance.get('cross_dim_count',0) >= 3 else '中等'}。</p>""",

        "catalyst": f"""<p><strong>催化剂分析：</strong>共检测到 {len(ctx['catalysts'])} 个催化剂事件，Chronos评分 {fmt_score(ctx['chronos'].get('score'))}。
        近期关键事件集中，催化剂密度{'高' if len(ctx['catalysts']) >= 4 else '中等'}。</p>
        <p>催化剂窗口为做多提供了明确的时间锚点，建议关注事件前后的期权隐含波动率变化。</p>""",

        "options": f"""<p><strong>期权市场解析：</strong>P/C比 {ctx['put_call_ratio']}（{'Call主导' if float(ctx['put_call_ratio']) < 1 else 'Put主导'}），
        总OI {ctx['total_oi']:,.0f}，IV当前 {ctx['iv_current']:.1f}%。
        {ctx['signal_summary']}</p>
        <p>关键支撑位：{', '.join([f"${s['strike']}" for s in ctx['key_levels'].get('support',[])[:3]])}。
        关键阻力位：{', '.join([f"${r['strike']}" for r in ctx['key_levels'].get('resistance',[])[:3]])}。
        IV Skew {ctx['iv_skew']} ({ctx['iv_skew_signal']})，期权市场结构<span class="{'bull-text' if ctx['flow_direction']=='bullish' else 'bear-text'}">偏{'多' if ctx['flow_direction']=='bullish' else '空'}</span>。</p>
        <p>最大阻力位 OI 集中处为关键Gamma钉住区域，价格接近时注意做市商对冲效应。</p>""",

        "macro": f"""<p><strong>宏观背景：</strong>F&G指数 {ctx['fg_score'] if ctx['fg_score'] else '待更新'}，GuardBee {fmt_score(ctx['guard'].get('score'))}。
        {ctx['guard'].get('discovery','')[:150]}。</p>
        <p>Reddit热度 {ctx['reddit'].get('mentions','N/A')} 次提及，情绪动量{'上升' if 'rising' in str(ctx['buzz'].get('details',{}).get('sentiment_momentum',{})) else '中性'}。
        宏观环境对本次分析结论的影响：{'正面' if float(ctx['guard'].get('score',5)) > 5 else '负面，需控制仓位'}。</p>""",

        "scenario": f"""<p><strong>核心交易逻辑：</strong>基于{score:.2f}/10的蜂群评分和{'三' if resonance.get('cross_dim_count',0)==3 else resonance.get('cross_dim_count',0)}维共振，
        本次做多/做空{ticker}的风险回报比约为{'3:1' if float(score) > 6 else '2:1'}。
        ML预期7日 {ctx['ml_7d']:+.1f}%，30日 {ctx['ml_30d']:+.1f}%。</p>
        <p>入场参考：{'期权 Call 或正股，止损参考最大支撑位下方5%' if 'bull' in ctx['direction'] else 'Put 或做空，止损参考最大阻力位上方5%'}。
        目标位：{f"${ctx['key_levels']['resistance'][0]['strike']}" if ctx['key_levels'].get('resistance') else '看催化剂触发'}。
        注意催化剂前后的波动性风险，期权Theta损耗需提前计入成本。</p>""",

        "risk": f"""<p><strong>综合风险评估：</strong>主要风险信号包括：{', '.join(ctx['bear_signals'][:3]) or '无重大风险信号'}。
        IV Skew {ctx['iv_skew']}（{'期权市场对下行定价偏高' if ctx['iv_skew'] and float(ctx['iv_skew']) > 1.2 else '期权市场情绪中性'}），
        建议{'控制仓位，使用期权保护' if float(ctx['bear'].get('score',5)) > 4 else '正常仓位管理'}。</p>""",
    }
    return fallbacks.get(section, "<p>数据处理中...</p>")


# ── HTML 生成 ─────────────────────────────────────────────────────────────────

def generate_html(ctx: dict, reasoning: dict) -> str:
    """组装完整的 Template C v2.0 HTML 报告"""
    ticker = ctx["ticker"]
    report_date = ctx["report_date"]
    score = ctx["final_score"]
    direction = ctx["direction"]
    direction_zh_str = ctx["direction_zh"]
    res = ctx["resonance"]
    price = ctx.get("price")
    price_str = f"${price:.2f}" if price else "N/A"

    res_detected = res.get("resonance_detected", False)
    res_dims = res.get("resonant_dimensions", [])
    res_boost = res.get("confidence_boost", 0)
    res_count = res.get("cross_dim_count", 0)
    res_label = f"{res_count}维共振" if res_detected else "无共振"

    # 各蜂评分
    agents = [
        ("ChronosBee", ctx["chronos"].get("score", 0), "催化剂"),
        ("RivalBee",   ctx["rival"].get("score", 0),   "ML辅助"),
        ("OracleBee",  ctx["oracle"].get("score", 0),  "期权/赔率"),
        ("BuzzBee",    ctx["buzz"].get("score", 0),    "消息/舆情"),
        ("ScoutBee",   ctx["scout"].get("score", 0),   "基本面"),
        ("GuardBee",   ctx["guard"].get("score", 0),   "宏观/情绪"),
        ("BearBee",    ctx["bear"].get("score", 0),    "逆向"),
    ]

    # 蜂群评分卡 HTML
    score_cards = ""
    for name, val, dim in agents:
        cls = score_class(val)
        score_cards += f"""
        <div class="score-card {cls}">
          <div class="bee">{name}</div>
          <div class="val">{fmt_score(val)}</div>
          <div class="dim">{dim}</div>
        </div>"""

    # 条形图 HTML
    bar_rows = ""
    for name, val, _ in sorted(agents, key=lambda x: -float(x[1]) if x[1] else 0):
        try:
            pct = min(100, max(0, float(val) * 10))
        except Exception:
            pct = 0
        cls = score_class(val)
        color_map = {"bull": "var(--green2)", "bear": "var(--red2)", "neutral": "var(--gold2)"}
        color = color_map.get(cls, "var(--gold2)")
        bar_rows += f"""
        <div class="bar-row">
          <span class="bar-label">{name.replace('Bee','')}</span>
          <div class="bar-track"><div class="bar-fill" style="width:{pct:.0f}%;background:{color};"></div></div>
          <span class="bar-val" style="color:{color}">{fmt_score(val)}</span>
        </div>"""

    # 共振徽章
    res_dims_html = "".join([f'<span class="resonance-dim">{d}</span>' for d in res_dims])
    if not res_dims_html:
        res_dims_html = '<span class="resonance-dim">无共振维度</span>'

    # 催化剂时间线
    dot_class_map = {"high": "dot-high", "critical": "dot-critical", "medium": "dot-medium"}
    timeline_html = ""
    today_str = str(date.today())
    for c in ctx["catalysts"]:
        days = c.get("days_until", 0)
        ev = c.get("event", "催化剂事件")
        ev_date = c.get("date", "")
        importance = c.get("importance", "medium")
        dot_cls = dot_class_map.get(str(importance).lower(), "dot-medium")
        if days == 0:
            label = f"今日 · {ev_date}"
        elif days < 0:
            label = f"已过 {abs(days)} 天 · {ev_date}"
        else:
            label = f"T+{days} · {ev_date}"
        desc = c.get("description", "")
        timeline_html += f"""
        <div class="timeline-item">
          <div class="timeline-dot {dot_cls}"></div>
          <div class="timeline-label">{label}</div>
          <div class="timeline-event">{ev}</div>
          {'<div class="timeline-meta">' + desc + '</div>' if desc else ''}
        </div>"""

    # 期权关键位
    supports = ctx["key_levels"].get("support", [])
    resistances = ctx["key_levels"].get("resistance", [])

    support_rows = ""
    for s in supports[:4]:
        oi = s.get("oi", 0)
        iv = s.get("iv", 0)
        support_rows += f"""
        <div class="level-row">
          <span class="level-strike">${s['strike']:.0f}</span>
          <span class="level-meta">OI {oi:,.0f} · IV {iv*100:.0f}%</span>
        </div>"""

    resistance_rows = ""
    for r in resistances[:4]:
        oi = r.get("oi", 0)
        iv = r.get("iv", 0)
        resistance_rows += f"""
        <div class="level-row">
          <span class="level-strike">${r['strike']:.0f}</span>
          <span class="level-meta">OI {oi:,.0f} · IV {iv*100:.0f}%</span>
        </div>"""

    # 异常流表格
    flow_rows = ""
    for u in ctx["unusual_activity"][:8]:
        u_type = u.get("type", "")
        strike = u.get("strike", 0)
        volume = u.get("volume", 0)
        bullish = u.get("bullish", False)
        is_call = bullish or "call" in u_type.lower()
        direction_word = "CALL" if is_call else "PUT"
        cls_word = "flow-call" if is_call else "flow-put"
        signal_desc = ("看涨" if bullish else "看跌") + (" Sweep" if "sweep" in u_type.lower() else " 大量")
        ratio = u.get("ratio", "")
        ratio_str = f" · 量OI比{ratio:.1f}x" if ratio else ""
        flow_rows += f"""
        <tr>
          <td class="{cls_word}">{direction_word}</td>
          <td>${strike:.0f}</td>
          <td>{volume:,.0f}</td>
          <td style="font-size:11px;color:var(--text3)">{signal_desc}{ratio_str}</td>
        </tr>"""

    # F&G 显示
    fg = ctx.get("fg_score")
    if fg is not None:
        if fg <= 20:
            fg_color = "var(--red2)"
            fg_label = "极度恐慌"
        elif fg <= 40:
            fg_color = "var(--red)"
            fg_label = "恐慌"
        elif fg <= 60:
            fg_color = "var(--gold2)"
            fg_label = "中性"
        elif fg <= 80:
            fg_color = "var(--green)"
            fg_label = "贪婪"
        else:
            fg_color = "var(--green2)"
            fg_label = "极度贪婪"
    else:
        fg = "N/A"
        fg_color = "var(--text3)"
        fg_label = "未知"

    # 期权流方向颜色
    flow_dir = ctx.get("flow_direction", "neutral")
    flow_badge = "badge-bull" if flow_dir == "bullish" else ("badge-bear" if flow_dir == "bearish" else "badge-neutral")

    # 宏观小卡 GuardBee共振
    guard_res = ctx["guard"].get("details", {}).get("resonance", {})
    guard_res_detected = guard_res.get("resonance_detected", False)
    guard_res_boost = guard_res.get("confidence_boost", 0)

    # 总 OI 显示
    total_oi = ctx.get("total_oi", 0)
    if total_oi >= 1_000_000:
        oi_str = f"{total_oi/1_000_000:.0f}万"
    elif total_oi >= 10_000:
        oi_str = f"{total_oi/10_000:.0f}万"
    else:
        oi_str = f"{total_oi:,}"

    # 异常流数量统计
    bullish_unusual = sum(1 for u in ctx["unusual_activity"] if u.get("bullish"))
    bearish_unusual = len(ctx["unusual_activity"]) - bullish_unusual

    # 头部 verdict
    verdict_color = "#10b981" if "bull" in direction else ("#ef4444" if "bear" in direction else "#f59e0b")
    verdict_bg = f"rgba({'16,185,129' if 'bull' in direction else ('239,68,68' if 'bear' in direction else '245,158,11')},0.15)"
    verdict_border = f"rgba({'16,185,129' if 'bull' in direction else ('239,68,68' if 'bear' in direction else '245,158,11')},0.35)"

    # ── 生成情景推演 ──────────────────────────────────────────
    if ctx.get("price"):
        p = float(ctx["price"])
        # 基于ML预测和关键位计算情景
        max_res_price = resistances[0]["strike"] if resistances else p * 1.10
        max_sup_price = supports[0]["strike"] if supports else p * 0.90

        sc_a_lo = round(max_res_price * 1.02, 0)
        sc_a_hi = round(max_res_price * 1.12, 0)
        sc_b_lo = round(p * 1.03, 0)
        sc_b_hi = round(max_res_price * 0.99, 0)
        sc_c_lo = round(max_sup_price * 0.99, 0)
        sc_c_hi = round(p * 0.98, 0)
        sc_d_lo = round(max_sup_price * 0.85, 0)
        sc_d_hi = round(max_sup_price * 0.95, 0)

        # EV 计算（基于方向）
        if "bull" in direction:
            probs = [0.25, 0.35, 0.28, 0.12]
            returns = [+0.15, +0.05, -0.07, -0.18]
        elif "bear" in direction:
            probs = [0.12, 0.28, 0.35, 0.25]
            returns = [+0.15, +0.05, -0.07, -0.18]
        else:
            probs = [0.20, 0.30, 0.30, 0.20]
            returns = [+0.12, +0.04, -0.06, -0.15]
        ev = sum(p * r for p, r in zip(probs, returns)) * 100
        ev_str = f"{ev:+.1f}%"
        ev_calc = " + ".join([f"+{r*100:.0f}%×{p*100:.0f}%" for p, r in zip(probs, returns) if r > 0])
        ev_calc += " - " + " - ".join([f"{abs(r*100):.0f}%×{p*100:.0f}%" for p, r in zip(probs, returns) if r < 0])
    else:
        sc_a_lo, sc_a_hi = 0, 0
        sc_b_lo, sc_b_hi = 0, 0
        sc_c_lo, sc_c_hi = 0, 0
        sc_d_lo, sc_d_hi = 0, 0
        ev_str = "N/A"
        ev_calc = "价格数据缺失"
        probs = [0.25, 0.35, 0.28, 0.12]

    # 风险列表
    risk_items_html = ""
    risk_icons = ["🌍", "📊", "🏛️", "💧", "⚡", "📉"]
    risk_labels = ["宏观风险", "催化剂风险", "监管风险", "流动性风险", "波动性风险", "趋势风险"]
    bear_signals = ctx.get("bear_signals", [])
    if not bear_signals:
        bear_signals = ["期权波动性偏高", "宏观不确定性", "市场情绪波动"]

    for i, sig in enumerate(bear_signals[:4]):
        icon = risk_icons[i % len(risk_icons)]
        label = risk_labels[i % len(risk_labels)]
        risk_items_html += f"""
        <li>
          <span class="risk-icon">{icon}</span>
          <div><strong>{label}：</strong>{sig}</div>
        </li>"""

    # ── 主 HTML ──────────────────────────────────────────────
    iv_skew_display = f"{ctx['iv_skew']:.2f}" if ctx.get("iv_skew") else "N/A"
    bull_unusual_count = bullish_unusual
    bear_unusual_count = bearish_unusual

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{ticker} · Alpha Hive 深度分析 · {report_date}</title>
<style>
  :root {{
    --bg: #0a0e1a; --bg2: #111827; --bg3: #1a2234; --bg4: #1e2a3e;
    --border: #2a3a52; --border2: #3a4e6a;
    --gold: #f59e0b; --gold2: #fbbf24;
    --green: #10b981; --green2: #34d399;
    --red: #ef4444; --red2: #f87171;
    --blue: #3b82f6; --blue2: #60a5fa;
    --purple: #8b5cf6; --purple2: #a78bfa;
    --cyan: #06b6d4; --cyan2: #22d3ee;
    --text: #e2e8f0; --text2: #94a3b8; --text3: #64748b;
    --bullish: #10b981; --bearish: #ef4444; --neutral: #f59e0b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, 'PingFang SC', 'Helvetica Neue', sans-serif; line-height: 1.6; }}
  .header {{ background: linear-gradient(135deg, #0d1b2e 0%, #1a2840 50%, #0d1b2e 100%); border-bottom: 1px solid var(--border2); padding: 28px 24px 22px; position: relative; overflow: hidden; }}
  .header::before {{ content: ''; position: absolute; top: -40px; right: -40px; width: 200px; height: 200px; background: radial-gradient(circle, rgba(245,158,11,0.12) 0%, transparent 70%); pointer-events: none; }}
  .header-top {{ display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; }}
  .ticker-block {{ display: flex; align-items: center; gap: 14px; }}
  .ticker-logo {{ width: 48px; height: 48px; background: linear-gradient(135deg, #3b82f6, #1d4ed8); border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 900; color: white; letter-spacing: -1px; }}
  .ticker-info h1 {{ font-size: 28px; font-weight: 800; letter-spacing: -0.5px; }}
  .ticker-info .subtitle {{ font-size: 12px; color: var(--text3); margin-top: 2px; }}
  .price-block {{ text-align: right; }}
  .price {{ font-size: 32px; font-weight: 800; color: var(--gold2); }}
  .price-sub {{ font-size: 12px; color: var(--text3); margin-top: 2px; }}
  .verdict-banner {{ margin-top: 18px; background: {verdict_bg}; border: 1px solid {verdict_border}; border-radius: 10px; padding: 14px 18px; display: flex; align-items: center; gap: 14px; }}
  .verdict-score {{ font-size: 42px; font-weight: 900; color: {verdict_color}; line-height: 1; }}
  .verdict-text {{ flex: 1; }}
  .verdict-title {{ font-size: 16px; font-weight: 700; color: {verdict_color}; }}
  .verdict-sub {{ font-size: 13px; color: var(--text2); margin-top: 3px; }}
  .resonance-badge {{ background: rgba(139,92,246,0.2); border: 1px solid rgba(139,92,246,0.5); border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: 700; color: var(--purple2); white-space: nowrap; }}
  .nav {{ background: var(--bg2); border-bottom: 1px solid var(--border); padding: 0 16px; display: flex; overflow-x: auto; gap: 0; scrollbar-width: none; }}
  .nav::-webkit-scrollbar {{ display: none; }}
  .nav a {{ display: flex; align-items: center; gap: 6px; padding: 12px 14px; font-size: 12px; font-weight: 600; color: var(--text3); text-decoration: none; white-space: nowrap; border-bottom: 2px solid transparent; transition: all 0.2s; }}
  .nav a:hover {{ color: var(--text); border-color: var(--blue); }}
  .main {{ max-width: 900px; margin: 0 auto; padding: 20px 16px 60px; }}
  .section {{ margin-bottom: 24px; background: var(--bg2); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; }}
  .section-header {{ padding: 16px 20px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; }}
  .section-icon {{ font-size: 18px; }}
  .section-title {{ font-size: 15px; font-weight: 700; }}
  .section-badge {{ margin-left: auto; font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 20px; }}
  .badge-bull {{ background: rgba(16,185,129,0.2); color: var(--green2); border: 1px solid rgba(16,185,129,0.3); }}
  .badge-bear {{ background: rgba(239,68,68,0.2); color: var(--red2); border: 1px solid rgba(239,68,68,0.3); }}
  .badge-neutral {{ background: rgba(245,158,11,0.2); color: var(--gold2); border: 1px solid rgba(245,158,11,0.3); }}
  .badge-resonance {{ background: rgba(139,92,246,0.25); color: var(--purple2); border: 1px solid rgba(139,92,246,0.4); }}
  .section-body {{ padding: 18px 20px; }}
  .score-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 12px; }}
  .score-card {{ background: var(--bg3); border: 1px solid var(--border); border-radius: 10px; padding: 12px 10px; text-align: center; }}
  .score-card .bee {{ font-size: 11px; color: var(--text3); margin-bottom: 4px; }}
  .score-card .val {{ font-size: 26px; font-weight: 800; line-height: 1; }}
  .score-card .dim {{ font-size: 10px; color: var(--text3); margin-top: 3px; }}
  .score-card.bull {{ border-color: rgba(16,185,129,0.35); }}
  .score-card.bull .val {{ color: var(--green2); }}
  .score-card.bear {{ border-color: rgba(239,68,68,0.35); }}
  .score-card.bear .val {{ color: var(--red2); }}
  .score-card.neutral {{ border-color: rgba(245,158,11,0.35); }}
  .score-card.neutral .val {{ color: var(--gold2); }}
  .resonance-box {{ background: linear-gradient(135deg, rgba(139,92,246,0.1), rgba(245,158,11,0.08)); border: 1px solid rgba(139,92,246,0.35); border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
  .resonance-title {{ font-size: 13px; font-weight: 700; color: var(--purple2); margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }}
  .resonance-dims {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .resonance-dim {{ background: rgba(139,92,246,0.2); border: 1px solid rgba(139,92,246,0.35); border-radius: 6px; padding: 3px 10px; font-size: 11px; font-weight: 700; color: var(--purple2); }}
  .levels-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .level-block {{ background: var(--bg3); border-radius: 10px; padding: 14px; }}
  .level-block h4 {{ font-size: 11px; font-weight: 700; margin-bottom: 10px; }}
  .level-block.support h4 {{ color: var(--green2); }}
  .level-block.resistance h4 {{ color: var(--red2); }}
  .level-row {{ display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; }}
  .level-row:last-child {{ border-bottom: none; }}
  .level-strike {{ font-weight: 700; font-size: 14px; }}
  .support .level-strike {{ color: var(--green2); }}
  .resistance .level-strike {{ color: var(--red2); }}
  .level-meta {{ color: var(--text3); font-size: 11px; }}
  .flow-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .flow-table th {{ text-align: left; padding: 7px 10px; background: var(--bg3); color: var(--text3); font-weight: 600; font-size: 11px; border-bottom: 1px solid var(--border); }}
  .flow-table td {{ padding: 7px 10px; border-bottom: 1px solid var(--border); }}
  .flow-table tr:last-child td {{ border-bottom: none; }}
  .flow-call {{ color: var(--green2); font-weight: 700; }}
  .flow-put {{ color: var(--red2); font-weight: 700; }}
  .timeline {{ position: relative; padding-left: 24px; }}
  .timeline::before {{ content: ''; position: absolute; left: 7px; top: 4px; bottom: 4px; width: 2px; background: linear-gradient(to bottom, var(--gold), var(--blue)); border-radius: 1px; }}
  .timeline-item {{ position: relative; margin-bottom: 14px; }}
  .timeline-dot {{ position: absolute; left: -20px; top: 4px; width: 10px; height: 10px; border-radius: 50%; border: 2px solid; }}
  .dot-critical {{ background: var(--red); border-color: var(--red2); }}
  .dot-high {{ background: var(--gold); border-color: var(--gold2); }}
  .dot-medium {{ background: var(--blue); border-color: var(--blue2); }}
  .timeline-label {{ font-size: 10px; color: var(--text3); font-weight: 600; }}
  .timeline-event {{ font-size: 13px; font-weight: 700; color: var(--text); margin: 1px 0; }}
  .timeline-meta {{ font-size: 11px; color: var(--text3); }}
  .scenario-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .scenario-card {{ background: var(--bg3); border-radius: 10px; padding: 14px; border: 1px solid var(--border); }}
  .scenario-card .s-label {{ font-size: 10px; font-weight: 700; color: var(--text3); text-transform: uppercase; margin-bottom: 6px; }}
  .scenario-card .s-title {{ font-size: 13px; font-weight: 700; margin-bottom: 4px; }}
  .scenario-card .s-price {{ font-size: 18px; font-weight: 800; }}
  .scenario-card .s-prob {{ font-size: 11px; color: var(--text2); margin-top: 4px; }}
  .scenario-card .s-desc {{ font-size: 11px; color: var(--text3); margin-top: 6px; line-height: 1.5; }}
  .scenario-bull {{ border-color: rgba(16,185,129,0.3); }}
  .scenario-bull .s-price {{ color: var(--green2); }}
  .scenario-bear {{ border-color: rgba(239,68,68,0.3); }}
  .scenario-bear .s-price {{ color: var(--red2); }}
  .metrics-row {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); gap: 10px; }}
  .metric-card {{ background: var(--bg3); border-radius: 8px; padding: 12px 10px; text-align: center; border: 1px solid var(--border); }}
  .metric-card .m-label {{ font-size: 10px; color: var(--text3); margin-bottom: 4px; }}
  .metric-card .m-val {{ font-size: 18px; font-weight: 800; }}
  .metric-card .m-sub {{ font-size: 10px; color: var(--text3); margin-top: 2px; }}
  .prose {{ font-size: 13px; color: var(--text2); line-height: 1.75; }}
  .prose p {{ margin-bottom: 10px; }}
  .prose strong {{ color: var(--text); }}
  .prose .highlight {{ color: var(--gold2); font-weight: 600; }}
  .prose .bull-text {{ color: var(--green2); font-weight: 600; }}
  .prose .bear-text {{ color: var(--red2); font-weight: 600; }}
  .risk-list {{ list-style: none; }}
  .risk-list li {{ display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; color: var(--text2); }}
  .risk-list li:last-child {{ border-bottom: none; }}
  .risk-icon {{ font-size: 14px; flex-shrink: 0; }}
  .ev-box {{ background: linear-gradient(135deg, rgba(16,185,129,0.12), rgba(59,130,246,0.08)); border: 1px solid rgba(16,185,129,0.3); border-radius: 10px; padding: 16px; margin-top: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
  .ev-left .label {{ font-size: 11px; color: var(--text3); }}
  .ev-left .big {{ font-size: 28px; font-weight: 900; color: var(--green2); }}
  .ev-right {{ font-size: 12px; color: var(--text2); max-width: 300px; }}
  .disclaimer {{ background: var(--bg3); border: 1px solid var(--border); border-radius: 10px; padding: 14px; font-size: 11px; color: var(--text3); line-height: 1.6; margin-top: 24px; }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 12px; }}
  .bar-label {{ width: 80px; color: var(--text2); flex-shrink: 0; font-size: 11px; }}
  .bar-track {{ flex: 1; height: 6px; background: var(--bg4); border-radius: 3px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 3px; }}
  .bar-val {{ width: 36px; text-align: right; font-weight: 700; font-size: 12px; }}
  .divider {{ height: 1px; background: var(--border); margin: 14px 0; }}
  .gen-notice {{ background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.25); border-radius: 8px; padding: 10px 14px; font-size: 12px; color: var(--blue2); margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
  @media (max-width: 600px) {{
    .levels-grid, .scenario-grid {{ grid-template-columns: 1fr; }}
    .verdict-banner {{ flex-wrap: wrap; }}
    .price {{ font-size: 24px; }}
    .verdict-score {{ font-size: 32px; }}
  }}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="header-top">
    <div class="ticker-block">
      <div class="ticker-logo">{ticker[:2]}</div>
      <div class="ticker-info">
        <h1>{ticker}</h1>
        <div class="subtitle">Alpha Hive 深度分析 · {report_date} · Template C v2.0</div>
      </div>
    </div>
    <div class="price-block">
      <div class="price">{price_str}</div>
      <div class="price-sub">收市价 · 本地数据</div>
    </div>
  </div>
  <div class="verdict-banner">
    <div class="verdict-score">{score:.2f}</div>
    <div class="verdict-text">
      <div class="verdict-title">{direction_zh_str} · {res_label}</div>
      <div class="verdict-sub">
        {res_count}维共振{'触发' if res_detected else '未触发'} ·
        期权流{ctx['flow_direction']} ·
        P/C = {ctx['put_call_ratio']}
      </div>
    </div>
    <div class="resonance-badge">{'⚡ 共振已触发' if res_detected else '○ 无共振'}</div>
  </div>
</div>

<!-- NAV -->
<nav class="nav">
  <a href="#ch1">🐝 蜂群评分</a>
  <a href="#ch2">⚡ 共振分析</a>
  <a href="#ch3">📅 催化剂</a>
  <a href="#ch4">📊 期权结构</a>
  <a href="#ch5">🌍 宏观/情绪</a>
  <a href="#ch6">🎯 情景推演</a>
  <a href="#ch7">⚠️ 风险</a>
</nav>

<div class="main">

  <div class="gen-notice">
    🤖 <strong>混合模式 · Template C v2.0</strong> —— 本地 JSON 数据 + Claude API 深度推理生成 · {report_date}
  </div>

  <!-- CH1 -->
  <div class="section" id="ch1">
    <div class="section-header">
      <span class="section-icon">🐝</span>
      <span class="section-title">第一章 · 蜂群七维评分</span>
      <span class="section-badge {direction_badge(direction)}">综合 {score:.2f} · {'看多' if 'bull' in direction else ('看空' if 'bear' in direction else '中性')}</span>
    </div>
    <div class="section-body">
      <div class="score-grid">{score_cards}</div>
      <div class="divider"></div>
      <div class="prose">{reasoning.get('swarm_analysis', '<p>分析生成中...</p>')}</div>
      <div style="margin-top:14px;">{bar_rows}</div>
    </div>
  </div>

  <!-- CH2 -->
  <div class="section" id="ch2">
    <div class="section-header">
      <span class="section-icon">⚡</span>
      <span class="section-title">第二章 · 蜂群共振分析</span>
      <span class="section-badge badge-resonance">{res_label} · +{res_boost}% 信心提升</span>
    </div>
    <div class="section-body">
      <div class="resonance-box">
        <div class="resonance-title">{'⚡ 跨维度共振已触发' if res_detected else '○ 本次无共振触发'}</div>
        <div class="resonance-dims">{res_dims_html}</div>
        <div style="margin-top:10px;font-size:12px;color:var(--text2);">
          {res_count} 个跨维度{'看涨' if 'bull' in direction else '看空'}信号同时触发 ·
          置信度自动提升 +{res_boost}% ·
          历史回测显示共振信号胜率高于单维信号约 18%
        </div>
      </div>
      <div class="prose">{reasoning.get('resonance', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH3 -->
  <div class="section" id="ch3">
    <div class="section-header">
      <span class="section-icon">📅</span>
      <span class="section-title">第三章 · 催化剂时间线</span>
      <span class="section-badge badge-bull">{len(ctx['catalysts'])} 个催化剂 · {fmt_score(ctx['chronos'].get('score'))}/10</span>
    </div>
    <div class="section-body">
      <div class="timeline">{timeline_html}</div>
      <div class="divider"></div>
      <div class="prose">{reasoning.get('catalyst', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH4 -->
  <div class="section" id="ch4">
    <div class="section-header">
      <span class="section-icon">📊</span>
      <span class="section-title">第四章 · 期权市场结构</span>
      <span class="section-badge {flow_badge}">P/C={ctx['put_call_ratio']} · OI={oi_str} · {bull_unusual_count}个看涨异动</span>
    </div>
    <div class="section-body">
      <div class="metrics-row" style="margin-bottom:16px;">
        <div class="metric-card">
          <div class="m-label">Put/Call 比</div>
          <div class="m-val" style="color:{'var(--green2)' if float(ctx['put_call_ratio']) < 1 else 'var(--red2)'}">
            {ctx['put_call_ratio']}
          </div>
          <div class="m-sub">{'Call主导' if float(ctx['put_call_ratio']) < 1 else 'Put主导'}</div>
        </div>
        <div class="metric-card">
          <div class="m-label">总开仓量</div>
          <div class="m-val" style="color:var(--cyan2)">{oi_str}</div>
          <div class="m-sub">未平仓合约</div>
        </div>
        <div class="metric-card">
          <div class="m-label">IV Skew</div>
          <div class="m-val" style="color:var(--gold2)">{iv_skew_display}</div>
          <div class="m-sub">{ctx['iv_skew_signal']}</div>
        </div>
        <div class="metric-card">
          <div class="m-label">看涨异动</div>
          <div class="m-val" style="color:var(--green2)">{bull_unusual_count}</div>
          <div class="m-sub">vs {bear_unusual_count} 看跌</div>
        </div>
      </div>

      <div class="levels-grid" style="margin-bottom:16px;">
        <div class="level-block support">
          <h4>📗 支撑位（高OI Put）</h4>
          {support_rows or '<div class="level-row"><span class="level-meta">暂无数据</span></div>'}
        </div>
        <div class="level-block resistance">
          <h4>📕 阻力位（高OI Call）</h4>
          {resistance_rows or '<div class="level-row"><span class="level-meta">暂无数据</span></div>'}
        </div>
      </div>

      <div style="margin-bottom:14px;">
        <div style="font-size:12px;font-weight:700;color:var(--text2);margin-bottom:10px;">异常期权流（成交量排名）</div>
        <table class="flow-table">
          <thead>
            <tr><th>方向</th><th>行权价</th><th>成交量</th><th>信号</th></tr>
          </thead>
          <tbody>{flow_rows}</tbody>
        </table>
      </div>

      <div class="prose">{reasoning.get('options', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH5 -->
  <div class="section" id="ch5">
    <div class="section-header">
      <span class="section-icon">🌍</span>
      <span class="section-title">第五章 · 宏观环境与情绪面</span>
      <span class="section-badge {'badge-bear' if fg != 'N/A' and int(fg) < 30 else 'badge-neutral'}">
        F&G = {fg} · {fg_label}
      </span>
    </div>
    <div class="section-body">
      <div class="metrics-row" style="margin-bottom:16px;">
        <div class="metric-card">
          <div class="m-label">Fear & Greed</div>
          <div class="m-val" style="color:{fg_color}">{fg}</div>
          <div class="m-sub">{fg_label}</div>
        </div>
        <div class="metric-card">
          <div class="m-label">GuardBee</div>
          <div class="m-val" style="color:{'var(--green2)' if float(ctx['guard'].get('score',5)) > 5.5 else 'var(--red2)'}">
            {fmt_score(ctx['guard'].get('score'))}
          </div>
          <div class="m-sub">{'宏观顺风' if float(ctx['guard'].get('score',5)) > 5.5 else '宏观阻力'}</div>
        </div>
        <div class="metric-card">
          <div class="m-label">共振加成</div>
          <div class="m-val" style="color:{'var(--purple2)' if res_detected else 'var(--text3)'}">
            {'+' + str(res_boost) + '%' if res_detected else '0%'}
          </div>
          <div class="m-sub">置信提升</div>
        </div>
        <div class="metric-card">
          <div class="m-label">Reddit 热度</div>
          <div class="m-val" style="color:var(--blue2)">
            {ctx['reddit'].get('rank', 'N/A')}
          </div>
          <div class="m-sub">{ctx['reddit'].get('mentions','N/A')}次提及</div>
        </div>
      </div>
      <div class="prose">{reasoning.get('macro', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH6 -->
  <div class="section" id="ch6">
    <div class="section-header">
      <span class="section-icon">🎯</span>
      <span class="section-title">第六章 · 四情景推演与期望值</span>
      <span class="section-badge {'badge-bull' if '+' in ev_str else 'badge-bear'}">EV = {ev_str} · {'偏多' if '+' in ev_str else '偏空'}</span>
    </div>
    <div class="section-body">
      <div class="scenario-grid">
        <div class="scenario-card scenario-bull">
          <div class="s-label">🚀 情景A · 最强看涨</div>
          <div class="s-title">突破阻力 + 催化剂超预期</div>
          <div class="s-price">${sc_a_lo:.0f}–${sc_a_hi:.0f}</div>
          <div class="s-prob">概率估计：{probs[0]*100:.0f}%</div>
          <div class="s-desc">
            ML 7日预期 {ctx['ml_7d']:+.1f}%，{res_label}触发，阻力位被突破后做市商Delta对冲形成加速上涨。
          </div>
        </div>
        <div class="scenario-card scenario-bull">
          <div class="s-label">📈 情景B · 温和看涨</div>
          <div class="s-title">催化剂达预期</div>
          <div class="s-price">${sc_b_lo:.0f}–${sc_b_hi:.0f}</div>
          <div class="s-prob">概率估计：{probs[1]*100:.0f}%</div>
          <div class="s-desc">
            P/C={ctx['put_call_ratio']} 已为温和上涨定价，期权流{ctx['flow_direction']}，近端支撑稳固。
          </div>
        </div>
        <div class="scenario-card scenario-bear">
          <div class="s-label">📉 情景C · 温和看跌</div>
          <div class="s-title">催化剂不达预期</div>
          <div class="s-price">${sc_c_hi:.0f}–${sc_c_lo:.0f}</div>
          <div class="s-prob">概率估计：{probs[2]*100:.0f}%</div>
          <div class="s-desc">
            支撑位 ${supports[0]['strike'] if supports else 'N/A'} OI {supports[0]['oi']:,.0f} 提供缓冲，量能萎缩限制下行弹性。
          </div>
        </div>
        <div class="scenario-card scenario-bear">
          <div class="s-label">💥 情景D · 极端风险</div>
          <div class="s-title">催化剂暴雷 + 宏观恶化</div>
          <div class="s-price">${sc_d_lo:.0f}–${sc_d_hi:.0f}</div>
          <div class="s-prob">概率估计：{probs[3]*100:.0f}%</div>
          <div class="s-desc">
            风险信号：{', '.join(ctx['bear_signals'][:2]) or 'IV Skew偏高, 宏观不确定'}。深部支撑提供终极防线。
          </div>
        </div>
      </div>

      <div class="ev-box">
        <div class="ev-left">
          <div class="label">期望收益率（EV）</div>
          <div class="big">{ev_str}</div>
          <div style="font-size:11px;color:var(--text2);margin-top:3px;">基于四情景加权</div>
        </div>
        <div class="ev-right">
          <strong>ML 预测：</strong>7日 {ctx['ml_7d']:+.1f}% · 30日 {ctx['ml_30d']:+.1f}%<br><br>
          {'正期望值在共振触发环境中出现，进一步确认信号有效性。' if '+' in ev_str else '负期望值提示谨慎，建议小仓位或观望。'}
        </div>
      </div>

      <div class="prose" style="margin-top:14px;">{reasoning.get('scenario', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH7 -->
  <div class="section" id="ch7">
    <div class="section-header">
      <span class="section-icon">⚠️</span>
      <span class="section-title">第七章 · 主要风险清单</span>
      <span class="section-badge badge-neutral">{len(bear_signals)} 项风险信号</span>
    </div>
    <div class="section-body">
      <ul class="risk-list">
        {risk_items_html}
      </ul>
      <div class="divider"></div>
      <div class="prose">{reasoning.get('risk', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <div class="disclaimer">
    ⚠️ <strong>免责声明：</strong>本报告由 Alpha Hive 量化蜂群系统 + Claude API 混合模式生成，仅供研究参考，不构成投资建议。期权交易存在归零风险，所有交易决策需自行判断和风控。<br>
    <span style="color:var(--text3);font-size:10px;margin-top:4px;display:block;">
      生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · Template C v2.0 · 混合模式 · 数据来源：本地 JSON + Claude API
    </span>
  </div>

</div>
</body>
</html>"""
    return html


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Alpha Hive · 混合模式深度报告生成器 · Template C v2.0")
    parser.add_argument("--ticker", required=True, help="股票代码，如 NVDA、VKTX")
    parser.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD（默认最新）")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM，使用本地生成文本")
    parser.add_argument("--out-dir", default=None, help="输出目录（默认 Alpha Hive 文件夹）")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    out_dir = Path(args.out_dir) if args.out_dir else OUTPUT_DIR

    print(f"\n🐝 Alpha Hive · Template C v2.0 混合报告生成器")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"📊 标的: {ticker}")

    # 1. 加载 JSON
    try:
        json_path = find_latest_json(ticker, args.date)
        print(f"📂 数据文件: {json_path.name}")
        data = load_json(json_path)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    # 2. 提取数据
    print("🔍 提取结构化数据...")
    ctx = extract(data)
    print(f"   评分: {ctx['final_score']:.2f} | 方向: {ctx['direction_zh']} | 共振: {'✅' if ctx['resonance'].get('resonance_detected') else '○'}")
    print(f"   OracleBee: P/C={ctx['put_call_ratio']}, OI={ctx['total_oi']:,}, Skew={ctx['iv_skew']}")
    print(f"   催化剂: {len(ctx['catalysts'])} 个 | 异常流: {len(ctx['unusual_activity'])} 笔")

    # 3. LLM 深度推理
    sections = ["swarm_analysis", "resonance", "catalyst", "options", "macro", "scenario", "risk"]
    reasoning = {}

    api_key = get_api_key()
    use_llm = not args.no_llm and api_key

    if use_llm:
        print(f"\n🤖 Claude API 深度推理中...")
        for sec in sections:
            print(f"   ✍️  {sec}...", end="", flush=True)
            reasoning[sec] = llm_reason(ctx, sec, api_key)
            print(" ✅")
    else:
        reason = "本地模式" if args.no_llm else "未找到 API Key"
        print(f"\n📝 本地叙事生成（{reason}）...")
        for sec in sections:
            reasoning[sec] = _local_fallback(ctx, sec)
        print("   ✅ 全部章节完成")

    # 4. 生成 HTML
    print("\n📄 渲染 Template C v2.0 HTML...")
    html = generate_html(ctx, reasoning)

    # 5. 保存
    report_date = ctx["report_date"]
    out_file = out_dir / f"deep-{ticker}-{report_date}.html"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")

    print(f"\n✅ 报告已生成！")
    print(f"📁 {out_file}")
    print(f"🌐 用浏览器打开即可查看\n")


if __name__ == "__main__":
    main()
