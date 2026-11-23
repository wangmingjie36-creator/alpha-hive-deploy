#!/usr/bin/env python3
"""接货型卖 put 账本的盘中开仓入口（v0.45.810，用户 2026-11-23 定）：launchd 每个工作日在美东 15:30 触发 →
`sell_put_wheel.run_for_date(今天, phase="open")`：结算 + 按那一刻 CBOE 报价（延迟约 15 分钟）的 bid 开仓。

为什么在盘中
------------
此前开仓在收盘后的扫描里、按收盘那份报价「成交」——收盘后已经成交不了，真下单最早是下一个交易日，跳空时
（2026-11-20 → 11-23 的 NVDA 110P：5.75 → 0.60）账本记的是拿不到的价。改到盘中：决定与成交用同一份报价。
为什么是 15:30：用户选的时刻；本机 24~25 个交易日的盘中快照实测，一天里各时段的 iv30 与收盘差不到 ±0.2 个波动率点、
没有稳定更好的时段，所以时刻本身不是在「挑价」。

失败要看得见
------------
· 窗口外（机器睡眠，launchd 唤醒后补跑，已过 15:59 ET）⇒ 不开仓、记 `outside_window`；抛了 ⇒ 记 `error`。
· 根本没跑（机器没开 / 任务没装）⇒ 没有记录。三种都由当晚收盘后扫描的 mark 阶段核对出来 ⇒ `open_missing` ⇒ P2。
· 跑成了但链全取不到 / RV 全算不出 ⇒ 这一轮的计数存进 meta，mark 阶段并进扫描计数，照原有 P2 报。
· 休市日直接退出、不记（当晚也没有扫描来核对）。提前收盘的半天：15:30 已收盘 ⇒ 报价不是盘中的，每只记
  `quote_not_intraday`、不开仓（这是规则，不报警）。

跑哪份代码：`~/alpha-hive-prod`（生产克隆，由每个扫描日的 production_sync 快进；launchd 无 TCC 限制）。数据根缺省
`$HOME/alpha-hive-data`，plist 不设 `ALPHA_HIVE_HOME`。plist 带与 LLM 影子任务同一组代理变量（RV 走 yfinance，
curl_cffi 不读系统代理；守卫核对两边一致）。

用法
----
  sell_put_open.py --now          现在跑一次（窗口外只记一笔 outside_window、不开仓）
  sell_put_open.py --print-plist  打印 launchd plist（装法见下）

装 / 卸（plist 按生成时本机时区把美东 15:30 换成本地钟点；换时区后要重新生成——换错了会被窗口闸拦下并报 P2）：
  /usr/local/bin/python3 ~/alpha-hive-prod/sell_put_open.py --print-plist > ~/Library/LaunchAgents/com.alpha.hive.sellput.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.alpha.hive.sellput.plist
  launchctl bootout   gui/$(id -u)/com.alpha.hive.sellput
"""
from __future__ import annotations

import argparse
import plistlib
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo

LABEL = "com.alpha.hive.sellput"
ET = ZoneInfo("America/New_York")
PROXY_URL = "http://127.0.0.1:7897"
NO_PROXY = "localhost,127.0.0.1,::1,.local"
#: 与 `llm_shadow_daily.PROXY_ENV` 同一组（守卫逐项核对）
PROXY_ENV = {"HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL, "http_proxy": PROXY_URL, "https_proxy": PROXY_URL,
             "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY}
EXIT_OK, EXIT_ERROR, EXIT_CLOSED, EXIT_OUTSIDE = 0, 1, 2, 3


def local_fire_time(on: Optional[date] = None, tz: Optional[ZoneInfo] = None) -> tuple:
    """美东 OPEN_AT_ET 在本机时区是几点几分（plist 的 StartCalendarInterval 按本地时间）。"""
    import sell_put_wheel as W
    d = on or date.today()
    et = datetime(d.year, d.month, d.day, *W.OPEN_AT_ET, tzinfo=ET)
    loc = et.astimezone(tz) if tz else et.astimezone()
    return loc.hour, loc.minute


def render_plist(*, python: str, script: str, log_dir: str, hour: int, minute: int) -> bytes:
    doc = {
        "Label": LABEL,
        "ProgramArguments": [python, script, "--now"],
        "StartCalendarInterval": [{"Weekday": wd, "Hour": hour, "Minute": minute} for wd in range(1, 6)],
        "RunAtLoad": False,
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", **PROXY_ENV},
        "StandardOutPath": str(Path(log_dir) / "sell_put_open.out.log"),
        "StandardErrorPath": str(Path(log_dir) / "sell_put_open.err.log"),
    }
    return plistlib.dumps(doc)


def _trading_day(d: date) -> bool:
    import sell_put_wheel as W
    return W.expected_session(d.isoformat()) == d.isoformat()


def run_now(now_et: Optional[datetime] = None, *, runner: Optional[Callable] = None,
            trading_day: Optional[Callable[[date], bool]] = None) -> int:
    import sell_put_wheel as W
    now = now_et or datetime.now(ET)
    as_of = now.date().isoformat()
    if not (trading_day or _trading_day)(now.date()):
        print(f"sell_put_open: {as_of} 休市，不跑")
        return EXIT_CLOSED
    if not W.open_window_ok(now):
        W.record_open_outcome(as_of, "outside_window", f"{now:%H:%M} ET 不在盘中开仓窗口内（机器睡眠后补触发？）")
        print(f"sell_put_open: {now:%H:%M} ET 不在窗口内，只记 outside_window")
        return EXIT_OUTSIDE
    try:
        r = (runner or W.run_for_date)(as_of, phase="open")
    except Exception as exc:  # noqa: BLE001 - 落一笔再退出：当晚 mark 阶段据此报 P2
        W.record_open_outcome(as_of, "error", f"{type(exc).__name__}: {exc}")
        print(f"sell_put_open: {as_of} 抛了 {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    ev = [f"{e['ticker']} {e['event']} {e.get('strike')} {e.get('expiry')} @ {e.get('premium')}" for e in r.get("events") or []]
    sk = [f"{s['ticker']} {s['leg']}:{s['reason']}" for s in r.get("skipped") or []]
    print(f"sell_put_open: {as_of} {now:%H:%M} ET | 成交 {ev or '无'} | 没开 {sk or '无'} | 现金 {r.get('cash')}")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="接货账本盘中开仓入口（详见模块 docstring）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--now", action="store_true", help="现在跑一次盘中开仓")
    g.add_argument("--print-plist", action="store_true", help="打印 launchd plist 后退出")
    args = ap.parse_args(argv)
    if args.print_plist:
        h, m = local_fire_time()
        sys.stdout.write(render_plist(python="/usr/local/bin/python3", script=str(Path(__file__).resolve()),
                                      log_dir=str(Path.home() / ".claude" / "logs"), hour=h, minute=m).decode())
        return EXIT_OK
    return run_now()


if __name__ == "__main__":
    sys.exit(main())
