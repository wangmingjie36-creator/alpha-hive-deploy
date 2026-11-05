#!/usr/bin/env python3
"""CHANGELOG 版本碎片：每个版本一个文件 `changelog.d/<版本>.md`（v0.45.728）

    /usr/local/bin/python3 changelog_fragments.py claim "一句话说明" --push --trailer "Co-Authored-By: …"
    /usr/local/bin/python3 changelog_fragments.py adopt <占号提交>     # 占号由别人推上 main 后，带进当前分支
    /usr/local/bin/python3 changelog_fragments.py status               # 最大号 / 下一个号 / 进行中的占位 / 跳号
    /usr/local/bin/python3 changelog_fragments.py check [--rev REV]    # 同全量测试里的碎片检查
    /usr/local/bin/python3 changelog_fragments.py render [--limit N] [--rev REV]  # 合并视图，打到标准输出（不提交）

为什么
------
v0.45.728 之前所有会话都往 `CHANGELOG.md` **顶部**插条目：10-01 起 main 上 886 个提交里 800 个动了它，
号不同也照样冲突，解冲突成了例行公事、也就成了事故源（memory 记了 9 次）。碎片化后不同的号是不同的文件，
rebase 不再冲突；只有真撞号才冲突，冲突重新成为有意义的信号。`CHANGELOG.md` 冻结为历史档案
（`ARCHIVE_FROZEN_*`），合并视图由 `render` 按需生成，**不提交**——提交了就是新的热点。

格式（唯一真相在本模块；人读的速查在 `changelog.d/README.md`）
--------------------------------------------------------------
文件名 = 版本号（`code_version._FRAGMENT_NAME_RE`：三段整数、无前导零、小写 `.md`）；目录里另外只许一个
`README.md`。首个非空行是 `## [版本] — YYYY-MM-DD — 标题`，版本 == 文件名，全文件只有这一个 `## [`。
**认领注释**恰好一行：`<!-- 认领：<分支> <时刻> <8 位十六进制> -->`，由 `claim` 写，推上 main 后不许改。
占位的标题是 `占位（进行中：…）`，可以没有正文；完工只改标题、补正文，认领行原样保留。

认领注释为什么必须有（2026-11-05 临时仓库实测，git 2.50.1）
------------------------------------------------------------
* 两个会话写出**内容相同**的同号碎片：后来者 `git pull --rebase` 退出码 0，它的占号提交被当成「已在上游」
  **静默丢弃**，双方都以为占到了。两个空文件就是同一个 blob ⇒「占号 = 建一个空文件」不可行。
* 内容不同：`AA changelog.d/<号>.md`，rebase 停下。
认领行带随机串 ⇒ 内容永远不同 ⇒ 撞号一定出声。它推上 main 后不许变（`protect`）⇒ 撞号后解冲突选错一边、
盖掉了别人的碎片，推送时会红——这是整套方案里最坏的结局（别人的记录无声消失）。

占号（`claim`）
--------------
fetch → 取号（origin/main 的碎片号与档案顶号取最大 +1）→ 在 **origin/main 上**另建一个只含新碎片的提交
（临时索引，不碰当前分支与工作区，做到一半也能占号）→ `--push` 推送并以 `git ls-remote` 为准判成功
（v0.45.354 凭输出文字判成功、一口气占了四个号）→ 被拒且 main 前进了就重新取号重推 → 成功后带进当前分支
（`adopt`：分支落后就快进，否则 cherry-pick；以后 rebase 到 main 时 git 自动跳过这个重复提交，已实测）。
推不了 main 的会话不带 `--push`：工具打印推送命令，推完再 `adopt`。**推上去之前号没占住。**
按这个流程，撞号只表现为「推送被拒、换下一个号」，不会出现冲突；冲突只发生在绕过工具手写碎片时。
工具用 Python、每个 git 调用都核返回码：同一流程用 zsh 写时 `"$C:refs/heads/main"` 的 `:r` 会被当成修饰符、
推送静默失败、后面照跑（实测）。
推完之后（v0.45.733 二次检查）：转述推送输出里 pre-push 守卫的那几行，看不到放行那句（`GUARD_PASS_MARK`）就提示
没装钩子；核实远端失败时按推送退出码分说「号多半已占住」/「号大概没占住」，号已占住的情形一律给 `adopt` 命令、
写明别重跑 claim（重跑会再占一个号）。`adopt` 按认领行判「已带进来」（完工后内容早变了），cherry-pick 停在半路就撤回。

判据（`lint` / `protect` / `archive_freeze_problems`，守卫与全量测试共用）
-----------------------------------------------------------------------
红：文件名不合规（前导零、`.MD`、iCloud 副本 `0.45.728 2.md`、子目录、多余文件）；标题缺失 / 格式或日期不对 /
版本 ≠ 文件名 / 不止一个；冲突标记；空文件；非占位没有正文；认领行缺失、不止一行或格式不对；
碎片号 ≤ 档案最大号；推送删掉或改了认领行的 main 上已有碎片；档案出现新号或条目数变了。
**不红、只在 `status` 里报**：跳号（按 minor 系列分别算；可能是别人占了还没推，也可能遗弃）、占位未完成（进行中是合法状态，
按时长报红会变成定时炸弹）。

守卫接线（`changelog_guard.py`）
------------------------------
碎片检查读**要提交 / 要推送的 git 对象**（暂存区 / 被推提交里的 blob），不读工作区——「在 origin/main 上另建的
占号提交」推送时，当前分支落后 main 的话工作区必然与之不同，读工作区的守卫只能拒。读 blob 则「测的就是推的」
由构造保证（同契约闸）。档案 `CHANGELOG.md` 那条旧路径（跑 `tests/test_changelog_entry_integrity.py`）不动。

过渡期
------
冻结前已在 `CHANGELOG.md` 占的号**就地完成**（照旧改标题、补正文：条目数与最大号不变，冻结断言照过）。
冻结后往档案里加新号 ⇒ 提交 / 推送 / 全量测试红，报错直接给出 `claim` 命令。第一个碎片号 = 冻结最大号 + 1。
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional

from code_version import FRAGMENT_DIRNAME, _CHANGELOG_RE, _FRAGMENT_NAME_RE, version_key

ARCHIVE = "CHANGELOG.md"
README = "README.md"
REMOTE = "origin"
MAIN_REF = "refs/heads/main"
CLAIM_MAX_ATTEMPTS = 5
#: 守卫查过碎片、放行时打印的字样（`changelog_guard._report_fragments`）。`claim` 推送后在钩子输出里找它：
#: 找不到 ⇒ 这次推送没经过碎片检查（没装钩子 / 钩子是旧代码），要说出来。
GUARD_PASS_MARK = f"{FRAGMENT_DIRNAME}/ 碎片检查通过"

#: 档案冻结值：冻结时 `CHANGELOG.md` 的最大号与 `## [` 标题数（合入碎片协议前最后一次 rebase 时现取）。
#: 冻结后只许就地完成冻结前占的号，不许增删条目——两个值都不会再变。
ARCHIVE_FROZEN_MAX = "0.45.728"
ARCHIVE_FROZEN_COUNT = 871

#: 碎片标题。em 破折号两侧各一个空格，与档案的主流写法一致（档案里 34 条不规范的旧标题不追究）。
HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]*)\] — (?P<date>\d{4}-\d{2}-\d{2}) — (?P<title>\S.*)$")
#: 进行中的占位标题。档案里的「占位关闭：…」「…占位价…」之类不算占位。
PLACEHOLDER_RE = re.compile(r"^占位（进行中：.+）$")
CLAIM_RE = re.compile(r"^<!-- 认领：(?P<who>\S+) (?P<at>\S+) (?P<nonce>[0-9a-f]{8}) -->$")
_CLAIM_PREFIX = "<!-- 认领"
#: git 冲突标记：行首**恰好 7 个**字符，后接空格或行尾。`<<<<<<< HEAD` / `=======` / `>>>>>>> origin/main` /
#: `||||||| merged common ancestors`（diff3）。锚行首：不锚就会命中**在讨论**冲突标记的散文；恰好 7 个：
#: 否则 `={7}` 吃掉 markdown setext 标题的下划线。档案与碎片共用这一份（v0.45.728 前在
#: `tests/test_changelog_entry_integrity.py` 里，那里的牙齿用例与变异记录照旧有效）。
CONFLICT_RE = re.compile(r"^(?:<{7}|={7}|>{7}|\|{7})(?=[ \t]|$)")
#: markdown 水平线的字符集。本仓只用 `---`，`***` / `___` 一并认掉是零成本的——漏认一种就会把分隔线当成正文。
_RULE_CHARS = set("-*_")
#: 档案里进行中的占位（`status` 列出用）。
_ARCHIVE_PLACEHOLDER_RE = re.compile(r"^## \[(?P<version>[0-9.]+)\] — (?P<date>\S+) — 占位（进行中：(?P<desc>.*)）\s*$")


class GitError(RuntimeError):
    """git 调用失败。守卫拿到它 ⇒「未被检查」，不是「检查通过」。"""


class NotRegular(NamedTuple):
    """git 树 / 工作区里不是普通文件的条目（符号链接、子模块……）。"""
    mode: str


def is_rule(line: str) -> bool:
    """这行是不是水平线（`---`）。⚠️ 水平线**不算正文**：算了的话 `标题 / --- / 标题` 这种畸形会被判成「有正文」放过，
    而档案里每条条目之间都隔着一条 `---`——把分隔线当正文，「无正文」判据对绝大多数形态失去判别力。"""
    s = line.strip()
    return len(s) >= 3 and set(s) <= _RULE_CHARS


def fragment_path(version: str) -> str:
    return f"{FRAGMENT_DIRNAME}/{version}.md"


# ───────────────────────────────────────── 解析与判据（纯函数）──────────

class Parsed(NamedTuple):
    first_line: Optional[str]          # 首个非空行（去掉行尾空白）
    heading: Optional[re.Match]        # 首个非空行匹配 HEADING_RE 的结果
    n_headings: int                    # 行首 `## [数字` 标题的个数（口径 = 档案的 `_CHANGELOG_RE`）
    claims: list                       # 合规的认领行
    bad_claims: list                   # 以 `<!-- 认领` 开头却不合规的行
    has_body: bool                     # 标题之后有没有真正文（不算认领行、水平线、空行）
    conflicts: list                    # 冲突标记所在行号（1 起）


def parse(text: str) -> Parsed:
    lines = text.splitlines()
    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    first = lines[first_idx].rstrip() if first_idx is not None else None
    heading = HEADING_RE.match(first) if first is not None else None
    claims, bad = [], []
    for ln in lines:
        s = ln.strip()
        if CLAIM_RE.match(s):
            claims.append(s)
        elif s.startswith(_CLAIM_PREFIX):
            bad.append(s)
    after = lines[first_idx + 1:] if first_idx is not None else []
    has_body = any(ln.strip() and not ln.strip().startswith(_CLAIM_PREFIX) and not is_rule(ln) for ln in after)
    return Parsed(
        first_line=first,
        heading=heading,
        n_headings=sum(1 for ln in lines if _CHANGELOG_RE.match(ln)),
        claims=claims,
        bad_claims=bad,
        has_body=has_body,
        conflicts=[i for i, ln in enumerate(lines, 1) if CONFLICT_RE.match(ln)],
    )


def claim_of(text: str) -> Optional[str]:
    """碎片里**唯一**那行合规认领注释；没有或不止一行 ⇒ None。"""
    c = parse(text).claims
    return c[0] if len(c) == 1 else None


def is_placeholder(text: str) -> bool:
    h = parse(text).heading
    return bool(h and PLACEHOLDER_RE.match(h["title"]))


def _decode(data):
    """bytes → str；不是 UTF-8 ⇒ None。str 原样返回。"""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _lint_one(version: str, text: str, archive_top: Optional[str]) -> list:
    p = parse(text)
    out = []
    if p.conflicts:
        out.append(f"未解决的合并冲突标记（行 {', '.join(map(str, p.conflicts[:5]))}）——同号碎片撞了：保留 main 上那份，"
                   "自己的内容重新 claim 一个号")
    if p.first_line is None:
        out.append("空文件——碎片至少要有标题行和认领注释（占号一律用 `changelog_fragments.py claim`）")
        return out
    if p.heading is None:
        out.append(f"首个非空行不是标题 `## [{version}] — YYYY-MM-DD — 标题`：{p.first_line[:70]!r}")
    else:
        if p.heading["version"] != version:
            out.append(f"标题里的号 [{p.heading['version']}] ≠ 文件名的号 {version}")
        try:
            datetime.date.fromisoformat(p.heading["date"])
        except ValueError:
            out.append(f"标题日期不合法：{p.heading['date']}")
    if p.n_headings > 1:
        out.append(f"有 {p.n_headings} 个行首 `## [` 标题——一个碎片只放一个版本")
    if p.bad_claims:
        out.append(f"认领注释格式不对：{p.bad_claims[0][:80]!r}（要 `<!-- 认领：<分支> <时刻> <8 位十六进制> -->`）")
    if len(p.claims) != 1:
        out.append(f"认领注释要恰好 1 行，实有 {len(p.claims)} 行——占号一律用 `changelog_fragments.py claim`，"
                   "它会写这一行；完工时原样保留")
    if p.heading is not None and not PLACEHOLDER_RE.match(p.heading["title"]) and not p.has_body:
        out.append("标题不是「占位（进行中：…）」却没有正文——空条目")
    if archive_top and version_key(version) <= version_key(archive_top):
        out.append(f"碎片号 {version} ≤ 档案 {ARCHIVE} 的最大号 {archive_top}——碎片号必须排在冻结档案之后")
    return out


def lint(files: dict, archive_top: Optional[str]) -> list:
    """`files`：{相对仓库根的路径: 内容(bytes/str/NotRegular)}，只含 `changelog.d/` 下的条目；
    `archive_top`：同一棵树里档案的最大号（None = 没有档案，不比）。返回问题列表，空 = 通过。纯函数。"""
    problems = []
    prefix = FRAGMENT_DIRNAME + "/"
    for path in sorted(files):
        if not path.startswith(prefix):
            problems.append(f"{path}：不在 {prefix} 下（调用方传错了）")
            continue
        name = path[len(prefix):]
        data = files[path]
        if "/" in name:
            problems.append(f"{path}：{prefix} 下不许有子目录")
            continue
        if isinstance(data, NotRegular):
            problems.append(f"{path}：不是普通文件（mode {data.mode}）")
            continue
        if name == README:
            continue
        m = _FRAGMENT_NAME_RE.match(name)
        if not m:
            problems.append(f"{path}：文件名不合规——只认 `X.Y.Z.md`（三段整数、无前导零、小写 .md）与 {README}；"
                            "iCloud 的「 2」副本、前导零都会让一个号有两个文件")
            continue
        text = _decode(data)
        if text is None:
            problems.append(f"{path}：不是 UTF-8")
            continue
        problems += [f"{path}：{p}" for p in _lint_one(m.group(1), text, archive_top)]
    return problems


def protect(base_files: dict, new_files: dict) -> list:
    """推送前：远端 main（`base_files`）上已有的碎片，要推的提交（`new_files`）里不许删、认领行不许变。"""
    out = []
    prefix = FRAGMENT_DIRNAME + "/"
    for path in sorted(base_files):
        name = path[len(prefix):] if path.startswith(prefix) else ""
        if not _FRAGMENT_NAME_RE.match(name):
            continue
        if path not in new_files:
            out.append(f"{path}：main 上已有的碎片被删掉 / 改名了——碎片推上 main 就不删（遗弃的号就地把标题改成"
                       "「占位关闭：…」并写明原因）")
            continue
        old_text, new_text = _decode(base_files[path]), _decode(new_files[path])
        old = claim_of(old_text) if old_text is not None else None
        new = claim_of(new_text) if new_text is not None else None
        if old and new != old:
            out.append(f"{path}：认领注释变了（main 上是 {old!r}，要推的是 {new!r}）——多半是撞号后解冲突选错了边、"
                       "盖掉了别人的碎片：恢复 main 上那份，自己的内容重新 claim 一个号")
    return out


def archive_versions(text: str) -> list:
    """档案里行首 `## [x.y.z]` 的版本号，按出现顺序（口径 = `code_version._CHANGELOG_RE`）。"""
    return [m.group(1) for m in map(_CHANGELOG_RE.match, text.splitlines()) if m]


def archive_top(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    for line in text.splitlines():
        m = _CHANGELOG_RE.match(line)
        if m:
            return m.group(1)
    return None


def archive_freeze_problems(text: str, frozen_max: str = ARCHIVE_FROZEN_MAX,
                            frozen_count: int = ARCHIVE_FROZEN_COUNT) -> list:
    """档案冻结：不许出现比冻结最大号更大的号、不许增删条目、顶号不变。只报最先发现的那一类。"""
    vs = archive_versions(text)
    newer = [v for v in vs if version_key(v) > version_key(frozen_max)]
    if newer:
        return [f"{ARCHIVE} 已冻结（最大号 {frozen_max}），却出现了新号 {', '.join(newer[:5])}——新条目写进 "
                f"{FRAGMENT_DIRNAME}/：`/usr/local/bin/python3 changelog_fragments.py claim \"一句话说明\" --push`。"
                "冻结前已在本文件占的号就地完成，别加新标题"]
    if len(vs) != frozen_count:
        return [f"{ARCHIVE} 的 `## [` 标题有 {len(vs)} 条 ≠ 冻结值 {frozen_count}——档案只许就地完成冻结前占的号，"
                f"不许增删条目（新号走 {FRAGMENT_DIRNAME}/）"]
    if vs and vs[0] != frozen_max:
        return [f"{ARCHIVE} 顶部的号 {vs[0]} ≠ 冻结最大号 {frozen_max}——档案条目被挪动或替换了"]
    return []


def render_placeholder(version: str, desc: str, *, who: str, at: str, nonce: str, date: str) -> str:
    return f"## [{version}] — {date} — 占位（进行中：{desc}）\n<!-- 认领：{who} {at} {nonce} -->\n"


def next_version(versions) -> str:
    """已有号里最大的那个，补丁位 +1。"""
    top = max(versions, key=version_key)
    parts = top.split(".")
    if len(parts) != 3:
        raise ValueError(f"最大号 {top} 不是三段式，无法自动取下一个号")
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


def skipped_versions(frags, frozen_max: str = ARCHIVE_FROZEN_MAX) -> list:
    """冻结后的跳号，按 minor 系列分别算：档案所在系列从冻结最大号 +1 查起，之后的新系列从 `.0` 查起，
    各查到本系列最大的碎片号为止。比档案系列还低的碎片不在冻结后的范围里（lint 已判红）。

    v0.45.739 前只按档案那一个系列算：碎片一跨到下一个 minor，整段不查却照样报「无」。"""
    base = version_key(frozen_max)
    have = {version_key(v) for v in frags}
    gaps = []
    for series in sorted({k[:2] for k in have if k[:2] >= base[:2]}):
        first = base[2] + 1 if series == base[:2] else 0
        top = max(k[2] for k in have if k[:2] == series)
        gaps += [f"{series[0]}.{series[1]}.{n}" for n in range(first, top) if (*series, n) not in have]
    return gaps


def fragment_versions_of(files: dict) -> list:
    prefix = FRAGMENT_DIRNAME + "/"
    out = []
    for path in files:
        m = _FRAGMENT_NAME_RE.match(path[len(prefix):]) if path.startswith(prefix) else None
        if m:
            out.append(m.group(1))
    return sorted(out, key=version_key)


def _fragment_body(text: str) -> str:
    """碎片去掉认领注释后的条目文本（合并视图用）。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith(_CLAIM_PREFIX)).strip()


