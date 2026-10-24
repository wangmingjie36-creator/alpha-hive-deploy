#!/usr/bin/env python3
"""LLM 影子 A/B 的每日自动入口（v0.45.592）：launchd 每个工作日 14:45 触发 → 等生产扫描写完 → 跑一次 `llm_shadow_scan.py --run`。

为什么要等
----------
规则臂 = 生产当天自己的结果，影子必须**紧随**生产扫描之后跑（价格静止、`cache/` 已被生产更新，两边输入一致；
2026-10-20 的 control 实测：同日基线分差 0.14，隔夜再跑是 0.43）。生产 14:00 开跑、通常 14:35~14:40 写完，
时长不定，所以这里不按钟点硬跑，而是轮询「生产今天写完了没有」：

  · 生产库里今天有 predictions 行；
  · `.swarm_results_<今天>.json` 存在、已经 5 分钟没再改；
  · 没有 `alpha_hive_daily_report.py` / 编排器进程在跑。

三条同时满足才开跑。等不到（节假日生产不扫、机器没开、生产崩了）最多等 3 小时，然后**跳过**、不花钱，
退出码 4，由 `llm_shadow_report` 的「缺失日」告警（生产有扫描而这一代没有 LLM 记录的日子）把缺口暴露出来。
生产写完后超过 2 小时才轮到我们（比如机器睡过头）也跳过：输入已不再与生产同口径。

花钱的闸（这里是唯一替用户设 `ALPHA_HIVE_LLM_SHADOW_SPEND=1` 的地方）
------------------------------------------------------------------
· 用户 2026-10-22 明确要求「换 Sonnet 5.5、30 只全跑、接成每天自动跑」，预计 ≈ $1.9/天（估算，待验证）、每日硬上限 $3
  （`llm_shadow_scan.DAILY_CAP_USD`，进程内预检 + 跨进程「一天只许付费跑一次」）⇒ 月度上限 ≈ 22 × $3 = $66。
· **关停开关**：存在 `<影子根>/DISABLED` 文件即不跑、不花钱（`touch ~/alpha-hive-llm-shadow/DISABLED`）；
  彻底停用：`launchctl bootout gui/$(id -u)/com.alpha.hive.llmshadow`。
· plist 里**不**写 `ALPHA_HIVE_LLM_SHADOW_SPEND`：只有本脚本在起子进程时才给，别的东西（比如手滑的 `--run`）拿不到。

跑哪份代码：`~/alpha-hive-prod`（生产克隆，不在 Desktop 里，launchd 无 TCC 限制；它由每个扫描日的 production_sync 快进，
代码随 main 自动更新）。数据根缺省 `$HOME/alpha-hive-data`（`hive_logger.resolve_data_root`），所以 plist 不设
`ALPHA_HIVE_HOME`。API Key 走 `~/.anthropic_api_key`（0600；launchd 不读 shell 环境）。
plist 写上代理环境变量（v0.45.623，`PROXY_ENV`；627 补小写那套）：curl_cffi（yfinance）这类不读 macOS 系统代理的客户端也走 Clash。

用法
----
  llm_shadow_daily.py                 等生产 → 跑（launchd 用这个）
  llm_shadow_daily.py --print-plist   打印 launchd plist（装法见下）
  llm_shadow_daily.py --once          不等待：现在的状态能不能跑？只打印判定，不花钱

装 / 卸：
  /usr/local/bin/python3 ~/alpha-hive-prod/llm_shadow_daily.py --print-plist > ~/Library/LaunchAgents/com.alpha.hive.llmshadow.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.alpha.hive.llmshadow.plist
  launchctl bootout   gui/$(id -u)/com.alpha.hive.llmshadow
plist 里 `RunAtLoad=false`：装上不会立刻花钱。

开跑前探活网络（v0.45.633）：生产写完后不立刻开跑，先对 3 个数据源做 HTTPS 握手探测（CBOE / Yahoo / Finnhub，
≥2 个通才算通；任何 HTTP 状态码都算通——要测的是握手不是授权）。10-23 全站 TLS EOF（`_ssl.c:992`）时，扫描白耗 18 分钟预取、
30/30 只取不到现价被输入闸拦下、丢了一天；现在网络不通就每分钟重探，通了再跑，直到「生产写完已超 2 小时」或「等了 3 小时」才跳过
（退出码 4，原因写明是网络）。探活走的是和扫描同一条路（urllib：环境代理 / macOS 系统代理）。⚠️ 它只管开跑那一刻；
扫描中途才断仍会被扫描的输入闸拦下，那种日子零花费、状态文件不再锁死当天（见 `llm_shadow_scan.day_lock`），但不会自动重跑。

退出码：0 跑完且健康；2 用法 / 子进程拒绝；3 扫描异常；4 跳过（关停开关 / 等不到生产 / 太晚）；5 跑完但 LLM 臂不健康。
（2 / 3 / 5 原样透传 `llm_shadow_scan` 的退出码。）
"""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

