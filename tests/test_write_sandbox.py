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
