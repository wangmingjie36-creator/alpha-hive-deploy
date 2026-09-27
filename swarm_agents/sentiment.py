"""情绪基线 SQLite 存储 + 情绪动量 + 情绪-价格背离检测"""

from typing import Dict, Optional
from swarm_agents._config import _log


# ── 情绪基线 SQLite 存储（#13）──
def _sentiment_db_path():
    from pathlib import Path
    from hive_logger import PATHS
    return Path(PATHS.home) / "sentiment_baseline.db"


def _init_sentiment_db():
    """初始化情绪基线 DB（幂等）"""
    import sqlite3 as _sq
    db = _sentiment_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = _sq.connect(str(db))
    conn.execute("""CREATE TABLE IF NOT EXISTS sentiment_baseline (
        ticker TEXT NOT NULL,
        date   TEXT NOT NULL,
        sentiment_pct INTEGER NOT NULL,
        PRIMARY KEY (ticker, date)
    )""")
    conn.commit()
    conn.close()


def _upsert_sentiment(ticker: str, date_str: str, pct: int):
    """写入或更新当日情绪值"""
    import sqlite3 as _sq
    try:
        _init_sentiment_db()
        with _sq.connect(str(_sentiment_db_path())) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sentiment_baseline (ticker, date, sentiment_pct) VALUES (?,?,?)",
                (ticker, date_str, pct),
            )
            conn.execute(
                "DELETE FROM sentiment_baseline WHERE date < date('now', '-60 days')"
            )
    except (OSError, ValueError, TypeError) as _e:
        _log.debug("sentiment_baseline upsert error: %s", _e)


def _get_sentiment_baseline(ticker: str, days: int = 30) -> Optional[float]:
    """获取过去 N 天的平均情绪值（排除今日），无数据返回 None"""
    import sqlite3 as _sq
    try:
        with _sq.connect(str(_sentiment_db_path())) as conn:
            row = conn.execute(
                f"SELECT AVG(sentiment_pct) FROM sentiment_baseline "
                f"WHERE ticker=? AND date < date('now') AND date >= date('now', '-{days} days')",
                (ticker,),
            ).fetchone()
            if row and row[0] is not None:
                return float(row[0])
    except (OSError, ValueError, TypeError) as _e:
        _log.debug("sentiment_baseline query error: %s", _e)
    return None


_SENTIMENT_SPIKE_THRESHOLD = 20   # 偏差超过 20 个百分点触发告警
_SENTIMENT_MIN_DAYS = 5            # 至少 5 天基线才触发告警

# ── 情绪动量配置 ──
try:
    from config import SENTIMENT_MOMENTUM_CONFIG as _SM_CFG
except ImportError:
    _SM_CFG = {"surge_threshold": 15, "rise_threshold": 5, "crash_threshold": -15,
               "decline_threshold": -5, "divergence_bull_trap_sentiment": 65,
               "divergence_hidden_opp_sentiment": 35, "divergence_price_threshold": 3.0}


#: 回看参照允许的最大间隔 = N + 本常量（日历日）。3 日回看最多接受 7 天前的参照：周末（≤5 天）、
#: 漏扫一天（6~7 天）都在内；扫描断档期拿一两周前的值来比「3 日动量」则不算（按无历史处理）。
#: v0.45.340 起是 buzz_v1 定义的一部分（维度 IC 协议 §13.2）——改它 = 改冻结层。
_MOMENTUM_REF_SLACK_DAYS = 4

#: 历史来源：`signal_archive` 表里的 `sentiment.pct`（每次扫描收尾按业务日期写入）。
#: 与 `signal_archive.TABLE` / 其抽取器同名——tests 钉住；这里不 import signal_archive（它反向依赖 swarm_agents）
_ARCHIVE_TABLE = "signal_archive"
_ARCHIVE_PCT_SIGNAL = "sentiment.pct"


def _archive_db_path():
    """`pheromone.db`，调用时求值（数据根迁移后跟着 `PATHS` 走）。"""
    from pathlib import Path
    from hive_logger import PATHS
    return Path(PATHS.db)


