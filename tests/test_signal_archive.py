"""
signal_archive 测试（v0.43.2）

单信号 IC 档案：把「哪块砖在承重」从每次重新考古，变成可查的表。
系统有 7 只蜂、60+ 原始字段，但此前只有 5 个聚合维度进入评估。
"""

import datetime
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_archive as sa


def _tr(**kw):
    """构造一个最小 ticker_result"""
    base = {
        "final_score": 6.5,
        "agent_details": {
            "ScoutBeeNova": {"score": 7.0, "direction": "bullish", "details": {
                "crowding_score": 42.0, "momentum_5d": 3.5,
                "insider": {"sentiment": "bearish", "score": 2.0, "filings": 4,
                            "dollar_bought": 0.0, "dollar_sold": 1e6,
                            "notable_trades": []},
                "congress": {"congress_score": 5.0}}},
            "GuardBeeSentinel": {"score": 5.5, "direction": "neutral",
                                 "details": {"consistency": 0.6}},
        },
    }
    base.update(kw)
    return base


class TestExtraction:
    def test_extracts_nested_paths(self):
        out = sa.extract(_tr())
        assert out["composite.final_score"] == 6.5
        assert out["crowding.score"] == 42.0
        assert out["insider.filings"] == 4.0
        assert out["agent.ScoutBeeNova.score"] == 7.0

    def test_direction_encoded_numerically(self):
        out = sa.extract(_tr())
        assert out["agent.ScoutBeeNova.direction"] == 1.0
        assert out["agent.GuardBeeSentinel.direction"] == 0.0
        assert out["insider.sentiment"] == -1.0

    def test_missing_path_is_omitted_not_zero(self):
        """缺失字段必须被省略，不能填 0 —— 否则会被当成真实观测值参与排序"""
        out = sa.extract({"final_score": 5.0, "agent_details": {}})
        assert "crowding.score" not in out
        assert "insider.filings" not in out

    def test_bool_is_not_coerced_to_number(self):
        """True→1.0 会把布尔混进数值信号，必须拒绝"""
        assert sa._num(True) is None
        assert sa._num(False) is None
        assert sa._num(1) == 1.0

    def test_nan_and_inf_rejected(self):
        assert sa._num(float("nan")) is None
        assert sa._num(float("inf")) is None

    def test_extractor_failure_isolated(self):
        """单个提取器抛异常不得影响其余信号"""
        bad = {"final_score": 6.0, "agent_details": "not-a-dict"}
        out = sa.extract(bad)
        assert out["composite.final_score"] == 6.0

    def test_insider_buys_whitelist_code_p(self):
        """只有 code=P（公开市场买入）才算买入。
        S=卖出 / A=授予 / M=行权 / F=代扣税 均无信息量。"""
        tr = _tr()
        tr["agent_details"]["ScoutBeeNova"]["details"]["insider"]["notable_trades"] = [
            {"code": "P", "insider": "A", "is_officer": True},
            {"code": "P", "insider": "B", "is_officer": False},
            {"code": "S", "insider": "C", "is_officer": True},
            {"code": "M", "insider": "D", "is_officer": True},
        ]
        out = sa.extract(tr)
        assert out["insider.distinct_buyers"] == 2.0
        assert out["insider.officer_buys"] == 1.0

    def test_notable_trades_repr_string_parsed(self):
        """历史文件里 notable_trades 被存成 repr 字符串，必须容错"""
        tr = _tr()
        tr["agent_details"]["ScoutBeeNova"]["details"]["insider"]["notable_trades"] = \
            "[{'code': 'P', 'insider': 'X', 'is_officer': True}]"
        assert sa.extract(tr)["insider.distinct_buyers"] == 1.0

    def test_malformed_notable_trades_does_not_raise(self):
        tr = _tr()
        tr["agent_details"]["ScoutBeeNova"]["details"]["insider"]["notable_trades"] = "{{bad"
        assert sa.extract(tr)["insider.distinct_buyers"] == 0.0

    def test_swarm_agreement_needs_three_agents(self):
        tr = {"agent_details": {"A": {"direction": "bullish"},
                                "B": {"direction": "bullish"}}}
        assert "composite.swarm_agreement" not in sa.extract(tr)
        tr["agent_details"]["C"] = {"direction": "bearish"}
        assert sa.extract(tr)["composite.swarm_agreement"] == pytest.approx(2 / 3)


