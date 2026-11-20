"""深度页「让 Claude 核对」（v0.45.794）：人工证伪条件里没有结构化数据、`thesis_auto` 判不了的那几条，由用户点按钮、
看过预估费用并确认后，让 Claude（Opus 5.5 + 联网搜索）逐条核对。用户 2026-11-20 定：**只做 NVDA、按钮触发、先报价**。

不变式（改之前先读）
--------------------
- **花钱只走对话抽屉的同一道闸**：每条经 `ChatEngine.one_shot`——同一个每日上限（与对话共用）、同一份 `chat/spend.jsonl`、
  同一个失败分类器（可能已计费的按最坏情况记账、不重发）。本模块不碰 API 客户端、不记账。
- **人点了才跑**：`start(confirm=True)` 是唯一入口（页面先弹出预估、人确认才带 confirm）；没有定时、没有自动续跑、
  重启后不接着跑；演示模式不跑。不看 `chat_enabled`（那是对话抽屉的开关），每次点击本身就是一次明确同意。
- **一条一个请求，出错就停**：任何一条报错（额度不够 / 可能已计费 / 连不上 / 被拒…）⇒ 后面的不发，页面写明停在哪、为什么。
- **结果逐条落盘**（`ledger_io`，花了钱的东西不可再生），连同核对时的条件原文；配置里的原文后来改了 ⇒ 那条结果作废、不显示。
- **只对最新一天显示**（结论是核对那一刻的，拿去判过去那天会用上后来的信息；同 `thesis_auto`）。来源来自网络、未经本系统核实。
- **不进评分、不写配置、不写扫描**。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import ledger_io
from alphabot.chat import MODEL, WEB_SEARCH_MAX_USES, ChatError
from hive_logger import get_logger

_log = get_logger("alphabot.thesis_claude")

TICKERS = ("NVDA",)
#: 每条的费用区间（美元）：2026-11-20 NVDA 试跑实测单条 $0.36 / $0.46（每条一个请求、最多搜 5 次）。
#: 只用来给人看预估；真正拦的是对话的每日上限（每次请求前按最坏情况预检）。联网搜索带回的内容计入输入，单条可能更贵
EST_PER_CONDITION_USD = (0.35, 0.55)
STATUSES = ("fired", "not_fired", "unjudgeable")
CONFIDENCE = ("high", "medium", "low")
ET = ZoneInfo("America/New_York")
MAX_FINDING_CHARS = 600
MAX_SOURCES = 8

SYSTEM = ("你在帮用户核对一只股票的「投资论点失效条件」此刻是否已经满足。只依据本次联网搜索到的公开信息下结论；"
          "凭自身知识的说法要标明「未联网核实」；不给投资建议，不加与条件无关的评论。")

REASONS = {
    "unsupported": "这只票没开 Claude 核对（用户 2026-11-20 定只做 NVDA）",
    "demo": "演示模式不调 API",
    "config_unreadable": "证伪条件配置读不了",
    "no_conditions": "这只票没有需要 Claude 核对的人工条件",
    "running": "上一次核对还在进行",
    "not_confirmed": "没有确认预估费用，没发请求",
}


def manual_conditions(block: dict, auto_ids=()) -> List[dict]:
    """这只票配置里**没有**机器判据、也**不归** `thesis_auto` 核对（`auto_ids`，由 service 给——只有 service 可 import
    `thesis_auto`）的人工条件：`[{id, level, label, trigger}]`。"""
    out = []
    for lv in ("level_1_warning", "level_2_stop_loss"):
        b = (block or {}).get(lv)
        conds = b.get("conditions") if isinstance(b, dict) else b
        for c in conds if isinstance(conds, list) else []:
            if not isinstance(c, dict) or c.get("_machine") or not c.get("id") or not c.get("trigger"):
                continue
            if str(c["id"]) in auto_ids:
                continue
            out.append({"id": str(c["id"]), "level": 2 if "2" in lv else 1, "label": c.get("metric"),
                        "trigger": str(c["trigger"])})
    return out


def prompt(ticker: str, cond: dict, today_et: str) -> str:
    body = json.dumps({"id": cond["id"], "metric": cond.get("label"), "trigger": cond["trigger"]}, ensure_ascii=False)
    return f"""今天是 {today_et}（美东）。请用联网搜索，核对下面这一条关于 {ticker} 的「投资论点失效条件」此刻是否已经满足。

条件：{body}

规则：
1. 结论只能是 fired（已满足）/ not_fired（未满足）/ unjudgeable（判不了）。
2. 数字类条件：找最近一个已公布季度的确切数字与上一季度数字，写出两个数和算出的变化；找不到确切数字就 unjudgeable，不许估算。
3. 事件类条件：not_fired 只在你搜过、且能说出「查到的最新相关进展是什么、截至哪天」时才写；没搜到不等于没发生，那种情况写 unjudgeable。
4. 「超越」「禁止」这类判断要有可信来源支撑：厂商自称、传闻不算，只有这些时写清楚。
5. 条件措辞若有歧义，按最贴近原文的理解下结论，并把歧义写进 ambiguity。
6. 每个关键事实给来源（URL + 发布日期），优先公司公告 / 监管机构 / 主流财经媒体原文。

