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

import ic_diagnostics as icd
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
    def test_panel_uses_close_t7_not_return_t7_nor_price_t7(self, tmp_path):
        """前瞻收益必须用未截断收盘价 close_t7，return_t7 与 price_t7 都不行。

        return_t7 是路径依赖收益（触 SL/TP 提前出场），被档位截断。
        ⚠️ v0.45.321 更正：本测试原名 `test_panel_uses_pure_price_return`，断言用 price_t7
        算出的 10% ——那正是 bug 本身。price_t7 = exit_price，同样截断（见
        ic_diagnostics.FORWARD_CLOSE_COL；符号相反的回归夹具在 test_signal_archive_close_target）。
        """
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            price_at_predict REAL, price_t7 REAL, return_t7 REAL, close_t7 REAL,
            checked_t7 INTEGER DEFAULT 0)""")
        # 止盈离场：price_t7 = 离场价 110、return_t7 = 钳位值 9.945、收盘 105
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "price_t7,return_t7,close_t7,checked_t7) VALUES "
                    "('2026-03-02','A',100.0,110.0,9.945,105.0,1)")
        con.commit()
        con.close()
        sa.ensure_schema(db)
        sa.archive({"A": _tr()}, "2026-03-02", db)

        panel = sa.load_panel(db, "t7", min_width=1)
        vals = panel["composite.final_score"]["2026-03-02"]
        assert vals[0][1] == pytest.approx(5.0), \
            "应使用 (105-100)/100=5%，而非离场价 110 或 return_t7 的 9.945"



class TestRealObservationGates:
    """v0.45.751：`REAL_OBSERVATION_GATES`——Buzz 价格三通道的探索性 IC 只用输入确实取到的行（用户 11-09 定）。

    输入取不到时生产照写回落值，这些行不是观测。三条闸各自的排除法不同：
    波动率 = 输入入档 + 08-26 前的 0.0 哨兵；动量 = 输入入档 + 通道恰为 50.0；量比 = 输入入档 + 任何日期的 1.0 哨兵。
    变红的变异：闸整个去掉、不排除缺失、不排除哨兵、哨兵截止日写错方向、动量不判回落值、量比按通道 50 判、计数不进报告。"""

    VOL, INP = "buzz.comp.volatility_signal", "price.volatility_20d"
    MOM, MIN = "buzz.comp.momentum_signal", "price.momentum_5d"
    VR, VIN = "buzz.comp.volume_signal", "price.volume_ratio"

    def _db(self, tmp_path, rows):
        """rows：(date, ticker, {信号: 值})；每个 (date, ticker) 都有已结算的 t7。"""
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            price_at_predict REAL, price_t7 REAL, return_t7 REAL, close_t7 REAL,
            checked_t7 INTEGER DEFAULT 0)""")
        con.commit()
        con.close()
        sa.ensure_schema(db)
        con = sqlite3.connect(db)
        for d, tk, sigs in rows:
            con.execute("INSERT INTO predictions (date,ticker,price_at_predict,price_t7,return_t7,close_t7,checked_t7) "
                        "VALUES (?,?,100.0,101.0,1.0,101.0,1)", (d, tk))
            for sig, v in sigs.items():
                con.execute(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)", (d, tk, sig, v))
        con.commit()
        con.close()
        return db

    _ROWS = [
        ("2026-08-20", "A", {VOL: 60.0, INP: 30.0, "composite.final_score": 5.0}),   # 真实观测
        ("2026-08-20", "B", {VOL: 75.0, INP: 0.0, "composite.final_score": 5.0}),    # 08-26 前的缺失哨兵 ⇒ 假 75
        ("2026-08-20", "C", {VOL: 50.0, "composite.final_score": 5.0}),             # 输入没入档
        ("2026-09-01", "D", {VOL: 75.0, INP: 0.0, "composite.final_score": 5.0}),    # 08-26 后的 0 是值，不是缺失
        ("2026-09-01", "E", {VOL: 50.0, "composite.final_score": 5.0}),             # 08-26 后取不到 ⇒ 回落 50
    ]

    # 动量：通道恰为 50.0 ⇒ 回落（与输入是否为 0 无关：Scout 那份输入不是 Buzz 读到的那份）
    _MOM_ROWS = [
        ("2026-09-01", "A", {MOM: 60.0, MIN: 2.0}),     # 真实观测
        ("2026-06-09", "B", {MOM: 50.0, MIN: 0.0}),     # 08-15 前的 0.0 哨兵 ⇒ 通道 50.0
        ("2026-09-11", "C", {MOM: 50.0, MIN: 0.0093}),  # Scout 有值、Buzz 回落（生产 09-11 WMT 的形状）
        ("2026-09-25", "D", {MOM: 61.3}),               # 输入被隔离 / 没入档
        ("2026-09-01", "E", {MOM: 50.05, MIN: 0.01}),   # 贴近 50 但不是 50.0 ⇒ 真实
    ]

    # 量比：1.0 是任何日期的哨兵；通道 50 是真实档位，不能当回落
    _VR_ROWS = [
        ("2026-09-01", "A", {VR: 50.0, VIN: 1.2}),      # 真实观测（50 档）
        ("2026-06-09", "B", {VR: 35.0, VIN: 1.0}),      # 1.0 哨兵（旧）
        ("2026-11-02", "C", {VR: 35.0, VIN: 1.0}),      # 1.0 哨兵（不设截止日：CBOESource 仍写着 `or 1.0`）
        ("2026-11-02", "D", {VR: 50.0}),                # 输入没入档 ⇒ 回落 50
        ("2026-09-01", "E", {VR: 35.0, VIN: 0.9999}),   # 贴近 1.0 但不是 ⇒ 真实
    ]

    @staticmethod
    def _kept(panel, sig):
        return sorted(tk for rows in panel.get(sig, {}).values() for _v, _r, tk in rows)

    def test_only_real_observations_enter_the_panel(self, tmp_path):
        st = {}
        p = sa.load_panel(self._db(tmp_path, self._ROWS), "t7", min_width=1, with_ticker=True, gate_stats=st)
        assert self._kept(p, self.VOL) == ["A", "D"]
        assert st[self.VOL] == {"kept": 2, "missing_input": 2, "quarantined_input": 0, "sentinel": 1, "fallback": 0}, st

    def test_momentum_drops_exact_fallback_value(self, tmp_path):
        st = {}
        p = sa.load_panel(self._db(tmp_path, self._MOM_ROWS), "t7", min_width=1, with_ticker=True, gate_stats=st)
        assert self._kept(p, self.MOM) == ["A", "E"]
        assert st[self.MOM] == {"kept": 2, "missing_input": 1, "quarantined_input": 0, "sentinel": 0, "fallback": 2}, st

    def test_volume_drops_sentinel_at_any_date_but_keeps_real_50(self, tmp_path):
        st = {}
        p = sa.load_panel(self._db(tmp_path, self._VR_ROWS), "t7", min_width=1, with_ticker=True, gate_stats=st)
        assert self._kept(p, self.VR) == ["A", "E"]
        assert st[self.VR] == {"kept": 2, "missing_input": 1, "quarantined_input": 0, "sentinel": 2, "fallback": 0}, st

    def test_quarantined_input_is_excluded_even_if_row_kept(self, tmp_path, monkeypatch):
        """带 `keep_rows` 的隔离条目（10-14 BILI 动量）库里还在；通道是用那个坏输入算的 ⇒ 也排除。
        `load_panel` 本身不问隔离名单，所以闸要自己问——只看「输入在不在」会把它当真实观测放进来。"""
        monkeypatch.setattr(sa, "QUARANTINE", [{"date": "2026-10-14", "tickers": ("BILI",),
                                                "signals": (self.MIN,), "reason": "测试", "keep_rows": "测试"}])
        rows = [("2026-10-14", "BILI", {self.MOM: 52.01, self.MIN: 0.4021}),
                ("2026-10-14", "NVDA", {self.MOM: 55.0, self.MIN: 1.0}),
                ("2026-10-15", "BILI", {self.MOM: 52.01, self.MIN: 0.4021})]
        st = {}
        p = sa.load_panel(self._db(tmp_path, rows), "t7", min_width=1, with_ticker=True, gate_stats=st)
        kept = sorted((d, tk) for d, rs in p[self.MOM].items() for *_x, tk in rs)
        assert kept == [("2026-10-14", "NVDA"), ("2026-10-15", "BILI")], kept
        assert st[self.MOM]["quarantined_input"] == 1, st
        raw = sorted((d, tk) for d, rs in p[self.MIN].items() for *_x, tk in rs)
        assert raw == [("2026-10-14", "NVDA"), ("2026-10-15", "BILI")], (
            "隔离的输入本身也不进面板（keep_rows 只是不删库，分析照样排除）", raw)

    def test_real_keep_rows_entries_hit_a_gate_input(self):
        """真名单：带 `keep_rows`、点名闸输入的条目（现为 10-14 BILI 动量）确实会被闸接住——防名单改了、闸的说法没跟上。"""
        gate_inputs = {g["input"] for g in sa.REAL_OBSERVATION_GATES.values()}
        hits = [(q["date"], s) for q in sa.QUARANTINE if q.get("keep_rows") for s in q["signals"] if s in gate_inputs]
        assert hits, "已没有带 keep_rows 的闸输入隔离条目 —— 上一条测试的合成场景已不对应生产，可降为纯机制测试"
        for d, s in hits:
            q = next(q for q in sa.QUARANTINE if q["date"] == d and s in q["signals"] and q.get("keep_rows"))
            for tk in q.get("tickers") or ("X",):
                assert sa.is_quarantined(d, tk, s), (d, tk, s)

    def test_other_signals_untouched(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        p = sa.load_panel(self._db(tmp_path / "a", self._ROWS), "t7", min_width=1, with_ticker=True)
        assert self._kept(p, "composite.final_score") == list("ABCDE")
        assert self._kept(p, self.INP) == list("ABD")
        p = sa.load_panel(self._db(tmp_path / "b", self._VR_ROWS), "t7", min_width=1, with_ticker=True)
        assert self._kept(p, self.VIN) == list("ABCE"), "输入信号本身不受闸影响（哨兵 1.0 照样在 price.volume_ratio 里）"

    def test_analyze_reports_the_exclusions(self, tmp_path, capsys):
        st = {}
        sa.load_panel(self._db(tmp_path, self._ROWS + self._MOM_ROWS + self._VR_ROWS), "t7", min_width=1, gate_stats=st)
        sa._print_generations([], {"signals": {}, "undeclared_versions": [], "real_obs_gates": st})
        out = capsys.readouterr().out
        assert "真实观测闸：buzz.comp.volatility_signal" in out and "2026-08-26 前的缺失哨兵 0 1 行" in out, out
        assert "真实观测闸：buzz.comp.momentum_signal" in out and "通道恰为回落值 50 2 行" in out, out
        assert "真实观测闸：buzz.comp.volume_signal" in out and "任何日期的缺失哨兵 1 2 行" in out, out

    def test_analyze_carries_gate_stats(self, tmp_path, monkeypatch):
        """`analyze()` 的报告里带 `real_obs_gates`（排除不能只发生、不出声）。"""
        seen = {}
        real = sa.load_panel

        def _spy(*a, **k):
            out = real(*a, **k)
            seen["stats"] = k.get("gate_stats")
            return out
        monkeypatch.setattr(sa, "load_panel", _spy)
        sa.analyze(self._db(tmp_path, self._ROWS), "t7", min_samples=1, min_width=1, draws=5)
        assert seen["stats"] is not None and set(sa.REAL_OBSERVATION_GATES) <= set(seen["stats"]), seen

    def test_registry_matches_the_code(self):
        """登记的每个闸：两个信号都入档、字段齐；三条与 buzz_bee 的回落写法对得上。"""
        assert set(sa.REAL_OBSERVATION_GATES) == {self.VOL, self.MOM, self.VR}
        for sig, gate in sa.REAL_OBSERVATION_GATES.items():
            assert set(gate) == {"input", "sentinel", "sentinel_before", "fallback_value"}, (sig, gate)
            assert sig in sa.SIGNAL_EXTRACTORS and gate["input"] in sa.SIGNAL_EXTRACTORS, (sig, gate)
            if gate["sentinel_before"]:
                assert gate["sentinel"] is not None, sig
                datetime.date.fromisoformat(gate["sentinel_before"])
        src = (sa.Path(sa.__file__).parent / "swarm_agents" / "buzz_bee.py").read_text(encoding="utf-8")
        assert 'vol20 = stock.get("volatility_20d")' in src and "if vol20 is None:\n                vol_sentiment = 50" in src, (
            "buzz_bee 的波动率回落写法变了 —— 重审 REAL_OBSERVATION_GATES 那条")
        # 动量按通道值判，前提是「有值时只有 0 映射到 50.0」——映射式变了这条就不成立
        assert ('_mom_raw = stock.get("momentum_5d")\n            if _mom_raw is None:\n                momentum_sentiment = 50.0'
                in src and "momentum_sentiment = (momentum_pct + 10) / 20 * 100" in src), (
            "buzz_bee 的动量回落 / 映射写法变了 —— 重审 REAL_OBSERVATION_GATES 动量那条的 fallback_value")
        assert sa.REAL_OBSERVATION_GATES[self.MOM]["fallback_value"] == 50.0
        # 量比的 50 是真实档位 ⇒ 只能按输入判，不能按通道值判
        assert "if vol_ratio is None:\n                volume_signal = 50" in src and "volume_signal = 50\n            elif" in src
        assert sa.REAL_OBSERVATION_GATES[self.VR]["fallback_value"] is None, "量比通道 50 是真实档位，不能按回落值排除"

    def test_dim_ic_does_not_read_through_the_gate(self):
        """维度 IC（H1 / 描述项 buzz_v1）不走 load_panel：闸只管探索性 IC，冻结层仍逐位复现生产写下的通道。"""
        src = (sa.Path(sa.__file__).parent / "experiments" / "dim_ic_forward_test.py").read_text(encoding="utf-8")
        assert "load_panel" not in src and "REAL_OBSERVATION_GATES" not in src


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


class TestBackfillOnlyAndDryRun:
    """v0.45.250：全量回填会用**当前**抽取器重写全部历史文件。

    实测（生产库副本，2026-09-15）：除了要补的 fund.*，它还会新增 1,484 行
    `guard.consistency_census`，其中 1,394 行是 v0.45.163 之前的旧口径 ——
    正是 v0.45.182 改名要防的「两段定义被池化」。故补某几个信号必须能限定范围，
    写库前必须能先看行数。
    """

    @staticmethod
    def _dir(tmp_path, final_score=6.5):
        (tmp_path / ".swarm_results_2026-03-02.json").write_text(
            json.dumps({"NVDA": _tr(final_score=final_score), "TSLA": _tr()}),
            encoding="utf-8")
        return tmp_path / "p.db"

    @staticmethod
    def _signals(db):
        with sqlite3.connect(db) as c:
            return {r[0] for r in c.execute(f"SELECT DISTINCT signal FROM {sa.TABLE}")}

    def test_only_restricts_written_signals(self, tmp_path):
        db = self._dir(tmp_path)
        st = sa.backfill(db_path=db, only={"composite.final_score"})
        assert self._signals(db) == {"composite.final_score"}
        assert st["rows"] == 2

    def test_only_rejects_unknown_signal_name(self, tmp_path):
        """拼错名字 ⇒ 静默「回填了 0 行」看起来像「本来就没缺」。必须抛。"""
        db = self._dir(tmp_path)
        with pytest.raises(ValueError, match="fund.market_cpa"):
            sa.backfill(db_path=db, only={"fund.market_cpa"})

    def test_dry_run_on_missing_db_creates_nothing(self, tmp_path):
        db = self._dir(tmp_path)
        st = sa.backfill(db_path=db, only={"composite.final_score"}, dry_run=True)
        assert (st["new"], st["changed"], st["same"]) == (2, 0, 0)
        assert not db.exists(), "dry-run 不许建库"

    def test_dry_run_classifies_new_changed_same_and_writes_nothing(self, tmp_path):
        db = self._dir(tmp_path, final_score=7.0)
        sa.ensure_schema(db)
        with sqlite3.connect(db) as c:        # NVDA 旧值不同、TSLA 旧值相同
            c.executemany(f"INSERT INTO {sa.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)",
                          [("2026-03-02", "NVDA", "composite.final_score", 6.0),
                           ("2026-03-02", "TSLA", "composite.final_score", 6.5)])
        st = sa.backfill(db_path=db, only={"composite.final_score", "crowding.score"},
                         dry_run=True)
        assert (st["new"], st["changed"], st["same"]) == (2, 1, 1)
        assert st["by_signal"]["crowding.score"]["new"] == 2
        with sqlite3.connect(db) as c:
            rows = c.execute(f"SELECT ticker, value FROM {sa.TABLE} ORDER BY ticker").fetchall()
        assert rows == [("NVDA", 6.0), ("TSLA", 6.5)], "dry-run 改了库"

    def test_dry_run_predicts_the_real_run(self, tmp_path):
        """**成对**：dry-run 报的数必须就是真跑会写的数 —— 真跑之后再 dry-run 应一行不剩。"""
        db = self._dir(tmp_path)
        only = {"composite.final_score", "crowding.score"}
        predicted = sa.backfill(db_path=db, only=only, dry_run=True)
        real = sa.backfill(db_path=db, only=only)
        assert real["rows"] == predicted["new"] + predicted["changed"] + predicted["same"]
        after = sa.backfill(db_path=db, only=only, dry_run=True)
        assert (after["new"], after["changed"]) == (0, 0)

    def test_quarantine_still_applies_under_only(self, tmp_path):
        (tmp_path / ".swarm_results_2026-08-24.json").write_text(json.dumps({"NVDA": {
            "agent_details": {"OracleBeeEcho": {"details": {"iv_current": 0.5}}}}}),
            encoding="utf-8")
        st = sa.backfill(db_path=tmp_path / "p.db", only={"options.iv_current"}, dry_run=True)
        assert st["new"] == 0, "隔离名单在 only/dry-run 路径上被绕过"


class TestFixedVsTimeVaryingDecomposition:
    """v0.43.6：区分「选股标签」与「择时信号」

    常规 IC 无法区分两种性质完全不同的信号，而它们长得一模一样：
      • 固定效应主导 = 某类标的的身份标记，不随时间提供新信息
        → 只能做筛选池；塞进每日评分等于每天重新发现「MSFT 是大盘股」
      • 票内时变主导 = 同一标的相对自身均值的波动有预测力
        → 才适合做每日评分

    2026-07-30 实测的两个反例：
      risk_adj      固定 +0.006 / 时变 −0.161  ⇒ 100% 择时
      crowding_score 全样本驼峰形显著且样本外延续，但票内去均值后**完全消失**
                     ⇒ 100% 固定效应，当每日信号用是错的
    """

    def _panel(self, kind, n_days=30, n_tickers=8):
        """构造已知性质的合成面板

        kind="fixed"  : 值只由 ticker 决定（票内无变化），收益也只由 ticker 决定
        kind="timing" : 每票均值相同，值围绕均值波动，收益跟随波动
        kind="none"   : 值与收益均随机
        """
        import random as _r
        rng = _r.Random(11)
        out = {}
        d0 = datetime.datetime.fromisoformat("2026-03-02").date()
        made = i = 0
        base = {f"T{j}": float(j) for j in range(n_tickers)}
        while made < n_days:
            d = d0 + datetime.timedelta(days=i)
            i += 1
            if d.weekday() >= 5:
                continue
            made += 1
            rows = []
            for tk in base:
                if kind == "fixed":
                    v = base[tk]
                    ret = base[tk] + rng.gauss(0, 0.3)
                elif kind == "timing":
                    dev = rng.gauss(0, 1)
                    v = 5.0 + dev
                    ret = dev * 2 + rng.gauss(0, 0.3)
                else:
                    v = rng.gauss(0, 1)
                    ret = rng.gauss(0, 1)
                rows.append((v, ret, tk))
            out[d.isoformat()] = rows
        return out

    def test_pure_fixed_effect_is_labeled_selection(self):
        r = sa.decompose_fixed_vs_timevarying(self._panel("fixed"))
        assert r["nature"] == "选股标签", r
        assert abs(r["ic_fixed"]) > abs(r["ic_within"])
        # 票内完全无变化 → 必须显式标记，而非留一个含义模糊的 nan
        assert r["no_within_variation"] is True
        assert r["ic_within"] == 0.0

    def test_pure_time_varying_is_labeled_timing(self):
        r = sa.decompose_fixed_vs_timevarying(self._panel("timing"))
        assert r["nature"] == "择时", r
        assert abs(r["ic_within"]) > abs(r["ic_fixed"])

    def test_pure_noise_is_labeled_none(self):
        r = sa.decompose_fixed_vs_timevarying(self._panel("none"))
        assert r["nature"] == "无", r

    def test_too_few_tickers_returns_empty(self):
        """标的太少时无法可靠估计票均值，应返回空而非给出可疑判定"""
        p = {"2026-03-02": [(1.0, 1.0, "A"), (2.0, 2.0, "B")]}
        assert sa.decompose_fixed_vs_timevarying(p) == {}

    def test_load_panel_with_ticker_returns_triples(self, tmp_path):
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            price_at_predict REAL, close_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "close_t7,checked_t7) VALUES ('2026-03-02','A',100.0,110.0,1)")
        con.commit(); con.close()
        sa.ensure_schema(db)
        sa.archive({"A": _tr()}, "2026-03-02", db)

        pairs = sa.load_panel(db, "t7", min_width=1)
        triples = sa.load_panel(db, "t7", min_width=1, with_ticker=True)
        k = "composite.final_score"
        assert len(pairs[k]["2026-03-02"][0]) == 2, "默认必须返回二元组（向后兼容）"
        assert len(triples[k]["2026-03-02"][0]) == 3
        assert triples[k]["2026-03-02"][0][2] == "A"


