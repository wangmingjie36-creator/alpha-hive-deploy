"""元守卫：依赖生产产物的测试，条件性必须写在 marker 上，不许写进 skip（v0.45.165）

为什么需要这条
--------------
v0.45.157 修了 `TestSPYBenchmarkUnavailable`：它挂着
`if not PROD_DB.exists(): pytest.skip()`，而 `pheromone.db` 命中 `.gitignore`
的 `*.db` ⇒ **任何干净检出与 CI runner 上都没有它** ⇒ 三条测试恒 skip；
唯一真跑它们的地方是主 checkout，而一跑就打外网被离线闸判红。
即：**唯一会执行的地方正是唯一会失败的地方**，断言从未被求值过。

v0.45.165 普查全仓，同一形状另有 **20 条**，横跨 5 个文件。最刺眼的一处是
`test_distribution_invariants.py` 的**模块级** `pytestmark = skipif(...)`：它把
`TestGuardsHaveTeeth`（纯合成数据、零外部依赖）一起连坐掉 —— 而那个类的
docstring 写着「没有这一组，本文件的全绿证明不了任何事」。证明守卫有牙的那组，
自己从未被执行过。

判据（CLAUDE.md 硬检查项）
--------------------------
写 `if not X.exists(): pytest.skip()` 之前先答一句：**「X 在哪些环境里存在？」**
若答案是「只有一台机器上的一个目录」，这条测试**等于没有** ——
「加一个 skip 守卫」和「让这条测试在任何地方都跑不到」之间，
只隔着一个未被 git 跟踪的文件。

**同样的条件性，写在 marker 上可见，写在 skip 里不可见**：
`-m integration` 由 addopts 显式排除、由命令行显式选中，默认摘要里是
`N deselected`；而 skip 只在 `-rs` 时吐一行，默认输出里它和 PASSED 一样是个点。

本文件做什么
------------
扫 `tests/` 全部测试模块，找「条件提到 git 忽略的生产产物、却用 skip 表达」的地方。
命中即红，并给出该改成 marker 的位置。

⚠️ 这条守卫自己也必须有牙 —— 见 `TestDetectorHasTeeth`：正反两个方向各喂一次
（该抓的抓到、不该抓的不抓）。只验一个方向的检测器，证明不了它在工作
（CLAUDE.md：探针/检测器两个方向都要自证）。
"""

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# git 忽略的生产产物 —— 「只在一台机器上存在」的那些。
# 口径不是「文件名」而是「它在哪些环境里存在」：这些全部命中 .gitignore
# （`*.db` / `cache/` / `.swarm_results_*`），因此在干净检出与 CI 上恒不存在。
# ① 路径型：指向具体文件，由 `git check-ignore` 逐个校验（见文件末尾那条测试）。
PROD_PATH_TOKENS = {
    "pheromone.db": "pheromone.db",
    "PROD_DB": "pheromone.db",              # 常量名，探它指向的那个文件
    ".swarm_results": ".swarm_results_TEST.json",
    "vix_history_cboe": "cache/vix_history_cboe.csv",
}

# ② 理由文本型：子进程以 `cwd=<仓库根>` 跑生产脚本时，一个文件名 token 都不会
# 出现在函数里 —— 依赖藏在 cwd 里，只有 skip 的理由字符串泄露了它。
# 这类没有对应路径可校验，由 `TestDetectorHasTeeth` 的正反两条覆盖。
PROD_REASON_TOKENS = ("生产库", "生产 pheromone")

PROD_ARTIFACT_TOKENS = tuple(PROD_PATH_TOKENS) + PROD_REASON_TOKENS
# ⚠️ 名单里**不放** `paper_portfolio_state` / `ml_model_history` —— 它们是
# **被 git 跟踪**的（MEMORY.md：新增 `*_state/` 要同 PR `git add`），
# 干净检出里确实存在，挂在它们上面的 skip 不属于本 species。
# 这两条是下面那个 token 校验测试**第一次跑就抓出来**的：6 个 token 里错了 2 个。
# 名单靠人记会腐化，所以让 `git check-ignore` 说了算，不让写名单的人说了算。
#
# 注意 `cache/` 目录本身在干净检出里**是存在**的（有被跟踪的文件住在里面），
# 但 `cache/vix_history_cboe.csv` 确实被忽略 —— 口径必须落到**具体文件**，
# 不能看目录在不在。

# 本文件自己会大量引用上面那些词（docstring / 常量 / 合成样例），扫描时跳过。
SELF = Path(__file__).name


def _has_integration_mark(decorators) -> bool:
    """装饰器里有没有 `@pytest.mark.integration`。"""
    for d in decorators:
        node = d.func if isinstance(d, ast.Call) else d
        if isinstance(node, ast.Attribute) and node.attr == "integration":
            return True
    return False


