"""深度页 / 对比页的三处解说（v0.45.769）：异动流按到期日分层、VIX 期限结构说明、对比页逐项解读。

规矩：只写算得出的结论——倍数、占比、行权价范围、与上一层的差；取不到就是 None / 「没取到」，不给结论；
不把 Call 多写成看涨（记录不分买卖方向）。全部离线、纯函数。
"""
from __future__ import annotations

import re
from pathlib import Path

from alphabot import deep_report as DR
from alphabot import options_compare as OC

REPO = Path(__file__).resolve().parent.parent
BANNED = ("看涨", "看跌", "看多", "看空", "鲸鱼", "押注", "信念", "有利", "博弈", "投机", "多头", "空头")


def _f(typ, strike, prem, expiry, vol=1000, dte=10):
    return {"type": typ, "strike": strike, "volume": vol, "oi": 5000, "ratio": 0.2, "bullish": "call" in typ,
            "otm_pct": 1.0, "dollar_premium": prem, "days_to_exp": dte, "expiry": expiry}


FLOWS = [
    _f("large_call_volume", 200, 6_000_000, "2026-11-20", dte=9), _f("large_put_volume", 190, 4_000_000, "2026-11-20", dte=9),
    _f("call_sweep", 210, 9_000_000, "2026-11-27", dte=16), _f("large_call_volume", 220, 500_000, "2026-11-27", dte=16),
    _f("large_put_volume", 180, 500_000, "2026-11-27", dte=16),
]
RES = [{"strike": 205.0, "oi": 1}, {"strike": 150.0, "oi": 1}]     # 150 在现价下方，不算阻力


class TestFlowByExpiry:
    def test_layers_are_grouped_sorted_and_totalled(self):
        fx = OC.flow_by_expiry(FLOWS, spot=195.0, resistances=RES)
        assert [L["expiry"] for L in fx["layers"]] == ["2026-11-20", "2026-11-27"]
        a, b = fx["layers"]
        assert (a["n_call"], a["n_put"], a["call_premium"], a["put_premium"]) == (1, 1, 6_000_000, 4_000_000)
        assert a["share_pct"] == 50.0 and b["share_pct"] == 50.0 and a["days_to_exp"] == 9
        assert fx["count"] == 5 and fx["n_call"] == 3 and fx["n_put"] == 2 and fx["nearest_resistance"] == 205.0

    def test_lean_labels_follow_the_premium_ratio_only(self):
        a, b = OC.flow_by_expiry(FLOWS, 195.0, RES)["layers"]
        assert (a["lean"], a["times"]) == ("call_mild", 1.5)             # 6 : 4
        assert (b["lean"], b["times"]) == ("call_strong", 19.0)          # 9.5 : 0.5
        only = OC.flow_by_expiry([_f("large_put_volume", 100, 1e6, "2026-12-18")])["layers"][0]
        assert only["lean"] == "put_only" and only["times"] is None
        near = OC.flow_by_expiry([_f("large_call_volume", 100, 1.2e6, "2026-12-18"), _f("large_put_volume", 100, 1e6, "2026-12-18")])["layers"][0]
        assert near["lean"] == "balanced"

    def test_facts_are_computed_quantities(self):
        a, b = OC.flow_by_expiry(FLOWS, 195.0, RES)["layers"]
        assert "占全部异动权利金的 50%" in a["facts"] and "Call 权利金是 Put 的 1.5 倍" in a["facts"]
        assert "Call 异动的行权价在 $200；1 个里 0 个在最近的阻力位 $205 或之上" in a["facts"]
        assert "Call 对 Put 的倍数：上一个到期日 1.5，这里 19.0" in b["facts"]
        assert "单个合约（Call 成交远超未平仓 $210）占本到期日权利金 90%，其余 2 个合计 10%" in b["facts"]
        assert "Call 异动的行权价在 $210–$220；2 个里 2 个在最近的阻力位 $205 或之上" in b["facts"]
        assert "权利金合计是上一个到期日的 1.0 倍" in b["facts"]
        assert any(x.startswith("Call 按权利金加权的行权价 $210.53，比上一个到期日高 $10.53") for x in b["facts"])

    def test_small_differences_and_tiny_shares_are_not_dressed_up(self):
        flows = [_f("large_call_volume", 200.0, 99_000_000, "2026-11-20"), _f("large_call_volume", 200.5, 500_000, "2026-11-27")]
        a, b = OC.flow_by_expiry(flows)["layers"]
        assert "占全部异动权利金不到 1%" in b["facts"]
        assert "权利金合计不到上一个到期日的 1%" in b["facts"]
        assert "Call 按权利金加权的行权价 $200.50" in b["facts"], "差 0.25% 不提「高多少」"
        assert not any("0.0 倍" in x or " 0%" in x for x in a["facts"] + b["facts"])

    def test_no_judgement_words_and_no_direction_claims(self):
        for L in OC.flow_by_expiry(FLOWS, 195.0, RES)["layers"]:
            text = " ".join(L["facts"]) + L["lean_zh"]
            assert not [w for w in BANNED if w in text], text

    def test_unusable_rows_are_counted_not_guessed(self):
        junk = [{"type": "weird", "strike": 1, "expiry": "2026-11-20"}, {"type": "call_sweep", "strike": None, "expiry": "2026-11-20"},
                {"type": "call_sweep", "strike": 100, "expiry": None}, "x",
                {"type": "call_sweep", "strike": 100, "expiry": "2026-11-20", "dollar_premium": None}]
        fx = OC.flow_by_expiry(junk)
        assert fx["unclassified"] == 3 and fx["missing_premium"] == 1 and fx["count"] == 1
        assert fx["layers"][0]["lean"] == "none" and fx["layers"][0]["share_pct"] is None
        empty = OC.flow_by_expiry(None)
        assert empty["layers"] == [] and empty["count"] == 0 and empty["lean"] == "none"

    def test_top_list_is_capped_and_says_how_many_more(self):
        many = [_f("large_call_volume", 100 + i, 1e6 + i, "2026-11-20") for i in range(9)]
        L = OC.flow_by_expiry(many)["layers"][0]
        assert len(L["top"]) == OC.RULES["expiry_flows_shown"] and L["n_more"] == 9 - OC.RULES["expiry_flows_shown"]
        assert L["top"][0]["strike"] == 108 and L["top"][0]["type_zh"] == "Call 大额成交"

    def test_day_view_carries_it(self):
        res = {"final_score": 6.0, "direction": "bearish",
               "agent_details": {"OracleBeeEcho": {"details": {"_snapshot_stock_price": 195.0, "unusual_activity": FLOWS,
                                                               "key_levels": {"resistance": RES}}}}}
        fx = OC.day_view(res)["flow_expiries"]
        assert len(fx["layers"]) == 2 and fx["nearest_resistance"] == 205.0


