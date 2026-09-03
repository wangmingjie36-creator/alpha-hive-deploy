"""波动率目标仓位（v0.45.100）—— 替代 v0.39.0 固定分档。

三条硬约束：
1. σ=35% 的标的必须落回旧档位（high 5% / mid 3%）——「校准」不是口头说说；
2. sizing_mode="tier" 逐字节还原旧算法，供 run_replay 对照历史；
3. 波动率缺失 / NaN 时**显式**退回分档（note 里可见 + warning），绝不静默。
"""

import logging
import math
import sqlite3

import pytest

import paper_portfolio as pp

NAV = 50_000.0
VT = pp.CONFIG["vol_target"]


@pytest.fixture(autouse=True)
def _clear_caches():
    pp._PRICE_CACHE.clear()
    pp._OHLC_FULL.clear()
    pp._VOL_ANN_CACHE.clear()
    yield
    pp._PRICE_CACHE.clear()
    pp._OHLC_FULL.clear()
    pp._VOL_ANN_CACHE.clear()


@pytest.fixture(autouse=True)
def _vol_target_mode(monkeypatch):
    """每个测试从干净的 vol_target 模式出发；胜率乘数保持中性。"""
    monkeypatch.setitem(pp.CONFIG, "sizing_mode", "vol_target")
    monkeypatch.setattr(pp, "_size_multiplier", lambda ticker, closed: 1.0)


def _old_tier_size(nav, conf, low_conviction=False):
    """v0.39.0 旧算法的独立复刻（胜率乘数=1）——作为「逐字节」对照基准。"""
    size = nav * (pp.CONFIG["size_pct_by_tier"].get(conf, 0.0) / 100.0) * 1.0
    if low_conviction:
        size *= 0.5
    return size


# ══════════════════════════════════════════════════════════════════════════════
# 公式与校准
# ══════════════════════════════════════════════════════════════════════════════

class TestVolTargetFormula:

    def test_sigma_35_matches_old_tiers(self):
        """(a) 校准声明：σ=35% → high 5% / mid 3%，与旧分档持平。"""
        size_h, note_h = pp._compute_position_size(NAV, "high", "X", [], vol_ann=35.0)
        size_m, note_m = pp._compute_position_size(NAV, "mid", "X", [], vol_ann=35.0)
        assert size_h == pytest.approx(NAV * 0.05)
        assert size_m == pytest.approx(NAV * 0.03)
        assert note_h.startswith("vol_target(") and "σ=35.0%" in note_h and "→5.0%" in note_h
        assert "→3.0%" in note_m

    def test_low_conf_gets_zero(self):
        size, _ = pp._compute_position_size(NAV, "low", "X", [], vol_ann=35.0)
        assert size == 0.0

    def test_sigma_70_is_half_of_sigma_35(self):
        """(b) 仓位与 σ 反比。"""
        s35, _ = pp._compute_position_size(NAV, "high", "X", [], vol_ann=35.0)
        s70, _ = pp._compute_position_size(NAV, "high", "X", [], vol_ann=70.0)
        assert s70 == pytest.approx(s35 / 2)

    def test_clamp_max_at_low_sigma(self):
        """(c) σ=10 → 原始 17.5% 被钳到 size_pct_max。"""
        size, note = pp._compute_position_size(NAV, "high", "X", [], vol_ann=10.0)
        assert size == pytest.approx(NAV * VT["size_pct_max"] / 100.0)
        assert "clamp_max" in note

    def test_clamp_min_at_high_sigma(self):
        """(c) σ=200 → 原始 0.875% 被钳到 size_pct_min。"""
        size, note = pp._compute_position_size(NAV, "high", "X", [], vol_ann=200.0)
        assert size == pytest.approx(NAV * VT["size_pct_min"] / 100.0)
        assert "clamp_min" in note

    def test_clamp_applies_before_conf_multiplier(self):
        """钳位作用于原始比例，置信乘数在钳位之后——mid 在 σ=10 时应为 max×0.6。"""
        size, _ = pp._compute_position_size(NAV, "mid", "X", [], vol_ann=10.0)
        assert size == pytest.approx(NAV * VT["size_pct_max"] / 100.0 * VT["conf_multiplier"]["mid"])

    def test_low_conviction_halving_still_applies(self):
        """(f) v0.22.0 的低置信减半在 vol_target 模式下仍生效。"""
        full, _ = pp._compute_position_size(NAV, "high", "X", [], vol_ann=35.0)
        half, _ = pp._compute_position_size(NAV, "high", "X", [], vol_ann=35.0,
                                            low_conviction=True)
        assert half == pytest.approx(full / 2)

    def test_win_rate_multiplier_still_applies(self, monkeypatch):
        monkeypatch.setattr(pp, "_size_multiplier", lambda ticker, closed: 1.2)
        size, _ = pp._compute_position_size(NAV, "high", "X", [], vol_ann=35.0)
        assert size == pytest.approx(NAV * 0.05 * 1.2)


