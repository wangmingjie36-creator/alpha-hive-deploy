"""`ic_rerun_readiness` 的 F&G 子状态：子进程 + 时间预算 + 检查点 + SIGTERM 连带（v0.45.392）。

背景（见被测模块 docstring「F&G 子状态的时间预算与检查点」）：编排器 Step 11 在 60s 超时下跑本工具，09-30
F&G 重放拖过 60s ⇒ 进程被杀、`--out` 一次都没写 ⇒ 与 F&G 无关的世代边界核对也一起丢了。

三层，各管一段「谁会红」：

  · **父进程处置**（`TestChildOutcomes` / `TestMainFlow`）：进程内调 `_FgChild` / `main()`，子进程换成一段
    `python -c`（只测父进程怎么接）。起不来 / 超时 / 非 0 退出 / 被信号杀 / 输出不合约定 / TERM 不理
    ——每条都落成 F&G 段的 `cannot_judge` + 专属 attention id，不抛、不吞；检查点在等 F&G **之前**写出、
    键序与最终结果相同；子进程在 `main()` 其余各项**之前**就起（并行）；`with` 退出连带终止子进程。
  · **真实进程树**（`TestRealProcessTree`）：把仓库里的 `ic_rerun_readiness.py` / `step_contract.py`
    **原样**拷进 tmp，旁边放一个**假的** `experiments/fg_exposure_gate_forward_test.py`（会挂住 / 往 stdout 乱写）。
    父子进程都是生产代码，只有 F&G 执行器是假的：SIGTERM 父进程 ⇒ 退出码 143、`--out` 是检查点、子进程已死；
    SIGKILL 父进程 ⇒ 孤儿子进程靠看门狗自己退出；执行器往 fd 1 乱写不污染结果。
  · **消费方**（`TestOrchestratorReadsIt` / `TestBudgetFitsOrchestratorTimeout`）：编排器解释器
    `orchestrator_steps.render` 读检查点 / 超预算 JSON 的样子；缺省预算放得进编排器 Step 11 的超时（读仓库编排器原文）。

离线：进程内部分受 conftest 出网闸约束；子进程部分经 `PYTHONPATH` 注入一个 `sitecustomize` 闸（记账 + 拒绝），
两层都钩：Python `socket.connect`（非 AF_UNIX 一律拒，**含回环**——本机代理在 127.0.0.1，放行回环 = 放行外网）与
`curl_cffi.Curl.perform`（yfinance 的 libcurl 在 C 层开 socket，Python socket 钩子看不见它）；代理再指向不监听的
127.0.0.1:9 兜底。每条真实进程树测试结束断言账本为空；闸本身先用 canary（子进程 + 孙进程、socket + curl_cffi
两条路）反向自证会记账。临时目录：父进程 / 子进程的「系统临时目录」在每条测试里都圈进 tmp，结束断言为空。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

import ic_rerun_readiness as rr
import orchestrator_steps as osteps
import step_contract as sc
from tests._orchestrator import repo_orchestrator_text
from tests.test_dim_ic_forward_test import _check_quiet_segments
from tests.test_step_contract_ic_rerun import _assert_icon_consistent, _make_db

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
TODAY = "2026-10-01"
FG = "ic_rerun.fg_exposure_gate_forward"
GOOD = {"status": "not_ready", "line": "⏳ F&G 敞口门前瞻检验：3/15 个合格周",
        "_detail": {"status": "not_ready", "weeks": 3, "next_look_at": 15}}


def _gone(pid: int, within: float = 6.0) -> bool:
    """`pid` 在 `within` 秒内消失（被收尸）⇒ True。"""
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _kill_if_ours(pid: int, marker: str) -> None:
    """`pid` 还活着且命令行里带 `marker`（本条测试的代码副本路径）⇒ KILL。核命令行防 pid 被复用后误杀别人。"""
    if pid <= 0:
        return
    # -ww：环境里有 COLUMNS 时 procps 按它截管道输出（CI 上会截），marker 是长临时路径 ⇒ 可能被截掉、静默不收尸
    r = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    if marker in r.stdout:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _wait_for(path: Path, within: float = 20.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.02)
    return False


def _fake_child(monkeypatch, code: str) -> dict:
    """`_FgChild` 的子进程命令换成一段 python（这一层只测**父进程**的处置，不经子进程入口 `_fg_child_main`）。
    返回的 dict 记下父进程传来的 `today` / `max_seconds`。"""
    seen: dict = {}

    def _argv(today, max_seconds):
        seen.update(today=today, max_seconds=max_seconds)
        return [sys.executable, "-c", textwrap.dedent(code)]

    monkeypatch.setattr(rr, "_fg_child_argv", _argv)
    return seen


@pytest.fixture
def scoped_sys_tmp(tmp_path, monkeypatch):
    """把本进程的「系统临时目录」圈进 tmp（`_FgChild` 的私有 TMPDIR 建在这里），结束断言为空：
    不论哪条出路，子进程的临时目录都由父进程删掉——漏删的变异在这里红，也不会把垃圾留进真实的系统临时目录。"""
    root = tmp_path / "sysT"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    # 环境变量也圈上：被测代码若没把私有 TMPDIR 交给子进程（变异 M12 实测），子进程继承的是这里而不是真实系统临时目录
    monkeypatch.setenv("TMPDIR", str(root))
    yield root
    left = sorted(x.name for x in root.iterdir())
    assert not left, f"F&G 子进程的临时目录没删干净：{left}"


def _print_good() -> str:
    return f"import json, sys\nsys.stdout.write(json.dumps({GOOD!r}, ensure_ascii=False))\n"


# ═══════════════════════════════ 父进程处置（进程内） ═══════════════════════════════

class TestChildOutcomes:
    """`_FgChild.result()` 的每条出路都是一个 F&G 段：成功原样转交，其余全部 `cannot_judge` + `_detail.runner`。

    每条之后系统临时目录（这里圈在 tmp 里）必须是空的：子进程的私有 `TMPDIR` 不论哪条出路都由父进程删掉。"""

    @pytest.fixture(autouse=True)
    def _scoped(self, scoped_sys_tmp):
        yield

    def _run(self, monkeypatch, code, deadline_in=10.0):
        _fake_child(monkeypatch, code)
        with rr._FgChild("2026-10-01", None if deadline_in is None else time.monotonic() + deadline_in) as h:
            return h.result(), h

    def test_success_passes_result_through(self, monkeypatch):
        res, _ = self._run(monkeypatch, _print_good())
        assert res == GOOD

    @pytest.mark.parametrize("payload,why", [
        ("", "空输出"),
        ("not json at all", "不是 JSON"),
        ("[1, 2]", "顶层不是对象"),
        ('{"status": "not_ready"}', "缺 line"),
        ('{"status": 1, "line": "x"}', "status 不是字符串"),
        ('{"status": "x", "line": "y", "_detail": [1]}', "_detail 不是对象"),
        ('{"status": "x", "line": "y", "extra": 1}', "多出键（会原样进 --out）"),
    ])
    def test_malformed_output_is_child_failed(self, monkeypatch, payload, why):
        res, _ = self._run(monkeypatch, f"import sys\nsys.stdout.write({payload!r})\n")
        assert res["status"] == "cannot_judge", why
        assert res[rr._DETAIL_KEY]["runner"] == "child_failed", why
        assert res["line"].startswith("⚠️ F&G 敞口门前瞻检验无法判定：F&G 子进程输出不合约定"), (why, res["line"])

    def test_nonzero_exit_is_failure_even_with_valid_json(self, monkeypatch):
        """先打了合法结果再以 3 退出：退出码说了算（子进程自己说它没成）。"""
        res, _ = self._run(monkeypatch, _print_good() + "sys.exit(3)\n")
        assert res[rr._DETAIL_KEY]["runner"] == "child_failed"
        assert "退出码 3" in res["line"]

    def test_killed_by_signal_is_named(self, monkeypatch):
        res, _ = self._run(monkeypatch, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
        assert res[rr._DETAIL_KEY]["runner"] == "child_failed"
        assert "被信号 9 终止" in res["line"]

    def test_spawn_failure_is_child_failed_not_raise(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rr, "_fg_child_argv", lambda today, ms: [str(tmp_path / "no-such-python")])
        with rr._FgChild(None, time.monotonic() + 5) as h:
            assert h.proc is None
            res = h.result()
        assert res[rr._DETAIL_KEY]["runner"] == "child_failed"
        assert "无法启动 F&G 子进程" in res["line"]

    def test_finished_child_is_kept_when_only_the_parent_overran(self, monkeypatch):
        """二次检查实测的 bug：子进程按时跑完，是父进程自己的部分（评估 / 边界核对 / 写检查点）拖过了预算——
        结果就在管道里，必须原样转交，不能报成「F&G 子进程跑了 Xs 仍未结束，已终止」并归因到行情源。
        变异「删掉 `poll() is not None` 那段」⇒ 红（CPython communicate(timeout=0) 先判超时就抛）。"""
        _fake_child(monkeypatch, _print_good())
        with rr._FgChild("2026-10-01", time.monotonic() + 0.3) as h:
            h.proc.wait(timeout=10)          # 子进程在预算内跑完
            time.sleep(0.5)                  # 父进程自己的活拖过了预算
            assert time.monotonic() > h.deadline
            res = h.result()
        assert res == GOOD, res

    def test_budget_exceeded_terminates_child(self, monkeypatch):
        t = time.monotonic()
        res, h = self._run(monkeypatch, "import time\ntime.sleep(60)\n", deadline_in=0.8)
        took = time.monotonic() - t
        assert res[rr._DETAIL_KEY]["runner"] == "budget_exceeded"
        assert res["line"].startswith("⚠️ F&G 敞口门前瞻检验无法判定：超出时间预算")
        assert h.proc.poll() is not None, "超时后子进程必须已结束"
        assert took < 0.8 + rr._FG_KILL_GRACE_SECONDS + 2.0, took

    def test_term_ignoring_child_is_killed_after_grace(self, monkeypatch, capsys):
        """子进程不理 TERM ⇒ 宽限后 KILL，且 stderr 记一行（生产子进程保持 TERM 默认动作，走到 KILL 说明它的信号处置被改了）。
        没有 KILL 这一步，父进程会在 `wait()` 上陪它一起挂到 60s。"""
        monkeypatch.setattr(rr, "_FG_KILL_GRACE_SECONDS", 0.3)
        code = ("import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "sys.stdout.write('armed'); sys.stdout.flush()\ntime.sleep(60)\n")
        t = time.monotonic()
        res, h = self._run(monkeypatch, code, deadline_in=0.8)
        assert res[rr._DETAIL_KEY]["runner"] == "budget_exceeded"
        assert h.proc.returncode == -signal.SIGKILL, h.proc.returncode
        assert time.monotonic() - t < 0.8 + 0.3 + 2.0
        assert "TERM 后 0.3s 仍未退出，已 KILL" in capsys.readouterr().err

    def test_private_tmpdir_is_removed_after_kill(self, monkeypatch, tmp_path):
        """子进程在私有 TMPDIR 里建了目录、写了文件后被终止（没机会跑自己的 finally）⇒ 父进程收尸后整个删掉。
        这是 v0.45.392 真实数据实测的漏法：子进程的 `TemporaryDirectory` 清理靠不住（见 `_FgChild` docstring）。"""
        mark = tmp_path / "child_tmp.txt"
        code = textwrap.dedent(f"""
            import os, tempfile, time
            d = tempfile.mkdtemp(prefix="fg_gate_fwd_")
            open(os.path.join(d, "A_baseline"), "w").write("x")
            open({str(mark)!r}, "w").write(d + "\\n" + tempfile.gettempdir())
            time.sleep(60)
        """)
        _fake_child(monkeypatch, code)
        with rr._FgChild(None, time.monotonic() + 10) as h:
            root = h.tmp_root
            assert root and os.path.isdir(root)
            assert _wait_for(mark, 10.0)
            h.deadline = time.monotonic()            # 现在就到点
            res = h.result()
        made, child_tmp = mark.read_text().splitlines()
        assert os.path.realpath(child_tmp) == os.path.realpath(root), "子进程没拿到父进程给的私有 TMPDIR"
        assert res[rr._DETAIL_KEY]["runner"] == "budget_exceeded"
        assert h.proc.returncode == -signal.SIGTERM, "fake 子进程保持 TERM 默认动作，应被 TERM 直接终止"
        assert not os.path.exists(made) and not os.path.exists(root) and h.tmp_root is None

    def test_unbounded_waits_as_long_as_it_takes(self, monkeypatch):
        res, _ = self._run(monkeypatch, "import time\ntime.sleep(1.2)\n" + _print_good(), deadline_in=None)
        assert res == GOOD

    def test_with_exit_on_exception_kills_child(self, monkeypatch):
        _fake_child(monkeypatch, "import time\ntime.sleep(60)\n")
        with pytest.raises(RuntimeError):
            with rr._FgChild(None, None) as h:
                assert h.proc.poll() is None
                raise RuntimeError("主流程在等 F&G 之前崩了")
        assert h.proc.poll() is not None, "主流程异常退出时子进程必须一起终止（不留孤儿）"

    def test_child_argv_carries_parent_pid_today_and_budget(self):
        argv = rr._fg_child_argv("2026-10-01", 12.5)
        assert argv[:3] == [sys.executable, str((REPO_ROOT / "ic_rerun_readiness.py").resolve()), rr._FG_CHILD_FLAG]
        assert argv[argv.index("--parent-pid") + 1] == str(os.getpid())
        assert argv[argv.index("--today") + 1] == "2026-10-01"
        assert float(argv[argv.index("--max-seconds") + 1]) == 12.5
        bare = rr._fg_child_argv(None, None)
        assert "--today" not in bare and "--max-seconds" not in bare


class TestRunnerAttention:
    """执行层判的三种无法判定：段首 ⚠️、各有专属 warn id，与检验自己的 `.cannot_judge` 分开。"""

    @pytest.mark.parametrize("kind", sorted(rr._FG_RUNNER_TEXT))
    def test_own_id_warn_and_icon(self, kind):
        st = rr._fg_runner_result(kind, "原因X")
        line = st["line"]
        items = rr._forward_test_attention("fg_exposure_gate_forward", rr._take_detail(st))
        assert set(st) == {"status", "line"}, "取走 _detail 后 F&G 段必须只剩 {status, line}（payload 形状）"
        assert [a["id"] for a in items] == [f"{FG}.{kind}"]
        assert items[0]["level"] == "warn"
        assert "原因X" in items[0]["message"] and rr._FG_RUNNER_TEXT[kind] in items[0]["message"]
        _assert_icon_consistent(line, items)
        # 周度任务按段首名字 / 图标认段：仍是「⚠️ F&G 敞口门前瞻检验…」，占 F&G 那一段的位
        segs = ["⏳ IC 重跑未就绪：…", "⏳ 共振加成前瞻检验：…", line, "⏳ 维度 IC 协议（v0.45.320）：…"]
        assert _check_quiet_segments(segs) == [], _check_quiet_segments(segs)

    def test_ids_are_distinct_from_executor_cannot_judge(self):
        executor = rr._forward_test_attention("fg_exposure_gate_forward",
                                              {"status": "cannot_judge", "reason": "自证 8/10"})
        runner_ids = {f"{FG}.{k}" for k in rr._FG_RUNNER_TEXT}
        assert [a["id"] for a in executor] == [f"{FG}.cannot_judge"]
        assert f"{FG}.cannot_judge" not in runner_ids and len(runner_ids) == 3

    def test_placeholder_is_interrupted(self):
        st = rr._fg_checkpoint_placeholder()
        assert st["status"] == "cannot_judge" and "进程在 F&G 重放结束前终止" in st["line"]
        assert st[rr._DETAIL_KEY]["runner"] == "interrupted"


class TestMainFlow:
    """进程内 `main()`，`_FG_IN_PROCESS` 置回 False（conftest 默认置 True，见那里的 docstring）。"""

    @pytest.fixture(autouse=True)
    def _subprocess_mode(self, monkeypatch, scoped_sys_tmp):
        monkeypatch.setattr(rr, "_FG_IN_PROCESS", False)

    def _argv(self, monkeypatch, tmp_path, *extra, out=True):
        db = _make_db(tmp_path / "home", [])
        argv = ["ic_rerun_readiness.py", "--db", str(db), "--today", TODAY, *extra]
        o = tmp_path / "out.json"
        if out:
            argv += ["--out", str(o)]
        monkeypatch.setattr(sys, "argv", argv)
        return db, o

    def test_checkpoint_is_written_before_waiting_and_final_overwrites_it(self, monkeypatch, tmp_path):
        """假子进程等到 `--out` 出现（= 检查点）就把它拷走，再交结果——能拷到，就证明检查点写在等 F&G 之前。"""
        ckpt = tmp_path / "ckpt_seen_by_child.json"
        db, out = self._argv(monkeypatch, tmp_path, "--quiet")
        _fake_child(monkeypatch, textwrap.dedent(f"""
            import shutil, time
            from pathlib import Path
            out, end = Path({str(out)!r}), time.monotonic() + 20
            while not out.exists() and time.monotonic() < end:
                time.sleep(0.02)
            shutil.copyfile(out, {str(ckpt)!r})
        """) + _print_good())
        assert rr.main() == 1
        assert ckpt.exists(), "子进程在 20s 内没等到检查点：--out 只在最后写了一次？"
        c, f = json.loads(ckpt.read_text("utf-8")), json.loads(out.read_text("utf-8"))
        for env in (c, f):
            assert sc.validate(env) == [], sc.validate(env)
            assert env["date"] == TODAY
        # 检查点：F&G 是显式占位，其余各项是真结果
        assert c["fg_exposure_gate_forward_test"] == {
            "status": "cannot_judge", "line": "⚠️ F&G 敞口门前瞻检验无法判定：进程在 F&G 重放结束前终止"}
        assert f"{FG}.interrupted" in {a["id"] for a in c["attention"]}
        assert c["status"] == "attention"
        for k in ("cohort", "weeks_accrued", "resonance_forward_test", "dim_ic_forward_test", "cohort_boundary_evidence"):
            assert c[k] == f[k], f"检查点里的 {k} 与最终结果不同——它应是本轮真实结果，不是占位"
        assert "verdict" in c["cohort_boundary_evidence"]
        # 最终：F&G 换成子进程的结果，占位条目消失
        assert f["fg_exposure_gate_forward_test"] == {"status": GOOD["status"], "line": GOOD["line"]}
        assert not any(a["id"].startswith(FG + ".") for a in f["attention"]), f["attention"]
        # 键序：检查点 = 最终 = v0.45.391 的顺序（assess 的键，然后 共振 → F&G → 维度 IC → 世代边界，然后外壳）
        expect = (list(rr.assess(db_path=db, today=TODAY))
                  + ["resonance_forward_test", "fg_exposure_gate_forward_test", "dim_ic_forward_test",
                     "cohort_boundary_evidence"] + list(sc.ENVELOPE_KEYS))
        assert list(c) == expect and list(f) == expect, (list(c), list(f))

    def test_child_starts_before_the_cheap_parts(self, monkeypatch, tmp_path):
        """并行：`assess` 被调用时 F&G 子进程已经在跑（否则 F&G 只剩「预算 − 其余各项耗时」）。"""
        mark = tmp_path / "child_started"
        self._argv(monkeypatch, tmp_path, "--quiet")
        _fake_child(monkeypatch, f"open({str(mark)!r}, 'w').write('1')\n" + _print_good())
        real_assess = rr.assess

        def _assess(*a, **k):
            assert _wait_for(mark, 10.0), "assess 开始时 F&G 子进程还没起：子进程不是开跑就起的"
            return real_assess(*a, **k)

        monkeypatch.setattr(rr, "assess", _assess)
        assert rr.main() == 1

    def test_budget_overrun_is_visible_everywhere_and_verdict_unchanged(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(rr, "_FG_KILL_GRACE_SECONDS", 0.3)
        db, out = self._argv(monkeypatch, tmp_path, "--quiet", "--budget-seconds", "1.5")
        seen = _fake_child(monkeypatch, "import time\ntime.sleep(60)\n")
        t = time.monotonic()
        rc = rr.main()
        assert time.monotonic() - t < 1.5 + 0.3 + 3.0
        assert rc == 1, "超预算不改变就绪度判定与退出码"
        assert 0 < seen["max_seconds"] <= 1.5
        env = json.loads(out.read_text("utf-8"))
        assert sc.validate(env) == []
        fg = env["fg_exposure_gate_forward_test"]
        assert set(fg) == {"status", "line"} and fg["status"] == "cannot_judge"
        assert fg["line"].startswith("⚠️ F&G 敞口门前瞻检验无法判定：超出时间预算")
        ids = {a["id"] for a in env["attention"]}
        assert f"{FG}.budget_exceeded" in ids
        assert not ({f"{FG}.interrupted", f"{FG}.cannot_judge"} & ids), ids
        assert env["status"] == "attention"
        segs = capsys.readouterr().out.strip().split("｜")
        assert _check_quiet_segments(segs) == [] and segs[2] == fg["line"]

    def test_budget_zero_is_unbounded(self, monkeypatch, tmp_path):
        """0 = 不限时：子进程跑 1.2s 照样等它（同样的子进程在 1s 预算下会被终止，见上一条）。"""
        _db, out = self._argv(monkeypatch, tmp_path, "--quiet", "--budget-seconds", "0")
        seen = _fake_child(monkeypatch, "import time\ntime.sleep(1.2)\n" + _print_good())
        assert rr.main() == 1
        assert seen["max_seconds"] is None
        assert json.loads(out.read_text("utf-8"))["fg_exposure_gate_forward_test"]["line"] == GOOD["line"]

    def test_default_budget_is_counted_from_main_start(self, monkeypatch, tmp_path):
        self._argv(monkeypatch, tmp_path, "--quiet")
        seen = _fake_child(monkeypatch, _print_good())
        rr.main()
        assert rr.FG_BUDGET_SECONDS_DEFAULT - 5 < seen["max_seconds"] <= rr.FG_BUDGET_SECONDS_DEFAULT

    @pytest.mark.parametrize("bad", ["-1", "nan", "inf", "abc"])
    def test_bad_budget_is_a_usage_error(self, monkeypatch, tmp_path, bad):
        self._argv(monkeypatch, tmp_path, "--budget-seconds", bad)
        with pytest.raises(SystemExit) as e:
            rr.main()
        assert e.value.code == 2

    def test_crash_in_cheap_parts_kills_the_child(self, monkeypatch, tmp_path):
        pidf = tmp_path / "child.pid"
        self._argv(monkeypatch, tmp_path, "--quiet")
        _fake_child(monkeypatch, f"import os, time\nopen({str(pidf)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")

        def _boom(*a, **k):
            assert _wait_for(pidf, 10.0)
            raise RuntimeError("assess 崩了")

        monkeypatch.setattr(rr, "assess", _boom)
        with pytest.raises(RuntimeError):
            rr.main()
        assert _gone(int(pidf.read_text())), "主流程崩溃后 F&G 子进程还活着（孤儿）"

    def test_json_stdout_is_one_document_even_with_checkpoint(self, monkeypatch, tmp_path, capsys):
        """检查点只写 `--out`：`--json` 的 stdout 仍是**一个** JSON 文档，且与 `--out` 最终内容相同。"""
        _db, out = self._argv(monkeypatch, tmp_path, "--json")
        _fake_child(monkeypatch, _print_good())
        assert rr.main() == 1
        doc = json.loads(capsys.readouterr().out)
        assert doc == json.loads(out.read_text("utf-8"))
        assert doc["fg_exposure_gate_forward_test"]["line"] == GOOD["line"]


def test_conftest_keeps_fg_in_process_for_in_process_main(monkeypatch, tmp_path, capsys):
    """conftest 的 `_fg_sub_status_in_process` 真的生效：进程内调 `main()`（**不**手动置钩子）时，打在
    `fg_exposure_gate_forward_status` 上的桩被用到。删掉那个夹具 ⇒ 这里起真子进程、桩被无声绕过 ⇒ 红。
    没有这一条，删夹具不会让任何测试变红（沙箱里真子进程恰好也很快返回一个 ⚠️ 段），而那些进程内测试
    从此在子进程里绕开出网闸与 monkeypatch。"""
    assert rr._FG_IN_PROCESS is True, "conftest 没把 _FG_IN_PROCESS 置 True"
    db = _make_db(tmp_path / "home", [])
    monkeypatch.setattr(rr, "fg_exposure_gate_forward_status",
                        lambda today=None: {"status": "not_ready", "line": "⏳ F&G 敞口门前瞻检验：进程内桩"})
    monkeypatch.setattr(sys, "argv", ["ic_rerun_readiness.py", "--db", str(db), "--today", TODAY, "--quiet"])
    rr.main()
    assert capsys.readouterr().out.split("｜")[2] == "⏳ F&G 敞口门前瞻检验：进程内桩"


def test_sigterm_handler_raises_143_and_ignores_repeats():
    old = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as e:
            rr._sigterm_to_exit(signal.SIGTERM, None)
        assert e.value.code == 143, "编排器把 143 记成超时（124）；换成别的码会被记成「异常」"
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN, "清理期间不该被第二次 TERM 打断"
    finally:
        signal.signal(signal.SIGTERM, old)


# ═══════════════════════════════ 真实进程树（子进程） ═══════════════════════════════

_SITECUSTOMIZE = '''\
"""测试用套接字闸（tests/test_ic_rerun_fg_budget.py 注入）：非 AF_UNIX 的 connect 一律记账并拒绝。"""
import os, socket
_LOG = os.environ["FG_BUDGET_SOCKET_LOG"]
_real = socket.socket.connect
def _deny(self, addr, *a, **k):
    if self.family == getattr(socket, "AF_UNIX", None):
        return _real(self, addr, *a, **k)
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(f"{os.getpid()} {addr!r}\\n")
    raise OSError("测试离线：已挡下 socket.connect")
socket.socket.connect = _deny
socket.socket.connect_ex = lambda self, addr: (_deny(self, addr), 0)[1]
try:                                    # libcurl 在 C 层开 socket：上面那层看不见，钩库级入口
    import curl_cffi.curl as _cc
except Exception:
    _cc = None
if _cc is not None:
    def _deny_curl(self, *a, **k):
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()} curl_cffi.Curl.perform\\n")
        raise OSError("测试离线：已挡下 curl_cffi.Curl.perform")
    _cc.Curl.perform = _deny_curl
'''

#: 代理指向不监听的端口：钩子万一漏了，libcurl / requests / urllib 也只会连到一个拒绝连接的本地端口（失败即关）
_DEAD_PROXY = {k: "http://127.0.0.1:9" for k in ("http_proxy", "https_proxy", "all_proxy",
                                                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
_DEAD_PROXY.update(no_proxy="", NO_PROXY="")

_FAKE_EXECUTOR = '''\
"""假的 F&G 执行器（只给 tests/test_ic_rerun_fg_budget.py 的拷贝目录用）。行为由 FG_FAKE_MODE 决定。"""
import os, sys, tempfile, time
def _c_callback_forever():
    """主线程绝大部分时间待在一个由 C（libc qsort）回调的 Python 函数里——与真实事故里 curl_cffi 的
    `buffer_callback` 同形：信号处理器若在这里抛异常，会被 ctypes 吞掉。"""
    import ctypes, ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    cmp_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
    def _cmp(a, b):
        time.sleep(0.01)
        return a[0] - b[0]
    cb = cmp_t(_cmp)
    end = time.monotonic() + 120             # 有上限：被测代码坏了（变异）也不留不死的进程
    while time.monotonic() < end:
        arr = (ctypes.c_int * 32)(*range(32, 0, -1))
        libc.qsort(arr, 32, ctypes.sizeof(ctypes.c_int), cb)
def run(today=None):
    mode = os.environ["FG_FAKE_MODE"]
    if mode in ("hang", "c_callback"):
        with tempfile.TemporaryDirectory(prefix="fg_gate_fwd_") as td:
            with open(os.environ["FG_FAKE_TDFILE"], "w") as f:
                f.write(td)
            with open(os.environ["FG_FAKE_PIDFILE"], "w") as f:
                f.write(str(os.getpid()))
            if mode == "hang":
                time.sleep(120)
            else:
                _c_callback_forever()
    with open(os.environ["FG_FAKE_PIDFILE"], "w") as f:
        f.write(str(os.getpid()))
    if mode == "noisy":
        print("NOISE-PRINT")
        sys.stdout.flush()
        os.write(1, b"NOISE-FD1\\n")
    if mode == "degraded":   # v0.45.391 复审 S1 的回放窗口计数（形状同 paper_portfolio 窗口对象的 stats()）
        return {"status": "not_ready", "weeks": 3, "next_look_at": 15,
                "ohlc_window": {"wide_fetches": 20, "served": 300, "out_of_window": 0, "fallback": 2,
                                "fallback_tickers": {"BRK-B": "TypeError: x", "TMO": "空结果"},
                                "direct_requests": 41, "direct_empty": 3, "degraded": True}}
    return {"status": "not_ready", "weeks": 3, "next_look_at": 15}
def status_line(res):
    return f"⏳ F&G 敞口门前瞻检验：{res['weeks']}/{res['next_look_at']} 个合格周"
'''


@pytest.fixture
def tree(tmp_path):
    """tmp 里的「仓库」：生产的两个文件原样拷入 + 假 F&G 执行器 + 套接字闸。返回启动 CLI 的工具对象。"""
    code = tmp_path / "code"
    (code / "experiments").mkdir(parents=True)
    for rel in ("ic_rerun_readiness.py", "step_contract.py", "experiments/hv_gap_equivalence_20260928.json"):
        shutil.copyfile(REPO_ROOT / rel, code / rel)
    (code / "experiments" / "fg_exposure_gate_forward_test.py").write_text(_FAKE_EXECUTOR, encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    home = tmp_path / "home"
    db = _make_db(home, [])

    class T:
        out = tmp_path / "out.json"
        pidfile = tmp_path / "fg_child.pid"
        tdfile = tmp_path / "fg_child_tempdir.txt"
        sock_log = tmp_path / "socket_attempts.log"
        stderr = tmp_path / "stderr.txt"
        sys_tmp = tmp_path / "sysT"           # 父进程与子进程的「系统临时目录」都圈在这里
        expect_tmp_leftover = False

        def env(self, mode):
            e = dict(os.environ)
            e.update({"ALPHA_HIVE_HOME": str(home), "ALPHA_HIVE_DB_PATH": str(db),
                      "ALPHA_HIVE_LOGS_DIR": str(home / "logs"), "ALPHA_HIVE_CACHE_DIR": str(home / "cache"),
                      "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(site),
                      "FG_BUDGET_SOCKET_LOG": str(self.sock_log), "TMPDIR": str(self.sys_tmp), **_DEAD_PROXY,
                      "FG_FAKE_MODE": mode, "FG_FAKE_PIDFILE": str(self.pidfile), "FG_FAKE_TDFILE": str(self.tdfile)})
            return e

        def child_tempdir(self):
            assert _wait_for(self.tdfile), "假执行器没记下它的临时目录"
            return Path(self.tdfile.read_text())

        procs: list = []

        def start(self, mode, *args):
            argv = [sys.executable, str(code / "ic_rerun_readiness.py"), "--db", str(db), "--today", TODAY,
                    "--quiet", "--out", str(self.out), *map(str, args)]
            self._err = open(self.stderr, "w", encoding="utf-8")
            p = subprocess.Popen(argv, cwd=str(tmp_path), env=self.env(mode),
                                 stdout=subprocess.PIPE, stderr=self._err, text=True)
            self.procs.append(p)
            return p

        def child_pid(self):
            assert _wait_for(self.pidfile), f"F&G 子进程没起来：\n{self.stderr.read_text('utf-8')[-2000:]}"
            return int(self.pidfile.read_text())

        def env_json(self):
            return json.loads(self.out.read_text("utf-8"))

    t = T()
    t.procs = []
    t.sys_tmp.mkdir()
    yield t
    # 收尸：测试失败（或被测代码坏了）时 CLI 父进程与 F&G 子进程不许活过本条测试（变异实测：不收会留下不死的进程）
    for p in t.procs:
        if p.poll() is None:
            p.kill()
        p.communicate()
    if t.pidfile.exists():
        _kill_if_ours(int(t.pidfile.read_text() or 0), str(code))
    if getattr(t, "_err", None):
        t._err.close()
    assert not t.sock_log.exists(), f"子进程伸手连外网了：{t.sock_log.read_text('utf-8')}"
    if not t.expect_tmp_leftover:
        left = sorted(x.name for x in t.sys_tmp.iterdir())
        assert not left, f"临时目录没删干净：{left}"


def test_socket_gate_canary_records_child_and_grandchild(tmp_path):
    """闸本身先自证：同样的注入方式下，真去连外网（子进程 + 孙进程，socket 与 curl_cffi 两条路）**会**被记账、被拒。
    没有这一条，`tree` 夹具 teardown 的「账本为空」可能只是闸没装上——或只装上了 yfinance 根本不走的那一层。"""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    log = tmp_path / "log"
    env = {**os.environ, "PYTHONPATH": str(site), "FG_BUDGET_SOCKET_LOG": str(log), **_DEAD_PROXY}
    def _connect(host):
        return f"import socket\ntry:\n    socket.create_connection(({host!r}, 443), timeout=2)\nexcept OSError:\n    pass\n"

    curl = ("import curl_cffi.requests as r\ntry:\n    r.get('https://203.0.113.9/', timeout=2)\n"
            "except Exception:\n    pass\n")
    probe = (_connect("203.0.113.7") + curl                # 子进程自己连（socket + curl_cffi）
             + f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {_connect('203.0.113.8')!r}], check=True)\n")
    subprocess.run([sys.executable, "-c", probe], env=env, timeout=30, check=True)
    text = log.read_text("utf-8") if log.exists() else ""
    assert "203.0.113.7" in text and "203.0.113.8" in text, f"闸没罩住子进程 / 孙进程：{text!r}"
    assert "curl_cffi.Curl.perform" in text, f"闸看不见 curl_cffi（yfinance 走的就是它）：{text!r}"


class TestRealProcessTree:
    """父子进程都是生产代码（原样拷贝），只有 F&G 执行器是假的。"""

    def test_sigterm_after_checkpoint_leaves_checkpoint_and_kills_child(self, tree):
        """编排器超时只 TERM 父进程：退出码 143（编排器记 124）、`--out` 是检查点、F&G 子进程跟着死。"""
        p = tree.start("hang")
        child = tree.child_pid()
        assert _wait_for(tree.out), "检查点没写出来"
        p.send_signal(signal.SIGTERM)
        rc = p.wait(timeout=15)
        assert rc == 143, f"rc={rc}\n{tree.stderr.read_text('utf-8')[-2000:]}"
        assert _gone(child), "父进程被 TERM 后 F&G 子进程还活着"
        assert not tree.child_tempdir().exists(), "子进程的重放临时目录残留"
        env = tree.env_json()
        assert sc.validate(env) == [] and env["date"] == TODAY
        assert env["fg_exposure_gate_forward_test"]["line"] == "⚠️ F&G 敞口门前瞻检验无法判定：进程在 F&G 重放结束前终止"
        assert f"{FG}.interrupted" in {a["id"] for a in env["attention"]}
        assert "verdict" in env["cohort_boundary_evidence"], "检查点必须带世代边界核对（这次修复的初衷）"

    def test_budget_overrun_real_tree(self, tree):
        t = time.monotonic()
        p = tree.start("hang", "--budget-seconds", "2")
        child = tree.child_pid()
        stdout, _ = p.communicate(timeout=30)
        took = time.monotonic() - t
        assert p.returncode == 1, tree.stderr.read_text("utf-8")[-2000:]
        assert took < 2 + rr._FG_KILL_GRACE_SECONDS + 5, took
        assert _gone(child)
        env = tree.env_json()
        assert f"{FG}.budget_exceeded" in {a["id"] for a in env["attention"]}
        segs = stdout.strip().split("｜")
        assert _check_quiet_segments(segs) == [] and "超出时间预算" in segs[2]

    def test_parent_sigkill_orphan_child_exits_by_itself(self, tree):
        """编排器 TERM 后 10s 还会 KILL：父进程没机会收尾 ⇒ 子进程靠看门狗（`getppid()` 变了）自己退。
        这条路上没人删临时目录（父进程已死）——那一行要把目录名点出来，不能无声残留。"""
        p = tree.start("hang")
        child = tree.child_pid()
        td = tree.child_tempdir()
        p.kill()
        p.wait(timeout=10)
        assert _gone(child, within=rr._FG_CHILD_POLL_SECONDS + 4), "孤儿 F&G 子进程没退出"
        err = tree.stderr.read_text("utf-8")
        assert "父进程已不在" in err and "可能残留" in err
        root = td.parent
        assert root.name.startswith("ic_rerun_fg_child_") and str(root) in err, (str(root), err[-800:])
        tree.expect_tmp_leftover = True           # 预期残留：本条自己收拾，别漏进别处
        shutil.rmtree(root)

    def test_term_landing_in_c_callback_still_kills_child_without_escalation(self, tree):
        """真实事故的形状：TERM 到达时子进程主线程在 C 回调里。子进程保持 TERM 默认动作 ⇒ 立即死，用不着 KILL；
        临时目录由父进程删。若子进程改成「TERM → 抛 SystemExit」，异常被回调吞掉、要等宽限后 KILL ⇒ stderr 出现「已 KILL」。"""
        t = time.monotonic()
        p = tree.start("c_callback", "--budget-seconds", "2")
        child = tree.child_pid()
        td = tree.child_tempdir()
        p.communicate(timeout=30)
        took = time.monotonic() - t
        err = tree.stderr.read_text("utf-8")
        assert p.returncode == 1, err[-2000:]
        assert "已 KILL" not in err, f"子进程没被 TERM 直接终止（落到了宽限后 KILL）：\n{err[-1500:]}"
        assert _gone(child) and not td.exists()
        assert took < 2 + rr._FG_KILL_GRACE_SECONDS + 1.5, took
        assert f"{FG}.budget_exceeded" in {a["id"] for a in tree.env_json()["attention"]}

    def test_executor_stdout_noise_does_not_corrupt_result(self, tree):
        """执行器（及其依赖库）往 stdout 打字——Python 层与 fd 层都有——只进 stderr，结果照常解析。"""
        p = tree.start("noisy")
        stdout, _ = p.communicate(timeout=60)
        assert p.returncode == 1, tree.stderr.read_text("utf-8")[-2000:]
        assert tree.env_json()["fg_exposure_gate_forward_test"]["line"] == "⏳ F&G 敞口门前瞻检验：3/15 个合格周"
        err = tree.stderr.read_text("utf-8")
        assert "NOISE-PRINT" in err and "NOISE-FD1" in err
        assert "NOISE" not in stdout

    def test_ohlc_window_degraded_survives_the_process_hop(self, tree):
        """v0.45.391 复审 S1 的降级条目，经 `_detail` 白名单 → 子进程 JSON → 父进程解析后照样列出。
        它在 391 自己的测试里只走进程内；生产里这份细节现在要过一道子进程边界。"""
        p = tree.start("degraded")
        p.communicate(timeout=60)
        assert p.returncode == 1, tree.stderr.read_text("utf-8")[-2000:]
        items = {a["id"]: a for a in tree.env_json()["attention"]}
        it = items.get(f"{FG}.ohlc_window_degraded")
        assert it is not None, sorted(items)
        assert "2/20" in it["message"] and "BRK-B" in it["message"] and "TMO" in it["message"], it["message"]

    def test_child_self_deadline_without_parent_action(self, tree):
        """父进程在场却没动手（不该发生，但看门狗是最后一道）：子进程超过自身时限（剩余时间 + 余量）自己退。"""
        env = tree.env(mode="hang")
        argv = [sys.executable, str(tree.out.parent / "code" / "ic_rerun_readiness.py"), rr._FG_CHILD_FLAG,
                "--parent-pid", str(os.getpid()), "--max-seconds", "0"]
        t = time.monotonic()
        with open(tree.stderr, "w", encoding="utf-8") as err:
            c = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=err)
            tree.procs.append(c)                  # 失败时由夹具收尸
            c.communicate(timeout=rr._FG_CHILD_SELF_DEADLINE_SLACK + 10)
        assert c.returncode == rr._FG_CHILD_WATCHDOG_RC, c.returncode
        assert time.monotonic() - t >= rr._FG_CHILD_SELF_DEADLINE_SLACK
        assert "超过自身时限" in tree.stderr.read_text("utf-8")
        tree.expect_tmp_leftover = True           # 直接起的子进程没有父进程替它收拾；目录在 tmp 里，随 tmp_path 清走


# ═══════════════════════════════ 消费方 ═══════════════════════════════

class TestOrchestratorReadsIt:
    """编排器经 `orchestrator_steps.render` 读 Step 11 的 JSON：检查点 / 超预算两种文件都要读得出、看得见。"""

    def _render(self, path, rc, run_start):
        return osteps.render(["--step", "11", "--rc", str(rc), "--json", str(path), "--date", TODAY,
                              "--run-start", str(run_start)])

    def test_killed_run_reads_checkpoint_not_missing(self, tree):
        run_start = time.time() - 1
        p = tree.start("hang")
        tree.child_pid()
        assert _wait_for(tree.out)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=15) == 143
        out = self._render(tree.out, 124, run_start)            # bash 把 143 记成 124
        frag = out["steps_fragment"]["step11_ic_rerun_readiness"]
        assert out["level"] in ("warn", "error")
        assert frag.get("cohort_boundary") is not None, (frag, out["message"])
        assert f"{FG}.interrupted" in {a["id"] for a in frag["attention"]}, frag
        assert "需人看" in out["message"], out["message"]
        assert "代码早于 v0.45.334" not in out["message"], "检查点在场时不该再落进「JSON 不可读」的文案"
        assert "⏰ Step 11 超时" in out["message"] and "本轮未核对" not in out["message"], out["message"]
        assert "json_problem" not in frag and frag["status"] == "error" and frag["rc"] == 124, frag

    def test_budget_overrun_is_warn_with_reason(self, tree):
        run_start = time.time() - 1
        p = tree.start("hang", "--budget-seconds", "2")
        tree.child_pid()
        p.communicate(timeout=30)
        out = self._render(tree.out, p.returncode, run_start)
        frag = out["steps_fragment"]["step11_ic_rerun_readiness"]
        assert frag["status"] == "accruing" and out["level"] == "warn", (frag, out["level"])
        assert f"{FG}.budget_exceeded" in {a["id"] for a in frag["attention"]}, frag


class TestBudgetFitsOrchestratorTimeout:
    """缺省预算必须放得进编排器 Step 11 的超时：编排器不传 `--budget-seconds`（改它要隔一个扫描日才部署），
    缺省值就是生产值。到点后还有 TERM 宽限 + 写最终 JSON，再留 ≥10s 余量（解释器启动、慢盘、负载）。"""

    def test_default_budget_plus_grace_fits(self):
        """超时取 `orchestrator_steps.STEP11_TIMEOUT_DEFAULT`（v0.45.391 起由 `TestStep11Timeout` 钉在仓库编排器那一行上，
        一份真相）；编排器若显式传了 `--budget-seconds`，以它为准。"""
        import re
        text = repo_orchestrator_text()
        m = re.search(r'run_step\s+--timeout\s+\d+\s+"\$\{PROJECT_DIR\}/ic_rerun_readiness\.py"([^\n]*\n[^\n]*)', text)
        assert m, "编排器里找不到 Step 11 的 run_step 行（改了写法？这条守卫要跟着改，不许删）"
        flag = re.search(r"--budget-seconds\s+(\S+)", m.group(1))
        budget = float(flag.group(1)) if flag else rr.FG_BUDGET_SECONDS_DEFAULT
        timeout = osteps.STEP11_TIMEOUT_DEFAULT
        assert budget > 0, "生产不许不限时：0 = 不限时，会重新把 Step 11 推回超时"
        assert budget + rr._FG_KILL_GRACE_SECONDS + 10 <= timeout, (budget, timeout)
