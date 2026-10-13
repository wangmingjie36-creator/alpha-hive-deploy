"""数据根缺省规则（v0.45.422）与测试会话的数据根接线。

规则：`ALPHA_HIVE_HOME` 非空就用它，否则 `$HOME/alpha-hive-data`（`hive_logger.resolve_data_root`）。
此前未设时兜底到 `hive_logger.py` 所在目录（代码检出）——阶段 5 之后那里只剩冻结旧数据，交互 shell
（不设该变量）里手动跑的工具静默读写旧数据。生产（编排器 export + launchd plist）一直设着，不受影响。

缺省改成生产位置之后，测试侧多了一个必须堵的口子：收集期 import 就冻住的 PATHS 派生常量（`config.py`
的 *_CONFIG 等，清单见 `test_paths_not_frozen_at_import::KNOWN`）若在未设变量时求值，会指向**本机真实生产
数据**。所以 `conftest.pytest_configure` 在收集之前设会话级沙箱，并先记下调用 pytest 时的环境给两道
「生产在哪」的守卫用（真实数据根闸、paper_portfolio 真身）。下面第二组就是这条接线的自证。
"""
import os
from pathlib import Path

import pytest

import hive_logger
from hive_logger import DEFAULT_DATA_ROOT_NAME, PATHS, explicit_data_root, resolve_data_root
from tests import _root_data_guard

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 与 conftest._REPO_ROOT_FOR_GUARD 同式

# pytest **收集**本文件（import）那一刻的 ALPHA_HIVE_HOME——此刻任何逐条沙箱都还没动手。
# 会话沙箱若没在收集前设上，这里就是 None（或调用者 shell 里的生产值）。
_COLLECTED_UNDER = os.environ.get("ALPHA_HIVE_HOME")


class TestResolveDataRoot:
    def test_unset_defaults_to_home_alpha_hive_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "h"))
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        assert DEFAULT_DATA_ROOT_NAME == "alpha-hive-data"
        assert PATHS.home == tmp_path / "h" / "alpha-hive-data"
        assert not (tmp_path / "h").exists(), "解析数据根不许建目录"

    def test_empty_string_counts_as_unset(self, tmp_path, monkeypatch):
        """与 `_root_data_guard.real_data_root` 同口径；`Path("")` 会变成 cwd，比哪个缺省都糟。"""
        monkeypatch.setenv("HOME", str(tmp_path / "h"))
        monkeypatch.setenv("ALPHA_HIVE_HOME", "")
        assert PATHS.home == tmp_path / "h" / "alpha-hive-data"

    @pytest.mark.parametrize("raw, want", [(None, None), ("", None), ("~/x", "~/x"), ("rel/x", "rel/x"), ("/abs/x", "/abs/x")])
    def test_explicit_part_is_taken_verbatim(self, tmp_path, raw, want):
        """`explicit_data_root` 是 `resolve_data_root` 的前一半：原样、不展开 `~`、不按 cwd 解析（v0.45.461：.app
        生成器此前自己 expanduser，字面 `~/x` 与扫描读到的不是一个目录）。给了就与 `resolve_data_root` 完全相同。"""
        env = {"HOME": str(tmp_path / "h")} if raw is None else {"HOME": str(tmp_path / "h"), "ALPHA_HIVE_HOME": raw}
        got = explicit_data_root(env)
        assert got == (None if want is None else Path(want))
        if got is not None:
            assert resolve_data_root(env) == got

    def test_explicit_value_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "h"))
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "custom"))
        assert PATHS.home == tmp_path / "custom"

    def test_evaluated_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        got = []
        for h in ("a", "b"):
            monkeypatch.setenv("HOME", str(tmp_path / h))
            got.append(PATHS.home)
        assert got == [tmp_path / "a" / "alpha-hive-data", tmp_path / "b" / "alpha-hive-data"]

    def test_does_not_depend_on_where_hive_logger_lives(self, tmp_path, monkeypatch):
        """退回旧的 `__file__` 兜底（v0.45.422 前）这条会红：把 hive_logger 挪到别处，未设时的数据根不许跟着变。"""
        monkeypatch.setenv("HOME", str(tmp_path / "h"))
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        monkeypatch.setattr(hive_logger, "__file__", str(tmp_path / "elsewhere" / "hive_logger.py"))
        assert PATHS.home == tmp_path / "h" / "alpha-hive-data"

    def test_explicit_mapping_ignores_live_environ(self, tmp_path, monkeypatch):
        """测试框架用调用 pytest 时的环境快照求「真实」数据根——传了映射就只看映射。"""
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "live_sandbox"))
        assert resolve_data_root({"HOME": str(tmp_path / "h")}) == tmp_path / "h" / "alpha-hive-data"
        assert resolve_data_root({"ALPHA_HIVE_HOME": str(tmp_path / "x"), "HOME": str(tmp_path / "h")}) == tmp_path / "x"

    def test_agrees_with_real_data_root_guard(self, tmp_path):
        """两处实现同一条规则（生产解析器 / 测试闸），不许漂移：同一份环境 ⇒ 同一个根。"""
        home = tmp_path / "h"
        (home / "alpha-hive-data").mkdir(parents=True)
        (tmp_path / "custom").mkdir()
        for env in ({"HOME": str(home)}, {"HOME": str(home), "ALPHA_HIVE_HOME": ""},
                    {"HOME": str(home), "ALPHA_HIVE_HOME": str(tmp_path / "custom")}):
            assert _root_data_guard.real_data_root(env, env["HOME"], _REPO_ROOT) == str(resolve_data_root(env)), env


