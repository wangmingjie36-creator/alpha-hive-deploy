"""Zero Gamma 日间拆解（v0.45.816，`alphabot/zg_decompose.py` + `AlphaBotService.zg_decompose`）。

守住的几件事：
  · 两端（全取昨天 / 全取今天）就是页面上那条 Zero Gamma（与 `level_map` 逐视图一致）——拆的是同一个量；
  · 只改一项（OI / IV / 合约集合）时，结论指向那一项；两天一样 ⇒「基本没动」；
  · 两个方向指向不同项 ⇒「交互」，不硬给主因；
  · 缺存档 / 只有一份 / 读坏 / 算不出各报各的原因，不装成「没动」；算不出要计数 + 日志；
  · 结果按两份文件的修改时刻缓存，存档被覆盖就重算。
"""
from __future__ import annotations

import copy
import logging
from datetime import date, timedelta

import pytest

import sell_strike_levels as L
from alphabot import service as SV
from alphabot import synthetic as syn
from alphabot import zg_decompose as ZD
from alphabot.service import AlphaBotService

INJ = dict(view_fn=L.view_contracts, sweep_fn=L.zero_gamma_sweep, profile_fn=L.strike_profile)


def _chain():
    raw, _ = syn.demo_fetch("NVDA")
    return copy.deepcopy(raw)


def _scale(raw, field, k, pred=lambda c: True):
    for c in raw["contracts"]:
        if pred(c) and c.get(field):
            c[field] *= k
    return raw


def _near_puts(raw):
    S = raw["underlying_price"]
    return lambda c: c["cp"] == "P" and abs(c["strike"] / S - 1) <= 0.08


class TestEndpoints:
    def test_both_ends_are_the_page_zero_gamma(self):
        cur = _chain()
        prev = _scale(_chain(), "oi", 3.0, _near_puts(cur))
        res = ZD.build(prev, cur, **INJ)
        for raw, end in ((prev, "prev"), (cur, "cur")):
            lm = L.level_map(raw["contracts"], raw["underlying_price"])["views"]
            for v in ZD.VIEWS:
                got, want = res["views"][v][end], lm[v]["zero_gamma"]
                assert got["curve_state"] == want["curve_state"], (v, end)
                assert got["nearest"] == want["nearest"], (v, end)
                assert got["sign_at_spot"] == want["sign_at_spot"], (v, end)


