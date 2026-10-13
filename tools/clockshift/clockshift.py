"""拨钟 harness：把「现在」整体拨到未来 N 天跑测试，找写死日期的时间炸弹（v0.45.495 起在仓库里）。

用法——**审计请用 `audit.py`**（基线 / 拨钟各跑一遍、只报差集、重跑滤 flaky）：

    /usr/local/bin/python3 tools/clockshift/audit.py --days 7 --days 45 -- -m "not integration and not network"

只想拿一个文件在某个时钟下跑一遍（快速核对夹具，不做差分）：

    CLOCK_SHIFT_DAYS=30 PYTHONPATH="$PWD/tools/clockshift" /usr/local/bin/python3 -m pytest -p clockshift tests/test_x.py

`CLOCK_SHIFT_DAYS` 未设或为 0 时什么都不做（`CLOCK_SHIFT_FORCE=1` 则偏移 0 也装上全部补丁——
用来证明 harness 自己不制造红）。

**设计不变式：拨的是整个世界，不是一只钟。** v0.45.495 之前的 harness 只拨 `datetime.now/today`，
刻意不拨 `time.time()` 与文件 mtime ⇒ 「现在」和「刚写的文件」差 N 天——真实的未来里不存在这种世界，
被测代码（编排器解释器的陈旧判定 `mtime_before_date`）**正确地**判了陈旧：+30 天全套件 41 条假红里
30 条是这个（R2），另 11 条是拨钟只在 pytest 进程内、子进程读真钟（R1）。所以这里：

- **一致前移**：`datetime.now/today/utcnow`、`date.today`、`time.time/time_ns/localtime/gmtime/ctime/
  asctime/strftime`、pandas `Timestamp.now`；`os.stat/lstat/fstat` 读出的 atime/mtime/ctime(+birthtime) +N，
  `os.utime` 写入显式时间时 −N（往返一致：刚写的文件 mtime ≈ 现在；`utime(p, (t, t))` 后读回仍是 t）。
  `time.monotonic/perf_counter` 不拨（只用作差值）。
- **跨进程**：本目录的 `sitecustomize.py` 让每个 Python 进程**启动时**自己装上（拨钟是环境
  `CLOCK_SHIFT_DAYS` + `PYTHONPATH` 的属性，不是某个进程的状态）；`subprocess.Popen` 包一层，把这两项补进
  测试自建的 env，以及 `env -i ...` 形式的参数里。
- **C 扩展安全网 = 导入钩子**，不是 pre-import：标准库 / site-packages 里的模块**执行导入时**临时换回真类
  （pandas 的 Cython `Timestamp` 继承 `datetime.datetime`，基类若是 Python 层子类会段错误；
  `pydantic.v1` 的 `ConstrainedDate` 会元类冲突）；本仓与测试自己的模块拿假类。旧 harness 靠启动时 AST 扫全仓
  再 import 全部第三方包（每进程 3.4–3.9 s，本仓 ~100 个 Python 子进程负担不起），并因此必须在仓库根运行。

**已知漏点**（拨不到、会与 Python 侧不一致；`audit.py` 的差分会把真撞上的测试暴露出来）：
SQLite `CURRENT_TIMESTAMP` / `datetime('now')` 默认值、`git` 提交时间、bash `date`、`numpy.datetime64('now')`、
C 扩展直读系统钟、`os.DirEntry.stat()`（本仓零使用）、`python -I/-E` 起的子进程（本仓测试零使用）、
导入钩子窗口内其它线程动态取 `datetime.datetime`（拿到真类，极少见）。

**外部世界拨不动**：测试之外按真实钟写下的记录（部署记录、Alpha Bot 盘中采样……）拿来和「现在」比，拨钟后恒红、
什么也不说明 ⇒ 这类测试标 `@pytest.mark.external_clock`，本插件启用时**摘掉**（摘要里 N deselected、终端摘要逐条列出）。
别给夹具自造日期的测试打这个标——那正是审计要抓的。并发写入者（Alpha Bot 盘中写真实数据根）带不带 harness
真实数据根闸都红；`audit.py` 的基线也会红、重跑会把偶发的滤成 flaky。
"""
import datetime as _dt
import os
import subprocess as _subprocess
import sys
import threading as _threading
import time as _time

