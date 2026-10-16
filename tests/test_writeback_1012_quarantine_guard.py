"""v0.45.502：`experiments/writeback_1012_replay_v0484.py` 的整日替换不得把 QUARANTINE 里的行写回。

10-13 第二次写回把 v0.45.499 刚隔离并清掉的 BILI 10-12 options.iv_current 插了回去（plan 在 499 之后生成，
但沙箱是 499 之前的代码跑的；整日「删再插」不经 archive() 的入库口）。现在 apply 先按**现行**名单核一遍，
命中即整体中止——本文件钉「中止且什么都没写」，并配一条「名单里没有它就放行到写前值核对」的对照。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

import signal_archive

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("writeback_1012", _ROOT / "experiments" / "writeback_1012_replay_v0484.py")
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

_ROW = {"date": "2026-10-12", "ticker": "BILI", "signal": "options.iv_current", "value": 6.25, "created_at": "x"}


def _setup(tmp_path):
    prod = tmp_path / "prod"
    prod.mkdir()
    con = sqlite3.connect(prod / "pheromone.db")
    con.execute("CREATE TABLE signal_archive (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT, "
                "signal TEXT, value REAL, created_at TEXT)")
    con.commit()
    cols, old = wb._rows(str(prod / "pheromone.db"), "signal_archive")
    con.close()
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    P = {"date": "2026-10-12", "scope": ["core"], "files": {}, "jsonl_rows": {}, "sentiment_baseline": None,
         "db": {"signal_archive": {"cols": cols, "old_hash": wb._h(old), "old_n": 0, "old": old, "new": [_ROW]}}}
    (plan_dir / "plan.json").write_text(json.dumps(P), encoding="utf-8")
    return prod, plan_dir


def test_apply_refuses_quarantined_row(tmp_path, monkeypatch):
    monkeypatch.setattr(signal_archive, "QUARANTINE",
                        [{"date": "2026-10-12", "tickers": ("BILI",), "signals": ("options.iv_current",), "reason": "t"}])
    prod, plan_dir = _setup(tmp_path)
    with pytest.raises(SystemExit) as ei:
        wb.apply(str(plan_dir), str(prod), str(tmp_path / "bk"), dry_run=False)
    assert "QUARANTINE" in str(ei.value)
    assert not (tmp_path / "bk").exists()                       # 连备份都没开始 ⇒ 什么都没写
    con = sqlite3.connect(prod / "pheromone.db")
    assert con.execute("SELECT count(*) FROM signal_archive").fetchone()[0] == 0
    con.close()


def test_control_not_quarantined_passes_to_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(signal_archive, "QUARANTINE", [])
    prod, plan_dir = _setup(tmp_path)
    wb.apply(str(plan_dir), str(prod), str(tmp_path / "bk"), dry_run=True)
    assert "写前值核对通过" in capsys.readouterr().out
