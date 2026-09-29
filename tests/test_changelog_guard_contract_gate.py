"""`changelog_guard.py` 的**契约闸**（pre-push，v0.45.355）

CHANGELOG 部分的管道测试在 `tests/test_changelog_guard_hook.py`，本文件只测契约闸：
推往 main、且被推区间动了代码时，在**被推提交的导出树**里跑 `CONTRACT_TEST_GLOBS` 选出的契约测试。

每个推送用例都在真 git 仓库里走真实 `git push`，钩子由 `--install-hook` 装进去（不直接调函数）。
临时仓库里的契约测试是个**桩**：断言 `app.KEYS == ["a", "b"]`（不等就带 `CONTRACT-BROKEN` 红），
并把「在哪棵树里跑的」记到仓库外的 `gate_runs.txt`——用来区分「闸放行了」与「闸根本没跑」。
`CONTRACT_REQUIRED_TESTS` 里其余的必选文件各是一条空测试的占位（按常量生成：别的 session 往里追加路径，
夹具自动跟上，不用改本文件）。

⚠️ 同 `test_changelog_guard_hook.py`：「推送被拦」有好几种成因（契约红了 / 找不到契约测试 /
被跳过 / 被摘掉 / 收集出错），每条拦截用例都断言**具体原因**与桩跑没跑。

⚠️ 本文件名不能匹配 `CONTRACT_TEST_GLOBS`（否则每次推送都会在闸里再跑一遍本文件——自我递归），
`TestSelection.test_real_repo_selection` 钉着这一条。
"""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
sys.path.insert(0, str(_ROOT))

import changelog_guard as cg  # noqa: E402

_CONTRACT_STUB = '''
import json, os, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
def test_app_keys():
    with open(os.environ["GATE_RUNS"], "a") as f:
        f.write(json.dumps({"root": str(ROOT)}) + "\\n")
    sys.path.insert(0, str(ROOT))
    import app
    assert app.KEYS == ["a", "b"], f"CONTRACT-BROKEN {app.KEYS}"
'''

GOOD_APP = 'KEYS = ["a", "b"]\n'
BROKEN_APP = 'KEYS = ["a"]   # 删了一个 --out 键\n'
_REQUIRED_PLACEHOLDER = "def test_present():\n    pass\n"


@pytest.fixture(scope="module")
def installed_hooks(tmp_path_factory):
    """真 `--install-hook` 装一次，各用例**硬链接**过去（理由同 test_changelog_guard_hook.py 同名夹具）。"""
    d = tmp_path_factory.mktemp("hook_template")
    env = {k: v for k, v in os.environ.items() if k not in cg._GIT_LOCATION_VARS}
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True, env=env)
    r = subprocess.run([sys.executable, str(_ROOT / "changelog_guard.py"), "--install-hook"],
                       cwd=d, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return d / ".git" / "hooks"


@pytest.fixture
def repo(tmp_path, monkeypatch, installed_hooks):
    for var in cg._GIT_LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALPHA_HIVE_HOOK_PYTHON", sys.executable)
    runs_file = tmp_path / "gate_runs.txt"
    monkeypatch.setenv("GATE_RUNS", str(runs_file))
    tmpd = tmp_path / "tmpd"          # 钩子的 tempfile 落在这里 ⇒ 能核对导出树有没有清掉
    tmpd.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpd))

    origin, r = tmp_path / "origin.git", tmp_path / "repo"

    def git(*args, check=True, cwd=r):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=dict(os.environ))
        if check and p.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{p.stdout}{p.stderr}")
        return p

    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", str(r), cwd=tmp_path)
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    shutil.copy(_ROOT / "changelog_guard.py", r / "changelog_guard.py")
    (r / "tests").mkdir()
    (r / "tests" / "test_changelog_entry_integrity.py").write_text("def test_ok():\n    pass\n")
    for req in cg.CONTRACT_REQUIRED_TESTS:              # 必选文件缺一个就拦 ⇒ 夹具里得全有
        (r / req).parent.mkdir(parents=True, exist_ok=True)
        (r / req).write_text(_REQUIRED_PLACEHOLDER)
    (r / "tests" / "test_step_contract.py").write_text(_CONTRACT_STUB)
    (r / "CHANGELOG.md").write_text("# log\n\n## [1] — y\n\nbody\n")
    (r / "app.py").write_text(GOOD_APP)
    (r / "report.json").write_text("{}")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")            # 此时还没装钩子

    for name in cg.HOOKS:
        os.link(installed_hooks / name, r / ".git" / "hooks" / name)

    def runs():
        return [json.loads(x) for x in runs_file.read_text().splitlines()] if runs_file.exists() else []

    def origin_main():
        return git("--git-dir", str(origin), "rev-parse", "main", cwd=tmp_path).stdout.strip()

    def commit(files, msg, *extra):
        for name, text in files.items():
            p = r / name
            if text is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
        git("add", "-A")
        git("commit", "-qm", msg, *extra)
        return git("rev-parse", "HEAD").stdout.strip()

    def leftover_exports():
        return sorted(p.name for p in tmpd.glob("alpha-hive-contract-gate-*"))

    return type("Repo", (), dict(path=r, git=staticmethod(git), runs=staticmethod(runs), tmp=tmp_path,
                                 origin_main=staticmethod(origin_main), commit=staticmethod(commit),
                                 leftover_exports=staticmethod(leftover_exports)))


