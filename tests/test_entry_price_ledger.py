"""入场价为 0 的账本行：观测点 + 回填（v0.45.360）

存在理由：2026-09-24 / 09-25 predictions 各 30/30 行 price_at_predict=0。
全仓统计一律 `price_at_predict > 0` ⇒ 60 行对全部收益 / IC 计算不可见；
close_correction 只选 `> 0` 的行 ⇒ 永远补不回来。而当时**没有一处**说
「账本少了 30 个样本」——Step 12 只看扫描字段，`last_save_stats` 零读者。
字段全健康、只丢入场价的日子（08-12 / 08-14 BRK-B）则**什么都不红**。

本文件每条测试都必须在把对应改动拆掉时变红。全程离线：三个价格来源都注入。
"""

import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import entry_price_backfill as epb
import scan_coverage_gate as gate

ROOT = Path(__file__).resolve().parent.parent
D = "2026-09-24"
FULL = {"OracleBeeEcho": {"details": {
            "rv_30d": 30.0, "iv_rank": 45.0, "iv_current": 50.0,
            "iv_skew_ratio": 1.0, "put_call_ratio": 0.9, "iv_rv_spread": 20.0}},
        "ChronosBeeHorizon": {"details": {"catalysts": [{"e": 1}]}}}


def _results(tmp_path, tickers, date=D):
    p = tmp_path / f".swarm_results_{date}.json"
    p.write_text(json.dumps({t: {"agent_details": json.loads(json.dumps(FULL))}
                             for t in tickers}))
    return p


