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

    def test_carries_the_orchestrator_proxy(self, doc):
        """代理变量写进生成器（v0.45.623）：已装的 plist 10-22 经用户批准手改过，生成器不写，重新生成就把它们无声丢掉。
        名字与值都与编排器探活通过后导出的那组**逐项**一致（v0.45.627，623 二次检查 #7：此前只核了地址、只写大写那套——
        curl_cffi 对 http:// 只读小写 `http_proxy`；`NO_PROXY` 是又一份抄写、只核了子集）。编排器是唯一真相，经
        tests/_orchestrator 读原文。"""
        import re
        from tests._orchestrator import repo_orchestrator_text
        text = repo_orchestrator_text()
        m = re.search(r'^PROXY_URL="\$\{ALPHA_HIVE_PROXY:-([^}"]+)\}"', text, re.M)
        assert m, "编排器 PROXY_URL 的写法变了：重看这条守卫"
        block = re.search(r'^if \[ "\$_proxy_ok" = "1" \]; then\n(.*?)^else\n', text, re.M | re.S)
        assert block, "编排器探活通过那一支的写法变了：重看这条守卫"
        exported = dict(kv for line in block.group(1).splitlines() if line.strip().startswith("export ")
                        for kv in re.findall(r'(\w+)="([^"]*)"', line))
        refs = {"$PROXY_URL": m.group(1), "$NO_PROXY": exported.get("NO_PROXY")}
        want = {k: refs.get(v, v) for k, v in exported.items()}
        assert {"HTTP_PROXY", "https_proxy", "NO_PROXY", "no_proxy"} <= set(want), want     # 正对照：真解析到了那组导出
        env = dict(doc["EnvironmentVariables"])
        assert env.pop("PATH").startswith("/usr/local/bin")
        assert env == want == dly.PROXY_ENV, (env, want)
        assert {"localhost", "127.0.0.1", "::1"} <= set(env["NO_PROXY"].split(","))     # 回环请求不送去代理

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


def _net_up():
    return True, "通 3/3"


def _run(prod_env, probes, spawn, clock=None, disabled=False, once=False, net_probe=None):
    clock = clock or _Clock()
    it = iter(probes)
    last = {}

    def probe():
        nonlocal last
        last = dict(next(it, last))
        return last
    # 一律注入探活：测试里绝不真的打外网
    return dly.run_daily(once=once, environ=dict(os.environ), sleep=clock.sleep, clock=clock.now, probe=probe,
                         spawn=spawn, disabled_probe=lambda: disabled, net_probe=net_probe or _net_up), clock


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


# ==================== 开跑前探活网络（v0.45.633） ====================

class TestDecideWithNetwork:
    def test_done_but_network_down_waits(self):
        act, why = dly.decide({**_state(), "net_ok": False, "net_detail": "通 0/3"}, 100, disabled=False)
        assert act == dly.ACTION_WAIT and "探活不通" in why and "通 0/3" in why

    def test_done_and_network_up_runs(self):
        assert dly.decide({**_state(), "net_ok": True}, 0, disabled=False)[0] == dly.ACTION_RUN

    def test_network_not_probed_yet_is_not_a_block(self):
        """net_ok 缺省（生产还没写完时不探）——不能把「没探」当「不通」。"""
        assert dly.decide(_state(), 0, disabled=False)[0] == dly.ACTION_RUN

    def test_network_down_until_timeout_skips_and_says_network(self):
        act, why = dly.decide({**_state(), "net_ok": False, "net_detail": "通 0/3"}, dly.WAIT_MAX_SEC, disabled=False)
        assert act == dly.ACTION_SKIP and "网络探活一直不通" in why

    def test_too_late_still_skips_before_network_is_considered(self):
        act, why = dly.decide({**_state(age=dly.MAX_LAG_SEC + 60), "net_ok": False}, 0, disabled=False)
        assert act == dly.ACTION_SKIP and "不再与生产同口径" in why

    def test_production_done_helper_matches_decide(self):
        assert dly.production_done(_state()) is True
        for st in (_state(rows=0), _state(age=None), _state(running=True), _state(age=299.0)):
            assert dly.production_done(st) is False


