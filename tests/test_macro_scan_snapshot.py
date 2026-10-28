"""一轮扫描的宏观快照只取一次（v0.45.674）

根因（用户 10-28「照建议的」）：「这一轮扫描用哪份宏观」没有主人——Guard / 日报宏观段 / 网站各调 `fred_macro.get_macro_context()`，
靠一个 30 分钟的进程缓存凑成「同一份」。缓存只在查 / 写时加锁、取数不在锁里：
  · 扫描 4 个并行线程在 Guard 阶段开头同时看到空缓存、**各取一次**（日志每天 30 秒内 4 次）；最先跑的几只标的各用自己那份。
    几次结果相同就看不出来；碰上网络抖动就分叉——09-29 Guard 30 行分成 27 + 2 + 1 三份（4 次取数各命中 5 / 5 / 2 / 7 个来源）；
  · 网站在扫描末尾渲染，比取数晚 22–36 分钟：10-22 晚了 35.9 分钟 ⇒ 缓存过期重取，那次 CBOE 断连 ⇒ 网站 VIX 16.7（前一天的值），
    日报与 Guard 是 19.0；
  · 补跑：快照上下文退出时 `set_macro_snapshot(None)` 清缓存，网站在上下文之外渲染 ⇒ 重取到运行当天的宏观（代码路径推出，未实跑）。

修法：扫描开头（`alpha_hive_daily_report._fetch_scan_macro`，快照上下文里）取一次 → `inject_prefetched` 注入各蜂
（`BeeAgent._scan_macro()`），日报宏观段用同一个对象（`self._scan_macro`），网站读 `report["macro_context"]`。
没注入（单票 CLI / 测试 / 深度分析）⇒ 读者照旧自己调，那条路逐字不变。单飞锁 / 「扫描期间不过期」都不需要：扫描里不再有并发调用。

本文件守：
  A. Guard：注入了就不调 `get_macro_context`（注入的是 {} 也不调）；没注入照旧调；4 线程 × 多票全读同一份；
  B. 扫描主流程：`prefetched["macro"]` 在 `inject_prefetched` 之前由 `_fetch_scan_macro()` 填；取不到 ⇒ fallback + error，不留给读者重取；
     日报宏观段用本轮快照；
  C. 网站：读 `report["macro_context"]`，不调 `get_macro_context`；缺这个键 ⇒ 全「—」+ WARNING；
  D. 读者白名单（AST，双向）：生产代码里调 `get_macro_context` 的只有三处（扫描取数 + 两条非扫描回退）；
  E. 观测点：`fred_macro.stats()` → `scan_timing.counters()["macro_context"]` → alert_manager P2（calls ≠ 1）；
  F. 世代边界登记（同 654 ~ 669：并进 10-19 同日标签，逐行等价判据读当天全部 Guard 行）。
"""
import ast
import json
import logging
import pathlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

import fred_macro as fm

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = "2026-10-27"
MACRO = {"vix": 31.5, "vix_source": "cboe", "vix_regime": "high", "vix_stale": False, "data_source": "treasury+finnhub",
         "treasury_10y": 4.8, "yield_curve": "normal", "gold_trend": "stable", "sector_rotation": {}, "summary": "本轮快照"}


@pytest.fixture(autouse=True)
def _offline_vix_term(monkeypatch):
    import vix_term_structure
    monkeypatch.setattr(vix_term_structure, "get_vix_term_structure", lambda force_refresh=False: {"structure": ""})


def _boom():
    raise AssertionError("扫描里不该再调 get_macro_context")


def _guard(injected=..., ticker="NVDA"):
    from swarm_agents.guard_bee import GuardBeeSentinel
    g = GuardBeeSentinel.__new__(GuardBeeSentinel)
    if injected is not ...:
        g._prefetched_macro = injected
    return g


# ══════════════════════════════════════════════════════════════════════════
# A. Guard 读注入的快照
# ══════════════════════════════════════════════════════════════════════════

