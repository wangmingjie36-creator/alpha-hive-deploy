#!/usr/bin/env python3
"""
CBOE 延迟报价期权链获取器 — yfinance 限流/不可用时的真实数据降级源

数据源：CBOE（芝加哥期权交易所）公开延迟报价 JSON
    https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json
特点：全链逐合约 OI/IV/greeks，15 分钟延迟（盘后=已结算 EOD），无 API key、无限流。

产出格式与 options_analyzer.OptionsAgent.fetch_options_chain 完全一致：
    {ticker, timestamp, calls:[...], puts:[...], expirations:[...], near_expiry_set:[...]}
每个 call/put 记录键：strike / openInterest / impliedVolatility / gamma / expiry /
    dte / dte_weight / bid / ask / volume / lastPrice / contractSymbol。
失败一律返回 None（调用方据此再降级到样本数据）。

设计原则：纯 urllib（零额外依赖）、镜像 yfinance 路径的后处理（到期日筛选 / ATM 过滤 /
40-strike 上限 / DTE 加权 / gamma 注入），保证下游 GEX / Max Pain / OI 墙零改动复用。
"""
from __future__ import annotations

import json
import math
import re
import statistics
import threading
import time
import urllib.request
from datetime import datetime, time as _dtime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.cboe_options")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.cboe_options")

_CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"


def _cboe_symbol(ticker: str) -> str:
    """项目内部 ticker → CBOE URL 用的符号。

    类份额标的的写法各家不同：项目内部（含 config.WATCHLIST / yfinance）用
    **连字符** `BRK-B`，而 CBOE 用**点** `BRK.B`。2026-08-25 实测：
    `BRK.B` 返回 2054 个合约，`BRK-B` 与 `BRKB` 均 **403 Forbidden**。

    后果不是报错而是**全链降级**：`fetch_cboe_chain` / `fetch_cboe_full_chain_oi`
    / `fetch_cboe_iv_term_structure` 三处主源一起失败，BRK-B 每次扫描都白烧
    3 次 CBOE 重试（约 5s）再整体退回 yfinance——恰好落回 SSL 风暴高发路径。
    BRK-B 是 30 只每日扫描标的之一，且在 v0.45.2 有过被静默中性化的前科。

    注意只规范化 **URL**；`_payload_cache` 的键、日志、返回结构一律沿用调用方
    传入的原始 ticker，避免上下游出现第二种写法。
    OCC 合约符号不受影响——CBOE 返回的是 `BRKB260828C00270000`，
    现有 `_OCC` 正则 `^([A-Z]+)…` 照常匹配（实测 2054/2054 全部解析成功）。
    """
    return ticker.upper().replace("-", ".")
# OCC 合约符号：NVDA 260702 C 00200000 → 标的 / YYMMDD / C|P / 8 位行权价(×1000)
_OCC = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_BS_RISK_FREE = 0.045  # 与 options_analyzer 一致的参考无风险利率
_ATM_LO, _ATM_HI = 0.30, 1.70  # ATM 过滤区间（±70%），与 yfinance 路径一致
_MAX_STRIKES_PER_SIDE = 40      # 每到期日每边最多保留 40 strike（按 OI），内存保护
# IV 期限结构取点：与 options_analyzer yfinance 路径同一组目标 DTE，保证口径可比。
# 下界 7 天剔除 theta 扭曲的临期合约，上界 270 天剔除 LEAPS（把 842 DTE 当"远端"
# 会让 spread 与历史已发布数值不同源）。
_TS_TARGET_DTE = (25, 55, 85, 150)
_TS_MIN_DTE, _TS_MAX_DTE = 7, 270

# 串行化 CBOE 网络请求（信号量限 1）：不给对端限流器加压。
# v0.43.27: 指向全进程唯一的 HTTPS 闸门。此前这把锁只保护 CBOE，
# yfinance/Finnhub/AlphaVantage 各走各的，不受任何闸门约束
# （8/24 那天全天 96 次 SSL EOF、Step 2 超时被杀、当天零产出）。
# ⚠️ 这里原写「本机老 SSL 栈（LibreSSL 2.8.3）扛不住并发」，并附「4 并发挂 50-70s /
# 顺序 8-11s」的实测——**该归因 2026-08-25 已被重测证伪，那组数字是单次取样**
# （详见 http_gate 模块 docstring）。顺带：LibreSSL 2.8.3 是系统 /usr/bin/python3
# 3.9.6 的 TLS 栈，生产扫描跑的是 3.11.1/OpenSSL 1.1.1q，根本不是它。
# 96 次 EOF 是真的，但根因至今未定——复发时抓现场，别照版本号推断。
try:
    from http_gate import _GATE as _CBOE_SEM
except Exception:  # pragma: no cover - 闸门不可得时退回本地锁，至少保住 CBOE
    _CBOE_SEM = threading.Semaphore(1)
# 进程内 payload 缓存：同一标的的主链(fetch_cboe_chain)与全链(fetch_cboe_full_chain_oi)
# 共享一次下载，避免每标的拉 2 次大 JSON。短 TTL 防长驻进程取到陈旧数据。
_payload_cache = {}  # ticker -> (timestamp, data)
_cache_lock = threading.Lock()
_CACHE_TTL = 120.0

# ── 快照供给器（v0.45.38）：补跑历史交易日时接管全部取数 ──────────
# 装载后本模块四个取数入口一律走快照，**不回落实时抓取** ——
# 补跑的是过去某天，实时抓取会拿到今天的链再贴上那天的日期，
# 与 v0.45.36 拦下的污染同源，只是方向相反。装卸见 cloud_snapshot_loader。
_SNAPSHOT_PROVIDER = None

