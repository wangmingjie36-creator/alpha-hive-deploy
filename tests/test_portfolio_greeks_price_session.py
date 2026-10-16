"""v0.45.423 组合 Greeks 的标的价必须属于 as_of 那一场（portfolio_greeks × cboe_options × twelve_data）。

事故（2026-10-07 取证：生产 hedge_state/ 审计文件 + 日志 + yfinance 日线对照）
---------------------------------------------------------------------------
`_default_close` 取 Twelve Data 日线「≤ as_of、5 个日历日内最后一根」。`twelve_data._drop_forming_bar`
按**美东日期**丢当日那根、不看收没收盘，生产扫描却在收盘后（17:xx ET）跑——每天拿到的都是**前一交易日**
的收盘，配当天的 CBOE 期权报价算 $Delta，SPY 对冲按昨收「收盘成交」：
  · 10-06 那轮 14:15 PDT（17:15 ET，收盘 75 分钟后）丢了 31 根「日期 2026-10-06 是美东当日」；
  · 09-04~10-06 的 20 份审计文件里 190/267 个价晚一个交易日，正常时刻的运行 SPY 全晚一天；
    只有过了美东午夜才跑的那几次（09-08、10-05 重跑于 02:56 ET）是对的——「偶发」其实是常态；
  · 5 笔 SPY 成交 4 笔按前一日收盘成交，3 笔股数因此不同（09-04 −13 应 −12、09-28 +12 应 +10、10-01 +30 应 +29）；
  · 10-06：JNJ S=252.93（10-05 收盘）配 261016 P270 mid 15.95，低于内在价值 17.07——美式认沽不可能；
    用 10-06 的 254.78 就自洽（`TestPairedSpotIsSelfConsistent` 把它写成不变式）。

写法约束（变异检验 M0 拿改动前的 portfolio_greeks.py 跑这些测试）
-----------------------------------------------------------------
行为测试**只走改动前就有的入口**：compute_day / run_for_date 的默认取价、默认报价（不注入 closes_fn /
quotes_fn）。桩只打在数据边界——CBOE payload（`_fetch_cboe_payload`）、`twelve_data._fetch_rows`、三个
模块的钟；twelve_data 那道「丢当日那根」的闸**用真的**：复现的是事故的机制，不是事故的结果。
"""

import json
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import cboe_options as co
import options_paper_leg as opl
import paper_portfolio as pp
import portfolio_greeks as pg
import twelve_data as td
from is_trading_day import is_trading_day

ET = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")
AS_OF, PREV = "2026-10-06", "2026-10-05"
AFTER_CLOSE = datetime(2026, 10, 6, 17, 42, tzinfo=ET)       # 生产尾段时刻（14:42 PDT）
EXPIRY = "2026-10-16"
JNJ_CALL, JNJ_PUT = "JNJ261016C00270000", "JNJ261016P00270000"
# (10-05 收盘, 10-06 收盘)：前者 = greeks_2026-10-06.json 里实际用的价，后者 = 当晚 CBOE 收盘后快照
CLOSE = {"AMZN": (251.40, 256.29), "TSLA": (378.73, 380.68), "JNJ": (252.93, 254.78), "SPY": (774.83, 779.09)}
_BASE = {"AMZN": 240.0, "TSLA": 350.0, "JNJ": 245.0, "SPY": 760.0}


def _trading_days(start: str, end: str):
    d, out = date.fromisoformat(start), []
    while d <= date.fromisoformat(end):
        if is_trading_day(d)[0]:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _series(tk: str, end: str):
    """2026-09-21 起到 `end`（含）的完整日线；PREV / AS_OF 两天是真实收盘。"""
    rows = [{"date": d, "close": round(_BASE[tk] + 0.1 * i, 2), "vol": 1e6}
            for i, d in enumerate(_trading_days("2026-09-21", end))]
    for r in rows:
        if r["date"] in (PREV, AS_OF):
            r["close"] = CLOSE[tk][0 if r["date"] == PREV else 1]
    return rows


def _payload(tk, close, last_trade, *, current=None, options=()):
    """CBOE `data` 段的最小形状。current_price 故意 ≠ close：收盘后它是盘后价，取错了要看得出来。"""
    return {"symbol": tk, "close": close, "last_trade_time": last_trade, "options": list(options),
            "current_price": round(close * 1.004, 2) if current is None else current}


def _jnj_options():
    return [{"option": JNJ_CALL, "bid": 0.30, "ask": 0.40, "iv": 0.22, "delta": 0.08, "gamma": 0.012,
             "vega": 0.05, "theta": -0.03, "open_interest": 300},
            {"option": JNJ_PUT, "bid": 15.80, "ask": 16.10, "iv": 0.22, "delta": -0.92, "gamma": 0.012,
             "vega": 0.06, "theta": -0.01, "open_interest": 500}]