def archive_entries(text: str):
    """档案按 `## [` 标题切成条目：依次 yield (版本, 条目文本)，去掉条目尾部的空行与水平线。"""
    lines = text.splitlines()
    heads = [(i, m.group(1)) for i, m in ((i, _CHANGELOG_RE.match(ln)) for i, ln in enumerate(lines)) if m]
    for n, (i, version) in enumerate(heads):
        chunk = lines[i:heads[n + 1][0] if n + 1 < len(heads) else len(lines)]
        while chunk and (not chunk[-1].strip() or is_rule(chunk[-1])):
            chunk.pop()
        yield version, "\n".join(chunk)


def render(files: dict, archive_text: Optional[str], limit: Optional[int] = None) -> str:
    """合并视图：碎片按版本号倒序在前（去掉认领注释），接着是档案条目。**不提交**生成结果。"""
    blocks = [_fragment_body(_decode(files[fragment_path(v)]) or "") for v in reversed(fragment_versions_of(files))]
    if archive_text:
        blocks += [chunk for _, chunk in archive_entries(archive_text)]
    if limit is not None:
        blocks = blocks[:limit]
    head = ("# Alpha Hive · 版本变更历史（合并视图）\n\n"
            "> 由 `changelog_fragments.py render` 生成：`changelog.d/` 碎片在前、冻结档案 `CHANGELOG.md` 在后。"
            "**勿提交本文件。**\n")
    return head + "".join(f"\n---\n\n{b}\n" for b in blocks)


