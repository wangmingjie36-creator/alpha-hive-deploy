#!/usr/bin/env python3
"""
美国财政部日度收益率曲线（v0.45.60）
====================================
`fred_macro` 的 10Y / 5Y / 2Y 此前走 yfinance 的 `^TNX` / `^FVX`。
2026-08-27 全天 687 次 429 把它们连同整块宏观打成了 `data_source: "fallback"`，
报告里出现的 `treasury_10y: 4.5` 是兜底常量，不是观测值。

为什么用财政部而不是 FRED
--------------------------
FRED 的 `DGS10` **转发的正是这份数据**，但要晚一天：实测 2026-08-28 查询时
FRED 最新是 08-26，财政部已有 **08-27**。扫描在当日 17:00 ET 跑，
FRED 那时还没发布当天的值 —— 所以 FRED 只适合补跑，当日必须直接问财政部。

- 免 API key、免注册
- 一次请求返回**整月**所有交易日，补跑任意目标日不需要额外请求
- 发布时刻约在当日 15:30 ET（⚠️ 待验证：本模块首次接入时应记录实际可得时间，
  见 `_LAG_NOTE`）

口径
----
返回的是 **par yield（票面收益率）**，与 `^TNX` 的口径一致但**不需要除以 10**
（`^TNX` 是收益率 ×10）。接线时别再除。

拿不到就返回 None，绝不返回常量 —— 4.5 与「真的是 4.5%」无法区分。
"""

from __future__ import annotations

import re
import time
import urllib.request
from typing import Dict, Optional

try:
    from hive_logger import get_logger
    _log = get_logger("treasury")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.treasury")

_BASE = ("https://home.treasury.gov/resource-center/data-chart-center/"
         "interest-rates/pages/xml?data=daily_treasury_yield_curve"
         "&field_tdr_date_value_month=")

# 发布时刻的经验值，尚未实测确认。扫描在 17:00 ET，若实际晚于此需要改走 T-1。
_LAG_NOTE = "约 15:30 ET 发布（待验证）"

# 只取评分链真正用到的三个期限。多解析几个字段不要钱，但多一个字段就多一处
# 「取到了却没人读」的可能 —— 见 MEMORY「死字段：算了没人读」。
_TENORS = {
    "y2":  "BC_2YEAR",
    "y5":  "BC_5YEAR",
    "y10": "BC_10YEAR",
}

_CACHE: Dict[str, Dict] = {}
_CACHE_TS: Dict[str, float] = {}
_TTL = 3600.0          # 曲线一天只更新一次，一小时足够


def _fetch_month(yyyymm: str, attempts: int = 3) -> Optional[str]:
    """抓整月 XML。带退避重试。

    为什么要重试：实测 2026-08-28 连续两次调用，一次成功、一次
    `RemoteDisconnected`。这里**一次失败的代价是 3 个字段掉回 yfinance**，
    而 yfinance 正是要绕开的那个 —— 值得多试两次。

    退避 1.5s / 3s：财政部没有已知的速率限制，这里防的是瞬时连接断开，
    不是配额。与 yfinance 的 429 是两类故障，不该套同一套退避
    （对 429 加倍施压正是 v0.45.56 修掉的错）。
    """
    from http_gate import urlopen_gated
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(_BASE + yyyymm,
                                         headers={"User-Agent": "alpha-hive/1.0"})
            return urlopen_gated(req, timeout=30).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - 任何失败都退化为「不可得」
            last = e
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    _log.warning("财政部收益率曲线抓取失败 %s（试了 %d 次）: %s", yyyymm, attempts, last)
    return None


def _parse(xml: str) -> Dict[str, Dict[str, float]]:
    """→ {"YYYY-MM-DD": {"y2":..,"y5":..,"y10":..}}

    用正则而非 XML 解析器：这份 Atom feed 的命名空间前缀历史上变过，
    而我们只要四个标量。解析器换 schema 会整份炸掉，正则只会少匹配到，
    后者的失败方式更安全（少 = 报不可得，而不是报个错值）。
    """
    dates = re.findall(r"<d:NEW_DATE[^>]*>([^<]+)</d:NEW_DATE>", xml)
    cols = {k: re.findall(rf"<d:{tag}[^>]*>([^<]*)</d:{tag}>", xml)
            for k, tag in _TENORS.items()}
    out: Dict[str, Dict[str, float]] = {}
    for i, raw_date in enumerate(dates):
        d = raw_date[:10]
        row = {}
        for k, vals in cols.items():
            if i >= len(vals):
                continue
            try:
                v = float(vals[i])
            except (TypeError, ValueError):
                continue
            # 合理区间：负利率时代美债 par yield 也没到过 -1%，20% 是 1981 峰值上方
            if -1.0 <= v <= 20.0:
                row[k] = v
        if row:
            out[d] = row
    return out


def get_yield_curve(date: Optional[str] = None) -> Optional[Dict]:
    """取某日的收益率曲线。`date=None` 表示「最新可得的那天」。

    Returns
    -------
    {"date": "YYYY-MM-DD", "y2": float, "y5": float, "y10": float,
     "source": "treasury_gov", "is_latest": bool}   或 None

    `date` 指定但当天无数据（周末/假日/尚未发布）时返回 None —— **不回退到
    前一交易日**。补跑要的是那一天的值，给前一天的值而不说，就是伪造。
    调用方需要就近取值时应自己显式回退并标注。
    """
    if date:
        yyyymm = date[:4] + date[5:7]
    else:
        yyyymm = time.strftime("%Y%m")

    now = time.time()
    if yyyymm not in _CACHE or now - _CACHE_TS.get(yyyymm, 0) > _TTL:
        xml = _fetch_month(yyyymm)
        if xml is None:
            return None
        if xml.lstrip().lower().startswith(("<!doctype", "<html")):
            _log.warning("财政部返回 HTML 而非 XML（%s），视为不可得", yyyymm)
            return None
        parsed = _parse(xml)
        if not parsed:
            _log.warning("财政部 XML 解析出 0 条（%s）——schema 可能变了", yyyymm)
            return None
        _CACHE[yyyymm] = parsed
        _CACHE_TS[yyyymm] = now

    rows = _CACHE[yyyymm]
    if not rows:
        return None
    latest = max(rows)
    key = date if date else latest
    if key not in rows:
        _log.info("财政部曲线无 %s 的数据（最新 %s）", key, latest)
        return None
    out = dict(rows[key])
    out.update({"date": key, "source": "treasury_gov", "is_latest": key == latest})
    return out


def clear_cache() -> None:
    """测试用。"""
    _CACHE.clear()
    _CACHE_TS.clear()


if __name__ == "__main__":  # pragma: no cover - 手工核对
    import json
    print(json.dumps(get_yield_curve(), ensure_ascii=False, indent=2))
    print(json.dumps(get_yield_curve("2026-08-27"), ensure_ascii=False, indent=2))