_DAYS = float(os.environ.get("CLOCK_SHIFT_DAYS") or "0")
_FORCE = os.environ.get("CLOCK_SHIFT_FORCE") == "1"
ACTIVE = _DAYS != 0.0 or _FORCE
HERE = os.path.dirname(os.path.abspath(__file__))

_SHIFT = _dt.timedelta(days=_DAYS)
_SHIFT_S = _SHIFT.total_seconds()
_SHIFT_NS = round(_SHIFT_S * 1e9)

_RealDate = _dt.date
_RealDatetime = _dt.datetime

INSTALLED = False
PANDAS_CLOCK_PATCHED = False
CHILD_ENV_PATCHES = 0


# ────────────────────────────────────────────────────────────── 假 datetime 类

class _DateMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _RealDate)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _RealDate)


class _DatetimeMeta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _RealDatetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _RealDatetime)


class _ShiftedDate(_RealDate, metaclass=_DateMeta):
    __slots__ = ()  # tp_basicsize 必须与 datetime.date 相同，否则 numpy/pandas C 扩展尺寸校验后段错误

    @classmethod
    def today(cls):
        return (_RealDatetime.now() + _SHIFT).date()


class _ShiftedDatetime(_RealDatetime, metaclass=_DatetimeMeta):
    __slots__ = ()

    @classmethod
    def now(cls, tz=None):
        return _RealDatetime.now(tz) + _SHIFT

    @classmethod
    def today(cls):
        return _RealDatetime.now() + _SHIFT

    @classmethod
    def utcnow(cls):
        return _RealDatetime.utcnow() + _SHIFT


# ────────────────────────────────────────────────────────────── 导入钩子：第三方 / 标准库拿真类

_swap_lock = _threading.RLock()
_swap_depth = 0


class _RealClasses:
    """进入时把 `datetime.date/datetime` 换回真类，退出时换回假类；可重入（嵌套导入）。"""

    def __enter__(self):
        global _swap_depth
        _swap_lock.acquire()
        if _swap_depth == 0:
            _dt.date, _dt.datetime = _RealDate, _RealDatetime
        _swap_depth += 1

    def __exit__(self, *exc):
        global _swap_depth
        _swap_depth -= 1
        if _swap_depth == 0:
            _dt.date, _dt.datetime = _ShiftedDate, _ShiftedDatetime
        _swap_lock.release()


def _protected_prefixes():
    import site
    import sysconfig
    out = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p:
            out.add(p)
    try:
        out.update(site.getsitepackages())
    except AttributeError:  # pragma: no cover - virtualenv 老版本
        pass
    try:
        out.add(site.getusersitepackages())
    except AttributeError:  # pragma: no cover
        pass
    return tuple(sorted({os.path.realpath(p) + os.sep for p in out}))


_PROTECTED = ()


def _is_protected(spec):
    origin = getattr(spec, "origin", None)
    if origin in (None, "built-in", "frozen", "namespace"):
        return True
    return os.path.realpath(origin).startswith(_PROTECTED)


class _SwapLoader:
    """包住原 loader：只在 `exec_module` 期间换回真类。其余属性原样转发。"""

    def __init__(self, loader):
        self._loader = loader

    def __getattr__(self, name):
        return getattr(self._loader, name)

    def create_module(self, spec):
        with _RealClasses():  # 单阶段初始化的 C 扩展在这里就执行了模块体
            return self._loader.create_module(spec)

    def exec_module(self, module):
        with _RealClasses():
            self._loader.exec_module(module)
        if module.__name__ == "pandas":
            _patch_pandas_clock(module)


class _ProtectedImportFinder:
    """放在 `sys.meta_path` 最前：找到 spec 后，若模块在标准库 / site-packages 里就包一层 `_SwapLoader`。"""

    def find_spec(self, name, path=None, target=None):
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is not None and _is_protected(spec) and not isinstance(spec.loader, _SwapLoader):
            spec.loader = _SwapLoader(spec.loader)
        return spec


