"""v0.45.362 GEX 第 2 层守卫：每只标的一份 GEX 状态，就是政体路由用的那份；展示只读它。

守四件事（理由见 `gex_state` 模块 docstring）：

1. **不改分数**：步骤 0 喂给 `RegimeWeightAdjuster` 的 regime 仍是
   `dealer_gex.get("regime", "unknown") if dealer_gex else "unknown"`（v0.45.361 之前的表达式），
   各条路径（传入 / 现场算 / 出错 dict / 抛异常 / 没有 Scout 价）逐一核对；落盘的
   `gex_state.regime` 与喂进去的值相等。
2. **可得性标记如实**：出错 dict 的 `total_gex=0.0` 是哨兵值，状态里必须是 None + reason；
   NaN 的 total 不算可得（路由照旧，见模块 docstring 的「已知 bug、刻意不修」）。
3. **`build` 不抛**：它在步骤 0 的大 try 里，抛了会让权重退回基准 ⇒ 改分数。
4. **展示不回退主链**：没有状态时网站 / 日报显示「不可得」，不拿 OracleBee `gamma_exposure` 顶。

⚠️ 本文件不用 skip：全部合成数据、零外部依赖（现场算的那条路径把 `DealerGEXAnalyzer.analyze` 桩掉，不出网）。
"""
from __future__ import annotations

import copy

import pytest

import gex_state
from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents.queen_distiller import QueenDistiller
from tests.test_gex_modifier_disconnected import _agent_results, _pin_config


def _old_expr(dealer_gex):
    """v0.45.361 之前步骤 0 的原文（逐字抄下，作为「不改分数」的对照）。"""
    _gex_data = dealer_gex or {}
    return _gex_data.get("regime", "unknown") if _gex_data else "unknown"


def _ok(regime="positive_gex", total=12.5):
    return {"ticker": "SYN", "stock_price": 100.0, "total_gex": total, "gex_normalized_pct": 0.8,
            "regime": regime, "gex_flip": 95.0, "largest_call_wall": 110.0, "largest_put_wall": 90.0,
            "gex_profile": [], "chain_view": "cboe_full_expiries",
            "expiries_used": ["2026-10-02", "2026-10-09", "2026-10-16"], "vanna_stress": {}}


_ERR = {"error": "CBOE 全链视图不可得（不回退截断链）", "total_gex": 0.0}


# ════════════════════════════════════════════════════════════════════════════
# 纯函数
# ════════════════════════════════════════════════════════════════════════════

