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
from typing import Dict, Optional, List

_log = _logging.getLogger("alpha_hive.llm_service")

# API Key 加载优先级：环境变量 > 配置文件
_api_key: Optional[str] = None
_client = None
_lock = threading.Lock()

# Token 使用追踪
_token_usage = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_cost_usd": 0.0,
    "call_count": 0,
}

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

    Returns:
        模型输出文本，失败返回 None
    """
    client = _get_client()
    if client is None:
        return None

    # 预算硬限制：超过每日上限后停止调用，防止意外费用
    try:
        from config import LLM_CONFIG as _llm_cfg
        _budget = _llm_cfg.get("daily_budget_usd", 1.0)
    except (ImportError, KeyError):
        _budget = 1.0
    with _lock:
        _current_cost = _token_usage["total_cost_usd"]
    if _current_cost >= _budget:
        _log.warning("LLM 每日预算已耗尽（$%.3f >= $%.2f），自动降级", _current_cost, _budget)
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
            kwargs["system"] = system

        response = client.messages.create(**kwargs)

        # 提取文本
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        # 追踪用量
        usage = response.usage
        pricing = _PRICING.get(model, {"input": 1.0 / 1_000_000, "output": 5.0 / 1_000_000})
        cost = usage.input_tokens * pricing["input"] + usage.output_tokens * pricing["output"]

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
) -> Optional[Dict]:
    """
    调用 Claude API 并解析 JSON 响应

    Returns:
        解析后的 dict，失败返回 None
    """
    text = call(prompt, system=system, model=model, max_tokens=max_tokens, temperature=temperature)
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

def distill_with_reasoning(
    ticker: str,
    agent_results: List[Dict],
    dim_scores: Dict,
    resonance: Dict,
    rule_score: float,
    rule_direction: str,
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

    Returns:
        {
            "final_score": float,      # LLM 调整后的最终分
            "direction": str,          # LLM 判断的方向
            "reasoning": str,          # 中文推理链
            "key_insight": str,        # 核心洞察（一句话）
            "risk_flag": str,          # 风险标记
            "confidence": float,       # 0-1 置信度
        }
    """
    system = """你是 Alpha Hive 的 QueenDistiller（最终蒸馏蜂）。
你的任务是基于 6 个专业 Agent 的分析结果，做出最终投资机会评估。

输出要求：
1. 严格 JSON 格式
2. final_score: 0-10 浮点数（可以与规则引擎不同，但需要给出理由）
3. direction: "bullish" / "bearish" / "neutral"
4. reasoning: 2-3 句中文推理链（因为…所以…）
5. key_insight: 一句话核心洞察
6. risk_flag: 主要风险（一句话）
7. confidence: 0.0-1.0

重要：你不是简单重复 Agent 的结论，而是要：
- 识别 Agent 之间的矛盾信号
- 发现规则引擎可能忽略的模式
- 对数据质量低的维度降权
- 给出规则引擎无法做到的定性判断"""

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
                "discovery": r.get("discovery", "")[:150],
                "data_real_pct": f"{real_pct:.0f}%",
            })

    prompt = f"""分析 **{ticker}** 的投资机会。

## 6 Agent 分析结果
{json.dumps(agent_summaries, ensure_ascii=False, indent=2)}

## 5 维评分
{json.dumps(dim_scores, ensure_ascii=False)}

## 共振检测
- 共振: {"是" if resonance.get("resonance_detected") else "否"}
- 支持 Agent 数: {resonance.get("supporting_agents", 0)}
- 置信度增强: {resonance.get("confidence_boost", 0)}%

## 规则引擎基础分
- 评分: {rule_score}/10
- 方向: {rule_direction}

请输出 JSON："""

    result = call_json(prompt, system=system, max_tokens=512, temperature=0.3)
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
{json.dumps(notable, ensure_ascii=False, indent=2)}

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
{json.dumps(catalysts[:6], ensure_ascii=False, indent=2)}

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
{json.dumps({k: v for k, v in options_result.items() if k not in ('raw_chain',)}, ensure_ascii=False, indent=2)}

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
    board_snapshot: List[Dict] = None,
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
{json.dumps(snapshot_clean, ensure_ascii=False, indent=2)}

共振检测:
- 共振触发: {"是" if resonance.get("resonance_detected") else "否"}
- 支持 Agent 数: {resonance.get("supporting_agents", 0)}
- 主导方向: {resonance.get("direction", "neutral")}

输出 JSON："""

    return call_json(prompt, system=system, max_tokens=256, temperature=0.2)
