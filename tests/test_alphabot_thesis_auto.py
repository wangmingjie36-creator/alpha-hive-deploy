"""v0.45.777 深度页证伪条件的自动核对（`alphabot/thesis_auto.py` + `service.thesis_auto` + `/api/thesis-auto/{T}`）。

全部离线：判定是纯函数，取数用假 yfinance / 假 http；钟钉住。管这几件事：
1. 每种判法的各个结局，尤其「错的未触发」那一侧：舍入区间跨阈值 ⇒ 临界、最近一期太旧 ⇒ 过旧、「A 且 B」只判一半 ⇒ 不写已触发；
2. 规则表登记的条件原文与现行配置**逐字**一致（不一致 = 规则悄悄失效、页面照旧「未核对」，所以这里要红）；原文变了 ⇒ 不判；
3. 原因码双向对账；
4. service：只对最新一天、缓存与失败留存、限流带等待时间、注入了日线没给取数 ⇒ 不出网；
5. 只展示：除了 service 没人 import 它，它也不 import 评分 / 账本模块。
"""
from __future__ import annotations

import ast
import json
import re
from datetime import date, datetime
from pathlib import Path


from alphabot import thesis_auto as TA
from alphabot.service import AlphaBotService

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 16)


def q(end, gp, rev):
    return {"end": end, "gross_profit": gp, "revenue": rev}


# ───────────────────────────── 判定

class TestGrossMargin:
    def test_drop_beyond_threshold_fires(self):
        v = TA.judge_gm_qoq_drop([q("2026-09-30", 40, 100), q("2026-06-30", 43, 100)], 2.0, TODAY)
        assert v["status"] == "fired" and "-3.00 个百分点" in v["facts"]

    def test_exactly_threshold_does_not_fire(self):
        v = TA.judge_gm_qoq_drop([q("2026-09-30", 41, 100), q("2026-06-30", 43, 100)], 2.0, TODAY)
        assert v["status"] == "not_fired"

    def test_stale_latest_quarter(self):
        v = TA.judge_gm_qoq_drop([q("2025-12-31", 40, 100), q("2025-09-30", 43, 100)], 2.0, TODAY)
        assert v["status"] == "unjudgeable" and v["reason"] == "stale"

    def test_nonconsecutive_quarters(self):
        v = TA.judge_gm_qoq_drop([q("2026-09-30", 40, 100), q("2026-03-31", 43, 100)], 2.0, TODAY)
        assert v["reason"] == "nonconsecutive"

    def test_missing_revenue_rows_skipped_then_insufficient(self):
        v = TA.judge_gm_qoq_drop([q("2026-09-30", 40, 100), q("2026-06-30", 43, None)], 2.0, TODAY)
        assert v["reason"] == "insufficient"


class TestEps:
    def e(self, act, est, quarter="2026-09-30"):
        return [{"quarter": quarter, "actual": act, "estimate": est}]

    def test_miss_fires(self):
        assert TA.judge_eps_miss(self.e(0.70, 1.00), 20, TODAY)["status"] == "fired"

    def test_beat_does_not_fire(self):
        v = TA.judge_eps_miss(self.e(2.22, 2.09113), 20, TODAY)
        assert v["status"] == "not_fired" and "+6.2%" in v["facts"]

    def test_rounding_band_is_borderline(self):
        # 阈值线 0.80；实际值只给到两位小数，0.80 的真值可能在 0.795～0.805 ⇒ 判不了
        assert TA.judge_eps_miss(self.e(0.80, 1.00), 20, TODAY)["reason"] == "borderline"

    def test_nonpositive_estimate(self):
        assert TA.judge_eps_miss(self.e(-0.1, -0.2), 20, TODAY)["reason"] == "estimate_nonpositive"

    def test_stale(self):
        assert TA.judge_eps_miss(self.e(0.7, 1.0, "2025-12-31"), 20, TODAY)["reason"] == "stale"