def _get_sentiment_momentum(ticker: str, current_pct: int, as_of: Optional[str] = None,
                            db_path=None) -> Dict:
    """
    计算情绪动量：当前情绪 − 归档里 N 天前（N=1/3/7）的情绪。
    返回 1d/3d/7d 情绪变化和动量体制分类。

    momentum_regime: surging / rising / stable / declining / crashing / unknown
    momentum_score_adj: -0.5 ~ +0.5 的评分调整（只看 delta_3d）

    v0.45.340（buzz_v1 阶段 1，维度 IC 协议 §13.2 的锚点）——此前两处让它不可复现：
      ① 回看用 SQLite `date('now')`（墙上时钟），补跑 / 重跑同一业务日会取到另一段历史；
      ② `date('now')` 是 **UTC**，而写基线的 `_upsert_sentiment` 用本地日期——两个时间基准混用；
      ③ 历史来自 `sentiment_baseline.db`，任何一次 Buzz 运行（MCP、深度报告、重放）都会往里写，不只正式扫描。
    现在：
      · 时间基准 = `as_of`（扫描业务日期，BuzzBee 传 `self._target_date` = 报告的 `date_str`）；
        缺省（非扫描调用）才退回本地当天，并记 `as_of_source="wall_clock"`。
      · 历史 = `signal_archive` 的 `sentiment.pct`，取「日期 ≤ as_of − N」的最近一行，且不早于
        `as_of − N − _MOMENTUM_REF_SLACK_DAYS`（否则该回看按无历史处理）。
      · 读不到归档（库 / 表不存在、查询出错）⇒ warning + `history_source="unavailable"`，不再静默。
        「表在、但这只票没有够近的历史」是正常的无历史（`history_source="signal_archive"`、delta=None）。
    """
    import datetime as _dt
    import sqlite3 as _sq

    as_of_source = "scan" if as_of else "wall_clock"
    day0 = _dt.date.fromisoformat(as_of) if as_of else _dt.date.today()   # 格式错直接抛：那是调用方的 bug
    result: Dict = {"delta_1d": None, "delta_3d": None, "delta_7d": None,
                    "momentum_regime": "unknown", "momentum_score_adj": 0.0,
                    "as_of": day0.isoformat(), "as_of_source": as_of_source,
                    "history_source": "signal_archive", "ref_dates": {}}
    path = db_path if db_path is not None else _archive_db_path()
    try:
        # mode=ro：库不存在时报错而不是凭空建一个空库（那会把「路径错了」伪装成「没有历史」）
        conn = _sq.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for days, key in ((1, "delta_1d"), (3, "delta_3d"), (7, "delta_7d")):
                cutoff = (day0 - _dt.timedelta(days=days)).isoformat()
                oldest = (day0 - _dt.timedelta(days=days + _MOMENTUM_REF_SLACK_DAYS)).isoformat()
                row = conn.execute(
                    f"SELECT date, value FROM {_ARCHIVE_TABLE} "
                    "WHERE signal=? AND ticker=? AND date <= ? AND value IS NOT NULL "
                    "ORDER BY date DESC LIMIT 1",
                    (_ARCHIVE_PCT_SIGNAL, ticker, cutoff),
                ).fetchone()
                if row and row[0] >= oldest:
                    result[key] = current_pct - int(round(row[1]))
                    result["ref_dates"][key] = row[0]
        finally:
            conn.close()
    except _sq.Error as _e:
        _log.warning("情绪动量：读归档 %s 失败（%s）——%s 本次按无历史处理，动量调整为 0", path, _e, ticker)
        result["history_source"] = "unavailable"
        return result

    # 基于 3d delta 判断动量体制
    d3 = result["delta_3d"]
    if d3 is not None:
        surge = _SM_CFG.get("surge_threshold", 15)
        rise = _SM_CFG.get("rise_threshold", 5)
        crash = _SM_CFG.get("crash_threshold", -15)
        decline = _SM_CFG.get("decline_threshold", -5)
        if d3 > surge:
            result["momentum_regime"] = "surging"
            result["momentum_score_adj"] = +0.5
        elif d3 > rise:
            result["momentum_regime"] = "rising"
            result["momentum_score_adj"] = +0.2
        elif d3 < crash:
            result["momentum_regime"] = "crashing"
            result["momentum_score_adj"] = -0.5
        elif d3 < decline:
            result["momentum_regime"] = "declining"
            result["momentum_score_adj"] = -0.2
        else:
            result["momentum_regime"] = "stable"
    return result