class TestBuild:

    @pytest.mark.parametrize("dg", [None, {}, _ERR, _ok(), _ok("negative_gex", -3.0),
                                    {"regime": "unknown"}, _ok(total=float("nan")),
                                    {"regime": "weird", "total_gex": 1.0}])
    def test_routing_regime_is_the_old_expression(self, dg):
        """变红的变异：改 `routing_regime` 的取法（例如「不可得 ⇒ unknown」、NaN ⇒ unknown）。"""
        assert gex_state.routing_regime(copy.deepcopy(dg) if dg else dg) == _old_expr(dg)
        assert gex_state.build(dg)["regime"] == _old_expr(dg)

    def test_available_state_carries_the_numbers(self):
        st = gex_state.build(_ok())
        assert st["available"] is True and st["reason"] is None
        assert st["total_gex"] == 12.5 and st["regime"] == "positive_gex"
        assert (st["gex_flip"], st["largest_call_wall"], st["largest_put_wall"]) == (95.0, 110.0, 90.0)
        assert st["chain_view"] == "cboe_full_expiries" and st["n_expiries"] == 3
        assert st["schema_version"] == gex_state.SCHEMA_VERSION
        assert st["score_channel"] == "regime_weight_routing"

    def test_error_sentinel_zero_is_not_a_number(self):
        """出错 dict 的 total_gex=0.0 是哨兵。变红的变异：把 `v if available else None` 改成 `v`。"""
        st = gex_state.build(_ERR)
        assert st["available"] is False
        assert st["reason"] == _ERR["error"]
        assert st["total_gex"] is None and st["regime"] == "unknown"

    @pytest.mark.parametrize("dg,why", [(None, "no_scout_price"), ({}, "exception:TimeoutError")])
    def test_missing_reason_is_recorded(self, dg, why):
        st = gex_state.build(dg, missing_reason=why)
        assert st["available"] is False and st["reason"] == why and st["total_gex"] is None

    def test_nan_total_is_unavailable_but_routing_unchanged(self):
        """NaN ≥ 0 为假 ⇒ 旧代码判负 gamma。状态如实记「不可得」，但 regime（路由用的）不许动。"""
        dg = _ok("negative_gex", float("nan"))
        st = gex_state.build(dg)
        assert st["available"] is False and st["reason"] == "non_finite_total_gex"
        assert st["regime"] == "negative_gex" == _old_expr(dg)
        assert st["total_gex"] is None

    def test_bad_regime_is_unavailable(self):
        st = gex_state.build({"regime": "weird", "total_gex": 1.0})
        assert st["available"] is False and st["reason"] == "bad_regime:weird"

    def test_build_never_raises(self, monkeypatch):
        """变红的变异：删掉 `build` 的 try/except（构造出错会连坐步骤 0 的整个 try ⇒ 权重退回基准）。"""
        def boom(*a, **k):
            raise KeyError("x")
        monkeypatch.setattr(gex_state, "_build", boom)
        st = gex_state.build(_ok("negative_gex"))
        assert st["available"] is False and st["reason"] == "state_build_failed:KeyError"
        assert st["regime"] == "negative_gex", "兜底分支也必须给出路由本来会拿到的 regime"

    def test_of_rejects_foreign_shapes(self):
        assert gex_state.of(None) is None
        assert gex_state.of({}) is None
        assert gex_state.of({"gex_state": {"schema_version": 999}}) is None
        st = gex_state.build(_ok())
        assert gex_state.of({"gex_state": st}) is st

    def test_display_line(self):
        assert gex_state.display_line(None).startswith("净 GEX：不可得")
        assert "no_scout_price" in gex_state.display_line(gex_state.build(None, missing_reason="no_scout_price"))
        line = gex_state.display_line(gex_state.build(_ok("negative_gex", -3.0)))
        assert line == "净 GEX：-3.00M$ · 负 gamma（放大波动）"


# ════════════════════════════════════════════════════════════════════════════
# QueenDistiller 步骤 0：落盘的状态 = 路由收到的 regime（各条路径）
# ════════════════════════════════════════════════════════════════════════════

def _run(monkeypatch, *, dealer_gex=None, lazy=None, scout_price=100.0):
    """跑一次 distill，记下 RegimeWeightAdjuster 收到的 gex_regime。

    `lazy`：现场算那条路径上 `DealerGEXAnalyzer.analyze` 的行为（返回值，或一个要抛的异常）。
    """
    _pin_config(monkeypatch)
    import gex_regime
    import advanced_analyzer

    seen = []
    orig = gex_regime.RegimeWeightAdjuster.adjust_weights

    def spy(self, *a, **k):
        seen.append(k.get("gex_regime"))
        return orig(self, *a, **k)
    monkeypatch.setattr(gex_regime.RegimeWeightAdjuster, "adjust_weights", spy)

    calls = []

    def fake_analyze(self, ticker, price):
        calls.append((ticker, price))
        if isinstance(lazy, BaseException):
            raise lazy
        return copy.deepcopy(lazy)
    monkeypatch.setattr(advanced_analyzer.DealerGEXAnalyzer, "analyze", fake_analyze)

    results = _agent_results("bullish")
    for r in results:
        if r["source"] == "ScoutBeeNova":
            r["details"] = {"price": scout_price}
    board = PheromoneBoard()
    for r in results:
        board.publish(PheromoneEntry(agent_id=r["source"], ticker="SYN", discovery=r["discovery"],
                                     source="test", self_score=float(r["score"]),
                                     direction=r["direction"], details={}))
    q = QueenDistiller(board, enable_llm=False, ml_model=None)
    out = q.distill("SYN", copy.deepcopy(results), dealer_gex=copy.deepcopy(dealer_gex))
    return out, seen, calls


