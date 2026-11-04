#!/usr/bin/env python3
"""
标的开跑前等 yfinance 冷却结束（v0.45.716）
==========================================

「冷却期内先别开工，等它结束」——给**还没开始取数**、晚几分钟无妨的工作单元用。

为什么需要（2026-11-03 取证）
----------------------------
扫描第 282 次请求撞上**一次** 429（CRCL `option_chain`），`yf_gate` 进入 180s 冷却，期间所有 yfinance
调用秒回 `YFRateLimited`。冷却本身没错（429 = 现在就停），错在冷却期间**仍在开新标的**：
二十几只标的的 HV / 异常流在 90 秒内全部失败（iv_rank 10/30、unusual_flow 9/30 ⇒ 判数据源故障日）；
失败是秒回的，标的处理速度从 ≈1.2 只/分钟冲到十几只/分钟，又把 Massive 新闻的 5 次/分钟名额挤爆
（9 只降级 Yahoo 新闻）。11-02 同样一次 429，落在 yfinance 步骤之后，零损失——损失取决于 429 落在哪。

设计
----
- **不重试、不发请求**：只在开工前睡到冷却结束，不给 Yahoo 加任何压力。所以放在 `yf_gate` 外面——
  闸门只管「429 = 现在就停」，那个模块结构上不含循环 / 睡眠（守卫 `test_yf_rate_limit_handling`）。
- **预算按冷却段计，不按线程计**：4 个并行标的同时等同一段冷却，墙钟只过去一次，只扣一次。
  - 单段剩余 > `per_wait_max` ⇒ 不等（冷却被拉长了 / 参数异常），照旧失败；
  - 累计已扣 + 本段剩余 > `total_s` ⇒ 不等（429 风暴日：等也等不完，别把整轮扫描拖进超时）。
- **谁会红**：等过的记 `cooldown_waits` / `cooldown_wait_s`，不等的记 `cooldown_wait_refused`，
  都在 `yf_gate.stats()` 里，经 `scan_timing` 落盘；没等的那些标的照旧由覆盖率闸判降级。
- `sleep` / `now` 可注入，测试用假时钟；生产缺省 `time.sleep` / `time.monotonic`（与闸门冷却同一只钟）。
"""

from __future__ import annotations

import threading
import time

import yf_gate

try:
    from hive_logger import get_logger
    _log = get_logger("yf_cooldown_wait")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.yf_cooldown_wait")


class CooldownWaitBudget:
    """一轮工作（如一次扫描）共用的冷却等待预算。线程安全。"""

    def __init__(self, total_s: float, per_wait_max: float = yf_gate.RATE_LIMIT_COOLDOWN + 5.0,
                 *, sleep=None, now=None):
        self.total_s = float(total_s)
        self.per_wait_max = float(per_wait_max)
        self._sleep = sleep or time.sleep
        self._now = now or time.monotonic
        self._mu = threading.Lock()
        self._charged: dict = {}      # 冷却段（= 那一刻的冷却截止时刻）→ 已扣秒数
        self.spent_s = 0.0

    def wait(self, label: str = "") -> bool:
        """开工前调。True = 现在不在冷却（本来就不在，或等完了）；False = 没等（预算 / 单次上限），调用方照旧开工、照旧失败。

        等完之后若又被别的线程拉进新一段冷却，按同样的规则再判一次（每段只扣一次、预算有限 ⇒ 不会死等）。"""
        while True:
            until = yf_gate.cooldown_deadline()
            remaining = until - self._now()
            if remaining <= 0:
                return True
            with self._mu:
                first = until not in self._charged
                refuse = first and (remaining > self.per_wait_max or self.spent_s + remaining > self.total_s)
                if first and not refuse:
                    self._charged[until] = remaining
                    self.spent_s += remaining
                spent = self.spent_s
            if refuse:
                yf_gate.note_cooldown_wait(refused=True)
                _log.warning("yfinance 冷却还剩 %.0fs，不等（%s；本轮已等 %.0fs / 预算 %.0fs、单次上限 %.0fs）——照旧开工，"
                             "期间 yfinance 取数会失败", remaining, label or "-", spent, self.total_s, self.per_wait_max)
                return False
            if first:
                yf_gate.note_cooldown_wait(waited_s=remaining)
                _log.warning("yfinance 冷却中，%s 等 %.0fs 再开工（本轮累计 %.0fs / 预算 %.0fs）",
                             label or "-", remaining, spent, self.total_s)
            self._sleep(remaining)
