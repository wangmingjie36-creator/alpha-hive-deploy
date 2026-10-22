"""生成 macOS 的 `Alpha Bot.app`：`/usr/local/bin/python3 -m alphabot.macos_app [--dest DIR] [--home 数据根] [--repo 代码目录]`
（或 `make alphabot-app`）。

.app 只是个壳：`Contents/MacOS/AlphaBot` 是一段 bash，`cd` 到代码目录后
`exec /usr/local/bin/python3 -m alphabot.launcher`——逻辑全在 `alphabot/launcher.py`，
所以代码目录更新之后，下次启动 .app 自动用上新代码，**不必重新生成**；只有代码目录换了、或换了 Python 才要重跑。

代码目录缺省是**生产克隆** `~/alpha-hive-prod`（数据根迁移阶段 8，v0.45.436；见 `default_repo`）：它由扫描前的
`production_sync` 快进。克隆不在就报错、不退回本检出（v0.45.439）。代码目录不是本检出时，**整次生成**交给那份代码
自己的 `main`（`run_target_generator`，v0.45.451）：壳、Info.plist 版本号、数据根处理、提示都与将执行的代码同版。
统一入口：`make -C ~/alpha-hive-prod alphabot-app`（克隆自己的 Makefile + 克隆自己的代码，与在哪个检出敲命令无关）。

默认装到 `~/Applications`，不装进仓库：开发检出在 iCloud 同步的「桌面」下，.app 放那里会被复制出
`Alpha Bot 2.app` 这类副本（CLAUDE.md「重名副本」一节）。未签名：本机生成的文件没有隔离标记，
Gatekeeper 不拦；代码目录若在「桌面」下（`--repo` 指过去），第一次运行时 macOS 会问一次是否允许访问「桌面」文件夹，选允许。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
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
    problem = generator_dir_problem(clone)
    if problem:
        raise BuildError(f"生产克隆 {clone} {problem}。先在开发检出里跑 "
                         f"{running_python_word()} production_clone.py setup 建好它；确实要让 .app 跑别的检出，显式给 --repo")
    return clone


#: 一份「能生成 .app、也能被 .app 执行」的 Alpha Hive 代码必须有的文件：生成器本身（委托要跑它）+ 壳要 exec 的 launcher
#: + 生成器在目标里 import 的仓库根模块（v0.45.461：提示取 `production_clone.default_dest`、环境数据根取
#: `hive_logger.explicit_data_root`——缺了就是委托后子进程甩 traceback，正是本判据要消灭的形状）。
#: 生成器新 import 仓库里别的模块时**加进来**；测试的假克隆按这份清单拷真文件（`_copy_generator`）。
GENERATOR_FILES = ("alphabot/__init__.py", "alphabot/macos_app.py", "alphabot/launcher.py",
                   "production_clone.py", "hive_logger.py")


def generator_dir_problem(repo: Path) -> Optional[str]:
    """「这是不是一份可用的 Alpha Hive 代码目录」的**唯一**判据 ⇒ 问题描述，没问题 ⇒ None（v0.45.458）。

    v0.45.456 前三处各写一份：缺省克隆查 launcher.py、显式 `--repo` 查 macos_app.py、`build_app` 又查 launcher.py。
    半截克隆（有其一缺其二）在缺省路径下被判可用，委托后子进程 import 失败甩 traceback——正是 `--repo` 那条预检
    要消灭的误诊形状；显式给同一个目录却被直白拒绝。缺省克隆、`--repo`、`build_app` 一律读这里。
    """
    repo = Path(repo)
    if not repo.is_dir():
        return "目录不存在"
    missing = [rel for rel in GENERATOR_FILES if not (repo / rel).is_file()]
    return f"不是 Alpha Hive 代码目录（缺 {'、'.join(missing)}）" if missing else None


#: 本模块的 `main` 能被别的检出整段委托（v0.45.451）：接受 `--repo 自己` 并在自己里一次跑完出壳 + 数据根。
#: 委托方见到目标低于这个号就拒绝，不去猜旧版的参数——别删，也别在不兼容地改 `main` 参数时忘了加一。
DELEGATE_PROTOCOL = 1
_DELEGATED_ENV = "ALPHABOT_APP_DELEGATED_FROM"   # 被委托的那次进程里有它 ⇒ 不许再委托

#: 在目标代码里跑它自己的 `main`。**不换 cwd**：调用者给的相对路径保持原意（v0.45.439 换 cwd，`--dest ./Apps`
#: 被建进了生产克隆）；import 靠显式把目标放到 `sys.path` 最前，再核对 import 到的确实是目标（不读产物文本）。
_DELEGATE = "\n".join([
    "import sys",
    "from pathlib import Path",
    "target = Path(sys.argv[1]).resolve()",
    "sys.path.insert(0, str(target))",
    "from alphabot import macos_app as m",
    "got = Path(m.__file__).resolve().parent.parent",
    "if got != target:",
    "    sys.exit(f'alphabot.macos_app: 委托 import 到的生成器在 {got}，不是 {target}（PYTHONPATH / 包重定向？）')",
    "if getattr(m, 'DELEGATE_PROTOCOL', 0) < 1:",
    "    sys.exit(f'alphabot.macos_app: {target} 的生成器太旧，不能被委托。在那份代码里直接跑：'",
    "             f'make -C {target} alphabot-app；或等它更新到 v0.45.451 之后')",
    "sys.exit(m.main(sys.argv[2:]))",
])


def run_target_generator(repo: Path, argv: list) -> int:
    """把整次生成交给 `repo` **自己的** `main`：壳、Info.plist、数据根处理、给人看的提示，全出自将要执行的那份代码。

    v0.45.439 只委托了「出壳」：数据根仍按委托方自己那版 launcher 判断（跨版本又是两份规则）、委托方去概括子进程
    的报错（stdout 一有输出就盖住真正的错误行）、生成后读壳里的 `REPO=` 文本核对（把委托方绑在目标的壳格式上）。
    根因是只交出去一半——现在整段交出去：子进程的输出原样透传、退出码原样返回，委托方不再插手（v0.45.451）。
    `argv` 里的路径必须已是绝对路径（`main` 在入口统一做）。

    子进程带 `_DELEGATED_ENV`：被委托的那次**绝不再委托**（`main` 见到它就拒）。没有这道闸时，目标若因任何原因
    认为自己不是 `--repo`（变异实测：拿掉 import 来源核对 + 包被重定向），就一层层委托下去直到被杀。
    """
    env = {**os.environ, _DELEGATED_ENV: str(repo)}
    return subprocess.run([sys.executable, "-c", _DELEGATE, str(repo), *argv], env=env).returncode


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


def _home_tilde(p: Path) -> str:
    """家目录下的路径写成 `~/…`：给人抄的命令里不嵌用户名（测试里也不嵌临时 HOME）。"""
    try:
        return "~/" + str(Path(p).relative_to(Path.home()))
    except ValueError:
        return str(p)


def clone_make(target: str) -> str:
    """给人抄的「在生产克隆上跑 make 目标」：带 `-C`，在哪个目录敲都找得到 Makefile（v0.45.612：停止服务页曾写
    不带 `-C` 的 `make alphabot`，不在代码目录里敲就 No rule to make target）。静态页抄它，守卫 `TestMakeCommandsNameTheirDirectory`。"""
    import production_clone
    return f"make -C {_home_tilde(production_clone.default_dest())} {target}"


def canonical_entry() -> str:
    """推荐入口：重新生成指向生产克隆的 .app。帮助页抄的就是它（守卫 `TestRegenHints`）。"""
    return clone_make("alphabot-app")


def _is_default_clone(repo: Path) -> bool:
    import production_clone
    return Path(repo).expanduser().resolve() == production_clone.default_dest().resolve()


#: 给人抄进终端时**不用引号**也是一个词的字符（非 ASCII 字母照常：占位「/新解释器的绝对路径」原样）；其余 ⇒ 单引号引起来
_PLAIN_WORD = re.compile(r"[^\s'\"$`\\;&|<>()*?\[\]{}!#~]+")

#: 写进 .app 的解释器还要进给人抄的命令（`regen_hint`）、壳里双引号字符串中的提示与 make 配方的 `"$(PYTHON)"`：
#: 这几个字符在那里会被 shell 解释或截断字符串（单引号：`shlex.quote` 引含单引号的词会生出双引号）。v0.45.498
PY_UNSAFE_CHARS = "\"$`\\'\n"


#: 记下的解释器不能沿用、或要教人换解释器时，命令里放的占位（给人改成实际路径；不含要引号的字符，原样进壳）
PLACEHOLDER_PY = "/新解释器的绝对路径"


def _cmd_word(s: str) -> str:
    return s if _PLAIN_WORD.fullmatch(s) else shlex.quote(s)


def _runner_word(python: str) -> str:
    """给人抄的命令里「用 .app 记下的解释器跑」：合现行规则就是它（按需加引号），否则占位（v0.45.607）。
    `regen_hint` 与「生产克隆丢了」弹窗共用，占位规则只在这里。"""
    return PLACEHOLDER_PY if python_rule_problem(python) else _cmd_word(python)


def running_python_word() -> str:
    """给人抄的命令里「用此刻这个解释器跑」= 正在跑本进程的那个（此刻必然能跑），按需加引号（v0.45.611）。

    生成时打印的提示（克隆不在、换数据根）与帮助页（经 `/api/method`）用它：此前写死缺省解释器，换解释器常常正是
    因为缺省坏了，照抄就失败。冻进壳里的文字不用它（那是生成时的环境），用 `_runner_word`（记下的那个）。
    守卫 `TestHintsNeverHardcodeTheDefaultInterpreter`。"""
    return _cmd_word(sys.executable)


def here_command(args: str) -> str:
    """在哪个目录照抄都跑得通的命令：先进本代码目录（`-m alphabot…` / `-r requirements.txt` 都要从那里跑），再用此刻的
    解释器（v0.45.611）。此前「换数据根」只写 `… -m alphabot.launcher --reset`：不在代码目录里敲就 No module named alphabot。"""
    d = _home_tilde(_repo_root())
    d = d if d.startswith("~/") and _PLAIN_WORD.fullmatch(d[2:]) else shlex.quote(str(_repo_root()))
    return f"cd {d} && {running_python_word()} {args}"


def python_rule_problem(python: str) -> Optional[str]:
    """`python` 现在能不能作为 .app 的解释器写进壳、写进给人抄的命令 ⇒ 问题描述；能 ⇒ None。

    **现行**规则只此一份（v0.45.597）：生成时 `build_app` 拒、提示时 `regen_hint` 判沿不沿用，都经这里——此前提示照搬
    壳里记下的值，而 456 之前的生成器把裸名字 / 相对路径原样写进过壳，照抄就被生成器自己拒掉。不核对存在与否
    （那是 `interpreter_problem`：缺省解释器在 CI 上本来就不存在，人也可能之后才装）。
    """
    if not os.path.isabs(python):
        return "不是绝对路径"
    bad = sorted({c for c in python if c in PY_UNSAFE_CHARS}, key=PY_UNSAFE_CHARS.index)
    if bad:
        return f"含 shell 会解释或截断的字符 {bad!r}"
    return None


def regen_hint(repo: Path, python: str) -> str:
    """「怎么重新生成 cd 进 `repo`、用 `python` 跑的那个 .app」——给人的命令**只从这里出**（v0.45.461）。

    此前四处各写一份自由文本：壳里两处还是 `make alphabot-app PYTHON=新路径`（不带 `-C`：在随便哪个目录敲都找不到
    Makefile；也不提绝对路径：照写裸名字会被 456 的规则拒），进不去代码目录那句又说「用 --repo 重跑 make」（make 不收
    `--repo`）。代码目录是生产克隆 ⇒ 推荐入口 `canonical_entry`（帮助页抄的也是它）；否则（开发时 `--repo` 生成的）⇒
    在那份代码里直接跑生成器。不嵌代码目录路径：结果要进壳里的双引号字符串。

    `python` **必填**（v0.45.498）：重新生成要沿用 .app 记下的那个解释器——等于缺省就不写（缺省命令生成的就是它），
    否则带上 `PYTHON=` / `--python`。此前它可省、省了就等于「用缺省」：487 的过时提醒、壳头注释、「代码目录挪了」那句
    都没传，照抄就把自定义解释器悄悄换回 `/usr/local/bin/python3`。要教人**换**解释器的那句传占位（`_python_fix_hint`）。

    给出的命令**照抄要跑得通**（v0.45.597，498 二次检查 #1 / #2）——此前只管「沿用」，没核「今天还跑不跑得通」：
    - 记下的解释器不合现行规则（`python_rule_problem`：456 之前的生成器把 `python3` 这类裸名字原样写进过壳）⇒ 沿用就被
      生成器拒 ⇒ 改给占位 `PLACEHOLDER_PY`，前面说明一句；那个值本身不嵌进来（可能带 shell 特殊字符，结果要进双引号）。
    - **跑生成器**的解释器 = 要写进 .app 的那个。推荐入口**不在提示里写**：Makefile 自己定——命令行上的 `PYTHON` 是可用的
      绝对路径就由它跑，否则退回缺省（v0.45.607）；所以帮助页、Makefile 注释、任何教人换解释器的地方只写 `PYTHON=X` 就
      都跑得通（600 把 `ALPHABOT_GEN_PY=X` 写进提示文字，帮助页那处就漏了）。直跑没有 make 可依，命令开头写它。此前恒为
      缺省：换解释器常常正是因为缺省坏了，照抄第一步就失败。

    ⚠️ 确定规则，不看「缺省此刻能不能用」（v0.45.600）：本函数的结果有一半是在**生成 .app 时**写进壳里的文字（头注释、
    两处弹窗），要到以后出事那天才被人看见——按生成那一刻的环境选（597），生成时缺省好好的、弹窗里就不带 runner，等缺省
    坏了照做照样失败；同一份代码在两台机器 / 两个时刻还会生成不同的壳。各出现场景：启动器的过时提醒——记下的解释器正跑着
    启动器，必然能跑；「找不到 / 执行失败」弹窗——传的是占位（新解释器，同一句已先教人用它装依赖）；头注释与「代码目录
    挪了」——壳只核过 PY 是**可执行文件**（`SHELL_PY_USABLE`，不保证真能运行），记下的是缺省而它文件在、却跑不起来，同时
    代码目录又挪了，直跑那句会失败（两件同时坏，极少见，不为它加分支）。生成器在 import 期只用标准库与仓库模块、启动器自己
    也 import 它（守卫 `TestDefaultRepoIsProductionClone::test_generator_runs_with_only_the_standard_library`，
    关掉 site-packages 真跑一次生成），能跑启动器的解释器就能跑生成器。
    """
    note = ("（.app 里记的解释器不合现行规则：须是不含引号等特殊字符的绝对路径，把命令里的占位换成实际要用的解释器）"
            if python_rule_problem(python) else "")
    runner = _runner_word(python)
    py = "" if runner == DEFAULT_PYTHON else runner
    if _is_default_clone(repo):
        return f"{note}在终端执行 {canonical_entry()}" + (f" PYTHON={py}" if py else "")
    return (f"{note}在那份代码目录里执行 {runner} -m alphabot.macos_app --repo ."
            + (f" --python {py}" if py else ""))


def _repo_unusable_hint(repo: Path, python: str) -> str:
    """进不去代码目录 / 代码目录不完整时弹窗里的处理办法——按代码目录**是什么、在哪**给，不写死某一种。

    v0.45.436 起缺省代码目录是生产克隆（不在「桌面」下），弹窗却仍只说「打开『桌面文件夹』权限」「重跑
    make alphabot-app」：前者与克隆无关，后者在克隆丢了时只会报错（v0.45.439 二次检查）。
    """
    if _is_default_clone(repo):
        hint = (f"生产克隆丢了或不完整：在开发检出里跑 {_runner_word(python)} production_clone.py setup 重建它"
                "（每日扫描也靠它，丢了扫描同样停），重建后不用重新生成 .app。")
    else:
        hint = f"代码目录挪了位置就到新位置重新生成（{regen_hint(repo, python)}）。"
    if _under_desktop_or_documents(repo):    # 只有这两处受 TCC 单独管控（iCloud 同步的也正是这两处）
        hint += "若是权限问题：系统设置 → 隐私与安全性 → 文件与文件夹 → Alpha Bot → 打开「桌面文件夹」/「文稿文件夹」。"
    return hint


#: 壳里「解释器现在能不能 exec」的判断——与 `interpreter_problem` 是**同一条**：普通文件（跟随软链）且可执行。
#: 只用 `[ -x ]` 时目录也算可执行（v0.45.461）。守卫 `TestInterpreterCheck` 拿同一批路径核对两边结论一致。
SHELL_PY_USABLE = '[ -f "$PY" ] && [ -x "$PY" ]'


def interpreter_problem(python: str) -> Optional[str]:
    """写进 .app 的解释器现在能不能 exec ⇒ 问题描述；能 ⇒ None。与壳里的 `SHELL_PY_USABLE` 同一条判据（v0.45.461）。

    v0.45.458 生成时只看 `os.access(X_OK)`、壳只看 `[ -x ]`——**目录也算可执行**：`PYTHON=/usr/local/bin` 两道都放行，
    双击时 exec 失败只进日志、不弹窗，看着就是「双击没反应」。
    """
    if not os.path.exists(python):
        return "不存在"
    if not os.path.isfile(python):
        return "不是文件（是目录？）"
    if not os.access(python, os.X_OK):
        return "没有执行权限"
    return None


def launch_script(repo: Path, python: str, osascript: str = DEFAULT_OSASCRIPT) -> str:
    """路径一律 `shlex.quote`：代码目录可能带空格（开发检出就在 `~/Desktop/Alpha Hive`）。

    `osascript` 只给测试换：真的那个弹的是模态对话框，测试会卡到 pytest-timeout，
    被杀的只是 bash，对话框留在屏幕上（Mac 实测）。

    `exec` 本身失败（格式 / 架构不对、刚被删）也要弹窗：bash 缺省直接退出、错误只进日志——双击的人什么也看不到。
    `execfail` 让脚本接着跑到下一行的 alert（v0.45.461，bash 3.2 实测：exec 的重定向随之撤销；目录退出码 126、
    二进制格式不对 1）。
    """
    q_repo, q_py, q_osa = shlex.quote(str(repo)), shlex.quote(python), shlex.quote(osascript)
    # 下面两段进双引号 bash 字符串：不许含 " $ ` \（守卫 TestLaunchScriptHints）
    repo_hint = _repo_unusable_hint(repo, python)
    py_fix = _python_fix_hint(repo)
    return f"""#!/bin/bash
