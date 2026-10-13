"""测试里 `env -i` 起的子进程必须自带数据根（v0.45.483）。

`env -i` 把 `ALPHA_HIVE_HOME` 与 `HOME` 一起剥掉。`hive_logger.resolve_data_root` 先看前者，再取
`expanduser("~")`——没有 HOME 时 Python 查用户库 ⇒ `/Users/<你>/alpha-hive-data` = **真实生产数据根**。
conftest 在 `pytest_configure` 设的会话沙箱同样被剥掉，而测试察觉不到：子进程碰不碰数据，全看被测代码
碰巧走哪条路。2026-10-13 在沙箱（`sandbox-exec`，写数据根 / 读 `pheromone.db*` 全拒）下实测
`TestMacBash` 的子进程从不打开数据根——那是**走运**（`orchestrator_steps` 在这几条路上只读显式给的 JSON），
不是隔离；v0.45.422 记的「剥 ALPHA_HIVE_* 的都显式设了或把 HOME 指空目录」对它不成立。

判据：测试里每一处 `env -i`（及同义的 `--ignore-environment`、单独的 `-`）都要显式带 `ALPHA_HIVE_HOME=`
或 `HOME=`（后者让缺省落到你给的家目录）。生产 launchd 跑的编排器本来就 export ALPHA_HIVE_HOME（plist + 编排器顶部），
所以带上它不是削弱测试，是在数据根上与生产同形。⚠️ 这里只核「有没有」，**不核值**——`HOME=<真家目录>` 照样放行；
值由用的那条测试自己断言（`TestMacBash` 在派生前用 `resolve_data_root` 求值、断言落在 tmp_path）。

只认字面写法：
  · 参数表字面量 `["/usr/bin/env", "-i", ...]`（本仓实际用的形状）——其后元素里要有以上前缀之一；
  · 命令串 `"env -i PATH=… cmd"`——认「`env -i` 后跟环境赋值」的串，避免把说明文字里的「env -i」当成命令。
把 `env -i` 拆进变量再拼接的写法、`env -i` 后不跟赋值的命令串都扫不到；扫不到的那部分只剩会话级真实数据根闸兜底
（只管写，且 `logs/` 豁免）。另：清单经 `git ls-files` 取，**未提交的新测试文件扫不到**（含本文件自己，见下）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Optional, Tuple

from tests._repo_files import own_python_files

REPO = Path(__file__).resolve().parent.parent          # 指向**代码**（扫仓库里的测试源码），故用 __file__

_ENV_EXE = {"/usr/bin/env", "env"}
#: `env` 清空环境的三种写法（`-` 是 POSIX 旧式，与 `-i` 同义）
_CLEAR = {"-i", "--ignore-environment", "-"}
_PINS = ("ALPHA_HIVE_HOME=", "HOME=")
#: 命令串里的 `env -i`：其后紧跟一个 `NAME=` 赋值（说明文字里的「env -i 子进程」「env -i children」都不算）
_SHELL_ENV_I = re.compile(r"(?:^|[\s;&|(`])(?:/usr/bin/)?env\s+(?:-i|--ignore-environment|-)\s+[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_PIN = re.compile(r"(?:^|\s)(?:ALPHA_HIVE_)?HOME=")

#: 已知会被扫到的那一处：守卫若一处都扫不到，就是自己瞎了（扫描形状与写法脱节），不是「没有问题」
KNOWN_SITE = "tests/test_orchestrator_step_interp.py"


def _lead(node: ast.AST) -> Optional[str]:
    """字符串常量 ⇒ 原文；f-string ⇒ 第一段字面常量（`f"HOME={x}"` ⇒ `"HOME="`）；其余 ⇒ None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return str(node.values[0].value)
    return None