# ── CBOE CDN 陈旧文件防线（v0.45.39）────────────────────────────────
# CBOE 的 CDN 对某些符号**不重新生成文件**：HTTP 200、字段齐全、看不出异常，
# 但整份 JSON（现价/期权链/IV/OI）停在旧日期。2026-08-26 实测 TMO 卡了
# 44.5 小时（现价是 8/24 收盘），TMUS 滞后约 20 分钟后自愈。
# 历史对账：pheromone.db 877 条可对账样本里 13 条（1.5%）的 price_at_predict
# 精确等于更早某日收盘，2026-07-24 一天中了 8 只。
# 检出后返回 None → data_pipeline 落到 YFinanceSource（降级源 0）拿正确数据。
_ET_TZ = ZoneInfo("America/New_York")
_vintage_stats = {"checked": 0, "stale": 0}


class CboeStaleVintageError(RuntimeError):
    """CDN 发的是更早交易日的文件。**只在调用方显式要求时抛**（`on_stale="raise"`）。

    v0.45.91：在此之前，陈旧一律 `return None` —— 和「网络失败 / 403 / 空期权链」
    共用同一个出口，调用方无从分辨。实时路径不在乎（三者都该降级到 yfinance），
    但云端快照在乎：它的补抓 pass 只认「陈旧」这一类，靠 `StaleVintageError`
    分流。信号被压平后那条分支成了死代码，08-28 BILI / 08-31 TMO / 09-01 BILI
    连着三次都以泛化 `RuntimeError: CBOE payload 为空` 落进普通失败，
    一次都没被补抓过。
    """

    def __init__(self, ticker: str, vintage_date: str, expected_date: Optional[str]):
        self.ticker = ticker
        self.vintage_date = vintage_date
        self.expected_date = expected_date
        super().__init__(f"CBOE {ticker} 数据陈旧：last_trade={vintage_date}，"
                         f"应为 {expected_date}")


def _expected_vintage_date() -> Optional[str]:
    """本时点**应当**拿到的数据日期（ET 日历日）；无法判定返回 None。

    开盘前拿到上一交易日的收盘天经地义，开盘后就该是今天 —— 所以判据随
    时间变化，分界取 ET 09:30。

    ⚠️ fail-open：交易日历不可用时返回 None，调用方**跳过**校验。
    宁可放过陈旧数据，也不能因为日历挂了把 30 只全打成陈旧、连锁压到
    yfinance 上 —— 7/23 那次限流雪崩就是这么来的。
    """
    try:
        from is_trading_day import is_trading_day
        now = datetime.now(_ET_TZ)
        today = now.date()
        if is_trading_day(today)[0] and now.time() >= _ET_OPEN:
            return today.isoformat()
        d = today - timedelta(days=1)
        for _ in range(10):          # 长假最多跨约 5 天，10 天足够
            if is_trading_day(d)[0]:
                return d.isoformat()
            d -= timedelta(days=1)
    except Exception:  # noqa: BLE001 —— 见上方 fail-open 说明
        _log.debug("交易日历不可用，跳过 CBOE vintage 校验", exc_info=True)
    return None


def _payload_stale_vintage(ticker: str, data: dict) -> Optional[tuple]:
    """陈旧则返回 `(实际日期, 应有日期)`，否则 None。判不了返回 None（fail-open）。

    统计与告警都记在这里，所以每份 payload 只该调用一次
    —— `_payload_is_stale` 是它的布尔外壳，两者别串着调。
    """
    expected = _expected_vintage_date()
    raw = (data or {}).get("last_trade_time")
    if not expected or not raw or not isinstance(raw, str):
        return None
    try:
        dt_ = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt_.tzinfo is not None:
        dt_ = dt_.astimezone(_ET_TZ)
    got = dt_.strftime("%Y-%m-%d")
    _vintage_stats["checked"] += 1
    if got >= expected:
        return None
    _vintage_stats["stale"] += 1
    _log.warning("CBOE %s 数据陈旧：last_trade=%s，应为 %s —— 弃用本源，交由降级链",
                 ticker, got, expected)
    c, st = _vintage_stats["checked"], _vintage_stats["stale"]
    if c >= 8 and st / c > 0.5:
        _log.error("CBOE vintage 陈旧率 %d/%d >50%% —— 疑似校验口径错误或 CBOE "
                   "大面积故障，请核对；此刻全部标的正在落到 yfinance", st, c)
    return got, expected


def _payload_is_stale(ticker: str, data: dict) -> bool:
    """payload 的成交时刻是否早于应有日期。判不了一律返回 False（fail-open）。"""
    return _payload_stale_vintage(ticker, data) is not None


def invalidate_payload_cache(ticker: Optional[str] = None) -> None:
    """清掉进程内 payload 缓存（传 None 清全部）。供陈旧标的稍后重试用。"""
    with _cache_lock:
        if ticker is None:
            _payload_cache.clear()
        else:
            _payload_cache.pop(ticker.upper(), None)


def set_snapshot_provider(fn) -> None:
    """装载/卸载快照供给器。`fn(ticker) -> dict | None`；传 None 卸载。

    刻意放在本模块而不是让调用方各自 monkeypatch：四个消费点
    （options_analyzer ×3、oracle_bee ×1）都是函数内 `from cboe_options import X`，
    在本模块内拦截即全覆盖，调用方一行不用改。
    """
    global _SNAPSHOT_PROVIDER
    _SNAPSHOT_PROVIDER = fn


def _snapshot(ticker: str):
    """返回该标的的快照 dict；未装载供给器时返回 None。"""
    if _SNAPSHOT_PROVIDER is None:
        return None
    try:
        return _SNAPSHOT_PROVIDER(ticker)
    except Exception:  # noqa: BLE001 - 供给器故障不得连累实时路径判断
        _log.exception("快照供给器对 %s 抛错", ticker)
        return None