def _jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _beta1(ticker, as_of):
    return (1.0, "ols60")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """三本账的状态目录全进 tmp；钟、CBOE payload、Twelve Data 原始行都由测试摆。"""
    hs = tmp_path / "hedge_state"
    for name, val in (("STATE_DIR", hs), ("POSITIONS_FILE", hs / "positions.jsonl"),
                      ("TRADES_FILE", hs / "trades.jsonl"), ("EQUITY_FILE", hs / "equity_curve.jsonl"),
                      ("META_FILE", hs / "meta.json"), ("BETA_CACHE_FILE", hs / "beta_cache.json")):
        monkeypatch.setattr(pg, name, val)
    ps, os_ = tmp_path / "paper_portfolio_state", tmp_path / "options_paper_state"
    for mod, d in ((pp, ps), (opl, os_)):
        monkeypatch.setattr(mod, "POSITIONS_FILE", d / "positions.jsonl")
        monkeypatch.setattr(mod, "EQUITY_FILE", d / "equity_curve.jsonl")
        monkeypatch.setattr(mod, "META_FILE", d / "meta.json")
    monkeypatch.setitem(pg.CONFIG, "rebalance_to", "edge")
    monkeypatch.setitem(pg.CONFIG, "beta_delta_band_pct", 15.0)
    monkeypatch.setitem(pg.CONFIG, "beta_delta_target_pct", 0.0)
    monkeypatch.setattr(pg, "_LAST_PRICE_CHECK", None, raising=False)    # 改动前没有这个属性

    w = SimpleNamespace(payloads={}, bars_end=AS_OF, fetches=[], overrides={})

    def at(now_et):
        monkeypatch.setattr(co, "_et_now", lambda: now_et)
        monkeypatch.setattr(co, "_pdt_now", lambda: now_et.astimezone(LA).replace(tzinfo=None))
        monkeypatch.setattr(td, "_et_today", lambda: now_et.date().isoformat())
        monkeypatch.setattr(td, "_et_now", lambda: now_et, raising=False)   # 改动前没有这个钩子
        monkeypatch.setattr(pg, "_et_now", lambda: now_et, raising=False)   # 改动前没有这个钩子
    w.at = at

    def fetch_payload(tk, timeout, **kw):
        w.fetches.append(tk.upper())
        return w.payloads.get(tk.upper())
    monkeypatch.setattr(co, "_fetch_cboe_payload", fetch_payload)
    monkeypatch.setattr(co, "_SNAPSHOT_PROVIDER", None)

    def fetch_rows(ticker, days, end_date=None):
        rows = [dict(r, close=w.overrides.get((ticker, r["date"]), r["close"]))
                for r in _series(ticker, w.bars_end) if end_date is None or r["date"] <= end_date]
        return td._drop_forming_bar(rows, ticker)             # 真的那道闸：按美东日期丢当日那根
    monkeypatch.setattr(td, "is_configured", lambda: True)
    monkeypatch.setattr(td, "_fetch_rows", fetch_rows)

    def closed(session=AS_OF, tickers=("AMZN", "TSLA", "JNJ", "SPY")):
        """收盘后的新鲜文件：last_trade 贴着收盘，close = 那一场的官方收盘。"""
        for tk in tickers:
            px = CLOSE[tk][0 if session == PREV else 1]
            w.payloads[tk] = _payload(tk, px, f"{session}T15:59:58",
                                      options=_jnj_options() if tk == "JNJ" else ())
    w.closed = closed

    def intraday(tickers=("AMZN", "TSLA", "JNJ", "SPY"), last=f"{AS_OF}T11:59:30"):
        """盘中文件：close / current_price 都是此刻的成交价（取 as_of 收盘的 99.9%）。"""
        for tk in tickers:
            live = round(CLOSE[tk][1] * 0.999, 2)
            w.payloads[tk] = _payload(tk, live, last, current=live,
                                      options=_jnj_options() if tk == "JNJ" else ())
    w.intraday = intraday

    def book(as_of=AS_OF, stocks=(("AMZN", 400), ("TSLA", 20)), straddle=True):
        """默认组合：股票 ≈ $110k 多头 + JNJ 深度实值认沽的长跨式（净 $Delta ≈ −$21k），合并 NAV $200k
        ⇒ β·Δ ≈ +44% NAV，出 ±15% 带 ⇒ 正常应卖 SPY。"""
        _jsonl(pp.POSITIONS_FILE, [{"ticker": t, "direction": "bullish", "entry_date": "2026-09-25",
                                    "entry_price": 100.0, "shares": n} for t, n in stocks])
        _jsonl(pp.EQUITY_FILE, [{"date": as_of, "nav": 100_000.0}])
        _jsonl(opl.EQUITY_FILE, [{"date": as_of, "nav": 100_000.0}])
        if straddle:
            _jsonl(opl.POSITIONS_FILE, [{"ticker": "JNJ", "side": "long", "contracts": 1, "strike": 270.0,
                                         "expiry": EXPIRY, "call_symbol": JNJ_CALL, "put_symbol": JNJ_PUT}])
    w.book = book

    pg._BARS_CACHE.clear()
    td.clear_bars_cache()
    at(AFTER_CLOSE)
    yield w
    pg._BARS_CACHE.clear()
    td.clear_bars_cache()


def _rows(res, kind):
    return [r for r in res["rows"] if r["kind"] == kind]


# ── 事故复现：收盘后同日跑（10-06 17:42 ET）──────────────────────────────────

class TestTwelveDataDropsTheSameDayBar:
    """v0.45.438：Twelve Data 的当日那根**当晚是临时值**——2026-10-08 收盘 38 分钟后 SPY 773.86（官方 773.93，
    CBOE 与 yfinance 一致）、成交量只有窗口中位数的 3.4%。v0.45.423 曾以为「收完的当日那根被误丢」、把规则 ①
    改成收盘 + 30 分钟后照收，前提不成立，已恢复按美东日期丢。
    夹具里当日那根的成交量是**满的**（与中位数相同）：规则 ② 拦不住它，只剩 ① ——钉的就是 ①。"""

    @pytest.mark.parametrize("hh,mm", [(11, 0), (15, 59), (16, 38), (17, 42), (23, 59)])
    def test_same_day_bar_is_dropped_all_evening(self, world, hh, mm):
        world.at(datetime(2026, 10, 6, hh, mm, tzinfo=ET))
        assert td.fetch_bars("JNJ", 120, end_date=AS_OF)[-1]["date"] == PREV

    def test_after_midnight_et_the_bar_is_kept(self, world):
        """美东日期翻页之后那根才是定稿值（09-08 / 10-05 两次午夜后重跑与官方收盘逐分相等）。"""
        world.at(datetime(2026, 10, 7, 0, 30, tzinfo=ET))
        rows = td.fetch_bars("JNJ", 120, end_date=AS_OF)
        assert (rows[-1]["date"], rows[-1]["close"]) == (AS_OF, CLOSE["JNJ"][1]), rows[-1]

    def test_early_close_day_is_dropped_all_evening_too(self, world):
        """感恩节次日 13:00 收盘：当晚同样是临时值，与平日一样按日期丢（不按 `session_close_et` 放行）。"""
        world.bars_end = "2026-11-27"
        world.at(datetime(2026, 11, 27, 16, 0, tzinfo=ET))
        assert td.fetch_bars("JNJ", 120, end_date="2026-11-27")[-1]["date"] == "2026-11-25"

    def test_the_measured_provisional_bar_is_dropped_even_at_full_volume(self, monkeypatch):
        """末根收盘 = 10-08 16:38 ET 实测的临时值 773.86；成交量换成满的（实测是 1,517,497），成交量闸拦不住，
        只有日期规则拦——v0.45.423 的规则在这里会把它当 10-08 的收盘收下。前五根合成。"""
        monkeypatch.setattr(td, "_et_today", lambda: "2026-10-08")
        # v0.45.423 的规则还读分钟钟 `_et_now`：钉在实测时刻之后（17:42 ET），否则它在别的日子按「此刻不在那一天」
        # 照样丢，这条对旧规则就不红了（钩子现已不存在，raising=False）
        monkeypatch.setattr(td, "_et_now", lambda: datetime(2026, 10, 8, 17, 42, tzinfo=ET), raising=False)
        rows = [{"date": d, "close": 770.0 + i, "vol": 40e6}
                for i, d in enumerate(("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"))]
        rows.append({"date": "2026-10-08", "close": 773.86, "vol": 40e6})
        assert td._drop_forming_bar(rows, "SPY")[-1]["date"] == "2026-10-07"


class TestSameDayRunUsesTheAsOfSession:

    def test_stock_rows_priced_at_the_as_of_close(self, world):
        world.closed()
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        px = {r["ticker"]: r["price"] for r in _rows(res, "stock")}
        assert px == {"AMZN": CLOSE["AMZN"][1], "TSLA": CLOSE["TSLA"][1]}, f"用的不是 {AS_OF} 的收盘：{px}"

    def test_option_spot_comes_from_the_quote_payload(self, world):
        world.closed()
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        opt = _rows(res, "option")
        assert len(opt) == 2 and [r["price"] for r in opt] == [CLOSE["JNJ"][1]] * 2
        put = next(r for r in opt if r["cp"] == "put")
        assert put["dollar_delta"] == pytest.approx(100 * -0.92 * CLOSE["JNJ"][1], abs=0.01)

    def test_spy_price_is_the_as_of_close(self, world):
        world.closed()
        world.book()
        assert pg.compute_day(AS_OF, beta_fn=_beta1)["spy_price"] == CLOSE["SPY"][1]

    def test_after_hours_current_price_is_never_used_after_close(self, world):
        """收盘后 payload 的 current_price 是盘后价（这里故意 +0.4%）——取价必须是 close。"""
        world.closed()
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        after_hours = {tk: p["current_price"] for tk, p in world.payloads.items()}
        assert all(r["price"] != after_hours[r["ticker"]] for r in res["rows"] if r.get("price") is not None)


