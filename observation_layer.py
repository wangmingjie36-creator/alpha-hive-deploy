#!/usr/bin/env python3
"""
🐝 Alpha Hive — 观测层（v0.45.580 起，buzz_v1 根治：取数与解释分家）
=====================================================================

为什么有它
----------
v0.45.568 把喂 H1 情绪通道的原始取数落了档（`observation_archive`），但取数代码仍长在各个评分模块里。根治路线
（用户 2026-10-20 定）的终点是：**取数**（去哪个源、带什么参数、按什么顺序兜底）是一个独立的、有版本号的组件；
评分代码与冻结版 buzz_v1 都只是它的消费者。生产要换取数方式只能发新版本，buzz_v1 固定用旧版本——旧版本照样有人取，
「这条边界动没动 H1 的输入」于是按结构永远是「没动」，不用人判、也不用事后检测。

本模块是这个组件的第一块（阶段 1b，**行情日线**）：
- `yf_history`：yfinance 日线——生产行情字段（动量 / 量比 / 波动率）的主路径都从这里取，取到即原样落档；
- `spy_last_minute`：交易所时钟（SPY 最后一根分钟线）——「末根日线还在不在形成」靠它判；
- `clock_ref`：把某一次判断实际用到的时钟值登记成一份观测。
这些函数**只取数、只落档，不改返回内容**：返回的 DataFrame / 时间戳与直接调 yfinance 逐位相同。判「末根在不在形成」、
算窗口、算公式都留在 `data_pipeline`（那是解释，阶段 2 由冻结版逐行照抄、从档案重算）。

版本
----
`observation_archive.LAYER_VERSION`（"obs-1"）是这里所有取数的版本，每条取数事件都记它。**改本模块的请求参数 /
源 / 兜底顺序 = 新版本**（届时旧版本的函数原样留在这里，buzz_v1 继续用旧的；双版本并存的接线在「观测层版本化」那一步做）。
在那之前，本模块整份在输入层指纹守卫 `price.fields` 单元里（`signal_code_fingerprints.UNITS`），改了就红。

原始内容与回放
--------------
- 日线：`frame_payload` 把 DataFrame 原样序列化（列、逐列 dtype、带时区的时间戳及其分辨率、逐格数值；NaN / ±inf 记 null），
  `frame_from_payload` 逐位还原（测试钉住往返相等）。
- 时钟：`{"spy_last_minute": 带时区 ISO 时间戳 或 null}`（null = 当时没取到，`_drop_forming_bar` 因此原样返回）。

Reddit / 新闻 / 热搜 / F&G 的取数目前仍在各自模块里调 `observation_archive.observe`（v0.45.568），「观测层版本化」时一起搬进来。

取数缓存（v0.45.591，观测层版本化第一块）
-----------------------------------------
新闻与 F&G 的缓存原先存**解释之后的结果**（分数 + 观测地址），不存原始内容。没开档的进程（MCP、手动开发跑）先把它
填了、随后的扫描命中 ⇒ 链接里有地址、档案里没有那份 payload ⇒ 当天桥接核对 incomplete、白白浪费一个桥接日。
现在缓存存的是**这次取数的结局**：用了哪种观测（`kind`）、原始内容（`payload`）、取数时刻、调用方要的附带记录
（新闻：主源那一步的结局）。命中时调用方用与新取数**同一个**解释函数重算，`cache_read` 顺手 `reuse` 补档。
⇒ 缓存命中与新取数走同一条解释路径，档案里一定有被用到的那份观测，不论缓存是谁填的。

- 文件名带观测层版本（`cache_path`）：别的版本、以及 v0.45.591 之前的派生缓存写的文件，这一版读不到；还在跑旧代码的
  长驻进程（MCP / Alpha Bot 在扫描日快进之前起的）也读不到新格式——两边互不误读。
- 读到形状不对、版本不对、过期的文件 ⇒ 当没命中（重新取数），与 `read_json_cache` 对坏 JSON 的处理同一语义：
  缓存只省网络请求，丢了不丢观测。
- `kind=None` = 这次整条取数链都没拿到内容（兜底结果）：照旧缓存这个结局（与改前缓存兜底结果同一 TTL），不记观测。
"""
from __future__ import annotations