class TestQueenPersistsTheRoutedState:

    @pytest.mark.parametrize("dg", [_ok(), _ok("negative_gex", -2.0), _ERR, {"regime": "unknown"}])
    def test_passed_in(self, monkeypatch, dg):
        out, seen, calls = _run(monkeypatch, dealer_gex=dg)
        assert calls == [], "传入了 dealer_gex 就不该现场算"
        assert seen == [_old_expr(dg)], f"路由收到的 regime 变了：{seen}"
        assert out["gex_state"]["regime"] == seen[0]
        assert out["gex_state"]["available"] is (dg is not _ERR and dg.get("regime") != "unknown")

    @pytest.mark.parametrize("lazy", [_ok(), _ok("negative_gex", -2.0), _ERR, None,
                                      TimeoutError("cboe")])
    def test_lazy_path(self, monkeypatch, lazy):
        out, seen, calls = _run(monkeypatch, lazy=lazy)
        assert calls == [("SYN", 100.0)]
        expected = _old_expr(None if isinstance(lazy, BaseException) else lazy)
        assert seen == [expected]
        st = out["gex_state"]
        assert st["regime"] == expected
        if isinstance(lazy, BaseException):
            assert st["available"] is False and st["reason"] == "exception:TimeoutError"
        elif lazy is None:
            assert st["available"] is False and st["reason"] == "not_computed"
        elif lazy is _ERR:
            assert st["available"] is False and st["total_gex"] is None
        else:
            assert st["available"] is True and st["total_gex"] == lazy["total_gex"]

    def test_no_scout_price(self, monkeypatch):
        out, seen, calls = _run(monkeypatch, scout_price=0.0)
        assert calls == [] and seen == ["unknown"]
        assert out["gex_state"]["available"] is False
        assert out["gex_state"]["reason"] == "no_scout_price"

    def test_scores_identical_whether_or_not_state_is_built(self, monkeypatch):
        """同一输入，把 `gex_state.build` 换成恒不可得的桩，分数与权重逐位不变 ⇒ 状态只是旁记，不进分。

        变红的变异：任何让路由改读 `gex_state`（例如 `_gex_regime_str = _gex_state["regime"] if
        _gex_state["available"] else "unknown"`）的改动——NaN 那条会翻。这里用正常 dict 验「旁记」，
        NaN 的路由不变由 `test_nan_routing_unchanged` 验。
        """
        a, _, _ = _run(monkeypatch, dealer_gex=_ok("negative_gex", -2.0))
        monkeypatch.setattr(gex_state, "build", lambda *x, **k: {"schema_version": 1, "available": False,
                                                                  "reason": "stub", "regime": "unknown"})
        b, _, _ = _run(monkeypatch, dealer_gex=_ok("negative_gex", -2.0))
        for k in ("final_score", "direction", "dimension_weights", "regime_weights_description"):
            assert k in a and k in b, f"蒸馏输出没有 {k}：对照没接上"
            assert a[k] == b[k], k

    def test_nan_routing_unchanged(self, monkeypatch, caplog):
        dg = _ok("negative_gex", float("nan"))
        out, seen, _ = _run(monkeypatch, dealer_gex=dg)
        assert seen == ["negative_gex"]
        assert out["gex_state"]["reason"] == "non_finite_total_gex"
        assert any("non_finite_total_gex" in r.getMessage() and r.levelname == "ERROR"
                   for r in caplog.records), "NaN 那条必须打 error 可见"


# ════════════════════════════════════════════════════════════════════════════
# 展示：读状态、不回退主链
# ════════════════════════════════════════════════════════════════════════════

def _ticker_result(state, oracle_gex=-7.7):
    # Scout 带价：`_detail` 缺价会去 yfinance 现取（离线闸会拦）
    return {"agent_details": {"OracleBeeEcho": {"details": {"gamma_exposure": oracle_gex,
                                                            "iv_rank": 40, "put_call_ratio": 0.9}},
                              "ScoutBeeNova": {"details": {"price": 100.0, "momentum_5d": 1.0}}},
            **({"gex_state": state} if state is not None else {})}


class TestDisplayReadsTheState:

    def test_dashboard(self):
        from dashboard_renderer import _detail
        ok = gex_state.build(_ok("positive_gex", 12.5))
        assert _detail("SYN", {"SYN": _ticker_result(ok)})["gex"] == "+12.5M"
        # 主链值 −7.7 就在旁边：没有状态 / 状态不可得时都不许拿它顶上
        assert _detail("SYN", {"SYN": _ticker_result(None)})["gex"] == "-"
        assert _detail("SYN", {"SYN": _ticker_result(gex_state.build(_ERR))})["gex"] == "-"

    def test_markdown_report(self):
        from report_formatters import _build_market_expectations
        ok = gex_state.build(_ok("negative_gex", -3.0))
        md = "\n".join(_build_market_expectations([("SYN", _ticker_result(ok))]))
        assert "净 GEX：-3.00M$ · 负 gamma（放大波动）" in md
        md2 = "\n".join(_build_market_expectations([("SYN", _ticker_result(None))]))
        assert "净 GEX：不可得" in md2
        for text in (md, md2):
            assert "-7.7" not in text and "Gamma Exposure" not in text, "主链值不许再以 GEX 之名出现"


