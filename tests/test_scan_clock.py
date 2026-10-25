"""`tests/_scan_clock.py`——财报信号 `scan` 的钉钟辅助，以及守它的两道闸。

闸 1：`earnings_vol_signal.py` 里的每一次墙钟读取，都必须落在登记过的 (函数, 时钟) 对里——两个注入缝 `_today_pdt` /
`_now_et` 各读一只钟，外加 `main` 里「没给日期就用今天」那一个调用。哪天有人在 `scan` 里直接读钟，所有「钉了 `_now_et`」的
测试都钉不住它，而偶发红只会在撞秒的时候才出现——这道闸让它在写下的那一刻就红。认法按**名字解析**而不是按写法：
`import datetime as dt` / `from datetime import datetime as D` / `datetime.datetime.now()` / `from hive_logger import
pdt_today as p` 都认得出，且不把 `from datetime import time as _t; _t(16, 0)`（构造时刻，不是读钟）误报。
闸 2：凡是在进程内调用 `earnings_vol_signal.scan` / `main` / `_scan_with_info` 的测试文件（按 AST 自动圈，不是写死的名单）
不许再手写钉这两只钟——不论是 `setattr(evs, "_now_et", …)`、字符串路径 `setattr("earnings_vol_signal._now_et", …)`、
`patch.object` 还是只钉 `_today_pdt`——并且必须真的调用共享辅助。同一个钉法写几份，哪天多出第三只钟，漏改的那份就是下一颗偶发。

不管的东西：`scan` 调到的别的模块（`ledger_io` 等）里的读钟——那不是这两个缝；计时用的 `time.monotonic` / `perf_counter`
（量时长，不是盖时间戳）。
"""

import ast
from pathlib import Path

import pytest

import earnings_vol_signal as evs
from tests import _repo_files as RF
from tests import _scan_clock as SC

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

#: 读墙钟的完整限定名（`import` 解析之后）
CLOCK_NAMES = frozenset({"datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today",
                         "datetime.date.today", "time.time", "time.time_ns", "hive_logger.pdt_today"})
#: 允许出现的 (所在最外层函数, 时钟) 对。`main` 只放它缺省日期那一个调用，不是整个函数豁免
ALLOWED_READS = frozenset({("_today_pdt", "hive_logger.pdt_today"),
                           ("_now_et", "datetime.datetime.now"),
                           ("main", "hive_logger.pdt_today")})
SEAM_NAMES = ("_now_et", "_today_pdt")
#: 进程内跑 `scan` 的入口（`scan` 只是 `_scan_with_info` 的转发，`main` 是 CLI）
SCAN_ENTRY_POINTS = frozenset({"earnings_vol_signal.scan", "earnings_vol_signal.main",
                               "earnings_vol_signal._scan_with_info"})
HELPER_NAMES = frozenset({"tests._scan_clock.pin_scan_clock", "tests._scan_clock.tick_scan_clock"})
#: 自动圈出来的文件里必须有的几个（圈不到它们说明圈法坏了——下面的断言都不作数）
KNOWN_SCAN_TESTS = frozenset({"test_earnings_vol_signal.py", "test_straddle_gex_shadow.py",
                              "test_straddle_strangle_shadow.py", "test_earnings_event_shadow.py"})


# ───────────── AST 工具：按名字解析，不按写法 ─────────────

