"""生成 macOS 的 `Alpha Bot.app`：`/usr/local/bin/python3 -m alphabot.macos_app [--dest DIR] [--home 数据根]`
（或 `make alphabot-app`）。

.app 只是个壳：`Contents/MacOS/AlphaBot` 是一段 bash，`cd` 到本仓库后
`exec /usr/local/bin/python3 -m alphabot.launcher`——逻辑全在 `alphabot/launcher.py`，
所以 `git pull` 之后 .app 自动用上新代码，**不必重新生成**；只有仓库挪了位置、或换了 Python 才要重跑。

默认装到 `~/Applications`，不装进仓库：仓库在 iCloud 同步的「桌面」下，.app 放那里会被复制出
`Alpha Bot 2.app` 这类副本（CLAUDE.md「重名副本」一节）。未签名：本机生成的文件没有隔离标记，
Gatekeeper 不拦；第一次运行时 macOS 会问一次是否允许访问「桌面」文件夹（仓库在那里），选允许。
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from alphabot import __version__

APP_NAME = "Alpha Bot"
BUNDLE_ID = "local.alphahive.alphabot"
EXECUTABLE = "AlphaBot"
DEFAULT_PYTHON = "/usr/local/bin/python3"      # CLAUDE.md 硬规则：禁用裸 python3


class BuildError(Exception):
    pass


def _repo_root() -> Path:
    # 代码锚点：.app 要 cd 进去的就是「代码在哪」
    return Path(__file__).resolve().parent.parent


def _icon_path() -> Path:
    return Path(__file__).resolve().parent / "macos" / "AlphaBot.icns"


def info_plist() -> dict:
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIconFile": "AlphaBot",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.finance",
    }


def launch_script(repo: Path, python: str) -> str:
    """路径一律 `shlex.quote`：仓库就在 `~/Desktop/Alpha Hive`，带空格。"""
    q_repo, q_py = shlex.quote(str(repo)), shlex.quote(python)
    return f"""#!/bin/bash
# Alpha Bot.app 启动脚本——由 alphabot/macos_app.py 生成（v{__version__}）。
# 别手改：改生成器后重跑 make alphabot-app。逻辑在 alphabot/launcher.py，git pull 即生效。
REPO={q_repo}
PY={q_py}
export LANG="${{LANG:-en_US.UTF-8}}"   # 从 Finder / Dock 启动没有 LANG：中文日志与对话框都按 UTF-8
LOG_DIR="$HOME/Library/Logs/Alpha Bot"
mkdir -p "$LOG_DIR"
alert() {{
  /usr/bin/osascript -e 'on run argv' -e 'activate' -e 'display alert "Alpha Bot 无法启动" message (item 1 of argv) as critical' -e 'end run' "$1" >/dev/null 2>&1
  echo "$(date '+%F %T') $1" >>"$LOG_DIR/launcher.log"
}}
if [ ! -x "$PY" ]; then
  alert "找不到 ${{PY}}（Alpha Hive 用 Homebrew 的 Python 3.11）。换了 Python 的话：make alphabot-app PYTHON=新路径"
  exit 1
fi
if ! cd "$REPO" 2>/dev/null; then
  alert "进不去代码目录 ${{REPO}}。仓库挪过位置就在新位置重跑 make alphabot-app；若是权限问题：系统设置 → 隐私与安全性 → 文件与文件夹 → Alpha Bot → 打开「桌面文件夹」。"
  exit 1
fi
if [ ! -f alphabot/launcher.py ]; then
  alert "$REPO 下没有 alphabot/launcher.py——仓库挪过位置或分支太旧。在新位置重跑 make alphabot-app。"
  exit 1