# Alpha Bot.app 启动脚本——由 alphabot/macos_app.py 生成（v{__version__}）。
# 别手改：要改就重新生成（{regen_hint(repo, python)}）。逻辑在 alphabot/launcher.py，代码目录更新后下次启动即生效。
REPO={q_repo}
PY={q_py}
export LANG="${{LANG:-en_US.UTF-8}}"   # 从 Finder / Dock 启动没有 LANG：中文日志与对话框都按 UTF-8
LOG_DIR="$HOME/Library/Logs/Alpha Bot"
mkdir -p "$LOG_DIR"
alert() {{
  {q_osa} -e 'on run argv' -e 'activate' -e 'display alert "Alpha Bot 无法启动" message (item 1 of argv) as critical' -e 'end run' "$1" >/dev/null 2>&1
  echo "$(date '+%F %T') $1" >>"$LOG_DIR/launcher.log"
}}
if ! {{ {SHELL_PY_USABLE}; }}; then
  alert "找不到可执行的 ${{PY}}（不存在、是目录或没有执行权限）——Alpha Bot 与每日扫描、MCP 用的是同一个解释器。{py_fix}"
  exit 1
fi
if ! cd "$REPO" 2>/dev/null; then
  alert "进不去代码目录 ${{REPO}}。{repo_hint}"
  exit 1
