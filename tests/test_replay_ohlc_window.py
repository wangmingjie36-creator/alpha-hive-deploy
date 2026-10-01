"""回放 OHLC 窗口（v0.45.391，`paper_portfolio.replay_ohlc_window`）的守卫。

事故：F&G 敞口门前瞻检验每天把 [FORWARD_START, 今天) 整段重放两遍，`run_for_date` 对每个在场仓位
按 (ticker, entry_date, as_of+2) 取一次 OHLC——键里带 as_of，逐日都是新键。窗口 8 个快照日即
124 次 yfinance 调用，Step 11 在 09-28 / 09-29 / 09-30 用了 37s / 47s / >60s（被 60s 超时杀掉）。
修法：同一次检验里每个标的整段只取一次，之后按区间切片。

这里守的是「**只改了取数次数，没改任何一个数**」这件事的每一条腿：
  1. 逐根 NaN / inf / 非数值过滤与直连路径同一份代码（`prefetch_ohlc` 就是漏了它的反例）；
  2. 窗口外的请求走原直连路径，**不截断**；
  3. 整段取数抛异常 / 返回空 ⇒ 该标的退回原直连路径（结果不变）并记 WARNING——返回与抛两条路径各一条；
  4. 作用域：`run()` 返回后、以及中途抛异常后，窗口都已撤掉；`_PRICE_CACHE` / `_OHLC_FULL` 不被碰；
  5. 每个标的每次检验至多一次整段取数，且零窗口外请求（调用点前视被放宽而窗口没跟上 ⇒ 这里红）；
     窗口内的空切片不再打网络（复审 N3）；
  5b.（复审 S1 / S3）降级要有机读出口：整段取数退回直连时 `run()` 结果的 `ohlc_window`、进度行与 attention
     都看得见，而判定 / 统计不变；行情全断时自证失败的原因说「OHLC 不可得」，不推给评分链；
  6. 合成数据上，开窗口与不开窗口时 `evaluate()` / `rehearse()` 的返回值与 A/B 沙箱状态文件逐字节相同
     ——世界里有 SL / TP / TIME 出场、TIME 止损当天 NaN、as_of 当天缺 bar（mark-to-market 前视）、
     快照无 entry_price、极端 F&G 日 B 开出与 A 不同的标的（夹具自证见 `TestWorldIsNotVacuous`）。

全部离线：yfinance 换成从一张主表切片的假模块（「窄区间一次取」≡「宽区间取了再切片」按构造成立），
真 Yahoo 上这条前提由 `experiments/replay_ohlc_window_premise.py` 核（显式命令，不是 skip）。
每条测试的 docstring 写明「哪个变异会让它红」。
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import json
import logging
import math
import re
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest

import ic_rerun_readiness as rr   # 只给「降级看得见」那一节核对 attention 条目（复审 S1）
import paper_portfolio as pp

_ROOT = Path(__file__).resolve().parent.parent


def _load_fwd():
    spec = importlib.util.spec_from_file_location(
        "fg_exposure_gate_forward_test_rw", _ROOT / "experiments" / "fg_exposure_gate_forward_test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fwd = _load_fwd()

_NAN = float("nan")
_INF = float("inf")

# ── 合成世界（全部在 2026-08：远早于任何一次运行的「今天」，`run_for_date` 不会去取实时 F&G）──────
DATES = ["2026-08-12", "2026-08-13", "2026-08-14", "2026-08-17", "2026-08-18", "2026-08-19",
         "2026-08-20", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"]
SINCE, BEFORE = "2026-08-12", "2026-08-28"
#: 种子持仓最早 entry_date = 08-04；窗口右端 = 最后回放日 + max(2, 3) 天
WINDOW = ("2026-08-04", "2026-08-30")

_SCORES = {"NEW1": 8.4, "NEW2": 8.2, "NEW3": 8.0, "NEW4": 7.8, "NOPX": 7.7, "NEW5": 7.6}


def _bdays(start, end):
    d, e, out = dt.date.fromisoformat(start), dt.date.fromisoformat(end), []
    while d < e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _flat(base, drift=0.0):
    return {d: (base + i * drift, base + i * drift + 0.5, base + i * drift - 0.5, base + i * drift + 0.1)
            for i, d in enumerate(_bdays("2026-07-27", "2026-09-12"))}


def _master():
    """{ticker: {date: (O, H, L, C)}}。值可以是 NaN / inf / 字符串——专门喂过滤器。"""
    m = {
        "TIMEX": _flat(100.0, 0.05),   # 种子多头，entry 08-04，TIME 止损日 08-18
        "SLX": _flat(100.0),           # 种子多头，entry 08-07，08-13 触 SL（93）
        "TPX": _flat(50.0),            # 种子空头，entry 08-06，08-14 触 TP（42.5）
        "GAPX": _flat(100.0, 0.1),     # 种子多头，entry 08-10，08-17 无 bar ⇒ MTM 用 08-18（前视）
        "NEW1": _flat(80.0, 0.05),
        "NEW2": _flat(60.0),
        "NEW3": _flat(60.0),
        "NEW4": _flat(40.0, 0.02),
        "NEW5": _flat(120.0, -0.03),
        "NOPX": _flat(30.0, 0.01),     # 快照没有 entry_price ⇒ 入场价取 Step 2 那次取数的当日收盘
        "BEAR1": _flat(70.0, -0.05),
    }
    m["TIMEX"]["2026-08-18"] = (100.0, 100.5, 99.5, _NAN)        # TIME 止损当天 Close=NaN ⇒ 丢 bar，顺延到 08-19
    m["SLX"]["2026-08-13"] = (99.0, 99.5, 92.0, 93.5)
    m["TPX"]["2026-08-14"] = (48.0, 48.5, 42.0, 43.0)
    del m["GAPX"]["2026-08-17"]
    m["GAPX"]["2026-08-18"] = (104.0, 108.5, 103.5, 108.0)
    m["NEW1"]["2026-08-20"] = (80.5, 81.0, 80.0, _NAN)
    m["NEW1"]["2026-08-25"] = (80.7, _INF, 80.2, 80.8)
    for d in _bdays("2026-08-17", "2026-09-12"):
        m["NEW2"][d] = (55.0, 55.5, 54.0, 55.0)                     # 08-17 起跌穿 60×0.93
    for d in _bdays("2026-08-25", "2026-09-12"):
        m["NEW3"][d] = (68.0, 70.0, 67.5, 69.5)                     # 08-25 起摸到 60×1.15
    m["NEW4"]["2026-08-19"] = (40.1, "n/a", 39.9, 40.2)             # float("n/a") ⇒ ValueError ⇒ 丢
    return m


class FakeYF:
    """yfinance 替身：`Ticker(t).history(start=, end=, **kw)` 从主表切 [start, end)。

    `wide_fail(ticker, start, end)` 返回 "raise" / "empty" / "allnan" / "shift" / None——只对指定调用生效，
    用来造「整段取数失败但逐次直连照常」的世界。
    """

    def __init__(self, master, wide_fail=None):
        self.master = master
        self.calls = []
        self.wide_fail = wide_fail or (lambda t, s, e: None)

    def module(self):
        return types.SimpleNamespace(Ticker=self._ticker)

    def _ticker(self, t):
        fake = self

        class _T:
            def history(self, *args, **kw):
                fake.calls.append((t, args, dict(kw)))
                start, end = kw["start"], kw["end"]
                how = fake.wide_fail(t, start, end)
                if how == "raise":
                    raise ConnectionError(f"fake outage for {t}")
                import pandas as pd
                if how == "empty":
                    return pd.DataFrame()
                # 同真 yfinance：start / end 先解析成日期再切（'2026-08-2' 是 8 月 2 日，不是按字符串比较）——
                # 否则「非 ISO 但字面落在窗口内」的请求，窗口切片与直连会因为两边都按字符串比而碰巧相同（N1）
                lo_d, hi_d = (pd.Timestamp(x).strftime("%Y-%m-%d") for x in (start, end))
                rows = sorted((d, v) for d, v in fake.master.get(t, {}).items() if lo_d <= d < hi_d)
                if how == "shift":   # 这一次响应的收盘整体 +1：造「宽取 ≠ 窄取」
                    rows = [(d, (o, h, lo, c + 1.0)) for d, (o, h, lo, c) in rows]
                if how == "allnan":  # 非空、但每一根都是 NaN 收盘（v0.45.97 那天的形状）
                    rows = [(d, (o, h, lo, _NAN)) for d, (o, h, lo, c) in rows]
                if not rows:
                    return pd.DataFrame()
                return pd.DataFrame([list(v) for _, v in rows], columns=["Open", "High", "Low", "Close"],
                                    index=pd.to_datetime([d for d, _ in rows]))
        return _T()


def _snapshot(snap_dir, ticker, date, score, direction, master):
    row = {"ticker": ticker, "date": date, "composite_score": score, "direction": direction,
           "agent_votes": {"a": score, "b": score}}
    if ticker != "NOPX":
        c = master[ticker].get(date, (None, None, None, None))[3]
        row["entry_price"] = c if isinstance(c, float) and math.isfinite(c) else 100.0
    (snap_dir / f"{ticker}_{date}.json").write_text(json.dumps(row), encoding="utf-8")


def _pos(ticker, entry_date, entry_price, direction="bullish", size_usd=5000.0):
    bull = direction == "bullish"
    return {"ticker": ticker, "direction": direction, "entry_date": entry_date, "entry_price": entry_price,
            "sl_price": round(entry_price * (0.93 if bull else 1.07), 4),
            "tp_price": round(entry_price * (1.15 if bull else 0.85), 4),
            "shares": round(size_usd / entry_price, 4), "size_usd": size_usd,
            "time_stop_date": (dt.date.fromisoformat(entry_date) + dt.timedelta(days=14)).isoformat(),
            "confidence": "high", "score": 7.5, "rationale": "seed", "sizing": "tier"}


def _jsonl(rows):
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


SEED = {
    "meta.json": json.dumps({"version": "test", "starting_capital": 50000.0, "starting_date": "2026-03-09",
                             "cash": 30000.0, "last_run_date": "2026-08-11", "config_snapshot": {}}).encode(),
    "positions.jsonl": _jsonl([_pos("TIMEX", "2026-08-04", 100.0), _pos("SLX", "2026-08-07", 100.0),
                               _pos("TPX", "2026-08-06", 50.0, "bearish"), _pos("GAPX", "2026-08-10", 100.0)]),
    "closed_trades.jsonl": _jsonl([{
        "ticker": "NEW5", "direction": "bullish", "entry_date": "2026-07-20", "entry_price": 118.0,
        "exit_date": "2026-07-27", "exit_price": 121.0, "holding_days": 7, "shares": 10.0,
        "gross_return_pct": 2.5, "net_return_pct": 2.4, "cost_pct": 0.1, "pnl_usd": 28.3,
        "exit_reason": "TP", "confidence": "high", "score": 7.6}]),
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """快照 + F&G/波动率库 + 假 yfinance；`_fetch_ohlc` 是真的（不打桩），缓存各自是新 dict。"""
    master = _master()
    snap = tmp_path / "snapshots"
    snap.mkdir()
    for d in DATES:
        for t, s in _SCORES.items():
            _snapshot(snap, t, d, s, "bullish", master)
    _snapshot(snap, "BEAR1", "2026-08-18", 3.0, "bearish", master)
    db = tmp_path / "fg.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL)")
    rows = []
    for d, v in (("2026-08-12", 90.0), ("2026-08-18", 10.0), ("2026-08-24", 80.0)):
        rows += [(d, "_MARKET", "market.fear_greed", v), (d, "_MARKET", "market.fear_greed_is_cnn", 1.0)]
    rows += [(d, "NEW1", "price.volatility_20d", 25.0) for d in ("2026-08-11", "2026-08-19")]
    con.executemany("INSERT INTO signal_archive VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    monkeypatch.setattr(pp, "SNAPSHOT_DIR", snap)
    monkeypatch.setattr(pp, "_pheromone_db_path", lambda: db)
    monkeypatch.setattr(pp, "_PRICE_CACHE", {})
    monkeypatch.setattr(pp, "_OHLC_FULL", {})
    # 顶层键 setitem（run_replay 的 finally 会整体替换嵌套 dict，嵌套 setitem 恢复不了）：
    # 让「已部署占比」闸在极度贪婪日真的卡住 A、放过减半后的 B ⇒ B 开出 A 没开的标的。
    monkeypatch.setitem(pp.CONFIG, "max_deployed_pct", 52.0)
    fake = FakeYF(master)
    monkeypatch.setitem(sys.modules, "yfinance", fake.module())
    return types.SimpleNamespace(master=master, fake=fake, tmp=tmp_path, monkeypatch=monkeypatch)


def _write_production_records(w):
    """「生产实际记录」= 门关闭、从同一份种子起跑的重放（直连路径），起点文件直接写盘。"""
    prod = w.tmp / "prod_run"
    prod.mkdir()
    for name, blob in SEED.items():
        (prod / name).write_bytes(blob)
    pp.run_replay({}, prod, dates=DATES)
    pp.CLOSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(prod / "closed_trades.jsonl", pp.CLOSED_FILE)
    shutil.copy(prod / "positions.jsonl", pp.POSITIONS_FILE)


def _sandbox_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _no_window(dates, seed):
    return contextlib.nullcontext()


def _spy_windows(w):
    """包一层 `pp.replay_ohlc_window`，把 yield 出来的窗口对象记下来（照常调用原函数）。"""
    seen = []
    orig = pp.replay_ohlc_window

    @contextlib.contextmanager
    def spy(start, end):
        with orig(start, end) as win:
            seen.append(win)
            yield win
    w.monkeypatch.setattr(pp, "replay_ohlc_window", spy)
    return seen


def _evaluate(w, label, *, batched, insample=False):
    """跑一次 `evaluate()`；返回（结果的规范 JSON、沙箱全部文件字节、这次的 yfinance 调用、结果 dict）。
    `batched=False` ⇒ 不开回放窗口，全部走 `_fetch_ohlc` 原直连路径（= 改动前的取数方式）。"""
    w.monkeypatch.setattr(pp, "_PRICE_CACHE", {})
    if not batched:
        w.monkeypatch.setattr(fwd, "_replay_ohlc_scope", _no_window)
    n0 = len(w.fake.calls)
    root = w.tmp / label
    res = fwd.evaluate(DATES, SINCE, BEFORE, root, insample=insample, seed=None if insample else SEED)
    if not batched:
        w.monkeypatch.setattr(fwd, "_replay_ohlc_scope", _REAL_SCOPE)
    # 复审 S1：开了窗口的结果多一个 `ohlc_window`（取数计数）；等价比的是**其余全部键**，新键单独核
    assert ("ohlc_window" in res) == batched, sorted(res)
    return _core_json(res), _sandbox_bytes(root), w.fake.calls[n0:], res


def _core_json(res):
    """结果去掉复审 S1 新增的 `ohlc_window` 后的规范 JSON——「改动前就有的键与值」逐字节比的对象。"""
    return json.dumps({k: v for k, v in res.items() if k != "ohlc_window"}, sort_keys=True, default=str)


_REAL_SCOPE = fwd._replay_ohlc_scope


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


@pytest.fixture
def pp_log():
    h = _Capture()
    pp._log.addHandler(h)
    yield h
    pp._log.removeHandler(h)


# ── 0. 夹具自证：没有这一组，下面的「逐字节相同」可能只是两个空世界相同 ─────────────────────

class TestWorldIsNotVacuous:
    def test_world_exercises_every_path_the_equivalence_claims_to_cover(self, world):
        """夹具自证：SL / TP / TIME 出场都发生；TIME 止损日的 NaN bar 让出场顺延一天；
        GAPX 在 as_of 缺 bar 的那天按前视价估值；NOPX 的入场价来自 Step 2 的取数；
        极度贪婪日 B 开出了 A 没开的标的。
        变异：改掉 `_master()` 里任一个刻意制造的形状 ⇒ 这里红（而下面的等价测试可能照样绿——
        两个都没走到该路径的世界当然相同）。"""
        _, files, _, res = _evaluate(world, "self_proof", batched=True)
        a_closed = [json.loads(x) for x in files["A_baseline/closed_trades.jsonl"].decode().splitlines()]
        reasons = {(t["ticker"], t["exit_reason"], t["exit_date"]) for t in a_closed}
        assert ("SLX", "SL", "2026-08-13") in reasons
        assert ("TPX", "TP", "2026-08-14") in reasons
        assert ("TIMEX", "TIME", "2026-08-19") in reasons, "08-18 的 NaN bar 应让 TIME 出场顺延到 08-19"
        assert ("GAPX", "TIME", "2026-08-24") in reasons, "GAPX 须持有过 08-17（缺 bar 那天），MTM 前视才被走到"
        assert any(t == "NEW3" and r == "TP" for t, r, _ in reasons)
        assert any(t == "NEW2" and r == "SL" for t, r, _ in reasons)
        a_opened = {t["ticker"] for t in a_closed + [json.loads(x) for x in
                    files["A_baseline/positions.jsonl"].decode().splitlines()] if t["entry_date"] == "2026-08-12"}
        b_rows = [json.loads(x) for x in files["B_treatment/closed_trades.jsonl"].decode().splitlines()] + \
                 [json.loads(x) for x in files["B_treatment/positions.jsonl"].decode().splitlines()]
        b_opened = {t["ticker"] for t in b_rows if t["entry_date"] == "2026-08-12"}
        assert b_opened - a_opened, f"B 应开出 A 没开的标的：A={a_opened} B={b_opened}"
        nopx = [t for t in b_rows if t["ticker"] == "NOPX" and t["entry_date"] == "2026-08-12"]
        assert nopx and nopx[0]["entry_price"] == pytest.approx(world.master["NOPX"]["2026-08-12"][3])
        a_eq = [json.loads(x) for x in files["A_baseline/equity_curve.jsonl"].decode().splitlines()]
        assert len(a_eq) == len(DATES)
        # GAPX 08-17 没有 bar：它的 MTM 必须用到 08-18 的 108（这一天 A 仍持有 GAPX）
        assert "2026-08-17" not in world.master["GAPX"]
        assert res["mode"] == "forward"


# ── 1. 逐根过滤：与直连路径同一份代码 ─────────────────────────────────────────────

class TestNanFilterSameAsDirectPath:
    @pytest.mark.parametrize("ticker,start,end", [
        ("TIMEX", "2026-08-04", "2026-08-20"),   # 含 NaN Close
        ("NEW1", "2026-08-12", "2026-08-27"),    # 含 NaN Close 与 inf High
        ("NEW4", "2026-08-12", "2026-08-22"),    # 含非数值 "n/a"
    ])
    def test_window_slice_equals_direct_fetch_and_drops_bad_bars(self, world, pp_log, ticker, start, end):
        """窗口切片 == 窗口外直连取的结果，且坏 bar 都不在里面、丢弃有 WARNING。
        变异「窗口路径不走 `_bars_from_history`（换成 prefetch_ohlc 那种裸 float 转换）」⇒ 红。"""
        direct = pp._fetch_ohlc(ticker, start, end)
        with pp.replay_ohlc_window(*WINDOW) as win:
            sliced = pp._fetch_ohlc(ticker, start, end)
        assert sliced == direct
        assert all(all(math.isfinite(v) for v in bar.values()) for bar in sliced.values())
        bad = {d for d, v in world.master[ticker].items()
               if start <= d < end and not all(isinstance(x, float) and math.isfinite(x) for x in v)}
        assert bad and not (bad & set(sliced))
        assert win.served == 1 and win.wide_fetches == 1
        assert sum("丢弃" in m and ticker in m for lvl, m in pp_log.records if lvl == "WARNING") == 2  # 直连一次 + 整段一次


    def test_mark_to_market_lookahead_when_as_of_bar_is_missing(self, world):
        """as_of 当天没有 bar（GAPX 08-17）：mark-to-market 回退到 max(ohlc) = 08-18 的 108（前视，生产既有行为）。
        窗口切片必须保留 as_of 之后的 bar 才能复现。变异「切片截到 as_of 为止 / 右端只到窗口内 as_of」⇒ 红。"""
        pos = pp.Position(**_pos("GAPX", "2026-08-10", 100.0))
        direct = pp._mark_to_market([pos], "2026-08-17")
        with pp.replay_ohlc_window(*WINDOW):
            sliced = pp._mark_to_market([pos], "2026-08-17")
        assert sliced == direct
        assert direct[1][0]["current_price"] == 108.0


# ── 2. 区间核对：窗口外 ⇒ 原直连路径，绝不截断 ────────────────────────────────────

class TestOutOfWindowFallsBackNotTruncates:
    @pytest.mark.parametrize("start,end", [
        ("2026-08-17", "2026-08-29"),   # 右端超出窗口
        ("2026-08-03", "2026-08-20"),   # 左端早于窗口
        ("2026-08-20", "2026-08-20"),   # 空区间
        ("2026-8-20", "2026-08-25"),    # 不是 YYYY-MM-DD（字典序反而落在 end 之后 ⇒ 空区间那道先拦下）
        # N1：不是 YYYY-MM-DD、但**字典序落在窗口内**（'2026-08-10' <= '2026-08-2' < '2026-08-25' <= '2026-08-26'）——
        # 只有 ISO 正则拦得住它。真 yfinance 把它读成 8 月 2 日（早于窗口），按字符串切片会丢掉 08-03..08-19。
        ("2026-08-2", "2026-08-25"),
    ])
    def test_request_outside_window_goes_direct(self, world, pp_log, start, end):
        """窗口 [08-10, 08-26)：窗口外请求返回与直连逐字节相同的结果（直连做了一次窄取数），计数 + WARNING。
        变异「删掉 `serve()` 里的区间核对」⇒ 右端超出那条返回截断到 08-25 的切片 ⇒ 红；
        变异「删掉 `serve()` 里的 ISO 正则」⇒ '2026-08-2' 那条被窗口按字符串切片（served=1、少了 08-03..08-19）⇒ 红。"""
        with pp.replay_ohlc_window("2026-08-10", "2026-08-26") as win:
            n0 = len(world.fake.calls)
            got = pp._fetch_ohlc("NEW5", start, end)
            narrow = [c for c in world.fake.calls[n0:] if (c[2]["start"], c[2]["end"]) == (start, end)]
        world.monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        assert got == pp._fetch_ohlc("NEW5", start, end)
        assert narrow, "窗口外请求必须真的走直连（窄区间）取数"
        assert win.out_of_window == 1 and win.served == 0
        assert any("窗口" in m and "外的请求" in m for lvl, m in pp_log.records if lvl == "WARNING")

    def test_truncation_would_have_been_visible(self, world):
        """正对照：右端超出窗口的那条请求，直连结果确实比窗口切片多出 bar——上一条的「相同」不是巧合。"""
        full = pp._fetch_ohlc("NEW5", "2026-08-17", "2026-08-29")
        assert "2026-08-26" in full and "2026-08-27" in full


# ── 3. 整段取数失败：返回空 / 抛异常两条路径，都退回直连且结果不变 ─────────────────────

class TestWideFetchFailureFallsBack:
    @pytest.mark.parametrize("how", ["raise", "empty", "allnan"])
    def test_evaluate_identical_when_wide_fetch_fails_for_some_tickers(self, world, pp_log, how):
        """SLX / NEW2 的整段取数失败（抛 / 空 / 非空但每根都是 NaN 收盘），它们的逐次直连照常：结果与不开窗口时
        逐字节相同，每个失败标的恰好一条「整段取数失败」WARNING，且它们确实走了窄区间直连。
        变异「整段返回空时当成权威 {}（不退回直连）」⇒ empty 这条红（SLX 不再止损）；
        变异「抛异常时吞掉并返回 {}」⇒ raise 这条红；
        变异「过滤后为空不退回（`if not bars:` → `if False:`）」⇒ allnan 这条红（v0.45.97 那天的形状：
        frame 非空、`len(hist) > 0` 那道拦不住，整段过滤成 {} 后被当成权威答案）。"""
        _write_production_records(world)
        base_json, base_files, _, _ = _evaluate(world, "base", batched=False)
        world.fake.wide_fail = (lambda t, s, e: how if t in ("SLX", "NEW2") and (s, e) == WINDOW else None)
        got_json, got_files, calls, res = _evaluate(world, "win", batched=True)
        assert got_json == base_json
        assert got_files == base_files
        warned = [m for lvl, m in pp_log.records if lvl == "WARNING" and "整段取数失败" in m]
        assert sorted(re.search(r"窗口 (\S+) \[", m).group(1) for m in warned) == ["NEW2", "SLX"], warned
        narrow_tickers = {t for t, _, kw in calls if (kw["start"], kw["end"]) != WINDOW}
        assert narrow_tickers == {"SLX", "NEW2"}
        # 复审 S1：同一件事在机读结果里也看得见（此前只在上面那两条 WARNING 里）
        ow = res["ohlc_window"]
        assert ow["degraded"] is True and sorted(ow["fallback_tickers"]) == ["NEW2", "SLX"] and ow["fallback"] == 2
        assert ow["direct_requests"] > 0 and ow["direct_empty"] == 0


# ── 4. 作用域：返回后 / 抛异常后都撤掉；不碰 _PRICE_CACHE / _OHLC_FULL ─────────────────

class TestScopeIsCleared:
    def test_cleared_after_run_and_caches_untouched(self, world, monkeypatch):
        """`run()` 返回后窗口已撤、两个模块级缓存一条没多（窗口路径从不写它们）。
        变异「finally 里不恢复 `_REPLAY_OHLC_WINDOW`」⇒ 红；变异「切片结果顺手写进 `_PRICE_CACHE`」⇒ 红。"""
        _write_production_records(world)
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: SEED)
        monkeypatch.setattr(fwd, "FORWARD_START", SINCE)
        res = fwd.run(today=BEFORE)
        assert res["mode"] == "forward" and res["n_dates"] == len(DATES)
        assert pp._REPLAY_OHLC_WINDOW is None
        assert pp._PRICE_CACHE == {} and pp._OHLC_FULL == {}

    def test_cleared_after_exception_inside_the_replay(self, world, monkeypatch):
        """重放中途抛异常：异常原样传出，窗口照样撤掉。变异「去掉 try/finally 只在正常路径恢复」⇒ 红。"""
        calls = {"n": 0}
        orig = pp.run_for_date

        def boom(as_of, *a, **k):
            calls["n"] += 1
            if calls["n"] == 3:
                assert pp._REPLAY_OHLC_WINDOW is not None, "异常发生时窗口应处于打开状态（否则本测试是空的）"
                raise RuntimeError("synthetic crash mid-replay")
            return orig(as_of, *a, **k)
        monkeypatch.setattr(pp, "run_for_date", boom)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            fwd.evaluate(DATES, SINCE, BEFORE, world.tmp / "sb", insample=False, seed=SEED)
        assert pp._REPLAY_OHLC_WINDOW is None

    def test_nested_windows_restore_the_outer_one(self, world):
        """嵌套：内层退出后恢复外层（不是置 None）。变异「finally 里直接置 None」⇒ 红。"""
        with pp.replay_ohlc_window(*WINDOW) as outer:
            with pytest.raises(ValueError):
                with pp.replay_ohlc_window("2026-08-10", "2026-08-20"):
                    raise ValueError("inner")
            assert pp._REPLAY_OHLC_WINDOW is outer
        assert pp._REPLAY_OHLC_WINDOW is None

    def test_production_path_never_opens_a_window(self):
        """生产 `run_for_date` 路径不许开始用窗口：非测试代码里只有前瞻检验脚本（与定义处）引用它。
        变异「在 run_for_date / run_replay 里套上 replay_ohlc_window」⇒ 红。"""
        from tests._repo_files import own_python_files
        files, _how = own_python_files(_ROOT)
        users = {str(p.relative_to(_ROOT)) for p in files
                 if "tests" not in p.relative_to(_ROOT).parts
                 and "replay_ohlc_window" in p.read_text(encoding="utf-8", errors="replace")}
        allowed = {"paper_portfolio.py", "experiments/fg_exposure_gate_forward_test.py",
                   "experiments/replay_ohlc_window_premise.py"}
        assert users - allowed == set(), f"新增了回放窗口的使用者：{users - allowed}"
        assert {"paper_portfolio.py", "experiments/fg_exposure_gate_forward_test.py"} <= users, users
        # 复审 N2：此前只扫 `def run_for_date(` 到 `def bootstrap_from_history(` 那一段字符串——`run_replay` 在它之后，
        # 「在 run_replay 里套窗口」根本扫不到，docstring 却说会红。改为按 AST 取这几个**生产**函数的源码逐个查；
        # 函数被改名 / 删掉也红（否则这条会悄悄变成没扫任何东西）。
        import ast
        src = (_ROOT / "paper_portfolio.py").read_text(encoding="utf-8")
        funcs = {n.name: ast.get_source_segment(src, n) for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
        production = ("run_for_date", "run_replay", "bootstrap_from_history", "_mark_to_market",
                      "_open_position", "_check_exit", "main")
        assert set(production) <= set(funcs), f"生产函数改名 / 删了，这条守卫就扫不到它们：{set(production) - set(funcs)}"
        for name in production:
            body = funcs[name]
            hits = [tok for tok in ("replay_ohlc_window", "replay_ohlc_bounds", "_REPLAY_OHLC_WINDOW",
                                    "_ReplayOhlcWindow") if tok in body]
            assert not hits, f"生产函数 {name} 引用了回放窗口：{hits}"


# ── 5. 取数次数：每个标的每次检验一次整段；零窗口外请求 ────────────────────────────

class TestOneWideFetchPerTickerPerRun:
    def test_forward_evaluate_fetches_each_ticker_once_over_the_window(self, world):
        """前瞻 evaluate（A、B 两遍重放）：yfinance 调用全部是整段窗口、每个标的恰好一次，
        零窗口外请求、零退回直连；不开窗口时同一场景的调用数多得多。
        变异「批量化形同虚设（serve 恒返回 None）」⇒ 红；变异「只给 A 套窗口、B 自己再取一遍」⇒ 红；
        变异「种子持仓不进窗口左端（_seed_held_entry_dates 返回 []）」⇒ 红；
        变异「某个调用点前视放宽（如 as_of+4）而 _EXIT/_ENTRY_OHLC_LOOKAHEAD_DAYS 没跟」⇒ 红。"""
        _write_production_records(world)
        seen = _spy_windows(world)
        _, _, calls, _ = _evaluate(world, "win", batched=True)
        assert len(seen) == 1, "A、B 必须在同一个窗口里"
        win = seen[0]
        assert (win.start, win.end) == WINDOW
        assert win.out_of_window == 0 and win.fallback_tickers == {}
        tickers = [t for t, _, _ in calls]
        assert len(tickers) == len(set(tickers)), f"有标的被整段取了不止一次：{tickers}"
        assert {(kw["start"], kw["end"]) for _, _, kw in calls} == {WINDOW}
        assert all(args == () and kw == {"start": WINDOW[0], "end": WINDOW[1], "auto_adjust": False}
                   for _, args, kw in calls), "整段取数必须与直连路径同一个调用形状"
        assert win.served > len(tickers)
        _, _, base_calls, _ = _evaluate(world, "base", batched=False)
        assert len(base_calls) >= 3 * len(calls), (len(base_calls), len(calls))

    def test_empty_in_window_slice_is_answered_without_the_network(self, world):
        """复审 N3：窗口内、但这个子区间确实没有 bar（周六、周日）⇒ 切片是 `{}`，它就是答案，不许再去打网络。
        变异「`if sliced is not None` → `if sliced`」⇒ 空切片落到直连、多一次 yfinance 调用 ⇒ 红
        （结果照样是 `{}`，只有调用数能看出来——这正是「性能回归」那一类）。"""
        assert [dt.date.fromisoformat(d).weekday() for d in ("2026-08-15", "2026-08-16")] == [5, 6]
        with pp.replay_ohlc_window(*WINDOW) as win:
            first = pp._fetch_ohlc("NEW5", "2026-08-12", "2026-08-14")   # 触发该标的唯一一次整段取数
            n0 = len(world.fake.calls)
            got = pp._fetch_ohlc("NEW5", "2026-08-15", "2026-08-17")     # 窗口内、只有周末
            assert len(world.fake.calls) == n0, world.fake.calls[n0:]
        assert first and got == {}
        assert (win.served, win.wide_fetches, win.direct_requests) == (2, 1, 0)
        world.monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        assert pp._fetch_ohlc("NEW5", "2026-08-15", "2026-08-17") == {}   # 直连也是空：切片没有丢东西

    def test_bounds_are_derived_from_the_lookahead_constants(self):
        """窗口右端 = 最后回放日 + max(两个前视常量)；左端含种子持仓的 entry_date。"""
        look = max(pp._EXIT_OHLC_LOOKAHEAD_DAYS, pp._ENTRY_OHLC_LOOKAHEAD_DAYS)
        s, e = pp.replay_ohlc_bounds(DATES, ["2026-08-04", "garbage", None])
        assert s == "2026-08-04"
        assert e == (dt.date.fromisoformat(DATES[-1]) + dt.timedelta(days=look)).isoformat()
        assert pp.replay_ohlc_bounds([], ["2026-08-04"]) is None


# ── 5b. 降级看得见（复审 S1）与行情全断时自证归因（复审 S3）─────────────────────────────

def _core(res):
    return {k: v for k, v in res.items() if k != "ohlc_window"}


class TestDegradedWindowIsMachineReadable:
    """复审 S1：整段取数全部退回逐次直连时（Yahoo 拒绝宽区间请求），此前 `run()` 结果、`--quiet` 那一行与
    attention 与健康时**逐字节相同**，只有 stderr 的 WARNING 看得见（实测 20 → 144 次调用、17s → 56s，
    离 Step 11 的 60s 超时只差 4s）。现在：结果多一个 `ohlc_window`，降级时进度行换 ⚠️ 并追加一段、
    attention 多一条 `ohlc_window_degraded`；统计量与判定不变（`_core` 与健康时相等）。"""

    @pytest.fixture
    def fwd_run(self, world, monkeypatch):
        _write_production_records(world)
        monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: SEED)
        monkeypatch.setattr(fwd, "FORWARD_START", SINCE)

        def _run():
            monkeypatch.setattr(pp, "_PRICE_CACHE", {})
            n0 = len(world.fake.calls)
            res = fwd.run(today=BEFORE)
            return res, world.fake.calls[n0:]
        return _run

    def test_healthy_run_adds_counters_but_changes_no_line_and_no_attention(self, fwd_run):
        res, calls = fwd_run()
        ow = res["ohlc_window"]
        assert ow["window"] == list(WINDOW) and ow["degraded"] is False
        assert (ow["fallback"], ow["fallback_tickers"], ow["out_of_window"], ow["direct_requests"]) == (0, {}, 0, 0)
        assert ow["wide_fetches"] == len(calls) == 11 and ow["served"] > 11
        line = fwd.status_line(res)
        assert line == fwd.status_line(_core(res)) and line.startswith("⏳ "), line
        assert rr._forward_test_attention("fg_exposure_gate_forward", rr._detail(res, rr._FWD_DETAIL_KEYS)) == []

    def test_every_wide_fetch_rejected_is_visible_in_result_line_and_attention(self, world, fwd_run):
        """变异「隐藏降级信号」（`stats()` 的 degraded 恒 False / `_FWD_DETAIL_KEYS` 去掉 ohlc_window /
        status_line 不看它）⇒ 这条红。"""
        healthy, _ = fwd_run()
        world.fake.wide_fail = (lambda t, s, e: "raise" if (s, e) == WINDOW else None)
        res, calls = fwd_run()
        assert _core(res) == _core(healthy), "降级只许改取数方式，不许改判定 / 统计 / 自证"
        ow = res["ohlc_window"]
        assert ow["degraded"] is True and ow["served"] == 0 and ow["fallback"] == ow["wide_fetches"] == 11
        assert ow["direct_requests"] > 0 and ow["direct_empty"] == 0
        assert len(calls) >= 3 * 11, "退回逐次直连：调用数回到改动前的量级"
        line = fwd.status_line(res)
        assert line.startswith("⚠️ ") and "回放行情窗口降级" in line and "11/11 个标的整段取数失败" in line, line
        items = rr._forward_test_attention("fg_exposure_gate_forward", rr._detail(res, rr._FWD_DETAIL_KEYS))
        assert [(a["id"], a["level"]) for a in items] == [
            ("ic_rerun.fg_exposure_gate_forward.ohlc_window_degraded", "warn")]


class TestSelfproofBlamesDataWhenOhlcIsGone:
    def test_total_ohlc_outage_is_named_as_such(self, world):
        """复审 S3：整段与逐次直连全部失败（行情全断）⇒ 自证失败的原因说「OHLC 不可得」，不再推给评分链 / 配置 /
        漏跑；状态照旧 cannot_judge。变异「`_ohlc_unavailable` 恒 False」⇒ 红。"""
        _write_production_records(world)
        world.fake.wide_fail = (lambda t, s, e: "raise")
        _, _, _, res = _evaluate(world, "outage", batched=True)
        assert res["status"] == "cannot_judge", res
        ow = res["ohlc_window"]
        assert ow["served"] == 0 and ow["direct_requests"] > 0 and ow["direct_empty"] == ow["direct_requests"]
        assert "OHLC 不可得" in res["reason"] and "已被改动" not in res["reason"] and "先查" not in res["reason"], res["reason"]

    def test_wide_rejected_but_direct_fine_keeps_the_original_reason(self, world, monkeypatch):
        """反面：整段被拒、直连照常（S1 的形状）时重放拿到的就是改动前那份行情——自证若因别的原因失败
        （这里：入场门槛被改），原因照旧指向配置，不许被说成「行情不可得」。"""
        _write_production_records(world)
        monkeypatch.setitem(pp.CONFIG, "entry_score_bull", 9.5)
        world.fake.wide_fail = (lambda t, s, e: "raise" if (s, e) == WINDOW else None)
        _, _, _, res = _evaluate(world, "cfg", batched=True)
        assert res["status"] == "cannot_judge" and res["ohlc_window"]["degraded"] is True
        assert "OHLC 不可得" not in res["reason"], res["reason"]


# ── 6. 等价：合成数据上开 / 不开窗口，返回值与沙箱状态逐字节相同 ─────────────────────

class TestEquivalenceOnSyntheticData:
    def test_forward_evaluate_is_byte_identical(self, world):
        """前瞻模式（种子起跑，自证比对「生产」）：返回值与 A/B 沙箱四个状态文件逐字节相同。
        变异「窗口路径丢掉 NaN 过滤」⇒ 红（TIMEX 在 NaN 那天 TIME 出场，现金变 NaN）；
        变异「切片右端改成含 end（<=）」⇒ 红。"""
        _write_production_records(world)
        base_json, base_files, _, _ = _evaluate(world, "base", batched=False)
        got_json, got_files, _, res = _evaluate(world, "win", batched=True)
        assert res["selfproof_rate"] == 1.0
        assert got_json == base_json
        assert got_files == base_files

    def test_insample_evaluate_is_byte_identical(self, world):
        """样本内模式（无种子，含 A_check 第三遍重放；返回值里有周度差与被调整单的盈亏）。"""
        base_json, base_files, _, base = _evaluate(world, "base", batched=False, insample=True)
        got_json, got_files, calls, _ = _evaluate(world, "win", batched=True, insample=True)
        assert base["status"] == "insample" and base["mechanism_selfcheck_ok"] is True
        assert base["adjusted_trades"]["adjusted_closed_trades"] > 0
        assert got_json == base_json
        assert got_files == base_files
        tickers = [t for t, _, _ in calls]
        assert len(tickers) == len(set(tickers)), "A、B、A_check 三遍重放共用一个窗口"

    def test_rehearse_is_byte_identical_and_batched(self, world, monkeypatch):
        """`--rehearse` 路径同样套了窗口：结果相同、每个标的一次整段取数。
        变异「rehearse 里去掉 `_replay_ohlc_scope`」⇒ 调用数断言红。"""
        _write_production_records(world)
        manifest = {"source": {"commit": "synthetic"}, "seed_last_run_date": "2026-08-11"}
        monkeypatch.setattr(fwd, "build_seed_from_git", lambda since, repo_root=None: (SEED, manifest))
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(fwd, "_replay_ohlc_scope", _no_window)
        base = fwd.rehearse(SINCE, BEFORE)
        monkeypatch.setattr(fwd, "_replay_ohlc_scope", _REAL_SCOPE)
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        n0 = len(world.fake.calls)
        got = fwd.rehearse(SINCE, BEFORE)
        calls = world.fake.calls[n0:]
        assert base["status"] == "rehearsal_ok"
        assert "ohlc_window" not in base and got["ohlc_window"]["degraded"] is False
        assert _core_json(got) == _core_json(base)
        assert {(kw["start"], kw["end"]) for _, _, kw in calls} == {WINDOW}
        assert len(calls) == len({t for t, _, _ in calls})


# ── 7. 真 Yahoo 前提核对脚本（显式命令）本身有牙 ──────────────────────────────────

def _load_premise():
    spec = importlib.util.spec_from_file_location(
        "replay_ohlc_window_premise_rw", _ROOT / "experiments" / "replay_ohlc_window_premise.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPremiseScript:
    @pytest.fixture
    def prem(self, world, monkeypatch):
        _write_production_records(world)
        monkeypatch.setattr(pp, "_PRICE_CACHE", {})
        monkeypatch.setattr(fwd, "load_seed", lambda *a, **k: SEED)
        monkeypatch.setattr(fwd, "FORWARD_START", SINCE)
        mod = _load_premise()
        monkeypatch.setattr(mod, "_load_fwd", lambda: fwd)
        return mod

    def test_ok_when_narrow_equals_wide_slice(self, prem, world, capsys):
        """假 yfinance 按构造满足前提 ⇒ ok（exit 0），且真的核了每个标的的每个不同请求。"""
        res = prem.check(today=BEFORE, fwd_module=fwd)
        assert res["status"] == "ok", res
        assert res["n_tickers"] == 11 and res["n_requests"] > 11 and res["n_excluded_tickers"] == 0
        assert prem.main(["--today", BEFORE]) == 0
        out = capsys.readouterr().out
        assert out.startswith("✅ 回放 OHLC 窗口前提：ok") and "未参与核对" not in out, out
        assert pp._REPLAY_OHLC_WINDOW is None and pp.replay_ohlc_window.__name__ == "replay_ohlc_window"

    def test_excluded_fallback_tickers_are_counted_in_the_line(self, prem, world, capsys):
        """复审 S2（nit）：整段取数退回直连的标的不参与核对——11 个里 10 个被排除时不能印一个干净的 ✅。
        状态照旧 ok / exit 0（核了的那一个确实相同），但那一行写出排除了几个、是哪些，且段首是 ⚠️。"""
        world.fake.wide_fail = (lambda t, s, e: "raise" if (s, e) == WINDOW and t != "NEW5" else None)
        res = prem.check(today=BEFORE, fwd_module=fwd)
        assert res["status"] == "ok" and res["n_tickers"] == 1 and res["n_excluded_tickers"] == 10, res
        assert prem.main(["--today", BEFORE]) == 0
        out = capsys.readouterr().out
        assert out.startswith("⚠️ 回放 OHLC 窗口前提：ok") and "另有 10 个标的整段取数退回了直连、未参与核对" in out, out

    def test_empty_refetch_is_cannot_judge_not_mismatch(self, prem, world):
        """复审 S2：窗口外直连重取返回**空 DataFrame**（yfinance 不抛异常的失败形状）⇒ cannot_judge（exit 3），
        与抛异常同一结局——此前拿 `{}` 去比切片，报成 mismatch（exit 1），把「取不到」说成「Yahoo 前后不一致」。
        变异「空结果照旧按 {} 去比」⇒ 红。"""
        world.fake.wide_fail = (lambda t, s, e: "empty" if (s, e) != WINDOW and t == "NEW5" else None)
        res = prem.check(today=BEFORE, fwd_module=fwd)
        assert res["status"] == "cannot_judge" and res["mismatches"] == [], res
        assert res["failures"] and {f["ticker"] for f in res["failures"]} == {"NEW5"}
        assert all("空 DataFrame" in f["error"] for f in res["failures"])
        assert prem.main(["--today", BEFORE]) == 3

    def test_mismatch_is_red(self, prem, world):
        """NEW5 的整段响应收盘整体 +1（「宽取 ≠ 窄取」）⇒ mismatch、exit 1，并指出差在哪几天。
        变异「check() 不比对 / 恒返回 ok」⇒ 红。"""
        world.fake.wide_fail = (lambda t, s, e: "shift" if t == "NEW5" and (s, e) == WINDOW else None)
        res = prem.check(today=BEFORE, fwd_module=fwd)
        assert res["status"] == "mismatch"
        assert {m["ticker"] for m in res["mismatches"]} == {"NEW5"} and res["mismatches"][0]["days"]
        assert prem.main(["--today", BEFORE]) == 1

    def test_no_window_is_cannot_judge_not_ok(self, prem, monkeypatch):
        """run() 提前返回、没打开窗口 ⇒ cannot_judge（exit 3），不是「没发现不同 ⇒ ok」。"""
        monkeypatch.setattr(fwd, "run", lambda **k: {"status": "not_ready"})
        res = prem.check(today=BEFORE, fwd_module=fwd)
        assert res["status"] == "cannot_judge" and res["n_windows"] == 0
        assert prem.main(["--today", BEFORE]) == 3

    def test_refetch_failure_is_cannot_judge(self, prem, world):
        """窗口外直连重取抛异常 ⇒ cannot_judge（exit 3），不许把「取不到」当成「相同」。"""
        world.fake.wide_fail = (lambda t, s, e: "raise" if (s, e) != WINDOW and t == "NEW5" else None)
        res = prem.check(today=BEFORE, fwd_module=fwd)
        assert res["status"] == "cannot_judge" and res["failures"]
