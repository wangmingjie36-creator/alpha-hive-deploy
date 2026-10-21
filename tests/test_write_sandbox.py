"""测试会话的内核写沙箱（v0.45.573）——它真的生效、判据真的有牙。设计与理由见 `tests/_write_sandbox.py`。

三层：
1. 纯逻辑（任何平台都跑）：规则文本、统一日志解析的边界、查询失败不能当「没有拒绝」。
2. 闸的判定（`conftest._guard_by_write_sandbox`，任何平台都跑）：有拒绝 ⇒ 红并定位；查不了 ⇒ 红；只有外部变化 ⇒ 不红只说明；
   继承外层沙箱的内层会话不查日志。
3. 本机实测（macOS + 本机有真实数据根时）：沙箱**必须**生效（不许静默退回指纹闸）；子进程写金丝雀被内核拒；
   查日志助手真看得见本会话的拒绝（否则会话末尾的「没有拒绝」是瞎的）。
   ⚠️ 已在沙箱里的进程不能再套一层 ⇒「写真实数据根会被拒」只能靠同一份规则里的金丝雀证明——永远不往生产目录真写。
   端到端（假数据根：吞掉的写被报红并定位到测试、沙箱外写入只说明）在新进程里手工跑过，记录在 CHANGELOG v0.45.573。
"""
from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

import _write_sandbox as WS

ROOT = "/nonexistent/alpha-hive-data"
CANARY = "/private/var/folders/x/alpha_hive_write_sandbox_canary_ab12"

LOG_TEXT = "\n".join([
    "Timestamp               Ty Process[PID:TID]",
    "2026-10-21 02:46:27.864 E  kernel[0:1] (Sandbox) Sandbox: Python(8083) deny(1) file-write-create "
    f"{ROOT}/paper_portfolio_state/meta.json",
    "2026-10-21 02:46:27.870 E  kernel[0:2] (Sandbox) Sandbox: bash(8332) deny(1) file-write-data "
    f"{ROOT}/cache/x y.json",
    "2026-10-21 02:46:28.000 E  kernel[0:3] (Sandbox) Sandbox: mdworker(77) deny(1) file-read-data "
    f"{ROOT}/pheromone.db",                                    # 读拒绝：不是本闸的事
    "2026-10-21 02:46:28.100 E  kernel[0:4] (Sandbox) Sandbox: Python(8083) deny(1) file-write-create "
    f"{ROOT}-other/x",                                         # 只是前缀相同的兄弟目录
    "2026-10-21 02:46:28.200 E  kernel[0:5] (Sandbox) Sandbox: Python(8083) deny(1) file-write-create "
    f"{CANARY}/probe-8083",                                    # 金丝雀：预期内的拒绝
    "2026-10-21 02:46:28.300 E  kernel[0:6] (Sandbox) Sandbox: Python(8083) deny(1) file-write-unlink "
    f"{ROOT}",                                                 # 根本身
    "some unrelated line",
])