class TestAttribution:
    def test_oi_only_change_points_at_oi(self):
        cur = _chain()
        prev = _scale(_chain(), "oi", 4.0, _near_puts(cur))
        v = ZD.build(prev, cur, **INJ)["views"]["le_45dte"]
        assert v["total_dist_pct"] >= ZD.RULES["flat_pct"]
        assert v["verdict"]["factor"] == "oi" and v["verdict"]["verdict"] in ("dominant", "leading")
        assert "OI" in v["headline"]
        # 其它单项几乎不动（只有 OI 不同）
        for f in ("iv", "t", "spot", "set"):
            assert v["forward"][f]["gain_pct"] == pytest.approx(0.0, abs=1e-9), f

    def test_iv_only_change_points_at_iv(self):
        cur = _chain()
        prev = _scale(_chain(), "iv", 0.5)
        v = ZD.build(prev, cur, **INJ)["views"]["le_45dte"]
        assert v["verdict"]["factor"] == "iv", v["headline"]

    def test_identical_chains_are_flat_and_not_highlighted(self):
        res = ZD.build(_chain(), _chain(), **INJ)
        for v in res["views"].values():
            assert v["verdict"]["verdict"] == "flat" and v["highlight"] is False
            assert v["matching"]["n_only_prev"] == v["matching"]["n_only_cur"] == 0
        assert res["highlight"] is False

    def test_expiry_roll_is_the_contract_set(self):
        """昨天还有一个今天已到期的近月：下一到期视图比的是两张合约 ⇒ 合约集合主导、标出 expiry_rolled。"""
        cur = _chain()
        prev = _chain()
        S = prev["underlying_price"]
        # 合成链按真实今天生成到期日：近月从链里取，「已到期近月」取它前一天——不写死日期（时间炸弹，v0.45.822）
        near = min(c["expiry"] for c in prev["contracts"])
        gone = (date.fromisoformat(near) - timedelta(days=1)).isoformat()
        tag = lambda iso: iso[2:4] + iso[5:7] + iso[8:10]  # noqa: E731  symbol 里的 YYMMDD
        extra = []
        for c in prev["contracts"]:
            if c["expiry"] == near:
                assert tag(near) in c["symbol"], c["symbol"]
                e = dict(c, expiry=gone, dte=1, symbol=c["symbol"].replace(tag(near), tag(gone)))
                if e["cp"] == "P" and abs(e["strike"] / S - 1) <= 0.06:
                    e["oi"] = 50000.0
                extra.append(e)
        prev["contracts"] = extra + prev["contracts"]
        v = ZD.build(prev, cur, **INJ)["views"]["next_expiry"]
        assert v["flags"]["expiry_rolled"] is True
        assert v["matching"]["n_only_prev"] > 0 and v["matching"]["n_common"] == 0
        assert v["verdict"]["factor"] == "set", v["headline"]

    def test_state_change_is_highlighted_and_described(self):
        """昨天全是 call OI（全段为正）、今天正常过零 ⇒ 状态变了要高亮，文字写出「全段为正」。"""
        cur = _chain()
        prev = _chain()
        for c in prev["contracts"]:
            if c["cp"] == "P":
                c["oi"] = 0.0
        v = ZD.build(prev, cur, **INJ)["views"]["le_45dte"]
        assert v["prev"]["curve_state"] == "all_positive" and v["cur"]["curve_state"] == "crosses"
        assert v["flags"]["state_changed"] is True and v["highlight"] is True
        assert v["total_dist_pct"] == ZD.STATE_GAP
        assert "全段为正" in v["headline"]
        assert v["verdict"]["factor"] == "oi"

    def test_strike_oi_signs(self):
        """昨天 put OI 更多 ⇒ 今天 put OI 变化为负、净 GEX 变化为正（put 记负号）。"""
        cur = _chain()
        prev = _scale(_chain(), "oi", 3.0, _near_puts(cur))
        rows = ZD.build(prev, cur, **INJ)["views"]["le_45dte"]["strike_oi"]
        assert 0 < len(rows) <= ZD.RULES["top_strikes"]
        for r in rows:
            assert r["delta_put_oi"] < 0 and r["delta_call_oi"] == 0
            assert r["delta_net_gex_usd_per_1pct"] > 0
        mags = [abs(r["delta_net_gex_usd_per_1pct"]) for r in rows]
        assert mags == sorted(mags, reverse=True)

    def test_interaction_when_directions_disagree(self):
        fwd = dict.fromkeys(ZD.FACTORS, 0.0)
        bwd = dict.fromkeys(ZD.FACTORS, 0.0)
        fwd["oi"], bwd["iv"] = 3.0, 3.0
        v = ZD._verdict(4.0, fwd, bwd, ZD.RULES)
        assert v["verdict"] == "interaction" and v["factor"] is None
        assert (v["top_forward"], v["top_backward"]) == ("oi", "iv")

    def test_leading_when_one_direction_falls_short(self):
        fwd = dict.fromkeys(ZD.FACTORS, 0.0)
        bwd = dict.fromkeys(ZD.FACTORS, 0.0)
        fwd["oi"], bwd["oi"] = 3.0, 1.0
        assert ZD._verdict(4.0, fwd, bwd, ZD.RULES) == {"verdict": "leading", "factor": "oi"}


