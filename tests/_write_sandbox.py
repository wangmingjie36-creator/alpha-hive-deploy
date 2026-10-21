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

**并发会话归属（v0.45.577）。** 两个测试会话护同一个根时规则相同，`log show` 对谁都一样——内核那行只有
`进程名(pid)`，没有父进程、没有会话（实测 ndjson 全字段）。只按路径筛，X 会话的写会让无辜的 Y 会话也红（实测复现）。
所以每个会话在 `REGISTRY_DIR` 登记自己（pid / 根 / 起止 / 是否已核对），并经 `subprocess.Popen` 与 `os.register_at_fork`
钩子把**自己起的进程号**追加进 `<会话>.pids`。会话末尾 `attribute()` 分三类：
- 本会话的进程号 ⇒ 红；
- 另一个**同时段、护同一个根**的会话登记过的进程号，且它还活着或已核对完 ⇒ 不红，由它自己报（终端摘要说明）；
  它已结束却没核对 ⇒ 本会话照样报红（总得有人红）；
- 谁都没登记（孙进程、`os.system` 起的 shell……）⇒ **照样红**，有并发会话时如实写「可能是它的」——
  宁可两边都红并说明归属不明，也不让两边都以为是对方的。
登记目录不可用 ⇒ 退回「全算本会话」（即 v0.45.573 的行为），报告里写明。
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import functools
import re
import shutil
import stat
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
#: 会话登记目录。固定在 /tmp 而不跟 $TMPDIR：Claude Code / 终端里起的会话 TMPDIR 可能不同，登记必须彼此看得见。
REGISTRY_DIR = f"/tmp/alpha-hive-write-sandbox-{os.getuid()}"
CANARY_PREFIX = "alpha_hive_write_sandbox_canary_"
#: 结束这么久的登记 / 这么久没动的空金丝雀目录，下个会话开始时清掉（金丝雀 v0.45.573 起每会话漏一个，10-21 已积 186 个）
PRUNE_AFTER_S = 2 * 86400

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
    if len(inherited) >= 2 and inherited[0] == real_root:
        st.update(root=real_root, canary=inherited[1], inherited=True)
        if _verify_canary(st, inherited[1])["active"]:
            pids_file = inherited[2] if len(inherited) > 2 and inherited[2] else None
            if pids_file:
                _record_pid(pids_file, os.getpid())
                _install_pid_hooks(pids_file)
            return st
        # v0.45.577：标记是残留 / 外来的（本进程其实不在沙箱里）⇒ 不信它，照常自己套。只凭环境变量就跳过会让沙箱静默失效。
        stale = st["reason"]
        st.update(active=False, canary=None, inherited=False, reason="")
        os.environ.pop(ACTIVE_ENV, None)
    else:
        stale = None
    _prune_old_canaries(tempfile.gettempdir())
    canary = os.path.realpath(tempfile.mkdtemp(prefix=CANARY_PREFIX))
    st.update(root=real_root, canary=canary, owns_canary=True)
    if stale:
        st["stale_marker"] = f"环境里的 {ACTIVE_ENV} 指向同一个根，但金丝雀验不过（{stale}）——按残留标记处理，已重新套"
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
        _register(st)
        os.environ[ACTIVE_ENV] = f"{real_root}\n{canary}\n{st.get('pids_file') or ''}"
    else:
        close(st)
        st["check_log"] = False
    return st


# ---------------- 并发会话登记与归属（v0.45.577） ----------------

_PID_HOOK = {"file": None, "installed": False, "failures": 0}


def _registry_dir(d: str) -> str:
    """建（或认）本用户私有的登记目录；/tmp 共享 ⇒ 必须是本用户的真目录，不是别人放的符号链接。"""
    os.makedirs(d, mode=0o700, exist_ok=True)
    s = os.lstat(d)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid():
        raise OSError(f"{d} 不是本用户的目录")
    return d


def _write_json(path: str, obj: dict) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _record_pid(pids_file: str, pid: int) -> None:
    try:
        with open(pids_file, "a") as f:
            f.write(f"{pid}\n")
    except OSError:
        _PID_HOOK["failures"] += 1          # 记不上 ⇒ 这些进程的拒绝落进「谁都没登记」⇒ 照样红，不会漏


