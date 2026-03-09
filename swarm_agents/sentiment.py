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


def _get_sentiment_momentum(ticker: str, current_pct: int) -> Dict:
    """
    计算情绪动量（基于 sentiment_baseline 历史数据）。
    返回 1d/3d/7d 情绪变化率和动量体制分类。

    momentum_regime: surging / rising / stable / declining / crashing / unknown
    momentum_score_adj: -0.5 ~ +0.5 的评分调整
    """
    import sqlite3 as _sq
    result: Dict = {"delta_1d": None, "delta_3d": None, "delta_7d": None,
                    "momentum_regime": "unknown", "momentum_score_adj": 0.0}
    try:
        with _sq.connect(str(_sentiment_db_path())) as conn:
            for days, key in [(1, "delta_1d"), (3, "delta_3d"), (7, "delta_7d")]:
                row = conn.execute(
                    "SELECT sentiment_pct FROM sentiment_baseline "
                    "WHERE ticker=? AND date <= date('now', ? || ' days') "
                    "ORDER BY date DESC LIMIT 1",
                    (ticker, f"-{days}"),
                ).fetchone()
                if row:
                    result[key] = current_pct - row[0]
    except Exception as _e:
        _log.debug("sentiment_momentum 查询失败: %s", _e)
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
    对比当日情绪与 30 天基线，偏差 >THRESHOLD 时触发 Slack 告警。
    返回告警描述字符串（无告警时返回 None）。
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
    try:
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        if getattr(n, "enabled", False):
            n.send_risk_alert(
                alert_title=f"{ticker} 情绪突变告警",
                alert_message=msg,
                severity="HIGH" if abs(delta) >= 30 else "MEDIUM",
            )
    except Exception as _se:
        _log.debug("Slack 情绪突变告警发送失败: %s", _se)
    return msg