# ════════════════════════════════════════════════════════════════════════════
# 深度报告 / ML 报告：同一份状态；Gamma 压榨风险按状态分档（负 gamma ⇒ high）
# ════════════════════════════════════════════════════════════════════════════

def _deep_data(state, dealer=None, oracle_gex=-7.7, oracle_squeeze="high"):
    sr = {"agent_details": {"OracleBeeEcho": {"details": {"gamma_exposure": oracle_gex,
                                                          "gamma_squeeze_risk": oracle_squeeze}}}}
    if state is not None:
        sr["gex_state"] = state
    return {"ticker": "SYN", "timestamp": "2026-09-28", "swarm_results": sr,
            "advanced_analysis": {"dealer_gex": dealer} if dealer else {}}


class TestDeepReport:

    def test_reads_state_and_squeeze_direction(self):
        import generate_deep_v2 as g
        ctx = g.extract(_deep_data(gex_state.build(_ok("positive_gex", 12.5))))
        assert ctx["gamma_exposure"] == 12.5 and ctx["gex_regime"] == "positive_gex"
        # 正 gamma ⇒ 压榨风险低。Oracle 的主链分档在这里写的是 "high"（方向相反），不许漏进来
        assert ctx["gamma_squeeze_risk"] == "low"
        assert (ctx["gex_flip"], ctx["gex_call_wall"], ctx["gex_put_wall"]) == (95.0, 110.0, 90.0)

    def test_unavailable_state_rules_over_other_copies(self, monkeypatch):
        """有状态但不可得 ⇒ 不可得：不读 ML 报告里另算的 dealer_gex，也不现场补算。
        变红的变异：deep_v2 的 `if _gst is not None` 改回 `if _gst and _gst.get("available")`；
        删掉 `_try_compute_gex` 的 `gex_state_present` 早退。"""
        import generate_deep_v2 as g
        import advanced_analyzer

        # 记下调用而不是抛：`_try_compute_gex` 用宽 except 吞异常，抛在这里会被吞成「补算跳过」而测试照绿
        calls = []
        monkeypatch.setattr(advanced_analyzer.DealerGEXAnalyzer, "analyze",
                            lambda self, t, p: calls.append((t, p)) or _ok("positive_gex", 9.0))
        ctx = g.extract(_deep_data(gex_state.build(_ERR), dealer=_ok("negative_gex", -2.0)))
        assert ctx["gamma_exposure"] == 0 and ctx["gamma_squeeze_risk"] == "" and ctx["gex_regime"] == ""
        ctx["price"] = 100.0
        g._try_compute_gex(ctx)
        assert calls == [], "有状态（哪怕不可得）时不许现场补算 GEX"
        assert ctx["gamma_exposure"] == 0

    def test_old_record_still_recomputes(self, monkeypatch):
        """正对照：没有状态的旧记录照旧现场补算——否则上一条的「没补算」可能只是补算坏了。"""
        import generate_deep_v2 as g
        import advanced_analyzer
        calls = []
        monkeypatch.setattr(advanced_analyzer.DealerGEXAnalyzer, "analyze",
                            lambda self, t, p: calls.append((t, p)) or _ok("positive_gex", 9.0))
        ctx = g.extract(_deep_data(None))
        ctx["price"] = 100.0
        g._try_compute_gex(ctx)
        assert calls == [("SYN", 100.0)] and ctx["gamma_exposure"] == 9.0

    def test_old_record_never_falls_back_to_main_chain(self):
        """没有状态、也没有 dealer_gex 的旧记录：不许拿 Oracle 主链 −7.7 和它反向的分档顶上。"""
        import generate_deep_v2 as g
        ctx = g.extract(_deep_data(None))
        assert ctx["gamma_exposure"] == 0 and ctx["gamma_squeeze_risk"] == ""
        assert ctx["gex_state_present"] is False

    def test_old_record_still_reads_same_view_dealer(self):
        import generate_deep_v2 as g
        ctx = g.extract(_deep_data(None, dealer=_ok("negative_gex", -2.0)))
        assert ctx["gamma_exposure"] == -2.0 and ctx["gamma_squeeze_risk"] == "high"


