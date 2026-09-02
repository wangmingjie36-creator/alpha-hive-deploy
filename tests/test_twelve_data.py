"""Twelve Data 日K（v0.45.61）

补上整条链最后一处非 yfinance 不可的依赖：逐标的 30 日收盘
（`rv_30d` / `iv_rv_spread` / hv_proxy 口径 `iv_rank` 全由它派生）。
2026-08-27 yfinance 687 次 429，这三项各 0/30。

其余候选全部实打验证过不可用：Finnhub `/stock/candle` 403、
Alpha Vantage 25/天被 newsapi 占满、Stooq 上了 JS 反爬、FRED 只有指数。
"""

import json

import pytest

import twelve_data as td


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    td.reset_stats()
    monkeypatch.setattr(td, "_limiter", None)
    yield
    td.reset_stats()


def _resp(payload):
    return json.dumps(payload).encode()


_OK = {"values": [{"datetime": f"2026-08-{d:02d}", "close": str(100 + d)}
                  for d in range(1, 28)]}


def _patch_http(monkeypatch, payload_or_exc):
    import http_gate

    def _open(*a, **k):
        if isinstance(payload_or_exc, Exception):
            raise payload_or_exc
        return payload_or_exc

    monkeypatch.setattr(http_gate, "urlopen_gated", _open)


class TestUnconfigured:
    """没 key 时必须安静地让路，不报错、不阻断降级链。"""

    def test_all_entrypoints_return_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        assert td.is_configured() is False
        assert td.fetch_daily_closes("NVDA") is None
        assert td.realized_vol("NVDA") is None

    def test_makes_no_request(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        import http_gate

        def _boom(*a, **k):
            raise AssertionError("未配 key 却发了请求")

        monkeypatch.setattr(http_gate, "urlopen_gated", _boom)
        assert td.fetch_daily_closes("NVDA") is None


class TestErrorHandling:
    def test_http_200_with_error_status_is_not_data(self, monkeypatch):
        """免费档超额时 **HTTP 仍是 200**，错误在 body 里。

        只看状态码就会把错误当数据 —— 正是「看着成功其实早废了」的形状。
        """
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(
            {"code": 429, "message": "You have run out of API credits",
             "status": "error"}))
        assert td.fetch_daily_closes("NVDA") is None

    def test_network_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, ConnectionError("down"))
        assert td.fetch_daily_closes("NVDA") is None

    def test_non_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, b"<html>nope</html>")
        assert td.fetch_daily_closes("NVDA") is None

    def test_too_few_bars_returns_none_not_partial(self, monkeypatch):
        """数据不足要报不可得。返回半截序列会让 RV 算在错误的窗口上。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(
            {"values": [{"close": "100"}, {"close": "101"}]}))
        assert td.fetch_daily_closes("NVDA") is None

    def test_exception_is_caught_by_network_errors(self):
        """新造的异常必须继承既有网络异常族。

        v0.45.56 在 `YFRateLimited` 上踩过：不继承就会穿透所有降级路径。
        """
        from resilience import NETWORK_ERRORS
        assert issubclass(td.TwelveDataUnavailable, NETWORK_ERRORS)
        assert issubclass(td.TwelveDataUnavailable, OSError)


class TestParsing:
    def test_returns_ascending_closes(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(_OK))
        cs = td.fetch_daily_closes("NVDA")
        assert len(cs) == 27
        assert cs[0] == 101.0 and cs[-1] == 127.0

    def test_drops_bad_rows_without_zero_filling(self, monkeypatch):
        """坏行剔除，**不补 0.0** —— 0.0 会让 np.std 算出假的低波动。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        bad = {"values": [{"close": "0"}, {"close": "-5"}, {"close": None},
                          {"close": "abc"}] + _OK["values"]}
        _patch_http(monkeypatch, _resp(bad))
        cs = td.fetch_daily_closes("NVDA")
        assert all(c > 0 for c in cs)
        assert len(cs) == 27

    def test_counts_credits(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(_OK))
        td.fetch_daily_closes("NVDA")
        td.fetch_daily_closes("TSLA")
        assert td.stats()["daily_used"] == 2