def _bindings(tree):
    """本地名 → 它指向的完整限定名集合（整个模块所有作用域；函数里的局部 import 也算）。"""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.setdefault(a.asname or a.name.split(".")[0], set()).add(a.name if a.asname else a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for a in n.names:
                out.setdefault(a.asname or a.name, set()).add(f"{n.module}.{a.name}")
    return out


def _chain(node):
    """`a.b.c` → ['a', 'b', 'c']；根不是 Name（如 `f().x`）→ None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def _qualified(node, bind):
    """Name / Attribute 节点解析成完整限定名集合；根没有 import 绑定（局部变量等）⇒ 空集。"""
    ch = _chain(node)
    if not ch or ch[0] not in bind:
        return set()
    return {".".join([b] + ch[1:]) for b in bind[ch[0]]}


def _clock_reads(tree):
    """`[(最外层函数名或 '<module>', 行号, 完整限定名)]`：源码里每一处**引用**到墙钟的地方（不只是调用，
    `default_factory=datetime.now` 这种不带括号的也算）。"""
    bind = _bindings(tree)
    out = []

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            o = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner == "<module>":
                o = child.name
            if isinstance(child, (ast.Name, ast.Attribute)):
                for q in _qualified(child, bind) & CLOCK_NAMES:
                    out.append((o, child.lineno, q))
            visit(child, o)

    visit(tree, "<module>")
    return out


def _calls_scan(tree):
    """这份源码在进程内调用了 `earnings_vol_signal` 的 scan / main / _scan_with_info 吗。"""
    bind = _bindings(tree)
    return any(isinstance(n, ast.Call) and _qualified(n.func, bind) & SCAN_ENTRY_POINTS for n in ast.walk(tree))


_PIN_CALLS = {"setattr", "setitem", "object", "patch"}


def _hand_written_pins(tree):
    """手写钉钟的行号：`setattr` / `setitem` / `patch.object` / `patch` 的前两个实参里出现 `_now_et` 或 `_today_pdt`
    ——不论是 `(evs, "_now_et", …)` 还是字符串路径 `("earnings_vol_signal._now_et", …)`，也不论只钉其中一只。"""
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        if name not in _PIN_CALLS:
            continue
        for a in n.args[:2]:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and any(a.value == s or a.value.endswith("." + s) for s in SEAM_NAMES):
                out.append(n.lineno)
                break
    return out


def _uses_helper(tree):
    bind = _bindings(tree)
    return any(isinstance(n, ast.Call) and _qualified(n.func, bind) & HELPER_NAMES for n in ast.walk(tree))


@pytest.fixture(scope="module")
def scan_test_files():
    """`{文件名: 解析后的 AST}`：tests/ 下所有在进程内调用 scan / main / _scan_with_info 的文件。
    先用子串粗筛（文件名里没有 `earnings_vol_signal` 的不可能 import 它），再解析；走共享的剪枝遍历，
    不碰 iCloud 重名副本。"""
    res = RF.walk_own_python_files(TESTS)
    assert res.errors == [], res.errors[:3]
    assert len(res.files) >= 100, "tests/ 下的文件数少得不正常——遍历坏了"
    out = {}
    for p in res.files:
        src = p.read_text(encoding="utf-8")
        if "earnings_vol_signal" not in src:
            continue
        tree = ast.parse(src)
        if _calls_scan(tree):
            out[p.name] = tree
    return out


# ───────────── 闸 1 ─────────────

class TestEveryClockReadGoesThroughASeam:
    def test_earnings_vol_signal_reads_the_wall_clock_only_at_the_registered_places(self):
        reads = _clock_reads(ast.parse((ROOT / "earnings_vol_signal.py").read_text(encoding="utf-8")))
        found = {(o, q) for o, _, q in reads}
        extra = sorted((o, ln, q) for o, ln, q in reads if (o, q) not in ALLOWED_READS)
        assert extra == [], (f"这些地方在登记的 (函数, 时钟) 对之外读墙钟：{extra}——改成调 `_now_et()` / `_today_pdt()`，"
                             "否则 `tests/_scan_clock.py` 钉不住它，偶发红只会在撞秒时出现")
        # 反方向：登记的对必须真的还在，否则登记表是过期的（探针本身坏了也会在这里红）
        assert ALLOWED_READS - found == set(), f"登记的读钟点已经不存在，更新 ALLOWED_READS：{sorted(ALLOWED_READS - found)}"

    def test_the_default_date_in_main_is_the_only_unseamed_read(self):
        """`main` 不是整体豁免：它里面除了「没给日期就用今天」这一个调用，不许再读钟。"""
        reads = _clock_reads(ast.parse((ROOT / "earnings_vol_signal.py").read_text(encoding="utf-8")))
        assert [(q) for o, _, q in reads if o == "main"] == ["hive_logger.pdt_today"]

    @pytest.mark.parametrize("src, expect", [
        # 五种写法都得认出来（每一种都曾是闸 1 第一版的盲区）
        ("import datetime\ndef scan():\n    return datetime.datetime.now()\n", "datetime.datetime.now"),
        ("import datetime as dt\ndef scan():\n    return dt.datetime.utcnow()\n", "datetime.datetime.utcnow"),
        ("from datetime import datetime as D\ndef scan():\n    return D.now()\n", "datetime.datetime.now"),
        ("from datetime import date\ndef scan():\n    return date.today()\n", "datetime.date.today"),
        ("import time\ndef scan():\n    return time.time()\n", "time.time"),
        ("from time import time as t\ndef scan():\n    return t()\n", "time.time"),
        ("from hive_logger import pdt_today as p\ndef scan():\n    return p()\n", "hive_logger.pdt_today"),
        ("import hive_logger\ndef scan():\n    return hive_logger.pdt_today()\n", "hive_logger.pdt_today"),
        # 不带括号的引用也算（传出去以后在别处调用）
        ("from datetime import datetime\ndef scan(clock=datetime.now):\n    return clock()\n", "datetime.datetime.now"),
        # 局部 import 也解析
        ("def scan():\n    from hive_logger import pdt_today\n    return pdt_today()\n", "hive_logger.pdt_today"),
    ])
    def test_probe_sees_every_spelling_of_a_clock_read(self, src, expect):
        reads = _clock_reads(ast.parse(src))
        assert [(o, q) for o, _, q in reads] == [("scan", expect)], reads

    @pytest.mark.parametrize("src", [
        "from datetime import time as _t\ndef session():\n    return _t(16, 0)\n",          # 构造时刻，不是读钟
        "from datetime import datetime\ndef f(d):\n    return datetime.strptime(d, '%Y')\n",
        "import time\ndef f():\n    time.sleep(1)\n",
        "def scan(datetime):\n    return datetime.now()\n",                                  # 局部变量，没 import 绑定
        "def _now_et():\n    return 1\ndef scan():\n    return _now_et()\n",                 # 调缝本身不是读钟
    ])
    def test_probe_does_not_flag_look_alikes(self, src):
        assert _clock_reads(ast.parse(src)) == []

    def test_probe_attributes_the_read_to_the_outermost_function(self):
        src = ("from datetime import datetime\nclass C:\n    def m(self):\n        def inner():\n"
               "            return datetime.now()\n        return inner()\n")
        assert [o for o, _, _ in _clock_reads(ast.parse(src))] == ["m"]


# ───────────── 闸 2 ─────────────

class TestNoHandWrittenClockPins:
    def test_discovery_finds_the_known_scan_tests(self, scan_test_files):
        assert KNOWN_SCAN_TESTS <= set(scan_test_files), (
            f"自动圈漏了已知的文件：{sorted(KNOWN_SCAN_TESTS - set(scan_test_files))}——圈法坏了，下面的闸全不作数")

    def test_no_scan_test_pins_the_seams_by_hand(self, scan_test_files):
        bad = {n: _hand_written_pins(t) for n, t in scan_test_files.items() if _hand_written_pins(t)}
        assert bad == {}, f"这些文件手写了钉钟（行号）：{bad}——改用 tests/_scan_clock.pin_scan_clock"

    def test_every_scan_test_calls_the_shared_helper(self, scan_test_files):
        bad = sorted(n for n, t in scan_test_files.items() if not _uses_helper(t))
        assert bad == [], (f"这些文件在进程内跑 scan / main 却没有调用 pin_scan_clock / tick_scan_clock：{bad}——"
                           "不钉钟，跨秒 / 跨 PDT 午夜时结果会随真实时钟变")

    @pytest.mark.parametrize("src, lines", [
        ('monkeypatch.setattr(evs, "_now_et", lambda: X)\n', [1]),
        ('monkeypatch.setattr(evs, "_today_pdt", lambda: X)\n', [1]),                       # 只钉一只也算
        ('monkeypatch.setattr("earnings_vol_signal._now_et", lambda: X)\n', [1]),           # 字符串路径
        ('monkeypatch.setattr("earnings_vol_signal._today_pdt", lambda: X)\n', [1]),
        ('with mock.patch.object(evs, "_now_et", return_value=X):\n    pass\n', [1]),
        ('with mock.patch("earnings_vol_signal._now_et", return_value=X):\n    pass\n', [1]),
        ('monkeypatch.setitem(vars(evs), "_now_et", lambda: X)\n', [1]),
        ('setattr(evs, "_now_et", lambda: X)\n', [1]),
        # 不该报的
        ("pin_scan_clock(monkeypatch, evs)\n", []),
        ('monkeypatch.setattr(evs, "STATE_DIR", p)\n', []),
        ('x = getattr(evs, "_now_et")\n', []),
    ])
    def test_hand_pin_probe_has_teeth(self, src, lines):
        assert _hand_written_pins(ast.parse(src)) == lines

    def test_helper_use_probe_has_teeth(self):
        yes = ("from tests._scan_clock import pin_scan_clock\ndef f(m):\n    pin_scan_clock(m, 1)\n")
        also = "from tests import _scan_clock as SC\ndef f(m):\n    SC.tick_scan_clock(m, 1)\n"
        only_import = "from tests._scan_clock import pin_scan_clock\n# pin_scan_clock mentioned in a comment\n"
        assert _uses_helper(ast.parse(yes)) and _uses_helper(ast.parse(also))
        assert not _uses_helper(ast.parse(only_import))

    def test_scan_detection_probe_has_teeth(self):
        assert _calls_scan(ast.parse("import earnings_vol_signal as evs\ndef t():\n    evs.scan('x')\n"))
        assert _calls_scan(ast.parse("from earnings_vol_signal import scan as s\ndef t():\n    s('x')\n"))
        assert _calls_scan(ast.parse("import earnings_vol_signal\ndef t():\n    earnings_vol_signal.main(['x'])\n"))
        assert not _calls_scan(ast.parse("import earnings_vol_signal as evs\ndef t():\n    evs.compute_signal(1)\n"))
        assert not _calls_scan(ast.parse("def t(evs):\n    evs.scan('x')\n"))


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
        close, intraday = evs.session_live_at(SC.POST_CLOSE), evs.session_live_at(SC.INTRADAY)
        assert close is not None and intraday is not None, (
            "session_live_at 返回了 None = 交易日历读不了或已过期（is_trading_day / session_close_et 抛错），"
            "不是钉钟辅助的问题——先修日历（硬编码日历会过期）")
        assert close is False and intraday is True
        assert SC.POST_CLOSE.date().isoformat() == SC.SCAN_AS_OF == SC.INTRADAY.date().isoformat()

    def test_a_missing_calendar_is_what_turns_session_live_into_none(self, monkeypatch):
        """上一条失败话术的前提：日历抛错 ⇒ `session_live_at` 是 None（不是 True / False）。"""
        import is_trading_day as itd

        def boom(_d):
            raise RuntimeError("no calendar")
        monkeypatch.setattr(itd, "is_trading_day", boom)
        assert evs.session_live_at(SC.POST_CLOSE) is None and evs.session_live_at(SC.INTRADAY) is None
