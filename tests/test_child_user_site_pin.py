"""守卫 conftest 的 `_pin_child_user_site`（v0.45.426）：测试把 HOME 指到 tmp，子进程里真 Python 的用户 site 不跟着挪。

谁会红：删掉那个夹具 ⇒ `test_sandbox_home_keeps_child_user_site` 在**任何机器**上红（含 CI）——
沙箱 HOME 下推出的用户 site 必然是另一个路径，不靠「依赖恰好装在用户 site」。
它守的那类 bug 本身只在 Mac 红（v0.45.397：真起的 Alpha Bot 服务 `No module named 'starlette'`），
CI 依赖在系统 site、永远绿——所以要一条在哪都红的。

`test_sandbox_home_really_moves_user_site_without_pin` 是这把尺子的牙：不钉时沙箱 HOME 确实会挪走用户 site。
没有它，换到一个不按 HOME 推用户 site 的平台 / 解释器，上一条恒真也看不出来。
（外部环境若本来就设了 `PYTHONUSERBASE`，上一条在删夹具后仍绿——那里性质本来就成立，守的是性质不是写法。）
"""
from __future__ import annotations

import os
import site
import subprocess
import sys

_PROBE = "import site; print(site.getusersitepackages())"


def _child_user_site(env: dict) -> str:
    return subprocess.run([sys.executable, "-c", _PROBE], env=env,
                          capture_output=True, text=True, check=True).stdout.strip()


def test_sandbox_home_keeps_child_user_site(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _child_user_site(dict(os.environ)) == site.getusersitepackages()


def test_sandbox_home_really_moves_user_site_without_pin(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUSERBASE"}
    env["HOME"] = str(tmp_path)
    moved = _child_user_site(env)
    assert moved != site.getusersitepackages()
    assert moved.startswith(str(tmp_path)), moved