def _detect_sentiment_price_divergence(
    sentiment_pct: int, momentum_5d: float, ticker: str
) -> Dict:
    """
    检测情绪与价格走势的背离信号。

    看多陷阱（bull_trap）：情绪高但价格跌 → 市场过度乐观
    隐藏机会（hidden_opportunity）：情绪低但价格涨 → 市场低估
    """
    result: Dict = {"divergence_type": "none", "severity": 0, "score_adj": 0.0, "description": ""}

    # v0.43.25: momentum_5d 现在可能是 None（诚实缺数据）。此前 ScoutBee 用
    # `or 0.0` 伪造"持平"，0.0 永远够不到下面的阈值——实测近 28 个扫描日
    # 395 次检测**全部** severity=0，整个背离检测是死的。
    # 改诚实后必须在这里接住 None，否则 `momentum_5d < -price_thresh` 直接抛
    # TypeError（与 v0.43.23 的 ML 报告崩溃同一模式）。
    # 用 "unavailable" 而非 "none"：前者是"查不了"，后者是"查过、没背离"，
    # 两者对下游的含义完全不同。
    if momentum_5d is None:
        result["divergence_type"] = "unavailable"
        return result

    bull_trap_sent = _SM_CFG.get("divergence_bull_trap_sentiment", 65)
    hidden_opp_sent = _SM_CFG.get("divergence_hidden_opp_sentiment", 35)
    price_thresh = _SM_CFG.get("divergence_price_threshold", 3.0)

    if sentiment_pct > bull_trap_sent and momentum_5d < -price_thresh:
        severity = min(3, int((sentiment_pct - bull_trap_sent) / 10) + int(abs(momentum_5d) / price_thresh))
        result.update({
            "divergence_type": "bull_trap",
            "severity": severity,
            "score_adj": round(-0.3 * severity, 2),
            "description": f"⚠️ 看多陷阱：情绪{sentiment_pct}%看多但5日跌{momentum_5d:.1f}%"
        })
    elif sentiment_pct < hidden_opp_sent and momentum_5d > price_thresh:
        severity = min(3, int((hidden_opp_sent - sentiment_pct) / 10) + int(momentum_5d / price_thresh))
        result.update({
            "divergence_type": "hidden_opportunity",
            "severity": severity,
            "score_adj": round(+0.3 * severity, 2),
            "description": f"💡 隐藏机会：情绪仅{sentiment_pct}%看多但5日涨{momentum_5d:.1f}%"
        })
    return result


def _check_sentiment_spike(ticker: str, current_pct: int, today: str) -> Optional[str]:
    """
    对比当日情绪与 30 天基线，偏差 >THRESHOLD 时写一条 WARNING 日志。
    返回告警描述字符串（无告警时返回 None）。

    v0.45.339：**只写日志，不发 Slack。** 此前这里在 `n.enabled` 时调
    `send_risk_alert` —— 而 `enabled = bool(user_token) or webhook_alive`，
    本机有 Bot Token ⇒ 恒真；Bot 不在 #alpha-hive ⇒ `not_in_channel` 降级成
    **私信用户**（生产日志实测 6 条：CRM / DELL×2 / VKTX / RKLB / MU）。
    CLAUDE.md「Slack 通知精简规则」明令禁止逐标的预警；守卫
    `tests/test_slack_send_whitelist.py`。
    """
    baseline = _get_sentiment_baseline(ticker, days=30)
    if baseline is None:
        return None
    delta = current_pct - baseline
    if abs(delta) < _SENTIMENT_SPIKE_THRESHOLD:
        return None

    direction_str = "看多骤升" if delta > 0 else "看空骤降"
    msg = (
        f"{ticker} 情绪突变 [{direction_str}]：当日 {current_pct}%，"
        f"30日均值 {baseline:.1f}%，偏差 {delta:+.1f}ppt"
    )
    _log.warning("📡 情绪突变告警 %s", msg)
    return msg