class TestSqueezeLabel:

    @pytest.mark.parametrize("st,dealer,want", [
        (gex_state.build(_ok("negative_gex", -2.0)), None, "high"),
        (gex_state.build(_ok("positive_gex", 2.0)), None, "low"),
        (gex_state.build(_ERR), _ok("negative_gex", -2.0), "unknown"),   # 有状态就以它为准
        (None, _ok("negative_gex", -2.0), "high"),                        # 旧记录退到同一视图
        (None, _ERR, "unknown"),
        (None, None, "unknown"),
        (gex_state.build(_ok("negative_gex", float("nan"))), None, "unknown"),
    ])
    def test_display_regime_and_label(self, st, dealer, want):
        tr = {"gex_state": st} if st is not None else {}
        assert gex_state.squeeze_label(gex_state.display_regime(tr, dealer)) == want

    def test_ml_report_chapters_use_the_passed_label(self):
        """变红的变异：_ch3_oracle / _ch6_risk_radar 忽略 `squeeze`、照读 OracleBee / options 的主链分档。"""
        from generate_ml_report import MLEnhancedReportGenerator
        gen = MLEnhancedReportGenerator.__new__(MLEnhancedReportGenerator)
        ad = {"OracleBeeEcho": {"score": 6.0, "direction": "neutral",
                                "details": {"gamma_squeeze_risk": "high", "iv_rank": 40.0}}}
        opts = {"gamma_squeeze_risk": "high", "iv_rank": 40.0}
        html_low = gen._ch3_oracle(ad, opts, squeeze="low")
        assert "<td>low</td>" in html_low and "<td>high</td>" not in html_low
        html_unknown = gen._ch3_oracle(ad, opts, squeeze="unknown")
        assert "<td>Gamma 压榨风险</td><td>—</td><td>—</td>" in html_unknown
        r_high = gen._ch6_risk_radar({}, {}, {"iv_rank": 40.0}, squeeze="high")
        r_unknown = gen._ch6_risk_radar({}, {}, {"iv_rank": 40.0}, squeeze="unknown")
        assert "Gamma 压榨风险：high" in r_high and "Gamma 压榨风险：数据不可用" in r_unknown


class TestMcpGetGex:
    """MCP `alphahive_get_gex`：报告里有扫描时的状态就以它为准。
    变红的变异：删掉 `_st is not None` 那一支（又回到只读 ML 报告另算的 dealer_gex）。"""

    def _call(self, monkeypatch, data):
        import asyncio
        import json as _json
        import alpha_hive_mcp as m
        monkeypatch.setattr(m, "_load_json", lambda t, d: data)
        fn = getattr(m.alphahive_get_gex, "fn", m.alphahive_get_gex)
        return _json.loads(asyncio.run(fn(m.TickerDateInput(ticker="NVDA", date_str="2026-09-28"))))

    def test_unavailable_state_is_not_papered_over(self, monkeypatch):
        out = self._call(monkeypatch, {"swarm_results": {"gex_state": gex_state.build(_ERR)},
                                       "advanced_analysis": {"dealer_gex": _ok("positive_gex", 5.0)}})
        assert out["source"] == "scan_gex_state" and out["available"] is False
        assert out["total_gex"] is None and out["regime"] is None

    def test_available_state_wins_over_ml_copy(self, monkeypatch):
        out = self._call(monkeypatch, {"swarm_results": {"gex_state": gex_state.build(_ok("negative_gex", -2.0))},
                                       "advanced_analysis": {"dealer_gex": _ok("positive_gex", 5.0)}})
        assert out["source"] == "scan_gex_state" and out["available"] is True
        assert out["total_gex"] == -2.0 and out["regime"] == "negative_gex"

    def test_old_report_reads_dealer(self, monkeypatch):
        out = self._call(monkeypatch, {"swarm_results": {},
                                       "advanced_analysis": {"dealer_gex": _ok("positive_gex", 5.0)}})
        assert out["source"] == "ml_report_dealer_gex" and out["total_gex"] == 5.0


