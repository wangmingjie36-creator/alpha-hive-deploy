#!/usr/bin/env python3
"""
🐝 Alpha Hive - T+N 实际价格回填器 (Phase 6 闭环核心)

从 report_snapshots/*.json 中找出 actual_price_t1/t7/t30 为 null 的快照，
用 yfinance 回填实际价格，并更新 MemoryStore 中的 outcome 数据。

数据流:
  report_snapshots/*.json  →  actual_prices 回填
  MemoryStore.agent_memory →  outcome / return_t1 / t7 / t30 更新
"""

import json
import logging as _logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

_log = _logging.getLogger("alpha_hive.outcomes_fetcher")

# ── 交易日计算：NYSE 日历的唯一实现（v0.45.520 前是 pandas 联邦假日表，哥伦布 / 退伍军人日判休、受难日判开）──
from is_trading_day import add_trading_days

try:
    import yfinance as _yf
except ImportError:
    _yf = None


# 偏移天数 → 快照里 actual_prices 的子键
_OFFSET_KEYS = {1: "t1", 7: "t7", 30: "t30"}

#: 待回填里最老的一格逾期超过这么多自然日 ⇒ 队列没在排空（alert_manager P2）。
#: 正常节奏下到期当天的扫描就会补上（本地快照价覆盖绝大多数），周末 / 假日 / 一两次取数失败最多拖几天。
MAX_OVERDUE_DAYS = 7


def _due_date(report_date, offset: int):
    """某一格从哪天起「该回填了」：T+N 交易日 ≈ N×1.4 自然日，再加 3 天缓冲（修复 Bug #22：覆盖连续节假日 / 周末堆积）。"""
    return report_date + timedelta(days=int(round(offset * 1.4)) + 3)


def _is_rate_limit(e: Exception) -> bool:
    msg = str(e)
    return (type(e).__name__ == "YFRateLimitError"
            or "Rate limited" in msg or "Too Many Requests" in msg)


def backlog_summary(snapshots_dir: str, today=None) -> Dict:
    """回填队列现状（只读，v0.45.493）：待回填几份 / 几格、最老一格逾期几天。

    给 `alpha_hive_daily_report.main` 写进 `scan_timing.extra.outcomes_backlog` → status.json → alert_manager。
    看**最老一格的逾期天数**而不是队列长度：回填在扫描后台线程里跑，写 scan_timing 时它可能还没跑完，
    长度会随时序抖；逾期天数不受影响——队列在排空时它停在 0~2，饿死时它只增不减。
    读原文 JSON（不经 ReportSnapshot），坏文件计数、不抛。
    """
    today = today or datetime.now().date()
    files = offsets = bad = 0
    oldest = None   # (逾期天数, "TICKER_date tN")
    if not os.path.isdir(snapshots_dir):
        return {"available": False, "reason": f"快照目录不存在：{snapshots_dir}"}
    for fname in os.listdir(snapshots_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(snapshots_dir, fname), encoding="utf-8") as f:
                d = json.load(f)
            report_date = datetime.strptime(d["date"], "%Y-%m-%d").date()
            ap = d.get("actual_prices") or {}
        except (OSError, ValueError, KeyError, TypeError):
            bad += 1
            continue
        n = 0
        for off, key in _OFFSET_KEYS.items():
            if ap.get(key) is not None:
                continue
            due = _due_date(report_date, off)
            if today >= due:
                n += 1
                late = (today - due).days
                if oldest is None or late > oldest[0]:
                    oldest = (late, f"{fname[:-5]} {key}")
        if n:
            files += 1
            offsets += n
    return {"available": True, "pending_files": files, "pending_offsets": offsets,
            "oldest_overdue_days": oldest[0] if oldest else 0,
            "oldest_item": oldest[1] if oldest else None,
            "max_overdue_days": MAX_OVERDUE_DAYS, "unreadable_files": bad}


