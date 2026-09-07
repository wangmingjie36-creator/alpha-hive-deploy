"""训练数据构建的两条路径必须同口径（v0.45.142）。

背景：`generate_ml_report._build_real_training_data` 与
`ml_predictor.build_training_data_from_db` 都从 `predictions` 表构建
`TrainingData`，但口径长期相反：

| 项 | `_build_real_training_data`（旧） | `build_training_data_from_db` |
|---|---|---|
| 维度缺失 | `ds.get("signal", 5.0)` 补 5.0 | v0.45.50 起**剔除** |
| `ambiguous_t7=1` | 收下 | v0.45.9 P0 起排除（标签无意义） |
| `return_t7 IS NULL` | `or 0` 变成 0.0 收益 | 排除 |
| `momentum_5d` | 写死 `0.0` | 由 signal/sentiment 派生 |
| `iv_rank` / `put_call_ratio` | 只判 None，哨兵值原样收 | 哨兵值改由 odds 派生 |

真库实测（2026-09-07，`pheromone.db`，旧路径 `checked_t7=1 LIMIT 200` 口径）：
200 行里 2 行 `dimension_scores` 为空字典（五维全补 5.0）、26 行 `ambiguous_t7=1`；
旧路径产出的 11 个特征里 **3 个是常数**（momentum/iv_rank/put_call_ratio），
树模型在常数列上无法分裂。

本文件把「两条路径同口径」写成断言。写在 v0.45.142 之前跑必须红。
"""

import json
import sqlite3

import pytest

import backtester
import ml_predictor
from generate_ml_report import MLEnhancedReportGenerator


# ── 夹具 ───────────────────────────────────────────────────────────────

_GOOD_DIMS = {"signal": 6.0, "catalyst": 7.0, "sentiment": 4.0,
              "odds": 5.5, "risk_adj": 6.5}