def _patch_pandas_clock(pd):
    """pandas 有自己的钟（`Timestamp.now/today` 直读 C 钟）。**只拨 `now`**：pandas 的 `today()` 就是
    `cls.now()`，两处都拨会拨两次（旧 harness 实测 +30 变 +60）。"""
    global PANDAS_CLOCK_PATCHED
    if PANDAS_CLOCK_PATCHED:
        return
    ts_now = pd.Timestamp.now
    pd.Timestamp.now = classmethod(lambda cls, tz=None: ts_now(tz) + _SHIFT)
    PANDAS_CLOCK_PATCHED = True
    _agree("pd.Timestamp.now()", pd.Timestamp.now().to_pydatetime())
    _agree("pd.Timestamp.today()", pd.Timestamp.today().to_pydatetime())


# ────────────────────────────────────────────────────────────── epoch 钟与文件时间戳

_real_time, _real_time_ns = _time.time, _time.time_ns
_real_localtime, _real_gmtime = _time.localtime, _time.gmtime
_real_ctime, _real_asctime, _real_strftime = _time.ctime, _time.asctime, _time.strftime
_real_stat, _real_lstat, _real_fstat, _real_utime = os.stat, os.lstat, os.fstat, os.utime


def _now_s():
    return _real_time() + _SHIFT_S


def _localtime(*args):
    return _real_localtime(_now_s() if not args or args[0] is None else args[0])


def _gmtime(*args):
    return _real_gmtime(_now_s() if not args or args[0] is None else args[0])


def _ctime(*args):
    return _real_ctime(_now_s() if not args or args[0] is None else args[0])


def _asctime(*args):
    return _real_asctime(_localtime() if not args else args[0])


def _strftime(fmt, *args):
    return _real_strftime(fmt, _localtime() if not args else args[0])


_TIME_FIELDS = ("st_atime", "st_mtime", "st_ctime", "st_birthtime")


def _shift_stat(st):
    cls, (seq, extra) = st.__reduce__()[:2]
    seq = list(seq)
    for i in (7, 8, 9):  # int atime / mtime / ctime
        seq[i] = int(seq[i] + _SHIFT_S)
    extra = dict(extra)
    for f in _TIME_FIELDS:
        if extra.get(f) is not None:
            extra[f] += _SHIFT_S
        if extra.get(f + "_ns") is not None:
            extra[f + "_ns"] += _SHIFT_NS
    return cls(tuple(seq), extra)


def _stat(*a, **k):
    return _shift_stat(_real_stat(*a, **k))


def _lstat(*a, **k):
    return _shift_stat(_real_lstat(*a, **k))


def _fstat(*a, **k):
    return _shift_stat(_real_fstat(*a, **k))


def _utime(path, times=None, *, ns=None, **kw):
    if times is not None:
        times = (times[0] - _SHIFT_S, times[1] - _SHIFT_S)
    if ns is not None:
        ns = (ns[0] - _SHIFT_NS, ns[1] - _SHIFT_NS)
    if ns is None:
        return _real_utime(path, times, **kw)
    return _real_utime(path, times, ns=ns, **kw)


# ────────────────────────────────────────────────────────────── 子进程

def _clock_env_items():
    items = {"CLOCK_SHIFT_DAYS": os.environ.get("CLOCK_SHIFT_DAYS") or repr(_DAYS)}
    if _FORCE:
        items["CLOCK_SHIFT_FORCE"] = "1"
    return items


def _with_pythonpath(value):
    parts = [p for p in (value or "").split(os.pathsep) if p]
    if any(os.path.isabs(p) and os.path.realpath(p) == os.path.realpath(HERE) for p in parts):
        return value
    return os.pathsep.join([HERE, *parts])


def _child_env(env):
    """补齐子进程 env：**只补缺、不覆盖**（测试显式给的 `CLOCK_SHIFT_DAYS` 照用——harness 自测就靠它起真实钟的
    子进程）。不缺就原样返回（不造多余拷贝）。"""
    global CHILD_ENV_PATCHES
    want = {k: v for k, v in _clock_env_items().items() if k not in env}
    pp = _with_pythonpath(env.get("PYTHONPATH"))
    if not want and pp == env.get("PYTHONPATH"):
        return env
    CHILD_ENV_PATCHES += 1
    return {**env, **want, "PYTHONPATH": pp}


