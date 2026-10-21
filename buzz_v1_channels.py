#!/usr/bin/env python3
"""
🐝 Alpha Hive — buzz_v1 冻结通道层（v0.45.583，buzz_v1 根治阶段 2）
=================================================================

为什么有它
----------
`buzz_v1`（v0.45.556）冻结了「7 个通道值 → 分」这一层，通道值本身仍是生产 BuzzBee 算好、入档的数——生产一改通道算法 / 关键词表 /
阈值 / 行情窗口，buzz_v1 的输入就跟着变，只能靠人登记边界时声明「动没动输入」。根治（用户 2026-10-20 定）：原始观测在取数处落档
（`observation_archive` v0.45.568 / `observation_layer` v0.45.580），本模块把「原始观测 → 7 个通道值」也冻结下来——
之后生产怎么改 Buzz 的解释代码都碰不到 buzz_v1。

定义锚在代码
------------
逐字抄自 **H1 窗口起点 2026-10-12 实际运行的代码**（`0993efde`，生产日志 `alpha_hive.code_version` 核实）加上窗口内**已登记确认**的
测量修正（行情：v0.45.505 / 557 / 562 / 563 / 564 / 566，`signal_code_fingerprints.ACKS`）。逐段出处：
- Reddit：`reddit_sentiment.RedditSentimentClient.get_ticker_sentiment` 的评分（查票、三态 24h 基线、加减分、clamp、round 1 位）；
- 新闻：`newsapi_client` 三个取数函数里「条目 → 文章」那段、`_label_sentiment`（Yahoo 关键词打标）、`_build_result`
  （清洗、标题 Jaccard 去重、按标签计数算分）；关键词表 / 去重阈值抄自 `config.SENTIMENT_KEYWORDS` / `NEWS_FILTER_CONFIG`
  （10-12 与现行逐字相同，v0.45.583 核对）；
- 热搜：`yahoo_trending.get_ticker_attention` 的名次分档；F&G：`fear_greed._fetch_cnn_fg` / `_fetch_alternative_me` 的取值与换算；
- 行情：`data_pipeline._drop_forming_bar` + `_session_last_minute` + `_fetch_history_metrics` / `YFinanceSource.fetch` 的三个公式，
  补跑路径 `_fetch_historical_stock_data`；收盘时刻规则抄自 `is_trading_day.session_close_et`；
- 通道映射：`swarm_agents/buzz_bee.py` 合成之前的那段 + `swarm_agents.cache._safe_score` + `config.AGENT_SCORING` 的两组阈值。
**本模块不 import 任何生产评分 / 取数代码，也不读 config**（`tests/test_buzz_v1_channels.py` 的 AST 守卫钉住）；与生产逐例对拍的差分
测试也在那里——生产先改了、却没登记，开发期就红。

照抄的含义（别「顺手修」）
-------------------------
冻结保证的是「检验对象不变」，不保证对象正确。下面这些都是窗口起点的真实行为，照抄、不修（要修 = 新定义 buzz_v2，重新登记）：
- 新闻时效加权是死代码：`_build_result` 读文章的 `published` 键，而各取数函数写的是 `published_at` ⇒ 权重恒取不到 ⇒ 分数恒为
  「看多篇数 / 总篇数」。本模块直接写成后者（行为逐位相同）。
- 关键词表里的多词短语（"record high" 等）永远匹配不上（按空格切词后比集合）。
- F&G 取不到 CNN 时兜底的是 alternative.me 的**加密市场** F&G；动量 NaN 时 `max(-10, min(10, nan))` 得 10 ⇒ 动量通道 100；
  量比 NaN 时通道落到最低档 20。

数值（逐位相同的前提）
----------------------
生产的均值 / 标准差出自 pandas（底下是 numpy 的成对求和）。本模块用纯 Python 复刻：`_np_sum` = numpy 双精度 add.reduce 的成对求和
（< 8 个逐个加；≤ 128 个 8 路累加再两两合并、余数逐个加；更长的对半递归），平方用乘法（`x * x`，numpy 的 `**2` 就是它；Python 的
`** 2` 走 pow，偶尔差 1 ulp），开方 `math.sqrt`。实测与 numpy / pandas 逐位相同（v0.45.583：add.reduce 4000/4000、mean 3000/3000、
std 20000/20000）。**不依赖 pandas / numpy**：库升级改了数值，冻结版不跟。

输入
----
`channels_from_observations(ticker, block, load)`：`block` = BuzzBee 写的溯源块（`observation_archive.buzz_refs` 的产物，经
`observation_links` 表读回），`load(sha)` 按地址取回 `{"kind", "payload"}`。返回 7 个通道值 + 行情三字段，以及两组（情绪 / 行情）
是否完整；不完整的组报原因、不猜。
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# ── 冻结常数（逐字抄自窗口起点；与现行生产的逐项对照在 tests/test_buzz_v1_channels.py）────────────────
BULLISH_KWS = frozenset({
    "accelerates", "beat", "beats", "blowout", "boost", "boosted", "breakout", "breakthrough", "bullish", "buy",
    "climbed", "exceed", "exceeds", "expand", "expansion", "gain", "gains", "growth", "higher", "impressive",
    "jumped", "momentum", "optimistic", "outperform", "positive", "profit", "raise", "raised", "rallies", "rally",
    "record", "record high", "rises", "robust", "soar", "soars", "strong", "strong buy", "surge", "surges",
    "top pick", "upbeat", "upgrade", "upgrades", "upside", "win"})
BEARISH_KWS = frozenset({
    "bearish", "breakdown", "concern", "contraction", "crash", "crashes", "cut", "cuts", "decelerate", "decline",
    "disappoints", "downgrade", "downgrades", "drop", "drops", "fall", "falls", "fears", "fell", "headwind",
    "investigation", "lawsuit", "layoff", "loss", "losses", "lowered", "miss", "misses", "negative", "pessimistic",
    "plunge", "probe", "recall", "recession", "risk", "sell", "selloff", "slash", "slashed", "slowdown", "slumped",
    "tumbled", "underperform", "warning", "weak"})
VALID_LABELS = frozenset({"bullish", "bearish", "neutral"})
DEDUP_JACCARD = 0.5
AV_BULL_THRESHOLD, AV_BEAR_THRESHOLD = 0.15, -0.15
MASSIVE_LABELS = {"positive": "bullish", "negative": "bearish", "neutral": "neutral"}
NEWS_MIN_ARTICLES = 3
#: 量比 → 通道（`buzz_bee` 第 2 步；config.AGENT_SCORING["volume_thresholds"]）：严格大于才进上一档
VOLUME_STEPS = ((2.0, 80), (1.5, 65), (1.0, 50), (0.5, 35))
VOLUME_FLOOR = 20
#: 20 日波动率 → 通道（第 3 步；config.AGENT_SCORING["volatility_thresholds"]）
VOLATILITY_STEPS = ((60, 25), (40, 40), (20, 60))
VOLATILITY_FLOOR = 75
VOL_MIN_BARS = 19          # v0.45.564（窗口内已确认的测量修正）
NEUTRAL = 50.0

SENTIMENT_CHANNELS = ("reddit_signal", "news_signal", "yahoo_signal", "fear_greed_signal")
PRICE_CHANNELS = ("momentum_signal", "volume_signal", "volatility_signal")


class IncompleteObservations(Exception):
    """这一组的原始观测不全（缺链接 / 缺 payload / 用了但没存），重算不了——不猜。"""


# ────────────────────────────────────────────────────────────────────────────
# 数值：numpy 成对求和的纯 Python 复刻
# ────────────────────────────────────────────────────────────────────────────

def _pairwise(a: Sequence[float], lo: int, n: int) -> float:
    if n < 8:
        res = 0.0
        for i in range(n):
            res += a[lo + i]
        return res
    if n <= 128:
        r = [a[lo + j] for j in range(8)]
        i = 8
        while i < n - (n % 8):
            for j in range(8):
                r[j] += a[lo + i + j]
            i += 8
        res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]))
        while i < n:
            res += a[lo + i]
            i += 1
        return res
    n2 = n // 2
    n2 -= n2 % 8
    return _pairwise(a, lo, n2) + _pairwise(a, lo + n2, n - n2)


def _np_sum(a: Sequence[float]) -> float:
    return _pairwise(a, 0, len(a))


def _nanmean(vals: Sequence[Optional[float]]) -> float:
    """pandas `Series.mean()`（skipna）：NaN 位置按 0 参与求和、计数不含；全缺 ⇒ NaN。"""
    xs = [0.0 if _isnan(v) else float(v) for v in vals]
    cnt = sum(1 for v in vals if not _isnan(v))
    return _np_sum(xs) / cnt if cnt else float("nan")


def _std(vals: Sequence[float]) -> float:
    """pandas `Series.std()`（ddof=1，两遍法）——调用方保证无 NaN、至少 2 个。"""
    n = len(vals)
    avg = _np_sum(vals) / n
    return math.sqrt(_np_sum([(avg - v) * (avg - v) for v in vals]) / (n - 1))


def _isnan(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def safe_score(value: Any, default: float, lo: float, hi: float) -> float:
    """`swarm_agents.cache._safe_score`。"""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return max(lo, min(hi, f))


# ────────────────────────────────────────────────────────────────────────────
# Reddit
# ────────────────────────────────────────────────────────────────────────────

def _find_ticker(rankings: Sequence[Mapping], ticker: str) -> Optional[Mapping]:
    for item in rankings:
        if item.get("ticker", "").upper() == ticker:
            return item
    return None


def reddit_sentiment_score(ticker: str, all_stocks: Sequence[Mapping], wsb: Sequence[Mapping]) -> float:
    t = ticker.upper()
    all_match, wsb_match = _find_ticker(all_stocks, t), _find_ticker(wsb, t)
    primary = all_match or wsb_match
    if primary is None:
        return 5.0                                              # 安静 / 兜底：`_quiet_result`

    def _num(key, default):
        v = primary.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default

    mentions, upvotes, rank = _num("mentions", 0), _num("upvotes", 0), _num("rank", 999)
    raw24 = primary.get("mentions_24h_ago")
    known = isinstance(raw24, (int, float)) and not isinstance(raw24, bool)
    m24 = raw24 if known else 0
    delta = mentions - m24
    if not known:
        mom = 0.0
    elif m24 > 0:
        mom = (delta / m24) * 100
    else:
        mom = 100.0 if mentions > 0 else 0.0
    score = 5.0
    if rank <= 3:
        score += 1.5
    elif rank <= 10:
        score += 1.0
    elif rank <= 25:
        score += 0.5
    if mom > 50:
        score += 1.5
    elif mom > 20:
        score += 1.0
    elif mom > 0:
        score += 0.5
    elif mom < -30:
        score -= 1.0
    if mentions > 0:
        quality = upvotes / mentions
        if quality > 5:
            score += 0.5
        elif quality < 1:
            score -= 0.5
    if wsb_match:
        wr = wsb_match.get("rank")
        wsb_rank = wr if isinstance(wr, (int, float)) and not isinstance(wr, bool) else 999
        if wsb_rank <= 5:
            score += 0.5
    score = max(1.0, min(10.0, score))
    return round(score, 1)


def reddit_signal(ticker: str, all_stocks: Sequence[Mapping], wsb: Sequence[Mapping]) -> float:
    return safe_score(reddit_sentiment_score(ticker, all_stocks, wsb), 5.0, 0, 10) * 10


# ────────────────────────────────────────────────────────────────────────────
# 新闻
# ────────────────────────────────────────────────────────────────────────────

def _label_by_keywords(articles: List[Dict]) -> None:
    for art in articles:
        words = set((art.get("title", "") + " " + art.get("summary", "")).lower().split())
        bull, bear = len(words & BULLISH_KWS), len(words & BEARISH_KWS)
        if bull > bear:
            art["sentiment_label"] = "bullish"
        elif bear > bull:
            art["sentiment_label"] = "bearish"


def news_articles(kind: str, payload: Mapping) -> List[Dict]:
    """「条目 → 文章」（只留影响分数的字段：标题 / 摘要 / 标签）。"""
    mx = payload["max_articles"]
    out: List[Dict] = []
    if kind == "news.massive":
        sym, ticker = payload["symbol"], payload["ticker"]
        for item in payload["results"][:mx]:
            ins = next((x for x in (item.get("insights") or [])
                        if isinstance(x, dict) and x.get("ticker") in (sym, ticker)), None)
            out.append({"title": item.get("title", ""), "summary": (item.get("description") or "")[:300],
                        "sentiment_label": MASSIVE_LABELS.get((ins or {}).get("sentiment"), "neutral")})
        return out
    if kind == "news.alpha_vantage":
        ticker = payload["ticker"]
        for item in payload["feed"][:mx]:
            ts = next((s for s in item.get("ticker_sentiment", []) if s.get("ticker") == ticker), {})
            try:
                v = float(ts.get("ticker_sentiment_score", "0"))
            except (ValueError, TypeError):
                v = 0.0
            if math.isnan(v) or math.isinf(v):
                v = 0.0
            label = "bullish" if v > AV_BULL_THRESHOLD else "bearish" if v < AV_BEAR_THRESHOLD else "neutral"
            out.append({"title": item.get("title", ""), "summary": item.get("summary", "")[:300], "sentiment_label": label})
        return out
    if kind == "news.yahoo_finance":
        for item in payload["news"][:mx]:
            out.append({"title": item.get("title", ""), "summary": item.get("summary", item.get("title", ""))[:300],
                        "sentiment_label": "neutral"})
        _label_by_keywords(out)
        return out
    raise IncompleteObservations(f"不认识的新闻观测种类 {kind!r}")


def _dedupe(articles: List[Dict]) -> List[Dict]:
    seen: List[set] = []
    result: List[Dict] = []
    for art in articles:
        words = set(art.get("title", "").lower().split())
        if not words:
            result.append(art)
            continue
        if any((len(words | p) > 0 and len(words & p) / len(words | p) >= DEDUP_JACCARD) for p in seen):
            continue
        seen.append(words)
        result.append(art)
    return result


def news_result(articles: List[Dict]) -> Dict[str, Any]:
    """`_build_result` 里决定分数的部分：清洗 → 去重 → 计数 → 分数（时效加权是死代码，见模块说明）。"""
    if not articles:
        return {"is_real_data": False}
    cleaned = []
    for art in articles:
        title = (art.get("title") or "").strip()
        if not title:
            continue
        lab = art.get("sentiment_label", "neutral")
        lab = lab.lower() if isinstance(lab, str) and lab.lower() in VALID_LABELS else "neutral"
        cleaned.append({"title": title[:200], "summary": (art.get("summary") or "")[:300], "sentiment_label": lab})
    if not cleaned:
        return {"is_real_data": False}
    kept = _dedupe(cleaned)
    total = len(kept)
    bullish = sum(1 for a in kept if a["sentiment_label"] == "bullish")
    ratio = bullish / total if total > 0 else 0.5
    raw = 1.0 + ratio * 9.0
    raw = 5.0 if (math.isnan(raw) or math.isinf(raw)) else max(0.0, min(10.0, float(raw)))   # models.clean_score
    return {"is_real_data": True, "total_articles": total, "sentiment_score": max(1.0, min(10.0, round(raw, 2)))}


def news_signal(kind: str, payload: Mapping) -> float:
    r = news_result(news_articles(kind, payload))
    if r.get("is_real_data") and r.get("total_articles", 0) >= NEWS_MIN_ARTICLES:
        return safe_score(r.get("sentiment_score"), 5.0, 0.0, 10.0) * 10
    return 50


# ────────────────────────────────────────────────────────────────────────────
# 热搜 / F&G
# ────────────────────────────────────────────────────────────────────────────

def yahoo_signal(ticker: str, symbols: Sequence[str]) -> float:
    if not symbols:
        return NEUTRAL                                          # 取不到 ⇒ is_real_data=False ⇒ Buzz 留 50.0
    t = ticker.upper()
    rank = next((i for i, s in enumerate(symbols, 1) if s == t), None)
    if rank is None:
        return 4.5 * 10
    score = 8.5 if rank <= 3 else 7.5 if rank <= 8 else 6.5 if rank <= 15 else 6.0
    return score * 10


def fear_greed_signal(kind: str, payload: Mapping) -> float:
    if kind == "fear_greed.cnn":
        raw = payload["fear_and_greed"].get("score")
        if raw is None:
            raise IncompleteObservations("CNN 观测里没有 score（生产此时不会登记这份观测）")
        value = int(round(float(raw)))
    elif kind == "fear_greed.alternative_me":
        value = int(payload["data"].get("value", 50))
    else:
        raise IncompleteObservations(f"不认识的 F&G 观测种类 {kind!r}")
    return round(max(1.0, min(10.0, value / 10.0)), 1) * 10


# ────────────────────────────────────────────────────────────────────────────
# 行情
# ────────────────────────────────────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def session_last_minute(d: dt.date) -> dt.time:
    """`data_pipeline._session_last_minute`：该场收盘时刻 − 1 分钟（收盘规则抄 `is_trading_day.session_close_et`）。"""
    early = (d == _nth_weekday(d.year, 11, 3, 4) + dt.timedelta(days=1)
             # 12/24 是交易日才提前收：周末不开；12/25 落周六时 12/24（周五）是圣诞节的补休日
             or (d.month == 12 and d.day == 24 and d.weekday() < 5 and dt.date(d.year, 12, 25).weekday() != 5)
             or (d.month == 7 and d.day == 3 and dt.date(d.year, 7, 4).weekday() in (1, 2, 3, 4)))
    close = dt.time(13, 0) if early else dt.time(16, 0)
    return (dt.datetime.combine(d, close) - dt.timedelta(minutes=1)).time()


def daily_rows(payload: Mapping) -> List[Tuple[str, Optional[float], Optional[float]]]:
    """yfinance 日线观测 → [(交易所日期 ISO, 收盘, 成交量)]（null = NaN）。"""
    cols = list(payload["columns"])
    ic, iv = cols.index("Close"), cols.index("Volume")
    return [(ts[:10], row[ic], row[iv]) for ts, row in zip(payload["index"], payload["data"])]


def drop_forming_bar(rows: List[Tuple], clock_iso: Optional[str]) -> List[Tuple]:
    """`data_pipeline._drop_forming_bar`：末根是交易所当日、且时钟早于该场最后一根分钟线 ⇒ 去掉。"""
    if len(rows) < 3 or clock_iso is None:
        return rows
    xnow = dt.datetime.fromisoformat(clock_iso)
    if rows[-1][0] == xnow.date().isoformat() and xnow.time() < session_last_minute(xnow.date()):
        return rows[:-1]
    return rows


def _f(v: Any) -> float:
    return float("nan") if v is None else float(v)


def _div(b: float, a: float) -> float:
    """numpy 的浮点除法语义：除以 0 不抛，得 ±inf（0/0、nan/0 得 nan）。生产在 numpy 上做这几个除法。"""
    if a == 0:
        if b == 0 or math.isnan(b):
            return float("nan")
        return math.copysign(math.inf, b) * math.copysign(1.0, a)
    return b / a


def price_fields(rows: List[Tuple]) -> Dict[str, Optional[float]]:
    """`_fetch_history_metrics` / `YFinanceSource.fetch` 的三个公式（行已过末根判断 / 补跑窗口）。"""
    closes = [_f(r[1]) for r in rows]
    vols = [r[2] for r in rows]
    out: Dict[str, Optional[float]] = {"momentum_5d": None, "volume_ratio": None, "volatility_20d": None}
    if len(rows) >= 5:
        out["momentum_5d"] = float((_div(closes[-1], closes[-5]) - 1) * 100)
    recent = _f(vols[-1])
    avg = _nanmean(vols[-20:]) if len(rows) >= 20 else _nanmean(vols)
    if not (math.isnan(avg) or avg <= 0):
        out["volume_ratio"] = _div(recent, avg)
    if len(rows) >= VOL_MIN_BARS:
        rets = [_div(b, a) - 1 for a, b in zip(closes[:-1], closes[1:])]
        rets = [x for x in rets if not math.isnan(x)]
        if len(rets) >= 2:
            vol = float(_std(rets) * (252 ** 0.5) * 100)
            if not (math.isnan(vol) or math.isinf(vol)):
                out["volatility_20d"] = vol
    return out


def _window_lo(as_of: dt.date) -> dt.date:
    """`data_pipeline._volatility_20d_window_lo`：上个日历月同一天；没有那一天取上月最后一天。"""
    y, m = (as_of.year, as_of.month - 1) if as_of.month > 1 else (as_of.year - 1, 12)
    for day in (as_of.day, 30, 29, 28):
        try:
            return as_of.replace(year=y, month=m, day=day)
        except ValueError:
            continue
    raise ValueError(as_of)


def price_fields_from_daily(payload: Mapping, clock_iso: Optional[str], has_clock: bool) -> Dict[str, Optional[float]]:
    """按请求形状分两条路径：实时（`period=1mo`，过末根判断）/ `--date` 补跑（`start/end`，截到 (as_of−1 月, as_of]）。"""
    rows = daily_rows(payload)
    req = payload.get("request") or {}
    if "period" in req:
        if len(rows) < 2:
            raise IncompleteObservations("日线不足 2 根（生产此时走回落，应登记缺口）")
        if len(rows) >= 3 and not has_clock:
            raise IncompleteObservations("日线 ≥ 3 根却没有登记交易所时钟")
        rows = drop_forming_bar(rows, clock_iso)
        if len(rows) < 2:
            raise IncompleteObservations("去掉形成中的末根后不足 2 根（生产此时走回落）")
        return price_fields(rows)
    if "start" in req and "end" in req:
        as_of = dt.date.fromisoformat(str(req["end"])[:10]) - dt.timedelta(days=1)
        rows = [r for r in rows if r[0] <= as_of.isoformat()]
        rows = [r for r in rows if not _isnan(r[1])]
        if not rows or rows[-1][1] <= 0:
            raise IncompleteObservations("补跑：该日没有有效收盘（生产此时标记不可用）")
        if rows[-1][0] != as_of.isoformat():
            raise IncompleteObservations("补跑：末行不是目标日（生产此时改用云端快照拼接，应登记缺口）")
        lo = _window_lo(as_of).isoformat()
        return price_fields([r for r in rows if r[0] > lo])
    raise IncompleteObservations(f"认不出的日线请求形状 {sorted(req)}")


def momentum_channel(m: Optional[float]) -> float:
    if m is None:
        return 50.0
    return (max(-10, min(10, m)) + 10) / 20 * 100


def volume_channel(vr: Optional[float]) -> float:
    if vr is None:
        return 50
    for th, v in VOLUME_STEPS:
        if vr > th:
            return v
    return VOLUME_FLOOR


def volatility_channel(v20: Optional[float]) -> float:
    if v20 is None:
        return 50
    for th, v in VOLATILITY_STEPS:
        if v20 > th:
            return v
    return VOLATILITY_FLOOR


# ────────────────────────────────────────────────────────────────────────────
# 溯源块 → 通道
# ────────────────────────────────────────────────────────────────────────────

Loader = Callable[[str], Optional[Mapping]]


def _need(load: Loader, sha: Any, what: str) -> Mapping:
    doc = load(sha) if isinstance(sha, str) else None
    if doc is None:
        raise IncompleteObservations(f"{what} 的观测不在档（{sha!r}）")
    return doc


def sentiment_channels(ticker: str, block: Mapping, load: Loader) -> Dict[str, float]:
    """四个情绪通道。`expected=False` 的通道 = 生产当时没拿到真实数据 ⇒ 中性（与生产同值同类型）。"""
    out: Dict[str, float] = {}
    for ch in ("reddit", "news", "yahoo", "fear_greed"):
        if not isinstance(block.get(ch), Mapping):
            raise IncompleteObservations(f"溯源块里没有 {ch}")
    r = block["reddit"]
    if r.get("expected"):
        ref = r.get("ref")
        if not isinstance(ref, Mapping):
            raise IncompleteObservations("Reddit 缺链接")
        lists = {f: _need(load, ref.get(f), f"Reddit {f}")["payload"]["results"] for f in ("all-stocks", "wallstreetbets")}
        out["reddit_signal"] = reddit_signal(ticker, lists["all-stocks"], lists["wallstreetbets"])
    else:
        out["reddit_signal"] = 50
    n = block["news"]
    if n.get("expected"):
        doc = _need(load, n.get("ref"), "新闻")
        out["news_signal"] = news_signal(doc["kind"], doc["payload"])
    else:
        out["news_signal"] = 50
    y = block["yahoo"]
    out["yahoo_signal"] = (yahoo_signal(ticker, _need(load, y.get("ref"), "热搜")["payload"]["symbols"])
                           if y.get("expected") else NEUTRAL)
    f = block["fear_greed"]
    if f.get("expected"):
        doc = _need(load, f.get("ref"), "F&G")
        out["fear_greed_signal"] = fear_greed_signal(doc["kind"], doc["payload"])
    else:
        out["fear_greed_signal"] = NEUTRAL
    return out


def price_inputs(block: Mapping, load: Loader) -> Dict[str, Optional[float]]:
    """三个行情字段（动量原值也给背离调整用）。"""
    p = block.get("price")
    if not isinstance(p, Mapping):
        raise IncompleteObservations("溯源块里没有 price（v0.45.580 之前的扫描没存行情观测）")
    if not p.get("expected"):
        return {"momentum_5d": None, "volume_ratio": None, "volatility_20d": None}
    ref = p.get("ref")
    if not isinstance(ref, Mapping) or not ref:
        raise IncompleteObservations("行情观测不全：" + "、".join(p.get("gaps") or ["缺链接"]))
    extra = sorted(set(ref) - {"yf_daily", "exchange_clock"})
    if extra:
        raise IncompleteObservations(f"行情用到了冻结层不认识的观测：{extra}")
    daily = _need(load, ref.get("yf_daily"), "日线")
    clock_iso = None
    if "exchange_clock" in ref:
        clock_iso = _need(load, ref["exchange_clock"], "交易所时钟")["payload"]["spy_last_minute"]
    return price_fields_from_daily(daily["payload"], clock_iso, "exchange_clock" in ref)


def channels_from_observations(ticker: str, block: Mapping, load: Loader) -> Dict[str, Any]:
    """`{"sentiment": {4 个通道} | {"incomplete": 原因}, "price": {3 个字段 + 3 个通道} | {"incomplete": 原因}}`。"""
    out: Dict[str, Any] = {}
    try:
        out["sentiment"] = sentiment_channels(ticker, block, load)
    except IncompleteObservations as e:
        out["sentiment"] = {"incomplete": str(e)}
    try:
        f = price_inputs(block, load)
        out["price"] = {**f, "momentum_signal": momentum_channel(f["momentum_5d"]),
                        "volume_signal": volume_channel(f["volume_ratio"]),
                        "volatility_signal": volatility_channel(f["volatility_20d"])}
    except IncompleteObservations as e:
        out["price"] = {"incomplete": str(e)}
    return out