class TestSignalArchive:
    """存档读的也是这一份。变红的变异：`gex.available` 在没有状态时写 0.0（把「旧记录」混成「不可得」）；
    不可得时 `gex.total_gex` 写 0.0（哨兵值进档）。"""

    def test_three_shapes(self):
        import signal_archive as sa
        ok = sa.extract({"gex_state": gex_state.build(_ok("negative_gex", -2.0))})
        assert (ok["gex.available"], ok["gex.total_gex"], ok["gex.negative"]) == (1.0, -2.0, 1.0)
        bad = sa.extract({"gex_state": gex_state.build(_ERR)})
        assert bad["gex.available"] == 0.0
        assert "gex.total_gex" not in bad and "gex.negative" not in bad
        old = sa.extract({"agent_details": {"OracleBeeEcho": {"details": {"gamma_exposure": -7.7}}}})
        assert not any(k.startswith("gex.") for k in old), "旧记录没有状态：三列都该缺，不是 0"
        assert old["options.gamma_exposure"] == -7.7

    def test_registered_as_leaves(self):
        import signal_archive as sa
        assert {"gex.available", "gex.total_gex", "gex.negative"} <= set(sa.SIGNAL_LEAVES)


class TestSnapshotModeIsLabelledTruncated:
    """快照模式（`--date` 补跑）下 `fetch_cboe_chain` 忽略 `expiry_selector`，GEX 视图拿到的是 ≤4 个到期日的主链。
    v0.45.362 起：计数单列、链上标 `snapshot_main_chain`、DealerGEX 如实写 `chain_view`、状态判不可得；
    **路由照旧**（改成不可得会改补跑日分数，另登边界）。
    变红的变异：`fetch_cboe_chain_for_gex` 不看 `_SNAPSHOT_PROVIDER`（恒标 full）；DealerGEX 写死 `cboe_full_expiries`；
    `gex_state` 不查 `chain_view`。"""

    def _chain(self):
        return {"calls": [{"strike": 100.0, "openInterest": 1000, "gamma": 0.05, "impliedVolatility": 0.3,
                           "expiration": "2026-10-16"}],
                "puts": [{"strike": 95.0, "openInterest": 400, "gamma": 0.04, "impliedVolatility": 0.3,
                          "expiration": "2026-10-16"}],
                "expirations": ["2026-10-16"]}

    def test_fetch_labels_and_counts(self, monkeypatch):
        import cboe_options as C
        C.reset_gex_view_stats()
        monkeypatch.setattr(C, "fetch_cboe_chain", lambda *a, **k: self._chain())
        assert C.fetch_cboe_chain_for_gex("SYN")["gex_view"] == "cboe_full_expiries"
        monkeypatch.setattr(C, "_SNAPSHOT_PROVIDER", lambda t: {"chain": self._chain()})
        assert C.fetch_cboe_chain_for_gex("SYN")["gex_view"] == "snapshot_main_chain"
        st = C.gex_view_stats()
        assert (st["ok"], st["snapshot_main_chain"], st["unavailable"]) == (1, 1, 0)
        C.reset_gex_view_stats()

    def test_dealer_and_state(self, monkeypatch):
        import advanced_analyzer
        import cboe_options as C
        monkeypatch.setattr(C, "fetch_cboe_chain", lambda *a, **k: self._chain())
        monkeypatch.setattr(C, "_SNAPSHOT_PROVIDER", lambda t: {"chain": self._chain()})
        a = advanced_analyzer.DealerGEXAnalyzer()
        monkeypatch.setattr(a, "_fetch_chain", C.fetch_cboe_chain_for_gex)
        dg = a.analyze("SYN", 100.0)
        assert dg["chain_view"] == "snapshot_main_chain" and dg["regime"] in ("positive_gex", "negative_gex")
        st = gex_state.build(dg)
        assert st["available"] is False and st["reason"] == "chain_view:snapshot_main_chain"
        assert st["regime"] == dg["regime"], "路由用的 regime 照旧（不改分数）"
        C.reset_gex_view_stats()

    def test_queen_logs_it(self, monkeypatch, caplog):
        dg = _ok("positive_gex", 3.0)
        dg["chain_view"] = "snapshot_main_chain"
        out, seen, _ = _run(monkeypatch, dealer_gex=dg)
        assert seen == ["positive_gex"]
        assert out["gex_state"]["reason"] == "chain_view:snapshot_main_chain"
        assert any("chain_view:snapshot_main_chain" in r.getMessage() and r.levelname == "ERROR"
                   for r in caplog.records)


