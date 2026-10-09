"""生成 macOS 的 `Alpha Bot.app`：`/usr/local/bin/python3 -m alphabot.macos_app [--dest DIR] [--home 数据根] [--repo 代码目录]`
（或 `make alphabot-app`）。

.app 只是个壳：`Contents/MacOS/AlphaBot` 是一段 bash，`cd` 到代码目录后
`exec /usr/local/bin/python3 -m alphabot.launcher`——逻辑全在 `alphabot/launcher.py`，
所以代码目录更新之后，下次启动 .app 自动用上新代码，**不必重新生成**；只有代码目录换了、或换了 Python 才要重跑。

代码目录缺省是**生产克隆** `~/alpha-hive-prod`（数据根迁移阶段 8，v0.45.436；见 `default_repo`）：它由扫描前的
`production_sync` 快进。克隆不在就报错、不退回本检出（v0.45.439）。代码目录不是本检出时，.app 由**那份代码自己的
生成器**生成（`build_app_from`）：壳、Info.plist 版本号与将执行的代码同版，从哪个检出跑本命令结果都一样。

默认装到 `~/Applications`，不装进仓库：开发检出在 iCloud 同步的「桌面」下，.app 放那里会被复制出
`Alpha Bot 2.app` 这类副本（CLAUDE.md「重名副本」一节）。未签名：本机生成的文件没有隔离标记，
Gatekeeper 不拦；代码目录若在「桌面」下（`--repo` 指过去），第一次运行时 macOS 会问一次是否允许访问「桌面」文件夹，选允许。
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from alphabot import __version__

APP_NAME = "Alpha Bot"
BUNDLE_ID = "local.alphahive.alphabot"
EXECUTABLE = "AlphaBot"
DEFAULT_PYTHON = "/usr/local/bin/python3"      # CLAUDE.md 硬规则：禁用裸 python3
DEFAULT_OSASCRIPT = "/usr/bin/osascript"


class BuildError(Exception):
    pass


def _repo_root() -> Path:
    # 代码锚点：.app 要 cd 进去的就是「代码在哪」
    return Path(__file__).resolve().parent.parent


def default_repo() -> Path:
    """`.app` 缺省 cd 进哪份代码 = 生产克隆 `production_clone.default_dest()`；不在就 `BuildError`。

    阶段 8（v0.45.431）起生产代码是独立克隆，只由扫描前 `production_sync` 快进；开发检出再没人快进，worktree
    随时会被删。此前缺省是「本文件所在的检出」⇒ 在开发检出 / worktree 里 `make alphabot-app` 会把 .app 无声
    指回一份会冻结（或会消失）的代码。v0.45.436 改指克隆，但克隆不在时退回本检出、只在 stderr 留一行——
    同一个形状换了个入口（v0.45.439 二次检查）。现在不退回：要让 .app 跑别的检出，显式给 `--repo`。
    """
    import production_clone              # 同在仓库根（`-m` 从仓库根跑）；调用时求值：测试换 HOME
    clone = production_clone.default_dest()
    if not (clone / "alphabot" / "launcher.py").is_file():
        raise BuildError(f"生产克隆 {clone} 不在（或缺 alphabot/launcher.py）。先在开发检出里跑 "
                         f"{DEFAULT_PYTHON} production_clone.py setup 建好它；确实要让 .app 跑别的检出，显式给 --repo")
    return clone


#: 委托给目标代码目录自己的生成器。**只依赖 `build_app(dest, python=…)`**：v0.45.390 起每一版都有这个签名，
#: 且 repo 缺省就是它自己所在的检出。别往这里加新参数——目标常是还没快进到新版的生产克隆。
_DELEGATE = ("import sys; from pathlib import Path; from alphabot import macos_app as m; "
             "print(m.build_app(Path(sys.argv[1]), python=sys.argv[2]))")


def _bundle_repo(bundle: Path) -> Optional[Path]:
    """读回 .app 启动脚本里写死的 `REPO=`；读不到 ⇒ None。"""
    try:
        text = (bundle / "Contents" / "MacOS" / EXECUTABLE).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("REPO="):
            parts = shlex.split(line[len("REPO="):])
            return Path(parts[0]) if parts else None
    return None


def build_app_from(repo: Path, dest_dir: Path, *, python: str = DEFAULT_PYTHON, timeout: float = 120.0) -> Path:
    """用 `repo` **自己的**生成器生成 .app ⇒ 启动脚本、Info.plist（含版本号）、图标与 .app 将执行的代码同版。

    v0.45.436 在开发检出 / worktree 里生成、只把 REPO 指向克隆：壳出自生成器所在检出、代码出自克隆，两版错开——
    改了壳（比如给 launcher 加参数）的 worktree 一生成，.app 就拿新壳去 exec 克隆里的旧 launcher，双击即失败；
    版本号也显示 worktree 的（v0.45.439 二次检查）。目标就是本检出时直接建，不起子进程。

    生成完读回启动脚本的 `REPO=` 核对：委托子进程若 import 到了别处的 alphabot（如 PYTHONPATH 抢先），这里红。
    """
    repo = Path(repo).expanduser().resolve()
    if repo == _repo_root():
        return build_app(dest_dir, python=python)
    if not (repo / "alphabot" / "macos_app.py").is_file():
        raise BuildError(f"{repo} 没有 alphabot/macos_app.py（不是 Alpha Hive 仓库，或分支太旧）")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}       # 目标常是生产克隆：不往里写 __pycache__
    try:
        r = subprocess.run([sys.executable, "-c", _DELEGATE, str(Path(dest_dir).expanduser()), python],
                           cwd=str(repo), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BuildError(f"{repo} 自己的生成器 {timeout:.0f} 秒没跑完") from None
    if r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()[-1:] or ["（无输出）"]
        raise BuildError(f"{repo} 自己的生成器失败（rc={r.returncode}）：{tail[0]}")
    out = r.stdout.strip().splitlines()
    bundle = Path(out[-1]) if out else None
    if bundle is None or not bundle.is_dir():
        raise BuildError(f"{repo} 自己的生成器没报出 .app 路径：{r.stdout.strip()[-200:] or '（无输出）'}")
    got = _bundle_repo(bundle)
    if got is None or got.resolve() != repo:
        raise BuildError(f"{bundle} 的启动脚本指向 {got}，不是 {repo}：委托生成用的不是目标的代码")
    return bundle


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
        # 主程序是 bash 脚本，LaunchServices 看不出架构 ⇒ Apple 芯片上按 x86_64（Rosetta）起，子进程随之
        # 优先 x86_64 ⇒ 通用版 python3 也跑成 x86_64，载不了只有 arm64 的 numpy（用户 site，实测）。Intel 机上自动退回 x86_64
        "LSArchitecturePriority": ["arm64", "x86_64"],
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.finance",
    }


def _cd_failure_hint(repo: Path) -> str:
    """进不去代码目录时弹窗里的处理办法——按代码目录**在哪**给，不写死某一种。

    v0.45.436 起缺省代码目录是生产克隆（不在「桌面」下），弹窗却仍只说「打开『桌面文件夹』权限」「重跑
    make alphabot-app」：前者与克隆无关，后者在克隆丢了时只会报错（v0.45.439 二次检查）。
    """
    hint = (f"代码目录是生产克隆就在开发检出里跑 {DEFAULT_PYTHON} production_clone.py setup 重建它"
            "（每日扫描也靠它，丢了扫描同样停）；代码目录换了位置，就用 --repo 指向新位置重跑 make alphabot-app。")
    if _under_desktop_or_documents(repo):    # 只有这两处受 TCC 单独管控（iCloud 同步的也正是这两处）
        hint += "若是权限问题：系统设置 → 隐私与安全性 → 文件与文件夹 → Alpha Bot → 打开「桌面文件夹」/「文稿文件夹」。"
    return hint


def launch_script(repo: Path, python: str, osascript: str = DEFAULT_OSASCRIPT) -> str:
    """路径一律 `shlex.quote`：代码目录可能带空格（开发检出就在 `~/Desktop/Alpha Hive`）。

    `osascript` 只给测试换：真的那个弹的是模态对话框，测试会卡到 pytest-timeout，
    被杀的只是 bash，对话框留在屏幕上（Mac 实测）。
    """
    q_repo, q_py, q_osa = shlex.quote(str(repo)), shlex.quote(python), shlex.quote(osascript)
    cd_hint = _cd_failure_hint(repo)          # 进双引号 bash 字符串：不许含 " $ `（守卫 TestLaunchScriptHints）
    return f"""#!/bin/bash