class TestGuardUsesTheOneRule:
    """测试闸的「真实数据根」只从 `resolve_data_root` 来（v0.45.488）。上面那条对拍只覆盖未设 / 空串 / 绝对路径——
    两份实现恰在 `~` 与相对路径上分岔（闸自己 expanduser、按 pytest 的 cwd 解析；生产原样用、按各自 cwd）。"""

    @pytest.mark.parametrize("raw", ["~/x", "x", " "])
    def test_non_absolute_value_guards_the_default_root(self, tmp_path, raw):
        """非绝对值：生产各进程按各自 cwd 解析，闸无从知道是哪 ⇒ 不猜，改守缺省位置（不是失效：那会丢掉保护）。"""
        home = tmp_path / "h"
        (home / DEFAULT_DATA_ROOT_NAME).mkdir(parents=True)
        (home / "x").mkdir()                                   # 旧实现把 `~/x` 展开到这里——新实现不许去这里
        env = {"HOME": str(home), "ALPHA_HIVE_HOME": raw}
        assert not resolve_data_root(env).is_absolute()
        assert _root_data_guard.real_data_root(env, env["HOME"], _REPO_ROOT) == str(home / DEFAULT_DATA_ROOT_NAME)
        assert "不是绝对路径" in _root_data_guard.nonabsolute_env_note(env)

    @pytest.mark.parametrize("env", [{}, {"ALPHA_HIVE_HOME": ""}, {"ALPHA_HIVE_HOME": "/abs/data"}])
    def test_note_only_when_the_value_was_not_used(self, env):
        assert _root_data_guard.nonabsolute_env_note(env) is None

    def test_report_header_says_so(self, request):
        """报告头照实说「环境值没被采用、改守缺省」——不能静默改守别处。"""
        mod = next(m for m in request.config.pluginmanager.get_plugins()
                   if hasattr(m, "_DATA_ROOT_BEFORE") and hasattr(m, "pytest_report_header"))

        class _Stash:
            def get(self, key, default):
                return ("/x/alpha-hive-data", {"a": "dir"})

        class _Cfg:
            stash = _Stash()
            _alpha_hive_invocation_env = {"ALPHA_HIVE_HOME": "~/x"}
        line = mod.pytest_report_header(_Cfg())
        assert "active on /x/alpha-hive-data" in line and "不是绝对路径" in line and "改守缺省数据根" in line, line


#: 生产代码里直接读 `ALPHA_HIVE_HOME` 的地方 ⇒ 理由（v0.45.488）。把它**变成路径**的只许 hive_logger（唯一规则）；
#: 其余只问「设没设」，或刻意不取缺省。**双向相等**：新增读者要登记（先问能不能改经 hive_logger），删了要清。
ENV_READERS = {
    "hive_logger.py::explicit_data_root": "唯一规则（resolve_data_root 的前一半）",
    "alphabot/__main__.py::main": "只问设没设：没设时提示「用缺省数据根」",
    "sell_strike_ledger.py::state_dir_status": "只问设没设，并把原值写进给人看的提示",
    "sell_strike_ledger.py::main": "只问设没设：没设时提示「按缺省数据根读写」",
    "orchestrator_steps.py::_resolve_dirs": "刻意不取缺省：生产总给 --data-dir；都没有就明说「无从核对」，不猜",
}


