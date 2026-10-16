"""`earnings_calendar`：财报日的唯一解析器 + 唯一读取入口（v0.45.500）。

背景见模块 docstring 与 `experiments/earnings_date_cache_staleness_report.md`。本文件钉五件事：

1. 解析器：`Earnings Date: []` / 键缺失 / NaT / 垃圾 ⇒ 没有日期（不是字符串 "[]"）；日历不可用 ⇒ None。
2. 读取：当天的财报算「当天」；只剩已过日期 ⇒「视野内没有」；日历里没有财报日 / 蜂失败 ⇒ 判不了（None）。
3. **新标签从不比旧标签宽**（`TestNeverLooserThanLegacy`）：把 v0.45.104 的整条旧链（ChronosBee 按 `datetime.now()`
   过滤 → `_earnings_date_from_swarm` → 兜底 watcher 的旧解析器）原样抄成参照，逐情形比卖权 `earnings_status`：
   新标签进检验（none / after_expiry）时必须与旧标签相同。卖权预注册按「修 bug、不截断」记录的依据就是这一条。
4. 唯一入口（`TestSingleEntry`，AST）：`get_earnings_date` / `EarningsWatcher` / `_date.json` 只许出现在指定文件。
5. 观测点：`summarize` → `scan_timing.extra.earnings_dates` → alert_manager P2；`main()` 真的写了它。
"""
from __future__ import annotations

import ast
import json
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import earnings_calendar as EC
import sell_strike_candidates as SC

REPO = Path(__file__).resolve().parent.parent
AS_OF = "2026-10-13"
EXCLUDED = {"before_expiry", "unknown"}       # 卖权预注册：这两种整单位排出检验


def _d(offset: int, base: str = AS_OF) -> date:
    return date.fromisoformat(base) + timedelta(days=offset)


def _entry_from_cal(cal, *, failed=False):
    if failed:
        return {"agent_details": {"ChronosBeeHorizon": {"details": {}, "error": "catalyst_sources_unavailable"}}}
    return {"agent_details": {"ChronosBeeHorizon": {"details": {EC.FIELD: EC.observation(cal)}}}}


# ═════════════════════════════════════════ 1 · 解析器

class TestParseCalendar:

    def test_empty_list_is_no_dates_not_the_string(self):
        """AMD 07-20 的病：`[]` 是假值却不是 None，旧 watcher 一路放行到 `str([])[:10]`。"""
        assert EC.parse_calendar({"Earnings Date": [], "Ex-Dividend Date": date(2026, 11, 1)}) == []

    @pytest.mark.parametrize("cal, want", [
        ({"Earnings Date": [date(2026, 10, 28), date(2026, 10, 21)]}, ["2026-10-21", "2026-10-28"]),
        ({"Earnings Date": [datetime(2026, 10, 21, 16, 30)]}, ["2026-10-21"]),
        ({"Earnings Date": date(2026, 10, 21)}, ["2026-10-21"]),
        ({"Earnings Date": {"a": date(2026, 10, 21), "b": date(2026, 10, 21)}}, ["2026-10-21"]),
        ({"Earnings Date": ["2026-10-21 00:00:00", "soon", "[]", None]}, ["2026-10-21"]),
        ({"Revenue Avg": 1.0}, []),
        ({}, []),
    ])
    def test_shapes(self, cal, want):
        assert EC.parse_calendar(cal) == want

    def test_pandas_timestamp_and_nat(self):
        pd = pytest.importorskip("pandas")
        cal = {"Earnings Date": [pd.Timestamp("2026-10-21"), pd.NaT]}
        assert EC.parse_calendar(cal) == ["2026-10-21"]

    @pytest.mark.parametrize("cal", [None, 42, "2026-10-21"])
    def test_unusable_calendar_is_none(self, cal):
        assert EC.parse_calendar(cal) is None

    def test_to_dict_object_and_broken_to_dict(self):
        good = types.SimpleNamespace(to_dict=lambda: {"Earnings Date": [date(2026, 10, 21)]})
        assert EC.parse_calendar(good) == ["2026-10-21"]

        def boom():
            raise RuntimeError("bad frame")
        assert EC.parse_calendar(types.SimpleNamespace(to_dict=boom)) is None

    def test_observation_records_availability(self):
        assert EC.observation(None)["available"] is False
        ob = EC.observation({"Earnings Date": []}, now=datetime(2026, 10, 13, 14, 0))
        assert ob == {"schema": EC.SCHEMA, "available": True, "dates": [], "fetched_at": "2026-10-13T14:00:00"}


