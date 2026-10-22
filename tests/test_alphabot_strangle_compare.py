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
from tests.test_alphabot_straddle import AS_OF, NOW, _client, _fingerprint, _fn_src, _keys, _straddle_js, _svc, _write_ledger

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
#: 账本在 2026-09-29 给 T 定仓用的 NAV = 开仓**前**的净值（`nav_for_sizing`）。入场日那行净值曲线是开仓之后写的，
#: 只有 102546.5（= 102995 − (1.695 − 1.58) × 100 × 39，卖跨式按 bid 入账、按 mid 盯市少掉的半个点差）——拿它定仓会少 1 张
NAV = 102995.0
NAV_ROW_AFTER_OPEN = 102546.5


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
        sig, nav, info = SD.pick_signal_and_nav(sigs, eq, "T", "2026-09-29")
        assert nav == 101.0 and sig["x"] == 1 and info["nav_date"] == "2026-09-29"       # 当天没开仓：那行就是开仓前 NAV
        assert SD.pick_signal_and_nav(sigs, eq, "T", "2026-09-26")[:2] == (None, None)
        assert SD.pick_signal_and_nav(sigs, eq, "ZZ", "2026-09-29")[0] is None
        # 没有 as_of 当天的净值行（周末 / 扫描没跑）：取日期更早的最近一行，从不取之后的 999，并如实标成「近似」
        _, nav2, info2 = SD.pick_signal_and_nav(sigs, [e for e in eq if e["date"] != "2026-09-29"], "T", "2026-09-29")
        assert nav2 == 100.0 and info2["basis"] == "latest_on_or_before" and info2["nav_date"] == "2026-09-28"

    def test_wilson_edges(self):
        assert SC.wilson(0, 0) is None
        lo, hi = SC.wilson(0, 8)
        assert lo == 0.0 and 0.0 < hi < 0.4
        lo, hi = SC.wilson(8, 8)
        assert hi == 1.0 and 0.6 < lo < 1.0

    def test_sizing_uses_the_ledgers_own_rule(self):
        r = _cmp()
        a, b = r["structures"]["straddle"]["sizing"], r["structures"]["strangle"]["sizing"]
        assert (a["contracts"], b["contracts"]) == (39, 150)                             # floor(6179.7 / 158) / floor(6179.7 / 41)：账本实际开了 39 张
        assert a["shares"] == 3900 and b["shares"] == 15000
        assert b["notional_usd"] == pytest.approx(150 * 100 * 24.48)
        assert r["premium_ratio"] == pytest.approx(0.41 / 1.58) and r["contracts_ratio"] == pytest.approx(150 / 39)
        for prem, n in ((1.58, a["contracts"]), (0.41, b["contracts"])):
            assert _size(prem, NAV)[0] == n                                              # 对拍账本函数本身

    def test_the_row_written_after_the_open_would_be_one_contract_short(self):
        """为什么 NAV 要还原：入场日那行净值（开仓后）定仓少 1 张——这条钉住「38 是错的、39 才是账本的」。"""
        assert _cmp(nav=NAV_ROW_AFTER_OPEN)["structures"]["straddle"]["sizing"]["contracts"] == 38
        assert _cmp(nav=NAV)["structures"]["straddle"]["sizing"]["contracts"] == 39

    def test_sizing_premium_usd_is_bid_based(self):
        s = _cmp()["structures"]["straddle"]["sizing"]
        assert s["premium_usd"] == pytest.approx(1.58 * 100 * 39)                        # 两腿 bid 之和 × 100 × 张数，不是 ask

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
        for fn in ("load_quote_set", "load_hist_moves", "load_signal_and_nav", "_cache_dir", "sizing_nav",
                   "pick_signal_and_nav", "entries_on", "_open_slippage_usd"):
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn)
            names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(node) if isinstance(n, (ast.Attribute, ast.Name))}
            assert not (names & {"write_text", "write_bytes", "mkdir", "open", "atomic_json_write", "cache_dir"}), (fn, names)

    # ── v0.45.593：入场日定仓 NAV 用开仓前的净值（真实账本 T 2026-09-29：账本 39 张、入场后那行 NAV 只够 38 张）
    def test_entry_day_card_matches_the_ledgers_contract_count(self, tmp_path):
        root = _write_t_ledger(tmp_path / "data")
        r = _svc(root).straddle_compare("T", "2026-09-29")
        assert r["available"] and r["demo"] is False
        assert r["nav"] == pytest.approx(102995.0, abs=0.01)                              # 102546.5 + (1.695 − 1.58) × 100 × 39
        assert r["structures"]["straddle"]["sizing"]["contracts"] == T_POSITION["contracts"] == 39
        assert r["structures"]["strangle"]["sizing"]["contracts"] == 150                 # floor(6179.7 / 41)
        assert r["contracts_ratio"] == pytest.approx(150 / 39)
        nb = r["nav_basis"]
        assert nb["kind"] == "pre_open" and nb["nav_date"] == "2026-09-29" and nb["adjust_usd"] == pytest.approx(448.5, abs=0.01)
        assert "开仓前" in nb["label"] and any(c["code"] == "nav_basis" and "开仓前" in c["text"] for c in r["caveats"])

    def test_a_day_without_a_ledger_row_is_flagged_as_an_approximation(self, tmp_path):
        root = _write_t_ledger(tmp_path / "data")
        (root / "cache" / "options_snapshot_T_2026-09-30.json").write_text(json.dumps({"quote_set": _qs()}), encoding="utf-8")
        r = _svc(root).straddle_compare("T", "2026-09-30")                               # 账本只有 09-28 / 09-29 / 10-05 三行
        assert r["nav"] == pytest.approx(102546.5) and r["nav_basis"]["kind"] == "latest_on_or_before"
        assert r["nav_basis"]["nav_date"] == "2026-09-29" and "近似" in r["nav_basis"]["text"]

    def test_point_in_time_history_through_the_service(self, tmp_path):
        """moves.json 里同日 / 之后的财报（事后才发生的那次）经 loader 读进来后，也不许进「事前的历史」。"""
        root = _write_t_ledger(tmp_path / "data")
        (root / "earnings_cache" / "T_moves.json").write_text(json.dumps({"moves": RAW + [
            {"earnings_date": "2026-09-29", "move_pct": 50.0}, {"earnings_date": "2026-10-21", "move_pct": -50.0}]}), encoding="utf-8")
        r = _svc(root).straddle_compare("T", "2026-09-29")
        assert r["hist"]["n"] == 8 and r["structures"]["straddle"]["hist_outside"]["n"] == 8

    def test_demo_entry_day_goes_through_the_real_nav_path(self):
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, now_fn=lambda: NOW)
        led = syn.demo_straddle_ledger(NOW.date().isoformat())
        p = led["positions"][0]
        r = svc.straddle_compare(p["ticker"], p["entry_date"])
        assert r["available"] and r["nav_basis"]["kind"] == "pre_open", r["nav_basis"]
        assert r["nav"] == pytest.approx(next(e["nav"] for e in led["equity"] if e["date"] == p["entry_date"]))   # 演示入场不造滑点