def _env_readers(src: str, rel: str) -> set:
    """`rel::所在函数` 的集合：`.get/.getenv/.pop/.setdefault("ALPHA_HIVE_HOME")` 与读取型下标。写入（赋值）不算。"""
    import ast
    out = set()

    def visit(node, owner):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        hit = False
        if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "ALPHA_HIVE_HOME"):
            f = node.func
            hit = (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) in {
                "get", "getenv", "pop", "setdefault"}
        elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
              and isinstance(node.slice, ast.Constant) and node.slice.value == "ALPHA_HIVE_HOME"):
            hit = True
        if hit:
            out.add(f"{rel}::{owner or '<module>'}")
        for ch in ast.iter_child_nodes(node):
            visit(ch, owner)

    visit(ast.parse(src), None)
    return out


class TestDataRootFromEnvHasOneImplementation:
    """「从 ALPHA_HIVE_HOME 得出数据根」只有一份实现（v0.45.488）：测试闸自己写的那份就是这么漂出去的。"""

    def test_readers_are_exactly_the_registered_ones(self):
        from tests._repo_files import own_python_files
        root = Path(_REPO_ROOT)
        files, _how = own_python_files(root)
        found = set()
        for f in files:
            rel = f.relative_to(root).as_posix()
            if rel.split("/")[0] in {"tests", "experiments"} and rel != "tests/_root_data_guard.py":
                continue                                   # 测试闸那份单独纳入：它就是这次收掉的重复实现
            found |= _env_readers(f.read_text(encoding="utf-8", errors="replace"), rel)
        assert "hive_logger.py::explicit_data_root" in found, "扫描器没看到唯一规则本身——扫描失明"
        assert found == set(ENV_READERS), (
            f"新增（先问能不能改经 hive_logger，确需直读再登记理由）：{sorted(found - set(ENV_READERS))}；"
            f"已不存在（从 ENV_READERS 删掉）：{sorted(set(ENV_READERS) - found)}")

    @pytest.mark.parametrize("src, hit", [
        ("import os\ndef f():\n    return os.environ.get('ALPHA_HIVE_HOME')\n", True),
        ("import os\ndef f():\n    return os.getenv('ALPHA_HIVE_HOME', '/x')\n", True),
        ("def f(env):\n    return env['ALPHA_HIVE_HOME']\n", True),
        ("import os\ndef f(h):\n    os.environ['ALPHA_HIVE_HOME'] = h\n", False),        # 写入不是读者
        ("def f():\n    return 'ALPHA_HIVE_HOME'\n", False),
    ])
    def test_scanner_has_teeth(self, src, hit):
        assert bool(_env_readers(src, "x.py")) is hit


class TestSessionWiring:
    def test_session_sandbox_was_in_place_at_collection(self, data_root_session_facts, tmp_path_factory):
        """收集期的 ALPHA_HIVE_HOME 就是会话沙箱：收集期冻住的常量落在沙箱里，碰不到真实数据根。"""
        facts = data_root_session_facts
        assert _COLLECTED_UNDER is not None, "收集期 ALPHA_HIVE_HOME 未设：收集期冻住的常量会指向缺省生产数据根"
        assert _COLLECTED_UNDER == facts["session_home"]
        assert Path(_COLLECTED_UNDER).resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())
        assert Path(_COLLECTED_UNDER) != resolve_data_root(facts["invocation_env"])
        assert facts["invocation_env"].get("ALPHA_HIVE_HOME") != facts["session_home"], \
            "「调用时环境」快照取晚了：已经是会话沙箱"

    def test_real_data_root_guard_uses_invocation_env(self, data_root_session_facts, tmp_path_factory):
        """闸若改读 os.environ（此刻是会话沙箱），会把沙箱认成「真实数据根」——在每台机器上都红。"""
        facts = data_root_session_facts
        inv = facts["invocation_env"]
        expected = _root_data_guard.real_data_root(inv, inv.get("HOME") or os.path.expanduser("~"), _REPO_ROOT)
        assert facts["guard_root"] == expected
        if expected is not None:
            assert not Path(expected).resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve())

    def test_paper_portfolio_real_location_follows_invocation_env(
            self, _real_paper_portfolio_state_dir, data_root_session_facts):
        facts = data_root_session_facts
        assert _real_paper_portfolio_state_dir == \
            resolve_data_root(facts["invocation_env"]) / "paper_portfolio_state"
        assert not _real_paper_portfolio_state_dir.is_relative_to(Path(facts["session_home"])), \
            "paper_portfolio 真身落进了会话沙箱：守卫盯着自己（v0.45.303 那种失明）"