@pytest.mark.usefixtures("stub_cboe_payload", "stub_http_gate", "stub_yfinance")
class TestChronosWritesTheObservation:
    """真跑 ChronosBee（离线桩同 test_catalyst_availability）：观测字段写了**未过滤**的日期，评分口径不动。

    变异：删掉 details 里的 FIELD ⇒ 第一条红（此后读者全部退回旧格式、当天财报又被丢）；
    把评分用的过滤也改成含当天 ⇒ 第二条红（那会改 catalyst 分数，截断维度 IC 协议 H2）。
    """

    def _run(self, monkeypatch, board, cal):
        import pandas as pd
        import yfinance as yf

        class _T:
            def __init__(self, *a, **k):
                pass

            @property
            def calendar(self):
                return cal

            def history(self, *a, **k):
                return pd.DataFrame()

            @property
            def info(self):
                return {}

        monkeypatch.setattr(yf, "Ticker", _T)
        from swarm_agents.chronos_bee import ChronosBeeHorizon
        return ChronosBeeHorizon(board).analyze("ABBV")      # 真实 WATCHLIST 票：假 ticker 会被提前挡下

    def test_observation_keeps_today_and_scoring_still_drops_it(self, monkeypatch, board):
        today, later = date.today(), date.today() + timedelta(days=90)
        r = self._run(monkeypatch, board, {"Earnings Date": [today, later]})
        obs = (r.get("details") or {}).get(EC.FIELD)
        assert obs and obs["available"] is True and obs["dates"] == [today.isoformat(), later.isoformat()], r
        scored = [c["date"] for c in r["details"]["catalysts"] if c.get("type") == "earnings"]
        assert scored == [later.isoformat()], "评分用的催化剂口径变了（当天被纳入）——会改 catalyst 分数"

    def test_empty_earnings_list_is_recorded_as_no_dates(self, monkeypatch, board):
        r = self._run(monkeypatch, board, {"Earnings Date": []})
        assert r["details"][EC.FIELD]["available"] is True and r["details"][EC.FIELD]["dates"] == []


# ═════════════════════════════════════════ 2 · 读取

class TestResolve:

    def _fn(self, cal, **kw):
        return EC.upcoming_fn_from_swarm({"T": _entry_from_cal(cal, **kw)}, AS_OF)("T")

    def test_today_counts(self):
        assert self._fn({"Earnings Date": [_d(0), _d(90)]})["earnings_date"] == AS_OF

    def test_first_on_or_after_as_of(self):
        assert self._fn({"Earnings Date": [_d(-3), _d(5), _d(12)]})["earnings_date"] == _d(5).isoformat()

    def test_only_past_dates_means_none_in_view(self):
        got = self._fn({"Earnings Date": [_d(-1)]})
        assert got["earnings_date"] is None and got["reason"] == "only_past_dates"
        assert SC.classify_earnings(got, AS_OF, _d(30).isoformat()) == "none"

    @pytest.mark.parametrize("cal, kw", [({"Earnings Date": []}, {}), (None, {}), ({}, {"failed": True})])
    def test_cannot_judge(self, cal, kw):
        got = self._fn(cal, **kw)
        assert got is None and SC.classify_earnings(got, AS_OF, _d(30).isoformat()) == "unknown"

    def test_missing_ticker_is_none(self):
        assert EC.upcoming_fn_from_swarm({}, AS_OF)("X") is None

    def test_legacy_catalysts_are_read_but_no_earnings_cannot_be_judged(self):
        """v0.45.500 前落盘的结果没有观测字段：读旧催化剂；旧格式已按 now() 过滤过，「没有财报条目」分不清 ⇒ None。"""
        def legacy(cats):
            return {"T": {"agent_details": {"ChronosBeeHorizon": {"details": {"catalysts": cats}}}}}
        got = EC.upcoming_fn_from_swarm(legacy([{"type": "earnings", "date": "2026-10-21"}]), AS_OF)("T")
        assert got["earnings_date"] == "2026-10-21" and got["source"] == EC.SOURCE_LEGACY
        assert EC.upcoming_fn_from_swarm(legacy([{"type": "dividend", "date": "2026-10-21"}]), AS_OF)("T") is None


