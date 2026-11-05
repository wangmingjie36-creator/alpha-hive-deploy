"""根目录 CLAUDE.md 分区的守卫（v0.45.727）。

根目录 `CLAUDE.md` 每个会话都整份进上下文。v0.45.727 前它涨到 88KB（约 2.8 万 token，
每个会话都付）；瘦身后细节放进**按需加载**的文件，根目录每块只留「是什么 + 何时去读哪个」：

- 子目录 `CLAUDE.md`（`alphabot/`、`tests/`、`experiments/`）：读 / 改该目录下的文件时加载；
- `.claude/rules/*.md`：frontmatter `paths:`（gitignore 式、相对仓库根）匹配的文件被读 / 改时加载；
- 项目 skill `.claude/skills/<名>/SKILL.md`：harness 按 frontmatter `description` 里的指令词加载。

这套布局有四种**不报错**的失效方式，各配一条会红的断言（「谁会红？」）：

1. `paths:` 写错、或它指向的文件被改名 / 删掉 ⇒ 规则文件再也不加载，里面的知识等于丢了。
   ⇒ 每个模式至少匹配一个被 git 跟踪的文件。没有 `paths:` 的规则文件每个会话都加载、
   等于写进根目录却逃过预算 ⇒ 也红。
2. 根目录没指过去 ⇒ 还没用 Read / Edit / Write 碰到相关文件的会话（规划阶段、只用 Bash 看过、新文件匹配不到 `paths:`）都找不到它。
   ⇒ 每个分区文件都被根目录点名；任何一份里点名的分区路径都真实存在。
3. 代码注释常写「见 CLAUDE.md 的某节」、节名放在紧跟其后的书名号里（瘦身时 90 处）。被引的那节若只剩在别的目录的分区里，
   读这段代码的会话上下文里就没有它 ⇒ 引用必须在「读引用者时会加载的 CLAUDE.md」里找得到
   （根目录 + 引用者所在各级目录的 `CLAUDE.md`）。存量意译登记在 `LEGACY_UNRESOLVED_CITATIONS`，只许减。
4. 根目录慢慢涨回去 ⇒ 每个会话照付 ⇒ 字节预算 `ROOT_BUDGET_BYTES`，贴着目标设：
   预算里的余量就是这种「慢慢涨」的藏身处，余量越大，这条断言对它越瞎。

`paths:` 只认本守卫解析得了的写法（每项一行、双引号；不支持花括号 / `!` / `[]`）：写了别的就红，
先在 `_pattern_regex` 补上语义——否则守卫对它恒真。双引号也是必需的：YAML 里以 `*` 开头的
裸值是锚点别名，整段 frontmatter 会解析失败、规则文件变成「不知道什么时候加载」。
"""
import functools
import re
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

import pytest

_ROOT = Path(__file__).resolve().parent.parent  # 代码锚点：查的就是本 checkout 的文档

#: 根目录 CLAUDE.md 的字节上限 = 用户定的目标（约 25KB），不另加余量（v0.45.734 从 30,000 降下来）。
#: 余量就是看不见的漂移：0.45.728 让根目录涨了 1.7KB，落在旧预算约 5KB 的余量里，什么都没红。
#: 红了 ⇒ 先把细节**逐字**搬进分区文件、根目录只留指针；**别先调大这个数**。确实必须留在根目录的
#: （开工第一件事就要用的、新模块匹配不到 `paths:` 的不变式）才调大，并在同一提交的版本碎片里写理由。
ROOT_BUDGET_BYTES = 25_000

#: 项目 skill `alpha-hive-brief` 的指令词：根目录点名给人看，skill 的 description 给 harness 匹配，两边都得有。
BRIEF_TRIGGERS = ("开始晨扫", "生成今日简报", "跑简报", "分析一下", "深度研究")

#: v0.45.727 普查时就找不到出处的引用（全部早于本次瘦身）。**只许减**：改到引用处时顺手改成逐字标题，
#: 再从这里删掉；新引用一律逐字引用标题或原句。
LEGACY_UNRESOLVED_CITATIONS = {
    "有 key 就自动开 LLM": "意译：根目录原句是「禁止在代码里用 `api_key 存在就自动开 LLM` 的逻辑」",
    "有 key 就自动开": "意译：同上",
    "skip 守卫要问 X 在哪些环境存在": "意译：标题是「`skip` 守卫要问「X 在哪些环境里存在？」」（少一个「里」）",
    "你手动跑时用哪个": "意译：指「用户偏好」的 Python 解释器硬规则",
    "这个默认值会不会让下游误以为掌握了信息": "仓库里任何 CLAUDE.md 都没有这句（实验产物 JSON 里的判据转述）",
    "不编数据": "用户级 `~/.claude/CLAUDE.md` 的标题，不在仓库里（根目录写的是「不编造数据」）",
    "先用装好的工具，再手搓": "用户级 `~/.claude/CLAUDE.md` 的标题，不在仓库里（引用处写成了「项目 CLAUDE.md」）",
}

