"""
Alpha Hive - LLM 服务层

封装 Claude API 调用，提供统一的 LLM 推理接口。
无 API Key 时自动降级到规则引擎。

用量控制：
- 默认使用 claude-haiku-4-5（最低成本，~$0.02/ticker）
- 每次调用有 token 预算限制
- 内置重试 + 超时 + 降级
"""

import json
import logging as _logging
import os
import threading
from datetime import date
from typing import Dict, Optional, List

_log = _logging.getLogger("alpha_hive.llm_service")

# API Key 加载优先级：环境变量 > 配置文件
_api_key: Optional[str] = None
_client = None
_lock = threading.Lock()

# Token 使用追踪（每日自动重置）
_token_usage = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_cost_usd": 0.0,
    "call_count": 0,
}
_budget_date: Optional[date] = None  # 当前预算对应的日期


def _maybe_reset_daily_budget() -> None:
    """如果日期变更，重置 token 使用计数器（线程安全）"""
    global _budget_date
    today = date.today()
    with _lock:
        if _budget_date != today:
            if _budget_date is not None:
                _log.info(
                    "LLM 每日预算重置：上一日（%s）消耗 $%.4f / %d 次调用",
                    _budget_date,
                    _token_usage["total_cost_usd"],
                    _token_usage["call_count"],
                )
            _token_usage["input_tokens"] = 0
            _token_usage["output_tokens"] = 0
            _token_usage["total_cost_usd"] = 0.0
            _token_usage["call_count"] = 0
            _budget_date = today

# 定价（claude-haiku-4-5）
_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.0 / 1_000_000, "output": 5.0 / 1_000_000},
    "claude-sonnet-4-6": {"input": 3.0 / 1_000_000, "output": 15.0 / 1_000_000},
}


def _load_api_key() -> Optional[str]:
    """加载 API Key"""
    # 1. 环境变量
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    # 2. 配置文件
    key_files = [
        os.path.expanduser("~/.anthropic_api_key"),
        os.path.expanduser("~/.alpha_hive_anthropic_key"),
    ]
    for f in key_files:
        try:
            # 检查文件权限：API key 文件应仅限 owner 读写（0600）
            import stat
            st = os.stat(f)
            mode = st.st_mode & 0o777
            if mode & 0o077:
                _log.warning(
                    "API key file %s has insecure permissions %o (should be 0600), skipping",
                    f, mode,
                )
                continue
            with open(f) as fh:
                k = fh.read().strip()
                if k.startswith("sk-"):
                    return k
        except (OSError, UnicodeDecodeError):
            _log.debug("Could not read API key from %s", f)

    return None


def _get_client():
    """获取 Anthropic client（懒加载）"""
    global _client, _api_key
    if _disabled:
        return None
    with _lock:
        if _client is not None:
            return _client

        _api_key = _load_api_key()
        if not _api_key:
            return None

        try:
            import anthropic
            _client = anthropic.Anthropic(api_key=_api_key)
            return _client
        except (ImportError, ValueError, OSError) as e:
            _log.debug("Failed to initialize Anthropic client: %s", e)
            return None


_disabled: bool = False


def disable() -> None:
    """临时禁用 LLM（本次进程内有效，规则引擎模式）"""
    global _disabled
    _disabled = True


def is_available() -> bool:
    """检查 LLM 服务是否可用"""
    if _disabled:
        return False
    return _get_client() is not None


def get_usage() -> Dict:
    """获取 token 使用统计"""
    with _lock:
        return dict(_token_usage)