class TestPairedSpotIsSelfConsistent:
    """10-06 的取证本身写成不变式：S 与期权报价同一时刻时，美式期权的 mid 不会低于内在价值。
    S=252.93（前一日）配 P270 mid 15.95 ⇒ 内在价值 17.07 > mid——这组价不可能同时成立。"""

    def test_option_mid_not_below_intrinsic_at_the_spot_used(self, world):
        world.closed()
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        checked = 0
        for r in _rows(res, "option"):
            if r.get("mid") is None or r.get("price") is None:
                continue
            S, K = r["price"], r["strike"]
            intrinsic = max(K - S, 0.0) if r["cp"] == "put" else max(S - K, 0.0)
            assert r["mid"] >= intrinsic - 0.01, (
                f"{r['symbol']} mid {r['mid']} < 内在价值 {intrinsic:.2f}（S={S}）——S 与报价不是同一时刻")
            checked += 1
        assert checked == 2


class TestHedgeFillsAtTheAsOfClose:

    def test_trade_and_mark_use_the_as_of_spy_close(self, world):
        world.closed()
        world.book()
        r = pg.run_for_date(AS_OF, beta_fn=_beta1)
        t = r["executed_trade"]
        assert t is not None and t["action"] == "sell_spy", r["recommendation"]
        assert t["price"] == CLOSE["SPY"][1], f"按 {t['price']} 成交，不是 {AS_OF} 的收盘 {CLOSE['SPY'][1]}"
        eq = pg._load_jsonl(pg.EQUITY_FILE)[-1]
        assert eq["date"] == AS_OF and eq["spy_price"] == CLOSE["SPY"][1]
        # 股数按 as_of 的价算：β·Δ 超出 +15% 带边的部分 / SPY 收盘，向外取整
        expect = (100 * 0.08 + 100 * -0.92) * CLOSE["JNJ"][1] + 400 * CLOSE["AMZN"][1] + 20 * CLOSE["TSLA"][1]
        excess = expect - 0.15 * 200_000.0
        import math
        assert t["shares"] == -math.ceil(excess / CLOSE["SPY"][1] - 1e-9)


# ── 拿不到 as_of 的价：判陈旧、看得见、不对冲 ─────────────────────────────────

class TestNoAsOfPriceIsStaleNotSilent:
    """AMZN 的 CBOE 文件拿不到，Twelve Data 也只到前一交易日：旧代码会拿 10-05 的 251.40 照算、照成交。"""

    def _run(self, world, caplog):
        world.bars_end = PREV                    # Twelve Data 也还没有 10-06 那根
        world.closed(tickers=("TSLA", "JNJ", "SPY"))
        world.book()
        with caplog.at_level(logging.INFO):
            return pg.run_for_date(AS_OF, beta_fn=_beta1)

    def test_row_is_not_priced_and_nothing_trades(self, world, caplog):
        r = self._run(world, caplog)
        amzn = next(x for x in _rows(r, "stock") if x["ticker"] == "AMZN")
        assert amzn["price"] is None and amzn["dollar_delta"] is None, amzn
        assert r["aggregate"]["band_status"] == "unknown"          # 带外的组合，但缺一行就不知道
        assert r["executed"] is False and not pg.TRADES_FILE.exists()

    def test_stale_price_is_named_with_its_real_session(self, world, caplog):
        r = self._run(world, caplog)
        amzn = next(x for x in _rows(r, "stock") if x["ticker"] == "AMZN")
        assert amzn["price_stale"] is True and amzn["price_missing"] is True
        assert amzn["stale_price"] == CLOSE["AMZN"][0] and amzn["price_session"] == PREV
        cov = r["aggregate"]["coverage"]
        assert cov["n_price_stale"] == 1 and cov["n_price_missing"] == 1
        assert "1 stale" in r["recommendation"]["reason"]
        chk = r["price_check"]
        assert chk["n_stale"] == 1 and chk["stale"][0]["ticker"] == "AMZN" and chk["stale"][0]["session"] == PREV
        assert chk["n_unpriced"] == 0, "陈旧 ≠ 取不到价：看到了旧价的行不能再算一遍「取不到」（v0.45.435）"
        assert pg.price_check_stats()["n_stale"] == 1               # → scan_timing → status.json

    def test_error_log_names_ticker_and_session(self, world, caplog):
        self._run(world, caplog)
        errs = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR]
        assert any("AMZN" in m and PREV in m and AS_OF in m for m in errs), errs

    def test_report_names_the_stale_row(self, world, caplog):
        r = self._run(world, caplog)
        md = pg.render_markdown(AS_OF, result=r)
        assert f"价不属于 {AS_OF} 这一场" in md and f"AMZN {CLOSE['AMZN'][0]} @ {PREV}" in md
        assert "其中陈旧 1" in md

    def test_audit_file_carries_the_price_check(self, world, caplog):
        self._run(world, caplog)
        audit = json.loads((pg.STATE_DIR / f"greeks_{AS_OF}.json").read_text(encoding="utf-8"))
        assert audit["price_check"]["n_stale"] == 1 and audit["version"] == "0.45.423"


class TestCboeStaleIntradayFile:
    """收盘后 CDN 发的是 12:10 ET 生成的文件（v0.45.234 那类）：close = 中午的成交价。"""

    def _setup(self, world):
        world.closed(tickers=("TSLA", "SPY"))
        world.payloads["AMZN"] = _payload("AMZN", 254.00, f"{AS_OF}T12:10:05")
        world.payloads["JNJ"] = _payload("JNJ", 254.10, f"{AS_OF}T12:10:05", options=_jnj_options())
        world.book()

    def test_stock_row_is_stale_the_same_evening(self, world):
        """收盘后同日跑：CBOE 是中午的价、Twelve Data 当晚只有临时值（按日期丢）⇒ as_of 的官方收盘无处可取 ⇒
        陈旧、标红、不对冲。v0.45.423 曾在这里收下 Twelve Data 的当日那根（当晚是临时值，v0.45.438 撤回）。"""
        self._setup(world)
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        amzn = next(x for x in _rows(res, "stock") if x["ticker"] == "AMZN")
        assert amzn["price"] is None and amzn["price_stale"] is True
        assert amzn["stale_price"] == 254.00 and amzn["price_source"] == "cboe_stale_intraday"

    def test_after_midnight_the_settled_bar_prices_it_not_the_midday_price(self, world):
        self._setup(world)
        world.at(datetime(2026, 10, 7, 0, 30, tzinfo=ET))
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        amzn = next(x for x in _rows(res, "stock") if x["ticker"] == "AMZN")
        assert amzn["price"] == CLOSE["AMZN"][1] and amzn["price_source"] == "twelve_data_bar"

    def test_option_spot_pairs_with_its_own_payload(self, world):
        """同一份陈旧文件里的标的价与期权报价是同一时刻的——配对算 $Delta 要的正是它（标签照记）。"""
        self._setup(world)
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        opt = _rows(res, "option")
        assert [r["price"] for r in opt] == [254.10, 254.10]
        assert all(r["price_source"] == "cboe_stale_intraday" and not r["price_stale"] for r in opt)


