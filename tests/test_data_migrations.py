"""阶段 7（v0.45.419）：数据迁移运行器 + 三个回填工具缺省 dry-run 先备份 + 编排器接入段。

每条守卫都配「喂坏的看它红」：运行器的行为用合成 versions 目录与合成库；工具用真子进程 / 真 main()。
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_migrations import runner as R  # noqa: E402
from data_migrations.history import HISTORICAL  # noqa: E402

OK_MIG = textwrap.dedent('''
    DESCRIPTION = "{desc}"
    def build_fixture(conn):
        conn.execute("create table t(k text primary key, v int)")
    def apply(ctx):
        import sqlite3
        c = sqlite3.connect(str(ctx.db_path))
        n = c.execute("insert or ignore into t values ('{key}', 1)").rowcount
        c.commit(); c.close()
        open(str(ctx.home / "ran_{key}"), "a").write("x")
        return {{"rows_affected": n}}
''')


def _mk(tmp_path, specs=(("0001_a", "a"),)):
    vd = tmp_path / "versions"
    vd.mkdir(exist_ok=True)
    for name, key in specs:
        (vd / f"{name}.py").write_text(OK_MIG.format(desc=f"mig {key}", key=key))
    db = tmp_path / "pheromone.db"
    if not db.exists():
        c = sqlite3.connect(db)
        c.execute("create table t(k text primary key, v int)")
        c.commit()
        c.close()
    return vd, db


def _run(tmp_path, vd, db, **kw):
    return R.run_pending(db_path=db, versions_dir=vd, ledger_dir=tmp_path / "ledger",
                         backup_dir=tmp_path / "bk", **kw)


def _ledger(tmp_path):
    return R.read_ledger(tmp_path / "ledger")


def _rows(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("select k from t order by k").fetchall()
    finally:
        c.close()


class TestRunner:
    def test_empty_versions_is_ok_seeds_history_once_and_takes_no_backup(self, tmp_path):
        vd, db = _mk(tmp_path, specs=())
        r1 = _run(tmp_path, vd, db)
        assert r1["status"] == "ok" and r1["n_pending"] == 0 and r1["backup"] is None
        hist = [x for x in _ledger(tmp_path) if x["kind"] == "historical"]
        assert len(hist) == len(HISTORICAL) == len({h["id"] for h in HISTORICAL})
        _run(tmp_path, vd, db)
        assert len(_ledger(tmp_path)) == len(HISTORICAL), "补记历史必须按 id 幂等"
        assert not (tmp_path / "bk").exists()

    def test_applies_once_records_and_backs_up_before_writing(self, tmp_path):
        vd, db = _mk(tmp_path, (("0001_a", "a"), ("0002_b", "b")))
        r = _run(tmp_path, vd, db)
        assert r["status"] == "ok" and r["applied"] == ["0001", "0002"]
        assert _rows(db) == [("a",), ("b",)]
        bk = Path(r["backup"])
        assert bk.exists() and _rows(bk) == [], "备份必须是迁移**之前**的库"
        mig = [x for x in _ledger(tmp_path) if x["kind"] == "migration"]
        assert [x["id"] for x in mig] == ["0001", "0002"] and all(x["status"] == "applied" for x in mig)
        assert mig[0]["sha256"] == hashlib.sha256((vd / "0001_a.py").read_bytes()).hexdigest()
        r2 = _run(tmp_path, vd, db)
        assert r2["applied"] == [] and r2["n_pending"] == 0 and r2["backup"] is None
        assert (tmp_path / "ran_a").read_text() == "x", "已应用的迁移不许重跑"

    def test_failure_stops_records_and_retries_next_time(self, tmp_path):
        vd, db = _mk(tmp_path, (("0001_a", "a"), ("0002_b", "b"), ("0003_c", "c")))
        (vd / "0002_b.py").write_text('DESCRIPTION="boom"\ndef apply(ctx):\n    raise RuntimeError("坏了")\n')
        r = _run(tmp_path, vd, db)
        assert r["status"] == "failed" and r["failed"] == "0002" and r["applied"] == ["0001"]
        assert _rows(db) == [("a",)], "失败之后的迁移不许跑"
        last = R.applied_state(_ledger(tmp_path))
        assert last["0001"]["status"] == "applied" and last["0002"]["status"] == "failed" and "0003" not in last
        (vd / "0002_b.py").write_text(OK_MIG.format(desc="fixed", key="b"))   # 未应用过的可以改
        r2 = _run(tmp_path, vd, db)
        assert r2["status"] == "ok" and r2["applied"] == ["0002", "0003"]

    def test_apply_must_return_rows_affected_int(self, tmp_path):
        vd, db = _mk(tmp_path, specs=())
        (vd / "0001_x.py").write_text('DESCRIPTION="x"\ndef apply(ctx):\n    return None\n')
        r = _run(tmp_path, vd, db)
        assert r["status"] == "failed" and "rows_affected" in r["error"]

    def test_editing_an_applied_migration_is_tampering_and_runs_nothing(self, tmp_path):
        vd, db = _mk(tmp_path)
        assert _run(tmp_path, vd, db)["status"] == "ok"
        (vd / "0001_a.py").write_text((vd / "0001_a.py").read_text() + "\n# 偷偷改一行\n")
        (vd / "0002_b.py").write_text(OK_MIG.format(desc="new", key="b"))
        r = _run(tmp_path, vd, db)
        assert r["status"] == "tampered" and "0001" in r["error"]
        assert _rows(db) == [("a",)], "篡改时连新的待办也不许跑"

    def test_deleting_an_applied_migration_is_tampering(self, tmp_path):
        vd, db = _mk(tmp_path)
        _run(tmp_path, vd, db)
        (vd / "0001_a.py").unlink()
        assert _run(tmp_path, vd, db)["status"] == "tampered"

    @pytest.mark.parametrize("names", [
        ("0001_a", "0003_c"),            # 断号
        ("0001_a", "0001_b"),            # 重号
        ("0001_a", "2_bad"),             # 名字不合规
        ("0002_a",),                     # 不是从 0001 起
        ("0001_Upper",),                 # 大写
    ])
    def test_layout_errors_run_nothing(self, tmp_path, names):
        vd, db = _mk(tmp_path, specs=tuple((n, n[-1]) for n in names))
        r = _run(tmp_path, vd, db)
        assert r["status"] == "layout_error", r
        assert _rows(db) == [] and not (tmp_path / "ledger").exists()

    def test_dry_run_touches_nothing(self, tmp_path):
        vd, db = _mk(tmp_path)
        before = R.db_fingerprint(db)
        r = _run(tmp_path, vd, db, apply=False)
        assert r["status"] == "ok" and r["pending_ids"] == ["0001"] and r["dry_run"] is True
        assert R.db_fingerprint(db) == before
        assert not (tmp_path / "ledger").exists() and not (tmp_path / "bk").exists()

    def test_no_backup_no_migration(self, tmp_path):
        vd, db = _mk(tmp_path)
        db.unlink()
        r = _run(tmp_path, vd, db)
        assert r["status"] == "backup_failed" and not (tmp_path / "ran_a").exists()
        assert not any(x["kind"] == "migration" for x in _ledger(tmp_path))

    def test_ledger_write_failure_after_apply_is_red_not_silent(self, tmp_path, monkeypatch):
        vd, db = _mk(tmp_path)
        real = R.append_record

        def flaky(d, rec):
            if rec.get("kind") == "migration":
                raise OSError("磁盘满")
            return real(d, rec)
        monkeypatch.setattr(R, "append_record", flaky)
        r = _run(tmp_path, vd, db)
        assert r["status"] == "ledger_error" and "已执行但运行记录写不进去" in r["error"]
        assert (tmp_path / "ran_a").exists(), "夹具前提：迁移确实跑了"

    def test_corrupt_ledger_is_red(self, tmp_path):
        vd, db = _mk(tmp_path)
        (tmp_path / "ledger").mkdir()
        (tmp_path / "ledger" / R.LEDGER_NAME).write_text('{"id": "0001", "status": "applied"}\n不是json\n')
        r = _run(tmp_path, vd, db)
        assert r["status"] == "ledger_error" and not (tmp_path / "ran_a").exists()


class TestIdempotenceCheckHasTeeth:
    def test_idempotent_passes(self, tmp_path):
        vd, _ = _mk(tmp_path)
        assert R.check_idempotent(R.discover(vd)[0], tmp_path)["ok"] is True

    def test_non_idempotent_is_caught(self, tmp_path):
        vd, _ = _mk(tmp_path, specs=())
        (vd / "0001_bad.py").write_text(textwrap.dedent('''
            DESCRIPTION = "每跑一遍多一行"
            def build_fixture(conn):
                conn.execute("create table t(id integer primary key autoincrement, v int)")
            def apply(ctx):
                import sqlite3
                c = sqlite3.connect(str(ctx.db_path)); c.execute("insert into t(v) values (1)"); c.commit(); c.close()
                return {"rows_affected": 1}
        '''))
        res = R.check_idempotent(R.discover(vd)[0], tmp_path)
        assert res["ok"] is False and "第二遍" in res["reason"]

    def test_missing_fixture_is_not_ok(self, tmp_path):
        vd, _ = _mk(tmp_path, specs=())
        (vd / "0001_nofix.py").write_text('DESCRIPTION="x"\ndef apply(ctx):\n    return {"rows_affected": 0}\n')
        assert R.check_idempotent(R.discover(vd)[0], tmp_path)["ok"] is False

    def test_every_real_migration_is_well_formed_and_idempotent(self, tmp_path):
        for m in R.discover():           # 真实 versions/；现在为空时靠上面三条证明判据有牙
            res = R.check_idempotent(m, tmp_path)
            assert res["ok"], f"{m.path.name}: {res['reason']}"


class TestHistoryRecords:
    def test_history_is_honest_about_what_it_is(self):
        from data_migrations.history import HISTORY_SOURCE
        assert "未逐条回溯核对" in HISTORY_SOURCE
        ids = [h["id"] for h in HISTORICAL]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        for h in HISTORICAL:
            assert h["evidence"] and h["what"] and h["date"].startswith("2026-")


# ── 三个回填工具：缺省 dry-run、真写先备份、写完留记录 ───────────────────────
def _pred_db(path):
    c = sqlite3.connect(path)
    c.execute("""create table predictions(id integer primary key, date text, ticker text, direction text,
        final_score real, price_at_predict real, price_t7 real, exit_reason text, checked_t7 int,
        return_t1 real, return_t7 real, return_t30 real, checked_t1 int, checked_t30 int,
        correct_t1 int, correct_t7 int, correct_t30 int)""")
    c.execute("insert into predictions(date,ticker,direction,final_score,price_at_predict,price_t7,exit_reason,"
              "checked_t7,return_t7,checked_t1,checked_t30,correct_t7) values "
              "('2026-08-03','AAA','bullish',7,100,105,'T7_CLOSE',1,5.0,0,0,1)")
    c.commit()
    c.close()


def _md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


def _env(home):
    e = dict(os.environ)
    e["ALPHA_HIVE_HOME"] = str(home)
    e.pop("ALPHA_HIVE_DB_PATH", None)
    return e


def _tool(args, home):
    return subprocess.run([sys.executable, *args], cwd=str(ROOT), env=_env(home),
                          capture_output=True, text=True, timeout=120)


class TestRepairToolsDefaultToDryRun:
    def test_migrate_ambiguous_default_is_dry_and_apply_backs_up_and_records(self, tmp_path):
        db = tmp_path / "pheromone.db"
        _pred_db(db)
        before = _md5(db)
        r = _tool(["migrate_ambiguous_backfill.py", "--db", str(db)], tmp_path)
        assert r.returncode == 0 and "DRY RUN" in r.stdout, r.stdout + r.stderr
        assert _md5(db) == before and not (tmp_path / "db_backups").exists()
        assert not (tmp_path / "migrations_state").exists()
        r = _tool(["migrate_ambiguous_backfill.py", "--db", str(db), "--apply"], tmp_path)
        assert r.returncode == 0 and "回填完成" in r.stdout, r.stdout + r.stderr
        assert list((tmp_path / "db_backups").glob("pheromone_pre_P0_tolerance_fix_*.db"))
        rec = R.read_ledger(tmp_path / "migrations_state")
        assert rec and rec[-1]["tool"] == "migrate_ambiguous_backfill" and rec[-1]["kind"] == "manual_tool"

    def test_signal_archive_backfill_default_is_dry_and_apply_backs_up_and_records(self, tmp_path):
        db = tmp_path / "pheromone.db"
        _pred_db(db)
        (tmp_path / ".swarm_results_2026-08-03.json").write_text("{}")
        before = _md5(db)
        r = _tool(["signal_archive.py", "--backfill", "--db", str(db)], tmp_path)
        assert r.returncode == 0 and "dry-run" in r.stdout, r.stdout + r.stderr
        assert _md5(db) == before and not (tmp_path / "_manual_backups").exists()
        r = _tool(["signal_archive.py", "--backfill", "--apply", "--db", str(db)], tmp_path)
        assert r.returncode == 0 and "回填完成" in r.stdout, r.stdout + r.stderr
        bks = list((tmp_path / "_manual_backups").glob("pheromone.db.bak-signal-archive-backfill-*"))
        assert len(bks) == 1 and _rows_pred(bks[0]) == 1
        assert R.read_ledger(tmp_path / "migrations_state")[-1]["tool"] == "signal_archive.backfill"

    def test_signal_archive_apply_with_dry_run_is_an_argument_error(self, tmp_path):
        db = tmp_path / "pheromone.db"
        _pred_db(db)
        r = _tool(["signal_archive.py", "--backfill", "--apply", "--dry-run", "--db", str(db)], tmp_path)
        assert r.returncode == 2 and "互斥" in r.stderr
        r = _tool(["signal_archive.py", "--list", "--apply", "--db", str(db)], tmp_path)
        assert r.returncode == 2

    def test_signal_archive_apply_without_backup_writes_nothing(self, tmp_path):
        """备份失败（库不存在）⇒ 中止，不建库、不写记录。"""
        db = tmp_path / "pheromone.db"
        db.write_bytes(b"this is not a sqlite file" * 10)    # 库在、却备不了
        before = _md5(db)
        r = _tool(["signal_archive.py", "--backfill", "--apply", "--db", str(db)], tmp_path)
        assert r.returncode == 2 and "备份失败" in r.stderr, r.stdout + r.stderr
        assert _md5(db) == before and not (tmp_path / "migrations_state").exists()

    @staticmethod
    def _run_dir_accuracy(monkeypatch, tmp_path, argv):
        import pandas as pd
        import backfill_dir_accuracy as M
        idx = pd.bdate_range("2026-07-27", "2026-08-31")
        s = pd.Series(100.0, index=idx)
        s[pd.Timestamp("2026-08-12")] = 105.0
        monkeypatch.setattr(M, "_fetch_closes", lambda tickers, a, b: {"AAA": s})
        import data_pipeline   # `_close_after` 的盘中护栏会向 Yahoo 问交易所时钟——桩成收盘后的某一刻
        from datetime import datetime
        monkeypatch.setattr(data_pipeline, "_exchange_now", lambda: datetime(2026, 10, 6, 17, 0))
        monkeypatch.setattr(sys, "argv", ["backfill_dir_accuracy.py", *argv])
        return M.main()

    def test_dir_accuracy_default_is_dry_and_does_not_even_alter_the_real_db(self, monkeypatch, tmp_path):
        db = tmp_path / "pheromone.db"
        _pred_db(db)
        before = _md5(db)
        assert self._run_dir_accuracy(monkeypatch, tmp_path, ["--db", str(db)]) == 0
        assert _md5(db) == before, "旧版 dry-run 会先对真库 ALTER TABLE——缺省 dry-run 必须一字节不动"
        cols = [r[1] for r in sqlite3.connect(db).execute("pragma table_info(predictions)")]
        assert "close_t7" not in cols

    def test_dir_accuracy_apply_backs_up_writes_and_records(self, monkeypatch, tmp_path):
        db = tmp_path / "pheromone.db"
        _pred_db(db)
        assert self._run_dir_accuracy(monkeypatch, tmp_path, ["--db", str(db), "--apply"]) == 0
        c = sqlite3.connect(db)
        assert c.execute("select close_t7, dir_correct_t7 from predictions").fetchone() == (105.0, 1)
        bks = list((tmp_path / "_manual_backups").glob("pheromone.db.bak-dir-accuracy-*"))
        assert len(bks) == 1
        assert "close_t7" not in [r[1] for r in sqlite3.connect(bks[0]).execute("pragma table_info(predictions)")], \
            "备份必须是写入之前的库"
        assert R.read_ledger(tmp_path / "migrations_state")[-1]["tool"] == "backfill_dir_accuracy"

    def test_dir_accuracy_apply_aborts_when_backup_fails(self, monkeypatch, tmp_path):
        import backfill_dir_accuracy as M  # noqa: F401
        db = tmp_path / "pheromone.db"
        _pred_db(db)
        from data_migrations import runner
        monkeypatch.setattr(runner, "backup_before_write", lambda *a, **k: (_ for _ in ()).throw(OSError("盘满")))
        before = _md5(db)
        assert self._run_dir_accuracy(monkeypatch, tmp_path, ["--db", str(db), "--apply"]) == 2
        assert _md5(db) == before


def _rows_pred(p):
    c = sqlite3.connect(p)
    try:
        return c.execute("select count(*) from predictions").fetchone()[0]
    finally:
        c.close()


# ── 编排器接入段：抽出函数在 bash 下真跑 ───────────────────────────────────
def _orch_text():
    from tests._orchestrator import repo_orchestrator_text
    return repo_orchestrator_text()


def _extract_step():
    t = _orch_text()
    a = t.index("_data_migrations_step() {")
    b = t.index("\n_data_migrations_step\n", a)
    return t[a:b + 1]


class TestOrchestratorStep:
    def _run(self, tmp_path, *, record, rc, script_present=True):
        body = _extract_step()
        rec_json = "" if record is None else json.dumps(record)
        proj = tmp_path / "proj"
        proj.mkdir(exist_ok=True)
        if script_present:
            (proj / "run_data_migrations.py").write_text("")
        script = f'''
set -uo pipefail
LOGDIR={shlex.quote(str(tmp_path))}; LOGFILE=/dev/null; PROJECT_DIR={shlex.quote(str(proj))}
log() {{ printf 'LOG\\t%s\\t%s\\n' "$1" "$2"; }}
run_step() {{
  shift 2; shift   # --timeout N script
  while [ $# -gt 0 ]; do if [ "$1" = "--out" ]; then OUT="$2"; fi; shift; done
  if [ -n {shlex.quote(rec_json)} ]; then printf '%s\\n' {shlex.quote(rec_json)} > "$OUT"; fi
  return {rc}
}}
STEPS_RESULT='{{}}'
{body}
_data_migrations_step
printf 'STEPS\\t%s\\n' "$(printf '%s' "$STEPS_RESULT" | jq -c .)"
'''
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        steps = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("STEPS\t")).split("\t", 1)[1])
        logs = [tuple(l.split("\t", 2)[1:]) for l in r.stdout.splitlines() if l.startswith("LOG\t")]
        return steps["data_migrations"], logs

    def test_ok(self, tmp_path):
        d, logs = self._run(tmp_path, record={"status": "ok", "applied": ["0001"], "n_pending": 1}, rc=0)
        assert d["status"] == "success" and d["outcome"] == "ok" and d["applied"] == ["0001"]
        assert any(lvl == "INFO" for lvl, _ in logs)

    @pytest.mark.parametrize("outcome", ["failed", "tampered", "layout_error", "backup_failed", "ledger_error", "crash"])
    def test_every_non_ok_outcome_is_failed_which_alert_manager_turns_into_p1(self, tmp_path, outcome):
        d, logs = self._run(tmp_path, record={"status": outcome, "error": "x"}, rc=1)
        assert d["status"] == "failed" and d["outcome"] == outcome
        assert any(lvl == "ERROR" and outcome in msg for lvl, msg in logs), logs

    def test_no_record_is_failed_not_success(self, tmp_path):
        d, _ = self._run(tmp_path, record=None, rc=124)
        assert d["status"] == "failed" and d["outcome"] == "no_record"

    def test_tool_missing_is_skipped_and_visible(self, tmp_path):
        d, logs = self._run(tmp_path, record=None, rc=0, script_present=False)
        assert d["status"] == "skipped" and d["outcome"] == "tool_missing"
        assert any(lvl == "WARN" for lvl, _ in logs)

    def test_stale_record_is_not_trusted(self, tmp_path):
        (tmp_path / "data_migrations.json").write_text(json.dumps({"status": "ok", "applied": [], "n_pending": 0}))
        d, _ = self._run(tmp_path, record=None, rc=1)
        assert d["outcome"] == "no_record", "上一轮遗留的 ok 记录不许当成本轮结果"

    def test_step_sits_between_db_backup_and_step2(self):
        t = _orch_text()
        assert t.index("# <<< DB_BACKUP_END") < t.index("\n_data_migrations_step\n") < t.index("【Step 2/5】蜂群分析 - 启动")


class TestRegistrations:
    def test_paths_property_follows_home_at_call_time(self, tmp_path, monkeypatch):
        from hive_logger import PATHS
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "x"))
        assert PATHS.migrations_state == tmp_path / "x" / "migrations_state"

    def test_listed_in_backup_move_rules_and_gitignore(self):
        from data_backup import export, migrate_data_root as mig
        assert "migrations_state" in export.STATE_DIRS and "migrations_state" in mig.MOVE_DIRS
        assert "/migrations_state/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert mig.SKIP_EXACT.get("data_migrations", "").startswith("代码")


# ── 二次检查（v0.45.420）：首版的四处缺陷，各一条先红后绿的守卫 ─────────────────
def _wal_db_without_sidecars(path):
    c = sqlite3.connect(path)
    c.execute("pragma journal_mode=wal")
    c.execute("create table t(k text primary key, v int)")
    c.execute("insert into t values ('seed', 1)")
    c.commit()
    c.close()
    assert not Path(str(path) + "-wal").exists(), "夹具前提：已 checkpoint，没有 -wal"


def _sidecars(path):
    return sorted(p.name for p in Path(path).parent.glob(Path(path).name + "-*"))


class TestReadOnlyOpenFollowsTheExistingStrategy:
    """首版自己写了 `mode=ro` 打开，没用现成的 `data_backup/sqlite_readonly.py`：
    WAL 且没有 -wal 的库被光 `mode=ro` 打开，会在**生产目录新建** -wal/-shm 且关闭后删不掉
    （阶段 0 实测、该模块 docstring 第 2 条）；hot journal 也不会拒绝。"""

    def test_backup_of_checkpointed_wal_db_creates_no_sidecars(self, tmp_path):
        db = tmp_path / "w.db"
        _wal_db_without_sidecars(db)
        R.online_backup(db, tmp_path / "bk" / "copy.db")
        assert _sidecars(db) == [], "备份读源库不许在源目录留下 -wal/-shm"
        assert _rows(tmp_path / "bk" / "copy.db") == [("seed",)]

    def test_fingerprint_creates_no_sidecars(self, tmp_path):
        db = tmp_path / "w.db"
        _wal_db_without_sidecars(db)
        R.db_fingerprint(db)
        assert _sidecars(db) == []

    def test_run_pending_apply_leaves_no_sidecars_from_its_own_backup(self, tmp_path):
        vd, _ = _mk(tmp_path, specs=())
        db = tmp_path / "pheromone.db"
        db.unlink(missing_ok=True)
        _wal_db_without_sidecars(db)
        (vd / "0001_a.py").write_text(OK_MIG.format(desc="a", key="a"))
        r = _run(tmp_path, vd, db)
        assert r["status"] == "ok", r
        # 迁移自己用普通连接写库，WAL 会在它关闭时清掉；这里只核**备份**没造出孤儿
        assert not [n for n in _sidecars(db) if n.endswith("-journal")]

    def test_hot_journal_refuses_backup_and_runs_nothing(self, tmp_path):
        vd, db = _mk(tmp_path)
        Path(str(db) + "-journal").write_bytes(b"hot")
        r = _run(tmp_path, vd, db)
        assert r["status"] == "backup_failed" and "hot journal" in r["error"], r
        assert not (tmp_path / "ran_a").exists()

    def test_dir_accuracy_dry_run_creates_no_sidecars_on_a_wal_db(self, monkeypatch, tmp_path):
        db = tmp_path / "pheromone.db"
        _pred_db(db)
        c = sqlite3.connect(db)
        c.execute("pragma journal_mode=wal")
        c.close()
        assert _sidecars(db) == []
        assert TestRepairToolsDefaultToDryRun._run_dir_accuracy(monkeypatch, tmp_path, ["--db", str(db)]) == 0
        assert _sidecars(db) == []


class TestBackupAndLedgerFollowTheDbNotTheEnvironment:
    """首版的备份与记录落在 `PATHS.home` 下，不管 `--db` 指哪：对 /tmp 里的库副本 `--apply`，
    备份会堆进生产 `_manual_backups/`、生产 `applied.jsonl` 里多一行描述别的库的记录。"""

    def test_apply_on_a_db_elsewhere_writes_beside_that_db_only(self, tmp_path):
        home, other = tmp_path / "home", tmp_path / "copy"
        home.mkdir()
        other.mkdir()
        db = other / "pheromone.db"
        _pred_db(db)
        (other / ".swarm_results_2026-08-03.json").write_text("{}")
        r = _tool(["signal_archive.py", "--backfill", "--apply", "--db", str(db)], home)
        assert r.returncode == 0, r.stdout + r.stderr
        assert list((other / "_manual_backups").glob("pheromone.db.bak-signal-archive-backfill-*"))
        assert R.read_ledger(other / "migrations_state")[-1]["tool"] == "signal_archive.backfill"
        assert not (home / "_manual_backups").exists() and not (home / "migrations_state").exists(), \
            "生产根不许被一个别处的库污染"


class TestSignalArchiveApplyOnEmptyRoot:
    def test_apply_with_no_db_yet_is_not_refused_and_takes_no_backup(self, tmp_path):
        """空数据根里用 swarm 文件从零建 signal_archive（恢复演练的真用法）：没有库就没有东西可备份，
        不该因此拒绝写入；首版把它当备份失败中止了。"""
        (tmp_path / ".swarm_results_2026-08-03.json").write_text("{}")
        db = tmp_path / "pheromone.db"
        r = _tool(["signal_archive.py", "--backfill", "--apply", "--db", str(db)], tmp_path)
        assert r.returncode == 0 and "无库可备份" in r.stdout, r.stdout + r.stderr
        rec = R.read_ledger(tmp_path / "migrations_state")[-1]
        assert rec["backup"] is None


class TestOrchestratorToolMissingIsDecidedByTheFile:
    def test_rc2_with_the_script_present_is_failed_not_skipped(self, tmp_path):
        """首版按退出码 2 判「脚本不存在」——但 argparse 错误 / run_step 的哨兵也是 2；
        同类的编排器自动部署段明说「不按退出码分支」。现在按文件在不在判。"""
        body = _extract_step()
        script_dir = tmp_path / "proj"
        script_dir.mkdir()
        (script_dir / "run_data_migrations.py").write_text("")
        script = f'''
set -uo pipefail
LOGDIR={shlex.quote(str(tmp_path))}; LOGFILE=/dev/null; PROJECT_DIR={shlex.quote(str(script_dir))}
log() {{ printf 'LOG\\t%s\\t%s\\n' "$1" "$2"; }}
run_step() {{ return 2; }}
STEPS_RESULT='{{}}'
{body}
_data_migrations_step
printf 'STEPS\\t%s\\n' "$(printf '%s' "$STEPS_RESULT" | jq -c .)"
'''
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
        steps = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("STEPS\t")).split("\t", 1)[1])
        assert steps["data_migrations"]["status"] == "failed", steps
