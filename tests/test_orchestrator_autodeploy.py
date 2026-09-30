"""编排器阶段 3 自动部署块的守卫（v0.45.370）。

`_orchestrator_autodeploy`（仓库编排器、经 `tests/_orchestrator.py` 读 ⇒ CI 上也跑）在扫描前、生产同步之后
调 `deploy_orchestrator.py --ref HEAD --out <记录>`。这里守两件事：

1. **位置与形状**（静态）：只调一次、在 production_sync 之后 / STEPS_RESULT 初始化之后 / Step 1 之前；
   同步成功才置 `_PROD_SYNC_OK=1`；函数体不许出现 `--accept-drift` / `--dry-run` / `--dest` / `set_status` /
   `OVERALL_STATUS` / `exit` / `exec` / `cp` / `ln`，run_step 不许放进命令替换或管道（会白等满超时）。
2. **行为**（抽出函数 + 真 `run_step`，`/bin/bash` + `set -uo pipefail`，C 与 UTF-8 两种 locale）：
   任何结局都走到下一句（不中断扫描）、不升级 OVERALL_STATUS、不冲掉别的 steps_result、stderr 无 unbound variable；
   分支只认记录里的 outcome，不认退出码；非成功结局一律写一份小记录（先删旧的）。

需要 `/bin/bash` 与 `jq`（编排器本身就依赖二者；CI 的 ubuntu-latest 自带）。假 PYTHON3 是一个 bash 脚本 ⇒
真部署工具永远不会被执行。
"""
import json
import os
import re
import subprocess
import time

import pytest

from tests._orchestrator import DEPLOY_RECORD, extract_function, repo_orchestrator_text

_FUNC = "_orchestrator_autodeploy"
_TEXT = repo_orchestrator_text()
_BODY = extract_function(_TEXT, _FUNC)
_RUN_STEP = extract_function(_TEXT, "run_step")


def _strip_comments(src: str) -> str:
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


# ════════════════════════════════════════════════════════════════════════════
# 1. 位置与形状
# ════════════════════════════════════════════════════════════════════════════

class TestPlacement:

    def test_called_once_between_sync_init_and_step1(self):
        calls = [m.start() for m in re.finditer(rf"^{_FUNC}$", _TEXT, re.M)]
        assert len(calls) == 1, calls
        call = calls[0]
        assert (_TEXT.index('production_sync.py" --date') < _TEXT.index('OVERALL_STATUS="success"')
                < call < _TEXT.index("STEP1_START"))

    def test_sync_flag_set_only_on_success(self):
        """`_PROD_SYNC_OK=1` 只出现一次、在同步 `if !` 的 else 分支里（同步命令与 `_WL_RAW=` 之间）；
        `=0` 只出现一次、在同步段之前。变红的变异：把 `=1` 挪到 if 外（同步失败也部署 ⇒ 可能降级）。"""
        ones = [m.start() for m in re.finditer(r"^\s*_PROD_SYNC_OK=1$", _TEXT, re.M)]
        zeros = [m.start() for m in re.finditer(r"^_PROD_SYNC_OK=0$", _TEXT, re.M)]
        assert len(ones) == 1 and len(zeros) == 1
        sync = _TEXT.index('production_sync.py" --date')
        # `_WL_RAW=` 在同步前也出现过（第一次读 WATCHLIST），取同步之后那一处
        assert zeros[0] < _TEXT.index('if [ -f "$PROJECT_DIR/production_sync.py" ]') < sync < ones[0] < _TEXT.index("_WL_RAW=", sync)
        between = _TEXT[sync:ones[0]]
        assert [ln.strip() for ln in between.splitlines() if ln.strip()][-1] == "else", between[-200:]

    def test_body_shape(self):
        body = _strip_comments(_BODY)
        for need in ("run_step --timeout", "--ref HEAD", '--out "${_rec}"', '>> "${LOGFILE}" 2>&1'):
            assert need in body, need
        for banned in ("--accept-drift", "--dry-run", "--dest", "set_status", "OVERALL_STATUS", "exit",
                       "exec ", "SCRIPTDIR", "cp ", "ln "):
            assert banned not in body, banned
        assert re.search(r"\$\([^)]*run_step|run_step[^\n]*\|", body) is None, "run_step 进了命令替换 / 管道"

    def test_record_path_is_the_guards_path(self):
        """bash 写的记录路径 = 一致性守卫读的路径（`tests/_orchestrator.DEPLOY_RECORD`）。"""
        assert '"${LOGDIR}/orchestrator_deploy.json"' in _BODY
        assert re.search(r'^LOGDIR="[^"]*/\.claude/logs"$', _TEXT, re.M)
        assert DEPLOY_RECORD.parts[-3:] == (".claude", "logs", "orchestrator_deploy.json")


