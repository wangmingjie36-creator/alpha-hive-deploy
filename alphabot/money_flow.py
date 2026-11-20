"""「资金」子页（v0.45.795）：个股按单笔金额分档的资金流 + 板块 SPDR 的每日申购赎回。只展示，不进评分、不进任何账本。

数据从哪来（2026-11-20 实测后定的，两条都免费、不调 LLM）
------------------------------------------------------
· **个股资金流**：东方财富的日线资金流接口（`push2his…/fflow/daykline`），一只票一个请求、一次给近几十天。
  口径是**按单笔成交金额分档**推算的：超大单 + 大单 = 「主力」，中单、小单 = 「散户」。它**不是**账户数据——
  一个机构拆成很多小单就被算进「散户」；也只数交易所里的公开成交（暗池 / 场外不在里面），所以四档加起来**不等于零**。
  ⚠️ 实测**走代理会被对方断开**（`RemoteDisconnected`），直连通 ⇒ 这一条请求固定直连（`_http_get(direct=True)`），
  别「顺手」改成跟随环境代理（Alpha Bot 启动时会给本进程设代理变量，见 `alphabot/outbound.py`）。
  市场代码：东方财富用 105 = 纳斯达克、106 = 纽交所、107 = 美交所；不知道的票按 `EM_MARKETS` 顺序逐个试，试中的记下来。
· **ETF 申购赎回**：道富官网每只 SPDR 的「净值历史」xlsx（`navhist-us-en-<代码>.xlsx`），逐日有净值与流通份额。
  当日资金流 = （今天份额 − 上一行份额）× 今天净值——ETFdb / ETF.com 也是这个算法（2026-11-18 XLP 实测：
  本式 −4.29 亿美元，ETFdb −4.30 亿）。只收 SPY + 11 只板块 SPDR：
  ⚠️ **SMH（VanEck）/ SOXX（iShares）不收**——VanEck 的数据接口只给「今天的」份额，iShares 对程序请求回 403 / 只回网页；
  ETFdb 也对程序回 403。别为它们去抓网页图表。

日期口径：资金流记在发行商文件上份额变动的那一天；申赎的实际成交可能早一天（各家结算口径不同），页面写明。

纯函数（`parse_em` / `parse_navhist` / `etf_daily_flows` / `weekly` / `build_*`）与取数（`fetch_stock_flow` /
`fetch_etf_navhist`）分开；xlsx 读法与「板块」页同一个（`sector_map.read_xlsx_rows`，由 `service` 注入 `read_rows`）；取数永不抛、失败带原因码（唯一表 `REASONS`）。缓存、限频都在 `service.money_flow`。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

EM_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
#: f51 日期、f52 主力、f53 小单、f54 中单、f55 大单、f56 超大单（净流入，美元）；f57–f61 各档净占比（不用）；f62 收盘、f63 涨跌幅
EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
#: 东方财富美股市场代码：纳斯达克、纽交所、美交所（按这个顺序试）
EM_MARKETS = (105, 106, 107)
NAVHIST_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
               "navhist-us-en-{}.xlsx")
#: 收哪些 ETF：SPY + 11 只板块 SPDR。与「板块」页 `sector_map.SECTORS` 同一份名单、同一顺序（测试逐项核对）——
#: 不 import 它：`sector_map` 只许 `service` 引用（`tests/test_alphabot_sector_map.py::test_only_service_imports_it`）
ETFS: Dict[str, str] = {
    "SPY": "标普 500", "XLK": "科技", "XLF": "金融", "XLV": "医疗", "XLY": "可选消费", "XLC": "通信", "XLI": "工业",
    "XLP": "必需消费", "XLE": "能源", "XLU": "公用事业", "XLRE": "房地产", "XLB": "材料",
}
TIERS = ("main", "super", "large", "medium", "small")
TIER_ZH = {"main": "主力（超大 + 大单）", "super": "超大单", "large": "大单", "medium": "中单", "small": "小单"}

RULES = {
    "stock_days": 30,               # 向东方财富要几天（页面按周汇总，显示最近这些）
    "stock_ttl_live_sec": 600,      # 盘中同一份最多用多久（也是请求量的硬上限）
    "stock_ttl_idle_sec": 3600,
    "stock_fail_ttl_sec": 300,      # 失败后多久内不再试
    "etf_days": 30,                 # ETF 每只显示最近几行（文件有几千行）
    "etf_ttl_sec": 3 * 3600,        # 道富文件一天更新一次；同一场次内最多这么久重取一次
    "etf_fail_ttl_sec": 900,
    "etf_workers": 4,               # 12 份文件并发几份
    "http_timeout_sec": 25,
    "main_tol_usd": 2.0e6,          # 「主力 = 超大单 + 大单」允许的舍入误差；超出 ⇒ 字段顺序变了，整份拒收
    "max_body_bytes": 2_000_000,    # 东方财富一次回复实测约 3 KB
}
REASONS = {
    "demo": "演示模式不取真实数据",
    "no_fetcher": "这个实例没接取数",
    "index": "指数没有个股资金流",
    "bad_ticker": "代码认不出",
    "not_listed": "东方财富没有这只票的资金流（三个美股市场代码都试过）",
    "fetch_error": "取数失败",
    "bad_payload": "返回格式和预期不符，不出数",
    "inconsistent": "「主力 ≠ 超大单 + 大单」：接口字段顺序可能变了，整份不用",
    "empty": "接口没返回任何一天",
    "no_header": "净值历史文件里找不到表头（日期 / 净值 / 流通份额）",
    "too_few_rows": "净值历史不够两行，算不出变化",
}
DISCLAIMER = ("个股资金流来自东方财富，按单笔成交金额分档推算，不是真实账户数据；ETF 资金流 = 份额变化 × 净值，"
              "来自道富官网。只展示，不进评分，不构成投资建议。")
CAVEATS = (
    "「主力」= 超大单 + 大单，是按单笔成交金额分的档：机构拆单下就会被算进中单、小单，散户的大单也会被算进主力。",
    "只数交易所里的公开成交：暗池和场外成交（美股约四成成交量）不在里面，所以各档加起来不等于零——看方向，别拿绝对金额互相比。",
    "资金流没有买卖双方之分：每一笔成交都有一个买方一个卖方，「净流入」是按主动买 / 主动卖推算的。",
    "ETF 资金流记在份额变动出现在发行商文件上的那一天，实际申购赎回可能早一天；一天里同时有大额申购和赎回会互相抵掉。",
    "SMH（VanEck）、SOXX（iShares）没有可程序读取的份额历史，所以这里没有半导体 ETF；只收 SPY 和 11 只板块 SPDR。",
)

_TICKER_OK = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


# ── 个股：东方财富 ──────────────────────────────────────────────────────────────

def em_code(ticker: str) -> Optional[str]:
    """Alpha Bot 的代码 → 东方财富的代码（BRK.B → BRK_B，这种写法未经实测）。认不出 ⇒ None。"""
    t = (ticker or "").strip().upper()
    return t.replace(".", "_") if _TICKER_OK.match(t) else None


def parse_em(payload) -> Tuple[Optional[List[dict]], Optional[str]]:
    """东方财富回复（已 `json.loads`）→ `(逐日行（日期升序）, 原因码)`。`data` 为空 ⇒ `(None, "not_listed")`。
    每行核对「主力 = 超大单 + 大单」：对不上说明字段顺序变了 ⇒ 整份拒收（`inconsistent`），不拿错位的数出图。"""
    if not isinstance(payload, dict):
        return None, "bad_payload"
    data = payload.get("data")
    if data is None:
        return None, "not_listed"
    kl = data.get("klines") if isinstance(data, dict) else None
    if not isinstance(kl, list):
        return None, "bad_payload"
    rows = []
    for line in kl:
        parts = str(line).split(",")
        if len(parts) < 13:
            return None, "bad_payload"
        try:
            day = date.fromisoformat(parts[0]).isoformat()
        except ValueError:
            return None, "bad_payload"
        v = [_num(x) for x in parts[1:6]]
        if any(x is None for x in v):
            return None, "bad_payload"
        main, small, medium, large, sup = v
        if abs(main - (large + sup)) > RULES["main_tol_usd"]:
            return None, "inconsistent"
        rows.append({"date": day, "main": main, "super": sup, "large": large, "medium": medium, "small": small,
                     "close": _num(parts[11]), "pct": _num(parts[12])})
    if not rows:
        return None, "empty"
    rows.sort(key=lambda r: r["date"])
    return rows, None


def _http_get(url: str, *, direct: bool = False, headers: Optional[dict] = None) -> bytes:
    import urllib.request
    h = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/130 Safari/537.36", **(headers or {})}
    handlers = [urllib.request.ProxyHandler({})] if direct else []     # 东方财富走代理会被断开，见模块头
    opener = urllib.request.build_opener(*handlers)
    with opener.open(urllib.request.Request(url, headers=h), timeout=RULES["http_timeout_sec"]) as r:  # noqa: S310 - 固定的公开 https 地址
        body = r.read(RULES["max_body_bytes"] * 4 + 1)
    return body


def em_url(market: int, code: str, days: int) -> str:
    return (f"{EM_URL}?lmt={int(days)}&klt=101&secid={int(market)}.{code}"
            f"&fields1=f1,f2,f3,f7&fields2={EM_FIELDS2}")


def fetch_stock_flow(ticker: str, *, known_market: Optional[int] = None,
                     http_get: Optional[Callable[[str], bytes]] = None) -> Tuple[Optional[List[dict]], dict]:
    """→ `(逐日行 | None, {reason, market, n_requests, detail?})`。永不抛。
    先试记下的市场代码，再按 `EM_MARKETS` 试其余的；都说「没有」⇒ `not_listed`；网络错 ⇒ `fetch_error`（不再往下试）。"""
    code = em_code(ticker)
    if code is None:
        return None, {"reason": "bad_ticker", "n_requests": 0}
    get = http_get or (lambda u: _http_get(u, direct=True, headers={"Referer": "https://quote.eastmoney.com/"}))
    order = ([known_market] if known_market in EM_MARKETS else []) + [m for m in EM_MARKETS if m != known_market]
    n = 0
    for m in order:
        n += 1
        try:
            body = get(em_url(m, code, RULES["stock_days"]))
            if len(body) > RULES["max_body_bytes"]:
                return None, {"reason": "bad_payload", "detail": "body_too_large", "n_requests": n}
            payload = json.loads(body)
        except ValueError as e:
            return None, {"reason": "bad_payload", "detail": f"{type(e).__name__}", "n_requests": n}
        except Exception as e:  # noqa: BLE001 - 断开 / 超时 / 4xx 都归成 fetch_error，原因写进 detail
            return None, {"reason": "fetch_error", "detail": f"{type(e).__name__}: {str(e)[:80]}", "n_requests": n}
        rows, why = parse_em(payload)
        if why == "not_listed":
            continue
        if rows is None:
            return None, {"reason": why, "market": m, "n_requests": n}
        return rows, {"reason": None, "market": m, "n_requests": n}
    return None, {"reason": "not_listed", "n_requests": n}


# ── ETF：道富净值历史 ───────────────────────────────────────────────────────────

def _parse_day(s) -> Optional[str]:
    s = (s or "").strip()
    for fmt in ("%d-%b-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_navhist(rows: Sequence[Dict[str, Optional[str]]]) -> Tuple[Optional[List[dict]], Optional[str], int]:
    """净值历史表（`read_rows(xlsx 字节)` 的输出：`[{列字母: 文本}]`）→ `(逐日 [{date, nav, shares}]（升序）, 原因码, 跳过的行数)`。
    表头按名字找列（「Date」「NAV」「Shares Outstanding」），不写死列字母；日期 / 数字认不出的行跳过并计数。"""
    head_i, cols = None, {}
    for i, r in enumerate(rows):
        names = {(v or "").strip().lower(): k for k, v in r.items()}
        if "date" in names and "nav" in names and "shares outstanding" in names:
            head_i = i
            cols = {"date": names["date"], "nav": names["nav"], "shares": names["shares outstanding"]}
            break
    if head_i is None:
        return None, "no_header", 0
    out: Dict[str, dict] = {}
    skipped = 0
    for r in rows[head_i + 1:]:
        if not any((v or "").strip() for v in r.values()):
            continue
        d, nav, sh = _parse_day(r.get(cols["date"])), _num(r.get(cols["nav"])), _num(r.get(cols["shares"]))
        if d is None or nav is None or sh is None or nav <= 0 or sh < 0:
            skipped += 1
            continue
        out[d] = {"date": d, "nav": nav, "shares": sh}
    series = [out[k] for k in sorted(out)]
    if len(series) < 2:
        return None, "too_few_rows", skipped
    return series, None, skipped


def etf_daily_flows(series: Sequence[dict], is_trading: Optional[Callable[[str], bool]] = None) -> List[dict]:
    """逐日资金流：`(份额 − 上一行份额) × 当天净值`。第一行没有上一行，不出数。
    `is_trading(日期) -> bool`：给了就核对两行之间有没有漏掉的交易日——漏了 ⇒ 这一行的变化跨了几天（`span_days` > 1），
    照算但标出来；日历抛 ⇒ `span_days=None`（判不了）。"""
    out = []
    for prev, cur in zip(series, series[1:]):
        span: Optional[int] = 1
        if is_trading is not None:
            try:
                d0, d1 = date.fromisoformat(prev["date"]), date.fromisoformat(cur["date"])
                span = 0
                d = d0
                while d < d1:
                    d = date.fromordinal(d.toordinal() + 1)
                    if is_trading(d.isoformat()):
                        span += 1
                span = max(span, 1)
            except Exception:  # noqa: BLE001 - 日历坏了：照算，但说「判不了」
                span = None
        out.append({"date": cur["date"], "nav": cur["nav"], "shares": cur["shares"],
                    "shares_change": cur["shares"] - prev["shares"],
                    "flow": (cur["shares"] - prev["shares"]) * cur["nav"], "span_days": span})
    return out


def fetch_etf_navhist(symbols: Iterable[str], *, read_rows: Callable[[bytes], List[dict]],
                      http_get: Optional[Callable[[str], bytes]] = None) -> Tuple[Optional[Dict[str, List[dict]]], dict]:
    """`read_rows`：xlsx 字节 → 行表（格式不对抛 `ValueError`）。→ `({代码: 逐日 [{date, nav, shares}]}, {reason, errors: {代码: 原因码}, skipped: {代码: 行数}, n_requests})`。永不抛。
    一份都没取到 ⇒ `(None, …)`；缺几份 ⇒ 照给其余的，缺的写进 `errors`。"""
    syms = list(dict.fromkeys(symbols))
    get = http_get or (lambda u: _http_get(u))

    def one(sym):
        try:
            blob = get(NAVHIST_URL.format(sym.lower()))
            series, why, skipped = parse_navhist(read_rows(blob))
        except ValueError as e:
            return sym, None, f"bad_payload:{e}", 0
        except Exception as e:  # noqa: BLE001
            return sym, None, f"fetch_error:{type(e).__name__}", 0
        return sym, series, why, skipped

    data: Dict[str, List[dict]] = {}
    errors: Dict[str, str] = {}
    skipped: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=RULES["etf_workers"]) as ex:
        for sym, series, why, sk in ex.map(one, syms):
            if series is None:
                errors[sym] = why or "fetch_error"
            else:
                data[sym] = series
            if sk:
                skipped[sym] = sk
    meta = {"reason": None if data else "fetch_error", "errors": errors, "skipped": skipped, "n_requests": len(syms)}
    return (data or None), meta


# ── 汇总与成页 ─────────────────────────────────────────────────────────────────

def _week_key(day: str) -> str:
    d = date.fromisoformat(day)
    return date.fromordinal(d.toordinal() - d.weekday()).isoformat()      # 那一周的周一


def weekly(rows: Sequence[dict], fields: Sequence[str]) -> List[dict]:
    """逐日行按自然周（周一起）相加 → `[{week, start, end, n_days, <字段>: 合计}]`（升序）。缺值（None）不加、计 `n_missing`。"""
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        groups.setdefault(_week_key(r["date"]), []).append(r)
    out = []
    for wk in sorted(groups):
        g = sorted(groups[wk], key=lambda r: r["date"])
        row = {"week": wk, "start": g[0]["date"], "end": g[-1]["date"], "n_days": len(g)}
        for f in fields:
            vals = [r.get(f) for r in g]
            ok = [v for v in vals if isinstance(v, (int, float))]
            row[f] = sum(ok) if ok else None
            if len(ok) < len(vals):
                row.setdefault("n_missing", {})[f] = len(vals) - len(ok)
        out.append(row)
    return out


def build_stock(rows: List[dict], *, today_et: Optional[str] = None, live: bool = False) -> dict:
    """个股：逐日（最近的在前）、按周、整段合计。`today_et` 那一天且盘中 ⇒ 标 `partial`（还没收盘，数会变）。"""
    days = [dict(r, partial=bool(live and today_et and r["date"] == today_et)) for r in rows]
    tot = {f: sum(r[f] for r in days) for f in TIERS}
    first, last = days[0], days[-1]
    chg = None
    if isinstance(first.get("close"), float) and isinstance(last.get("close"), float) and first["close"] > 0:
        chg = (last["close"] / first["close"] - 1) * 100
    weeks = weekly(days, TIERS)
    for w in weeks:
        inside = [r for r in days if w["start"] <= r["date"] <= w["end"]]
        w["close_end"] = inside[-1].get("close")
        w["partial"] = any(r["partial"] for r in inside)
    return {"days": list(reversed(days)), "weeks": list(reversed(weeks)), "totals": tot,
            "range": {"start": first["date"], "end": last["date"], "n_days": len(days),
                      "close_start": first.get("close"), "close_end": last.get("close"), "change_pct": chg}}


def build_etf(navhist: Dict[str, List[dict]], *, is_trading: Optional[Callable[[str], bool]] = None,
              days: Optional[int] = None) -> dict:
    """ETF：每只最近 `days` 行的逐日资金流 + 按周；再出一张「周 × 基金」的表（页面那张总表）。"""
    n = int(days or RULES["etf_days"])
    funds, weeks_all = [], set()
    for sym in ETFS:
        series = navhist.get(sym)
        if not series:
            continue
        flows = etf_daily_flows(series[-(n + 1):], is_trading)       # 多拿一行：第一行只当「上一行」
        wk = weekly(flows, ("flow",))
        weeks_all.update(w["week"] for w in wk)
        last = series[-1]
        funds.append({"symbol": sym, "name": ETFS[sym], "days": list(reversed(flows)), "weeks": list(reversed(wk)),
                      "total": sum(f["flow"] for f in flows), "last_date": last["date"],
                      "aum": last["shares"] * last["nav"],
                      "n_span_gaps": sum(1 for f in flows if f["span_days"] not in (1,))})
    order = sorted(weeks_all, reverse=True)
    table = []
    for wk in order:
        row = {"week": wk, "cells": {}}
        for f in funds:
            w = next((x for x in f["weeks"] if x["week"] == wk), None)
            if w is not None:
                row["cells"][f["symbol"]] = {"flow": w["flow"], "start": w["start"], "end": w["end"], "n_days": w["n_days"]}
        table.append(row)
    return {"funds": funds, "week_table": table}


def public_rules() -> dict:
    return {**RULES, "reasons": REASONS, "tiers": TIER_ZH, "etfs": ETFS, "caveats": list(CAVEATS),
            "em_markets": list(EM_MARKETS)}