# ══════════════════════════════════════════════════════════════════════════════
# 降级必须可见
# ══════════════════════════════════════════════════════════════════════════════

class TestFallbackIsVisible:

    def test_missing_vol_falls_back_to_tier_with_warning(self, monkeypatch, caplog):
        """(d) 查不到 σ → 用旧分档，note 带 tier_fallback，且有 warning 点名标的与日期。"""
        monkeypatch.setattr(pp, "_lookup_vol_ann", lambda ticker, as_of, db_path=None: None)
        with caplog.at_level(logging.WARNING, logger=pp._log.name):
            size, note = pp._compute_position_size(NAV, "high", "TSLA", [], as_of="2026-09-02")
        assert size == pytest.approx(_old_tier_size(NAV, "high"))
        assert "tier_fallback" in note
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("TSLA" in m and "2026-09-02" in m for m in msgs), msgs

    def test_nan_vol_treated_as_missing(self, caplog):
        """(g) bool(nan) is True —— NaN 必须按缺失处理，输出里不得有 NaN。"""
        with caplog.at_level(logging.WARNING, logger=pp._log.name):
            size, note = pp._compute_position_size(NAV, "high", "X", [], as_of="2026-09-02",
                                                   vol_ann=float("nan"))
        assert math.isfinite(size)
        assert size == pytest.approx(_old_tier_size(NAV, "high"))
        assert "tier_fallback" in note
        assert "nan" not in note.lower()

    @pytest.mark.parametrize("bad", [0.0, -5.0, float("inf")])
    def test_nonpositive_or_inf_vol_treated_as_missing(self, bad):
        size, note = pp._compute_position_size(NAV, "high", "X", [], as_of="2026-09-02",
                                               vol_ann=bad)
        assert math.isfinite(size) and "tier_fallback" in note

    def test_open_position_records_sizing_note(self, monkeypatch):
        """_open_position 把 note 写进 Position.sizing 与 rationale。"""
        monkeypatch.setattr(pp, "_lookup_vol_ann",
                            lambda ticker, as_of, db_path=None: 35.0)
        snap = {"ticker": "TSLA", "direction": "bullish",
                "entry_price": 100.0, "composite_score": 7.8}
        pos = pp._open_position(snap, NAV, "2026-09-02", {}, [])
        assert pos is not None
        assert pos.sizing.startswith("vol_target(")
        assert pos.sizing in pos.rationale
        assert pos.size_usd == pytest.approx(NAV * 0.05)

    def test_open_position_passes_as_of_to_lookup(self, monkeypatch):
        seen = {}

        def _fake(ticker, as_of, db_path=None):
            seen["args"] = (ticker, as_of)
            return None
        monkeypatch.setattr(pp, "_lookup_vol_ann", _fake)
        snap = {"ticker": "NVDA", "direction": "bullish",
                "entry_price": 100.0, "composite_score": 7.8}
        pos = pp._open_position(snap, NAV, "2026-09-02", {}, [])
        assert seen["args"] == ("NVDA", "2026-09-02")
        assert pos is not None and pos.sizing == "tier_fallback(no_vol)"


# ══════════════════════════════════════════════════════════════════════════════
# 旧模式逐字节还原
# ══════════════════════════════════════════════════════════════════════════════

