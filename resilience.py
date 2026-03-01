"""
Alpha Hive - 弹性层：RateLimiter + CircuitBreaker + retry

统一所有外部 API 调用的限流、熔断和重试逻辑。
"""

import time
import threading
import functools
from typing import Optional, Callable, Any
from hive_logger import get_logger

_log = get_logger("resilience")


# ==================== Token Bucket RateLimiter ====================

class RateLimiter:
    """
    Token Bucket 限流器

    用法：
        limiter = RateLimiter(rate=10, burst=10)  # 10 req/s, burst 10
        limiter.acquire()  # 阻塞直到有 token
    """

    def __init__(self, rate: float, burst: int = 1):
        """
        Args:
            rate:  每秒补充的 token 数量
            burst: 桶容量（允许瞬间并发数）
        """
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """
        获取一个 token，阻塞直到可用或超时。

        Returns:
            True 成功获取, False 超时
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            # 计算等待时间
            wait = 1.0 / self._rate
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(wait)

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now


# ==================== Circuit Breaker ====================

class CircuitBreaker:
    """
    熔断器 - 连续失败 N 次后自动熔断，冷却后半开探测

    状态转移：CLOSED -> OPEN -> HALF_OPEN -> CLOSED
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        """
        Args:
            name: 熔断器名称（用于日志）
            failure_threshold: 连续失败多少次后熔断
            recovery_timeout: 熔断后等待多少秒尝试半开
        """
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                    self._state = self.HALF_OPEN
                    _log.info("CircuitBreaker[%s] OPEN -> HALF_OPEN", self.name)
            return self._state

    def allow_request(self) -> bool:
        """当前是否允许请求通过"""
        s = self.state
        return s in (self.CLOSED, self.HALF_OPEN)

    def record_success(self):
        """记录成功调用"""
        with self._lock:
            if self._state == self.HALF_OPEN:
                _log.info("CircuitBreaker[%s] HALF_OPEN -> CLOSED", self.name)
            self._state = self.CLOSED
            self._failure_count = 0

    def record_failure(self):
        """记录失败调用（连续 N 次失败触发告警）"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self._failure_threshold:
                if self._state != self.OPEN:
                    _log.warning(
                        "CircuitBreaker[%s] -> OPEN (failures=%d)",
                        self.name, self._failure_count,
                    )
                    # #18: 连续失败告警 → 尝试 Slack 通知
                    try:
                        from slack_report_notifier import SlackReportNotifier
                        _sn = SlackReportNotifier()
                        _sn.send_risk_alert(
                            f"数据源 {self.name} 连续失败",
                            f"CircuitBreaker 熔断：连续 {self._failure_count} 次失败",
                            severity="HIGH",
                        )
                    except (ImportError, OSError, ValueError):
                        pass
                self._state = self.OPEN

    def reset(self):
        """手动重置"""
        with self._lock:
            self._state = self.CLOSED
            self._failure_count = 0


# ==================== retry 装饰器 ====================

def retry(
    max_retries: int = 3,
    backoff_base: float = 1.0,
    backoff_max: float = 30.0,
    exceptions: tuple = (Exception,),
    circuit_breaker: Optional[CircuitBreaker] = None,
    rate_limiter: Optional[RateLimiter] = None,
):
    """
    带指数退避的重试装饰器，可选集成 CircuitBreaker + RateLimiter

    用法：
        @retry(max_retries=3, circuit_breaker=sec_cb, rate_limiter=sec_rl)
        def fetch_sec_data(ticker):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # 检查熔断器
            if circuit_breaker and not circuit_breaker.allow_request():
                _log.warning(
                    "retry: %s 被熔断器 [%s] 阻止",
                    func.__name__, circuit_breaker.name,
                )
                return None

            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    # 限流
                    if rate_limiter:
                        if not rate_limiter.acquire():
                            _log.warning("retry: %s 限流超时", func.__name__)
                            return None

                    result = func(*args, **kwargs)

                    # 成功
                    if circuit_breaker:
                        circuit_breaker.record_success()
                    return result

                except exceptions as e:
                    last_exc = e
                    if attempt < max_retries:
                        delay = min(backoff_base * (2 ** attempt), backoff_max)
                        _log.warning(
                            "retry: %s attempt %d/%d failed (%s), backoff %.1fs",
                            func.__name__, attempt + 1, max_retries, e, delay,
                        )
                        time.sleep(delay)
                    else:
                        _log.error(
                            "retry: %s 最终失败 (%d 次尝试): %s",
                            func.__name__, max_retries + 1, e,
                        )

            # 全部失败
            if circuit_breaker:
                circuit_breaker.record_failure()
            return None

        return wrapper
    return decorator


# ==================== 预置实例（各数据源共享） ====================

# SEC EDGAR: 10 req/s（留 30% 余量防 429）
sec_limiter = RateLimiter(rate=6.0, burst=2)
sec_breaker = CircuitBreaker("sec_edgar", failure_threshold=6, recovery_timeout=120.0)

# Polymarket: 保守 2 req/s
polymarket_limiter = RateLimiter(rate=2.0, burst=2)
polymarket_breaker = CircuitBreaker("polymarket", failure_threshold=5, recovery_timeout=60.0)

# yfinance: ~3 req/s
yfinance_limiter = RateLimiter(rate=3.0, burst=2)
yfinance_breaker = CircuitBreaker("yfinance", failure_threshold=5, recovery_timeout=90.0)

# Reddit (ApeWisdom): 保守限流
reddit_limiter = RateLimiter(rate=1.0, burst=1)
reddit_breaker = CircuitBreaker("reddit", failure_threshold=5, recovery_timeout=60.0)

# Finviz: 限流严格
finviz_limiter = RateLimiter(rate=0.5, burst=1)
finviz_breaker = CircuitBreaker("finviz", failure_threshold=3, recovery_timeout=120.0)

# Slack webhook: 低频发送
slack_breaker = CircuitBreaker("slack", failure_threshold=3, recovery_timeout=300.0)
