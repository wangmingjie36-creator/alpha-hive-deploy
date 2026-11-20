"""Alpha Bot「资金」子页（v0.45.795）的守卫。全部离线（东方财富回复与道富文件都注入假的）。

每组回答一句「谁会红」：
  · 东方财富字段：「主力 = 超大单 + 大单」对不上 ⇒ 整份拒收，不拿错位的数出图；data 为空 ⇒ 换下一个市场代码（`TestParseEm`）；
  · 取数失败看得见：网络错、格式错、三个市场都没有各有原因码，网络错不再往下试（`TestFetchStock`）；
  · ETF 算法：份额变化 × 当天净值，与 2026-11-18 XLP 实测一致；表头按名字找；跨了交易日的行标出来（`TestEtf`）；
  · 请求量有硬上限：缓存期内不重取、失败冷却期内不重取（连「重新拉取」也不破）、刷新失败沿用旧的并写明（`TestService`）；
  · 接线与边界：路由、子页、帮助章节；东方财富固定直连；不碰评分与账本模块（`TestWiring`）。
"""
from __future__ import annotations

import ast
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alphabot import money_flow as MF
from alphabot import sector_map as SM
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "alphabot" / "static"
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 19, 11, 0, tzinfo=NY)          # 周四盘中；服务的钟只经 now_fn / clock 注入


def _kline(day, main, small, medium, large, sup, close=100.0, pct=1.0):
    return f"{day},{main},{small},{medium},{large},{sup},0,0,0,0,0,{close},{pct},0.00,0.00"


# 2026-11-19 / 18 NVDA 的真实两行（东方财富，11-20 取）
REAL = [
    "2026-11-18,115752160.0,279816928.0,249123584.0,7936496.0,107815664.0,0.22,0.52,0.46,0.01,0.20,150.960,-2.89,0.00,0.00",
    "2026-11-19,-9231024.0,157633088.0,1601024.0,-95093264.0,85862240.0,-0.02,0.32,0.00,-0.20,0.18,145.980,-3.30,0.00,0.00",
]


def _payload(klines):
    return {"rc": 0, "data": {"code": "NVDA", "market": 105, "klines": klines}}


class TestParseEm:
    def test_real_rows_parse_and_main_equals_large_plus_super(self):
        rows, why = MF.parse_em(_payload(list(reversed(REAL))))
        assert why is None and [r["date"] for r in rows] == ["2026-11-18", "2026-11-19"]      # 升序
        r = rows[1]
        assert r["main"] == pytest.approx(-9231024.0) and r["close"] == pytest.approx(145.98)
        assert r["main"] == pytest.approx(r["large"] + r["super"], abs=MF.RULES["main_tol_usd"])

    def test_swapped_fields_are_rejected_not_plotted(self):
        # 字段顺序变了（这里把小单与大单对调）⇒ 主力 ≠ 超大 + 大单 ⇒ 整份不用
        bad = _kline("2026-11-19", -9231024.0, -95093264.0, 1601024.0, 157633088.0, 85862240.0)
        assert MF.parse_em(_payload([bad])) == (None, "inconsistent")

    def test_missing_data_means_not_listed_and_garbage_is_bad_payload(self):
        assert MF.parse_em({"rc": 100, "data": None}) == (None, "not_listed")
        assert MF.parse_em({"data": {"klines": ["2026-11-19,1,2"]}})[1] == "bad_payload"
        assert MF.parse_em({"data": {"klines": ["2026-13-40,1,2,3,4,5,0,0,0,0,0,1,1"]}})[1] == "bad_payload"
        assert MF.parse_em({"data": {"klines": [_kline("2026-11-19", "x", 1, 1, 1, 1)]}})[1] == "bad_payload"
        assert MF.parse_em({"data": {"klines": []}}) == (None, "empty")
        assert MF.parse_em([]) == (None, "bad_payload")