class TestTierModeReproducesOld:

    @pytest.mark.parametrize("conf", ["high", "mid", "low"])
    def test_tier_mode_exact(self, monkeypatch, conf):
        """(e) sizing_mode="tier" 与旧算法完全一致，且不碰 σ 查询。"""
        monkeypatch.setitem(pp.CONFIG, "sizing_mode", "tier")
        monkeypatch.setattr(pp, "_lookup_vol_ann",
                            lambda *a, **k: pytest.fail("tier 模式不得查波动率"))
        for lc in (False, True):
            size, note = pp._compute_position_size(NAV, conf, "X", [], low_conviction=lc,
                                                   as_of="2026-09-02", vol_ann=35.0)
            assert size == _old_tier_size(NAV, conf, lc)   # 精确相等，不用 approx
            assert note == "tier"

    def test_unknown_mode_raises(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "sizing_mode", "kelly")
        with pytest.raises(ValueError):
            pp._compute_position_size(NAV, "high", "X", [], vol_ann=35.0)


# ══════════════════════════════════════════════════════════════════════════════
# 向后兼容
# ══════════════════════════════════════════════════════════════════════════════

class TestBackwardCompat:

    def test_old_record_without_sizing_loads(self):
        """(h) 旧 positions.jsonl 没有 sizing 字段，Position(**p) 仍能加载。"""
        old = {"ticker": "TSLA", "direction": "bullish", "entry_date": "2026-08-31",
               "entry_price": 367.95, "sl_price": 342.19, "tp_price": 423.14,
               "shares": 6.7944, "size_usd": 2500.0, "time_stop_date": "2026-09-14",
               "confidence": "high", "score": 7.8, "rationale": "score=7.8 · high"}
        pos = pp.Position(**old)
        assert pos.sizing == ""
        assert pos.to_dict()["sizing"] == ""


# ══════════════════════════════════════════════════════════════════════════════
# 库查询：无前视、窗口、缺表
# ══════════════════════════════════════════════════════════════════════════════