# Alpha Bot.app 启动脚本——由 alphabot/macos_app.py 生成（v{__version__}）。
# 别手改：改生成器后重跑 make alphabot-app。逻辑在 alphabot/launcher.py，代码目录更新后下次启动即生效。
REPO={q_repo}
PY={q_py}
export LANG="${{LANG:-en_US.UTF-8}}"   # 从 Finder / Dock 启动没有 LANG：中文日志与对话框都按 UTF-8
LOG_DIR="$HOME/Library/Logs/Alpha Bot"
mkdir -p "$LOG_DIR"
alert() {{
  {q_osa} -e 'on run argv' -e 'activate' -e 'display alert "Alpha Bot 无法启动" message (item 1 of argv) as critical' -e 'end run' "$1" >/dev/null 2>&1
  echo "$(date '+%F %T') $1" >>"$LOG_DIR/launcher.log"
}}
if [ ! -x "$PY" ]; then
  alert "找不到 ${{PY}}——Alpha Bot 与每日扫描、MCP 用的是同一个解释器。装回它即可；要换解释器：先用新解释器 -m pip install -r requirements.txt，再 make alphabot-app PYTHON=新路径（没装依赖的解释器起不了服务）"
  exit 1
fi
if ! cd "$REPO" 2>/dev/null; then
  alert "进不去代码目录 ${{REPO}}。{cd_hint}"
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


def build_app(dest_dir: Path, *, repo: Optional[Path] = None, python: str = DEFAULT_PYTHON,
              osascript: str = DEFAULT_OSASCRIPT) -> Path:
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
        exe.write_text(launch_script(repo, python, osascript), encoding="utf-8")
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