class TestDiskReader:

    def _write(self, home, day, swarm):
        (home / f".swarm_results_{day}.json").write_text(json.dumps(swarm), encoding="utf-8")

    def test_same_day_only_by_default(self, tmp_path):
        self._write(tmp_path, _d(-1).isoformat(), {"T": _entry_from_cal({"Earnings Date": [_d(5)]})})
        assert EC.upcoming_fn_from_disk(AS_OF, home=tmp_path)("T") is None, "缺省只认当天的观测"
        got = EC.upcoming_fn_from_disk(AS_OF, max_age_days=2, home=tmp_path)("T")
        assert got["earnings_date"] == _d(5).isoformat() and got["observed_on"] == _d(-1).isoformat()

    def test_latest_observation_skips_days_where_the_bee_failed(self, tmp_path):
        self._write(tmp_path, _d(-2).isoformat(), {"T": _entry_from_cal({"Earnings Date": [_d(5)]})})
        self._write(tmp_path, _d(-1).isoformat(), {"T": _entry_from_cal(None, failed=True)})
        day, obs = EC.latest_observation("T", AS_OF, max_age_days=3, home=tmp_path)
        assert day == _d(-2).isoformat() and obs["dates"] == [_d(5).isoformat()]

    def test_corrupt_file_is_logged_and_treated_as_missing(self, tmp_path, caplog):
        (tmp_path / f".swarm_results_{AS_OF}.json").write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert EC.upcoming_fn_from_disk(AS_OF, home=tmp_path)("T") is None
        assert any("无财报日观测" in r.getMessage() for r in caplog.records)

    def test_each_day_is_read_once(self, tmp_path, monkeypatch):
        self._write(tmp_path, AS_OF, {t: _entry_from_cal({"Earnings Date": [_d(5)]}) for t in ("A", "B", "C")})
        opened = []
        real_open = open

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)
        monkeypatch.setattr("builtins.open", spy)
        fn = EC.upcoming_fn_from_disk(AS_OF, home=tmp_path)
        assert [fn(t)["earnings_date"] for t in ("A", "B", "C")] == [_d(5).isoformat()] * 3
        assert len([p for p in opened if ".swarm_results_" in p]) == 1

    def test_default_home_is_the_data_root_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
        self._write(tmp_path, AS_OF, {"T": _entry_from_cal({"Earnings Date": [_d(1)]})})
        assert EC.upcoming_fn_from_disk(AS_OF)("T")["earnings_date"] == _d(1).isoformat()


# ═════════════════════════════════════════ 3 · 新标签从不比旧标签宽

