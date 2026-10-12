#!/usr/bin/env python3
"""
自攒收盘价历史（v0.43.25）
==========================
`momentum_5d` / `volume_ratio` 长期依赖 `yf.Ticker(t).history()`。宏观与历史
K线都排在 30 只标的扫完之后，yfinance 配额早已耗尽（2026-08-14 全天 363 条
Too Many Requests），于是 `_fetch_history_metrics` 返回 None。

后果分两条，一条响一条不响：
- **BuzzBee 诚实写 None** → 下游 `momentum > 0` 抛 TypeError，ML 报告每日
  11/12 份丢失（已由 v0.43.23 修掉渲染侧）
- **ScoutBee 用 `or 0.0` 伪造"持平"** → 无声进入评分。实测近 28 个扫描日、
  395 次情绪背离检测**全部**是 `none` / severity 0：0.0 永远够不到阈值，
  整个背离检测功能是死的

本模块的解法：不再向外求数。日报每天都会把 CBOE 观测到的收盘价写进期权快照
（`_snapshot_stock_price`），**这本来就是我们自己的真实观测**，只是从没被
攒起来用过。攒成紧凑索引后，5 日动量完全不需要外部历史接口。

存储与读取沿用 `iv_history.py`（v0.43.21）已验证的模式：每票一个追加式
`cache/price_history_{TICKER}.jsonl`，sentinel 控制的一次性快照迁移，
日常热路径不解析任何快照。

⚠️ 实际上索引自 08-15/16 那次一次性迁移后再没长过：`append_observation` 从来没有
生产调用方。新鲜度全靠读时合并的 DB 权威价（`predictions.price_at_predict`），
而**扫描日当天**那一行要到取数之后才写 ⇒ 取数时序列最新一点是上一个扫描日。
当天收盘由调用方在内存里经 `latest_close` 补上（v0.45.465，见 `momentum_5d_detail`），
不落盘。

**拿不到就返回 None，绝不返回 0.0。** 0.0 不是"没有动量"，是"持平"——
这两者在背离检测里是完全不同的输入。
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Dict, List, Optional, Tuple

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.price_history")
except ImportError:  # pragma: no cover - 独立运行兜底
    import logging
    _log = logging.getLogger("alpha_hive.price_history")

# ⚠️「5 日动量」的全仓口径是 **4 个交易日**的收益：主路径（yfinance）一律
# `Close.iloc[-1] / Close.iloc[-5]`——5 个点、4 个间隔（`data_pipeline` 4 处、
# `swarm_agents/cache.py`、`risk_engine`、`crowding_detector`），这里是它缺数时的回落，
# 必须量同一个窗口（v0.45.469；此前按 ≥5 个交易日挑锚点，生产库回放见该版 CHANGELOG）。
# 守卫 `tests/test_momentum_window_alignment.py`。改成真 5 日要改主路径并追加世代边界，
# 且 `price.momentum_5d` 在维度 IC 协议 H1 输入层里——不在这里单方面改。
MOMENTUM_LOOKBACK = 4
MIN_DAYS_FOR_MOMENTUM = MOMENTUM_LOOKBACK + 1
# 锚点最多允许比 LOOKBACK 远 2 个交易日：扫描偶尔漏一两天可接受，
# 再远就名不副实了
MOMENTUM_MAX_GAP = MOMENTUM_LOOKBACK + 2
# 孤立尖峰阈值：期权快照价有已知污染（dashboard_renderer:1883 记录 NVDA 2026-06-15
# 显示 $145、真实 $212，偏差 32%）。真实跳空（财报）不会次日原路返回，
# 而污染点的签名正是"相对前后两点都大幅偏离、且方向相反"。
_SPIKE_PCT = 0.25
# 只在前后两点都足够近时才做尖峰判定：索引有空档，隔一周的两点相差 10%
# 完全正常，用它当"邻居"会把正常波动误杀（实测误杀率一度到 21.6%）。
_SPIKE_MAX_NEIGHBOR_GAP_DAYS = 5
FULL_WINDOW = 252

_SNAP_RE = re.compile(r"options_snapshot_(.+)_(\d{4}-\d{2}-\d{2})\.json$")
# v0.45.462：快照现价出处里算「收盘价」的（见 options_analyzer._resolve_stock_price）。
# caller = 蜂群当天用的价（与 DB price_at_predict 同源）；cboe_close = 已核实的官方收盘。
# 盘中价 / 盘中生成的陈旧文件 / 判不了 / 不可得 / 没有出处（旧快照）一律不收。
_SNAPSHOT_CLOSE_SOURCES = frozenset({"caller", "cboe_close"})
_MIN_VALID_PRICE = 0.01
_MAX_VALID_PRICE = 1_000_000.0


def _default_cache_dir() -> str:
    """缓存目录缺省 = `PATHS.cache_dir`，**调用时**求值（v0.45.463）。

    此前 `data_pipeline` 传 `os.environ.get("ALPHA_HIVE_CACHE_DIR", "cache")`、
    `coverage_report` / CLI 缺省 `"cache"`——都是 cwd 相对。数据根迁移（09-26）与
    生产克隆（10-09）之后扫描的 cwd 是代码克隆，那里没有 `cache/`：回落读空、
    首次调用还会在代码克隆里建 `cache/` + 迁移标记。`PATHS.cache_dir` 照读
    `ALPHA_HIVE_CACHE_DIR`，覆盖语义不变。
    """
    from hive_logger import PATHS
    return str(PATHS.cache_dir)


def _default_db_path() -> str:
    """权威价库缺省 = `PATHS.db`，**调用时**求值（v0.45.463）。理由同 `_default_cache_dir`。"""
    from hive_logger import PATHS
    return str(PATHS.db)


# 库文件不存在是「回落静默失效」的原形（v0.45.463 之前 cwd 相对的默认值让它
# 在生产上恒不存在，而 `return {}` 与「这只票库里没记录」长得一样）——每个路径报一次 warning。
_MISSING_DB_WARNED: set = set()


def _index_path(ticker: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"price_history_{ticker}.jsonl")


def _migration_marker(ticker: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f".price_index_migrated_{ticker}")


def _is_trading_date(date: str) -> bool:
    """只收交易日。手动/强制跑（周末补跑、临时测试）会写出非交易日快照，
    混进索引会把 5 日窗口整体挪位，算出一个看着合理其实错位的动量。
    护栏不可得时放行——宁可多收一天，也不要因为护栏本身故障而丢掉全部历史。"""
    try:
        from datetime import date as _d
        from is_trading_day import is_trading_day
        y, m, dd = (int(x) for x in date.split("-"))
        return bool(is_trading_day(_d(y, m, dd))[0])
    except Exception:  # noqa: BLE001
        return True


def _valid(px) -> Optional[float]:
    try:
        p = float(px)
    except (TypeError, ValueError):
        return None
    return p if _MIN_VALID_PRICE <= p <= _MAX_VALID_PRICE else None


def append_observation(ticker: str, cache_dir: str, date: str, close) -> bool:
    """把当日观测收盘价追加进索引。同日重复调用只保留最后一次。

    Returns: True=已写入 / False=跳过（值无效或当日已记录同值）
    """
    px = _valid(close)
    if px is None:
        return False
    if not _is_trading_date(date):
        return False

    path = _index_path(ticker, cache_dir)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                last = None
                for last in f:  # noqa: B007 - 只要最后一行
                    pass
            if last:
                try:
                    rec = json.loads(last)
                    if rec.get("date") == date and abs(float(rec.get("close", -1)) - px) < 1e-9:
                        return False
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass  # 末行损坏不阻断写入

        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": date, "close": round(px, 4)}, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        _log.debug("[%s] 价格索引写入失败（不阻断评分）: %s", ticker, e)
        return False


def _read_index(ticker: str, cache_dir: str) -> Dict[str, float]:
    path = _index_path(ticker, cache_dir)
    if not os.path.exists(path):
        return {}
    by_date: Dict[str, float] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    d, px = rec.get("date"), _valid(rec.get("close"))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue  # 跳过损坏行，不让单行错误废掉整个索引
                # 此处**不**过滤交易日：非交易日的快照（周末/强制跑）虽不进最终
                # 序列，却是识别孤立尖峰的关键旁证——QCOM 2026-08-14 的 185.0
                # 正是靠周六那笔 165.7 才被证伪。过滤放到 despike 之后。
                if d and px is not None:
                    by_date[d] = px
    except OSError as e:
        _log.debug("[%s] 价格索引读取失败: %s", ticker, e)
        return {}
    return by_date


def _scan_snapshots(ticker: str, cache_dir: str) -> Dict[str, float]:
    """慢路径：从期权快照扫出 _snapshot_stock_price。仅用于一次性迁移。"""
    by_date: Dict[str, float] = {}
    for path in glob.glob(os.path.join(cache_dir, f"options_snapshot_{ticker}_*.json")):
        m = _SNAP_RE.search(os.path.basename(path))
        if not m or m.group(1) != ticker:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        # v0.45.462：只收**能自证出处**的快照价。v0.45.462 前 analyze() 取不到现价时会写
        # 行权价中位数或常数 145.0，且快照不带出处——这些旧值与真价同形、分不开（索引里实测
        # 40 行 145.0、29 行 100.0；09-17 全源中断那天 30/30 份是行权价中位数）。现有索引是
        # 08-15/16 一次性迁移建的（没有任何调用方往里追加），这条只管**以后**的迁移
        # （新标的 / 标记丢失重迁）不把旧假价再收进来；旧快照的真价由读时合并的 DB 权威价覆盖。
        if snap.get("_snapshot_stock_price_source") not in _SNAPSHOT_CLOSE_SOURCES:
            continue
        # v0.45.468：`caller` 价是调用方当时手上的价，快照若在收盘前写下，它就是盘中价，不是收盘
        # （`_snapshot_session_complete` 是写快照时该场是否已收盘，v0.45.238 起每份都带）。
        if snap.get("_snapshot_session_complete") is not True:
            continue
        px = _valid(snap.get("_snapshot_stock_price"))
        if px is not None and _is_trading_date(m.group(2)):
            by_date[m.group(2)] = px
    return by_date


def _load_db_prices(ticker: str, db_path: Optional[str] = None) -> Dict[str, float]:
    """从 pheromone.db 的 predictions.price_at_predict 取权威收盘价。

    这是蜂群当天实际使用的价格（v0.43.15 起复用 CBOE 快照价），比期权快照的
    `_snapshot_stock_price` 干净得多——后者有已知污染（QCOM 2026-08-14 快照
    185.0，DB 实为 165.94；dashboard_renderer:1883 另记 NVDA 2026-06-15 案例）。
    代价是只覆盖扫描日、较稀疏，所以两者合并、**DB 优先**。

    `db_path=None` ⇒ `PATHS.db`（调用时求值，v0.45.463；此前缺省 cwd 相对的
    `"pheromone.db"`，生产 cwd 里没有它 ⇒ 恒返回 {}）。
    """
    out: Dict[str, float] = {}
    try:
        import sqlite3
        if db_path is None:
            db_path = _default_db_path()
        if not os.path.exists(db_path):
            if db_path not in _MISSING_DB_WARNED:
                _MISSING_DB_WARNED.add(db_path)
                _log.warning("权威价库不存在：%s —— 价格索引只剩期权快照部分", db_path)
            return {}
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for d, px in conn.execute(
                "SELECT date, price_at_predict FROM predictions WHERE ticker=?", (ticker,)
            ):
                v = _valid(px)
                if d and v is not None:
                    out[d] = v
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - DB 不可用不该阻断评分
        _log.debug("[%s] DB 权威价读取失败: %s", ticker, e)
    return out


def merge_snapshots_into_index(ticker: str, cache_dir: str) -> int:
    """把快照里的收盘价**合并**进索引（冲突时索引优先）。返回索引总条数。

    必须是"合并"而非"覆盖"：覆盖会抹掉索引里已有的记录（此坑 iv_history 初版踩过）。
    ⚠️ 此前这里写「调用方会先 append 当日观测再读历史」——从来没有调用方这么做
    （`append_observation` 无生产调用方），当天收盘走的是 `load_price_history(latest_close=)`
    的内存合并（v0.45.465）。
    """
    by_date = _scan_snapshots(ticker, cache_dir)
    by_date.update(_read_index(ticker, cache_dir))
    if not by_date:
        return 0
    path = _index_path(ticker, cache_dir)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for d in sorted(by_date):
                f.write(json.dumps({"date": d, "close": round(by_date[d], 4)}, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        _log.info("[%s] 价格索引迁移完成：%d 条", ticker, len(by_date))
        return len(by_date)
    except OSError as e:
        _log.debug("[%s] 价格索引迁移失败: %s", ticker, e)
        return 0


def load_price_history(
    ticker: str,
    cache_dir: Optional[str] = None,
    max_days: int = FULL_WINDOW,
    latest_close: Optional[Tuple[str, float]] = None,
) -> List[Tuple[str, float]]:
    """返回 [(ISO 日期, 收盘)] 升序。索引优先；首次调用做一次性快照迁移。

    `cache_dir=None` ⇒ `PATHS.cache_dir`（调用时求值）。

    `latest_close=(ISO 日期, 收盘)`（v0.45.465）：调用方**已核实**的某一场官方收盘，
    只在内存里并进序列、**不写索引**；同日覆盖 DB 与索引（DB 的当日行可能出自盘中陈旧价，
    这一份是按 `last_trade_time` 核过的）。值无效 / 非交易日照常被下面的过滤拦掉——
    调用方要自己核对它有没有进序列（`momentum_5d_detail` 的 `as_of`）。
    """
    if cache_dir is None:
        cache_dir = _default_cache_dir()
    marker = _migration_marker(ticker, cache_dir)
    if not os.path.exists(marker):
        merge_snapshots_into_index(ticker, cache_dir)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                f.write("v0.43.25\n")
        except OSError:
            pass  # 标记写不成只会导致下次重复迁移，不影响正确性

    by_date = _read_index(ticker, cache_dir)
    by_date.update(_load_db_prices(ticker))  # DB 是权威，覆盖同日快照值
    if latest_close is not None:
        _d, _px = latest_close
        _px = _valid(_px)
        if _d and _px is not None:
            by_date[str(_d)] = _px
    if not by_date:
        return []
    # 顺序要紧：先用**全部**观测去尖峰（非交易日快照是旁证），再筛交易日
    ordered = [(d, px) for d, px in _despike([(d, by_date[d]) for d in sorted(by_date)])
               if _is_trading_date(d)]
    return ordered[-max_days:] if max_days else ordered


def _despike(series: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """剔除孤立尖峰：相对前后两点都偏离 >_SPIKE_PCT 且方向相反的点。

    期权快照价存在已知污染（NVDA 2026-06-15 $145 vs 真实 $212）。真实的财报
    跳空不会次日原路返回，所以"两侧都大幅偏离且反向"是污染而非行情的签名。
    首尾点无双侧邻居，保守保留。
    """
    if len(series) < 3:
        return series
    out = [series[0]]
    for i in range(1, len(series) - 1):
        (prev_d, prev_px), (cur_d, cur_px), (next_d, next_px) = series[i - 1], series[i], series[i + 1]
        _near = (0 <= _trading_days_between(prev_d, cur_d) <= _SPIKE_MAX_NEIGHBOR_GAP_DAYS
                 and 0 <= _trading_days_between(cur_d, next_d) <= _SPIKE_MAX_NEIGHBOR_GAP_DAYS)
        if _near and prev_px > 0 and next_px > 0:
            # 判据是"偏离前后两点的均值"，不是逐日涨跌幅。理由：单看涨跌幅
            # 区分不了尖峰和台阶——QCOM 2026-08-14 是 +12.5% 后 −10.4%，
            # 两个 15% 阈值都够不到，却明显是尖峰。用均值判据：
            #   尖峰 164→185→166：185/165.1 = +12.1%  → 命中
            #   台阶 164→185→186：185/175.0 =  +5.7%  → 放行（真实跳空）
            neighbor_mean = (prev_px + next_px) / 2
            if neighbor_mean > 0 and abs(cur_px / neighbor_mean - 1) > _SPIKE_PCT:
                _log.debug("剔除孤立尖峰 %s=%.2f（前 %.2f / 后 %.2f）",
                           series[i][0], cur_px, prev_px, next_px)
                continue
        out.append(series[i])
    out.append(series[-1])
    return out


def _trading_days_between(d1: str, d2: str) -> int:
    """d1 到 d2 之间的交易日数（不含 d1，含 d2）。跨度很小，逐日走即可。"""
    from datetime import date as _d, timedelta as _td
    try:
        y1, m1, day1 = (int(x) for x in d1.split("-"))
        y2, m2, day2 = (int(x) for x in d2.split("-"))
        cur, end = _d(y1, m1, day1), _d(y2, m2, day2)
    except (ValueError, TypeError):
        return -1
    n = 0
    while cur < end and n < 400:  # 上限防脏数据死循环
        cur += _td(days=1)
        if _is_trading_date(cur.isoformat()):
            n += 1
    return n


def momentum_5d(
    ticker: str,
    cache_dir: Optional[str] = None,
    latest_close: Optional[Tuple[str, float]] = None,
) -> Optional[float]:
    """`momentum_5d_detail` 的数值部分；算不出返回 None。参数见那里。"""
    r = momentum_5d_detail(ticker, cache_dir, latest_close)
    return None if r is None else r["momentum_5d"]


def momentum_5d_detail(
    ticker: str,
    cache_dir: Optional[str] = None,
    latest_close: Optional[Tuple[str, float]] = None,
) -> Optional[Dict]:
    """自攒收盘价算「5 日动量」（%）→ `{"momentum_5d", "as_of", "anchor"}`。
    算不出就返回 None——**绝不返回 0.0**。

    窗口 = **4 个交易日**（`MOMENTUM_LOOKBACK`，v0.45.469），与主路径 yfinance
    `Close.iloc[-1] / Close.iloc[-5]` 同口径——它是主路径缺数时的回落，必须量同一个量。

    `as_of` = 窗口末端那一天（序列最新一点），`anchor` = 起点。

    ⚠️ 窗口末端（v0.45.465）：不传 `latest_close` 时末端是**库里最新的一天**——扫描日
    取数时那是上一个扫描日（当天 DB 行在取数之后才写）。yfinance 收盘后的日线含当天那根，
    两者差一个扫描日：09-28~10-09 回放 300 对，平均 |Δ| 3.79pp、符号一致 66%；补上当天
    收盘后 1.91pp / 83%（VZ 10-09 当天跌约 10%：yfinance −9.16%，滞后的回落 +0.80%）。
    所以扫描路径要把**已核实**的当场官方收盘经 `latest_close` 传进来；调用方拿 `as_of`
    与自己的场次日期对账，不一致就是那一份没进窗口。

    0.0 的含义是"持平"，会让 sentiment.py 的背离检测永远不触发
    （实测 395/395 次 severity=0）。缺数据必须是 None。

    ⚠️ 关键：索引只有"扫描跑过的日子"，漏跑的交易日在索引里根本不存在。
    所以**不能简单往回数 5 条**——NVDA 实测往回 5 条跨越的是 9 个交易日，
    算出来的数字挂着"5 日动量"的名字，实际是 9 日动量。这里按真实交易日
    距离挑锚点，偏离过大宁可返回 None，也不出一个名不副实的数。

    `cache_dir=None` ⇒ `PATHS.cache_dir`（调用时求值）。
    """
    hist = load_price_history(ticker, cache_dir, max_days=40, latest_close=latest_close)
    if len(hist) < 2:
        return None
    latest_date, latest = hist[-1]
    if latest <= 0:
        return None

    # 从近往远找第一个距离 ≥ MOMENTUM_LOOKBACK 个交易日的锚点
    for date, px in reversed(hist[:-1]):
        gap = _trading_days_between(date, latest_date)
        if gap < 0:
            continue
        if gap >= MOMENTUM_LOOKBACK:
            if gap > MOMENTUM_MAX_GAP or px <= 0:
                return None  # 数据太稀，名不副实
            return {"momentum_5d": round((latest / px - 1) * 100, 4),
                    "as_of": latest_date, "anchor": date}
    return None  # 历史不够 MOMENTUM_LOOKBACK 个交易日


def coverage_report(cache_dir: Optional[str] = None) -> Dict[str, int]:
    """各标的已攒天数，按天数降序。索引与快照取并集。`cache_dir=None` ⇒ `PATHS.cache_dir`。"""
    if cache_dir is None:
        cache_dir = _default_cache_dir()
    tickers: Dict[str, None] = {}
    for path in glob.glob(os.path.join(cache_dir, "price_history_*.jsonl")):
        name = os.path.basename(path)
        tickers.setdefault(name[len("price_history_"):-len(".jsonl")], None)
    for path in glob.glob(os.path.join(cache_dir, "options_snapshot_*.json")):
        m = _SNAP_RE.search(os.path.basename(path))
        if m:
            tickers.setdefault(m.group(1), None)
    rep = {t: len(load_price_history(t, cache_dir)) for t in tickers}
    return dict(sorted(rep.items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":  # pragma: no cover
    import sys
    cd = sys.argv[1] if len(sys.argv) > 1 else _default_cache_dir()
    rep = coverage_report(cd)
    ready = sum(1 for n in rep.values() if n >= MIN_DAYS_FOR_MOMENTUM)
    print(f"标的 {len(rep)} 只，其中 {ready} 只已够算 5 日动量（≥{MIN_DAYS_FOR_MOMENTUM} 天）")
    for t, n in list(rep.items())[:10]:
        m = momentum_5d(t, cd)
        print(f"  {t:<6} {n:>3} 天   5日动量 = {'—' if m is None else f'{m:+.2f}%'}")
