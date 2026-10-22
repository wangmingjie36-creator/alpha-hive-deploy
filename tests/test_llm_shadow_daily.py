"""
tests/test_llm_shadow_daily.py — v0.45.592：LLM 影子 A/B 每日自动入口

全部用假探针 / 假子进程 / 假时钟，**零 API 调用、不真睡**。要保住的不变式：
  · 生产没写完不开跑；等不到 / 太晚 / 关停开关 ⇒ 跳过、不花钱、不起子进程
  · 只有这个入口替用户设 ALPHA_HIVE_LLM_SHADOW_SPEND；plist 里没有它，别的代码也不设
  · plist：工作日 14:45、RunAtLoad=False（装上不会立刻花钱）、绝对路径、跑生产克隆里的脚本
"""
import ast
import os
import plistlib
import sqlite3
import sys
from pathlib import Path

import pytest

import llm_shadow_daily as dly
import llm_shadow_scan as sh

_REPO = Path(__file__).resolve().parent.parent


def _state(rows=30, age=600.0, running=False):
    return {"rows": rows, "file_age": age, "running": running}


# ==================== decide ====================

class TestDecide:
    def test_done_and_fresh_runs(self):
        assert dly.decide(_state(), 0, disabled=False)[0] == dly.ACTION_RUN

    @pytest.mark.parametrize("st", [_state(rows=0), _state(age=None), _state(running=True), _state(age=299.0)])
    def test_not_done_waits(self, st):
        assert dly.decide(st, 100, disabled=False)[0] == dly.ACTION_WAIT

    def test_stable_boundary_inclusive(self):
        assert dly.decide(_state(age=dly.STABLE_SEC), 0, disabled=False)[0] == dly.ACTION_RUN

    def test_disabled_beats_everything(self):
        assert dly.decide(_state(), 0, disabled=True)[0] == dly.ACTION_SKIP

    def test_timeout_skips_with_reason(self):
        act, why = dly.decide(_state(rows=0), dly.WAIT_MAX_SEC, disabled=False)
        assert act == dly.ACTION_SKIP and "没有 predictions" in why

    def test_just_before_timeout_still_waits(self):
        assert dly.decide(_state(rows=0), dly.WAIT_MAX_SEC - 1, disabled=False)[0] == dly.ACTION_WAIT

    def test_too_late_skips_even_though_done(self):
        act, why = dly.decide(_state(age=dly.MAX_LAG_SEC + 60), 0, disabled=False)
        assert act == dly.ACTION_SKIP and "不再与生产同口径" in why

    def test_lag_boundary_still_runs(self):
        assert dly.decide(_state(age=dly.MAX_LAG_SEC), 0, disabled=False)[0] == dly.ACTION_RUN


# ==================== plist ====================

class TestPlist:
    # 合成参数：render_plist 只把它们原样写进 plist、不碰文件系统 ⇒ /nonexistent/ 前缀即可
    # （家目录路径会被 test_reads_own_checkout 拦）；解释器用 sys.executable（CI 上没有 Mac 那份）
    PY = sys.executable
    SCRIPT = "/nonexistent/alpha-hive-prod/llm_shadow_daily.py"
    LOG_DIR = "/nonexistent/.claude/logs"

    @pytest.fixture
    def doc(self):
        return plistlib.loads(dly.render_plist(python=self.PY, script=self.SCRIPT, log_dir=self.LOG_DIR))

    def test_schedule_is_weekdays_1445(self, doc):
        got = doc["StartCalendarInterval"]
        assert sorted(g["Weekday"] for g in got) == [1, 2, 3, 4, 5]
        assert all(g["Hour"] == 14 and g["Minute"] == 45 for g in got)

    def test_run_at_load_is_false(self, doc):
        assert doc["RunAtLoad"] is False                       # 装上 / 登录不会立刻花钱

    def test_label_and_program(self, doc):
        assert doc["Label"] == dly.LABEL == "com.alpha.hive.llmshadow"
        assert doc["ProgramArguments"] == [self.PY, self.SCRIPT]
        assert all(os.path.isabs(a) for a in doc["ProgramArguments"])

    def test_does_not_carry_spend_or_data_root(self, doc):
        env = doc.get("EnvironmentVariables", {})
        assert sh.SPEND_ENV not in env and "ALPHA_HIVE_HOME" not in env
        assert env["PATH"].startswith("/usr/local/bin")        # 内部 spawn 的子 python 也走 3.11

    def test_logs_outside_repo(self, doc):
        assert doc["StandardOutPath"] == str(Path(self.LOG_DIR) / "llm_shadow_daily.out.log")
        assert doc["StandardErrorPath"] == str(Path(self.LOG_DIR) / "llm_shadow_daily.err.log")

    def test_print_plist_cli_targets_the_production_clone(self, capsys):
        assert dly.main(["--print-plist"]) == 0
        doc = plistlib.loads(capsys.readouterr().out.encode("utf-8"))
        assert doc["ProgramArguments"][1].endswith("/alpha-hive-prod/llm_shadow_daily.py")
        clone = Path(doc["ProgramArguments"][1]).parent
        for k in ("StandardOutPath", "StandardErrorPath"):              # 日志不落进代码克隆
            assert clone not in Path(doc[k]).parents


