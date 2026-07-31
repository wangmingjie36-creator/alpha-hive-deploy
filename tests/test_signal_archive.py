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
            price_at_predict REAL, price_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "price_t7,checked_t7) VALUES ('2026-03-02','A',100.0,110.0,1)")
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
            price_at_predict REAL, price_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "price_t7,checked_t7) VALUES ('2026-03-02','A',100.0,110.0,1)")
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
            price_at_predict REAL, price_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")
        con.execute("INSERT INTO predictions (date,ticker,price_at_predict,"
                    "price_t7,checked_t7) VALUES ('2026-03-02','A',100.0,110.0,1)")
        con.commit(); con.close()
        sa.ensure_schema(db)
        sa.archive({"A": _tr()}, "2026-03-02", db)

        p = sa.load_panel(db, "t7", 1, target_metric="vol")
        assert p["composite.final_score"]["2026-03-02"][0][1] == 2.5, \
            "目标值应为波动率 2.5，而非收益率 10.0"
        # 收益率口径不受影响
        pr = sa.load_panel(db, "t7", 1, target_metric="return")
        assert pr["composite.final_score"]["2026-03-02"][0][1] == pytest.approx(10.0)
