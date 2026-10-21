#!/usr/bin/env python3
"""
🐝 Alpha Hive - 上游宏观日程发布监视器（v0.45.67）

`economic_calendar.py` 的四张表是硬编码的，只能覆盖官方**已发布**的范围。
2026-08-29 核对时，BLS 的 2027 CPI/NFP 日程、BEA 的 2027 GDP 日期都还没发布，
所以那三张表只到 2026-12。v0.45.65 给它加了地平线告警和一条会变红的单测，
但告警只会说「快见底了」，说不出「上游发了没」——那得有人每周去源站看一眼。

这个脚本就是那个人。

## 它做什么

去四个源站抓页面，问一个问题：**上游现在能提供的日程，超出我们表里已核对的
边界了吗？** 超出了就退出码 1，把发现写进日志和 JSON，等人去抄。

## 它不做什么 —— 这条比上面重要

**它绝不往 `economic_calendar.py` 里写日期。**

v0.45.65 修的那个 bug，本质是「用规律推算代替抄写」。如果这个监视器自动把抓到的
日期写进表里，等于把同一个错误自动化了：解析器对页面结构的每一个假设，都会变成
一批看起来像抄来的、实际是程序猜的日期。所以它只负责喊「去看」，抄写永远是人工的。

## 「找不到新东西」和「看不出来」必须分开

一个静默失败的监视器，就是它要治的那个 bug 本身升了一级：页面改版 → 解析器抓到
0 条 → 报告「无新日程」→ 一切正常 → 直到日历见底。

所以每个源都要求**解析出足够多的条目**（CPI/NFP ≥ 12、FOMC ≥ 8、GDP 两页合计 ≥ 1），
达不到就报 `undetermined`（退出码 3），绝不报「健康」。

## GDP 读两页（v0.45.574）

BEA 的 `/news/schedule`（Upcoming Releases）只列**当年**剩下的发布（10-21 实测表头 `Year 2026`、末条 12-23），
**次年整年只在 `/news/schedule/next-year`**。v0.45.574 之前只读前一页 ⇒ BEA 发了次年日程，监视器也要等
次年初值滚进「近期」窗口才看得见，其间对 GDP 报的「上游无新」（退出码 0）不可信——10-21 才查明 2027 早挂在
next-year 页上，而监视器与 10-04 两次手工核对都记成「BEA 2027 未发布」。

现在两页的初值季度取并集再比 `verified_through`；**两页都得查通**，GDP 才可判定。单页「0 条初值行」本身是
合法状态（滚动页过了 10 月末的 Q3 初值就一条都没有；次年页在 BEA 发布前没有内容），所以单页只要求**页面结构
可信**——判据见 `parse_bea_advance_quarters` / `parse_bea_next_year`；「≥ 1」的下限落在两页并集上。

## 抓取时限（v0.45.576）

五页并行抓、共用一个墙钟截止 `_FETCH_BUDGET_S`（编排器 Step 13 的 `run_step --timeout` 之内留余量，守卫
`TestFetchBudget`）。到点没回来的页只让它那张表「无法判定」——不能让一个慢源拖到编排器把整个进程杀掉、四表结论全丢。
`--timeout` 仍是单个源的 socket 超时。

## 退出码（沿用 Step 10/11/12 约定）

- 0 = 日历健康，且上游无新日程
- 1 = **要人动手**：上游发了新日程可抄，或本地日历已 stale/exhausted
- 3 = 无法判定（抓取失败 / 解析条目数不足 / 页面改版）
- 2 由编排器 run_step 占用（脚本不存在），本脚本不使用

## User-Agent

bls.gov 用 UA 做准入：浏览器 UA、`curl/8.4.0`、`python-urllib/3.11` 一律 403，
**只要 UA 里带一个形如邮箱的 token 就放行**（实测 `nobody@nowhere.invalid` 同样 200，
即它只校验格式、不校验地址）。默认 UA 因此用占位地址，不写入任何私人邮箱。
BLS 设这个字段本意是留联系人，想做个好公民就设环境变量：

    export ALPHA_HIVE_CONTACT_EMAIL="you@example.com"

## 输出契约（v0.45.355，`step_contract`）

`--out` 写 `step_contract.envelope(...)`：原有键原样留在顶层（编排器 Step 13 现行内联解析照读；
注意编排器读的是 `calendar_health.status`，与外壳顶层 `status` 不同层、不撞名）。
`date` = `--today`（v0.45.355 新增，缺省 `step_contract.business_today()` = 洛杉矶当日；本地体检与节流按这一天算）。
⚠️ 缺省值**不等于**编排器本机时区的 `DATE_STR`（2026-11-01 起冬令时每天有一小时差一天，见 `business_today`
的 docstring）——消费方要比新鲜度就显式传 `--today`。
0 ⇒ ok、1 ⇒ attention、3 ⇒ undetermined；未捕获异常 ⇒ 退出码 3 + `status: "error"` 外壳
（此前 Python 默认 1 = 「要人动手」，崩溃会被编排器当成「有新日程可抄」）。
`step_contract` 本身导入失败 ⇒ 入口也是退出码 3（不写 `--out`）；**本模块其余的导入期失败仍是 1**
（`run_tool` 兜不到 import，见其 docstring）。
`attention` 由 `contract_attention()` 显式列出：上游新日程 / 本地表低于地平线阈值各一条，
`deadline` = 该表覆盖到的最后一天（此后该类事件从日历里消失）。
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# `step_contract` 本身导入失败 ⇒ 置哨兵，`__main__` 入口见哨兵退出码 3——不让它落成 Python 默认的 1
# （= 本工具的「要人动手」）。⚠️ 只兜这一个 import：本模块其余的导入期失败仍是 1（见 step_contract.run_tool）。
try:
    import step_contract
    _STEP_CONTRACT_IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # noqa: BLE001 —— 连语法错也要兜：哨兵只记下原因，入口据此退出码 3
    step_contract = None
    _STEP_CONTRACT_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

_TOOL = "economic_calendar_watch"
#: 退出码 → 外壳 status（v0.45.355）。退出码约定本身不变；崩溃的 3 由 run_tool 写 "error"。
_STATUS_BY_RC = {0: "ok", 1: "attention", 3: "undetermined"}

_DEFAULT_CONTACT = "alpha-hive-calendar-watch@localhost.invalid"
_UA_TEMPLATE = "AlphaHive-calendar-watch/1.0 ({contact})"

# v0.45.160：`_STATE_PATH` 现在是**覆盖钩子**，默认 `None` ⇒ 运行时解析 `PATHS.cache_dir`。
# 原本是 `Path(__file__).resolve().parent / "cache" / ...`——它**压根不读环境变量**。
_STATE_PATH = None


def _state_path() -> Path:
    """节流状态文件。**调用时求值。**"""
    if _STATE_PATH is not None:
        return Path(_STATE_PATH)
    from hive_logger import PATHS
    return Path(PATHS.cache_dir) / "economic_calendar_watch.json"
_DEFAULT_MAX_AGE_DAYS = 7

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# 每个源至少要解析出这么多条，否则判为「看不出来」而非「没有新的」
_MIN_PARSED = {"fomc": 8, "cpi": 12, "nfp": 12, "gdp": 1}

_SOURCES = {
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "cpi": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "nfp": "https://www.bls.gov/schedule/news_release/empsit.htm",
    "gdp": "https://www.bea.gov/news/schedule",
}

#: GDP 的第二页：BEA 次年日程（v0.45.574，见模块 docstring「GDP 读两页」）。不放进 `_SOURCES`：
#: 那张表的键是日历表名，一张表一个结论；这一页只是 gdp 那个结论的另一半证据。
_BEA_NEXT_YEAR = "https://www.bea.gov/news/schedule/next-year"


def _source_label(key: str) -> str:
    """一个表的结论出自哪些页面。GDP 是两页，其余一页。"""
    return f"{_SOURCES[key]} + {_BEA_NEXT_YEAR}" if key == "gdp" else _SOURCES[key]


# ──────────────────────────── 抓取与文本化 ────────────────────────────

def _user_agent() -> str:
    contact = os.environ.get("ALPHA_HIVE_CONTACT_EMAIL", "").strip() or _DEFAULT_CONTACT
    return _UA_TEMPLATE.format(contact=contact)


def _fetch(url: str, timeout: int, ua: str) -> str:
    """抓页面。urllib 默认认 http_proxy/https_proxy 环境变量，
    因此自动继承编排器 v0.45.66 探活后设置的代理。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        final = resp.geturl()
    # v0.45.579：urllib 会**静默**跟随跳转。跳到别的路径 = 读到的不是要读的那页——最要命的是次年页被跳回
    # 滚动页：两页同一张表结构，照样解析得通，次年的空缺会被读成「上游无新」。按抓取失败记（ValueError ∈
    # `_FETCH_ERRORS`）⇒ 该表「无法判定」。只比路径（忽略协议 / 主机 / 末尾斜杠）；10-21 实测五个源都不跳转。
    if _url_path(final) != _url_path(url):
        raise _Redirected(f"{url} 被跳转到 {final}，读到的不是要读的那页")
    return raw.decode("utf-8", errors="replace")


