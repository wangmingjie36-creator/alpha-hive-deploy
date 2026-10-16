"""编排器里没有任何路径能打开 LLM 模式；扫描前 Slack 确认已去掉（v0.45.510）。

背景：编排器曾在 Step 2 前调 `pre_scan_notify.py` 等 10 分钟用户回复，exit 42 ⇒ `LLM_FLAG="--use-llm"`。
现在每天由 launchd 自己跑、Slack Webhook 早已 404，那一步只是每天空等 10 分钟。去掉它的同时，
**「编排器永远 --no-llm」是个花钱护栏**（~/.claude/CLAUDE.md「花钱前必须先问」对定时任务同样生效）：
`LLM_FLAG` 只许有一处纯字面赋值 `--no-llm`，且代码里（注释除外）不再出现 `--use-llm` / `pre_scan_notify`
/ 旧防重复标记。以后谁要重新开 LLM 路径，必须先改这条测试——也就是先过一遍「要花钱」这个问题。

不在这里测的：`pre_scan_notify.py` 本身仍在（`test_slack_send_whitelist` 把它登记为合法发送方），只是没人调用。
"""
from tests._orchestrator import orch_literal, repo_orchestrator_text

#: 代码（非注释行）里不许再出现的符号。`--use-llm` 是日报 CLI 的开关，编排器不该传；其余是旧确认链的零件。
BANNED_IN_CODE = ("--use-llm", "pre_scan_notify", "PRE_SCAN_SCRIPT", "NOTIFY_RC", "LLM_DONE_FILE",
                  "alpha_hive_llm_done")


def _code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def violations(text: str) -> list:
    out = []
    if orch_literal(text, "LLM_FLAG") != "--no-llm":
        out.append('LLM_FLAG 不是「唯一一处纯字面 `--no-llm`」（多处赋值 / 非字面 / 值不对）')
    code = _code(text)
    out += [f"代码里出现 {b!r}" for b in BANNED_IN_CODE if b in code]
    return out


class TestOrchestratorNeverEnablesLLM:

    def test_repo_orchestrator_has_no_violations(self):
        assert violations(repo_orchestrator_text()) == []

    def test_step2_scan_still_receives_the_flag(self):
        """旗标要真的传给日报：否则「恒为 --no-llm」只是一个没人读的变量，而日报自己的缺省才是实际行为。"""
        code = _code(repo_orchestrator_text())
        lines = [ln for ln in code.splitlines() if "alpha_hive_daily_report.py" in ln and "--swarm" in ln]
        assert len(lines) == 1 and "$LLM_FLAG" in lines[0], lines

    def test_guard_has_teeth(self):
        """正对照先过（上一条）；再把旧确认链的几种形状种进去，必须每一种都被抓到。"""
        base = repo_orchestrator_text()
        assert violations(base) == []
        old_chain = (
            'PRE_SCAN_SCRIPT="$PROJECT_DIR/pre_scan_notify.py"\n'
            'run_step --timeout 900 "$PRE_SCAN_SCRIPT" --wait 5\n'
            'NOTIFY_RC=$?\n'
            'if [ $NOTIFY_RC -eq 42 ]; then\n    LLM_FLAG="--use-llm"\nfi\n')
        assert violations(base + "\n" + old_chain), "把旧确认链整段放回去没被抓到"
        assert violations(base + '\nLLM_FLAG="--use-llm"\n'), "第二处 LLM_FLAG 赋值没被抓到"
        assert violations(base.replace('LLM_FLAG="--no-llm"', 'LLM_FLAG="$X"', 1)), "LLM_FLAG 改成非字面没被抓到"
        assert violations(base + '\nrun_step x --use-llm\n'), "代码里出现 --use-llm 没被抓到"
        # 注释里提到这些名字是允许的（本版注释就在解释为什么去掉它）
        assert violations(base + "\n# 以前这里调 pre_scan_notify，exit 42 ⇒ --use-llm\n") == []
