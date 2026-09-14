"""元守卫：测试读随代码发布的文件，读的必须是**本检出**那份（v0.45.219 起，v0.45.222 迁出并补全）

为什么需要这条
--------------
v0.45.219：`test_thesis_break_schema.py` 用
`"/Users/igg/Desktop/Alpha Hive/thesis_breaks_config.json"` 读一个**被 git 跟踪**的配置。
换台机器它恒 skip；更糟的是在 worktree 里它是**绿的** —— 校验的是主 checkout 那份，
改动中的文件从未被检查（实测：往 worktree 配置注入第三种 schema，照绿）。
同版还修了 `open("thesis_breaks_config.json")`：跟着 cwd 走，换个 cwd 同一份坏配置就转绿。

`test_no_invisible_prod_data_skips.py` 抓不到这类：它按「文件被 git 忽略」定口径，
而这里文件处处都在，出问题的是**到达它的路径**。两者与 skip 也无关 —— 故单独成文件。

两个检测器
----------
1. `home_absolute_paths`：家目录下的路径 —— `/Users/…`、`/home/…`，以及
   `~/…` 与 `Path.home() / …` 两种写法（v0.45.222 补：本仓生产代码就是这么写主 checkout 的，
   `alpha_hive_mcp.py` 的 `Path.home() / "Desktop" / "Alpha Hive"`、
   `self_analyst.py` 等的 `expanduser("~/Desktop/Alpha Hive")`，测试照抄就漏）。
   放行家目录下**按用户存放应用状态**的地方：点目录（`~/.claude`）与 `~/Library` —— 仓库不住那里。
   豁免：标了 `integration` 的函数 / 类 / 模块（测的就是这台机器上的生产状态）。
2. `cwd_relative_reads`：`open("x")` / `Path("x")` 的第一个参数是相对路径字面量。
   v0.45.219 说「裸相对文件名合法用法太多，不做检测器」—— **那句没量过**。v0.45.222 实测
   全 tests/ 这种写法共 8 处：6 处是真 bug（本版已修），另 2 处是 `led.open("a")` 的**模式参数**、
   根本不是路径（方法调用，本检测器不看）。纯字符串路径运算请用 `PurePath`，它不碰文件系统。

已知边界（量过、刻意不管）
------------------------
- `os.path.join("/", "Users", …)` 把前缀拆开写、`os.path.join(os.path.expanduser("~"), "Desktop")`
  —— 全仓（含生产代码）零处。
- 测试 import 生产模块里写死主 checkout 的常量（如 `generate_deep_v2.ALPHAHIVE_DIR`）——
  字面量扫描看不见；现有用到它的测试都 monkeypatch 了。

⚠️ 检测器自己也必须有牙：正反两个方向各喂一次；变异见 v0.45.222 CHANGELOG。
"""

import ast
import re
from pathlib import Path

import pytest

from tests.test_no_invisible_prod_data_skips import _has_integration_mark, invisible_prod_skips

TESTS_DIR = Path(__file__).resolve().parent

_HOME_ABS = re.compile(r"^/(?:Users|home)(?:/|$)")
_HOME_TILDE = re.compile(r"^~/([^/]*)")


def _per_user_state(first_segment) -> bool:
    """家目录下按用户存放应用状态的地方：点目录与 macOS 的 `~/Library`。"""
    return first_segment.startswith(".") or first_segment == "Library"


def _is_home_call(node) -> bool:
    """`Path.home()` / `pathlib.Path.home()`。"""
    return (isinstance(node, ast.Call) and not node.args
            and isinstance(node.func, ast.Attribute) and node.func.attr == "home")


