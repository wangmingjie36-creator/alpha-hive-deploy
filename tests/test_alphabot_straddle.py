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


def _straddle_js() -> str:
    """straddle.js 去掉 // 注释行（注释里讨论某句话不等于对用户说了它——本仓「文本探针」老坑）。"""
    return "\n".join(ln for ln in (STATIC / "pages" / "straddle.js").read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("//"))


def _fn_src(js: str, name: str) -> str:
    """JS 里 `function name` / `async function name` 的函数体源码（按大括号配对；模板串里的 ${} 是配对的）。"""
    import re
    m = re.search(rf"(?:async\s+)?function {name}\b", js)
    assert m, f"找不到函数 {name}"
    i = js.index("{", js.index(")", m.end()))                                             # 参数表之后的第一个 {
    depth = 0
    for j in range(i, len(js)):
        depth += (js[j] == "{") - (js[j] == "}")
        if depth == 0:
            return js[m.start():j + 1]
    raise AssertionError(f"{name} 的大括号没配上")


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
                  "written_off_exits", "event_split"):
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


#: 2026-10-21 财报日实测的 T 24.50 call 腿：股价跌到 22.4 后变深度虚值，bid=0、ask 真实、IV 与 Greeks 正常（v0.45.581）
BID_ZERO_CALL = {"bid": 0.0, "ask": 0.02, "mid": None, "spread_pct": None, "iv": 0.2955, "delta": 0.0511,
                 "gamma": 0.0842, "vega": 0.0038, "theta": -0.0065, "quote_ok": False}


def _book(side):
    return next(p for p in syn.demo_straddle_ledger(AS_OF)["positions"] if p["side"] == side)


def _live_with(root, p, *, call=None, put=None, drop=()):
    """演示报价上改写某条腿再走一遍真实的 `straddle_live`（服务层 + live_view）。返回 (结果, 未改写的报价)。
    `drop=("put",)` = 那条腿在 payload 里根本没有（quotes[符号] = None）。"""
    base = syn.demo_quote_held(p["ticker"], [p["call_symbol"], p["put_symbol"]], AS_OF)["quotes"]

    def fn(t, syms):
        q = syn.demo_quote_held(t, syms, AS_OF)
        for name, sym, over in (("call", p["call_symbol"], call), ("put", p["put_symbol"], put)):
            if over is not None:
                q["quotes"][sym] = {**q["quotes"][sym], **over}
            if name in drop:
                q["quotes"][sym] = None
        return q
    return _svc(root, straddle_quote_fn=fn).straddle_live(p["ticker"], force=True), base


