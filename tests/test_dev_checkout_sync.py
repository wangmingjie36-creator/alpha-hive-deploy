"""开发检出会话启动时自动快进 main（SessionStart 钩子 `scripts/dev_checkout_sync.py`，v0.45.467）。

全用真 git：本地裸仓库扮演 GitHub，`up` 扮演别的会话往 main 推，`dev` 扮演 `~/Desktop/Alpha Hive`。
钩子绝不能挡住会话启动（退出码恒 0），也绝不能动有人在用的工作区——每种「不碰」各一条。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "dev_checkout_sync.py"


def _git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class _World:
    def __init__(self, tmp: Path, dev_at: Path = None):
        self.tmp, self.home = tmp, tmp / "home"
        self.home.mkdir()
        self.bare, self.up = tmp / "origin.git", tmp / "up"
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(self.bare))
        _git(tmp, "clone", "-q", str(self.bare), str(self.up))
        (self.up / "CLAUDE.md").write_text("# 规则\n- 旧规则\n", encoding="utf-8")
        (self.up / "code.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.up, "add", "-A")
        _git(self.up, "commit", "-qm", "init")
        _git(self.up, "push", "-q", "origin", "main")
        self.dev = dev_at or tmp / "dev"
        self.dev.parent.mkdir(parents=True, exist_ok=True)
        _git(tmp, "clone", "-q", str(self.bare), str(self.dev))

    def push_upstream(self, name: str, text: str) -> None:
        (self.up / name).parent.mkdir(parents=True, exist_ok=True)
        (self.up / name).write_text(text, encoding="utf-8")
        _git(self.up, "add", "-A")
        _git(self.up, "commit", "-qm", f"改 {name}")
        _git(self.up, "push", "-q", "origin", "main")

    def run(self, start: Path = None):
        env = {**os.environ, "HOME": str(self.home)}
        env.pop("CLAUDE_PROJECT_DIR", None)
        r = subprocess.run([sys.executable, str(SCRIPT), str(start or self.dev)], env=env,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout

    def head(self, where: Path = None) -> str:
        return _git(where or self.dev, "rev-parse", "HEAD")

    def origin_main(self) -> str:
        return _git(self.up, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    return _World(tmp_path)


class TestFastForward:
    def test_behind_and_clean_fast_forwards_and_shows_the_claude_md_diff(self, world):
        world.push_upstream("CLAUDE.md", "# 规则\n- 旧规则\n- 新规则：先占号\n")
        rc, out = world.run()
        assert rc == 0 and world.head() == world.origin_main(), out
        assert "已快进" in out and "CLAUDE.md 有变化" in out and "+- 新规则：先占号" in out, out

    def test_no_claude_md_change_no_diff_section(self, world):
        world.push_upstream("code.py", "x = 2\n")
        rc, out = world.run()
        assert rc == 0 and world.head() == world.origin_main()
        assert "已快进" in out and "CLAUDE.md 有变化" not in out, out

    def test_up_to_date_is_silent(self, world):
        assert world.run() == (0, "")

    def test_long_diff_is_truncated_with_the_full_command(self, world):
        limit = _load_script().MAX_DIFF_LINES
        world.push_upstream("CLAUDE.md", "".join(f"- 第 {i} 条\n" for i in range(limit + 50)))
        rc, out = world.run()
        assert rc == 0 and "截断" in out and "git diff" in out and "-- CLAUDE.md" in out, out[-300:]


    def test_settings_and_nested_claude_md_are_reported_too(self, world):
        """会话启动时就载入的不止根 CLAUDE.md：`.claude/` 下的设置 / 钩子 / skills、子目录的 CLAUDE.md 变了也要说
        （v0.45.467 二次检查：初版只给根 CLAUDE.md 的 diff，会话不知道自己仍按旧设置跑）。"""
        world.push_upstream(".claude/settings.json", '{"hooks": {}}\n')
        world.push_upstream("docs/CLAUDE.md", "# 子目录规则\n- 新的子规则\n")
        rc, out = world.run()
        assert rc == 0 and world.head() == world.origin_main(), out
        assert "docs/CLAUDE.md" in out and "+- 新的子规则" in out, out
        assert ".claude/ 下有变化（.claude/settings.json）" in out and "下次会话才生效" in out, out


class TestLeavesItAlone:
    def test_uncommitted_changes(self, world):
        world.push_upstream("code.py", "x = 2\n")
        (world.dev / "CLAUDE.md").write_text("我正在改\n", encoding="utf-8")
        before = world.head()
        rc, out = world.run()
        assert rc == 0 and world.head() == before
        assert "未提交改动（CLAUDE.md）" in out, out          # 文件名完整（porcelain 首行前导空格不能被 strip 掉）
        assert (world.dev / "CLAUDE.md").read_text(encoding="utf-8") == "我正在改\n"

    def test_unpushed_commits(self, world):
        world.push_upstream("code.py", "x = 2\n")
        (world.dev / "local.txt").write_text("l\n", encoding="utf-8")
        _git(world.dev, "add", "-A")
        _git(world.dev, "commit", "-qm", "本地")
        before = world.head()
        rc, out = world.run()
        assert rc == 0 and world.head() == before and "1 个未推送提交" in out, out

    def test_other_branch(self, world):
        world.push_upstream("code.py", "x = 2\n")
        _git(world.dev, "checkout", "-q", "-b", "feat")
        before = world.head()
        rc, out = world.run()
        assert rc == 0 and world.head() == before and "不在 main（feat）" in out, out

    def test_linked_worktree_is_silent(self, world):
        world.push_upstream("code.py", "x = 2\n")
        wt = world.tmp / "wt"
        _git(world.dev, "worktree", "add", "-q", "-b", "w", str(wt))
        before = world.head()
        assert world.run(wt) == (0, "")
        assert world.head() == before, "在 worktree 里启动的会话动了开发检出的 main"

    def test_production_clone(self, tmp_path):
        """生产克隆也是独立仓库的主工作区——按 `production_clone.default_dest()` 认出来，不碰，提醒一句。"""
        w = _World(tmp_path, dev_at=tmp_path / "home" / "alpha-hive-prod")
        w.push_upstream("code.py", "x = 2\n")
        before = w.head()
        rc, out = w.run()
        assert rc == 0 and w.head() == before and "生产克隆" in out, out

    def test_not_a_git_repo_is_silent(self, world):
        plain = world.tmp / "plain"
        plain.mkdir()
        assert world.run(plain) == (0, "")


class TestNeverBlocksTheSession:
    def test_fetch_failure_is_reported_not_raised(self, world):
        _git(world.dev, "remote", "set-url", "origin", "/nonexistent/origin.git")
        rc, out = world.run()
        assert rc == 0 and "没能取 origin/main" in out and "CLAUDE.md 可能落后" in out, out

    def test_unexpected_error_still_exits_zero(self, monkeypatch, capsys):
        mod = _load_script()

        def boom(start):
            raise RuntimeError("意外")
        monkeypatch.setattr(mod, "run", boom)
        assert mod.main(["/nonexistent"]) == 0
        assert "dev_checkout_sync 出错" in capsys.readouterr().out


class TestTimeBudget:
    """一个时间预算管全部 git 调用；改工作区的 merge 从不被杀（v0.45.467 二次检查：初版 merge 单独给 60 秒，
    钩子却只有 30 秒——宿主杀在 merge 半路会留下 index.lock）。"""

    def test_merge_not_finished_in_budget_is_reported_and_completes_unkilled(self, world, monkeypatch):
        import time
        mod = _load_script()
        world.push_upstream("CLAUDE.md", "# 规则\n- 旧规则\n- 后台快进也给 diff\n")
        real = mod._merge_detached
        monkeypatch.setattr(mod, "_merge_detached", lambda repo, target, wait: real(repo, target, 0))
        out = "\n".join(mod.run(world.dev))
        assert "正在后台快进" in out and "+- 后台快进也给 diff" in out, out
        deadline = time.monotonic() + 15
        while world.head() != world.origin_main() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert world.head() == world.origin_main(), "不再等了的 merge 没有做完（被杀了？）"
        assert not (world.dev / ".git" / "index.lock").exists()

    def test_exhausted_budget_refuses_further_calls(self, world):
        mod = _load_script()
        assert mod._git(world.dev, "status", budget=mod._Budget(0)) == (1, "", "时间预算用完")

    def test_hung_read_is_terminated_within_its_slice(self, world, monkeypatch):
        """挂住的 fetch（这里用 sleep 冒充 ssh，不出网）按份额超时结束、如实报超时，不吃满整个预算。"""
        import time
        mod = _load_script()
        monkeypatch.setenv("GIT_SSH_COMMAND", "sh -c 'sleep 30' --")
        t0 = time.monotonic()
        rc, _, err = mod._git(world.dev, "ls-remote", "git@example.invalid:x/y.git",
                              budget=mod._Budget(10), cap=1)
        assert rc == 1 and err.startswith("超时"), err
        assert time.monotonic() - t0 < 6

    def test_reads_never_take_optional_locks(self, world, monkeypatch):
        mod = _load_script()
        seen, real_popen = [], subprocess.Popen

        class _Sub:
            PIPE, DEVNULL, TimeoutExpired = subprocess.PIPE, subprocess.DEVNULL, subprocess.TimeoutExpired

            @staticmethod
            def Popen(argv, **k):
                seen.append(argv)
                return real_popen(argv, **k)
        monkeypatch.setattr(mod, "subprocess", _Sub)
        mod._git(world.dev, "status", "--porcelain", budget=mod._Budget(5))
        assert seen and seen[0][:2] == ["git", "--no-optional-locks"], seen

    def test_merge_only_runs_detached_with_output_off_pipes(self):
        """写工作区的 merge 只在 `_merge_detached` 里：自己的进程组（宿主杀钩子波及不到）、输出不进管道（本脚本
        先走了也不会被 SIGPIPE 杀在半路）。"""
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        owners = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                  for n in ast.walk(fn) if isinstance(n, ast.Constant) and n.value == "merge"}
        assert owners == {"_merge_detached"}, owners
        fn = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_merge_detached")
        popen = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "Popen")
        kw = {k.arg: ast.unparse(k.value) for k in popen.keywords}
        assert kw.get("start_new_session") == "True", kw
        assert "PIPE" not in kw.get("stdout", "PIPE") and "PIPE" not in kw.get("stderr", "PIPE"), kw


def _load_script():
    spec = importlib.util.spec_from_file_location("dev_checkout_sync", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHookIsRegistered:
    """钩子配置在仓库里（`.claude/settings.json`，跟踪）：命令、匹配、超时都核对；再按配置原样真跑一次。"""

    @staticmethod
    def _entry():
        cfg = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
        entries = cfg["hooks"]["SessionStart"]
        hits = [(e, h) for e in entries for h in e["hooks"] if "dev_checkout_sync.py" in h.get("command", "")]
        assert len(hits) == 1, entries
        return hits[0]

    def test_registered_for_startup_with_a_timeout_above_the_fetch(self):
        entry, hook = self._entry()
        assert "startup" in entry["matcher"].split("|")
        assert hook["type"] == "command"
        mod = _load_script()
        assert mod.FETCH_TIMEOUT + mod.DIFF_RESERVE < mod.TOTAL_BUDGET, "预算里放不下 fetch + diff"
        assert mod.TOTAL_BUDGET + 5 <= hook["timeout"] <= 60, "钩子超时要比脚本预算大、留出解释器启动的余量"
        interp = hook["command"].split(" ", 1)[0]
        assert os.path.isabs(interp) and Path(interp).name.startswith("python3"), \
            f"CLAUDE.md：解释器写绝对路径、禁用裸 python3（收到 {interp!r}）"
        assert '"$CLAUDE_PROJECT_DIR/scripts/dev_checkout_sync.py"' in hook["command"], "路径带空格要加引号"

    def test_configured_command_really_runs(self, world):
        """把配置里的命令原样交给 bash（解释器换成本测试的，免得依赖本机路径）：引号、环境变量展开都对。"""
        _, hook = self._entry()
        (world.dev / "scripts").mkdir()
        shutil.copy2(SCRIPT, world.dev / "scripts" / SCRIPT.name)          # 未跟踪：不算工作区脏
        world.push_upstream("code.py", "x = 2\n")
        cmd = shlex.quote(sys.executable) + " " + hook["command"].split(" ", 1)[1]
        env = {**os.environ, "HOME": str(world.home), "CLAUDE_PROJECT_DIR": str(world.dev)}
        r = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "已快进" in r.stdout, (r.stdout, r.stderr)
        assert world.head() == world.origin_main()
