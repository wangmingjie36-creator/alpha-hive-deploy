"""`tools/burn_cpu.py`（v0.45.668）：压力负载进程自带寿命、清理失败会红。

背景：10-27 手写的 `yes &` 负载清理无声失败，20 个进程空转 40 分钟。这里钉的是三件事，对应「泄漏要同时坏三处才发生」：

1. 正常路径：退出时负载进程全死（逐个核对，不是只发信号）。
2. **清理逻辑整个失效时，负载仍会自己停**：到截止时刻自己退出；父进程没了也自己退出（后者用 60 秒的截止证明——
   测试只等 20 秒，能过的只可能是「父进程不在」那条判据）。
3. 清理失败**会红**：残留 ⇒ `BurnerLeak` / 命令行退出码 4，点名 pid；不是静默。

所有测试在失败路径里都会自己 SIGKILL 残留的负载——测试不能成为下一次泄漏的来源。
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("burn_cpu_under_test", _TOOLS / "burn_cpu.py")
B = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = B
_spec.loader.exec_module(B)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_all_dead(pids, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return not any(_alive(p) for p in pids)


def _kill_stragglers(pids) -> None:
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class TestCleanup:
    def test_context_exit_leaves_nothing_alive(self):
        with B.Burners(procs=2, seconds=30) as b:
            pids = b.pids
            assert len(pids) == 2 and all(_alive(p) for p in pids), "负载进程应当在跑"
        assert not any(_alive(p) for p in pids), "退出后必须逐个核对为零残留"

    def test_cleanup_runs_even_when_the_body_raises(self):
        pids = []
        with pytest.raises(RuntimeError, match="body boom"):
            with B.Burners(procs=2, seconds=30) as b:
                pids = b.pids
                raise RuntimeError("body boom")
        assert pids and not any(_alive(p) for p in pids)


class TestSelfTermination:
    """清理逻辑整个失效时，负载靠自己停下来——这才是「最多到截止时刻、不是永远」。"""

    def test_children_exit_on_their_own_at_the_deadline(self):
        b = B.Burners(procs=2, seconds=1)
        b.__enter__()
        try:
            # 是本进程的子进程：没被 wait 的已退出进程是僵尸，`kill(pid, 0)` 对僵尸仍成功 ⇒ 这里必须用 poll（会回收）
            end = time.time() + 20
            while time.time() < end and b._alive():
                time.sleep(0.1)
            assert not b._alive(), "到截止时刻负载没有自己退出"
        finally:
            _kill_stragglers(b.pids)
            b._cleanup()

    def test_children_exit_when_the_parent_dies(self):
        """父进程没清理就死了（os._exit，不跑任何 finally）。截止时刻设成 60 秒、只等 20 秒 ⇒
        能过说明走的是「父进程不在了」那条判据，不是截止时刻。"""
        code = ("import os, sys\n"
                f"sys.path.insert(0, {str(_TOOLS)!r})\n"
                "from burn_cpu import Burners\n"
                "b = Burners(procs=2, seconds=60)\n"
                "b.__enter__()\n"
                "print(' '.join(map(str, b.pids)), flush=True)\n"
                "os._exit(0)\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        pids = [int(x) for x in r.stdout.split()]
        try:
            assert r.returncode == 0 and len(pids) == 2, r.stdout + r.stderr
            assert _wait_all_dead(pids, 20), f"父进程死后负载仍在空转：{[p for p in pids if _alive(p)]}"
        finally:
            _kill_stragglers(pids)


class TestFailureIsLoud:
    def test_leak_after_cleanup_raises_with_the_pids(self, monkeypatch):
        monkeypatch.setattr(B.Burners, "_signal_all", lambda self, sig: None)     # 模拟「清理逻辑整个失效」
        monkeypatch.setattr(B, "_TERM_GRACE", 0.2)
        pids = []
        try:
            with pytest.raises(B.BurnerLeak) as ei:
                with B.Burners(procs=2, seconds=30) as b:
                    pids = b.pids
            assert sorted(ei.value.pids) == sorted(pids), "要点名残留的 pid"
            assert str(pids[0]) in str(ei.value) and "kill -9" in str(ei.value)
        finally:
            _kill_stragglers(pids)

    @pytest.mark.parametrize("seconds", [0, -1, B.MAX_SECONDS + 1])
    def test_unbounded_lifetime_is_refused(self, seconds):
        with pytest.raises(ValueError, match="无期限"):
            B.Burners(procs=1, seconds=seconds)

    def test_zero_procs_is_refused(self):
        with pytest.raises(ValueError, match="procs"):
            B.Burners(procs=0, seconds=5)


class TestCli:
    @staticmethod
    def _record_burners(monkeypatch):
        made = []

        class Recording(B.Burners):
            def __enter__(self):
                made.append(self)
                return super().__enter__()

        monkeypatch.setattr(B, "Burners", Recording)
        return made

    def test_returns_the_commands_exit_code_and_cleans_up(self, monkeypatch):
        made = self._record_burners(monkeypatch)
        rc = B.main(["--procs", "1", "--seconds", "30", "--", sys.executable, "-c", "import sys; sys.exit(3)"])
        assert rc == 3
        assert len(made) == 1 and not any(_alive(p) for p in made[0].pids)

    def test_load_is_really_running_while_the_command_runs(self, monkeypatch):
        made = self._record_burners(monkeypatch)
        # 被压的命令自己断言「负载进程此刻活着」：在命令里读不到 Burners 对象，所以从 argv 传 pid 不现实——
        # 改为命令结束后核对「命令运行期间 Burners 确实被进入过且起了进程」
        rc = B.main(["--procs", "2", "--seconds", "30", "--", sys.executable, "-c", "import time; time.sleep(0.3)"])
        assert rc == 0 and len(made[0].pids) == 2

    def test_leak_gives_exit_code_4(self, monkeypatch, capsys):
        made = self._record_burners(monkeypatch)
        monkeypatch.setattr(B.Burners, "_signal_all", lambda self, sig: None)
        monkeypatch.setattr(B, "_TERM_GRACE", 0.2)
        try:
            rc = B.main(["--procs", "1", "--seconds", "30", "--", sys.executable, "-c", "pass"])
            assert rc == B.EXIT_LEAK == 4
            assert "仍有" in capsys.readouterr().err
        finally:
            for b in made:
                _kill_stragglers(b.pids)

    def test_missing_command_is_a_usage_error(self):
        with pytest.raises(SystemExit) as ei:
            B.main(["--procs", "1", "--"])
        assert ei.value.code == 2

    def test_bad_seconds_is_a_usage_error(self):
        with pytest.raises(SystemExit) as ei:
            B.main(["--seconds", "0", "--", sys.executable, "-c", "pass"])
        assert ei.value.code == 2
