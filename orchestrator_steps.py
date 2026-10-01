#!/usr/bin/env python3
"""编排器步骤解释器（v0.45.355）：「退出码 + `--out` JSON → 日志级别 / 一行日志 / STEPS_RESULT 片段」收进仓库。

为什么要有这个文件（2026-09-27 根因调查）
----------------------------------------
编排器（仓库 `scripts/alpha-hive-orchestrator.sh`，v0.45.353 起受版本控制、是唯一真相；launchd 跑的部署副本
`~/.claude/scripts/alpha-hive-orchestrator.sh` 与它逐字一致，由 `tests/test_orchestrator_deployed_matches_repo.py` 管）
在 Step 10/11/12/13/15 各用一段内联 `python3 -c` 读工具的 `--out` JSON、按退出码分支拼日志与 `STEPS_RESULT`。
这些格式知识在编排器里、没有测试，与生产方（仓内 CLI）之间的接口只在散文里各抄一份，于是：
  · Python 未捕获异常的退出码是 1，与这些工具「未就绪 / 降级（正常）」同码 ⇒ 工具崩了被记成正常
    （实测：`ic_rerun_readiness --db <坏库>` rc=1、不写 `--out`，编排器记 `accruing`）；
  · JSON 没有日期 ⇒ 同一 DATE_STR 重跑、这一步超时，编排器照读上一次留下的文件，毫无察觉；
  · 「要人看」靠段首图标推断 ⇒ H1 锚点「须早于 2026-10-12」藏在 ⏳ 段里永远不报；
  · Step 2（`alpha_hive_daily_report.py --swarm`）自 v0.45.145 起在 ML 概率退化成常数时 exit 1，
    而编排器把 0/124/2 以外一律记 failed ⇒ 09-24/25 日报与部署都做完了，日志照打
    「Step 2 没跑完……本轮网站不会更新」。

本模块把这些格式知识收成一个**有测试**的解释器，供下一步（B）接进编排器，让 bash 不再持有任何格式知识。
B（v0.45.385）起编排器经 `_apply_step_interp` 调用（本版接了 Step 10/11/12/13/15；Step 2/4 随 B2 接）；
解释器不可用时 `_step_rc_fallback` 退回按退出码记（Step 2/4 逐字复现 B 之前的 status，10–15 记 interpreter_unavailable），
片段带 `interp_fallback`、日志多一行 ERROR。

用法
----
    /usr/local/bin/python3 orchestrator_steps.py --step 11 --rc "$STEP11_RC" \\
        --json "$READINESS_JSON" --date "$DATE_STR" --run-start "$STEP11_START"

stdout **恰好一行** JSON、退出码**恒为 0**（任何内部错误都渲染成 `render_error`，绝不抛）：

    {"level": "info"|"warn"|"error", "message": "<一行日志文本>",
     "steps_fragment": {"<step key>": {...}}}

输出用 `ensure_ascii`（纯 ASCII）：launchd 下 stdout 编码是什么都炸不了；bash 侧 `jq -r .message`
会把 `\\uXXXX` 还原成中文。连这一行都解析不了时，bash 应退回只按 rc 记（B 的事）。

参数
----
  --step        2 | 4 | 10 | 11 | 12 | 13 | 15（也接受完整键名，如 `step11_ic_rerun_readiness`）
  --rc          该步 `run_step` 的返回码（`4` 传 **Step 2** 的 rc：Step 4 本身不跑任何东西）
  --json        该步 `--out` 写出的 JSON（10–15）
  --date        本轮业务日期 DATE_STR（新鲜度与 Step 2 产物核对都按它）
  --run-start   可选，该步开始时刻的 epoch 秒（`$(date +%s)`）。**B 必须传**：同一 DATE_STR 重跑时
                上一次留下的 JSON 日期同样对得上，只有「写于本步开始之前」分得出来；跨午夜的判定（见下）
                也靠它才硬。（更省事的兜底：B 在跑工具前先 `rm -f` 该 JSON，两道一起上。）
  --duration    可选，bash 算的耗时秒数。片段里 `duration_seconds` 与现行内联**逐字一致**要靠它；不传就不带这个键
  --timeout-seconds  可选，超时文案里的秒数（Step 2 用 STEP2_TIMEOUT；10/15 默认沿用现行常数）
  --data-dir    Step 2 / 4：`.swarm_results_<date>.json` 与 `logs/scan_timing.json` 所在（默认 `$ALPHA_HIVE_HOME`）
  --report-dir  Step 2 / 4：日报 `alpha-hive-daily-<date>.{json,md}` 与 `analysis-*-ml-<date>.json` 所在
                （默认同 `--data-dir`；生产里二者都是 DATA_DIR = `PATHS.home`）

B 接线须知：日期必须**显式**传给生产方
--------------------------------------
编排器只在开头算一次 `DATE_STR=$(date +"%Y-%m-%d")`（本机时区），而五个工具不给日期参数时，外壳 `date`
取的是**工具自己运行那一刻**的 `step_contract.business_today()`（America/Los_Angeles）。两者会在两种情况下错开：
  ① **跨午夜**：2026-09-08 那轮 Step 10–13 实际在 09-09 00:27 才跑（`~/.claude/logs/scan_coverage-2026-09-08.json`
     里 `"date": "2026-09-09"`）。外壳改造后这种文件会被判 `stale_json`——本解释器为此开了一个窄口子
     `date_rollover`（见 (a)），但那只是兜底；
  ② **2026-11-01 起 America/Vancouver 常年 UTC-7**（不再回拨；本机时区就是它），而 America/Los_Angeles
     每年冬令时（11 月第一个周日至次年 3 月第二个周日）回到 UTC-8 ⇒ 这段时间里每天本地 00:00–01:00，
     DATE_STR（温哥华日期）比洛杉矶业务日**早一天**，工具写出的外壳日期是 DATE_STR 的**前一天**——
     与「上一轮留下的文件」无从区分，**不**放行，只会是 `stale_json`。
所以 B **必须**把 DATE_STR 原样传给每个工具（参数名各不相同：`--end` / `--today` / `--date`，
唯一真相是下方 `_TOOL_STEPS` 各条的 `date_flag`，测试核对它在生产方的 argparse 里真实存在），
让外壳日期由编排器决定、与 `--date` 恒等；不要依赖工具按时钟取日期。（B 起编排器已照做：各步 `run_step` 行
带 `date_flag "${DATE_STR}"`，由 `tests/test_orchestrator_step_interp.py` 的结构守卫按 `_TOOL_STEPS` 核对。）

与现行内联的关系
----------------
对照对象是 B 之前的冻结副本 `tests/fixtures/orchestrator_pre_b.sh.frozen` 里各步的**分支链**
（B 起仓库编排器里的这些内联已删、换成 `_apply_step_interp` 调用；按 shell 变量名定位，不记行号：
`STEP10_RC` / `STEP11_RC` / `STEP12_RC` / `STEP13_RC` / `STEP15_RC` / `STEP2_RC` 的 `if … elif … fi`，
以及 `CONT_SUMMARY` / `READINESS_LINE` / `BOUNDARY_JSON` / `_S11_NEW` / `COV_SUMMARY` / `CALWATCH_SUMMARY` /
`BACKUP_CONT_SUMMARY` 这几段内联）。对**旧格式 JSON（无 `schema_version`）**，各步的 `status` / 其余键 /
日志级别与 B 之前的 bash **逐字相同**，只多一个 `"contract": "legacy"`——`tests/test_orchestrator_steps.py::TestGoldenLegacy`
把这些分支链按模式从冻结副本里抽出来、在 bash 里配同一批夹具**真跑**对照（摘要钉在
`tests/test_orchestrator_step_interp.py`，副本永远不许从 B 之后的编排器重新生成）。以下是**刻意的改动**，每条都有测试：

  (a) 新鲜度：外壳 `date` ≠ `--date`，或（给了 `--run-start` 时）`generated_at` 早于本步开始；
      旧格式则看 mtime（早于 `--run-start`，或 mtime 的本地日期早于 `--date`）⇒ 该 JSON「不是本轮的」。
      **唯一例外 `date_rollover`**：外壳 `date` 恰是 `--date` 的**次日**，且有本轮写出的证据——给了
      `--run-start` 时 `generated_at` 不早于它；没给时退一步，要 `generated_at` 的日期 = 外壳 `date` 且
      文件 mtime 不早于 `--date` 当天本地 0 点 ⇒ 按本轮结果采用，片段记 `"date_rollover": true`、message 说明。
      其余一切日期不符（前一天、隔两天、次日但写于本步之前）照旧判「不是本轮的」。
      rc=2（脚本不存在）时根本不读 JSON：那条路径上的任何 JSON 都不可能是本轮写的
      （现行 bash 在这里仍会把遗留文件的边界核对抄进片段）。
  (a') JSON 问题（不是本轮的 / 契约不符 / 缺失 / 不可解析）**只在现行 bash 真的读这份 JSON 的那几支**
      覆盖判定：Step 10/12/13/15 只有 rc=1 那一支读（摘要内联 python 在 `elif … -eq 1` 里）；Step 11 的
      `READINESS_LINE` / `BOUNDARY_JSON` 在分支链之前无条件求值 ⇒ rc≠2 的每一支都读（各步的读取集合见
      `_ToolStep.json_rcs`，测试从仓库副本核对）。覆盖时 `status` 改成问题状态（`stale_json` /
      `contract_error` / `missing_json` / `unparsable_json`），原判定进 `rc_status`，内容**一个字都不用**
      （Step 11 的 `cohort_boundary` 记 null）。**不读的那几支**（例：Step 10 rc=124 遇到坏 JSON、rc=0 遇到
      上一轮的文件）保留按退出码的判定，问题只记进追加键 `json_problem`（`{"status": <问题状态>, …细节}`）。
  (b) 契约：带 `schema_version` 却过不了 `step_contract.validate()`（含 `tool` 不是本步那个工具），
      或 JSON 顶层不是对象 ⇒ `contract_error` + `problems`，级别 error（覆盖与否按 (a')）。
  (c) 崩溃：外壳 `status: "error"`（`step_contract.run_tool` 兜住的未捕获异常，rc=3）⇒ `status: "error"`，
      级别 error —— 不再落进「3 = 无法判定」那一支的 `undetermined`。这是工具的问题、不是 JSON 的问题，不受 (a') 限制。
  (d) attention（只读生产方**显式**列出的条目，不从图标反推），片段里原样放 `attention`、外壳 `status` 放
      `envelope_status`：外壳 `status: "attention"`（生产方说「要人看」）⇒ **全部**条目压缩进 message
      （含 info：「已就绪 / 已到检视点」正是要人去做的事），按严重度排序、总长封顶，超出记「…(+N)」；
      其余外壳状态只报 warn/alarm 或带 deadline 的条目。级别：warn ⇒ 至少 warn；deadline 已过 ⇒ 至少 warn；
      **alarm ⇒ warn**，message 里带 🚨 —— 与现行 bash 对同类事件的处理一致（Step 11 世代边界告警、Step 12
      来源标签矛盾都刻意打 WARN，「不计入 OVERALL_STATUS」）。**error 只留给**：工具崩溃（(c)）、契约不符（(b)）、
      bash 会读却缺失 / 不可解析 / 不是本轮的且 rc=1（(f)）、`render_error`，以及现行 bash 自己就打 ERROR 的分支（超时等）。
      Step 11 的边界告警若已作为 attention 条目列出，基础段只留一句指针，不再把原文与处理方式重复一遍。
  (e) 旧格式照旧接受，片段标 `"contract": "legacy"`；新格式标 `"contract": "v1"`。
  (f) 退出码 0/1（工具声称已跑完——五个工具在这两条路径上都**先写 `--out` 再返回**）却没有 JSON
      ⇒ `missing_json`；JSON 存在但读不了 ⇒ `unparsable_json`（覆盖与否按 (a')）。rc=1 且该支读 JSON 时级别 error：
      旧代码的未捕获异常退出码也是 1，「崩了」与「降级 / 未就绪」在这里分不开，不能按正常记（「不是本轮的」同理：
      本轮那份等于缺失）。rc=3/124/其他 时 JSON 缺失是预期的（早退、被杀），照旧。
  (g) Step 2 rc=1：扫描主流程确实跑完了（当日 `.swarm_results`、日报 JSON+MD 都在，且
      `logs/scan_timing.json` 是**本轮**写的（须给 `--run-start`）、日期对、不是早退）⇒ `status: "success_with_warning"`，
      级别 warn。理由见 `_step2_rc1_evidence`。Step 4 同判据：此时仪表板已生成，记 `skipped_builtin`
      （附 `step2_status`），不再打「Step 2 没跑完」ERROR。
  (h) 读文件的硬化：只读**普通文件**（FIFO / 字符设备 / 目录一律不打开读，绝不阻塞）、`--out` JSON 大小封顶
      `MAX_JSON_BYTES`、嵌套深度封顶 `MAX_JSON_DEPTH`（真实产出只有个位数层；过深的合法 JSON 会让本模块的递归
      遍历与 `json.dumps` 撞上 RecursionError）——超限一律按「不可解析」记，原因写进片段，从不崩。
  (i) Step 11 超时与就绪度 JSON 不可用时的**文案**（v0.45.391 复审 S5；`status` / `rc` / `cohort_boundary` / 级别
      与 B 之前逐字相同，只换 message）：rc=124 说「⏰ 超时（>60s，run_step 看门狗杀掉…）」而不是「⚠️ 异常（exit=124）」；
      JSON 本轮不可用（rc≠2）时边界那句说「本轮未核对——JSON 没写出来 / 不可用（原因）」，不再推给「代码早于 v0.45.334」
      （那只在读到 JSON 却缺键时成立；09-30 的 rc=124 被它说成了版本问题）。秒数默认 `STEP11_TIMEOUT_DEFAULT`，
      与仓库编排器 Step 11 的 `run_step --timeout` 有测试核对。冻结副本对照里 rc=124 那一行的文案按此替换后比对。

**不归本解释器的：Step 5（gh-pages 部署）。** 自 v0.45.351 起由编排器的 `_step5_gh_pages_verdict` 调
`report_deployer.py --gh-pages-step-status --since $STEP2_START`，按部署日志的**实际结局**判——那是比 Step 2
退出码更直接的证据，判据与测试都在它自己那里，这里不再复制一份（复制就是第二份真相）。它的降级支
（helper 无有效输出 ⇒ 退回按 `STEP2_RC` 判）仍把 rc=1 记成「本轮网站不会更新」；B 接线时让那一支改用
`--step 2` 的 `status`（`success_with_warning` ⇒ 与 rc=0 同一支），不要在 bash 里再写一遍判据。

片段里新增的键（`contract` / `rc_status` / `attention` / `envelope_status` / `json_problem` / `date_rollover` /
`problems` / `stale` / `error` / `warning` / `ml_model_guard` / `git_push_success` / `rc1_unverified` / `step2_status`）
都是**追加**，现行键一个没删、没改名。

守卫：`tests/test_orchestrator_steps.py`（表驱动 + 旧格式对照仓库副本真跑 + 子进程恒一行 + render_error）。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

#: 输出的日志级别（bash 侧映射成 log "INFO"/"WARN"/"ERROR"）
LEVELS = ("info", "warn", "error")
_RANK = {"info": 0, "warn": 1, "error": 2}

#: 一行 message 里各段的分隔符（原先是几行 log，现在是一行里的几段）
SEP = " ｜ "

#: 片段里的问题状态（覆盖原判定时，原判定进 `rc_status`；不覆盖时进 `json_problem.status`）
STALE, CONTRACT_ERROR, MISSING, UNPARSABLE, RENDER_ERROR = (
    "stale_json", "contract_error", "missing_json", "unparsable_json", "render_error")

#: 退出码 0/1 = 工具声称已跑完且写了 `--out`（逐个核对过：scan_continuity / ic_rerun_readiness /
#: scan_coverage_gate / economic_calendar_watch / backup_continuity 在这两条路径上都先写盘再 return）
_RC_CLAIMS_WRITTEN = (0, 1)

#: (h) `--out` JSON 的大小上限。真实产出是 KB 级；上限只为「读到一个不该读的东西时不把内存吃光」
MAX_JSON_BYTES = 5 * 1024 * 1024
#: (h) Step 2 核对 `.swarm_results_<date>.json` 用的上限：它是真实的大文件（实测 0.5–1.8 MB，随标的数涨），
#: 只核「是非空对象」，内容不进片段——上限放宽，免得正常的大文件被误判成「没跑完」
MAX_SWARM_BYTES = 256 * 1024 * 1024
#: (h) 嵌套深度上限。真实产出实测 ≤8 层；本模块的遍历（`_jq_roundtrip` / `_sanitize`）与 `json.dumps` 都是递归的，
#: 实测 500 层的合法 JSON 就让它们撞上 RecursionError（此前落成 render_error，说不出是 JSON 的问题）
MAX_JSON_DEPTH = 64

#: (d) 「需人看」段：单条原文截断长度与整段总长上限（超出的条目按严重度从低往高省略，记「…(+N)」）
_ITEM_CHARS = 200
_ATTENTION_CHARS = 900


def _max_level(*levels: str) -> str:
    return max(levels, key=lambda lv: _RANK[lv])


def _one_line(s: Any, limit: Optional[int] = None) -> str:
    """换行及其两侧空白压成一个空格；行内的连续空格原样保留（「⏭️  Step」照旧是两个空格）。"""
    t = re.sub(r"\s*[\r\n]+\s*", " ", str(s)).strip() if s is not None else ""
    if limit and len(t) > limit:
        t = t[: limit - 1] + "…"
    return t


def _repo_import(name: str):
    """从**代码**所在目录 import（step_contract / ml_model_guard 与本文件同址，`__file__` 是正确锚点）。"""
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module(name)


def _iso_local(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, OverflowError):
        return None


def _parse_iso_epoch(s: Any) -> Optional[float]:
    """ISO 时间 → epoch 秒；无时区的按本机时区（与 bash `date` 同口径）。解析不了回 None。"""
    d = _parse_iso(s)
    if d is None:
        return None
    try:
        return d.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _day_after(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()


# ═══════════════════════════════════════════ 运行上下文 ═══════════════════════════════════════════

class _Ctx:
    def __init__(self, *, rc: int, date: str, json_path: Optional[str] = None,
                 run_start: Optional[float] = None, duration: Optional[int] = None,
                 timeout: Optional[int] = None, data_dir: Optional[str] = None,
                 report_dir: Optional[str] = None):
        self.rc, self.date, self.json_path = rc, date, json_path
        self.run_start, self.duration, self.timeout = run_start, duration, timeout
        self.data_dir, self.report_dir = data_dir, report_dir

    def dur(self) -> Dict[str, int]:
        """bash 的 `"duration_seconds": $STEPn_DURATION`——没传 `--duration` 就不带（不编一个 0 出来）。"""
        return {} if self.duration is None else {"duration_seconds": self.duration}

    @property
    def dur_text(self) -> str:
        return "?" if self.duration is None else str(self.duration)

    def timeout_text(self, default: Optional[int]) -> str:
        t = self.timeout if self.timeout is not None else default
        return "?" if t is None else str(t)


# ═══════════════════════════════════════════ 读 JSON ═══════════════════════════════════════════

class _JsonProblem(Exception):
    """读得到文件、但它不是一份能安全使用的 JSON（非普通文件 / 过大 / 过深 / 解析不了）。"""


def _file_kind(mode: int) -> str:
    for test, name in ((stat.S_ISFIFO, "FIFO"), (stat.S_ISCHR, "字符设备"), (stat.S_ISBLK, "块设备"),
                       (stat.S_ISDIR, "目录"), (stat.S_ISSOCK, "socket")):
        if test(mode):
            return name
    return f"mode={oct(mode)}"


def _json_depth_exceeds(obj: Any, limit: int) -> bool:
    """迭代（不递归）地看嵌套是否超过 `limit` 层——检查本身不能撞上它要防的 RecursionError。"""
    stack: List[Tuple[Any, int]] = [(obj, 1)]
    while stack:
        x, d = stack.pop()
        if isinstance(x, (dict, list)):
            if d > limit:
                return True
            stack.extend((v, d + 1) for v in (x.values() if isinstance(x, dict) else x))
    return False


def _read_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES,
               max_depth: Optional[int] = MAX_JSON_DEPTH) -> Tuple[Any, float]:
    """(h) 安全读一份 JSON → (对象, mtime)。不存在抛 FileNotFoundError；其余问题抛 `_JsonProblem`/`OSError`。

    · 先 `O_NONBLOCK` 打开再对**这个 fd** 做 `fstat`：FIFO 没有写端时 `open` 本身就会一直等，先 stat 再 open
      也挡不住两步之间被换掉——对 fd 判才可靠；不是普通文件一个字节都不读（`/dev/zero` 会读到内存耗尽）。
    · 边读边数，超过 `max_bytes` 立刻停（文件在读的过程中变大也挡得住）。
    · 深度超限按问题记（见 `MAX_JSON_DEPTH`）；解析器自己撞上递归上限同样按问题记。
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    fd = os.open(str(path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _JsonProblem(f"不是普通文件（{_file_kind(st.st_mode)}），未读取")
        if st.st_size > max_bytes:
            raise _JsonProblem(f"文件过大（{st.st_size} 字节 > 上限 {max_bytes}），未读取")
        chunks: List[bytes] = []
        total = 0
        while True:
            b = os.read(fd, min(1 << 20, max_bytes + 1 - total))
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > max_bytes:
                raise _JsonProblem(f"文件过大（读到 >{max_bytes} 字节），未读完")
        mtime = st.st_mtime
    finally:
        os.close(fd)
    try:
        obj = json.loads(b"".join(chunks).decode("utf-8"))
    except RecursionError:
        raise _JsonProblem("JSON 嵌套过深，解析器撞上递归上限") from None
    except (UnicodeDecodeError, ValueError) as e:     # JSONDecodeError 是 ValueError 的子类
        raise _JsonProblem(f"{type(e).__name__}: {_one_line(e, 200)}") from None
    if max_depth is not None and _json_depth_exceeds(obj, max_depth):
        raise _JsonProblem(f"JSON 嵌套超过 {max_depth} 层（真实产出只有个位数层），按不可解析处理")
    return obj, mtime


class _Doc:
    """一份 `--out` JSON 的解读结果。

    state: missing | unparsable | contract_error | legacy | v1 | stale | crashed | not_read
    只有 legacy / v1 的 `data` 可以拿来用；stale / crashed 的 `data` 只用于写说明，不参与判定。
    `info` 里的 `date_rollover`（v1 / crashed）说明「外壳日期是次日、但确是本轮写的」（(a)）。
    """
    __slots__ = ("state", "data", "contract", "info")

    def __init__(self, state: str, data: Optional[dict] = None, contract: Optional[str] = None,
                 info: Optional[dict] = None):
        self.state, self.data, self.contract, self.info = state, data, contract, dict(info or {})

    @property
    def usable(self) -> Optional[dict]:
        return self.data if self.state in ("legacy", "v1") else None


def _legacy_staleness(mtime: float, ctx: _Ctx) -> Optional[dict]:
    """旧格式没有日期，只能看 mtime（用 `--run-start` 最准；没有就退到「mtime 的本地日期早于 --date」）。"""
    if ctx.run_start is not None and mtime < math.floor(ctx.run_start):
        return {"reason": "written_before_run", "written_at": _iso_local(mtime),
                "run_start": _iso_local(ctx.run_start)}
    if datetime.fromtimestamp(mtime).date().isoformat() < ctx.date:
        return {"reason": "mtime_before_date", "written_at": _iso_local(mtime), "expected_date": ctx.date}
    return None


def _rollover_evidence(env: dict, mtime: float, ctx: _Ctx) -> Optional[dict]:
    """(a) 外壳日期恰是 `--date` 的次日时，有没有「这是本轮写的」证据。没有 ⇒ None（照旧判不是本轮的）。

    生产方不给日期参数时按**自己运行那一刻**取日期；本轮跑过午夜，外壳就是 DATE_STR 的次日
    （2026-09-08 实况，见模块 docstring「B 接线须知」）。只开这一个方向：前一天的外壳与上一轮留下的文件分不开。
    证据要求 `generated_at` 解析得了（这里不像同日分支那样退回 mtime——例外口子只给证据清楚的）：
      · 给了 `--run-start`：`generated_at` 不早于本步开始（整秒比，同 `_envelope_staleness`）；
      · 没给：`generated_at` 的日期 = 外壳 `date`，且文件 mtime 不早于 `--date` 当天本地 0 点（较弱：
        分不出同一 DATE_STR 上一轮也跨了午夜的情形——所以 B 必须传 `--run-start`）。
    """
    if env["date"] != _day_after(ctx.date):
        return None
    gen = _parse_iso(env.get("generated_at"))
    if gen is None:
        return None
    ev = {"json_date": env["date"], "expected_date": ctx.date, "written_at": env.get("generated_at")}
    if ctx.run_start is not None:
        ts = _parse_iso_epoch(env.get("generated_at"))
        if ts is None or math.floor(ts) < math.floor(ctx.run_start):
            return None
        return dict(ev, basis="generated_at>=run_start", run_start=_iso_local(ctx.run_start))
    day_start = datetime.strptime(ctx.date, "%Y-%m-%d").timestamp()
    if gen.date().isoformat() != env["date"] or mtime < day_start:
        return None
    return dict(ev, basis="generated_at_date+mtime", mtime=_iso_local(mtime))


def _envelope_staleness(env: dict, mtime: float, ctx: _Ctx) -> Tuple[Optional[dict], Optional[dict]]:
    """(不是本轮的说明, date_rollover 说明)——至多一个非 None。"""
    if env["date"] != ctx.date:
        roll = _rollover_evidence(env, mtime, ctx)
        if roll is not None:
            return None, roll
        return {"reason": "date_mismatch", "json_date": env["date"], "expected_date": ctx.date,
                "written_at": env.get("generated_at")}, None
    if ctx.run_start is not None:
        ts = _parse_iso_epoch(env.get("generated_at"))
        if ts is None:          # generated_at 解析不了才退回 mtime
            ts = mtime
        # generated_at 截到秒 ⇒ 两边都取整秒比，本轮在同一秒里写出的不会被误判
        if math.floor(ts) < math.floor(ctx.run_start):
            return {"reason": "written_before_run", "json_date": env["date"],
                    "written_at": env.get("generated_at") or _iso_local(mtime),
                    "run_start": _iso_local(ctx.run_start)}, None
    return None, None


def _contract_problems(env: dict, tool: str) -> List[str]:
    try:
        sc = _repo_import("step_contract")
    except Exception as e:  # noqa: BLE001 —— 核对不了就说核对不了，不假装合规
        return [f"step_contract 不可导入（{type(e).__name__}: {e}），无法核对契约"]
    problems = list(sc.validate(env))
    if not problems and env.get("tool") != tool:
        problems.append(f"tool={env.get('tool')!r}，本步期望 {tool!r}（--json 指错了文件？）")
    return problems


def _load_doc(path: Optional[str], ctx: _Ctx, tool: str) -> _Doc:
    if not path:
        return _Doc("missing", info={"error": "未提供 --json"})
    p = Path(path)
    try:
        obj, mtime = _read_json(p)
    except FileNotFoundError:
        return _Doc("missing", info={"error": f"{p.name} 不存在"})
    except _JsonProblem as e:
        return _Doc("unparsable", info={"error": str(e)})
    except Exception as e:  # noqa: BLE001 —— 读不了（权限 / socket / I/O）都归「不可解析」，原因带出去
        return _Doc("unparsable", info={"error": f"{type(e).__name__}: {_one_line(e, 200)}"})
    if not isinstance(obj, dict):
        return _Doc("contract_error", info={"problems": [f"JSON 顶层必须是对象，收到 {type(obj).__name__}"]})
    if "schema_version" not in obj:
        stale = _legacy_staleness(mtime, ctx)
        return _Doc("stale", obj, "legacy", stale) if stale else _Doc("legacy", obj, "legacy")
    problems = _contract_problems(obj, tool)
    if problems:
        return _Doc("contract_error", info={"problems": problems})
    stale, roll = _envelope_staleness(obj, mtime, ctx)
    if stale:     # 先判新鲜度再判崩溃：上一次崩溃留下的外壳不能冒充本轮崩溃
        return _Doc("stale", obj, "v1", stale)
    info = {"date_rollover": roll} if roll else None
    if obj["status"] == "error":
        return _Doc("crashed", obj, "v1", info)
    return _Doc("v1", obj, "v1", info)


def _cap_join(texts: List[str], limit: int, sep: str = "；") -> str:
    """按顺序拼，总长超过 `limit` 就停，剩下的记「…(+N)」。第一条恒保留（它本身已截断到 `_ITEM_CHARS`）。"""
    out: List[str] = []
    used = 0
    for i, t in enumerate(texts):
        add = len(t) + (len(sep) if out else 0)
        if out and used + add > limit:
            return sep.join(out) + f"…(+{len(texts) - i})"
        out.append(t)
        used += add
    return sep.join(out)


def _attention(doc: _Doc, ctx: _Ctx) -> Tuple[List[dict], str, str]:
    """(要报的条目, 压缩文本, 级别下限)，见模块 docstring (d)。

    外壳 `status: "attention"` ⇒ 生产方说「要人看」⇒ **全部**条目都报（`ic_rerun.*.checkpoint` / `.ready`
    这类 info 条目就是要人去跑、去判断的事，此前被「info 不刷屏」过滤得一条不剩）；其余状态只报
    warn/alarm 或带 deadline 的。message 里按严重度排（alarm > warn > 已过期 > 带截止 > 其余），
    总长封顶，省略的是排在最后的；片段 `attention` 保留生产方原顺序。
    """
    data = doc.data or {}
    raw = data.get("attention")
    items = [a for a in raw if isinstance(a, dict)] if isinstance(raw, list) else []
    report_all = data.get("status") == "attention"
    if report_all and not items:
        # 外壳说要人看却一条都没列 ⇒ 生产方前后不一致；照报，不让「要人看」因为形状不对而消失
        return [], "需人看：外壳 status=attention，却没有列出任何条目（生产方前后不一致，需查该工具）", "warn"
    rep: List[dict] = []
    keyed: List[Tuple[int, int, str]] = []
    floor = "info"
    for a in items:
        lvl, dl = a.get("level"), a.get("deadline")
        if not (report_all or lvl in ("warn", "alarm") or dl):
            continue
        rep.append(dict(a))
        overdue = bool(dl) and str(dl) < ctx.date
        if lvl in ("warn", "alarm") or overdue:      # alarm ⇒ warn（不是 error），见 docstring (d)
            floor = "warn"
        t = f"{'🚨 ' if lvl == 'alarm' else ''}[{lvl}] {_one_line(a.get('message'), _ITEM_CHARS)}"
        if dl:
            t += f"（截止 {dl}{'，已过期' if overdue else ''}）"
        sev = 0 if lvl == "alarm" else 1 if lvl == "warn" else 2 if overdue else 3 if dl else 4
        keyed.append((sev, len(keyed), t))
    body = _cap_join([t for _sev, _i, t in sorted(keyed)], _ATTENTION_CHARS)
    return rep, ("需人看：" + body) if body else "", floor


# ═══════════════════════════════ 各步的现行语义（逐字照抄编排器内联）═══════════════════════════════
# 每个 base 函数：(rc, 可用的 JSON 或 None, ctx, JSON 不可用时的说明) -> (level, [日志段], 片段)
# 对照的是仓库 scripts/alpha-hive-orchestrator.sh 里该步的分支链，按 shell 变量名引用（不记行号——行号随
# 任何一处上游改动整体漂移，记了就是快照）。分支链里每一支注成 `STEPn_RC=k`，内联 python 注成它赋给的变量名。

def _cont_summary(d: dict) -> str:
    """Step 10 的 CONT_SUMMARY（`STEP10_RC=1` 支里的内联 python）。字段异常时说出来，而不是像内联那样打一个空串。"""
    try:
        w = d.get("window", {})
        s = (f"过去 {w.get('trading_days', '?')} 个交易日跑了 {d.get('scanned_days', '?')} 次"
             f"（覆盖率 {d.get('coverage', 0):.0%}），ISO 周覆盖 "
             f"{d.get('weeks_covered', '?')}/{d.get('weeks_total', '?')}，"
             f"最长空档 {d.get('longest_gap', '?')} 个交易日")
    except Exception as e:  # noqa: BLE001
        return f"（连续性 JSON 字段异常：{type(e).__name__}: {e}）"
    try:
        miss = d.get("weeks_missed") or []
        if miss:
            s += f"；完全无扫描的周: {', '.join(miss)} ← 每个都是一个永久丢失的 T+7 观测"
    except Exception:  # noqa: BLE001 —— 内联同样只丢第二行
        pass
    return s


def _base10(rc: int, d: Optional[dict], ctx: _Ctx, why: str):
    if rc == 0:      # STEP10_RC=0
        return "info", [f"✅ Step 10：连续性健康（耗时 {ctx.dur_text}s）"], {"status": "healthy", **ctx.dur()}
    if rc == 1:      # STEP10_RC=1（CONT_SUMMARY）
        summ = _cont_summary(d) if d is not None else why
        return ("warn", [f"⚠️ Step 10：扫描连续性降级 — {summ}",
                         "（不计入 OVERALL_STATUS：这是历史覆盖率问题，非本轮失败）"],
                {"status": "degraded", **ctx.dur(), "detail_json": ctx.json_path})
    if rc == 3:      # STEP10_RC=3
        return "warn", ["⚠️ Step 10：无法判定连续性（找不到 pheromone.db）"], {"status": "undetermined"}
    if rc == 2:      # STEP10_RC=2
        return "warn", ["⏭️  Step 10 跳过（scan_continuity.py 不存在）"], {"status": "skipped"}
    if rc == 124:    # STEP10_RC=124（超时常数沿用 run_step --timeout 120）
        return "error", [f"⏰ Step 10 超时（>{ctx.timeout_text(120)}s）"], {"status": "timeout"}
    return ("warn", [f"⚠️ Step 10 异常（exit={rc}），不影响主流程"],   # STEP10_RC 其他（else）
            {"status": "error", "rc": rc})


def _readiness_line(d: dict) -> str:
    """Step 11 的 READINESS_LINE（分支链之前的内联 python）。"""
    try:
        c = d.get("cohort", {})
        s = (f"世代自 {c.get('date', '?')}（{c.get('version', '?')}）起，"
             f"已攒 {d.get('weeks_accrued', '?')}/{d.get('weeks_required', '?')} 个不重叠周"
             f"（{d.get('n_ripe_samples', '?')} 条已回填样本）")
        if d.get("eta_date"):
            s += f"，预计 ≈{d['eta_date']} 到位"
        if d.get("pool_note"):
            s += f" ⚠️ {d['pool_note']}"
        return s
    except Exception as e:  # noqa: BLE001
        return f"（就绪度 JSON 字段异常：{type(e).__name__}: {e}）"


#: BOUNDARY_JSON 的内联 python 里的 `keep`：Step 11 只把这几个键抄进 status.json
_BOUNDARY_KEEP = ("version", "boundary", "verdict", "marker_first_seen", "unmarked_after_boundary",
                  "alarm", "line", "error")

#: ic_rerun_readiness 把世代边界告警列成 attention 条目时用的 id 前缀（`_boundary_attention`：每条边界一条，
#: id = 前缀 + `.<版本>`，形状不一致时 `.inconsistent`——同一外壳内 id 须唯一）
_BOUNDARY_ATTENTION_ID = "ic_rerun.boundary_evidence"


#: jq 1.7 把 ±Infinity 读成 ±DBL_MAX（实测 jq-1.7.1-apple：`{"b":Infinity}` → `{"b":1.7976931348623157e+308}`）
_JQ_DBL_MAX = 1.7976931348623157e+308


def _jq_roundtrip(x: Any) -> Any:
    """一个值经 `jq --argjson` 进 STEPS_RESULT 后的样子：NaN → null，±Infinity → ±DBL_MAX。

    内联 python 用 `json.dumps` 打 BOUNDARY_JSON（默认 allow_nan）⇒ 文本里是裸 `NaN`/`Infinity`；
    其后的 `jq -e .` 合法性检查**吃得下**它们（jq 1.7 起），所以 BOUNDARY_JSON 不会退成 null，
    而是带着被 jq 改写过的数进片段。逐字复刻这个行为，而不是想当然地「整个退成 null」。
    """
    if isinstance(x, float) and not math.isfinite(x):
        return None if math.isnan(x) else math.copysign(_JQ_DBL_MAX, x)
    if isinstance(x, dict):
        return {k: _jq_roundtrip(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_jq_roundtrip(v) for v in x]
    return x


def _boundary_raw(d: dict) -> Optional[dict]:
    """BOUNDARY_JSON 的内联 python：不是 dict ⇒ null；只留 keep 里的键。
    返回的是**未经 jq 改写**的原值（文案用它）；进片段前再过 `_jq_roundtrip`（`_S11_NEW` 那条 `--argjson`）。"""
    b = d.get("cohort_boundary_evidence")
    if not isinstance(b, dict):
        return None
    return {k: b.get(k) for k in _BOUNDARY_KEEP if k in b}


def _jq_alt(b: dict) -> str:
    """`jq -r '.line // .verdict'`（边界核对两条日志里的那个，吃的是未改写的 BOUNDARY_JSON 文本）：
    null / false 视为缺；字符串原样；NaN 不算缺、打成 null；其余按 JSON 打（一行）。"""
    v = b.get("line")
    if v is None or v is False:
        v = b.get("verdict")
    if isinstance(v, str):
        return v
    return json.dumps(_jq_roundtrip(v), ensure_ascii=False)


def _boundary_listed_in_attention(d: dict) -> bool:
    """v1 外壳里生产方已把世代边界告警列成 alarm 条目 ⇒ 原文与处理方式会出现在「需人看」段里。"""
    att = d.get("attention") if "schema_version" in d else None
    return isinstance(att, list) and any(
        isinstance(a, dict) and a.get("level") == "alarm" and isinstance(a.get("id"), str)
        and (a["id"] == _BOUNDARY_ATTENTION_ID or a["id"].startswith(_BOUNDARY_ATTENTION_ID + "."))
        for a in att)


#: Step 11 的 `run_step --timeout`（仓库编排器 Step 11 那一行；编排器不给 Step 11 传 --timeout-seconds）。
#: `tests/test_orchestrator_steps.py::TestStep11Timeout` 核对它与仓库编排器那一行一致——改了超时而这里没跟，那条红。
STEP11_TIMEOUT_DEFAULT = 60


def _base11(rc: int, d: Optional[dict], ctx: _Ctx, why: str):
    line = _readiness_line(d) if d is not None else why
    if rc == 0:      # STEP11_RC=0
        level, parts, frag = ("info", [f"🎯 Step 11：IC 重跑**已就绪** — {line}",
                                       "→ 该跑: experiments/ml_expected_return_replay.py 与 signal_archive.py --analyze",
                                       "（刻意不自动跑：IC 分析要人看结果并做判断）"], {"status": "ready"})
    elif rc == 1:    # STEP11_RC=1
        level, parts, frag = "info", [f"⏳ Step 11：{line}"], {"status": "accruing"}
    elif rc == 3:    # STEP11_RC=3
        level, parts, frag = "warn", ["⚠️ Step 11：无法判定 IC 重跑就绪度"], {"status": "undetermined"}
    elif rc == 2:    # STEP11_RC=2
        level, parts, frag = "warn", ["⏭️  Step 11 跳过（ic_rerun_readiness.py 不存在）"], {"status": "skipped"}
    elif rc == 124:  # STEP11_RC=124：B 之前没有这一支（超时落进 else）。(i) 只改文案——status/rc/级别与 else 支逐字相同
        level, parts, frag = ("warn", [f"⏰ Step 11 超时（>{ctx.timeout_text(STEP11_TIMEOUT_DEFAULT)}s，"
                                       f"run_step 看门狗杀掉了 ic_rerun_readiness.py，exit={rc}），不影响主流程"],
                              {"status": "error", "rc": rc})
    else:            # STEP11_RC 其他（else）
        level, parts, frag = ("warn", [f"⚠️ Step 11 异常（exit={rc}），不影响主流程"],
                              {"status": "error", "rc": rc})
    # 世代边界核对（BOUNDARY_JSON 段 + `_S11_NEW` 合并）：片段恒带 cohort_boundary（取不到记 null，不假装正常）
    raw = _boundary_raw(d) if d is not None else None
    b = _jq_roundtrip(raw)
    if raw is None and d is None and rc != 2:
        # (i) 就绪度 JSON 本轮不可用（没写出 / 不是本轮的 / 读不了）：不再说「代码早于 v0.45.334」——那是读到了
        # JSON 却没有这个键时才成立的推断。`--out` 只在最后写一次、边界核对排在 F&G 前瞻检验之后，所以超时 =
        # 本轮根本没核对（09-30 rc=124 实况）。rc=2（脚本不存在）照旧，与 B 之前逐字相同。
        cause = ("ic_rerun_readiness.py 被 run_step 看门狗按超时杀掉，就绪度 JSON 没写出来"
                 "（边界核对排在 F&G 前瞻检验之后、--out 只在最后写一次）" if rc == 124
                 else "就绪度 JSON 本轮不可用")
        parts.append(f"Step 11 世代边界核对：本轮未核对——{cause}{why}")
    elif raw is None:
        parts.append("Step 11 世代边界核对：就绪度 JSON 里没有 cohort_boundary_evidence"
                     "（代码早于 v0.45.334 或 JSON 不可读），本轮未核对")
    elif b.get("alarm") is True:
        level = _max_level(level, "warn")
        if _boundary_listed_in_attention(d):
            # (d) 原文与处理方式已在「需人看」段（生产方的 alarm 条目）里，这里只留指针，不重复一遍
            parts.append(f"🚨 Step 11：世代边界核对未通过（逐条原文与处理方式见本行「需人看」；"
                         f"不计入 OVERALL_STATUS：口径问题，非本轮失败；完整判别在 {ctx.json_path}）")
        else:
            parts += [f"🚨 Step 11：世代边界核对未通过 — {_jq_alt(raw)}",
                      "→ 处理：按 ic_rerun_readiness.py 顶部「更正条目」流程**追加**一条更正（新标签 + _CORRECTS），不改写原条目",
                      f"（不计入 OVERALL_STATUS：口径问题，非本轮失败；完整判别在 {ctx.json_path}）"]
    else:
        parts.append(f"Step 11 世代边界核对：{_jq_alt(raw)}")
    frag["cohort_boundary"] = b
    return level, parts, frag


def _cov_summary(d: dict) -> str:
    """Step 12 的 COV_SUMMARY（`STEP12_RC=1` 支里的内联 python）。"""
    try:
        bad = [f for f in d.get("fields", []) if f.get("degraded")]
        s = "; ".join(f"{f['field']} {f['have']}/{f['total']}" for f in bad)
        if d.get("likely_network_layer"):
            s += "  ⚠️ 多个不同数据源同时降级，疑为网络/闸门层"
        return s
    except Exception as e:  # noqa: BLE001
        return f"（覆盖率 JSON 字段异常：{type(e).__name__}: {e}）"


def _base12(rc: int, d: Optional[dict], ctx: _Ctx, why: str):
    if rc == 0:      # STEP12_RC=0
        return "info", ["✅ Step 12：字段覆盖率健康 + 来源标签自洽"], {"status": "healthy"}
    if rc == 1:      # STEP12_RC=1（COV_SUMMARY）
        summ = _cov_summary(d) if d is not None else why
        return ("warn", [f"⚠️ Step 12：检出字段降级或来源标签矛盾 — {summ}",
                         "→ 本次报告里这些指标会显示「—」；若需补数据，重跑当日扫描"], {"status": "degraded"})
    if rc == 3:      # STEP12_RC=3
        return ("warn", ["⚠️ Step 12：无法判定字段覆盖率（扫描结果文件缺失或不可解析）"],
                {"status": "undetermined"})
    if rc == 2:      # STEP12_RC=2
        return "warn", ["⏭️  Step 12 跳过（scan_coverage_gate.py 不存在）"], {"status": "skipped"}
    return ("warn", [f"⚠️ Step 12 异常（exit={rc}），不影响主流程"],   # STEP12_RC 其他（else，无 124 分支）
            {"status": "error", "rc": rc})


def _calwatch_summary(d: dict) -> str:
    """Step 13 的 CALWATCH_SUMMARY（`STEP13_RC=1` 支里的内联 python）。"""
    try:
        h = d.get("calendar_health", {})
        new = d.get("new_schedule_tables") or []
        if new:
            s = "上游已发布新日程：" + "、".join(new) + " —— 待人工抄录"
            for k in new:
                u = d.get("upstream", {}).get(k, {})
                items = u.get("new_items") or []
                s += f"；{k} 新增 {len(items)} 条（{u.get('source', '')}）"
            return s
        undet = d.get("undeterminable_tables") or []
        tail = ("上游暂无新日程可抄" if d.get("upstream_conclusive")
                else f"⚠️ 上游 {len(undet)} 个源无法判定（{'、'.join(undet)}），本次无法确认上游有无新日程")
        return (f"本地日历 {h.get('status')}：{h.get('binding_table')} 表只到 "
                f"{h.get('binding_last_date')}（剩 {h.get('binding_horizon_days')} 天），{tail}")
    except Exception as e:  # noqa: BLE001
        return f"（监视器 JSON 字段异常：{type(e).__name__}: {e}）"


def _base13(rc: int, d: Optional[dict], ctx: _Ctx, why: str):
    if rc == 0:      # STEP13_RC=0
        return "info", ["✅ Step 13：宏观日历健康，上游无新日程"], {"status": "healthy"}
    if rc == 1:      # STEP13_RC=1（CALWATCH_SUMMARY）
        summ = _calwatch_summary(d) if d is not None else why
        return ("warn", [f"⚠️ Step 13：{summ}",
                         "→ 抄录必须人工：只抄官方已发布日程，禁止按「每月第二周/第一个周五」推算",
                         "→ 抄完同步上移 economic_calendar._TABLE_SPECS 里该表的 verified_through"],
                {"status": "action_required", "detail_json": ctx.json_path})
    if rc == 3:      # STEP13_RC=3
        return ("warn", ["⚠️ Step 13：无法判定上游日程（抓取失败或页面改版）",
                         "→ 这**不等于**「上游没有新日程」；需人工看一眼源站，必要时修解析器"],
                {"status": "undetermined"})
    if rc == 2:      # STEP13_RC=2
        return "warn", ["⏭️  Step 13 跳过（economic_calendar_watch.py 不存在）"], {"status": "skipped"}
    return ("warn", [f"⚠️ Step 13 异常（exit={rc}），不影响主流程"],   # STEP13_RC 其他（else，无 124 分支）
            {"status": "error", "rc": rc})


def _backup_summary(d: dict) -> str:
    """Step 15 的 BACKUP_CONT_SUMMARY（`STEP15_RC=1` 支里的内联 python）。"""
    try:
        w = d.get("window", {})
        s = (f"过去 {w.get('trading_days', '?')} 个交易日只成功了 {d.get('backed_up_days', '?')} 次"
             f"（覆盖率 {d.get('coverage', 0):.0%}），最长空档 {d.get('longest_gap', '?')} 个交易日")
    except Exception as e:  # noqa: BLE001
        return f"（备份连续性 JSON 字段异常：{type(e).__name__}: {e}）"
    try:
        miss = d.get("weeks_missed") or []
        if miss:
            s += f"；完全无成功备份的周: {', '.join(miss)}"
    except Exception:  # noqa: BLE001
        pass
    return s


def _base15(rc: int, d: Optional[dict], ctx: _Ctx, why: str):
    if rc == 0:      # STEP15_RC=0
        return "info", [f"✅ Step 15：备份连续性健康（耗时 {ctx.dur_text}s）"], {"status": "healthy", **ctx.dur()}
    if rc == 1:      # STEP15_RC=1（BACKUP_CONT_SUMMARY）
        summ = _backup_summary(d) if d is not None else why
        return ("warn", [f"⚠️ Step 15：备份连续性降级 — {summ}",
                         "（不计入 OVERALL_STATUS：单次失败是噪音，这是聚合信号，需要人工介入排查）"],
                {"status": "degraded", **ctx.dur(), "detail_json": ctx.json_path})
    if rc == 3:      # STEP15_RC=3
        return ("warn", ["⚠️ Step 15：无法判定连续性（找不到 backup_status_history.jsonl，可能是刚接入生产）"],
                {"status": "undetermined"})
    if rc == 2:      # STEP15_RC=2
        return "warn", ["⏭️  Step 15 跳过（backup_continuity.py 不存在）"], {"status": "skipped"}
    if rc == 124:    # STEP15_RC=124（超时常数沿用 run_step --timeout 60）
        return "error", [f"⏰ Step 15 超时（>{ctx.timeout_text(60)}s）"], {"status": "timeout"}
    return ("warn", [f"⚠️ Step 15 异常（exit={rc}），不影响主流程"],   # STEP15_RC 其他（else）
            {"status": "error", "rc": rc})


# ═══════════════════════════════════ 工具步骤（10/11/12/13/15）的合成 ═══════════════════════════════════

class _ToolStep:
    """一个工具步骤。

    json_rcs   现行 bash 在哪几支**读** `--out` JSON（(a')）。None = 除 rc=2 外每一支都读
               （Step 11：READINESS_LINE / BOUNDARY_JSON 在分支链之前无条件求值）。测试从仓库副本核对。
    date_flag  生产方接收业务日期的参数名。B 必须用它把 DATE_STR 显式传进去（模块 docstring「B 接线须知」）；
               测试核对它在生产方的 argparse 里真实存在。
    """

    def __init__(self, label: str, key: str, tool: str, noun: str, base: Callable,
                 json_rcs: Optional[FrozenSet[int]], date_flag: str):
        self.label, self.key, self.tool, self.noun, self.base = label, key, tool, noun, base
        self.json_rcs, self.date_flag = json_rcs, date_flag

    def reads_json(self, rc: int) -> bool:
        if rc == 2:
            return False
        return self.json_rcs is None or rc in self.json_rcs


def _why(doc: _Doc, noun: str) -> str:
    """JSON 不可用时替代摘要的那句话（内联原文是「（无法解析…JSON）」，这里把原因说具体）。"""
    reason = {
        "missing": lambda: f"不存在（{doc.info.get('error', '')}）",
        "unparsable": lambda: f"无法解析（{doc.info.get('error', '')}）",
        "contract_error": lambda: "不符合输出契约",
        "stale": lambda: "不是本轮写的，未采用",
        "crashed": lambda: "是崩溃外壳",
        "not_read": lambda: "本轮未读取",
    }.get(doc.state, lambda: doc.state)()
    return f"（{noun} JSON {reason}）"


def _stale_text(info: dict) -> str:
    r = info.get("reason")
    if r == "date_mismatch":
        return f"外壳 date={info.get('json_date')}，本轮是 {info.get('expected_date')}"
    if r == "written_before_run":
        return f"写于 {info.get('written_at')}，早于本步开始 {info.get('run_start')}"
    if r == "mtime_before_date":
        return f"旧格式、最后修改于 {info.get('written_at')}，早于本轮日期 {info.get('expected_date')}"
    return str(r)


def _rollover_text(step: _ToolStep, json_name: str, roll: dict) -> str:
    why = (f"写于 {roll.get('written_at')}，不早于本步开始 {roll.get('run_start')}"
           if roll.get("basis") == "generated_at>=run_start"
           else f"写于 {roll.get('written_at')}、mtime {roll.get('mtime')}（未给 --run-start，按日期与 mtime 判）")
    return (f"ℹ️ {step.label}：{json_name} 外壳 date={roll.get('json_date')} 是本轮 {roll.get('expected_date')} 的次日"
            f"——工具跑过了午夜、按时钟取了日期（{why}），按本轮结果采用；编排器已用 {step.date_flag} 传 DATE_STR，仍出现 ⇒ 生产方没收到它")


def _json_problem(step: _ToolStep, doc: _Doc, rc: int, json_name: str,
                  rc_status: str) -> Optional[Tuple[str, dict, str]]:
    """(问题状态, 细节, 日志段)；这份 JSON 没有问题、或它的缺失是预期的 ⇒ None。"""
    if doc.state == "stale":                                          # (a)
        return STALE, {"contract": doc.contract, "stale": doc.info}, (
            f"⚠️ {step.label}：{json_name} 不是本轮写的（{_stale_text(doc.info)}），"
            f"未采用其内容——不拿上一次的结果冒充本轮")
    if doc.state == "contract_error":                                 # (b)
        problems = doc.info.get("problems") or []
        return CONTRACT_ERROR, {"problems": problems}, (
            f"🚨 {step.label}：{json_name} 不符合输出契约：{'；'.join(problems)}")
    if doc.state == "unparsable":                                     # (f)(h)
        return UNPARSABLE, {"error": doc.info.get("error")}, (
            f"⚠️ {step.label}：{json_name} 无法解析（{doc.info.get('error')}）")
    if doc.state == "missing" and rc in _RC_CLAIMS_WRITTEN:           # (f)
        if rc == 1:
            text = (f"🚨 {step.label}：退出码 1 却没有写出 {json_name}——旧代码的未捕获异常退出码也是 1，"
                    f"「{rc_status}」与「工具崩了」在这里分不开，不按正常记")
        else:
            text = f"⚠️ {step.label}：退出码 0 却没有写出 {json_name}（写盘失败？）——判定按退出码，细节缺失"
        return MISSING, {}, text
    return None      # missing 且 rc=3/124/其他：没有 JSON 是预期的，照旧


def _interpret_tool_step(step: _ToolStep, ctx: _Ctx) -> Tuple[str, List[str], Dict[str, dict]]:
    rc = ctx.rc
    json_name = Path(ctx.json_path).name if ctx.json_path else "（未提供 --json）"
    if rc == 2:
        # (a) 脚本不存在 ⇒ 这条路径上的任何 JSON 都不是本轮写的，一个字都不读
        doc = _Doc("not_read")
        level, parts, frag = step.base(rc, None, ctx, _why(doc, step.noun))
        try:
            leftover = bool(ctx.json_path) and Path(ctx.json_path).exists()
        except OSError:
            leftover = False
        if leftover:
            parts.append(f"（{json_name} 是先前留下的，脚本本轮没有运行，未采用）")
        return level, parts, {step.key: frag}

    doc = _load_doc(ctx.json_path, ctx, step.tool)
    level, parts, frag = step.base(rc, doc.usable, ctx, _why(doc, step.noun))
    roll = doc.info.get("date_rollover") if doc.state in ("v1", "crashed") else None

    def override(status: str, **extra) -> dict:
        o = {"status": status, "rc": rc, "rc_status": frag["status"]}
        for k in ("duration_seconds", "cohort_boundary"):
            if k in frag:
                o[k] = frag[k]
        o.update(extra)
        return o

    if doc.state in ("legacy", "v1"):
        frag["contract"] = doc.contract
        if doc.state == "v1":
            frag["envelope_status"] = doc.data.get("status")                  # (d)
            rep, text, floor = _attention(doc, ctx)
            if rep:
                frag["attention"] = rep
            if text:
                parts.append(text)
                level = _max_level(level, floor)
        if roll:                                                             # (a) date_rollover
            frag["date_rollover"] = True
            parts.append(_rollover_text(step, json_name, roll))
        return level, parts, {step.key: frag}

    if doc.state == "crashed":                                        # (c)：工具的问题，不受 (a') 限制
        rep, text, _floor = _attention(doc, ctx)
        err = doc.data.get("error")
        head = f"🚨 {step.label}：{step.tool} 运行时崩溃（exit={rc}），本轮没有可用结果"
        tail = text or (f"错误：{_one_line(err, 300)}" if err else "")
        extra = {"contract": "v1", "envelope_status": doc.data.get("status"), "error": err}
        if rep:
            extra["attention"] = rep
        msg = [head] + ([tail] if tail else [])
        if roll:
            extra["date_rollover"] = True
            msg.append(_rollover_text(step, json_name, roll))
        return "error", msg, {step.key: override("error", **extra)}

    problem = _json_problem(step, doc, rc, json_name, frag["status"])
    if problem is None:
        return level, parts, {step.key: frag}
    status, details, text = problem
    if step.reads_json(rc):                                           # (a') 这一支 bash 读 JSON ⇒ 覆盖
        parts.append(text)
        # rc=1 与崩溃同码：这一支本该有的结果不可用 ⇒ error（(f)）；契约不符恒 error（(b)）
        floor = "error" if status == CONTRACT_ERROR or rc == 1 else "warn"
        return _max_level(level, floor), parts, {step.key: override(status, **details)}
    # (a') 这一支 bash 不读 JSON ⇒ 判定仍按退出码，问题记进 json_problem
    parts.append(f"{text}（退出码 {rc} 这一支不读该 JSON：判定按退出码，问题记在 json_problem）")
    frag["json_problem"] = {"status": status, **details}
    floor = "error" if status == CONTRACT_ERROR else "warn"
    return _max_level(level, floor), parts, {step.key: frag}


# ═══════════════════════════════════════════ Step 2 与 Step 4 ═══════════════════════════════════════════

def _resolve_dirs(ctx: _Ctx) -> Tuple[Optional[Path], Optional[Path]]:
    data = ctx.data_dir or os.environ.get("ALPHA_HIVE_HOME")
    data_p = Path(data) if data else None
    report_p = Path(ctx.report_dir) if ctx.report_dir else data_p
    return data_p, report_p


def _nonempty(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _step2_rc1_evidence(ctx: _Ctx) -> Tuple[bool, List[str], dict]:
    """rc=1 时，扫描主流程是否**确实跑完了**。返回 (是否跑完, 缺哪几样, 附加信息)。

    `alpha_hive_daily_report.py` 退出码 1 有两类来源（2026-09-28 读 main() 与 `__main__` 核对）：
      ① v0.45.145 ML 退化闸：`main()` **正常返回**后，`__main__` 见 `ml_model_guard.verdict ∈
         {constant, near_constant}` 就 `sys.exit(1)`（线程卡死强退走 `os._exit(同一个码)`）；
      ② 任何未捕获异常（Python 默认 1）——其中一部分发生在 `.swarm_results` 与日报都已落盘之后
         （`.swarm_results` 早在 `_post_scan_enrichment` 就写了；`auto_commit_and_notify` 只兜四种异常），
         所以「当日产物在」**证明不了**跑完。
    能区分二者的结构化信号：`main()` 的**最后一步**是 `_timing.write()` 写 `logs/scan_timing.json`
    （`extra` 带 `git_push` 键；空扫描护栏那条早退写的是 `early_exit`）。它是本轮写的、日期对、不是早退
    ⇒ `main()` 已正常返回 ⇒ 退出码 1 只可能来自 ①。三样缺一样就保守记 failed（与现行一致）。
    「本轮写的」**只能**靠 `--run-start` 判（没给就不放行）：同一 DATE_STR 重跑时上一轮留下的
    scan_timing.json 日期同样对得上，而本轮可能根本没跑起来——`run_step` 在 TCC 拒绝时也返回 1。
    ML 判决另用 `ml_model_guard.check_day()`（纯读）复核一次，只用于把原因说清楚，不参与放行。
    """
    missing: List[str] = []
    extra: dict = {}
    data_dir, report_dir = _resolve_dirs(ctx)
    if data_dir is None:
        return False, ["未给 --data-dir 且没有 ALPHA_HIVE_HOME，无从核对产物"], extra

    swarm = data_dir / f".swarm_results_{ctx.date}.json"
    try:
        # (h) 同一套安全读：FIFO / 设备不打开读；内容不进片段、只核「非空对象」⇒ 不查深度、上限放宽
        sw, _ = _read_json(swarm, max_bytes=MAX_SWARM_BYTES, max_depth=None)
        if not (isinstance(sw, dict) and sw):
            missing.append(f"{swarm.name} 为空或不是对象")
    except FileNotFoundError:
        missing.append(f"{swarm.name} 不存在")
    except _JsonProblem as e:
        missing.append(f"{swarm.name} 读不了（{e}）")
    except Exception as e:  # noqa: BLE001
        missing.append(f"{swarm.name} 读不了（{type(e).__name__}）")

    for suffix in ("json", "md"):
        p = report_dir / f"alpha-hive-daily-{ctx.date}.{suffix}"
        if not _nonempty(p):
            missing.append(f"{p.name} 不存在或为空")

    timing = data_dir / "logs" / "scan_timing.json"
    try:
        t, t_mtime = _read_json(timing)
        t_extra = t.get("extra") if isinstance(t, dict) else None
        if not isinstance(t, dict) or t.get("date") != ctx.date:
            missing.append(f"scan_timing.json 的 date={t.get('date') if isinstance(t, dict) else None!r}"
                           f" ≠ {ctx.date}（主流程没跑到最后一步）")
        elif not isinstance(t_extra, dict) or "early_exit" in t_extra or "git_push" not in t_extra:
            missing.append("scan_timing.json 不是跑完主流程写的（早退或缺 git_push）")
        else:
            if ctx.run_start is None:
                missing.append("未给 --run-start：分不出 scan_timing.json 是本轮写的还是同日上一轮留下的")
            else:
                ts = _parse_iso_epoch(t.get("written_at"))
                if ts is None:
                    ts = t_mtime
                if math.floor(ts) < math.floor(ctx.run_start):
                    missing.append(f"scan_timing.json 写于 {t.get('written_at')}，早于本步开始")
            gp = t_extra.get("git_push")
            ok = gp.get("success") if isinstance(gp, dict) else None
            # 只认布尔：别的形状（列表 / 字符串）原样往下传，会在拼「推送成功 / 失败」时撞 unhashable → render_error
            extra["git_push_success"] = ok if isinstance(ok, bool) else None
    except FileNotFoundError:
        missing.append("scan_timing.json 不存在（主流程没跑到最后一步）")
    except _JsonProblem as e:
        missing.append(f"scan_timing.json 读不了（{e}）")
    except Exception as e:  # noqa: BLE001
        missing.append(f"scan_timing.json 读不了（{type(e).__name__}）")

    try:
        v = _repo_import("ml_model_guard").check_day(report_dir, ctx.date)
        extra["ml_model_guard"] = {"verdict": v.verdict, "n_files": v.n_files,
                                   "n_numeric": v.n_numeric, "distinct": v.distinct}
    except Exception as e:  # noqa: BLE001 —— 复核不了就如实写，不影响放行判据
        extra["ml_model_guard"] = {"verdict": None, "error": f"{type(e).__name__}: {_one_line(e, 200)}"}
    return not missing, missing, extra


def _step2_outcome(ctx: _Ctx) -> Tuple[str, List[str], dict]:
    """(status, rc=1 时缺的证据, 附加信息)。Step 2 与 Step 4 共用这一处判定，不各写各的。"""
    rc = ctx.rc
    if rc == 0:
        return "success", [], {}
    if rc == 124:
        return "timeout", [], {}
    if rc == 2:
        return "skipped", [], {}
    if rc == 1:
        ok, missing, extra = _step2_rc1_evidence(ctx)
        return ("success_with_warning" if ok else "failed"), missing, extra
    return "failed", [], {}


def _interpret_step2(ctx: _Ctx) -> Tuple[str, List[str], Dict[str, dict]]:
    key = "step2_hive_analysis"
    status, missing, extra = _step2_outcome(ctx)
    if status == "success":          # STEP2_RC=0
        return "info", [f"✅ Step 2 成功（耗时 {ctx.dur_text}s）"], {key: {"status": "success", **ctx.dur()}}
    if status == "timeout":          # STEP2_RC=124
        return ("error", [f"⏰ Step 2 超时（>{ctx.timeout_text(None)}s），蜂群分析被终止！"],
                {key: {"status": "timeout", **ctx.dur()}})
    if status == "skipped":          # STEP2_RC=2
        return "warn", ["⏭️  Step 2 跳过（脚本不存在）"], {key: {"status": "skipped"}}
    if status == "success_with_warning":                              # (g)
        ml = extra.get("ml_model_guard") or {}
        if ml.get("verdict") in ("constant", "near_constant"):
            warning = "ml_model_constant"
            why = (f"ML 概率退化（{ml['verdict']}：{ml.get('n_numeric')} 份里只有 {ml.get('distinct')} 个不同值）"
                   f"——需要人看模型：/usr/local/bin/python3 ml_model_guard.py --date {ctx.date}")
        else:
            warning = "rc1_after_completion_unexplained"
            why = (f"按现行代码只有 ML 退化闸会在主流程跑完后退出 1，但复核判决为 {ml.get('verdict')!r}"
                   f"{'（' + ml['error'] + '）' if ml.get('error') else ''}——原因待查")
        push = extra.get("git_push_success")
        # 直接指向 scan_timing.json 本身：它并进 status.json 的那条 jq 合并 09-14~09-25 从没生效过（见 v0.45.351）
        push_txt = {True: "推送成功",
                    False: f"推送失败（见 {_resolve_dirs(ctx)[0] / 'logs' / 'scan_timing.json'} 的 extra.git_push）"}.get(
            push, "推送结果未知")
        frag = {"status": "success_with_warning", **ctx.dur(), "rc": 1, "warning": warning,
                "ml_model_guard": ml, "git_push_success": push}
        return ("warn", [f"⚠️ Step 2 已跑完（耗时 {ctx.dur_text}s，日报已生成、{push_txt}）但退出码 1", why],
                {key: frag})
    # failed（STEP2_RC 其他，else）
    parts = [f"⚠️ Step 2 失败，但继续进行（耗时 {ctx.dur_text}s）"]
    frag = {"status": "failed", **ctx.dur()}
    if ctx.rc == 1:
        parts.append("退出码 1 且无法确认扫描主流程已跑完（Python 未捕获异常的退出码也是 1）："
                     + "；".join(missing))
        frag["rc1_unverified"] = missing
    return "warn", parts, {key: frag}


def _interpret_step4(ctx: _Ctx) -> Tuple[str, List[str], Dict[str, dict]]:
    """Step 4（仪表板）：Step 4 自己不跑任何东西，只看 Step 2 有没有跑完。Step 5 不在这里（见模块 docstring）。"""
    key = "step4_dashboard"
    status, _missing, _extra = _step2_outcome(ctx)
    if status in ("success", "success_with_warning"):     # 【Step 4/5】段 STEP2_RC=0
        frag = {"status": "skipped_builtin"}
        if status == "success_with_warning":              # (g) 追加键：rc=1 但主流程跑完了
            frag["step2_status"] = status
        return "info", ["⏭️  Step 4 跳过（仪表板已由 Step 2 pipeline 生成）"], {key: frag}
    rc = ctx.rc                                             # 【Step 4/5】段 else
    return ("error", [f"🚨 Step 4 未生成仪表板！仪表板由 Step 2 pipeline 生成，而 Step 2 没跑完（RC={rc}）"],
            {key: {"status": "failed", "reason": "step2_did_not_complete", "step2_rc": rc}})


# ═══════════════════════════════════════════ 步骤表 ═══════════════════════════════════════════

#: json_rcs：现行 bash 哪几支读 `--out` JSON（(a')）；date_flag：B 显式传 DATE_STR 用的参数（「B 接线须知」）
_RC1_ONLY: FrozenSet[int] = frozenset({1})
_TOOL_STEPS: Dict[str, _ToolStep] = {
    "10": _ToolStep("Step 10", "step10_scan_continuity", "scan_continuity", "连续性", _base10,
                    json_rcs=_RC1_ONLY, date_flag="--end"),
    "11": _ToolStep("Step 11", "step11_ic_rerun_readiness", "ic_rerun_readiness", "就绪度", _base11,
                    json_rcs=None, date_flag="--today"),
    "12": _ToolStep("Step 12", "step12_scan_coverage", "scan_coverage_gate", "覆盖率", _base12,
                    json_rcs=_RC1_ONLY, date_flag="--date"),
    "13": _ToolStep("Step 13", "step13_calendar_watch", "economic_calendar_watch", "监视器", _base13,
                    json_rcs=_RC1_ONLY, date_flag="--today"),
    "15": _ToolStep("Step 15", "step15_backup_continuity", "backup_continuity", "备份连续性", _base15,
                    json_rcs=_RC1_ONLY, date_flag="--end"),
}

#: step id → 片段里的键（render_error 也用它，所以不依赖任何会失败的东西）
STEP_KEYS: Dict[str, Tuple[str, ...]] = {
    "2": ("step2_hive_analysis",),
    "4": ("step4_dashboard",),
    **{sid: (s.key,) for sid, s in _TOOL_STEPS.items()},
}


def _normalize_step(raw: Any) -> Optional[str]:
    s = str(raw).strip() if raw is not None else ""
    if s in STEP_KEYS:
        return s
    for sid, keys in STEP_KEYS.items():
        if s in keys or s == "step" + sid:
            return sid
    return None


def _interpret(step_id: str, ctx: _Ctx) -> Tuple[str, List[str], Dict[str, dict]]:
    if step_id == "2":
        return _interpret_step2(ctx)
    if step_id == "4":
        return _interpret_step4(ctx)
    return _interpret_tool_step(_TOOL_STEPS[step_id], ctx)


# ═══════════════════════════════════════════ CLI ═══════════════════════════════════════════

class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse 出错默认打用法再 `sys.exit(2)`。`render` 的兜底接得住 SystemExit，但 message 就只剩
    「SystemExit: 2」——说不出错在哪个参数。`exit` 的覆盖把原因带进 render_error（关键的那一个，
    见 `test_argument_errors_say_what_was_wrong`）；`error` 的覆盖再省掉往 stderr 打的用法。"""

    def error(self, message):
        raise _ArgError(message)

    def exit(self, status=0, message=None):
        raise _ArgError(message or f"argparse exit {status}")


def _build_parser() -> _Parser:
    ap = _Parser(add_help=False, allow_abbrev=False)
    ap.add_argument("--step", required=True)
    ap.add_argument("--rc", required=True)
    ap.add_argument("--json", default=None)
    ap.add_argument("--date", required=True)
    ap.add_argument("--run-start", default=None)
    ap.add_argument("--duration", default=None)
    ap.add_argument("--timeout-seconds", default=None)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--report-dir", default=None)
    return ap


def _check_date(s: str) -> str:
    try:
        ok = len(s) == 10 and s[4] == "-" and s[7] == "-" and bool(datetime.strptime(s, "%Y-%m-%d"))
    except ValueError:
        ok = False
    if not ok:
        raise _ArgError(f"--date 必须是 YYYY-MM-DD，收到 {s!r}")
    return s


def _render(argv: List[str]) -> dict:
    ns = _build_parser().parse_args(argv)
    step_id = _normalize_step(ns.step)
    if step_id is None:
        raise _ArgError(f"未知 --step {ns.step!r}（可选 {'/'.join(STEP_KEYS)}）")
    ctx = _Ctx(
        rc=int(ns.rc), date=_check_date(ns.date), json_path=ns.json,
        run_start=None if ns.run_start is None else float(ns.run_start),
        duration=None if ns.duration is None else int(ns.duration),
        timeout=None if ns.timeout_seconds is None else int(ns.timeout_seconds),
        data_dir=ns.data_dir, report_dir=ns.report_dir,
    )
    level, parts, fragment = _interpret(step_id, ctx)
    return {"level": level, "message": SEP.join(_one_line(p) for p in parts if p and _one_line(p)),
            "steps_fragment": fragment}


def _peek(argv: List[str], name: str) -> Optional[str]:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _render_error(argv: List[str], exc: BaseException) -> dict:
    sid = _normalize_step(_peek(argv, "--step"))
    keys = STEP_KEYS.get(sid, ("unknown_step",)) if sid else ("unknown_step",)
    raw_rc = _peek(argv, "--rc")
    try:
        rc: Any = int(raw_rc) if raw_rc is not None else None
    except ValueError:
        rc = raw_rc
    msg = f"render_error: {type(exc).__name__}: {_one_line(exc, 500)}"
    return {"level": "error", "message": msg,
            "steps_fragment": {k: {"status": RENDER_ERROR, "rc": rc} for k in keys}}


def _sanitize(x: Any) -> Any:
    """输出前清洗：非有限浮点 → null（jq 不认 NaN/Infinity），非常规类型 → 字符串。"""
    if isinstance(x, dict):
        return {str(k): _sanitize(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_sanitize(v) for v in x]
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if x is None or isinstance(x, (str, int, bool)):
        return x
    return str(x)


def _check_shape(out: Any) -> None:
    if not (isinstance(out, dict) and set(out) == {"level", "message", "steps_fragment"}
            and out["level"] in LEVELS and isinstance(out["message"], str)
            and isinstance(out["steps_fragment"], dict) and out["steps_fragment"]
            and all(isinstance(v, dict) and isinstance(v.get("status"), str)
                    for v in out["steps_fragment"].values())):
        raise ValueError(f"解释结果形状不对：{out!r}"[:500])


def render(argv: Optional[List[str]] = None) -> dict:
    """解释一次；**永不抛**。内部任何异常 ⇒ render_error 结果（级别 error）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # 解释期间 stdout 改到 stderr：被 import 的模块若打了什么，不许混进那「恰好一行」
        with contextlib.redirect_stdout(sys.stderr):
            out = _sanitize(_render(argv))
            _check_shape(out)
            json.dumps(out, allow_nan=False)
        return out
    except BaseException as e:  # noqa: BLE001 —— 这里就是「永不抛」的那道兜底
        try:
            return _sanitize(_render_error(argv, e))
        except BaseException:  # noqa: BLE001
            return {"level": "error", "message": "render_error: (render_error 自身失败)",
                    "steps_fragment": {"unknown_step": {"status": RENDER_ERROR, "rc": None}}}


def main(argv: Optional[List[str]] = None) -> int:
    out = render(argv)
    try:
        line = json.dumps(out, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except BaseException as e:  # noqa: BLE001
        line = json.dumps(_render_error(list(sys.argv[1:] if argv is None else argv), e),
                          ensure_ascii=True, separators=(",", ":"))
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001 —— 连 stdout 都写不了（管道断了），bash 会退回只按 rc 记
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