def call(
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    temperature: float = 0.3,
    timeout: float = 30.0,
    cache_system: bool = True,
) -> Optional[str]:
    """
    调用 Claude API

    Args:
        prompt: 用户提示
        system: 系统提示
        model: 模型 ID
        max_tokens: 最大输出 token
        temperature: 温度 (0-1)
        timeout: 超时秒数
        cache_system: 是否缓存 system prompt（减少重复 token 费用）

    Returns:
        模型输出文本，失败返回 None
    """
    client = _get_client()
    if client is None:
        return None

    # 每日预算重置（跨日自动清零）
    _maybe_reset_daily_budget()

    # 预算硬限制：调用前估算本次费用，原子比较防止超支
    try:
        from config import LLM_CONFIG as _llm_cfg
        _budget = _llm_cfg.get("daily_budget_usd", 1.0)
    except (ImportError, KeyError):
        _budget = 1.0
    pricing = _PRICING.get(model, {"input": 1.0 / 1_000_000, "output": 5.0 / 1_000_000})
    # 保守估算：假设输出填满 max_tokens（实际通常远小于此）
    _estimated_cost = max_tokens * pricing["output"]
    with _lock:
        _current_cost = _token_usage["total_cost_usd"]
        if _current_cost + _estimated_cost >= _budget:
            _log.warning(
                "LLM 预算预检不通过（已用 $%.3f + 预估 $%.4f >= 上限 $%.2f），自动降级",
                _current_cost, _estimated_cost, _budget,
            )
            return None

    try:
        messages = [{"role": "user", "content": prompt}]

        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
        }
        if system:
            if cache_system:
                # Prompt caching: 相同 system prompt 在多 ticker 调用间复用缓存
                # 首次写入缓存 1.25x 成本，后续读取 0.1x 成本 → 净省 ~60%
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                kwargs["system"] = system

        response = client.messages.create(**kwargs)

        # 提取文本
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        # 追踪用量（含 prompt caching 计费），pricing 已在预算预检处定义
        usage = response.usage
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        non_cached_input = usage.input_tokens - cache_create - cache_read
        cost = (
            non_cached_input * pricing["input"]
            + cache_create * pricing["input"] * 1.25   # 缓存写入 1.25x
            + cache_read * pricing["input"] * 0.10     # 缓存读取 0.1x
            + usage.output_tokens * pricing["output"]
        )

        with _lock:
            _token_usage["input_tokens"] += usage.input_tokens
            _token_usage["output_tokens"] += usage.output_tokens
            _token_usage["total_cost_usd"] += cost
            _token_usage["call_count"] += 1

        return text

    except (ConnectionError, TimeoutError, OSError, ValueError) as e:
        _log.error("LLM API call failed: %s", type(e).__name__)
        _log.debug("LLM API call details:", exc_info=True)
        return None


