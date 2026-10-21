"""Alpha Bot「跨式 vs 宽跨」并排对比卡（v0.45.584）的守卫。每组回答一句「谁会红」：

  · 数是对的：用 2026-09-29 T 的真实报价形状（ATM 24.50：0.72/0.86；25Δ：call 26 / put 23）逐项手算——权利金、盈亏平衡带、
    进出成本、落在带外 k/n、Wilson 区间、定仓张数（与账本自己的 `size_contracts` 对拍）（`TestNumbersByHand`）；
  · 判不了就说判不了：bid=0 / 倒挂 / 缺腿 / 缺快照 / 缺历史各有原因码，且原因码都登记在表里（`TestUnavailable`）；
  · 不下结论：返回值里没有推荐 / 胜出 / 综合分之类的键，前端对比卡代码里也没有（`TestNoVerdict`）；
  · 盲期：信号行带 `gex_ctx` 时，对比卡里一个都没有（`TestNoVerdict`）；
  · 点时点：历史财报只取财报日早于 as_of 的，事后才发生的那次不混进「事前的历史」（`TestPointInTime`）；
  · 只读、不联网、不凭空建目录；缺快照不退回演示数据（`TestService`）。
全部离线：手写夹具 + 合成账本。
"""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest

import options_paper_leg as opl
from alphabot import straddle as SD
from alphabot import strangle_compare as SC
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService
from tests.test_alphabot_straddle import AS_OF, NOW, _client, _fingerprint, _keys, _svc, _write_ledger

STATIC = Path(__file__).resolve().parent.parent / "alphabot" / "static"


def _leg(strike, bid, ask, delta, oi, *, quote_ok=True, iv=0.30):
    mid = (bid + ask) / 2.0 if quote_ok else None
    return {"strike": strike, "bid": bid, "ask": ask, "mid": mid, "spread_pct": ((ask - bid) / mid) if mid else None,
            "delta": delta, "iv": iv, "oi": oi, "volume": 0.0, "quote_ok": quote_ok}


def _qs(**over):
    """2026-09-29 T 的快照形状（实测值）：ATM 24.50 = 0.72 / 0.86（合计 1.58，账本入场价），25Δ = call 26 / put 23。"""
    c = {"atm_call": _leg(24.5, 0.72, 0.82, 0.4914, 4), "atm_put": _leg(24.5, 0.86, 0.99, -0.5262, 163),
         "c25": _leg(26.0, 0.24, 0.28, 0.2284, 523), "p25": _leg(23.0, 0.17, 0.47, -0.2576, 1030)}
    c.update(over)
    return {"data_available": True, "error": None, "selected_expiry": "2026-10-30", "selected_dte": 31,
            "underlying_price": 24.48, "market_open": False, "fetched_at": "2026-09-29T17:29:19-04:00", "contracts": c}


#: T 过去 8 次财报的 2 日涨跌（实测，earnings_cache/T_moves.json）
T_MOVES = [("2026-07-22", 3.1447), ("2026-04-22", 2.8207), ("2026-01-28", 9.2609), ("2025-10-22", -5.4894),
           ("2025-07-23", 1.8235), ("2025-04-23", 2.1142), ("2025-01-27", 7.3944), ("2024-10-23", 3.2558)]
RAW = [{"earnings_date": d, "move_pct": m, "abs_move_pct": abs(m)} for d, m in T_MOVES]
NAV = 102546.5


@pytest.fixture(autouse=True)
def _pin_risk(monkeypatch):
    """定仓手算依赖 6%（账本现行值）；钉住，免得改配置时这组数无声漂移。"""
    monkeypatch.setitem(opl.CONFIG, "risk_per_trade_pct", 6.0)


def _size(premium, nav):
    return opl.size_contracts("short", premium, nav)


def _cmp(qs=None, **kw):
    kw.setdefault("raw_moves", RAW)
    kw.setdefault("nav", NAV)
    kw.setdefault("size_fn", _size)
    kw.setdefault("max_spread_frac", 0.15)
    return SC.compare("T", "2026-09-29", qs=qs if qs is not None else _qs(), **kw)


