"""experiments 脚本按文档方式运行时必须找得到仓库根模块（v0.45.294）

背景
----
`python3 experiments/xxx.py` 运行时 `sys.path[0]` 是 `experiments/`，不是仓库根。脚本里凡是
`from hive_logger import PATHS` 之类的仓库根模块 import，都得先有一行 `sys.path` 注入，否则
`ModuleNotFoundError`。`signal_ic_sweep.py` 在 v0.45.290 修好过一次；同一目录里还有三个同样坏着：

  · vol_regime_filter.py          顶层 import ⇒ 任何运行方式都炸（v0.45.260 迁移遗留）
  · ticker_winrate_persistence.py 惰性 import（`find_db()` 内）⇒ 文档用法（无参）即炸（同上）
  · bear_read_miss_audit.py       惰性 import（`main()` 内，仅 `--root` 缺省时）⇒ `--help` 与文档
                                  示例（显式给 --root）都正常，只有缺省路径炸。⚠️ 这个是
                                  **v0.45.288 新写的脚本**——不是迁移遗留，是新代码把同一个错重犯了一次

三个脚本零测试、零调用方，没有任何东西会替它们红。

⚠️ 为什么本文件不用 `--help` 当探针
  · vol_regime_filter 没有 argparse，`--help` 根本不被识别（直接开库跑全量）；
  · 另两个的 `--help` **恒绿**——argparse 在走到函数体里的 import 之前就退出了。
  「验收清单要真跑每个被动过的脚本 `--help`」这条旧教训本身有洞：探针必须走到 import 所在的
  **代码路径**，不是走到 argparse。

本文件补两道，且第二道有 canary 自证：
  1. 静态守卫 —— AST 扫 experiments/*.py：任何 import 仓库根模块的脚本，必须在**首个**该类 import
     之前、**模块层**（不在函数体内）有 sys.path 注入。函数体内的注入只在那个函数被调用时才执行，
     保护不了别处的 import，所以不算。
  2. 真子进程 —— cwd 与仓库根无关、清 PYTHONPATH，逐脚本走到 import 所在的缺省路径。

静态守卫的已知局限（勿当成它没有）
  · 只检查「注入存在且在 import 之前」，**不校验注入的是不是仓库根**。一次性脚本
    patch_swarm_results_20260917.py / replay_swarm_sequence_20260917.py 硬编码了主 checkout 路径，
    开发机上有效、别处无效，本守卫放行。「注入得对不对」靠第二道逐脚本钉。
  · 只认 sys.path.insert / append / extend 三种调用；别的写法（`sys.path[0] = …`、`sys.path = …`）
    会红——有意为之：宁可误报、要人改成标准写法，也不放过没识别的形式。
"""

import ast
import os
import random
import sqlite3
import subprocess
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS = ROOT / "experiments"

_INJECT_METHODS = {"insert", "append", "extend"}


# ══════════════════════════════════════════════════════════════════════════
# 静态检查器（纯函数：源码字符串进、违规列表出，好喂合成样本）
# ══════════════════════════════════════════════════════════════════════════

def _root_modules() -> set:
    """仓库根下的模块/包名——现算，不写死（写死的清单会过期）。
    去掉与标准库同名者：experiments 脚本里的 `import types` 几乎一定是标准库，误报比漏报更烦。"""
    names = {p.stem for p in ROOT.glob("*.py")}
    names |= {p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    return names - set(sys.stdlib_module_names)


def _is_sys_path_call(node: ast.Call) -> bool:
    f = node.func
    return (isinstance(f, ast.Attribute) and f.attr in _INJECT_METHODS
            and isinstance(f.value, ast.Attribute) and f.value.attr == "path"
            and isinstance(f.value.value, ast.Name) and f.value.value.id == "sys")


def _walk(node, in_func=False):
    """先序遍历，附带「该节点是否位于函数体内」。类体在定义时执行（≈模块层），故不计入函数。"""
    for child in ast.iter_child_nodes(node):
        yield child, in_func
        yield from _walk(
            child, in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))


