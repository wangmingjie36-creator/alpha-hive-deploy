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