import logging as _logging
import math
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import observation_archive as _archive

_log = _logging.getLogger("alpha_hive.observation_layer")

KIND_DAILY = "yfinance.daily"
KIND_CLOCK = "exchange.clock"

#: 取数函数把「这次用到的原始观测」随结果交给调用方的键：``{"kind", "payload"}``。调用方写缓存前取走（`pop`），
#: 不进返回给评分代码的结果、不进缓存的结果层
CARRIER_KEY = "_observed"


# ────────────────────────────────────────────────────────────────────────────
# DataFrame ⇄ 原始内容
# ────────────────────────────────────────────────────────────────────────────

def _cell(v: Any) -> Any:
    """一格的 JSON 写法：整数原样、浮点有限才写（NaN / ±inf ⇒ null）。"""
    if v is None:
        return None
    try:
        import numpy as np
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            v = float(v)
        elif isinstance(v, np.bool_):
            return bool(v)
    except ImportError:  # pragma: no cover
        pass
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return str(v)


def frame_payload(ticker: str, request: Mapping[str, Any], hist) -> Dict[str, Any]:
    """yfinance `history()` 返回的 DataFrame 原样写成可落档的内容（不改 `hist`）。"""
    idx = hist.index
    tz = getattr(idx, "tz", None)
    return {
        "ticker": ticker,
        "request": {str(k): (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                    for k, v in request.items()},
        "tz": str(tz) if tz is not None else None,
        "index_unit": getattr(idx, "unit", None),   # 时间戳分辨率（s / ms / us / ns），还原时照原样
        "index": [ts.isoformat() for ts in idx],
        "columns": [str(c) for c in hist.columns],
        "dtypes": [str(t) for t in hist.dtypes],
        "data": [[_cell(v) for v in row] for row in hist.itertuples(index=False, name=None)],
    }


def frame_from_payload(payload: Mapping[str, Any]):
    """`frame_payload` 的逆：还原出逐位相同的 DataFrame（阶段 2 的冻结层与回放用）。"""
    import pandas as pd
    cols = list(payload["columns"])
    rows = payload["data"]
    data = {}
    for j, (col, dt) in enumerate(zip(cols, payload["dtypes"])):
        # 一律按记下的 dtype 还原（浮点列里的 null ⇒ NaN）。不靠推断：整列 NaN 的浮点列推断出来是 object
        data[col] = pd.Series([r[j] for r in rows], dtype=dt).to_numpy()
    if payload.get("tz"):
        index = pd.DatetimeIndex(pd.to_datetime(payload["index"], utc=True)).tz_convert(payload["tz"])
    else:
        index = pd.DatetimeIndex(pd.to_datetime(payload["index"]))
    if payload.get("index_unit"):
        index = index.as_unit(payload["index_unit"])
    index.name = "Date"
    return pd.DataFrame(data, index=index, columns=cols)


# ────────────────────────────────────────────────────────────────────────────
# 取数入口（只取数、只落档，返回内容不变）
# ────────────────────────────────────────────────────────────────────────────

def yf_history(ticker: str, **request) -> Tuple[Any, Optional[str]]:
    """`yf.Ticker(ticker).history(**request)`，返回 `(DataFrame, 观测地址)`；DataFrame 与直接调用逐位相同。

    异常照原样抛给调用方（各调用方本来就有自己的 except 与降级）；抛之前记一条失败事件。
    地址算不出（落档模块绝不抛，见 `observation_archive`）⇒ 第二项为 None，调用方据此把这份输入记成「用了但没存」。
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(**request)
    except Exception as e:
        _archive.fetch_failed(KIND_DAILY, "error", request={"ticker": ticker, **request},
                              message=f"{type(e).__name__}: {e}")
        raise
    ref = None
    try:
        payload = frame_payload(ticker, request, hist)
    except Exception as e:  # noqa: BLE001 —— 序列化失败不得影响取数；记失败，地址给 None
        _archive._fail(f"frame_payload:{ticker}", e)
    else:
        ref = _archive.observe(KIND_DAILY, payload, request={"ticker": ticker, **request})
    return hist, ref


def _clock_payload(ts) -> Dict[str, Optional[str]]:
    return {"spy_last_minute": ts.isoformat() if ts is not None else None}


def spy_last_minute():
    """SPY 今天最后一根分钟线的时间戳（美东、带时区）；取不到 ⇒ None。`data_pipeline._exchange_now` 唯一的取数处。

    取到 / 取不到都记一份观测（取不到也是事实：那一轮 `_drop_forming_bar` 因此原样返回）。异常不外抛——与改前
    `_exchange_now` 里的 `except Exception: pass` 同一语义。
    """
    ts = None
    try:
        import yfinance as yf
        intra = yf.Ticker("SPY").history(period="1d", interval="1m")
        if not intra.empty:
            ts = intra.index[-1]
    except Exception as e:  # noqa: BLE001
        _archive.fetch_failed(KIND_CLOCK, "error", request={"ticker": "SPY", "period": "1d", "interval": "1m"},
                              message=f"{type(e).__name__}: {e}")
        return None
    _archive.observe(KIND_CLOCK, _clock_payload(ts), request={"ticker": "SPY", "period": "1d", "interval": "1m"})
    return ts


def clock_ref(ts) -> Optional[str]:
    """某次判断实际用到的时钟值（`data_pipeline._exchange_now()` 的返回）登记成观测，返回地址。

    时钟整进程缓存一次（`_exchange_now`），每只票的判断都用同一个值 ⇒ 内容寻址后是同一份；扫描前就取过的
    （没开档时）也会在这里补档。
    """
    return _archive.reuse(KIND_CLOCK, _clock_payload(ts))


# ────────────────────────────────────────────────────────────────────────────
# 取数缓存：存原始观测，不存解释（v0.45.591，见模块说明「取数缓存」）
# ────────────────────────────────────────────────────────────────────────────

def cache_path(directory, stem: str) -> Path:
    """缓存文件路径：``<directory>/<stem>.<观测层版本>.json``（调用时求值，`directory` 由调用方按 `PATHS` 解析）。"""
    return Path(directory) / f"{stem}.{_archive.LAYER_VERSION}.json"


def cache_write(path, *, kind: Optional[str], payload: Any, fetched_at: Any,
                meta: Optional[Mapping[str, Any]] = None) -> None:
    """把一次取数的结局写进缓存。写不进只记 debug：下次重新取数，观测照样落档，不丢任何东西（与改前缓存写失败同一语义）。"""
    from hive_logger import atomic_json_write
    doc = {"layer": _archive.LAYER_VERSION, "fetched_at": fetched_at, "kind": kind,
           "payload": payload if kind is not None else None, "meta": dict(meta or {})}
    try:
        atomic_json_write(path, doc)
    except (OSError, TypeError, ValueError) as e:
        _log.debug("取数缓存写入失败 (%s): %s", path, e)


def _cache_doc_ok(doc: Any) -> bool:
    if not isinstance(doc, dict) or doc.get("layer") != _archive.LAYER_VERSION:
        return False
    if not isinstance(doc.get("fetched_at"), str) or not isinstance(doc.get("meta"), dict):
        return False
    kind = doc.get("kind")
    if kind is None:
        return doc.get("payload") is None
    return kind in _archive.KINDS and "payload" in doc


def cache_read(path, ttl: float) -> Optional[Dict[str, Any]]:
    """读一份没过期、本版本写的取数缓存：``{"kind", "payload", "fetched_at", "meta", "ref"}``；否则 None（当没命中）。

    命中且有观测 ⇒ `reuse`：扫描进程里确保那份 payload 在档（缓存可能是没开档的进程填的），`ref` 是它的地址。
    """
    from hive_logger import read_json_cache
    doc = read_json_cache(path, ttl)
    if not _cache_doc_ok(doc):
        return None
    kind = doc["kind"]
    ref = _archive.reuse(kind, doc["payload"]) if kind is not None else None
    return {"kind": kind, "payload": doc["payload"], "fetched_at": doc["fetched_at"], "meta": doc["meta"], "ref": ref}
