"""断裂点集成测试 — 覆盖 #16~#18 新增/修改的工具函数"""

import json
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest


# ==================== read_json_cache ====================

class TestReadJsonCache:
    def test_miss_nonexistent(self, tmp_path):
        from hive_logger import read_json_cache
        assert read_json_cache(tmp_path / "nope.json", ttl=300) is None

    def test_hit_fresh(self, tmp_path):
        from hive_logger import read_json_cache
        p = tmp_path / "data.json"
        p.write_text(json.dumps({"a": 1}))
        assert read_json_cache(p, ttl=300) == {"a": 1}

    def test_miss_expired(self, tmp_path):
        from hive_logger import read_json_cache
        p = tmp_path / "old.json"
        p.write_text(json.dumps([1, 2]))
        os.utime(p, (0, 0))  # mtime = epoch
        assert read_json_cache(p, ttl=300) is None

    def test_miss_corrupt_json(self, tmp_path):
        from hive_logger import read_json_cache
        p = tmp_path / "bad.json"
        p.write_text("{invalid json!!!")
        assert read_json_cache(p, ttl=9999) is None

    def test_list_data(self, tmp_path):
        from hive_logger import read_json_cache
        p = tmp_path / "list.json"
        p.write_text(json.dumps(["AAPL", "NVDA"]))
        assert read_json_cache(p, ttl=300) == ["AAPL", "NVDA"]

    def test_ttl_zero_always_expired(self, tmp_path):
        from hive_logger import read_json_cache
        p = tmp_path / "zero.json"
        p.write_text(json.dumps({"x": 1}))
        assert read_json_cache(p, ttl=0) is None


# ==================== atomic_json_write ====================

class TestAtomicJsonWrite:
    def test_roundtrip(self, tmp_path):
        from hive_logger import atomic_json_write, read_json_cache
        p = tmp_path / "rt.json"
        data = {"ticker": "NVDA", "score": 8.5}
        atomic_json_write(p, data)
        assert read_json_cache(p, ttl=300) == data

    def test_writes_valid_json(self, tmp_path):
        from hive_logger import atomic_json_write
        p = tmp_path / "valid.json"
        atomic_json_write(p, {"val": 3.14, "dt": "2026-02-28"})
        parsed = json.loads(p.read_text())
        assert parsed["val"] == 3.14


# ==================== SafeJSONEncoder ====================

class TestSafeJSONEncoder:
    def test_nan(self):
        from hive_logger import SafeJSONEncoder
        result = json.loads(json.dumps({"v": float("nan")}, cls=SafeJSONEncoder))
        assert result["v"] is None

    def test_inf(self):
        from hive_logger import SafeJSONEncoder
        result = json.loads(json.dumps({"v": float("inf")}, cls=SafeJSONEncoder))
        assert result["v"] == "Inf"

    def test_neg_inf(self):
        from hive_logger import SafeJSONEncoder
        result = json.loads(json.dumps({"v": float("-inf")}, cls=SafeJSONEncoder))
        assert result["v"] == "-Inf"

    def test_datetime(self):
        from hive_logger import SafeJSONEncoder
        dt = datetime(2026, 2, 28, 12, 0, 0)
        result = json.loads(json.dumps({"t": dt}, cls=SafeJSONEncoder))
        assert result["t"] == "2026-02-28T12:00:00"

    def test_set(self):
        from hive_logger import SafeJSONEncoder
        result = json.loads(json.dumps({"s": {"b", "a"}}, cls=SafeJSONEncoder))
        assert result["s"] == ["a", "b"]  # sorted

    def test_bytes(self):
        from hive_logger import SafeJSONEncoder
        result = json.loads(json.dumps({"b": b"hello"}, cls=SafeJSONEncoder))
        assert result["b"] == "hello"

    def test_path(self):
        from hive_logger import SafeJSONEncoder
        result = json.loads(json.dumps({"p": Path("/tmp/test")}, cls=SafeJSONEncoder))
        assert result["p"] == "/tmp/test"

    def test_nested_nan(self):
        from hive_logger import SafeJSONEncoder
        data = {"a": [1.0, float("nan"), {"b": float("inf")}]}
        result = json.loads(json.dumps(data, cls=SafeJSONEncoder))
        assert result["a"][1] is None
        assert result["a"][2]["b"] == "Inf"


