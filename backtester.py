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

from hive_logger import PATHS, get_logger, FeatureRegistry

try:
    import pandas as _pd
    from pandas.tseries.holiday import USFederalHolidayCalendar as _USCal
    from pandas.tseries.offsets import CustomBusinessDay as _CBDay
    _US_BDAY = _CBDay(calendar=_USCal())
    _BDAY_AVAILABLE = True
except Exception:
    _BDAY_AVAILABLE = False
FeatureRegistry.register("pandas_bday", _BDAY_AVAILABLE,
                          "T+N 交易日计算降级为自然日" if not _BDAY_AVAILABLE else "")

try:
    import yfinance as yf
except ImportError:
    yf = None
FeatureRegistry.register("yfinance", yf is not None,
                          "回测/价格获取不可用" if yf is None else "")

_log = get_logger("backtester")

DB_PATH = PATHS.db


class PredictionStore:
    """预测记录存储（SQLite）"""

    TABLE = "predictions"

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_table()

    def _init_table(self):
        conn = None
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
                    -- 期权分析字段
                    options_score      REAL,
                    iv_rank            REAL,
                    put_call_ratio     REAL,
                    gamma_exposure     REAL,
                    flow_direction     TEXT,
                    -- T+1 回测
                    price_t1           REAL,
                    return_t1          REAL,
                    correct_t1         INTEGER,
                    checked_t1         INTEGER DEFAULT 0,
                    iv_rank_t1         REAL,
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
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_pred_checked_t7_date "
                         f"ON {self.TABLE}(checked_t7, date)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_pred_date_ticker "
                         f"ON {self.TABLE}(date, ticker)")
            # 迁移：如果旧表缺少期权字段，添加它们
            self._migrate_options_columns(conn)
            conn.commit()
        except (sqlite3.Error, OSError) as e:
            _log.warning("预测表初始化失败: %s", e)
        finally:
            if conn:
                conn.close()

    def _migrate_options_columns(self, conn):
        """为旧表添加期权相关字段（兼容已有数据库）"""
        new_columns = [
            ("options_score", "REAL"),
            ("iv_rank", "REAL"),
            ("put_call_ratio", "REAL"),
            ("gamma_exposure", "REAL"),
            ("flow_direction", "TEXT"),
            ("iv_rank_t1", "REAL"),
            ("pheromone_compact", "TEXT"),  # NA5: Agent 自评分快照
        ]
        for col_name, col_type in new_columns:
            try:
                conn.execute(f"ALTER TABLE {self.TABLE} ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # 新增复合索引（幂等，IF NOT EXISTS 保证安全）
        for idx_sql in [
            f"CREATE INDEX IF NOT EXISTS idx_pred_checked_t7_date ON {self.TABLE}(checked_t7, date)",
            f"CREATE INDEX IF NOT EXISTS idx_pred_date_ticker ON {self.TABLE}(date, ticker)",
        ]:
            try:
                conn.execute(idx_sql)
            except sqlite3.OperationalError:
                pass

    def save_prediction(
        self,
        ticker: str,
        final_score: float,
        direction: str,
        price: float,
        dimension_scores: Dict = None,
        agent_directions: Dict = None,
        options_data: Dict = None,
        pheromone_compact: list = None,
    ) -> bool:
        """保存一条预测记录（含期权分析数据 + Agent 自评分快照）"""
        conn = None
        opts = options_data or {}
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(f"""
                INSERT OR REPLACE INTO {self.TABLE}
                (date, ticker, final_score, direction, price_at_predict,
                 dimension_scores, agent_directions,
                 options_score, iv_rank, put_call_ratio, gamma_exposure, flow_direction,
                 pheromone_compact)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d"),
                ticker,
                final_score,
                direction,
                price,
                json.dumps(dimension_scores or {}),
                json.dumps(agent_directions or {}),
                opts.get("options_score"),
                opts.get("iv_rank"),
                opts.get("put_call_ratio"),
                opts.get("gamma_exposure"),
                opts.get("flow_direction"),
                json.dumps(pheromone_compact or []),
            ))
            conn.commit()
            return True
        except (sqlite3.Error, OSError, TypeError) as e:
            _log.warning("保存预测失败 (%s): %s", ticker, e)
            return False
        finally:
            if conn:
                conn.close()

    def get_pending_checks(self, period: str) -> List[Dict]:
        """
        获取待回测的预测记录

        period: "t1" / "t7" / "t30"
        """
        days_map = {"t1": 1, "t7": 7, "t30": 30}
        days = days_map.get(period, 7)
        checked_col = f"checked_{period}"

        # 目标日期：预测日 + N 个交易日 <= 今天（跳过周末和联邦假日）
        if _BDAY_AVAILABLE:
            cutoff_dt = _pd.Timestamp.now() - days * _US_BDAY
            cutoff = cutoff_dt.strftime("%Y-%m-%d")
        else:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT * FROM {self.TABLE}
                WHERE date <= ? AND {checked_col} = 0
                ORDER BY date ASC
            """, (cutoff,)).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.Error, OSError) as e:
            _log.warning("获取待回测记录失败: %s", e)
            return []
        finally:
            if conn:
                conn.close()

    def update_check_result(
        self, pred_id: int, period: str,
        price: float, ret: float, correct: bool
    ) -> bool:
        """更新回测结果"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(f"""
                UPDATE {self.TABLE}
                SET price_{period} = ?, return_{period} = ?,
                    correct_{period} = ?, checked_{period} = 1
                WHERE id = ?
            """, (price, ret, 1 if correct else 0, pred_id))
            conn.commit()
            return True
        except (sqlite3.Error, OSError) as e:
            _log.warning("更新回测结果失败: %s", e)
            return False
        finally:
            if conn:
                conn.close()

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

        conn = None
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
        finally:
            if conn:
                conn.close()

    def get_all_predictions(self, days: int = 30) -> List[Dict]:
        """获取最近 N 天所有预测"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""
                SELECT * FROM {self.TABLE}
                WHERE date >= ? ORDER BY date DESC, ticker
            """, (cutoff,)).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.Error, OSError) as e:
            _log.warning("获取预测列表失败: %s", e)
            return []
        finally:
            if conn:
                conn.close()


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

            # 提取期权分析数据（如果蜂群结果中包含）
            options_data = data.get("options_data") or {}

            ok = self.store.save_prediction(
                ticker=ticker,
                final_score=data.get("final_score", 5.0),
                direction=data.get("direction", "neutral"),
                price=price,
                dimension_scores=data.get("dimension_scores"),
                agent_directions=agent_dirs,
                options_data=options_data,
                pheromone_compact=data.get("pheromone_compact", []),
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

                # T+1 期权回验：记录 T+1 的 IV Rank 变化
                if period == "t1" and pred.get("iv_rank") is not None:
                    self._check_options_t1(pred)

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
        """获取预测日后 N 个交易日的收盘价（跳过周末和美国法定假日）"""
        if yf is None:
            return None

        try:
            start = datetime.strptime(predict_date, "%Y-%m-%d")
            if _BDAY_AVAILABLE:
                # 用 pandas CustomBusinessDay 计算真实交易日偏移
                import pandas as _pd
                target_ts = _pd.Timestamp(start) + days_ahead * _US_BDAY
                target_date = target_ts.to_pydatetime()
            else:
                # 降级：自然日偏移（原行为）
                target_date = start + timedelta(days=days_ahead)

            # 向后留 10 天窗口应对节假日连休
            end_date = target_date + timedelta(days=10)

            stock = yf.Ticker(ticker)
            hist = stock.history(
                start=target_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
            )

            if hist.empty:
                return None

            # 取目标交易日（或之后第一个交易日）的收盘价
            return float(hist["Close"].iloc[0])

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("Future price fetch failed for %s +%dd: %s", ticker, days_ahead, e)
            return None

    def _check_options_t1(self, pred: Dict):
        """T+1 期权回验：获取 T+1 的 IV Rank 用于对比"""
        ticker = pred["ticker"]
        try:
            from options_analyzer import OptionsAgent
            agent = OptionsAgent()
            result = agent.analyze(ticker)
            iv_rank_t1 = result.get("iv_rank")

            if iv_rank_t1 is not None:
                conn = None
                try:
                    conn = sqlite3.connect(self.store.db_path)
                    conn.execute(f"""
                        UPDATE {PredictionStore.TABLE}
                        SET iv_rank_t1 = ?
                        WHERE id = ?
                    """, (iv_rank_t1, pred["id"]))
                    conn.commit()
                except (sqlite3.Error, OSError) as e:
                    _log.debug("IV Rank T+1 update failed: %s", e)
                finally:
                    if conn:
                        conn.close()

        except (ImportError, ConnectionError, TimeoutError, OSError,
                ValueError, KeyError, TypeError) as e:
            _log.debug("Options T+1 check skipped for %s: %s", ticker, e)

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

        # 期权分析回验统计
        lines.append("\n  [期权信号回验]")
        try:
            conn = sqlite3.connect(self.store.db_path)
            conn.row_factory = sqlite3.Row
            opts_row = conn.execute(f"""
                SELECT COUNT(*) as total,
                       AVG(options_score) as avg_opts_score,
                       AVG(iv_rank) as avg_iv_rank,
                       AVG(put_call_ratio) as avg_pc_ratio
                FROM {PredictionStore.TABLE}
                WHERE options_score IS NOT NULL AND date >= ?
            """, ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),)).fetchone()

            if opts_row and opts_row["total"] > 0:
                lines.append(f"  期权数据记录: {opts_row['total']} 条")
                lines.append(f"  平均期权评分: {opts_row['avg_opts_score']:.1f}/10")
                lines.append(f"  平均 IV Rank: {opts_row['avg_iv_rank']:.1f}")
                lines.append(f"  平均 P/C Ratio: {opts_row['avg_pc_ratio']:.2f}")

                # IV Rank 变化（T+1）
                iv_change_row = conn.execute(f"""
                    SELECT COUNT(*) as cnt,
                           AVG(iv_rank_t1 - iv_rank) as avg_iv_change
                    FROM {PredictionStore.TABLE}
                    WHERE iv_rank IS NOT NULL AND iv_rank_t1 IS NOT NULL AND date >= ?
                """, ((datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),)).fetchone()

                if iv_change_row and iv_change_row["cnt"] > 0:
                    lines.append(f"  IV Rank T+1 均值变化: {iv_change_row['avg_iv_change']:+.1f}")
            else:
                lines.append("  暂无期权分析数据")

            conn.close()
        except (sqlite3.Error, OSError, KeyError, TypeError) as e:
            lines.append(f"  期权回验查询失败: {e}")

        # 最近预测列表
        recent = self.store.get_all_predictions(days=14)
        if recent:
            lines.append(f"\n  最近预测记录 ({len(recent)} 条):")
            lines.append(f"  {'日期':<12} {'标的':<6} {'评分':>5} {'方向':<8} "
                         f"{'价格':>8} {'T+1':>8} {'T+7':>8} {'T+30':>8} {'OPT':>5}")
            lines.append("  " + "-" * 76)

            for p in recent[:20]:
                t1_str = f"{p['return_t1']:+.1f}%" if p.get("checked_t1") else "待检"
                t7_str = f"{p['return_t7']:+.1f}%" if p.get("checked_t7") else "待检"
                t30_str = f"{p['return_t30']:+.1f}%" if p.get("checked_t30") else "待检"
                opt_str = f"{p['options_score']:.0f}" if p.get("options_score") else "-"
                dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(
                    p["direction"], p["direction"]
                )
                lines.append(
                    f"  {p['date']:<12} {p['ticker']:<6} "
                    f"{p['final_score']:5.1f} {dir_cn:<8} "
                    f"${p.get('price_at_predict', 0):7.1f} "
                    f"{t1_str:>8} {t7_str:>8} {t30_str:>8} {opt_str:>5}"
                )

        lines.append("\n" + "=" * 70)

        report = "\n".join(lines)
        _log.info(report)
        return report

    # ==================== 权重自适应 ====================

    def analyze_self_score_bias(
        self, period: str = "t1", min_samples: int = 5
    ) -> Dict[str, float]:
        """
        NA5：分析各 Agent 的 self_score 系统性偏差

        偏差定义：Agent 预测错误时 self_score 的均值 - 预测正确时 self_score 的均值
          正值（>0）= 系统性乐观：高分时经常错，overconfident
          负值（<0）= 系统性保守：低分时反而对，underconfident
          ~0       = 自评校准良好

        返回: {agent_id_abbrev_8chars: bias_float}，样本不足的 Agent 返回 0.0
        """
        # agent 全名 → 缩写（pheromone_compact 用 agent_id[:8] 截取）
        # 注意：OracleBeeEcho[:8] = "OracleBe"（非 "OracleBee"）
        agent_abbrevs = {
            "ScoutBeeNova":      "ScoutBee",
            "OracleBeeEcho":     "OracleBe",   # [:8] = "OracleBe"，不是"OracleBee"
            "BuzzBeeWhisper":    "BuzzBeeW",
            "ChronosBeeHorizon": "ChronosB",
            "GuardBeeSentinel":  "GuardBee",
            "RivalBeeVanguard":  "RivalBee",
        }

        bias: Dict[str, float] = {abbrev: 0.0 for abbrev in agent_abbrevs.values()}
        conn = None
        try:
            conn = sqlite3.connect(self.store.db_path)
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            rows = conn.execute(f"""
                SELECT pheromone_compact, correct_{period}, return_{period}
                FROM {PredictionStore.TABLE}
                WHERE checked_{period} = 1
                  AND pheromone_compact IS NOT NULL
                  AND date >= ?
            """, (cutoff,)).fetchall()

            # {abbrev: {correct: [self_scores], wrong: [self_scores]}}
            buckets: Dict[str, Dict[str, list]] = {
                a: {"correct": [], "wrong": []} for a in agent_abbrevs.values()
            }

            for row in rows:
                try:
                    compact = json.loads(row["pheromone_compact"] or "[]")
                    correct = bool(row[f"correct_{period}"])
                    ret = row[f"return_{period}"]
                    if ret is None:
                        continue
                    for entry in compact:
                        abbrev = entry.get("a", "")
                        if abbrev in buckets:
                            ss = entry.get("s", 5.0)
                            bucket_key = "correct" if correct else "wrong"
                            buckets[abbrev][bucket_key].append(ss)
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue

            for abbrev, b in buckets.items():
                n_correct = len(b["correct"])
                n_wrong = len(b["wrong"])
                if n_correct + n_wrong < min_samples:
                    continue
                mean_correct = sum(b["correct"]) / n_correct if n_correct else 5.0
                mean_wrong = sum(b["wrong"]) / n_wrong if n_wrong else 5.0
                # 乐观偏差 = 错误时均值 - 正确时均值（越大表示越倾向在错误时给高分）
                bias[abbrev] = round(mean_wrong - mean_correct, 3)

        except (sqlite3.Error, OSError) as e:
            _log.warning("self_score 偏差分析失败: %s", e)
        finally:
            if conn:
                conn.close()

        _log.info("Agent self_score 偏差分析: %s", {k: f"{v:+.3f}" for k, v in bias.items()})
        return bias

    def adapt_weights(self, min_samples: int = 10, period: str = "t7") -> Optional[Dict]:
        """
        根据历史方向准确率自动调整 5 维公式权重

        优先使用 T+7（更可靠），T+7 样本不足时自动降级到 T+1：
        - T+7：平滑因子 80% 新权重（充分信任）
        - T+1：平滑因子 50% 新权重（T+1 噪声更大，保守调整）

        规则：
        - 按 Agent 方向 vs 实际收益计算各维度准确率
        - 准确率^2 归一化后作为新权重（放大高准确率维度的优势）
        - 最低样本数：min_samples（T+7 默认 10，T+1 可用 5）

        返回: {dimension: new_weight} 或 None（样本不足）
        """
        # Agent → 维度映射（与 pheromone_board.AGENT_DIMENSIONS 保持一致）
        agent_dim_map = {
            "ScoutBeeNova":      "signal",
            "OracleBeeEcho":     "odds",
            "BuzzBeeWhisper":    "sentiment",
            "ChronosBeeHorizon": "catalyst",
            "GuardBeeSentinel":  "risk_adj",
        }

        # 默认权重（来自 config，此处作为兜底）
        _fallback_weights = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15}
        try:
            from config import EVALUATION_WEIGHTS
            base = {k: v for k, v in EVALUATION_WEIGHTS.items() if k in agent_dim_map.values()}
            # Bug 9: 补全 config 中可能缺失的维度，避免后续 KeyError
            default_weights = {dim: base.get(dim, _fallback_weights[dim]) for dim in _fallback_weights}
        except (ImportError, AttributeError):
            default_weights = _fallback_weights

        # T+1 平滑因子更保守（T+1 噪声大，不能大幅改变权重）
        new_weight_ratio = 0.8 if period == "t7" else 0.5

        # 获取每个维度的准确率
        dim_accuracy = {}
        total_samples = 0

        conn = None
        try:
            conn = sqlite3.connect(self.store.db_path)
            conn.row_factory = sqlite3.Row

            for agent_name, dim in agent_dim_map.items():
                rows = conn.execute(f"""
                    SELECT agent_directions, return_{period}, direction
                    FROM {PredictionStore.TABLE}
                    WHERE checked_{period} = 1 AND agent_directions IS NOT NULL
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
                        ret = row[f"return_{period}"]
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
                    dim_accuracy[dim] = 0.5  # 样本不足时用中性 50%

        except (sqlite3.Error, OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            _log.warning("权重自适应失败 (%s): %s", period, e)
            return None
        finally:
            if conn:
                conn.close()

        if total_samples < min_samples:
            _log.debug("权重自适应：%s 样本不足 (%d < %d)", period, total_samples, min_samples)
            return None

        # 计算新权重：准确率^2 归一化（放大高准确率维度的优势）
        raw = {dim: max(0.05, acc ** 2) for dim, acc in dim_accuracy.items()}
        total_raw = sum(raw.values())
        new_weights = {dim: round(v / total_raw, 3) for dim, v in raw.items()}

        # 平滑过渡：new_weight_ratio × 新权重 + (1-ratio) × 默认权重
        smoothed = {}
        for dim in default_weights:
            old_w = default_weights[dim]
            new_w = new_weights.get(dim, old_w)
            smoothed[dim] = round(old_w * (1 - new_weight_ratio) + new_w * new_weight_ratio, 3)

        # 归一化确保总和 = 1.0
        s = sum(smoothed.values())
        smoothed = {dim: round(v / s, 3) for dim, v in smoothed.items()}

        # NA5：self_score 偏差校正
        # 若某 Agent 系统性乐观（高分时经常错），小幅下调其维度权重
        # 规则：|bias| > 0.5 才修正，最大修正幅度 ±10%，避免震荡
        dim_to_abbrev = {
            "signal":    "ScoutBee",
            "odds":      "OracleBe",   # OracleBeeEcho[:8] = "OracleBe"
            "sentiment": "BuzzBeeW",
            "catalyst":  "ChronosB",
            "risk_adj":  "GuardBee",
        }
        try:
            bias_map = self.analyze_self_score_bias(period=period, min_samples=3)
            bias_applied = {}
            for dim, abbrev in dim_to_abbrev.items():
                bias = bias_map.get(abbrev, 0.0)
                if abs(bias) > 0.5:
                    # 乐观偏差（bias>0）→ 降权；保守偏差（bias<0）→ 小幅升权
                    correction = -bias * 0.05   # 每1分偏差调整 5%，最大 ±10%
                    correction = max(-0.10, min(0.05, correction))
                    smoothed[dim] = round(smoothed[dim] * (1.0 + correction), 3)
                    bias_applied[dim] = round(correction, 4)
            if bias_applied:
                # 再次归一化
                s2 = sum(smoothed.values())
                smoothed = {dim: round(v / s2, 3) for dim, v in smoothed.items()}
                _log.info("NA5 self_score 偏差校正: %s", bias_applied)
        except (sqlite3.Error, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            _log.debug("self_score 偏差校正跳过（样本不足或异常）: %s", e)

        _log.info(
            "权重自适应（%s，%d 样本）: %s | 各维度准确率: %s",
            period, total_samples,
            {k: f"{v:.3f}" for k, v in smoothed.items()},
            {k: f"{v:.1%}" for k, v in dim_accuracy.items()},
        )

        self._save_adapted_weights(smoothed, dim_accuracy, total_samples, period)
        return smoothed

    def _save_adapted_weights(
        self, weights: Dict, accuracy: Dict, samples: int, period: str = "t7"
    ):
        """将自适应权重持久化到 SQLite"""
        conn = None
        try:
            conn = sqlite3.connect(self.store.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS adapted_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    weights TEXT NOT NULL,
                    accuracy TEXT NOT NULL,
                    sample_count INTEGER,
                    period TEXT DEFAULT 't7',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 迁移旧表缺少 period 列
            try:
                conn.execute("ALTER TABLE adapted_weights ADD COLUMN period TEXT DEFAULT 't7'")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                INSERT INTO adapted_weights (date, weights, accuracy, sample_count, period)
                VALUES (?, ?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d"),
                json.dumps(weights),
                json.dumps({k: round(v, 3) for k, v in accuracy.items()}),
                samples,
                period,
            ))
            conn.commit()
        except (sqlite3.Error, OSError, TypeError) as e:
            _log.warning("保存自适应权重失败: %s", e)
        finally:
            if conn:
                conn.close()

    @staticmethod
    def load_adapted_weights(db_path: str = DB_PATH) -> Optional[Dict]:
        """
        加载最近的自适应权重（供 QueenDistiller 使用）

        优先加载 T+7 权重（更可靠），其次加载 T+1 权重（早期降级）。
        返回的权重已附加 _meta 字段，QueenDistiller 会自动忽略未知 key。

        Returns:
            {signal: 0.xx, ..., _meta: {period, samples}} 或 None
        """
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            # 优先取 T+7，再取 T+1
            row = conn.execute("""
                SELECT weights, sample_count, period
                FROM adapted_weights
                WHERE sample_count >= 3
                ORDER BY
                    CASE period WHEN 't7' THEN 0 WHEN 't1' THEN 1 ELSE 2 END,
                    created_at DESC
                LIMIT 1
            """).fetchone()

            if row:
                weights = json.loads(row[0])
                period = row[2] or "t7"
                samples = row[1]
                _log.info("加载自适应权重（%s，%d 样本）: %s", period, samples, weights)
                return weights
            return None
        except (sqlite3.Error, OSError, json.JSONDecodeError, KeyError) as e:
            _log.debug("Adapted weights load failed: %s", e)
            return None
        finally:
            if conn:
                conn.close()


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