class TestNumbersByHand:
    def test_straddle(self):
        s = _cmp()["structures"]["straddle"]
        assert s["available"] and s["premium"] == pytest.approx(1.58)                    # 0.72 + 0.86 = 账本入场价
        assert s["premium_frac_of_spot"] == pytest.approx(1.58 / 24.48)
        assert s["cost_to_close"] == pytest.approx(0.82 + 0.99)                           # 买回按 ask
        assert s["round_trip_frac"] == pytest.approx((1.81 - 1.58) / 1.58)               # ≈ 14.6%
        assert (s["band"]["lo"], s["band"]["hi"]) == pytest.approx((22.92, 26.08))
        assert s["band"]["lo_pct"] == pytest.approx((22.92 / 24.48 - 1) * 100) and s["band"]["hi_pct"] == pytest.approx((26.08 / 24.48 - 1) * 100)
        assert s["max_spread_frac"] == pytest.approx(0.14054, abs=1e-4) and s["flags"] == []

    def test_strangle(self):
        s = _cmp()["structures"]["strangle"]
        assert s["available"] and s["premium"] == pytest.approx(0.41)                    # 0.24 + 0.17
        assert s["round_trip_frac"] == pytest.approx((0.28 + 0.47 - 0.41) / 0.41)        # ≈ 82.9%：宽跨的腿便宜、点差占比大
        assert (s["band"]["lo"], s["band"]["hi"]) == pytest.approx((23.0 - 0.41, 26.0 + 0.41))
        assert [f["code"] for f in s["flags"]] == ["wide_spread"] and s["max_spread_frac"] == pytest.approx(0.9375)

    def test_history_outside_the_band(self):
        r = _cmp()
        a, b = r["structures"]["straddle"]["hist_outside"], r["structures"]["strangle"]["hist_outside"]
        # 带外 = 涨跌 ≥ +6.54%（跨式）/ +7.88%（宽跨），或 ≤ −6.37% / −7.72%：9.26、7.39 在跨式带外；只有 9.26 在宽跨带外；−5.49 都在带内
        assert (a["k"], a["n"]) == (2, 8) and (b["k"], b["n"]) == (1, 8)
        assert sorted(m["move_pct"] for m in a["outside"]) == [7.3944, 9.2609]
        assert a["ci95"] == pytest.approx([0.0712, 0.5911], abs=1e-3)                    # Wilson：8 次里 2 次，区间 7%–59%

    def test_downside_moves_count_too(self):
        """−7.0% 在跨式带外（≤ −6.37%）、在宽跨带内（> −7.72%）：只数上行的实现会把 k 数少。"""
        r = _cmp(raw_moves=RAW + [{"earnings_date": "2024-07-24", "move_pct": -7.0}])
        a, b = r["structures"]["straddle"]["hist_outside"], r["structures"]["strangle"]["hist_outside"]
        assert (a["k"], a["n"]) == (3, 9) and (b["k"], b["n"]) == (1, 9)
        assert -7.0 in [m["move_pct"] for m in a["outside"]]

    def test_after_hours_caveat_follows_the_snapshot_session(self):
        codes = lambda r: [c["code"] for c in r["caveats"]]  # noqa: E731
        assert "after_hours" in codes(_cmp(_qs()))
        assert "after_hours" not in codes(_cmp({**_qs(), "market_open": True}))
        assert {"band_is_expiry", "small_sample", "no_verdict"} <= set(codes(_cmp()))

    def test_nav_is_the_latest_at_or_before_the_day_never_after(self):
        eq = [{"date": "2026-09-28", "nav": 100.0}, {"date": "2026-09-29", "nav": 101.0}, {"date": "2026-09-30", "nav": 999.0},
              {"date": "2026-09-27", "nav": None}]
        sigs = [{"ticker": "T", "as_of": "2026-09-29", "x": 1}, {"ticker": "T", "as_of": "2026-09-30", "x": 2}]
        sig, nav = SD.pick_signal_and_nav(sigs, eq, "T", "2026-09-29")
        assert nav == 101.0 and sig["x"] == 1
        assert SD.pick_signal_and_nav(sigs, eq, "T", "2026-09-26") == (None, None)
        assert SD.pick_signal_and_nav(sigs, eq, "ZZ", "2026-09-29")[0] is None

    def test_wilson_edges(self):
        assert SC.wilson(0, 0) is None
        lo, hi = SC.wilson(0, 8)
        assert lo == 0.0 and 0.0 < hi < 0.4
        lo, hi = SC.wilson(8, 8)
        assert hi == 1.0 and 0.6 < lo < 1.0

    def test_sizing_uses_the_ledgers_own_rule(self):
        r = _cmp()
        a, b = r["structures"]["straddle"]["sizing"], r["structures"]["strangle"]["sizing"]
        assert (a["contracts"], b["contracts"]) == (38, 150)                             # floor(6152.79 / 158) / floor(6152.79 / 41)
        assert a["shares"] == 3800 and b["shares"] == 15000
        assert b["notional_usd"] == pytest.approx(150 * 100 * 24.48)
        assert r["premium_ratio"] == pytest.approx(0.41 / 1.58) and r["contracts_ratio"] == pytest.approx(150 / 38)
        for prem, n in ((1.58, a["contracts"]), (0.41, b["contracts"])):
            assert _size(prem, NAV)[0] == n                                              # 对拍账本函数本身

    def test_no_nav_means_no_contract_count_not_a_made_up_one(self):
        r = _cmp(nav=None)
        s = r["structures"]["straddle"]["sizing"]
        assert s["contracts"] == 0 and s["reason"] and r["contracts_ratio"] is None