class TestLiveBidZeroLeg:
    """v0.45.581：某条腿 bid=0 时，mid 仍不给；卖跨式的立即平仓价按 ask 给；Greeks 在 IV 正常时照用。
    每条「放行」都配一条「不放行」的对照——否则规则放宽成「什么都放」时这组测试不会红。"""

    def test_short_exit_now_uses_ask_and_mid_stays_empty(self, root):
        p = _book("short")
        r, base = _live_with(root, p, call=BID_ZERO_CALL)
        put_ask = base[p["put_symbol"]]["ask"]
        assert r["quote_ok"] is False and r["mark_mid"] is None and r["unrealized_mid_usd"] is None
        assert r["exit_now_basis"] == "ask_only" and r["bid_zero_legs"] == ["call"]
        assert r["exit_now_premium"] == pytest.approx(0.02 + put_ask)
        # 卖跨式盈亏 = (入场权利金 − 买回价) × 100 × 张数，手算，不借被测函数
        assert r["exit_now_usd"] == pytest.approx((p["entry_premium"] - (0.02 + put_ask)) * 100.0 * p["contracts"])

    def test_greeks_keep_the_bid_zero_leg_and_say_so(self, root):
        p = _book("short")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL)
        g = r["greeks"]
        assert g["complete"] is True and g["missing"] == [] and g["degraded_legs"] == ["call"]
        assert all(g[k] is not None for k in ("dollar_delta", "gamma_dollar_per_1pct", "theta_dollar_per_day", "vega_dollar_per_pt"))

    def test_long_exit_still_needs_a_bid(self, root):
        """买跨式按 bid 卖出：bid=0 即无买盘，立即平仓价仍不给；Greeks 与方向无关，照用。"""
        p = _book("long")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL)
        assert r["exit_now_premium"] is None and r["exit_now_usd"] is None and r["exit_now_basis"] is None
        assert r["bid_zero_legs"] == ["call"] and r["greeks"]["degraded_legs"] == ["call"]

    def test_both_legs_bid_zero_short_still_exits_on_asks(self, root):
        p = _book("short")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL, put={**BID_ZERO_CALL, "ask": 0.05})
        assert r["bid_zero_legs"] == ["call", "put"] and r["exit_now_premium"] == pytest.approx(0.07)

    def test_normal_quotes_are_unchanged(self, root):
        p = _book("short")
        r, base = _live_with(root, p)
        assert r["quote_ok"] and r["exit_now_basis"] == "bid_ask" and r["bid_zero_legs"] == []
        assert r["greeks"]["degraded_legs"] == []
        assert r["exit_now_premium"] == pytest.approx(base[p["call_symbol"]]["ask"] + base[p["put_symbol"]]["ask"])

    @pytest.mark.parametrize("name,over", [
        ("ask 为 0（整条腿没有卖盘）", {"bid": 0.0, "ask": 0.0}),
        ("ask 缺失", {"bid": 0.0, "ask": None}),
        ("报价倒挂（ask < bid）", {"bid": 0.30, "ask": 0.10}),
        ("bid 为负", {"bid": -0.01, "ask": 0.10}),
    ])
    def test_dead_or_crossed_leg_is_not_waved_through(self, root, name, over):
        p = _book("short")
        r, _ = _live_with(root, p, call={**BID_ZERO_CALL, **over})
        assert r["exit_now_usd"] is None and r["exit_now_basis"] is None, name
        assert r["bid_zero_legs"] == [] and r["greeks"]["degraded_legs"] == [], name
        assert r["greeks"]["complete"] is False and "call:no_quote" in r["greeks"]["missing"], name
        assert r["greeks"]["dollar_delta"] is None, name       # 缺的腿不当 0

    @pytest.mark.parametrize("name,over", [
        ("iv 为 0（CBOE 全零占位）", {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}),
        ("iv 缺失", {"iv": None}),
        ("某个 Greek 缺失", {"gamma": None}),
    ])
    def test_degenerate_greeks_are_not_used_but_exit_still_is(self, root, name, over):
        """平仓价与 Greeks 各判各的：ask 真实就给平仓价；Greeks 不可信就不拿来用（不当 0）。"""
        p = _book("short")
        r, _ = _live_with(root, p, call={**BID_ZERO_CALL, **over})
        assert r["exit_now_basis"] == "ask_only" and r["exit_now_usd"] is not None, name
        assert r["greeks"]["degraded_legs"] == [] and r["greeks"]["complete"] is False, name
        assert r["greeks"]["dollar_delta"] is None, name

    def test_front_end_reads_exactly_the_fields_the_backend_sends(self, root):
        """后端新增的三个字段，前端都要读到；前端读的名字后端都要真给（不靠肉眼对）。"""
        r, _ = _live_with(root, _book("short"), call=BID_ZERO_CALL)
        names = ("exit_now_basis", "bid_zero_legs", "degraded_legs")
        assert "exit_now_basis" in r and "bid_zero_legs" in r and "degraded_legs" in r["greeks"]
        js = "\n".join(ln for ln in (STATIC / "pages" / "straddle.js").read_text(encoding="utf-8").splitlines()
                       if not ln.lstrip().startswith("//"))
        for n in names:
            assert n in js, f"前端没读 {n}"


#: 手写的逐腿 per-share Greeks（金额 = 张数 × 100 × 方向 × …，见 straddle.py 模块头；卖跨式方向 −1）
CALL_G = {"delta": 0.0511, "gamma": 0.0842, "vega": 0.0038, "theta": -0.0065}
PUT_G = {"delta": -0.4812, "gamma": 0.0911, "vega": 0.0049, "theta": -0.0071}
GOOD_PUT = {"bid": 0.85, "ask": 0.95, "mid": 0.90, "spread_pct": 0.11, "iv": 0.31, "quote_ok": True, **PUT_G}
GOOD_CALL = {"bid": 0.40, "ask": 0.50, "mid": 0.45, "spread_pct": 0.22, "iv": 0.30, "quote_ok": True, **CALL_G}
BID_ZERO_PUT = {"bid": 0.0, "ask": 0.05, "mid": None, "spread_pct": None, "iv": 0.33, "quote_ok": False, **PUT_G}


