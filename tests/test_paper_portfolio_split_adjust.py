"""纸面组合拆股口径（v0.45.416，`paper_portfolio._reconcile_split_basis`）的守卫。

事故形状（生产没发生过，10-05 核过）：Yahoo 在除权日回溯复权全部历史日线，仓位的 entry / SL / TP 却是开仓当时
（复权前）的绝对价位，Step 1 每天重扫 (entry_date, as_of] ⇒ 拆股后第一次运行，多头入场次日假止损、空头假止盈，
出场日倒填；`_mark_to_market` 拿复权前入场价对复权后收盘。

这里守的腿：
  1. 多头 / 空头跨拆股：不出假单、仓位换到复权口径（shares × r，entry / SL / TP ÷ r，size_usd 不变）、只换一次；
     换完之后**真的**止损 / 止盈 / 时间止损照常、盈亏按复权口径算对；反向拆股同理；
  2. 拆股早于（或等于）入场日：不换、一次拆股查询都不打；
  3. 时点：除权日晚于 as_of 不换——时点日线下根本不去查；非时点日线（事后复权）下判不了、不换、看得见；
  4. 判不了（查询失败 / 比例对不上 / 缺入场日日线 / 大幅对不上却没有拆股记录）看得见：ERROR、返回值、净值行、卡片，且当天不碰仓位、
     按入场价估值，之后能自愈；日线对不上而 Yahoo 确认无拆股 ⇒ 照常（不冻仓）；
  5. `run_replay`（每个重放日看到 Yahoo 当天给的日线、拆股查询发生在重放时刻、能看到之后的拆股）
     与生产逐日 `run_for_date` 跨拆股**逐字节**相同；
  6. 没有拆股时落盘逐字节同改动前（不多写键）、不多打网络；
  7. F&G 自证键 `round(shares × entry_price, 2)` 跨调整不变。

全部离线：yfinance 换成从「连续价值序列 + 拆股表」现算的假模块——第 V 天的 Yahoo 给出的日线 = 价值 ÷（除权日 ≤ V 的
拆股比例积），与「除权日回溯复权全段历史」同形。夹具自证见 `TestWorldIsNotVacuous`（拆股逻辑关掉 ⇒ 假单真的出现）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import sqlite3
import sys
import types

import pytest

import paper_portfolio as pp

ENTRY = "2026-08-12"
EX = "2026-08-19"
FINAL = "2026-09-04"


def _bdays(start, end):
    d, e, out = dt.date.fromisoformat(start), dt.date.fromisoformat(end), []
    while d < e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


ALL_DAYS = _bdays("2026-08-03", "2026-09-12")
RUN_DAYS = _bdays("2026-08-13", FINAL)          # 生产逐日运行的日子（种子 last_run_date = 08-12）


def _values():
    """{ticker: {date: 连续价值}}——「市场」本身，与报价口径无关。"""
    def drift(base, step):
        return {d: base * (1 + step * i) for i, d in enumerate(ALL_DAYS)}
    v = {
        "LONGX": drift(100.0, 0.0005),     # 多头，1 拆 2 @EX；入场后一直在 SL/TP 之间 ⇒ 真实结局是 TIME
        "SHORTX": drift(100.0, 0.0),       # 空头，1 拆 2 @EX；08-25 价值涨到 108 ⇒ 真实 SL（复权口径 54 ≥ 53.5）
        "REVX": drift(10.0, 0.0),          # 多头，10 并 1（0.1）@EX
        "TPX": drift(100.0, 0.0),          # 多头，1 拆 2 @EX；08-24 价值 116 ⇒ 真实 TP（复权口径 58 ≥ 57.5）
        "PREX": drift(100.0, 0.0),         # 拆股 08-14，08-17 才入场（拆股后的价）
        "EQX": drift(100.0, 0.0),          # 拆股就在入场日 08-17
        "NEWA": drift(40.0, 0.001),        # 无拆股，08-13 开仓
        "NEWB": drift(60.0, -0.0005),      # 无拆股，08-20 开仓
        "PLAIN": drift(80.0, 0.0002),      # 无拆股种子持仓
    }
    v["SHORTX"]["2026-08-25"] = 108.0
    v["TPX"]["2026-08-24"] = 116.0
    return v


SPLITS = {"LONGX": [(EX, 2.0)], "SHORTX": [(EX, 2.0)], "REVX": [(EX, 0.1)], "TPX": [(EX, 2.0)],
          "PREX": [("2026-08-14", 2.0)], "EQX": [("2026-08-17", 2.0)]}


class SplitYF:
    """yfinance 替身。`bars_day`：日线请求（带 end）看到的「Yahoo 当天」；`now_day`：拆股查询（不带 end）看到的那天。

    第 V 天 Yahoo 给的 d ≤ V 的日线 = 价值 ÷ ∏{除权日 ≤ V 的比例}；「Stock Splits」列在除权日那行（同真 yfinance，
    日线响应也带这一列——证明日线路径不靠它）。`fail[ticker]` ∈ {"raise", "empty", "nocol"} 只作用于拆股查询。
    """

    def __init__(self, values, splits):
        self.values, self.splits = values, splits
        self.bars_day = self.now_day = FINAL
        self.fail = {}
        self.unlisted = set()     # 这些标的：日线照常复权，但「Stock Splits」列里没有这次拆股（Yahoo 记录没跟上）
        self.calls = []

    @property
    def lookups(self):
        return [c for c in self.calls if "end" not in c[1]]

    def module(self):
        return types.SimpleNamespace(Ticker=self._ticker)

    def _ticker(self, t):
        fake = self

        class _T:
            def history(self, **kw):
                import pandas as pd
                fake.calls.append((t, dict(kw)))
                is_lookup = "end" not in kw
                if is_lookup:
                    how = fake.fail.get(t)
                    if how == "raise":
                        raise ConnectionError(f"fake outage {t}")
                    if how == "empty":
                        return pd.DataFrame()
                view = fake.now_day if is_lookup else fake.bars_day
                start = pd.Timestamp(kw["start"]).strftime("%Y-%m-%d")
                end = pd.Timestamp(kw["end"]).strftime("%Y-%m-%d") if not is_lookup else "9999-12-31"
                sp = [(x, r) for x, r in fake.splits.get(t, []) if x <= view]
                rows = []
                for d, v in sorted(fake.values.get(t, {}).items()):
                    if start <= d < end and d <= view:
                        c = v / math.prod(r for _, r in sp)
                        s = 0.0 if t in fake.unlisted else next((r for x, r in sp if x == d), 0.0)
                        rows.append((d, [c, c * 1.003, c * 0.997, c, 0.0, s]))
                if not rows:
                    return pd.DataFrame()
                cols = ["Open", "High", "Low", "Close", "Dividends", "Stock Splits"]
                if is_lookup and fake.fail.get(t) == "nocol":
                    cols[-1] = "Something Else"
                return pd.DataFrame([r for _, r in rows], columns=cols, index=pd.to_datetime([d for d, _ in rows]))
        return _T()


def _pos(ticker, entry_date, entry_price, direction="bullish", size_usd=5000.0):
    bull = direction == "bullish"
    return {"ticker": ticker, "direction": direction, "entry_date": entry_date, "entry_price": entry_price,
            "sl_price": round(entry_price * (0.93 if bull else 1.07), 4),
            "tp_price": round(entry_price * (1.15 if bull else 0.85), 4),
            "shares": round(size_usd / entry_price, 4), "size_usd": size_usd,
            "time_stop_date": (dt.date.fromisoformat(entry_date) + dt.timedelta(days=14)).isoformat(),
            "confidence": "high", "score": 7.5, "rationale": "seed", "sizing": "tier"}


def _seed_positions(values, tickers=("LONGX", "SHORTX", "REVX", "TPX", "PLAIN")):
    out = []
    for t in tickers:
        out.append(_pos(t, ENTRY, values[t][ENTRY], "bearish" if t == "SHORTX" else "bullish"))
    return out


def _write_seed(state_dir, positions, cash=25000.0):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "meta.json").write_text(json.dumps({
        "version": "test", "starting_capital": 50000.0, "starting_date": "2026-03-09",
        "cash": cash, "last_run_date": ENTRY, "config_snapshot": {}}), encoding="utf-8")
    (state_dir / "positions.jsonl").write_text("".join(json.dumps(p) + "\n" for p in positions), encoding="utf-8")
    (state_dir / "closed_trades.jsonl").write_text("", encoding="utf-8")


def _snapshot(snap_dir, ticker, date, values, score=8.0, direction="bullish"):
    row = {"ticker": ticker, "date": date, "composite_score": score, "direction": direction,
           "agent_votes": {"a": score, "b": score}, "entry_price": values[ticker][date]}
    (snap_dir / f"{ticker}_{date}.json").write_text(json.dumps(row), encoding="utf-8")


@pytest.fixture
def world(monkeypatch, tmp_path):
    values = _values()
    fake = SplitYF(values, SPLITS)
    monkeypatch.setitem(sys.modules, "yfinance", fake.module())
    snap = tmp_path / "snapshots"
    snap.mkdir()
    _snapshot(snap, "NEWA", "2026-08-13", values)
    _snapshot(snap, "NEWB", "2026-08-20", values)
    db = tmp_path / "pheromone.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL)")
    con.commit()
    con.close()
    monkeypatch.setattr(pp, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(pp, "_pheromone_db_path", lambda: db)
    monkeypatch.setattr(pp, "_PRICE_CACHE", {})
    monkeypatch.setattr(pp, "_OHLC_FULL", {})
    monkeypatch.setattr(pp, "_SPLIT_EVENTS_CACHE", {})
    monkeypatch.setitem(pp.CONFIG, "sizing_mode", "tier")
    return types.SimpleNamespace(values=values, fake=fake, tmp=tmp_path, monkeypatch=monkeypatch, snap=snap)


def _live(w, days=RUN_DAYS, seed=None, on_day=None):
    """生产：每天一个新进程（缓存清空），Yahoo 的日线与拆股记录都是「那一天」的。状态写在 `pp.STATE_DIR`
    （conftest 的沙箱）。`on_day(d)`：每天运行前调用（造某几天查询失败）。"""
    _write_seed(pp.STATE_DIR, seed if seed is not None else _seed_positions(w.values))
    results = {}
    for d in days:
        pp._PRICE_CACHE.clear()
        pp._SPLIT_EVENTS_CACHE.clear()
        w.fake.bars_day = w.fake.now_day = d
        if on_day:
            on_day(d)
        results[d] = pp.run_for_date(d)
    return results


def _state(state_dir):
    return {n: (state_dir / n).read_bytes() for n in
            ("positions.jsonl", "closed_trades.jsonl", "equity_curve.jsonl", "meta.json")}


def _rows(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _by_ticker(rows):
    return {r["ticker"]: r for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# 0. 夹具自证：拆股逻辑关掉 ⇒ 这个世界真的造出假单（否则下面的「没有假单」什么都证明不了）
# ══════════════════════════════════════════════════════════════════════════════

class TestWorldIsNotVacuous:
    def test_without_the_fix_the_split_fakes_an_sl_and_a_tp_backdated_to_entry_plus_one(self, world):
        """把 `_reconcile_split_basis` 换成恒 ok（= 改动前）：多头假止损、空头假止盈、反向拆股多头假止盈，
        出场日都倒填到入场后第一个交易日。变异「删掉 Step 1 里的口径核对」就是这个世界。"""
        world.monkeypatch.setattr(pp, "_reconcile_split_basis", lambda *a, **k: {"status": "ok"})
        _live(world, days=_bdays("2026-08-13", "2026-08-20"))
        closed = _by_ticker(_rows(pp.CLOSED_FILE))
        assert closed["LONGX"]["exit_reason"] == "SL" and closed["LONGX"]["exit_date"] == "2026-08-13"
        assert closed["SHORTX"]["exit_reason"] == "TP" and closed["SHORTX"]["exit_date"] == "2026-08-13"
        assert closed["REVX"]["exit_reason"] == "TP" and closed["REVX"]["exit_date"] == "2026-08-13"
        assert closed["LONGX"]["gross_return_pct"] == pytest.approx(-7.0)
        assert closed["SHORTX"]["gross_return_pct"] == pytest.approx(15.0)

    def test_before_the_ex_date_nothing_trips(self, world):
        """拆股之前（08-13~08-18）这个世界里没有任何出场——假单只能来自拆股。"""
        _live(world, days=_bdays("2026-08-13", EX))
        assert _rows(pp.CLOSED_FILE) == []


# ══════════════════════════════════════════════════════════════════════════════
# 1. 多头 / 空头 / 反向拆股跨除权日
# ══════════════════════════════════════════════════════════════════════════════

class TestAcrossTheSplit:
    def test_long_is_rescaled_once_and_not_stopped_out(self, world):
        seed = _seed_positions(world.values)
        orig = _by_ticker(seed)["LONGX"]
        res = _live(world, days=_bdays("2026-08-13", "2026-08-25"))
        pos = _by_ticker(_rows(pp.POSITIONS_FILE))["LONGX"]
        assert pos["shares"] == pytest.approx(orig["shares"] * 2)
        assert pos["entry_price"] == pytest.approx(orig["entry_price"] / 2)
        assert pos["sl_price"] == pytest.approx(orig["sl_price"] / 2, abs=1e-4)
        assert pos["tp_price"] == pytest.approx(orig["tp_price"] / 2, abs=1e-4)
        assert pos["size_usd"] == orig["size_usd"]
        assert pos["split_adjustments"] == [{"ex_date": EX, "ratio": 2.0, "applied_as_of": EX}]   # 只换一次
        assert "LONGX" not in _by_ticker(_rows(pp.CLOSED_FILE))
        assert {"ticker": "LONGX", "entry_date": ENTRY, "ex_date": EX, "ratio": 2.0} in res[EX]["split_adjusted"]
        assert all(not r["split_adjusted"] for d, r in res.items() if d != EX)

    def test_short_is_rescaled_and_not_taken_profit(self, world):
        _live(world, days=_bdays("2026-08-13", "2026-08-26"))
        pos = _by_ticker(_rows(pp.POSITIONS_FILE)).get("SHORTX")
        closed = _by_ticker(_rows(pp.CLOSED_FILE)).get("SHORTX")
        # 08-25 价值涨到 108 ⇒ 复权口径 54 ≥ 53.5 ⇒ 08-25 当天的**真**止损（−7%），不是 08-13 的假止盈
        assert pos is None and closed["exit_reason"] == "SL" and closed["exit_date"] == "2026-08-25"
        assert closed["entry_price"] == pytest.approx(50.0)
        assert closed["exit_price"] == pytest.approx(53.5)
        assert closed["gross_return_pct"] == pytest.approx(-7.0)
        assert closed["split_adjustments"] == [{"ex_date": EX, "ratio": 2.0, "applied_as_of": EX}]

    def test_real_tp_after_the_split_is_booked_on_the_adjusted_basis(self, world):
        _live(world, days=_bdays("2026-08-13", "2026-08-26"))
        t = _by_ticker(_rows(pp.CLOSED_FILE))["TPX"]
        assert (t["exit_reason"], t["exit_date"]) == ("TP", "2026-08-24")
        assert t["gross_return_pct"] == pytest.approx(15.0)
        # 平仓市值 = 初始建仓市值 × (1 + 毛收益)：口径换了，钱没变
        assert t["shares"] * t["exit_price"] == pytest.approx(5000.0 * 1.15, rel=1e-6)

    def test_time_exit_after_the_split_has_a_sane_return(self, world):
        _live(world)
        t = _by_ticker(_rows(pp.CLOSED_FILE))["LONGX"]
        assert t["exit_reason"] == "TIME" and t["exit_date"] == "2026-08-26"
        v = world.values["LONGX"]
        assert t["gross_return_pct"] == pytest.approx((v["2026-08-26"] / v[ENTRY] - 1) * 100, abs=1e-4)

    def test_reverse_split_is_rescaled_and_not_taken_profit(self, world):
        _live(world, days=_bdays("2026-08-13", "2026-08-25"))
        pos = _by_ticker(_rows(pp.POSITIONS_FILE))["REVX"]
        assert pos["entry_price"] == pytest.approx(100.0) and pos["shares"] == pytest.approx(50.0)
        assert pos["split_adjustments"][0]["ratio"] == 0.1

    def test_nav_has_no_split_cliff(self, world):
        """改动前：多头仓位估值凭空 −50%。现在除权日前后净值只差真实价格变动。"""
        res = _live(world, days=_bdays("2026-08-13", "2026-08-21"))
        nav_before, nav_ex = res["2026-08-18"]["nav"], res[EX]["nav"]
        assert abs(nav_ex - nav_before) < 50.0

    def test_rerunning_the_same_day_does_not_rescale_twice(self, world):
        _live(world, days=_bdays("2026-08-13", "2026-08-21"))
        before = _by_ticker(_rows(pp.POSITIONS_FILE))["LONGX"]
        world.fake.bars_day = world.fake.now_day = "2026-08-20"
        pp._PRICE_CACHE.clear()
        pp._SPLIT_EVENTS_CACHE.clear()
        pp.run_for_date("2026-08-20")
        after = _by_ticker(_rows(pp.POSITIONS_FILE))["LONGX"]
        assert after == before

    def test_entry_key_is_invariant_under_the_rescale(self, world):
        """F&G 前瞻检验的自证键 `round(shares × entry_price, 2)`：换口径前后同一个数（shares / entry 不取整）。"""
        for r in (2.0, 1.5, 3.0 / 2.0, 0.1, 4.0):
            p = pp.Position(**_pos("X", ENTRY, 33.61000061035156, size_usd=750.0))
            k0 = round(p.shares * p.entry_price, 2)
            pp._apply_split(p, EX, r, EX)
            assert round(p.shares * p.entry_price, 2) == k0


# ══════════════════════════════════════════════════════════════════════════════
# 2. 拆股早于 / 等于入场日
# ══════════════════════════════════════════════════════════════════════════════

class TestSplitBeforeEntry:
    @pytest.mark.parametrize("ticker", ["PREX", "EQX"])
    def test_no_adjustment_and_no_lookup(self, world, ticker):
        """入场价本来就是拆股后的价（除权日 08-14 < 入场 08-17；或除权日 = 入场日）：不换、一次拆股查询都不打。"""
        seed = [_pos(ticker, "2026-08-17", world.values[ticker]["2026-08-17"] / 2)]
        _write_seed(pp.STATE_DIR, seed)
        meta = json.loads(pp.META_FILE.read_text())
        meta["last_run_date"] = "2026-08-17"
        pp.META_FILE.write_text(json.dumps(meta))
        for d in _bdays("2026-08-18", "2026-08-26"):
            world.fake.bars_day = world.fake.now_day = d
            pp._SPLIT_EVENTS_CACHE.clear()
            pp.run_for_date(d)
        pos = _rows(pp.POSITIONS_FILE)[0]
        assert pos["entry_price"] == seed[0]["entry_price"] and "split_adjustments" not in pos
        assert world.fake.lookups == []

    def test_ex_date_equal_to_entry_is_not_pending(self, monkeypatch):
        """单元：即便查询被触发（入场价与入场日收盘差 6%），除权日 = 入场日的拆股也不在 (entry_date, as_of] 里 ⇒
        不换、照常。变异「`entry_date < x` 改 `<=`」⇒ 它成了待换拆股、比例对不上 ⇒ 判不了 ⇒ 红。"""
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([(ENTRY, 2.0)], ""))
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        out = pp._reconcile_split_basis(p, "2026-08-14", {ENTRY: {"Close": 94.0}, "2026-08-13": {"Close": 94.0}})
        assert out["status"] == "ok" and p.entry_price == 100.0 and p.split_adjustments == []


# ══════════════════════════════════════════════════════════════════════════════
# 3. 时点：只换除权日 ≤ as_of 的拆股
# ══════════════════════════════════════════════════════════════════════════════

class TestPointInTime:
    def test_point_in_time_bars_before_the_ex_date_never_trigger_a_lookup(self, world):
        """时点日线（Yahoo 当天还没复权）⇒ 判据不触发 ⇒ 除权日之前一次拆股查询都不打，自然不会换。"""
        _live(world, days=_bdays("2026-08-13", EX))
        assert world.fake.lookups == []
        assert all("split_adjustments" not in p for p in _rows(pp.POSITIONS_FILE))

    def test_applied_exactly_on_the_ex_date(self, world):
        _live(world, days=_bdays("2026-08-13", "2026-08-20"))
        assert _by_ticker(_rows(pp.POSITIONS_FILE))["LONGX"]["split_adjustments"][0]["applied_as_of"] == EX

    def test_lookup_sees_a_future_split_but_it_is_not_applied(self, monkeypatch):
        """单元：查询返回 as_of 之后的拆股（回放 / 事后查询的形状），日线是时点的 ⇒ 判据根本不触发；
        日线是事后复权的 ⇒ `future_split` 判不了、不换。变异「去掉 `x <= as_of`」⇒ 这里换了、红。"""
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([("2026-08-20", 2.0)], ""))
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        pit = {ENTRY: {"Close": 100.0}, "2026-08-13": {"Close": 100.2}}
        assert pp._reconcile_split_basis(p, "2026-08-13", pit) == {"status": "ok"}
        adjusted_later = {ENTRY: {"Close": 50.0}, "2026-08-13": {"Close": 50.1}}
        out = pp._reconcile_split_basis(p, "2026-08-13", adjusted_later)
        assert out["status"] == "unresolved" and out["reason"] == "future_split"
        assert p.entry_price == 100.0 and p.split_adjustments == []

    def test_non_pit_bars_hold_the_position_visibly_instead_of_faking_a_trade(self, world, caplog):
        """非时点回放（日线是今天下载的、已为之后的拆股复权）：改动前假止损；现在当天不碰、看得见。"""
        world.fake.bars_day = world.fake.now_day = FINAL
        _write_seed(pp.STATE_DIR, _seed_positions(world.values, ("LONGX",)))
        with caplog.at_level(logging.ERROR, logger="alpha_hive.paper_portfolio"):
            res = pp.run_for_date("2026-08-13")
        assert _rows(pp.CLOSED_FILE) == []
        assert res["split_unresolved"][0]["reason"] == "future_split"
        assert _rows(pp.EQUITY_FILE)[-1]["split_unresolved"] == ["LONGX"]
        assert any("future_split" in r.getMessage() for r in caplog.records)


# ══════════════════════════════════════════════════════════════════════════════
# 4. 判不了要看得见；之后能自愈；无拆股的口径不一致不冻仓
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectionFailureIsVisible:
    @pytest.mark.parametrize("how", ["raise", "empty", "nocol"])
    def test_lookup_failure_holds_marks_at_entry_and_is_reported(self, world, caplog, how):
        """拆股查询失败（抛 / 空结果 / 缺列——返回与抛两条路径各堵一次）：当天不查出场（没有假单）、不换口径、
        按入场价估值；ERROR + 返回值 + 净值行 + 卡片四处看得见。变异「判不了照常查出场」⇒ 假止损、红。"""
        _live(world, days=_bdays("2026-08-13", EX))
        world.fake.fail["LONGX"] = how
        world.fake.bars_day = world.fake.now_day = EX
        pp._SPLIT_EVENTS_CACHE.clear()
        with caplog.at_level(logging.ERROR, logger="alpha_hive.paper_portfolio"):
            res = pp.run_for_date(EX)
        assert "LONGX" not in _by_ticker(_rows(pp.CLOSED_FILE))
        pos = _by_ticker(_rows(pp.POSITIONS_FILE))["LONGX"]
        assert pos["entry_price"] == pytest.approx(world.values["LONGX"][ENTRY]) and "split_adjustments" not in pos
        u = [x for x in res["split_unresolved"] if x["ticker"] == "LONGX"]
        assert u and u[0]["reason"] == "lookup_failed"
        detail = _by_ticker(res["positions"])["LONGX"]
        assert detail["unreal_usd"] == 0.0 and detail["split_unresolved"] == "lookup_failed"
        assert _rows(pp.EQUITY_FILE)[-1]["split_unresolved"] == ["LONGX"]
        assert any(r.levelno == logging.ERROR and "LONGX" in r.getMessage() for r in caplog.records)
        html = pp.render_portfolio_card()
        assert "口径待核" in html and "LONGX 拆股口径待核" in html

    def test_failure_heals_next_day_and_the_ledger_matches_a_clean_run(self, world, tmp_path):
        """查询失败的那天只是推迟：次日成功即换口径，之后的出场与从没失败过逐笔相同（`_check_exit` 每天重扫全段）。"""
        clean = _live(world)
        clean_closed = pp.CLOSED_FILE.read_bytes()
        world.monkeypatch.setattr(pp, "POSITIONS_FILE", tmp_path / "f" / "positions.jsonl")
        world.monkeypatch.setattr(pp, "CLOSED_FILE", tmp_path / "f" / "closed_trades.jsonl")
        world.monkeypatch.setattr(pp, "EQUITY_FILE", tmp_path / "f" / "equity_curve.jsonl")
        world.monkeypatch.setattr(pp, "META_FILE", tmp_path / "f" / "meta.json")
        world.monkeypatch.setattr(pp, "STATE_DIR", tmp_path / "f")

        def flaky(d):
            if d in (EX, "2026-08-20"):
                world.fake.fail["LONGX"] = "raise"
            else:
                world.fake.fail.pop("LONGX", None)
        res = _live(world, on_day=flaky)
        assert res[EX]["split_unresolved"] and res["2026-08-20"]["split_unresolved"]
        assert res["2026-08-21"]["split_adjusted"] and not res["2026-08-21"]["split_unresolved"]
        healed = _rows(pp.CLOSED_FILE)
        assert _by_ticker(healed)["LONGX"]["split_adjustments"][0]["applied_as_of"] == "2026-08-21"

        def _strip(rows):   # 唯一的差别该是留痕里的「哪天换的」
            return [{**r, "split_adjustments": [{k: v for k, v in a.items() if k != "applied_as_of"}
                                                for a in r.get("split_adjustments", [])]} for r in rows]
        assert _strip(healed) == _strip([json.loads(x) for x in clean_closed.decode().splitlines()])
        assert clean[RUN_DAYS[-1]]["nav"] == pytest.approx(res[RUN_DAYS[-1]]["nav"])

    def test_ratio_mismatch_is_unresolved(self, monkeypatch):
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([("2026-08-13", 3.0)], ""))
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        out = pp._reconcile_split_basis(p, "2026-08-13", {ENTRY: {"Close": 50.0}, "2026-08-13": {"Close": 50.0}})
        assert out["status"] == "unresolved" and out["reason"] == "ratio_mismatch" and p.split_adjustments == []

    def test_missing_entry_bar_with_a_pending_split_is_unresolved(self, monkeypatch):
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([("2026-08-13", 2.0)], ""))
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        out = pp._reconcile_split_basis(p, "2026-08-14", {"2026-08-13": {"Close": 50.0}})
        assert out["status"] == "unresolved" and out["reason"] == "no_entry_bar"

    def test_missing_entry_bar_without_split_or_with_failed_lookup_proceeds(self, monkeypatch, caplog):
        """缺入场日那根只是弱证据：Yahoo 确认没有拆股 ⇒ 照常；查询失败 ⇒ 照常 + WARNING（不因一次取数失败冻仓）。"""
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([], ""))
        assert pp._reconcile_split_basis(p, "2026-08-14", {"2026-08-13": {"Close": 101.0}}) == {"status": "ok"}
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: (None, "ConnectionError: x"))
        with caplog.at_level(logging.WARNING, logger="alpha_hive.paper_portfolio"):
            assert pp._reconcile_split_basis(p, "2026-08-14", {"2026-08-13": {"Close": 101.0}}) == {"status": "ok"}
        assert any("拆股记录也取不到" in r.getMessage() for r in caplog.records)

    def test_mismatch_without_any_split_proceeds_with_a_warning(self, monkeypatch, caplog):
        """入场价与入场日收盘差 6%（陈旧报价）、Yahoo 确认无拆股 ⇒ 不是口径问题，照常（否则这个仓位永远冻住）。"""
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([], ""))
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        with caplog.at_level(logging.WARNING, logger="alpha_hive.paper_portfolio"):
            out = pp._reconcile_split_basis(p, "2026-08-14", {ENTRY: {"Close": 94.0}})
        assert out == {"status": "ok"} and p.entry_price == 100.0
        assert any("不是口径问题" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("close,status", [(80.5, "ok"), (79.9, "unresolved"), (124.0, "ok"),
                                              (125.1, "unresolved"), (50.0, "unresolved"), (1000.0, "unresolved")])
    def test_large_mismatch_without_any_split_is_unexplained(self, monkeypatch, close, status):
        """对不上 ≥ 25%、Yahoo 没有能解释它的拆股 ⇒ `unexplained`（判不了）；不到 25% ⇒ 照常。两侧对称（对数）。
        变异「删 unexplained 分支」⇒ 1 拆 2 那组照常 ⇒ 红。"""
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([], ""))
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        out = pp._reconcile_split_basis(p, "2026-08-14", {ENTRY: {"Close": close}})
        assert out["status"] == status and p.entry_price == 100.0
        if status == "unresolved":
            assert out["reason"] == "unexplained"

    def test_bars_adjusted_but_split_not_listed_holds_instead_of_faking(self, world):
        """端到端：Yahoo 已为除权日复权日线、拆股记录却还没有这次拆股 ⇒ 改动前（以及「对不上就照常」）是假止损；
        现在当天不碰、`unexplained` 看得见；记录跟上那天换口径，出场与从没出过岔子逐笔相同。"""
        world.fake.unlisted.add("LONGX")

        def catch_up(d):
            if d >= "2026-08-21":
                world.fake.unlisted.discard("LONGX")
        res = _live(world, days=_bdays("2026-08-13", "2026-08-25"), on_day=catch_up)
        assert [u["reason"] for u in res[EX]["split_unresolved"] if u["ticker"] == "LONGX"] == ["unexplained"]
        assert "LONGX" not in _by_ticker(_rows(pp.CLOSED_FILE))
        pos = _by_ticker(_rows(pp.POSITIONS_FILE))["LONGX"]
        assert pos["split_adjustments"] == [{"ex_date": EX, "ratio": 2.0, "applied_as_of": "2026-08-21"}]

    def test_small_noise_inside_tolerance_never_looks_up(self, monkeypatch):
        def boom(t, s):
            raise AssertionError("容差内不该查拆股")
        monkeypatch.setattr(pp, "_fetch_split_events", boom)
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        for c in (100.0, 99.32, 100.68, 97.2, 102.9):      # 账本实测最大 0.68%；容差 3%
            assert pp._reconcile_split_basis(p, "2026-08-14", {ENTRY: {"Close": c}}) == {"status": "ok"}

    def test_already_applied_ex_date_is_not_applied_again(self, monkeypatch):
        """同一除权日只换一次：即便之后日线又和入场价对不上（再来一次 2 倍），已换过的拆股不再计入——
        没有别的拆股能解释 ⇒ `unexplained`，而不是把同一次拆股再换一遍。"""
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([("2026-08-13", 2.0)], ""))
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        assert pp._reconcile_split_basis(p, "2026-08-13", {ENTRY: {"Close": 50.0}})["status"] == "applied"
        out = pp._reconcile_split_basis(p, "2026-08-14", {ENTRY: {"Close": 25.0}})
        assert (out["status"], out["reason"]) == ("unresolved", "unexplained")
        assert len(p.split_adjustments) == 1 and p.entry_price == 50.0

    def test_two_pending_splits_are_applied_together(self, monkeypatch):
        monkeypatch.setattr(pp, "_fetch_split_events", lambda t, s: ([("2026-08-13", 2.0), ("2026-08-14", 3.0)], ""))
        p = pp.Position(**_pos("X", ENTRY, 120.0))
        out = pp._reconcile_split_basis(p, "2026-08-14", {ENTRY: {"Close": 20.0}})
        assert out["status"] == "applied" and p.entry_price == pytest.approx(20.0)
        assert [a["ex_date"] for a in p.split_adjustments] == ["2026-08-13", "2026-08-14"]


class TestLookupCache:
    def test_success_is_cached_failure_is_retried(self, world):
        world.fake.now_day = FINAL
        assert pp._fetch_split_events("LONGX", ENTRY) == ([(EX, 2.0)], "")
        assert pp._fetch_split_events("LONGX", ENTRY) == ([(EX, 2.0)], "")
        assert len(world.fake.lookups) == 1
        world.fake.fail["SHORTX"] = "raise"
        assert pp._fetch_split_events("SHORTX", ENTRY)[0] is None
        world.fake.fail.pop("SHORTX")
        assert pp._fetch_split_events("SHORTX", ENTRY) == ([(EX, 2.0)], "")
        assert len(world.fake.lookups) == 3

    def test_no_splits_is_an_empty_list_not_a_failure(self, world):
        assert pp._fetch_split_events("PLAIN", ENTRY) == ([], "")


# ══════════════════════════════════════════════════════════════════════════════
# 5. run_replay ≡ 生产逐日 run_for_date（跨拆股，时点日线）
# ══════════════════════════════════════════════════════════════════════════════

class TestReplayEqualsLive:
    def test_replay_is_byte_identical_to_day_by_day_production(self, world, tmp_path):
        """生产：每天新进程、Yahoo 的日线和拆股记录都是那一天的。回放：每个重放日看到 Yahoo 那天给的日线
        （= v0.45.415 时点行情库的承诺），拆股查询发生在**重放时刻**（看得到之后所有拆股）、缓存跨重放日。
        四个状态文件逐字节相同。变异「去掉 `x <= as_of`」不会让本条红（时点日线下判据不触发）——那一条由
        `TestPointInTime` 守；本条守的是「判据只看喂进来的日线 ⇒ 两边同一天做同一个决定」。"""
        _live(world)
        live = _state(pp.STATE_DIR)
        assert b"split_adjustments" in live["positions.jsonl"] + live["closed_trades.jsonl"]   # 真的跨了拆股

        rdir = tmp_path / "replay"
        _write_seed(rdir, _seed_positions(world.values))
        pp._PRICE_CACHE.clear()
        pp._SPLIT_EVENTS_CACHE.clear()
        world.fake.now_day = FINAL
        orig = pp.run_for_date

        def pit_run_for_date(d, **kw):
            # 每个重放日的日线是「Yahoo 那天给的」：直连缓存不许跨重放日——Step 2 在 d 取的 [d, d+3) 与 Step 1 在
            # 次日取的 [entry=d, d+1+2) 是同一个键，而假 Yahoo 只给到「那天」为止。真回放里行情窗口 / 行情库按重放日切片、
            # 不经 `_PRICE_CACHE`，真 Yahoo 也总是给到取数当时为止，两者都没有这个混叠；这里清掉是为了让替身忠实于前者。
            pp._PRICE_CACHE.clear()
            world.fake.bars_day = d
            return orig(d, **kw)
        world.monkeypatch.setattr(pp, "run_for_date", pit_run_for_date)
        n0 = len(world.fake.lookups)
        pp.run_replay({}, rdir, dates=RUN_DAYS)
        assert _state(rdir) == live
        # 回放里拆股查询每个 (标的, 入场日) 至多一次（缓存跨重放日）
        looked = [(c[0], c[1]["start"]) for c in world.fake.lookups[n0:]]
        assert len(looked) == len(set(looked)) <= 4

    def test_replay_with_seed_already_adjusted_matches(self, world, tmp_path):
        """逐日重锚：从生产「已换过口径」的状态起跑一天，结果与生产那天相同（不再换第二次）。"""
        days = _bdays("2026-08-13", "2026-08-25")
        _live(world, days=days[:-1])
        anchor = {n: (pp.STATE_DIR / n).read_bytes() for n in ("positions.jsonl", "closed_trades.jsonl", "meta.json")}
        world.fake.bars_day = world.fake.now_day = days[-1]
        pp._PRICE_CACHE.clear()
        pp._SPLIT_EVENTS_CACHE.clear()
        pp.run_for_date(days[-1])
        live = {n: (pp.STATE_DIR / n).read_bytes() for n in ("positions.jsonl", "closed_trades.jsonl")}
        rdir = tmp_path / "seg"
        rdir.mkdir()
        for n, b in anchor.items():
            (rdir / n).write_bytes(b)
        pp._PRICE_CACHE.clear()
        pp.run_replay({}, rdir, dates=[days[-1]])
        assert {n: (rdir / n).read_bytes() for n in live} == live


# ══════════════════════════════════════════════════════════════════════════════
# 6. 没有拆股：落盘逐字节同改动前、不多打网络
# ══════════════════════════════════════════════════════════════════════════════

class TestNoSplitIsUnchanged:
    def test_no_new_keys_and_no_lookups(self, world):
        world.fake.splits = {}
        _live(world)
        for f in (pp.POSITIONS_FILE, pp.CLOSED_FILE):
            assert b"split_adjustments" not in f.read_bytes()
        assert b"split_unresolved" not in pp.EQUITY_FILE.read_bytes()
        assert world.fake.lookups == []
        assert _rows(pp.CLOSED_FILE)      # 有平仓、有开仓——不是空跑

    def test_to_dict_matches_the_pre_change_shape(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pp, "_pheromone_db_path", lambda: tmp_path / "bo.db")   # _close_position 回写屏障结果
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        assert p.to_dict() == _pos("X", ENTRY, 100.0)
        t, _ = pp._close_position(p, "TIME", 101.0, "2026-08-26")
        assert "split_adjustments" not in t.to_dict()

    def test_old_rows_and_adjusted_rows_both_load(self):
        p = pp.Position(**_pos("X", ENTRY, 100.0))
        pp._apply_split(p, EX, 2.0, EX)
        again = pp.Position(**json.loads(json.dumps(p.to_dict())))
        assert again == p

