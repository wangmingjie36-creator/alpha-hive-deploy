"""v0.45.648 `tests/_scan_clock.py`——财报信号 `scan` 的钉钟辅助，以及守它的两道闸。

闸 1：`earnings_vol_signal.py` 里的每一次墙钟读取，都必须在 `_today_pdt` / `_now_et` 这两个注入缝里（或 `main`
的缺省日期）。哪天有人在 `scan` 里直接 `datetime.now()`，所有「钉了 `_now_et`」的测试都钉不住它，而偶发红只会
在撞秒的时候才出现——这道闸让它在写下的那一刻就红。
闸 2：用这个 scan 的几个测试文件不再自己手写 `setattr(evs, "_now_et", …)`——同一个钉法写三份，哪天多出第三只钟，
漏改的那份就是下一颗偶发。
"""

import ast
from pathlib import Path

import pytest

import earnings_vol_signal as evs
from tests import _scan_clock as SC

ROOT = Path(__file__).resolve().parent.parent

#: 允许直接读墙钟的函数：两个注入缝本身，以及 `main` 里「没给日期就用今天」的缺省
CLOCK_SEAMS = frozenset({"_today_pdt", "_now_et", "main"})
_CLOCK_ATTRS = {("datetime", "now"), ("datetime", "utcnow"), ("date", "today"), ("time", "time"),
                ("datetime", "today")}


def _direct_clock_reads(tree):
    """`[(最外层函数名或 '<module>', 行号, 调用文本)]`：源码里每一次直接读墙钟的调用。"""
    out = []

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            o = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner == "<module>":
                o = child.name
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                        and (f.value.id, f.attr) in _CLOCK_ATTRS:
                    out.append((o, child.lineno, f"{f.value.id}.{f.attr}()"))
                elif isinstance(f, ast.Name) and f.id == "pdt_today":
                    out.append((o, child.lineno, "pdt_today()"))
            visit(child, o)

    visit(tree, "<module>")
    return out


class TestEveryClockReadGoesThroughASeam:
    def test_earnings_vol_signal_reads_the_wall_clock_only_inside_the_seams(self):
        reads = _direct_clock_reads(ast.parse((ROOT / "earnings_vol_signal.py").read_text(encoding="utf-8")))
        assert reads, "探针一次读钟都没找到——它自己坏了（两个注入缝里至少各有一次）"
        bad = [r for r in reads if r[0] not in CLOCK_SEAMS]
        assert bad == [], (f"这些函数在注入缝之外直接读墙钟：{bad}——改成调 `_now_et()` / `_today_pdt()`，"
                           "否则 `tests/_scan_clock.py` 钉不住它，偶发红只会在撞秒时出现")
        assert {r[0] for r in reads} >= {"_today_pdt", "_now_et"}

    def test_probe_has_teeth(self):
        bad = ast.parse("import datetime\n"
                        "def _now_et():\n    return datetime.now()\n"
                        "def scan():\n    return datetime.now(), date.today(), pdt_today(), time.time()\n")
        reads = _direct_clock_reads(bad)
        assert [r[0] for r in reads if r[0] not in CLOCK_SEAMS] == ["scan"] * 4
        assert [r[0] for r in reads if r[0] in CLOCK_SEAMS] == ["_now_et"]
        ok = ast.parse("def _now_et():\n    return datetime.now()\ndef scan():\n    return _now_et()\n")
        assert [r for r in _direct_clock_reads(ok) if r[0] not in CLOCK_SEAMS] == []


GUARDED_TEST_FILES = ("test_earnings_vol_signal.py", "test_straddle_gex_shadow.py", "test_straddle_strangle_shadow.py")


def _hand_written_now_pins(src):
    """源码里手写的 `setattr(<任何>, "_now_et", …)` 行号。钉钟只许走 `tests/_scan_clock`。"""
    tree = ast.parse(src)
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "setattr"
            and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "_now_et"]


class TestNoHandWrittenClockPins:
    @pytest.mark.parametrize("fname", GUARDED_TEST_FILES)
    def test_file_uses_the_shared_helper(self, fname):
        src = (ROOT / "tests" / fname).read_text(encoding="utf-8")
        assert _hand_written_now_pins(src) == [], f"{fname} 里有手写的 `_now_et` 钉钟——改用 tests/_scan_clock.pin_scan_clock"
        assert "pin_scan_clock" in src or "tick_scan_clock" in src, f"{fname} 没有用共享钉钟辅助"

    def test_probe_has_teeth(self):
        assert _hand_written_now_pins('monkeypatch.setattr(evs, "_now_et", lambda: X)\n') == [1]
        assert _hand_written_now_pins('monkeypatch.setattr(evs, "_today_pdt", lambda: X)\n') == []
        assert _hand_written_now_pins("pin_scan_clock(monkeypatch, evs)\n") == []


class TestHelper:
    def test_pin_pins_both_seams(self, monkeypatch):
        SC.pin_scan_clock(monkeypatch, evs)
        assert evs._today_pdt() == SC.SCAN_AS_OF and evs._now_et() == SC.POST_CLOSE

    def test_pin_takes_explicit_day_and_instant(self, monkeypatch):
        SC.pin_scan_clock(monkeypatch, evs, "2026-09-20", SC.INTRADAY)
        assert evs._today_pdt() == "2026-09-20" and evs._now_et() == SC.INTRADAY

    def test_tick_advances_by_the_step_on_every_read_starting_at_start(self, monkeypatch):
        SC.tick_scan_clock(monkeypatch, evs)
        a, b, c = evs._now_et(), evs._now_et(), evs._now_et()
        assert a == SC.POST_CLOSE
        assert (b - a).total_seconds() == 1 and (c - b).total_seconds() == 1
        SC.tick_scan_clock(monkeypatch, evs, step_seconds=30)
        assert (evs._now_et() - SC.POST_CLOSE).total_seconds() == 0 and (evs._now_et() - SC.POST_CLOSE).total_seconds() == 30

    def test_the_two_instants_mean_what_their_names_say(self):
        """POST_CLOSE 是收盘后（记录的 session_live=False），INTRADAY 是盘中（True）——协议只收收盘后的记录。"""
        assert evs.session_live_at(SC.POST_CLOSE) is False and evs.session_live_at(SC.INTRADAY) is True
        assert SC.POST_CLOSE.date().isoformat() == SC.SCAN_AS_OF == SC.INTRADAY.date().isoformat()
