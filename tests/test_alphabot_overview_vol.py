"""Alpha Bot 总览 IV / RV 列（v0.45.691）的守卫。

每组回答一句「谁会红」：
  · 选行：只取总览那一天的 VRP 行、同票两行不猜、缺值 / 非有限 / RV ≤ 0 不出比值（`TestVolBlock`）；
  · 失败说清楚：账本读不了 / 文件不在 / 那天没有行各有原因，不混成一种（`TestVolBlock`、`TestOverviewWiring`）；
  · 接线：总览走的是数据根下的 VRP 账本、不打网，演示模式不带这一列（`TestOverviewWiring`）；
  · 页面：口径说明写明「不是同一个 IV」「未经前向检验」（`TestPage`）。
全部离线。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from alphabot.service import AlphaBotService, vol_block

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "alphabot" / "static"
D = "2026-11-02"


def _row(t, iv, rv, date=D, **kw):
    return {"ticker": t, "date": date, "iv": iv, "rv_30d": rv, "iv_source": "iv_raw_observed", **kw}


class TestVolBlock:
    def test_picks_only_that_day_and_computes_ratio(self):
        rows = [_row("NVDA", 47.68, 29.58), _row("NVDA", 40.0, 20.0, date="2026-10-30"), _row("brk-b", 22.555, 11.8)]
        out = vol_block(rows, None, D)
        assert out["available"] is True and out["reason"] is None and out["date"] == D
        assert out["by_ticker"]["NVDA"] == {"iv": 47.68, "rv_30d": 29.58, "iv_rv": 1.612, "iv_source": "iv_raw_observed"}
        assert out["by_ticker"]["BRK-B"]["iv_rv"] == 1.911          # 代码统一大写，与总览行对得上

    def test_no_older_day_stands_in(self):
        out = vol_block([_row("NVDA", 40.0, 20.0, date="2026-10-30")], None, D)
        assert out["available"] is False and out["reason"] == "vrp_no_rows_for_date" and out["by_ticker"] == {}

    def test_duplicate_rows_are_not_guessed(self):
        out = vol_block([_row("NVDA", 47.0, 29.0), _row("NVDA", 48.0, 30.0)], None, D)
        assert out["by_ticker"]["NVDA"]["duplicate"] is True and out["by_ticker"]["NVDA"]["iv"] is None

    @pytest.mark.parametrize("iv,rv", [(None, 20.0), (40.0, None), (40.0, 0.0), (40.0, -1.0), (float("nan"), 20.0),
                                       (40.0, float("inf")), (True, 20.0), ("40", 20.0)])
    def test_bad_values_give_no_ratio(self, iv, rv):
        v = vol_block([_row("NVDA", iv, rv)], None, D)["by_ticker"]["NVDA"]
        assert v["iv_rv"] is None

    def test_errors_and_missing_date(self):
        assert vol_block(None, "ledger_unreadable:LedgerCorrupt", D)["reason"] == "ledger_unreadable:LedgerCorrupt"
        assert vol_block([_row("NVDA", 40.0, 20.0)], None, None)["reason"] == "no_as_of"
        assert vol_block([None, "x", {"date": D}], None, D)["reason"] == "vrp_no_rows_for_date"


class TestOverviewWiring:
    def _svc(self, tmp_path, monkeypatch, vrp_rows):
        """非演示服务：卖权账本只读不到（overview 的账本部分打桩），VRP 账本写在本测试自己的数据根里。"""
        import alphabot.service as S
        svc = AlphaBotService(fetch_fn=lambda *a, **k: (None, "offline"), state_dir=tmp_path / "st")
        home = tmp_path / "home"
        monkeypatch.setattr(svc, "_premium_home", lambda: home)
        path = svc._vrp_path()
        assert path.is_relative_to(tmp_path)                  # 夹具只写进本测试的临时目录
        if vrp_rows is not None:
            path.parent.mkdir(parents=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in vrp_rows), encoding="utf-8")
        monkeypatch.setattr(S.R, "rows_for_date_view",
                            lambda d, **k: {"data_available": True, "as_of": d, "tenors": {"monthly": [], "weekly": []}})
        return svc

    def test_overview_carries_vol_for_its_own_date(self, tmp_path, monkeypatch):
        svc = self._svc(tmp_path, monkeypatch, [_row("NVDA", 47.68, 29.58), _row("NVDA", 1.0, 1.0, date="2026-10-30")])
        v = svc.overview(D)["vol"]
        assert v["available"] is True and v["by_ticker"]["NVDA"]["iv_rv"] == 1.612

    def test_missing_ledger_is_not_reported_as_no_rows(self, tmp_path, monkeypatch):
        v = self._svc(tmp_path, monkeypatch, None).overview(D)["vol"]
        assert v["available"] is False and v["reason"] == "vrp_ledger_missing"

    def test_corrupt_ledger_says_unreadable(self, tmp_path, monkeypatch):
        svc = self._svc(tmp_path, monkeypatch, [_row("NVDA", 47.68, 29.58)])
        with open(svc._vrp_path(), "a", encoding="utf-8") as f:
            f.write("{坏行\n")
        v = svc.overview(D)["vol"]
        assert v["available"] is False and v["reason"].startswith("ledger_unreadable")

    def test_premium_keeps_its_old_missing_file_semantics(self, tmp_path, monkeypatch):
        """溢价页对「文件不在」沿用旧口径（load_jsonl 返回 []），这次没改它。"""
        svc = self._svc(tmp_path, monkeypatch, None)
        assert svc._vrp_rows() == ([], None)


class TestSecondCheck691:
    """691 合入前二次检查抓到的。"""

    def test_demo_overview_says_demo_not_unavailable(self, tmp_path):
        from alphabot import synthetic as syn
        svc = AlphaBotService(demo=True, fetch_fn=syn.demo_fetch, state_dir=tmp_path / "demo")
        v = svc.overview()["vol"]
        assert v["available"] is False and v["reason"] == "demo"
        assert 'demo: "演示模式没有 VRP 账本"' in TestPage.SRC

    def test_fallback_iv_source_is_shown_in_the_cell(self):
        assert 'v.iv_source !== "iv_raw_observed"' in TestPage.SRC and "IV 来源 ${v.iv_source}" in TestPage.SRC

    def test_canonical_redirects_replace_history_and_encode(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert "location.replace(hash)" in app
        assert app.count("redirect(`#/t/${encodeURIComponent(t)}/") == 1
        assert app.count("redirect(`#/intraday/${encodeURIComponent(t)}`)") == 1
        assert "go(`#/t/${t}/" not in app and "go(`#/intraday/${t}`)" not in app


class TestPage:
    SRC = (STATIC / "pages" / "overview.js").read_text(encoding="utf-8")

    def test_column_reads_server_block(self):
        assert "vol.by_ticker?.[r.ticker]" in self.SRC and '"IV / RV（30 天）"' in self.SRC

    def test_note_says_what_it_is_not(self):
        assert "与标的页的 CBOE IV30 不是同一个量" in self.SRC and "未经前向检验" in self.SRC

    def test_every_server_reason_has_a_label(self):
        for code in ("vrp_ledger_missing", "vrp_no_rows_for_date", "no_as_of"):
            assert f"{code}:" in self.SRC, code
        assert 'startsWith("ledger_unreadable")' in self.SRC