class TestProbeNetwork:
    def _opener(self, behaviour):
        def op(url, timeout):
            r = behaviour[url.split("//")[1].split("/")[0]]
            if isinstance(r, Exception):
                raise r
            return r
        return op

    URLS = ("https://a.example/", "https://b.example/", "https://c.example/")

    def test_all_up(self):
        ok, det = dly.probe_network(self.URLS, opener=self._opener({"a.example": 1, "b.example": 1, "c.example": 1}))
        assert ok is True and "通 3/3" in det

    def test_http_error_counts_as_reachable(self):
        """Yahoo 常给 429：有响应说明握手通了。"""
        import urllib.error
        e = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        ok, _ = dly.probe_network(self.URLS, opener=self._opener({"a.example": e, "b.example": e, "c.example": e}))
        assert ok is True

    def test_tls_eof_on_all_hosts_is_down(self):
        """10-23 的形状：所有数据源同时 `EOF occurred in violation of protocol`。"""
        import ssl
        e = ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:992)")
        ok, det = dly.probe_network(self.URLS, opener=self._opener({"a.example": e, "b.example": e, "c.example": e}))
        assert ok is False and "SSLEOFError" in det and "通 0/3" in det

    def test_two_of_three_is_enough_one_is_not(self):
        e = OSError("timed out")
        assert dly.probe_network(self.URLS, opener=self._opener({"a.example": 1, "b.example": 1, "c.example": e}))[0] is True
        assert dly.probe_network(self.URLS, opener=self._opener({"a.example": 1, "b.example": e, "c.example": e}))[0] is False

    def test_timeout_and_refused_count_as_down(self):
        import socket
        ok, _ = dly.probe_network(self.URLS, opener=self._opener(
            {"a.example": socket.timeout("t"), "b.example": ConnectionRefusedError("r"), "c.example": 1}))
        assert ok is False

    def test_default_urls_are_the_three_scan_dependencies(self):
        hosts = {u.split("//")[1].split("/")[0] for u in dly.NET_PROBE_URLS}
        assert hosts == {"cdn.cboe.com", "query1.finance.yahoo.com", "finnhub.io"} and dly.NET_PROBE_MIN_OK == 2


class TestRunDailyWithNetwork:
    def test_waits_for_network_then_runs_once(self, prod_env):
        answers = iter([(False, "通 0/3；不通 x"), (False, "通 0/3；不通 x"), (True, "通 3/3")])
        spawn = _Spawn(rc=0)
        rc, clk = _run(prod_env, [_state()], spawn, net_probe=lambda: next(answers))
        assert rc == 0 and len(spawn.calls) == 1 and clk.sleeps == [dly.POLL_SEC, dly.POLL_SEC]

    def test_network_never_up_skips_without_spawn_and_without_spend(self, prod_env):
        spawn = _Spawn()
        rc, clk = _run(prod_env, [_state()], spawn, net_probe=lambda: (False, "通 0/3"))
        assert rc == dly.EXIT_SKIPPED and spawn.calls == []
        assert sum(clk.sleeps) >= dly.WAIT_MAX_SEC - dly.POLL_SEC

    def test_network_not_probed_while_production_is_still_running(self, prod_env):
        calls = []

        def net():
            calls.append(1)
            return True, "通 3/3"
        _run(prod_env, [_state(running=True), _state(rows=0, age=None), _state()], _Spawn(), net_probe=net)
        assert calls == [1]                                  # 只有最后一轮（生产写完）才探

    def test_every_network_wait_round_is_logged(self, prod_env, capsys):
        answers = iter([(False, "通 0/3；不通 a: SSLEOFError"), (True, "通 3/3")])
        _run(prod_env, [_state()], _Spawn(), net_probe=lambda: next(answers))
        out = capsys.readouterr().out
        assert "探活不通" in out and "SSLEOFError" in out

    def test_once_reports_network_state_without_spawning(self, prod_env, capsys):
        rc, clk = _run(prod_env, [_state()], _Spawn(), once=True, net_probe=lambda: (False, "通 0/3"))
        assert rc == 0 and clk.sleeps == [] and "wait" in capsys.readouterr().out


# ==================== 二次检查补的三处（v0.45.641） ====================

class TestProbeErrorsDoNotKillTheLauncher:
    """读生产状态偶发失败（库正被备份、文件系统忙）不该让启动脚本崩掉、丢一天——和 10-23 同形状。"""

    def _run_with_probe(self, prod_env, probe_fn, spawn=None, clock=None):
        clock = clock or _Clock()
        return dly.run_daily(once=False, environ=dict(os.environ), sleep=clock.sleep, clock=clock.now, probe=probe_fn,
                             spawn=spawn or _Spawn(), disabled_probe=lambda: False, net_probe=_net_up), clock

    def test_transient_probe_error_is_retried_then_runs(self, prod_env, capsys):
        answers = iter([RuntimeError("database is locked"), OSError("busy"), _state()])

        def probe():
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        spawn = _Spawn(rc=0)
        rc, clk = self._run_with_probe(prod_env, probe, spawn)
        assert rc == 0 and len(spawn.calls) == 1 and clk.sleeps == [dly.POLL_SEC, dly.POLL_SEC]
        out = capsys.readouterr().out
        assert "读生产状态失败" in out and "database is locked" in out        # 每次失败都留痕

    def test_probe_that_never_recovers_skips_and_names_the_error(self, prod_env, capsys):
        def probe():
            raise RuntimeError("unable to open database file")
        spawn = _Spawn()
        rc, _ = self._run_with_probe(prod_env, probe, spawn)
        assert rc == dly.EXIT_SKIPPED and spawn.calls == []
        assert "读生产状态一直失败" in capsys.readouterr().out

    def test_skip_reason_mentions_probe_error(self):
        st = {"rows": 0, "file_age": None, "running": False, "probe_error": "RuntimeError: boom"}
        act, why = dly.decide(st, dly.WAIT_MAX_SEC, disabled=False)
        assert act == dly.ACTION_SKIP and "读生产状态一直失败" in why and "boom" in why


