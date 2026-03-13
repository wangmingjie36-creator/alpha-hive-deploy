#!/usr/bin/env python3
"""
🐝 Alpha Hive · 混合模式深度报告生成器 · Template C v3.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本地 JSON 读取  +  Claude API 深度推理  →  Template C HTML

用法:
  python3 generate_deep_v2.py --ticker NVDA
  python3 generate_deep_v2.py --ticker NVDA --date 2026-03-10
  python3 generate_deep_v2.py --ticker VKTX --no-llm   # 纯本地模式，跳过 API

输出:
  deep-{TICKER}-{DATE}.html  保存到 Alpha Hive 文件夹

版本历史:
  v3.0  2026-03-12  Template C v3.0 基础版本，7章结构 + LLM推理
  v3.1  2026-03-13  ① Delta分析(昨日JSON对比上下文) ② 链式推理(delta注入swarm/macro prompt)
                    ③ 昨日涨跌箭头(score-card Δ显示) ④ T-7预测闭环widget ⑤ 4-tuple agents修复
  v3.2  2026-03-13  SYSTEM_PROMPT全面升级：期权阈值体系/信号优先级/冲突处理规则/评分解读标准
  v3.3  2026-03-13  真正两步链式推理：Step1分析框架(STEP1_SYSTEM+step1_prompts) → Step2叙事写作
  v3.4  2026-03-13  跨章上下文：extract_thesis()+master_thesis注入CH6/CH7 step1&step2 prompt
  v3.5  2026-03-13  矛盾检测：detect_conflicts()自动检测5类矛盾，强制注入swarm/options prompt
"""

VERSION = "3.5"

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


def find_prev_json(ticker: str, current_path: Path, days_back: int = 1) -> Path | None:
    """找到当前文件之前第 N 个交易日的 JSON"""
    pattern = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-*.json")
    files = sorted(glob.glob(pattern))
    try:
        idx = files.index(str(current_path))
    except ValueError:
        idx = len(files)
    target_idx = idx - days_back
    if target_idx >= 0:
        return Path(files[target_idx])
    return None


def extract_thesis(html: str) -> str:
    """从 CH1 HTML 叙事中提取核心论点（首段第一句话，去除HTML标签）"""
    text = re.sub(r'<[^>]+>', '', html).strip()
    m = re.search(r'^(.+?[。！？.!?])', text)
    thesis = m.group(1).strip() if m else text[:100].strip()
    return thesis


def detect_conflicts(ctx: dict) -> list[str]:
    """自动检测蜂群信号中的矛盾对，返回中文描述列表（空列表=无矛盾）"""
    conflicts = []
    score = ctx.get("final_score", 5.0)
    direction = ctx.get("direction", "neutral")
    ml_7d = ctx.get("ml_7d", 0.0)
    flow = ctx.get("flow_direction", "").lower()
    res_detected = ctx.get("resonance", {}).get("resonance_detected", False)

    try:
        pc = float(ctx.get("put_call_ratio", 1.0))
    except (ValueError, TypeError):
        pc = 1.0
    try:
        iv_skew = float(ctx.get("iv_skew", 1.0))
    except (ValueError, TypeError):
        iv_skew = 1.0

    # 1. P/C vs 综合评分
    if pc < 0.80 and score < 4.5:
        conflicts.append(
            f"⚠️ 期权P/C={pc:.2f}（<0.80偏多）↔ 综合评分{score:.1f}（<4.5偏空）"
            f"：smart money买Call但蜂群系统看空，需判断谁领先"
        )
    if pc > 1.20 and score > 6.5:
        conflicts.append(
            f"⚠️ 期权P/C={pc:.2f}（>1.20偏空）↔ 综合评分{score:.1f}（>6.5偏多）"
            f"：机构大量买Put对冲但蜂群系统看多，需判断是对冲还是预警"
        )

    # 2. ML预期 vs 蜂群方向
    if ml_7d > 3.0 and "bear" in direction:
        conflicts.append(
            f"⚠️ ML量化预期7日+{ml_7d:.1f}%（看涨）↔ 蜂群方向看空"
            f"：量化模型与多维信号分歧，关注期权流方向作为裁判"
        )
    if ml_7d < -3.0 and "bull" in direction:
        conflicts.append(
            f"⚠️ ML量化预期7日{ml_7d:.1f}%（看跌）↔ 蜂群方向看多"
            f"：量化模型与多维信号分歧，关注是否有基本面支撑"
        )

    # 3. IV Skew vs P/C（期权内部矛盾）
    if iv_skew > 1.15 and pc < 0.85:
        conflicts.append(
            f"⚠️ IV Skew={iv_skew:.2f}（>1.15下行恐慌溢价）↔ P/C={pc:.2f}（<0.85 Call主导）"
            f"：期权内部矛盾，保护性Put贵但同时也在买Call，可能是双向押注"
        )

    # 4. 期权流向 vs 综合评分
    if "bull" in flow and score < 4.5:
        conflicts.append(
            f"⚠️ 期权流向偏多（{ctx.get('flow_direction','')}）↔ 综合评分{score:.1f}偏空"
            f"：期权流显示买方看多但蜂群量化偏空，注意流向是否为短线投机"
        )
    if "bear" in flow and score > 6.5:
        conflicts.append(
            f"⚠️ 期权流向偏空（{ctx.get('flow_direction','')}）↔ 综合评分{score:.1f}偏多"
            f"：期权流偏空头对冲但蜂群量化看多，需判断是风险管理还是看空押注"
        )

    # 5. 共振激活 vs 中性评分
    if res_detected and 4.0 < score < 6.0:
        res_dir = ctx.get("resonance", {}).get("direction", "N/A")
        conflicts.append(
            f"⚠️ 蜂群共振激活（方向:{res_dir}）↔ 综合评分{score:.1f}处于中性区间（4-6）"
            f"：共振方向与评分未形成合力，需核实共振维度是否与期权流一致"
        )

    return conflicts


def extract_simple(data: dict) -> dict:
    """轻量提取，用于昨日/T-7 对比，只取关键数值"""
    sr  = data.get("swarm_results", {})
    ad  = sr.get("agent_details", {})
    odet = ad.get("OracleBeeEcho", {}).get("details", {})
    scout_det = ad.get("ScoutBeeNova", {}).get("details", {})
    price = data.get("closing_price") or scout_det.get("price", None)
    def _s(key): return float(ad.get(key, {}).get("score", 0) or 0)
    return {
        "final_score": float(sr.get("final_score", 0) or 0),
        "direction":   sr.get("direction", "neutral"),
        "price":       float(price) if price else None,
        "chronos": _s("ChronosBeeHorizon"),
        "rival":   _s("RivalBeeVanguard"),
        "oracle":  _s("OracleBeeEcho"),
        "buzz":    _s("BuzzBeeWhisper"),
        "scout":   _s("ScoutBeeNova"),
        "guard":   _s("GuardBeeSentinel"),
        "bear":    _s("BearBeeContrarian"),
        "put_call_ratio": odet.get("put_call_ratio", None),
        "iv_skew":        odet.get("iv_skew_ratio", None),
    }


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

    # IV Skew + OTM IV + IV Rank
    iv_skew = odet.get("iv_skew_ratio", None)
    iv_skew_signal = odet.get("iv_skew_signal", "neutral")
    iv_skew_detail = odet.get("iv_skew_detail", {})
    otm_put_iv  = iv_skew_detail.get("otm_put_iv", None)   # e.g. 33.33
    otm_call_iv = iv_skew_detail.get("otm_call_iv", None)  # e.g. 26.67
    iv_percentile = odet.get("iv_percentile", None)        # 0–100 百分位

    # 综合建议
    combined_prob = cr.get("combined_probability", final_score * 10)
    rating = cr.get("rating", "HOLD")
    action = cr.get("action", "观察等待")

    # 价格：优先使用顶层 closing_price（收盘后更新），其次用 ScoutBee 扫描时价格
    scout_det = scout.get("details", {})
    price = data.get("closing_price") or scout_det.get("price", None)
    price_is_close = bool(data.get("closing_price"))

    # 历史准确率（从 bear_discovery 字符串解析）
    bear_disc = bear.get("discovery", "") or ""
    hist_acc_m   = re.search(r'T\+7准确率(\d+)%', bear_disc)
    hist_n_m     = re.search(r'\((\d+)次', bear_disc)
    hist_accuracy = int(hist_acc_m.group(1)) if hist_acc_m else None
    hist_sample_n = int(hist_n_m.group(1)) if hist_n_m else None

    # 逆向蜂信号
    bear_signals = bear.get("details", {}).get("bearish_signals", [])

    return {
        "ticker": ticker,
        "report_date": report_date,
        "price": price,
        "price_is_close": price_is_close,
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
        "otm_put_iv": otm_put_iv,
        "otm_call_iv": otm_call_iv,
        "iv_current": odet.get("iv_current", 0),
        "iv_rank": odet.get("iv_rank", 0),
        "iv_percentile": iv_percentile,
        "options_score": odet.get("options_score", 0),
        "flow_direction": odet.get("flow_direction", "neutral"),
        "signal_summary": odet.get("signal_summary", ""),
        "unusual_activity": unusual,
        "expiration_dates": odet.get("expiration_dates", []),
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
        # Historical accuracy
        "hist_accuracy": hist_accuracy,
        "hist_sample_n": hist_sample_n,
        # Overview
        "overview": aa.get("overview", ""),
        # Raw JSON for LLM context
        "_raw": data,
    }


# ── Claude API 深度推理 ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 Alpha Hive 首席期权策略师，拥有十年衍生品交易经验，专门解读蜂群量化系统的多维信号并生成可操作的深度分析。

