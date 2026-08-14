"""
v0.43.18 自攒 IV 历史测试

背景：原 IV Rank 的"历史 IV"是 `HV序列 × 单个标量 iv_premium`，而给整条序列
乘同一常数不改变排序 ⇒ 算出的 IV Rank 数学上恒等于 HV Rank（实测 NVDA 未
clamp 时 62.91 vs 62.91）。真 IV Rank 必须用真实 IV 的历史分布，只能自攒。
"""

import json

import pytest

from iv_history import (
    IV_RANK_MIN_DAYS,
    coverage_report,
    iv_rank_from_history,
    load_iv_history,
)


def _write_snap(cache_dir, ticker, date, **fields):
    p = cache_dir / f"options_snapshot_{ticker}_{date}.json"
    p.write_text(json.dumps({"ticker": ticker, "date": date, **fields}), encoding="utf-8")
    return p


class TestLoadIVHistory:
    def test_reads_iv_raw_observed_in_date_order(self, tmp_path):
        _write_snap(tmp_path, "NVDA", "2026-08-03", iv_raw_observed=30.0)
        _write_snap(tmp_path, "NVDA", "2026-08-01", iv_raw_observed=10.0)
        _write_snap(tmp_path, "NVDA", "2026-08-02", iv_raw_observed=20.0)

        ivs, n = load_iv_history("NVDA", str(tmp_path))
        assert ivs == [10.0, 20.0, 30.0]   # 按日期升序，不是文件名顺序
        assert n == 3

    def test_ignores_iv_current_entirely(self, tmp_path):
        """绝不退回读 iv_current——它是 120h 缓存造出的 5 天一阶阶梯，非每日观测"""
        _write_snap(tmp_path, "NVDA", "2026-08-01", iv_current=55.0)  # 无 iv_raw_observed
        ivs, n = load_iv_history("NVDA", str(tmp_path))
        assert ivs == [] and n == 0

    def test_filters_out_of_range_and_bad_values(self, tmp_path):
        _write_snap(tmp_path, "NVDA", "2026-08-01", iv_raw_observed=3.0)      # < 5 无效
        _write_snap(tmp_path, "NVDA", "2026-08-02", iv_raw_observed=200.0)    # > 150 异常
        _write_snap(tmp_path, "NVDA", "2026-08-03", iv_raw_observed=None)
        _write_snap(tmp_path, "NVDA", "2026-08-04", iv_raw_observed="bad")
        _write_snap(tmp_path, "NVDA", "2026-08-05", iv_raw_observed=42.0)     # 唯一有效

        ivs, n = load_iv_history("NVDA", str(tmp_path))
        assert ivs == [42.0] and n == 1

    def test_does_not_leak_across_tickers(self, tmp_path):
        _write_snap(tmp_path, "NVDA", "2026-08-01", iv_raw_observed=40.0)
        _write_snap(tmp_path, "TSLA", "2026-08-01", iv_raw_observed=60.0)

        assert load_iv_history("NVDA", str(tmp_path))[0] == [40.0]
        assert load_iv_history("TSLA", str(tmp_path))[0] == [60.0]

    def test_respects_max_days_window(self, tmp_path):
        for i in range(1, 11):
            _write_snap(tmp_path, "NVDA", f"2026-08-{i:02d}", iv_raw_observed=float(i))
        ivs, n = load_iv_history("NVDA", str(tmp_path), max_days=3)
        assert ivs == [8.0, 9.0, 10.0]  # 取最近 3 天
        assert n == 3

    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_iv_history("NVDA", str(tmp_path / "nope")) == ([], 0)


class TestIVRankFromHistory:
    def test_minmax_rank_and_percentile(self):
        hist = [10.0, 20.0, 30.0, 40.0]
        rank, pct = iv_rank_from_history(30.0, hist)
        assert rank == pytest.approx((30 - 10) / (40 - 10) * 100, abs=0.01)
        assert pct == pytest.approx(50.0, abs=0.01)  # 2/4 低于 30

    def test_clamped_to_0_100(self):
        hist = [20.0, 30.0]
        assert iv_rank_from_history(5.0, hist)[0] == 0.0
        assert iv_rank_from_history(99.0, hist)[0] == 100.0

    def test_degenerate_cases_return_none_not_fake_neutral(self):
        """样本不足/全等时返回 None 让调用方降级，绝不编造 50.0 中性值"""
        assert iv_rank_from_history(30.0, []) == (None, None)
        assert iv_rank_from_history(30.0, [25.0]) == (None, None)
        assert iv_rank_from_history(30.0, [25.0, 25.0]) == (None, None)  # hi==lo
        assert iv_rank_from_history(None, [10.0, 20.0]) == (None, None)


class TestCoverageReport:
    def test_counts_days_per_ticker_sorted_desc(self, tmp_path):
        for i in range(1, 4):
            _write_snap(tmp_path, "NVDA", f"2026-08-{i:02d}", iv_raw_observed=40.0 + i)
        _write_snap(tmp_path, "TSLA", "2026-08-01", iv_raw_observed=50.0)

        rep = coverage_report(str(tmp_path))
        assert rep["NVDA"] == 3
        assert rep["TSLA"] == 1
        assert list(rep) == ["NVDA", "TSLA"]  # 按天数降序


