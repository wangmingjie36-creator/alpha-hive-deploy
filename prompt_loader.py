"""
Alpha Hive - Prompt / persona 加载层（MD 源 + Python 包装解耦，pattern 5）

对标 anthropics/financial-services：Agent 的人格 / 指令写在 Markdown 里（数据），
Python 只负责加载与接线。本模块从 prompts/<name>.md 读取正文（跳过 YAML
frontmatter），任何错误静默回退到调用方提供的 fallback——保持行为字节级不变，
且不打印任何「未找到文件 / key」类告警。

刻意不读取任何 API key、不触发 LLM；纯文件读取，叶子模块。
"""

from pathlib import Path as _Path
from typing import Optional

_PROMPTS_DIR = _Path(__file__).parent / "prompts"


def _strip_frontmatter(text: str) -> str:
    """去掉开头的 YAML frontmatter（--- 之间），返回正文。无 frontmatter 时原样返回。"""
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        if len(parts) == 2:
            # parts[0] = '---\n<frontmatter>'，parts[1] = '\n<body>'
            return parts[1].lstrip("\n")
    return text


def load_prompt(name: str, fallback: str = "") -> str:
    """读取 prompts/<name>.md 正文；任何错误静默回退 fallback（无告警）。

    Args:
        name: prompts/ 下的文件名（不含 .md 后缀）
        fallback: 文件缺失 / 读取失败时返回的默认 prompt（通常 = 原内联常量，
                  保证 prompts/ 缺失时行为不变）
    """
    try:
        path = _PROMPTS_DIR / f"{name}.md"
        text = path.read_text(encoding="utf-8")
        body = _strip_frontmatter(text).strip()
        return body if body else fallback
    except (OSError, UnicodeError, ValueError):
        return fallback


def prompt_path(name: str) -> _Path:
    """返回某 prompt 的预期路径（供测试 / 工具使用）。"""
    return _PROMPTS_DIR / f"{name}.md"