def invisible_prod_skips(source: str, filename: str = "<test>") -> list[str]:
    """返回「条件依赖生产产物、却用 skip 表达」的位置列表。

    命中两种写法：
      · 模块级 `pytestmark = pytest.mark.skipif(<提到生产产物>)` —— 一律算，
        因为它连坐整个文件，包括与该条件无关的测试。
      · 函数体内 `pytest.skip(...)` / 装饰器 `@pytest.mark.skipif(...)`，
        且所在函数与其所在类都没标 `integration`。
    """
    tree = ast.parse(source, filename=filename)
    hits: list[str] = []

    def mentions_prod(node) -> bool:
        seg = ast.get_source_segment(source, node) or ""
        return any(tok in seg for tok in PROD_ARTIFACT_TOKENS)

    # ① 模块级 pytestmark
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark"
                   for t in node.targets):
            continue
        seg = ast.get_source_segment(source, node) or ""
        if "skipif" in seg and any(tok in seg for tok in PROD_ARTIFACT_TOKENS):
            hits.append(
                f"{filename}:{node.lineno} 模块级 pytestmark=skipif 依赖生产产物"
                " —— 它连坐整个文件（含与该条件无关的测试）；"
                "改成给读生产库的类逐个标 @pytest.mark.integration"
            )

    # ② 函数级：skip 调用与 skipif 装饰器
    for cls in [None] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        scope = tree.body if cls is None else cls.body
        cls_marked = cls is not None and _has_integration_mark(cls.decorator_list)
        cls_name = "" if cls is None else f"{cls.name}::"
        for fn in scope:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if cls_marked or _has_integration_mark(fn.decorator_list):
                continue        # 条件性已经写在 marker 上 —— 正是我们要的形状
            for dec in fn.decorator_list:
                if mentions_prod(dec) and "skipif" in (
                        ast.get_source_segment(source, dec) or ""):
                    hits.append(
                        f"{filename}:{dec.lineno} {cls_name}{fn.name} "
                        "的 @skipif 依赖生产产物，却没标 integration"
                    )
            # 口径是**整个函数体**，不是「skip 调用 + 它外面那个 if」。
            # 后者漏掉两种真实写法（v0.45.165 实测，检测器第一版就漏了这两个）：
            #   · `files = Path('.').glob('.swarm_results_*'); if not files: skip()`
            #     —— 产物名在赋值语句上，不在 if 上
            #   · `subprocess.run(..., cwd=_ROOT); if '无可用样本' in out: skip()`
            #     —— 依赖是「子进程的 cwd 指向仓库根」，一个 token 都不出现
            # 所以：函数体里出现生产产物 **且** 有 skip **且** 没标 integration ⇒ 报。
            if not mentions_prod(fn):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "skip"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "pytest"):
                    continue
                hits.append(
                    f"{filename}:{node.lineno} {cls_name}{fn.name} "
                    "的 pytest.skip 所在函数依赖生产产物，却没标 integration —— "
                    "干净检出与 CI 上它恒 skip，断言从未被求值"
                )
    return hits


class TestNoInvisibleProdDataSkips:
    def test_no_test_module_hides_prod_dependency_in_a_skip(self):
        offenders: list[str] = []
        scanned = 0
        for f in sorted(TESTS_DIR.glob("test_*.py")):
            if f.name == SELF:
                continue
            scanned += 1
            offenders += invisible_prod_skips(
                f.read_text(encoding="utf-8"), f.name)
        # 成对断言：「没抓到 offender」必须配「确实扫到了文件」，
        # 否则 glob 写错、扫了 0 个文件也会全绿（CLAUDE.md：断言要成对）。
        assert scanned > 50, f"只扫到 {scanned} 个测试模块 —— glob 可能写错了"
        assert not offenders, (
            "以下测试把「依赖生产产物」这件事藏在 skip 里（默认输出不可见），"
            "应改标 @pytest.mark.integration：\n  " + "\n  ".join(offenders)
        )