# ───────────────────────────────────────── 读 git 对象 / 工作区 ──────────

def _git(args, *, cwd, env=None, input=None, check=True, text=True):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, env=env, input=input, capture_output=True, text=text)
    except OSError as e:
        raise GitError(f"git {' '.join(args)} 起不来：{e}") from e
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise GitError(f"git {' '.join(args)} 退出码 {r.returncode}：{err.strip()[:300]}")
    return r


def _cat_blobs(shas, *, cwd) -> dict:
    """{sha: bytes}，一次 `git cat-file --batch` 读完。"""
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    r = _git(["cat-file", "--batch"], cwd=cwd, input=("\n".join(shas) + "\n").encode(), text=False)
    out, buf, pos = {}, r.stdout, 0
    for sha in shas:
        nl = buf.index(b"\n", pos)
        header = buf[pos:nl].split()
        if len(header) != 3 or header[1] != b"blob":
            raise GitError(f"cat-file 读 {sha} 得到 {buf[pos:nl][:80]!r}")
        size = int(header[2])
        out[sha] = buf[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return out


def _parse_entries(raw: bytes, *, index: bool):
    """`ls-tree -r -z` / `ls-files -s -z` 的输出 → [(mode, sha, stage, path)]。"""
    out = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        parts = meta.split()
        if index:                                   # <mode> <sha> <stage>
            mode, sha, stage = parts[0], parts[1], int(parts[2])
        else:                                       # <mode> <type> <sha>
            mode, sha, stage = parts[0], parts[2], 0
        out.append((mode.decode(), sha.decode(), stage, os.fsdecode(path)))
    return out


def _load(entries, *, cwd) -> dict:
    regular = [(m, s, p) for m, s, _, p in entries if m in ("100644", "100755")]
    blobs = _cat_blobs([s for _, s, _ in regular], cwd=cwd)
    files = {p: blobs[s] for _, s, p in regular}
    files.update({p: NotRegular(m) for m, _, _, p in entries if m not in ("100644", "100755")})
    return files


def tree_files(rev: str, *, cwd) -> dict:
    """提交 `rev` 里 `changelog.d/` 下的全部条目。"""
    r = _git(["ls-tree", "-r", "-z", "--full-tree", rev, "--", FRAGMENT_DIRNAME], cwd=cwd, text=False)
    return _load(_parse_entries(r.stdout, index=False), cwd=cwd)


def index_files(*, cwd):
    """暂存区里 `changelog.d/` 下的条目 → (files, 未解决冲突的路径)。认 `GIT_INDEX_FILE`（`commit -- <路径>` 用临时索引）。"""
    r = _git(["ls-files", "-s", "-z", "--", FRAGMENT_DIRNAME], cwd=cwd, text=False)
    entries = _parse_entries(r.stdout, index=True)
    unmerged = sorted({p for _, _, st, p in entries if st != 0})
    return _load([e for e in entries if e[2] == 0], cwd=cwd), unmerged


def blob_text(spec: str, *, cwd) -> Optional[str]:
    """`git cat-file -p <spec>`（`REV:路径` 或 `:路径` = 暂存区）。不存在 ⇒ None。"""
    r = _git(["cat-file", "-p", spec], cwd=cwd, check=False, text=False)
    return _decode(r.stdout) if r.returncode == 0 else None


def worktree_files(root: Path) -> dict:
    """`changelog.d/` 下**被跟踪**的条目（含已暂存的新文件），内容取**工作区**（没暂存的改动也查）。

    未跟踪 / 被忽略的文件不算（v0.45.733）：开发检出在 iCloud 同步下，Finder 会写 `.DS_Store`（.gitignore 已忽略），
    iCloud 会造 `0.45.730 2.md` 式副本——它们不会进提交（真 `git add` 进来时 pre-commit 照样拦），按目录扫却让
    全量测试为一个不会被提交的文件变红。口径同 `tests/_repo_files.py`：git 跟踪的才是本仓的。
    工作区里删掉了的被跟踪文件当它不在；冲突中的文件（多个 stage）照样列出，工作区里的冲突标记由 `lint` 报。"""
    r = _git(["ls-files", "-z", "--cached", "--", FRAGMENT_DIRNAME], cwd=root, text=False)
    files = {}
    for raw in r.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        full = Path(root) / rel
        if full.is_symlink():
            files[rel] = NotRegular("symlink")
        elif full.is_file():
            files[rel] = full.read_bytes()
        elif full.exists():
            files[rel] = NotRegular("not-a-file")
    return files


def check_index(root: Path) -> list:
    """pre-commit：暂存区里的碎片。"""
    files, unmerged = index_files(cwd=root)
    problems = [f"{p}：未解决的合并冲突（暂存区里有多个 stage）" for p in unmerged]
    return problems + lint(files, archive_top(blob_text(f":{ARCHIVE}", cwd=root)))


def check_commit(root: Path, sha: str, base: Optional[str]) -> list:
    """pre-push：被推提交 `sha` 里的碎片；`base`（远端 main）给了就再做认领保护。"""
    files = tree_files(sha, cwd=root)
    problems = lint(files, archive_top(blob_text(f"{sha}:{ARCHIVE}", cwd=root)))
    if base:
        problems += protect(tree_files(base, cwd=root), files)
    return problems


# ───────────────────────────────────────── 占号 ──────────

def _fetch(root, remote):
    _git(["fetch", "-q", remote, f"+{MAIN_REF}:refs/remotes/{remote}/main"], cwd=root)
    return _git(["rev-parse", "--verify", f"refs/remotes/{remote}/main^{{commit}}"], cwd=root).stdout.strip()


def _is_ancestor(a, b, *, cwd) -> bool:
    r = _git(["merge-base", "--is-ancestor", a, b], cwd=cwd, check=False)
    if r.returncode not in (0, 1):
        raise GitError(f"merge-base --is-ancestor 退出码 {r.returncode}：{r.stderr.strip()}")
    return r.returncode == 0


def _remote_main(root, remote) -> Optional[str]:
    r = _git(["ls-remote", remote, MAIN_REF], cwd=root, check=False)
    return r.stdout.split()[0] if r.returncode == 0 and r.stdout.strip() else None


def _who(root) -> str:
    r = _git(["symbolic-ref", "-q", "--short", "HEAD"], cwd=root, check=False)
    name = r.stdout.strip() if r.returncode == 0 else ""
    return re.sub(r"\s+", "_", name) or "detached"


def build_claim_commit(root, base: str, version: str, content: str, message: str) -> str:
    """在 `base` 上另建只多了 `changelog.d/<version>.md` 的提交（临时索引，不碰当前分支、索引与工作区）。"""
    blob = _git(["hash-object", "-w", "--stdin"], cwd=root, input=content).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="changelog-claim-") as td:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(td, "index")}
        _git(["read-tree", base], cwd=root, env=env)
        _git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{fragment_path(version)}"], cwd=root, env=env)
        tree = _git(["write-tree"], cwd=root, env=env).stdout.strip()
    return _git(["commit-tree", tree, "-p", base, "-F", "-"], cwd=root, input=message).stdout.strip()


