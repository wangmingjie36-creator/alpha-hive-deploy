#!/usr/bin/env python3
"""关注事项：人工维护的前瞻事件，**只进报告、不进评分**（v0.45.32）。

为什么单独一个模块而不是接回 catalyst 维度
------------------------------------------
前身 `catalysts.json` 与 `catalyst_refinement.py` 是人工维护的前瞻日历，
直接喂 ChronosBee 的 `catalysts_found` → catalyst 维度 → **final_score 的 18.78%**。
两种失败都真实发生过：

1. **腐烂**：`catalysts.json` 最后更新 2026-07-23，一个月后窗口内 0 事件；
   `catalyst_refinement` 的硬编码日期停在 2026-03-15，5 个月仍在被读取。
2. **编造**：VKTX 曾有两条 `critical` 级条目是错误信息（把二期试验当三期、
   把公司指引 2027 年的顶线数据写成 2026-08-15），在文件里驱动评分直到
   有人专门核实（见该文件当时的 `_vktx_note`）。违反 CLAUDE.md「不编数据」。

关键区别在**失败模式**：
- 喂评分时，一条错误的 critical 催化剂**静默推动 18.78% 的权重**，
  产出与真催化剂完全同形，人看不见。
- 只进报告时，失败降级为「误导一个能自己判断的读者」——可恢复。

所以本模块**没有**任何函数返回可用于评分的结构。这是刻意的约束，
`tests/test_watchlist_events.py` 有断言守住。

反腐烂设计
----------
- 过期条目不删除，标 `expired` 一起渲染 —— 静默消失正是前身烂到没人发现的原因
- 文件 mtime 随报告输出；超过 `STALE_AFTER_DAYS` 显示陈旧警告
- 每条强制带 `source` 与 `verified`，缺失即标 `⚠️ 未填`
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from typing import Dict, List, Optional

EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "watchlist_events.md")
STALE_AFTER_DAYS = 30
_HORIZON_DAYS = 90          # 报告里显示的前瞻窗口
_ROW = re.compile(r"^\s*\|(?!\s*[-:| ]+\|)(.+)\|\s*$")


def _parse_rows(text: str) -> List[Dict]:
    out: List[Dict] = []
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if len(cells) < 6:
            continue
        if cells[0] in ("标的", "------") or cells[0].startswith("--"):
            continue
        ticker, date, event, etype, source, verified = cells[:6]
        if not ticker or not date:
            continue
        try:
            _dt.date.fromisoformat(date)
        except ValueError:
            # 日期写坏了必须看得见，不能静默丢弃
            out.append({"ticker": ticker, "date": date, "event": event,
                        "type": etype, "source": source, "verified": verified,
                        "days_until": None, "status": "bad_date"})
            continue
        out.append({"ticker": ticker, "date": date, "event": event,
                    "type": etype, "source": source, "verified": verified})
    return out


def load_events(path: Optional[str] = None, today: Optional[str] = None) -> Dict:
    """读取关注事项。返回 dict，**不含任何可用于评分的字段**。

    Returns:
        {
          "events": [...],          # 每条带 days_until / status
          "file_mtime": "YYYY-MM-DD" | None,
          "days_since_update": int | None,
          "is_stale": bool,
          "available": bool,        # 文件不存在时 False（不是错误，是合法空状态）
        }
    """
    p = path or EVENTS_FILE
    ref = _dt.date.fromisoformat(today) if today else _dt.date.today()
    if not os.path.isfile(p):
        return {"events": [], "file_mtime": None, "days_since_update": None,
                "is_stale": False, "available": False}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            text = fh.read()
        mtime = _dt.date.fromtimestamp(os.path.getmtime(p))
    except OSError:
        return {"events": [], "file_mtime": None, "days_since_update": None,
                "is_stale": False, "available": False}

    rows = _parse_rows(text)
    for r in rows:
        if r.get("status") == "bad_date":
            continue
        d = _dt.date.fromisoformat(r["date"])
        r["days_until"] = (d - ref).days
        r["status"] = "expired" if r["days_until"] < 0 else "upcoming"

    days_since = (ref - mtime).days
    return {
        "events": sorted(rows, key=lambda r: r.get("date", "9999")),
        "file_mtime": mtime.isoformat(),
        "days_since_update": days_since,
        "is_stale": days_since > STALE_AFTER_DAYS,
        "available": True,
    }


def format_for_report(data: Optional[Dict] = None,
                      tickers: Optional[List[str]] = None) -> List[str]:
    """渲染成 markdown 行。tickers 非空时只显示这些标的。"""
    d = data if data is not None else load_events()
    md: List[str] = ["## 📌 关注事项（人工维护 · 不参与评分）", ""]

    if not d["available"]:
        md += ["- （无 `watchlist_events.md`，本段留空）", ""]
        return md

    md.append(f"> 来源：`watchlist_events.md`，最后更新 {d['file_mtime']}"
              f"（{d['days_since_update']} 天前）")
    if d["is_stale"]:
        md.append(f">")
        md.append(f"> ⚠️ **已超过 {STALE_AFTER_DAYS} 天未更新** —— "
                  f"下列内容可能已过时，判断前请自行核实。")
    md.append(">")
    md.append("> 本段**不影响任何评分**，仅供人工判断；每条的核实状态见末列。")
    md.append("")

    rows = d["events"]
    if tickers:
        keep = set(tickers)
        rows = [r for r in rows if r["ticker"] in keep]
    upcoming = [r for r in rows
                if r.get("status") == "upcoming" and (r.get("days_until") or 0) <= _HORIZON_DAYS]
    expired = [r for r in rows if r.get("status") == "expired"]
    broken = [r for r in rows if r.get("status") == "bad_date"]

    if upcoming:
        md.append(f"**未来 {_HORIZON_DAYS} 天内**")
        md.append("")
        for r in upcoming:
            v = r["verified"] or "⚠️ 未填"
            src = r["source"] or "⚠️ 未填来源"
            md.append(f"- **{r['ticker']}** {r['date']}（{r['days_until']:+d} 天）"
                      f"｜{r['event']}｜*{v}*｜{src}")
        md.append("")
    else:
        md.append("- 未来 90 天内无登记事件")
        md.append("")

    if expired:
        md.append(f"<details><summary>已过期 {len(expired)} 条（保留可见，"
                  f"静默消失正是前身腐烂到没人发现的原因）</summary>")
        md.append("")
        for r in expired:
            md.append(f"- ~~{r['ticker']} {r['date']}｜{r['event']}~~")
        md.append("")
        md.append("</details>")
        md.append("")
    if broken:
        md.append(f"⚠️ **{len(broken)} 条日期格式非法，请修**："
                  + "、".join(f"{r['ticker']}/{r['date']}" for r in broken))
        md.append("")
    return md


if __name__ == "__main__":  # pragma: no cover
    print("\n".join(format_for_report()))