class TestHalfConditions:
    """「A 且 B」：数字那半满足 ⇒ partial（绝不写已触发）；不满足 ⇒ 整条未触发。"""

    def f(self, *vals, start=("2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31")):
        return [{"end": e, "fcf": v} for e, v in zip(start, vals)]

    def test_fcf_all_negative_is_partial_not_fired(self):
        v = TA.judge_fcf_neg_streak(self.f(-1, -2, -3), 3, "「X」", TODAY)
        assert v["status"] == "partial" and v["status"] != "fired" and "人工" in v["facts"]

    def test_fcf_one_positive_is_not_fired(self):
        assert TA.judge_fcf_neg_streak(self.f(-1, 2, -3), 3, "「X」", TODAY)["status"] == "not_fired"

    def test_fcf_too_few_quarters(self):
        assert TA.judge_fcf_neg_streak(self.f(-1, -2), 3, "「X」", TODAY)["reason"] == "insufficient"

    def test_fcf_stale_like_bili(self):
        v = TA.judge_fcf_neg_streak([{"end": "2025-06-30", "fcf": 1.6e9}, {"end": "2025-03-31", "fcf": 9e8}], 3, "「X」", TODAY)
        assert v["reason"] == "stale"

    def test_runway_short_is_partial(self):
        bal = [{"end": "2026-09-30", "cash": 100e6}]
        v = TA.judge_cash_runway(bal, self.f(-60e6, -60e6, -60e6, -60e6), 6, "「融资无望」", TODAY)
        assert v["status"] == "partial" and "5.0 个月" in v["facts"]

    def test_runway_long_is_not_fired(self):
        bal = [{"end": "2026-09-30", "cash": 4.6e9}]
        v = TA.judge_cash_runway(bal, self.f(-24e6, -110e6, -77e6, -114e6), 6, "「融资无望」", TODAY)
        assert v["status"] == "not_fired"

    def test_not_burning_is_not_fired(self):
        bal = [{"end": "2026-09-30", "cash": 1e6}]
        assert TA.judge_cash_runway(bal, self.f(5e6, 1e6), 6, "「X」", TODAY)["status"] == "not_fired"


class TestDividends:
    def test_cut_fires(self):
        divs = [{"date": "2026-08-25", "amount": 1.00}, {"date": "2026-05-26", "amount": 1.34}]
        assert TA.judge_dividend_cut(divs, TODAY)["status"] == "fired"

    def test_unchanged_is_not_cut(self):
        divs = [{"date": "2026-08-25", "amount": 1.34}, {"date": "2026-05-26", "amount": 1.34}]
        assert TA.judge_dividend_cut(divs, TODAY)["status"] == "not_fired"

    def test_tiny_drop_within_rounding_is_borderline(self):
        divs = [{"date": "2026-08-25", "amount": 1.339}, {"date": "2026-05-26", "amount": 1.340}]
        assert TA.judge_dividend_cut(divs, TODAY)["reason"] == "borderline"

    def test_nee_real_case_is_borderline_not_fired(self):
        """2026-11-16 实测：Yahoo 给 0.623 / 0.567，直接算 9.88% < 10%——但三位小数的舍入下真值可能 ≥ 10%。
        照直算会报「已触发」，这是要防的那种错。"""
        divs = [{"date": "2026-08-28", "amount": 0.623}, {"date": "2026-06-05", "amount": 0.623},
                {"date": "2026-02-27", "amount": 0.623}, {"date": "2025-11-21", "amount": 0.567},
                {"date": "2025-08-28", "amount": 0.567}]
        v = TA.judge_dividend_growth(divs, 10.0, TODAY)
        assert v["status"] == "unjudgeable" and v["reason"] == "borderline" and "9.88%" in v["facts"]

    def test_clear_low_growth_fires_and_clear_high_does_not(self):
        lo = [{"date": "2026-08-28", "amount": 0.600}, {"date": "2025-08-28", "amount": 0.567}]
        hi = [{"date": "2026-08-28", "amount": 0.650}, {"date": "2025-08-28", "amount": 0.567}]
        assert TA.judge_dividend_growth(lo, 10.0, TODAY)["status"] == "fired"
        assert TA.judge_dividend_growth(hi, 10.0, TODAY)["status"] == "not_fired"

    def test_no_year_ago_payment(self):
        divs = [{"date": "2026-08-28", "amount": 0.6}, {"date": "2026-05-28", "amount": 0.6}]
        assert TA.judge_dividend_growth(divs, 10.0, TODAY)["reason"] == "insufficient"