def _claim_state(root, base: str):
    """origin/main（`base`）上的碎片与档案：(files, 档案顶号, 全部已有的号)。"""
    files = tree_files(base, cwd=root)
    top = archive_top(blob_text(f"{base}:{ARCHIVE}", cwd=root))
    versions = fragment_versions_of(files) + ([top] if top else [])
    return files, top, versions


def _abort_cherry_pick(root) -> str:
    """cherry-pick 停在半路（冲突）就撤回，别把仓库留在中途；git 拒绝动手（没开始）就什么也不做。返回给人看的补充说明。"""
    if _git(["rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"], cwd=root, check=False).returncode != 0:
        return ""
    ab = _git(["cherry-pick", "--abort"], cwd=root, check=False)
    if ab.returncode == 0:
        return "\n（已 `git cherry-pick --abort`：仓库回到执行前的状态。）"
    return (f"\n⚠️ `git cherry-pick --abort` 也失败了：{(ab.stderr or ab.stdout).strip()[:300]}\n"
            "仓库停在 cherry-pick 中途——先手动处理（`git status`）。")


def adopt(root, commit: str, remote: str = REMOTE) -> int:
    """把已推上 main 的占号提交带进当前分支：分支落后就快进，否则 cherry-pick。

    「已经带进来了」按**认领行**判、不按文件内容（v0.45.733）：带进来以后会话会改标题、补正文，内容早就不同了——
    按内容判会再 cherry-pick 一次、撞 add/add 冲突、把仓库留在 cherry-pick 中途（二次检查实测）。
    当前分支里已有同名碎片、认领行却不同 ⇒ 同一个号两份，拒绝，不去盖它。"""
    main = _fetch(root, remote)
    if not _is_ancestor(commit, main, cwd=root):
        print(f"❌ 远端 main 里还没有 {commit[:10]}——号**没占住**：先推送它（被拒就重跑 claim）。", file=sys.stderr)
        return 1
    added = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", f"{commit}^", commit], cwd=root).stdout.split()
    if len(added) != 1 or not added[0].startswith(FRAGMENT_DIRNAME + "/"):
        print(f"❌ {commit[:10]} 不是占号提交（它改了 {added[:5]}）", file=sys.stderr)
        return 1
    path = added[0]
    want = claim_of(blob_text(f"{commit}:{path}", cwd=root) or "")
    have_text = blob_text(f"HEAD:{path}", cwd=root)
    if have_text is not None:
        have = claim_of(have_text)
        if want and have == want:
            print(f"{path} 已在当前分支里（认领行一致），无需带入。")
            return 0
        print(f"❌ 当前分支里已经有 {path}，认领行却不是这个占号提交的——同一个号有两份，不去盖它：\n"
              f"  当前分支：{have or '（没有合规的认领行）'}\n  占号提交：{want or '（没有合规的认领行）'}\n"
              f"先查清分支里那份从哪来（`git log -- {path}`）。", file=sys.stderr)
        return 1
    if _is_ancestor("HEAD", commit, cwd=root):
        r = _git(["merge", "--ff-only", "-q", commit], cwd=root, check=False)
        if r.returncode == 0:
            print(f"已快进到占号提交 {commit[:10]}：{path} 在工作区里了。")
            return 0
    staged = _git(["diff", "--cached", "--quiet"], cwd=root, check=False)
    if staged.returncode not in (0, 1):
        raise GitError(f"git diff --cached --quiet 退出码 {staged.returncode}：{staged.stderr.strip()}")
    if staged.returncode == 1:
        print(f"❌ 暂存区有未提交的改动，cherry-pick 会拒绝——号已经占住（在 main 上），只是还没带进当前分支。\n"
              f"先提交（或撤销暂存）再重跑：adopt {commit}；别用裸 `git stash`（stash 栈是所有 worktree 共用的）。",
              file=sys.stderr)
        return 1
    r = _git(["cherry-pick", commit], cwd=root, check=False)
    if r.returncode != 0:
        note = _abort_cherry_pick(root)
        print(f"❌ cherry-pick {commit[:10]} 失败：{(r.stderr or r.stdout).strip()[:400]}{note}\n"
              f"号已经占住（在 main 上），只是没带进当前分支：处理完工作区后重跑 `adopt {commit}`，"
              "或完工前 `git rebase origin/main` 取回。", file=sys.stderr)
        return 1
    print(f"已 cherry-pick 占号提交 {commit[:10]}：{path} 在工作区里了（以后 rebase 到 main 时 git 会自动跳过这个重复提交）。")
    return 0


