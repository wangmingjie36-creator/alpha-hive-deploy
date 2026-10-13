"""财报日的单一真相（v0.45.500）：唯一的日历解析器 + 唯一的读取入口。

为什么有这个模块
----------------
v0.45.104 起扫描的「下次财报日」取自本轮 ChronosBee，`EarningsWatcher.get_earnings_date`
（及其 `earnings_cache/{T}_date.json`）退成「蜂没跑出来」时的 yfinance 兜底。2026-10-13 的调查
（`experiments/earnings_date_cache_staleness_report.md`）实测四个问题，根子都是**同一个量有两份真相、两个解析器**：

1. 两个解析器口径不同：watcher 把 `Earnings Date: []` 写成字符串 "[]"（空列表是假值但不是 None，
   最后 `str([])[:10]`）；ChronosBee 不会。
2. ChronosBee 用 `(strptime(d) - datetime.now()).days >= 0` 判未来，**当天**得 −1 被丢。它的分数照旧——
   改它会截断维度 IC 协议 H2（`experiments/sell_strike_routing_prereg.md` 修订第 5 条）——本模块只读
   ChronosBee 另记的**未过滤**原始日期，过滤按 as_of 做日期算术（当天算「当天」）。
3. 兜底与主路径同源（yfinance calendar）：主路径失败的那天兜底同样失败；而那几天期权取数也一起挂了，
   兜底一次都没被调到（结构化日志 09-24 / 09-25 / 10-12：watcher 0 条）。
4. 第三个读者（Alpha Bot 溢价页）绕开扫描直接读 `_date.json`，只能靠 mtime 猜新旧；那份缓存自 09-04 起没人写，
   与 10-09 的扫描比 9/30 只不一致（NEE、CRCL 是缓存日期更晚的危险方向）。

现在
----
* **唯一解析器** `parse_calendar(cal)`：ChronosBee 与 watcher 都用它，空列表 / NaT / 非日期一律在这里处理。
* **观测**：ChronosBee 把 `observation(cal)` 放进 `details["earnings_calendar"]`，随 `.swarm_results_<date>.json`
  每个扫描日落盘——那就是按日记录的观测。**不另建缓存**（再建一份就是第三份真相）。
* **唯一读取入口** `resolve(obs, as_of)`，以及两个 `upcoming_fn` 工厂：`upcoming_fn_from_swarm`（扫描内，用本轮内存里的结果）
  与 `upcoming_fn_from_disk`（CLI / 展示，读已落盘的观测）。**都不打网。**
  兜底是**本地**的「最近一次有效观测 + 它的日期」，与网络故障不相关。扫描与账本用 `max_age_days=0`
  （只认当天的观测；取不到 ⇒ None ⇒ 卖权记 unknown），展示类读者自己决定能接受多旧，并把 `observed_on` 显示出来。
* ⚠️ `.swarm_results_<date>.json` 可能被事后重放改写（10-12 那份在 10-13 07:33 被重放覆盖）：`observed_on` 是文件日期，
  `observed_fetched_at` 是 ChronosBee 真正取日历的墙钟时刻，在意时点的读者比一下这两个。

`upcoming_fn` 返回形状（下游 `earnings_vol_signal._precheck`、`sell_strike_candidates.classify_earnings` 读）：

  {"earnings_date": "YYYY-MM-DD", ...}                      as_of 当天或之后的第一个财报日（当天算）
  {"earnings_date": None, "reason": "only_past_dates", ...}  日历里只剩已过的日期（Yahoo 还没换下一季）⇒「视野内没有」
  None                                                       判不了：蜂失败 / 没有观测 / 日历里没有财报日 / 观测太旧

与 v0.45.104 的 `_earnings_date_from_swarm`（兜底成功时）相比，卖权的 `earnings_status` 每种情形要么相同、
要么更严（before_expiry / unknown），守卫 `tests/test_earnings_calendar.py::TestNeverLooserThanLegacy`；
预注册里按「修 bug、不截断」记录（`sell_strike_routing_prereg.md` 2026-10-13 一节）。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from hive_logger import PATHS, get_logger

_log = get_logger("earnings_calendar")

SCHEMA = 1
#: ChronosBee details 里的字段名
FIELD = "earnings_calendar"
_CHRONOS = "ChronosBeeHorizon"
#: 扫描时允许「当天取不到财报日」的票数（超过 ⇒ alert_manager P2）。0：正常日 30/30 都能取到。
MAX_UNRESOLVED = 0

SOURCE_CALENDAR = "chronos_bee_calendar"
#: v0.45.500 之前落盘的结果没有 FIELD，只能读 ChronosBee 已按 `datetime.now()` 过滤过的催化剂（标签沿用旧值）
SOURCE_LEGACY = "chronos_bee_catalyst"


# ─────────────────────────────────────────────── 解析

def iso_date(v) -> Optional[str]:
    """一个日期样的值 → "YYYY-MM-DD"；解析不了 / NaT ⇒ None。只认以 ISO 日期开头的字符串。"""
    if v is None:
        return None
    try:
        if isinstance(v, str):
            s = v.strip()[:10]
        elif isinstance(v, datetime):        # 含 pandas.Timestamp
            s = v.date().isoformat()
        elif isinstance(v, date):
            s = v.isoformat()
        elif hasattr(v, "strftime"):
            s = v.strftime("%Y-%m-%d")
        else:
            return None
        # 每条分支的产出都再过一遍：`pd.NaT.date().isoformat()` 不抛、返回字符串 "NaT"（测试实测）
        return date.fromisoformat(str(s)).isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


_iso = iso_date


def parse_calendar(cal) -> Optional[List[str]]:
    """yfinance `Ticker.calendar`（dict 或 DataFrame，随版本）→ 「Earnings Date」的 ISO 日期（去重、升序）。

    None ⇒ 日历本身不可用（None / 不认识的类型 / `to_dict` 抛了）；[] ⇒ 日历在、但没有可解析的财报日
    （键缺失、空列表、全是 NaT / 垃圾）。调用方据此区分「没取到」与「取到了、里面没有」。
    """
    if cal is None:
        return None
    if not isinstance(cal, dict) and hasattr(cal, "to_dict"):
        try:
            cal = cal.to_dict()
        except Exception:  # noqa: BLE001 - 第三方对象，任何异常都按「日历不可用」处理
            return None
    if not isinstance(cal, dict):
        return None
    raw = cal.get("Earnings Date")
    if raw is None:
        vals: list = []
    elif isinstance(raw, dict):
        vals = list(raw.values())
    elif isinstance(raw, (list, tuple)):
        vals = list(raw)
    else:
        vals = [raw]
    return sorted({d for d in (_iso(x) for x in vals) if d})


def observation(cal, *, now: Optional[datetime] = None) -> Dict:
    """ChronosBee 写进 `details[FIELD]` 的记录。`available=False` ⇒ 日历不可用（读者当「判不了」）。"""
    dates = parse_calendar(cal)
    return {"schema": SCHEMA, "available": dates is not None, "dates": dates or [],
            "fetched_at": (now or datetime.now()).isoformat(timespec="seconds")}


# ─────────────────────────────────────────────── 读

def from_swarm_entry(entry) -> Optional[Dict]:
    """一只票的蜂群结果 → 规范化观测 `{"dates", "kind", "fetched_at"}`；蜂失败 / 没有观测 ⇒ None。"""
    if not isinstance(entry, dict):
        return None
    agent = (entry.get("agent_details") or {}).get(_CHRONOS)
    if not isinstance(agent, dict) or agent.get("error"):
        return None
    det = agent.get("details")
    if not isinstance(det, dict):
        return None
    obs = det.get(FIELD)
    if isinstance(obs, dict) and obs.get("schema") == SCHEMA:
        if not obs.get("available"):
            return None
        dates = sorted({d for d in (_iso(x) for x in (obs.get("dates") or [])) if d})
        return {"dates": dates, "kind": "calendar", "fetched_at": obs.get("fetched_at")}
    cats = det.get("catalysts")
    if isinstance(cats, list) and cats:
        dates = sorted({d for d in (_iso(c.get("date")) for c in cats
                                    if isinstance(c, dict) and c.get("type") == "earnings") if d})
        return {"dates": dates, "kind": "legacy_catalysts", "fetched_at": None}
    return None


def resolve(obs: Optional[Dict], as_of, *, observed_on: Optional[str] = None) -> Optional[Dict]:
    """规范化观测 + as_of → `upcoming_fn` 的返回形状（见模块 docstring）。"""
    a = _iso(as_of)
    if obs is None or a is None:
        return None
    dates = obs.get("dates") or []
    base = {"earnings_time": "AMC",
            # ⚠️ 假定值不是观测：yfinance calendar 不给时段（同 earnings_watcher / v0.45.105）
            "earnings_time_assumed": True,
            "source": SOURCE_CALENDAR if obs.get("kind") == "calendar" else SOURCE_LEGACY,
            "observed_on": observed_on, "observed_fetched_at": obs.get("fetched_at")}
    nxt = next((d for d in dates if d >= a), None)
    if nxt is not None:
        return {**base, "earnings_date": nxt}
    if obs.get("kind") == "calendar" and dates:
        return {**base, "earnings_date": None, "reason": "only_past_dates"}
    # 日历里没有财报日（Yahoo 没排期 ≠ 视野内没有）/ 旧格式过滤过、分不清 ⇒ 判不了
    return None


def upcoming_fn_from_swarm(swarm_results: Optional[Dict], as_of) -> Callable[[str], Optional[Dict]]:
    """扫描内：用本轮内存里的蜂群结果（观测日 = as_of）。"""
    def _fn(ticker: str) -> Optional[Dict]:
        return resolve(from_swarm_entry((swarm_results or {}).get(ticker)), as_of, observed_on=str(as_of))
    return _fn


class _DiskObservations:
    """按日期读 `.swarm_results_<date>.json`，进程内按日期缓存（一份约 2 MB，不为每只票重读）。"""

    def __init__(self, home=None):
        self.home = Path(home) if home is not None else None
        self._cache: Dict[str, Optional[Dict]] = {}

    def results(self, day: str) -> Optional[Dict]:
        if day not in self._cache:
            root = self.home if self.home is not None else PATHS.home     # 调用时求值（测试会改数据根）
            path = root / f".swarm_results_{day}.json"
            data = None
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError) as exc:
                    # 坏文件 ⇒ 这一天当没有观测，但要看得见：读者会因此记 unknown / 显示「未知」
                    _log.warning("读 %s 失败，按该日无财报日观测处理: %s", path.name, exc)
            self._cache[day] = data if isinstance(data, dict) else None
        return self._cache[day]

    def latest(self, ticker: str, as_of, max_age_days: int) -> Optional[Tuple[str, Dict]]:
        a = _iso(as_of)
        if a is None:
            return None
        start = date.fromisoformat(a)
        for k in range(max(0, int(max_age_days)) + 1):
            day = (start - timedelta(days=k)).isoformat()
            obs = from_swarm_entry((self.results(day) or {}).get(ticker))
            if obs is not None:
                return day, obs
        return None


def latest_observation(ticker: str, as_of, *, max_age_days: int = 0, home=None) -> Optional[Tuple[str, Dict]]:
    """as_of 当天及往前 `max_age_days` 个日历日里，最近一份有该票有效观测的扫描 ⇒ (观测日, 观测)；没有 ⇒ None。"""
    return _DiskObservations(home).latest(ticker, as_of, max_age_days)


def upcoming_fn_from_disk(as_of, *, max_age_days: int = 0, home=None) -> Callable[[str], Optional[Dict]]:
    """CLI / 展示：读已落盘的观测。缺省只认 as_of 当天那次扫描（与扫描内同口径）。"""
    disk = _DiskObservations(home)

    def _fn(ticker: str) -> Optional[Dict]:
        hit = disk.latest(ticker, as_of, max_age_days)
        if hit is None:
            return None
        day, obs = hit
        return resolve(obs, as_of, observed_on=day)
    return _fn


# ─────────────────────────────────────────────── 观测点

def summarize(swarm_results: Optional[Dict], as_of) -> Dict:
    """本轮每只票的财报日能不能判 → `scan_timing.extra.earnings_dates` → alert_manager P2。

    `unresolved` = 判不了（卖权记 unknown、跨式不出信号）；`legacy_source` = 读的是旧格式催化剂
    ——v0.45.500 之后的扫描应恒为 0，非 0 说明 ChronosBee 没写观测字段（接线断了）。
    """
    fn = upcoming_fn_from_swarm(swarm_results, as_of)
    kinds: Counter = Counter()
    unresolved: List[str] = []
    legacy = 0
    for tk in sorted(swarm_results or {}):
        r = fn(tk)
        if r is None:
            kinds["unresolved"] += 1
            unresolved.append(tk)
            continue
        kinds["dated" if r.get("earnings_date") else "only_past_dates"] += 1
        if r.get("source") == SOURCE_LEGACY:
            legacy += 1
    return {"available": True, "n": sum(kinds.values()), "dated": kinds["dated"],
            "only_past_dates": kinds["only_past_dates"], "unresolved": kinds["unresolved"],
            "unresolved_tickers": unresolved, "legacy_source": legacy, "max_unresolved": MAX_UNRESOLVED}
