#!/usr/bin/env python3
"""在一条命令运行期间给机器加 CPU 负载，**负载进程自带寿命、清理失败会红**（v0.45.668）。

为什么要有它
------------
想知道「某个测试在机器忙的时候会不会偶发失败」，得先把机器压忙。10-27 有会话手写
`for i in $(seq 20); do yes > /dev/null & pids="$pids $!"; done; trap "kill $pids 2>/dev/null" EXIT`：

1. 本机 Bash 是 zsh，`$pids` 不按空格拆词 ⇒ `kill` 收到一整个字符串、报 `illegal pid`，一个都没杀；
2. `2>/dev/null` 把这条报错吞了；
3. 校验用的 `pgrep -c` 在 macOS 上不存在（打 usage、退出码非 0），`|| echo "none left"` 于是恒打印「已清理」。

结果 20 个 `yes` 空转 40 分钟、load 124，别的会话的整套测试被饿到 8% CPU。三处缺陷各自都小，叠起来才泄漏；
所以这里不修补 shell 写法，而是把「谁负责让负载停下来」换个位置：

- **负载进程自己负责死**：每个子进程带一个截止时刻，到点自己退出；并且每隔一小段检查自己的父进程还在不在
  （父进程没了 ⇒ `getppid()` 变成 1），不在就退出。即使整套清理逻辑都失效，代价也只是「最多到截止时刻」，
  不是「永远」。
- **清理仍然做，但不信它**：退出时对整个进程组先 SIGTERM、再 SIGKILL，然后**逐个核对已经死了**；还有活的 ⇒
  抛 `BurnerLeak` / 命令行退出码 4，并点名 PID。「清理失败」从无声变成会红。
- 不在 shell 里拼 PID 列表：进程由 Python 的 `subprocess` 管，没有「变量拆不拆词」这一类问题。

用法
----
命令行（被压的命令的退出码原样返回；清理有残留则退出码 4）：

    /usr/local/bin/python3 tools/burn_cpu.py --procs 20 --seconds 600 -- <命令> [参数...]

代码里：

    with Burners(procs=20, seconds=600):
        ...被压的代码...

`--procs` 缺省取逻辑核数的 2 倍；`--seconds` 是**硬上限**（必填语义：不给就用缺省 600 秒，不存在「无期限」）。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

#: 硬上限的缺省值：压测命令通常几分钟；取 10 分钟，给的值超过 `MAX_SECONDS` 一律拒绝（不存在无期限负载）。
DEFAULT_SECONDS = 600.0
MAX_SECONDS = 3600.0

#: 子进程每干这么多次空转就看一眼钟与父进程；太小 = 系统调用占满、太大 = 退出变慢。
_SPIN_CHUNK = 200_000

#: 清理时给 SIGTERM 的宽限，过了就 SIGKILL。
_TERM_GRACE = 2.0

#: 退出码：清理后仍有负载进程活着。与被压命令的退出码区分开。
EXIT_LEAK = 4

# 子进程的全部代码：到点退出 / 父进程不在了退出。写成字符串经 `-c` 传，不依赖任何仓库模块、不读任何文件。
_CHILD = (
    "import os, sys, time\n"
    "deadline = float(sys.argv[1]); ppid = int(sys.argv[2]); n = 0\n"
    "while True:\n"
    f"    for _ in range({_SPIN_CHUNK}):\n"
    "        n += 1\n"
    "    if time.time() >= deadline or os.getppid() != ppid:\n"
    "        break\n"
)


class BurnerLeak(RuntimeError):
    """清理之后仍有负载进程活着（携带它们的 pid）。"""

    def __init__(self, pids: list[int]):
        super().__init__(f"负载进程清理后仍有 {len(pids)} 个活着：{pids}——手动处理：kill -9 {' '.join(map(str, pids))}")
        self.pids = pids


class Burners:
    """上下文管理器：进入时起 `procs` 个满载子进程，退出时清理并核对零残留。"""

    def __init__(self, procs: int | None = None, seconds: float = DEFAULT_SECONDS):
        if not (0 < seconds <= MAX_SECONDS):
            raise ValueError(f"seconds 必须在 (0, {MAX_SECONDS:g}] 内（不存在无期限负载），收到 {seconds!r}")
        n = procs if procs is not None else 2 * (os.cpu_count() or 1)
        if n < 1:
            raise ValueError(f"procs 必须 ≥ 1，收到 {procs!r}")
        self.procs = n
        self.seconds = seconds
        self._children: list[subprocess.Popen] = []

    @property
    def pids(self) -> list[int]:
        return [p.pid for p in self._children]

    def __enter__(self) -> "Burners":
        deadline = time.time() + self.seconds
        me = os.getpid()
        try:
            for _ in range(self.procs):
                # 每个子进程自成一组：清理时按「自己起的」逐个 killpg，不碰别人的进程组
                self._children.append(subprocess.Popen(
                    [sys.executable, "-c", _CHILD, repr(deadline), str(me)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True))
        except BaseException as e:
            leaked = self._cleanup()        # 起到一半失败也要收掉已起的；收不掉就换成 BurnerLeak（原异常挂在 __cause__）
            if leaked:
                raise BurnerLeak(leaked) from e
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        leaked = self._cleanup()
        if leaked:
            raise BurnerLeak(leaked)

    def _signal_all(self, sig: int) -> None:
        for p in self._children:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, sig)          # start_new_session ⇒ 子进程是自己进程组的组长
                except (ProcessLookupError, PermissionError):
                    pass                            # 已经死了；是否真死由 `_alive` 核对，不靠这里的返回

    def _alive(self) -> list[int]:
        return [p.pid for p in self._children if p.poll() is None]

    def _cleanup(self) -> list[int]:
        """杀掉并等待；返回仍活着的 pid（空 = 干净）。"""
        self._signal_all(signal.SIGTERM)
        end = time.time() + _TERM_GRACE
        while self._alive() and time.time() < end:
            time.sleep(0.05)
        if self._alive():
            self._signal_all(signal.SIGKILL)
            end = time.time() + _TERM_GRACE
            while self._alive() and time.time() < end:
                time.sleep(0.05)
        return self._alive()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="在命令运行期间加 CPU 负载（负载进程自带寿命、清理失败会红）")
    ap.add_argument("--procs", type=int, default=None, help="负载进程数（缺省：逻辑核数 × 2）")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                    help=f"负载硬上限秒数，到点自己退出（缺省 {DEFAULT_SECONDS:g}，最大 {MAX_SECONDS:g}）")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="`--` 之后是被压的命令")
    a = ap.parse_args(argv)
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        ap.error("缺命令：把被压的命令放在 `--` 之后")
    try:
        burners = Burners(procs=a.procs, seconds=a.seconds)
    except ValueError as e:
        ap.error(str(e))
    rc = 1
    try:
        with burners:
            print(f"burn_cpu: {burners.procs} 个负载进程，硬上限 {a.seconds:g} 秒", file=sys.stderr)
            rc = subprocess.call(cmd)
    except BurnerLeak as e:
        print(f"burn_cpu: ⚠️ {e}", file=sys.stderr)
        return EXIT_LEAK
    print("burn_cpu: 负载进程已清理（逐个核对为零残留）", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
