"""部署副本必须是 `origin/main` 上的最新版（v0.45.353，编排器纳入版本控制·阶段 1）

背景
----
v0.45.353 起编排器的唯一真相是仓库 `scripts/alpha-hive-orchestrator.sh`；launchd 执行的是
`~/.claude/scripts/alpha-hive-orchestrator.sh`（部署副本，路径不变——Desktop 有 TCC 限制，
不让 launchd 下的 bash 直接跑仓库里那份，见编排器 production_sync 段注释）。

阶段 3（v0.45.370）起编排器每轮扫描前、生产同步成功后自动部署（`_orchestrator_autodeploy` →
`deploy_orchestrator.py --ref HEAD --out ~/.claude/logs/orchestrator_deploy.json`）；手动部署仍可用（首次上线 / 回滚）。
两份文件有两种坏法，本文件各拦一种：

* **漂移**：部署副本的内容在 `origin/main` 的历史里从没出现过 ⇒ 有人绕过仓库直接改了生产。
  这正是纳入版本控制要消灭的状态；不拦，下一次部署会把那次热修复静默冲掉。
* **未部署**（且不属于「待下一轮」）：内容是 main 上的**旧**版本。阶段 3 起「合入 → 下一轮扫描部署」之间是**正常态**，
  所以这条读部署记录判：上一轮部署成功、记录里的副本就是现在的副本、那一轮判定用的 main 提交仍在 main 上且当时
  部署的正是它的版本、记录不超过 `PENDING_MAX` 天 ⇒ 合入发生在上一轮之后，放行；否则红，并按理由给出处置。

谁会红：本机跑全套时（部署副本只在装了定时任务的那台 Mac 上，别处按类 skip）。每轮扫描的结局另进
status.json `steps_result.orchestrator_deploy`（失败 ⇒ alert_manager 的「步骤失败」P1）。
"""

import json
import subprocess
from datetime import datetime, timedelta

import pytest

import deploy_orchestrator
from tests._orchestrator import DEPLOY_RECORD, DEPLOYED_ORCH, REPO_ORCH

_ROOT = REPO_ORCH.parent.parent
_REL = REPO_ORCH.relative_to(_ROOT).as_posix()
_REF = "origin/main"


def classify(deployed_blob: str, history_blobs: set, tip_blob: str) -> str:
    """`latest` / `stale`（旧版未部署新版）/ `drift`（内容不在 git 历史里）。"""
    if deployed_blob == tip_blob:
        return "latest"
    return "stale" if deployed_blob in history_blobs else "drift"


def _git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(_ROOT), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)} 失败（rc={r.returncode}）：{r.stderr.strip()}"
    return r.stdout.strip()


#: 「合入后待下一轮」最多容忍多久（日历天）。最长的非交易间隔（长周末）+ 余量；超过 ⇒ 扫描停了，该红。
#: 交易日间隔**待核**：这里取日历天是保守上界（误红只在机器停机 > 6 天时出现，那本来就该有人看）。
PENDING_MAX = timedelta(days=6)
_RECORD_OK = frozenset({"deployed", "already_current"})


def pending_verdict(record, deployed, main_commit_blob, main_commit_on_main, now):
    """部署副本是 main 上的旧版（stale）时才调用：None = 合入后待下一轮（放行）；否则返回红的理由（字符串）。

    放行要同时满足：上一轮部署成功；记录里的副本就是现在的副本（之后没人手动换过）；那一轮判定用的 main 提交仍在
    当前 main 上、且当时部署的正是它的版本（⇒ 当时已是最新，stale 是之后才合入的）；记录不超过 PENDING_MAX。
    """
    if record is None:
        return "no_record"
    if record.get("outcome") not in _RECORD_OK:
        return "last_deploy_not_ok"
    if record.get("candidate_blob") != deployed:
        return "copy_changed_after_last_deploy"
    if not record.get("main_commit") or main_commit_on_main is None:
        return "record_main_commit_unknown"
    if main_commit_on_main is False:
        return "record_main_commit_not_on_main"
    if main_commit_blob != deployed:
        return "last_run_deployed_older_than_its_main"
    try:
        at = datetime.fromisoformat(record["at"])
    except (KeyError, TypeError, ValueError):
        return "record_no_timestamp"
    if now - at > PENDING_MAX:
        return "no_scan_since_record"
    return None