def _adopt_cmd(root, commit: str) -> str:
    """交给人的接入命令。adopt 作用于**当前目录**所在的 checkout，所以带 `cd`。"""
    return f"cd '{root}' && /usr/local/bin/python3 '{Path(__file__).resolve()}' adopt {commit}"


def _landed(root, remote, commit):
    """推送之后：占号提交在不在远端 main 上 → (在不在, 远端 main)。先问 ls-remote；对不上（推完别人紧跟着
    又推了）再 fetch 判祖先。fetch 失败抛 GitError——调用方必须按「不知道」处理，不能当成「没推上去」。"""
    if _remote_main(root, remote) == commit:
        return True, commit
    new_base = _fetch(root, remote)
    return _is_ancestor(commit, new_base, cwd=root), new_base


def _say_unverified(root, remote, commit, pushed, err) -> None:
    """推完核实不了远端（网络断了）。推送命令成功了 ⇒ 号多半已占住：只报一句「出错了」的话，人会重跑 claim、
    再占一个号，前一个变成挂着自己分支名的孤儿占位（v0.45.733 二次检查）。"""
    if pushed.returncode == 0:
        print(f"⚠️ 推送命令成功了（退出码 0），但之后核实远端失败：{err}\n"
              f"号**多半已经占住**——先确认再动：`git -C '{root}' ls-remote {remote} {MAIN_REF}`"
              f"（或 `changelog_fragments.py status`）。远端 main 是 {commit[:10]} 或含它 ⇒ 带进当前分支：\n"
              f"  {_adopt_cmd(root, commit)}\n**别直接重跑 claim**——那会再占一个号。", file=sys.stderr)
    else:
        print(f"❌ 推送失败（退出码 {pushed.returncode}），之后核实远端也失败：{err}\n"
              f"{(pushed.stderr or pushed.stdout).strip()[-1500:]}\n"
              f"号大概没占住；网络恢复后先 `changelog_fragments.py status` 看一眼再重跑 claim。", file=sys.stderr)