class TestUnavailable:
    @pytest.mark.parametrize("name,over,reason", [
        ("bid 为 0（25Δ put 常见）", {"p25": _leg(23.0, 0.0, 0.05, -0.05, 10, quote_ok=False)}, "no_bid:put"),
        ("ask < bid 倒挂", {"c25": {**_leg(26.0, 0.30, 0.20, 0.23, 10), "quote_ok": False}}, "quote_not_ok:call"),
        ("缺腿", {"p25": None}, "missing_leg:put"),
        ("bid 缺失", {"c25": {**_leg(26.0, 0.24, 0.28, 0.23, 10), "bid": None, "quote_ok": False}}, "no_bid:call"),
    ])
    def test_a_bad_leg_only_kills_its_own_structure(self, name, over, reason):
        r = _cmp(_qs(**over))
        s = r["structures"]["strangle"]
        assert not s["available"] and s["reason"] == reason and s["premium"] is None and s["band"] is None, name
        assert s["reason_zh"] and not s["reason_zh"].startswith("未登记"), name
        assert r["structures"]["straddle"]["available"] and r["premium_ratio"] is None and r["contracts_ratio"] is None

    @pytest.mark.parametrize("qs,qs_error,reason", [
        (None, "snapshot_missing", "snapshot_missing"),
        (None, "snapshot_unreadable", "snapshot_unreadable"),
        (None, "quote_set_unavailable", "quote_set_unavailable"),
        ({"data_available": False, "error": "chain empty"}, None, "quote_set_unavailable"),
        ({**_qs(), "underlying_price": None}, None, "no_spot"),
        ({**_qs(), "underlying_price": 0}, None, "no_spot"),
    ])
    def test_whole_card_unavailable_names_why(self, qs, qs_error, reason):
        r = SC.compare("T", "2026-09-29", qs=qs, qs_error=qs_error, raw_moves=RAW)
        assert r["available"] is False and r["reason"] == reason and r["structures"] is None
        assert r["reason_zh"] and not r["reason_zh"].startswith("未登记")

    def test_missing_history_is_reported_not_zero(self):
        for raw, err, why in ((None, None, "no_history"), ([], None, "no_history"), (None, "history_unreadable", "history_unreadable")):
            r = _cmp(raw_moves=raw, hist_error=err)
            assert r["available"] and r["hist"] is None and r["hist_reason"] == why and r["hist_reason_zh"]
            ho = r["structures"]["straddle"]["hist_outside"]
            assert ho["n"] == 0 and ho["k"] == 0 and ho["frac"] is None and ho["ci95"] is None   # 0 次不是「0/0 = 没有尾部」

    def test_every_reason_code_is_registered(self):
        """跑一圈退化输入，所有 reason / hist_reason 都查得到表（带 `:` 的按前缀）。"""
        seen = set()
        for over in ({"p25": None}, {"c25": _leg(26.0, 0.0, 0.1, 0.2, 1, quote_ok=False)},
                     {"atm_put": {**_leg(24.5, 0.9, 0.8, -0.5, 1), "quote_ok": False}}):
            for s in _cmp(_qs(**over))["structures"].values():
                seen.add(s["reason"])
        seen |= {_cmp(raw_moves=None)["hist_reason"], SC.compare("T", "2026-09-29", qs=None)["reason"],
                 SC.compare("T", "2026-09-29", qs=_qs(), raw_moves=[{"earnings_date": "2030-01-01", "move_pct": 1.0}])["hist_reason"]}
        seen.discard(None)
        assert seen, "夹具没造出任何原因码"
        assert {c.partition(":")[0] for c in seen} <= set(SC.REASONS)
        assert all(not SC.reason_zh(c).startswith("未登记") for c in seen)
        assert SC.reason_zh("nope_not_registered").startswith("未登记")                  # 正对照：真有未登记的，它会这样说


