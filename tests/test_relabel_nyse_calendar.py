"""`relabel_nyse_calendar.py`（v0.45.521）：按 NYSE 日历离线重算旧联邦日历算错的历史 T+N 标签。

全在临时库 / 临时快照目录里跑（schema 由 `backtester.PredictionStore` 建，与生产同一份）。
每条断言的 docstring 写明什么变异会让它变红。
"""
import json
import sqlite3

import pytest

import relabel_nyse_calendar as R
from backtester import PredictionStore

# 2026-10-09（周五）的 T+1：旧联邦日历跳过 10-12 哥伦布日 ⇒ 取 10-13；NYSE 应取 10-12。
DAY, USED, RIGHT = "2026-10-09", "2026-10-13", "2026-10-12"


def _row(conn, **kw):
    kw.setdefault("final_score", 5.0)   # 生产 schema 的 NOT NULL 列
    cols = ", ".join(kw)
    conn.execute(f"INSERT INTO predictions ({cols}) VALUES ({', '.join('?' * len(kw))})", tuple(kw.values()))


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "pheromone.db"
    PredictionStore(str(db))
    snaps = tmp_path / "report_snapshots"
    snaps.mkdir()
    with sqlite3.connect(db) as c:
        # 价格来源：同标的在 USED / RIGHT 两天的 price_at_predict（与生产「库优先」口径同）
        _row(c, date=USED, ticker="NVDA", direction="neutral", price_at_predict=98.0)
        _row(c, date=RIGHT, ticker="NVDA", direction="neutral", price_at_predict=103.0)
        _row(c, date=USED, ticker="AMD", direction="neutral", price_at_predict=50.0)
        _row(c, date=RIGHT, ticker="AMD", direction="neutral", price_at_predict=51.0)
        # 受影响、存值 = 旧取价日收盘 ⇒ 应重算（看多：-2% 判错 → +3% 判对）
        _row(c, id=100, date=DAY, ticker="NVDA", direction="bullish", price_at_predict=100.0,
             price_t1=98.0, return_t1=-2.0, correct_t1=0, ambiguous_t1=0, checked_t1=1)
        # 受影响、但存值不等于旧取价日收盘 ⇒ 不是本 bug 造成的，跳过不覆盖
        _row(c, id=101, date=DAY, ticker="AMD", direction="bullish", price_at_predict=50.0,
             price_t1=77.0, return_t1=54.0, correct_t1=1, ambiguous_t1=0, checked_t1=1)
        # 不受影响（10-05 → 10-06 两表一致）⇒ 不碰
        _row(c, id=102, date="2026-10-05", ticker="NVDA", direction="bullish", price_at_predict=100.0,
             price_t1=99.0, return_t1=-1.0, correct_t1=0, ambiguous_t1=0, checked_t1=1)
    (snaps / f"NVDA_{DAY}.json").write_text(json.dumps({
        "ticker": "NVDA", "date": DAY, "direction": "Long", "entry_price": 100.0,
        "actual_prices": {"t1": 98.0, "t7": None, "t30": None}, "cs_rank": {"final": 0.5}}), encoding="utf-8")
    return db, snaps


def _get(db, rid, cols):
    with sqlite3.connect(db) as c:
        return c.execute(f"SELECT {cols} FROM predictions WHERE id = ?", (rid,)).fetchone()


def test_dry_run_writes_nothing(world, capsys):
    """缺省 dry-run：库与快照字节不变。变红的变异：缺省改成写入。"""
    db, snaps = world
    before = (db.read_bytes(), (snaps / f"NVDA_{DAY}.json").read_bytes())
    assert R.main(["--db", str(db), "--snapshots", str(snaps)]) == 0
    assert (db.read_bytes(), (snaps / f"NVDA_{DAY}.json").read_bytes()) == before
    out = capsys.readouterr().out
    assert "t1 将重算: 1" in out and "t1 跳过：存值≠旧取价日收盘: 1" in out


def test_apply_relabels_only_this_bug_and_backs_up(world):
    """写入：受影响且自校验通过的行按 NYSE 应取日重算（价、收益、判定）；自校验不过的、不受影响的都不碰；
    快照只改那一格、`cs_rank` 等原样保留；库备份进 db_backups/、快照备份 + 审计进 _manual_backups/。
    变红的变异：新值取旧取价日；跳过自校验；判定不重算；快照整份重写丢键；不备份。"""
    db, snaps = world
    assert R.main(["--apply", "--db", str(db), "--snapshots", str(snaps)]) == 0
    assert _get(db, 100, "price_t1, return_t1, correct_t1, ambiguous_t1") == (103.0, 3.0, 1, 0)
    assert _get(db, 101, "price_t1, return_t1, correct_t1") == (77.0, 54.0, 1)
    assert _get(db, 102, "price_t1, return_t1") == (99.0, -1.0)
    snap = json.loads((snaps / f"NVDA_{DAY}.json").read_text(encoding="utf-8"))
    assert snap["actual_prices"]["t1"] == 103.0 and snap["cs_rank"] == {"final": 0.5}
    assert list((db.parent / "db_backups").glob("pheromone.db.bak-pre-v0.45.521-nyse-relabel-*"))
    bdir = db.parent / "_manual_backups" / "report_snapshots-pre-v0.45.521-nyse-relabel"
    assert json.loads((bdir / f"NVDA_{DAY}.json").read_text(encoding="utf-8"))["actual_prices"]["t1"] == 98.0
    audit = json.loads((bdir / "nyse_relabel_v0.45.521_audit.json").read_text(encoding="utf-8"))
    assert [(c["id"], c["used"], c["right"]) for c in audit["db_changes"]] == [(100, USED, RIGHT)]


def test_rerun_is_idempotent(world, capsys):
    """重算后再跑：取价日已是应取日 ⇒ 0 处可改（自校验把新值当「存值≠旧取价日」跳过）。
    变红的变异：自校验比对的是应取日（会再改一遍）。"""
    db, snaps = world
    R.main(["--apply", "--db", str(db), "--snapshots", str(snaps)])
    capsys.readouterr()
    conn = sqlite3.connect(db)
    changes, _ = R.plan_db(conn, R.Prices(conn))
    snap_changes, _ = R.plan_snapshots(str(snaps), R.Prices(conn))
    assert changes == [] and snap_changes == []


def test_concurrent_change_rolls_back_everything(world, tmp_path):
    """计划之后、写入之前别人改了某行 ⇒ 整个事务回滚，一行都不写。变红的变异：去掉 `WHERE … AND 旧值 = ?`
    或不检查影响行数。"""
    db, snaps = world
    conn = sqlite3.connect(db)
    prices = R.Prices(conn)
    changes, _ = R.plan_db(conn, prices)
    conn.close()
    with sqlite3.connect(db) as c:
        c.execute("UPDATE predictions SET price_t1 = 99.5 WHERE id = 100")
    with pytest.raises(RuntimeError, match="回滚"):
        R.apply(db, str(snaps), changes, [], tmp_path / "_mb")
    assert _get(db, 100, "price_t1") == (99.5,)


def test_old_calendar_reconstruction_matches_known_divergence():
    """自校验靠「旧日历取价日」——它必须真是旧行为：10-09 T+1 → 10-13；受难日前 04-02 T+1 → 04-06（NYSE 同）；
    03-26 T+7 → 04-06（NYSE 04-07）。变红的变异：旧日历重建成 NYSE（自校验恒过、恒改）。"""
    from datetime import date
    assert R._old_calendar_target(date(2026, 10, 9), 1) == date(2026, 10, 13)
    assert R._old_calendar_target(date(2026, 3, 26), 7) == date(2026, 4, 6)
