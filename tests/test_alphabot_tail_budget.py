"""Alpha Bot 对冲页「组合尾部预算」面板（v0.45.649）：`alphabot/hedge.py::tail_overview` + `/api/hedge` 的 `tail` +
`static/pages/hedge_tail_budget.js`。

  · 整理（`TestOverview`）：最新一份**带 `tail` 的**审计才是 `latest`（此前的审计没有这一节——数出来、不当成「没有尾部」）；
    算不出来的那天在曲线里是 None 不是 0；影子闸记录新的在前；逐标的表截断；
  · 经服务层（`TestService`）：真实账本夹具（经 `load_history` 放行闸）里写进 `tail`，接口原样带出；只读（目录指纹不变）；演示可用；
  · 前端（`TestFrontend`）：文案取自服务端、不在浏览器里排序、明说「只记录不真跳过 / 不是反事实」。
全部离线。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from alphabot import hedge as HG
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService
from tests.test_alphabot_hedge import DIRNAME, NOW, _client, _fingerprint, _svc, _write
from tests.test_tail_budget import _audit as _mc_audit
from tests.test_tail_budget import opt as _mc_opt
from tests.test_tail_budget import quad_stress

STATIC = Path(__file__).resolve().parent.parent / "alphabot" / "static" / "pages"


def tail(loss, *, entries=None, available=True, reason=None, n=3):
    if not available:
        return {"available": False, "reason": reason}
    rows = [{"ticker": f"T{i}", "sector": "S", "at_worst": -100.0 * (i + 1), "at_worst_pct": -0.1 * (i + 1)} for i in range(n)]
    return {"available": True, "loss_pct_nav": loss, "nav": 100_000.0, "alert_pct": 20.0, "partial": False, "excluded": [],
            "worst": {"spot_pct": -10.0, "iv_pts": 20.0, "pnl": -loss * 1000, "pct_nav": -loss},
            "downside_worst": None, "by_ticker": rows, "by_sector": [], "residual_usd": 0.0,
            "brief": {"breach": loss > 20.0},
            "gate_shadow": {"budgets_pct": ["15", "20"], "basis": "b", "entries": entries or []}}


def entry(tk, **kw):
    return {"ticker": tk, "sector": "S", "side": "short", "applies": True, "evaluable": True, "reason": None, "reason_zh": None,
            "loss_before_pct": 10.0, "loss_after_pct": 16.0, "marginal_pct": 6.0, "would_skip": {"15": True, "20": False}, **kw}


class TestOverview:
    AUDITS = {"2026-10-20": {}, "2026-10-21": {"tail": tail(15.0)},
              "2026-10-22": {"tail": tail(None, available=False, reason="exception:ValueError")},
              "2026-10-23": {"tail": tail(23.0, entries=[entry("X")])},
              "2026-10-26": {"tail": tail(21.0, entries=[entry("Y"), entry("Z", evaluable=False, reason="not_in_book_order")])}}

    def test_latest_is_the_newest_audit_with_a_usable_tail(self):
        t = HG.tail_overview(self.AUDITS)
        assert t["latest"]["date"] == "2026-10-26" and t["latest"]["loss_pct_nav"] == 21.0
        assert t["n_audits_without_tail"] == 1                        # 10-20 是该功能之前的审计：数出来、不算「没有尾部」

    def test_an_unavailable_day_is_none_in_the_series_not_zero(self):
        s = {x["date"]: x for x in HG.tail_overview(self.AUDITS)["series"]}
        assert s["2026-10-22"]["loss_pct_nav"] is None and s["2026-10-22"]["reason"] == "exception:ValueError"
        assert s["2026-10-21"]["loss_pct_nav"] == 15.0 and s["2026-10-23"]["breach"] is True and s["2026-10-21"]["breach"] is False
        assert "2026-10-20" not in s

    def test_an_unusable_newest_audit_does_not_hide_the_older_usable_one(self):
        audits = {"2026-10-21": {"tail": tail(15.0)}, "2026-10-22": {"tail": tail(None, available=False, reason="nav_unavailable")}}
        assert HG.tail_overview(audits)["latest"]["date"] == "2026-10-21"

    def test_gate_log_is_newest_first_with_dates_and_counts_everything(self):
        t = HG.tail_overview(self.AUDITS)
        # 整体倒过来：新的日期在前；同一天里后开的在前（Z 在 Y 之后）
        assert [(e["date"], e["ticker"]) for e in t["gate_log"]] == [("2026-10-26", "Z"), ("2026-10-26", "Y"), ("2026-10-23", "X")]
        assert t["n_gate_entries"] == 3
        assert t["budgets_pct"] == ["15", "20"] and t["gate_basis"] == "b"
        z = next(e for e in t["gate_log"] if e["ticker"] == "Z")
        assert z["evaluable"] is False and z["reason"] == "not_in_book_order"

    def test_by_ticker_is_truncated_for_the_page(self):
        audits = {"2026-10-21": {"tail": tail(15.0, n=40)}}
        assert len(HG.tail_overview(audits)["latest"]["by_ticker"]) == HG.TAIL_BY_TICKER_SHOWN

    def test_no_tail_anywhere_is_an_honest_empty_state(self):
        t = HG.tail_overview({"2026-10-20": {}, "2026-10-21": {"aggregate": {}}})
        assert t["latest"] is None and t["series"] == [] and t["gate_log"] == [] and t["n_audits_without_tail"] == 2
        assert HG.tail_overview({})["latest"] is None

    def test_series_and_log_are_bounded(self):
        audits = {f"2026-{m:02d}-{d:02d}": {"tail": tail(10.0, entries=[entry("X")])} for m in (1, 2, 3, 4) for d in range(1, 29)}
        t = HG.tail_overview(audits)
        assert len(t["series"]) == HG.TAIL_SERIES_SHOWN and len(t["gate_log"]) == HG.TAIL_GATE_SHOWN and t["n_gate_entries"] == 112


@pytest.fixture
def root_with_tail(tmp_path):
    root = _write(tmp_path / "data")
    f = root / DIRNAME / "greeks_2026-10-09.json"
    a = json.loads(f.read_text(encoding="utf-8"))
    a["tail"] = syn._demo_tail(143_635.99, 3, [entry("XOM")])
    f.write_text(json.dumps(a), encoding="utf-8")
    return root


class TestService:
    def test_hedge_endpoint_carries_the_tail_overview_through_the_history_gate(self, root_with_tail):
        d = _svc(root_with_tail).hedge()
        t = d["tail"]
        assert t["latest"]["date"] == "2026-10-09" and len(t["latest"]["by_ticker"]) == 6
        assert t["n_audits_without_tail"] == 1                           # 10-08 的审计没有这一节
        assert [e["ticker"] for e in t["gate_log"]] == ["XOM"] and t["budgets_pct"] == ["15", "20"]

    def test_the_gate_still_hides_blocked_audits_including_their_tail(self, root_with_tail):
        """10-07 那份无定价证明的审计会被历史闸拦下；即使它带 tail 也不许露出来。"""
        f = root_with_tail / DIRNAME / "greeks_2026-10-07.json"
        a = json.loads(f.read_text(encoding="utf-8"))
        a["tail"] = syn._demo_tail(143_635.99, 0, [entry("LEAK")])
        f.write_text(json.dumps(a), encoding="utf-8")
        body = json.dumps(_svc(root_with_tail).hedge()["tail"])
        assert "LEAK" not in body and "2026-10-07" not in body

    def test_read_only_the_data_root_is_untouched(self, root_with_tail):
        before = _fingerprint(root_with_tail)
        _svc(root_with_tail).hedge()
        c = _client(_svc(root_with_tail))
        assert c.get("/api/hedge").status_code == 200
        assert _fingerprint(root_with_tail) == before

    def test_route_returns_strict_json(self, root_with_tail):
        r = _client(_svc(root_with_tail)).get("/api/hedge")
        assert r.status_code == 200 and "tail" in r.json() and "NaN" not in r.text

    def test_demo_mode_has_a_tail_panel_with_a_breach_and_a_shadow_entry(self, tmp_path):
        d = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, state_dir=tmp_path, now_fn=lambda: NOW).hedge()
        t = d["tail"]
        assert t["latest"]["loss_pct_nav"] > t["latest"]["alert_pct"] and t["n_gate_entries"] == 1
        assert sum(x["at_worst"] for x in t["latest"]["by_ticker"]) == pytest.approx(t["latest"]["worst"]["pnl"], abs=0.1)


class TestModelCheckView:
    def pair_audits(self):
        return {"2026-10-15": _mc_audit(600.0, 1e5, 1000.0, [_mc_opt()]),
                "2026-10-16": _mc_audit(606.0, 99e3, 880.0, [_mc_opt(price=102.0, mid=6.2, iv=0.31)])}

    def test_tail_overview_carries_the_model_check_with_the_stress_function_it_is_given(self):
        t = HG.tail_overview(self.pair_audits(), quad_stress)
        mc = t["model_check"]
        assert mc["n_used"] == 1 and mc["pairs"][0]["pred_grid"] == pytest.approx(-0.5)
        assert t["latest"] is None                       # 这些审计没有 tail 一节：模型检验不依赖它（rows / nav / spy_price 本来就在审计里）
        assert HG.tail_overview(self.pair_audits())["model_check"]["pairs"][0]["pred_grid"] is None     # 没传压力函数：只缺这一项

    def test_overview_threads_the_stress_function_through(self):
        view = {"exists": True, "history": {"audits": self.pair_audits(), "equity": [], "trades": []}, "meta": {}, "opened": True}
        d = HG.build_overview(view, now_et=NOW, stress_fn=quad_stress)
        assert d["tail"]["model_check"]["pairs"][0]["pred_grid"] == pytest.approx(-0.5)

    def test_the_service_hands_over_the_real_stress_table(self, root_with_tail):
        import portfolio_greeks as pg
        assert _svc(root_with_tail)._stress_fn() is pg.stress_table

    def test_endpoint_reports_why_pairs_were_skipped(self, root_with_tail):
        mc = _svc(root_with_tail).hedge()["tail"]["model_check"]
        assert mc["n_pairs"] == 1 and mc["n_used"] == 0 and mc["skipped_by_reason"] == {"no_options": 1}
        assert mc["skip_reasons"]["no_options"] and "scope" in mc["notes"]

    def test_hedge_rules_show_the_derived_alert_lines(self, root_with_tail):
        r = _svc(root_with_tail).hedge_rules()
        assert r["gamma_alert_pct"] == 0.1 and r["vega_alert_pct"] == 0.5 and r["alert_lines_source"] == "derived"

    def test_demo_tail_has_slot_caps(self, tmp_path):
        t = AlphaBotService(fetch_fn=syn.demo_fetch, demo=True, state_dir=tmp_path, now_fn=lambda: NOW).hedge()["tail"]["latest"]
        assert t["slot_cap_pct"] == 3.33 and t["max_open"] == 6 and t["n_over_cap"] == len(t["over_cap_tickers"]) > 0
        assert all(x["slot"]["over_cap"] is (x["slot"]["loss_pct"] > x["slot"]["cap_pct"]) for x in t["by_ticker"])


class TestFrontend:
    BUDGET = (STATIC / "hedge_tail_budget.js").read_text(encoding="utf-8")
    HEDGE = (STATIC / "hedge.js").read_text(encoding="utf-8")

    def test_hedge_page_mounts_the_panel_with_the_data_it_already_fetches(self):
        assert 'import { tailBudgetPanel } from "./hedge_tail_budget.js"' in self.HEDGE
        assert "tailBudgetPanel(d.tail)" in self.HEDGE and "tailBudgetEl._after?.()" in self.HEDGE
        assert re.search(r"tailBudgetEl,\s*h\(\"div\"", self.HEDGE)

    def test_panel_says_what_it_is_and_what_it_is_not(self):
        for s in ("只读观测", "主观常数", "不真跳过", "不是反事实", "收盘标记价"):
            assert s in self.BUDGET, s

    def test_rows_are_never_resorted_in_the_browser(self):
        assert ".sort(" not in self.BUDGET                     # 顺序 = 服务端（最亏的在前、新的在前）

    def test_unevaluable_entries_show_the_servers_reason_not_a_blank_or_a_zero(self):
        assert "r.reason_zh" in self.BUDGET and "不适用（买入）" in self.BUDGET

    def test_slot_cap_and_model_check_are_on_the_page(self):
        for sn in ("单仓上限", "跨式腿 / 单仓上限", "压力模型 vs 实际净值", "预测·压力网格", "预测·Greeks 展开", "R²", "误差（预测−实际）",
                   "正 = 模型比实际乐观"):
            assert sn in self.BUDGET, sn
        assert "modelCheckBlock(T.model_check)" in self.BUDGET and "N.small_n" in self.BUDGET and "N.pred_grid" in self.BUDGET

    def test_model_check_is_shown_even_before_any_audit_carries_a_tail_section(self):
        """有牙：真实数据上线当天所有审计都没有 `tail`——模型检验若跟着空状态一起藏掉，就白做了（浏览器里实测踩到过）。"""
        empty = self.BUDGET[self.BUDGET.index("if (!L) {"):self.BUDGET.index("const chartEl")]
        assert "modelCheckBlock(T?.model_check)" in empty and "return node" in empty

    def test_the_two_actual_columns_agree_note_is_computed_not_asserted(self):
        assert "ledger_audit_max_diff_usd" in self.BUDGET and "（一致）" in self.BUDGET

    def test_empty_state_names_the_pre_feature_audits(self):
        assert "n_audits_without_tail" in self.BUDGET and "上线之前" in self.BUDGET
