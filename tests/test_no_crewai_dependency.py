#!/usr/bin/env python3
"""CrewAI 已于 v0.45.74 彻底移除——这条闸防它回来。

背景：`crewai_adapter.py` 是 Phase 3 P5 的实验，把 6 只自研蜂包成 CrewAI Tool、
交给一个 LLM manager 走 `Process.hierarchical` 调度。它从未接通：
`run_crew_scan()` 零调用方、`requirements.txt` 里那行是注释掉的、
`Agent(...)` 从没传 `llm=`（会落到 crewai 默认的 OpenAI `gpt-4.1-mini`，
而本仓无 `OPENAI_API_KEY`）。同时它在 import 时会起线程给 api.scarf.sh 打埋点。

两条测试守两件不同的事，别合并：

1. `test_daily_report_does_not_import_crewai`
   —— 守**运行时**：日报主入口的 import 图里不许出现 crewai。
   ⚠️ 这条的有效性取决于 crewai 是否还装着。若哪天 `pip uninstall crewai`，
   它会变成恒真（import 不进来是因为没装，不是因为我们没写）。所以必须配第 2 条。

2. `test_repo_has_no_crewai_import`
   —— 守**源码**：全仓不许再出现 `import crewai`。
   静态检查，与 crewai 装没装无关，卸载后仍然有效。
"""

import os
import re
import subprocess
import sys
import textwrap

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只匹配真正的 import 语句，不匹配注释、字符串、文档里提到的名字
_IMPORT_RE = re.compile(r"^\s*(?:from\s+crewai[\w.]*\s+import|import\s+crewai\b)", re.M)

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


def test_daily_report_does_not_import_crewai():
    """import alpha_hive_daily_report 之后，sys.modules 里不该有 crewai。"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import sys
            import alpha_hive_daily_report  # noqa: F401
            print("CREWAI_IMPORTED=%s" % ("crewai" in sys.modules))
        """)],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "CREWAI_IMPORTED=False" in result.stdout, (
        "crewai 又被拖进日报的 import 图了。它是 v0.45.74 移除的死集成："
        "零调用方、无 llm 配置、import 时还会打第三方埋点。\n"
        f"stdout={result.stdout!r}"
    )


def test_repo_has_no_crewai_import():
    """全仓源码不该再有 `import crewai` / `from crewai import ...`。"""
    offenders = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.abspath(full) == os.path.abspath(__file__):
                continue  # 本文件的正则字面量不算
            try:
                src = open(full, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if _IMPORT_RE.search(src):
                offenders.append(os.path.relpath(full, PROJECT_ROOT))

    assert not offenders, (
        "以下文件重新引入了 crewai：\n  " + "\n  ".join(sorted(offenders))
        + "\n\nCrewAI 集成已于 v0.45.74 移除（详见 CHANGELOG）。真要重新引入，"
          "先回答三个问题：manager 用哪个 LLM、谁付钱、"
          "以及它凭什么比现行的 QueenDistiller 固定权重聚合更准。"
    )
