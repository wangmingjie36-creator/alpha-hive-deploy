"""数据根迁移 · 阶段 3 —— `data_backup/` 的回归覆盖。

只用**合成**数据（临时 sqlite 库 + 临时凭据文件），不碰生产 `pheromone.db`
也不含任何真实密钥字面量——真实密钥的变异演练是阶段 3 落地时用本机真实
密钥文件手动跑的一次性验收（见 CHANGELOG），不适合写进常跑的测试文件
（那样等于把「密钥去哪扫」的清单和一次真实凭据样本焊进了 git 历史）。
"""
import json
import os
import pwd
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from data_backup import export as export_mod
from data_backup import restore as restore_mod
from data_backup import run_backup
from data_backup.scan_secrets import (
    load_known_secrets,
    load_known_secrets_with_diagnostics,
    scan_directory,
)
from data_backup.sqlite_readonly import HotJournalError, db_open_uri


@pytest.fixture
def _sandbox_home(tmp_path_factory, monkeypatch):
    """`run_backup.main()` 的 `--backup-dir/--status-file/--history-file` 默认值都是
    `Path.home() / "alpha-hive-data" / ...`——真实数据根。`_isolate_env` 只隔离
    `ALPHA_HIVE_*`、不隔离 `$HOME`，所以没显式传路径的测试会把伪造记录写进真实
    `backup_status_history.jsonl`（v0.45.284 给 `run()` 加追加历史后，
    `TestRunBackupStageReporting` 的 7 个老测试没跟着传 `--history-file`）。
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    return home


# 整个文件套沙箱 HOME：新增的测试即使忘了传路径也不会写真实目录。
# `_ORCH` 在 import 时已算好、bash 沙箱不读 HOME，所以读编排器的那批不受影响。
pytestmark = pytest.mark.usefixtures("_sandbox_home")


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


def _bypass_secret_scan(monkeypatch):
    """给不测密钥扫描本身、只关心 commit/push/git_error 阶段的用例用：喂一个
    肯定不会出现在任何合成导出产物里的假密钥，绕过 v0.45.307 新增的"扫描
    等于没扫就拒绝"守卫（secrets 为空，或已存在的凭据文件零贡献/不可读，
    一律拒绝提交）。monkeypatch 的是 `load_known_secrets_with_diagnostics`
    ——`run()` 内部实际调用的正是这个名字，不是薄包装 `load_known_secrets`
    （v0.45.307 前测试 monkeypatch 的是后者，签名改了要跟着改，否则这些
    monkeypatch 不再生效，测试会意外真的去读本机文件系统）。"""
    monkeypatch.setattr(
        run_backup, "load_known_secrets_with_diagnostics",
        lambda *a, **kw: (
            [("fake_test_credentials", "not-present-in-any-synthetic-export-abcdef123456")],
            {"existing_files": ["fake_test_credentials"], "files_unreadable": [], "files_with_zero_secrets": []},
        ),
    )


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

    # ── issue 6（二次检查 2026-09-21）：状态目录白名单跳过的文件必须留痕 ──

    def test_copy_state_dir_records_skipped_non_text_files(self, tmp_path):
        """`copy_state_dir` 按后缀白名单过滤是刻意设计（只拷文本状态文件），
        但旧代码对被过滤掉的文件完全静默——今天只漏 `.gitkeep` 和一份旧
        `.db` 快照，以后新增 `.csv`/`.pkl` 之类文件会在 MANIFEST 里连痕迹
        都没有。"""
        src_root = tmp_path / "src"
        state_dir = src_root / "hedge_state"
        state_dir.mkdir(parents=True)
        (state_dir / "keep.json").write_text("{}")
        (state_dir / ".gitkeep").write_text("")
        (state_dir / "legacy_snapshot.db").write_bytes(b"\x00\x01sqlite-ish-binary-stuff")
        (state_dir / "notes.csv").write_text("a,b\n1,2\n")

        out_dir = tmp_path / "backup"
        copied, skipped = export_mod.copy_state_dir(src_root, "hedge_state", out_dir)

        assert {Path(r["rel"]).name for r in copied} == {"keep.json"}
        skipped_names = {Path(r["rel"]).name for r in skipped}
        assert skipped_names == {".gitkeep", "legacy_snapshot.db", "notes.csv"}
        # 跳过记录要带大小（诊断用），不能读取/拷贝内容——不能把本该被排除
        # 的文件反而纳入产物。
        for r in skipped:
            assert "sha256" not in r
            assert r["size"] >= 0
        for name in skipped_names:
            assert not (out_dir / "hedge_state" / name).exists()

    def test_run_export_manifest_carries_state_dirs_skipped(self, tmp_path):
        """`run_export()` 的 manifest 里必须能看到每个 STATE_DIRS 目录各自的
        跳过清单——不是只有 `copy_state_dir` 内部知道，manifest 才是"下游
        怎么知道"的落地位置（同项目 CLAUDE.md「这个失败，下游怎么知道？」，
        这里不是失败但同一形状：被排除的文件不能连痕迹都没有）。"""
        src_root = tmp_path / "src"
        src_root.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src_root / db_name)
        state_dir = src_root / export_mod.STATE_DIRS[0]
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "keep.json").write_text("{}")
        (state_dir / "orphan.csv").write_text("a,b\n1,2\n")

        out_dir = tmp_path / "backup"
        manifest = export_mod.run_export(src_root, out_dir)

        assert "state_dirs_skipped" in manifest
        first_dir = export_mod.STATE_DIRS[0]
        skipped_here = manifest["state_dirs_skipped"][first_dir]
        assert {Path(r["rel"]).name for r in skipped_here} == {"orphan.csv"}
        # 没有跳过任何文件的目录，键要在（空列表），不能因为"没什么可报"就
        # 连键都不出现——一致的结构比"有才有"更容易被下游代码正确处理。
        for d in export_mod.STATE_DIRS[1:]:
            assert d in manifest["state_dirs_skipped"]


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

    # ── issue 3：JSON 凭据叶子里的公开 URL 不该被当密钥（二次检查 2026-09-21）──

    def _fake_oauth_credentials(self, tmp_path):
        """仿真 `~/.alpha_hive_gmail_credentials.json` 的真实结构（标准 Google
        OAuth client_secret 文件格式，全部是假值）：三个公开 URL 字段
        （auth_uri/token_uri/auth_provider_x509_cert_url）+ 一个公开 URL 列表
        叶子（redirect_uris[0] == "http://localhost"，16 字符，够旧门槛）+
        一个真正的密钥（client_secret）。"""
        creds = tmp_path / ".fake_gmail_credentials.json"
        creds.write_text(json.dumps({
            "installed": {
                "client_id": "123456-fakeidfakeidfakeid.apps.googleusercontent.com",
                "project_id": "alpha-hive-fake-project",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_secret": "GOCSPX-fake-secret-value-000",
                "redirect_uris": ["http://localhost"],
            }
        }))
        return creds

    def test_public_url_leaves_are_not_loaded_as_secrets(self, tmp_path):
        creds = self._fake_oauth_credentials(tmp_path)
        secrets = load_known_secrets([str(creds)])
        values = {v for _n, v in secrets}
        for url in (
            "https://accounts.google.com/o/oauth2/auth",
            "https://oauth2.googleapis.com/token",
            "https://www.googleapis.com/oauth2/v1/certs",
            "http://localhost",
        ):
            assert url not in values, f"公开 URL {url!r} 不该被当密钥字面量收进比对表"
        assert "GOCSPX-fake-secret-value-000" in values, "真正的 client_secret 不能被一并滤掉"

    def test_public_url_leaf_does_not_false_positive_on_unrelated_files(self, tmp_path):
        """回归复现：http://localhost 这种公开 URL 一旦被当密钥，会在任意提到过
        localhost 的无关文件里"命中"，导致备份被永久拦停——这里用 20 个不相关的
        会话记录文件模拟这个场景，命中数必须是 0。"""
        creds = self._fake_oauth_credentials(tmp_path)
        secrets = load_known_secrets([str(creds)])

        export_dir = tmp_path / "unrelated_sessions"
        export_dir.mkdir()
        for i in range(20):
            (export_dir / f"session_{i}.json").write_text(json.dumps({
                "note": f"session {i} referenced http://localhost during local dev",
                "some_field": "see https://accounts.google.com/o/oauth2/auth for spec",
            }))

        result = scan_directory(export_dir, secrets)
        assert result["hits"] == [], f"公开 URL 造成了 {len(result['hits'])} 处误报"

    def test_short_generic_json_leaf_below_higher_threshold_is_dropped(self, tmp_path):
        """键名不像密钥、又不是 URL 的短字符串退回更高门槛（20 字符），
        压低通用短语的误报面——这条本身不是复现的 bug，是新加的过滤器的
        正对照：确认它真的在生效，不是形同虚设。"""
        creds = tmp_path / ".fake_generic.json"
        creds.write_text(json.dumps({"note": "short-ish"}))  # 9 字符，两档门槛都不够
        secrets = load_known_secrets([str(creds)])
        assert secrets == []

    def test_sensitive_key_name_still_uses_lower_threshold(self, tmp_path):
        """键名含 secret/token/key 等标记词时，仍按旧的低门槛（12 字符）收——
        不能因为新加了通用高门槛就误伤了真正的短密钥字段。"""
        creds = tmp_path / ".fake_short_secret.json"
        creds.write_text(json.dumps({"api_key": "short-key-13c"}))  # 13 字符：过 12，不过 20
        secrets = load_known_secrets([str(creds)])
        assert any(v == "short-key-13c" for _n, v in secrets)

    # ── issue 2b：坏 JSON 凭据文件的诊断可见性 ──────────────────────────

    def test_malformed_json_credential_file_is_reported_unreadable(self, tmp_path):
        """存在但解析不出来的 JSON 凭据文件（截断/非法语法）此前被 `continue`
        悄悄跳过——诊断字典现在要能指出具体是哪个文件。"""
        bad = tmp_path / ".alpha_hive_gmail_credentials.json"
        bad.write_text("{not valid json,,,")
        secrets, diag = load_known_secrets_with_diagnostics([str(bad)])
        assert secrets == []
        assert diag["files_unreadable"] == [".alpha_hive_gmail_credentials.json"]
        assert diag["files_with_zero_secrets"] == []

    def test_non_utf8_credential_file_is_reported_unreadable(self, tmp_path):
        bad = tmp_path / ".alpha_hive_fake_key"
        bad.write_bytes(b"\xff\xfe\x00not-utf8")
        secrets, diag = load_known_secrets_with_diagnostics([str(bad)])
        assert secrets == []
        assert diag["files_unreadable"] == [".alpha_hive_fake_key"]

    def test_existing_file_with_only_filtered_leaves_is_reported_zero_secrets(self, tmp_path):
        """文件存在、可读，但过滤后一个字面量都没贡献（比如整份 JSON 都是
        公开 URL）——这跟"读取失败"是不同的诊断类别，两者都要能看见。"""
        creds = tmp_path / ".fake_all_urls.json"
        creds.write_text(json.dumps({"auth_uri": "https://example.com/auth"}))
        secrets, diag = load_known_secrets_with_diagnostics([str(creds)])
        assert secrets == []
        assert diag["existing_files"] == [".fake_all_urls.json"]
        assert diag["files_unreadable"] == []
        assert diag["files_with_zero_secrets"] == [".fake_all_urls.json"]

    def test_load_known_secrets_thin_wrapper_still_returns_plain_list(self, tmp_path):
        """向后兼容：既有直接调用方（本文件其余测试）用的签名不能变。"""
        key_files = self._fake_key_files(tmp_path)
        secrets = load_known_secrets(key_files)
        assert isinstance(secrets, list)
        assert all(isinstance(t, tuple) and len(t) == 2 for t in secrets)


class TestSecretScanGuardInRun:
    """issue 2a（二次检查 2026-09-21）：`run()` 里要有"扫描等于没扫就拒绝"的
    守卫。`scan_secrets.py::main()` CLI 入口本来就有这道守卫（见
    `TestSecretScan.test_no_key_files_found_is_treated_as_scan_failure`），但
    生产实际路径 `run()` 此前直接调 `load_known_secrets()` + `scan_directory()`，
    一个密钥文件都读不到、或磁盘上确实存在的凭据文件读取失败/零贡献，都会
    被当成"扫了、没扫到"直接放行提交。
    """

    def _synthetic_src(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        return src

    def _backup_dir_with_remote(self, tmp_path):
        bare_remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)],
                        check=True, capture_output=True)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare_remote)],
                        cwd=str(backup_dir), check=True, capture_output=True)
        return backup_dir, bare_remote

    def test_zero_secrets_loaded_blocks_commit(self, tmp_path, monkeypatch):
        """本机一个真实密钥文件都没读到——此前会被当成"扫了、没扫到"直接放行。"""
        monkeypatch.setattr(
            run_backup, "load_known_secrets_with_diagnostics",
            lambda *a, **kw: ([], {"existing_files": [], "files_unreadable": [], "files_with_zero_secrets": []}),
        )
        backup_dir, bare_remote = self._backup_dir_with_remote(tmp_path)
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 1
        status = json.loads(status_file.read_text())
        assert status["stage"] == "secret_scan"
        assert status["ok"] is False
        remote_log = subprocess.run(["git", "log", "--oneline"], cwd=bare_remote,
                                     capture_output=True, text=True).stdout.strip()
        assert remote_log == "", "守卫没拦住——依然提交推送了"

    def test_existing_unreadable_credential_file_blocks_commit(self, tmp_path, monkeypatch):
        """磁盘上确实存在某个凭据文件，但读取/解析失败——不能悄悄放行。"""
        monkeypatch.setattr(
            run_backup, "load_known_secrets_with_diagnostics",
            lambda *a, **kw: (
                [("other_key", "some-other-secret-value-1234567890")],
                {"existing_files": ["other_key", "bad.json"], "files_unreadable": ["bad.json"],
                 "files_with_zero_secrets": []},
            ),
        )
        backup_dir, _bare_remote = self._backup_dir_with_remote(tmp_path)
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 1
        status = json.loads(status_file.read_text())
        assert status["stage"] == "secret_scan"
        assert status["secret_scan"]["files_unreadable"] == ["bad.json"]

    def test_existing_zero_contribution_credential_file_blocks_commit(self, tmp_path, monkeypatch):
        """磁盘上确实存在某个凭据文件、可读，但过滤后一个字面量都没贡献——
        同样不能悄悄放行（跟"读取失败"是不同的诊断类别）。"""
        monkeypatch.setattr(
            run_backup, "load_known_secrets_with_diagnostics",
            lambda *a, **kw: (
                [("other_key", "some-other-secret-value-1234567890")],
                {"existing_files": ["other_key", "zero.json"], "files_unreadable": [],
                 "files_with_zero_secrets": ["zero.json"]},
            ),
        )
        backup_dir, _bare_remote = self._backup_dir_with_remote(tmp_path)
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 1
        status = json.loads(status_file.read_text())
        assert status["stage"] == "secret_scan"
        assert status["secret_scan"]["files_with_zero_secrets"] == ["zero.json"]

    def test_guard_failure_status_never_contains_secret_values(self, tmp_path, monkeypatch):
        """守卫拒绝时 status.json 里绝不能出现真实密钥值——即便诊断信息里带着
        一个"其它凭据文件贡献的"密钥值，也只能报文件名。"""
        monkeypatch.setattr(
            run_backup, "load_known_secrets_with_diagnostics",
            lambda *a, **kw: (
                [("other_key", "SECRET_VALUE_MUST_NOT_APPEAR_1234567890")],
                {"existing_files": ["other_key", "zero.json"], "files_unreadable": [],
                 "files_with_zero_secrets": ["zero.json"]},
            ),
        )
        backup_dir, _bare_remote = self._backup_dir_with_remote(tmp_path)
        status_file = tmp_path / "status.json"
        run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert "SECRET_VALUE_MUST_NOT_APPEAR_1234567890" not in status_file.read_text()

    def test_real_integration_no_key_files_in_sandbox_home_blocks_commit(self, tmp_path):
        """不 monkeypatch `load_known_secrets_with_diagnostics`——本文件套的沙箱
        HOME 下真实没有任何 `~/.alpha_hive_*` 文件，走真实的
        `load_known_secrets_with_diagnostics()` 逻辑，验证生产路径确实接上了
        这道守卫（不是只在被 monkeypatch 出的假场景里生效）。"""
        backup_dir, bare_remote = self._backup_dir_with_remote(tmp_path)
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 1
        status = json.loads(status_file.read_text())
        assert status["stage"] == "secret_scan"
        remote_log = subprocess.run(["git", "log", "--oneline"], cwd=bare_remote,
                                     capture_output=True, text=True).stdout.strip()
        assert remote_log == "", "守卫没拦住——依然提交推送了"


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
        """库范围直接取 export_mod.DBS，缺一个 export_db() 就会
        FileNotFoundError——测 commit/push 阶段前必须先让 export 真实跑通。"""
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        return src

    def _init_backup_git_repo(self, backup_dir, branch="main"):
        """本地 config 必须显式覆盖可能存在的全局 `commit.gpgsign` / `core.hooksPath`——
        否则在全局开着提交签名（无可用密钥）或挂了会失败的全局 hook 的机器上，
        这里的真实 `git commit` 会因环境而失败，把 stage 误判成 "commit"，
        看起来像是被测代码的逻辑问题，实际与之无关（本机核实过当前不触发，
        但这是可移植性缺口，非假设性场景）。"""
        backup_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", branch], cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "core.hooksPath", os.devnull],
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

        _bypass_secret_scan(monkeypatch)
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
        _bypass_secret_scan(monkeypatch)

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

    def test_git_init_failure_reports_init_stage(self, tmp_path, monkeypatch):
        """git init 失败必须立刻中止——不能带着未初始化的仓库继续往下走 add/diff/
        commit：`git diff --cached --quiet` 在非 git 仓库里返回 128（不是"无变化"
        的 0），旧代码会把 128 误判成"有变化"进而尝试 commit，commit 失败后把
        "仓库根本没初始化成功"误标成 stage="commit"（v0.45.278 修）。"""
        real_run_git = run_backup._run_git

        def fake_run_git(args, cwd):
            if args and args[0] == "init":
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="模拟 git init 失败")
            return real_run_git(args, cwd)

        monkeypatch.setattr(run_backup, "_run_git", fake_run_git)

        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 2
        status = json.loads(status_file.read_text())
        assert status["stage"] == "init"
        assert status["ok"] is False
        # 中止必须发生在 init 就地——不能继续跑到 export，manifest_summary 不该出现
        assert "manifest_summary" not in status

    def test_git_exception_during_commit_phase_reports_git_error_stage(self, tmp_path, monkeypatch):
        """add/diff/commit 阶段任一次 git 调用抛异常（如 `_run_git` 的 timeout=60
        触发 `subprocess.TimeoutExpired`）必须映射到专门的 stage="git_error"，
        不能让异常一路不捕获地把 Python 进程以默认退出码 1 崩溃退出——退出码 1
        是专门留给 secret_scan 的，会被编排器 Step 14 误报成"密钥扫描命中"
        （v0.45.278 修）。"""
        real_run_git = run_backup._run_git

        def fake_run_git(args, cwd):
            if args and args[0] == "add":
                raise subprocess.TimeoutExpired(cmd=["git", "add", "-A"], timeout=60)
            return real_run_git(args, cwd)

        _bypass_secret_scan(monkeypatch)
        monkeypatch.setattr(run_backup, "_run_git", fake_run_git)

        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 2  # 不能是 1——1 是 secret_scan 专属
        status = json.loads(status_file.read_text())
        assert status["stage"] == "git_error"
        assert status["ok"] is False

    def test_git_exception_during_push_reports_git_error_stage(self, tmp_path, monkeypatch):
        """push 阶段抛异常（超时）同样要落进 stage="git_error"，且要能看出
        commit 其实已经真实成功——跟 stage="push"（返回码非 0，不是异常）
        不是一回事，下游据此决定"下一轮直接重推还是要人工排查"。"""
        real_run_git = run_backup._run_git

        def fake_run_git(args, cwd):
            if args and args[0] == "push":
                raise subprocess.TimeoutExpired(cmd=["git", "push", "origin", "main"], timeout=60)
            return real_run_git(args, cwd)

        _bypass_secret_scan(monkeypatch)
        monkeypatch.setattr(run_backup, "_run_git", fake_run_git)

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
        assert status["stage"] == "git_error"
        assert status["ok"] is False
        assert status["commit"]["made"] is True  # 提交已经真实成功，只是 push 抛了异常

    def test_full_success_reports_done_stage(self, tmp_path, monkeypatch):
        """真实本地裸仓库模拟远端，走完整 export→scan→commit→push 全流程。"""
        _bypass_secret_scan(monkeypatch)

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
        assert status["date"] == status["started_at"][:10]  # 编排器新鲜度校验读的就是这个字段

    # ── issue 1（二次检查 2026-09-21）：git add -A 返回码不能被忽略 ──────

    def test_git_add_failure_reports_git_error_stage(self, tmp_path, monkeypatch):
        """`git add -A` 返回非 0 时旧代码完全不检查——紧接着 `git diff --cached
        --quiet` 因为什么都没暂存而返回 0（"无变化"），整个流程就"成功"地
        什么都没提交。这里直接注入 add 失败，不依赖真实 index.lock 竞态。"""
        real_run_git = run_backup._run_git

        def fake_run_git(args, cwd):
            if args and args[0] == "add":
                return subprocess.CompletedProcess(args, returncode=128, stdout="",
                                                     stderr="模拟 git add -A 失败（index.lock）")
            return real_run_git(args, cwd)

        _bypass_secret_scan(monkeypatch)
        monkeypatch.setattr(run_backup, "_run_git", fake_run_git)

        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(status_file),
        ])
        assert rc == 2  # 不是 0——不能被误判成"无变化"而放行
        status = json.loads(status_file.read_text())
        assert status["stage"] == "git_error"
        assert status["ok"] is False
        assert "commit" not in status, "add 失败必须在 diff/commit 之前就中止"

    def test_stale_index_lock_does_not_silently_succeed(self, tmp_path, monkeypatch):
        """端到端复现原始 bug 场景（二次检查 2026-09-21 报告的复现步骤）：
        第一轮正常成功建立基线提交；改 src 让第二轮导出内容变化，在第二轮
        `run()` 调用前放一个残留的 `.git/index.lock`（模拟 git 进程被杀在
        add 半路）。修复前：`git add -A` 以 128 失败被忽略 → `git diff
        --cached --quiet` 因为空暂存区返回 0（"无变化"）→ `git push` 空转
        成功 → `status.json` 记 `ok: true, stage: "done"`，但工作树其实还有
        4 个文件的改动没提交、远端 HEAD 没前进。修复后：必须在 add 失败当场
        中止，`stage` 落 `git_error`，工作树保持"脏"但这个事实是可见的。"""
        _bypass_secret_scan(monkeypatch)

        bare_remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)],
                        check=True, capture_output=True)
        backup_dir = tmp_path / "backup"
        self._init_backup_git_repo(backup_dir)
        subprocess.run(["git", "remote", "add", "origin", str(bare_remote)],
                        cwd=str(backup_dir), check=True, capture_output=True)

        src = self._synthetic_src(tmp_path)
        status_file = tmp_path / "status.json"
        history_file = tmp_path / "history.jsonl"

        status1 = run_backup.run(src, backup_dir, remote="origin", branch="main",
                                  status_file=status_file, history_file=history_file)
        assert status1["ok"] is True
        remote_head_after_round1 = subprocess.run(
            ["git", "rev-parse", "main"], cwd=str(bare_remote),
            capture_output=True, text=True).stdout.strip()

        # 改 src，让第二轮导出内容真的发生变化
        conn = sqlite3.connect(src / export_mod.DBS["pheromone"])
        conn.execute("INSERT INTO t1 (id, name, score, blob_col) VALUES (99, 'x', 1.0, NULL)")
        conn.commit()
        conn.close()

        lock = backup_dir / ".git" / "index.lock"
        lock.write_text("stale lock left by a killed git process")
        try:
            status2 = run_backup.run(src, backup_dir, remote="origin", branch="main",
                                      status_file=status_file, history_file=history_file)
        finally:
            if lock.exists():
                lock.unlink()  # 清理测试自己留下的锁，不影响后续断言之外的状态

        assert status2["stage"] == "git_error"
        assert status2["ok"] is False

        porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=str(backup_dir),
                                    capture_output=True, text=True).stdout
        assert porcelain.strip(), "工作树应该还有真实存在的未提交改动（这是本来就该发生的，不是 bug）"

        remote_head_after_round2 = subprocess.run(
            ["git", "rev-parse", "main"], cwd=str(bare_remote),
            capture_output=True, text=True).stdout.strip()
        assert remote_head_after_round2 == remote_head_after_round1, (
            "远端不该前进——第二轮改动其实从未被提交/推送，"
            "如果远端头变了说明又发生了「假装成功」")


class TestRunBackupHistoryAppend:
    """`run()` 每条退出路径都要追加一行到 `history_file`（v0.45.284）——
    这是 `backup_continuity.py` 判"连续 N 天未识别/陈旧"唯一能读到的历史，
    `status.json` 本身每次调用整份覆盖，没有这份 JSONL 就无从判定连续性。
    """

    def _synthetic_src(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        return src

    def test_failure_appends_ok_false_record(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("模拟并发写检测命中")

        monkeypatch.setattr(export_mod, "run_export", boom)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        history_file = tmp_path / "history.jsonl"
        run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(tmp_path / "status.json"),
            "--history-file", str(history_file),
        ])
        lines = history_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["stage"] == "export"
        assert rec["ok"] is False
        assert rec["date"]

    def test_success_appends_ok_true_record(self, tmp_path, monkeypatch):
        _bypass_secret_scan(monkeypatch)
        bare_remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)],
                        check=True, capture_output=True)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "core.hooksPath", os.devnull],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare_remote)],
                        cwd=str(backup_dir), check=True, capture_output=True)

        history_file = tmp_path / "history.jsonl"
        rc = run_backup.main([
            "--src", str(self._synthetic_src(tmp_path)),
            "--backup-dir", str(backup_dir),
            "--status-file", str(tmp_path / "status.json"),
            "--history-file", str(history_file),
        ])
        assert rc == 0
        lines = history_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec == {"date": rec["date"], "stage": "done", "ok": True}

    def test_repeated_calls_append_not_overwrite(self, tmp_path, monkeypatch):
        """同一个 history_file 跨多次调用（跨天）必须累积成多行，
        不能像 status.json 那样被后一次覆盖——否则判连续性又回到只看"最近一次"。"""
        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(export_mod, "run_export", boom)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        history_file = tmp_path / "history.jsonl"
        src = self._synthetic_src(tmp_path)
        for _ in range(3):
            run_backup.main([
                "--src", str(src),
                "--backup-dir", str(backup_dir),
                "--status-file", str(tmp_path / "status.json"),
                "--history-file", str(history_file),
            ])
        lines = history_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

    def test_history_file_none_is_noop(self, tmp_path, monkeypatch):
        """`history_file` 不传（老调用方）不能报错——同 `status_file=None` 的既有行为。"""
        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(export_mod, "run_export", boom)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        status = run_backup.run(self._synthetic_src(tmp_path), backup_dir,
                                 status_file=tmp_path / "status.json")
        assert status["stage"] == "export"

    def test_history_write_failure_does_not_change_status(self, tmp_path, monkeypatch):
        """历史日志写不进去（比如父目录其实是个文件）不能影响本次判定结果——
        判定在写盘之前就已经完成，同 `_write_status` 已有的这条纪律。"""
        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(export_mod, "run_export", boom)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        blocked_parent = tmp_path / "not_a_dir"
        blocked_parent.write_text("occupied")  # 让 mkdir(parents=True) 必然失败
        history_file = blocked_parent / "history.jsonl"
        status = run_backup.run(self._synthetic_src(tmp_path), backup_dir,
                                 status_file=tmp_path / "status.json",
                                 history_file=history_file)
        assert status["stage"] == "export"
        assert status["ok"] is False


class TestWriteStatusOsErrorProtection:
    """issue 4（二次检查 2026-09-21）：`_write_status` 写盘失败不能让 `run()`
    以未分类异常向上爆炸。旧代码这里没有异常保护——`status_file` 指向一个
    不合法路径（比如父目录其实是个文件）时 `mkdir`/`write_text` 会抛
    `NotADirectoryError`，一路不捕获地让 Python 以默认退出码 1 崩溃退出。
    """

    def _synthetic_src(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        return src

    def _init_backup_git_repo(self, backup_dir, branch="main"):
        """本地 config 必须显式覆盖可能存在的全局配置——道理同
        `TestRunBackupStageReporting._init_backup_git_repo`。"""
        backup_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", branch], cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"],
                        cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "core.hooksPath", os.devnull],
                        cwd=str(backup_dir), check=True, capture_output=True)

    def test_write_status_swallows_os_error_and_returns_status_dict(self, tmp_path, monkeypatch):
        """`run()` 直接调用（不经 `main()`）时，`status_file` 路径不合法不能
        让 `run()` 抛异常——判定（status dict）已经算完，只是写盘失败。"""
        _bypass_secret_scan(monkeypatch)
        backup_dir = tmp_path / "backup"
        self._init_backup_git_repo(backup_dir)  # 没配 remote，会在 push 这步失败
        bogus_parent = tmp_path / "not_a_dir"
        bogus_parent.write_text("occupied")
        bogus_status_file = bogus_parent / "sub" / "status.json"

        # 走完 export→scan(绕过)→commit 后会在 push 这步失败（没配 remote）——
        # 具体落在哪个 stage 不是本测试关心的，只关心 _write_status 本身不会
        # 因为 status_file 路径不合法而向上抛异常，`run()` 必须正常返回。
        status = run_backup.run(self._synthetic_src(tmp_path), backup_dir,
                                 status_file=bogus_status_file)
        assert status["stage"] == "push"
        assert status["ok"] is False
        assert not bogus_status_file.exists()

    def test_main_catches_unexpected_exception_and_returns_3_not_1(self, tmp_path, monkeypatch):
        """`main()` 顶层 try/except：`run()` 之外/之内任何未被内部各阶段
        try/except 兜住的异常，都不能让 Python 以默认退出码 1 崩溃退出——
        1 是专属 `stage=="secret_scan"` 的语义，编排器 Step 14 会把任意
        rc==1 无条件报成"🚨 密钥扫描命中，已拒绝提交"。这里直接把 `run()`
        整个换成一个必炸的假函数，模拟"某处未预料到的 bug"，断言：
        ① rc 不是 0/1（不能跟 done/secret_scan 撞车）；
        ② status.json 落一个独立的 `stage: "crash"`，人工能看出这不是真的
        密钥扫描命中。"""
        def boom(*a, **kw):
            raise RuntimeError("模拟一个未被内部 try/except 覆盖的 bug")

        monkeypatch.setattr(run_backup, "run", boom)
        status_file = tmp_path / "status.json"
        rc = run_backup.main([
            "--src", str(tmp_path / "src"),
            "--backup-dir", str(tmp_path / "backup"),
            "--status-file", str(status_file),
            "--history-file", str(tmp_path / "history.jsonl"),
        ])
        assert rc not in (0, 1), f"rc={rc} 不能是 0 或 1——1 会被编排器误报成密钥扫描命中"
        status = json.loads(status_file.read_text())
        assert status["stage"] == "crash"
        assert status["ok"] is False
        assert "模拟一个未被内部 try/except 覆盖的 bug" in status["error"]

    def test_main_crash_status_file_write_itself_failing_does_not_raise(self, tmp_path, monkeypatch):
        """双重失败的边界情形：`run()` 炸了，且顶层异常处理里想把 `stage:
        "crash"` 写进 status.json 时，`status_file` 路径本身也不合法——
        `_write_status` 自己的 OSError 保护要能兜住这第二层失败，`main()`
        不能因此又抛出一个新异常盖过原始的 RuntimeError。"""
        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(run_backup, "run", boom)
        bogus_parent = tmp_path / "not_a_dir"
        bogus_parent.write_text("occupied")
        bogus_status_file = bogus_parent / "sub" / "status.json"

        rc = run_backup.main([
            "--src", str(tmp_path / "src"),
            "--backup-dir", str(tmp_path / "backup"),
            "--status-file", str(bogus_status_file),
            "--history-file", str(tmp_path / "history.jsonl"),
        ])
        assert rc not in (0, 1)
        assert not bogus_status_file.exists()


class TestHomeSandboxHasTeeth:
    """上面那层 `pytestmark` 沙箱本身要有牙：没有这两条，有人删掉它，全套照绿，
    而真实 `~/alpha-hive-data` 又开始被伪造记录污染（v0.45.291 实测那份文件里全是伪造、无一真实）。
    """

    def test_module_mark_is_in_effect_and_home_is_not_the_real_one(self, request):
        # 真实家目录取自用户库、不读 $HOME，所以不会被 monkeypatch 骗到。
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        assert "_sandbox_home" in request.fixturenames, "模块级 pytestmark 没生效"
        assert Path.home() != real_home, (
            f"Path.home() 仍是真实家目录 {real_home}——本文件里不传路径参数的 run_backup 调用"
            "会把伪造记录写进真实 ~/alpha-hive-data/logs/backup_status_history.jsonl")

    def test_run_backup_defaults_follow_home(self, tmp_path, monkeypatch, _sandbox_home):
        """沙箱靠「默认路径跟着 $HOME 走」这个前提成立；若 run_backup 哪天把默认值
        改成不看 HOME 的写法，这条会红，提醒沙箱已失效。"""
        def boom(*a, **kw):
            raise RuntimeError("模拟导出失败")

        monkeypatch.setattr(export_mod, "run_export", boom)
        src = tmp_path / "src"
        src.mkdir()
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        rc = run_backup.main(["--src", str(src), "--backup-dir", str(backup_dir)])  # 不传 status/history
        assert rc == 2
        logs = _sandbox_home / "alpha-hive-data" / "logs"
        rows = [json.loads(x) for x in (logs / "backup_status_history.jsonl").read_text().splitlines()]
        assert [(r["stage"], r["ok"]) for r in rows] == [("export", False)]
        assert (logs / "backup_status.json").is_file()


_ORCH = os.path.expanduser("~/.claude/scripts/alpha-hive-orchestrator.sh")
_STEP14_RC2_START = "elif [ $STEP14_RC -eq 2 ]; then"
_STEP14_RC2_END = "elif [ $STEP14_RC -eq 124 ]; then"


class TestOrchestratorStep14StageDispatch:
    """编排器 Step 14 的 rc==2 分支不受版本控制、pytest import 不到——抽出这段
    真实脚本片段，接进一个最小 bash 沙箱（假 `log()` 收日志、真 `jq` 判断）跑，
    锁定 stage 分发 + 新鲜度校验的行为。

    动机：二次检查这次修复时发现，v0.45.269 加的 stage 分发和 v0.45.273 加的
    新鲜度校验（`backup_status.json` 必须是"今天"写的才可信——`run_step()`
    自己也把 rc=2 当"脚本不存在，跳过"的哨兵值，跟这里的 rc=2 同一个数字、
    不同含义；陈旧文件会被误当成本轮结果）此前只有 CHANGELOG/memory 里的文字
    记录，没有任何可执行测试盯着——两处都可能被静默改回旧行为而没有测试报红。
    """

    def _extract_block(self):
        if not os.path.isfile(_ORCH):
            pytest.skip("编排器不在本机（仓库外文件）")
        text = Path(_ORCH).read_text(encoding="utf-8")
        start = text.index(_STEP14_RC2_START) + len(_STEP14_RC2_START)
        end = text.index(_STEP14_RC2_END, start)
        return text[start:end]

    def _run(self, tmp_path, *, status_json, date_str="2026-01-01"):
        block = self._extract_block()
        status_file = tmp_path / "backup_status.json"
        if status_json is not None:
            status_file.write_text(json.dumps(status_json), encoding="utf-8")
        script = f'''
set -uo pipefail
log() {{ printf 'LOG\\t%s\\t%s\\n' "$1" "$2"; }}
BACKUP_STATUS_JSON={shlex.quote(str(status_file))}
DATE_STR={shlex.quote(date_str)}
STEP14_RC=2
STEPS_RESULT='{{}}'
{block}
printf 'STEPS_RESULT_JSON\\t%s\\n' "$(echo "$STEPS_RESULT" | jq -c .)"
'''
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"bash 片段本身跑挂了：{result.stderr}"
        logs = [tuple(ln.split("\t", 2)[1:]) for ln in result.stdout.splitlines() if ln.startswith("LOG\t")]
        steps_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("STEPS_RESULT_JSON\t"))
        steps_result = json.loads(steps_line.split("\t", 1)[1])
        return logs, steps_result

    @pytest.mark.parametrize("stage, expect_level, expect_log_substr, expect_status", [
        ("init", "ERROR", "git 仓库初始化失败", "init_failed"),
        ("export", "ERROR", "数据导出失败", "export_failed"),
        ("git_error", "ERROR", "git 调用异常", "git_error_failed"),
        ("commit", "ERROR", "git commit 失败", "commit_failed"),
        ("push", "WARN", "已提交但推送失败", "push_failed"),
    ])
    def test_known_fresh_stage_dispatches_correct_message(
        self, tmp_path, stage, expect_level, expect_log_substr, expect_status
    ):
        logs, steps_result = self._run(
            tmp_path, status_json={"date": "2026-01-01", "stage": stage})
        assert any(lvl == expect_level and expect_log_substr in msg for lvl, msg in logs), logs
        assert steps_result["step14_data_backup"]["status"] == expect_status

    def test_unrecognized_but_fresh_stage_falls_to_default_branch(self, tmp_path):
        logs, steps_result = self._run(
            tmp_path, status_json={"date": "2026-01-01", "stage": "some_future_stage"})
        assert any("未识别" in msg for _, msg in logs), logs
        assert steps_result["step14_data_backup"]["status"] == "some_future_stage_failed"

    def test_missing_status_file_does_not_claim_a_specific_stage(self, tmp_path):
        """status.json 整个不存在（比如脚本本轮从没被 run_step 真正调用过）——
        不能落进 export/commit/push 任何一支，必须显式标"陈旧/缺失"。"""
        logs, steps_result = self._run(tmp_path, status_json=None)
        assert any("缺失或不是今天写的" in msg for _, msg in logs), logs
        assert steps_result["step14_data_backup"]["status"] == "stale_or_missing_failed"

    def test_stale_status_file_from_a_previous_day_is_not_trusted(self, tmp_path):
        """回归测试的核心：昨天的 status.json 恰好是 stage="push"——修复前，
        这种陈旧文件会被原样当成"已提交但推送失败"汇报（run_step 把脚本
        不存在的 rc=2 跟这里的 rc=2 混同的那个场景），这正是二次检查揪出、
        v0.45.273 补的新鲜度校验缺口。"""
        logs, steps_result = self._run(
            tmp_path, status_json={"date": "2025-12-31", "stage": "push"}, date_str="2026-01-01")
        assert not any("已提交但推送失败" in msg for _, msg in logs), (
            f"陈旧的 stage=push 被当成本轮结果汇报了——新鲜度校验没生效：{logs}")
        assert any("缺失或不是今天写的" in msg for _, msg in logs), logs
        assert steps_result["step14_data_backup"]["status"] == "stale_or_missing_failed"

    def test_status_file_without_date_field_is_treated_as_stale(self, tmp_path):
        """老格式的 status.json（v0.45.269 之前写的，没有 date 字段）也不能被信——
        `.date == $d` 对缺失字段该判 false，不能因为 jq 的 null 处理意外放行。"""
        logs, steps_result = self._run(tmp_path, status_json={"stage": "push"})
        assert steps_result["step14_data_backup"]["status"] == "stale_or_missing_failed"


_STEP14_RC1_START = "elif [ $STEP14_RC -eq 1 ]; then"
_STEP14_RC1_END = "elif [ $STEP14_RC -eq 2 ]; then"


class TestOrchestratorStep14Rc1Dispatch:
    """issue 4（二次检查 2026-09-21）：rc==1 本该专属 `stage=="secret_scan"`，
    但 `run_backup.py` 里任何未被顶层 try/except 捕获的崩溃（比如 import 期
    就出错）在 Python 里同样以默认退出码 1 退出，跟这里撞车。v0.45.307 给
    rc==1 分支加了跟 rc==2 分支同款的"新鲜度校验"（核对 status.json 是今天
    写的、stage 确实是 secret_scan），这里同 `TestOrchestratorStep14StageDispatch`
    的做法，抽出真实脚本片段接进最小 bash 沙箱跑，锁定这个新分支的行为。
    """

    def _extract_block(self):
        if not os.path.isfile(_ORCH):
            pytest.skip("编排器不在本机（仓库外文件）")
        text = Path(_ORCH).read_text(encoding="utf-8")
        start = text.index(_STEP14_RC1_START) + len(_STEP14_RC1_START)
        end = text.index(_STEP14_RC1_END, start)
        return text[start:end]

    def _run(self, tmp_path, *, status_json, date_str="2026-01-01"):
        block = self._extract_block()
        status_file = tmp_path / "backup_status.json"
        if status_json is not None:
            status_file.write_text(json.dumps(status_json), encoding="utf-8")
        script = f'''
set -uo pipefail
log() {{ printf 'LOG\\t%s\\t%s\\n' "$1" "$2"; }}
BACKUP_STATUS_JSON={shlex.quote(str(status_file))}
DATE_STR={shlex.quote(date_str)}
STEP14_RC=1
STEPS_RESULT='{{}}'
{block}
printf 'STEPS_RESULT_JSON\\t%s\\n' "$(echo "$STEPS_RESULT" | jq -c .)"
'''
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"bash 片段本身跑挂了：{result.stderr}"
        logs = [tuple(ln.split("\t", 2)[1:]) for ln in result.stdout.splitlines() if ln.startswith("LOG\t")]
        steps_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("STEPS_RESULT_JSON\t"))
        steps_result = json.loads(steps_line.split("\t", 1)[1])
        return logs, steps_result

    def test_fresh_secret_scan_stage_reports_blocked(self, tmp_path):
        """真的是今天写的、stage 确实是 secret_scan——这才是真正的"密钥扫描
        命中"，要维持原有的 ERROR 文案。"""
        logs, steps_result = self._run(
            tmp_path, status_json={"date": "2026-01-01", "stage": "secret_scan"})
        assert any(lvl == "ERROR" and "密钥扫描命中" in msg for lvl, msg in logs), logs
        assert steps_result["step14_data_backup"]["status"] == "secret_scan_blocked"

    def test_fresh_but_different_stage_is_not_reported_as_secret_scan(self, tmp_path):
        """回归核心：status.json 是今天写的，但 stage 不是 secret_scan（比如
        `main()` 顶层 try/except 落的 "crash"）——不能被当成密钥扫描命中，
        数据可能其实早已提交推送成功。"""
        logs, steps_result = self._run(
            tmp_path, status_json={"date": "2026-01-01", "stage": "crash"})
        # 精确匹配那条 ERROR 文案本身（"🚨...已拒绝提交"），不用宽松子串——
        # WARN 分支的"不能确认是真的密钥扫描命中"这句本身也含"密钥扫描命中"
        # 这个子串，宽松匹配会把 WARN 误判成命中。
        assert not any(lvl == "ERROR" and "已拒绝提交" in msg for lvl, msg in logs), (
            f"stage=crash 被误报成密钥扫描命中了：{logs}")
        assert any(lvl == "WARN" and "不能确认是真的密钥扫描命中" in msg for lvl, msg in logs), logs
        assert steps_result["step14_data_backup"]["status"] == "rc1_unverified_failed"

    def test_stale_status_file_is_not_trusted(self, tmp_path):
        """昨天的 status.json 恰好是 stage="secret_scan"——不能被当成本轮结果，
        同 rc==2 分支已有的新鲜度校验纪律。"""
        logs, steps_result = self._run(
            tmp_path, status_json={"date": "2025-12-31", "stage": "secret_scan"}, date_str="2026-01-01")
        assert not any(lvl == "ERROR" and "已拒绝提交" in msg for lvl, msg in logs), (
            f"陈旧的 stage=secret_scan 被当成本轮结果汇报了：{logs}")
        assert steps_result["step14_data_backup"]["status"] == "rc1_unverified_failed"

    def test_missing_status_file_is_not_trusted(self, tmp_path):
        """status.json 整个不存在（比如脚本在 import 期就崩了，压根没跑到
        写 status.json 那一步）——同样不能被当成密钥扫描命中。"""
        logs, steps_result = self._run(tmp_path, status_json=None)
        assert not any(lvl == "ERROR" and "已拒绝提交" in msg for lvl, msg in logs), logs
        assert steps_result["step14_data_backup"]["status"] == "rc1_unverified_failed"