class TestMarket:
    def test_yield_above(self):
        v = TA.judge_yield_above({"date": "2026-11-13", "y10": 5.15}, 5.0, TODAY)
        assert v["status"] == "fired" and "财政部" in v["facts"] and "5.15%" in v["facts"]

    def test_yield_below(self):
        assert TA.judge_yield_above({"date": "2026-11-13", "y10": 4.99}, 5.0, TODAY)["status"] == "not_fired"

    def test_yield_rounding_band(self):
        """官方只给两位小数：5.00 的真值在 4.995～5.005，分不出是否「高于 5.0」；5.01 一定高于。"""
        v = TA.judge_yield_above({"date": "2026-11-13", "y10": 5.00}, 5.0, TODAY)
        assert v["status"] == "unjudgeable" and v["reason"] == "borderline"
        assert TA.judge_yield_above({"date": "2026-11-13", "y10": 5.01}, 5.0, TODAY)["status"] == "fired"

    def test_yield_stale_and_out_of_bounds(self):
        assert TA.judge_yield_above({"date": "2026-10-01", "y10": 5.2}, 5.0, TODAY)["reason"] == "stale"
        assert TA.judge_yield_above({"date": "2026-11-13", "y10": 51.5}, 5.0, TODAY)["reason"] == "out_of_bounds"
        assert TA.judge_yield_above(None, 5.0, TODAY)["reason"] == "insufficient"

    def hourly(self, closes):
        """逐小时递增、最后一根落在 TODAY（真实小时线的样子）。"""
        from datetime import timedelta
        end = datetime(2026, 11, 16, 9)
        n = len(closes)
        return [{"t": (end - timedelta(hours=n - 1 - i)).isoformat(), "close": c} for i, c in enumerate(closes)]

    def test_depeg_run_fires(self):
        closes = [1.0] * 40 + [0.96] * 24 + [1.0] * 56
        assert TA.judge_usdc_depeg(self.hourly(closes), 0.97, 24, TODAY)["status"] == "fired"

    def test_short_dip_does_not_fire(self):
        closes = [1.0] * 40 + [0.96] * 23 + [1.0] * 57
        assert TA.judge_usdc_depeg(self.hourly(closes), 0.97, 24, TODAY)["status"] == "not_fired"

    def test_window_is_last_days_from_last_bar(self):
        """多取的那两天不算：最早 30 小时里的脱钩落在窗口外，窗口里 120 根齐全 ⇒ 未触发（不是判不了）。"""
        closes = [0.96] * 30 + [1.0] * 120
        assert TA.judge_usdc_depeg(self.hourly(closes), 0.97, 24, TODAY)["status"] == "not_fired"

    def test_coverage_is_counted_inside_window(self):
        """取回的根数比窗口多，不代表窗口里齐：窗口内缺 20 小时 ⇒ 覆盖 83%，判不了。"""
        rows = self.hourly([1.0] * 150)
        rows = rows[:40] + rows[60:]
        v = TA.judge_usdc_depeg(rows, 0.97, 24, TODAY)
        assert v["status"] == "unjudgeable" and v["reason"] == "insufficient"

    def test_fetch_takes_more_than_window(self):
        src = Path(TA.__file__).read_text(encoding="utf-8")
        assert "period=f\"{int(RULES['usdc_window_days']) + 2}d\"" in src

    def test_low_coverage_cannot_say_not_fired(self):
        """缺的那几小时可能正是脱钩：覆盖不够时不许写「未触发」。"""
        v = TA.judge_usdc_depeg(self.hourly([1.0] * 60), 0.97, 24, TODAY)
        assert v["status"] == "unjudgeable" and v["reason"] == "insufficient"

    CIRCLE = {"usdc": 68.6e9, "chains": 39, "as_of": "2026-11-16"}

    def test_share(self):
        st = {"usdc": 69e9, "total": 287e9, "n_assets": 300}
        assert TA.judge_usdc_share(st, self.CIRCLE, 20, TODAY)["status"] == "not_fired"
        assert TA.judge_usdc_share(st, {**self.CIRCLE, "usdc": 50e9}, 20, TODAY)["status"] == "fired"
        assert TA.judge_usdc_share({"usdc": None, "total": 287e9}, self.CIRCLE, 20, TODAY)["reason"] == "insufficient"
        assert TA.judge_usdc_share(st, None, 20, TODAY)["reason"] == "insufficient"

    def test_share_swaps_llama_usdc_for_circle(self):
        """分母 = DeFiLlama 合计里把它自己的 USDC 换成 Circle 的：(287 − 69) + 68.6 = 286.6。"""
        v = TA.judge_usdc_share({"usdc": 69e9, "total": 287e9, "n_assets": 300}, self.CIRCLE, 20, TODAY)
        assert "286.6" in v["facts"] and f"{68.6 / 286.6 * 100:.1f}%" in v["facts"] and "Circle" in v["facts"]

    def test_share_stale_circle(self):
        v = TA.judge_usdc_share({"usdc": 69e9, "total": 287e9}, {**self.CIRCLE, "as_of": "2026-11-01"}, 20, TODAY)
        assert v["reason"] == "stale"


