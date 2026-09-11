#!/usr/bin/env python3
"""
Twelve Data 日K 客户端（v0.45.61）
==================================
补上整条链上**最后一处非 yfinance 不可**的依赖：逐标的 30 日收盘。
`rv_30d` / `iv_rv_spread` / hv_proxy 口径的 `iv_rank` 全部由它派生 ——
2026-08-27 yfinance 687 次 429，这三项各 0/30。

为什么是 Twelve Data
--------------------
2026-08-28 逐个实打验过的结论：

| 源 | 逐标的日K | 判定 |
|---|---|---|
| Finnhub `/stock/candle` | HTTP 403 | 已转付费 |
| Alpha Vantage | 25 次/天 | **已被 newsapi 的新闻情绪占满** |
| Stooq | JS 工作量证明挑战页 | 已上反爬 |
| FRED | 只有指数，无逐标的 | 不适用 |
| **Twelve Data** | **800 次/天、8 次/分** | 30 只 × 1 credit，唯一量级够的 |

配置
----
把 key 写进 `~/.alpha_hive_twelvedata_key`（一行，无引号）：

    echo 'YOUR_KEY' > ~/.alpha_hive_twelvedata_key && chmod 600 ~/.alpha_hive_twelvedata_key

**未配置时本模块直接返回 None**，降级链原样退回 yfinance（已限流到 0.5 req/s），
不报错、不阻断 —— 与 `FinnhubSource` 未配 key 时的行为一致。

⚠️ 限流口径与 yfinance 不同
---------------------------
免费档是 **8 次/分钟**。这不是 yfinance 那种「打到 429 就熔断很久」的软限，
而是明确的每分钟窗口。所以用**独立的**令牌桶（0.13 req/s ≈ 8/分），
不与 `resilience.yfinance_limiter` 共用 —— 共用会让两边互相拖慢，
而它们的配额本来就是分开的。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

try:
    from hive_logger import get_logger
    _log = get_logger("twelve_data")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.twelve_data")

_BASE = "https://api.twelvedata.com/time_series"

# 免费档 8 次/分。取 7/分留一成余量 —— 撞上限的代价是整批失败，
# 而慢一点只是慢一点。
_RATE_PER_SEC = 7.0 / 60.0
_BURST = 2

_limiter = None
_daily_used = 0          # 观测用：免费档 800/天，跨过就该知道
_DAILY_BUDGET = 800

# ── 进程内日线缓存（v0.45.105）────────────────────────────────────────────────
# 同一只票的日线在一次扫描里曾被取 3 遍（portfolio_greeks / vrp_signal /
# options_paper_leg 各一次）。缓存放在**本模块**而不是任何一个消费方：
# 三个消费方都已经依赖 twelve_data，把它放在这片叶子上不给任何方向新增依赖；
# 放进 portfolio_greeks 反而会让 options_paper_leg 反向 import 它，成环
# （portfolio_greeks 已经 import options_paper_leg）。
SHARED_BARS_WINDOW = 120   # 三个消费方谈拢的统一窗口，见 `fetch_bars`

# key = (ticker, end_date)；value = (请求过的最大窗口, 该次返回的行)
# v0.45.125：`end_date=None` 在键上归一为美东当日（见 `_bars_key`），
# 蜂群段（fetch_daily_closes / fetch_volume_ratio，None）与尾段
# （portfolio_greeks / vrp / options_paper_leg，end_date=as_of=今天）共用一份。
_BARS_CACHE: Dict[Tuple[str, Optional[str]], Tuple[int, List[dict]]] = {}
_bars_cache_stats = {"hits": 0, "misses": 0, "refetch_larger": 0, "fetches": 0,
                     "inflight_waits": 0, "warmed": 0}
_BARS_LOCK = threading.Lock()                       # 护 _BARS_CACHE / _INFLIGHT / 计数
_INFLIGHT: Dict[Tuple[str, Optional[str]], threading.Lock] = {}   # 同键并发只发一次请求


class TwelveDataUnavailable(ConnectionError):
    """未配置 key、限流、或接口不可用。

    继承 `ConnectionError`（⊂ OSError ⊂ resilience.NETWORK_ERRORS），
    确保被现有的 `except NETWORK_ERRORS` 接住 —— v0.45.56 在 `YFRateLimited`
    上踩过这个坑：新造的异常若不继承既有网络异常族，会穿透所有降级路径。
    """


def _get_limiter():
    global _limiter
    if _limiter is None:
        try:
            from resilience import RateLimiter
            _limiter = RateLimiter(rate=_RATE_PER_SEC, burst=_BURST)
        except Exception:  # pragma: no cover
            return None
    return _limiter


def api_key() -> str:
    try:
        from config import get_secret
        return (get_secret("TWELVEDATA_API_KEY") or "").strip()
    except Exception:  # pragma: no cover
        import os
        return os.environ.get("TWELVEDATA_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(api_key())


def stats() -> Dict:
    return {"daily_used": _daily_used, "daily_budget": _DAILY_BUDGET,
            "configured": is_configured()}


def reset_stats() -> None:
    global _daily_used
    _daily_used = 0


def _et_today() -> Optional[str]:
    """美东当日日期。时区换算靠 zoneinfo（绝对时间正确即可），不靠本机 tz 设置。"""
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # pragma: no cover
        return None


def _drop_forming_bar(rows: List[dict], ticker: str = "") -> List[dict]:
    """去掉「当日盘中正在形成」的末根日线。

    2026-08-28 10:09 ET 实测：NVDA 末根 `2026-08-28 close=224.57 volume=103412`，
    而 8/24–8/27 的成交量是 1.2~3.0 亿 —— 开盘才十分钟的半根 bar。
    把它算进 RV30 等于用一个残缺的日收益污染波动率。

    项目里已有 `data_pipeline._drop_forming_bar`，但它靠 yfinance 探 SPY 分钟线
    判断交易所时间 —— 那正是本模块要绕开的东西。这里改用返回体自带的两个信号，
    不额外发请求：

      ① **日期**：末根日期 == 美东当日 → 今天的 bar，收盘前必然未完成
      ② **成交量**：末根 < 窗口中位数的 30% → 几乎只可能是半根

    两道各自独立、任一命中即丢。误丢的代价极小（30 根里少一根），
    漏丢的代价是波动率失真 —— 不对称，所以宁可宽。
    """
    if len(rows) < 5:
        return rows
    last = rows[-1]
    reason = ""

    today_et = _et_today()
    if today_et and last["date"] >= today_et:
        reason = f"日期 {last['date']} 是美东当日"
    else:
        vols = sorted(r["vol"] for r in rows[:-1] if r["vol"] > 0)
        if vols:
            med = vols[len(vols) // 2]
            if med > 0 and last["vol"] < med * 0.30:
                reason = f"成交量 {last['vol']:,.0f} 不足中位 {med:,.0f} 的 30%"

    if reason:
        _log.info("[%s] 丢弃盘中未完成的末根日线（%s）", ticker or "?", reason)
        return rows[:-1]
    return rows


def fetch_daily_closes(ticker: str, days: int = 60,
                       end_date: Optional[str] = None) -> Optional[List[float]]:
    """取日线收盘价，**按日期升序**（最旧 → 最新）。

    Parameters
    ----------
    days : 要多少根。`rv_30d` 需要 ≥31 根才能算 30 个收益率。
    end_date : "YYYY-MM-DD"。给定时取**截至该日（含）**的窗口，用于补跑；
        `None` = 最新（末根是最近一个已收完的交易日，当日那根由
        `_drop_forming_bar` 丢掉）。
        「含」是本函数的承诺，不是接口的行为 —— Twelve Data 的 `end_date`
        是**不含**的，差值在 `_api_end_date` 里补掉（v0.45.90）。

    Returns
    -------
    收盘价列表，或 None（未配 key / 限流 / 接口异常 / 数据不足）。

    **绝不返回空列表或 0.0 填充** —— 下游 `np.std` 拿到常数列会算出
    `rv=0`，与「波动率真的是 0」不可区分（MEMORY 静默降级三件套）。
    """
    # v0.45.125：走 `fetch_bars` 缓存，且一次多要到 SHARED_BARS_WINDOW 根——
    # outputsize 不多花配额，却让尾段的 120 根请求直接命中，不必 refetch_larger。
    # 末尾按 `days` 切尾巴：与旧的直接 `_fetch_rows(ticker, days)` 相比，输出取的是
    # 同一条时间序列的最后 `days` 根，下游 `realized_vol` 只用尾部收益，结果不变。
    rows = fetch_bars(ticker, max(days, SHARED_BARS_WINDOW), end_date)
    if rows is None:
        return None
    rows = rows[-days:]

    closes = [r["close"] for r in rows]
    if len(closes) < 10:
        _log.warning("[%s] Twelve Data 有效收盘价仅 %d 根", ticker, len(closes))
        return None
    return closes


def _api_end_date(end_date: str) -> str:
    """把调用方的「截至该日（**含**）」翻成 Twelve Data 的 `end_date`（**不含**）。

    2026-09-01 实测（NVDA，`outputsize=15`）：

        end_date=2026-08-31 → 末根 2026-08-28 close=217.55
        end_date=None       → 末根 2026-08-31 close=220.78

    8/29–8/30 是周末，所以这正好差**一个交易日**：接口把 `end_date` 按左闭
    右开处理，当日那根不返回。而本模块对外的契约（`fetch_daily_closes`
    docstring）和唯一真正传这个参数的生产消费方
    （`market_intelligence.calculate_iv_rv_spread` 的补跑路径）要的都是含目标日：

      · HV30 的标准定义就是「截至 D 收盘的 30 个日收益」；
      · 补跑 D 的报告在 D 收盘后才发（编排器时间闸 13:30 PDT = 16:30 ET），
        D 的收盘价那时已是公开信息 —— 用它不构成前视。

    所以这里 +1 个自然日抵掉那个开区间。非交易日没有 bar，落到周末/假日也
    不会多带一根（实测 `end_date=2026-08-29` 周六 → 末根仍是 2026-08-28）。

    ⚠️ 这**不能**由「让 `_drop_forming_bar` 去掉当日」代劳 —— 两者管的不是
    一件事。那道闸判的是「这根还没走完」（`date >= 美东当日`），是运行时的
    数据质量护栏；`end_date` 说的是「窗口画到哪」。补跑 D 时 D 那根早已收完，
    根本没有半根可言。反过来，+1 之后若目标日恰好是今天，当日那根会被拉进来，
    那道闸照旧拦得住（实测 `end_date=2026-09-02` → 末根仍是 2026-08-31）。

    ⚠️ 也**不要**据此去对齐「实时扫描当天拿到的窗口末端是 D-1」：那不是口径
    约定，只是 `_drop_forming_bar` 在跑的当下无法确认 D 那根收没收完。同一
    函数里的 yfinance 兜底腿（`yf.download(period=...)`）压根没有这道闸、
    当天那根照收 —— 两条腿本来就差一根，没有「D-1 口径」可言。

    解析不了就原样透传：Twelve Data 也收 `YYYY-MM-DD hh:mm:ss`，那种写法自带
    时刻语义，不该被本函数按「日」平移。
    """
    try:
        import datetime as _dt
        return (_dt.date.fromisoformat(end_date) + _dt.timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        # 不静默：透传意味着「含目标日」这条承诺在本次调用上不成立，
        # 得让日志说出来，而不是让调用方以为补偿生效了。
        _log.warning("end_date=%r 不是纯日期，原样透传（不做含/不含补偿）",
                     str(end_date)[:40])
        return end_date


def _fetch_rows(ticker: str, days: int,
                end_date: Optional[str] = None) -> Optional[List[dict]]:
    """共享抓取层：一次请求拿回 `{date, close, vol}` 逐根日线（已剔除半根）。

    `fetch_daily_closes` / `fetch_volume_ratio` 共用同一次限流令牌 + 同一份
    解析逻辑，避免两个口径各发一次请求、各自出一套"哪天算今天"的判断。
    """
    key = api_key()
    if not key:
        return None

    params = {
        "symbol": ticker,
        "interval": "1day",
        "outputsize": str(max(days, 10)),
        "apikey": key,
        "order": "ASC",
    }
    if end_date:
        # 接口是左闭右开的，+1 天才等于调用方要的「含该日」（见 `_api_end_date`）
        params["end_date"] = _api_end_date(end_date)

    rl = _get_limiter()
    if rl is not None and not rl.acquire(timeout=90.0):
        _log.warning("[%s] Twelve Data 限流令牌等待超时", ticker)
        return None

    try:
        from http_gate import urlopen_gated
        req = urllib.request.Request(
            f"{_BASE}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": "alpha-hive/1.0"})
        raw = urlopen_gated(req, timeout=25)
    except Exception as e:  # noqa: BLE001
        _log.warning("[%s] Twelve Data 请求失败: %s", ticker, e)
        return None

    global _daily_used
    _daily_used += 1

    try:
        d = json.loads(raw)
    except ValueError:
        _log.warning("[%s] Twelve Data 返回非 JSON", ticker)
        return None

    # 免费档超额/参数错时返回 {"code":..,"message":..,"status":"error"}，
    # HTTP 仍是 200 —— 只看状态码会把错误当成数据（这正是 8/26 那类
    # 「看着成功其实早废了」的形状）。
    if isinstance(d, dict) and d.get("status") == "error":
        _log.warning("[%s] Twelve Data 错误 %s: %s", ticker,
                     d.get("code"), str(d.get("message"))[:120])
        return None

    values = d.get("values") if isinstance(d, dict) else None
    if not values:
        _log.warning("[%s] Twelve Data 无 values 段", ticker)
        return None

    rows: List[dict] = []
    for row in values:
        try:
            c = float(row.get("close"))
        except (TypeError, ValueError, AttributeError):
            continue
        if c <= 0:
            continue
        try:
            v = float(row.get("volume") or 0)
        except (TypeError, ValueError):
            v = 0.0
        rows.append({"date": str(row.get("datetime") or "")[:10], "close": c, "vol": v})

    return _drop_forming_bar(rows, ticker)


def fetch_bars(ticker: str, days: int = SHARED_BARS_WINDOW,
               end_date: Optional[str] = None) -> Optional[List[dict]]:
    """**日线的对外入口**：与 `_fetch_rows` 同样的 `[{date, close, vol}]` 升序，
    但同一 `(ticker, end_date)` 在一个进程里只真去取一次。

    为什么需要它
    ------------
    Twelve Data 是**串行 7 次/分钟**的队列（`_RATE_PER_SEC`），一次调用排队
    ≈8.6 秒。日报里三个消费方各取一次同一只票的日线：

        options_paper_leg._default_close   （先跑）
        vrp_signal._default_bars
        portfolio_greeks.daily_bars        （后跑）

    30 只票 × 多余的 2 次 = 60 次串行调用 ≈ 8.6 分钟，全部是重复数据。
    编排器 `STEP2_TIMEOUT` 对 ~2700 秒的扫描只剩 ~900 秒余量，而 v0.45.89
    已经有过一次「扫描被超时杀在 20/30」——白花的串行调用是可靠性成本，
    不只是慢。

    缓存语义
    --------
    · **键 = `(ticker, end_date)`，其中 `None` 归一为美东当日**（v0.45.125，
      见 `_bars_key`）。为什么现在可以合并：`_fetch_rows` 对显式 `end_date` 也过
      `_drop_forming_bar`，所以同一个美东日里 `None`（最新，丢当日半根）与
      `end_date=今天`（接口 +1 天拉进当日那根，再被同一道闸丢掉）返回**逐行相同**
      ——2026-09-04 实测蜂群段 30 只与尾段 17 只取的就是同一份数据、各发一次。
      显式的**过去**日期仍是独立键，与 `None` 不同（补跑窗口末端不是今天）。
      `_et_today()` 取不到时退回 `None` 键，不猜。
    · **同键并发只发一次请求**：4 个标的 worker + 预热线程可能同时要同一只票，
      第二个到的等第一个拿回来再读缓存（`inflight_waits`），不重复排限流队。
    · **存"迄今请求过的最大窗口"**。后来的**小**窗口请求切尾巴返回
      （行是升序的，"小窗口"就是最后 N 行），不再发请求；后来的**大**窗口
      请求会重新取数并整条替换。
    · **失败不入缓存**。`None`（未配 key / 限流 / 接口错）和空列表都直接返回、
      不写进去，下一次调用照常重试 —— v0.45.50 有过「缓存住一次失败把整轮
      钉死」的教训（`paper_portfolio._PRICE_CACHE` 就是为此修的）。
      大窗口取数失败时也**不动**已有的小窗口条目：那份数据仍然是好的。
    · **返回副本**（连同每行的 dict），调用方随便改都碰不到缓存里的那份。
    · **新取和命中走同一条裁剪规则**（都只回最后 `days` 根）。写这个函数时第一版
      只在命中时切、新取时原样返回，于是「同样的参数，第一次 130 根、第二次
      120 根」——同一个入口两种口径，正是本项目最爱出的那类不一致。生产上看不
      出来（`outputsize=days`，接口不会多给），但看不出来的不一致最贵。
      注意 `_fetch_rows` 的 `outputsize` 有 `max(days, 10)` 下限，所以 `days < 10`
      时本函数会比它少给几根 —— 现有三个消费方都要 120 根，够不着这个边角。
    · `_drop_forming_bar` 的行为一点没变：缓存的就是 `_fetch_rows` 今天返回
      的东西，本函数不做任何额外裁剪。

    生命周期
    --------
    **进程内、一次运行内不设上限**，也不设 TTL。一次扫描就是一个进程：
    30 只票 + SPY × 各自几个窗口 ≈ 几十条 × 120 行，量级完全无所谓。
    但正因为没有 TTL，**长驻进程别用它**（会一直端着当天第一次取到的数）；
    真需要重取就调 `clear_bars_cache()`。

    Parameters
    ----------
    days : 要多少根。三个消费方统一用 `SHARED_BARS_WINDOW`(=120)，
        这样谁先跑都只发一次请求 —— 窗口不一致的话，先跑的小窗口会让后跑的
        大窗口白白重取一次（options_paper_leg 只要 10 根，却是最先跑的那个）。
        多要几十根不多花配额：那只是同一次请求的 `outputsize`。
    end_date : 与 `fetch_daily_closes` 同义，**截至该日（含）**。
    """
    days = max(int(days), 1)
    key = _bars_key(ticker, end_date)

    def _lookup():
        ent = _BARS_CACHE.get(key)
        if ent is not None and days <= ent[0]:
            return [dict(r) for r in ent[1][-days:]]
        return None

    with _BARS_LOCK:
        got = _lookup()
        if got is not None:
            _bars_cache_stats["hits"] += 1
            return got
        gate = _INFLIGHT.setdefault(key, threading.Lock())

    # 同键并发：第二个到的在这里等第一个拿回来，然后直接读缓存
    with gate:
        with _BARS_LOCK:
            got = _lookup()
            if got is not None:
                _bars_cache_stats["inflight_waits"] += 1
                _bars_cache_stats["hits"] += 1
                return got
            # v0.45.130：misses / refetch_larger 只在**真要发请求**时计——此前在闸前计，
            # 并发首次请求时输的那个线程既计了 miss 又计了 hit，`fetches == misses +
            # refetch_larger` 这条文档写明的不变式就破了（独立审查抓到）。
            _bars_cache_stats["refetch_larger" if _BARS_CACHE.get(key) is not None else "misses"] += 1
            _bars_cache_stats["fetches"] += 1
        rows = _fetch_rows(ticker, days, end_date)
        if not rows:
            # None / [] 都不入缓存，且不动已有条目（见上面「失败不入缓存」）
            return rows
        with _BARS_LOCK:
            _BARS_CACHE[key] = (days, rows)
        return [dict(r) for r in rows[-days:]]


def _bars_key(ticker: str, end_date: Optional[str]) -> Tuple[str, Optional[str]]:
    """缓存键。`None`（最新）归一为美东当日——同日里两者返回逐行相同（见 fetch_bars 文档）。
    只改键，**不改请求**：`None` 调用方发出的请求仍不带 `end_date` 参数。"""
    return (ticker, end_date if end_date else _et_today())


def warm_bars_cache(tickers: List[str], days: int = SHARED_BARS_WINDOW,
                    end_date: Optional[str] = None) -> Dict[str, int]:
    """顺序预热：把这些票的日线各取一次进缓存。单票失败只记数、不中断。

    Twelve Data 是 7 次/分的串行队列，30 只 ≈ 4.4 分钟——放在**后台线程**里跑
    （见 `start_bars_warmer`），蜂群段和尾段的消费方到时直接命中；先于预热到达
    的消费方自己取，预热线程随后命中同一键，总请求数不变。
    """
    out = {"warmed": 0, "failed": 0}
    for t in tickers:
        try:
            rows = fetch_bars(t, days, end_date)
        except Exception as e:  # noqa: BLE001 - 预热是优化，不是前提
            _log.debug("[%s] 日线预热异常: %s", t, e)
            rows = None
        if rows:
            out["warmed"] += 1
        else:
            out["failed"] += 1
    with _BARS_LOCK:
        _bars_cache_stats["warmed"] += out["warmed"]
    _log.info("Twelve Data 日线预热完成：%d/%d 只", out["warmed"], len(tickers))
    return out


def start_bars_warmer(tickers: List[str], days: int = SHARED_BARS_WINDOW,
                      end_date: Optional[str] = None) -> Optional[threading.Thread]:
    """开后台守护线程跑 `warm_bars_cache`。未配置 key / 空名单 → 不开、返回 None。"""
    symbols = [t for t in dict.fromkeys(tickers) if t]
    if not symbols or not is_configured():
        return None
    th = threading.Thread(target=warm_bars_cache, args=(symbols, days, end_date),
                          name="twelvedata-warmer", daemon=True)
    th.start()
    _log.info("Twelve Data 日线预热已启动：%d 只（后台，串行 %.1f 次/分）",
              len(symbols), _RATE_PER_SEC * 60)
    return th


def bars_cache_stats() -> Dict:
    """`fetch_bars` 的命中情况。`hits + misses + refetch_larger` = 总调用次数，
    `fetches` = 真正发出去的请求数（= misses + refetch_larger）。"""
    d = dict(_bars_cache_stats)
    d["entries"] = len(_BARS_CACHE)
    return d


def clear_bars_cache() -> None:
    """清空日线缓存并把计数归零（测试之间、以及长驻进程换日时用）。"""
    with _BARS_LOCK:
        _BARS_CACHE.clear()
        _INFLIGHT.clear()
        for k in _bars_cache_stats:
            _bars_cache_stats[k] = 0


def fetch_volume_ratio(ticker: str, window: int = 20,
                       end_date: Optional[str] = None) -> Optional[Dict]:
    """最新成交量 / 近 `window` 根均量（含当日）。算不出返回 None。

    与 `data_pipeline._fetch_history_metrics` 的 yfinance 口径保持一致：
    均量窗口含最新一根，不是"最新对比前 window 根"——两条腿走不同口径的话，
    同一只标的会因为落到哪条降级路径而算出不同的比值。

    **不兜底 1.0** —— 均量算不出（数据不足/为 0/NaN）时置 None，
    绝不让"量比正常"这个假象混进评分（MEMORY 静默降级三件套）。

    `end_date` 与 `fetch_daily_closes` 同义：**截至该日（含）**。
    （生产端 `data_pipeline._fill_volume_from_twelvedata` 目前只走实时口径、
    不传这个参数；语义仍与另外两个入口保持一致，免得将来接补跑时又差一根。）
    """
    # v0.45.125：走 `fetch_bars` 缓存（与 fetch_daily_closes / 尾段三个消费方同键），
    # 只用尾部 `window` 根，多取的不参与计算。
    rows = fetch_bars(ticker, max(window + 10, SHARED_BARS_WINDOW), end_date)
    if not rows or len(rows) < window:
        return None

    recent_vol = rows[-1]["vol"]
    window_vols = [r["vol"] for r in rows[-window:]]
    avg_vol = sum(window_vols) / len(window_vols)

    if recent_vol <= 0 or avg_vol <= 0:
        _log.warning("[%s] Twelve Data 成交量不可得或为 0，volume_ratio 置 None", ticker)
        return None

    return {
        "volume_ratio": recent_vol / avg_vol,
        "avg_volume": int(avg_vol),
        "recent_volume": int(recent_vol),
    }


def realized_vol(ticker: str, lookback: int = 30,
                 end_date: Optional[str] = None) -> Optional[float]:
    """年化已实现波动率（%）。算不出返回 None。

    与 `market_intelligence.calculate_iv_rv_spread` 的口径保持一致：
    对数收益、`ddof=1`、×√252×100、剔除 |log_ret| > 0.5 的异常跳升。
    口径不一致的话，同一只标的会因为走了哪条源而得到不同的 RV —— 那种
    差异会被误读成波动率变化。

    `end_date` 与 `fetch_daily_closes` 同义：**截至该日（含）**，
    即窗口末根就是该日的收盘（HV30 的标准定义）。
    """
    import math

    closes = fetch_daily_closes(ticker, days=lookback + 30, end_date=end_date)
    if not closes or len(closes) < lookback // 2:
        return None

    rets = []
    for a, b in zip(closes[:-1], closes[1:]):
        if a > 0 and b > 0:
            r = math.log(b / a)
            if abs(r) < 0.5:
                rets.append(r)
    rets = rets[-lookback:]
    if len(rets) < 5:
        return None

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    rv = math.sqrt(var) * math.sqrt(252) * 100
    # 与上游同一条 sanity check：正常股票 HV30 不该超过 300%
    return rv if 0 < rv <= 300 else None


if __name__ == "__main__":  # pragma: no cover - 手工核对
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    if not is_configured():
        print("未配置 key。写入 ~/.alpha_hive_twelvedata_key 后重试。")
        sys.exit(1)
    cs = fetch_daily_closes(t)
    print(f"{t}: {len(cs) if cs else 0} 根收盘价，末 3 根 {cs[-3:] if cs else None}")
    print(f"{t}: RV30 = {realized_vol(t)}")
    print("stats:", stats())