class TestIntradayRunValuesLiveButDoesNotFill:
    """盘中手动跑（12:00 ET）：估值用此刻的实时价（同一时刻），但 SPY 不是收盘价 ⇒ 不许记成「收盘成交」。"""

    def _setup(self, world):
        world.at(datetime(2026, 10, 6, 12, 0, tzinfo=ET))
        world.intraday()
        world.book()

    def test_rows_use_the_live_price(self, world):
        self._setup(world)
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        assert {r["ticker"]: r["price"] for r in _rows(res, "stock")} == {
            "AMZN": round(CLOSE["AMZN"][1] * 0.999, 2), "TSLA": round(CLOSE["TSLA"][1] * 0.999, 2)}
        assert res["spy_mark"]["live"] is True and res["spy_mark"]["at_close"] is False

    def test_no_fill_at_a_live_price(self, world):
        self._setup(world)
        r = pg.run_for_date(AS_OF, beta_fn=_beta1)
        assert r["recommendation"]["action"] == "sell_spy"          # 建议照算
        assert r["executed"] is False and not pg.TRADES_FILE.exists()
        assert "官方收盘" in (r["execution_blocked"] or "") and r["price_check"]["execution_blocked"]


# ── 正对照：该接受的价照样接受（防修过头）──────────────────────────────────

class TestLateAndBackfillRuns:

    def test_after_midnight_et_run_accepts_the_official_close(self, world):
        """10-05 那次 02:56 ET（次日）的重跑：CBOE 是那一场的官方收盘，照收。"""
        world.at(datetime(2026, 10, 7, 2, 56, tzinfo=ET))
        world.closed()
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        assert {r["ticker"]: r["price"] for r in _rows(res, "stock")} == {
            "AMZN": CLOSE["AMZN"][1], "TSLA": CLOSE["TSLA"][1]}
        assert res["spy_price"] == CLOSE["SPY"][1]

    def test_after_midnight_falls_back_to_the_closed_twelve_data_bar(self, world):
        """CBOE 拿不到时，过了美东午夜 twelve_data 不再丢 10-06 那根——它已收完，可以兜底。"""
        world.at(datetime(2026, 10, 7, 2, 56, tzinfo=ET))
        world.closed(tickers=("JNJ", "SPY"))
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        amzn = next(r for r in _rows(res, "stock") if r["ticker"] == "AMZN")
        assert amzn["price"] == CLOSE["AMZN"][1]

    def test_next_morning_backfill_rejects_todays_quotes_but_prices_stocks(self, world):
        """次日 10:30 ET 补跑 10-06：CBOE 文件已是 10-07 的盘中链——报价与 S 都不是 10-06 的，
        期权行不进 Greeks；股票行退到 10-06 那根已收完的日线。"""
        world.at(datetime(2026, 10, 7, 10, 30, tzinfo=ET))
        world.bars_end = "2026-10-07"
        for tk in ("AMZN", "TSLA", "JNJ", "SPY"):
            world.payloads[tk] = _payload(tk, CLOSE[tk][1] + 1.0, "2026-10-07T10:15:00",
                                          current=CLOSE[tk][1] + 1.0,
                                          options=_jnj_options() if tk == "JNJ" else ())
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        assert {r["ticker"]: r["price"] for r in _rows(res, "stock")} == {
            "AMZN": CLOSE["AMZN"][1], "TSLA": CLOSE["TSLA"][1]}
        opt = _rows(res, "option")
        assert all(r["quote_missing"] and r.get("quote_stale") and r["vega_dollar_per_pt"] is None for r in opt)
        assert res["price_check"]["n_quote_stale"] == 2 and res["aggregate"]["band_status"] == "unknown"
        assert res["price_check"]["n_quote_missing"] == 0, "报价错场不能再算一遍「缺报价」（v0.45.435）"

    def test_weekend_as_of_uses_the_last_session(self, world):
        """as_of 是周六：那天没有行情，周五的收盘就是对的价，不能判陈旧。"""
        sat = "2026-10-10"
        world.at(datetime(2026, 10, 10, 11, 0, tzinfo=ET))
        world.bars_end = "2026-10-09"
        for tk in ("AMZN", "TSLA", "SPY"):
            fri = CLOSE[tk][1] + 2.0
            world.overrides[(tk, "2026-10-09")] = fri               # 两个来源同一个真相：正对照才可比
            world.payloads[tk] = _payload(tk, fri, "2026-10-09T15:59:59")
        world.book(as_of=sat, straddle=False)
        res = pg.compute_day(sat, beta_fn=_beta1)
        assert {r["ticker"]: r["price"] for r in _rows(res, "stock")} == {
            "AMZN": CLOSE["AMZN"][1] + 2.0, "TSLA": CLOSE["TSLA"][1] + 2.0}


class TestControlsAreLabelledAndClean:
    """正对照的价改动前后都对（上一组）；这一组核改动后新增的标注：来源 / 场次 / 零陈旧。"""

    def test_after_midnight_bar_fallback_is_labelled(self, world):
        world.at(datetime(2026, 10, 7, 2, 56, tzinfo=ET))
        world.closed(tickers=("JNJ", "SPY"))
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        amzn = next(r for r in _rows(res, "stock") if r["ticker"] == "AMZN")
        assert (amzn["price_source"], amzn["price_session"]) == ("twelve_data_bar", AS_OF)
        assert res["price_check"]["n_stale"] == 0 and res["spy_mark"]["at_close"] is True

    def test_weekend_expected_session_is_the_last_trading_day(self, world):
        sat = "2026-10-10"
        world.at(datetime(2026, 10, 10, 11, 0, tzinfo=ET))
        world.bars_end = "2026-10-09"
        for tk in ("AMZN", "TSLA", "SPY"):
            world.payloads[tk] = _payload(tk, CLOSE[tk][1] + 2.0, "2026-10-09T15:59:59")
        world.book(as_of=sat, straddle=False)
        chk = pg.compute_day(sat, beta_fn=_beta1)["price_check"]
        assert chk["expected_session"] == "2026-10-09" and chk["n_stale"] == 0 and chk["spy"]["at_close"] is True


class TestOneSpyPricePerRun:

    def test_spy_fetched_once_even_when_unavailable(self, world):
        """SPY 的价在建议 / 净值 / 盯市 / 交易后聚合里要是同一个数；CBOE 取不到时也别每处各重试一遍
        （每次 3 次重试 + 退避）。持有 SPY 仓 ⇒ 交易前行、合并 NAV、交易后行都会要它。
        前提是 SPY **真的**取不到：Twelve Data 有收完的 10-06 那根时第一次就取到了，记忆与否都只取一次
        （v0.45.423 变异 M10 就是这样活下来的）。"""
        world.bars_end = PREV                    # CBOE 与 Twelve Data 都给不出 SPY 10-06 的收盘
        world.closed(tickers=("AMZN", "TSLA", "JNJ"))
        world.book()
        _jsonl(pg.POSITIONS_FILE, [{"ticker": "SPY", "shares": -10.0, "avg_price": 770.0}])
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        assert world.fetches.count("SPY") == 1, world.fetches