fi
if [ ! -f alphabot/launcher.py ]; then
  alert "$REPO 下没有 alphabot/launcher.py。{repo_hint}"
  exit 1
fi
export PATH="/usr/local/bin:$PATH"
shopt -s execfail
exec "$PY" -m alphabot.launcher "$@" >>"$LOG_DIR/launcher.log" 2>&1
alert "执行 ${{PY}} 失败（退出码 $?，详情在 ~/Library/Logs/Alpha Bot/launcher.log）。{py_fix}"
exit 126
"""


def _python_fix_hint(repo: Path) -> str:
    """解释器找不到 / 执行失败时的处理办法（两处弹窗共用）。"""
    return ("装回它即可；要换解释器：先用新解释器 -m pip install -r requirements.txt，再"
            f"{regen_hint(repo, python=PLACEHOLDER_PY)}（没装依赖的解释器起不了服务）")


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

    这里是写进 .app 的**唯一出口**：输入规则（显式空值拒绝、解释器须绝对路径）在这里执行，不只在命令行入口
    （v0.45.461：458 只在 `main` 拒空值，`build_app(repo="")` 仍被当成当前目录——同一输入两个入口两种含义）。
    """
    dest_dir = Path(dest_dir).expanduser()
    repo = _repo_root() if _given(repo, "repo") is None else Path(repo).resolve()
    python = _must_be_absolute(python, "python")
    rule = python_rule_problem(python)          # 与提示判「能不能沿用」同一条（v0.45.597）
    if rule:
        raise BuildError(f"解释器路径 {python!r} {rule}：它要进 .app 里的提示与给人抄的重新生成命令，在那里会被 shell "
                         "解释或截断。换一个不含这些字符的路径（指过去的软链即可）")
    problem = generator_dir_problem(repo)
    if problem:
        raise BuildError(f"{repo} {problem}")
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