class TestSplitStability:
    """v0.43.7：训练/测试分段 —— 检测全样本 IC 是否只是异号平均

    2026-07-30 实测：综合分全样本 IC=−0.09 看似稳定弱负，
    切开后训练 −0.214(t=−5.28) / 测试 +0.025(t=+0.46)，**符号相反**。
    那个 −0.09 描述的不是效应，是两段异号数据的中间值。
    """

    def _panel(self, kind, n_days=40, width=8):
        import random as _r
        rng = _r.Random(3)
        out = {}
        d0 = datetime.datetime.fromisoformat("2026-03-02").date()
        made = i = 0
        while made < n_days:
            d = d0 + datetime.timedelta(days=i); i += 1
            if d.weekday() >= 5:
                continue
            made += 1
            # 后 40% 为测试期
            late = made > n_days * 0.6
            rows = []
            for _ in range(width):
                x = rng.gauss(0, 1)
                if kind == "stable":
                    ret = x * 2 + rng.gauss(0, 0.5)
                elif kind == "flip":
                    ret = (-x if late else x) * 2 + rng.gauss(0, 0.5)
                elif kind == "decay":
                    ret = (x * 0.15 if late else x * 2) + rng.gauss(0, 0.5)
                else:
                    ret = rng.gauss(0, 1)
                rows.append((x, ret))
            out[d.isoformat()] = rows
        return out

    def test_stable_signal_labeled_stable(self):
        r = sa.split_stability(self._panel("stable"), floor=0.077)
        assert r["stability"] == "稳定", r

    def test_sign_flip_is_caught(self):
        """核心：符号翻转必须被标出 —— 否则全样本 IC 会误导所有下游结论"""
        r = sa.split_stability(self._panel("flip"), floor=0.077)
        assert r["stability"] == "翻转", r
        assert (r["ic_train"] > 0) != (r["ic_test"] > 0)

    def test_decay_is_distinguished_from_stable(self):
        r = sa.split_stability(self._panel("decay"), floor=0.077)
        assert r["stability"] == "衰减", r

    def test_pure_noise_labeled_noise(self):
        r = sa.split_stability(self._panel("noise"), floor=0.077)
        assert r["stability"] in ("均噪音", "翻转"), r

    def test_too_few_days_returns_insufficient(self):
        r = sa.split_stability(self._panel("stable", n_days=8), floor=0.077)
        assert r["stability"] == "样本不足"

    def test_train_test_are_disjoint_and_ordered(self):
        """训练必须在测试之前 —— 否则是前视"""
        p = self._panel("stable", n_days=40)
        r = sa.split_stability(p, floor=0.077, train_frac=0.6)
        assert r["n_train"] > 0 and r["n_test"] > 0
        assert r["n_train"] + r["n_test"] <= len(p)


