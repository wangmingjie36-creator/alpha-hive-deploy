"""元守卫：CHANGELOG.md 每个版本号恰好一条条目，且条目必须有正文（v0.45.195）

治的形状
--------
v0.45.193 修的那次：`## [0.45.172] — 2026-09-09 — EVALUATION_WEIGHTS 改写（…）`
**连续两行逐字节相同**，上面那条无正文（下一非空行就是第二条标题），正文挂在
下面那条上。考古（`git log -S` 数标题出现次数）定位到 `389a451`（写入 v0.45.173
的提交）：那次把自己的占位标题换成真标题、补正文时，连带把相邻的 172 标题
多吐了一份。

重复在 `main` 上从 2026-09-09 躺到 2026-09-11，**是人眼发现的**。
按 CLAUDE.md 那条判据「**谁会红？**」——没人。本文件就是那个会红的人。

影响面（别读大了）
------------------
生产读取口径 `code_version.changelog_version()` **取首个匹配即返回**，所以
「在跑的是哪一版代码」这个上报从未出错。受影响的只有**全文件枚举**：按版本号
去重的扫描、考古脚本、以及人。

这一句不是 docstring 里的自我安慰——`TestProductionReaderIsUnaffected` 直接
喂一份带重复的 CHANGELOG 验过。写成断言而不是散文，是因为散文不会被执行：
本仓反复栽的正是「文档里写着的保证，从来没人求值过」。

为什么值得一条守卫
------------------
MEMORY.md 指定 `git show origin/main:CHANGELOG.md | grep -m1 '^## \\['` 为
「下一个可用版本号」的唯一真相，而 CLAUDE.md「并发开工必须先占号」整套协议
建立在**一个号一条条目**之上。号重了，占号协议就失去仲裁能力。

这本身就是占号工作流的一个失败形状：**替换占位标题 + 补正文时，可能复制到
相邻条目的标题**。它与 CLAUDE.md 已记录的「撞号」是同一工作流的不同故障——
撞号是两个 session 抢同一个号，本条是一个 session 把一个号写了两遍。

两条断言，各覆盖一类畸形
------------------------
① 版本号不重复；② 每条 `## [x.y.z]` 标题下必须有正文。

本次的畸形同时踩中两条，单靠 ① 也能抓到。但二者**不可互相替代**，
`TestBothAssertionsAreNeeded` 各喂一个只有一方抓得到的样本：
  · 两条同号、**都有正文** ⇒ 只有 ① 够得到
  · 一条空标题、**号不重复** ⇒ 只有 ② 够得到
⚠️ 还有一层：占位条目对 ② 是**豁免**的（见下），所以万一重复的恰好是两条
占位标题，② 结构上够不到，那时只剩 ① 在守。这正是不能只留一条的理由。

⚠️ ① 稳，② 脆——别把两条当等价
------------------------------
① 是结构性的：版本号重复就是重复，没有别的写法能掩盖。
② 是 **best-effort 补充**：它只能看见「标题下什么都没有」这一种畸形，
两条重复标题之间只要夹进**任意一行真正文**，② 就测不出来。
v0.45.172 那次恰好是空标题，但那是运气，不是 ② 的能力边界证明。
**真正兜底的是 ①。** 谁要精简这个文件，请删 ② 留 ①，别反过来。

占位豁免为什么必须有
--------------------
`## [0.45.19x] — … — 占位（进行中：…）` 是并发协议**要求**的形态
（CLAUDE.md「并发开工必须先占号」第 2 步只要求插入**一行标题**，
不要求正文）——所以**裸的单行占位是合法状态**。

这里的数字依赖于「什么算正文」这个口径，必须连口径一起报，否则就是个会骗人的
快照。实测 origin/main @ `e54ca02`（337 条标题）：

  · 窄口径（`---` **算**正文，即只认「下一非空行又是标题」）⇒ 无正文 **0 条**
  · 本文件的宽口径（`---` **不算**正文，见 `_is_rule`）⇒ 无正文 **2 条**
    （`0.45.195` / `0.45.190`），**两条都是占位，非占位 0 条**

也就是说：**豁免之所以此刻就是必需的，是因为本文件选了宽口径**。
窄口径下它只是前瞻性的保险。两种口径下这条豁免都该有——区别只在于
「现在就会红」还是「迟早会红」，而宽口径换来的是能抓住
`标题 / --- / 标题`（见 `test_horizontal_rule_is_not_counted_as_body`）。

⚠️ 豁免**只验合成样本，不验当天的真文件**：仓库里此刻有没有占位条目是
一个**瞬时工作流状态**，不是不变式。拿它当证据，等于断言「任何时刻都有人
正在开工」——安静的那天就会假红。见 `test_placeholder_is_exempt_from_body_check`。
（这一节的前身写反过了：曾把「当前 main 上就有占位条目」当成豁免生效的证据。
由写 v0.45.193 那个 session 二次核查时发现并更正。）

考虑过但**不加**第三条：版本号单调
----------------------------------
「相邻条目版本号必须递减」会**当场红**：origin/main 上有 5 处既存逆序，
均为同日成对，全部早于 v0.45.193——`0.45.17/0.45.18`(L14356)、
`0.45.9/0.45.11`(L14797)、`0.42.4/0.42.5`(L17306)、`0.29.1/0.29.3`(L18354)、
`0.17.0/0.17.1`(L19950)。已独立复算确认。
而 MEMORY.md 真正依赖的不变式是**顶部 == 全局最大**，这一条成立
（顶部 `0.45.195` 即全局最大），故 `grep -m1` 是安全的。

两条口径上的选择
----------------
1. **正则不另写一套**：直接复用 `code_version._CHANGELOG_RE`（CLAUDE.md
   「不要手搓已有的东西」）。同一个文件被两套正则读，早晚漂移，而漂移那一刻
   两边都还是绿的。
2. **路径锚点用 `code_version._repo_dir()`**，不用 `PATHS.home`。CHANGELOG.md
   是随代码发布的**只读文档 = 代码**，不是数据（CLAUDE.md「这个路径指向代码
   还是数据？」）。`_repo_dir()` 由 `code_version.__file__` 派生，因此天然只
   读得到**本 worktree** 的那一份；`test_guard_reads_this_worktrees_changelog`
   把这件事钉死，免得守卫哪天去审别的 checkout。
   （`tests/` 被 `test_paths_not_frozen_at_import.py::_scan` 显式排除，故本文件
   不需要登记进 `MUST_STAY_FILE_ANCHORED`——已核对该处第 237 行的排除清单。）
"""

