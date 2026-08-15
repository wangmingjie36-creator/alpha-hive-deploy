#!/usr/bin/env python3
"""
CBOE VIX 历史（v0.43.24 Step 2）
================================
VIX 现货与历史收盘，直接取自 CBOE 官方 CSV：

    https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv

为什么不继续用 yfinance
----------------------
`fred_macro` 原先直接调 yfinance 拿 ^VIX，**绕过了项目既定的 CBOE 优先链**
（CLAUDE.md：CBOE → yfinance → AV → Finnhub）。宏观数据在 30 只标的扫完之后才
抓，配额早被耗尽——实测 2026-08-14 全天 363 条 Too Many Requests，7 个宏观标的
全军覆没，于是整体降级到 base 常量 `vix=20.0`。88 个扫描日里 13 天如此，
2026 年 8 月的 9 个扫描日里占 5 天，且在恶化。

CBOE 这个 CSV 无 key、无限流、无并发配额，且**自带 1990 年至今的完整历史**
（9000+ 行），顺带解决了 252 日分位没有真实样本的问题。

诚实降级
--------
网络失败时优先用磁盘缓存（并如实返回缓存日期，调用方可自行判断新鲜度）；
缓存也没有则返回 `None` —— **绝不返回猜测值**。这正是本次事故的教训：
`fred_macro` 的 `vix=20.0` 是个合法 float，不会崩，只会一路冒充观测值。
"""

from __future__ import annotations

import csv
import io
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.cboe_vix")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.cboe_vix")

try:
    # 复用 cboe_options 的信号量：本机老 SSL 栈扛不住并发 HTTPS（实测 4 并发
    # 挂 50-70s 甚至 SSL EOF）。共用同一把锁才能真正串行化所有 CBOE 请求。
    from cboe_options import _CBOE_SEM
except Exception:  # pragma: no cover
    import threading
    _CBOE_SEM = threading.Semaphore(1)

_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
_CACHE_PATH = Path(__file__).parent / "cache" / "vix_history_cboe.csv"
_CACHE_TTL = 6 * 3600           # 6 小时：日频数据，一天扫一次绰绰有余
_NET_TIMEOUT = 15.0

# VIX 合理区间：史上最低 8.56（2017）、最高 82.69（2020-03）。超出即视为解析错误。
_MIN_VALID_VIX = 5.0
_MAX_VALID_VIX = 150.0

# 标准 IV/VIX Rank 窗口
VIX_PERCENTILE_WINDOW = 252


def _download() -> Optional[str]:
    """拉取 CBOE CSV 原文；失败返回 None（不抛，让调用方走缓存）"""
    try:
        with _CBOE_SEM:  # 串行化：避免并发压垮本机 SSL 栈
            req = urllib.request.Request(_VIX_URL, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=_NET_TIMEOUT).read()
        text = raw.decode("utf-8", errors="replace")
        if "DATE" not in text[:200]:
            _log.warning("CBOE VIX CSV 格式异常（首行无 DATE），丢弃")
            return None
        return text
    except Exception as e:  # noqa: BLE001 - 网络层什么都可能抛
        _log.warning("CBOE VIX 下载失败: %s", e)
        return None


def _write_cache(text: str) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _CACHE_PATH)  # 原子替换，避免读到半截文件
    except OSError as e:
        _log.debug("CBOE VIX 缓存写入失败（不阻断）: %s", e)


def _read_cache() -> Optional[str]:
    try:
        if _CACHE_PATH.exists():
            return _CACHE_PATH.read_text(encoding="utf-8")
    except OSError as e:
        _log.debug("CBOE VIX 缓存读取失败: %s", e)
    return None


def _cache_fresh() -> bool:
    try:
        return _CACHE_PATH.exists() and (time.time() - _CACHE_PATH.stat().st_mtime) < _CACHE_TTL
    except OSError:
        return False


def _parse(text: str) -> List[Tuple[str, float]]:
    """CSV → [(ISO 日期, 收盘)]，按日期升序。坏行跳过，不让单行毁掉整份历史。"""
    out: List[Tuple[str, float]] = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            d, c = (row.get("DATE") or "").strip(), (row.get("CLOSE") or "").strip()
            if not d or not c:
                continue
            try:
                # CBOE 用 M/D/YYYY；早年数据偶有 YYYY-MM-DD
                dt = datetime.strptime(d, "%m/%d/%Y") if "/" in d else datetime.strptime(d, "%Y-%m-%d")
                close = float(c)
            except (ValueError, TypeError):
                continue
            if _MIN_VALID_VIX <= close <= _MAX_VALID_VIX:
                out.append((dt.strftime("%Y-%m-%d"), close))
    except Exception as e:  # noqa: BLE001
        _log.warning("CBOE VIX CSV 解析失败: %s", e)
        return []
    out.sort(key=lambda x: x[0])
    return out


def get_vix_history(max_days: Optional[int] = None, force_refresh: bool = False) -> List[Tuple[str, float]]:
    """返回 [(ISO 日期, 收盘)] 升序。网络失败时回落到磁盘缓存；都没有则返回 []。"""
    text = None
    if not force_refresh and _cache_fresh():
        text = _read_cache()
    if text is None:
        text = _download()
        if text:
            _write_cache(text)
        else:
            text = _read_cache()  # 用陈旧缓存也好过没有
            if text:
                _log.info("CBOE VIX 使用陈旧缓存（网络不可用）")
    if not text:
        return []
    hist = _parse(text)
    return hist[-max_days:] if max_days else hist


def get_vix_spot() -> Optional[Tuple[float, str]]:
    """最新收盘 VIX → (值, ISO 日期)。拿不到返回 None，**不返回猜测值**。"""
    hist = get_vix_history()
    if not hist:
        return None
    date, close = hist[-1][0], hist[-1][1]
    return (close, date)


def get_vix_percentile(window: int = VIX_PERCENTILE_WINDOW) -> Optional[float]:
    """最新 VIX 在过去 `window` 个交易日中的**恐慌分位**（0~100）。

    注意方向：返回值是"比多少比例的交易日更高"。VIX 14.6 → 约 1~5，
    代表极度平静。若要"平静度分位"需自行取 100 - 本值——两者方向相反，
    混用会得到完全相反的结论。
    """
    hist = get_vix_history(max_days=window)
    if len(hist) < 30:  # 样本太少，分位无意义
        return None
    vals = [v for _, v in hist]
    cur = vals[-1]
    below = sum(1 for v in vals if v < cur)
    return round(below / len(vals) * 100, 1)


def get_vix_regime(vix: Optional[float]) -> str:
    """与 fred_macro 同口径的分档，供两条路径产出一致的标签"""
    if vix is None:
        return "unknown"
    if vix < 15:
        return "low"
    if vix < 20:
        return "moderate"
    if vix < 30:
        return "elevated"
    if vix < 40:
        return "high"
    return "spike"


if __name__ == "__main__":  # pragma: no cover
    spot = get_vix_spot()
    if spot:
        v, d = spot
        pct = get_vix_percentile()
        print(f"VIX {v:.2f} ({d})  regime={get_vix_regime(v)}")
        print(f"252 日恐慌分位: {pct}%（平静度 {round(100 - pct, 1) if pct is not None else 'N/A'}%）")
        print(f"历史样本: {len(get_vix_history())} 个交易日")
    else:
        print("VIX 不可用（网络与缓存均失败）")