class TestDefaultMarkContract:
    """生产默认取价只返回 mark dict（自带场次）。`_resolve_mark` 把裸数字当「调用方担保是 as_of 的价」，
    这条担保只许测试注入用——生产路径一旦退回裸数字，场次校验就形同虚设。"""

    KEYS = {"price", "source", "session", "at_close", "live"}

    @pytest.mark.parametrize("case", ["cboe_ok", "cboe_none_bars_prev", "nothing_at_all"])
    def test_always_a_mark_with_a_session_field(self, world, monkeypatch, case):
        if case == "cboe_ok":
            world.closed(tickers=("AMZN",))
        elif case == "cboe_none_bars_prev":
            world.bars_end = PREV
        elif case == "nothing_at_all":
            monkeypatch.setattr(td, "_fetch_rows", lambda *a, **k: None)
            import price_history
            monkeypatch.setattr(price_history, "load_price_history", lambda *a, **k: [])
        m = pg._default_mark("AMZN", AS_OF)
        assert isinstance(m, dict) and self.KEYS <= set(m), m
        if case == "cboe_ok":
            assert m["price"] == CLOSE["AMZN"][1] and m["session"] == AS_OF and m["at_close"] is True
        elif case == "cboe_none_bars_prev":
            assert m["session"] == PREV                     # 交出去的是它真实的场次，由调用方判陈旧
            assert pg._resolve_mark(m, AS_OF)["stale"] is True
        else:
            assert m["price"] is None and m["source"] == "unavailable"


# ── cboe_options：标的 mark 的判据（复用 close_verdict，不另起口径）────────────

class TestUnderlyingMark:
    P = staticmethod(lambda lt, close=100.0, cur=101.0: {"close": close, "current_price": cur, "last_trade_time": lt})

    def test_official_close_after_the_session(self):
        m = co.underlying_mark(self.P(f"{AS_OF}T15:59:58"), AFTER_CLOSE)
        assert (m["price"], m["source"], m["session"], m["at_close"], m["live"]) == (
            100.0, "cboe_close", AS_OF, True, False)

    def test_live_price_while_the_session_is_open(self):
        m = co.underlying_mark(self.P(f"{AS_OF}T11:59:30"), datetime(2026, 10, 6, 12, 0, tzinfo=ET))
        assert (m["price"], m["source"], m["live"], m["at_close"]) == (101.0, "cboe_intraday", True, False)

    def test_midday_file_after_close_is_neither_close_nor_live(self):
        m = co.underlying_mark(self.P(f"{AS_OF}T12:10:05"), AFTER_CLOSE)
        assert m["source"] == co.STALE_INTRADAY_SOURCE and m["session"] == AS_OF
        assert m["at_close"] is False and m["live"] is False and m["price"] == 100.0

    def test_no_last_trade_time_means_no_session(self):
        m = co.underlying_mark({"close": 100.0, "current_price": 101.0}, AFTER_CLOSE)
        assert m["session"] is None and m["source"] == "cboe_unverifiable" and m["at_close"] is False

    def test_early_close_day_uses_close_not_after_hours(self):
        """感恩节次日 13:00 收盘；14:00 ET 时按挂钟（09:30–16:00）算还在「盘中」，current_price 已是盘后价。
        按那一场的收盘时刻判 ⇒ 官方收盘、取 close。"""
        m = co.underlying_mark(self.P("2026-11-27T12:59:59"), datetime(2026, 11, 27, 14, 0, tzinfo=ET))
        assert m["at_close"] is True and m["price"] == 100.0 and m["session"] == "2026-11-27"

    @pytest.mark.parametrize("payload", [None, {}, {"close": 0, "last_trade_time": f"{AS_OF}T15:59:58"},
                                         {"close": float("inf"), "last_trade_time": f"{AS_OF}T15:59:58"}])
    def test_unusable_payload_is_unavailable(self, payload):
        m = co.underlying_mark(payload, AFTER_CLOSE)
        assert m["price"] is None and m["source"] == "unavailable" and not m["at_close"]


class TestQuoteContractsWithUnderlying:

    def test_quotes_and_spot_come_from_one_payload(self, world):
        world.closed(tickers=("JNJ",))
        quotes, und = co.quote_contracts_with_underlying("JNJ", [JNJ_CALL, JNJ_PUT])
        assert world.fetches == ["JNJ"]                                  # 一次取数，两样东西
        assert quotes[JNJ_PUT]["mid"] == pytest.approx(15.95) and und["price"] == CLOSE["JNJ"][1]
        assert co.quote_contracts("JNJ", [JNJ_CALL, JNJ_PUT]) == quotes  # 旧入口逐字不变

    def test_unavailable_payload_and_snapshot_mode(self, world, monkeypatch):
        quotes, und = co.quote_contracts_with_underlying("JNJ", [JNJ_PUT])
        assert quotes == {JNJ_PUT: None} and und["source"] == "unavailable"
        monkeypatch.setattr(co, "_SNAPSHOT_PROVIDER", lambda tk: {})
        quotes, und = co.quote_contracts_with_underlying("JNJ", [JNJ_PUT])
        assert quotes == {JNJ_PUT: None} and und["price"] is None and world.fetches == ["JNJ"]


# ── 观测链：price_check → scan_timing → status.json → alert_manager ─────────────

def _stale_check(world, caplog=None):
    world.bars_end = PREV
    world.closed(tickers=("TSLA", "JNJ", "SPY"))
    world.book()
    pg.run_for_date(AS_OF, beta_fn=_beta1)
    return pg.price_check_stats()


class TestScanTimingCarriesThePriceCheck:

    def test_counters_expose_the_last_check(self, world):
        import scan_timing as stt
        chk = _stale_check(world)
        assert stt.counters()["portfolio_greeks"] == chk and chk["n_stale"] == 1

    def test_summary_line_names_stale_rows_only_when_there_are_some(self, world):
        import scan_timing as stt
        chk = _stale_check(world)
        line = stt.summary_line({"phases": {}, "counters": {"portfolio_greeks": chk}})
        assert "Greeks 陈旧价 1 行" in line and f"AMZN@{PREV}" in line
        clean = dict(chk, n_stale=0, stale=[], n_quote_stale=0, spy={"stale": False}, execution_blocked=None,
                     hedge_undecided=None, gaps=[])
        assert "Greeks" not in stt.summary_line({"phases": {}, "counters": {"portfolio_greeks": clean}})


class TestAlertManagerSeesStalePrices:

    @staticmethod
    def _analyze(tmp_path, counters):
        import alert_manager as am
        status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"counters": counters, "extra": {"gh_pages": {"success": True}},
                                  "production_sync": {"outcome": "up_to_date"}}}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a.analyze(p)
        return a, [x for x in a.alerts if "组合 Greeks" in x.message]

    def test_stale_rows_raise_p2(self, world, tmp_path):
        import alert_manager as am
        a, hits = self._analyze(tmp_path, {"portfolio_greeks": _stale_check(world)})
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.MEDIUM, [x.message for x in a.alerts]
        assert f"AMZN {CLOSE['AMZN'][0]}@{PREV}" in hits[0].details["陈旧行"]

    def test_clean_day_is_silent(self, world, tmp_path):
        world.closed()
        world.book()
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        _, hits = self._analyze(tmp_path, {"portfolio_greeks": pg.price_check_stats()})
        assert hits == []

    def test_missing_counter_is_a_skipped_check_not_a_clean_one(self, tmp_path):
        a, hits = self._analyze(tmp_path, {"portfolio_greeks": None})
        assert hits == [] and any("组合 Greeks" in s for s in a.checks_skipped), a.checks_skipped