def _build_db(path, rows):
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE signal_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ticker TEXT NOT NULL,
        signal TEXT NOT NULL, value REAL, UNIQUE(date, ticker, signal))""")
    con.executemany("INSERT INTO signal_archive(date, ticker, signal, value) VALUES (?,?,?,?)",
                    rows)
    con.commit()
    con.close()


class TestLookupVolAnn:

    def test_picks_latest_on_or_before_as_of(self, tmp_path):
        """(i) 取 ≤ as_of 窗口内最新一行；as_of 之后的行绝不可见（无前视）。"""
        db = tmp_path / "p.db"
        _build_db(db, [
            ("2026-08-28", "NVDA", "price.volatility_20d", 40.0),
            ("2026-08-31", "NVDA", "price.volatility_20d", 43.7),
            ("2026-09-03", "NVDA", "price.volatility_20d", 99.0),   # 未来行
            ("2026-08-31", "NVDA", "options.iv_current", 0.5),     # 别的信号
            ("2026-08-31", "TSLA", "price.volatility_20d", 70.0),  # 别的标的
        ])
        assert pp._lookup_vol_ann("NVDA", "2026-09-02", db) == pytest.approx(43.7)
        assert pp._lookup_vol_ann("NVDA", "2026-08-31", db) == pytest.approx(43.7)
        assert pp._lookup_vol_ann("NVDA", "2026-08-30", db) == pytest.approx(40.0)
        assert pp._lookup_vol_ann("TSLA", "2026-09-02", db) == pytest.approx(70.0)

    def test_outside_window_returns_none(self, tmp_path, monkeypatch):
        db = tmp_path / "p.db"
        _build_db(db, [("2026-08-20", "NVDA", "price.volatility_20d", 40.0)])
        max_age = int(pp.CONFIG["vol_target"]["vol_source_max_age_days"])
        # 恰在窗口边缘（含）
        assert pp._lookup_vol_ann("NVDA", f"2026-08-{20 + max_age:02d}", db) == pytest.approx(40.0)
        # 超出一天 → None
        assert pp._lookup_vol_ann("NVDA", f"2026-08-{21 + max_age:02d}", db) is None

    def test_nonfinite_or_nonpositive_rows_return_none(self, tmp_path):
        db = tmp_path / "p.db"
        _build_db(db, [
            ("2026-09-01", "A", "price.volatility_20d", 0.0),
            ("2026-09-01", "B", "price.volatility_20d", None),
            ("2026-09-01", "C", "price.volatility_20d", float("nan")),
        ])
        for t in ("A", "B", "C"):
            assert pp._lookup_vol_ann(t, "2026-09-02", db) is None

    def test_missing_table_returns_none(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        assert pp._lookup_vol_ann("NVDA", "2026-09-02", db) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert pp._lookup_vol_ann("NVDA", "2026-09-02", tmp_path / "nope.db") is None

    def test_locked_db_is_not_swallowed(self, tmp_path, monkeypatch):
        """`database is locked` 是瞬时可重试状态，不是「没数据」——必须往上抛。"""
        db = tmp_path / "p.db"
        _build_db(db, [("2026-09-01", "NVDA", "price.volatility_20d", 40.0)])

        class _Con:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

            def close(self):
                pass
        monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _Con())
        with pytest.raises(sqlite3.OperationalError):
            pp._lookup_vol_ann("NVDA", "2026-09-02", db)

    def test_memoized_per_ticker_date(self, tmp_path):
        """同 (ticker, as_of) 第二次不再查库：把库文件删掉后仍返回同值。"""
        db = tmp_path / "p.db"
        _build_db(db, [("2026-09-01", "NVDA", "price.volatility_20d", 40.0)])
        assert pp._lookup_vol_ann("NVDA", "2026-09-02", db) == pytest.approx(40.0)
        db.unlink()
        assert pp._lookup_vol_ann("NVDA", "2026-09-02", db) == pytest.approx(40.0)
        assert pp._lookup_vol_ann("NVDA", "2026-09-01", db) is None   # 不同 as_of 不命中


# ══════════════════════════════════════════════════════════════════════════════
# 卡片文案跟着模式走
# ══════════════════════════════════════════════════════════════════════════════

class TestCardRuleText:

    def test_vol_target_text(self):
        txt = pp._sizing_rule_text()
        assert "波动率目标" in txt and str(VT["target_position_vol_pct"]) in txt
        assert str(VT["size_pct_min"]) in txt and str(VT["size_pct_max"]) in txt

    def test_tier_text(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "sizing_mode", "tier")
        txt = pp._sizing_rule_text()
        assert "高置信 5.0% NAV" in txt and "波动率目标" not in txt


# ══════════════════════════════════════════════════════════════════════════════
# v0.45.104 二次复查修复
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoKeyIncludesMaxAge:
    """#2：`vol_source_max_age_days` 是查询条件的一部分，必须进 memo key。"""

    def test_widening_the_window_is_not_served_from_cache(self, tmp_path, monkeypatch):
        db = tmp_path / "p.db"
        _build_db(db, [("2026-08-29", "NVDA", "price.volatility_20d", 40.0)])
        as_of = "2026-09-02"          # 该行在 4 天前
        vt = pp.CONFIG["vol_target"]

        monkeypatch.setitem(vt, "vol_source_max_age_days", 1)
        assert pp._lookup_vol_ann("NVDA", as_of, db) is None       # 窗口太窄，本就该是 None

        monkeypatch.setitem(vt, "vol_source_max_age_days", 10)
        # 修复前：key 里没有 max_age → 命中上一次那个 None，40.0 永远拿不到
        assert pp._lookup_vol_ann("NVDA", as_of, db) == pytest.approx(40.0)

    def test_narrowing_the_window_is_not_served_from_cache(self, tmp_path, monkeypatch):
        """反方向同样成立——否则测试只钉住了一半。"""
        db = tmp_path / "p.db"
        _build_db(db, [("2026-08-29", "NVDA", "price.volatility_20d", 40.0)])
        as_of = "2026-09-02"
        vt = pp.CONFIG["vol_target"]

        monkeypatch.setitem(vt, "vol_source_max_age_days", 10)
        assert pp._lookup_vol_ann("NVDA", as_of, db) == pytest.approx(40.0)

        monkeypatch.setitem(vt, "vol_source_max_age_days", 1)
        assert pp._lookup_vol_ann("NVDA", as_of, db) is None

    def test_same_window_still_memoized(self, tmp_path):
        """收紧 key 不能顺手把缓存本身废掉（同桶必须仍然复用）。"""
        db = tmp_path / "p.db"
        _build_db(db, [("2026-09-01", "NVDA", "price.volatility_20d", 40.0)])
        assert pp._lookup_vol_ann("NVDA", "2026-09-02", db) == pytest.approx(40.0)
        db.unlink()
        assert pp._lookup_vol_ann("NVDA", "2026-09-02", db) == pytest.approx(40.0)

    def test_run_replay_isolates_the_vol_cache(self, tmp_path, monkeypatch):
        """沙盒进出各清一次：生产的 σ 不进沙盒，沙盒的 σ 不回生产。"""
        pp._VOL_ANN_CACHE[("PRE", "2026-09-02", "prod.db", 5)] = 99.0
        seen = {}

        def _fake_day(d, verbose=False):
            seen["at_entry"] = dict(pp._VOL_ANN_CACHE)
            pp._VOL_ANN_CACHE[("INSIDE", d, "sb.db", 5)] = 1.0

        monkeypatch.setattr(pp, "run_for_date", _fake_day)
        pp.run_replay({}, tmp_path / "sb", dates=["2026-09-01"])
        assert seen["at_entry"] == {}, "生产缓存漏进了沙盒"
        assert pp._VOL_ANN_CACHE == {}, "沙盒缓存漏回了生产"


