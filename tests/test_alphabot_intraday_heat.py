"""Alpha Bot 盘中热力图叠价格路径（v0.45.684）的守卫。

每组回答一句「谁会红」：
  · 取值：格子取对口径那一列、缺值不当 0、逐拍差只配同一行权价（`TestCells`）；
  · 颜色截断：分位按最近秩、截断值只进上色列、真值保留（`TestClip`）；
  · 不等距：x 轴按拍排列，长间隔被记下来（`TestGaps`）；
  · 说不出来就说明白：没快照 / 只有一拍 / 全空各有原因码（`TestUnavailable`）；
  · 纯函数：不 import 卖权模块、不做 IO（`TestPurity`）；
  · 接线：接口能打、坏参数 400、页面用服务端数据画、口径常量不在前端另写（`TestWiring`）。
全部离线。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from alphabot import intraday_heat as IH
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService, BadRequest

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "alphabot" / "static"


def _snap(ts, price, rows, zg=None, view="le_45dte"):
    return {"ts": ts, "underlying_price": price,
            "views": {view: {"strikes": rows, "zero_gamma": {"nearest": zg}}}}


S3 = [
    _snap("2026-10-28T10:00:00-04:00", 100.0, [[95.0, 10.0, 1.0], [100.0, -20.0, 2.0], [105.0, 5.0, None]], zg=98.0),
    _snap("2026-10-28T10:10:00-04:00", 101.0, [[95.0, 12.0, 4.0], [100.0, -18.0, 3.0]], zg=None),
    _snap("2026-10-28T10:20:00-04:00", 102.0, [[100.0, -10.0, 9.0], [105.0, 7.0, 6.0], [110.0, 1.0, 1.0]], zg=99.5),
]


def _cells(out):
    return {(c[0], c[1]): c[2] for c in out["cells"]}


class TestCells:
    def test_level_takes_the_chosen_column(self):
        oi = _cells(IH.build(S3, basis="oi"))
        vol = _cells(IH.build(S3, basis="volume"))
        assert oi[(0, 95.0)] == 10.0 and vol[(0, 95.0)] == 1.0
        assert oi[(2, 110.0)] == 1.0

    def test_missing_value_is_no_cell_not_zero(self):
        vol = _cells(IH.build(S3, basis="volume"))
        assert (0, 105.0) not in vol          # 值是 None
        assert (1, 105.0) not in vol          # 这一拍窗口里没有这个行权价

    def test_delta_pairs_same_strike_with_previous_snapshot_only(self):
        d = _cells(IH.build(S3, basis="oi", mode="delta"))
        assert not any(ti == 0 for ti, _ in d)            # 首拍没有上一拍
        assert d[(1, 95.0)] == pytest.approx(2.0) and d[(1, 100.0)] == pytest.approx(2.0)
        assert d[(2, 100.0)] == pytest.approx(8.0)
        assert (2, 105.0) not in d                         # 上一拍没有 105（不拿两拍前的配）
        assert (2, 95.0) not in d                          # 这一拍没有 95

    def test_strikes_are_the_union_and_step_is_the_smallest_gap(self):
        out = IH.build(S3)
        assert out["strikes"] == [95.0, 100.0, 105.0, 110.0] and out["step"] == 5.0

    def test_price_and_zero_gamma_paths_keep_gaps_as_none(self):
        out = IH.build(S3)
        assert out["price"] == [100.0, 101.0, 102.0]
        assert out["zero_gamma"] == [98.0, None, 99.5]
        assert out["times"] == ["10:00", "10:10", "10:20"]

    def test_non_finite_and_bool_values_are_missing(self):
        s = [_snap("2026-10-28T10:00:00-04:00", float("nan"), [[95.0, float("inf"), True], [100.0, 3.0, 1.0]])]
        out = IH.build(s, basis="oi")
        assert _cells(out) == {(0, 100.0): 3.0} and out["price"] == [None]
        assert _cells(IH.build(s, basis="volume")) == {(0, 100.0): 1.0}

    def test_view_selects_the_tenor_view(self):
        s = [_snap("2026-10-28T10:00:00-04:00", 100.0, [[100.0, 7.0, 1.0]], view="next_expiry")]
        assert IH.build(s, view="next_expiry")["available"] is True
        assert IH.build(s, view="le_45dte")["reason"] == "no_values"

    def test_zero_gamma_path_comes_from_the_same_view_as_the_cells(self):
        s = _snap("2026-10-28T10:00:00-04:00", 100.0, [[100.0, 1.0, 1.0]], zg=97.0)
        s["views"]["next_expiry"] = {"strikes": [[100.0, 2.0, 2.0]], "zero_gamma": {"nearest": 103.0}}
        assert IH.build([s], view="le_45dte")["zero_gamma"] == [97.0]
        assert IH.build([s], view="next_expiry")["zero_gamma"] == [103.0]


class TestClip:
    def _out(self, n=20):
        rows = [[float(90 + i), float(i + 1) * (-1) ** i, 0.0] for i in range(n)]
        return IH.build([_snap("2026-10-28T10:00:00-04:00", 100.0, rows)])

    def test_nearest_rank_quantile(self):
        out = self._out(20)
        assert out["clip"] == 19.0                 # ceil(0.95·20) = 19 ⇒ 第 19 小
        assert out["max_abs"] == 20.0 and out["n_clipped"] == 1

    def test_clip_only_changes_the_colour_column(self):
        out = self._out(20)
        big = [c for c in out["cells"] if abs(c[2]) == 20.0][0]
        assert abs(big[3]) == 19.0 and big[2] == 20.0 * (-1) ** 19
        assert all(c[2] == c[3] for c in out["cells"] if abs(c[2]) <= 19.0)

    def test_quantile_follows_the_constant(self, monkeypatch):
        monkeypatch.setattr(IH, "CLIP_QUANTILE", 0.5)
        assert self._out(20)["clip"] == 10.0
        assert IH.build(S3)["rules"]["clip_quantile"] == 0.5


class TestGaps:
    def test_long_interval_is_recorded(self):
        ts = ["10:00", "10:10", "10:20", "10:30", "11:15", "11:25"]
        s = [_snap(f"2026-10-28T{t}:00-04:00", 100.0, [[100.0, 1.0, 1.0]]) for t in ts]
        assert IH.build(s)["gaps"] == [{"index": 4, "minutes": 45.0}]

    def test_regular_spacing_has_no_gaps_and_bad_ts_is_skipped(self):
        s = [_snap(f"2026-10-28T10:{m:02d}:00-04:00", 100.0, [[100.0, 1.0, 1.0]]) for m in (0, 10, 20, 30)]
        s.insert(2, _snap("坏时刻", 100.0, [[100.0, 1.0, 1.0]]))
        assert IH.build(s)["gaps"] == []


class TestUnavailable:
    def test_reasons(self):
        assert IH.build([])["reason"] == "no_snapshots"
        assert IH.build(S3[:1], mode="delta")["reason"] == "need_two_snapshots"
        empty = [_snap("2026-10-28T10:00:00-04:00", 100.0, [[100.0, None, None]])]
        out = IH.build(empty)
        assert out["available"] is False and out["reason"] == "no_values" and out["cells"] == []

    def test_all_zero_is_no_values_not_a_divide_by_zero(self):
        out = IH.build([_snap("2026-10-28T10:00:00-04:00", 100.0, [[100.0, 0.0, 0.0]])])
        assert out["reason"] == "no_values" and out["clip"] is None

    def test_every_reason_has_a_label(self):
        for snaps, mode in (([], "level"), (S3[:1], "delta"),
                            ([_snap("2026-10-28T10:00:00-04:00", 1.0, [])], "level")):
            assert IH.build(snaps, mode=mode)["reason"] in IH.REASONS

    @pytest.mark.parametrize("kw", [{"view": "x"}, {"basis": "x"}, {"mode": "x"}])
    def test_unknown_params_raise(self, kw):
        with pytest.raises(ValueError):
            IH.build(S3, **kw)


class TestPurity:
    SRC = (REPO / "alphabot" / "intraday_heat.py").read_text(encoding="utf-8")

    def test_no_sell_module_import_and_no_io(self):
        tree = ast.parse(self.SRC)
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert not {m for m in mods if m.startswith("sell_strike")}, mods
        assert not mods & {"os", "pathlib", "socket", "requests", "urllib", "subprocess", "shutil", "json"}, mods
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"open", "print", "exec", "eval"}, calls

    def test_guard_has_teeth(self):
        bad = ast.parse("import sell_strike_report\nopen('x')")
        assert {n.names[0].name for n in ast.walk(bad) if isinstance(n, ast.Import)} == {"sell_strike_report"}


class TestWiring:
    def _svc(self, tmp_path):
        return AlphaBotService(demo=True, fetch_fn=syn.demo_fetch, state_dir=tmp_path / "demo")

    def test_demo_endpoint_and_bad_params(self, tmp_path):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        c = TestClient(create_app(self._svc(tmp_path), port=8765), base_url="http://127.0.0.1:8765")
        for q in ("", "?basis=volume&mode=delta", "?view=next_expiry"):
            r = c.get(f"/api/intraday-heat/NVDA{q}")
            assert r.status_code == 200, r.text[:200]
            d = r.json()
            assert d["available"] is True and d["n_snapshots"] == 40 and d["ticker"] == "NVDA"
            assert len(d["price"]) == 40 and d["rules"]["strike_band"] > 0
        assert c.get("/api/intraday-heat/NVDA?mode=bogus").status_code == 400
        assert not (tmp_path / "demo").exists()

    def test_service_uses_the_same_snapshots_as_the_intraday_endpoint(self, tmp_path):
        svc = self._svc(tmp_path)
        day = svc.intraday("NVDA")
        hm = svc.intraday_heat("NVDA")
        assert hm["price"] == [s["underlying_price"] for s in day["snapshots"]]
        with pytest.raises(BadRequest):
            svc.intraday_heat("NVDA", basis="gamma")

    def test_page_draws_server_data_and_old_heatmap_is_gone(self):
        page = (STATIC / "pages" / "intraday.js").read_text(encoding="utf-8")
        charts = (STATIC / "charts.js").read_text(encoding="utf-8")
        assert "/api/intraday-heat/" in page and "gammaTrace(hmEl, hm)" in page
        assert not re.search(r"export function heatmap\(", charts) and "heatmap(" not in page
        assert "charts.forEach(release)" in page        # 同页重画时释放旧图

    def test_page_does_not_restate_rule_constants(self):
        """截断分位 / 行权价窗口只从服务端 rules 取：页面里写死 95% 或 12% 就会和代码分岔。"""
        page = (STATIC / "pages" / "intraday.js").read_text(encoding="utf-8")
        assert "r.clip_quantile" in page and "r.strike_band" in page
        assert not re.search(r"95\s*%|12\s*%|0\.95|0\.12", page)

    def test_volume_delta_note_says_it_has_no_direction(self):
        page = (STATIC / "pages" / "intraday.js").read_text(encoding="utf-8")
        assert "没有买卖方向" in page and "不是 orderflow" in page