class TestGuardReadsTheScanSnapshot:

    def test_injected_snapshot_is_used_and_nothing_is_fetched(self):
        """变红的变异：Guard 照旧调 `get_macro_context()`（不读 `_scan_macro()`）。"""
        with patch("fred_macro.get_macro_context", side_effect=_boom) as m:
            r = _guard(MACRO)._calc_macro_adjustment("NVDA")
        assert m.call_count == 0
        assert r["details"]["vix"] == 31.5 and r["details"]["macro_data_source"] == "treasury+finnhub"
        assert r["regime_votes"]["risk_off"] >= 1

    def test_injected_empty_is_still_the_scan_snapshot(self):
        """{} 也算注入了（这一轮取到的就是空）。变红的变异：`if not macro: macro = get_macro_context()`——再取的那份不是日报那份。"""
        with patch("fred_macro.get_macro_context", side_effect=_boom) as m:
            r = _guard({})._calc_macro_adjustment("NVDA")
        assert m.call_count == 0 and r["details"]["macro_data_source"] == "unknown"

    def test_not_injected_falls_back_to_the_old_direct_call(self):
        """单票 CLI / 测试 / 深度分析：逐字等于改动前（自己调一次）。变红的变异：没注入就不取宏观。"""
        with patch("fred_macro.get_macro_context", return_value=MACRO) as m:
            r = _guard()._calc_macro_adjustment("NVDA")
        assert m.call_count == 1 and r["details"]["vix"] == 31.5

    def test_inject_prefetched_carries_the_snapshot_or_none(self):
        """没有 "macro" 键（deep_analysis / 实验脚本的预取）⇒ None ⇒ 照旧自己取。"""
        from swarm_agents.base import inject_prefetched
        g1, g2 = _guard(), _guard()
        inject_prefetched([g1], {"macro": MACRO})
        inject_prefetched([g2], {})
        assert g1._scan_macro() is MACRO and g2._scan_macro() is None

    def test_four_threads_all_read_one_snapshot(self):
        """扫描的真实形状：4 个线程并行跑多只标的。旧代码这里 4 个线程各取一次（09-29 分成三份）。"""
        from swarm_agents.base import inject_prefetched
        from config import WATCHLIST
        tickers = list(WATCHLIST)[:12]
        guards = [_guard() for _ in tickers]
        inject_prefetched(guards, {"macro": dict(MACRO)})
        with patch("fred_macro.get_macro_context", side_effect=_boom) as m:
            with ThreadPoolExecutor(max_workers=4) as ex:
                res = list(ex.map(lambda gt: gt[0]._calc_macro_adjustment(gt[1]), zip(guards, tickers)))
        assert m.call_count == 0
        assert {(r["details"]["vix"], r["details"]["macro_data_source"]) for r in res} == {(31.5, "treasury+finnhub")}


# ══════════════════════════════════════════════════════════════════════════
# B. 扫描主流程：取一次、注入、日报宏观段同一个对象
# ══════════════════════════════════════════════════════════════════════════

