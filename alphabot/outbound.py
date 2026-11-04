"""Alpha Bot 出站方式：启动时探一次本机代理，通就让本进程走代理（v0.45.713）。

为什么要有：从 `Alpha Bot.app` 起的服务继承 launchd 环境，**没有** `HTTPS_PROXY`；yfinance 1.2 用 curl_cffi，
不读 macOS 系统代理 ⇒ 直连。直连出口被 Yahoo 掐（编排器 08-29 起实测：直连 yfinance 403/429、走代理 200）。
2026-11-04 同一时刻、各起新进程：带代理变量 `Ticker("NVDL").options` 通，去掉就 429 ——而 Alpha Bot 当天
07:31 起每 10 分钟 1 个请求、全 429。10-27/28「谁在耗额度日志里查不出」的答案就是这条：没人在耗，是走了直连。

规则（与编排器 `_configure_outbound` 同一个代理地址、同一种探活；不共用代码：那边是部署副本里的 shell）：
- 环境里已有能管 HTTPS 的代理变量（`HTTPS_PROXY_VARS`，终端里起的）⇒ 原样不动（`inherited`）；只有 `http_proxy` 不算。
- `ALPHA_HIVE_PROXY=none` ⇒ 不探、不改（`disabled`，显式关闭，非故障）。
- 否则探 `ALPHA_HIVE_PROXY`（缺省 `http://127.0.0.1:7897`）：端口可连 + 经它的 HTTPS 隧道可用 ⇒ 导出代理变量（`proxy`）。
- 探不通 ⇒ **环境不改**（`unchanged`：yfinance 照旧直连，urllib 仍可能经 macOS 系统代理），
  并且失败要看得见：WARNING 日志 + `/api/meta` 的 `outbound` + 页面横幅。

⚠️ 只在启动时探一次（Alpha Bot 常驻一整天）：代理晚于 Alpha Bot 起来 ⇒ 要重启 Alpha Bot 才走代理；
横幅就是为这种情况写的。不在运行中改环境变量——别的线程正拿着已建好的会话。
"""
from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from typing import Callable, MutableMapping, Optional

DEFAULT_PROXY = "http://127.0.0.1:7897"
PROBE_URL = "https://cdn.cboe.com/"          # 与编排器同一个目标：几百字节，只验隧道
#: 能让 HTTPS 请求走代理的变量。⚠️ 只有 `http_proxy` 不算：libcurl（curl_cffi）对 https URL 只认 https_proxy / all_proxy，
#: 2026-11-04 实测只设 http_proxy 时 yfinance 照样直连、429（v0.45.713 二次检查）。
HTTPS_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
LOOPBACK_NO_PROXY = "localhost,127.0.0.1,::1,.local"


def probe_proxy(url: str, connect_timeout: float = 3.0, https_timeout: float = 8.0) -> Optional[str]:
    """通 ⇒ None；不通 ⇒ 一句原因。只探端口不够（端口开着但转发坏了会把整轮拖死），所以再连一次真实 HTTPS。"""
    hostport = url.split("//")[-1].split("/")[0]
    host, _, port = hostport.rpartition(":") if ":" in hostport else (hostport, "", "80")
    try:
        with socket.create_connection((host, int(port or 80)), timeout=connect_timeout):
            pass
    except (OSError, ValueError) as exc:
        return f"端口连不上（{type(exc).__name__}: {exc}）"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
    try:
        opener.open(urllib.request.Request(PROBE_URL, headers={"User-Agent": "alpha-hive/1.0"}),
                    timeout=https_timeout)
    except urllib.error.HTTPError:
        return None          # 4xx/5xx 说明隧道通了，目标站怎么答不重要
    except Exception as exc:  # noqa: BLE001 — 任何别的失败都是「隧道不可用」，原因写进返回值
        return f"HTTPS 隧道不可用（{type(exc).__name__}: {exc}）"
    return None


def configure(environ: Optional[MutableMapping[str, str]] = None,
              probe: Callable[[str], Optional[str]] = probe_proxy) -> dict:
    """定本进程出站方式，必要时改 `environ`（缺省 `os.environ`）。返回 `{"mode", "proxy", "detail"}`。"""
    env = os.environ if environ is None else environ
    inherited = next((k for k in HTTPS_PROXY_VARS if env.get(k)), None)
    if inherited:
        return {"mode": "inherited", "proxy": env[inherited], "detail": f"环境里已有 {inherited}，原样使用"}
    url = (env.get("ALPHA_HIVE_PROXY") or DEFAULT_PROXY).strip()
    if url.lower() == "none":
        return {"mode": "disabled", "proxy": None, "detail": "ALPHA_HIVE_PROXY=none，显式不走代理"}
    why = probe(url)
    if why is not None:
        return {"mode": "unchanged", "proxy": url,
                "detail": f"代理 {url} 探活失败：{why} —— 本进程不走代理，yfinance 直连（直连常被 Yahoo 限流）；"
                          "代理起来后重启 Alpha Bot"}
    for k in ("HTTPS_PROXY", "https_proxy"):
        env[k] = url
    for k in ("HTTP_PROXY", "http_proxy"):       # 已有的 http 代理原样保留（只缺 https 那半才走到这里）
        if not env.get(k):
            env[k] = url
    for k in ("NO_PROXY", "no_proxy"):
        have = [x for x in (env.get(k) or "").split(",") if x.strip()]
        env[k] = ",".join(have + [x for x in LOOPBACK_NO_PROXY.split(",") if x not in have])
    return {"mode": "proxy", "proxy": url, "detail": f"探活通过，出站走代理 {url}"}