class TestRealizedVol:
    def test_matches_upstream_formula(self, monkeypatch):
        """口径必须与 `calculate_iv_rv_spread` 一致：对数收益 / ddof=1 / ×√252×100。

        口径不一致的话，同一只标的会因为走了哪条源而得到不同的 RV，
        那种差异会被误读成波动率变化。
        """
        import math
        monkeypatch.setattr(td, "api_key", lambda: "k")
        _patch_http(monkeypatch, _resp(_OK))
        rv = td.realized_vol("NVDA", lookback=26)

        closes = [101.0 + i for i in range(27)]
        rets = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:])]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        expect = math.sqrt(var) * math.sqrt(252) * 100
        assert rv == pytest.approx(expect, rel=1e-6)

    def test_rejects_absurd_values(self, monkeypatch):
        """HV30 > 300% 视为数据污染 —— 与上游同一条 sanity check。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        wild = {"values": [{"close": str(100 * (3 ** i))} for i in range(30)]}
        _patch_http(monkeypatch, _resp(wild))
        assert td.realized_vol("NVDA") is None

    def test_no_data_returns_none(self, monkeypatch):
        monkeypatch.setattr(td, "api_key", lambda: "")
        assert td.realized_vol("NVDA") is None


class TestIntegrationWithMarketIntelligence:
    def test_prefers_twelve_data_when_configured(self, monkeypatch):
        import market_intelligence as mi
        monkeypatch.setattr(td, "api_key", lambda: "k")
        monkeypatch.setattr(td, "realized_vol", lambda *a, **k: 36.14)

        import yfinance as yf

        def _boom(*a, **k):
            raise AssertionError("Twelve Data 可用时不该打 yfinance")

        monkeypatch.setattr(yf, "download", _boom)
        d = mi.calculate_iv_rv_spread("NVDA", 48.61)
        assert d["rv_30d"] == 36.14
        assert d["source"] == "twelve_data"
        assert d["iv_rv_signal"] == "expensive"     # 48.61-36.14 = +12.47 > 10

    def test_falls_back_to_yfinance_when_unavailable(self, monkeypatch):
        """Twelve Data 返回 None 时必须原样落回 yfinance，不能把整项打空。"""
        import market_intelligence as mi
        monkeypatch.setattr(td, "api_key", lambda: "k")
        monkeypatch.setattr(td, "realized_vol", lambda *a, **k: None)

        import yfinance as yf
        calls = {"n": 0}

        def _dl(*a, **k):
            calls["n"] += 1
            raise ConnectionError("no net")

        monkeypatch.setattr(yf, "download", _dl)
        d = mi.calculate_iv_rv_spread("NVDA", 48.61)
        assert calls["n"] >= 1, "未回落到 yfinance"
        assert d["rv_30d"] is None and d["data_available"] is False


class TestFormingBarGuard:
    """盘中未完成的末根日线必须丢掉（v0.45.61 自检时实测发现）。

    2026-08-28 10:09 ET 跑自检，NVDA 末根是
    `2026-08-28 close=224.57 volume=103412` —— 开盘才十分钟的半根 bar，
    而 8/24–8/27 成交量是 1.2~3.0 亿。把它算进 RV30 等于用一个残缺的
    日收益污染波动率（实测 42.56 vs 修复后 42.89）。

    项目里已有 `data_pipeline._drop_forming_bar`，但它靠 yfinance 探 SPY
    分钟线判交易所时间 —— 正是本模块要绕开的东西。这里用返回体自带的
    日期与成交量，不额外发请求。
    """

    def _rows(self, n=10, last_date="2026-08-27", last_vol=2e8):
        rows = [{"date": f"2026-08-{10 + i:02d}", "close": 100.0 + i, "vol": 2e8}
                for i in range(n - 1)]
        rows.append({"date": last_date, "close": 200.0, "vol": last_vol})
        return rows

    def test_drops_bar_dated_today_et(self, monkeypatch):
        monkeypatch.setattr(td, "_et_today", lambda: "2026-08-28")
        rows = self._rows(last_date="2026-08-28", last_vol=103412)
        assert len(td._drop_forming_bar(rows)) == len(rows) - 1

    def test_keeps_completed_last_bar(self, monkeypatch):
        """收盘后末根就是完整的，不该被丢。"""
        monkeypatch.setattr(td, "_et_today", lambda: "2026-08-28")
        rows = self._rows(last_date="2026-08-27", last_vol=2.9e8)
        assert len(td._drop_forming_bar(rows)) == len(rows)

    def test_volume_signal_catches_it_without_a_clock(self, monkeypatch):
        """时钟不可得时，成交量是独立的第二道判据。

        两道各自独立、任一命中即丢 —— 误丢的代价是 30 根少一根，
        漏丢的代价是波动率失真，不对称。
        """
        monkeypatch.setattr(td, "_et_today", lambda: None)
        rows = self._rows(last_date="2026-08-28", last_vol=103412)   # 中位的 0.05%
        assert len(td._drop_forming_bar(rows)) == len(rows) - 1

    def test_volume_signal_does_not_fire_on_normal_day(self, monkeypatch):
        monkeypatch.setattr(td, "_et_today", lambda: None)
        rows = self._rows(last_date="2026-08-27", last_vol=1.6e8)    # 中位的 80%
        assert len(td._drop_forming_bar(rows)) == len(rows)

    def test_too_few_rows_untouched(self, monkeypatch):
        """样本太少时不做判断——丢一根可能让整批不可用。"""
        monkeypatch.setattr(td, "_et_today", lambda: "2026-08-28")
        rows = self._rows(n=4, last_date="2026-08-28", last_vol=1)
        assert len(td._drop_forming_bar(rows)) == len(rows)

    def test_end_to_end_excludes_today(self, monkeypatch):
        """整条取数链上生效，不只是那个私有函数。"""
        monkeypatch.setattr(td, "api_key", lambda: "k")
        monkeypatch.setattr(td, "_et_today", lambda: "2026-08-28")
        payload = {"values": [
            {"datetime": f"2026-08-{10 + i:02d}", "close": str(100 + i),
             "volume": "200000000"} for i in range(18)
        ] + [{"datetime": "2026-08-28", "close": "224.57", "volume": "103412"}]}
        _patch_http(monkeypatch, _resp(payload))
        cs = td.fetch_daily_closes("NVDA")
        assert 224.57 not in cs, "盘中半根 bar 进了收盘价序列"
        assert len(cs) == 18


class TestBackfillTargetDate:
    """补跑必须取**目标日**窗口，不是最新的（v0.45.61 二次检查）。

    `realized_vol` 一开始就支持 `end_date`，但调用处没传 —— 补跑会拿最新窗口
    冒充目标日。对次日补跑影响小（forming-bar 已丢、窗口末端恰好是目标日），
    补跑更早的日子会严重错位。

    ⚠️ 本组只钉「目标日传到了取数层」这一段。传进去之后窗口末端到底落在哪，
    v0.45.61 并没验 —— 实际差了一个交易日（接口左闭右开），
    见下面的 `TestEndDateIsInclusive`（v0.45.90）。
    """

    def test_target_date_reaches_the_api(self, monkeypatch):
        import market_intelligence as mi
        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", "2026-08-20")
        monkeypatch.setattr(td, "api_key", lambda: "k")
        seen = {}

        def _rv(ticker, lookback=30, end_date=None):
            seen["end_date"] = end_date
            return 36.14

        monkeypatch.setattr(td, "realized_vol", _rv)
        d = mi.calculate_iv_rv_spread("NVDA", 48.61)
        assert seen["end_date"] == "2026-08-20", "补跑目标日没传到取数层"
        assert d["source"] == "twelve_data@2026-08-20", "来源标签未写明口径日"

    def test_no_target_date_means_latest(self, monkeypatch):
        import market_intelligence as mi
        monkeypatch.delenv("ALPHA_HIVE_TARGET_DATE", raising=False)
        monkeypatch.setattr(td, "api_key", lambda: "k")
        seen = {}

        def _rv(ticker, lookback=30, end_date=None):
            seen["end_date"] = end_date
            return 36.14

        monkeypatch.setattr(td, "realized_vol", _rv)
        d = mi.calculate_iv_rv_spread("NVDA", 48.61)
        assert seen["end_date"] is None
        assert d["source"] == "twelve_data"

    def test_malformed_target_date_is_ignored(self, monkeypatch):
        """环境变量可能来自残留 export / 别的工具，格式不合法就当没有。

        与 options_analyzer 对同一变量的处理一致（那里实测过
        `ALPHA_HIVE_TARGET_DATE=../../../tmp/evil` 会让快照写到 cache/ 之外）。
        """
        import market_intelligence as mi
        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", "../../../tmp/evil")
        monkeypatch.setattr(td, "api_key", lambda: "k")
        seen = {}
        monkeypatch.setattr(td, "realized_vol",
                            lambda t, lookback=30, end_date=None:
                            (seen.setdefault("end_date", end_date), 36.14)[1])
        mi.calculate_iv_rv_spread("NVDA", 48.61)
        assert seen["end_date"] is None, "非法日期被当成了目标日"


# 假日历的终点 = 下面钉住的「美东当日」，两者必须一起改。
# 日历比时钟长的话，`_drop_forming_bar` 会去咬日历尾巴，测的就不是本意了
# （MEMORY：fixture 日期要从被测日期倒推，别各写各的）。
_ET_TODAY = "2026-09-01"          # 实测那天


def _trading_days(start: str = "2026-07-01", end: str = _ET_TODAY):
    """周一~周五（不管假日 —— 假日只会让窗口少一根，不影响本组断言）。"""
    import datetime as dt
    d, last, out = dt.date.fromisoformat(start), dt.date.fromisoformat(end), []
    while d <= last:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


class TestEndDateIsInclusive:
    """`end_date` 对外语义 = **截至该日（含）**；接口是不含的（v0.45.90）。

    2026-09-01 实测（NVDA，`outputsize=15`）：

        _fetch_rows("NVDA", 15, "2026-08-31") → 末根 2026-08-28 close=217.55
        _fetch_rows("NVDA",  8, None)         → 末根 2026-08-31 close=220.78

    8/29–8/30 是周末，所以恰好差**一个交易日**：Twelve Data 的 `end_date`
    左闭右开，当日那根不返回。

    判定为 off-by-one 而不是「有意排除当日、让指标只含该日之前的信息」：

      · `fetch_daily_closes` docstring 明写「截至该日（含）」；
      · `market_intelligence` 的 v0.45.61 注释把「窗口末端恰好是目标日」当作
        **正确**状态在描述（"对次日补跑影响小"），即作者要的就是含；
      · 补跑 D 的报告在 D 收盘后才发（编排器时间闸 13:30 PDT = 16:30 ET），
        用 D 的收盘价不构成前视，HV30 的标准定义本来就含当日；
      · 不存在可对齐的「D-1 口径」：同一个 `calculate_iv_rv_spread` 里的
        yfinance 兜底腿走 `yf.download(period=...)`、没有 forming-bar 闸、
        当天那根照收 —— 两条腿本来就差一根。

    下面这组假接口**照真接口的左闭右开行为**造数据，所以断言钉的是语义，
    不是「参数长什么样」—— 把 `_api_end_date` 的 +1 去掉，它们会红。
    """

    def _fake_api(self, monkeypatch, seen):
        """模拟 Twelve Data：`end_date` 不含，`outputsize` 从末端往回数。"""
        import http_gate
        from urllib.parse import parse_qs, urlparse

        monkeypatch.setattr(td, "api_key", lambda: "k")
        monkeypatch.setattr(td, "_get_limiter", lambda: None)   # 7 次/分，别在测试里真等

        def _open(req, *a, **k):
            q = parse_qs(urlparse(req.full_url).query)
            seen["end_date"] = q.get("end_date", [None])[0]
            days = _trading_days()
            if seen["end_date"]:
                days = [d for d in days if d < seen["end_date"]]   # ← 不含，与真接口一致
            days = days[-int(q["outputsize"][0]):]
            return _resp({"values": [
                {"datetime": d, "close": str(100.0 + i), "volume": "200000000"}
                for i, d in enumerate(days)]})

        monkeypatch.setattr(http_gate, "urlopen_gated", _open)

    def test_last_bar_is_the_requested_day(self, monkeypatch):
        """核心不变式：要 2026-08-31（周一，交易日），末根就得是 2026-08-31。"""
        seen = {}
        self._fake_api(monkeypatch, seen)
        monkeypatch.setattr(td, "_et_today", lambda: _ET_TODAY)
        rows = td._fetch_rows("NVDA", 15, "2026-08-31")
        assert rows[-1]["date"] == "2026-08-31", (
            f"窗口末根 {rows[-1]['date']} ≠ 请求的 end_date 2026-08-31 —— "
            "接口左闭右开，`_api_end_date` 的 +1 天是不是被去掉了？")

    def test_last_bar_is_the_requested_day_across_a_weekend(self, monkeypatch):
        """目标日是周五时 +1 落到周六 —— 周六没有 bar，不能因此多带一根。"""
        seen = {}
        self._fake_api(monkeypatch, seen)
        monkeypatch.setattr(td, "_et_today", lambda: _ET_TODAY)
        rows = td._fetch_rows("NVDA", 15, "2026-08-28")
        assert seen["end_date"] == "2026-08-29", "没有 +1 到周六"
        assert rows[-1]["date"] == "2026-08-28"

    def test_realized_vol_window_ends_on_the_target_day(self, monkeypatch):
        """唯一的生产消费方走的是这条链：补跑 D 的 RV 必须含 D 的收盘。"""
        seen = {}
        self._fake_api(monkeypatch, seen)
        monkeypatch.setattr(td, "_et_today", lambda: _ET_TODAY)
        assert td.realized_vol("NVDA", lookback=30, end_date="2026-08-31") is not None
        assert seen["end_date"] == "2026-09-01"

    def test_forming_bar_guard_survives_the_shift(self, monkeypatch):
        """+1 之后目标日若正是今天，当日那根会被拉进来 —— 那道闸必须照旧拦住。

        两道机制管的不是一件事：`end_date` 画窗口，`_drop_forming_bar` 判
        「这根走完没有」。补上前者不能把后者顶掉。
        """
        seen = {}
        self._fake_api(monkeypatch, seen)
        monkeypatch.setattr(td, "_et_today", lambda: "2026-08-31")
        rows = td._fetch_rows("NVDA", 15, "2026-08-31")
        assert seen["end_date"] == "2026-09-01"
        assert rows[-1]["date"] == "2026-08-28", "美东当日的半根 bar 漏进来了"

    def test_live_path_is_untouched(self, monkeypatch):
        """`end_date=None` 不该因为这次修改而改变行为。

        钉的就是 2026-09-01 那次实测：`_fetch_rows("NVDA", 8, None)`
        末根 2026-08-31 —— 当日（9/1）那根由 forming 闸丢掉。
        """
        seen = {}
        self._fake_api(monkeypatch, seen)
        monkeypatch.setattr(td, "_et_today", lambda: _ET_TODAY)
        rows = td._fetch_rows("NVDA", 15, None)
        assert seen["end_date"] is None, "实时口径不该出现 end_date 参数"
        assert rows[-1]["date"] == "2026-08-31"


class TestApiEndDateHelper:
    def test_shifts_one_calendar_day(self):
        assert td._api_end_date("2026-08-31") == "2026-09-01"

    def test_shifts_across_month_and_year_ends(self):
        assert td._api_end_date("2026-08-31") == "2026-09-01"
        assert td._api_end_date("2026-12-31") == "2027-01-01"
        assert td._api_end_date("2028-02-28") == "2028-02-29"   # 闰年

    def test_passes_through_datetime_form(self):
        """Twelve Data 也收 `YYYY-MM-DD hh:mm:ss`，那种写法自带时刻语义，
        不该被按「日」平移。"""
        assert td._api_end_date("2026-08-31 16:00:00") == "2026-08-31 16:00:00"

    def test_garbage_passes_through_loudly(self, monkeypatch):
        """透传 = 「含该日」这条承诺本次不成立，日志必须说出来，不能静默。

        直接钉 `_log.warning` 而不用 caplog：`hive_logger` 的 handler/propagate
        配置不该决定这条断言的成败。
        """
        said = []
        monkeypatch.setattr(td._log, "warning",
                            lambda msg, *a, **k: said.append(msg % a if a else msg))
        assert td._api_end_date("not-a-date") == "not-a-date"
        assert any("原样透传" in m for m in said), f"非法 end_date 被静默透传了：{said}"
