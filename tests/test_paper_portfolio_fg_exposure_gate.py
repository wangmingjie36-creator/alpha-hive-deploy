"""F&G 组合层敞口控制门（v0.45.262，实验中，`CONFIG["fg_exposure_gate"]["enabled"]` 默认 False）。

背景：`BuzzBeeWhisper` 把 F&G 当逐标的情绪信号用已实测证据不支持（顺周期现状明显是
"顺周期/去掉/逆周期"三种处理里最差的，且结构上"当天全池同一常数塞进逐标的阈值分类"
本身就是噪音，与顺/逆周期无关；完整推导见 auto-memory `alpha-hive-fear-greed-dead-wire.md`）。
CNN 官方把 F&G 设计成大盘/组合择时工具，本版把它挪到组合层的仓位敞口控制。

三条硬约束：
1. **`enabled=False`（默认）时必须是真正的 no-op**——不是"看起来关了"，是无论传入什么
   `market_fear_greed` 都不改变 `size_usd`。这是本次改动不追加 `ic_rerun_readiness`
   世代边界的直接依据，必须实测，不能靠"有 if 挡着"推断。
2. **只收紧顺势方向**：极度贪婪日的新多头、极度恐惧日的新空头才打折；逆势方向
   （贪婪日空头、恐惧日多头）与非极端日一律不动——不给逆势方向加仓。
3. **`_lookup_market_fear_greed` 禁止前视、禁止用陈旧读数顶替**：只精确匹配 `date == as_of`，
   缺当天的行就是没有，不查"最近几天"（同 `_lookup_vol_ann` 的"不前视"原则，但窗口收紧到
   0 天——F&G 是逐日读数，不是慢变量）。
"""

import sqlite3

import pytest

import paper_portfolio as pp

NAV = 100_000.0


@pytest.fixture(autouse=True)
def _clear_caches():
    pp._VOL_ANN_CACHE.clear()
    pp._FG_LOOKUP_CACHE.clear()
    yield
    pp._VOL_ANN_CACHE.clear()
    pp._FG_LOOKUP_CACHE.clear()


@pytest.fixture(autouse=True)
def _tier_mode_neutral(monkeypatch):
    """固定分档模式 + 中性胜率乘数，仓位大小只随 conf 与本文件要测的 F&G 门变化。"""
    monkeypatch.setitem(pp.CONFIG, "sizing_mode", "tier")
    monkeypatch.setattr(pp, "_size_multiplier", lambda ticker, closed: 1.0)


def _gate(**over):
    g = {"enabled": True, "extreme_fear": 25, "extreme_greed": 75,
         "long_size_mult": 0.5, "short_size_mult": 0.5}
    g.update(over)
    return g


def _snapshot(ticker="NVDA", direction="bullish", score=7.0, entry_price=100.0):
    return {"ticker": ticker, "direction": direction, "composite_score": score,
            "entry_price": entry_price, "agent_votes": {"a": 7.0, "b": 7.0}}


# ══════════════════════════════════════════════════════════════════════════════
# 1. _fg_exposure_multiplier：纯函数，穷举方向 × F&G 区间
# ══════════════════════════════════════════════════════════════════════════════