# ==================== FeatureRegistry ====================

class TestFeatureRegistry:
    def setup_method(self):
        from hive_logger import FeatureRegistry
        FeatureRegistry._features = {}  # 每个测试隔离

    def test_register_and_summary(self):
        from hive_logger import FeatureRegistry
        FeatureRegistry.register("mod_a", True)
        FeatureRegistry.register("mod_b", False, reason="ImportError")
        s = FeatureRegistry.summary()
        assert s["mod_a"]["available"] is True
        assert s["mod_b"]["available"] is False
        assert s["mod_b"]["reason"] == "ImportError"

    def test_log_status_returns_degraded(self):
        from hive_logger import FeatureRegistry
        FeatureRegistry.register("ok", True)
        FeatureRegistry.register("bad1", False, reason="missing")
        FeatureRegistry.register("bad2", False, reason="old version")
        degraded = FeatureRegistry.log_status()
        assert "bad1" in degraded
        assert "bad2" in degraded
        assert "ok" not in degraded

    def test_empty_registry(self):
        from hive_logger import FeatureRegistry
        assert FeatureRegistry.summary() == {}
        assert FeatureRegistry.log_status() == {}


# ==================== _rebalance_weights ====================

class TestRebalanceWeights:
    def test_empty_dict(self):
        from agent_weight_manager import AgentWeightManager
        assert AgentWeightManager._rebalance_weights({}) == {}

    def test_single_agent(self):
        from agent_weight_manager import AgentWeightManager
        result = AgentWeightManager._rebalance_weights({"A": 2.0})
        assert result == {"A": 1.0}

    def test_mean_equals_one(self):
        from agent_weight_manager import AgentWeightManager
        w = {"A": 0.8, "B": 1.2, "C": 1.5, "D": 0.5, "E": 1.0, "F": 1.0}
        result = AgentWeightManager._rebalance_weights(w)
        mean = sum(result.values()) / len(result)
        assert abs(mean - 1.0) < 0.01

    def test_preserves_relative_order(self):
        from agent_weight_manager import AgentWeightManager
        w = {"A": 3.0, "B": 1.0, "C": 2.0}
        result = AgentWeightManager._rebalance_weights(w)
        assert result["A"] > result["C"] > result["B"]

    def test_zero_total_fallback(self):
        from agent_weight_manager import AgentWeightManager
        result = AgentWeightManager._rebalance_weights({"A": 0.0, "B": 0.0})
        assert result == {"A": 1.0, "B": 1.0}


# ==================== validate_watchlist ====================

class TestValidateWatchlist:
    def test_returns_list(self):
        from config import validate_watchlist
        result = validate_watchlist()
        assert isinstance(result, list)

    def test_current_watchlist_valid(self):
        from config import validate_watchlist
        warnings = validate_watchlist()
        # 现有 WATCHLIST 应该无严重警告（格式/字段问题）
        format_errors = [w for w in warnings if "格式异常" in w or "缺少必填字段" in w]
        assert format_errors == [], f"WATCHLIST 有格式问题: {format_errors}"


# ==================== MetricsCollector thread_count ====================

class TestMetricsCollectorThreads:
    def test_get_thread_count(self):
        from metrics_collector import MetricsCollector
        count = MetricsCollector.get_thread_count()
        assert isinstance(count, int)
        assert count >= 1  # 至少主线程

    def test_record_scan_captures_threads(self, tmp_path):
        import sqlite3
        from metrics_collector import MetricsCollector
        db = str(tmp_path / "test_metrics.db")
        mc = MetricsCollector(db_path=db)
        mc.record_scan(ticker_count=3, duration_seconds=2.0, agent_count=6, agent_total=6)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT thread_count FROM scan_metrics ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        assert row["thread_count"] >= 1

    def test_summary_includes_thread_stats(self, tmp_path):
        from metrics_collector import MetricsCollector
        db = str(tmp_path / "test_metrics.db")
        mc = MetricsCollector(db_path=db)
        mc.record_scan(ticker_count=2, duration_seconds=1.5, agent_count=6, agent_total=6)
        summary = mc.get_summary(days=1)
        assert "max_thread_count" in summary
        assert "avg_thread_count" in summary
        assert summary["max_thread_count"] >= 1


# ==================== Slack webhook env var (#20) ====================

