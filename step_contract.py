"""编排器各步骤工具的输出契约（v0.45.355）：统一 JSON 外壳 + 显式 attention + 崩溃退出码 3。

为什么要有这一层（2026-09-27 根因调查，只读）：生产方（仓内 CLI，有 git + pytest）与消费方
（编排器 / 周度任务 SKILL.md，在仓库外、没有测试）之间的接口——`--out` JSON 的键、`--quiet` 的段、
退出码的含义——从来没有一份机器能核对的定义，只在至少 4 处用散文各抄一份，于是任何一端单独变了
另一端都不会红。实测后果：
  · `--quiet` 12 天变了 4 次格式，周度 SKILL 一直写「三段」；
  · Python 未捕获异常的退出码就是 1，与「未就绪 / 降级」同码 ⇒ 工具崩了被编排器记成「正常攒样本」
    （实测：`ic_rerun_readiness --db <坏库>` rc=1、不写 `--out`、stdout 为空）；
  · JSON 没有日期 ⇒ 同一 DATE_STR 重跑超时时，编排器会无声读到上一次留下的文件；
  · 「要人看」靠段首图标推断 ⇒ H1 锚点「须早于 2026-10-12」藏在 ⏳ 段里，按图标永远不报。

本模块只定义**外壳**，不改各工具已有的键与退出码语义（向后兼容：编排器现行的内联解析照常读得到）：

    {
      ...各工具原有的键...,
      "schema_version": 1,
      "tool": "ic_rerun_readiness",
      "date": "2026-09-28",            # 这次运行服务的业务日期（消费方拿它与 DATE_STR 比新鲜度）
      "generated_at": "2026-09-28T14:52:03-07:00",
      "status": "ok" | "attention" | "undetermined" | "error",
      "attention": [ {"id", "level", "message", "deadline", "source"}, ... ]
    }

`status` 是**语义**（要不要人看），与退出码**分开**：各工具的退出码约定（0/1/3 各自含义）不变。
`attention` 由生产方**显式**列出（带截止日期的也在这里），消费方不再从图标反推。
崩溃统一由 `run_tool` 兜：退出码 3（「无法判定」，编排器已有这一支）+ 照样写出 `status: "error"` 的外壳。
⚠️ `run_tool` 兜不住模块导入期的失败——哪些兜住了（`step_contract` 本身导入失败 ⇒ 3）、哪些仍是 1，见其 docstring。

守卫：`tests/test_step_contract.py`（外壳形状、崩溃路径、各工具真实产出满足契约）；pre-push 契约守卫。
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

#: 外壳顶层键。各工具的原有键不得与之重名（`envelope` 撞名即抛——静默覆盖就是把一个失败改写成另一个值）。
ENVELOPE_KEYS = ("schema_version", "tool", "date", "generated_at", "status", "attention")

#: ok = 没有要人做的事；attention = 要人看（含「已就绪、该去跑分析」）；
#: undetermined = 这次判断不了（数据缺失等，工具自己知道）；error = 工具自己崩了（由 run_tool 写）。
STATUSES = ("ok", "attention", "undetermined", "error")

#: info = 知会（例：已就绪）；warn = 要人处理但不紧急；alarm = 口径 / 数据可能已经错了。
LEVELS = ("info", "warn", "alarm")

_ATTENTION_KEYS = ("id", "level", "message", "deadline", "source")
_PT = ZoneInfo("America/Los_Angeles")


def business_today() -> str:
    """America/Los_Angeles 的当前日历日（夏令时 PDT、冬令时 PST）——各工具**未显式给日期时**的缺省业务日。

    与扫描本身给结果标日期的口径相同（`alpha_hive_daily_report` 的 `date_str`、`hive_logger.pdt_today`、
    `alpha_hive_bot/config.py::pdt_today` 都取 LA 日）。

    ⚠️ **不是**编排器 `DATE_STR` 的口径：`DATE_STR=$(date +"%Y-%m-%d")` 取本机时区，本机是
    `America/Vancouver`（`/etc/localtime`）。两地目前同一套偏移，但本机 tzdata（2026c）里温哥华自
    2026-11-01 02:00 起**常年 UTC-7**（`zdump -v America/Vancouver`：`Sun Nov  1 09:00:00 2026 UT =
    Sun Nov  1 02:00:00 2026 MST isdst=0 gmtoff=-25200`，而洛杉矶同一刻回到 PST gmtoff=-28800）⇒
    此后每逢洛杉矶冬令时，本机 00:00–01:00 之间 `DATE_STR` 比本函数**超前一天**（本机已是次日，LA 仍是当日）。
    launchd 14:00 那一次不受影响；开机补跑（RunAtLoad）落在这一小时里才会撞上。

    所以消费方**必须显式传日期**（`--date` / `--end` / `--today`，外壳 `date` 就等于传入值），不能指望缺省值
    与自己的 `DATE_STR` 相等——编排器接线（B）会传 `DATE_STR`。注意那一小时里传 `DATE_STR` 也有代价：
    读扫描产物的工具（例如 `scan_coverage_gate` 找 `.swarm_results_<date>.json`）拿到的是扫描按 LA 日
    写出的文件名的次日。本版不改任何一方的口径，只把关系写实。
    """
    return datetime.now(_PT).strftime("%Y-%m-%d")


def _check_date(s, what: str) -> str:
    # 形状也要卡：`strptime` 放行 "2026-9-28"，而消费方拿它与 DATE_STR 做**字符串**比较（新鲜度），不补零就永远不等。
    s = str(s) if s is not None else ""
    try:
        ok = len(s) == 10 and s[4] == "-" and s[7] == "-" and bool(datetime.strptime(s, "%Y-%m-%d"))
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{what} 必须是 YYYY-MM-DD，收到 {s!r}")
    return s


def _is_iso_date(s) -> bool:
    """JSON 层面的日期：**字符串**且严格 YYYY-MM-DD（`_check_date` 会先 `str()`，只适合构造时归一化输入）。"""
    if not isinstance(s, str):
        return False
    try:
        _check_date(s, "")
    except ValueError:
        return False
    return True


def _item_problems(a: Dict) -> List[str]:
    """一条 attention 的逐字段问题（已确认是含全部键的 dict）。`attention_item`（构造时抛）、`envelope`（包装时抛）
    与 `validate`（消费方核对）共用这一处规则——此前 validate 只看键在不在与 level，int id / None message /
    dict 或 "2026-9-5" 的 deadline 都放行，生产方构造时会被拒的东西消费方照单全收。"""
    out = []
    if not isinstance(a["id"], str) or not a["id"]:
        out.append(f"id 必须是非空字符串，收到 {a['id']!r}")
    if not isinstance(a["level"], str) or a["level"] not in LEVELS:
        out.append(f"level 只接受 {LEVELS}，收到 {a['level']!r}")
    if not isinstance(a["message"], str) or not a["message"]:
        out.append(f"message 必须是非空字符串，收到 {a['message']!r}")
    if a["deadline"] is not None and not _is_iso_date(a["deadline"]):
        out.append(f"deadline 必须是 None 或 YYYY-MM-DD 字符串，收到 {a['deadline']!r}")
    if a["source"] is not None and not isinstance(a["source"], str):
        out.append(f"source 必须是字符串或 None，收到 {a['source']!r}")
    return out


def _attention_problems(items) -> List[str]:
    """attention 列表的问题清单：逐条形状 + 字段规则（`_item_problems`）+ **同一外壳内 id 唯一**。

    id 唯一：`id` 是消费方去重 / 路由的键（见 `attention_item`）。同一外壳里两条同 id ⇒ 按 id 保留首条的消费方
    会无声丢掉后一条——实测 v0.45.355 初版 `ic_rerun_readiness` 每条报警的世代边界都用同一个
    `ic_rerun.boundary_evidence`，而带截止日 2026-10-12 的 v0.45.340 那条排在后面。
    """
    if not isinstance(items, list):
        return ["attention 必须是列表"]
    problems, seen = [], {}
    for i, a in enumerate(items):
        where = f"attention[{i}]"
        if not isinstance(a, dict):
            problems.append(f"{where} 不是对象：{a!r}")
            continue
        missing = [k for k in _ATTENTION_KEYS if k not in a]
        if missing:
            problems.append(f"{where} 缺键 {missing}（请用 attention_item 构造）：{a!r}")
            continue
        problems += [f"{where}.{p}" for p in _item_problems(a)]
        iid = a["id"]
        if isinstance(iid, str) and iid:
            if iid in seen:
                problems.append(f"{where}.id={iid!r} 与 attention[{seen[iid]}] 重复——同一外壳内 id 必须唯一"
                                "（消费方按 id 去重 / 路由，重复的那条会被无声丢掉）")
            else:
                seen[iid] = i
    return problems


def attention_item(id: str, level: str, message: str, *, deadline: Optional[str] = None,
                   source: Optional[str] = None) -> Dict:
    """一条「要人看」的事项。`id` 是稳定的机器键（例 `ic_rerun.boundary_evidence.v0.45.340`），消费方靠它去重 / 路由
    ——同一外壳内必须唯一（`envelope` / `validate` 都查）；同一类事项可能出现多条时，把区分它们的键（版本、表名 …）拼进 id。"""
    item = {"id": id, "level": level, "message": message,
            # deadline 先归一化（接受 `date` 对象），再与其余字段走同一套规则
            "deadline": None if deadline is None else _check_date(deadline, f"attention {id!r} 的 deadline"),
            "source": source}
    problems = _item_problems(item)
    if problems:
        raise ValueError(f"attention 条目不合契约（{id!r}）：{'；'.join(problems)}")
    return item


def envelope(tool: str, date: str, status: str, *, attention: Iterable[Dict] = (),
             payload: Optional[Dict] = None, generated_at: Optional[str] = None) -> Dict:
    """把工具原有的结果 `payload` 包进统一外壳（原有键原样保留在顶层，向后兼容）。

    attention 条目与 `validate` 走同一套规则（`_attention_problems`，含 id 唯一），不合即抛——生产方写不出消费方会拒收的
    **条目**。（`status=ok` 却带 warn/alarm 这类整体自洽性只由 `validate` 查，这里不查。）"""
    if status not in STATUSES:
        raise ValueError(f"status 只接受 {STATUSES}，收到 {status!r}")
    items = [dict(a) if isinstance(a, dict) else a for a in attention]
    for a in items:
        if isinstance(a, dict) and "source" in a and a["source"] is None:
            a["source"] = tool
    problems = _attention_problems(items)
    if problems:
        raise ValueError(f"{tool} 的 attention 不合契约：{'；'.join(problems)}")
    body = dict(payload or {})
    clash = [k for k in ENVELOPE_KEYS if k in body]
    if clash:
        raise ValueError(f"{tool} 的结果里已有外壳保留键 {clash}——改名，别让外壳静默覆盖它")
    body.update({
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "date": _check_date(date, "envelope date"),
        "generated_at": generated_at or datetime.now(_PT).isoformat(timespec="seconds"),
        "status": status,
        "attention": items,
    })
    return body


def validate(env) -> List[str]:
    """外壳的问题清单（空 = 合规）。消费方与测试共用这一处定义。"""
    if not isinstance(env, dict):
        return [f"外壳必须是 JSON 对象，收到 {type(env).__name__}"]
    problems = [f"缺外壳键 {k}" for k in ENVELOPE_KEYS if k not in env]
    if problems:
        return problems
    if env["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version={env['schema_version']!r}，本代码只认 {SCHEMA_VERSION}")
    if env["status"] not in STATUSES:
        problems.append(f"status={env['status']!r} 不在 {STATUSES}")
    if not _is_iso_date(env["date"]):
        problems.append(f"date 必须是 YYYY-MM-DD 字符串，收到 {env['date']!r}")
    # 与生产方构造时（attention_item / envelope）同一套规则，含同一外壳内 id 唯一
    problems += _attention_problems(env["attention"])
    if env["status"] == "ok" and isinstance(env["attention"], list) and any(
            isinstance(a, dict) and a.get("level") in ("warn", "alarm") for a in env["attention"]):
        problems.append("status=ok 却带 warn/alarm 级 attention——两者自相矛盾")
    return problems


def _create_tmp(parent: Path, name: str):
    """在 `parent` 里独占创建一个临时文件，返回 (fd, 路径)。

    不用 `tempfile.mkstemp`：它把权限写死成 0600。这里用 `os.open(..., O_CREAT | O_EXCL, 0o666)`，
    由内核按 umask 裁剪 ⇒ 与普通 `open(path, "w")` 新建文件的权限相同（典型 0644），且不必临时改进程 umask。
    """
    for _ in range(100):
        tmp = parent / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), tmp
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "连续 100 次撞上已存在的临时文件名", str(parent))


def write_out(path, env: Dict) -> Path:
    """原子写（tmp + replace）：半截 JSON 比没有文件更糟——消费方会当成「解析失败」而不是「没跑」。

    · **不建目录**：父目录不存在 ⇒ 抛 `FileNotFoundError`（`OSError` 子类）。调用方各按改造前的语义接：
      scan_continuity / ic_rerun_readiness / backup_continuity 打 stderr、判定与退出码不变（见各自的 `_emit`）；
      scan_coverage_gate 改造前就不兜（未捕获 ⇒ 现按崩溃记 3）；economic_calendar_watch 改造前自己 `mkdir`，照旧。
      此前这里 `mkdir(parents=True)`，于是 `run_tool` 的崩溃路径会建出 Step 10/15 正常路径刻意不建的目录
      （它们的 `test_out_failure_does_not_change_verdict` 就是断言「目录不会被建出来」）。
    · **权限同普通 `open()`**（0o666 & ~umask，见 `_create_tmp`）：此前 `mkstemp` 产出 0600，而本仓的环境笔记
      正是拿 0600 认 iCloud 重名副本——正常产物也是 0600 就是在误导排查。`os.replace` 换的是新 inode，
      所以目标文件原有的权限**不沿用**，与新建文件同一口径。
    """
    p = Path(path)
    if not p.parent.is_dir():
        raise FileNotFoundError(errno.ENOENT, "父目录不存在（write_out 不替调用方建目录）", str(p.parent))
    fd, tmp = _create_tmp(p.parent, p.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(env, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def _arg_value(argv: List[str], *names: str) -> Optional[str]:
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def run_tool(tool: str, main: Callable[[], Optional[int]], *, argv: Optional[List[str]] = None,
             date_args: Iterable[str] = ("--date", "--today")) -> int:
    """`sys.exit(run_tool("x", main))` 替代 `sys.exit(main())`：未捕获异常 ⇒ 退出码 **3** + 错误外壳。

    · 为什么是 3：编排器对这些工具已有「3 = 无法判定」一支（打 WARN）；Python 默认的 1 在它们的约定里
      是「未就绪 / 降级（正常）」——崩溃被记成正常，就是这次要堵的洞。
    · 有 `--out` 就照样写出 `status: "error"` 的外壳（附异常类型与消息），让「崩了」在 JSON 里也看得见，
      而不是「文件不存在 ⇒ 无法解析」这种与「没跑」分不开的状态。写外壳本身失败（含 `--out` 的父目录不存在
      ——`write_out` 不建目录，崩溃路径不比正常路径多建任何东西）只打 stderr，退出码仍是 3。
    · `SystemExit`（argparse 用法错 = 2、工具自己的 `sys.exit(n)`）与 `KeyboardInterrupt` 原样放行。

    ⚠️ **兜不住导入期**：`run_tool` 只包 `main()` 的调用，模块顶层的 import 早于它执行。
      · `step_contract` 本身导入失败（缺文件 / 语法错 …）——五个工具各自在 import 处兜住、置哨兵，
        `__main__` 入口见哨兵即 **退出码 3** + stderr 说明（此时没有外壳可写，`--out` 不产出）。
        守卫：`tests/test_step_contract.py::TestStepContractImportFailure`。
      · **工具模块自己的其他导入期失败**（例如 `scan_continuity` 缺 `is_trading_day`、`ic_rerun_readiness`
        顶层代码抛异常）**仍是 Python 默认的退出码 1**，与「降级 / 未就绪」同码、也不写 `--out`。
        现行编排器按退出码分支，会把它记成正常；要等编排器改走 `orchestrator_steps.py`（它把「rc 1 却没有
        JSON」判为 error，见其 docstring (f)）才分得开。本版没有堵这一条，别把它读成「崩溃一律是 3」。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        rc = main()
        return 0 if rc is None else int(rc)
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:  # noqa: BLE001 —— 这里就是那个「谁会红」的兜底
        msg = f"{type(e).__name__}: {e}"
        print(f"{tool}: 未预期异常（退出码 3，按「无法判定」处理）—— {msg}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        out = _arg_value(argv, "--out")
        if out:
            date = _arg_value(argv, *date_args)
            try:
                date = _check_date(date, "date") if date else business_today()
            except ValueError:
                date = business_today()
            try:
                write_out(out, envelope(tool, date, "error", attention=[attention_item(
                    f"{tool}.crashed", "alarm", f"{tool} 运行时抛出未预期异常：{msg}", source=tool)],
                    payload={"error": msg}))
            except Exception as we:  # noqa: BLE001
                print(f"{tool}: 错误外壳写入 {out} 失败：{type(we).__name__}: {we}", file=sys.stderr)
        return 3
