"""开跑时的并发提示（v0.45.590，`tests/_concurrency_hint.py`）——只提示、不阻塞、不报红。

四层：
1. 纯逻辑 `lines`：什么时候说、什么时候不说（小会话不吵、对方死了 / 太小 / 条数未知不算、登记不可用要如实说）。
2. 经登记目录的 `session_lines`：本会话把条数与拨钟标记写进登记；读到别的活会话；任何失败都变成一行说明、绝不抛。
3. 接线：conftest 里真有这个钩子，且在**真实的 pytest 会话、`-q` 下**打出来（子进程跑一遍——
   曾经拆成 `pytest_collection_finish` 存 stash、报告钩子再读，读到的是空，`-q` 下什么都不打，单测全绿）。
4. 不阻塞：提示函数在登记坏掉时不抛。
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

import _concurrency_hint as CH

ROOT = "/nonexistent/alpha-hive-data"
DEAD = 2 ** 22 + 12345            # 远超 pid_max：kill(pid, 0) 必是 ProcessLookupError
_TESTS = pathlib.Path(__file__).resolve().parent


def _other(pid=1234, items=5000, alive=True, shifted=False):
    return {"pid": pid, "items": items, "alive": alive, "shifted": shifted}


class TestLines:
    def test_small_run_never_warns(self):
        assert CH.lines(10, [_other()]) == []
        assert CH.lines(CH.BIG_RUN_ITEMS - 1, [_other()], unavailable="x") == []

    def test_big_run_with_big_live_other_warns_and_names_it(self):
        out = CH.lines(CH.BIG_RUN_ITEMS, [_other(pid=777, items=11000)])
        assert len(out) == 1 and out[0].startswith("concurrency:") and "pid 777" in out[0] and "11000" in out[0]
        assert "clockshift" not in out[0]

    def test_shifted_other_is_called_out_as_clockshift(self):
        assert "clockshift" in CH.lines(5000, [_other(shifted=True)])[0]

    @pytest.mark.parametrize("other", [
        _other(alive=False),                    # 对方已结束
        _other(items=CH.BIG_RUN_ITEMS - 1),     # 对方只是个小会话
        _other(items=None),                     # 对方还在收集、条数未知
    ])
    def test_irrelevant_others_do_not_count(self, other):
        assert CH.lines(5000, [other]) == []

    def test_no_others_is_silent(self):
        assert CH.lines(5000, []) == [] and CH.lines(5000, None) == []

    def test_unavailable_is_said_out_loud_for_big_runs(self):
        out = CH.lines(5000, None, unavailable="写沙箱未生效：非 macOS")
        assert len(out) == 1 and "无法检测" in out[0] and "非 macOS" in out[0]

    def test_threshold_override(self):
        assert CH.lines(5, [_other(items=9)], threshold=3) and not CH.lines(5, [_other(items=2)], threshold=3)


def _registry(tmp_path):
    reg = tmp_path / "registry"
    reg.mkdir(parents=True)
    st = {"root": ROOT, "started_epoch": 1000.0, "registry": str(reg), "session_id": "me",
          "pids_file": str(reg / "me.pids")}
    (reg / "me.json").write_text(json.dumps({"pid": os.getpid(), "root": ROOT, "started": 1000.0, "ended": None}))
    return reg, st


def _write_other(reg, sid, pid, **extra):
    rec = {"pid": pid, "root": ROOT, "started": 900.0, "ended": None, "checked": False}
    rec.update(extra)
    (reg / f"{sid}.json").write_text(json.dumps(rec))


class TestSessionLines:
    def test_registers_own_items_and_shift_flag(self, tmp_path, monkeypatch):
        reg, st = _registry(tmp_path)
        monkeypatch.setenv("CLOCK_SHIFT_DAYS", "7")
        CH.session_lines(st, 3000)
        rec = json.loads((reg / "me.json").read_text())
        assert rec["items"] == 3000 and rec["shifted"] is True, "别的会话要靠这两个字段看见我"
        monkeypatch.setenv("CLOCK_SHIFT_DAYS", "0")
        CH.session_lines(st, 3100)
        assert json.loads((reg / "me.json").read_text())["shifted"] is False

    def test_sees_other_live_big_session_through_the_registry(self, tmp_path):
        reg, st = _registry(tmp_path)
        _write_other(reg, "x", os.getpid(), items=9000, shifted=True)      # 借本进程号证明 alive
        out = CH.session_lines(st, 9000)
        assert len(out) == 1 and "clockshift" in out[0] and "9000" in out[0]

    def test_ignores_dead_other_through_the_registry(self, tmp_path):
        reg, st = _registry(tmp_path)
        _write_other(reg, "x", DEAD, items=9000)
        assert CH.session_lines(st, 9000) == []

    def test_small_session_does_not_warn_but_still_registers(self, tmp_path):
        reg, st = _registry(tmp_path)
        _write_other(reg, "x", os.getpid(), items=9000)
        assert CH.session_lines(st, 5) == []
        assert json.loads((reg / "me.json").read_text())["items"] == 5

    @pytest.mark.parametrize("sb, expect", [
        ({"inherited": True}, "继承外层"),
        ({"registry_error": "会话登记不可用（权限）"}, "会话登记不可用（权限）"),
        ({"reason": "平台 linux 没有 sandbox_init"}, "平台 linux"),
        (None, "未尝试"),
    ])
    def test_no_registry_says_why_for_big_runs(self, sb, expect):
        out = CH.session_lines(sb, 5000)
        assert len(out) == 1 and "无法检测" in out[0] and expect in out[0]
        assert CH.session_lines(sb, 5) == []

    def test_broken_registry_never_raises(self, tmp_path):
        reg, st = _registry(tmp_path)
        (reg / "me.json").unlink()
        for f in reg.iterdir():
            f.unlink()
        reg.rmdir()                                            # 登记目录被删：listdir 抛 OSError
        out = CH.session_lines(st, 5000)
        assert len(out) == 1 and "无法检测" in out[0]


class TestWiring:
    def test_conftest_defines_the_hook_with_items(self):
        tree = ast.parse((_TESTS / "conftest.py").read_text(encoding="utf-8"))
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        hook = fns.get("pytest_report_collectionfinish")
        assert hook is not None and "items" in [a.arg for a in hook.args.args]
        assert any(isinstance(n, ast.Attribute) and n.attr == "session_lines" for n in ast.walk(hook))
        # 别拆成「收集结束钩子存 stash、这里再读」——那是已经踩过的坑，由下面的真实会话测试兜底

    def test_real_quiet_session_prints_the_line(self):
        """真起一个 pytest 会话（`-q`），阈值调到 3，选一个与阈值无关的小测试类（5 条）⇒ 必须出现 `concurrency:`。
        内层要么继承外层写沙箱、要么沙箱没套上，两种都会打出一行（这里只验接线，不验内容）。
        ⚠️ 内层不能选本文件的测试：它们读 `BIG_RUN_ITEMS`，而内层把它改成了 3。"""
        code = ("import sys, pytest; sys.path.insert(0, 'tests'); import _concurrency_hint as H; H.BIG_RUN_ITEMS = 3\n"
                "sys.exit(pytest.main(['-q', '--no-header', '-p', 'no:cacheprovider', '-o', 'addopts=',"
                " 'tests/test_write_sandbox.py::TestPureLogic']))")
        r = subprocess.run([sys.executable, "-c", code], cwd=_TESTS.parent, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "concurrency:" in r.stdout, f"-q 下没有并发提示行：\n{r.stdout}"


class TestLoadLines:
    """系统 load（v0.45.668）：登记看不见的那一半——泄漏的后台负载不是 pytest 会话。"""

    def test_small_run_never_warns_even_when_overloaded(self):
        assert CH.load_lines(10, 124.0, 10) == []
        assert CH.load_lines(CH.BIG_RUN_ITEMS - 1, None, None, unavailable="x") == []

    def test_quiet_machine_is_silent(self):
        assert CH.load_lines(5000, 2.7, 10) == []
        assert CH.load_lines(5000, 9.99, 10) == []

    def test_load_at_or_above_core_count_warns_with_numbers_and_the_command(self):
        for load1 in (10.0, 10.4, 124.0):
            out = CH.load_lines(5000, load1, 10)
            assert len(out) == 1 and out[0].startswith("concurrency:") and "⚠️" in out[0]
            assert f"{load1:.0f} / 10 核" in out[0]
            assert CH.WHO_IS_BUSY in out[0] and "ppid=1" in out[0]

    def test_threshold_is_the_logged_slow_case(self):
        """590 那轮 load≈11、10 核、整套跑了 1339 秒（安静时 500–650 秒）——必须被提示到。"""
        assert CH.load_lines(11000, 11.0, 10)
        assert CH.LOAD_WARN_PER_CORE == 1.0

    @pytest.mark.parametrize("load1, ncpu", [(None, 10), (5.0, None), (5.0, 0)])
    def test_unreadable_values_are_said_out_loud(self, load1, ncpu):
        out = CH.load_lines(5000, load1, ncpu)
        assert len(out) == 1 and "无法读取系统 load" in out[0]

    def test_explicit_unavailable_reason_is_shown(self):
        out = CH.load_lines(5000, None, None, unavailable="OSError: boom")
        assert "无法读取系统 load" in out[0] and "OSError: boom" in out[0]

    def test_threshold_override(self):
        assert CH.load_lines(5, 50.0, 10, threshold=3) and not CH.load_lines(2, 50.0, 10, threshold=3)


class TestSystemLoadLines:
    def test_reads_the_real_values_and_hands_them_over(self, monkeypatch):
        monkeypatch.setattr(os, "getloadavg", lambda: (33.0, 20.0, 10.0))
        monkeypatch.setattr(os, "cpu_count", lambda: 8)
        out = CH.system_load_lines(5000)
        assert len(out) == 1 and "33 / 8 核" in out[0]
        monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
        assert CH.system_load_lines(5000) == []

    @pytest.mark.parametrize("exc", [OSError("no load"), AttributeError("no getloadavg")])
    def test_read_failure_never_raises_and_says_so(self, monkeypatch, exc):
        def boom():
            raise exc

        monkeypatch.setattr(os, "getloadavg", boom)
        out = CH.system_load_lines(5000)
        assert len(out) == 1 and "无法读取系统 load" in out[0] and type(exc).__name__ in out[0]

    def test_small_run_does_not_even_matter_when_unreadable(self, monkeypatch):
        monkeypatch.setattr(os, "getloadavg", lambda: (_ for _ in ()).throw(OSError("x")))
        assert CH.system_load_lines(5) == []


class TestLoadWiring:
    def test_conftest_hook_also_calls_system_load_lines(self):
        """接线：钩子里必须真的调 `system_load_lines`（只写了函数没接线，等于没有——整套全绿、提示永远不出现）。"""
        tree = ast.parse((_TESTS / "conftest.py").read_text(encoding="utf-8"))
        hook = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "pytest_report_collectionfinish")
        called = {n.attr for n in ast.walk(hook) if isinstance(n, ast.Attribute)}
        assert {"session_lines", "system_load_lines"} <= called

    def test_real_overloaded_session_prints_the_load_line(self):
        """真起一个 pytest 会话（`-q`）：阈值调到 3、把读到的 load 钉成「远超核数」，选一个小测试类（5 条）
        ⇒ 报告头里必须出现 load 那一行。（`BIG_RUN_ITEMS` 与 `os.getloadavg` 都只在内层进程里改。）"""
        code = ("import os, sys, pytest; sys.path.insert(0, 'tests'); import _concurrency_hint as H; H.BIG_RUN_ITEMS = 3\n"
                "os.getloadavg = lambda: (500.0, 400.0, 300.0)\n"
                "sys.exit(pytest.main(['-q', '--no-header', '-p', 'no:cacheprovider', '-o', 'addopts=',"
                " 'tests/test_write_sandbox.py::TestPureLogic']))")
        r = subprocess.run([sys.executable, "-c", code], cwd=_TESTS.parent, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "系统 1 分钟 load 500" in r.stdout and "机器已超订" in r.stdout, f"-q 下没有 load 提示行：\n{r.stdout}"