class TestSmallReaders:

    def test_collect_data_uses_state_label(self):
        """手动喂 Claude 的材料：正 gamma ⇒ low（Oracle 主链分档写的是 high，方向相反）。"""
        import collect_data
        d = {"ticker": "SYN", "advanced_analysis": {},
             "swarm_results": {"gex_state": gex_state.build(_ok("positive_gex", 3.0)),
                               "agent_details": {"OracleBeeEcho": {"details": {"gamma_squeeze_risk": "high"}}}}}
        assert collect_data.extract_raw(d)["options"]["gamma_squeeze_risk"] == "low"

    def test_chart_title_maps_gex_labels(self, monkeypatch, tmp_path):
        """图表标题：DealerGEX 产出 positive_gex，旧映射只认 positive_gamma ⇒ 印英文原值。"""
        import chart_engine
        titles = []
        import matplotlib.axes
        orig = matplotlib.axes.Axes.set_title

        def spy(self, label, *a, **k):
            titles.append(label)
            return orig(self, label, *a, **k)
        monkeypatch.setattr(matplotlib.axes.Axes, "set_title", spy)
        dg = _ok("positive_gex", 12.5)
        dg["gex_profile"] = [{"strike": 90.0 + i, "call_gex": 1.0, "put_gex": -0.5, "net_gex": 0.5} for i in range(20)]
        chart_engine.render_gex_profile_chart({"advanced_analysis": {"dealer_gex": dg}}, "SYN", str(tmp_path))
        assert any("正Gamma（做市商抑制波动）" in t for t in titles), titles


