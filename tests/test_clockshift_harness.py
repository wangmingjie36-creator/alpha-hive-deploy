"""拨钟 harness（`tools/clockshift/`）自己的守卫（v0.45.495）。

harness 是找时间炸弹的尺子；尺子歪了，审计的全绿什么也证明不了。v0.45.495 之前它有两个根因不同的盲区：
**R1** 拨钟只在 pytest 进程内（子进程读真钟），**R2** 只拨 `datetime`、不拨 `time.time()` 与文件 mtime
（「现在」与「刚写的文件」差 N 天）。+30 天全套件 41 条假红全来自这两条。本文件在一个临时目录里造一份
迷你测试集，用**真实子进程 pytest** 跑 harness，逐条钉住：

- 写死日期的炸弹：真实钟绿、拨钟红（harness 确实拨到了被测代码）；
- R2：拨钟下刚写的文件 mtime 与「现在」同一天、`utime` 显式时间往返不变；
- R1：自建 env、`env -i`、经 bash 的孙进程，三种子进程都拿到拨后的日期；
- 第三方库导入安全：拨钟下 import pandas 不段错误、`pd.Timestamp.now()` 与 `datetime.now()` 一致；
- `audit.py`：差分只报「拨钟红而基线绿」；某一轮跑崩时退出码 2，不报成「0 条新红」。

**纪律。** 每条断言写明什么变异让它变红。全部离线；外层自己若正被 harness 拨着（CI 的拨钟审计会跑到本文件），
「真实今天」按外层偏移换算，不受影响。
"""
import datetime as dt
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent / "tools" / "clockshift"
_spec = importlib.util.spec_from_file_location("clockshift_audit", HERE / "audit.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

SHIFT = 7


def _real_today():
    return dt.date.today() - dt.timedelta(days=float(os.environ.get("CLOCK_SHIFT_DAYS") or 0))


def _write_suite(d: Path, bomb_in_days=3):
    bomb = (_real_today() + dt.timedelta(days=bomb_in_days)).isoformat()
    (d / "test_inner.py").write_text(textwrap.dedent(f'''
        import datetime, logging, os, subprocess, sys, tempfile, time
        import pytest

        def test_bomb():
            assert datetime.date.today() < datetime.date.fromisoformat("{bomb}")

        def test_fresh_file_mtime_is_now():
            fd, p = tempfile.mkstemp(); os.close(fd)
            try:
                assert datetime.date.fromtimestamp(os.stat(p).st_mtime) == datetime.date.today()
                assert abs(time.time() - datetime.datetime.now().timestamp()) < 60
                os.utime(p, (1_000_000_000.0, 1_000_000_000.0))
                assert os.stat(p).st_mtime == 1_000_000_000.0
            finally:
                os.unlink(p)

        @pytest.mark.external_clock
        def test_external_state():
            assert os.environ.get("CLOCK_SHIFT_DAYS") in (None, "0", "0.0"), "拨钟下本条应被摘掉、不该跑到"

        def test_logging_timestamp_is_shifted():
            r = logging.makeLogRecord({{"msg": "x"}})
            assert logging.Formatter("%(asctime)s").format(r).startswith(str(datetime.date.today()))

        def _child(argv, **kw):
            return subprocess.run(argv, capture_output=True, text=True, check=True, **kw).stdout.strip()

        CODE = "import datetime; print(datetime.date.today())"

        def test_child_env_from_scratch():
            assert _child([sys.executable, "-c", CODE], env={{"PATH": os.environ["PATH"]}}) == str(datetime.date.today())

        def test_child_env_i():
            assert _child(["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", sys.executable, "-c", CODE]) == str(datetime.date.today())

        def test_grandchild_via_bash():
            assert _child(["/bin/bash", "-c", f'"{{sys.executable}}" -c "{{CODE}}"']) == str(datetime.date.today())
    '''))
    return bomb


def _run(d: Path, days, *args):
    env = {**os.environ, "CLOCK_SHIFT_DAYS": str(days), "PYTHONPATH": str(HERE)}
    env.pop("CLOCK_SHIFT_FORCE", None)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA",
                           "--rootdir", str(d), *args, str(d / "test_inner.py")],
                          cwd=d, env=env, capture_output=True, text=True)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    d = tmp_path_factory.mktemp("clockshift_suite")
    _write_suite(d)
    return {days: _run(d, days, *(["-p", "clockshift"] if days else [])) for days in (0, SHIFT)}