# ==================== production_state ====================

class TestProductionState:
    def _prod(self, tmp_path, rows):
        db = tmp_path / "pheromone.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, date TEXT, ticker TEXT)")
        con.executemany("INSERT INTO predictions(date,ticker) VALUES (?,?)", rows)
        con.commit()
        con.close()
        return db

    def test_counts_only_today_and_file_age(self, tmp_path):
        db = self._prod(tmp_path, [("2026-10-22", "A"), ("2026-10-22", "B"), ("2026-10-21", "A")])
        f = tmp_path / ".swarm_results_2026-10-22.json"
        f.write_text("{}")
        os.utime(f, (1000, 1000))
        st = dly.production_state(prod_db=db, home=tmp_path, today="2026-10-22", now=1600.0, is_running=lambda: False)
        assert st == {"rows": 2, "file_age": 600.0, "running": False}

    def test_missing_file_and_db(self, tmp_path):
        st = dly.production_state(prod_db=tmp_path / "nope.db", home=tmp_path, today="2026-10-22",
                                  is_running=lambda: True)
        assert st == {"rows": 0, "file_age": None, "running": True}

    def test_production_db_unchanged(self, tmp_path):
        db = self._prod(tmp_path, [("2026-10-22", "A")])
        before = (db.stat().st_mtime_ns, db.stat().st_size)
        dly.production_state(prod_db=db, home=tmp_path, today="2026-10-22", is_running=lambda: False)
        assert (db.stat().st_mtime_ns, db.stat().st_size) == before


# ==================== run_daily ====================

class _Spawn:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, cmd, env=None, cwd=None):
        self.calls.append({"cmd": cmd, "env": env, "cwd": cwd})
        return type("P", (), {"returncode": self.rc})()


class _Clock:
    """假时钟：每次 sleep 前进 sec。"""
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, sec):
        self.sleeps.append(sec)
        self.t += sec


@pytest.fixture
def prod_env(tmp_path, monkeypatch):
    prod = tmp_path / "alpha-hive-data"
    prod.mkdir()
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(prod))
    monkeypatch.delenv(sh.SPEND_ENV, raising=False)
    return prod


def _run(prod_env, probes, spawn, clock=None, disabled=False, once=False):
    clock = clock or _Clock()
    it = iter(probes)
    last = {}

    def probe():
        nonlocal last
        last = next(it, last)
        return last
    return dly.run_daily(once=once, environ=dict(os.environ), sleep=clock.sleep, clock=clock.now, probe=probe,
                         spawn=spawn, disabled_probe=lambda: disabled), clock