def _bs_gamma(S: float, K: float, T: float, sigma: float) -> float:
    """Black-Scholes gamma — CBOE gamma 为 0（深 ITM/低流动）时兜底，与 options_analyzer 同公式"""
    if S <= 0 or K <= 0 or T <= 1e-6 or sigma < 0.01:
        return 0.0
    try:
        d1 = (math.log(S / K) + (_BS_RISK_FREE + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _parse_occ(sym: str) -> Optional[tuple]:
    """OCC 符号 → (expiry 'YYYY-MM-DD', 'C'|'P', strike float)；非法返回 None"""
    m = _OCC.match(sym or "")
    if not m:
        return None
    _tk, yy, mm, dd, cp, strike = m.groups()
    try:
        expiry = f"20{yy}-{mm}-{dd}"
        # 校验是合法日期
        datetime.strptime(expiry, "%Y-%m-%d")
        return expiry, cp, int(strike) / 1000.0
    except ValueError:
        return None


def _pdt_now() -> datetime:
    """PDT 锚定的 naive datetime。项目硬规则：绝不用裸 datetime.now()——用户本机钟
    可偏移 ~15h（UTC 绝对时间但时区 Asia/Shanghai），裸 now() 会把 DTE 算错 ±1 天、
    误判近月/远月。锚 America/Los_Angeles 得正确美股日期。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
    except Exception:  # pragma: no cover - zoneinfo 不可用时退裸 now
        return datetime.now()


def _select_expiries(by_expiry: Dict[str, dict], today: datetime, max_expiries: int = 4):
    """镜像 yfinance 路径：DTE≥3，优先 DTE≥7 前 4 + DTE 3-6 前 2，封顶 max_expiries。
    返回 (选中到期日列表, near_expiry_set=DTE<7)。"""
    dte_pairs = []
    for e in sorted(by_expiry.keys()):
        try:
            dte = (datetime.strptime(e, "%Y-%m-%d") - today).days
        except ValueError:
            continue
        if dte >= 3:
            dte_pairs.append((e, dte))
    far = [e for e, d in dte_pairs if d >= 7][:4]
    near = [e for e, d in dte_pairs if 3 <= d < 7][:2]
    chosen = (far + near)[:max_expiries] if far else [e for e, _ in dte_pairs[:max_expiries]]
    near_set = {e for e, d in dte_pairs if d < 7}
    return chosen, list(near_set)


def _fetch_cboe_payload(ticker: str, timeout: int, *, retries: int = 3,
                        on_stale: str = "none") -> Optional[dict]:
    """拉取 CBOE 延迟报价 JSON，返回 data 段（含 options / current_price / close）；失败返回 None。

    `on_stale` 决定检出 CDN 陈旧文件时怎么办：
      - `"none"`（默认）→ 返回 None，与网络失败同一个出口。实时路径要的就是这个：
        三种失败都该降级到 yfinance，分辨它们没有意义。
      - `"raise"` → 抛 `CboeStaleVintageError`。云端快照要的是这个：它必须把
        「陈旧」（等 CDN 刷新就能补回）和「真失败」（补也白补）分开处理。
        默认值保持 `"none"`，实时路径行为逐字节不变。

    串行化（`_CBOE_SEM` 限 1）：全进程一次只发一个出站请求，不给 CBOE 的限流器加压
    （⚠️ **不是**因为并发压垮 TLS 栈——那条归因 2026-08-25 已证伪，见 http_gate docstring）。
    进程缓存：同标的主链+全链共享一次下载。重试退避：瞬时网络故障错开即恢复。
    """
    if on_stale not in ("none", "raise"):
        # 拼错就静默退回旧行为 = 这次事故的同一形态，宁可当场炸。
        raise ValueError(f"on_stale 只接受 'none' / 'raise'，收到 {on_stale!r}")

    # 快照模式不做 vintage 校验：补跑的快照在**落盘时**已经验过一次
    # （cloud_snapshot_fetch 拒绝陈旧才写文件），这里再验一次会拿今天的
    # 日历去判那天的数据。`on_stale` 在这条路径上无意义。
    if _SNAPSHOT_PROVIDER is not None:
        snap = _snapshot(ticker)
        if not snap:
            return None
        # 合成最小 payload：快照不存原始 options 数组（体积 10 倍），
        # 但快照模式下三个解析函数都短路了，没人会去读它。
        return {"current_price": snap.get("price_at_fetch"),
                "close": snap.get("price_at_fetch"),
                "last_trade_time": snap.get("last_trade_time_et"),
                "prev_day_close": snap.get("prev_day_close"),
                "_from_snapshot": True}

    key = ticker.upper()
    now = time.time()
    with _cache_lock:
        hit = _payload_cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

    url = _CBOE_URL.format(_cboe_symbol(key))
    last_err = None
    for attempt in range(retries):
        try:
            with _CBOE_SEM:  # 串行化：不给对端限流器加压（非 TLS 栈原因，见 http_gate）
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=timeout).read()
            data = (json.loads(raw) or {}).get("data") or {}
            if not data.get("options"):
                _log.warning("CBOE %s 返回空期权链", ticker)
                return None
            # v0.45.39：陈旧 CDN 文件在此拦下。**不写缓存** ——
            # 写了就等于把陈旧数据在进程内又保鲜 120 秒。
            stale = _payload_stale_vintage(ticker, data)
            if stale is not None:
                if on_stale == "raise":
                    raise CboeStaleVintageError(ticker, stale[0], stale[1])
                return None
            with _cache_lock:
                _payload_cache[key] = (now, data)
            return data
        except CboeStaleVintageError:
            # 必须先于下面的兜底 except 捕获，否则会被当成网络故障吞掉、
            # 白白重试 3 次、最后仍旧 return None —— 那等于这个参数没写。
            # 重试本身也无意义：同一份 CDN 文件，再拉几次还是同一天的。
            raise
        except Exception as e:  # 网络/SSL/解析失败 → 退避重试
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.7 * (attempt + 1))
    _log.warning("CBOE 拉取 %s 失败（重试 %d 次耗尽）：%s", ticker, retries, last_err)
    return None



# ────────────────────────────────────────────────────────────────────────────
# 官方收盘价 vs 盘后价（v0.45.46）
# ────────────────────────────────────────────────────────────────────────────
# CBOE payload 同时给 `current_price` 与 `close`，含义完全不同：
#   current_price —— **最近一笔成交**。收盘后它是**盘后价**（延长时段到 20:00 ET）
#   close         —— 该交易日的**官方收盘价**
#
# 全部定时扫描都在 14:00 PDT（= 17:00 ET）跑，即收盘之后、盘后时段之内。
# 旧代码三处一律 `current_price or close`，于是记录的一直是**盘后价**。
#
# 2026-08-26 实测（CBOE 实拉，对照 yfinance 官方收盘）：
#   CRM   current_price=232.3187  close=205.62  ← 当日财报，盘后 +12.98%
#   NVDA  current_price=219.53    close=209.66  ← 盘后 +4.71%
#   MSFT  current_price=495.94    close=496.37  ← 盘后 −0.09%
# 三只的 `close` 与 yfinance 官方收盘**逐分不差**。
#
# 这个偏差不是小数点问题：`price_at_predict` 是所有收益计算的**入场价**
# （backtester / dynamic_exit_backtest / ic_diagnostics）。用盘后价当入场价，
# 等于假设能在财报公布后、以盘后价成交——收益全错。
#
# 判据：**盘中用 current_price，收盘后用 close，绝不在收盘后用 current_price。**
_ET_OPEN = _dtime(9, 30)
_ET_CLOSE = _dtime(16, 0)


def _et_now() -> "datetime":
    """当前美东时间。

    v0.45.41 合并时改用 `ZoneInfo`：原实现是自述的「粗略 DST 近似」
    （`-4 if 3 <= month <= 11 else -5`），而 2026 年夏令时 3/8 起、11/1 止 ——
    3/1–3/7 与 11/2–11/30 共约 37 天会算早一小时。后果正是 v0.45.46 要修的
    那一类：真实 08:30 ET（盘前）被算成 09:30 → `is_market_open` 判为盘中
    → 取 `current_price`（盘前价）而不是 `close`。
    """
    return datetime.now(_ET_TZ)


def is_market_open(now_et: "Optional[datetime]" = None) -> bool:
    """美股常规时段（09:30–16:00 ET 工作日）。不含盘前/盘后。"""
    et = now_et or _et_now()
    return et.weekday() < 5 and _ET_OPEN <= et.time() < _ET_CLOSE


def official_price(payload: dict, now_et: "Optional[datetime]" = None) -> Tuple[float, str]:
    """从 CBOE payload 取「该用的」股价。

    Returns
    -------
    (price, source)  source ∈ {"cboe_intraday", "cboe_close", "unavailable"}
        盘中   → current_price（实时成交价才是此刻的真实价格）
        收盘后 → close（官方收盘价）；**不回退到 current_price**，
                 那是盘后价，回退等于把这个 bug 原样放回来。
    取不到返回 (0.0, "unavailable") —— 由调用方决定怎么处理，不猜。
    """
    if not isinstance(payload, dict):
        return 0.0, "unavailable"

    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        # 必须 isfinite：`inf > 0` 为真，只判正数会让 inf 当成合法价格漏过去。
        # Python 的 json.loads 默认接受 `Infinity` 字面量，所以这不是纯理论问题。
        # （NaN 恰好被 `> 0` 挡住，inf 不会 —— 二次检查实测发现。）
        return f if (math.isfinite(f) and f > 0) else None

    if is_market_open(now_et):
        px = _num(payload.get("current_price")) or _num(payload.get("close"))
        return (px, "cboe_intraday") if px else (0.0, "unavailable")

    px = _num(payload.get("close"))
    return (px, "cboe_close") if px else (0.0, "unavailable")


def fetch_cboe_chain(
    ticker: str,
    stock_price: float = 0.0,
    *,
    timeout: int = 15,
    max_expiries: int = 4,
) -> Optional[Dict]:
    """拉取并解析 CBOE 期权链 → options_analyzer 兼容 result dict；任何失败返回 None。"""
    if _SNAPSHOT_PROVIDER is not None:
        return (_snapshot(ticker) or {}).get("chain")

    data = _fetch_cboe_payload(ticker, timeout)
    if not data:
        return None
    options = data["options"]

    # 现价：优先入参 → 按交易时段选 CBOE 字段（见 official_price 的长注释）
    S = float(stock_price or 0.0) or official_price(data)[0]

    # ── 解析全部合约，按到期日分组 ─────────────────────────────
    # CBOE 每合约 iv 已是小数（实测 ATM ~0.18-0.34，与 yfinance impliedVolatility 同尺度），
    # 直接用；深 ITM/低流动合约 iv=0 由下方 BS gamma 兜底。不做百分数/小数启发式检测，
    # 避免对高 IV 标的（biotech 催化剂期 >300%）误判压缩。
    by_expiry: Dict[str, dict] = {}
    dropped = 0
    for o in options:
        parsed = _parse_occ(o.get("option", ""))
        if not parsed:
            dropped += 1
            continue
        expiry, cp, strike = parsed
        bucket = by_expiry.setdefault(expiry, {"C": [], "P": []})
        bucket[cp].append({
            "strike": strike,
            "openInterest": float(o.get("open_interest") or 0.0),
            "impliedVolatility": float(o.get("iv") or 0.0),
            "_cboe_gamma": float(o.get("gamma") or 0.0),
            "bid": float(o.get("bid") or 0.0),
            "ask": float(o.get("ask") or 0.0),
            "volume": float(o.get("volume") or 0.0),
            "lastPrice": float(o.get("last_trade_price") or 0.0),
            "contractSymbol": o.get("option", ""),
        })

    # 解析丢弃率告警：CBOE 格式变更 / 调整后符号（如拆股 AAPL1）会令 OCC 正则失配，
    # 静默跳过在多标的批量里不可见 → 丢弃 >5% 时告警，及时暴露格式回归。
    if options and dropped > len(options) * 0.05:
        _log.warning("CBOE %s：%d/%d 合约 OCC 符号解析失败（疑格式变更）", ticker, dropped, len(options))

    if not by_expiry:
        _log.warning("CBOE %s 无可解析合约", ticker)
        return None

    today = _pdt_now()
    expirations, near_expiry_set = _select_expiries(by_expiry, today, max_expiries)
    if not expirations:
        return None

    def _finalize_side(rows: List[dict], expiry: str) -> List[dict]:
        """单到期日单边：ATM 过滤 → 40-cap → DTE/gamma 注入。"""
        # ATM 过滤（与 yfinance 路径一致，仅当有现价时）
        if S > 0:
            rows = [r for r in rows if _ATM_LO * S <= r["strike"] <= _ATM_HI * S]
        # 每边最多 40 strike（按 OI 降序）
        rows = sorted(rows, key=lambda r: r["openInterest"], reverse=True)[:_MAX_STRIKES_PER_SIDE]
        try:
            dte = max(1, (datetime.strptime(expiry, "%Y-%m-%d") - today).days)
        except ValueError:
            dte = 30
        for r in rows:
            r["expiry"] = expiry
            r["dte"] = dte
            # gamma：优先 CBOE，缺失（0）时 BS 兜底
            g = r.pop("_cboe_gamma", 0.0)
            if not g:
                T = max(dte, 0.5) / 365.0
                g = _bs_gamma(S, r["strike"], T, r["impliedVolatility"])
            r["gamma"] = g
        return rows

    calls: List[dict] = []
    puts: List[dict] = []
    for e in expirations:
        b = by_expiry.get(e, {"C": [], "P": []})
        calls.extend(_finalize_side(list(b["C"]), e))
        puts.extend(_finalize_side(list(b["P"]), e))

    if not calls and not puts:
        return None

    # DTE 加权（1/sqrt(DTE) 归一化，与 yfinance 路径一致），跨整个 calls/puts
    def _apply_dte_weight(rows: List[dict]):
        if not rows:
            return
        raw_w = [1.0 / (r["dte"] ** 0.5) for r in rows]
        max_w = max(raw_w) if raw_w else 1.0
        for r, w in zip(rows, raw_w):
            r["dte_weight"] = w / max_w

    _apply_dte_weight(calls)
    _apply_dte_weight(puts)

    total_oi = sum(r["openInterest"] for r in calls) + sum(r["openInterest"] for r in puts)
    _log.info(
        "CBOE %s 期权链：%d 到期日，%d calls + %d puts，总 OI %s，现价 $%.2f",
        ticker, len(expirations), len(calls), len(puts), f"{int(total_oi):,}", S,
    )

    return {
        "ticker": ticker,
        "timestamp": _pdt_now().isoformat(),
        "calls": calls,
        "puts": puts,
        "expirations": expirations,
        "near_expiry_set": near_expiry_set,
        "_source": "cboe",  # 数据来源标记，供下游/调试识别
    }


def fetch_cboe_full_chain_oi(
    ticker: str,
    stock_price: float,
    max_expirations: int = 24,
    *,
    timeout: int = 15,
) -> Optional[Dict]:
    """全链 OI 聚合（供 options_analyzer._fetch_full_chain_oi 限流兜底）。

    返回与该方法 yfinance 路径相同的中间结构，复用其 Max Pain / OI 墙计算（零重复）：
        {call_oi:{strike:oi}, put_oi:{strike:oi},
         call_exp_oi:{strike:{exp:oi}}, put_exp_oi:{strike:{exp:oi}},
         expiry_breakdown:[{expiry,call_oi,put_oi,total}], used_exps:int}
    行权价过滤区间 [0.60×S, 1.45×S]，与 yfinance 路径一致。失败返回 None。
    """
    # 钩子放在 stock_price 守卫之前：快照里的 OI 是当日用有效价算好的，
    # 此刻传进来的 stock_price 是不是 0 与它无关。
    if _SNAPSHOT_PROVIDER is not None:
        return (_snapshot(ticker) or {}).get("full_chain_oi")

    if not stock_price or stock_price <= 0:
        return None
    data = _fetch_cboe_payload(ticker, timeout)
    if not data:
        return None
    lo, hi = stock_price * 0.60, stock_price * 1.45

    # 按到期日分组（仅 strike + OI，过滤价格区间 + 正 OI）
    by_exp: Dict[str, Dict[str, Dict[float, int]]] = {}
    for o in data["options"]:
        parsed = _parse_occ(o.get("option", ""))
        if not parsed:
            continue
        expiry, cp, strike = parsed
        if not (lo <= strike <= hi):
            continue
        oi = int(o.get("open_interest") or 0)
        if oi <= 0:
            continue
        b = by_exp.setdefault(expiry, {"C": {}, "P": {}})
        b[cp][strike] = b[cp].get(strike, 0) + oi

    if not by_exp:
        return None

    call_oi: Dict[float, int] = {}
    put_oi: Dict[float, int] = {}
    call_exp_oi: Dict[float, Dict[str, int]] = {}
    put_exp_oi: Dict[float, Dict[str, int]] = {}
    expiry_breakdown: List[dict] = []
    used = 0
    for exp in sorted(by_exp.keys())[:max_expirations]:
        b = by_exp[exp]
        c_sum, p_sum = sum(b["C"].values()), sum(b["P"].values())
        expiry_breakdown.append({"expiry": exp, "call_oi": c_sum, "put_oi": p_sum, "total": c_sum + p_sum})
        for s, oi in b["C"].items():
            call_oi[s] = call_oi.get(s, 0) + oi
            d = call_exp_oi.setdefault(s, {})
            d[exp] = d.get(exp, 0) + oi
        for s, oi in b["P"].items():
            put_oi[s] = put_oi.get(s, 0) + oi
            d = put_exp_oi.setdefault(s, {})
            d[exp] = d.get(exp, 0) + oi
        used += 1

    if not call_oi and not put_oi:
        return None
    _log.info("CBOE %s 全链 OI 聚合：%d 到期日，总 OI %s",
              ticker, used, f"{sum(call_oi.values()) + sum(put_oi.values()):,}")
    return {
        "call_oi": call_oi, "put_oi": put_oi,
        "call_exp_oi": call_exp_oi, "put_exp_oi": put_exp_oi,
        "expiry_breakdown": expiry_breakdown, "used_exps": used,
    }


def fetch_cboe_iv_term_structure(
    ticker: str,
    stock_price: float,
    *,
    timeout: int = 15,
    max_points: int = 6,
) -> Optional[List[Dict]]:
    """从 CBOE 全链算 ATM IV 期限结构（供 options_analyzer 主源）。

    为什么走 CBOE 而不是 yfinance：yfinance 路径要 1 次 `.options` + 4 次
    `option_chain()` 共 **5 次额外网络往返**，8/24 当天大面积抛 SSLError；
    实测那批 12 只标的有 7 只期限结构 pts=0，页面显示「0.0% / 0.0%」。
    （⚠️ 原注把这些 SSLError 归因于「本机老 SSL 栈」，该归因 2026-08-25 已证伪，
    见 http_gate docstring；但"少发 4 次请求"这个收益与根因无关，照样成立。）
    CBOE 是**一次请求拿全部到期日**，已进 `http_gate` 闸门，
    且 `_fetch_cboe_payload` 有进程缓存——主链已拉过时**零额外网络开销**。

    ATM 容差：`max(4%×S, 1.2×中位行权价间距)`。纯百分比容差对低价股会归零
    （AMC ≈$3 时 ±4% = ±$0.12，而行权价间距 $0.50 → 一个候选都选不到）。

    Returns: [{"expiry": str, "dte": int, "atm_iv": float(%)}, ...] 按 DTE 升序；
             无法计算返回 None（**不是空列表**——空列表会被误读为"算过了，没有"）。
    """
    if _SNAPSHOT_PROVIDER is not None:
        return (_snapshot(ticker) or {}).get("iv_term_structure")

    if not stock_price or stock_price <= 0:
        return None
    data = _fetch_cboe_payload(ticker, timeout)
    if not data:
        return None

    today = _pdt_now()
    # expiry -> {strike: [iv, ...]}（只用 call，与 yfinance 路径口径一致）
    by_exp: Dict[str, Dict[float, List[float]]] = {}
    for o in data["options"]:
        parsed = _parse_occ(o.get("option", ""))
        if not parsed:
            continue
        expiry, cp, strike = parsed
        if cp != "C":
            continue
        iv = float(o.get("iv") or 0.0)
        # CBOE iv 已是小数；深 ITM/零流动合约 iv=0，上界 2.0 与 yfinance 路径一致
        if not (0.02 < iv < 2.0):
            continue
        by_exp.setdefault(expiry, {}).setdefault(strike, []).append(iv)

    if not by_exp:
        return None

    # 自适应 ATM 容差：用全链行权价间距中位数兜底百分比容差
    all_strikes = sorted({k for m in by_exp.values() for k in m})
    gaps = [b - a for a, b in zip(all_strikes, all_strikes[1:]) if b > a]
    gap_med = statistics.median(gaps) if gaps else 0.0
    atm_tol = max(stock_price * 0.04, gap_med * 1.2)

    points: List[Dict] = []
    for expiry, strike_map in by_exp.items():
        try:
            dte = (datetime.strptime(expiry, "%Y-%m-%d") - today).days
        except ValueError:
            continue
        if not (_TS_MIN_DTE <= dte <= _TS_MAX_DTE):
            continue
        ivs = [iv for k, m in strike_map.items() if abs(k - stock_price) <= atm_tol for iv in m]
        if not ivs:
            continue
        points.append({
            "expiry": expiry,
            "dte": dte,
            "atm_iv": round(statistics.mean(ivs) * 100, 1),
        })

    if len(points) < 2:
        return None

    points.sort(key=lambda p: p["dte"])
    # 按与 yfinance 路径**相同的目标 DTE** 抽点，保证新旧报告的 front/back 口径可比。
    # 直接返回全部到期日会把 5 DTE（theta 扭曲）与 842 DTE（LEAPS）拿来比，
    # 得出的 spread 与历史已发布数值不同源。
    picked: List[Dict] = []
    for tgt in _TS_TARGET_DTE:
        best = min(points, key=lambda p: abs(p["dte"] - tgt))
        if best not in picked:
            picked.append(best)
    picked.sort(key=lambda p: p["dte"])
    return picked[:max_points] if len(picked) >= 2 else None


# ── 四合约报价集（v0.45.99）───────────────────────────────────────────
# 为将来的期权纸面交易腿攒数据：每天在 ~30 DTE 到期日上**持久化四张合约的真实
# 买卖报价**（ATM call / ATM put / 25Δ call / 25Δ put）。当前**没有任何下游消费者**，
# 只落进 options_snapshot；不进评分、不进 IC、不改任何既有字段。
#
# 为什么从**原始 payload** 选而不是从 `fetch_cboe_chain` 的成品链选：
# 成品链每边按 OI 只留 40 个行权价、再套 ATM 带宽过滤，25Δ 的行权价（离 ATM
# 约 1σ）很容易被裁掉；原始 payload 是全链，且 `_fetch_cboe_payload` 有 120s
# 进程缓存——主链刚拉过时**零额外网络开销**。
#
# 诚实降级（项目硬规则）：bid=0 / NaN / ask<bid 的报价 `quote_ok=False`，
# mid 与 spread_pct 置 None；缺 delta 的槽位置 None 并给 reason；任何一步
# 拿不到都返回 `data_available=False` + error 字符串，**不填看起来合理的默认值**。
_QS_MIN_DTE = 7          # 与主链一致：<7 DTE 的合约 theta 扭曲，不作候选
_QS_ROLES = ("atm_call", "atm_put", "c25", "p25")


def _qs_num(v) -> Optional[float]:
    """float 且有限 → float；否则 None。`bool(nan) is True`，所以不能用真值判断。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _quote_set_unavailable(error: str, *, source: str = "cboe",
                           target_dte: int = 30) -> dict:
    """`data_available=False` 的统一形状：contracts 四个键都在、值为 None，
    下游可以无脑 `.get("contracts", {}).get("atm_call")` 而不必先判可用。"""
    return {
        "data_available": False,
        "source": source,
        "error": error,
        "target_dte": target_dte,
        "selected_expiry": None,
        "selected_dte": None,
        "underlying_price": None,
        "underlying_price_source": None,
        "iv30": None,
        "market_open": None,
        "fetched_at": None,
        "contracts": {role: None for role in _QS_ROLES},
        "atm_straddle_mid": None,
        "implied_move_pct": None,
    }


def _qs_contract(row: dict, *, role: str, cp: str, strike: float,
                 expiry: str, dte: int) -> dict:
    """把一行 CBOE 原始合约整理成持久化形状。价格 4dp；报价不合法时 mid/spread 为 None。"""
    bid = _qs_num(row.get("bid"))
    ask = _qs_num(row.get("ask"))
    # quote_ok：双边有限、bid>0、ask>=bid。bid=0 的 mid 是「一半的 ask」，不是市价——
    # 拿它当成交价等于凭空造出一个不存在的对手方。
    quote_ok = bid is not None and ask is not None and bid > 0 and ask >= bid
    mid = round((bid + ask) / 2.0, 4) if quote_ok else None
    spread_pct = round((ask - bid) / mid, 4) if (quote_ok and mid) else None
    return {
        "symbol": row.get("option"),
        "type": cp,
        "role": role,
        "strike": strike,
        "expiry": expiry,
        "dte": dte,
        "bid": round(bid, 4) if bid is not None else None,
        "ask": round(ask, 4) if ask is not None else None,
        "mid": mid,
        "spread_pct": spread_pct,
        "iv": _qs_num(row.get("iv")),
        "delta": _qs_num(row.get("delta")),
        "gamma": _qs_num(row.get("gamma")),
        "vega": _qs_num(row.get("vega")),
        "theta": _qs_num(row.get("theta")),
        "oi": _qs_num(row.get("open_interest")),
        "volume": _qs_num(row.get("volume")),
        "theo": _qs_num(row.get("theo")),
        "last_trade_time": row.get("last_trade_time"),
        "quote_ok": quote_ok,
    }


def _qs_pick_row(rows: List[dict]) -> dict:
    """同 (到期日, 方向, 行权价) 理论上只有一行；万一重复，取 OI 最大的那行。"""
    return max(rows, key=lambda r: _qs_num(r.get("open_interest")) or 0.0)


def select_quote_set(data: dict, S: float, *, target_dte: int = 30,
                     now: Optional[datetime] = None) -> dict:
    """从 CBOE 原始 payload 选出 ~target_dte 到期日上的四张合约（纯函数，可离线测）。

    选法：
      到期日 —— 所有 dte ≥ 7 的候选里取 |dte − target_dte| 最小者，平手取更远的
                （更远的 theta 更平缓，作为纸面持仓更不吃时间）。
      ATM    —— 离 S 最近的行权价，**优先 call/put 两边都有的**行权价；
                ATM call 与 ATM put 同一行权价。
      25Δ    —— call 取 delta 最接近 +0.25、put 取最接近 −0.25；只在 delta
                有限且非零的行里选（CBOE 对零流动合约给 delta=0，那不是观测值）。
    `now` 只用来算 DTE；缺省取 `_pdt_now()`（禁用裸 datetime.now()，见其 docstring）。
    """
    today = (now or _pdt_now()).date()
    S_num = _qs_num(S)
    if S_num is None or S_num <= 0:
        return _quote_set_unavailable("underlying price unavailable", target_dte=target_dte)

    # 按到期日分组：expiry -> {"C": {strike: [row,...]}, "P": {...}}
    by_expiry: Dict[str, Dict[str, Dict[float, List[dict]]]] = {}
    for row in (data or {}).get("options") or []:
        if not isinstance(row, dict):
            continue
        parsed = _parse_occ(row.get("option", ""))
        if not parsed:
            continue
        expiry, cp, strike = parsed
        by_expiry.setdefault(expiry, {"C": {}, "P": {}})[cp].setdefault(strike, []).append(row)

    candidates: List[Tuple[str, int]] = []
    for expiry in by_expiry:
        try:
            dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if dte >= _QS_MIN_DTE:
            candidates.append((expiry, dte))
    if not candidates:
        return _quote_set_unavailable(f"no expiry with dte>={_QS_MIN_DTE}",
                                      target_dte=target_dte)

    # 平手取更远：key 里第二项用 -dte，dte 越大越靠前
    sel_expiry, sel_dte = min(candidates, key=lambda ed: (abs(ed[1] - target_dte), -ed[1]))
    calls = by_expiry[sel_expiry]["C"]
    puts = by_expiry[sel_expiry]["P"]

    # ATM 行权价：优先两边都有的；没有交集才退到任一边的全集
    both = set(calls) & set(puts)
    pool = both or (set(calls) | set(puts))
    contracts: Dict[str, Optional[dict]] = {role: None for role in _QS_ROLES}
    reasons: Dict[str, str] = {}
    if pool:
        atm_strike = min(pool, key=lambda k: (abs(k - S_num), k))
        if atm_strike in calls:
            contracts["atm_call"] = _qs_contract(_qs_pick_row(calls[atm_strike]), role="atm_call",
                                                 cp="C", strike=atm_strike, expiry=sel_expiry, dte=sel_dte)
        else:
            reasons["atm_call"] = "no call at atm strike"
        if atm_strike in puts:
            contracts["atm_put"] = _qs_contract(_qs_pick_row(puts[atm_strike]), role="atm_put",
                                                cp="P", strike=atm_strike, expiry=sel_expiry, dte=sel_dte)
        else:
            reasons["atm_put"] = "no put at atm strike"
    else:
        reasons["atm_call"] = reasons["atm_put"] = "no strikes in selected expiry"

    # 25Δ：只认有限且非零的 delta
    def _by_delta(side: Dict[float, List[dict]], target: float):
        best = None
        for strike, rows in side.items():
            for row in rows:
                d = _qs_num(row.get("delta"))
                if d is None or d == 0.0:
                    continue
                dist = abs(d - target)
                if best is None or dist < best[0]:
                    best = (dist, strike, row)
        return best

    c25 = _by_delta(calls, 0.25)
    if c25:
        contracts["c25"] = _qs_contract(c25[2], role="c25", cp="C", strike=c25[1],
                                        expiry=sel_expiry, dte=sel_dte)
    else:
        reasons["c25"] = "no delta"
    p25 = _by_delta(puts, -0.25)
    if p25:
        contracts["p25"] = _qs_contract(p25[2], role="p25", cp="P", strike=p25[1],
                                        expiry=sel_expiry, dte=sel_dte)
    else:
        reasons["p25"] = "no delta"

    ac, ap = contracts["atm_call"], contracts["atm_put"]
    straddle = None
    if ac and ap and ac["quote_ok"] and ap["quote_ok"]:
        straddle = round(ac["mid"] + ap["mid"], 4)
    implied_move = round(straddle / S_num * 100.0, 4) if straddle is not None else None

    return {
        "data_available": True,
        "source": "cboe",
        "error": None,
        "target_dte": target_dte,
        "selected_expiry": sel_expiry,
        "selected_dte": sel_dte,
        "underlying_price": round(S_num, 4),
        "underlying_price_source": "caller",
        "iv30": _qs_num((data or {}).get("iv30")),
        "market_open": is_market_open(),
        "fetched_at": _et_now().isoformat(),
        "contracts": contracts,
        "missing_reasons": reasons,
        "atm_straddle_mid": straddle,
        "implied_move_pct": implied_move,
    }


def fetch_cboe_quote_set(ticker: str, stock_price: float = 0.0, *,
                         target_dte: int = 30, timeout: int = 15) -> dict:
    """拉 CBOE 原始链并选四合约报价集；任何失败都返回 `data_available=False` 的 dict
    （**永不抛错、永不返回 None**——调用方是 analyze() 的出口，不该被它绊倒）。

    快照模式（补跑历史日）下 payload 没有 options 数组，直接诚实返回不可用：
    补跑那天的真实报价谁也拿不到，用今天的链冒充比缺失更糟。
    """
    if _SNAPSHOT_PROVIDER is not None:
        return _quote_set_unavailable("snapshot mode has no raw chain", target_dte=target_dte)

    data = _fetch_cboe_payload(ticker, timeout)
    if not data:
        return _quote_set_unavailable("cboe payload unavailable", target_dte=target_dte)

    S = _qs_num(stock_price)
    price_source = "caller"
    if S is None or S <= 0:
        S, price_source = official_price(data)
    if not S or S <= 0 or not math.isfinite(S):
        return _quote_set_unavailable("underlying price unavailable", target_dte=target_dte)

    qs = select_quote_set(data, S, target_dte=target_dte)
    if qs.get("data_available"):
        qs["underlying_price_source"] = price_source
    return qs


if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    r = fetch_cboe_chain(tk)
    if not r:
        print(f"{tk}: CBOE 获取失败")
        sys.exit(1)
    toi = sum(c["openInterest"] for c in r["calls"]) + sum(p["openInterest"] for p in r["puts"])
    print(f"{tk}: {len(r['expirations'])} 到期日 {r['expirations']}")
    print(f"  calls={len(r['calls'])} puts={len(r['puts'])} 总OI={int(toi):,} source={r['_source']}")
    if r["calls"]:
        c = r["calls"][0]
        print(f"  样例 call: strike={c['strike']} OI={c['openInterest']} IV={c['impliedVolatility']:.3f} gamma={c['gamma']:.5f} dte={c['dte']}")