LABEL = "com.alpha.hive.llmshadow"
TRIGGER_HOUR, TRIGGER_MINUTE = 14, 45          # 本机时区（PDT）；生产 14:00 开跑
WEEKDAYS = (1, 2, 3, 4, 5)                     # launchd：1=周一 … 5=周五
POLL_SEC = 60
WAIT_MAX_SEC = 3 * 3600                        # 等生产最多 3 小时
STABLE_SEC = 300                               # swarm_results 文件至少这么久没再改，才算生产写完
MAX_LAG_SEC = 2 * 3600                         # 生产写完后超过这么久才轮到我们 ⇒ 输入不再同口径，跳过
DISABLE_FILENAME = "DISABLED"
SPEND_ENV = "ALPHA_HIVE_LLM_SHADOW_SPEND"      # 与 llm_shadow_scan.SPEND_ENV 同值（测试钉住）
PROD_CLONE = "alpha-hive-prod"
EXIT_SKIPPED = 4
#: plist 里写上的代理（v0.45.623，用户 10-22 定）。与编排器 `PROXY_URL` 的缺省同值（守卫核对）。Python 的
#: urllib / requests / httpx 本来就经 macOS 系统代理（`_scproxy`）走它；写成环境变量后，不读系统代理的客户端
#: （curl_cffi ⇒ yfinance）也走它——此前它们在 launchd 里直连、易被限流（auto-memory environment-facts）。
#: ⚠️ 与编排器不同，这里**不探活**：Clash 没开时这类请求连不上（Anthropic 那头此时本来也会被按地区拒绝）。
PROXY_URL = "http://127.0.0.1:7897"
NO_PROXY = "localhost,127.0.0.1,::1,.local"
#: 写进 plist 的代理变量：与编排器探活通过后导出的那组逐项一致（名字与值都由守卫按编排器原文核对）。大小写两套都要：
#: libcurl（curl_cffi ⇒ yfinance）对 http:// 只读小写 `http_proxy`（httpoxy 防护），只写大写那套时 http 请求仍直连
#: （v0.45.627，623 二次检查 #7）
PROXY_ENV = {"HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL, "http_proxy": PROXY_URL, "https_proxy": PROXY_URL,
             "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY}

#: 开跑前的网络探活（v0.45.633）：三个扫描真正依赖的数据源，TLS 握手能通 ≥ NET_PROBE_MIN_OK 个才算网络可用。
NET_PROBE_URLS = ("https://cdn.cboe.com/", "https://query1.finance.yahoo.com/", "https://finnhub.io/")
NET_PROBE_TIMEOUT = 10
NET_PROBE_MIN_OK = 2

ACTION_RUN, ACTION_WAIT, ACTION_SKIP = "run", "wait", "skip"


# ==================== 纯函数（有单测） ====================

def decide(state: Dict, waited_sec: float, *, disabled: bool) -> Tuple[str, str]:
    """(动作, 原因)。`state`：{"rows": 今天生产 predictions 行数, "file_age": swarm_results 文件的年龄秒数或 None,
    "running": 生产扫描进程是否还在跑}。"""
    if disabled:
        return ACTION_SKIP, f"存在 {DISABLE_FILENAME} 文件（用户关停开关）"
    rows = int(state.get("rows") or 0)
    age = state.get("file_age")
    done = rows >= 1 and age is not None and not state.get("running") and age >= STABLE_SEC
    if done:
        if age > MAX_LAG_SEC:
            return ACTION_SKIP, (f"生产写完已 {age / 3600:.1f} 小时（> {MAX_LAG_SEC / 3600:.0f}h）——"
                                 f"输入不再与生产同口径，今天不跑")
        if state.get("net_ok") is False:
            if waited_sec >= WAIT_MAX_SEC:
                return ACTION_SKIP, f"等了 {waited_sec / 3600:.1f} 小时，网络探活一直不通：{state.get('net_detail')}"
            return ACTION_WAIT, f"生产已写完，但网络探活不通，稍后重探：{state.get('net_detail')}"
        return ACTION_RUN, f"生产今天已写完（{rows} 行，swarm_results 已 {age / 60:.0f} 分钟没再改）"
    if waited_sec >= WAIT_MAX_SEC:
        why = ("生产扫描进程仍在跑" if state.get("running") else
               "生产今天没有 predictions 行（节假日 / 没开机 / 生产崩了？）" if rows < 1 else
               "没有今天的 swarm_results 文件" if age is None else
               f"swarm_results 才 {age:.0f}s 没再改（< {STABLE_SEC}s）")
        return ACTION_SKIP, f"等了 {waited_sec / 3600:.1f} 小时仍不满足：{why}"
    return ACTION_WAIT, "生产还没写完"


def production_done(state: Dict) -> bool:
    """生产今天写完了没有（与 `decide` 里的判据同一份，只是不含网络）。"""
    age = state.get("file_age")
    return int(state.get("rows") or 0) >= 1 and age is not None and not state.get("running") and age >= STABLE_SEC


def probe_network(urls: Sequence[str] = NET_PROBE_URLS, *, timeout: float = NET_PROBE_TIMEOUT,
                  min_ok: int = NET_PROBE_MIN_OK, opener: Optional[Callable] = None) -> Tuple[bool, str]:
    """对几个数据源做 HTTPS 探测，返回 (通?, 明细)。**拿到任何 HTTP 响应（含 4xx / 5xx）都算通**——测的是 TLS 握手 / 连接，
    不是授权（Yahoo 常给 429）；超时 / 握手失败 / 连接被拒才算不通。`opener` 仅供测试注入。"""
    import urllib.error
    import urllib.request
    open_fn = opener or (lambda u, t: urllib.request.urlopen(urllib.request.Request(u, method="GET"), timeout=t))
    ok, bad = [], []
    for u in urls:
        host = u.split("//", 1)[-1].split("/", 1)[0]
        try:
            open_fn(u, timeout)
            ok.append(host)
        except urllib.error.HTTPError:
            ok.append(host)                                   # 有响应 = 握手通了
        except Exception as e:  # noqa: BLE001 —— URLError / ssl.SSLError / socket.timeout / OSError 都算不通，明细写进日志
            bad.append(f"{host}: {type(e).__name__}: {str(e)[:80]}")
    detail = f"通 {len(ok)}/{len(urls)}" + (f"；不通 {'; '.join(bad)}" if bad else "")
    return len(ok) >= min_ok, detail


def render_plist(*, python: str, script: str, log_dir: str) -> bytes:
    """launchd plist（XML）。`RunAtLoad=False`：装上 / 重启登录不会立刻花钱；不带 SPEND / 数据根环境变量。
    带代理变量（v0.45.623）：10-22 用户批准先手改了已装的 plist，这里同步写上，否则重新生成会把它们无声丢掉。"""
    doc = {
        "Label": LABEL,
        "ProgramArguments": [python, script],
        "StartCalendarInterval": [
            {"Weekday": d, "Hour": TRIGGER_HOUR, "Minute": TRIGGER_MINUTE} for d in WEEKDAYS],
        "RunAtLoad": False,
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", **PROXY_ENV},
        "WorkingDirectory": str(Path(script).parent),
        "StandardOutPath": str(Path(log_dir) / "llm_shadow_daily.out.log"),
        "StandardErrorPath": str(Path(log_dir) / "llm_shadow_daily.err.log"),
    }
    return plistlib.dumps(doc)


# ==================== 有副作用的部分 ====================

def production_state(*, prod_db: Path, home: Path, today: str, now: Optional[float] = None,
                     is_running: Optional[Callable[[], bool]] = None) -> Dict:
    """读生产此刻的状态（只读：生产库以 `mode=ro` 打开）。"""
    import sqlite3
    rows = 0
    if Path(prod_db).exists():
        con = sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True)
        try:
            rows = int(con.execute("SELECT COUNT(*) FROM predictions WHERE date=?", (today,)).fetchone()[0])
        finally:
            con.close()
    f = Path(home) / f".swarm_results_{today}.json"
    age = (time.time() if now is None else now) - f.stat().st_mtime if f.exists() else None
    running = (is_running or _scan_running)()
    return {"rows": rows, "file_age": age, "running": running}


