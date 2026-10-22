"""开发期选测脚本（`tools/select_tests/`，v0.45.615）——选得对、不静默选少、登记表不过期。

五层：
1. `classify` 的归类表；
2. `make_plan`（纯函数）：测试文件 / 非代码文件 / 登记表 / 核心文件 / 生产代码 各选什么、什么时候回退；
3. 登记表 IO 与「由 dump 重建 / 并入 / 找漏登的读」：读不了就抛 `RegistryError`（上层回退整套），写出来的是合法 JSON 且每个键一行；
4. 脚本入口（子进程 + 假 pytest）：退出码、随跑测量发现登记表过期 ⇒ 退出码 4、pytest 红时不被 4 盖住；
5. **真审计插件正对照**：真起一个 pytest 会话，插件必须真看见「元守卫读了全部测试文件」；以及**真登记表**必须点名两次事故里红过的守卫
   （v0.45.439 / v0.45.592→596：只跑相关测试、没跑这两个元守卫，main 变红）——登记表被掏空时这条先红。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_TOOLS = _REPO / "tools" / "select_tests"

_spec = importlib.util.spec_from_file_location("select_tests_under_test", _TOOLS / "select_tests.py")
ST = importlib.util.module_from_spec(_spec)
sys.modules["select_tests_under_test"] = ST
_spec.loader.exec_module(ST)

#: 两次事故里红过的元守卫（逐个读全部测试文件）
INCIDENT_GUARDS = ("tests/test_tests_use_running_interpreter.py", "tests/test_reads_own_checkout.py")


def _reg(**kw) -> dict:
    reg = ST.empty_registry()
    reg.update(kw)
    return reg


REG = _reg(
    reads_all_tests=["tests/test_meta_a.py", "tests/test_meta_b.py"],
    reads_tests={"tests/test_narrow.py": ["tests/test_target.py"]},
    nonpy_readers={"CHANGELOG.md": ["tests/test_changelog_x.py"],
                   "scripts/run.sh": ["tests/test_orch.py", "tests/test_orch2.py"]},
)
NO_TEXTS: dict = {}


def _plan(changes, reg=REG, texts=NO_TEXTS, gone=()):
    return ST.make_plan(changes, reg, exists=lambda p: p not in gone, test_texts=texts)


class TestClassify:
    @pytest.mark.parametrize("path, kind", [
        ("tests/test_x.py", "test"),
        ("tests/_must_run.json", "registry"),
        ("tests/conftest.py", "core"),
        ("tests/_helper.py", "core"),
        ("tests/fixtures/a.json", "core"),
        ("tests/sub/test_deep.py", "core"),          # 子目录里的 test_*.py：规则圈不住 ⇒ 核心
        ("config.py", "core"),
        ("hive_logger.py", "core"),
        ("pyproject.toml", "core"),
        ("requirements.txt", "core"),
        (".github/workflows/tests.yml", "core"),
        ("tools/clockshift/audit.py", "core"),
        ("tools/select_tests/select_tests.py", "core"),
        ("swarm_agents/oracle.py", "prod"),
        ("pheromone_board.py", "prod"),
        ("CHANGELOG.md", "nonpy"),
        ("scripts/alpha-hive-orchestrator.sh", "nonpy"),
        ("experiments/x_prereg.md", "nonpy"),
    ])
    def test_table(self, path, kind):
        assert ST.classify(path) == kind


class TestPlan:
    def test_test_change_selects_readall_extra_self_narrow_and_importers(self):
        texts = {"tests/test_user.py": "from test_target import helper\n", "tests/test_other.py": "import os\n"}
        p = _plan([("M", "tests/test_target.py")], texts=texts)
        assert p.mode == "subset" and p.exit_code == 0
        assert set(p.selected) == {"tests/test_meta_a.py", "tests/test_meta_b.py", *ST.EXTRA_ALWAYS,
                                   "tests/test_target.py", "tests/test_narrow.py", "tests/test_user.py"}
        assert p.changed_tests == ["tests/test_target.py"]

    def test_unrelated_narrow_reader_is_not_selected(self):
        p = _plan([("M", "tests/test_something_else.py")])
        assert "tests/test_narrow.py" not in p.selected

    def test_new_unregistered_test_file_still_gets_the_meta_guards(self):
        """事故形状：新增一个登记表不认识的测试文件——它本身 + 全部元守卫都得跑。"""
        p = _plan([("A", "tests/test_brand_new.py")])
        assert {"tests/test_brand_new.py", "tests/test_meta_a.py", *ST.EXTRA_ALWAYS} <= set(p.selected)

    def test_deleted_test_is_not_run_but_meta_guards_and_its_readers_are(self):
        p = _plan([("D", "tests/test_target.py")], gone={"tests/test_target.py"})
        assert "tests/test_target.py" not in p.selected and "tests/test_narrow.py" in p.selected
        assert "tests/test_meta_a.py" in p.selected and p.changed_tests == []

    def test_nonpy_change_selects_only_its_readers(self):
        p = _plan([("M", "CHANGELOG.md")])
        assert p.selected == ["tests/test_changelog_x.py"]
        assert p.changed_tests == [] and p.mode == "subset"

    def test_nonpy_with_no_known_reader_selects_nothing_and_says_why(self):
        p = _plan([("M", "docs/unread.md")])
        assert p.mode == "none" and p.selected == [] and any("子进程里的读看不到" in n for n in p.notes)

    def test_test_plus_changelog_is_the_realistic_commit(self):
        p = _plan([("M", "CHANGELOG.md"), ("A", "tests/test_brand_new.py")])
        assert {"tests/test_changelog_x.py", "tests/test_brand_new.py", "tests/test_meta_a.py"} <= set(p.selected)

    def test_registry_change_runs_only_its_guard(self):
        p = _plan([("M", ST.REGISTRY_REL)])
        assert p.selected == [ST.REGISTRY_GUARD_TEST]

    def test_core_file_falls_back_to_full_even_with_tests_mixed_in(self):
        p = _plan([("M", "tests/test_target.py"), ("M", "tests/conftest.py")])
        assert p.mode == "full" and p.exit_code == ST.EXIT_FULL and p.selected == []

    def test_production_code_is_not_guessed(self):
        p = _plan([("M", "tests/test_target.py"), ("M", "pheromone_board.py")])
        assert p.mode == "uncovered" and p.exit_code == ST.EXIT_UNCOVERED and p.selected == []

    def test_core_wins_over_prod(self):
        assert _plan([("M", "pheromone_board.py"), ("M", "config.py")]).mode == "full"

    def test_registry_reference_to_missing_file_is_skipped_with_a_note(self):
        reg = _reg(reads_all_tests=["tests/test_meta_a.py", "tests/test_vanished.py"])
        p = _plan([("M", "tests/test_x.py")], reg=reg, gone={"tests/test_vanished.py"})
        assert "tests/test_vanished.py" not in p.selected and any("已不存在" in n for n in p.notes)


class TestRegistryIO:
    def test_missing_corrupt_wrong_version_and_malformed_all_raise(self, tmp_path):
        with pytest.raises(ST.RegistryError):
            ST.load_registry(tmp_path / "absent.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        with pytest.raises(ST.RegistryError):
            ST.load_registry(bad)
        bad.write_text(json.dumps({"version": 99, "reads_all_tests": [], "reads_tests": {}, "nonpy_readers": {}}))
        with pytest.raises(ST.RegistryError):
            ST.load_registry(bad)
        bad.write_text(json.dumps({"version": 1, "reads_all_tests": {}, "reads_tests": {}, "nonpy_readers": {}}))
        with pytest.raises(ST.RegistryError):
            ST.load_registry(bad)

    def test_roundtrip_and_one_key_per_line(self, tmp_path):
        path = tmp_path / "reg.json"
        ST.save_registry(path, REG)
        assert ST.load_registry(path)["reads_tests"] == REG["reads_tests"]
        lines = path.read_text().splitlines()
        # 并行 session 各改不同读者时 git 才能按行合并：每个读者占一行
        assert sum('"tests/test_orch.py"' in ln for ln in lines) == 1 and any('"scripts/run.sh": [' in ln for ln in lines)
        json.loads(path.read_text())


def _dump(by_file, n=100):
    return {"version": 1, "n_collected_files": n, "by_file": by_file}


#: 合成仓库的测试文件总数（「读全部测试文件」的分母）。真仓库的分母由 `ST.repo_test_count()` 数磁盘。
N_TESTS = 100


class TestBuildMergeDrift:
    def test_build_splits_readall_narrow_and_nonpy(self):
        d = _dump({
            "tests/test_meta.py": {"other_test": [f"tests/test_{i}.py" for i in range(60)], "nonpy": ["CHANGELOG.md"]},
            "tests/test_narrow.py": {"other_test": ["tests/test_t.py"]},
            "tests/test_plain.py": {"prod_py": ["a.py"]},
            "<session>": {"nonpy": ["IGNORED.md"]},
        })
        reg = ST.build_registry(d, "abc", n_tests=N_TESTS)
        assert reg["reads_all_tests"] == ["tests/test_meta.py"] and reg["reads_tests"] == {"tests/test_narrow.py": ["tests/test_t.py"]}
        assert reg["nonpy_readers"] == {"CHANGELOG.md": ["tests/test_meta.py"]} and reg["commit"] == "abc"

    def test_build_refuses_an_empty_measurement(self):
        with pytest.raises(ValueError):
            ST.build_registry(_dump({}), "x", n_tests=N_TESTS)
        with pytest.raises(ValueError):
            ST.build_registry(_dump({"tests/test_a.py": {}}, n=0), "x", n_tests=N_TESTS)

    def test_gaps_flag_new_reads_but_not_fewer_reads(self):
        d = _dump({"tests/test_narrow.py": {"other_test": ["tests/test_target.py", "tests/test_new.py"]}})
        assert any("tests/test_new.py" in g for g in ST.observed_gaps(REG, d, n_tests=N_TESTS))
        fewer = _dump({"tests/test_narrow.py": {"other_test": []}, "tests/test_meta_a.py": {"other_test": ["tests/test_x.py"]}})
        assert ST.observed_gaps(REG, fewer, n_tests=N_TESTS) == [], "CI 上有的测试 skip ⇒ 观测到的比登记表少是正常的，不算漂移"

    def test_gaps_flag_a_new_readall_and_new_nonpy_reads(self):
        many = [f"tests/test_{i}.py" for i in range(60)]
        d = _dump({"tests/test_new_meta.py": {"other_test": many}})
        assert any("reads_all_tests" in g for g in ST.observed_gaps(REG, d, n_tests=N_TESTS))
        d2 = _dump({"tests/test_plain.py": {"nonpy": ["docs/new.md"]}})
        assert any("docs/new.md" in g for g in ST.observed_gaps(REG, d2, n_tests=N_TESTS))

    def test_gaps_respect_the_files_filter(self):
        d = _dump({"tests/test_narrow.py": {"other_test": ["tests/test_new.py"]}})
        assert ST.observed_gaps(REG, d, files={"tests/test_other.py"}, n_tests=N_TESTS) == []

    def test_merge_unions_never_deletes_and_promotes_readall(self):
        d = _dump({"tests/test_narrow.py": {"other_test": ["tests/test_new.py"]},
                   "tests/test_promoted.py": {"other_test": [f"tests/test_{i}.py" for i in range(60)]}})
        out = ST.merge_observation(REG, d, n_tests=N_TESTS)
        assert out["reads_tests"]["tests/test_narrow.py"] == ["tests/test_new.py", "tests/test_target.py"]
        assert "tests/test_promoted.py" in out["reads_all_tests"] and "tests/test_meta_a.py" in out["reads_all_tests"]
        assert REG["reads_tests"]["tests/test_narrow.py"] == ["tests/test_target.py"], "不许改传入的登记表"
        assert ST.observed_gaps(out, d, n_tests=N_TESTS) == []


class TestReadAllDenominator:
    """v0.45.619：「读 ≥ 一半测试文件」的分母曾取 dump 的 `n_collected_files`。`--update` / `--run` 只收集被改 / 被选的
    几个文件 ⇒ 分母小到 1 ⇒ 只读了一个别的测试文件的测试被当成元守卫、挪出 `reads_tests`
    （2026-10-22 实测：改 `tests/test_alphabot_chat.py` 后 `--update` 把它挪进了 `reads_all_tests`）。"""

    #: 2026-10-22 那次 `--update` 的 dump 形状：只收集了被改的一个文件，它读了一个别的测试文件
    PARTIAL = {"version": 1, "n_collected_files": 1,
               "by_file": {"tests/test_alphabot_chat.py": {"other_test": ["tests/test_alphabot.py"]}}}

    def test_partial_dump_with_one_narrow_read_merges_into_reads_tests(self):
        out = ST.merge_observation(ST.empty_registry(), self.PARTIAL, files={"tests/test_alphabot_chat.py"}, n_tests=375)
        assert "tests/test_alphabot_chat.py" not in out["reads_all_tests"], "只读一个测试文件的测试被当成了元守卫"
        assert out["reads_tests"] == {"tests/test_alphabot_chat.py": ["tests/test_alphabot.py"]}

    def test_partial_dump_gap_is_reported_as_a_narrow_read(self):
        gaps = ST.observed_gaps(ST.empty_registry(), self.PARTIAL, n_tests=375)
        assert len(gaps) == 1 and "tests/test_alphabot.py" in gaps[0] and "reads_all_tests" not in gaps[0], gaps

    def test_a_real_meta_guard_still_promotes_in_a_partial_run(self):
        """反方向：元守卫读的是磁盘上的全部测试文件，与这一轮收集了几个无关——部分收集时照样认得出。"""
        d = {"version": 1, "n_collected_files": 1,
             "by_file": {"tests/test_meta.py": {"other_test": [f"tests/test_{i}.py" for i in range(374)]}}}
        assert "tests/test_meta.py" in ST.merge_observation(ST.empty_registry(), d, n_tests=375)["reads_all_tests"]
        assert any("reads_all_tests" in g for g in ST.observed_gaps(ST.empty_registry(), d, n_tests=375))

    def test_nonpositive_denominator_raises_instead_of_demoting_meta_guards(self):
        """分母 ≤ 0 时若答「不算读全部」，真元守卫会被降成窄读者 ⇒ 改新测试文件时漏选它。只能抛。"""
        for n in (0, -1):
            with pytest.raises(ValueError):
                ST.counts_as_read_all(400, n)
        with pytest.raises(ValueError):
            ST.merge_observation(ST.empty_registry(), self.PARTIAL, n_tests=0)

    def test_repo_test_count_is_the_on_disk_test_files(self):
        n = ST.repo_test_count()
        assert n == len(list((_REPO / "tests").glob("test_*.py"))) and n > 100
        assert ST.repo_test_count(_REPO / "no_such_dir") == 0


def _run_cli(*args, cwd=_REPO, timeout=None):
    return subprocess.run([sys.executable, str(_TOOLS / "select_tests.py"), *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)


class TestCli:
    def test_missing_registry_falls_back_to_full_not_to_nothing(self, tmp_path):
        r = _run_cli("--registry", str(tmp_path / "absent.json"), "--files", "tests/test_concurrency_hint.py")
        assert r.returncode == ST.EXIT_FULL and "回退整套" in r.stdout and "tests/" in r.stdout

    def test_exit_codes_against_a_synthetic_registry(self, tmp_path):
        reg = tmp_path / "reg.json"
        ST.save_registry(reg, _reg(reads_all_tests=["tests/test_concurrency_hint.py"],
                                   nonpy_readers={"CHANGELOG.md": ["tests/test_concurrency_hint.py"]}))
        assert _run_cli("--registry", str(reg), "--files", "tests/test_write_sandbox.py").returncode == 0
        assert _run_cli("--registry", str(reg), "--files", "tests/conftest.py").returncode == ST.EXIT_FULL
        assert _run_cli("--registry", str(reg), "--files", "pheromone_board.py").returncode == ST.EXIT_UNCOVERED
        r = _run_cli("--registry", str(reg), "--files", "CHANGELOG.md")
        assert r.returncode == 0 and "tests/test_concurrency_hint.py" in r.stdout and "收尾 / CI 仍整套" in r.stdout

    def test_check_drift_exit_codes(self, tmp_path):
        reg = tmp_path / "reg.json"
        ST.save_registry(reg, REG)
        ok, bad, empty = tmp_path / "ok.json", tmp_path / "bad.json", tmp_path / "empty.json"
        ok.write_text(json.dumps(_dump({"tests/test_narrow.py": {"other_test": ["tests/test_target.py"]}})))
        bad.write_text(json.dumps(_dump({"tests/test_narrow.py": {"other_test": ["tests/test_unregistered.py"]}})))
        empty.write_text(json.dumps(_dump({})))
        assert _run_cli("--registry", str(reg), "--check-drift", str(ok)).returncode == 0
        r = _run_cli("--registry", str(reg), "--check-drift", str(bad))
        assert r.returncode == ST.EXIT_STALE and "test_unregistered" in r.stdout
        assert _run_cli("--registry", str(reg), "--check-drift", str(empty)).returncode == ST.EXIT_USAGE, \
            "空 dump 不能当「没有漂移」——测量没生效"

    def test_build_writes_a_loadable_registry(self, tmp_path):
        dump = tmp_path / "dump.json"
        dump.write_text(json.dumps(_dump({"tests/test_narrow.py": {"other_test": ["tests/test_t.py"]}})))
        out = tmp_path / "reg.json"
        assert _run_cli("--registry", str(out), "--build", str(dump)).returncode == 0
        assert ST.load_registry(out)["reads_tests"] == {"tests/test_narrow.py": ["tests/test_t.py"]}


class _FakePytest:
    """替换 `subprocess.run`：认出 pytest 调用（命令里有 `-p audit_plugin`）就按场景写 dump、返回给定退出码；别的调用放行。"""

    def __init__(self, real, rc, dump):
        self.real, self.rc, self.dump, self.calls = real, rc, dump, []

    def __call__(self, cmd, *a, **k):
        if "audit_plugin" in cmd:
            self.calls.append(list(cmd))
            out = k["env"]["SELECT_TESTS_OUT"]
            if self.dump is not None:
                Path(out).write_text(json.dumps(self.dump))
            return subprocess.CompletedProcess(cmd, self.rc)
        return self.real(cmd, *a, **k)


class TestRunRideAlongMeasurement:
    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        reg = tmp_path / "reg.json"
        ST.save_registry(reg, _reg(reads_all_tests=["tests/test_write_sandbox.py"]))
        real = subprocess.run

        def go(rc, dump, *extra):
            fake = _FakePytest(real, rc, dump)
            monkeypatch.setattr(ST.subprocess, "run", fake)
            code = ST.main(["--registry", str(reg), "--files", "tests/test_concurrency_hint.py", "--run", *extra])
            return code, fake
        return go, reg

    def test_clean_run_exits_zero_and_attaches_the_plugin(self, setup):
        go, _ = setup
        code, fake = go(0, _dump({"tests/test_concurrency_hint.py": {}}))
        assert code == 0 and fake.calls and "audit_plugin" in fake.calls[0]
        assert "tests/test_concurrency_hint.py" in fake.calls[0]

    def test_changed_test_that_now_reads_unregistered_files_exits_stale(self, setup, capsys):
        go, _ = setup
        code, _ = go(0, _dump({"tests/test_concurrency_hint.py": {"other_test": ["tests/test_new_victim.py"]}}))
        assert code == ST.EXIT_STALE and "登记表过期" in capsys.readouterr().out

    def test_red_tests_are_not_masked_by_the_stale_code(self, setup):
        go, _ = setup
        code, _ = go(1, _dump({"tests/test_concurrency_hint.py": {"other_test": ["tests/test_new_victim.py"]}}))
        assert code == 1

    def test_missing_dump_is_said_out_loud_not_treated_as_clean(self, setup, capsys):
        go, _ = setup
        code, _ = go(0, None)
        assert code == 0 and "没能随跑测量" in capsys.readouterr().err

    def test_update_merges_the_measured_reads(self, setup):
        """`n=1`：`--update` 只收集被改的那一个文件，真 dump 就是这个数（v0.45.619 前这里写 100，正好绕开了分母 bug）。"""
        go, reg = setup
        code, _ = go(0, _dump({"tests/test_concurrency_hint.py": {"other_test": ["tests/test_new_victim.py"]}}, n=1), "--update")
        assert code == 0
        after = ST.load_registry(reg)
        assert after["reads_tests"] == {"tests/test_concurrency_hint.py": ["tests/test_new_victim.py"]}
        assert after["reads_all_tests"] == ["tests/test_write_sandbox.py"], "只读一个测试文件的测试不能被并成元守卫"

    def test_run_reports_a_two_file_read_as_narrow_not_as_readall(self, setup, capsys):
        """`--run` 这里收集 3 个文件（元守卫 + EXTRA_ALWAYS + 被改的）：读 2 个测试文件（真登记表里有这种窄读者）按旧分母 ≥ 1.5。"""
        go, _ = setup
        reads = ["tests/test_new_victim.py", "tests/test_other_victim.py"]
        code, _ = go(0, _dump({"tests/test_concurrency_hint.py": {"other_test": reads}}, n=3))
        out = capsys.readouterr().out
        assert code == ST.EXIT_STALE and "登记表没有的测试文件" in out and "应归入 reads_all_tests" not in out, out

    def test_update_does_not_write_when_the_measured_tests_are_red(self, setup):
        go, reg = setup
        before = reg.read_text()
        code, _ = go(1, _dump({"tests/test_concurrency_hint.py": {"other_test": ["tests/test_new_victim.py"]}}), "--update")
        assert code == 1 and reg.read_text() == before


class TestRealAuditPlugin:
    """**正对照**：真起 pytest、真加载插件。没有它，上面的一切都是在假设插件看得见读。"""

    def _run(self, tmp_path, *targets):
        out = tmp_path / "dump.json"
        env = dict(os.environ, SELECT_TESTS_OUT=str(out), PYTHONPATH=os.pathsep.join([str(_TOOLS), os.environ.get("PYTHONPATH", "")]))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
                            "-p", "audit_plugin", *targets], cwd=_REPO, env=env, capture_output=True, text=True, timeout=240)
        assert r.returncode == 0, r.stdout + r.stderr
        return json.loads(out.read_text())

    def test_plugin_sees_a_meta_guard_reading_every_test_file_and_a_plain_test_reading_none(self, tmp_path):
        d = self._run(tmp_path, "tests/test_tests_use_running_interpreter.py", "tests/test_concurrency_hint.py::TestLines")
        guard = d["by_file"]["tests/test_tests_use_running_interpreter.py"]
        n_tests = len(list((_REPO / "tests").glob("test_*.py")))
        assert len(guard["other_test"]) >= 0.9 * (n_tests - 1), "元守卫逐个读测试文件：插件必须看见"
        plain = d["by_file"].get("tests/test_concurrency_hint.py", {})
        assert not plain.get("other_test"), "纯逻辑测试不读别的测试文件：插件不能凭空多记"
        assert d["n_collected_files"] == 2 and "tests/test_tests_use_running_interpreter.py" not in guard["other_test"]

    def test_real_update_of_one_narrow_reader_keeps_it_narrow(self, tmp_path):
        """v0.45.619 事故的真复现：真 `--update`（真插件、真子 pytest）只收集一个文件 ⇒ dump 的 `n_collected_files` = 1。
        `test_ast_cache` 按文件名读 `test_no_invisible_prod_data_skips.py`（确定性的数据读，不经 import）。"""
        target, read = "tests/test_ast_cache.py", "tests/test_no_invisible_prod_data_skips.py"
        assert read.split("/")[1] in (_REPO / target).read_text(encoding="utf-8"), \
            f"{target} 不再按名字读 {read}——这条复现要换一个只读一个测试文件的目标"
        reg = tmp_path / "reg.json"
        ST.save_registry(reg, ST.empty_registry())
        r = _run_cli("--registry", str(reg), "--files", target, "--update", timeout=240)
        assert r.returncode == 0, r.stdout + r.stderr
        after = ST.load_registry(reg)
        assert target not in after["reads_all_tests"], "只读一个测试文件的测试被 --update 当成了元守卫"
        assert read in after["reads_tests"].get(target, []), after["reads_tests"]

    def test_plugin_refuses_to_load_without_an_output_path(self):
        env = {k: v for k, v in os.environ.items() if k != "SELECT_TESTS_OUT"}
        r = subprocess.run([sys.executable, "-c", "import audit_plugin"], cwd=_TOOLS, env=env, capture_output=True, text=True)
        assert r.returncode != 0 and "SELECT_TESTS_OUT" in r.stderr


class TestRealRegistry:
    def test_registry_names_the_incident_guards(self):
        """v0.45.439 / v0.45.592→596 的事故：只跑相关测试、漏了这两个元守卫，main 变红。登记表被掏空 / 重建丢了它们 ⇒ 这条先红。"""
        reg = ST.load_registry(_REPO / ST.REGISTRY_REL)
        for g in INCIDENT_GUARDS:
            assert g in reg["reads_all_tests"], f"{g} 不在「读全部测试文件」里——改测试文件时它不会被选中"
        assert (_REPO / ST.EXTRA_ALWAYS[0]).exists()

    def test_replayed_incident_diff_selects_the_guards_that_went_red(self):
        """v0.45.592 的真实改动形状：新增 tests/test_llm_shadow_daily.py + CHANGELOG（+ 生产文件，那一档不覆盖）。"""
        reg = ST.load_registry(_REPO / ST.REGISTRY_REL)
        p = ST.make_plan([("A", "tests/test_llm_shadow_daily.py"), ("M", "CHANGELOG.md")], reg,
                         exists=lambda x: True, test_texts={})
        assert p.mode == "subset" and set(INCIDENT_GUARDS) <= set(p.selected)
        assert ST.make_plan([("A", "tests/test_llm_shadow_daily.py"), ("M", "llm_shadow_daily.py")], reg,
                            exists=lambda x: True, test_texts={}).mode == "uncovered"

    def test_registry_is_saved_in_the_canonical_one_key_per_line_form(self, tmp_path):
        reg = ST.load_registry(_REPO / ST.REGISTRY_REL)
        again = tmp_path / "again.json"
        ST.save_registry(again, reg)
        assert again.read_text() == (_REPO / ST.REGISTRY_REL).read_text(), "登记表不是规范写法——别手改，用 --build / --update"