class TestVolatilityTarget:
    """v0.43.8：波动率预测目标

    2026-07-30 实测，同一宇宙（90 只）× 897 交易日 × 同样朴素的特征：
        未来 7 日**收益率**   ← 20 日动量   IC = +0.012  (t=+1.7)
        未来 7 日**已实现波动** ← 60 日波动  IC = **+0.710** (t=+288.6)

    波动率的可学性高 60 倍。这不是过拟合，是波动率聚集。
    含义：系统当前在预测一个 IC≈0.01 的目标，天花板与架构无关。
    """

    def test_target_metrics_declared(self):
        assert icd is not None  # 确保模块导入链完好
        assert sa.TARGET_METRICS == ("return", "vol")

    def test_load_panel_default_is_return(self):
        """默认必须是 return —— 换目标是显式选择，不能静默改变既有分析"""
        import inspect
        sig = inspect.signature(sa.load_panel)
        assert sig.parameters["target_metric"].default == "return"
        assert inspect.signature(sa.analyze).parameters["target_metric"].default == "return"

    def test_vol_target_degrades_gracefully_without_network(self, tmp_path, monkeypatch):
        """行情不可用时返回空面板，不得崩溃或退回错误的目标"""
        monkeypatch.setattr(sa, "_forward_realized_vol", lambda *a, **k: {})
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            price_at_predict REAL, close_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "close_t7,checked_t7) VALUES ('2026-03-02','A',100.0,110.0,1)")
        con.commit(); con.close()
        sa.ensure_schema(db)
        sa.archive({"A": _tr()}, "2026-03-02", db)
        assert sa.load_panel(db, "t7", 1, target_metric="vol") == {}

    def test_vol_target_uses_same_sample_keys(self, tmp_path, monkeypatch):
        """波动率目标必须沿用同一批 (ticker, date)，否则与收益率口径不可对照"""
        fake_vol = {("A", "2026-03-02"): 2.5}
        monkeypatch.setattr(sa, "_forward_realized_vol", lambda *a, **k: fake_vol)
        db = tmp_path / "p.db"
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
            price_at_predict REAL, close_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "close_t7,checked_t7) VALUES ('2026-03-02','A',100.0,110.0,1)")
        con.commit(); con.close()
        sa.ensure_schema(db)
        sa.archive({"A": _tr()}, "2026-03-02", db)

        p = sa.load_panel(db, "t7", 1, target_metric="vol")
        assert p["composite.final_score"]["2026-03-02"][0][1] == 2.5, \
            "目标值应为波动率 2.5，而非收益率 10.0"
        # 收益率口径不受影响
        pr = sa.load_panel(db, "t7", 1, target_metric="return")
        assert pr["composite.final_score"]["2026-03-02"][0][1] == pytest.approx(10.0)


