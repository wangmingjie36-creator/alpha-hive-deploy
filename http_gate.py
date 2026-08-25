#!/usr/bin/env python3
"""
全局出站 HTTPS 闸门（v0.43.27）
================================
本机 OpenSSL 是 **1.1.1q（2022，已 EOL）**，并发 HTTPS 会大面积抛
`SSLEOFError: EOF occurred in violation of protocol`。`cboe_options` 早就记录过
实测数据：4 并发拉 CBOE 每个挂 50~70s 甚至直接 EOF，而顺序拉仅 8~11s。

问题是那把信号量**只锁了 CBOE**。yfinance / Finnhub / AlphaVantage 各走各的，
在 3~6 个工作线程里同时发 HTTPS，照样把 SSL 栈压垮。

2026-08-24 的事故正是如此：全天 **96 次 SSL EOF**，四个源轮流失败，
每只标的都要走完"CBOE 重试 3 次 → yfinance 限流 → Finnhub EOF → AV EOF"
才降级，30 只标的拖过 1800s，**Step 2 蜂群分析超时被杀**，当天零产出。

⚠️ 值得记一笔：v0.43.26 接通 Finnhub/AV 之前，这两个源因拿不到 key 直接
返回 None、**一个请求都不发**。接通后它们成了两个不受保护的并发调用方——
那个"修复"很可能是 8/24 EOF 风暴的助推之一。**加源之前先确认闸门覆盖它。**

用法
----
    from http_gate import https_gate
    with https_gate():
        resp = urllib.request.urlopen(req, timeout=15)

或直接用封装：

    from http_gate import urlopen_gated
    raw = urlopen_gated(req, timeout=15).read()

不要在持锁期间做解析、落盘等 CPU/IO 工作——闸门是全进程唯一的，
握着它做无关的事会把所有数据源一起堵死。
"""

from __future__ import annotations

import threading
import urllib.request
from contextlib import contextmanager

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.http_gate")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.http_gate")

# 全进程唯一。限 1 是实测结论，不是保守估计——见模块 docstring 的 4 并发数据。
_GATE = threading.Semaphore(1)

# 默认等待上限：拿不到闸门就放弃，好过让调用方无限期堵住工作线程。
# 单次 CBOE 全链约 8~11s，留足串行排队的余量。
DEFAULT_ACQUIRE_TIMEOUT = 120.0


class GateBusy(RuntimeError):
    """等待闸门超时。调用方应视同该数据源不可用，走降级链下一环。"""


@contextmanager
def https_gate(timeout: float = DEFAULT_ACQUIRE_TIMEOUT):
    """串行化一次出站 HTTPS 请求。拿不到闸门抛 GateBusy。"""
    if not _GATE.acquire(timeout=timeout):
        raise GateBusy(f"等待 HTTPS 闸门超过 {timeout}s")
    try:
        yield
    finally:
        _GATE.release()


def urlopen_gated(req, timeout: float = 15.0, gate_timeout: float = DEFAULT_ACQUIRE_TIMEOUT):
    """`urllib.request.urlopen` 的串行化版本。

    注意返回的是已读完的 bytes 而非 response 对象——持锁期间必须把数据读完，
    否则连接仍开着、闸门却已释放，等于没串行化。
    """
    with https_gate(gate_timeout):
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