def _push(repo, refspec="HEAD:main", remote="origin"):
    return repo.git("push", remote, refspec, check=False)


# ═════════════════════════════ 选哪些测试 ═════════════════════════════

class TestSelection:

    def test_glob_selects_contract_files_and_nothing_else(self, tmp_path, monkeypatch):
        """变红的变异：把 glob 写成固定文件名 ⇒ 新契约文件不入闸；漏掉 orchestrator_steps ⇒ 它不入闸。

        只测 glob（必选清单清空）：别的 session 往必选清单里登记 `test_orchestrator_braced_vars.py` 后，
        这里的「它不该被 glob 选中」依然成立；必选的并入由下一条测。"""
        monkeypatch.setattr(cg, "CONTRACT_REQUIRED_TESTS", ())
        t = tmp_path / "tests"
        (t / "sub").mkdir(parents=True)
        for name in ("test_step_contract.py", "test_step_contract_ic_rerun.py", "test_orchestrator_steps.py",
                     "test_orchestrator_braced_vars.py", "test_changelog_guard_contract_gate.py",
                     "test_other.py", "step_contract_helpers.py", "sub/test_step_contract.py"):
            (t / name).write_text("")
        (t / "test_step_contract_pkg.py").mkdir()      # 同名的目录不是测试文件
        assert cg._select_contract_tests(tmp_path) == [
            "tests/test_orchestrator_steps.py",
            "tests/test_step_contract.py",
            "tests/test_step_contract_ic_rerun.py",
        ]

    def test_empty_tree_selects_nothing(self, tmp_path):
        assert cg._select_contract_tests(tmp_path) == []

    def test_required_files_join_even_off_glob_and_absent_ones_are_not_selected(self, tmp_path, monkeypatch):
        """登记进 `CONTRACT_REQUIRED_TESTS` 即入闸，不必匹配 glob（给别的 session 追加检查用）。
        不存在的必选文件这里不选——由闸在跑 pytest 之前单独报「缺必选」（见 TestRequiredAndExport）。"""
        monkeypatch.setattr(cg, "CONTRACT_REQUIRED_TESTS",
                            ("tests/test_orchestrator_braced_vars.py", "tests/test_absent.py"))
        t = tmp_path / "tests"
        t.mkdir()
        (t / "test_orchestrator_braced_vars.py").write_text("")
        (t / "test_step_contract.py").write_text("")
        assert cg._select_contract_tests(tmp_path) == [
            "tests/test_orchestrator_braced_vars.py",
            "tests/test_step_contract.py",
        ]

    def test_real_repo_selection(self):
        """真仓库：必选下限一个不少、都在、都被选中；且选不到本闸自己的测试（否则每次推送都自我递归地多跑一遍）。

        「只许变大」：下限是 v0.45.355 的四个契约文件，别的 session 可以往常量里追加，不许删。"""
        baseline = {
            "tests/test_step_contract.py",
            "tests/test_step_contract_ic_rerun.py",
            "tests/test_step_contract_producers.py",
            "tests/test_orchestrator_steps.py",
        }
        assert baseline <= set(cg.CONTRACT_REQUIRED_TESTS), (
            f"必选下限被删了：{sorted(baseline - set(cg.CONTRACT_REQUIRED_TESTS))}")
        absent = [f for f in cg.CONTRACT_REQUIRED_TESTS if not (_ROOT / f).is_file()]
        assert absent == [], f"真仓库里缺必选契约测试（每次改代码的推送都会被拦）：{absent}"
        chosen = cg._select_contract_tests(_ROOT)
        assert set(cg.CONTRACT_REQUIRED_TESTS) <= set(chosen), (
            f"必选契约测试没被选中：{sorted(set(cg.CONTRACT_REQUIRED_TESTS) - set(chosen))}")
        assert not [c for c in chosen if "changelog_guard" in c], chosen