def _report_push_guard(root, pushed) -> None:
    """把推送时守卫的输出带出来：`capture_output` 会吞掉它，成功时就看不出守卫跑没跑（v0.45.733 二次检查）。
    没看到守卫放行的字样 ⇒ 这个 checkout 多半没装钩子、或钩子跑的是旧代码：这次的占位是工具自己生成、自己查过的，
    但以后推完工的碎片就没人拦了——要说出来，安静地成功正是「谁会红？」要防的。"""
    lines = [ln for ln in (pushed.stderr or "").splitlines() if ln.startswith("pre-push:")]
    for ln in lines:
        print(f"  {ln}")
    if not any(GUARD_PASS_MARK in ln for ln in lines):
        print(f"⚠️ 推送输出里没有守卫的「{GUARD_PASS_MARK}」——这个 checkout 多半没装 changelog_guard 钩子"
              "（或钩子跑的是旧代码）：这次的占位是工具自己生成并检查过的，但以后推完工的碎片不会被拦。装钩子：\n"
              f"  cd '{root}' && /usr/local/bin/python3 changelog_guard.py --install-hook", file=sys.stderr)


def claim(root, desc: str, *, push: bool, trailers=(), remote: str = REMOTE, now=None) -> int:
    desc = desc.strip()
    if not desc or "\n" in desc or "\r" in desc:
        print("❌ 说明要一行、非空", file=sys.stderr)
        return 2
    who = _who(root)
    for attempt in range(1, CLAIM_MAX_ATTEMPTS + 1):
        base = _fetch(root, remote)
        files, top, versions = _claim_state(root, base)
        if FRAGMENT_DIRNAME + "/" + README not in files:
            print(f"❌ {remote}/main 上还没有 {FRAGMENT_DIRNAME}/{README}——碎片协议还没合入 main，按旧规则占号。",
                  file=sys.stderr)
            return 1
        try:
            version = next_version(versions)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        mine = [v for v in fragment_versions_of(files)
                if is_placeholder(_decode(files[fragment_path(v)]) or "")
                and (claim_of(_decode(files[fragment_path(v)]) or "") or "").startswith(f"<!-- 认领：{who} ")]
        if mine and attempt == 1:
            print(f"⚠️ 本分支（{who}）在 main 上已有进行中的占位 {', '.join(mine)}；照样再占一个新号。")
        t = now or datetime.datetime.now().astimezone()
        content = render_placeholder(version, desc, who=who, at=t.isoformat(timespec="seconds"),
                                     nonce=secrets.token_hex(4), date=t.date().isoformat())
        bad = lint({fragment_path(version): content}, top)
        if bad:
            raise AssertionError(f"自己生成的占位碎片没过检查（工具的 bug）：{bad}")
        message = f"chore: 占号 {version}（{desc}；本提交只含 {fragment_path(version)} 占位碎片）\n"
        if trailers:
            message += "\n" + "\n".join(trailers) + "\n"
        commit = build_claim_commit(root, base, version, content, message)
        if not push:
            print(f"已建占号提交 {commit}（{version}，基于 {remote}/main {base[:10]}）——**还没推送，号还没占住**。\n"
                  f"请立刻推送（在这个 checkout 里执行，pre-push 守卫会检查它）：\n"
                  f"  git -C '{root}' push {remote} {commit}:{MAIN_REF}\n"
                  f"推上去之后带进当前分支（adopt 作用于当前目录所在的 checkout，所以先 cd）：\n"
                  f"  {_adopt_cmd(root, commit)}\n"
                  f"推送被拒（号被抢 / main 又动了）⇒ 重跑 claim。")
            return 0
        print(f"推送占号提交 {commit}（{version}）到 {remote}/main …")
        pushed = _git(["push", remote, f"{commit}:{MAIN_REF}"], cwd=root, check=False)
        try:
            ok, new_base = _landed(root, remote, commit)
        except GitError as e:
            _say_unverified(root, remote, commit, pushed, e)
            return 1
        if not ok and new_base == base:
            print(f"❌ 推送失败，而远端 main 没有前进——不是被抢号，重试也没用：\n"
                  f"{(pushed.stderr or pushed.stdout).strip()[-1500:]}", file=sys.stderr)
            return 1
        if not ok:
            print(f"第 {attempt} 次推送被拒：{remote}/main 前进了（{base[:10]} → {new_base[:10]}），重新取号。")
            continue
        print(f"✅ 已占 {version}：{remote}/main 含占号提交 {commit[:10]}（以 ls-remote 核实）。")
        _report_push_guard(root, pushed)
        try:
            rc = adopt(root, commit, remote)
        except GitError as e:
            print(f"❌ 号已占住（{version}，已核实），但带进当前分支失败：{e}\n"
                  f"稍后重跑这一步（**别重跑 claim**——那会再占一个号）：\n  {_adopt_cmd(root, commit)}", file=sys.stderr)
            rc = 1
        print(f"完工时把 {fragment_path(version)} 的占位标题换成正式标题、补正文；认领那行原样保留。")
        return rc
    print(f"❌ 连续 {CLAIM_MAX_ATTEMPTS} 次推送都被拒（main 前进太快）——稍后重跑 claim。", file=sys.stderr)
    return 1