TODAY = {"score": 5.96, "spot": 168.89, "full_pc_ratio": 1.30, "full_oi": 11_240_000.0, "iv_rank": 100.0, "iv_rank_source": "hv_proxy",
         "iv_current": 79.06, "rv_30d": 42.7, "skew": 1.057, "max_pain": 185.0, "pc_ratio": 0.73, "total_oi": 1.72e6,
         "direction": "bearish", "flow_summary": {}}
PREV = {**TODAY, "score": 6.13, "spot": 178.67, "full_pc_ratio": 1.19, "full_oi": 11_140_000.0, "iv_current": 72.92, "skew": 1.049,
        "max_pain": 195.0}


class TestReadings:
    def _by_key(self, today=TODAY, prev=PREV):
        return {x["key"]: x for x in OC.readings(today, prev, OC.compare(today, prev))}

    def test_states_level_and_change_without_reading_direction(self):
        r = self._by_key()
        assert r["full_pc_ratio"]["now"] == "1.30（Put 未平仓多于 Call）"
        assert r["full_pc_ratio"]["change"] == "比上一次高 0.11：Put 相对 Call 变多了"
        assert r["spot"]["change"] == "比上一次跌 5.47%" and r["score"]["change"] == "比上一次低 0.17"
        assert r["iv_rank"]["now"] == "100（已实现波动率的排名）：等于过去一年的最高值" and r["iv_rank"]["change"] == "与上一次相同"
        assert r["iv_rv"]["now"] == "79.1 / 42.7：IV 是 RV 的 1.85 倍"
        assert r["max_pain"]["now"] == "185.00，在现价上方 9.5%" and r["max_pain"]["change"] == "比上一次低 10.00"
        for x in r.values():
            text = f"{x['now']} {x['change']} {x['read']}"
            assert "看涨" not in text and "看跌" not in text.replace("不解读成看涨 / 看跌", "") and "有利" not in text

    def test_missing_values_stay_missing(self):
        r = self._by_key({"score": None, "flow_summary": {}}, None)
        assert all(x["now"] is None and x["change"] is None for x in r.values())
        assert all(x["read"] for x in r.values()), "「怎么读」是固定说明，没数据也在"

    def test_iv_rank_window_is_not_hard_coded_to_a_year(self):
        # 现在是已实现波动率的排名（窗口一年）；真 IV 历史攒够后自动切换，那时窗口是已攒下的天数，不能再写「过去一年」
        real = {**TODAY, "iv_rank_source": "real_iv_63d"}
        now = self._by_key(real, {**PREV, "iv_rank_source": "real_iv_63d"})["iv_rank"]["now"]
        assert now == "100：等于所用历史窗口的最高值" and "一年" not in now
        low = self._by_key({**TODAY, "iv_rank": 0.0})["iv_rank"]["now"]
        assert low == "0（已实现波动率的排名）：等于过去一年的最低值"

    def test_incomparable_iv_rank_says_so(self):
        r = self._by_key(TODAY, {**PREV, "iv_rank_source": "real_iv_63d"})
        assert r["iv_rank"]["change"] == "两次的口径不同，不比"

    def test_every_reading_has_a_read_and_the_table_has_no_orphans(self):
        keys = [x["key"] for x in OC.readings(TODAY, PREV, OC.compare(TODAY, PREV))]
        assert set(keys) == set(OC.METRIC_READS) and len(keys) == len(set(keys))

    def test_build_ticker_carries_readings(self):
        days = [("2026-11-09", {"NVDA": PREV | {"supports": [], "resistances": [], "flows": []}}),
                ("2026-11-10", {"NVDA": TODAY | {"supports": [], "resistances": [], "flows": []}})]
        for _, d in days:
            d["NVDA"].setdefault("iv_rank_source", "hv_proxy")
            d["NVDA"]["flow_summary"] = {"call_pct": None, "call_centroid": None}
        out = OC.build_ticker("NVDA", days, "2026-11-10")
        assert out["available"] and len(out["readings"]) == len(OC.METRIC_READS)


