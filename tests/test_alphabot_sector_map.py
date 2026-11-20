"""Alpha Bot「板块」页（标普 500 热力图，v0.45.783）的守卫。全部离线（持仓文件与报价都注入假的）。

每组回答一句「谁会红」：
  · 读表：道富的 xlsx 用标准库读得出来；现金、期货、或有权利不进成分（`TestHoldingsParse`）；
  · 板块归属：哪只板块基金里有它就归哪个；文件缺了 / 对不上 ⇒「未归类」并点名，不猜（`TestCombine`）；
  · 不当 0：没报价 / 缺价 / 停牌 / 涨跌幅离谱的票单独点名、不上色，不算成 0%（`TestBuild`）；
  · 取数失败看得见：限流、HTTP 错、空回复各有原因码；一块失败整体失败（`TestFetch`）；
  · 请求量有硬上限：缓存期内不重取、失败后冷却期内不重取、刷新失败沿用上一份并写明（`TestServiceCache`）；
  · 接线与边界：路由、导航、帮助章节；不走逐票日线、不碰评分与账本模块（`TestWiring`）。
"""
from __future__ import annotations

import ast
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alphabot import sector_map as SM
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "alphabot" / "static"
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 19, 11, 0, tzinfo=NY)                     # 周四盘中；服务的钟只经 now_fn / clock 注入
T_SESSION = datetime(2026, 11, 19, 10, 45, tzinfo=NY).timestamp()   # 这一场里的一笔成交
T_OLD = datetime(2026, 11, 17, 15, 59, tzinfo=NY).timestamp()       # 两天前（停牌的票）


# ── 夹具：一份最小的 xlsx（inline 字符串 + 共享字符串两种单元格都有，道富的文件两种都出现过）──────────

