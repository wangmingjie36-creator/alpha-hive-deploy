"""
v0.45.26 回归：数据隔离名单必须在**入库口**生效。

守的不变式：已证实取自错误交易日的观测，不得通过任何路径进入 `signal_archive`。

为什么必须挡在入库口而不是分析时过滤：`backfill()` 用 `INSERT OR REPLACE`
从 `.swarm_results_*.json` 重建，所以删库不足以清除污染——下一次回填会原样带回。
而若改在分析时过滤，每个下游都得记得过滤一次，漏一个就前功尽弃。

按项目惯例配「喂退化数据看它红」的用例。
"""
import json
import sqlite3

import pytest

import signal_archive as sa


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "p.db"
    sa.ensure_schema(p)
    return p


def _fake_swarm(iv=41.0, sent=55.0):
    """最小 swarm_results：含一个被隔离信号与一个未被隔离信号。"""
    return {
        "AAA": {
            "agent_details": {
                "OracleBeeEcho": {"details": {"iv_current": iv}},
                "BuzzBeeWhisper": {"details": {"sentiment_pct": sent}},
            }
        }
    }


def test_quarantined_signal_never_enters_db(db):
    """隔离日期的 options.iv_current 不得入库，同批其它信号照常入库。"""
    q = sa.QUARANTINE[0]
    sa.archive(_fake_swarm(), q["date"], db)
    conn = sqlite3.connect(db)
    iv = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date=? AND signal='options.iv_current'",
        (q["date"],)).fetchone()[0]
    sent = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date=? AND signal='sentiment.pct'",
        (q["date"],)).fetchone()[0]
    assert iv == 0, "被隔离的信号入库了——回填会把已清除的污染带回来"
    assert sent == 1, "隔离误伤了同批的其它信号"


def test_other_dates_unaffected(db):
    """非隔离日期的同一信号必须正常入库——隔离是按 (日期, 信号) 精确生效的。"""
    sa.archive(_fake_swarm(), "2026-08-25", db)
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date='2026-08-25' "
        "AND signal='options.iv_current'").fetchone()[0]
    assert n == 1, "隔离扩散到了未被污染的日期"


def test_is_quarantined_matrix():
    q = sa.QUARANTINE[0]
    assert sa.is_quarantined(q["date"], "options.iv_current") is True
    assert sa.is_quarantined(q["date"], "sentiment.pct") is False
    assert sa.is_quarantined("2026-08-25", "options.iv_current") is False


def test_quarantine_entries_are_documented():
    """
    每条隔离都必须写明原因与证据 —— 没有出处的隔离等于凭空删数据，
    半年后没人知道该不该恢复。
    """
    assert sa.QUARANTINE, "隔离名单为空（若确已清空，请连同本测试一并移除）"
    for q in sa.QUARANTINE:
        assert q.get("date") and q.get("signals"), "隔离项缺 date/signals"
        assert len(q.get("reason", "")) > 40, f"{q['date']} 的 reason 过于简略"
        assert q.get("evidence"), f"{q['date']} 缺 evidence（日志/文件出处）"


def test_backfill_does_not_resurrect(db, tmp_path, monkeypatch):
    """端到端：删库后再跑 backfill，被隔离的行不得复活。"""
    q = sa.QUARANTINE[0]
    f = tmp_path / f".swarm_results_{q['date']}.json"
    f.write_text(json.dumps(_fake_swarm()), encoding="utf-8")
    monkeypatch.setattr(sa, "DB_PATH", db)
    sa.backfill(".swarm_results_*.json", db)
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date=? AND signal='options.iv_current'",
        (q["date"],)).fetchone()[0]
    assert n == 0, "backfill 让被隔离的污染复活了"