def _result(vix_term, **extra):
    return {"final_score": 6.0, "direction": "neutral",
            "agent_details": {"GuardBeeSentinel": {"details": {"vix_term_structure": {"vix": 19.46, "vix_term": vix_term, **extra}}}}}


class TestVixTerm:
    def test_known_shapes_get_a_name_and_a_definition(self):
        for term, zh in (("contango", "正价差"), ("backwardation", "倒挂"), ("flat", "持平")):
            m = DR.build("NVDA", "2026-11-10", _result(term))["macro"]
            assert m["vix_term_zh"] == zh and m["vix_term_read"] and "VIX 期货" in m["vix_term_read"]

    def test_unknown_or_missing_gets_no_conclusion(self):
        for term in ("unknown", None, "something_new"):
            m = DR.build("NVDA", "2026-11-10", _result(term))["macro"]
            assert m["vix_term_zh"] is None and m["vix_term_read"] is None, term

    def test_definitions_carry_no_trading_advice(self):
        # 旧报告在这里写过「月期权卖方策略有利」，还在 M1 / M2 都是 N/A 时给出「正价差」
        for zh, read in DR.VIX_TERM_READS.values():
            assert not [w for w in ("有利", "策略", "卖方", "买入", "卖出", "适合") if w in read], read

    def test_page_never_prints_levels_it_does_not_have(self):
        js = (REPO / "alphabot" / "static" / "pages" / "deep.js").read_text(encoding="utf-8")
        body = js.split("function vixTerm(m) {")[1].split("\nfunction ")[0]
        assert "M1" not in body and "M2" not in body and "没取到" in body and '"unknown"' in body


class TestPages:
    def test_pages_render_the_new_blocks_and_explain_the_colors(self):
        deep = (REPO / "alphabot" / "static" / "pages" / "deep.js").read_text(encoding="utf-8")
        cmp_ = (REPO / "alphabot" / "static" / "pages" / "compare.js").read_text(encoding="utf-8")
        assert "flowLayers(r.flow_expiries)" in deep and "vixTerm(m)" in deep and "d.readings" in cmp_
        assert "不表示看涨看跌" in deep and "买 Call 与卖 Call 在这里长得一样" in deep
        assert "style=" not in deep and "style=" not in cmp_, "本站 CSP 不许行内样式"

    def test_chart_tooltips_never_use_echarts_inline_styled_layout(self):
        """本站 CSP 不许行内样式：ECharts 自带的提示排版（缺省 formatter / `valueFormatter` / `p.marker`）整段是行内样式，
        被拦掉后色点没了、名字和数值挤在一起（用户 2026-11-11 的 P/C 截图里「全链0.99」）。提示一律自己排、色点经 CSSOM 涂色。"""
        js = (REPO / "alphabot" / "static" / "charts.js").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in js.splitlines() if not ln.lstrip().startswith("//"))
        assert "valueFormatter" not in code and ".marker" not in code and "style=" not in code
        tips = re.findall(r"tooltip: \{[^\n]*", code)
        assert len(tips) >= 8 and all("formatter" in t or "backgroundColor" in t or t.rstrip().endswith("{") for t in tips), \
            [t for t in tips if "formatter" not in t]
        # 没起名字的系列，ECharts 的缺省名是 "series" + 空字符 + 序号——过滤规则要认得那个空字符，否则提示里多出「series0」
        import subprocess
        probe = ('const re=' + re.search(r"!(/\^series[^/]*/)\.test", code).group(1)
                 + ';console.log(re.test("series\\u00000"),re.test("series3"),re.test("全链"),re.test("series A"))')
        assert subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30).stdout.split() == ["true", "true", "false", "false"]
        assert 'n.style.backgroundColor = n.dataset.c' in code and "MutationObserver" in code and "__tipObs?.disconnect()" in code
        css = (REPO / "alphabot" / "static" / "alphabot.css").read_text(encoding="utf-8")
        assert ".tip-dot {" in css and ".tip-name {" in css

    def test_thresholds_live_in_one_table(self):
        src = (REPO / "alphabot" / "options_compare.py").read_text(encoding="utf-8")
        body = src.split("def flow_by_expiry")[1].split("\ndef ")[0]
        assert not re.search(r">= ?(3\.0|1\.5|40)\b", body), "阈值只经 RULES 取"