def _func(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _daily_tree():
    return ast.parse((ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))


class TestScanFetchesOnce:

    def test_snapshot_is_put_into_prefetched_before_injection(self):
        """变红的变异：删掉那一行 / 挪到 `inject_prefetched` 之后 / 改成别的来源。"""
        tree = _daily_tree()
        host = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                    and any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "prefetch_shared_data"
                            for c in ast.walk(f)))
        put = [a for a in ast.walk(host) if isinstance(a, ast.Assign)
               and any(isinstance(t, ast.Subscript) and getattr(t.value, "id", None) == "prefetched"
                       and isinstance(t.slice, ast.Constant) and t.slice.value == "macro" for t in a.targets)]
        assert len(put) == 1, "run_swarm_scan 里要恰好一处 prefetched[\"macro\"] = …"
        assert isinstance(put[0].value, ast.Call) and getattr(put[0].value.func, "id", None) == "_fetch_scan_macro"
        assert any(isinstance(t, ast.Attribute) and t.attr == "_scan_macro" for t in put[0].targets), "日报宏观段要拿到同一个对象"
        inject = [c for c in ast.walk(host) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "inject_prefetched"]
        prefetch = [c for c in ast.walk(host) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "prefetch_shared_data"]
        assert len(inject) == 1 and prefetch[0].lineno < put[0].lineno < inject[0].lineno

    def test_fetch_scan_macro_calls_once(self):
        import alpha_hive_daily_report as dr
        with patch("fred_macro.get_macro_context", return_value=MACRO) as m:
            assert dr._fetch_scan_macro() is MACRO
        assert m.call_count == 1

    def test_fetch_scan_macro_failure_is_labelled_missing(self, caplog):
        """取不到 ⇒ fallback + error（各观测量不写 = 读者按缺失），WARNING；不返回 None（None 会让 Guard 各自再取）。"""
        import alpha_hive_daily_report as dr

        def boom():
            raise ImportError("no fred_macro")
        with patch("fred_macro.get_macro_context", side_effect=boom), caplog.at_level(logging.WARNING):
            out = dr._fetch_scan_macro()
        assert out == {"data_source": "fallback", "error": "ImportError: no fred_macro"}
        assert any("本轮宏观快照取不到" in m for m in caplog.messages)

    def test_report_context_uses_the_scan_snapshot(self, monkeypatch):
        """日报宏观段 = Guard 读的那份。变红的变异：`_fetch_report_context` 照旧调 `get_macro_context()`。"""
        import alpha_hive_daily_report as dr
        monkeypatch.setattr(dr, "Backtester", None)
        rep = dr.AlphaHiveDailyReporter.__new__(dr.AlphaHiveDailyReporter)
        rep._scan_macro = MACRO
        with patch("fred_macro.get_macro_context", side_effect=_boom) as m:
            _conc, macro, _bt = rep._fetch_report_context({})
        assert macro is MACRO and m.call_count == 0

    def test_report_context_outside_a_scan_still_fetches(self, monkeypatch):
        import alpha_hive_daily_report as dr
        monkeypatch.setattr(dr, "Backtester", None)
        rep = dr.AlphaHiveDailyReporter.__new__(dr.AlphaHiveDailyReporter)
        with patch("fred_macro.get_macro_context", return_value=MACRO) as m:
            _conc, macro, _bt = rep._fetch_report_context({})
        assert macro is MACRO and m.call_count == 1


# ══════════════════════════════════════════════════════════════════════════
# C. 网站读这份日报自己的快照
# ══════════════════════════════════════════════════════════════════════════

def _render(tmp_path, macro=...):
    from dashboard_renderer import render_dashboard_html
    report = {"opportunities": [{"ticker": "NVDA", "direction": "bullish", "score": 7.0, "confidence": 0.6,
                                 "dimension_scores": {}}],
              "swarm_metadata": {"tickers_analyzed": 1, "total_agents": 7, "resonances_detected": 0}}
    if macro is not ...:
        report["macro_context"] = macro
    return render_dashboard_html(report=report, date_str=D, report_dir=tmp_path, opportunities=report["opportunities"])


