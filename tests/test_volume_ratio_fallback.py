"""volume_ratio 的 Twelve Data 回落（v0.45.81）

yfinance 限流时 momentum_5d 早就有自攒价格索引兜底（v0.43.25），volume_ratio
一直没有——索引只存收盘价，硬凑一个比值等于编数据。这里补的是独立数据源
（Twelve Data，不经 yfinance），不是拿价格索引硬算。

覆盖两层：
- `twelve_data.fetch_volume_ratio`：解析/成交量不可得时置 None，不兜 1.0
- `data_pipeline._fill_volume_from_twelvedata`：已有真实值时不重复请求；
  Twelve Data 也没有时保持 None，不阻断降级链
"""

import json

import pytest

import twelve_data as td
import data_pipeline as dp


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    td.reset_stats()
    td.clear_bars_cache()      # v0.45.125：fetch_volume_ratio 走进程内日线缓存，会跨测试泄漏
    monkeypatch.setattr(td, "_limiter", None)
    yield
    td.reset_stats()
    td.clear_bars_cache()


def _resp(payload):
    return json.dumps(payload).encode()


def _patch_http(monkeypatch, payload_or_exc):
    import http_gate

    def _open(*a, **k):
        if isinstance(payload_or_exc, Exception):
            raise payload_or_exc
        return payload_or_exc

    monkeypatch.setattr(http_gate, "urlopen_gated", _open)


def _bars(n, vol=1_000_000, last_vol=None):
    """构造 n 根日线，收盘价随便给、成交量恒定（最后一根可单独指定）。

    日期故意用确定性的过去年份（2020），而不是随会话漂移的"今天"附近——
    `_drop_forming_bar` 会把日期 == 美东当日的末根当半根丢掉，用当前年份
    的日期会让这个测试随运行日期变成薛定谔的通过/失败。
    """
    values = []
    for i in range(n):
        v = vol if (last_vol is None or i < n - 1) else last_vol
        values.append({"datetime": f"2020-01-{i+1:02d}", "close": str(100 + i),
                       "volume": str(v)})
    return {"values": values}


class TestFetchVolumeRatio:
    def test_unconfigured_returns_none_no_request(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        import http_gate

        def _boom(*a, **k):
            raise AssertionError("未配 key 却发了请求")

        monkeypatch.setattr(http_gate, "urlopen_gated", _boom)
        assert td.fetch_volume_ratio("NVDA") is None

    def test_computes_ratio_against_window_mean(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        # 20 根均量 1,000,000，最后一根 2,000,000 → ratio 应 > 1
        payload = _bars(30, vol=1_000_000, last_vol=2_000_000)
        _patch_http(monkeypatch, _resp(payload))
        r = td.fetch_volume_ratio("NVDA", window=20)
        assert r is not None
        assert r["recent_volume"] == 2_000_000
        # 19 根 1,000,000 + 1 根 2,000,000，均量含最新一根自己 = 1,050,000
        assert r["avg_volume"] == 1_050_000
        assert r["volume_ratio"] == pytest.approx(2_000_000 / 1_050_000, rel=1e-6)

    def test_zero_volume_returns_none_not_fake_ratio(self, monkeypatch):
        """成交量全 0（数据不可得的常见形状）不能算出一个假的 1.0/0.0 比值。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        payload = _bars(30, vol=0)
        _patch_http(monkeypatch, _resp(payload))
        assert td.fetch_volume_ratio("NVDA") is None

    def test_too_few_bars_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        payload = _bars(5)
        _patch_http(monkeypatch, _resp(payload))
        assert td.fetch_volume_ratio("NVDA", window=20) is None

    def test_api_error_status_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(
            {"code": 429, "message": "out of credits", "status": "error"}))
        assert td.fetch_volume_ratio("NVDA") is None

    def test_shares_daily_used_counter_with_fetch_daily_closes(self, monkeypatch):
        """两个入口共用同一次抓取层——各自独立计数会让配额观测失真。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(_bars(30)))
        assert td.stats()["daily_used"] == 0
        td.fetch_volume_ratio("NVDA")
        assert td.stats()["daily_used"] == 1


class TestFillVolumeFromTwelveData:
    def test_skips_request_when_already_present(self, monkeypatch):
        """yfinance 已经给出真实 volume_ratio 时不该再打一次外部请求。"""
        def _boom(*a, **k):
            raise AssertionError("已有真实值却还去请求 Twelve Data")

        import twelve_data as _td_mod
        monkeypatch.setattr(_td_mod, "fetch_volume_ratio", _boom)

        out = {"momentum_5d": 1.2, "volume_ratio": 0.83}
        result = dp._fill_volume_from_twelvedata("XOM", out)
        assert result == out

    def test_fills_gap_when_missing(self, monkeypatch):
        import twelve_data as _td_mod
        monkeypatch.setattr(
            _td_mod, "fetch_volume_ratio",
            lambda ticker: {"volume_ratio": 0.81, "avg_volume": 14_000_000,
                            "recent_volume": 11_000_000})

        out = {"momentum_5d": -4.47, "momentum_source": "price_index"}
        result = dp._fill_volume_from_twelvedata("XOM", out)
        assert result["volume_ratio"] == 0.81
        assert result["avg_volume"] == 14_000_000
        assert result["volume_source"] == "twelvedata"
        # 不能顺带把已有字段挤掉
        assert result["momentum_5d"] == -4.47
        assert result["momentum_source"] == "price_index"

    def test_none_input_still_fills_volume(self, monkeypatch):
        """momentum 索引也没数据时 out 是 None——volume 仍应独立尝试补上。"""
        import twelve_data as _td_mod
        monkeypatch.setattr(
            _td_mod, "fetch_volume_ratio",
            lambda ticker: {"volume_ratio": 0.5, "avg_volume": 900_000,
                            "recent_volume": 450_000})

        result = dp._fill_volume_from_twelvedata("XOM", None)
        assert result["volume_ratio"] == 0.5

    def test_twelvedata_also_unavailable_keeps_honest_none(self, monkeypatch):
        """两条回落都没有时必须保持诚实缺数据，不能编一个默认值。"""
        import twelve_data as _td_mod
        monkeypatch.setattr(_td_mod, "fetch_volume_ratio", lambda ticker: None)

        out = {"momentum_5d": -4.47}
        result = dp._fill_volume_from_twelvedata("XOM", out)
        assert result == out
        assert "volume_ratio" not in result

        result_none = dp._fill_volume_from_twelvedata("XOM", None)
        assert result_none is None

    def test_import_failure_does_not_raise(self, monkeypatch):
        """Twelve Data 模块本身有问题（比如没装）不该炸掉整条降级链。"""
        import twelve_data as _td_mod

        def _boom(ticker):
            raise RuntimeError("module broken")

        monkeypatch.setattr(_td_mod, "fetch_volume_ratio", _boom)
        out = {"momentum_5d": 1.0}
        result = dp._fill_volume_from_twelvedata("XOM", out)
        assert result == out  # 静默降级，不抛异常