# ───────────────────────────── 规则表与配置

def _config():
    return json.loads((ROOT / "thesis_breaks_config.json").read_text(encoding="utf-8"))


class TestRegistry:
    def test_every_registered_trigger_matches_current_config_verbatim(self):
        """登记的原文与配置不一致 ⇒ 这条运行时会回 trigger_changed、页面照旧「未核对」——功能悄悄没了。在这里红。
        配置改了阈值是该红的：先确认新阈值，再改 CHECKS。"""
        cfg = _config()
        for (t, cid), spec in TA.CHECKS.items():
            got = [m for m in TA.match(t, cfg[t]) if m["id"] == cid]
            assert got and got[0]["spec"] is spec, (t, cid, got)

    def test_changed_trigger_is_not_judged(self):
        cfg = json.loads(json.dumps(_config()["NVDA"]))
        for c in cfg["level_2_stop_loss"]["conditions"]:
            if c.get("id") == "eps_miss_severe":
                c["trigger"] = "实际 < 预期 10%+"
        m = {x["id"]: x for x in TA.match("NVDA", cfg)}
        assert m["eps_miss_severe"]["spec"] is None and m["eps_miss_severe"]["reason"] == "trigger_changed"
        assert m["margin_compression"]["spec"] is not None

    def test_missing_condition_reported(self):
        m = TA.match("NVDA", {"level_1_warning": {"conditions": []}, "level_2_stop_loss": {"conditions": []}})
        assert {x["reason"] for x in m} == {"not_in_config"}

    def test_machine_conditions_never_matched(self):
        cfg = {"level_1_warning": {"conditions": [{"id": "margin_compression", "_machine": True, "trigger": "季度环比下降 > 200bps"}]}}
        assert [x["reason"] for x in TA.match("NVDA", cfg)] == ["not_in_config", "not_in_config"]

    def test_every_kind_has_needs_and_dispatch(self):
        for spec in TA.CHECKS.values():
            assert spec["kind"] in TA.NEEDS
        src = Path(TA.__file__).read_text(encoding="utf-8")
        for k in TA.NEEDS:
            assert f'k == "{k}"' in src, k


class TestReasons:
    def test_reason_codes_two_way(self):
        src = (Path(TA.__file__).read_text(encoding="utf-8") + (ROOT / "alphabot/service.py").read_text(encoding="utf-8"))
        used = set(re.findall(r'_cant\("([a-z_]+)"', src)) | set(re.findall(r'"reason": "([a-z_]+)"', Path(TA.__file__).read_text(encoding="utf-8")))
        used |= {"trigger_changed", "not_in_config"}           # 经 match() 的 reason 字段传给 _cant(m["reason"])
        assert used - set(TA.REASONS) == set(), used - set(TA.REASONS)
        assert set(TA.REASONS) - used - {"demo", "not_latest_date", "no_fetcher"} == set()

    def test_service_side_codes_registered(self):
        src = (ROOT / "alphabot/service.py").read_text(encoding="utf-8")
        for code in ("demo", "not_latest_date", "no_fetcher"):
            assert code in TA.REASONS and f'"{code}"' in src, code


# ───────────────────────────── 取数（假 yfinance）

class FakeFrame:
    """够 `_frame_rows` 用的最小 DataFrame：index = 科目、columns = 季末。"""

    def __init__(self, data):  # {科目: {季末: 值}}
        self._d = data
        self.index = list(data)
        cols = []
        for row in data.values():
            for c in row:
                if c not in cols:
                    cols.append(c)
        self.columns = cols
        self.empty = not cols
        self.loc = self

    def __getitem__(self, key):
        lb, col = key
        return self._d[lb].get(col)


class FakeYF:
    def __init__(self, raise_exc=None):
        self.raise_exc = raise_exc

    def Ticker(self, t):  # noqa: N802
        outer = self

        class T:
            @property
            def quarterly_income_stmt(self):
                if outer.raise_exc:
                    raise outer.raise_exc
                return FakeFrame({"Gross Profit": {"2026-09-30": 40.0, "2026-06-30": 43.0},
                                  "Total Revenue": {"2026-09-30": 100.0, "2026-06-30": 100.0}})
        return T()