def _shell_assignment(script: str, var: str) -> str:
    """壳里 `VAR=<shlex 引过的值>` 那一行的值（生成器写的就是这个形状）；没有就 `BuildError`。"""
    for line in script.splitlines():
        if line.startswith(f"{var}="):
            parts = shlex.split(line[len(var) + 1:])
            if len(parts) == 1:
                return parts[0]
    raise BuildError(f"启动脚本里找不到 {var}=（不是本生成器写的壳？）")


def installed_drift(bundle: Path) -> dict:
    """已装的 .app 与「本代码按它自己记下的输入**现在**会生成的」逐项比（v0.45.487）。只读。

    ⇒ `{"repo", "python", "parts": [不同的部分], "fingerprint": 两端内容的摘要}`，`parts` 空 = 一致。
    不是本生成器产出的（bundle id 不同）/ 壳里读不出 `REPO=`、`PY=` ⇒ `BuildError`。

    为什么比内容、不比版本号：壳在生成那一刻写死，不随代码目录更新；v0.45.436~461 一路改壳，`alphabot.__version__`
    却从 09-30 起没动过——版本号判不了「壳过时」。唯一真相是生成器本身：用壳里记下的代码目录与解释器在内存里重
    生成一份，与装着的逐项比，没有第二份规则要同步。`fingerprint` 同时摘要「装着的」与「应生成的」，任一端变了就变
    （调用方据此「同一处差异只提醒一次」）。
    """
    bundle = Path(bundle)
    if not _is_ours(bundle):
        raise BuildError(f"{bundle} 不是 Alpha Bot 生成器产出的（bundle id ≠ {BUNDLE_ID}）")
    contents = bundle / "Contents"
    have_script = (contents / "MacOS" / EXECUTABLE).read_text(encoding="utf-8")
    repo, python = _shell_assignment(have_script, "REPO"), _shell_assignment(have_script, "PY")
    with open(contents / "Info.plist", "rb") as f:
        have_plist = plistlib.load(f)
    icon = contents / "Resources" / "AlphaBot.icns"
    have_icon = icon.read_bytes() if icon.is_file() else b""
    want_script, want_plist, want_icon = launch_script(Path(repo), python), info_plist(), _icon_path().read_bytes()
    parts = [name for name, have, want in (("启动脚本", have_script, want_script),
                                           ("Info.plist", have_plist, want_plist),
                                           ("图标", have_icon, want_icon)) if have != want]
    h = hashlib.sha256()
    for blob in (have_script.encode(), plistlib.dumps(have_plist), have_icon,
                 want_script.encode(), plistlib.dumps(want_plist), want_icon):
        h.update(hashlib.sha256(blob).digest())
    return {"repo": repo, "python": python, "parts": parts, "fingerprint": h.hexdigest()}