━━ 期权数据解读标准 ━━
P/C比阈值：<0.7 强多头情绪 | 0.7-0.9 偏多 | 0.9-1.1 中性 | 1.1-1.3 偏空 | >1.3 强空头对冲
IV Skew（Put/Call隐波比）：>1.15 市场为下行大幅对冲（恐慌溢价）| 0.95-1.05 平衡 | <0.90 上行投机热
IV Rank：>80 期权昂贵适合卖方策略 | <20 期权便宜适合方向性买入 | 50左右中性
OI集中度：单行权价OI超过总OI的10% → 形成Gamma墙，到期前做市商Delta对冲产生磁吸或压制
异常流解读：Call Sweep主动买入=机构方向性押注看多 | Put Sweep主动买入=对冲需求或做空 | 卖Call=限制上行空间

━━ 信号优先级（从高到低）━━
① 期权异常流（机构smart money，最难伪造）
② ML量化预测（RivalBee，基于历史模型）
③ 蜂群共振（多维度信号同向，置信度加成）
④ 技术面/价格关键位（支撑阻力的OI验证）
⑤ 基本面/催化剂/宏观情绪（辅助背景）

━━ 信号冲突处理规则 ━━
当期权流看多但综合评分偏空：优先相信期权流（smart money通常领先），在叙事中明确点出此矛盾并解释
当ML看涨但期权Skew偏高（>1.1）：期权市场在为下行对冲，谨慎对待ML信号，需说明对冲需求来源
当共振信号存在但评分中性：共振提升置信度，但需核实共振维度与期权流方向是否一致
任何矛盾都必须在分析中明确表态，不得含糊绕过

━━ 蜂群评分解读 ━━
≥7.5 强看多（可考虑进攻性多头仓位）| 6.5-7.4 温和看多 | 4.6-6.4 中性等待
3.6-4.5 温和看空 | ≤3.5 强看空（警惕下行风险）

━━ 写作要求 ━━
- 每章必须在第一句点出本章最关键的1个信号及其含义
- 所有判断必须引用具体数字（不说"较高"，要说"P/C=0.69低于中性阈值0.9"）
- 结论必须可操作：明确看多/看空/等待，并给出触发改变判断的条件
- 字数：每段100-180字，直接输出HTML <p>段落，使用<strong>/<span class="bull-text">/<span class="bear-text">/<span class="highlight">标注关键词
- 语气：专业分析师口吻，不说废话，不重复数据，每句都要推进论点"""

# Step 1：分析框架专用系统提示（不写HTML，只做结构化推理）
STEP1_SYSTEM = """你是量化信号分析引擎。你的唯一任务是在写报告前完成结构化预分析。
输出格式严格如下（纯文字，禁止输出HTML）：
核心论点: [1句话，本章最重要的发现]
关键证据: [最强的2-3个具体数字或信号]
主要矛盾: [信号之间的冲突，或写"无明显矛盾"]
权衡结论: [如何处理矛盾，给出明确方向判断]
置信度: [1-5，5为最高]"""


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
    try:
        pc_float = float(ctx['put_call_ratio'])
    except (ValueError, TypeError):
        pc_float = 1.0

    # 链式推理：delta 上下文供各章引用
    _delta = ctx.get("delta_context", "")
    _delta_block = f"\n\n{_delta}" if _delta else ""

    _master = ctx.get("master_thesis", "")
    _master_block = f"\n\n【蜂群整体论点（来自CH1）】{_master}" if _master else ""

    _conflicts = detect_conflicts(ctx)
    _conflict_block = (
        "\n\n【系统检测到以下信号矛盾——必须在分析中明确指出并给出权衡判断，不得绕过】\n"
        + "\n".join(_conflicts)
    ) if _conflicts else ""

    # ── Step 1：分析框架提示（每章专属，纯分析不写HTML）─────────────────────────
    step1_prompts = {
        "swarm_analysis": f"""分析 {ticker} 蜂群七维信号结构：
综合评分{score}/10 | Scout {fmt_score(ctx['scout'].get('score'))} | Rival {fmt_score(ctx['rival'].get('score'))} ML7d{ctx['ml_7d']:+.1f}%
Buzz {fmt_score(ctx['buzz'].get('score'))} | Chronos {fmt_score(ctx['chronos'].get('score'))} | Oracle {fmt_score(ctx['oracle'].get('score'))} P/C={ctx['put_call_ratio']}
Guard {fmt_score(ctx['guard'].get('score'))} | Bear {fmt_score(ctx['bear'].get('score'))} 信号:{', '.join(ctx['bear_signals'][:2])}{_delta_block}{_conflict_block}
完成结构化预分析（严格按格式，无HTML）：""",

        "resonance": f"""分析 {ticker} 蜂群共振信号：
共振检测:{res.get('resonance_detected')} | 方向:{res.get('direction','N/A')} | 维度:{', '.join(res_dims)}
支持Agent:{res.get('supporting_agents',0)} | 置信度提升:+{res.get('confidence_boost',0)}% | 期权流:{ctx['flow_direction']}
ML 7d:{ctx['ml_7d']:+.1f}% | 30d:{ctx['ml_30d']:+.1f}%
完成结构化预分析（严格按格式，无HTML）：""",

        "catalyst": f"""分析 {ticker} 催化剂时间线（{len(ctx['catalysts'])}个）：
{json.dumps(ctx['catalysts'][:4], ensure_ascii=False)}
完成结构化预分析（严格按格式，无HTML）：""",

        "options": f"""分析 {ticker} 期权市场结构：
P/C={ctx['put_call_ratio']} | 总OI={ctx['total_oi']:,.0f} | IV Skew={ctx['iv_skew']}({ctx['iv_skew_signal']}) | IV={ctx['iv_current']:.1f}%
流向:{ctx['flow_direction']} | 关键阻力:{json.dumps(ctx['key_levels'].get('resistance',[])[:2],ensure_ascii=False)}
关键支撑:{json.dumps(ctx['key_levels'].get('support',[])[:2],ensure_ascii=False)}
异常流:{json.dumps(ctx['unusual_activity'][:3],ensure_ascii=False)}
完成结构化预分析（严格按格式，无HTML）：""",

        "macro": f"""分析 {ticker} 宏观与情绪环境：
F&G指数:{ctx['fg_score']} | Guard:{fmt_score(ctx['guard'].get('score'))}({ctx['guard'].get('direction','neutral')})
宏观发现:{ctx['guard'].get('discovery','')[:150]}
Buzz情绪%:{ctx['buzz'].get('details',{}).get('sentiment_pct','N/A')} | Reddit:{ctx['reddit'].get('rank','N/A')}名{_delta_block}
完成结构化预分析（严格按格式，无HTML）：""",

        "scenario": f"""分析 {ticker} 情景推演基础：
价格:{'$'+str(ctx['price']) if ctx['price'] else '市价'} | 综合评分:{score}/10 | ML 7d:{ctx['ml_7d']:+.1f}% 30d:{ctx['ml_30d']:+.1f}%
最大阻力:${ctx['key_levels'].get('resistance',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('resistance') else 'N/A'}
最大支撑:${ctx['key_levels'].get('support',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('support') else 'N/A'}
风险信号:{', '.join(ctx['bear_signals'][:2])}{_master_block}
完成结构化预分析（严格按格式，无HTML）：""",

        "risk": f"""分析 {ticker} 风险信号优先级：
逆向信号:{', '.join(ctx['bear_signals'])} | Bear评分:{fmt_score(ctx['bear'].get('score'))}
F&G:{ctx['fg_score']} | IV Skew:{ctx['iv_skew']} | 宏观:{ctx['guard'].get('discovery','')[:120]}{_master_block}
完成结构化预分析（严格按格式，无HTML）：""",
    }

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

生成2段深度叙事分析，解释评分背后的逻辑和各蜂之间的分歧。直接输出两段 HTML <p> 标签。每段使用 <strong>、<span class="bull-text">、<span class="bear-text">、<span class="highlight"> 进行关键词标注。{_delta_block}{_conflict_block}
若有昨日对比数据，请在第二段末尾用一句话点出最显著的评分变化趋势。""",

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
- P/C 比: {ctx['put_call_ratio']}（{'>1 偏空' if pc_float > 1 else '<1 偏多'}）
- 总OI: {ctx['total_oi']:,.0f}
- IV Skew: {ctx['iv_skew']} ({ctx['iv_skew_signal']})
- IV 当前: {ctx['iv_current']:.1f}%
- 流向: {ctx['flow_direction']}
- 关键支撑: {json.dumps(ctx['key_levels'].get('support',[])[:3], ensure_ascii=False)}
- 关键阻力: {json.dumps(ctx['key_levels'].get('resistance',[])[:3], ensure_ascii=False)}
- 异常活动（前5）: {json.dumps(ctx['unusual_activity'][:5], ensure_ascii=False)}
- 系统信号: {ctx['signal_summary']}{_conflict_block}

生成3段深度期权结构分析：1)P/C与OI含义, 2)关键位分析与Gamma机制, 3)IV Skew解读。输出三段 HTML <p> 标签，使用强调标签。""",

        "macro": f"""
分析 {ticker} 当前宏观与情绪环境：
- Fear & Greed 指数: {ctx['fg_score'] if ctx['fg_score'] else '未知'}
- GuardBee 评分: {fmt_score(ctx['guard'].get('score'))} ({ctx['guard'].get('direction','neutral')})
- 宏观发现: {ctx['guard'].get('discovery','')[:200]}
- Buzz情绪%: {ctx['buzz'].get('details',{}).get('sentiment_pct','N/A')}
- Reddit: {ctx['reddit'].get('rank','N/A')}名, {ctx['reddit'].get('mentions','N/A')}次提及

生成2段宏观分析：分析宏观逆风/顺风对该股的影响，以及F&G极值下的反向做多机会。输出两段 HTML <p> 标签，使用强调标签。{_delta_block}
若有昨日对比，请在分析中引用宏观情绪的变化方向。""",

        "scenario": f"""