class TestLiveGreeksNumbersAndGating:
    """二次检查 [15] [1]：此前 bid=0 腿的 Greeks 只断言「不是 None」，put 腿作为 bid=0 腿时一个数都没核；
    另一条腿缺时 `degraded_legs` 仍被填上，页面同时写「含 call 腿的模型值」和一排「—」。数值全部手算（卖跨式：张数 × 100 × −1）。"""

    @staticmethod
    def _expect(n, S, c, p):
        q = -n * 100.0
        return {"dollar_delta": q * (c["delta"] + p["delta"]) * S,
                "gamma_dollar_per_1pct": 0.5 * q * (c["gamma"] + p["gamma"]) * (0.01 * S) ** 2,
                "vega_dollar_per_pt": q * (c["vega"] + p["vega"]),
                "theta_dollar_per_day": q * (c["theta"] + p["theta"])}

    def test_call_leg_degraded_numbers_by_hand(self, root):
        p = _book("short")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL, put=GOOD_PUT)
        g, want = r["greeks"], self._expect(p["contracts"], r["underlying_price"], CALL_G, PUT_G)
        assert g["degraded_legs"] == ["call"] and g["complete"] is True
        for k, v in want.items():
            assert g[k] == pytest.approx(v), k
        assert g["dollar_delta"] > 0 and g["gamma_dollar_per_1pct"] < 0 and g["theta_dollar_per_day"] > 0   # 卖跨式：净 Δ 由 put 腿的负 delta 反号、Γ 空头、θ 收租

    def test_put_leg_degraded_numbers_by_hand(self, root):
        """put 是唯一 bid=0 腿：它的 Greeks 一样要进合计、并被标出（此前整条路径没有任何数值断言）。"""
        p = _book("short")
        r, base = _live_with(root, p, call=GOOD_CALL, put=BID_ZERO_PUT)
        g, want = r["greeks"], self._expect(p["contracts"], r["underlying_price"], CALL_G, PUT_G)
        assert g["degraded_legs"] == ["put"] and r["bid_zero_legs"] == ["put"] and g["complete"] is True
        for k, v in want.items():
            assert g[k] == pytest.approx(v), k
        assert r["exit_now_basis"] == "ask_only" and r["exit_now_premium"] == pytest.approx(0.50 + 0.05)   # 买回 = 两腿 ask 之和

    def test_both_legs_degraded_numbers_by_hand(self, root):
        p = _book("short")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL, put=BID_ZERO_PUT)
        g, want = r["greeks"], self._expect(p["contracts"], r["underlying_price"], CALL_G, PUT_G)
        assert g["degraded_legs"] == ["call", "put"]
        for k, v in want.items():
            assert g[k] == pytest.approx(v), k

    @pytest.mark.parametrize("name,put", [
        ("put 腿 ask 为 0", {"bid": 0.0, "ask": 0.0}),
        ("put 腿倒挂", {"bid": 0.50, "ask": 0.10}),
        ("put 腿 ask 缺失", {"bid": 0.0, "ask": None}),
    ])
    def test_a_dead_put_blocks_the_exit_price_even_when_the_call_has_a_real_ask(self, root, name, put):
        """卖跨式买回要两条腿都有真实 ask；只查 call 腿会凭空少付 put 的买回成本（exit = 0.02 + 0）。"""
        p = _book("short")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL, put=put)
        assert r["exit_now_premium"] is None and r["exit_now_usd"] is None and r["exit_now_basis"] is None, name

    @pytest.mark.parametrize("name,kw", [
        ("put 腿在 payload 里根本没有", {"drop": ("put",)}),
        ("put 腿 bid 与 ask 都是 0（没有任何盘口）", {"put": {"bid": 0.0, "ask": 0.0, "mid": None, "quote_ok": False}}),
    ])
    def test_degraded_leg_is_not_announced_when_the_other_leg_is_missing(self, root, name, kw):
        """另一条腿缺 ⇒ 四个合计全是 None ⇒ 没有任何一格用到 call 腿的模型值 ⇒ 不许告诉页面「含 call 腿的模型值」。"""
        p = _book("short")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL, **kw)
        g = r["greeks"]
        assert g["complete"] is False and g["missing"] == ["put:no_quote"], name
        assert all(g[k] is None for k in ("dollar_delta", "gamma_dollar_per_1pct", "theta_dollar_per_day", "vega_dollar_per_pt")), name
        assert g["degraded_legs"] == [], name
        assert r["bid_zero_legs"] == ["call"]                                            # bid=0 这件事本身仍如实给出

    def test_partial_totals_still_name_the_degraded_leg(self, root):
        """put 只缺 gamma：Δ / vega / θ 的合计仍用到了 call 腿的模型值 ⇒ 仍要标出（所以判据是「有合计」不是「complete」）。"""
        p = _book("short")
        r, _ = _live_with(root, p, call=BID_ZERO_CALL, put={"gamma": None})
        g = r["greeks"]
        assert g["complete"] is False and "put:gamma" in g["missing"] and g["gamma_dollar_per_1pct"] is None
        assert g["vega_dollar_per_pt"] is not None and g["degraded_legs"] == ["call"]


