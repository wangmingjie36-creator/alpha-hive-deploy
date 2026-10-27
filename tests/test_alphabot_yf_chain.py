"""Alpha Bot 的 yfinance 备用取数源（v0.45.603，`alphabot/yf_chain.py` + `service._chain_fetch`）。

起因：NVDL 的 CBOE 文件盘中整个上午停在昨天（`stale_vintage`），用户要求换 yfinance。每组回答一句「谁会红」：
  · 适配器吐出的合约与 CBOE 的 `fetch_cboe_raw_contracts` **同形**（键集对拍），水平引擎吃得下（`compute_live_detail` 端到端）；
  · 陈旧照样拒（yfinance 的最新成交也是昨天 ⇒ `stale_vintage`），不拿旧数据冒充盘中；Yahoo 的 IV 求解失败值不当观测；
  · 只在「CBOE 陈旧 ∧ 票在名单里」时才走 yfinance——CBOE 好的时候、名单外的票、注入的取数函数都不受影响；失败带原因、计数。
全部离线（假 yfinance 模块；时钟全部注入）。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import cboe_options as CB
import sell_strike_report as R
from alphabot import service as SV
from alphabot import yf_chain as Y

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=ET)          # 周四，常规交易时段
TRADE_TODAY = pd.Timestamp("2026-10-22 14:52:30", tz="UTC")      # = 10:52:30 ET
TRADE_YESTERDAY = pd.Timestamp("2026-10-21 19:59:00", tz="UTC")  # = 15:59 ET
PLACEHOLDER_IV = 0.062509                                         # Yahoo 求解失败留下的「减半阶梯」值（yahoo_iv 的 docstring）


def _frame(expiry, cp, last_trade, *, strikes=(28, 30, 32, 34, 36)):
    ymd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    rows = []
    for k in strikes:
        rows.append({"contractSymbol": f"NVDL{ymd}{cp}{int(k * 1000):08d}", "lastTradeDate": last_trade, "strike": float(k),
                     "bid": 1.0, "ask": 1.2, "volume": 5.0, "openInterest": 120.0, "impliedVolatility": 0.85})
    return pd.DataFrame(rows)


class FakeYF:
    """`yfinance` 的最小替身：`Ticker(t).options / .history / .option_chain`。"""

    def __init__(self, *, expiries=("2026-10-23", "2026-11-06", "2026-11-20"), last_trade=TRADE_TODAY, price=31.66,
                 mutate=None, boom=None):
        self.expiries, self.last_trade, self.price, self.mutate, self.boom = list(expiries), last_trade, price, mutate, boom
        self.calls = []

    def Ticker(self, t):                               # noqa: N802 - 与 yfinance 同名
        self.calls.append(("Ticker", t))
        return self

    @property
    def options(self):
        if self.boom:
            raise self.boom
        return tuple(self.expiries)

    def history(self, period, interval):
        if self.price is None:
            return pd.DataFrame({"Close": []})
        return pd.DataFrame({"Close": [self.price - 0.1, self.price]})

    def option_chain(self, e):
        self.calls.append(("chain", e))
        calls, puts = _frame(e, "C", self.last_trade), _frame(e, "P", self.last_trade)
        if self.mutate:
            calls, puts = self.mutate(e, calls, puts)
        return type("Chain", (), {"calls": calls, "puts": puts})()


def _fetch(fake, **kw):
    return Y.fetch_yf_raw_contracts("NVDL", now_et=kw.pop("now_et", NOW), yf_module=fake, **kw)


class TestAdapter:
    def test_contracts_are_parsed_by_hand(self):
        raw, why = _fetch(FakeYF())
        assert why is None and raw["underlying_price"] == pytest.approx(31.66) and raw["underlying_price_source"] == "yfinance_intraday"
        assert raw["vintage_date"] == "2026-10-22" and raw["as_of"] == "2026-10-22" and raw["session_live"] is True
        assert raw["payload_last_trade_time"] == "2026-10-22T10:52:30" and raw["data_source"] == "yfinance"
        cs = raw["contracts"]
        assert len(cs) == 3 * 2 * 5
        c = next(x for x in cs if x["symbol"] == "NVDL261106C00030000")
        assert (c["expiry"], c["cp"], c["strike"], c["dte"], c["iv"], c["oi"], c["bid"], c["ask"], c["volume"]) == \
            ("2026-11-06", "C", 30.0, 15, 0.85, 120.0, 1.0, 1.2, 5.0)
        assert c["last_trade_time"] == "2026-10-22T10:52:30"
        assert all(c[k] is None for k in ("delta", "gamma", "vega", "theta", "theo")), "yfinance 没有 Greeks，不编"

    def test_same_contract_shape_as_the_cboe_fetcher(self, monkeypatch):
        payload = {"last_trade_time": "2026-10-22T10:50:00", "current_price": 100.0, "close": 99.0, "iv30": 30.0,
                   "options": [{"option": "XYZ261106C00100000", "bid": 1.0, "ask": 1.2, "iv": 0.3, "delta": 0.5, "gamma": 0.05,
                                "vega": 0.1, "theta": -0.1, "theo": 1.1, "open_interest": 10, "volume": 3,
                                "last_trade_time": "2026-10-22T10:50:00"}]}
        monkeypatch.setattr(CB, "_fetch_cboe_payload", lambda *a, **k: payload)
        monkeypatch.setattr(CB, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(CB, "official_price", lambda d, now_et=None: (100.0, "cboe_intraday"))
        cboe, why = CB.fetch_cboe_raw_contracts("XYZ", as_of=None, now_et=NOW)
        assert why is None
        yf, _ = _fetch(FakeYF())
        assert set(yf["contracts"][0]) == set(cboe["contracts"][0])
        assert set(cboe) <= set(yf) | {"x"}, set(cboe) - set(yf)

    def test_yahoo_solver_failure_values_are_not_observations(self):
        def mutate(e, calls, puts):
            if e == "2026-11-06":
                calls.loc[0, "impliedVolatility"] = PLACEHOLDER_IV
                calls.loc[1, "impliedVolatility"] = 1e-05
                calls.loc[2, "impliedVolatility"] = float("nan")
                calls.loc[3, "impliedVolatility"] = 0.0
            return calls, puts
        raw, _ = _fetch(FakeYF(mutate=mutate))           # 三个到期日：单个到期日的链现在会被 OI 判据判「判不了」
        ivs = [c["iv"] for c in raw["contracts"] if c["cp"] == "C" and c["expiry"] == "2026-11-06"]
        assert ivs[:4] == [None, None, None, None] and ivs[4] == 0.85

    def test_nan_open_interest_and_bad_rows(self):
        def mutate(e, calls, puts):
            if e == "2026-11-06":                                   # 只动一个到期日；链整体仍够判 OI（≥20 个近期成交合约）
                calls.loc[0, "openInterest"] = float("nan")
                calls.loc[1, "strike"] = float("nan")
            return calls, puts
        raw, why = _fetch(FakeYF(mutate=mutate))
        assert why is None
        calls = [c for c in raw["contracts"] if c["cp"] == "C" and c["expiry"] == "2026-11-06"]
        assert len(calls) == 4 and calls[0]["oi"] == 0.0            # 坏行丢弃、缺 OI 记 0（CBOE 同约定）

    @pytest.mark.parametrize("kw,reason", [
        ({"last_trade": TRADE_YESTERDAY}, "stale_vintage"),         # yfinance 也是昨天 ⇒ 照样拒，不冒充盘中
        ({"expiries": []}, "yf_no_expiries"),
        ({"price": None}, "yf_price_unavailable"),
        ({"boom": TimeoutError("x")}, "yf_exception:TimeoutError"),
    ])
    def test_every_failure_has_a_named_reason(self, kw, reason):
        raw, why = _fetch(FakeYF(**kw))
        assert raw is None and why == reason

    def test_far_expiries_are_not_fetched(self):
        fake = FakeYF(expiries=["2026-11-06", "2027-06-18", "2026-10-01"])
        _fetch(fake)
        assert [c for c in fake.calls if c[0] == "chain"] == [("chain", "2026-11-06")], "超过 120 天 / 已过期的到期日不拉"

    def test_expiring_today_is_dropped_after_the_close(self):
        after = datetime(2026, 10, 22, 17, 5, tzinfo=ET)
        raw, why = _fetch(FakeYF(expiries=["2026-10-22", "2026-11-06"]), now_et=after)
        assert why is None and raw["session_live"] is False and {c["expiry"] for c in raw["contracts"]} == {"2026-11-06"}

    def test_as_of_mismatch_is_refused(self):
        assert _fetch(FakeYF(), as_of="2026-10-21") == (None, "vintage_mismatch")

    def test_the_levels_engine_takes_it_end_to_end(self):
        raw, _ = _fetch(FakeYF())
        d = R.compute_live_detail("NVDL", fetch_fn=lambda t, as_of=None: (raw, None))
        assert d["data_available"] is True and d["underlying_price_source"] == "yfinance_intraday"
        assert {tv["status"] for tv in d["tenors"].values()} == {"recorded"}


class TestOiGuard:
    """BILI 加进备用名单时补的护栏（v0.45.662）：Yahoo 的 OI 不是这一场的 ⇒ 整份链拒收，不拿 OI=0 照用。

    起因：2026-10-12 Yahoo 全市场 OI 清零，BILI 当天 put/call 被算成 298.46（`oi_staleness`）。水平引擎吃 OI，
    OI 成片为 0 时它不报错、只会画一张看着正常的错图——所以每条用例都配「OI 正常的同一夹具照常通过」的对照。
    """

    @staticmethod
    def _zero_oi(only=None):
        def mutate(e, calls, puts):
            if only is None or e == only:
                calls["openInterest"] = 0.0
                puts["openInterest"] = 0.0
            return calls, puts
        return mutate

    def test_a_healthy_chain_passes_and_reports_its_check(self):
        raw, why = _fetch(FakeYF())
        assert why is None
        chk = raw["oi_check"]
        assert chk["judgeable"] is True and chk["stale"] is False and chk["ratio"] == 1.0 and chk["recent_contracts"] == 30

    def test_chain_with_zeroed_oi_is_refused_not_used_as_zero(self):
        """OI 全 0（Yahoo 没发布这一场）⇒ `yf_oi_stale`。变红的变异：删掉 OI 判据 / 把 stale 当 False。"""
        raw, why = _fetch(FakeYF(mutate=self._zero_oi()))
        assert raw is None and why == "yf_oi_stale"

    def test_a_chain_too_thin_to_judge_is_refused_not_waved_through(self):
        """近期成交合约 < `oi_staleness.MIN_RECENT` ⇒ 判不了 ⇒ 备用源不放行（页面写原因）。单个到期日只有 10 个合约。"""
        raw, why = _fetch(FakeYF(expiries=["2026-11-06"]))
        assert raw is None and why == "yf_oi_unjudgeable"

    def test_the_judge_pools_all_expiries(self):
        """只有一个到期日 OI 为 0（10-19 SNOW 那种个别到期日发布不全）不算整链陈旧——口径与全仓共用判据一致。"""
        raw, why = _fetch(FakeYF(mutate=self._zero_oi(only="2026-11-20")))
        assert why is None and raw["oi_check"]["stale"] is False and raw["oi_check"]["ratio"] == pytest.approx(2 / 3, abs=1e-3)

    def test_zeroed_oi_after_the_close_is_still_refused(self):
        """收盘后 DTE=0 的合约被丢掉，不该因此绕过 OI 判定（全 0 照样拒）。"""
        after = datetime(2026, 10, 22, 17, 5, tzinfo=ET)
        raw, why = _fetch(FakeYF(expiries=["2026-10-22", "2026-11-06", "2026-11-20"], mutate=self._zero_oi()), now_et=after)
        assert raw is None and why == "yf_oi_stale"

    def test_the_oi_judge_sees_the_unfiltered_expiries(self):
        """判据吃的是过滤之前的原始表：收盘后丢掉的 DTE=0 到期日也算在内。
        夹具：只有今天到期的那个到期日 OI 正常、其余两个全 0。含它 ⇒ OI>0 占 10/30 = 0.33 ≥ 0.25 ⇒ 放行；
        若判据只看过滤之后的表 ⇒ 0/20 ⇒ 陈旧。变红的变异：把判据改成吃 `kept` 对应的表。
        （上一版同名意图的用例在判据放前放后得到同一结果，抓不到这种变异——v0.45.662 二次检查。）"""
        after = datetime(2026, 10, 22, 17, 5, tzinfo=ET)

        def mutate(e, calls, puts):
            if e != "2026-10-22":
                calls["openInterest"] = 0.0
                puts["openInterest"] = 0.0
            return calls, puts
        raw, why = _fetch(FakeYF(expiries=["2026-10-22", "2026-11-06", "2026-11-20"], mutate=mutate), now_et=after)
        assert why is None and raw["oi_check"]["ratio"] == pytest.approx(1 / 3, abs=1e-3)
        assert {c["expiry"] for c in raw["contracts"]} == {"2026-11-06", "2026-11-20"}, "DTE=0 仍被丢掉，只是判据看得见它"

    def test_a_never_traded_contract_cannot_make_a_stale_chain_look_fresh(self):
        """`lastTradeDate` 为 NaT（从没成交过）的合约：`pd.NaT.isoformat()` 是 `"NaT"`，字符串比较里比 `"2026-…"` 大，
        曾把昨天的链的 vintage 顶成 `"NaT"`、绕过 `stale_vintage`（自 v0.45.603，v0.45.662 二次检查实测）。"""
        def mutate(e, calls, puts):
            calls.loc[0, "lastTradeDate"] = pd.NaT
            return calls, puts
        raw, why = _fetch(FakeYF(last_trade=TRADE_YESTERDAY, mutate=mutate))
        assert raw is None and why == "stale_vintage"

    def test_a_never_traded_contract_in_a_fresh_chain_is_kept_without_a_time(self):
        def mutate(e, calls, puts):
            calls.loc[0, "lastTradeDate"] = pd.NaT
            return calls, puts
        raw, why = _fetch(FakeYF(mutate=mutate))
        assert why is None and raw["vintage_date"] == "2026-10-22" and raw["payload_last_trade_time"] == "2026-10-22T10:52:30"
        nat_rows = [c for c in raw["contracts"] if c["last_trade_time"] is None]
        assert len(nat_rows) == 3, "每个到期日一个 NaT 合约，时间记 None，合约本身仍在"
        assert Y._et_naive_iso(pd.NaT) is None and Y._et_naive_iso(None) is None and Y._et_naive_iso(float("nan")) is None

    def test_staleness_of_the_prints_is_reported_before_the_oi_question(self):
        raw, why = _fetch(FakeYF(last_trade=TRADE_YESTERDAY, mutate=self._zero_oi()))
        assert raw is None and why == "stale_vintage"

    def test_placeholder_ivs_are_counted_so_the_page_can_say_how_thin_the_chain_is(self):
        def mutate(e, calls, puts):
            calls.loc[0, "impliedVolatility"] = PLACEHOLDER_IV
            puts.loc[0, "impliedVolatility"] = float("nan")
            return calls, puts
        raw, why = _fetch(FakeYF(mutate=mutate))
        assert why is None and raw["n_iv_missing"] == 3 * 2          # 3 个到期日 × (1 个 call + 1 个 put)
        clean, _ = _fetch(FakeYF())
        assert clean["n_iv_missing"] == 0

    def test_the_judge_is_the_shared_one_not_a_private_copy(self):
        """判据全仓只有一份（`oi_staleness`）：适配器不许自己再写一套阈值。"""
        import inspect
        import oi_staleness
        src = inspect.getsource(Y)
        assert "oi_staleness.assess_frames" in src and "MAX_RATIO" not in src and "MIN_RECENT" not in src
        assert oi_staleness.MIN_RECENT == 20 and oi_staleness.MAX_RATIO == 0.25     # 夹具的 30 个近期合约、30/30 OI 的前提


class TestServiceFallback:
    @pytest.fixture
    def svc(self, tmp_path):
        return SV.AlphaBotService(state_dir=tmp_path / "st", ledger_dir=tmp_path / "led", now_fn=lambda: NOW)

    @pytest.fixture
    def seen(self, monkeypatch):
        seen = {"cboe": [], "yf": [], "inval": []}
        monkeypatch.setattr(R, "_invalidate_cached_payload", lambda t: seen["inval"].append(t))

        def yf(t, as_of=None, **k):
            seen["yf"].append(t)
            return ({"ticker": t, "contracts": [{"x": 1}], "data_source": "yfinance"}, None)
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", yf)
        return seen

    def _cboe(self, monkeypatch, seen, result):
        def f(t, *, as_of):
            seen["cboe"].append(t)
            return result
        monkeypatch.setattr(R.LG, "_default_fetch", f)

    def test_stale_cboe_and_listed_ticker_falls_back_and_counts(self, svc, seen, monkeypatch):
        self._cboe(monkeypatch, seen, (None, "stale_vintage"))
        raw, why = svc._chain_fetch(invalidate=True)("NVDL", as_of=None)
        assert why is None and raw["data_source"] == "yfinance" and seen["yf"] == ["NVDL"] and seen["inval"] == ["NVDL"]
        assert svc.stats["yf_fallback_used"] == 1 and svc.stats["yf_fallback_failed"] == 0

    def test_the_fallback_logs_the_quality_numbers_nobody_else_reads(self, svc, seen, monkeypatch):
        """`oi_check` / `n_iv_missing` 页面不显示，唯一的读者是这条 info 日志（死字段检查，v0.45.662 二次检查）。"""
        self._cboe(monkeypatch, seen, (None, "stale_vintage"))
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", lambda t, as_of=None, **k: (
            {"ticker": t, "contracts": [{"x": 1}] * 40, "oi_check": {"ratio": 0.9535}, "n_iv_missing": 15}, None))
        lines = []

        class _Rec:
            def info(self, msg, *a, **k):
                lines.append(msg % a)

            def warning(self, *a, **k):
                pass
        monkeypatch.setattr(SV, "_log", _Rec())
        svc._chain_fetch(invalidate=False)("BILI", as_of=None)
        assert len(lines) == 1 and "0.9535" in lines[0] and "15 / 40" in lines[0], lines

    def test_a_healthy_cboe_never_touches_yfinance(self, svc, seen, monkeypatch):
        self._cboe(monkeypatch, seen, ({"contracts": []}, None))
        raw, why = svc._chain_fetch(invalidate=False)("NVDL", as_of=None)
        assert why is None and seen["yf"] == [] and seen["inval"] == []

    def test_unlisted_tickers_keep_cboes_failure(self, svc, seen, monkeypatch):
        self._cboe(monkeypatch, seen, (None, "stale_vintage"))
        assert svc._chain_fetch(invalidate=False)("NVDA", as_of=None) == (None, "stale_vintage") and seen["yf"] == []

    @pytest.mark.parametrize("reason", ["payload_unavailable", "vintage_mismatch", "price_unavailable"])
    def test_only_staleness_triggers_the_fallback(self, svc, seen, monkeypatch, reason):
        self._cboe(monkeypatch, seen, (None, reason))
        assert svc._chain_fetch(invalidate=False)("NVDL", as_of=None) == (None, reason) and seen["yf"] == []

    def test_yfinance_failure_is_reported_with_its_reason_and_counted(self, svc, seen, monkeypatch):
        self._cboe(monkeypatch, seen, (None, "stale_vintage"))
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", lambda t, as_of=None, **k: (None, "yf_no_expiries"))
        assert svc._chain_fetch(invalidate=False)("NVDL", as_of=None) == (None, "yf_no_expiries")
        assert svc.stats["yf_fallback_failed"] == 1 and svc.stats["yf_fallback_used"] == 0

    def test_an_injected_fetch_function_is_used_untouched(self, tmp_path):
        fn = lambda t, as_of=None: (None, "x")                      # noqa: E731
        s = SV.AlphaBotService(state_dir=tmp_path / "st", ledger_dir=tmp_path / "led", fetch_fn=fn, now_fn=lambda: NOW)
        assert s._chain_fetch(invalidate=True) is fn

    def test_the_sell_strike_ledger_still_only_knows_cboe(self):
        import inspect
        src = inspect.getsource(R.LG._default_fetch)
        assert "yf_chain" not in src and "fetch_cboe_raw_contracts" in src
        assert SV.YF_FALLBACK_TICKERS == frozenset({"NVDL", "BILI"})

    def test_bili_falls_back_on_the_live_pages(self, svc, seen, monkeypatch):
        """BILI 的 CBOE 文件盘中不更新（v0.45.662）⇒ 现算页走 Yahoo，并计数。"""
        self._cboe(monkeypatch, seen, (None, "stale_vintage"))
        raw, why = svc._chain_fetch(invalidate=False)("BILI", as_of=None)
        assert why is None and raw["data_source"] == "yfinance" and seen["yf"] == ["BILI"]
        assert svc.stats["yf_fallback_used"] == 1

    def test_bili_in_the_list_does_not_reach_the_premium_page_or_the_ledger(self, svc, seen, monkeypatch):
        """名单只管现算页：溢价页的取数入口（与卖权账本同一个取数函数）对 BILI 仍只认 CBOE，陈旧就如实陈旧。"""
        self._cboe(monkeypatch, seen, (None, "stale_vintage"))
        assert svc._premium_fetch()("BILI", as_of=None) == (None, "stale_vintage") and seen["yf"] == []

    @pytest.mark.parametrize("reason", ["yf_oi_stale", "yf_oi_unjudgeable"])
    def test_a_refused_dirty_chain_surfaces_as_unavailable_with_its_reason(self, svc, seen, monkeypatch, reason):
        """Yahoo OI 脏 ⇒ 页面拿到的是带原因的失败（并计入 yf_fallback_failed），不是一张用坏 OI 画出来的图。"""
        self._cboe(monkeypatch, seen, (None, "stale_vintage"))
        monkeypatch.setattr(Y, "fetch_yf_raw_contracts", lambda t, as_of=None, **k: (None, reason))
        assert svc._chain_fetch(invalidate=False)("BILI", as_of=None) == (None, reason)
        assert svc.stats["yf_fallback_failed"] == 1 and svc.stats["yf_fallback_used"] == 0
