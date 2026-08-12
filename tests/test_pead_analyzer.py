"""
v0.43.x 回归测试：_compute_post_earnings_drift 不应因 yfinance MultiIndex
列名（单票 download() 返回 ('Close','TICKER') 而非纯 'Close'）而静默失效。

事故链：ChronosBeeHorizon 依赖 pead_analyzer.get_pead_analysis() 提供
PEAD（财报后价格漂移）方向证据，是它唯一真正带方向的信号源（v0.43.0
CHANGELOG）。_compute_post_earnings_drift 内部裸调 yf.download()，MultiIndex
列名下 `float(row["Close"])` 抛 TypeError，被外层宽泛的
`except (KeyError, TypeError, ValueError): continue` 静默吞掉——
price_map 永远为空，drift_records 永远是 []，t5_avg 永远是 None，
bias 永远退化成 "neutral"。表现为 ChronosBee 结构上"没有方向证据"，
不是"证据都是中性"。
"""

import pandas as pd

from pead_analyzer import _compute_post_earnings_drift


def _fake_multiindex_download(*args, **kwargs):
    idx = pd.date_range("2026-01-01", periods=40, freq="B")
    cols = pd.MultiIndex.from_product([["Close", "High", "Low", "Open", "Volume"], ["NVDA"]])
    data = {}
    for col in cols:
        base = 200.0 if col[0] != "Volume" else 1_000_000
        data[col] = [base + i * 0.5 for i in range(40)]
    return pd.DataFrame(data, index=idx, columns=cols)


def test_drift_computation_survives_multiindex_columns(monkeypatch):
    import yfinance as yf
    monkeypatch.setattr(yf, "download", _fake_multiindex_download)

    earnings_dates = ["2026-01-05"]
    records = _compute_post_earnings_drift("NVDA", earnings_dates)

    assert records, "MultiIndex 列名下不应返回空列表（此前的静默失败模式）"
    assert records[0].get("t5") is not None