from __future__ import annotations

import collections
import re
from pathlib import Path
from typing import NamedTuple

# 复用生产口径：正则与仓库锚点都只有这一份真相。
from code_version import _CHANGELOG_RE, _repo_dir

_TESTS_DIR = Path(__file__).resolve().parent
_ROOT = _TESTS_DIR.parent

#: 标准 markdown 水平线的字符集。本仓当前只用 `---`（实测 173 处、无第二种），
#: 但把 `***` / `___` 一并认掉是零成本的——漏认一种就会把分隔线当成正文，
#: 于是 `标题 / *** / 标题` 这种畸形被静默放过。
_RULE_CHARS = set("-*_")

#: 占位条目的标志物。并发协议规定的写法见 CLAUDE.md「并发开工必须先占号」。
_PLACEHOLDER_MARK = "占位"

#: git 冲突标记：**恰好 7 个**字符，后接空格或行尾。
#: `<<<<<<< HEAD` / `=======` / `>>>>>>> origin/main` / `||||||| merged common ancestors`（diff3）
_CONFLICT_RE = re.compile(r"^(?:<{7}|={7}|>{7}|\|{7})(?=[ \t]|$)")

#: 全文件枚举的下限。成对断言用：「没抓到畸形」必须配「确实解析到了条目」，
#: 否则正则失配、解析出 0 条也会全绿。2026-09-11 实测 337 条，只增不减；
#: 真要把历史条目归档拆分，这条会红——那时该来人看一眼，不是调低阈值。
_MIN_ENTRIES = 100


class Entry(NamedTuple):
    lineno: int          # 1-based，报错时人要拿它去 `sed -n`
    version: str
    title: str
    has_body: bool

    @property
    def is_placeholder(self) -> bool:
        return _PLACEHOLDER_MARK in self.title