# ═════════════════════════════ 推送端到端 ═════════════════════════════

class TestPushGate:

    def test_green_contract_passes_and_runs_in_an_export_of_the_pushed_commit(self, repo):
        """正对照：闸真的跑了（桩记了一次）、跑在导出树里（不是工作区）、跑完清掉了导出树。"""
        head = repo.commit({"app.py": GOOD_APP + "# 无害改动\n"}, "改代码")
        r = _push(repo)
        assert r.returncode == 0, r.stderr
        assert repo.origin_main() == head
        runs = repo.runs()
        assert len(runs) == 1, f"推了改代码的提交，契约闸却没跑：{r.stderr}"
        assert Path(runs[0]["root"]) != repo.path.resolve(), "契约测试跑在工作区里，而不是被推提交的导出树里"
        assert "契约测试通过" in r.stderr and "用时" in r.stderr, r.stderr
        assert repo.leftover_exports() == [], "导出树没清掉"

    def test_broken_contract_blocks_the_push_and_names_the_test(self, repo):
        before = repo.origin_main()
        repo.commit({"app.py": BROKEN_APP}, "删键")
        r = _push(repo)
        assert r.returncode != 0, "契约测试红了，推送却被放行了"
        assert "CONTRACT-BROKEN" in r.stderr, f"被拦了，但不是被契约测试拦的：{r.stderr}"
        assert "tests/test_step_contract.py::test_app_keys" in r.stderr, "拦截信息没点名失败的测试"
        assert "--no-verify" in r.stderr, "拦截信息没说紧急情况怎么绕过"
        assert repo.origin_main() == before
        assert len(repo.runs()) == 1
        assert repo.leftover_exports() == []

    def test_a_new_contract_file_joins_via_the_glob(self, repo):
        """新增 `tests/test_step_contract_<x>.py` 不改本闸就入闸（红的那个被点名）。"""
        repo.commit({"tests/test_step_contract_extra.py":
                     "def test_extra():\n    assert False, 'EXTRA-CONTRACT-RED'\n"}, "新契约测试")
        r = _push(repo)
        assert r.returncode != 0, r.stderr
        assert "EXTRA-CONTRACT-RED" in r.stderr
        assert "tests/test_step_contract_extra.py::test_extra" in r.stderr
        assert len(repo.runs()) == 1, "原有的契约测试也应当照跑"

    def test_the_pushed_commit_is_tested_not_the_worktree(self, repo):
        """最危险的一种：被推的那份坏了、工作区那份是好的 —— 在工作区里测就会测好的、放坏的。"""
        before = repo.origin_main()
        repo.commit({"app.py": BROKEN_APP}, "删键")
        (repo.path / "app.py").write_text(GOOD_APP)           # 工作区修好了，但没提交
        r = _push(repo)
        assert r.returncode != 0, "被推提交的契约是坏的，却因为工作区是好的而被放行"
        assert "CONTRACT-BROKEN" in r.stderr, r.stderr
        assert repo.origin_main() == before

    def test_a_dirty_worktree_does_not_block_a_good_pushed_commit(self, repo):
        """反方向：被推的好、工作区坏（未提交）⇒ 放行。不然每个脏工作区都得 --no-verify。"""
        head = repo.commit({"app.py": GOOD_APP + "# x\n"}, "改代码")
        (repo.path / "app.py").write_text(BROKEN_APP)
        (repo.path / "stray 2.py").write_text("raise SystemExit('iCloud 副本')\n")   # 未跟踪的 .py
        r = _push(repo)
        assert r.returncode == 0, r.stderr
        assert repo.origin_main() == head
        assert len(repo.runs()) == 1

    def test_no_contract_tests_is_a_failure_not_a_silent_pass(self, repo):
        before = repo.origin_main()
        gone = {f: None for f in {*cg.CONTRACT_REQUIRED_TESTS, "tests/test_step_contract.py"}}
        repo.commit({**gone, "app.py": GOOD_APP + "# x\n"}, "删光契约测试")
        r = _push(repo)
        assert r.returncode != 0, "被推提交里一条契约测试都没有，推送却被放行了"
        assert "找不到契约测试" in r.stderr, r.stderr
        assert "--no-verify" in r.stderr
        assert repo.runs() == []
        assert repo.origin_main() == before
        assert repo.leftover_exports() == []

    def test_push_to_another_branch_does_not_run_the_gate(self, repo):
        repo.commit({"app.py": BROKEN_APP}, "草稿")
        r = _push(repo, "HEAD:refs/heads/wip")
        assert r.returncode == 0, r.stderr
        assert repo.runs() == []

    def test_data_only_push_to_main_does_not_run_the_gate_even_over_a_broken_base(self, repo):
        """每日日报推送（只含报告产物）不跑闸——哪怕 main 上的契约此前已被 `--no-verify` 推坏。

        拦它修不好任何东西（区间没动代码 ⇒ 结果与 base 相同），只会让无人值守的日报推送失败、网站停更。"""
        repo.commit({"app.py": BROKEN_APP}, "绕过推坏")
        assert repo.git("push", "--no-verify", "origin", "HEAD:main", check=False).returncode == 0
        head = repo.commit({"report.json": '{"day": 2}', "alpha-hive-daily-2026-09-28.json": "{}"}, "日报")
        r = _push(repo)
        assert r.returncode == 0, r.stderr
        assert repo.origin_main() == head
        assert repo.runs() == []

    def test_uncomputable_range_runs_the_gate(self, repo):
        """远端还没有 main、本地也没有它的 tracking ref ⇒ 算不出区间 ⇒ 按「动了」跑。"""
        fresh = repo.tmp / "fresh.git"
        repo.git("init", "-q", "--bare", "-b", "main", str(fresh), cwd=repo.tmp)
        repo.git("remote", "add", "fresh", str(fresh))
        r = _push(repo, remote="fresh")
        assert r.returncode == 0, r.stderr
        assert len(repo.runs()) == 1, f"算不出区间时没跑闸：{r.stderr}"

    @pytest.mark.parametrize("var, value", [
        ("PYTEST_ADDOPTS", "-k 谁也不匹配"),                 # 不清掉 ⇒ 契约测试被静默摘光
        ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1"),            # 不清掉 ⇒ pyproject 的 --timeout 不认识 ⇒ pytest 用法错
    ])
    def test_hostile_pytest_env_is_not_inherited(self, repo, monkeypatch, var, value):
        (repo.path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ["--timeout=60"]\n')
        head = repo.commit({"app.py": GOOD_APP + "# x\n"}, "改代码")
        monkeypatch.setenv(var, value)
        r = _push(repo)
        assert r.returncode == 0, r.stderr
        assert repo.origin_main() == head
        assert len(repo.runs()) == 1

    @pytest.mark.parametrize("stub, reason", [
        ("import pytest\ndef test_x():\n    pytest.skip('没环境')\n", "被跳过"),
        ("import pytest\n@pytest.mark.network\ndef test_x():\n    pass\n", "被 -m 摘掉"),
        ("import pytest\n@pytest.mark.integration\ndef test_x():\n    pass\n", "被 -m 摘掉"),
        ("def test_x(:\n    pass\n", "收集出错"),
        # 收集期的整文件 skip：没有 runtest 报告、pytest 退出码 0（v0.45.355 复核实测曾被报「通过」）
        ("import pytest\npytest.skip('没环境', allow_module_level=True)\n\n\ndef test_x():\n    assert False\n",
         "整个文件被跳过"),
        ("import pytest\npytest.importorskip('no_such_module_for_the_gate')\n\n\ndef test_x():\n    assert False\n",
         "整个文件被跳过"),
        # 一条测试都没有：不失败、不 skip、不 deselect —— 只有「每个文件至少 1 条通过」抓得到
        ("def helper():\n    return 1\n", "0 条通过"),
    ], ids=["skip", "network-marker", "integration-marker", "collect-error",
            "module-level-skip", "importorskip", "no-tests-in-file"])
    def test_a_contract_test_that_did_not_run_blocks(self, repo, stub, reason):
        """没跑 ≠ 没问题：被跳过（含整文件）/ 被标记摘掉（离线闸里跑不到）/ 收集不起来 / 文件里没有测试，
        都拦，且说清是哪种。"""
        before = repo.origin_main()
        repo.commit({"tests/test_step_contract_more.py": stub}, "契约测试没跑")
        r = _push(repo)
        assert r.returncode != 0, f"{reason} 的契约测试被当成通过了"
        assert reason in r.stderr, r.stderr
        assert "tests/test_step_contract_more.py" in r.stderr, "拦截信息没点名那个文件"
        assert repo.origin_main() == before


class TestEveryFileMustPass:

    def test_tests_silently_dropped_by_a_conftest_block(self, repo):
        """conftest 的 `pytest_collection_modifyitems` 悄悄删掉一个契约文件的全部测试：不留失败 / skip /
        deselect 记录，pytest 退出码 0 —— 只有「每个被选中的文件至少 1 条通过」抓得到。"""
        before = repo.origin_main()
        repo.commit({
            "tests/test_step_contract_more.py": "def test_x():\n    assert False, 'NEVER-RUN'\n",
            "tests/conftest.py": ("def pytest_collection_modifyitems(items):\n"
                                  "    items[:] = [i for i in items if 'more' not in i.nodeid]\n"),
        }, "藏测试")
        r = _push(repo)
        assert r.returncode != 0, f"一个契约文件的测试全被 conftest 删掉，推送却被放行了：{r.stderr}"
        assert "tests/test_step_contract_more.py：0 条通过" in r.stderr, r.stderr
        assert "NEVER-RUN" not in r.stderr, "前提：那条测试不该跑（否则是被失败拦下的，测不到本条）"
        assert len(repo.runs()) == 1, "别的契约测试应当照跑"
        assert repo.origin_main() == before


# ═════════════════════════════ 必选下限与导出树完整性 ═════════════════════════════

class TestRequiredAndExport:

    def test_deleting_required_contract_files_blocks(self, repo):
        """复核实测 rc 0 的那种：删掉两个契约文件，glob 少选两个、剩下的照跑照绿。"""
        before = repo.origin_main()
        gone = ["tests/test_orchestrator_steps.py", "tests/test_step_contract_producers.py"]
        repo.commit({g: None for g in gone}, "删两个契约文件")
        r = _push(repo)
        assert r.returncode != 0, f"删了必选契约测试，推送却被放行了：{r.stderr}"
        for g in gone:
            assert f"缺必选契约测试（CONTRACT_REQUIRED_TESTS）：{g}（被推提交里没有这个文件）" in r.stderr, r.stderr
        assert "--no-verify" in r.stderr
        assert repo.runs() == [], "缺必选文件应当在跑 pytest 之前就拦"
        assert repo.origin_main() == before
        assert repo.leftover_exports() == []

    @pytest.mark.parametrize("touch_code", [True, False], ids=["plus-a-py-change", "gitattributes-only"])
    def test_export_ignore_cannot_hide_required_contract_files(self, repo, touch_code):
        """复核实测 rc 0 的两种：`.gitattributes` export-ignore 两个契约文件 + 改一行 `.py`；
        以及只改 `.gitattributes`（当时连闸都不触发）。"""
        before = repo.origin_main()
        hidden = ["tests/test_orchestrator_steps.py", "tests/test_step_contract_producers.py"]
        files = {".gitattributes": "".join(f"{h} export-ignore\n" for h in hidden)}
        if touch_code:
            files["app.py"] = GOOD_APP + "# x\n"
        repo.commit(files, "export-ignore")
        r = _push(repo)
        assert r.returncode != 0, f"export-ignore 藏掉了必选契约测试，推送却被放行了：{r.stderr}"
        for h in hidden:
            assert f"{h}（被推提交里有，但被 export-ignore 挡在导出树外）" in r.stderr, r.stderr
        assert "导出树缺了被推提交里的 2 个文件" in r.stderr, r.stderr
        assert repo.runs() == []
        assert repo.origin_main() == before
        assert repo.leftover_exports() == []

    def test_export_ignore_cannot_hide_a_non_required_contract_file(self, repo):
        """不在必选清单里的新契约文件被 export-ignore：glob 在导出树里选不到它，闸照样绿——
        必选清单管不到它，要靠「导出树与被推提交逐文件一致」。"""
        before = repo.origin_main()
        repo.commit({"tests/test_step_contract_extra.py": "def test_extra():\n    assert False, 'HIDDEN-RED'\n",
                     ".gitattributes": "tests/test_step_contract_extra.py export-ignore\n"}, "藏一个红的")
        r = _push(repo)
        assert r.returncode != 0, f"被 export-ignore 藏起来的红契约测试被放行了：{r.stderr}"
        assert "导出树缺了被推提交里的 1 个文件" in r.stderr, r.stderr
        assert "tests/test_step_contract_extra.py" in r.stderr
        assert repo.runs() == []
        assert repo.origin_main() == before

    def test_export_gaps_is_empty_for_a_plain_commit(self, repo, monkeypatch, tmp_path):
        """正对照：没有 export-ignore 时导出树与提交逐文件一致（不然上面几条可能是误报在拦），
        且核对真的逐文件在看（删掉导出树里一个嵌套文件 ⇒ 点名它）。"""
        sha = repo.commit({"app.py": GOOD_APP + "# x\n", "deep/nested dir/f.txt": "x"}, "普通提交")
        monkeypatch.chdir(repo.path)
        tree = tmp_path / "export"
        tree.mkdir()
        assert cg._export_commit("pre-push", sha, tree)
        assert cg._export_gaps("pre-push", sha, tree) == []
        (tree / "deep" / "nested dir" / "f.txt").unlink()
        assert cg._export_gaps("pre-push", sha, tree) == ["deep/nested dir/f.txt"]


# ═════════════════════════════ 子进程环境 ═════════════════════════════

class TestGateEnvironment:

    def test_inherited_pythonpath_cannot_supply_a_module_missing_from_the_pushed_commit(self, repo, monkeypatch):
        """复核实测 rc 0：被推提交缺一个模块（忘了 git add），工作区里有，PYTHONPATH 指着工作区 ⇒
        导出树里的测试从工作区 import 到它 ⇒ 「测的就是推的」落空。"""
        before = repo.origin_main()
        repo.commit({"tests/test_step_contract_helper.py":
                     "import gate_helper\n\n\ndef test_helper():\n    assert gate_helper.OK\n"}, "忘了 add 辅助模块")
        (repo.path / "gate_helper.py").write_text("OK = True\n")     # 只在工作区，被推提交里没有
        monkeypatch.setenv("PYTHONPATH", str(repo.path))
        r = _push(repo)
        assert r.returncode != 0, f"被推提交缺模块，却因为 PYTHONPATH 指向工作区而被放行：{r.stderr}"
        assert "收集出错：tests/test_step_contract_helper.py" in r.stderr, r.stderr
        assert "gate_helper" in r.stderr, r.stderr
        assert repo.origin_main() == before

    def test_python_interpreter_vars_are_not_inherited(self, monkeypatch, tmp_path):
        """PYTHONHOME / PYTHONSTARTUP 走不了真推送（钩子自己的解释器也会继承），在环境构造处直接核对。"""
        for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            monkeypatch.setenv(var, str(tmp_path / "worktree"))
        env = cg._contract_env(tmp_path / "plugin", tmp_path / "report.json")
        assert env["PYTHONPATH"] == str(tmp_path / "plugin"), "PYTHONPATH 只许有本闸的结果插件"
        assert "PYTHONHOME" not in env and "PYTHONSTARTUP" not in env, sorted(env)
        assert env["ALPHA_HIVE_CONTRACT_GATE_REPORT"] == str(tmp_path / "report.json")


# ═════════════════════════════ 与 CHANGELOG 部分的组合 ═════════════════════════════

class TestChangelogMismatchStillRunsTheGate:

    @pytest.mark.parametrize("app, gate_says", [
        (BROKEN_APP, "CONTRACT-BROKEN"),
        (GOOD_APP + "# x\n", "契约测试通过"),
    ], ids=["contract-red", "contract-green"])
    def test_changelog_differing_from_worktree_still_runs_the_gate(self, repo, app, gate_says):
        """「CHANGELOG.md 与工作区不同」曾直接 return 1、契约闸不跑 ⇒ 修完 CHANGELOG 再推才发现契约也红。
        契约绿的那一例确认：闸通过不会把 CHANGELOG 部分的拦截洗成放行。"""
        before = repo.origin_main()
        repo.commit({"CHANGELOG.md": "# log\n\n## [2] — x\n\nbody\n\n## [1] — y\n\nbody\n", "app.py": app},
                    "两件事", "--no-verify")
        (repo.path / "CHANGELOG.md").write_text("# log\n\n## [1] — y\n\nbody\n")   # 工作区 ≠ 被推提交
        r = _push(repo)
        assert r.returncode != 0, r.stderr
        assert "与工作区不同" in r.stderr, r.stderr
        assert gate_says in r.stderr, f"CHANGELOG 部分拦了之后契约闸没跑：{r.stderr}"
        assert len(repo.runs()) == 1
        assert repo.origin_main() == before


# ═════════════════════════════ 触发范围 ═════════════════════════════

def _daily_artifact_samples():
    """从 `report_deployer` 的白名单常量（AST 读，不 import）造出每一类日报产物的样例路径。"""
    tree = ast.parse((_ROOT / "report_deployer.py").read_text(encoding="utf-8"))
    consts = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id in ("_ARTIFACT_PREFIXES", "_ARTIFACT_GLOBS", "_ARTIFACT_EXACT"):
                    consts[t.id] = ast.literal_eval(node.value)
    assert set(consts) == {"_ARTIFACT_PREFIXES", "_ARTIFACT_GLOBS", "_ARTIFACT_EXACT"}, (
        f"report_deployer 的白名单常量改名了（只找到 {sorted(consts)}）——本测试要跟着改，别让它空转")
    samples = list(consts["_ARTIFACT_EXACT"])
    samples += [g.replace("*", "2026-09-28") for g in consts["_ARTIFACT_GLOBS"]]
    for pre in consts["_ARTIFACT_PREFIXES"]:
        samples += [f"{pre}sample.json", f"{pre}nested/sample.md", f"{pre}sample.html"]
    assert len(samples) >= 15, samples
    return samples


class TestTrigger:

    @pytest.fixture
    def plain_repo(self, tmp_path, monkeypatch):
        for var in cg._GIT_LOCATION_VARS:
            monkeypatch.delenv(var, raising=False)
        r = tmp_path / "r"
        subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True)
        monkeypatch.chdir(r)                      # cg._git 跑在 cwd 上

        def commit(paths):
            for p in paths:
                (r / p).parent.mkdir(parents=True, exist_ok=True)
                (r / p).write_text(f"{p}\n")
            subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                           cwd=r, check=True, capture_output=True)
            return subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True,
                                  text=True, check=True).stdout.strip()
        commit(["seed.txt"])
        return commit

    def test_daily_report_artifacts_never_trigger_the_gate(self, plain_repo):
        """无人值守的日报推送若触发契约闸，契约一红网站就停更。变红的变异：往触发集里加 `*.json` / `*.md`。"""
        base = plain_repo(["seed2.txt"])
        samples = _daily_artifact_samples()
        head = plain_repo(samples)                 # 一次提交全部样例（逐条提交要 ~5s）
        offenders = subprocess.run(["git", "diff", "--name-only", base, head, "--", *cg.CONTRACT_TRIGGER_PATHSPECS],
                                   capture_output=True, text=True, check=True).stdout.split("\n")
        assert [o for o in offenders if o] == [], "这些日报产物会触发契约闸"
        assert subprocess.run(["git", "diff", "--name-only", base, head], capture_output=True, text=True,
                              check=True).stdout.count("\n") == len(set(samples)), "前提：样例都进了这次提交"
        assert cg._contract_triggered("pre-push", base, head) is False

    @pytest.mark.parametrize("path", [
        "step_contract.py", "pkg/tool.py", "tests/test_step_contract.py", "tests/fixtures/out.json",
        "scripts/alpha-hive-orchestrator.sh", "pyproject.toml", "skills/weekly/SKILL.md",
        # export-ignore 能改变导出树，而它不是代码；嵌套的那个放在别的规则都不匹配的目录里（tests/ 会被 `tests/` 兜住）
        ".gitattributes", "docs/.gitattributes",
    ])
    def test_code_changes_trigger_the_gate(self, plain_repo, path):
        """正对照：上一条要是因为「什么都不触发」而绿，这里就红。"""
        base = plain_repo(["seed2.txt"])
        head = plain_repo([path])
        assert cg._contract_triggered("pre-push", base, head) is True, path

    def test_uncomputable_base_triggers(self):
        assert cg._contract_triggered("pre-push", "", "deadbeef") is True


