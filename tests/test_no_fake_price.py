"""v0.40.1: 假价占位反模式契约测试

历史教训：`price = 100.0` 兜底写法从 v36.0 到 v40.1 造成同一 bug 三次返工
（网站显示 $100 假价）——每次只修抓到现行的一处，漏网的在深夜 yfinance
限流时复发。本测试静态扫描生产代码，永久禁止该反模式重新进入代码库。

规约：现价取不到时用 0.0 哨兵（下游注入/显示逻辑跳过 0），
取价一律走 data_pipeline.fetch_stock_data（CBOE 起头多源链）。

⚠️ **覆盖边界（v0.45.199 补记）**：上面那句「永久禁止该反模式重新进入代码库」
**对全新未提交的文件不成立**。v0.45.189 把枚举换到 `own_python_files` 之后，
git 口径只列**被跟踪**的文件（已跟踪文件的未提交修改照常扫得到——只拿 git 要
路径清单，内容从磁盘读）。实测：新建未跟踪的 `zz.py` 写上 `price = 100.0`，
本守卫**看不见**；`git add -N` 之后立刻看得见。
这是 v0.45.150 明确接受的取舍（口径可复现 > 把「本地碰巧有什么」算进来），
v0.45.189 把它扩到了本守卫**却没有在这里复述**——于是这段 docstring 在那一版
之后一直在承诺一件它做不到的事。真要堵这个窗口，是改
`tests/_repo_files.own_python_files`（加 `--others --exclude-standard`，
`.claude/worktrees/` 自 v0.45.124 已在 .gitignore 里、`_is_ours` 还有第二道），
那是**推翻 v0.45.150 的既定取舍**，要单独决定、不在自查里顺手做。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._repo_files import own_python_files

ROOT = Path(__file__).parent.parent

# 生产代码范围（排除测试/实验/第三方）。
# v0.45.189 移除 `.git` 与 `__pycache__`：换到 `own_python_files` 后二者已被
# `_repo_files._is_ours`（点号开头那道过滤）与「`__pycache__` 里没有 .py」覆盖，
# 留在这里是**删掉也没有任何测试会红**的等价变异行。剩下的每一项都仍是
# load-bearing 的：它们判的是「这段代码会不会进每日扫描」，而非「是不是垃圾目录」。
_EXCLUDE_DIRS = {"tests", "experiments", "mcp-servers", "alpha_hive_bot", "gui"}

# 反模式：price 类变量被赋值为 100.0 字面量（允许注释里出现）
_PATTERNS = [
    re.compile(r'(?<!#)\s*[\w\]"\']*price[\w"\']*\s*[:=]\s*100\.0\b', re.IGNORECASE),
    re.compile(r'"current_price"\s*:\s*100\.0\b'),
    re.compile(r"or\s+100\.0\b"),
]

# 已知合法例外（数学运算/百分比换算等，逐行白名单）
_ALLOWLIST_SUBSTR = [
    "/ 100.0",      # 百分比换算
    "* 100.0",
    "100.0)",       # clamp/min/max 上限
]


def _iter_prod_py(root=None):
    """本仓自己的生产 .py。

    `root` 是**参数**不是模块常量——调用时求值，测试才能把它指向一棵带病灶的
    tmp 树（本文件里这棵树是 `.claude/worktrees/<副本>/`）。
    """
    root = Path(root) if root is not None else ROOT
    # v0.45.189：**不是** `root.rglob("*.py")`。生产 checkout 的
    # `.claude/worktrees/` 下挂着 14 个嵌套 git worktree，裸 rglob 在那里扫到
    # 2128 个 .py（其中 1975 个在 `.claude/` 下），git 跟踪的只有 340 个。
    # 理由全文见 `tests/_repo_files.py`；本文件末尾那组测试是它的守卫。
    for p in own_python_files(root)[0]:
        # ⚠️ 判**相对** root 的路径段，不是 `p.parts`。按绝对路径判时，仓库被
        # checkout 到的位置会改变覆盖面——放进任何叫 `gui`/`tests` 的目录，
        # 整条守卫静默扫零个文件且照样是绿的（`test_exclude_dirs_are_matched
        # _relative_to_root` 钉这一点）。
        if any(part in _EXCLUDE_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


def _scan_violations(root=None):
    """扫出所有 `price = 100.0` 反模式命中，返回 `"相对路径:行号: 源码"` 清单。"""
    root = Path(root) if root is not None else ROOT
    violations = []
    for py in _iter_prod_py(root):
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines, 1):
            code = line.split("#", 1)[0]  # 忽略注释
            if "100.0" not in code:
                continue
            # 跳过含 CJK 的行（docstring/注释里的中文说明文字，非代码赋值）
            if any("\u4e00" <= ch <= "\u9fff" for ch in code):
                continue
            if any(a in code for a in _ALLOWLIST_SUBSTR):
                continue
            for pat in _PATTERNS:
                if pat.search(code):
                    violations.append(f"{py.relative_to(root)}:{i}: {line.strip()[:90]}")
                    break
    return violations


def test_no_hardcoded_price_100_placeholder():
    violations = _scan_violations()
    assert not violations, (
        "发现 price=100.0 假价占位反模式（v36→v40.1 三次返工的根因）。\n"
        "规约：取不到现价用 0.0 哨兵 + data_pipeline.fetch_stock_data 多源链。\n"
        + "\n".join(violations)
    )


def test_max_pain_degenerate_guards():
    """v0.41.2: Max Pain 退化保护——全零/薄 OI 或偏离现价 >50% 必须返回 None。

    事故：yfinance 深夜限流返回全零 OI 链时，旧实现每个行权价痛苦值恒 0，
    取到链内最低行权价（NVDA 现价 $203 算出磁吸价 $50，+307%）。
    """
    from swarm_agents.oracle_bee import OracleBeeEcho
    f = OracleBeeEcho._max_pain_from_oi
    assert f({50.0: 0, 100.0: 0, 200.0: 0}, {50.0: 0}, 203.62) is None      # 全零 OI
    assert f({200.0: 100}, {195.0: 100}, 203.62) is None                     # OI 太薄
    assert f({50.0: 5000}, {55.0: 5000}, 203.62) is None                     # 偏离 >50%
    mp = f({200.0: 5000, 210.0: 3000}, {195.0: 4000, 190.0: 2000}, 203.62)  # 正常
    assert mp is not None and 180 <= mp <= 215


# ════════════════════════════════════════════════════════════════════════
# v0.45.189：枚举范围本身的守卫
#
# 本文件的扫描是**枚举驱动**的——它禁止什么，完全由 `_iter_prod_py()` 列出
# 哪些文件决定。此前它是 `ROOT.rglob("*.py")`：在生产 checkout
# `~/Desktop/Alpha Hive` 上，`.claude/worktrees/` 下挂着 14 个**嵌套 git
# worktree**（每个是一份停在各自版本的完整仓库副本），实测扫到 2128 个 .py，
# 其中 **1975 个（92%）在 `.claude/` 下**；git 跟踪的只有 340 个。
#
# ⚠️ 为什么当时是绿的、而绿并不等于没事：
#   `_EXCLUDE_DIRS` 用 `any(part in p.parts)` 判**绝对路径**的每一段，于是嵌套
#   副本里的 `tests/` `experiments/` **恰好**被排掉了；但它们根目录下的
#   `data_pipeline.py` / `alpha_hive_daily_report.py` 一个都没排掉。也就是说
#   当时全绿的唯一原因是「碰巧没有哪份陈旧副本里写着 `price = 100.0`」——
#   哪天有一份里有，红会**只出现在生产 checkout 上**（worktree 里没有嵌套
#   worktree），而造成它的人看到的是绿。这正是 v0.45.186 那条守卫的形状：
#   MEMORY `alpha-hive-test-writes-production` 记的「病灶只长在没人看的地方」。
#
# 下面三条测试**必须**用 tmp 树而不是本仓：在 worktree 里跑什么都证明不了，
# 因为病灶只存在于生产 checkout。
# ════════════════════════════════════════════════════════════════════════

_PATHOLOGY = 'price = 100.0\n'


def _build_pathological_tree(tmp_path):
    """造一棵**带病灶**的假仓库，四个样本各自覆盖一条排除理由。

    返回 `tmp_path`。四个文件内容完全相同，区别只在位置——
    因此任何「应被排除的却没排除」都只能由路径过滤解释，不会被内容差异混淆。
    """
    # ① 仓库根的生产文件：**必须被扫到**（否则整个守卫是空跑）
    (tmp_path / "data_pipeline.py").write_text(_PATHOLOGY, encoding="utf-8")
    # ② 病灶：嵌套 worktree 里的陈旧副本，**不许被扫到**
    stale = tmp_path / ".claude" / "worktrees" / "stale-copy-abc123"
    stale.mkdir(parents=True)
    (stale / "data_pipeline.py").write_text(_PATHOLOGY, encoding="utf-8")
    # ③ `_EXCLUDE_DIRS` 的本职：离线代码，**不许被扫到**
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(_PATHOLOGY, encoding="utf-8")
    # ④ 第三方：`_is_ours` 的 vendored 那道过滤，**不许被扫到**
    vend = tmp_path / "libs" / "site-packages" / "pkg"
    vend.mkdir(parents=True)
    (vend / "mod.py").write_text(_PATHOLOGY, encoding="utf-8")
    return tmp_path


def test_scanner_has_teeth(tmp_path):
    """先自证探针有效——扫不到任何东西的扫描器会**恒真地绿**。

    没有这一条，下面那条「嵌套副本不许被扫到」可以靠「什么都扫不到」通过。

    变红的变异：把 `_PATTERNS` 三条正则全删；或让 `_iter_prod_py` 返回空。
    """
    _build_pathological_tree(tmp_path)
    hits = _scan_violations(tmp_path)
    assert any(h.startswith("data_pipeline.py:") for h in hits), (
        f"仓库根那份 `price = 100.0` 都没扫出来，探针坏了，不是「没有违规」：{hits}")


def test_scanner_does_not_cross_into_nested_worktrees(tmp_path):
    """**本版要修的那件事**：枚举不许越界进 `.claude/worktrees/`。

    这条在修之前是红的（`ROOT.rglob` 会把嵌套副本一并扫进来），
    修后绿。两个方向都被钉住：根目录那份必须在、其余三份必须不在。

    变红的变异（任一）：
      * 把 `_iter_prod_py` 的 `own_python_files(root)[0]` 换回 `root.rglob("*.py")`
        —— 样本 ② ④ 漏进来；
      * 把 `_EXCLUDE_DIRS` 那句 `continue` 删掉 —— 样本 ③ 漏进来；
      * 把 `any(part in _EXCLUDE_DIRS for part in rel.parts)` 写回 `p.parts`
        —— 在 tmp 树下 `pytest-of-<user>` 路径段不含排除名，样本 ③ 仍被排除，
        **这条变异在本测试里是等价的**；钉它的是
        `test_exclude_dirs_are_matched_relative_to_root`。
    """
    _build_pathological_tree(tmp_path)
    # 前提：夹具确实走的是 rglob 回退分支（tmp 树不是 git 仓库）——
    # 病灶只存在于这条分支上（嵌套 worktree 从不被外层仓库的索引跟踪，
    # 实测 `git ls-files '*.py' | grep ^.claude` 为 0 条）。
    # 不断言这一点，未来某天夹具悄悄走到 git 分支，这条测的就不是它要测的东西。
    from tests._repo_files import own_python_files
    assert own_python_files(tmp_path)[1] == "rglob", (
        "夹具走到了 git 分支——病灶不在那条分支上，这条测试失去意义")

    hits = _scan_violations(tmp_path)
    assert hits == ["data_pipeline.py:1: price = 100.0"], (
        "枚举范围错了。应当只扫到仓库根那一份；实际：\n  " + "\n  ".join(hits)
        + "\n\n越界扫进 `.claude/worktrees/` 的后果不是「慢」，是**红只在生产 "
          "checkout 上可见**——写代码的人看到绿，唯一会红的那台机器没人看。")


def test_exclude_dirs_are_matched_relative_to_root(tmp_path):
    """`_EXCLUDE_DIRS` 必须按**相对仓库根**的路径段判，不是绝对路径段。

    按绝对路径判时，仓库**被 checkout 到的位置**会改变守卫的覆盖面：
    把仓库放进任何一个名叫 `gui` / `tests` / `experiments` 的目录，
    整条守卫就**静默地扫零个文件**，而它照样是绿的。

    变红的变异：把 `_iter_prod_py` 里的 `rel.parts` 写回 `p.parts`。
    """
    root = tmp_path / "gui" / "checkout"      # 祖先目录叫 `gui`（在排除清单里）
    root.mkdir(parents=True)
    (root / "data_pipeline.py").write_text(_PATHOLOGY, encoding="utf-8")
    hits = _scan_violations(root)
    assert hits == ["data_pipeline.py:1: price = 100.0"], (
        "仓库根的祖先目录叫 `gui`，整个守卫就扫不到任何文件了（绿，但是空跑）。"
        f"实际命中：{hits}")


def test_enumeration_covers_the_real_repo():
    """量级护栏：本仓的生产 .py 是**上百**量级，不是几千也不是零。

    几千 ⇒ 又混进了嵌套 worktree 或第三方库；
    零/个位数 ⇒ 清单机制自己坏了，而坏掉的清单让上面那条主断言恒真地绿。

    变红的变异：把 `_iter_prod_py` 换回 `ROOT.rglob`。

    ⚠️ v0.45.199 更正：**这条变异只在生产 checkout 上让本条变红**
    （那里裸 rglob 是 2741，越过 500 上界）；**在 worktree 里是 153，不会红**。
    实测确认过——原 docstring 写「变红的变异」是过度断言。
    ⭐ 也就是说：**本版要修的那个「红只在生产 checkout 上可见」，
    在守卫自己的文档里又复发了一次。** 处处可红的是
    `test_scanner_does_not_cross_into_nested_worktrees`（tmp 夹具），
    量级护栏是它的补充、不是替代。

    ⚠️ 顺带钉住 rglob 回退分支：无 git 的机器上口径是 153（`_is_ours` 已滤掉
    `.claude/`），仍在 50..500 内 —— 上界不会在那种机器上误红。
    """
    files = list(_iter_prod_py())
    assert 50 < len(files) < 500, (
        f"扫到 {len(files)} 个生产 .py。过多＝混进了嵌套 worktree / 第三方；"
        "过少＝枚举坏了（而坏掉的枚举不会让主断言变红）")
    names = {f.name for f in files}
    assert "alpha_hive_daily_report.py" in names, (
        f"日报主入口不在扫描范围内——守卫覆盖不到生产主路径：{sorted(names)[:20]}")
    # ⚠️ 必须判**相对** ROOT 的路径段。本仓的 worktree 自己就住在
    # `…/Alpha Hive/.claude/worktrees/<name>/`，绝对路径里恒含 `.claude`——
    # 用 `f.parts` 写这条断言，它在每个 worktree 里都恒红，且红的理由是假的。
    leaked = [str(f.relative_to(ROOT)) for f in files
              if ".claude" in f.relative_to(ROOT).parts]
    assert not leaked, f"枚举里还有 `.claude/` 下的文件：{leaked[:5]}"