def _is_rule(line: str) -> bool:
    """这行是不是水平线（`---`）。

    ⚠️ 水平线**不算正文**。算了的话，`标题 / --- / 标题` 这种畸形就会被判成
    「有正文」而放过；而本仓 CHANGELOG 恰好每条条目之间都隔着一条 `---`，
    也就是说：把分隔线当正文，断言 ② 会对**绝大多数**形态失去判别力。
    """
    s = line.strip()
    return len(s) >= 3 and set(s) <= _RULE_CHARS


def parse_entries(text: str) -> list[Entry]:
    """把 CHANGELOG 正文解析成条目列表。**纯函数**，喂字符串不碰磁盘。

    纯函数是刻意的：牙齿测试要喂十来个畸形样本，走磁盘就得造临时文件，
    而临时文件一旦造在仓库里，就成了「跑测试写穿生产产物」那一族
    （MEMORY `alpha-hive-test-writes-production`）。

    ⚠️ 已知边界：口径是**行首** `## [`，所以**代码围栏里顶格写的**
    `## [x.y.z]` 会被算成标题，可能报出一条假重号。当前文件零命中
    （两处非行首出现都在行内，L22/L7304）。真撞上了有两种情况：要么把那行
    缩进两格，要么它本来就是一条该被发现的真重复 —— 先看一眼再决定。
    **刻意没做围栏跟踪**：为一个尚未出现的形态加状态机，换来的是一个更难
    自证的解析器，不划算。
    """
    lines = text.splitlines()
    heads = [(i, ln) for i, ln in enumerate(lines) if _CHANGELOG_RE.match(ln)]
    out: list[Entry] = []
    for n, (i, title) in enumerate(heads):
        stop = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        has_body = any(ln.strip() and not _is_rule(ln) for ln in lines[i + 1:stop])
        out.append(Entry(
            lineno=i + 1,
            version=_CHANGELOG_RE.match(title).group(1),
            title=title,
            has_body=has_body,
        ))
    return out


def duplicate_versions(text: str) -> list[str]:
    """断言 ①：同一个版本号出现在多条标题上。占位与否一律算。"""
    by_version: dict[str, list[int]] = collections.defaultdict(list)
    for e in parse_entries(text):
        by_version[e.version].append(e.lineno)
    dups = [(lines[0], ver, lines)
            for ver, lines in by_version.items() if len(lines) > 1]
    return [f"[{ver}] 出现 {len(ls)} 次，行 "
            + ", ".join(str(x) for x in ls)
            for _, ver, ls in sorted(dups)]


def bodyless_entries(text: str) -> list[str]:
    """断言 ②：标题下没有正文（下一个有意义的行又是标题）。占位条目豁免。"""
    return [f"L{e.lineno} {e.title.strip()[:80]}"
            for e in parse_entries(text)
            if not e.has_body and not e.is_placeholder]


def conflict_markers(text: str) -> list[str]:
    """断言 ③：文件里有没有**未解决的合并冲突标记**。

    ⚠️ 两个约束都不是装饰，各挡一类误报：

    1. **必须锚定行首。** 不锚行首就会命中**正在讨论冲突标记**的条目 ——
       本仓 L7500/7501 就有一条，记的正是「一份含冲突标记的 CHANGELOG 被推上
       main、存活约 3 分钟」那次事故。v0.45.200 那个 session 用
       `"<<<<<<<" not in s` 做解冲突自检，命中的就是那两行散文。
       「**在讨论 X**」被误判成「**是 X**」—— 本仓文本探针第四次栽在这上面。
    2. **必须要求「恰好 7 个」**（靠 `(?=[ \t]|$)` 断住）。否则 `={7}` 会吃掉
       markdown 的 setext H1 下划线（`=====…`，长度任意）。本仓实测零行首全 `=`
       的行、风险当下为 0，但判据不该靠「本仓碰巧不用 setext 标题」撑着 ——
       那是内容依赖，不是结构性（v0.45.195 补记 1 的判据）。
    """
    return [f"L{i} {line.rstrip()[:60]}"
            for i, line in enumerate(text.splitlines(), 1)
            if _CONFLICT_RE.match(line)]


def _real_changelog_text() -> str:
    return (_repo_dir() / "CHANGELOG.md").read_text(encoding="utf-8")