def _under_desktop_or_documents(path: Path) -> bool:
    """在「桌面」/「文稿」下：iCloud「桌面与文稿」同步会复制出「xxx 2」副本，TCC 也单独管这两处的访问权限。"""
    home = Path.home()
    try:
        rel = Path(path).expanduser().resolve().relative_to(home)
    except ValueError:
        return False
    return rel.parts[:1] in (("Desktop",), ("Documents",))


def _must_be_absolute(value: str, what: str) -> str:
    """要**持久写下、以后在别处才解析**的路径（壳里的 `PY=`、启动器配置的数据根）：入口就必须是绝对路径（v0.45.456）。

    **不猜「相对于哪」**：`make -C 克隆` 先切了目录，程序看不到人是在哪敲的命令；裸名字 `python3` 取决于当时的
    PATH（CLAUDE.md：裸 `python3` 可能是系统 3.9）。v0.45.451 按当下 cwd / PATH 解析，换了入口就解析到别处。
    `~` 可以展开（它不依赖 cwd）。不核对存在与否：壳在双击时会核对并弹窗，缺省解释器在 CI 上本来就不存在。
    """
    p = os.path.expanduser(value)
    if not os.path.isabs(p):
        raise BuildError(f"{what} 会写进 .app / 启动器配置、以后在别的目录下才用，必须是绝对路径（收到 {value!r}）")
    return os.path.normpath(p)


