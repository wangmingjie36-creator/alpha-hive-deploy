#!/usr/bin/env python3
"""
🐝 Alpha Hive · 轨道 B — 自我诊断数据生成器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
流程：
  1. 扫描 report_snapshots/ → 提取 T+7 已完成预测
  2. 拆分正确 / 错误预测，提取失败案例的七蜂上下文
  3. 生成结构化 Markdown briefing → self_analysis_briefs/YYYY-MM.md
  4. 由 Cowork Claude 读取 briefing，离线推理，输出信号假说

不需要 Claude API Key，推理完全由 Cowork 会话承担。

用法：
  python3 self_analyst.py                 # 生成本月 briefing
  python3 self_analyst.py --months 3      # 分析最近 3 个月数据
  python3 self_analyst.py --ticker NVDA   # 只分析指定标的
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 路径配置 ──────────────────────────────────────────────────────────────────
ALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))
_VM_PATH = Path("/sessions/keen-magical-wright/mnt/Alpha Hive")
if _VM_PATH.exists():
    ALPHAHIVE_DIR = _VM_PATH

_VM_DEEP_DIR = Path("/sessions/keen-magical-wright/mnt/深度分析报告/深度")
OUTPUT_DIR   = _VM_DEEP_DIR if _VM_DEEP_DIR.exists() else Path(
    os.path.expanduser("~/Desktop/深度分析报告/深度"))

SNAPSHOTS_DIR = OUTPUT_DIR / "report_snapshots"
BRIEFS_DIR    = ALPHAHIVE_DIR / "self_analysis_briefs"

# Agent 中文名映射
AGENT_ZH = {
    "ScoutBeeNova":      "Scout（基本面）",
    "BuzzBeeWhisper":    "Buzz（舆情）",
    "OracleBeeEcho":     "Oracle（期权）",
    "ChronosBeeHorizon": "Chronos（催化剂）",
    "RivalBeeVanguard":  "Rival（ML辅助）",
    "GuardBeeSentinel":  "Guard（宏观/风控）",
    "BearBeeContrarian": "Bear（空头/对立）",
}

# ─────────────────────────────────────────────────────────────────────────────

def load_snapshots(snapshots_dir: Path, months_back: int = 3,
                   ticker_filter: Optional[str] = None) -> list:
    """加载最近 N 个月内、T+7 已回填的快照"""
    if not snapshots_dir.exists():
        return []
    cutoff = datetime.now() - timedelta(days=months_back * 31)
    results = []
    for f in sorted(snapshots_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # 必须有 T+7 实际价格
            if data.get("actual_prices", {}).get("t7") is None:
                continue
            # 日期过滤
            created = data.get("created_at", "")
            if created:
                try:
                    if datetime.fromisoformat(created) < cutoff:
                        continue
                except ValueError:
                    pass
            # Ticker 过滤
            if ticker_filter and data.get("ticker", "").upper() != ticker_filter.upper():
                continue
            results.append(data)
        except Exception:
            pass
    # 按报告日期升序排列，确保 [-10:] 切片取到的是最近失败案例
    results.sort(key=lambda x: x.get("date", ""))
    return results


def classify(snap: dict) -> str:
    """判断预测方向是否正确 → 'correct' | 'wrong' | 'neutral'"""
    direction  = snap.get("direction", "Neutral")
    entry      = snap.get("entry_price", 0.0) or 0.0
    actual_t7  = snap.get("actual_prices", {}).get("t7") or 0.0
    if not entry or not actual_t7:
        return "unknown"
    ret = (actual_t7 - entry) / entry * 100
    if direction == "Long":
        return "correct" if ret > 0 else "wrong"
    elif direction == "Short":
        return "correct" if ret < 0 else "wrong"
    return "neutral"


def compute_stats(snaps: list) -> dict:
    """汇总统计"""
    classified = [(s, classify(s)) for s in snaps]
    correct  = [s for s, c in classified if c == "correct"]
    wrong    = [s for s, c in classified if c == "wrong"]
    neutral  = [s for s, c in classified if c == "neutral"]
    unknown  = [s for s, c in classified if c == "unknown"]

    returns_t7 = []
    for s in snaps:
        ep = s.get("entry_price") or 0
        t7 = s.get("actual_prices", {}).get("t7") or 0
        if ep and t7:
            returns_t7.append((t7 - ep) / ep * 100)

    avg_ret = round(sum(returns_t7) / len(returns_t7), 2) if returns_t7 else 0.0
    win_rate = round(len(correct) / max(len(correct) + len(wrong), 1) * 100, 1)

    return {
        "total":    len(snaps),
        "correct":  len(correct),
        "wrong":    len(wrong),
        "neutral":  len(neutral),
        "unknown":  len(unknown),
        "win_rate": win_rate,
        "avg_ret_7d": avg_ret,
        "wrong_snaps": wrong,
        "correct_snaps": correct,
    }


def extract_failure_context(snap: dict) -> dict:
    """从错误预测快照中提取关键上下文"""
    ep  = snap.get("entry_price", 0) or 0
    t7  = snap.get("actual_prices", {}).get("t7") or 0
    ret = round((t7 - ep) / ep * 100, 2) if ep and t7 else 0.0

    votes    = snap.get("agent_votes", {})
    top_bull = sorted(votes.items(), key=lambda x: x[1], reverse=True)[:3]
    top_bear = sorted(votes.items(), key=lambda x: x[1])[:2]

    return {
        "ticker":    snap.get("ticker", "?"),
        "date":      snap.get("date", "?"),
        "direction": snap.get("direction", "?"),
        "score":     snap.get("composite_score", 0),
        "entry":     ep,
        "actual_t7": t7,
        "actual_ret": ret,
        "top_bull_agents": [(AGENT_ZH.get(a, a), round(v, 1)) for a, v in top_bull],
        "top_bear_agents": [(AGENT_ZH.get(a, a), round(v, 1)) for a, v in top_bear],
        "all_votes": {AGENT_ZH.get(k, k): round(v, 1) for k, v in votes.items()},
    }


def analyze_failure_patterns(wrong_snaps: list) -> dict:
    """分析失败案例的共同特征"""
    if not wrong_snaps:
        return {}

    # 方向分布
    dir_count = defaultdict(int)
    for s in wrong_snaps:
        dir_count[s.get("direction", "?")] += 1

    # 评分分布（错误预测时系统评分偏高/偏低）
    scores = [s.get("composite_score") for s in wrong_snaps if s.get("composite_score") is not None]
    avg_score = round(sum(scores) / len(scores), 2) if scores else 0

    # 哪些 agent 在失败案例中评分偏高（最具误导性）
    agent_totals = defaultdict(list)
    for s in wrong_snaps:
        for agent, score in s.get("agent_votes", {}).items():
            agent_totals[agent].append(score)
    agent_avg = {
        AGENT_ZH.get(k, k): round(sum(v) / len(v), 2)
        for k, v in agent_totals.items() if v
    }
    most_misleading = sorted(agent_avg.items(), key=lambda x: x[1], reverse=True)[:3]

    # Ticker 集中度
    ticker_count = defaultdict(int)
    for s in wrong_snaps:
        ticker_count[s.get("ticker", "?")] += 1
    top_tickers = sorted(ticker_count.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "direction_breakdown": dict(dir_count),
        "avg_composite_score": avg_score,
        "most_misleading_agents": most_misleading,
        "top_failing_tickers": top_tickers,
    }


def format_briefing(stats: dict, patterns: dict,
                    wrong_contexts: list,
                    months_back: int,
                    ticker_filter: Optional[str]) -> str:
    """生成给 Cowork Claude 阅读的结构化 Markdown briefing"""

    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    scope   = f"最近 {months_back} 个月"
    if ticker_filter:
        scope += f" · 标的: {ticker_filter}"

    lines = [
        f"# 🐝 Alpha Hive 自我诊断简报",
        f"**生成时间:** {now}　**分析范围:** {scope}",
        "",
        "---",
        "",
        "## 一、总体准确率",
        "",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 总预测数（T+7 已完成）| {stats['total']} |",
        f"| 方向正确 | {stats['correct']} |",
        f"| 方向错误 | {stats['wrong']} |",
        f"| 方向胜率 | {stats['win_rate']}% |",
        f"| T+7 平均收益 | {stats['avg_ret_7d']:+.2f}% |",
        "",
    ]

    # 失败模式
    if patterns:
        lines += [
            "## 二、失败模式分析",
            "",
            f"**失败案例数:** {stats['wrong']}",
            "",
        ]
        if patterns.get("direction_breakdown"):
            lines.append("**方向分布（错误预测中）:**")
            for d, c in patterns["direction_breakdown"].items():
                lines.append(f"- {d}: {c} 次")
            lines.append("")

        lines.append(f"**失败时综合评分均值:** {patterns.get('avg_composite_score', 'N/A')}")
        lines.append("*(系统自信度越高越危险，说明高评分≠高准确)*")
        lines.append("")

        if patterns.get("most_misleading_agents"):
            lines.append("**失败案例中评分最高的维度（最具误导性）:**")
            for agent, avg in patterns["most_misleading_agents"]:
                lines.append(f"- {agent}: 平均 {avg}/10")
            lines.append("")

        if patterns.get("top_failing_tickers"):
            lines.append("**失败集中的标的:**")
            for tk, cnt in patterns["top_failing_tickers"]:
                lines.append(f"- {tk}: {cnt} 次失误")
            lines.append("")

    # 失败案例详情
    if wrong_contexts:
        lines += [
            "## 三、失败案例明细（最近 10 条）",
            "",
        ]
        for i, ctx in enumerate(wrong_contexts[-10:], 1):
            lines += [
                f"### 案例 {i}：{ctx['ticker']} · {ctx['date']}",
                f"- **预测方向:** {ctx['direction']}　**综合评分:** {ctx['score']:.1f}",
                f"- **入场价:** ${ctx['entry']:.2f}　**T+7 实际价:** ${ctx['actual_t7']:.2f}　**实际涨跌:** {ctx['actual_ret']:+.2f}%",
                f"- **支持预测的前三维度:** " + "、".join(
                    f"{a}({s})" for a, s in ctx['top_bull_agents']
                ),
                f"- **最悲观维度:** " + "、".join(
                    f"{a}({s})" for a, s in ctx['top_bear_agents']
                ),
                f"- **七蜂评分快照:** " + " | ".join(
                    f"{a}:{s}" for a, s in ctx['all_votes'].items()
                ),
                "",
            ]

    # 给 Claude 的推理任务
    lines += [
        "---",
        "",
        "## 四、分析任务（由 Cowork Claude 完成）",
        "",
        "请你作为 Alpha Hive 系统的架构顾问，基于以上数据完成以下分析：",
        "",
        "### 任务 1：失败根因分析",
        "观察案例明细，识别这些错误预测有哪些共同的市场环境特征或结构特征。",
        "例如：是否集中在财报前？是否某类板块？是否在高 VIX 环境？",
        "",
        "### 任务 2：信号盲区识别",
        "现有七蜂维度（基本面/舆情/期权/催化剂/ML/宏观/空头）中，",
        "哪个维度在这些失败案例中表现出系统性误导？原因是什么？",
        "",
        "### 任务 3：新信号假说（最重要）",
        "基于失败模式，建议 2-3 个新的分析维度或过滤条件，",
        "每个假说必须包含：",
        "- **信号名称**",
        "- **数据来源**（具体说明：哪个 API / 哪个字段）",
        "- **计算方法**（简要伪代码）",
        "- **预期效果**（估计可改善哪类失败场景）",
        "- **实现难度**（低/中/高）",
        "",
        "### 任务 4：优先级排序",
        "综合改善效果与实现难度，给出一个「本月最值得实现的信号」建议。",
        "",
        "---",
        "*本文件由 `self_analyst.py` 自动生成，推理由 Cowork Claude 完成，无需 API Key。*",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Alpha Hive · 自我诊断数据生成器（轨道 B）"
    )
    parser.add_argument("--months",  type=int, default=3,
                        help="分析最近 N 个月数据（默认 3）")
    parser.add_argument("--ticker",  type=str, default=None,
                        help="只分析指定标的（默认全部）")
    parser.add_argument("--out",     type=str, default=None,
                        help="指定输出文件路径（默认 self_analysis_briefs/YYYY-MM.md）")
    args = parser.parse_args()

    print(f"\n🐝 Alpha Hive · self_analyst 启动")
    print(f"   快照目录: {SNAPSHOTS_DIR}")

    # 1. 加载快照
    snaps = load_snapshots(SNAPSHOTS_DIR,
                           months_back=args.months,
                           ticker_filter=args.ticker)
    print(f"   加载快照: {len(snaps)} 条（T+7 已回填）")

    if not snaps:
        print("⏭  无可分析数据，请先积累至少 7 天的快照。")
        return

    # 2. 统计与分类
    stats = compute_stats(snaps)
    print(f"   胜率: {stats['win_rate']}% | 正确: {stats['correct']} | 错误: {stats['wrong']}")

    # 3. 失败模式分析
    patterns = analyze_failure_patterns(stats["wrong_snaps"])

    # 4. 提取失败上下文
    wrong_contexts = [extract_failure_context(s) for s in stats["wrong_snaps"]]

    # 5. 生成 briefing
    briefing = format_briefing(stats, patterns, wrong_contexts,
                               args.months, args.ticker)

    # 6. 写文件
    if args.out:
        out_path = Path(args.out)
    else:
        BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
        month_tag = datetime.now().strftime("%Y-%m")
        suffix    = f"-{args.ticker.upper()}" if args.ticker else ""
        out_path  = BRIEFS_DIR / f"self_analysis_{month_tag}{suffix}.md"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(briefing, encoding="utf-8")

    print(f"\n✅ 诊断简报已生成：")
    print(f"   {out_path}")
    print(f"\n📋 下一步：在 Cowork 中打开此文件，Claude 会自动完成第四节的推理任务。\n")


if __name__ == "__main__":
    main()
