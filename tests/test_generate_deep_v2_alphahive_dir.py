"""`generate_deep_v2.ALPHAHIVE_DIR` 硬编码遗留 bug 的回归测试（数据根迁移
阶段 4 后续，见 CHANGELOG v0.45.268「刻意不做」一节 + spawn_task 独立跟踪）。

背景：此前 `ALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))`
是模块级字面量——既不是 `__file__` 派生，也完全不读 `ALPHA_HIVE_HOME`，是本仓
这类 bug 里锁得最死的一种（`weekly_optimizer.py`/`self_analyst.py` 的同构遗留
项已在 v0.45.259 点名，仍待各自修复，不在本文件范围内）。数据根迁移阶段 5
把生产数据搬到 `~/alpha-hive-data` 后，这个字面量会继续读旧位置的陈旧/缺失
数据，且不会有任何报错。

改法沿用 `collect_data.py`（v0.45.263）已验证过的惯例：`ALPHAHIVE_DIR` 降级为
覆盖钩子（默认 `None`），真正的路径经 `_alphahive_dir()` 在调用时解析
（无覆盖 ⇒ VM 挂载点探测 ⇒ 兜底 `hive_logger.PATHS.home`）。

本文件补的是仓库里此前没有覆盖到的三个调用点：`find_latest_json`/
`find_prev_json`（读 analysis JSON）与 `get_api_key`（读 API key 文件）。
`_load_ticker_accuracy` 的 `ALPHAHIVE_DIR` 用法已由
`tests/test_close_t7_production_wiring.py` / `tests/test_ticker_accuracy_direction.py`
覆盖，此处不重复。
"""

import pytest


@pytest.fixture()
def g():
    import generate_deep_v2
    return generate_deep_v2


class TestAlphahiveDirResolution:
    def test_default_override_is_none_not_hardcoded_literal(self, g):
        """`ALPHAHIVE_DIR` 必须是覆盖钩子，不能再是导入期算好的字面量——
        否则任何 monkeypatch/环境变量都对它无效（事故原形）。"""
        assert g.ALPHAHIVE_DIR is None

    def test_follows_alpha_hive_home_when_no_override(self, g, tmp_path):
        """`_isolate_env`（conftest autouse）已把 `ALPHA_HIVE_HOME` 设到本测试
        自己的 `tmp_path`；没有覆盖时 `_alphahive_dir()` 必须跟着走，
        而不是恒定指向硬编码的 `~/Desktop/Alpha Hive`。"""
        from hive_logger import PATHS
        resolved = g._alphahive_dir()
        assert resolved == PATHS.home
        assert str(tmp_path) in str(resolved)
        assert "Desktop/Alpha Hive" not in str(resolved)

    def test_explicit_override_still_wins(self, g, tmp_path, monkeypatch):
        """显式设置 `ALPHAHIVE_DIR`（测试/VM 场景）必须优先于 `PATHS.home`。"""
        override = tmp_path / "explicit_override"
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", override)
        assert g._alphahive_dir() == override

    def test_not_frozen_across_repeated_calls_with_changing_override(self, g, tmp_path, monkeypatch):
        """同一进程内改一次覆盖值，两次调用必须给出不同结果——
        证明路径是调用时求值，不是在 import 那一刻被算死。"""
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", first)
        assert g._alphahive_dir() == first
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", second)
        assert g._alphahive_dir() == second


class TestFindJsonUsesAlphahiveDir:
    def test_find_latest_json_reads_from_current_alphahive_dir(self, g, tmp_path, monkeypatch):
        data_dir = tmp_path / "data_root"
        data_dir.mkdir()
        (data_dir / "analysis-AAA-ml-2026-09-01.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", data_dir)

        found = g.find_latest_json("AAA")
        assert found == data_dir / "analysis-AAA-ml-2026-09-01.json"

    def test_find_latest_json_does_not_see_files_only_in_stale_override(self, g, tmp_path, monkeypatch):
        """把 `ALPHAHIVE_DIR` 指到一个没有该文件的目录，必须找不到——
        对照组：防止实现偷偷绕回旧的硬编码 Desktop 路径。"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", empty_dir)

        with pytest.raises(FileNotFoundError):
            g.find_latest_json("ZZZ_NO_SUCH_TICKER")

    def test_find_prev_json_reads_from_current_alphahive_dir(self, g, tmp_path, monkeypatch):
        data_dir = tmp_path / "data_root"
        data_dir.mkdir()
        older = data_dir / "analysis-AAA-ml-2026-09-01.json"
        newer = data_dir / "analysis-AAA-ml-2026-09-02.json"
        older.write_text("{}", encoding="utf-8")
        newer.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", data_dir)

        prev = g.find_prev_json("AAA", newer)
        assert prev == older


class TestGetApiKeyUsesAlphahiveDir:
    def test_reads_key_from_alphahive_dir_when_default_home_missing(self, g, tmp_path, monkeypatch):
        """`~/.anthropic_api_key` 与 Mac 兜底路径都不存在时，必须回落到
        `_alphahive_dir()/.anthropic_api_key`——不能像修复前那样恒读
        `~/Desktop/Alpha Hive/.anthropic_api_key`。"""
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        data_dir = tmp_path / "data_root"
        data_dir.mkdir()
        (data_dir / ".anthropic_api_key").write_text("sk-test-from-alphahive-dir", encoding="utf-8")
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", data_dir)

        assert g.get_api_key() == "sk-test-from-alphahive-dir"

    def test_falls_back_to_env_var_when_no_key_file_anywhere(self, g, tmp_path, monkeypatch):
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", tmp_path / "no_such_data_dir")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")

        assert g.get_api_key() == "sk-from-env"

    def test_returns_none_when_nothing_available(self, g, tmp_path, monkeypatch):
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setattr(g, "ALPHAHIVE_DIR", tmp_path / "no_such_data_dir")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        assert g.get_api_key() is None
