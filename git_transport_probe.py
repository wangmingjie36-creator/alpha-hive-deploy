"""git 远端传输层探测（v0.45.351）——只在推送失败时跑一次，**纯观测**。

为什么存在（⚠️ 调用点少、平时不触发，别当死代码删）
----------------------------------------------------
2026-09-25 扫描时生产机连不上 GitHub：`ssh: connect to host github.com port 22:
Undefined error: 0`，同一时刻 `getaddrinfo(<github.io 域名>, 443)` 也失败，而 yfinance
的 HTTPS 调用照常（803 次无一失败）。远端是 `git@github.com:...`（ssh 22 端口）；GitHub
另提供 `ssh.github.com:443` 作为 22 被挡时的入口（2026-09-27 已在本机实测可用：主机密钥
SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU 与 github.com 一致，
`ssh -T -p 443` 认证通过）。

**用户决定暂不切换远端，先攒证据**。判据：

  - DNS 正常、只有 `github.com:22` 不通、`ssh.github.com:443` 通 ⇒ 切 443 能救（`port22_blocked_443_ok`）
  - DNS 本身失败（09-25 那种）⇒ 切 443 救不了（`dns_failed`）
  - 两个端口都不通 ⇒ 整体断网，切了也没用（`both_blocked`）
  - 两个端口都通 ⇒ 失败原因不在传输层（认证 / 非快进 / 远端拒收），与端口无关（`transport_ok`）

本模块就是为区分这几种情况留的一条记录：结果进 `commit_and_push_gh_pages` 的返回值
（→ `.gh_pages_deploy_log.jsonl` 与 `scan_timing.extra.gh_pages`）外加一行日志。
**不改任何远端 URL，不碰 `~/.ssh/config` / `known_hosts`，不重试，不影响部署成败判定。**

⚠️ 在 Claude 会话里手测的结论不代表生产：Claude 的 Bash 带 `HTTP(S)_PROXY`（本机经
127.0.0.1 代理出网），launchd 拉起的生产扫描没有——所以记录里带 `proxy_env`（只记变量名，
不记值：值里可能有凭据）。ssh 与这里的裸 socket 都不走 HTTP 代理，但代理进程的存在
可能改变本机 DNS 行为，记下来免得以后拿 Claude 里的实测去推生产。

时间戳带完整日期与时区：`alpha_hive.log` 只有 HH:MM:SS（auto-memory
`alpha-hive-log-no-date.md`），跨天比对要靠这里。

超时：每一步（DNS / TCP）硬上限 `_STEP_TIMEOUT` 秒，两个目标顺序跑，最坏约 20 秒。
`getaddrinfo` 走系统解析器、**不认 socket 超时**，只能放进守护线程里限时等；
挂住的线程是 daemon，不拖住进程退出（`_force_exit_if_threads_stuck` 只等非守护线程）。

测试：`tests/conftest.py::_block_git_transport_probe` 把 `_resolve` / `_tcp_connect`
钉死成离线异常——conftest 的离线闸挡在 urllib/requests/curl_cffi 库级 API 上，
**挡不住这里的裸 socket**，没有那条桩，任何一个走到推送失败路径的测试都会真去连 GitHub。
"""

import os
import socket
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

#: (主机, 端口)。第一项是现行远端的入口，第二项是候选替代入口。
TARGETS: Tuple[Tuple[str, int], ...] = (("github.com", 22), ("ssh.github.com", 443))

_STEP_TIMEOUT = 5.0

_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
               "ALL_PROXY", "all_proxy")

#: verdict → 对「要不要切 ssh.github.com:443」意味着什么。
VERDICT_MEANING: Dict[str, str] = {
    "dns_failed": "DNS 解析失败——切 443 救不了（09-25 那种）",
    "port22_blocked_443_ok": "只有 22 不通、443 通——切到 ssh.github.com:443 能救",
    "both_blocked": "两个端口都连不上——整体断网，切 443 也没用",
    "port443_blocked_22_ok": "22 通、443 不通——现行远端没问题，失败原因在别处",
    "transport_ok": "两个端口都通——失败不在传输层（认证 / 非快进 / 远端拒收）",
    "probe_error": "探测本身出错——无结论",
}


def _resolve(host: str, port: int):
    """`getaddrinfo` 的薄包装（测试桩的替换点）。抛异常 = 解析失败。"""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _tcp_connect(family: int, sockaddr, timeout: float) -> None:
    """对**已解析好的地址**做一次 TCP 握手（测试桩的替换点）。抛异常 = 连不上。

    不用 `socket.create_connection((host, port))`：它内部会再解析一次主机名，
    那次解析不受超时约束。
    """
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sockaddr)
    finally:
        s.close()