def _scan_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "alpha_hive_daily_report.py|alpha-hive-orchestrator"],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | llm_shadow_daily | {msg}", flush=True)


def run_daily(*, once: bool = False, environ: Optional[Dict[str, str]] = None,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic,
              probe: Optional[Callable[[], Dict]] = None,
              spawn: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
              disabled_probe: Optional[Callable[[], bool]] = None,
              net_probe: Optional[Callable[[], Tuple[bool, str]]] = None) -> int:
    """等生产写完 → 跑一次 `--run`。`once=True`：只判定一次、不等、不跑（给人看现状）。"""
    env = dict(os.environ if environ is None else environ)
    import hive_logger
    from llm_shadow_scan import default_shadow_root
    prod_root = Path(hive_logger.resolve_data_root(env))
    shadow_root = default_shadow_root(prod_root)
    today = hive_logger.pdt_today()
    if probe is None:
        def probe() -> Dict:
            return production_state(prod_db=prod_root / "pheromone.db", home=prod_root, today=today)
    if disabled_probe is None:
        def disabled_probe() -> bool:
            return (shadow_root / DISABLE_FILENAME).exists()

    net = net_probe or probe_network
    t0 = clock()
    while True:
        state = probe()
        if production_done(state):                    # 生产没写完就不探网络（不必每分钟打外网）
            state["net_ok"], state["net_detail"] = net()
        action, why = decide(state, clock() - t0, disabled=disabled_probe())
        if once:
            _log(f"[--once] {today}: {action} — {why} | 状态 {state}")
            return 0 if action != ACTION_SKIP else EXIT_SKIPPED
        if action == ACTION_SKIP:
            _log(f"{today}: 跳过，不花钱 — {why}")
            return EXIT_SKIPPED
        if action == ACTION_RUN:
            break
        if state.get("net_ok") is False:
            _log(f"{today}: {why}")                  # 等网络的每一轮都留痕：事后能看出断了多久
        sleep(POLL_SEC)

    _log(f"{today}: 开跑 — {why}")
    child_env = dict(env)
    child_env[SPEND_ENV] = "1"          # 全仓唯一替用户设这个变量的地方（用户 2026-10-22 批准每日自动跑）
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "llm_shadow_scan.py"), "--run"]
    proc = spawn(cmd, env=child_env, cwd=str(Path(__file__).resolve().parent))
    rc = int(proc.returncode)
    _log(f"{today}: llm_shadow_scan 退出码 {rc}")
    return rc


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LLM 影子 A/B 每日自动入口（详见模块 docstring）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--print-plist", action="store_true", help="打印 launchd plist 后退出")
    g.add_argument("--once", action="store_true", help="只判定一次现在能不能跑，不等、不跑、不花钱")
    args = ap.parse_args(argv)
    if args.print_plist:
        home = Path.home()
        sys.stdout.write(render_plist(
            python="/usr/local/bin/python3",
            script=str(home / PROD_CLONE / "llm_shadow_daily.py"),
            log_dir=str(home / ".claude" / "logs")).decode("utf-8"))
        return 0
    return run_daily(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
