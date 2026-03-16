"""
国会议员交易追踪模块
━━━━━━━━━━━━━━━━━━
数据源：Quiver Quant 免费公开端点（无需 API Key）
https://api.quiverquant.com/beta/live/congresstrading
返回最近 1000 条国会披露交易（House + Senate）

为什么重要：
  - 国会议员在立法前 45 天内必须申报交易（STOCK Act）
  - 历史胜率显著高于市场（尤其委员会成员）
  - 大额买入（>$50k）是最强信号
  - ExcessReturn 字段直接给出相对 SPY 的 alpha 表现

信号优先级：
  >$250k Purchase  + 委员会主席  → 极高价值信号
  $50k-$250k Buy   + 相关委员会  → 高价值信号
  <$15k 交易       + 非相关议员  → 低价值（例行披露）
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

_log = logging.getLogger("alpha_hive.congress_trades")

_ENDPOINT = "https://api.quiverquant.com/beta/live/congresstrading"
_CACHE_PATH = Path(__file__).parent / "cache" / "congress_trades.json"
_CACHE_TTL = 3600 * 4  # 4 小时（国会披露通常有 45 天延迟，无需高频刷新）
_lock = threading.Lock()

# 金额范围 → 中位数估算
_AMOUNT_MAP = {
    "$1,001 - $15,000":      8000,
    "$15,001 - $50,000":    32000,
    "$50,001 - $100,000":   75000,
    "$100,001 - $250,000": 175000,
    "$250,001 - $500,000": 375000,
    "$500,001 - $1,000,000": 750000,
    "$1,000,001 - $5,000,000": 3000000,
    "$5,000,001 - $25,000,000": 15000000,
    "Over $25,000,000": 25000000,
}

# 关键委员会（与科技/金融/医疗/国防高度相关）
_KEY_COMMITTEES_KEYWORDS = [
    "Armed Services", "Finance", "Banking", "Commerce", "Science",
    "Intelligence", "Appropriations", "Energy", "Health", "Technology",
    "Ways and Means", "Financial Services",
]


def get_congress_trades(
    tickers: Optional[List[str]] = None,
    days_back: int = 60,
    min_amount: int = 0,
    force_refresh: bool = False,
) -> List[Dict]:
    """
    获取国会议员最新交易记录，可按股票代码过滤。

    Args:
        tickers:       过滤特定标的列表（None = 全量）
        days_back:     仅返回最近 N 天的交易
        min_amount:    最低金额过滤（默认 0，不过滤）
        force_refresh: 强制忽略缓存

    Returns:
        交易列表，每条包含：
        {
          ticker, representative, party, house,
          transaction, amount_est, range, report_date,
          transaction_date, days_since_transaction,
          excess_return, price_change, signal_strength,
          description,
        }
    """
    with _lock:
        raw = _load_cached(force_refresh)
        if raw is None:
            raw = _fetch_raw()
            _save_cache(raw)

    if not raw:
        return []

    cutoff = datetime.now() - timedelta(days=days_back)
    results = []

    for item in raw:
        try:
            # 日期过滤
            tx_date_str = item.get("TransactionDate") or item.get("ReportDate", "")
            if not tx_date_str:
                continue
            tx_date = datetime.strptime(tx_date_str[:10], "%Y-%m-%d")
            if tx_date < cutoff:
                continue

            # 标的过滤
            ticker_raw = (item.get("Ticker") or "").strip().upper()
            if not ticker_raw or ticker_raw in ("N/A", "--", ""):
                continue
            if tickers and ticker_raw not in [t.upper() for t in tickers]:
                continue

            # 金额估算
            range_str = item.get("Range", "")
            amount_est = _AMOUNT_MAP.get(range_str, 0)
            if amount_est < min_amount:
                continue

            # 信号强度评分（0~10）
            strength = _calc_signal_strength(item, amount_est)

            results.append({
                "ticker": ticker_raw,
                "representative": item.get("Representative", ""),
                "party": item.get("Party", ""),
                "house": item.get("House", ""),      # "house" or "senate"
                "transaction": item.get("Transaction", ""),   # Purchase / Sale
                "amount_est": amount_est,
                "range": range_str,
                "report_date": item.get("ReportDate", ""),
                "transaction_date": tx_date_str[:10],
                "days_since_transaction": (datetime.now() - tx_date).days,
                "excess_return": item.get("ExcessReturn"),   # 相对 SPY 的超额收益
                "price_change": item.get("PriceChange"),
                "spy_change": item.get("SPYChange"),
                "description": item.get("Description", ""),
                "ticker_type": item.get("TickerType", ""),
                "signal_strength": strength,
            })
        except Exception as e:
            _log.debug("Congress trade parse error: %s | %s", e, item)
            continue

    # 按信号强度 + 金额降序排列
    results.sort(key=lambda x: (x["signal_strength"], x["amount_est"]), reverse=True)
    return results


def get_congress_trades_for_ticker(
    ticker: str,
    days_back: int = 90,
) -> Dict:
    """
    获取特定标的的国会交易汇总，供 ScoutBee 使用。

    Returns:
      {
        "ticker": str,
        "trades": [...],
        "buy_count": int,
        "sell_count": int,
        "net_amount_est": int,   # 买入 - 卖出 金额估算
        "latest_trade_date": str,
        "top_signal": str,       # 最强信号摘要
        "congress_score": float, # 0-10 国会信号强度
        "summary": str,          # 注入 prompt 的文字摘要
      }
    """
    trades = get_congress_trades(tickers=[ticker], days_back=days_back)

    buys = [t for t in trades if "purchase" in t["transaction"].lower()]
    sells = [t for t in trades if "sale" in t["transaction"].lower()]

    net_amount = sum(t["amount_est"] for t in buys) - sum(t["amount_est"] for t in sells)
    latest = trades[0]["transaction_date"] if trades else None

    # 综合评分（0-10）
    congress_score = 0.0
    if trades:
        # 买入多于卖出 → 加分
        buy_bias = (len(buys) - len(sells)) / max(len(trades), 1)
        amount_score = min(10, sum(t["amount_est"] for t in buys) / 100_000)
        strength_avg = sum(t["signal_strength"] for t in trades) / len(trades)
        congress_score = round((buy_bias * 3 + amount_score * 0.5 + strength_avg * 0.5), 1)
        congress_score = max(0.0, min(10.0, congress_score))

    top_signal = ""
    if trades:
        t = trades[0]
        top_signal = (
            f"{t['representative']} ({t['party']}) "
            f"{t['transaction']} {t['range']} "
            f"on {t['transaction_date']}"
        )

    return {
        "ticker": ticker.upper(),
        "trades": trades,
        "buy_count": len(buys),
        "sell_count": len(sells),
        "net_amount_est": net_amount,
        "latest_trade_date": latest,
        "top_signal": top_signal,
        "congress_score": congress_score,
        "summary": _format_summary(ticker, trades, buys, sells, net_amount, congress_score),
    }


def get_watchlist_congress_summary(tickers: List[str], days_back: int = 60) -> Dict[str, Dict]:
    """扫描整个 Watchlist 的国会交易（一次 API 调用，本地分 ticker 过滤）"""
    all_trades = get_congress_trades(days_back=days_back)
    result = {}
    for ticker in tickers:
        ticker_up = ticker.upper()
        trades = [t for t in all_trades if t["ticker"] == ticker_up]
        buys = [t for t in trades if "purchase" in t["transaction"].lower()]
        sells = [t for t in trades if "sale" in t["transaction"].lower()]
        net = sum(t["amount_est"] for t in buys) - sum(t["amount_est"] for t in sells)
        score = 0.0
        if trades:
            buy_bias = (len(buys) - len(sells)) / max(len(trades), 1)
            amount_score = min(10, sum(t["amount_est"] for t in buys) / 100_000)
            strength_avg = sum(t["signal_strength"] for t in trades) / len(trades)
            score = round(max(0.0, min(10.0, buy_bias * 3 + amount_score * 0.5 + strength_avg * 0.5)), 1)
        result[ticker_up] = {
            "buy_count": len(buys),
            "sell_count": len(sells),
            "net_amount_est": net,
            "congress_score": score,
            "top_signal": trades[0]["top_signal"] if trades and "top_signal" in trades[0] else "",
            "trades": trades[:5],  # 最多返回前 5 条
        }
    return result


# ── 内部工具函数 ──────────────────────────────────────────────────────────────

def _calc_signal_strength(item: Dict, amount_est: int) -> float:
    """计算单条交易的信号强度（0~10）"""
    score = 0.0
    tx = (item.get("Transaction") or "").lower()

    # 金额权重
    if amount_est >= 1_000_000:  score += 4.0
    elif amount_est >= 250_000:  score += 3.0
    elif amount_est >= 100_000:  score += 2.0
    elif amount_est >= 50_000:   score += 1.0
    else:                        score += 0.3

    # 买入优先（卖出可能是多元化操作，买入是主动信号）
    if "purchase" in tx:     score += 2.0
    elif "sale" in tx:       score += 0.5

    # Senate > House（参议员信息面更广）
    house_val = (item.get("House") or "").lower()
    if "senate" in house_val:  score += 1.0
    else:                      score += 0.5

    # ExcessReturn（历史上这条交易后的超额收益，如有）
    excess = item.get("ExcessReturn")
    if excess is not None:
        try:
            excess_f = float(excess)
            if excess_f > 20:    score += 2.0
            elif excess_f > 10:  score += 1.0
            elif excess_f > 5:   score += 0.5
        except (TypeError, ValueError):
            pass

    return round(min(10.0, score), 1)


def _format_summary(
    ticker: str,
    trades: List[Dict],
    buys: List[Dict],
    sells: List[Dict],
    net_amount: int,
    congress_score: float,
) -> str:
    """生成注入 LLM prompt 的文字摘要"""
    if not trades:
        return f"【国会议员交易 · {ticker}】近 90 天无披露记录"

    lines = [f"【国会议员交易 · {ticker}】(近90天, Quiver Quant)"]
    lines.append(
        f"买入: {len(buys)}笔 | 卖出: {len(sells)}笔 | "
        f"净方向: {'净买入' if net_amount > 0 else '净卖出'} "
        f"~${abs(net_amount):,.0f}"
    )
    lines.append(f"国会信号评分: {congress_score}/10")

    for t in trades[:3]:
        er = f" | 事后超额收益: {t['excess_return']:+.1f}%" if t.get("excess_return") is not None else ""
        lines.append(
            f"  • {t['representative']} ({t['party']}, {t['house'].title()}) "
            f"→ {t['transaction']} {t['range']} [{t['transaction_date']}]{er}"
        )

    return "\n".join(lines)


def _load_cached(force_refresh: bool) -> Optional[List]:
    if force_refresh or not _CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(_CACHE_PATH.read_text())
        ts = datetime.fromisoformat(cached.get("_ts", "2000-01-01"))
        if (datetime.now() - ts).total_seconds() < _CACHE_TTL:
            return cached.get("data")
    except Exception:
        pass
    return None


def _save_cache(data: List) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(
            {"_ts": datetime.now().isoformat(), "data": data},
            ensure_ascii=False,
        ))
    except Exception as e:
        _log.debug("Congress trades cache write failed: %s", e)


def _fetch_raw() -> List:
    try:
        req = urllib.request.Request(
            _ENDPOINT,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh)", "Accept": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=12)
        return json.loads(resp.read())
    except Exception as e:
        _log.warning("Congress trades fetch failed: %s", e)
        return []


# ── CLI 测试 ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        ticker = sys.argv[1].upper()
        result = get_congress_trades_for_ticker(ticker)
        print(result["summary"])
        print(f"\n总计 {len(result['trades'])} 条交易记录")
        for t in result["trades"][:5]:
            print(
                f"  {t['transaction_date']}  {t['representative']:30s}"
                f"  {t['transaction']:10s}  {t['range']:25s}"
                f"  strength={t['signal_strength']}"
            )
    else:
        # 全量最新高分交易
        trades = get_congress_trades(min_amount=50_000, days_back=30)
        print(f"近30天 ≥$50k 交易: {len(trades)} 条")
        for t in trades[:10]:
            print(
                f"  {t['transaction_date']}  {t['ticker']:6s}"
                f"  {t['representative']:28s}  {t['transaction']:10s}"
                f"  {t['range']:25s}  strength={t['signal_strength']}"
            )