def _home_path(node):
    """节点若是指向家目录（非应用状态区）的路径，返回用于报错的文本，否则 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value
        if _HOME_ABS.match(v):
            return repr(v)
        m = _HOME_TILDE.match(v)
        if m and m.group(1) and not _per_user_state(m.group(1)):
            return repr(v)
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and _is_home_call(node.left)
            and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
        first = node.right.value.split("/")[0]
        if first and not _per_user_state(first):
            return f"Path.home() / {node.right.value!r}"
    return None


def _module_marked_integration(tree) -> bool:
    """模块级 `pytestmark` 里**有 `.integration` 这个 mark 节点**。

    v0.45.219 版判的是「源码片段里出现 integration 这个词」—— 于是
    `pytestmark = skipif(..., reason="integration 机才有")` 把整个模块豁免掉（实测）。
    与 CHANGELOG 里六次「冲突标记子串自检」同一个坑：子串分不清「是它」和「提到它」。
    """
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
                and any(isinstance(n, ast.Attribute) and n.attr == "integration"
                        for n in ast.walk(node.value))):
            return True
    return False


def home_absolute_paths(source: str, filename: str = "<test>") -> list[str]:
    """返回「未标 integration 的作用域里出现家目录路径」的位置列表。

    **不看有没有 skip**：没有 skip 时它在别的机器上红得很可见，但在 worktree 里绿得很安静。
    模块级常量不豁免 —— 要用就挪进标了 marker 的类里。
    """
    tree = ast.parse(source, filename=filename)
    if _module_marked_integration(tree):
        return []
    hits: list[str] = []

    def visit(node, where: str) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if _has_integration_mark(node.decorator_list):
                return          # 条件性写在 marker 上 —— 正确形状
            where = f"{where}{node.name}::"
        shown = _home_path(node)
        if shown:
            hits.append(
                f"{filename}:{node.lineno} {where.rstrip(':') or '<module>'} "
                f"写死了家目录路径 {shown} —— 换台机器恒缺、在 worktree 里读的是主 checkout。"
                "随代码发布的文件锚 `Path(__file__)`；真要测本机生产状态就标 "
                "@pytest.mark.integration；合成路径改用 tmp_path 或 /nonexistent/ 前缀"
            )
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(tree, "")
    return hits


def cwd_relative_reads(source: str, filename: str = "<test>") -> list[str]:
    """返回 `open(<相对路径字面量>)` / `Path(…)` / `pathlib.Path(…)` / `io.open(…)` 的位置。

    只看**函数调用**的第一个参数：`led.open("a")` 这种方法调用的实参是模式、不是路径，不看。
    """
    tree = ast.parse(source, filename=filename)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        fn = node.func
        is_opener = (
            (isinstance(fn, ast.Name) and fn.id in {"open", "Path"})
            or (isinstance(fn, ast.Attribute) and fn.attr in {"open", "Path"}
                and isinstance(fn.value, ast.Name) and fn.value.id in {"pathlib", "io"})
        )
        arg = node.args[0]
        if not (is_opener and isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        v = arg.value
        if v and not v.startswith(("/", "~")) and "\n" not in v:
            hits.append(
                f"{filename}:{node.lineno} {ast.get_source_segment(source, node) or v!r} "
                "是 cwd 相对路径 —— 从别处起 pytest 就读别处那份（或直接找不到）。"
                "随代码发布的文件用 `Path(__file__).resolve().parent.parent / …`；"
                "测试自己造的文件用 tmp_path；只做字符串运算用 PurePath"
            )
    return hits


def _scan(detector):
    offenders: list[str] = []
    scanned = 0
    # 含 conftest.py / _repo_files.py（写在那里连坐全套），也含本文件 —— 合成样例都在字符串里
    for f in sorted(TESTS_DIR.glob("*.py")):
        scanned += 1
        offenders += detector(f.read_text(encoding="utf-8"), f.name)
    return scanned, offenders


class TestNoTestReadsAnotherCheckout:
    def test_no_home_directory_paths(self):
        scanned, offenders = _scan(home_absolute_paths)
        assert scanned > 50, f"只扫到 {scanned} 个模块 —— glob 可能写错了"
        assert not offenders, "\n  ".join(["以下测试写死了家目录路径："] + offenders)

    def test_no_cwd_relative_reads(self):
        scanned, offenders = _scan(cwd_relative_reads)
        assert scanned > 50, f"只扫到 {scanned} 个模块 —— glob 可能写错了"
        assert not offenders, "\n  ".join(["以下测试按 cwd 相对路径读文件："] + offenders)


class TestHomePathDetectorHasTeeth:
    """正反各喂一次。BAD_SKIP 是 v0.45.219 修掉的原文形状；带 v0.45.222 的三条是二次检查漏网的。"""

    BAD = {
        "skip": (
            "import os, pytest\n"
            "def test_real_config():\n"
            "    p = os.path.join('/Users/igg/Desktop/Alpha Hive', 'cfg.json')\n"
            "    if not os.path.exists(p):\n"
            "        pytest.skip('生产配置不可得')\n"
            "    assert p\n"
        ),
        "no_skip": (
            "def test_reads_main_checkout():\n"
            "    assert open('/Users/igg/Desktop/Alpha Hive/cfg.json').read()\n"
        ),
        "module_const": (
            "import pytest\n"
            "ROOT = '/home/ci/alpha-hive'\n"
            "@pytest.mark.integration\n"
            "class TestX:\n"
            "    def test_a(self):\n"
            "        assert ROOT\n"
        ),
        "tilde_v0.45.222": (
            "import os\n"
            "def test_a():\n"
            "    assert os.path.exists(os.path.expanduser('~/Desktop/Alpha Hive/cfg.json'))\n"
        ),
        "path_home_v0.45.222": (
            "from pathlib import Path\n"
            "def test_a():\n"
            "    assert (Path.home() / 'Desktop' / 'Alpha Hive' / 'cfg.json').exists()\n"
        ),
        "reason_mentions_integration_v0.45.222": (
            "import pytest, sys\n"
            "pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='integration 机才有')\n"
            "def test_a():\n"
            "    assert open('/Users/igg/Desktop/Alpha Hive/cfg.json')\n"
        ),
    }
    GOOD = {
        "integration_class": (
            "import pytest\n"
            "@pytest.mark.integration\n"
            "class TestLive:\n"
            "    def test_a(self):\n"
            "        assert '/Users/igg/Desktop/Alpha Hive'\n"
        ),
        "integration_module": (
            "import pytest\n"
            "pytestmark = [pytest.mark.integration]\n"
            "def test_a():\n"
            "    assert '/Users/igg/Desktop/Alpha Hive'\n"
        ),
        "per_user_state": (   # 本仓测试里真实出现过的四种仓库外写法
            "import os, pathlib\n"
            "from pathlib import Path\n"
            "ORCH = os.path.expanduser('~/.claude/scripts/alpha-hive-orchestrator.sh')\n"
            "PLIST = os.path.expanduser('~/Library/LaunchAgents/x.plist')\n"
            "A = Path.home() / '.claude' / 'scripts'\n"
            "B = pathlib.Path.home() / '.claude/scripts/x.sh'\n"
            "def test_a(today):\n"
            "    assert os.path.expanduser(f'~/.claude/logs/orchestrator-{today}.log')\n"
        ),
        "not_home": (
            "import os\n"
            "from pathlib import Path\n"
            "CFG = Path(__file__).resolve().parent.parent / 'cfg.json'\n"
            "def test_a():\n"
            "    '''docstring 里提到 /Users/igg/Desktop/Alpha Hive 不算。'''\n"
            "    assert os.path.basename('/nonexistent/Users/x.json') == 'x.json'\n"
        ),
    }

    @pytest.mark.parametrize("name", sorted(BAD))
    def test_catches(self, name):
        assert home_absolute_paths(self.BAD[name], "bad.py"), name

    @pytest.mark.parametrize("name", sorted(GOOD))
    def test_does_not_flag(self, name):
        assert home_absolute_paths(self.GOOD[name], "good.py") == [], name

    def test_skip_detector_really_misses_it(self):
        """钉住「为什么要另一个检测器」：按 git 忽略定口径的那个，对这段原文确实不响。"""
        assert invisible_prod_skips(self.BAD["skip"], "bad.py") == []


class TestCwdDetectorHasTeeth:
    BAD = {
        "open_v0.45.219": "import json\ndef test_a():\n    json.load(open('thesis_breaks_config.json'))\n",
        "pathlib_Path": "import pathlib\ndef test_a():\n    pathlib.Path('dashboard_renderer.py').read_text()\n",
        "Path_subdir": "from pathlib import Path\ndef test_a():\n    Path('templates/dashboard.js').read_text()\n",
        "Path_dot": "from pathlib import Path\ndef test_a():\n    list(Path('.').glob('.swarm_results_*'))\n",
        "io_open": "import io\ndef test_a():\n    io.open('config.py').read()\n",
    }
    GOOD = {
        "method_mode_arg": "def test_a(led):\n    led.open('a', encoding='utf-8')\n",
        "tmp_path": "import json\ndef test_a(tmp_path):\n    json.load(open(tmp_path / 'x.json'))\n",
        "file_anchor": (
            "from pathlib import Path\n"
            "def test_a():\n"
            "    (Path(__file__).resolve().parent.parent / 'templates' / 'dashboard.js').read_text()\n"
        ),
        "variable": "def test_a(p):\n    open(p).read()\n",
        "purepath_string_math": (
            "from pathlib import PurePosixPath\n"
            "def test_a():\n"
            "    assert PurePosixPath('templates/dashboard.js').name == 'dashboard.js'\n"
        ),
    }

    @pytest.mark.parametrize("name", sorted(BAD))
    def test_catches(self, name):
        assert cwd_relative_reads(self.BAD[name], "bad.py"), name

    @pytest.mark.parametrize("name", sorted(GOOD))
    def test_does_not_flag(self, name):
        assert cwd_relative_reads(self.GOOD[name], "good.py") == [], name