_REMEDY = {
    "no_record": ("没有部署记录（阶段 3 首次上线前、或记录被删）。手动部署并留记录：\n"
                  f"  /usr/local/bin/python3 '{_ROOT}/deploy_orchestrator.py' --ref {_REF} --dry-run\n"
                  f"  /usr/local/bin/python3 '{_ROOT}/deploy_orchestrator.py' --ref {_REF} --out {DEPLOY_RECORD}"),
    "last_deploy_not_ok": "上一轮自动部署没成功——看 status.json 的 steps_result.orchestrator_deploy 与 scan_timing.production_sync",
    "copy_changed_after_last_deploy": "部署记录之后部署副本被换过（手动部署 / 回滚没留 --out 记录？）",
    "record_main_commit_unknown": "部署记录缺 main_commit，或无法判断它在不在 main 上",
    "record_main_commit_not_on_main": "部署记录里的 main 提交已不在 origin/main 上（main 被改写？）",
    "last_run_deployed_older_than_its_main": "上一轮部署时 main 上已有更新的编排器却没部署它",
    "record_no_timestamp": "部署记录没有可读的时间戳",
    "no_scan_since_record": f"部署记录超过 {PENDING_MAX.days} 天没更新——扫描停了？见 alpha-hive-scan-continuity",
}