def _body(fn: str) -> str:
    return _fn_src(_straddle_js(), fn)


class TestFrozenMarkEstimate:
    """账本 mark 冻结时，持仓卡并排给「按当前报价估算」的盈亏（v0.45.621）。源码形状钉，每条对应一个会真出错的方向。"""

    def test_estimate_prefers_mid_then_ask_only_and_never_the_ledger_mark(self):
        import re
        fn = _body("estimateOf")
        ifs = re.findall(r"^\s*if \((.*?)\) return", fn, re.M)
        # 条件逐字钉：多加一个 `&& …`（把 mid 路径整个短路掉）也算红
        assert ifs == ["isNum(r.unrealized_mid_usd)", "isNum(r.exit_now_usd)"], "先 mid，mid 不可得才退到立即平仓价"
        assert "ask_only" in fn
        # 估算只许来自当前报价：不得出现账本 mark / 账本盈亏顶替
        for banned in ("last_mark", "p.unrealized_usd", "unrealized_pct"):
            assert banned not in fn, banned

    def test_estimate_is_labelled_not_ledger_and_says_when_unavailable(self):
        fn = _body("renderEst")
        assert "非账本数字" in fn and "旧价" in fn
        import re
        for why in ("r.error", "!r.available", "!e"):
            assert re.search(r"^\s*if \(" + re.escape(why) + r"\) \{ fill\(", fn, re.M), f"{why} 这条失败路径必须有话说，不能留着旧价当没事"

    def test_only_frozen_positions_get_an_estimate_row_and_an_auto_pull(self):
        card = _body("positionCard")
        assert 'const stale = p.mark_source !== "cboe_mid"' in card
        assert "const estEl = stale ?" in card, "没冻结的持仓不该多出这一行"
        render = _body("renderStraddle")
        assert "liveEls[k].estEl" in render, "进页面自动拉的只有冻结的那几只"

    def test_pull_one_updates_both_live_block_and_estimate(self):
        fn = _body("renderStraddle")
        one = fn[fn.index("async function pullOne"):fn.index("async function pullAll")]
        assert "renderLive(" in one and "renderEst(" in one
        # 手动 / 自动拉全部也走同一条路径，不另写一份（两份会各自漏掉一边）
        assert "pullOne(t, force)" in fn


