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
    """隔离日期的 options.iv_current 不得入库，同批其它信号照常入库。

    用的是 08-24 那条**全天**隔离（无 `tickers` 键）——`_fake_swarm()` 的
    "AAA" 不在任何标的名单里，专门用来证明这条隔离不挑标的。
    """
    q = sa.QUARANTINE[0]
    assert "tickers" not in q, "本测试假定第一条是全天隔离，条目顺序变了要检查这里"
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
    """`is_quarantined` 现在是 (date, ticker, signal) 三元——v0.45.266 加了按标的
    精确隔离的条目后，只按 (日期, 信号) 判会把全部标的一起误伤或一起放过。"""
    q = sa.QUARANTINE[0]
    assert sa.is_quarantined(q["date"], "ANY_TICKER", "options.iv_current") is True, (
        "无 tickers 键的条目必须对任意标的生效（既有全天隔离语义不变）")
    assert sa.is_quarantined(q["date"], "ANY_TICKER", "sentiment.pct") is False
    assert sa.is_quarantined("2026-08-25", "ANY_TICKER", "options.iv_current") is False


def test_quarantine_entries_are_documented():
    """
    每条隔离都必须写明原因与证据 —— 没有出处的隔离等于凭空删数据，
    半年后没人知道该不该恢复。含 tickers 键的条目还必须非空且无重复
    （空元组会静默不隔离任何标的，与「本条目存在」自相矛盾）。
    """
    assert sa.QUARANTINE, "隔离名单为空（若确已清空，请连同本测试一并移除）"
    for q in sa.QUARANTINE:
        assert q.get("date") and q.get("signals"), "隔离项缺 date/signals"
        assert len(q.get("reason", "")) > 40, f"{q['date']} 的 reason 过于简略"
        assert q.get("evidence"), f"{q['date']} 缺 evidence（日志/文件出处）"
        if "tickers" in q:
            tickers = q["tickers"]
            assert tickers, f"{q['date']} 的 tickers 是空的——等于隔离了个寂寞"
            assert len(set(tickers)) == len(tickers), f"{q['date']} 的 tickers 有重复"


# ══════════════════════════════════════════════════════════════════════════
# v0.45.266：按标的精确隔离（v0.45.238 期权快照会话错位）
#
# 与 2026-08-24 那条不同：v0.45.238 的普查（session_date_at 逐份重判）显示
# 11 个坏槽位日里**同一天既有陈旧标的也有正常标的**（例：2026-09-03 只有
# 24/30 只是坏的），若照旧全天拦截会连累好数据。故 QUARANTINE 条目支持可选
# `tickers` 白名单：给了就只挡这些标的，不给（如 08-24 那条）就仍是全天。
# ══════════════════════════════════════════════════════════════════════════

def _stale_snapshot_entry():
    """找到 2026-07-08 那条——9 个标的正常、只有 NVDA 是坏的，最适合验证精确隔离。"""
    for q in sa.QUARANTINE:
        if q["date"] == "2026-07-08":
            return q
    raise AssertionError("2026-07-08 的隔离条目不存在——v0.45.266 还没加")


class TestPerTickerQuarantine:

    def test_stale_ticker_is_quarantined(self):
        q = _stale_snapshot_entry()
        assert sa.is_quarantined("2026-07-08", "NVDA", "options.iv_current") is True

    def test_clean_ticker_same_day_is_not_quarantined(self):
        """成对断言：没有这条，「整天一起挡」也能让上一条为真。"""
        assert sa.is_quarantined("2026-07-08", "AMZN", "options.iv_current") is False

    def test_archive_gates_only_the_stale_ticker(self, db):
        """端到端：同一批 swarm_results 里 NVDA 与 AMZN 都有期权信号，
        归档后只有 NVDA 的 options.iv_current 缺席。"""
        swarm = {
            "NVDA": {"agent_details": {"OracleBeeEcho": {"details": {"iv_current": 55.0}}}},
            "AMZN": {"agent_details": {"OracleBeeEcho": {"details": {"iv_current": 30.0}}}},
        }
        sa.archive(swarm, "2026-07-08", db)
        with sqlite3.connect(db) as c:
            rows = {t: v for t, v in c.execute(
                "SELECT ticker, value FROM signal_archive WHERE date='2026-07-08' "
                "AND signal='options.iv_current'")}
        assert "NVDA" not in rows, "NVDA 那天的期权数据是陈旧的（v0.45.238），入库了"
        assert rows.get("AMZN") == 30.0, "AMZN 当天数据正常，被精确隔离误伤了"

    def test_untouched_ticker_family_stays(self, db):
        """隔离只按 `signals` 白名单里的信号生效——同一标的的 Agent 评分照常入库
        （划界原则：只隔离市场观测，不隔离审计轨迹，见 v0.45.28）。"""
        swarm = {"NVDA": {"final_score": 6.0, "agent_details": {
            "OracleBeeEcho": {"details": {"iv_current": 55.0}, "score": 7.0}}}}
        sa.archive(swarm, "2026-07-08", db)
        with sqlite3.connect(db) as c:
            sigs = {s for (s,) in c.execute(
                "SELECT signal FROM signal_archive WHERE date='2026-07-08' AND ticker='NVDA'")}
        assert "agent.OracleBeeEcho.score" in sigs
        assert "composite.final_score" in sigs
        assert "options.iv_current" not in sigs

    def test_all_eleven_stale_days_are_present(self):
        """11 个坏槽位日（v0.45.238 CHANGELOG 表）必须每天都有条目，一天都不许漏。"""
        days = {"2026-06-10", "2026-07-08", "2026-07-15", "2026-07-17", "2026-07-22",
                "2026-07-23", "2026-07-24", "2026-08-11", "2026-08-14",
                "2026-09-03", "2026-09-09"}
        present = {q["date"] for q in sa.QUARANTINE if q["date"] in days}
        assert present == days, f"缺 {days - present}"


def test_backfill_does_not_resurrect(db, tmp_path, monkeypatch):
    """端到端：删库后再跑 backfill，被隔离的行不得复活。

    用第一条（全天隔离）而不是按标的隔离的条目——`_fake_swarm()` 只造了
    一个不在任何标的白名单里的假标的 "AAA"，配全天隔离才对得上。
    """
    q = sa.QUARANTINE[0]
    assert "tickers" not in q
    f = tmp_path / f".swarm_results_{q['date']}.json"
    f.write_text(json.dumps(_fake_swarm()), encoding="utf-8")
    monkeypatch.setattr(sa, "DB_PATH", db)
    sa.backfill(".swarm_results_*.json", db)
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM signal_archive WHERE date=? AND signal='options.iv_current'",
        (q["date"],)).fetchone()[0]
    assert n == 0, "backfill 让被隔离的污染复活了"
