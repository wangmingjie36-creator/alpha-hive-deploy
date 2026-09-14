"""import 生产模块不许在数据根下建任何东西（v0.45.233）。

仓库根「默认拒绝」总闸（conftest）在全套里抓到的两处，都是 import 期的 mkdir：

- `config.py` 的 `CACHE_CONFIG["cache_dir"]` 调了会顺手 mkdir 的 `PATHS.cache_dir` getter；
- `earnings_watcher.py` 模块级 `CACHE_DIR.mkdir()`。

二者发生在 pytest 收集期、或 conftest 里按名字排在 `_isolate_env` 前面的 autouse fixture
（`_block_same_day_macro` / `_fast_yfinance_limiter`——pytest 按字母序登记 conftest 的 fixture）里，
那时 `ALPHA_HIVE_HOME` 还没设 ⇒ 建进 checkout 根。这类问题在已有 `cache/` 的 checkout
（生产、跑过测试的 worktree）里**看不见**，只在干净克隆里现形。

⚠️ 必须用子进程：本进程里这两个模块早在收集期就 import 过了，再 import 是空操作，
测不到 import 期副作用——在进程内写这条测试会恒绿。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _fresh_import(tmp_path: Path, code: str, extra_env: dict | None = None) -> dict:
    home = tmp_path / "data_root"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALPHA_HIVE_")}
    env.update({
        "ALPHA_HIVE_HOME": str(home),
        "ALPHA_HIVE_LOGS_DIR": str(tmp_path / "logs"),   # 日志句柄 import 期就建目录，放到数据根外面
        "PYTHONPATH": str(_ROOT),
    })
    env.update(extra_env or {})
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    out["home"] = str(home)
    return out


@pytest.mark.parametrize("module", ["config", "earnings_watcher"])
def test_import_leaves_data_root_empty(tmp_path, module):
    got = _fresh_import(tmp_path, (
        "import importlib, json, os\n"
        f"importlib.import_module({module!r})\n"
        "print(json.dumps({'listing': sorted(os.listdir(os.environ['ALPHA_HIVE_HOME']))}))\n"))
    assert got["listing"] == [], f"import {module} 在数据根下建了：{got['listing']}"


@pytest.mark.parametrize("cache_env", [None, "elsewhere_cache"])
def test_config_cache_dir_value_matches_paths_getter(tmp_path, cache_env):
    """去掉 mkdir 不许改值：与 `PATHS.cache_dir` 的取值规则逐字相同（含 `ALPHA_HIVE_CACHE_DIR` 覆盖）。"""
    extra = {"ALPHA_HIVE_CACHE_DIR": str(tmp_path / cache_env)} if cache_env else None
    got = _fresh_import(tmp_path, (
        "import json, os, config\n"
        "frozen = config.CACHE_CONFIG['cache_dir']\n"
        "existed = os.path.exists(frozen)\n"
        "from hive_logger import PATHS\n"
        "print(json.dumps({'frozen': frozen, 'existed_after_import': existed, 'getter': str(PATHS.cache_dir)}))\n"),
        extra)
    assert got["frozen"] == got["getter"]
    assert got["existed_after_import"] is False
    expected = str(tmp_path / cache_env) if cache_env else str(Path(got["home"]) / "cache")
    assert got["frozen"] == expected


def test_earnings_cache_dir_still_under_home(tmp_path):
    got = _fresh_import(tmp_path, (
        "import json, earnings_watcher as ew\n"
        "print(json.dumps({'cache_dir': str(ew.CACHE_DIR)}))\n"))
    assert got["cache_dir"] == str(Path(got["home"]) / "earnings_cache")
