#!/usr/bin/env python3
"""CrewAI 已于 v0.45.74 彻底移除——这条闸防它回来。

背景：`crewai_adapter.py` 是 Phase 3 P5 的实验，把 6 只自研蜂包成 CrewAI Tool、
交给一个 LLM manager 走 `Process.hierarchical` 调度。它从未接通：
`run_crew_scan()` 零调用方、`requirements.txt` 里那行是注释掉的、
`Agent(...)` 从没传 `llm=`（会落到 crewai 默认的 OpenAI `gpt-4.1-mini`，
而本仓无 `OPENAI_API_KEY`）。同时它在 import 时会起线程给 api.scarf.sh 打埋点。

两条测试守两件不同的事，别合并：

1. `test_daily_report_does_not_import_crewai`
   —— 守**运行时**：日报主入口的 import 图里不许出现 crewai。
   ⚠️ 这条的有效性取决于 crewai 是否还装着。若哪天 `pip uninstall crewai`，
   它会变成恒真（import 不进来是因为没装，不是因为我们没写）。所以必须配第 2 条。

2. `test_repo_has_no_crewai_import`
   —— 守**源码**：全仓不许再出现 `import crewai`。
   静态检查，与 crewai 装没装无关，卸载后仍然有效。
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from tests._repo_files import own_python_files

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只匹配真正的 import 语句，不匹配注释、字符串、文档里提到的名字
_IMPORT_RE = re.compile(r"^\s*(?:from\s+crewai[\w.]*\s+import|import\s+crewai\b)", re.M)

#: v0.45.189：换到 `own_python_files` 后，此前六项里**只剩 `venv` 还承重**。
#: `.git` / `.pytest_cache` / `.venv` 由 `_repo_files._is_ours` 的「点号开头」
#: 那道过滤覆盖，`node_modules` 由它的 `VENDORED` 覆盖，`__pycache__` 里没有 .py。
#: 留着它们就是**删掉也没有任何测试会红**的等价变异行。
#: `venv`（裸名、无点号、不在 `VENDORED` 里）两道都盖不到，因此显式留下——
#: 真装了 crewai 的话，它的源码就在那儿。夹具样本 ③ 钉这一点。
_SKIP_DIRS = {"venv"}


def test_daily_report_does_not_import_crewai():
    """import alpha_hive_daily_report 之后，sys.modules 里不该有 crewai。"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import sys
            import alpha_hive_daily_report  # noqa: F401
            print("CREWAI_IMPORTED=%s" % ("crewai" in sys.modules))
        """)],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": PROJECT_ROOT},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "CREWAI_IMPORTED=False" in result.stdout, (
        "crewai 又被拖进日报的 import 图了。它是 v0.45.74 移除的死集成："
        "零调用方、无 llm 配置、import 时还会打第三方埋点。\n"
        f"stdout={result.stdout!r}"
    )


def _scanned_files(root=None):
    """`_crewai_offenders` **实际会读**的文件清单，返回 `([(绝对路径, 相对路径)], 口径名)`。

    ⚠️ 抽出来是为了让真仓库量级护栏能打到**本文件的枚举**，而不是打到
    `tests/_repo_files.own_python_files`。v0.45.199 自查实测：护栏此前直接调
    `own_python_files(PROJECT_ROOT)`，于是「把 `_crewai_offenders` 的枚举换回裸
    rglob」这条变异**根本不会让它变红**——而它的 docstring 白纸黑字说会。
    测 helper 没坏 ≠ 测本文件没坏；**中间隔着的正是本文件自己那几行**。

    v0.45.189：枚举**不是** `os.walk(root)`。生产 checkout 的 `.claude/worktrees/`
    下挂着 14 个嵌套 git worktree，`os.walk` 在那里走过 5029 个 .py
    （其中 4295 个在 `.claude/` 下）——**2026-09-11 实测的历史值**，当时 git 跟踪的是 340 个。
    理由全文见 `tests/_repo_files.py`；本文件末尾那组测试是它的守卫。

    ⚠️ **已知覆盖取舍（v0.45.199 补记，此前守卫里一个字都没写）**：git 口径只列
    **被跟踪**的文件，因此**全新未提交**的 .py 扫不到（已跟踪文件的未提交修改照常
    扫得到——只拿 git 要路径清单，内容从磁盘读）。实测：新建一个未跟踪的
    `zz.py` 写上 `import crewai`，本守卫**看不见**；`git add -N` 之后立刻看得见。
    这是 v0.45.150 就明确接受的取舍（口径可复现 > 把「本地碰巧有什么」算进来），
    v0.45.189 把它扩到了本守卫却没有复述——**取舍不写在守卫里，下一个人只会读到
    模块 docstring 那句「全仓源码不该再有」，并信以为真**。
    """
    root = Path(PROJECT_ROOT if root is None else root)
    files, mode = own_python_files(root)
    out = []
    for py in files:
        rel = py.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if py.resolve() == Path(__file__).resolve():
            continue  # 本文件的正则字面量不算
        out.append((py, rel))
    return out, mode


def _crewai_offenders(root=None):
    """扫出所有 `import crewai` 的文件，返回相对 `root` 的路径清单。

    `root` 是**参数**不是模块常量——调用时求值，测试才能把它指向一棵带病灶的
    tmp 树（本文件里这棵树是 `.claude/worktrees/<副本>/`）。
    """
    offenders = []
    for py, rel in _scanned_files(root)[0]:
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _IMPORT_RE.search(src):
            offenders.append(str(rel))
    return offenders


def test_repo_has_no_crewai_import():
    """全仓源码不该再有 `import crewai` / `from crewai import ...`。"""
    offenders = _crewai_offenders()
    assert not offenders, (
        "以下文件重新引入了 crewai：\n  " + "\n  ".join(sorted(offenders))
        + "\n\nCrewAI 集成已于 v0.45.74 移除（详见 CHANGELOG）。真要重新引入，"
          "先回答三个问题：manager 用哪个 LLM、谁付钱、"
          "以及它凭什么比现行的 QueenDistiller 固定权重聚合更准。"
    )


# ════════════════════════════════════════════════════════════════════════
# v0.45.189：枚举范围本身的守卫
#
# `test_repo_has_no_crewai_import` 是**枚举驱动**的——它禁止什么，完全由
# `_crewai_offenders()` 走过哪些目录决定。此前它是 `os.walk(PROJECT_ROOT)`
# 配一份 **不含 `.claude`** 的 `_SKIP_DIRS`：在生产 checkout
# `~/Desktop/Alpha Hive` 上，`.claude/worktrees/` 下挂着 14 个**嵌套 git
# worktree**（每个是一份停在各自版本的完整仓库副本），实测走过 5029 个 .py，
# 其中 **4295 个（85%）在 `.claude/` 下**；git 跟踪的只有 340 个。
#
# ⚠️ 与 `test_no_fake_price.py` 同源同版一起修，但泄漏面**不一样**，所以判据
#   要各自重推一遍、不能照抄：那边 `_EXCLUDE_DIRS` 判绝对路径段，把嵌套副本的
#   `tests/` `experiments/` 恰好排掉了；这边**一个都没排**——嵌套副本里的
#   测试文件、实验脚本全都算进「全仓源码」。
#
# 它当时是绿的，唯一原因是「碰巧没有哪份陈旧副本里还写着 `import crewai`」。
# 而 crewai 是 v0.45.74 才移除的：**任何一个 09-01 之前分出去的 worktree
# 里都还有 `crewai_adapter.py`**。红一旦出现，只出现在生产 checkout 上
# （worktree 里没有嵌套 worktree），造成它的人看到的是绿。
# MEMORY `alpha-hive-test-writes-production`：病灶只长在没人看的地方。
#
# 下面这几条**必须**用 tmp 树：在 worktree 里跑什么都证明不了。
# ════════════════════════════════════════════════════════════════════════

#: 夹具载荷。⚠️ 刻意写成**缩进在三引号串里的 `import crewai`**——这样本文件
#: 自己就被 `_IMPORT_RE`（`^\s*import\s+crewai`，re.M）命中，
#: `_crewai_offenders` 里那句自排除因而是 load-bearing 的，
#: 而不是一行谁都测不到的「防御」。见 `test_this_file_is_excluded_from_its_own_scan`。
_CREWAI_FIXTURE_SRC = textwrap.dedent("""\
    import crewai

    def run():
        return crewai.Crew()
