"""编排器 Step 11 `ic_rerun_readiness` 的输出契约——**生产方**侧守卫（v0.45.355）。

`step_contract` 只定义外壳；本文件钉住「`ic_rerun_readiness` 真的按外壳写、`attention` 从结构化数据列、
没把消费方今天读的键弄丢」。四类断言：

  · **真实 CLI 子进程**（`TestRealCli`）：入口 `sys.exit(step_contract.run_tool(...))` 本身就是被测对象——
    进程内调 `main()` 测不到它。沙箱 `ALPHA_HIVE_HOME` / `--db` / `--out` / cwd 全在 tmp，代理指向不监听的
    本地端口兜底（本工具全程不该出网）。逐条断言 `validate(env) == []`、`date`、status ↔ 退出码，以及
    按 **id** 点名的条目：已就绪、世代边界报警、维度 IC 协议 H1 锚点截止日（段首仍是 ⏳ 时）、崩溃、找不到库；
  · **编排器今天读的键**（`ORCH_READS`，从仓库外 `/Users/igg/.claude/scripts/alpha-hive-orchestrator.sh`
    Step 11 的两段内联 python 逐条抄出）一个不少；payload 顶层键只增不减（`PAYLOAD_KEYS_V0_45_354`）；
    三个前瞻检验子字典仍是 `{status, line}`，私有的 `_detail` 不外泄；
  · **进程内**：`status=ok` 的路径（沙箱里共振 / F&G 两个执行器恒 cannot_judge，子进程到不了 ok）；
  · **条目 ↔ 图标对照**（`TestAttentionMatchesRenderedIcons`）：生产侧**不**从图标反推条目，
    但两者描述同一个状态，测试侧对照它们没有各说各话——⚠️ 段必有 warn、🔔 段必有检视点条目、
    ⏳ 段除「H1 锚点待登记」（正是按图标读漏掉的那条）外没有条目。

周度任务 `~/.claude/scheduled-tasks/alpha-hive-weekly-optimizer/SKILL.md` 只跑 `--quiet`（不读 JSON），
它读的是那一行的段——段结构的守卫在 `tests/test_dim_ic_forward_test.py::_check_quiet_segments`。

谁会红（v0.45.355 在 APFS 克隆里逐个变异实测过）：入口改回 `sys.exit(main())`（崩溃类红）；
`--out` 写回裸 `res`（validate 类红）；`build_attention` 漏掉就绪 / 边界 / 锚点截止日任何一条（点名那条红）；
边界条目的 id 不带版本（同日多条报警同 id ⇒ validate 类红 + 按 id 保留首条会丢掉锚点截止日那条）；
锚点条目丢了 `deadline`；`_take_detail` 从 `pop` 改成 `get`（`_detail` 外泄，payload 形状类红）；
外壳 `date` 不跟 `--today`；找不到库的路径不写 `--out`；删掉编排器读的任何一个键。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import ic_rerun_readiness as rr
import step_contract as sc
from tests.test_ic_rerun_readiness import _COHORT_START, _weekly_rows, _write_two_marker_archive

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
TOOL = "ic_rerun_readiness"
SCRIPT = REPO_ROOT / f"{TOOL}.py"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load("dim_ic_protocol", "experiments/dim_ic_protocol.py")

#: 编排器 Step 11 读的键（最初逐条抄自 2026-09-28 版编排器的 `READINESS_LINE=` 与 `BOUNDARY_JSON=` 两段内联 python；
#: B（v0.45.385）起那两段已删，编排器经 orchestrator_steps.py 的 `_readiness_line` / `_BOUNDARY_KEEP` 读——清单照旧）。
#: READINESS_LINE 段读 `cohort.date/version`、`weeks_accrued`、`weeks_required`、`n_ripe_samples`、
#: `eta_date`、`pool_note`；BOUNDARY_JSON 段读 `cohort_boundary_evidence` 的 keep 元组。
#: 那边一律 `d.get(...)`，缺键不崩、只会印 '?' —— 正因为不崩，缺了没人会红，所以钉在这里。
#: `cohort_boundary_evidence.error` 只在判别器抛异常时才有（编排器也是 `if k in b` 才取），不钉。
#: ⚠️ 只许加：从这里删一个键之前，先证明编排器与 `~/.claude/scheduled-tasks/*/SKILL.md` 都不再读它。
ORCH_READS = [
    ("cohort", "date"), ("cohort", "version"),
    ("weeks_accrued",), ("weeks_required",), ("n_ripe_samples",), ("eta_date",), ("pool_note",),
    ("cohort_boundary_evidence", "version"), ("cohort_boundary_evidence", "boundary"),
    ("cohort_boundary_evidence", "verdict"), ("cohort_boundary_evidence", "marker_first_seen"),
    ("cohort_boundary_evidence", "unmarked_after_boundary"), ("cohort_boundary_evidence", "alarm"),
    ("cohort_boundary_evidence", "line"),
]

#: v0.45.355 改造前（HEAD = v0.45.354）`--json` / `--out` 的全部顶层键（生产库只读实跑取得）。
#: 契约是「只增不减、不改名」：外壳六键是**加**在顶层的，原有键一个不许丢。
PAYLOAD_KEYS_V0_45_354 = frozenset({
    "calendar_weeks_elapsed", "cohort", "cohort_boundary_evidence", "dim_ic_forward_test",
    "eta_calendar_weeks", "eta_date", "fg_exposure_gate_forward_test", "n_all_samples", "n_ripe_samples",
    "next_step", "pool_drift", "pool_note", "ready", "resonance_forward_test", "scan_weeks_in_cohort",
    "target_ic", "weeks_accrued", "weeks_per_calendar_week", "weeks_remaining", "weeks_required",
})
_FORWARD_KEYS = ("resonance_forward_test", "fg_exposure_gate_forward_test", "dim_ic_forward_test")

#: 本工具的退出码约定（与外壳 status **分开**）：0 = 已就绪（要人去跑 IC 分析 ⇒ 必是 attention），
#: 1 = 未就绪（正常；有没有别的事要人看由 attention 说）、3 = 无法判定 / 崩溃。
_STATUS_BY_RC = {0: ("attention",), 1: ("ok", "attention"), 3: ("undetermined", "error")}

_SCHEMA = ("CREATE TABLE predictions (date TEXT, ticker TEXT, dimension_scores TEXT, "
           "price_at_predict REAL, checked_t7 INTEGER, close_t7 REAL, price_t7 REAL, "
           "return_t7 REAL, exit_price REAL, exit_reason TEXT)")


def _make_db(home: Path, rows) -> Path:
    """`predictions` 用维度 IC 执行器要的全列（缺列它会 cannot_judge——⚠️ 段，锚点那条就不是「藏在 ⏳ 里」了）。
    `rows` 形状同 `tests/test_ic_rerun_readiness.py::_weekly_rows`：`(date, ticker, ripe)`。"""
    home.mkdir(parents=True, exist_ok=True)
    db = home / "pheromone.db"
    con = sqlite3.connect(db)
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(d, t, None, 100.0, 1 if ripe else 0, 110.0 if ripe else None, None, None, None, None)
                     for d, t, ripe in rows])
    con.commit()
    con.close()
    return db


def _env(home: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "ALPHA_HIVE_HOME": str(home),
        "ALPHA_HIVE_DB_PATH": str(home / "pheromone.db"),
        "ALPHA_HIVE_LOGS_DIR": str(home / "logs"),
        "ALPHA_HIVE_CACHE_DIR": str(home / "cache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        # 兜底：万一哪条路径真去抓网络，走一个不监听的本地端口 ⇒ 立刻连接被拒（而不是出网）
        "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "no_proxy": "", "NO_PROXY": "",
    })
    return env


def _run(tmp_path: Path, db: Path, *args, prelude: str = ""):
    """真起 CLI 子进程，返回 (rc, 外壳 | None, stdout, stderr)。

    `prelude` 为空 ⇒ 直接 `python ic_rerun_readiness.py …`；非空 ⇒ `python -c`：先执行 prelude（只准改被测
    CLI 看得到的**外部数据**视图，例如执行器读的边界表），再以 `runpy.run_path(..., run_name="__main__")`
    跑**同一个真实脚本文件**——入口那一行照样是被测对象。
    """
    out = tmp_path / "out.json"
    argv = ["--db", str(db), *map(str, args), "--out", str(out)]
    if prelude:
        code = (f"import sys, runpy\nsys.path.insert(0, {str(REPO_ROOT)!r})\n{prelude}\n"
                f"sys.argv = [{str(SCRIPT)!r}] + {argv!r}\n"
                f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n")
        cmd = [sys.executable, "-c", code]
    else:
        cmd = [sys.executable, str(SCRIPT), *argv]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path), env=_env(db.parent), timeout=120)
    env_json = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return r.returncode, env_json, r.stdout, r.stderr


def _check(rc, env, stderr, *, expect_rc, expect_date, expect_status=None):
    """所有子进程路径共用的契约断言；返回 attention 按 id 分组（id 在同一外壳内唯一——`validate` 查，
    这里再显式钉一次：每组恰好一条）。"""
    assert rc == expect_rc, f"退出码 {rc} ≠ {expect_rc}\nstderr={stderr[-2000:]}"
    assert env is not None, f"退出码 {rc} 却没写 --out（消费方分不开「崩了」和「没跑」）\n{stderr[-2000:]}"
    assert sc.validate(env) == [], sc.validate(env)
    assert env["tool"] == TOOL
    assert env["date"] == expect_date, f"外壳 date={env['date']!r}，应为这次判定用的业务日 {expect_date!r}"
    assert env["status"] in _STATUS_BY_RC[rc], f"status={env['status']!r} 与退出码 {rc} 不一致"
    if expect_status:
        assert env["status"] == expect_status, (env["status"], env["attention"])
    ids: dict = {}
    for a in env["attention"]:
        ids.setdefault(a["id"], []).append(a)
    dup = {k: len(v) for k, v in ids.items() if len(v) > 1}
    assert not dup, f"attention id 重复（消费方按 id 去重会丢条目）：{dup}"
    assert ("ic_rerun.ready" in ids) == (rc == 0), "「已就绪」条目必须与退出码 0 同进同出"
    return ids


def _keep_first_by_id(items):
    """模拟最朴素的消费方：按 id 去重、保留首条（「id 是去重 / 路由的键」的字面用法）。"""
    seen = {}
    for a in items:
        seen.setdefault(a["id"], a)
    return list(seen.values())


def _dig(d, path):
    for i, k in enumerate(path):
        assert isinstance(d, dict) and k in d, f"编排器读的键 {'.'.join(path[:i + 1])} 消失了"
        d = d[k]
    return d


def _assert_payload_intact(env):
    for path in ORCH_READS:
        _dig(env, path)
    missing = PAYLOAD_KEYS_V0_45_354 - set(env)
    assert not missing, f"v0.45.354 已有的顶层键被删 / 改名：{sorted(missing)}"
    for k in _FORWARD_KEYS:
        assert set(env[k]) == {"status", "line"}, f"{k} 的形状变了（私有 _detail 外泄？）：{sorted(env[k])}"
    def _keys(x):
        if isinstance(x, dict):
            for k, v in x.items():
                yield k
                yield from _keys(v)
        elif isinstance(x, list):
            for v in x:
                yield from _keys(v)
    assert rr._DETAIL_KEY not in set(_keys(env)), "私有 _detail 泄进了 --out"


def _quiet_segments(stdout: str):
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"--quiet 必须只打一行：{lines}"
    return lines[0].split("｜")


# ═══════════════════════════════ 真实 CLI 子进程 ═══════════════════════════════

class TestRealCli:
    TODAY = "2026-10-01"          # 早于维度 IC 窗口起点（锚点「待登记」只在窗口开始前出现）

    def test_ready_is_exit_0_with_ready_item(self, tmp_path):
        """攒够不重叠周 ⇒ 退出码 0 + status attention + info 级 `ic_rerun.ready`，消息里带该跑的命令。"""
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        db = _make_db(tmp_path / "home", _weekly_rows(need))
        today = "2027-06-01"
        rc, env, _out, err = _run(tmp_path, db, "--today", today, "--quiet")
        ids = _check(rc, env, err, expect_rc=0, expect_date=today, expect_status="attention")
        (ready,) = ids["ic_rerun.ready"]
        assert ready["level"] == "info" and ready["source"] == TOOL
        assert env["next_step"] in ready["message"], "已就绪条目必须带该跑的命令（next_step）"
        assert env["ready"] is True
        _assert_payload_intact(env)

    def test_not_ready_is_exit_1_without_ready_item(self, tmp_path):
        db = _make_db(tmp_path / "home", _weekly_rows(2))
        rc, env, out, err = _run(tmp_path, db, "--today", self.TODAY, "--quiet")
        _check(rc, env, err, expect_rc=1, expect_date=self.TODAY)
        assert env["ready"] is False
        _assert_payload_intact(env)
        assert _quiet_segments(out)[0] == rr.summary_line(env), "--quiet 第一段仍是 IC 摘要（格式不因外壳变）"

    def test_boundary_alarm_one_item_per_alarming_version(self, tmp_path):
        """边界日当天已有归档、却一份新印记都没有（推送晚了）⇒ 每条报警的边界一条 alarm，
        id = `ic_rerun.boundary_evidence.<版本>`（同一外壳内唯一）；恰是维度 IC 协议 H1 锚点版本的那条
        带截止日 = 窗口起点（协议 §13.4），其余不带。

        D3：v0.45.355 初版每条都叫 `ic_rerun.boundary_evidence`，而 v0.45.340（锚点、带 2026-10-12）排在
        v0.45.334 之后 ⇒ 按 id 保留首条的消费方恰好丢掉带截止日的那条。变红的变异：id 去掉版本后缀。"""
        home = tmp_path / "home"
        db = _make_db(home, [])
        _write_two_marker_archive(home, _COHORT_START, gex_new=False, buzz_new=False, oracle_new=False)
        rc, env, out, err = _run(tmp_path, db, "--today", self.TODAY, "--quiet")
        ids = _check(rc, env, err, expect_rc=1, expect_date=self.TODAY, expect_status="attention")
        alarmed = [e for e in env["cohort_boundary_evidence"]["per_version"] if e["alarm"]]
        assert len(alarmed) >= 2 and P.H1_ANCHOR_VERSION in [e["version"] for e in alarmed][1:], (
            "前提：夹具造出了至少两条报警、且锚点那条不是第一条（否则测不到「同 id 丢后一条」）",
            [e["version"] for e in alarmed])
        items = [a for a in env["attention"] if a["id"].startswith("ic_rerun.boundary_evidence")]
        assert [a["id"] for a in items] == [f"ic_rerun.boundary_evidence.{e['version']}" for e in alarmed]
        for e, a in zip(alarmed, items):          # 条目按 per_version 表序
            assert a["level"] == "alarm" and e["version"] in a["message"], (e["version"], a)
            is_anchor = e["version"] == P.H1_ANCHOR_VERSION
            assert (a["deadline"] == P.FORWARD_START) is is_anchor, (e["version"], a["deadline"])
            assert (a["deadline"] is None) is (not is_anchor), (e["version"], a["deadline"])
        kept = _keep_first_by_id(env["attention"])
        assert any(a["deadline"] == P.FORWARD_START and a["id"].startswith("ic_rerun.boundary_evidence")
                   for a in kept), "按 id 去重后锚点更正的截止日丢了"
        segs = _quiet_segments(out)
        assert segs[-1].startswith("🚨"), "边界报警时 --quiet 仍追加 🚨 末段（格式不因外壳变）"
        _assert_payload_intact(env)

    def test_h1_anchor_deadline_reported_while_segment_is_hourglass(self, tmp_path):
        """维度 IC 协议 H1 锚点「待登记」：`--quiet` 里它只是 ⏳ 段尾的一句「须早于 2026-10-12」，
        按段首图标读的消费方永远报不出来；外壳里必须有带 `deadline` 的 warn 条目。

        造「待登记」：执行器通过 `signal_archive._cohort_history()` 读边界表——prelude 只让**这个视图**
        里少掉锚点那条（`anchor_status` ⇒ `pending`），被测脚本本身原样跑。
        段首图标与条目的对照写成双条件（不是 skip）：维度 IC 除锚点外没有别的 warn ⇒ 段首必须是 ⏳；
        有（例如浅克隆 / 无 git 的环境里「权重历史无法判定」）⇒ 段首必须是 ⚠️。两支都是真断言。
        """
        db = _make_db(tmp_path / "home", [])
        prelude = (
            "import importlib.util, signal_archive, ic_rerun_readiness as _rr\n"
            f"_s = importlib.util.spec_from_file_location('dim_ic_protocol', "
            f"{str(REPO_ROOT / 'experiments' / 'dim_ic_protocol.py')!r})\n"
            "_P = importlib.util.module_from_spec(_s); _s.loader.exec_module(_P)\n"
            "_hist = [e for e in _rr._COHORT_HISTORY if e[1] != _P.H1_ANCHOR_VERSION]\n"
            "assert len(_hist) < len(_rr._COHORT_HISTORY), '前提：锚点版本确实在边界表里'\n"
            "signal_archive._cohort_history = lambda: _hist\n")
        rc, env, out, err = _run(tmp_path, db, "--today", self.TODAY, "--quiet", prelude=prelude)
        ids = _check(rc, env, err, expect_rc=1, expect_date=self.TODAY, expect_status="attention")
        (anchor,) = ids["ic_rerun.dim_ic.h1_anchor_pending"]
        assert anchor["level"] == "warn"
        assert anchor["deadline"] == P.FORWARD_START == "2026-10-12", anchor
        seg = _quiet_segments(out)[3]
        assert seg.split(" ", 1)[1].startswith("维度 IC 协议") and f"须早于 {P.FORWARD_START}" in seg, seg
        other_dim_warns = [i for i, v in ids.items() if i.startswith("ic_rerun.dim_ic.")
                           and i != "ic_rerun.dim_ic.h1_anchor_pending" and v[0]["level"] == "warn"]
        if other_dim_warns:
            assert seg.startswith("⚠️ "), (other_dim_warns, seg)
        else:
            assert seg.startswith("⏳ "), f"维度 IC 没有别的 warn 时段首应是 ⏳（截止日正是藏在这里）：{seg}"
        _assert_payload_intact(env)

    def test_crash_is_exit_3_with_error_envelope(self, tmp_path):
        """坏库（不是 SQLite）⇒ `assess()` 抛 `sqlite3.DatabaseError`。改造前：rc=1（= 未就绪·正常）、
        不写 `--out`、stdout 为空 ⇒ 编排器把崩溃记成「正常攒样本」。"""
        home = tmp_path / "home"
        home.mkdir()
        bad = home / "pheromone.db"
        bad.write_bytes(b"this is not a database" * 64)
        rc, env, out, err = _run(tmp_path, bad, "--today", self.TODAY, "--quiet")
        ids = _check(rc, env, err, expect_rc=3, expect_date=self.TODAY, expect_status="error")
        assert ids[f"{TOOL}.crashed"][0]["level"] == "alarm"
        assert "DatabaseError" in env["error"] and "退出码 3" in err
        assert out.strip() == "", "崩溃时 --quiet 不该印出一行看起来正常的摘要"

    def test_missing_db_is_undetermined_and_still_writes(self, tmp_path):
        """找不到库 ⇒ 退出码 3 不变，但 `--out` 照样写出 status=undetermined 的外壳——此前这条路不写文件，
        编排器读到的是「文件不存在」（或同一 DATE_STR 下更早一次留下的旧文件）。"""
        home = tmp_path / "home"
        home.mkdir()
        rc, env, out, err = _run(tmp_path, home / "absent.db", "--today", self.TODAY, "--quiet")
        ids = _check(rc, env, err, expect_rc=3, expect_date=self.TODAY, expect_status="undetermined")
        assert ids["ic_rerun.undetermined"][0]["level"] == "warn"
        assert out.strip() == "", "找不到库时 --quiet 的 stdout 与改造前一样为空"

    def test_out_parent_missing_keeps_verdict_and_creates_nothing(self, tmp_path):
        """`--out` 的父目录不存在 ⇒ 与 v0.45.354 同语义：打 stderr、退出码照判定（这里 1），**不建目录**。
        v0.45.355 初版 `write_out` 会 `mkdir(parents=True)`，于是本工具悄悄开始替调用方建目录。
        变红的变异：`step_contract.write_out` 恢复 mkdir。"""
        db = _make_db(tmp_path / "home", _weekly_rows(2))
        root = tmp_path / "root"
        root.mkdir()
        r = subprocess.run([sys.executable, str(SCRIPT), "--db", str(db), "--today", self.TODAY, "--quiet",
                            "--out", str(root / "no_such_dir" / "out.json")],
                           capture_output=True, text=True, cwd=str(tmp_path), env=_env(db.parent), timeout=120)
        assert r.returncode == 1, r.stderr[-2000:]
        assert "无法写入" in r.stderr, r.stderr[-2000:]
        assert list(root.iterdir()) == [], "父目录不存在时建出了东西"

    def test_default_date_is_business_today(self, tmp_path):
        """不给 `--today` ⇒ 外壳 `date` = `business_today()`（洛杉矶当日）。⚠️ 它**不**保证等于编排器的
        DATE_STR（本机时区；2026-11-01 起冬令时每天有一小时差一天）——消费方要比新鲜度就显式传 `--today`。"""
        before = sc.business_today()
        db = _make_db(tmp_path / "home", [])
        rc, env, _out, err = _run(tmp_path, db, "--quiet")
        assert rc == 1, err[-2000:]
        assert sc.validate(env) == [] and env["date"] in {before, sc.business_today()}

    def test_json_stdout_is_the_same_envelope(self, tmp_path):
        db = _make_db(tmp_path / "home", _weekly_rows(2))
        rc, env, out, err = _run(tmp_path, db, "--today", self.TODAY, "--json")
        assert rc == 1, err[-2000:]
        printed = json.loads(out)
        assert printed == env and sc.validate(printed) == []


# ═══════════════════════════════ 进程内：status=ok ═══════════════════════════════

class TestOkPath:
    """沙箱子进程里共振 / F&G 两个执行器恒 cannot_judge（没有归档 / 快照），到不了 ok——ok 路径在进程内验：
    共振 / F&G 换成只回 `{status, line}` 的 ⏳ 桩（同 `test_*_forward_test.py::TestCarriedByReadiness` 的桩形状），
    维度 IC 的桩额外带 `_detail`（锚点 ok）——不带的后果见下一条。"""

    def test_nothing_to_look_at_is_ok_and_exit_1(self, monkeypatch, tmp_path):
        for fn, name in (("resonance_forward_status", "共振加成前瞻检验"),
                         ("fg_exposure_gate_forward_status", "F&G 敞口门前瞻检验")):
            monkeypatch.setattr(rr, fn, lambda *a, _n=name, **k: {"status": "not_ready", "line": f"⏳ {_n}：桩"})
        monkeypatch.setattr(rr, "dim_ic_forward_status", lambda *a, **k: {
            "status": "not_ready", "line": "⏳ 维度 IC 协议：桩", rr._DETAIL_KEY: dict(_DIM_BASE)})
        db = _make_db(tmp_path / "home", _weekly_rows(2))
        out = tmp_path / "o.json"
        monkeypatch.setattr(sys, "argv", [TOOL, "--db", str(db), "--today", "2026-10-01", "--quiet", "--out", str(out)])
        assert rr.main() == 1
        env = json.loads(out.read_text(encoding="utf-8"))
        assert sc.validate(env) == [] and env["status"] == "ok" and env["attention"] == [], env["attention"]
        _assert_payload_intact(env)

    def test_anchor_deadline_survives_executor_cannot_judge(self, monkeypatch, tmp_path):
        """执行器 cannot_judge（这里：库缺维度列）时早退、不算锚点；锚点与库无关，`dim_ic_forward_status`
        补算它 ⇒ 截止日不被一个无关的故障挡住。变红的变异：删掉补算那段。"""
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT)")
        con.commit()
        con.close()
        monkeypatch.setattr(rr, "_COHORT_HISTORY",
                            [e for e in rr._COHORT_HISTORY if e[1] != P.H1_ANCHOR_VERSION])
        st = rr.dim_ic_forward_status(db, today="2026-10-01")
        assert st["status"] == "cannot_judge" and st["line"].startswith("⚠️ "), st["line"]
        items = rr._dim_ic_attention(rr._take_detail(st))
        assert set(st) == {"status", "line"}, "取走 _detail 后只剩公开形状"
        got = {a["id"]: a for a in items}
        assert set(got) == {"ic_rerun.dim_ic.cannot_judge", "ic_rerun.dim_ic.h1_anchor_pending"}, sorted(got)
        assert got["ic_rerun.dim_ic.h1_anchor_pending"]["deadline"] == P.FORWARD_START

    def test_degraded_ohlc_window_reaches_quiet_line_detail_and_out(self, monkeypatch, tmp_path, capsys):
        """复审 S1 端到端（进程内）：F&G 执行器报「回放行情窗口降级」⇒ 经真的 `fg_exposure_gate_forward_status`
        （代码锚点指向一个只换掉 `run()` 的垫片，`status_line` 是真的）到达 `--quiet` 那一行、私有 `_detail`
        与 `--out` 的 attention；`--out` 的公开形状不变（`{status, line}`）。此前三处与健康时逐字节相同。"""
        canned = {**_NR, "selfproof_rate": 1.0, "mode": "forward", "n_dates": 10, "ohlc_window": _OW_DEGRADED}
        code = tmp_path / "code"
        (code / "experiments").mkdir(parents=True)
        (code / "experiments" / "canned.json").write_text(json.dumps(canned), encoding="utf-8")
        real = REPO_ROOT / "experiments" / "fg_exposure_gate_forward_test.py"
        (code / "experiments" / "fg_exposure_gate_forward_test.py").write_text(
            "import importlib.util, json, pathlib\n"
            f"_spec = importlib.util.spec_from_file_location('_real_fg_for_shim', {str(real)!r})\n"
            "_real = importlib.util.module_from_spec(_spec)\n_spec.loader.exec_module(_real)\n"
            "status_line = _real.status_line\n"
            "def run(today=None):\n"
            "    return json.loads((pathlib.Path(__file__).parent / 'canned.json').read_text(encoding='utf-8'))\n",
            encoding="utf-8")
        monkeypatch.setattr(rr, "ALPHAHIVE_DIR", code)
        st = rr.fg_exposure_gate_forward_status(today="2026-10-01")
        assert st["line"] == _FG.status_line(canned) and st["line"].startswith("⚠️ F&G 敞口门前瞻检验："), st["line"]
        assert st[rr._DETAIL_KEY]["ohlc_window"] == _OW_DEGRADED

        monkeypatch.setattr(rr, "resonance_forward_status",
                            lambda *a, **k: {"status": "not_ready", "line": "⏳ 共振加成前瞻检验：桩"})
        monkeypatch.setattr(rr, "dim_ic_forward_status", lambda *a, **k: {
            "status": "not_ready", "line": "⏳ 维度 IC 协议：桩", rr._DETAIL_KEY: dict(_DIM_BASE)})
        db = _make_db(tmp_path / "home", _weekly_rows(2))
        out = tmp_path / "o.json"
        monkeypatch.setattr(sys, "argv", [TOOL, "--db", str(db), "--today", "2026-10-01", "--quiet", "--out", str(out)])
        assert rr.main() == 1
        segs = _quiet_segments(capsys.readouterr().out)
        assert segs[2] == _FG.status_line(canned) and "回放行情窗口降级" in segs[2], segs
        env = json.loads(out.read_text(encoding="utf-8"))
        assert sc.validate(env) == [] and env["status"] == "attention"
        assert [a["id"] for a in env["attention"]] == ["ic_rerun.fg_exposure_gate_forward.ohlc_window_degraded"]
        assert env["fg_exposure_gate_forward_test"]["line"] == segs[2]
        _assert_payload_intact(env)

    def test_missing_anchor_state_is_not_silently_ok(self):
        """执行器若不再给出 `h1_anchor`（形状变了 / 桩没带 `_detail`），锚点截止日会无声消失 ⇒ 单列一条 warn，
        不让「读不到」渲染成「没事」。"""
        items = rr._dim_ic_attention({"status": "not_ready", "forward_start": P.FORWARD_START})
        assert [a["id"] for a in items] == ["ic_rerun.dim_ic.h1_anchor_unknown"]
        assert items[0]["level"] == "warn" and items[0]["deadline"] == P.FORWARD_START


# ═══════════════════════════════ 条目 ↔ 图标对照 ═══════════════════════════════

_RES = _load("resonance_boost_forward_test_for_contract", "experiments/resonance_boost_forward_test.py")
_FG = _load("fg_exposure_gate_forward_test_for_contract", "experiments/fg_exposure_gate_forward_test.py")
_DIM = _load("dim_ic_forward_test_for_contract", "experiments/dim_ic_forward_test.py")

_NR = {"status": "not_ready", "weeks": 3, "next_look_at": 10, "looks_passed_without_verdict": []}
_FWD_STATES = [
    ("cannot_judge", {"status": "cannot_judge", "reason": "自证 8/10（< 95%）"}, {"cannot_judge"}),
    ("not_ready", dict(_NR), set()),
    ("stale", {**_NR, "stale": True, "reason": "登记后 30 天仍无前瞻样本"}, {"stale"}),
    ("confirmed", {"status": "confirmed", "look": "中期"}, {"checkpoint"}),
    ("not_confirmed", {"status": "not_confirmed", "look": "终期"}, {"checkpoint"}),
    ("unknown", {"status": "weird"}, {"unknown_status"}),
]

_ANC_OK = {"state": "ok", "version": P.H1_ANCHOR_VERSION, "date": "2026-09-28"}
_DIM_BASE = {"status": "not_ready", "forward_start": P.FORWARD_START, "today": "2026-10-01", "h1_weeks": 0,
             "next_look_at": 26, "looks_done": [], "verdicts": {}, "truncation": {"H1": None, "H2": None},
             "weight_change": {"date": None}, "stale": False, "h1_anchor": _ANC_OK}
_DIM_STATES = [
    ("clean", {}, set()),
    ("anchor_pending", {"h1_anchor": {"state": "pending", "version": P.H1_ANCHOR_VERSION, "date": None,
                                      "problem": f"{P.H1_ANCHOR_VERSION} 不在 _COHORT_HISTORY 里"}},
     {"h1_anchor_pending"}),
    ("anchor_fallback", {"h1_anchor": {"state": "fallback", "version": P.H1_ANCHOR_VERSION, "date": None,
                                       "problem": "x 不在 _COHORT_HISTORY 里"}}, {"h1_anchor_fallback"}),
    ("h1_truncated", {"truncation": {"H1": {"date": "2026-11-02", "version": "vX", "layer": "input"}, "H2": None}},
     {"h1_truncated"}),
    ("h2_truncated", {"truncation": {"H1": None, "H2": {"date": "2026-11-02", "version": "vX"}}}, {"h2_truncated"}),
    ("weight_changed", {"weight_change": {"date": "2026-10-20", "source": "提交 abc"}}, {"weight_changed"}),
    ("weight_unknown", {"weight_change": {"unknown": "浅克隆"}}, {"weight_history_unknown"}),
    ("stale", {"stale": True}, {"stale"}),
    ("in_progress", {"status": "in_progress", "looks_done": ["中检"], "h1_weeks": 30, "next_look_at": 52},
     {"checkpoint"}),
    ("in_progress_warn", {"status": "in_progress", "looks_done": ["中检"], "h1_weeks": 30, "next_look_at": 52,
                          "weight_change": {"unknown": "浅克隆"}}, {"checkpoint", "weight_history_unknown"}),
    ("concluded", {"status": "concluded", "verdicts": {"H1": {"result": "not_significant"},
                                                       "H2": {"result": "not_tested"}}}, {"checkpoint"}),
    ("cannot_judge", {"status": "cannot_judge", "reason": "找不到库"}, {"cannot_judge"}),
    ("cannot_judge_anchor_pending", {"status": "cannot_judge", "reason": "predictions 缺列",
                                     "h1_anchor": {"state": "pending", "version": P.H1_ANCHOR_VERSION,
                                                   "date": None, "problem": "未登记"}},
     {"cannot_judge", "h1_anchor_pending"}),
]


def _assert_icon_consistent(line, items, *, silent_ids=()):
    """⚠️ ⇒ 有 warn；🔔 ⇒ 有检视点；⏳ ⇒ 除 `silent_ids`（按图标读会漏掉、正是外壳要补上的）外没有条目。"""
    icon = line.split(" ", 1)[0]
    warns = [a for a in items if a["level"] == "warn" and a["id"] not in silent_ids]
    ckpt = [a for a in items if a["id"].endswith(".checkpoint")]
    if icon == "⚠️":
        assert warns, f"段首 ⚠️ 却没有 warn 条目：{line}"
    elif icon == "🔔":
        assert ckpt, f"段首 🔔 却没有检视点条目：{line}"
    elif icon == "⏳":
        assert not warns and not ckpt, f"段首 ⏳ 却有条目 {[a['id'] for a in warns + ckpt]}：{line}"
    else:
        raise AssertionError(f"未知段首图标：{line}")


#: v0.45.391 复审 S1：F&G 结果里的回放行情窗口计数（`paper_portfolio._ReplayOhlcWindow.stats()` 的形状）
_OW_HEALTHY = {"window": ["2026-09-15", "2026-10-02"], "wide_fetches": 20, "served": 300, "out_of_window": 0,
               "fallback": 0, "fallback_tickers": {}, "direct_requests": 0, "direct_empty": 0, "degraded": False}
_OW_DEGRADED = dict(_OW_HEALTHY, served=0, fallback=20, direct_requests=144,
                    fallback_tickers={f"T{i:02d}": "ConnectionError: wide rejected" for i in range(20)}, degraded=True)
#: v0.45.410：回放行情库计数（`replay_ohlc_store.ReplayOhlcStore.stats()` 的形状）
_STORE_OK = {"fetch_day": "2026-10-02", "store_only": 0, "tail_fetches": 15, "full_fetches": 0, "settled_days_added": 15,
             "served_from_store": 200, "served_on_fallback": 0, "revisions_new": 0, "revised_bars": 0,
             "revised_tickers": [], "revised_recent": 0, "revised_recent_tickers": [], "revision_alarm_days": 7,
             "quarantined": [], "invalid_files": [], "write_errors": [], "problem": False}
_FG_WINDOW_STATES = [
    ("not_ready_healthy", {**_NR, "ohlc_window": _OW_HEALTHY}, set()),
    ("not_ready_degraded", {**_NR, "ohlc_window": _OW_DEGRADED}, {"ohlc_window_degraded"}),
    ("stale_degraded", {**_NR, "stale": True, "reason": "x", "ohlc_window": _OW_DEGRADED},
     {"stale", "ohlc_window_degraded"}),
    ("cannot_judge_degraded", {"status": "cannot_judge", "reason": "r", "ohlc_window": _OW_DEGRADED},
     {"cannot_judge", "ohlc_window_degraded"}),
    ("confirmed_degraded", {"status": "confirmed", "look": "中期", "ohlc_window": _OW_DEGRADED},
     {"checkpoint", "ohlc_window_degraded"}),
    ("out_of_window_only", {**_NR, "ohlc_window": dict(_OW_HEALTHY, out_of_window=3, direct_requests=3,
                                                       degraded=True)}, {"ohlc_window_degraded"}),
    # v0.45.410（回放行情库）：只因库的问题降级 / 近几天确认的修订 ⇒ warn；过了告警期的修订 ⇒ 只陈述
    ("store_problem_only", {**_NR, "ohlc_window": dict(_OW_HEALTHY, degraded=True, store=dict(
        _STORE_OK, quarantined=["AAA.json.invalid-2026-10-01-0"], problem=True))}, {"ohlc_window_degraded"}),
    ("revised_recent", {**_NR, "ohlc_window": dict(_OW_HEALTHY, store=dict(
        _STORE_OK, revised_bars=6, revised_tickers=["NVDA"], revised_recent=6, revised_recent_tickers=["NVDA"]))},
     {"ohlc_store_revised"}),
    ("revised_long_ago", {**_NR, "ohlc_window": dict(_OW_HEALTHY, store=dict(
        _STORE_OK, revised_bars=6, revised_tickers=["NVDA"]))}, set()),
    # v0.45.415 二次检查：不知道重放日（应恒为 0）⇒ 窗口降级
    ("as_of_unknown_only", {**_NR, "ohlc_window": dict(_OW_HEALTHY, degraded=True, store=dict(
        _STORE_OK, as_of_unknown=3))}, {"ohlc_window_degraded"}),
]


def test_as_of_unknown_only_says_what_happened_not_a_download_failure():
    """只因「不知道重放日」降级时，attention 说它（时点数据失效），不说「整段取数 0/20 失败、退回直连」（那不是事实）。
    变异「parts 条件不看 as_of_unknown」/「不加那一段」⇒ 红。"""
    fres = next(
        s[1] for s in _FG_WINDOW_STATES if s[0] == "as_of_unknown_only")
    (a,) = rr._forward_test_attention("fg_exposure_gate_forward", rr._detail(fres, rr._FWD_DETAIL_KEYS))
    assert "不知道重放日" in a["message"] and "整段取数" not in a["message"], a["message"]


class TestAttentionMatchesRenderedIcons:
    """生产侧**不**从图标反推条目（两者各自从结构化字段来）；这里对照两者没有各说各话。"""

    @pytest.mark.parametrize("label,fres,expect", _FG_WINDOW_STATES, ids=[s[0] for s in _FG_WINDOW_STATES])
    def test_fg_ohlc_window_states(self, label, fres, expect):
        """复审 S1：窗口降级 ⇒ 独立 id 的 warn 条目，且 F&G 那一段的段首与之对得上（⏳ 段不许藏 warn）。
        变异「`_FWD_DETAIL_KEYS` 去掉 ohlc_window」/「attention 不看 degraded」/「status_line 降级时仍打 ⏳」⇒ 红。"""
        items = rr._forward_test_attention("fg_exposure_gate_forward", rr._detail(fres, rr._FWD_DETAIL_KEYS))
        assert {a["id"] for a in items} == {f"ic_rerun.fg_exposure_gate_forward.{s}" for s in expect}, label
        _assert_icon_consistent(_FG.status_line(fres), items)
        degraded = [a for a in items if a["id"].endswith(".ohlc_window_degraded")]
        assert all(a["level"] == "warn" for a in degraded)
        if fres["ohlc_window"]["fallback"] > 5:
            assert "等 20 个" in degraded[0]["message"], degraded[0]["message"]   # 名单封顶，不把 20 个全列进一行

    @pytest.mark.parametrize("key,mod", [("resonance_forward", _RES), ("fg_exposure_gate_forward", _FG)])
    @pytest.mark.parametrize("label,fres,expect", _FWD_STATES, ids=[s[0] for s in _FWD_STATES])
    def test_forward_tests(self, key, mod, label, fres, expect):
        items = rr._forward_test_attention(key, rr._detail(fres, rr._FWD_DETAIL_KEYS))
        assert {a["id"] for a in items} == {f"ic_rerun.{key}.{s}" for s in expect}, label
        _assert_icon_consistent(mod.status_line(fres), items)

    @pytest.mark.parametrize("label,patch,expect", _DIM_STATES, ids=[s[0] for s in _DIM_STATES])
    def test_dim_ic(self, label, patch, expect):
        fres = {**_DIM_BASE, **patch}
        items = rr._dim_ic_attention(rr._detail(fres, rr._DIM_DETAIL_KEYS))
        assert {a["id"] for a in items} == {f"ic_rerun.dim_ic.{s}" for s in expect}, label
        _assert_icon_consistent(_DIM.status_line(fres), items, silent_ids={"ic_rerun.dim_ic.h1_anchor_pending"})

    def test_anchor_pending_is_hourglass_with_deadline(self):
        """这一条就是 v0.45.355 要补的洞：进度行 ⏳（按图标 = 正常），截止日只在段尾文字里。"""
        fres = {**_DIM_BASE, "h1_anchor": {"state": "pending", "version": P.H1_ANCHOR_VERSION, "date": None,
                                           "problem": "未登记"}}
        line = _DIM.status_line(fres)
        assert line.startswith("⏳ ") and f"须早于 {P.FORWARD_START}" in line, line
        (a,) = rr._dim_ic_attention(rr._detail(fres, rr._DIM_DETAIL_KEYS))
        assert (a["id"], a["level"], a["deadline"]) == ("ic_rerun.dim_ic.h1_anchor_pending", "warn", P.FORWARD_START)

    def test_detail_whitelist_excludes_verdicts(self):
        """盲化：检视点之后执行器才有结论与效应量；本工具只报「到点了、去跑脚本」，不转述，也不带走。"""
        assert "verdicts" not in rr._DIM_DETAIL_KEYS and "descriptive" not in rr._DIM_DETAIL_KEYS

    @pytest.mark.parametrize("res,expect", [
        ({"ready": True, "pool_note": None, "weeks_accrued": 25, "weeks_required": 25, "n_ripe_samples": 50,
          "next_step": "run-it"}, {"ic_rerun.ready"}),
        ({"ready": False, "pool_note": "当前池 30 只里有 9 只（30%）…", "weeks_accrued": 3, "weeks_required": 25,
          "n_ripe_samples": 6, "eta_date": None}, {"ic_rerun.pool_note"}),
        ({"ready": False, "pool_note": None, "weeks_accrued": 3, "weeks_required": 25, "n_ripe_samples": 6,
          "eta_date": "2027-03-01", "eta_calendar_weeks": 22.0}, set()),
    ])
    def test_readiness_summary(self, res, expect):
        items = rr.build_attention(res, {}, {"status": "not_ready"}, {"status": "not_ready"}, dict(_DIM_BASE))
        assert {a["id"] for a in items} == expect
        icon = rr.summary_line(res).split(" ", 1)[0]
        assert (icon == "✅") == ("ic_rerun.ready" in expect) and (icon == "⚠️") == ("ic_rerun.pool_note" in expect)

    def test_boundary_alarm_matches_fifth_segment(self):
        """顶层 `alarm`（= `--quiet` 追加 🚨 末段的条件）⇔ 至少一条 alarm 条目；逐条与 per_version 一一对应。"""
        ok = {"version": "vA", "boundary": "2099-01-05", "verdict": "matches", "alarm": False, "line": "✅"}
        bad = {"version": P.H1_ANCHOR_VERSION, "boundary": "2099-01-05", "verdict": "boundary_too_early",
               "alarm": True, "line": "🚨 世代边界 …"}
        dim = {**_DIM_BASE}
        assert rr._boundary_attention({**ok, "per_version": [ok]}, dim) == []
        items = rr._boundary_attention({**bad, "per_version": [ok, bad]}, dim)
        assert [(a["id"], a["level"], a["deadline"]) for a in items] == [
            (f"ic_rerun.boundary_evidence.{P.H1_ANCHOR_VERSION}", "alarm", P.FORWARD_START)]
        # 顶层说报警、per_version 却没有报警条目（形状不一致）⇒ 照顶层报一条，不让它消失
        items = rr._boundary_attention({**bad, "per_version": [ok]}, dim)
        assert [(a["id"], a["level"]) for a in items] == [("ic_rerun.boundary_evidence.inconsistent", "alarm")]

    def test_two_alarms_same_day_have_distinct_ids(self):
        """D3（进程内，不依赖夹具造出哪几条）：同日两条报警、锚点排第二 ⇒ 两个 id 不同、外壳过 validate，
        按 id 保留首条的消费方仍拿得到锚点截止日。变红的变异：id 去掉版本后缀。"""
        first = {"version": "v0.45.334", "boundary": "2026-09-28", "verdict": "boundary_too_early",
                 "alarm": True, "line": "🚨 v0.45.334 …"}
        anchor = {"version": P.H1_ANCHOR_VERSION, "boundary": "2026-09-28", "verdict": "boundary_too_early",
                  "alarm": True, "line": f"🚨 {P.H1_ANCHOR_VERSION} …"}
        items = rr._boundary_attention({**first, "alarm": True, "per_version": [first, anchor]}, dict(_DIM_BASE))
        assert [a["id"] for a in items] == ["ic_rerun.boundary_evidence.v0.45.334",
                                            f"ic_rerun.boundary_evidence.{P.H1_ANCHOR_VERSION}"]
        env = sc.envelope(TOOL, "2026-10-01", "attention", attention=items)
        assert sc.validate(env) == []
        assert [a["deadline"] for a in _keep_first_by_id(env["attention"])] == [None, P.FORWARD_START]
