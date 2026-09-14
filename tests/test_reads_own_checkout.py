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
   放行家目录下**按用户存放应用状态**的地方：点目录（`~/.claude`）与 `~/Library`，
   但 **`~/Library/Mobile Documents` 与 `~/Library/CloudStorage` 除外**（v0.45.224）——
   v0.45.222 写的是「`~/Library` 仓库不住那里」，本机实测
   `~/Library/Mobile Documents/com~apple~CloudDocs/Desktop` 就是指向 `~/Desktop` 的符号链接，
   `samefile` 判定它**就是**主 checkout（项目在 iCloud 桌面上）。`Path.home() / …` 按整条链判，不只看第一段。
   豁免：标了 `integration` 的函数 / 类 / 模块（测的就是这台机器上的生产状态）。
2. `cwd_relative_reads`：`open("x")` / `Path("x")` 的第一个参数是相对路径字面量。
   v0.45.219 说「裸相对文件名合法用法太多，不做检测器」—— **那句没量过**。v0.45.222 实测
   全 tests/ 这种写法共 8 处：6 处是真 bug（本版已修），另 2 处是 `led.open("a")` 的**模式参数**、
   根本不是路径（方法调用，本检测器不看）。纯字符串路径运算请用 `PurePath`，它不碰文件系统。

已知边界（量过、刻意不管）
------------------------
- `os.path.join("/", "Users", …)` 把前缀拆开写、`os.path.join(os.path.expanduser("~"), "Desktop")`
  —— 全仓（含生产代码）零处。
- 测试 import 生产模块里写死主 checkout 的常量（如 `weekly_optimizer.ALPHAHIVE_DIR`）——
  字面量扫描看不见。v0.45.222 写「现有用到它的测试都 monkeypatch 了」，只 grep 了常量名。
  v0.45.224 用审计钩子按**后果**量：进程内读写主 checkout 的只有 import 期一次 `scandir`；
  但 `weekly_optimizer` 把主 checkout **插进了 `sys.path`**（全套 40 次、从不拿掉），
  此后函数体内 import 的模块会从主 checkout 加载 —— 由 conftest `_isolate_cwd_and_sys_path` 挡住。
- 变量持有家目录再拼（`h = Path.home(); h / "Desktop"`）、`Path.home().joinpath(…)` —— 全仓零处。

⚠️ 检测器自己也必须有牙：正反两个方向各喂一次；变异见 v0.45.222 CHANGELOG。
"""

import ast
import os
import re
import sys
from pathlib import Path

import pytest

from tests.test_no_invisible_prod_data_skips import _has_integration_mark, invisible_prod_skips

TESTS_DIR = Path(__file__).resolve().parent

_HOME_ABS = re.compile(r"^/(?:Users|home)(?:/|$)")
_HOME_TILDE = re.compile(r"^~/(.*)$")
# `~/Library` 下挂着网盘根：iCloud Drive（含「桌面与文稿」同步）与 File Provider 网盘（Dropbox 等）。
_SYNC_ROOTS_UNDER_LIBRARY = {"Mobile Documents", "CloudStorage"}


def _per_user_state(segments) -> bool:
    """家目录下按用户存放应用状态的地方：点目录，与 `~/Library` 里**不是网盘根**的部分。"""
    if not segments:
        return True
    if segments[0].startswith("."):
        return True
    return segments[0] == "Library" and (len(segments) < 2 or segments[1] not in _SYNC_ROOTS_UNDER_LIBRARY)


def _segments(text):
    return [seg for seg in text.split("/") if seg]


def _is_home_call(node) -> bool:
    """`Path.home()` / `pathlib.Path.home()`。"""
    return (isinstance(node, ast.Call) and not node.args
            and isinstance(node.func, ast.Attribute) and node.func.attr == "home")


def _home_div_segments(node):
    """`Path.home() / "a" / "b/c" / name` → `["a", "b", "c"]`（取到第一个非字面量为止）；不是这种链 ⇒ None。"""
    rights = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        rights.append(node.right)
        node = node.left
    if not (rights and _is_home_call(node)):
        return None
    segs = []
    for r in reversed(rights):
        if not (isinstance(r, ast.Constant) and isinstance(r.value, str)):
            break
        segs += _segments(r.value)
    return segs


def _home_path(node):
    """节点若是指向家目录（非应用状态区）的路径，返回用于报错的文本，否则 None。

    `/` 链只在**最外层**那个 BinOp 上判（由 `visit` 保证）：v0.45.222 逐个 BinOp 看第一段，
    `Path.home() / "Library" / "Mobile Documents"` 的内层只看得到 `"Library"`，放行。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        v = node.value
        if _HOME_ABS.match(v):
            return repr(v)
        m = _HOME_TILDE.match(v)
        if m and _segments(m.group(1)) and not _per_user_state(_segments(m.group(1))):
            return repr(v)
    segs = _home_div_segments(node)
    if segs and not _per_user_state(segs):
        return "Path.home() / " + repr("/".join(segs))
    return None