class TestPageFrontEndContracts:
    """前端源码形状钉（读 straddle.js / help.js，去 // 注释行）。每条对应一个已确认的显示缺陷。"""

    def test_degraded_note_needs_a_total_that_actually_exists(self):
        import re
        js = _straddle_js()
        totals = re.search(r"const GREEK_TOTALS = \[(.*?)\];", js, re.S).group(1)
        names = set(re.findall(r'"(\w+)"', totals))
        sent = SD.position_greeks([GOOD_CALL, GOOD_PUT], "short", 1, 100.0)
        assert names == {"dollar_delta", "gamma_dollar_per_1pct", "theta_dollar_per_day", "vega_dollar_per_pt"} <= set(sent)
        grid = _body("greeksGrid")
        note = grid[grid.index("g.degraded_legs"):]
        assert "GREEK_TOTALS.some" in note[:note.index("仅供参考")], "降级腿那句话必须以「合计里有数」为前提"

    def test_closed_count_is_split_into_since_start_and_before_start(self):
        panel = _body("strangleShadowPanel")
        assert "自 ${s.start_date} 起入场的" not in panel, "n_closed 含登记前入场的，不能挂在「自 start_date 起入场」下"
        assert "by_status?.before_start" in panel and "s.n_closed" in panel
        assert "登记前入场" in panel and "登记后入场" in panel
        assert "进检验 ${s.n_units}" in panel
        # 只给个数：这个函数里不许出现任何盈亏字段
        for banned in ("pnl", "profit", "盈亏 ", "d_mean", "mean_d"):
            assert banned not in panel, banned

    def test_status_label_table_matches_what_units_can_produce(self):
        import re
        import straddle_strangle_shadow as SS
        block = re.search(r"const SS_STATUS = \{(.*?)\};", _straddle_js(), re.S).group(1)
        keys = set(re.findall(r"(\w+): \"", block))
        # 用真实的 units() 造出每一种可达状态，逐个核对都有中文
        qs = {"schema_version": SS.PREREG["quote_schema"], "available": True, "underlying_price": 24.48, "hist_moves": [],
              "call": {"symbol": "C", "strike": 26.0, "bid": 0.24, "ask": 0.28, "mid": 0.26, "quote_ok": True},
              "put": {"symbol": "P", "strike": 23.0, "bid": 0.17, "ask": 0.47, "mid": 0.32, "quote_ok": True}}
        straddle = {"call": None, "put": None, "premium": None, "contracts": 39, "underlying": 24.48}
        ok = SS.entry_record(qs, "short", 100000.0, lambda s, p, n: (3, None), straddle, as_of="2026-10-22")
        entries = {"ok": ok,
                   "no_size": SS.entry_record(qs, "short", 100000.0, lambda s, p, n: (0, "too big"), straddle, as_of="2026-10-22"),
                   "no_signal_record": SS.entry_record(None, "short", 100000.0, lambda s, p, n: (3, None), straddle, as_of="2026-10-22"),
                   "unquotable": SS.entry_record({**qs, "available": False, "reason": "x"}, "short", 100000.0,
                                                 lambda s, p, n: (3, None), straddle, as_of="2026-10-22")}
        assert {k: v["status"] for k, v in entries.items()} == {k: k for k in entries}, "夹具没造出这四种入场状态"
        start = SS.PREREG["start_date"]
        rows = [{"ticker": "A", "side": "short", "entry_date": "2000-01-01", "earnings_date": "2026-10-21", "shadow_strangle": None}]
        for name, e in entries.items():
            rows.append({"ticker": "A", "side": "short", "entry_date": start, "earnings_date": "2026-10-21", "pnl_usd": 1.0,
                         "shadow_strangle": e})
        for ex in (None, {"status": "unpriced"}, {"status": "error"}):                    # exit_missing / exit_unpriced / exit_error
            rows.append({"ticker": "A", "side": "short", "entry_date": start, "earnings_date": "2026-10-21", "pnl_usd": 1.0,
                         "shadow_strangle": {**ok, "exit": ex}})
        for xd in ("2026-10-23", "2026-10-21"):                                          # 平仓日晚于 / 不晚于财报日 ⇒ ok / exit_before_event
            rows.append({"ticker": "A", "side": "short", "entry_date": start, "earnings_date": "2026-10-21", "pnl_usd": 3.0,
                         "exit_date": xd, "shadow_strangle": {**ok, "exit": {"status": "ok", "pnl_usd": 5.0}}})
        rows.append({"ticker": "A", "side": "long", "entry_date": start, "earnings_date": "2026-10-21", "shadow_strangle": ok})
        rows.append({"ticker": "A", "side": "short", "entry_date": start, "earnings_date": "2026-10-21", "shadow_strangle": {**ok, "schema_version": 0}})
        reachable = {u["status"] for u in SS.units(rows)}
        assert {"before_start", "ok", "long_side", "exit_missing", "exit_unpriced", "exit_error", "exit_before_event",
                "entry_no_size", "entry_unquotable", "entry_no_signal_record"} <= reachable
        for st in reachable:
            if st.startswith("schema:"):
                continue                                                                 # 版本号不固定：ssLabel 用前缀规则翻
            assert st in keys, f"状态 {st} 没有中文"
        assert {"exit_missing", "exit_before_event"} <= keys, "新增 / 补登记的状态"
        assert "exit_not_applicable" not in keys, "units() 里不可达（入场不是 ok 的单位到不了平仓那一步），表里不留死条目"
        assert "exit_not_applicable" not in reachable
        # 持仓中那一行：入场状态四种都得翻成中文（ok 单独处理，其余走 entry_ 前缀），未登记的原样显示不留空
        for k in ("unquotable", "no_signal_record", "no_size"):
            assert f"entry_{k}" in keys, k
        open_label, label = _body("ssOpenLabel"), _body("ssLabel")
        assert '"入场可成交"' in open_label and "entry_${k}" in open_label
        assert re.search(r":\s*String\(k\);\s*}\s*$", label) and "schema:" in label        # 兜底：未登记的状态码原样显示、不留空

    def test_open_row_uses_the_label_functions_not_raw_keys(self):
        panel = _body("strangleShadowPanel")
        assert "ssOpenLabel(k)" in panel and "ssLabel(k)" in panel
        assert "`${k} ${v}`" not in panel, "括号里不许再直接拼英文状态码"

    def test_help_page_says_the_ledger_marks_and_exits_stay_strict(self):
        txt = (STATIC / "pages" / "help.js").read_text(encoding="utf-8")
        i = txt.index("账本自己的盯市和财报后平仓同样仍是严格口径")
        seg = txt[i:i + 400]
        assert "冻结 mark" in seg and "不会驱动账本" in seg and "不平仓" in seg
        assert "开仓前" in txt and "卖方视角" in txt                                       # 对比卡的 NAV 规则与视角也写进帮助页


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


