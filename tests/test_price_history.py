"""
自攒收盘价历史与动量诚实性（v0.43.25 回归）

背景：`momentum_5d` 长期依赖 `yf.Ticker(t).history()`，而历史K线排在 30 只标的
扫完之后，yfinance 配额已耗尽（2026-08-14 全天 363 条 429）→ 返回 None。
两个 Agent 拿同一份 None，处理方式相反：
- BuzzBee 诚实写 None → 下游崩（v0.43.23 已修渲染侧）
- ScoutBee `or 0.0` 伪造"持平" → 无声进入评分，近 28 个扫描日 395 次背离检测
  **全部** severity=0，功能结构性死亡

本模块从自有观测（pheromone.db 权威价 + 期权快照）攒收盘价，不经任何外部接口。
"""

import json

import pytest

import price_history as ph


def _idx(tmp_path, ticker, rows):
    """直接写索引文件，绕开 DB 与快照"""
    p = tmp_path / f"price_history_{ticker}.jsonl"
    p.write_text("".join(json.dumps({"date": d, "close": c}) + "\n" for d, c in rows),
                 encoding="utf-8")
    (tmp_path / f".price_index_migrated_{ticker}").write_text("test\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """隔离生产 pheromone.db，测试只看索引"""
    monkeypatch.setattr(ph, "_load_db_prices", lambda t, db_path="pheromone.db": {})


class TestMomentumHonesty:
    def test_returns_none_not_zero_when_history_short(self, tmp_path):
        """缺数据必须是 None。0.0 的含义是"持平"——一个真实的市场状态，
        它让背离检测永远不触发（实测 395/395 severity=0）"""
        _idx(tmp_path, "NVDA", [("2026-08-13", 100.0), ("2026-08-14", 101.0)])
        assert ph.momentum_5d("NVDA", str(tmp_path)) is None

    def test_no_history_returns_none(self, tmp_path):
        assert ph.momentum_5d("NOPE", str(tmp_path)) is None

    def test_computes_from_real_closes(self, tmp_path):
        # 8/7(五) 8/10 8/11 8/12 8/13 8/14 —— 8/14 距 8/7 恰好 5 个交易日
        _idx(tmp_path, "NVDA", [
            ("2026-08-07", 100.0), ("2026-08-10", 101.0), ("2026-08-11", 102.0),
            ("2026-08-12", 103.0), ("2026-08-13", 104.0), ("2026-08-14", 110.0),
        ])
        assert ph.momentum_5d("NVDA", str(tmp_path)) == pytest.approx(10.0, abs=0.01)


class TestTradingDayAnchoring:
    def test_anchor_uses_trading_day_distance_not_row_count(self, tmp_path):
        """索引只有"扫描跑过的日子"，往回数 5 条可能跨越 9 个交易日——
        那样算出来的数字挂着"5 日动量"的名字却名不副实"""
        _idx(tmp_path, "NVDA", [
            ("2026-06-01", 50.0),   # 很久以前
            ("2026-08-07", 100.0),  # 距 8/14 恰好 5 个交易日 → 应选这个
            ("2026-08-12", 103.0),
            ("2026-08-13", 104.0),
            ("2026-08-14", 110.0),
        ])
        assert ph.momentum_5d("NVDA", str(tmp_path)) == pytest.approx(10.0, abs=0.01)

    def test_returns_none_when_data_too_sparse(self, tmp_path):
        """锚点落在 MOMENTUM_MAX_GAP 之外时宁可 None，不出名不副实的数"""
        _idx(tmp_path, "NVDA", [("2026-05-01", 50.0), ("2026-08-14", 110.0)])
        assert ph.momentum_5d("NVDA", str(tmp_path)) is None

    def test_non_trading_days_excluded(self, tmp_path):
        """周末/强制跑写出的非交易日快照不得进入序列（会把窗口整体挪位）"""
        _idx(tmp_path, "NVDA", [
            ("2026-08-14", 110.0),
            ("2026-08-15", 999.0),  # 周六
            ("2026-08-16", 888.0),  # 周日
        ])
        dates = [d for d, _ in ph.load_price_history("NVDA", str(tmp_path))]
        assert dates == ["2026-08-14"]


class TestDespike:
    def test_isolated_spike_removed(self, tmp_path):
        """期权快照价有已知污染：NVDA 2026-06-10 记为 63.0，前后都是 ~200"""
        _idx(tmp_path, "NVDA", [
            ("2026-08-10", 200.0), ("2026-08-11", 63.0), ("2026-08-12", 202.0),
        ])
        vals = [c for _, c in ph.load_price_history("NVDA", str(tmp_path))]
        assert 63.0 not in vals

    def test_real_gap_up_preserved(self, tmp_path):
        """真实跳空（财报）不会次日原路返回——判据是偏离前后均值，不是逐日涨跌幅，
        正是为了区分"涨上去又回来"（尖峰）和"涨上去留在那"（台阶）"""
        _idx(tmp_path, "NVDA", [
            ("2026-08-10", 100.0), ("2026-08-11", 130.0), ("2026-08-12", 132.0),
        ])
        vals = [c for _, c in ph.load_price_history("NVDA", str(tmp_path))]
        assert 130.0 in vals

    def test_distant_neighbors_not_treated_as_spike(self, tmp_path):
        """隔一周的两点相差 10% 完全正常。不做 gap 感知时误杀率实测高达 21.6%"""
        _idx(tmp_path, "NVDA", [
            ("2026-06-01", 100.0), ("2026-07-01", 140.0), ("2026-08-03", 105.0),
        ])
        vals = [c for _, c in ph.load_price_history("NVDA", str(tmp_path))]
        assert 140.0 in vals


class TestDBAuthority:
    def test_db_price_overrides_contaminated_snapshot(self, tmp_path, monkeypatch):
        """QCOM 2026-08-14：期权快照 185.0，DB 权威价 165.94。DB 必须赢。"""
        _idx(tmp_path, "QCOM", [("2026-08-13", 164.47), ("2026-08-14", 185.0)])
        monkeypatch.setattr(ph, "_load_db_prices",
                            lambda t, db_path="pheromone.db": {"2026-08-14": 165.94})
        assert dict(ph.load_price_history("QCOM", str(tmp_path)))["2026-08-14"] == 165.94


class TestDivergenceHandlesNone:
    """上游改诚实必须同时改下游——否则 None 只会把崩溃点搬个家（v0.43.23 教训）"""

    def test_none_momentum_does_not_crash(self):
        from swarm_agents.sentiment import _detect_sentiment_price_divergence as d
        assert d(80, None, "NVDA")["divergence_type"] == "unavailable"

    def test_unavailable_is_distinct_from_none(self):
        """"查不了"和"查过、没背离"对下游含义完全不同，不能混为一谈"""
        from swarm_agents.sentiment import _detect_sentiment_price_divergence as d
        assert d(50, 0.5, "NVDA")["divergence_type"] == "none"
        assert d(50, None, "NVDA")["divergence_type"] == "unavailable"

    @pytest.mark.parametrize("sent,mom,expect", [
        (80, -8.0, "bull_trap"),
        (20, 8.0, "hidden_opportunity"),
        (80, 8.0, "none"),
        (20, -8.0, "none"),
    ])
    def test_detection_actually_fires(self, sent, mom, expect):
        """证明功能不是死的：修复前 momentum 恒为 0.0，两个分支永远够不到阈值"""
        from swarm_agents.sentiment import _detect_sentiment_price_divergence as d
        assert d(sent, mom, "NVDA")["divergence_type"] == expect