def _flat(node: ast.AST) -> Optional[str]:
    """字符串常量 / f-string 拼成一串（插值处用占位符），给命令串判据用。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def scan(source: str) -> Tuple[List[int], List[int]]:
    """返回 (`env -i` 出现处的行号, 其中没带数据根的行号)。"""
    tree = ast.parse(source)
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, (ast.Constant, ast.JoinedStr))}
    inside_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    sites: List[int] = []
    bad: List[int] = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.List, ast.Tuple)) and len(n.elts) >= 2:
            if _lead(n.elts[0]) in _ENV_EXE and _lead(n.elts[1]) in _CLEAR:
                sites.append(n.lineno)
                if not any((_lead(e) or "").startswith(_PINS) for e in n.elts[2:]):
                    bad.append(n.lineno)
        elif id(n) not in docstrings and id(n) not in inside_fstring:
            s = _flat(n)
            if s is not None and _SHELL_ENV_I.search(s):
                sites.append(n.lineno)
                if not _SHELL_PIN.search(s):
                    bad.append(n.lineno)
    return sorted(sites), sorted(bad)


def _test_sources() -> List[Path]:
    files, _how = own_python_files(REPO)
    return [p for p in files if p.relative_to(REPO).parts[0] == "tests"]


class TestScannerHasTeeth:
    """判据本身：缺数据根要红、带了要放行、说明文字不算命令。样本在运行时拼（字面量会被本守卫扫到自己）。

    ⚠️ 别写成 `" ".join(("env", "-i"))`：那个元组本身就是守卫要找的参数表形状（首版这么写，文件一被 git 跟踪就扫到自己）。
    `ast.parse` 不折叠常量，`"env" + " -i"` 是两个各自无害的常量。"""

    ENV_I = "env" + " -i"

    def test_argv_literal_without_pin_is_flagged(self):
        src = 'import subprocess\nsubprocess.run(["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "python3", "x.py"])\n'
        assert scan(src) == ([2], [2])

    def test_argv_literal_with_pin_passes(self):
        for pin in ('"ALPHA_HIVE_HOME=/tmp/x"', 'f"ALPHA_HIVE_HOME={d}"', 'f"HOME={d}"'):
            src = f'P = ["/usr/bin/env", "-i", "PATH=/usr/bin", {pin}, "python3"]\n'
            assert scan(src) == ([1], []), pin

    def test_pin_must_be_an_assignment_not_any_mention(self):
        src = 'P = ["env", "-i", "PATH=/usr/bin", "echo", "ALPHA_HIVE_HOME"]\n'
        assert scan(src) == ([1], [1])

    def test_shell_string_without_pin_is_flagged(self):
        src = f'CMD = "{self.ENV_I} PATH=/usr/bin:/bin /usr/local/bin/python3 x.py"\n'
        assert scan(src) == ([1], [1])

    def test_shell_fstring_with_pin_passes(self):
        src = f'CMD = f"{self.ENV_I} PATH=/usr/bin HOME={{h}} python3 x.py"\n'
        assert scan(src) == ([1], [])

    def test_ignore_environment_synonyms_are_caught(self):
        for flag in ('"--ignore-environment"', '"-"'):
            src = f'P = ["env", {flag}, "PATH=/usr/bin", "python3"]\n'
            assert scan(src) == ([1], [1]), flag
        for flag in ("--ignore-environment", "-"):
            src = f'CMD = "env {flag} PATH=/usr/bin /usr/local/bin/python3 x.py"\n'
            assert scan(src) == ([1], [1]), flag

    def test_prose_mentioning_env_i_is_not_a_command(self):
        src = (f'"""{self.ENV_I} PATH=/usr/bin 是 docstring"""\n'
               f'MSG = f"{self.ENV_I} 子进程的数据根解析到 {{root}}"\n'
               f'MSG2 = "{self.ENV_I} children resolve the real root"\n')
        assert scan(src) == ([], [])


class TestNoBareEnvChildrenInTests:
    def test_every_env_i_in_tests_pins_the_data_root(self):
        offenders, n_sites, known_seen = [], 0, False
        for p in _test_sources():
            try:
                sites, bad = scan(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue                     # 语法错误由收集期报，不归这里
            rel = p.relative_to(REPO).as_posix()
            n_sites += len(sites)
            known_seen = known_seen or (rel == KNOWN_SITE and bool(sites))
            offenders += [f"{rel}:{ln}" for ln in bad]
        assert known_seen, (f"在 {KNOWN_SITE} 里一处 env -i 都没扫到（共扫到 {n_sites} 处）——"
                            "那里的写法变了而本守卫没跟上，它此刻是瞎的；改 scan() 或更新 KNOWN_SITE")
        assert not offenders, (
            "这些 env -i 子进程没带 ALPHA_HIVE_HOME= / HOME=：env -i 把会话沙箱一并剥掉，"
            "数据根会解析到真实生产 ~/alpha-hive-data：\n  " + "\n  ".join(offenders))