class TestExposureMultiplierPureFunction:

    def test_disabled_is_noop_regardless_of_fg(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate(enabled=False))
        for direction in ("bullish", "bearish"):
            for fg in (None, {"value": 5.0, "is_cnn": True}, {"value": 95.0, "is_cnn": True}):
                assert pp._fg_exposure_multiplier(direction, fg) == (1.0, "")

    def test_no_fg_reading_is_noop(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate())
        assert pp._fg_exposure_multiplier("bullish", None) == (1.0, "")
        assert pp._fg_exposure_multiplier("bullish", {}) == (1.0, "")

    def test_extreme_greed_discounts_new_long(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate(long_size_mult=0.4))
        mult, note = pp._fg_exposure_multiplier("bullish", {"value": 80.0, "is_cnn": True})
        assert mult == 0.4
        assert "fg_extreme_greed" in note

    def test_extreme_fear_discounts_new_short(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate(short_size_mult=0.3))
        mult, note = pp._fg_exposure_multiplier("bearish", {"value": 15.0, "is_cnn": False})
        assert mult == 0.3
        assert "fg_extreme_fear" in note

    def test_off_trend_direction_is_not_adjusted(self, monkeypatch):
        """不给逆势方向加仓：贪婪日的空头、恐惧日的多头——不调整，不是本方案要做的事。"""
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate())
        assert pp._fg_exposure_multiplier("bearish", {"value": 80.0, "is_cnn": True}) == (1.0, "")
        assert pp._fg_exposure_multiplier("bullish", {"value": 15.0, "is_cnn": True}) == (1.0, "")

    def test_non_extreme_reading_is_not_adjusted(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate())
        for v in (25.0, 50.0, 75.0):  # 边界值本身不算"超过"阈值（严格 >/<）
            assert pp._fg_exposure_multiplier("bullish", {"value": v, "is_cnn": True}) == (1.0, "")
            assert pp._fg_exposure_multiplier("bearish", {"value": v, "is_cnn": True}) == (1.0, "")

    def test_non_finite_value_is_not_adjusted(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate())
        for v in (float("nan"), float("inf"), None, "80"):
            assert pp._fg_exposure_multiplier("bullish", {"value": v}) == (1.0, "")


# ══════════════════════════════════════════════════════════════════════════════
# 2. _open_position：接线正确，且 enabled=False 是真正的 no-op
# ══════════════════════════════════════════════════════════════════════════════

class TestOpenPositionIntegration:

    def test_disabled_gate_never_changes_size_regardless_of_fg(self, monkeypatch):
        """核心落地前提：`enabled=False` 时，无论传什么 F&G 读数，size_usd 必须与不传
        `market_fear_greed` 完全相同——这是允许本次改动不追加世代边界的实测依据。"""
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate(enabled=False))
        snap = _snapshot()
        ohlc = {"2026-09-15": {"Close": 100.0}}
        baseline = pp._open_position(snap, NAV, "2026-09-15", ohlc, [])
        for fg in ({"value": 95.0, "is_cnn": True}, {"value": 3.0, "is_cnn": False}):
            pos = pp._open_position(snap, NAV, "2026-09-15", ohlc, [], market_fear_greed=fg)
            assert pos.size_usd == baseline.size_usd
            assert pos.sizing == baseline.sizing

    def test_enabled_gate_discounts_matching_new_position(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate(long_size_mult=0.5))
        snap = _snapshot(direction="bullish")
        ohlc = {"2026-09-15": {"Close": 100.0}}
        baseline = pp._open_position(snap, NAV, "2026-09-15", ohlc, [], market_fear_greed=None)
        discounted = pp._open_position(snap, NAV, "2026-09-15", ohlc, [],
                                       market_fear_greed={"value": 90.0, "is_cnn": True})
        assert discounted.size_usd == pytest.approx(baseline.size_usd * 0.5, rel=1e-6)
        assert "fg_extreme_greed" in discounted.sizing
        assert "fg_extreme_greed" not in baseline.sizing

    def test_enabled_gate_leaves_off_trend_position_untouched(self, monkeypatch):
        """极度贪婪日的空头不受影响——同一份极端读数，方向不匹配就不调整。"""
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate())
        snap = _snapshot(direction="bearish")
        ohlc = {"2026-09-15": {"Close": 100.0}}
        baseline = pp._open_position(snap, NAV, "2026-09-15", ohlc, [], market_fear_greed=None)
        same = pp._open_position(snap, NAV, "2026-09-15", ohlc, [],
                                 market_fear_greed={"value": 90.0, "is_cnn": True})
        assert same.size_usd == baseline.size_usd

    def test_discount_applied_before_minimum_size_validation(self, monkeypatch):
        """v0.45.97 的 `size_usd<=1` 守卫必须校验**打折之后**的最终值——不能让一个
        打折前侥幸站在 $1 上方、打折后跌到 $1 以下的候选蒙混过关。

        取数刻意让"打折前"与"打折后"分别落在守卫的两侧（1.8 → 0.9）：若守卫读的是
        打折前的值就会误放行，返回一个 size_usd=0.9 的仓位（本该在守卫这里就被拒绝）。
        """
        monkeypatch.setitem(pp.CONFIG, "size_pct_by_tier", {"low": 0.0018, "mid": 0.0, "high": 0.0})
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate(long_size_mult=0.5))
        snap = _snapshot(direction="bullish")
        snap["agent_votes"] = {"a": 5.0, "b": 9.0, "c": 1.0}  # dim_std 高 + 有 bear 信号 → conf="low"
        snap["bear_signals"] = ["overvalued"]
        ohlc = {"2026-09-15": {"Close": 100.0}}
        # 打折前 0.0018% of 100_000 = $1.8（> 1，若守卫读这个值会误放行）；
        # 打折后 $0.9（<= 1，正确实现应该在这里拒绝）。
        pos = pp._open_position(snap, NAV, "2026-09-15", ohlc, [],
                                market_fear_greed={"value": 90.0, "is_cnn": True})
        assert pos is None, "打折后 $0.9 应被 size_usd<=1 守卫拒绝——若非 None 说明守卫读的是打折前的值"

    def test_discount_that_does_not_cross_the_floor_still_opens(self, monkeypatch):
        """对照上一条：打折后仍 > 1 时必须正常开仓——不是"打了折就一律拒绝"。"""
        monkeypatch.setitem(pp.CONFIG, "size_pct_by_tier", {"low": 0.006, "mid": 0.0, "high": 0.0})
        monkeypatch.setitem(pp.CONFIG, "fg_exposure_gate", _gate(long_size_mult=0.5))
        snap = _snapshot(direction="bullish")
        snap["agent_votes"] = {"a": 5.0, "b": 9.0, "c": 1.0}
        snap["bear_signals"] = ["overvalued"]
        ohlc = {"2026-09-15": {"Close": 100.0}}
        # 打折前 $6，打折后 $3——两侧都 > 1。
        pos = pp._open_position(snap, NAV, "2026-09-15", ohlc, [],
                                market_fear_greed={"value": 90.0, "is_cnn": True})
        assert pos is not None and pos.size_usd == pytest.approx(3.0, rel=1e-6)