class TestSizingModeValidatedAtEntry:
    """#10：模式名打错必须当天就炸，不能等到「恰好有仓要开」。"""

    def test_run_for_date_rejects_unknown_mode_before_any_position(self, monkeypatch):
        monkeypatch.setitem(pp.CONFIG, "sizing_mode", "vol_targt")   # 打错一个字母
        monkeypatch.setattr(pp, "_load_meta",
                            lambda: pytest.fail("校验必须发生在读状态之前"))
        with pytest.raises(ValueError) as e:
            pp.run_for_date("2026-09-02")
        assert "vol_targt" in str(e.value)

    def test_valid_modes_pass_the_entry_check(self, monkeypatch, tmp_path):
        """闸门不许顺手把合法模式也拦下——两个合法值都要放行。

        ⚠️ run_for_date 收尾用 `_write_jsonl` **整体重写** POSITIONS_FILE /
        EQUITY_FILE，不走 _append_jsonl。四个路径必须全部重绑到 tmp，
        否则这个测试会把生产 paper_portfolio_state/ 清空（实测踩过）。
        """
        for mode in pp._SIZING_MODES:
            sb = tmp_path / mode
            sb.mkdir()
            monkeypatch.setattr(pp, "POSITIONS_FILE", sb / "positions.jsonl")
            monkeypatch.setattr(pp, "CLOSED_FILE", sb / "closed_trades.jsonl")
            monkeypatch.setattr(pp, "EQUITY_FILE", sb / "equity_curve.jsonl")
            monkeypatch.setattr(pp, "META_FILE", sb / "meta.json")
            monkeypatch.setitem(pp.CONFIG, "sizing_mode", mode)
            monkeypatch.setattr(pp, "_load_meta", lambda: {"cash": 50_000.0})
            monkeypatch.setattr(pp, "_load_snapshots_for_date", lambda d: [])
            pp.run_for_date("2026-09-02")      # 不抛即通过
            assert (sb / "equity_curve.jsonl").exists()   # 确实跑完了整条路径


class TestClampWordingMatchesBehaviour:
    """#7：钳位作用在**置信乘数之前**的百分比上，文案不得声称 NAV 边界。"""

    def test_measured_mid_bounds_are_not_the_config_numbers(self):
        lo = pp._compute_position_size(NAV, "mid", "X", [], vol_ann=200.0)[0] / NAV * 100
        hi = pp._compute_position_size(NAV, "mid", "X", [], vol_ann=1.0)[0] / NAV * 100
        assert lo == pytest.approx(0.90)      # ≠ size_pct_min 1.5
        assert hi == pytest.approx(4.80)      # ≠ size_pct_max 8.0

    def test_rule_text_does_not_claim_an_unenforced_nav_range(self):
        txt = pp._sizing_rule_text()
        assert f'{VT["size_pct_min"]}–{VT["size_pct_max"]}% NAV' not in txt
        assert "置信乘数前钳位" in txt
        # 数值本身仍要露出（读者要能对上 CONFIG）
        assert str(VT["size_pct_min"]) in txt and str(VT["size_pct_max"]) in txt

    def test_high_tier_really_does_hit_the_clamp_bounds(self):
        """high 档（×1.0）确实等于钳位区间——文案改动不是把真话也删了。"""
        lo = pp._compute_position_size(NAV, "high", "X", [], vol_ann=200.0)[0] / NAV * 100
        hi = pp._compute_position_size(NAV, "high", "X", [], vol_ann=1.0)[0] / NAV * 100
        assert lo == pytest.approx(VT["size_pct_min"])
        assert hi == pytest.approx(VT["size_pct_max"])


