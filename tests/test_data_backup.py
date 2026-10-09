"""数据根迁移 · 阶段 3 —— `data_backup/` 的回归覆盖。

只用**合成**数据（临时 sqlite 库 + 临时凭据文件），不碰生产 `pheromone.db`
也不含任何真实密钥字面量——真实密钥的变异演练是阶段 3 落地时用本机真实
密钥文件手动跑的一次性验收（见 CHANGELOG），不适合写进常跑的测试文件
（那样等于把「密钥去哪扫」的清单和一次真实凭据样本焊进了 git 历史）。
"""
import fnmatch
import glob
import json
import os
import pwd
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
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
from data_backup.sqlite_readonly import HotJournalError, db_open_uri, sha256_file


@pytest.fixture
def _sandbox_home(tmp_path_factory, monkeypatch):
    """`run_backup.main()` 的 `--status-file/--history-file` 默认值是
    `Path.home() / "alpha-hive-data" / ...`——真实数据根。`_isolate_env` 只隔离
    `ALPHA_HIVE_*`、不隔离 `$HOME`，所以没显式传路径的测试会把伪造记录写进真实
    `backup_status_history.jsonl`（v0.45.284 给 `run()` 加追加历史后，
    `TestRunBackupStageReporting` 的 7 个老测试没跟着传 `--history-file`）。
    `--backup-dir` 自 v0.45.414 起不跟 `$HOME`、跟 `PATHS.data_backup_repo`（`ALPHA_HIVE_HOME`），
    由 `_isolate_env` 隔离——见 `TestBackupRepoFollowsPaths`。
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    # 沙箱 HOME 同时隔掉了 `~/.gitconfig` —— 也就隔掉了提交身份。此时 `git commit`
    # 能否成功取决于**机器主机名**：git 退回 `用户@主机名` 推邮箱，Mac 的 `xxx.local`
    # 推得出，Linux 容器 / GitHub runner 的无域名主机名推不出 ⇒ "Author identity
    # unknown" ⇒ `run()` 停在 stage="commit"。于是同一条测试 Mac 绿、CI 红
    # （v0.45.345 的 TestPushTimeout 正是这样红了一路；它之前的同类测试各自手写
    # `git config user.email`，新测试忘了抄）。在夹具里一次给全，谁再写新测试都不用记。
    # commit.gpgsign=false：宿主全局配置若开了签名，沙箱里没有签名程序也不该让提交失败。
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Alpha Hive Test\n\temail = test@example.com\n"
        "[commit]\n\tgpgsign = false\n", encoding="utf-8")
    return home


# 整个文件套沙箱 HOME：新增的测试即使忘了传路径也不会写真实目录。
# 读编排器的那批读仓库 `scripts/` 里那份（`__file__` 锚点，v0.45.353），不经 HOME，所以不受影响。
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


def _git_repo_with_commit(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    # 去掉继承的 GIT_*：在 git 钩子里跑时 GIT_DIR / GIT_INDEX_FILE 会把 init/commit 引到真仓库
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, env=env, capture_output=True)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


class TestSourceGitHeadAfterDataRootSplit:
    """v0.45.322（阶段 5 前置）：`source_git_head` 取**代码**仓库的 HEAD，不是 `--src`（数据根）。

    迁移后 `--src` 是 `~/alpha-hive-data`，没有 `.git`。旧写法 `git -C <src_root>` 只读 stdout
    ⇒ 128 退出被吞成空串。第一条就是那个场景：旧实现在这里得到 ""，断言红。
    """

    def test_data_root_without_git_still_records_code_head(self, tmp_path, monkeypatch):
        head = _git_repo_with_commit(tmp_path / "code")
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(tmp_path / "code"))
        src_root = tmp_path / "data"          # 数据根：无 .git
        src_root.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src_root / db_name)
        manifest = export_mod.run_export(src_root, tmp_path / "out")
        assert manifest["source_git_head"] == head

    def test_missing_hive_logger_is_reported_not_raised(self, monkeypatch):
        """仓库根不在 sys.path 时延迟 import 失败——只作记录的字段不许把导出搞崩。"""
        import builtins
        real_import = builtins.__import__

        def no_hive_logger(name, *a, **k):
            if name == "hive_logger":
                raise ImportError("simulated: repo root not on sys.path")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_hive_logger)
        got = export_mod._code_git_head(None)
        assert got.startswith("unavailable:") and "simulated" in got, got

    def test_non_git_code_repo_is_reported_not_blank(self, tmp_path):
        not_repo = tmp_path / "not_a_repo"
        not_repo.mkdir()
        got = export_mod._code_git_head(not_repo)
        assert got.startswith("unavailable: git rc="), got
        assert got != ""


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


class TestForeignEntriesRefused:
    """备份仓里只许有本轮导出的东西（v0.45.427）。

    2026-10-03 测试夹具 `bk_src`（带 `.git` 的嵌套仓库）被搬进生产备份仓库，10-05 的备份 `git add -A` 把它**静默**封成
    gitlink 提交进了永久备份历史（`b43a5de`），四天没人红。红组照搬那两种形状（未跟踪的嵌套仓库 / 已成 gitlink），
    对照组钉住「首次备份全是未跟踪产物」「已跟踪的陈旧文件」不被误拦——误拦等于让生产备份天天失败。
    """
    _h = TestRunBackupStageReporting

    def _run(self, tmp_path, backup_dir):
        status_file = tmp_path / "status.json"
        src = tmp_path / "src"
        if not src.exists():                       # 同一条测试里备份两轮：数据源只建一次
            src = self._h._synthetic_src(None, tmp_path)
        rc = run_backup.main(["--src", str(src),
                              "--backup-dir", str(backup_dir), "--status-file", str(status_file)])
        return rc, json.loads(status_file.read_text())

    @staticmethod
    def _commits(backup_dir):
        r = subprocess.run(["git", "rev-list", "--all", "--count"], cwd=str(backup_dir), capture_output=True, text=True)
        return int(r.stdout.strip() or 0) if r.returncode == 0 else 0

    @staticmethod
    def _nested_repo(path):
        path.mkdir(parents=True)
        (path / "paper_portfolio_state").mkdir()
        (path / "paper_portfolio_state/meta.json").write_text('{"version": "test"}')
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, capture_output=True)
        subprocess.run([*g, "add", "-A"], cwd=str(path), check=True, capture_output=True)
        subprocess.run([*g, "commit", "-qm", "c0"], cwd=str(path), check=True, capture_output=True)

    def test_first_backup_with_only_exported_files_is_not_refused(self, tmp_path, monkeypatch):
        """对照：首次备份时工作区全是未跟踪的导出产物——不能被当成外来。"""
        _bypass_secret_scan(monkeypatch)
        backup_dir = tmp_path / "backup"
        self._h._init_backup_git_repo(None, backup_dir)
        rc, st = self._run(tmp_path, backup_dir)
        assert st["stage"] == "push" and st["commit"]["made"] is True, st      # 没配远端 ⇒ 停在 push，提交已成
        assert "foreign_entries" not in st

    def test_stale_tracked_file_not_in_this_export_is_not_refused(self, tmp_path, monkeypatch):
        """对照：已跟踪、本轮没导出的陈旧文件（导出不删数据根里已消失的根文件）早已进了备份历史，不算外来。"""
        _bypass_secret_scan(monkeypatch)
        backup_dir = tmp_path / "backup"
        self._h._init_backup_git_repo(None, backup_dir)
        self._run(tmp_path, backup_dir)
        (backup_dir / "alpha-hive-daily-2026-01-01.md").write_text("old report")
        subprocess.run(["git", "add", "-A"], cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "old"], cwd=str(backup_dir), check=True, capture_output=True)
        rc, st = self._run(tmp_path, backup_dir)
        assert st["stage"] == "push", st

    def test_untracked_foreign_file_is_refused_and_nothing_committed(self, tmp_path, monkeypatch):
        _bypass_secret_scan(monkeypatch)
        backup_dir = tmp_path / "backup"
        self._h._init_backup_git_repo(None, backup_dir)
        (backup_dir / "notes_from_someone.txt").write_text("x")
        rc, st = self._run(tmp_path, backup_dir)
        assert rc == 2 and st["stage"] == "foreign_entries" and st["ok"] is False, st
        assert st["foreign_entries"] == ["notes_from_someone.txt"] and st["foreign_count"] == 1, st
        assert self._commits(backup_dir) == 0, "拒绝了还提交了"

    def test_fixture_repo_moved_into_backup_is_refused(self, tmp_path, monkeypatch):
        """10-03 原形：带 `.git` 的夹具仓库被搬进备份仓（未跟踪的嵌套仓库）。"""
        _bypass_secret_scan(monkeypatch)
        backup_dir = tmp_path / "backup"
        self._h._init_backup_git_repo(None, backup_dir)
        self._nested_repo(tmp_path / "bk_src")
        shutil.move(str(tmp_path / "bk_src"), str(backup_dir))
        rc, st = self._run(tmp_path, backup_dir)
        assert st["stage"] == "foreign_entries" and "嵌套仓库:bk_src/" in st["foreign_entries"], st
        assert self._commits(backup_dir) == 0

    def test_already_sealed_gitlink_is_refused(self, tmp_path, monkeypatch):
        """10-05 之后生产备份仓的现状：嵌套仓库已被 `git add -A` 封成 gitlink（mode 160000）。后续每轮都要红，不能因为「已跟踪」就放过。"""
        _bypass_secret_scan(monkeypatch)
        backup_dir = tmp_path / "backup"
        self._h._init_backup_git_repo(None, backup_dir)
        self._nested_repo(backup_dir / "bk_src")
        subprocess.run(["git", "add", "-A"], cwd=str(backup_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "sealed"], cwd=str(backup_dir), check=True, capture_output=True)
        ls = subprocess.run(["git", "ls-files", "-s", "bk_src"], cwd=str(backup_dir), capture_output=True, text=True).stdout
        assert ls.startswith("160000 "), f"前提：git add -A 把嵌套仓库封成 gitlink：{ls!r}"
        rc, st = self._run(tmp_path, backup_dir)
        assert st["stage"] == "foreign_entries" and st["foreign_entries"] == ["嵌套仓库:bk_src/"], st
        assert self._commits(backup_dir) == 1, "只该有夹具那一个提交"

    def test_judgment_failure_is_refused_not_waved_through(self, tmp_path, monkeypatch):
        """判不了（git 出错 / SHA256SUMS 读不了）⇒ 同样不提交，不能当「没有外来条目」放行。"""
        _bypass_secret_scan(monkeypatch)

        def boom(_d):
            raise RuntimeError("git status 失败（模拟）")

        monkeypatch.setattr(run_backup, "foreign_entries", boom)
        backup_dir = tmp_path / "backup"
        self._h._init_backup_git_repo(None, backup_dir)
        rc, st = self._run(tmp_path, backup_dir)
        assert rc == 2 and st["stage"] == "foreign_entries" and "无法核对" in st["error"], st
        assert self._commits(backup_dir) == 0


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


def _forbid_side_effects(monkeypatch):
    """守卫若失效，`run()` 会往下走到 git init / 导出——这里一律变成可见的失败而不是真去做，
    `ALPHA_HIVE_HOME` 指到检出的用例里那等于在代码检出里动手。返回调用记录供断言「一次都没碰」。"""
    calls = []

    def no_git(args, cwd, timeout=None):
        calls.append(("git", args, str(cwd)))
        raise AssertionError(f"不该跑 git：{args} @ {cwd}")

    def no_export(*a, **k):
        calls.append(("export", a))
        raise AssertionError("不该导出")

    monkeypatch.setattr(run_backup, "_run_git", no_git)
    monkeypatch.setattr(export_mod, "run_export", no_export)
    return calls


class TestBackupRepoFollowsPaths:
    """v0.45.414：备份仓缺省位置的唯一真相是 `PATHS.data_backup_repo`，调用时求值。

    此前 `run_backup.main` / `export.main` 缺省值与编排器 Step 14 各写死一份
    `~/alpha-hive-data/_git_backup`。生产里 `ALPHA_HIVE_HOME=~/alpha-hive-data` ⇒ 新旧同值；
    `ALPHA_HIVE_HOME` 未设时 v0.45.422 起取 `$HOME/alpha-hive-data/_git_backup`（之前落进代码检出）；
    被显式设成代码检出（阶段 5 回退）⇒ 由「备份仓不许在代码仓库里」拦下。
    """

    def _capture_run(self, monkeypatch):
        seen = []

        def spy(src, backup_dir, *a, **k):
            seen.append(Path(backup_dir))
            return {"ok": True, "stage": "done"}

        monkeypatch.setattr(run_backup, "run", spy)
        return seen

    def test_run_backup_default_follows_alpha_hive_home_at_call_time(self, tmp_path, monkeypatch, _sandbox_home):
        seen = self._capture_run(monkeypatch)
        src = tmp_path / "src"
        src.mkdir()
        for home in (tmp_path / "data_a", tmp_path / "data_b"):   # 两次不同 env ⇒ 两个值：没被冻住
            monkeypatch.setenv("ALPHA_HIVE_HOME", str(home))
            assert run_backup.main(["--src", str(src)]) == 0
        assert seen == [tmp_path / "data_a" / "_git_backup", tmp_path / "data_b" / "_git_backup"]
        assert not any(p.is_relative_to(_sandbox_home) for p in seen), "缺省值还在跟 $HOME 走"

    def test_explicit_backup_dir_still_wins(self, tmp_path, monkeypatch):
        """合入后首个扫描日跑的是旧编排器（仍传 --backup-dir）+ 新 Python——必须照旧用传进来的。"""
        seen = self._capture_run(monkeypatch)
        assert run_backup.main(["--src", str(tmp_path), "--backup-dir", str(tmp_path / "explicit")]) == 0
        assert seen == [tmp_path / "explicit"]

    def test_explicit_empty_path_is_not_swapped_for_default(self, tmp_path, monkeypatch):
        """二次检查补：用真值判断选缺省，会把显式空串（调用方变量为空）悄悄换成缺省值——
        调用方的 bug 被改写成「没发生」。显式值一律原样用，与改动前同（`Path("")` 即 cwd，守卫照常判）。"""
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "data"))
        monkeypatch.chdir(tmp_path)   # 空串 = cwd：钉在 tmp，不让判定随起跑目录变
        seen = self._capture_run(monkeypatch)
        assert run_backup.main(["--src", str(tmp_path), "--backup-dir", ""]) == 0
        exported = []
        monkeypatch.setattr(export_mod, "run_export", lambda src, out: exported.append(Path(out)) or {"duration_seconds": 0})
        monkeypatch.setattr(export_mod, "write_manifest_and_sums", lambda out, m: None)
        assert export_mod.main(["--src", str(tmp_path), "--out", ""]) == 0
        assert seen == exported == [Path("")]

    def test_export_main_default_follows_alpha_hive_home(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(export_mod, "run_export", lambda src, out: seen.append(Path(out)) or {"duration_seconds": 0})
        monkeypatch.setattr(export_mod, "write_manifest_and_sums", lambda out, m: None)
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path / "data"))
        assert export_mod.main(["--src", str(tmp_path)]) == 0
        assert seen == [tmp_path / "data" / "_git_backup"]

    @pytest.mark.parametrize("rel", ["_git_backup", "."])
    def test_run_refuses_backup_repo_inside_code_repo(self, tmp_path, monkeypatch, rel):
        code = tmp_path / "code"
        code.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(code))
        backup_dir = (code / rel)
        backup_dir.mkdir(exist_ok=True)
        calls = _forbid_side_effects(monkeypatch)
        status = run_backup.run(tmp_path / "src", backup_dir, status_file=tmp_path / "s.json",
                                history_file=tmp_path / "h.jsonl")
        assert (status["stage"], status["ok"], status.get("refused")) == ("init", False, "inside_code_repo")
        assert calls == []
        assert not (backup_dir / ".git").exists()
        assert json.loads((tmp_path / "s.json").read_text())["refused"] == "inside_code_repo"

    def test_sibling_of_code_repo_is_not_refused(self, tmp_path, monkeypatch):
        """前缀相同不算「在里面」：`/x/code_backup` 不在 `/x/code` 里（字符串前缀比较会误伤）。"""
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(tmp_path / "code"))
        assert export_mod.backup_repo_inside_code_repo(tmp_path / "code_backup") is None
        assert export_mod.backup_repo_inside_code_repo(tmp_path / "code" / "x") == (tmp_path / "code").resolve()

    @pytest.mark.parametrize("alias", ["case", "firmlink"])
    def test_same_directory_spelled_differently_is_refused(self, tmp_path, monkeypatch, alias):
        """二次检查补：`resolve()` 不折叠大小写（APFS）与固件链接（`/System/Volumes/Data/…`），
        字符串判定会放行同一目录的另一种写法。按 inode 判。不 skip：别名在这台机器上不存在
        （大小写敏感的 FS / 非 macOS）时它就是另一个目录，断言「不拒绝」同样是真断言。"""
        code = (tmp_path / "code")
        code.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(code))
        spelled = Path(str(code).swapcase()) if alias == "case" else Path("/System/Volumes/Data" + str(code.resolve()))
        same_dir = spelled.exists() and os.path.samefile(spelled, code)
        got = export_mod.backup_repo_inside_code_repo(spelled / "_git_backup")
        assert got == (code.resolve() if same_dir else None), (spelled, same_dir, got)

    def test_unset_alpha_hive_home_defaults_to_home_data_root(self, tmp_path, monkeypatch, _sandbox_home):
        """用户交互 shell 没设 `ALPHA_HIVE_HOME`（2026-10-03 实测）：v0.45.422 起缺省 = `$HOME/alpha-hive-data/_git_backup`，
        与编排器 export 的生产值同址（本文件 HOME 已是沙箱）。此前这里解析到 `<代码检出>/_git_backup`。"""
        monkeypatch.delenv("ALPHA_HIVE_HOME", raising=False)
        seen = self._capture_run(monkeypatch)
        assert run_backup.main(["--src", str(tmp_path)]) == 0
        assert seen == [_sandbox_home / "alpha-hive-data" / "_git_backup"]
        assert export_mod.backup_repo_inside_code_repo(seen[0]) is None

    def test_alpha_hive_home_pointing_at_checkout_is_refused_end_to_end(self, tmp_path, monkeypatch):
        """阶段 5 回退的配置（`ALPHA_HIVE_HOME` 设成代码检出、不传 --backup-dir）：缺省解析进检出 ⇒
        CLI 全链路拒绝，rc=2、stage=init。检出用 tmp 替身（`ALPHA_HIVE_GIT_REPO`），git / 导出都是「一调就炸」的桩。"""
        code = tmp_path / "code"
        code.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(code))
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(code))
        default = export_mod.default_backup_repo()
        assert export_mod.backup_repo_inside_code_repo(default) is not None, default
        existed_before = default.exists()   # 比前后，不断言「不存在」：检出里若本有残留，不该算守卫的账
        calls = _forbid_side_effects(monkeypatch)
        status_file = tmp_path / "s.json"
        rc = run_backup.main(["--src", str(tmp_path), "--status-file", str(status_file),
                              "--history-file", str(tmp_path / "h.jsonl")])
        status = json.loads(status_file.read_text())
        assert (rc, status["stage"], status.get("refused")) == (2, "init", "inside_code_repo"), status
        assert calls == []
        assert default.exists() == existed_before, f"代码检出里被造出了 {default}"

    def test_export_main_refuses_inside_code_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_HIVE_GIT_REPO", str(tmp_path / "code"))
        calls = _forbid_side_effects(monkeypatch)
        assert export_mod.main(["--src", str(tmp_path), "--out", str(tmp_path / "code" / "_git_backup")]) == 2
        assert calls == []
        assert not (tmp_path / "code").exists()

    def test_orchestrator_does_not_spell_the_backup_repo(self):
        """位置只在 PATHS：编排器的可执行行里不许再出现 `_git_backup` / `--backup-dir`（注释不算）。"""
        live = [ln for ln in repo_orchestrator_text().splitlines() if not ln.lstrip().startswith("#")]
        assert [ln for ln in live if "_git_backup" in ln or "--backup-dir" in ln] == []
        assert any("run_data_backup.py" in ln for ln in live), "Step 14 的调用不见了——上面那条会空转变绿"


from tests._orchestrator import extract_function, repo_orchestrator_text  # 仓库里那份（v0.45.353）
_STEP14_RC2_START = "elif [ $STEP14_RC -eq 2 ]; then"
_STEP14_RC2_END = "elif [ $STEP14_RC -eq 124 ]; then"


class TestOrchestratorStep14StageDispatch:
    """编排器 Step 14 的 rc==2 分支是 bash、pytest import 不到——抽出这段
    真实脚本片段，接进一个最小 bash 沙箱（假 `log()` 收日志、真 `jq` 判断）跑，
    锁定 stage 分发 + 新鲜度校验的行为。

    动机：二次检查这次修复时发现，v0.45.269 加的 stage 分发和 v0.45.273 加的
    新鲜度校验（`backup_status.json` 必须是"今天"写的才可信——`run_step()`
    自己也把 rc=2 当"脚本不存在，跳过"的哨兵值，跟这里的 rc=2 同一个数字、
    不同含义；陈旧文件会被误当成本轮结果）此前只有 CHANGELOG/memory 里的文字
    记录，没有任何可执行测试盯着——两处都可能被静默改回旧行为而没有测试报红。
    """

    def _extract_block(self):
        text = repo_orchestrator_text()
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
        ("foreign_entries", "ERROR", "备份仓里有导出之外的条目", "foreign_entries_failed"),
        ("git_error", "ERROR", "git 调用异常", "git_error_failed"),
        ("commit", "ERROR", "git commit 失败", "commit_failed"),
        ("push", "WARN", "已提交但推送失败", "push_failed"),
        ("size_budget", "ERROR", "备份仓体量超出预算", "size_budget_failed"),
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
        text = repo_orchestrator_text()
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


_STEP15_START = 'log "INFO" "【Step 15】'
_STEP15_END = 'log "INFO" "【Final】写入系统状态"'


class TestOrchestratorStep15Dispatch:
    """编排器 Step 15（数据备份连续性体检，v0.45.284）的 rc 分发——抽出真实脚本片段接进最小 bash 沙箱跑，
    锁定 exit code → 日志级别/文案 → `STEPS_RESULT` 状态字符串的映射。

    B（v0.45.385）起这段不再是内联分支链：`run_step` 之后一行 `_apply_step_interp`，判定在
    `orchestrator_steps.py`（`_base15`）。所以这里贴上原文的两个 helper 与全局量，`run_step` 换成替身：
    跑 `orchestrator_steps.py` 时转给真解释器；跑 `backup_continuity.py` 时记下参数、把夹具拷到 `--out`
    （拷贝发生在段内 `STEP15_START` 之后 ⇒ mtime 是本轮的）、返回 `FAKE_RC`。
    日期按运行时的今天算（不写死）。
    """

    def _extract_block(self):
        text = repo_orchestrator_text()
        start = text.index(_STEP15_START)
        end = text.index(_STEP15_END, start)
        return text, text[start:end]

    def _run(self, tmp_path, *, step15_rc, continuity_json=None, leftover=None):
        text, block = self._extract_block()
        helpers = "\n".join([extract_function(text, "_step_rc_fallback"), extract_function(text, "_apply_step_interp"),
                             *re.findall(r'^(?:STEP_INTERP_TIMEOUT=\d+|_SI_STATUS="")$', text, re.M)])
        fixture = tmp_path / "fixture.json"
        if continuity_json is not None:
            fixture.write_text(json.dumps(continuity_json), encoding="utf-8")
        logdir = tmp_path / "logs"
        logdir.mkdir()
        today = date.today().isoformat()
        out_json = logdir / f"backup_continuity-{today}.json"
        if leftover is not None:                       # 上一次（同一 DATE_STR）留下的文件
            out_json.write_text(leftover, encoding="utf-8")
        args_file = tmp_path / "tool_args.txt"
        repo_root = Path(__file__).resolve().parent.parent      # 指向代码（真解释器），故用 __file__
        script = f'''
set -uo pipefail
log() {{ printf 'LOG\\t%s\\t%s\\n' "$1" "$2"; }}
PYTHON3={shlex.quote(sys.executable)}
PROJECT_DIR={shlex.quote(str(repo_root))}
DATE_STR={shlex.quote(today)}
LOGDIR={shlex.quote(str(logdir))}
LOGFILE={shlex.quote(str(logdir / "orchestrator.log"))}
BACKUP_HISTORY_JSONL={shlex.quote(str(tmp_path / "history.jsonl"))}
FAKE_RC={step15_rc}
FAKE_JSON={shlex.quote(str(fixture) if continuity_json is not None else "")}
ARGS_FILE={shlex.quote(str(args_file))}
STEPS_RESULT='{{}}'
{helpers}
run_step() {{
    if [ "$1" = "--timeout" ]; then shift 2; fi
    local script="$1" prev="" out="" a
    shift
    if [ "$(basename "$script")" = "orchestrator_steps.py" ]; then "$PYTHON3" "$script" "$@"; return $?; fi
    printf '%s\\n' "$@" > "$ARGS_FILE"
    for a in "$@"; do [ "$prev" = "--out" ] && out="$a"; prev="$a"; done
    if [ -n "$FAKE_JSON" ] && [ -n "$out" ]; then cp "$FAKE_JSON" "$out"; fi
    return "$FAKE_RC"
}}
{block}
printf 'STEPS_RESULT_JSON\\t%s\\n' "$(echo "$STEPS_RESULT" | jq -c .)"
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env,
                                cwd=str(tmp_path))
        assert result.returncode == 0, f"bash 片段本身跑挂了：{result.stderr}"
        logs = [tuple(ln.split("\t", 2)[1:]) for ln in result.stdout.splitlines() if ln.startswith("LOG\t")]
        steps_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("STEPS_RESULT_JSON\t"))
        steps_result = json.loads(steps_line.split("\t", 1)[1])
        self.tool_args = args_file.read_text(encoding="utf-8").splitlines() if args_file.exists() else None
        self.out_json, self.today = out_json, today
        return logs, steps_result

    _HEALTHY = {"window": {"trading_days": 30}, "backed_up_days": 30, "coverage": 1.0, "longest_gap": 0,
                "weeks_missed": [], "healthy": True}

    def test_healthy_rc0_dispatches_info(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=0, continuity_json=self._HEALTHY)
        assert any(lvl == "INFO" and "连续性健康" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "healthy"

    def test_degraded_rc1_dispatches_warn_with_summary(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=1, continuity_json={
            "window": {"trading_days": 10}, "backed_up_days": 6, "coverage": 0.6,
            "longest_gap": 4, "weeks_missed": ["2026-W10"],
        })
        assert any(lvl == "WARN" and "连续性降级" in msg and "只成功了 6 次" in msg for lvl, msg in logs), logs
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
        assert any(lvl == "ERROR" and "超时（>60s）" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "timeout"

    def test_unexpected_rc_dispatches_warn_error_status(self, tmp_path):
        logs, steps_result = self._run(tmp_path, step15_rc=99)
        assert any(lvl == "WARN" and "异常" in msg for lvl, msg in logs), logs
        assert steps_result["step15_backup_continuity"]["status"] == "error"
        assert steps_result["step15_backup_continuity"]["rc"] == 99

    def test_degraded_summary_survives_unparseable_json(self, tmp_path):
        """退出码 1 却没有 JSON（比如脚本半途被杀 / 崩在写盘前）：整段不许崩。B 起这是**刻意的改动**
        （orchestrator_steps docstring (f)）：旧代码的未捕获异常退出码也是 1，「降级」与「崩了」分不开，
        不再按正常的 degraded 记，而是 missing_json（原判定进 rc_status）+ 一行 ERROR。"""
        logs, steps_result = self._run(tmp_path, step15_rc=1, continuity_json=None)
        f = steps_result["step15_backup_continuity"]
        assert f["status"] == "missing_json" and f["rc_status"] == "degraded", f
        assert any(lvl == "ERROR" for lvl, _ in logs), logs

    def test_tool_gets_date_history_and_out(self, tmp_path):
        """B：日期显式传 DATE_STR（跨午夜时工具按时钟取的日期会与本轮错开）。
        变异：删 `--end "${DATE_STR}"` ⇒ 红。"""
        self._run(tmp_path, step15_rc=0, continuity_json=self._HEALTHY)
        a = self.tool_args
        assert a is not None and a[a.index("--end") + 1] == self.today, a
        assert a[a.index("--history") + 1] == str(tmp_path / "history.jsonl"), a
        assert a[a.index("--out") + 1] == str(self.out_json), a

    def test_leftover_json_from_an_earlier_run_is_removed_first(self, tmp_path):
        """同一 DATE_STR 重跑、工具这次没写出 JSON：上一次留下的文件不许冒充本轮。
        变异：删 `rm -f "${BACKUP_CONTINUITY_JSON}"` ⇒ 读到遗留文件、状态不再是 missing_json 红。"""
        logs, steps_result = self._run(tmp_path, step15_rc=1, leftover=json.dumps(self._HEALTHY))
        assert not self.out_json.exists()
        assert steps_result["step15_backup_continuity"]["status"] == "missing_json"


class TestExportScopeCoversMoveRules:
    """v0.45.342：数据仓库备份范围 × 阶段 5 迁移分类表，两份表不许各自漂移。

    v0.45.342 之前 `EXCLUDED_FROM_THIS_PASS` 以「已被代码仓库 git 跟踪并推送」为由排除了
    `report_snapshots/` 与报告文件；阶段 5 让代码检出里那份冻结后，这个理由失效，却没有任何东西变红
    ⇒ 新快照零异地副本。现在要求：MOVE 规则每一项要么被导出覆盖、要么写明排除理由；反向也不许有过期条目。
    """

    @staticmethod
    def _tables():
        from data_backup import migrate_data_root as mig
        move = set(mig.MOVE_DBS) | set(mig.MOVE_DIRS) | set(mig.MOVE_GLOBS) | set(mig.MOVE_EXACT)
        covered = set(export_mod.DBS.values()) | set(export_mod.STATE_DIRS) | set(export_mod.ROOT_FILE_GLOBS)
        excluded = set(export_mod.EXCLUDED_FROM_THIS_PASS)
        return move, covered, excluded

    def test_every_move_rule_is_backed_up_or_explicitly_excluded(self):
        move, covered, excluded = self._tables()
        missing = sorted(move - covered - excluded)
        assert not missing, f"这些数据项搬到了数据根，却既不备份也没写排除理由：{missing}"

    def test_no_stale_or_contradictory_entries(self):
        move, covered, excluded = self._tables()
        assert not (covered & excluded), f"同时出现在备份范围与排除表：{sorted(covered & excluded)}"
        assert not (excluded - move), f"排除表里有迁移分类表不认识的过期条目：{sorted(excluded - move)}"
        assert not (covered - move), f"备份范围里有迁移分类表不认识的条目：{sorted(covered - move)}"

    def test_every_exclusion_has_a_reason(self):
        assert all(isinstance(v, str) and v.strip() for v in export_mod.EXCLUDED_FROM_THIS_PASS.values())

    def test_every_exclusion_reason_is_classified(self):
        """v0.45.417：理由必须以 rebuildable: / derived: / accepted-loss: 开头，且 accepted-loss 要有
        「日期 + 谁决定」。曾经两个不可重取的文件族挂着「用户未定」，没有任何东西让它过期。"""
        import re
        bad = {k: v for k, v in export_mod.EXCLUDED_FROM_THIS_PASS.items()
               if not v.startswith(export_mod.EXCLUSION_REASON_PREFIXES)}
        assert not bad, f"排除理由没有分类前缀（{export_mod.EXCLUSION_REASON_PREFIXES}）：{sorted(bad)}"
        for k, v in export_mod.EXCLUDED_FROM_THIS_PASS.items():
            if v.startswith("accepted-loss:"):
                assert re.match(r"accepted-loss:\d{4}-\d{2}-\d{2} \S", v), f"{k}: accepted-loss 缺日期 / 决定人：{v!r}"
            assert "未定" not in v.replace("用户未单独审议", ""), f"{k}: 「未定」不是一个决定：{v!r}"

    def test_classifier_has_teeth(self):
        """正对照：旧写法（「用户未定」、无前缀、accepted-loss 无日期）喂给同一判据必须被抓。"""
        import re
        pre = export_mod.EXCLUSION_REASON_PREFIXES
        assert not "体量大，是否进数据仓库**用户未定**".startswith(pre)
        assert not "可重建缓存".startswith(pre)
        assert not re.match(r"accepted-loss:\d{4}-\d{2}-\d{2} \S", "accepted-loss: 用户定")

    def test_swarm_results_are_backed_up_and_analysis_is_a_recorded_loss(self):
        assert ".swarm_results_*.json" in export_mod.ROOT_FILE_GLOBS
        assert ".swarm_results_*.json" not in export_mod.EXCLUDED_FROM_THIS_PASS
        assert export_mod.EXCLUDED_FROM_THIS_PASS["analysis-*-ml-*.json"].startswith("accepted-loss:2026-10-05")

    def test_swarm_results_actually_exported_and_analysis_is_not(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        (src / ".swarm_results_2026-10-05.json").write_text('{"AAA": {"score": 1}}')
        (src / "analysis-AAA-ml-2026-10-05.json").write_text('{"x": 1}')
        out = tmp_path / "out"
        manifest = export_mod.run_export(src, out, code_repo=tmp_path)
        assert (out / ".swarm_results_2026-10-05.json").read_text() == '{"AAA": {"score": 1}}'
        assert not (out / "analysis-AAA-ml-2026-10-05.json").exists()
        assert ".swarm_results_2026-10-05.json" in {r["rel"] for r in manifest["root_files"]}

    def test_report_snapshots_and_reports_are_actually_exported(self, tmp_path):
        """正面走一遍 run_export：v0.45.342 新纳入的两类真的进了产物，而不只是出现在常量里。"""
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        (src / "report_snapshots").mkdir()
        (src / "report_snapshots" / "AAA_2026-09-24.json").write_text('{"ticker": "AAA"}')
        (src / "alpha-hive-daily-2026-09-24.md").write_text("# daily")
        (src / "alpha-hive-AAA-ml-enhanced-2026-09-24.html").write_text("<html></html>")
        out = tmp_path / "out"
        manifest = export_mod.run_export(src, out, code_repo=tmp_path)
        assert (out / "report_snapshots" / "AAA_2026-09-24.json").exists()
        assert (out / "alpha-hive-daily-2026-09-24.md").exists()
        assert (out / "alpha-hive-AAA-ml-enhanced-2026-09-24.html").exists()
        rels = {r["rel"] for r in manifest["root_files"]}
        assert {"alpha-hive-daily-2026-09-24.md", "alpha-hive-AAA-ml-enhanced-2026-09-24.html"} <= rels


class TestPushTimeout:
    """v0.45.345：push 单独放宽超时（09-24 扩大备份范围后首推超 60s 被判 git_error）。"""

    def test_push_gets_longer_timeout_than_local_git_but_fits_step14_budget(self):
        assert run_backup.GIT_PUSH_TIMEOUT_S >= 180
        assert run_backup.GIT_PUSH_TIMEOUT_S < 300, "必须留在编排器 Step 14 的 run_step --timeout 300 之内"
        assert run_backup.GIT_TIMEOUT_S < run_backup.GIT_PUSH_TIMEOUT_S

    def test_push_call_actually_passes_the_push_timeout(self, tmp_path, monkeypatch, _sandbox_home):
        _bypass_secret_scan(monkeypatch)   # 本类只测超时参数；密钥扫描另有专门的测试类
        seen = []
        real = run_backup._run_git

        def spy(args, cwd, timeout=run_backup.GIT_TIMEOUT_S):
            seen.append((args[0], timeout))
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 0, "", "")
            return real(args, cwd, timeout=timeout)

        monkeypatch.setattr(run_backup, "_run_git", spy)
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        (tmp_path / "bk").mkdir()
        st = run_backup.run(src, tmp_path / "bk", status_file=tmp_path / "s.json",
                            history_file=tmp_path / "h.jsonl")
        assert st["stage"] == "done", st
        pushes = [t for a, t in seen if a == "push"]
        assert pushes == [run_backup.GIT_PUSH_TIMEOUT_S], seen
        assert all(t == run_backup.GIT_TIMEOUT_S for a, t in seen if a != "push")


class TestBackupRepoSizeBudget:
    """v0.45.417：备份仓体量预算闸（600 MB）。数据已推送，超预算 / 量不出来把这一轮标红。"""

    @staticmethod
    def _src(tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        (tmp_path / "bk").mkdir()
        return src

    @staticmethod
    def _fake_push(monkeypatch):
        real = run_backup._run_git

        def spy(args, cwd, timeout=run_backup.GIT_TIMEOUT_S):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 0, "", "")
            return real(args, cwd, timeout=timeout)
        monkeypatch.setattr(run_backup, "_run_git", spy)

    def test_budget_is_600(self):
        assert run_backup.BACKUP_REPO_BUDGET_MB == 600

    def test_size_counts_loose_objects_not_only_the_pack(self, tmp_path):
        """本仓从未 gc——只读 size-pack 会把松散对象的体量报成 0。"""
        repo = tmp_path / "r"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        (repo / "f.bin").write_bytes(os.urandom(300_000))
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                       cwd=str(repo), check=True, capture_output=True)
        packs = subprocess.run(["git", "count-objects", "-v"], cwd=str(repo), capture_output=True, text=True).stdout
        assert "size-pack: 0" in packs, "夹具前提：对象应是松散的"
        assert run_backup.repo_size_mb(repo) >= 0.25

    def test_within_budget_is_green_and_reports_size(self, tmp_path, monkeypatch):
        _bypass_secret_scan(monkeypatch)
        self._fake_push(monkeypatch)
        st = run_backup.run(self._src(tmp_path), tmp_path / "bk", status_file=tmp_path / "s.json",
                            history_file=tmp_path / "h.jsonl")
        assert st["stage"] == "done" and st["ok"] is True, st
        assert st["repo_size_mb"] > 0 and st["repo_budget_mb"] == 600 and st["pushed"] is True

    def test_over_budget_is_red_but_data_is_pushed(self, tmp_path, monkeypatch):
        _bypass_secret_scan(monkeypatch)
        self._fake_push(monkeypatch)
        monkeypatch.setattr(run_backup, "BACKUP_REPO_BUDGET_MB", 0)
        st = run_backup.run(self._src(tmp_path), tmp_path / "bk", status_file=tmp_path / "s.json",
                            history_file=tmp_path / "h.jsonl")
        assert st["ok"] is False and st["stage"] == "size_budget", st
        assert st["pushed"] is True and "超出预算" in st["error"]
        assert json.loads((tmp_path / "s.json").read_text())["stage"] == "size_budget"

    def test_unmeasurable_size_is_red_not_zero(self, tmp_path, monkeypatch):
        _bypass_secret_scan(monkeypatch)
        self._fake_push(monkeypatch)

        def boom(_):
            raise RuntimeError("count-objects 坏了")
        monkeypatch.setattr(run_backup, "repo_size_mb", boom)
        st = run_backup.run(self._src(tmp_path), tmp_path / "bk", status_file=tmp_path / "s.json",
                            history_file=tmp_path / "h.jsonl")
        assert st["ok"] is False and st["stage"] == "size_budget" and "量不出来" in st["error"], st
        assert "repo_size_mb" not in st

    def test_failed_push_never_reaches_the_budget_gate(self, tmp_path, monkeypatch):
        _bypass_secret_scan(monkeypatch)
        real = run_backup._run_git

        def spy(args, cwd, timeout=run_backup.GIT_TIMEOUT_S):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "denied")
            return real(args, cwd, timeout=timeout)
        monkeypatch.setattr(run_backup, "_run_git", spy)
        st = run_backup.run(self._src(tmp_path), tmp_path / "bk", status_file=tmp_path / "s.json",
                            history_file=tmp_path / "h.jsonl")
        assert st["stage"] == "push" and "pushed" not in st


# ── v0.45.430：网站 PWA `manifest.json` 与备份元数据 `MANIFEST.json` 在 APFS 上是同一个目录项 ─────────
# 09-24（v0.45.342 把 `manifest.json` 纳入备份）起：先拷进来的网站 manifest 被随后写的元数据覆盖，
# `SHA256SUMS` 照列网站 manifest 的真实哈希，索引里只有 `MANIFEST.json`。没有任何东西红过。

_PWA_MANIFEST = '{"name": "Alpha Hive 投资仪表板", "short_name": "Alpha Hive", "icons": []}\n'


def _fs_is_case_insensitive(d: Path) -> bool:
    probe = d / "_case_probe_a"
    probe.write_text("")
    try:
        return "_case_probe_a" in os.listdir(d) and (d / "_CASE_PROBE_A").exists()
    finally:
        probe.unlink()


def _exact_exists(root: Path, rel: str) -> bool:
    """逐级按**精确拼写**找目录项——大小写不敏感的盘上 `Path.exists()` 对另一种拼写也返回 True。"""
    cur = root
    for part in Path(rel).parts:
        if part not in os.listdir(cur):
            return False
        cur = cur / part
    return True


def _src_with_full_scope(tmp_path: Path) -> Path:
    """数据根：每个库、每个状态目录各一个文件、每条根文件模式各实例化一个（内容互不相同）。"""
    src = tmp_path / "src"
    src.mkdir()
    for db_name in export_mod.DBS.values():
        _make_synthetic_db(src / db_name)
    for d in export_mod.STATE_DIRS:
        (src / d).mkdir()
        (src / d / "s.json").write_text(json.dumps({"dir": d}))
    for pat in export_mod.ROOT_FILE_GLOBS:
        name = pat.replace("*", "2026-10-08")
        (src / name).write_text(_PWA_MANIFEST if name == "manifest.json" else f"content of {name}\n")
    return src


class TestNoCaseCollisionsInBackup:
    """导出产物里任何两个路径（含元数据文件）在大小写 / 规范化不敏感的盘上都不许落进同一个目录项。"""

    def test_case_collisions_finds_files_directories_and_normalization_variants(self):
        cc = export_mod.case_collisions
        assert cc(["manifest.json", "MANIFEST.json"]) == [["MANIFEST.json", "manifest.json"]]
        assert cc(["Reports/a.html", "reports/b.html"]) == [["Reports", "reports"]], "目录分量也要比"
        nfc, nfd = "café.json", "café.json"
        assert cc([nfc, nfd]) == [sorted([nfc, nfd])]
        assert cc(["a.json", "a.json", "b/a.json"]) == [], "一字不差的重复（两条 glob 命中同一文件）不算碰撞"

    @staticmethod
    def _scope_collisions(meta_files) -> list:
        """声明的导出范围里会撞的组合。导出自己生成的名字（`db_exports/` + 元数据）与从数据根拷来的名字
        连一字不差也不许相同（那是覆盖）；拷来的名字之间只算拼写不同的（一字不差 = 同一个源文件）。"""
        fold = export_mod._fold
        generated = ["db_exports", *meta_files]
        copied = [*export_mod.STATE_DIRS, *(p for p in export_mod.ROOT_FILE_GLOBS if not glob.has_magic(p))]
        patterns = [p for p in export_mod.ROOT_FILE_GLOBS if glob.has_magic(p)]
        bad = export_mod.case_collisions(generated + copied)
        bad += [[g, c] for g in generated for c in copied if g == c]
        bad += [[g, p] for g in generated for p in patterns if fnmatch.fnmatchcase(fold(g), fold(p))]
        bad += [[c, p] for c in copied for p in patterns
                if fnmatch.fnmatchcase(fold(c), fold(p)) and not fnmatch.fnmatchcase(c, p)]
        return bad

    def test_declared_scope_has_no_case_collisions(self):
        assert self._scope_collisions(export_mod.META_FILES) == []

    def test_scope_check_has_teeth(self):
        """正对照：v0.45.430 前的元数据名喂给同一判据必须红——正是 09-24 起每天发生的那次覆盖。"""
        old = (export_mod.SUMS_NAME, export_mod.LEGACY_MANIFEST_NAME)
        assert ["MANIFEST.json", "manifest.json"] in self._scope_collisions(old)
        assert self._scope_collisions((export_mod.SUMS_NAME, "Report_RAW.json")), "被带通配符的根文件模式命中也要红"
        assert self._scope_collisions((export_mod.SUMS_NAME, "Reports")), "与状态目录同名（不同大小写）也要红"

    def test_full_scope_export_sums_are_true_on_disk(self, tmp_path):
        """端到端：每条 SHA256SUMS 都以精确拼写存在、且哈希相符。修复前在 APFS 上 `manifest.json`
        这一条两样都不成立（另一种拼写、另一份内容）；大小写敏感的盘上由上面的静态检查兜底。"""
        src = _src_with_full_scope(tmp_path)
        out = tmp_path / "out"
        manifest = export_mod.run_export(src, out, code_repo=tmp_path)
        export_mod.write_manifest_and_sums(out, manifest)

        lies = []
        for line in (out / export_mod.SUMS_NAME).read_text(encoding="utf-8").splitlines():
            h, rel = line.split("  ", 1)
            if not _exact_exists(out, rel) or sha256_file(out / rel) != h:
                lies.append(rel)
        assert lies == [], f"SHA256SUMS 声称持有、磁盘上却不是那个拼写 / 那份内容：{lies}"
        assert (out / "manifest.json").read_text(encoding="utf-8") == _PWA_MANIFEST
        assert all(_exact_exists(out, m) for m in export_mod.META_FILES)
        assert json.loads((out / export_mod.MANIFEST_NAME).read_text(encoding="utf-8"))["root_files"]

    def test_write_refuses_and_writes_nothing_on_collision(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        m = {"databases": {}, "state_dirs": {},
             "root_files": [{"rel": "Foo.json", "sha256": "a"}, {"rel": "foo.json", "sha256": "b"}]}
        with pytest.raises(RuntimeError, match="互相覆盖"):
            export_mod.write_manifest_and_sums(out, m)
        assert os.listdir(out) == [], "撞名时一个元数据文件都不许写"

    def test_runtime_check_catches_the_original_bug(self, tmp_path, monkeypatch):
        """变异：元数据名改回旧名 ⇒ 写清单前就抛（生产里落 stage="export"、不提交），而不是静默覆盖。"""
        monkeypatch.setattr(export_mod, "META_FILES", (export_mod.SUMS_NAME, export_mod.LEGACY_MANIFEST_NAME))
        out = tmp_path / "out"
        out.mkdir()
        m = {"databases": {}, "state_dirs": {}, "root_files": [{"rel": "manifest.json", "sha256": "x"}]}
        with pytest.raises(RuntimeError, match="MANIFEST.json"):
            export_mod.write_manifest_and_sums(out, m)

    def test_export_retires_legacy_file_but_keeps_website_manifest(self, tmp_path):
        """磁盘上旧的大写目录项删掉，网站 manifest 以小写拼写拷进来；不删同名小写文件。"""
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        (src / "manifest.json").write_text(_PWA_MANIFEST)
        out = tmp_path / "out"
        out.mkdir()
        (out / "MANIFEST.json").write_text('{"legacy": true, "root_files": []}')
        manifest = export_mod.run_export(src, out, code_repo=tmp_path)
        export_mod.write_manifest_and_sums(out, manifest)
        names = os.listdir(out)
        assert "manifest.json" in names and "MANIFEST.json" not in names
        assert (out / "manifest.json").read_text(encoding="utf-8") == _PWA_MANIFEST
        # 第二轮：小写那份不是旧元数据，不许被当成旧目录项删掉
        assert export_mod._retire_legacy_manifest_file(out) is False
        assert "manifest.json" in os.listdir(out)


class TestRestoreAcrossManifestRename:
    """恢复读新名 `BACKUP_MANIFEST.json`，旧提交只有 `MANIFEST.json`；根文件按清单哈希核对后才拷。"""

    def test_new_layout_round_trip_restores_website_manifest(self, tmp_path):
        src = _src_with_full_scope(tmp_path)
        out = tmp_path / "out"
        export_mod.write_manifest_and_sums(out, export_mod.run_export(src, out, code_repo=tmp_path))
        dest = tmp_path / "dest"
        assert restore_mod.main(["--export-dir", str(out), "--dest-root", str(dest), "--dbs"]) == 0
        assert (dest / "manifest.json").read_text(encoding="utf-8") == _PWA_MANIFEST
        assert _exact_exists(dest, "manifest.json")

    def _legacy_export_dir(self, tmp_path) -> Path:
        """照生产 09-24~10-07 的提交布局手搭：只有元数据 `MANIFEST.json`，它的 root_files 列着网站
        `manifest.json`（真实哈希），磁盘上却没有这个拼写的文件。"""
        out = tmp_path / "legacy"
        out.mkdir()
        (out / "rss.xml").write_text("<rss/>")
        legacy = {"databases": {}, "state_dirs": {}, "root_files": [
            {"rel": "manifest.json", "sha256": "267f16ab" + "0" * 56, "size": 1},
            {"rel": "rss.xml", "sha256": sha256_file(out / "rss.xml"), "size": 6},
        ]}
        (out / "MANIFEST.json").write_text(json.dumps(legacy))
        return out

    def test_legacy_layout_reports_overwritten_manifest_instead_of_restoring_it(self, tmp_path, capsys):
        """旧代码：APFS 上按 `manifest.json` 打开的是元数据，照拷不误 ⇒ 把备份清单「恢复」成网站 manifest；
        大小写敏感的盘上静默跳过。现在两种都不拷、都报出来、退出码 1。"""
        out = self._legacy_export_dir(tmp_path)
        assert restore_mod.find_manifest(out) == out / "MANIFEST.json"
        dest = tmp_path / "dest"
        rc = restore_mod.main(["--export-dir", str(out), "--dest-root", str(dest), "--dbs"])
        assert rc == 1
        assert "manifest.json" not in os.listdir(dest), "不许把备份元数据当网站 manifest 恢复出来"
        assert (dest / "rss.xml").read_text() == "<rss/>"
        err = capsys.readouterr().err
        assert "manifest.json" in err and "v0.45.430 前" in err

    def test_restore_state_sorts_records_into_restored_mismatched_missing(self, tmp_path):
        out = self._legacy_export_dir(tmp_path)
        recs = json.loads((out / "MANIFEST.json").read_text())["root_files"]
        recs.append({"rel": "gone.json", "sha256": "0" * 64})
        got = restore_mod.restore_state(out, tmp_path / "dest", [], recs)
        assert [r["rel"] for r in got["restored"]] == ["rss.xml"]
        bad = {r["rel"] for r in got["mismatched"] + got["missing"]}
        assert bad == {"manifest.json", "gone.json"}
        assert {r["rel"] for r in got["missing"]} >= {"gone.json"}

    def test_legacy_name_is_recognised_only_by_exact_spelling(self, tmp_path):
        """新布局缺了新清单：大小写不敏感的盘上 `MANIFEST.json` 能打开网站 manifest，不许拿它当备份清单。"""
        out = tmp_path / "out"
        out.mkdir()
        (out / "manifest.json").write_text(_PWA_MANIFEST)
        assert restore_mod.find_manifest(out) is None


class TestLegacyManifestMigrationInRun:
    """升级后第一轮：旧条目从索引摘掉，网站 manifest 以小写拼写进备份；索引对不上清单就不提交。"""

    @staticmethod
    def _repo_with_remote(tmp_path) -> tuple[Path, Path]:
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
        bk = tmp_path / "bk"
        bk.mkdir()
        for args in (["init", "-b", "main"], ["config", "commit.gpgsign", "false"],
                     ["config", "core.hooksPath", os.devnull],
                     # 与生产备份仓一致（macOS 上 git init 的缺省）；大小写敏感的盘上显式设，走同一条 git 代码路径
                     ["config", "core.ignorecase", "true"],
                     ["remote", "add", "origin", str(bare)]):
            subprocess.run(["git", *args], cwd=str(bk), check=True, capture_output=True)
        return bk, bare

    @staticmethod
    def _legacy_commit(bk: Path) -> None:
        """照生产：索引里只有旧名元数据 `MANIFEST.json`。"""
        (bk / "MANIFEST.json").write_text('{"created_at": "2026-10-07", "root_files": []}')
        (bk / "SHA256SUMS").write_text("")
        subprocess.run(["git", "add", "-A"], cwd=str(bk), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "legacy"], cwd=str(bk), check=True, capture_output=True)

    @staticmethod
    def _src(tmp_path) -> Path:
        src = tmp_path / "src"
        src.mkdir()
        for db_name in export_mod.DBS.values():
            _make_synthetic_db(src / db_name)
        (src / "manifest.json").write_text(_PWA_MANIFEST)
        (src / "rss.xml").write_text("<rss/>")
        return src

    @staticmethod
    def _ls(bk: Path) -> set[str]:
        return set(subprocess.run(["git", "ls-files"], cwd=str(bk), check=True,
                                  capture_output=True, text=True).stdout.split())

    def _run(self, src, bk, tmp_path):
        return run_backup.run(src, bk, status_file=tmp_path / "s.json", history_file=tmp_path / "h.jsonl")

    def test_upgrade_from_legacy_layout_tracks_both_files_and_restores_from_clone(self, tmp_path, monkeypatch):
        _bypass_secret_scan(monkeypatch)
        bk, bare = self._repo_with_remote(tmp_path)
        self._legacy_commit(bk)
        src = self._src(tmp_path)

        st = self._run(src, bk, tmp_path)
        assert st["stage"] == "done", st
        assert st["legacy_manifest_retired"] is True
        tracked = self._ls(bk)
        assert {"manifest.json", export_mod.MANIFEST_NAME, export_mod.SUMS_NAME} <= tracked
        assert "MANIFEST.json" not in tracked
        head_pwa = subprocess.run(["git", "show", "HEAD:manifest.json"], cwd=str(bk), check=True,
                                  capture_output=True, text=True).stdout
        assert head_pwa == _PWA_MANIFEST

        st2 = self._run(src, bk, tmp_path)
        assert st2["stage"] == "done" and "legacy_manifest_retired" not in st2, st2

        # 异地恢复：从远端 clone 出来的副本还原，网站 manifest 原样回来
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, capture_output=True)
        dest = tmp_path / "dest"
        assert restore_mod.main(["--export-dir", str(clone), "--dest-root", str(dest), "--dbs"]) == 0
        assert (dest / "manifest.json").read_text(encoding="utf-8") == _PWA_MANIFEST

    def test_without_index_retirement_the_case_drift_is_caught(self, tmp_path, monkeypatch):
        """变异：不摘旧索引条目 ⇒ APFS 上 `git add -A` 把磁盘上的 `manifest.json` 记成 `MANIFEST.json`
        （`core.ignorecase`）。检查必须红、不提交。只在大小写不敏感的盘上能复现——生产 Mac 正是这种盘。"""
        if not _fs_is_case_insensitive(tmp_path):
            pytest.skip("大小写敏感的盘上不会发生同名合并；本机（APFS 缺省）与生产都会跑到这条")
        _bypass_secret_scan(monkeypatch)
        monkeypatch.setattr(run_backup, "retire_legacy_manifest_from_index", lambda d: False)
        bk, _ = self._repo_with_remote(tmp_path)
        self._legacy_commit(bk)
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(bk), capture_output=True,
                                     text=True).stdout
        st = self._run(self._src(tmp_path), bk, tmp_path)
        assert st["stage"] == "git_error" and st["ok"] is False, st
        assert "manifest.json" in st["sums_not_tracked"]
        assert "commit" not in st
        assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(bk), capture_output=True,
                              text=True).stdout == head_before

    def test_sums_path_swallowed_by_ignore_rules_blocks_commit(self, tmp_path, monkeypatch):
        """与平台无关的正对照：备份仓的忽略规则吞掉一个导出文件，同一道检查红。"""
        _bypass_secret_scan(monkeypatch)
        bk, _ = self._repo_with_remote(tmp_path)
        (bk / ".git" / "info").mkdir(exist_ok=True)
        (bk / ".git" / "info" / "exclude").write_text("rss.xml\n")
        st = self._run(self._src(tmp_path), bk, tmp_path)
        assert st["stage"] == "git_error" and st["sums_not_tracked"] == ["rss.xml"], st
        assert st["sums_not_tracked_count"] == 1 and "commit" not in st

    def test_retirement_failure_is_git_error_and_nothing_is_exported(self, tmp_path, monkeypatch):
        def boom(d):
            raise RuntimeError("git rm 失败")
        monkeypatch.setattr(run_backup, "retire_legacy_manifest_from_index", boom)
        bk, _ = self._repo_with_remote(tmp_path)
        st = self._run(self._src(tmp_path), bk, tmp_path)
        assert st["stage"] == "git_error" and "退役失败" in st["error"], st
        assert "manifest_summary" not in st
        assert export_mod.SUMS_NAME not in os.listdir(bk)


class TestManifestSecondReview:
    """v0.45.430 二次检查补：清单坏了要响亮、不许退回旧清单；SHA256SUMS 只按 "\\n" 切。"""

    @staticmethod
    def _export_with_legacy(tmp_path) -> Path:
        out = tmp_path / "out"
        out.mkdir()
        (out / "rss.xml").write_text("<rss/>")
        (out / "MANIFEST.json").write_text(json.dumps({"databases": {}, "state_dirs": {}, "root_files": [
            {"rel": "rss.xml", "sha256": sha256_file(out / "rss.xml")}]}))
        return out

    def test_corrupt_new_manifest_is_loud_and_never_falls_back_to_legacy(self, tmp_path, capsys):
        """旧写法：新清单解析失败 ⇒ `continue` ⇒ 退回陈旧的 MANIFEST.json，照它还原、rc=0。"""
        out = self._export_with_legacy(tmp_path)
        (out / export_mod.MANIFEST_NAME).write_text('{"root_files": [')   # 截断
        with pytest.raises(ValueError, match="不是合法 JSON"):
            restore_mod.find_manifest(out)
        dest = tmp_path / "dest"
        assert restore_mod.main(["--export-dir", str(out), "--dest-root", str(dest), "--dbs"]) == 1
        assert not (dest / "rss.xml").exists(), "不许照旧清单还原"
        assert "不退回其他清单" in capsys.readouterr().err

    def test_named_file_that_is_not_a_manifest_raises(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / export_mod.MANIFEST_NAME).write_text(_PWA_MANIFEST)
        with pytest.raises(ValueError, match="不是备份清单"):
            restore_mod.find_manifest(out)

    def test_no_manifest_is_rc1_unless_state_restore_was_skipped(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        assert restore_mod.main(["--export-dir", str(out), "--dest-root", str(tmp_path / "d1"), "--dbs"]) == 1
        assert restore_mod.main(["--export-dir", str(out), "--dest-root", str(tmp_path / "d2"), "--dbs",
                                 "--skip-state"]) == 0

    def test_sums_parsing_survives_unicode_line_separators_in_file_names(self, tmp_path):
        """`str.splitlines()` 会在 U+2028 处断行 ⇒ 半行没有「两个空格」⇒ IndexError。文件名里它是合法字符。"""
        bk = tmp_path / "bk"
        bk.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(bk), check=True, capture_output=True)
        odd = "a b.json"
        (bk / odd).write_text("{}")
        (bk / export_mod.MANIFEST_NAME).write_text('{"root_files": []}')
        (bk / export_mod.SUMS_NAME).write_text(f"{sha256_file(bk / odd)}  {odd}\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(bk), check=True, capture_output=True)
        assert odd in "".join(run_backup._tracked_paths(bk)), "夹具前提：怪名文件确实进了索引"
        assert run_backup.sums_paths_not_tracked(bk) == []
        assert run_backup.foreign_entries(bk) == []
