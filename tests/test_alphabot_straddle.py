"""Alpha Bot「跨式账本」页（v0.45.424）的守卫。每组回答一句「谁会红」：

  · 只读：打遍两个接口前后，合成账本目录（跨式账本 + greeks 文件）指纹不变（`TestReadOnly`）；
  · 同一口径：逐仓 Greeks 与 `portfolio_greeks.option_exposures`、KPI 与 `options_paper_leg.compute_kpis`、
    浮动盈亏与 `options_paper_leg._unrealized` 拿同一组输入对照（`TestSameNumbersAsTheLedger`）；
  · 账目恒等式有牙：NAV ≠ 起始 + 已实现 + 浮动 时 `identity_ok=False`（`TestIdentity`）；
  · 盲期：信号行带 `gex_ctx` 时，接口返回里一个 `gex_ctx` 都没有；正对照证明输入里确实有（`TestBlinding`）；
  · 不编价：盘中报价拿不到时 mark 为空、不拿账本 mark 顶上；没持仓的票 400（`TestLive`）；
  · 缺目录不当空账本、陈旧要报（`TestHonestStates`）；帮助页登记 / 导航接线 / 常量取自服务端（`TestStraddleHelp`）。
全部离线：合成账本 + 合成链 + 注入报价函数。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from alphabot import straddle as SD
from alphabot import synthetic as syn
from alphabot.service import ET, AlphaBotService

AS_OF = "2026-10-06"
NOW = datetime(2026, 10, 6, 20, 0, tzinfo=ET)          # 当天 19:00 ET 之后 ⇒ 期望账本日 = 当天
STATIC = Path(__file__).resolve().parent.parent / "alphabot" / "static"


def _write_ledger(root: Path, led: dict, *, gex=True) -> Path:
    ld, gd = root / SD.LEDGER_DIRNAME, root / SD.GREEKS_DIRNAME
    ld.mkdir(parents=True)
    gd.mkdir(parents=True)
    (ld / "meta.json").write_text(json.dumps(led["meta"]), encoding="utf-8")
    signals = [dict(s) for s in led["signals"]]
    if gex:
        for s in signals:
            s["gex_ctx"] = {"schema_version": 1, "available": True, "regime": "positive_gex", "total_gex": 12.3,
                            "captured_on": s.get("as_of"), "as_of": s.get("as_of")}
    for name, rows in (("positions.jsonl", led["positions"]), ("closed_trades.jsonl", led["closed"]),
                       ("equity_curve.jsonl", led["equity"]), ("earnings_signals.jsonl", signals)):
        (ld / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (gd / f"greeks_{AS_OF}.json").write_text(json.dumps(led["greeks"]), encoding="utf-8")
    return root


@pytest.fixture
def root(tmp_path):
    return _write_ledger(tmp_path / "data", syn.demo_straddle_ledger(AS_OF))


def _svc(root, **kw):
    kw.setdefault("fetch_fn", syn.demo_fetch)
    kw.setdefault("straddle_quote_fn", lambda t, syms: syn.demo_quote_held(t, syms, AS_OF))
    return AlphaBotService(straddle_root=root, now_fn=lambda: NOW, **kw)


def _client(svc, port=8765):
    from starlette.testclient import TestClient
    from alphabot.server import create_app
    return TestClient(create_app(svc, port=port), base_url=f"http://127.0.0.1:{port}")


def _fingerprint(root: Path):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


class TestReadOnly:
    def test_both_endpoints_leave_the_ledger_untouched(self, root):
        before = _fingerprint(root)
        c = _client(_svc(root))
        assert c.get("/api/straddle").status_code == 200
        tickers = [p["ticker"] for p in c.get("/api/straddle").json()["positions"]]
        assert tickers, "合成账本里应当有持仓——否则下面的盘中接口没被打到"
        for t in tickers:
            r = c.get(f"/api/straddle/live/{t}?force=1")
            assert r.status_code == 200 and r.json()["available"] is True, r.text[:200]
        assert _fingerprint(root) == before

    def test_package_never_imports_the_ledger_writers(self):
        """跨式账本的写者是 options_paper_leg.run_for_date / earnings_vol_signal.scan / straddle_gex_prereg.run_once：
        Alpha Bot 包里一个都不许出现（只读页面）。"""
        import ast
        bad = {}
        for p in sorted((STATIC.parent).rglob("*.py")):
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                name = n.attr if isinstance(n, ast.Attribute) else (n.id if isinstance(n, ast.Name) else None)
                if name in {"run_for_date", "run_once", "scan", "settle_signals", "_write_jsonl", "_append_jsonl"}:
                    bad.setdefault(p.name, []).append((name, n.lineno))
        assert not bad, bad


class TestSameNumbersAsTheLedger:
    def test_position_greeks_match_portfolio_greeks(self):
        import portfolio_greeks as pg
        led = syn.demo_straddle_ledger(AS_OF)
        for p in led["positions"]:
            q = syn.demo_quote_held(p["ticker"], [p["call_symbol"], p["put_symbol"]], AS_OF)
            S = q["underlying_price"]
            rows = pg.option_exposures(AS_OF, positions=[p], quotes_fn=lambda t, syms, _q=q: _q["quotes"],
                                       beta_fn=lambda t, d: (1.0, "test"), closes_fn=lambda t, d, _S=S: _S)
            mine = SD.position_greeks([q["quotes"][p["call_symbol"]], q["quotes"][p["put_symbol"]]],
                                      p["side"], p["contracts"], S)
            assert mine["complete"], mine["missing"]
            for k in ("dollar_delta", "gamma_dollar_per_1pct", "vega_dollar_per_pt", "theta_dollar_per_day"):
                assert mine[k] == pytest.approx(sum(r[k] for r in rows), abs=0.02), (p["ticker"], k)

    def test_missing_leg_is_none_not_zero(self):
        g = SD.position_greeks([{"delta": 0.5, "gamma": 0.01, "vega": 0.2, "theta": -0.1}, None], "short", 2, 100.0)
        assert g["dollar_delta"] is None and g["complete"] is False and "put:no_quote" in g["missing"]
        g = SD.position_greeks([{"delta": 0.5, "gamma": 0.01, "vega": 0.2, "theta": -0.1}] * 2, "long", 2, None)
        assert g["dollar_delta"] is None and g["delta_shares"] == pytest.approx(200.0)

    def test_kpis_match_compute_kpis(self, tmp_path, monkeypatch):
        import options_paper_leg as opl
        led = syn.demo_straddle_ledger(AS_OF)
        for attr, name, rows in (("CLOSED_FILE", "c.jsonl", led["closed"]), ("POSITIONS_FILE", "p.jsonl", led["positions"]),
                                 ("EQUITY_FILE", "e.jsonl", led["equity"])):
            f = tmp_path / name
            f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            monkeypatch.setattr(opl, attr, f)
        ref, mine = opl.compute_kpis(), SD.kpis(led["closed"])
        for k in ("n", "win_rate", "avg_pnl_pct", "total_pnl_usd", "by_side", "by_label", "intrinsic_exits",
                  "written_off_exits"):
            assert mine[k] == ref[k], k

    @pytest.mark.parametrize("side", ["long", "short"])
    def test_unrealized_matches_ledger(self, side):
        import options_paper_leg as opl
        p = opl.StraddlePosition.from_record({**syn.demo_straddle_ledger(AS_OF)["positions"][0], "side": side})
        assert SD.unrealized(side, p.entry_premium, p.last_mark, p.contracts) == pytest.approx(opl._unrealized(p))


class TestIdentity:
    def test_consistent_ledger_passes(self, root):
        a = _svc(root).straddle()["account"]
        assert a["identity_ok"] is True and abs(a["identity_gap_usd"]) <= 1.0

    def test_has_teeth(self, tmp_path):
        led = syn.demo_straddle_ledger(AS_OF)
        led["equity"][-1]["nav"] += 500.0
        a = _svc(_write_ledger(tmp_path / "d", led)).straddle()["account"]
        assert a["identity_ok"] is False and a["identity_gap_usd"] == pytest.approx(500.0)


class TestBlinding:
    def test_no_gex_ctx_anywhere_in_the_responses(self, root):
        rows = (root / SD.LEDGER_DIRNAME / "earnings_signals.jsonl").read_text(encoding="utf-8")
        assert "gex_ctx" in rows, "正对照：输入里必须真的有 gex_ctx，否则「没漏」不说明任何事"
        c = _client(_svc(root))
        body = c.get("/api/straddle").json()
        assert not (SD.BLINDED_KEYS & set(_keys(body)))
        assert body["calibration"] and body["closed"] and body["positions"]
        t = body["positions"][0]["ticker"]
        assert not (SD.BLINDED_KEYS & set(_keys(c.get(f"/api/straddle/live/{t}").json())))

    def test_entry_fields_never_carry_gex(self):
        assert not (SD.BLINDED_KEYS & set(SD.ENTRY_FIELDS))


class TestLive:
    def test_unavailable_quote_is_not_papered_over(self, root):
        svc = _svc(root, straddle_quote_fn=lambda t, syms: {"available": False, "reason": "payload_unavailable",
                                                             "quotes": {s: None for s in syms}})
        t = svc.straddle()["positions"][0]["ticker"]
        r = svc.straddle_live(t)
        assert r["available"] is False and r["reason"] == "payload_unavailable"
        assert r["mark_mid"] is None and r["unrealized_mid_usd"] is None and r["exit_now_usd"] is None

    def test_quote_exception_is_reported(self, root):
        def boom(t, syms):
            raise TimeoutError("cboe")
        svc = _svc(root, straddle_quote_fn=boom)
        t = svc.straddle()["positions"][0]["ticker"]
        assert svc.straddle_live(t)["reason"] == "exception:TimeoutError"

    def test_exit_now_pays_the_spread(self, root):
        r = _svc(root).straddle_live(syn.demo_straddle_ledger(AS_OF)["positions"][0]["ticker"])
        assert r["quote_ok"] and r["exit_now_usd"] < r["unrealized_mid_usd"]   # 立即平仓永远不比 mid 好

    def test_unknown_ticker_is_400(self, root):
        c = _client(_svc(root))
        assert c.get("/api/straddle/live/ZZZZ").status_code == 400
        assert c.get("/api/straddle/live/bad$").status_code == 400

    def test_live_reads_only_positions(self, root, monkeypatch):
        """v0.45.428：盘中接口只要两个合约代码，不许每次重读整本账本（信号文件每天长 30 行）。"""
        svc = _svc(root)
        t = svc.straddle()["positions"][0]["ticker"]
        monkeypatch.setattr(SD, "load_ledger", lambda *a, **k: pytest.fail("盘中接口重读了整本账本"))
        assert svc.straddle_live(t)["available"] is True

    def test_auto_refresh_forces_and_matches_its_label(self):
        """v0.45.428：自动刷新不 force 就只拿到服务端 60 秒缓存（实际每 2 拍才新一次）；间隔与文案要对得上。"""
        import re
        js = (STATIC / "pages" / "straddle.js").read_text(encoding="utf-8")
        interval = re.search(r"setInterval\(\(\) => \{(.*?)\}, (\w+)\)", js, re.S)
        assert interval and "pullAll(true)" in interval.group(1), "自动刷新没有 force"
        ms = int(re.search(r"const AUTO_MS = (\d+);", js).group(1))
        assert interval.group(2) == "AUTO_MS" and ms >= 2 * 60_000 and "约每 2 分钟" in js
        # 只查代码、不查注释：注释里「每分钟强刷会……」是在讨论它，不是在对用户承诺它（本仓「文本探针」老坑）。
        # 用正则抠字符串字面量会跨两段模板字符串把注释框进去（首版就这样假红），按行去掉 // 注释更稳
        for name in ("straddle.js", "help.js"):
            code = "\n".join(ln for ln in (STATIC / "pages" / name).read_text(encoding="utf-8").splitlines()
                             if not ln.lstrip().startswith("//"))
            assert "每分钟" not in code, f"{name} 还在对用户说「每分钟」"
        assert "每分钟" in js, "正对照：注释里确实提到了它，上面的「不在代码里」才有意义"

    def test_cache_and_force(self, root):
        calls = []

        def q(t, syms):
            calls.append(t)
            return syn.demo_quote_held(t, syms, AS_OF)
        now = [100.0]
        svc = _svc(root, straddle_quote_fn=q, clock=lambda: now[0])
        t = svc.straddle()["positions"][0]["ticker"]
        svc.straddle_live(t); svc.straddle_live(t)
        assert calls == [t]
        svc.straddle_live(t, force=True)
        assert calls == [t, t]


class TestDemoDates:
    def test_demo_today_is_the_eastern_date(self, monkeypatch):
        """v0.45.428：演示链缺省日期按美东（跨式演示账本与报价都按美东造）；按本机（太平洋）日期时，
        每晚 21:00–24:00 PT 水平链与持仓合约的到期日差一天。"""
        from datetime import date
        monkeypatch.setattr(syn, "_today_et", lambda: date(2026, 10, 7))
        assert syn.demo_payload("NVDA")["as_of"] == "2026-10-07"
        assert syn.demo_bars("NVDA")[-1]["date"] <= "2026-10-07"
        assert syn.demo_straddle_ledger()["meta"]["last_run_date"] == "2026-10-07"


class TestHonestStates:
    def test_missing_dir_is_not_an_empty_ledger(self, tmp_path):
        d = _svc(tmp_path / "nope").straddle()
        assert d["data_available"] is False and d["state_dir"]["exists"] is False and "nope" in d["state_dir"]["path"]

    def test_stale_ledger_is_flagged(self, root):
        later = datetime(2026, 10, 8, 20, 0, tzinfo=ET)
        d = AlphaBotService(fetch_fn=syn.demo_fetch, straddle_root=root, now_fn=lambda: later).straddle()
        assert d["freshness"]["stale"] is True and d["freshness"]["expected_as_of"] == "2026-10-08"
        assert _svc(root).straddle()["freshness"]["stale"] is False

    def test_before_scan_time_expects_previous_session(self):
        assert SD.expected_ledger_date(datetime(2026, 10, 7, 12, 0, tzinfo=ET))[0] == "2026-10-06"
        assert SD.expected_ledger_date(datetime(2026, 10, 10, 12, 0, tzinfo=ET))[0] == "2026-10-09"   # 周六

    def test_missing_greeks_file_is_reported(self, root):
        (root / SD.GREEKS_DIRNAME / f"greeks_{AS_OF}.json").unlink()
        d = _svc(root).straddle()
        assert d["greeks_file"]["available"] is False and d["greeks_file"]["error"] == "missing"
        assert all(p["greeks"]["dollar_delta"] is None for p in d["positions"])


class TestStraddleHelp:
    def test_page_is_wired(self):
        assert 'href="#/straddle" data-nav="straddle"' in (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert 'from "./pages/straddle.js"' in app and 'page === "straddle"' in app
        assert 'helpLink("straddle")' in (STATIC / "pages" / "straddle.js").read_text(encoding="utf-8")

    def test_help_reads_rules_from_the_server(self):
        txt = (STATIC / "pages" / "help.js").read_text(encoding="utf-8")
        assert '["straddle", "' in txt
        for key in ("rich_ratio", "cheap_ratio", "max_spread_pct", "expiry_buffer_days", "risk_per_trade_pct", "max_open",
                    "directional_delta_warn", "min_units", "min_per_group", "min_informative_blocks"):
            assert key in txt, key

    def test_method_exposes_the_rules(self, root):
        m = _client(_svc(root)).get("/api/method").json()["straddle"]
        for k in ("rich_ratio", "cheap_ratio", "max_spread_pct", "max_open", "directional_delta_warn"):
            assert m.get(k) is not None, (k, m)
        assert m["prereg"]["min_units"] > 0
