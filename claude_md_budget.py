"""根目录 CLAUDE.md 的字节预算，与超预算时给读者的操作说明（v0.45.738 从 `tests/test_claude_md_partitions.py` 挪出）。

根目录 `CLAUDE.md` 每个会话都整份加载。分区（v0.45.727）之后它只留指针与不变式，预算贴着用户定的目标设、
不另加余量（v0.45.734：余量就是看不见的漂移）。两处用这个模块，同一个数、同一段话：

- `tests/test_claude_md_partitions.py::TestRootBudget`：查**工作区**那份（选测 / 整套 / CI）。
- `changelog_guard.py` 的 pre-push 预算闸：推往 main、且被推区间动了根目录 `CLAUDE.md` 时，查**被推提交里**那份。
  预算也从被推提交里本文件的源码读（`budget_in_source`，不 import 被推的版本）：工作区里没提交的改动不算数。

纯函数、只吃字节和字符串：不读文件、不碰 git，要查的那份内容由调用方拿来。
"""
from __future__ import annotations

import re

#: 根目录 CLAUDE.md 的字节上限 = 用户定的目标（约 25KB），不另加余量（v0.45.734 从 30,000 降下来）。
#: 余量就是看不见的漂移：0.45.728 让根目录涨了 1.7KB，落在旧预算约 5KB 的余量里，什么都没红。
#: **调大它 = 改用户定的数，先问用户**（v0.45.736）。红了之后按什么顺序做，写在 `over_budget_message`。
#: ⚠️ 保持「独占一行的整数字面量」：pre-push 闸用 `BUDGET_LINE` 从被推提交的源码里读这个数，读不出就拦。
ROOT_BUDGET_BYTES = 25_000

#: pre-push 闸从源码读预算的模式。`tests/test_claude_md_partitions.py` 钉着：对本文件恰好读出 `ROOT_BUDGET_BYTES`。
BUDGET_LINE = re.compile(r"^ROOT_BUDGET_BYTES = (\d[\d_]*)[ \t]*$", re.M)


def budget_in_source(source: str) -> int | None:
    """从本文件某个版本的源码里读预算。读不出、或不是恰好一行 ⇒ None：调用方当「未被检查」处理，不当通过。"""
    found = BUDGET_LINE.findall(source)
    return int(found[0].replace("_", "")) if len(found) == 1 else None


def section_sizes(raw: bytes) -> list[tuple[int, str]]:
    """按 `#` / `##` 标题切节，返回 [(字节数, 标题行)]，大的在前。

    `###` 及更深算进所在的节；代码块里以 `#` 开头的行（shell 注释）不算标题。每个字节恰好归一节、
    合计等于文件大小（`tests/test_claude_md_partitions.py` 钉着）——列给读者的数才能直接拿来算要搬多少。"""
    sections, title, size, fenced = [], "（第一个标题之前）", 0, False
    for line in raw.splitlines(keepends=True):
        text = line.decode("utf-8", errors="replace")
        if text.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,2} ", text):
            if size:
                sections.append((size, title))
            title, size = text.strip(), 0
        size += len(line)
    sections.append((size, title))
    return sorted(sections, key=lambda s: -s[0])


def over_budget_message(raw: bytes, budget: int, top: int = 5) -> str:
    """根目录超预算时给读者的操作说明。

    读者多半是刚往根目录加了东西的会话：跑选测用的是 Bash，Bash 不触发分区加载，它手里只有这段话；
    被 pre-push 拦下时也一样。所以这段话要自足，并且按读者最可能的反应来写：
    - 先拦住两条最省事的错路：压措辞 / 删内容（丢知识），和自己调大预算（改的是用户定的数）。
      v0.45.734 把「调大并在碎片里写理由」写成了第二条正路：改一个数加一句话，比搬家省事得多，
      撞线的会话很容易认定自己加的正是「必须留根目录」的那种；碎片里的理由在推上 main 之前没人看。
    - 正路按代价从低到高排：先搬自己这次加的（它最清楚该放哪），再从最大的几节里搬，搬不出来才问用户。
    - 下一步要用的数据直接算好：超了多少、哪几节最大。"""
    over = len(raw) - budget
    biggest = "\n".join(f"  {n:>7,}  {title}" for n, title in section_sizes(raw)[:top])
    return (
        f"根目录 CLAUDE.md {len(raw):,} 字节，超预算 {over:,}（上限 {budget:,} 是用户定的）。"
        "每个会话都整份加载它，超出的字节每个会话都要付。\n"
        f"最大的 {top} 节（字节）：\n{biggest}\n"
        "别做：为过闸压措辞或删内容（会丢知识）；自己调大 `claude_md_budget.py` 的 ROOT_BUDGET_BYTES（等于改用户定的数）。\n"
        "按顺序做：\n"
        "  1. 这次你往根目录加了内容：先把细节逐字搬进分区文件，根目录只留一行「是什么 → 去读哪个文件」。"
        "确实得留在根目录的（开工第一件事就要用的、新模块匹配不到 paths: 的不变式），也只留一两句核心规则，细节照样搬。\n"
        "     搬到读相关文件时一定会加载的那份：子目录的 CLAUDE.md，或 .claude/rules/*.md（paths: 写这条规则管的文件）；"
        "布局见根目录开篇「文档分工原则」里的「分区」。\n"
        "  2. 还超，或超额不是你加的（几个会话各加一点会一起超，`git log --oneline --stat -5 -- CLAUDE.md` 看是谁）："
        f"从上面几节里逐字搬出至少 {over:,} 字节。方法和三道核对见 auto-memory "
        "`alpha-hive-claude-md-partitions.md` 的「再瘦身 / 搬家的方法」。\n"
        "  3. 搬不出来：先问用户要不要调大预算。用户同意后，在同一提交里改 `claude_md_budget.py` 的 ROOT_BUDGET_BYTES，"
        "并在版本碎片里写明用户的决定和理由。问不到人（定时任务、无人值守）就让它红着，在汇报里写明超了多少。\n"
        "搬完重跑 `/usr/local/bin/python3 -m pytest tests/test_claude_md_partitions.py`。它会抓三种搬坏的情况："
        "新分区文件没被根目录点名、paths: 一个文件都匹配不到、代码注释引用的节搬到了读那段代码时不加载的地方。"
    )