最后只输出一个 JSON（```json 代码块）：
{{"status": "fired|not_fired|unjudgeable", "finding": "两三句中文：查到了什么", "ambiguity": "条件措辞的歧义，没有就空字符串",
  "as_of": "所依据信息的最新日期", "sources": [{{"url": "...", "date": "...", "what": "这条来源说了什么"}}],
  "confidence": "high|medium|low"}}
"""


def _s(x, n: int) -> str:
    return str(x if x is not None else "").strip()[:n]


def parse_reply(text: str) -> Tuple[Optional[dict], Optional[str]]:
    """回复里最后一个 ```json 块 → 规整后的结论；读不懂 ⇒ `(None, 原因)`（钱已花，原因要写出来）。"""
    blocks = re.findall(r"```json\s*(.*?)```", text or "", flags=re.S)
    if not blocks:
        return None, "回复里没有 JSON 结论块"
    try:
        obj = json.loads(blocks[-1])
    except ValueError as exc:
        return None, f"结论块不是合法 JSON（{exc}）"
    if not isinstance(obj, dict):
        return None, "结论块不是对象"
    status = obj.get("status")
    if status not in STATUSES:
        return None, f"结论不是三种之一（{_s(status, 30)!r}）"
    sources = []
    for s in obj.get("sources") if isinstance(obj.get("sources"), list) else []:
        if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith(("http://", "https://")):
            sources.append({"url": _s(s["url"], 400), "date": _s(s.get("date"), 40), "what": _s(s.get("what"), 300)})
    conf = obj.get("confidence")
    return {"status": status, "finding": _s(obj.get("finding"), MAX_FINDING_CHARS),
            "ambiguity": _s(obj.get("ambiguity"), MAX_FINDING_CHARS), "as_of": _s(obj.get("as_of"), 40),
            "sources": sources[:MAX_SOURCES], "confidence": conf if conf in CONFIDENCE else None}, None


def estimate(n: int) -> dict:
    lo, hi = EST_PER_CONDITION_USD
    return {"low_usd": round(lo * n, 2), "high_usd": round(hi * n, 2), "per_condition_usd": [lo, hi]}


class ThesisClaude:
    """一个 Alpha Bot 进程一个。`conditions_fn(ticker) -> (条件, 错误)`、`dates_fn() -> [日期]` 由 server 接到 service；
    `spawn` 只给测试换（缺省起后台线程）。"""

    def __init__(self, svc, chat, *, conditions_fn: Callable, dates_fn: Callable,
                 now_fn: Optional[Callable[[], datetime]] = None, spawn: Optional[Callable] = None):
        self.svc = svc
        self.chat = chat
        self._conditions = conditions_fn
        self._dates = dates_fn
        self._now = now_fn or (lambda: datetime.now(ET))
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, name="thesis-claude", daemon=True).start())
        self._lock = threading.Lock()
        self._running: Dict[str, dict] = {}       # 票 ⇒ 进度（只在内存：重启后「进行中」的那份就是被打断的）
        self._unsaved: Dict[str, Tuple[dict, str]] = {}   # 票 ⇒ (落盘失败的那份结果, 原因)：钱已花，页面照样显示并写明

    def _path(self, t: str) -> Path:
        return Path(self.svc.state_dir()) / "thesis_claude" / f"{t}.json"

    def _save(self, t: str, doc: dict) -> None:
        ledger_io.write_json(self._path(t), doc)

    def _load(self, t: str) -> Optional[dict]:
        return ledger_io.load_json(self._path(t), default=None)

    def status(self, ticker: str, date: Optional[str] = None) -> dict:
        t = str(ticker or "").upper()
        out = {"ticker": t, "supported": t in TICKERS, "available": False, "rules": {
            "model": MODEL, "web_search_max_uses": WEB_SEARCH_MAX_USES, "per_condition_usd": list(EST_PER_CONDITION_USD)}}
        if not out["supported"]:
            return out | {"reason": "unsupported", "reason_zh": REASONS["unsupported"]}
        if self.svc.demo:
            return out | {"reason": "demo", "reason_zh": REASONS["demo"]}
        conds, err = self._conditions(t)
        if err:
            return out | {"reason": "config_unreadable", "reason_zh": f"{REASONS['config_unreadable']}（{err}）"}
        dates = self._dates()
        latest = dates[-1] if dates else None
        target = date or latest
        try:
            spend = self.chat.status()
        except Exception as exc:  # noqa: BLE001 - 额度显示不了不挡页面，照实说
            spend = {"spent_today": None, "spend_error": f"{type(exc).__name__}: {exc}"}
        try:
            last = self._load(t)
            load_error = None
        except (ledger_io.LedgerError, OSError, ValueError) as exc:
            last, load_error = None, f"上次核对结果读不了（{type(exc).__name__}: {exc}）"
        with self._lock:
            progress = dict(self._running[t]) if t in self._running else None
            unsaved = self._unsaved.get(t)
        save_error = None
        if unsaved is not None:
            last, save_error = json.loads(json.dumps(unsaved[0])), f"这次结果没能存盘（{unsaved[1]}），Alpha Bot 重启后就没了"
        if last and last.get("state") == "running" and progress is None:
            last = last | {"state": "interrupted"}      # 落盘时还在跑、进程里却没有 ⇒ Alpha Bot 中途重启过
        current = {c["id"]: c for c in conds}
        results = []
        for r in (last or {}).get("results") or []:
            c = current.get(r.get("id"))
            stale = None if c and c["trigger"] == r.get("trigger") else ("trigger_changed" if c else "not_in_config")
            results.append(r | {"stale": stale})
        return out | {
            "available": True, "date": target, "latest_date": latest,
            "applicable": latest is not None and target == latest,
            "conditions": conds, "estimate": estimate(len(conds)),
            "spent_today": spend.get("spent_today"), "cap_usd": spend.get("cap_usd"), "spend_error": spend.get("spend_error"),
            "running": progress, "last": (last | {"results": results}) if last else None, "load_error": load_error,
            "save_error": save_error}

    def start(self, ticker: str, confirm) -> dict:
        t = str(ticker or "").upper()
        if t not in TICKERS:
            raise ChatError(REASONS["unsupported"])
        if self.svc.demo:
            raise ChatError(REASONS["demo"])
        if confirm is not True:
            raise ChatError(REASONS["not_confirmed"])
        conds, err = self._conditions(t)
        if err:
            raise ChatError(f"{REASONS['config_unreadable']}（{err}）")
        if not conds:
            raise ChatError(REASONS["no_conditions"])
        with self._lock:
            if t in self._running:
                e = ChatError(REASONS["running"])
                e.status = 409
                raise e
            self._running[t] = {"done": 0, "total": len(conds), "current": conds[0]["id"],
                                "started_at": self._now().isoformat(timespec="seconds")}
        try:
            self._spawn(lambda: self._run(t, conds))
        except Exception:
            with self._lock:
                self._running.pop(t, None)
            raise
        return {"started": True, "total": len(conds), "estimate": estimate(len(conds))}

    def _run(self, t: str, conds: List[dict]) -> None:
        today_et = self._now().astimezone(ET).date().isoformat()
        doc = {"ticker": t, "state": "running", "started_at": self._now().isoformat(timespec="seconds"),
               "finished_at": None, "stopped_zh": None, "spent_usd": 0.0, "results": [], "not_run": []}
        try:
            self._save_quietly(t, doc)
            for i, c in enumerate(conds):
                with self._lock:
                    self._running[t].update(done=i, current=c["id"])
                row = {"id": c["id"], "level": c["level"], "label": c["label"], "trigger": c["trigger"],
                       "checked_at": self._now().isoformat(timespec="seconds")}
                try:
                    r = self.chat.one_shot(prompt(t, c, today_et), tag=f"thesis:{t}:{c['id']}", system=SYSTEM)
                except ChatError as exc:
                    # 额度不够 / 可能已计费 / 连不上 / 被拒：后面的不再发（重发可能再付一次，或者同样失败）
                    row.update(status=None, error_zh=str(exc))
                    doc["results"].append(row)
                    doc["not_run"] = [x["id"] for x in conds[i + 1:]]
                    doc["stopped_zh"] = f"核对「{c['label'] or c['id']}」时出错，后面 {len(conds) - i - 1} 条没发请求"
                    break
                doc["spent_usd"] = round(doc["spent_usd"] + float(r.get("cost_usd") or 0.0), 4)
                parsed, why = parse_reply(r.get("text") or "")
                row.update(cost_usd=r.get("cost_usd"), searches=r.get("searches"), notes=r.get("notes") or [],
                           served_by=r.get("served_by"), stop_reason=r.get("stop_reason"))
                if parsed is None:
                    row.update(status=None, error_zh=f"回复读不懂：{why}（这次已计费）")
                else:
                    row.update(parsed)
                doc["results"].append(row)
                self._save_quietly(t, doc)
        except Exception as exc:  # noqa: BLE001 - 后台线程里的意外：写进结果，别让「进行中」永远挂着
            _log.exception("Claude 核对 %s 出错", t)
            doc["stopped_zh"] = f"核对中途出错（{type(exc).__name__}: {exc}）"
        finally:
            doc["state"] = "done" if not doc["stopped_zh"] else "stopped"
            doc["finished_at"] = self._now().isoformat(timespec="seconds")
            self._save_quietly(t, doc)
            with self._lock:
                self._running.pop(t, None)

    def _save_quietly(self, t: str, doc: dict) -> None:
        try:
            self._save(t, doc)
        except Exception as exc:  # noqa: BLE001 - 结果已付费：落盘失败要说出来（留在内存、页面照样显示并写明），不能静默
            _log.error("Claude 核对结果落盘失败（%s）：%s", t, exc)
            with self._lock:
                self._unsaved[t] = (json.loads(json.dumps(doc)), f"{type(exc).__name__}: {exc}")
            return
        with self._lock:
            self._unsaved.pop(t, None)
