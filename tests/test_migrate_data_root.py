"""数据根迁移阶段 5 工具 `data_backup/migrate_data_root.py` 的回归覆盖（全合成数据）。

覆盖面按「这一步出错时谁会红」组织：
- 未登记项 ⇒ plan 失败、copy 拒绝（默认拒绝，不许把新产物默认成搬或不搬）
- WAL 里尚未回写的行必须随在线备份带走（阶段 0 实测的真坑：只拷主文件行数相同、内容不同）
- 目标已存在且内容不同 ⇒ 一个都不复制
- verify 抓得住：新根少行 / 文件内容被改
- retire 只挪未跟踪项、被跟踪数据原地冻结（挪走 = 下一次日报把账本删除推上 main）
- check-old 抓得住：旧根重新长出活库 / 冻结数据被写
- unretire 能完整回退
"""
import json
import os
import sqlite3
import subprocess

import pytest

from data_backup import migrate_data_root as m


def _clean_git_env():
    """去掉继承来的 GIT_*：在 git 钩子里跑时 git 会导出 GIT_DIR / GIT_INDEX_FILE，
    不去掉的话夹具的 add/commit 会落到真仓库（被测模块的 git 调用同理，见 _no_git_env）。"""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


@pytest.fixture(autouse=True)
def _no_git_env(monkeypatch):
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)


def _git(root, *args):
    env = {**_clean_git_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)


def _wal_db_with_pending_rows(path, n_committed=3, n_pending=2):
    """WAL 模式库：一部分行已 checkpoint 进主文件，另一部分只在 -wal 里。连接保持打开返回。"""
    c = sqlite3.connect(str(path), isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t(x)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n_committed)])
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n_pending)])
    return c