def _check_generator_dir(repo: Path) -> None:
    """委托之前委托方唯一该核对的：目标**是不是一份生成器代码**——与版本无关的文件系统事实（v0.45.456）。

    v0.45.451 删 `build_app_from` 时连这条一起删了：`--repo` 拼错，子进程 import 落到调用者 cwd 里的那份 alphabot，
    报成「import 被重定向 / PYTHONPATH？」（误诊），cwd 不是检出时则甩 ModuleNotFoundError 整段 traceback。
    """
    problem = generator_dir_problem(repo)
    if problem:
        raise BuildError(f"--repo {repo} {problem}")


def _validate_home(home_arg: Optional[str]) -> Optional[Path]:
    """显式 `--home`：**生成之前**就核对，失败即退出、什么都不改（v0.45.451）。只在目标代码自己的 `main` 里跑
    （委托方不判配置语义，v0.45.456）。

    v0.45.439 先换掉 .app、再处理数据根，`--home` 打错时只在 stderr 留一行、返回 0——人明确要求的事没做成，
    命令却算成功。顺手记环境变量那种不在这里：它失败只是没顺手做成，不算错。
    """
    if home_arg is None:                 # 没给；给了空串由 _reject_empty 在入口拒（不当「没给」）
        return None
    hp = Path(_must_be_absolute(home_arg, "--home")).resolve()
    if not hp.is_dir():
        raise BuildError(f"--home {hp} 不存在：没生成 .app、没改启动器配置")
    from alphabot import launcher
    try:
        launcher.load_config()
    except launcher.LauncherError as exc:
        raise BuildError(f"--home 要写启动器配置，但它读不了：{exc}") from None
    return hp


def _given(value, what: str):
    """「给没给」的**唯一**判法：`None` = 没给（用缺省）；给了空白字符串 = 无效输入，拒绝，不当「没给」（v0.45.458 / 461）。

    v0.45.456 本地分支用 `x or 缺省`、`if not x` 判：空串被悄悄换成缺省解释器 / 缺省目录 / 不改数据根，委托分支却用
    `is not None` 原样转发——同一输入两支语义不同，显式输入被吞。命令行入口（`_reject_empty`）与 `build_app` 都经这里。
    """
    if isinstance(value, str) and not value.strip():
        raise BuildError(f"{what} 给了空值（{value!r}）：不当成「没给」去用缺省；要缺省就别传这个参数")
    return value


