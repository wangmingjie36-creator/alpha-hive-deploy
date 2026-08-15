#!/usr/bin/env python3
"""
自攒 IV 历史（v0.43.18）
========================
真 IV Rank 需要"过去 N 天期权市场每天的隐含波动率"。免费数据源（yfinance /
CBOE 延迟报价）只暴露**当前**期权链，无法回查历史 IV，付费源（ORATS /
IVolatility / CBOE DataShop）才有。因此唯一可行路径是**自己每天记一笔**。

存储：每票一个追加式紧凑索引 `cache/iv_history_{TICKER}.jsonl`（一行一天
`{"date","iv"}`）。数据源是 options_analyzer 每次分析写入的 `iv_raw_observed`
（降级前的当日真实观测 IV，v0.43.18 起）。

v0.43.21 起**读取只走索引**：此前每次都全量解析该票所有期权快照（单票
1120KB）只为取出几十个 float，30 只/扫描 680ms 且随天数线性增长。快照扫描
现已降级为一次性迁移路径（sentinel `.iv_index_migrated_{TICKER}` 控制），
不再进日常热路径。

⚠️ 为什么不用快照里的 `iv_current`：
扫描固定在收盘后运行 → `_market_open` 恒 False → options_analyzer 的降级
分支每次都用 `last_valid_iv` 缓存（TTL 120h），当日真实值被丢弃。实测
NVDA 76 份快照里 `iv_current` 只有 15 个不同值（≈76/5，与 120h TTL 精确
吻合）——那是一条"5 天一阶的阶梯"，拿它算 min/max 会严重失真。
`iv_raw_observed` 才是每日独立观测。

数据从 v0.43.18 上线日开始积累，此前的快照没有该字段（会被自动跳过）。
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import List, Optional, Tuple

try:
    from hive_logger import get_logger
    _log = get_logger("alpha_hive.iv_history")
except ImportError:  # pragma: no cover - 独立运行兜底
    import logging
    _log = logging.getLogger("alpha_hive.iv_history")

# 标准 IV Rank 用 252 个交易日（1 年）。样本不足时退回 HV 代理，绝不假装。
IV_RANK_FULL_WINDOW = 252
# 最低可用样本：63 个交易日 ≈ 3 个月，是业界公认的"季度 IV Rank"变体下限。
# 低于此值 rank 的分辨率太粗（1/N），宁可诚实降级也不出一个看着像真的假数。
IV_RANK_MIN_DAYS = 63

_SNAP_RE = re.compile(r"options_snapshot_(.+)_(\d{4}-\d{2}-\d{2})\.json$")
# IV 合理区间，与 options_analyzer 的 _MIN/_MAX_VALID_IV 保持一致
_MIN_VALID_IV = 5.0
_MAX_VALID_IV = 150.0


def _index_path(ticker: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"iv_history_{ticker}.jsonl")


def append_observation(ticker: str, cache_dir: str, date: str, iv: Optional[float]) -> bool:
    """把当日真实观测 IV 追加进紧凑索引（每票一个 JSONL，一行一天）。

    v0.43.21：此前 `load_iv_history` 直接 glob + 全量解析该票的所有期权快照
    （每份 ~46KB）只为取出一个 float——单票 4.3MB / 30 只一次扫描 ~680ms，
    且随天数线性增长（攒满 252 天约 2.3s）且永不回落。改为写紧凑索引后，
    读取成本与快照体积解耦。

    幂等：同一天重复调用只保留最后一次（同日重跑/补跑不会产生重复行；
    读取端亦按日期去重，后写覆盖）。

    Returns: True=已写入 / False=跳过（值无效或当日已记录同值）
    """
    if iv is None:
        return False
    try:
        iv = float(iv)
    except (TypeError, ValueError):
        return False
    if not (_MIN_VALID_IV <= iv <= _MAX_VALID_IV):
        return False

    path = _index_path(ticker, cache_dir)
    try:
        # 幂等检查：末行已是同日同值则不重复写（避免同日多次扫描堆积冗余行）
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                last = None
                for last in f:  # noqa: B007 - 只要最后一行
                    pass
            if last:
                try:
                    rec = json.loads(last)
                    if rec.get("date") == date and abs(float(rec.get("iv", -1)) - iv) < 1e-9:
                        return False
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass  # 末行损坏不阻断写入

        os.makedirs(cache_dir, exist_ok=True)
        # 单次 write 一整行：POSIX 追加模式下短写入实质原子，多票并行互不干扰
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"date": date, "iv": round(iv, 4)}, ensure_ascii=False) + "\n")
        return True
    except OSError as e:
        _log.debug("[%s] IV 索引写入失败（不阻断评分）: %s", ticker, e)
        return False


def _read_index(ticker: str, cache_dir: str) -> dict:
    """读紧凑索引 → {date: iv}。同日多行取最后一行（后写覆盖）。"""
    path = _index_path(ticker, cache_dir)
    if not os.path.exists(path):
        return {}
    by_date: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    d, iv = rec.get("date"), float(rec.get("iv"))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue  # 跳过损坏行，不让单行错误废掉整个索引
                if d and _MIN_VALID_IV <= iv <= _MAX_VALID_IV:
                    by_date[d] = iv
    except OSError as e:
        _log.debug("[%s] IV 索引读取失败: %s", ticker, e)
        return {}
    return by_date


def _scan_snapshots(ticker: str, cache_dir: str) -> dict:
    """慢路径：从期权快照全量扫出 iv_raw_observed → {date: iv}。

    仅用于索引缺失时的一次性重建（或索引被误删后的自愈），不进日常热路径。
    """
    by_date: dict = {}
    for path in glob.glob(os.path.join(cache_dir, f"options_snapshot_{ticker}_*.json")):
        m = _SNAP_RE.search(os.path.basename(path))
        if not m or m.group(1) != ticker:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        iv = snap.get("iv_raw_observed")
        if iv is None:
            continue
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            continue
        if _MIN_VALID_IV <= iv <= _MAX_VALID_IV:
            by_date[m.group(2)] = iv
    return by_date


def _migration_marker(ticker: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f".iv_index_migrated_{ticker}")


def merge_snapshots_into_index(ticker: str, cache_dir: str) -> int:
    """把快照里的 iv_raw_observed **合并**进索引（冲突时索引优先）。返回索引总条数。

    ⚠️ 用途仅限**一次性迁移/自愈**，绝不能进日常热路径——它要全量解析该票
    所有快照（单票数 MB）。由 `load_iv_history` 用 sentinel 文件保证每票只跑
    一次；跑过之后即便快照数远多于索引条数（老快照没有 iv_raw_observed 字段，
    永远不会被收录）也不再重复扫描。

    为什么必须是"合并"而非"覆盖"：analyze() 会先 append 当日观测再读取历史，
    若此处覆盖，刚写入的当日记录会被抹掉。
    """
    by_date = _scan_snapshots(ticker, cache_dir)
    by_date.update(_read_index(ticker, cache_dir))  # 索引更权威，覆盖同日快照值
    if not by_date:
        return 0
    path = _index_path(ticker, cache_dir)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for d in sorted(by_date):
                f.write(json.dumps({"date": d, "iv": round(by_date[d], 4)}, ensure_ascii=False) + "\n")
        os.replace(tmp, path)  # 原子替换，避免半截文件被读到
        _log.info("[%s] IV 索引迁移完成：%d 条", ticker, len(by_date))
        return len(by_date)
    except OSError as e:
        _log.debug("[%s] IV 索引迁移失败: %s", ticker, e)
        return 0


def load_iv_history(
    ticker: str,
    cache_dir: str,
    max_days: int = IV_RANK_FULL_WINDOW,
) -> Tuple[List[float], int]:
    """读取该标的的真实观测 IV 序列（按日期升序）。

    v0.43.21 读取策略：**紧凑索引优先**（`iv_history_{TICKER}.jsonl`）。
    索引缺失时自动从快照重建一次（自愈），重建后仍走索引。日常热路径不再
    解析任何快照——此前每次调用要全量解析该票所有快照（单票 4.3MB）只为
    取出几十个 float。

    Args:
        ticker: 标的代码
        cache_dir: 缓存目录（通常是 OptionsDataFetcher.cache_dir）
        max_days: 最多回看多少个交易日（默认 252）

    Returns:
        (iv_list, n_days) —— iv_list 按日期升序，每个交易日至多一条；
        n_days 是实际可用的独立观测天数。数据不足时返回 ([], 0)。
    """
    # 一次性迁移：把 v0.43.21 之前只存在于快照里的观测并入索引。
    # 用 sentinel 文件保证每票只跑一次——不能用"索引为空"当条件：analyze()
    # 会先 append 当日观测再读历史，索引因此恒非空，历史将被永久忽略
    # （本设计缺陷由 test_uses_real_iv_when_enough_history 抓出）。
    _marker = _migration_marker(ticker, cache_dir)
    if not os.path.exists(_marker):
        merge_snapshots_into_index(ticker, cache_dir)
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(_marker, "w", encoding="utf-8") as f:
                f.write("v0.43.21\n")
        except OSError:
            pass  # 标记写不成只会导致下次重复迁移，不影响正确性

    by_date = _read_index(ticker, cache_dir)
    if not by_date:
        return [], 0

    ordered = [by_date[d] for d in sorted(by_date)][-max_days:]
    return ordered, len(ordered)


def iv_rank_from_history(
    current_iv: float,
    iv_history: List[float],
) -> Tuple[Optional[float], Optional[float]]:
    """用真实 IV 历史算 IV Rank / IV Percentile。

    与 options_analyzer.calculate_iv_rank 同口径（min-max 归一），
    但分母是**真实观测 IV**而非 HV 代理。

    Returns:
        (iv_rank, iv_percentile)；样本不足或退化时返回 (None, None)，
        由调用方决定是否降级——本函数不编造中性值。
    """
    if not iv_history or len(iv_history) < 2 or current_iv is None:
        return None, None

    lo, hi = min(iv_history), max(iv_history)
    if hi <= lo:
        return None, None  # 全部相同 → 无法排名，诚实返回 None

    iv_rank = max(0.0, min(100.0, (current_iv - lo) / (hi - lo) * 100))
    below = sum(1 for v in iv_history if v < current_iv)
    iv_percentile = below / len(iv_history) * 100
    return round(iv_rank, 2), round(iv_percentile, 2)


def coverage_report(cache_dir: str) -> dict:
    """自攒进度报告：各标的已积累多少个交易日的真实 IV 观测。"""
    tickers: dict = {}
    # 索引是 v0.43.21 起的唯一真相源，必须枚举（快照可能被清理或从无该字段）
    for path in glob.glob(os.path.join(cache_dir, "iv_history_*.jsonl")):
        name = os.path.basename(path)
        tickers.setdefault(name[len("iv_history_"):-len(".jsonl")], None)
    # 快照仍需枚举：尚未迁移过的票只在快照里有数据
    for path in glob.glob(os.path.join(cache_dir, "options_snapshot_*.json")):
        m = _SNAP_RE.search(os.path.basename(path))
        if not m:
            continue
        tickers.setdefault(m.group(1), None)

    out = {}
    for tk in tickers:
        _, n = load_iv_history(tk, cache_dir)
        out[tk] = n
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


if __name__ == "__main__":  # pragma: no cover
    import sys
    _dir = sys.argv[1] if len(sys.argv) > 1 else "cache"
    rep = coverage_report(_dir)
    ready = {k: v for k, v in rep.items() if v >= IV_RANK_MIN_DAYS}
    print(f"自攒 IV 历史进度（阈值 {IV_RANK_MIN_DAYS} 个交易日）")
    print(f"  已达标: {len(ready)}/{len(rep)} 只")
    for tk, n in list(rep.items())[:15]:
        flag = "✅" if n >= IV_RANK_MIN_DAYS else f"({IV_RANK_MIN_DAYS - n} 天后可用)"
        print(f"  {tk:6s} {n:4d} 天 {flag}")
