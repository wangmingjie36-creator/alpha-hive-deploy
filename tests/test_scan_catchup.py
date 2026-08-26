"""开机补跑闸的守卫（v0.45.34）。

治的是：plist 原 RunAtLoad=false，关机错过 14:00 就永久漏掉那天
（实测 W29/W32/W34 三周完全无扫描，日志零记录 = 机器没开）。

⚠️ 最危险的不是漏跑，是**修复本身**：RunAtLoad=true 会在每次登录触发，
若无时间闸，早上开机就会在盘中跑，把盘中价当收盘价写进 predictions ——
那正是 MEMORY alpha-hive-accuracy-metrics-suspect 记的头号污染源。
所以本文件的重点是守住时间闸，而不是守住"能补跑"。
"""

import os
import re
import subprocess

import pytest

ORCH = os.path.expanduser("~/.claude/scripts/alpha-hive-orchestrator.sh")
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.alpha.hive.daily.plist")


@pytest.fixture(scope="module")
def orch_text():
    if not os.path.isfile(ORCH):
        pytest.skip("编排器不在本机（仓库外文件）")
    with open(ORCH, encoding="utf-8") as f:
        return f.read()


class TestCatchupGate:
    def test_gate_exists(self, orch_text):
        assert "CATCHUP_AFTER_HHMM" in orch_text, "补跑闸不见了"
        assert "SWARM_MARKER" in orch_text, "幂等标记不见了"

    def test_time_gate_not_before_market_close(self, orch_text):
        """阈值必须在美股收盘（本机 13:00 PT）之后。
        调早到盘中 = 直接制造盘中价污染，比漏跑更糟。"""
        m = re.search(r'CATCHUP_AFTER_HHMM="(\d{4})"', orch_text)
        assert m, "读不到阈值"
        assert int(m.group(1)) >= 1300, (
            f"阈值 {m.group(1)} 早于美股收盘 1300 PT —— 会把盘中价当收盘价")

    def test_gate_runs_before_any_scan_step(self, orch_text):
        """闸必须在 Step 1 之前，否则先抓完数据再退出等于白跑。"""
        gate = orch_text.index("CATCHUP_AFTER_HHMM")
        step1 = orch_text.index("STEP1_START")
        assert gate < step1, "补跑闸在 Step 1 之后，起不到节流作用"

    def test_gate_exits_zero_not_error(self, orch_text):
        """跳过是正常状态，必须 exit 0 —— 非 0 会被编排器/监控当成故障。"""
        seg = orch_text[orch_text.index("CATCHUP_AFTER_HHMM"):
                        orch_text.index("STEP1_START")]
        assert "exit 0" in seg
        assert "exit 1" not in seg

    def test_syntax_valid(self):
        r = subprocess.run(["bash", "-n", ORCH], capture_output=True, text=True)
        assert r.returncode == 0, f"编排器语法错误：{r.stderr}"


class TestPlistRunAtLoad:
    @pytest.fixture(scope="class")
    def plist(self):
        if not os.path.isfile(PLIST):
            pytest.skip("plist 不在本机")
        import plistlib
        with open(PLIST, "rb") as f:
            return plistlib.load(f)

    def test_runatload_enabled(self, plist):
        assert plist.get("RunAtLoad") is True, (
            "RunAtLoad=false —— 关机错过 14:00 的那天将永久漏掉")

    def test_schedule_still_present(self, plist):
        """补跑是**补充**不是替代：定时时刻不能被顺手删掉。"""
        sci = plist.get("StartCalendarInterval") or []
        assert len(sci) >= 5, f"定时时刻少于 5 个工作日：{sci}"
        assert all(e.get("Hour") is not None for e in sci)

    def test_points_at_orchestrator(self, plist):
        args = plist.get("ProgramArguments") or []
        assert any("alpha-hive-orchestrator.sh" in a for a in args)


@pytest.mark.integration
class TestGateBranchesLive:
    """真跑编排器验证幂等闸。标 integration：编排器启动阶段有权限/网络检查，
    耗时超过默认 60s 上限，且依赖本机状态。默认套件跑静态守卫即可，
    这条用 `pytest -m integration tests/test_scan_catchup.py` 单跑。

    （三条分支都已在 v0.45.34 落地时手工端到端验证过，含真实 RunAtLoad 触发。）"""

    def _run(self, tmp_path):
        """⚠️ 不能用 capture_output=True：编排器会 spawn 一个后台全局超时
        看门狗，它继承 stdout/stderr，管道要等**所有**写入端关闭才 EOF，
        于是 subprocess 会一直等到看门狗自己超时退出（实测直接跑只要 0 秒，
        走管道却卡满 120s）。改为重定向到文件。"""
        out = tmp_path / "orch.out"
        with open(out, "w") as fh:
            subprocess.run(["bash", ORCH], stdout=fh, stderr=subprocess.STDOUT,
                           timeout=120, env=dict(os.environ))
        return out.read_text(errors="ignore")

    @pytest.mark.skipif(not os.path.isfile(ORCH), reason="编排器不在本机")
    @pytest.mark.timeout(300)
    def test_idempotent_when_today_already_scanned(self, tmp_path):
        """今日已有 .swarm_results → 必须跳过。
        否则 RunAtLoad 会让每次登录都重跑一次全量扫描。"""
        proj = "/Users/igg/Desktop/Alpha Hive"
        if not os.path.isdir(proj):
            pytest.skip("项目目录不在本机")
        import datetime
        today = datetime.date.today().isoformat()
        marker = os.path.join(proj, f".swarm_results_{today}.json")
        created = not os.path.exists(marker)
        if created:
            with open(marker, "w") as f:
                f.write("{}")
        try:
            out = self._run(tmp_path)
            log = os.path.expanduser(
                f"~/.claude/logs/orchestrator-{today}.log")
            if os.path.exists(log):
                with open(log, encoding="utf-8") as f:
                    out += f.read()[-3000:]
            assert "已有扫描产出" in out, "幂等闸没拦住，会重复全量扫描"
        finally:
            if created:
                os.remove(marker)
