"""Alpha Bot.app 双击后跑的就是这里：`/usr/local/bin/python3 -m alphabot.launcher [--reset]`。

流程（每一步失败都弹窗说清楚，并写 `~/Library/Logs/Alpha Bot/launcher.log`——「谁会红」：弹窗 + 日志）：
  1. 探测端口：已经是 Alpha Bot ⇒ 直接开浏览器，不重复起服务；被别的程序占着 ⇒ 弹窗，退出。
  2. 定数据根：读 `~/Library/Application Support/Alpha Bot/launcher.json` 的 `alpha_hive_home`；
     没有（首次启动）或目录不存在 ⇒ 弹窗让用户选一次目录（或本次用演示模式），选了才写回配置。
     GUI 程序拿不到 shell 里 export 的 `ALPHA_HIVE_HOME`，所以这一步不能省——省了就会静默读错目录。
  3. 后台起 `python -m alphabot`（脱离启动器进程组，启动器退出后服务继续跑），日志进 `server.log`；
     等 `/api/ping` 应答（服务进程先死了 ⇒ 弹窗附日志尾巴，不干等到超时）。
  4. 开浏览器。停止服务：页面底部「停止服务」，或 `--stop`。

启动器自己的配置 / 日志在 `~/Library`（按调用时的 `$HOME` 求值），与 Alpha Hive 数据根无关：
数据根恰恰是它要去选的东西。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

APP_NAME = "Alpha Bot"
DEFAULT_PORT = 8765
HOST = "127.0.0.1"
READY_TIMEOUT_SEC = 90.0
SERVER_LOG_MAX_BYTES = 5 * 1024 * 1024


class LauncherError(Exception):
    """要弹给用户看的失败（消息就是弹窗正文）。"""


# ── 路径（调用时求值：测试改 HOME 即隔离）────────────────────────────────

def support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_NAME


def log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / APP_NAME


def config_path() -> Path:
    return support_dir() / "launcher.json"


def _repo_root() -> Path:
    # 代码锚点（要的就是「代码在哪」），`__file__` 才对；放函数里，不在 import 期冻结
    return Path(__file__).resolve().parent.parent


# ── 配置 ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 不回退成空配置：那会让用户莫名其妙又被问一遍目录，而坏文件还在
        raise LauncherError(f"启动器配置读不了：{p}\n{exc}\n\n删掉该文件后重新打开 Alpha Bot 即可重新选择数据目录。")
    if not isinstance(cfg, dict):
        raise LauncherError(f"启动器配置格式不对（应为 JSON 对象）：{p}")
    return cfg


def save_config(cfg: dict) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ── 探测 ────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float):
    # 不走代理：用户环境里的 http_proxy 会把回环请求也送去代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(port: int = DEFAULT_PORT, timeout: float = 2.0) -> dict:
    """`{"state": "free" | "alphabot" | "other", "info": ..., "detail": ...}`。"""
    base = f"http://{HOST}:{port}"
    try:
        info = _get_json(base + "/api/ping", timeout)
        if isinstance(info, dict) and info.get("app") == "alphabot":
            return {"state": "alphabot", "info": info}
        return {"state": "other", "detail": "端口有应答，但不是 Alpha Bot"}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 0.45.388 的服务没有 /api/ping：认 /api/meta（它停不了，只能开页面）
            try:
                meta = _get_json(base + "/api/meta", timeout)
                if isinstance(meta, dict) and meta.get("app") == "Alpha Bot":
                    return {"state": "alphabot", "info": {"app": "alphabot", "version": meta.get("version"),
                                                          "demo": meta.get("demo"), "can_shutdown": False}}
            except Exception:  # noqa: BLE001 —— 认不出来就按「别的程序」报，下面有明确文案
                pass
        return {"state": "other", "detail": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError):
            return {"state": "free"}
        return {"state": "other", "detail": str(reason)}
    except ConnectionRefusedError:
        return {"state": "free"}
    except (TimeoutError, OSError, ValueError) as exc:
        return {"state": "other", "detail": f"{type(exc).__name__}: {exc}"}


# ── 弹窗 / 浏览器（macOS 走 osascript；测试注入假的）────────────────────────

class MacUI:
    """全部文本经 argv 传给 AppleScript，不拼进脚本源码（路径里的引号 / 反斜杠不会破坏脚本）。"""

    def _osa(self, lines, *argv) -> Optional[str]:
        cmd = ["/usr/bin/osascript"]
        for ln in ["on run argv", *lines, "end run"]:
            cmd += ["-e", ln]
        r = subprocess.run(cmd + [str(a) for a in argv], capture_output=True, text=True)
        if r.returncode != 0:          # 用户点了取消（-128）或关掉了对话框
            return None
        return r.stdout.strip()

    def alert(self, message: str) -> None:
        self._osa(['display alert "Alpha Bot" message (item 1 of argv) as critical'], message)

    def ask(self, message: str, buttons, default: str) -> Optional[str]:
        """按钮最多 3 个；返回被点的按钮文字，取消 / 关窗 ⇒ None。"""
        refs = ", ".join(f"item {i + 2} of argv" for i in range(len(buttons)))
        di = buttons.index(default) + 2
        out = self._osa([f'set r to display dialog (item 1 of argv) with title "Alpha Bot" '
                         f'buttons {{{refs}}} default button (item {di} of argv) with icon note',
                         'return button returned of r'], message, *buttons)
        return out or None

    def choose_folder(self, prompt: str) -> Optional[str]:
        out = self._osa(['POSIX path of (choose folder with prompt (item 1 of argv) '
                         'default location (path to home folder))'], prompt)
        return out or None

    def open_url(self, url: str) -> None:
        subprocess.run(["/usr/bin/open", url], check=False)


# ── 数据根 ──────────────────────────────────────────────────────────────

CHOOSE, DEMO, CANCEL = "选择数据目录…", "演示模式", "取消"


def resolve_mode(cfg: dict, ui) -> Optional[dict]:
    """→ `{"demo": True}` / `{"demo": False, "home": 路径}`；用户取消 ⇒ None。选了新目录会写回配置。"""
    if cfg.get("demo") is True:
        return {"demo": True}
    home = cfg.get("alpha_hive_home")
    if home and Path(home).is_dir():
        return {"demo": False, "home": str(Path(home))}
    if home:
        msg = (f"配置里的 Alpha Hive 数据根不存在了：\n{home}\n\n"
               "重新选择数据目录（与编排器 / MCP 用的 ALPHA_HIVE_HOME 同一个），或本次用演示模式。")
    else:
        msg = ("首次启动：请选择 Alpha Hive 数据根目录（与编排器 / MCP 用的 ALPHA_HIVE_HOME 同一个）。"
               "\n\n只选一次，之后记住。也可以先用演示模式（合成数据）看看。")
    while True:
        choice = ui.ask(msg, [CANCEL, DEMO, CHOOSE], CHOOSE)
        if choice == DEMO:
            return {"demo": True}
        if choice != CHOOSE:
            return None
        picked = ui.choose_folder("选择 Alpha Hive 数据根（ALPHA_HIVE_HOME）")
        if not picked:
            continue
        picked = str(Path(picked).expanduser())      # 规整尾斜杠等（osascript 的 POSIX path 带 /）
        # 不在这里核对目录内容：卖权账本目录名只许卖权模块提及（tests/test_sell_strike_integration.py 火墙）；
        # 选错了，页面的账本 / 结果页会显示账本不存在，`--reset` 后重选
        save_config({**cfg, "alpha_hive_home": picked})
        return {"demo": False, "home": picked}


# ── 起服务 ──────────────────────────────────────────────────────────────

def _open_server_log() -> tuple:
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "server.log"
    if p.exists() and p.stat().st_size > SERVER_LOG_MAX_BYTES:
        os.replace(p, p.with_name("server.log.1"))
    return p, open(p, "ab")


def _tail(p: Path, n: int = 12) -> str:
    try:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "（日志读不到）"


def spawn_server(mode: dict, port: int, *, python: Optional[str] = None) -> tuple:
    cmd = [python or sys.executable, "-m", "alphabot", "--port", str(port)] + (["--demo"] if mode["demo"] else [])
    env = dict(os.environ)
    env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")      # 子进程再 spawn python 也走 3.11（CLAUDE.md）
    env["PYTHONUNBUFFERED"] = "1"
    if not mode["demo"]:
        env["ALPHA_HIVE_HOME"] = mode["home"]
    log_path, fh = _open_server_log()
    with fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动：{' '.join(cmd)}"
                 f"{'' if mode['demo'] else '  ALPHA_HIVE_HOME=' + mode['home']}\n".encode("utf-8"))
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=str(_repo_root()), env=env, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    return proc, log_path


def wait_ready(proc, port: int, log_path: Path, timeout: float = READY_TIMEOUT_SEC) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = probe(port, timeout=1.0)
        if p["state"] == "alphabot":           # 先看端口：双击两次时另一份可能已经起来了
            return p
        rc = proc.poll()
        if rc is not None:
            raise LauncherError(f"Alpha Bot 服务启动失败（退出码 {rc}）。\n\n日志 {log_path} 末尾：\n{_tail(log_path)}")
        time.sleep(0.3)
    raise LauncherError(f"等了 {int(timeout)} 秒服务还没应答（进程 {proc.pid} 仍在跑）。\n\n日志：{log_path}\n{_tail(log_path)}")


def stop_running(port: int) -> bool:
    """`--stop`：经 `/api/shutdown` 停掉本机 Alpha Bot；没在跑 ⇒ False。"""
    p = probe(port)
    if p["state"] != "alphabot":
        return False
    if not p["info"].get("can_shutdown"):
        raise LauncherError(f"端口 {port} 上的 Alpha Bot（{p['info'].get('version')}）不支持远程停止——"
                            "它是从终端起的旧版本，去那个终端 Ctrl-C。")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://{HOST}:{port}/api/shutdown", data=b"", method="POST",
                                 headers={"X-AlphaBot": "1"})
    with opener.open(req, timeout=5):
        pass
    return True


# ── 入口 ────────────────────────────────────────────────────────────────

def run(ui, *, port: Optional[int] = None, python: Optional[str] = None) -> int:
    cfg = load_config()
    port = int(port or cfg.get("port") or DEFAULT_PORT)
    url = f"http://{HOST}:{port}/"
    p = probe(port)
    if p["state"] == "alphabot":
        _log(f"已在运行（{p['info'].get('version')}{'，演示' if p['info'].get('demo') else ''}）⇒ 只开页面")
        ui.open_url(url)
        return 0
    if p["state"] == "other":
        raise LauncherError(f"端口 {port} 被别的程序占着（{p.get('detail')}）。\n\n"
                            f"查是谁：终端里 lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
                            f"或在 {config_path()} 里改 \"port\"。")
    mode = resolve_mode(cfg, ui)
    if mode is None:
        _log("用户取消")
        return 0
    proc, log_path = spawn_server(mode, port, python=python)
    _log(f"起服务 pid={proc.pid} {'演示模式' if mode['demo'] else 'ALPHA_HIVE_HOME=' + mode['home']}")
    wait_ready(proc, port, log_path)
    ui.open_url(url)
    return 0


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def main(argv=None, ui=None) -> int:
    ap = argparse.ArgumentParser(prog="alphabot.launcher", description="Alpha Bot.app 的启动逻辑")
    ap.add_argument("--reset", action="store_true", help="忘掉已选的数据根（下次双击重新选）")
    ap.add_argument("--stop", action="store_true", help="停掉本机正在跑的 Alpha Bot")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    ui = ui or MacUI()
    if args.reset:
        cfg = load_config()
        cfg.pop("alpha_hive_home", None)
        print(f"已清除数据根：{save_config(cfg)}")
        return 0
    if args.stop:
        try:
            stopped = stop_running(int(args.port or load_config().get("port") or DEFAULT_PORT))
        except LauncherError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("已停止" if stopped else "没有在跑的 Alpha Bot")
        return 0
    try:
        return run(ui, port=args.port)
    except LauncherError as exc:
        _log(f"失败：{exc}")
        ui.alert(str(exc))
        return 1
    except Exception:  # noqa: BLE001 —— 双击启动没有终端，未预料的异常也必须弹出来
        tb = traceback.format_exc()
        _log(tb)
        ui.alert(f"启动器出错：\n{tb.strip().splitlines()[-1]}\n\n完整信息：{log_dir() / 'launcher.log'}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