def _patch_env_i(args):
    """`["/usr/bin/env", "-i", "K=V", ..., prog]`：把拨钟两项塞到 `-i` 后面（`env -i` 会清空环境）。"""
    if not isinstance(args, (list, tuple)) or len(args) < 2:
        return args
    if os.path.basename(str(args[0])) != "env":
        return args
    args = list(args)
    for i in range(1, len(args)):
        a = str(args[i])
        if a in ("-i", "--ignore-environment", "-"):
            assigns = {}
            j = i + 1
            while j < len(args) and "=" in str(args[j]) and not str(args[j]).startswith("-"):
                k, _, v = str(args[j]).partition("=")
                assigns[k] = v
                j += 1
            add = [f"{k}={v}" for k, v in _clock_env_items().items() if k not in assigns]
            add.append(f"PYTHONPATH={_with_pythonpath(assigns.get('PYTHONPATH'))}")
            rest = [x for x in args[i + 1:j] if not str(x).startswith("PYTHONPATH=")]
            global CHILD_ENV_PATCHES
            CHILD_ENV_PATCHES += 1
            return [*args[:i + 1], *rest, *add, *args[j:]]
        if not a.startswith("-"):
            break
    return args


_real_popen_init = _subprocess.Popen.__init__


def _popen_init(self, args, *a, **kw):
    args = _patch_env_i(args)
    env = kw.get("env")
    kw["env"] = _child_env(os.environ if env is None else env)
    if env is None and kw["env"] is os.environ:
        kw["env"] = None  # os.environ 本身已齐全：保持「继承」语义
    return _real_popen_init(self, args, *a, **kw)


# ────────────────────────────────────────────────────────────── 替身要像内建函数一样「不绑定」

def _replacements():
    return [(_time, "time", _now_s), (_time, "time_ns", lambda: _real_time_ns() + _SHIFT_NS),
            (_time, "localtime", _localtime), (_time, "gmtime", _gmtime), (_time, "ctime", _ctime),
            (_time, "asctime", _asctime), (_time, "strftime", _strftime),
            (os, "stat", _stat), (os, "lstat", _lstat), (os, "fstat", _fstat), (os, "utime", _utime)]


_CAPABILITY_SETS = ("supports_dir_fd", "supports_fd", "supports_follow_symlinks", "supports_effective_ids")


def _install_replacement(mod, name, fn):
    """内建函数存成类属性时**不绑定** `self`，普通 Python 函数会——`logging.Formatter` 在类上存着
    `converter = time.localtime`，换成普通函数后 `self.converter(t)` 变成 `f(formatter, t)`，拨钟下日志格式化
    全部 TypeError（v0.45.495 全套件实测：再叠上 3.11 `HTTPError(fp=None)` 的 `__getattr__` 缺陷，pytest 自己
    INTERNALERROR 崩掉整轮）。⇒ 一律包成 `staticmethod`（3.10+ 可直接调用、作类属性不绑定），并照原函数登记进
    `os.supports_*` 能力集合（`shutil` 等按「函数 in 集合」决定走哪条路径）。"""
    orig = getattr(mod, name)
    sm = staticmethod(fn)
    setattr(mod, name, sm)
    if mod is os:
        for cap in _CAPABILITY_SETS:
            caps = getattr(os, cap, None)
            if isinstance(caps, set) and orig in caps:
                caps.add(sm)


# ────────────────────────────────────────────────────────────── 自检与安装

_expect = None


def _agree(label, got):
    skew = abs((got - _expect).total_seconds())
    assert skew < 120, f"clockshift 自检：{label} 偏离期望 {_expect} 共 {skew:.0f}s"