def _module_marked_integration(tree) -> bool:
    """模块级 `pytestmark` 里**有 `.integration` 这个 mark 节点**。

    v0.45.219 版判的是「源码片段里出现 integration 这个词」—— 于是
    `pytestmark = skipif(..., reason="integration 机才有")` 把整个模块豁免掉（实测）。
    与 CHANGELOG 里多次记过的「冲突标记子串自检」同一个坑：子串分不清「是它」和「提到它」。
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

    def visit(node, where: str, inner_of_div_chain: bool = False) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if _has_integration_mark(node.decorator_list):
                return          # 条件性写在 marker 上 —— 正确形状
            where = f"{where}{node.name}::"
        is_div = isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        shown = None if (is_div and inner_of_div_chain) else _home_path(node)
        if shown:
            hits.append(
                f"{filename}:{node.lineno} {where.rstrip(':') or '<module>'} "
                f"写死了家目录路径 {shown} —— 换台机器恒缺、在 worktree 里读的是主 checkout。"
                "随代码发布的文件锚 `Path(__file__)`；真要测本机生产状态就标 "
                "@pytest.mark.integration；合成路径改用 tmp_path 或 /nonexistent/ 前缀"
            )
        for child in ast.iter_child_nodes(node):
            visit(child, where, inner_of_div_chain=is_div and child is node.left)

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
        # v0.45.224：本机 ~/Library/Mobile Documents/com~apple~CloudDocs/Desktop 就是 ~/Desktop
        "tilde_icloud_v0.45.224": (
            "import os\n"
            "def test_a():\n"
            "    assert os.path.exists(os.path.expanduser("
            "'~/Library/Mobile Documents/com~apple~CloudDocs/Desktop/Alpha Hive/cfg.json'))\n"
        ),
        "path_home_chain_icloud_v0.45.224": (
            "from pathlib import Path\n"
            "def test_a():\n"
            "    assert (Path.home() / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs').exists()\n"
        ),
        "path_home_then_variable": (
            "from pathlib import Path\n"
            "def test_a(name):\n"
            "    assert (Path.home() / 'Desktop' / name).exists()\n"
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
            "C = Path.home() / 'Library' / 'LaunchAgents' / 'x.plist'\n"
            "D = os.path.expanduser('~/Library/Application Support/x')\n"
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

    def test_div_chain_reported_once(self):
        """整条链只报一次 —— 内层 BinOp 不许重复报。"""
        assert len(home_absolute_paths(self.BAD["path_home_v0.45.222"], "bad.py")) == 1

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


_LEAK = {"planted": False}
_SENTINEL = "/nonexistent/v0.45.224-sys-path-sentinel"


class TestProcessStateStaysPut:
    """收集期不许把进程的 cwd / sys.path 挪走（v0.45.224）。

    实测 `test_deep_analysis_prefetch_injection.py` 收集期 `import deep_analysis` ⇒ cwd 从调用目录
    变成仓库根、sys.path 多一个 `"."`。此后全套都在仓库根跑：cwd 相对的 bug 一律被掩盖，
    「从空目录跑全套」的普查（v0.45.224 自己第一轮就是这么做的）结果与仓库根一模一样 —— 量的是假的。
    只在**收集到会挪它的模块**时有区分力（全套即是）；单跑本文件恒绿是口径，不是漏洞。
    ⚠️ cwd 那条从**仓库根**起 pytest 时是瞎的（chdir 到仓库根 = 没挪），实测撤掉隔离只有
    sys.path 那条红；从别的目录起两条都红。两条缺一不可。
    """

    def test_collection_did_not_move_cwd(self, request):
        after = getattr(request.config, "_alpha_hive_cwd_after_collection", None)
        assert after is not None, "conftest 的 pytest_collection_finish 没接上 —— 本守卫空转"
        assert after == str(request.config.invocation_params.dir), (
            f"收集期 cwd 被挪走：{request.config.invocation_params.dir} → {after}。"
            "有测试模块在 import 时 chdir（多半是 import 了 CLI 脚本）；在 import 点前后存还原 cwd。")

    def test_collection_left_no_relative_sys_path(self, request):
        rel = getattr(request.config, "_alpha_hive_relative_sys_path", None)
        assert rel is not None, "conftest 的 pytest_collection_finish 没接上 —— 本守卫空转"
        assert rel == [], (
            f"收集期 sys.path 多了 cwd 相对项 {rel}：之后谁 chdir，import 就跟着换目录解析。")


class TestRuntimeLeaksAreUndone:
    """conftest cwd / sys.path 隔离的牙（v0.45.224 起，v0.45.237 重写）。

    v0.45.224 版的 test_2 只断言「本测试 cwd ≠ 上一条泄漏的 cwd」—— 每条测试 setup 本来就会 chdir，
    **删掉 teardown 的还原照样全绿**（实测）。要看「测试之间」的 cwd，只能让一个 **class 级 fixture**
    在两条测试之间 setup，由它记下当时的 cwd。
    """

    @pytest.fixture(scope="class")
    def cwd_between_tests(self):
        here = os.getcwd()
        return here, sorted(os.listdir(here))

    def test_0_runs_in_its_own_empty_dir(self, request, tmp_path):
        here = Path(os.getcwd()).resolve()
        assert here != Path(request.config.invocation_params.dir).resolve(), "测试没被挪进自己的空目录"
        assert here.is_relative_to(tmp_path.resolve()) and not any(here.iterdir()), (
            f"测试 cwd 不是本测试 tmp 下的空目录：{here}")

    def test_1_leak_on_purpose(self, tmp_path):
        os.chdir(tmp_path)                      # 故意不经 monkeypatch
        sys.path.insert(0, _SENTINEL)
        _LEAK.update(planted=True, cwd=os.getcwd())

    def test_1b_monkeypatch_chdir(self, monkeypatch, tmp_path):
        """monkeypatch 的撤销若排在 conftest 还原之后，cwd 会停在本测试的 `_cwd` —— test_2 看得见。"""
        monkeypatch.chdir(tmp_path)
        _LEAK["monkeypatched"] = True

    def test_2_between_tests_cwd_is_the_session_empty_dir(self, request, cwd_between_tests):
        if not (_LEAK["planted"] and _LEAK.get("monkeypatched")):
            pytest.skip("只在与 test_1 / test_1b 同跑时有意义")
        cwd, listing = cwd_between_tests
        between = request.config._alpha_hive_between_tests_cwd
        assert Path(cwd).resolve() == Path(between).resolve(), (
            f"测试之间 cwd 应停在会话级空目录 {between}，实际 {cwd}"
            f"（test_1 故意 chdir 到 {_LEAK['cwd']}；也可能是 monkeypatch 撤销排到了还原之后）")
        assert listing == [], f"会话级空目录不空：{listing[:5]} —— 有 fixture 往 cwd 写了相对路径"
        assert _SENTINEL not in sys.path, "test_1 塞进 sys.path 的项没被拿掉"

    def test_3_higher_scoped_fixture_is_not_in_invocation_dir(self, request, cwd_between_tests):
        """v0.45.224 实测：module 级 fixture 在调用目录（仓库根）里 setup，相对读取照绿。"""
        cwd, _ = cwd_between_tests
        assert Path(cwd).resolve() != Path(request.config.invocation_params.dir).resolve(), (
            "class 级 fixture 在调用目录里 setup —— 高于函数级的 cwd 相对读取又看不见了")


class TestConftestGuardsDoNotAnchorOnCwd:
    """conftest 里除了收集期记录，谁都不许用 cwd 决定「看哪儿」（v0.45.240）。

    v0.45.224 起 cwd 在测试期间被挪进空目录，任何在 fixture 里用 cwd 推出「要守的真身在哪」
    或「这个路径在不在沙箱里」的守卫都会**安静地改守 tmp**。实测两处：

    - `_isolate_ml_model_file` 的 cwd 臂用 `Path.cwd()`：路径在收集期冻结到调用目录、测试往里写模型，它不响。
    - 同一 fixture 的防线①用 `Path(default_model_path()).resolve()` 判「在 tmp 里」：默认值改回相对路径时
      resolve 按 cwd（= tmp）补全，恒真。**这一种不调 `cwd()`** —— 本类第一版只认 `cwd`/`getcwd`，漏了它。

    所以读 cwd 分两类：显式（`cwd`/`getcwd`）一律报；隐式（`resolve`/`absolute`/`abspath`/`realpath`）
    主语里带锚点（`__file__` / `invocation_params` / `tmp_path*`）才放行。
    """

    EXPLICIT = {"cwd", "getcwd"}
    IMPLICIT = {"resolve", "absolute", "abspath", "realpath"}
    ANCHORS = {"__file__", "invocation_params", "tmp_path", "tmp_path_factory"}
    ALLOWED = {
        "pytest_collection_finish",          # 它记的就是「收集结束那一刻的 cwd」
        "_assert_default_path_in_sandbox",   # 先断言 is_absolute 再 resolve；由下面的运行时自证守着
    }

    @classmethod
    def cwd_readers_outside(cls, source, allowed):
        tree = ast.parse(source)
        hits = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if fn.name in allowed:
                continue
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                    continue
                attr = n.func.attr
                if attr in cls.EXPLICIT:
                    hits.append(f"{fn.name}:{n.lineno}")
                elif attr in cls.IMPLICIT:
                    # `x.resolve()` 的主语是 x；`os.path.abspath(x)` 的主语是 x
                    subject = n.args[0] if attr in {"abspath", "realpath"} and n.args else n.func.value
                    names = {s.id for s in ast.walk(subject) if isinstance(s, ast.Name)}
                    names |= {s.attr for s in ast.walk(subject) if isinstance(s, ast.Attribute)}
                    if not names & cls.ANCHORS:
                        hits.append(f"{fn.name}:{n.lineno}")
        return hits

    def test_no_cwd_derived_watch_in_conftest(self):
        src = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
        hits = self.cwd_readers_outside(src, self.ALLOWED)
        assert not hits, (
            f"conftest 在 fixture 里读 cwd：{hits}。测试期间 cwd 是空目录（v0.45.224/237），"
            "拿它定位真身、或拿它补全相对路径再判「在不在 tmp 里」，都等于守 tmp；"
            "调用目录用 `request.config.invocation_params.dir`，仓库根用 `__file__`，判沙箱先断言 `is_absolute()`。")

    def test_detector_has_teeth(self):
        cases = {
            "import pathlib\ndef _guard(tmp_path):\n    watched = pathlib.Path.cwd() / 'x'\n": ["_guard:3"],
            "import pathlib\ndef _guard(tmp_path, mp):\n    r = pathlib.Path(mp.default_model_path()).resolve()\n": ["_guard:3"],
            "import os\ndef _guard(p):\n    r = os.path.abspath(p)\n": ["_guard:3"],
            "import os\ndef pytest_collection_finish(session):\n    x = os.getcwd()\n": [],
            "import pathlib\ndef _guard(tmp_path, mp):\n    a = pathlib.Path(mp.__file__).resolve()\n    b = tmp_path.resolve()\n": [],
            "import pathlib\ndef _guard(request):\n    d = pathlib.Path(request.config.invocation_params.dir).resolve()\n": [],
        }
        for src, want in cases.items():
            assert self.cwd_readers_outside(src, self.ALLOWED) == want, src

    def test_sandbox_check_rejects_relative_default(self, tmp_path, default_path_sandbox_check):
        """运行时自证：cwd 就在 tmp 里（本测试正处在这种环境）时，相对默认值也必须红。"""
        assert Path.cwd().resolve().is_relative_to(tmp_path.resolve()), "前提不成立：本测试的 cwd 不在 tmp 里"
        with pytest.raises(AssertionError, match="不是绝对路径"):
            default_path_sandbox_check("ml_model.json", tmp_path)
        with pytest.raises(AssertionError, match="逃出了测试沙箱"):
            default_path_sandbox_check(str(TESTS_DIR.parent / "ml_model.json"), tmp_path)
        default_path_sandbox_check(str(tmp_path / "ml_model.json"), tmp_path)