# ════════════════════════════════════════════════════════════════════════════
# 2. 行为
# ════════════════════════════════════════════════════════════════════════════

_FAKE = r'''#!/bin/bash
# 假 PYTHON3：-c（run_step 的 TCC 探针）直接成功；否则记下 argv，按 FAKE_MODE 写 --out 记录并以对应码退出
if [ "$1" = "-c" ]; then exit 0; fi
printf '%s\n' "$*" > "$FAKE_ARGS"
out=""; prev=""
for a in "$@"; do [ "$prev" = "--out" ] && out="$a"; prev="$a"; done
rec() { printf '%s\n' "$1" > "$out"; }
case "$FAKE_MODE" in
  deployed)        rec '{"outcome": "deployed", "previous_blob": "aaaa1111bbbb", "candidate_blob": "cccc2222dddd"}'; exit 0 ;;
  already_current) rec '{"outcome": "already_current", "candidate_blob": "cccc2222dddd"}'; exit 0 ;;
  refused_gate)    rec '{"outcome": "refused_gate", "detail": "1 项关卡未过", "gate_failures": ["bash -n 失败：第 3 行"]}'; exit 1 ;;
  refused_drift)   rec '{"outcome": "refused_drift", "detail": "有人直接改过生产"}'; exit 2 ;;
  error)           rec '{"outcome": "error", "detail": "GitError"}'; exit 3 ;;
  would_deploy)    rec '{"outcome": "would_deploy"}'; exit 0 ;;
  crash)           echo "Traceback" >&2; exit 1 ;;
  outfail)         echo '{"outcome": "deployed"}'; exit 3 ;;
  garbage)         rec '[1, 2]'; exit 0 ;;
  hang)            exec sleep 100 ;;
esac
exit 9
'''


def _run(tmp_path, mode, *, locale, sync="1", tool=True, timeout_override=None):
    proj, logs = tmp_path / "proj", tmp_path / "logs"
    proj.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    if tool:
        (proj / "deploy_orchestrator.py").write_text("# 空桩：真工具永远不会跑\n", encoding="utf-8")
    fake = tmp_path / "fakepy"
    fake.write_text(_FAKE, encoding="utf-8")
    fake.chmod(0o755)
    rec = logs / "orchestrator_deploy.json"
    rec.write_text('{"outcome": "deployed"}\n', encoding="utf-8")      # 陈旧记录：必须被先删
    body = _BODY
    if timeout_override is not None:
        assert body.count("--timeout 30") == 1
        body = body.replace("--timeout 30", f"--timeout {timeout_override}")
    sync_line = "" if sync is None else f"_PROD_SYNC_OK={sync}"
    script = f"""set -uo pipefail
log() {{ printf 'LOG[%s] %s\\n' "$1" "$2"; }}
LOGFILE={json.dumps(str(tmp_path / 'orch.log'))}
PROJECT_DIR={json.dumps(str(proj))}
LOGDIR={json.dumps(str(logs))}
PYTHON3={json.dumps(str(fake))}
STEPS_RESULT='{{"db_backup": {{"status": "success"}}}}'
OVERALL_STATUS=success
{sync_line}
{_RUN_STEP}
{body}
{_FUNC}
echo SENTINEL
printf 'CHARMAP=%s\\n' "$(locale charmap 2>/dev/null)"
printf 'STATUS=%s\\n' "$OVERALL_STATUS"
printf 'RESULT=%s\\n' "$(printf '%s' "$STEPS_RESULT" | jq -c .)"
"""
    env = dict(os.environ, FAKE_MODE=mode, FAKE_ARGS=str(tmp_path / "args.txt"))
    # 三个都要清：PEP 538 让 Python 3.7+ 往自己的 os.environ 塞 LC_CTYPE=C.UTF-8，只 pop LC_ALL 的话「C」那一路
    # 其实也是 UTF-8（二次审查实测）。再显式设 LC_ALL，两路才真的分开。
    for k in ("LC_ALL", "LC_CTYPE", "LANG"):
        env.pop(k, None)
    env["LC_ALL"] = locale
    _LAST_ENV[0] = f"LC_ALL={locale}\n"
    t0 = time.monotonic()
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    dt = time.monotonic() - t0
    out = r.stdout
    res = json.loads(re.search(r"^RESULT=(.*)$", out, re.M).group(1)) if "RESULT=" in out else None
    return r, out, res, rec, dt, tmp_path / "args.txt"


_LOCALES = ["C", "en_US.UTF-8"]
_LAST_ENV = [""]


