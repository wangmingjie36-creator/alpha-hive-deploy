"""
② PEAD 历史量化分析器（Post-Earnings Announcement Drift）
Alpha Hive — ChronosBee 插件模块

功能：
- 从 yfinance 获取历史财报日期
- 计算 T+1 / T+5 / T+10 / T+20 财报后价格漂移
- 结果缓存为 JSON，避免重复计算
- 提供摘要文字供 LLM 注入 CH3 催化剂推理
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

try:
    from hive_logger import PATHS, get_logger
    _log = get_logger("pead")
    _CACHE_DIR = str(PATHS.cache_dir)
except ImportError:
    import logging
    _log = logging.getLogger("pead")
    _CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")

try:
    import yfinance as yf
except ImportError:
    yf = None

_CACHE_TTL_DAYS = 7   # PEAD 结果缓存 7 天（财报历史不频繁变化）

# ──────────────────────────────────────────────────────────────────────────────
# 缓存 I/O
# ──────────────────────────────────────────────────────────────────────────────

def _cache_path(ticker: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"pead_{ticker.upper()}.json")


def _load_cache(ticker: str) -> Optional[Dict]:
    path = _cache_path(ticker)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        ts = data.get("generated_at", "")
        if ts:
            age = (datetime.now() - datetime.fromisoformat(ts)).days
            if age > _CACHE_TTL_DAYS:
                return None
        return data
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _save_cache(ticker: str, data: Dict) -> None:
    try:
        path = _cache_path(ticker)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        _log.debug("PEAD cache write failed for %s: %s", ticker, e)


# ──────────────────────────────────────────────────────────────────────────────
# 核心计算
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_earnings_dates(ticker: str) -> List[str]:
    """从 yfinance 获取历史财报日期（最近 8 季）"""
    if yf is None:
        return []
    try:
        t = yf.Ticker(ticker)
        # earnings_dates 返回 DataFrame，index 是日期
        df = t.earnings_dates
        if df is None or df.empty:
            return []
        dates = []
        for idx in df.index:
            try:
                if hasattr(idx, 'strftime'):
                    date_str = idx.strftime("%Y-%m-%d")
                else:
                    date_str = str(idx)[:10]
                # 只取过去的财报（非未来）
                if datetime.strptime(date_str, "%Y-%m-%d") < datetime.now():
                    dates.append(date_str)
            except (ValueError, TypeError):
                continue
        # 最近 8 季，降序排列
        dates.sort(reverse=True)
        return dates[:8]
    except Exception as e:
        _log.debug("yfinance earnings_dates unavailable for %s: %s", ticker, e)
        return []


def _compute_post_earnings_drift(
    ticker: str,
    earnings_dates: List[str],
    horizons: List[int] = (1, 5, 10, 20),
) -> List[Dict]:
    """
    计算每个财报日后 T+N 的价格漂移。
    返回 list of dict:
      { "earnings_date": "2024-11-20", "t1": 2.3, "t5": -1.2, "t10": 4.5, "t20": 6.1 }
    """
    if yf is None or not earnings_dates:
        return []

    max_horizon = max(horizons)
    # 下载覆盖所有财报日的历史价格（早3天 + 晚max_horizon+5天缓冲）
    try:
        earliest = min(earnings_dates)
        start = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        hist = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if hist.empty:
            return []
        # 统一 index 为字符串日期
        hist.index = [str(d)[:10] for d in hist.index]
        price_map: Dict[str, float] = {}
        for date, row in hist.iterrows():
            try:
                close = float(row["Close"])
                price_map[date] = close
            except (KeyError, TypeError, ValueError):
                continue
    except Exception as e:
        _log.debug("PEAD price download failed for %s: %s", ticker, e)
        return []

    results = []
    price_dates = sorted(price_map.keys())

    for e_date in earnings_dates:
        # 找财报日当天或之后第一个交易日
        base_date = None
        for d in price_dates:
            if d >= e_date:
                base_date = d
                break
        if base_date is None:
            continue

        base_price = price_map.get(base_date)
        if not base_price or base_price <= 0:
            continue

        base_idx = price_dates.index(base_date)
        entry: Dict = {"earnings_date": e_date, "base_price": round(base_price, 2)}

        for h in horizons:
            target_idx = base_idx + h
            if target_idx < len(price_dates):
                target_price = price_map.get(price_dates[target_idx])
                if target_price and target_price > 0:
                    drift_pct = round((target_price / base_price - 1) * 100, 2)
                    entry[f"t{h}"] = drift_pct
                else:
                    entry[f"t{h}"] = None
            else:
                entry[f"t{h}"] = None  # 数据不够

        results.append(entry)

    return results


def _aggregate_drift_stats(drift_records: List[Dict], horizons=(1, 5, 10, 20)) -> Dict:
    """汇总平均漂移、胜率（上涨比例）、最大漂移"""
    stats: Dict = {}
    for h in horizons:
        key = f"t{h}"
        vals = [r[key] for r in drift_records if r.get(key) is not None]
        if not vals:
            stats[key] = {"avg": None, "win_rate": None, "max": None, "min": None, "n": 0}
        else:
            avg = round(sum(vals) / len(vals), 2)
            win_rate = round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)
            stats[key] = {
                "avg": avg,
                "win_rate": win_rate,
                "max": round(max(vals), 2),
                "min": round(min(vals), 2),
                "n": len(vals),
            }
    return stats


# ──────────────────────────────────────────────────────────────────────────────
# 公共接口
# ──────────────────────────────────────────────────────────────────────────────

def get_pead_analysis(ticker: str, force_refresh: bool = False) -> Dict:
    """
    主接口：获取 PEAD 历史量化数据。
    优先读缓存，miss 时重新计算并写入缓存。

    返回 dict:
      {
        "ticker": "NVDA",
        "generated_at": "2026-03-18T...",
        "earnings_dates": [...],
        "drift_records": [...],
        "stats": { "t1": {...}, "t5": {...}, "t10": {...}, "t20": {...} },
        "summary": "近8季财报后平均漂移 T+1: +3.2% (胜率75%) | T+5: +5.1% (胜率63%)",
        "bias": "bullish" | "bearish" | "neutral",
      }
    """
    if not force_refresh:
        cached = _load_cache(ticker)
        if cached:
            return cached

    earnings_dates = _fetch_earnings_dates(ticker)
    drift_records = _compute_post_earnings_drift(ticker, earnings_dates)
    stats = _aggregate_drift_stats(drift_records)

    # 综合偏向判定（T+5 平均漂移为主判据）
    t5_avg = (stats.get("t5") or {}).get("avg")
    if t5_avg is not None:
        if t5_avg > 2.0:
            bias = "bullish"
        elif t5_avg < -2.0:
            bias = "bearish"
        else:
            bias = "neutral"
    else:
        bias = "neutral"

    # 生成摘要文字供 LLM 注入
    summary_parts = []
    for h, label in [(1, "T+1"), (5, "T+5"), (10, "T+10"), (20, "T+20")]:
        s = stats.get(f"t{h}", {})
        avg = s.get("avg")
        wr = s.get("win_rate")
        n = s.get("n", 0)
        if avg is not None and n >= 2:
            summary_parts.append(f"{label}: {avg:+.1f}% (胜率{wr:.0f}%, n={n})")
    summary = "近期财报后价格漂移 " + " | ".join(summary_parts) if summary_parts else "PEAD 数据不足"

    result = {
        "ticker": ticker,
        "generated_at": datetime.now().isoformat(),
        "earnings_dates": earnings_dates,
        "drift_records": drift_records,
        "stats": stats,
        "summary": summary,
        "bias": bias,
        "source": "yfinance" if earnings_dates else "unavailable",
    }
    _save_cache(ticker, result)
    return result


def format_pead_for_chronos(pead: Dict) -> str:
    """格式化 PEAD 结果供 ChronosBee discovery 文字使用"""
    if not pead or pead.get("source") == "unavailable":
        return ""
    summary = pead.get("summary", "")
    bias = pead.get("bias", "neutral")
    bias_label = {"bullish": "📈历史漂移偏多", "bearish": "📉历史漂移偏空", "neutral": "历史漂移中性"}.get(bias, "")
    return f"{bias_label} | {summary}" if bias_label and summary else summary


# ──────────────────────────────────────────────────────────────────────────────
# CLI 测试
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    _ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    _result = get_pead_analysis(_ticker, force_refresh=True)
    print(json.dumps(_result, indent=2, ensure_ascii=False))