class TestPureLogic:

    def test_profile_denies_writes_under_each_path(self):
        p = WS.profile([ROOT, CANARY, '/tmp/we"ird\\path'])
        assert p.startswith("(version 1)(allow default)")
        assert f'(deny file-write* (subpath "{ROOT}"))' in p and f'(deny file-write* (subpath "{CANARY}"))' in p
        assert '(subpath "/tmp/we\\"ird\\\\path")' in p, "路径里的引号 / 反斜杠要转义，否则规则串坏掉、sandbox_init 拒绝"

    def test_parse_keeps_only_writes_under_root_minus_canary(self):
        got = WS.parse_denials(LOG_TEXT, ROOT, CANARY)
        assert [(d["proc"], d["pid"], d["op"], d["path"]) for d in got] == [
            ("Python", 8083, "file-write-create", f"{ROOT}/paper_portfolio_state/meta.json"),
            ("bash", 8332, "file-write-data", f"{ROOT}/cache/x y.json"),
            ("Python", 8083, "file-write-unlink", ROOT),
        ]
        assert got[0]["ts"] == "2026-10-21 02:46:27.864" and WS.ts_epoch(got[0]["ts"]) is not None

    def test_query_failure_is_an_error_not_an_empty_list(self):
        st = {"started_local": __import__("datetime").datetime(2026, 10, 21, 2, 0), "root": ROOT, "canary": CANARY}
        found, err = WS.denials(st, ask=lambda c: {"rc": 64, "stdout": "", "stderr": "log: Cannot run while sandboxed"})
        assert found == [] and "Cannot run while sandboxed" in err
        found, err = WS.denials({**st, "helper": None})
        assert found == [] and "没有查日志助手" in err
        found, err = WS.denials(st, ask=lambda c: {"rc": 0, "stdout": LOG_TEXT, "stderr": ""})
        assert err is None and len(found) == 3

    def test_denials_before_session_start_are_dropped(self):
        """`--start` 只到秒：同一秒里、本会话开始之前的拒绝（上一个会话的）不能算到本会话头上（v0.45.577）。"""
        import datetime as _dt
        t0 = _dt.datetime(2026, 10, 21, 2, 46, 27, 917000).timestamp()      # 减去 LOG_TS_LAG 后落在 .864 与 .870 之间
        st = {"started_local": _dt.datetime(2026, 10, 21, 2, 46, 27), "started_epoch": t0, "root": ROOT, "canary": CANARY}
        found, err = WS.denials(st, ask=lambda c: {"rc": 0, "stdout": LOG_TEXT, "stderr": ""})
        assert err is None and [d["path"] for d in found] == [f"{ROOT}/cache/x y.json", ROOT]

    def test_log_command_uses_full_path_and_session_start(self):
        st = {"started_local": __import__("datetime").datetime(2026, 10, 21, 2, 10, 52)}
        cmd = WS.log_command(st)
        assert cmd[0] == "/usr/bin/log", "zsh 里 `log` 是内建命令——一律全路径"
        assert cmd[1:4] == ["show", "--start", "2026-10-21 02:10:52"]


class TestGuardDecision:

    @staticmethod
    def _cfg():
        return types.SimpleNamespace(stash={})       # 判定报红时经 `_gate_timeline(config)` 读 stash 定位

    def test_denials_make_it_red_and_locate(self, write_sandbox_guard_fn, monkeypatch):
        found = WS.parse_denials(LOG_TEXT, ROOT, CANARY)
        monkeypatch.setattr(WS, "denials", lambda st: (found, None))
        monkeypatch.setattr(WS, "close", lambda st: None)
        with pytest.raises(AssertionError) as e:
            write_sandbox_guard_fn(self._cfg(), {"check_log": True, "root": ROOT}, ROOT, {})
        msg = str(e.value)
        assert "拒绝了 3 次" in msg and "paper_portfolio_state/meta.json" in msg and "bash(8332)" in msg

    def test_query_failure_makes_it_red(self, write_sandbox_guard_fn, monkeypatch):
        monkeypatch.setattr(WS, "denials", lambda st: ([], "log show 失败"))
        monkeypatch.setattr(WS, "close", lambda st: None)
        with pytest.raises(AssertionError, match="核对失败不能当没有"):
            write_sandbox_guard_fn(self._cfg(), {"check_log": True, "root": ROOT}, ROOT, {})

    def test_outside_changes_only_noted(self, write_sandbox_guard_fn, monkeypatch):
        monkeypatch.setattr(WS, "denials", lambda st: ([], None))
        monkeypatch.setattr(WS, "close", lambda st: None)
        cfg = self._cfg()
        changes = {"modified": ["alphabot_state/intraday/2026-10-21/NVDA.jsonl"]}
        write_sandbox_guard_fn(cfg, {"check_log": True, "root": ROOT}, ROOT, changes)   # 不抛
        assert cfg._alpha_hive_external_writes == changes

    def test_inherited_session_does_not_query(self, write_sandbox_guard_fn, monkeypatch):
        def boom(st):
            raise AssertionError("继承外层沙箱的内层会话不该查日志")
        monkeypatch.setattr(WS, "denials", boom)
        cfg = self._cfg()
        write_sandbox_guard_fn(cfg, {"check_log": False, "root": ROOT}, ROOT, {"added": ["x"]})
        assert cfg._alpha_hive_external_writes == {"added": ["x"]}


