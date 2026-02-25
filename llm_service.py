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
import time
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


def is_available() -> bool:
    """检查 LLM 服务是否可用"""
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
        _log.error("LLM API call failed: %s", e, exc_info=True)
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
