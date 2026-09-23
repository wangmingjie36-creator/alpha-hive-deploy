"""
ic_diagnostics `--benchmark` 的前瞻收益终点列（v0.45.328）

背景：`build_benchmark_panel()` 写死 `price_col = f"price_{horizon}"`，从不读传进来的
`target_col`、也不收 `target` ⇒ t7 的基准套件（综合分 / 5 维 / 经典因子 / 噪音地板）一直对着
SL/TP 离场价算，`--target` 对它无效。v0.45.19 把同文件的 `load_daily_ic` 改成 close_t7
时没改这里，于是**同一次默认运行里维度表与基准表对同一维度印出两个不同的 IC**
（2026-09-23 快照：sentiment 维度表 +0.104 / 基准表 +0.074，risk_adj −0.113 / −0.134）。
与 v0.45.321（signal_archive）、v0.45.326（ml_expected_return_replay）同一误解的又一个读者。

核心夹具沿用 `test_signal_archive_close_target.py` 的形状，**故意让两列给出相反的 IC 符号**：
分数高的票最终收涨，但途中先跌穿止损（离场价 −5%）；分数低的票最终收跌，但途中先冲过止盈
（离场价 +10%）。读 price_t7 ⇒ IC 为负；读 close_t7 ⇒ IC 为正。符号不同，回退即红。
"""

import datetime
import json
import os
import random
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ic_diagnostics as icd
import signal_archive as sa

TICKERS = [f"T{i}" for i in range(8)]
SYS = "🐝 综合分 final_score"


def _bdays(start: str, n: int):
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


DAYS = _bdays("2026-06-01", 24)   # ≥10 天才进 --benchmark 的因子表