@pytest.mark.usefixtures("stub_yfinance")     # 网站渲染另有 yfinance 读者（与宏观无关），钉死离线
class TestDashboardReadsTheReport:

    def test_shows_the_report_snapshot_without_fetching(self, tmp_path):
        """10-22：网站 VIX 16.7（缓存过期重取）、日报 19.0。变红的变异：网站照旧调 `get_macro_context()`。"""
        with patch("fred_macro.get_macro_context", side_effect=_boom) as m:
            html = _render(tmp_path, {**MACRO, "vix": 19.0})
        assert m.call_count == 0
        assert 'class="ah-macro-val">19.0<' in html and 'class="ah-macro-val">4.80%<' in html

    def test_backfill_day_keeps_its_own_macro_after_the_snapshot_is_unloaded(self, tmp_path):
        """补跑：快照上下文退出（清缓存）之后才渲染网站——旧代码此时重取到运行当天的宏观。"""
        fm.set_macro_snapshot("2026-10-16", {})
        fm.set_macro_snapshot(None)
        with patch("fred_macro.get_macro_context", return_value={**MACRO, "vix": 99.0}) as m:
            html = _render(tmp_path, {**MACRO, "vix": 20.0})
        assert m.call_count == 0 and 'class="ah-macro-val">20.0<' in html

    def test_report_without_macro_shows_dashes_and_says_so(self, tmp_path, caplog):
        """缺 `macro_context` ⇒ 全「—」+ WARNING，**不**去取运行当时的。变红的变异：缺键时回退 `get_macro_context()`。"""
        with patch("fred_macro.get_macro_context", return_value={**MACRO, "vix": 99.0}) as m, \
                caplog.at_level(logging.WARNING):
            html = _render(tmp_path)
        assert m.call_count == 0 and "99.0" not in html
        assert 'class="ah-macro-val">—<' in html
        assert any("没有宏观快照" in r.getMessage() for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════════
# D. 读者白名单（双向）
# ══════════════════════════════════════════════════════════════════════════

#: 生产代码里允许调 `get_macro_context` 的（文件, 所在函数）。扫描里只有第一处会跑；后两处是「没在扫描里」的回退。
ALLOWED = {
    ("alpha_hive_daily_report.py", "_fetch_scan_macro"),        # 本轮快照：整轮只取这一次
    ("alpha_hive_daily_report.py", "_fetch_report_context"),    # 回退：不经扫描调到日报宏观段（测试 / 重放）
    ("swarm_agents/guard_bee.py", "_calc_macro_adjustment"),    # 回退：没注入（单票 CLI / 测试 / 深度分析）
}


def _sites(sources=None):
    """（文件, 所在函数）——`sources` 给 {相对路径: 源码} 时只扫它们（守卫有牙用），否则扫 git 跟踪的全部生产 .py。"""
    if sources is None:
        files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True,
                               check=True).stdout.split()
        sources = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in files
                   if not rel.startswith(("tests/", "experiments/")) and rel != "fred_macro.py"}
    found = set()
    for rel, src in sources.items():
        tree = ast.parse(src)

        def visit(node, fn):
            for ch in ast.iter_child_nodes(node):
                name = ch.name if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if isinstance(ch, ast.ImportFrom) and ch.module == "fred_macro" \
                        and any(a.name == "get_macro_context" for a in ch.names):
                    found.add((rel, fn))
                elif isinstance(ch, ast.Attribute) and ch.attr == "get_macro_context":
                    found.add((rel, fn))
                visit(ch, name)
        visit(tree, "<module>")
    return found


class TestReaderWhitelist:

    def test_only_the_registered_sites_call_get_macro_context(self):
        """新读者绕过本轮快照自己取 ⇒ 红（09-29 / 10-22 的形状）；白名单过期（某处已不再调）⇒ 也红。
        变红的变异：网站恢复 `from fred_macro import get_macro_context`。"""
        assert _sites() == ALLOWED

    @pytest.mark.parametrize("snippet", [
        "def _x():\n    from fred_macro import get_macro_context as g\n    return g()\n",
        "def _x():\n    import fred_macro\n    return fred_macro.get_macro_context()\n",
    ], ids=["from_import_alias", "module_attribute"])
    def test_scanner_has_teeth(self, snippet):
        """守卫有牙：给 dashboard_renderer 的副本加回直接调用（两种写法），同一个扫描函数认得出来、记到所在函数。"""
        src = (ROOT / "dashboard_renderer.py").read_text(encoding="utf-8")
        assert _sites({"dashboard_renderer.py": src}) == set()
        assert _sites({"dashboard_renderer.py": src + "\n\n" + snippet}) == {("dashboard_renderer.py", "_x")}


# ══════════════════════════════════════════════════════════════════════════
# E. 观测点：调用次数 → scan_timing → alert_manager
# ══════════════════════════════════════════════════════════════════════════