class _Redirected(ValueError):
    """抓取被跳转到别的路径（v0.45.579）。"""


def _url_path(u: str) -> str:
    return urllib.parse.urlsplit(u).path.rstrip("/")


def _visible_lines(html: str) -> List[str]:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", "\n", s))
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


# ──────────────────────────── 各源解析 ────────────────────────────

def parse_fomc(html: str) -> Tuple[List[date], Optional[str]]:
    """美联储日历：`YYYY FOMC Meetings` 标题 + `Month` + `D-D`。
    取两日会议的第二天 = 声明发布日，与 _FOMC 表口径一致。"""
    lines = _visible_lines(html)
    month_names = {m.lower(): i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)}
    out: List[date] = []
    year: Optional[int] = None
    for i, ln in enumerate(lines):
        m = re.fullmatch(r"(20\d\d) FOMC Meetings", ln)
        if m:
            year = int(m.group(1))
            continue
        if year is None or ln.lower() not in month_names:
            continue
        if i + 1 >= len(lines):
            continue
        nxt = lines[i + 1]
        # "26-27"、"16-17*"、"8-9*"；"22 (notation vote)" 之类非例会跳过
        dm = re.fullmatch(r"(\d{1,2})-(\d{1,2})\*?", nxt)
        if not dm:
            continue
        day = int(dm.group(2))
        try:
            out.append(date(year, month_names[ln.lower()], day))
        except ValueError:
            continue
    # 脚注里那条「A two-day meeting is scheduled for January 25-26, 2028」
    for ln in lines:
        fm = re.search(r"scheduled for ([A-Z][a-z]+) (\d{1,2})-(\d{1,2}), (20\d\d)", ln)
        if fm and fm.group(1).lower() in month_names:
            try:
                out.append(date(int(fm.group(4)), month_names[fm.group(1).lower()],
                                int(fm.group(3))))
            except ValueError:
                pass
    return sorted(set(out)), None


def parse_bls_schedule(html: str) -> Tuple[List[date], Optional[str]]:
    """BLS 发布日程表：Reference Month | Release Date | Release Time。
    只取发布日期列的 `Mon. DD, YYYY`。"""
    lines = _visible_lines(html)
    # 页面 <title> 与表头同文，`next()` 会命中第 0 行的 title，把整段导航纳入切片。
    # 目前导航里恰好没有能被日期正则误吃的行（都是 "FEBRUARY 2026" 这种格式），
    # 但那是运气不是设计。锚到真表头：它后面紧跟着 "Reference Month" 列名。
    i = None
    for n, ln in enumerate(lines):
        if ln.startswith("Schedule of Releases for") and \
                any("Reference Month" in x for x in lines[n + 1:n + 4]):
            i = n
            break
    if i is None:
        return [], "页面找不到「Schedule of Releases for …」+「Reference Month」表头，疑为改版"
    try:
        j = next(n for n, ln in enumerate(lines)
                 if n > i and "Subscribe to the BLS Online Calendar" in ln)
    except StopIteration:
        j = len(lines)

    out: List[date] = []
    for ln in lines[i:j]:
        m = re.fullmatch(r"([A-Z][a-z]{2})\.?\s+(\d{1,2}),\s*(20\d\d)", ln)
        if not m:
            continue
        mon = _MONTHS.get(m.group(1).lower())
        if not mon:
            continue
        try:
            out.append(date(int(m.group(3)), mon, int(m.group(2))))
        except ValueError:
            continue
    return sorted(set(out)), None


