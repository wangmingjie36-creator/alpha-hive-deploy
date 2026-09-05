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
from datetime import datetime, timedelta

# v0.27.3: 与美股交易日对齐的日期工具，避免本地时区为 CST/北京时跨午夜偏移
try:
    from zoneinfo import ZoneInfo
    _PDT = ZoneInfo("America/Los_Angeles")
    def _pdt_today() -> str:
        return datetime.now(_PDT).strftime("%Y-%m-%d")
    def _pdt_now() -> datetime:
        return datetime.now(_PDT)
except Exception:
    def _pdt_today() -> str:
        return datetime.now().strftime("%Y-%m-%d")
    def _pdt_now() -> datetime:
        return datetime.now()
from typing import Dict, List, Optional

from hive_logger import PATHS, get_logger, FeatureRegistry, SafeJSONEncoder

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


def _wilson_ci(k: int, n: int, z: float = 1.96) -> Optional[tuple]:
    """Wilson 95% 置信区间（百分数）。

    ⚠️ 传入的 n 是**名义样本数**。本项目 30 只标的每日滚动、T+7 持有，
    相邻样本高度重叠，名义 n 约高估有效样本 25×。所以这个区间只用来
    **展示精度量级**，不能拿它判显著性——判显著性用 `_t_test_vs` 的周度序列。
    """
    if n <= 0:
        return None
    import math as _m
    p = k / n
    d = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / d
    half = z * _m.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round((ctr - half) * 100, 1), round((ctr + half) * 100, 1))


def _t_test_vs(series: List[float], null_value: float) -> Optional[float]:
    """对不重叠周序列做单样本 t 检验，返回双尾 p（正态近似）。

    序列每个元素是**一个 ISO 周**的准确率——这是本项目唯一诚实的独立观测单位。
    少于 3 周不给结论（返回 None），不要用 0.0 之类的值兜底：
    「样本不足」和「p=0」在下游看起来会一模一样。
    """
    n = len(series)
    if n < 3:
        return None
    import math as _m
    mean_v = sum(series) / n
    var = sum((x - mean_v) ** 2 for x in series) / (n - 1)
    if var <= 0:
        return None
    t = (mean_v - null_value) / (_m.sqrt(var) / _m.sqrt(n))
    return round(_m.erfc(abs(t) / _m.sqrt(2)), 4)


