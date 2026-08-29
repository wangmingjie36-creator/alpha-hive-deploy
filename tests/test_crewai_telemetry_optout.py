#!/usr/bin/env python3
"""crewai import 期埋点的回归闸（v0.45.73）

crewai 1.9.3 的 `crewai/__init__.py` 在模块顶层调 `_track_install_async()`，
起一个 daemon 线程 `Thread-1 (_track_install)` 给 `api.scarf.sh` 发一个像素点。
它不阻塞任何东西、也不是任何测试的依赖 —— 纯第三方 phone-home，且是一次
**不在 `http_gate` 覆盖范围内的出站 HTTPS**（发生在闸门自己还没被 import 的时刻）。
⚠️ 别把它和 2026-08-24 的 SSL EOF 风暴挂因果：那次「OpenSSL 1.1.1q 扛不住并发」的
归因 2026-08-25 已被重测证伪，`http_gate.py` 的 docstring 在这点上陈旧。

这里三条测试各守一件不同的事，别合并：

1. `test_installed_crewai_honours_our_env_names`
   —— 守「环境变量名有没有跟着 crewai 版本漂走」。
   断言的是 **crewai 自己的** `Telemetry._is_telemetry_disabled()` 返回 True，
   而不是断言我们写没写 os.environ（后者恒真，等于没测）。
   升级 crewai 后若它改了变量名，这条会红。
   ⚠️ 反例记一笔：网上流传最广的 `CREWAI_TELEMETRY_OPT_OUT` 在 1.9.3 里
   **全仓不存在** —— 设了它一切照旧，却看着像已经关掉了。

2. `test_importing_crewai_adapter_starts_no_tracking_thread`
   —— 守 `crewai_adapter.py` 顶部那几行 setdefault 的位置（必须在 import crewai 之前）。
   **子进程里显式把这三个变量从 env 里删掉**再跑：否则它会继承 conftest 设的值，
   于是不管 `crewai_adapter.py` 写没写都绿 —— 那就又是一条恒真断言。

3. `test_importing_daily_report_does_not_import_crewai`
   —— 守惰性导入。`run_crew_scan()` 全仓零调用方，默认路径不该把 crewai 拖进来。
"""

import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# crewai 1.9.3 `Telemetry._is_telemetry_disabled()` 实际读的全部名字。
# 子进程必须把它们清干净，才能证明「是被测代码自己设上的」。
_TELEMETRY_ENV_NAMES = (
    "CREWAI_DISABLE_TELEMETRY",
    "CREWAI_DISABLE_TRACKING",
    "OTEL_SDK_DISABLED",
)

crewai_installed = pytest.mark.skipif(
    importlib.util.find_spec("crewai") is None,
    reason="crewai 未安装（适配层本就会降级，无埋点可关）",
)


def _run_clean(code: str, *, probe: bool = False) -> subprocess.CompletedProcess:
    """在**清空了埋点环境变量**的子进程里跑一段代码。

    清空是这几条测试的全部意义所在 —— 继承父进程的 env 会让断言恒真。

    `probe=True` 时前置线程探针。注意必须**先 dedent 再拼接**：探针本身顶格写，
    和缩进的 code 拼起来后公共前缀是空串，dedent 就成了空操作。
    """
    body = textwrap.dedent(code)
    if probe:
        body = _PROBE_PREAMBLE + body
    env = dict(os.environ)
    for name in _TELEMETRY_ENV_NAMES:
        env.pop(name, None)
    env["PYTHONPATH"] = PROJECT_ROOT
    return subprocess.run(
        [sys.executable, "-c", body],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# 探针：在 import 之前替换 threading.Thread.start，记下 _track_install 有没有起来。
# 只看 socket 不够 —— 没网/代理拒连时它也不会连，那样测试会假绿。
_PROBE_PREAMBLE = """
import threading, sys
_hits = []
_orig = threading.Thread.start
def _patched(self):
    tgt = getattr(self, "_target", None)
    if getattr(tgt, "__name__", None) == "_track_install":
        _hits.append(getattr(tgt, "__module__", "?"))
    return _orig(self)
threading.Thread.start = _patched
"""


@crewai_installed
def test_installed_crewai_honours_our_env_names():
    """装着的这版 crewai 必须真的认我们设的变量名。"""
    result = _run_clean(
        """
        import os
        for k in ("CREWAI_DISABLE_TELEMETRY", "CREWAI_DISABLE_TRACKING"):
            os.environ[k] = "true"
        from crewai.telemetry.telemetry import Telemetry
        print("DISABLED=%s" % Telemetry._is_telemetry_disabled())
        """
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "DISABLED=True" in result.stdout, (
        "装着的 crewai 不认 CREWAI_DISABLE_TELEMETRY / CREWAI_DISABLE_TRACKING。"
        "多半是升级后改了变量名 —— 去读 "
        "site-packages/crewai/telemetry/telemetry.py::_is_telemetry_disabled，"
        "把新名字同步到 crewai_adapter.py / tests/conftest.py / "
        "~/.claude/scripts/alpha-hive-orchestrator.sh 三处。\n"
        f"stdout={result.stdout!r}"
    )


@crewai_installed
def test_importing_crewai_adapter_starts_no_tracking_thread():
    """import crewai_adapter 不得起 _track_install 线程。

    子进程里三个埋点变量都已被清掉 —— 绿的唯一原因只能是
    `crewai_adapter.py` 自己在 import crewai 之前设上了。
    """
    result = _run_clean(
        """
        import crewai_adapter
        print("CREWAI_AVAILABLE=%s" % crewai_adapter.CREWAI_AVAILABLE)
        print("TRACK_INSTALL_THREADS=%d" % len(_hits))
        """,
        probe=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "CREWAI_AVAILABLE=True" in result.stdout, (
        f"crewai 本应可用，适配层却报不可用：stdout={result.stdout!r}"
    )
    assert "TRACK_INSTALL_THREADS=0" in result.stdout, (
        "crewai 的 _track_install 埋点线程仍然起来了。"
        "检查 crewai_adapter.py 顶部的 os.environ.setdefault 是否被挪到了 "
        "`from crewai import ...` **之后** —— crewai 读的是 import 那一刻的 env，"
        "晚一行都无效。\n"
        f"stdout={result.stdout!r}"
    )


def test_importing_daily_report_does_not_import_crewai():
    """默认扫描路径不该把 crewai 拖进来（惰性导入回归闸）。

    `run_crew_scan()` 全仓零调用方；顶层 import 等于让每次 pytest / 每次生产
    扫描都白付一次出站 HTTPS + 约 0.6s 导入开销。
    """
    result = _run_clean(
        """
        import alpha_hive_daily_report  # noqa: F401
        print("CREWAI_IMPORTED=%s" % ("crewai" in sys.modules))
        print("TRACK_INSTALL_THREADS=%d" % len(_hits))
        """,
        probe=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "CREWAI_IMPORTED=False" in result.stdout, (
        "alpha_hive_daily_report 又在顶层把 crewai 拖进来了。"
        "crewai_adapter 的 import 必须留在 run_crew_scan() 内部。\n"
        f"stdout={result.stdout!r}"
    )
    assert "TRACK_INSTALL_THREADS=0" in result.stdout, (
        f"import 日报模块时起了埋点线程：stdout={result.stdout!r}"
    )
