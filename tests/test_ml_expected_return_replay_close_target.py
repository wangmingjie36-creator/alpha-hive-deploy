"""
experiments/ml_expected_return_replay.py 的「真实 7 日收益」终点列（v0.45.326）

背景：脚本 docstring 写「不用 return_t7（路径依赖、被 SL/TP 截断），由 price_t7 直接算」
——理由对，列选错了。`price_t7` 存的是 `_simulate_trade_path` 的 `exit_price`（离场价），
与 return_t7 一样截断；另有 CRWD 两行拆股单位错配读成 +325% / +340%。与 ic_diagnostics
（v0.45.19）、signal_archive.load_panel（v0.45.321）同一个误解。本脚本是就绪度闸
`next_step` 的前半句。2026-09-23 快照上前后对照（详见 CHANGELOG v0.45.326）：
动量 IC 的判定由「⚠️ 负相关 / 短期反转」变「➖ 接近 0」，方向准确率由「差于恒定看多」变「持平」。

核心夹具**故意让两列给出相反的结论**：动量为正的票最终收涨、但途中先跌穿止损
（离场价 −5%）；动量为负的票最终收跌、但途中先冲过止盈（离场价 +10%）。
读 close_t7 ⇒ 动量 IC ≈ +1、sign(动量) 方向全对；读 price_t7 ⇒ IC ≈ −1、方向全错。
符号相反，回退即红——不是数值漂移。
"""

import datetime as dt
import importlib.util
import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "ml_expected_return_replay", _ROOT / "experiments" / "ml_expected_return_replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rep = _load_module()

# v0.45.329：脚本的判定改为横截面周序列 t 检验（每 ISO 周取第一个可用交易日），
# 原 6 天只跨 2 个 ISO 周 ⇒ 周数 < MIN_WEEKS、永远「无法判定」。改为 6 周 × 每周 2 天（周一、周二）。
DAYS = [str(dt.date(2026, 6, 1) + dt.timedelta(days=7 * k + o)) for k in range(6) for o in (0, 1)]
TICKERS = [f"T{i}" for i in range(8)]          # 12 × 8 = 96 ≥ 脚本的 30 条下限