T_POSITION = {"ticker": "T", "side": "short", "entry_date": "2026-09-29", "contracts": 39, "entry_premium": 1.58,
              "label": "rich", "expiry": "2026-10-30", "strike": 24.5}
#: 信号行里的两腿报价（2026-09-29 T 实测）：bid 之和 1.58 = 账本入场价，mid 之和 1.695 = 账本入场 mark
T_SIGNAL_QUOTE = {"call": {"symbol": "T261030C00024500", "strike": 24.5, "bid": 0.72, "ask": 0.82, "mid": 0.77, "quote_ok": True},
                  "put": {"symbol": "T261030P00024500", "strike": 24.5, "bid": 0.86, "ask": 0.99, "mid": 0.925, "quote_ok": True}}


def _jsonl(path: Path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _write_t_ledger(root: Path) -> Path:
    """真实账本形状的最小夹具：09-28 一行、09-29 入场日一行（开仓之后写的 102546.5）、10-05 一行（之后的 NAV，绝不许用）。"""
    ld = root / SD.LEDGER_DIRNAME
    ld.mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "earnings_cache").mkdir()
    (ld / "meta.json").write_text(json.dumps({"last_run_date": "2026-10-05", "starting_capital": 100000.0}), encoding="utf-8")
    _jsonl(ld / "positions.jsonl", [T_POSITION])
    _jsonl(ld / "closed_trades.jsonl", [])
    _jsonl(ld / "equity_curve.jsonl", [{"date": "2026-09-28", "nav": 100800.0, "opened_today": 0},
                                       {"date": "2026-09-29", "nav": 102546.5, "opened_today": 1},
                                       {"date": "2026-10-05", "nav": 999999.0, "opened_today": 0}])
    _jsonl(ld / "earnings_signals.jsonl", [{"ticker": "T", "as_of": "2026-09-29", "eligible": True, "label": "rich",
                                            "earnings_date": "2026-10-21", "quote": T_SIGNAL_QUOTE}])
    (root / "cache" / "options_snapshot_T_2026-09-29.json").write_text(json.dumps({"quote_set": _qs()}), encoding="utf-8")
    (root / "earnings_cache" / "T_moves.json").write_text(json.dumps({"n": 8, "moves": RAW}), encoding="utf-8")
    return root