class OutcomesFetcher:
    """T+1 / T+7 / T+30 实际价格回填器

    v0.45.493 队列饿死根治：
      · 本地快照价（不出网）不再受任何配额限制、也不 sleep——旧实现每轮只处理 50 份「文件」，且每查一格都 sleep，
        而 9 成的格子本地就能查到；30 只标的 × 3 个期限每天最多到期 ~90 次 > 50 ⇒ 队列只增不减。
      · 配额只管真正出网的 yfinance 请求（`max_network_fetches`），sleep 也只跟在出网请求后面。
      · 待回填按**到期先后**排（最老的先），不再按文件名——旧实现每轮都从字母表开头重来，T / TMUS / TSLA / VKTX /
        VZ / WMT / XOM 09 月起一份都没回填过，日志却每轮 `updated=50 errors=0`。
      · 连续限流只停出网，本地回填照做完。
    """

    # 偏移天数 → 快照字段映射
    _OFFSETS = {
        1:  "actual_price_t1",
        7:  "actual_price_t7",
        30: "actual_price_t30",
    }

    def __init__(
        self,
        snapshots_dir: str,
        memory_store=None,
        rate_limit: float = 0.5,
        max_network_fetches: int = 50,
    ):
        """
        Args:
            snapshots_dir: report_snapshots 目录路径
            memory_store: MemoryStore 实例（可选，用于回写 agent_memory）
            rate_limit: yfinance 请求间隔（秒）；只跟在真正出网的请求后面
            max_network_fetches: 单次最多发几次 yfinance 请求（本地快照价不计）；用完的格子留到下一轮，排在最前
        """
        self.snapshots_dir = snapshots_dir
        self.memory_store = memory_store
        self.rate_limit = rate_limit
        self.max_network_fetches = max_network_fetches
        # {ticker: {date_str: entry_price}} 本地快照价格索引（懒加载）
        self._price_index_cache: Dict[str, Dict[str, float]] = {}

    # ──────────────────── 公开接口 ────────────────────

    def run(self) -> Dict[str, int]:
        """主入口：扫描 → 回填 → 更新 MemoryStore

        Returns:
            {"scanned": int, "updated": int, "memory_updated": int, "errors": int}
        """
        stats = {"scanned": 0, "updated": 0, "memory_updated": 0, "errors": 0,
                 "local_hits": 0, "network_fetches": 0, "network_deferred": 0, "unresolved": 0}

        pending = self._scan_pending()
        stats["scanned"] = len(pending)
        if not pending:
            _log.debug("OutcomesFetcher: 无待回填快照")
            return stats

        _log.info("OutcomesFetcher: 发现 %d 个待回填快照", len(pending))

        _rl_streak = 0  # 连续 yfinance 限流计数：达阈值则本轮不再出网（本地回填照做）
        network_ok = _yf is not None   # 没装 yfinance ⇒ 要出网的格子全部记 deferred，不假装「试过了」
        for filepath, snapshot, missing_offsets in pending:
            try:
                prices = {}
                for offset in missing_offsets:
                    price = self._fetch_local(snapshot.ticker, snapshot.date, offset)
                    if price is not None:
                        stats["local_hits"] += 1
                    elif not network_ok or stats["network_fetches"] >= self.max_network_fetches:
                        stats["network_deferred"] += 1   # 留到下一轮（按到期先后排，下一轮排在最前）
                        continue
                    else:
                        stats["network_fetches"] += 1
                        try:
                            price = self._fetch_remote(snapshot.ticker, snapshot.date, offset)
                            _rl_streak = 0
                        except Exception as e:  # noqa: BLE001 —— 只吞限流，其余照抛给下面的整份处理
                            if not _is_rate_limit(e):
                                raise
                            stats["errors"] += 1
                            stats["network_deferred"] += 1
                            _rl_streak += 1
                            if _rl_streak >= 3:
                                network_ok = False
                                _log.warning(
                                    "OutcomesFetcher: 连续 %d 次 yfinance 限流，本轮不再出网"
                                    "（本地快照价照常回填；要出网的格子下次运行时排在最前）", _rl_streak)
                            else:
                                _log.warning("OutcomesFetcher: %s +%d 限流跳过（连击 %d/3）",
                                             filepath, offset, _rl_streak)
                            continue
                        finally:
                            if self.rate_limit > 0:
                                time.sleep(self.rate_limit)
                    if price is not None:
                        prices[offset] = price
                    else:
                        stats["unresolved"] += 1

                if self._update_snapshot(filepath, snapshot, prices):
                    stats["updated"] += 1

                mem_count = self._update_memory_store(snapshot, prices)
                stats["memory_updated"] += mem_count

            except Exception as e:  # noqa: BLE001 —— 单份失败不拖垮整轮，计数并说出来
                stats["errors"] += 1
                _log.warning("OutcomesFetcher: %s 处理失败: %s", filepath, e)

        _log.info(
            "OutcomesFetcher 完成: scanned=%d updated=%d memory=%d errors=%d "
            "local=%d network=%d deferred=%d unresolved=%d",
            stats["scanned"], stats["updated"], stats["memory_updated"], stats["errors"],
            stats["local_hits"], stats["network_fetches"], stats["network_deferred"], stats["unresolved"],
        )
        return stats

    # ──────────────────── 内部方法 ────────────────────

    def _scan_pending(self) -> List[Tuple[str, object, List[int]]]:
        """扫描需要回填价格的快照——**全部**，按最早到期的那一格从老到新排（v0.45.493）

        不截断：截断只该落在出网请求上（见 run）。旧实现按文件名排、取前 50 份，字母序靠后的标的永远轮不到。

        Returns:
            [(filepath, ReportSnapshot, [missing_offset_days, ...])]
        """
        from feedback_loop import ReportSnapshot

        if not os.path.isdir(self.snapshots_dir):
            return []

        today = datetime.now().date()
        results = []

        for fname in sorted(os.listdir(self.snapshots_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(self.snapshots_dir, fname)
            try:
                snap = ReportSnapshot.load_from_json(fpath)
            except (ValueError, KeyError, OSError) as e:
                _log.debug("跳过无法加载的快照 %s: %s", fname, e)
                continue

            # 找出需要回填的偏移天数
            missing = []
            try:
                report_date = datetime.strptime(snap.date, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            first_due = None
            for offset, attr in self._OFFSETS.items():
                if getattr(snap, attr, None) is not None:
                    continue  # 已有价格，跳过
                due = _due_date(report_date, offset)
                if today >= due:
                    missing.append(offset)
                    first_due = due if first_due is None else min(first_due, due)

            if missing:
                results.append((first_due, fname, (fpath, snap, missing)))

        results.sort(key=lambda r: (r[0], r[1]))
        return [r[2] for r in results]

    def _load_price_index(self, ticker: str) -> Dict[str, float]:
        """构建 {date_str: entry_price} 本地价格索引（来自 report_snapshots）。

        每份 {ticker}_{YYYY-MM-DD}.json 的 entry_price 即该交易日收盘价，
        所有快照拼成一条按日期可查的本地价格序列。结果按 ticker 缓存。
        """
        if ticker in self._price_index_cache:
            return self._price_index_cache[ticker]

        index: Dict[str, float] = {}
        snaps: List[dict] = []
        try:
            if os.path.isdir(self.snapshots_dir):
                prefix = "%s_" % ticker
                for fname in os.listdir(self.snapshots_dir):
                    if not (fname.startswith(prefix) and fname.endswith(".json")):
                        continue
                    fpath = os.path.join(self.snapshots_dir, fname)
                    try:
                        with open(fpath) as f:
                            snap = json.load(f)
                        snaps.append(snap)
                        d = snap.get("date")
                        px = snap.get("entry_price")
                        # 主来源：当日 entry_price 即收盘价（过滤 0.0 坏数据）
                        if d and isinstance(px, (int, float)) and px > 0:
                            index[str(d)[:10]] = float(px)
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        continue
        except OSError:
            pass

        # 补缺：用已回填的 actual_prices(t1/t7/t30) 反推目标交易日，
        # 填补 entry_price=0.0 留下的空洞（如 NVDA_2026-03-25）。entry_price 优先，不覆盖。
        def _target_key(base_str: str, off: int) -> Optional[str]:
            try:
                base = datetime.strptime(str(base_str)[:10], "%Y-%m-%d")
            except (ValueError, TypeError):
                return None
            return add_trading_days(base, off).strftime("%Y-%m-%d")

        for snap in snaps:
            base = snap.get("date")
            ap = snap.get("actual_prices") or {}
            if not base or not isinstance(ap, dict):
                continue
            for off in (1, 7, 30):
                val = ap.get("t%d" % off)
                if not isinstance(val, (int, float)) or val <= 0:
                    continue
                key = _target_key(base, off)
                if key and key not in index:
                    index[key] = float(val)

        self._price_index_cache[ticker] = index
        return index

    def _lookup_local_price(
        self, ticker: str, target_date: datetime, end_date: datetime
    ) -> Optional[float]:
        """从本地快照价格索引取目标日（或其后首个有快照的交易日）的收盘价。

        镜像 yfinance 行为：取 [target_date, end_date] 窗口内最早一个有价格的日期。
        无任何覆盖时返回 None，由调用方决定是否回退 yfinance。
        """
        index = self._load_price_index(ticker)
        if not index:
            return None
        cur = target_date.date() if hasattr(target_date, "date") else target_date
        last = end_date.date() if hasattr(end_date, "date") else end_date
        while cur <= last:
            key = cur.strftime("%Y-%m-%d")
            if key in index:
                return index[key]
            cur += timedelta(days=1)
        return None

    def _fetch_price(
        self, ticker: str, report_date: str, offset_days: int
    ) -> Optional[float]:
        """获取预测日后 N 个交易日的收盘价（复用 backtester 模式）

        Args:
            ticker: 股票代码
            report_date: 预测日期 "YYYY-MM-DD"
            offset_days: 交易日偏移（1/7/30）

        Returns:
            收盘价 or None
        """
        # ① 本地快照优先：目标日的收盘价 = 该日快照的 entry_price，
        #    无需联网。覆盖不到时（如 T+30 尚无未来快照）再走 yfinance。
        _local = self._fetch_local(ticker, report_date, offset_days)
        if _local is not None:
            return _local
        return self._fetch_remote(ticker, report_date, offset_days)

    def _target_window(self, ticker: str, report_date: str, offset_days: int):
        """(目标交易日, 窗口末日)；日期解析失败 ⇒ None。"""
        try:
            start = datetime.strptime(report_date, "%Y-%m-%d")
            target_date = add_trading_days(start, offset_days)
            return target_date, target_date + timedelta(days=10)
        except (ValueError, TypeError) as e:
            _log.debug("日期解析失败 %s +%dd: %s", ticker, offset_days, e)
            return None

    def _fetch_local(self, ticker: str, report_date: str, offset_days: int) -> Optional[float]:
        """只查本地快照价（不出网、不受配额、不 sleep）。"""
        win = self._target_window(ticker, report_date, offset_days)
        if win is None:
            return None
        return self._lookup_local_price(ticker, *win)

    def _fetch_remote(self, ticker: str, report_date: str, offset_days: int) -> Optional[float]:
        """yfinance 取价。限流异常照抛（run 据此计连击）；其它网络 / 解析错误 ⇒ None。"""
        win = self._target_window(ticker, report_date, offset_days)
        if win is None or _yf is None:
            return None
        target_date, end_date = win

        try:
            stock = _yf.Ticker(ticker)
            import yf_daily
            # v0.45.658：经唯一入口（带日期窗口、按日期用；此前是「dated_window」例外——结构上只核得到 start=，「末行碰不到」核不了）
            hist = yf_daily.history(stock, source="outcomes",
                start=target_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
            )[0]

            if hist.empty:
                return None
            return float(hist["Close"].iloc[0])

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("价格获取失败 %s +%dd: %s", ticker, offset_days, e)
            return None

    def _update_snapshot(
        self, filepath: str, snapshot, prices: Dict[int, float]
    ) -> bool:
        """幂等更新快照 JSON（仅写入之前为 None 的字段）

        Returns:
            是否有字段被更新
        """
        if not prices:
            return False

        changed = False
        for offset, attr in self._OFFSETS.items():
            if offset in prices and getattr(snapshot, attr, None) is None:
                setattr(snapshot, attr, prices[offset])
                changed = True

        if changed:
            try:
                # save_to_json 接受目录而非文件路径
                snapshot.save_to_json(os.path.dirname(filepath))
                _log.debug("快照已更新: %s (回填 %s)", filepath, list(prices.keys()))
            except (OSError, TypeError) as e:
                _log.warning("快照写入失败 %s: %s", filepath, e)
                return False

        return changed

    def _update_memory_store(self, snapshot, prices: Dict[int, float]) -> int:
        """更新 MemoryStore 中对应的 agent_memory 记录

        Returns:
            更新的记录数
        """
        if not self.memory_store or not prices:
            return 0

        # 计算收益率
        entry = snapshot.entry_price
        if not entry or entry <= 0:
            return 0

        # 本轮回填到的 + 快照里早已存好的，一起写（v0.45.527）：`prices` 只含本轮到期的 offset，
        # 只拿它算 ⇒ 缺的 horizon 是 None，旧 SQL 会把前几轮写好的覆写成 NULL。快照是这几列的唯一来源，
        # 每轮按它整组重写还顺带把仍在途、曾被抹掉的行补回来。存储层另有 COALESCE 兜底。
        rets: Dict[int, Optional[float]] = {}
        for off, attr in self._OFFSETS.items():
            px = prices[off] if off in prices else getattr(snapshot, attr, None)
            rets[off] = ((px - entry) / entry) if isinstance(px, (int, float)) and px > 0 else None
        t1_ret, t7_ret, t30_ret = rets[1], rets[7], rets[30]

        # 确定 outcome（基于 T+7 方向准确性）；还没有 T+7 ⇒ None（存储层保留原值），
        # 不写 "neutral"——「还判不了」不等于「判为中性」（读者曾把 neutral 计进准确率分母，v0.45.527）
        outcome = self._determine_outcome(snapshot.direction, t7_ret) if t7_ret is not None else None

        # 按 (date, ticker) 匹配 agent_memory 中的记录
        updated = 0
        try:
            memory_ids = self._find_memory_ids(snapshot.ticker, snapshot.date)
            for mid in memory_ids:
                if self.memory_store.update_memory_outcome(
                    mid, outcome, t1_ret, t7_ret, t30_ret
                ):
                    updated += 1
        except (OSError, ValueError, TypeError, AttributeError) as e:
            _log.debug("MemoryStore 更新跳过 (%s/%s): %s",
                       snapshot.ticker, snapshot.date, e)

        return updated

    def _determine_outcome(
        self, direction: str, return_pct: Optional[float]
    ) -> str:
        """判断预测方向是否正确（方案12: 统一标准）

        Args:
            direction: "Long" / "Short" / "Neutral"
            return_pct: T+7 收益率（小数，如 0.05 = 5%）

        Returns:
            "correct" / "incorrect" / "neutral"
        """
        if return_pct is None or direction == "Neutral":
            return "neutral"

        # 方案12: 使用共享判定函数，含 1% 容差
        # return_pct 是小数（0.05 = 5%），需转为百分比
        from outcome_utils import determine_correctness
        return determine_correctness(direction, return_pct * 100)

    def _find_memory_ids(self, ticker: str, date: str) -> List[str]:
        """从 agent_memory 表中查找匹配的 memory_id

        Args:
            ticker: 股票代码
            date: 预测日期

        Returns:
            匹配的 memory_id 列表
        """
        try:
            import sqlite3
            conn = self.memory_store._connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT memory_id FROM agent_memory WHERE ticker = ? AND date = ?",
                (ticker, date),
            )
            return [row[0] for row in cursor.fetchall()]
        except (sqlite3.Error, OSError, AttributeError) as e:
            _log.debug("memory_id 查询失败 (%s/%s): %s", ticker, date, e)
            return []
