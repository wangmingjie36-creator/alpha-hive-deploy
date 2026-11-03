"""Alpha Bot 盘中成交活跃度（v0.45.698）的守卫。

每组回答一句「谁会红」：
  · 口径：call 与 put 的增量不互相抵掉；现价移动但没新成交 ⇒ 0（不是旧成交量乘新 gamma 的差）；
    先逐合约减再乘这一拍的 gamma（`TestBuildView`）；
  · 算不出就留空、不当 0，并说清楚为什么（`TestBuildView`、`TestCompareState`、`TestSnapshotFlow`）；
  · gamma 取数与 `strike_profile` 同一条规则（`TestGammaRule`）；
  · 快照：用的是**同一份**原始链；上一拍只取落了盘的那一拍；重启 / 跨日 / 换源 / 出错各有原因、出错不挡快照（`TestSnapshotFlow`）；
  · 热力图：读快照里的活跃度、不分正负、只有「当前值」、旧快照写明没有这项（`TestHeat`）；
  · 纯度与页面（`TestPurityAndPage`）。
全部离线。
"""
from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alphabot import intraday_activity as IA
from alphabot import intraday_heat as IH
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService, BadRequest, activity_gamma
import sell_strike_levels as L

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parent.parent
D = "2026-09-30"
W = 100.0 * 0.01                       # 合约乘数 × 1%


def _c(sym, k, cp, vol, gamma=0.02, **kw):
    return {"symbol": sym, "strike": k, "cp": cp, "expiry": "2026-10-16", "dte": 16, "volume": vol,
            "gamma": gamma, "iv": 0.3, **kw}


def _g(c, S):
    return c.get("gamma")


class TestBuildView:
    def test_call_and_put_add_not_cancel(self):
        prev = {"C1": 100.0, "P1": 100.0}
        cur = [_c("C1", 100, "C", 150.0), _c("P1", 100, "P", 150.0)]
        out = IA.build_view(prev, cur, 100.0, gamma_fn=_g, band=0.12)
        k, act, dv, miss = out["strikes"][0]
        assert (k, dv, miss) == (100.0, 100.0, 0)
        assert act == pytest.approx(100.0 * 0.02 * W * 100.0 ** 2)      # 50 + 50 张，正值相加
        assert out["totals"]["volume_added"] == 100.0

    def test_price_move_without_new_volume_is_zero(self):
        """旧口径（累计成交量 × 当前 gamma 相减）这里会有差值；新口径只看新增成交 ⇒ 0。"""
        prev = {"C1": 500.0}
        out = IA.build_view(prev, [_c("C1", 100, "C", 500.0, gamma=0.09)], 104.0, gamma_fn=_g, band=0.12)
        assert out["strikes"][0][1] == 0.0 and out["counts"]["n_used"] == 1

    def test_weights_only_the_increment_by_current_gamma(self):
        prev = {"C1": 1000.0}
        out = IA.build_view(prev, [_c("C1", 100, "C", 1010.0, gamma=0.05)], 100.0, gamma_fn=_g, band=0.12)
        assert out["strikes"][0][1] == pytest.approx(10 * 0.05 * W * 100.0 ** 2)

    def test_zero_increment_needs_no_gamma(self):
        out = IA.build_view({"C1": 5.0}, [_c("C1", 100, "C", 5.0, gamma=None)], 100.0, gamma_fn=_g, band=0.12)
        assert out["strikes"][0][1] == 0.0 and out["counts"]["n_no_gamma"] == 0

    @pytest.mark.parametrize("prev,c,count", [
        ({}, _c("C1", 100, "C", 5.0), "n_no_previous"),
        ({"C1": 9.0}, _c("C1", 100, "C", 5.0), "n_volume_decreased"),
        ({"C1": 1.0}, _c("C1", 100, "C", None), "n_no_volume"),
        ({"C1": 1.0}, _c("C1", 100, "C", 5.0, gamma=None), "n_no_gamma"),
        ({"C1": 1.0}, _c("C1", 100, "C", 5.0, gamma=-0.1), "n_no_gamma"),
    ])
    def test_excluded_contracts_leave_cell_empty_not_zero(self, prev, c, count):
        out = IA.build_view(prev, [c], 100.0, gamma_fn=_g, band=0.12)
        assert out["counts"][count] == 1
        assert out["strikes"] == [[100.0, None, None, 1]]
        assert out["totals"]["activity_usd_per_1pct"] is None

    def test_one_good_contract_makes_the_cell_real(self):
        out = IA.build_view({"C1": 1.0}, [_c("C1", 100, "C", 3.0), _c("P1", 100, "P", 3.0)], 100.0,
                            gamma_fn=_g, band=0.12)
        assert out["strikes"][0][2] == 2.0 and out["strikes"][0][3] == 1

    def test_duplicate_identity_is_not_guessed(self):
        out = IA.build_view({"C1": 1.0}, [_c("C1", 100, "C", 3.0), _c("C1", 100, "C", 9.0)], 100.0,
                            gamma_fn=_g, band=0.12)
        assert out["counts"]["n_duplicate_key"] == 2 and out["strikes"][0][1] is None
        assert "C1" not in IA.volume_map([_c("C1", 100, "C", 3.0), _c("C1", 100, "C", 9.0)])

    def test_spx_and_spxw_same_strike_are_two_contracts(self):
        a = _c("SPX   261016C05000000", 5000, "C", 10.0)
        b = _c("SPXW  261016C05000000", 5000, "C", 30.0)
        assert IA.contract_key(a) != IA.contract_key(b)
        assert IA.volume_map([a, b]) == {IA.contract_key(a): 10.0, IA.contract_key(b): 30.0}

    def test_band_and_fallback_key(self):
        c = {"strike": 130.0, "cp": "P", "expiry": "2026-10-16", "dte": 16, "volume": 3.0, "gamma": 0.01}
        assert IA.contract_key(c) == "2026-10-16|130|P"
        assert IA.build_view({}, [c], 100.0, gamma_fn=_g, band=0.12)["strikes"] == []
        assert IA.contract_key({"strike": 1.0, "cp": "X", "expiry": "e"}) is None

    def test_unknown_reason_raises(self):
        with pytest.raises(ValueError):
            IA.unavailable("made_up")