class TestChangelogEntryIntegrity:
    """对**本 worktree 真实的** CHANGELOG.md 求值的三条。"""

    def test_guard_reads_this_worktrees_changelog(self):
        """元守卫：先证明锚点指对了，否则下面两条证明不了任何事。

        `_repo_dir()` 由 `code_version.__file__` 派生。这条钉死它 == 本 worktree
        的仓库根，堵两件事：① 守卫跑去审别的 checkout（生产目录的
        `.claude/worktrees/` 下挂着十个完整副本，见 `tests/_repo_files.py` 那次
        「病灶只长在没人看的地方」）；② `sys.path` 上有另一份 `code_version.py`
        把它影子掉。

        变异：把 `_real_changelog_text()` 改成读 `PATHS.home / "CHANGELOG.md"`
        ⇒ `_isolate_env` 已把 HOME 指到 tmp ⇒ 文件不存在 ⇒ 红。
        """
        assert _repo_dir() == _ROOT, (
            f"守卫的仓库锚点 {_repo_dir()} 不是本 worktree 的根 {_ROOT} —— "
            "它正在读另一份 checkout 的 CHANGELOG，本文件其余断言全部作废")
        assert (_ROOT / "CHANGELOG.md").is_file(), "本 worktree 里没有 CHANGELOG.md"

    def test_no_unresolved_conflict_markers(self):
        """③ 不许有未解决的合并冲突标记。**本条必须定义在 ① 之前。**

        位置是语义的一部分：`pyproject.toml` 的 addopts 带 `-x`，**谁先红，
        谁就是人看到的那句诊断**。一份含冲突标记的 CHANGELOG 会让 ① 报
        「重复的版本号标题」—— 但**真因是冲突，重号只是症状**。
        那正是 CLAUDE.md 记的「把一种失败误报成另一种」。

        不是假想：`4b5e4d9` 是一份**真被推上 main** 的含冲突标记 CHANGELOG
        （存活约 3 分钟）。把它喂给本文件的 ①，报的是
        `[0.45.120] 出现 2 次，行 109, 111` —— 109/111 正好骑在第 110 行那条
        `=======` 的两侧。本 session 自己合并 origin/main 时也复现过一次。

        ⚠️ 刻意**不**去读 `4b5e4d9` 那份历史文件来做断言：那会让这条测试依赖
        git 历史存在（浅克隆 / `git archive` 出来的树就没有），等于给自己埋一条
        「只在某些机器上跑得到」的测试（CLAUDE.md skip 守卫那节）。
        历史文件只用于开发期核对，常驻断言喂的是合成夹具。
        """
        hits = conflict_markers(_real_changelog_text())
        assert not hits, (
            "CHANGELOG.md 里有**未解决的合并冲突标记**：\n  " + "\n  ".join(hits)
            + "\n\n先解冲突再谈别的 —— 下面那条「重复的版本号标题」多半只是"
              "它的症状（冲突两侧各带一份标题）。\n"
              "前科：`4b5e4d9` 就是一份含冲突标记、真被推上 main 的 CHANGELOG。")

    def test_no_duplicate_version_headings(self):
        """① 一个版本号只许有一条条目。

        变异：`tests/` 外插一条重复标题即红（本 session 在真文件上实测过，
        见 CHANGELOG v0.45.195；`TestDetectorHasTeeth` 用合成样本常驻覆盖）。
        """
        text = _real_changelog_text()
        entries = parse_entries(text)
        # 成对断言：没抓到畸形，必须配「确实解析到了东西」。
        assert len(entries) >= _MIN_ENTRIES, (
            f"只解析到 {len(entries)} 条 `## [x.y.z]` 标题（预期 ≥{_MIN_ENTRIES}）——"
            "正则或文件结构变了，本条此刻是恒真的")
        dups = duplicate_versions(text)
        assert not dups, (
            "CHANGELOG.md 里有重复的版本号标题：\n  " + "\n  ".join(dups)
            + "\n\n最可能的成因（v0.45.193 实测）：把自己的占位标题换成真标题、"
              "补正文时，连带复制了**相邻条目**的标题。\n"
              "MEMORY.md 指定 `grep -m1 '^## \\['` 为下一个可用号的唯一真相，"
              "而占号协议建立在「一个号一条条目」之上——号重了它就失去仲裁能力。\n"
              "处置：`git log -S '<那条标题>' -- CHANGELOG.md` 数出现次数定位引入的提交，"
              "删掉无正文的那条，保留挂着正文的那条。\n"
              "⚠️ 若 `test_no_unresolved_conflict_markers` 同时红了，**先看那条** —— "
              "本条多半只是它的症状（冲突两侧各带一份标题），真因是冲突没解完。")

    def test_every_entry_has_body(self):
        """② 每条标题下必须有正文；占位条目豁免。

        **这是 best-effort 补充，不是与 ① 等价的第二道锁**：两条重复标题之间
        只要夹进任意一行真正文，本条就测不出来。真正兜底的是 ①。

        豁免不是网开一面：占位条目按并发协议**就该**没有正文
        （CLAUDE.md「并发开工必须先占号」），不豁免会让守卫在任何有人开工的
        时刻恒红，而恒红的守卫会被关掉。
        """
        text = _real_changelog_text()
        entries = parse_entries(text)
        assert len(entries) >= _MIN_ENTRIES, (
            f"只解析到 {len(entries)} 条标题（预期 ≥{_MIN_ENTRIES}），本条此刻是恒真的")
        # ⚠️ 这里**刻意不**断言「当前文件里存在占位条目」。
        # 那是个瞬时工作流状态（没人开工时就该是 0 条），拿它当豁免生效的证据
        # 等于断言「任何时刻都有人正在开工」，安静的那天会假红。
        # 豁免分支是否还活着，由 `test_placeholder_is_exempt_from_body_check`
        # 用**合成样本**常驻证明——那才是不变式。
        empty = bodyless_entries(text)
        assert not empty, (
            "以下 `## [x.y.z]` 标题下没有正文（下一个有意义的行又是标题或水平线）：\n  "
            + "\n  ".join(empty)
            + "\n\n要么是重复标题残留（删掉无正文的那条），"
              "要么是条目正文忘了补。\n"
              "若这是并发协议的占位条目，标题里要带「" + _PLACEHOLDER_MARK + "」二字。")


