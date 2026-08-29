#!/usr/bin/env python3
"""
全局出站 HTTPS 闸门（v0.43.27 引入；归因于 v0.45.75 更正）
==========================================================
把本进程所有出站 HTTPS 请求串行化——全局信号量限 1，同一时刻只允许一个在飞。

为什么保留这道闸
----------------
**不给对端限流器加压。** 本项目撞过的、有实测的那个问题是限流，不是 TLS：
yfinance 的 `Too Many Requests` 逐日翻倍（**8/25=364 → 8/26=487 → 8/27=687**），
8/27 那天 `rv_30d` / `iv_rank` / `catalysts` **各 0/30**。串行化消灭不了限流，
但它把本进程的瞬时并发压到 1，是"不主动施压"这条纪律的执行点，
与 `yf_gate` 的「一次 429 就停、不重试」同向。

代价也确实小：闸门只锁网络往返，不锁解析与落盘（见下方用法约束）。
所以这道闸留着——**但它的理由是限流，不是下面那条已经撤回的。**

⚠️ 已撤回的理由：并发压垮 TLS 栈（2026-08-25 证伪）
---------------------------------------------------
本 docstring 此前断言「本机 OpenSSL 1.1.1q（2022，已 EOL）并发 HTTPS 会大面积
抛 SSLEOFError」，并引 `cboe_options` 的「4 并发拉 CBOE 每个挂 50~70s，顺序拉
仅 8~11s」当实测证据。**这个归因是错的，别再复述、也别拿它当新代码的理由。**

2026-08-25 重测（12 只 × 并发 1/4/8/12，另 3 轮 × 8 只 并发 4）：

    解释器                        24 次请求失败    耗时均值
    3.11.1  / OpenSSL 1.1.1q            0           52s
    3.12.13 / OpenSSL 3.6.1             0           53s

两个 TLS 版本**无可测差异**，EOF 一次都没复现出来；且串行（80.5s）反而比
并发（62s）**更慢**，与原结论方向相反。原先那组数字是**单次取样、顺序执行**的，
第二次很可能吃到了 CDN 热缓存——把缓存效应读成了闸门效果。

2026-08-24 全天 **96 次 SSL EOF 是真的**，但**根因至今未确定**。最强的反证是
它的分布：EOF 同时打在 Slack(18)、reddit(13)、SEC EDGAR(8)、polymarket(4)、
CBOE(20)、Finnhub(11)、AlphaVantage(13)——**七个互不相干的域名同时挂**，
指向主机/网络层的瞬时状况，而不是某个 TLS 库的缺陷。

**EOF 再犯时怎么查**：先抓现场——当时的并发数、网络状态、有没有 VPN/代理、
机器负载、是所有域名一起挂还是只有一个——**再**谈归因。别照着版本号往回推。
（一个顺带的旁证：`cboe_options` 把同一场事故记成「LibreSSL 2.8.3 扛不住并发」，
而 LibreSSL 2.8.3 是系统 `/usr/bin/python3` 3.9.6 的 TLS 栈；生产扫描跑的是
`/usr/local/bin/python3` 3.11.1 / OpenSSL 1.1.1q。两处版本号互相矛盾，
却都被当成了实测证据——版本号是最容易顺手拿来的解释，也最不用付代价。）

仍然成立的那条教训
------------------
v0.43.27 之前这把信号量**只锁了 CBOE**，yfinance / Finnhub / AlphaVantage
各走各的。而 v0.43.26 接通 Finnhub/AV 之前，这两个源因拿不到 key 直接返回
None、**一个请求都不发**；接通后它们成了两个不受任何闸门约束的调用方。
不论 8/24 的根因是什么，这个结构性事实与它独立地成立：

    **加源之前先确认闸门覆盖它。**

同源续集两条：`yf_gate`（yfinance 40 个调用点只有 6 个接了线，故改为 patch
库本身，接线一次）、v0.45.73（crewai 的 install 埋点发生在闸门自己被 import
之前——该集成已于 v0.45.74 整体移除，本仓现在 grep 不到它，查经过看 CHANGELOG）。

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

# 全进程唯一。限 1 = 「本进程一次只发一个出站请求」，理由是不给对端限流器加压
# （见 docstring），**不是**"并发会压垮 TLS 栈"——那条归因 2026-08-25 已证伪。
_GATE = threading.Semaphore(1)

# 默认等待上限：拿不到闸门就放弃，好过让调用方无限期堵住工作线程。
# 120s 是给多个调用方排队留的余量。⚠️ 单次往返耗时**没有当前实测**——
# docstring 里那组 8~11s 属已撤回的单次取样数据，要调这个值先自己测一遍。
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