_STEP15_START = "if [ $STEP15_RC -eq 0 ]; then"
_STEP15_END = 'log "INFO" "【Final】写入系统状态"'


class TestOrchestratorStep15Dispatch:
    """编排器 Step 15（数据备份连续性体检，v0.45.284）的 rc 分发同样不受版本
    控制、pytest import 不到——同 `TestOrchestratorStep14StageDispatch` 的做法，
    抽出真实脚本片段接进最小 bash 沙箱跑，锁定 exit code → 日志级别/文案 →
    `STEPS_RESULT` 状态字符串的映射，不然这条新加的分发逻辑可能被静默改坏
    而没有测试报红。
    """

    def _extract_block(self):
        if not os.path.isfile(_ORCH):
            pytest.skip("编排器不在本机（仓库外文件）")
        text = Path(_ORCH).read_text(encoding="utf-8")
        start = text.index(_STEP15_START)
        end = text.index(_STEP15_END, start)
        return text[start:end]

    def _run(self, tmp_path, *, step15_rc, continuity_json=None):
        block = self._extract_block()
        json_file = tmp_path / "backup_continuity.json"
        if continuity_json is not None:
            json_file.write_text(json.dumps(continuity_json), encoding="utf-8")
        script = f'''
set -uo pipefail
log() {{ printf 'LOG\\t%s\\t%s\\n' "$1" "$2"; }}
PYTHON3={shlex.quote(sys.executable)}
BACKUP_CONTINUITY_JSON={shlex.quote(str(json_file))}
STEP15_RC={step15_rc}
STEP15_DURATION=1
STEPS_RESULT='{{}}'
{block}
printf 'STEPS_RESULT_JSON\\t%s\\n' "$(echo "$STEPS_RESULT" | jq -c .)"
'''
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"bash 片段本身跑挂了：{result.stderr}"
        logs = [tuple(ln.split("\t", 2)[1:]) for ln in result.stdout.splitlines() if ln.startswith("LOG\t")]
        steps_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("STEPS_RESULT_JSON\t"))
        steps_result = json.loads(steps_line.split("\t", 1)[1])
        return logs, steps_result

    def test_healthy_rc0_dispatches_info(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=0)
        assert any(lvl == "INFO" and "连续性健康" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "healthy"

    def test_degraded_rc1_dispatches_warn_with_summary(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=1, continuity_json={
            "window": {"trading_days": 10}, "backed_up_days": 6, "coverage": 0.6,
            "longest_gap": 4, "weeks_missed": ["2026-W10"],
        })
        assert any(lvl == "WARN" and "连续性降级" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "degraded"

    def test_undetermined_rc3_dispatches_warn(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=3)
        assert any(lvl == "WARN" and "无法判定" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "undetermined"

    def test_skipped_rc2_dispatches_warn(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=2)
        assert any(lvl == "WARN" and "跳过" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "skipped"

    def test_timeout_rc124_dispatches_error(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=124)
        assert any(lvl == "ERROR" and "超时" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "timeout"

    def test_unexpected_rc_dispatches_warn_error_status(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=99)
        assert any(lvl == "WARN" and "异常" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "error"
        assert steps_result["step15_backup_continuity"]["rc"] == 99

    def test_degraded_summary_survives_unparseable_json(self, tmp_path):
        """连 continuity JSON 都读不出来（比如脚本半途被杀）不能让整段 Step 15
        崩掉——python 摘要脚本要能优雅报"无法解析"而不是让 bash 片段整体失败。"""
        logs, steps_result = self._run(tmp_path, step15_rc=1, continuity_json=None)
        assert steps_result["step15_backup_continuity"]["status"] == "degraded"