class TestReviewFollowups:
    """二次审查（v0.45.362）补的守卫。每条旁注能让它变红的变异。"""

    def test_ml_report_wiring(self, monkeypatch):
        """generate_html_report 真把状态分档接到三处（期权段 / 第 3 章 / 第 6 章）。
        变红的变异：删掉 options 覆盖；ch3 或 ch6 调用处不传 `squeeze=`。"""
        import datetime as _dt
        from generate_ml_report import MLEnhancedReportGenerator as G
        calls = {}

        def _stub(name):
            def f(self, *a, **k):
                calls[name] = (a, k)
                return ""
            return f
        for name in dir(G):
            if name.startswith("_ch") or name in ("_generate_options_section_html", "_build_valuation_pills"):
                if callable(getattr(G, name)):
                    monkeypatch.setattr(G, name, _stub(name))   # _build_valuation_pills 会去 yfinance
        gen = G.__new__(G)
        gen.timestamp = _dt.datetime(2026, 9, 28, 14, 0)
        er = {"combined_recommendation": {"combined_probability": 50.0}, "ml_prediction": {},
              "advanced_analysis": {"options_analysis": {"gamma_squeeze_risk": "high", "iv_rank": 40.0}},
              "swarm_results": {"gex_state": gex_state.build(_ok("positive_gex", 3.0)), "direction": "neutral",
                                "agent_details": {"OracleBeeEcho": {"score": 6.0, "direction": "neutral",
                                                                    "details": {"gamma_squeeze_risk": "high"}}}}}
        gen.generate_html_report("SYN", er)
        assert calls["_ch3_oracle"][1].get("squeeze") == "low"
        assert calls["_ch6_risk_radar"][1].get("squeeze") == "low"
        assert calls["_generate_options_section_html"][0][0]["gamma_squeeze_risk"] == "low"
        assert er["advanced_analysis"]["options_analysis"]["gamma_squeeze_risk"] == "high", "覆盖要在副本上做"

    def test_deep_report_extras_follow_the_state(self):
        """状态不可得 ⇒ 归一化 / vanna / 翻转加速度也不拿 ML 报告另算的那份顶上。"""
        import generate_deep_v2 as g
        dealer = {**_ok("negative_gex", -2.0), "flip_acceleration": {"level": "高"},
                  "vanna_stress": {"can_flip_gex": True}}
        ctx = g.extract(_deep_data(gex_state.build(_ERR), dealer=dealer))
        assert ctx["gex_available"] is False and ctx["gex_state_reason"] == _ERR["error"]
        assert ctx["flip_acceleration"] == {} and ctx["vanna_stress"] == {} and ctx["gex_normalized_pct"] is None
        ctx_ok = g.extract(_deep_data(gex_state.build(_ok("positive_gex", 12.5)), dealer=dealer))
        assert ctx_ok["gex_available"] is True and ctx_ok["gex_normalized_pct"] == 0.8
        assert ctx_ok["vanna_stress"] == {"can_flip_gex": True}

    def test_deep_chart_uses_state_or_is_skipped(self, monkeypatch):
        import chart_engine
        import generate_deep_v2 as g
        seen = []
        monkeypatch.setattr(chart_engine, "render_gex_profile_chart",
                            lambda data, *a, **k: seen.append(data["advanced_analysis"]["dealer_gex"]) or "b64")
        for name in ("render_confidence_chart", "render_options_chart", "render_iv_term_chart",
                     "render_deep_skew_chart"):
            monkeypatch.setattr(chart_engine, name, lambda *a, **k: None)
        dealer = _ok("negative_gex", -2.0)
        for st, want in ((gex_state.build(_ERR), None),
                         (gex_state.build(_ok("positive_gex", 12.5)), ("positive_gex", 12.5))):
            data = _deep_data(st, dealer=dealer)
            ctx = g.extract(data)
            ctx["_raw_data"], ctx["report_date"], ctx["price"] = data, "2026-09-28", 100.0
            seen.clear()
            out = g._try_charts(ctx)
            if want is None:
                assert seen == [] and out[3] == "", "状态不可得时不许画 ML 报告另算的那份"
            else:
                assert (seen[0]["regime"], seen[0]["total_gex"]) == want, "标题的总量 / regime 取自状态"

    def test_deep_card_true_zero_is_not_failure(self):
        import generate_deep_v2 as g
        ctx = g.extract(_deep_data(gex_state.build(_ok("positive_gex", 0.0))))
        assert ctx["gex_available"] is True and ctx["gamma_exposure"] == 0.0

    def test_mcp_routed_regime(self, monkeypatch):
        """快照截断链：路由其实按 positive_gex 偏移了，MCP 不许说成 unknown。"""
        dg = _ok("positive_gex", 3.0)
        dg["chain_view"] = "snapshot_main_chain"
        out = TestMcpGetGex()._call(monkeypatch, {"swarm_results": {"gex_state": gex_state.build(dg)},
                                                   "advanced_analysis": {}})
        assert out["available"] is False and out["routed_regime"] == "positive_gex"
        assert "仍按 positive_gex" in out["interpretation"]
        z = TestMcpGetGex()._call(monkeypatch, {"swarm_results": {"gex_state": gex_state.build(_ok("positive_gex", 0.0))},
                                                "advanced_analysis": {}})
        assert z["available"] is True and z["total_gex"] == 0.0 and "缺失" not in z["interpretation"]

    def test_old_record_fallback_uses_build_rules(self):
        """旧记录退到 dealer_gex 时与 build 同一套规则：快照截断链 / NaN 都不算可得。"""
        snap = {**_ok("positive_gex", 3.0), "chain_view": "snapshot_main_chain"}
        assert gex_state.display_regime({}, snap) == "unknown"
        assert gex_state.display_regime({}, _ok("negative_gex", float("nan"))) == "unknown"
        assert gex_state.display_regime({}, _ok("negative_gex", -1.0)) == "negative_gex"

    def test_report_gex_line_without_oracle_details(self):
        from report_formatters import _build_market_expectations
        md = "\n".join(_build_market_expectations(
            [("SYN", {"agent_details": {}, "gex_state": gex_state.build(_ok("negative_gex", -3.0))})]))
        assert "净 GEX：-3.00M$" in md

    def test_routing_applied_flag(self, monkeypatch):
        out, _, _ = _run(monkeypatch, dealer_gex=_ok())
        assert out["gex_state"]["routing_applied"] is True
        import gex_regime

        def boom(self, *a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(gex_regime.RegimeWeightAdjuster, "adjust_weights", boom)
        q = QueenDistiller(PheromoneBoard(), enable_llm=False, ml_model=None)
        out2 = q.distill("SYN", _agent_results("bullish"), dealer_gex=_ok())
        assert out2["gex_state"]["routing_applied"] is False, "路由抛了，状态不许替它作证"
