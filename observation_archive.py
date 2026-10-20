#!/usr/bin/env python3
"""
🐝 Alpha Hive — H1 原始观测档案（v0.45.568，buzz_v1 根治阶段 1）
=================================================================

为什么有它
----------
维度 IC 协议的 H1 检验冻结版 Buzz 评分 buzz_v1。冻结只挡住了「通道值 → 分」这一层；通道本身由生产代码从外部数据算出来，
而生产把**取数**（去 ApeWisdom / Massive / Yahoo / CNN 拿东西）和**解释**（把拿回来的东西算成通道分）写在同一段代码里、
只存解释之后的数字。于是「这次改动只动了解释、没动取数」只能靠人在登记边界时声明，算不出来——423/438 改了兜底日的取数、
按惯例只点名了分；输入层指纹守卫刻意不看取数层；10-12 整日重放时 Reddit 排名晚取约 12 小时，只能标「待验证」。

根治（用户 2026-10-20 定，四阶段）：① 本模块把喂 H1 情绪通道的原始观测在**取数那一刻**原样落档；② buzz_v1 向下冻结到
通道层，从档案重算（桥接期 ≥10 个扫描日逐位核对）；③ 守住取数链；④ 协议修订 3、执行器切换。之后生产怎么改 Buzz 都碰不到
buzz_v1，第 3 块原本要回答的「这条边界是不是只动了冻结层」不再需要人判。

本模块只做 ①：**只存、不改任何值**——四个取数模块与 BuzzBee 的输出逐位不变（`tests/test_observation_archive.py` 钉住）。
三个行情通道（动量 / 量比 / 波动率）是已全精度入档的三个标量的纯函数，阶段 1 不存日线。

存什么
------
- `payloads/<sha 前两位>/<sha>.json` = ``{"kind": …, "payload": …}``。sha = 这两项规范 JSON 的 sha256——**内容寻址**：同一份
  榜单无论被哪个标的、哪个进程用到，地址都相同，写过就不再写。payload 是取数模块在网络边界拿到的原始内容（Reddit 两张榜的
  results、新闻供应商返回的条目、热搜代码表、CNN 的 fear_and_greed 对象），外加重算要用的请求量（票、条数上限）——
  每种 kind 的形状见 `KINDS`。
- `events/<业务日>.jsonl`：每次真去取数（不含缓存命中）的结局——kind、成败、sha、请求参数（**去掉一切像密钥的键**，
  `_redact`）、时刻。取数失败也记（没有 payload）：以后判「主源失败、走了兜底」靠它。

溯源（为什么不能只在网络层抓）
------------------------------
四个模块各有 5–60 分钟的缓存，同一轮扫描里不同标的可能用到不同快照 ⇒ 光有「这一天取到过什么」不够，每个分数要能指出
**它用的是哪一份**。做法：取数模块把所用观测的地址放进返回结果的 `observation` 键（`REF_KEY`；派生缓存里一起存，缓存命中
也带着）；BuzzBee 在合成之后把四个通道的地址写进 ``details["observations"]``（`buzz_refs`）；`signal_archive.archive` 在写
`buzz.comp.*` 的同一事务里把它写进 `observation_links` 表（`link_rows` + `write_links`）——与通道值同一轮扫描、同一套覆盖语义
（同日重扫后者覆盖前者，两边一致）。

何时落盘
--------
只在扫描进程 `enable()` 之后（`alpha_hive_daily_report.main` 开扫前调）。其余进程（MCP、Alpha Bot、云端快照例程、测试）
照样算地址、**不写盘**：它们填的派生缓存若被随后的扫描用上，链接里就有地址、档案里却没有那份 payload——`summarize` 数成
`missing_payloads`、进 P2（缺了看得见，不会被当成「没缺」）。Reddit 榜单与热搜表的缓存本身就是原始内容 ⇒ 命中时
`reuse()` 顺手补档，这两种不会缺。

失败观测（「谁会红？」）
------------------------
本模块**绝不抛**进取数代码：取数照常、分数照常。写盘走 `ledger_io`（失败在它源头登记 ⇒ status.json ⇒ P2）；本模块另记
`stats()["failures"]`（含算不出地址——payload 不能规范化，如 NaN）。扫描收尾 `summarize()` 逐票核对「该有的链接 /
该在档的 payload」，结果进 `scan_timing.extra.observation_archive`，`alert_manager._check_observation_archive` 不全即 P2。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

#: 取数模块放所用观测地址的键（四个模块共用一个名字；Reddit 的值是 {榜名: sha}，其余是 sha）
REF_KEY = "observation"

#: 允许的观测种类 → payload 的形状（**这里是唯一登记处**；新增种类 = 改这里 + 阶段 2 的冻结通道代码）
KINDS: Dict[str, str] = {
    "apewisdom.ranking": '{"filter": 榜名, "results": ApeWisdom 第 1 页 results（约 100 行原样）}',
    "news.massive": '{"ticker", "symbol": 请求用的写法, "max_articles", "results": Massive results 原样}',
    "news.alpha_vantage": '{"ticker", "max_articles", "feed": AV feed 原样}',
    "news.yahoo_finance": '{"ticker", "max_articles", "news": Yahoo 搜索 news 原样}',
    "yahoo.trending": '{"symbols": 热搜代码表（按名次）}',
    "fear_greed.cnn": '{"fear_and_greed": CNN graphdata 的 fear_and_greed 对象原样}',
    "fear_greed.alternative_me": '{"data": alternative.me data[0] 原样（⚠️ 加密市场 F&G）}',
}

#: BuzzBee 四个情绪通道（`buzz_refs` / `summarize` 的键）
CHANNELS = ("reddit", "news", "yahoo", "fear_greed")
#: Reddit 通道每次都用这两张榜（`reddit_sentiment.get_ticker_sentiment`）
REDDIT_FILTERS = ("all-stocks", "wallstreetbets")
#: 链接表（`signal_archive.archive` 同一事务写；读者 = 阶段 2 的冻结通道层）
LINKS_TABLE = "observation_links"
CONSUMER_BUZZ = "BuzzBeeWhisper"

#: 请求参数里键名含这些片段的一律丢掉（AV / Twelve Data 的 key 在 URL 参数里）
_SECRET_HINTS = ("key", "token", "secret", "password", "auth", "credential", "signature")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MESSAGE_MAX = 300
_FAILURE_SAMPLES = 5

_lock = threading.RLock()
_state: Dict[str, Any] = {"enabled": False, "business_date": None, "mode": None}
_known: set = set()


def _fresh_stats() -> Dict[str, Any]:
    return {"payloads_written": 0, "payloads_present": 0, "events": 0, "failures": 0, "failure_samples": []}


_stats: Dict[str, Any] = _fresh_stats()


# ────────────────────────────────────────────────────────────────────────────
# 开关与统计
# ────────────────────────────────────────────────────────────────────────────

def enable(business_date: str, mode: str) -> None:
    """扫描进程开扫前调：此后本进程的观测落盘。`business_date` = 报告业务日（事件按它分文件），`mode` = live / backfill。"""
    dt.date.fromisoformat(business_date)          # 拼错日期在这里炸，不要静默写进一个怪名字的事件文件
    if mode not in ("live", "backfill"):
        raise ValueError(f"mode 只能是 live / backfill，收到 {mode!r}")
    with _lock:
        _state.update(enabled=True, business_date=business_date, mode=mode)


def disable() -> None:
    with _lock:
        _state.update(enabled=False, business_date=None, mode=None)


def is_enabled() -> bool:
    return bool(_state["enabled"])


def stats() -> Dict[str, Any]:
    """本进程的落盘统计（副本）。`failures` > 0 ⇒ `summarize` 判不全。"""
    with _lock:
        return {**_stats, "failure_samples": list(_stats["failure_samples"]),
                "enabled": _state["enabled"], "business_date": _state["business_date"], "mode": _state["mode"]}


def _reset_for_tests() -> None:
    with _lock:
        disable()
        _known.clear()
        _stats.clear()
        _stats.update(_fresh_stats())


def _fail(op: str, exc: BaseException) -> None:
    with _lock:
        _stats["failures"] += 1
        if len(_stats["failure_samples"]) < _FAILURE_SAMPLES:
            _stats["failure_samples"].append(f"{op}: {type(exc).__name__}: {str(exc)[:_MESSAGE_MAX]}")


# ────────────────────────────────────────────────────────────────────────────
# 地址与路径
# ────────────────────────────────────────────────────────────────────────────

def root() -> Path:
    """档案根目录，调用时求值（`PATHS.observation_archive`）。"""
    from hive_logger import PATHS
    return Path(PATHS.observation_archive)


def address(kind: str, payload: Any) -> str:
    """规范 JSON（键排序、紧凑、拒 NaN）的 sha256。不认识的 kind / 不能规范化的 payload ⇒ 抛。"""
    if kind not in KINDS:
        raise ValueError(f"未登记的观测种类 {kind!r}（登记处 observation_archive.KINDS）")
    blob = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_address(v: Any) -> bool:
    return isinstance(v, str) and bool(_SHA_RE.match(v))


def payload_path(sha: str, base: Optional[Path] = None) -> Path:
    if not is_address(sha):
        raise ValueError(f"不是观测地址：{sha!r}")
    return (Path(base) if base is not None else root()) / "payloads" / sha[:2] / f"{sha}.json"


def events_path(business_date: str, base: Optional[Path] = None) -> Path:
    return (Path(base) if base is not None else root()) / "events" / f"{business_date}.jsonl"


def _redact(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _redact(v) for k, v in obj.items()
                if not any(h in str(k).lower() for h in _SECRET_HINTS)}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    return str(obj)


# ────────────────────────────────────────────────────────────────────────────
# 取数模块调用的三个入口（都不抛）
# ────────────────────────────────────────────────────────────────────────────

def observe(kind: str, payload: Any, *, request: Optional[Mapping] = None) -> Optional[str]:
    """一次真去取数拿到了内容：返回它的地址；扫描进程里落档 payload（写过不重写）+ 记一条 `ok` 事件。

    算不出地址 ⇒ 返回 None（链接缺失，`summarize` 会数到）并计入 `stats()["failures"]`。
    """
    sha = _address_or_none(kind, payload)
    if sha is not None and is_enabled():
        _ensure_payload(kind, payload, sha)
        _event(kind, "ok", sha=sha, request=request)
    return sha


def reuse(kind: str, payload: Any) -> Optional[str]:
    """缓存命中时用的那份原始内容：返回地址；扫描进程里确保它在档（缓存可能是别的进程填的）。不记事件。"""
    sha = _address_or_none(kind, payload)
    if sha is not None and is_enabled():
        _ensure_payload(kind, payload, sha)
    return sha


def fetch_failed(kind: str, status: str, *, request: Optional[Mapping] = None,
                 message: Optional[str] = None) -> None:
    """一次真去取数没拿到内容（限速 / 熔断 / HTTP 错 / 空结果）：扫描进程里记一条事件，没有 payload。"""
    if kind not in KINDS:
        _fail("fetch_failed", ValueError(f"未登记的观测种类 {kind!r}"))
        return
    if is_enabled():
        _event(kind, str(status), sha=None, request=request, message=message)


def _address_or_none(kind: str, payload: Any) -> Optional[str]:
    try:
        return address(kind, payload)
    except Exception as e:  # noqa: BLE001 —— 本模块绝不抛进取数代码；失败计数、链接缺失由 summarize 报出
        _fail(f"address:{kind}", e)
        return None


def _ensure_payload(kind: str, payload: Any, sha: str) -> None:
    import ledger_io
    with _lock:
        if sha in _known:
            return
        try:
            path = payload_path(sha)
            if path.exists():
                _stats["payloads_present"] += 1
            else:
                ledger_io.write_json(path, {"kind": kind, "payload": payload}, indent=None, sort_keys=True)
                _stats["payloads_written"] += 1
            _known.add(sha)
        except Exception as e:  # noqa: BLE001 —— ledger_io 已在源头登记写失败；这里再计一次本模块的
            _fail(f"write_payload:{kind}", e)


def _event(kind: str, status: str, *, sha: Optional[str], request: Optional[Mapping],
           message: Optional[str] = None) -> None:
    import ledger_io
    rec: Dict[str, Any] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                           "mode": _state["mode"], "kind": kind, "status": status, "sha": sha}
    if request:
        rec["request"] = _redact(request)
    if message:
        rec["message"] = str(message)[:_MESSAGE_MAX]
    try:
        ledger_io.append_jsonl(events_path(_state["business_date"]), rec)
        with _lock:
            _stats["events"] += 1
    except Exception as e:  # noqa: BLE001
        _fail(f"event:{kind}", e)


# ────────────────────────────────────────────────────────────────────────────
# 读者（阶段 2 的冻结通道层、核对工具）
# ────────────────────────────────────────────────────────────────────────────

def load(sha: str, base: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """按地址取回 ``{"kind", "payload"}``；不在档 ⇒ None。内容与地址对不上 ⇒ 抛（档案被改过，不能拿来重算）。"""
    import ledger_io
    path = payload_path(sha, base)
    if not path.exists():
        return None
    doc = ledger_io.load_json(path)
    if address(doc.get("kind"), doc.get("payload")) != sha:
        raise ledger_io.LedgerCorrupt(f"{path} 的内容与地址不符——档案被改过")
    return doc


# ────────────────────────────────────────────────────────────────────────────
# BuzzBee 的溯源块与链接表
# ────────────────────────────────────────────────────────────────────────────

def _ref(d: Any) -> Optional[str]:
    v = d.get(REF_KEY) if isinstance(d, Mapping) else None
    return v if is_address(v) else None


def buzz_refs(*, reddit: Any, news: Any, yahoo: Any, fear_greed: Any) -> Dict[str, Dict[str, Any]]:
    """四个情绪通道各自用的观测：``{通道: {"expected": 该不该有, "ref": 地址或 None}}``。

    `expected`：Reddit = 拿到了结果（安静 / 兜底也是由那两张榜决定的，空榜也是一份观测）；其余三个 = 结果是真实数据
    （不是真实数据时通道就是中性 50，重算不需要观测）。`expected` 却拿不到地址 ⇒ `summarize` 计缺失。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(reddit, Mapping):
        r = reddit.get(REF_KEY)
        refs = {f: r.get(f) for f in REDDIT_FILTERS} if isinstance(r, Mapping) else None
        ok = refs is not None and all(is_address(v) for v in refs.values())
        out["reddit"] = {"expected": True, "ref": refs if ok else None}
    else:
        out["reddit"] = {"expected": False, "ref": None}
    for ch, d in (("news", news), ("yahoo", yahoo), ("fear_greed", fear_greed)):
        exp = bool(isinstance(d, Mapping) and d.get("is_real_data"))
        out[ch] = {"expected": exp, "ref": _ref(d) if exp else None}
    return out


