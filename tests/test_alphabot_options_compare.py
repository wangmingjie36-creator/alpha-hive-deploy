"""Alpha Bot「对比」页（v0.45.759，`alphabot/options_compare.py` + `/api/compare` + `static/pages/compare.js`）的守卫。离线。

每组回答一句「谁会红」：
  · 读数照记录原样抽：取不到 ⇒ None 不填默认值；全链 OI 缺一侧不拿另一侧顶替（`TestDayView`）；
  · 变化：任一边缺 ⇒ None（不当 0 变化）；IV Rank 两天口径不同不比；提示恰在阈值上挂、差一点不挂；
    近端窗口的 OI / P/C 跳变**不挂提示**（月度到期日进出窗口造成的假跳变，`TestCompare`）；
  · 「上一次」= 这只票有记录的上一个扫描日，跳过了几天要说（`TestBuild`）；
  · 读盘：文件在自己的 tmp 里；坏文件 ≠ 空的一天、没文件 ≠ 坏文件、演示模式不读盘（`TestService`）；
  · 前端接线：顶栏 / 路由 / 帮助章节一致；阈值取自服务端不写字面量；页面不出方向判断字段（`TestFrontend`）；
  · 本模块不读文件、不联网、不 import 项目里别的模块（`TestPurity`）。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from alphabot import options_compare as OC
from alphabot.service import AlphaBotService

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "alphabot" / "static"
D0, D1, D2 = "2026-11-06", "2026-11-09", "2026-11-10"


def _res(score=6.0, direction="bullish", spot=100.0, pc=0.80, ivr=50.0, src="iv_history", skew=1.00, oi=1000.0,
         flows=None, fpc=1.20, fcall=5000, fput=6000, **extra):
    flows = flows if flows is not None else [
        {"type": "call_sweep", "strike": 110.0, "volume": 300, "oi": 100, "ratio": 3.0, "otm_pct": 10.0,
         "dollar_premium": 9e5, "days_to_exp": 9, "expiry": "2026-11-20"},
        {"type": "large_put_volume", "strike": 90.0, "volume": 100, "oi": 400, "ratio": 0.25, "otm_pct": -10.0,
         "dollar_premium": 2e6, "days_to_exp": 9, "expiry": "2026-11-20"}]
    details = {"_snapshot_stock_price": spot, "put_call_ratio": pc, "iv_rank": ivr, "iv_rank_source": src,
               "iv_current": 40.0, "rv_30d": 30.0, "iv_rv_spread": 10.0, "iv_skew_ratio": skew,
               "iv_skew_detail": {"otm_put_iv": 42.0, "otm_call_iv": 42.0}, "total_oi": oi, "data_quality": "real",
               "max_pain": {"max_pain": 95.0}, "unusual_activity": flows, "unusual_flow_ok": True,
               "iv_term_structure": {"shape": "contango", "front_iv": 40.0, "back_iv": 45.0},
               "key_levels": {"support": [{"strike": 90.0, "oi": 500.0}, {"strike": 85.0, "oi": 300.0}],
                              "resistance": [{"strike": 110.0, "oi": 700.0}]},
               "full_chain_oi": {"full_pc_ratio": fpc, "total_call_oi": fcall, "total_put_oi": fput,
                                 "oi_by_strike_call": {"100.0": 1}}, **extra}
    return {"final_score": score, "direction": direction, "resonance": {"resonance_detected": True},
            "agent_details": {"OracleBeeEcho": {"details": details}, "ScoutBeeNova": {"details": {"price": 99.0}}}}


class TestDayView:
    def test_reads_recorded_values(self):
        v = OC.day_view(_res())
        assert (v["score"], v["direction"], v["spot"], v["spot_source"]) == (6.0, "bullish", 100.0, "options_snapshot")
        assert (v["pc_ratio"], v["full_pc_ratio"], v["iv_rank"], v["total_oi"], v["max_pain"]) == (0.8, 1.2, 50.0, 1000.0, 95.0)
        assert v["full_oi"] == 11000.0
        assert v["supports"][0] == {"strike": 90.0, "oi": 500.0, "dist_pct": -10.0}
        fs = v["flow_summary"]                     # call 300 手 @110、put 100 手 ⇒ call 占 75%，重心 110
        assert (fs["call_pct"], fs["call_centroid"], fs["count"], fs["unclassified"]) == (75.0, 110.0, 2, 0)
        assert [f["side"] for f in v["flows"]] == ["put", "call"]          # 按权利金金额排，不按成交量

    def test_missing_values_stay_none(self):
        v = OC.day_view({"final_score": 5.0, "direction": "neutral"})
        assert v["options_available"] is False and v["score"] == 5.0
        for k in ("spot", "pc_ratio", "full_pc_ratio", "iv_rank", "skew", "total_oi", "full_oi", "max_pain"):
            assert v[k] is None, k
        assert v["flow_summary"]["call_pct"] is None and v["flows"] == [] and v["supports"] == []
        assert OC.day_view(None) is None and OC.day_view("x") is None

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "0.8", None])
    def test_non_finite_and_non_numbers_are_none(self, bad):
        assert OC.day_view(_res(pc=bad))["pc_ratio"] is None

    def test_spot_falls_back_to_scout_and_says_so(self):
        v = OC.day_view(_res(spot=None))
        assert (v["spot"], v["spot_source"]) == (99.0, "scout")

    def test_unclassifiable_flow_is_counted_not_guessed(self):
        v = OC.day_view(_res(flows=[{"type": "block", "strike": 100.0, "volume": 50}]))
        assert v["flow_summary"] == {"count": 0, "unclassified": 1, "call_volume": 0.0, "put_volume": 0.0,
                                     "call_pct": None, "call_centroid": None}

    def test_full_oi_needs_both_sides(self):
        assert OC.day_view(_res(fcall=None))["full_oi"] is None and OC.day_view(_res(fput=float("nan")))["full_oi"] is None

    def test_views_of_uppercases_and_drops_non_objects(self):
        v = OC.views_of({"nvda": _res(), "bad": 3, "x": None})
        assert list(v) == ["NVDA"] and v["NVDA"] == OC.day_view(_res()) and OC.views_of(None) == {}
        assert len(json.dumps(v)) < 4000 and "oi_by_strike_call" not in json.dumps(v)     # 缓存的是读数，不是原始大表


class TestCompare:
    def test_deltas(self):
        t = OC.day_view(_res(score=6.5, spot=110.0, pc=1.0, fpc=1.3, ivr=60.0, skew=1.05, oi=1100.0, fcall=5600, fput=6500))
        p = OC.day_view(_res())
        d = OC.compare(t, p)
        assert (d["score"], d["spot_pct"], d["full_pc_ratio"], d["iv_rank"], d["skew"], d["full_oi_pct"]) == \
            (0.5, 10.0, 0.1, 10.0, 0.05, 10.0)
        assert (d["window_pc_ratio"], d["window_oi_pct"]) == (0.2, 10.0)
        assert d["direction_flipped"] is False and d["iv_rank_comparable"] is True

    def test_missing_side_is_none_not_zero(self):
        d = OC.compare(OC.day_view(_res()), OC.day_view({"final_score": 6.0, "direction": "bullish"}))
        assert d["available"] is True and d["score"] == 0.0
        for k in ("spot_pct", "full_pc_ratio", "window_pc_ratio", "skew", "full_oi_pct", "window_oi_pct", "max_pain",
                  "call_pct", "call_centroid"):
            assert d[k] is None, k
        assert OC.compare(OC.day_view(_res()), None) == {"available": False}

    def test_iv_rank_not_compared_across_sources(self):
        d = OC.compare(OC.day_view(_res(ivr=90.0, src="hv_proxy")), OC.day_view(_res(ivr=10.0, src="iv_history")))
        assert d["iv_rank"] is None and d["iv_rank_comparable"] is False
        assert not [a for a in OC.alerts(OC.day_view(_res(ivr=90.0, src="hv_proxy")), OC.day_view(_res()), d)
                    if a["key"] == "iv_rank"]

    @pytest.mark.parametrize("kw,key,hit", [
        ({"score": 7.5}, "score", True), ({"score": 7.49}, "score", False),
        ({"fpc": 1.45}, "full_pc_ratio", True), ({"fpc": 1.44}, "full_pc_ratio", False),
        ({"ivr": 70.0}, "iv_rank", True), ({"ivr": 69.0}, "iv_rank", False),
        ({"skew": 1.10}, "skew", True), ({"skew": 1.09}, "skew", False),
        ({"fcall": 6650}, "full_oi", True), ({"fcall": 6649}, "full_oi", False),
        ({"fput": 4350}, "full_oi", True),
    ])
    def test_alert_thresholds(self, kw, key, hit):
        t, p = OC.day_view(_res(**kw)), OC.day_view(_res())
        keys = [a["key"] for a in OC.alerts(t, p, OC.compare(t, p))]
        assert (key in keys) is hit, keys

    def test_window_jumps_raise_no_alert(self):
        """月度到期日进出 4 个到期日的窗口 ⇒ 窗口 OI 跳 3～5 倍、窗口 P/C 跟着变，全链几乎不动（2026-11-11 实测 NVDA
        10-29 → 10-30：窗口 OI 52.8 万 → 175.7 万、窗口 P/C 1.24 → 0.85；全链 OI 1106 万 → 1096 万）。那不是持仓变化。"""
        t = OC.day_view(_res(oi=1757385.0, pc=0.85, fpc=0.979, fcall=5500000, fput=5462461))
        p = OC.day_view(_res(oi=527916.0, pc=1.24, fpc=0.906, fcall=5800000, fput=5259639))
        d = OC.compare(t, p)
        assert d["window_oi_pct"] > 200 and abs(d["full_oi_pct"]) < 1
        assert OC.alerts(t, p, d) == []

    def test_flip_and_centroid_alerts(self):
        up = [{"type": "call_sweep", "strike": 113.0, "volume": 300, "dollar_premium": 1.0}]
        t, p = OC.day_view(_res(direction="bearish", flows=up)), OC.day_view(_res())
        al = OC.alerts(t, p, OC.compare(t, p))
        assert al[0] == {"level": "flip", "key": "direction", "text": "方向翻转：看多 → 看空"}
        assert [a["key"] for a in al][-1] == "call_centroid" and "上移 3.00" in al[-1]["text"]   # 3 / 100 = 3% ≥ 2.5%
        assert OC.alerts(t, None, {"available": False}) == []

    def test_alert_text_never_reads_direction_into_a_move(self):
        t, p = OC.day_view(_res(score=9.0, fpc=2.0, ivr=99.0, skew=1.5, fcall=50000)), OC.day_view(_res())
        assert len(OC.alerts(t, p, OC.compare(t, p))) == 5
        text = " ".join(a["text"] for a in OC.alerts(t, p, OC.compare(t, p)))
        for word in ("看涨", "看跌", "买入", "卖出", "建议", "升温", "谨慎"):
            assert word not in text, word


class TestBuild:
    DAYS = [(D0, OC.views_of({"AAA": _res(score=5.0), "BBB": _res()})), (D1, OC.views_of({"BBB": _res()})),
            (D2, OC.views_of({"AAA": _res(score=7.0, direction="bearish"), "BBB": _res(), "CCC": _res()}))]

    def test_prev_is_last_scan_with_that_ticker(self):
        o = OC.build_overview(self.DAYS, D2)
        rows = {r["ticker"]: r for r in o["rows"]}
        assert (rows["AAA"]["prev_date"], rows["AAA"]["scans_between"], rows["AAA"]["delta"]["score"]) == (D0, 1, 2.0)
        assert (rows["BBB"]["prev_date"], rows["BBB"]["scans_between"]) == (D1, 0)
        assert rows["CCC"]["prev_date"] is None and rows["CCC"]["delta"] == {"available": False} and rows["CCC"]["alerts"] == []
        assert o["rows"][0]["ticker"] == "AAA" and o["prev_scan_date"] == D1          # 翻转的排最前
        assert o["rules"]["score_jump"] == OC.RULES["score_jump"]

    def test_unknown_date_and_ticker(self):
        assert OC.build_overview(self.DAYS, "2026-11-08")["reason"] == "no_such_date"
        assert OC.build_ticker("ZZZ", self.DAYS, D2)["reason"] == "ticker_not_scanned"
        assert OC.build_ticker("AAA", self.DAYS, D1)["reason"] == "ticker_not_scanned"   # 那天没扫它：不拿别的日子顶替

    def test_ticker_detail_and_history(self):
        d = OC.build_ticker("AAA", self.DAYS, D2)
        assert d["available"] and d["prev_date"] == D0 and d["scans_between"] == 1
        assert [h["date"] for h in d["history"]] == [D0, D2] and d["alerts"][0]["key"] == "direction"
        first = OC.build_ticker("CCC", self.DAYS, D2)
        assert first["available"] and first["reason"] == "no_prev" and first["prev"] is None and first["alerts"] == []

    def test_history_is_capped_and_never_looks_past_the_date(self):
        days = [(f"2026-{m:02d}-{d:02d}", OC.views_of({"AAA": _res()})) for m in (9, 10) for d in range(1, 29)]
        d = OC.build_ticker("AAA", days, days[-3][0])
        assert len(d["history"]) == OC.RULES["history_days"] and d["history"][-1]["date"] == days[-3][0]


class TestService:
    def _svc(self, tmp_path, files, **kw):
        for date, body in files.items():
            (tmp_path / f".swarm_results_{date}.json").write_text(body if isinstance(body, str) else json.dumps(body))
        return AlphaBotService(fetch_fn=lambda *a, **k: (None, "offline"), state_dir=tmp_path / "st",
                               compare_root=tmp_path, **kw)

    def test_reads_latest_and_compares(self, tmp_path):
        svc = self._svc(tmp_path, {D1: {"nvda": _res(score=5.0), "BRK-B": _res()}, D2: {"nvda": _res(score=7.0), "BRK-B": _res()}})
        (tmp_path / ".swarm_results_backup.json").write_text("{}")           # 不合命名的文件不当成一天
        o = svc.compare()
        assert o["available"] and o["date"] == D2 and o["dates"] == [D1, D2] and o["unreadable"] == {}
        assert {r["ticker"]: r["delta"]["score"] for r in o["rows"]} == {"NVDA": 2.0, "BRK-B": 0.0}
        assert svc.compare(D1)["rows"][0]["prev_date"] is None               # 只往前看，不拿后一天当「上一次」
        d = svc.compare_ticker("brk.b")                                       # CBOE 写法 ⇒ 扫描侧的 BRK-B
        assert d["available"] and d["ticker"] == "BRK-B" and d["prev_date"] == D1

    def test_fixture_is_actually_wired(self, tmp_path):
        """正面核对读的是本测试自己的目录：同一个服务，目录里没文件就得报 no_scan_files（而不是读到真实数据根）。"""
        assert self._svc(tmp_path, {}).compare()["reason"] == "no_scan_files"
        assert self._svc(tmp_path, {}).compare_ticker("NVDA")["reason"] == "no_scan_files"

    def test_bad_file_is_not_an_empty_day(self, tmp_path):
        svc = self._svc(tmp_path, {D0: {"NVDA": _res(score=5.0)}, D1: "{not json", D2: {"NVDA": _res(score=7.0)}})
        o = svc.compare()
        assert o["available"] and list(o["unreadable"]) == [D1] and "读不了" in o["unreadable"][D1]
        assert o["rows"][0]["prev_date"] == D0                                # 越过坏的那天，并且说了
        bad = svc.compare(D1)
        assert bad["available"] is False and bad["reason"] == "file_unreadable" and bad["rows"] == []
        assert svc.compare("2026-11-08")["reason"] == "no_such_date"
        top = self._svc(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, {D2: "[1, 2]"}).compare()
        assert top["reason"] == "file_unreadable"                             # 顶层不是对象

    def test_rewritten_file_is_reread(self, tmp_path):
        svc = self._svc(tmp_path, {D2: {"NVDA": _res(score=5.0)}})
        assert svc.compare()["rows"][0]["score"] == 5.0
        (tmp_path / f".swarm_results_{D2}.json").write_text(json.dumps({"NVDA": _res(score=8.25), "AMD": _res()}))
        assert {r["ticker"]: r["score"] for r in svc.compare()["rows"]} == {"NVDA": 8.25, "AMD": 6.0}

    def test_nan_in_file_does_not_break_json(self, tmp_path):
        svc = self._svc(tmp_path, {D2: '{"NVDA": {"final_score": NaN, "direction": "neutral"}}'})
        row = svc.compare()["rows"][0]
        assert row["score"] is None
        json.dumps(svc.compare(), allow_nan=False)

    def test_demo_never_reads_disk(self, tmp_path):
        svc = self._svc(tmp_path, {D2: {"NVDA": _res()}}, demo=True)
        assert svc.compare()["reason"] == "demo" and svc.compare_ticker("NVDA")["reason"] == "demo"

    def test_endpoints(self, tmp_path):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        svc = self._svc(tmp_path, {D1: {"NVDA": _res()}, D2: {"NVDA": _res(fpc=1.6)}})
        c = TestClient(create_app(svc, port=8765), base_url="http://127.0.0.1:8765")
        assert c.get("/api/compare").json()["rows"][0]["alerts"][0]["key"] == "full_pc_ratio"
        assert c.get(f"/api/compare/NVDA?date={D1}").json()["reason"] == "no_prev"
        assert c.get("/api/compare?date=nope").status_code == 400
        assert c.get("/api/compare/N!VDA").status_code == 400
        assert c.get("/api/method").json()["compare"]["pc_jump"] == OC.RULES["pc_jump"]
        assert c.get("/static/pages/compare.js").status_code == 200


class TestFrontend:
    PAGE = STATIC / "pages" / "compare.js"

    def test_nav_route_help_are_wired(self):
        assert 'href="#/compare" data-nav="compare"' in (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert 'from "./pages/compare.js"' in app and 'page === "compare"' in app
        assert '["compare", "期权跨日对比"' in (STATIC / "pages" / "help.js").read_text(encoding="utf-8")
        assert 'helpLink("compare")' in self.PAGE.read_text(encoding="utf-8")

    def test_thresholds_are_not_copied_into_the_frontend(self):
        """阈值只在 `options_compare.RULES`：帮助页经 m.compare.* 取；对比页自己不判阈值（提示由服务端给）。"""
        help_txt = (STATIC / "pages" / "help.js").read_text(encoding="utf-8")
        sec = help_txt.split('["compare", "期权跨日对比"', 1)[1].split('\n  ["wheel"', 1)[0]
        for key in ("score_jump", "pc_jump", "iv_rank_jump", "skew_jump", "oi_jump_pct", "centroid_shift_pct", "history_days"):
            assert f"r.{key}" in sec, key
            assert key not in self.PAGE.read_text(encoding="utf-8"), key
        assert set(OC.public_rules()) >= set(OC.RULES)

    def test_every_field_the_page_reads_exists(self):
        """页面读的 `r.x` / `t.x` / `dl.x` 字段都在服务端输出里（改名一头忘了另一头 ⇒ 页面静默显示「—」）。"""
        txt = self.PAGE.read_text(encoding="utf-8")
        days = TestBuild.DAYS
        row = OC.build_overview(days, D2)["rows"][0]
        det = OC.build_ticker("AAA", days, D2)
        hist = set(det["history"][0])
        assert set(re.findall(r"hist\.map\(\(r\) => r\.(\w+)\)", txt)) <= hist
        for pat, have in ((r"\br\.delta\.(\w+)", set(row["delta"])), (r"\bdl\.(\w+)", set(det["delta"])),
                          (r"\bp\.(\w+)", set(det["today"])),
                          (r"\(r\) => \w+\((?:num|oiShort)\(r\.(\w+)", set(row)),
                          (r"\bfs\.(\w+)", set(det["today"]["flow_summary"])),
                          (r"\bt\.(\w+)", set(det["today"])), (r"\bd\.(\w+)", set(det) | {"prev_scan_date", "rows", "dates", "unreadable"})):
            used = set(re.findall(pat, txt))
            assert used and used <= have, (pat, used - have)

    def test_no_recommendation_or_prediction_fields(self):
        keys = set()

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    keys.add(k)
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(OC.build_ticker("AAA", TestBuild.DAYS, D2))
        assert not {k for k in keys if re.search(r"recommend|predict|confidence|accuracy|signal$|advice", k)}, keys


class TestPurity:
    def test_module_is_pure(self):
        tree = ast.parse((REPO / "alphabot" / "options_compare.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert mods == {"__future__", "math", "typing"}, mods
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"open", "exec", "eval", "__import__"}

    def test_reason_codes_all_have_text(self):
        src = (REPO / "alphabot" / "options_compare.py").read_text(encoding="utf-8") + \
            (REPO / "alphabot" / "service.py").read_text(encoding="utf-8").split("# ── 期权结构跨日对比", 1)[1].split("# ── 接货 · 筛选", 1)[0]
        used = set(re.findall(r'"(demo|no_[a-z_]+|file_unreadable|ticker_not_scanned)"', src))
        assert used and used <= set(OC.REASONS), used - set(OC.REASONS)
        assert OC.reason_zh("file_unreadable:JSONDecodeError") == "这一天的扫描结果文件读不了（JSONDecodeError）"
        assert OC.reason_zh(None) is None