def _legacy_chain(cal, *, failed, dividend, fallback_ok, as_of=AS_OF):
    """v0.45.104 ~ v0.45.499 的整条旧链，逐字照抄语义（不是调用旧代码——旧代码已删）：

    ChronosBee：`days_until = (strptime(d) - now).days >= 0`，扫描在下午 ⇒ 只留 d > as_of；外加分红催化剂。
    `_earnings_date_from_swarm`：蜂失败或 catalysts 为空 ⇒ watcher 兜底；有催化剂 ⇒ 第一个 earnings，没有 ⇒ None。
    watcher 旧解析：`ed = cal.get("Earnings Date")`；非空列表取 [0]；None ⇒ 返回 None；否则 strftime / `str(ed)[:10]`。
    """
    def watcher():
        if not fallback_ok:
            return None
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed and isinstance(ed, list):
            ed = ed[0]
        if ed is None:
            return None
        return {"earnings_date": ed.strftime("%Y-%m-%d") if hasattr(ed, "strftime") else str(ed)[:10],
                "source": "yfinance"}

    if failed:
        return watcher()
    a = date.fromisoformat(as_of)
    raw = (cal or {}).get("Earnings Date", []) if isinstance(cal, dict) else []
    cats = [{"type": "earnings", "date": d.isoformat()} for d in (raw if isinstance(raw, list) else []) if d > a]
    if dividend:
        cats.append({"type": "dividend", "date": (a + timedelta(days=9)).isoformat()})
    if not cats:
        return watcher()
    for c in cats:
        if c["type"] == "earnings":
            return {"earnings_date": c["date"], "source": "chronos_bee_catalyst"}
    return {"earnings_date": None, "source": "chronos_bee_no_earnings"}


_CALS = {
    "missing_key": {"Revenue Avg": 1.0},
    "empty_list": {"Earnings Date": []},
    "past": {"Earnings Date": [_d(-1)]},
    "today": {"Earnings Date": [_d(0)]},
    "today+next": {"Earnings Date": [_d(0), _d(91)]},
    "tomorrow": {"Earnings Date": [_d(1)]},
    "range": {"Earnings Date": [_d(5), _d(12)]},
    "straddling": {"Earnings Date": [_d(-2), _d(10)]},
    "far": {"Earnings Date": [_d(60)]},
}
_EXPIRIES = [_d(0), _d(3), _d(8), _d(30), _d(45)]


def _cases():
    for name, cal in _CALS.items():
        for failed in (False, True):
            for dividend in (False, True):
                yield name, cal, failed, dividend


class TestNeverLooserThanLegacy:

    @pytest.mark.parametrize("name, cal, failed, dividend", list(_cases()))
    def test_against_legacy_with_successful_fallback(self, name, cal, failed, dividend):
        new = EC.upcoming_fn_from_swarm({"T": _entry_from_cal(cal, failed=failed)}, AS_OF)("T")
        old = _legacy_chain(cal, failed=failed, dividend=dividend, fallback_ok=True)
        for exp in _EXPIRIES:
            s_new = SC.classify_earnings(new, AS_OF, exp.isoformat())
            s_old = SC.classify_earnings(old, AS_OF, exp.isoformat())
            assert s_new in EXCLUDED or s_new == s_old, (
                f"{name} failed={failed} dividend={dividend} expiry={exp}: 新 {s_new} 比旧 {s_old} 宽")

    def test_known_tightenings_are_real(self):
        """自证这把尺有牙：两处有意的收紧确实出现了（不然上面那条可能对「新旧恒等」恒绿）。"""
        exp = _d(30).isoformat()

        def pair(name, dividend, failed=False):
            new = EC.upcoming_fn_from_swarm({"T": _entry_from_cal(_CALS[name], failed=failed)}, AS_OF)("T")
            old = _legacy_chain(_CALS[name], failed=failed, dividend=dividend, fallback_ok=True)
            return SC.classify_earnings(old, AS_OF, exp), SC.classify_earnings(new, AS_OF, exp)
        assert pair("today+next", dividend=True) == ("after_expiry", "before_expiry")   # 当天财报不再被丢
        assert pair("today", dividend=True) == ("none", "before_expiry")
        assert pair("empty_list", dividend=True) == ("none", "unknown")                 # 没排期 ≠ 视野内没有
        assert pair("tomorrow", dividend=False, failed=True) == ("before_expiry", "unknown")  # 不再打网兜底

    def test_the_only_loosening_when_the_old_fallback_failed(self):
        """如实记录：旧兜底**打网失败**时旧标签是 unknown，新链不打网、照观测判。逐情形列出新标签更宽的那些，
        必须恰好是「日历里只剩已过日期、且没有别的催化剂」——新判 none（与旧兜底成功时同），旧因网络失败判 unknown。"""
        looser = set()
        for name, cal, failed, dividend in _cases():
            if failed:
                continue            # 蜂失败时新链没有观测 ⇒ 恒 unknown
            new = EC.upcoming_fn_from_swarm({"T": _entry_from_cal(cal)}, AS_OF)("T")
            old = _legacy_chain(cal, failed=False, dividend=dividend, fallback_ok=False)
            for exp in _EXPIRIES:
                s_new, s_old = (SC.classify_earnings(x, AS_OF, exp.isoformat()) for x in (new, old))
                if s_new not in EXCLUDED and s_new != s_old:
                    looser.add((name, dividend))
        assert looser == {("past", False)}, looser


