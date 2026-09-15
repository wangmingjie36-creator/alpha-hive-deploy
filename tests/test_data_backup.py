"""数据根迁移 · 阶段 3 —— `data_backup/` 的回归覆盖。

只用**合成**数据（临时 sqlite 库 + 临时凭据文件），不碰生产 `pheromone.db`
也不含任何真实密钥字面量——真实密钥的变异演练是阶段 3 落地时用本机真实
密钥文件手动跑的一次性验收（见 CHANGELOG），不适合写进常跑的测试文件
（那样等于把「密钥去哪扫」的清单和一次真实凭据样本焊进了 git 历史）。
"""
import json
import sqlite3

import pytest

from data_backup import export as export_mod
from data_backup import restore as restore_mod
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
