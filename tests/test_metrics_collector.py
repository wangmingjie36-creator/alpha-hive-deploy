"""MetricsCollector 测试 - 指标收集 + SLO 检查"""

import pytest
import time


@pytest.fixture
def mc(tmp_path):
    from metrics_collector import MetricsCollector
    return MetricsCollector(db_path=str(tmp_path / "test_metrics.db"))


class TestRecordScan:
    def test_record_and_summary(self, mc):
        mc.record_scan(ticker_count=5, duration_seconds=3.2, agent_count=6)
        s = mc.get_summary(days=1)
        assert s["total_scans"] == 1
        assert s["avg_duration"] == 3.2

    def test_record_multiple_scans(self, mc):
        mc.record_scan(ticker_count=3, duration_seconds=2.0)
        mc.record_scan(ticker_count=5, duration_seconds=4.0)
        s = mc.get_summary(days=1)
        assert s["total_scans"] == 2
        assert s["avg_duration"] == 3.0

    def test_record_with_all_fields(self, mc):
        mc.record_scan(
            ticker_count=5, duration_seconds=3.0, agent_count=6,
            prefetch_seconds=0.5, avg_score=7.5, max_score=9.0, min_score=4.0,
            agent_errors=2, agent_total=30, data_real_pct=80.0,
            resonance_count=3, llm_calls=10, llm_cost_usd=0.05,
            session_id="test_123", scan_mode="swarm",
        )
        s = mc.get_summary(days=1)
        assert s["total_scans"] == 1
        assert s["avg_score"] == 7.5
        assert s["total_llm_calls"] == 10

    def test_empty_summary(self, mc):
        s = mc.get_summary(days=1)
        assert s["total_scans"] == 0
        assert s["avg_duration"] == 0.0


class TestRecordTicker:
    def test_record_ticker(self, mc):
        mc.record_ticker(
            ticker="NVDA", final_score=8.5, direction="bullish",
            supporting_agents=5, data_real_pct=90.0, resonance_detected=True,
        )
        history = mc.get_ticker_history("NVDA", days=1)
        assert len(history) == 1
        assert history[0]["final_score"] == 8.5
        assert history[0]["direction"] == "bullish"

    def test_ticker_history_multiple(self, mc):
        for i in range(3):
            mc.record_ticker(ticker="TSLA", final_score=5.0 + i)
        history = mc.get_ticker_history("TSLA", days=1)
        assert len(history) == 3

    def test_ticker_history_empty(self, mc):
        history = mc.get_ticker_history("UNKNOWN", days=1)
        assert history == []


class TestSLOCheck:
    def test_no_violations_when_healthy(self, mc):
        mc.record_scan(
            ticker_count=5, duration_seconds=3.0,
            agent_errors=0, agent_total=30, data_real_pct=80.0,
        )
        violations = mc.check_slo(days=1)
        assert len(violations) == 0

    def test_latency_violation(self, mc):
        mc.record_scan(ticker_count=5, duration_seconds=60.0)
        violations = mc.check_slo(days=1)
        names = [v["slo_name"] for v in violations]
        assert "scan_latency_p95" in names

    def test_error_rate_violation(self, mc):
        mc.record_scan(
            ticker_count=5, duration_seconds=3.0,
            agent_errors=10, agent_total=30,
        )
        violations = mc.check_slo(days=1)
        names = [v["slo_name"] for v in violations]
        assert "agent_error_rate" in names

    def test_data_quality_violation(self, mc):
        mc.record_scan(
            ticker_count=5, duration_seconds=3.0,
            data_real_pct=20.0,
        )
        violations = mc.check_slo(days=1)
        names = [v["slo_name"] for v in violations]
        assert "data_real_pct" in names

    def test_no_data_returns_empty(self, mc):
        violations = mc.check_slo(days=1)
        assert violations == []

    def test_violations_persisted(self, mc):
        mc.record_scan(ticker_count=1, duration_seconds=60.0)
        mc.check_slo(days=1)
        # Second check should still find persisted violations
        s = mc.get_summary(days=1)
        assert s["slo_violations"] > 0


class TestCleanup:
    def test_cleanup_removes_old(self, mc):
        mc.record_scan(ticker_count=1, duration_seconds=1.0)
        # cleanup with 0 days retention = remove everything
        mc.cleanup(retention_days=0)
        s = mc.get_summary(days=365)
        assert s["total_scans"] == 0


class TestCorrelationId:
    def test_set_and_get(self):
        from hive_logger import set_correlation_id, get_correlation_id
        set_correlation_id("test_abc123")
        assert get_correlation_id() == "test_abc123"

    def test_default_value(self):
        import threading
        from hive_logger import get_correlation_id

        result = []
        def worker():
            result.append(get_correlation_id())

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert result[0] == "no_corr"

    def test_auto_generate(self):
        from hive_logger import set_correlation_id, get_correlation_id
        set_correlation_id()
        cid = get_correlation_id()
        assert len(cid) == 12
        assert cid != "no_corr"


class TestJSONFormatter:
    def test_produces_valid_json(self, tmp_path):
        import json
        import logging
        from hive_logger import JSONFormatter

        handler = logging.FileHandler(str(tmp_path / "test.jsonl"))
        handler.setFormatter(JSONFormatter())
        test_logger = logging.getLogger("test_json_fmt")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.DEBUG)

        test_logger.info("test message %d", 42)
        handler.flush()

        with open(tmp_path / "test.jsonl") as f:
            line = f.readline().strip()

        entry = json.loads(line)
        assert entry["msg"] == "test message 42"
        assert entry["level"] == "INFO"
        assert "ts" in entry
        assert "corr_id" in entry

        test_logger.removeHandler(handler)
        handler.close()
