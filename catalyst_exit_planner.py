#!/usr/bin/env python3
"""
🐝 Alpha Hive — 催化剂驱动的动态 Exit 规划器 (v0.23.0)
==========================================================
固定 T+7 持仓在 FF 归因上显示 α=-25%，而 T+30 显示 α=+49% ***。
原因：-5% 路径依赖 SL 在中短期波动中过早止损，错过基本面展开。

本模块让 ChronosBee 催化剂驱动 exit 时机：

规则（按优先级）：
  earnings / guidance 类型，事件距 entry > 3 天 → exit = event_date - 2 days（避免盲盒）
  fda_approval / product_launch，事件距 entry > 2 天 → exit = event_date + 3 days（吃完 reaction）
  regulatory 类型 → exit = event_date + 1 day
  多个催化剂 → 取最近的；距离太近（< 3d）则取次近
  无 30 天内催化剂 → 默认 T+21（T+7 到 T+30 中位数）

持仓天数范围：[3, 45] 交易日（防 edge case）

用法
----
    from catalyst_exit_planner import plan_exit
    hold_days, rationale = plan_exit(
        ticker="NVDA",
        entry_date="2026-03-09",
        catalysts=[{"event": "Q4 earnings", "date": "2026-03-15", "type": "earnings", ...}],
    )
    # -> (4, "Q4 earnings 前 2d 平仓")
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

MIN_HOLD_DAYS = 3
MAX_HOLD_DAYS = 45
DEFAULT_HOLD_DAYS = 21   # T+21 = T+7 与 T+30 中位数

# 事件类型对应的 exit 规则（entry → event_date 的相对天数调整）
# 正值 = event_date + N （事件后平仓）
# 负值 = event_date + N （事件前平仓）
EVENT_EXIT_OFFSETS = {
    "earnings":       -2,   # 财报前 2d 平仓
    "guidance":       -2,   # 业绩指引同理
    "fda_approval":   +3,   # FDA 后 3d 平仓
    "product_launch": +3,   # 产品发布后 3d
    "regulatory":     +1,   # 监管事件后 1d
    "clinical_trial": +3,   # 临床试验同 FDA
    "conference":     +1,   # 行业会议后 1d
    "dividend":       None, # 分红不作 exit 依据（跳过）
}


def _parse_date(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d")
    except (ValueError, AttributeError, TypeError):
        return None


def plan_exit(
    ticker: str,
    entry_date: str,
    catalysts: Optional[List[Dict]] = None,
    *,
    min_hold: int = MIN_HOLD_DAYS,
    max_hold: int = MAX_HOLD_DAYS,
    default_hold: int = DEFAULT_HOLD_DAYS,
    lookback_days: int = 45,
) -> Tuple[int, str]:
    """
    根据 entry_date 和 catalysts 列表推荐 hold_days + 说明

    Args:
        ticker: 标的代码（仅用于日志/rationale 文字）
        entry_date: 建仓日 "YYYY-MM-DD"
        catalysts: ChronosBee 风格的催化剂列表
            [{"event": str, "date": "YYYY-MM-DD", "type": str, "severity": str}, ...]
        min_hold / max_hold: 硬性边界
        default_hold: 无催化剂时默认天数
        lookback_days: 只考虑 entry_date 后 N 天内的催化剂

    Returns:
        (hold_days, rationale)
    """
    ed = _parse_date(entry_date)
    if ed is None:
        return default_hold, f"entry_date 解析失败，退回 T+{default_hold}"

    if not catalysts:
        return default_hold, f"无催化剂数据，默认 T+{default_hold}"

    # 筛选 entry 后 lookback_days 内、且不为 dividend 类型的事件
    valid: List[Tuple[datetime, Dict, int]] = []
    for c in catalysts:
        c_date = _parse_date(c.get("date", ""))
        if c_date is None:
            continue
        days_from_entry = (c_date - ed).days
        if days_from_entry < 0:  # 过去事件
            continue
        if days_from_entry > lookback_days:
            continue
        c_type = (c.get("type") or "").lower().strip()
        if c_type not in EVENT_EXIT_OFFSETS:
            continue
        if EVENT_EXIT_OFFSETS[c_type] is None:
            continue
        valid.append((c_date, c, days_from_entry))

    if not valid:
        return default_hold, f"30 天内无可用催化剂，默认 T+{default_hold}"

    # 按 event_date 排序，取最近的（但如果 < 3d 且有下一个，取下一个避免 hold 太短）
    valid.sort(key=lambda x: x[0])

    for c_date, c, days_from_entry in valid:
        c_type = c["type"].lower().strip()
        offset = EVENT_EXIT_OFFSETS[c_type]
        # 推荐 exit date = event_date + offset
        recommended_exit = c_date + timedelta(days=offset)
        recommended_hold = (recommended_exit - ed).days

        # 持仓过短（< min_hold）跳过，尝试下一个
        if recommended_hold < min_hold:
            continue
        # 持仓过长（> max_hold）截断
        recommended_hold = min(recommended_hold, max_hold)

        event_name = c.get("event", c_type)
        direction_word = "前" if offset < 0 else "后"
        rationale = (
            f"{event_name} ({c_date.strftime('%Y-%m-%d')}, {c_type}) "
            f"{direction_word} {abs(offset)}d 平仓 → hold={recommended_hold}d"
        )
        return recommended_hold, rationale

    # 所有催化剂都太近 → 默认 T+N
    return default_hold, (
        f"所有催化剂距 entry < {min_hold}d，取默认 T+{default_hold}"
    )


def load_catalysts_for_ticker(ticker: str, catalysts_file: str = "catalysts.json") -> List[Dict]:
    """从 catalysts.json 加载指定 ticker 的催化剂列表"""
    import json
    from pathlib import Path
    p = Path(__file__).parent / catalysts_file
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = data.get(ticker, [])
    if isinstance(events, list):
        return events
    return []


# ══════════════════════════════════════════════════════════════════════════════
# CLI 自测
# ══════════════════════════════════════════════════════════════════════════════

def _selftest():
    """快速验证几个常见场景"""
    cases = [
        ("NVDA", "2026-03-09",
         [{"event": "Q4 Earnings", "date": "2026-03-15", "type": "earnings"}],
         4, "earnings -2d"),
        ("VKTX", "2026-06-20",
         [{"event": "Phase 3 Data", "date": "2026-08-15", "type": "fda_approval"}],
         21, "超出 lookback 45 天 → 默认"),  # 8-15 减 6-20 = 56 天，超 45
        ("TSLA", "2026-03-20",
         [
             {"event": "Q1 Delivery", "date": "2026-04-02", "type": "guidance"},
             {"event": "Q1 Earnings", "date": "2026-04-22", "type": "earnings"},
         ],
         11, "取第一个 guidance"),
        ("AAPL", "2026-02-01", [],  21, "无催化剂"),
    ]
    for ticker, ed, cats, expected_hold, note in cases:
        h, r = plan_exit(ticker, ed, cats)
        tag = "✓" if (h == expected_hold or abs(h - expected_hold) <= 2) else "✗"
        print(f"  {tag} {ticker} {ed} → {h}d ({note}): {r}")


if __name__ == "__main__":
    _selftest()
