"""测试会话的内核写沙箱：让「测试写不进真实数据根」由内核保证，而不是事后比指纹去猜（v0.45.573）。

**为什么。** 真实数据根闸（`conftest._guard_real_data_root`）原本拿「会话前后数据根有没有变」推断「测试写没写」。
可这台机器上还有合法的并发写入者——Alpha Bot 盘中每 ~10 分钟追加一批快照、14:00 每日扫描、别的会话的工具——
它们改了目录，闸就红，测试名还落在收集序最后一条头上（本仓反复被当成「不明 teardown ERROR」，见 auto-memory
alpha-hive-data-root-migration）。豁免路径会盖住真写穿、按进程号放行是放宽闸，都不是根治。

**做法。** `pytest_configure`（收集之前）调 `sandbox_init` 给 pytest 进程套一层
`(deny file-write* (subpath <真实数据根>))`——内核强制，**所有子进程继承**（实测：Python 子进程、`sh`、`env -i`、
`sqlite3` 命令行全被拒），Alpha Bot / 扫描不在沙箱里、照常写。于是：
- 测试一旦写生产，当场 `PermissionError(EPERM)`，堆栈就落在肇事那条测试上；
- 被生产代码 `except OSError` 吞掉的写入，内核仍记进统一日志（`Sandbox: <进程>(<pid>) deny(1) file-write-… <路径>`），
  会话末尾 `denials()` 按「写操作 + 真实数据根路径前缀」捞出来报红；
- 沙箱生效时，指纹差异**只可能**来自沙箱外的写入者 ⇒ 降级为说明，不再报红。

**自证（金丝雀）。** 同一份规则里多保护一个 scratch 目录，套上后立刻试写：必须被拒才算生效。规则失效时也只写到 scratch，
从不靠「往生产目录写一下试试」来证明。金丝雀自己的拒绝记录在 `denials()` 里剔除。

**不可用时**（非 macOS、没有真实数据根、`libsandbox` 缺失、已在别的沙箱里、`ALPHA_HIVE_TEST_WRITE_SANDBOX=0`）：
`apply()` 返回 `active=False` + 原因，闸回到原来的指纹比对；报告头照实写明，不静默。

⚠️ 沙箱里 exec 不了 setuid 程序（`/bin/ps`、`/usr/bin/top`，连 `(allow default)` 也拒，实测）——用 `pgrep`（非 setuid）代替。
⚠️ 已在沙箱里的进程不能再套一层（`sandbox_init` 返回 -1 / `sandbox-exec` 退出 71）——测试里证明有牙只能靠「子进程继承」。
⚠️ zsh 里 `log` 是内建命令；这里一律调 `/usr/bin/log` 全路径。
⚠️ `log show` 在沙箱里拒绝运行（「Cannot run while sandboxed」，实测）——而沙箱之后起的子进程全都继承沙箱。
   所以**套沙箱之前**先起一个常驻的查日志助手（`_HELPER`，只会跑 `log show`、不跑测试代码），会话末尾经它的 stdin/stdout 代查。
   进程内再跑 pytest 的内层会话起不了不受限的助手 ⇒ 内层不查（`check_log=False`），它期间的拒绝落在外层会话的查询窗里，由外层核对。
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time

LIB = "/usr/lib/libsandbox.1.dylib"
LOG = "/usr/bin/log"
#: 内核日志时刻与 pytest 自记的墙钟有毫秒级偏差（端到端实测：call 阶段的写被定位成 setup 阶段）⇒ 定位时放宽这么多秒、
#: 相邻阶段一并列出，不替读者挑一个。
LOG_TS_LAG = 0.05
OPT_OUT_ENV = "ALPHA_HIVE_TEST_WRITE_SANDBOX"      # 设成 "0" ⇒ 不套（调试用；报告头会说）
#: 外层会话套好后写进环境：`<真实数据根>\n<金丝雀>`。进程内再跑 pytest（再调一次 pytest_configure）时据此认出
#: 「已继承」——不能再套一层（内核拒），但要用金丝雀再验一次，不凭这个变量就信。
ACTIVE_ENV = "ALPHA_HIVE_TEST_WRITE_SANDBOX_ACTIVE"

#: `Sandbox: Python(44562) deny(1) file-write-create /Users/igg/alpha-hive-data/x.json`
#: 查日志助手：在**套沙箱之前**起（之后起的进程都继承沙箱，跑不了 `log show`）。一行请求 → 一行 JSON 回答；stdin 关闭即退出。
_HELPER = r"""
import json, subprocess, sys
for line in sys.stdin:
    req = json.loads(line)
    try:
        r = subprocess.run(req["cmd"], capture_output=True, text=True, timeout=req.get("timeout", 120))
        out = {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except Exception as e:  # noqa: BLE001 —— 原样回报给调用方，由它报红
        out = {"rc": None, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
"""

_DENY = re.compile(r"Sandbox: (?P<proc>.+?)\((?P<pid>\d+)\) deny\(\d+\) (?P<op>\S+) (?P<path>/.*)$")
_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")


def _sbpl_string(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(protected: list[str]) -> str:
    """允许一切，只禁对 `protected` 各子树的写（含创建 / 删除 / 改名 / 改属性：`file-write*`）。"""
    rules = "".join(f"(deny file-write* (subpath {_sbpl_string(p)}))" for p in protected)
    return f"(version 1)(allow default){rules}"


def apply(root: str | None) -> dict:
    """给**当前进程**（及其后所有子进程）套写沙箱。只能调一次；返回状态字典（报告头 / 闸都读它）。

    `active=True` 当且仅当：规则装上了 **且** 金丝雀试写被内核拒绝。"""
    st = {"active": False, "reason": "", "root": None, "canary": None, "inherited": False,
          "check_log": False, "helper": None,
          "started_local": datetime.datetime.now(), "started_epoch": time.time()}
    if not root:
        st["reason"] = "本机没有真实数据根"
        return st
    if sys.platform != "darwin":
        st["reason"] = f"平台 {sys.platform} 没有 sandbox_init"
        return st
    if os.environ.get(OPT_OUT_ENV, "").strip() == "0":
        st["reason"] = f"{OPT_OUT_ENV}=0 显式关闭"
        return st
    try:
        lib = ctypes.CDLL(LIB)
        lib.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
        lib.sandbox_init.restype = ctypes.c_int
    except (OSError, AttributeError) as e:
        st["reason"] = f"加载 {LIB} 失败：{e}"
        return st
    real_root = os.path.realpath(root)
    inherited = (os.environ.get(ACTIVE_ENV) or "").split("\n")
    if len(inherited) == 2 and inherited[0] == real_root:
        st.update(root=real_root, canary=inherited[1], inherited=True)
        return _verify_canary(st, inherited[1])
    canary = os.path.realpath(tempfile.mkdtemp(prefix="alpha_hive_write_sandbox_canary_"))
    st.update(root=real_root, canary=canary)
    try:   # 必须在 sandbox_init 之前起：之后起的进程继承沙箱，`log show` 拒绝运行
        st["helper"] = subprocess.Popen([sys.executable, "-I", "-c", _HELPER], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True)
        st["check_log"] = True
    except OSError as e:
        st["reason"] = f"起查日志助手失败（{e}）——被吞掉的写拒绝将无法核对"
    err = ctypes.c_char_p()
    rc = lib.sandbox_init(profile([real_root, canary]).encode(), 0, ctypes.byref(err))
    if rc != 0:
        st["reason"] = f"sandbox_init 返回 {rc}：{(err.value or b'').decode(errors='replace')}（已在别的沙箱里？）"
        close(st)
        st["check_log"] = False
        return st
    _verify_canary(st, canary)
    if st["active"]:
        os.environ[ACTIVE_ENV] = f"{real_root}\n{canary}"
    else:
        close(st)
        st["check_log"] = False
    return st


def _verify_canary(st: dict, canary: str) -> dict:
    """在受保护的 scratch 目录试写：被内核拒 ⇒ 生效。规则失效时也只写到 scratch，不碰生产。"""
    probe = os.path.join(canary, f"probe-{os.getpid()}")
    try:
        with open(probe, "w") as f:
            f.write("沙箱失效：这一行本不该写得进来")
    except PermissionError:
        st["active"] = True
        return st
    except OSError as e:
        st["reason"] = f"金丝雀试写出了别的错（{type(e).__name__}: {e}），判不了沙箱是否生效"
        return st
    st["reason"] = "规则已装上，但金丝雀试写**成功**了——沙箱没生效（按未生效处理，闸回到指纹比对）"
    return st


def parse_denials(text: str, root: str, canary: str | None = None) -> list[dict]:
    """从 `log show --style compact` 输出里挑出「对 `root` 子树的写操作被拒」。金丝雀目录的拒绝不算。"""
    root = root.rstrip("/")
    out = []
    for line in text.splitlines():
        m = _DENY.search(line)
        if not m or not m["op"].startswith("file-write"):
            continue
        path = m["path"].strip()
        if not (path == root or path.startswith(root + "/")):
            continue
        if canary and (path == canary or path.startswith(canary.rstrip("/") + "/")):
            continue
        ts = _TS.match(line)
        out.append({"proc": m["proc"], "pid": int(m["pid"]), "op": m["op"], "path": path,
                    "ts": ts["ts"] if ts else None})
    return out


def log_command(st: dict) -> list[str]:
    start = st["started_local"].strftime("%Y-%m-%d %H:%M:%S")
    return [LOG, "show", "--start", start, "--predicate", 'sender == "Sandbox"', "--style", "compact"]


def _ask_helper(helper, cmd: list[str]) -> dict:
    helper.stdin.write(json.dumps({"cmd": cmd, "timeout": 120}) + "\n")
    helper.stdin.flush()
    line = helper.stdout.readline()
    if not line:
        return {"rc": None, "stdout": "", "stderr": f"查日志助手已退出（returncode={helper.poll()}）"}
    return json.loads(line)


def denials(st: dict, *, ask=None) -> tuple[list[dict], str | None]:
    """会话开始至今，沙箱拒掉的对真实数据根的写。返回 (拒绝列表, 查询失败原因 | None)。

    查询失败**不**当「没有拒绝」——调用方要把它报出来（失败要有人红）。`ask` 供测试注入（缺省经助手代查）。"""
    cmd = log_command(st)
    if ask is None:
        helper = st.get("helper")
        if helper is None:
            return [], "没有查日志助手（起失败，或本会话继承了外层沙箱）"
        ask = lambda c: _ask_helper(helper, c)  # noqa: E731
    try:
        r = ask(cmd)
    except (OSError, ValueError) as e:
        return [], f"代查 {LOG} show 失败：{type(e).__name__}: {e}"
    if r.get("rc") != 0:
        return [], f"{LOG} show 失败（rc={r.get('rc')}）：{(r.get('stderr') or '').strip()[:300]}"
    return parse_denials(r.get("stdout") or "", st["root"], st.get("canary")), None


def close(st: dict) -> None:
    """会话收尾：关掉查日志助手（stdin 关闭它就退出）。"""
    helper = st.get("helper")
    if helper is not None and helper.poll() is None:
        try:
            helper.stdin.close()
            helper.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            helper.kill()


def ts_epoch(ts: str | None) -> float | None:
    """统一日志的本地时刻字符串 → epoch（给 `_root_data_guard.locate` 定位是哪条测试）。"""
    if not ts:
        return None
    try:
        return datetime.datetime.strptime(ts[:26], "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except ValueError:
        return None