def _buzz_details(tr: Any) -> Any:
    return (((tr or {}).get("agent_details") or {}).get(CONSUMER_BUZZ) or {}).get("details") \
        if isinstance(tr, Mapping) else None


def _shas(ch: str, ref: Any) -> Optional[List[str]]:
    """一个通道链接里的全部地址；不完整 ⇒ None。"""
    if ch == "reddit":
        if not isinstance(ref, Mapping):
            return None
        vals = [ref.get(f) for f in REDDIT_FILTERS]
        return vals if all(is_address(v) for v in vals) else None
    return [ref] if is_address(ref) else None


def ensure_links_schema(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {LINKS_TABLE} (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            consumer TEXT NOT NULL,
            refs TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, ticker, consumer)
        )
    """)


def write_links(conn: sqlite3.Connection, rows: List[tuple]) -> None:
    """在调用方的连接 / 事务里写链接（`signal_archive.archive` 与 buzz.comp.* 同一事务调它）。同键后者覆盖前者。"""
    if not rows:
        return
    ensure_links_schema(conn)
    conn.executemany(f"INSERT OR REPLACE INTO {LINKS_TABLE} (date,ticker,consumer,refs) VALUES (?,?,?,?)", rows)


def link_rows(swarm_results: Mapping, date: str) -> List[tuple]:
    """一次扫描要写进链接表的 (date, ticker, consumer, refs_json)。没有溯源块的标的（Buzz 出错 / 旧代码）不写。"""
    rows = []
    for ticker, tr in sorted((swarm_results or {}).items()):
        obs = (_buzz_details(tr) or {}).get("observations")
        if isinstance(obs, Mapping):
            rows.append((date, ticker, CONSUMER_BUZZ,
                         json.dumps(obs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))
    return rows


def load_links(db_path: Path, date: str, consumer: str = CONSUMER_BUZZ) -> Dict[str, Dict]:
    """某业务日的链接：``{ticker: 溯源块}``（只读；表不存在 ⇒ 空）。"""
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        try:
            rows = con.execute(f"SELECT ticker, refs FROM {LINKS_TABLE} WHERE date=? AND consumer=?",
                               (date, consumer)).fetchall()
        except sqlite3.OperationalError:
            return {}
    finally:
        con.close()
    return {t: json.loads(r) for t, r in rows}


# ────────────────────────────────────────────────────────────────────────────
# 扫描收尾的完整性核对
# ────────────────────────────────────────────────────────────────────────────

def summarize(swarm_results: Mapping, base: Optional[Path] = None) -> Dict[str, Any]:
    """逐票核对本轮的溯源是否完整 ⇒ `scan_timing.extra.observation_archive`（`alert_manager` 读）。

    `ok` 要同时满足：本进程已 `enable()`；每只有 Buzz 结果的票都带溯源块；该有观测的通道都有地址；地址指向的 payload
    都在档；本进程没有落盘失败。Buzz 出错的票（details 为空）没有分，不计缺失，单列 `n_buzz_error`。
    """
    missing_refs = {ch: 0 for ch in CHANNELS}
    expected = {ch: 0 for ch in CHANNELS}
    n_tickers = n_buzz_error = n_without_block = missing_payloads = 0
    examples: List[str] = []
    for ticker, tr in sorted((swarm_results or {}).items()):
        if not isinstance(tr, Mapping):
            continue
        n_tickers += 1
        det = _buzz_details(tr)
        if not det:
            n_buzz_error += 1
            continue
        obs = det.get("observations")
        if not isinstance(obs, Mapping):
            n_without_block += 1
            if len(examples) < _FAILURE_SAMPLES:
                examples.append(f"{ticker}: 无溯源块")
            continue
        for ch in CHANNELS:
            v = obs.get(ch) or {}
            if not v.get("expected"):
                continue
            expected[ch] += 1
            shas = _shas(ch, v.get("ref"))
            if shas is None:
                missing_refs[ch] += 1
                if len(examples) < _FAILURE_SAMPLES:
                    examples.append(f"{ticker}: {ch} 无地址")
                continue
            for s in shas:
                if not payload_path(s, base).exists():
                    missing_payloads += 1
                    if len(examples) < _FAILURE_SAMPLES:
                        examples.append(f"{ticker}: {ch} 的 {s[:12]}… 不在档")
    st = stats()
    ok = (st["enabled"] and n_without_block == 0 and not any(missing_refs.values())
          and missing_payloads == 0 and st["failures"] == 0)
    return {
        "available": True, "ok": bool(ok), "enabled": st["enabled"], "mode": st["mode"],
        "n_tickers": n_tickers, "n_buzz_error": n_buzz_error, "n_without_block": n_without_block,
        "expected": expected, "missing_refs": missing_refs, "missing_payloads": missing_payloads,
        "payloads_written": st["payloads_written"], "payloads_present": st["payloads_present"],
        "events": st["events"], "failures": st["failures"], "failure_samples": st["failure_samples"],
        "examples": examples,
    }
