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
import scan_timing
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


def _production(w, outage=None, *, pre_535=False):
    """生产：每天一个新进程（缓存清空），`outage={日期: 标的集合 | {"ALL"}}` 那天 yfinance 对它们抛异常。
    逐日收盘状态写成锚点历史；最后把生产账本放到 `pp.*_FILE`（conftest 沙箱）给自证读。

    `pre_535=True`：模拟 v0.45.535 之前的生产——持仓日线全断也照常跑（09-24 / 09-25 / 10-14 当时就是这样），
    即那三天真实生产账本的形状；缺省是现行代码（全断 ⇒ 整天跳过）。"""
    outage = outage or {}
    orig_rule = pp._held_ohlc_outage
    if pre_535:
        pp._held_ohlc_outage = lambda held: False
    prod = w.tmp / "prod"
    prod.mkdir()
    seed = _make_seed()
    for n, b in seed.items():
        (prod / n).write_bytes(b)
    history, results = {}, {}
    try:
        for d in _S_DATES:
            pp._PRICE_CACHE.clear()
            w.fake.down = set(outage.get(d, ()))
            results[d] = pp.run_replay({}, prod, dates=[d])
            history[d] = {n: (prod / n).read_bytes() for n in fwd.SEED_STATE_FILES}
    finally:
        pp._held_ohlc_outage = orig_rule
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
        """（pre_535：历史故障日的账本形状。）这里经 `run_replay` 跑 ⇒ v0.45.535 起这条只记 INFO、不打 ERROR——
        F&G 自证每天按「没有日线」复刻 09-24 / 09-25 / 10-14，打 ERROR 就是每天三条与当天无关的「故障」。
        变红的变异：重放里仍打 ERROR。"""
        with caplog.at_level(logging.INFO, logger="alpha_hive.paper_portfolio"):
            _, _, prod = _production(world, {_S_D2: {"ALL"}}, pre_535=True)
        eq = _equity(prod / "equity_curve.jsonl")
        assert eq[_S_D2]["ohlc_unavailable"] == ["AMD", "NVDA"]
        assert eq[_S_D2]["unrealized"] == 0 and eq[_S_D2]["trades_closed_today"] == 0   # 10-14 的形状
        assert all("ohlc_unavailable" not in eq[d] for d in (_S_D1, _S_D3, _S_D4))
        hits = [r for r in caplog.records if "日线一次都没取到" in r.getMessage() and "AMD、NVDA" in r.getMessage()]
        assert hits and all(r.levelno == logging.INFO for r in hits)

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
        seed, hist, _ = _production(world, {_S_D2: {"ALL"}}, pre_535=True)
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed,
                           plan=_plan(seed, hist, mask=False))
        assert res["status"] == "cannot_judge" and "仓位金额" in res["reason"]
        assert res["selfproof"]["decision_reproduced"] == res["selfproof"]["real_entries"]
        assert res["selfproof"]["reproduced"] < res["selfproof"]["real_entries"]
        assert f"{_S_D2}×1" in res["reason"]

    def test_masked_reproduces_every_entry_and_the_outage_row_byte_for_byte(self, world):
        seed, hist, prod = _production(world, {_S_D2: {"ALL"}}, pre_535=True)
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
        seed, hist, _ = _production(world, {_S_D2: {"NVDA"}}, pre_535=True)
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed,
                           plan=_plan(seed, hist))
        assert res["selfproof_segments"]["ohlc_outage_days"] == {_S_D2: ["NVDA"]}
        assert res["selfproof"]["reproduced"] == res["selfproof"]["real_entries"]

    def test_mask_does_not_make_the_selfproof_always_green(self, world):
        """「谁会红？」：遮罩只换行情输入，仓位参数被改照样红。"""
        seed, hist, _ = _production(world, {_S_D2: {"ALL"}}, pre_535=True)
        world.monkeypatch.setitem(pp.CONFIG, "size_pct_by_tier", {**pp.CONFIG["size_pct_by_tier"], "high": 4.0})
        world.monkeypatch.setitem(pp.CONFIG, "sizing_mode", "tier")
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed,
                           plan=_plan(seed, hist))
        assert res["status"] == "cannot_judge" and res["selfproof"]["reproduced"] == 0

    def test_statistic_arms_are_never_masked(self, world):
        seed, hist, _ = _production(world, {_S_D2: {"ALL"}}, pre_535=True)
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



