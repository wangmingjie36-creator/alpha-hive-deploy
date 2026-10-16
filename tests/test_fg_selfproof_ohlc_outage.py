"""v0.45.509：生产纸面组合「日线取不到」的记录，与 F&G 敞口门自证按生产当时没有日线重放故障日（事后修订）。

10-14（及 09-24/25）生产 `_fetch_ohlc` 全被限流：当天 0 出场、仓位按入场价估值，新仓按没扣出场的净值定额，
`run_for_date` 照常成功。自证拿有日线的重放去比 ⇒ 4 笔金额对不上（28/32，无法判定）。本文件钉：

1. 生产：本轮日线一次都没取到的标的进净值行 `ohlc_unavailable`、时有时无的进 `ohlc_partial`、都没有时不写键；
2. `replay_ohlc_mask`：只在重放、只对那天那几只生效，退出恢复；
3. 端到端（**真实** `_fetch_ohlc` 路径 + 假 yfinance）：故障日不遮 ⇒ 金额层红；遮了 ⇒ 逐笔复现，且那天的净值行
   与生产逐字节相同；遮罩不让自证恒绿（仓位参数一改照样红）；统计量的连续 A / B 不遮；
4. 遮哪天只认两个来源（生产净值行 / 历史登记表），故障形状却两处都没记的日子报出来、不自动遮。
"""
from __future__ import annotations

import json
import logging
import sys
import types

import pytest

import paper_portfolio as pp
from tests.test_fg_exposure_gate_forward_test import (
    _S_D1, _S_D2, _S_D3, _S_D4, _S_DATES, _S_BEFORE, _S_SINCE, _assert_no_unexpected_top_level_keys,
    _make_seed, _write_snapshot, fwd,
)


class FakeYF:
    """yfinance 替身：所有标的平价 100，`spikes={(标的, 日期): High}`；`down` 里的标的（`"ALL"` = 全部）history 抛异常
    （同生产 10-14 的 YFRateLimited）；`flaky[标的] = n` ⇒ 前 n 次抛、之后正常。"""

    def __init__(self, spikes=None):
        self.spikes = spikes or {}
        self.down: set = set()
        self.flaky: dict = {}
        self.calls = 0

    def module(self):
        return types.SimpleNamespace(Ticker=self._ticker)

    def _ticker(self, t):
        fake = self

        class _T:
            def history(self, start, end, auto_adjust=False, **kw):
                import pandas as pd
                fake.calls += 1
                if "ALL" in fake.down or t in fake.down:
                    raise RuntimeError(f"fake rate limit {t}")
                if fake.flaky.get(t, 0) > 0:
                    fake.flaky[t] -= 1
                    raise RuntimeError(f"fake flaky {t}")
                days = [d for d in pd.bdate_range(start, end, inclusive="left")]
                rows = [[100.0, fake.spikes.get((t, d.strftime("%Y-%m-%d")), 100.5), 99.5, 100.0] for d in days]
                return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.DatetimeIndex(days))
        return _T()


