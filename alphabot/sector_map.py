"""「板块」页（v0.45.783）：标普 500 成分股按权重 × 涨跌幅的热力图。只展示，不进评分、不进任何账本。

数据从哪来（2026-11-19 实测后定的，三条都免费、不调 LLM）
------------------------------------------------------
· **成分与面积**：SPY 每日持仓文件（道富官网的 xlsx）。面积 = SPY 权重（流通市值占比）——不是总市值：
  Yahoo 的 `marketCap` 对双重股权的公司（GOOGL / GOOG、FOX / FOXA…）每一类都报整家公司的市值，直接相加会算两遍。
· **板块**：SPY 文件的板块列全是「-」（实测），所以板块 = 「它在 11 只板块 SPDR 里的哪一只」。实测 504 只成分
  全部恰好落进一只、无遗漏无重复；哪天对不上 ⇒ 归「未归类」并在页面写数，不猜。
· **涨跌**：Yahoo 报价接口（`v7/finance/quote`），一次请求带几百只票：503 只 = 2 个请求、约 3 秒。
  ⚠️ **别改成 `yf.download` 取日线**：那是每只票一个请求（503 个），实测一轮 3–4 分钟、第二轮缺 50 只。
  代价是色块图只有报价里现成的周期（今日、52 周），没有 5 日 / 1 月。
· **顶部那排的 5 日 / 1 月**（v0.45.792，用户 2026-11-20 定「只加板块那一排」）：SPY + 11 只板块基金的复权日线
  （`yf_daily`，一天 12 个请求），现价 ÷ 往回数第 5 / 21 个交易日的收盘。成分股没有这两个周期。

纯函数（`read_xlsx_rows` / `parse_holdings` / `combine` / `build`）与取数（`fetch_holdings` / `fetch_quotes`）分开；
取数永不抛、失败带原因码（唯一表 `REASONS`）。缓存、限频、落盘都在 `service.sector_map`。
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET  # noqa: S405 - 只解析道富官网的持仓表，且先过大小上限
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from alphabot import beta_long as BL

NY = ZoneInfo("America/New_York")

#: 板块 SPDR → 中文名；顺序就是页面上的缺省顺序
SECTORS: Dict[str, str] = {
    "XLK": "科技", "XLF": "金融", "XLV": "医疗", "XLY": "可选消费", "XLC": "通信", "XLI": "工业",
    "XLP": "必需消费", "XLE": "能源", "XLU": "公用事业", "XLRE": "房地产", "XLB": "材料",
}
BENCH = "SPY"
UNSORTED = "_"                      # 没落进任何板块文件的成分（`sector` 键的取值；页面写「未归类」）
HOLDINGS_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
                "holdings-daily-us-en-{}.xlsx")
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

RULES = {
    "quote_chunk": 250,             # 一次报价请求带几只票（实测 260 只一次返回齐）
    "holdings_ttl_sec": 20 * 3600,  # 持仓文件一天一份
    "holdings_fail_ttl_sec": 600,   # 持仓刷新失败（或缺了几份板块文件）后多久内不再试
    "holdings_attempts": 2,         # 每份文件最多取几次
    "quote_ttl_live_sec": 120,      # 盘中同一份报价最多用多久（也是请求量的硬上限：≤ 每 2 分钟 3 个请求）
    "quote_ttl_idle_sec": 900,
    "quote_fail_ttl_sec": 60,
    "max_xlsx_bytes": 5_000_000,    # 实测每份 2–6 万字节
    "max_xml_bytes": 40_000_000,
    "implausible_pct": 50.0,        # 标普成分一天 ±50% 以上按坏数据算，不上色
    "etf_period": "3mo",            # 板块基金日线取多长（1 月 = 21 根，留足余量）
    "etf_fail_ttl_sec": 600,        # 板块基金日线取不到后多久内不再试
    "session_min_share": 0.05,     # 至少这么大比例的票落在某个美东日上，那天才可能是「这一场」
    "min_spy_rows": 400,          # SPY 文件少于这么多行 ⇒ 当没取到（防半截文件）
}
#: 周期 → 页面名、取哪个字段、色阶封顶（±这么多 % 及以上是最深的颜色）、色块图有没有这个周期。
#: 5 日 / 1 月（v0.45.792）只有顶部那排板块基金有（`map=False`）：成分股要逐只取日线，见模块头。
PERIODS = {
    "1d": {"label": "今日", "field": "change_1d", "clamp_pct": 3.0, "map": True},
    "5d": {"label": "5 日", "field": "change_5d", "clamp_pct": 6.0, "map": False},
    "1m": {"label": "1 月", "field": "change_1m", "clamp_pct": 12.0, "map": False},
    "52w": {"label": "52 周", "field": "change_52w", "clamp_pct": 50.0, "map": True},
}
#: 板块基金的回看周期 → 往回数几个交易日的收盘当基准
ETF_LOOKBACK = {"5d": 5, "1m": 21}
REASONS = {
    "demo": "演示模式不取真实行情",
    "no_fetcher": "这个实例没接取数",
    "holdings_unavailable": "持仓文件取不到",
    "holdings_too_small": "持仓文件不完整",
    "rate_limited": "Yahoo 限流",
    "fetch_error": "取数失败",
    "empty": "接口没返回任何报价",
    "no_quote": "没有报价",
    "bad_price": "现价或昨收缺失",
    "implausible": "涨跌幅异常，按坏数据处理",
    "stale_quote": "最后成交不在这一场（停牌或报价没更新）",
    "no_bars": "这只基金的日线没取到",
    "too_few_bars": "日线不够长",
    "bars_gap": "日线中间缺了交易日，往回数会数错，不出数",
    "session_unknown": "判不了这一场是哪天",
}
DISCLAIMER = ("面积 = 在 SPY 里的权重（流通市值占比），颜色 = 涨跌幅。行情来自 Yahoo，盘中约有 15 分钟延迟；"
              "只展示，不进评分，不构成投资建议。")

_TICKER_OK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ── xlsx（只用标准库：生产解释器的依赖清单里没有表格库，不为一页展示加依赖）──────────────────

def read_xlsx_rows(blob: bytes) -> List[Dict[str, Optional[str]]]:
    """第一张工作表 → `[{列字母: 文本}]`。格式不对抛 `ValueError`（调用方归成 `fetch_error`）。"""
    if len(blob) > RULES["max_xlsx_bytes"]:
        raise ValueError("xlsx_too_large")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise ValueError("not_xlsx") from e
    names = z.namelist()
    sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
    if not sheets:
        raise ValueError("no_sheet")

    def xml(name):
        if z.getinfo(name).file_size > RULES["max_xml_bytes"]:
            raise ValueError("xml_too_large")
        return ET.fromstring(z.read(name))  # noqa: S314 - 见模块顶部 import 处

    shared: List[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in xml("xl/sharedStrings.xml").findall(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    out = []
    for row in xml(sheets[0]).iter(f"{_NS}row"):
        cells: Dict[str, Optional[str]] = {}
        for c in row.findall(f"{_NS}c"):
            col = "".join(ch for ch in (c.get("r") or "") if ch.isalpha())
            v, kind = c.find(f"{_NS}v"), c.get("t")
            if kind == "s" and v is not None and v.text is not None:
                i = int(v.text)
                val = shared[i] if 0 <= i < len(shared) else None
            elif kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
            else:
                val = v.text if v is not None else None
            if col:
                cells[col] = val
        out.append(cells)
    return out


def _num(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def parse_holdings(rows: List[Dict[str, Optional[str]]]) -> dict:
    """一份道富持仓表 → `{as_of, rows: [{ticker, name, weight}], skipped: [...], cash_weight}`。

    列按表头名找（Name / Ticker / Weight），不按位置。现金（代码「-」）单独计；代码不像股票的（期货、或有权利）进 `skipped`。"""
    as_of = None
    for r in rows[:12]:
        if (r.get("A") or "").strip().startswith("Holdings"):
            m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", r.get("B") or "")
            if m:
                try:
                    as_of = datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
                except ValueError:
                    as_of = None
    head_i, cols = None, {}
    for i, r in enumerate(rows):
        names = {(v or "").strip(): k for k, v in r.items()}
        if {"Name", "Ticker", "Weight"} <= set(names):
            head_i, cols = i, names
            break
    if head_i is None:
        raise ValueError("no_header")
    out, skipped, cash = [], [], 0.0
    for r in rows[head_i + 1:]:
        tk = (r.get(cols["Ticker"]) or "").strip().upper()
        w = _num(r.get(cols["Weight"]))
        name = (r.get(cols["Name"]) or "").strip()
        if not tk or w is None:
            continue
        if tk == "-":
            cash += w
        elif _TICKER_OK.match(tk):
            out.append({"ticker": tk, "name": name, "weight": w})
        else:
            skipped.append({"ticker": tk, "name": name, "weight": w})
    return {"as_of": as_of, "rows": out, "skipped": skipped, "cash_weight": round(cash, 6)}


def combine(spy: dict, by_sector: Dict[str, Optional[dict]]) -> dict:
    """SPY 成分 + 各板块文件 → 带板块的成分表。板块文件缺了（None）或某只成分哪只板块都不在 ⇒ `sector = UNSORTED`；
    同一只落进多只板块 ⇒ 取第一只并记进 `multi_sector`（实测没出现过，出现了页面要看得见）。"""
    where: Dict[str, List[str]] = {}
    for etf in SECTORS:
        for r in ((by_sector.get(etf) or {}).get("rows") or []):
            where.setdefault(r["ticker"], []).append(etf)
    cons = [{**r, "sector": (where.get(r["ticker"]) or [UNSORTED])[0]} for r in spy["rows"]]
    return {
        "as_of": spy.get("as_of"),
        "constituents": cons,
        "skipped": list(spy.get("skipped") or []),
        "cash_weight": spy.get("cash_weight"),
        "failed_sectors": [e for e in SECTORS if not by_sector.get(e)],
        "multi_sector": {t: v for t, v in where.items() if len(v) > 1 and any(c["ticker"] == t for c in cons)},
        "sector_as_of": {e: (by_sector.get(e) or {}).get("as_of") for e in SECTORS},
    }


# ── 取数（永不抛）────────────────────────────────────────────────────────────

def _default_http_get(url: str) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})   # 道富对缺省的 Python UA 回 403
    with urllib.request.urlopen(req, timeout=40) as r:   # noqa: S310 - 固定的公开 https 地址
        return r.read(RULES["max_xlsx_bytes"] + 1)


def fetch_holdings(http_get: Optional[Callable[[str], bytes]] = None) -> Tuple[Optional[dict], dict]:
    """12 份持仓文件（SPY + 11 只板块）→ `(combine() 的结果, {reason, detail?})`。SPY 那份取不到 = 整体失败；
    板块文件缺几份不算失败（那几个板块的成分归「未归类」，`failed_sectors` 点名）。"""
    get = http_get or _default_http_get

    def one(etf):
        err = None
        for _ in range(int(RULES["holdings_attempts"])):     # 道富偶尔单份超时（2026-11-19 实测 XLU 掉过一次）：当场再试一次
            try:
                return etf, parse_holdings(read_xlsx_rows(get(HOLDINGS_URL.format(etf.lower())))), None
            except Exception as e:  # noqa: BLE001 - 单份失败不连坐
                err = type(e).__name__
        return etf, None, err
    try:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="sector-holdings") as pool:
            got = list(pool.map(one, [BENCH, *SECTORS]))
    except Exception as e:  # noqa: BLE001
        return None, {"reason": "fetch_error", "detail": type(e).__name__}
    parsed = {etf: p for etf, p, _ in got}
    errs = {etf: err for etf, _, err in got if err}
    spy = parsed.get(BENCH)
    if not spy:
        return None, {"reason": "holdings_unavailable", "detail": errs.get(BENCH)}
    if len(spy["rows"]) < RULES["min_spy_rows"]:
        return None, {"reason": "holdings_too_small", "detail": f"{len(spy['rows'])} 行"}
    return combine(spy, parsed), {"reason": None, "errors": errs}


def yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-")


def _quote_row(q: dict) -> dict:
    return {"price": _num(q.get("regularMarketPrice")), "prev_close": _num(q.get("regularMarketPreviousClose")),
            "change_52w_pct": _num(q.get("fiftyTwoWeekChangePercent")), "market_cap": _num(q.get("marketCap")),
            "time": _num(q.get("regularMarketTime")), "state": q.get("marketState"),
            "delay_min": _num(q.get("exchangeDataDelayedBy")), "name": q.get("shortName") or q.get("longName")}


def fetch_quotes(symbols: Iterable[str], yf_data=None) -> Tuple[Optional[Dict[str, dict]], dict]:
    """Yahoo 报价 → `({Yahoo 代码: {price, prev_close, …}}, {reason, retry_after_sec?, n_requests})`。

    `yf_data`：带 `.get(url, params=, timeout=)` 的对象（缺省 `yfinance.data.YfData()`，它管 cookie / crumb）；注入时不过限流闸。
    某一块失败 ⇒ 整体失败（半张图比没有图更容易看错）。"""
    syms = list(dict.fromkeys(symbols))
    gated: Callable = lambda fn, what: fn()   # noqa: E731
    n_req = 0
    try:
        if yf_data is None:
            from yfinance.data import YfData  # noqa: PLC0415
            yf_data = YfData()
            try:
                import yf_gate  # noqa: PLC0415
                gated = yf_gate.gated_call
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断（与 price_ohlc / beta_long 同）
                pass
        out: Dict[str, dict] = {}
        size = int(RULES["quote_chunk"])
        for i in range(0, len(syms), size):
            part = syms[i:i + size]

            def call(part=part):
                r = yf_data.get(QUOTE_URL, params={"symbols": ",".join(part)}, timeout=20)
                code = getattr(r, "status_code", 200)
                if code == 429:
                    raise RuntimeError("Too Many Requests")      # 闸门按这句话认限流并进冷却
                if code != 200:
                    raise RuntimeError(f"http_{code}")
                return r.json()
            js = gated(call, "sector_map.quote")
            n_req += 1
            for q in ((js or {}).get("quoteResponse") or {}).get("result") or []:
                if isinstance(q, dict) and q.get("symbol"):
                    out[str(q["symbol"]).upper()] = _quote_row(q)
        if not out:
            return None, {"reason": "empty", "n_requests": n_req}
        return out, {"reason": None, "n_requests": n_req}
    except Exception as exc:  # noqa: BLE001 - 取不到只是这张图画不出来，页面照实写
        if BL._rate_limited(exc):
            return None, {"reason": "rate_limited", "retry_after_sec": BL._retry_after(), "n_requests": n_req}
        return None, {"reason": "fetch_error", "detail": f"{type(exc).__name__}: {str(exc)[:80]}", "n_requests": n_req}


def _fetch_closes(yf_module, symbol: str, ensure: Callable[[], None]) -> Dict[str, float]:
    import yf_daily  # noqa: PLC0415
    ensure()
    # 末根是不是公司行动空行不用管：只按日期取「这一场之前」的收盘（登记的策略 = ignore）。source 必须是字面量（守卫按字面量核对注册表）
    hist, _ = yf_daily.history(yf_module.Ticker(symbol), source="alphabot_sector_etf", period=RULES["etf_period"],
                               auto_adjust=True)
    return BL._closes_from_history(hist)


def fetch_etf_closes(symbols: Iterable[str], yf_module=None) -> Tuple[Optional[Dict[str, Dict[str, float]]], dict]:
    """SPY + 板块基金的复权日线 → `({代码: {日期: 收盘}}, {reason, errors, retry_after_sec?})`。一只一个请求（12 只）。
    单只取不到不连坐（记进 `errors`，页面那一格写原因）；被限流 ⇒ 其后的不再去撞。一只都没有才整体失败。永不抛。"""
    out: Dict[str, Dict[str, float]] = {}
    errs: Dict[str, str] = {}
    ensure: Callable[[], None] = lambda: None  # noqa: E731
    try:
        if yf_module is None:
            import yfinance as yf_module  # noqa: PLC0415
            try:
                from yf_gate import ensure  # noqa: PLC0415
            except Exception:  # noqa: BLE001 - 闸门不可得不阻断（与 beta_long / price_ohlc 同）
                pass
        for s in dict.fromkeys(symbols):
            try:
                c = _fetch_closes(yf_module, s, ensure)
            except Exception as exc:  # noqa: BLE001 - 单只取不到不连坐其余
                if BL._rate_limited(exc):
                    meta = {"reason": "rate_limited", "retry_after_sec": BL._retry_after(), "errors": errs}
                    return (out or None), meta
                errs[s] = type(exc).__name__
                continue
            if c:
                out[s] = c
            else:
                errs[s] = "empty"
    except Exception as exc:  # noqa: BLE001 - 导入 yfinance 失败等
        return None, {"reason": "fetch_error", "detail": type(exc).__name__, "errors": errs}
    if not out:
        return None, {"reason": "fetch_error", "detail": "一只都没取到", "errors": errs}
    return out, {"reason": None, "errors": errs}


# ── 排图（纯函数）────────────────────────────────────────────────────────────

def _et_date(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(NY).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _change(q: Optional[dict], session: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """`(今日涨跌 %, 画不出来的原因码)`。"""
    if not q:
        return None, "no_quote"
    p, prev = q.get("price"), q.get("prev_close")
    if not (p and prev and p > 0 and prev > 0):
        return None, "bad_price"
    if session and _et_date(q.get("time")) != session:
        return None, "stale_quote"
    chg = (p / prev - 1.0) * 100.0
    if abs(chg) > RULES["implausible_pct"]:
        return None, "implausible"
    return round(chg, 3), None


def pick_session(days: Counter) -> Optional[str]:
    """「这一场」= 至少有 `session_min_share` 的票落在上面的**最晚**那个美东日。

    不取「最多的那天」：开盘头一两分钟只有一部分票成交过，那样会把已经开盘的票标成「不在这一场」、
    而没开盘的票照画昨天的涨跌。也不直接取最晚的一天：一只票的时间戳错了不该把整张图变灰。"""
    total = sum(days.values())
    if not total:
        return None
    ok = [d for d, n in days.items() if n >= max(1, total * RULES["session_min_share"])]
    return max(ok) if ok else days.most_common(1)[0][0]


def lookback_change(closes: Optional[Dict[str, float]], price: Optional[float], session: Optional[str], n: int,
                    is_trading: Optional[Callable[[str], bool]] = None) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """`(n 个交易日的涨跌 %, 基准日, 原因码)`：现价 ÷「这一场」往回数第 n 个交易日的复权收盘 − 1（含分红）。

    只用日期早于 `session` 的收盘（今天那根没走完的不算）。按位置往回数有个老毛病——日线中间缺一根就无声数错一天；
    所以给了 `is_trading`（日期 → 是不是交易日）时，要求基准日之后到这一场之间**恰好** n 个交易日，对不上 ⇒ `bars_gap`、不出数。"""
    if not session:
        return None, None, "session_unknown"
    if not closes:
        return None, None, "no_bars"
    if not (price and price > 0):
        return None, None, "bad_price"
    days = sorted(d for d in closes if d < session)
    if len(days) < n:
        return None, None, "too_few_bars"
    base_day = days[-n]
    base = closes[base_day]
    if not (base and base > 0):
        return None, base_day, "bad_price"
    if is_trading is not None:
        try:
            d0, d1 = datetime.fromisoformat(base_day).date(), datetime.fromisoformat(session).date()
            span = sum(1 for i in range(1, (d1 - d0).days + 1) if is_trading((d0 + timedelta(days=i)).isoformat()))
        except Exception:  # noqa: BLE001 - 日历判不了 ⇒ 不出数，比数错一天好
            return None, base_day, "bars_gap"
        if span != n:
            return None, base_day, "bars_gap"
    return round((price / base - 1.0) * 100.0, 2), base_day, None


def _wavg(pairs: List[Tuple[float, Optional[float]]]) -> Optional[float]:
    num = sum(w * v for w, v in pairs if v is not None)
    den = sum(w for w, v in pairs if v is not None)
    return round(num / den, 3) if den > 0 else None


def build(holdings: dict, quotes: Dict[str, dict], *, watchlist: Iterable[str] = (),
          etf_closes: Optional[Dict[str, Dict[str, float]]] = None,
          is_trading: Optional[Callable[[str], bool]] = None) -> dict:
    """成分表 + 报价 → 页面要的树。「这一场」= 报价里最多的那个美东日（停牌的票停在更早的一天 ⇒ `stale_quote`、不上色）。

    板块今日涨跌给两个数：`change_1d`（成分按 SPY 权重加权，只算有报价的）与 `etf`（那只板块 SPDR 自己的报价）——
    两者权重口径不同（板块基金有单票上限），差一点是正常的。52 周**只**给板块基金自己的：拿现在的权重去加权过去一年的涨幅
    会偏向涨得多的票（实测科技板块加权 33% 对基金 20%），那个数不出。"""
    wl = {str(t).upper() for t in watchlist}
    q_of = lambda tk: quotes.get(yahoo_symbol(tk).upper())   # noqa: E731
    days = Counter(d for d in (_et_date((quotes.get(yahoo_symbol(c["ticker"]).upper()) or {}).get("time"))
                               for c in holdings["constituents"]) if d)
    session = pick_session(days)
    states = Counter((q_of(c["ticker"]) or {}).get("state") for c in holdings["constituents"] if q_of(c["ticker"]))
    delays = [q["delay_min"] for q in quotes.values() if q.get("delay_min") is not None]
    last_ts = max((q["time"] for c in holdings["constituents"] if (q := q_of(c["ticker"])) and q.get("time")), default=None)

    groups: Dict[str, List[dict]] = {}
    missing: List[dict] = []
    for c in holdings["constituents"]:
        q = q_of(c["ticker"])
        chg, why = _change(q, session)
        leaf = {"ticker": c["ticker"], "name": (q or {}).get("name") or c["name"], "weight": round(c["weight"], 6),
                "price": (q or {}).get("price"), "prev_close": (q or {}).get("prev_close"), "change_1d": chg,
                "change_52w": None if why else (round(q["change_52w_pct"], 2) if q.get("change_52w_pct") is not None else None),
                "market_cap": (q or {}).get("market_cap"), "reason": why, "in_watchlist": c["ticker"] in wl}
        if why:
            missing.append({"ticker": c["ticker"], "weight": leaf["weight"], "reason": why})
        groups.setdefault(c["sector"], []).append(leaf)

    def etf_block(sym):
        q = quotes.get(sym)
        chg, why = _change(q, session)
        out = {"symbol": sym, "price": (q or {}).get("price"), "change_1d": chg, "reason": why,
               "change_52w": None if why or not q or q.get("change_52w_pct") is None else round(q["change_52w_pct"], 2)}
        # 5 日 / 1 月：这一场的报价不可用（why）⇒ 现价不可信，也不出；日线没接（etf_closes is None）⇒ 整节由 `etf_periods` 说明
        for key, n in ETF_LOOKBACK.items():
            field = PERIODS[key]["field"]
            if why or etf_closes is None:
                out[field], out[f"{field}_base"], out[f"{field}_reason"] = None, None, (why or None)
                continue
            v, base_day, bad = lookback_change(etf_closes.get(sym), (q or {}).get("price"), session, n, is_trading)
            out[field], out[f"{field}_base"], out[f"{field}_reason"] = v, base_day, bad
        return out

    sectors = []
    for key in [*SECTORS, UNSORTED]:
        kids = sorted(groups.get(key) or [], key=lambda x: -x["weight"])
        if not kids:
            continue
        sectors.append({
            "key": key, "name": SECTORS.get(key, "未归类"), "weight": round(sum(k["weight"] for k in kids), 4),
            "n": len(kids), "n_quoted": sum(1 for k in kids if k["reason"] is None),
            "change_1d": _wavg([(k["weight"], k["change_1d"]) for k in kids]),
            "etf": etf_block(key) if key in SECTORS else None, "children": kids})
    sectors.sort(key=lambda s: -s["weight"])
    leaves = [k for s in sectors for k in s["children"]]
    n_ok = sum(1 for k in leaves if k["reason"] is None)
    return {
        "session_date": session,
        "market_state": states.most_common(1)[0][0] if states else None,
        "delay_min": max(delays) if delays else None,
        "last_trade_et": (datetime.fromtimestamp(last_ts, timezone.utc).astimezone(NY).isoformat(timespec="minutes")
                          if last_ts else None),
        "bench": etf_block(BENCH) | {"change_1d_weighted": _wavg([(k["weight"], k["change_1d"]) for k in leaves])},
        "sectors": sectors,
        "stats": {"n": len(leaves), "n_quoted": n_ok, "n_missing": len(leaves) - n_ok,
                  "weight_quoted": round(sum(k["weight"] for k in leaves if k["reason"] is None), 3),
                  "weight_total": round(sum(k["weight"] for k in leaves), 3),
                  "missing": sorted(missing, key=lambda m: -m["weight"]),
                  "up": sum(1 for k in leaves if (k["change_1d"] or 0) > 0),
                  "down": sum(1 for k in leaves if (k["change_1d"] or 0) < 0),
                  "flat": sum(1 for k in leaves if k["change_1d"] == 0)},
    }


def carry_sectors(new: dict, old: Optional[dict]) -> dict:
    """这次有板块文件没取到 ⇒ 那几只成分的板块沿用上一份成分表里的（板块归属几乎不变；沿用了几只记在 `carried`）。
    上一份里也没有的仍是「未归类」。没缺文件 / 没有上一份 ⇒ 原样返回。"""
    if not new.get("failed_sectors") or not old:
        return new
    prev = {c["ticker"]: c.get("sector") for c in old.get("constituents") or []}
    cons, n = [], 0
    for c in new["constituents"]:
        was = prev.get(c["ticker"])
        if c["sector"] == UNSORTED and was in new["failed_sectors"]:
            c, n = {**c, "sector": was}, n + 1
        cons.append(c)
    return {**new, "constituents": cons, "carried": n}


def symbols_for(holdings: dict) -> List[str]:
    """要问报价的全部代码：成分 + 11 只板块基金 + SPY。"""
    return [yahoo_symbol(c["ticker"]) for c in holdings["constituents"]] + [*SECTORS, BENCH]


def public_rules() -> dict:
    return {"reasons": dict(REASONS), "periods": {k: dict(v) for k, v in PERIODS.items()},
            "sectors": dict(SECTORS), "bench": BENCH, "etf_lookback": dict(ETF_LOOKBACK), "quote_ttl_live_sec": RULES["quote_ttl_live_sec"],
            "quote_ttl_idle_sec": RULES["quote_ttl_idle_sec"], "implausible_pct": RULES["implausible_pct"]}