def _self_check():
    """所有钟必须一致 ≈ 真实现在 + 偏移；刚写的文件 mtime 必须 ≈ 现在（R2 的不变式）。失败在启动时就炸——
    一个钟没拨齐的 harness 跑出来的绿什么也证明不了。"""
    import tempfile
    global _expect
    _expect = _RealDatetime.now() + _SHIFT

    class _AsClassAttr:  # 替身存成类属性、经实例调用，不能绑定 self（见 _install_replacement）
        pass
    for mod, name, _ in _replacements():
        setattr(_AsClassAttr, "f", getattr(mod, name))
        f = _AsClassAttr().f
        assert f is getattr(mod, name) or getattr(f, "__self__", None) is None, \
            f"clockshift 自检：{mod.__name__}.{name} 的替身作类属性会绑定 self"
    _agree("datetime.now()", _dt.datetime.now())
    _agree("datetime.today()", _dt.datetime.today())
    assert _dt.date.today() == _expect.date(), f"clockshift 自检：date.today()={_dt.date.today()} 期望 {_expect.date()}"
    _agree("time.time()", _RealDatetime.fromtimestamp(_time.time()))
    _agree("time.localtime()", _RealDatetime(*_time.localtime()[:6]))
    fd, p = tempfile.mkstemp(prefix="clockshift_selfcheck_")
    try:
        os.close(fd)
        _agree("刚写文件的 os.stat().st_mtime", _RealDatetime.fromtimestamp(os.stat(p).st_mtime))
        os.utime(p, (1_000_000_000.0, 1_000_000_000.0))
        assert os.stat(p).st_mtime == 1_000_000_000.0, "clockshift 自检：os.utime 显式时间往返不一致"
    finally:
        os.unlink(p)


def install():
    global INSTALLED, _PROTECTED
    if INSTALLED or not ACTIVE:
        return
    _PROTECTED = _protected_prefixes()
    sys.meta_path.insert(0, _ProtectedImportFinder())
    _dt.date, _dt.datetime = _ShiftedDate, _ShiftedDatetime
    for mod, name, fn in _replacements():
        _install_replacement(mod, name, fn)
    _subprocess.Popen.__init__ = _popen_init
    import sqlite3  # 假类构造出来的实例也要能绑定进 sqlite
    sqlite3.register_adapter(_ShiftedDate, lambda d: d.isoformat())
    sqlite3.register_adapter(_ShiftedDatetime, lambda d: d.isoformat(" "))
    INSTALLED = True
    _self_check()
    if "pandas" in sys.modules:  # 安装前就已导入（理论上 sitecustomize 场景不会）
        _patch_pandas_clock(sys.modules["pandas"])


install()


def pytest_report_header(config):
    if not ACTIVE:
        return ["clockshift: 未启用（CLOCK_SHIFT_DAYS 未设或为 0）"]
    return [f"clockshift: CLOCK_SHIFT_DAYS={_DAYS:+g} -> 假的现在 = {_RealDatetime.now() + _SHIFT:%Y-%m-%d %H:%M}"
            f"（sitecustomize 启动时安装 = {'sitecustomize' in sys.modules}）"]


EXTERNAL_CLOCK_DESELECTED = []


def pytest_collection_modifyitems(config, items):
    """`external_clock` 标记的测试拿外部真实状态（部署记录、Alpha Bot 采样……）和「现在」比：harness 挪不动
    外部世界，拨钟下它们恒红、什么也不说明 ⇒ 启用时**摘掉**（本仓约定：条件性用 marker 摘、摘要里可见，不写 skip）。"""
    if not ACTIVE:
        return
    keep, drop = [], []
    for item in items:
        (drop if item.get_closest_marker("external_clock") else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
        EXTERNAL_CLOCK_DESELECTED.extend(i.nodeid for i in drop)


def pytest_terminal_summary(terminalreporter):
    if ACTIVE:
        terminalreporter.write_line(
            f"clockshift: pandas 钟已拨 = {PANDAS_CLOCK_PATCHED}；补进子进程 env 的次数 = {CHILD_ENV_PATCHES}；"
            f"摘掉的 external_clock 测试 {len(EXTERNAL_CLOCK_DESELECTED)} 条")
        for nodeid in EXTERNAL_CLOCK_DESELECTED:
            terminalreporter.write_line(f"clockshift:   - {nodeid}")