def _install_pid_hooks(pids_file: str) -> None:
    """把本会话起的进程号记进 `pids_file`：Popen（含 asyncio 子进程）+ fork（multiprocessing）。只装一次。"""
    _PID_HOOK["file"] = pids_file
    if _PID_HOOK["installed"]:
        return
    orig = subprocess.Popen.__init__

    @functools.wraps(orig)
    def __init__(self, *a, **k):
        orig(self, *a, **k)
        if _PID_HOOK["file"]:
            _record_pid(_PID_HOOK["file"], self.pid)

    subprocess.Popen.__init__ = __init__
    os.register_at_fork(after_in_child=lambda: _PID_HOOK["file"] and _record_pid(_PID_HOOK["file"], os.getpid()))
    _PID_HOOK["installed"] = True


def _register(st: dict, registry_dir: str | None = None) -> None:
    d = registry_dir or REGISTRY_DIR
    sid = f"{os.getpid()}-{int(st['started_epoch'] * 1000)}"
    try:
        _registry_dir(d)
        _prune_registry(d)
        st.update(registry=d, session_id=sid, pids_file=os.path.join(d, f"{sid}.pids"))
        _write_json(os.path.join(d, f"{sid}.json"), {
            "pid": os.getpid(), "root": st["root"], "started": st["started_epoch"], "ended": None, "checked": False})
        _record_pid(st["pids_file"], os.getpid())
        _install_pid_hooks(st["pids_file"])
    except OSError as e:
        st.update(registry=None, pids_file=None, registry_error=f"会话登记不可用（{e}）——并发会话的拒绝分不清归属，一律按本会话报")


def mark(st: dict, **fields) -> None:
    """更新本会话的登记（`checked=True` / `ended=…`）。失败不抛：最坏是别的会话把它当「没核对」替它报红。"""
    if not st.get("registry"):
        return
    path = os.path.join(st["registry"], f"{st['session_id']}.json")
    try:
        with open(path) as f:
            rec = json.load(f)
        rec.update(fields)
        _write_json(path, rec)
    except (OSError, ValueError):
        pass


