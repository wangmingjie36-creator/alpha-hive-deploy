"""v0.45.448 `ledger_io`：账本读写的唯一实现——代码写不出坏行；读到坏行（外部损坏）就停，不猜不跳过。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ledger_io as L  # noqa: E402


def _bytes(p: Path) -> bytes:
    return p.read_bytes() if p.exists() else b""


def _leftover_tmp(d: Path) -> list:
    return sorted(x.name for x in d.iterdir() if ".tmp." in x.name)


# ── 写：不合法的一条都不进文件，文件一个字节都不动 ──────────────────────────────

class TestWriteRefusesBadRecords:

    @pytest.mark.parametrize("bad", [
        [{"ok": 1}, [1, 2]],                          # 非对象
        [{"x": float("nan")}],                        # NaN —— json.dumps 缺省会写出非法的 NaN 字面量
        [{"x": float("inf")}],
        [{"x": object()}],                            # 不可序列化
    ])
    def test_write_jsonl_rejects_and_leaves_file_untouched(self, tmp_path, bad):
        p = tmp_path / "led.jsonl"
        p.write_text('{"old": 1}\n', encoding="utf-8")
        before = _bytes(p)
        with pytest.raises(L.LedgerWriteError):
            L.write_jsonl(p, bad)
        assert _bytes(p) == before and _leftover_tmp(tmp_path) == []

    def test_append_rejects_nan_before_touching_the_file(self, tmp_path):
        p = tmp_path / "led.jsonl"
        p.write_text('{"old": 1}\n', encoding="utf-8")
        with pytest.raises(L.LedgerWriteError):
            L.append_jsonl(p, {"x": float("nan")})
        assert p.read_text(encoding="utf-8") == '{"old": 1}\n'


# ── 写：原子——中途崩溃只会留下旧版或新版 ──────────────────────────────────────

class TestWriteIsAtomic:

    def test_crash_at_replace_keeps_the_old_file_and_no_temp(self, tmp_path, monkeypatch):
        p = tmp_path / "led.jsonl"
        p.write_text('{"old": 1}\n', encoding="utf-8")

        def boom(src, dst):
            raise OSError("模拟：替换前断电")
        monkeypatch.setattr(L.os, "replace", boom)
        with pytest.raises(OSError):
            L.append_jsonl(p, {"new": 2})
        assert p.read_text(encoding="utf-8") == '{"old": 1}\n' and _leftover_tmp(tmp_path) == []

    def test_readback_mismatch_is_loud(self, tmp_path, monkeypatch):
        p = tmp_path / "led.jsonl"
        real = os.replace

        def replace_then_damage(src, dst):
            real(src, dst)
            with open(dst, "w", encoding="utf-8") as f:     # noqa: 测试里模拟磁盘 / 并发写者改了内容
                f.write("{坏\n")
        monkeypatch.setattr(L.os, "replace", replace_then_damage)
        with pytest.raises(L.LedgerWriteError, match="读回"):
            L.write_jsonl(p, [{"a": 1}])

    def test_written_file_mode_is_0644(self, tmp_path):
        p = tmp_path / "led.jsonl"
        L.write_jsonl(p, [{"a": 1}])
        assert (p.stat().st_mode & 0o777) == 0o644


# ── 追加：旧行逐字节不变；坏文件上不续写；缺换行不粘行 ─────────────────────────

class TestAppend:

    def test_existing_lines_are_kept_byte_for_byte(self, tmp_path):
        p = tmp_path / "led.jsonl"
        old = '{"a": 1.50, "b": "中文"}\n{"c":2}\n'                   # 重新序列化会改写法（1.5 / 空格）
        p.write_text(old, encoding="utf-8")
        L.append_jsonl(p, {"d": 3})
        assert p.read_text(encoding="utf-8") == old + '{"d": 3}\n'

    def test_corrupt_existing_line_stops_the_append(self, tmp_path):
        p = tmp_path / "led.jsonl"
        p.write_text('{"a": 1}\n{坏\n', encoding="utf-8")
        before = _bytes(p)
        with pytest.raises(L.LedgerCorrupt, match="第 2 行"):
            L.append_jsonl(p, {"b": 2})
        assert _bytes(p) == before

    def test_missing_trailing_newline_does_not_glue_lines(self, tmp_path):
        p = tmp_path / "led.jsonl"
        p.write_text('{"a": 1}', encoding="utf-8")
        L.append_jsonl(p, {"b": 2})
        assert L.load_jsonl(p) == [{"a": 1}, {"b": 2}]

    def test_append_creates_missing_file_and_dir(self, tmp_path):
        p = tmp_path / "new_dir" / "led.jsonl"
        L.append_jsonl(p, {"a": 1})
        assert L.load_jsonl(p) == [{"a": 1}]


# ── 读：严格，带行号；缺文件 = [] ────────────────────────────────────────────────

class TestStrictLoad:

    def test_missing_file_is_empty(self, tmp_path):
        assert L.load_jsonl(tmp_path / "nope.jsonl") == []

    @pytest.mark.parametrize("content, where", [
        ('{"a": 1}\n{坏\n', "第 2 行不是合法 JSON"),
        ('{"a": 1}\n[1, 2]\n', "第 2 行不是对象"),
        ('{"a": NaN}\n', "第 1 行不是合法 JSON"),
        ('{"a": 1}\n\n42\n', "第 3 行不是对象"),
    ])
    def test_bad_lines_raise_with_line_number(self, tmp_path, content, where):
        p = tmp_path / "led.jsonl"
        p.write_text(content, encoding="utf-8")
        with pytest.raises(L.LedgerCorrupt, match=where):
            L.load_jsonl(p)

    def test_invalid_utf8_is_corrupt(self, tmp_path):
        p = tmp_path / "led.jsonl"
        p.write_bytes(b'{"a": "\xff"}\n')
        with pytest.raises(L.LedgerCorrupt, match="UTF-8"):
            L.load_jsonl(p)


# ── 锁：同线程可重入；跨线程、跨进程互斥 ────────────────────────────────────────

class TestLock:

    def test_reentrant_in_the_same_thread(self, tmp_path):
        with L.locked(tmp_path):
            with L.locked(tmp_path, timeout=0.2):
                L.append_jsonl(tmp_path / "led.jsonl", {"a": 1})       # append 自己也取锁：不自锁
            assert L.is_locked_here(tmp_path)
        assert not L.is_locked_here(tmp_path)

    def test_other_thread_waits_then_times_out(self, tmp_path):
        held, release = threading.Event(), threading.Event()

        def holder():
            with L.locked(tmp_path):
                held.set()
                release.wait(5)
        t = threading.Thread(target=holder)
        t.start()
        try:
            assert held.wait(5)
            with pytest.raises(L.LedgerLockTimeout):
                with L.locked(tmp_path, timeout=0.2):
                    pass
        finally:
            release.set()
            t.join(5)
        with L.locked(tmp_path, timeout=1):                             # 释放后能拿到
            pass

    def test_other_process_is_excluded(self, tmp_path):
        ready = tmp_path / "ready"
        code = ("import sys, time, pathlib; sys.path.insert(0, %r); import ledger_io as L\n"
                "with L.locked(%r):\n    pathlib.Path(%r).write_text('1')\n    time.sleep(3)\n"
                % (str(ROOT), str(tmp_path), str(ready)))
        proc = subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ))
        try:
            t0 = time.monotonic()
            while not ready.exists():
                assert time.monotonic() - t0 < 20 and proc.poll() is None, "子进程没拿到锁"
                time.sleep(0.02)
            with pytest.raises(L.LedgerLockTimeout):
                with L.locked(tmp_path, timeout=0.3):
                    pass
        finally:
            proc.wait(20)
        assert proc.returncode == 0

    def test_concurrent_appends_from_processes_lose_nothing(self, tmp_path):
        """四个进程各追加 25 条：100 条全在、每行都是合法对象——裸 open("a") + 无锁读改写保证不了的两件事。"""
        p = tmp_path / "led.jsonl"
        code = ("import sys; sys.path.insert(0, %r); import ledger_io as L\n"
                "w = int(sys.argv[1])\n"
                "for i in range(25):\n    L.append_jsonl(%r, {'w': w, 'i': i, 'pad': 'x' * 300})\n"
                % (str(ROOT), str(p)))
        procs = [subprocess.Popen([sys.executable, "-c", code, str(w)], env=dict(os.environ)) for w in range(4)]
        for pr in procs:
            pr.wait(120)
        assert [pr.returncode for pr in procs] == [0, 0, 0, 0]
        rows = L.load_jsonl(p)
        assert len(rows) == 100 and len({(r["w"], r["i"]) for r in rows}) == 100
        assert all(json.loads(line) for line in p.read_text(encoding="utf-8").splitlines())
