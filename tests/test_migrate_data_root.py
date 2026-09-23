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


def _git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
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
    assert r["tracked_changed"] == ["index.html"]

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