def _reject_empty(args) -> None:
    """命令行上显式给了空值（`--python ""`、脚本里 `--dest "$X"` 而 X 为空）⇒ 入口即拒（委托方与目标都先过它）。"""
    for flag in ("dest", "python", "home", "repo"):
        _given(getattr(args, flag), f"--{flag}")


def _env_home_absolute() -> Optional[Path]:
    """环境 `ALPHA_HIVE_HOME` 能不能参与（记下 / 与配置比较）——**入口一次定类**（v0.45.458）。

    取值走数据根的唯一规则 `hive_logger.explicit_data_root`：空串按未设、**原样**用（不展开 `~`、不规整）——扫描、
    MCP 读到的就是这个值（v0.45.461：458 自己 `expanduser`，字面 `~/x` 在这里算「绝对、与配置相同」，扫描却在读
    `./~/x`，分歧被藏住）。不是绝对路径**整个不参与**：不记、也不拿去比较——v0.45.456 只在「首次记」那支拒，
    却在「与配置比较」那支照样按当前 cwd 解析，`make -C 克隆` 下就会误报「与启动器配置不同」。
    """
    from hive_logger import explicit_data_root     # 同在仓库根（`-m` 从仓库根跑）；import 不碰文件系统
    p = explicit_data_root()
    if p is None:
        return None
    if not p.is_absolute():
        tilde = "（`~` 不会被展开：扫描、MCP 也按字面用它）" if str(p).startswith("~") else ""
        print(f"环境里的 ALPHA_HIVE_HOME={p} 是相对路径{tilde}：不记进启动器配置、也不拿它和配置比较"
              "（双击时在别的目录下解析，含义会变；要设数据根用 --home 给绝对路径）", file=sys.stderr)
        return None
    return p