def _xlsx(rows, shared=("Name",)):
    def cell(col, r, v):
        ref = f"{col}{r}"
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        if v in shared:
            return f'<c r="{ref}" t="s"><v>{shared.index(v)}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    body = "".join(f'<row r="{i}">' + "".join(cell("ABCDEFGH"[j], i, v) for j, v in enumerate(r)) + "</row>"
                   for i, r in enumerate(rows, 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")
    return buf.getvalue()


def _holdings_file(rows, as_of="18-Nov-2026"):
    head = [["Fund Name:", "X"], ["Ticker Symbol:", "X"], ["Holdings:", f"As of {as_of}"], [],
            ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"]]
    return _xlsx(head + [[n, t, "id", "sedol", w, "-", 1.0, "USD"] for n, t, w in rows])


def _tickers(n):
    """n 个互不相同的四字母代码（AAAA、AAAB…）。"""
    out = []
    for i in range(n):
        a, b = divmod(i, 26)
        out.append("A" + chr(65 + a // 26) + chr(65 + a % 26) + chr(65 + b))
    return out


def _quote(price, prev, t=T_SESSION, **kw):
    return {"price": price, "prev_close": prev, "change_52w_pct": 10.0, "market_cap": 1e11, "time": t,
            "state": "REGULAR", "delay_min": 15, "name": "Name Inc.", **kw}


def _holdings(cons, **kw):
    return {"as_of": "2026-11-18", "constituents": [{"ticker": t, "name": t, "weight": w, "sector": s} for t, w, s in cons],
            "skipped": [], "cash_weight": 0.1, "failed_sectors": [], "multi_sector": {}, **kw}


# ── 读表 ───────────────────────────────────────────────────────────────────

class TestHoldingsParse:
    def test_reads_rows_date_and_skips_non_stocks(self):
        blob = _holdings_file([("APPLE INC", "AAPL", 8.13), ("BERKSHIRE B", "BRK.B", 1.69), ("US DOLLAR", "-", 0.119),
                               ("CONTRA X", "2602335D", 0.000003), ("FUT", "IXAZ6", 0.2)])
        p = SM.parse_holdings(SM.read_xlsx_rows(blob))
        assert p["as_of"] == "2026-11-18"
        assert [r["ticker"] for r in p["rows"]] == ["AAPL", "BRK.B"]
        assert p["rows"][0] == {"ticker": "AAPL", "name": "APPLE INC", "weight": 8.13}
        assert [s["ticker"] for s in p["skipped"]] == ["2602335D", "IXAZ6"]      # 点名，不悄悄丢
        assert p["cash_weight"] == pytest.approx(0.119)

    def test_columns_are_found_by_header_name_not_position(self):
        rows = [["Holdings:", "As of 02-Jan-2027"], ["Weight", "Ticker", "Name"], [1.5, "MSFT", "MICROSOFT"]]
        p = SM.parse_holdings(SM.read_xlsx_rows(_xlsx(rows)))
        assert p["rows"] == [{"ticker": "MSFT", "name": "MICROSOFT", "weight": 1.5}] and p["as_of"] == "2027-01-02"

    def test_garbage_raises_instead_of_returning_empty(self):
        with pytest.raises(ValueError):
            SM.read_xlsx_rows(b"<html>blocked</html>")
        with pytest.raises(ValueError):
            SM.parse_holdings(SM.read_xlsx_rows(_xlsx([["nothing", "here"]])))    # 没有表头 ≠ 空成分表
        with pytest.raises(ValueError):
            SM.read_xlsx_rows(b"x" * (SM.RULES["max_xlsx_bytes"] + 1))


class TestCombine:
    SPY = {"as_of": "2026-11-18", "rows": [{"ticker": t, "name": t, "weight": w} for t, w in
                                           (("AAPL", 8.0), ("JPM", 1.5), ("NEE", 0.3), ("ODD", 0.1))],
           "skipped": [], "cash_weight": 0.1}

    def test_sector_is_the_fund_that_holds_it(self):
        by = {e: {"as_of": "2026-11-18", "rows": []} for e in SM.SECTORS}
        by["XLK"]["rows"] = [{"ticker": "AAPL", "name": "", "weight": 20}]
        by["XLF"]["rows"] = [{"ticker": "JPM", "name": "", "weight": 9}]
        by["XLU"]["rows"] = [{"ticker": "NEE", "name": "", "weight": 9}]
        c = SM.combine(self.SPY, by)
        assert {x["ticker"]: x["sector"] for x in c["constituents"]} == {
            "AAPL": "XLK", "JPM": "XLF", "NEE": "XLU", "ODD": SM.UNSORTED}        # 哪都不在 ⇒ 未归类，不猜
        assert c["failed_sectors"] == [] and c["multi_sector"] == {}

    def test_missing_sector_file_is_named_and_multi_sector_is_reported(self):
        by = {e: {"as_of": "x", "rows": []} for e in SM.SECTORS}
        by["XLU"] = None
        by["XLK"]["rows"] = [{"ticker": "AAPL", "name": "", "weight": 1}]
        by["XLC"]["rows"] = [{"ticker": "AAPL", "name": "", "weight": 1}]
        c = SM.combine(self.SPY, by)
        assert c["failed_sectors"] == ["XLU"]
        assert c["multi_sector"] == {"AAPL": ["XLK", "XLC"]}
        assert next(x for x in c["constituents"] if x["ticker"] == "NEE")["sector"] == SM.UNSORTED

    def test_carry_only_fills_failed_sectors_from_previous(self):
        new = _holdings([("NEE", 0.3, SM.UNSORTED), ("ODD", 0.1, SM.UNSORTED)], failed_sectors=["XLU"])
        old = _holdings([("NEE", 0.3, "XLU"), ("ODD", 0.1, "XLK")])
        out = SM.carry_sectors(new, old)
        # ODD 上一份在 XLK，而 XLK 这次取到了却没有它 ⇒ 它真的不在了，不沿用
        assert {c["ticker"]: c["sector"] for c in out["constituents"]} == {"NEE": "XLU", "ODD": SM.UNSORTED}
        assert out["carried"] == 1
        assert SM.carry_sectors(new, None) is new
        ok = _holdings([("NEE", 0.3, "XLU")])
        assert SM.carry_sectors(ok, old) is ok


# ── 排图 ───────────────────────────────────────────────────────────────────

class TestBuild:
    def _build(self):
        h = _holdings([("AAPL", 8.0, "XLK"), ("MSFT", 6.0, "XLK"), ("JPM", 1.5, "XLF"), ("BRK.B", 1.7, "XLF"),
                       ("HALT", 0.2, "XLF"), ("NOQ", 0.1, "XLE"), ("BAD", 0.1, "XLE"), ("WILD", 0.1, "XLE")])
        q = {"AAPL": _quote(101.0, 100.0), "MSFT": _quote(98.0, 100.0), "JPM": _quote(50.0, 50.0),
             "BRK-B": _quote(202.0, 200.0), "HALT": _quote(11.0, 10.0, t=T_OLD), "BAD": _quote(None, 10.0),
             "WILD": _quote(30.0, 10.0), "XLK": _quote(99.5, 100.0, change_52w_pct=20.0), "SPY": _quote(700.0, 707.0)}
        return SM.build(h, q, watchlist=["MSFT"])

    def test_change_is_price_over_prev_close(self):
        r = self._build()
        leaves = {k["ticker"]: k for s in r["sectors"] for k in s["children"]}
        assert leaves["AAPL"]["change_1d"] == pytest.approx(1.0) and leaves["MSFT"]["change_1d"] == pytest.approx(-2.0)
        assert leaves["BRK.B"]["change_1d"] == pytest.approx(1.0)                 # 持仓表写 BRK.B、Yahoo 写 BRK-B
        assert leaves["JPM"]["change_1d"] == 0.0 and leaves["JPM"]["reason"] is None    # 真 0 是数据，照上色
        assert leaves["MSFT"]["in_watchlist"] is True and leaves["AAPL"]["in_watchlist"] is False
        assert r["session_date"] == "2026-11-19"

    def test_session_is_the_latest_day_with_enough_tickers(self):
        from collections import Counter
        # 开盘头一分钟：只有三成的票成交过 ⇒ 这一场已经是今天（没开盘的那七成不上色，而不是反过来）
        assert SM.pick_session(Counter({"2026-11-18": 350, "2026-11-19": 150})) == "2026-11-19"
        # 一只票的时间戳错到了后一天 ⇒ 不许把整张图带过去
        assert SM.pick_session(Counter({"2026-11-19": 502, "2026-11-20": 1})) == "2026-11-19"
        assert SM.pick_session(Counter()) is None
        h = _holdings([(f"T{chr(65 + i)}", 1.0, "XLK") for i in range(10)])
        q = {f"T{chr(65 + i)}": _quote(101.0, 100.0, t=T_OLD if i < 7 else None) for i in range(10)}
        q = {k: (v if i < 7 else _quote(101.0, 100.0)) for i, (k, v) in enumerate(q.items())}
        r = SM.build(h, q)
        assert r["session_date"] == "2026-11-19"
        assert r["stats"]["n_quoted"] == 3 and {m["reason"] for m in r["stats"]["missing"]} == {"stale_quote"}

    def test_unusable_quotes_are_named_not_zero(self):
        r = self._build()
        leaves = {k["ticker"]: k for s in r["sectors"] for k in s["children"]}
        want = {"HALT": "stale_quote", "NOQ": "no_quote", "BAD": "bad_price", "WILD": "implausible"}
        for tk, why in want.items():
            assert leaves[tk]["reason"] == why and leaves[tk]["change_1d"] is None, tk
            assert leaves[tk]["change_52w"] is None            # 这一场的报价不可用 ⇒ 另一个周期也不上色
        assert {m["ticker"]: m["reason"] for m in r["stats"]["missing"]} == want
        assert r["stats"]["n"] == 8 and r["stats"]["n_quoted"] == 4 and r["stats"]["n_missing"] == 4
        assert (r["stats"]["up"], r["stats"]["down"], r["stats"]["flat"]) == (2, 1, 1)
        assert set(want.values()) <= set(SM.REASONS)

    def test_sector_numbers(self):
        r = self._build()
        by = {s["key"]: s for s in r["sectors"]}
        assert [s["key"] for s in r["sectors"]] == ["XLK", "XLF", "XLE"]          # 按权重从大到小
        assert by["XLK"]["change_1d"] == pytest.approx((8 * 1.0 + 6 * -2.0) / 14, abs=1e-3)
        assert by["XLF"]["change_1d"] == pytest.approx((1.5 * 0 + 1.7 * 1.0) / 3.2, abs=1e-3)   # 停牌那只不进分母
        assert by["XLE"]["change_1d"] is None and by["XLE"]["n_quoted"] == 0      # 整个板块没数 ⇒ 空，不是 0
        assert by["XLK"]["etf"]["change_1d"] == pytest.approx(-0.5) and by["XLK"]["etf"]["change_52w"] == 20.0
        assert by["XLF"]["etf"]["reason"] == "no_quote"
        assert "change_52w" not in by["XLK"]                   # 52 周不做成分加权（偏向涨得多的），只给板块基金自己的
        assert r["bench"]["change_1d"] == pytest.approx(-0.99, abs=0.01)

    def test_reason_codes_used_in_module_are_all_registered(self):
        src = (ROOT / "alphabot" / "sector_map.py").read_text(encoding="utf-8")
        used = set(re.findall(r'"reason": "([a-z_]+)"', src)) | set(re.findall(r'return None, "([a-z_]+)"', src))
        assert len(used) >= 8, used                            # 解析规则坏了就在这里红，不空转
        assert used <= set(SM.REASONS), used - set(SM.REASONS)


# ── 取数 ───────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


class _YF:
    def __init__(self, codes=None):
        self.calls, self.codes = [], list(codes or [])

    def get(self, url, params=None, timeout=None):
        syms = params["symbols"].split(",")
        self.calls.append(syms)
        code = self.codes.pop(0) if self.codes else 200
        return _Resp(code, {"quoteResponse": {"result": [
            {"symbol": s, "regularMarketPrice": 10.0, "regularMarketPreviousClose": 9.0, "regularMarketTime": T_SESSION,
             "marketState": "REGULAR", "fiftyTwoWeekChangePercent": 5.0, "marketCap": 1e9, "shortName": s} for s in syms]}})


class TestFetch:
    def test_quotes_are_chunked_not_one_request_per_ticker(self):
        yf = _YF()
        syms = _tickers(503)
        q, meta = SM.fetch_quotes(syms, yf_data=yf)
        assert meta == {"reason": None, "n_requests": 3} and len(yf.calls) == 3      # 503 只 = 3 个请求，不是 503 个
        assert max(len(c) for c in yf.calls) <= SM.RULES["quote_chunk"]
        assert len(q) == 503 and q[syms[0]]["price"] == 10.0 and q[syms[0]]["prev_close"] == 9.0

    def test_rate_limit_http_error_and_empty_each_have_a_reason(self):
        q, meta = SM.fetch_quotes(_tickers(300), yf_data=_YF([200, 429]))
        assert q is None and meta["reason"] == "rate_limited" and "retry_after_sec" in meta   # 半张图不要
        q, meta = SM.fetch_quotes(["AAPL"], yf_data=_YF([503]))
        assert q is None and meta["reason"] == "fetch_error" and "http_503" in meta["detail"]

        class Empty:
            def get(self, *a, **k):
                return _Resp(200, {"quoteResponse": {"result": []}})
        assert SM.fetch_quotes(["AAPL"], yf_data=Empty()) == (None, {"reason": "empty", "n_requests": 1})

    def _files(self, n=420):
        tks = _tickers(n)
        files = {"spy": _holdings_file([(t, t, 0.2) for t in tks])}
        for i, e in enumerate(SM.SECTORS):
            files[e.lower()] = _holdings_file([(t, t, 1.0) for t in tks[i::len(SM.SECTORS)]])
        return tks, files

    def _get(self, files, fail=(), log=None):
        def get(url):
            key = url.rsplit("-", 1)[1].split(".")[0]
            if log is not None:
                log.append(key)
            if key in fail:
                raise OSError("boom")
            return files[key]
        return get

    def test_holdings_ok(self):
        tks, files = self._files()
        data, meta = SM.fetch_holdings(self._get(files))
        assert meta["reason"] is None and len(data["constituents"]) == 420 and data["failed_sectors"] == []
        assert all(c["sector"] in SM.SECTORS for c in data["constituents"])

    def test_one_sector_file_failing_is_partial_and_retried_once(self):
        tks, files = self._files()
        log = []
        data, meta = SM.fetch_holdings(self._get(files, fail={"xlu"}, log=log))
        assert meta["reason"] is None and data["failed_sectors"] == ["XLU"] and meta["errors"] == {"XLU": "OSError"}
        assert log.count("xlu") == SM.RULES["holdings_attempts"] == 2
        assert sum(1 for c in data["constituents"] if c["sector"] == SM.UNSORTED) == len(tks[list(SM.SECTORS).index("XLU")::11])

    def test_spy_failing_or_truncated_is_a_failure_not_a_small_map(self):
        tks, files = self._files()
        assert SM.fetch_holdings(self._get(files, fail={"spy"})) == (None, {"reason": "holdings_unavailable", "detail": "OSError"})
        _, small = self._files(50)
        data, meta = SM.fetch_holdings(self._get(small))
        assert data is None and meta["reason"] == "holdings_too_small"


# ── 服务：缓存与请求量上限 ─────────────────────────────────────────────────────

class _Env:
    """假钟 + 计数的假取数。"""

    def __init__(self, tmp_path, holdings=None):
        self.t, self.now = 1000.0, NOW
        self.h_calls, self.q_calls = 0, 0
        self.h_result = (holdings or _holdings([("AAPL", 8.0, "XLK"), ("JPM", 1.5, "XLF")]), {"reason": None})
        self.q_result = ({"AAPL": _quote(101.0, 100.0), "JPM": _quote(49.0, 50.0), "SPY": _quote(700.0, 707.0)}, {"reason": None})
        self.dir = tmp_path

    def svc(self, **kw):
        def hf():
            self.h_calls += 1
            return self.h_result

        def qf(symbols):
            self.q_calls += 1
            self.symbols = list(symbols)
            return self.q_result
        return AlphaBotService(bars_fn=lambda t: {"available": True, "source": "demo", "bars": syn.demo_bars(t, "2026-09-01")},
                               state_dir=self.dir, clock=lambda: self.t, now_fn=lambda: self.now,
                               sector_holdings_fn=hf, sector_quotes_fn=qf, **kw)


class TestServiceCache:
    def test_quotes_are_cached_for_the_live_ttl(self, tmp_path):
        env = _Env(tmp_path)
        svc = env.svc()
        r = svc.sector_map()
        assert r["available"] and r["session"]["live"] and r["stats"]["n_quoted"] == 2
        assert set(env.symbols) == {"AAPL", "JPM", *SM.SECTORS, "SPY"}             # 板块基金与 SPY 同一批问
        env.t += SM.RULES["quote_ttl_live_sec"] - 1
        svc.sector_map()
        assert (env.h_calls, env.q_calls) == (1, 1)
        env.t += 2
        assert svc.sector_map()["quotes"]["age_sec"] == 0
        assert (env.h_calls, env.q_calls) == (1, 2)

    def test_refresh_button_cannot_hammer(self, tmp_path):
        env = _Env(tmp_path)
        svc = env.svc()
        svc.sector_map()
        for _ in range(5):
            env.t += 5
            svc.sector_map(refresh=True)
        assert env.q_calls == 1                                  # 25 秒内连点 5 次「刷新」⇒ 一次都不多取
        env.t += SM.RULES["quote_fail_ttl_sec"]
        svc.sector_map(refresh=True)
        assert env.q_calls == 2

    def test_idle_ttl_is_longer_outside_the_session(self, tmp_path):
        env = _Env(tmp_path)
        env.now = datetime(2026, 11, 19, 20, 0, tzinfo=NY)
        svc = env.svc()
        assert svc.sector_map()["session"]["live"] is False
        env.t += SM.RULES["quote_ttl_live_sec"] + 10
        svc.sector_map()
        assert env.q_calls == 1

    def test_refresh_failure_keeps_previous_quotes_and_says_so(self, tmp_path):
        env = _Env(tmp_path)
        svc = env.svc()
        svc.sector_map()
        env.q_result = (None, {"reason": "rate_limited", "retry_after_sec": 170.0})
        env.t += 200
        r = svc.sector_map()
        assert r["available"] and r["stats"]["n_quoted"] == 2
        assert r["quotes"]["age_sec"] == 200                     # 真实年龄，不冒充刚取的
        rf = r["quotes"]["refresh_failed"]
        assert rf["reason"] == "rate_limited" and rf["reason_zh"] == "Yahoo 限流" and rf["retry_after_sec"] == 170
        env.t += 100                                             # 冷却期内：一个请求都不发
        r = svc.sector_map(refresh=True)
        assert env.q_calls == 2 and r["quotes"]["refresh_failed"]["retry_after_sec"] == 70
        env.t += 71
        env.q_result = ({"AAPL": _quote(102.0, 100.0)}, {"reason": None})
        r = svc.sector_map()
        assert env.q_calls == 3 and r["quotes"]["refresh_failed"] is None and svc.stats["sector_failures"] == 1

    def test_first_fetch_failing_is_unavailable_with_reason(self, tmp_path):
        env = _Env(tmp_path)
        env.q_result = (None, {"reason": "fetch_error", "detail": "TimeoutError"})
        r = env.svc().sector_map()
        assert r["available"] is False and r["reason"] == "fetch_error" and r["reason_zh"] == "取数失败"
        assert "sectors" not in r

        env2 = _Env(tmp_path / "b")
        env2.h_result = (None, {"reason": "holdings_unavailable", "detail": "HTTPError"})
        svc = env2.svc()
        r = svc.sector_map()
        assert r["available"] is False and r["reason"] == "holdings_unavailable" and env2.q_calls == 0
        svc.sector_map()
        assert env2.h_calls == 1                                 # 失败后 `holdings_fail_ttl_sec` 内不再下 12 份文件

    def test_holdings_persist_across_restart_and_expire_daily(self, tmp_path):
        env = _Env(tmp_path)
        env.svc().sector_map()
        assert (tmp_path / "sector_map" / "holdings.json").is_file()
        svc2 = env.svc()
        r = svc2.sector_map()
        assert env.h_calls == 1 and r["holdings"]["as_of"] == "2026-11-18"        # 重启不重下
        env.now = datetime(2026, 11, 20, 11, 0, tzinfo=NY)
        env.t += 10_000
        svc2.sector_map()
        assert env.h_calls == 2

    def test_stale_holdings_survive_a_failed_refresh(self, tmp_path):
        env = _Env(tmp_path)
        svc = env.svc()
        svc.sector_map()
        env.now = datetime(2026, 11, 20, 11, 0, tzinfo=NY)
        env.t += 10_000
        env.h_result = (None, {"reason": "holdings_unavailable", "detail": "HTTPError"})
        r = svc.sector_map()
        assert r["available"] and r["holdings"]["refresh_failed"]["reason"] == "holdings_unavailable"
        assert r["holdings"]["as_of"] == "2026-11-18"

    def test_partial_holdings_are_retried_soon_and_carry_previous_sectors(self, tmp_path):
        full = _holdings([("AAPL", 8.0, "XLK"), ("NEE", 0.3, "XLU")])
        env = _Env(tmp_path, holdings=full)
        svc = env.svc()
        svc.sector_map()
        env.now = datetime(2026, 11, 20, 11, 0, tzinfo=NY)
        env.t += 10_000
        env.h_result = (_holdings([("AAPL", 8.0, "XLK"), ("NEE", 0.3, SM.UNSORTED)], failed_sectors=["XLU"]), {"reason": None})
        r = svc.sector_map()
        assert r["holdings"]["failed_sectors"] == ["XLU"] and r["holdings"]["carried"] == 1
        assert [s["key"] for s in r["sectors"]] == ["XLK", "XLU"]                  # NEE 沿用上一份的归属
        env.now = datetime(2026, 11, 20, 11, 11, tzinfo=NY)                        # 缺文件的那份只留十分钟
        env.t += 700
        svc.sector_map()
        assert env.h_calls == 3

    def test_demo_and_injected_services_never_reach_the_network(self, tmp_path):
        assert AlphaBotService(demo=True, state_dir=tmp_path).sector_map()["reason"] == "demo"
        svc = AlphaBotService(bars_fn=lambda t: {"available": False, "bars": []}, state_dir=tmp_path / "x")
        r = svc.sector_map()                                     # 注入了日线而没给板块取数 ⇒ 不取（测试里不许悄悄出网）
        assert r["available"] is False and r["reason"] == "no_fetcher" and r["reason_zh"]


# ── 接线与边界 ────────────────────────────────────────────────────────────────

class TestWiring:
    def test_route_nav_and_help(self, tmp_path):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        env = _Env(tmp_path)
        c = TestClient(create_app(env.svc(), port=8765), base_url="http://127.0.0.1:8765")
        r = c.get("/api/sector-map")
        assert r.status_code == 200 and r.json()["available"] is True and r.json()["rules"]["periods"]["1d"]["clamp_pct"] > 0
        assert c.get("/static/pages/sectors.js").status_code == 200
        assert 'data-nav="sectors">板块</a>' in (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        assert 'from "./pages/sectors.js"' in app and 'page === "sectors"' in app
        assert re.search(r'^  \["sectors", "', (STATIC / "pages" / "help.js").read_text(encoding="utf-8"), re.M)
        page = (STATIC / "pages" / "sectors.js").read_text(encoding="utf-8")
        assert 'helpLink("sectors")' in page and "innerHTML" not in page

    def test_tooltip_escapes_external_names(self):
        js = (STATIC / "charts.js").read_text(encoding="utf-8")
        tip = js.split("export function sectorTreemap", 1)[1].split("const chart = mount", 1)[0].split("const tip =", 1)[1]
        raw = re.findall(r"\$\{(k\.(?:ticker|name)|s\.(?:name|key)|s\.etf\.symbol)[^}]*\}", tip)
        assert not raw, f"提示里有没转义的外部文本：{raw}"       # 公司名来自 Yahoo / 道富，直接拼进 HTML 就是注入口
        assert tip.count("esc(") >= 5

    def test_module_stays_out_of_scoring_ledgers_and_per_ticker_bars(self):
        src = (ROOT / "alphabot" / "sector_map.py").read_text(encoding="utf-8")
        mods = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert mods <= {"__future__", "io", "re", "zipfile", "xml", "collections", "concurrent", "datetime", "typing",
                        "zoneinfo", "alphabot", "urllib", "yfinance", "yf_gate"}, mods
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#", "·", "⚠")))
        body = code.split('"""', 2)[2]                           # 模块说明里讲了为什么不用，代码里不许出现
        assert ".download(" not in body and ".history(" not in body

    def test_only_service_imports_it(self):
        offenders = []
        for py in ROOT.glob("**/*.py"):
            rel = py.relative_to(ROOT).as_posix()
            if rel.startswith((".claude/", "tests/", "_retired")) or rel in ("alphabot/service.py", "alphabot/sector_map.py"):
                continue
            if re.search(r"^\s*(from alphabot import .*\bsector_map\b|from alphabot\.sector_map|import alphabot\.sector_map)",
                         py.read_text(encoding="utf-8", errors="ignore"), re.M):
                offenders.append(rel)
        assert offenders == []

    def test_real_clock_is_not_read(self):
        """模块里没有 `now()` / `today()` / `time.time()`：「这一场」是从报价自己的时间戳里数出来的，服务的钟经注入。"""
        src = (ROOT / "alphabot" / "sector_map.py").read_text(encoding="utf-8")
        assert not re.search(r"\.now\(|\.today\(|time\.time\(|utcnow\(", src)
        assert datetime.fromtimestamp(T_SESSION, timezone.utc).astimezone(NY).date().isoformat() == "2026-11-19"