def _under_desktop_or_documents(path: Path) -> bool:
    """在「桌面」/「文稿」下：iCloud「桌面与文稿」同步会复制出「xxx 2」副本，TCC 也单独管这两处的访问权限。"""
    home = Path.home()
    try:
        rel = Path(path).expanduser().resolve().relative_to(home)
    except ValueError:
        return False
    return rel.parts[:1] in (("Desktop",), ("Documents",))


def _record_data_root(explicit_home: Optional[str]) -> int:
    """处理启动器配置里的数据根，并说清楚**双击时**会发生什么（判断与启动器同源：`launcher.config_state`）。

    只有 `--home` 改写已有配置。环境里的 `ALPHA_HIVE_HOME` 只在配置还没有数据根时顺手记下：开发 shell 常把它
    指向沙箱，v0.45.436 前无条件写入 ⇒ 在开发检出里重新生成一次，生产 Alpha Bot 就改读沙箱（v0.45.439 二次检查）。
    配置文件坏了：照实报，不改写（双击时启动器会弹同一个错）。
    """
    from alphabot import launcher
    try:
        cfg = launcher.load_config()
    except launcher.LauncherError as exc:
        print(f"⚠️ {exc}", file=sys.stderr)
        return 1 if explicit_home else 0
    env_home = os.environ.get("ALPHA_HIVE_HOME")
    state, home = launcher.config_state(cfg)

    want, why = None, ""
    if explicit_home:
        want, why = explicit_home, "--home"
    elif env_home and state == "first":
        want, why = env_home, "环境 ALPHA_HIVE_HOME（配置里还没有数据根）"
    elif env_home and (home is None or Path(env_home).expanduser().resolve() != Path(home).expanduser().resolve()):
        print(f"环境里的 ALPHA_HIVE_HOME={env_home} 与启动器配置不同，没有改写配置；真要换用 --home", file=sys.stderr)

    if want:
        hp = Path(want).expanduser()
        if not hp.is_dir():
            print(f"⚠️ 数据根 {hp}（来自 {why}）不存在，没写进配置", file=sys.stderr)
        else:
            for k in launcher.RESET_KEYS:        # 连同 demo 一起换掉：demo 优先于数据根，只写数据根等于没换
                cfg.pop(k, None)
            cfg["alpha_hive_home"] = str(hp.resolve())
            print(f"数据根 {cfg['alpha_hive_home']}（来自 {why}）→ {launcher.save_config(cfg)}")
            state, home = launcher.config_state(cfg)

    print({
        "home": f"沿用启动器配置里的数据根 {home}",
        "demo": f"启动器配置是演示模式（demo: true）：双击直接进演示；换真实数据先 {DEFAULT_PYTHON} -m alphabot.launcher --reset",
        "stale": f"⚠️ 启动器配置里的数据根 {home} 不存在了：双击时会要你重选（或用 --home 指定）",
        "first": "还没有数据根（--home / ALPHA_HIVE_HOME 都没给）：首次双击时会让你选一次",
    }[state])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alphabot.macos_app", description="生成 macOS 的 Alpha Bot.app")
    ap.add_argument("--dest", default="~/Applications", help="装到哪个目录（缺省 ~/Applications）")
    ap.add_argument("--python", default=DEFAULT_PYTHON, help=f"缺省 {DEFAULT_PYTHON}")
    ap.add_argument("--home", default=None,
                    help="改写启动器配置里的数据根。不给时：配置还没有数据根才顺手记下当前环境的 ALPHA_HIVE_HOME，"
                         "已有的绝不改写；都没有就首次双击时再选")
    ap.add_argument("--repo", default=None,
                    help="`.app` cd 进哪份代码（由那份代码自己的生成器出壳）；缺省生产克隆 ~/alpha-hive-prod，"
                         "不在就报错。只在测自己的检出时给")
    args = ap.parse_args(argv)

    dest = Path(args.dest).expanduser()
    if _under_desktop_or_documents(dest):
        print(f"⚠️ {dest} 在 iCloud 同步范围内，可能被复制出「Alpha Bot 2.app」；建议用缺省的 ~/Applications",
              file=sys.stderr)
    try:
        repo = Path(args.repo).expanduser().resolve() if args.repo else default_repo().resolve()
        bundle = build_app_from(repo, dest, python=args.python)
    except BuildError as exc:
        print(f"alphabot.macos_app: {exc}", file=sys.stderr)
        return 1
    print(f"已生成 {bundle}（代码目录 {repo}，壳与 Info.plist 出自同一份代码）")

    rc = _record_data_root(args.home)
    if sys.platform != "darwin":
        print("（当前不是 macOS：.app 生成了，但只能在 Mac 上双击运行）", file=sys.stderr)
    print("用法：双击打开；拖到 Dock 常驻。停止服务：页面底部「停止服务」。换数据根："
          f"{DEFAULT_PYTHON} -m alphabot.launcher --reset")
    return rc


if __name__ == "__main__":
    sys.exit(main())