def _live(pytestconfig) -> dict:
    """本机实测的前提：macOS、本机有真实数据根、没被显式关掉。条件不满足就说清楚为什么不测（CI 是 Linux、没有真实数据根）。"""
    st = getattr(pytestconfig, "_alpha_hive_write_sandbox", None) or {}
    if sys.platform != "darwin":
        pytest.skip(f"只在 macOS 上有 sandbox_init（本机 {sys.platform}）")
    if st.get("reason") == "本机没有真实数据根":
        pytest.skip("本机没有真实数据根（CI / 干净克隆）：沙箱无处可套，真实数据根闸本身也不生效")
    if os.environ.get(WS.OPT_OUT_ENV, "").strip() == "0":
        pytest.skip(f"{WS.OPT_OUT_ENV}=0 显式关闭（报告头已说明）")
    return st


class TestLiveOnThisMac:

    def test_sandbox_is_active_where_it_should_be(self, pytestconfig):
        """macOS + 有真实数据根 ⇒ 必须生效。套不上 / 金丝雀没被拒都要红，不许静默退回会被 Alpha Bot 误报的指纹闸。"""
        st = _live(pytestconfig)
        assert st.get("active"), f"写沙箱没生效：{st.get('reason')}"
        assert st["root"] == os.path.realpath(st["root"]) and os.path.isdir(st["root"])

    def test_children_inherit_and_are_denied(self, pytestconfig, tmp_path):
        """子进程（Python / sh / env -i）写同一份规则里的金丝雀 ⇒ 都被内核拒。同一机制护着真实数据根，但从不往生产真写。"""
        st = _live(pytestconfig)
        if not st.get("active"):
            pytest.fail(f"写沙箱没生效：{st.get('reason')}")
        target = os.path.join(st["canary"], f"child-{os.getpid()}")
        py = subprocess.run([sys.executable, "-c", f"open({target!r}, 'w').write('x')"], capture_output=True, text=True)
        assert py.returncode != 0 and "Operation not permitted" in py.stderr
        for cmd in (["/bin/sh", "-c", f"echo x > '{target}.sh'"], ["env", "-i", f"ALPHA_HIVE_HOME={tmp_path}", "/bin/sh", "-c", f"echo x > '{target}.envi'"]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            assert r.returncode != 0 and "Operation not permitted" in r.stderr, (cmd, r.stderr)
        assert not [n for n in os.listdir(st["canary"]) if n.startswith("child-")]

    def test_log_helper_sees_this_sessions_denials(self, pytestconfig):
        """会话末尾的「没有拒绝」要可信，得先证明助手看得见拒绝：金丝雀上的拒绝（会话开始那次 + 本条的）必须查得到。"""
        st = _live(pytestconfig)
        if not st.get("active"):
            pytest.fail(f"写沙箱没生效：{st.get('reason')}")
        if not st.get("check_log"):
            pytest.skip("本会话继承了外层沙箱（进程内再跑的 pytest），日志由外层核对")
        target = os.path.join(st["canary"], f"seen-{os.getpid()}")
        try:
            open(target, "w").write("x")
        except PermissionError:
            pass
        import time
        found = []
        for _ in range(10):        # 统一日志落盘有一两秒延迟
            r = WS._ask_helper(st["helper"], WS.log_command(st))
            assert r.get("rc") == 0, r.get("stderr")
            found = WS.parse_denials(r["stdout"], st["canary"])
            if any(d["path"] == target for d in found):
                break
            time.sleep(0.5)
        assert any(d["path"] == target for d in found), f"助手查不到本会话刚发生的拒绝：{found[-3:]}"


# ---------------- v0.45.577 二次检查 ----------------

class _FakeLib:
    """代替 libsandbox：记下 sandbox_init 有没有被调、返回给定值。测试进程已在沙箱里，真调会被拒（也不该真套）。"""

    def __init__(self, rc):
        self.calls = []
        lib = self

        def sandbox_init(profile, flags, err):
            lib.calls.append(profile)
            return rc
        self.sandbox_init = sandbox_init


def _registry(tmp_path, root=ROOT):
    """一份临时登记目录 + 本会话状态（不碰真 REGISTRY_DIR）。"""
    reg = tmp_path / "registry"
    st = {"root": root, "started_epoch": 1000.0, "registry": str(reg), "session_id": "me",
          "pids_file": str(reg / "me.pids")}
    reg.mkdir(parents=True)
    (reg / "me.json").write_text('{"pid": 1, "root": "%s", "started": 1000.0, "ended": null}' % root)
    return reg, st


def _other(reg, sid, pid, *, root=ROOT, started=900.0, ended=None, checked=False, pids=()):
    import json
    (reg / f"{sid}.json").write_text(json.dumps(
        {"pid": pid, "root": root, "started": started, "ended": ended, "checked": checked}))
    (reg / f"{sid}.pids").write_text("".join(f"{p}\n" for p in pids))


def _den(pid, path=f"{ROOT}/x.json"):
    return {"proc": "Python", "pid": pid, "op": "file-write-create", "path": path, "ts": "2026-10-21 10:00:00.000"}


class TestConcurrentSessionAttribution:
    """B1：两个会话护同一个根，X 的写曾让无辜的 Y 也红（实测复现）。现在按登记的进程号分归属。"""

    DEAD = 2 ** 22 + 12345           # 远超 pid_max：kill(pid, 0) 必是 ProcessLookupError

    def test_other_live_sessions_pid_is_theirs(self, tmp_path):
        reg, st = _registry(tmp_path)
        _other(reg, "x", os.getpid(), pids=[4242])          # 「活着」的另一个会话（借本进程号证明 alive）
        mine, foreign = WS.attribute(st, [_den(4242)], now=2000.0)
        assert not mine and [d["pid"] for d in foreign] == [4242] and "它自己会报" in foreign[0]["who"]

    def test_own_pid_is_mine_even_if_other_also_lists_it(self, tmp_path):
        reg, st = _registry(tmp_path)
        (reg / "me.pids").write_text("4242\n")
        _other(reg, "x", os.getpid(), pids=[4242])          # 进程号复用：两边都登记过 ⇒ 保守算自己的
        mine, foreign = WS.attribute(st, [_den(4242)], now=2000.0)
        assert [d["who"] for d in mine] == ["本会话"] and not foreign

    def test_dead_unchecked_other_is_reported_here(self, tmp_path):
        reg, st = _registry(tmp_path)
        _other(reg, "x", self.DEAD, ended=None, checked=False, pids=[4242])
        mine, foreign = WS.attribute(st, [_den(4242)], now=2000.0)
        assert not foreign and "代报" in mine[0]["who"], "对方死了没核对 ⇒ 总得有人红"

    def test_finished_and_checked_other_is_theirs(self, tmp_path):
        reg, st = _registry(tmp_path)
        _other(reg, "x", self.DEAD, ended=1500.0, checked=True, pids=[4242])
        mine, foreign = WS.attribute(st, [_den(4242)], now=2000.0)
        assert not mine and len(foreign) == 1

    def test_unknown_pid_stays_red_and_says_ambiguous(self, tmp_path):
        reg, st = _registry(tmp_path)
        _other(reg, "x", os.getpid(), pids=[4242])
        mine, foreign = WS.attribute(st, [_den(5151)], now=2000.0)
        assert not foreign and "可能是它的" in mine[0]["who"] and str(os.getpid()) in mine[0]["who"]
        reg2, st2 = _registry(tmp_path / "alone")
        mine, _ = WS.attribute(st2, [_den(5151)], now=2000.0)
        assert "可能是它的" not in mine[0]["who"], "没有并发会话时不该暗示「可能是别人的」"

    def test_non_overlapping_or_other_root_sessions_are_ignored(self, tmp_path):
        reg, st = _registry(tmp_path)
        _other(reg, "old", self.DEAD, ended=999.0, checked=True, pids=[4242])          # 在本会话开始前就结束了
        _other(reg, "elsewhere", os.getpid(), root="/nonexistent/other-root", pids=[4343])
        assert WS.sessions(st, now=2000.0) == []
        mine, foreign = WS.attribute(st, [_den(4242), _den(4343)], now=2000.0)
        assert len(mine) == 2 and not foreign

    def test_no_registry_means_everything_is_mine(self):
        st = {"root": ROOT, "started_epoch": 1000.0, "registry": None, "registry_error": "会话登记不可用（测试）"}
        mine, foreign = WS.attribute(st, [_den(4242)], now=2000.0)
        assert not foreign and mine[0]["who"] == "会话登记不可用（测试）"

    def test_guard_notes_foreign_and_stays_green(self, write_sandbox_guard_fn, monkeypatch, tmp_path):
        reg, st = _registry(tmp_path)
        _other(reg, "x", os.getpid(), pids=[4242])
        monkeypatch.setattr(WS, "denials", lambda s: ([_den(4242)], None))
        monkeypatch.setattr(WS, "close", lambda s: None)
        cfg = types.SimpleNamespace(stash={})
        write_sandbox_guard_fn(cfg, {**st, "check_log": True}, ROOT, {})          # 不抛
        assert [d["pid"] for d in cfg._alpha_hive_foreign_denials] == [4242]
        import json
        assert json.loads((reg / "me.json").read_text())["checked"] is True, "核对完要登记，别的会话才敢把它的拒绝留给它"

    def test_pid_hook_records_children(self, tmp_path, monkeypatch):
        """Popen 钩子真的把子进程号写进登记（本会话已装好钩子；临时把落点换到 tmp）。"""
        if not WS._PID_HOOK["installed"]:
            pytest.skip("本会话没装进程号钩子（写沙箱未生效 / 登记不可用，报告头已说明）")
        f = tmp_path / "pids"
        monkeypatch.setitem(WS._PID_HOOK, "file", str(f))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        assert str(p.pid) in f.read_text().split()


class TestStaleMarker:
    """B3：环境里残留 / 外来的 ACTIVE 标记曾让 apply() 直接信它、根本不调 sandbox_init ⇒ 沙箱静默失效。"""

    def test_failed_inherited_canary_falls_through_to_apply(self, tmp_path, monkeypatch):
        root = os.path.realpath(tmp_path / "root")
        os.makedirs(root)
        not_protected = tmp_path / "old_canary"
        not_protected.mkdir()                                   # 写得进 ⇒ 标记是假的
        monkeypatch.setenv(WS.ACTIVE_ENV, f"{root}\n{not_protected}\n")
        monkeypatch.delenv(WS.OPT_OUT_ENV, raising=False)
        fake = _FakeLib(rc=-1)
        monkeypatch.setattr(WS.ctypes, "CDLL", lambda path: fake)
        st = WS.apply(root)
        assert fake.calls, "标记验不过就得自己套（旧代码在这里直接返回，从不调 sandbox_init）"
        assert st["stale_marker"] and not st["inherited"] and not st["active"]
        assert WS.ACTIVE_ENV not in os.environ
        assert st["canary"] and not os.path.exists(st["canary"]), "B4：套失败时 close() 也要删掉自己的金丝雀"
        assert os.listdir(not_protected) == [], "验标记时写进去的探针要删掉，否则那个目录永远不空、清不掉"


class TestCanaryCleanup:
    """B4：金丝雀目录每会话漏一个（10-21 实测 186 个）。"""

    def test_close_removes_own_canary_without_helper(self, tmp_path):
        c = tmp_path / f"{WS.CANARY_PREFIX}x"
        c.mkdir()
        WS.close({"canary": str(c), "owns_canary": True, "helper": None})
        assert not c.exists()

    def test_close_never_removes_what_it_does_not_own(self, tmp_path):
        inherited = tmp_path / f"{WS.CANARY_PREFIX}outer"
        inherited.mkdir()
        WS.close({"canary": str(inherited), "owns_canary": False, "helper": None})
        odd = tmp_path / "not_a_canary"
        odd.mkdir()
        WS.close({"canary": str(odd), "owns_canary": True, "helper": None})
        assert inherited.exists() and odd.exists()

    def test_prune_only_old_empty_canaries(self, tmp_path):
        old, new, full, other = (tmp_path / f"{WS.CANARY_PREFIX}old", tmp_path / f"{WS.CANARY_PREFIX}new",
                                 tmp_path / f"{WS.CANARY_PREFIX}full", tmp_path / "unrelated_old")
        for d in (old, new, full, other):
            d.mkdir()
        (full / "f").write_text("x")
        t = 1_000_000.0
        for d in (old, full, other):
            os.utime(d, (t, t))
        os.utime(new, (t + WS.PRUNE_AFTER_S, t + WS.PRUNE_AFTER_S))
        WS._prune_old_canaries(str(tmp_path), now=t + WS.PRUNE_AFTER_S + 1)
        assert not old.exists() and new.exists() and full.exists() and other.exists()


class TestPaperPortfolioGuardDefers:
    """B2：逐条夹具 `_isolate_paper_portfolio_state` 的指纹比对在 573 没改——14:00 扫描写这四个文件时全量照样一串 ERROR。"""

    @staticmethod
    def _run(fn, tmp_path, monkeypatch, real_dir, sb):
        cfg = types.SimpleNamespace(_alpha_hive_write_sandbox=sb)
        gen = fn(tmp_path / "t", monkeypatch, real_dir, cfg)
        (tmp_path / "t").mkdir(exist_ok=True)
        next(gen)
        (real_dir / "meta.json").write_text('{"changed": "by the scan"}')     # 沙箱外写入者
        try:
            next(gen)
        except StopIteration:
            return

    def test_defers_when_sandbox_covers_it(self, paper_portfolio_isolation_fn, tmp_path, monkeypatch):
        root = tmp_path / "root"
        real = root / "paper_portfolio_state"
        real.mkdir(parents=True)
        sb = {"active": True, "root": os.path.realpath(root)}
        self._run(paper_portfolio_isolation_fn, tmp_path, monkeypatch, real, sb)        # 不抛

    def test_positive_control_still_red_without_sandbox(self, paper_portfolio_isolation_fn, tmp_path, monkeypatch):
        """正对照：同一份写入在沙箱未生效 / 不覆盖时必须红——否则上一条的「不抛」可能只是夹具根本没在比。"""
        real = tmp_path / "root" / "paper_portfolio_state"
        real.mkdir(parents=True)
        for sb in ({"active": False, "root": os.path.realpath(tmp_path / "root")},
                   {"active": True, "root": "/nonexistent/elsewhere"}):
            (real / "meta.json").unlink(missing_ok=True)
            with pytest.raises(AssertionError, match="生产"):
                self._run(paper_portfolio_isolation_fn, tmp_path, monkeypatch, real, sb)

    def test_covers(self):
        sb = {"active": True, "root": ROOT}
        assert WS.covers(sb, f"{ROOT}/paper_portfolio_state") and WS.covers(sb, ROOT)
        assert not WS.covers(sb, f"{ROOT}-other/paper_portfolio_state")
        assert not WS.covers({**sb, "active": False}, ROOT) and not WS.covers(None, ROOT)

    def test_live_real_dir_is_covered(self, pytestconfig, _real_paper_portfolio_state_dir):
        """本机实测：生产真身确实落在被禁写的根下——否则让位条件在真实环境里恒假，B2 等于没修。"""
        st = _live(pytestconfig)
        if not st.get("active"):
            pytest.fail(f"写沙箱没生效：{st.get('reason')}")
        assert WS.covers(st, _real_paper_portfolio_state_dir), (st["root"], _real_paper_portfolio_state_dir)
