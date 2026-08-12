"""
v0.44.x 回归测试：_generate_technical_analysis 生成的代码不应因 yfinance
MultiIndex 列名（单只股票 download() 返回 ('Close','TICKER') 而非纯 'Close'）
而崩溃。

事故：CodeExecutorAgent 过去一个多月 91/91 笔预测恒定看多——根因是这份生成
代码里 `float(latest["Close"])` 遇到 MultiIndex 列时拿到的是 Series 不是
标量，直接 TypeError 崩溃；CodeExecutorAgent.analyze() 捕获到 analysis
脚本失败后落入的兜底分支设计上只能返回 bullish/neutral，从不返回
bearish，于是"技术分析崩溃"表现成了"永远看多"。
_generate_yfinance（数据爬取脚本）早就修过同一个坑（"避免 download() 多层
列名 TypeError"），本次只是把同款修法补到 _generate_technical_analysis。
"""

import pandas as pd

from code_generator import CodeGenerator


def _fake_multiindex_download(*args, **kwargs):
    """模拟 yf.download() 对单只股票返回的 MultiIndex 列结构"""
    idx = pd.date_range("2026-06-01", periods=60, freq="D")
    cols = pd.MultiIndex.from_product([["Close", "High", "Low", "Open", "Volume"], ["NVDA"]])
    data = {}
    for i, col in enumerate(cols):
        base = 200.0 + i if col[0] != "Volume" else 1_000_000
        data[col] = [base + j * 0.1 for j in range(60)]
    return pd.DataFrame(data, index=idx, columns=cols)


def test_technical_analysis_code_survives_multiindex_columns(monkeypatch):
    import yfinance as yf
    monkeypatch.setattr(yf, "download", _fake_multiindex_download)

    code = CodeGenerator.generate_analysis("technical", {"ticker": "NVDA", "period": "1mo"})
    exec_globals = {}
    # 不应抛出 TypeError: float() argument must be a string or a real number, not 'Series'
    exec(code, exec_globals)


def test_technical_analysis_code_flattens_multiindex_before_use():
    code = CodeGenerator.generate_analysis("technical", {"ticker": "NVDA", "period": "1mo"})
    assert 'hasattr(data.columns, "levels")' in code
    assert "get_level_values(0)" in code