# ═══════════════════════════════ 5. v0.45.535：持仓日线全断 ⇒ 整天跳过 ═══════════════════════════════

def _seed_live(positions=()):
    """生产（直接 `run_for_date`，非重放）：把种子写进 conftest 沙箱的 `pp.*_FILE`。"""
    pp.POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    for n, b in _make_seed(positions=positions).items():
        (pp.POSITIONS_FILE.parent / n).write_bytes(b)


def _ledger_bytes():
    return {n: (pp.POSITIONS_FILE.parent / n).read_bytes() if (pp.POSITIONS_FILE.parent / n).exists() else None
            for n in ("positions.jsonl", "closed_trades.jsonl", "equity_curve.jsonl", "meta.json")}


_FG = {"value": 50.0, "is_cnn": True}


class TestWholeDaySkip:
    def test_production_skips_the_day_and_writes_nothing(self, world, caplog, monkeypatch):
        """D1 开 NVDA；D2 全断 ⇒ 不开 AMD、不写净值 / 持仓 / meta（逐字节不动）、ERROR、run_stats 带 skipped。
        变红的变异：去掉跳过（D2 照开 AMD、写净值行）；跳过后仍写账本；run_stats 不带 skipped。"""
        monkeypatch.setattr(pp, "_LAST_RUN", None)
        _seed_live()
        pp.run_for_date(_S_D1, market_fear_greed=_FG)
        before = _ledger_bytes()
        pp._PRICE_CACHE.clear()
        world.fake.down = {"ALL"}
        with caplog.at_level(logging.ERROR, logger="alpha_hive.paper_portfolio"):
            res = pp.run_for_date(_S_D2, market_fear_greed=_FG)
        assert res["skipped"] == "ohlc_unavailable" and res["ohlc_unavailable"] == ["NVDA"]
        assert _ledger_bytes() == before
        assert pp.run_stats() == {"as_of": _S_D2, "ok": True, "error": None, "skipped": "ohlc_unavailable",
                                  "ohlc_unavailable": ["NVDA"]}
        assert any(r.levelno == logging.ERROR and "整天跳过" in r.getMessage() for r in caplog.records)

    def test_next_run_books_the_missed_exit_on_its_real_day(self, world, monkeypatch):
        """跳过之后下一次运行照常从 (entry_date, as_of] 补查：NVDA 在 D2 冲过止盈 ⇒ D3 运行记出场、出场日是 D2。"""
        _seed_live()
        pp.run_for_date(_S_D1, market_fear_greed=_FG)
        pp._PRICE_CACHE.clear()
        world.fake.down = {"ALL"}
        pp.run_for_date(_S_D2, market_fear_greed=_FG)
        pp._PRICE_CACHE.clear()
        world.fake.down = set()
        pp.run_for_date(_S_D3, market_fear_greed=_FG)
        closed = pp._load_jsonl(pp.CLOSED_FILE)
        assert [(t["ticker"], t["exit_date"]) for t in closed] == [("NVDA", _S_D2)]
        assert _S_D2 not in _equity(pp.EQUITY_FILE)

    def test_partial_outage_is_not_skipped(self, world):
        """只有新仓那只取不到（持仓 NVDA 正常）⇒ 照常跑、只打 ohlc_unavailable 标记。"""
        _, _, prod = _production(world, {_S_D2: {"AMD"}})
        assert _S_D2 in _equity(prod / "equity_curve.jsonl")

    def test_one_of_two_held_down_is_not_skipped(self, world):
        """持仓两只（D1 开 NVDA、META）、D2 只有 NVDA 取不到 ⇒ 照常跑（META 能查出场），NVDA 记进 ohlc_unavailable。
        变红的变异：「每一只持仓都取不到」写成「任一只取不到」。"""
        _write_snapshot(world.snap, "META", _S_D1, 7.5, "bullish")
        _, _, prod = _production(world, {_S_D2: {"NVDA"}})
        eq = _equity(prod / "equity_curve.jsonl")
        assert _S_D2 in eq and "NVDA" in eq[_S_D2]["ohlc_unavailable"] and "META" not in eq[_S_D2]["ohlc_unavailable"]
        assert eq[_S_D1]["positions_count"] == 2

    def test_an_exit_then_the_rest_down_never_double_books(self, world):
        """v0.45.544（535 二次检查，高）：持仓 NVDA、META；D2 NVDA 取到日线并冲过止盈 ⇒ Step 1 已写平仓记录；
        META 取不到。535 原实现在 Step 1 之后取持仓 ⇒ 只剩 [META] ⇒ 判「全断」跳过 ⇒ NVDA 的平仓已落盘、持仓 / 现金
        没写 ⇒ D3 再平一次（重复记账）。变红的变异：持仓集合改回 Step 1 之后取，且去掉「已写平仓不跳过」保险。"""
        _write_snapshot(world.snap, "META", _S_D1, 7.5, "bullish")
        _, _, prod = _production(world, {_S_D2: {"META"}})
        closed = pp._load_jsonl(prod / "closed_trades.jsonl")
        assert [t["ticker"] for t in closed].count("NVDA") == 1, closed
        assert _S_D2 in _equity(prod / "equity_curve.jsonl")
        assert "NVDA" not in {p["ticker"] for p in pp._load_jsonl(prod / "positions.jsonl")}

    def test_written_exit_blocks_the_skip_even_if_the_rule_says_outage(self, world, monkeypatch):
        """第二道保险单独验：判据被（假设地）判成全断，但 Step 1 已写过平仓 ⇒ 仍照常跑完、账本一致。"""
        monkeypatch.setattr(pp, "_held_ohlc_outage", lambda held: bool(held))
        _, _, prod = _production(world)
        closed = pp._load_jsonl(prod / "closed_trades.jsonl")
        assert [t["ticker"] for t in closed].count("NVDA") == 1
        assert _S_D2 in _equity(prod / "equity_curve.jsonl")

    def test_no_positions_is_not_skipped(self, world, monkeypatch):
        """没有持仓 ⇒ 仓位基数就是现金，不跳（D1 全断也照开 NVDA，入场价来自快照）。"""
        monkeypatch.setattr(pp, "_LAST_RUN", None)
        _seed_live()
        world.fake.down = {"ALL"}
        res = pp.run_for_date(_S_D1, market_fear_greed=_FG)
        assert not res.get("skipped") and res["opened_today"] == 1
        assert "skipped" not in pp.run_stats()

    def test_replay_skips_too_but_only_logs_info(self, world, caplog):
        """重放（没有遮罩）遇到全断同样跳过——与生产同一规则；日志降为 INFO。"""
        with caplog.at_level(logging.INFO, logger="alpha_hive.paper_portfolio"):
            _, _, prod = _production(world, {_S_D2: {"ALL"}})
        assert _S_D2 not in _equity(prod / "equity_curve.jsonl")
        hits = [r for r in caplog.records if "整天跳过" in r.getMessage()]
        assert hits and all(r.levelno == logging.INFO for r in hits)

    def test_selfproof_with_skipped_production_day(self, world):
        """规则生效后的全断日：生产没写净值行 ⇒ 自证不重放那天（生产没处理），其余几天照常逐笔复现。"""
        seed, hist, prod = _production(world, {_S_D2: {"ALL"}})
        processed = set(_equity(prod / "equity_curve.jsonl"))
        assert _S_D2 not in processed
        plan = fwd._attach_ohlc_mask(fwd._anchor_plan(seed, [("测试历史", hist)], _S_DATES, processed=processed),
                                     _S_DATES)
        res = fwd.evaluate(_S_DATES, _S_SINCE, _S_BEFORE, world.tmp / "sb", insample=False, seed=seed, plan=plan)
        assert res["selfproof"]["reproduced"] == res["selfproof"]["real_entries"] > 0
        assert res["selfproof_segments"]["production_unprocessed_dates"] == [_S_D2]

    def test_alert_and_summary_line(self, tmp_path):
        from tests.test_ledger_wiring import _analyze
        import alert_manager as am
        st = {"as_of": _S_D2, "ok": True, "error": None, "skipped": "ohlc_unavailable", "ohlc_unavailable": ["NVDA"]}
        _, hits = _analyze(tmp_path, {"paper_portfolio": st})
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.MEDIUM and "整天跳过" in hits[0].message
        assert "纸面组合整天跳过" in scan_timing.summary_line({"phases": {}, "counters": {"paper_portfolio": st}})
        _, ok_hits = _analyze(tmp_path, {"paper_portfolio": {"as_of": _S_D2, "ok": True, "error": None}})
        assert ok_hits == []