#: 初值行标题。⚠️ Q4 那行写作 `4th Quarter and Year 2026`（该季初值连带发全年数）——v0.45.574 前的正则
#: 要求 `Quarter 2026` 紧挨着，**每年的 Q4 初值行都读不到**（10-21 次年页 4 行只认出 3 行），且无声。
_BEA_ADVANCE_RE = re.compile(
    r"GDP \(Advance Estimate\).*?(\d)(?:st|nd|rd|th) Quarter(?: and Year)? (20\d\d)")
#: 首次估计的「季度在前、标签在后」写法（v0.45.576）。两种都在真实页面上出现过（Wayback 实测）：
#: - `Gross Domestic Product, 4th Quarter and Year 2024 (Advance Estimate)`：~2025-12 之前 BEA 的标题写法；
#: - `Gross Domestic Product, 3rd Quarter 2025 (Initial Estimate)`：停摆后 3Q25 的首次估计改名发布（原文写明它取代
#:   原定 10-30 的 advance estimate；`economic_calendar._GDP` 记它为该季首次估计 2025-12-23）。
_BEA_TRAILING_FIRST_RE = re.compile(
    r"(?:GDP|Gross Domestic Product)\b.*?(\d)(?:st|nd|rd|th) Quarter(?: and Year)? (20\d\d)"
    r" \((?:Advance|Initial) Estimate\)")
#: BEA 未发布次年日程时次年页的样子（Wayback 2025-02 ~ 2026-08 全部未发布快照同形）：Drupal 空视图，
#: **没有表头、没有日期格**，只有这一句（有时包在 <p> 里）。v0.45.574 当时约定的「有表头的空表」从未观测到。
_BEA_NEXT_YEAR_EMPTY_RE = re.compile(r"(?i)see the next year tab for upcoming releases\.?")
#: GDP 估计行的类型标签 `(X Estimate)`（大小写不敏感，取出后首字母大写比对）
_BEA_ESTIMATE_LABEL_RE = re.compile(r"(?i)\((\w+) estimate\)")
#: 认识的标签。Advance / Initial = 首次估计（必须读出季度）；其余是同一季度的后续修订，与表无关。
#: **不认识的标签 ⇒ 看不懂**（v0.45.576 二次检查）：BEA 改过一次名（Advance → Initial），再改一次的话，
#: 改名后的首次估计行会被当成「不是初值」静默跳过——而滚动页 0 条初值本是合法读数，次年页又只在
#: 「有发布却没有 GDP 初值」时才报警，改名就此无声。实测页面上出现过的五种都在这里。
_BEA_FIRST_LABELS = frozenset({"Advance", "Initial"})
_BEA_KNOWN_LABELS = _BEA_FIRST_LABELS | {"Second", "Third", "Updated"}
#: BEA 日程表的表头单元格（`<th>Year 2027</th>`）
_BEA_YEAR_HEADER_RE = re.compile(r"Year (20\d\d)")
#: 发布日期单元格（`<div class="release-date">January 28</div>`，不带年份）
_BEA_DATE_CELL_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r" \d{1,2}")


def _read_bea_page(html: str) -> Dict[str, Any]:
    """BEA 日程页的原始读数：初值季度、读不出季度的「像初值」行、表头年份、表头之后的日期格数。

    「像初值」= 同时提到 GDP（或 Gross Domestic Product）与 advance。这种行读不出季度时**不能当它不存在**：
    v0.45.574 前 Q4 那行正是这样被静默漏掉的。
    """
    lines = _visible_lines(html)
    quarters: List[Tuple[int, int]] = []
    unreadable: List[str] = []
    unknown_label: List[str] = []
    for ln in lines:
        m = _BEA_ADVANCE_RE.search(ln) or _BEA_TRAILING_FIRST_RE.search(ln)
        if m:
            quarters.append((int(m.group(2)), int(m.group(1))))
            continue
        if not re.search(r"(?i)\bGDP\b|gross domestic product", ln):
            continue
        labels = {lb.capitalize() for lb in _BEA_ESTIMATE_LABEL_RE.findall(ln)}
        if re.search(r"(?i)\badvance\b", ln) or labels & _BEA_FIRST_LABELS:
            unreadable.append(ln)           # 像首次估计，却读不出参考季度
        elif labels - _BEA_KNOWN_LABELS:
            unknown_label.append(ln)        # GDP 估计行，类型标签不认识（可能是首次估计改了名）
    header_at = [n for n, ln in enumerate(lines) if _BEA_YEAR_HEADER_RE.fullmatch(ln)]
    years = [int(_BEA_YEAR_HEADER_RE.fullmatch(lines[n]).group(1)) for n in header_at]
    dated = (sum(1 for ln in lines[header_at[0]:] if _BEA_DATE_CELL_RE.fullmatch(ln))
             if header_at else 0)
    return {"quarters": sorted(set(quarters)), "unreadable": unreadable,
            "unknown_label": unknown_label, "year_headers": years, "dated_rows": dated,
            # v0.45.576：未发布空视图的判据要看**全页**（没有表头时 dated_rows 恒 0，等于没看）
            "date_cells_anywhere": sum(1 for ln in lines if _BEA_DATE_CELL_RE.fullmatch(ln)),
            "empty_notice": any(_BEA_NEXT_YEAR_EMPTY_RE.fullmatch(ln) for ln in lines)}


def _unreadable_reason(page: Dict[str, Any], what: str) -> Optional[str]:
    rows = page["unreadable"]
    if rows:
        return (f"{what}有 {len(rows)} 行像 GDP 初值却读不出参考季度（如 {rows[0]!r}），疑为改版——"
                f"读不出来的初值行不能当它不存在")
    odd = page["unknown_label"]
    if odd:
        return (f"{what}有 {len(odd)} 行 GDP 估计的类型标签不认识（如 {odd[0]!r}；认识的是 "
                f"{'/'.join(sorted(_BEA_KNOWN_LABELS))}），疑为改名——可能是首次估计换了叫法，不能当它不是初值")
    return None


