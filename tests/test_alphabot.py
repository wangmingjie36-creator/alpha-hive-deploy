"""Alpha Bot（卖权选择器本机前端，v0.45.388）的守卫。

每组回答一句「谁会红」：
  · 曲线一致：图上的 gamma 曲线 / 期限视图子集就是路由读的那一份（`TestCurveIsTheRoutedCurve`）；
  · 盲期：冻结前任何接口都不吐结算字段 / 效应量键，历史水平整体锁住；正对照证明判据有牙（`TestBlinding`）；
  · 只读：打遍接口前后卖权账本目录指纹不变、不生成冻结文件（`TestReadOnlyLedger`）；
  · 火墙：只有 `alphabot/service.py` import 卖权模块，且全包不出现任何写路径 / 冻结调用（`TestFirewall`）；
  · 本机：只绑回环、Host 白名单、写请求要自定义头、CSP（`TestLocalOnly`）；
  · 盘中：只在交易时段、按关注列表拍，只存聚合、只给单日、失败有计数（`TestIntraday`）；
  · 前端资产：模块引用都存在、无内联脚本、不拼 innerHTML、不引外部脚本（`TestStaticAssets`）。
全部离线（合成链注入 `fetch_fn`；conftest `_offline_transport` 另挡传输层）。
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import sell_strike_candidates as C
import sell_strike_ledger as LG
import sell_strike_levels as L
import sell_strike_report as R
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService, IntradayPoller, ET, MAX_FOCUS
from hive_logger import PATHS

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "alphabot"
AS_OF = "2026-09-01"
TICKERS = ("NVDA", "JNJ", "TSLA", "AMC")


def _demo_service(**kw):
    kw.setdefault("fetch_fn", syn.demo_fetch)
    kw.setdefault("bars_fn", lambda t: {"available": True, "source": "demo", "bars": syn.demo_bars(t, AS_OF)})
    return AlphaBotService(**kw)


def _client(svc, port=8765):
    from starlette.testclient import TestClient
    from alphabot.server import create_app
    return TestClient(create_app(svc, port=port), base_url=f"http://127.0.0.1:{port}")


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_keys(v)


@pytest.fixture
def ledger(tmp_path):
    """合成账本：4 只票 × 两档，**已结算**（带到期收盘）——盲期检查要有东西可漏才有意义。"""
    d = tmp_path / "ledger"
    for tenor in LG.TENORS:
        rows = []
        for t in TICKERS:
            raw = syn.demo_payload(t, AS_OF)
            lm = L.level_map(raw["contracts"], raw["underlying_price"])
            r = LG.build_tenor_row(raw, lm, tenor, as_of=AS_OF, earnings_info={"earnings_date": None}, ticker=t)
            r.update({"settle_status": "settled", "expiry_close": raw["underlying_price"] * 0.97,
                      "expiry_close_date": r["expiry"], "expiry_close_source": "test", "settled_on": AS_OF})
            rows.append(r)
        LG._write_shard(LG._shard(tenor, AS_OF, d), rows)
    return d


def _fingerprint(root: Path):
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out[str(p.relative_to(root))] = (p.is_dir(), st.st_size, st.st_mtime_ns)
    return out


# ─────────────────────────────── 曲线一致

class TestCurveIsTheRoutedCurve:
    @pytest.mark.parametrize("ticker", TICKERS)
    @pytest.mark.parametrize("view", L.VIEW_NAMES)
    def test_curve_crossings_and_spot_total_match_the_sweep(self, ticker, view):
        raw = syn.demo_payload(ticker, AS_OF)
        S = raw["underlying_price"]
        lm = L.level_map(raw["contracts"], S)
        zg = lm["views"][view]["zero_gamma"]
        cv = L.gex_curve(L.view_contracts(raw["contracts"], view), S)
        assert cv["crossings"] == zg["crossings"]
        assert cv["total_at_spot"] == pytest.approx(zg["total_at_spot"])
        assert cv["n_contracts"] == zg["n_contracts"]
        assert len(cv["grid"]) == zg["grid_points"] and cv["grid"][0] == zg["grid_lo"]

    def test_curve_defaults_are_the_sweep_defaults(self):
        a = inspect.signature(L.gex_curve).parameters
        b = inspect.signature(L.zero_gamma_sweep).parameters
        assert a["band_pct"].default == b["band_pct"].default
        assert a["grid_points"].default == b["grid_points"].default

    def test_view_subsets_are_what_term_views_used(self):
        raw = syn.demo_payload("TSLA", AS_OF)
        views = L.term_views(raw["contracts"], raw["underlying_price"])
        for v in L.VIEW_NAMES:
            assert len(L.view_contracts(raw["contracts"], v)) == views[v]["n_contracts"]
        with pytest.raises(ValueError):
            L.view_contracts(raw["contracts"], "le_60dte")

    def test_empty_chain_draws_no_zero_line(self):
        cv = L.gex_curve([], 100.0)
        assert cv["grid"] and cv["total"] == [] and cv["total_at_spot"] is None

    def test_detail_reuses_structure_quote_and_pnl(self):
        d = R.compute_live_detail("NVDA", fetch_fn=syn.demo_fetch)
        assert d["data_available"]
        row = d["tenors"]["monthly"]
        for side in LG.SIDES:
            for k, q in row["ladder_quotes"][side].items():
                ref = C.structure_quote(row["ladder"], f"short_{side}", float(k))
                assert q["yield_raw"] == ref["yield_raw"] and q["quotable"] == ref["quotable"]
        pf = d["payoff"]["monthly"]
        q = C.structure_quote(row["ladder"], "short_put", C.BASE_RUNG)
        assert pf["base"]["short_put"]["pnl"][0] == C.structure_pnl_at_expiry(q, pf["grid"][0])
        assert d["views"]["le_45dte"]["curve"]["crossings"] == d["views"]["le_45dte"]["zero_gamma"]["crossings"]

    def test_unavailable_is_reported_not_raised(self):
        d = R.compute_live_detail("NVDA", fetch_fn=lambda t, as_of=None: (None, "payload_unavailable"))
        assert d == {**d, "data_available": False, "reason": "payload_unavailable"}


# ─────────────────────────────── 盲期

_LEAK_KEYS = set(R.BLINDED_ROW_FIELDS) | set(LG.BLINDED_KEYS)


class TestBlinding:
    def test_nothing_leaks_before_all_tenors_are_frozen(self, ledger):
        svc = _demo_service(ledger_dir=ledger, demo=False)
        c = _client(svc)
        urls = (f"/api/overview?date={AS_OF}", f"/api/ledger/rows?date={AS_OF}", f"/api/ledger/rows?date={AS_OF}&full=1",
                f"/api/ledger/ticker/NVDA?date={AS_OF}", "/api/assess", "/api/history/NVDA")
        for u in urls:
            body = c.get(u).json()
            leaked = _LEAK_KEYS & set(_all_keys(body))
            assert not leaked, f"{u} 在盲期吐出了 {sorted(leaked)}"
        rows = c.get(f"/api/ledger/rows?date={AS_OF}").json()
        assert rows["settlement_blinded"] is True and len(rows["tenors"]["monthly"]) == len(TICKERS)
        assert c.get("/api/history/NVDA").json()["locked"] is True

    def test_positive_control_fields_appear_once_all_frozen(self, ledger, monkeypatch):
        """有牙自证：同一个账本，判据改成「全部冻结」后结算字段与历史水平就出来了——上一条的「没漏」不是因为本来就没有。"""
        monkeypatch.setattr(R, "_all_unblinded", lambda assessed: True)
        c = _client(_demo_service(ledger_dir=ledger, demo=False))
        rows = c.get(f"/api/ledger/rows?date={AS_OF}").json()
        assert rows["settlement_blinded"] is False
        assert rows["tenors"]["monthly"][0]["expiry_close"] is not None
        hist = c.get("/api/history/NVDA").json()
        assert hist["locked"] is False and hist["series"]["monthly"]

    def test_intraday_is_single_day_only(self):
        sig = inspect.signature(AlphaBotService.intraday).parameters
        assert list(sig) == ["self", "ticker", "date_et"], "盘中接口只收一个日期：跨日拼接 = 当日路由叠其后价格"


# ─────────────────────────────── 只读

class TestReadOnlyLedger:
    def test_every_endpoint_leaves_the_ledger_untouched(self, ledger):
        before = _fingerprint(ledger)
        c = _client(_demo_service(ledger_dir=ledger, demo=False))
        for u in ("/api/meta", "/api/live/NVDA", "/api/live/NVDA?force=1", "/api/overview", f"/api/overview?date={AS_OF}",
                  "/api/ledger/dates", f"/api/ledger/rows?date={AS_OF}&full=1", f"/api/ledger/ticker/JNJ?date={AS_OF}",
                  "/api/assess", "/api/history/NVDA", "/api/bars/NVDA", "/api/method", "/api/intraday/NVDA",
                  "/api/intraday-dates/NVDA", "/api/settings"):
            r = c.get(u)
            assert r.status_code == 200, (u, r.text[:200])
        assert c.put("/api/settings", json={"favorites": ["SPY"]}, headers={"X-AlphaBot": "1"}).status_code == 200
        assert _fingerprint(ledger) == before
        assert not list(ledger.rglob(LG.PREREG_RESULT_NAME))

    def test_own_state_goes_to_paths_alphabot_state(self, tmp_path):
        svc = _demo_service()
        svc.update_settings({"focus": ["NVDA"]})
        assert svc.state_dir() == PATHS.alphabot_state
        assert (PATHS.alphabot_state / "settings.json").is_file()
        assert PATHS.alphabot_state.is_relative_to(tmp_path)

    def test_overview_reads_latest_ledger_date(self, ledger):
        d = _demo_service(ledger_dir=ledger, demo=False).overview()
        assert d["as_of"] == AS_OF and d["source"] == "ledger"
        r = d["tenors"]["monthly"][0]
        assert r["base_legs"]["put"]["rung"] == C.BASE_RUNG and "ladder" not in r

    def test_missing_ledger_is_not_reported_as_empty(self, tmp_path):
        d = _demo_service(ledger_dir=tmp_path / "nope", demo=False).overview()
        assert d["data_available"] is False and d["state_dir"]["exists"] is False


# ─────────────────────────────── 火墙

_WRITE_NAMES = {"record_rows", "_record_rows", "settle", "_settle", "run_for_date", "write_local_report",
                "_freeze_prereg_result", "_write_shard"}


def _py_files():
    return sorted(PKG.rglob("*.py"))


class TestFirewall:
    def test_only_service_imports_sell_strike(self):
        hits = {}
        for p in _py_files():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                names = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                    ([n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                if any("sell_strike_" in x for x in names):
                    hits.setdefault(p.relative_to(REPO).as_posix(), []).append(n.lineno)
        assert set(hits) == {"alphabot/service.py"}, hits

    @staticmethod
    def _violations(src: str):
        bad = []
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Attribute) and n.attr in _WRITE_NAMES:
                bad.append(n.attr)
            if isinstance(n, ast.Name) and n.id in _WRITE_NAMES:
                bad.append(n.id)
            if isinstance(n, ast.keyword) and n.arg == "freeze" and not (
                    isinstance(n.value, ast.Constant) and n.value.value is False):
                bad.append("freeze=")
        return bad

    def test_package_never_touches_a_ledger_write_path(self):
        bad = {p.name: self._violations(p.read_text(encoding="utf-8")) for p in _py_files()}
        assert not {k: v for k, v in bad.items() if v}, bad

    @pytest.mark.parametrize("src", ["R.LG.record_rows(d, t, rows)", "LG.settle(d, t)",
                                     "R.write_local_report(d, freeze=True)", "R.LG.assess('monthly', freeze=flag)"])
    def test_guard_has_teeth(self, src):
        assert self._violations(src)

    def test_no_forbidden_tokens(self):
        # 双向白名单（test_sell_strike_integration）按令牌数：账本目录名与原始取数函数名不许出现在本包
        for p in list(_py_files()) + sorted((PKG / "static").rglob("*.js")):
            text = p.read_text(encoding="utf-8")
            assert "sell_strike_state" not in text and "fetch_cboe_raw_contracts" not in text, p


# ─────────────────────────────── 本机

class TestLocalOnly:
    @pytest.mark.parametrize("host,ok", [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                         ("0.0.0.0", False), ("192.168.1.5", False), ("example.com", False)])
    def test_only_loopback(self, host, ok):
        from alphabot.__main__ import _is_loopback
        assert _is_loopback(host) is ok

    def test_cli_refuses_non_loopback(self, capsys):
        from alphabot.__main__ import main
        assert main(["--host", "0.0.0.0"]) == 2
        assert "拒绝" in capsys.readouterr().err

    def test_host_header_write_header_preflight_and_csp(self):
        c = _client(_demo_service())
        r = c.get("/api/method")
        assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]
        assert c.get("/api/method", headers={"host": "evil.example:8765"}).status_code == 403
        assert c.put("/api/settings", json={"favorites": []}).status_code == 403
        assert c.options("/api/settings").status_code == 405
        assert c.put("/api/settings", json={"favorites": []}, headers={"X-AlphaBot": "1"}).status_code == 200

    def test_bad_inputs_are_400(self):
        c = _client(_demo_service())
        assert c.get("/api/live/bad$").status_code == 400
        assert c.get("/api/ledger/rows?date=2026-13-01").status_code == 400
        too_many = [f"T{i}" for i in range(MAX_FOCUS + 1)]
        r = c.put("/api/settings", json={"focus": too_many}, headers={"X-AlphaBot": "1"})
        assert r.status_code == 400 and str(MAX_FOCUS) in r.json()["error"]

    def test_live_cache_and_single_flight(self):
        calls = []

        def fetch(t, as_of=None):
            calls.append(t)
            return syn.demo_payload(t, AS_OF), None
        now = [1000.0]
        svc = _demo_service(fetch_fn=fetch, clock=lambda: now[0])
        svc.live("NVDA"); svc.live("NVDA")
        assert calls == ["NVDA"]
        now[0] += 61
        svc.live("NVDA")
        assert calls == ["NVDA", "NVDA"]
        svc.live("NVDA", force=True)
        assert len(calls) == 3

    def test_json_has_no_nan(self):
        from alphabot.service import clean_json
        assert json.dumps(clean_json({"a": float("nan"), "b": [float("inf"), 1.0]}), allow_nan=False) == \
            '{"a": null, "b": [null, 1.0]}'


# ─────────────────────────────── 盘中

def _live_session(now="2026-09-30T11:00:00-04:00"):
    return lambda: {"live": True, "now_et": now}


class TestIntraday:
    def test_snapshot_is_aggregate_and_deduplicated(self):
        svc = _demo_service()
        now = datetime(2026, 9, 30, 11, 0, tzinfo=ET)
        assert svc.take_snapshot("NVDA", now=now)["recorded"] is True
        assert svc.take_snapshot("NVDA", now=now)["reason"] == "payload_unchanged"
        day = svc.intraday("NVDA", "2026-09-30")
        assert len(day["snapshots"]) == 1
        snap = day["snapshots"][0]
        assert "contracts" not in json.dumps(snap) and "ladder" not in snap
        assert snap["views"]["le_45dte"]["strikes"], "逐行权价净 GEX 要在（热力图 / 回看靠它）"
        f = PATHS.alphabot_state / "intraday" / "2026-09-30" / "NVDA.jsonl"
        assert f.is_file()

    def test_poller_only_runs_in_session_and_when_due(self):
        svc = _demo_service()
        svc.update_settings({"focus": ["NVDA", "JNJ"], "interval_min": 10})
        closed = IntradayPoller(svc, session_fn=lambda: {"live": False})
        assert closed.tick(now_mono=0) is None
        p = IntradayPoller(svc, session_fn=_live_session())
        res = p.tick(now_mono=0)
        assert [r["ticker"] for r in res] == ["NVDA", "JNJ"]
        assert p.tick(now_mono=300) is None                    # 没到 10 分钟
        assert p.tick(now_mono=601) is not None
        svc.update_settings({"poll_enabled": False})
        assert p.tick(now_mono=5000) is None

    def test_poller_failures_are_counted_and_logged(self, caplog):
        svc = _demo_service(fetch_fn=lambda t, as_of=None: (None, "payload_unavailable"))
        svc.update_settings({"focus": ["NVDA"]})
        p = IntradayPoller(svc, session_fn=_live_session())
        with caplog.at_level("WARNING"):
            res = p.tick(now_mono=0)
        assert res == [{"ticker": "NVDA", "recorded": False, "reason": "payload_unavailable"}]
        assert "未记录" in caplog.text
        assert p.status["last_results"] == res

    def test_demo_intraday_needs_no_state(self, tmp_path):
        svc = _demo_service(demo=True, state_dir=tmp_path / "demo")
        d = svc.intraday("NVDA")
        assert len(d["snapshots"]) == 40 and not (tmp_path / "demo").exists()


# ─────────────────────────────── 前端资产

STATIC = PKG / "static"


class TestStaticAssets:
    def test_module_imports_resolve(self):
        for js in sorted(STATIC.rglob("*.js")):
            if "vendor" in js.parts:
                continue
            for m in re.finditer(r'from "(\.[^"]+)"', js.read_text(encoding="utf-8")):
                assert (js.parent / m.group(1)).resolve().is_file(), (js, m.group(1))

    def test_index_references_exist_and_no_inline_script(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="/static/([^"]+)"', html):
            assert (STATIC / ref).is_file(), ref
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "CSP 是 script-src 'self'：不许内联脚本"
        assert not re.findall(r'<script[^>]+src="https?://', html), "脚本只许本源（本机工具要能离线用）"

    def test_no_innerhtml(self):
        for js in sorted(STATIC.rglob("*.js")):
            if "vendor" not in js.parts:
                assert not re.search(r"\.(inner|outer)HTML\s*=|insertAdjacentHTML|document\.write",
                                     js.read_text(encoding="utf-8")), js

    def test_vendored_echarts_is_pinned(self):
        import hashlib
        data = (STATIC / "vendor" / "echarts.min.js").read_bytes()
        assert hashlib.sha256(data).hexdigest() == "e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf"
        assert (STATIC / "vendor" / "ECHARTS_LICENSE").is_file()

    def test_not_deployable(self):
        import report_deployer as rd
        for p in ("alphabot/static/index.html", "alphabot_state/settings.json",
                  "alphabot_state/intraday/2026-09-30/NVDA.jsonl"):
            assert not rd._is_report_artifact(p), p
