"""Alpha Bot 指数期权（SPX / XSP，v0.45.689）的守卫。

每组回答一句「谁会红」：
  · 名字：SPX / ^SPX / $SPX 都落到 CBOE 的 `_SPX`（裸 `SPX` 在 CBOE 是 403，不映射就只会报「取不到」）（`TestNames`）；
  · 上午结算：SPX 月度根在到期日（dte == 0）开盘已结算，不能当成当天的 gamma；SPXW / 别的到期日 / XSP 不动（`TestAmSettled`）；
  · 接线：现算经过滤、计数上页面，「怎么卖」带指数说明，股票票不受影响（`TestLiveWiring`）；
  · 范围：指数只在 Alpha Bot——不进 WATCHLIST、不进卖权账本，名单常量在前端不另抄（`TestScope`）。
全部离线（合成链注入 `fetch_fn`）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from alphabot import synthetic as syn
from alphabot.service import (AlphaBotService, BadRequest, INDEX_TICKERS, drop_am_settled_expiring,
                              norm_ticker)

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "alphabot" / "static"


def _c(symbol, dte, **kw):
    return {"symbol": symbol, "dte": dte, "strike": 7700.0, "cp": "C", "expiry": "2026-11-20", "oi": 10.0, **kw}


class TestNames:
    @pytest.mark.parametrize("given,want", [("SPX", "_SPX"), ("spx", "_SPX"), ("^SPX", "_SPX"), ("$SPX", "_SPX"),
                                            (" xsp ", "_XSP"), ("_SPX", "_SPX"), ("NVDA", "NVDA"), ("BRK-B", "BRK-B")])
    def test_aliases(self, given, want):
        assert norm_ticker(given) == want

    def test_only_verified_indices_are_aliased(self):
        """NDX / RUT 在 CBOE 上的文件名没验证过：不映射，照普通代码处理（取不到就如实报）。"""
        assert norm_ticker("NDX") == "NDX" and set(INDEX_TICKERS) == {"_SPX", "_XSP"}

    def test_bad_symbols_still_rejected(self):
        for bad in ("^NDX", "$", "SP X", ""):
            with pytest.raises(BadRequest):
                norm_ticker(bad)


class TestAmSettled:
    RAW = {"contracts": [
        _c("SPX261120C07700000", 0),     # 上午结算根、到期日 ⇒ 丢
        _c("SPXW261120C07700000", 0),    # 下午结算 ⇒ 留
        _c("SPX261218C07700000", 28),    # 上午结算根、没到期 ⇒ 留
        _c("SPX261120P07600000", 0, cp="P"),
        {"symbol": None, "dte": 0},      # 读不出根 ⇒ 留（不猜）
    ], "underlying_price": 7720.0}

    def test_drops_only_am_root_on_expiry_day(self):
        out = drop_am_settled_expiring(self.RAW, "SPX")
        assert [c["symbol"] for c in out["contracts"]] == ["SPXW261120C07700000", "SPX261218C07700000", None]
        assert out["n_am_settled_expiry_day_excluded"] == 2

    def test_does_not_mutate_input_and_counts_zero(self):
        n = len(self.RAW["contracts"])
        out = drop_am_settled_expiring({"contracts": self.RAW["contracts"][1:3]}, "SPX")
        assert out["n_am_settled_expiry_day_excluded"] == 0 and len(self.RAW["contracts"]) == n

    def test_root_must_match_exactly(self):
        """SPXW 以 SPX 开头：按前缀判就会把下午结算的 0DTE 也丢掉。"""
        out = drop_am_settled_expiring({"contracts": [_c("SPXW261120C07700000", 0)]}, "SPX")
        assert out["n_am_settled_expiry_day_excluded"] == 0

    def test_only_spx_has_an_am_root(self):
        assert INDEX_TICKERS["_SPX"]["am_root"] == "SPX" and INDEX_TICKERS["_XSP"]["am_root"] is None


class TestLiveWiring:
    def _svc(self, tmp_path, seen):
        def fetch(t, *, as_of=None):
            raw, why = syn.demo_fetch(t, as_of=as_of)
            if raw is not None:
                exp = raw["contracts"][0]["expiry"]
                raw = {**raw, "contracts": raw["contracts"] + [
                    {**raw["contracts"][0], "symbol": "SPX" + raw["contracts"][0]["symbol"][-15:], "dte": 0, "expiry": exp}]}
            seen.append(t)
            return raw, why
        return AlphaBotService(demo=True, fetch_fn=fetch, state_dir=tmp_path / "demo")

    def test_index_live_goes_through_filter_and_reports_it(self, tmp_path):
        seen = []
        d = self._svc(tmp_path, seen).live("SPX")
        assert d["data_available"] is True and seen == ["_SPX"]
        assert d["index"] == {"label": "SPX", "n_am_settled_expiry_day_excluded": 1}
        assert "现金结算" in d["howto"]["index_note"] and "不会被提前指派" in d["howto"]["index_note"]

    def test_xsp_has_no_am_filter_but_has_note(self, tmp_path):
        d = self._svc(tmp_path, []).live("XSP")
        assert d["index"]["n_am_settled_expiry_day_excluded"] is None and d["howto"]["index_note"]

    def test_stocks_are_untouched(self, tmp_path):
        d = self._svc(tmp_path, []).live("NVDA")
        assert "index" not in d and "index_note" not in d["howto"]

    def test_watchlist_lists_indices_once(self, tmp_path):
        wl = self._svc(tmp_path, []).watchlist()
        assert wl["indices"] == ["_SPX", "_XSP"] and wl["index_labels"] == {"_SPX": "SPX", "_XSP": "XSP"}
        # tickers = 观察列表 + 自选：概览演示 / 搜索候选 / 结果页下拉都按它走，指数没有账本行（二次检查 #2）
        assert "_SPX" not in wl["tickers"] and "_SPX" not in wl["watchlist"]

    def test_index_added_as_favorite_is_listed_once(self, tmp_path):
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, state_dir=tmp_path / "st")
        svc.update_settings({"favorites": ["SPX", "SPY"]})
        wl = svc.watchlist()
        assert wl["favorites"] == ["SPY"] and "_SPX" not in wl["tickers"]


class TestSecondCheck:
    """684 / 689 二次检查抓到的。"""

    def test_premium_tab_also_drops_am_settled_contracts(self, tmp_path, monkeypatch):
        """溢价页取链不走 live()：调用处必须同样过滤，否则 dte_min=0 时已结算的月度合约会被排进去。"""
        import alphabot.service as S

        def fetch(t, *, as_of=None):
            raw, why = syn.demo_fetch(t, as_of=as_of)
            c0 = raw["contracts"][0]
            return {**raw, "contracts": raw["contracts"] + [{**c0, "symbol": "SPX" + c0["symbol"][-15:], "dte": 0}]}, why
        seen = []
        real = S.drop_am_settled_expiring

        def spy(raw, root):
            out = real(raw, root)
            seen.append(out["n_am_settled_expiry_day_excluded"])
            return out
        monkeypatch.setattr(S, "drop_am_settled_expiring", spy)
        svc = AlphaBotService(demo=True, fetch_fn=fetch, state_dir=tmp_path / "demo")
        svc._premium_full("_SPX", True)
        svc._premium_full("NVDA", True)
        assert seen == [1]                       # 只对 _SPX 过了一次、丢了那 1 张

    def test_index_page_has_no_favorite_button_and_routes_canonicalise(self):
        side = (STATIC / "pages" / "ticker.js").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert "watchlist?.indices || []).includes(t)" in side
        assert app.count("canonTicker(a)") == 2 and app.count("t !== a") == 2
        assert "const t = canonTicker(input.value);" in app

    def test_intraday_settings_show_labels(self):
        page = (STATIC / "pages" / "intraday.js").read_text(encoding="utf-8")
        assert "`关注 ${tLabel(t)}`" in page and "`立即给 ${tLabel(t)} 拍一张`" in page and "tLabel(x))" in page

    def test_non_object_snapshot_lines_are_bad_lines(self, tmp_path):
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, state_dir=tmp_path / "st")
        d = svc._intraday_dir("2026-10-28")
        assert d.is_relative_to(tmp_path)        # 夹具只写进本测试的临时目录
        d.mkdir(parents=True)
        good = '{"ts": "2026-10-28T10:00:00-04:00", "underlying_price": 100.0, "views": {}}'
        (d / "NVDA.jsonl").write_text("\n".join([good, "null", "[1, 2]", "{坏"]) + "\n", encoding="utf-8")
        day = svc.intraday("NVDA", "2026-10-28")
        assert len(day["snapshots"]) == 1 and day["n_bad_lines"] == 3
        hm = svc.intraday_heat("NVDA", "2026-10-28")
        assert hm["n_bad_lines"] == 3 and hm["reason"] == "no_values"


class TestScope:
    def test_indices_are_not_in_the_production_watchlist(self):
        from config import WATCHLIST
        assert not set(INDEX_TICKERS) & {str(t).upper() for t in WATCHLIST}

    def test_index_constants_live_only_in_alphabot_service(self):
        """卖权账本 / 日报 / 预注册模块不认指数：名单只在 alphabot/service.py。"""
        hits = [p for p in REPO.glob("*.py") if re.search(r"INDEX_TICKERS|drop_am_settled_expiring", p.read_text(encoding="utf-8"))]
        assert hits == []

    def test_frontend_takes_labels_from_meta(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        side = (STATIC / "pages" / "ticker.js").read_text(encoding="utf-8")
        assert "index_labels" in app and "index_labels" in side and '"指数"' in side
        # 只看字符串字面量（注释里提到名字不算抄名单）
        assert not re.search(r"""["'`]_(SPX|XSP)["'`]""", app + side), "前端不另抄指数名单：取 /api/meta 的 index_labels"
        assert re.search(r"""["'`]_(SPX|XSP)["'`]""", 'const x = "_SPX";')      # 判据有牙

    def test_howto_panel_shows_the_note(self):
        assert "hw.index_note" in (STATIC / "pages" / "howto.js").read_text(encoding="utf-8")