class TestSlackWebhookEnvVar:
    def test_env_var_takes_priority(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/from-env")
        from slack_notifier import SlackNotifier
        n = SlackNotifier()
        assert n.webhook_url == "https://hooks.slack.com/from-env"

    def test_report_notifier_env_var(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/report-env")
        from slack_report_notifier import SlackReportNotifier
        n = SlackReportNotifier()
        assert n.webhook_url == "https://hooks.slack.com/report-env"

    def test_empty_env_falls_through(self, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
        from slack_notifier import SlackNotifier
        n = SlackNotifier()
        # Should fall through to file (which may or may not exist)
        assert n.webhook_url != ""  or n.webhook_url is None


# ==================== ConfigLoader hot-reload (#22) ====================

class TestConfigLoader:
    def test_reload_no_file_returns_builtin(self):
        from config import ConfigLoader
        result = ConfigLoader.reload()
        assert result["source"] == "builtin"
        assert result["watchlist_count"] > 0

    def test_reload_json_override(self, tmp_path, monkeypatch):
        import config as _cfg
        # Save originals to restore later
        orig_wl = dict(_cfg.WATCHLIST)
        orig_cat = dict(_cfg.CATALYSTS)
        try:
            override = tmp_path / "watchlist_override.json"
            override.write_text(json.dumps({
                "watchlist": {
                    "TEST": {"name": "Test Corp", "sector": "Test", "monitor_events": ["earnings"]}
                },
                "catalysts": {
                    "TEST": [{"event": "Q1 Earnings", "scheduled_date": "2026-06-01",
                              "scheduled_time": "16:00", "time_zone": "US/Eastern"}]
                }
            }))
            monkeypatch.setattr(_cfg.ConfigLoader, "_OVERRIDE_JSON", str(override))
            monkeypatch.setattr(_cfg.ConfigLoader, "_OVERRIDE_YAML", str(tmp_path / "nope.yaml"))

            result = _cfg.ConfigLoader.reload()
            assert result["source"] == "watchlist_override.json"
            assert _cfg.WATCHLIST == {"TEST": {"name": "Test Corp", "sector": "Test",
                                               "monitor_events": ["earnings"]}}
            assert "TEST" in _cfg.CATALYSTS
        finally:
            # Restore originals
            _cfg.WATCHLIST.clear()
            _cfg.WATCHLIST.update(orig_wl)
            _cfg.CATALYSTS.clear()
            _cfg.CATALYSTS.update(orig_cat)

    def test_reload_if_changed_skips_when_unchanged(self, tmp_path, monkeypatch):
        import config as _cfg
        override = tmp_path / "watchlist_override.json"
        override.write_text(json.dumps({"watchlist": {}}))
        monkeypatch.setattr(_cfg.ConfigLoader, "_OVERRIDE_JSON", str(override))
        monkeypatch.setattr(_cfg.ConfigLoader, "_OVERRIDE_YAML", str(tmp_path / "nope.yaml"))

        # First call sets mtime
        _cfg.ConfigLoader._last_mtime = os.path.getmtime(str(override))
        # Second call should detect no change
        assert _cfg.ConfigLoader.reload_if_changed() is False

    def test_reload_if_changed_detects_update(self, tmp_path, monkeypatch):
        import config as _cfg
        orig_wl = dict(_cfg.WATCHLIST)
        try:
            override = tmp_path / "watchlist_override.json"
            override.write_text(json.dumps({"watchlist": {"ZZZ": {
                "name": "Zzz", "sector": "Test", "monitor_events": ["x"]}}}))
            monkeypatch.setattr(_cfg.ConfigLoader, "_OVERRIDE_JSON", str(override))
            monkeypatch.setattr(_cfg.ConfigLoader, "_OVERRIDE_YAML", str(tmp_path / "nope.yaml"))
            _cfg.ConfigLoader._last_mtime = 0.0  # Force stale

            assert _cfg.ConfigLoader.reload_if_changed() is True
            assert "ZZZ" in _cfg.WATCHLIST
        finally:
            _cfg.WATCHLIST.clear()
            _cfg.WATCHLIST.update(orig_wl)

    def test_reload_convenience_function(self):
        from config import reload_config
        result = reload_config()
        assert "watchlist_count" in result
        assert "source" in result
