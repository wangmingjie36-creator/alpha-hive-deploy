"""整套测试开跑时提示「同时还有别的大测试会话在跑」（v0.45.590）。**只提示，从不阻塞、从不报红。**

**为什么。** 整套是单进程、纯 CPU 的。同一套 ~1.1 万条，机器安静时 500–650 秒，同时有别的整套 / clockshift
审计在跑时 1000–1150 秒（本机 M4 只有 4 个性能核，见 auto-memory `alpha-hive-test-suite-speed`）。慢本身不是 bug，
但「为什么这次这么慢」要有人先排除这一条——所以让它在报告头里自己说出来，而不是靠事后翻日志。

**为什么只提示不阻塞。** 排队锁会让一个会话无声地卡住，与「失败要可见」冲突；测试里还有靠真睡、靠超时的用例，
排队等待会和它们互相干扰。提示的代价是零，且不改变任何测试的行为。

**怎么知道别的会话在跑。** 复用写沙箱的会话登记（`_write_sandbox.sessions`：每个会话的 pid / 起止），再给登记补两个字段
——选中的条数 `items`（收集结束才知道）和是否在 clockshift 拨钟下 `shifted`。只数**条数 ≥ `BIG_RUN_ITEMS`** 的会话：
别人跑单个文件不该让你收到提示。登记不可用（非 macOS / 沙箱没套上）⇒ 如实说「并发检测不可用」，不静默。

**登记看不见的那一半：系统 load（v0.45.668）。** 上面只数「登记过的 pytest 会话」。10-27 一个会话为复现偶发失败起了
20 个 `yes` 当负载、清理无声失败，空转 40 分钟，load 124，另一会话的整套被饿到约 8% CPU、跑了 21 分钟以上；
当时开跑提示一个字没打，因为那 20 个进程不是 pytest。`load_lines` 补这一半：1 分钟 load ≥ 核数 ⇒ 打一行，
指给人去看谁在占 CPU。阈值取「≥ 核数」有实测依据：590 那轮 load≈11（10 核）整套跑了 1339 秒（安静时 500–650 秒）。
同样只提示不阻塞；读不到 load ⇒ 说「无法读取」而不是沉默。

纯函数（`lines` / `load_lines`）不碰文件系统与系统调用，测试直接喂登记记录 / load 数。接线只在
`conftest.pytest_report_collectionfinish`（计算必须放在这个钩子里，不能拆成「收集结束钩子存 stash、报告钩子再读」，
见 conftest 该钩子的注释）。真实读取 load 的包装 `system_load_lines` 刻意与 `session_lines` 分开：合在一起的话，
机器忙时那些断言 `session_lines(...) == []` 的测试会无故变红。
"""
from __future__ import annotations

import os

import _write_sandbox

#: 选中条数达到它才算「整套」：本会话达到才提示，别的会话达到才算「大会话」。常量不分环境——整套约 1.1 万条，
#: 日常跑的单个文件 / 单个类都在几十到几百条，两头隔得很开，取中间一个整数即可。
BIG_RUN_ITEMS = 2000

#: 1 分钟 load 达到「核数 × 它」就提示。1.0 = 可运行进程数已不少于核数：单进程整套拿不到一整个核。
LOAD_WARN_PER_CORE = 1.0

#: 提示里指给人的那条命令——**不在测试进程里自己跑**：本会话的写沙箱里 exec 不了 setuid 程序（`/bin/ps`）。
WHO_IS_BUSY = "ps -Ao pid,ppid,etime,pcpu,comm -r | head"


def _is_shifted() -> bool:
    """本进程是不是在 clockshift 拨钟下跑（拨钟审计的子进程）。判据与 `tools/clockshift/sitecustomize.py` 一致。"""
    days = (os.environ.get("CLOCK_SHIFT_DAYS") or "0").strip()
    return days not in ("0", "0.0", "") or os.environ.get("CLOCK_SHIFT_FORCE") == "1"


