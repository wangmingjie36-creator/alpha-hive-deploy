"""Alpha Bot 正在跑哪份代码、磁盘上的代码有没有更新（v0.45.726）。

为什么要有
----------
Alpha Bot 常驻一整天，代码只在启动时载入；它跑的生产克隆 `~/alpha-hive-prod` 却在每个扫描日被 `production_sync`
快进。磁盘上换成了新代码，进程里跑的还是旧的——页面上看不出来：页脚那个 `__version__`（0.45.411）是 .app 的版本号，
写进 Info.plist，不跟代码走。2026-11-04：09:16 起的进程一直跑到晚上；09:51 合入的 v0.45.713（启动时探代理）18:01
已进生产克隆，进程没重启 ⇒ `/api/meta` 里没有 `outbound`、NVDL 备用源当天 19 次全被 Yahoo 限流，没有任何提示要重启。

规则
----
- 建服务时记下代码目录的 HEAD = 「正在跑的代码」（`git rev-parse HEAD`）。
- `status()`（`/api/meta` 每次都问；页面每分钟刷一次）再读一次 HEAD，结果缓存 `TTL_S` 秒：与启动时不同 ⇒ `stale`，
  再数磁盘上新了几个提交（`git rev-list --count 运行中..磁盘`），页面挂横幅「代码已更新，重启生效」。
- 只比**提交**，不比未提交的改动（生产克隆是干净的；开发检出里改了没提交不提示）。
- 判不了（不是 git 检出 / 找不到 git / 超时 / 启动时就没读到）⇒ `available: False` + `reason`，页脚如实写「判不了」，
  **不当成「没更新」**；启动时读不到另记 WARNING。
- 只读 git（`rev-parse` / `rev-list` / `log`），不写任何东西——生产克隆的钩子拒绝一切写入，这里也不该碰。

代码目录取 `__file__`：要的就是「代码在哪」（CLAUDE.md「指向代码还是数据」那张表）；在函数里求值，不冻在 import 期。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from hive_logger import get_logger

_log = get_logger("alphabot.code_version")

#: `status()` 结果缓存秒数：页面每分钟问一次、多开几个标签也只跑一两次 git。
TTL_S = 30.0
#: 单次 git 调用上限（秒）。本机读引用通常几毫秒；卡住时宁可报「判不了」也不拖住 `/api/meta`。
GIT_TIMEOUT_S = 5.0
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SUBJECT_MAX = 120


def code_dir() -> Path:
    """本进程载入的代码所在的检出（`alphabot/` 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str, run: Callable = subprocess.run,
         timeout: float = GIT_TIMEOUT_S) -> Tuple[Optional[str], Optional[str]]:
    """跑一条只读 git 命令：`(去掉首尾空白的输出, None)`，失败 ⇒ `(None, 一句原因)`。从不抛。"""
    try:
        r = run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "找不到 git"
    except subprocess.TimeoutExpired:
        return None, f"git {args[0]} 超时（>{timeout:.0f}s）"
    except Exception as exc:  # noqa: BLE001 —— 任何别的失败都是「判不了」，原因写进返回值
        return None, f"git {args[0]} 跑不起来（{type(exc).__name__}: {exc}）"
    if r.returncode != 0:
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        return None, f"git {args[0]} 退出码 {r.returncode}：{lines[-1] if lines else '无输出'}"[:240]
    return (r.stdout or "").strip(), None


class CodeVersion:
    """启动时的代码提交 + 按需比对磁盘上的现状。线程安全（Starlette 的同步端点跑在线程池里）。"""

    def __init__(self, repo: Optional[Path] = None, *, run: Callable = subprocess.run,
                 now: Callable[[], float] = time.monotonic, ttl: float = TTL_S):
        self.repo = Path(repo) if repo is not None else code_dir()
        self._run, self._now, self._ttl = run, now, ttl
        self._lock = threading.Lock()
        self._cache: Optional[Tuple[float, dict]] = None
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.running, self.start_error = self._head()
        self.running_subject = self._subject(self.running) if self.running else None
        if self.running is None:
            _log.warning("Alpha Bot 启动时读不到代码版本（%s）：%s——页面判不了代码有没有更新", self.repo, self.start_error)

    def _head(self) -> Tuple[Optional[str], Optional[str]]:
        out, err = _git(self.repo, "rev-parse", "HEAD", run=self._run)
        if out is not None and not _SHA_RE.fullmatch(out):
            return None, f"git rev-parse 的输出不是提交号：{out[:60]!r}"
        return out, err

    def _subject(self, sha: str) -> Optional[str]:
        out, _ = _git(self.repo, "log", "-1", "--format=%s", sha, run=self._run)
        return out[:_SUBJECT_MAX] if out else None

    def status(self) -> dict:
        """`{available, stale, running, on_disk, behind, …}`。`available` 为假时 `stale` 是 None（判不了 ≠ 没更新）。"""
        with self._lock:
            if self._cache is not None and self._now() - self._cache[0] < self._ttl:
                return dict(self._cache[1])
            st = self._compute()
            self._cache = (self._now(), st)
            return dict(st)

    def _compute(self) -> dict:
        st = {"repo": str(self.repo), "started_at": self.started_at,
              "running": self.running[:7] if self.running else None, "running_full": self.running,
              "running_subject": self.running_subject,
              "available": False, "stale": None, "on_disk": None, "on_disk_subject": None,
              "behind": None, "reason": None,
              "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if self.running is None:
            st["reason"] = f"启动时没读到代码版本：{self.start_error}"
            return st
        head, err = self._head()
        if head is None:
            st["reason"] = f"现在读不到磁盘上的代码版本：{err}"
            return st
        st.update(available=True, stale=head != self.running, on_disk=head[:7])
        if head != self.running:
            st["on_disk_subject"] = self._subject(head)
            n, err = _git(self.repo, "rev-list", "--count", f"{self.running}..{head}", run=self._run)
            if n is not None and n.isdigit():
                st["behind"] = int(n)
            else:      # 数不出几个（启动时那个提交已不在本检出等）：横幅照挂，只是不写个数
                st["reason"] = f"数不出新了几个提交：{err or repr(n)}"
        return st