def _db(tmp_path, rows):
    """rows: [(date, ticker, price)]；列与生产 predictions 同名（只取用到的）"""
    p = tmp_path / "ledger.db"
    cn = sqlite3.connect(p)
    cn.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        final_score REAL DEFAULT 5, direction TEXT DEFAULT 'neutral',
        price_at_predict REAL, price_at_predict_raw REAL,
        close_corrected_at TEXT, close_correction_source TEXT,
        checked_t1 INTEGER DEFAULT 0, checked_t7 INTEGER DEFAULT 0,
        return_t7 REAL, UNIQUE(date, ticker))""")
    cn.executemany("INSERT INTO predictions (date, ticker, price_at_predict) VALUES (?,?,?)", rows)
    cn.commit()
    cn.close()
    return p


def _gate_cli(tmp_path, results_path, db, date=D):
    out = tmp_path / "cov.json"
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scan_coverage_gate.py"), "--date", date,
         "--file", str(results_path), "--db", str(db), "--quiet", "--out", str(out),
         "--log-dir", str(tmp_path)],
        capture_output=True, text=True)
    return rc.returncode, json.loads(out.read_text()), rc.stdout


T3 = ["AAA", "BBB", "CCC"]


# ───────────────────────── 观测点：scan_coverage_gate ─────────────────────────

class TestGateGoesRedOnZeroEntryPrice:
    def test_all_zero_is_red_and_named(self, tmp_path):
        """09-24 形状：30/30 为 0 ⇒ 退出码 1，摘要行（编排器读 fields）点名 entry_price"""
        db = _db(tmp_path, [(D, t, 0.0) for t in T3])
        rc, d, _ = _gate_cli(tmp_path, _results(tmp_path, T3), db)
        assert rc == 1
        assert "entry_price" in d["degraded_fields"]
        row = [f for f in d["fields"] if f["field"] == "entry_price"][0]
        assert (row["have"], row["total"], row["degraded"]) == (0, 3, True)

    def test_single_zero_with_healthy_fields_is_red(self, tmp_path):
        """08-12 BRK-B 形状：字段全健康、只丢一只入场价——改前退出码 0（什么都不红）"""
        db = _db(tmp_path, [(D, "AAA", 10.0), (D, "BBB", 20.0), (D, "CCC", 0.0)])
        rc, d, _ = _gate_cli(tmp_path, _results(tmp_path, T3), db)
        assert rc == 1
        assert d["entry_price"]["zero"] == ["CCC"]

    @pytest.mark.parametrize("bad", [None, -1.0])
    def test_null_and_negative_count_as_unusable(self, tmp_path, bad):
        db = _db(tmp_path, [(D, "AAA", 10.0), (D, "BBB", 20.0), (D, "CCC", bad)])
        r = gate.check_entry_prices(D, T3, str(db))
        assert r["healthy"] is False and r["zero"] == ["CCC"]

    def test_missing_ledger_rows_are_red(self, tmp_path):
        """扫描结果有、账本没有 ⇒ 同样是样本静默消失"""
        db = _db(tmp_path, [(D, "AAA", 10.0)])
        rc, d, _ = _gate_cli(tmp_path, _results(tmp_path, T3), db)
        assert rc == 1
        assert d["entry_price"]["missing"] == ["BBB", "CCC"]

    def test_healthy_ledger_stays_green(self, tmp_path):
        db = _db(tmp_path, [(D, t, 10.0) for t in T3])
        rc, d, _ = _gate_cli(tmp_path, _results(tmp_path, T3), db)
        assert rc == 0
        assert d["entry_price"]["healthy"] is True

    def test_backlog_reported_but_not_in_exit_code(self, tmp_path):
        """别的日子没补的行：报出来，但不让今天红（用户可能决定不补）"""
        db = _db(tmp_path, [(D, t, 10.0) for t in T3] + [("2026-08-12", "BRK-B", 0.0)])
        rc, d, out = _gate_cli(tmp_path, _results(tmp_path, T3), db)
        assert rc == 0
        assert d["entry_price"]["backlog"] == {"2026-08-12": ["BRK-B"]}
        assert "账本积压" in out

    def test_missing_db_is_undeterminable_not_red_and_not_created(self, tmp_path):
        db = tmp_path / "nope.db"
        rc, d, _ = _gate_cli(tmp_path, _results(tmp_path, T3), db)
        assert rc == 0
        assert d["entry_price"]["determinable"] is False
        assert not db.exists(), "只读核对不许凭空建库"


class TestCheckPricesDoesNotSkipZero:
    """--check-prices 原先 `not px: continue`：30/30 为 0 的日子报「无可比对样本」"""

    def test_zero_price_is_bad_not_skipped(self, tmp_path, monkeypatch):
        import pandas as pd
        idx = pd.to_datetime([D])
        fake = pd.concat({"Close": pd.DataFrame({"AAA": [10.0], "BBB": [20.0]}, index=idx)}, axis=1)
        monkeypatch.setitem(sys.modules, "yfinance",
                            type("M", (), {"download": staticmethod(lambda *a, **k: fake)})())
        db = _db(tmp_path, [(D, "AAA", 0.0), (D, "BBB", 0.0)])
        r = gate.check_prices(D, str(db))
        assert r["determinable"] is True
        assert r["healthy"] is False
        assert sorted(x["ticker"] for x in r["bad"]) == ["AAA", "BBB"]


# ───────────────────────────── 回填：entry_price_backfill ─────────────────────────────

class TestSelection:
    def test_selects_exactly_unusable_rows(self, tmp_path):
        db = _db(tmp_path, [(D, "A", 0.0), (D, "B", None), (D, "C", -2.0), (D, "D", 5.0)])
        cn = sqlite3.connect(db)
        got = [r["ticker"] for r in epb.load_unusable(cn)]
        assert got == ["A", "B", "C"]

    def test_close_correction_cannot_see_them(self, tmp_path):
        """为什么要新工具：close_correction.load_rows 只选 > 0 —— 这一类整个排除。
        若哪天它改成能选到，这条会红，提醒合并两工具而不是各修一半。"""
        import close_correction as cc
        db = _db(tmp_path, [(D, "A", 0.0), (D, "B", None)])
        assert cc.load_rows(sqlite3.connect(db)) == []


class TestDecide:
    def test_two_families_agree_backfill_uses_yf(self):
        r = epb.decide({"yf": 100.0, "cboe": 100.05, "td": None})
        assert r["verdict"] == epb.V_BACKFILL and r["value"] == 100.0
        assert r["families"] == ["cboe", "yf"]

    def test_disagreement_refused(self):
        r = epb.decide({"yf": 100.0, "cboe": 101.0})
        assert r["verdict"] == epb.V_DISPUTED and r["value"] is None

    def test_single_source_not_written_by_default(self):
        assert epb.decide({"yf": 100.0})["verdict"] == epb.V_SINGLE
        assert epb.decide({"yf": 100.0}, allow_single=True)["verdict"] == epb.V_BACKFILL

    def test_no_source(self):
        assert epb.decide({"yf": None, "cboe": 0.0, "td": float("nan")})["verdict"] == epb.V_NONE

    def test_cboe_internal_disagreement_drops_cboe_vote(self):
        """当日 close 与次日 prev_day_close 自相矛盾 ⇒ cboe 不计票，只剩 yf ⇒ 单源不写"""
        r = epb.decide({"yf": 100.0, "cboe": 100.0},
                       {"snap_close": 100.0, "next_prev_close": 103.0})
        assert r["verdict"] == epb.V_SINGLE


def _plan(rows, yf=None, cboe=None, td=None, **kw):
    calls = {"td": []}

    def _td(tk, lo, hi):
        calls["td"].append(list(tk))
        return td or {}
    out = epb.plan(rows, yf_closes=lambda *a: yf or {},
                   cboe=lambda d, t: (cboe or {}).get((d, t), {}),
                   td_closes=_td, **kw)
    return out, calls


class TestPlan:
    ROWS = [{"id": 1, "date": D, "ticker": "AAA", "price_at_predict": 0.0},
            {"id": 2, "date": D, "ticker": "BBB", "price_at_predict": 0.0}]

    def test_twelve_data_only_for_rows_without_cboe(self):
        out, calls = _plan(self.ROWS, yf={(D, "AAA"): 10.0, (D, "BBB"): 20.0},
                           cboe={(D, "AAA"): {"snap_close": 10.0}},
                           td={(D, "BBB"): 20.01})
        assert calls["td"] == [["BBB"]]
        assert [x["verdict"] for x in out] == [epb.V_BACKFILL, epb.V_BACKFILL]
        assert out[1]["families"] == ["td", "yf"]

    def test_trading_day_never_falls_back_to_prior_close(self):
        """yfinance 缺当天时不许拿前一天收盘冒充（close_correction._resolve_close 口径）"""
        out, _ = _plan(self.ROWS[:1], yf={("2026-09-23", "AAA"): 9.0},
                       cboe={(D, "AAA"): {"snap_close": 10.0}})
        assert out[0]["yf"] is None
        assert out[0]["verdict"] == epb.V_SINGLE

    def test_yf_only_closes_excludes_twelve_data(self, monkeypatch):
        """official_closes 缺覆盖时会换成 Twelve Data 的价——那样同一个价会以 yf、td 各计一票"""
        import close_correction as cc
        monkeypatch.setitem(sys.modules, "yfinance", None)   # yfinance 不可得 ⇒ 全缺覆盖
        monkeypatch.setattr(cc, "_twelve_data_closes", lambda t, lo, hi: {(D, "AAA"): 10.0})
        assert epb._yf_only_closes(["AAA"], D, D) == {}
        assert cc._twelve_data_closes(["AAA"], D, D) == {(D, "AAA"): 10.0}, "要还原"


class TestCboeSnapshotAttribution:
    """归属由 last_trade_time 自述：陈旧的当日文件、隔了交易日的次日文件都不许凑数"""

    @staticmethod
    def _patch(monkeypatch, snaps):
        import cloud_snapshot_loader as csl
        monkeypatch.setattr(csl, "load_ticker", lambda d, t, **k: snaps.get(d))

    def test_official_same_day_and_next_prev_close(self, monkeypatch):
        self._patch(monkeypatch, {
            D: {"last_trade_time_et": f"{D}T16:00:00", "price_at_fetch": 10.0,
                "fetched_at_utc": f"{D}T21:02:00+00:00"},
            "2026-09-25": {"last_trade_time_et": "2026-09-25T16:00:00", "prev_day_close": 10.0}})
        got = epb.cboe_snapshot_candidates(D, "AAA", later_dates=[D, "2026-09-25"])
        assert got == {"snap_close": 10.0, "next_prev_close": 10.0}

    def test_stale_intraday_same_day_rejected(self, monkeypatch):
        self._patch(monkeypatch, {
            D: {"last_trade_time_et": f"{D}T12:10:00", "price_at_fetch": 9.7,
                "fetched_at_utc": f"{D}T21:02:00+00:00"}})
        assert epb.cboe_snapshot_candidates(D, "AAA", later_dates=[D]) == {}

    def test_next_snapshot_across_gap_rejected(self, monkeypatch):
        """下一份快照隔了一个交易日 ⇒ 它的 prev_day_close 属于别的日子"""
        self._patch(monkeypatch, {
            "2026-09-28": {"last_trade_time_et": "2026-09-28T16:00:00", "prev_day_close": 11.0}})
        assert epb.cboe_snapshot_candidates(D, "AAA", later_dates=["2026-09-28"]) == {}


class TestApply:
    def test_writes_only_backfill_rows_and_leaves_trace(self, tmp_path):
        db = _db(tmp_path, [(D, "AAA", 0.0), (D, "BBB", 0.0), (D, "CCC", None)])
        cn = sqlite3.connect(db)
        cn.row_factory = sqlite3.Row
        dec = [{"id": 1, "verdict": epb.V_BACKFILL, "value": 10.0, "families": ["cboe", "yf"]},
               {"id": 2, "verdict": epb.V_DISPUTED, "value": None, "families": ["cboe", "yf"]},
               {"id": 3, "verdict": epb.V_BACKFILL, "value": 30.0, "families": ["td", "yf"]}]
        assert epb.apply_plan(cn, dec, now="T") == 2
        got = {r["ticker"]: dict(r) for r in cn.execute("SELECT * FROM predictions")}
        assert got["AAA"]["price_at_predict"] == 10.0
        assert got["AAA"]["price_at_predict_raw"] == 0.0, "原值 0 必须留痕"
        assert got["AAA"]["close_correction_source"] == "entry_backfill:cboe+yf"
        assert got["BBB"]["price_at_predict"] == 0.0, "分歧行不许写"
        assert got["CCC"]["price_at_predict_raw"] == 0.0, "NULL 原值也要留痕成 0.0"
        assert got["AAA"]["checked_t7"] == 0 and got["AAA"]["return_t7"] is None, "不动派生列"

    def test_idempotent_and_never_overwrites_a_usable_price(self, tmp_path):
        db = _db(tmp_path, [(D, "AAA", 0.0)])
        cn = sqlite3.connect(db)
        dec = [{"id": 1, "verdict": epb.V_BACKFILL, "value": 10.0, "families": ["cboe", "yf"]}]
        assert epb.apply_plan(cn, dec) == 1
        dec[0]["value"] = 99.0
        assert epb.apply_plan(cn, dec) == 0
        assert cn.execute("SELECT price_at_predict FROM predictions").fetchone()[0] == 10.0

    def test_dry_run_cli_does_not_write(self, tmp_path):
        """默认 dry-run：库逐字节不变（空账本分支，不出网）"""
        db = _db(tmp_path, [(D, "AAA", 5.0)])
        before = db.read_bytes()
        rc = subprocess.run([sys.executable, str(ROOT / "entry_price_backfill.py"), "--db", str(db)],
                            capture_output=True, text=True)
        assert rc.returncode == 0 and "没有入场价不可用的行" in rc.stdout
        assert db.read_bytes() == before

    def test_backfilled_rows_become_visible_to_stats(self, tmp_path):
        """补完之后 `price_at_predict > 0` 的统计口径能看见它们 ⇒ gate 也转绿"""
        db = _db(tmp_path, [(D, t, 0.0) for t in T3])
        cn = sqlite3.connect(db)
        epb.apply_plan(cn, [{"id": i, "verdict": epb.V_BACKFILL, "value": 1.0 + i,
                             "families": ["cboe", "yf"]} for i in (1, 2, 3)])
        cn.close()
        assert gate.check_entry_prices(D, T3, str(db))["healthy"] is True
        assert math.isclose(sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM predictions WHERE price_at_predict > 0").fetchone()[0], 3)
