#!/usr/bin/env python3
"""
CBOE VIX 历史（v0.43.24 Step 2）
================================
VIX 现货与历史收盘，直接取自 CBOE 官方 CSV：

    https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv

为什么不继续用 yfinance
----------------------
`fred_macro` 原先直接调 yfinance 拿 ^VIX，**绕过了项目既定的 CBOE 优先链**
（CLAUDE.md：CBOE → yfinance → AV → Finnhub）。宏观数据在 30 只标的扫完之后才
抓，配额早被耗尽——实测 2026-08-14 全天 363 条 Too Many Requests，7 个宏观标的
全军覆没，于是整体降级到 base 常量 `vix=20.0`。88 个扫描日里 13 天如此，
2026 年 8 月的 9 个扫描日里占 5 天，且在恶化。

CBOE 这个 CSV 无 key、无限流、无并发配额，且**自带 1990 年至今的完整历史**
（9000+ 行），顺带解决了 252 日分位没有真实样本的问题。

诚实降级
--------
网络失败时优先用磁盘缓存（并如实返回缓存日期，调用方可自行判断新鲜度）；
缓存也没有则返回 `None` —— **绝不返回猜测值**。这正是本次事故的教训：
`fred_macro` 的 `vix=20.0` 是个合法 float，不会崩，只会一路冒充观测值。

当日收盘：延迟报价优先（v0.45.357）
----------------------------------
上面这份 CSV **要到美东约 20:30 才追加当日一行**（09-25 那行的 `Last-Modified`
= 20:30:54 ET），而日报扫描在 17:00 ET 跑 ⇒ 自 v0.43.24 改走 CBOE 起，日报里的
`vix` 系统性是**上一交易日**收盘（19 份 CBOE 日报里 12 份落后一场、2 份落后两三场；
v0.43.24 之前走 yfinance 时 65/65 是当日）。滞后值经 GuardBee 的 `vix<15` 票进评分，
按归档的票数重放：293 行里 38 行 Guard 宏观政体会不同。

所以 `get_vix_observation()`（`fred_macro` 的唯一入口）先试同源的延迟报价
`delayed_quotes/quotes/_VIX.json`，但**只在收盘后**采用：VIX 16:15 ET 停算
（提前收盘日 13:15），报价自带的 `last_trade_time` 必须落在 [收盘, 收盘+30min]、
日期是今天 —— 盘中 / 盘前 / CDN 发来的盘中陈旧文件一律拒收、退回 CSV，
**绝不拿盘中值冒充收盘**。拒收原因原样带出（`quote_reason`），不静默。

自动核对：采用过的报价收盘记进账本（`PATHS.cache_dir/vix_quote_ledger.json`），
次日 CSV 公布那一场之后逐条比对；对不上 ⇒ WARNING + 14 天内停用报价（fail-closed，
退回 CSV），并如实写进 `quote_check`。同一次运行里还拿报价的 `prev_day_close`
比 CSV 的上一场收盘，不一致当场拒收。
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import urllib.request
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.cboe_vix")
except Exception:  # pragma: no cover - 叶子模块降级
    import logging
    _log = logging.getLogger("alpha_hive.cboe_vix")

try:
    # 复用 cboe_options 的信号量：共用同一把锁，才能真正串行化所有 CBOE 请求，
    # 不给对端限流器加压。（⚠️ 原注「本机老 SSL 栈扛不住并发」的归因
    # 2026-08-25 已证伪，见 http_gate docstring。）
    from cboe_options import _CBOE_SEM
except Exception:  # pragma: no cover
    import threading
    _CBOE_SEM = threading.Semaphore(1)

_VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
# v0.45.160：`_CACHE_PATH` 现在是**覆盖钩子**，默认 `None` ⇒ 运行时解析 `PATHS.*`。
# 原本是 `Path(__file__).parent / "cache" / "vix_history_cboe.csv"` —— 它**压根不读任何环境变量**，
# 比「模块级常量冻在 import 期」更彻底：`ALPHA_HIVE_HOME` / `ALPHA_HIVE_CACHE_DIR`
# 设成什么都无效，`tests/conftest.py::_isolate_env` 对它完全无效。
# 保留这个名字是因为 `tests/` 有 `monkeypatch.setattr(<mod>, "_CACHE_PATH", ...)` 依赖它。
_CACHE_PATH = None


def _cache_path() -> Path:
    """本模块的缓存落点。**调用时求值**（别求值成模块级常量或默认参数）。"""
    if _CACHE_PATH is not None:
        return Path(_CACHE_PATH)
    from hive_logger import PATHS
    return Path(PATHS.cache_dir) / "vix_history_cboe.csv"
_CACHE_TTL = 6 * 3600           # 6 小时：日频数据，一天扫一次绰绰有余
_NET_TIMEOUT = 15.0

# VIX 合理区间：史上最低 8.56（2017）、最高 82.69（2020-03）。超出即视为解析错误。
_MIN_VALID_VIX = 5.0
_MAX_VALID_VIX = 150.0

# 标准 IV/VIX Rank 窗口
VIX_PERCENTILE_WINDOW = 252

# ── 当日收盘：延迟报价（v0.45.357，见模块 docstring）──
_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"
# VIX 比股票收盘晚 15 分钟停算（16:15 ET；提前收盘日 13:15）。钟没过这一刻，报价不是收盘。
_VIX_CALC_TAIL = timedelta(minutes=15)
# 报价的 last_trade_time 必须落在 [交易所收盘, 收盘+30min]：早于它 = CDN 发的盘中陈旧文件
# （cboe_options v0.45.234 实测过同形）；晚于它 = 已不是这一场的收盘值，一律拒收。
_QUOTE_LTT_WINDOW = timedelta(minutes=30)
# 覆盖钩子（同 `_CACHE_PATH`），默认 None ⇒ 调用时解析 `PATHS.cache_dir`。
_LEDGER_PATH = None
_LEDGER_KEEP = 60                  # 只留最近 60 场
_LEDGER_TOL = 0.005                # 两位小数的收盘，差半分即不符
_MISMATCH_LOOKBACK_DAYS = 14       # 这段日历天内出现过不符 ⇒ 停用报价（fail-closed）

# `get_vix_history` 最近一次在**本线程**里是怎么拿到数据的：
# "download" / "cache_fresh" / "cache_stale"（下载失败、退回过期缓存）/ "none"。
# 放线程局部而不是改返回值：`get_vix_history` 的签名被测试与 `cboe_fetcher` 依赖。
_fetch_state = threading.local()


def _download() -> Optional[str]:
    """拉取 CBOE CSV 原文；失败返回 None（不抛，让调用方走缓存）"""
    try:
        with _CBOE_SEM:  # 串行化：不给对端限流器加压（非 TLS 栈原因，见 http_gate）
            req = urllib.request.Request(_VIX_URL, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=_NET_TIMEOUT).read()
        text = raw.decode("utf-8", errors="replace")
        if "DATE" not in text[:200]:
            _log.warning("CBOE VIX CSV 格式异常（首行无 DATE），丢弃")
            return None
        return text
    except Exception as e:  # noqa: BLE001 - 网络层什么都可能抛
        _log.warning("CBOE VIX 下载失败: %s", e)
        return None


def _write_cache(text: str) -> None:
    try:
        _cache_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_cache_path()) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, _cache_path())  # 原子替换，避免读到半截文件
    except OSError as e:
        _log.debug("CBOE VIX 缓存写入失败（不阻断）: %s", e)


def _read_cache() -> Optional[str]:
    try:
        if _cache_path().exists():
            return _cache_path().read_text(encoding="utf-8")
    except OSError as e:
        _log.debug("CBOE VIX 缓存读取失败: %s", e)
    return None


def _cache_fresh() -> bool:
    try:
        return _cache_path().exists() and (time.time() - _cache_path().stat().st_mtime) < _CACHE_TTL
    except OSError:
        return False


def _parse(text: str) -> List[Tuple[str, float]]:
    """CSV → [(ISO 日期, 收盘)]，按日期升序。坏行跳过，不让单行毁掉整份历史。"""
    out: List[Tuple[str, float]] = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            d, c = (row.get("DATE") or "").strip(), (row.get("CLOSE") or "").strip()
            if not d or not c:
                continue
            try:
                # CBOE 用 M/D/YYYY；早年数据偶有 YYYY-MM-DD
                dt = datetime.strptime(d, "%m/%d/%Y") if "/" in d else datetime.strptime(d, "%Y-%m-%d")
                close = float(c)
            except (ValueError, TypeError):
                continue
            if _MIN_VALID_VIX <= close <= _MAX_VALID_VIX:
                out.append((dt.strftime("%Y-%m-%d"), close))
    except Exception as e:  # noqa: BLE001
        _log.warning("CBOE VIX CSV 解析失败: %s", e)
        return []
    out.sort(key=lambda x: x[0])
    return out


def get_vix_history(max_days: Optional[int] = None, force_refresh: bool = False) -> List[Tuple[str, float]]:
    """返回 [(ISO 日期, 收盘)] 升序。网络失败时回落到磁盘缓存；都没有则返回 []。

    v0.45.357：走了哪条路记在 `last_history_fetch()`。陈旧缓存照样返回（「也好过没有」），
    但此前**只打一行 INFO**：09-24 / 09-25 两份日报都读到 09-22 的 14.21，标着 `cboe`，
    下游无从分辨。现在 `get_vix_observation` 把它连同观测日一起带出去。
    """
    mode = "none"
    text = None
    if not force_refresh and _cache_fresh():
        text = _read_cache()
        mode = "cache_fresh" if text else mode
    if text is None:
        text = _download()
        if text:
            _write_cache(text)
            mode = "download"
        else:
            text = _read_cache()  # 用陈旧缓存也好过没有 —— 但必须让下游看得见
            if text:
                mode = "cache_stale"
                _log.warning("CBOE VIX 下载失败，使用过期缓存（观测日见 vix_as_of）")
    _fetch_state.mode = mode
    if not text:
        return []
    hist = _parse(text)
    return hist[-max_days:] if max_days else hist


def last_history_fetch() -> str:
    """本线程最近一次 `get_vix_history` 的取数方式；从未调用过（或被测试桩替换）⇒ "unknown"。"""
    return getattr(_fetch_state, "mode", "unknown")


# ════════════════════════════════════════════════════════════════════
# 当日收盘：延迟报价 + 自动核对账本（v0.45.357）
# ════════════════════════════════════════════════════════════════════

def _download_quote() -> Optional[dict]:
    """CBOE 延迟报价 JSON 的 `data` 段；失败返回 None（不抛，调用方据此退回 CSV）。"""
    try:
        with _CBOE_SEM:
            req = urllib.request.Request(_QUOTE_URL, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=_NET_TIMEOUT).read()
        data = json.loads(raw.decode("utf-8", errors="replace")).get("data")
        return data if isinstance(data, dict) else None
    except Exception as e:  # noqa: BLE001 - 网络层 / JSON 什么都可能抛
        _log.warning("CBOE VIX 报价下载失败: %s", e)
        return None


def _ledger_path() -> Path:
    """报价核对账本的落点。**调用时求值**（同 `_cache_path`）。"""
    if _LEDGER_PATH is not None:
        return Path(_LEDGER_PATH)
    from hive_logger import PATHS
    return Path(PATHS.cache_dir) / "vix_quote_ledger.json"


def _read_ledger() -> Dict[str, dict]:
    try:
        p = _ledger_path()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError) as e:
        _log.warning("VIX 报价账本读取失败（按空账本处理）: %s", e)
    return {}


def _write_ledger(led: Dict[str, dict]) -> None:
    try:
        keep = dict(sorted(led.items())[-_LEDGER_KEEP:])
        p = _ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, p)
    except OSError as e:
        _log.warning("VIX 报价账本写入失败: %s", e)


def verify_quote_ledger(hist: List[Tuple[str, float]]) -> dict:
    """拿 CSV 历史核对账本里还没核过的报价收盘；核过的写回账本。

    → `{"verified": 累计核对相符场数, "pending": 待 CSV 公布的场数,
        "mismatches": [{"session", "quote", "csv"}…]（账本里全部不符，按日期升序）}`
    新发现的不符打 WARNING —— 这是「报价 = 官方收盘」这个前提唯一的会红的观测点。
    """
    led = _read_ledger()
    csv_close = dict(hist)
    changed = False
    for sess, e in led.items():
        if e.get("status") != "pending" or sess not in csv_close:
            continue
        c, q = csv_close[sess], e.get("quote")
        ok = isinstance(q, (int, float)) and abs(c - q) <= _LEDGER_TOL
        e["status"], e["csv"] = ("verified" if ok else "mismatch"), c
        changed = True
        if not ok:
            _log.warning("VIX 报价收盘与 CBOE 官方收盘不符：%s 报价 %s vs CSV %s —— "
                         "%d 天内停用报价、退回 CSV", sess, q, c, _MISMATCH_LOOKBACK_DAYS)
    if changed:
        _write_ledger(led)
    return {
        "verified": sum(1 for e in led.values() if e.get("status") == "verified"),
        "pending": sum(1 for e in led.values() if e.get("status") == "pending"),
        "mismatches": [{"session": s, "quote": e.get("quote"), "csv": e.get("csv")}
                       for s, e in sorted(led.items()) if e.get("status") == "mismatch"],
    }


def _record_quote_close(session: str, quote: float, last_trade_time: Optional[str],
                        captured_at: str) -> None:
    led = _read_ledger()
    led[session] = {"quote": quote, "last_trade_time": last_trade_time,
                    "captured_at": captured_at, "status": "pending"}
    _write_ledger(led)


def _prev_trading_day(d: _date) -> Optional[_date]:
    """`d` 之前最近的交易日（走 is_trading_day 的假日表，不心算）；10 天内找不到 → None。"""
    from is_trading_day import is_trading_day
    for _ in range(10):
        d -= timedelta(days=1)
        if is_trading_day(d)[0]:
            return d
    return None


def _vix_close_moment(d: _date) -> datetime:
    """该交易日 VIX 停算时刻（美东朴素）：交易所收盘 + 15 分钟。"""
    from is_trading_day import session_close_et
    return datetime.combine(d, session_close_et(d)) + _VIX_CALC_TAIL


def _expected_session(now_naive: datetime) -> Optional[_date]:
    """此刻「最新一场已收完的 VIX」应是哪一天。"""
    from is_trading_day import is_trading_day
    d = now_naive.date()
    if is_trading_day(d)[0] and now_naive >= _vix_close_moment(d):
        return d
    return _prev_trading_day(d)


def vix_staleness(as_of: Optional[str], now_et: Optional[datetime] = None) -> Tuple[Optional[int], Optional[bool]]:
    """→ `(lag_sessions, stale)`；`as_of` 缺失或日历算不出 ⇒ `(None, None)`（判不了就说判不了）。

    - `lag_sessions`：`as_of` 落后「此刻最新一场已收完的 VIX」几个交易日（0 = 当日）。
    - `stale`：`as_of` 早于**扫描日的前一交易日** —— CSV 路径落后一场（17:00 ET 扫描时
      CBOE 还没追加当日）是已知常态、**不算**陈旧；落后更多（09-24/25 读到 09-22）才算。
    """
    try:
        a = _date.fromisoformat(str(as_of)) if as_of else None
        if a is None:
            return None, None
        if now_et is None:
            from cboe_options import _et_now
            now_et = _et_now()
        now_naive = now_et.replace(tzinfo=None)
        from is_trading_day import is_trading_day
        prev = _prev_trading_day(now_naive.date())
        exp = _expected_session(now_naive)
        if prev is None or exp is None:
            return None, None
        lag, d = 0, a
        while d < exp and lag <= 30:
            d += timedelta(days=1)
            if is_trading_day(d)[0]:
                lag += 1
        return lag, a < prev
    except Exception as e:  # noqa: BLE001 - 日历/时钟模块不可得：留空，不猜
        _log.debug("VIX 新鲜度判不了: %s", e)
        return None, None


def get_vix_session_close(now_et: Optional[datetime] = None) -> Tuple[Optional[dict], str]:
    """当日 VIX 收盘（延迟报价）→ `(obs, reason)`；拒收时 `obs=None`、`reason` 说明为什么。

    `obs = {"close", "as_of", "prev_close_quote", "last_trade_time"}`；
    `reason ∈ {"ok", "not_trading_day", "before_close", "disabled_after_mismatch",
               "quote_unavailable", "quote_bad_value", "quote_vintage_mismatch", "clock_unavailable"}`。
    只在「钟已过 VIX 停算 **且** 报价的 last_trade_time 落在这一场收盘窗口」时才返回值 ——
    盘中值、盘前值、CDN 陈旧文件都不冒充收盘。
    """
    try:
        from cboe_options import _et_now, _payload_last_trade_et
        from is_trading_day import is_trading_day, session_close_et
        now_naive = (now_et or _et_now()).replace(tzinfo=None)
    except Exception as e:  # noqa: BLE001
        _log.debug("VIX 报价：时钟/日历不可得: %s", e)
        return None, "clock_unavailable"
    d = now_naive.date()
    if not is_trading_day(d)[0]:
        return None, "not_trading_day"
    if now_naive < _vix_close_moment(d):
        return None, "before_close"
    # fail-closed：近期核对出过不符 ⇒ 这个前提已被证伪，停用到它滚出窗口
    cutoff = (d - timedelta(days=_MISMATCH_LOOKBACK_DAYS)).isoformat()
    if any(e.get("status") == "mismatch" and s >= cutoff for s, e in _read_ledger().items()):
        return None, "disabled_after_mismatch"
    q = _download_quote()
    if not q:
        return None, "quote_unavailable"
    try:
        v = float(q.get("current_price"))
    except (TypeError, ValueError):
        return None, "quote_bad_value"
    if not (_MIN_VALID_VIX <= v <= _MAX_VALID_VIX):
        return None, "quote_bad_value"
    ltt = _payload_last_trade_et(q)
    close_dt = datetime.combine(d, session_close_et(d))
    if ltt is None or ltt.date() != d or not (close_dt <= ltt <= close_dt + _QUOTE_LTT_WINDOW):
        _log.warning("CBOE VIX 报价不是 %s 的收盘值（last_trade_time=%s），退回 CSV",
                     d, q.get("last_trade_time"))
        return None, "quote_vintage_mismatch"
    try:
        pc = float(q.get("prev_day_close"))
        pc = pc if _MIN_VALID_VIX <= pc <= _MAX_VALID_VIX else None
    except (TypeError, ValueError):
        pc = None
    return ({"close": v, "as_of": d.isoformat(), "prev_close_quote": pc,
             "last_trade_time": q.get("last_trade_time")}, "ok")


def get_vix_observation(now_et: Optional[datetime] = None) -> dict:
    """`fred_macro` 的唯一入口：当前 VIX 观测 + 它属于哪一天 + 怎么拿到的。

    返回（取不到时各值为 None，**键始终齐全**）::

        vix, as_of, prev_close, prev_as_of   观测值与前一收盘，均带观测日
        feed           "delayed_quote" | "history_csv" | None
        quote_reason   报价为什么没被采用（采用时为 "ok"）
        history_fetch  CSV 这次怎么拿到的（download / cache_fresh / cache_stale / none / unknown）
        lag_sessions, stale   见 `vix_staleness`
        quote_check    报价核对账本摘要（见 `verify_quote_ledger`）
    """
    obs = {"vix": None, "as_of": None, "prev_close": None, "prev_as_of": None,
           "feed": None, "quote_reason": None, "history_fetch": None,
           "lag_sessions": None, "stale": None, "quote_check": None}
    hist = get_vix_history(max_days=30)
    obs["history_fetch"] = last_history_fetch()
    try:
        obs["quote_check"] = verify_quote_ledger(hist)
    except Exception as e:  # noqa: BLE001 - 核对坏了不能拖垮取数，但要看得见
        _log.warning("VIX 报价账本核对失败: %s", e)

    sess, reason = get_vix_session_close(now_et)
    if sess is not None:
        prev_d = _prev_trading_day(_date.fromisoformat(sess["as_of"]))
        prev_iso = prev_d.isoformat() if prev_d else None
        csv_prev = dict(hist).get(prev_iso) if prev_iso else None
        pq = sess["prev_close_quote"]
        # 同场自检：报价说的上一场收盘，必须等于 CSV 已公布的上一场收盘
        if csv_prev is not None and pq is not None and abs(csv_prev - pq) > _LEDGER_TOL:
            _log.warning("CBOE VIX 报价的 prev_day_close=%s 与 CSV %s 收盘 %s 不符，拒收报价",
                         pq, prev_iso, csv_prev)
            sess, reason = None, "quote_prev_close_disagrees"
    obs["quote_reason"] = reason

    if sess is not None:
        obs.update(vix=sess["close"], as_of=sess["as_of"], feed="delayed_quote",
                   prev_close=csv_prev if csv_prev is not None else pq,
                   prev_as_of=prev_iso if (csv_prev is not None or pq is not None) else None)
        try:
            from cboe_options import _et_now
            _record_quote_close(sess["as_of"], sess["close"], sess["last_trade_time"],
                                (now_et or _et_now()).isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            _log.warning("VIX 报价收盘未能记账（次日无法核对）: %s", e)
    elif hist:
        obs.update(vix=hist[-1][1], as_of=hist[-1][0], feed="history_csv")
        if len(hist) >= 2:
            obs.update(prev_as_of=hist[-2][0], prev_close=hist[-2][1])
    obs["lag_sessions"], obs["stale"] = vix_staleness(obs["as_of"], now_et)
    if obs["stale"]:
        _log.warning("VIX 观测陈旧：as_of=%s，落后 %s 场（取数方式 %s）",
                     obs["as_of"], obs["lag_sessions"], obs["history_fetch"])
    return obs


def get_vix_observation_asof(as_of: str) -> dict:
    """补跑（`--date D`）专用：D 的 CBOE 官方收盘 —— CSV 里**按日期精确取**那一行（v0.45.366）。

    返回与 `get_vix_observation` 同形（键始终齐全），区别三处：

    - **绝不取「最后一行」**：补跑在 D 之后跑，最后一行属于运行当天 —— 正是 v0.45.59 要治的
      「今天的数贴上那天的日期」。
    - **不走延迟报价**：报价只代表「此刻最新一场」，对过去某天没有意义（`quote_reason="backfill"`）。
    - **新鲜度按 D 判**：观测日就是 D ⇒ `lag_sessions=0`、`stale=False`。`vix_staleness` 按「此刻」判，
      拿它判补跑会把任何过去的 D 都判成陈旧、Guard 一票不投。

    为什么不用快照里的 `vix_spot`：云端 17:05 ET 抓它时 CSV 还没有 D 行，08-26~09-11 那 12 份
    `market.json` 全是上一场收盘（2026-09-28 实测，对照新拉的 CSV、交易日按 `is_trading_day` 数）；
    且快照不记观测日，09-14 起它变成当日值（来源待验证，同刻 SKEW CSV 仍是上一场）后也分辨不出来。
    补跑是事后跑，D 的官方收盘早已在 CSV 里。

    CSV 里没有 D（缓存是 D 当晚 CSV 追加之前下的 / 下载失败读到旧缓存）⇒ 强制重下一次；
    仍没有 ⇒ `vix=None`、`quote_reason="asof_row_missing"` 并打 WARNING，由调用方决定退路
    （`fred_macro` 退回快照值、标 `cloud_snapshot_cboe`，GuardBee 不计这票）。

    前一收盘取 D 的**前一交易日**那一行（同 `get_vix_observation` 的报价路径）。
    ⚠️ CSV 含非交易日行（2026-09-07 劳动节有一行 15.30），「上一行」不一定是上一交易日。
    """
    obs = {"vix": None, "as_of": None, "prev_close": None, "prev_as_of": None,
           "feed": None, "quote_reason": "backfill", "history_fetch": None,
           "lag_sessions": None, "stale": None, "quote_check": None}
    try:
        d = _date.fromisoformat(str(as_of))
    except (TypeError, ValueError):
        obs["quote_reason"] = "asof_invalid"
        return obs
    closes = dict(get_vix_history())
    obs["history_fetch"] = last_history_fetch()
    if d.isoformat() not in closes:
        closes = dict(get_vix_history(force_refresh=True))
        obs["history_fetch"] = last_history_fetch()
    close = closes.get(d.isoformat())
    if close is None:
        _log.warning("CBOE VIX CSV 里没有 %s 这一行（取数方式 %s）—— 补跑取不到该日官方收盘",
                     d, obs["history_fetch"])
        obs["quote_reason"] = "asof_row_missing"
        return obs
    prev_d = _prev_trading_day(d)
    prev_iso = prev_d.isoformat() if prev_d else None
    pc = closes.get(prev_iso) if prev_iso else None
    obs.update(vix=close, as_of=d.isoformat(), feed="history_csv_asof",
               prev_close=pc, prev_as_of=prev_iso if pc is not None else None,
               lag_sessions=0, stale=False)
    return obs


def get_vix_spot() -> Optional[Tuple[float, str]]:
    """最新收盘 VIX → (值, ISO 日期)。拿不到返回 None，**不返回猜测值**。"""
    hist = get_vix_history()
    if not hist:
        return None
    date, close = hist[-1][0], hist[-1][1]
    return (close, date)


def get_vix_percentile(window: int = VIX_PERCENTILE_WINDOW) -> Optional[float]:
    """最新 VIX 在过去 `window` 个交易日中的**恐慌分位**（0~100）。

    注意方向：返回值是"比多少比例的交易日更高"。VIX 14.6 → 约 1~5，
    代表极度平静。若要"平静度分位"需自行取 100 - 本值——两者方向相反，
    混用会得到完全相反的结论。
    """
    hist = get_vix_history(max_days=window)
    if len(hist) < 30:  # 样本太少，分位无意义
        return None
    vals = [v for _, v in hist]
    cur = vals[-1]
    below = sum(1 for v in vals if v < cur)
    return round(below / len(vals) * 100, 1)


def get_vix_regime(vix: Optional[float]) -> str:
    """与 fred_macro 同口径的分档，供两条路径产出一致的标签"""
    if vix is None:
        return "unknown"
    if vix < 15:
        return "low"
    if vix < 20:
        return "moderate"
    if vix < 30:
        return "elevated"
    if vix < 40:
        return "high"
    return "spike"


if __name__ == "__main__":  # pragma: no cover
    spot = get_vix_spot()
    if spot:
        v, d = spot
        pct = get_vix_percentile()
        print(f"VIX {v:.2f} ({d})  regime={get_vix_regime(v)}")
        print(f"252 日恐慌分位: {pct}%（平静度 {round(100 - pct, 1) if pct is not None else 'N/A'}%）")
        print(f"历史样本: {len(get_vix_history())} 个交易日")
    else:
        print("VIX 不可用（网络与缓存均失败）")
