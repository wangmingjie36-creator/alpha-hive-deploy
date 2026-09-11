"""`dashboard_renderer` 的报告日期解析器不许接受非日期串（v0.45.192）。

背景取证：`~/Desktop` 开着 iCloud 同步，会产出 `alpha-hive-daily-2026-09-09 2.json`。
`Path(...).stem.replace("alpha-hive-daily-", "")` 得到 `"2026-09-09 2"`，
此前被当成一个**独立日期**用下去，在 gh-pages 上实测造成：
历史列表多一张幽灵卡片（5 个链接全死）、趋势序列多一个 `{"date": "2026-09-09 2"}` 点、
**「与 X 对比」的基准日变成幽灵**。

本文件三组断言，缺一不可：
  A. helper 本身的取值表（正反两向）
  B. **拒绝时必须留 warning** —— 否则就是 CLAUDE.md「这个失败，下游怎么知道？」
     要治的那个形状：静默 continue 把「读到坏文件」渲染成「没发生过」。
  C. **两个调用点真的走了 helper**（AST 结构检查，不看源码文案）——
     没有这一组，A/B 全绿也证明不了生产路径被保护。
"""
import ast
import logging
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
SRC = REPO_ROOT / "dashboard_renderer.py"

from dashboard_renderer import _report_stem_date            # noqa: E402
from is_trading_day import filename_is_nontrading_day       # noqa: E402


# ───────────────────────── A. 取值表 ─────────────────────────

@pytest.mark.parametrize("stem", ["2026-09-09", "2026-01-01", "2025-12-31"])
def test_real_dates_pass_through(stem):
    assert _report_stem_date(stem) == stem


@pytest.mark.parametrize("stem", [
    "2026-09-09 2",      # ← iCloud 重名副本，本闸的正主
    "2026-09-09 3",
    "2026-09-09 10",
    "2026-13-45",        # 形状合法但不是日期 ⇒ 只比正则会漏
    "2026-02-30",        # 同上
    "2026-9-9",          # 非零填充
    "2026-09-09x",
    "",
    "latest",
])
def test_non_dates_are_rejected(stem):
    assert _report_stem_date(stem) is None


# ─────────────── B. 既有守卫拦不住，所以本闸不是冗余 ───────────────

def test_existing_nontrading_guard_cannot_catch_the_phantom():
    """成对断言：证明 `filename_is_nontrading_day` 结构上够不到这个 bug。

    没有这一条，后来的人会觉得「都有幽灵守卫了，这道闸多余」而删掉它。
    它用的是 `re.search` 子串搜索，会从 `"2026-09-09 2"` 里找到 `2026-09-09`
    并判定其为交易日 ⇒ 返回 False（放行），连它自己的 fail-safe 分支都没进。
    """
    assert filename_is_nontrading_day("2026-09-09 2") is False, (
        "若此处变 True，说明 filename_is_nontrading_day 语义被改过——"
        "它有 5 个调用者（含 report_deployer），改它会让合法报告被漏发，"
        "请回去看 dashboard_renderer._report_stem_date 的 docstring。"
    )
    # 而本闸拦得住 —— 两句必须同时成立，才说明「新闸补的正是旧闸的盲区」
    assert _report_stem_date("2026-09-09 2") is None


def test_rejection_emits_a_warning(caplog):
    """拒绝必须可观测。静默跳过 == 把失败改写成没发生过。

    ⚠️ 这里**故意不用** `"2026-09-09 2"` 当探针值：那个字符串**硬编码在告警正文里**
    （作为给人看的例子），拿它断言 `in caplog.text` 会被消息模板自己满足 ——
    真实坏值被换成 `<redacted>` 也照样全绿（v0.45.192 变异校验实测）。
    同 MEMORY.md「守卫标志物别用源码文案」。
    改法两条：① 用一个消息模板里不可能出现的坏值；② 断言落在 `record.args` 上，
    那是**真实传入的值**，不是渲染后的文本。
    """
    probe = "1999-01-02 7"
    assert probe not in SRC.read_text(encoding="utf-8"), (
        f"探针值 {probe!r} 出现在源码里了，会让本断言失去判别力，换一个"
    )
    with caplog.at_level(logging.WARNING, logger="alpha_hive.dashboard_renderer"):
        _report_stem_date(probe)
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warns, "拒绝了却没留 warning —— 生产上没人会知道读到了 iCloud 副本"
    assert any(probe in (r.args or ()) for r in warns), (
        "warning 没把**真实坏值**作为参数传进去，排查时无从知道是哪个文件"
    )
    assert probe in caplog.text, "渲染后的消息里也应能看到坏值"


def test_accepting_a_real_date_is_silent():
    """反向：正常日期不许刷 warning，否则告警会被淹没成背景噪音。"""
    logger = logging.getLogger("alpha_hive.dashboard_renderer")
    seen = []

    class _Cap(logging.Handler):
        def emit(self, record):
            seen.append(record)

    h = _Cap(level=logging.WARNING)
    logger.addHandler(h)
    try:
        _report_stem_date("2026-09-09")
    finally:
        logger.removeHandler(h)
    assert seen == []


# ────────── C. 两个调用点真的走了 helper（AST，不看文案） ──────────

def _daily_prefix_strips(tree):
    """找出所有 `<x>.replace("alpha-hive-daily-", "")` 调用节点。"""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "alpha-hive-daily-"):
            out.append(node)
    return out


def test_every_daily_stem_strip_is_wrapped_by_the_guard():
    """枚举驱动：**每一处**把文件名去前缀得到日期的地方，都必须套上本闸。

    这里故意不按变量名（`_hdate`/`_pdate`）匹配 —— 那是名单驱动，
    新加的第三处叫别的名字就溜过去了。改为枚举「去前缀」这个动作本身。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    strips = _daily_prefix_strips(tree)
    assert len(strips) >= 2, (
        f"只找到 {len(strips)} 处 `.replace('alpha-hive-daily-', '')`，"
        "预期至少 2 处（历史时间线 + 分数变化基准日）。"
        "若确实减少了，请同步更新本断言。"
    )

    wrapped = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_report_stem_date"):
            for arg in node.args:
                for inner in ast.walk(arg):
                    if inner in strips:
                        wrapped.add(id(inner))

    naked = [n for n in strips if id(n) not in wrapped]
    assert not naked, (
        "以下行把报告文件名去前缀后**没有**过 `_report_stem_date()`，"
        f"iCloud 重名副本会再次变成幽灵日期：行号 {[n.lineno for n in naked]}"
    )