def _create_tables(con):
    con.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        price_at_predict REAL, price_t7 REAL, return_t7 REAL, close_t7 REAL,
        exit_price REAL, exit_reason TEXT, checked_t7 INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE signal_archive (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ticker TEXT NOT NULL,
        signal TEXT NOT NULL, value REAL, UNIQUE(date, ticker, signal))""")


def _insert(con, date, ticker, mom, *, pap, price_t7, close_t7, reason, crowding=23.3):
    ret_path = None if price_t7 is None else (price_t7 / pap - 1) * 100
    con.execute("INSERT INTO predictions (date,ticker,price_at_predict,price_t7,return_t7,"
                "close_t7,exit_price,exit_reason,checked_t7) VALUES (?,?,?,?,?,?,?,?,1)",
                (date, ticker, pap, price_t7, ret_path, close_t7, price_t7, reason))
    con.executemany("INSERT INTO signal_archive (date,ticker,signal,value) VALUES (?,?,?,?)",
                    [(date, ticker, "price.momentum_5d", mom),
                     (date, ticker, "crowding.score", crowding)])


def _build_reversal_db(tmp_path):
    """close_t7 与动量同号，price_t7（= 离场价）与动量反号。动量从不为 0（不弃权）。"""
    rng = random.Random(326)
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    _create_tables(con)
    for d in DAYS:
        for tk in TICKERS:
            mom = rng.choice((-1, 1)) * rng.uniform(1.0, 5.0)
            ret_close = 1.5 * mom + rng.gauss(0, 0.3)          # 收盘：跟着动量走
            if ret_close > 0:
                reason, exit_px = "SL", 95.0                    # 先跌穿止损，再收涨
            else:
                reason, exit_px = "TP", 110.0                   # 先冲过止盈，再收跌
            _insert(con, d, tk, mom, pap=100.0, price_t7=exit_px,
                    close_t7=100.0 * (1 + ret_close / 100.0), reason=reason,
                    crowding=rng.uniform(10.0, 40.0))
    con.commit()
    con.close()
    return db


def _run_main(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["ml_expected_return_replay.py", *args])
    assert rep.main() == 0, capsys.readouterr().err
    return capsys.readouterr().out


class TestFixtureDiscriminates:
    """没有这一组，下面的「IC 为正 / 方向全对」可能只是夹具本来就分不出两列。"""

    @pytest.mark.parametrize("col, positive", [("close_t7", True), ("price_t7", False)])
    def test_columns_give_opposite_momentum_ic(self, tmp_path, col, positive):
        with sqlite3.connect(_build_reversal_db(tmp_path)) as c:
            rows = c.execute(
                f"SELECT m.value, (p.{col} / p.price_at_predict - 1) * 100 FROM predictions p "
                f"JOIN signal_archive m ON m.date=p.date AND m.ticker=p.ticker "
                f"AND m.signal='price.momentum_5d'").fetchall()
        assert len(rows) == len(DAYS) * len(TICKERS)
        ic = rep._spearman([m for m, _ in rows], [r for _, r in rows])
        assert (ic > 0.5) if positive else (ic < -0.5), f"{col}: ic={ic:+.3f}"


class TestHeadlineFollowsClose:
    """脚本的**结论**（动量 IC 判定、方向准确率）必须建立在 close_t7 上。

    ⚠️ 本组只断言统计量本身、不碰 `forward_close_col`——v0.45.326 拿改动前的脚本原样来跑，
    红的理由就是「结论对着离场价算」，而不是「新属性不存在」。v0.45.329 起动量 IC 的键
    改为 `momentum_ic`（横截面周序列），旧键 `ic_momentum_vs_forward`（池化）已删；
    横截面口径本身的守卫在 `test_ml_expected_return_replay_cross_sectional.py`。
    """

    def test_json_momentum_ic_and_accuracy_follow_close_t7(self, tmp_path, monkeypatch, capsys):
        db = _build_reversal_db(tmp_path)
        res = json.loads(_run_main(monkeypatch, capsys, "--db", str(db), "--json"))
        assert res["n_pairs"] == len(DAYS) * len(TICKERS)
        wk = res["momentum_ic"]["weekly"]
        assert wk is not None and wk["n_weeks"] == 6, res["momentum_ic"]
        assert wk["mean"] > 0.5 and res["momentum_ic"]["verdict"] == "positive", (
            f"动量周序列 IC={wk['mean']:+.3f}（{res['momentum_ic']['verdict']}）：真实收益没用 close_t7"
            f"（price_t7 是 SL/TP 离场价，见 ic_diagnostics.FORWARD_CLOSE_COL）")
        acc = res["new_accuracy"]
        assert acc["abstain"] == 0 and acc["accuracy"] > 0.9, (
            f"v0.44.2 方向准确率 {acc['accuracy']:.1%}：对着离场价算时这里接近 0%")

    def test_text_verdict_is_not_a_truncation_made_reversal(self, tmp_path, monkeypatch, capsys):
        """文本结论层：close 口径是「✅ 显著正相关」；回退到离场价会印出「⚠️ 显著负相关 / 短期反转」。"""
        out = _run_main(monkeypatch, capsys, "--db", str(_build_reversal_db(tmp_path)))
        assert "✅ 显著正相关 —— sign(动量)" in out
        assert "短期反转" not in out, "动量 IC 判定是对着 SL/TP 离场价算出来的"


class TestLoadPairsValues:
    def test_return_is_close_based_not_exit_or_clamped(self, tmp_path):
        """离场价 110（止盈档）、收盘 105、return_t7 是钳位值 —— 三者各不相同。"""
        db = tmp_path / "p.db"
        with sqlite3.connect(db) as con:
            _create_tables(con)
            _insert(con, "2026-06-01", "A", 2.0, pap=100.0, price_t7=110.0, close_t7=105.0,
                    reason="TP")
        (_d, _t, _m, _c, ret), = rep.load_pairs(db)
        assert ret == pytest.approx(5.0), "应为 (105/100−1)×100，不是离场价 110 或 return_t7"

    def test_split_mismatch_row_is_not_garbage(self, tmp_path):
        """生产 CRWD 04-26：price_at_predict 已按 4:1 拆股复权（112.03）、price_t7 未复权
        （476.53）⇒ 旧口径 +325%。close_t7（119.13）与入场价同单位 ⇒ +6.3%。"""
        db = tmp_path / "p.db"
        with sqlite3.connect(db) as con:
            _create_tables(con)
            _insert(con, "2026-04-26", "CRWD", 1.0, pap=112.03, price_t7=476.53,
                    close_t7=119.13, reason="T7_CLOSE")
        (_d, _t, _m, _c, ret), = rep.load_pairs(db)
        assert ret == pytest.approx((119.13 / 112.03 - 1) * 100)

    def test_rows_without_price_t7_are_kept(self, tmp_path):
        """生产快照有 8 行配对样本 price_t7 为空、close_t7 已有 —— 样本成员跟终点列走。"""
        db = tmp_path / "p.db"
        with sqlite3.connect(db) as con:
            _create_tables(con)
            _insert(con, "2026-06-01", "A", 2.0, pap=100.0, price_t7=None, close_t7=103.0,
                    reason="T7_CLOSE")
        pairs = rep.load_pairs(db)
        assert len(pairs) == 1, "price_t7 为空的行被过滤掉了 —— 过滤条件没跟终点列走"
        assert pairs[0][4] == pytest.approx(3.0)


class TestSingleSourceOfTruth:
    """终点列必须**调用时**查 `ic_diagnostics.FORWARD_CLOSE_COL`，不在脚本里写死。

    用一个哨兵列证明「脚本在读那张表」：把表项临时指向哨兵列，load_pairs 必须跟着走。
    在 SQL 里写死 'close_t7'、或在模块层冻成常量，这条都会红（上面几组照样绿）。
    """

    def test_load_pairs_follows_the_registry(self, tmp_path, monkeypatch):
        db = tmp_path / "p.db"
        with sqlite3.connect(db) as con:
            _create_tables(con)
            con.execute("ALTER TABLE predictions ADD COLUMN probe_t7 REAL")
            _insert(con, "2026-06-01", "A", 2.0, pap=100.0, price_t7=110.0, close_t7=105.0,
                    reason="TP")
            con.execute("UPDATE predictions SET probe_t7 = 142.0")
        monkeypatch.setitem(rep._icd.FORWARD_CLOSE_COL, "t7", "probe_t7")
        (_d, _t, _m, _c, ret), = rep.load_pairs(db)
        assert ret == pytest.approx(42.0), "load_pairs 没有查 FORWARD_CLOSE_COL"

    def test_registry_value_and_output_name_it(self, tmp_path, monkeypatch, capsys):
        assert rep.forward_close_col() == "close_t7"
        res = json.loads(_run_main(monkeypatch, capsys, "--db",
                                   str(_build_reversal_db(tmp_path)), "--json"))
        assert res["forward_close_col"] == "close_t7", "输出要点名终点列，不是静默选择"