def _sig(t, day, call_mid, put_mid):
    return {"ticker": t, "as_of": day, "quote": {"call": {"mid": call_mid}, "put": {"mid": put_mid}}}


def _rec(t, side, n, entry, day="2026-09-29"):
    return {"ticker": t, "side": side, "contracts": n, "entry_premium": entry, "entry_date": day}


class TestSizingNav:
    """账本定仓用开仓**前**的 NAV；入场日那行净值曲线是开仓之后写的。每个期望值手算，不借被测函数。"""

    DAY = "2026-09-29"

    def _eq(self, nav, opened=1):
        return [{"date": "2026-09-28", "nav": 100800.0, "opened_today": 0}, {"date": self.DAY, "nav": nav, "opened_today": opened}]

    def test_real_t_entry_restores_the_pre_open_nav_and_the_ledgers_39(self):
        sig, nav, info = SD.pick_signal_and_nav([_sig("T", self.DAY, 0.77, 0.925)], self._eq(102546.5), "T", self.DAY,
                                                [_rec("T", "short", 39, 1.58)])
        assert nav == pytest.approx(102546.5 + (0.77 + 0.925 - 1.58) * 100 * 39) == pytest.approx(102995.0)   # 手算：+448.5
        assert info["basis"] == "pre_open" and info["row_nav"] == 102546.5 and info["adjust_usd"] == pytest.approx(448.5)
        assert info["opened"] == ["T"] and info["nav_date"] == self.DAY and sig["ticker"] == "T"
        assert opl.size_contracts("short", 1.58, nav)[0] == 39                           # 账本实际开了 39 张
        assert opl.size_contracts("short", 1.58, 102546.5)[0] == 38                      # 直接用入场日那行：少 1 张

    def test_two_openings_on_the_same_day_are_both_added_back(self):
        """账本一天只算一次 nav_for_sizing，当天所有新开仓共用它 ⇒ 两笔的滑点都要加回。
        空头 A：入场 3.00（bid 和）、mid 和 3.10、2 张 ⇒ 少 (3.10−3.00)×100×2 = 20；
        多头 B：入场 1.50（ask 和）、mid 和 1.40、3 张 ⇒ 少 (1.50−1.40)×100×3 = 30。"""
        sigs = [_sig("A", self.DAY, 1.55, 1.55), _sig("B", self.DAY, 0.70, 0.70)]
        opened = [_rec("A", "short", 2, 3.00), _rec("B", "long", 3, 1.50)]
        _, nav, info = SD.pick_signal_and_nav(sigs, self._eq(100000.0, opened=2), "A", self.DAY, opened)
        assert nav == pytest.approx(100050.0, abs=0.01) and info["adjust_usd"] == pytest.approx(50.0, abs=0.01)
        assert info["opened"] == ["A", "B"]

    def test_closed_trades_that_entered_that_day_count_too(self):
        """已经平掉的仓（在 closed_trades 里）当天同样新开过；`entries_on` 把持仓和平仓记录一起找。"""
        pos, closed = [_rec("A", "short", 2, 3.00)], [_rec("B", "long", 3, 1.50), _rec("C", "short", 1, 9.0, day="2026-09-22")]
        opened = SD.entries_on(self.DAY, pos, closed)
        assert [r["ticker"] for r in opened] == ["A", "B"]                               # C 是别的日子开的
        sigs = [_sig("A", self.DAY, 1.55, 1.55), _sig("B", self.DAY, 0.70, 0.70)]
        assert SD.pick_signal_and_nav(sigs, self._eq(100000.0, opened=2), "A", self.DAY, opened)[1] == pytest.approx(100050.0, abs=0.01)

    def test_nothing_opened_means_the_row_already_is_the_pre_open_nav(self):
        _, nav, info = SD.pick_signal_and_nav([], self._eq(102546.5, opened=0), "T", self.DAY, [])
        assert nav == 102546.5 and info["basis"] == "pre_open" and info["adjust_usd"] == 0.0

    def test_missing_mid_falls_back_to_the_entry_price_like_the_ledger(self):
        """账本 `_open_from_signal`：任一腿缺 mid ⇒ 入场 mark = 入场价 ⇒ 开仓瞬间 NAV 不变 ⇒ 不加回。"""
        sig = {"ticker": "T", "as_of": self.DAY, "quote": {"call": {"mid": 0.77}, "put": {}}}
        _, nav, info = SD.pick_signal_and_nav([sig], self._eq(102546.5), "T", self.DAY, [_rec("T", "short", 39, 1.58)])
        assert nav == 102546.5 and info["basis"] == "pre_open" and info["adjust_usd"] == 0.0

    def test_unrestorable_day_says_so_and_keeps_the_row_nav(self):
        for sigs in ([], [{"ticker": "T", "as_of": self.DAY}], [{"ticker": "T", "as_of": self.DAY, "quote": "x"}]):
            _, nav, info = SD.pick_signal_and_nav(sigs, self._eq(102546.5), "T", self.DAY, [_rec("T", "short", 39, 1.58)])
            assert nav == 102546.5 and info["basis"] == "post_open_unadjusted" and info["reason"] == "cannot_restore:T"
        bad = _rec("T", "short", 0, 1.58)                                               # 张数不可用也还原不了，不当 0 滑点
        assert SD.pick_signal_and_nav([_sig("T", self.DAY, 0.77, 0.925)], self._eq(102546.5), "T", self.DAY, [bad])[2]["basis"] == "post_open_unadjusted"

    def test_count_mismatch_with_the_ledger_row_is_not_papered_over(self):
        """净值行说当天开了 2 笔、账本记录里只找到 1 笔 ⇒ 不知道漏了谁，不还原。"""
        _, nav, info = SD.pick_signal_and_nav([_sig("T", self.DAY, 0.77, 0.925)], self._eq(102546.5, opened=2), "T", self.DAY,
                                              [_rec("T", "short", 39, 1.58)])
        assert nav == 102546.5 and info["basis"] == "post_open_unadjusted" and info["reason"].startswith("opened_today_mismatch")

    def test_no_row_on_the_day_takes_the_latest_earlier_row_never_a_later_one(self):
        eq = [{"date": "2026-09-26", "nav": 100000.0}, {"date": "2026-10-05", "nav": 999999.0}]
        _, nav, info = SD.pick_signal_and_nav([], eq, "T", self.DAY, [])
        assert nav == 100000.0 and info["basis"] == "latest_on_or_before" and info["nav_date"] == "2026-09-26"

    def test_no_usable_row_is_none_not_zero(self):
        for eq in ([], [{"date": "2026-10-05", "nav": 5.0}], [{"date": "2026-09-26", "nav": None}, {"date": "2026-09-27", "nav": 0}]):
            _, nav, info = SD.pick_signal_and_nav([], eq, "T", self.DAY, [])
            assert nav is None and info["basis"] == "none" and info["reason"] == "no_equity_row"

    def test_nav_basis_text_is_written_for_every_kind(self):
        kinds = [SD.sizing_nav([_sig("T", self.DAY, 0.77, 0.925)], self._eq(102546.5), [_rec("T", "short", 39, 1.58)], self.DAY),
                 SD.sizing_nav([], self._eq(102546.5, opened=0)[:1], [], self.DAY),
                 SD.sizing_nav([], self._eq(102546.5), [_rec("T", "short", 39, 1.58)], self.DAY),
                 SD.sizing_nav([], [], [], self.DAY)]
        out = [SC.nav_basis(i) for i in kinds]
        assert [o["kind"] for o in out] == ["pre_open", "latest_on_or_before", "post_open_unadjusted", "none"]
        assert all(o["text"] for o in out) and out[3]["label"] is None
        assert "$449" in out[0]["text"] or "$448" in out[0]["text"]                      # 加回的滑点写进文案（448.5 取整）
        assert "少 1 张" in out[2]["text"] and "近似" in out[1]["text"]
        assert SC.nav_basis(None) is None and _cmp(nav_info=None)["nav_basis"] is None
        assert "nav_basis" not in [c["code"] for c in _cmp(nav_info=None)["caveats"]]


