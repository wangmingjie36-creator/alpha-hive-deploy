"""Alpha Bot「标的 · 成交」子页（成交榜，v0.45.776）的守卫。全部离线（合成链注入 `fetch_fn`）。

每组回答一句「谁会红」：
  · 排名：按成交量从高到低、只收有成交的、筛→排→截断、汇总不受截断影响（`TestSelect`）；
  · 不当 0：缺成交量 / 认不出的合约单独计数、OI 为 0 时比值写空、没有中间价时约成交额写空（`TestNoFakeZero`）；
  · 不另取链：标的页刚现算过 ⇒ 成交榜不多打一次取数；这次现算没取到链 ⇒ 不拿上一次的链冒充（`TestReusesLiveChain`）；
  · 参数先验：错参数 ⇒ 400 + 参数名、不白拉链（`TestParams`）；
  · 纯度、接线（子页 tab、帮助页章节、规则常量出自 /api/method）（`TestPurityAndWiring`）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from alphabot import synthetic as syn
from alphabot import volume_board as VB
from alphabot.service import AlphaBotService, BadRequest, RAW_KEEP

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "alphabot" / "static"
AS_OF = "2026-09-01"


def _svc(fetch=None, **kw):
    kw.setdefault("bars_fn", lambda t: {"available": True, "source": "demo", "bars": syn.demo_bars(t, AS_OF)})
    return AlphaBotService(fetch_fn=fetch or (lambda t, as_of=None: syn.demo_fetch(t, as_of=AS_OF)), **kw)


def _client(svc, port=8765):
    from starlette.testclient import TestClient
    from alphabot.server import create_app
    return TestClient(create_app(svc, port=port), base_url=f"http://127.0.0.1:{port}")


def _c(cp, k, vol, oi=1000.0, bid=1.0, ask=1.2, exp="2026-09-18", **kw):
    return {"symbol": f"X{exp}{cp}{k}", "cp": cp, "strike": k, "expiry": exp, "dte": 17, "volume": vol, "oi": oi,
            "bid": bid, "ask": ask, "iv": 0.4, "delta": 0.3 if cp == "C" else -0.3, **kw}


def _raw(contracts, S=100.0):
    return {"as_of": AS_OF, "underlying_price": S, "contracts": contracts, "session_live": True}


P = VB.parse_params({})


class TestSelect:
    def test_ranked_by_volume_desc_and_untraded_dropped(self):
        full = VB.build(_raw([_c("C", 105, 50), _c("P", 95, 900), _c("C", 110, 0), _c("P", 90, 300)]))
        out = VB.select(full, P)
        assert [r["volume"] for r in out["rows"]] == [900, 300, 50]
        assert [r["rank"] for r in out["rows"]] == [1, 2, 3]
        assert out["summary"]["n_traded"] == 3

    def test_summary_covers_whole_scope_not_just_listed(self):
        rows = [_c("C", 100 + i, 10 + i) for i in range(60)] + [_c("P", 90, 5)]
        out = VB.select(VB.build(_raw(rows)), VB.parse_params({"limit": "25"}))
        assert len(out["rows"]) == 25
        tot = sum(10 + i for i in range(60)) + 5
        assert out["summary"]["total_volume"] == tot
        assert out["summary"]["top_volume_share_pct"] == round(sum(r["volume"] for r in out["rows"]) / tot * 100, 1)
        assert out["rows"][0]["volume_share_pct"] == round(69 / tot * 100, 2)

    def test_side_and_expiry_filters(self):
        rows = [_c("C", 105, 100), _c("P", 95, 200), _c("P", 95, 999, exp="2026-10-16")]
        full = VB.build(_raw(rows))
        out = VB.select(full, VB.parse_params({"side": "put", "expiry": "2026-09-18"}))
        assert [(r["side"], r["volume"]) for r in out["rows"]] == [("put", 200)]
        assert out["summary"]["call_volume"] == 0 and out["summary"]["put_volume"] == 200
        with pytest.raises(VB.ParamError) as ei:
            VB.select(full, VB.parse_params({"expiry": "2030-01-18"}))
        assert ei.value.param == "expiry"

    def test_vol_oi_sort_pushes_small_volume_and_zero_oi_last(self):
        lo = VB.RULES["vol_oi_min_volume"]
        rows = [_c("C", 101, lo - 1, oi=1),          # 比值最大，但成交量太小
                _c("C", 102, 5000, oi=0),             # OI 为 0：比值空
                _c("C", 103, 2000, oi=1000),          # 2×
                _c("C", 104, 600, oi=100)]            # 6×
        out = VB.select(VB.build(_raw(rows)), VB.parse_params({"sort": "vol_oi"}))
        assert [r["strike"] for r in out["rows"]][:2] == [104, 103]
        assert {r["strike"] for r in out["rows"][2:]} == {101, 102}

    def test_notional_estimate_and_otm_sign(self):
        out = VB.select(VB.build(_raw([_c("C", 110, 10, bid=2.0, ask=3.0), _c("P", 110, 5)])), P)
        call = next(r for r in out["rows"] if r["side"] == "call")
        put = next(r for r in out["rows"] if r["side"] == "put")
        assert call["notional_est"] == 10 * 2.5 * 100
        assert call["otm_pct"] == 10.0 and put["otm_pct"] == -10.0      # 110 的 put 在 100 现价下是实值

    def test_no_volume_today_is_reported_not_empty_success(self):
        out = VB.select(VB.build(_raw([_c("C", 105, 0), _c("P", 95, 0)])), P)
        assert out["rows"] == [] and out["reason"] == "no_volume_today"


class TestNoFakeZero:
    def test_missing_volume_and_unparseable_are_counted(self):
        rows = [_c("C", 105, None), _c("X", 100, 50), _c("P", 95, 10)]
        full = VB.build(_raw(rows))
        assert full["counts"] == {"n_contracts": 3, "n_unparseable": 1, "n_volume_missing": 1, "n_traded": 1}

    def test_zero_oi_ratio_and_missing_mid_are_none(self):
        out = VB.select(VB.build(_raw([_c("C", 105, 50, oi=0, bid=None), _c("P", 95, 20, bid=1.5, ask=1.0)])), P)
        r0, r1 = out["rows"]
        assert r0["vol_oi"] is None and r0["notional_est"] is None
        assert r1["notional_est"] is None                       # ask < bid：不是有效报价

    @pytest.mark.parametrize("raw,reason", [(None, "no_chain"), ({"underlying_price": None, "contracts": [1]},
                                            "underlying_price_unavailable"), (_raw([]), "no_contracts")])
    def test_unavailable_reasons_are_registered(self, raw, reason):
        full = VB.build(raw)
        assert full == {"data_available": False, "reason": reason}
        assert reason in VB.REASONS


class TestReusesLiveChain:
    def _counting(self):
        calls = []

        def fetch(t, as_of=None):
            calls.append(t)
            return syn.demo_fetch(t, as_of=AS_OF)
        return fetch, calls

    def test_board_after_live_does_not_fetch_again(self):
        fetch, calls = self._counting()
        svc = _svc(fetch)
        assert svc.live("NVDA")["data_available"]
        n = len(calls)
        out = svc.volume_board("NVDA")
        assert out["data_available"] and out["rows"]
        assert len(calls) == n, "成交榜另取了一次链"
        # 和标的页是同一份链：总成交量 = 那份链逐合约加总
        raw, _ = syn.demo_fetch("NVDA", as_of=AS_OF)
        assert out["summary"]["total_volume"] == sum(c["volume"] for c in raw["contracts"] if c["volume"])

    def test_force_refetches(self):
        fetch, calls = self._counting()
        svc = _svc(fetch)
        svc.volume_board("NVDA")
        n = len(calls)
        svc.volume_board("NVDA", {"force": "1"})
        assert len(calls) > n

    def test_evicted_chain_triggers_one_recompute(self):
        fetch, calls = self._counting()
        svc = _svc(fetch)
        others = ["AAPL", "MSFT", "TSLA", "AMD", "META"][:RAW_KEEP]
        for t in ["NVDA", *others]:
            svc.live(t)
        assert "NVDA" not in svc._raw_last
        n = len(calls)
        out = svc.volume_board("NVDA")
        assert out["data_available"] and len(calls) == n + 1
        assert len(svc._raw_last) <= RAW_KEEP

    def test_failed_refetch_does_not_reuse_previous_chain(self):
        state = {"ok": True}

        def fetch(t, as_of=None):
            return syn.demo_fetch(t, as_of=AS_OF) if state["ok"] else (None, "cboe_http_error")
        svc = _svc(fetch)
        assert svc.volume_board("NVDA")["data_available"]
        state["ok"] = False
        out = svc.volume_board("NVDA", {"force": "1"})
        assert out["data_available"] is False and out["fetch_failed"] is True
        assert "NVDA" not in svc._raw_last


    def test_previous_chain_stays_readable_while_recomputing(self):
        """二次检查 #2：现算一开始就清旧链 ⇒ 轮询线程重算同一只票的那几秒里，成交榜读不到链、以为被挤掉、再多打一次取数。
        旧链要留到这次取到新链（或确定取不到）为止。"""
        seen = []
        svc = None

        def fetch(t, as_of=None):
            if svc is not None and svc._raw_last:
                seen.append(t in svc._raw_last)
            return syn.demo_fetch(t, as_of=AS_OF)
        svc = _svc(fetch)
        svc.live("NVDA")
        seen.clear()
        svc.live("NVDA", force=True)
        assert seen == [True], "重算进行中旧链已被清掉"

    def test_ticker_refresh_then_board_fetches_once(self):
        """二次检查 #1：「重新拉取」= 标的页 live(force) + 成交榜请求。页面不带 force ⇒ 整个过程只取一次链。"""
        calls = []
        svc = _svc(lambda t, as_of=None: calls.append(t) or syn.demo_fetch(t, as_of=AS_OF))
        svc.live("NVDA")
        calls.clear()
        svc.live("NVDA", force=True)                 # 顶部「重新拉取」
        svc.volume_board("NVDA")                     # 随后本页自己的请求（不带 force）
        assert calls == ["NVDA"]
        js = (STATIC / "pages" / "volume.js").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in js.splitlines() if not ln.lstrip().startswith("//"))
        assert "force" not in code, "成交榜页又带上了 force：同一次「重新拉取」会打两遍 CBOE"