class TestEventMissedMarker:
    """v0.45.606：页面标出「事件前被平」，KPI 单列；分类与账本模块同一处（`options_paper_leg.event_missed`）。"""

    def _row(self, exit_d, ed, **kw):
        return {"ticker": "TSLA", "side": "long", "label": "cheap", "entry_date": "2026-09-22", "exit_date": exit_d,
                "earnings_date": ed, "pnl_usd": -1825.0, "pnl_pct": -45.9, "exit_reason": "expiry_buffer", **kw}

    def test_closed_view_carries_the_marker_three_valued(self):
        for exit_d, ed, want in (("2026-10-21", "2026-10-21", True), ("2026-10-21", "2026-10-20", False),
                                 ("2026-10-21", None, None)):
            assert SD.closed_view(self._row(exit_d, ed), sig_entry=None, event_rows=[])["event_missed"] is want

    def test_kpis_split_matches_the_ledger_module(self):
        import options_paper_leg as opl
        rows = [self._row("2026-10-21", "2026-10-21"), self._row("2026-10-21", "2026-10-20", pnl_usd=-664.0)]
        k = SD.kpis(rows)
        assert k["event_split"] == opl.split_by_event(rows)
        assert k["n"] == 2 and k["event_split"]["event_missed"]["n"] == 1

    def test_page_wires_marker_and_kpi_tiles(self):
        """前端接线别被改没：行上的标记、两块 KPI 都得在（变异：删掉任一处 ⇒ 红）。"""
        js = (Path(SD.__file__).parent / "static" / "pages" / "straddle.js").read_text(encoding="utf-8")
        assert "r.event_missed === true" in js and "事件前被平" in js
        assert 'kpi("其中测到事件"' in js and "event_split" in js
        assert "es.saw_event.by_side" in js, "「按方向」表要只含测到事件的那批，否则事件前被平的会拉低买跨式胜率"