class TestFetch:
    def test_income_rows_newest_first(self):
        rows, meta = TA.fetch_part("income", "NVDA", today=TODAY, yf_module=FakeYF())
        assert meta["reason"] is None and [r["end"] for r in rows] == ["2026-09-30", "2026-06-30"]
        assert TA.judge_gm_qoq_drop(rows, 2.0, TODAY)["status"] == "fired"

    def test_exception_becomes_reason_never_raises(self):
        rows, meta = TA.fetch_part("income", "NVDA", today=TODAY, yf_module=FakeYF(ValueError("x")))
        assert rows is None and meta == {"reason": "fetch_error", "detail": "ValueError"}

    def test_rate_limit_carries_wait(self):
        import yf_gate
        rows, meta = TA.fetch_part("income", "NVDA", today=TODAY, yf_module=FakeYF(yf_gate.YFRateLimited("429")))
        assert meta["reason"] == "rate_limited" and isinstance(meta["retry_after_sec"], (int, float))

    def test_stables_parse(self):
        payload = {"peggedAssets": [{"symbol": "USDT", "circulating": {"peggedUSD": 180e9}},
                                    {"symbol": "USDC", "circulating": {"peggedUSD": 70e9}},
                                    {"symbol": "X", "circulating": {"peggedUSD": None}}]}
        st, meta = TA.fetch_part("@stables", "CRCL", today=TODAY, http_get=lambda url: payload)
        assert meta["reason"] is None and st["usdc"] == 70e9 and st["total"] == 250e9 and st["n_assets"] == 2

    def test_circle_parse(self):
        payload = {"data": [{"symbol": "EURC", "totalAmount": "300000000"},
                            {"symbol": "USDC", "totalAmount": "68622128844.16",
                             "chains": [{"chain": "ETH", "amount": "1", "updateDate": "2026-11-15T03:00:00Z"},
                                        {"chain": "SOL", "amount": "1", "updateDate": "2026-11-16T03:01:01.861Z"}]}]}
        got, meta = TA.fetch_part("@circle", "CRCL", today=TODAY, http_get=lambda url: payload)
        assert meta["reason"] is None and got == {"usdc": 68622128844.16, "chains": 2, "as_of": "2026-11-16"}

    def test_circle_without_usdc_is_empty_not_success(self):
        got, meta = TA.fetch_part("@circle", "CRCL", today=TODAY, http_get=lambda url: {"data": []})
        assert got is None and meta["reason"] == "empty"

    def test_ust10_latest(self):
        asked = []

        def tg(d):
            asked.append(d)
            return {"date": "2026-11-13", "y10": 5.15, "y2": 4.7, "source": "treasury_gov"}
        got, meta = TA.fetch_part("@ust10", "NEE", today=TODAY, treasury_get=tg)
        assert meta["reason"] is None and got == {"date": "2026-11-13", "y10": 5.15} and asked == [None]

    def test_ust10_month_start_asks_previous_month_once(self, monkeypatch):
        """本月还没有官方值（11-01 是周日）⇒ 只补问一次上月最后一个工作日（10-30 周五），不逐日回找。"""
        monkeypatch.setattr(TA, "_et_today", lambda: date(2026, 11, 1))
        asked = []

        def tg(d):
            asked.append(d)
            return {"date": "2026-10-30", "y10": 4.94} if d == "2026-10-30" else None
        got, meta = TA.fetch_part("@ust10", "NEE", today=date(2026, 11, 1), treasury_get=tg)
        assert got == {"date": "2026-10-30", "y10": 4.94} and asked == [None, "2026-10-30"]

    def test_ust10_month_end_evening_uses_et_month(self, monkeypatch):
        """本机（太平洋）11-30 晚 21 点后美东已是 12-01：本月（12 月）还空 ⇒ 该问 11 月最后一个工作日 11-30，不是 10 月的。"""
        monkeypatch.setattr(TA, "_et_today", lambda: date(2026, 12, 1))
        asked = []

        def tg(d):
            asked.append(d)
            return {"date": "2026-11-30", "y10": 5.02} if d == "2026-11-30" else None
        got, meta = TA.fetch_part("@ust10", "NEE", today=date(2026, 11, 30), treasury_get=tg)
        assert got == {"date": "2026-11-30", "y10": 5.02} and asked == [None, "2026-11-30"]

    def test_ust10_unavailable_is_empty(self, monkeypatch):
        monkeypatch.setattr(TA, "_et_today", lambda: TODAY)   # 钉住美东钟：不钉的话真实日期到下个月初会触发补问
        asked = []
        got, meta = TA.fetch_part("@ust10", "NEE", today=TODAY, treasury_get=lambda d: asked.append(d))
        assert got is None and meta["reason"] == "empty" and asked == [None]

    def test_ust10_exception_never_raises(self):
        def tg(d):
            raise OSError("down")
        got, meta = TA.fetch_part("@ust10", "NEE", today=TODAY, treasury_get=tg)
        assert got is None and meta == {"reason": "fetch_error", "detail": "OSError"}


