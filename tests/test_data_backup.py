"""数据根迁移 · 阶段 3 —— `data_backup/` 的回归覆盖。

只用**合成**数据（临时 sqlite 库 + 临时凭据文件），不碰生产 `pheromone.db`
也不含任何真实密钥字面量——真实密钥的变异演练是阶段 3 落地时用本机真实
密钥文件手动跑的一次性验收（见 CHANGELOG），不适合写进常跑的测试文件
（那样等于把「密钥去哪扫」的清单和一次真实凭据样本焊进了 git 历史）。
"""
import json
import sqlite3
import subprocess

import pytest

from data_backup import export as export_mod
from data_backup import restore as restore_mod
from data_backup import run_backup
from data_backup.scan_secrets import load_known_secrets, scan_directory
from data_backup.sqlite_readonly import HotJournalError, db_open_uri


def _make_synthetic_db(path, wal=False):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY, name TEXT, score REAL, blob_col BLOB)")
    conn.execute("CREATE INDEX idx_t1_name ON t1(name)")
    rows = [(1, "alice", 1.5, b"\x00\x01"), (2, "o'brien", None, None), (3, "碳基", 3.0, b"")]
    conn.executemany("INSERT INTO t1 VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return rows


class TestSqliteReadonlyOpenStrategy:
    def test_rollback_mode_opens_ro(self, tmp_path):
        db = tmp_path / "a.db"
        _make_synthetic_db(db, wal=False)
        uri, how = db_open_uri(db)
        assert uri == f"file:{db}?mode=ro"
        assert "rollback" in how

    def test_wal_without_sidecar_forces_immutable(self, tmp_path):
        db = tmp_path / "b.db"
        _make_synthetic_db(db, wal=True)
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        assert not (tmp_path / "b.db-wal").exists() or (tmp_path / "b.db-wal").stat().st_size == 0
        uri, how = db_open_uri(db)
        # WAL 模式的 header 不受 checkpoint 影响，只有 -wal 文件是否存在会变；
        # TRUNCATE 后文件仍可能存在但是 0 字节——用真正删除来测「无 -wal」分支。
        wal_sidecar = tmp_path / "b.db-wal"
        if wal_sidecar.exists():
            wal_sidecar.unlink()
        uri, how = db_open_uri(db)
        assert "immutable=1" in uri
        assert "无-wal" in how

    def test_hot_journal_rejected(self, tmp_path):
        db = tmp_path / "c.db"
        _make_synthetic_db(db)
        (tmp_path / "c.db-journal").write_bytes(b"\x00")
        with pytest.raises(HotJournalError):
            db_open_uri(db)


class TestExportRestoreRoundTrip:
    def test_table_split_export_and_restore_matches_source(self, tmp_path):
        src_root = tmp_path / "src"
        src_root.mkdir()
        db_path = src_root / "sample.db"
        rows = _make_synthetic_db(db_path)

        out_dir = tmp_path / "backup"
        info = export_mod.export_db(src_root, "sample", "sample.db", out_dir / "db_exports")
        assert info["row_counts"] == {"t1": len(rows)}
        assert (out_dir / "db_exports" / "sample" / "t1.sql").exists()
        assert "CREATE INDEX idx_t1_name" in (
            out_dir / "db_exports" / "sample" / "_schema_extra.sql").read_text()

        restored_path = tmp_path / "restored" / "sample.db"
        result = restore_mod.restore_db(out_dir, "sample", restored_path)
        assert result["integrity_check"] == ["ok"]
        assert result["row_counts"] == {"t1": len(rows)}

        conn = sqlite3.connect(restored_path)
        got = conn.execute("SELECT id, name, score, blob_col FROM t1 ORDER BY id").fetchall()
        conn.close()
        assert got == rows
        # 索引也要真的建出来了，不能只是数据对
        conn = sqlite3.connect(restored_path)
        idx_names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        assert "idx_t1_name" in idx_names

    def test_export_detects_concurrent_writer(self, tmp_path, monkeypatch):
        """导出期间如果行数变了必须报红，不能悄悄导出一份不一致的快照。"""
        src_root = tmp_path / "src"
        src_root.mkdir()
        db_path = src_root / "sample.db"
        _make_synthetic_db(db_path)

        real_table_row_counts = export_mod.table_row_counts
        call_count = {"n": 0}

        def flaky_counts(conn):
            call_count["n"] += 1
            counts = real_table_row_counts(conn)
            if call_count["n"] == 2:  # 模拟“导出中途被写了一行”
                counts = dict(counts)
                counts["t1"] = counts["t1"] + 1
            return counts

        monkeypatch.setattr(export_mod, "table_row_counts", flaky_counts)
        with pytest.raises(RuntimeError, match="行数变了"):
            export_mod.export_db(src_root, "sample", "sample.db", tmp_path / "backup" / "db_exports")


class TestSecretScan:
    def _fake_key_files(self, tmp_path, av_key="AKIA_FAKE_TOKEN_123456"):
        key_dir = tmp_path / "home"
        key_dir.mkdir()
        plain = key_dir / ".fake_av_key"
        plain.write_text(av_key + "\n")
        creds = key_dir / ".fake_gmail_credentials.json"
        creds.write_text(json.dumps({"installed": {"client_secret": "GOCSPX-fake-secret-value-000"}}))
        return [str(plain), str(creds)]

    def test_clean_export_passes(self, tmp_path):
        key_files = self._fake_key_files(tmp_path)
        secrets = load_known_secrets(key_files)
        assert len(secrets) >= 2

        export_dir = tmp_path / "clean_export"
        export_dir.mkdir()
        (export_dir / "t1.sql").write_text("INSERT INTO t1 VALUES (1, 'no secrets here');\n")
        result = scan_directory(export_dir, secrets)
        assert result["hits"] == []

    def test_injected_plain_secret_is_rejected(self, tmp_path):
        key_files = self._fake_key_files(tmp_path)
        secrets = load_known_secrets(key_files)

        export_dir = tmp_path / "mutated_export"
        export_dir.mkdir()
        (export_dir / "t1.sql").write_text(
            "INSERT INTO t1 VALUES (1, 'oops');\n-- leaked: AKIA_FAKE_TOKEN_123456\n"
        )
        result = scan_directory(export_dir, secrets)
        assert len(result["hits"]) == 1
        assert result["hits"][0]["credential_source"] == ".fake_av_key"

    def test_injected_json_leaf_secret_is_rejected(self, tmp_path):
        key_files = self._fake_key_files(tmp_path)
        secrets = load_known_secrets(key_files)

        export_dir = tmp_path / "mutated_export_json"
        export_dir.mkdir()
        (export_dir / "state.json").write_text(
            json.dumps({"note": "client_secret leaked: GOCSPX-fake-secret-value-000"})
        )
        result = scan_directory(export_dir, secrets)
        assert any(h["credential_source"] == ".fake_gmail_credentials.json" for h in result["hits"])

    def test_scan_never_returns_the_secret_value(self, tmp_path):
        key_files = self._fake_key_files(tmp_path)
        secrets = load_known_secrets(key_files)
        export_dir = tmp_path / "mutated_export2"
        export_dir.mkdir()
        (export_dir / "t1.sql").write_text("AKIA_FAKE_TOKEN_123456")
        result = scan_directory(export_dir, secrets)
        dumped = json.dumps(result)
        assert "AKIA_FAKE_TOKEN_123456" not in dumped

    def test_no_key_files_found_is_treated_as_scan_failure(self, tmp_path, monkeypatch):
        """一个密钥文件都读不到时不能悄悄放行——那等于"扫了"但其实什么都没比对。"""
        from data_backup import scan_secrets as scan_mod
        monkeypatch.setattr(scan_mod, "REAL_KEY_FILES", [str(tmp_path / "does_not_exist")])
        rc = scan_mod.main(["--dir", str(tmp_path)])
        assert rc == 2


class TestRunBackupStageReporting:
    """退出码 2 是 export/commit/push 三种失败共用的（`run_backup.main()` 只把
    `stage == "secret_scan"` 单独映射成退出码 1，其余一律 2）——这组测试锁定
    `status.json` 的 `stage` 字段在每条失败路径下都准确，不会被下游（编排器
    Step 14）误读成另一种失败。对应项目 CLAUDE.md 硬检查项「这个失败，下游
    怎么知道？」，修复见 CHANGELOG v0.45.269：编排器此前把 rc==2 硬解读成
    「已提交但推送失败」，导致 export/commit 失败（根本没提交成功）也被误报
    成已提交。

    不 mock `load_known_secrets` 之外的任何东西时用真实 git（`_init_backup_git_repo`
    起一个真实的本地仓库/裸仓库），只在需要精确注入某一步失败时才 monkeypatch
    `_run_git`——这样"提交成功"、"推送失败"这些断言测的是真实 git 行为，不是
    自己模拟出来的假象。
    """

    def _synthetic_src(self, tmp_path):
        """4 个库是 export_mod.DBS 的硬编码范围，缺一个 export_db() 就会
        FileNotFoundError——测 commit/push 阶段前必须先让 export 真实跑通。"""
        src = tmp_path / "src"
        src.mkdir()
        for db_name in ("pheromone.db", "metrics.db", "sentiment_baseline.db", "hive_predictions.db"):
            _make_synthetic_db(src / db_name)
        return src

    def _init_backup_git_repo(self, backup_dir, branch="main"):
        backup_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", branch], cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                        cwd=str(backup_dir), check=True, capture_output=True)

    def test_export_failure_reports_export_stage(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("模拟并发写检测命中")

        monkeypatch.setattr(export_mod, "run_export", boom)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()  # run() 里的 git init 假定 backup_dir 已存在，不会自己创建
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 2
        status = json.loads(status_file.read_text())
        assert status["stage"] == "export"
        assert status["ok"] is False

    def test_commit_failure_reports_commit_stage(self, tmp_path, monkeypatch):
        real_run_git = run_backup._run_git

        def fake_run_git(args, cwd):
            if args and args[0] == "commit":
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="模拟 commit 失败")
            return real_run_git(args, cwd)

        monkeypatch.setattr(run_backup, "load_known_secrets", lambda *a, **kw: [])
        monkeypatch.setattr(run_backup, "_run_git", fake_run_git)

        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()  # run() 里的 git init 假定 backup_dir 已存在，不会自己创建
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 2
        status = json.loads(status_file.read_text())
        assert status["stage"] == "commit"
        assert status["ok"] is False

    def test_push_failure_reports_push_stage(self, tmp_path, monkeypatch):
        """不 mock git——真实起一个没配 remote 的仓库，`git push origin main`
        会真实失败，验证 run() 把这类失败真的分类成 stage="push"，且提交
        必须已经真实成功（跟 export/commit 失败不是一回事）。"""
        monkeypatch.setattr(run_backup, "load_known_secrets", lambda *a, **kw: [])

        backup_dir = tmp_path / "backup"
        self._init_backup_git_repo(backup_dir)

        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 2
        status = json.loads(status_file.read_text())
        assert status["stage"] == "push"
        assert status["ok"] is False
        assert status["commit"]["made"] is True

    def test_full_success_reports_done_stage(self, tmp_path, monkeypatch):
        """真实本地裸仓库模拟远端，走完整 export→scan→commit→push 全流程。"""
        monkeypatch.setattr(run_backup, "load_known_secrets", lambda *a, **kw: [])

        bare_remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)],
                        check=True, capture_output=True)

        backup_dir = tmp_path / "backup"
        self._init_backup_git_repo(backup_dir)
        subprocess.run(["git", "remote", "add", "origin", str(bare_remote)],
                        cwd=str(backup_dir), check=True, capture_output=True)

        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 0
        status = json.loads(status_file.read_text())
        assert status["stage"] == "done"
        assert status["ok"] is True
