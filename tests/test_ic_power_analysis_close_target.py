"""
experiments/ic_power_analysis.py 的前瞻收益终点列（v0.45.327）

背景：第 2 步（`observed_weekly_var`，显式 `target="price"`）与第 3 步（`_load_day_pairs`，
拼 `f"price_{horizon}"`）都读 `price_t7`——那是 `_simulate_trade_path` 的 `exit_price`
（SL/TP 离场价），与 ic_diagnostics（v0.45.19）、signal_archive（v0.45.321）、
ml_expected_return_replay（v0.45.326）同一个误解。2026-09-23 快照上的后果（详见
CHANGELOG v0.45.327）：报告里「signal 是唯一能检测到时间变异的维度、倍数只有 1.78×」
是截断造的——改读 close_t7 后 signal 的周度 IC 方差落回置换零分布 90% 带内，σ_t² 占比
43.7% → 17.6%、倍数 1.83× → 2.98×；另 4 维仍钳 0，头条 5.18× 不变。

核心夹具针对的是本脚本的**头条统计量**（σ_t² 是否钳 0 ⇒ 扩池倍数），不是 IC 符号：
方差对符号不敏感（Var(−X) = Var(X)），v0.45.321/326 那种「两列 IC 反号」的夹具在这里
照样绿。所以本夹具让两列给出**相反的时间结构**——

  · close_t7：IC 逐周交替 +1 / −1（因子有效性随时间翻转）⇒ 周度方差 ≈ 1 ≫ 置换零分布
    ≈ 1/7 ⇒ σ_t² 占主导、**不**钳 0、扩池倍数 ≈ 1×（扩池帮不上忙）；
  · price_t7：翻转那几周里高分票先冲过止盈（离场 +10%）再收跌、低分票先跌穿止损
    （离场 −5%）再收涨 ⇒ 离场价的 IC 恒为正、周间几乎不动 ⇒ 周度方差 ≈ 0 ⇒ 钳 0、
    倍数 = 1/缩放系数 ≈ 5.7×（「扩池缩短 5 倍」）。

回退到旧列 ⇒ 头条结论从「扩池无济于事」变成「扩池缩短 5.7 倍」——不是数值漂移。
"""

import datetime
import importlib.util
import json
import sqlite3
import statistics
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "ic_power_analysis", _ROOT / "experiments" / "ic_power_analysis.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


icp = _load_module()

DIMS = ["signal", "catalyst", "sentiment", "odds", "risk_adj"]
TICKERS = [f"T{i}" for i in range(8)]
N_WEEKS = 8                      # 每周两天（周一、周二），周一是不重叠取样日


def _week_days():
    """[(周序号, 日期)]：从 2026-06-01（周一）起 N_WEEKS 周，每周周一、周二。"""
    start = datetime.date(2026, 6, 1)
    out = []
    for w in range(N_WEEKS):
        mon = start + datetime.timedelta(weeks=w)
        out += [(w, mon.isoformat()), (w, (mon + datetime.timedelta(days=1)).isoformat())]
    return out