def _read_pids(path: str) -> set[int]:
    try:
        with open(path) as f:
            return {int(x) for x in f.read().split() if x.isdigit()}
    except OSError:
        return set()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sessions(st: dict, now: float | None = None) -> list[dict]:
    """登记目录里**其他**、护同一个根、与本会话时段重叠的会话。"""
    d = st.get("registry")
    if not d:
        return []
    now = time.time() if now is None else now
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json") or name[:-5] == st.get("session_id"):
            continue
        try:
            with open(os.path.join(d, name)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("root") != st["root"] or rec.get("started", now + 1) > now:
            continue
        if rec.get("ended") is not None and rec["ended"] < st["started_epoch"]:
            continue
        rec["id"] = name[:-5]
        rec["pids"] = _read_pids(os.path.join(d, f"{rec['id']}.pids")) | {rec.get("pid")}
        rec["alive"] = rec.get("ended") is None and _alive(rec.get("pid") or 0)
        out.append(rec)
    return out


def attribute(st: dict, found: list[dict], now: float | None = None) -> tuple[list[dict], list[dict]]:
    """拒绝分成（本会话报红的, 另一个会话自己会报的）。每条加 `who`：给人看的归属说明。"""
    others = sessions(st, now)
    own = {os.getpid()} | (_read_pids(st["pids_file"]) if st.get("pids_file") else set())
    mine, foreign = [], []
    for d in found:
        d = dict(d)
        hit = [o for o in others if d["pid"] in o["pids"]]
        if d["pid"] in own or not st.get("registry"):
            d["who"] = "本会话" if st.get("registry") else st.get("registry_error", "会话登记不可用")
            mine.append(d)
        elif hit and all(o["alive"] or o.get("checked") for o in hit):
            d["who"] = "另一个测试会话 " + "、".join(f"pid {o['pid']}" for o in hit) + "（它自己会报）"
            foreign.append(d)
        elif hit:
            d["who"] = ("另一个测试会话 " + "、".join(f"pid {o['pid']}" for o in hit)
                        + " 的进程，但那个会话已结束且没完成核对——由本会话代报")
            mine.append(d)
        else:
            d["who"] = ("进程号不属于任何已登记会话（孙进程 / os.system 起的 shell 等）"
                        + (f"；同时段还有会话 {'、'.join(str(o['pid']) for o in others)} 在护同一个根，可能是它的"
                           if others else ""))
            mine.append(d)
    return mine, foreign


def _prune_registry(d: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            with open(p) as f:
                rec = json.load(f)
            last = rec.get("ended") or os.path.getmtime(p)
            if now - last < PRUNE_AFTER_S or (rec.get("ended") is None and _alive(rec.get("pid") or 0)):
                continue
        except (OSError, ValueError):
            continue
        for q in (p, p[:-5] + ".pids"):
            try:
                os.unlink(q)
            except OSError:
                pass


def _prune_old_canaries(tmp: str, now: float | None = None) -> None:
    """清掉很久没动的**空**金丝雀目录（rmdir 只删空目录：活会话的、或里面有东西的都不碰）。"""
    now = time.time() if now is None else now
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for n in names:
        if not n.startswith(CANARY_PREFIX):
            continue
        p = os.path.join(tmp, n)
        try:
            if now - os.path.getmtime(p) >= PRUNE_AFTER_S:
                os.rmdir(p)
        except OSError:
            pass


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
    try:          # v0.45.577：写进去的探针是自己的（那里并不受保护）——留着会让空目录清理永远清不掉它
        os.unlink(probe)
    except OSError:
        pass
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
    found = parse_denials(r.get("stdout") or "", st["root"], st.get("canary"))
    # v0.45.577：`--start` 只到秒 ⇒ 窗口最多早开 1 秒，会捞到本会话开始前（上一个会话末尾）的拒绝。按毫秒级时刻剔掉。
    t0 = st.get("started_epoch")
    if t0 is not None:
        found = [d for d in found if (ts_epoch(d["ts"]) is None or ts_epoch(d["ts"]) >= t0 - LOG_TS_LAG)]
    return found, None


def close(st: dict) -> None:
    """会话收尾：删本会话的金丝雀目录（v0.45.577：此前每会话漏一个）、关掉查日志助手（stdin 关闭它就退出）、登记结束时刻。

    金丝雀在本进程的沙箱里不可写（连删除也是 `file-write-unlink`）⇒ 沙箱生效时经沙箱外的助手删；没生效时本进程直接删。"""
    helper = st.get("helper")
    canary = st.get("canary")
    if st.get("owns_canary") and canary and os.path.basename(canary).startswith(CANARY_PREFIX):
        if helper is not None and helper.poll() is None:
            try:
                r = _ask_helper(helper, ["/bin/rm", "-rf", canary])
                st["canary_removed"] = r.get("rc") == 0 and not os.path.exists(canary)
            except (OSError, ValueError):
                st["canary_removed"] = False
        else:
            shutil.rmtree(canary, ignore_errors=True)
            st["canary_removed"] = not os.path.exists(canary)
    mark(st, ended=time.time())
    if helper is not None and helper.poll() is None:
        try:
            helper.stdin.close()
            helper.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            helper.kill()


def covers(st: dict | None, path) -> bool:
    """沙箱生效、且 `path` 落在被禁写的根下 ⇒ 测试进程树写不进它（逐条指纹闸可以让位给内核）。"""
    if not st or not st.get("active") or not st.get("root"):
        return False
    p = os.path.realpath(str(path))
    return p == st["root"] or p.startswith(st["root"].rstrip("/") + "/")


def ts_epoch(ts: str | None) -> float | None:
    """统一日志的本地时刻字符串 → epoch（给 `_root_data_guard.locate` 定位是哪条测试）。"""
    if not ts:
        return None
    try:
        return datetime.datetime.strptime(ts[:26], "%Y-%m-%d %H:%M:%S.%f").timestamp()
    except ValueError:
        return None