class TestRunDaily:
    def test_waits_until_production_done_then_runs_once(self, prod_env):
        spawn = _Spawn(rc=0)
        rc, clk = _run(prod_env, [_state(rows=0, age=None), _state(running=True, age=10), _state()], spawn)
        assert rc == 0 and len(spawn.calls) == 1
        assert clk.sleeps == [dly.POLL_SEC, dly.POLL_SEC]

    def test_child_gets_spend_env_and_runs_the_scan_entry(self, prod_env):
        spawn = _Spawn()
        _run(prod_env, [_state()], spawn)
        (call,) = spawn.calls
        assert call["env"][sh.SPEND_ENV] == "1"
        assert call["cmd"][-2].endswith("llm_shadow_scan.py") and call["cmd"][-1] == "--run"
        assert call["cmd"][0] == __import__("sys").executable
        assert call["cwd"] == str(_REPO)

    def test_parent_environment_is_not_polluted(self, prod_env):
        _run(prod_env, [_state()], _Spawn())
        assert sh.SPEND_ENV not in os.environ

    @pytest.mark.parametrize("rc", [0, 2, 3, 5])
    def test_child_exit_code_propagates(self, prod_env, rc):
        assert _run(prod_env, [_state()], _Spawn(rc=rc))[0] == rc

    def test_disabled_skips_without_spawn(self, prod_env):
        spawn = _Spawn()
        rc, clk = _run(prod_env, [_state()], spawn, disabled=True)
        assert rc == dly.EXIT_SKIPPED and spawn.calls == [] and clk.sleeps == []

    def test_timeout_skips_without_spawn(self, prod_env):
        spawn = _Spawn()
        rc, clk = _run(prod_env, [_state(rows=0, age=None)], spawn)
        assert rc == dly.EXIT_SKIPPED and spawn.calls == []
        assert sum(clk.sleeps) >= dly.WAIT_MAX_SEC - dly.POLL_SEC

    def test_too_late_skips_without_spawn(self, prod_env):
        spawn = _Spawn()
        rc, _ = _run(prod_env, [_state(age=dly.MAX_LAG_SEC + 1)], spawn)
        assert rc == dly.EXIT_SKIPPED and spawn.calls == []

    def test_once_never_spawns_or_sleeps(self, prod_env, capsys):
        spawn = _Spawn()
        rc, clk = _run(prod_env, [_state(rows=0, age=None)], spawn, once=True)
        assert rc == 0 and spawn.calls == [] and clk.sleeps == []
        assert "wait" in capsys.readouterr().out

    def test_once_reports_skip_exit_code(self, prod_env):
        rc, _ = _run(prod_env, [_state()], _Spawn(), once=True, disabled=True)
        assert rc == dly.EXIT_SKIPPED

    def test_kill_switch_path_is_inside_the_shadow_root(self, prod_env):
        """DISABLED 文件在影子根里：用户 touch 一下就停，不用动 launchd。"""
        assert (sh.default_shadow_root(prod_env) / dly.DISABLE_FILENAME).parent.name == sh.SHADOW_DIRNAME


# ==================== 花钱闸的归属 ====================

class TestSpendGateOwnership:
    def test_spend_env_name_matches_scan(self):
        assert dly.SPEND_ENV == sh.SPEND_ENV

    def test_only_the_daily_entry_sets_the_spend_variable(self):
        """全仓非测试代码里，往环境字典写 SPEND_ENV 的只许 llm_shadow_daily.py（别的入口手滑就能花钱）。"""
        offenders = []
        for p in sorted(_REPO.glob("*.py")):
            if p.name == "llm_shadow_daily.py":
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Subscript):
                            key = tgt.slice
                            if (isinstance(key, ast.Name) and key.id == "SPEND_ENV") or \
                               (isinstance(key, ast.Constant) and key.value == "ALPHA_HIVE_LLM_SHADOW_SPEND"):
                                offenders.append(f"{p.name}:{node.lineno}")
        assert offenders == []

    def test_orchestrator_never_mentions_it(self):
        for p in (_REPO / "scripts").glob("*.sh"):
            assert "ALPHA_HIVE_LLM_SHADOW_SPEND" not in p.read_text(encoding="utf-8"), p.name

    def test_the_guard_has_teeth(self, tmp_path):
        """正对照：同样的 AST 判据能抓到违规写法。"""
        tree = ast.parse('import os\nenv = {}\nenv["ALPHA_HIVE_LLM_SHADOW_SPEND"] = "1"\n')
        hit = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Subscript)
               and isinstance(n.targets[0].slice, ast.Constant)
               and n.targets[0].slice.value == "ALPHA_HIVE_LLM_SHADOW_SPEND"]
        assert len(hit) == 1
