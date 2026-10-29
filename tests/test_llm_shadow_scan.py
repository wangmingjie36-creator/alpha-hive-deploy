"""
tests/test_llm_shadow_scan.py — v0.45.555：LLM 影子 A/B 入口

全部用假子进程 / 假扫描，**不起真实蜂群、不调任何 LLM、不碰真实数据根**。覆盖：
  · 标的数 / 轮换 / 根目录互斥 / 质量判定（纯函数）
  · 拷贝：只读生产、影子库 = 生产此刻的样子、状态目录跨天保留
  · 花钱闸：--run 无环境变量 ⇒ 拒绝且不拷贝；--plan 不落任何东西
  · 两进程隔离：子进程环境指向影子根；config 固化路径到生产 ⇒ 拒绝
  · 子进程：扫描异常 ⇒ 状态记 error + 退出码 3；LLM 一次没调成 ⇒ 不健康 + 退出码 5
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import types
from datetime import date
from pathlib import Path

import pytest

import llm_shadow_scan as sh

_REPO = Path(__file__).resolve().parent.parent


# ==================== 纯函数 ====================

class TestMaxTickers:
    @pytest.mark.parametrize("cap,expected", [(3.0, 33), (0.09, 1), (0.089, 0), (0, 0), (-1, 0), (10.0, 111)])
    def test_cases(self, cap, expected):
        assert sh.max_tickers(cap) == expected


class TestGenerationTwoDefinition:
    """第 2 代（v0.45.592）：Sonnet 5.5、30 只全跑、每日上限 $3。改任何一项 ⇒ 新一代。"""

    def test_model_is_sonnet_5_5_and_registered(self):
        import llm_service
        assert sh.MODEL == "claude-sonnet-5-5"
        assert sh.MODEL in llm_service._PRICING and sh.MODEL in llm_service._MODEL_PROFILES

    def test_full_watchlist_fits_under_the_daily_cap_by_the_stated_estimate(self):
        import config
        n = len(config.WATCHLIST)
        assert sh.max_tickers(sh.DAILY_CAP_USD) >= n          # 缺省就是全名单，不轮换
        assert n * sh.PER_TICKER_USD <= sh.DAILY_CAP_USD      # 估算不超上限（--plan 不会打出「预计超上限」警告）

    def test_status_and_stub_carry_the_generation(self):
        st = sh.build_status(day=date(2026, 10, 22), mode="run", model=sh.MODEL, cap=3.0, requested=["A"],
                             usage=_usage(), per_ticker=_tickers(1), elapsed=1.0, error=None)
        assert st["generation"] == sh.GENERATION == "g2-sonnet-5-5"
        stub = sh.started_stub(day=date(2026, 10, 22), mode="run", cap=3.0, requested=["A"])
        assert stub["generation"] == sh.GENERATION

    def test_control_status_has_no_generation(self):
        st = sh.build_status(day=date(2026, 10, 22), mode="control", model=sh.MODEL, cap=3.0, requested=["A"],
                             usage=_usage(), per_ticker=_tickers(1), elapsed=1.0, error=None)
        assert st["generation"] is None and st["model"] is None


class TestHistoricalAnalogyDisabledInShadow:
    """第 2 代起影子里关掉「历史类比」（agent_memory 的唯一读者；只改分不改方向）。关闭要真生效，且失效会红。"""

    @pytest.fixture
    def qd(self, monkeypatch):
        from swarm_agents.queen_distiller import QueenDistiller
        monkeypatch.setattr(QueenDistiller, "enrich_with_historical_analogy",
                            QueenDistiller.enrich_with_historical_analogy)      # 登记原值，测试结束自动还原
        return QueenDistiller

    class _Boom:
        """任何访问都炸：证明关闭后既不碰向量记忆也不碰 agent_memory。"""
        enabled = True

        def __getattr__(self, name):
            raise AssertionError(f"关闭后不该访问 {name}")

    def test_patched_function_is_a_noop_that_touches_nothing(self, qd, monkeypatch):
        import llm_service
        monkeypatch.setattr(llm_service, "find_historical_analogy",
                            lambda **kw: (_ for _ in ()).throw(AssertionError("不该调 LLM")))
        sh.disable_historical_analogy()
        d = {"final_score": 6.0, "direction": "bullish"}
        out = qd.enrich_with_historical_analogy(types.SimpleNamespace(enable_llm=True), "AAA", d,
                                                vector_memory=self._Boom(), memory_store=self._Boom())
        assert out is d and d["final_score"] == 6.0 and d["direction"] == "bullish"
        assert d["historical_analogy"] is None

    def test_positive_control_original_would_adjust_the_score(self, qd, monkeypatch):
        """有牙：没打补丁时，同样的入参会读 agent_memory、调 LLM、改分——证明上面那条「没动」是补丁的功劳。"""
        import llm_service
        calls = []

        class VM:
            enabled = True
            def search(self, **kw):
                return [{"x": i} for i in range(6)]            # 命中 ≥5 ⇒ 过门槛

        class MS:
            def get_recent_memories(self, **kw):
                calls.append("agent_memory")
                return []
        monkeypatch.setattr(llm_service, "find_historical_analogy",
                            lambda **kw: {"analogy_found": True, "confidence_adjustment": -0.03})
        d = {"final_score": 6.0, "direction": "bullish"}
        qd.enrich_with_historical_analogy(types.SimpleNamespace(enable_llm=True), "AAA", d,
                                          vector_memory=VM(), memory_store=MS())
        assert calls == ["agent_memory"] and d["final_score"] != 6.0

    def test_default_scan_applies_the_patch_before_scanning(self, qd, monkeypatch):
        import alpha_hive_daily_report as adr
        seen = {}

        class _Stop(Exception):
            pass

        class FakeReporter:
            date_str = "2026-10-22"

            def run_swarm_scan(self, focus_tickers=None):
                seen["fn"] = qd.enrich_with_historical_analogy.__name__
                raise _Stop()
        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", FakeReporter)
        with pytest.raises(_Stop):
            sh.default_scan(["AAA"])
        assert seen["fn"] == "_skipped"

    def test_run_child_does_not_patch_when_scan_is_injected(self, child_env, tmp_path):
        """补丁只在真实扫描入口（default_scan）里打：run_child 注入假扫描时不污染进程里的真类。"""
        from swarm_agents.queen_distiller import QueenDistiller
        before = QueenDistiller.enrich_with_historical_analogy
        sh.run_child(_child_args(child_env, tmp_path, "--control"), scan=lambda t: {}, now=date(2026, 10, 22))
        assert QueenDistiller.enrich_with_historical_analogy is before

    def test_fired_analogy_makes_the_day_unhealthy(self):
        pt = _tickers(10)
        pt["T3"]["analogy_fired"] = True
        q = sh.quality_report(_usage(), pt, llm_on=True)
        assert q["healthy"] is False and any("历史类比被触发（T3）" in r for r in q["reasons"])

    def test_control_mode_ignores_it(self):
        pt = _tickers(10)
        pt["T3"]["analogy_fired"] = True
        assert sh.quality_report(_usage(), pt, llm_on=False)["healthy"] is True

    @pytest.mark.parametrize("val,expected", [(None, False), ({}, False), ({"analogy_found": False}, True),
                                              ({"analogy_found": True, "score_adjustment_applied": -0.15}, True)])
    def test_per_ticker_records_whether_it_fired(self, val, expected):
        sr = {"AAA": {"final_score": 6.0, "direction": "bullish", "historical_analogy": val}}
        assert sh.per_ticker_from_swarm_results(sr, ["AAA"])["AAA"]["analogy_fired"] is expected

    def test_missing_key_means_not_fired(self):
        sr = {"AAA": {"final_score": 6.0, "direction": "bullish"}}
        assert sh.per_ticker_from_swarm_results(sr, ["AAA"])["AAA"]["analogy_fired"] is False


class TestPickTickers:
    U = [f"T{i:02d}" for i in range(30)]

    def test_deterministic(self):
        d = date(2026, 10, 19)
        assert sh.pick_tickers(self.U, d, 22) == sh.pick_tickers(self.U, d, 22)

    def test_length_and_wraps(self):
        got = sh.pick_tickers(self.U, date(2026, 10, 19), 22)
        assert len(got) == 22 and len(set(got)) == 22 and set(got) <= set(self.U)

    def test_k_ge_n_returns_all_in_order(self):
        assert sh.pick_tickers(self.U, date(2026, 10, 19), 30) == self.U
        assert sh.pick_tickers(self.U, date(2026, 10, 19), 99) == self.U

    @pytest.mark.parametrize("k", [0, -3])
    def test_nonpositive_k_is_empty(self, k):
        assert sh.pick_tickers(self.U, date(2026, 10, 19), k) == []

    def test_empty_universe(self):
        assert sh.pick_tickers([], date(2026, 10, 19), 5) == []

    def test_coverage_roughly_even_over_a_month(self):
        """每天只放 22/30：长期看每只都被覆盖，且次数相差不大（否则固定漏掉几只名字）。"""
        counts = {t: 0 for t in self.U}
        for i in range(60):
            for t in sh.pick_tickers(self.U, date.fromordinal(739900 + i), 22):
                counts[t] += 1
        assert min(counts.values()) > 0
        assert max(counts.values()) - min(counts.values()) <= 6

    def test_consecutive_days_differ(self):
        assert sh.pick_tickers(self.U, date(2026, 10, 19), 22) != sh.pick_tickers(self.U, date(2026, 10, 20), 22)


class TestRoots:
    def test_sibling_is_ok(self, tmp_path):
        sh.check_roots(tmp_path / "prod", tmp_path / "shadow")

    @pytest.mark.parametrize("shadow", ["prod", "prod/_shadow", "."])
    def test_equal_or_nested_rejected(self, tmp_path, shadow):
        prod = tmp_path / "prod"
        prod.mkdir()
        s = (tmp_path / shadow).resolve() if shadow != "." else tmp_path
        with pytest.raises(ValueError):
            sh.check_roots(prod, s)

    def test_default_is_sibling_of_prod(self, tmp_path):
        d = sh.default_shadow_root(tmp_path / "alpha-hive-data")
        assert d == tmp_path / sh.SHADOW_DIRNAME
        sh.check_roots(tmp_path / "alpha-hive-data", d)


def _usage(**kw):
    base = {"call_count": 100, "truncated_calls": 0, "json_parse_failures": 0,
            "budget_refusals": 0, "unpriced_refusals": 0, "failed_calls": 0}
    base.update(kw)
    return base


def _tickers(n=10, mode="llm_enhanced"):
    return {f"T{i}": {"present": True, "distill_mode": mode} for i in range(n)}


class TestQualityFailedCalls:
    """call_count 只数成功的——请求失败（429 耗尽 / 超时 / API 报错）以前无处可见，那些蜂静默回落规则引擎。"""

    def test_failed_calls_over_threshold_unhealthy(self):
        q = sh.quality_report(_usage(call_count=80, failed_calls=20), _tickers(), llm_on=True)   # 20/100 = 20%
        assert q["healthy"] is False and q["failed_call_rate"] == 0.2
        assert any("failed_call_rate" in r for r in q["reasons"])

    def test_failed_calls_at_threshold_ok(self):
        q = sh.quality_report(_usage(call_count=90, failed_calls=10), _tickers(), llm_on=True)   # 10/100 == 阈值
        assert q["healthy"] is True

    def test_missing_key_treated_as_zero(self):
        u = _usage()
        u.pop("failed_calls")
        assert sh.quality_report(u, _tickers(), llm_on=True)["healthy"] is True

    def test_all_failed_is_unhealthy_with_both_reasons(self):
        q = sh.quality_report(_usage(call_count=0, failed_calls=30), _tickers(mode="rule_engine"), llm_on=True)
        assert q["healthy"] is False and q["failed_call_rate"] == 1.0
        assert any("call_count=0" in r for r in q["reasons"])

    def test_status_records_model_refusals_without_affecting_health(self):
        """v0.45.673：拒答只记录、不进健康判定（NFLX 那种单只回落由 queen_llm_rate 兜）。"""
        st = sh.build_status(day=date(2026, 10, 27), mode="run", model=sh.MODEL, cap=3.0, requested=["A"],
                             usage=_usage(model_refusals=3), per_ticker=_tickers(1), elapsed=1.0, error=None)
        assert st["usage"]["model_refusals"] == 3
        assert sh.quality_report(_usage(model_refusals=50), _tickers(), llm_on=True)["healthy"] is True

    def test_status_records_failed_calls(self):
        st = sh.build_status(day=date(2026, 10, 19), mode="run", model=sh.MODEL, cap=3.0, requested=["A"],
                             usage=_usage(failed_calls=4), per_ticker=_tickers(1), elapsed=1.0, error=None)
        assert st["usage"]["failed_calls"] == 4


class TestQuality:
    def test_healthy(self):
        q = sh.quality_report(_usage(), _tickers(), llm_on=True)
        assert q["healthy"] and q["reasons"] == []

    @pytest.mark.parametrize("kw,needle", [
        ({"budget_refusals": 1}, "budget_refusals"),
        ({"unpriced_refusals": 2}, "unpriced_refusals"),
        ({"call_count": 0}, "call_count=0"),
        ({"truncated_calls": 11}, "truncation_rate"),
        ({"json_parse_failures": 11}, "json_parse_failure_rate"),
    ])
    def test_each_failure_mode_is_unhealthy(self, kw, needle):
        q = sh.quality_report(_usage(**kw), _tickers(), llm_on=True)
        assert not q["healthy"]
        assert any(needle in r for r in q["reasons"])

    def test_queen_not_using_llm_is_unhealthy(self):
        q = sh.quality_report(_usage(), _tickers(mode="rule_engine"), llm_on=True)
        assert not q["healthy"] and any("queen_llm_rate" in r for r in q["reasons"])

    def test_boundary_exactly_at_threshold_is_healthy(self):
        q = sh.quality_report(_usage(truncated_calls=10), _tickers(), llm_on=True)   # 10/100 == 阈值，不超过
        assert q["healthy"]

    def test_control_mode_never_judged_on_llm_metrics(self):
        q = sh.quality_report(_usage(call_count=0), _tickers(mode="rule_engine"), llm_on=False)
        assert q["healthy"] and q["llm_on"] is False

    def test_missing_counters_do_not_crash(self):
        q = sh.quality_report({}, {}, llm_on=True)
        assert not q["healthy"]                                    # call_count=0
        assert q["truncation_rate"] is None                         # 没有分母 ⇒ None，不是 0


class TestPerTicker:
    def test_missing_ticker_marked_absent(self):
        out = sh.per_ticker_from_swarm_results({"AAA": {"final_score": 6.1}}, ["AAA", "BBB"])
        assert out["BBB"] == {"present": False}
        assert out["AAA"]["present"] is True

    def test_missing_fields_are_none_not_zero(self):
        out = sh.per_ticker_from_swarm_results({"AAA": {"final_score": 6.1}}, ["AAA"])["AAA"]
        assert out["final_score"] == 6.1
        assert out["rule_score"] is None and out["llm_confidence"] is None

    def test_non_dict_entry_is_absent(self):
        assert sh.per_ticker_from_swarm_results({"AAA": None}, ["AAA"])["AAA"] == {"present": False}


class TestBuildStatus:
    def _st(self, mode="run"):
        pt = {"AAA": {"present": True, "distill_mode": "llm_enhanced"}, "BBB": {"present": False}}
        return sh.build_status(day=date(2026, 10, 19), mode=mode, model=sh.MODEL, cap=3.0,
                               requested=["AAA", "BBB"], usage=_usage(total_cost_usd=0.5, input_tokens=9, output_tokens=3),
                               per_ticker=pt, elapsed=12.34, error=None)

    def test_scanned_excludes_absent(self):
        st = self._st()
        assert st["tickers_requested"] == ["AAA", "BBB"] and st["tickers_scanned"] == ["AAA"]

    def test_model_recorded_only_when_llm_on(self):
        assert self._st("run")["model"] == sh.MODEL
        assert self._st("control")["model"] is None

    def test_json_serialisable_and_strict(self):
        json.dumps(self._st(), allow_nan=False)


# ==================== 拷贝 ====================

def _make_prod(tmp_path):
    prod = tmp_path / "prod"
    (prod / "cache").mkdir(parents=True)
    db = prod / "pheromone.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, ticker TEXT)")
    con.executemany("INSERT INTO predictions(ticker) VALUES (?)", [("AAA",), ("BBB",)])
    con.commit()
    con.close()
    (prod / "cache" / "iv_history_AAA.jsonl").write_text('{"iv": 1}\n', encoding="utf-8")
    (prod / "cache" / "options_snapshot_AAA_2026-10-19.json").write_text("{}", encoding="utf-8")
    return prod


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class TestSeed:
    def test_db_and_cache_copied(self, tmp_path):
        prod = _make_prod(tmp_path)
        shadow = tmp_path / "shadow"
        sh.seed_shadow_root(prod / "pheromone.db", prod / "cache", shadow)
        con = sqlite3.connect(shadow / "pheromone.db")
        assert [r[0] for r in con.execute("SELECT ticker FROM predictions ORDER BY id")] == ["AAA", "BBB"]
        con.close()
        assert (shadow / "cache" / "iv_history_AAA.jsonl").read_text(encoding="utf-8") == '{"iv": 1}\n'
        assert (shadow / "cache" / "options_snapshot_AAA_2026-10-19.json").exists()

    def test_production_is_not_modified(self, tmp_path):
        prod = _make_prod(tmp_path)
        files = [p for p in prod.rglob("*") if p.is_file()]
        before = {p: (_digest(p), p.stat().st_mtime_ns) for p in files}
        sh.seed_shadow_root(prod / "pheromone.db", prod / "cache", tmp_path / "shadow")
        after = {p: (_digest(p), p.stat().st_mtime_ns) for p in prod.rglob("*") if p.is_file()}
        assert {p: v for p, v in after.items() if p in before} == before
        assert set(after) - set(before) <= {prod / "pheromone.db-wal", prod / "pheromone.db-shm"}

    def test_reseed_replaces_stale_db_and_cache_but_keeps_status(self, tmp_path):
        prod = _make_prod(tmp_path)
        shadow = tmp_path / "shadow"
        (shadow / "cache").mkdir(parents=True)
        (shadow / "cache" / "stale.json").write_text("old", encoding="utf-8")
        (shadow / "pheromone.db").write_bytes(b"not a database")
        (shadow / sh.STATUS_DIRNAME).mkdir()
        (shadow / sh.STATUS_DIRNAME / "2026-10-16.json").write_text("{}", encoding="utf-8")
        sh.seed_shadow_root(prod / "pheromone.db", prod / "cache", shadow)
        assert not (shadow / "cache" / "stale.json").exists()
        assert (shadow / sh.STATUS_DIRNAME / "2026-10-16.json").exists()      # 比较结果跨天保留
        sqlite3.connect(shadow / "pheromone.db").execute("SELECT count(*) FROM predictions").fetchone()

    def test_missing_cache_is_ok(self, tmp_path):
        prod = _make_prod(tmp_path)
        sh.seed_shadow_root(prod / "pheromone.db", tmp_path / "nope", tmp_path / "shadow")
        assert (tmp_path / "shadow" / "pheromone.db").exists()

    def test_missing_source_db_fails_loudly(self, tmp_path):
        with pytest.raises(sqlite3.Error):
            sh.seed_shadow_root(tmp_path / "absent.db", tmp_path / "cache", tmp_path / "shadow")


# ==================== 子进程环境 / config 固化检查 ====================

class TestChildEnv:
    def test_points_to_shadow_and_sets_budget(self, tmp_path):
        env = sh.build_child_env({"PATH": "/bin", "ALPHA_HIVE_HOME": "/prod"}, tmp_path / "shadow", 3.0)
        assert env["ALPHA_HIVE_HOME"] == str(tmp_path / "shadow")
        assert float(env["ALPHA_HIVE_LLM_BUDGET_USD"]) == 3.0
        assert env["PATH"] == "/bin"

    def test_overrides_that_redirect_paths_are_removed(self, tmp_path):
        env = sh.build_child_env({"ALPHA_HIVE_DB_PATH": "/prod/x.db", "ALPHA_HIVE_CACHE_DIR": "/prod/c",
                                  "ALPHA_HIVE_TARGET_DATE": "2026-01-01", "ALPHA_HIVE_PHEROMONE_DB": "/p"},
                                 tmp_path / "shadow", 3.0)
        for k in ("ALPHA_HIVE_DB_PATH", "ALPHA_HIVE_CACHE_DIR", "ALPHA_HIVE_TARGET_DATE", "ALPHA_HIVE_PHEROMONE_DB"):
            assert k not in env

    def test_does_not_mutate_input_and_keeps_spend_flag(self, tmp_path):
        src = {"ALPHA_HIVE_LLM_SHADOW_SPEND": "1", "ALPHA_HIVE_HOME": "/prod"}
        env = sh.build_child_env(src, tmp_path / "shadow", 3.0)
        assert src == {"ALPHA_HIVE_LLM_SHADOW_SPEND": "1", "ALPHA_HIVE_HOME": "/prod"}
        assert env["ALPHA_HIVE_LLM_SHADOW_SPEND"] == "1"


class TestFrozenPaths:
    def test_detects_top_level_and_nested(self, tmp_path):
        prod = tmp_path / "prod"
        mod = types.SimpleNamespace(
            DB=str(prod / "pheromone.db"),
            CFG={"db_path": prod / "x.db", "cache_dir": str(tmp_path / "shadow" / "cache"),
                 "deep": {"log": str(prod / "logs" / "a.log")}},
            OK={"cache_dir": str(tmp_path / "shadow" / "cache")},
        )
        bad = sh.frozen_prod_paths(mod, prod)
        assert "DB" in bad
        assert "CFG['db_path']" in bad
        assert "CFG['deep']['log']" in bad
        assert not any(b.startswith("OK") for b in bad)

    def test_clean_module_is_empty(self, tmp_path):
        mod = types.SimpleNamespace(A="x", B={"p": str(tmp_path / "shadow")})
        assert sh.frozen_prod_paths(mod, tmp_path / "prod") == []


# ==================== 参数 ====================

class TestParseArgs:
    def test_default_is_plan(self):
        a = sh.parse_args([])
        assert a.plan and not a.run and not a.control and a.daily_cap == 3.0

    def test_modes_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            sh.parse_args(["--run", "--control"])

    @pytest.mark.parametrize("cap", ["0", "-1"])
    def test_nonpositive_cap_rejected(self, cap):
        with pytest.raises(SystemExit):
            sh.parse_args(["--daily-cap", cap])

    def test_child_requires_its_arguments(self):
        with pytest.raises(SystemExit):
            sh.parse_args(["--child", "--run"])


# ==================== 父进程：计划 / 闸 / 拷贝 / 起子进程 ====================

@pytest.fixture
def prod_env(tmp_path, monkeypatch):
    prod = _make_prod(tmp_path)
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(prod))
    monkeypatch.delenv("ALPHA_HIVE_DB_PATH", raising=False)
    monkeypatch.delenv("ALPHA_HIVE_CACHE_DIR", raising=False)
    monkeypatch.delenv(sh.SPEND_ENV, raising=False)
    return prod


class _Spawn:
    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, cmd, env=None, cwd=None):
        self.calls.append({"cmd": cmd, "env": env, "cwd": cwd})
        return types.SimpleNamespace(returncode=self.rc)


class TestRunShadowParent:
    def test_plan_touches_nothing(self, prod_env, tmp_path, capsys):
        spawn = _Spawn()
        rc = sh.run_shadow(sh.parse_args(["--plan"]), spawn=spawn, now=date(2026, 10, 19))
        assert rc == 0 and spawn.calls == []
        assert not (tmp_path / sh.SHADOW_DIRNAME).exists()
        out = capsys.readouterr().out
        assert "标的 30/30" in out and "$3.00" in out

    def test_run_without_spend_env_refused_and_nothing_copied(self, prod_env, tmp_path):
        spawn = _Spawn()
        rc = sh.run_shadow(sh.parse_args(["--run"]), spawn=spawn, now=date(2026, 10, 19))
        assert rc == sh.EXIT_USAGE and spawn.calls == []
        assert not (tmp_path / sh.SHADOW_DIRNAME).exists()

    def test_run_with_spend_env_seeds_and_spawns_child_on_shadow(self, prod_env, tmp_path, monkeypatch):
        monkeypatch.setenv(sh.SPEND_ENV, "1")
        spawn = _Spawn(rc=0)
        rc = sh.run_shadow(sh.parse_args(["--run", "--tickers", "3"]), spawn=spawn, now=date(2026, 10, 19))
        assert rc == 0
        shadow = tmp_path / sh.SHADOW_DIRNAME
        assert (shadow / "pheromone.db").exists() and (shadow / "cache").is_dir()
        (call,) = spawn.calls
        cmd, env = call["cmd"], call["env"]
        assert "--child" in cmd and "--run" in cmd
        assert cmd[cmd.index("--shadow-root") + 1] == str(shadow)
        assert cmd[cmd.index("--prod-root") + 1] == str(prod_env)
        assert len(cmd[cmd.index("--child-tickers") + 1].split(",")) == 3
        assert env["ALPHA_HIVE_HOME"] == str(shadow)               # 子进程从启动那刻起就指向影子根
        assert float(env["ALPHA_HIVE_LLM_BUDGET_USD"]) == 3.0
        assert call["cwd"] == str(_REPO)

    def test_control_needs_no_spend_env(self, prod_env, tmp_path):
        spawn = _Spawn()
        rc = sh.run_shadow(sh.parse_args(["--control", "--tickers", "2"]), spawn=spawn, now=date(2026, 10, 19))
        assert rc == 0 and len(spawn.calls) == 1 and "--control" in spawn.calls[0]["cmd"]

    def test_child_exit_code_is_propagated(self, prod_env, monkeypatch):
        monkeypatch.setenv(sh.SPEND_ENV, "1")
        assert sh.run_shadow(sh.parse_args(["--run", "--tickers", "1"]), spawn=_Spawn(rc=5),
                             now=date(2026, 10, 19)) == 5

    def test_nested_shadow_root_rejected_before_any_copy(self, prod_env):
        bad = prod_env / "inside"
        with pytest.raises(ValueError):
            sh.run_shadow(sh.parse_args(["--control", "--shadow-root", str(bad)]), spawn=_Spawn(),
                          now=date(2026, 10, 19))
        assert not bad.exists()

    def test_production_files_unchanged_by_a_run(self, prod_env, monkeypatch):
        monkeypatch.setenv(sh.SPEND_ENV, "1")
        before = {p: _digest(p) for p in prod_env.rglob("*") if p.is_file() and p.suffix != ".db-shm"}
        sh.run_shadow(sh.parse_args(["--run", "--tickers", "2"]), spawn=_Spawn(), now=date(2026, 10, 19))
        after = {p: _digest(p) for p in prod_env.rglob("*") if p.is_file()}
        assert {p: after[p] for p in before if p in after} == before


# ==================== 子进程 ====================

@pytest.fixture
def child_env(tmp_path, monkeypatch):
    import llm_service
    import config
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    sqlite3.connect(shadow / "pheromone.db").close()
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(shadow))
    monkeypatch.delenv("ALPHA_HIVE_DB_PATH", raising=False)
    monkeypatch.setenv(sh.SPEND_ENV, "1")        # 子进程自己也核一次花钱闸（见 TestChildSpendGate）
    monkeypatch.setitem(config.LLM_CONFIG, "daily_budget_usd", 3.0)
    monkeypatch.setattr(llm_service, "_disabled", False)
    monkeypatch.setattr(llm_service, "_model_override", None)
    monkeypatch.setattr(llm_service, "_client", None)
    monkeypatch.setattr(llm_service, "_token_usage", {
        "input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0, "call_count": 0,
        "truncated_calls": 0, "json_parse_failures": 0, "budget_refusals": 0, "unpriced_refusals": 0,
        "failed_calls": 0})
    return shadow


def _child_args(shadow, tmp_path, *flags, tickers="AAA,BBB"):
    return sh.parse_args([*flags, "--child", "--shadow-root", str(shadow),
                          "--prod-root", str(tmp_path / "prod"), "--child-tickers", tickers])


def _fake_results(mode="llm_enhanced"):
    return {t: {"final_score": 6.5, "direction": "bullish", "rule_score": 6.0, "rule_direction": "neutral",
                "distill_mode": mode, "llm_confidence": 0.7, "data_real_pct": 90.0} for t in ("AAA", "BBB")}


class TestRunChild:
    def test_refuses_when_env_root_is_not_shadow(self, child_env, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "somewhere-else"))
        with pytest.raises(RuntimeError, match="不是影子根"):
            sh.run_child(_child_args(child_env, tmp_path, "--control"), scan=lambda t: {})

    def test_refuses_when_config_froze_production_paths(self, child_env, tmp_path, monkeypatch):
        monkeypatch.setattr(sh, "frozen_prod_paths", lambda cfg, prod: ["PIPELINE['db_path']"])
        with pytest.raises(RuntimeError, match="固化在生产根"):
            sh.run_child(_child_args(child_env, tmp_path, "--control"), scan=lambda t: {})

    def test_refuses_when_budget_did_not_reach_config(self, child_env, tmp_path, monkeypatch):
        import config
        monkeypatch.setitem(config.LLM_CONFIG, "daily_budget_usd", 1.0)     # 与 --daily-cap 3.0 不一致
        with pytest.raises(RuntimeError, match="预算上限没传到位"):
            sh.run_child(_child_args(child_env, tmp_path, "--control"), scan=lambda t: {})

    def test_control_scan_writes_status_and_results_and_exits_zero(self, child_env, tmp_path):
        calls = []
        rc = sh.run_child(_child_args(child_env, tmp_path, "--control"),
                          scan=lambda t: calls.append(t) or _fake_results("rule_engine"), now=date(2026, 10, 19))
        assert rc == 0 and calls == [["AAA", "BBB"]]
        st = json.loads((child_env / sh.STATUS_DIRNAME / "2026-10-19.control.json").read_text(encoding="utf-8"))
        assert st["mode"] == "control" and st["model"] is None
        assert st["per_ticker"]["AAA"]["rule_score"] == 6.0
        assert (child_env / sh.STATUS_DIRNAME / "swarm_results-2026-10-19.control.json").exists()

    def test_run_mode_sets_model_override(self, child_env, tmp_path, monkeypatch):
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: _fake_results(),
                     now=date(2026, 10, 19))
        assert llm_service._model_override == sh.MODEL

    def test_run_mode_with_zero_llm_calls_is_unhealthy(self, child_env, tmp_path, monkeypatch):
        """LLM 一次都没调成（比如全被拒绝）时，不能把这一天当成「LLM 臂」：退出码 5，状态里写明原因。"""
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        rc = sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: _fake_results("rule_engine"),
                          now=date(2026, 10, 19))
        assert rc == sh.EXIT_UNHEALTHY
        st = json.loads((child_env / sh.STATUS_DIRNAME / "2026-10-19.json").read_text(encoding="utf-8"))
        assert st["quality"]["healthy"] is False
        assert any("call_count=0" in r for r in st["quality"]["reasons"])

    def test_run_mode_healthy_day(self, child_env, tmp_path, monkeypatch):
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        monkeypatch.setattr(llm_service, "get_usage", lambda: _usage(total_cost_usd=0.9))
        rc = sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: _fake_results(),
                          now=date(2026, 10, 19))
        assert rc == 0

    def test_scan_exception_recorded_and_exit_3(self, child_env, tmp_path):
        def boom(_):
            raise RuntimeError("CBOE 全挂")
        rc = sh.run_child(_child_args(child_env, tmp_path, "--control"), scan=boom, now=date(2026, 10, 19))
        assert rc == sh.EXIT_SCAN_ERROR
        st = json.loads((child_env / sh.STATUS_DIRNAME / "2026-10-19.control.json").read_text(encoding="utf-8"))
        assert "CBOE 全挂" in st["error"]

    # ---- v0.45.683：扫描中途出错时救回已写的结果（2026-10-29：27 只付费结果因 P4b 抛 API 超时而没进状态文件）----

    def _salvage_run(self, child_env, tmp_path, monkeypatch, *, day, write_file=True, age=0.0, mode_flag="--run"):
        import os
        import time
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        monkeypatch.setattr(llm_service, "get_usage", lambda: _usage(total_cost_usd=2.0, call_count=200))

        def scan_then_boom(_):
            if write_file:
                f = child_env / f".swarm_results_{day.isoformat()}.json"
                f.write_text(json.dumps(_fake_results()), encoding="utf-8")
                if age:
                    old = time.time() - age
                    os.utime(f, (old, old))
            raise TimeoutError("P4b 超时")
        rc = sh.run_child(_child_args(child_env, tmp_path, mode_flag), scan=scan_then_boom, now=day)
        suffix = ".control.json" if mode_flag == "--control" else ".json"
        st = json.loads((child_env / sh.STATUS_DIRNAME / f"{day.isoformat()}{suffix}").read_text(encoding="utf-8"))
        return rc, st

    def test_paid_scan_error_after_results_were_written_salvages_them_but_stays_unhealthy(
            self, child_env, tmp_path, monkeypatch):
        day = date(2026, 10, 29)
        rc, st = self._salvage_run(child_env, tmp_path, monkeypatch, day=day)
        assert rc == sh.EXIT_SCAN_ERROR                                   # 仍然报扫描异常
        assert "P4b 超时" in st["error"] and st["salvaged"] is True
        assert st["tickers_scanned"] == ["AAA", "BBB"]                    # 已写的逐只结果记下来了
        assert st["per_ticker"]["AAA"]["distill_mode"] == "llm_enhanced"
        assert st["quality"]["healthy"] is False                          # ……但这天不入比较，是否纳入由人定
        assert any(r.startswith("scan_error") for r in st["quality"]["reasons"])
        assert (child_env / sh.STATUS_DIRNAME / f"swarm_results-{day.isoformat()}.json").exists()

    def test_salvaged_day_would_be_healthy_without_the_scan_error_reason(self):
        """守住「出错必须是不健康理由」：救回后逐只数据全是 LLM 蒸馏，别的指标都正常——只有 scan_error 把它挡在比较之外。"""
        pt = {t: {"present": True, "distill_mode": "llm_enhanced"} for t in ("AAA", "BBB")}
        assert sh.quality_report(_usage(), pt, llm_on=True)["healthy"] is True
        q = sh.quality_report(_usage(), pt, llm_on=True, error="APITimeoutError: x")
        assert q["healthy"] is False and q["reasons"][0].startswith("scan_error")

    def test_scan_error_reason_is_not_added_in_control_mode(self):
        pt = {"AAA": {"present": True, "distill_mode": "rule_engine"}}
        assert sh.quality_report(_usage(), pt, llm_on=False, error="boom")["healthy"] is True

    def test_no_file_written_means_nothing_to_salvage_and_the_reason_is_in_the_error(
            self, child_env, tmp_path, monkeypatch):
        rc, st = self._salvage_run(child_env, tmp_path, monkeypatch, day=date(2026, 10, 29), write_file=False)
        assert rc == sh.EXIT_SCAN_ERROR and "salvaged" not in st
        assert "没救回已写的结果" in st["error"] and "不存在" in st["error"]
        assert st["tickers_scanned"] == []

    def test_a_file_from_before_this_run_is_not_salvaged(self, child_env, tmp_path, monkeypatch):
        """同日更早一次零花费失败留下的旧文件，不能当成这次付费跑的结果。"""
        rc, st = self._salvage_run(child_env, tmp_path, monkeypatch, day=date(2026, 10, 29), age=3600.0)
        assert "salvaged" not in st and "起跑之前" in st["error"] and st["tickers_scanned"] == []

    def test_control_mode_does_not_salvage(self, child_env, tmp_path, monkeypatch):
        """control 免费、可重跑；救回来只会把一份不完整的基线写进去。"""
        rc, st = self._salvage_run(child_env, tmp_path, monkeypatch, day=date(2026, 10, 29), mode_flag="--control")
        assert rc == sh.EXIT_SCAN_ERROR and "salvaged" not in st and st["tickers_scanned"] == []

    def test_unreadable_file_does_not_raise_from_the_salvage_path(self, child_env, tmp_path):
        day = date(2026, 10, 29)
        (child_env / f".swarm_results_{day.isoformat()}.json").write_text("{坏的", encoding="utf-8")
        got, why = sh.salvage_swarm_results(child_env, day, 0.0)
        assert got == {} and "读" in why and "失败" in why
        (child_env / f".swarm_results_{day.isoformat()}.json").write_text("[]", encoding="utf-8")
        assert sh.salvage_swarm_results(child_env, day, 0.0)[0] == {}

    def test_clean_run_has_no_salvaged_key(self, child_env, tmp_path, monkeypatch):
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        monkeypatch.setattr(llm_service, "get_usage", lambda: _usage(total_cost_usd=0.9))
        sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: _fake_results(), now=date(2026, 10, 30))
        st = json.loads((child_env / sh.STATUS_DIRNAME / "2026-10-30.json").read_text(encoding="utf-8"))
        assert "salvaged" not in st and st["error"] is None

    def test_run_without_api_key_exits_usage_without_scanning(self, child_env, tmp_path, monkeypatch):
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: None)
        called = []
        rc = sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: called.append(t) or {})
        assert rc == sh.EXIT_USAGE and called == []


class TestChildSpendGate:
    def test_child_run_without_spend_env_refused_before_anything(self, child_env, tmp_path, monkeypatch):
        """有人直接 `--child --run` 就绕过了父进程的花钱闸——子进程自己也得核。"""
        monkeypatch.delenv(sh.SPEND_ENV, raising=False)
        called = []
        rc = sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: called.append(t) or {})
        assert rc == sh.EXIT_USAGE and called == []
        assert not (child_env / sh.STATUS_DIRNAME).exists()

    def test_child_control_needs_no_spend_env(self, child_env, tmp_path, monkeypatch):
        monkeypatch.delenv(sh.SPEND_ENV, raising=False)
        assert sh.run_child(_child_args(child_env, tmp_path, "--control"), scan=lambda t: _fake_results("rule_engine"),
                            now=date(2026, 10, 19)) == 0

    def test_child_day_argument_overrides_clock(self, child_env, tmp_path):
        args = sh.parse_args(["--control", "--child", "--shadow-root", str(child_env), "--prod-root",
                              str(tmp_path / "prod"), "--child-tickers", "AAA", "--child-day", "2026-10-17"])
        sh.run_child(args, scan=lambda t: _fake_results("rule_engine"), now=date(2026, 10, 19))
        assert (child_env / sh.STATUS_DIRNAME / "2026-10-17.control.json").exists()


class TestRunAndControlDoNotCollide:
    """晚到的 --control 不许覆盖已经花了钱的 --run（状态与 swarm_results 全文都是）。"""

    def test_control_after_run_same_day_keeps_run_record(self, child_env, tmp_path, monkeypatch):
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        monkeypatch.setattr(llm_service, "get_usage", lambda: _usage(total_cost_usd=0.9))
        day = date(2026, 10, 19)
        sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: _fake_results(), now=day)
        before = (child_env / sh.STATUS_DIRNAME / "2026-10-19.json").read_bytes()
        before_sr = (child_env / sh.STATUS_DIRNAME / "swarm_results-2026-10-19.json").read_bytes()
        sh.run_child(_child_args(child_env, tmp_path, "--control"), scan=lambda t: _fake_results("rule_engine"), now=day)
        assert (child_env / sh.STATUS_DIRNAME / "2026-10-19.json").read_bytes() == before
        assert (child_env / sh.STATUS_DIRNAME / "swarm_results-2026-10-19.json").read_bytes() == before_sr
        assert (child_env / sh.STATUS_DIRNAME / "2026-10-19.control.json").exists()

    def test_status_path_by_mode(self, tmp_path):
        assert sh.status_path(tmp_path, "2026-10-19", "run").name == "2026-10-19.json"
        assert sh.status_path(tmp_path, "2026-10-19", "control").name == "2026-10-19.control.json"
        assert sh.swarm_results_path(tmp_path, "2026-10-19", "control").name == "swarm_results-2026-10-19.control.json"


class TestDayLockIsSpendAware:
    """v0.45.633：锁的是「花过钱或花费未知」，不是「文件存在」。10-23 全站 TLS EOF、$0 的失败不该把当天锁死。"""

    def _doc(self, **over):
        d = {"schema": 1, "date": "2026-10-23", "mode": "run", "model": sh.MODEL, "generation": sh.GENERATION,
             "usage": {k: 0 for k in sh._SPEND_SIGNS}, "quality": {"healthy": False, "reasons": ["call_count=0"]},
             "per_ticker": {}, "error": "ScanInputsUnavailable: 30/30 只标的取不到可用现价", "elapsed_sec": 1068.4,
             "written_at": "2026-10-23T15:02:50"}
        d.update(over)
        return d

    def _write(self, tmp_path, doc):
        p = tmp_path / "2026-10-23.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def test_no_file_not_locked(self, tmp_path):
        assert sh.day_lock(tmp_path / "nope.json") == (False, "", [])

    def test_zero_spend_failure_is_not_locked_and_summarised(self, tmp_path):
        locked, why, attempts = sh.day_lock(self._write(tmp_path, self._doc()))
        assert locked is False and "零花费" in why
        (a,) = attempts
        assert a["error"].startswith("ScanInputsUnavailable") and a["spent_usd"] == 0.0 and a["elapsed_sec"] == 1068.4

    def test_earlier_attempts_are_carried_forward(self, tmp_path):
        doc = self._doc(previous_attempts=[{"error": "first", "spent_usd": 0.0}])
        _, _, attempts = sh.day_lock(self._write(tmp_path, doc))
        assert [a["error"] for a in attempts][0] == "first" and len(attempts) == 2

    @pytest.mark.parametrize("sign", list(sh._SPEND_SIGNS))
    def test_any_spend_sign_locks(self, tmp_path, sign):
        usage = {k: 0 for k in sh._SPEND_SIGNS}
        usage[sign] = 0.01 if sign == "total_cost_usd" else 1
        locked, why, _ = sh.day_lock(self._write(tmp_path, self._doc(usage=usage)))
        assert locked is True and "付费记录" in why

    def test_crash_stub_locks(self, tmp_path):
        stub = sh.started_stub(day=date(2026, 10, 23), mode="run", cap=3.0, requested=["A"])
        locked, why, _ = sh.day_lock(self._write(tmp_path, stub))
        assert locked is True and "占位" in why

    @pytest.mark.parametrize("bad", ["{not json", "[]", '"x"'])
    def test_unreadable_or_odd_file_locks(self, tmp_path, bad):
        p = tmp_path / "2026-10-23.json"
        p.write_text(bad, encoding="utf-8")
        assert sh.day_lock(p)[0] is True

    @pytest.mark.parametrize("usage", [{}, {"call_count": 0}, None, "x", {"total_cost_usd": None}])
    def test_missing_or_unknown_spend_locks(self, tmp_path, usage):
        """花费字段缺失 = 花费未知，不能当成 $0。"""
        locked, _, _ = sh.day_lock(self._write(tmp_path, self._doc(usage=usage)))
        assert locked is True


class TestRerunAfterZeroSpendFailure:
    @pytest.fixture
    def failed_day(self, prod_env, tmp_path, monkeypatch):
        monkeypatch.setenv(sh.SPEND_ENV, "1")
        shadow = tmp_path / sh.SHADOW_DIRNAME

        def spawn_fail(cmd, env=None, cwd=None):
            sh.write_status(shadow, sh.build_status(
                day=date(2026, 10, 23), mode="run", model=sh.MODEL, cap=3.0, requested=["A"], usage=_usage(
                    call_count=0, total_cost_usd=0.0, failed_calls=0), per_ticker={}, elapsed=1068.4,
                error="ScanInputsUnavailable: 30/30 只标的取不到可用现价"))
            return types.SimpleNamespace(returncode=sh.EXIT_SCAN_ERROR)
        assert sh.run_shadow(sh.parse_args(["--run", "--tickers", "2"]), spawn=spawn_fail,
                             now=date(2026, 10, 23)) == sh.EXIT_SCAN_ERROR
        return shadow

    def test_second_run_allowed_and_old_failure_moves_into_the_stub(self, failed_day, tmp_path, monkeypatch):
        seen = {}

        def spawn(cmd, env=None, cwd=None):
            seen["stub"] = json.loads((failed_day / sh.STATUS_DIRNAME / "2026-10-23.json").read_text(encoding="utf-8"))
            return types.SimpleNamespace(returncode=0)
        assert sh.run_shadow(sh.parse_args(["--run", "--tickers", "2"]), spawn=spawn, now=date(2026, 10, 23)) == 0
        assert seen["stub"]["state"] == "started"
        (a,) = seen["stub"]["previous_attempts"]
        assert "ScanInputsUnavailable" in a["error"]

    def test_second_run_after_a_paid_day_is_still_refused(self, prod_env, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(sh.SPEND_ENV, "1")
        shadow = tmp_path / sh.SHADOW_DIRNAME

        def spawn_paid(cmd, env=None, cwd=None):
            sh.write_status(shadow, sh.build_status(
                day=date(2026, 10, 23), mode="run", model=sh.MODEL, cap=3.0, requested=["A"],
                usage=_usage(total_cost_usd=1.9, call_count=200), per_ticker=_tickers(1), elapsed=1.0, error=None))
            return types.SimpleNamespace(returncode=0)
        sh.run_shadow(sh.parse_args(["--run", "--tickers", "2"]), spawn=spawn_paid, now=date(2026, 10, 23))
        spawn2 = _Spawn()
        assert sh.run_shadow(sh.parse_args(["--run", "--tickers", "2"]), spawn=spawn2, now=date(2026, 10, 23)) == sh.EXIT_USAGE
        assert spawn2.calls == [] and "付费记录" in capsys.readouterr().out

    def test_child_run_final_status_carries_attempts(self, child_env, tmp_path, monkeypatch):
        import llm_service
        monkeypatch.setattr(llm_service, "_get_client", lambda: object())
        monkeypatch.setattr(llm_service, "get_usage", lambda: _usage(total_cost_usd=0.9))
        day = date(2026, 10, 23)
        sh.write_status(child_env, sh.started_stub(day=day, mode="run", cap=3.0, requested=["AAA", "BBB"],
                                                   previous_attempts=[{"error": "boom", "spent_usd": 0.0}]))
        sh.run_child(_child_args(child_env, tmp_path, "--run"), scan=lambda t: _fake_results(), now=day)
        final = json.loads((child_env / sh.STATUS_DIRNAME / "2026-10-23.json").read_text(encoding="utf-8"))
        assert final.get("state") is None and final["previous_attempts"][0]["error"] == "boom"


class TestSameDayRerunGuard:
    """每日硬上限只在进程内计；同一天再起一个 --run 就是第二份预算。跨进程靠状态文件挡。"""

    def _run(self, prod_env, spawn, monkeypatch, day=date(2026, 10, 19)):
        monkeypatch.setenv(sh.SPEND_ENV, "1")
        return sh.run_shadow(sh.parse_args(["--run", "--tickers", "2"]), spawn=spawn, now=day)

    def test_stub_written_before_child_starts(self, prod_env, tmp_path, monkeypatch):
        seen = {}

        def spawn(cmd, env=None, cwd=None):
            p = tmp_path / sh.SHADOW_DIRNAME / sh.STATUS_DIRNAME / "2026-10-19.json"
            seen["doc"] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
            return types.SimpleNamespace(returncode=0)
        self._run(prod_env, spawn, monkeypatch)
        assert seen["doc"]["state"] == "started" and seen["doc"]["quality"]["healthy"] is False
        assert seen["doc"]["mode"] == "run"

    def test_second_run_same_day_refused_and_nothing_copied_or_spawned(self, prod_env, tmp_path, monkeypatch, capsys):
        assert self._run(prod_env, _Spawn(rc=0), monkeypatch) == 0         # 子进程没写完 ⇒ 占位还在
        spawn2 = _Spawn()
        marker = tmp_path / sh.SHADOW_DIRNAME / "pheromone.db"
        mtime = marker.stat().st_mtime_ns
        assert self._run(prod_env, spawn2, monkeypatch) == sh.EXIT_USAGE
        assert spawn2.calls == [] and marker.stat().st_mtime_ns == mtime
        assert "已有付费记录" in capsys.readouterr().out

    def test_next_day_not_blocked(self, prod_env, monkeypatch):
        assert self._run(prod_env, _Spawn(rc=0), monkeypatch, day=date(2026, 10, 19)) == 0
        assert self._run(prod_env, _Spawn(rc=0), monkeypatch, day=date(2026, 10, 20)) == 0

    def test_finished_status_also_blocks_rerun(self, prod_env, tmp_path, monkeypatch):
        """子进程正常写完（哪怕不健康，exit 5）也算今天花过——不能再来一次。"""
        shadow = tmp_path / sh.SHADOW_DIRNAME

        def spawn(cmd, env=None, cwd=None):
            sh.write_status(shadow, sh.build_status(
                day=date(2026, 10, 19), mode="run", model=sh.MODEL, cap=3.0, requested=["A"],
                usage=_usage(total_cost_usd=2.5), per_ticker={"A": {"present": True, "distill_mode": "llm_enhanced"}},
                elapsed=1.0, error=None))
            return types.SimpleNamespace(returncode=5)
        assert self._run(prod_env, spawn, monkeypatch) == 5
        assert self._run(prod_env, _Spawn(), monkeypatch) == sh.EXIT_USAGE

    def test_usage_error_exit_releases_the_day(self, prod_env, tmp_path, monkeypatch):
        """子进程在花钱之前就以用法错误退出（没 API Key）⇒ 没花钱，不该锁住这一天。"""
        assert self._run(prod_env, _Spawn(rc=sh.EXIT_USAGE), monkeypatch) == sh.EXIT_USAGE
        assert not (tmp_path / sh.SHADOW_DIRNAME / sh.STATUS_DIRNAME / "2026-10-19.json").exists()
        assert self._run(prod_env, _Spawn(rc=0), monkeypatch) == 0

    def test_spawn_failure_releases_the_day_and_propagates(self, prod_env, tmp_path, monkeypatch):
        def boom(cmd, env=None, cwd=None):
            raise OSError("exec failed")
        with pytest.raises(OSError):
            self._run(prod_env, boom, monkeypatch)
        assert not (tmp_path / sh.SHADOW_DIRNAME / sh.STATUS_DIRNAME / "2026-10-19.json").exists()

    def test_control_never_blocked_and_leaves_run_record_alone(self, prod_env, tmp_path, monkeypatch):
        assert self._run(prod_env, _Spawn(rc=0), monkeypatch) == 0
        stub = tmp_path / sh.SHADOW_DIRNAME / sh.STATUS_DIRNAME / "2026-10-19.json"
        before = stub.read_bytes()
        for _ in range(2):                                                   # 免费的 control 可重复
            assert sh.run_shadow(sh.parse_args(["--control", "--tickers", "2"]), spawn=_Spawn(rc=0),
                                 now=date(2026, 10, 19)) == 0
        assert stub.read_bytes() == before

    def test_stub_counts_as_unhealthy_day_for_the_report(self, prod_env, tmp_path, monkeypatch):
        import llm_shadow_report as rpt
        self._run(prod_env, _Spawn(rc=1), monkeypatch)                       # 子进程崩了，只剩占位
        days = rpt.load_days(tmp_path / sh.SHADOW_DIRNAME)
        assert len(days) == 1 and rpt.healthy_days(days) == [] and rpt.llm_rows(days) == []

    def test_child_command_carries_the_day(self, prod_env, monkeypatch):
        spawn = _Spawn(rc=0)
        self._run(prod_env, spawn, monkeypatch)
        cmd = spawn.calls[0]["cmd"]
        assert cmd[cmd.index("--child-day") + 1] == "2026-10-19"


# ==================== 命令行冒烟 ====================

class TestCliSmoke:
    def test_plan_via_subprocess_creates_nothing(self, tmp_path):
        prod = _make_prod(tmp_path)
        env = dict(os.environ, ALPHA_HIVE_HOME=str(prod))
        r = subprocess.run([sys.executable, str(_REPO / "llm_shadow_scan.py"), "--plan"],
                           env=env, capture_output=True, text=True, cwd=str(_REPO), timeout=60)
        assert r.returncode == 0, r.stderr[-800:]
        assert "标的 30/30" in r.stdout
        assert not (tmp_path / sh.SHADOW_DIRNAME).exists()

    def test_run_without_env_refused_via_subprocess(self, tmp_path):
        prod = _make_prod(tmp_path)
        env = {k: v for k, v in os.environ.items() if k != sh.SPEND_ENV}
        env["ALPHA_HIVE_HOME"] = str(prod)
        r = subprocess.run([sys.executable, str(_REPO / "llm_shadow_scan.py"), "--run"],
                           env=env, capture_output=True, text=True, cwd=str(_REPO), timeout=60)
        assert r.returncode == sh.EXIT_USAGE
        assert sh.SPEND_ENV in r.stdout
        assert not (tmp_path / sh.SHADOW_DIRNAME).exists()