# ═════════════════════════════════════════ 4 · 唯一入口（AST）

def _prod_files():
    skip = {"tests", ".claude", "experiments", "_archive", "node_modules", ".git"}
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO)
        if not skip & set(rel.parts[:-1]) and not rel.parts[0].startswith("."):
            yield rel.as_posix(), p


def _refs(pred):
    hits = {}
    for rel, p in _prod_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        n = sum(1 for node in ast.walk(tree) if pred(node))
        if n:
            hits[rel] = n
    return hits


def _named(name):
    def pred(node):
        return ((isinstance(node, ast.Name) and node.id == name)
                or (isinstance(node, ast.Attribute) and node.attr == name)
                or (isinstance(node, ast.alias) and node.name == name))
    return pred


def _date_json_literal(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith("_date.json")


class TestSingleEntry:
    """财报日只有一个读取入口。`get_earnings_date`（打网）与它写的 `_date.json` 只服务 `--check-earnings`。

    变异（会红）：任一 `sell_strike_*` / `earnings_vol_signal` / `alphabot/*` 调回 `EarningsWatcher().get_earnings_date`，
    或直接拼 `f"{T}_date.json"` 去读缓存。
    """

    def test_get_earnings_date_only_in_watcher(self):
        assert set(_refs(_named("get_earnings_date"))) == {"earnings_watcher.py"}

    def test_earnings_watcher_constructed_only_for_check_earnings(self):
        assert set(_refs(_named("EarningsWatcher"))) == {"earnings_watcher.py", "alpha_hive_daily_report.py"}

    def test_date_cache_filename_only_in_watcher(self):
        assert set(_refs(_date_json_literal)) == {"earnings_watcher.py"}

    def test_probe_has_teeth(self):
        """自证：探针扫到了足够多的生产文件，且在一段有违规的源码上会命中。"""
        files = dict(_prod_files())
        assert {"earnings_watcher.py", "sell_strike_ledger.py", "earnings_vol_signal.py",
                "alpha_hive_daily_report.py", "earnings_calendar.py"} <= set(files)
        bad = ast.parse('w = EarningsWatcher()\nw.get_earnings_date("X")\np = f"{t}_date.json"\n')
        assert sum(_named("get_earnings_date")(n) for n in ast.walk(bad)) == 1
        assert sum(_date_json_literal(n) for n in ast.walk(bad)) == 1


# ═════════════════════════════════════════ 5 · 观测点

def _swarm_for_summary():
    return {"A": _entry_from_cal({"Earnings Date": [_d(3)]}),
            "B": _entry_from_cal({"Earnings Date": [_d(-5)]}),
            "C": _entry_from_cal(None, failed=True),
            "D": {"agent_details": {"ChronosBeeHorizon": {"details": {"catalysts": [
                {"type": "earnings", "date": _d(9).isoformat()}]}}}}}


class TestSummarize:

    def test_counts(self):
        s = EC.summarize(_swarm_for_summary(), AS_OF)
        assert (s["n"], s["dated"], s["only_past_dates"], s["unresolved"], s["legacy_source"]) == (4, 2, 1, 1, 1)
        assert s["unresolved_tickers"] == ["C"] and s["max_unresolved"] == EC.MAX_UNRESOLVED


import alert_manager as am  # noqa: E402


def _status(ed="__absent__", step2="success"):
    extra = {"gh_pages": {"success": True}}
    if ed != "__absent__":
        extra["earnings_dates"] = ed
    return {"status": "success", "total_duration_seconds": 2000,
            "steps_result": {"step2_hive_analysis": {"status": step2}},
            "scan_timing": {"extra": extra, "production_sync": {"outcome": "up_to_date"}}}


def _hits(tmp_path, status):
    a = am.AlertAnalyzer(report_dir=tmp_path)
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False))
    a.analyze(p)
    return [x for x in a.alerts if "财报日" in x.message]