def _bounded(fn, timeout: float, *args) -> Tuple[bool, Any]:
    """在守护线程里跑 `fn(*args)`，最多等 `timeout` 秒。返回 `(成功?, 返回值或错误文本)`。"""
    box: Dict[str, Any] = {}

    def _run():
        try:
            box["value"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 - 纯观测，任何异常都只记录
            box["error"] = e

    t = threading.Thread(target=_run, name="git_transport_probe", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, f"超时（>{timeout:.0f}s 未返回）"
    if "error" in box:
        e = box["error"]
        return False, f"{type(e).__name__}: {e}"[:200]
    return True, box.get("value")


def _last_line(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    return lines[-1][:300] if lines else None


def _probe_target(host: str, port: int) -> Dict[str, Any]:
    entry: Dict[str, Any] = {}
    t0 = time.monotonic()
    ok, val = _bounded(_resolve, _STEP_TIMEOUT, host, port)
    entry["dns_ok"] = ok
    entry["dns_seconds"] = round(time.monotonic() - t0, 2)
    if not ok or not val:
        entry["dns_error"] = str(val)[:200] if not ok else "getaddrinfo 返回空"
        entry["dns_ok"] = False
        entry["tcp_ok"] = None                 # 没解析出地址，TCP 无从谈起（不是「连上了」也不是「没连上」）
        return entry
    family, _type, _proto, _canon, sockaddr = val[0]
    entry["addr"] = str(sockaddr[0])
    t1 = time.monotonic()
    ok2, val2 = _bounded(_tcp_connect, _STEP_TIMEOUT + 1, family, sockaddr, _STEP_TIMEOUT)
    entry["tcp_ok"] = ok2
    entry["tcp_seconds"] = round(time.monotonic() - t1, 2)
    if not ok2:
        entry["tcp_error"] = str(val2)[:200]
    return entry


def classify(targets: Dict[str, Dict[str, Any]]) -> str:
    """按 `TARGETS` 两项的结果给出 verdict（键见 `VERDICT_MEANING`）。"""
    cur = targets.get(f"{TARGETS[0][0]}:{TARGETS[0][1]}") or {}
    alt = targets.get(f"{TARGETS[1][0]}:{TARGETS[1][1]}") or {}
    if not cur.get("dns_ok") or not alt.get("dns_ok"):
        return "dns_failed"
    if cur.get("tcp_ok") and alt.get("tcp_ok"):
        return "transport_ok"
    if not cur.get("tcp_ok") and alt.get("tcp_ok"):
        return "port22_blocked_443_ok"
    if not cur.get("tcp_ok") and not alt.get("tcp_ok"):
        return "both_blocked"
    return "port443_blocked_22_ok"


def probe_github_transport(git_stderr: Optional[str] = None) -> Dict[str, Any]:
    """探测一次并返回记录。**永不抛异常**——探测失败本身也只是一条记录。"""
    started = time.monotonic()
    rec: Dict[str, Any] = {}
    try:
        rec["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        rec["purpose"] = "为「是否把 git 远端切到 ssh.github.com:443」攒判据（v0.45.351）"
        rec["git_stderr_last_line"] = _last_line(git_stderr)
        rec["proxy_env"] = sorted(v for v in _PROXY_VARS if os.environ.get(v))
        targets = {f"{h}:{p}": _probe_target(h, p) for h, p in TARGETS}
        rec["targets"] = targets
        rec["verdict"] = classify(targets)
    except Exception as e:  # noqa: BLE001 - 纯观测：探测代码的 bug 不得影响部署判定
        rec["verdict"] = "probe_error"
        rec["probe_error"] = f"{type(e).__name__}: {e}"[:200]
    rec["meaning"] = VERDICT_MEANING.get(rec.get("verdict"), "")
    rec["seconds"] = round(time.monotonic() - started, 2)
    return rec


def one_line(rec: Optional[Dict[str, Any]]) -> str:
    """日志用的一行摘要。"""
    if not isinstance(rec, dict):
        return "（无探测记录）"
    parts = []
    for key, e in (rec.get("targets") or {}).items():
        if not e.get("dns_ok"):
            parts.append(f"{key} DNS✗({e.get('dns_error', '')[:60]})")
        else:
            parts.append(f"{key} TCP{'✓' if e.get('tcp_ok') else '✗'}({e.get('tcp_seconds')}s)")
    return (f"{rec.get('verdict')}：{rec.get('meaning')} | " + "；".join(parts)
            + f" | proxy_env={rec.get('proxy_env')} | {rec.get('at')}")