class TestCallCounter:

    def test_stats_count_calls_and_fetches(self, monkeypatch):
        monkeypatch.setattr(fm, "_fetch_macro_data", lambda: dict(MACRO))
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        fm.get_macro_context()
        fm.get_macro_context()
        assert fm.stats() == {"calls": 2, "fetches": 1}
        fm._reset_stats()
        assert fm.stats() == {"calls": 0, "fetches": 0}

    def test_scan_timing_carries_the_counter(self, monkeypatch):
        import scan_timing as stt
        monkeypatch.setattr(fm, "_fetch_macro_data", lambda: dict(MACRO))
        fm._CACHE, fm._CACHE_TS = {}, 0.0
        fm.get_macro_context()
        c = stt.counters()
        assert c["macro_context"] == {"calls": 1, "fetches": 1}
        assert "宏观快照" not in stt.summary_line({"phases": {}, "counters": c})
        fm.get_macro_context()
        assert "宏观快照被调 2 次" in stt.summary_line({"phases": {}, "counters": stt.counters()})


def _analyze(tmp_path, counter, *, ran=True, early_exit=None):
    import alert_manager as am
    extra = {"gh_pages": {"success": True}}
    if early_exit:
        extra["early_exit"] = early_exit
    status = {"status": "success",
              "steps_result": {"step2_hive_analysis": {"status": "success" if ran else "failed"}},
              "scan_timing": {"counters": {"macro_context": counter}, "extra": extra,
                              "production_sync": {"outcome": "up_to_date"}}}
    if counter is ...:
        del status["scan_timing"]["counters"]["macro_context"]
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return [x for x in a.alerts if "macro_snapshot" in (x.tags or [])], a


class TestAlert:

    @pytest.mark.parametrize("calls,word", [(2, "调了 2 次"), (5, "调了 5 次"), (0, "没有取宏观快照")])
    def test_not_exactly_once_is_p2(self, tmp_path, calls, word):
        """变红的变异：analyze() 不调这道检查；≠1 的判据写成 >1（0 次漏报）；级别不是 P2。"""
        import alert_manager as am
        hits, _ = _analyze(tmp_path, {"calls": calls, "fetches": min(calls, 1)})
        (h,) = hits
        assert h.level == am.AlertLevel.MEDIUM and word in h.message

    def test_once_is_silent_missing_is_skipped(self, tmp_path):
        hits, a = _analyze(tmp_path, {"calls": 1, "fetches": 1})
        assert hits == [] and not any("宏观快照" in s for s in a.checks_skipped)
        hits, a = _analyze(tmp_path, ...)
        assert hits == [] and any("宏观快照" in s for s in a.checks_skipped)

    def test_no_scan_or_early_exit_is_silent(self, tmp_path):
        hits, _ = _analyze(tmp_path, {"calls": 0, "fetches": 0}, ran=False)
        assert hits == []
        hits, _ = _analyze(tmp_path, {"calls": 0, "fetches": 0}, early_exit="price_gate")
        assert hits == []


# ══════════════════════════════════════════════════════════════════════════
# F. 世代边界登记（同 654 ~ 669：并进 10-19 同日标签）
# ══════════════════════════════════════════════════════════════════════════

_V = "v0.45.674"
_MARK = "macro_scan_snapshot_checked"
_SIG = {"macro_as_of_mode": "realtime", "macro_ref_date": None, "macro_data_source": "treasury+finnhub+yfinance+fred",
        "vix_source": "cboe", "vix_feed": "delayed_quote", "vix_as_of": "2026-10-27", "vix_stale": False, "vix": 17.5,
        "yield_curve": "normal", "gold_trend": "surging", "fomc_days": 1, "vix_term": "contango", "vix_term_source": "live"}


def _row(*, new=False, guard="ok", mode="realtime", **sig):
    if guard == "absent":
        return {"agent_details": {}}
    if guard == "errored":
        return {"agent_details": {"GuardBeeSentinel": {"score": 5.0, "details": {}, "error": "TypeError: boom"}}}
    det = {"macro_adj": 0.0}
    if guard == "ok":
        det["vix_term_structure"] = {**_SIG, "macro_as_of_mode": mode, **sig}
    if new:
        det[_MARK] = True
    return {"agent_details": {"GuardBeeSentinel": {"score": 5.0, "details": det}}}