class TestBothAssertionsAreNeeded:
    """两条断言各喂一个**只有己方抓得到**的样本。

    没有这一组，「加两条断言」就只是看着更周全——实际可能其中一条永远
    被另一条盖住，删掉也没人发现（本仓 v0.45.168 实测过这种「守卫其实是
    冗余的」形态：反向改 5 处，4 处全绿）。
    """

    DUP_BOTH_HAVE_BODY = (
        "## [0.1.0] — d — 甲\n\n正文甲。\n\n---\n\n"
        "## [0.1.0] — d — 乙\n\n正文乙。\n"
    )
    BODYLESS_UNIQUE_VERSION = (
        "## [0.2.0] — d — 甲\n\n---\n\n"
        "## [0.1.0] — d — 乙\n\n正文乙。\n"
    )

    def test_duplicate_with_bodies_is_caught_only_by_assertion_1(self):
        """两条同号、都有正文 ⇒ ② 够不到，只有 ① 抓得住。"""
        assert duplicate_versions(self.DUP_BOTH_HAVE_BODY)
        assert bodyless_entries(self.DUP_BOTH_HAVE_BODY) == [], (
            "这个样本本该是「② 够不到」的那类，它却被 ② 抓到了——"
            "样本选错了，本条证明不了两条断言相互独立")

    def test_bodyless_unique_version_is_caught_only_by_assertion_2(self):
        """一条空标题、号不重复 ⇒ ① 够不到，只有 ② 抓得住。"""
        assert bodyless_entries(self.BODYLESS_UNIQUE_VERSION)
        assert duplicate_versions(self.BODYLESS_UNIQUE_VERSION) == [], (
            "这个样本本该是「① 够不到」的那类，它却被 ① 抓到了——"
            "样本选错了，本条证明不了两条断言相互独立")