为 {ticker} 生成四情景推演（基于以下数据）：
- 当前价格: {'$'+str(ctx['price']) if ctx['price'] else '市价'}
- 综合评分: {score}/10，方向: {direction}
- ML 7日预期: {ctx['ml_7d']:+.1f}%，30日: {ctx['ml_30d']:+.1f}%
- 最大阻力: ${ctx['key_levels'].get('resistance',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('resistance') else 'N/A'}
- 最大支撑: ${ctx['key_levels'].get('support',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('support') else 'N/A'}
- 催化剂: {len(ctx['catalysts'])} 个
- 风险信号: {', '.join(ctx['bear_signals'][:2])}

生成四情景的核心交易逻辑段落（1段），以及止盈止损具体参考。输出两段 HTML <p> 标签，使用强调标签。在第二段给出具体入场思路。{_master_block}
情景推演须与蜂群整体论点保持方向一致，若有矛盾需说明原因。""",

        "risk": f"""
为 {ticker} 生成风险分析总结：
- 逆向信号: {', '.join(ctx['bear_signals'])}
- Bear评分: {fmt_score(ctx['bear'].get('score'))} ({ctx['bear'].get('direction')})
- F&G: {ctx['fg_score'] if ctx['fg_score'] else '未知'}
- IV Skew: {ctx['iv_skew']}
- 宏观: {ctx['guard'].get('discovery','')[:150]}

生成1段综合风险总结，整合所有风险信号并给出风险管理建议。输出一段 HTML <p> 标签，使用强调标签。{_master_block}
风险分析须与蜂群整体论点形成对照，指出威胁该论点成立的核心风险。""",
    }

    prompt = prompts.get(section, "")
    if not prompt:
        return _local_fallback(ctx, section)

    try:
        client = anthropic.Anthropic(api_key=api_key)

        # ── Step 1：分析框架（草稿思考，不输出给用户）────────────────────────────
        step1_framework = ""
        s1_prompt = step1_prompts.get(section, "")
        if s1_prompt:
            try:
                s1_msg = client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=200,
                    system=STEP1_SYSTEM,
                    messages=[{"role": "user", "content": s1_prompt}]
                )
                step1_framework = s1_msg.content[0].text.strip()
                _s1_preview = step1_framework[:60].replace(chr(10), ' ')
                _s1_suffix  = '...' if len(step1_framework) > 60 else ''
                print(f"    📋 Step1框架 ({section}): {_s1_preview}{_s1_suffix}")
            except Exception as e1:
                print(f"    ⚠️  Step1失败({section})，降级为单步: {e1}")

        # ── Step 2：基于分析框架写叙事 HTML ─────────────────────────────────────
        if step1_framework:
            enriched_prompt = (
                f"【第一步分析框架】\n{step1_framework}\n\n"
                f"【第二步任务】基于以上分析框架，{prompt.lstrip()}"
            )
        else:
            enriched_prompt = prompt  # 降级：直接单步

        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": enriched_prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠️  LLM 调用失败 ({section}): {e}")
        return _local_fallback(ctx, section)


def _build_swarm_narrative(ctx: dict) -> str:
    ticker = ctx["ticker"]; score = ctx["final_score"]; direction = ctx["direction_zh"]
    sc_cls = 'bull-text' if 'bull' in ctx['direction'] else 'bear-text'
    agents_info = []
    for key, label, dim in [("chronos","ChronosBee","催化剂"), ("guard","GuardBee","风险调整"),
                             ("rival","RivalBee","ML辅助"), ("buzz","BuzzBee","情绪"),
                             ("scout","ScoutBee","信号"), ("oracle","OracleBee","期权/赔率"),
                             ("bear","BearBee","逆向")]:
        a = ctx.get(key, {}); sc = float(a.get('score', 5))
        color = '#22c55e' if sc >= 6.5 else ('#ef4444' if sc <= 4.0 else '#94a3b8')
        disc = a.get('discovery','')[:80]
        agents_info.append(f'<span style="color:{color};font-weight:600">{label} {sc:.1f}</span>（{dim}）：{disc}')
    return (f'<p><strong>蜂群综合评分 <span class="{sc_cls}">{score:.2f}/10</span>，方向 {direction}。</strong>'
            f'7 Agent 投票结果：{ctx.get("resonance",{}).get("supporting_agents",0)} 个同向信号，'
            f'期权流 <span class="highlight">{ctx["flow_direction"]}</span>，P/C={ctx["put_call_ratio"]}，总OI {ctx["total_oi"]:,.0f}。</p>'
            f'<p>' + ' · '.join(agents_info[:3]) + '</p>'
            f'<p>' + ' · '.join(agents_info[3:]) + '</p>'
            f'<p>看空信号：{", ".join(ctx["bear_signals"][:3]) or "无重大看空信号"}。</p>')


def _build_resonance_narrative(ctx: dict) -> str:
    resonance = ctx["resonance"]; res_dims = resonance.get("resonant_dimensions", [])
    detected = resonance.get('resonance_detected', False)
    boost = resonance.get('confidence_boost', 0); count = resonance.get('supporting_agents', 0)
    dim_zh = {"catalyst":"催化剂","ml_auxiliary":"ML辅助","odds":"期权/赔率",
              "risk_adj":"风险调整","signal":"聪明钱","sentiment":"情绪"}
    dims_str = "、".join([dim_zh.get(d, d) for d in res_dims]) or "无"
    status = f"⚡ {count}维共振已触发，信心提升 +{boost}%" if detected else "○ 共振未触发（信号分散）"
    scout_det = ctx.get('scout',{}).get('details',{}) or {}
    guard_det = ctx.get('guard',{}).get('details',{}) or {}
    consistency = guard_det.get('consistency', 0)
    if isinstance(consistency, float) and consistency < 1:
        consistency = int(consistency * 100)
    return (f'<p><strong>{status}</strong></p>'
            f'<p>共振维度：{dims_str}。'
            f'蜂群一致性指数 {consistency}%，GuardBee 风险调整系数 {guard_det.get("adjustment_factor",0.95):.2f}。'
            f'ML预测 7日 <span class="highlight">{ctx["ml_7d"]:+.1f}%</span>，30日 {ctx["ml_30d"]:+.1f}%。</p>'
            f'<p>期权市场配合度：P/C={ctx["put_call_ratio"]}（{"Call主导，看多气氛" if float(ctx["put_call_ratio"]) < 0.9 else "Put主导，看空偏好"}），'
            f'IV Skew {ctx["iv_skew"]}（{"看跌期权溢价偏高" if ctx["iv_skew"] and float(ctx["iv_skew"])>1.2 else "中性"}）。'
            f'共振方向 {resonance.get("direction","neutral")} 与蜂群整体方向{"一致" if resonance.get("direction") == ctx["direction"] else "存在分歧，需注意"}。</p>')


def _build_catalyst_narrative(ctx: dict) -> str:
    cats = ctx.get('catalysts', [])
    near = [c for c in cats if isinstance(c.get('days_until'), (int,float)) and c['days_until'] <= 14]
    chronos_sc = float(ctx.get('chronos',{}).get('score',5))
    cat_lines = []
    for c in cats[:5]:
        days = c.get('days_until','?'); ev = c.get('event', c.get('name',''))[:35]
        sev = c.get('severity','medium'); sev_icon = '🔴' if sev=='critical' else ('🟡' if sev=='high' else '⚪')
        cat_lines.append(f'{sev_icon} <strong>{ev}</strong>（+{days}天）')
    cats_html = ''.join(f'<li>{l}</li>' for l in cat_lines)
    return (f'<p><strong>ChronosBee 评分 {chronos_sc:.1f}/10，检测到 {len(cats)} 个催化剂，{len(near)} 个在14天内。</strong></p>'
            f'<ul style="margin:8px 0 8px 16px;line-height:1.8">{cats_html}</ul>'
            f'<p>{"⚠️ 关键窗口：近期催化剂密度极高，财报/重大事件前期权隐含波动率（IV）通常显著上升，建议关注 IV crush 风险。" if len(near)>=2 else "催化剂相对分散，短期波动性压力适中。"}'
            f'期权到期日集中于 {", ".join(ctx.get("oracle",{}).get("details",{}).get("expiration_dates",[])[:3] or ["近期"])}，'
            f'催化剂与到期日重合度高，期权博弈激烈。</p>')


def _build_options_narrative(ctx: dict) -> str:
    pcr = ctx['put_call_ratio']; oi = ctx['total_oi']; iv = ctx['iv_current']
    skew = ctx['iv_skew']; flow = ctx['flow_direction']
    sups = ctx['key_levels'].get('support',[])[:3]
    ress = ctx['key_levels'].get('resistance',[])[:3]
    sup_str = '、'.join([f"${s['strike']}" for s in sups]) or 'N/A'
    res_str = '、'.join([f"${r['strike']}" for r in ress]) or 'N/A'
    unusual = ctx.get('unusual_activity',[])
    bull_flows = [u for u in unusual if u.get('bullish')]
    bear_flows = [u for u in unusual if not u.get('bullish')]
    flow_cls = 'bull-text' if flow=='bullish' else 'bear-text'
    return (f'<p><strong>期权市场结构：P/C比 {pcr}（{"Call主导，看多氛围浓厚" if float(pcr)<0.9 else "Put主导，看跌保护需求强"}），'
            f'总OI {oi:,.0f}，当前IV {iv:.1f}%，IV Skew {skew}（{"看跌期权溢价偏高，市场对下行有对冲需求" if skew and float(skew)>1.2 else "中性"}）。</strong></p>'
            f'<p>期权流方向：<span class="{flow_cls}">{"净看涨流" if flow=="bullish" else "净看跌流"}</span>。'
            f'异常流检测：{len(bull_flows)}笔看涨异动、{len(bear_flows)}笔看跌异动。'
            f'{ctx["signal_summary"]}</p>'
            f'<p>关键支撑位（高OI Put钉住）：{sup_str}。'
            f'关键阻力位（高OI Call钉住）：{res_str}。'
            f'最大阻力位 OI 集中处为 Gamma 钉住区域，做市商对冲效应显著，价格接近时注意方向性突破。</p>')


def _build_macro_narrative(ctx: dict) -> str:
    fg = ctx.get('fg_score'); guard_sc = float(ctx.get('guard',{}).get('score',5))
    guard_disc = ctx.get('guard',{}).get('discovery','')
    buzz_det = ctx.get('buzz',{}).get('details',{}) or {}
    reddit = ctx.get('reddit',{}) or {}
    sentiment = buzz_det.get('sentiment_pct', 0)
    vol_ratio = buzz_det.get('volume_ratio', 1.0)
    momentum = buzz_det.get('momentum_5d', 0)
    fg_label = '极度恐惧' if fg and fg<=25 else ('恐惧' if fg and fg<=45 else ('中性' if fg and fg<=55 else ('贪婪' if fg and fg<=75 else '极度贪婪')))
    return (f'<p><strong>宏观环境：F&G指数 {fg if fg else "N/A"}（{fg_label}），GuardBee {guard_sc:.1f}/10。</strong>'
            f'{guard_disc[:200]}</p>'
            f'<p>情绪面：看多情绪 {sentiment}%（{"偏多" if sentiment>55 else ("偏空" if sentiment<45 else "中性")}），'
            f'5日动量 {momentum:+.2f}%，成交量比 {vol_ratio:.2f}x（{"放量" if vol_ratio>1.2 else ("缩量" if vol_ratio<0.8 else "正常")}）。'
            f'Reddit 排名 #{reddit.get("rank","N/A")}，提及 {reddit.get("mentions",0)} 次。</p>'
            f'<p>{"⚠️ 当前极度恐惧市场环境增加了短期波动性，建议降低仓位或等待恐慌情绪缓和再入场。" if fg and fg<=25 else ("市场情绪相对中性，系统性风险较低，以个股信号为主要决策依据。" if fg and fg<=55 else "市场情绪偏乐观，注意过热风险，控制追高仓位。")}</p>')


def _build_scenario_narrative(ctx: dict) -> str:
    score = ctx["final_score"]; direction = ctx["direction"]
    ress = ctx['key_levels'].get('resistance',[])[:1]
    sups = ctx['key_levels'].get('support',[])[:1]
    price = ctx.get('price'); ml7 = ctx['ml_7d']; ml30 = ctx['ml_30d']
    target = f"${ress[0]['strike']}" if ress else "关键阻力位"
    stop = f"${sups[0]['strike']}" if sups else "关键支撑位"
    is_bull = 'bull' in direction
    rr = '3:1' if float(score) > 7 else ('2:1' if float(score) > 5.5 else '1.5:1')
    action = 'Call/正股多头' if is_bull else 'Put/空头/防守'
    cats = ctx.get('catalysts',[])
    near_cat = next((c.get('event','') for c in cats if isinstance(c.get('days_until'),(int,float)) and c['days_until']<=7), None)
    return (f'<p><strong>核心交易逻辑（本地模式推演）：</strong>'
            f'蜂群评分 {score:.2f}/10，方向{"看多" if is_bull else "看空"}，建议操作：{action}。</p>'
            f'<p>ML 预期 7日 <span class="highlight">{ml7:+.1f}%</span>，30日 {ml30:+.1f}%。'
            f'{"当前" if price else ""}{"入场价参考 $"+str(round(price,2))+"，" if price else ""}'
            f'目标位 {target}，止损参考 {stop}，风险回报比约 {rr}。</p>'
            f'{f"<p>⚡ 近期催化剂：{near_cat}（7天内），事件驱动窗口，注意期权IV变化，建议事件前1-2天内完成建仓。</p>" if near_cat else ""}'
            f'<p>{"注意：IV Skew偏高意味着下行保护成本较高，如做多建议使用垂直价差降低期权成本。" if ctx["iv_skew"] and float(ctx["iv_skew"])>1.2 else "IV结构中性，单腿期权可作为入场工具。"}</p>')


def _build_risk_narrative(ctx: dict) -> str:
    bear_sigs = ctx['bear_signals']; bear_sc = float(ctx.get('bear',{}).get('score',5))
    skew = ctx['iv_skew']; fg = ctx.get('fg_score',50)
    guard_det = ctx.get('guard',{}).get('details',{}) or {}
    consistency = guard_det.get('consistency', 0)
    if isinstance(consistency, float) and consistency < 1:
        consistency = int(consistency * 100)
    sig_html = ''.join(f'<li>⚠️ {s}</li>' for s in bear_sigs[:4]) if bear_sigs else '<li>无重大看空信号</li>'
    risk_level = '高' if bear_sc > 5 or (fg and fg <= 25) else ('中' if bear_sc > 3 else '低')
    return (f'<p><strong>综合风险等级：{risk_level}。BearBee评分 {bear_sc:.1f}/10，'
            f'IV Skew {skew}（{"偏高，下行对冲溢价" if skew and float(skew)>1.2 else "中性"}），'
            f'蜂群一致性 {consistency}%。</strong></p>'
            f'<ul style="margin:8px 0 8px 16px;line-height:1.8">{sig_html}</ul>'
            f'<p>{"⚠️ F&G极度恐惧（"+str(fg)+"），市场系统性恐慌可能导致非理性抛售，需额外保护。" if fg and fg<=25 else ""}'
            f'建议：{"控制仓位≤总资金5%，配置Put保险或止损单" if risk_level=="高" else ("正常仓位管理，关注信号变化" if risk_level=="中" else "可适度加仓，保持止损纪律")}。</p>')


def _local_fallback(ctx: dict, section: str) -> str:
    """当 LLM 不可用时的本地生成文本"""
    ticker = ctx["ticker"]
    score = ctx["final_score"]
    direction = ctx["direction_zh"]
    resonance = ctx["resonance"]
    res_dims = resonance.get("resonant_dimensions", [])

    fallbacks = {
        "swarm_analysis": _build_swarm_narrative(ctx),
        "resonance": _build_resonance_narrative(ctx),
        "catalyst": _build_catalyst_narrative(ctx),
        "options": _build_options_narrative(ctx),
        "macro": _build_macro_narrative(ctx),
        "scenario": _build_scenario_narrative(ctx),
        "risk": _build_risk_narrative(ctx),
    }
    return fallbacks.get(section, "<p>数据处理中...</p>")


# ── HTML 生成 ─────────────────────────────────────────────────────────────────

def generate_html(ctx: dict, reasoning: dict) -> str:
    """组装完整的 Template C v3.0 HTML 报告"""
    ticker = ctx["ticker"]
    report_date = ctx["report_date"]
    score = ctx["final_score"]
    direction = ctx["direction"]
    direction_zh_str = ctx["direction_zh"]
    res = ctx["resonance"]
    price = ctx.get("price")
    price_str = f"${price:.2f}" if price else "N/A"
    price_label = "收市价" if ctx.get("price_is_close") else "扫描时价格"

    res_detected = res.get("resonance_detected", False)
    res_dims = res.get("resonant_dimensions", [])
    res_boost = res.get("confidence_boost", 0)
    res_count = res.get("cross_dim_count", 0)
    res_label = f"{res_count}维共振" if res_detected else "无共振"

    # 各蜂评分（name, score, dim_label, prev_key）
    agents = [
        ("ChronosBee", ctx["chronos"].get("score", 0), "催化剂",  "chronos"),
        ("RivalBee",   ctx["rival"].get("score", 0),   "ML辅助",  "rival"),
        ("OracleBee",  ctx["oracle"].get("score", 0),  "期权/赔率","oracle"),
        ("BuzzBee",    ctx["buzz"].get("score", 0),    "消息/舆情","buzz"),
        ("ScoutBee",   ctx["scout"].get("score", 0),   "基本面",  "scout"),
        ("GuardBee",   ctx["guard"].get("score", 0),   "宏观/情绪","guard"),
        ("BearBee",    ctx["bear"].get("score", 0),    "逆向",    "bear"),
    ]

    # 蜂群评分卡 HTML（含昨日 Δ 箭头）
    _prev = ctx.get("prev")
    score_cards = ""
    for name, val, dim, pkey in agents:
        cls = score_class(val)
        # 计算与昨日的差值
        delta_html = ""
        if _prev:
            try:
                prev_val = float(_prev.get(pkey, 0) or 0)
                cur_val  = float(val or 0)
                diff = cur_val - prev_val
                if abs(diff) >= 0.1:
                    arrow = "↑" if diff > 0 else "↓"
                    d_color = "#10b981" if diff > 0 else "#ef4444"
                    delta_html = (f'<div class="delta" style="font-size:10px;color:{d_color};'
                                  f'margin-top:2px;">{arrow}{abs(diff):.1f}</div>')
            except Exception:
                pass
        score_cards += f"""
        <div class="score-card {cls}">
          <div class="bee">{name}</div>
          <div class="val">{fmt_score(val)}</div>
          <div class="dim">{dim}</div>{delta_html}
        </div>"""

    # 蜂群投票统计 + 综合评分卡
    try:
        agent_votes_bull = sum(1 for n, v, d, pk in agents if v and float(v) >= 6.5)
        agent_votes_bear = sum(1 for n, v, d, pk in agents if v and float(v) <= 3.5)
        agent_votes_neut = len(agents) - agent_votes_bull - agent_votes_bear
    except Exception:
        agent_votes_bull = agent_votes_neut = agent_votes_bear = 0
    score_color = "var(--green2)" if "bull" in direction else ("var(--red2)" if "bear" in direction else "var(--gold2)")

    # ⑤ T-7 预测闭环 widget
    t7_widget_html = ""
    _t7 = ctx.get("t7")
    _cur_price = ctx.get("price")
    if _t7 and _t7.get("price") and _cur_price:
        try:
            t7_price   = float(_t7["price"])
            cur_price  = float(_cur_price)
            t7_dir     = _t7.get("direction", "neutral")
            pct_change = (cur_price - t7_price) / t7_price * 100
            actual_dir = "bull" if pct_change >= 1.0 else ("bear" if pct_change <= -1.0 else "neutral")
            # 严格命中：预判方向必须与实际方向完全一致
            is_correct = (t7_dir == actual_dir)
            # T-7方向中文 & 颜色（基于T-7预判，而非今日）
            t7_dir_zh   = {"bull":"看多","bear":"看空","neutral":"中性"}.get(t7_dir, t7_dir)
            t7_dir_color= {"bull":"#10b981","bear":"#ef4444","neutral":"#f59e0b"}.get(t7_dir, "var(--text2)")
            chg_color   = "#10b981" if pct_change >= 0 else "#ef4444"
            result_icon = "✅" if is_correct else "❌"
            result_txt  = "方向命中" if is_correct else "方向未中"
            result_color= "#10b981" if is_correct else "#ef4444"
            t7_widget_html = (
                f'<div class="t7-verify" style="margin-top:10px;padding:8px 12px;'
                f'background:var(--bg3);border-radius:8px;border:1px solid var(--border);'
                f'font-size:11px;color:var(--text3);display:flex;align-items:center;gap:10px;">'
                f'<span style="font-weight:600;color:var(--text2);">T-7 回测</span>'
                f'<span>7日前预判: <strong style="color:{t7_dir_color};">{t7_dir_zh}</strong></span>'
                f'<span>实际涨跌: <strong style="color:{chg_color};">{pct_change:+.1f}%</strong></span>'
                f'<span style="font-weight:700;color:{result_color};">{result_icon} {result_txt}</span>'
                f'</div>'
            )
        except Exception:
            pass

    score_summary_html = f"""<div class="score-summary">
      <div class="score-summary-row">
        <div class="final-score-big" style="color:{score_color};">{score:.2f}</div>
        <span class="stat-pill bull-pill">看多 {agent_votes_bull}</span>
        <span class="stat-pill neut-pill">中性 {agent_votes_neut}</span>
        <span class="stat-pill bear-pill">看空 {agent_votes_bear}</span>
      </div>{t7_widget_html}
    </div>"""

    # 条形图 HTML
    bar_rows = ""
    for name, val, _, _pk in sorted(agents, key=lambda x: -float(x[1]) if x[1] else 0):
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

    # 智能重要性升级：JSON 数据常把关键事件标为 medium，根据关键词+天数本地修正
    _CRITICAL_KW = ['财报', 'earnings', 'result', 'revenue', 'q4', 'q3', 'q2', 'q1']
    _HIGH_KW     = ['gtc', 'conference', 'keynote', '大会', '发布会', '会议',
                    'fed', 'fomc', 'cpi', 'opex', 'gdp', 'nonfarm', '非农']
    def smart_importance(ev_name: str, days_until: int, original: str) -> str:
        ev_l = ev_name.lower()
        if days_until is not None and 0 <= days_until <= 10:
            if any(kw in ev_l for kw in _CRITICAL_KW):
                return "critical"
        if days_until is not None and 0 <= days_until <= 14:
            if any(kw in ev_l for kw in _HIGH_KW):
                return "high"
        return original

    timeline_html = ""
    today_str = str(date.today())
    for c in ctx["catalysts"]:
        days = c.get("days_until", 0)
        ev = c.get("event", "催化剂事件")
        ev_date = c.get("date", "")
        importance = c.get("importance", "medium")
        # 智能升级
        eff_importance = smart_importance(ev, days, str(importance).lower())
        dot_cls = dot_class_map.get(eff_importance, "dot-medium")
        # timeline-item 附加 CSS 类
        is_past = days is not None and days < 0
        item_extra = " item-past" if is_past else (f" item-{eff_importance}" if eff_importance in ("high", "critical") else "")
        if days == 0:
            label = f"今日 · {ev_date}"
        elif is_past:
            label = f"已过 {abs(days)} 天 · {ev_date}"
        else:
            label = f"T+{days} · {ev_date}"
        desc = c.get("description", "")
        timeline_html += f"""
        <div class="timeline-item{item_extra}">
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

    # 异常流表格 + unusual-list
    flow_rows = ""
    unusual_items_html = ""
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
        item_cls = "bull-item" if is_call else "bear-item"
        val_color = "var(--green2)" if is_call else "var(--red2)"
        unusual_items_html += f"""
        <div class="unusual-item {item_cls}">
          <strong style="color:{val_color};">{direction_word} ${strike:.0f}</strong>
          <span style="font-size:11px;color:var(--text2);"> · {volume:,.0f} 手 · {signal_desc}{ratio_str}</span>
        </div>"""
    if not unusual_items_html:
        unusual_items_html = '<div style="color:var(--text3);font-size:12px;">暂无异常期权流数据</div>'

    # 到期日标签行（在异常流列表标题旁展示）
    exp_dates = ctx.get("expiration_dates", [])
    if exp_dates:
        exp_tags = "".join(
            f'<span style="background:var(--bg4);border:1px solid var(--border);border-radius:4px;'
            f'font-size:10px;font-weight:600;padding:2px 7px;color:var(--text2);">{d}</span>'
            for d in exp_dates[:4]
        )
        expiry_row_html = f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">' \
                          f'<span style="font-size:10px;color:var(--text3);align-self:center;">到期日：</span>' \
                          f'{exp_tags}</div>'
    else:
        expiry_row_html = ""

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

    # 总 OI 显示（万 = ×10,000；不使用"百万"，避免单位混淆）
    total_oi = ctx.get("total_oi", 0)
    if total_oi >= 100_000_000:
        oi_str = f"{total_oi/100_000_000:.1f}亿"
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

    # ── 止盈/止损计划 ─────────────────────────────────────────────
    win_rate = (probs[0] + probs[1]) * 100  # 看涨情景合计胜率
    if price and price > 0:
        # 止盈目标（基于情景概率梯度）
        tp1_pct = returns[1] * 0.5 if 'bull' in direction else abs(returns[2]) * 0.5
        tp2_pct = returns[1] if 'bull' in direction else abs(returns[2])
        tp3_pct = returns[0] * 0.85 if 'bull' in direction else abs(returns[2]) * 1.8
        if 'bear' in direction:
            # 看空情景：止盈是下行目标，止损是上行
            tp1 = price * (1 - tp1_pct)
            tp2 = price * (1 - tp2_pct)
            tp3 = price * (1 - tp3_pct)
            sl_cons  = price * 1.03   # 保守止损 +3%
            sl_std   = price * 1.055  # 标准止损 +5.5%
            sl_aggr  = price * 1.085  # 激进止损 +8.5%
            tp_color, sl_color = "var(--red2)", "var(--green2)"
            tp_sign, sl_sign = "-", "+"
        else:
            tp1 = price * (1 + tp1_pct)
            tp2 = price * (1 + tp2_pct)
            tp3 = price * (1 + tp3_pct)
            sl_cons  = price * 0.97   # 保守止损 -3%
            sl_std   = price * 0.945  # 标准止损 -5.5%
            sl_aggr  = price * 0.915  # 激进止损 -8.5%
            tp_color, sl_color = "var(--green2)", "var(--red2)"
            tp_sign, sl_sign = "+", "-"
        tp1_pct_show = abs(tp1/price - 1) * 100
        tp2_pct_show = abs(tp2/price - 1) * 100
        tp3_pct_show = abs(tp3/price - 1) * 100
        risk_reward = tp2_pct_show / (5.5) if 5.5 > 0 else 0  # 收益/风险比
    else:
        tp1 = tp2 = tp3 = 0
        sl_cons = sl_std = sl_aggr = 0
        tp1_pct_show = tp2_pct_show = tp3_pct_show = 0
        tp_color, sl_color = "var(--green2)", "var(--red2)"
        tp_sign, sl_sign = "+", "-"
        risk_reward = 0

    # 止盈/止损 HTML 块
    tp_title = "📉 止盈计划（下行目标）" if "bear" in direction else "📈 止盈计划"
    sl_title = "🛡️ 止损计划（上行止损）" if "bear" in direction else "🛡️ 止损计划"
    if price and price > 0:
        trade_grid_html = f"""<div class="trade-grid">
      <div class="tp-box">
        <div class="tb-title">{tp_title}</div>
        <div class="tp-row">
          <span class="tr-price">${tp1:.2f}</span>
          <span class="tr-pct" style="color:{tp_color}">{tp_sign}{tp1_pct_show:.1f}%</span>
          <span style="font-size:11px;color:var(--text3)">减仓 1/3</span>
        </div>
        <div class="tp-row">
          <span class="tr-price">${tp2:.2f}</span>
          <span class="tr-pct" style="color:{tp_color}">{tp_sign}{tp2_pct_show:.1f}%</span>
          <span style="font-size:11px;color:var(--text3)">减仓 1/3</span>
        </div>
        <div class="tp-row">
          <span class="tr-price">${tp3:.2f}</span>
          <span class="tr-pct" style="color:{tp_color}">{tp_sign}{tp3_pct_show:.1f}%</span>
          <span style="font-size:11px;color:var(--text3)">清仓剩余</span>
        </div>
        <div style="margin-top:8px;font-size:11px;color:var(--text3)">综合胜率 {win_rate:.1f}% · 持仓建议 5–10天</div>
      </div>
      <div class="sl-box">
        <div class="tb-title">{sl_title}</div>
        <div class="sl-row">
          <span class="sr-label">保守止损</span>
          <span class="sr-price" style="color:{sl_color}">${sl_cons:.2f}</span>
        </div>
        <div class="sl-row">
          <span class="sr-label">标准止损</span>
          <span class="sr-price" style="color:{sl_color}">${sl_std:.2f}</span>
        </div>
        <div class="sl-row">
          <span class="sr-label">激进止损</span>
          <span class="sr-price" style="color:{sl_color}">${sl_aggr:.2f}</span>
        </div>
        <div style="margin-top:8px;font-size:11px;color:var(--text3)">建议标准止损 ${sl_std:.2f} · 风险/收益比 ≈ 1:{risk_reward:.1f}</div>
      </div>
    </div>"""
    else:
        trade_grid_html = ""

    # 风险列表 —— 基于实际数据构建情景化风险项
    bear_signals = ctx.get("bear_signals", [])
    smart_risks = []   # list of (level_cls, badge_label, icon, title, note)

    # R1: 催化剂事件触发窗口
    cat_days = None
    for ev in ctx.get("catalysts", []):
        d = ev.get("days_until")
        if d is not None and d >= 0:
            cat_days = d if cat_days is None else min(cat_days, d)
    if cat_days is not None and cat_days <= 7:
        cat_name = next((e.get("event","催化剂") for e in ctx.get("catalysts",[]) if e.get("days_until") == cat_days), "关键催化剂事件")
        smart_risks.append(("risk-high", "HIGH", "📅",
            f"催化剂窗口 · {cat_name}（{cat_days}天后）",
            f"{cat_name} 即将在 {cat_days} 天内触发。财报/会议期间 IV 通常先扩张后骤降（IV Crush），"
            f"期权多头即使方向正确也可能因 Vega 损失而亏损；"
            f"事件冲击往往在盘前/盘后成交稀薄时放大，建议避免追价入场。"))
    elif cat_days is not None and cat_days <= 14:
        cat_name = next((e.get("event","催化剂") for e in ctx.get("catalysts",[]) if e.get("days_until") == cat_days), "催化剂事件")
        smart_risks.append(("risk-med", "MED", "📅",
            f"催化剂事件临近（{cat_days}天）",
            f"{cat_name} 将在 {cat_days} 天内发生。短期 IV 可能随事件临近持续攀升，"
            f"事件前后波动率变化将影响期权定价，需关注 IV 曲线结构变化。"))

    # R2: IV Skew / Vol Crush 风险
    iv_sk = ctx.get("iv_skew")
    if iv_sk and float(iv_sk) > 1.15:
        smart_risks.append(("risk-high", "HIGH", "📊",
            f"IV Skew 偏高 · Vol Crush 风险（Skew={iv_sk:.2f}）",
            f"Put/Call IV Skew {iv_sk:.2f} 显示市场对下行保护需求强烈。"
            f"若催化剂结果超预期，IV 将大幅下降（通常 -30%~-50%），"
            f"导致期权多头即使方向正确也因 Vega 损失而亏损。正股持有者无此风险。"))
    elif iv_sk and float(iv_sk) > 1.05:
        smart_risks.append(("risk-med", "MED", "📊",
            f"IV Skew 温和偏高（Skew={iv_sk:.2f}）",
            f"当前 IV Skew {iv_sk:.2f}，看跌期权溢价略高于中性水平。"
            f"市场存在一定的下行保护需求，期权策略需注意 Vega 敞口。"))

    # R3: ML 内部模型分歧
    ml7 = ctx.get("ml_7d", 0)
    ml_raw = ctx.get("ml_raw_7d")   # 如有根层预测
    if ml_raw is not None and abs(float(ml7) - float(ml_raw)) > 10:
        smart_risks.append(("risk-med", "MED", "🤖",
            f"ML 内部模型分歧（代理层 {ml7:+.1f}% vs 根层 {ml_raw:+.1f}%）",
            f"RivalBee 代理层预测 {ml7:+.1f}%，但底层模型输出 {ml_raw:+.1f}%，两者分歧显著。"
            f"分歧通常源于底层特征归一化异常（如 crowding_score 极端值）。"
            f"代理层输出更稳定可信，但内部分歧本身代表模型不确定性上升。"))

    # R4: 宏观情绪极端（使用独立变量名，避免覆盖后续 fg 显示变量）
    fg_raw = ctx.get("fg_score")
    if fg_raw is not None:
        fg_int = int(fg_raw)
        if fg_int <= 20:
            smart_risks.append(("risk-med", "MED", "😱",
                f"宏观极度恐慌（F&G={fg_int}）",
                f"恐慌贪婪指数 {fg_int}（极度恐慌区间 ≤25）。极度恐慌通常是买入信号，"
                f"但也可能意味着系统性风险尚未出清（关税、美联储路径、地缘政治）。"
                f"若宏观继续恶化，个股催化剂效应可能被市场整体情绪压制。"))
        elif fg_int >= 75:
            smart_risks.append(("risk-med", "MED", "🤑",
                f"宏观极度贪婪（F&G={fg_int}）",
                f"恐慌贪婪指数 {fg_int}（极度贪婪区间 ≥75）。市场已充分定价乐观预期，"
                f"任何负面意外都可能触发更强烈的获利了结。高位追涨的风险/收益比不佳。"))

    # R5: IV Rank 数据质量
    iv_pct_raw = ctx.get("iv_percentile")
    if iv_pct_raw is not None and float(iv_pct_raw) == 0:
        smart_risks.append(("risk-med", "MED", "📡",
            "IV Rank 数据缺失",
            "本次运行 IV Rank/Percentile 数据显示为 0（非真实 0 值，系采集异常）。"
            "期权定价难以准确评估当前 IV 是否偏贵或偏便宜。"
            "建议通过 Thinkorswim / Tastytrade 实时确认 IV 水平，避免基于过时数据入场。"))

    # R6: 历史样本量不足
    hist_acc = ctx.get("hist_accuracy")
    hist_n   = ctx.get("hist_sample_n")
    if hist_n is not None and int(hist_n) <= 5:
        smart_risks.append(("risk-low", "LOW", "📉",
            f"历史样本偏小（{hist_n} 次，准确率 {hist_acc or 'N/A'}%）",
            f"Alpha Hive 在类似评分形态下仅有 {hist_n} 次历史记录，"
            f"样本量不足以产生统计显著性，历史胜率参考意义有限，"
            f"主要依赖当前实时信号质量而非历史回测进行判断。"))

    # 如果智能风险项不足 3 条，用原始 bear_signals 补充
    if len(smart_risks) < 3 and bear_signals:
        fallback_icons  = ["⚡", "💧", "🏛️"]
        fallback_titles = ["波动性风险", "流动性风险", "监管风险"]
        for j, sig in enumerate(bear_signals[:max(0, 3 - len(smart_risks))]):
            lvl = "risk-med"
            smart_risks.append((lvl, "MED", fallback_icons[j % 3],
                fallback_titles[j % 3], sig))

    risk_items_html = ""
    for (lvl_cls, badge_txt, icon, title, note) in smart_risks:
        risk_items_html += f"""
        <div class="risk-item {lvl_cls}">
          <div class="risk-badge">{badge_txt}</div>
          <div>
            <div class="risk-title">{icon} {title}</div>
            <div class="risk-note">{note}</div>
          </div>
        </div>"""

    # ── 主 HTML ──────────────────────────────────────────────
    # 安全转换 put_call_ratio（可能为 'N/A' 字符串）
    try:
        pc_float = float(ctx['put_call_ratio'])
    except (ValueError, TypeError):
        pc_float = 1.0   # 无法解析时视为中性
    iv_skew_display = f"{ctx['iv_skew']:.2f}" if ctx.get("iv_skew") else "N/A"
    bull_unusual_count = bullish_unusual
    bear_unusual_count = bearish_unusual
    # OTM IV 副文本（在 IV Skew 卡里显示）
    otm_p = ctx.get("otm_put_iv")
    otm_c = ctx.get("otm_call_iv")
    if otm_p and otm_c:
        otm_sub = f"Put {otm_p:.0f}% / Call {otm_c:.0f}%"
    else:
        otm_sub = ctx['iv_skew_signal']
    # IV Rank 显示（用 iv_percentile 百分位）
    iv_pct = ctx.get("iv_percentile")
    if iv_pct is not None and float(iv_pct) > 0:
        iv_rank_display = f"{iv_pct:.0f}th"
        iv_rank_color   = "var(--red2)" if float(iv_pct) >= 70 else ("var(--green2)" if float(iv_pct) <= 30 else "var(--gold2)")
        iv_rank_sub     = "高波动区间" if float(iv_pct) >= 70 else ("低波动区间" if float(iv_pct) <= 30 else "中等波动")
    else:
        iv_rank_display = "—"
        iv_rank_color   = "var(--text3)"
        iv_rank_sub     = "数据待更新"

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
  .ticker-info .company {{ font-size: 13px; color: var(--text2); margin-top: 3px; font-weight: 500; }}
  .ticker-info .subtitle {{ font-size: 11px; color: var(--text3); margin-top: 2px; }}
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
  .timeline-item.item-critical .timeline-event {{ color: var(--red2); }}
  .timeline-item.item-critical .timeline-label {{ color: var(--red); }}
  .timeline-item.item-high .timeline-event {{ color: var(--gold2); }}
  .timeline-item.item-high .timeline-label {{ color: var(--gold); }}
  .timeline-item.item-past {{ opacity: 0.45; }}
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
  /* ── Rich template additions ─────────────────────────────── */
  .ch-num {{ font-size:11px; color:var(--text3); background:var(--bg3); border:1px solid var(--border); border-radius:4px; padding:2px 7px; font-weight:600; }}
  .score-summary {{ background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-top:12px; }}
  .score-summary-row {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
  .final-score-big {{ font-size:44px; font-weight:900; line-height:1; }}
  .final-score-big.bear-score {{ color:var(--red2); }} .final-score-big.bull-score {{ color:var(--green2); }} .final-score-big.neutral-score {{ color:var(--gold2); }}
  .final-score-label {{ font-size:11px; color:var(--text3); margin-top:3px; }}
  .score-divider {{ width:1px; height:44px; background:var(--border); align-self:stretch; }}
  .stat-pill {{ background:var(--bg4); border-radius:6px; padding:7px 14px; text-align:center; }}
  .stat-pill .sp-label {{ font-size:10px; color:var(--text3); }}
  .stat-pill .sp-val {{ font-size:16px; font-weight:800; }}
  .bull-pill .sp-val {{ color:var(--green2); }} .bear-pill .sp-val {{ color:var(--red2); }} .neut-pill .sp-val {{ color:var(--gold2); }}
  .opt-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:10px; margin-bottom:14px; }}
  .opt-card {{ background:var(--bg3); border:1px solid var(--border); border-radius:8px; padding:14px; }}
  .opt-card .oc-label {{ font-size:11px; color:var(--text3); margin-bottom:4px; }}
  .opt-card .oc-val {{ font-size:24px; font-weight:800; }}
  .opt-card .oc-sub {{ font-size:11px; color:var(--text3); margin-top:3px; }}
  .oc-bull .oc-val {{ color:var(--green2); }} .oc-bear .oc-val {{ color:var(--red2); }} .oc-neut .oc-val {{ color:var(--gold2); }} .oc-info .oc-val {{ color:var(--blue2); }}
  .macro-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px; margin-bottom:14px; }}
  .unusual-list {{ display:flex; flex-direction:column; gap:6px; margin:12px 0; }}
  .unusual-item {{ display:flex; align-items:center; gap:10px; background:var(--bg3); border-radius:6px; padding:8px 12px; border-left:3px solid; }}
  .unusual-item.bull-item {{ border-color:var(--green); }} .unusual-item.bear-item {{ border-color:var(--red); }}
  .unusual-item .ui-type {{ font-size:10px; font-weight:700; width:56px; flex-shrink:0; }}
  .bull-item .ui-type {{ color:var(--green2); }} .bear-item .ui-type {{ color:var(--red2); }}
  .unusual-item .ui-strike {{ font-size:13px; font-weight:700; color:var(--text); width:52px; }}
  .unusual-item .ui-vol {{ font-size:12px; color:var(--text2); flex:1; }}
  .unusual-item .ui-signal {{ font-size:11px; font-weight:700; }}
  .bull-item .ui-signal {{ color:var(--green2); }} .bear-item .ui-signal {{ color:var(--red2); }}
  .sc-card {{ background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:14px; }}
  .sc-card .sc-name {{ font-size:12px; font-weight:700; margin-bottom:5px; }}
  .sc-card .sc-prob {{ font-size:11px; color:var(--text2); margin-bottom:8px; }}
  .sc-card .sc-price {{ font-size:22px; font-weight:800; margin-bottom:3px; }}
  .sc-card .sc-gain {{ font-size:13px; font-weight:700; }}
  .sc-card .sc-note {{ font-size:11px; color:var(--text2); margin-top:7px; line-height:1.6; }}
  .sc-bull .sc-name, .sc-bull .sc-price, .sc-bull .sc-gain {{ color:var(--green2); }}
  .sc-meh .sc-name, .sc-meh .sc-price, .sc-meh .sc-gain {{ color:var(--gold2); }}
  .sc-bear .sc-name, .sc-bear .sc-price, .sc-bear .sc-gain {{ color:var(--red2); }}
  .risk-list {{ display:flex; flex-direction:column; gap:8px; }}
  .risk-item {{ background:var(--bg3); border:1px solid var(--border); border-radius:8px; padding:10px 14px; display:flex; gap:12px; align-items:flex-start; }}
  .risk-badge {{ font-size:10px; font-weight:700; padding:3px 8px; border-radius:4px; white-space:nowrap; flex-shrink:0; margin-top:2px; }}
  .risk-high .risk-badge {{ background:rgba(239,68,68,.2); color:var(--red2); border:1px solid rgba(239,68,68,.4); }}
  .risk-med .risk-badge {{ background:rgba(245,158,11,.2); color:var(--gold2); border:1px solid rgba(245,158,11,.4); }}
  .risk-low .risk-badge {{ background:rgba(59,130,246,.2); color:var(--blue2); border:1px solid rgba(59,130,246,.4); }}
  .risk-title {{ font-size:13px; font-weight:600; color:var(--text); }}
  .risk-note {{ font-size:12px; color:var(--text2); margin-top:3px; line-height:1.5; }}
  /* ── TRADE GRID (止盈/止损) ── */
  .trade-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }}
  .tp-box, .sl-box {{ background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:14px; }}
  .tp-box {{ border-color:rgba(34,197,94,0.3); }}
  .sl-box {{ border-color:rgba(239,68,68,0.3); }}
  .tb-title {{ font-size:12px; font-weight:700; margin-bottom:10px; }}
  .tp-box .tb-title {{ color:var(--green2); }}
  .sl-box .tb-title {{ color:var(--red2); }}
  .tp-row {{ display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px solid var(--border); font-size:12px; }}
  .tp-row:last-of-type {{ border:none; }}
  .tp-row .tr-price {{ font-weight:700; color:var(--text1); }}
  .tp-row .tr-pct {{ color:var(--green2); font-weight:600; }}
  .sl-row {{ display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px solid var(--border); font-size:12px; }}
  .sl-row:last-of-type {{ border:none; }}
  .sl-row .sr-label {{ color:var(--text2); }}
  .sl-row .sr-price {{ font-weight:700; color:var(--red2); }}
  @media (max-width: 600px) {{
    .levels-grid, .scenario-grid, .trade-grid {{ grid-template-columns: 1fr; }}
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
        <div class="company">{ctx.get('overview','') if ctx.get('overview') else 'Alpha Hive · 深度分析'}</div>
        <div class="subtitle">📅 {report_date} · Alpha Hive 深度分析 v3.0</div>
      </div>
    </div>
    <div class="price-block">
      <div class="price">{price_str}</div>
      <div class="price-sub">NASDAQ · {price_label}</div>
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
    🤖 <strong>混合模式 · Template C v3.0</strong> —— 本地 JSON 数据 + Claude API 深度推理生成 · {report_date}
  </div>

  <!-- CH1 -->
  <div class="section" id="ch1">
    <div class="section-header">
      <span class="ch-num">CH1</span>
      <span class="section-icon">🐝</span>
      <span class="section-title">第一章 · 蜂群七维评分</span>
      <span class="section-badge {direction_badge(direction)}">综合 {score:.2f} · {'看多' if 'bull' in direction else ('看空' if 'bear' in direction else '中性')}</span>
    </div>
    <div class="section-body">
      {score_summary_html}
      <div class="score-grid" style="margin-top:14px;">{score_cards}</div>
      <div class="divider"></div>
      <div class="prose">{reasoning.get('swarm_analysis', '<p>分析生成中...</p>')}</div>
      <div style="margin-top:14px;">{bar_rows}</div>
    </div>
  </div>

  <!-- CH2 -->
  <div class="section" id="ch2">
    <div class="section-header">
      <span class="ch-num">CH2</span>
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
      <span class="ch-num">CH3</span>
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
      <span class="ch-num">CH4</span>
      <span class="section-icon">📊</span>
      <span class="section-title">第四章 · 期权市场结构</span>
      <span class="section-badge {flow_badge}">P/C={ctx['put_call_ratio']} · OI={oi_str} · {bull_unusual_count}个看涨异动</span>
    </div>
    <div class="section-body">
      <div class="opt-grid" style="margin-bottom:16px;">
        <div class="opt-card oc-bull">
          <div class="oc-label">Put/Call 比</div>
          <div class="oc-val" style="color:{'var(--green2)' if pc_float < 1 else 'var(--red2)'}">
            {ctx['put_call_ratio']}
          </div>
          <div class="oc-sub">{'Call主导' if pc_float < 1 else 'Put主导'}</div>
        </div>
        <div class="opt-card oc-info">
          <div class="oc-label">总开仓量</div>
          <div class="oc-val" style="color:var(--cyan2)">{oi_str}</div>
          <div class="oc-sub">未平仓合约</div>
        </div>
        <div class="opt-card oc-neut">
          <div class="oc-label">IV Skew</div>
          <div class="oc-val" style="color:var(--gold2)">{iv_skew_display}</div>
          <div class="oc-sub">{otm_sub}</div>
        </div>
        <div class="opt-card oc-bull">
          <div class="oc-label">看涨异动</div>
          <div class="oc-val" style="color:var(--green2)">{bull_unusual_count}</div>
          <div class="oc-sub">vs {bear_unusual_count} 看跌</div>
        </div>
        <div class="opt-card oc-info">
          <div class="oc-label">IV Rank</div>
          <div class="oc-val" style="color:{iv_rank_color}">{iv_rank_display}</div>
          <div class="oc-sub">{iv_rank_sub}</div>
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
        <div style="font-size:12px;font-weight:700;color:var(--text2);margin-bottom:8px;">异常期权流（成交量排名）</div>
        {expiry_row_html}
        <div class="unusual-list">{unusual_items_html}</div>
      </div>

      <div class="prose">{reasoning.get('options', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH5 -->
  <div class="section" id="ch5">
    <div class="section-header">
      <span class="ch-num">CH5</span>
      <span class="section-icon">🌍</span>
      <span class="section-title">第五章 · 宏观环境与情绪面</span>
      <span class="section-badge {'badge-bear' if fg != 'N/A' and int(fg) < 30 else 'badge-neutral'}">
        F&G = {fg} · {fg_label}
      </span>
    </div>
    <div class="section-body">
      <div class="macro-grid" style="margin-bottom:16px;">
        <div class="opt-card oc-neut">
          <div class="oc-label">Fear & Greed</div>
          <div class="oc-val" style="color:{fg_color}">{fg}</div>
          <div class="oc-sub">{fg_label}</div>
        </div>
        <div class="opt-card oc-bull">
          <div class="oc-label">GuardBee</div>
          <div class="oc-val" style="color:{'var(--green2)' if float(ctx['guard'].get('score',5)) > 5.5 else 'var(--red2)'}">
            {fmt_score(ctx['guard'].get('score'))}
          </div>
          <div class="oc-sub">{'宏观顺风' if float(ctx['guard'].get('score',5)) > 5.5 else '宏观阻力'}</div>
        </div>
        <div class="opt-card oc-info">
          <div class="oc-label">共振加成</div>
          <div class="oc-val" style="color:{'var(--purple2)' if res_detected else 'var(--text3)'}">
            {'+' + str(res_boost) + '%' if res_detected else '0%'}
          </div>
          <div class="oc-sub">置信提升</div>
        </div>
        <div class="opt-card oc-neut">
          <div class="oc-label">Reddit 热度</div>
          <div class="oc-val" style="color:var(--blue2)">
            {ctx['reddit'].get('rank', 'N/A')}
          </div>
          <div class="oc-sub">{ctx['reddit'].get('mentions','N/A')}次提及</div>
        </div>
      </div>
      <div class="prose">{reasoning.get('macro', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH6 -->
  <div class="section" id="ch6">
    <div class="section-header">
      <span class="ch-num">CH6</span>
      <span class="section-icon">🎯</span>
      <span class="section-title">第六章 · 四情景推演与期望值</span>
      <span class="section-badge {'badge-bull' if '+' in ev_str else 'badge-bear'}">EV = {ev_str} · {'偏多' if '+' in ev_str else '偏空'}</span>
    </div>
    <div class="section-body">
      <div class="scenario-grid">
        <div class="sc-card sc-bull">
          <div class="sc-name">🚀 情景A · 最强看涨</div>
          <div class="sc-prob">突破阻力 + 催化剂超预期 · 概率 {probs[0]*100:.0f}%</div>
          <div class="sc-price">${sc_a_lo:.0f}–${sc_a_hi:.0f}</div>
          <div class="sc-gain" style="color:var(--green2)">ML 7日预期 {ctx['ml_7d']:+.1f}%</div>
          <div class="sc-note">{res_label}触发，阻力位被突破后做市商Delta对冲形成加速上涨。</div>
        </div>
        <div class="sc-card sc-bull">
          <div class="sc-name">📈 情景B · 温和看涨</div>
          <div class="sc-prob">催化剂达预期 · 概率 {probs[1]*100:.0f}%</div>
          <div class="sc-price">${sc_b_lo:.0f}–${sc_b_hi:.0f}</div>
          <div class="sc-gain" style="color:var(--green2)">P/C={ctx['put_call_ratio']} 支持</div>
          <div class="sc-note">期权流{ctx['flow_direction']}，近端支撑稳固，温和上涨定价合理。</div>
        </div>
        <div class="sc-card sc-meh">
          <div class="sc-name">📉 情景C · 温和看跌</div>
          <div class="sc-prob">催化剂不达预期 · 概率 {probs[2]*100:.0f}%</div>
          <div class="sc-price">${sc_c_hi:.0f}–${sc_c_lo:.0f}</div>
          <div class="sc-gain" style="color:var(--gold2)">支撑 ${supports[0]['strike'] if supports else 'N/A'}</div>
          <div class="sc-note">OI {f"{supports[0]['oi']:,.0f}" if supports else "N/A"} 提供缓冲，量能萎缩限制下行弹性。</div>
        </div>
        <div class="sc-card sc-bear">
          <div class="sc-name">💥 情景D · 极端风险</div>
          <div class="sc-prob">催化剂暴雷 + 宏观恶化 · 概率 {probs[3]*100:.0f}%</div>
          <div class="sc-price">${sc_d_lo:.0f}–${sc_d_hi:.0f}</div>
          <div class="sc-gain" style="color:var(--red2)">高风险情景</div>
          <div class="sc-note">风险：{', '.join(ctx['bear_signals'][:2]) or 'IV Skew偏高, 宏观不确定'}。深部支撑防线。</div>
        </div>
      </div>

      {trade_grid_html}

      <div class="ev-box">
        <div class="ev-left">
          <div class="label">期望收益率（EV）</div>
          <div class="big">{ev_str}</div>
          <div style="font-size:11px;color:var(--text2);margin-top:3px;">综合胜率 {win_rate:.1f}%</div>
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
      <span class="ch-num">CH7</span>
      <span class="section-icon">⚠️</span>
      <span class="section-title">第七章 · 主要风险清单</span>
      <span class="section-badge badge-neutral">{len(smart_risks)} 项风险信号</span>
    </div>
    <div class="section-body">
      <div class="risk-list">
        {risk_items_html}
      </div>
      <div class="divider"></div>
      <div class="prose">{reasoning.get('risk', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <div class="disclaimer">
    ⚠️ <strong>免责声明：</strong>本报告由 Alpha Hive 量化蜂群系统 + Claude API 混合模式生成，仅供研究参考，不构成投资建议。期权交易存在归零风险，所有交易决策需自行判断和风控。<br>
    <span style="color:var(--text3);font-size:10px;margin-top:4px;display:block;">
      生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · Template C v3.0 · 混合模式 · 数据来源：本地 JSON + Claude API
    </span>
  </div>

</div>
</body>
</html>"""
    return html


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Alpha Hive · 混合模式深度报告生成器 · Template C v3.0")
    parser.add_argument("--ticker", required=True, help="股票代码，如 NVDA、VKTX")
    parser.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD（默认最新）")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM，使用本地生成文本")
    parser.add_argument("--out-dir", default=None, help="输出目录（默认 Alpha Hive 文件夹）")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    out_dir = Path(args.out_dir) if args.out_dir else OUTPUT_DIR

    print(f"\n🐝 Alpha Hive · generate_deep v{VERSION} 混合报告生成器")
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

    # 2b. 加载昨日 JSON + T-7 JSON，构建 Delta 上下文
    prev_path = find_prev_json(ticker, json_path, days_back=1)
    t7_path   = find_prev_json(ticker, json_path, days_back=7)
    ctx["prev"] = extract_simple(load_json(prev_path)) if prev_path else None
    ctx["t7"]   = extract_simple(load_json(t7_path))   if t7_path   else None

    # 计算 delta_context 供 LLM 推理使用
    delta_context = ""
    if ctx["prev"]:
        prev = ctx["prev"]
        score_diff = ctx["final_score"] - prev["final_score"]
        bee_map = [
            ("ChronosBee", float(ctx["chronos"].get("score",0) or 0), prev["chronos"]),
            ("RivalBee",   float(ctx["rival"].get("score",0)   or 0), prev["rival"]),
            ("OracleBee",  float(ctx["oracle"].get("score",0)  or 0), prev["oracle"]),
            ("BuzzBee",    float(ctx["buzz"].get("score",0)    or 0), prev["buzz"]),
            ("ScoutBee",   float(ctx["scout"].get("score",0)   or 0), prev["scout"]),
            ("GuardBee",   float(ctx["guard"].get("score",0)   or 0), prev["guard"]),
            ("BearBee",    float(ctx["bear"].get("score",0)    or 0), prev["bear"]),
        ]
        delta_lines = [f"  {n}: {ps:.1f} → {ts:.1f} ({ts-ps:+.1f})"
                       for n, ts, ps in bee_map if abs(ts - ps) >= 0.3]
        extras = []
        try:
            if prev.get("put_call_ratio") and ctx.get("put_call_ratio") not in (None, "N/A"):
                pd = float(ctx["put_call_ratio"]) - float(prev["put_call_ratio"])
                extras.append(f"  P/C: {float(prev['put_call_ratio']):.2f} → {float(ctx['put_call_ratio']):.2f} ({pd:+.2f})")
        except Exception:
            pass
        try:
            if prev.get("iv_skew") and ctx.get("iv_skew"):
                sd = float(ctx["iv_skew"]) - float(prev["iv_skew"])
                extras.append(f"  IV Skew: {float(prev['iv_skew']):.2f} → {float(ctx['iv_skew']):.2f} ({sd:+.2f})")
        except Exception:
            pass
        delta_context = (
            f"【昨日对比】综合评分 {prev['final_score']:.2f} → {ctx['final_score']:.2f} ({score_diff:+.2f})\n"
            + ("\n".join(delta_lines) if delta_lines else "  各蜂评分变化均 <0.3，基本持平")
            + ("\n" + "\n".join(extras) if extras else "")
        )
        print(f"   📊 Delta: {score_diff:+.2f} | 昨日文件: {prev_path.name if prev_path else '无'}")
    ctx["delta_context"] = delta_context
    print(f"   OracleBee: P/C={ctx['put_call_ratio']}, OI={ctx['total_oi']:,}, Skew={ctx['iv_skew']}")
    print(f"   催化剂: {len(ctx['catalysts'])} 个 | 异常流: {len(ctx['unusual_activity'])} 笔")
    _pre_conflicts = detect_conflicts(ctx)
    if _pre_conflicts:
        print(f"   ⚡ 检测到 {len(_pre_conflicts)} 个信号矛盾：" + " | ".join(
            c[2:c.index('↔')].strip() if '↔' in c else c[2:30] for c in _pre_conflicts
        ))

    # 3. LLM 深度推理
    sections = ["swarm_analysis", "resonance", "catalyst", "options", "macro", "scenario", "risk"]
    reasoning = {}

    api_key = get_api_key()
    use_llm = not args.no_llm and api_key

    if use_llm:
        print(f"\n🤖 Claude API 深度推理中（两步链式 + 跨章上下文）...")
        ctx["master_thesis"] = ""  # 初始化，CH1跑完后填入

        # Phase 1：CH1 先跑，提取核心论点作为后续章节的上下文
        print(f"   ✍️  swarm_analysis (Phase 1)...", end="", flush=True)
        reasoning["swarm_analysis"] = llm_reason(ctx, "swarm_analysis", api_key)
        ctx["master_thesis"] = extract_thesis(reasoning["swarm_analysis"])
        _thesis_preview = ctx["master_thesis"][:50]
        _thesis_suffix  = "..." if len(ctx["master_thesis"]) > 50 else ""
        print(f" ✅  → 论点: {_thesis_preview}{_thesis_suffix}")

        # Phase 2：其余章节带入 master_thesis
        for sec in ["resonance", "catalyst", "options", "macro", "scenario", "risk"]:
            print(f"   ✍️  {sec}...", end="", flush=True)
            reasoning[sec] = llm_reason(ctx, sec, api_key)
            print(" ✅")
    else:
        ctx["master_thesis"] = ""
        reason = "本地模式" if args.no_llm else "未找到 API Key"
        print(f"\n📝 本地叙事生成（{reason}）...")
        for sec in sections:
            reasoning[sec] = _local_fallback(ctx, sec)
        print("   ✅ 全部章节完成")

    # 4. 生成 HTML
    print("\n📄 渲染 Template C v3.0 HTML...")
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
