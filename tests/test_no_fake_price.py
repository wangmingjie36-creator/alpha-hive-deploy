"""v0.40.1: 假价占位反模式契约测试

历史教训：`price = 100.0` 兜底写法从 v36.0 到 v40.1 造成同一 bug 三次返工
（网站显示 $100 假价）——每次只修抓到现行的一处，漏网的在深夜 yfinance
限流时复发。本测试静态扫描生产代码，永久禁止该反模式重新进入代码库。

规约：现价取不到时用 0.0 哨兵（下游注入/显示逻辑跳过 0），
取价一律走 data_pipeline.fetch_stock_data（CBOE 起头多源链）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent

# 生产代码范围（排除测试/实验/第三方）
_EXCLUDE_DIRS = {"tests", "experiments", "mcp-servers", ".git", "__pycache__",
                 "alpha_hive_bot", "gui"}

# 反模式：price 类变量被赋值为 100.0 字面量（允许注释里出现）
_PATTERNS = [
    re.compile(r'(?<!#)\s*[\w\]"\']*price[\w"\']*\s*[:=]\s*100\.0\b', re.IGNORECASE),
    re.compile(r'"current_price"\s*:\s*100\.0\b'),
    re.compile(r"or\s+100\.0\b"),
]

# 已知合法例外（数学运算/百分比换算等，逐行白名单）
_ALLOWLIST_SUBSTR = [
    "/ 100.0",      # 百分比换算
    "* 100.0",
    "100.0)",       # clamp/min/max 上限
]


def _iter_prod_py():
    for p in ROOT.rglob("*.py"):
        if any(part in _EXCLUDE_DIRS for part in p.parts):
            continue
        yield p


def test_no_hardcoded_price_100_placeholder():
    violations = []
    for py in _iter_prod_py():
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines, 1):
            code = line.split("#", 1)[0]  # 忽略注释
            if "100.0" not in code:
                continue
            # 跳过含 CJK 的行（docstring/注释里的中文说明文字，非代码赋值）
            if any("\u4e00" <= ch <= "\u9fff" for ch in code):
                continue
            if any(a in code for a in _ALLOWLIST_SUBSTR):
                continue
            for pat in _PATTERNS:
                if pat.search(code):
                    violations.append(f"{py.relative_to(ROOT)}:{i}: {line.strip()[:90]}")
                    break
    assert not violations, (
        "发现 price=100.0 假价占位反模式（v36→v40.1 三次返工的根因）。\n"
        "规约：取不到现价用 0.0 哨兵 + data_pipeline.fetch_stock_data 多源链。\n"
        + "\n".join(violations)
    )