class TestPointInTime:
    def test_only_earnings_before_as_of_count(self):
        """历史 = 财报日早于 as_of 的。同日 / 之后的（含「事后才发生的那次」）一律不进。"""
        extra = RAW + [{"earnings_date": "2026-09-29", "move_pct": 50.0}, {"earnings_date": "2026-10-21", "move_pct": -50.0}]
        r = _cmp(raw_moves=extra)
        assert r["hist"]["n"] == 8 and max(abs(m["move_pct"]) for m in r["hist"]["moves"]) == pytest.approx(9.2609)
        assert r["structures"]["straddle"]["hist_outside"]["n"] == 8

    def test_earlier_as_of_drops_later_events(self):
        r = SC.compare("T", "2026-02-01", qs=_qs(), raw_moves=RAW, nav=NAV, size_fn=_size)
        assert r["hist"]["n"] == 6 and [m["earnings_date"] for m in r["hist"]["moves"]][0] == "2026-01-28"   # 新 → 旧

    def test_malformed_rows_are_dropped_not_zeroed(self):
        r = _cmp(raw_moves=RAW + ["x", {"earnings_date": "2026-01-01"}, {"earnings_date": "2026-01-02", "move_pct": float("nan")},
                                  {"earnings_date": "2026-01-03", "move_pct": True}])
        assert r["hist"]["n"] == 8

    def test_hist_stats(self):
        h = _cmp()["hist"]
        absm = sorted(abs(m) for _, m in T_MOVES)
        assert h["median_abs_pct"] == pytest.approx((absm[3] + absm[4]) / 2) and h["max_abs_pct"] == pytest.approx(9.2609)
        assert h["mean_abs_pct"] == pytest.approx(sum(absm) / 8)


class TestNoVerdict:
    BANNED = ("recommend", "verdict", "winner", "better", "prefer", "pick", "score", "best", "choose", "advice")

    def test_no_verdict_keys_anywhere(self):
        r = _cmp(signal={"label": "rich", "ratio": 1.58, "gex_ctx": {"regime": "positive_gex"}})
        keys = set(_keys(r))
        assert {"structures", "legs", "hist_outside"} <= keys, "正对照：遍历器真的走到了深处"
        bad = [k for k in keys if any(b in str(k).lower() for b in self.BANNED)]
        assert not bad, bad
        assert any(c["code"] == "no_verdict" for c in r["caveats"])

    def test_gex_ctx_never_enters(self):
        sig = {"label": "rich", "ratio": 1.58, "earnings_date": "2026-10-21", "gex_ctx": {"regime": "positive_gex"}, "shadow_event": {"x": 1}}
        r = _cmp(signal=sig)
        assert r["signal"]["ratio"] == 1.58 and r["signal"]["earnings_date"] == "2026-10-21"
        assert not (SD.BLINDED_KEYS & set(_keys(r))) and "shadow_event" not in set(_keys(r))

    def test_front_end_card_has_no_verdict_wording(self):
        js = (STATIC / "pages" / "straddle.js").read_text(encoding="utf-8")
        a, b = js.index("// ── 跨式 vs 宽跨"), js.index("function renderLive")
        code = "\n".join(ln for ln in js[a:b].splitlines() if not ln.lstrip().startswith("//"))
        assert "compareCard" in code, "正对照：截到的确实是对比卡那一段"
        for w in ("推荐", "建议", "更优", "更好", "更划算", "胜出", "应该卖", "首选"):
            assert w not in code, w

    def test_front_end_reads_the_fields_the_backend_sends(self):
        r = _cmp()
        s = r["structures"]["straddle"]
        sent = set(_keys(r)) | set(s)
        js = "\n".join(ln for ln in (STATIC / "pages" / "straddle.js").read_text(encoding="utf-8").splitlines()
                       if not ln.lstrip().startswith("//"))
        for name in ("hist_outside", "round_trip_frac", "premium_frac_of_spot", "max_spread_frac", "cost_to_close", "band", "sizing",
                     "caveats", "hist_reason_zh", "premium_ratio", "contracts_ratio", "reason_zh", "median_abs_pct", "max_abs_pct",
                     "implied_event_move_pct", "market_open", "selected_expiry"):
            assert name in js, f"前端没读 {name}"
            assert name in sent or name == "implied_event_move_pct", f"后端没给 {name}"


