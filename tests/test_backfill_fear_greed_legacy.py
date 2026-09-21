"""
backfill_fear_greed_legacy.py（v0.45.267）

`market.fear_greed` / `market.fear_greed_is_cnn` 是 v0.45.247 才加的抽取器，读的是
`agent_details.BuzzBeeWhisper.details.fear_greed.value` —— 这个结构化字段在此之前的
`.swarm_results_*.json` 里从不存在，`signal_archive.backfill()` 现有的抽取器重跑一遍
历史文件也抽不出东西（抽取器读的字段压根没写过，不是"口径变了"，是"以前没有"）。

真值另有出处：老版本 Buzz 把 F&G 写进了人类可读的 `discovery` 文本字段
（形如 "... F&G 21(Extreme Fear) ..."），是当天扫描真实用过的值。实测比对过
CNN 官方历史序列端点：同一天两者经常相差 1~10 点（intraday 快照 vs 官方历史点，
口径不同），直接拿 CNN 历史序列回填会让同一个信号列前后两段用不同口径——
所以真值来源选 discovery 文本，不选外部历史序列。

来源标签留白是有意的：`signal_archive._fear_greed_is_cnn` 的 docstring 已经证实
2026-03-10~13 那几天 discovery 记的其实是 Alternative.me 加密 F&G（CNN 当日
17-22，记录值却是 13/15）——直接沿用这个已核实的结论标 is_cnn=0.0。除此之外的
历史区间，老代码没留 source 字段，无法可靠判定当天走的是 CNN 还是加密备用源；
不编数据，宁可对应 is_cnn 一行不写，也不用"数值接近就当 CNN"这类猜测。
"""

import json
import sqlite3
from pathlib import Path


import backfill_fear_greed_legacy as M


def _write_snapshot(path: Path, date: str, tickers_discovery: dict):
    """写一份最小 .swarm_results_<date>.json，每个 ticker 只带 discovery 文本。"""
    payload = {
        tk: {"agent_details": {"BuzzBeeWhisper": {"discovery": disc}}}
        for tk, disc in tickers_discovery.items()
    }
    (path / f".swarm_results_{date}.json").write_text(json.dumps(payload))


def _make_db(path: Path, rows):
    """rows: [(date, ticker, signal, value), ...]"""
    db_path = path / "pheromone.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL, "
            "UNIQUE(date, ticker, signal))"
        )
        conn.executemany(
            "INSERT INTO signal_archive (date,ticker,signal,value) VALUES (?,?,?,?)", rows
        )
        conn.commit()
    return db_path


class TestExtractLegacyFG:
    def test_matches_fg_in_discovery_text(self):
        disc = "情绪 50% | 动量 -1.4% | F&G 21(Extreme Fear) | 历史5条:多5/空0"
        snapshot = {"MSFT": {"agent_details": {"BuzzBeeWhisper": {"discovery": disc}}}}
        assert M._extract_legacy_fg(snapshot) == (21, "Extreme Fear")

    def test_no_fg_substring_returns_none(self):
        disc = "情绪 50% | 动量 -1.4% | 历史5条:多5/空0"
        snapshot = {"MSFT": {"agent_details": {"BuzzBeeWhisper": {"discovery": disc}}}}
        assert M._extract_legacy_fg(snapshot) is None

    def test_falls_through_to_second_ticker_when_first_has_no_match(self):
        snapshot = {
            "AAA": {"agent_details": {"BuzzBeeWhisper": {"discovery": "无 F&G 字样"}}},
            "BBB": {"agent_details": {"BuzzBeeWhisper": {"discovery": "F&G 60(Greed)"}}},
        }
        assert M._extract_legacy_fg(snapshot) == (60, "Greed")

    def test_missing_buzz_block_does_not_crash(self):
        snapshot = {"AAA": {"agent_details": {}}, "BBB": {"not_a_dict": True}}
        assert M._extract_legacy_fg(snapshot) is None

    def test_empty_snapshot_returns_none(self):
        assert M._extract_legacy_fg({}) is None