class TestCompareState:
    P = {"date": D, "source": "cboe", "ts": "t1", "vols": {}}

    @pytest.mark.parametrize("prev,kw,why", [
        (None, {}, "no_previous_in_process"),
        ({**P, "date": "2026-09-29"}, {}, "previous_other_day"),
        (P, {"last_ts": "t0"}, "previous_not_last_snapshot"),
        (P, {"source": "yfinance"}, "source_changed"),
        ({**P, "vols": None}, {}, "previous_not_computed"),
        (P, {}, None),
    ])
    def test_reasons(self, prev, kw, why):
        args = {"date_et": D, "source": "cboe", "last_ts": "t1", **kw}
        assert IA.compare_state(prev, **args) == why
        assert why is None or why in IA.REASONS


class TestGammaRule:
    @pytest.mark.parametrize("drop_cboe_gamma", [False, True])
    def test_same_rule_as_strike_profile(self, drop_cboe_gamma):
        """用 activity_gamma 按 OI 重建逐行权价 call / put GEX，必须与 strike_profile 逐行相等（CBOE gamma 路径与 BS 兜底路径都核）。"""
        raw = syn.demo_payload("NVDA", D)
        S = raw["underlying_price"]
        cs = [c for c in raw["contracts"] if c["dte"] <= 45]
        # 合成链的 gamma 本来就是 BS 算的：CBOE 路径要换成与 BS 不同的值，否则「CBOE 优先」删了也核不出来
        cs = [{**c, "gamma": None if drop_cboe_gamma else c["gamma"] * 1.3} for c in cs]
        prof = {r["strike"]: r for r in L.strike_profile(cs, S)["rows"]}
        mine = {}
        for c in cs:
            g = activity_gamma(c, S)
            if g is None or not c["oi"] or c["oi"] <= 0:
                continue
            row = mine.setdefault(c["strike"], [0.0, 0.0])
            row[0 if c["cp"] == "C" else 1] += g * c["oi"] * 100.0 * S * S * 0.01
        assert prof and set(mine) == set(prof)
        for k, (cg, pg) in mine.items():
            assert cg == pytest.approx(prof[k]["call_gex_usd_per_1pct"], rel=1e-9, abs=1e-6)
            assert -pg == pytest.approx(prof[k]["put_gex_usd_per_1pct"], rel=1e-9, abs=1e-6)