class TestPriceCacheKey:
    """_PX_CACHE 的键必须反映**完整**日期集合，不能只用首尾两天。

    旧键 = (tickers, dates[0], dates[-1], fwd_days)。同进程内若两次调用
    首尾相同但中间不同（例如新样本回填后重查），第二次会静默复用陈旧
    结果、丢掉中间日期 —— 属于本项目最常见的"看着成功其实早废了"形态。
    """

    #: 直接引用生产代码的键函数。**不要在这里重抄一遍实现**——
    #: 那样测的是测试自己，生产代码改回旧写法照样绿。
    _key = staticmethod(sa._px_cache_key)

    def test_same_endpoints_different_length_no_collision(self):
        tk = ["AAPL", "MSFT"]
        a = ["2026-07-01", "2026-07-15"]
        b = ["2026-07-01", "2026-07-08", "2026-07-15"]
        assert self._key(tk, a, 7) != self._key(tk, b, 7)

    def test_same_endpoints_same_length_different_middle_no_collision(self):
        """len() 单独救不了的情形：首尾与长度都相同，只有中间那天不同。"""
        tk = ["AAPL", "MSFT"]
        b = ["2026-07-01", "2026-07-08", "2026-07-15"]
        c = ["2026-07-01", "2026-07-09", "2026-07-15"]
        assert self._key(tk, b, 7) != self._key(tk, c, 7)

    def test_identical_inputs_still_hit(self):
        """修复不能把缓存打废：完全相同的输入必须仍然命中。"""
        tk = ["AAPL", "MSFT"]
        d = ["2026-07-01", "2026-07-08"]
        assert self._key(tk, d, 7) == self._key(list(reversed(tk)), list(d), 7)

    def test_fwd_days_still_separates(self):
        tk, d = ["AAPL"], ["2026-07-01", "2026-07-08"]
        assert self._key(tk, d, 7) != self._key(tk, d, 30)


