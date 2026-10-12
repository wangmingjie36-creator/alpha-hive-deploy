"""Alpha Bot「对冲」页（v0.45.459）的守卫。每组回答一句「谁会红」：

  · 数据只经账本属主的 `portfolio_greeks.ledger_view`，历史只放行能自证定价场次的记录；被拦下的只给计数与日期
    范围、不出现在任何返回值里（`TestHistoryGoesThroughTheGate`，用与生产同形的记录造夹具）；
  · 账户口径：净值 = 现金 + 股数 × 标记价，且与同一天净值行核对——对不上要报，有牙（`TestAccount`）；
  · 读不了 ≠ 空仓：持仓文件坏了如实报错、不出数；缺目录不当空账本；陈旧要报（`TestHonestStates`）；
  · 只读：打接口前后夹具目录指纹不变（`TestReadOnly`）；演示模式不碰磁盘且恒等式成立（`TestDemo`）；
  · 导航 / 路由 / 帮助页接线、规则常量取自服务端（`TestWiring`）。
全部离线。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import portfolio_greeks as pg
from alphabot import hedge as HG
from alphabot import synthetic as syn
from alphabot.service import ET, AlphaBotService

NOW = datetime(2026, 10, 9, 20, 0, tzinfo=ET)            # 当天 19:00 ET 之后 ⇒ 期望账本日 = 当天
STATIC = Path(__file__).resolve().parent.parent / "alphabot" / "static"
DIRNAME = "hedge_state"

# 与生产同形（取自 2026-10-08 / 10-09 的真实记录）：新格式带定价证明；旧格式没有（会被历史闸拦下）
OLD_EQUITY = {"date": "2026-10-07", "spy_shares": 48.0, "spy_price": 770.1, "cash": -36630.13, "market_value": 36964.8,
              "nav": 334.67, "trades_today": 0, "band_status": "inside"}
EQUITY = [
    {"date": "2026-10-08", "spy_shares": 48.0, "spy_price": 773.93, "spy_price_source": "cboe_close",
     "spy_price_session": "2026-10-08", "cash": -36630.13, "market_value": 37148.64, "nav": 518.51,
     "trades_today": 0, "band_status": "inside"},
    {"date": "2026-10-09", "spy_shares": 44.0, "spy_price": 778.57, "spy_price_source": "cboe_close",
     "spy_price_session": "2026-10-09", "spy_price_at_close": True, "cash": -33515.85, "market_value": 34257.08,
     "nav": 741.23, "trades_today": 1, "band_status": "above"},
]
OLD_TRADE = {"date": "2026-10-01", "ticker": "SPY", "shares": 6, "price": 760.0, "action": "buy_spy"}
TRADE = {"date": "2026-10-09", "ticker": "SPY", "shares": -4, "price": 778.57, "action": "sell_spy", "fill": "close",
         "excess_usd": 2697.33, "target_usd": 21545.4, "beta_dd_usd_before": 24242.73, "beta_dd_pct_before": 16.8779,
         "nav": 143635.99, "shares_after": 44.0, "reason": "β·Δ +16.88% NAV above band → rebalance to edge",
         "price_session": "2026-10-09", "price_source": "cboe_close", "price_at_close": True}
POSITIONS = [{"ticker": "SPY", "shares": 44.0, "avg_price": 763.7475, "entry_date": "2026-09-04",
              "last_price": 778.57, "last_mark_date": "2026-10-09"}]
META = {"version": "0.45.423", "starting_date": "2026-09-04", "cash": -33515.8498, "last_run_date": "2026-10-09"}


def _audit(day, *, pct, after=None, executed=False, provenance=True):
    a = {"as_of": day, "nav": 143635.99, "spy_price": 778.57, "spy_mark": {"source": "cboe_close", "session": day},
         "aggregate": {"band": {"band_pct": 15.0}, "band_status": "above" if pct > 15 else "inside",
                       "pct_nav": {"beta_dollar_delta": pct, "dollar_delta": 138.2}, "coverage": {"n_rows": 24},
                       "partial": False, "nav": 143635.99},
         "aggregate_after": {"pct_nav": {"beta_dollar_delta": after if after is not None else pct},
                             "band_status": "inside"},
         "recommendation": {"action": "sell_spy" if executed else "hold", "spy_shares": -4 if executed else 0},
         "executed": executed, "executed_trade": TRADE if executed else None,
         "rows": [{"kind": "stock", "ticker": "NVDA", "price_source": "cboe_close"}]}
    if provenance:
        a["price_check"] = {"as_of": day, "n_stale": 0, "gaps": [], "spy": {"live": False}}
    return a


def _write(root: Path) -> Path:
    d = root / DIRNAME
    d.mkdir(parents=True)
    (d / "positions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in POSITIONS), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (d / "equity_curve.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [OLD_EQUITY, *EQUITY]), encoding="utf-8")
    (d / "trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [OLD_TRADE, TRADE]), encoding="utf-8")
    (d / "greeks_2026-10-07.json").write_text(json.dumps(_audit("2026-10-07", pct=9.0, provenance=False)), encoding="utf-8")
    (d / "greeks_2026-10-08.json").write_text(json.dumps(_audit("2026-10-08", pct=10.873)), encoding="utf-8")
    (d / "greeks_2026-10-09.json").write_text(json.dumps(_audit("2026-10-09", pct=16.8779, after=14.7097, executed=True)),
                                              encoding="utf-8")
    return root


@pytest.fixture
def root(tmp_path):
    return _write(tmp_path / "data")


def _svc(root, now=NOW, **kw):
    return AlphaBotService(fetch_fn=syn.demo_fetch, straddle_root=root, now_fn=lambda: now, **kw)


def _client(svc, port=8765):
    from starlette.testclient import TestClient
    from alphabot.server import create_app
    return TestClient(create_app(svc, port=port), base_url=f"http://127.0.0.1:{port}")


def _fingerprint(root: Path):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


class TestHistoryGoesThroughTheGate:
    def test_only_records_that_prove_their_session_are_shown(self, root):
        d = _svc(root).hedge()
        assert [e["date"] for e in d["equity"]] == ["2026-10-08", "2026-10-09"]
        assert [t["date"] for t in d["trades"]] == ["2026-10-09"]
        assert [x["date"] for x in d["days"]] == ["2026-10-08", "2026-10-09"]
        g = d["gate"]
        assert g["excluded_total"] == {"trades": 1, "equity": 1, "audits": 1}
        assert g["excluded_range"]["equity"] == ["2026-10-07", "2026-10-07"]
        assert g["excluded"]["audits"] == {"no_provenance": 1}

    def test_blocked_records_appear_nowhere_in_the_response(self, root):
        """正对照：夹具里确实有旧记录（10-07 净值 / 10-01 成交）；返回值里除了闸的计数与日期范围，一处都不许有。"""
        assert "2026-10-01" in (root / DIRNAME / "trades.jsonl").read_text(encoding="utf-8")
        body = json.dumps({k: v for k, v in _svc(root).hedge().items() if k != "gate"})
        assert "2026-10-01" not in body and "2026-10-07" not in body and "334.67" not in body

    def test_root_none_resolves_the_data_root_at_call_time(self, root, monkeypatch):
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(root))
        v = pg.ledger_view()
        assert v["exists"] and Path(v["state_dir"]) == root / DIRNAME

    def test_page_module_does_not_touch_ledger_files(self):
        """alphabot/hedge.py 不读文件、不碰账本文件名：数据只经 ledger_view（读历史的守卫另在 test_hedge_history_gate）。"""
        import ast
        tree = ast.parse((STATIC.parent / "hedge.py").read_text(encoding="utf-8"))
        names = {n.attr if isinstance(n, ast.Attribute) else n.id
                 for n in ast.walk(tree) if isinstance(n, (ast.Attribute, ast.Name))}
        imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                   for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        bad = names & {"open", "read_text", "read_bytes", "load_jsonl", "load_json", "load_history", "glob", "iterdir"}
        assert not bad, bad
        assert "portfolio_greeks" not in imports and "ledger_io" not in imports, imports


class TestAccount:
    def test_numbers_and_identity(self, root):
        a = _svc(root).hedge()["account"]
        assert a["shares"] == 44.0 and a["mark"] == 778.57 and a["mark_date"] == "2026-10-09"
        assert a["market_value"] == pytest.approx(44 * 778.57)
        assert a["nav"] == pytest.approx(-33515.8498 + 44 * 778.57)
        assert a["unrealized_usd"] == pytest.approx((778.57 - 763.7475) * 44)
        assert a["realized_usd"] == pytest.approx(a["nav"] - a["unrealized_usd"])
        assert a["identity_ok"] is True and a["identity_checked_against"] == "2026-10-09"

    def test_identity_has_teeth(self, root):
        p = root / DIRNAME / "meta.json"
        p.write_text(json.dumps({**META, "cash": META["cash"] + 100.0}), encoding="utf-8")
        a = _svc(root).hedge()["account"]
        assert a["identity_ok"] is False and a["identity_gap_usd"] == pytest.approx(100.0, abs=0.01)

    def test_flat_book_is_zero_not_missing(self):
        a = HG.account([], {"cash": 12.5}, [])
        assert a["shares"] == 0.0 and a["market_value"] == 0.0 and a["nav"] == 12.5 and a["identity_ok"] is None

    def test_latest_day(self, root):
        d = _svc(root).hedge()
        L = d["latest"]
        assert L["date"] == "2026-10-09" and L["executed"] is True and L["executed_shares"] == -4
        assert L["beta_dd_pct"] == pytest.approx(16.8779) and L["beta_dd_pct_after"] == pytest.approx(14.7097)
        assert d["latest_is_as_of"] is True
        assert "NVDA" not in json.dumps(L), "逐行明细（审计的 rows）不给页面"


class TestHonestStates:
    def test_unreadable_positions_is_an_error_not_a_flat_book(self, root):
        (root / DIRNAME / "positions.jsonl").write_text('{"ticker": "SPY", "shares": 44', encoding="utf-8")
        d = _svc(root).hedge()
        assert d["data_available"] is True and "positions" in d["errors"] and "LedgerCorrupt" in d["errors"]["positions"]
        assert d["account"]["shares"] is None and d["account"]["nav"] is None, "读不了不许当成 0 股"

    def test_missing_dir_is_not_an_empty_ledger(self, tmp_path):
        d = _svc(tmp_path / "nope").hedge()
        assert d["data_available"] is False and d["state_dir"]["exists"] is False

    def test_stale_ledger_is_flagged(self, root):
        later = datetime(2026, 10, 13, 20, 0, tzinfo=ET)
        assert _svc(root, now=later).hedge()["freshness"]["stale"] is True
        assert _svc(root).hedge()["freshness"]["stale"] is False

    def test_missing_latest_audit_is_reported(self, root):
        (root / DIRNAME / "greeks_2026-10-09.json").unlink()
        d = _svc(root).hedge()
        assert d["latest"]["date"] == "2026-10-08" and d["latest_is_as_of"] is False


class TestReadOnly:
    def test_endpoint_leaves_the_ledger_untouched(self, root):
        before = _fingerprint(root)
        c = _client(_svc(root))
        r = c.get("/api/hedge")
        assert r.status_code == 200 and r.json()["data_available"] is True
        assert _fingerprint(root) == before


class TestDemo:
    def test_demo_needs_no_disk_and_balances(self, tmp_path):
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, straddle_root=tmp_path / "nope", now_fn=lambda: NOW)
        d = svc.hedge()
        assert d["source"] == "demo" and d["data_available"] is True
        assert d["account"]["identity_ok"] is True
        assert len(d["trades"]) >= 2 and {t["action"] for t in d["trades"]} == {"sell_spy", "buy_spy"}
        assert any(x["executed"] for x in d["days"]) and d["gate"]["excluded_total"]["equity"] == 3
        assert not (tmp_path / "nope").exists()


class TestWiring:
    def test_page_is_wired(self):
        assert 'href="#/hedge" data-nav="hedge"' in (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert 'from "./pages/hedge.js"' in app and 'page === "hedge"' in app
        assert 'helpLink("hedge")' in (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")

    def test_help_reads_rules_from_the_server(self):
        txt = (STATIC / "pages" / "help.js").read_text(encoding="utf-8")
        assert '["hedge", "' in txt
        for key in ("beta_delta_band_pct", "hedge_instrument", "rebalance_to", "beta_window", "vega_alert_pct",
                    "gamma_alert_pct", "measurement_start"):
            assert f"r.{key}" in txt, key

    def test_method_exposes_the_rules(self, root):
        m = _client(_svc(root)).get("/api/method").json()["hedge"]
        assert m["beta_delta_band_pct"] == pg.CONFIG["beta_delta_band_pct"]
        assert m["measurement_start"] == pg.MEASUREMENT_START
        for k in pg.PUBLIC_RULE_KEYS:
            assert k in m, k


class TestSecondCheck:
    """v0.45.459 二次检查（独立审阅 + 浏览器实看异常状态）补的守卫。"""

    def test_never_opened_ledger_says_so_instead_of_zero_shares(self, tmp_path):
        (tmp_path / "data" / DIRNAME).mkdir(parents=True)
        d = _svc(tmp_path / "data").hedge()
        assert d["data_available"] is True and d["opened"] is False and not d["errors"]
        assert d["account"]["shares"] is None, "没开过账不是「0 股」"
        assert d["account"]["identity_reason"] == "账本还没有记录"
        assert d["freshness"]["stale"] is None and d["latest_is_as_of"] is None

    def test_missing_positions_after_opening_is_an_error(self, root):
        (root / DIRNAME / "positions.jsonl").unlink()
        d = _svc(root).hedge()
        assert "positions" in d["errors"] and "不存在" in d["errors"]["positions"]
        assert d["account"]["shares"] is None and d["account"]["identity_reason"] == "持仓文件读不了"

    def test_flat_book_file_present_is_zero_shares(self, root):
        (root / DIRNAME / "positions.jsonl").write_text("", encoding="utf-8")
        d = _svc(root).hedge()
        assert not d["errors"] and d["account"]["shares"] == 0.0

    def test_unreadable_meta_makes_date_unknown_not_null_text(self, root):
        (root / DIRNAME / "meta.json").write_text('{"cash": NaN, "last_run_date": "2026-10-09"}', encoding="utf-8")
        d = _svc(root).hedge()
        assert "meta" in d["errors"] and d["as_of"] is None
        assert d["latest_is_as_of"] is None and d["freshness"]["stale"] is None, "日期不可得 ⇒ 判断不了，不是 False"
        assert d["account"]["identity_reason"] == "meta.json 读不了"
        js = (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")
        assert "d.latest_is_as_of === false" in js, "前端只在明确为 false 时报（null 不能被当成 false 印出「最后一天 null」）"

    def test_old_mark_is_flagged(self, root):
        p = root / DIRNAME / "positions.jsonl"
        p.write_text(json.dumps({**POSITIONS[0], "last_mark_date": "2026-10-08", "last_price": 773.93}) + "\n",
                     encoding="utf-8")
        a = _svc(root).hedge()["account"]
        assert a["mark_stale"] is True and a["mark_date"] == "2026-10-08"
        assert _svc(_write(Path(str(root) + "_b"))).hedge()["account"]["mark_stale"] is False

    def test_no_dead_error_field_from_audits(self, root):
        """审计文件的 price_check 没有 error（异常中断的那一轮不写审计）：页面不再读一个永远不存在的字段。"""
        assert "error" not in _svc(root).hedge()["latest"]["price_check"]
        assert "pc.error" not in (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")

    def test_price_source_labels_match_the_writer(self):
        """标签键必须是 _default_mark 真会写的来源名（twelve_data_bar，不是 twelve_data_close）。"""
        import inspect
        js = (STATIC / "pages" / "hedge.js").read_text(encoding="utf-8")
        src = inspect.getsource(pg._default_mark)
        assert '"twelve_data_bar"' in src and "twelve_data_bar:" in js and "twelve_data_close:" not in js
        assert "cboe_close:" in js and "cboe_intraday:" in js

    def test_static_assets_are_revalidated_not_heuristically_cached(self, root):
        """升级后新 index.html + 旧缓存 app.js ⇒ 新页面「没有这个页面」（浏览器里实际复现过）。/static/ 必须 no-cache。"""
        c = _client(_svc(root))
        for u in ("/static/app.js", "/static/pages/hedge.js", "/static/alphabot.css"):
            r = c.get(u)
            assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache", (u, r.headers.get("cache-control"))
        assert c.get("/").headers.get("cache-control") == "no-store"
        assert c.get("/api/hedge").status_code == 200