# ══════════════════════════════════════════════════════════════════════════════
# 3. _lookup_market_fear_greed：禁止前视、禁止陈旧值顶替（同 _lookup_vol_ann 的用例结构）
# ══════════════════════════════════════════════════════════════════════════════

class TestLookupMarketFearGreed:

    def _seed(self, db_path, rows):
        con = sqlite3.connect(str(db_path))
        con.execute("CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL)")
        con.executemany("INSERT INTO signal_archive VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_missing_db_returns_none(self, tmp_path):
        assert pp._lookup_market_fear_greed("2026-09-15", tmp_path / "nope.db") is None

    def test_missing_table_returns_none(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        assert pp._lookup_market_fear_greed("2026-09-15", db) is None

    def test_no_row_for_that_exact_date_returns_none(self, tmp_path):
        """不做"最近几天"窗口——前一天有**完整**的两行读数也不算数，防止悄悄延续
        过时的极端值。两个信号都为前一天而不是只缺一个，排除"因为缺 is_cnn 那道
        校验而顺带挡住"这种巧合：这里要单独测的是"日期必须精确匹配"这条不变式。"""
        db = tmp_path / "t.db"
        self._seed(db, [("2026-09-14", "NVDA", "market.fear_greed", 30.0),
                        ("2026-09-14", "NVDA", "market.fear_greed_is_cnn", 1.0)])
        assert pp._lookup_market_fear_greed("2026-09-15", db) is None

    def test_exact_date_wins_over_earlier_complete_reading(self, tmp_path):
        """当天与前一天**都**有完整读数时，必须返回当天的值——不能被"就近"或
        `ORDER BY date DESC` 之类的宽松匹配悄悄换成别的日期。"""
        db = tmp_path / "t.db"
        self._seed(db, [("2026-09-14", "NVDA", "market.fear_greed", 30.0),
                        ("2026-09-14", "NVDA", "market.fear_greed_is_cnn", 1.0),
                        ("2026-09-15", "NVDA", "market.fear_greed", 70.0),
                        ("2026-09-15", "NVDA", "market.fear_greed_is_cnn", 0.0)])
        assert pp._lookup_market_fear_greed("2026-09-15", db) == {"value": 70.0, "is_cnn": False}

    def test_value_without_is_cnn_row_is_untrusted(self, tmp_path):
        """`market.fear_greed_is_cnn` 缺失 = 归档时判定为非真实观测——两者必须同时存在。"""
        db = tmp_path / "t.db"
        self._seed(db, [("2026-09-15", "NVDA", "market.fear_greed", 30.0)])
        assert pp._lookup_market_fear_greed("2026-09-15", db) is None

    def test_real_reading_is_returned_with_source(self, tmp_path):
        db = tmp_path / "t.db"
        self._seed(db, [("2026-09-15", "NVDA", "market.fear_greed", 33.0),
                        ("2026-09-15", "NVDA", "market.fear_greed_is_cnn", 1.0)])
        got = pp._lookup_market_fear_greed("2026-09-15", db)
        assert got == {"value": 33.0, "is_cnn": True}

    def test_crypto_source_is_labelled(self, tmp_path):
        db = tmp_path / "t.db"
        self._seed(db, [("2026-09-15", "AMC", "market.fear_greed", 13.0),
                        ("2026-09-15", "AMC", "market.fear_greed_is_cnn", 0.0)])
        assert pp._lookup_market_fear_greed("2026-09-15", db) == {"value": 13.0, "is_cnn": False}

    def test_result_is_cached(self, tmp_path):
        db = tmp_path / "t.db"
        self._seed(db, [("2026-09-15", "NVDA", "market.fear_greed", 33.0),
                        ("2026-09-15", "NVDA", "market.fear_greed_is_cnn", 1.0)])
        first = pp._lookup_market_fear_greed("2026-09-15", db)
        db.unlink()  # 删库后仍能从缓存拿到同一个结果，证明真的缓存了
        assert pp._lookup_market_fear_greed("2026-09-15", db) == first


# ══════════════════════════════════════════════════════════════════════════════
# 4. run_for_date：F&G 解析顺序——显式传入 > 今天实时 > 历史归档
# ══════════════════════════════════════════════════════════════════════════════

class TestRunForDateFearGreedResolution:

    def test_explicit_market_fear_greed_bypasses_lookup(self, monkeypatch):
        monkeypatch.setattr(pp, "pdt_today", lambda: "2099-01-01")  # 保证不会走"今天"分支
        called = []
        monkeypatch.setattr(pp, "_lookup_market_fear_greed", lambda *a, **k: called.append(1) or None)
        monkeypatch.setattr(pp, "_load_snapshots_for_date", lambda d: [])
        pp.run_for_date("2026-09-15", market_fear_greed={"value": 10.0, "is_cnn": True})
        assert called == [], "显式传入 market_fear_greed 时不应该再查历史归档"

    def test_today_calls_live_fear_greed_module(self, monkeypatch):
        monkeypatch.setattr(pp, "pdt_today", lambda: "2026-09-15")
        monkeypatch.setattr(pp, "_load_snapshots_for_date", lambda d: [_snapshot()])
        monkeypatch.setattr(pp, "_fetch_ohlc", lambda *a, **k: {"2026-09-15": {"Close": 100.0}})
        import fear_greed
        monkeypatch.setattr(fear_greed, "get_fear_greed",
                            lambda: {"value": 20, "is_real_data": True, "source": "cnn"})
        called = []
        monkeypatch.setattr(pp, "_lookup_market_fear_greed", lambda *a, **k: called.append(1) or None)
        captured = {}
        real_open = pp._open_position

        def _spy(snapshot, nav, as_of, ohlc, closed, market_fear_greed=None):
            captured["fg"] = market_fear_greed
            return real_open(snapshot, nav, as_of, ohlc, closed, market_fear_greed=market_fear_greed)
        monkeypatch.setattr(pp, "_open_position", _spy)
        pp.run_for_date("2026-09-15")
        assert called == [], "今天分支不应该查历史归档"
        assert captured["fg"] == {"value": 20, "is_cnn": True}

    def test_fallback_fear_greed_is_treated_as_no_reading(self, monkeypatch):
        """`fear_greed.get_fear_greed()` 的兜底结果 `is_real_data=False`——
        不能被当成真实读数喂进敞口门（否则会把兜底 50 误判为"非极端、不调整"，
        看似安全实则是把假数据当真数据处理了同一类问题）。用真实候选 + 在
        `_open_position` 上打桩，直接核对流进去的 `market_fear_greed` 是 None。"""
        monkeypatch.setattr(pp, "pdt_today", lambda: "2026-09-15")
        monkeypatch.setattr(pp, "_load_snapshots_for_date", lambda d: [_snapshot()])
        monkeypatch.setattr(pp, "_fetch_ohlc", lambda *a, **k: {"2026-09-15": {"Close": 100.0}})
        import fear_greed
        monkeypatch.setattr(fear_greed, "get_fear_greed",
                            lambda: {"value": 50, "is_real_data": False})
        captured = {}
        real_open = pp._open_position

        def _spy(snapshot, nav, as_of, ohlc, closed, market_fear_greed=None):
            captured["fg"] = market_fear_greed
            return real_open(snapshot, nav, as_of, ohlc, closed, market_fear_greed=market_fear_greed)
        monkeypatch.setattr(pp, "_open_position", _spy)
        result = pp.run_for_date("2026-09-15")
        assert result is not None  # 不应该因为 F&G 解析而炸掉整天的运行
        assert captured["fg"] is None, "兜底(非真实)读数必须被当作'今天没有 F&G 读数'，不能带值往下传"

    def test_past_date_queries_archive_not_live_module(self, monkeypatch):
        monkeypatch.setattr(pp, "pdt_today", lambda: "2026-09-15")
        monkeypatch.setattr(pp, "_load_snapshots_for_date", lambda d: [])
        called = []
        monkeypatch.setattr(pp, "_lookup_market_fear_greed", lambda as_of, db=None: called.append(as_of) or None)
        import fear_greed
        monkeypatch.setattr(fear_greed, "get_fear_greed",
                            lambda: (_ for _ in ()).throw(AssertionError("不应调用今天的实时读数")))
        pp.run_for_date("2026-03-10")
        assert called == ["2026-03-10"]

    def test_run_replay_clears_fg_lookup_cache_between_sandboxes(self, tmp_path, monkeypatch):
        """同一进程里连跑两个 `run_replay` 沙盒（`fg_exposure_gate_forward_test.py` 会话内
        对照两个变体正是这么用的）不能互相串味——同 `_VOL_ANN_CACHE` 那条教训。"""
        pp._FG_LOOKUP_CACHE[("2026-09-15", "leaked")] = {"value": 999.0, "is_cnn": True}
        monkeypatch.setattr(pp, "_all_snapshot_dates", lambda: [])
        pp.run_replay({}, tmp_path / "sandbox", dates=[])
        assert ("2026-09-15", "leaked") not in pp._FG_LOOKUP_CACHE, \
            "run_replay 退出后必须清空 F&G 查询缓存，否则下一个沙盒会读到上一轮缓存的结果"

    def test_fear_greed_lookup_failure_does_not_break_the_day(self, monkeypatch):
        """观测点而非致命路径：F&G 解析失败要降级成"当天无读数"，不能让整个交易日报错。"""
        monkeypatch.setattr(pp, "pdt_today", lambda: "2026-09-15")
        monkeypatch.setattr(pp, "_load_snapshots_for_date", lambda d: [])
        import fear_greed
        monkeypatch.setattr(fear_greed, "get_fear_greed",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        result = pp.run_for_date("2026-09-15")
        assert result is not None