def _create_table(con):
    con.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        dimension_scores TEXT, price_at_predict REAL, price_t7 REAL, close_t7 REAL,
        exit_price REAL, exit_reason TEXT, return_t7 REAL, checked_t7 INTEGER DEFAULT 0)""")


def _insert(con, date, ticker, score, *, pap, price_t7, close_t7, reason):
    ds = json.dumps({d: score for d in DIMS})
    ret_path = None if price_t7 is None else (price_t7 / pap - 1) * 100
    con.execute(
        "INSERT INTO predictions (date,ticker,dimension_scores,price_at_predict,price_t7,"
        "close_t7,exit_price,exit_reason,return_t7,checked_t7) VALUES (?,?,?,?,?,?,?,?,?,1)",
        (date, ticker, ds, pap, price_t7, close_t7, price_t7, reason, ret_path))


def _build_flipping_db(tmp_path):
    """close_t7 的 IC 逐周翻转（+1/−1）；price_t7（= 离场价）的 IC 周周为正。

    分数 1..8 两两不同（五个维度共用同一套分数，每维都走同一条判定）。
    偶数周：收盘收益 = +分数（IC=+1），不触 SL/TP，离场价 = 收盘价。
    奇数周：收盘收益 = −分数（IC=−1）；高分半先冲止盈（离场 110）、低分半先破止损（离场 95）
            ⇒ 离场价与分数同向（两档并列，IC ≈ +0.87）。
    """
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    _create_table(con)
    for w, d in _week_days():
        for i, tk in enumerate(TICKERS):
            score = float(i + 1)
            if w % 2 == 0:
                close = 100.0 + score
                exit_px, reason = close, "T7_CLOSE"
            else:
                close = 100.0 - score
                exit_px, reason = (110.0, "TP") if score > 4 else (95.0, "SL")
            _insert(con, d, tk, score, pap=100.0, price_t7=exit_px, close_t7=close,
                    reason=reason)
    con.commit()
    con.close()
    return db


def _run_json(monkeypatch, capsys, db):
    monkeypatch.setattr(sys, "argv", ["ic_power_analysis.py", "--db", str(db), "--json",
                                      "--no-network", "--draws", "40"])
    assert icp.main() == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


class TestFixtureDiscriminates:
    """没有这一组，下面「不钳 0 / 倍数 ≈ 1」可能只是夹具本来就分不出两列。

    直接对夹具库算周度 IC 方差（不经被测脚本，右边的真值是独立来源）。
    """

    @pytest.mark.parametrize("col, big", [("close_t7", True), ("price_t7", False)])
    def test_columns_give_opposite_time_structure(self, tmp_path, col, big):
        with sqlite3.connect(_build_flipping_db(tmp_path)) as c:
            rows = c.execute(f"SELECT date, dimension_scores, ({col} / price_at_predict - 1) "
                             f"FROM predictions").fetchall()
        by_day = {}
        for d, ds, ret in rows:
            by_day.setdefault(d, []).append((json.loads(ds)["signal"], ret))
        weekly = {}
        for d in sorted(by_day):
            wk = datetime.date.fromisoformat(d).isocalendar()[:2]
            if wk not in weekly:
                pairs = by_day[d]
                weekly[wk] = icp.spearman([s for s, _ in pairs], [r for _, r in pairs])
        assert len(weekly) == N_WEEKS
        var = statistics.variance(list(weekly.values()))
        assert (var > 0.5) if big else (var < 0.05), f"{col}: 周度 IC 方差 {var:.4f}"


class TestHeadlineFollowsClose:
    """脚本的**头条**（σ_t² 是否钳 0、扩池倍数）必须建立在 close_t7 上。

    ⚠️ 本组只断言统计量本身、不碰 `forward_close_col` / JSON 新键——这样拿改动前的
    脚本原样来跑，红的理由就是「结论对着离场价算」，而不是「新属性不存在」。
    """

    def test_time_variation_is_detected_and_multiplier_collapses(self, tmp_path, monkeypatch,
                                                                 capsys):
        res = _run_json(monkeypatch, capsys, _build_flipping_db(tmp_path))
        scale = res["scales"]["neff"]
        assert 0.1 < scale < 0.3, f"缩放系数 {scale}：--no-network 记忆值 N_eff 口径应约 0.176"
        for dim in DIMS:
            row = res["decomposition"][dim]
            assert row["var_obs"] > 0.5, (
                f"{dim}: 周度 IC 实测方差 {row['var_obs']:.4f}——close_t7 下 IC 逐周翻转、方差≈1；"
                f"≈0 说明第 2 步读的是 price_t7 离场价（见 ic_diagnostics.FORWARD_CLOSE_COL）")
            assert row["censored"] is False and row["t_share"] > 0.5, (
                f"{dim}: σ_t² 被钳 0 / 占比 {row['t_share']:.1%}——时间变异被截断收益抹平了")
            assert row["multiplier_neff"] < 2.0, (
                f"{dim}: 扩池倍数 {row['multiplier_neff']:.2f}×——对着离场价算时这里是 "
                f"1/缩放系数 ≈ {1 / scale:.1f}×")

    def test_text_table_has_no_censored_marker(self, tmp_path, monkeypatch, capsys):
        """文本层：人读到的是「⚠钳0」标记。close 口径下五维都检测得到时间变异，一个都不该钳。"""
        monkeypatch.setattr(sys, "argv", ["ic_power_analysis.py", "--db",
                                          str(_build_flipping_db(tmp_path)),
                                          "--no-network", "--draws", "40"])
        assert icp.main() == 0
        out = capsys.readouterr().out
        assert "【第 2~3 步】" in out
        assert "⚠钳0" not in out, "时间变异被截断收益抹平 ⇒ σ_t² 钳 0（对着离场价算的）"

    def test_power_table_needs_more_weeks_under_close(self, tmp_path, monkeypatch, capsys):
        """功效表（喂给 ic_rerun_readiness._WEEKS_REQUIRED 的那张）：σ_t² 不可约 ⇒ 30 只
        所需周数与 10 只相差不到 2 倍；旧口径钳 0 后相差 ≈5.7 倍。"""
        res = _run_json(monkeypatch, capsys, _build_flipping_db(tmp_path))
        for dim in DIMS:
            cell = res["power"][dim]["0.2"]
            ratio = cell["weeks_10"] / cell["weeks_30"]
            assert ratio < 2.0, f"{dim}: 10 只 / 30 只所需周数之比 {ratio:.2f}"


class TestLoadDayPairsValues:
    def _db(self, tmp_path, rows):
        db = tmp_path / "p.db"
        with sqlite3.connect(db) as con:
            _create_table(con)
            for tk, score, pap, p7, c7, reason in rows:
                _insert(con, "2026-06-01", tk, score, pap=pap, price_t7=p7, close_t7=c7,
                        reason=reason)
        return db

    def test_return_is_close_based_not_exit(self, tmp_path):
        """离场价 110（止盈档）、收盘 105 —— 前瞻收益应为 +5%，不是 +10%。"""
        db = self._db(tmp_path, [("A", 1.0, 100.0, 110.0, 105.0, "TP"),
                                 ("B", 2.0, 100.0, 95.0, 97.0, "SL")])
        pairs = icp._load_day_pairs(db, "t7", "signal", min_width=2)
        assert sorted(pairs["2026-06-01"]) == [(1.0, pytest.approx(5.0)),
                                               (2.0, pytest.approx(-3.0))]

    def test_split_mismatch_row_is_not_garbage(self, tmp_path):
        """生产 CRWD 04-26：price_at_predict 已按 4:1 拆股复权（112.03）、price_t7 未复权
        （476.53）⇒ 旧口径 +325%。close_t7（119.13）与入场价同单位 ⇒ +6.3%。"""
        db = self._db(tmp_path, [("CRWD", 1.0, 112.03, 476.53, 119.13, "T7_CLOSE"),
                                 ("B", 2.0, 100.0, 100.0, 100.0, "T7_CLOSE")])
        pairs = dict(icp._load_day_pairs(db, "t7", "signal", min_width=2)["2026-06-01"])
        assert pairs[1.0] == pytest.approx((119.13 / 112.03 - 1) * 100)

    def test_rows_without_price_t7_are_kept(self, tmp_path):
        """生产快照有 8 行 price_t7 为空、close_t7 已有 —— 样本成员跟终点列走。"""
        db = self._db(tmp_path, [("A", 1.0, 100.0, None, 103.0, "T7_CLOSE"),
                                 ("B", 2.0, 100.0, None, 99.0, "T7_CLOSE")])
        pairs = icp._load_day_pairs(db, "t7", "signal", min_width=2)
        assert len(pairs.get("2026-06-01", [])) == 2, (
            "price_t7 为空的行被过滤掉了 —— 过滤条件没跟终点列走")


class TestSingleSourceOfTruth:
    """两条路径的终点列都必须**调用时**查 `ic_diagnostics.FORWARD_CLOSE_COL`。

    用哨兵列证明：把表项临时指向 probe_t7，第 2 步（σ_IC²）与第 3 步（σ_cs² 的骨架）
    **都**必须跟着走——两步读不同列，第 4 步的方差分解就是在两条收益序列上做减法。
    在 SQL 里写死 'close_t7'、在模块层冻成常量、或只改一条路径，这组都会红。
    """

    def _probe_db(self, tmp_path):
        """probe_t7 的 IC 逐周翻转（方差≈1）；close_t7 与 price_t7 都恒为同向（方差≈0）。"""
        db = tmp_path / "p.db"
        with sqlite3.connect(db) as con:
            _create_table(con)
            con.execute("ALTER TABLE predictions ADD COLUMN probe_t7 REAL")
            for w, d in _week_days():
                for i, tk in enumerate(TICKERS):
                    score = float(i + 1)
                    _insert(con, d, tk, score, pap=100.0, price_t7=100.0 + score,
                            close_t7=100.0 + score, reason="T7_CLOSE")
                    sign = 1 if w % 2 == 0 else -1
                    con.execute("UPDATE predictions SET probe_t7=? WHERE date=? AND ticker=?",
                                (100.0 + sign * score, d, tk))
        return db

    def test_both_paths_follow_the_registry(self, tmp_path, monkeypatch):
        db = self._probe_db(tmp_path)
        monkeypatch.setitem(icp._icd.FORWARD_CLOSE_COL, "t7", "probe_t7")

        pairs = icp._load_day_pairs(db, "t7", "signal", min_width=5)
        mon, tue = _week_days()[2][1], _week_days()[3][1]         # 第 1 周（奇数周）
        assert dict(pairs[mon])[8.0] == pytest.approx(-8.0), (
            "_load_day_pairs 没有查 FORWARD_CLOSE_COL（第 3 步骨架）")
        assert tue in pairs

        obs = icp.observed_weekly_var(db, "t7", min_width=5)
        assert obs["by_dim"]["signal"]["var"] > 0.5, (
            "observed_weekly_var 没有查 FORWARD_CLOSE_COL（第 2 步 σ_IC²）")

    def test_registry_value_and_output_name_it(self, tmp_path, monkeypatch, capsys):
        assert icp.forward_close_col("t7") == "close_t7"
        assert icp.forward_close_col("t30") == "price_t30"   # 本来就是收盘价，不是回退
        db = _build_flipping_db(tmp_path)
        res = _run_json(monkeypatch, capsys, db)
        assert res["forward_close_col"] == "close_t7", "输出要点名终点列，不是静默选择"
        monkeypatch.setattr(sys, "argv", ["ic_power_analysis.py", "--db", str(db),
                                          "--no-network", "--draws", "5"])
        assert icp.main() == 0
        assert "前瞻收益终点列: close_t7" in capsys.readouterr().out