""")


def _build_pathological_tree(tmp_path):
    """造一棵**带病灶**的假仓库，三个样本各自覆盖一条排除理由。

    三份文件内容完全相同，区别只在位置——于是任何「该排的没排」都只能由
    路径过滤解释，不会被内容差异混淆。
    """
    # ① 仓库根的生产文件：**必须被扫到**（否则整条守卫是空跑）
    (tmp_path / "crewai_adapter.py").write_text(_CREWAI_FIXTURE_SRC, encoding="utf-8")
    # ② 病灶：嵌套 worktree 里的陈旧副本（v0.45.74 之前的分支就长这样），
    #    **不许被扫到**
    stale = tmp_path / ".claude" / "worktrees" / "stale-copy-abc123"
    stale.mkdir(parents=True)
    (stale / "crewai_adapter.py").write_text(_CREWAI_FIXTURE_SRC, encoding="utf-8")
    # ③ 虚拟环境：`venv` 既**不以点号开头**、也**不在 `_repo_files.VENDORED` 里**，
    #    因此它是 `_SKIP_DIRS` 换到 `own_python_files` 之后**唯一仍然
    #    load-bearing 的一项**——真装了 crewai 的话，它的源码就在这里。
    site = tmp_path / "venv" / "lib" / "python3.11"
    site.mkdir(parents=True)
    (site / "crewai_shim.py").write_text(_CREWAI_FIXTURE_SRC, encoding="utf-8")
    return tmp_path


def test_scanner_has_teeth(tmp_path):
    """先自证探针有效——扫不到任何东西的扫描器会**恒真地绿**。

    没有这一条，下面「嵌套副本不许被扫到」可以靠「什么都扫不到」通过；
    而真仓库里本就该是零命中，所以主断言自己分不清这两种情况。

    变红的变异：把 `_IMPORT_RE` 改成匹配不到的东西（如 `crewaiX`）。
    """
    _build_pathological_tree(tmp_path)
    # ⚠️ 只调**一次**并绑住结果：原写法在断言与失败信息里各调一遍，
    #    扫描器若不确定，断言看到的和人看到的会是两个不同的清单。
    hits = _crewai_offenders(tmp_path)
    assert "crewai_adapter.py" in hits, (
        f"仓库根那份 `import crewai` 都没扫出来，探针坏了，不是「没有违规」：{hits}")


def test_scanner_does_not_cross_into_nested_worktrees(tmp_path):
    """**本版要修的那件事**：枚举不许越界进 `.claude/worktrees/`。

    修之前是红的（`os.walk` 的 `_SKIP_DIRS` 里没有 `.claude`），修后绿。
    两个方向都钉住：根目录那份必须在，另外两份必须不在。

    变红的变异（各自对应一个样本）：
      * 把枚举换回 `os.walk(root)` 且 `_SKIP_DIRS` 不含 `.claude` —— 样本 ② 漏进来；
      * 把 `_SKIP_DIRS` 里的 `venv` 删掉 —— 样本 ③ 漏进来
        （`_repo_files._is_ours` 覆盖不到它：既非点号开头，也不在 `VENDORED` 中）。
    """
    _build_pathological_tree(tmp_path)
    # 前提：夹具确实走 rglob 回退分支（tmp 树不是 git 仓库）——病灶只在这条分支上
    # （嵌套 worktree 从不被外层仓库索引跟踪，实测 `git ls-files '*.py' | grep
    # ^.claude` 为 0 条）。不断言这一点，未来夹具悄悄走到 git 分支，
    # 这条测的就不是它要测的东西。
    from tests._repo_files import own_python_files
    assert own_python_files(tmp_path)[1] == "rglob", (
        "夹具走到了 git 分支——病灶不在那条分支上，这条测试失去意义")

    assert _crewai_offenders(tmp_path) == ["crewai_adapter.py"], (
        "枚举范围错了。应当只扫到仓库根那一份；实际：\n  "
        + "\n  ".join(_crewai_offenders(tmp_path))
        + "\n\n越界扫进 `.claude/worktrees/` 的后果不是「慢」，是**红只在生产 "
          "checkout 上可见**——而 crewai 是 v0.45.74 才移除的，任何一个那之前"
          "分出去的 worktree 里都还留着 `crewai_adapter.py`。")


def test_this_file_is_excluded_from_its_own_scan():
    """本文件的夹具载荷里有一行能被正则命中的 `import crewai`，必须排除自己。

    正则分不清「代码」与「字符串字面量」——这是它的已知代价（换 AST 就要
    面对陈旧副本里语法不兼容的文件），因此自排除那行是真的在承重。

    变红的变异：删掉 `_crewai_offenders` 里
    `if os.path.abspath(full) == os.path.abspath(__file__): continue` 这两行。
    """
    # 前提自证：本文件里**确实**还有能被命中的行。没有它，下面那条恒真地绿，
    # 自排除也就退化成一行谁都测不到的死代码。
    with open(__file__, encoding="utf-8") as fh:
        assert _IMPORT_RE.search(fh.read()), (
            "本文件里已经没有能被 `_IMPORT_RE` 命中的行了——下一条断言恒真，"
            "自排除那行成了测不到的死行。要么恢复夹具的缩进三引号写法，"
            "要么把自排除一起删掉，别留一行没人验的「防御」")
    # ⚠️ 必须按后缀匹配。`_crewai_offenders` 返回的是**相对路径**
    # （`tests/test_no_crewai_dependency.py`），拿 basename 去 `not in` 一个
    # 字符串列表是**恒真**的——实测：把自排除那两行删掉，这条照样绿。
    # 「两边是不同种东西」的断言永远不会红。
    me = os.path.basename(__file__)
    assert not [r for r in _crewai_offenders() if os.path.basename(r) == me], (
        "本文件把自己举报了——自排除那行被删了")


def test_enumeration_covers_the_real_repo():
    """量级护栏：本仓的 .py 是**几百**量级，不是几千也不是零。

    几千 ⇒ 又混进了嵌套 worktree 或第三方库（生产 checkout 上 `os.walk`
    走的是 5029 个）；零/个位数 ⇒ 枚举自己坏了，而坏掉的枚举让主断言恒真地绿。

    变红的变异：把 `_scanned_files` 的枚举换回 `root.rglob("*.py")`
    （生产 checkout 上跳到 **24123**，越过 2000 上界）。

    ⚠️ 别把 24123 和正文里的 5029 搞混，它们是**两个不同的量**：
    5029 是 v0.45.189 之前 `os.walk` + 旧六项 `_SKIP_DIRS` 的历史实测；
    24123 是**今天**把枚举换成裸 rglob 会得到的数（`_SKIP_DIRS` 已缩到
    `{"venv"}`，不再挡 `.git/` 等目录）。v0.45.199 自查时这里原本写的是 5029
    ——**一个在改动中失效的旧数**，同样是「docstring 说了没人核的话」。

    ⚠️ v0.45.199 更正：这条此前直接调 `own_python_files(PROJECT_ROOT)`，
    于是上面那条变异**根本打不到它**（实测只有 `..._nested_worktrees` 变红）。
    **测的是 helper 没坏，而不是本文件的枚举没坏。** 现在改打 `_scanned_files`。
    ⚠️ 这条只在**生产 checkout** 上能被上述变异钉住（那里裸枚举是 5029）；
    在 worktree 里裸枚举只有几百，落在界内不会红。**处处可红的是
    `test_scanner_does_not_cross_into_nested_worktrees`（tmp 夹具）**，
    量级护栏是它的补充、不是替代。
    """
    scanned, mode = _scanned_files()
    assert 50 < len(scanned) < 2000, (
        f"口径={mode} 扫到 {len(scanned)} 个 .py。过多＝混进了嵌套 worktree / "
        "第三方；过少＝清单机制自己坏了")
    rels = [str(rel) for _py, rel in scanned]
    assert "alpha_hive_daily_report.py" in rels, (
        "日报主入口不在扫描范围内——守卫覆盖不到生产主路径")
    leaked = [r for r in rels if r.split(os.sep)[0] == ".claude"]
    assert not leaked, f"枚举里还有 `.claude/` 下的文件：{leaked[:5]}"
    assert os.path.basename(__file__) not in [os.path.basename(r) for r in rels], (
        "本文件没有被 `_scanned_files` 排掉——自排除搬家时掉了")