# ───────────────────────────────────────── status / check / render ──────────

def status(root, *, rev: Optional[str], fetch: bool, remote: str = REMOTE) -> int:
    if fetch:
        _fetch(root, remote)
    rev = rev or f"refs/remotes/{remote}/main"
    sha = _git(["rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=root).stdout.strip()
    files = tree_files(sha, cwd=root)
    arch = blob_text(f"{sha}:{ARCHIVE}", cwd=root) or ""
    frags = fragment_versions_of(files)
    top_a = archive_top(arch)
    allv = frags + ([top_a] if top_a else [])
    print(f"{rev}（{sha[:10]}）：最大号 {max(allv, key=version_key) if allv else '—'}；下一个号 "
          f"{next_version(allv) if allv else '—'}（号以 claim 推送成功为准，这里只是快照）")
    print(f"碎片 {len(frags)} 个；档案 {ARCHIVE} 顶号 {top_a or '—'}（冻结值 {ARCHIVE_FROZEN_MAX} / {ARCHIVE_FROZEN_COUNT} 条）")
    open_rows = []
    for v in frags:
        text = _decode(files[fragment_path(v)]) or ""
        if is_placeholder(text):
            c = CLAIM_RE.match(claim_of(text) or "")
            open_rows.append(f"  {v}  碎片  {c['at'] if c else '?'}  {c['who'] if c else '?'}  {parse(text).heading['title']}")
    for line in arch.splitlines():
        m = _ARCHIVE_PLACEHOLDER_RE.match(line)
        if m:
            open_rows.append(f"  {m['version']}  档案（冻结前占的号，就地完成）  {m['date']}  「{m['desc']}」")
    print("进行中的占位：" + ("\n" + "\n".join(open_rows) if open_rows else "无"))
    if frags:
        gaps = skipped_versions(frags)
        print("跳号（冻结后，可能是别人占了还没推，也可能遗弃）：" + (", ".join(gaps) if gaps else "无"))
    return 0


def _load_view(root, rev: Optional[str]):
    """(碎片条目, 档案文本)：给了 `rev` 读那个提交；没给读工作区（被跟踪的文件，见 `worktree_files`）。
    `check` 与 `render` 共用这一份——各写一份的话，改一处漏一处，两者就会对 changelog.d/ 里有什么各执一词。"""
    if rev:
        sha = _git(["rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=root).stdout.strip()
        return tree_files(sha, cwd=root), blob_text(f"{sha}:{ARCHIVE}", cwd=root)
    p = Path(root) / ARCHIVE
    return worktree_files(root), (p.read_text(encoding="utf-8") if p.is_file() else None)


def check(root, *, rev: Optional[str]) -> int:
    files, arch = _load_view(root, rev)
    problems = lint(files, archive_top(arch)) + (archive_freeze_problems(arch) if arch is not None else [])
    for p in problems:
        print("❌ " + p)
    if not problems:
        print(f"碎片检查通过（{len(fragment_versions_of(files))} 个碎片；档案冻结断言通过）")
    return 1 if problems else 0


def render_cmd(root, *, rev: Optional[str], limit: Optional[int]) -> int:
    """只打印到标准输出、不写文件：生成结果没有读者（要存就 `> 路径`），
    写盘就得进 `tests/test_persistent_writes_have_readers.py` 的登记——「没人读就别写」。"""
    files, arch = _load_view(root, rev)
    sys.stdout.write(render(files, arch, limit))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim", help="占下一个号（在 origin/main 上另建只含占位碎片的提交）")
    c.add_argument("desc", help="一句话说明（进占位标题）")
    c.add_argument("--push", action="store_true", help="直接推送并以 ls-remote 核实；推不了 main 就别带，把打印的命令交给用户")
    c.add_argument("--trailer", action="append", default=[], help="占号提交信息末尾的行（可多次），如 Co-Authored-By")
    a = sub.add_parser("adopt", help="把已推上 main 的占号提交带进当前分支")
    a.add_argument("commit")
    s = sub.add_parser("status", help="最大号 / 下一个号 / 进行中的占位 / 跳号")
    s.add_argument("--rev", help="看哪个提交（缺省 origin/main）")
    s.add_argument("--no-fetch", action="store_true")
    k = sub.add_parser("check", help="碎片检查 + 档案冻结断言（缺省查工作区里被跟踪的文件；未跟踪 / 被忽略的不算）")
    k.add_argument("--rev")
    r = sub.add_parser("render", help="合并视图（碎片在前、档案在后）；**勿提交**生成结果")
    r.add_argument("--rev")
    r.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    # 作用于**当前目录所在的** checkout（同 git 自己的命令）：拿别处的脚本副本在自己的 worktree 里跑，
    # 占号提交也要带进自己的分支，而不是脚本所在那个 checkout 的分支
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        print(f"❌ 不在 git 工作区里：{top.stderr.strip()}", file=sys.stderr)
        return 1
    root = Path(top.stdout.strip())
    try:
        if args.cmd == "claim":
            return claim(root, args.desc, push=args.push, trailers=args.trailer)
        if args.cmd == "adopt":
            return adopt(root, args.commit)
        if args.cmd == "status":
            return status(root, rev=args.rev, fetch=not args.no_fetch)
        if args.cmd == "check":
            return check(root, rev=args.rev)
        return render_cmd(root, rev=args.rev, limit=args.limit)
    except GitError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