def _record_data_root(explicit_home: Optional[Path]) -> int:
    """处理启动器配置里的数据根，并说清楚**双击时**会发生什么（判断与启动器同源：`launcher.config_state`）。

    只有 `--home` 改写已有配置。环境里的 `ALPHA_HIVE_HOME` 只在配置还没有数据根时顺手记下：开发 shell 常把它
    指向沙箱，v0.45.436 前无条件写入 ⇒ 在开发检出里重新生成一次，生产 Alpha Bot 就改读沙箱（v0.45.439 二次检查）。
    配置文件坏了：照实报，不改写（双击时启动器会弹同一个错）。`--home` 已在生成前核对过（`_validate_home`）；
    这里仍失败（期间被删 / 改坏）就返回 1——显式要求没做成不能算成功。
    """
    from alphabot import launcher
    try:
        cfg = launcher.load_config()
    except launcher.LauncherError as exc:
        print(f"⚠️ {exc}", file=sys.stderr)
        return 1 if explicit_home is not None else 0
    # 给了 --home 时环境变量根本不参与 ⇒ 不去定类、不打它的提示（v0.45.458：此前照样提示「要设数据根用 --home」，与刚用的 --home 自相矛盾）
    env_home = _env_home_absolute() if explicit_home is None else None
    state, home = launcher.config_state(cfg)

    want, why = None, ""
    if explicit_home is not None:
        want, why = explicit_home, "--home"
    elif env_home and state == "first":
        want, why = env_home, "环境 ALPHA_HIVE_HOME（配置里还没有数据根）"
    elif env_home and (home is None or env_home.resolve() != Path(home).expanduser().resolve()):
        print(f"环境里的 ALPHA_HIVE_HOME={env_home} 与启动器配置不同，没有改写配置；真要换用 --home", file=sys.stderr)

    if want:
        hp = Path(want)                      # 两个来源都已是绝对路径（--home 已规整，环境变量不绝对就不参与）
        if not hp.is_dir():
            print(f"⚠️ 数据根 {hp}（来自 {why}）不存在，没写进配置", file=sys.stderr)
            if explicit_home is not None:
                return 1
        else:
            for k in launcher.RESET_KEYS:        # 连同 demo 一起换掉：demo 优先于数据根，只写数据根等于没换
                cfg.pop(k, None)
            cfg["alpha_hive_home"] = str(hp.resolve())
            print(f"数据根 {cfg['alpha_hive_home']}（来自 {why}）→ {launcher.save_config(cfg)}")
            state, home = launcher.config_state(cfg)

    print({
        "home": f"沿用启动器配置里的数据根 {home}",
        "demo": f"启动器配置是演示模式（demo: true）：双击直接进演示；换真实数据先 {here_command('-m alphabot.launcher --reset')}",
        "stale": f"⚠️ 启动器配置里的数据根 {home} 不存在了：双击时会要你重选（或用 --home 指定）",
        "first": "启动器配置里还没有数据根：首次双击时会让你选一次（或用 --home 给绝对路径）",
    }[state])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alphabot.macos_app", description="生成 macOS 的 Alpha Bot.app")
    # 缺省值都是 None、在「本检出就是目标」那支里才填：委托时只转发人**明确给了**的参数，缺省由目标代码自己定（v0.45.456）
    ap.add_argument("--dest", default=None, help="装到哪个目录（缺省 ~/Applications；相对路径按当前目录，当场就用）")
    ap.add_argument("--python", default=None,
                    help=f"写进 .app 的解释器，必须是绝对路径（缺省 {DEFAULT_PYTHON}）：双击时才用，那时 cwd / PATH 早已不同")
    ap.add_argument("--home", default=None,
                    help="改写启动器配置里的数据根，必须是绝对路径。不给时：配置还没有数据根才顺手记下当前环境的 "
                         "ALPHA_HIVE_HOME（须绝对），已有的绝不改写；都没有就首次双击时再选")
    ap.add_argument("--repo", default=None,
                    help="`.app` cd 进哪份代码（整次生成都交给那份代码自己的生成器）；缺省生产克隆 ~/alpha-hive-prod，"
                         "不在就报错。只在测自己的检出时给")
    args = ap.parse_args(argv)

    try:
        _reject_empty(args)
        repo = Path(args.repo).expanduser().resolve() if args.repo is not None else default_repo().resolve()
        if repo != _repo_root():
            _check_generator_dir(repo)
    except BuildError as exc:
        print(f"alphabot.macos_app: {exc}", file=sys.stderr)
        return 1

    if repo != _repo_root():
        # 委托方只做与版本无关的事：选目标、认目标、转发。参数语义（绝对路径规则、--home、配置可读）全归目标（v0.45.456）
        if os.environ.get(_DELEGATED_ENV):
            print(f"alphabot.macos_app: 已是被委托的那次（来自 {os.environ[_DELEGATED_ENV]}），却又要委托给 {repo}"
                  f"（本检出 {_repo_root()}）——停下，不一层层委托下去", file=sys.stderr)
            return 1
        print(f"代码目录 {repo} 不是本检出（{_repo_root()}）⇒ 整次生成交给它自己的生成器", file=sys.stderr)
        forward = ["--repo", str(repo)]
        if args.dest is not None:            # 当场用的路径：按本进程 cwd 定死（委托不换 cwd，定死只为让 argv 自带含义）
            forward += ["--dest", str(Path(args.dest).expanduser().resolve())]
        for flag, val in (("--python", args.python), ("--home", args.home)):
            if val is not None:              # 原样转发：要持久写下的值由目标按它的规则核对，委托方不预判
                forward += [flag, val]
        return run_target_generator(repo, forward)

    try:
        # 本检出就是目标：所有校验与缺省都在这里——先核对显式输入，后动手
        dest = Path("~/Applications" if args.dest is None else args.dest).expanduser().resolve()
        python = _must_be_absolute(DEFAULT_PYTHON if args.python is None else args.python, "--python")
        home = _validate_home(args.home)
    except BuildError as exc:
        print(f"alphabot.macos_app: {exc}", file=sys.stderr)
        return 1

    if _under_desktop_or_documents(dest):
        print(f"⚠️ {dest} 在 iCloud 同步范围内，可能被复制出「Alpha Bot 2.app」；建议用缺省的 ~/Applications",
              file=sys.stderr)
    try:
        bundle = build_app(dest, repo=repo, python=python)
    except BuildError as exc:
        print(f"alphabot.macos_app: {exc}", file=sys.stderr)
        return 1
    print(f"已生成 {bundle}（代码目录 {repo}）")
    # 写进 .app 的解释器当场说出来、当场核对（v0.45.458）：此前要到双击才暴露（如旧生成器把裸名字原样写进壳）。
    # 只警告不拒：缺省解释器在 CI 上本来就不存在，人也可能之后才装；双击时壳仍会核对并弹窗
    print(f"写进 .app 的解释器：{python}")
    problem = interpreter_problem(python)    # 与壳里双击时那道检查同一条判据（v0.45.461）
    if problem:
        print(f"⚠️ {python} 现在不能执行（{problem}）：双击 .app 时会弹窗「找不到」，装好它或用 --python 换一个",
              file=sys.stderr)

    rc = _record_data_root(home)
    if sys.platform != "darwin":
        print("（当前不是 macOS：.app 生成了，但只能在 Mac 上双击运行）", file=sys.stderr)
    print("用法：双击打开；拖到 Dock 常驻。停止服务：页面底部「停止服务」。换数据根："
          f"{here_command('-m alphabot.launcher --reset')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
