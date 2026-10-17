"""隔离条目的 `keep_rows`：照常隔离、库里已有的行不删（v0.45.530）。

v0.45.530 清掉 508 / 517 的 92 行时，10-14 BILI `price.momentum_5d` 故意留下：维度 IC 冻结评分器（buzz_v1 阶段 2）
要用归档的原始动量重算背离调整，自证 ② 要求「生产有分、重算缺输入」= 0，10-14 又在自证窗口内。光在 CHANGELOG 里写
「别删」挡不住下一次不带标记的 `purge --expect N`——所以做成清库工具认的键。本文件钉：

1. 带 `keep_rows` 的条目恰好就这一条（怕变多也怕变少：多了是有人悄悄把「该清的」变成「不清」，少了是例外被抹掉）；
2. `signal_archive.purge_due`（唯一判据）的真值表：只被 keep 条目命中 ⇒ 不删；同一格还被别的条目命中 ⇒ 照删；没隔离 ⇒ 不删；
3. 真脚本端到端（临时库）：dry-run 报「保留 N 行」，`--apply` 后那一行还在、同批其它隔离行没了、未隔离行不动；
4. 整库恢复的报告：故意保留的行另报 `quarantined_kept_rows`，**不**计进「该清」的 `quarantined_signal_rows`
   （否则那个数恒 ≥ 1，「非 0 就去清库」的信号失灵——v0.45.530 二次检查抓到）。
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import signal_archive as sa

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "experiments" / "purge_quarantined_signal_rows.py"
_KEEP = ("2026-10-14", ("BILI",), ("price.momentum_5d",))


class TestRegistry:
    def test_exactly_one_keep_entry(self):
        keep = [q for q in sa.QUARANTINE if "keep_rows" in q]
        assert [(q["date"], tuple(q["tickers"]), tuple(q["signals"])) for q in keep] == [_KEEP]

    def test_keep_reason_is_written(self):
        q = next(q for q in sa.QUARANTINE if "keep_rows" in q)
        assert isinstance(q["keep_rows"], str) and len(q["keep_rows"]) > 40
        assert "自证" in q["keep_rows"]
        assert sa.is_quarantined("2026-10-14", "BILI", "price.momentum_5d")    # 仍然隔离：只是不删


class TestPurgeable:
    def test_truth_table_on_real_registry(self):
        assert not sa.purge_due("2026-10-14", "BILI", "price.momentum_5d")
        assert not sa.purge_due("2026-10-14", "BILI", "price.momentum_5d", "v0.45.508")
        assert sa.purge_due("2026-10-14", "BILI", "price.volume_ratio")          # 同一只、别的信号照删
        assert sa.purge_due("2026-10-14", "NVDA", "price.volume_ratio")
        assert sa.purge_due("2026-09-15", "NVDA", "price.momentum_5d")
        assert not sa.purge_due("2026-10-15", "NVDA", "price.volume_ratio")      # 没隔离
        assert not sa.purge_due("2026-10-14", "JNJ", "price.volume_ratio")

    def test_only_tag_still_filters(self):
        assert sa.purge_due("2026-10-14", "NVDA", "price.volume_ratio", "v0.45.508")
        assert not sa.purge_due("2026-10-14", "NVDA", "price.volume_ratio", "v0.45.517")

    def test_another_entry_on_the_same_cell_wins(self, monkeypatch):
        cell = {"date": "2026-01-02", "tickers": ("AAA",), "signals": ("price.momentum_5d",),
                "reason": "x" * 50, "evidence": ("e",)}
        monkeypatch.setattr(sa, "QUARANTINE", [{**cell, "keep_rows": "k" * 50}])
        assert not sa.purge_due("2026-01-02", "AAA", "price.momentum_5d")
        monkeypatch.setattr(sa, "QUARANTINE", [{**cell, "keep_rows": "k" * 50}, cell])
        assert sa.purge_due("2026-01-02", "AAA", "price.momentum_5d")


class TestScriptEndToEnd:
    def _db(self, tmp_path):
        db = tmp_path / "p.db"
        sa.ensure_schema(db)
        con = sqlite3.connect(db)
        con.executemany(f"INSERT INTO {sa.TABLE} (date, ticker, signal, value) VALUES (?,?,?,?)", [
            ("2026-10-14", "BILI", "price.momentum_5d", 0.4021),    # keep_rows ⇒ 留
            ("2026-10-14", "NVDA", "price.volume_ratio", 0.7960),   # 508 量比 ⇒ 删
            ("2026-10-14", "BILI", "price.volume_ratio", 1.3305),   # 508 量比 ⇒ 删
            ("2026-10-15", "NVDA", "price.volume_ratio", 1.1671),   # 没隔离 ⇒ 不动
        ])
        con.commit()
        con.close()
        return db

    def _run(self, *args):
        return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, cwd=_ROOT)

    def _left(self, db):
        con = sqlite3.connect(db)
        try:
            return {(d, t, s) for d, t, s in con.execute(f"SELECT date, ticker, signal FROM {sa.TABLE}")}
        finally:
            con.close()

    def test_dry_run_reports_kept(self, tmp_path):
        db = self._db(tmp_path)
        r = self._run("--db", str(db), "--expect", "2")
        assert r.returncode == 0, r.stderr
        assert "命中隔离名单 2 行" in r.stdout and "另有 1 行按 keep_rows 保留" in r.stdout
        assert len(self._left(db)) == 4

    def test_apply_keeps_the_row(self, tmp_path):
        db = self._db(tmp_path)
        r = self._run("--db", str(db), "--expect", "2", "--apply", "--backup", str(tmp_path / "bk.db"))
        assert r.returncode == 0, r.stderr
        assert self._left(db) == {("2026-10-14", "BILI", "price.momentum_5d"),
                                  ("2026-10-15", "NVDA", "price.volume_ratio")}
        assert len(self._left(tmp_path / "bk.db")) == 4                       # 备份是删之前的状态

    def test_plain_expect_counting_the_kept_row_stops(self, tmp_path):
        """不带标记、按「全部隔离行」数成 3 ⇒ 对不上、停下，什么都不删——保留那一行不会被误清。"""
        db = self._db(tmp_path)
        r = self._run("--db", str(db), "--expect", "3", "--apply", "--backup", str(tmp_path / "bk.db"))
        assert r.returncode != 0 and len(self._left(db)) == 4


class TestRestoreReport:
    def _restore(self, tmp_path, rows):
        from data_backup import export as export_mod, restore as restore_mod
        src = tmp_path / "src"
        src.mkdir()
        with sqlite3.connect(src / "p.db") as c:
            c.execute("CREATE TABLE signal_archive (id INTEGER PRIMARY KEY, date TEXT, ticker TEXT, "
                      "signal TEXT, value REAL, created_at TEXT, UNIQUE(date, ticker, signal))")
            c.executemany("INSERT INTO signal_archive (date,ticker,signal,value) VALUES (?,?,?,?)", rows)
        out = tmp_path / "backup"
        export_mod.export_db(src, "pheromone", "p.db", out / "db_exports")
        return restore_mod.restore_db(out, "pheromone", tmp_path / "restored" / "p.db")

    def test_kept_row_reported_apart(self, tmp_path):
        r = self._restore(tmp_path, [("2026-10-14", "BILI", "price.momentum_5d", 0.4021)])
        assert r["quarantined_signal_rows"] == 0 and r["quarantined_kept_rows"] == 1

    def test_pre_purge_backup_still_flags_the_92_shape(self, tmp_path):
        """备份早于 v0.45.530 清库时，那批行会回来 ⇒ 「该清」照样非 0；保留行不混进去。"""
        r = self._restore(tmp_path, [("2026-10-14", "BILI", "price.momentum_5d", 0.4021),
                                     ("2026-10-14", "NVDA", "price.volume_ratio", 0.7960),
                                     ("2026-09-25", "T", "price.momentum_5d", -6.098)])
        assert r["quarantined_signal_rows"] == 2 and r["quarantined_kept_rows"] == 1
