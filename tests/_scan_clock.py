"""财报信号 `scan` 的墙钟——**钉钟的唯一实现**（v0.45.648）。

为什么要有它：`earnings_vol_signal.scan` 给每一行的 `gex_ctx` 盖 `captured_at`（精确到秒的美东墙钟）与 `captured_on`
（PDT 日期），都经两个模块级注入缝取：`evs._now_et` / `evs._today_pdt`。任何「跑两次比字节 / 比行」或「结果不许随跑测试的
时钟变」的测试都必须把它们钉住，否则撞秒边界（或 PDT 午夜）就偶发红——v0.45.642 那一轮整套里就红过一次
（两次 scan 之间跨了一秒，账本里**恰好一个字节**不同，偏移 5963，落在 `captured_at` 的秒位）。

此前这个钉法在三个测试文件里各手写一份（含同一个 `datetime(2026, 9, 3, 18, 0, …)` 字面量），`scan` 哪天再多读一只钟，
漏改的那个文件就是下一颗偶发。现在只有这里一份；`tests/test_scan_clock.py` 守「`scan` 的每一次读钟都经这两个缝」
与「三个测试文件不再自己钉」。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
#: 测试里统一的「信号日」：2026-09-03 周四
SCAN_AS_OF = "2026-09-03"
#: 信号日收盘后（生产扫描的时刻）；记录的 `session_live` 为 False
POST_CLOSE = datetime(2026, 9, 3, 18, 0, tzinfo=ET)
#: 同一天盘中（有人手动跑日报）；`session_live` 为 True
INTRADAY = datetime(2026, 9, 3, 11, 0, tzinfo=ET)


def pin_scan_clock(monkeypatch, evs, as_of: str = SCAN_AS_OF, now: datetime = POST_CLOSE) -> None:
    """把 `evs._today_pdt`（记录日）与 `evs._now_et`（记录时刻）钉死。`evs` 是 `earnings_vol_signal` 模块。"""
    monkeypatch.setattr(evs, "_today_pdt", lambda: as_of)
    monkeypatch.setattr(evs, "_now_et", lambda: now)


def tick_scan_clock(monkeypatch, evs, start: datetime = POST_CLOSE, step_seconds: int = 1) -> None:
    """让 `evs._now_et` 每被读一次就走 `step_seconds` 秒（第一次读到 `start`）——把「两次 scan 之间跨了一秒」
    做成确定性场景，不靠真实时钟碰运气。"""
    n = {"i": -1}

    def tick() -> datetime:
        n["i"] += 1
        return start + timedelta(seconds=n["i"] * step_seconds)

    monkeypatch.setattr(evs, "_now_et", tick)