def _judge(day, tk):
    import ic_rerun_readiness as rr
    return rr._equiv_macro_scan_snapshot({"swarm_results": day[tk], "_date": D, "_ticker": tk, "_day": day})


def _write(root, day, rows):
    (root / f".swarm_results_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    for tk, r in rows.items():
        (root / f"analysis-{tk}-ml-{day}.json").write_text(json.dumps({"swarm_results": r}), encoding="utf-8")


def _guard_details_literals(key):
    tree = ast.parse((ROOT / "swarm_agents" / "guard_bee.py").read_text(encoding="utf-8"))
    found = []
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "AgentResult":
            for kw in call.keywords:
                if kw.arg == "details" and isinstance(kw.value, ast.Dict):
                    found += [v for k, v in zip(kw.value.keys, kw.value.values)
                              if isinstance(k, ast.Constant) and k.value == key]
    return found


class TestBoundaryRegistered:

    def test_entry_scope_marker_equivalence(self):
        import ic_rerun_readiness as rr
        import signal_archive as sa
        from swarm_agents.guard_bee import GENERATION_MARKS
        assert [d for d, v, _r in rr._COHORT_HISTORY if v == _V] == ["2026-10-19"]
        assert sa.COHORT_SIGNAL_SCOPE[_V] == ("agent.GuardBeeSentinel.*",)
        assert _V in rr._BOUNDARY_MARKERS and _V in rr._BOUNDARY_EQUIVALENCE
        others = {rr._BOUNDARY_MARKERS[v][1] for v in ("v0.45.652", "v0.45.654", "v0.45.657", "v0.45.658",
                                                         "v0.45.660", "v0.45.663", "v0.45.669")}
        assert rr._BOUNDARY_MARKERS[_V][1] not in others
        assert _MARK in GENERATION_MARKS          # 出错路径也写（`_with_generation_marks`）

    def test_invalidates_no_rows(self):
        """「作废 0 条」。变红的变异：条目日期写成新代码上线日。"""
        import ic_rerun_readiness as rr
        without = [e for e in rr._COHORT_HISTORY if e[1] != _V]
        assert rr.cohort_start()["date"] == without[-1][0] == "2026-10-19"

    def test_moves_no_signal_generation(self):
        """「0 个信号后移」。变红的变异：作用域里加上 `guard.macro_adj`。"""
        import signal_archive as sa
        hist = sa._cohort_history()
        assert any(v == _V for _d, v, _r in hist)
        sigs = set(sa.SIGNAL_EXTRACTORS) | set(sa.UNARCHIVED_NODES)
        without = [e for e in hist if e[1] != _V]
        before, after = sa.generation_boundaries(sigs, without), sa.generation_boundaries(sigs, hist)
        moved = {s for s in sigs if (before.get(s) or {}).get("date") != (after.get(s) or {}).get("date")}
        assert moved == set(), moved

    def test_does_not_truncate_dim_ic_h1_or_h2(self):
        import sys
        sys.path.insert(0, str(ROOT / "experiments"))
        import dim_ic_forward_test as F
        import dim_ic_protocol as P
        import ic_rerun_readiness as rr
        entry = next(e for e in rr._COHORT_HISTORY if e[1] == _V)
        for name, _obj, sigs in P.HYPOTHESES:
            assert F.truncation_point(sigs, P, [entry]) is None, name

    def test_ml_estimator_generation_label_extended(self):
        import probability_scorecard as ps
        assert any(d == "2026-10-19" and _V in v.split("+") for d, v, _t in ps._ML_ESTIMATOR_GENERATIONS)

    def test_guard_success_path_writes_the_marker_unconditionally(self):
        """变红的变异：删掉那一行 / 挪进 if。"""
        found = _guard_details_literals(_MARK)
        assert len(found) == 1 and isinstance(found[0], ast.Constant) and found[0].value is True, found

    def test_guard_error_path_writes_the_marker(self):
        from swarm_agents.guard_bee import _with_generation_marks
        assert _with_generation_marks({"details": {}})["details"][_MARK] is True


class TestMarkerAndJudge:

    @pytest.mark.parametrize("d", [{}, {"swarm_results": _row()},
                                   {"swarm_results": _row(new=True, mode="backfill")}],
                             ids=["empty", "old_code", "backfill"])
    def test_marker_only_on_live_new_rows(self, d):
        """变红的变异：日期印记不排补跑行；认 669 的键。"""
        import ic_rerun_readiness as rr
        assert rr._marker_guard_scan_snapshot(d) is False
        assert rr._marker_guard_scan_snapshot({"swarm_results": _row(new=True)}) is True
        r669 = _row()
        r669["agent_details"]["GuardBeeSentinel"]["details"]["macro_single_path_checked"] = True
        assert rr._marker_guard_scan_snapshot({"swarm_results": r669}) is False

    def test_one_snapshot_day_is_equivalent(self):
        """10-19 ~ 10-27 实况：30 行同一份。出错 / 不在的行不参与比较。"""
        day = {"AAA": _row(), "BBB": _row(), "CCC": _row(guard="errored"), "DDD": _row(guard="absent")}
        assert all(_judge(day, tk) for tk in day)

    def test_split_day_is_not(self):
        """09-29：27 + 2 + 1。变红的变异：判据只看本行（恒真）/ 只比来源不比 VIX。"""
        day = {"AAA": _row(), "BBB": _row(), "CCC": _row(vix=16.07), "DDD": _row(macro_data_source="finnhub+yfinance+fred")}
        assert [_judge(day, tk) for tk in ("AAA", "BBB", "CCC", "DDD")] == [False, False, False, False]

    def test_vix_term_is_not_part_of_the_snapshot(self):
        """`vix_term` 是另一次独立取数（自己的锁 + 文件缓存），不同不算两份宏观。"""
        day = {"AAA": _row(), "BBB": _row(vix_term="backwardation")}
        assert _judge(day, "AAA") and _judge(day, "BBB")

    def test_unprovable_cases(self):
        """宏观细节读不出 / 没带当天全部行 ⇒ 证不出。一行读不出 ⇒ 全天证不出（那一行可能是另一份）。"""
        import ic_rerun_readiness as rr
        day = {"AAA": _row(), "BBB": _row(guard="no_macro")}
        assert _judge(day, "AAA") is False and _judge(day, "BBB") is False
        assert rr._equiv_macro_scan_snapshot({"swarm_results": _row(), "_date": D, "_ticker": "AAA"}) is False
        assert rr._equiv_macro_scan_snapshot({"swarm_results": _row(guard="errored"), "_date": D, "_ticker": "AAA"}) is True


class TestBoundaryVerdict:

    def test_before_deploy_is_no_evidence_yet(self, tmp_path):
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row(guard="errored")})
        _write(tmp_path, "2026-10-27", {"AAA": _row(), "BBB": _row()})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "no_evidence_yet" and ev["unmarked_after_boundary"] == [], ev

    def test_marker_on_deploy_day_matches(self, tmp_path):
        """变红的变异：删掉 `_BOUNDARY_EQUIVALENCE["v0.45.674"]`；判别器不传 `_day`（⇒ 证不出 ⇒ boundary_too_early）。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-27", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-29", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2026-10-29", ev

    def test_old_code_split_day_after_boundary_is_too_early(self, tmp_path):
        """边界之后有一天旧代码分成了两份 ⇒ 报出来，不当作等价。"""
        import ic_rerun_readiness as rr
        _write(tmp_path, "2026-10-19", {"AAA": _row(), "BBB": _row()})
        _write(tmp_path, "2026-10-28", {"AAA": _row(), "BBB": _row(vix=19.0)})
        _write(tmp_path, "2026-10-29", {"AAA": _row(new=True), "BBB": _row(new=True)})
        ev = rr.cohort_boundary_evidence(tmp_path, version=_V)
        assert ev["verdict"] == "boundary_too_early" and ev["unmarked_after_boundary"] == ["2026-10-28"], ev
