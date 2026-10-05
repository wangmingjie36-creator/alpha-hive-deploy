"""回放行情库（v0.45.410）：已落定的日线只向 Yahoo 取一次，之后从本地读。

为什么有它
----------
F&G 敞口门前瞻检验（`experiments/fg_exposure_gate_forward_test.py`）每次运行都从冻结种子把整个前瞻窗口重放
一遍——这是自证必须的（每次都用**当前**代码从头复现，才测得出代码漂移）。重放本身很便宜（10-03 实测：连续 A、
连续 B、11 段逐日重放合计约 0.2s），贵的是行情：`paper_portfolio.replay_ohlc_window`（v0.45.391）每个标的整段
向 Yahoo 取一次，22 个标的串行 ≈ 13s，占 `run()` 的 97%。而那些日线大多是几周前的、早已不会再变。

更要紧的是降级路径：Yahoo 拒绝整段请求时，该标的退回逐次直连，调用次数**与快照天数成正比**（10-03 实测
182 次 / 11 个快照日 ≈ 每天 16.5 次；到终期约 150 个快照日 ≈ 2,500 次）。Step 11 给 F&G 的预算是 45s，
降级日越往后越不可能跑完——加大预算、并行下载、快速失败都只是推迟或掩盖这件事。根因是「每次运行都把
不会再变的历史重新下载一遍」，所以修法是让历史只下载一次。

怎么用
------
`paper_portfolio.replay_ohlc_window(start, end, store=ReplayOhlcStore(PATHS.replay_ohlc_state))`。窗口第一次
用到某个标的时：
  · 库里的已落定段从窗口左端起覆盖到 `settled.end` ⇒ 只补下 `[settled.end − OVERLAP_DAYS + 1, 窗口右端)`；
    覆盖到窗口右端 ⇒ 不打网络；
  · 否则（新标的 / 首次运行 / 窗口左端比库更早）⇒ 整段下载，与 v0.45.391 完全相同。
  · 本次窗口对该标的的回答 = 已落定段（库里的值）∪ 这次下载里已落定段之外的部分。
  · 请求整个落在已落定段内 ⇒ 直接由库回答、不触发下载；只有碰到未落定日子的请求才让这个标的下载（补尾）
    ⇒ 只在早几周持有过的标的整次检验都不打网络，每天的下载数只与最近几天的持仓有关。
  · 下载失败（该标的退回逐次直连）时，完全落在已落定段内的请求照样由库回答，只有碰到未落定日子的请求才直连
    ⇒ 降级时的直连次数只与最近几天的持仓有关，不再随快照天数增长。

落定规则（用户 2026-10-04 定）
------------------------------
某一天 d 的日线（含「这天没有日线」）**落定** = 两次在不同美东日期做的下载都覆盖 d、给出逐字段相同的结果，
且 d 早于较早那次下载的美东日期。
  · 「早于较早那次下载的日期」：两次下载时那个交易日都已收完盘——未来的日子在两次下载里都「没有日线」，
    这一条防止把它们误判成「确认没有」；
  · 「两次、不同日期」：编排器在 14:00 PDT（收盘后一小时）跑，当天日线可能还是临时值；Yahoo 偶尔一次响应漏一天
    （v0.45.383/387）。按「满 N 天就落定」会把那次漏掉的响应永久冻进库，要求两次一致则两种情况都挡住。
已落定段是一个连续的日历区间 `[start, end]`，区间内没有日线的日子是确认过的休市日。

已落定之后 Yahoo 又改了（用户 2026-10-04 定）
---------------------------------------------
每次补下载与已落定段重叠 `OVERLAP_DAYS` 天。重叠部分与库里不同 ⇒ **沿用库里的值**（回答永远是库里的值）。
「Yahoo 改了」同落定一样要两次**不同美东日期**的观察给出同一个新值才算（二次检查：单次响应漏一天曾被永久记成
修订）：第一次只记进 `revision_suspects`，之后看到同一个新值 ⇒ 记进 `revisions`（first_seen = 第一次看到那天）、
随结果报出，**只要窗口里有就每次都报**；看到库里的值 ⇒ 撤销嫌疑。补尾起点会退回去盖住还没确认的嫌疑。
理由：拆股后 Yahoo 会回溯复权历史，生产当时看到的是复权前的价格；冻结的库更接近生产当时的输入，A 与 B 读同一份，
两者之差不受影响。重叠之外更早的修订看不见——那是「首次落定后冻结」的本意。

⚠️ 已知局限（二次检查实测）：拆股时**还没落定**的最近 1~3 天，会在拆股后由两次复权后的下载确认、按复权价落定 ⇒
库里在「已落定段末端」与这几天之间出现一个假跳空（不在拆股日、生产也没见过），跨这几天持仓的 A / B 都可能因此假止损。
要彻底避免得按「运行日」存多份视图（时点数据），本版不做。拆股会以修订的形式出现在进度行里——看到就要人看。
不用库时更糟：重新下载拿到全段复权价，拆股前每一笔仓位的历史都会被改写。

停牌 / 退市
-----------
补尾拿到空结果、而已落定段在同一段里本来就没有日线 ⇒ 空是权威答案（照常落定、不降级）；库里那段有日线时
空结果仍当下载失败（Yahoo 抽风的形状，v0.45.391 起就不把它当权威答案）。

坏文件 / 写不进去
-----------------
读不懂的文件 ⇒ 改名为 `<原名>.invalid-<美东日期>-<序号>` 留证、本次当作没有库（整段下载、重新建），计数报出；
改名也失败 ⇒ 本次不写这个标的（不覆盖证据）。写入失败 ⇒ 本次结果不受影响（已下载的照用），计数报出——
否则下次照样整段下载、慢回去，却没人知道为什么。两者都让 `ohlc_window.degraded` 为真（谁会红：进度行段首 ⚠️ +
attention `ic_rerun.fg_exposure_gate_forward.ohlc_window_degraded`）。

写入
----
每个标的一个文件 `<TICKER>.json`，下载完立即原子写（同目录临时文件 + `os.replace`）——Step 11 到点被杀时，
已写的标的照样有效，下次接着补，不会每次都从零开始。并发的两次运行（Step 11 与每周只读诊断任务）各自
从同一份旧状态推出的结果都合法，后写者胜，不会写出坏文件。

只有前瞻检验的 `run()` 用它；`--rehearse` / `--insample` 照旧现场下载（用户 2026-10-04 定）。
目录由 `hive_logger.PATHS.replay_ohlc_state` 调用时求值，进数据备份（`data_backup/export.py` 的 `STATE_DIRS`）。
文件里只有价格、没有任何净值 / 收益 / 效应量；`stats()` 只有计数与日期，不含价格（盲化不受影响）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

_log = logging.getLogger("alpha_hive.replay_ohlc_store")

SCHEMA = 1
#: 补下载与已落定段重叠的日历天数（顺带核对已落定的日线有没有被 Yahoo 改过）。补下载每个标的只是一次请求，
#: 多几天几乎不加时间；太短则拆股等回溯修订更容易落在核对范围之外。
OVERLAP_DAYS = 10
#: 确认修订后这么多天内进度行段首 ⚠️ + attention（二次检查：两位审查者都实测了拆股会让 A / B 凭空出场，只陈述不够）；
#: 之后只陈述不换图标——窗口左端固定，修订会一直在窗口里，永久 ⚠️ 会把人训练成无视 ⚠️。7 天保证每周诊断任务至少看到一次。
REVISION_ALARM_DAYS = 7
#: 合理日期范围：防 0001-01-01 / 9999-12-31 之类在日期加减时溢出（二次检查）
_DATE_LO, _DATE_HI = "2000-01-01", "2099-12-31"
_BAR_KEYS = ("Open", "High", "Low", "Close")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
#: 文件名安全的标的代码（BRK-B、^VIX 之类都在内）；不符合的标的不进库，照旧整段下载
_TICKER = re.compile(r"[A-Za-z0-9^][A-Za-z0-9.\-^=]{0,19}\Z")


def _et_today() -> dt.date:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York")).date()


def _iso(d: dt.date) -> str:
    return d.isoformat()


def _day(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _shift(s: str, days: int) -> str:
    return _iso(_day(s) + dt.timedelta(days=days))


def _days(start: str, end_exclusive: str) -> List[str]:
    """[start, end) 的全部日历日（含周末 / 假日——「这天没有日线」也是要落定的事实）。"""
    out, d, e = [], _day(start), _day(end_exclusive)
    while d < e:
        out.append(_iso(d))
        d += dt.timedelta(days=1)
    return out


class StoreFileInvalid(ValueError):
    pass


def _check_bar(v) -> None:
    if not (isinstance(v, dict) and set(v) == set(_BAR_KEYS)):
        raise StoreFileInvalid(f"日线不是 {{{', '.join(_BAR_KEYS)}}}：{v!r:.80}")
    for k in _BAR_KEYS:
        x = v[k]
        try:
            finite = not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)
        except OverflowError:   # 400 位的整数（二次检查实测）
            finite = False
        if not finite:
            raise StoreFileInvalid(f"日线字段 {k} 不是有限数：{x!r:.40}")


def _check_bars(bars, lo: str, hi_exclusive: str, what: str) -> None:
    if not isinstance(bars, dict):
        raise StoreFileInvalid(f"{what}.bars 不是对象")
    for d, v in bars.items():
        _check_date(d, f"{what}.bars 的日期")   # 二次检查：「2026-09-31」能过正则与区间比较，之后在 strptime 里崩
        if not lo <= d < hi_exclusive:
            raise StoreFileInvalid(f"{what}.bars 的日期 {d!r} 不在 [{lo}, {hi_exclusive}) 内")
        _check_bar(v)


def _check_date(v, what: str) -> str:
    if not (isinstance(v, str) and _ISO.match(v)):
        raise StoreFileInvalid(f"{what} 不是 YYYY-MM-DD：{v!r}")
    _day(v)   # 2026-02-30 之类 ⇒ ValueError ⇒ 由调用方按坏文件处理
    if not _DATE_LO <= v <= _DATE_HI:
        raise StoreFileInvalid(f"{what} 超出 [{_DATE_LO}, {_DATE_HI}]：{v}")
    return v


def validate_entry(entry, ticker: str) -> Dict:
    """库文件的完整校验（读进来就校验，不信任磁盘）。不合法 ⇒ `StoreFileInvalid` / `ValueError`。"""
    if not isinstance(entry, dict) or entry.get("schema") != SCHEMA or entry.get("ticker") != ticker:
        raise StoreFileInvalid(f"schema / ticker 不对（要 schema={SCHEMA}、ticker={ticker}）")
    # 二次检查：只用 .get 校验时缺键也能过，之后在 merge 里 KeyError——检验每天崩、文件却从不被改名留证
    missing = {"settled", "pending"} - set(entry)
    if missing:
        raise StoreFileInvalid(f"缺键 {sorted(missing)}")
    s = entry.get("settled")
    if s is not None:
        if not isinstance(s, dict):
            raise StoreFileInvalid("settled 不是对象")
        lo, hi = _check_date(s.get("start"), "settled.start"), _check_date(s.get("end"), "settled.end")
        if lo > hi:
            raise StoreFileInvalid(f"settled 区间倒置：{lo} > {hi}")
        _check_bars(s.get("bars"), lo, _shift(hi, 1), "settled")
    p = entry.get("pending")
    if p is not None:
        if not isinstance(p, dict):
            raise StoreFileInvalid("pending 不是对象")
        _check_date(p.get("fetched_on"), "pending.fetched_on")
        lo, hi = _check_date(p.get("start"), "pending.start"), _check_date(p.get("end"), "pending.end")
        if lo >= hi:
            raise StoreFileInvalid(f"pending 是空区间：[{lo}, {hi})")
        _check_bars(p.get("bars"), lo, hi, "pending")
    rv = entry.get("revisions", {})
    if not isinstance(rv, dict):
        raise StoreFileInvalid("revisions 不是对象")
    for d, r in rv.items():
        _check_date(d, "revisions 的日期")
        if s is None or not (s["start"] <= d <= s["end"]):
            raise StoreFileInvalid(f"revisions 记了已落定段之外的日期 {d}")
        if not isinstance(r, dict):
            raise StoreFileInvalid(f"revisions[{d}] 不是对象")
        _check_date(r.get("first_seen"), f"revisions[{d}].first_seen")
        if r.get("confirmed_on") is not None:
            _check_date(r["confirmed_on"], f"revisions[{d}].confirmed_on")
        if r.get("yahoo") is not None:
            _check_bar(r["yahoo"])
    sus = entry.get("revision_suspects", {})
    if not isinstance(sus, dict):
        raise StoreFileInvalid("revision_suspects 不是对象")
    for d, r in sus.items():
        _check_date(d, "revision_suspects 的日期")
        if s is None or not (s["start"] <= d <= s["end"]) or not isinstance(r, dict):
            raise StoreFileInvalid(f"revision_suspects[{d}] 不合法")
        _check_date(r.get("seen_on"), f"revision_suspects[{d}].seen_on")
        if r.get("yahoo") is not None:
            _check_bar(r["yahoo"])
    return entry


class ReplayOhlcStore:
    """见模块 docstring。一次检验构造一个（`fetch_day` 在构造时定下，同一次运行内不变）。"""

    def __init__(self, root: Path, *, clock: Optional[Callable[[], dt.date]] = None):
        self.root = Path(root)
        self.fetch_day = _iso((clock or _et_today)())
        self._entries: Dict[str, Optional[Dict]] = {}
        self._no_write: set = set()
        # 计数（`stats()`；只有次数、日期与文件名，没有价格）
        self.store_only = 0          # 全部由库回答、没打网络的标的数
        self.tail_fetches = 0        # 只补下最近一段的标的数
        self.full_fetches = 0        # 整段下载的标的数（新标的 / 首次 / 窗口左端比库早）
        self.settled_days_added = 0  # 本次新落定的日历日数
        self.revisions_new = 0       # 本次首次发现的「已落定后被 Yahoo 改了」的日线数
        self.served_from_store = 0   # 整个落在已落定段内、没触发下载就由库回答的请求数
        self.served_on_fallback = 0  # 下载失败后仍由已落定段回答的请求数
        self.invalid_files: List[str] = []
        self.write_errors: List[str] = []

    # ── 读 ─────────────────────────────────────────────────────────────────────

    def supports(self, ticker) -> bool:
        return isinstance(ticker, str) and bool(_TICKER.match(ticker))

    def _path(self, ticker: str) -> Path:
        return self.root / f"{ticker}.json"

    def _entry(self, ticker: str) -> Optional[Dict]:
        if ticker in self._entries:
            return self._entries[ticker]
        path = self._path(ticker)
        entry = None
        try:
            # 二次检查：`path.exists()` 对 EACCES 之类会直接抛（只吞 ENOENT 等）——放进 try，读不了就按坏文件处理
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):   # 没有这个文件（库目录还没建 / 被一个文件占了名）
            raw = None
        except (OSError, ValueError) as e:   # 权限 / 是目录 / UnicodeDecodeError（ValueError 子类）
            self._quarantine(ticker, path, e)
            raw = None
        if raw is not None:
            try:
                entry = validate_entry(json.loads(raw), ticker)
            except Exception as e:  # noqa: BLE001 —— 磁盘上的任何畸形都改名留证、本次整段下载，不许让检验崩（二次检查）
                self._quarantine(ticker, path, e)
                entry = None
        self._entries[ticker] = entry
        return entry

    def _quarantine(self, ticker: str, path: Path, err: Exception) -> None:
        try:
            n = 0
            while True:   # 库目录没权限时 `exists()` 也会抛——整段放进 try（二次检查实测）
                dst = path.with_name(f"{path.name}.invalid-{self.fetch_day}-{n}")
                if not dst.exists():
                    break
                n += 1
            os.replace(path, dst)
            self.invalid_files.append(f"{path.name} → {dst.name}（{type(err).__name__}: {str(err)[:120]}）")
        except OSError as e2:
            self._no_write.add(ticker)   # 移不开就不写：不覆盖证据
            self.invalid_files.append(f"{path.name}（{type(err).__name__}: {str(err)[:120]}；移开失败 {e2}，本次不写）")
        _log.warning("[ReplayOhlcStore] 库文件 %s 读不懂（%s: %s）——本次当作没有库、整段下载",
                     path, type(err).__name__, err)

    def fetch_start(self, ticker: str, wstart: str, wend: str) -> Optional[str]:
        """这个标的要从哪天开始下载（到 `wend`）。`None` ⇒ 已落定段覆盖整个 [wstart, wend)，不打网络。"""
        e = self._entry(ticker)
        s = e.get("settled") if e else None
        if not s or s["start"] > wstart:
            return wstart
        if s["end"] >= _shift(wend, -1):
            return None
        lo = _shift(s["end"], -(OVERLAP_DAYS - 1))
        sus = e.get("revision_suspects") or {}
        if sus:   # 二次检查：修订要两次观察，补尾得盖住还没确认的那几天
            lo = min(lo, min(sus))
        return max(wstart, lo)

    def settled_quiet_from(self, ticker: str, start: str) -> bool:
        """已落定段在 [start, settled.end] 里一根日线都没有（停牌 / 退市）——这时补尾拿到空结果与库一致，
        是权威答案而不是下载失败（二次检查：否则这类标的每天降级、直连次数逐日增长，落定段永远不前进）。"""
        e = self._entry(ticker)
        s = e.get("settled") if e else None
        return bool(s) and s["start"] <= start <= s["end"] and not any(start <= d for d in s["bars"])

    def settled_slice(self, ticker: str, start: str, end: str, *, on_fallback: bool = False) -> Optional[Dict[str, Dict]]:
        """请求 [start, end) 整个落在已落定段内 ⇒ 由库回答；否则 `None`（调用方走原路径）。
        `on_fallback`：这个标的这次下载已失败（只影响计数）。"""
        if not self.supports(ticker):
            return None
        e = self._entry(ticker)
        s = e.get("settled") if e else None
        if not s or not (s["start"] <= start and _shift(end, -1) <= s["end"]):
            return None
        if on_fallback:
            self.served_on_fallback += 1
        else:
            self.served_from_store += 1
        return {d: dict(b) for d, b in s["bars"].items() if start <= d < end}

    # ── 合并一次下载 ────────────────────────────────────────────────────────────

    def merge(self, ticker: str, wstart: str, wend: str, fstart: Optional[str],
              bars: Optional[Dict[str, Dict]]) -> Dict[str, Dict]:
        """把这次下载（`[fstart, wend)` 的 `bars`；`fstart=None` ⇒ 没下载）并进库、立即写盘，返回本次窗口
        对该标的的权威回答（覆盖 `[wstart, wend)`）。写盘失败只计数，不影响返回值。"""
        old = self._entry(ticker)
        entry = json.loads(json.dumps(old)) if old else {"schema": SCHEMA, "ticker": ticker, "settled": None,
                                                         "pending": None, "revisions": {}, "revision_suspects": {}}
        entry.setdefault("revisions", {})
        entry.setdefault("revision_suspects", {})
        if fstart is None:
            self.store_only += 1
        else:
            if fstart == wstart:
                self.full_fetches += 1
            else:
                self.tail_fetches += 1
            self._absorb(entry, fstart, wend, bars or {})
            self._write(ticker, entry)
            self._entries[ticker] = entry
        s = entry["settled"]
        out: Dict[str, Dict] = {}
        if fstart is not None:
            for d, b in (bars or {}).items():
                if wstart <= d < wend and not (s and s["start"] <= d <= s["end"]):
                    out[d] = dict(b)
        if s:
            for d, b in s["bars"].items():
                if wstart <= d < wend:
                    out[d] = dict(b)
        return out

    def _absorb(self, entry: Dict, fstart: str, fend: str, bars: Dict[str, Dict]) -> None:
        today = self.fetch_day
        s = entry["settled"]
        # ① 与已落定段重叠的部分：与库里不同 ⇒ 沿用库里的值。「Yahoo 改了」同落定一样要两次**不同美东日期**的观察
        #    给出同一个新值才算（二次检查：单次响应漏一天曾被永久记成修订，进度行整个检验期都在误报）——第一次只记
        #    嫌疑；之后看到同一个新值 ⇒ 记修订（first_seen = 第一次看到那天）；看到库里的值 ⇒ 撤销嫌疑。
        if s:
            sus = entry["revision_suspects"]
            for d in _days(max(fstart, s["start"]), min(fend, _shift(s["end"], 1))):
                v = bars.get(d)
                if v == s["bars"].get(d):
                    sus.pop(d, None)
                    continue
                if d in entry["revisions"]:
                    continue
                prev = sus.get(d)
                if prev and prev["yahoo"] == v and prev["seen_on"] < today:
                    entry["revisions"][d] = {"first_seen": prev["seen_on"], "confirmed_on": today, "yahoo": v}
                    del sus[d]
                    self.revisions_new += 1
                elif not (prev and prev["yahoo"] == v):   # 同一天再看到同一个值不算第二次
                    sus[d] = {"seen_on": today, "yahoo": v}
        # ② 与上一次（不同美东日期的）下载逐日核对，一致的、两次下载时都已收盘的日子并进已落定段
        p = entry["pending"]
        if p and p["fetched_on"] < today:
            c_lo, c_hi = max(p["start"], fstart), min(p["end"], fend, p["fetched_on"])   # c_hi 排他
            agreed = {d for d in (_days(c_lo, c_hi) if c_lo < c_hi else ())
                      if bars.get(d) == p["bars"].get(d)}
            if s:
                lo, hi = s["start"], s["end"]
            elif c_lo in agreed:
                lo = hi = c_lo
            else:
                lo = hi = None
            if lo is not None:
                while _shift(lo, -1) in agreed:
                    lo = _shift(lo, -1)
                while _shift(hi, 1) in agreed:
                    hi = _shift(hi, 1)
                new_bars = dict(s["bars"]) if s else {}
                added = 0
                for d in _days(lo, _shift(hi, 1)):
                    if s and s["start"] <= d <= s["end"]:
                        continue
                    added += 1
                    if d in bars:
                        new_bars[d] = dict(bars[d])
                if added or not s:
                    entry["settled"] = {"start": lo, "end": hi, "bars": dict(sorted(new_bars.items()))}
                    self.settled_days_added += added
        # ③ 这次下载成为下一次核对的对象（同一天重跑 ⇒ 换成更新的这次；时钟倒退 ⇒ 不动）
        if not p or p["fetched_on"] <= today:
            entry["pending"] = {"fetched_on": today, "start": fstart, "end": fend,
                                "bars": {d: dict(b) for d, b in sorted(bars.items()) if fstart <= d < fend}}

    def _write(self, ticker: str, entry: Dict) -> None:
        if ticker in self._no_write:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{ticker}.", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(entry, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                os.replace(tmp, self._path(ticker))
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, TypeError, ValueError) as e:
            self.write_errors.append(f"{ticker}: {type(e).__name__}: {str(e)[:120]}")
            _log.warning("[ReplayOhlcStore] 写 %s 失败（%s: %s）——本次结果不受影响，下次仍要整段下载",
                         self._path(ticker), type(e).__name__, e)

    # ── 报告 ───────────────────────────────────────────────────────────────────

    def quarantined(self) -> List[str]:
        """目录里还留着的改名留证坏文件（含以前各次运行留下的）。二次检查：坏文件原先只在改名那一次报，那次运行若被
        预算看门狗杀掉就一次都不报，之后该标的的冻结历史按当时的 Yahoo 重建、没人知道——现在留证文件在就一直报，
        人看过后删掉才消。"""
        try:
            return sorted(p.name for p in self.root.glob("*.json.invalid-*"))
        except OSError:
            return []

    def problem(self) -> bool:
        return bool(self.invalid_files or self.write_errors or self.quarantined())

    def revised_in(self, wstart: Optional[str] = None, wend: Optional[str] = None, *,
                   since: Optional[str] = None) -> Dict[str, int]:
        """本次运行**碰过**的标的（含只由库回答、没下载的）里，日期落在 [wstart, wend) 的已记修订数。
        二次检查补：原先只在 `merge()` 里数——标的平仓后请求全落在已落定段内、不再下载，修订就从进度行消失，
        而重放照旧用着冻结值（「只要窗口里有就每次都报」没做到）。"""
        out = {}
        for t, e in self._entries.items():
            rv = (e or {}).get("revisions") or {}
            n = sum(1 for d, r in rv.items()
                    if (wstart is None or wstart <= d) and (wend is None or d < wend)
                    and (since is None or (r.get("confirmed_on") or "") >= since))
            if n:
                out[t] = n
        return out

    def stats(self, wstart: Optional[str] = None, wend: Optional[str] = None) -> Dict:
        """`wstart` / `wend`：窗口范围（`_ReplayOhlcWindow.stats()` 传），只数窗口内的修订。"""
        revised = self.revised_in(wstart, wend)
        recent = self.revised_in(wstart, wend, since=_shift(self.fetch_day, -REVISION_ALARM_DAYS))
        return {
            "fetch_day": self.fetch_day,
            "store_only": self.store_only,
            "tail_fetches": self.tail_fetches,
            "full_fetches": self.full_fetches,
            "settled_days_added": self.settled_days_added,
            "served_from_store": self.served_from_store,
            "served_on_fallback": self.served_on_fallback,
            "revisions_new": self.revisions_new,
            "revised_bars": sum(revised.values()),
            "revised_tickers": sorted(revised),
            "revised_recent": sum(recent.values()),
            "revised_recent_tickers": sorted(recent),
            "revision_alarm_days": REVISION_ALARM_DAYS,
            "quarantined": self.quarantined(),
            "invalid_files": list(self.invalid_files),
            "write_errors": list(self.write_errors),
            "problem": self.problem(),
        }

    def summary(self, wstart: Optional[str] = None, wend: Optional[str] = None) -> str:
        st = self.stats(wstart, wend)
        return (f"行情库：整段 {st['full_fetches']}、补尾 {st['tail_fetches']}、免下载 {st['store_only']} 个标的，"
                f"新落定 {st['settled_days_added']} 天，库直接回答 {st['served_from_store']} 次、"
                f"下载失败后由库回答 {st['served_on_fallback']} 次，"
                f"修订 {st['revised_bars']} 根（本次新发现 {st['revisions_new']}），"
                f"坏文件 {len(st['invalid_files'])}、写入失败 {len(st['write_errors'])}")