fi
export PATH="/usr/local/bin:$PATH"
exec "$PY" -m alphabot.launcher "$@" >>"$LOG_DIR/launcher.log" 2>&1
"""


def _is_ours(bundle: Path) -> bool:
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False


def build_app(dest_dir: Path, *, repo: Optional[Path] = None, python: str = DEFAULT_PYTHON) -> Path:
    """在 `dest_dir` 下生成 / 替换 `Alpha Bot.app`，返回其路径。

    已存在同名 .app 但不是本生成器产出的（bundle id 不同）⇒ 拒绝覆盖，不删别人的东西。
    先在同目录临时位置建好，再换上去：中途失败不会留下半个 .app。
    """
    dest_dir = Path(dest_dir).expanduser()
    repo = Path(repo).resolve() if repo else _repo_root()
    if not (repo / "alphabot" / "launcher.py").is_file():
        raise BuildError(f"{repo} 不是 Alpha Hive 仓库（缺 alphabot/launcher.py）")
    icon = _icon_path()
    if not icon.is_file():
        raise BuildError(f"缺图标 {icon}")
    bundle = dest_dir / f"{APP_NAME}.app"
    if bundle.exists() and not _is_ours(bundle):
        raise BuildError(f"{bundle} 已存在且不是 Alpha Bot 生成器产出的（bundle id ≠ {BUNDLE_ID}），不覆盖")
    dest_dir.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=".alphabot-app-", dir=dest_dir))
    try:
        new = staging / f"{APP_NAME}.app"
        (new / "Contents" / "MacOS").mkdir(parents=True)
        (new / "Contents" / "Resources").mkdir()
        with open(new / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump(info_plist(), f)
        (new / "Contents" / "PkgInfo").write_text("APPL????", encoding="ascii")
        exe = new / "Contents" / "MacOS" / EXECUTABLE
        exe.write_text(launch_script(repo, python), encoding="utf-8")
        exe.chmod(0o755)
        shutil.copy2(icon, new / "Contents" / "Resources" / "AlphaBot.icns")
        old = staging / "old.app"
        if bundle.exists():
            os.replace(bundle, old)
        os.replace(new, bundle)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    os.utime(bundle)                       # 让 Finder / Dock 重新读图标
    return bundle


def _icloud_risk(dest: Path) -> bool:
    home = Path.home()
    try:
        rel = dest.expanduser().resolve().relative_to(home)
    except ValueError:
        return False
    return rel.parts[:1] in (("Desktop",), ("Documents",))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alphabot.macos_app", description="生成 macOS 的 Alpha Bot.app")
    ap.add_argument("--dest", default="~/Applications", help="装到哪个目录（缺省 ~/Applications）")
    ap.add_argument("--python", default=DEFAULT_PYTHON, help=f"缺省 {DEFAULT_PYTHON}")
    ap.add_argument("--home", default=None,
                    help="顺手记下数据根（写启动器配置）；缺省取当前环境的 ALPHA_HIVE_HOME，都没有就首次双击时再选")
    args = ap.parse_args(argv)

    dest = Path(args.dest).expanduser()
    if _icloud_risk(dest):
        print(f"⚠️ {dest} 在 iCloud 同步范围内，可能被复制出「Alpha Bot 2.app」；建议用缺省的 ~/Applications",
              file=sys.stderr)
    try:
        bundle = build_app(dest, python=args.python)
    except BuildError as exc:
        print(f"alphabot.macos_app: {exc}", file=sys.stderr)
        return 1
    print(f"已生成 {bundle}")

    home = args.home or os.environ.get("ALPHA_HIVE_HOME")
    if home:
        from alphabot import launcher
        hp = Path(home).expanduser()
        if not hp.is_dir():
            print(f"⚠️ 数据根 {hp} 不存在，没写进配置；首次双击时再选", file=sys.stderr)
        else:
            try:
                cfg = launcher.load_config()
            except launcher.LauncherError as exc:
                print(f"⚠️ 数据根没写进配置：{exc}", file=sys.stderr)
                return 1
            cfg["alpha_hive_home"] = str(hp.resolve())
            print(f"数据根 {cfg['alpha_hive_home']} → {launcher.save_config(cfg)}")
    else:
        print("没给数据根（--home / ALPHA_HIVE_HOME）：首次双击时会让你选一次")
    if sys.platform != "darwin":
        print("（当前不是 macOS：.app 生成了，但只能在 Mac 上双击运行）", file=sys.stderr)
    print("用法：双击打开；拖到 Dock 常驻。停止服务：页面底部「停止服务」。换数据根："
          f"{DEFAULT_PYTHON} -m alphabot.launcher --reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