# ── v0.45.435：不是陈旧、是缺——对冲决定做不出来也要红 ───────────────────────────
# 生产 2026-09-15~25 连续 7 天 band unknown、零告警：09-15~22 BRK-B 的 β 取不到（Twelve Data 代码映射），
# 09-24/25 SPY 价与全部 8 张期权报价取不到。v0.45.423 只让「陈旧」会红，这几种「缺」照旧静默。

def _td_down_for(monkeypatch, *tickers):
    """Twelve Data 对这几只什么都不给；其余照旧（CBOE 由 world.payloads 决定给不给）。"""
    orig, down = td._fetch_rows, {t.upper() for t in tickers}
    monkeypatch.setattr(td, "_fetch_rows", lambda ticker, days, end_date=None:
                        None if ticker.upper() in down else orig(ticker, days, end_date))


def _outage_like_09_24(world, monkeypatch):
    """09-24 的形状：期权报价一张都取不到、SPY 两源都取不到，覆盖账本持有 SPY。股票 / JNJ 的标的价给 CBOE 收盘
    （09-24 那天股票价来自本地价格索引，按现在的口径会判陈旧——这里单测「缺」那条路，不混进陈旧）。"""
    _td_down_for(monkeypatch, "SPY")
    world.closed(tickers=("AMZN", "TSLA", "JNJ"))
    world.payloads["JNJ"]["options"] = []                    # 链里没有持仓合约 ⇒ 报价缺（不是错场）
    world.book()
    _jsonl(pg.POSITIONS_FILE, [{"ticker": "SPY", "shares": -10, "avg_price": 770.0}])
    pg.META_FILE.write_text(json.dumps({"cash": 7700.0}), encoding="utf-8")
    return pg.compute_day(AS_OF, beta_fn=_beta1)


def _beta_missing_for(*tickers):
    return lambda tk, as_of: (None, None) if tk in tickers else (1.0, "ols60")