def parse_bea_advance_quarters(html: str) -> Tuple[List[Tuple[int, int]], Optional[str]]:
    """BEA 滚动日程页（`/news/schedule`）：行标题形如 `GDP (Advance Estimate), 3rd Quarter 2026`。

    这里比对的是**参考季度**而不是发布日期 —— BEA 的日期单元格是
    `<div class="release-date">October 29</div>`，**不带年份**（年份只在区块标题里），
    跨年时会歧义；而标题里的季度自带年份，无歧义。

    **0 条初值行是合法读数**（v0.45.574）：这页只列当年剩下的发布，10 月末的 Q3 初值一过就一条都没有。
    但只有页面证明自己还是那张日程表（找得到 `Year YYYY` 表头）时才信它；表头和初值行都没有 ⇒ 判改版。
    「至少一条」的下限改由 `check_upstream` 落在两页并集上。
    """
    page = _read_bea_page(html)
    err = _unreadable_reason(page, "")
    if err:
        return [], err
    if not page["quarters"] and not page["year_headers"]:
        return [], ("页面找不到任何「GDP (Advance Estimate), Nth Quarter YYYY」行，"
                    "也找不到「Year YYYY」表头，疑为改版")
    return page["quarters"], None


def parse_bea_next_year(html: str) -> Tuple[Optional[int], List[Tuple[int, int]], Optional[str]]:
    """BEA 次年日程页（`/news/schedule/next-year`）→ (表头年份, 初值季度, 错误)。

    与滚动页同一张表的结构（10-21 实测表头 `Year 2027`，Q4 2026 + Q1–Q3 2027 四行初值）。

    这页**一年里大半时间合法地为空**：BEA 每年约 9~10 月才挂出次年日程（Wayback：2026 日程在 2025-08-28~11-01 之间、
    2027 日程在 2026-08-08~10-07 之间挂出）。「还没发」只认**观测到的那个形状**，别的空都判看不懂：
    - **未发布空视图** ⇒ **还没发**（可判定，贡献 0 个季度）：`See the Next Year tab for upcoming releases.` 那句提示，
      全页没有日期格、没有初值行、没有可疑的 GDP 行（v0.45.576 修：574 当时约定「有表头的空表 = 还没发」，
      而真实的未发布页**没有表头** ⇒ 每年 1 月起到 BEA 挂出次年前，GDP 每周报「无法判定」）。
    - 找不到 `Year YYYY` 表头、也不是上面那个空视图 ⇒ **无法判定**（改版 / 抓到错误页）。
    - 有表头、表里一条有日期的发布都没有 ⇒ **无法判定**：从未观测过的形状，不当「还没发」（574 正是这样猜错的）。
    - 有表头、列了有日期的发布却一条 GDP 初值都没有 ⇒ **无法判定**：BEA 次年日程是整年一起挂的，
      GDP 是它的头号发布，「别的都排了、唯独没 GDP」更像标题改版。
    - 有「像初值」却读不出季度、或 GDP 估计标签不认识的行 ⇒ **无法判定**（同滚动页）。
    """
    page = _read_bea_page(html)
    if not page["year_headers"]:
        err = _unreadable_reason(page, "次年页")
        if (page["empty_notice"] and not err and not page["quarters"]
                and page["date_cells_anywhere"] == 0):
            return None, [], None                        # BEA 还没挂出次年日程（观测到的形状）
        return None, [], err or ("次年页找不到「Year YYYY」表头，也不是「还没发」时的空视图，疑为改版——"
                                 "分不清「BEA 还没发次年日程」与「页面看不懂」")
    year = max(page["year_headers"])
    err = _unreadable_reason(page, f"次年页（Year {year}）")
    if err:
        return year, [], err
    if not page["dated_rows"]:
        return year, [], (f"次年页有表头（Year {year}）却一条有日期的发布都没有——从未观测过的形状"
                          f"（BEA 未发布时是无表头的空视图），疑为改版")
    if not page["quarters"]:
        return year, [], (f"次年页（Year {year}）列了 {page['dated_rows']} 条有日期的发布，"
                          f"却没有一条 GDP 初值行，疑为改版")
    return year, page["quarters"], None


def _release_date_to_quarter(d: date) -> Optional[Tuple[int, int]]:
    """由 GDP 初值的**发布月**反推参考季度。

    这是 BEA 发布节奏的固有性质（初值在季度结束后约一个月发），不是对日期的猜测：
    1月→上年Q4、4月→Q1、7月→Q2、10月→Q3。落在其它月份说明口径变了，返回 None。
    """
    return {1: (d.year - 1, 4), 4: (d.year, 1),
            7: (d.year, 2), 10: (d.year, 3)}.get(d.month)


# ──────────────────────────── 主检查 ────────────────────────────

def _load_state(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _cache_shape_problem(state: Any) -> Optional[str]:
    """节流缓存的形状对不对（v0.45.579）。对 ⇒ None；不对 ⇒ 一句原因。

    核的是**容器**：`check()` / `_render` 会对它 `.get` / `in` 的每一层——顶层、`upstream`（若有）、其中每个表的
    结论（若有）、GDP 结论里的 `pages`（若有）与其中每一页（若有）都得是对象。读坏的 JSON 早已由 `_load_state` 变成
    `{}`（= 从未查过），这里管的是「能解析、形状不对」。字段值（如 `new_items` 的类型）不在这里核：坏了在节流读时
    照崩（响亮），`--force` / 过了节流窗口的那一轮整份重写。
    v0.45.585：补 `pages` 两层——579 漏了它，`{"gdp": {"pages": 5}}` 在节流判断前就 TypeError，连 `--force` 都救不回。
    """
    if not isinstance(state, dict):
        return f"状态文件顶层是 {type(state).__name__}，不是对象"
    upstream = state.get("upstream")
    if upstream is None:
        return None
    if not isinstance(upstream, dict):
        return f"状态文件的 upstream 是 {type(upstream).__name__}，不是对象"
    for key, entry in upstream.items():
        if not isinstance(entry, dict):
            return f"状态文件的 upstream.{key} 是 {type(entry).__name__}，不是对象"
        pages = entry.get("pages")
        if pages is None:
            continue
        if not isinstance(pages, dict):
            return f"状态文件的 upstream.{key}.pages 是 {type(pages).__name__}，不是对象"
        for name, page in pages.items():
            if page is not None and not isinstance(page, dict):
                return f"状态文件的 upstream.{key}.pages.{name} 是 {type(page).__name__}，不是对象"
    return None


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        print(f"⚠️  监视器状态写入失败（不影响本次结论）: {e}", file=sys.stderr)


def _days_since(iso: Optional[str], today: date) -> Optional[int]:
    if not iso:
        return None
    try:
        return (today - datetime.fromisoformat(iso).date()).days
    except (ValueError, TypeError):
        return None


_FETCH_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError)

