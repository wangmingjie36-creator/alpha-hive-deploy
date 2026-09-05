"""
静态守卫：模块作用域不得引用未定义的名字（v0.43.5）

## 为什么单独立一个测试

2026-07-30 一天之内连续犯了 **3 次**同型错误：

| # | 文件 | 缺失的名字 | 触发路径 |
|---|---|---|---|
| 1 | `alpha_hive_daily_report.py` | `sys` | 线程卡死后的强制退出 |
| 2 | `weekly_optimizer.py` | `_log` | 权重约束无解时的拒绝写入分支 |
| 3 | `report_deployer.py` | `List`（只导了 `Dict`） | 模块级类型标注 |

共同特征：**名字缺失在模块作用域，而引用它的代码在正常路径上不执行**。
- `import` 不报 —— 函数体不求值
- 单元测试不报 —— 这些是异常/边界分支，覆盖不到
- 人工 review 不报 —— `from typing import Dict` 看起来完全正常

后来还用同一方法扫出 **4 个存量**：`alpha_hive_daily_report` 两处
`except` 里调用 `logging.getLogger()` 而模块**根本没 import logging**
（异常处理路径里再抛 NameError），以及 `factor_attribution._build_summary`
里 `r2_str`/`ir_str` 从未定义（重构漏改，factors 为空时必崩）。

## 结论

这类 bug **唯一可靠的发现方式不是"实际执行到那行"，而是静态未定义名检查**。
ruff 的 F821 正是为此设计，且本仓库早已装好并配置了 ruff —— 只是没人跑。
所以把它接进 pytest：跟着现有工作流走，不需要新习惯。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# 跑 ruff 用**当前正在跑测试的那个解释器**，不写死路径。
#
# 原先硬编码 `/usr/local/bin/python3`（CLAUDE.md 的 Mac 硬规则）。在本机两者
# 完全等价——按规矩执行 `/usr/local/bin/python3 -m pytest` 时 `sys.executable`
# 就是它——但 GitHub runner 上 Python 在 `/opt/hostedtoolcache/...`，
# 那个路径不存在，`subprocess.run` 抛 FileNotFoundError，7 项全部 ERROR。
#
# 注意这**不是** fixture 里那句 `pytest.skip("ruff 不可用")` 能兜住的：
# 它只看 returncode，而解释器不存在时根本走不到 returncode。
# v0.45.117 把 CI 接上后首次运行即暴露（2634 passed / 7 errors）。
PY = sys.executable


def _run_ruff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "ruff", "check", "--no-cache", "--output-format=concise",
         *args, str(REPO)],
        capture_output=True, text=True, timeout=120,
    )


@pytest.fixture(scope="module")
def ruff_available():
    r = subprocess.run([PY, "-m", "ruff", "--version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("ruff 不可用")
    return True


def test_no_undefined_names_anywhere(ruff_available):
    """F821：全仓库不得有未定义名。

    这是本 session 三次事故 + 四个存量问题的统一防线。
    失败时输出里直接带 文件:行:列，照着修即可。
    """
    r = _run_ruff("--select", "F821")
    assert r.returncode == 0, (
        "发现未定义名（NameError 隐患，多在异常/边界分支上，"
        "正常路径与单元测试都覆盖不到）：\n\n" + (r.stdout or r.stderr)
    )


def test_no_undefined_local_before_assignment(ruff_available):
    """F823：局部变量在赋值前被使用 —— 与 F821 同族的另一种 NameError"""
    r = _run_ruff("--select", "F823")
    assert r.returncode == 0, (r.stdout or r.stderr)


def test_no_fstring_placeholder_mistakes(ruff_available):
    """F502/F506/F522 等 % 与 .format 的参数错配 —— 同样只在执行到才崩"""
    r = _run_ruff("--select", "F501,F502,F506,F522,F524")
    assert r.returncode == 0, (r.stdout or r.stderr)


class TestGuardItselfWorks:
    """守卫必须真的能抓到这三类错误 —— 否则它只是个永远绿的摆设"""

    def _check_snippet(self, tmp_path: Path, code: str) -> str:
        f = tmp_path / "snippet.py"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run(
            [PY, "-m", "ruff", "check", "--no-cache", "--select", "F821",
             "--output-format=concise", str(f)],
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout

    def test_catches_missing_logger(self, tmp_path, ruff_available):
        """复原事故 #2：weekly_optimizer 的 `_log` 从未定义"""
        out = self._check_snippet(tmp_path, (
            "def main():\n"
            "    try:\n"
            "        raise ValueError('x')\n"
            "    except ValueError as e:\n"
            "        _log.error('失败: %s', e)\n"
        ))
        assert "F821" in out and "_log" in out

    def test_catches_missing_typing_import(self, tmp_path, ruff_available):
        """复原事故 #3：report_deployer 只导了 Dict 却用了 List"""
        out = self._check_snippet(tmp_path, (
            "from typing import Dict\n"
            "PATHS: List[str] = ['a']\n"
            "def f(d: Dict) -> None: ...\n"
        ))
        assert "F821" in out and "List" in out

    def test_catches_missing_stdlib_import(self, tmp_path, ruff_available):
        """复原事故 #1：alpha_hive_daily_report 的 `sys` 未在模块作用域"""
        out = self._check_snippet(tmp_path, (
            "import os\n"
            "def _force_exit():\n"
            "    sys.stdout.flush()\n"
            "    os._exit(0)\n"
        ))
        assert "F821" in out and "sys" in out

    def test_clean_code_passes(self, tmp_path, ruff_available):
        """反向：正确代码不得误报，否则守卫会被噪音淹没而遭忽略"""
        out = self._check_snippet(tmp_path, (
            "import logging\n"
            "from typing import Dict, List\n"
            "_log = logging.getLogger('x')\n"
            "PATHS: List[str] = ['a']\n"
            "def f(d: Dict) -> None:\n"
            "    _log.info('%s', d)\n"
        ))
        assert "F821" not in out


def test_ruff_is_configured_in_pyproject():
    """配置必须在版本控制里，否则守卫在别的机器上行为不同"""
    cfg = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff]" in cfg
    assert '"F"' in cfg or "'F'" in cfg, "ruff lint select 必须包含 F 系列"
    # F821/F823 不得被 ignore 掉
    ignore_block = cfg.split("ignore = [")[1].split("]")[0] if "ignore = [" in cfg else ""
    for code in ("F821", "F823"):
        assert code not in ignore_block, f"{code} 不得被 ignore —— 它是 NameError 的唯一静态防线"