class TestFetchStock:
    def test_tries_markets_in_order_and_reports_the_hit(self):
        seen = []

        def get(url):
            seen.append(re.search(r"secid=(\d+)\.", url).group(1))
            return json.dumps(_payload(REAL) if "secid=106." in url else {"rc": 100, "data": None}).encode()
        rows, meta = MF.fetch_stock_flow("XLP", http_get=get)
        assert rows and meta == {"reason": None, "market": 106, "n_requests": 2} and seen == ["105", "106"]
        seen.clear()
        MF.fetch_stock_flow("XLP", known_market=106, http_get=get)
        assert seen == ["106"]                                # 记下的市场先试，命中就不再问别的

    def test_network_error_stops_and_is_named(self):
        calls = []

        def boom(url):
            calls.append(url)
            raise ConnectionResetError("Remote end closed connection")
        rows, meta = MF.fetch_stock_flow("NVDA", http_get=boom)
        assert rows is None and meta["reason"] == "fetch_error" and "ConnectionResetError" in meta["detail"]
        assert len(calls) == 1                                # 网络错不是「这个市场没有」，不该接着试别的

    def test_not_listed_anywhere_and_bad_ticker(self):
        rows, meta = MF.fetch_stock_flow("ZZZZ", http_get=lambda u: b'{"rc":100,"data":null}')
        assert rows is None and meta == {"reason": "not_listed", "n_requests": len(MF.EM_MARKETS)}
        assert MF.fetch_stock_flow("bad ticker!", http_get=lambda u: b"")[1]["reason"] == "bad_ticker"
        assert MF.fetch_stock_flow("NVDA", http_get=lambda u: b"<html>")[1]["reason"] == "bad_payload"
        assert MF.em_code("BRK.B") == "BRK_B"

    def test_build_stock_totals_weeks_and_partial_today(self):
        rows = [MF.parse_em(_payload([_kline(d, m, 1.0, 1.0, m - 2.0, 2.0, close=c)]))[0][0]
                for d, m, c in [("2026-11-13", -10.0, 100.0), ("2026-11-16", 5.0, 90.0), ("2026-11-19", -3.0, 81.0)]]
        b = MF.build_stock(rows, today_et="2026-11-19", live=True)
        assert b["totals"]["main"] == pytest.approx(-8.0)
        assert b["days"][0]["date"] == "2026-11-19" and b["days"][0]["partial"] is True and b["days"][1]["partial"] is False
        assert [(w["start"], w["end"], w["main"], w["partial"]) for w in b["weeks"]] == [
            ("2026-11-16", "2026-11-19", 2.0, True), ("2026-11-13", "2026-11-13", -10.0, False)]
        assert b["range"]["change_pct"] == pytest.approx(-19.0)
        assert MF.build_stock(rows, today_et="2026-11-19", live=False)["days"][0]["partial"] is False


# ── ETF ─────────────────────────────────────────────────────────────────────

