"""编排器的步骤解释器接线（B：v0.45.385 = B1，v0.45.386 = B2）：`_apply_step_interp` / `_step_rc_fallback` 与 Step 2/4/10/11/12/13/15 的调用点。

B 把 Step 10–15 的内联分支链（`CONT_SUMMARY` / `READINESS_LINE` / `BOUNDARY_JSON` / `_S11_NEW` / … 与
`if [ $STEPn_RC -eq … ]` 链）换成一行 `_apply_step_interp`，格式知识只剩 `orchestrator_steps.py` 一份。
本文件守的是**接线本身**；解释器的判定表在 `tests/test_orchestrator_steps.py`（其 bash 对照已钉到 B 之前的
冻结副本 `tests/fixtures/orchestrator_pre_b.sh.frozen`）。

读编排器原文一律经 `tests/_orchestrator.py`（仓库那份，唯一真相）；函数按名字抽出来在 `/bin/bash` 下真跑
（`set -uo pipefail`，bash 3.2 与 Homebrew bash 都行），`log` 换成记录器、其余（`run_step` / `set_status` /
`_status_rank` / 两个 helper / 两个全局量）全是原文。日期按运行时的 `date.today()` 算，不写死（不做时间炸弹）。

分组与「谁会红」（每条变异都在仓库副本的拷贝上实测过，见 CHANGELOG 0.45.385）：
  · TestFrozenFixture —— 冻结副本逐字节钉住：改一个字节 / 从 B 之后的编排器重新生成 ⇒ 红
  · TestGoldenParity —— 旧格式 JSON 经 helper + 真解释器 = B 之前 bash 的片段与级别（GOLDEN 表）；
    helper 多嵌一层 / 丢键 / 改级别映射 ⇒ 红
  · TestNeverTouchesOverall —— helper 任何输入都不动 OVERALL_STATUS：helper 里调 `set_status` ⇒ 红
  · TestFallback —— `_step_rc_fallback` 的 Step 2/4 与冻结副本逐字相同；六种触发（解释器缺失 / 非 JSON /
    错键 / render_error / 超时 / 允许集合外的判定）都退回、都留痕（一行 ERROR + `interp_fallback`）；
    删形状校验里的 `keys == [$k]` / 删允许集合 / 改 10–15 的兜底 status ⇒ 红
  · TestMergeGuard —— STEPS_RESULT 坏了不许被清空：删 `if [ -n "${_new}" ]` ⇒ 红
  · TestRc2ScriptPresent —— 退出码 2 而工具在 ⇒ WARN「参数错误」：删那段 ⇒ 红
  · TestNoCommandSubstitution —— 正常调用远快于看门狗超时：把 `run_step` 包进 `$( )` ⇒ 红（白等满超时）
  · TestLiveWiring —— 调用点结构：每个工具步骤恰一个调用、键对得上；START → `rm -f` → `run_step` 的顺序；
    `run_step` 带 `_TOOL_STEPS[sid].date_flag "${DATE_STR}"`；`--json` / `--run-start` 传对；
    内联分支链与 `"$PYTHON3" -c` 摘要已删；helper 里没有 `set_status`、没有 `$(run_step`
  · TestMacBash —— `/usr/bin/env -i`（C locale）与 `LC_ALL=en_US.UTF-8` 下重跑：无 `unbound variable`、结果不变

  · TestStep2CallSite（B2）—— Step 2 / 4 调用点**原文**抽出来真跑：rc × 起点 × 解释器真 / 缺，OVERALL_STATUS
    与 B 之前的冻结副本逐格相同（只看退出码：rc≠0 ⇒ partial，永不下调）；rc=1 且跑完 ⇒ success_with_warning、
    STEP2_STATUS 在 Step 4 调用之后仍是它；删 `set_status partial` / STEP2_STATUS 挪到 Step 4 之后 /
    --data-dir 改传 REPORTDIR / 删允许集合 ⇒ 各自红
  · TestLiveWiring 的 Step 2/4 部分（B2）—— 两处调用各恰一个、传 `--data-dir "${DATA_DIR}"`、不碰 REPORTDIR；
    `STEP2_STATUS="${_SI_STATUS}"` 紧跟 Step 2 调用，随后是按 STEP2_RC 的 `set_status partial`；旧分支链已删

helper 对 2/4 的兜底与允许集合按 helper 层测（B1 起），调用点与 OVERALL_STATUS 矩阵按原文测（B2 起）。
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

import orchestrator_steps as ost
import step_contract as sc
import tests.test_orchestrator_steps as tos
from tests._orchestrator import extract_function, repo_orchestrator_text

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**（要跑仓库里的 orchestrator_steps.py），故用 __file__

#: B 之前编排器冻结副本的摘要（SHA-256）。**只许在「副本本身有意更换」时改**——它存在的意义就是对照不漂。
PRE_B_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "orchestrator_pre_b.sh.frozen"
_PRE_B_ORCH_DIGEST = "f0445dd2e0e91758e3d3791bddf21a4a147d44945e1d535e1ab90be33be9004d"

TODAY = date.today().isoformat()
TOOL_SIDS = ("10", "11", "12", "13", "15")
KEY = {sid: keys[0] for sid, keys in ost.STEP_KEYS.items()}
SENTINEL = {"sentinel": {"status": "success"}}
_RANK = {"INFO": 0, "WARN": 1, "ERROR": 2}
_LEVEL = {"INFO": "info", "WARN": "warn", "ERROR": "error"}
#: 各工具步骤在调用点上传给解释器的 duration（10 / 15 传 bash 算的耗时，11/12/13 历来不带）
LIVE_DUR = {"10": "7", "11": "", "12": "", "13": "", "15": "7"}
LIVE_TIMEOUT = {"10": "120", "15": "60"}


# ═══════════════════════════════════════════ 夹具：抽原文、在 bash 里真跑 ═══════════════════════════════════════════

_FUNCS = ("_status_rank", "set_status", "run_step", "_step_rc_fallback", "_apply_step_interp")
_GLOBALS = (r"^STEP_INTERP_TIMEOUT=\d+$", r'^_SI_STATUS=""$')


@functools.lru_cache(maxsize=1)
def _live_text() -> str:
    return repo_orchestrator_text()


@functools.lru_cache(maxsize=1)
def _live_parts() -> str:
    text = _live_text()
    parts = [extract_function(text, n) for n in _FUNCS]
    for pat in _GLOBALS:
        hits = re.findall(pat, text, re.M)
        assert len(hits) == 1, f"编排器里 {pat!r} 应恰有一处，实有 {hits}"
        parts.append(hits[0])
    return "\n".join(parts)


def _bash_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    assert shutil.which("jq", path=env["PATH"]), "找不到 jq：编排器本身依赖它，没有它这组就是假绿"
    env.update(extra or {})
    return env


@dataclass
class Call:
    """一次 `_apply_step_interp` 调用。dur / script 用 "" 表示「不传」（与调用点写法一致）。"""
    sid: str
    rc: int
    dur: str = ""
    script: str = ""
    args: Sequence[str] = ()
    key: Optional[str] = None
    overall: str = "success"

    def line(self) -> str:
        argv = ["_apply_step_interp", self.sid, self.key or KEY[self.sid], str(self.rc), self.dur, self.script,
                *map(str, self.args)]
        return " ".join(shlex.quote(a) for a in argv)


@dataclass
class Result:
    logs: List[Tuple[str, str]] = field(default_factory=list)
    overall: str = ""
    si_status: str = ""
    steps_raw: str = ""

    @property
    def steps(self) -> dict:
        return json.loads(self.steps_raw)

    def frag(self, key: str) -> dict:
        return self.steps[key]

    @property
    def level(self) -> str:
        assert self.logs, "helper 一行日志都没打"
        return _LEVEL[max((lv for lv, _ in self.logs), key=_RANK.__getitem__)]

    def errors_with(self, needle: str) -> List[str]:
        return [m for lv, m in self.logs if lv == "ERROR" and needle in m]


@dataclass
class Run:
    results: List[Result]
    stderr: str
    elapsed: float


def run_helper(tmp_path: Path, calls: Sequence[Call], *, project_dir: Path = REPO_ROOT, date_str: str = TODAY,
               steps_result: str = json.dumps(SENTINEL), interp_timeout: Optional[int] = None,
               exports: Optional[Dict[str, str]] = None, argv_prefix: Sequence[str] = (),
               env: Optional[Dict[str, str]] = None, timeout: int = 55) -> Run:
    """在 `/bin/bash` 里：设好编排器的全局量 → 贴原文函数 → 逐个调用，每次调用后记 OVERALL_STATUS / _SI_STATUS / STEPS_RESULT。"""
    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    lines = [
        "set -uo pipefail",
        r"""log() { printf '\036LOG\037%s\037%s' "$1" "$2"; }""",
        f"PYTHON3={shlex.quote(sys.executable)}",
        f"PROJECT_DIR={shlex.quote(str(project_dir))}",
        f"DATA_DIR={shlex.quote(str(tmp_path / 'data'))}",
        f"LOGDIR={shlex.quote(str(logdir))}",
        f"LOGFILE={shlex.quote(str(logdir / 'orchestrator.log'))}",
        f"DATE_STR={shlex.quote(date_str)}",
        f"STEPS_RESULT={shlex.quote(steps_result)}",
        "OVERALL_STATUS=success",
        *(f"export {k}={shlex.quote(v)}" for k, v in (exports or {}).items()),
        _live_parts(),
    ]
    if interp_timeout is not None:
        lines.append(f"STEP_INTERP_TIMEOUT={int(interp_timeout)}")
    for c in calls:
        lines += [f"OVERALL_STATUS={shlex.quote(c.overall)}",
                  r"printf '\036CALL\037'",
                  c.line(),
                  r"""printf '\036STATE\037%s\037%s\037%s' "${OVERALL_STATUS}" "${_SI_STATUS}" "${STEPS_RESULT}" """]
    t0 = time.monotonic()
    p = subprocess.run([*argv_prefix, "/bin/bash", "-c", "\n".join(lines) + "\n"], capture_output=True, text=True,
                       env=env if env is not None else _bash_env(), timeout=timeout, cwd=str(tmp_path))
    elapsed = time.monotonic() - t0
    assert p.returncode == 0, f"bash rc={p.returncode}\nSTDERR:{p.stderr}\nSTDOUT:{p.stdout}"
    results: List[Result] = []
    for rec in p.stdout.split("\x1e"):
        f = rec.split("\x1f")
        if f[0] == "CALL":
            results.append(Result())
        elif f[0] == "LOG" and len(f) == 3:
            assert results, f"调用之前就打了日志：{f}"
            results[-1].logs.append((f[1], f[2]))
        elif f[0] == "STATE" and len(f) == 4:
            results[-1].overall, results[-1].si_status, results[-1].steps_raw = f[1], f[2], f[3]
    assert len(results) == len(calls) and all(r.overall for r in results), p.stdout
    return Run(results, p.stderr, elapsed)


def fresh_legacy(tmp_path: Path, sid: str, payload=None) -> Path:
    """旧格式（无 schema_version）JSON，mtime = 现在（本轮写的）。"""
    return tos.legacy_json(tmp_path, sid, payload, mtime=time.time())


def fresh_v1(tmp_path: Path, sid: str, **kw) -> Path:
    now = time.time()
    kw.setdefault("date", TODAY)
    kw.setdefault("generated_at", datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"))
    return tos.v1_json(tmp_path, sid, mtime=now, **kw)


def tool_call(tmp_path: Path, sid: str, rc: int, json_path: Path, *, run_start: str = "0", script: str = "",
              overall: str = "success") -> Call:
    """与调用点同形的一次工具步骤调用（duration / --timeout-seconds 照调用点传）。"""
    args = ["--json", str(json_path), "--run-start", run_start]
    if sid in LIVE_TIMEOUT:
        args += ["--timeout-seconds", LIVE_TIMEOUT[sid]]
    return Call(sid, rc, LIVE_DUR[sid], script, args, overall=overall)


# ═══════════════════════════════════════════ 1. 冻结副本 ═══════════════════════════════════════════

class TestFrozenFixture:
    def test_frozen_fixture_pinned(self):
        """变异：副本改一个字节 / 从 B 之后的编排器重新生成 ⇒ 摘要不符红；锚点断言防「换成了别的旧版本」。"""
        data = PRE_B_FIXTURE.read_bytes()
        assert hashlib.sha256(data).hexdigest() == _PRE_B_ORCH_DIGEST, (
            "B 之前的编排器冻结副本被改了——它是 test_orchestrator_steps.py 的对照真相，永远不许重新生成")
        text = data.decode("utf-8")
        for pat in (r"^STEP10_RC=\$\?$", r"^READINESS_LINE=\$\(", r"^_S11_NEW=\$\(", r"^if \[ \$STEP2_RC -eq 0 \]; then$"):
            assert re.search(pat, text, re.M), pat
        assert "【Step 4/5】" in text and "_apply_step_interp" not in text

    def test_steps_golden_reads_the_frozen_copy_not_the_live_one(self):
        """变异：把 test_orchestrator_steps 的对照改回读仓库编排器 ⇒ 它在 B 之后找不到分支链；这里先说清为什么。"""
        assert tos.orchestrator_text() == PRE_B_FIXTURE.read_text(encoding="utf-8")
        assert tos.orchestrator_text() != _live_text()


# ═══════════════════════════════════════════ 2. 与 B 之前逐字相同 ═══════════════════════════════════════════

class TestGoldenParity:
    @pytest.mark.parametrize("step,rc,with_json,level,expected", tos.GOLDEN, ids=tos.GOLDEN_IDS)
    def test_golden_parity(self, tmp_path, step, rc, with_json, level, expected):
        """旧格式 JSON：helper + 真解释器写进 STEPS_RESULT 的片段（去掉 contract）与级别 = B 之前 bash 的那一支
        （`tos.GOLDEN` 本身由 test_orchestrator_steps 对照冻结副本真跑核过）。其他键原样保留、不带 interp_fallback。"""
        p = fresh_legacy(tmp_path, step) if with_json else tmp_path / "absent.json"
        r = run_helper(tmp_path, [tool_call(tmp_path, step, rc, p, script=str(tmp_path / "absent_tool.py"))]).results[0]
        f = r.frag(KEY[step])
        assert {k: v for k, v in f.items() if k != "contract"} == tos._subst(expected, p)
        assert r.level == level
        assert r.steps["sentinel"] == SENTINEL["sentinel"]
        assert "interp_fallback" not in f and r.si_status == expected["status"]
        assert not [m for _, m in r.logs if "步骤解释器不可用" in m]

    def test_one_line_per_call_carries_the_interpreter_message(self, tmp_path):
        """解释器 message 原样打一行（多段用 ` ｜ ` 连成一行），级别映射 info/warn/error → INFO/WARN/ERROR。"""
        p = fresh_legacy(tmp_path, "10")
        r = run_helper(tmp_path, [tool_call(tmp_path, "10", 1, p)]).results[0]
        out = ost.render(["--step", "10", "--rc", "1", "--date", TODAY, "--json", str(p), "--run-start", "0",
                          "--duration", "7", "--timeout-seconds", "120"])
        assert r.logs == [("WARN", out["message"])]


# ═══════════════════════════════════════════ 3. 恒不动 OVERALL_STATUS ═══════════════════════════════════════════

_VARIANTS = ("legacy", "v1_ok", "v1_alarm", "stale", "contract_error", "crash", "missing", "unparsable")


def _variant(tmp_path: Path, sid: str, variant: str) -> Path:
    if variant == "legacy":
        return fresh_legacy(tmp_path, sid)
    if variant == "v1_ok":
        return fresh_v1(tmp_path, sid, name="ok")
    if variant == "v1_alarm":
        return fresh_v1(tmp_path, sid, name="alarm",
                        attention=[sc.attention_item(f"{tos.TOOL[sid]}.x", "alarm", "要人看")])
    if variant == "stale":
        return fresh_v1(tmp_path, sid, name="stale", date="2000-01-03")
    if variant == "contract_error":
        return fresh_v1(tmp_path, sid, name="contract", tool="someone_else")
    if variant == "crash":
        p = tmp_path / f"crash-{sid}.json"
        sc.write_out(p, sc.envelope(tos.TOOL[sid], TODAY, "error", payload={"error": "X: y"},
                                    attention=[sc.attention_item(f"{tos.TOOL[sid]}.crashed", "alarm", "崩了")]))
        return p
    if variant == "missing":
        return tmp_path / f"absent-{sid}.json"
    if variant == "unparsable":
        return tos._write(tmp_path / f"bad-{sid}.json", "{", mtime=time.time())
    raise AssertionError(variant)


class TestNeverTouchesOverall:
    @pytest.mark.parametrize("rc", (0, 1, 2, 3, 124, 99))
    @pytest.mark.parametrize("sid", TOOL_SIDS)
    def test_tool_steps_never_touch_overall(self, tmp_path, sid, rc):
        """变异：helper 里加 `set_status partial`（或 failed）⇒ 起点 success / partial 那一格变了就红。"""
        calls = [tool_call(tmp_path, sid, rc, _variant(tmp_path, sid, v), overall=start)
                 for v in _VARIANTS for start in ("success", "partial")]
        for c, r in zip(calls, run_helper(tmp_path, calls).results):
            assert r.overall == c.overall, (c.args, r.logs)
            assert r.si_status == r.frag(KEY[sid])["status"]

    @pytest.mark.parametrize("sid", ("2", "4"))
    def test_helper_never_touches_overall_for_steps_2_and_4_either(self, tmp_path, sid):
        """OVERALL_STATUS 只由 Step 2 **调用点**按 STEP2_RC 定；helper 本身对 2/4 也不许动它（含兜底支）。"""
        empty = tmp_path / "empty"
        calls = [Call(sid, rc, "7" if sid == "2" else "", "", ["--run-start", "0", "--data-dir", str(empty)],
                      overall=start)
                 for rc in (0, 1, 2, 124, 99) for start in ("success", "partial")]
        runs = [run_helper(tmp_path, calls), run_helper(tmp_path, calls, project_dir=tmp_path / "no_interp")]
        for run in runs:
            for c, r in zip(calls, run.results):
                assert r.overall == c.overall, (c.sid, c.rc, r.logs)


# ═══════════════════════════════════════════ 4. 兜底 ═══════════════════════════════════════════

def _fallback(tmp_path: Path, sid: str, rc: int, dur: str) -> Tuple[str, dict]:
    script = "\n".join([
        "set -uo pipefail", _live_parts(),
        f"_step_rc_fallback {shlex.quote(sid)} {shlex.quote(str(rc))} {shlex.quote(dur)}",
        r"""printf '%s\037%s' "${_FB_LVL}" "${_FB_FRAG}" """]) + "\n"
    p = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=_bash_env(), timeout=30,
                       cwd=str(tmp_path))
    assert p.returncode == 0, p.stderr
    lvl, frag_ = p.stdout.split("\x1f")
    return lvl, json.loads(frag_)


#: 可控的替身解释器：STUB_MODE = garbage / unknown_step / extra_key / status（STUB_STATUS）/ sleep
_STUB = r'''
import json, os, sys, time
argv = sys.argv[1:]
sid = argv[argv.index("--step") + 1]
key = {KEYS}[sid]
mode = os.environ["STUB_MODE"]
if mode == "garbage":
    print("definitely not json")
elif mode == "unknown_step":
    print(json.dumps({"level": "error", "message": "x", "steps_fragment": {"unknown_step": {"status": "render_error", "rc": 1}}}))
elif mode == "extra_key":        # 本步的键对、却多带一个键：解释器糊涂了，不许只挑自己那个键并进去
    print(json.dumps({"level": "info", "message": "x", "steps_fragment": {key: {"status": "success"},
                                                                        "step99_other": {"status": "success"}}}))
elif mode == "status":
    print(json.dumps({"level": "info", "message": "stub says " + os.environ["STUB_STATUS"],
                      "steps_fragment": {key: {"status": os.environ["STUB_STATUS"]}}}))
elif mode == "sleep":
    time.sleep(60)
'''


def _stub_dir(tmp_path: Path) -> Path:
    d = tmp_path / "stub_project"
    d.mkdir(exist_ok=True)
    (d / "orchestrator_steps.py").write_text(_STUB.replace("{KEYS}", repr(KEY)), encoding="utf-8")
    return d


def _pre_b(sid: str, rc: int) -> Tuple[str, dict]:
    """B 之前 bash（冻结副本）对 Step 2 / 4 的判定：(级别, 片段)。"""
    lvl, _texts, frag_ = tos.run_bash(sid, rc)
    return lvl, frag_


def _expected_fallback(sid: str, rc: int) -> dict:
    if sid in ("2", "4"):
        return _pre_b(sid, rc)[1]
    return {"status": "interpreter_unavailable", "rc": rc}


def _fallback_calls(tmp_path: Path, rcs=(0, 1, 2, 124, 99)) -> List[Call]:
    calls = []
    for sid in ("2", "4", *TOOL_SIDS):
        for rc in rcs:
            if sid in ("2", "4"):
                calls.append(Call(sid, rc, str(tos.DUR) if sid == "2" else "", "",
                                  ["--run-start", "0", "--data-dir", str(tmp_path / "empty")]))
            else:
                calls.append(Call(sid, rc, LIVE_DUR[sid], "", ["--json", str(tmp_path / "absent.json"),
                                                                "--run-start", "0"]))
    return calls


def _assert_fell_back(c: Call, r: Result, why_needle: str) -> None:
    f = dict(r.frag(KEY[c.sid]))
    why = f.pop("interp_fallback", None)
    assert why and why_needle in why, (c.sid, c.rc, why, r.logs)
    assert f == _expected_fallback(c.sid, c.rc), (c.sid, c.rc, f)
    assert len(r.errors_with("步骤解释器不可用")) == 1, r.logs
    assert r.si_status == f["status"]
    assert r.steps["sentinel"] == SENTINEL["sentinel"]


class TestFallback:
    @pytest.mark.parametrize("rc", (0, 1, 2, 3, 124, 99))
    @pytest.mark.parametrize("sid", ("2", "4"))
    def test_fallback_parity_steps_2_4(self, tmp_path, sid, rc):
        """变异：兜底表里改 Step 2/4 任一支（例：2:2 带上 duration、4:* 丢 step2_rc）⇒ 与冻结副本不等红。
        alert_manager 读这两个键（'failed' 出 P1），兜底必须与 B 之前逐字相同。"""
        lvl, frag_ = _fallback(tmp_path, sid, rc, str(tos.DUR) if sid == "2" else "")
        assert (lvl, frag_) == _pre_b(sid, rc)

    @pytest.mark.parametrize("rc", (0, 1, 3, 124))
    @pytest.mark.parametrize("sid", TOOL_SIDS)
    def test_tool_steps_fall_back_to_interpreter_unavailable(self, tmp_path, sid, rc):
        """10–15 没有程序读者：兜底不在 bash 里再抄一份 rc→status 表，只记 interpreter_unavailable + rc。"""
        assert _fallback(tmp_path, sid, rc, "7") == ("warn", {"status": "interpreter_unavailable", "rc": rc})

    @pytest.mark.parametrize("trigger,why", [
        ("no_interpreter", "orchestrator_steps.py 不存在"),
        ("garbage", "不是合法的一行"),
        ("unknown_step", "不是合法的一行"),
        ("extra_key", "不是合法的一行"),
        ("render_error", "解释器内部出错"),
    ])
    def test_fallback_triggers(self, tmp_path, trigger, why):
        """每种触发 × 每个步骤 × 多个退出码：退回按退出码记、`interp_fallback` 说明原因、恰一行 ERROR。
        变异：删 `(.steps_fragment | keys) == [$k]` ⇒ extra_key 被当成合法输出红；删 render_error 判断 ⇒ 那格红。"""
        calls = _fallback_calls(tmp_path)
        kw: dict = {}
        if trigger == "no_interpreter":
            kw["project_dir"] = tmp_path / "no_interp"
            kw["project_dir"].mkdir()
        elif trigger in ("garbage", "unknown_step", "extra_key"):
            kw["project_dir"], kw["exports"] = _stub_dir(tmp_path), {"STUB_MODE": trigger}
        else:
            kw["date_str"] = "garbage"                    # 真解释器：--date 非法 ⇒ render_error（键是对的）
        for c, r in zip(calls, run_helper(tmp_path, calls, **kw).results):
            _assert_fell_back(c, r, why)

    @pytest.mark.parametrize("sid,rc", [("2", 0), ("12", 1)])
    def test_hung_interpreter_is_bounded(self, tmp_path, sid, rc):
        """解释器挂死 ⇒ STEP_INTERP_TIMEOUT 后看门狗收掉、退回；不拖住扫描。"""
        c = _fallback_calls(tmp_path, rcs=(rc,))[("2", "4", *TOOL_SIDS).index(sid)]
        run = run_helper(tmp_path, [c], project_dir=_stub_dir(tmp_path), exports={"STUB_MODE": "sleep"},
                         interp_timeout=2)
        assert run.elapsed < 20, run.elapsed
        _assert_fell_back(c, run.results[0], "解释器超时（>2s）")

    def test_unwritable_tmpdir_falls_to_logdir_then_says_why(self, tmp_path):
        """审查跟进：TMPDIR 坏了 ⇒ 临时文件退到 LOGDIR，解释器照常跑；两处都坏 ⇒ 兜底且原因写「建不了临时文件」，
        不冒充「输出不是合法的一行」。变异：删 LOGDIR 那一退 ⇒ 第一段红；删 else 分支的原因 ⇒ 第二段原因对不上红。"""
        ro = tmp_path / "ro_tmp"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            p = fresh_legacy(tmp_path, "12")
            ok = run_helper(tmp_path, [tool_call(tmp_path, "12", 0, p)], exports={"TMPDIR": str(ro)}).results[0]
            assert "interp_fallback" not in ok.frag(KEY["12"]) and not ok.errors_with("步骤解释器不可用"), ok.logs
            calls = _fallback_calls(tmp_path, rcs=(1,))
            run = run_helper(tmp_path, calls, exports={"TMPDIR": str(ro), "LOGDIR": str(ro)})
            for c, r in zip(calls, run.results):
                _assert_fell_back(c, r, "建不了临时文件")
        finally:
            ro.chmod(0o755)

    @pytest.mark.parametrize("sid,rc,stub_status,accepted", [
        ("2", 124, "success", False),               # 超时却说成功：B 之前不存在的判定 ⇒ 退回 timeout
        ("2", 0, "success_with_warning", False),    # 放行只限 rc=1
        ("2", 1, "success_with_warning", True),     # 唯一刻意改动：rc=1 且跑完了
        ("2", 1, "failed", True),
        ("2", 1, "skipped", False),
        ("2", 2, "skipped", True),
        ("4", 1, "skipped_builtin", True),
        ("4", 124, "skipped_builtin", False),
        ("4", 0, "failed", False),
        ("10", 1, "brand_new_status", True),        # 10–15 无程序读者，不设允许集合
    ])
    def test_allow_list_for_steps_2_and_4(self, tmp_path, sid, rc, stub_status, accepted):
        """alert_manager 只在 == 'failed' 时出 P1：解释器若回归成别的字符串，允许集合把它变成兜底而不是丢一条 P1。
        变异：删掉 helper 里 `2|4)` 那段 ⇒ 前几格（accepted=False）红。"""
        c = _fallback_calls(tmp_path, rcs=(rc,))[("2", "4", *TOOL_SIDS).index(sid)]
        r = run_helper(tmp_path, [c], project_dir=_stub_dir(tmp_path),
                       exports={"STUB_MODE": "status", "STUB_STATUS": stub_status}).results[0]
        if accepted:
            assert r.frag(KEY[sid]) == {"status": stub_status} and r.logs == [("INFO", f"stub says {stub_status}")]
        else:
            _assert_fell_back(c, r, f"「{stub_status}」不在允许集合里")


# ═══════════════════════════════════════════ 5. 合并守卫 / 退出码 2 / 不许命令替换 ═══════════════════════════════════════════

class TestMergeGuard:
    def test_broken_steps_result_is_kept_not_emptied(self, tmp_path):
        """write_status 把 STEPS_RESULT 原样嵌进 status.json：变空 = status.json 非法。
        变异：`STEPS_RESULT="${_new}"` 无条件赋值 ⇒ 变空红。"""
        p = fresh_legacy(tmp_path, "12")
        r = run_helper(tmp_path, [tool_call(tmp_path, "12", 0, p)], steps_result="{broken").results[0]
        assert r.steps_raw == "{broken"
        assert [m for lv, m in r.logs if lv == "WARN" and "jq 合并失败" in m], r.logs
        assert r.si_status == "healthy"

    def test_other_keys_survive(self, tmp_path):
        """只并本步那一个键：阶段 3 的 orchestrator_deploy 等其他键原样保留。"""
        seed = json.dumps({"orchestrator_deploy": {"status": "success", "outcome": "deployed"}, **SENTINEL})
        calls = [tool_call(tmp_path, sid, 0, fresh_legacy(tmp_path, sid)) for sid in TOOL_SIDS]
        steps = run_helper(tmp_path, calls, steps_result=seed).results[-1].steps
        assert steps["orchestrator_deploy"] == {"status": "success", "outcome": "deployed"}
        assert set(steps) == {"orchestrator_deploy", "sentinel", *(KEY[s] for s in TOOL_SIDS)}


class TestRc2ScriptPresent:
    def test_rc2_with_script_present_warns(self, tmp_path):
        """run_step 的 2 = 脚本不存在；脚本明明在还是 2 ⇒ 只可能是 argparse 用法错（日期参数不被识别？）。
        变异：删 helper 开头那段 ⇒ 红。"""
        script = REPO_ROOT / "scan_continuity.py"
        assert script.is_file()
        r = run_helper(tmp_path, [tool_call(tmp_path, "10", 2, tmp_path / "absent.json", script=str(script))]).results[0]
        assert [m for lv, m in r.logs if lv == "WARN" and "参数错误" in m and "scan_continuity.py" in m], r.logs
        assert r.frag(KEY["10"])["status"] == "skipped"

    def test_rc2_with_script_absent_does_not_warn(self, tmp_path):
        r = run_helper(tmp_path, [tool_call(tmp_path, "10", 2, tmp_path / "absent.json",
                                            script=str(tmp_path / "gone.py"))]).results[0]
        assert not [m for _, m in r.logs if "参数错误" in m], r.logs


class TestNoCommandSubstitution:
    def test_normal_call_does_not_wait_for_the_watchdog(self, tmp_path):
        """`run_step --timeout` 的看门狗 sleep 握着 stdout：放进 `$( )` 会白等满 STEP_INTERP_TIMEOUT。
        变异：`_out="$(run_step …)"` ⇒ 每次调用 ≥ 10s 红。"""
        calls = [tool_call(tmp_path, "12", 0, fresh_legacy(tmp_path, "12"))] * 2
        run = run_helper(tmp_path, calls, interp_timeout=10)
        assert run.elapsed < 8, run.elapsed
        assert all(r.frag(KEY["12"])["status"] == "healthy" for r in run.results)


# ═══════════════════════════════════════════ 6b. Step 2 / 4 调用点原文真跑（B2）═══════════════════════════════════════════

_STATUS_RANK = {"success": 0, "partial": 1, "failed": 2}
STARTS = ("success", "partial", "failed")
#: 退出码情形：(标签, STEP2_RC, 有无「主流程跑完」证据)。99 = 其余一切（B 之前的 else 支）
RC_CASES = [("rc0", 0, True), ("rc1_done", 1, True), ("rc1_not_done", 1, False), ("rc2", 2, True),
            ("rc124", 124, True), ("rc99", 99, True)]


def _site(text: str, head: str, *, through_fi: bool) -> str:
    """调用点原文：从 `head` 那一行（含 `\\` 续行）起；through_fi ⇒ 一直到其后第一个顶格 `fi`。"""
    m = re.search(rf"^{re.escape(head)}(?:[^\n]*\\\n)*[^\n]*$", text, re.M)
    assert m, f"编排器里找不到调用点 {head!r}"
    if not through_fi:
        return m.group(0)
    f = re.compile(r"^fi$", re.M).search(text, m.end())
    assert f, head
    return text[m.start():f.end()]


def _live_sites() -> Tuple[str, str]:
    text = _live_text()
    return (_site(text, "_apply_step_interp 2 step2_hive_analysis ", through_fi=True),
            _site(text, "_apply_step_interp 4 step4_dashboard ", through_fi=False))


def make_proof_day(root: Path) -> Path:
    """今天（运行时算）的 Step 2 产物，scan_timing 写于「现在」——晚于 step2_start ⇒ 算本轮跑完。"""
    return tos.make_day(root, TODAY, written_at=datetime.now().isoformat(timespec="seconds"))


@dataclass
class SiteResult:
    overall: str
    step2_status: str
    steps: dict
    logs: List[Tuple[str, str]]


def run_sites(tmp_path: Path, rc: int, *, data_dir: Path, project_dir: Path = REPO_ROOT,
              exports: Optional[Dict[str, str]] = None, sites: Optional[Tuple[str, str]] = None,
              argv_prefix: Sequence[str] = (), env: Optional[Dict[str, str]] = None) -> Dict[str, SiteResult]:
    """贴仓库编排器里 Step 2 / 4 调用点**原文**，每个起点 OVERALL_STATUS 各跑一遍（同一 bash 进程）。"""
    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    s2, s4 = sites or _live_sites()
    lines = [
        "set -uo pipefail",
        r"""log() { printf '\036LOG\037%s\037%s' "$1" "$2"; }""",
        f"PYTHON3={shlex.quote(sys.executable)}",
        f"PROJECT_DIR={shlex.quote(str(project_dir))}",
        f"DATA_DIR={shlex.quote(str(data_dir))}",
        # 故意给一个**别的**目录：调用点若改传 REPORTDIR，rc=1 跑完那格就拿不到证据（而不是 set -u 直接崩）
        f"REPORTDIR={shlex.quote(str(tmp_path / 'reports'))}",
        f"LOGDIR={shlex.quote(str(logdir))}",
        f"LOGFILE={shlex.quote(str(logdir / 'orchestrator.log'))}",
        f"DATE_STR={shlex.quote(TODAY)}",
        *(f"export {k}={shlex.quote(v)}" for k, v in (exports or {}).items()),
        _live_parts(),
        f"STEP2_RC={int(rc)}",
        f"STEP2_DURATION={tos.DUR}",
        f"STEP2_START={int(time.time()) - 120}",
        "STEP2_TIMEOUT=3600",
    ]
    for start in STARTS:
        lines += [f"STEPS_RESULT={shlex.quote(json.dumps(SENTINEL))}",
                  f"OVERALL_STATUS={start}",
                  "unset STEP2_STATUS",
                  r"printf '\036CALL\037'",
                  s2, s4,
                  r"""printf '\036STATE\037%s\037%s\037%s' "${OVERALL_STATUS}" "${STEP2_STATUS-UNSET}" "${STEPS_RESULT}" """]
    p = subprocess.run([*argv_prefix, "/bin/bash", "-c", "\n".join(lines) + "\n"], capture_output=True, text=True,
                       env=env if env is not None else _bash_env(), timeout=90, cwd=str(tmp_path))
    assert p.returncode == 0, f"bash rc={p.returncode}\nSTDERR:{p.stderr}\nSTDOUT:{p.stdout}"
    assert "unbound variable" not in p.stderr, p.stderr
    out: List[SiteResult] = []
    for rec in p.stdout.split("\x1e"):
        f = rec.split("\x1f")
        if f[0] == "CALL":
            out.append(SiteResult("", "", {}, []))
        elif f[0] == "LOG" and len(f) == 3:
            out[-1].logs.append((f[1], f[2]))
        elif f[0] == "STATE" and len(f) == 4:
            out[-1].overall, out[-1].step2_status, out[-1].steps = f[1], f[2], json.loads(f[3])
    assert len(out) == len(STARTS) and all(r.overall for r in out), p.stdout
    return dict(zip(STARTS, out))


@functools.lru_cache(maxsize=None)
def pre_b_overall(rc: int, start: str) -> str:
    """B 之前（冻结副本）Step 2 分支链 + 真 set_status / _status_rank 跑出来的 OVERALL_STATUS。"""
    frozen = tos.orchestrator_text()
    script = "\n".join([
        "set -uo pipefail", "log() { :; }",
        extract_function(frozen, "_status_rank"), extract_function(frozen, "set_status"),
        "STEPS_RESULT='{}'", f"OVERALL_STATUS={start}",
        f"STEP2_RC={int(rc)}", f"STEP2_DURATION={tos.DUR}", "STEP2_TIMEOUT=3600",
        tos.bash_block("2"),
        r"""printf '%s' "${OVERALL_STATUS}" """]) + "\n"
    p = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=_bash_env(), timeout=30)
    assert p.returncode == 0 and p.stdout in _STATUS_RANK, (p.stdout, p.stderr)
    return p.stdout


def _data_dir(tmp_path: Path, done: bool) -> Path:
    return make_proof_day(tmp_path / "data") if done else tmp_path / "empty"


class TestStep2CallSite:
    @pytest.mark.parametrize("interp", ["real", "missing"])
    @pytest.mark.parametrize("label,rc,done", RC_CASES, ids=[c[0] for c in RC_CASES])
    def test_overall_status_matrix(self, tmp_path, label, rc, done, interp):
        """OVERALL_STATUS 只看退出码、与 B 之前（冻结副本）逐格相同：rc≠0 ⇒ 至少 partial，永不下调；解释器真 / 缺都一样。
        变异：删调用点的 `set_status partial` / 改成按解释器 status 判 ⇒ 红。"""
        pd = REPO_ROOT if interp == "real" else tmp_path / "no_interp"
        res = run_sites(tmp_path, rc, data_dir=_data_dir(tmp_path, done), project_dir=pd)
        for start, r in res.items():
            want = max(start, "partial" if rc != 0 else "success", key=_STATUS_RANK.__getitem__)
            assert r.overall == want == pre_b_overall(rc, start), (label, interp, start, r.overall, r.logs)
            s2, s4 = r.steps[KEY["2"]], r.steps[KEY["4"]]
            assert r.steps["sentinel"] == SENTINEL["sentinel"]
            assert r.step2_status == s2["status"], "STEP2_STATUS 必须是 Step 2 那次调用并进片段的 status"
            pre2, pre4 = _pre_b("2", rc)[1], _pre_b("4", rc)[1]
            if interp == "missing":
                # 兜底：逐字复现 B 之前 + interp_fallback；alert_manager 的 'failed' P1 一条不丢
                assert {k: v for k, v in s2.items() if k != "interp_fallback"} == pre2, s2
                assert {k: v for k, v in s4.items() if k != "interp_fallback"} == pre4, s4
                assert s2["interp_fallback"] and s4["interp_fallback"]
                assert len([m for lv, m in r.logs if lv == "ERROR" and "步骤解释器不可用" in m]) == 2, r.logs
            elif rc == 1 and done:
                assert s2["status"] == "success_with_warning" and s2["warning"] == "ml_model_constant", s2
                assert s4 == {"status": "skipped_builtin", "step2_status": "success_with_warning"}, s4
            else:
                assert {k: v for k, v in s2.items() if k != "rc1_unverified"} == pre2, s2
                assert s4 == pre4, s4
                assert "interp_fallback" not in s2 and "interp_fallback" not in s4
                assert ("rc1_unverified" in s2) == (rc == 1)

    @pytest.mark.parametrize("interp", ["real", "missing"])
    @pytest.mark.parametrize("label,rc,done", RC_CASES, ids=[c[0] for c in RC_CASES])
    def test_step4_call_site_never_touches_overall(self, tmp_path, label, rc, done, interp):
        """Step 4 调用点单独跑：任何退出码 × 起点 × 解释器真 / 缺，OVERALL_STATUS 原样（B 之前那段也不调 set_status）。"""
        pd = REPO_ROOT if interp == "real" else tmp_path / "no_interp"
        _s2, s4 = _live_sites()
        res = run_sites(tmp_path, rc, data_dir=_data_dir(tmp_path, done), project_dir=pd, sites=(":", s4))
        for start, r in res.items():
            assert r.overall == start, (label, interp, start, r.logs)
            assert r.steps[KEY["4"]]["status"] in ("skipped_builtin", "failed")

    def test_step2_status_survives_the_step4_call(self, tmp_path):
        """Step 4 的调用会把 _SI_STATUS 改成 skipped_builtin；STEP2_STATUS 必须还是 Step 2 的 success_with_warning。
        变异：把 `STEP2_STATUS="${_SI_STATUS}"` 挪到 Step 4 调用之后 ⇒ 这里读到 skipped_builtin 红。"""
        s2, s4 = _live_sites()
        head, _, tail = s2.partition('\nSTEP2_STATUS="${_SI_STATUS}"')
        assert tail, "调用点里找不到 STEP2_STATUS 取值行"
        res = run_sites(tmp_path, 1, data_dir=_data_dir(tmp_path, True))
        assert {r.step2_status for r in res.values()} == {"success_with_warning"}
        # 反证：同样的原文、只把取值挪到 Step 4 之后，就读到 Step 4 的 status——证明上面那条不是恒真
        moved = (head + "\n" + tail.split("\n", 1)[1], s4 + '\nSTEP2_STATUS="${_SI_STATUS}"')
        res_moved = run_sites(tmp_path, 1, data_dir=_data_dir(tmp_path, True), sites=moved)
        assert {r.step2_status for r in res_moved.values()} == {"skipped_builtin"}

    @pytest.mark.parametrize("rc,stub_status,want2,want4", [
        (124, "success", "timeout", "failed"),          # 超时却说成功 ⇒ 两处都退回 B 之前的值
        (1, "brand_new", "failed", "failed"),           # 允许集合外的新字符串 ⇒ 退回 failed（alert_manager 的 P1 在）
        (0, "failed", "success", "skipped_builtin"),    # rc=0 却说 failed ⇒ 退回 success
    ])
    def test_allow_list_fallback_at_call_site(self, tmp_path, rc, stub_status, want2, want4):
        """解释器回归成 B 之前不存在的判定 ⇒ 调用点上照样退回、OVERALL_STATUS 照样只按退出码。
        变异：删 helper 里 `2|4)` 允许集合 ⇒ 片段带上替身的 status 红。"""
        res = run_sites(tmp_path, rc, data_dir=_data_dir(tmp_path, False), project_dir=_stub_dir(tmp_path),
                        exports={"STUB_MODE": "status", "STUB_STATUS": stub_status})
        for start, r in res.items():
            s2, s4 = r.steps[KEY["2"]], r.steps[KEY["4"]]
            assert (s2["status"], s4["status"]) == (want2, want4), (s2, s4)
            assert "不在允许集合里" in s2["interp_fallback"] and "不在允许集合里" in s4["interp_fallback"]
            assert r.step2_status == want2 and r.overall == pre_b_overall(rc, start)

    def test_reportdir_would_lose_the_completion_proof(self, tmp_path):
        """--data-dir 传错（REPORTDIR）⇒ rc=1 跑完的日子被判 failed。结构守卫在 TestLiveWiring，这里证明它不是空谈。"""
        s2, s4 = _live_sites()
        wrong = (s2.replace('--data-dir "${DATA_DIR}"', '--data-dir "${REPORTDIR}"'),
                 s4.replace('--data-dir "${DATA_DIR}"', '--data-dir "${REPORTDIR}"'))
        assert wrong != (s2, s4)
        res = run_sites(tmp_path, 1, data_dir=_data_dir(tmp_path, True), sites=wrong)
        assert {r.step2_status for r in res.values()} == {"failed"}


# ═══════════════════════════════════════════ 6. 调用点结构（仓库编排器原文）═══════════════════════════════════════════

def _logical_lines(text: str) -> List[Tuple[int, str]]:
    """(起始偏移, 把行尾 `\\` 续行拼起来的一逻辑行)。"""
    out, buf, start, pos = [], [], None, 0
    for raw in text.split("\n"):
        if start is None:
            start = pos
        pos += len(raw) + 1
        if raw.endswith("\\"):
            buf.append(raw[:-1])
            continue
        buf.append(raw)
        out.append((start, " ".join(s.strip() for s in buf)))
        buf, start = [], None
    return out


def _segment(text: str, sid: str) -> str:
    """`log "INFO" "【Step N】…` 到下一个 `【Step` / `【Final】` 标题之间。"""
    a = re.search(rf'^log "INFO" "【Step {sid}】', text, re.M)
    assert a, f"编排器里找不到【Step {sid}】标题"
    b = re.compile(r'^log "INFO" "【(?:Step \d+|Final)】', re.M).search(text, a.end())
    assert b
    return text[a.start():b.start()]


def _interp_calls(text: str) -> List[List[str]]:
    return [shlex.split(line) for _, line in _logical_lines(text) if line.startswith("_apply_step_interp ")]


def _opt(argv: List[str], name: str) -> Optional[str]:
    return argv[argv.index(name) + 1] if name in argv else None


class TestLiveWiring:
    def test_each_tool_step_has_exactly_one_call_with_its_key(self):
        """每个步骤（2/4 与 10–15，即 STEP_KEYS 全集）恰一个调用、键 = STEP_KEYS[sid]；没有未知步骤的调用。"""
        calls = _interp_calls(_live_text())
        pairs = [(c[1], c[2]) for c in calls]
        assert len(pairs) == len(set(pairs)), pairs
        for sid, key in pairs:
            assert KEY.get(sid) == key, (sid, key)
        for sid in KEY:
            assert [p for p in pairs if p[0] == sid] == [(sid, KEY[sid])], (sid, pairs)
        assert set(KEY) == {"2", "4", *TOOL_SIDS}

    def test_step2_and_4_calls_pass_data_dir_not_reportdir(self):
        """Step 2/4 的完成证据（日报 / .swarm_results / logs/scan_timing.json）都在 DATA_DIR。
        变异：改传 REPORTDIR ⇒ 这里红（行为面：TestStep2CallSite 里 rc=1 跑完那格变 failed 也红）。"""
        by_sid = {c[1]: c for c in _interp_calls(_live_text())}
        s2, s4 = by_sid["2"], by_sid["4"]
        assert s2[3:6] == ["${STEP2_RC}", "${STEP2_DURATION}", ""], s2
        assert s4[3:6] == ["${STEP2_RC}", "", ""], s4
        for argv in (s2, s4):
            assert _opt(argv, "--data-dir") == "${DATA_DIR}" and _opt(argv, "--run-start") == "${STEP2_START}", argv
            assert not any("REPORTDIR" in a for a in argv), argv
            assert "--report-dir" not in argv and "--json" not in argv, argv
        assert _opt(s2, "--timeout-seconds") == "${STEP2_TIMEOUT}", s2

    def test_step2_status_captured_right_after_step2_call_then_partial_on_rc(self):
        """_SI_STATUS 会被下一次调用（Step 4）覆盖 ⇒ 必须紧跟 Step 2 调用取走；OVERALL_STATUS 只按 STEP2_RC 定。
        变异：STEP2_STATUS 挪到 Step 4 调用之后 / 删 `set_status partial` / 改成按 _SI_STATUS 判 ⇒ 红。"""
        text = _live_text()
        lines = [(off, re.sub(r"\s+#.*$", "", l).strip()) for off, l in _logical_lines(text)]
        lines = [(off, l) for off, l in lines if l and not l.startswith("#")]
        i2 = [i for i, (_, l) in enumerate(lines) if l.startswith("_apply_step_interp 2 ")]
        assert len(i2) == 1
        i = i2[0]
        assert [l for _, l in lines[i + 1:i + 5]] == [
            'STEP2_STATUS="${_SI_STATUS}"',
            'if [ "${STEP2_RC}" -ne 0 ]; then',
            "set_status partial",
            "fi",
        ], lines[i + 1:i + 5]
        assert len(re.findall(r'^\s*STEP2_STATUS=', text, re.M)) == 1
        # Step 2 调用在 STEP2_DURATION 算完之后；Step 4 调用在【Step 4/5】段里、Step 5 判定之前
        assert text.index("STEP2_DURATION=$((") < lines[i][0]
        i4 = next(off for off, l in lines if l.startswith("_apply_step_interp 4 "))
        assert text.index("【Step 4/5】仪表板更新") < i4 < text.rindex("_step5_gh_pages_verdict")
        assert not re.search(r"^_step5_gh_pages_verdict$", text[:i4], re.M)

    def test_step2_4_inline_chains_are_gone(self):
        text = _live_text()
        assert not re.search(r"^if \[ \$\{?STEP2_RC\}? -eq 0 \]", text, re.M)
        assert "\"step2_hive_analysis\": {\"status\"" not in text.replace("\\\"", "\"")
        assert "\"step4_dashboard\": {\"status\"" not in text.replace("\\\"", "\"")
        assert 'elif [ "${STEP2_RC}" -eq 0 ] || [ "${STEP2_STATUS:-}" = "success_with_warning" ]; then' in \
            extract_function(text, "_step5_gh_pages_verdict")

    @pytest.mark.parametrize("sid", TOOL_SIDS)
    def test_tool_step_segment_shape(self, sid):
        """START → rm -f JSON → run_step（带日期参数、--out 同一 JSON、重定向进日志）→ _apply_step_interp（--json / --run-start 对得上）。
        变异：删 rm -f / 删日期参数 / START 挪到 run_step 之后 / --run-start 漏传 / 包进 $( ) ⇒ 各自红。"""
        seg = _segment(_live_text(), sid)
        lines = _logical_lines(seg)
        interp = [shlex.split(l) for _, l in lines if l.startswith("_apply_step_interp ")]
        assert len(interp) == 1, interp
        argv = interp[0]
        json_arg, run_start = _opt(argv, "--json"), _opt(argv, "--run-start")
        m = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", json_arg or "")
        assert m, f"Step {sid} 的 --json 应是一个加花括号的变量：{json_arg!r}"
        json_var = m.group(1)
        assert run_start == f"${{STEP{sid}_START}}", run_start
        assert argv[3] == f"${{STEP{sid}_RC}}", argv
        assert argv[5] == f"${{PROJECT_DIR}}/{ost._TOOL_STEPS[sid].tool}.py", argv
        assert bool(argv[4]) == (sid in ("10", "15")), f"duration 位置参数：10/15 传、11/12/13 不传（{argv[4]!r}）"

        def first(pred) -> int:
            hits = [off for off, l in lines if pred(l)]
            assert hits, seg
            return hits[0]

        i_start = first(lambda l: l == f"STEP{sid}_START=$(date +%s)")
        i_rm = first(lambda l: l == f'rm -f "${{{json_var}}}"')
        i_run = first(lambda l: l.startswith("run_step "))
        i_rc = first(lambda l: l == f"STEP{sid}_RC=$?")
        i_interp = first(lambda l: l.startswith("_apply_step_interp "))
        assert i_start < i_rm < i_run < i_rc < i_interp, (i_start, i_rm, i_run, i_rc, i_interp)
        run_line = next(l for off, l in lines if off == i_run)
        assert f'{ost._TOOL_STEPS[sid].date_flag} "${{DATE_STR}}"' in run_line, run_line
        assert f'--out "${{{json_var}}}"' in run_line and run_line.endswith('>> "${LOGFILE}" 2>&1'), run_line
        assert f"/{ost._TOOL_STEPS[sid].tool}.py" in run_line
        assert "$(" not in run_line and not re.search(r"\$\(\s*run_step", seg)
        assert not re.search(rf"^if \[ \$\{{?STEP{sid}_RC\}}? -eq", seg, re.M), "内联分支链应已删"

    def test_inline_summaries_are_gone(self):
        text = _live_text()
        for name in ("CONT_SUMMARY=", "READINESS_LINE=", "BOUNDARY_JSON=", "_S11_NEW=", "COV_SUMMARY=",
                     "CALWATCH_SUMMARY=", "BACKUP_CONT_SUMMARY="):
            assert not re.search(rf"^\s*{re.escape(name)}", text, re.M), name
        for a, b in (("【Step 10】", "【Step 14】"), ("【Step 15】", "【Final】")):
            seg = text[text.index(a):text.index(b)]
            assert '"$PYTHON3" -c' not in seg and '"${PYTHON3}" -c' not in seg, (a, b)

    def test_helpers_never_call_set_status_or_substitute_run_step(self):
        """变异：helper 里加 set_status / 把 run_step 放进 $( ) ⇒ 红（行为面另有 TestNeverTouchesOverall / TestNoCommandSubstitution）。"""
        text = _live_text()
        for name in ("_step_rc_fallback", "_apply_step_interp"):
            body = "\n".join(l for l in extract_function(text, name).split("\n") if not l.lstrip().startswith("#"))
            assert "set_status" not in body and "OVERALL_STATUS" not in body, name
            assert not re.search(r"\$\(\s*run_step", body), name
        assert re.search(r'^\s*run_step --timeout "\$\{STEP_INTERP_TIMEOUT\}" "\$\{PROJECT_DIR\}/orchestrator_steps\.py"',
                         extract_function(text, "_apply_step_interp"), re.M)

    def test_helper_is_defined_before_main_flow(self):
        """helper 要在第一次调用之前定义（bash 按顺序读）：紧跟 run_step，早于「主流程」横幅。"""
        text = _live_text()
        main = text.index("# 主流程")
        assert text.index("_apply_step_interp() {") < main and text.index("_step_rc_fallback() {") < main
        assert text.index("run_step() {") < text.index("_step_rc_fallback() {")


# ═══════════════════════════════════════════ 7. macOS /bin/bash 3.2 × locale ═══════════════════════════════════════════

@pytest.mark.skipif(platform.system() != "Darwin", reason="只有 macOS 的 /bin/bash 是 3.2（生产 launchd 跑的就是它）")
class TestMacBash:
    @pytest.mark.parametrize("locale_env", [
        {},                                       # env -i：C locale（launchd 的样子）
        {"LC_ALL": "en_US.UTF-8"},
    ], ids=["C", "UTF-8"])
    def test_parity_and_fallback_under_bare_env(self, tmp_path, locale_env):
        env_prefix = ["/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1",
                      f"TMPDIR={tmp_path}", *(f"{k}={v}" for k, v in locale_env.items())]
        calls = [tool_call(tmp_path, sid, 1, fresh_legacy(tmp_path, sid)) for sid in TOOL_SIDS]
        run = run_helper(tmp_path, calls, argv_prefix=env_prefix, env={})
        assert "unbound variable" not in run.stderr, run.stderr
        for sid, r in zip(TOOL_SIDS, run.results):
            f = {k: v for k, v in r.frag(KEY[sid]).items() if k != "contract"}
            p = tmp_path / f"legacy-{sid}.json"
            expected = next(g[4] for g in tos.GOLDEN if g[0] == sid and g[1] == 1)
            assert f == tos._subst(expected, p), (sid, f)
        for rc, done in ((1, True), (1, False), (124, True)):
            for interp_dir in (REPO_ROOT, tmp_path / "none"):
                res = run_sites(tmp_path, rc, data_dir=_data_dir(tmp_path, done), project_dir=interp_dir,
                                argv_prefix=env_prefix, env={})
                for start, r in res.items():
                    assert r.overall == pre_b_overall(rc, start), (rc, done, interp_dir, start)
                    if rc == 1 and done and interp_dir == REPO_ROOT:
                        assert r.step2_status == "success_with_warning", r.logs
        fb = run_helper(tmp_path, _fallback_calls(tmp_path, rcs=(1,)), project_dir=tmp_path / "none",
                        argv_prefix=env_prefix, env={})
        assert "unbound variable" not in fb.stderr, fb.stderr
        for c, r in zip(_fallback_calls(tmp_path, rcs=(1,)), fb.results):
            _assert_fell_back(c, r, "orchestrator_steps.py 不存在")