class TestAnalyzerIntegration:
    def test_falls_back_to_hv_proxy_when_history_short(self, tmp_path, monkeypatch):
        """真实 IV 样本不足阈值时必须降级并如实标注 hv_proxy"""
        from options_analyzer import OptionsAgent

        _write_snap(tmp_path, "NVDA", "2026-08-01", iv_raw_observed=40.0)

        agent = OptionsAgent()
        monkeypatch.setattr(agent.fetcher, "cache_dir", str(tmp_path))
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {
            "calls": [{"strike": 100, "openInterest": 500, "impliedVolatility": 0.40,
                       "gamma": 0.04, "volume": 100}],
            "puts": [{"strike": 95, "openInterest": 400, "impliedVolatility": 0.42,
                      "gamma": 0.03, "volume": 80}],
            "expirations": ["2026-09-18"], "source": "real",
        })
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                            lambda t: [20.0 + i for i in range(30)])
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda t, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda t: None)

        r = agent.analyze("NVDA", stock_price=100.0)
        assert r["iv_rank_source"] == "hv_proxy"
        assert r["iv_rank_window_days"] is None
        assert r["iv_rank"] is not None  # 仍产出可用值，只是标注了口径

    def test_uses_real_iv_when_enough_history(self, tmp_path, monkeypatch):
        """样本达标时必须切到真实 IV 历史并标注窗口长度"""
        from options_analyzer import OptionsAgent

        for i in range(IV_RANK_MIN_DAYS + 5):
            d = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
            _write_snap(tmp_path, "NVDA", d, iv_raw_observed=20.0 + (i % 40))

        agent = OptionsAgent()
        monkeypatch.setattr(agent.fetcher, "cache_dir", str(tmp_path))
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {
            "calls": [{"strike": 100, "openInterest": 500, "impliedVolatility": 0.40,
                       "gamma": 0.04, "volume": 100}],
            "puts": [{"strike": 95, "openInterest": 400, "impliedVolatility": 0.42,
                      "gamma": 0.03, "volume": 80}],
            "expirations": ["2026-09-18"], "source": "real",
        })
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                            lambda t: [20.0 + i for i in range(30)])
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda t, iv: None)
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda t: None)

        r = agent.analyze("NVDA", stock_price=100.0)
        assert r["iv_rank_source"].startswith("real_iv_")
        assert r["iv_rank_window_days"] >= IV_RANK_MIN_DAYS

    def test_real_iv_rank_uses_raw_observation_not_degraded_cache(self, tmp_path, monkeypatch):
        """v0.43.20 回归：分子必须用今日 iv_raw_observed，不能用被缓存降级的 current_iv

        分母 _real_hist 由每日 iv_raw_observed 组成；current_iv 在降级块可能被
        last_valid_iv（TTL 120h ≈ 最长 5 天前）替换。若拿陈旧分子比对新鲜分布，
        就是口径错配——与 v0.43.19 修掉的"拿真实 IV 比 HV 分布"同一类错误。

        ⚠️ 必须冻结时钟：降级分支只在 `not _market_open` 时进入。若按真实时间跑，
        盘中运行时该分支根本不执行，测试会变成永远通过的空跑（本测试初版即如此，
        在盘中写就、盘中验证，退回 bug 版仍绿）。
        """
        import options_analyzer as _oa
        from datetime import datetime as _dt, timezone as _tz

        class _AfterHoursDateTime(_dt):
            @classmethod
            def now(cls, tz=None):
                # 2026-08-14 22:00 UTC = 18:00 ET（周五收盘后）→ _market_open=False
                fixed = _dt(2026, 8, 14, 22, 0, tzinfo=_tz.utc)
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        monkeypatch.setattr(_oa, "datetime", _AfterHoursDateTime)

        from options_analyzer import OptionsAgent

        # 历史 IV 分布 20~59；今日链上真实 IV = 58%（分布高位）
        for i in range(IV_RANK_MIN_DAYS + 5):
            d = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
            _write_snap(tmp_path, "NVDA", d, iv_raw_observed=20.0 + (i % 40))

        agent = OptionsAgent()
        monkeypatch.setattr(agent.fetcher, "cache_dir", str(tmp_path))
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: {
            "calls": [{"strike": 100, "openInterest": 500, "impliedVolatility": 0.58,
                       "gamma": 0.04, "volume": 100}],
            "puts": [{"strike": 95, "openInterest": 400, "impliedVolatility": 0.58,
                      "gamma": 0.03, "volume": 80}],
            "expirations": ["2026-09-18"], "source": "real",
        })
        monkeypatch.setattr(agent.fetcher, "fetch_historical_hv",
                            lambda t: [20.0 + i for i in range(30)])
        monkeypatch.setattr(agent.fetcher, "_save_last_valid_iv", lambda t, iv: None)
        # 关键：模拟 120h 陈旧缓存 21%（分布低位），与今日真实 58% 相反
        monkeypatch.setattr(agent.fetcher, "_read_last_valid_iv", lambda t: 21.0)

        r = agent.analyze("NVDA", stock_price=100.0)

        # 前置校验：确认降级确实发生了，否则本测试无意义（防止再次退化为空跑）
        assert r["iv_current"] == pytest.approx(21.0, abs=0.5), (
            f"降级未生效（iv_current={r['iv_current']}），本测试无法验证口径问题"
        )
        assert r["iv_rank_source"].startswith("real_iv_")
        assert r["iv_raw_observed"] == pytest.approx(58.0, abs=0.5)
        # 用今日真实观测 58 → 高位 rank；若误用降级缓存 21 → 会掉到低位
        assert r["iv_rank"] > 80, (
            f"iv_rank={r['iv_rank']} 偏低，疑似误用了降级后的 current_iv "
            f"(={r['iv_current']}) 而非今日原始观测 {r['iv_raw_observed']}"
        )