def _common(r, out, res, dt, limit=10):
    assert r.returncode == 0, r.stderr
    # C 那一路必须真是 C（不是被 PEP 538 塞进来的 C.UTF-8）——否则两路 locale 是同一路，裸变量紧跟全角的风险测不到
    cm = re.search(r"^CHARMAP=(.*)$", out, re.M)
    assert cm, out
    if "LC_ALL=C\n" in _LAST_ENV[0]:
        assert "UTF-8" not in cm.group(1).upper(), cm.group(1)
    assert "SENTINEL" in out and "STATUS=success" in out, out
    assert res["db_backup"] == {"status": "success"}, res
    assert "unbound variable" not in r.stderr, r.stderr
    assert isinstance(res["orchestrator_deploy"]["duration_seconds"], int)
    assert dt < limit, dt


@pytest.mark.parametrize("locale", _LOCALES)
class TestLiveFunction:

    def test_deployed(self, tmp_path, locale):
        r, out, res, rec, dt, args = _run(tmp_path, "deployed", locale=locale)
        _common(r, out, res, dt)
        e = res["orchestrator_deploy"]
        assert e["status"] == "success" and e["rc"] == 0 and e["outcome"] == "deployed"
        proj, logs = tmp_path / "proj", tmp_path / "logs"
        assert args.read_text().strip() == f"{proj}/deploy_orchestrator.py --ref HEAD --out {logs}/orchestrator_deploy.json"
        assert "LOG[INFO] 🚀" in out and "aaaa1111 → cccc2222" in out

    def test_already_current(self, tmp_path, locale):
        r, out, res, rec, dt, _a = _run(tmp_path, "already_current", locale=locale)
        _common(r, out, res, dt)
        assert res["orchestrator_deploy"]["status"] == "success"

    @pytest.mark.parametrize("mode,rc", [("refused_gate", 1), ("refused_drift", 2), ("error", 3), ("would_deploy", 0)])
    def test_not_ok_outcomes_fail_but_scan_goes_on(self, tmp_path, locale, mode, rc):
        r, out, res, rec, dt, _a = _run(tmp_path, mode, locale=locale)
        _common(r, out, res, dt)
        e = res["orchestrator_deploy"]
        assert e["status"] == "failed" and e["outcome"] == mode and e["rc"] == rc
        assert json.loads(rec.read_text())["outcome"] == mode
        if mode == "refused_gate":
            assert "LOG[ERROR]" in out and "bash -n 失败" in out

    @pytest.mark.parametrize("mode,rc", [("crash", 1), ("outfail", 3), ("garbage", 0)])
    def test_no_parseable_record(self, tmp_path, locale, mode, rc):
        """变红的变异：删掉 `rm -f "${_rec}"`（陈旧的 deployed 记录会被读成本轮成功）。"""
        r, out, res, rec, dt, _a = _run(tmp_path, mode, locale=locale)
        _common(r, out, res, dt)
        e = res["orchestrator_deploy"]
        assert e["status"] == "failed" and e["outcome"] == "no_record" and e["rc"] == rc
        assert json.loads(rec.read_text()) == {"outcome": "no_record"}

    @pytest.mark.parametrize("sync", ["0", None])
    def test_sync_not_ok_skips_without_calling_tool(self, tmp_path, locale, sync):
        """变红的变异：删掉 `_PROD_SYNC_OK` 判断（同步失败也部署 ⇒ 可能把部署副本降级）。"""
        r, out, res, rec, dt, args = _run(tmp_path, "deployed", locale=locale, sync=sync)
        _common(r, out, res, dt)
        e = res["orchestrator_deploy"]
        assert e["status"] == "skipped" and e["reason"] == "production_sync_not_ok" and e["rc"] is None
        assert not args.exists(), "同步没成功却调了部署工具"
        assert json.loads(rec.read_text()) == {"outcome": "skipped", "reason": "production_sync_not_ok"}

    def test_tool_missing(self, tmp_path, locale):
        r, out, res, rec, dt, args = _run(tmp_path, "deployed", locale=locale, tool=False)
        _common(r, out, res, dt)
        assert res["orchestrator_deploy"]["status"] == "failed"
        assert res["orchestrator_deploy"]["outcome"] == "tool_missing" and not args.exists()

    def test_hang_is_bounded(self, tmp_path, locale):
        """超时 ⇒ run_step 124 ⇒ no_record。正常约 1s（超时 1s，`exec sleep` 收 TERM 即死）。
        变红的变异：把 run_step 包进 `$(...)`——看门狗 KILL 宽限里的 `sleep 10` 握着替换管道 ⇒ 约 11s，
        所以上限取 8s 而不是「超时 + 10s」（二次审查：原先 15s 的上限抓不住这个变异，只有静态形状测试抓得住）。"""
        r, out, res, rec, dt, _a = _run(tmp_path, "hang", locale=locale, timeout_override=1)
        _common(r, out, res, dt, limit=8)
        e = res["orchestrator_deploy"]
        assert e["status"] == "failed" and e["rc"] == 124 and e["outcome"] == "no_record"