@pytest.fixture
def roots(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    # 代码 + 被跟踪的数据
    (old / "app.py").write_text("print(1)\n")
    (old / "report_snapshots").mkdir()
    (old / "report_snapshots" / "a.json").write_text('{"a": 1}')
    (old / "index.html").write_text("<html>v1</html>")
    _git(old, "init", "-q")
    _git(old, "add", "-A")
    _git(old, "commit", "-q", "-m", "init")
    # 未跟踪的数据
    conn = _wal_db_with_pending_rows(old / "pheromone.db")
    (old / "chroma_db").mkdir()
    cc = sqlite3.connect(str(old / "chroma_db" / "chroma.sqlite3"))
    cc.execute("CREATE TABLE e(v)")
    cc.execute("INSERT INTO e VALUES (1)")
    cc.commit()
    cc.close()
    (old / "chroma_db" / "seg").mkdir()
    (old / "chroma_db" / "seg" / "index.bin").write_bytes(b"\x00\x01")
    (old / "analysis-AAA-ml-2026-09-01.json").write_text("{}")
    (old / ".swarm_results_2026-09-01.json").write_text("{}")
    (old / ".alpha_hive_av_key").write_text("not-a-real-key")
    yield old, new
    conn.close()


def test_unknown_entry_blocks_plan_and_copy(roots):
    old, new = roots
    (old / "mystery_output.bin").write_bytes(b"?")
    pl = m.plan(old)
    assert pl["unknown"] == ["mystery_output.bin"]
    with pytest.raises(RuntimeError, match="未登记"):
        m.copy(old, new, pl)
    assert not any(new.iterdir()), "拒绝时一个都不许复制"


def test_copy_verify_carries_wal_rows_and_skips_code_and_keys(roots):
    old, new = roots
    pl = m.plan(old)
    assert pl["unknown"] == []
    m.copy(old, new, pl)
    v = m.verify(old, new, pl)
    assert v["ok"], v["problems"]
    assert not (new / "pheromone.db-wal").exists(), "copy/verify 不许在新根留下 sidecar"
    with sqlite3.connect(str(new / "pheromone.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5, "WAL 里未回写的 2 行必须带走"
    assert (new / "chroma_db" / "seg" / "index.bin").read_bytes() == b"\x00\x01"
    assert (new / "chroma_db" / "chroma.sqlite3").exists()
    assert (new / "report_snapshots" / "a.json").exists()
    assert not (new / "app.py").exists(), "代码不进数据根"
    assert not (new / ".alpha_hive_av_key").exists(), "凭据不进数据根"


def test_conflicting_target_aborts_before_copying_anything(roots):
    old, new = roots
    (new / "index.html").write_text("<html>DIFFERENT</html>")
    with pytest.raises(RuntimeError, match="内容不同"):
        m.copy(old, new, m.plan(old))
    assert not (new / "pheromone.db").exists()
    assert not (new / "report_snapshots").exists()


def test_identical_existing_target_is_fine(roots):
    old, new = roots
    (new / "index.html").write_text("<html>v1</html>")
    pl = m.plan(old)
    m.copy(old, new, pl)
    assert m.verify(old, new, pl)["ok"]


def test_verify_catches_missing_rows_and_tampered_file(roots):
    old, new = roots
    pl = m.plan(old)
    m.copy(old, new, pl)
    with sqlite3.connect(str(new / "pheromone.db")) as c:
        c.execute("DELETE FROM t WHERE rowid = (SELECT MAX(rowid) FROM t)")
    (new / "report_snapshots" / "a.json").write_text('{"a": 2}')
    v = m.verify(old, new, pl)
    assert not v["ok"]
    joined = "\n".join(v["problems"])
    assert "pheromone.db 行数不一致" in joined
    assert "sha256 不同 report_snapshots/a.json" in joined


def test_retire_moves_untracked_only_then_check_old_and_unretire(roots):
    old, new = roots
    pl = m.plan(old)
    m.copy(old, new, pl)
    res = m.retire(old, new, pl)
    hold = old / m.RETIRE_DIRNAME
    # 未跟踪的挪走了，被跟踪的原地
    assert not (old / "pheromone.db").exists() and (hold / "pheromone.db").exists()
    assert (hold / "pheromone.db-wal").exists(), "活库 sidecar 跟着主文件一起挪"
    assert not (old / "chroma_db").exists()
    assert (old / "report_snapshots" / "a.json").exists()
    assert set(res["frozen_tracked"]) == {"report_snapshots", "index.html"}
    # git 看不到任何被跟踪文件的删除
    st = subprocess.run(["git", "-C", str(old), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert " D " not in st and "\nD " not in st, st
    # 暂存区只读
    assert not os.access(hold / "analysis-AAA-ml-2026-09-01.json", os.W_OK)

    assert m.check_old(old)["ok"]

    # 有人还按旧路径开库 ⇒ sqlite 会在旧根新建空库
    sqlite3.connect(str(old / "pheromone.db")).close()
    # 有人还在写冻结的被跟踪数据
    (old / "index.html").write_text("<html>v2</html>")
    r = m.check_old(old)
    assert not r["ok"]
    assert r["reappeared"] == ["pheromone.db"]
    assert r["written_outside_git"] == ["index.html"]
    assert r["synced_by_git"] == []

    # 回退：旧根重新长出来的那份必须先人工处理，unretire 拒绝覆盖
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        m.unretire(old)
    (old / "pheromone.db").unlink()
    m.unretire(old)
    with sqlite3.connect(str(old / "pheromone.db")) as c:
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5
    assert (old / "chroma_db" / "seg" / "index.bin").exists()
    assert os.access(old / "analysis-AAA-ml-2026-09-01.json", os.W_OK)


def test_retire_refuses_when_verify_fails(roots, capsys):
    old, new = roots
    pl = m.plan(old)
    m.copy(old, new, pl)
    (new / "report_snapshots" / "a.json").write_text("tampered")
    rc = m.main(["retire", "--old", str(old), "--new", str(new)])
    assert rc == 1
    assert (old / "pheromone.db").exists(), "verify 不过不许动旧根"


def test_cli_reports_are_written_under_new_root(roots):
    old, new = roots
    assert m.main(["copy", "--old", str(old), "--new", str(new)]) == 0
    assert m.main(["verify", "--old", str(old), "--new", str(new)]) == 0
    reports = sorted((new / m.MIGRATION_DIRNAME).glob("*.json"))
    assert [p.name.split("_")[0] for p in reports] == ["copy", "verify"]
    assert json.loads(reports[1].read_text(encoding="utf-8"))["ok"] is True


@pytest.mark.parametrize("name,is_dir,kind", [
    ("pheromone.db", False, "MOVE_DB"),
    ("pheromone.db-wal", False, "SKIP"),
    ("alpha-hive-daily-2026-09-01.md", False, "MOVE"),   # MOVE 先于 *.md → SKIP
    ("alpha-hive-thread-2026-09-01.txt", False, "MOVE"),  # MOVE 先于 *.txt → SKIP
    ("CHANGELOG.md", False, "SKIP"),
    ("NVDA_raw.json", False, "MOVE"),
    ("report_snapshots", True, "MOVE"),
    ("report_snapshots", False, "UNKNOWN"),                # 同名但不是目录：不许按目录规则放行
    ("brand_new_state", True, "UNKNOWN"),
])
def test_classify_table(name, is_dir, kind):
    assert m.classify(name, is_dir)[0] == kind


def test_every_backup_state_dir_is_moved():
    """私有备份认定为「状态目录」的每一项，迁移表都必须判 MOVE（v0.45.333 起）。

    两张表描述同一批目录、各管一件事：`export.STATE_DIRS`（备份什么）与本模块 `MOVE_DIRS`（搬什么）。
    v0.45.333 起初只登记了前者，`sell_strike_state` 在 plan 里是 UNKNOWN ⇒ copy 拒绝整次迁移；
    情急改 SKIP 则旧根里已记的账本行被留下（当日 CBOE 报价事后拿不回）。有这条，下一本新账本
    只登记一张表时当场红。正对照：STATE_DIRS 非空且真含卖权账本（防两边同时被清空后恒绿）。
    变异：从 MOVE_DIRS 删掉 `sell_strike_state`。"""
    from data_backup import export
    assert "sell_strike_state" in export.STATE_DIRS and len(export.STATE_DIRS) >= 5
    not_moved = {d: m.classify(d, True) for d in export.STATE_DIRS if m.classify(d, True)[0] != "MOVE"}
    assert not not_moved, f"备份清单里的状态目录没登记为迁移 MOVE：{not_moved}"
    # 同一条链上的根文件：备份的也必须搬（只核字面文件名，glob 模式无从逐个判）
    literal = [f for f in export.ROOT_FILE_GLOBS if not any(ch in f for ch in "*?[")]
    assert literal, "ROOT_FILE_GLOBS 里一个字面文件名都没有——下面的核对是空转"
    not_moved_files = {f: m.classify(f, False) for f in literal if m.classify(f, False)[0] != "MOVE"}
    assert not not_moved_files, f"备份清单里的根文件没登记为迁移 MOVE：{not_moved_files}"


# ── v0.45.335：v0.45.322 二次检查修复 ────────────────────────────────────────

def _retired(roots):
    old, new = roots
    pl = m.plan(old)
    m.copy(old, new, pl)
    m.retire(old, new, pl)
    return old, new


def test_check_old_git_synced_change_is_not_a_bypass_write(roots):
    """生产同步 `pull --ff-only` 改了冻结文件 ⇒ 与 HEAD 一致 ⇒ 不红，但单列 synced_by_git。"""
    old, _ = _retired(roots)
    (old / "index.html").write_text("<html>fix-from-main</html>")
    _git(old, "commit", "-q", "-am", "data fix landed via main")   # 等价于快进进来的提交
    r = m.check_old(old)
    assert r["ok"], r
    assert r["synced_by_git"] == ["index.html"]
    assert r["written_outside_git"] == []


def test_check_old_preexisting_dirty_file_is_baseline_not_a_write(roots):
    """retire 前就相对 HEAD 脏的冻结文件（生产里的 NVDA_raw.json）不许让 check-old 恒红；
    但它之后再被改，照样要红。"""
    old, new = roots
    (old / "index.html").write_text("<html>dirty-before-retire</html>")
    pl = m.plan(old)
    m.copy(old, new, pl)
    m.retire(old, new, pl)
    assert m.check_old(old)["ok"]
    (old / "index.html").write_text("<html>written-after-retire</html>")
    assert m.check_old(old)["written_outside_git"] == ["index.html"]


def test_check_old_new_untracked_file_in_frozen_dir_is_a_write(roots):
    old, _ = _retired(roots)
    (old / "report_snapshots" / "b.json").write_text("{}")
    r = m.check_old(old)
    assert not r["ok"]
    assert r["written_outside_git"] == ["report_snapshots/b.json"]


def test_copy_is_resumable_after_partial_failure(roots):
    old, new = roots
    pl = m.plan(old)
    m.copy(old, new, pl)
    (new / "analysis-AAA-ml-2026-09-01.json").unlink()        # 模拟文件复制中途失败
    res = m.copy(old, new, pl)                                 # 库已在：不许报冲突
    assert res["dbs"]["pheromone.db"] == {"skipped": "已存在且一致"}
    assert res["files_copied"] == 1
    assert m.verify(old, new, pl)["ok"]


def test_copy_refuses_db_that_exists_but_differs(roots):
    old, new = roots
    pl = m.plan(old)
    m.copy(old, new, pl)
    with sqlite3.connect(str(new / "pheromone.db")) as c:
        c.execute("DELETE FROM t WHERE rowid = 1")
    with pytest.raises(RuntimeError, match="与源不一致"):
        m.copy(old, new, pl)


def test_symlink_in_move_item_is_refused_not_skipped(roots):
    old, new = roots
    (old / "report_snapshots" / "latest.json").symlink_to(old / "report_snapshots" / "a.json")
    pl = m.plan(old)
    with pytest.raises(RuntimeError, match="符号链接"):
        m.copy(old, new, pl)
    assert not (new / "report_snapshots").exists()
    assert any("符号链接" in p for p in m.verify(old, new, pl)["problems"])


def test_retire_failure_midway_still_writes_record_so_unretire_works(roots, monkeypatch):
    old, new = roots
    pl = m.plan(old)
    m.copy(old, new, pl)
    real_rename, calls = os.rename, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise OSError("boom")
        return real_rename(src, dst)

    monkeypatch.setattr(m.os, "rename", flaky)
    with pytest.raises(OSError, match="boom"):
        m.retire(old, new, pl)
    monkeypatch.setattr(m.os, "rename", real_rename)
    rec = json.loads((old / m.RETIRE_DIRNAME / "RETIRE_RECORD.json").read_text(encoding="utf-8"))
    assert len(rec["moved"]) == 2
    m.unretire(old)
    assert (old / "pheromone.db").exists() and (old / "chroma_db").exists()
