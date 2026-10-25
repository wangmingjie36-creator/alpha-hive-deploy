"""财报信号 `scan` 的运行时钟断言——静态闸（`tests/test_scan_clock.py`）看不到的间接调用由它兜（v0.45.651）。

为什么要有它：`tests/test_scan_clock.py` 的闸 2 用 AST 推断「哪些测试会跑 scan、有没有钉钟」，推不到的写法它就看不见：
`getattr(importlib.import_module(name), "scan")`、别的测试模块里的 helper、日报路径里间接调用 scan……
v0.45.650 的运行时探针在 44 次 scan 调用里就查到 1 次没钉钟（`test_ledger_io_v2` 按参数化的模块名 `getattr` 调 scan）。
这里不推断，**直接看调用那一刻**：`earnings_vol_signal._scan_with_info`（`scan` / `main` 都经过它）被包一层，
进入时若 `_now_et` / `_today_pdt` 仍是模块原来的那两个函数，就记一条违规；每条测试的 teardown 里有违规就 ERROR。

两个设计点：
1. **记录 + teardown 失败，不在调用处抛**：调用方可能吞异常（日报路径、`try/except Exception: pass`），抛了也红不了；
   记在模块级列表里、由 teardown 统一判，吞不掉。
2. **不改 scan 的行为**：包装只读两只钟的身份，原样转发；`functools.wraps` 带上 `locked_by` 留下的 `__ledger_locked__`
   （`test_ledger_io_v2` 在断言它）。

真需要读真实时钟的测试（例如核对 `captured_at` 离现在不超过几秒）请求夹具 `allow_real_scan_clock`，这是显式的、grep 得到的例外；
其余一律用 `tests/_scan_clock.py` 钉住。夹具在 `tests/conftest.py` 里 import 进来（autouse 的会话级安装 + 函数级断言）。
"""
from __future__ import annotations

import functools

import pytest

#: 不钉就读真实时钟的两只钟（与 `tests/_scan_clock.py::pin_scan_clock` 钉的一致）
SEAMS = ("_now_et", "_today_pdt")
MARK = "__scan_clock_guard__"
_VIOLATIONS: list = []


def install() -> None:
    """把 `earnings_vol_signal._scan_with_info` 包一层（幂等）。必须在收集之后、测试之前调用——
    模块在收集期已被各测试文件 import 过，这里 import 的是同一份；没人 import 过时也是在会话沙箱环境里，和收集期等价。"""
    import earnings_vol_signal as evs
    real = evs._scan_with_info
    if getattr(real, MARK, False):
        return
    original = {name: getattr(evs, name) for name in SEAMS}

    @functools.wraps(real)
    def guarded(*args, **kwargs):
        unpinned = [name for name, fn in original.items() if getattr(evs, name) is fn]
        if unpinned:
            _VIOLATIONS.append(unpinned)
        return real(*args, **kwargs)

    setattr(guarded, MARK, True)
    evs._scan_with_info = guarded


def violations() -> list:
    """本条测试到目前为止的违规（每条是「没钉的钟名」列表）。供守卫自己的测试读。"""
    return [list(v) for v in _VIOLATIONS]


def clear() -> None:
    del _VIOLATIONS[:]


def message(nodeid: str) -> str:
    seams = sorted({name for v in _VIOLATIONS for name in v})
    return (f"{nodeid}：earnings_vol_signal 的 scan / main 在没钉钟的状态下被调用了 {len(_VIOLATIONS)} 次"
            f"（没钉的钟：{', '.join(seams)}）。结果会随跑测试的真实时钟变——撞秒或 PDT 午夜时偶发红。"
            "用 tests/_scan_clock.pin_scan_clock(monkeypatch, evs) 钉住；真需要读真实时钟就请求夹具 allow_real_scan_clock。")


@pytest.fixture(autouse=True, scope="session")
def _scan_clock_guard_installed():
    install()
    yield


@pytest.fixture
def allow_real_scan_clock():
    """显式例外：这条测试要读真实时钟，运行时钟断言不判它。"""
    return True


@pytest.fixture(autouse=True)
def _scan_clock_must_be_pinned(request, _scan_clock_guard_installed):
    clear()
    yield
    found, nodeid = bool(_VIOLATIONS), request.node.nodeid
    text = message(nodeid) if found else ""
    clear()
    if found and "allow_real_scan_clock" not in request.fixturenames:
        pytest.fail(text, pytrace=False)
