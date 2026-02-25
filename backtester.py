#!/usr/bin/env python3
"""
🔄 Alpha Hive 回测反馈循环（Phase 6）

T+1 / T+7 / T+30 自动回看预测偏差：
1. 保存预测：每次扫描后将蜂群评分+方向写入 predictions 表
2. 回测检验：定期检查到期的预测，用 yfinance 获取实际收益率
3. 评估准确率：按 Agent、维度、标的维度统计方向准确率
4. 权重自适应：根据历史准确率自动调整 5 维公式权重
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import yfinance as yf
except ImportError:
    yf = None

from hive_logger import PATHS, get_logger

_log = get_logger("backtester")

DB_PATH = PATHS.db


class PredictionStore:
    """预测记录存储（SQLite）"""

    TABLE = "predictions"

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_table()

    def _init_table(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    date               TEXT NOT NULL,
                    ticker             TEXT NOT NULL,
                    final_score        REAL NOT NULL,
                    direction          TEXT NOT NULL,
                    price_at_predict   REAL,
                    dimension_scores   TEXT,
                    agent_directions   TEXT,
                    -- T+1 回测
                    price_t1           REAL,
                    return_t1          REAL,
                    correct_t1         INTEGER,
                    checked_t1         INTEGER DEFAULT 0,
                    -- T+7 回测
                    price_t7           REAL,
                    return_t7          REAL,
                    correct_t7         INTEGER,
                    checked_t7         INTEGER DEFAULT 0,
                    -- T+30 回测
                    price_t30          REAL,
                    return_t30         REAL,
                    correct_t30        INTEGER,
                    checked_t30        INTEGER DEFAULT 0,
                    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, ticker)
                )
            """)
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_pred_date ON {self.TABLE}(date)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_pred_ticker ON {self.TABLE}(ticker)")
            conn.commit()
            conn.close()
        except (sqlite3.Error, OSError) as e:
            _log.warning("预测表初始化失败: %s", e)

    def save_prediction(
        self,
        ticker: str,
        final_score: float,
        direction: str,
        price: float,
        dimension_scores: Dict = None,
        agent_directions: Dict = None,
    ) -> bool:
        """保存一条预测记录"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(f"""
                INSERT OR REPLACE INTO {self.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 dimension_scores, agent_directions)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d"),
                ticker,
                final_score,
                direction,
                price,
                json.dumps(dimension_scores or {}),
                json.dumps(agent_directions or {}),
            ))
            conn.commit()
            conn.close()
            return True
        except (sqlite3.Error, OSError, TypeError) as e:
            _log.warning("保存预测失败 (%s): %s", ticker, e)
            return False

    def get_pending_checks(self, period: str) -> List[Dict]:
        """
        获取待回测的预测记录

        period: "t1" / "t7" / "t30"
        """
        days_map = {"t1": 1, "t7": 7, "t30": 30}
        days = days_map.get(period, 7)
        checked_col = f"checked_{period}"

        # 目标日期：预测日 + N 天 <= 今天
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT * FROM {self.TABLE}
                WHERE date <= ? AND {checked_col} = 0
                ORDER BY date ASC
            """, (cutoff,)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except (sqlite3.Error, OSError) as e:
            _log.warning("获取待回测记录失败: %s", e)
            return []

    def update_check_result(
        self, pred_id: int, period: str,
        price: float, ret: float, correct: bool
    ) -> bool:
        """更新回测结果"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(f"""
                UPDATE {self.TABLE}
                SET price_{period} = ?, return_{period} = ?,
                    correct_{period} = ?, checked_{period} = 1
                WHERE id = ?
            """, (price, ret, 1 if correct else 0, pred_id))
            conn.commit()
            conn.close()
            return True
        except (sqlite3.Error, OSError) as e:
            _log.warning("更新回测结果失败: %s", e)
            return False

    def get_accuracy_stats(self, period: str = "t7", days: int = 90) -> Dict:
        """
        获取准确率统计

        返回: {
            overall_accuracy, total_checked, correct_count,
            avg_return, by_direction: {bullish: {}, bearish: {}, neutral: {}},
            by_ticker: {NVDA: {}, ...}
        }
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        checked_col = f"checked_{period}"
        correct_col = f"correct_{period}"
        return_col = f"return_{period}"

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # 总体准确率
            row = conn.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM({correct_col}) as correct,
                    AVG({return_col}) as avg_ret,
                    AVG(final_score) as avg_score
                FROM {self.TABLE}
                WHERE {checked_col} = 1 AND date >= ?
            """, (cutoff,)).fetchone()

            total = row["total"] or 0
            correct = row["correct"] or 0
            overall_acc = correct / total if total > 0 else 0.0

            # 按方向分组
            by_direction = {}
            for direction in ["bullish", "bearish", "neutral"]:
                r = conn.execute(f"""
                    SELECT
                        COUNT(*) as total,
                        SUM({correct_col}) as correct,
                        AVG({return_col}) as avg_ret
                    FROM {self.TABLE}
                    WHERE {checked_col} = 1 AND direction = ? AND date >= ?
                """, (direction, cutoff)).fetchone()
                t = r["total"] or 0
                by_direction[direction] = {
                    "total": t,
                    "correct": r["correct"] or 0,
                    "accuracy": (r["correct"] or 0) / t if t > 0 else 0.0,
                    "avg_return": round(r["avg_ret"] or 0, 2),
                }

            # 按标的分组
            by_ticker = {}
            rows = conn.execute(f"""
                SELECT
                    ticker,
                    COUNT(*) as total,
                    SUM({correct_col}) as correct,
                    AVG({return_col}) as avg_ret,
                    AVG(final_score) as avg_score
                FROM {self.TABLE}
                WHERE {checked_col} = 1 AND date >= ?
                GROUP BY ticker
                ORDER BY total DESC
            """, (cutoff,)).fetchall()
            for r in rows:
                t = r["total"] or 0
                by_ticker[r["ticker"]] = {
                    "total": t,
                    "correct": r["correct"] or 0,
                    "accuracy": (r["correct"] or 0) / t if t > 0 else 0.0,
                    "avg_return": round(r["avg_ret"] or 0, 2),
                    "avg_score": round(r["avg_score"] or 0, 1),
                }

            conn.close()

            return {
                "period": period,
                "days_window": days,
                "overall_accuracy": round(overall_acc, 3),
                "total_checked": total,
                "correct_count": correct,
                "avg_return": round(row["avg_ret"] or 0, 3),
                "avg_score": round(row["avg_score"] or 0, 1),
                "by_direction": by_direction,
                "by_ticker": by_ticker,
            }
        except (sqlite3.Error, OSError, KeyError, TypeError) as e:
            _log.warning("获取准确率统计失败: %s", e)
            return {"overall_accuracy": 0, "total_checked": 0}

    def get_all_predictions(self, days: int = 30) -> List[Dict]:
        """获取最近 N 天所有预测"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT * FROM {self.TABLE}
                WHERE date >= ? ORDER BY date DESC, ticker
            """, (cutoff,)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except (sqlite3.Error, OSError) as e:
            _log.warning("获取预测列表失败: %s", e)
            return []


class Backtester:
    """
    回测引擎 - 自动检验预测准确率

    工作流：
    1. save_predictions()：扫描结束后保存所有预测
    2. run_backtest()：检查到期的预测，获取实际价格，计算收益率
    3. print_report()：输出准确率报告
    4. adapt_weights()：根据准确率调整 5 维公式权重
    """

    def __init__(self, db_path: str = DB_PATH):
        self.store = PredictionStore(db_path)

    # ==================== 保存预测 ====================

    def save_predictions(self, swarm_results: Dict) -> int:
        """
        将蜂群扫描结果保存为预测记录

        Args:
            swarm_results: {ticker: {final_score, direction, dimension_scores, ...}}

        Returns:
            保存的记录数
        """
        saved = 0
        for ticker, data in swarm_results.items():
            if not isinstance(data, dict):
                continue

            # 收集各 Agent 的方向（从 QueenDistiller 的 agent_directions 字段）
            agent_dirs = data.get("agent_directions", {})

            # 获取预测时的价格
            price = 0.0
            try:
                if yf:
                    stock = yf.Ticker(ticker)
                    price = stock.fast_info.get("lastPrice", 0)
            except (ConnectionError, TimeoutError, OSError, ValueError, KeyError, AttributeError) as e:
                _log.debug("Price fetch failed for %s: %s", ticker, e)

            ok = self.store.save_prediction(
                ticker=ticker,
                final_score=data.get("final_score", 5.0),
                direction=data.get("direction", "neutral"),
                price=price,
                dimension_scores=data.get("dimension_scores"),
                agent_directions=agent_dirs,
            )
            if ok:
                saved += 1

        return saved

    # ==================== 执行回测 ====================

    def run_backtest(self) -> Dict:
        """
        执行回测检验：检查所有到期的预测

        返回: {t1: {checked, correct}, t7: {...}, t30: {...}}
        """
        # 回测检验
        results = {}

        for period in ["t1", "t7", "t30"]:
            pending = self.store.get_pending_checks(period)
            if not pending:
                results[period] = {"checked": 0, "correct": 0, "skipped": 0}
                continue

            days_map = {"t1": 1, "t7": 7, "t30": 30}
            days = days_map[period]
            checked = 0
            correct = 0
            skipped = 0

            # {period.upper()} 回测

            for pred in pending:
                ticker = pred["ticker"]
                predict_date = pred["date"]
                predict_price = pred.get("price_at_predict", 0)
                direction = pred["direction"]

                if not predict_price or predict_price <= 0:
                    skipped += 1
                    continue

                # 获取 T+N 日的实际价格
                actual_price = self._get_price_at_date(
                    ticker, predict_date, days
                )

                if actual_price is None or actual_price <= 0:
                    skipped += 1
                    continue

                # 计算收益率
                ret = (actual_price - predict_price) / predict_price * 100

                # 判断方向是否正确
                is_correct = self._check_direction(direction, ret)

                self.store.update_check_result(
                    pred["id"], period, actual_price, round(ret, 3), is_correct
                )

                checked += 1
                if is_correct:
                    correct += 1

            results[period] = {
                "checked": checked,
                "correct": correct,
                "skipped": skipped,
                "accuracy": correct / checked if checked > 0 else 0,
            }

            pass  # 准确率已计算

        return results

    def _get_price_at_date(
        self, ticker: str, predict_date: str, days_ahead: int
    ) -> Optional[float]:
        """获取预测日后 N 天的收盘价"""
        if yf is None:
            return None

        try:
            target_date = datetime.strptime(predict_date, "%Y-%m-%d") + timedelta(days=days_ahead)
            # 向后多取几天以覆盖周末/假日
            end_date = target_date + timedelta(days=5)

            stock = yf.Ticker(ticker)
            hist = stock.history(
                start=target_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
            )

            if hist.empty:
                return None

            # 取第一个交易日的收盘价
            return float(hist["Close"].iloc[0])

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("Future price fetch failed for %s +%dd: %s", ticker, days_ahead, e)
            return None

    def _check_direction(self, direction: str, actual_return: float) -> bool:
        """
        检查预测方向是否正确

        规则:
        - bullish: 实际收益 > -1%（允许小幅回调）
        - bearish: 实际收益 < +1%
        - neutral: 实际收益在 ±3% 内
        """
        if direction == "bullish":
            return actual_return > -1.0
        elif direction == "bearish":
            return actual_return < 1.0
        else:  # neutral
            return abs(actual_return) < 3.0

    # ==================== 准确率报告 ====================

    def print_report(self, days: int = 90) -> str:
        """输出完整的准确率报告"""
        lines = []
        lines.append("\n" + "=" * 70)
        lines.append("  📊 Alpha Hive 回测准确率报告")
        lines.append(f"  📅 统计窗口：最近 {days} 天")
        lines.append("=" * 70)

        for period in ["t1", "t7", "t30"]:
            label = {"t1": "T+1（次日）", "t7": "T+7（一周）", "t30": "T+30（一月）"}
            stats = self.store.get_accuracy_stats(period, days)

            total = stats.get("total_checked", 0)
            if total == 0:
                lines.append(f"\n  [{label[period]}] 暂无数据")
                continue

            acc = stats["overall_accuracy"]
            avg_ret = stats["avg_return"]
            lines.append(f"\n  [{label[period]}]")
            lines.append(f"  总体准确率: {acc*100:.1f}% ({stats['correct_count']}/{total})")
            lines.append(f"  平均收益率: {avg_ret:+.2f}%")
            lines.append(f"  平均评分: {stats.get('avg_score', 0):.1f}/10")

            # 按方向
            by_dir = stats.get("by_direction", {})
            if by_dir:
                lines.append("  按方向:")
                for d, info in by_dir.items():
                    if info["total"] > 0:
                        label_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(d, d)
                        lines.append(
                            f"    {label_cn}: {info['accuracy']*100:.0f}% "
                            f"({info['correct']}/{info['total']}) "
                            f"平均收益 {info['avg_return']:+.2f}%"
                        )

            # 按标的
            by_ticker = stats.get("by_ticker", {})
            if by_ticker:
                lines.append("  按标的:")
                for t, info in sorted(by_ticker.items(), key=lambda x: x[1]["total"], reverse=True):
                    lines.append(
                        f"    {t}: {info['accuracy']*100:.0f}% "
                        f"({info['correct']}/{info['total']}) "
                        f"平均收益 {info['avg_return']:+.2f}%"
                    )

        # 最近预测列表
        recent = self.store.get_all_predictions(days=14)
        if recent:
            lines.append(f"\n  最近预测记录 ({len(recent)} 条):")
            lines.append(f"  {'日期':<12} {'标的':<6} {'评分':>5} {'方向':<8} "
                         f"{'价格':>8} {'T+1':>8} {'T+7':>8} {'T+30':>8}")
            lines.append("  " + "-" * 66)

            for p in recent[:20]:
                t1_str = f"{p['return_t1']:+.1f}%" if p.get("checked_t1") else "待检"
                t7_str = f"{p['return_t7']:+.1f}%" if p.get("checked_t7") else "待检"
                t30_str = f"{p['return_t30']:+.1f}%" if p.get("checked_t30") else "待检"
                dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(
                    p["direction"], p["direction"]
                )
                lines.append(
                    f"  {p['date']:<12} {p['ticker']:<6} "
                    f"{p['final_score']:5.1f} {dir_cn:<8} "
                    f"${p.get('price_at_predict', 0):7.1f} "
                    f"{t1_str:>8} {t7_str:>8} {t30_str:>8}"
                )

        lines.append("\n" + "=" * 70)

        report = "\n".join(lines)
        _log.info(report)
        return report

    # ==================== 权重自适应 ====================

    def adapt_weights(self, min_samples: int = 10) -> Optional[Dict]:
        """
        根据 T+7 准确率自动调整 5 维公式权重

        规则：
        - 收集每个维度（signal, catalyst, sentiment, odds, risk_adj）
          对应 Agent 的 T+7 方向准确率
        - 准确率高的维度获得更高权重
        - 权重归一化：总和 = 1.0
        - 最低样本数要求：min_samples

        返回: {dimension: new_weight} 或 None（样本不足）
        """
        # Agent → 维度映射
        agent_dim_map = {
            "ScoutBeeNova": "signal",
            "OracleBeeEcho": "odds",
            "BuzzBeeWhisper": "sentiment",
            "ChronosBeeHorizon": "catalyst",
            "GuardBeeSentinel": "risk_adj",
        }

        # 默认权重
        default_weights = {
            "signal": 0.30,
            "catalyst": 0.20,
            "sentiment": 0.20,
            "odds": 0.15,
            "risk_adj": 0.15,
        }

        # 获取每个维度的准确率
        dim_accuracy = {}
        total_samples = 0

        try:
            conn = sqlite3.connect(self.store.db_path)
            conn.row_factory = sqlite3.Row

            for agent_name, dim in agent_dim_map.items():
                # 从 agent_directions JSON 中提取各 Agent 的方向预测
                # 然后与实际收益比较
                rows = conn.execute(f"""
                    SELECT
                        agent_directions, return_t7, direction
                    FROM {PredictionStore.TABLE}
                    WHERE checked_t7 = 1 AND agent_directions IS NOT NULL
                    AND date >= ?
                """, ((datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"),)).fetchall()

                correct = 0
                checked = 0
                for row in rows:
                    try:
                        dirs = json.loads(row["agent_directions"])
                        agent_dir = dirs.get(agent_name)
                        if not agent_dir:
                            continue
                        ret = row["return_t7"]
                        if ret is None:
                            continue

                        checked += 1
                        if self._check_direction(agent_dir, ret):
                            correct += 1
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                        _log.debug("Agent direction parse error: %s", e)
                        continue

                if checked >= min_samples:
                    dim_accuracy[dim] = correct / checked
                    total_samples += checked
                else:
                    dim_accuracy[dim] = 0.5  # 样本不足时用默认 50%

            conn.close()

        except (sqlite3.Error, OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            _log.warning("权重自适应失败: %s", e)
            return None

        if total_samples < min_samples:
            pass  # 样本不足，保持默认
            return None

        # 计算新权重：准确率^2 归一化（放大差异）
        raw = {dim: max(0.05, acc ** 2) for dim, acc in dim_accuracy.items()}
        total_raw = sum(raw.values())
        new_weights = {dim: round(v / total_raw, 3) for dim, v in raw.items()}

        # 平滑过渡：80% 新权重 + 20% 默认权重（防止剧烈波动）
        smoothed = {}
        for dim in default_weights:
            old_w = default_weights[dim]
            new_w = new_weights.get(dim, old_w)
            smoothed[dim] = round(old_w * 0.2 + new_w * 0.8, 3)

        # 再次归一化
        s = sum(smoothed.values())
        smoothed = {dim: round(v / s, 3) for dim, v in smoothed.items()}

        # 权重自适应完成

        # 持久化到数据库
        self._save_adapted_weights(smoothed, dim_accuracy, total_samples)

        return smoothed

    def _save_adapted_weights(
        self, weights: Dict, accuracy: Dict, samples: int
    ):
        """将自适应权重持久化到 SQLite"""
        try:
            conn = sqlite3.connect(self.store.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS adapted_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    weights TEXT NOT NULL,
                    accuracy TEXT NOT NULL,
                    sample_count INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT INTO adapted_weights (date, weights, accuracy, sample_count)
                VALUES (?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d"),
                json.dumps(weights),
                json.dumps({k: round(v, 3) for k, v in accuracy.items()}),
                samples,
            ))
            conn.commit()
            conn.close()
        except (sqlite3.Error, OSError, TypeError) as e:
            _log.warning("保存自适应权重失败: %s", e)

    @staticmethod
    def load_adapted_weights(db_path: str = DB_PATH) -> Optional[Dict]:
        """
        加载最近的自适应权重（供 QueenDistiller 使用）

        Returns:
            {signal: 0.xx, catalyst: 0.xx, ...} 或 None
        """
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute("""
                SELECT weights, sample_count FROM adapted_weights
                ORDER BY created_at DESC LIMIT 1
            """).fetchone()
            conn.close()

            if row and row[1] >= 5:  # 至少 5 个样本才使用
                weights = json.loads(row[0])
                return weights
            return None
        except (sqlite3.Error, OSError, json.JSONDecodeError, KeyError) as e:
            _log.debug("Adapted weights load failed: %s", e)
            return None


# ==================== 便捷函数 ====================

def run_full_backtest(swarm_results: Dict = None) -> Dict:
    """
    执行完整回测流程

    1. 保存新预测（如有）
    2. 检查到期预测
    3. 输出报告
    4. 尝试权重自适应

    返回: {backtest_results, accuracy_stats, adapted_weights}
    """
    bt = Backtester()

    # 1. 保存新预测
    if swarm_results:
        bt.save_predictions(swarm_results)

    # 2. 回测到期预测
    backtest_results = bt.run_backtest()

    # 3. 准确率报告
    bt.print_report()

    # 4. 权重自适应
    adapted = bt.adapt_weights(min_samples=10)

    return {
        "backtest_results": backtest_results,
        "adapted_weights": adapted,
    }