class TestParams:
    @pytest.mark.parametrize("q,param", [({"side": "both"}, "side"), ({"sort": "iv"}, "sort"), ({"limit": "7"}, "limit"),
                                         ({"limit": "x"}, "limit"), ({"expiry": "9/18"}, "expiry"), ({"foo": "1"}, "foo")])
    def test_bad_params_rejected_before_fetch(self, q, param):
        calls = []
        svc = _svc(lambda t, as_of=None: calls.append(t) or syn.demo_fetch(t, as_of=AS_OF))
        with pytest.raises(BadRequest) as ei:
            svc.volume_board("NVDA", q)
        assert ei.value.param == param and calls == []

    def test_http_route(self):
        c = _client(_svc())
        r = c.get("/api/volume-board/NVDA?side=call&sort=notional&limit=25")
        assert r.status_code == 200
        body = r.json()
        assert body["params"] == {"side": "call", "sort": "notional", "limit": 25, "expiry": None}
        assert body["rows"] and all(x["side"] == "call" for x in body["rows"])
        ests = [x["notional_est"] for x in body["rows"] if x["notional_est"] is not None]
        assert ests == sorted(ests, reverse=True)
        bad = c.get("/api/volume-board/NVDA?sort=iv")
        assert bad.status_code == 400 and bad.json().get("param") == "sort"