# ══════════════════════════════════════════════════════════════════════════
# v0.45.182：口径变了的信号必须改名，不能沿用旧名
#
# v0.45.163 把 GuardBee 的 risk_adj 改走普查读法，`guard.consistency` 与
# `guard.top_signals_count` 两条归档序列在 2026-09-08 **换了定义**（逐扫描日
# 实测：count 3.43~4.33 → 恒 6.00；consistency 0.50~0.72 → 0.47~0.49），
# 而当时只在 analysis JSON 里加了 `census_source` —— `signal_archive` 根本不抽
# 那个字段（实测归档里 0 行），`analyze()` 也既不按日期、也不按 `_COHORT_HISTORY`
# 切片，`load_panel` 直接把整张表拉进来 ⇒ 两段定义被池化。
#
# ⚠️ 判据：**名字变了，还是「量」变了？**
#   · 同一个量换名字（`stocktwits_volume` → `social_volume`）⇒ 合并读，
#     见 `_crowding_comp` 的 `_legacy` 映射。
#   · 同一个名字换量（本条）⇒ **必须拆成两条序列**。
#
# 为什么改名而不是加一列口径标记：
#   ① `value` 列是 REAL，存不下字符串标签，得先编码成数字 —— 又是一件要记住的事；
#   ② 加列等于要求每个消费方**记得**去 join，忘了就退回同一个静默 bug。
#      改名之后「忘了」在结构上不可能发生：两条序列不同名，自然不会被池化。
#   ③ `experiments/signal_ic_sweep_report.md:47` 已经把 `guard.consistency`
#      的 IC(+0.134 / t +2.12) 发表在**旧定义**上；同名延续会让下一个人拿两个量比。
# ══════════════════════════════════════════════════════════════════════════