def _failed(proc):
    bad, _ = audit.parse(proc.stdout)
    return {n.split("::")[-1] for n in bad}


def _passed(proc):
    """正面取「确实跑了且通过」的——只判「不在失败集合里」，子 pytest 整轮没跑起来时会空洞地绿。"""
    return {ln.split("::")[-1].strip() for ln in proc.stdout.splitlines() if ln.startswith("PASSED ")}


class TestHarnessShiftsTheWholeWorld:

    def test_real_clock_all_green(self, runs):
        """真实钟下迷你测试集全绿（否则下面「拨钟红」的对照没有意义）。"""
        assert runs[0].returncode == 0, runs[0].stdout[-2000:]

    def test_bomb_goes_off_only_when_shifted(self, runs):
        """写死 3 天后的日期：拨 7 天必须红。变红的变异：harness 不装假 `datetime`（`install()` 提前 return）。"""
        assert _failed(runs[SHIFT]) >= {"test_bomb"}, runs[SHIFT].stdout[-2000:]

    def test_r2_fresh_file_and_utime_consistent(self, runs):
        """R2：拨钟下刚写的文件 mtime 与今天同一天；`utime` 显式时间往返不变。
        变红的变异：去掉 `os.stat` 的 +N、或去掉 `os.utime` 的 −N——`install()` 的启动自检先炸，子 pytest
        整轮起不来（实测）；自检若也被删，则本条断言自己红（mtime 是 7 天前 / 读回多 7 天）。"""
        assert "test_fresh_file_mtime_is_now" in _passed(runs[SHIFT]), runs[SHIFT].stdout[-2000:]

    def test_stdlib_holding_replacements_as_class_attrs_still_works(self, runs):
        """`logging.Formatter.converter = time.localtime` 是类属性：替身若是普通函数就会绑定 self ⇒ 日志格式化
        TypeError（v0.45.495 全套件实测因此 INTERNALERROR）。变红的变异：`_install_replacement` 不包 `staticmethod`
        ——启动自检先炸；自检也删则本条断言自己红。"""
        assert "test_logging_timestamp_is_shifted" in _passed(runs[SHIFT]), runs[SHIFT].stdout[-2000:]

    @pytest.mark.parametrize("name", ["test_child_env_from_scratch", "test_child_env_i", "test_grandchild_via_bash"])
    def test_r1_children_see_the_shifted_clock(self, runs, name):
        """R1：三种子进程形态都拿到拨后的日期。变红的变异：去掉 `Popen.__init__` 包装（自建 env / `env -i`
        两条红）；或删掉 `sitecustomize.py`（三条全红）。"""
        assert name in _passed(runs[SHIFT]), runs[SHIFT].stdout[-2000:]

    def test_only_the_bomb_is_red(self, runs):
        """拨钟下红的**恰好**只有炸弹、其余 5 条全过（external_clock 那条被摘掉）——harness 自己不制造任何红。"""
        assert _failed(runs[SHIFT]) == {"test_bomb"}, runs[SHIFT].stdout[-2000:]
        assert _passed(runs[SHIFT]) == _passed(runs[0]) - {"test_bomb", "test_external_state"}, runs[SHIFT].stdout[-2000:]
        assert len(_passed(runs[0])) == 7, runs[0].stdout[-2000:]

    def test_external_clock_tests_are_deselected_visibly(self, runs):
        """`external_clock`（读外部真实状态）拨钟下**摘掉**、摘要里可见；真实钟下照常跑。
        变红的变异：删掉 `pytest_collection_modifyitems`（本条在拨钟下跑到并红）。"""
        assert "test_external_state" in _passed(runs[0])
        assert "test_external_state" not in _passed(runs[SHIFT]) | _failed(runs[SHIFT]), runs[SHIFT].stdout[-2000:]
        assert "1 deselected" in runs[SHIFT].stdout, runs[SHIFT].stdout[-2000:]