class TestHybrid:
    def test_new_contract_keeps_its_own_values_and_mixed_rows_drop_cboe_gamma(self):
        prev = ZD._Day({"underlying_price": 100, "contracts": [
            {"symbol": "A", "strike": 100, "cp": "C", "oi": 1.0, "iv": 0.5, "dte": 3, "gamma": 0.1}]})
        cur = ZD._Day({"underlying_price": 100, "contracts": [
            {"symbol": "A", "strike": 100, "cp": "C", "oi": 2.0, "iv": 0.6, "dte": 2, "gamma": 0.2},
            {"symbol": "B", "strike": 105, "cp": "P", "oi": 9.0, "iv": 0.7, "dte": 2, "gamma": 0.3}]})
        days = {"prev": prev, "cur": cur}
        keys = {"prev": [("sym", "A")], "cur": [("sym", "A"), ("sym", "B")]}
        choice = {"set": "cur", "oi": "prev", "iv": "cur", "t": "cur", "spot": "cur"}
        rows, S = ZD._hybrid(days, keys, choice)
        a, b = rows
        assert a["oi"] == 1.0 and a["iv"] == 0.6 and a["dte"] == 2 and "gamma" not in a
        assert b["oi"] == 9.0, "只在今天的新合约没有昨天的 OI：用它自己的（影响归合约集合）"
        rows, _ = ZD._hybrid(days, keys, dict.fromkeys(ZD.FACTORS, "cur"))
        assert rows[0]["gamma"] == 0.2, "不混天时保留 CBOE gamma"

    def test_missing_spot_is_unavailable(self):
        cur = _chain()
        prev = _chain()
        prev["underlying_price"] = None
        res = ZD.build(prev, cur, **INJ)
        assert res["available"] is False and res["reason"] == "underlying_price_unavailable"


# ───────────────────────────── service
def _svc(tmp_path):
    return AlphaBotService(fetch_fn=syn.demo_fetch, state_dir=tmp_path / "st")


def _put(svc, day, raw, t="NVDA"):
    SV._atomic_write_gzip_json(svc._chain_archive_path(t, day), raw)