def lines(my_items: int, others: list[dict] | None, *, unavailable: str | None = None,
          threshold: int | None = None) -> list[str]:
    """要在报告里打的提示行（空列表 = 不用说）。

    `others`：`_write_sandbox.sessions()` 的记录（带 `alive` / `pid` / `items` / `shifted`）；
    `unavailable`：登记用不了的原因（有值 ⇒ 无法判断，说出来）。"""
    t = BIG_RUN_ITEMS if threshold is None else threshold
    if my_items < t:
        return []
    if unavailable:
        return [f"concurrency: 无法检测并发的测试会话（{unavailable}）——整套与别的整套 / clockshift 审计同时跑会明显变慢，"
                "跑前自查：pgrep -fl pytest"]
    big = [o for o in (others or []) if o.get("alive") and (o.get("items") or 0) >= t]
    if not big:
        return []
    desc = "、".join(f"pid {o['pid']}（{o['items']} 条{'，clockshift 拨钟' if o.get('shifted') else ''}）" for o in big)
    return [f"concurrency: ⚠️ 同时还有 {len(big)} 个大测试会话在跑：{desc}——"
            "单进程整套在并发时会明显变慢（本机实测可达约两倍），不影响结果；要快就错开跑"]


def session_lines(sb: dict | None, my_items: int) -> list[str]:
    """本会话收集结束后调用：把本会话的条数登记出去（别的会话据此看见我），再看别人，返回要打的提示行。

    `sb`：`_write_sandbox.apply()` 的状态。任何一步失败都只会变成一行「无法检测」，绝不抛（提示不能弄红测试）。"""
    sb = sb or {}
    try:
        if sb.get("registry"):
            _write_sandbox.mark(sb, items=my_items, shifted=_is_shifted())
            return lines(my_items, _write_sandbox.sessions(sb))
        if sb.get("inherited"):
            why = "继承外层会话的写沙箱，内层不登记"
        else:
            why = sb.get("registry_error") or f"写沙箱未生效：{sb.get('reason') or '未尝试'}"
    except OSError as e:
        why = f"读会话登记失败：{e}"
    return lines(my_items, None, unavailable=why)


def load_lines(my_items: int, load1: float | None, ncpu: int | None, *, unavailable: str | None = None,
               threshold: int | None = None) -> list[str]:
    """系统 load 过高时要打的提示行（空列表 = 不用说）。

    `load1`：1 分钟 load average；`ncpu`：逻辑核数；`unavailable`：读不到它们的原因（有值 ⇒ 无法判断，说出来）。
    与 `lines` 同一道闸：条数没到 `BIG_RUN_ITEMS` 的小会话不吵。"""
    t = BIG_RUN_ITEMS if threshold is None else threshold
    if my_items < t:
        return []
    if unavailable is None and (load1 is None or not ncpu or ncpu < 1):
        unavailable = "load 或核数读到的值不可用"
    if unavailable:
        return [f"concurrency: 无法读取系统 load（{unavailable}）——整套慢得不对劲时先 uptime，再 {WHO_IS_BUSY}"]
    if load1 < LOAD_WARN_PER_CORE * ncpu:
        return []
    return [f"concurrency: ⚠️ 系统 1 分钟 load {load1:.0f} / {ncpu} 核——机器已超订，单进程整套会明显变慢、耗时不可比"
            f"（不影响结果）；谁在占 CPU：{WHO_IS_BUSY}（ppid=1 且 etime 很长的满载进程多半是泄漏的后台负载）"]


def system_load_lines(my_items: int) -> list[str]:
    """读真实的 load 与核数，交给 `load_lines`。任何读取失败都只会变成一行「无法读取」，绝不抛。"""
    try:
        load1 = os.getloadavg()[0]
        ncpu = os.cpu_count()
    except (OSError, AttributeError) as e:
        return load_lines(my_items, None, None, unavailable=f"{type(e).__name__}: {e}")
    return load_lines(my_items, load1, ncpu)