#: 口径已变、**不得再被写入**的旧信号名。旧行留在库里（历史数据不改写），
#: 但新数据必须走新名字。这是**黑名单**语义：我怕的是它变回去。
RETIRED_SIGNAL_NAMES = {
    "guard.consistency",        # v0.45.163 起分母由「窗口条数 ≤5」变为「本轮蜂数」
    "guard.top_signals_count",  # v0.45.163 起恒为蜂数，已非信号；改挂分布不变式
}


def test_retired_signal_names_are_not_reused():
    from signal_archive import SIGNAL_EXTRACTORS
    clash = RETIRED_SIGNAL_NAMES & set(SIGNAL_EXTRACTORS)
    assert not clash, (
        f"{sorted(clash)} 的口径在 2026-09-08 变过，沿用旧名会让 analyze() "
        "把两段定义池化 —— 换个名字，别加判别列"
    )


def test_renamed_consistency_signal_is_present():
    """成对断言：退役不等于删掉——新口径必须有自己的序列，否则是悄悄丢了一个信号。"""
    from signal_archive import SIGNAL_EXTRACTORS
    assert "guard.consistency_census" in SIGNAL_EXTRACTORS, (
        "只退役了旧名却没建新序列 —— GuardBee 的一致性从此不再被归档"
    )