#: 全部抓取共用的**墙钟**总时限（秒，v0.45.576）。编排器 Step 13 以 `run_step --timeout 120` 跑本脚本，
#: 超时整个进程被杀、连 `--out` 都不写 ⇒ 四张表一周的结论全丢，只因为一个源慢。`urlopen(timeout=)` 管不住
#: 墙钟：它是**单次 socket 操作**的超时（连接、握手、每次读各算一次），v0.45.574 起 5 页串行抓，最坏远超 120。
#: 所以各页并行抓、统一截止：到点还没回来的页按「抓取失败」记，只让它自己那张表「无法判定」（退出码 3，会响）。
#: 与编排器那个数的余量由 tests/test_economic_calendar_watch.py::TestFetchBudget 守。
_FETCH_BUDGET_S = 75.0


class _FetchDeadline(TimeoutError):
    """到总时限还没取回（是 OSError ⇒ 落进 `_FETCH_ERRORS`，与其它抓取失败同一条路）。"""


def _fetch_all(urls: List[str], timeout: int, ua: str,
               budget: Optional[float] = None) -> Dict[str, Tuple[Optional[str], Optional[BaseException]]]:
    """并行抓 `urls`，在 `budget` 秒（缺省 `_FETCH_BUDGET_S`，调用时求值）内收齐。

    返回 {url: (html, None) | (None, 异常)}。线程是 daemon：到点没回来的就丢下不等，进程照常退出。
    `_fetch` 抛出的**任何** Exception 都原样带回，由调用方按原规矩处理（`_FETCH_ERRORS` 记无法判定，
    其余照旧向上抛 ⇒ run_tool 的崩溃外壳），不在这里改写失败的性质。
    """
    budget = _FETCH_BUDGET_S if budget is None else budget
    got: Dict[str, Tuple[Optional[str], Optional[BaseException]]] = {}

    def _one(u: str) -> None:
        try:
            got[u] = (_fetch(u, timeout, ua), None)
        except Exception as e:  # noqa: BLE001 —— 带回主线程按原规矩分流
            got[u] = (None, e)

    threads = [threading.Thread(target=_one, args=(u,), daemon=True,
                                name=f"calendar-watch-fetch-{n}") for n, u in enumerate(urls)]
    deadline = time.monotonic() + budget
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    snapshot = dict(got)          # 截止之后才回来的线程写不进本次结论
    return {u: snapshot.get(u) or (None, _FetchDeadline(f"超出抓取总时限 {budget:g}s，未取回"))
            for u in urls}


def _take(fetched: Dict[str, Tuple[Optional[str], Optional[BaseException]]], url: str) -> str:
    """取 `_fetch_all` 的结果：成功给 html，失败把当时的异常原样抛出（调用方的 except 分流不变）。"""
    html, err = fetched[url]
    if err is not None:
        raise err
    return html


def _fmt_q(q: Tuple[int, int]) -> str:
    return f"{q[0]}Q{q[1]}"


def _check_gdp(ours: date, fetched: Dict[str, Tuple[Optional[str], Optional[BaseException]]]) -> Dict[str, Any]:
    """GDP：抓滚动页 + 次年页，初值季度取并集，再与 `verified_through` 反推出的参考季度比（v0.45.574）。

    返回要 `update` 进该表 entry 的字段。**任一页抓不到 / 看不懂 ⇒ 整个 GDP 判无法判定**——哪怕另一页已经
    显示有新季度：只看半边证据下结论，正是本版要治的那个盲区；人反正要去源站，`pages` 里留着各页读数。
    """
    pages: Dict[str, Dict[str, Any]] = {}
    union: set = set()
    problems: List[str] = []
    for name, label, url in (("rolling", "滚动页", _SOURCES["gdp"]),
                             ("next_year", "次年页", _BEA_NEXT_YEAR)):
        page: Dict[str, Any] = {"source": url}
        pages[name] = page
        try:
            html = _take(fetched, url)
        except _FETCH_ERRORS as e:
            page["reason"] = f"抓取失败: {type(e).__name__}: {e}"
        else:
            try:
                if name == "next_year":
                    year, quarters, err = parse_bea_next_year(html)
                    page["year_header"] = year
                else:
                    quarters, err = parse_bea_advance_quarters(html)
            except Exception as e:  # noqa: BLE001
                quarters, err = [], f"解析异常: {type(e).__name__}: {e}"
            page["reason"] = err
            if not err:
                page["quarters"] = [_fmt_q(q) for q in quarters]
                union.update(quarters)
        if page["reason"]:
            problems.append(f"{label} {url}: {page['reason']}")

    out: Dict[str, Any] = {"pages": pages, "source": _source_label("gdp")}
    if problems:
        out["reason"] = "；".join(problems)
        return out
    out["parsed_count"] = len(union)
    if len(union) < _MIN_PARSED["gdp"]:
        out["reason"] = (f"滚动页与次年页合计只解析出 {len(union)} 条初值行，低于下限 "
                         f"{_MIN_PARSED['gdp']}，判为无法判定")
        return out
    our_q = _release_date_to_quarter(ours)
    if our_q is None:
        out["reason"] = (f"本地最后一条 GDP 日期 {ours} 的发布月不在 1/4/7/10，"
                         f"无法反推参考季度")
        return out
    newer = sorted(q for q in union if q > our_q)
    new_items = [_fmt_q(q) for q in newer]
    if new_items:
        # `source` 只在有新条目时被人读（渲染 / attention / 编排器 Step 13 摘要）⇒ 指向新条目所在的页。
        # 10-04 的手工核对正是去错了页。
        out["source"] = " + ".join(p["source"] for p in pages.values()
                                   if set(p.get("quarters") or ()) & set(new_items))
    out.update({
        "determinable": True,
        "our_latest_quarter": _fmt_q(our_q),
        "upstream_latest_quarter": _fmt_q(max(union)),
        "new_available": bool(newer),
        "new_items": new_items,
    })
    return out


