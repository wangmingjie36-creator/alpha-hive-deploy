"""agent_memory 的 T+1 / T+7 / T+30 回填必须逐轮累积，后一轮不许把前一轮写成 NULL（v0.45.527）

缺陷（2026-10-16 v0.45.521 期间发现）：`OutcomesFetcher._update_memory_store(snapshot, prices)` 的
`prices` 只含**本轮**回填到的 offset，缺的 offset 算成 None，再交给
`MemoryStore.update_memory_outcome`，而后者的 SQL 无条件 `SET outcome_return_t1 = ?, … t7 = ?, … t30 = ?`
⇒ 每轮把之前存好的 horizon 覆写成 NULL（「最后一轮赢」）。`actual_outcome` 由 t7 推出，
本轮没有 t7 就变成 "neutral"——t30 那一轮把已判好的 correct / incorrect 降级成 neutral。

生产实测（只读，`pheromone.db::agent_memory`，2026-10-16）：23,800 行里 t1 非空 3,191、t7 6,616、
t30 10,812，至少两个 horizon 同时非空的只有 2,347 行——t30 > t7 > t1 正是「最后一轮赢」的形状。

这里**驱动真实的 `run()`**，三轮之间只靠本地快照价推进（`_yf` 置 None ⇒ 不出网、要出网的格子全记 deferred），
因为缺陷就在「多轮」这个维度上，单轮测试一条也抓不到。日期全部固定在过去：它只和真实钟比「到期没有」，
过去的日子永远到期，拨钟审计向前拨不会翻转结论。
"""

import json
import os
import sqlite3
from datetime import datetime

import pytest

import outcomes_fetcher as OF
from is_trading_day import add_trading_days

TICKER = "AAPL"
BASE = "2026-03-02"          # 周一；三个 horizon 的目标日都早已过去
ENTRY = 100.0
PX = {1: 101.0, 7: 110.0, 30: 95.0}   # T+1 +1%、T+7 +10%（Long ⇒ correct）、T+30 −5%


def _target(off: int) -> str:
    return add_trading_days(datetime.strptime(BASE, "%Y-%m-%d"), off).strftime("%Y-%m-%d")


def _write_snap(d, date, entry, direction="Long"):
    data = {
        "ticker": TICKER, "date": date, "composite_score": 7.5, "direction": direction,
        "price_target": 120.0, "stop_loss": 90.0, "entry_price": entry,
        "agent_votes": {}, "weights_used": {},
        "actual_prices": {"t1": None, "t7": None, "t30": None},
        "created_at": "%sT16:30:00" % date,
    }
    with open(os.path.join(d, "%s_%s.json" % (TICKER, date)), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _row(ms, mid):
    conn = ms._connect()
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT actual_outcome, outcome_return_t1 AS t1, outcome_return_t7 AS t7, "
        "outcome_return_t30 AS t30 FROM agent_memory WHERE memory_id = ?", (mid,)).fetchone()
    return dict(r)


def _run(d, ms):
    return OF.OutcomesFetcher(d, memory_store=ms, rate_limit=0).run()


@pytest.fixture
def setup(tmp_path, memory_store, monkeypatch):
    monkeypatch.setattr(OF, "_yf", None)      # 只走本地快照价；要出网的格子记 deferred
    d = tmp_path / "report_snapshots"
    d.mkdir()
    _write_snap(str(d), BASE, ENTRY)
    mids = [memory_store.save_agent_memory(
        {"date": BASE, "ticker": TICKER, "agent_id": a, "direction": "bullish",
         "discovery": "x", "source": "x", "self_score": 7.0}, "s1")
        for a in ("ScoutBeeNova", "BuzzBeeWhisper")]
    assert all(mids)
    return str(d), memory_store, mids


class TestHorizonsAccumulateAcrossPasses:

    def test_three_passes_t1_then_t7_then_t30(self, setup):
        d, ms, mids = setup

        # 第 1 轮：只有 T+1 目标日的本地价
        _write_snap(d, _target(1), PX[1])
        _run(d, ms)
        for mid in mids:
            r = _row(ms, mid)
            assert r["t1"] == pytest.approx(0.01)
            assert r["t7"] is None and r["t30"] is None
            # 没有 T+7 ⇒ 方向对错还判不了：留空，不写成 "neutral"
            #（「还判不了」≠「判为中性」；曾有读者把 neutral 计进准确率分母）
            assert r["actual_outcome"] is None

        # 第 2 轮：T+7 到了 —— T+1 必须还在
        _write_snap(d, _target(7), PX[7])
        _run(d, ms)
        for mid in mids:
            r = _row(ms, mid)
            assert r["t1"] == pytest.approx(0.01), "第 2 轮把 T+1 覆写成了 %r" % r["t1"]
            assert r["t7"] == pytest.approx(0.10)
            assert r["t30"] is None
            assert r["actual_outcome"] == "correct"

        # 第 3 轮：只到 T+30 —— T+1 / T+7 与已判的 correct 都不许被抹掉 / 降级
        _write_snap(d, _target(30), PX[30])
        _run(d, ms)
        for mid in mids:
            r = _row(ms, mid)
            assert r["t1"] == pytest.approx(0.01), "第 3 轮把 T+1 覆写成了 %r" % r["t1"]
            assert r["t7"] == pytest.approx(0.10), "第 3 轮把 T+7 覆写成了 %r" % r["t7"]
            assert r["t30"] == pytest.approx(-0.05)
            assert r["actual_outcome"] == "correct", "第 3 轮把 outcome 降级成了 %r" % r["actual_outcome"]

    def test_all_three_in_one_pass_still_written(self, setup):
        """正对照：一轮拿齐三个 horizon 的老路径不受影响。"""
        d, ms, mids = setup
        for off, px in PX.items():
            _write_snap(d, _target(off), px)
        _run(d, ms)
        r = _row(ms, mids[0])
        assert (r["t1"], r["t7"], r["t30"]) == (
            pytest.approx(0.01), pytest.approx(0.10), pytest.approx(-0.05))
        assert r["actual_outcome"] == "correct"


class TestUpdateMemoryOutcomeIsPerColumn:
    """存储层自己也得守住：None 的意思是「本次没有这一格」，不是「把这一格清空」。"""

    def _mid(self, ms):
        return ms.save_agent_memory(
            {"date": BASE, "ticker": TICKER, "agent_id": "ScoutBeeNova", "direction": "bullish",
             "discovery": "x", "source": "x", "self_score": 7.0}, "s1")

    def test_none_leaves_existing_values(self, memory_store):
        mid = self._mid(memory_store)
        assert memory_store.update_memory_outcome(mid, "correct", t1=0.01, t7=0.10)
        assert memory_store.update_memory_outcome(mid, None, t30=-0.05)
        r = _row(memory_store, mid)
        assert r == {"actual_outcome": "correct", "t1": pytest.approx(0.01),
                     "t7": pytest.approx(0.10), "t30": pytest.approx(-0.05)}

    def test_new_values_still_overwrite(self, memory_store):
        """COALESCE 只挡 None，不挡真值：同一格给了新值照样写进去。"""
        mid = self._mid(memory_store)
        memory_store.update_memory_outcome(mid, "neutral", t7=0.001)
        memory_store.update_memory_outcome(mid, "correct", t7=0.10)
        r = _row(memory_store, mid)
        assert r["t7"] == pytest.approx(0.10) and r["actual_outcome"] == "correct"

    def test_missing_row_still_reports_false(self, memory_store):
        assert memory_store.update_memory_outcome("no-such-id", None, t1=0.01) is False
