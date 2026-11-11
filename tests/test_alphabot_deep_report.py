"""Alpha Bot「深度」页（v0.45.760，`alphabot/deep_report.py` + `/api/deep/{T}` + `static/pages/deep.js`）的守卫。离线。

每组回答一句「谁会红」：
  · 各板块照扫描记录排：取不到 ⇒ None / 空表，不补默认值；人工证伪条件是「未核对」不是「未触发」（`TestBuild`）；
  · 只搬数字与事实：模板推断句、数字后的情绪词、符号、历史准确率、模型预期收益都不上页面，含数字的括号留着（`TestCleaning`）；
  · 参考价位不出概率：隐含区间只由跨式 ÷ 现价算；没有可用报价就没有区间（`TestLevels`）；
  · 读盘：文件在自己的 tmp 里；坏文件 / 没这天 / 没这只票 / 演示模式各有原因；原始结果只缓存两天（`TestService`）；
  · 前端接线：顶栏 / 路由 / 帮助章节一致；页面读的字段都在服务端输出里；输出里没有推荐 / 概率字段（`TestFrontend`）；
  · 本模块不读文件、不联网，只 import `options_compare`（`TestPurity`）。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from alphabot import deep_report as DR
from alphabot.service import AlphaBotService

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "alphabot" / "static"
D1, D2, D3 = "2026-11-06", "2026-11-09", "2026-11-10"


def _res(**top):
    oracle = {"_snapshot_stock_price": 100.0, "put_call_ratio": 0.8, "iv_rank": 50.0, "iv_rank_source": "iv_history",
              "iv_current": 40.0, "rv_30d": 30.0, "iv_rv_spread": 10.0, "iv_skew_ratio": 1.05, "total_oi": 1000,
              "max_pain": {"max_pain": 95.0}, "unusual_activity": [], "data_quality": "real",
              "full_chain_oi": {"full_pc_ratio": 1.2, "total_call_oi": 5000, "total_put_oi": 6000},
              "key_levels": {"support": [{"strike": 90.0, "oi": 5.0}], "resistance": [{"strike": 110.0, "oi": 7.0}]},
              "iv_term_structure": {"shape": "contango", "signal": "卖短期权占优",
                                    "term_structure": [{"expiry": "2026-12-04", "dte": 23, "atm_iv": 41.0}]},
              "gamma_calendar": {"charm_note": "Pin 目标价 $80"},
              "call_flow_classification": {"label": "directional"},
              "quote_set": {"data_available": True, "implied_move_pct": 10.0, "selected_expiry": "2026-12-11",
                            "selected_dte": 31, "atm_straddle_mid": 10.0, "iv30": 39.0}}
    r = {
        "final_score": 6.0, "direction": "bearish", "data_real_pct": 97.9, "dimension_coverage_pct": 100.0,
        "macro_regime": "risk_off", "base_score_before_resonance": 5.0, "total_penalty": 0.0, "coverage_warning": "",
        "confidence_calibration": {"confidence_band": [5.4, 6.5]}, "cs_rank": {"final": 0.586, "odds": 0.138},
        "agent_breakdown": {"bullish": 0, "bearish": 6, "neutral": 1},
        "vote_excluded_agents": ["BearBeeContrarian", "GuardBeeSentinel"],
        "dimension_scores": {"signal": 2.8, "odds": 3.7}, "dimension_weights": {"signal": 0.0, "odds": 0.39},
        "dimension_confidence": {"odds": 0.7}, "dimension_status": {"odds": "present"}, "dim_data_quality": {"odds": 100.0},
        "resonance": {"resonance_detected": True, "direction": "bearish", "supporting_agents": 5, "consistency": 0.833,
                      "resonant_dimensions": ["odds", "sentiment"], "confidence_boost": 20},
        "conflict_level": "none", "direction_vote_weights": {"bullish": 0, "bearish": 3.26, "neutral": 0.63},
        "gex_state": {"available": True, "regime": "negative_gex", "gex_flip": 96.0, "largest_call_wall": 120.0,
                      "largest_put_wall": 90.0, "gex_normalized_pct": -10.5, "routing_applied": True},
        "thesis_break_evaluations": [
            {"level": "level_1_warning", "label": "毛利率", "machine": False, "fired": None},
            {"level": "level_1_warning", "label": "综合分跌破 p2", "machine": True, "fired": False, "field": "score",
             "op": "<", "value": 4.0, "actual": 6.0},
            {"level": "level_2_stop_loss", "label": "看空信号达 p98", "machine": True, "fired": True,
             "field": "bear_signals_count", "op": ">=", "value": 7, "actual": 8},
            {"level": "level_2_stop_loss", "label": "IV 进入 p99", "machine": True, "fired": None, "field": "iv", "op": ">",
             "value": 109.0, "actual": None}, "junk"],
        "agent_details": {
            "OracleBeeEcho": {"score": 3.7, "direction": "bearish", "confidence": 0.7, "dimension": "odds",
                              "discovery": "97 个看涨异动 | Skew25d:1.06 | 历史5条:多0/空0,均分5.9|历史T+7准确率39%(41次,均收益-2.07%)", "details": oracle},
            "ScoutBeeNova": {"score": 2.8, "direction": "bearish", "dimension": "signal",
                             "discovery": "拥挤度 78/100（高拥挤度） | 供应链领先信号：TSM 5日涨幅领先 NVDA 超 1.5pp——上游需求强劲，有望传导至 NVDA 订单端。",
                             "details": {"price": 99.0, "crowding_score": 78.4,
                                         "insider": {"dollar_sold": 3e8, "dollar_bought": 0.0, "filings": 5,
                                                     "notable_trades": [{"insider": f"P{i}", "code_desc": "卖出",
                                                                         "shares": 10, "total_value": 1e6 * i}
                                                                        for i in range(8)]},
                                         "sector_relative_strength": {"sector_etf": "XLK", "sector_name": "科技",
                                                                      "relative_strength": -13.19},
                                         "supply_chain": {"peers": [{"symbol": "TSM", "return_5d_pct": -2.06}],
                                                          "supply_chain_note": "上游需求强劲，有望传导至订单端"}}},
            "ChronosBeeHorizon": {"score": 7.4, "direction": "neutral", "dimension": "catalyst", "details": {
                "catalysts": [{"event": "财报发布", "date": "2026-11-17", "days_until": 6, "type": "earnings",
                               "severity": "critical"}, "junk"],
                "analyst_targets": {"target_mean": 150.0, "target_low": 80.0, "target_high": 200.0, "target_median": 140.0},
                "recommended_hold_days": 4, "exit_rationale": "财报前 2d 平仓", "pead": {"summary": "PEAD 数据不足"}}},
            "RivalBeeVanguard": {"discovery": "预期 7d -12.49% | 30d -18.73% | RSI:19 | 目标价上行 +92.9%（分析师看多）", "details": {"expected_3d": -4.68, "expected_7d": -12.49, "expected_30d": -18.73,
                                             "eps_revision": {"num_analyst_opinions": 57, "forward_eps": 15.9},
                                             "technical_indicators": {"summary": "RSI:19"}}},
            "BuzzBeeWhisper": {"details": {"sentiment_pct": 42, "volume_ratio": 2.75, "momentum_5d": -15.6,
                                           "fear_greed": {"value": 33, "classification": "Fear", "is_real_data": True},
                                           "reddit": {"rank": 1, "mentions": 1207},
                                           "sentiment_momentum": {"delta_1d": -7, "delta_7d": -4}}},
            "GuardBeeSentinel": {"discovery": "共振✅ 4 Agent 同向 | ⚠️ Regime:风险规避", "details": {
                                             "macro_regime": "risk_off", "macro_signals": ["VIX Backwardation（恐慌结构）", "板块逆风"],
                                             "cycle_context": {"is_opex_week": False, "is_month_end_window": True,
                                                               "days_to_opex": 10, "cycle_description": "信号解读可按常规权重处理"},
                                             "signal_crowding": {"crowding_note": "当前分析具备较高独立 alpha 价值。"},
                                             "vix_term_structure": {"vix": 19.46, "vix_source": "cboe", "vix_stale": False,
                                                                    "vix_term": "backwardation"},
                                             "market_regime": {"macro_regime": "bull", "macro_detail": "SPX $7521 高于 200MA $7331 (+2.6%)，宏观处于牛市政体",
                                                               "stock_regime": "bear"}}},
            "BearBeeContrarian": {"score": 2.0, "direction": "bearish", "details": {
                "bear_score": 8.04, "bearish_signals": ["内幕大额抛售 $306,933,627（买入仅 $0）", "IV Rank 100（恐慌高位）",
                                                         "5日下跌 -15.6%", "", "ML预测7日回报-12.5%"],
                "insider_bear": 8.0, "options_bear": 7.0, "short_interest": {"short_pct_float": 1.3, "days_to_cover": 2.8}}},
        },
    }
    r.update(top)
    return r


class TestBuild:
    def test_sections_and_values(self):
        d = DR.build("NVDA", D3, _res(), {"rank": 13, "of": 30})
        assert d["available"] and {"summary", "risks", "catalysts", "options", "macro", "levels", "dimensions", "agents"} <= set(d)
        assert "scenarios" not in d and "resonance" not in d               # 并进摘要 / 改成参考价位
        s = d["summary"]
        assert (s["score"], s["direction"], s["spot"], s["confidence_band"], s["rank"]) == \
            (6.0, "bearish", 100.0, [5.4, 6.5], {"rank": 13, "of": 30})
        assert s["votes"] == {"bullish": 0.0, "bearish": 6.0, "neutral": 1.0} and s["coverage_warning"] is None
        assert s["resonance"]["detected"] and s["resonance"]["conflict_level"] == "none" and s["resonance"]["supporting_agents"] == 5.0
        dims = {x["key"]: x for x in d["dimensions"]}
        assert list(dims) == [k for k, _ in DR.DIMENSIONS] and dims["odds"]["weight"] == 0.39
        assert dims["odds"]["in_score"] is True and dims["signal"]["in_score"] is False and dims["catalyst"]["in_score"] is False
        ag = {a["name"]: a for a in d["agents"]}
        assert ag["BearBeeContrarian"]["votes"] is False and ag["OracleBeeEcho"]["votes"] is True
        c = d["catalysts"]
        assert len(c["events"]) == 1 and c["analyst_targets"]["mean"] == 150.0 and c["target_upside_pct"] == 50.0
        assert not {"exit_rationale", "hold_days", "pead_summary"} & set(c)   # 规则推出的操作建议不搬
        assert d["options"]["read"]["full_oi"] == 11000.0 and d["options"]["gex"]["flip"] == 96.0
        m = d["macro"]
        assert (m["vix"], m["fear_greed"], m["reddit_rank"], m["sector"]["relative"]) == (19.46, 33.0, 1.0, -13.19)
        assert [x["label"] for x in m["layers"]] == ["大盘", "个股"]            # 板块那层没记录 ⇒ 不出空行
        assert m["calendar"] == ["月末再平衡窗口"] and m["days_to_opex"] == 10.0   # 只列真命中的窗口

    def test_risks(self):
        r = DR.build("NVDA", D3, _res())["risks"]
        assert r["bearish_signals"] == ["内幕大额抛售 $306,933,627（买入仅 $0）", "IV Rank 100", "5日下跌 -15.6%"]
        assert r["enough_bear_points"] is True and "crowding_note" not in r
        assert len(r["insider"]["trades"]) == DR.RULES["insider_trades_shown"]
        tb = r["thesis_breaks"]
        assert [(x["level"], x["machine"], x["fired"]) for x in tb] == \
            [(1, False, None), (1, True, False), (2, True, True), (2, True, None)]   # 最后一条：有判据但当天缺数
        assert r["thesis_fired"] == 1                                         # 人工条件 fired=None：不数成已触发，也不写成未触发
        few = DR.build("NVDA", D3, _res(agent_details={"BearBeeContrarian": {"details": {"bearish_signals": ["a", "b"]}}}))
        assert few["risks"]["enough_bear_points"] is False

    def test_bare_result_gives_empty_sections_not_defaults(self):
        d = DR.build("X", D3, {"final_score": 5.0, "direction": "neutral"})
        assert d["available"] and d["agents"] == [] and d["catalysts"]["events"] == [] and d["catalysts"]["analyst_targets"] is None
        assert d["risks"]["bearish_signals"] == [] and d["risks"]["insider"] is None and d["risks"]["thesis_breaks"] == []
        assert d["macro"]["vix"] is None and d["macro"]["sector"] is None and d["macro"]["layers"] == []
        assert d["levels"] == {"spot": None, "implied": None, "rows": []} and d["summary"]["rank"] == {"rank": None, "of": None}
        assert d["macro"]["calendar"] == [] and all(x["in_score"] is False for x in d["dimensions"])
        assert d["options"]["gex"]["available"] is False and d["summary"]["confidence_band"] is None
        json.dumps(d, allow_nan=False)

    def test_not_scanned(self):
        for bad in (None, "x", 3):
            d = DR.build("ZZZ", D3, bad)
            assert d["available"] is False and d["reason"] == "ticker_not_scanned" and d["reason_zh"]


class TestCleaning:
    """只删不写：模板推断句 / 情绪词 / 符号 / 历史准确率 / 模型预期收益不上页面，事实原样留着。"""

    @pytest.mark.parametrize("raw,want", [
        ("IV Rank 100（恐慌高位）", "IV Rank 100"),                                  # 不含数字的括号 = 情绪词
        ("内幕大额抛售 $306,933,627（买入仅 $0）", "内幕大额抛售 $306,933,627（买入仅 $0）"),   # 含数字的括号 = 事实
        ("共振✅ 4 Agent 同向", "共振 4 Agent 同向"), ("⚠️ Regime:风险规避", "Regime:风险规避"),
        ("TSM 领先 1.5pp——上游需求强劲，有望传导至订单端。", "TSM 领先 1.5pp"),
        ("  ", None), (None, None), (3, None), ("（高拥挤度）", None),
        # 二次检查：不含数字但是事实的括号曾被误删
        ("高波动率 53%（年化）", "高波动率 53%（年化）"), ("期权数据不可用（真实链获取失败）", "期权数据不可用（真实链获取失败）"),
        ("F&G 33(Fear)", "F&G 33(Fear)"), ("宏观:VIX 27.3(elevated)", "宏观:VIX 27.3"),
        ("Error: unsupported operand type(s) for -: 'int' and 'NoneType'", "Error: unsupported operand type(s) for -: 'int' and 'NoneType'"),
    ])
    def test_clean_text(self, raw, want):
        assert DR.clean_text(raw) == want

    def test_failed_bee_is_flagged(self):
        r = _res()
        r["agent_details"]["ChronosBeeHorizon"]["discovery"] = "Error: catalyst_sources_unavailable: calendar=YFRateLimited"
        ag = {a["name"]: a for a in DR.build("NVDA", D3, r)["agents"]}
        assert ag["ChronosBeeHorizon"]["failed"] is True and ag["ChronosBeeHorizon"]["findings"][0].startswith("Error: catalyst")
        assert ag["OracleBeeEcho"]["failed"] is False
        page = (STATIC / "pages" / "deep.js").read_text(encoding="utf-8")
        assert "a.failed" in page and "运行出错" in page                      # 摘要里点名，不只躺在收起的明细里

    def test_findings_drop_accuracy_and_model_fragments(self):
        ag = {a["name"]: a["findings"] for a in DR.build("NVDA", D3, _res())["agents"]}
        assert ag["OracleBeeEcho"] == ["97 个看涨异动", "Skew25d:1.06"]
        assert ag["ScoutBeeNova"] == ["拥挤度 78/100", "供应链领先信号：TSM 5日涨幅领先 NVDA 超 1.5pp"]
        assert ag["RivalBeeVanguard"] == ["RSI:19", "目标价上行 +92.9%"]
        assert ag["GuardBeeSentinel"] == ["共振 4 Agent 同向", "Regime:风险规避"]
        assert DR.findings(None) == [] and DR.findings("a | a | b") == ["a", "b"]

    def test_no_templated_inference_reaches_the_page(self):
        blob = json.dumps(DR.build("NVDA", D3, _res()), ensure_ascii=False)
        for phrase in ("卖短期权占优", "Pin 目标价", "有望传导", "alpha 价值", "常规权重", "恐慌", "准确率", "ML预测",
                       "牛市政体", "财报前 2d 平仓", "PEAD", "✅", "⚠"):
            assert phrase not in blob, phrase
        assert "-12.49" not in blob and "-18.73" not in blob                 # 模型预期收益
        d = DR.build("NVDA", D3, _res())
        assert d["macro"]["signals"] == ["VIX Backwardation", "板块逆风"]
        assert d["macro"]["layers"][0]["detail"] == "SPX $7521 高于 200MA $7331 (+2.6%)"


class TestRank:
    def test_rank_by_score(self):
        day = {"A": {"final_score": 7.0}, "b": {"final_score": 6.0}, "C": {"final_score": 6.0}, "D": {"final_score": None}, "E": 3}
        assert DR.rank_of(day, "A") == {"rank": 1, "of": 3} and DR.rank_of(day, "B") == {"rank": 2, "of": 3}
        assert DR.rank_of(day, "C") == {"rank": 2, "of": 3}                  # 同分并列
        assert DR.rank_of(day, "D") == {"rank": None, "of": 3} and DR.rank_of(None, "A") == {"rank": None, "of": 0}


class TestLevels:
    def test_implied_range_is_straddle_over_spot(self):
        s = DR.build("NVDA", D3, _res())["levels"]
        assert (s["implied"]["up"], s["implied"]["down"], s["implied"]["pct"]) == (110.0, 90.0, 10.0)
        labels = [x["label"] for x in s["rows"]]
        assert labels == ["分析师目标价均值", "Call 墙", "期权隐含上沿", "Gamma 翻转位", "Max Pain（近 7 天）", "期权隐含下沿", "Put 墙"]
        assert {x["label"]: x["dist_pct"] for x in s["rows"]}["Gamma 翻转位"] == -4.0
        assert not [x for x in labels if "最高" in x or "最低" in x]

    def test_no_usable_quote_means_no_range(self):
        r = _res()
        r["agent_details"]["OracleBeeEcho"]["details"]["quote_set"]["data_available"] = False
        s = DR.build("NVDA", D3, r)["levels"]
        assert s["implied"] is None and not [x for x in s["rows"] if "隐含" in x["label"]]

    def test_no_probability_model_or_recommendation_keys(self):
        keys = set()

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    keys.add(k)
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(DR.build("NVDA", D3, _res()))
        assert not {k for k in keys if re.search(r"prob|recommend(?!ation_mean)|advice|target_price|win_rate|accuracy|expected|rationale", k)}, keys


class TestService:
    def _svc(self, tmp_path, files, **kw):
        for date, body in files.items():
            (tmp_path / f".swarm_results_{date}.json").write_text(body if isinstance(body, str) else json.dumps(body))
        return AlphaBotService(fetch_fn=lambda *a, **k: (None, "offline"), state_dir=tmp_path / "st",
                               compare_root=tmp_path, **kw)

    def test_reads_own_dir(self, tmp_path):
        svc = self._svc(tmp_path, {D2: {"nvda": _res(final_score=5.0)}, D3: {"nvda": _res(), "BRK-B": _res()}})
        d = svc.deep("nvda")
        assert d["available"] and d["date"] == D3 and d["tickers"] == ["BRK-B", "NVDA"] and d["dates"] == [D2, D3]
        assert d["summary"]["rank"] == {"rank": 1, "of": 2}
        assert svc.deep("NVDA", D2)["summary"]["score"] == 5.0
        assert svc.deep("brk.b")["ticker"] == "BRK-B" and svc.deep("brk.b")["available"]
        assert self._svc(tmp_path / "e" if (tmp_path / "e").mkdir() is None else tmp_path, {}).deep("NVDA")["reason"] == "no_scan_files"

    def test_each_failure_has_its_own_reason(self, tmp_path):
        svc = self._svc(tmp_path, {D2: "{bad", D3: {"NVDA": _res()}})
        assert svc.deep("AMD")["reason"] == "ticker_not_scanned" and svc.deep("AMD")["tickers"] == ["NVDA"]
        bad = svc.deep("NVDA", D2)
        assert bad["available"] is False and bad["reason"].startswith("file_unreadable") and "读不了" in bad["reason_zh"]
        assert svc.deep("NVDA", "2026-11-08")["reason"] == "no_such_date"
        assert self._svc(tmp_path, {}, demo=True).deep("NVDA")["reason"] == "demo"
        assert svc.deep("NVDA")["summary"]["rank"] == {"rank": 1, "of": 1}

    def test_raw_cache_keeps_two_days_and_rereads_changed_files(self, tmp_path):
        svc = self._svc(tmp_path, {D1: {"NVDA": _res()}, D2: {"NVDA": _res()}, D3: {"NVDA": _res(final_score=5.0)}})
        for d in (D1, D2, D3):
            svc.deep("NVDA", d)
        assert sorted(svc._deep_raw) == [D2, D3]
        (tmp_path / f".swarm_results_{D3}.json").write_text(json.dumps({"NVDA": _res(final_score=8.5), "AMD": _res()}))
        assert svc.deep("NVDA", D3)["summary"]["score"] == 8.5

    def test_endpoint(self, tmp_path):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        c = TestClient(create_app(self._svc(tmp_path, {D3: {"NVDA": _res()}}), port=8765), base_url="http://127.0.0.1:8765")
        assert c.get("/api/deep/NVDA").json()["risks"]["thesis_fired"] == 1
        assert c.get("/api/deep/NVDA?date=nope").status_code == 400 and c.get("/api/deep/N!V").status_code == 400
        assert c.get("/api/method").json()["deep"] == DR.public_rules()
        assert c.get("/static/pages/deep.js").status_code == 200


class TestFrontend:
    PAGE = STATIC / "pages" / "deep.js"

    def test_nav_route_help_are_wired(self):
        assert 'href="#/deep" data-nav="deep"' in (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert 'from "./pages/deep.js"' in app and 'page === "deep"' in app
        assert '["deep", "单票深度报告"' in (STATIC / "pages" / "help.js").read_text(encoding="utf-8")
        assert 'helpLink("deep")' in self.PAGE.read_text(encoding="utf-8")
        assert "#/deep/" in (STATIC / "pages" / "compare.js").read_text(encoding="utf-8")

    def test_every_field_the_page_reads_exists(self):
        """各板块函数里读的 `s.x` / `m.x` / `r.x` 都在服务端输出里（改名一头忘了另一头 ⇒ 页面静默显示「—」）。"""
        txt = self.PAGE.read_text(encoding="utf-8")
        d = DR.build("NVDA", D3, _res(), {"rank": 1, "of": 2})
        fn = {n: b for n, b in re.findall(r"\nfunction (\w+)\(d\) \{\n(.*?)\n\}\n", txt, re.S)}
        assert set(fn) == {"summary", "risks", "catalysts", "options", "macro", "levels", "swarm"}
        checks = [("summary", "s", d["summary"]), ("summary", "rs", d["summary"]["resonance"]), ("summary", "rk", d["summary"]["rank"]),
                  ("catalysts", "c", d["catalysts"]), ("options", "r", d["options"]["read"]), ("options", "g", d["options"]["gex"]),
                  ("macro", "m", d["macro"]), ("macro", "sc", d["macro"]["sector"]), ("levels", "s", d["levels"]),
                  ("levels", "im", d["levels"]["implied"]), ("risks", "r", d["risks"]), ("risks", "ins", d["risks"]["insider"])]
        for name, var, obj in checks:
            used = set(re.findall(rf"(?<![\w.]){var}\.(\w+)", fn[name])) - {"length", "map", "filter", "join", "slice", "split"}
            assert used and used <= set(obj), (name, var, used - set(obj))
        assert set(re.findall(r"\bd\.(\w+)", txt)) <= set(d) | {"dates", "tickers"}
        assert set(re.findall(r"\(r\) => .*?\br\.(\w+)", fn["swarm"])) <= set(d["dimensions"][0]) | set(d["agents"][0])

    def test_one_disclaimer_and_no_jargon(self):
        """提醒只在页面顶部说一次；自造词与套话不上页面（v0.45.760 去套话）。"""
        txt = self.PAGE.read_text(encoding="utf-8")
        body = "\n".join(ln for ln in txt.splitlines() if not ln.lstrip().startswith("//"))
        assert body.count("没有被证实") == 1 and body.count("不构成投资建议") == 1
        assert "「未核对」不等于「未触发」" in body and "缺数据，判不了" in body
        assert "调到" not in body and "兜底" not in body and "近期" not in body      # 二次检查删掉的三句没有数据支持的说法
        for word in ("照登", "原话", "规则引擎", "概率区间", "模型预期"):
            assert word not in body, word


class TestPurity:
    def test_module_is_pure(self):
        tree = ast.parse((REPO / "alphabot" / "deep_report.py").read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add(n.module or "")
        assert mods == {"__future__", "math", "re", "typing", "alphabot"}, mods
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"open", "exec", "eval", "__import__"}

    @pytest.mark.parametrize("code", ["demo", "no_scan_files", "no_such_date", "file_unreadable", "ticker_not_scanned"])
    def test_reason_codes_have_text(self, code):
        assert DR.reason_zh(code) and DR.reason_zh(code) != code