# ══════════════════════════════════════════════════════════════════════════
# v0.45.256：改名只拆开了「新数据」，没拆开「回填」
#
# v0.45.182 把名字换成 `guard.consistency_census`，但抽取器仍是无条件的
# `_path("...GuardBeeSentinel.details.consistency")`。`backfill()` 用**当前**抽取器
# 重写全部历史 `.swarm_results_*.json` ⇒ 一次全量回填就把 v0.45.163 之前的旧口径
# 写进新名字，v0.45.182 拆开的两段定义被重新池化。
# 生产库副本 dry-run 实测：会新增 1,484 行，其中 **1,394 行 `census_source` 缺失**
# （2026-03 ~ 09-04 各月都有），90 行 `live_agent_view`。
#
# 口径判别照 `tests/test_distribution_invariants.py::census_coverage_offenders`：
# 只认 `census_source == "live_agent_view"`（**白名单**，不是「非空即可」）。
# ══════════════════════════════════════════════════════════════════════════

def _guard_tr(census_source="__absent__", consistency=0.5):
    det = {"consistency": consistency, "adjustment_factor": 1.2, "macro_adj": -0.5}
    if census_source != "__absent__":
        det["census_source"] = census_source
    return {"final_score": 6.0,
            "agent_details": {"GuardBeeSentinel": {
                "score": 5.5, "direction": "neutral", "details": det}}}