class TestDetectorHasTeeth:
    """正反两个方向各喂一次——只验一个方向的检测器证明不了它在工作。"""

    # v0.45.193 的元凶形状：逐字节相同、相邻、只隔一个空行、上面那条无正文。
    THE_REAL_ONE = (
        "## [0.45.172] — 2026-09-09 — EVALUATION_WEIGHTS 改写（用户明确决策）\n"
        "\n"
        "## [0.45.172] — 2026-09-09 — EVALUATION_WEIGHTS 改写（用户明确决策）\n"
        "\n"
        "### 背景\n\n正文在这一条上。\n"
    )
    PLACEHOLDER_ONLY = (
        "## [0.45.195] — 2026-09-11 — 占位（进行中：某件事）\n\n---\n\n"
        "## [0.45.194] — 2026-09-11 — 真标题\n\n正文。\n"
    )
    HEALTHY = (
        "## [0.2.0] — d — 甲\n\n正文甲。\n\n---\n\n"
        "## [0.1.0] — d — 乙\n\n正文乙。\n"
    )
    RULE_ONLY_BETWEEN = (
        "## [0.2.0] — d — 甲\n\n---\n\n"
        "## [0.1.0] — d — 乙\n\n正文乙。\n"
    )
    VERSION_IN_BODY = (
        "## [0.2.0] — d — 甲\n\n正文里引用了 ## [0.1.0] 这个号。\n"
        "  ## [0.3.0] 缩进的也不算标题。\n"
    )

    def test_catches_the_actual_v45193_shape(self):
        """本次真正的元凶：两条断言应当**同时**抓到它。"""
        assert duplicate_versions(self.THE_REAL_ONE), "重复标题没被发现"
        assert bodyless_entries(self.THE_REAL_ONE), "无正文的那条没被发现"

    def test_healthy_changelog_is_not_flagged(self):
        """正常形态不得误报——误报的守卫会被整体关掉。"""
        assert duplicate_versions(self.HEALTHY) == []
        assert bodyless_entries(self.HEALTHY) == []

    def test_placeholder_is_exempt_from_body_check(self):
        """占位条目无正文是**协议要求**的形态，不得报。"""
        assert bodyless_entries(self.PLACEHOLDER_ONLY) == [], (
            "占位条目被判成了畸形——这条守卫会在任何有人开工的时刻恒红")
        assert duplicate_versions(self.PLACEHOLDER_ONLY) == []

    def test_placeholder_is_not_exempt_from_duplicate_check(self):
        """豁免只覆盖 ②。两条占位同号，① 必须照抓不误。"""
        dup_placeholders = (
            "## [0.45.195] — d — 占位（进行中：甲）\n\n---\n\n"
            "## [0.45.195] — d — 占位（进行中：乙）\n\n---\n"
        )
        assert duplicate_versions(dup_placeholders), (
            "两条占位共用一个号没被发现——占号协议的仲裁能力正是靠 ① 兜底，"
            "而 ② 对占位条目结构上够不到")

    def test_horizontal_rule_is_not_counted_as_body(self):
        """`标题 / --- / 标题` 必须算无正文。

        这条盯的是 `_is_rule`。把它改成恒 `False`（即水平线算正文）⇒ 本条红，
        而 `test_catches_the_actual_v45193_shape` **不会红**（那个样本里两条
        标题之间只有空行、没有水平线）——所以这条不是冗余的。
        """
        assert bodyless_entries(self.RULE_ONLY_BETWEEN), (
            "水平线被当成了正文——本仓每条条目之间都隔着一条 `---`，"
            "这等于让断言 ② 对绝大多数形态失去判别力")

    def test_only_line_start_headings_count(self):
        """正文里引用的版本号、缩进的伪标题都不算——口径来自 `_CHANGELOG_RE`。

        ⚠️ 变异实测纠正了一条我**先写下、后才跑**的说法。原话是「把正则的 `^`
        去掉 ⇒ 红」，**实跑是全绿**：`re.match()` 本来就只从字符串起点匹配，
        而本文件与 `code_version.changelog_version()` 都是**逐行** `.match()`，
        `^` 在这条路径上是**冗余**的。四象限实测：

            ^ + .match()   → ['0.2.0']                    安全
            ^ + .search()  → ['0.2.0']                    安全（^ 独立够用）
            无^ + .match() → ['0.2.0']                    安全（.match 独立够用）
            无^ + .search()→ ['0.2.0','0.1.0','0.3.0']    **破**

        所以护住行首锚定的是 `^` **与** `.match()` 的**合取**，两者互为冗余，
        单独改任一个都不会红（实测 A：只把 `.match` 换 `.search` ⇒ 13 passed）。
        **能让本条红的变异是：去掉 `^` 且把 `.match` 换成 `.search`**（实测 B）。

        ⚠️ B 让两条测试变红，但**两条的性质不同，不许混着说**。判据由 v0.45.193
        那个 session 复核时提出：**「X 会让它红」必须分清红的是「结构」还是
        「当下内容」** —— 前者永真，后者随文件漂，而 CHANGELOG 每天被十几个
        session 追加。分开看：
          · **本条是结构性的**：夹具 `VERSION_IN_BODY` 自带、把非行首出现放在
            第 2/3 行、断言比的是**整份列表**，与真 CHANGELOG 当天长什么样无关。
          · `test_no_duplicate_version_headings` 在 B 下的红是**内容依赖**的：
            它只是因为正文里恰好写过两处 `## [x.y.z]`（L22/L7304）。实测把那两行
            删掉再跑 B ⇒ 重号归零、那条转绿。**别把它算进「B 的牙」。**

        ⚠️ 再澄清一个易混点：同一个 B 变异下 `code_version.changelog_version()`
        **不红**。它取**首个**匹配即 return，而第一条真标题（行 8）排在那两处
        散文**前面** ⇒ 结果不变。本文件的 `parse_entries` 枚举**全部**匹配行
        ⇒ 342 条 vs 基线 340 条。**同一变异对两个消费者结论相反**，所以
        `tests/test_code_version.py:48` 那条说法不能靠本条的结果去修
        （详见 CHANGELOG v0.45.195 补记）。

        记在这里而不是只改掉旧话，是因为「举得出变异」和「变异真的跑过」是两件事
        （MEMORY `alpha-hive-dte-off-by-one` 同款教训）。
        ⚠️ `tests/test_code_version.py:48` 挂着同一条未跑过的说法，本次未改它
        （不属本条守卫的改动面），已在 CHANGELOG v0.45.195 记明。
        """
        entries = parse_entries(self.VERSION_IN_BODY)
        assert [e.version for e in entries] == ["0.2.0"], (
            f"解析到 {[e.version for e in entries]}——"
            "正则不再锚定行首，正文里引用的版本号被数成了标题")

    # ── ③ 冲突标记 ──────────────────────────────────────────────────
    # 形状照 `4b5e4d9`（真被推上 main 的那份）复刻：冲突两侧**各带一份标题**。
    CONFLICTED = (
        "## [0.2.0] — d — 甲\n\n正文甲。\n\n---\n\n"
        "<<<<<<< HEAD\n"
        "## [0.1.0] — d — 我这侧\n\n正文乙。\n"
        "=======\n"
        "## [0.1.0] — d — 对方那侧\n\n正文丙。\n"
        ">>>>>>> origin/main\n"
    )
    # v0.45.200 那个 session 的**真实误报**：散文在讨论冲突标记，行内引用。
    #
    # ⚠️ 第 3 行是**刻意不加反引号**的。第一版夹具两行都写成 `` `<<<<<<<` ``，
    # 结果它**根本测不到行首锚定**——反引号让 `(?=[ \t]|$)` 那道 lookahead
    # 先把它挡掉了，于是「去掉 `^`」这个变异照样全绿。夹具通过的理由
    # 和它声称要测的东西不是同一件事（MEMORY「断言两边是同一种东西吗」）。
    # 加上这行**裸写 + 后跟空格**的引用，本条才真正验的是 `^`。
    PROSE_ABOUT_CONFLICT = (
        "## [0.2.0] — d — 甲\n\n"
        "那次 `git add && git commit && git push` 照跑——一份含 `<<<<<<<` /\n"
        "`=======` / `>>>>>>>` 的 CHANGELOG 被推上 main（`4b5e4d9`，存活约 3 分钟）。\n"
        "排查手法：先搜 <<<<<<< 定位，再看 ======= 两侧各是谁。\n"
    )

    def test_catches_real_conflict_shape(self):
        """三种标记各认一条（照 4b5e4d9 的真实形状复刻）。"""
        hits = conflict_markers(self.CONFLICTED)
        assert len(hits) == 3, hits

    def test_catches_diff3_base_marker(self):
        """diff3 风格还会多一条 `|||||||`，漏掉它等于只认半种冲突。"""
        assert conflict_markers("||||||| merged common ancestors\n")

    def test_prose_discussing_conflict_markers_is_not_flagged(self):
        """**在讨论 X ≠ 是 X。** 不锚行首就会把本仓 L7500/7501 那条条目报成冲突。

        这是 v0.45.200 那个 session 真撞到的误报（它用 `"<<<<<<<" not in s`），
        也是本仓文本探针第四次栽在同一形状上。

        **能让本条红的变异（已实测）**：同时去掉 `^` 且把 `.match` 换成 `.search`。
        单改任一个都不红——又是一组合取护栏（`re.match()` 本就只从串首匹配，
        `^` 对它恒冗余），同 `test_only_line_start_headings_count` 的四象限。
        """
        assert conflict_markers(self.PROSE_ABOUT_CONFLICT) == [], (
            "把「散文里引用冲突标记」报成了未解决冲突——"
            "守卫会在一份完全正常的 CHANGELOG 上恒红")

    def test_setext_underline_is_not_a_conflict_marker(self):
        """markdown 的 H1 下划线（任意长的 `=`）不是冲突标记。

        **能让本条红的变异（已实测两条，各自独立）**：
          · 去掉 `(?=[ \\t]|$)` ⇒ `={7}` 吃掉下划线的前 7 个 `=`
          · 去 `^` 且换 `.search` ⇒ `.search` 会滑到下划线**末尾**那 7 个 `=`，
            它们后面正是行尾，lookahead 通过 ⇒ 误判
        第二条说明 lookahead 与行首锚定**不是**各管各的：`.search` 一旦放开，
        lookahead 就不再护得住 setext。
        本仓当下零行首全 `=` 的行，但那是**内容依赖**，判据不能靠它撑着。
        """
        assert conflict_markers("一级标题\n" + "=" * 20 + "\n") == []
        assert conflict_markers("=======\n"), "恰好 7 个 `=` 仍须认作冲突标记"

    def test_conflicted_file_also_looks_like_a_duplicate(self):
        """把「③ 为什么必须排在 ① 前面」写成可执行断言，而不是注释。

        同一份含冲突的文件：③ 说「有冲突」，① 说「重号」。两句都对，但只有
        前者是**真因**。addopts 带 `-x` ⇒ 先定义的先红 ⇒ 谁先定义决定了人
        看到哪句诊断。这条一旦红，说明有人调换了顺序或删了 ③。
        """
        assert conflict_markers(self.CONFLICTED), "③ 该抓到"
        assert duplicate_versions(self.CONFLICTED), (
            "① 在含冲突的文件上**本该**也报重号——若它不报了，"
            "本条所说的「误报风险」就不存在了，③ 的排序理由需重新评估")
        names = [n for n in vars(TestChangelogEntryIntegrity) if n.startswith("test_")]
        assert names.index("test_no_unresolved_conflict_markers") < \
               names.index("test_no_duplicate_version_headings"), (
            "③ 被挪到 ① 后面了——`-x` 下人会先看到「重号」这句误导性诊断")

    def test_parser_finds_nothing_in_empty_input(self):
        """空输入解析出 0 条——这正是成对断言 `_MIN_ENTRIES` 要挡的情形。"""
        assert parse_entries("") == []
        assert duplicate_versions("") == []
        assert bodyless_entries("") == []


class TestProductionReaderIsUnaffected:
    """把 docstring 里那句「影响面只有全文件枚举」变成可执行的断言。

    散文写的保证不会被求值。这一条直接喂一份带重复的 CHANGELOG 给生产读取口径，
    确认 `changelog_version()` 仍返回**第一条**——即版本上报从未因这次重复出错。
    后人因此不必猜影响面有多大，跑一下就知道。
    """

    def test_changelog_version_returns_first_match_despite_duplicates(
            self, tmp_path, monkeypatch):
        import code_version as cv

        (tmp_path / "CHANGELOG.md").write_text(
            "# 标题\n\n---\n\n" + TestDetectorHasTeeth.THE_REAL_ONE,
            encoding="utf-8")
        monkeypatch.setattr(cv, "_repo_dir", lambda: tmp_path)

        assert cv.changelog_version() == "0.45.172", (
            "生产读取口径在有重复标题时给出了意外结果——"
            "那本条守卫的影响面判断（只波及全文件枚举）就要重估")
        # 同一份输入，全文件枚举**确实**看得见畸形。两句并排才说明白
        # 「为什么生产没事、而这条守卫仍然必要」。
        assert duplicate_versions(
            (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8"))