def _load_record():
    if not DEPLOY_RECORD.exists():
        return None
    try:
        rec = json.loads(DEPLOY_RECORD.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise AssertionError(f"部署记录 {DEPLOY_RECORD} 读不出：{e}")
    assert isinstance(rec, dict), f"部署记录 {DEPLOY_RECORD} 不是 JSON 对象"
    return rec


def _is_ancestor(root, a: str, b: str):
    r = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", a, b], capture_output=True)
    return {0: True, 1: False}.get(r.returncode)


def stale_verdict(root, ref: str, rel: str, record, deployed: str, now):
    """stale 时的完整判定（读记录之后的胶水）：记录里的 main 提交在不在 `ref` 上、那个提交里的编排器是哪版，
    再交给 `pending_verdict`。抽成函数是为了能在临时仓库里测（二次审查：原先这段只在生产 Mac 上、且副本恰好
    stale 时才跑到）。"""
    mc = (record or {}).get("main_commit")
    on_main = _is_ancestor(root, mc, ref) if mc else None
    mblob = None
    if mc:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", f"{mc}:{rel}"], capture_output=True, text=True)
        mblob = r.stdout.strip() if r.returncode == 0 else None
    return pending_verdict(record, deployed, mblob, on_main, now)


class TestClassifyHasTeeth:
    """纯函数，任何机器都跑：三种结局各喂一次。"""

    def test_three_outcomes(self):
        hist = {"a1", "b2", "c3"}
        assert classify("c3", hist, "c3") == "latest"
        assert classify("a1", hist, "c3") == "stale"
        assert classify("zz", hist, "c3") == "drift"


class TestPendingVerdictHasTeeth:
    """纯函数、`now` 注入（无时间炸弹），任何机器都跑：每个返回分支各喂一次。"""

    NOW = datetime(2026, 10, 1, 14, 30)

    def _rec(self, **kw):
        r = {"outcome": "deployed", "candidate_blob": "d1", "main_commit": "m1", "at": "2026-09-30T14:05:00"}
        r.update(kw)
        return r

    def _v(self, rec, *, deployed="d1", mblob="d1", on_main=True, now=None):
        return pending_verdict(rec, deployed, mblob, on_main, now or self.NOW)

    def test_pending_passes(self):
        assert self._v(self._rec()) is None
        assert self._v(self._rec(outcome="already_current")) is None

    def test_exactly_pending_max_passes_and_one_second_more_fails(self):
        at = datetime.fromisoformat(self._rec()["at"])
        assert self._v(self._rec(), now=at + PENDING_MAX) is None
        assert self._v(self._rec(), now=at + PENDING_MAX + timedelta(seconds=1)) == "no_scan_since_record"

    @pytest.mark.parametrize("kw,want", [
        ({"rec": None}, "no_record"),
        ({"rec": {"outcome": "refused_gate", "candidate_blob": "d1", "main_commit": "m1", "at": "2026-09-30T14:05:00"}},
         "last_deploy_not_ok"),
        ({"deployed": "OTHER"}, "copy_changed_after_last_deploy"),
        ({"rec": {"outcome": "deployed", "candidate_blob": "d1", "at": "2026-09-30T14:05:00"}}, "record_main_commit_unknown"),
        ({"on_main": None}, "record_main_commit_unknown"),
        ({"on_main": False}, "record_main_commit_not_on_main"),
        ({"mblob": "OLDER"}, "last_run_deployed_older_than_its_main"),
        ({"rec": {"outcome": "deployed", "candidate_blob": "d1", "main_commit": "m1"}}, "record_no_timestamp"),
    ])
    def test_each_red_branch(self, kw, want):
        rec = kw.pop("rec", self._rec())
        assert self._v(rec, **kw) == want
        assert want in _REMEDY


class TestDeployedMatchesRepo:
    @pytest.fixture(scope="class")
    def blobs(self):
        if not DEPLOYED_ORCH.is_file():
            pytest.skip("部署副本不在本机（只在装了定时任务的那台 Mac 上）")
        # 浅克隆没有完整历史 ⇒ 判不了「在不在 main 历史里」（会把合入后待下一轮误判成漂移，二次审查实测）。
        # 「浅克隆在哪些环境里存在」：只有刻意 `--depth 1` 仿 CI 的检出；那里本就判不了，skip 是如实。
        if _git("rev-parse", "--is-shallow-repository") == "true":
            pytest.skip("浅克隆：没有完整历史，判不了漂移 / 未部署")
        # 仓库与 git 在任何开发检出里都在 ⇒ 下面取不到就是真错，断言不 skip
        # v0.45.370：历史与部署工具同一个定义（逐可达提交枚举，合并产物不漏）
        history = deploy_orchestrator.main_history_blobs(_ROOT, _REF)
        assert history, f"{_REF} 的历史里没有 {_REL} —— 读错了 ref 或路径，下面的判定会空转"
        tip = _git("rev-parse", f"{_REF}:{_REL}")
        deployed = _git("hash-object", str(DEPLOYED_ORCH))
        return deployed, history, tip

    def test_deployed_copy_is_not_hand_edited(self, blobs):
        deployed, history, tip = blobs
        assert classify(deployed, history, tip) != "drift", (
            f"部署副本 {DEPLOYED_ORCH} 的内容不在 {_REF} 的任何一版 {_REL} 里 —— "
            "有人绕过仓库直接改了生产。把那次改动搬进仓库 scripts/ 提交合入，再重新部署；"
            "别直接覆盖（会丢掉那次改动）。先 `diff` 看清改了什么。")

    def test_latest_main_version_is_deployed_or_pending(self, blobs):
        """stale（副本是 main 上的旧版）时：属于「合入后待下一轮」就放行，否则红并说明理由与处置。"""
        deployed, history, tip = blobs
        if classify(deployed, history, tip) != "stale":
            return
        rec = _load_record()
        why = stale_verdict(_ROOT, _REF, _REL, rec, deployed, datetime.now())
        assert why is None, (
            f"{_REF} 上的编排器比部署副本新，且不属于「合入后待下一轮」：{why} —— {_REMEDY.get(why, '')}"
            + (f"\n  记录：outcome={rec.get('outcome')} reason={rec.get('reason')} detail={rec.get('detail')}" if rec else ""))


class TestStaleVerdictInATempRepo:
    """胶水 `stale_verdict` 在临时仓库里真跑 git（任何机器都跑）：A→B 两版合入 main，副本停在 A。"""

    @pytest.fixture
    def repo(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()

        def g(*a):
            r = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                                "-c", "commit.gpgsign=false", *a], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            return r.stdout.strip()
        g("init", "-q", "-b", "main")
        f = root / "orch.sh"
        f.write_text("A\n")
        g("add", "-A"); g("commit", "-q", "-m", "A")
        A = g("rev-parse", "HEAD")
        f.write_text("B\n")
        g("add", "-A"); g("commit", "-q", "-m", "B")
        B = g("rev-parse", "HEAD")
        g("checkout", "-q", "-b", "side", A)
        f.write_text("S\n")
        g("add", "-A"); g("commit", "-q", "-m", "S")
        S = g("rev-parse", "HEAD")
        g("checkout", "-q", "main")

        def blob(c):
            return g("rev-parse", f"{c}:orch.sh")
        return root, {"A": A, "B": B, "S": S}, blob

    def _rec(self, mc, cand):
        return {"outcome": "deployed", "candidate_blob": cand, "main_commit": mc,
                "at": (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")}

    def test_pending_after_merge_passes(self, repo):
        root, c, blob = repo
        a = blob(c["A"])
        assert stale_verdict(root, "main", "orch.sh", self._rec(c["A"], a), a, datetime.now()) is None

    def test_last_run_deployed_older_than_its_main(self, repo):
        """上一轮时 main 已是 B，却部署了 A（例：手动 `--ref <旧提交>` 回滚）⇒ 红。"""
        root, c, blob = repo
        a = blob(c["A"])
        assert (stale_verdict(root, "main", "orch.sh", self._rec(c["B"], a), a, datetime.now())
                == "last_run_deployed_older_than_its_main")

    def test_record_main_commit_not_on_main(self, repo):
        root, c, blob = repo
        a = blob(c["A"])
        assert (stale_verdict(root, "main", "orch.sh", self._rec(c["S"], a), a, datetime.now())
                == "record_main_commit_not_on_main")

    def test_unknown_commit(self, repo):
        root, c, blob = repo
        a = blob(c["A"])
        assert (stale_verdict(root, "main", "orch.sh", self._rec("f" * 40, a), a, datetime.now())
                == "record_main_commit_unknown")