class TestFindBackfillTargets:
    def test_backfills_date_missing_market_fear_greed(self, tmp_path):
        db_path = _make_db(tmp_path, [
            ("2026-04-01", "MSFT", "price.momentum_5d", 1.0),
            ("2026-04-01", "NVDA", "price.momentum_5d", 2.0),
        ])
        _write_snapshot(tmp_path, "2026-04-01", {
            "MSFT": "F&G 45(Fear)", "NVDA": "F&G 45(Fear)",
        })

        targets = M.find_backfill_targets(tmp_path, db_path)

        assert set(targets) == {"2026-04-01"}
        info = targets["2026-04-01"]
        assert info["value"] == 45
        assert info["classification"] == "Fear"
        assert sorted(info["tickers"]) == ["MSFT", "NVDA"]
        assert info["is_cnn"] is None

    def test_skips_date_that_already_has_market_fear_greed(self, tmp_path):
        db_path = _make_db(tmp_path, [
            ("2026-09-15", "MSFT", "market.fear_greed", 29.0),
        ])
        _write_snapshot(tmp_path, "2026-09-15", {"MSFT": "F&G 29(Fear)"})

        targets = M.find_backfill_targets(tmp_path, db_path)

        assert targets == {}

    def test_skips_date_with_no_snapshot_file(self, tmp_path):
        db_path = _make_db(tmp_path, [("2026-04-02", "MSFT", "price.momentum_5d", 1.0)])
        # 故意不写 .swarm_results_2026-04-02.json

        targets = M.find_backfill_targets(tmp_path, db_path)

        assert targets == {}

    def test_skips_date_where_no_discovery_has_fg_text(self, tmp_path):
        db_path = _make_db(tmp_path, [("2026-04-03", "MSFT", "price.momentum_5d", 1.0)])
        _write_snapshot(tmp_path, "2026-04-03", {"MSFT": "情绪 50% | 动量 -1.4%"})

        targets = M.find_backfill_targets(tmp_path, db_path)

        assert targets == {}

    def test_known_crypto_fallback_window_marked_is_cnn_zero(self, tmp_path):
        db_path = _make_db(tmp_path, [("2026-03-10", "TSLA", "price.momentum_5d", 1.0)])
        _write_snapshot(tmp_path, "2026-03-10", {"TSLA": "F&G 13(Extreme Fear)"})

        targets = M.find_backfill_targets(tmp_path, db_path)

        assert targets["2026-03-10"]["is_cnn"] == 0.0

    def test_date_outside_known_crypto_window_leaves_is_cnn_unset(self, tmp_path):
        db_path = _make_db(tmp_path, [("2026-06-01", "TSLA", "price.momentum_5d", 1.0)])
        _write_snapshot(tmp_path, "2026-06-01", {"TSLA": "F&G 40(Fear)"})

        targets = M.find_backfill_targets(tmp_path, db_path)

        assert targets["2026-06-01"]["is_cnn"] is None


class TestRun:
    def test_dry_run_does_not_write(self, tmp_path):
        db_path = _make_db(tmp_path, [("2026-04-01", "MSFT", "price.momentum_5d", 1.0)])
        _write_snapshot(tmp_path, "2026-04-01", {"MSFT": "F&G 45(Fear)"})

        result = M.run(tmp_path, db_path, apply_changes=False)

        assert result["dates"] == 1
        with sqlite3.connect(str(db_path)) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM signal_archive WHERE signal='market.fear_greed'"
            ).fetchone()[0]
        assert n == 0

    def test_apply_writes_value_row_for_every_ticker_that_day(self, tmp_path):
        db_path = _make_db(tmp_path, [
            ("2026-04-01", "MSFT", "price.momentum_5d", 1.0),
            ("2026-04-01", "NVDA", "price.momentum_5d", 2.0),
        ])
        _write_snapshot(tmp_path, "2026-04-01", {
            "MSFT": "F&G 45(Fear)", "NVDA": "F&G 45(Fear)",
        })

        M.run(tmp_path, db_path, apply_changes=True)

        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT ticker, value FROM signal_archive WHERE signal='market.fear_greed' "
                "ORDER BY ticker"
            ).fetchall()
        assert rows == [("MSFT", 45.0), ("NVDA", 45.0)]

    def test_apply_writes_is_cnn_only_for_known_crypto_window(self, tmp_path):
        db_path = _make_db(tmp_path, [
            ("2026-03-10", "TSLA", "price.momentum_5d", 1.0),
            ("2026-06-01", "TSLA", "price.momentum_5d", 1.0),
        ])
        _write_snapshot(tmp_path, "2026-03-10", {"TSLA": "F&G 13(Extreme Fear)"})
        _write_snapshot(tmp_path, "2026-06-01", {"TSLA": "F&G 40(Fear)"})

        M.run(tmp_path, db_path, apply_changes=True)

        with sqlite3.connect(str(db_path)) as conn:
            is_cnn_rows = dict(conn.execute(
                "SELECT date, value FROM signal_archive WHERE signal='market.fear_greed_is_cnn'"
            ).fetchall())
        assert is_cnn_rows == {"2026-03-10": 0.0}

    def test_idempotent_rerun_does_not_duplicate_or_error(self, tmp_path):
        db_path = _make_db(tmp_path, [("2026-04-01", "MSFT", "price.momentum_5d", 1.0)])
        _write_snapshot(tmp_path, "2026-04-01", {"MSFT": "F&G 45(Fear)"})

        M.run(tmp_path, db_path, apply_changes=True)
        M.run(tmp_path, db_path, apply_changes=True)

        with sqlite3.connect(str(db_path)) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM signal_archive WHERE signal='market.fear_greed'"
            ).fetchone()[0]
        assert n == 1

    def test_does_not_touch_dates_that_already_have_data(self, tmp_path):
        """已有 market.fear_greed 的日期（如 v0.45.247 之后的真实扫描）必须原样保留，
        不能被回填脚本用 discovery 文本值覆盖——结构化字段是更可信的来源。"""
        db_path = _make_db(tmp_path, [
            ("2026-09-15", "MSFT", "market.fear_greed", 29.0),
        ])
        _write_snapshot(tmp_path, "2026-09-15", {"MSFT": "F&G 29(Fear)"})

        M.run(tmp_path, db_path, apply_changes=True)

        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT value FROM signal_archive WHERE signal='market.fear_greed'"
            ).fetchall()
        assert rows == [(29.0,)]