def _ed(unresolved=0, legacy=0):
    return {"available": True, "n": 30, "dated": 30 - unresolved, "only_past_dates": 0, "unresolved": unresolved,
            "unresolved_tickers": [f"T{i}" for i in range(unresolved)], "legacy_source": legacy,
            "max_unresolved": EC.MAX_UNRESOLVED}


class TestAlert:

    def test_normal_day_is_quiet(self, tmp_path):
        assert _hits(tmp_path, _status(_ed())) == []

    def test_unresolved_is_p2(self, tmp_path):
        hits = _hits(tmp_path, _status(_ed(unresolved=30)))
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "30/30" in hits[0].message

    def test_legacy_source_is_p2(self, tmp_path):
        hits = _hits(tmp_path, _status(_ed(legacy=2)))
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "旧格式" in hits[0].message

    def test_missing_observation_is_p2_not_silence(self, tmp_path):
        hits = _hits(tmp_path, _status())
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "未统计" in hits[0].message

    def test_scan_that_did_not_run_is_not_judged(self, tmp_path):
        assert _hits(tmp_path, _status(_ed(unresolved=30), step2="failed")) == []


class TestDailyScanWritesIt:
    """真跑 `main()` 的蜂群路径（报告生成器打桩，同 test_outcomes_backlog_starvation）：earnings_dates 写进 scan_timing。"""

    def test_main_writes_earnings_dates(self, monkeypatch, tmp_path):
        import alpha_hive_daily_report as adr
        import cloud_snapshot_loader as csl
        import yf_gate

        day = "2026-09-14"
        (tmp_path / f".swarm_results_{day}.json").write_text(json.dumps({
            "NVDA": _entry_from_cal({"Earnings Date": [date(2026, 11, 18)]}),
            "WMT": _entry_from_cal(None, failed=True)}), encoding="utf-8")

        class SwarmReporter:
            date_str = day
            report_dir = tmp_path

            def __init__(self, date_override=None):
                pass

            def run_swarm_scan(self, focus_tickers=None):
                return {"system_status": "✅ 蜂群协作完成", "swarm_metadata": {"tickers_analyzed": 2},
                        "opportunities": [{"ticker": "NVDA"}]}

            def save_report(self, report):
                return "/dev/null"

            def deploy_and_notify(self, report):
                return {"gh_pages": {"success": True}, "deploy_env": "production"}

        written = []
        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", "unset-by-teardown")
        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SwarmReporter)
        monkeypatch.setattr(yf_gate, "install", lambda: False)
        monkeypatch.setattr(adr._timing, "write", lambda d, extra=None, **k: written.append(extra))
        monkeypatch.setattr(csl, "lag_summary", lambda today, **k: {"available": True})
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--swarm", "--no-llm", "--force"])
        adr.main()
        ed = written[0]["earnings_dates"]
        assert ed["available"] is True and (ed["dated"], ed["unresolved"]) == (1, 1), ed
        assert ed["unresolved_tickers"] == ["WMT"]