class TestLoaders:
    """loader 层（读文件）单独钉：原因码、点时点、不凭空建目录。此前只有服务层测试间接碰到、变异后仍全绿。"""

    def test_quote_set_reasons_are_distinct(self, tmp_path):
        root = tmp_path / "d"
        (root / "cache").mkdir(parents=True)
        f = root / "cache" / "options_snapshot_T_2026-09-29.json"
        assert SD.load_quote_set("T", "2026-09-29", root) == (None, "snapshot_missing")
        f.write_text("{not json", encoding="utf-8")
        assert SD.load_quote_set("T", "2026-09-29", root) == (None, "snapshot_unreadable")
        for body in ({"ticker": "T"}, {"quote_set": "x"}, {"quote_set": None}, [1, 2]):   # 真实缓存里约 6 成的快照没有 quote_set
            f.write_text(json.dumps(body), encoding="utf-8")
            assert SD.load_quote_set("T", "2026-09-29", root) == (None, "quote_set_unavailable"), body
        f.write_text(json.dumps({"quote_set": _qs()}), encoding="utf-8")
        qs, why = SD.load_quote_set("T", "2026-09-29", root)
        assert why is None and qs["selected_expiry"] == "2026-10-30" and qs["contracts"]["atm_call"]["strike"] == 24.5
        # 文件名按票 + 日期精确匹配
        assert SD.load_quote_set("ZZ", "2026-09-29", root) == (None, "snapshot_missing")
        assert SD.load_quote_set("T", "2026-09-30", root) == (None, "snapshot_missing")

    def test_hist_moves_reasons_are_distinct(self, tmp_path):
        root = tmp_path / "d"
        (root / "earnings_cache").mkdir(parents=True)
        f = root / "earnings_cache" / "T_moves.json"
        assert SD.load_hist_moves("T", root) == (None, "no_history")
        for text in ("{not json", "[1]", json.dumps({"n": 0}), json.dumps({"moves": "x"}), json.dumps({"moves": {"a": 1}})):
            f.write_text(text, encoding="utf-8")
            assert SD.load_hist_moves("T", root) == (None, "history_unreadable"), text
        f.write_text(json.dumps({"moves": RAW}), encoding="utf-8")
        assert SD.load_hist_moves("T", root) == (RAW, None)
        f.write_text(json.dumps({"moves": []}), encoding="utf-8")
        assert SD.load_hist_moves("T", root) == ([], None)                               # 空列表不是「读不了」；上游把它报成 no_history

    def test_cache_dir_resolution_and_no_mkdir(self, tmp_path, monkeypatch):
        from hive_logger import PATHS
        root = tmp_path / "d"
        root.mkdir()
        assert SD._cache_dir(root) == root / "cache"
        monkeypatch.setenv("ALPHA_HIVE_CACHE_DIR", str(tmp_path / "envcache"))
        assert SD._cache_dir(None) == tmp_path / "envcache" == PATHS.cache_dir_unmade()    # 缺省走写入方同一个解析（认环境变量）
        for call in (lambda: SD.load_quote_set("T", "2026-09-29", root), lambda: SD.load_quote_set("T", "2026-09-29"),
                     lambda: SD.load_hist_moves("T", root), lambda: SD.load_signal_and_nav("T", "2026-09-29", root)):
            call()
        assert list(root.iterdir()) == [], "只读 loader 不许在数据根下凭空建出任何东西"
        assert not (tmp_path / "envcache").exists(), "缺省路径找不到也不许建缓存目录"

    def test_signal_and_nav_loader_reads_four_files_and_ignores_the_future(self, tmp_path):
        root = _write_t_ledger(tmp_path / "d")
        ld = root / SD.LEDGER_DIRNAME
        with open(ld / "equity_curve.jsonl", "a", encoding="utf-8") as fh:               # 坏行不能拖垮读取
            fh.write("{broken\n")
        sig, nav, info = SD.load_signal_and_nav("T", "2026-09-29", root)
        assert sig["ticker"] == "T" and nav == pytest.approx(102995.0, abs=0.01) and info["basis"] == "pre_open"
        assert SD.load_signal_and_nav("ZZ", "2026-09-29", root)[0] is None              # 没有这只票的信号行
        assert SD.load_signal_and_nav("T", "2026-09-28", root)[1] == 100800.0           # 之前一天：当天没开仓，行即开仓前 NAV
        _, nav0, info0 = SD.load_signal_and_nav("T", "2026-09-26", root)                # 比所有净值行都早
        assert nav0 is None and info0["basis"] == "none" and info0["reason"] == "no_equity_row"
        # 之后的 999999 从不被取到：09-30 没有净值行 ⇒ 取 09-29 那行（近似），而不是 10-05 的
        _, nav2, info2 = SD.load_signal_and_nav("T", "2026-09-30", root)
        assert nav2 == 102546.5 and info2["basis"] == "latest_on_or_before"

    def test_loader_finds_that_days_openings_in_the_closed_trades_too(self, tmp_path):
        """T 之后平掉了（在 closed_trades.jsonl 里、不在 positions.jsonl 里）——入场日的 NAV 照样要还原（真账本的 TSLA / NFLX 就是）。"""
        root = _write_t_ledger(tmp_path / "d")
        ld = root / SD.LEDGER_DIRNAME
        _jsonl(ld / "closed_trades.jsonl", [{**T_POSITION, "exit_date": "2026-10-22", "pnl_usd": 10.0}])
        _jsonl(ld / "positions.jsonl", [])
        _, nav, info = SD.load_signal_and_nav("T", "2026-09-29", root)
        assert nav == pytest.approx(102995.0, abs=0.01) and info["basis"] == "pre_open" and info["opened"] == ["T"]

    def test_missing_ledger_files_give_none_not_a_crash(self, tmp_path):
        sig, nav, info = SD.load_signal_and_nav("T", "2026-09-29", tmp_path / "nowhere")
        assert (sig, nav, info["basis"]) == (None, None, "none")


