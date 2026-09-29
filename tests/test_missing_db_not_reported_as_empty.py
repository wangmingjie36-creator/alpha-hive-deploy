"""缺库 ≠ 没数据（v0.45.382）：`PATHS.db` 找错地方时，只读 CLI 必须说「库不存在 + 路径」，不许说「空」。

同形事故：2026-09-28 不带 `ALPHA_HIVE_HOME` 手动跑 `sell_strike_ledger.py --assess`，账本目录不存在却印
「账本里还没有任何行」（那一族的测试在 `test_sell_strike_ledger.py::TestMissingStateDirIsNotEmpty`）。
同版普查出三个经 `PATHS.db` 的只读 CLI 有同一个形状，修法都只在出口：

- `replay_scoring`：库打不开时 notes 里有路径却不打印，照说「这是正常状态」；
- `vol_forecast`：缺库与「当日无归档」同一句「先跑 --backfill」——照做会在错的数据根下建库；
- `signal_archive --list`：读写模式 `ensure_schema` ⇒ **读一次就凭空造出 pheromone.db**，此后别的模块
  「库不存在」的判定全部失效。

每条都配「库在但为空」的对照（原话不变），并断言缺的库读完仍不存在。全部离线、tmp 目录
（conftest `_isolate_env` 已把 `ALPHA_HIVE_DB_PATH` 指到 `<tmp>/test.db`，测试开始时它不存在）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import replay_scoring as rs
import signal_archive as sa
import vol_forecast as vf
from tests.test_replay_scoring import _write_predictions_db


def _default_db() -> Path:
    return Path(os.environ["ALPHA_HIVE_DB_PATH"])


class TestReplayScoring:
    def test_unreadable_db_is_not_normal_empty(self, tmp_path, monkeypatch, capsys):
        """变异：空样本分支不看 ⛔ notes ⇒ 缺库又被说成「这是正常状态」。"""
        missing = tmp_path / "missing.db"
        monkeypatch.setattr(rs, "DB_PATH", str(missing))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py"])
        assert rs.main() == 3
        out = capsys.readouterr().out
        assert "这是正常状态" not in out, out
        assert "样本库读不到" in out and str(missing) in out
        assert not missing.exists()

    def test_empty_db_keeps_the_normal_message(self, tmp_path, monkeypatch, capsys):
        """对照：库在、世代内无样本 ⇒ 原话「无可用样本 / 这是正常状态」不变。"""
        monkeypatch.setattr(rs, "DB_PATH", _write_predictions_db(tmp_path / "empty.db", []))
        monkeypatch.setattr(sys, "argv", ["replay_scoring.py"])
        assert rs.main() == 3
        out = capsys.readouterr().out
        assert "无可用样本" in out and "这是正常状态" in out and "样本库读不到" not in out


class TestVolForecast:
    def test_missing_db_does_not_advise_backfill(self, monkeypatch, capsys):
        """缺库（缺省 PATHS.db）⇒ 退出 3 + 路径，不给「先跑 --backfill」（那会在错的数据根下建库）。
        变异：删掉 main 里的 exists 判定。"""
        monkeypatch.setattr(sys, "argv", ["vol_forecast.py", "--date", "2026-07-29"])
        code = vf.main()
        err = capsys.readouterr().err
        # 先断言误诊本身（改动前的代码红在这一句），再断言退出码与新文字
        assert "先跑 signal_archive.py --backfill" not in err, err
        assert code == 3
        assert "样本库不存在" in err and str(_default_db()) in err
        assert not _default_db().exists()

    def test_existing_db_without_rows_keeps_backfill_advice(self, tmp_path, monkeypatch, capsys):
        """对照：库在但当日无归档 ⇒ 原话与退出码 1 不变（`test_vol_forecast` 的子进程测试另有一条）。"""
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        monkeypatch.setattr(sys, "argv", ["vol_forecast.py", "--date", "2026-07-29", "--db", str(db)])
        assert vf.main() == 1
        assert "先跑 signal_archive.py --backfill" in capsys.readouterr().err


class TestSignalArchiveList:
    def test_list_on_missing_db_creates_nothing(self, tmp_path, monkeypatch, capsys):
        """`--list` 只读：缺库 ⇒ 退出 3 + 路径，**不建库**（显式 --db 与缺省 PATHS.db 两条）。
        变异：`--list` 分支先 `ensure_schema` 再查（原实现）。"""
        missing = tmp_path / "missing.db"
        for argv, path in ((["signal_archive.py", "--list", "--db", str(missing)], missing),
                           (["signal_archive.py", "--list"], _default_db())):
            monkeypatch.setattr(sys, "argv", argv)
            code = sa.main()
            cap = capsys.readouterr()
            # 先断言副作用本身（改动前的代码红在这一句），再断言退出码与文字
            assert not path.exists(), f"--list 读一次就造出了 {path}"
            assert code == 3
            assert "样本库不存在" in cap.err and str(path) in cap.err
            assert "信号" not in cap.out, "缺库时不该印出一张空表头（那读起来就是「没有信号」）"

    def test_list_on_existing_db_still_lists(self, tmp_path, monkeypatch, capsys):
        """对照：库在 ⇒ 照旧补表、印表头、退出 0。"""
        db = tmp_path / "exists.db"
        sqlite3.connect(str(db)).close()
        monkeypatch.setattr(sys, "argv", ["signal_archive.py", "--list", "--db", str(db)])
        assert sa.main() == 0
        assert "信号" in capsys.readouterr().out
