"""report_snapshots 读回再存不许丢字段 / 改生成时刻（v0.45.491）。

事故：`ReportSnapshot.load_from_json` 漏读 `cs_rank`、`save_to_json` 恒写 `created_at = now()`。
`outcomes_fetcher._update_snapshot`（每次扫描后台回填 T+1/T+7/T+30）走「读 → 填一格价格 → 存」，
于是每回填一次就把旧快照的 cs_rank 抹成 null、created_at 推到回填时刻。
数据备份仓库 435482c（10-09）/ ee261ff（10-12）逐日可见；10-12 生产 1771 份里 866 份 cs_rank 已被抹。

既有的 round-trip 测试只断言「几个手挑的字段读回相等」——清单是手抄的，漏一个字段谁都不红。
这里改成**按原文整份比**：读回不改就存，文件内容必须与原文完全相同。
"""

import copy
import json
import math

import pytest

from feedback_loop import ReportSnapshot


# 生产现行形状（10-12 生产 1046 份的键集合）+ 一个本类不认识的键 + actual_prices 里一个未知子键。
PROD_SHAPE = {
    "ticker": "ABBV",
    "date": "2026-10-05",
    "composite_score": 5.91,
    "direction": "bullish",
    "price_target": 0.0,
    "stop_loss": 0.0,
    "entry_price": 262.13,
    "agent_votes": {"ScoutBeeNova": 6.1, "BuzzBeeWhisper": 5.4, "QueenDistiller": 5.91},
    "agent_votes_source": "agent_details",
    "weights_used": {"signal": 0.0, "catalyst": 0.3, "sentiment": 0.4, "odds": 0.3, "risk_adj": 0.0},
    "low_conviction": False,
    "low_conviction_reason": "",
    "cs_rank": {"final": 0.345, "signal": 0.31, "catalyst": 0.379, "sentiment": 1.0,
                "odds": 0.241, "risk_adj": 0.172},
    "actual_prices": {"t1": None, "t7": None, "t30": None, "t90_future": 1.5},
    "created_at": "2026-10-05T14:42:11.994497",
    "some_future_field": {"nested": [1, 2, 3]},
}

# v0.40 之前的旧形状（10-12 生产仍有 321 份）：没有 cs_rank / low_conviction / agent_votes_source。
LEGACY_SHAPE = {
    "ticker": "NVDA",
    "date": "2026-03-10",
    "composite_score": 7.2,
    "direction": "Long",
    "price_target": 0.0,
    "stop_loss": 0.0,
    "entry_price": 0.0,
    "agent_votes": {"ScoutBeeNova": 7.0},
    "weights_used": {"signal": 0.3, "catalyst": 0.2, "sentiment": 0.2, "odds": 0.15, "risk_adj": 0.15},
    "actual_prices": {"t1": 180.0, "t7": None, "t30": None},
    "created_at": "2026-03-10T15:01:02.000001",
}


def _write(tmp_path, data, name=None):
    d = tmp_path / "report_snapshots"
    d.mkdir(exist_ok=True)
    p = d / (name or f"{data['ticker']}_{data['date']}.json")
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("shape", [PROD_SHAPE, LEGACY_SHAPE], ids=["prod", "legacy"])
def test_load_then_save_unchanged_is_identity(tmp_path, shape):
    """读回、什么都不改、存盘 ⇒ 文件内容与原文完全一致（含未知键、缺失键保持缺失）。"""
    p = _write(tmp_path, shape)
    ReportSnapshot.load_from_json(str(p)).save_to_json(str(p.parent))
    assert _read(p) == shape


@pytest.mark.parametrize("shape", [PROD_SHAPE, LEGACY_SHAPE], ids=["prod", "legacy"])
def test_backfill_one_price_changes_only_that_price(tmp_path, shape):
    """回填一格价格 ⇒ 只有那一格变；cs_rank / created_at / 其余一切照旧。"""
    p = _write(tmp_path, shape)
    snap = ReportSnapshot.load_from_json(str(p))
    snap.actual_price_t7 = 271.5
    snap.save_to_json(str(p.parent))

    expect = copy.deepcopy(shape)
    expect["actual_prices"]["t7"] = 271.5
    assert _read(p) == expect


def test_outcomes_fetcher_backfill_keeps_cs_rank_and_created_at(tmp_path):
    """生产写者本身：OutcomesFetcher._update_snapshot（扫描后台每天跑的那条）。"""
    from outcomes_fetcher import OutcomesFetcher

    p = _write(tmp_path, PROD_SHAPE)
    fetcher = OutcomesFetcher(str(p.parent))
    snap = ReportSnapshot.load_from_json(str(p))
    assert fetcher._update_snapshot(str(p), snap, {1: 266.69}) is True

    after = _read(p)
    assert after["actual_prices"]["t1"] == 266.69
    assert after["cs_rank"] == PROD_SHAPE["cs_rank"]
    assert after["created_at"] == PROD_SHAPE["created_at"]
    assert after["some_future_field"] == PROD_SHAPE["some_future_field"]