@pytest.fixture
def world(monkeypatch, tmp_path):
    """真实 `_fetch_ohlc` → `_fetch_ohlc_direct` → 假 yfinance。D1 开 NVDA；D2 NVDA 冲过止盈、开 AMD；D4 开 MSFT。"""
    fake = FakeYF({("NVDA", _S_D2): 130.0})
    monkeypatch.setitem(sys.modules, "yfinance", fake.module())
    snap = tmp_path / "snapshots"
    snap.mkdir()
    monkeypatch.setattr(pp, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(pp, "_pheromone_db_path", lambda: tmp_path / "fg.db")
    monkeypatch.setattr(pp, "_PRICE_CACHE", {})
    monkeypatch.setattr(pp, "_OHLC_FULL", {})
    for t, d in (("NVDA", _S_D1), ("AMD", _S_D2), ("MSFT", _S_D4)):
        _write_snapshot(snap, t, d, 7.5, "bullish")
    return types.SimpleNamespace(fake=fake, snap=snap, tmp=tmp_path, monkeypatch=monkeypatch)


def _production(w, outage=None):
    """生产：每天一个新进程（缓存清空），`outage={日期: 标的集合 | {"ALL"}}` 那天 yfinance 对它们抛异常。
    逐日收盘状态写成锚点历史；最后把生产账本放到 `pp.*_FILE`（conftest 沙箱）给自证读。"""
    outage = outage or {}
    prod = w.tmp / "prod"
    prod.mkdir()
    seed = _make_seed()
    for n, b in seed.items():
        (prod / n).write_bytes(b)
    history, results = {}, {}
    for d in _S_DATES:
        pp._PRICE_CACHE.clear()
        w.fake.down = set(outage.get(d, ()))
        results[d] = pp.run_replay({}, prod, dates=[d])
        history[d] = {n: (prod / n).read_bytes() for n in fwd.SEED_STATE_FILES}
    w.fake.down = set()
    pp._PRICE_CACHE.clear()
    pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    for n, dst in (("closed_trades.jsonl", pp.CLOSED_FILE), ("positions.jsonl", pp.POSITIONS_FILE),
                   ("equity_curve.jsonl", pp.EQUITY_FILE)):
        dst.write_bytes((prod / n).read_bytes())
    return seed, history, prod


def _plan(seed, history, *, mask=True):
    plan = fwd._anchor_plan(seed, [("测试历史", history)], _S_DATES, processed=set(_S_DATES))
    return fwd._attach_ohlc_mask(plan, _S_DATES) if mask else plan


def _equity(path):
    return {r["date"]: r for r in pp._load_jsonl(path)}


# ═══════════════════════════════ 1. 生产记录 ═══════════════════════════════

class TestProductionRecordsOhlcGaps:
    def test_healthy_day_writes_no_new_keys(self, world):
        _, _, prod = _production(world)
        for row in _equity(prod / "equity_curve.jsonl").values():
            assert set(row) == {"date", "cash", "deployed", "unrealized", "nav", "positions_count",
                                "trades_closed_today", "realized_pnl_today"}, row

    def test_outage_day_is_marked_with_every_ticker_it_never_got(self, world, caplog):
        with caplog.at_level(logging.ERROR, logger="alpha_hive.paper_portfolio"):
            _, _, prod = _production(world, {_S_D2: {"ALL"}})
        eq = _equity(prod / "equity_curve.jsonl")
        assert eq[_S_D2]["ohlc_unavailable"] == ["AMD", "NVDA"]
        assert eq[_S_D2]["unrealized"] == 0 and eq[_S_D2]["trades_closed_today"] == 0   # 10-14 的形状
        assert all("ohlc_unavailable" not in eq[d] for d in (_S_D1, _S_D3, _S_D4))
        assert any("日线一次都没取到" in r.getMessage() and "AMD、NVDA" in r.getMessage() for r in caplog.records)

    def test_single_ticker_outage_names_only_it(self, world):
        _, _, prod = _production(world, {_S_D2: {"AMD"}})
        assert _equity(prod / "equity_curve.jsonl")[_S_D2]["ohlc_unavailable"] == ["AMD"]

    def test_flaky_ticker_is_partial_not_unavailable(self, world):
        world.fake.flaky["NVDA"] = 1
        _, _, prod = _production(world)
        row = _equity(prod / "equity_curve.jsonl")[_S_D1]
        assert row.get("ohlc_partial") == ["NVDA"] and "ohlc_unavailable" not in row

    def test_return_value_and_outcomes_reset(self, world):
        prod = world.tmp / "p2"
        prod.mkdir()
        for n, b in _make_seed().items():
            (prod / n).write_bytes(b)
        world.fake.down = {"ALL"}
        seen = {}
        orig = pp._run_for_date
        world.monkeypatch.setattr(pp, "_run_for_date", lambda *a, **k: seen.setdefault("r", orig(*a, **k)))
        pp.run_replay({}, prod, dates=[_S_D1])
        assert seen["r"]["ohlc_unavailable"] == ["NVDA"] and seen["r"]["ohlc_partial"] == []
        assert pp._OHLC_RUN_OUTCOMES is None

    def test_outcomes_reset_after_exception(self, world):
        def boom(*a, **k):
            pp._note_ohlc("X", False)
            raise ValueError("boom")
        world.monkeypatch.setattr(pp, "_run_for_date", boom)
        with pytest.raises(ValueError):
            pp.run_for_date(_S_D1)
        assert pp._OHLC_RUN_OUTCOMES is None

    def test_outside_a_run_nothing_is_recorded(self, world):
        world.fake.down = {"ALL"}
        assert pp._fetch_ohlc("NVDA", _S_D1, _S_D2) == {}
        assert pp._OHLC_RUN_OUTCOMES is None


# ═══════════════════════════════ 2. 遮罩本身 ═══════════════════════════════

class TestReplayOhlcMask:
    def test_only_inside_replay_and_only_named_day_and_ticker(self, world):
        with pp.replay_ohlc_mask({_S_D2: frozenset({"AMD"})}) as hits:
            assert pp._fetch_ohlc("AMD", _S_D1, _S_D3)              # _REPLAY_AS_OF 是 None ⇒ 生产路径，不遮
            world.monkeypatch.setattr(pp, "_REPLAY_AS_OF", _S_D2)
            assert pp._fetch_ohlc("AMD", _S_D1, _S_D3) == {}
            assert pp._fetch_ohlc("NVDA", _S_D1, _S_D3)             # 别的标的不遮
            world.monkeypatch.setattr(pp, "_REPLAY_AS_OF", _S_D3)
            assert pp._fetch_ohlc("AMD", _S_D1, _S_D4)              # 别的日子不遮
        assert hits == {_S_D2: 1} and pp._REPLAY_OHLC_MASK is None

    def test_none_means_every_ticker(self, world):
        world.monkeypatch.setattr(pp, "_REPLAY_AS_OF", _S_D2)
        with pp.replay_ohlc_mask({_S_D2: None}):
            assert pp._fetch_ohlc("ZZZ", _S_D1, _S_D3) == {}

    def test_restored_on_exception_and_bad_input_rejected(self):
        with pytest.raises(RuntimeError), pp.replay_ohlc_mask({_S_D2: None}):
            raise RuntimeError
        assert pp._REPLAY_OHLC_MASK is None
        for bad in ({"10/14": None}, {_S_D2: {"AMD"}}, {_S_D2: "AMD"}):
            with pytest.raises(ValueError), pp.replay_ohlc_mask(bad):
                pass


# ═══════════════════════════════ 3. 端到端 ═══════════════════════════════

class TestSelfproofReplaysOutageDaysAsProductionSawThem:
    def test_unmasked_is_red_in_the_amount_layer(self, world):
        """对照（v0.45.509 之前的行为）：生产 D2 没出场 ⇒ AMD 按没扣 NVDA 止盈的净值定额，有日线的重放对不上。"""
        seed, hist, _ = _production(world, {_S_D2: {"ALL"}})
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed,
                           plan=_plan(seed, hist, mask=False))
        assert res["status"] == "cannot_judge" and "仓位金额" in res["reason"]
        assert res["selfproof"]["decision_reproduced"] == res["selfproof"]["real_entries"]
        assert res["selfproof"]["reproduced"] < res["selfproof"]["real_entries"]
        assert f"{_S_D2}×1" in res["reason"]

    def test_masked_reproduces_every_entry_and_the_outage_row_byte_for_byte(self, world):
        seed, hist, prod = _production(world, {_S_D2: {"ALL"}})
        plan = _plan(seed, hist)
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed, plan=plan)
        sp = res["selfproof"]
        assert sp["reproduced"] == sp["real_entries"] == 3 and sp["a_only_entries"] == 0, sp
        assert res["status"] == "not_ready"
        seg = res["selfproof_segments"]
        assert seg["ohlc_outage_days"] == {_S_D2: ["AMD", "NVDA"]}
        assert seg["ohlc_masked_requests"].get(_S_D2, 0) > 0 and set(seg["ohlc_masked_requests"]) == {_S_D2}
        _assert_no_unexpected_top_level_keys(res)
        # D2 那段（第 2 段）的净值行与生产逐字节相同——连 ohlc_unavailable 都一样
        i = [k for k, (_, ds) in enumerate(plan["segments"]) if ds == [_S_D2]][0]
        seg_row = _equity(world.tmp / "sb" / f"A_seg_{i:03d}" / "equity_curve.jsonl")[_S_D2]
        assert json.dumps(seg_row, sort_keys=True) == json.dumps(_equity(prod / "equity_curve.jsonl")[_S_D2],
                                                                 sort_keys=True)

    def test_per_ticker_mask_is_enough_when_only_one_ticker_failed(self, world):
        seed, hist, _ = _production(world, {_S_D2: {"NVDA"}})
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed,
                           plan=_plan(seed, hist))
        assert res["selfproof_segments"]["ohlc_outage_days"] == {_S_D2: ["NVDA"]}
        assert res["selfproof"]["reproduced"] == res["selfproof"]["real_entries"]

    def test_mask_does_not_make_the_selfproof_always_green(self, world):
        """「谁会红？」：遮罩只换行情输入，仓位参数被改照样红。"""
        seed, hist, _ = _production(world, {_S_D2: {"ALL"}})
        world.monkeypatch.setitem(pp.CONFIG, "size_pct_by_tier", {**pp.CONFIG["size_pct_by_tier"], "high": 4.0})
        world.monkeypatch.setitem(pp.CONFIG, "sizing_mode", "tier")
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed,
                           plan=_plan(seed, hist))
        assert res["status"] == "cannot_judge" and res["selfproof"]["reproduced"] == 0

    def test_statistic_arms_are_never_masked(self, world):
        seed, hist, _ = _production(world, {_S_D2: {"ALL"}})
        seen = []
        orig = fwd._replay_variant

        def spy(cfg, state_dir, dates, seed=None):
            seen.append((state_dir.name, pp._REPLAY_OHLC_MASK))
            return orig(cfg, state_dir, dates, seed=seed)
        world.monkeypatch.setattr(fwd, "_replay_variant", spy)
        fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed, plan=_plan(seed, hist))
        arms = {n: m for n, m in seen if n in ("A_baseline", "B_treatment")}
        assert set(arms) == {"A_baseline", "B_treatment"} and all(m is None for m in arms.values())
        assert all(m == {_S_D2: frozenset({"AMD", "NVDA"})} for n, m in seen if n.startswith("A_seg_"))