def call_json(
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    temperature: float = 0.2,
    cache_system: bool = True,
) -> Optional[Dict]:
    """
    调用 Claude API 并解析 JSON 响应

    Returns:
        解析后的 dict，失败返回 None
    """
    text = call(
        prompt, system=system, model=model, max_tokens=max_tokens,
        temperature=temperature, cache_system=cache_system,
    )
    if text is None:
        return None

    # 尝试提取 JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown code block 中提取
    import re
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试找到第一个 { 和最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


# ==================== 高级 API：蜂群专用 ====================

def generate_bear_thesis(
    ticker: str,
    bull_signals: List[Dict],
    bear_signals: List[str],
    insider_data: Optional[Dict] = None,
    options_data: Optional[Dict] = None,
    news_data: Optional[Dict] = None,
) -> Optional[Dict]:
    """
    BearBeeContrarian LLM 推理：生成投资论文级别的看空论点

    规则引擎只能做阈值判断（P/E > 80 = 看空），LLM 能识别：
    - 被市场忽视的隐藏风险
    - 过度乐观的叙事陷阱
    - 多维度看空信号的交叉共振

    Returns:
        {
            "bear_score": float (0-10),
            "thesis": str,           # 核心看空论点（1-2 句）
            "key_risks": list[str],  # 3 条关键风险
            "contrarian_insight": str, # 市场忽视了什么
            "thesis_break": str,     # 看空论点失效条件
        }
    """
    system = (
        "You are a contrarian investment analyst. Your job is to find the strongest "
        "bear case for a stock, even when the bull case looks compelling. "
        "Focus on: hidden risks, over-optimism, valuation concerns, insider behavior "
        "red flags, options market warnings, and narrative traps.\n\n"
        "Output strict JSON with keys: bear_score (float 0-10), thesis (str), "
        "key_risks (list of 3 strings), contrarian_insight (str), thesis_break (str).\n"
        "All text in Chinese."
    )

    bull_text = json.dumps(bull_signals, ensure_ascii=False, default=str)[:800] if bull_signals else "无"
    bear_text = json.dumps(bear_signals, ensure_ascii=False, default=str)[:500] if bear_signals else "无"
    insider_text = json.dumps(insider_data, ensure_ascii=False, default=str)[:400] if insider_data else "无"
    options_text = json.dumps(options_data, ensure_ascii=False, default=str)[:400] if options_data else "无"
    news_text = json.dumps(news_data, ensure_ascii=False, default=str)[:400] if news_data else "无"

    prompt = f"""分析 {ticker} 的看空案例。

## 其他 Agent 的看多信号
{bull_text}

## 规则引擎的看空信号
{bear_text}

## 内幕交易数据
{insider_text}

## 期权流数据
{options_text}

## 新闻情绪数据
{news_text}

请从反对蜂视角，生成最强看空论点。输出 JSON："""

    return call_json(prompt, system=system, max_tokens=400, temperature=0.3)


def _build_sentiment_section(sentiment_context: Optional[Dict]) -> str:
    """构建情绪动态上下文区块（注入 QueenDistiller prompt）"""
    if not sentiment_context:
        return ""
    parts = ["\n## 情绪动态"]
    m3 = sentiment_context.get("momentum_3d")
    regime = sentiment_context.get("momentum_regime", "unknown")
    if m3 is not None:
        parts.append(f"- 情绪动量(3d): {m3:+d} ppt ({regime})")
    div_type = sentiment_context.get("divergence_type", "none")
    severity = sentiment_context.get("divergence_severity", 0)
    if div_type != "none":
        parts.append(f"- 情绪-价格背离: {div_type} (严重度 {severity}/3)")
    # 冲突驱动上下文
    _cl = sentiment_context.get("conflict_level", "none")
    _ci = sentiment_context.get("conflict_info")
    if _cl == "heavy" and _ci:
        parts.append(f"- ⚠️ Agent 方向冲突: 重度 ({_ci.get('bullish_agents', 0)}多 vs {_ci.get('bearish_agents', 0)}空)")
        parts.append(f"  DQ加权再投票结果: {_ci.get('resolved_direction', 'neutral')}，折扣 {_ci.get('conflict_discount', 0)}")
    elif _cl == "moderate":
        parts.append("- Agent 方向冲突: 中度（需关注分歧维度）")
    if len(parts) == 1:
        return ""  # 无有效数据则不注入
    return "\n".join(parts) + "\n"


def distill_with_reasoning(
    ticker: str,
    agent_results: List[Dict],
    dim_scores: Dict,
    resonance: Dict,
    rule_score: float,
    rule_direction: str,
    bear_result: Optional[Dict] = None,
    sentiment_context: Optional[Dict] = None,
) -> Optional[Dict]:
    """
    QueenDistiller LLM 蒸馏：基于 6 Agent 的结构化数据，用 Claude 做最终推理

    Args:
        ticker: 股票代码
        agent_results: 6 个 Agent 的分析结果
        dim_scores: 5 维评分 {signal: x, catalyst: x, ...}
        resonance: 共振检测结果
        rule_score: 规则引擎计算的基础分
        rule_direction: 规则引擎计算的方向
        bear_result: BearBeeContrarian 的完整分析结果（含 LLM 看空论点）

    Returns:
        {
            "final_score": float,      # LLM 调整后的最终分
            "direction": str,          # LLM 判断的方向
            "reasoning": str,          # 中文推理链
            "key_insight": str,        # 核心洞察（一句话）
            "risk_flag": str,          # 风险标记
            "confidence": float,       # 0-1 置信度
            "narrative": str,          # 2-3 句投资级别叙事摘要
            "bull_bear_synthesis": str, # 多空综合判断
            "contrarian_view": str,    # 少数意见摘要
        }
    """
    system = """你是 Alpha Hive 的 QueenDistiller（最终蒸馏蜂）。
你的任务是基于 6 个专业 Agent 的分析结果，做出最终投资机会评估，并生成投资级别的叙事摘要。

输出要求（严格 JSON 格式）：
1. final_score: 0-10 浮点数（可以与规则引擎不同，但需要给出理由）
2. direction: "bullish" / "bearish" / "neutral"
3. reasoning: 2-3 句中文推理链（因为…所以…）
4. key_insight: 一句话核心洞察
5. risk_flag: 主要风险（一句话）
6. confidence: 0.0-1.0
7. narrative: 2-3 句投资级别叙事摘要（像投行研报的执行摘要，需包含核心催化剂和风险）
8. bull_bear_synthesis: 一句话多空综合判断（如"短期看多但中期需警惕…"）
9. contrarian_view: 一句话少数意见摘要（BearBee 的最强反对观点）

重要：你必须按以下 5 步顺序推理，在 reasoning 字段中体现全部步骤：

Step 1 — 证据扫描：哪些 Agent 给出了强信号（≥7）？哪些维度数据质量低（data_real_pct < 60%）？
Step 2 — 矛盾检测：Agent 之间是否存在方向冲突？BearBee 的反驳是否有力？情绪动量是否与价格走势背离？
Step 3 — 数据质量折扣：对 data_real_pct < 60% 的维度降权，对 fallback 数据标注不确定性
Step 4 — 综合判断：结合 Step 1-3，给出 final_score 和 direction，解释为什么与规则引擎一致或不同
Step 5 — 自我验证：你的判断是否存在确认偏误？如果你错了，最可能的原因是什么？写入 risk_flag"""

    # 构建 Agent 摘要
    agent_summaries = []
    for r in agent_results:
        if r and "error" not in r:
            dq = r.get("data_quality", {})
            real_pct = sum(1 for v in dq.values() if v == "real") / max(len(dq), 1) * 100 if isinstance(dq, dict) else 0
            agent_summaries.append({
                "agent": r.get("source", "?"),
                "dimension": r.get("dimension", "?"),
                "score": r.get("score"),
                "direction": r.get("direction"),
                "discovery": r.get("discovery", "")[:100],
                "data_real_pct": f"{real_pct:.0f}%",
            })

    # 构建 BearBee 看空论点区块
    bear_section = ""
    if bear_result and "error" not in bear_result:
        bear_details = bear_result.get("details", {})
        bear_section = f"""
## BearBeeContrarian 看空分析
- 看空强度: {bear_details.get('bear_score', 'N/A')}/10
- 看空信号: {', '.join(bear_details.get('bearish_signals', [])[:4])}
- LLM 看空论点: {bear_result.get('llm_thesis', 'N/A')}
- LLM 关键风险: {', '.join(bear_result.get('llm_key_risks', [])[:3])}
- LLM 反对洞察: {bear_result.get('llm_contrarian_insight', 'N/A')}
"""

    prompt = f"""分析 **{ticker}** 的投资机会。

## 6 Agent 分析结果
{json.dumps(agent_summaries, ensure_ascii=False)}

## 5 维评分
{json.dumps(dim_scores, ensure_ascii=False)}

## 共振检测
- 共振: {"是" if resonance.get("resonance_detected") else "否"}
- 支持 Agent 数: {resonance.get("supporting_agents", 0)}
- 置信度增强: {resonance.get("confidence_boost", 0)}%

## 规则引擎基础分
- 评分: {rule_score}/10
- 方向: {rule_direction}
{_build_sentiment_section(sentiment_context)}{bear_section}
请输出 JSON："""

    result = call_json(prompt, system=system, max_tokens=800, temperature=0.3)
    return result


def analyze_news_sentiment(
    ticker: str,
    headlines: List[str],
) -> Optional[Dict]:
    """
    BuzzBeeWhisper LLM 新闻分析：用 Claude 分析新闻标题的语义情绪

    Returns:
        {
            "sentiment_score": float (0-10),
            "sentiment_label": "bullish" / "bearish" / "neutral",
            "key_theme": str,           # 主要主题
            "reasoning": str,           # 推理
        }
    """
    if not headlines:
        return None

    system = """你是金融新闻情绪分析师。分析给定的新闻标题，判断整体情绪方向。
输出严格 JSON：
- sentiment_score: 0-10（5=中性，>7=明确看多，<3=明确看空）
- sentiment_label: "bullish"/"bearish"/"neutral"
- key_theme: 一句话概括新闻主题（中文）
- reasoning: 一句话推理（中文）"""

    titles_text = "\n".join(f"- {h}" for h in headlines[:15])
    prompt = f"分析 {ticker} 的以下新闻标题情绪：\n\n{titles_text}\n\n输出 JSON："

    return call_json(prompt, system=system, max_tokens=256, temperature=0.2)


# ==================== Agent 内部 LLM 推理（P1 升级）====================

def interpret_insider_trades(
    ticker: str,
    insider_data: Dict,
    stock_data: Dict,
) -> Optional[Dict]:
    """
    ScoutBeeNova LLM 推理：解读内幕交易意图（规则引擎无法判断计划性卖出 vs 信心丧失）

    Returns:
        {
            "intent_score": float (0-10, 10=强烈买入信心),
            "intent_label": "accumulation"/"distribution"/"planned_exit"/"neutral",
            "intent_reasoning": str,  # 中文一句话推理
            "red_flags": list[str],   # 值得注意的警示
        }
    """
    if not insider_data or insider_data.get("total_filings", 0) == 0:
        return None

    system = """你是内幕交易解读专家。分析 SEC Form 4 内幕交易数据，判断真实意图。

关键区分：
- 计划性卖出（10b5-1 预设计划）≠ 信心动摇：需降低看空权重
- CEO/CFO 主动买入（非期权行权）= 强烈看多信号
- 高管期权行权后立即卖出 = 流动性操作，非利空
- 多位高管同时大额卖出 = 真正风险信号

输出严格 JSON：
- intent_score: 0-10（10=极强看多信号，0=强烈看空信号，5=中性）
- intent_label: "accumulation"/"distribution"/"planned_exit"/"option_exercise"/"neutral"
- intent_reasoning: 一句话中文推理
- red_flags: 值得警惕的信号列表（可为空列表）"""

    notable = insider_data.get("notable_trades", [])[:5]
    prompt = f"""分析 {ticker} 的内幕交易数据：

买入总额: ${insider_data.get('dollar_bought', 0):,.0f}
卖出总额: ${insider_data.get('dollar_sold', 0):,.0f}
申报数量: {insider_data.get('total_filings', 0)} 份
规则引擎判断: {insider_data.get('insider_sentiment', 'neutral')}
摘要: {insider_data.get('summary', '无')}

重要交易明细:
{json.dumps(notable, ensure_ascii=False)}

股票价格: ${stock_data.get('price', 0):.2f}
5日动量: {stock_data.get('momentum_5d', 0):+.1f}%

输出 JSON："""

    return call_json(prompt, system=system, max_tokens=300, temperature=0.2)


def interpret_catalyst_impact(
    ticker: str,
    catalysts: List[Dict],
    stock_data: Dict,
) -> Optional[Dict]:
    """
    ChronosBeeHorizon LLM 推理：评估催化剂的市场影响方向和强度
    （规则引擎只知道"有催化剂"，不知道是利多还是利空）

    Returns:
        {
            "impact_score": float (0-10),
            "impact_direction": "bullish"/"bearish"/"neutral",
            "impact_reasoning": str,
            "key_catalyst": str,  # 最重要的一个催化剂
        }
    """
    if not catalysts:
        return None

    system = """你是催化剂影响力评估专家，熟悉股票事件驱动交易。

评估逻辑：
- 财报（earnings）：近期动量强 + 分析师预期偏低 → 看多；动量弱 + 预期高 → 看空
- 产品发布/FDA 批准：通常看多催化剂
- 监管调查/诉讼：看空催化剂
- 时间窗口越近（< 7 天）权重越高
- 多个正向催化剂叠加 → 强看多

输出严格 JSON：
- impact_score: 0-10（催化剂整体吸引力，10=极强正向催化）
- impact_direction: "bullish"/"bearish"/"neutral"
- impact_reasoning: 一句话中文推理（说明最核心的催化逻辑）
- key_catalyst: 最值得关注的催化剂名称（中文）"""

    prompt = f"""评估 {ticker} 的催化剂影响：

股票当前状态:
- 价格: ${stock_data.get('price', 0):.2f}
- 5日动量: {stock_data.get('momentum_5d', 0):+.1f}%
- 20日波动率: {stock_data.get('volatility_20d', 0):.1f}%

即将到来的催化剂:
{json.dumps(catalysts[:6], ensure_ascii=False)}

输出 JSON："""

    return call_json(prompt, system=system, max_tokens=256, temperature=0.2)


def interpret_options_flow(
    ticker: str,
    options_result: Dict,
    stock_data: Dict,
) -> Optional[Dict]:
    """
    OracleBeeEcho LLM 推理：解读期权流结构背后的聪明钱意图
    （规则引擎只看阈值，LLM 能识别结构性信号组合）

    Returns:
        {
            "smart_money_score": float (0-10),
            "smart_money_direction": "bullish"/"bearish"/"neutral",
            "flow_reasoning": str,
            "signal_type": str,  # "unusual_call_sweep"/"protective_puts"/"vol_crush"等
        }
    """
    if not options_result:
        return None

    system = """你是期权流分析专家，擅长从期权结构中识别聪明钱意图。

关键模式：
- 低 IV Rank (<30) + 低 P/C Ratio (<0.7) → 安静积累，看多
- 高 IV Rank (>70) + 低 P/C Ratio → 看多但恐慌对冲，方向待定
- 高 IV Rank (>70) + 高 P/C Ratio (>1.5) → 明确看空，保护性买入
- IV Rank 中等 + P/C 急剧上升 → 可能有负面消息预期
- 短期 OTM Call 大量买入 = 方向性多头押注

输出严格 JSON：
- smart_money_score: 0-10（10=极强看多信号，0=极强看空信号，5=中性）
- smart_money_direction: "bullish"/"bearish"/"neutral"
- flow_reasoning: 一句话中文推理
- signal_type: 识别到的期权信号类型（英文简短标签）"""

    prompt = f"""解读 {ticker} 的期权流数据：

期权分析结果:
{json.dumps({k: v for k, v in options_result.items() if k not in ('raw_chain',)}, ensure_ascii=False)}

股票状态:
- 价格: ${stock_data.get('price', 0):.2f}
- 5日动量: {stock_data.get('momentum_5d', 0):+.1f}%
- 20日波动率: {stock_data.get('volatility_20d', 0):.1f}%

输出 JSON："""

    return call_json(prompt, system=system, max_tokens=256, temperature=0.2)


def synthesize_agent_conflicts(
    ticker: str,
    pheromone_snapshot: List[Dict],
    resonance: Dict,
) -> Optional[Dict]:
    """
    GuardBeeSentinel LLM 推理：识别 Agent 间矛盾信号，给出风险级别评估
    （规则引擎只看一致性百分比，LLM 能识别"哪种矛盾更危险"）

    Returns:
        {
            "risk_score": float (0-10, 10=高风险/信号冲突严重),
            "conflict_type": "coherent"/"minor_divergence"/"major_conflict"/"data_quality_issue",
            "guard_reasoning": str,
            "recommended_action": str,  # "proceed"/"caution"/"avoid"
        }
    """
    if not pheromone_snapshot:
        return None

    system = """你是蜂群信号质量监控专家（Guard Bee Sentinel）。

你的职责是识别多 Agent 系统中的矛盾信号和数据质量问题。

矛盾严重性分级：
- coherent: 所有 Agent 方向一致，分数接近 → 低风险
- minor_divergence: 1-2 个 Agent 有轻微分歧，其余一致 → 可接受
- major_conflict: 方向对立且高分 Agent 存在冲突 → 高风险，需降权
- data_quality_issue: 多个 Agent 数据不可用/降级 → 置信度不足

输出严格 JSON：
- risk_score: 0-10（10=信号严重冲突，建议回避；0=高度一致，可信）
- conflict_type: 如上四类之一
- guard_reasoning: 一句话中文推理（说明主要冲突点或一致性来源）
- recommended_action: proceed / caution / avoid"""

    snapshot_clean = [
        {"agent": e.get("agent_id", "?")[:12], "dir": e.get("direction", "?"),
         "score": e.get("self_score", 0), "strength": e.get("pheromone_strength", 0)}
        for e in (pheromone_snapshot or [])
    ]

    prompt = f"""评估 {ticker} 的多 Agent 信号一致性：

信息素板快照（各 Agent 发布的信号）:
{json.dumps(snapshot_clean, ensure_ascii=False)}

共振检测:
- 共振触发: {"是" if resonance.get("resonance_detected") else "否"}
- 支持 Agent 数: {resonance.get("supporting_agents", 0)}
- 主导方向: {resonance.get("direction", "neutral")}

输出 JSON："""

    return call_json(prompt, system=system, max_tokens=256, temperature=0.2)


# ==================== Phase 2 升级：跨标的 & 历史类比 ====================

def analyze_cross_ticker_patterns(
    board_snapshot: List[Dict],
    distilled_scores: Dict,
    sector_map: Dict,
) -> Optional[Dict]:
    """
    跨标的关联分析：检测板块轮动、竞争动态、关联风险、板块趋势。
    每次扫描仅调用 1 次（非逐 ticker），覆盖全部标的。

    Args:
        board_snapshot: PheromoneBoard.compact_snapshot() — 所有 ticker 的信号摘要
        distilled_scores: {ticker: {"final_score": float, "direction": str, ...}}
        sector_map: {ticker: sector_name} from config.WATCHLIST

    Returns:
        {
            "sector_momentum": {"Technology": "leading", ...},
            "cross_ticker_insights": [{"tickers": [...], "type": str, "insight": str}],
            "correlation_warnings": [str],
            "sector_rotation_signal": str,
            "portfolio_adjustment_hints": [str],
        }
    """
    if not distilled_scores:
        return None

    system = """你是投资组合策略分析师，专长跨标的关联分析和板块轮动检测。

分析多个标的之间的关系，输出严格 JSON（保持简短，每条洞察不超过 20 字）：
1. sector_momentum: {板块: "leading"/"lagging"/"neutral"}
2. cross_ticker_insights: 最多 3 条，格式 {"tickers": ["X","Y"], "type": "competitive/correlated/divergent", "insight": "简短中文"}
3. correlation_warnings: 风险警告字符串列表，最多 2 条，每条 ≤ 15 字
4. sector_rotation_signal: 一句话（≤ 20 字）
5. portfolio_adjustment_hints: 1-2 条建议，每条 ≤ 15 字

所有文本用中文，追求极简。"""

    # 按板块分组构建紧凑摘要
    sector_groups = {}
    for tk, data in distilled_scores.items():
        sector = sector_map.get(tk, "Other")
        if sector not in sector_groups:
            sector_groups[sector] = []
        sector_groups[sector].append({
            "ticker": tk,
            "score": data.get("final_score", 5.0),
            "dir": data.get("direction", "neutral"),
        })

    # 紧凑信息素板摘要（仅保留关键字段）
    board_compact = []
    if isinstance(board_snapshot, list):
        for e in board_snapshot[:30]:
            if isinstance(e, dict):
                board_compact.append({
                    "tk": e.get("ticker", "?"),
                    "agent": e.get("agent_id", "?")[:10],
                    "dir": e.get("direction", "?"),
                    "s": e.get("self_score", 0),
                })

    prompt = f"""分析以下 {len(distilled_scores)} 个标的的跨标的关系。

## 按板块分组的标的评分
{json.dumps(sector_groups, ensure_ascii=False)}

## 信息素板信号摘要（各 Agent 发布的关键信号）
{json.dumps(board_compact, ensure_ascii=False)}

请输出 JSON："""

    return call_json(prompt, system=system, max_tokens=600, temperature=0.3)


def find_historical_analogy(
    ticker: str,
    current_signals: Dict,
    historical_memories: List[Dict],
    historical_outcomes: List[Dict],
) -> Optional[Dict]:
    """
    历史类比推理：将当前信号模式与历史场景对比，预测潜在走向。

    Args:
        ticker: 股票代码
        current_signals: 当前分析摘要
            {direction, final_score, key_signals: [...], bear_signals: [...]}
        historical_memories: VectorMemory 搜索结果 (top-5 相似)
            [{document, direction, score, date, agent_id, similarity}, ...]
        historical_outcomes: MemoryStore 含实际回报的历史记忆
            [{date, direction, self_score, outcome_return_t1, t7, t30}, ...]

    Returns:
        {
            "analogy_found": bool,
            "analogy_date": str,
            "analogy_summary": str,
            "historical_outcome": {"t1": str, "t7": str, "t30": str},
            "similarity_score": float,
            "key_differences": str,
            "confidence_adjustment": float,
            "warning": str,
        }
    """
    if not historical_memories or len(historical_memories) < 3:
        return None

    system = """你是投资模式匹配专家，擅长从历史数据中找到与当前情况最相似的先例。

分析当前信号与历史记录的相似度，输出严格 JSON：
1. analogy_found: bool（是否找到有意义的历史类比）
2. analogy_date: 最相似的历史日期
3. analogy_summary: 一句话类比摘要（如"当前形态类似X月X日：内幕买入+共振看多"）
4. historical_outcome: {"t1": "+X.X%", "t7": "+X.X%", "t30": "+X.X%"}（历史实际结果）
5. similarity_score: 0.0-1.0（当前与历史的整体相似度）
6. key_differences: 一句话关键差异（当前 vs 历史有何不同）
7. confidence_adjustment: -0.1 到 +0.1（历史类比对当前置信度的调整建议）
8. warning: 风险警示（如"历史类比T+7回撤概率高"，无则空字符串）

所有文本用中文。重要：
- 如果历史结果数据不完整（t7/t30 未知），只用已知数据
- 相似度 < 0.5 时 analogy_found 应为 false
- confidence_adjustment 应保守，极端情况才到 ±0.1"""

    # 构建历史记忆摘要
    mem_text = []
    for m in historical_memories[:6]:
        mem_text.append({
            "date": m.get("date", "?"),
            "dir": m.get("direction", "?"),
            "score": m.get("score", 0),
            "signal": m.get("document", "")[:100],
            "sim": m.get("similarity", 0),
        })

    # 构建历史结果摘要（仅含有实际回报的记录）
    outcomes_text = []
    for o in historical_outcomes[:10]:
        t1 = o.get("outcome_return_t1")
        t7 = o.get("outcome_return_t7")
        t30 = o.get("outcome_return_t30")
        if t1 is not None or t7 is not None:
            outcomes_text.append({
                "date": o.get("date", "?"),
                "dir": o.get("direction", "?"),
                "score": o.get("self_score", 0),
                "t1": f"{t1:+.1f}%" if t1 is not None else "N/A",
                "t7": f"{t7:+.1f}%" if t7 is not None else "N/A",
                "t30": f"{t30:+.1f}%" if t30 is not None else "N/A",
            })

    prompt = f"""为 {ticker} 寻找历史类比。

## 当前信号
{json.dumps(current_signals, ensure_ascii=False, default=str)[:500]}

## 历史相似记忆（按语义相似度排序）
{json.dumps(mem_text, ensure_ascii=False)}

## 历史实际结果（T+1/7/30 回报）
{json.dumps(outcomes_text, ensure_ascii=False) if outcomes_text else "暂无历史回报数据"}

请输出 JSON："""

    return call_json(prompt, system=system, max_tokens=400, temperature=0.2)


# ==================== P2 升级：论文失效检测 + 预测复盘 ====================


def detect_thesis_breaks(
    ticker: str,
    original_thesis: Dict,
    recent_news: List[str],
    current_metrics: Dict,
) -> Optional[Dict]:
    """
    论文失效 LLM 检测：分析最新消息和指标变化，判断原始投资论文是否已失效

    Args:
        ticker: 股票代码
        original_thesis: 原始投资论文 {direction, key_insight, risk_flag, narrative}
        recent_news: 最近 7 天的相关新闻标题列表
        current_metrics: 当前关键指标 {price, momentum_5d, volatility_20d, ...}

    Returns:
        {
            "thesis_intact": bool,          # 论文是否仍然有效
            "break_severity": str,          # "none"/"warning"/"critical"
            "break_reason": str,            # 中文说明
            "new_risk_factors": list[str],  # 新发现的风险因素
            "recommended_action": str,      # "hold"/"reduce"/"exit"
        }
    """
    if not original_thesis:
        return None

    system = """你是投资论文失效检测专家。评估原始投资假设是否仍然成立。

核心逻辑：
- 论文失效 ≠ 短期波动：忽略日常噪音，关注基本面变化
- 关键催化剂消失/延迟 = warning 级别
- 反向重大事件（管理层变动、监管打击、财务造假）= critical 级别
- 竞争格局根本性变化 = critical 级别

输出严格 JSON：
- thesis_intact: bool（论文是否仍有效）
- break_severity: "none"/"warning"/"critical"
- break_reason: 一句话中文说明
- new_risk_factors: 新发现的风险列表（最多 3 条，中文）
- recommended_action: "hold"/"reduce"/"exit\""""

    thesis_text = json.dumps(original_thesis, ensure_ascii=False, default=str)[:400]
    news_text = "\n".join(f"- {h}" for h in (recent_news or [])[:10])
    metrics_text = json.dumps(current_metrics, ensure_ascii=False, default=str)[:300]

    prompt = f"""评估 {ticker} 的投资论文是否失效。

## 原始论文
{thesis_text}

## 最近 7 天新闻
{news_text if news_text else "无新闻"}

## 当前指标
{metrics_text}

输出 JSON："""

    return call_json(prompt, system=system, max_tokens=256, temperature=0.2)


def analyze_prediction_miss(
    ticker: str,
    prediction: Dict,
    actual_outcome: Dict,
) -> Optional[Dict]:
    """
    预测复盘 LLM 分析：找出预测失败的根本原因

    Args:
        ticker: 股票代码
        prediction: 原始预测 {date, direction, score, key_insight, narrative}
        actual_outcome: 实际结果 {return_t1, return_t7, return_t30, direction_correct}

    Returns:
        {
            "miss_category": str,           # 失败类别
            "root_cause": str,              # 中文根因分析
            "agent_blame": str,             # 哪个维度判断最失误
            "lesson_learned": str,          # 应吸取的教训
            "weight_suggestion": dict,      # 建议权重调整 {dimension: delta}
        }
    """
    if not prediction or not actual_outcome:
        return None

    system = """你是投资预测复盘分析师。分析预测失败的根本原因，提供改进建议。

失败分类：
- timing_error: 方向对但时间窗口错（催化剂延迟/提前）
- macro_shift: 宏观环境突变（利率、地缘政治）
- data_quality: 输入数据质量差（数据源故障、信息过时）
- model_bias: 模型系统性偏差（过度看多/看空）
- black_swan: 不可预见事件
- narrative_trap: 被市场叙事误导

输出严格 JSON：
- miss_category: 如上六类之一
- root_cause: 一句话中文根因
- agent_blame: 哪个分析维度最失误（signal/catalyst/sentiment/odds/risk_adj）
- lesson_learned: 一句话教训
- weight_suggestion: 权重调整建议（如 {"sentiment": -0.05, "catalyst": +0.05}）"""

    pred_text = json.dumps(prediction, ensure_ascii=False, default=str)[:400]
    outcome_text = json.dumps(actual_outcome, ensure_ascii=False, default=str)[:300]

    prompt = f"""复盘 {ticker} 的预测失败。

## 原始预测
{pred_text}

## 实际结果
{outcome_text}

输出 JSON："""

    return call_json(prompt, system=system, max_tokens=256, temperature=0.2)


# ==================== P3 升级：简报叙事润色 ====================


def polish_briefing_narrative(
    ticker: str,
    raw_narrative: str,
    score: float,
    direction: str,
) -> Optional[str]:
    """
    简报叙事润色：把规则引擎拼接的投资摘要润色为投行研报风格

    仅用于最终输出阶段，不影响评分或决策。
    成本极低（输入 ~200 token，输出 ~100 token）。

    Args:
        ticker: 股票代码
        raw_narrative: 原始拼接叙事
        score: 综合评分
        direction: 方向

    Returns:
        润色后的中文叙事文本，失败返回 None（调用方保留原始文本）
    """
    if not raw_narrative or len(raw_narrative) < 20:
        return None

    system = (
        "你是投资简报编辑。把粗糙的分析拼接文本润色为投行研报执行摘要风格。"
        "要求：2-3 句中文，包含核心催化剂和风险，语言精炼专业。"
        "直接输出润色后文本，不要 JSON 包装。"
    )

    prompt = f"{ticker}（{direction} {score:.1f}/10）：{raw_narrative[:300]}"

    return call(prompt, system=system, max_tokens=150, temperature=0.3)