class TestStorage:
    @pytest.fixture
    def db(self, tmp_path):
        p = tmp_path / "p.db"
        sa.ensure_schema(p)
        return p

    def test_archive_writes_rows(self, db):
        n = sa.archive({"NVDA": _tr(), "TSLA": _tr()}, "2026-03-02", db)
        assert n > 0
        with sqlite3.connect(db) as c:
            got = c.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        assert got == n

    def test_archive_is_idempotent(self, db):
        """同一业务日重复归档不得产生重复行（UNIQUE + REPLACE）"""
        a = sa.archive({"NVDA": _tr()}, "2026-03-02", db)
        sa.archive({"NVDA": _tr()}, "2026-03-02", db)
        with sqlite3.connect(db) as c:
            got = c.execute(f"SELECT COUNT(*) FROM {sa.TABLE}").fetchone()[0]
        assert got == a

    def test_different_dates_coexist(self, db):
        """不同业务日必须并存 —— 与 v0.42.4 日期戳 bug 同类风险"""
        sa.archive({"NVDA": _tr()}, "2026-03-02", db)
        sa.archive({"NVDA": _tr()}, "2026-03-03", db)
        with sqlite3.connect(db) as c:
            d = c.execute(f"SELECT COUNT(DISTINCT date) FROM {sa.TABLE}").fetchone()[0]
        assert d == 2

    def test_empty_input_is_noop(self, db):
        assert sa.archive({}, "2026-03-02", db) == 0

    def test_non_dict_ticker_skipped(self, db):
        assert sa.archive({"BAD": "oops", "NVDA": _tr()}, "2026-03-02", db) > 0

    def test_ensure_schema_twice_ok(self, tmp_path):
        p = tmp_path / "p.db"
        sa.ensure_schema(p)
        sa.ensure_schema(p)


class TestAnalysisPanel:
    def test_panel_uses_pure_price_return(self, tmp_path):
        """前瞻收益必须用纯价格变动，而非 return_t7 列。

        return_t7 是路径依赖收益（触 SL/TP 提前出场），42.5% 的行被档位截断，
        会制造大量并列值破坏 rank-IC 尾部排序。
        """
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            price_at_predict REAL, price_t7 REAL, return_t7 REAL,
            checked_t7 INTEGER DEFAULT 0)""")
        # return_t7 故意与真实价格变动不符（模拟止盈档位）
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "price_t7,return_t7,checked_t7) VALUES "
                    "('2026-03-02','A',100.0,110.0,9.945,1)")
        con.commit()
        con.close()
        sa.ensure_schema(db)
        sa.archive({"A": _tr()}, "2026-03-02", db)

        panel = sa.load_panel(db, "t7", min_width=1)
        vals = panel["composite.final_score"]["2026-03-02"]
        assert vals[0][1] == pytest.approx(10.0), \
            "应使用 (110-100)/100=10%，而非 return_t7 的 9.945"


class TestBackfill:
    def test_backfill_parses_date_from_filename(self, tmp_path):
        (tmp_path / ".swarm_results_2026-03-02.json").write_text(
            json.dumps({"NVDA": _tr()}), encoding="utf-8")
        (tmp_path / "no-date.json").write_text("{}", encoding="utf-8")
        db = tmp_path / "p.db"
        st = sa.backfill(".swarm_results_*.json", db)
        assert st["files"] == 1 and st["rows"] > 0
        with sqlite3.connect(db) as c:
            d = c.execute(f"SELECT DISTINCT date FROM {sa.TABLE}").fetchone()[0]
        assert d == "2026-03-02"

    def test_backfill_tolerates_corrupt_file(self, tmp_path):
        (tmp_path / ".swarm_results_2026-03-02.json").write_text("{bad", encoding="utf-8")
        (tmp_path / ".swarm_results_2026-03-03.json").write_text(
            json.dumps({"NVDA": _tr()}), encoding="utf-8")
        st = sa.backfill(".swarm_results_*.json", tmp_path / "p.db")
        assert st["skipped"] == 1 and st["files"] == 1
