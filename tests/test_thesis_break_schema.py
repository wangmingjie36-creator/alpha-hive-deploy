"""失效条件读取端守卫（v0.45.55）。

背景（实测）
------------
`thesis_breaks_config.json` 里**两种 schema 并存**：

    人工条件 120 条：`metric` / `trigger`
    机器条件 210 条：`field` / `op` / `value`（v0.45.50 于 2026-08-27 新增）

旧读取端是列表推导 `c["metric"] + "：" + c["trigger"]` —— 一条机器条件的
`KeyError` 就把整个 try 块打掉。实测后果：**8/26 与 8/27 两天各 0/30 只标的
有失效条件**，日志只留一行 WARNING。而 CLAUDE.md 写着
「任何结论必须附失效条件（thesis break）」—— 这条硬性规则空转了两天。

守什么
------
1. 两种 schema 都能格式化
2. 机器条件必须标「自动」—— 它是从历史分位推出来的，不是人工判断
3. **逐条隔离**：一条读不懂不许让其余的一起消失（本次修复的重点）
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpha_hive_daily_report import _format_break_condition as fmt  # noqa: E402

HUMAN = {"id": "eps_miss", "metric": "EPS 大幅低于预期", "trigger": "实际 < 预期 20%+"}
MACHINE = {"field": "score", "op": "<", "value": 4.0, "_machine": True,
           "_added": "2026-08-27", "_note": "综合分跌破 p2（全历史 1191 obs）"}


def test_human_condition():
    assert fmt(HUMAN) == "EPS 大幅低于预期：实际 < 预期 20%+"


def test_machine_condition_marked_auto():
    """机器条件必须标来源 —— 它是历史分位自动推出来的，与人工判断不同重量。"""
    out = fmt(MACHINE)
    assert out == "综合分跌破 p2（全历史 1191 obs）：score < 4.0（自动）"
    assert "自动" in out


def test_machine_without_note_falls_back_to_field():
    c = {k: v for k, v in MACHINE.items() if k != "_note"}
    assert fmt(c) == "score：score < 4.0（自动）"


@pytest.mark.parametrize("bad", [
    None, "not-a-dict", 42, {},
    {"metric": "只有 metric"},                 # 缺 trigger
    {"field": "score", "op": "<"},             # 缺 value
])
def test_unrecognized_returns_none_not_raises(bad):
    """读不懂就返回 None —— **不抛异常**。抛出去就会连坐整批。"""
    assert fmt(bad) is None


def test_zero_value_is_a_real_threshold():
    """反向：value=0 是一个真阈值，不能被当成缺失。"""
    assert fmt({"field": "score", "op": "<", "value": 0}) == "score：score < 0"


# ══════════════════════════════════════════════════════════════════
# 逐条隔离 —— 本次修复的重点
# ══════════════════════════════════════════════════════════════════

def test_one_bad_condition_does_not_kill_the_rest():
    """🔴 回归：旧写法一条 KeyError 打掉整块，30 只标的的 l1/l2 全部为空。

    这里直接模拟读取端的逐条循环：混入一条读不懂的，其余必须照常产出。
    """
    conds = [HUMAN, {"garbage": 1}, MACHINE, None, dict(HUMAN, metric="第二条")]
    out = [t for t in (fmt(c) for c in conds) if t]
    assert len(out) == 3, f"逐条隔离失效，只剩 {out}"
    assert any("EPS" in t for t in out) and any("自动" in t for t in out)


def test_real_config_fully_formattable():
    """真配置 330 条必须 0 条无法识别 —— 有新 schema 混进来时这条会先红。"""
    p = os.path.join("/Users/igg/Desktop/Alpha Hive", "thesis_breaks_config.json")
    if not os.path.exists(p):
        pytest.skip("生产配置不可得")
    cfg = json.load(open(p))
    ok = bad = 0
    for v in cfg.values():
        if not isinstance(v, dict):
            continue
        for lvl in ("level_1_warning", "level_2_stop_loss"):
            for c in (v.get(lvl, {}) or {}).get("conditions", []) or []:
                if fmt(c):
                    ok += 1
                else:
                    bad += 1
    assert ok > 300, f"只格式化出 {ok} 条，疑似 schema 又变了"
    assert bad == 0, f"{bad} 条无法识别 —— 配置里出现了第三种 schema"