def _analyse(source: str):
    """→ (所有 import 的 [(行号, 顶层模块名)]，模块层 sys.path 注入的 Call 节点列表)。
    import 取**任意嵌套层级**——函数体里的惰性 import 一样会炸，只是要走到那条路径才炸。"""
    imports, injections = [], []
    for node, in_func in _walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports += [(node.lineno, a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append((node.lineno, node.module.split(".")[0]))
        elif isinstance(node, ast.Call) and not in_func and _is_sys_path_call(node):
            injections.append(node)
    return imports, injections


def find_bootstrap_violations(source: str, roots) -> list:
    imports, injections = _analyse(source)
    root_imports = sorted((ln, m) for ln, m in imports if m in roots)
    if not root_imports:
        return []
    first_ln, first_mod = root_imports[0]
    if not injections:
        return [f"第 {first_ln} 行 import 了仓库根模块 `{first_mod}`，但脚本没有模块层 sys.path 注入"]
    first_inj = min(c.lineno for c in injections)
    if first_inj > first_ln:
        return [f"sys.path 注入在第 {first_inj} 行，晚于第 {first_ln} 行对仓库根模块 `{first_mod}` 的 import"]
    return []


def _strip_injections(source: str) -> str:
    """删掉全部模块层 sys.path 注入语句（按 AST 行区间删，不靠正则），用于对真实文件做变异。"""
    kill = set()
    for node, in_func in _walk(ast.parse(source)):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and not in_func and _is_sys_path_call(node.value)):
            kill.update(range(node.lineno, node.end_lineno + 1))
    return "".join(ln for i, ln in enumerate(source.splitlines(keepends=True), 1) if i not in kill)


def _src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


# ══════════════════════════════════════════════════════════════════════════
# 守卫自己有牙：合成样本（零外部依赖，没有这一组，下面的全绿证明不了任何事）
# ══════════════════════════════════════════════════════════════════════════

_TEST_ROOTS = {"hive_logger", "config"}

_VIOLATING = {
    "顶层 import，无注入": """
        import sys
        from hive_logger import PATHS
    """,
    "惰性 import，无注入（bear_read_miss_audit 的形状，--help 看不见）": """
        import argparse
        def main():
            from hive_logger import PATHS
    """,
    "注入晚于 import": """
        from hive_logger import PATHS
        import sys
        sys.path.insert(0, "x")
    """,
    "注入只在函数体内（保护不了模块层的 import）": """
        import sys
        def boot():
            sys.path.insert(0, "x")
        from hive_logger import PATHS
    """,
    "`import config as C` 形式": """
        import config as C
    """,
    "`import 根模块.子模块` 形式": """
        import hive_logger.sub
    """,
}

_CLEAN = {
    "注入在前，顶层 import": """
        import sys
        sys.path.insert(0, "x")
        from hive_logger import PATHS
    """,
    "append 形式也认": """
        import sys
        sys.path.append("x")
        import config
    """,
    "模块层注入在前 + 函数体内惰性 import": """
        import sys
        sys.path.insert(0, "x")
        def main():
            from hive_logger import PATHS
    """,
    "只用标准库，不需要注入": """
        import os, sys
        import sqlite3
    """,
    "兄弟脚本 import 不算仓库根（sys.path[0] 就是 experiments/）": """
        from signal_ic_sweep import something
    """,
    "相对 import 不算": """
        from . import helper
    """,
}


@pytest.mark.parametrize("label", list(_VIOLATING))
def test_guard_flags_violating_samples(label):
    assert find_bootstrap_violations(_src(_VIOLATING[label]), _TEST_ROOTS), (
        f"守卫没认出违规样本「{label}」——它对这种形状是瞎的")


@pytest.mark.parametrize("label", list(_CLEAN))
def test_guard_accepts_clean_samples(label):
    assert find_bootstrap_violations(_src(_CLEAN[label]), _TEST_ROOTS) == [], (
        f"守卫误报了合规样本「{label}」")


# ══════════════════════════════════════════════════════════════════════════
# 静态守卫：真实的 experiments/*.py
# ══════════════════════════════════════════════════════════════════════════

def test_root_module_enumeration_sees_the_real_repo():
    """正对照：仓库根模块清单是真的枚举出来的（否则下面的扫描会因「清单为空」恒绿）。"""
    roots = _root_modules()
    assert {"hive_logger", "config"} <= roots, "根模块枚举漏了 hive_logger/config——glob 或路径出了问题"
    assert len(roots) > 50, f"只枚举到 {len(roots)} 个根模块，明显不对"


def test_every_experiments_script_bootstraps_before_importing_repo_modules():
    roots = _root_modules()
    scripts = sorted(EXPERIMENTS.glob("*.py"))
    assert scripts, "experiments/*.py 一个都没扫到——守卫会因空转恒绿"

    importers, bad = [], []
    for f in scripts:
        src = f.read_text(encoding="utf-8")
        if any(m in roots for _, m in _analyse(src)[0]):
            importers.append(f.name)
        bad += [f"{f.name}: {v}" for v in find_bootstrap_violations(src, roots)]

    assert importers, "没有任何 experiments 脚本被识别为「import 仓库根模块」——分类器是瞎的"
    assert not bad, (
        "experiments 脚本 import 了仓库根模块却没有在其之前的模块层 sys.path 注入，"
        "按 `python3 experiments/xxx.py` 运行会 ModuleNotFoundError（惰性 import 只在走到那条路径时才炸，"
        "`--help` 看不出来）：\n  " + "\n  ".join(bad)
        + "\n修法：在所有 import 之前加 "
        "`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`（内联表达式，"
        "不要赋给模块级路径常量——那会被 test_paths_not_frozen_at_import 当成新增 `__file__` 冻结路径）")


def test_stripping_the_injection_turns_every_real_file_red():
    """反向自证（用真文件，不是我编的样本）：把每个「现在合规」的真实脚本里的注入删掉，守卫必须变红。
    证明守卫的「绿」不是因为它认不出这些文件里真实存在的写法（多行、`str(ROOT)`、硬编码路径等）。"""
    roots = _root_modules()
    mutated, still_green = 0, []
    for f in sorted(EXPERIMENTS.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        if find_bootstrap_violations(src, roots):
            continue                                   # 已违规的由上一条报，这里只变异「现在合规」的
        if not any(m in roots for _, m in _analyse(src)[0]):
            continue
        stripped = _strip_injections(src)
        assert stripped != src, f"{f.name} 被判为「有注入」却没能删掉任何东西——变异器与检查器口径不一致"
        mutated += 1
        if not find_bootstrap_violations(stripped, roots):
            still_green.append(f.name)
    assert mutated >= 1, "一个真实文件都没变异成——这条测试空转"
    assert not still_green, f"删掉注入后守卫仍然放行：{still_green}——守卫对这些文件的写法没有牙"


# ══════════════════════════════════════════════════════════════════════════
# 真子进程：按文档方式，走到 import 所在的缺省代码路径
# ══════════════════════════════════════════════════════════════════════════

def _run(script: Path, args, home: Path, cwd: Path):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)               # 不能让环境替脚本补上它自己该注入的 sys.path
    # ticker_winrate_persistence 的专属 env：设了会在惰性 import 之前就短路（find_db 第一级），
    # 探针就绕开了要测的那行。
    env.pop("ALPHA_HIVE_PHEROMONE_DB", None)
    # `PATHS.db` 先读 ALPHA_HIVE_DB_PATH，而 conftest 的 `_isolate_env` 会把它设成别处，子进程
    # 照单全收——只设 ALPHA_HIVE_HOME 会让脚本去开一个不存在的库。日志/缓存/chroma 一并钉进临时目录。
    env["ALPHA_HIVE_HOME"] = str(home)
    env["ALPHA_HIVE_DB_PATH"] = str(home / "pheromone.db")
    env["ALPHA_HIVE_LOGS_DIR"] = str(home / "logs")
    env["ALPHA_HIVE_CACHE_DIR"] = str(home / "cache")
    env["ALPHA_HIVE_CHROMA_PATH"] = str(home / "chroma_db")
    return subprocess.run([sys.executable, str(script), *args],
                          cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180)


def _assert_bootstrapped(r, script):
    assert "ModuleNotFoundError" not in r.stderr, (
        f"{script} 按缺省路径运行时找不到仓库根模块——缺 sys.path 注入。stderr 尾部：\n{r.stderr[-500:]}")
    assert "Traceback" not in r.stderr, f"{script} 崩溃。stderr 尾部：\n{r.stderr[-800:]}"


def test_harness_canary_a_script_without_injection_really_fails(tmp_path):
    """探针自证：下面三条「不许 ModuleNotFoundError」只有在这套环境（临时 cwd + 清 PYTHONPATH）
    **确实看不到仓库根**时才有意义。若 site-packages 里有指向仓库的 .pth、或某处把仓库根塞进了
    sys.path，三条会永远假绿。这里造一个无注入的脚本，它在同一套环境里必须炸。"""
    home = tmp_path / "home"
    home.mkdir()
    canary = tmp_path / "canary.py"
    canary.write_text("from hive_logger import PATHS\n", encoding="utf-8")

    r = _run(canary, [], home, tmp_path)

    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr, (
        "canary 没炸：子进程环境能看到仓库根，本文件的真子进程测试是瞎的。"
        f"returncode={r.returncode} stderr={r.stderr[-300:]!r}")


N_VALID = 12 * 8


def _make_vol_db(path: Path) -> None:
    """合成库：12 个 ISO 周 × 8 只标的，每只标的中性/方向各 6 条（过 section_controls 的 ≥5 门槛），
    每周 4 中性 + 4 方向（过 section_power 的门槛）。另加三条应被 load_records 滤掉的行——
    样本计数指纹要靠它们证明：读到的是这份库，且三道过滤都生效。"""
    rng = random.Random(20260918)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE predictions (direction TEXT, ticker TEXT, date TEXT,
                   price_at_predict REAL, close_t7 REAL, checked_t7 INTEGER)""")
    con.execute("CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL)")

    def add(day, tk, direction, close_t7, checked, iv):
        con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?)",
                    (direction, tk, day, 100.0, close_t7, checked))
        con.execute("INSERT INTO signal_archive VALUES (?,?,?,?)", (day, tk, "options.iv_current", iv))

    d0 = date(2026, 3, 2)                                     # 周一
    day = d0.isoformat()
    for w in range(12):
        day = (d0 + timedelta(weeks=w)).isoformat()
        for k in range(8):
            add(day, f"T{k}", "neutral" if (w + k) % 2 == 0 else "bullish",
                100.0 * (1 + rng.gauss(0, 0.05)), 1, 40.0 + 5 * k)
    add(day, "X0", "bullish", 105.0, 1, 0.0)                  # IV=0 是缺失哨兵
    add(day, "X1", "bullish", None, 1, 50.0)                  # close_t7 缺失
    add(day, "X2", "bullish", 105.0, 0, 50.0)                 # checked_t7=0（未到期）
    con.commit()
    con.close()


def test_vol_regime_filter_runs_from_a_foreign_cwd(tmp_path):
    home = tmp_path / "home"
    _make_vol_db(home / "pheromone.db")

    r = _run(EXPERIMENTS / "vol_regime_filter.py", [], home, tmp_path)

    _assert_bootstrapped(r, "vol_regime_filter.py")
    # returncode 不能当判据：main() 的 0/1/3 都是「跑到了结论」，而未捕获异常也是 1。
    assert f"样本 {N_VALID} 条" in r.stdout, (
        f"没读到夹具库（或三道过滤没生效）。stdout 头部：\n{r.stdout[:300]}")
    assert "就绪度" in r.stdout, "没有一路跑到最后一节（section_power）"


def test_ticker_winrate_persistence_default_path_reaches_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()                                               # 故意没有 pheromone.db

    r = _run(EXPERIMENTS / "ticker_winrate_persistence.py", [], home, tmp_path)

    _assert_bootstrapped(r, "ticker_winrate_persistence.py")
    # 走过 find_db() 里的惰性 import，且 PATHS.db 落在夹具（不存在）而不是真库——若指向真库这里会是
    # 「DB: …」。（Cowork VM 的 /sessions/*/mnt 探测在开发机与 CI 上都不存在。）
    assert "找不到 pheromone.db" in r.stdout, f"stdout 头部：\n{r.stdout[:300]}"
    assert r.returncode == 1


def test_bear_read_miss_audit_default_root_reaches_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    r = _run(EXPERIMENTS / "bear_read_miss_audit.py", [], home, tmp_path)   # 不给 --root ⇒ 走惰性 import

    _assert_bootstrapped(r, "bear_read_miss_audit.py")
    assert f"root={home}" in r.stdout, (
        f"缺省 root 没解析到 PATHS.home（夹具目录）。stdout 头部：\n{r.stdout[:300]}")
    assert r.returncode == 3, "夹具目录里没有 .swarm_results_*.json ⇒ 该脚本约定退出码 3（无法判定）"