class PredictionStore:
    """预测记录存储（SQLite）"""

    TABLE = "predictions"

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_table()

    def _init_table(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
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
            # Sprint 1 / v16.0 真实策略回测新增
            ("net_return_t7", "REAL"),      # 扣成本后净收益率 (%)
            ("exit_reason", "TEXT"),        # TP / SL / T7_CLOSE
            ("exit_date", "TEXT"),          # 实际平仓日
            ("exit_price", "REAL"),         # 实际平仓价
            ("holding_days", "INTEGER"),    # 实际持仓天数
            ("cost_breakdown", "TEXT"),     # JSON: {slippage, commission, borrow}
            ("spy_return_t7", "REAL"),      # 同期 SPY 基准收益
            # v0.45.9 P0：容差语义修正。|return| <= tolerance 的样本标为模糊，
            # 既不算对也不算错，所有准确率查询须带 AND ambiguous_{period} = 0
            ("ambiguous_t1", "INTEGER"),
            ("ambiguous_t7", "INTEGER"),
            ("ambiguous_t30", "INTEGER"),
            # v0.45.17：把「方向判对了吗」与「这笔交易赚钱了吗」分开存。
            # 旧 correct_t7 由**路径依赖的离场收益**算出（SL/TP 触发即截断），
            # 它衡量的是交易结果，不是方向精度；而中性预测从不建仓、没有 SL/TP，
            # 两者混进同一个分母报成「整体准确率」是苹果比橘子。
            # 下面三列一律基于**未截断的 T+7 收盘价**，只回答方向问题。
            ("close_t7", "REAL"),           # 真实 T+7 收盘价（无路径截断）
            ("dir_correct_t7", "INTEGER"),  # 按 close_t7 判定的方向是否正确
            ("dir_ambiguous_t7", "INTEGER"),# |原始收益| <= 容差 → 不评分
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
        date: Optional[str] = None,
    ) -> bool:
        """保存一条预测记录（含期权分析数据 + Agent 自评分快照）

        Args:
            date: **业务日期**（YYYY-MM-DD），即"这份预测属于哪个交易日"。
                  留空则回退 `_pdt_today()`（写入时刻的 PDT 日历日）。

        ⚠️ v0.42.4 修复的核心：本表有 `UNIQUE(date, ticker)` + `INSERT OR REPLACE`，
        REPLACE 语义是「删除旧行 + 插入新行（分配新 rowid）」。旧实现无条件盖
        `_pdt_today()`，于是**同一 PDT 日历日跑第二次扫描会删掉第一次的记录**——
        `--date` 补跑历史交易日时尤其致命：报告和快照都标着目标日期，唯独预测
        记录盖成运行当天并互相覆盖。实测全库因此丢失 479 行（消耗 1294 个 id
        只保留 815 行，37%）。调用方**只要有业务日期就必须显式传入**。
        """
        opts = options_data or {}

        # 业务日期校验：格式不合法时回退当日并告警，不静默写脏数据
        entry_date = date or _pdt_today()
        if date:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except (ValueError, TypeError):
                _log.warning(
                    "save_prediction 收到非法业务日期 %r（应为 YYYY-MM-DD），"
                    "回退为 %s", date, _pdt_today()
                )
                entry_date = _pdt_today()

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(f"""
                    INSERT OR REPLACE INTO {self.TABLE}
                    (date, ticker, final_score, direction, price_at_predict,
                     dimension_scores, agent_directions,
                     options_score, iv_rank, put_call_ratio, gamma_exposure, flow_direction,
                     pheromone_compact)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry_date,  # v0.42.4: 业务日期（调用方传入）；留空才回退 PDT 当日
                    ticker,
                    final_score,
                    direction,
                    price,
                    json.dumps(dimension_scores or {}, cls=SafeJSONEncoder),
                    json.dumps(agent_directions or {}, cls=SafeJSONEncoder),
                    opts.get("options_score"),
                    opts.get("iv_rank"),
                    opts.get("put_call_ratio"),
                    opts.get("gamma_exposure"),
                    opts.get("flow_direction"),
                    json.dumps(pheromone_compact or [], cls=SafeJSONEncoder),
                ))
                conn.commit()
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

        # 目标日期：预测日 + N 个交易日 <= 今天（跳过周末和联邦假日）
        # v0.33.0: cutoff 锚定 PDT —— entry_date 由 save_prediction 盖 PDT(line 169)，
        # 此处 cutoff 原用裸本地时(上海)，两者口径不一致导致当天预测被误判已满 T+N。
        # 用 _pdt_today() 作业务日回看起点，与 date 列同历比较。
        if _BDAY_AVAILABLE:
            cutoff_dt = _pd.Timestamp(_pdt_today()) - days * _US_BDAY
            cutoff = cutoff_dt.strftime("%Y-%m-%d")
        else:
            cutoff = (_pdt_now() - timedelta(days=days)).strftime("%Y-%m-%d")

        try:
            with sqlite3.connect(self.db_path) as conn:
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

    def update_check_result(
        self, pred_id: int, period: str,
        price: float, ret: float, correct: bool,
        ambiguous: bool = False,
    ) -> bool:
        """
        更新回测结果。

        v0.45.9 P0：新增 ambiguous —— |return| <= 容差的样本既不算对也不算错，
        落库后由所有准确率查询以 `AND ambiguous_{period} = 0` 剔除。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(f"""
                    UPDATE {self.TABLE}
                    SET price_{period} = ?, return_{period} = ?,
                        correct_{period} = ?, ambiguous_{period} = ?,
                        checked_{period} = 1
                    WHERE id = ?
                """, (price, ret, 1 if correct else 0,
                      1 if ambiguous else 0, pred_id))
                conn.commit()
            return True
        except (sqlite3.Error, OSError) as e:
            _log.warning("更新回测结果失败: %s", e)
            return False

    def update_t7_path_result(
        self, pred_id: int,
        price_t7: float, return_t7: float, correct_t7: bool,
        net_return_pct: Optional[float],
        exit_reason: Optional[str],
        exit_date: Optional[str],
        exit_price: Optional[float],
        holding_days: Optional[int],
        cost_breakdown: Optional[Dict],
        spy_return: Optional[float],
        ambiguous_t7: bool = False,
        close_t7: Optional[float] = None,
        dir_correct_t7: Optional[bool] = None,
        dir_ambiguous_t7: Optional[bool] = None,
    ) -> bool:
        """
        Sprint 1: T+7 路径依赖 + 净收益 + 基准一次性写入。

        v0.45.9 P0：ambiguous_t7 作为**末位关键字参数**追加，不影响既有
        位置调用；|return| <= 容差时置 1，准确率统计会剔除该样本。

        v0.45.17：新增 close_t7 / dir_correct_t7 / dir_ambiguous_t7 三个
        **纯方向**字段，同样以末位关键字追加。
        - `price_t7`/`return_t7`/`correct_t7` 保持原语义（路径依赖的交易结果），
          equity curve、ML 训练、portfolio_backtest 全部继续吃它们，勿改。
        - 新三列只回答「方向判对了吗」，基于未截断的 T+7 收盘价。
        传 None 时该列不写（保持 NULL），供回填脚本分批补齐。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(f"""
                    UPDATE {self.TABLE}
                    SET price_t7 = ?, return_t7 = ?, correct_t7 = ?, checked_t7 = 1,
                        ambiguous_t7 = ?,
                        net_return_t7 = ?, exit_reason = ?, exit_date = ?,
                        exit_price = ?, holding_days = ?, cost_breakdown = ?,
                        spy_return_t7 = ?,
                        close_t7 = COALESCE(?, close_t7),
                        dir_correct_t7 = COALESCE(?, dir_correct_t7),
                        dir_ambiguous_t7 = COALESCE(?, dir_ambiguous_t7)
                    WHERE id = ?
                """, (
                    price_t7, return_t7, 1 if correct_t7 else 0,
                    1 if ambiguous_t7 else 0,
                    net_return_pct, exit_reason, exit_date,
                    exit_price, holding_days,
                    json.dumps(cost_breakdown or {}, cls=SafeJSONEncoder),
                    spy_return,
                    close_t7,
                    None if dir_correct_t7 is None else (1 if dir_correct_t7 else 0),
                    None if dir_ambiguous_t7 is None else (1 if dir_ambiguous_t7 else 0),
                    pred_id,
                ))
                conn.commit()
            return True
        except (sqlite3.Error, OSError) as e:
            _log.warning("Path result 更新失败 id=%s: %s", pred_id, e)
            return False

    def get_recently_verified_t7(self, limit: int = 50) -> List[Dict]:
        """获取最近一批已被 T+7 验证的记录（按日期倒序）

        用于增量 ML 训练：每次 run_backtest() 之后调用，
        获取 checked_t7=1 且 return_t7 IS NOT NULL 的最新记录。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(f"""
                    SELECT ticker, date, final_score, direction,
                           dimension_scores, iv_rank, put_call_ratio,
                           agent_directions,
                           return_t7, correct_t7
                    FROM {self.TABLE}
                    WHERE checked_t7 = 1 AND return_t7 IS NOT NULL
                    ORDER BY date DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                return [dict(r) for r in rows]
        except (sqlite3.Error, OSError) as e:
            _log.warning("获取已验证 T+7 记录失败: %s", e)
            return []

    def get_accuracy_stats(self, period: str = "t7", days: int = 90,
                           exclude_nontrading_days: bool = False,
                           use_direction_metric: bool = False) -> Dict:
        """
        获取准确率统计

        exclude_nontrading_days=True（dashboard 门面口径）：排除周末/假日预测
        （周日 sample-accumulator 扩展池样本 + 漂移幽灵），只算核心交易日。
        默认 False，研究/其它调用保留全样本。fail-open：构建过滤出错则不过滤。

        返回: {
            overall_accuracy, total_checked, correct_count,
            avg_return, by_direction: {bullish: {}, bearish: {}, neutral: {}},
            by_ticker: {NVDA: {}, ...}
        }

        use_direction_metric=True（仅 t7 有效，v0.45.17 新增）：
            改用 `dir_correct_t7` / `dir_ambiguous_t7` —— 基于**未截断的
            T+7 收盘价**的纯方向判定。默认 False 保持旧行为。

            为什么需要这个开关：默认的 `correct_t7` 由**路径依赖的离场收益**
            算出，触发 SL/TP 即提前离场、收益被钳在止损止盈档位（库里
            `-10.04` / `+9.95` 反复出现即此故）。所以它回答的是「这笔交易
            赚钱了吗」，不是「方向猜对了吗」；而中性预测从不建仓、从无 SL/TP。
            两者混进同一分母报成「整体准确率」是苹果比橘子。
            交易类指标（equity curve / ML 训练 / portfolio_backtest）应继续用
            默认口径，**只有展示「预测准确率」时才该开这个开关**。

        额外返回键（v0.45.17）：
            directional_accuracy / directional_total / directional_correct
                —— 只含 bullish+bearish，**不含中性**，这才是可比的方向精度。
            metric —— "direction" 或 "trade"，供上游标注口径，勿省略。
        """
        _use_dir = bool(use_direction_metric) and period == "t7"
        cutoff = (_pdt_now() - timedelta(days=days)).strftime("%Y-%m-%d")  # v0.33.0: 统计窗口口径 PDT 化（对齐 date 列）
        checked_col = f"checked_{period}"
        correct_col = "dir_correct_t7" if _use_dir else f"correct_{period}"
        return_col = f"return_{period}"
        # v0.45.9 P0：模糊样本（|return| <= 容差，或回测取价失败的幽灵行）
        # 不计入准确率的分子与分母。COALESCE 兼容尚未回填的旧库。
        _amb = (" AND COALESCE(dir_ambiguous_t7, 0) = 0 AND dir_correct_t7 IS NOT NULL"
                if _use_dir else f" AND COALESCE(ambiguous_{period}, 0) = 0")

        # v32.3: 门面口径 —— 构建"排除非交易日"子句（周日 sample-accumulator 样本）
        _excl = ""
        _excl_p: list = []
        if exclude_nontrading_days:
            try:
                from is_trading_day import is_trading_day as _itd
                from datetime import date as _d_acc
                with sqlite3.connect(self.db_path) as _c0:
                    _dates = [r[0] for r in _c0.execute(
                        f"SELECT DISTINCT date FROM {self.TABLE} WHERE {checked_col}=1 AND date>=?",
                        (cutoff,)).fetchall()]
                _nt = []
                for _ds in _dates:
                    try:
                        if _ds and not _itd(_d_acc.fromisoformat(_ds))[0]:
                            _nt.append(_ds)
                    except Exception:
                        pass
                if _nt:
                    _excl = " AND date NOT IN (%s)" % ",".join("?" * len(_nt))
                    _excl_p = _nt
            except Exception:
                _excl = ""
                _excl_p = []

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row

                # 总体准确率
                row = conn.execute(f"""
                    SELECT
                        COUNT(*) as total,
                        SUM({correct_col}) as correct,
                        AVG({return_col}) as avg_ret,
                        AVG(final_score) as avg_score
                    FROM {self.TABLE}
                    WHERE {checked_col} = 1{_amb} AND date >= ?{_excl}
                """, (cutoff, *_excl_p)).fetchone()

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
                        WHERE {checked_col} = 1{_amb} AND direction = ? AND date >= ?{_excl}
                    """, (direction, cutoff, *_excl_p)).fetchone()
                    t = r["total"] or 0
                    raw_ret = r["avg_ret"] or 0
                    # 做空方向：股价下跌 = 正收益，需取反
                    adj_ret = -raw_ret if direction == "bearish" else raw_ret
                    by_direction[direction] = {
                        "total": t,
                        "correct": r["correct"] or 0,
                        "accuracy": (r["correct"] or 0) / t if t > 0 else 0.0,
                        "avg_return": round(adj_ret, 2),
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
                    WHERE {checked_col} = 1{_amb} AND date >= ?{_excl}
                    GROUP BY ticker
                    ORDER BY total DESC
                """, (cutoff, *_excl_p)).fetchall()
                for r in rows:
                    t = r["total"] or 0
                    by_ticker[r["ticker"]] = {
                        "total": t,
                        "correct": r["correct"] or 0,
                        "accuracy": (r["correct"] or 0) / t if t > 0 else 0.0,
                        "avg_return": round(r["avg_ret"] or 0, 2),
                        "avg_score": round(r["avg_score"] or 0, 1),
                    }

                # 整体 avg_return：用方向调整后的各组加权平均（排除 neutral）
                _dir_rets = [
                    (by_direction[d]["avg_return"], by_direction[d]["total"])
                    for d in ("bullish", "bearish")
                    if by_direction[d]["total"] > 0
                ]
                if _dir_rets:
                    _total_w = sum(w for _, w in _dir_rets)
                    _adj_avg = sum(r * w for r, w in _dir_rets) / _total_w
                else:
                    _adj_avg = row["avg_ret"] or 0

                # v0.37.0 可执行方向单口径：看多需 score>=6.0（决策阈值内），看空全算，
                # 中性与观望档（score<6 的看多）不计入 —— 反映"系统建议行动的单子"真实质量。
                # 全样本验证：该口径 56% acc（vs 混合口径把 <6.0 观望档 + 中性 |ret|<=3% 判据全算进来）
                actionable = {"total": 0, "correct": 0, "accuracy": 0.0, "avg_pnl": 0.0}
                try:
                    _act_row = conn.execute(f"""
                        SELECT
                            COUNT(*) as total,
                            SUM({correct_col}) as correct,
                            AVG(CASE WHEN direction='bullish' THEN {return_col}
                                     ELSE -{return_col} END) as avg_pnl
                        FROM {self.TABLE}
                        WHERE {checked_col} = 1{_amb} AND date >= ?{_excl}
                          AND ((direction = 'bullish' AND final_score >= 6.0)
                               OR direction = 'bearish')
                    """, (cutoff, *_excl_p)).fetchone()
                    _at = _act_row["total"] or 0
                    actionable = {
                        "total": _at,
                        "correct": _act_row["correct"] or 0,
                        "accuracy": round((_act_row["correct"] or 0) / _at, 3) if _at else 0.0,
                        "avg_pnl": round(_act_row["avg_pnl"] or 0, 2),
                    }
                except (sqlite3.Error, KeyError, TypeError):
                    pass

                # v0.45.17：方向单合计（bullish+bearish，**排除中性**）。
                # 中性的判定标准是「涨跌幅 < 5%」，与方向单的 1% 容差不是同一
                # 难度，混进一个分母报「整体准确率」会系统性抬高读数。
                _dir_tot = sum(by_direction.get(d, {}).get("total", 0)
                               for d in ("bullish", "bearish"))
                _dir_cor = sum(by_direction.get(d, {}).get("correct", 0)
                               for d in ("bullish", "bearish"))

                # v0.45.22：门面必须同时给出精度与显著性，否则一个 56.8% 看着像成绩。
                #  - Wilson CI 按**名义 n**算，只用于展示区间宽度；
                #  - 真正的显著性走**不重叠 ISO 周**序列做 t 检验 —— 30 只标的
                #    每日滚动 × T+7 持有，名义 n 高估约 25×，朴素检验必然虚高。
                _dir_ci = _dir_p = None
                _n_eff_weeks = 0
                if _use_dir and _dir_tot > 0:
                    _dir_ci = _wilson_ci(_dir_cor, _dir_tot)
                    try:
                        _wk_rows = conn.execute(f"""
                            SELECT date, {correct_col} AS c FROM {self.TABLE}
                            WHERE {checked_col} = 1{_amb} AND date >= ?{_excl}
                              AND direction IN ('bullish','bearish')
                        """, (cutoff, *_excl_p)).fetchall()
                        _by_wk: Dict = {}
                        for _wr in _wk_rows:
                            _iso = datetime.strptime(_wr["date"], "%Y-%m-%d").isocalendar()[:2]
                            _by_wk.setdefault(_iso, []).append(_wr["c"])
                        _series = [sum(v) / len(v) * 100 for v in _by_wk.values()
                                   if len(v) >= 3]
                        _n_eff_weeks = len(_series)
                        _dir_p = _t_test_vs(_series, 50.0)
                    except (sqlite3.Error, ValueError, KeyError, TypeError):
                        pass

                # ── v0.45.52：SPY 同期基准 ──
                # 「均收益 +0.10%」单看没有意义 —— 同期大盘是涨是跌决定了它
                # 到底算好还是算差。取**同一批已结算样本**的 spy_return_t7 均值，
                # 口径与 avg_return 对齐（同样的 cutoff、同样的模糊样本排除）。
                # 取不到就留 None，由渲染层整行省略 —— 不用 0 兜底，
                # 0 是「大盘恰好没动」，与「没算出来」完全是两回事。
                _spy_avg = _spy_n = None
                try:
                    _sr = conn.execute(f"""
                        SELECT AVG(spy_return_t7) AS a, COUNT(spy_return_t7) AS n
                        FROM {self.TABLE}
                        WHERE {checked_col} = 1{_amb} AND date >= ?{_excl}
                          AND spy_return_t7 IS NOT NULL
                    """, (cutoff, *_excl_p)).fetchone()
                    if _sr and _sr["n"]:
                        _spy_avg, _spy_n = round(float(_sr["a"]), 3), int(_sr["n"])
                except (sqlite3.Error, ValueError, KeyError, TypeError):
                    pass

                return {
                    "period": period,
                    "days_window": days,
                    "spy_avg_return": _spy_avg,    # 同期 SPY 均收益（%），None = 不可得
                    "spy_sample_n": _spy_n,
                    "metric": "direction" if _use_dir else "trade",
                    "directional_accuracy": round(_dir_cor / _dir_tot, 3) if _dir_tot else 0.0,
                    "directional_total": _dir_tot,
                    "directional_correct": _dir_cor,
                    "directional_ci": _dir_ci,          # Wilson 95%，按名义 n（仅示区间宽度）
                    "directional_p": _dir_p,            # 不重叠周 t 检验 vs 50%，唯一诚实的 p
                    "n_eff_weeks": _n_eff_weeks,
                    "overall_accuracy": round(overall_acc, 3),
                    "total_checked": total,
                    "correct_count": correct,
                    "avg_return": round(_adj_avg, 3),
                    "avg_score": round(row["avg_score"] or 0, 1),
                    "actionable": actionable,
                    "by_direction": by_direction,
                    "by_ticker": by_ticker,
                }
        except (sqlite3.Error, OSError, KeyError, TypeError) as e:
            _log.warning("获取准确率统计失败: %s", e)
            return {"overall_accuracy": 0, "total_checked": 0}

    def get_dimension_accuracy(self, period: str = "t7", days: int = 90) -> Dict:
        """
        S12：维度级精度追踪 — 按 5 个维度分别统计方向准确率

        解析每条预测的 dimension_scores JSON（{signal: {score, direction}, ...}），
        逐维度与实际收益比对，输出各维度命中率 + 建议权重微调。

        返回: {
            signal:    {accuracy: 0.72, samples: 45, suggested_weight: 0.32},
            catalyst:  {accuracy: 0.58, samples: 38, suggested_weight: 0.18},
            ...
        }
        """
        cutoff = (_pdt_now() - timedelta(days=days)).strftime("%Y-%m-%d")  # v0.33.0: 统计窗口口径 PDT 化（对齐 date 列）
        checked_col = f"checked_{period}"
        return_col = f"return_{period}"
        _amb = f" AND COALESCE(ambiguous_{period}, 0) = 0"  # v0.45.9 P0

        # Agent → 维度映射
        agent_dim = {
            "ScoutBeeNova":      "signal",
            "OracleBeeEcho":     "odds",
            "BuzzBeeWhisper":    "sentiment",
            "ChronosBeeHorizon": "catalyst",
            "GuardBeeSentinel":  "risk_adj",
        }
        # 方案10: 从 config 统一读取权重，消除硬编码 drift
        _fallback_w = {"signal": 0.30, "catalyst": 0.20, "sentiment": 0.20, "odds": 0.15, "risk_adj": 0.15}
        try:
            from config import EVALUATION_WEIGHTS as _EW
            default_weights = {k: _EW.get(k, _fallback_w[k]) for k in _fallback_w}
        except (ImportError, AttributeError):
            default_weights = _fallback_w

        dim_stats = {d: {"correct": 0, "total": 0} for d in default_weights}

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(f"""
                    SELECT agent_directions, {return_col}
                    FROM {self.TABLE}
                    WHERE {checked_col} = 1{_amb} AND agent_directions IS NOT NULL AND date >= ?
                """, (cutoff,)).fetchall()

                for row in rows:
                    try:
                        dirs = json.loads(row["agent_directions"])
                        ret = row[return_col]
                        if ret is None:
                            continue
                        for agent_name, dim in agent_dim.items():
                            agent_dir = dirs.get(agent_name)
                            if not agent_dir:
                                continue
                            # 方案12: 统一使用共享判定函数
                            # v0.45.9 P0：三态判定，模糊样本不进 total
                            from outcome_utils import determine_outcome_triplet
                            _ok, _amb_row = determine_outcome_triplet(agent_dir, ret)
                            if _amb_row:
                                continue
                            dim_stats[dim]["total"] += 1
                            if _ok:
                                dim_stats[dim]["correct"] += 1
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue

        except (sqlite3.Error, OSError) as e:
            _log.warning("维度级精度统计失败: %s", e)
            return {}

        # 计算各维度准确率 + 建议权重
        result = {}
        raw_weights = {}
        for dim in default_weights:
            total = dim_stats[dim]["total"]
            correct = dim_stats[dim]["correct"]
            acc = correct / total if total > 0 else 0.5
            result[dim] = {
                "accuracy": round(acc, 3),
                "samples": total,
                "correct": correct,
            }
            raw_weights[dim] = max(0.05, acc ** 2)  # 准确率^2 归一化

        # 建议权重（±0.05 范围内微调 + 归一化确保总和=1.0）
        total_raw = sum(raw_weights.values())
        if total_raw > 0:
            suggested = {}
            for dim in default_weights:
                ideal = raw_weights[dim] / total_raw
                suggested[dim] = max(default_weights[dim] - 0.05,
                                     min(default_weights[dim] + 0.05, ideal))
            # 归一化：clamping 后总和可能偏离 1.0
            sw_sum = sum(suggested.values())
            if sw_sum > 0:
                for dim in suggested:
                    result[dim]["suggested_weight"] = round(suggested[dim] / sw_sum, 3)

        _log.info("S12 维度级精度: %s",
                  {d: f"{v['accuracy']:.1%}({v['samples']})" for d, v in result.items()})
        return result

    def get_all_predictions(self, days: int = 30) -> List[Dict]:
        """获取最近 N 天所有预测"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(f"""
                    SELECT * FROM {self.TABLE}
                    WHERE date >= ? ORDER BY date DESC, ticker
                """, (cutoff,)).fetchall()
                return [dict(r) for r in rows]
        except (sqlite3.Error, OSError) as e:
            _log.warning("获取预测列表失败: %s", e)
            return []


# ==================== v0.45.120：批量日线帧的拆分与切片（纯函数，便于离线测试） ====================

def _split_download_frame(raw, tickers: List[str]) -> Dict[str, "object"]:
    """把多票 `yf.download(group_by="ticker")` 的结果拆成 {ticker: 平列名 OHLC 帧}。

    只收「有 Close 且去 NaN 后非空」的票；其余不进字典，调用方按未缓存处理
    （走逐票回退）。形状不认识（多票却平列名）→ 空字典，整轮回退。
    """
    out: Dict[str, "object"] = {}
    if raw is None or getattr(raw, "empty", True):
        return out
    cols = raw.columns
    picked: Dict[str, "object"] = {}
    if hasattr(cols, "levels"):
        lvl0 = set(cols.get_level_values(0))
        lvl1 = set(cols.get_level_values(1))
        for t in tickers:
            if t in lvl0:
                picked[t] = raw[t]
            elif t in lvl1:
                picked[t] = raw.xs(t, axis=1, level=1)
    elif len(tickers) == 1:
        picked[tickers[0]] = raw
    else:
        return out
    for t, sub in picked.items():
        if "Close" not in sub.columns:
            continue
        sub = sub.dropna(subset=["Close"])
        if sub.empty:
            continue
        out[t] = sub
    return out


def _slice_by_date(df, start, end):
    """`df` 中日期落在 [start, end) 的行。按 `index.date` 比较，tz-aware / naive 皆可。"""
    dates = df.index.date
    mask = (dates >= start) & (dates < end)
    return df[mask]


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
        self._spy_entry_cache: Dict[str, float] = {}
        # v0.45.120：一轮回测内的日线缓存（ticker → 整段 OHLC），由
        # `_prefetch_backtest_prices` 一次 `yf.download` 填满，三个取价点
        # 通过 `_history()` 切片。窗口外/未缓存的请求走原来的逐票 history。
        self._ohlc_cache: Dict[str, "object"] = {}
        self._ohlc_window: Optional[tuple] = None      # (start_date, end_date)，[start, end)
        self._ohlc_stats = {"batch_downloads": 0, "batch_tickers": 0,
                            "cache_hits": 0, "fallback_history": 0}

    def _store_path_result(
        self, pred_id, price_t7, return_t7, is_correct,
        net_return_pct, exit_reason, exit_date, exit_price,
        holding_days, cost_breakdown, spy_return,
        ambiguous_t7=False,
        close_t7=None, dir_correct_t7=None, dir_ambiguous_t7=None,
    ):
        """代理调用 PredictionStore。"""
        return self.store.update_t7_path_result(
            pred_id=pred_id,
            price_t7=price_t7,
            return_t7=return_t7,
            correct_t7=is_correct,
            net_return_pct=net_return_pct,
            exit_reason=exit_reason,
            exit_date=exit_date,
            exit_price=exit_price,
            holding_days=holding_days,
            cost_breakdown=cost_breakdown,
            spy_return=spy_return,
            ambiguous_t7=ambiguous_t7,
            close_t7=close_t7,
            dir_correct_t7=dir_correct_t7,
            dir_ambiguous_t7=dir_ambiguous_t7,
        )

    # ==================== v0.45.120：回测批量取价 ====================
    #
    # 2026-09-04 实测：回测段 342s，其中待检预测 32 条全是**同一预测日**的 30 只票，
    # 却逐条各发一次 `yf.Ticker().history()`；t7 一条还要发 4 次（路径 OHLC、
    # SPY 收盘、SPY 入场、未截断 T+7 收盘）。每次都过 `yf_gate` 的 0.5 req/s 闸门，
    # 于是 ~40 次串行 × (2s 闸 + 延迟) 就是那 5 分钟。
    #
    # 改法：开跑前按全部待检预测算出一个 [最早预测日, 今天+11) 的窗口，所有票
    # （含 SPY）一次 `yf.download` 拉回来放进 `_ohlc_cache`，三个取价点改走
    # `_history()` 从缓存切 [start, end)——切片语义与 `Ticker.history(start, end)`
    # 一致（含 start、不含 end、按交易所日历的日期比较）。
    #
    # 不变的部分：
    #   · 缓存未覆盖（窗口外 / 批量下载失败 / 某票全 NaN）→ 原样走逐票 history，
    #     退化路径就是改动前的路径，不是另一套逻辑；
    #   · 未收盘护栏（`_get_price_at_date` 里的 `_exchange_now` 判定）照旧作用在
    #     切片结果上——批量下载同样会带回今天正在形成的那根 bar；
    #   · 失败不入缓存：download 抛错/空帧只记 warning，本轮全部回退。
    #
    # 多票 download 的两个坑（见记忆 alpha-hive-yfinance-multiindex）：
    #   · 列是 MultiIndex，`group_by="ticker"` 时 level-0 是票名；单票有时平列名；
    #   · 各票交易日历不同时用 NaN 行对齐——`Ticker.history` 不会有这些行，
    #     必须 `dropna(subset=["Close"])` 才是同一口径。

    _OHLC_TAIL_DAYS = 11   # 窗口末尾裕度：_get_price_at_date 的 end = 目标日 + 10

    def _prefetch_backtest_prices(self, pending_map: Dict[str, List[Dict]]) -> None:
        """按待检预测一次性批量下载日线。失败静默回退（记 warning），不抛。"""
        if yf is None:
            return
        tickers = set()
        dates = []
        for rows in pending_map.values():
            for pred in rows or []:
                t = pred.get("ticker")
                d = pred.get("date")
                if t and d:
                    tickers.add(str(t))
                    dates.append(str(d))
        if not tickers or not dates:
            return
        tickers.add("SPY")   # t7 基准：入场价 + 同期收盘
        try:
            start_date = datetime.strptime(min(dates), "%Y-%m-%d").date()
            # `_pdt_today()` 回的是 "YYYY-MM-DD" 字符串（与 get_pending_checks 同钟）
            end_date = (datetime.strptime(_pdt_today(), "%Y-%m-%d").date()
                        + timedelta(days=self._OHLC_TAIL_DAYS))
        except (ValueError, TypeError) as e:
            _log.warning("回测批量取价：日期解析失败，回退逐票取价：%s", e)
            return

        symbols = sorted(tickers)
        try:
            raw = yf.download(
                tickers=symbols,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                group_by="ticker",
                auto_adjust=True,      # 与 Ticker.history() 的默认一致
                progress=False,
                threads=False,         # 顺序打 Yahoo，不用 30 并发去撞 429
            )
        except Exception as e:  # noqa: BLE001 - 批量失败只回退，不阻断回测
            _log.warning("回测批量取价失败（%s: %s），本轮回退逐票取价",
                         type(e).__name__, e)
            return

        frames = _split_download_frame(raw, symbols)
        if not frames:
            _log.warning("回测批量取价：download 返回空帧或形状不认识，本轮回退逐票取价")
            return
        self._ohlc_cache = frames
        self._ohlc_window = (start_date, end_date)
        self._ohlc_stats["batch_downloads"] += 1
        self._ohlc_stats["batch_tickers"] += len(frames)
        missing = sorted(set(symbols) - set(frames))
        _log.info("回测批量取价：1 次 download 覆盖 %d/%d 只（%s ~ %s）%s",
                  len(frames), len(symbols), start_date, end_date,
                  f"，缺 {' '.join(missing)} 走逐票回退" if missing else "")

    def _history(self, ticker: str, start: str, end: str):
        """`yf.Ticker(ticker).history(start=, end=)` 的等价物：命中缓存就切片，
        否则原样逐票取。异常行为与原来完全一致（回退分支抛什么，这里就抛什么）。"""
        # getattr 带默认：`Backtester.__new__` 造出来的实例（既有测试与备份脚本
        # 的用法）没有这几个属性，必须表现得和「没预取」完全一样，而不是 AttributeError。
        win = getattr(self, "_ohlc_window", None)
        stats = getattr(self, "_ohlc_stats", None)
        df = getattr(self, "_ohlc_cache", {}).get(ticker) if win else None
        if df is not None:
            try:
                s = datetime.strptime(start, "%Y-%m-%d").date()
                e = datetime.strptime(end, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                s = e = None
            if s is not None and win[0] <= s and e <= win[1]:
                if stats is not None:
                    stats["cache_hits"] += 1
                return _slice_by_date(df, s, e)
        if stats is not None:
            stats["fallback_history"] += 1
        return yf.Ticker(ticker).history(start=start, end=end)

    def _get_spy_entry_price(self, predict_date: str) -> Optional[float]:
        """获取 SPY 在 predict_date 的收盘价（作为 benchmark 入场价），带缓存。"""
        if predict_date in self._spy_entry_cache:
            return self._spy_entry_cache[predict_date]
        if yf is None:
            return None
        try:
            start = datetime.strptime(predict_date, "%Y-%m-%d")
            end = start + timedelta(days=5)
            hist = self._history("SPY", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            if hist.empty:
                return None
            px = float(hist["Close"].iloc[0])
            self._spy_entry_cache[predict_date] = px
            return px
        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError):
            return None

    # ==================== 保存预测 ====================

    def save_predictions(self, swarm_results: Dict, date: Optional[str] = None) -> int:
        """
        将蜂群扫描结果保存为预测记录

        Args:
            swarm_results: {ticker: {final_score, direction, dimension_scores, ...}}
            date: **业务日期**（YYYY-MM-DD）。调用方有报告日期时**必须传入** ——
                  留空会回退到写入时刻的 PDT 当日，`--date` 补跑场景下会导致
                  多个业务日的预测盖成同一天并互相 REPLACE（v0.42.4 修复的 bug）。

        Returns:
            保存的记录数。**调用方应检查返回值**：返回 0 而 swarm_results 非空
            意味着学习闭环本次未获得任何样本。
        """
        saved = 0
        _unusable: List[str] = []  # v0.45.50：落库但无入场价 ⇒ 统计上不存在
        for ticker, data in swarm_results.items():
            if not isinstance(data, dict):
                continue

            # v0.43.15: 整只票的处理包进 try——此前 yfinance 限流异常
            # （YFRateLimitError 不在旧 except 清单里）会穿透并杀死整个循环，
            # 导致当天 0 条落库（自动扫描 14:00 整点常撞限流，7/28~8/11 多个
            # 交易日 predictions 全空即此因；手动跑的时段限流少所以能落库）。
            try:
                # 收集各 Agent 的方向（从 QueenDistiller 的 agent_directions 字段）
                agent_dirs = data.get("agent_directions", {})

                # v0.43.15: 价格优先复用 swarm_results 里的共享快照价
                # （ScoutBee 的 CBOE-first 价，与报告/仪表板同一口径），
                # 不再为落库单独打一轮 yfinance——那既是限流引爆点，
                # 又与报告价格口径不一致（fast_info 是另一时刻的实时价）。
                price = 0.0
                try:
                    price = float(
                        (data.get("agent_details") or {})
                        .get("ScoutBeeNova", {})
                        .get("details", {})
                        .get("price") or 0.0
                    )
                except (TypeError, ValueError):
                    price = 0.0
                if not price or price <= 0:
                    # 兜底：swarm 里没价才打 yfinance，且宽捕获（含 YFRateLimitError）
                    try:
                        if yf:
                            stock = yf.Ticker(ticker)
                            price = stock.fast_info.get("lastPrice", 0)
                    except Exception as e:  # noqa: BLE001 - 任何取价失败都不阻断落库
                        _log.debug("Price fetch failed for %s: %s", ticker, e)

                # ── v0.45.50：price=0 不是「零元」，是「这条样本作废」──
                # predictions.price_at_predict 是全部收益计算的入场价，
                # 而**13 处 SQL 带 `price_at_predict > 0`**（signal_archive /
                # ic_diagnostics / ic_rerun_readiness / replay_scoring / portfolio_backtest …）。
                # 所以 price=0 的行对**全部统计不可见** —— 它不是一条差样本，
                # 是一条根本不存在的样本。
                # 而旧实现下 `saved += 1` 照常计数、日志照常报「已落库 N 条」，
                # 两个取价路径的失败又都只有 debug —— 样本静默消失。
                # DB 实测：BRK-B 2026-08-12 与 08-14 两条即如此（ticker 正则那次事故的遗留）。
                if not price or price <= 0:
                    _unusable.append(ticker)
                    _log.warning("[%s] 取价失败（swarm 与 yfinance 兜底均无价）——"
                                 "该条落库后 price_at_predict=0，**对全部统计不可见**", ticker)

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
                    date=date,
                )
                if ok:
                    saved += 1
            except Exception as e:  # noqa: BLE001 - 单票失败不得杀死整个落库循环
                _log.warning("save_predictions 单票处理失败 %s: %s", ticker, e)

        # v0.45.50：把「落库了」与「落库且可用」分开报。
        # 返回值保持 int（调用方与既有测试依赖它），明细挂在实例上。
        self.last_save_stats = {
            "saved": saved,
            "unusable_no_price": len(_unusable),
            "unusable_tickers": list(_unusable),
        }
        if _unusable:
            _log.warning("落库 %d 条，其中 **%d 条无入场价、对全部统计不可见**：%s",
                         saved, len(_unusable), ", ".join(_unusable))
        return saved

    # ==================== 执行回测 ====================

    def run_backtest(self) -> Dict:
        """
        执行回测检验：检查所有到期的预测

        返回: {t1: {checked, correct}, t7: {...}, t30: {...}}
        """
        # 回测检验
        results = {}

        # v0.45.120：先把三个周期的待检都取出来，一次批量下载覆盖全部取价点
        pending_map = {p: (self.store.get_pending_checks(p) or []) for p in ("t1", "t7", "t30")}
        try:
            self._prefetch_backtest_prices(pending_map)
        except Exception as _pe:  # noqa: BLE001 - 预取是优化，不是前提
            _log.warning("回测批量取价异常（%s: %s），回退逐票取价", type(_pe).__name__, _pe)

        for period in ["t1", "t7", "t30"]:
            pending = pending_map[period]
            if not pending:
                results[period] = {"checked": 0, "correct": 0, "skipped": 0}
                continue

            days_map = {"t1": 1, "t7": 7, "t30": 30}
            days = days_map[period]
            checked = 0
            correct = 0
            skipped = 0
            ambiguous = 0   # v0.45.9 P0：|return| <= 容差，不评分

            # {period.upper()} 回测

            for pred in pending:
                ticker = pred["ticker"]
                predict_date = pred["date"]
                predict_price = pred.get("price_at_predict", 0)
                direction = pred["direction"]

                if not predict_price or predict_price <= 0:
                    skipped += 1
                    continue

                # ── Sprint 1 / P0-1: T+7 使用路径依赖退出；T+1/T+30 沿用旧逻辑 ──
                if period == "t7":
                    path = self._simulate_trade_path(
                        ticker, predict_date, days, predict_price, direction
                    )
                    if not path:
                        skipped += 1
                        continue

                    actual_price = path["exit_price"]
                    # gross_return_pct 已经是"方向调整后"的净方向收益（看空已取反）
                    ret = path["gross_return_pct"]

                    # 判断方向正确。v0.45.9 P0：原 `ret > -1.0` 是单边亏损豁免
                    # （亏 0.9% 记为判对）。改为双边模糊带：|ret| <= 容差 → 不评分。
                    # ret 已是方向调整后收益（看空取反），故按 bullish 语义判定。
                    from outcome_utils import determine_outcome_triplet
                    is_correct, is_ambiguous = determine_outcome_triplet("bullish", ret)

                    # ── Sprint 1 / P0-2: 应用交易成本得到净收益 ──
                    try:
                        from trading_costs import apply_costs
                        cost_res = apply_costs(
                            gross_return_pct=ret,
                            direction=direction,
                            ticker=ticker,
                            holding_days=path.get("holding_days", days),
                        )
                        net_ret = cost_res["net_return_pct"]
                        cost_breakdown = cost_res["breakdown"]
                    except Exception as _ce:
                        # ── v0.45.50：净收益算不出时，不许把毛收益冒充成净收益 ──
                        # net_return_t7 这一列在 portfolio_backtest.py:11 被明确定义为
                        # 「含滑点+佣金+借券费」，而这里写进去的是**未扣成本的毛收益**，
                        # 且 cost_breakdown={} 让事后审计也查不出是哪几笔。
                        # 下游 portfolio_backtest.load_verified_predictions 用
                        # `WHERE net_return_t7 IS NOT NULL` 取样 —— 于是毛收益混进净收益
                        # 样本后**没有任何标记可区分**，Net 权益曲线与全部组合级 KPI 被高估。
                        # 现改为置 None：该笔从净收益口径中诚实缺席，而不是伪装成一笔零成本交易。
                        _log.warning("[%s] 成本计算失败，net_return_t7 置 None"
                                     "（不以毛收益冒充净收益）：%s: %s",
                                     ticker, type(_ce).__name__, _ce)
                        net_ret = None
                        cost_breakdown = {}

                    # SPY 同期基准
                    spy_ret = None
                    try:
                        spy_close = self._get_price_at_date("SPY", predict_date, days)
                        spy_entry = self._get_spy_entry_price(predict_date)
                        if spy_close and spy_entry and spy_entry > 0:
                            spy_ret = round((spy_close - spy_entry) / spy_entry * 100, 4)
                    except Exception as _se:
                        _log.debug("SPY benchmark fetch failed %s: %s", predict_date, _se)

                    # 存路径 + 净收益 + 基准
                    # ret 现在是方向调整后的毛收益，存到 return_t7（兼容旧代码）
                    # 看空情况下旧代码期待"原始价格变动"，因此需要反回去存
                    _dir_lc = (direction or "").strip().lower()
                    raw_ret_store = -ret if _dir_lc == "bearish" else ret  # 还原原始价格变动以兼容旧显示

                    # ── v0.45.17：另取一次**未截断的 T+7 收盘价**判方向 ──
                    # 上面的 is_correct 由 path["gross_return_pct"] 算出，
                    # 而 path 在触发 SL/TP 时会提前离场，收益被钳在止损/止盈档位
                    # （库里 `-10.04` / `+9.95` 反复出现即此故）。用它判方向等于问
                    # 「这笔交易赚钱了吗」，不是「方向猜对了吗」。中性预测从不建仓、
                    # 从无 SL/TP，两者混一个分母就是苹果比橘子。
                    # 故此处独立取 T+7 收盘价，只回答方向问题。取价失败时留 None，
                    # 由 backfill_dir_accuracy.py 补齐——**不拿离场价冒充收盘价**。
                    _close_t7 = self._get_price_at_date(ticker, predict_date, days)
                    _dir_ok = _dir_amb = None
                    if _close_t7 and _close_t7 > 0:
                        _raw_ret = (_close_t7 - predict_price) / predict_price * 100
                        _dir_ok, _dir_amb = determine_outcome_triplet(direction, _raw_ret)

                    self._store_path_result(
                        pred["id"], actual_price, round(raw_ret_store, 3), is_correct,
                        net_return_pct=net_ret,
                        exit_reason=path["exit_reason"],
                        exit_date=path.get("exit_date"),
                        exit_price=actual_price,
                        holding_days=path.get("holding_days", days),
                        cost_breakdown=cost_breakdown,
                        spy_return=spy_ret,
                        ambiguous_t7=is_ambiguous,
                        close_t7=_close_t7,
                        dir_correct_t7=_dir_ok,
                        dir_ambiguous_t7=_dir_amb,
                    )
                else:
                    # T+1 / T+30 沿用旧逻辑
                    actual_price = self._get_price_at_date(ticker, predict_date, days)
                    if actual_price is None or actual_price <= 0:
                        skipped += 1
                        continue
                    ret = (actual_price - predict_price) / predict_price * 100
                    is_correct, is_ambiguous = self._check_direction(direction, ret)
                    self.store.update_check_result(
                        pred["id"], period, actual_price, round(ret, 3), is_correct,
                        ambiguous=is_ambiguous,
                    )

                # T+1 期权回验：记录 T+1 的 IV Rank 变化
                if period == "t1" and pred.get("iv_rank") is not None:
                    self._check_options_t1(pred)

                # v0.45.9 P0：模糊样本不计入 checked/correct 分母分子
                if is_ambiguous:
                    ambiguous += 1
                    continue
                checked += 1
                if is_correct:
                    correct += 1

            results[period] = {
                "checked": checked,
                "correct": correct,
                "ambiguous": ambiguous,
                "skipped": skipped,
                "accuracy": correct / checked if checked > 0 else 0,
            }

            pass  # 准确率已计算

        st = getattr(self, "_ohlc_stats", None) or {
            "batch_downloads": 0, "batch_tickers": 0, "cache_hits": 0, "fallback_history": 0}
        _log.info("回测取价：批量下载 %d 次覆盖 %d 只 | 缓存切片 %d 次 | 逐票回退 %d 次",
                  st["batch_downloads"], st["batch_tickers"],
                  st["cache_hits"], st["fallback_history"])
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

            hist = self._history(
                ticker, target_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))

            if hist.empty:
                return None

            # v0.45.10: 目标交易日**尚未收盘**时，yfinance 返回的那根 bar 是
            # "正在形成"的——它的 Close 是此刻最新价，不是收盘价。此前无护栏，
            # 于是任何盘中运行都会拿盘中价当收盘价评分。
            #
            # 实测 2026-08-24 那批 T+1（08-25 盘中评的）30 条里抽 5 只有 2 只判反：
            #   AMC  记 +2.251% 判对，真实收盘 -1.124% 应判错
            #   BILI 记 +0.812%（看空却收益为正）判对，应判错
            #
            # 正常 14:00 PDT 定时扫描在 13:00 收盘后跑，所以这个洞一直没暴露。
            # 拿不到收盘价时返回 None —— 调用方会跳过，预测留在"待检"，
            # 下次收盘后再评。**绝不用盘中价冒充收盘价。**
            #
            # 判据用交易所真实时钟（_exchange_now，来自 Yahoo 服务器），
            # 不依赖本机钟——与 data_pipeline._drop_forming_bar 同一套判据。
            # 那个函数丢的是末根且要求 len>=3，此处取 iloc[0] 且常只有 1 根，
            # 复用不了，故单独判。
            try:
                from datetime import time as _dt_time
                from data_pipeline import _exchange_now
                _xnow = _exchange_now()
                if (_xnow is not None
                        and hist.index[0].date() == _xnow.date()
                        and _xnow.time() < _dt_time(15, 59)):
                    _log.info(
                        "[%s] T+%d 目标日 %s 尚未收盘（交易所时间 %s），"
                        "跳过本次评分，留待收盘后重评",
                        ticker, days_ahead, hist.index[0].date(),
                        _xnow.strftime("%H:%M"),
                    )
                    return None
            except Exception as _e_fb:  # noqa: BLE001 - 护栏失效不该阻断回测
                _log.debug("未收盘护栏检查失败 %s: %s", ticker, _e_fb)

            # 取目标交易日（或之后第一个交易日）的收盘价
            return float(hist["Close"].iloc[0])

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("Future price fetch failed for %s +%dd: %s", ticker, days_ahead, e)
            return None

    # ==================== Sprint 1 / P0-1: 路径依赖退出 ====================

    def _simulate_trade_path(
        self,
        ticker: str,
        predict_date: str,
        days_ahead: int,
        entry_price: float,
        direction: str,
    ) -> Optional[Dict]:
        """模拟交易路径：逐日 OHLC 检查止损/止盈触发，否则持有到 T+N 收盘。

        真实交易语义：
          - 看多：涨到 +TP% 止盈，跌到 -SL% 止损
          - 看空：跌到 +TP%（即标的跌 TP%）止盈，涨到 +SL%（即标的涨 SL%）止损
          - 中性：不触发任何出场，持有到期

        返回:
            {
                "exit_date": "YYYY-MM-DD",
                "exit_price": float,
                "exit_reason": "TP" | "SL" | "T7_CLOSE",
                "gross_return_pct": float,   # 方向调整后的毛收益
                "holding_days": int,
            }
            or None (数据缺失)
        """
        if yf is None or not entry_price or entry_price <= 0:
            return None

        try:
            import config as _cfg
            _exit_cfg = getattr(_cfg, "TRADING_EXITS_CONFIG", {})
            if not _exit_cfg.get("enabled", True):
                # fallback: 用旧逻辑返回 T+N 收盘
                close_px = self._get_price_at_date(ticker, predict_date, days_ahead)
                if close_px is None:
                    return None
                raw_ret = (close_px - entry_price) / entry_price * 100
                _dir = (direction or "").strip().lower()
                dir_adj = -raw_ret if _dir == "bearish" else raw_ret
                return {
                    "exit_date": None,
                    "exit_price": close_px,
                    "exit_reason": "T7_CLOSE",
                    "gross_return_pct": round(dir_adj, 4),
                    "holding_days": days_ahead,
                }

            # 升级2: per-ticker 自适应止损
            _sl_overrides = _exit_cfg.get("sl_overrides") or {}
            sl_pct = float(_sl_overrides.get(ticker, _exit_cfg.get("stop_loss_pct", 5.0)))
            tp_pct = float(_exit_cfg.get("take_profit_pct", 10.0))
            exit_slip_bps = float(_exit_cfg.get("slippage_on_exit_bps", 5))

            _dir = (direction or "").strip().lower()

            # 拉 T+0 ~ T+N+缓冲 OHLC
            start_dt = datetime.strptime(predict_date, "%Y-%m-%d")
            if _BDAY_AVAILABLE:
                import pandas as _pd
                end_ts = _pd.Timestamp(start_dt) + (days_ahead + 3) * _US_BDAY
                end_dt = end_ts.to_pydatetime()
            else:
                end_dt = start_dt + timedelta(days=int((days_ahead + 3) * 1.5))

            hist = self._history(
                ticker,
                (start_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
                (end_dt + timedelta(days=2)).strftime("%Y-%m-%d"),
            )

            if hist.empty:
                return None

            # 修复 Bug #14：按真实交易日过滤（yfinance hist 本身已是交易日，但为防御 index 含 NaT）
            # 旧实现 `hist.head(days_ahead)` 按行数截断，在停牌/假日裕度下可能 holding<7 却落 T7_CLOSE
            try:
                hist = hist[~hist.index.isna()]
            except Exception:
                pass
            hist = hist.head(days_ahead) if len(hist) > days_ahead else hist
            if hist.empty:
                return None

            # 计算阈值价（基于 entry_price 和方向）
            if _dir == "bullish":
                tp_price = entry_price * (1 + tp_pct / 100.0)
                sl_price = entry_price * (1 - sl_pct / 100.0)
            elif _dir == "bearish":
                tp_price = entry_price * (1 - tp_pct / 100.0)   # 标的跌到这即止盈
                sl_price = entry_price * (1 + sl_pct / 100.0)   # 标的涨到这即止损
            else:
                # 中性方向：只有宽松止损保护（防止 CRCL -30% 类灾难），无止盈
                _neutral_sl = float(_exit_cfg.get("neutral_sl_pct", 15.0))
                sl_price = entry_price * (1 - _neutral_sl / 100.0)  # 下跌保护
                tp_price = None  # 中性不设止盈

            # 修复 Bug #12/#13：Gap-aware exit_px + 显式 direction 白名单 + 方向规范化
            # 旧实现：gap down 穿透 SL 时 exit_px=sl_price 低估亏损；`_dir not in (...)` 吞掉所有未知值
            _valid_dirs = ("bullish", "bearish", "neutral")
            _dir_normalized = _dir if _dir in _valid_dirs else "neutral"  # 未知值走中性保护

            # 逐日扫描 OHLC
            holding = 0
            for idx, row in hist.iterrows():
                holding += 1
                day_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                try:
                    op = float(row["Open"])
                    hi, lo, close = float(row["High"]), float(row["Low"]), float(row["Close"])
                except (KeyError, ValueError, TypeError):
                    continue

                if _dir_normalized == "bullish" and tp_price and sl_price:
                    hit_sl = lo <= sl_price
                    hit_tp = hi >= tp_price
                    # 保守：同日同时触发 → 假设先触发 SL
                    if hit_sl:
                        # 修复 #12：gap-aware — gap down 穿透 SL 时 open 已低于 sl_price，用 open 作成交价
                        fill_price = min(op, sl_price)
                        exit_px = fill_price * (1 - exit_slip_bps / 10000.0)
                        return {
                            "exit_date": day_str,
                            "exit_price": round(exit_px, 4),
                            "exit_reason": "SL",
                            "gross_return_pct": round((exit_px - entry_price) / entry_price * 100, 4),
                            "holding_days": holding,
                        }
                    if hit_tp:
                        # gap up 情况：open 已高于 tp_price，按 open 成交（对策略保守）
                        fill_price = max(op, tp_price) if op > tp_price else tp_price
                        exit_px = fill_price * (1 - exit_slip_bps / 10000.0)
                        return {
                            "exit_date": day_str,
                            "exit_price": round(exit_px, 4),
                            "exit_reason": "TP",
                            "gross_return_pct": round((exit_px - entry_price) / entry_price * 100, 4),
                            "holding_days": holding,
                        }
                elif _dir_normalized == "neutral" and sl_price:
                    # 中性方向：只检查下跌止损，无止盈
                    if lo <= sl_price:
                        fill_price = min(op, sl_price)  # gap-aware
                        exit_px = fill_price * (1 - exit_slip_bps / 10000.0)
                        return {
                            "exit_date": day_str,
                            "exit_price": round(exit_px, 4),
                            "exit_reason": "SL",
                            "gross_return_pct": round((exit_px - entry_price) / entry_price * 100, 4),
                            "holding_days": holding,
                        }
                elif _dir_normalized == "bearish" and tp_price and sl_price:
                    hit_sl = hi >= sl_price     # 标的涨 → 空头止损
                    hit_tp = lo <= tp_price     # 标的跌 → 空头止盈
                    if hit_sl:
                        # 修复 #12：gap up 穿透空头 SL 时用 open（更差价）
                        fill_price = max(op, sl_price)
                        exit_px = fill_price * (1 + exit_slip_bps / 10000.0)
                        gross = (entry_price - exit_px) / entry_price * 100
                        return {
                            "exit_date": day_str,
                            "exit_price": round(exit_px, 4),
                            "exit_reason": "SL",
                            "gross_return_pct": round(gross, 4),
                            "holding_days": holding,
                        }
                    if hit_tp:
                        fill_price = min(op, tp_price) if op < tp_price else tp_price
                        exit_px = fill_price * (1 + exit_slip_bps / 10000.0)
                        gross = (entry_price - exit_px) / entry_price * 100
                        return {
                            "exit_date": day_str,
                            "exit_price": round(exit_px, 4),
                            "exit_reason": "TP",
                            "gross_return_pct": round(gross, 4),
                            "holding_days": holding,
                        }

            # 未触发 → 按最后一根 K 线收盘平仓
            last_row = hist.iloc[-1]
            last_close = float(last_row["Close"])
            last_idx = hist.index[-1]
            last_day_str = last_idx.strftime("%Y-%m-%d") if hasattr(last_idx, "strftime") else str(last_idx)[:10]
            raw_ret = (last_close - entry_price) / entry_price * 100
            dir_adj = -raw_ret if _dir == "bearish" else raw_ret
            return {
                "exit_date": last_day_str,
                "exit_price": round(last_close, 4),
                "exit_reason": "T7_CLOSE",
                "gross_return_pct": round(dir_adj, 4),
                "holding_days": len(hist),
            }

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("Trade path simulation failed %s %s +%dd: %s",
                       ticker, predict_date, days_ahead, e)
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
                try:
                    with sqlite3.connect(self.store.db_path) as conn:
                        conn.execute(f"""
                            UPDATE {PredictionStore.TABLE}
                            SET iv_rank_t1 = ?
                            WHERE id = ?
                        """, (iv_rank_t1, pred["id"]))
                        conn.commit()
                except (sqlite3.Error, OSError) as e:
                    _log.debug("IV Rank T+1 update failed: %s", e)

        except (ImportError, ConnectionError, TimeoutError, OSError,
                ValueError, KeyError, TypeError) as e:
            _log.debug("Options T+1 check skipped for %s: %s", ticker, e)

    def _check_direction(self, direction: str, actual_return: float):
        """
        检查预测方向是否正确（方案12: 统一标准）

        v0.45.9 P0：返回值由 bool 改为 (correct, ambiguous) 二元组。
        原实现是单边容差（看多亏 0.9% 记为判对），现改为双边模糊带：
        |return| <= 容差 → ambiguous，既不算对也不算错，从统计中剔除。

        Args:
            direction: "bullish" / "bearish" / "neutral"
            actual_return: 实际收益率（百分比，如 5.0 = +5%）

        Returns:
            (correct: bool, ambiguous: bool)
        """
        from outcome_utils import determine_outcome_triplet
        return determine_outcome_triplet(direction, actual_return)

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
          with sqlite3.connect(self.store.db_path) as conn:
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

        except (sqlite3.Error, OSError, KeyError, TypeError) as e:
            lines.append(f"  期权回验查询失败: {e}")

        # S12: 维度级精度
        lines.append("\n  [维度级精度（S12）]")
        try:
            dim_acc = self.store.get_dimension_accuracy("t7", days)
            if dim_acc:
                # 用固定宽度标签避免中文字符宽度不一致
                dim_cn = {"signal": "信号  ", "catalyst": "催化剂", "sentiment": "情绪  ",
                          "odds": "赔率  ", "risk_adj": "风控  "}
                for dim, info in dim_acc.items():
                    label = dim_cn.get(dim, dim)
                    if info["samples"] > 0:
                        sw = info.get("suggested_weight", "—")
                        sw_str = f" →建议{sw:.3f}" if isinstance(sw, float) else ""
                        lines.append(
                            f"  {label}: "
                            f"{info['accuracy']*100:5.1f}% "
                            f"({info['correct']}/{info['samples']}){sw_str}"
                        )
                    else:
                        lines.append(f"  {label}: 样本不足")
            else:
                lines.append("  暂无维度级精度数据")
        except (KeyError, TypeError, ValueError) as e:
            lines.append(f"  维度精度查询失败: {e}")

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
        try:
            with sqlite3.connect(self.store.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
                rows = conn.execute(f"""
                    SELECT pheromone_compact, correct_{period}, return_{period}
                    FROM {PredictionStore.TABLE}
                    WHERE checked_{period} = 1
                      AND COALESCE(ambiguous_{period}, 0) = 0
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
                    bias[abbrev] = round(mean_wrong - mean_correct, 3)

        except (sqlite3.Error, OSError) as e:
            _log.warning("self_score 偏差分析失败: %s", e)

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

        try:
            with sqlite3.connect(self.store.db_path) as conn:
                conn.row_factory = sqlite3.Row

                for agent_name, dim in agent_dim_map.items():
                    rows = conn.execute(f"""
                        SELECT agent_directions, return_{period}, direction
                        FROM {PredictionStore.TABLE}
                        WHERE checked_{period} = 1
                        AND COALESCE(ambiguous_{period}, 0) = 0
                        AND agent_directions IS NOT NULL
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
                            # v0.45.9 P0：单蜂方向也走三态，模糊样本不进分母
                            _ok, _amb_row = self._check_direction(agent_dir, ret)
                            if _amb_row:
                                continue
                            checked += 1
                            if _ok:
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
        try:
            with sqlite3.connect(self.store.db_path) as conn:
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
                    _pdt_today(),  # v0.33.0: 写戳 PDT，与 predictions.date 同口径
                    json.dumps(weights),
                    json.dumps({k: round(v, 3) for k, v in accuracy.items()}),
                    samples,
                    period,
                ))
                conn.commit()
        except (sqlite3.Error, OSError, TypeError) as e:
            _log.warning("保存自适应权重失败: %s", e)

    def cleanup_old_predictions(self, days: int = 180) -> int:
        """删除超过 days 天的旧预测记录

        Returns:
            删除的记录数
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.store.db_path) as conn:
                cursor = conn.execute(
                    f"DELETE FROM {PredictionStore.TABLE} WHERE date < ?", (cutoff,)
                )
                deleted = cursor.rowcount
                conn.commit()
                if deleted:
                    _log.info("清理旧预测 %d 条（>%d 天）", deleted, days)
                return deleted
        except (sqlite3.Error, OSError) as e:
            _log.warning("cleanup_old_predictions 失败: %s", e)
            return 0

    @staticmethod
    def load_adapted_weights(db_path: str = DB_PATH) -> Optional[Dict]:
        """
        加载最近的自适应权重（供 QueenDistiller 使用）

        优先加载 T+7 权重（更可靠），其次加载 T+1 权重（早期降级）。
        返回的权重已附加 _meta 字段，QueenDistiller 会自动忽略未知 key。

        Returns:
            {signal: 0.xx, ..., _meta: {period, samples}} 或 None
        """
        try:
            with sqlite3.connect(db_path) as conn:
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


# ==================== 便捷函数 ====================

def run_full_backtest(swarm_results: Dict = None, date: Optional[str] = None) -> Dict:
    """
    执行完整回测流程

    1. 保存新预测（如有）
    2. 检查到期预测
    3. 输出报告
    4. 尝试权重自适应

    Args:
        date: 业务日期（YYYY-MM-DD）。**调用方若有报告日期应显式传入**，
              否则预测会盖成运行当天的 PDT 日期（见 save_prediction 的
              v0.42.4 说明）。留空仅适用于"就是为当天跑"的场景。

    返回: {backtest_results, accuracy_stats, adapted_weights}
    """
    bt = Backtester()

    # 1. 保存新预测
    if swarm_results:
        bt.save_predictions(swarm_results, date=date)

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