# ───────────────────────────── service

def _svc(tmp_path, fetch, *, dates=("2026-11-12", "2026-11-13"), config=None, demo=False):
    for d in dates:
        (tmp_path / f".swarm_results_{d}.json").write_text("{}", encoding="utf-8")
    cfg_path = tmp_path / "thesis_breaks_config.json"
    cfg_path.write_text(json.dumps(config if config is not None else _config(), ensure_ascii=False), encoding="utf-8")
    clock = [1000.0]
    s = AlphaBotService(bars_fn=lambda t: {}, compare_root=tmp_path, thesis_fetch_fn=fetch, thesis_config_path=cfg_path,
                        now_fn=lambda: datetime(2026, 11, 16, 10, 0), clock=lambda: clock[0], demo=demo, state_dir=tmp_path / "st")
    return s, clock


def _ok_fetch(calls):
    def fn(part, t, today):
        calls.append((part, t))
        if part == "income":
            return [q("2026-09-30", 40, 100), q("2026-06-30", 43, 100)], {"reason": None}
        if part == "eps":
            return [{"quarter": "2026-07-31", "actual": 2.22, "estimate": 2.09}], {"reason": None}
        raise AssertionError(part)
    return fn


class TestService:
    def test_latest_day_judged_with_source(self, tmp_path):
        calls = []
        s, _ = _svc(tmp_path, _ok_fetch(calls))
        r = s.thesis_auto("NVDA")
        assert r["available"] and r["applicable"] and r["date"] == "2026-11-13"
        by = {c["id"]: c for c in r["checks"]}
        assert by["margin_compression"]["verdict"]["status"] == "fired"
        assert by["eps_miss_severe"]["verdict"]["status"] == "not_fired"
        assert by["margin_compression"]["source"] and by["margin_compression"]["fetched_at"]

    def test_cache_then_expiry(self, tmp_path):
        calls = []
        s, clock = _svc(tmp_path, _ok_fetch(calls))
        s.thesis_auto("NVDA")
        s.thesis_auto("NVDA")
        assert len(calls) == 2 and s.stats["thesis_cache_hits"] == 2
        clock[0] += TA.RULES["cache_ttl_sec"] + 1
        s.thesis_auto("NVDA")
        assert len(calls) == 4

    def test_older_date_not_judged_and_no_fetch(self, tmp_path):
        calls = []
        s, _ = _svc(tmp_path, _ok_fetch(calls))
        r = s.thesis_auto("NVDA", "2026-11-12")
        assert r["applicable"] is False and calls == []
        assert {c["verdict"]["reason"] for c in r["checks"]} == {"not_latest_date"}

    def test_failure_cached_shortly_and_counted(self, tmp_path):
        calls = []

        def bad(part, t, today):
            calls.append(part)
            return None, {"reason": "rate_limited", "retry_after_sec": 120}
        s, clock = _svc(tmp_path, bad)
        r = s.thesis_auto("NVDA")
        v = r["checks"][0]["verdict"]
        assert v["reason"] == "rate_limited" and "120 秒" in v["reason_zh"]
        n = len(calls)
        s.thesis_auto("NVDA")
        assert len(calls) == n                      # 冷却内不重取
        clock[0] += 121
        s.thesis_auto("NVDA")
        assert len(calls) > n and s.stats["thesis_failures"] >= 2

    def test_fetch_exception_does_not_break_page(self, tmp_path):
        def boom(part, t, today):
            raise RuntimeError("x")
        s, _ = _svc(tmp_path, boom)
        r = s.thesis_auto("NVDA")
        assert all(c["verdict"]["reason"] == "fetch_error" for c in r["checks"])

    def test_injected_bars_without_fetcher_does_not_go_online(self, tmp_path):
        s, _ = _svc(tmp_path, None)
        r = s.thesis_auto("NVDA")
        assert {c["verdict"]["reason"] for c in r["checks"]} == {"no_fetcher"}

    def test_demo(self, tmp_path):
        s, _ = _svc(tmp_path, _ok_fetch([]), demo=True)
        assert s.thesis_auto("NVDA")["reason"] == "demo"

    def test_unregistered_ticker_has_no_checks(self, tmp_path):
        s, _ = _svc(tmp_path, _ok_fetch([]))
        r = s.thesis_auto("QCOM")
        assert r["available"] and r["checks"] == []

    def test_unreadable_config_is_reported(self, tmp_path):
        s, _ = _svc(tmp_path, _ok_fetch([]))
        s._thesis_config_path.write_text("{not json", encoding="utf-8")
        r = s.thesis_auto("NVDA")
        assert r["available"] is False and r["reason"] == "config_unreadable"

    def test_config_reread_when_file_changes(self, tmp_path):
        calls = []
        s, _ = _svc(tmp_path, _ok_fetch(calls))
        assert len(s.thesis_auto("NVDA")["checks"]) == 2
        cfg = _config()
        for c in cfg["NVDA"]["level_2_stop_loss"]["conditions"]:
            if c.get("id") == "eps_miss_severe":
                c["trigger"] = "实际 < 预期 10%+（改过）"
        s._thesis_config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        by = {c["id"]: c for c in s.thesis_auto("NVDA")["checks"]}
        assert by["eps_miss_severe"]["verdict"]["reason"] == "trigger_changed"

    def test_route_registered(self):
        src = (ROOT / "alphabot/server.py").read_text(encoding="utf-8")
        assert 'Route("/api/thesis-auto/{ticker}", thesis_auto)' in src