def test_every_saved_field_is_read_back(tmp_path):
    """新快照：每个由 save_to_json 写出的字段都要被 load_from_json 读回（防「加了写、忘了读」）。

    每个字段都设成非缺省值，读回后整份 _payload 必须相等——下次谁往 _payload 加字段
    而没在 load_from_json 里读它，这条就红（cs_rank 当年就是这么漏的）。
    """
    s = ReportSnapshot("TSLA", "2026-10-12")
    s.composite_score = 6.4
    s.direction = "bearish"
    s.price_target = 400.0
    s.stop_loss = 380.0
    s.entry_price = 391.2
    s.agent_votes = {"ScoutBeeNova": 4.2}
    s.agent_votes_source = "agent_details"
    s.weights_used = {"signal": 0.1}
    s.low_conviction = True
    s.low_conviction_reason = "score_high_guard"
    s.cs_rank = {"final": 0.793}
    s.actual_price_t1 = 1.0
    s.actual_price_t7 = 7.0
    s.actual_price_t30 = 30.0
    f = s.save_to_json(str(tmp_path))

    back = ReportSnapshot.load_from_json(f)
    defaults = ReportSnapshot("X", "Y")._payload()
    for k, v in s._payload().items():
        if k not in ("ticker", "date", "created_at"):
            assert v != defaults[k], f"夹具没把 {k} 设成非缺省值，这条测不到它"
    assert back._payload() == s._payload()


def test_fresh_snapshot_created_at_is_stamped_once(tmp_path):
    """新快照首存盖戳；同一对象再存、读回再存都不改。"""
    s = ReportSnapshot("AMD", "2026-10-12")
    f = s.save_to_json(str(tmp_path))
    first = _read_path(f)["created_at"]
    assert first and s.created_at == first

    s.actual_price_t1 = 150.0
    s.save_to_json(str(tmp_path))
    assert _read_path(f)["created_at"] == first

    back = ReportSnapshot.load_from_json(f)
    back.actual_price_t7 = 155.0
    back.save_to_json(str(tmp_path))
    assert _read_path(f)["created_at"] == first


def test_null_entry_price_is_not_rewritten_as_zero(tmp_path):
    """读时把 null 入场价规范成 0.0（v0.45.452），没人改它就不能把 0.0 写回盘里。"""
    shape = dict(PROD_SHAPE, entry_price=None)
    p = _write(tmp_path, shape)
    snap = ReportSnapshot.load_from_json(str(p))
    assert snap.entry_price == 0.0
    snap.actual_price_t1 = 1.0
    snap.save_to_json(str(p.parent))
    assert _read(p)["entry_price"] is None


def test_old_nan_entry_price_counts_as_unchanged(tmp_path):
    """旧文件里的 NaN 入场价：读回不改 ⇒ 视为未改动（NaN != NaN 不能被当成「改过」）。"""
    p = tmp_path / "RKLB_2026-06-26.json"
    p.write_text('{"ticker": "RKLB", "date": "2026-06-26", "entry_price": NaN, '
                 '"cs_rank": {"final": 0.5}, "actual_prices": {"t1": null}}', encoding="utf-8")
    snap = ReportSnapshot.load_from_json(str(p))
    assert math.isnan(snap.entry_price)
    snap.actual_price_t1 = 2.0
    snap.save_to_json(str(tmp_path))
    after = _read(p)
    assert after["cs_rank"] == {"final": 0.5}
    assert after["actual_prices"] == {"t1": 2.0}
    assert "t7" not in after["actual_prices"]  # 原文没有的子键不补


def test_in_place_mutation_is_detected(tmp_path):
    """就地改 dict（agent_votes[k] = …）也算改动、要写盘——at_load 必须是深拷贝。"""
    p = _write(tmp_path, PROD_SHAPE)
    snap = ReportSnapshot.load_from_json(str(p))
    snap.agent_votes["BearBeeContrarian"] = 3.3
    snap.save_to_json(str(p.parent))
    assert _read(p)["agent_votes"]["BearBeeContrarian"] == 3.3


def test_explicit_change_to_known_field_is_written(tmp_path):
    """反向对照：真改了的已知字段必须写盘（证明合并不是一律照抄原文）。"""
    p = _write(tmp_path, PROD_SHAPE)
    snap = ReportSnapshot.load_from_json(str(p))
    snap.cs_rank = None
    snap.direction = "neutral"
    snap.save_to_json(str(p.parent))
    after = _read(p)
    assert after["cs_rank"] is None and after["direction"] == "neutral"


def _read_path(f):
    with open(f, encoding="utf-8") as fh:
        return json.load(fh)