class TestService:
    def test_no_archive_and_single_archive(self, tmp_path):
        svc = _svc(tmp_path)
        assert svc.zg_decompose("NVDA")["reason"] == "no_current_archive"
        _put(svc, "2026-11-24", _chain())
        r = svc.zg_decompose("NVDA")
        assert r["available"] is False and r["reason"] == "no_previous_archive" and r["date"] == "2026-11-24"

    def test_pairs_latest_with_previous_archive_and_counts_trading_days(self, tmp_path):
        svc = _svc(tmp_path)
        cur = _chain()
        _put(svc, "2026-11-20", _chain())
        _put(svc, "2026-11-24", _scale(_chain(), "oi", 4.0, _near_puts(cur)))
        _put(svc, "2026-11-25", cur)
        r = svc.zg_decompose("NVDA")
        assert r["available"] is True and (r["prev_date"], r["date"]) == ("2026-11-24", "2026-11-25")
        assert r["trading_days_apart"] == 1
        assert r["views"]["le_45dte"]["verdict"]["factor"] == "oi"
        older = svc.zg_decompose("NVDA", "2026-11-24")
        assert (older["prev_date"], older["date"]) == ("2026-11-20", "2026-11-24")
        assert older["trading_days_apart"] == 2           # 11-23 周一、11-24 周二
        assert r["archive_dates"][:3] == ["2026-11-25", "2026-11-24", "2026-11-20"]
        assert r["rules"]["rules"] == ZD.RULES

    def test_corrupt_previous_archive_is_reported_not_flat(self, tmp_path):
        svc = _svc(tmp_path)
        _put(svc, "2026-11-25", _chain())
        p = svc._chain_archive_path("NVDA", "2026-11-24")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"not gzip")
        r = svc.zg_decompose("NVDA")
        assert r["available"] is False and r["reason"] == "archive_corrupt"
        assert r["bad_date"] == "2026-11-24" and r["error"]
        assert "views" not in r

    def test_exception_is_visible(self, tmp_path, monkeypatch, caplog):
        svc = _svc(tmp_path)
        _put(svc, "2026-11-24", _chain())
        _put(svc, "2026-11-25", _chain())

        def boom(*a, **k):
            raise RuntimeError("kaput")
        monkeypatch.setattr(ZD, "build", boom)
        with caplog.at_level(logging.WARNING):
            r = svc.zg_decompose("NVDA")
        assert r["available"] is False and r["reason"].startswith("exception:RuntimeError")
        assert svc.stats["zg_decompose_failures"] == 1
        assert any("Zero Gamma 拆解算不出" in x.getMessage() for x in caplog.records)

    def test_cache_until_archive_is_overwritten(self, tmp_path, monkeypatch):
        svc = _svc(tmp_path)
        _put(svc, "2026-11-24", _chain())
        _put(svc, "2026-11-25", _chain())
        calls = {"n": 0}
        real = ZD.build

        def counting(*a, **k):
            calls["n"] += 1
            return real(*a, **k)
        monkeypatch.setattr(ZD, "build", counting)
        svc.zg_decompose("NVDA")
        svc.zg_decompose("NVDA")
        assert calls["n"] == 1
        cur = _scale(_chain(), "oi", 2.0)
        p = svc._chain_archive_path("NVDA", "2026-11-25")
        st = p.stat()
        _put(svc, "2026-11-25", cur)
        import os
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        svc.zg_decompose("NVDA")
        assert calls["n"] == 2

    def test_cached_result_still_rejudges_close_and_archive_list(self, tmp_path, monkeypatch):
        """二次检查补：缓存只存由两份文件决定的部分。盘中算过、收盘后再取 ⇒ cur_final 要变 True；
        之后新多出一份更早的存档 ⇒ archive_dates 要跟着变（命中缓存也一样）。"""
        svc = _svc(tmp_path)
        today = SV.now_et().date().isoformat()
        _put(svc, "2026-11-24", _chain())
        _put(svc, today, _chain())
        monkeypatch.setattr(SV, "market_session", lambda now=None: {"live": True})
        assert svc.zg_decompose("NVDA")["cur_final"] is False
        monkeypatch.setattr(SV, "market_session", lambda now=None: {"live": False})
        assert svc.zg_decompose("NVDA")["cur_final"] is True
        _put(svc, "2026-11-20", _chain())
        assert "2026-11-20" in svc.zg_decompose("NVDA")["archive_dates"]

    def test_source_change_is_flagged(self, tmp_path):
        """二次检查补：昨天 CBOE、今天 yfinance 备用链（817 起收盘后陈旧的票会换源）⇒ 标 source_changed。"""
        svc = _svc(tmp_path)
        _put(svc, "2026-11-24", _chain())
        yf = _chain()
        yf["data_source"] = "yfinance"
        _put(svc, "2026-11-25", yf)
        r = svc.zg_decompose("NVDA")
        assert (r["source_prev"], r["source_cur"], r["source_changed"]) == ("cboe", "yfinance", True)
        _put(svc, "2026-11-25", _chain())
        import os
        p = svc._chain_archive_path("NVDA", "2026-11-25")
        os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10**9))
        assert svc.zg_decompose("NVDA")["source_changed"] is False

    def test_bad_date_rejected(self, tmp_path):
        with pytest.raises(SV.BadRequest):
            _svc(tmp_path).zg_decompose("NVDA", "../x")

    def test_rules_reach_method_page_and_route_works(self, tmp_path):
        svc = _svc(tmp_path)
        assert svc.meta_texts()["zg_decompose"]["rules"] == ZD.RULES
        _put(svc, "2026-11-24", _chain())
        _put(svc, "2026-11-25", _chain())
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        c = TestClient(create_app(svc, port=8765), base_url="http://127.0.0.1:8765")
        r = c.get("/api/zg-decompose/NVDA")
        assert r.status_code == 200 and r.json()["available"] is True
        assert r.json()["views"]["le_45dte"]["verdict"]["verdict"] == "flat"