# ───────────────────────────── 只展示

class TestDisplayOnly:
    def test_only_service_imports_it(self):
        users = set()
        for p in ROOT.rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith((".claude/", "tests/", "_retired")) or "/." in rel:
                continue
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"thesis_auto\b", txt) and re.search(r"import\s+thesis_auto|thesis_auto\s+as|from alphabot\.thesis_auto", txt):
                users.add(rel)
        assert users == {"alphabot/service.py"}, users

    def test_module_imports_nothing_from_scoring_or_ledgers(self):
        tree = ast.parse(Path(TA.__file__).read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        assert mods <= {"__future__", "math", "datetime", "typing", "alphabot", "yf_daily", "yfinance", "yf_gate",
                        "json", "urllib", "treasury_yields", "zoneinfo"}, mods

    def test_never_writes_config(self):
        src = Path(TA.__file__).read_text(encoding="utf-8") + (ROOT / "alphabot/service.py").read_text(encoding="utf-8")
        assert not re.search(r"thesis_breaks_config[^\n]*(write_text|json\.dump|open\([^)]*['\"]w)", src)


# ───────────────────────────── 二次检查补的（v0.45.777 同版）

class TestNoOlderQuarterImpersonation:
    """最近一季缺数时不许跳过它、拿更早的季度冒充「最近一季」。"""

    def test_gm_latest_missing_is_insufficient(self):
        v = TA.judge_gm_qoq_drop([q("2026-09-30", None, 100), q("2026-06-30", 40, 100), q("2026-03-31", 43, 100)], 2.0, TODAY)
        assert v["reason"] == "insufficient" and "2026-09-30" in v["reason_zh"]

    def test_eps_latest_without_estimate_is_insufficient(self):
        eps = [{"quarter": "2026-09-30", "actual": 0.5, "estimate": None}, {"quarter": "2026-06-30", "actual": 2.0, "estimate": 1.0}]
        assert TA.judge_eps_miss(eps, 20, TODAY)["reason"] == "insufficient"

    def test_eps_unreported_quarter_is_skipped(self):
        eps = [{"quarter": "2026-12-31", "actual": None, "estimate": 1.0}, {"quarter": "2026-09-30", "actual": 0.5, "estimate": 1.0}]
        assert TA.judge_eps_miss(eps, 20, TODAY)["status"] == "fired"

    def test_fcf_gap_in_latest_quarters_is_insufficient(self):
        cf = [{"end": "2026-09-30", "fcf": None}, {"end": "2026-06-30", "fcf": -1}, {"end": "2026-03-31", "fcf": -1},
              {"end": "2025-12-31", "fcf": -1}]
        assert TA.judge_fcf_neg_streak(cf, 3, "「X」", TODAY)["reason"] == "insufficient"

    def test_runway_cash_and_flow_must_be_same_quarter(self):
        bal = [{"end": "2026-09-30", "cash": 100e6}]
        cf = [{"end": "2026-09-30", "fcf": None}, {"end": "2026-06-30", "fcf": -60e6}, {"end": "2026-03-31", "fcf": -60e6}]
        assert TA.judge_cash_runway(bal, cf, 6, "「X」", TODAY)["reason"] in ("insufficient", "nonconsecutive")
        cf2 = [{"end": "2026-06-30", "fcf": -60e6}, {"end": "2026-03-31", "fcf": -60e6}]
        assert TA.judge_cash_runway(bal, cf2, 6, "「X」", TODAY)["reason"] == "nonconsecutive"


class TestUsdcGaps:
    def rows(self, spec):
        """spec = [(相对末根的小时数, 收盘)]，末根在 TODAY 09:00。"""
        from datetime import timedelta
        end = datetime(2026, 11, 16, 9)
        return [{"t": (end - timedelta(hours=h)).isoformat(), "close": c} for h, c in spec]

    def test_gap_does_not_glue_two_dips_into_a_fire(self):
        # 两段各 12 小时低于 0.97，中间缺 1 小时：真有的连续只有 12 ⇒ 不许「已触发」；算上缺口可达 25 ⇒ 临界
        spec = [(h, 1.0) for h in range(119, 30, -1)] + [(h, 0.96) for h in range(30, 18, -1)] \
            + [(h, 0.96) for h in range(17, 5, -1)] + [(h, 1.0) for h in range(5, -1, -1)]
        v = TA.judge_usdc_depeg(self.rows(spec), 0.97, 24, TODAY)
        assert v["status"] == "unjudgeable" and v["reason"] == "borderline"

    def test_full_coverage_short_dip_still_not_fired(self):
        spec = [(h, 0.96 if 40 <= h < 50 else 1.0) for h in range(119, -1, -1)]
        assert TA.judge_usdc_depeg(self.rows(spec), 0.97, 24, TODAY)["status"] == "not_fired"

    def test_real_style_timestamps_parse(self):
        rows = [{"t": f"2026-11-16 0{h}:00:00+00:00", "close": 1.0} for h in range(10)]
        assert TA._ts(rows[0]["t"]) is not None


class TestEmptyIsFailure:
    def test_empty_frame_is_a_failure_not_cached_as_success(self):
        class Empty(FakeYF):
            def Ticker(self, t):  # noqa: N802
                class T:
                    quarterly_income_stmt = FakeFrame({})
                return T()
        rows, meta = TA.fetch_part("income", "NVDA", today=TODAY, yf_module=Empty())
        assert rows is None and meta["reason"] == "empty" and "empty" in TA.REASONS

    def test_service_caches_empty_only_briefly(self, tmp_path):
        calls = []

        def fn(part, t, today):
            calls.append(part)
            return None, {"reason": "empty"}
        s, clock = _svc(tmp_path, fn)
        s.thesis_auto("NVDA")
        n = len(calls)
        clock[0] += TA.RULES["fail_ttl_sec"] + 1
        s.thesis_auto("NVDA")
        assert len(calls) > n


def test_yield_source_is_official_not_tnx():
    """有权威值的量用权威源（v0.45.785）：收益率不再走 Yahoo ^TNX，USDC 流通量要 Circle 的数。"""
    assert TA.NEEDS["yield_above"][0] == ("@ust10",) and "财政部" in TA.NEEDS["yield_above"][1]
    assert "@circle" in TA.NEEDS["usdc_share"][0]
    src = Path(TA.__file__).read_text(encoding="utf-8")
    assert 'Ticker("^TNX"' not in src and "alphabot_thesis_tnx" not in src


def test_no_scan_files_is_not_applicable_and_does_not_fetch(tmp_path):
    calls = []
    s, _ = _svc(tmp_path, _ok_fetch(calls), dates=())
    r = s.thesis_auto("NVDA")
    assert r["applicable"] is False and calls == []


def test_frontend_keeps_unchecked_rows_out_of_the_auto_table():
    """条件原文改过 / 配置里没有的那几条没核对：页面不许把它们放进「自动核对」表、算成「判不了」。"""
    js = (ROOT / "alphabot/static/pages/deep.js").read_text(encoding="utf-8")
    assert 'new Set(["trigger_changed", "not_in_config"])' in js and "!NOT_CHECKED.has(c.verdict?.reason)" in js