def _create_predictions(con):
    con.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, ticker TEXT,
        final_score REAL, dimension_scores TEXT,
        price_at_predict REAL, price_t7 REAL, return_t7 REAL, close_t7 REAL,
        exit_price REAL, exit_reason TEXT, checked_t7 INTEGER DEFAULT 0)""")


def _build_reversal_db(tmp_path, close_equals_exit: bool = False):
    """合成库：close_t7 与分数正相关，price_t7（= exit_price）与分数负相关。

    final_score 与 5 个维度都取同一个 x —— 每个系统因子都各自被检验一次。
    close_equals_exit=True：模拟「有人把离场价写进了 close_t7」的数据层回归 ——
    列名没变、代码没变，只有截断指纹会跳。
    """
    rng = random.Random(328)
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    _create_predictions(con)
    rows = []
    for d in DAYS:
        for tk in TICKERS:
            x = rng.gauss(0, 1)
            ret_close = 2.0 * x + rng.gauss(0, 0.3)            # 收盘：跟着分数走
            close = 100.0 * (1 + ret_close / 100.0)
            if ret_close > 0:
                reason, exit_px = "SL", 95.0                   # 先跌穿止损，再收涨
            else:
                reason, exit_px = "TP", 110.0                  # 先冲过止盈，再收跌
            if close_equals_exit:
                close = exit_px
            dims = {k: x for k in icd.DIMS}
            rows.append((d, tk, x, json.dumps(dims), 100.0, exit_px, exit_px - 100.0,
                         close, exit_px, reason))
    con.executemany(
        "INSERT INTO predictions (date,ticker,final_score,dimension_scores,price_at_predict,"
        "price_t7,return_t7,close_t7,exit_price,exit_reason,checked_t7) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1)", rows)
    con.commit()
    con.close()
    return db


def _one_row_db(tmp_path, **cols):
    """单行库：列按需给，缺省列为 NULL。"""
    db = tmp_path / "one.db"
    con = sqlite3.connect(db)
    _create_predictions(con)
    con.execute("ALTER TABLE predictions ADD COLUMN return_alt REAL")
    base = {"date": "2026-06-01", "ticker": "A", "final_score": 1.0,
            "dimension_scores": json.dumps({k: 1.0 for k in icd.DIMS}),
            "price_at_predict": 100.0, "checked_t7": 1}
    base.update(cols)
    ks = ",".join(base)
    con.execute(f"INSERT INTO predictions ({ks}) VALUES ({','.join('?' * len(base))})",
                list(base.values()))
    con.commit()
    con.close()
    return db


@pytest.fixture
def offline(monkeypatch):
    """价格类基准要联网；本文件只测系统自身那几行的终点列。"""
    monkeypatch.setattr(icd, "_load_prices", lambda *a, **k: None)


def _mean_ic(by_day) -> float:
    s = icd._ic_series_from_pairs(by_day)
    assert len(s) == len(DAYS), f"只算出 {len(s)} 天 IC —— 夹具没接上"
    return sum(s.values()) / len(s)


def _panel(db, **kw):
    return icd.build_benchmark_panel(db, "return_t7", "checked_t7", "t7", **kw)


class TestFixtureDiscriminates:
    """没有这一组，下面的「IC 为正」可能只是夹具本来就分不出两列。"""

    @pytest.mark.parametrize("col, sign", [("close_t7", +1), ("price_t7", -1)])
    def test_two_columns_give_opposite_ic_signs(self, tmp_path, col, sign):
        db = _build_reversal_db(tmp_path)
        by_day = {}
        with sqlite3.connect(db) as c:
            for d, s, r in c.execute(
                    f"SELECT date, final_score, ({col} - price_at_predict) / price_at_predict "
                    f"FROM predictions"):
                by_day.setdefault(d, []).append((s, r))
        assert sign * _mean_ic(by_day) > 0.5


class TestBenchmarkUsesClose:
    def test_system_and_every_dimension_follow_close_t7(self, tmp_path, offline):
        """回退到 price_t7 时这里全是 −0.8 左右 —— 符号相反，不是数值漂移。"""
        panel = _panel(_build_reversal_db(tmp_path))
        assert sum(len(v) for v in panel[SYS].values()) == len(DAYS) * len(TICKERS)
        for name in [SYS] + [f"   └ {d}" for d in icd.DIMS]:
            ic = _mean_ic(panel[name])
            assert ic > 0.5, (f"{name.strip()} IC={ic:+.3f}：基准前瞻收益没用 close_t7"
                              f"（price_t7 是 SL/TP 离场价，见 ic_diagnostics.FORWARD_CLOSE_COL）")

    def test_price_target_still_reproduces_history(self, tmp_path, offline):
        """`--target price` 保留给复现旧报告 —— 它必须真的还是旧口径，否则复现不了。"""
        assert _mean_ic(_panel(_build_reversal_db(tmp_path), target="price")[SYS]) < -0.5

    @pytest.mark.parametrize("target, want", [
        ("close", 5.0),       # (105 − 100) / 100
        ("price", 10.0),      # 离场价 110
        ("path", 9.945),      # return_t7 钳位值
    ])
    def test_single_row_value_per_target(self, tmp_path, offline, target, want):
        db = _one_row_db(tmp_path, price_t7=110.0, return_t7=9.945, close_t7=105.0,
                         exit_price=110.0, exit_reason="TP")
        (score, ret), = _panel(db, min_width=1, target=target)[SYS]["2026-06-01"]
        assert ret == pytest.approx(want)

    def test_path_reads_the_target_col_it_is_given(self, tmp_path, offline):
        """旧实现从不读 `target_col` 参数 —— 传什么都一样。"""
        db = _one_row_db(tmp_path, return_t7=9.945, return_alt=-3.0, close_t7=105.0)
        panel = icd.build_benchmark_panel(db, "return_alt", "checked_t7", "t7",
                                          min_width=1, target="path")
        assert panel[SYS]["2026-06-01"][0][1] == pytest.approx(-3.0)

    def test_rows_without_price_t7_are_kept(self, tmp_path, offline):
        """生产快照有 8 行 checked_t7=1、price_t7 为空、close_t7 已有 —— 样本成员跟终点列走。"""
        db = _one_row_db(tmp_path, close_t7=103.0)
        assert _panel(db, min_width=1)[SYS]["2026-06-01"][0][1] == pytest.approx(3.0)

    def test_end_column_is_looked_up_at_call_time(self, tmp_path, offline, monkeypatch):
        """写死 "close_t7" 也能过上面几条 —— 这条要求真的去查 FORWARD_CLOSE_COL。"""
        db = _one_row_db(tmp_path, price_t7=110.0, close_t7=105.0, return_alt=150.0)
        monkeypatch.setitem(icd.FORWARD_CLOSE_COL, "t7", "return_alt")   # 哨兵列
        assert _panel(db, min_width=1)[SYS]["2026-06-01"][0][1] == pytest.approx(50.0)

    def test_t30_uses_price_t30_without_close_t30_column(self, tmp_path, offline):
        """t30 没有 close_t30 列：price_t30 本身就是收盘价（SL/TP 路径模拟只作用于 t7）。"""
        db = tmp_path / "p30.db"
        with sqlite3.connect(db) as c:
            c.execute("""CREATE TABLE predictions (
                date TEXT, ticker TEXT, final_score REAL, dimension_scores TEXT,
                price_at_predict REAL, price_t30 REAL, return_t30 REAL, checked_t30 INTEGER)""")
            c.execute("INSERT INTO predictions VALUES ('2026-06-01','A',1.0,NULL,100.0,120.0,"
                      "7.0,1)")
        panel = icd.build_benchmark_panel(db, "return_t30", "checked_t30", "t30", min_width=1)
        assert panel[SYS]["2026-06-01"][0][1] == pytest.approx(20.0)

    def test_unregistered_horizon_raises_instead_of_guessing(self, tmp_path):
        with pytest.raises(ValueError, match="FORWARD_CLOSE_COL"):
            icd.build_benchmark_panel(tmp_path / "nope.db", "return_t1", "checked_t1", "t1")


class TestSameTargetAsDimensionTable:
    """维度表（load_daily_ic）与基准表必须是同一口径 —— 这正是 v0.45.19 之后断开的那条线。

    逐日逐维度比 IC，不只比符号：换任何一侧的取数都会在这里红。
    """

    @pytest.mark.parametrize("target", ["close", "price", "path"])
    def test_benchmark_dimension_rows_equal_dimension_table(self, tmp_path, offline, target):
        db = _build_reversal_db(tmp_path)
        ic, _, widths = icd.load_daily_ic(db, "return_t7", "checked_t7",
                                          target=target, horizon="t7")
        assert len(widths) == len(DAYS)
        panel = _panel(db, target=target)
        for dim in icd.DIMS:
            bench = icd._ic_series_from_pairs(panel[f"   └ {dim}"])
            assert bench.keys() == ic[dim].keys()
            for d in bench:
                assert bench[d] == pytest.approx(ic[dim][d]), (target, dim, d)

    def test_dimension_table_itself_reads_close(self, tmp_path):
        """`fake_db`（test_ic_diagnostics.py）刻意让 close_t7 == price_t7，钉不住这一条。"""
        ic, _, _ = icd.load_daily_ic(_build_reversal_db(tmp_path), "return_t7", "checked_t7")
        assert sum(ic["signal"].values()) / len(ic["signal"]) > 0.5


class TestCLIWiring:
    """main() 必须把 --target 传给基准表，并在输出里点名终点列。"""

    def _run(self, monkeypatch, capsys, db, *extra):
        monkeypatch.setattr(sys, "argv", ["ic_diagnostics.py", "--db", str(db), "--horizon",
                                          "t7", "--benchmark", "--draws", "10", *extra])
        assert icd.main() == 0
        return capsys.readouterr().out

    @pytest.mark.parametrize("target, sign, col", [("close", +1, "close_t7"),
                                                   ("price", -1, "price_t7")])
    def test_json_benchmark_follows_target(self, tmp_path, offline, monkeypatch, capsys,
                                           target, sign, col):
        out = json.loads(self._run(monkeypatch, capsys, _build_reversal_db(tmp_path),
                                   "--json", "--target", target))["t7"]
        assert sign * out["benchmark"]["factors"][SYS]["daily_ic"] > 0.5
        assert out["benchmark"]["end_col"] == out["meta"]["end_col"] == col
        assert out["benchmark"]["target_mode"] == target

    def test_text_benchmark_names_its_end_column(self, tmp_path, offline, monkeypatch, capsys):
        out = self._run(monkeypatch, capsys, _build_reversal_db(tmp_path))
        bench = out[out.index("【基准对照】"):]
        assert "close_t7" in bench.split("\n", 3)[2]


class TestTruncationFingerprint:
    """数据层观测点：列名对了，但列里装的是离场价 —— 只有截断指纹会跳。与 signal_archive 同一探测器。"""

    def test_benchmark_warns_when_close_column_holds_exit_prices(self, tmp_path, offline,
                                                                 capsys):
        _panel(_build_reversal_db(tmp_path, close_equals_exit=True))
        err = capsys.readouterr().err
        assert "close_t7" in err and "exit_price" in err

    def test_dimension_table_warns_too(self, tmp_path, capsys):
        icd.load_daily_ic(_build_reversal_db(tmp_path, close_equals_exit=True),
                          "return_t7", "checked_t7")
        assert "exit_price" in capsys.readouterr().err

    def test_silent_on_clean_close_column(self, tmp_path, offline, capsys):
        """负对照：真收盘价不能误报，否则告警会被当噪音无视。"""
        db = _build_reversal_db(tmp_path)
        _panel(db)
        icd.load_daily_ic(db, "return_t7", "checked_t7")
        assert "exit_price" not in capsys.readouterr().err

    def test_silent_on_price_target(self, tmp_path, offline, capsys):
        """price 口径本来就标明是离场价 —— 在那里告警是恒真的噪音。"""
        _panel(_build_reversal_db(tmp_path), target="price")
        assert "exit_price" not in capsys.readouterr().err

    def test_signal_archive_uses_the_same_detector(self, tmp_path):
        """signal_archive._truncation_share 保留名字（dim_ic_forward_test 在调），但只剩委托。"""
        db = _build_reversal_db(tmp_path)
        with sqlite3.connect(db) as c:
            assert sa._truncation_share(c, "price_t7", "checked_t7") == \
                icd.truncation_share(c, "price_t7", "checked_t7") == (len(DAYS) * len(TICKERS), 1.0)
            assert sa._truncation_share(c, "close_t7", "checked_t7")[1] == 0.0