class TestDetectorHasTeeth:
    """正反两个方向各喂一次 —— 只验一个方向的检测器证明不了它在工作。"""

    BAD_MODULE_LEVEL = (
        "import pytest\n"
        "from pathlib import Path\n"
        "PROD_DB = Path('pheromone.db')\n"
        "pytestmark = pytest.mark.skipif(not PROD_DB.exists(), reason='x')\n"
        "def test_a():\n"
        "    assert True\n"
    )
    BAD_INLINE = (
        "import pytest\n"
        "def test_b():\n"
        "    if not PROD_DB.exists():\n"
        "        pytest.skip('生产 pheromone.db 不存在')\n"
        "    assert True\n"
    )
    GOOD_MARKED = (
        "import pytest\n"
        "@pytest.mark.integration\n"
        "class TestX:\n"
        "    def test_c(self):\n"
        "        assert PROD_DB.exists()\n"
    )
    GOOD_UNRELATED_SKIP = (
        "import pytest\n"
        "def test_d():\n"
        "    if not HAS_SCHEDULE_LIB:\n"
        "        pytest.skip('schedule 库不可用')\n"
        "    assert True\n"
    )

    def test_catches_module_level_pytestmark(self):
        """本次真正的元凶形状：模块级 skipif 连坐整个文件。"""
        hits = invisible_prod_skips(self.BAD_MODULE_LEVEL, "bad.py")
        assert hits and "模块级" in hits[0], hits

    def test_catches_inline_skip(self):
        hits = invisible_prod_skips(self.BAD_INLINE, "bad.py")
        assert hits and "pytest.skip" in hits[0], hits

    def test_integration_marked_is_not_flagged(self):
        """条件性写在 marker 上 = 正确形状，不得误报。"""
        assert invisible_prod_skips(self.GOOD_MARKED, "good.py") == []

    def test_unrelated_skip_is_not_flagged(self):
        """与生产产物无关的 skip（缺 python 包等）不在管辖范围。"""
        assert invisible_prod_skips(self.GOOD_UNRELATED_SKIP, "good.py") == []

    def test_catches_token_outside_the_if(self):
        """产物名在**赋值语句**上、不在 `if` 上 —— 检测器第一版漏的形状之一。"""
        src = (
            "import pytest\n"
            "def test_e():\n"
            "    files = list(root.glob('.swarm_results_*.json'))\n"
            "    if not files:\n"
            "        pytest.skip('无扫描结果')\n"
            "    assert files\n"
        )
        assert invisible_prod_skips(src, "bad.py"), "赋值语句里的产物名没被看到"

    def test_catches_dependency_hidden_in_subprocess_cwd(self):
        """依赖藏在子进程的 cwd 里，一个文件名 token 都不出现 —— 靠理由文本兜底。"""
        src = (
            "import pytest\n"
            "def test_f():\n"
            "    out = subprocess.run(cmd, cwd=_ROOT).stdout\n"
            "    if '无可用样本' in out:\n"
            "        pytest.skip('生产库无到期样本')\n"
            "    assert '功效' in out\n"
        )
        assert invisible_prod_skips(src, "bad.py"), "理由文本里的『生产库』没被看到"


@pytest.mark.parametrize("token,probe", sorted(PROD_PATH_TOKENS.items()))
def test_every_listed_token_is_actually_gitignored(token, probe):
    """名单的口径是「git 忽略 ⇒ 只在生产机上存在」，不是「名字看着像产物」。

    这条防的是名单腐化：有人把一个**被跟踪**的文件加进来，检测器就会开始误报，
    进而被整体关掉。以实际判定为准，不靠人记 —— 第一次跑就抓出 6 个 token 里
    有 2 个（`paper_portfolio_state` / `ml_model_history`）其实是被跟踪的。

    ⚠️ `git check-ignore` 的退出码有**三**个含义，别把它们揉成一个：
      0 = 被忽略（要的就是这个）  1 = 未被忽略（名单错了）  128 = 这里不是 git 仓库
    第一版把 128 也判成「名单错了」，于是在 `git archive` 出来的干净树里
    报了一句与真实原因无关的错 —— 正是本文件在治的那个形状（把一种失败
    渲染成另一种）。非仓库环境改走第二条断言：那里的不变式可以**直接验**，
    因为「干净检出里不存在」本来就是这份名单要表达的意思。
    """
    import subprocess
    root = TESTS_DIR.parent
    out = subprocess.run(["git", "check-ignore", "-q", probe],
                         cwd=root, capture_output=True)

    if out.returncode == 128:                       # 这里不是 git 仓库
        # `.gitignore` 的判定**只能靠 git**，非仓库里无从检查 —— 这不是可以
        # 换个断言绕过去的事（第一版试过「非仓库 ⇒ 该文件不该存在」，
        # 结果在「非 git 树 + 手工拷进生产产物」的验证环境里假红：
        # 那是把「不是 git 仓库」错当成了「干净检出」，正是本文件在治的形状）。
        #
        # 这条 skip 是正当的，因为它过得了那句判据 ——
        # 「git 仓库在哪些环境里存在？」答：**开发检出 / worktree / CI 的
        # actions-checkout 全都是**；唯一不是的只有手工 `git archive` 出来的
        # 临时树。条件为真的环境是全集减一个人造特例，不是「只有一台机器」。
        pytest.skip("不在 git 仓库里（git archive 出来的树）——"
                    "check-ignore 无从判定；真实环境一律是仓库")

    assert out.returncode in (0, 1), (
        f"git check-ignore 退出码 {out.returncode}（预期 0/1/128）："
        f"{out.stderr.decode(errors='replace')[:200]}"
    )
    assert out.returncode == 0, (
        f"{probe} 并未被 .gitignore 忽略 —— 它在干净检出里是**存在**的，"
        f"不该出现在 PROD_PATH_TOKENS 里（token={token!r}）"
    )
