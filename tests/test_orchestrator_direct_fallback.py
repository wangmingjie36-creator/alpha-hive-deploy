"""编排器「退回直连」必须真的是直连（v0.45.496）。

2026-10-12：代理探活失败 ⇒ 编排器 `unset HTTPS_PROXY…` 并写「本轮退回直连」。但 macOS 上
`urllib.request.getproxies()` 在环境变量为空时经 `_scproxy` 读**系统代理**（Clash Verge 开着
「系统代理」⇒ 127.0.0.1:7897），requests 同源 ⇒ 扫描整小时仍走那个坏掉的代理，所有 HTTPS 主机
`SSLEOFError`，期权快照 0 份。修法：退回直连时 `no_proxy='*'`。

本文件把仓库编排器里**原样**的代理段抽出来用 bash 跑（不另抄一份逻辑），探活对准一个必然拒连的
本机端口（127.0.0.1:9 ⇒ socket connect 就失败，不出网），再在跑完后的环境里起解释器看它解析出什么。

为什么不只在 macOS 上断言 `getproxies()`：Linux / CI 上没有系统代理，修前修后 `getproxies()` 都是空，
那条断言在那里恒真。平台无关的不变式是「环境变量里有 no_proxy=* 且没有 http/https 代理」——
`getproxies_environment()` 非空 ⇒ macOS 不再回落到系统配置；bypass 对任意主机为真。
macOS 上额外断言真实的 `getproxies()`（本机开着系统代理时修前会红）。
"""

import os
import subprocess
import sys

import pytest

from tests._orchestrator import repo_orchestrator_text

_START = 'PROXY_URL="${ALPHA_HIVE_PROXY:-http://127.0.0.1:7897}"'
_END = "unset _proxy_ok\n"

# 跑完代理段后在同一环境里探一次：环境变量层 / 真实 getproxies / bypass / requests
_PROBE = r'''
import json, os, sys, urllib.request
out = {
    "env": {k: v for k, v in os.environ.items() if "proxy" in k.lower()},
    "env_proxies": urllib.request.getproxies_environment(),
    "proxies": urllib.request.getproxies(),
    "bypass": bool(urllib.request.proxy_bypass_environment("cdn.cboe.com")),
}
try:
    import requests
    out["requests"] = requests.utils.get_environ_proxies("https://cdn.cboe.com/")
except ImportError:
    out["requests"] = None
print("PROBE=" + json.dumps(out))
'''


def _proxy_block() -> str:
    text = repo_orchestrator_text()
    i = text.index(_START)
    j = text.index(_END, i) + len(_END)
    return text[i:j]


def _run(proxy_setting: str, extra_env=None):
    script = (
        "set -uo pipefail\n"
        'log() { echo "[$1] ${*:2}"; }\n'
        + _proxy_block()
        + '\n"$PYTHON3" -c "$PROBE_SRC"\n'
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHON3": sys.executable,
        "PROBE_SRC": _PROBE,
        "ALPHA_HIVE_PROXY": proxy_setting,
        # v0.45.504：探活失败会间隔重探（缺省 4 次 × 60s）；测试对准拒连端口，间隔置 0 免得白等 3 分钟
        "ALPHA_HIVE_PROXY_PROBE_INTERVAL": "0",
    }
    # 用户 site（numpy / requests 等）按 HOME 解析，conftest 钉的 PYTHONUSERBASE 要带上
    for k in ("PYTHONUSERBASE",):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update(extra_env or {})
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    import json
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("PROBE=")]
    assert lines, f"代理段没跑到探针：rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(lines[-1][len("PROBE="):]), r.stdout


# 终端里手工跑时 shell 带着代理变量 —— 退回直连必须把它们也清掉
_TERMINAL_ENV = {"HTTPS_PROXY": "http://127.0.0.1:7897", "http_proxy": "http://127.0.0.1:7897",
                 "ALL_PROXY": "socks5://127.0.0.1:7897"}


@pytest.mark.parametrize("setting,label", [
    ("http://127.0.0.1:9", "探活失败"),
    ("none", "显式关闭"),
])
@pytest.mark.parametrize("extra", [None, _TERMINAL_ENV], ids=["launchd", "terminal"])
def test_direct_fallback_is_really_direct(setting, label, extra):
    probe, stdout = _run(setting, extra)
    # 平台无关：环境变量层只剩 no_proxy=*（⇒ macOS 不再回落到系统代理），对任意主机 bypass
    # （getproxies_environment 把**任何** *_proxy 变量都收进来，ALPHA_HIVE_PROXY 会成为 'alpha_hive'
    #  这个协议 —— 无害，所以只看真会被用到的几个键）
    env_p = probe["env_proxies"]
    assert env_p.get("no") == "*", (label, env_p)
    assert not {k for k in env_p if k in ("http", "https", "all")}, (label, env_p)
    assert probe["bypass"] is True, label
    if probe["requests"] is not None:
        assert probe["requests"] == {}, (label, probe["requests"])
    # 真实解析结果：不得有 http / https（macOS 上开着系统代理时，修前这条红）
    assert not {k for k in probe["proxies"] if k in ("http", "https", "all")}, (label, probe["proxies"])
    # 自证没报错 —— ERROR 行只该在「清了还解析出代理」时出现
    assert "[ERROR]" not in stdout, stdout
    if setting == "none":
        assert "显式关闭" in stdout
    else:
        assert "探活失败" in stdout and "no_proxy=*" in stdout


def test_self_check_goes_red_when_proxy_still_resolves(tmp_path):
    """自证本身有牙：解释器在 no_proxy=* 下仍报告代理时，必须打 ERROR（不能只写「退回直连」）。"""
    fake = tmp_path / "fakepy"
    # 假解释器：探活一律失败；自证脚本（含 getproxies）一律打印代理并退 1；探针照常交给真解释器
    fake.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-c" ] && [[ "$2" == *getproxies\\(\\).items* ]]; then '
        "echo \"{'https': 'http://127.0.0.1:7897'}\"; exit 1; fi\n"
        'if [ "$1" = "-c" ] && [[ "$2" == *socket.socket* ]]; then exit 1; fi\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    fake.chmod(0o755)
    _, stdout = _run("http://127.0.0.1:9", {"PYTHON3": str(fake)})
    assert "[ERROR]" in stdout and "并非直连" in stdout, stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="系统代理回落只存在于 macOS 的 _scproxy")
def test_bare_unset_is_not_direct_when_system_proxy_on():
    """负对照：只 unset、不设 no_proxy 时，本机若开着系统代理，Python 仍会走它 ——
    证明上面那组断言在本机测的是真问题，不是恒真。系统代理没开就没有可对照的东西。"""
    import urllib.request
    sys_proxies = urllib.request.getproxies_macosx_sysconf()
    if not {k for k in sys_proxies if k in ("http", "https")}:
        pytest.skip("本机系统代理未开启，无从构造负对照")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    r = subprocess.run([sys.executable, "-c",
                        "import urllib.request;print(sorted(urllib.request.getproxies()))"],
                       env=env, capture_output=True, text=True, timeout=30)
    assert "https" in r.stdout, r.stdout