class TestChildDeadline:
    """启动脚本必须有子进程超时：launchd 在上一个实例未退出时不会再起新实例，挂死 = 此后每天被无声跳过。"""

    def test_normal_child_returns_its_exit_code(self):
        import sys
        d = dly.run_child_with_deadline([sys.executable, "-c", "import sys; sys.exit(5)"], env=dict(os.environ),
                                        cwd=str(_REPO), timeout=30)
        assert d.returncode == 5 and d.timed_out is False

    def test_hung_child_is_killed_with_its_grandchild(self, tmp_path):
        """真进程：子进程再起一个孙进程（像 llm_shadow_scan --run 起 --child），超时后两个都必须死——不然孙进程继续花钱。"""
        import sys
        import time as _t
        pidfile = tmp_path / "grandchild.pid"
        code = (
            "import subprocess, sys, time, pathlib\n"
            f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(g.pid))\n"
            "time.sleep(600)\n")
        t0 = _t.time()
        d = dly.run_child_with_deadline([sys.executable, "-c", code], env=dict(os.environ), cwd=str(_REPO),
                                        timeout=2.0, grace=5.0)
        assert d.timed_out is True and d.returncode == dly.EXIT_CHILD_TIMEOUT and _t.time() - t0 < 30
        gpid = int(pidfile.read_text())
        for _ in range(50):                                      # 给内核一点时间回收
            try:
                os.kill(gpid, 0)
            except ProcessLookupError:
                break
            _t.sleep(0.1)
        else:
            os.kill(gpid, 9)                                     # 清理，别留孤儿
            raise AssertionError("孙进程在超时后仍活着——进程组没杀干净")

    def test_stopping_the_launcher_takes_the_scan_children_with_it(self, tmp_path):
        """`launchctl bootout` 给启动脚本发 SIGTERM；子进程在新会话里不会跟着死——必须由启动脚本转发，否则留下继续花钱的孤儿。"""
        import signal as _sig
        import subprocess as _sp
        import sys
        import time as _t
        pidfile, ready = tmp_path / "child.pid", tmp_path / "ready"
        child_code = (f"import os, pathlib, time; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
                      f"pathlib.Path({str(ready)!r}).write_text('1'); time.sleep(600)")
        launcher = (f"import sys; sys.path.insert(0, {str(_REPO)!r}); import os, llm_shadow_daily as d\n"
                    f"d.run_child_with_deadline([sys.executable, '-c', {child_code!r}], env=dict(os.environ), cwd={str(_REPO)!r}, timeout=600)\n")
        lp = _sp.Popen([sys.executable, "-c", launcher])
        try:
            for _ in range(100):
                if ready.exists():
                    break
                _t.sleep(0.1)
            assert ready.exists(), "子进程没起来"
            cpid = int(pidfile.read_text())
            lp.send_signal(_sig.SIGTERM)
            assert lp.wait(timeout=20) == 128 + _sig.SIGTERM
            for _ in range(50):
                try:
                    os.kill(cpid, 0)
                except ProcessLookupError:
                    break
                _t.sleep(0.1)
            else:
                os.kill(cpid, 9)
                raise AssertionError("启动脚本被 SIGTERM 后，扫描子进程仍活着")
        finally:
            if lp.poll() is None:
                lp.kill()

    def test_run_daily_uses_the_deadline_runner_by_default_and_reports_timeout(self, prod_env, monkeypatch, capsys):
        seen = {}

        def fake_runner(cmd, *, env, cwd, **kw):
            seen["cmd"], seen["env"] = cmd, env
            return dly._Done(dly.EXIT_CHILD_TIMEOUT, timed_out=True)
        monkeypatch.setattr(dly, "run_child_with_deadline", fake_runner)
        clock = _Clock()
        rc = dly.run_daily(once=False, environ=dict(os.environ), sleep=clock.sleep, clock=clock.now,
                           probe=lambda: _state(), spawn=None, disabled_probe=lambda: False, net_probe=_net_up)
        assert rc == dly.EXIT_CHILD_TIMEOUT
        assert seen["env"][sh.SPEND_ENV] == "1" and seen["cmd"][-1] == "--run"
        out = capsys.readouterr().out
        assert "已杀掉整个进程组" in out and "下一个交易日照常" in out

    def test_deadline_constant_is_comfortably_above_a_normal_run(self):
        assert dly.CHILD_TIMEOUT_SEC >= 3600 * 1.5          # 实测一次约 25 分钟（10-22）；10-23 的网络全断预取也只有 18 分钟


class TestProbeClosesItsConnections:
    def test_default_opener_uses_a_context_manager(self, monkeypatch):
        """握手探测不读响应体，用 with 立即关连接；每分钟 3 个 × 3 小时不能靠 GC 回收 sockets。"""
        import urllib.request
        events = []

        class Resp:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, *a):
                events.append("exit")

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: Resp())
        ok, _ = dly.probe_network(("https://a.example/", "https://b.example/"), min_ok=2)
        assert ok is True and events == ["enter", "exit", "enter", "exit"]


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
