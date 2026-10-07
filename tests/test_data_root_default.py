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

import hive_logger
from hive_logger import DEFAULT_DATA_ROOT_NAME, PATHS, resolve_data_root
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
