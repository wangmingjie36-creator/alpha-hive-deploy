"""编排器 Step 10 / 12 / 13 / 15 四个工具的输出契约——**生产方**侧守卫（v0.45.355）。

`step_contract` 只定义外壳；本文件钉住的是「这四个工具**真的**按外壳写、且没把消费方今天读的键弄丢」。
每条都跑**真实 CLI 子进程**（入口 `sys.exit(step_contract.run_tool(...))` 本身就是被测对象——
进程内调 `main()` 测不到它），在沙箱里（`ALPHA_HIVE_HOME` / `--state` / `--history` / `--log-dir`
全指 tmp，cwd 也是 tmp），逐条断言：

  · `validate(env) == []`、`tool`、`date`（= 这次判定真正用的业务日）；
  · status ↔ 退出码：0 ⇒ ok、1 ⇒ attention、3 ⇒ undetermined | error；
    非 ok 路径至少有一条**有代表性的** attention（按 id 点名，不按图标 / 文案反推）；
  · 崩溃（注入坏输入）⇒ 退出码 **3** + `status: "error"` 外壳——改造前 Python 默认的 1
    在这四个工具的约定里是「降级 / 要人动手」，崩溃会被编排器记成正常；
  · 消费方读的每一个键都还在（清单见 `ORCH_READS`）。消费方原是编排器各步骤的内联 python；B（v0.45.385）起
    编排器经 `orchestrator_steps.py` 读——`_cont_summary` / `_cov_summary` / `_calwatch_summary` / `_backup_summary`。

谁会红：把任一工具入口改回 `sys.exit(main())`（崩溃类红）、`--out` 改回写裸 `res`（`validate` 类红）、
`contract_attention` 漏掉某条判据（按 id 点名的那条红）、`scan_coverage_gate` 写盘时丢掉 `date`
（`test_date_key_value_unchanged` 红）。上述每类红法 v0.45.355 都在 APFS 克隆里逐个变异实测过。

全部离线：`economic_calendar_watch` 只走节流缓存路径（从不联网），子进程另把代理指向一个
不监听的本地端口兜底，并逐条断言 `network_checked_this_run is False`。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import step_contract as sc
from tests.test_backup_continuity import _degraded_history_records, _write_jsonl
from tests.test_scan_coverage_gate import FULL, T30, _mk

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

#: 消费方读的键（最初逐条抄自 2026-09-28 02:03 版 alpha-hive-orchestrator.sh 的内联 python；B（v0.45.385）起
#: 那些内联已删，编排器经 orchestrator_steps.py 的 _cont_summary / _cov_summary / _calwatch_summary /
#: _backup_summary 读——清单照旧，读者换了）。注释按函数 / 变量名定位、不写行号。
#: 键路径是元组；`"*"` = 列表的每个元素；`"<new>"` = `new_schedule_tables` 里的每个表名。
#: ⚠️ 这里只许**加**：编排器新读了一个键就补进来；从这里删一个键之前，先证明编排器与
#: `~/.claude/scheduled-tasks/*/SKILL.md` 都不再读它（v0.45.355 核对时 SKILL 只跑人读模式，不读 JSON）。
ORCH_READS = {
    # Step 10：orchestrator_steps._cont_summary（B 之前是 rc==1 分支里算 CONT_SUMMARY 的内联 python）
    "scan_continuity": [
        ("window", "trading_days"), ("scanned_days",), ("coverage",),
        ("weeks_covered",), ("weeks_total",), ("longest_gap",), ("weeks_missed",),
    ],
    # Step 12：orchestrator_steps._cov_summary（B 之前是 rc==1 分支里算 COV_SUMMARY 的内联 python）
    "scan_coverage_gate": [
        ("fields",), ("fields", "*", "degraded"), ("fields", "*", "field"),
        ("fields", "*", "have"), ("fields", "*", "total"), ("likely_network_layer",),
    ],
    # Step 13：orchestrator_steps._calwatch_summary（B 之前是 rc==1 分支里算 CALWATCH_SUMMARY 的内联 python）
    "economic_calendar_watch": [
        ("calendar_health", "status"), ("calendar_health", "binding_table"),
        ("calendar_health", "binding_last_date"), ("calendar_health", "binding_horizon_days"),
        ("new_schedule_tables",), ("upstream", "<new>", "new_items"), ("upstream", "<new>", "source"),
        ("undeterminable_tables",), ("upstream_conclusive",),
    ],
    # Step 15：orchestrator_steps._backup_summary（B 之前是 rc==1 分支里算 BACKUP_CONT_SUMMARY 的内联 python）
    "backup_continuity": [
        ("window", "trading_days"), ("backed_up_days",), ("coverage",),
        ("longest_gap",), ("weeks_missed",),
    ],
}

_STATUS_BY_RC = {0: ("ok",), 1: ("attention",), 3: ("undetermined", "error")}

#: 缺省业务日必须是**洛杉矶**当日（`business_today()`），不是本机日历日。本机时区是 America/Vancouver：
#: 2026-11-01 之前与洛杉矶同一套偏移，「用了 date.today()」在这里与「用了洛杉矶日」取值相同、测不出来
#: （此后温哥华常年 UTC-7，冬令时每天也只有本机 00:00–01:00 那一小时不同）——所以子进程换两个时区跑：
#: 基里巴斯（UTC+14）与 UTC-12 分别比洛杉矶快 21/22 小时、慢 5/4 小时（夏 / 冬令时），两者合起来
#: 一天里任何时刻至少有一个能把「落回本机日历日」抓红。
_FOREIGN_TZ = pytest.mark.parametrize("tz", ["Pacific/Kiritimati", "Etc/GMT+12"])


# ────────────────────────────── 公共工具 ──────────────────────────────

def _run(tool: str, args, tmp_path: Path, name: str = "out.json", extra_env=None):
    """真起 CLI 子进程（沙箱 env + cwd=tmp），返回 (rc, 外壳 | None, CompletedProcess)。"""
    out = tmp_path / name
    env = dict(os.environ)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update({
        "ALPHA_HIVE_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        # 兜底：万一哪条路径真去抓页面，走一个不监听的本地端口 ⇒ 立刻连接被拒（而不是出网）
        "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "no_proxy": "", "NO_PROXY": "",
    })
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, str(REPO_ROOT / f"{tool}.py"), *map(str, args), "--out", str(out)],
                       capture_output=True, text=True, cwd=str(tmp_path), env=env, timeout=120)
    env_json = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return r.returncode, env_json, r


def _check(tool: str, rc: int, env, proc, *, expect_rc: int, expect_date: str, expect_status=None):
    """所有路径共用的契约断言。"""
    assert rc == expect_rc, f"{tool} 退出码 {rc} ≠ {expect_rc}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    assert env is not None, f"{tool} 退出码 {rc} 却没写 --out（消费方分不开「崩了」和「没跑」）\n{proc.stderr}"
    assert sc.validate(env) == [], sc.validate(env)
    assert env["tool"] == tool
    assert env["date"] == expect_date, f"外壳 date={env['date']!r}，应为这次判定用的业务日 {expect_date!r}"
    assert env["status"] in _STATUS_BY_RC[rc], f"status={env['status']!r} 与退出码 {rc} 不一致"
    if expect_status:
        assert env["status"] == expect_status
    levels = [a["level"] for a in env["attention"]]
    if env["status"] == "ok":
        assert not {"warn", "alarm"} & set(levels)
    else:
        assert {"warn", "alarm"} & set(levels), f"{env['status']} 却没有任何 warn/alarm：{env['attention']}"
    for a in env["attention"]:
        assert a["source"] == tool and a["id"].startswith(tool + "."), a
    return {a["id"]: a for a in env["attention"]}


def _dig(d, path, where=""):
    """按 ORCH_READS 的键路径取值；缺键即 AssertionError（带路径）。"""
    if not path:
        return [d]
    head, rest = path[0], path[1:]
    if head == "*":
        assert isinstance(d, list), f"{where} 应为列表"
        vals = []
        for i, x in enumerate(d):
            vals += _dig(x, rest, f"{where}[{i}]")
        return vals
    if head == "<new>":
        raise AssertionError("<new> 须由调用方展开")
    assert isinstance(d, dict) and head in d, f"编排器读的键 {where}.{head} 消失了"
    return _dig(d[head], rest, f"{where}.{head}")


def _assert_orch_keys(tool: str, env):
    for path in ORCH_READS[tool]:
        if "<new>" in path:
            # 编排器只对 new_schedule_tables 里的表名取 upstream[k]；表为空时这条路径今天不读
            i = path.index("<new>")
            for k in env.get("new_schedule_tables") or []:
                _dig(env, path[:i] + (k,) + path[i + 1:])
        else:
            _dig(env, path)


def _crash_checks(tool: str, rc, env, proc, expect_date: str):
    _check(tool, rc, env, proc, expect_rc=3, expect_date=expect_date, expect_status="error")
    assert f"{tool}.crashed" in {a["id"] for a in env["attention"]}
    assert env["error"], "错误外壳必须带异常摘要"
    assert "退出码 3" in proc.stderr


# ─────────────────────── Step 10：scan_continuity ───────────────────────

def _synth_db(path: Path, dates):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT)")
    con.executemany("INSERT INTO predictions VALUES (?,?)", [(d, "T0") for d in dates])
    con.commit()
    con.close()
    return path


class TestScanContinuity:
    TOOL = "scan_continuity"
    END = "2026-08-14"

    def _days(self, n=10):
        from scan_continuity import recent_trading_days
        return [d.isoformat() for d in recent_trading_days(n, end=date.fromisoformat(self.END))]

    def _args(self, db, tmp_path, n=10):
        return ["--db", db, "--snapshots", tmp_path / "nope", "--days", n, "--end", self.END, "--quiet"]

    def test_healthy_is_ok(self, tmp_path):
        db = _synth_db(tmp_path / "p.db", self._days())
        rc, env, p = _run(self.TOOL, self._args(db, tmp_path), tmp_path)
        _check(self.TOOL, rc, env, p, expect_rc=0, expect_date=self.END, expect_status="ok")
        assert env["healthy"] is True

    def test_degraded_lists_each_failed_criterion(self, tmp_path):
        db = _synth_db(tmp_path / "p.db", [self.END])
        rc, env, p = _run(self.TOOL, self._args(db, tmp_path), tmp_path)
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=self.END, expect_status="attention")
        for i in ("coverage_below_threshold", "longest_gap", "weeks_missed"):
            assert ids[f"{self.TOOL}.{i}"]["level"] == "warn", ids.keys()
        _assert_orch_keys(self.TOOL, env)

    def test_gap_alone_names_the_gap_not_coverage(self, tmp_path):
        """覆盖率达标（17/20）只有长空档 ⇒ 只点名 longest_gap——attention 与判据一一对应，不是一律全报。"""
        days = self._days(20)
        db = _synth_db(tmp_path / "p.db", [d for d in days if d not in days[5:8]])
        rc, env, p = _run(self.TOOL, [*self._args(db, tmp_path, 20), "--max-gap", 2], tmp_path)
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=self.END)
        assert f"{self.TOOL}.longest_gap" in ids
        assert f"{self.TOOL}.coverage_below_threshold" not in ids

    def test_missing_db_is_undetermined_and_still_writes(self, tmp_path):
        rc, env, p = _run(self.TOOL, ["--db", tmp_path / "absent.db", "--end", self.END, "--quiet"], tmp_path)
        ids = _check(self.TOOL, rc, env, p, expect_rc=3, expect_date=self.END, expect_status="undetermined")
        assert f"{self.TOOL}.db_missing" in ids

    def test_crash_is_3_with_error_envelope(self, tmp_path):
        """坏库（不是 SQLite）⇒ sqlite3.DatabaseError 未捕获。改造前 rc=1 = 「连续性降级」。"""
        bad = tmp_path / "p.db"
        bad.write_bytes(b"this is not a database" * 64)
        rc, env, p = _run(self.TOOL, self._args(bad, tmp_path), tmp_path)
        _crash_checks(self.TOOL, rc, env, p, self.END)

    @_FOREIGN_TZ
    def test_default_date_is_business_today(self, tmp_path, tz):
        before = sc.business_today()
        db = _synth_db(tmp_path / "p.db", self._days())
        rc, env, p = _run(self.TOOL, ["--db", db, "--snapshots", tmp_path / "nope", "--quiet"], tmp_path,
                          extra_env={"TZ": tz})
        assert env["date"] in {before, sc.business_today()}
        assert env["window"]["end"] <= env["date"], "窗口终点与外壳 date 必须同源"

    def test_json_stdout_is_the_same_envelope(self, tmp_path):
        db = _synth_db(tmp_path / "p.db", [self.END])
        rc, env, p = _run(self.TOOL, [*self._args(db, tmp_path)[:-1], "--json"], tmp_path)
        assert rc == 1 and json.loads(p.stdout) == env


# ─────────────────────── Step 12：scan_coverage_gate ───────────────────────

class TestScanCoverageGate:
    TOOL = "scan_coverage_gate"
    DATE = "2026-08-26"

    def _run_results(self, tmp_path, results, extra=()):
        f = tmp_path / f".swarm_results_{self.DATE}.json"
        f.write_text(json.dumps(results))
        logs = tmp_path / "logs"
        logs.mkdir(exist_ok=True)
        return _run(self.TOOL, ["--date", self.DATE, "--file", f, "--quiet", "--log-dir", logs, *extra], tmp_path)

    def test_healthy_is_ok(self, tmp_path):
        rc, env, p = self._run_results(tmp_path, _mk(T30, **FULL))
        _check(self.TOOL, rc, env, p, expect_rc=0, expect_date=self.DATE, expect_status="ok")

    def test_date_key_value_unchanged(self, tmp_path):
        """撞名处理：`date` 本是结果里的键，外壳也要它——键名与取值都得与改造前一致（= --date）。"""
        rc, env, p = self._run_results(tmp_path, _mk(T30, **FULL))
        import scan_coverage_gate as gate
        assert env["date"] == gate.check(self.DATE, tmp_path / f".swarm_results_{self.DATE}.json")["date"] == self.DATE

    def test_degraded_fields_listed(self, tmp_path):
        r = _mk(T30, **FULL)
        for t in T30[1:]:                       # 复刻 8/26：yfinance 派生字段 1/30
            r[t]["agent_details"]["OracleBeeEcho"]["details"].update({"rv_30d": None, "iv_rank": None})
        rc, env, p = self._run_results(tmp_path, r)
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=self.DATE, expect_status="attention")
        msg = ids[f"{self.TOOL}.fields_degraded"]["message"]
        assert "rv_30d 1/30" in msg and "iv_rank 1/30" in msg
        assert f"{self.TOOL}.likely_network_layer" not in ids   # 只有 yfinance 一个源挂
        _assert_orch_keys(self.TOOL, env)

    def test_multi_source_outage_flags_network_layer(self, tmp_path):
        r = _mk(T30, **FULL)
        for t in T30:
            r[t]["agent_details"]["OracleBeeEcho"]["details"].update(
                {"rv_30d": None, "iv_rank": None, "iv_current": None, "iv_skew_ratio": None})
        rc, env, p = self._run_results(tmp_path, r)
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=self.DATE)
        assert ids[f"{self.TOOL}.likely_network_layer"]["level"] == "warn"

    def test_label_contradiction_is_alarm(self, tmp_path):
        """字段覆盖率全健康、只有标签撒谎（宣称 real、值为空）⇒ 退出码 1 且是 alarm。"""
        r = _mk(T30, **FULL)
        r[T30[0]]["agent_details"]["OracleBeeEcho"]["details"].update({"data_quality": "real", "iv_current": None})
        rc, env, p = self._run_results(tmp_path, r)
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=self.DATE, expect_status="attention")
        assert ids[f"{self.TOOL}.label_contradictions"]["level"] == "alarm"
        assert f"{self.TOOL}.fields_degraded" not in ids

    def test_rate_limit_early_warning_stays_ok(self, tmp_path):
        """限流不进退出码：字段全 + 429 越闸 ⇒ 退出码 0、status ok、限流只作 info（ok 不许带 warn）。"""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / f"orchestrator-{self.DATE}.log").write_text("\n".join(
            f"14:00:0{i % 10} | WARNING | x | Too Many Requests" for i in range(150)))
        rc, env, p = self._run_results(tmp_path, _mk(T30, **FULL))
        ids = _check(self.TOOL, rc, env, p, expect_rc=0, expect_date=self.DATE, expect_status="ok")
        assert ids[f"{self.TOOL}.rate_limit"]["level"] == "info"

    def test_missing_results_is_undetermined(self, tmp_path):
        rc, env, p = _run(self.TOOL, ["--date", self.DATE, "--file", tmp_path / "nope.json", "--quiet",
                                      "--log-dir", tmp_path], tmp_path)
        ids = _check(self.TOOL, rc, env, p, expect_rc=3, expect_date=self.DATE, expect_status="undetermined")
        assert f"{self.TOOL}.undetermined" in ids
        assert env["determinable"] is False and env["reason"]

    @_FOREIGN_TZ
    def test_default_date_is_business_today(self, tmp_path, tz):
        """不传 --date ⇒ 业务日 = 洛杉矶当日（外壳 date 与 JSON 里原有的 date 同值）。
        v0.45.355 前缺省日期走一个从未存在过的 `timezone_utils`，每次静默落进 except 用本机日历日。"""
        before = sc.business_today()
        f = tmp_path / "r.json"
        f.write_text(json.dumps(_mk(T30, **FULL)))
        rc, env, p = _run(self.TOOL, ["--file", f, "--quiet", "--log-dir", tmp_path], tmp_path,
                          extra_env={"TZ": tz})
        assert rc == 0 and env["date"] in {before, sc.business_today()}

    def test_out_parent_missing_is_crash_and_creates_nothing(self, tmp_path):
        """`--out` 的父目录不存在：本工具改造前就不兜这个（`write_text` 未捕获）。现在 `write_out` 不建目录 ⇒
        照旧是未捕获异常 ⇒ 退出码 3；崩溃路径同样写不出外壳，只打 stderr——**两条路径都不建目录**。
        变红的变异：`step_contract.write_out` 恢复 mkdir（正常路径建出目录、写出 ok 外壳、rc 0）。"""
        f = tmp_path / f".swarm_results_{self.DATE}.json"
        f.write_text(json.dumps(_mk(T30, **FULL)))
        root = tmp_path / "root"
        root.mkdir()
        rc, env, r = _run(self.TOOL, ["--date", self.DATE, "--file", f, "--quiet", "--log-dir", tmp_path],
                          tmp_path, name="root/no_such_dir/cov.json")
        assert rc == 3 and env is None, r.stderr[-2000:]
        assert "FileNotFoundError" in r.stderr and "错误外壳写入" in r.stderr, r.stderr[-2000:]
        assert list(root.iterdir()) == [], "父目录不存在时建出了东西"

    def test_crash_is_3_with_error_envelope(self, tmp_path):
        """`details` 是列表 ⇒ `_dig` 里 `.get` 抛 AttributeError（未捕获）。改造前 rc=1 = 「检出降级」。
        ⚠️ 若将来 `_dig` 加固到能吃下这种输入，本条会因「没崩」而红——换一个故障注入，别删断言。"""
        r = _mk(T30, **FULL)
        r[T30[0]]["agent_details"]["OracleBeeEcho"]["details"] = [1, 2]
        rc, env, p = self._run_results(tmp_path, r)
        _crash_checks(self.TOOL, rc, env, p, self.DATE)


# ─────────────────────── Step 13：economic_calendar_watch ───────────────────────

def _health(d: date):
    from economic_calendar import get_calendar_health
    return get_calendar_health(ref_date=d)


def _day_all_ok() -> date:
    """各表都恰好不 stale 的最晚一天：min(表尾 − 该表阈值)。随表更新自动移动，不写死日期（防时间炸弹）。"""
    per = _health(date(2026, 1, 1))["per_table"]
    return min(date.fromisoformat(v["last_date"]) - timedelta(days=v["min_horizon_days"]) for v in per.values())


class TestEconomicCalendarWatch:
    TOOL = "economic_calendar_watch"

    @staticmethod
    def _cache(new=None):
        """节流缓存：四个源都「已查通、无新日程」；`new={表: [新日期...]}` 标出有新日程的表。"""
        from economic_calendar import _TABLE_SPECS
        from economic_calendar_watch import _SOURCES
        new = new or {}
        up = {}
        for k, url in _SOURCES.items():
            up[k] = {"source": url, "our_verified_through": _TABLE_SPECS[k]["verified_through"],
                     "determinable": True, "new_available": bool(new.get(k)),
                     "new_items": list(new.get(k, [])), "reason": None}
        return up

    def _run_state(self, tmp_path, today: date, state):
        st = tmp_path / "watch_state.json"
        st.write_text(json.dumps(state), encoding="utf-8")
        rc, env, p = _run(self.TOOL, ["--quiet", "--today", today.isoformat(), "--state", st], tmp_path)
        if env is not None and env.get("status") != "error":
            assert env["network_checked_this_run"] is False, "本文件必须离线：只许走节流缓存"
        return rc, env, p

    def _throttled(self, today, upstream):
        return {"last_checked_at": f"{today.isoformat()}T06:00:00", "upstream": upstream, "action_required": False}

    def test_healthy_is_ok(self, tmp_path):
        today = _day_all_ok()
        assert _health(today)["status"] == "ok"
        rc, env, p = self._run_state(tmp_path, today, self._throttled(today, self._cache()))
        _check(self.TOOL, rc, env, p, expect_rc=0, expect_date=today.isoformat(), expect_status="ok")

    def test_new_schedule_warn_with_deadline(self, tmp_path):
        today = _day_all_ok()
        rc, env, p = self._run_state(tmp_path, today, self._throttled(
            today, self._cache({"cpi": ["2027-01-13", "2027-02-10"]})))
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=today.isoformat(), expect_status="attention")
        a = ids[f"{self.TOOL}.new_schedule.cpi"]
        assert a["level"] == "warn" and env["new_schedule_tables"] == ["cpi"]
        assert a["deadline"] == _health(today)["per_table"]["cpi"]["last_date"], "截止日 = 本地 cpi 表覆盖到的最后一天"
        _assert_orch_keys(self.TOOL, env)

    def test_below_horizon_warn_with_deadline(self, tmp_path):
        """某表越过自己的地平线阈值一天 ⇒ 该表一条 warn，deadline = 它覆盖到的最后一天。"""
        per = _health(date(2026, 1, 1))["per_table"]
        key, v = min(per.items(), key=lambda kv: date.fromisoformat(kv[1]["last_date"])
                     - timedelta(days=kv[1]["min_horizon_days"]))
        today = date.fromisoformat(v["last_date"]) - timedelta(days=v["min_horizon_days"] - 1)
        assert key in _health(today)["stale_tables"]
        rc, env, p = self._run_state(tmp_path, today, self._throttled(today, self._cache()))
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=today.isoformat(), expect_status="attention")
        a = ids[f"{self.TOOL}.horizon.{key}"]
        assert a["level"] == "warn" and a["deadline"] == v["last_date"]
        assert env["new_schedule_tables"] == []
        _assert_orch_keys(self.TOOL, env)

    def test_exhausted_is_alarm(self, tmp_path):
        per = _health(date(2026, 1, 1))["per_table"]
        today = max(date.fromisoformat(v["last_date"]) for v in per.values()) + timedelta(days=1)
        rc, env, p = self._run_state(tmp_path, today, self._throttled(today, self._cache()))
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=today.isoformat())
        for k, v in per.items():
            assert ids[f"{self.TOOL}.horizon.{k}"]["level"] == "alarm"
            assert ids[f"{self.TOOL}.horizon.{k}"]["deadline"] == v["last_date"]

    def test_unchecked_sources_are_undetermined(self, tmp_path):
        """节流窗口内且无缓存 ⇒ 四源「无法判定」⇒ 退出码 3，绝不报健康。"""
        today = _day_all_ok()
        rc, env, p = self._run_state(tmp_path, today, {"last_checked_at": f"{today.isoformat()}T06:00:00"})
        ids = _check(self.TOOL, rc, env, p, expect_rc=3, expect_date=today.isoformat(), expect_status="undetermined")
        assert f"{self.TOOL}.upstream_undetermined" in ids

    @_FOREIGN_TZ
    def test_default_date_is_business_today(self, tmp_path, tz):
        """不传 --today ⇒ 业务日 = 洛杉矶当日，且节流 / 本地体检按同一天算（缓存标今天 ⇒ 节流命中、不联网）。"""
        today = date.fromisoformat(sc.business_today())
        st = tmp_path / "watch_state.json"
        st.write_text(json.dumps(self._throttled(today, self._cache())), encoding="utf-8")
        rc, env, p = _run(self.TOOL, ["--quiet", "--state", st], tmp_path, extra_env={"TZ": tz})
        assert env["date"] in {today.isoformat(), sc.business_today()}
        assert env["checked_at"] == env["date"], "本地体检用的日子必须就是外壳 date"
        assert env["network_checked_this_run"] is False

    def test_out_parent_still_created_on_normal_path(self, tmp_path):
        """本工具改造前（v0.45.67 起）就替 `--out` 建父目录；`write_out` 不再建目录后，这一行为留在工具自己的
        `main()` 里，照旧成立。变红的变异：删掉 `economic_calendar_watch.main` 里的 `mkdir`（写入失败只打 stderr）。"""
        today = _day_all_ok()
        st = tmp_path / "watch_state.json"
        st.write_text(json.dumps(self._throttled(today, self._cache())), encoding="utf-8")
        rc, env, p = _run(self.TOOL, ["--quiet", "--today", today.isoformat(), "--state", st], tmp_path,
                          name="new_dir/deeper/out.json")
        _check(self.TOOL, rc, env, p, expect_rc=0, expect_date=today.isoformat(), expect_status="ok")

    def test_crash_is_3_with_error_envelope(self, tmp_path):
        """坏状态文件（`upstream` 是列表）⇒ `dict(list)` 抛 TypeError（未捕获）。改造前 rc=1 = 「要人动手」。"""
        today = _day_all_ok()
        rc, env, p = self._run_state(tmp_path, today, {"last_checked_at": f"{today.isoformat()}T06:00:00",
                                                      "upstream": [1, 2]})
        _crash_checks(self.TOOL, rc, env, p, today.isoformat())


# ─────────────────────── Step 15：backup_continuity ───────────────────────

class TestBackupContinuity:
    TOOL = "backup_continuity"
    END = "2026-08-14"

    def _args(self, hist):
        return ["--history", hist, "--days", 10, "--end", self.END, "--quiet"]

    def test_healthy_is_ok(self, tmp_path):
        from scan_continuity import recent_trading_days
        h = tmp_path / "h.jsonl"
        _write_jsonl(h, [{"date": d.isoformat(), "stage": "done", "ok": True}
                         for d in recent_trading_days(10, end=date.fromisoformat(self.END))])
        rc, env, p = _run(self.TOOL, self._args(h), tmp_path)
        _check(self.TOOL, rc, env, p, expect_rc=0, expect_date=self.END, expect_status="ok")

    def test_degraded_lists_criteria_and_stage(self, tmp_path):
        h = tmp_path / "h.jsonl"
        _write_jsonl(h, _degraded_history_records())
        rc, env, p = _run(self.TOOL, self._args(h), tmp_path)
        ids = _check(self.TOOL, rc, env, p, expect_rc=1, expect_date=self.END, expect_status="attention")
        assert ids[f"{self.TOOL}.coverage_below_threshold"]["level"] == "warn"
        assert "push" in ids[f"{self.TOOL}.longest_gap"]["message"], "最长空档要带出卡住的阶段"
        _assert_orch_keys(self.TOOL, env)

    def test_missing_history_is_undetermined_and_still_writes(self, tmp_path):
        rc, env, p = _run(self.TOOL, ["--history", tmp_path / "absent.jsonl", "--end", self.END, "--quiet"],
                          tmp_path)
        ids = _check(self.TOOL, rc, env, p, expect_rc=3, expect_date=self.END, expect_status="undetermined")
        assert f"{self.TOOL}.history_missing" in ids

    def test_crash_is_3_with_error_envelope(self, tmp_path):
        """历史日志里有非 UTF-8 字节 ⇒ 逐行读时 UnicodeDecodeError（未捕获）。改造前 rc=1 = 「连续性降级」。"""
        h = tmp_path / "h.jsonl"
        h.write_bytes(b'{"date": "2026-08-14", "ok": true}\n\xff\xfe\xfa garbage\n')
        rc, env, p = _run(self.TOOL, self._args(h), tmp_path)
        _crash_checks(self.TOOL, rc, env, p, self.END)

    @_FOREIGN_TZ
    def test_default_date_is_business_today(self, tmp_path, tz):
        before = sc.business_today()
        h = tmp_path / "h.jsonl"
        _write_jsonl(h, [{"date": before, "stage": "done", "ok": True}])
        rc, env, p = _run(self.TOOL, ["--history", h, "--quiet"], tmp_path, extra_env={"TZ": tz})
        assert env["date"] in {before, sc.business_today()}
        assert env["window"]["end"] is None or env["window"]["end"] <= env["date"]

    def test_json_stdout_is_the_same_envelope(self, tmp_path):
        h = tmp_path / "h.jsonl"
        _write_jsonl(h, _degraded_history_records())
        rc, env, p = _run(self.TOOL, [*self._args(h)[:-1], "--json"], tmp_path)
        assert rc == 1 and json.loads(p.stdout) == env