class _Feed:
    """每次取链：现价与成交量都带上「第几次取」，好核对快照用的是不是同一份链。"""

    def __init__(self):
        self.n = 0
        self.vol_step = 10.0
        self.source = None
        self.base = syn.demo_payload("NVDA", D)

    def __call__(self, ticker, *, as_of=None):
        self.n += 1
        raw = json.loads(json.dumps(self.base))
        raw["payload_last_trade_time"] = f"{D}T10:{self.n:02d}:00"
        raw["session_live"] = True
        raw["underlying_price"] = round(self.base["underlying_price"] + 0.01 * self.n, 2)
        for c in raw["contracts"]:
            c["volume"] = c["volume"] + self.vol_step * self.n
        if self.source:
            raw["data_source"] = self.source
        return raw, None


def _svc(tmp_path, feed):
    return AlphaBotService(fetch_fn=feed, state_dir=tmp_path / "st")


def _snaps(svc, d=D):
    return svc.intraday("NVDA", d)["snapshots"]


def _act(snap, view="le_45dte"):
    return snap["views"][view]["activity"]


class TestSnapshotFlow:
    def test_first_then_available_with_exact_increment(self, tmp_path):
        feed = _Feed()
        svc = _svc(tmp_path, feed)
        assert svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 1, tzinfo=ET))["recorded"]
        assert svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 2, tzinfo=ET))["recorded"]
        s1, s2 = _snaps(svc)
        assert _act(s1) == {"available": False, "reason": "no_previous_in_process", "since_ts": None}
        a = _act(s2)
        assert a["available"] and a["since_ts"] == s1["ts"]
        # 每张合约增量 = vol_step（两次取之间加了一个 vol_step）⇒ 每个行权价的张数 = 10 × 进了几张
        assert a["totals"]["volume_added"] == 10.0 * a["counts"]["n_used"] and a["counts"]["n_used"] > 20
        assert all(miss == 0 and dv > 0 for _k, _w, dv, miss in a["strikes"])
        assert a["counts"]["n_no_previous"] == 0 and a["counts"]["n_volume_decreased"] == 0
        assert _act(s2, "next_expiry")["available"]

    def test_uses_the_same_chain_as_the_snapshot(self, tmp_path):
        """快照的现价出自第 n 次取链 ⇒ 活跃度也必须用第 n 次的成交量（不是之后别的现算换进来的链）。"""
        feed = _Feed()
        svc = _svc(tmp_path, feed)
        svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 1, tzinfo=ET))
        svc.live("NVDA", force=True)                        # 页面在两拍之间又现算了一次（第 2 次取）
        svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 3, tzinfo=ET))   # 第 3 次取
        s1, s2 = _snaps(svc)
        assert s2["underlying_price"] == round(feed.base["underlying_price"] + 0.03, 2)
        a = _act(s2)
        assert a["totals"]["volume_added"] == 20.0 * a["counts"]["n_used"]    # 第 1 次 → 第 3 次：每张 +20，不是 +10

    def test_unchanged_payload_does_not_move_previous(self, tmp_path):
        feed = _Feed()
        svc = _svc(tmp_path, feed)
        now = datetime(2026, 9, 30, 10, 1, tzinfo=ET)
        svc.take_snapshot("NVDA", now=now)
        prev = dict(svc._act_prev["NVDA"])
        feed.n -= 1                                          # 下一次取到的和上一份一样
        assert svc.take_snapshot("NVDA", now=now)["reason"] == "payload_unchanged"
        assert svc._act_prev["NVDA"]["ts"] == prev["ts"]

    def test_restart_other_day_and_source_change(self, tmp_path):
        feed = _Feed()
        svc = _svc(tmp_path, feed)
        svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 1, tzinfo=ET))
        svc2 = _svc(tmp_path, feed)                          # 重启：内存里的上一拍没了
        svc2.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 2, tzinfo=ET))
        assert _act(_snaps(svc2)[-1])["reason"] == "no_previous_in_process"
        svc2.take_snapshot("NVDA", now=datetime(2026, 10, 1, 9, 31, tzinfo=ET))
        assert _act(_snaps(svc2, "2026-10-01")[-1])["reason"] == "previous_other_day"
        feed.source = "yfinance"
        svc2.take_snapshot("NVDA", now=datetime(2026, 10, 1, 9, 32, tzinfo=ET))
        assert _act(_snaps(svc2, "2026-10-01")[-1])["reason"] == "source_changed"

    def test_previous_must_be_last_line_in_file(self, tmp_path):
        feed = _Feed()
        svc = _svc(tmp_path, feed)
        svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 1, tzinfo=ET))
        svc._act_prev["NVDA"]["ts"] = "2026-09-30T09:59:00-04:00"
        svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 2, tzinfo=ET))
        assert _act(_snaps(svc)[-1])["reason"] == "previous_not_last_snapshot"

    def test_failure_does_not_block_snapshot_and_is_visible(self, tmp_path, monkeypatch):
        feed = _Feed()
        svc = _svc(tmp_path, feed)
        svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 1, tzinfo=ET))

        def boom(*a, **k):
            raise RuntimeError("坏了")
        monkeypatch.setattr(IA, "build_view", boom)
        assert svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 2, tzinfo=ET))["recorded"]
        assert _act(_snaps(svc)[-1])["reason"] == "exception" and svc.stats["activity_failures"] == 1
        monkeypatch.undo()
        svc.take_snapshot("NVDA", now=datetime(2026, 9, 30, 10, 3, tzinfo=ET))
        assert _act(_snaps(svc)[-1])["reason"] == "previous_not_computed"