class TestBoundaries:
    """比较的边界约定与此前没人钉过的分支（二次检查 [6]：这些变异过去全部存活）。"""

    def test_odd_sample_median(self):
        h = _cmp(raw_moves=RAW[:5])["hist"]                                              # 5 个：|涨跌| 排序 1.8235 2.8207 3.1447 5.4894 9.2609，取正中间
        assert h["n"] == 5 and h["median_abs_pct"] == pytest.approx(3.1447)
        h3 = _cmp(raw_moves=RAW[:3])["hist"]                                             # 3 个：2.8207 3.1447 9.2609
        assert h3["n"] == 3 and h3["median_abs_pct"] == pytest.approx(3.1447)

    def test_a_move_exactly_on_the_band_edge_is_outside(self):
        base = _cmp()["structures"]["straddle"]
        for edge in ("lo_pct", "hi_pct"):
            r = _cmp(raw_moves=[{"earnings_date": "2026-01-01", "move_pct": base["band"][edge]}])
            assert r["structures"]["straddle"]["hist_outside"]["k"] == 1, edge           # 带外 = ≤ 下沿 或 ≥ 上沿（含等于）

    def test_spread_flag_is_strictly_above_the_limit(self):
        limit = _cmp()["structures"]["straddle"]["max_spread_frac"]
        assert _cmp(max_spread_frac=limit)["structures"]["straddle"]["flags"] == []      # 刚好等于闸门：不标
        assert [f["code"] for f in _cmp(max_spread_frac=limit - 1e-9)["structures"]["straddle"]["flags"]] == ["wide_spread"]

    def test_after_hours_only_when_the_snapshot_is_known_to_be_after_hours(self):
        codes = lambda qs: [c["code"] for c in _cmp(qs)["caveats"]]  # noqa: E731
        assert "after_hours" not in codes({**_qs(), "market_open": None})                # 时段不明 ≠ 盘后
        assert "after_hours" not in codes({**_qs(), "market_open": True}) and "after_hours" in codes(_qs())

    def test_after_hours_caveat_does_not_state_a_universal_multiplier(self):
        """依据只有一个样本（COST 25Δ 2026-09-03 一天一档）：不许写成「实测约为盘中的 2–3 倍」，要标待验证。"""
        text = SC.CAVEATS["after_hours"]
        assert "待验证" in text and "COST" in text
        for banned in ("2–3", "2-3", "2~3", "实测约为"):
            assert banned not in text, banned

    def test_the_card_is_labelled_seller_view(self):
        r = _cmp()
        assert "seller_view" in [c["code"] for c in r["caveats"]] and "卖方视角" in SC.CAVEATS["seller_view"]