# ─────────────────────────────── 纯度与接线

ALLOWED_IMPORTS = {"__future__", "math", "typing"}
FORBIDDEN = {"open", "Path", "os", "time", "datetime", "now", "today", "requests", "urlopen", "__import__"}


def _violations(src: str):
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names if a.name.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] not in ALLOWED_IMPORTS:
            out.append(n.module)
        elif isinstance(n, ast.Name) and n.id in FORBIDDEN:
            out.append(n.id)
        elif isinstance(n, ast.Attribute) and n.attr in FORBIDDEN:
            out.append(n.attr)
    return out


class TestPurityAndWiring:
    SRC = ROOT / "alphabot" / "volume_board.py"

    def test_module_is_pure(self):
        assert _violations(self.SRC.read_text(encoding="utf-8")) == []

    @pytest.mark.parametrize("inject", ["import os\n", "import sell_strike_levels\n", "x = open('f')\n",
                                        "from datetime import datetime\n"])
    def test_purity_has_teeth(self, inject):
        assert _violations(self.SRC.read_text(encoding="utf-8") + inject)

    def test_ticker_tab_and_help_section(self):
        tk = (STATIC / "pages" / "ticker.js").read_text(encoding="utf-8")
        assert '["volume", "成交"]' in tk and 'volume: "volume"' in tk
        assert re.search(r"\{[^}]*\bvolume\b[^}]*\}\[tab\]", tk)
        assert '  ["volume", "标的 · 成交榜"' in (STATIC / "pages" / "help.js").read_text(encoding="utf-8")

    def test_page_reads_rules_from_server_not_hardcoded(self):
        js = (STATIC / "pages" / "volume.js").read_text(encoding="utf-8")
        assert "R.vol_oi_min_volume" in js                         # 阈值只经 rules 给页面
        assert not re.search(r"不足\s*\d+\s*张", js)               # 不在页面里写死这个数
        m = _client(_svc()).get("/api/method").json()["volume_board"]
        assert m["vol_oi_min_volume"] == VB.RULES["vol_oi_min_volume"] and m["raw_keep"] == RAW_KEEP
        assert set(m["reasons"]) == set(VB.REASONS)
