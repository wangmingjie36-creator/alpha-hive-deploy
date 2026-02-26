"""resilience 模块测试 - RateLimiter + CircuitBreaker + retry"""

import time
import threading
import pytest


class TestRateLimiter:
    def test_acquire_within_rate(self):
        from resilience import RateLimiter
        rl = RateLimiter(rate=100.0, burst=5)
        # 5 个 burst 应立即获取
        for _ in range(5):
            assert rl.acquire(timeout=1.0)

    def test_acquire_blocks_when_empty(self):
        from resilience import RateLimiter
        rl = RateLimiter(rate=10.0, burst=1)
        assert rl.acquire(timeout=1.0)  # 消耗唯一 token
        start = time.monotonic()
        assert rl.acquire(timeout=2.0)  # 等待补充
        elapsed = time.monotonic() - start
        assert elapsed >= 0.05  # 至少等了一些时间

    def test_acquire_timeout(self):
        from resilience import RateLimiter
        rl = RateLimiter(rate=0.5, burst=1)
        rl.acquire(timeout=1.0)  # 消耗
        assert not rl.acquire(timeout=0.1)  # 超时

    def test_thread_safe(self):
        from resilience import RateLimiter
        rl = RateLimiter(rate=50.0, burst=10)
        results = []

        def worker():
            results.append(rl.acquire(timeout=5.0))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(results)


class TestCircuitBreaker:
    def test_starts_closed(self):
        from resilience import CircuitBreaker
        cb = CircuitBreaker("test", failure_threshold=3)
        assert cb.state == "closed"
        assert cb.allow_request()

    def test_opens_after_threshold(self):
        from resilience import CircuitBreaker
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60.0)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"
        assert not cb.allow_request()

    def test_stays_closed_below_threshold(self):
        from resilience import CircuitBreaker
        cb = CircuitBreaker("test", failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == "closed"
        assert cb.allow_request()

    def test_success_resets_count(self):
        from resilience import CircuitBreaker
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        # 只有连续 2 次，未达阈值
        assert cb.state == "closed"

    def test_half_open_after_timeout(self):
        from resilience import CircuitBreaker
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.15)
        assert cb.state == "half_open"
        assert cb.allow_request()

    def test_half_open_success_closes(self):
        from resilience import CircuitBreaker
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == "half_open"
        cb.record_success()
        assert cb.state == "closed"

    def test_reset(self):
        from resilience import CircuitBreaker
        cb = CircuitBreaker("test", failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        cb.reset()
        assert cb.state == "closed"


class TestRetryDecorator:
    def test_succeeds_first_try(self):
        from resilience import retry

        call_count = 0

        @retry(max_retries=3)
        def always_works():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert always_works() == "ok"
        assert call_count == 1

    def test_retries_on_failure(self):
        from resilience import retry

        attempt = 0

        @retry(max_retries=2, backoff_base=0.01)
        def fails_twice():
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                raise ValueError("not yet")
            return "ok"

        assert fails_twice() == "ok"
        assert attempt == 3

    def test_returns_none_after_max_retries(self):
        from resilience import retry

        @retry(max_retries=2, backoff_base=0.01)
        def always_fails():
            raise ConnectionError("down")

        assert always_fails() is None

    def test_respects_circuit_breaker(self):
        from resilience import retry, CircuitBreaker

        cb = CircuitBreaker("test_retry", failure_threshold=1)
        cb.record_failure()  # 打开熔断

        @retry(max_retries=3, circuit_breaker=cb)
        def protected():
            return "ok"

        assert protected() is None  # 被熔断阻止

    def test_respects_rate_limiter(self):
        from resilience import retry, RateLimiter

        rl = RateLimiter(rate=100.0, burst=5)

        @retry(max_retries=0, rate_limiter=rl)
        def limited():
            return "ok"

        assert limited() == "ok"


class TestPresetInstances:
    def test_sec_instances_exist(self):
        from resilience import sec_limiter, sec_breaker
        assert sec_limiter is not None
        assert sec_breaker is not None
        assert sec_breaker.state == "closed"

    def test_polymarket_instances_exist(self):
        from resilience import polymarket_limiter, polymarket_breaker
        assert polymarket_limiter is not None
        assert polymarket_breaker.state == "closed"

    def test_yfinance_instances_exist(self):
        from resilience import yfinance_limiter, yfinance_breaker
        assert yfinance_limiter is not None
        assert yfinance_breaker.state == "closed"