def test_third_party_import_safe_and_pandas_clock_agrees():
    """拨钟下 import pandas / pydantic.v1 不段错误、不元类冲突，`pd.Timestamp.now()` 与 `datetime.now()` 一致。
    变红的变异：去掉导入钩子（`_SwapLoader` 不换回真类 ⇒ pandas 段错误 / pydantic.v1 元类冲突）；
    或把 pandas 的 `today` 也拨一遍（拨两次）。"""
    code = ("import datetime, pandas as pd, pydantic.v1\n"
            "a = pd.Timestamp.now().to_pydatetime(); b = datetime.datetime.now(); c = pd.Timestamp.today().to_pydatetime()\n"
            "assert abs((a - b).total_seconds()) < 60 and abs((c - b).total_seconds()) < 60, (a, b, c)\n"
            "print(datetime.date.today())")
    env = {**os.environ, "CLOCK_SHIFT_DAYS": str(SHIFT), "PYTHONPATH": str(HERE)}
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.strip() == str(_real_today() + dt.timedelta(days=SHIFT))


@pytest.mark.parametrize("days", [0, SHIFT])
def test_sitecustomize_chains_to_the_next_one(tmp_path, days):
    """本目录的 `sitecustomize` 不吞掉 `sys.path` 上排在它之后的那个（拨不拨钟都照常执行），且拨钟照装。
    这也是 `test_paths_not_frozen_at_import` 放行 `_chain_next_sitecustomize` 那条 `__file__` 路由的行为守卫：
    `here` 只用来在 sys.path 上认出本目录。变红的变异：`here` 改成别的目录（如 `os.getcwd()`，认不出自己 ⇒
    一个都不执行）；或删掉模块末尾那次调用。"""
    nxt = tmp_path / "next"
    nxt.mkdir()
    (nxt / "sitecustomize.py").write_text("import sys\nsys._clockshift_chained_ran = True\n")
    code = "import datetime, sys; print(getattr(sys, '_clockshift_chained_ran', False), datetime.date.today())"
    env = {**os.environ, "CLOCK_SHIFT_DAYS": str(days), "PYTHONPATH": os.pathsep.join([str(HERE), str(nxt)])}
    env.pop("CLOCK_SHIFT_FORCE", None)
    p = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.split() == ["True", str(_real_today() + dt.timedelta(days=days))], p.stdout + p.stderr[-2000:]


class TestAudit:

    def test_reports_only_new_reds(self, tmp_path, capsys):
        """差分：基线已红的不报，只报「拨钟红而基线绿」，且重跑后确认。变红的变异：不减基线；或重跑判据写反。"""
        _write_suite(tmp_path)
        (tmp_path / "test_always_red.py").write_text("def test_red():\n    assert False\n")
        base_bad, _, results = audit.audit([SHIFT], ["--rootdir", str(tmp_path), str(tmp_path)])
        assert {n.split("::")[-1] for n in base_bad} == {"test_red"}
        assert [n.split("::")[-1] for n in results[0]["confirmed"]] == ["test_bomb"]
        assert results[0]["flaky"] == []
        report = audit.render(base_bad, {}, results)
        assert "test_red" in report, "基线已红的要逐条列出（不计入 ≠ 看不见）"

    def test_crashed_run_is_invalid_not_green(self, tmp_path):
        """某一轮跑崩（这里：收集错误）⇒ `RunInvalid` / 退出码 2，不能报成「0 条新红」。
        变红的变异：删掉 `run_pytest` 里对退出码 / 零用例的检查。"""
        (tmp_path / "test_broken.py").write_text("import nonexistent_module_xyz\n")
        assert audit.main(["--days", str(SHIFT), "--", "--rootdir", str(tmp_path), str(tmp_path)]) == 2

    def test_parse_summary_lines(self):
        """只认 `short test summary info` 段：之前「Captured stdout」里的 `FAILED x` 不算（v0.45.495 实测被它骗过）。
        变红的变异：`parse` 扫全文。"""
        out = ("----- Captured stdout call -----\nFAILED ../nested_audit_output.py::t\n"
               "=========================== short test summary info ============================\n"
               "FAILED tests/a.py::T::t[x-1] - AssertionError: boom - more\n"
               "ERROR tests/b.py::u\nPASSED nothing\n"
               "==== 1 failed, 9 passed, 2 deselected, 1 error in 3.0s ====")
        bad, counts = audit.parse(out)
        assert bad == {"tests/a.py::T::t[x-1]", "tests/b.py::u"}
        assert counts == {"failed": 1, "passed": 9, "deselected": 2, "error": 1}