_FM_ITEM = re.compile(r'  - "([^"]+)"')
_FM_KEY = re.compile(r"([a-z][a-z_-]*):(.*)")
#: 分区文件路径的写法（反引号包住的整段）：规则文件 / 子目录 CLAUDE.md / skill。
_POINTER = re.compile(
    r"`((?:\.claude/rules/[\w-]+\.md)|(?:[\w-]+(?:/[\w-]+)*/CLAUDE\.md)|(?:\.claude/skills/[\w-]+/SKILL\.md))`")
#: 文件名 CLAUDE.md 后面 24 个字符内跟着书名号节名的引用；前缀是 `~/.claude/` 的是用户级文件，不在仓库里，查不了。
#: ⚠️ 本文件的说明文字别写成被引的样子（会扫到自己）。
_CITATION = re.compile(r"(~/\.claude/)?CLAUDE\.md[^「\n]{0,24}「([^」\n]{1,60})」")
#: 比对引用时忽略空白、强调 / 代码标记、引号与常见标点（引用常省掉标题里的「」与问号）。
_NORM = re.compile(r"[\s`*「」『』“”\"'？?。：:，,、！!]")
_TEXT_SUFFIXES = (".py", ".sh", ".js", ".md", ".toml", ".yml", ".yaml", ".json", ".txt")


def _git_files(*extra: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(_ROOT), "ls-files", "-z", *extra],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"git ls-files 失败（退出码 {r.returncode}）：{r.stderr.strip()}"
    return [f for f in r.stdout.split("\0") if f]


def _rel(p: Path) -> str:
    return p.relative_to(_ROOT).as_posix()


