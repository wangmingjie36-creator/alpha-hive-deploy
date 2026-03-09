"""
Alpha Hive GUI - 实时监控引擎
LiveMonitor
"""

import random
import logging as _logging
from datetime import datetime
from threading import Thread

_log = _logging.getLogger("alpha_hive.app")


# ==================== 实时监控引擎 ====================

class LiveMonitor:
    """
    真实数据实时监控 - 后台线程定期拉取 yfinance 数据
    检测价格异动、成交量异动、波动率变化、催化剂倒计时
    """

    # 监控前 5 个 WATCHLIST 标的
    MONITOR_TICKERS = ["NVDA", "TSLA", "MSFT", "AMD", "QCOM"]

    # 阈值
    PRICE_ALERT_PCT = 1.5      # 价格变动 >1.5% 触发警报
    VOLUME_ALERT_RATIO = 1.5   # 量比 >1.5 触发警报
    REFRESH_INTERVAL = 30      # 基础刷新间隔（秒）

    def __init__(self):
        self.running = False
        self._cache = {}        # {ticker: {price, prev_price, volume_ratio, ...}}
        self._callbacks = []    # [(agent_id, msg, msg_type, bee_action)]
        self._lock = __import__("threading").Lock()
        self._last_catalyst_check = 0

    def start(self):
        self.running = True
        Thread(target=self._monitor_loop, daemon=True).start()

    def stop(self):
        self.running = False

    def pop_events(self):
        """主线程调用：取出所有待处理事件"""
        with self._lock:
            events = list(self._callbacks)
            self._callbacks.clear()
        return events

    def _emit(self, agent_id, msg, msg_type="discovery", bee_action=None):
        """推送事件到队列"""
        with self._lock:
            self._callbacks.append((agent_id, msg, msg_type, bee_action))

    def _monitor_loop(self):
        """后台循环"""
        import time as _time
        _time.sleep(3)  # 启动延迟

        cycle = 0
        while self.running:
            try:
                cycle += 1

                # 每次随机选 1-2 个标的拉取（避免并发请求过多）
                tickers = random.sample(self.MONITOR_TICKERS, min(2, len(self.MONITOR_TICKERS)))

                for ticker in tickers:
                    self._check_ticker(ticker)

                # 每 5 分钟检查催化剂倒计时
                now = _time.time()
                if now - self._last_catalyst_check > 300:
                    self._check_catalysts()
                    self._last_catalyst_check = now

            except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
                _log.debug("DataFeed cycle %d error: %s", cycle, e)

            # 30-45 秒随机间隔（避免完全规律的请求）
            _time.sleep(self.REFRESH_INTERVAL + random.randint(0, 15))

    def _check_ticker(self, ticker):
        """检查单个标的的价格和成交量"""
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist.empty or len(hist) < 2:
                return

            current_price = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2])
            change_pct = (current_price / prev_close - 1) * 100

            # 成交量
            current_vol = float(hist["Volume"].iloc[-1])
            avg_vol = float(hist["Volume"].mean())
            vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

            # 5 日动量
            if len(hist) >= 5:
                mom_5d = (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100
            else:
                mom_5d = change_pct

            prev = self._cache.get(ticker, {})
            self._cache[ticker] = {
                "price": current_price,
                "change_pct": change_pct,
                "volume_ratio": vol_ratio,
                "momentum_5d": mom_5d,
            }

            # === 价格异动检测 ===
            if abs(change_pct) >= self.PRICE_ALERT_PCT:
                dir_word = "涨" if change_pct > 0 else "跌"
                emoji_dir = "看多" if change_pct > 0 else "看空"
                self._emit(
                    "ScoutBeeNova",
                    f"{ticker} 价格异动！{dir_word} {change_pct:+.2f}%，现价 ${current_price:.2f}",
                    "alert",
                    {"state": "working", "score": min(10, 5 + abs(change_pct)), "say": f"{ticker} {dir_word}!"}
                )

                # Oracle 跟进评论
                self._emit(
                    "OracleBeeEcho",
                    f"{ticker} {dir_word} {abs(change_pct):.1f}%，关注期权隐含波动率变化",
                    "chat"
                )

            # === 成交量异动检测 ===
            if vol_ratio >= self.VOLUME_ALERT_RATIO:
                self._emit(
                    "BuzzBeeWhisper",
                    f"{ticker} 成交量异动！量比 {vol_ratio:.1f}x（{vol_ratio:.0%} 于 5 日均量）",
                    "alert",
                    {"state": "working", "say": f"{ticker} 量!"}
                )

            # === 常规价格播报（无异动时也偶尔播报）===
            elif not prev:  # 首次加载
                self._emit(
                    "ScoutBeeNova",
                    f"{ticker} ${current_price:.2f}（{change_pct:+.2f}%）| 量比 {vol_ratio:.1f}x | 5日 {mom_5d:+.1f}%",
                    "discovery",
                    {"state": "publishing", "score": 5 + change_pct * 0.3}
                )

        except (ConnectionError, TimeoutError, OSError, ValueError, KeyError) as e:
            _log.debug("Ticker check failed for %s: %s", ticker, e)

    def _check_catalysts(self):
        """检查催化剂倒计时"""
        try:
            import yfinance as yf
            from datetime import datetime

            for ticker in self.MONITOR_TICKERS[:3]:  # 只查前 3 个
                try:
                    t = yf.Ticker(ticker)
                    cal = t.calendar
                    if cal is None:
                        continue

                    if isinstance(cal, dict):
                        cal_dict = cal
                    elif hasattr(cal, 'to_dict'):
                        cal_dict = cal.to_dict()
                    else:
                        continue

                    earnings = cal_dict.get("Earnings Date", [])
                    if isinstance(earnings, list):
                        for ed in earnings:
                            if hasattr(ed, 'strftime'):
                                date_str = ed.strftime("%Y-%m-%d")
                                days_until = (datetime.strptime(date_str, "%Y-%m-%d") - datetime.now()).days
                                if 0 <= days_until <= 14:
                                    urgency = "紧急" if days_until <= 3 else "注意"
                                    self._emit(
                                        "ChronosBeeHorizon",
                                        f"[{urgency}] {ticker} 财报还有 {days_until} 天（{date_str}）",
                                        "alert" if days_until <= 3 else "discovery",
                                        {"state": "working", "say": f"{days_until}天!"} if days_until <= 3 else None
                                    )
                                    break
                except (ConnectionError, TimeoutError, ValueError, KeyError, AttributeError) as e:
                    _log.debug("Catalyst check failed for %s: %s", ticker, e)
        except (ImportError, ConnectionError, TimeoutError, OSError) as e:
            _log.debug("Catalyst check unavailable: %s", e)


