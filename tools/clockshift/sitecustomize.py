"""每个 Python 进程启动时自动执行（`PYTHONPATH` 含本目录时）——拨钟随环境走进每个子进程。

只在 `CLOCK_SHIFT_DAYS` 非 0（或 `CLOCK_SHIFT_FORCE=1`）时安装 `clockshift`；否则什么都不做。
之后把 `sys.path` 上排在本目录之后的那个 `sitecustomize`（若有，如发行版自带的）照常执行，不吞掉它。
"""
import os as _os
import sys as _sys

if (_os.environ.get("CLOCK_SHIFT_DAYS") or "0") not in ("0", "0.0", "") or _os.environ.get("CLOCK_SHIFT_FORCE") == "1":
    import clockshift  # noqa: F401  (导入即安装)


def _chain_next_sitecustomize():
    import importlib.util
    here = _os.path.realpath(_os.path.dirname(_os.path.abspath(__file__)))
    seen_self = False
    for entry in _sys.path:
        d = _os.path.realpath(entry or _os.getcwd())
        if d == here:
            seen_self = True
            continue
        if not seen_self:
            continue
        cand = _os.path.join(d, "sitecustomize.py")
        if _os.path.isfile(cand):
            spec = importlib.util.spec_from_file_location("_chained_sitecustomize", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return


_chain_next_sitecustomize()