class TestConsistencyCensusIsCaliberGated:

    def test_old_caliber_is_not_archived_under_new_name(self):
        """v0.45.163 之前的行没有 `census_source` —— 分母是排行榜窗口条数，不是蜂数。"""
        out = sa.extract(_guard_tr())
        assert "guard.consistency_census" not in out, (
            "旧口径（无 census_source）的 consistency 被写进了新名字 —— "
            "全量 --backfill 会把 v0.45.182 拆开的两段定义重新池化"
        )

    def test_live_agent_view_is_archived(self):
        """成对断言：没有这条，「干脆删掉这个信号」也能让上一条全绿。"""
        out = sa.extract(_guard_tr("live_agent_view", consistency=4 / 6))
        assert out.get("guard.consistency_census") == pytest.approx(4 / 6)

    @pytest.mark.parametrize("src", ["top_signals_fallback", "unavailable", None, ""])
    def test_other_calibers_are_not_archived(self, src):
        """白名单不是黑名单：把判据写成「census_source 非空」时，前两条照样全绿。

        · `top_signals_fallback`：窗口 24 的排行榜，既不是旧口径也不是普查；
        · `unavailable`：板读取失败，`consistency` 是**兜底写的 0**，不是观测值。
        """
        assert "guard.consistency_census" not in sa.extract(_guard_tr(src))

    def test_sibling_guard_signals_are_not_gated(self):
        """不许顺手把整个 `guard.*` 都按口径门控。

        `adj_factor` 与 `macro_adj` 在 v0.45.163 没有换量（生产实测：同一 30 只标的池，
        08-24~09-04 vs 09-08~09-14，1.2 档 76.7%→78.7%、0.95 档 21.7%→21.3%；
        `macro_adj` 不走读板路径）。门控它们 = 白白丢掉 1,394 行同口径历史。
        """
        out = sa.extract(_guard_tr())
        assert out.get("guard.adj_factor") == 1.2
        assert out.get("guard.macro_adj") == -0.5

    def test_backfill_does_not_repool_old_caliber(self, tmp_path):
        """症状本身：全量回填后，新名字下只能有新口径日期。"""
        (tmp_path / ".swarm_results_2026-09-04.json").write_text(
            json.dumps({"NVDA": _guard_tr(consistency=0.72)}), encoding="utf-8")
        (tmp_path / ".swarm_results_2026-09-08.json").write_text(
            json.dumps({"NVDA": _guard_tr("live_agent_view", consistency=0.47)}),
            encoding="utf-8")
        db = tmp_path / "p.db"
        st = sa.backfill(".swarm_results_*.json", db)
        assert st["files"] == 2
        with sqlite3.connect(db) as c:
            census = c.execute(
                f"SELECT date, value FROM {sa.TABLE} "
                "WHERE signal='guard.consistency_census' ORDER BY date").fetchall()
            old_day = {s for (s,) in c.execute(
                f"SELECT signal FROM {sa.TABLE} WHERE date='2026-09-04'")}
        assert census == [("2026-09-08", pytest.approx(0.47))], census
        # 前提自证：旧日期那份文件确实被读进来了，只是这一条被拦下 ——
        # 否则「旧文件整份没读到」也会让上面那条断言成立。
        assert "guard.adj_factor" in old_day, old_day
        assert not old_day & RETIRED_SIGNAL_NAMES, "退役名不许借回填复活"