class TestFallbackVisibleOnTheCard:
    """#8：Position.sizing 此前零读者，降级只对会开 jsonl 的人可见。"""

    def _card(self, monkeypatch, tmp_path, sizings):
        import json
        pos = [{"ticker": f"T{i}", "direction": "bullish", "entry_date": "2026-08-31",
                "entry_price": 100.0, "sl_price": 93.0, "tp_price": 115.0,
                "shares": 25.0, "size_usd": 2500.0, "time_stop_date": "2026-09-14",
                "confidence": "high", "score": 7.8, "rationale": "r", "sizing": sz}
               for i, sz in enumerate(sizings)]
        (tmp_path / "positions.jsonl").write_text(
            "".join(json.dumps(p) + "\n" for p in pos))
        (tmp_path / "closed_trades.jsonl").write_text("")
        (tmp_path / "equity_curve.jsonl").write_text(json.dumps(
            {"date": "2026-09-02", "nav": 50_000.0, "cash": 45_000.0,
             "deployed": 5_000.0, "unrealized": 0.0}) + "\n")
        monkeypatch.setattr(pp, "POSITIONS_FILE", tmp_path / "positions.jsonl")
        monkeypatch.setattr(pp, "CLOSED_FILE", tmp_path / "closed_trades.jsonl")
        monkeypatch.setattr(pp, "EQUITY_FILE", tmp_path / "equity_curve.jsonl")
        monkeypatch.setattr(pp, "_fetch_ohlc", lambda *a, **k: {})
        return pp.render_portfolio_card()

    def test_card_reports_how_many_positions_fell_back(self, monkeypatch, tmp_path):
        html = self._card(monkeypatch, tmp_path,
                          ["vol_target(σ=35.0%→5.0%)", "tier_fallback(no_vol)",
                           "tier_fallback(no_vol)"])
        assert "2/3 仓退回分档" in html

    def test_card_says_nothing_when_nothing_fell_back(self, monkeypatch, tmp_path):
        html = self._card(monkeypatch, tmp_path,
                          ["vol_target(σ=35.0%→5.0%)", "vol_target(σ=20.0%→8.0%)"])
        # 注意别撞上规则行里那句「无 σ 时退回分档」——那是常驻文案
        assert "仓退回分档" not in html


class TestProductionStateIsIsolated:
    """conftest 的 `_isolate_paper_portfolio_state` 两道防线各配一个自检。

    事故：`test_valid_modes_pass_the_entry_check` 曾只打桩 _load_meta /
    _append_jsonl / _save_meta，漏了路径本身，run_for_date 收尾的
    `_write_jsonl(EQUITY_FILE/POSITIONS_FILE, ...)` 直接重写了生产状态
    （equity 93 行→1、positions 13 行→0）。
    """

    def test_state_paths_point_outside_the_repo(self):
        """防线①：四个全局在测试期间必须已被重绑到 tmp。"""
        for f in (pp.POSITIONS_FILE, pp.CLOSED_FILE, pp.EQUITY_FILE, pp.META_FILE):
            assert pp.BASE_DIR not in f.parents, f"{f} 仍指向仓库内"
        assert pp.BASE_DIR not in pp.STATE_DIR.parents

    def test_digest_helper_actually_detects_changes(self, tmp_path):
        """防线②：指纹函数得真能看出改动，否则 teardown 的断言恒真。"""
        # conftest 在 pytest 的 importlib 模式下不是可直接 import 的模块名，
        # 按路径载入拿到同一个函数（conftest 顶层只有定义，重复执行无副作用）。
        import importlib.util
        import pathlib
        _spec = importlib.util.spec_from_file_location(
            "_pp_conftest_probe", pathlib.Path(__file__).parent / "conftest.py")
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _pp_state_digest = _mod._pp_state_digest
        f = tmp_path / "x.jsonl"
        f.write_text("a\n")
        d1 = _pp_state_digest(f)
        f.write_text("b\n")
        assert _pp_state_digest(f) != d1          # 内容改了要看得见
        f.unlink()
        assert _pp_state_digest(f) == "MISSING" != d1   # 删掉也要看得见