def check_upstream(timeout: int = 20, ua: Optional[str] = None) -> Dict[str, Any]:
    """抓各源，逐表判断上游是否已超出我们的 verified_through（GDP 两页，见 `_check_gdp`）"""
    from economic_calendar import _TABLE_SPECS

    ua = ua or _user_agent()
    result: Dict[str, Dict[str, Any]] = {}
    # v0.45.576：五页一起抓、共用一个墙钟截止（见 `_FETCH_BUDGET_S`）
    fetched = _fetch_all(list(_SOURCES.values()) + [_BEA_NEXT_YEAR], timeout, ua)

    for key, url in _SOURCES.items():
        spec = _TABLE_SPECS[key]
        ours = date.fromisoformat(spec["verified_through"])
        entry: Dict[str, Any] = {
            "source": url,
            "our_verified_through": spec["verified_through"],
            "determinable": False,
            "new_available": False,
            "reason": None,
        }
        if key == "gdp":
            entry.update(_check_gdp(ours, fetched))
            result[key] = entry
            continue
        try:
            html = _take(fetched, url)
        except _FETCH_ERRORS as e:
            entry["reason"] = f"抓取失败: {type(e).__name__}: {e}"
            result[key] = entry
            continue

        try:
            if key == "fomc":
                dates, err = parse_fomc(html)
            else:
                dates, err = parse_bls_schedule(html)
        except Exception as e:  # noqa: BLE001
            entry["reason"] = f"解析异常: {type(e).__name__}: {e}"
            result[key] = entry
            continue

        if err:
            entry["reason"] = err
            result[key] = entry
            continue

        entry["parsed_count"] = len(dates)
        if len(dates) < _MIN_PARSED[key]:
            entry["reason"] = (f"只解析出 {len(dates)} 个日期，低于下限 "
                               f"{_MIN_PARSED[key]}，判为无法判定（疑为页面改版）")
            result[key] = entry
            continue
        newer = [d for d in dates if d > ours]
        entry.update({
            "determinable": True,
            "upstream_latest": max(dates).isoformat(),
            "new_available": bool(newer),
            "new_items": [d.isoformat() for d in newer],
        })

        result[key] = entry

    return result


def check(force: bool = False, max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
          state_path: Optional[Path] = None, timeout: int = 20,
          today: Optional[date] = None) -> Dict[str, Any]:
    """本地体检（免费、每次都做）+ 上游探查（按周节流）"""
    state_path = Path(state_path) if state_path else _state_path()   # v0.45.160：调用时求值
    from economic_calendar import get_calendar_health

    today = today or date.today()
    health = get_calendar_health(ref_date=today)
    _b = health["per_table"][health["binding_table"]]
    state = _load_state(state_path)
    # v0.45.579：缓存形状不可信 ⇒ 当没有缓存（本轮联网重查、整份重写 = 自愈），并在结果里写明。
    # 此前下面几行对它 `.get` 会 AttributeError：574 起连 --force 也在节流判断前就崩，只能手删缓存文件。
    cache_problem = _cache_shape_problem(state)
    if cache_problem:
        state = {}
    age = _days_since(state.get("last_checked_at"), today)

    prev_action = bool(state.get("action_required"))
    # v0.45.574：缓存里的 GDP 结论若出自只读滚动页的旧版，它的「无新」正是本版要治的盲区，不能再拿来用
    _cached_gdp = (state.get("upstream") or {}).get("gdp")
    gdp_cache_predates_next_year = bool(_cached_gdp) and "next_year" not in (_cached_gdp.get("pages") or {})
    # 上次查出「有新日程可抄」而人还没抄 → 不节流，天天提醒；GDP 缓存出自旧版 → 不节流，立即重查
    throttled = (not force and age is not None and age < max_age_days
                 and not prev_action and not gdp_cache_predates_next_year)

    if throttled:
        upstream = dict(state.get("upstream") or {})
        # v0.45.68 二次检查：缓存里缺哪个源，就把哪个源判成「无法判定」。
        # 旧实现直接用缓存的空 dict —— 于是「从未成功检查过任何源」被算成
        # undeterminable=[]、new=[]，一路报「✅ 上游无新日程」。
        # 监视器自己犯了它要治的那个静默失败。
        for _k in _SOURCES:
            if not upstream.get(_k):
                upstream[_k] = {
                    "source": _source_label(_k),
                    "determinable": False,
                    "new_available": False,
                    "reason": "节流窗口内且无缓存结果 —— 此源从未被成功检查过",
                }
        checked_now = False
    else:
        upstream = check_upstream(timeout=timeout)
        checked_now = True

    new_tables = sorted(k for k, v in upstream.items() if v.get("new_available"))
    undet = sorted(k for k, v in upstream.items() if not v.get("determinable"))

    res: Dict[str, Any] = {
        "checked_at": today.isoformat(),
        "cache_problem": cache_problem,
        "network_checked_this_run": checked_now,
        "throttled": throttled,
        "days_since_last_check": age,
        "max_age_days": max_age_days,
        # ⚠️ binding_table（余量最小、最先报警）与 shortest_table（地平线最短）
        # 常常不是同一张表：阈值逐表不同。把甲的表名和乙的天数拼进同一句话，
        # 就是本项目反复在治的「列名说谎」。两组各自成套，不混用。
        "calendar_health": {
            "status": health["status"],
            "binding_table": health["binding_table"],
            "binding_last_date": _b["last_date"],
            "binding_horizon_days": _b["horizon_days"],
            "binding_margin_days": _b["margin_days"],
            "shortest_table": health["shortest_table"],
            "shortest_last_date": health["last_date"],
            "shortest_horizon_days": health["horizon_days"],
        },
        "upstream": upstream,
        "new_schedule_tables": new_tables,
        "undeterminable_tables": undet,
        # 「上游没有新日程」这句话，只有四个源都查通了才有资格说。
        # 少一个源，就只能说「不知道」——见 main() 的文案分支。
        "upstream_conclusive": not undet,
        "action_required": bool(new_tables),
    }

    if checked_now:
        state.update({
            "last_checked_at": datetime.now().isoformat(timespec="seconds"),
            "upstream": upstream,
            "action_required": bool(new_tables),
        })
        _save_state(state_path, state)

    return res