def _frontmatter(path: Path) -> dict:
    """解析分区文件的 frontmatter：`键: 值` 单行，或空值键下每行 `  - "模式"`。别的写法直接红。"""
    rel = _rel(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "---", f"{rel}：第一行必须是 frontmatter 起始 `---`"
    assert "---" in lines[1:], f"{rel}：frontmatter 没有结束的 `---`"
    out: dict = {}
    list_key = None
    for ln in lines[1:lines.index("---", 1)]:
        item = _FM_ITEM.fullmatch(ln)
        if item:
            assert list_key, f"{rel}：列表项前面没有空值的键：{ln!r}"
            out[list_key].append(item.group(1))
            continue
        kv = _FM_KEY.fullmatch(ln)
        assert kv, f"{rel}：本守卫解析不了这行 frontmatter（只认 `键: 值` 与 `  - \"模式\"`）：{ln!r}"
        key, value = kv.group(1), kv.group(2).strip()
        if value:
            # 裸值里的 ASCII「: 」「 #」与开头的 YAML 指示符会让真 YAML 解析成别的东西或报错
            assert ": " not in value and " #" not in value and value[0] not in "*&!|>%@`'\"[{", \
                f"{rel}：`{key}` 的裸值在 YAML 里会被误解析（含「: 」「 #」或以指示符开头），请改写"
            out[key], list_key = value, None
        else:
            out[key], list_key = [], key
    return out


def _pattern_regex(pat: str) -> re.Pattern:
    """`paths:` 的一个模式 → 正则（gitignore 语义的子集，与 Claude Code 的匹配一致）：
    含 `/`（末尾的不算）⇒ 相对仓库根锚定，否则任意深度；`**` 跨目录，`*` / `?` 不跨 `/`；
    匹配到目录即匹配其下一切（所以末尾 `/**` 与去掉它等价）。"""
    unsupported = sorted(set(pat) & set("{}[]!\\"))
    assert not unsupported, f"`paths:` 模式 {pat!r} 用了本守卫没实现的语法 {unsupported}：先在 _pattern_regex 补语义"
    body = pat[:-3] if pat.endswith("/**") else pat
    anchored = "/" in body.rstrip("/")
    body = body.strip("/")
    out, i = [], 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif body.startswith("**", i):
            out.append(".*"); i += 2
        elif body[i] == "*":
            out.append("[^/]*"); i += 1
        elif body[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(body[i])); i += 1
    return re.compile(("" if anchored else "(?:.*/)?") + "".join(out) + "(?:/.*)?")


def _rule_files() -> list[Path]:
    return sorted((_ROOT / ".claude/rules").glob("*.md"))


def _skill_files() -> list[Path]:
    return sorted((_ROOT / ".claude/skills").glob("*/SKILL.md"))


def _subdir_claude_mds() -> list[str]:
    # --others：还没 git add 的新分区也要被点名；.claude/worktrees/ 被 .gitignore 挡掉，不会数进嵌套 worktree
    return sorted({f for f in _git_files("--cached", "--others", "--exclude-standard")
                   if PurePosixPath(f).name == "CLAUDE.md" and f != "CLAUDE.md"})


def _partitions() -> list[str]:
    return [_rel(p) for p in _rule_files()] + [_rel(p) for p in _skill_files()] + _subdir_claude_mds()


def _root_text() -> str:
    return (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


class TestPatternMatcher:
    """`_pattern_regex` 自己得对：它恒真 / 恒假，下面「每个模式都匹配得到文件」就证明不了任何事。"""

    @pytest.mark.parametrize("pat,hit,miss", [
        ("config.py", ["config.py", "a/b/config.py"], ["config.pyc", "xconfig.py"]),
        ("tests/test_*.py", ["tests/test_a.py"], ["tests/sub/test_a.py", "x/tests/test_a.py"]),
        ("swarm_agents/**", ["swarm_agents/buzz_bee.py", "swarm_agents/a/b.py"], ["swarm_agentsx/b.py"]),
        ("experiments/dim_ic_*", ["experiments/dim_ic_protocol.py"], ["experiments/x/dim_ic_a.py"]),
        ("**/foo.py", ["foo.py", "a/b/foo.py"], ["foo.pyx"]),
        ("alphabot/static/pages/hedge*.js", ["alphabot/static/pages/hedge_tail.js"],
         ["alphabot/static/pages/x/hedge.js", "alphabot/hedge.js"]),
    ])
    def test_gitignore_subset(self, pat, hit, miss):
        rx = _pattern_regex(pat)
        assert all(rx.fullmatch(p) for p in hit), (pat, [p for p in hit if not rx.fullmatch(p)])
        assert not any(rx.fullmatch(p) for p in miss), (pat, [p for p in miss if rx.fullmatch(p)])

    def test_unsupported_syntax_is_red(self):
        with pytest.raises(AssertionError):
            _pattern_regex("alphabot/{a,b}.py")


class TestRulePathsMatchRealFiles:
    def test_rules_exist(self):
        assert _rule_files(), ".claude/rules/ 里没有任何规则文件——分区被整个挪走了？"

    @pytest.mark.parametrize("rule", _rule_files(), ids=lambda p: p.name)
    def test_every_pattern_hits_a_tracked_file(self, rule):
        fm = _frontmatter(rule)
        pats = fm.get("paths")
        assert isinstance(pats, list) and pats, \
            f"{_rel(rule)} 没有 `paths:` ⇒ 每个会话都加载，等于写进根目录却逃过预算：要么加 paths，要么并进根目录"
        tracked = _git_files()
        dead = [p for p in pats if not any(_pattern_regex(p).fullmatch(f) for f in tracked)]
        assert not dead, (f"{_rel(rule)} 的这些模式一个被跟踪的文件都匹配不到（改名 / 删除 / 写错？新文件先 git add）"
                          f"⇒ 规则永远不加载：{dead}")


class TestEveryPartitionIsPointedTo:
    def test_layout_present(self):
        assert _rule_files() and _skill_files() and _subdir_claude_mds(), "分区布局缺了一类文件"

    def test_root_points_to_every_partition(self):
        pointed = set(_POINTER.findall(_root_text()))
        missing = [p for p in _partitions() if p not in pointed]
        assert not missing, (f"根目录 CLAUDE.md 没点名这些分区文件（要写成反引号包住的完整路径）"
                             f"⇒ 还没碰到相关文件的会话找不到它们：{missing}")

    def test_no_dangling_pointer(self):
        docs = [_ROOT / "CLAUDE.md"] + [_ROOT / p for p in _partitions()]
        dangling = sorted({(_rel(d), p) for d in docs for p in _POINTER.findall(d.read_text(encoding="utf-8"))
                           if not (_ROOT / p).is_file()})
        assert not dangling, f"指向不存在的分区文件（文件、指针）：{dangling}"


@functools.lru_cache(maxsize=None)
def _loaded_claude_md(directory: str) -> str:
    """读 `directory` 下的文件时会在上下文里的 CLAUDE.md（根目录 + 各级子目录），归一化后拼起来。"""
    parts = PurePosixPath(directory).parts
    docs = [_ROOT / "CLAUDE.md"] + [_ROOT.joinpath(*parts[:i], "CLAUDE.md") for i in range(1, len(parts) + 1)]
    return _NORM.sub("", "\n".join(d.read_text(encoding="utf-8") for d in docs if d.is_file()))


@functools.lru_cache(maxsize=None)
def _unresolved_citations() -> dict:
    skip = set(_partitions()) | {"CHANGELOG.md"}  # CHANGELOG 是历史记录；分区文件自己写着「从根目录搬来「…」」
    out: dict = defaultdict(list)
    seen = 0
    for f in _git_files():
        if f in skip or PurePosixPath(f).name == "CLAUDE.md" or not f.endswith(_TEXT_SUFFIXES):
            continue
        try:
            text = (_ROOT / f).read_text(encoding="utf-8")
        except FileNotFoundError:  # 工作区里删了、还没提交
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for m in _CITATION.finditer(line):
                if m.group(1):
                    continue
                seen += 1
                if _NORM.sub("", m.group(2)) not in _loaded_claude_md(PurePosixPath(f).parent.as_posix()):
                    out[m.group(2)].append(f"{f}:{n}")
    assert seen >= 50, f"只扫到 {seen} 处 CLAUDE.md 引用（瘦身时 90 处）——扫描坏了，下面的断言会恒真"
    return dict(out)


class TestCitedSectionsResolve:
    def test_citations_resolve_in_loaded_claude_md(self):
        new = {p: locs for p, locs in _unresolved_citations().items() if p not in LEGACY_UNRESOLVED_CITATIONS}
        assert not new, ("这些按文件名加书名号节名写的引用，在读引用者时会加载的那几份 CLAUDE.md 里找不到"
                         f"（根目录 + 引用者所在各级目录）：{new}。被引的节若搬进了 .claude/rules/ 或别的目录，"
                         "根目录要留同名标题；新引用请逐字引用")

    def test_legacy_list_only_shrinks(self):
        stale = sorted(set(LEGACY_UNRESOLVED_CITATIONS) - set(_unresolved_citations()))
        assert not stale, f"这些已经找得到出处或不再被引用，从 LEGACY_UNRESOLVED_CITATIONS 删掉：{stale}"


class TestRootBudget:
    def test_root_within_budget(self):
        size = len((_ROOT / "CLAUDE.md").read_bytes())
        assert size <= ROOT_BUDGET_BYTES, (
            f"根目录 CLAUDE.md {size:,} 字节 > 预算 {ROOT_BUDGET_BYTES:,}（超 {size - ROOT_BUDGET_BYTES:,}）：每个会话都整份加载它。"
            "① 首选：把细节逐字搬进按需加载的分区（子目录 CLAUDE.md / .claude/rules/），根目录只留指针，别为过闸压措辞丢知识；"
            "② 确实必须留在根目录（开工第一件事就要用的、新模块匹配不到 paths: 的不变式）：在同一提交里调大 "
            "ROOT_BUDGET_BYTES，并在版本碎片里写理由")


class TestSkills:
    @pytest.mark.parametrize("skill", _skill_files(), ids=lambda p: p.parent.name)
    def test_frontmatter(self, skill):
        fm = _frontmatter(skill)
        assert fm.get("name") == skill.parent.name, f"{_rel(skill)}：`name` 须等于目录名"
        assert isinstance(fm.get("description"), str) and len(fm["description"]) >= 20, \
            f"{_rel(skill)}：`description` 是 harness 决定何时加载的唯一依据，不能空"

    def test_brief_triggers_on_both_sides(self):
        desc = _frontmatter(_ROOT / ".claude/skills/alpha-hive-brief/SKILL.md")["description"]
        root = _root_text()
        assert [t for t in BRIEF_TRIGGERS if f"「{t}」" not in desc] == [], "skill description 缺指令词 ⇒ harness 不加载"
        assert [t for t in BRIEF_TRIGGERS if f"「{t}」" not in root] == [], "根目录缺指令词 ⇒ 读根目录的人不知道何时加载"