class TestCompareCardFrontEnd:
    """前端行为靠源码形状钉（本仓前端测试的既有做法：读 straddle.js、去掉 // 注释行再断言）。
    每条都对应一个已确认的缺陷，改回去会红；行为本身另在浏览器里手测过。"""

    def test_heading_names_the_ticker_and_the_day(self):
        card = _fn_src(_straddle_js(), "compareCard")
        head = next(ln for ln in card.splitlines() if '"cmp-head"' in ln)
        assert "r.ticker" in head and "r.as_of" in head
        unavailable = next(ln for ln in card.splitlines() if "!r.available" in ln)
        assert "r.ticker" in unavailable and "r.as_of" in unavailable                     # 不可得的卡也说是哪只票

    def test_stale_responses_are_dropped_in_both_the_success_and_the_failure_path(self):
        src = _fn_src(_straddle_js(), "loadCompare")
        assert src.index("el.dataset.cmpSeq = seq") < src.index("await api(")             # 令牌在发请求之前领
        after = src[src.index("await api("):]
        ok_path, err_path = after[:after.index("catch")], after[after.index("catch"):]
        assert "current()" in ok_path and ok_path.index("current()") < ok_path.index("fill(el, compareCard(")
        assert "current()" in err_path and err_path.index("current()") < err_path.index("fill(el")
        assert "el.dataset.cmpSeq === seq" in src and "el.isConnected" in src             # 令牌相等 + 容器仍在页面上

    def test_a_failed_load_offers_a_retry_that_asks_again(self):
        src = _fn_src(_straddle_js(), "loadCompare")
        err_path = src[src.index("catch"):]
        assert "重试" in err_path and "loadCompare(el, ticker, asOf, opts)" in err_path

    def test_lazy_load_flag_is_set_only_after_a_usable_card(self):
        import re
        pos = _fn_src(_straddle_js(), "positionCard")
        ont = pos[pos.index("ontoggle"):]
        sets = [m.start() for m in re.finditer(r'dataset\.loaded\s*=\s*"1"', ont)]
        assert len(sets) == 1, "dataset.loaded 只在一处置位"
        assert ont.index("await loadCompare(") < sets[0], "标志必须在请求成功之后才置位（失败后再展开要能重试）"
        assert 'out === "ok"' in ont[ont.index("await loadCompare("):sets[0] + 40]
        assert 'dataset.busy' in ont                                                      # 加载中重复展开不发并发请求

    def test_long_positions_get_a_note_instead_of_the_seller_view(self):
        pos = _fn_src(_straddle_js(), "positionCard")
        assert 'p.side === "long"' in pos
        long_branch, details_branch = pos.split('p.side === "long"')[1].split('h("details"', 1)
        assert "买入跨式" in long_branch and "卖方视角" in long_branch
        assert "loadCompare" not in long_branch and "loadCompare(" in details_branch      # 买跨式不发对比请求，卖跨式照旧

    def test_the_position_card_passes_the_ledgers_contract_count(self):
        pos = _fn_src(_straddle_js(), "positionCard")
        assert "ledgerContracts: p.contracts" in pos
        assert "ledgerContracts" in _fn_src(_straddle_js(), "compareCard")

    def test_nav_basis_is_shown_in_the_sizing_row(self):
        assert "nav_basis" in _fn_src(_straddle_js(), "compareCard")


def test_module_is_pure():
    """strangle_compare 不读写文件、不联网、不 import 卖权模块（外部函数由服务层注入）。"""
    tree = ast.parse(Path(SC.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "math", "typing"}, imported
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in {"open", "print"}]
    assert math.isfinite(SC.wilson(1, 2)[0])