class TestService:
    def _root(self, tmp_path, *, snapshot=True, moves=True):
        root = _write_ledger(tmp_path / "data", syn.demo_straddle_ledger(AS_OF))
        tk = syn.demo_straddle_ledger(AS_OF)["positions"][0]["ticker"]
        if snapshot:
            (root / "cache").mkdir()
            qs = _qs()
            qs["underlying_price"] = 24.48
            (root / "cache" / f"options_snapshot_{tk}_{AS_OF}.json").write_text(json.dumps({"ticker": tk, "quote_set": qs}), encoding="utf-8")
        if moves:
            (root / "earnings_cache").mkdir()
            (root / "earnings_cache" / f"{tk}_moves.json").write_text(json.dumps({"n": 8, "moves": RAW}), encoding="utf-8")
        return root, tk

    def test_reads_local_files_and_matches_the_ledgers_sizing(self, tmp_path):
        root, tk = self._root(tmp_path)
        r = _svc(root).straddle_compare(tk, AS_OF)
        assert r["available"] and r["structures"]["straddle"]["available"] and r["hist"]["n"] == 8 and r["demo"] is False
        nav = max((e for e in syn.demo_straddle_ledger(AS_OF)["equity"] if e["date"] <= AS_OF), key=lambda e: e["date"])["nav"]
        assert r["nav"] == pytest.approx(nav)
        assert r["structures"]["straddle"]["sizing"]["contracts"] == opl.size_contracts("short", 1.58, nav)[0]
        assert r["signal"] is not None and "gex_ctx" not in r["signal"]                 # 合成账本信号行带 gex_ctx，对比卡里不许有

    def test_default_as_of_is_the_ledgers_last_scan_day(self, tmp_path):
        root, tk = self._root(tmp_path)
        assert _svc(root).straddle_compare(tk)["as_of"] == AS_OF

    def test_missing_snapshot_is_reported_and_creates_nothing(self, tmp_path):
        root, tk = self._root(tmp_path, snapshot=False)
        before = _fingerprint(root)
        r = _svc(root).straddle_compare(tk, AS_OF)
        assert r["available"] is False and r["reason"] == "snapshot_missing" and r["demo"] is False   # 不退回演示数据
        assert not (root / "cache").exists(), "只读页不许凭空建出缓存目录"
        assert _fingerprint(root) == before

    def test_missing_history_still_gives_the_card(self, tmp_path):
        root, tk = self._root(tmp_path, moves=False)
        r = _svc(root).straddle_compare(tk, AS_OF)
        assert r["available"] and r["hist"] is None and r["hist_reason"] == "no_history"

    def test_corrupt_snapshot_is_named(self, tmp_path):
        root, tk = self._root(tmp_path)
        (root / "cache" / f"options_snapshot_{tk}_{AS_OF}.json").write_text("{not json", encoding="utf-8")
        assert _svc(root).straddle_compare(tk, AS_OF)["reason"] == "snapshot_unreadable"

    def test_endpoint_validates_and_never_writes(self, tmp_path):
        root, tk = self._root(tmp_path)
        before = _fingerprint(root)
        c = _client(_svc(root))
        assert c.get(f"/api/straddle/compare/{tk}?as_of={AS_OF}").status_code == 200
        assert c.get(f"/api/straddle/compare/{tk}").status_code == 200
        assert c.get("/api/straddle/compare/bad$").status_code == 400
        assert c.get(f"/api/straddle/compare/{tk}?as_of=not-a-date").status_code == 400
        assert c.get(f"/api/straddle/compare/{tk}?as_of=../../etc/passwd").status_code == 400
        assert _fingerprint(root) == before

    def test_demo_mode_works_offline(self):
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, now_fn=lambda: NOW)
        r = svc.straddle_compare("NVDA")
        assert r["available"] and r["demo"] is True and r["hist"]["n"] == 8
        assert all(r["structures"][k]["available"] for k in ("straddle", "strangle"))

    def test_loaders_only_read(self):
        """straddle.py 里新增的三个读文件函数不含任何写调用。"""
        src = (Path(SD.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for fn in ("load_quote_set", "load_hist_moves", "load_signal_and_nav", "_cache_dir"):
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn)
            names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(node) if isinstance(n, (ast.Attribute, ast.Name))}
            assert not (names & {"write_text", "write_bytes", "mkdir", "open", "atomic_json_write", "cache_dir"}), (fn, names)


def test_module_is_pure():
    """strangle_compare 不读写文件、不联网、不 import 卖权模块（外部函数由服务层注入）。"""
    tree = ast.parse(Path(SC.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "math", "typing"}, imported
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in {"open", "print"}]
    assert math.isfinite(SC.wilson(1, 2)[0])