class TestIncompleteDataIsNotSilent:

    def test_total_outage_is_counted_as_missing_not_stale(self, world, monkeypatch):
        res = _outage_like_09_24(world, monkeypatch)
        chk = res["price_check"]
        assert res["aggregate"]["band_status"] == "unknown"
        assert chk["n_stale"] == 0 and chk["n_quote_stale"] == 0, chk           # 没看到价 ≠ 看到旧价
        assert [u["ticker"] for u in chk["unpriced"]] == ["SPY"] and chk["n_unpriced"] == 1, chk
        assert sorted(chk["quote_missing"]) == sorted([JNJ_CALL, JNJ_PUT]), chk
        assert chk["nav_missing"] == ["hedge_overlay"], chk
        assert "partial data" in (chk["hedge_undecided"] or ""), chk

    def test_total_outage_logs_an_error_naming_the_gaps(self, world, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            _outage_like_09_24(world, monkeypatch)
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("数据不全" in m and "取不到价 1 行（SPY）" in m and "缺报价 2 张" in m for m in msgs), msgs

    def test_missing_beta_alone_is_named(self, world):
        """BRK-B 那 5 天：价、报价都在，只有一只的 β 取不到——整本账不对冲。"""
        world.closed()
        world.book()
        chk = pg.compute_day(AS_OF, beta_fn=_beta_missing_for("TSLA"))["price_check"]
        assert chk["beta_missing"] == ["TSLA"] and chk["n_beta_missing"] == 1, chk
        assert chk["n_unpriced"] == 0 and chk["n_stale"] == 0, chk
        assert "1 beta missing" in (chk["hedge_undecided"] or ""), chk
        assert any("缺 β 1 行（TSLA）" in g for g in chk["gaps"]), chk["gaps"]

    def test_out_of_band_without_a_spy_price_is_undecided(self, world, monkeypatch):
        """覆盖账本没仓（NAV 照样算得出）、组合出带，SPY 两源都取不到 ⇒ 判出了带外却下不了单。"""
        _td_down_for(monkeypatch, "SPY")
        world.closed(tickers=("AMZN", "TSLA", "JNJ"))
        world.book()
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        assert res["aggregate"]["band_status"] == "above", res["aggregate"]["band_status"]
        assert res["recommendation"]["action"] == "hold"
        assert "SPY price unavailable" in (res["price_check"]["hedge_undecided"] or ""), res["price_check"]

    def test_a_decided_day_is_not_undecided(self, world):
        world.closed()
        world.book()
        chk = pg.compute_day(AS_OF, beta_fn=_beta1)["price_check"]
        assert chk["hedge_undecided"] is None and chk["gaps"] == [], chk
        assert (chk["n_unpriced"], chk["n_quote_missing"], chk["n_beta_missing"], chk["nav_missing"]) == (0, 0, 0, [])

    def test_alert_manager_raises_p2_for_missing_data(self, world, monkeypatch, tmp_path):
        import alert_manager as am
        _outage_like_09_24(world, monkeypatch)
        a, hits = TestAlertManagerSeesStalePrices._analyze(tmp_path, {"portfolio_greeks": pg.price_check_stats()})
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.MEDIUM, [x.message for x in a.alerts]
        assert "数据不全" in hits[0].message and "取不到价 1 行（SPY）" in hits[0].details["缺口"], hits[0].message

    def test_alert_manager_names_the_missing_beta(self, world, tmp_path):
        world.closed()
        world.book()
        pg.compute_day(AS_OF, beta_fn=_beta_missing_for("TSLA"))
        _, hits = TestAlertManagerSeesStalePrices._analyze(tmp_path, {"portfolio_greeks": pg.price_check_stats()})
        assert len(hits) == 1 and "缺 β 1 行（TSLA）" in hits[0].details["缺口"], [h.message for h in hits]

    def test_stale_alert_also_carries_the_other_gaps(self, world, tmp_path):
        """陈旧 + 缺 β 同一天：一条告警（不重复报），缺口挂在「另缺」里。"""
        world.bars_end = PREV
        world.closed(tickers=("TSLA", "JNJ", "SPY"))
        world.book()
        pg.compute_day(AS_OF, beta_fn=_beta_missing_for("TSLA"))
        _, hits = TestAlertManagerSeesStalePrices._analyze(tmp_path, {"portfolio_greeks": pg.price_check_stats()})
        assert len(hits) == 1 and "陈旧" in hits[0].message, [h.message for h in hits]
        assert "缺 β 1 行（TSLA）" in hits[0].details["另缺"], hits[0].details

    def test_old_scan_counters_without_the_key_stay_quiet(self, tmp_path):
        """v0.45.423~434 的计数没有 hedge_undecided：不猜、不报。"""
        old = {"as_of": AS_OF, "n_stale": 0, "n_quote_stale": 0, "spy": {"stale": False}, "execution_blocked": None}
        _, hits = TestAlertManagerSeesStalePrices._analyze(tmp_path, {"portfolio_greeks": old})
        assert hits == []

    def test_summary_line_says_no_hedge_for_missing_data(self, world, monkeypatch):
        import scan_timing as stt
        _outage_like_09_24(world, monkeypatch)
        line = stt.summary_line({"phases": {}, "counters": {"portfolio_greeks": pg.price_check_stats()}})
        assert "Greeks 数据不全不对冲" in line and "取不到价 1 行（SPY）" in line, line


class TestFormingBarNeverPassesAsAClose:
    """不靠 twelve_data 那道闸碰巧丢掉当日那根：它判不了美东日期时（`_et_today` → None）会把盘中半根
    原样放进来——这时 as_of 那根日期对、来源也是 Twelve Data，但那一场还没收，不能当收盘。"""

    def test_intraday_bar_dated_as_of_is_not_priced_as_a_close(self, world, monkeypatch):
        world.at(datetime(2026, 10, 6, 12, 0, tzinfo=ET))
        monkeypatch.setattr(td, "_et_today", lambda: None)          # 上游闸失明
        world.overrides[("AMZN", AS_OF)] = 253.33                    # 盘中半根：此刻的成交价，不是收盘
        world.intraday(tickers=("TSLA", "JNJ", "SPY"))               # AMZN 的 CBOE 拿不到，只剩日线
        world.book()
        last = td.fetch_bars("AMZN", 120, end_date=AS_OF)[-1]
        assert (last["date"], last["close"]) == (AS_OF, 253.33)      # 半根真的漏进来了
        res = pg.compute_day(AS_OF, beta_fn=_beta1)
        amzn = next(r for r in _rows(res, "stock") if r["ticker"] == "AMZN")
        assert amzn["price"] is None, f"盘中半根被当成 {AS_OF} 的收盘用了：{amzn['price']}"


# ── v0.45.440：账本历史只经一道闸读——记录自带定价场次证明，证不出的不放行 ──────────────
# 2026-09-04~10-07 的记录（标的价 / SPY 成交价晚一个交易日）原样保留、不重算（用户决定）；它们自己不说这件事，
# 按日期切要靠读者记得一个约定。`load_history()` 按每条记录自带的证明放行（直读的守卫见 test_hedge_history_gate.py）。

_OLD_TRADE = {"date": "2026-10-01", "ticker": "SPY", "shares": 30, "price": 762.63, "action": "buy_spy",
              "fill": "close", "nav": 152932.15, "shares_after": 48.0}               # 生产 v0.45.423 之前的格式
_OLD_EQUITY = {"date": "2026-10-07", "spy_shares": 48.0, "spy_price": 779.09, "cash": -36630.13,
               "market_value": 37396.32, "nav": 766.19, "trades_today": 0, "band_status": "inside"}


def _eq(day, *, session=None, at_close=True, shares=48.0, source="cboe_close", nav=518.51, legacy=False):
    e = {"date": day, "spy_shares": shares, "spy_price": 773.93, "spy_price_source": source,
         "spy_price_session": day if session is None else session, "cash": -36630.13, "nav": nav}
    if not legacy:
        e["spy_price_at_close"] = at_close
    return e


class TestRecordsCarryTheirPricingProof:

    def test_trade_and_equity_rows_say_which_close_they_used(self, world):
        world.closed()
        world.book()
        t = pg.run_for_date(AS_OF, beta_fn=_beta1)["executed_trade"]
        assert (t["price_session"], t["price_source"], t["price_at_close"]) == (AS_OF, "cboe_close", True), t
        eq = pg._load_jsonl(pg.EQUITY_FILE)[-1]
        assert (eq["spy_price_session"], eq["spy_price_at_close"]) == (AS_OF, True), eq

    def test_intraday_mark_is_recorded_as_not_a_close(self, world):
        world.intraday()
        world.book()
        _jsonl(pg.POSITIONS_FILE, [{"ticker": "SPY", "shares": -10, "avg_price": 770.0}])
        pg.META_FILE.write_text(json.dumps({"cash": 7700.0}), encoding="utf-8")
        world.at(datetime(2026, 10, 6, 12, 0, tzinfo=ET))
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        eq = pg._load_jsonl(pg.EQUITY_FILE)[-1]
        assert eq["spy_price_at_close"] is False and eq["spy_price_source"] == "cboe_intraday", eq


class TestHistoryGate:

    def test_end_to_end_keeps_this_run_and_drops_the_old_format(self, world):
        _jsonl(pg.TRADES_FILE, [_OLD_TRADE])
        _jsonl(pg.EQUITY_FILE, [dict(_OLD_EQUITY, date="2026-10-05")])
        world.closed()
        world.book()
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        h = pg.load_history()
        assert [t["date"] for t in h["trades"]] == [AS_OF] and [e["date"] for e in h["equity"]] == [AS_OF], h
        assert sorted(h["audits"]) == [AS_OF]
        assert h["excluded"]["trades"] == {"no_provenance": 1} and h["excluded"]["equity"] == {"no_provenance": 1}
        assert h["first_verified"] == {"trades": AS_OF, "equity": AS_OF, "audits": AS_OF}
        assert h["regressions"] == []

    def test_proof_that_says_wrong_session_or_not_close_is_excluded(self, tmp_path):
        _jsonl(tmp_path / "trades.jsonl", [dict(_OLD_TRADE, date="2026-10-08", price_session="2026-10-07",
                                                price_source="twelve_data_bar", price_at_close=True)])
        _jsonl(tmp_path / "equity_curve.jsonl", [_eq("2026-10-08", session="2026-10-07"),
                                                 _eq("2026-10-09", at_close=False, source="cboe_intraday"),
                                                 _eq("2026-10-12", nav=None)])
        h = pg.load_history(tmp_path)
        assert h["trades"] == [] and h["excluded"]["trades"] == {"not_its_session_close": 1}
        # 盘中实时价那行自 v0.45.442 起单列 intraday_run（更具体），不再混在 not_its_session_close 里
        assert h["equity"] == [] and h["excluded"]["equity"] == {"not_its_session_close": 1, "intraday_run": 1,
                                                                 "unpriced": 1}

    def test_weekend_row_proves_the_last_session(self, tmp_path):
        """周六的 as_of：那天没有行情，周五的收盘就是对的（`_expected_session`），不能因 session ≠ date 拒掉。"""
        _jsonl(tmp_path / "equity_curve.jsonl", [_eq("2026-10-10", session="2026-10-09")])
        assert [e["date"] for e in pg.load_history(tmp_path)["equity"]] == ["2026-10-10"]

    def test_no_spy_held_needs_no_spy_close(self, tmp_path):
        _jsonl(tmp_path / "equity_curve.jsonl", [_eq("2026-10-09", shares=0.0, at_close=False, source="unavailable",
                                                     session=None, nav=12.5)])
        assert len(pg.load_history(tmp_path)["equity"]) == 1

    def test_423_rows_without_the_at_close_field_judge_by_source(self, tmp_path):
        _jsonl(tmp_path / "equity_curve.jsonl", [_eq("2026-10-08", legacy=True),
                                                 _eq("2026-10-09", legacy=True, source="cboe_intraday")])
        h = pg.load_history(tmp_path)
        assert [e["date"] for e in h["equity"]] == ["2026-10-08"], h["excluded"]

    def test_unproven_record_after_proof_began_is_a_regression(self, tmp_path, caplog):
        _jsonl(tmp_path / "equity_curve.jsonl", [_eq("2026-10-08"), dict(_OLD_EQUITY, date="2026-10-09")])
        with caplog.at_level(logging.INFO):
            h = pg.load_history(tmp_path)
        assert h["regressions"] == [("equity", "2026-10-09")]
        assert any(r.levelno >= logging.ERROR and "无定价证明的新记录" in r.getMessage() for r in caplog.records)

    def test_proof_before_measurement_start_is_flagged(self, tmp_path, caplog):
        _jsonl(tmp_path / "equity_curve.jsonl", [_eq("2026-09-30")])
        with caplog.at_level(logging.INFO):
            h = pg.load_history(tmp_path)
        assert h["before_measurement_start"] == [("equity", "2026-09-30")]
        assert any(r.levelno == logging.WARNING and "MEASUREMENT_START" in r.getMessage() for r in caplog.records)

    def test_audit_files_without_price_check_or_unreadable_are_excluded(self, tmp_path):
        (tmp_path / "greeks_2026-10-07.json").write_text(json.dumps({"as_of": "2026-10-07"}), encoding="utf-8")
        (tmp_path / "greeks_2026-10-08.json").write_text("{坏", encoding="utf-8")
        (tmp_path / "greeks_2026-10-09.json").write_text(
            json.dumps({"as_of": "2026-10-09", "price_check": {"as_of": "2026-10-08"}}), encoding="utf-8")
        (tmp_path / "greeks_2026-10-12.json").write_text(
            json.dumps({"as_of": "2026-10-12", "price_check": {"as_of": "2026-10-12"}}), encoding="utf-8")
        h = pg.load_history(tmp_path)
        assert sorted(h["audits"]) == ["2026-10-12"]
        assert h["excluded"]["audits"] == {"no_provenance": 1, "unreadable": 1, "date_mismatch": 1}

    def test_measurement_start_is_the_first_day_v0_45_423_ran(self):
        """记载值与 CHANGELOG / 生产核对一致（10-08 首跑；`load_history` 对生产推出的 first_verified 也是它）。"""
        assert pg.MEASUREMENT_START == "2026-10-08"


class TestHistoryGateSurvivesBadLines:
    """v0.45.442 二次检查：闸读到坏行要「排除并计数」，不能崩、也不能静默跳过（此前 `"str"` / `[1, 2]` 一行就
    AttributeError；坏 JSON 被 `_load_jsonl` 无声丢掉）；没日期的记录不能放行（`_expected_session("")` 回 ""，
    会与空场次「相等」，并把 first_verified 推成 ""）。"""

    def test_malformed_lines_are_counted_by_reason(self, tmp_path):
        (tmp_path / "equity_curve.jsonl").write_text(
            "\n".join([json.dumps(_eq("2026-10-08")), "[1, 2]", "42", "{坏", json.dumps({"spy_price_session": "x"})]) + "\n",
            encoding="utf-8")
        (tmp_path / "trades.jsonl").write_text('"str"\n', encoding="utf-8")
        (tmp_path / "greeks_2026-10-08.json").write_text("[]", encoding="utf-8")
        h = pg.load_history(tmp_path)
        assert [e["date"] for e in h["equity"]] == ["2026-10-08"]
        assert h["excluded"]["equity"] == {"not_an_object": 2, "unreadable": 1, "no_date": 1}
        assert h["excluded"]["trades"] == {"not_an_object": 1} and h["excluded"]["audits"] == {"not_an_object": 1}

    def test_dateless_record_cannot_pass_or_move_the_start(self, tmp_path):
        _jsonl(tmp_path / "trades.jsonl", [{"price_session": "", "price_source": "cboe_close", "price_at_close": True},
                                           dict(_OLD_TRADE, date="2026-10-09", price_session="2026-10-09",
                                                price_source="cboe_close", price_at_close=True)])
        h = pg.load_history(tmp_path)
        assert [t["date"] for t in h["trades"]] == ["2026-10-09"] and h["excluded"]["trades"] == {"no_date": 1}
        assert h["first_verified"]["trades"] == "2026-10-09"


class TestRunFailureIsRed:
    """v0.45.442（独立审阅）：`run_for_date` 抛异常时日报只记一行 WARNING（非致命），`_LAST_PRICE_CHECK` 又只在
    compute_day 末尾赋值 ⇒ status.json 这一项是 None ⇒ alert_manager 只进 checks_skipped——覆盖层整轮没跑也不红。"""

    def test_corrupt_ledger_line_makes_the_run_fail_loudly(self, world, tmp_path):
        import alert_manager as am
        import scan_timing as stt
        world.closed()
        world.book()
        pg.TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        pg.TRADES_FILE.write_text('"不是对象"\n', encoding="utf-8")
        import ledger_io
        # v0.45.448 起读取严格：坏行在读的那一刻就抛 LedgerCorrupt（带文件与行号），不再等到下游 `.get` 炸 AttributeError
        with pytest.raises(ledger_io.LedgerCorrupt, match="trades.jsonl 第 1 行不是对象"):
            pg.run_for_date(AS_OF, beta_fn=_beta1)
        chk = pg.price_check_stats()
        assert chk["as_of"] == AS_OF and "LedgerCorrupt" in chk["error"], chk
        _, hits = TestAlertManagerSeesStalePrices._analyze(tmp_path, {"portfolio_greeks": chk})
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.MEDIUM and "异常中断" in hits[0].message
        assert "Greeks 异常中断" in stt.summary_line({"phases": {}, "counters": {"portfolio_greeks": chk}})

    def test_failure_after_compute_keeps_the_price_check(self, world, monkeypatch):
        world.closed()
        world.book()
        monkeypatch.setattr(pg, "_execute_trade", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            pg.run_for_date(AS_OF, beta_fn=_beta1)
        chk = pg.price_check_stats()
        assert chk["error"] == "OSError: disk full" and chk["n_stale"] == 0 and chk["n_rows"] > 0, chk


class TestIntradayRecordsAreNotCloses:
    """v0.45.442（独立审阅）：盘中手动跑写下的审计文件 / 不持 SPY 的净值行曾被闸当成「该场收盘」放行。"""

    def test_intraday_run_is_excluded_from_history(self, world):
        world.intraday()
        world.book()
        world.at(datetime(2026, 10, 6, 12, 0, tzinfo=ET))
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        h = pg.load_history()
        assert h["equity"] == [] and h["audits"] == {}, (h["equity"], sorted(h["audits"]))
        assert h["excluded"]["equity"] == {"intraday_run": 1} and h["excluded"]["audits"] == {"intraday_run": 1}

    def test_after_close_run_on_the_same_day_replaces_it_and_passes(self, world):
        world.intraday()
        world.book()
        world.at(datetime(2026, 10, 6, 12, 0, tzinfo=ET))
        pg.run_for_date(AS_OF, beta_fn=_beta1, execute=False)
        world.closed()
        world.at(AFTER_CLOSE)
        pg.run_for_date(AS_OF, beta_fn=_beta1)
        h = pg.load_history()
        assert [e["date"] for e in h["equity"]] == [AS_OF] and sorted(h["audits"]) == [AS_OF]


class TestUndecidedMeansCouldNotCompute:

    def test_center_mode_rounding_to_zero_shares_is_a_decision(self):
        """band 出带、center 模式股数四舍五入成 0：目标算出来了，只是不用动——不是「数据不全」（v0.45.442）。"""
        rec = {"band_status": "above", "action": "hold", "spy_shares": 0, "target_usd": 0.0,
               "reason": "β·Δ +15.10% NAV above band → rebalance to center (+0% = $0); excess $10 / SPY $773.93 = +0 sh"}
        assert pg._hedge_undecided(rec) is None

    def test_out_of_band_without_a_target_is_undecided(self):
        rec = {"band_status": "below", "action": "hold", "target_usd": None,
               "reason": "band below but SPY price unavailable"}
        assert pg._hedge_undecided(rec) == "band below but SPY price unavailable"