# ═════════════════════════════ 超时与薄调用 ═════════════════════════════

class TestTimeoutAndShim:

    def test_timeout_blocks_loudly(self, repo, monkeypatch, capsys):
        """跑不完 ≠ 通过。进程内直接调（钩子子进程里改不了超时常量）。"""
        sha = repo.commit({"tests/test_step_contract_slow.py":
                           "import time\ndef test_slow():\n    time.sleep(60)\n"}, "慢")
        monkeypatch.chdir(repo.path)
        monkeypatch.setattr(cg, "CONTRACT_TIMEOUT_S", 2)
        assert cg._run_contract_gate("pre-push", sha) == 1
        err = capsys.readouterr().err
        assert "没跑完" in err and "未被检查" in err, err
        assert repo.leftover_exports() == []

    def test_shim_is_unchanged_so_installed_hooks_need_no_reinstall(self):
        """契约闸靠「已装的 pre-push 薄调用 = exec changelog_guard.py --pre-push」自动生效。

        改了 `_SHIM` 就意味着**每个** checkout（主 checkout / 生产 / 所有 worktree 共用的 .git/hooks）都要重跑
        `/usr/local/bin/python3 changelog_guard.py --install-hook`，而旧钩子不会告诉任何人它旧了。
        真要改：更新下面的摘要，并在 CHANGELOG 里写明「需要重装钩子」。"""
        digests = {n: hashlib.sha256(b.encode("utf-8")).hexdigest() for n, b in cg.HOOKS.items()}
        assert digests == {
            "pre-commit": "7e44eaddfa9602a086af391d22ab8dda15a0d70474fe1e386486c1bfec73eda3",
            "pre-push": "ba6b3942f418c5c84f92a4fa7a1ea6091074c65921318ad0db44103c4be86664",
        }
        assert '"$SCRIPT" --pre-push "$@"' in cg.HOOKS["pre-push"]