# ──────────────────────────── 渲染与 CLI ────────────────────────────

def _render(res: Dict[str, Any]) -> str:
    h = res["calendar_health"]
    out = ["🗓️  宏观日程监视器", ""]
    out.append(f"本地日历：{h['status']} — 最先报警的是 {h['binding_table']} 表，"
               f"覆盖到 {h['binding_last_date']}，还剩 {h['binding_horizon_days']} 天"
               f"（距阈值 {h['binding_margin_days']:+d} 天）")
    if h["shortest_table"] != h["binding_table"]:
        out.append(f"          地平线最短的是 {h['shortest_table']} 表："
                   f"覆盖到 {h['shortest_last_date']}，还剩 "
                   f"{h['shortest_horizon_days']} 天（阈值更宽松，故不是它先报警）")
    if res.get("cache_problem"):
        out.append(f"⚠️ 节流缓存形状不对，已当作没有缓存、本轮联网重查并整份重写：{res['cache_problem']}")
    if res["throttled"]:
        out.append(f"上游：本次未联网（{res['days_since_last_check']} 天前查过，"
                   f"节流窗口 {res['max_age_days']} 天），下方为上次结果")
    out.append("")
    for key, v in sorted(res["upstream"].items()):
        if not v.get("determinable"):
            out.append(f"  ❓ {key:5s} 无法判定 — {v.get('reason')}")
            continue
        if key == "gdp":
            cur = f"上游最新参考季度 {v['upstream_latest_quarter']}（我们 {v['our_latest_quarter']}）"
            ny = (v.get("pages") or {}).get("next_year") or {}
            if "quarters" in ny:
                if ny.get("year_header") is None:      # 未发布空视图没有表头（v0.45.576）
                    cur += "；次年页：BEA 尚未挂出次年日程"
                else:
                    cur += (f"；次年页 Year {ny['year_header']}："
                            + ("、".join(ny["quarters"]) if ny["quarters"] else "BEA 尚未列出任何发布"))
        else:
            cur = f"上游最新 {v['upstream_latest']}（我们已核对到 {v['our_verified_through']}）"
        if v["new_available"]:
            items = "、".join(v["new_items"][:6])
            more = f" 等 {len(v['new_items'])} 条" if len(v["new_items"]) > 6 else ""
            out.append(f"  🆕 {key:5s} {cur}")
            out.append(f"        可抄：{items}{more}")
            out.append(f"        源：{v['source']}")
        else:
            out.append(f"  ✅ {key:5s} {cur}")
    out.append("")
    if res["action_required"]:
        out.append("→ 上游已发布新日程。请**人工**抄进 economic_calendar.py 并同步上移")
        out.append("  该表的 verified_through；禁止让脚本代抄，也禁止按规律推算。")
    elif res["undeterminable_tables"]:
        out.append("→ 有源无法判定（多半是页面改版或网络）。这不是「没有新日程」，")
        out.append("  需要人去看一眼源站，必要时修解析器。")
    elif h["status"] != "ok":
        out.append("→ 本地日历将见底，但上游确实还没发布新日程。只能等，别改阈值。")
    return "\n".join(out)


def _exit_code(res: Dict[str, Any]) -> int:
    """退出码优先级 action > health > undetermined（v0.45.355 从 `main()` 原样抽出——
    外壳 `status` 与退出码必须出自同一处判定）。"""
    if res["action_required"]:
        return 1
    if res["calendar_health"]["status"] != "ok":
        return 1
    if res["undeterminable_tables"]:
        return 3
    return 0


def _coverage_end(per_table: Dict[str, Any], key: str) -> Optional[str]:
    """该表覆盖到的最后一天（`last_date`，缺则 `verified_through`）——过了这天该类事件就从日历里消失。"""
    pt = per_table.get(key) or {}
    return pt.get("last_date") or pt.get("verified_through")