def _make_db(tmp_path, rows):
    """建一张含 `ambiguous_t7` 的 predictions 表并灌入 rows。

    `ambiguous_t7` 必须在——生产代码 `PRAGMA table_info` 探测到它才会拼上
    排除子句，夹具漏了这一列会让「排除模糊样本」的断言恒绿。
    """
    db = str(tmp_path / "pred.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, ticker TEXT NOT NULL,
            final_score REAL NOT NULL, direction TEXT NOT NULL,
            price_at_predict REAL, dimension_scores TEXT, agent_directions TEXT,
            options_score REAL, iv_rank REAL, put_call_ratio REAL,
            gamma_exposure REAL, flow_direction TEXT,
            price_t1 REAL, return_t1 REAL, correct_t1 INTEGER, checked_t1 INTEGER DEFAULT 0,
            price_t7 REAL, return_t7 REAL, correct_t7 INTEGER, checked_t7 INTEGER DEFAULT 0,
            ambiguous_t7 INTEGER DEFAULT 0,
            price_t30 REAL, return_t30 REAL, correct_t30 INTEGER, checked_t30 INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, ticker)
        )""")
    conn.executemany(
        "INSERT INTO predictions (date,ticker,final_score,direction,dimension_scores,"
        "agent_directions,iv_rank,put_call_ratio,return_t7,correct_t7,checked_t7,ambiguous_t7)"
        " VALUES (:date,:ticker,:final_score,:direction,:dims,:ad,:iv,:pc,:ret,:ok,1,:amb)",
        rows)
    conn.commit()
    conn.close()
    return db


def _row(i, *, dims=None, ret=0.05, amb=0, iv=None, pc=None, ticker=None):
    d = _GOOD_DIMS if dims is None else dims
    return {
        "date": f"2026-08-{(i % 28) + 1:02d}",
        "ticker": ticker or f"T{i:03d}",
        "final_score": 5.0 + (i % 5) * 0.4,
        "direction": "bullish" if i % 2 else "bearish",
        "dims": json.dumps(d) if d is not None else None,
        "ad": json.dumps({"ScoutBeeNova": "bullish", "OracleBeeEcho": "bearish"}),
        "iv": iv, "pc": pc, "ret": ret,
        "ok": 1 if ret and ret > 0 else 0, "amb": amb,
    }


def _healthy(n=40, **kw):
    """n 条五维齐全、非模糊、return_t7 非空的行。

    signal / sentiment / odds **逐条不同**，而 DB 的 `iv_rank`、
    `put_call_ratio` 两列留 NULL——这就是真库的情形（旧路径因此恒得
    50.0 / 1.0 两个常数，新路径改由 odds 派生，两列才有方差）。
    """
    out = []
    for i in range(n):
        d = dict(_GOOD_DIMS)
        d["signal"] = 2.0 + (i % 9)          # 让 momentum 有方差
        d["sentiment"] = 1.0 + (i % 8)
        d["odds"] = 1.5 + (i % 7)            # 让 iv_rank / put_call_ratio 有方差
        out.append(_row(i, dims=d, ret=0.05 if i % 2 else -0.03, **kw))
    return out


@pytest.fixture
def gen():
    """不跑 __init__（它会连库训练），只要方法本身。"""
    return object.__new__(MLEnhancedReportGenerator)


@pytest.fixture
def point_store_at(monkeypatch):
    """让 `_build_real_training_data` 里的 `PredictionStore()` 指向夹具库。

    打的是**源模块** `backtester`——函数内是 `from backtester import
    PredictionStore` 的局部 import，patch 源模块能打中（v0.45.135）。
    """
    def _point(db):
        class _PS:
            def __init__(self, *a, **k):
                self.db_path = db
        monkeypatch.setattr(backtester, "PredictionStore", _PS)
    return _point


# ── 口径：坏行必须被拒（成对：好行必须照常收）─────────────────────────

class TestCohortParity:
    """两条路径看见的行集必须一致。每条「拒绝」断言都配一条「照收」断言——
    只写前半边的话，`return []` 式的偷懒修法会全绿。"""

    def test_rejects_empty_dimension_scores(self, gen, point_store_at, tmp_path):
        rows = _healthy(40) + [_row(99, dims={}, ticker="EMPTY")]
        point_store_at(_make_db(tmp_path, rows))
        got = {d.ticker for d in gen._build_real_training_data()}
        assert "EMPTY" not in got, (
            "`dimension_scores` 为空字典的行被补成五维 5.0 的自洽假样本——"
            "正是 v0.45.50 要治的那种")

    def test_rejects_partial_dimension_scores(self, gen, point_store_at, tmp_path):
        partial = {"signal": 6.0, "catalyst": 7.0}      # 缺 sentiment/odds/risk_adj
        rows = _healthy(40) + [_row(98, dims=partial, ticker="PARTIAL")]
        point_store_at(_make_db(tmp_path, rows))
        got = {d.ticker for d in gen._build_real_training_data()}
        assert "PARTIAL" not in got, "部分缺维的行也必须剔除，不是只挡全缺的"

    def test_rejects_ambiguous_samples(self, gen, point_store_at, tmp_path):
        rows = _healthy(40) + [_row(97, amb=1, ticker="AMBIG")]
        point_store_at(_make_db(tmp_path, rows))
        got = {d.ticker for d in gen._build_real_training_data()}
        assert "AMBIG" not in got, (
            "`ambiguous_t7=1` 的 correct_t7 标签无意义（v0.45.9 P0），"
            "收进训练集就是灌噪音")

    def test_rejects_null_return_t7(self, gen, point_store_at, tmp_path):
        rows = _healthy(40) + [_row(96, ret=None, ticker="NULLRET")]
        point_store_at(_make_db(tmp_path, rows))
        got = {d.ticker for d in gen._build_real_training_data()}
        assert "NULLRET" not in got, (
            "`return_t7 IS NULL` 被 `or 0` 变成 0.0 收益——"
            "「没测到」与「恰好零收益」在训练集里同形")

    def test_rejects_bool_dimension_score(self, gen, point_store_at, tmp_path):
        """`bool` 是 `int` 子类：`isinstance(True, (int, float))` 为真，
        旧守卫放行后 `True` 会被当成 1.0（v0.45.121 同物种）。"""
        d = dict(_GOOD_DIMS); d["signal"] = True
        rows = _healthy(40) + [_row(95, dims=d, ticker="BOOLDIM")]
        point_store_at(_make_db(tmp_path, rows))
        got = {x.ticker for x in gen._build_real_training_data()}
        assert "BOOLDIM" not in got, "bool 维度分穿过了类型闸，会被当成 1.0 分"

    def test_rejects_nan_dimension_score(self, gen, point_store_at, tmp_path):
        """`json.loads` 接受裸 `NaN` 字面量，解出来的 NaN 是 float 且 truthy，
        `isinstance` / `or` / `if x:` 都拦不住（v0.45.93/97/110 同物种）。"""
        d = dict(_GOOD_DIMS); d["risk_adj"] = float("nan")
        rows = _healthy(40) + [_row(94, dims=d, ticker="NANDIM")]
        db = _make_db(tmp_path, rows)
        import sqlite3
        raw = sqlite3.connect(db).execute(
            "SELECT dimension_scores FROM predictions WHERE ticker='NANDIM'").fetchone()[0]
        assert "NaN" in raw, "夹具没真的写进 NaN，断言会恒绿"
        point_store_at(db)
        got = {x.ticker for x in gen._build_real_training_data()}
        assert "NANDIM" not in got, "NaN 维度分穿过了类型闸，会一路污染派生特征"

    def test_keeps_healthy_rows(self, gen, point_store_at, tmp_path):
        """成对的反面：合法行必须照常收，且数量对得上。"""
        rows = _healthy(40)
        point_store_at(_make_db(tmp_path, rows))
        got = gen._build_real_training_data()
        assert len(got) == 40, f"合法行被误杀：40 → {len(got)}"


# ── 特征：三个常数列必须活过来 ────────────────────────────────────────

class TestBadRowsDoNotConsumeLimit:
    """坏行必须在 **SQL 里**排掉，不能只靠循环里的异常兜住。

    两者在「产出不含坏行」这一点上同形，所以很容易以为等价 —— 但坏行若进了
    结果集就会**占掉 `LIMIT` 的名额**（也会混进 `len(rows) < min_samples`
    的计数），于是 `max_rows=N` 实际拿到的可用样本 < N。本仓 mutation check
    正是在这里发现：拆掉 `return_t7 IS NOT NULL` 之后 13 条断言无一变红，
    因为 `float(None)` 的 TypeError 把上游缺陷掩护住了。
    """

    def test_null_return_row_does_not_eat_a_limit_slot(self, tmp_path):
        rows = [_row(1, ticker="G1"), _row(2, ticker="G2"), _row(3, ticker="G3")]
        for i, r in enumerate(rows):
            r["date"] = f"2026-08-0{i + 1}"
        bad = _row(4, ret=None, ticker="NULLRET")
        bad["date"] = "2026-08-09"          # 最新的一条，排序上排在最前
        db = _make_db(tmp_path, rows + [bad])

        got = ml_predictor.build_training_data_from_db(db, min_samples=1, max_rows=2)
        assert len(got) == 2, (
            f"max_rows=2 只拿到 {len(got)} 条可用样本 —— `return_t7 IS NULL` 的行"
            "进了结果集、占了一个名额，才在循环里被异常丢掉")
        assert "NULLRET" not in {d.ticker for d in got}


class TestNoConstantFeatures:
    """真库实测旧路径 11 个特征里 3 个 sd=0。常数列在树模型里不可分裂。"""

    @pytest.mark.parametrize("feat", ["momentum_5d", "iv_rank", "put_call_ratio"])
    def test_feature_has_variance(self, gen, point_store_at, tmp_path, feat):
        point_store_at(_make_db(tmp_path, _healthy(40)))
        vals = {getattr(d, feat) for d in gen._build_real_training_data()}
        assert len(vals) > 1, (
            f"`{feat}` 在 40 条 signal/sentiment 各不相同的样本上只取到 "
            f"{vals} 一个值——这一列没有信息")


# ── 不变式：同库同口径 ⇒ 两条路径逐条相同 ─────────────────────────────

class TestPathsProduceIdenticalRows:

    def test_identical_when_both_above_threshold(self, gen, point_store_at, tmp_path):
        db = _make_db(tmp_path, _healthy(40) + [
            _row(99, dims={}, ticker="EMPTY"),
            _row(97, amb=1, ticker="AMBIG"),
            _row(96, ret=None, ticker="NULLRET"),
        ])
        point_store_at(db)
        a = gen._build_real_training_data()
        b = ml_predictor.build_training_data_from_db(db, min_samples=30, max_rows=500)
        assert [tuple(vars(x).items()) for x in a] == [tuple(vars(x).items()) for x in b], (
            "两条路径在同一库、同一 max_rows 下必须逐字段相同——"
            "这正是「口径并存陷阱」要消掉的东西")

    def test_only_difference_is_the_threshold(self, gen, point_store_at, tmp_path):
        """合并后两条路径唯一的区别就是样本量下限：
        `_build_real_training_data` 用 10（回退预加载），另一条用 30。"""
        db = _make_db(tmp_path, _healthy(15))
        point_store_at(db)
        assert len(gen._build_real_training_data()) == 15, "15 条应过 10 的闸"
        assert ml_predictor.build_training_data_from_db(db, min_samples=30) == [], \
            "15 条不应过 30 的闸"


# ── 接线：委托必须真的发生，且真的传了库路径 ─────────────────────────

class TestDelegationWiring:
    """测被调函数 ≠ 测接线（v0.45.126）。这里钉的是实参。"""

    def test_delegates_with_explicit_db_path(self, gen, point_store_at, tmp_path,
                                             monkeypatch):
        db = _make_db(tmp_path, _healthy(40))
        point_store_at(db)
        calls = []
        real = ml_predictor.build_training_data_from_db

        def spy(db_path=None, min_samples=30, max_rows=500):
            calls.append({"db_path": db_path, "min_samples": min_samples,
                          "max_rows": max_rows})
            return real(db_path, min_samples, max_rows)

        monkeypatch.setattr(ml_predictor, "build_training_data_from_db", spy)
        gen._build_real_training_data()

        assert len(calls) == 1, (
            f"期望委托 build_training_data_from_db 恰一次，实际 {len(calls)} 次"
            "（0 次 = 还在自己拼 SQL）")
        assert calls[0]["db_path"] == db, (
            "db_path 必须显式传——不传会走 `PATHS.db` 默认值，"
            "在 worktree 里是空桩库（v0.45.140 就栽在这）")
        assert calls[0]["min_samples"] == 10, (
            "回退预加载的门槛是 MIN_REAL_SAMPLES=10，不是默认的 30")
