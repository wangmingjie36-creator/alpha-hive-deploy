"""
tests/test_prompt_loader.py — pattern 5 MD prompt 加载层单元测试
"""

import prompt_loader as pl


def test_loads_real_md_and_strips_frontmatter():
    body = pl.load_prompt("options_strategist", "FALLBACK")
    assert body != "FALLBACK"
    # frontmatter 已剥离：正文以 persona 开头，不含 frontmatter 字段
    assert body.startswith("你是 Alpha Hive 首席期权策略师")
    assert "description:" not in body.splitlines()[0]


def test_missing_file_returns_fallback():
    assert pl.load_prompt("___does_not_exist___", "FB") == "FB"


def test_step1_md_loads():
    body = pl.load_prompt("step1_analysis_engine", "FB")
    assert "量化信号分析引擎" in body


def test_strip_frontmatter_helper():
    raw = "---\nname: x\ndescription: y\n---\nBODY LINE 1\nBODY LINE 2"
    assert pl._strip_frontmatter(raw) == "BODY LINE 1\nBODY LINE 2"


def test_strip_frontmatter_noop_without_frontmatter():
    raw = "no frontmatter here\njust body"
    assert pl._strip_frontmatter(raw) == raw


def test_news_sentiment_md_matches_inline_fallback():
    # MD 正文应与 llm_service 内联 fallback 字节级一致（行为不变保证）
    import llm_service
    loaded = pl.load_prompt("news_sentiment_analyst", "")
    assert loaded.strip() == llm_service._NEWS_SENTIMENT_SYSTEM.strip()