# ═══════════════════════════════ 4. 遮哪天 ═══════════════════════════════

def _write_equity(rows):
    pp.EQUITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    pp.EQUITY_FILE.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(d, unrealized=1.0, positions=3, **extra):
    return {"date": d, "cash": 1.0, "deployed": 1.0, "unrealized": unrealized, "nav": 3.0,
            "positions_count": positions, "trades_closed_today": 0, "realized_pnl_today": 0.0, **extra}


class TestWhichDaysAreMasked:
    def test_sources_and_reports(self, monkeypatch):
        monkeypatch.setattr(fwd, "HISTORICAL_OHLC_OUTAGES", {"2026-09-24": "日志证据"})
        _write_equity([_row("2026-09-24", 0.0), _row("2026-09-25", 0.0),        # 后者：形状像、两处都没记
                       _row("2026-09-26", 0.0, 0),                              # 空仓的 0 不是故障形状
                       _row("2026-10-20", ohlc_unavailable=["AMD"], ohlc_partial=["MU"]),
                       _row("2026-10-21", ohlc_partial=["MU"])])
        mask, info = fwd._production_ohlc_mask(["2026-09-24", "2026-09-25", "2026-09-26", "2026-10-20", "2026-10-21"])
        assert mask == {"2026-09-24": None, "2026-10-20": frozenset({"AMD"})}
        assert info["ohlc_outage_days"] == {"2026-09-24": "全部", "2026-10-20": ["AMD"]}
        assert info["ohlc_outage_signature_unmarked"] == ["2026-09-25"]
        assert info["ohlc_partial_days"] == {"2026-10-20": ["MU"], "2026-10-21": ["MU"]}

    def test_only_dates_in_the_window(self, monkeypatch):
        _write_equity([_row("2026-10-20", ohlc_unavailable=["AMD"])])
        assert fwd._production_ohlc_mask(["2026-10-21"])[0] == {}

    def test_unmarked_signature_is_named_in_the_failure_reason(self):
        sp = {"total": 20, "exact": 10, "decision": 20, "a_only": 0, "rate": 0.5, "decision_rate": 1.0}
        r = fwd._selfproof_failure_reason(sp, None, {"ohlc_outage_signature_unmarked": ["2026-10-22"]})
        assert "2026-10-22" in r and "HISTORICAL_OHLC_OUTAGES" in r

    def test_historical_registry_is_closed(self):
        """登记表只收 v0.45.509 之前（生产还不会自己写 `ohlc_unavailable`）的日子，各带取证——之后的故障日由生产
        净值行自己说，不许往这里加（否则它会变成一个手动挑哪天遮的旋钮）。"""
        assert fwd.HISTORICAL_OHLC_OUTAGES
        for d, ev in fwd.HISTORICAL_OHLC_OUTAGES.items():
            assert d < "2026-10-16" and "_fetch_ohlc 失败" in ev and "unrealized=0.0" in ev, d


def test_both_anchor_plans_attach_the_mask():
    """前瞻与演练两条分段方案都要挂遮罩——漏一条，那条路径的故障日就退回有日线重放（上面的端到端测不到它们，
    因为它们直接拼方案）。"""
    import ast
    import inspect
    for fn in (fwd._forward_anchor_plan, fwd._rehearse_anchor_plan):
        tree = ast.parse(inspect.getsource(fn))
        rets = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
        assert rets and all(isinstance(r.value, ast.Call) and getattr(r.value.func, "id", "") == "_attach_ohlc_mask"
                            for r in rets), fn.__name__