def _xlsx(rows):
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'

    def cell(col, r, v):
        if v is None:
            return ""
        return f'<c r="{col}{r}" t="inlineStr"><is><t>{v}</t></is></c>'
    body = "".join(f'<row r="{i}">' + "".join(cell("ABCD"[j], i, v) for j, v in enumerate(r)) + "</row>"
                   for i, r in enumerate(rows, 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")
    return buf.getvalue()


# 2026-11-17 / 18 XLP 的真实两行（道富 navhist，11-20 取）；ETFdb 记 11-18 为 −4.30 亿美元
XLP_ROWS = [["Fund Name:", "XLP"], ["Ticker Symbol:", "XLP"], ["Date", "NAV", "Shares Outstanding", "Total Net Assets"],
            ["18-Nov-2026", "91.212193", "1.62171809E8", "1.479204628489E10"],
            ["17-Nov-2026", "91.474567", "1.66871809E8", "1.526452643464E10"],
            ["30-May-2006", "23.77", "-", "-"],
            ["Distributor: footer text", None, None, None]]


class TestEtf:
    def test_flow_is_share_change_times_nav_matching_the_real_day(self):
        series, why, skipped = MF.parse_navhist(SM.read_xlsx_rows(_xlsx(XLP_ROWS)))
        assert why is None and [s["date"] for s in series] == ["2026-11-17", "2026-11-18"] and skipped == 2
        f = MF.etf_daily_flows(series)
        assert len(f) == 1 and f[0]["date"] == "2026-11-18"
        assert f[0]["flow"] == pytest.approx(-4.7e6 * 91.212193) and round(f[0]["flow"] / 1e8, 2) == -4.29

    def test_header_found_by_name_not_by_column_letter(self):
        rows = [{"A": "x"}, {"A": "Shares Outstanding", "B": "Date", "C": "NAV"},
                {"A": "100", "B": "17-Nov-2026", "C": "10"}, {"A": "110", "B": "18-Nov-2026", "C": "11"}]
        series, why, _ = MF.parse_navhist(rows)
        assert why is None and MF.etf_daily_flows(series)[0]["flow"] == pytest.approx(110.0)
        assert MF.parse_navhist([{"A": "Date", "B": "Price"}])[1] == "no_header"
        assert MF.parse_navhist([{"A": "Date", "B": "NAV", "C": "Shares Outstanding"},
                                 {"A": "18-Nov-2026", "B": "1", "C": "1"}])[1] == "too_few_rows"

    def test_gap_between_rows_is_flagged_not_hidden(self):
        series = [{"date": "2026-11-13", "nav": 1.0, "shares": 10.0}, {"date": "2026-11-18", "nav": 1.0, "shares": 12.0}]
        trading = lambda d: d not in ("2026-11-14", "2026-11-15")          # noqa: E731 - 周末
        assert MF.etf_daily_flows(series, trading)[0]["span_days"] == 3           # 16、17、18 三个交易日的变化压在一行

        def broken(d):
            raise RuntimeError("calendar down")
        assert MF.etf_daily_flows(series, broken)[0]["span_days"] is None         # 日历坏了：判不了，不说「1 天」

    def test_fetch_names_each_missing_fund(self):
        def get(url):
            if "xlp" in url:
                return _xlsx(XLP_ROWS)
            raise TimeoutError("slow")
        data, meta = MF.fetch_etf_navhist(["XLP", "XLK"], read_rows=SM.read_xlsx_rows, http_get=get)
        assert set(data) == {"XLP"} and meta["errors"] == {"XLK": "fetch_error:TimeoutError"} and meta["reason"] is None
        data, meta = MF.fetch_etf_navhist(["XLK"], read_rows=SM.read_xlsx_rows, http_get=lambda u: b"not a zip")
        assert data is None and meta["reason"] == "fetch_error" and meta["errors"]["XLK"].startswith("bad_payload")

    def test_fund_list_is_the_sector_pages_list(self):
        assert list(MF.ETFS) == ["SPY", *SM.SECTORS] and {k: MF.ETFS[k] for k in SM.SECTORS} == SM.SECTORS

    def test_build_etf_week_table(self):
        series, _, _ = MF.parse_navhist(SM.read_xlsx_rows(_xlsx(XLP_ROWS)))
        e = MF.build_etf({"XLP": series})
        assert [f["symbol"] for f in e["funds"]] == ["XLP"]
        assert e["week_table"][0]["week"] == "2026-11-16" and e["week_table"][0]["cells"]["XLP"]["n_days"] == 1


# ── 服务：缓存、冷却、各自失败 ─────────────────────────────────────────────────

class _Env:
    def __init__(self, tmp_path):
        self.dir, self.t, self.now = tmp_path, 1000.0, NOW
        self.s_calls, self.e_calls = 0, 0
        self.s_result = (MF.parse_em(_payload(REAL))[0], {"reason": None, "market": 105, "n_requests": 1})
        self.e_result = ({"XLP": MF.parse_navhist(SM.read_xlsx_rows(_xlsx(XLP_ROWS)))[0]},
                         {"reason": None, "errors": {}, "skipped": {}})
        self.markets = []

    def svc(self):
        def sf(t, known_market=None):
            self.s_calls += 1
            self.markets.append(known_market)
            if isinstance(self.s_result, Exception):
                raise self.s_result
            return self.s_result

        def ef(symbols):
            self.e_calls += 1
            self.symbols = list(symbols)
            return self.e_result
        return AlphaBotService(bars_fn=lambda t: {"available": True, "source": "demo", "bars": syn.demo_bars(t, "2026-09-01")},
                               state_dir=self.dir, clock=lambda: self.t, now_fn=lambda: self.now,
                               completed_session_fn=lambda: "2026-11-18", flow_stock_fn=sf, flow_etf_fn=ef)


class TestService:
    def test_cached_within_ttl_and_market_remembered(self, tmp_path):
        env = _Env(tmp_path)
        svc = env.svc()
        r = svc.money_flow("nvda")
        assert r["stock"]["available"] and r["stock"]["market"] == 105 and r["etf"]["available"]
        assert env.symbols == list(MF.ETFS)
        env.t += MF.RULES["stock_ttl_live_sec"] - 1
        svc.money_flow("NVDA")
        assert (env.s_calls, env.e_calls) == (1, 1)
        env.t += 2
        svc.money_flow("NVDA")
        assert env.s_calls == 2 and env.markets == [None, 105] and env.e_calls == 1   # ETF 按场次缓存

    def test_failure_cooldown_holds_even_on_refresh_and_old_copy_is_kept(self, tmp_path):
        env = _Env(tmp_path)
        svc = env.svc()
        svc.money_flow("NVDA")
        env.t += MF.RULES["stock_ttl_live_sec"] + 1
        env.s_result = (None, {"reason": "fetch_error", "detail": "RemoteDisconnected"})
        r = svc.money_flow("NVDA")
        st = r["stock"]
        assert st["available"] and st["refresh_failed"]["reason"] == "fetch_error" and st["age_sec"] > 0
        n = env.s_calls
        svc.money_flow("NVDA", refresh=True)
        assert env.s_calls == n                                      # 冷却期内连「重新拉取」也不发请求
        assert svc.stats["flow_failures"] == 1

    def test_halves_fail_independently_and_with_reasons(self, tmp_path):
        env = _Env(tmp_path)
        env.s_result = ValueError("boom")
        env.e_result = (None, {"reason": "fetch_error", "errors": {"SPY": "fetch_error:TimeoutError"}})
        r = env.svc().money_flow("NVDA")
        assert r["stock"] == {"available": False, "reason": "fetch_error", "reason_zh": MF.REASONS["fetch_error"],
                              "detail": "ValueError"}
        assert r["etf"]["available"] is False and r["etf"]["errors"] == {"SPY": "fetch_error:TimeoutError"}

    def test_partial_etf_set_names_missing_and_retries_after_cooldown(self, tmp_path):
        env = _Env(tmp_path)
        env.e_result = (env.e_result[0], {"reason": None, "errors": {"XLK": "fetch_error:TimeoutError"}, "skipped": {}})
        svc = env.svc()
        r = svc.money_flow("NVDA")
        assert r["etf"]["available"] and r["etf"]["errors"]["XLK"]["zh"] == MF.REASONS["fetch_error"]
        svc.money_flow("NVDA")
        assert env.e_calls == 1
        env.t += MF.RULES["etf_fail_ttl_sec"] + 1
        svc.money_flow("NVDA")
        assert env.e_calls == 2                                      # 缺几只的那份过冷却再取一次补

    def test_index_demo_and_injected_services_never_reach_the_network(self, tmp_path):
        env = _Env(tmp_path)
        assert env.svc().money_flow("SPX")["stock"]["reason"] == "index" and env.s_calls == 0
        r = AlphaBotService(demo=True, state_dir=tmp_path).money_flow("NVDA")
        assert r["stock"]["reason"] == "demo" and r["etf"]["reason"] == "demo"
        svc = AlphaBotService(bars_fn=lambda t: {"available": False, "bars": []}, state_dir=tmp_path / "x")
        r = svc.money_flow("NVDA")                                  # 注入了日线而没给资金流取数 ⇒ 不取
        assert r["stock"]["reason"] == "no_fetcher" and r["etf"]["reason"] == "no_fetcher"


# ── 接线与边界 ────────────────────────────────────────────────────────────────

class TestWiring:
    def test_route_tab_and_help(self, tmp_path):
        from starlette.testclient import TestClient
        from alphabot.server import create_app
        env = _Env(tmp_path)
        c = TestClient(create_app(env.svc(), port=8765), base_url="http://127.0.0.1:8765")
        r = c.get("/api/money-flow/NVDA")
        assert r.status_code == 200 and r.json()["stock"]["available"] is True
        assert c.get("/static/pages/flow.js").status_code == 200
        assert c.get("/api/method").json()["money_flow"]["tiers"]["main"]
        ticker = (STATIC / "pages" / "ticker.js").read_text(encoding="utf-8")
        assert '["flow", "资金"]' in ticker and 'flow: "flow"' in ticker and 'from "./flow.js"' in ticker
        assert re.search(r'^  \["flow", "', (STATIC / "pages" / "help.js").read_text(encoding="utf-8"), re.M)
        page = (STATIC / "pages" / "flow.js").read_text(encoding="utf-8")
        assert "innerHTML" not in page and "setTimeout" not in page and "style=" not in page

    def test_eastmoney_is_fetched_direct_not_through_the_proxy(self):
        # 走代理会被东方财富断开（2026-11-20 实测），Alpha Bot 启动时又会给本进程设代理变量 ⇒ 这一条必须显式直连
        src = (ROOT / "alphabot" / "money_flow.py").read_text(encoding="utf-8")
        fetch = src.split("def fetch_stock_flow", 1)[1].split("\ndef ", 1)[0]
        assert "direct=True" in fetch
        assert "ProxyHandler({})" in src.split("def _http_get", 1)[1].split("\ndef ", 1)[0]

    def test_module_stays_out_of_scoring_and_ledgers(self):
        src = (ROOT / "alphabot" / "money_flow.py").read_text(encoding="utf-8")
        mods = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        assert mods <= {"__future__", "json", "re", "concurrent", "datetime", "typing", "alphabot", "urllib"}, mods
