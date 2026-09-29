"""`deploy_orchestrator.py` 的行为守卫（v0.45.356，编排器纳入版本控制·阶段 2）

全部在临时 git 仓库 + 临时部署位置里跑，**不碰真实部署副本**：autouse 把 HOME 指向沙箱
（`default_dest()` 调用时读 HOME），且每条用例都显式传 `dest`。候选内容是合成的
「像编排器」脚本（`set -uo pipefail` + 500 多行），与真实编排器解耦、跑得快。
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import deploy_orchestrator as dep


def _script(tag: str, extra: str = "") -> str:
    body = "".join(f"# filler {i}\n" for i in range(520))
    return f"#!/bin/bash\nset -uo pipefail\n{body}{extra}echo start\nsleep 1\necho {tag}-END\n"


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert dep.default_dest().is_relative_to(home), "默认部署位置没跟着沙箱 HOME 走——会写到真实副本"


class Repo:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True)
        self._g("init", "-q", "-b", "main")

    def _g(self, *args):
        r = subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t",
                            "-c", "commit.gpgsign=false", *args], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def commit(self, text: str) -> str:
        p = self.root / dep.REL_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self._g("add", "-A")
        self._g("commit", "-q", "-m", "c")
        return self._g("rev-parse", "HEAD")

    def set_main(self, sha: str):
        self._g("update-ref", "refs/remotes/origin/main", sha)


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo")


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "deployed" / "alpha-hive-orchestrator.sh"


def _deploy(repo, dest, ref, **kw):
    return dep.deploy(ref, repo=repo.root, dest=dest, **kw)


def _leftovers(dest):
    return sorted(p.name for p in dest.parent.iterdir() if p.name != dest.name) if dest.parent.exists() else []


class TestHappyPath:
    def test_fresh_deploy_writes_exact_bytes_executable_no_backup(self, repo, dest):
        v1 = repo.commit(_script("V1"))
        repo.set_main(v1)
        r = _deploy(repo, dest, "origin/main")
        assert r["outcome"] == "deployed", r
        assert dest.read_text(encoding="utf-8") == _script("V1")
        assert os.stat(dest).st_mode & 0o777 == 0o755
        assert r["backup"] is None and _leftovers(dest) == []

    def test_second_run_is_already_current_and_does_not_rewrite(self, repo, dest):
        repo.set_main(repo.commit(_script("V1")))
        _deploy(repo, dest, "origin/main")
        ino = os.stat(dest).st_ino
        r = _deploy(repo, dest, "origin/main")
        assert r["outcome"] == "already_current" and os.stat(dest).st_ino == ino

    def test_upgrade_from_a_git_version_leaves_no_bak(self, repo, dest):
        """被覆盖的那版在 git 里 ⇒ git 就是备份，不再造 .bak（阶段 4 退役 .bak 惯例的依据）。"""
        repo.set_main(repo.commit(_script("V1")))
        _deploy(repo, dest, "origin/main")
        repo.set_main(repo.commit(_script("V2")))
        r = _deploy(repo, dest, "origin/main")
        assert r["outcome"] == "deployed" and r["drift"] is False
        assert "V2-END" in dest.read_text() and _leftovers(dest) == []

    def test_dry_run_changes_nothing(self, repo, dest):
        repo.set_main(repo.commit(_script("V1")))
        r = _deploy(repo, dest, "origin/main", dry_run=True)
        assert r["outcome"] == "would_deploy" and not dest.exists() and _leftovers(dest) == []

    def test_deploys_committed_blob_not_working_tree(self, repo, dest):
        """未提交的改动到不了 launchd。"""
        repo.set_main(repo.commit(_script("V1")))
        (repo.root / dep.REL_PATH).write_text(_script("DIRTY"), encoding="utf-8")
        assert _deploy(repo, dest, "HEAD")["outcome"] == "deployed"
        assert "V1-END" in dest.read_text() and "DIRTY" not in dest.read_text()

    def test_head_ahead_of_main_is_fine_when_orchestrator_blob_is_on_main(self, repo, dest):
        """生产 checkout 带着没推上去的日报提交（09-25 分叉形状）：按 blob 判，照常部署。"""
        repo.set_main(repo.commit(_script("V1")))
        (repo.root / "report.md").write_text("日报\n")
        repo._g("add", "-A")
        repo._g("commit", "-q", "-m", "report")
        assert _deploy(repo, dest, "HEAD")["outcome"] == "deployed"


class TestRunningProcessKeepsOldVersion:
    def test_replace_under_a_running_bash(self, repo, dest):
        """`os.replace` 给新 inode：正在跑的 bash 读完旧版，下一次执行才是新版。"""
        repo.set_main(repo.commit(_script("OLD")))
        _deploy(repo, dest, "origin/main")
        proc = subprocess.Popen(["/bin/bash", str(dest)], stdout=subprocess.PIPE, text=True)
        time.sleep(0.3)                                   # 已进入 sleep 1
        repo.set_main(repo.commit(_script("NEW")))
        assert _deploy(repo, dest, "origin/main")["outcome"] == "deployed"
        out, _ = proc.communicate(timeout=10)
        assert out.split() == ["start", "OLD-END"], out
        again = subprocess.run(["/bin/bash", str(dest)], capture_output=True, text=True, timeout=10)
        assert again.stdout.split() == ["start", "NEW-END"]


class TestGates:
    """每道关卡各喂一次坏版本：拒绝、现有副本原封不动、不留临时文件。"""

    @pytest.fixture
    def deployed_v1(self, repo, dest):
        repo.set_main(repo.commit(_script("V1")))
        _deploy(repo, dest, "origin/main")
        return dest.read_bytes()

    def _assert_refused(self, r, dest, before, needle):
        assert r["outcome"] == "refused_gate", r
        assert any(needle in f for f in r["gate_failures"]), r["gate_failures"]
        assert dest.read_bytes() == before and _leftovers(dest) == []

    def test_ref_not_merged_to_main(self, repo, dest, deployed_v1):
        repo.commit(_script("BRANCH"))                     # 在 HEAD 上，没进 origin/main
        self._assert_refused(_deploy(repo, dest, "HEAD"), dest, deployed_v1, "不在 origin/main 的历史里")

    def test_syntax_error(self, repo, dest, deployed_v1):
        repo.set_main(repo.commit(_script("BAD", extra="if true; then\n")))
        self._assert_refused(_deploy(repo, dest, "origin/main"), dest, deployed_v1, "bash -n")

    def test_bare_var_before_non_ascii(self, repo, dest, deployed_v1):
        repo.set_main(repo.commit(_script("BAD", extra='X=1\necho "a=$X）"\n')))
        self._assert_refused(_deploy(repo, dest, "origin/main"), dest, deployed_v1, "裸变量 $X")

    def test_shape(self, repo, dest, deployed_v1):
        repo.set_main(repo.commit("#!/bin/bash\necho hi\n"))
        self._assert_refused(_deploy(repo, dest, "origin/main"), dest, deployed_v1, "形状")


class TestDrift:
    @pytest.fixture
    def hand_edited(self, repo, dest):
        repo.set_main(repo.commit(_script("V1")))
        _deploy(repo, dest, "origin/main")
        dest.write_text(_script("V1") + "# 生产热修复\n", encoding="utf-8")
        repo.set_main(repo.commit(_script("V2")))
        return dest.read_bytes()

    def test_refuses_to_overwrite_hand_edit(self, repo, dest, hand_edited):
        r = _deploy(repo, dest, "origin/main")
        assert r["outcome"] == "refused_drift" and r["drift"] is True
        assert dest.read_bytes() == hand_edited and _leftovers(dest) == []

    def test_accept_drift_backs_up_first(self, repo, dest, hand_edited):
        r = _deploy(repo, dest, "origin/main", accept_drift=True)
        assert r["outcome"] == "deployed" and "V2-END" in dest.read_text()
        backup = Path(r["backup"])
        assert backup.read_bytes() == hand_edited and _leftovers(dest) == [backup.name]

    def test_gate_failure_wins_over_drift(self, repo, dest, hand_edited):
        repo.set_main(repo.commit(_script("BAD", extra="if true; then\n")))
        r = _deploy(repo, dest, "origin/main", accept_drift=True)
        assert r["outcome"] == "refused_gate" and dest.read_bytes() == hand_edited


class TestErrorsAreNotSwallowed:
    def test_unknown_ref(self, repo, dest):
        repo.set_main(repo.commit(_script("V1")))
        r = _deploy(repo, dest, "no-such-ref")
        assert r["outcome"] == "error" and "rev-parse" in r["detail"] and not dest.exists()

    def test_main_ref_without_the_file(self, repo, dest):
        (repo.root / "other.txt").write_text("x")
        repo._g("add", "-A")
        repo._g("commit", "-q", "-m", "c")
        repo.set_main(repo._g("rev-parse", "HEAD"))
        repo.commit(_script("V1"))
        r = _deploy(repo, dest, "HEAD")
        assert r["outcome"] == "error" and "历史里没有" in r["detail"]


class TestCli:
    def test_exit_codes_and_out_file(self, repo, dest, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(dep, "_repo_root", lambda: repo.root)
        repo.set_main(repo.commit(_script("V1")))
        out = tmp_path / "res" / "deploy.json"
        assert dep.main(["--ref", "origin/main", "--dest", str(dest), "--out", str(out)]) == 0
        assert json.loads(out.read_text())["outcome"] == "deployed"
        assert json.loads(capsys.readouterr().out)["outcome"] == "deployed"
        dest.write_text(dest.read_text() + "# 手改\n")
        assert dep.main(["--ref", "origin/main", "--dest", str(dest)]) == 2
        assert dep.main(["--ref", "nope", "--dest", str(dest)]) == 3

    def test_ref_is_required(self):
        with pytest.raises(SystemExit):
            dep.main([])


# ════════════════════════════════════════════════════════════════════════════
# v0.45.370（阶段 3）：历史按「每个可达提交」枚举；记录 main_commit；从检出按 --ref HEAD 真跑 CLI
# ════════════════════════════════════════════════════════════════════════════

def _edit_line(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


class TestHistoryIncludesMergeResults:
    """`git log --raw -- path` 的两处盲区（默认历史简化 + 合并提交不出 diff），v0.45.370 起改为逐提交枚举。"""

    def test_tip_blob_created_by_auto_merge_is_on_main(self, repo, dest):
        """两边改不同行、自动合并 ⇒ 合并提交里的文件是**哪一边都没有过**的新 blob。
        变红的变异：`main_history_blobs` 退回 `git log --raw`（⇒ 假 refused_gate，阶段 3 每轮一条 P1）。"""
        base_text = _script("M")
        base = repo.commit(base_text)
        repo._g("checkout", "-q", "-b", "side", base)
        repo.commit(_edit_line(base_text, "# filler 5\n", "# filler 5 side\n"))
        repo._g("checkout", "-q", "main")
        repo.commit(_edit_line(base_text, "# filler 400\n", "# filler 400 main\n"))
        repo._g("merge", "-q", "--no-edit", "side")
        tip = repo._g("rev-parse", "HEAD")
        merged = (repo.root / dep.REL_PATH).read_text(encoding="utf-8")
        assert "filler 5 side" in merged and "filler 400 main" in merged, "前提：确实是自动合并出的新内容"
        repo.set_main(tip)
        r = _deploy(repo, dest, "origin/main")
        assert r["outcome"] == "deployed", r

    def test_main_blob_dropped_by_merge_is_not_drift(self, repo, dest):
        """main 上 C 已部署；合并时「选边」取 side 的版本 ⇒ 默认历史简化会把 C 从 `log -- path` 里剪掉 ⇒
        部署副本（= C）被误判成漂移。变红的变异：同上。"""
        base_text = _script("B")
        base = repo.commit(base_text)
        c = repo.commit(_edit_line(base_text, "# filler 7\n", "# filler 7 C\n"))
        repo.set_main(c)
        assert _deploy(repo, dest, "origin/main")["outcome"] == "deployed"
        repo._g("checkout", "-q", "-b", "side", base)
        repo.commit(_edit_line(base_text, "# filler 9\n", "# filler 9 S\n"))
        repo._g("checkout", "-q", "main")
        repo._g("merge", "-q", "--no-ff", "--no-commit", "side")
        repo._g("checkout", "side", "--", dep.REL_PATH)
        repo._g("commit", "-q", "-m", "merge side, take side's orchestrator")
        repo.set_main(repo._g("rev-parse", "HEAD"))
        r = _deploy(repo, dest, "origin/main")
        assert r["outcome"] == "deployed" and r["drift"] is False, r

    def test_history_equals_bruteforce(self, repo):
        """集合 = 对每个可达提交 `rev-parse <c>:path`（文件不存在的提交跳过）——按定义对账。"""
        base_text = _script("X")
        base = repo.commit(base_text)
        repo._g("checkout", "-q", "-b", "side", base)
        repo.commit(_edit_line(base_text, "# filler 3\n", "# filler 3 s\n"))
        repo._g("checkout", "-q", "main")
        repo.commit(_edit_line(base_text, "# filler 300\n", "# filler 300 m\n"))
        repo._g("merge", "-q", "--no-edit", "side")
        (repo.root / "other.txt").write_text("x", encoding="utf-8")
        repo._g("add", "-A")
        repo._g("commit", "-q", "-m", "unrelated")
        repo.set_main(repo._g("rev-parse", "HEAD"))
        brute = set()
        for c in repo._g("rev-list", "origin/main").split():
            r = subprocess.run(["git", "-C", str(repo.root), "rev-parse", f"{c}:{dep.REL_PATH}"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                brute.add(r.stdout.strip())
        assert dep.main_history_blobs(repo.root, "origin/main") == brute and len(brute) == 4


class TestRecordsMainCommit:
    def test_main_commit_on_every_outcome(self, repo, dest):
        """一致性守卫拿 `main_commit` 判「合入后待下一轮」。变红的变异：不记录 / 记成 ref 名。"""
        repo.set_main(repo.commit(_script("V1")))
        tip = repo._g("rev-parse", "origin/main")
        assert _deploy(repo, dest, "origin/main")["main_commit"] == tip          # deployed
        assert _deploy(repo, dest, "origin/main")["main_commit"] == tip          # already_current
        side = repo.commit(_script("V2"))                                         # 未合入
        r = _deploy(repo, dest, side)
        assert r["outcome"] == "refused_gate" and r["main_commit"] == tip


class TestCliFromCheckoutAsPhase3CallsIt:
    """编排器阶段 3 的真实调用形状：在生产检出里 `deploy_orchestrator.py --ref HEAD --out <记录>`，**不传 --dest**。
    把工具（未跟踪）拷进临时仓库、子进程真跑 ⇒ `__file__` 解析仓库、默认目标跟 HOME 走都被真执行。"""

    def test_ref_head_default_dest(self, repo, tmp_path):
        import pwd
        import shutil
        import sys
        # 真实部署副本按账户家目录求，**不经 HOME**：autouse 已把 HOME 指向沙箱，而 `tests._orchestrator` 若在本用例里
        # 才首次被导入，它的 DEPLOYED_ORCH 会冻在沙箱里（实测踩过）⇒ 下面的「真实副本没被动」就恒真了。
        DEPLOYED_ORCH = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude" / "scripts" / "alpha-hive-orchestrator.sh"
        assert not DEPLOYED_ORCH.resolve().is_relative_to(tmp_path.resolve())
        root_code = Path(dep.__file__).resolve().parent
        for f in ("deploy_orchestrator.py", "orchestrator_lint.py"):
            shutil.copy(root_code / f, repo.root / f)
        repo.set_main(repo.commit(_script("P3")))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        home = Path(env["HOME"]).resolve()
        assert home.is_relative_to(tmp_path.resolve()), "HOME 没指向沙箱——会写到真实部署副本"

        def _fp():
            if not DEPLOYED_ORCH.exists():
                return None
            st = DEPLOYED_ORCH.stat()
            import hashlib
            return hashlib.sha256(DEPLOYED_ORCH.read_bytes()).hexdigest(), st.st_ino
        before = _fp()
        out = tmp_path / "logs" / "orchestrator_deploy.json"
        r = subprocess.run([sys.executable, str(repo.root / "deploy_orchestrator.py"), "--ref", "HEAD", "--out", str(out)],
                           capture_output=True, text=True, env=env, cwd=str(tmp_path))
        assert _fp() == before, "真实部署副本被动了"
        assert r.returncode == 0, r.stdout + r.stderr
        rec = json.loads(out.read_text(encoding="utf-8"))
        assert rec["outcome"] == "deployed" and rec["main_commit"] == repo._g("rev-parse", "origin/main")
        deployed = home / ".claude" / "scripts" / "alpha-hive-orchestrator.sh"
        assert deployed.read_text(encoding="utf-8") == _script("P3")
        assert os.stat(deployed).st_mode & 0o777 == 0o755


class TestSigtermCleansUp:
    """编排器 run_step 超时先发 SIGTERM：临时文件不许留在部署目录（二次审查实测过 Python 默认 TERM 不跑 finally）。
    变红的变异：删掉 `signal.signal(SIGTERM, _exit_on_sigterm)`。"""

    def test_term_mid_gate_leaves_no_tmp(self, repo, dest, tmp_path):
        import signal
        import sys
        repo.set_main(repo.commit(_script("T1")))
        stall = tmp_path / "stall_bash.sh"
        stall.write_text("#!/bin/bash\nexec sleep 6\n", encoding="utf-8")   # 孤儿最多活 6s
        stall.chmod(0o755)
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(Path(dep.__file__).resolve().parent)!r})\n"
            "import deploy_orchestrator as d\n"
            f"d._repo_root = lambda: __import__('pathlib').Path({str(repo.root)!r})\n"
            f"d.BASH = {str(stall)!r}\n"
            f"sys.exit(d.main(['--ref', 'origin/main', '--dest', {str(dest)!r}]))\n", encoding="utf-8")
        p = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (dest.parent.exists() and _leftovers(dest)):
            time.sleep(0.05)
        assert _leftovers(dest), "前提：临时文件已建出、正卡在关卡里"
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)
        assert p.returncode == 143, (p.returncode, p.stderr.read())
        assert _leftovers(dest) == [] and not dest.exists()