class TestHeat:
    def _snap(self, i, act):
        v = {"strikes": [[100.0, 5.0, 7.0]], "zero_gamma": {"nearest": 99.0}}
        if act is not None:
            v["activity"] = act
        return {"ts": f"{D}T10:{i:02d}:00-04:00", "underlying_price": 100.0, "views": {"le_45dte": v}}

    def test_reads_activity_unsigned_and_records_missing(self):
        good = {"available": True, "strikes": [[100.0, 300.0, 4.0, 0], [101.0, None, None, 2]],
                "counts": {"n_used": 3, "n_no_previous": 2}}
        snaps = [self._snap(1, None), self._snap(2, IA.unavailable("no_previous_in_process")), self._snap(3, good)]
        hm = IH.build(snaps, basis="activity")
        assert hm["available"] and hm["signed"] is False
        assert [c[:3] for c in hm["cells"]] == [[2, 100.0, 300.0]]
        assert hm["activity_missing"] == [{"index": 0, "reason": "not_in_snapshot"},
                                          {"index": 1, "reason": "no_previous_in_process"}]
        assert hm["activity_counts"] == {"n_used": 3, "n_no_previous": 2}
        assert [c[:3] for c in IH.build(snaps, basis="activity_volume")["cells"]] == [[2, 100.0, 4.0]]
        assert IH.build(snaps, basis="volume")["signed"] is True

    def test_level_only_and_all_missing(self):
        snaps = [self._snap(1, IA.unavailable("first_of_day")), self._snap(2, None)]
        assert IH.build(snaps, basis="activity", mode="delta")["reason"] == "activity_level_only"
        assert IH.build(snaps, basis="activity")["reason"] == "no_activity"

    def test_endpoint_labels_every_reason(self, tmp_path):
        svc = AlphaBotService(demo=True, fetch_fn=syn.demo_fetch, state_dir=tmp_path / "d")
        hm = svc.intraday_heat("NVDA", D, basis="activity")
        assert hm["available"] and hm["activity_missing"] == [{"index": 0, "reason": "first_of_day"}]
        assert set(IA.REASONS) | set(IH.ACTIVITY_MISSING) <= set(hm["activity_reason_labels"])
        with pytest.raises(BadRequest):
            svc.intraday_heat("NVDA", D, basis="nope")


class TestPurityAndPage:
    SRC = (REPO / "alphabot" / "static" / "pages" / "intraday.js").read_text(encoding="utf-8")

    def test_module_imports_nothing_from_sell_modules(self):
        tree = ast.parse((REPO / "alphabot" / "intraday_activity.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert names <= {"__future__", "math", "typing"}

    def test_page_offers_both_activity_bases_and_says_what_it_is_not(self):
        assert '["activity", "成交活跃度"]' in self.SRC and '["activity_volume", "活跃度（张）"]' in self.SRC
        assert "不是 GEXBot 的 State / Orderflow" in self.SRC and "未经前向检验" in self.SRC
        assert "hm.activity_reason_labels" in self.SRC

    def test_chart_uses_single_hue_for_unsigned(self):
        js = (REPO / "alphabot" / "static" / "charts.js").read_text(encoding="utf-8")
        assert "unsigned ? 0 : -clip" in js and "unsigned ? [c.sunk, c.posSoft, c.pos]" in js