def contract_attention(res: Dict[str, Any], health: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把结果**显式**翻成 `step_contract` 的 attention 条目（v0.45.355）。

    `health` 是 `economic_calendar.get_calendar_health(ref_date=<同一天>)` 的完整返回——
    JSON 里的 `calendar_health` 只留了 binding/shortest 两张表，逐表的截止日要从这里取
    （不往 `--out` 的原有键里加东西，键集合保持不变）。

    · 上游新日程（每表一条 warn）：deadline = 本地该表覆盖到的最后一天——得在那之前抄完。
    · 本地表低于地平线阈值（每表一条）：stale ⇒ warn；exhausted（表已用尽，该类事件此刻就不在
      日历里）⇒ alarm。deadline 同上（exhausted 的截止日已过，照写，表示「已经断供」）。
    · 上游有源无法判定 ⇒ warn（「看不出来」≠「没有新的」，见模块 docstring）。
    末尾兜底：退出码是 1/3 却一条 warn/alarm 都没有 ⇒ 补一条，不让「要人看」变成空列表。
    """
    A = step_contract.attention_item
    per_table = health.get("per_table") or {}
    items: List[Dict[str, Any]] = []
    for k in res.get("new_schedule_tables") or []:
        u = (res.get("upstream") or {}).get(k) or {}
        new = u.get("new_items") or []
        shown = "、".join(new[:6]) + (f" 等 {len(new)} 条" if len(new) > 6 else "")
        items.append(A(
            f"{_TOOL}.new_schedule.{k}", "warn",
            f"上游已发布 {k} 新日程 {len(new)} 条（{shown}）—— 待人工抄进 economic_calendar.py 并同步上移"
            f" verified_through（只抄官方已发布日程，禁止按规律推算）；源 {u.get('source', '')}",
            deadline=_coverage_end(per_table, k)))
    for k in health.get("exhausted_tables") or []:
        pt = per_table.get(k) or {}
        items.append(A(
            f"{_TOOL}.horizon.{k}", "alarm",
            f"本地 {k} 表已用尽：最后一条 {pt.get('last_date')}，此后该类宏观事件已不在日历里",
            deadline=_coverage_end(per_table, k)))
    for k in health.get("stale_tables") or []:
        pt = per_table.get(k) or {}
        items.append(A(
            f"{_TOOL}.horizon.{k}", "warn",
            f"本地 {k} 表只到 {pt.get('last_date')}（剩 {pt.get('horizon_days')} 天，"
            f"低于该表阈值 {pt.get('min_horizon_days')} 天）",
            deadline=_coverage_end(per_table, k)))
    undet = res.get("undeterminable_tables") or []
    if undet:
        why = "；".join(f"{k}: {((res.get('upstream') or {}).get(k) or {}).get('reason')}" for k in undet)
        items.append(A(
            f"{_TOOL}.upstream_undetermined", "warn",
            f"上游 {len(undet)} 个源无法判定（{why}）—— 这不等于「没有新日程」，需人工看一眼源站"))
    if _exit_code(res) != 0 and not any(a["level"] != "info" for a in items):
        h = res.get("calendar_health") or {}
        items.append(A(f"{_TOOL}.needs_review", "warn",
                       f"本地日历 {h.get('status')}：{h.get('binding_table')} 表只到 "
                       f"{h.get('binding_last_date')}（判据未归类）"))
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="上游宏观日程发布监视器")
    ap.add_argument("--force", action="store_true", help="忽略周节流，强制联网检查")
    ap.add_argument("--max-age-days", type=int, default=_DEFAULT_MAX_AGE_DAYS,
                    help=f"节流窗口天数（默认 {_DEFAULT_MAX_AGE_DAYS}）")
    ap.add_argument("--timeout", type=int, default=20, help="单个源抓取超时秒数")
    # v0.45.160：argparse 的 default 在 import 期求值，不能塞冻结值
    ap.add_argument("--state", default=None, help="节流状态文件路径（默认 PATHS.cache_dir 下）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论行")
    ap.add_argument("--out", default=None, help="把完整结果写成 JSON")
    # v0.45.355：业务日期。外壳 `date`、本地体检、节流窗口都按这一天算——三者同源，
    # 消费方拿 `date` 比新鲜度时比的就是这次判定真正用的那一天。缺省 = 洛杉矶当日（business_today）；
    # 此前是 `date.today()`（本机 America/Vancouver）：两地目前同日，但 2026-11-01 起温哥华常年 UTC-7，
    # 冬令时每天本机 00:00–01:00 两者差一天，那一小时里缺省值从此按洛杉矶日——也就**不等于**编排器的
    # DATE_STR（本机日），要比新鲜度就显式传 --today（见 step_contract.business_today）。也供测试 / 回放钉日期。
    ap.add_argument("--today", default=None, help="业务日期 YYYY-MM-DD（默认洛杉矶当日）")
    args = ap.parse_args()

    # 非法 --today 在这里抛 ⇒ run_tool 记为崩溃（退出码 3）
    today = date.fromisoformat(args.today or step_contract.business_today())

    res = check(force=args.force, max_age_days=args.max_age_days,
                state_path=Path(args.state) if args.state else None,
                timeout=args.timeout, today=today)
    rc = _exit_code(res)

    if args.out:
        from economic_calendar import get_calendar_health
        env = step_contract.envelope(
            _TOOL, today.isoformat(), _STATUS_BY_RC[rc], payload=res,
            attention=contract_attention(res, get_calendar_health(ref_date=today)))
        try:
            # 建父目录是本工具改造前（v0.45.67 起）就有的行为，照旧保留在这里；`write_out` 本身不建目录
            # （崩溃路径 run_tool 因此不会建目录——它只打 stderr、仍退出码 3）。
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            step_contract.write_out(args.out, env)
        except OSError as e:
            print(f"⚠️  JSON 写入失败: {e}", file=sys.stderr)

    if not args.quiet:
        print(_render(res))

    h = res["calendar_health"]
    undet = res["undeterminable_tables"]
    # v0.45.68 二次检查：旧版在 health 分支无条件打「上游暂无新日程」。
    # 但退出码优先级是 action > health > undetermined，于是「本地将见底 + 四个源全抓不到」
    # 这一格会打出一句**毫无依据**的断言，且把抓取失败整个吃掉——
    # 恰好抵消掉这个监视器存在的意义。
    _up_note = ("上游暂无新日程" if res["upstream_conclusive"]
                else f"⚠️ 上游 {len(undet)} 个源无法判定（{'、'.join(undet)}），"
                     f"本次**无法确认**上游有没有新日程")
    # 分支顺序与 `_exit_code` 一致（action > health > undetermined）；返回值统一用 rc
    if res["action_required"]:
        if args.quiet:
            print(f"🆕 上游已发布新日程：{'、'.join(res['new_schedule_tables'])} —— 待人工抄录")
        return rc
    if h["status"] != "ok":
        if args.quiet:
            print(f"⚠️  本地日历 {h['status']}：{h['binding_table']} 只到 "
                  f"{h['binding_last_date']}（剩 {h['binding_horizon_days']} 天），{_up_note}")
        return rc
    if res["undeterminable_tables"]:
        if args.quiet:
            print(f"❓ 无法判定：{'、'.join(res['undeterminable_tables'])}")
        return rc
    if args.quiet:
        print("✅ 日历健康，上游无新日程")
    return rc


if __name__ == "__main__":
    if step_contract is None:
        # 没有 step_contract 就写不出外壳：只打 stderr，退出码 3（「无法判定」），不写 --out
        print(f"{_TOOL}: 无法导入 step_contract（{_STEP_CONTRACT_IMPORT_ERROR}）—— 退出码 3，按「无法判定」处理；"
              "本次不写 --out", file=sys.stderr)
        sys.exit(3)
    # v0.45.355：未捕获异常 ⇒ 退出码 3 + error 外壳（不再是 Python 默认的 1 = 「要人动手」）
    sys.exit(step_contract.run_tool(_TOOL, main))
