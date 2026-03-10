#!/usr/bin/env python3
"""
🐝 Alpha Hive - T+N 实际价格回填器 (Phase 6 闭环核心)

从 report_snapshots/*.json 中找出 actual_price_t1/t7/t30 为 null 的快照，
用 yfinance 回填实际价格，并更新 MemoryStore 中的 outcome 数据。

数据流:
  report_snapshots/*.json  →  actual_prices 回填
  MemoryStore.agent_memory →  outcome / return_t1 / t7 / t30 更新
"""

import logging as _logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

_log = _logging.getLogger("alpha_hive.outcomes_fetcher")

# ── 交易日计算（复用 backtester.py 模式） ──
try:
    import pandas as _pd
    from pandas.tseries.holiday import USFederalHolidayCalendar as _USCal
    from pandas.tseries.offsets import CustomBusinessDay as _CBDay
    _US_BDAY = _CBDay(calendar=_USCal())
    _BDAY_AVAILABLE = True
except Exception:
    _BDAY_AVAILABLE = False

try:
    import yfinance as _yf
except ImportError:
    _yf = None


class OutcomesFetcher:
    """T+1 / T+7 / T+30 实际价格回填器"""

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
        max_snapshots: int = 50,
    ):
        """
        Args:
            snapshots_dir: report_snapshots 目录路径
            memory_store: MemoryStore 实例（可选，用于回写 agent_memory）
            rate_limit: yfinance 请求间隔（秒）
            max_snapshots: 单次最多处理快照数
        """
        self.snapshots_dir = snapshots_dir
        self.memory_store = memory_store
        self.rate_limit = rate_limit
        self.max_snapshots = max_snapshots

    # ──────────────────── 公开接口 ────────────────────

    def run(self) -> Dict[str, int]:
        """主入口：扫描 → 回填 → 更新 MemoryStore

        Returns:
            {"scanned": int, "updated": int, "memory_updated": int, "errors": int}
        """
        stats = {"scanned": 0, "updated": 0, "memory_updated": 0, "errors": 0}

        pending = self._scan_pending()
        stats["scanned"] = len(pending)
        if not pending:
            _log.debug("OutcomesFetcher: 无待回填快照")
            return stats

        _log.info("OutcomesFetcher: 发现 %d 个待回填快照", len(pending))

        for filepath, snapshot, missing_offsets in pending:
            try:
                prices = {}
                for offset in missing_offsets:
                    price = self._fetch_price(snapshot.ticker, snapshot.date, offset)
                    if price is not None:
                        prices[offset] = price
                    if self.rate_limit > 0:
                        time.sleep(self.rate_limit)

                if self._update_snapshot(filepath, snapshot, prices):
                    stats["updated"] += 1

                mem_count = self._update_memory_store(snapshot, prices)
                stats["memory_updated"] += mem_count

            except Exception as e:
                _log.warning("OutcomesFetcher: %s 处理失败: %s", filepath, e)
                stats["errors"] += 1

        _log.info(
            "OutcomesFetcher 完成: scanned=%d updated=%d memory=%d errors=%d",
            stats["scanned"], stats["updated"], stats["memory_updated"], stats["errors"],
        )
        return stats

    # ──────────────────── 内部方法 ────────────────────

    def _scan_pending(self) -> List[Tuple[str, object, List[int]]]:
        """扫描需要回填价格的快照

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

            for offset, attr in self._OFFSETS.items():
                if getattr(snap, attr, None) is not None:
                    continue  # 已有价格，跳过
                # 目标日期是否已过？（保守用自然日 + 余量）
                target_date = report_date + timedelta(days=int(offset * 1.5) + 2)
                if today >= target_date:
                    missing.append(offset)

            if missing:
                results.append((fpath, snap, missing))

            if len(results) >= self.max_snapshots:
                break

        return results

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
        if _yf is None:
            return None

        try:
            start = datetime.strptime(report_date, "%Y-%m-%d")
            if _BDAY_AVAILABLE:
                target_ts = _pd.Timestamp(start) + offset_days * _US_BDAY
                target_date = target_ts.to_pydatetime()
            else:
                # 降级：自然日偏移 × 1.5
                target_date = start + timedelta(days=int(offset_days * 1.5))

            end_date = target_date + timedelta(days=10)

            stock = _yf.Ticker(ticker)
            hist = stock.history(
                start=target_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
            )

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

        t1_ret = ((prices[1] - entry) / entry) if 1 in prices else None
        t7_ret = ((prices[7] - entry) / entry) if 7 in prices else None
        t30_ret = ((prices[30] - entry) / entry) if 30 in prices else None

        # 确定 outcome（基于 T+7 方向准确性）
        outcome = self._determine_outcome(snapshot.direction, t7_ret)

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
