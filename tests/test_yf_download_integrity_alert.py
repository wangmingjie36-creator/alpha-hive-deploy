"""v0.45.773：`yf.download` 完整性告警接线——串数据被丢弃 / 等串行锁超时都要红，计数缺失要进 checks_skipped。

走完整的 `AlertAnalyzer.analyze()`（同 tests/test_yf_daily.py::TestVisibility 的写法）——证明 analyze 真的调了这条检查。
变红的变异：analyze() 不调 `_check_yf_download_integrity`；级别不是 P2；计数缺失被渲染成「没问题」。
"""

import json

import alert_manager as am
import scan_timing
import yf_daily
import yf_gate


def _analyze(tmp_path, counters):
    status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
              "scan_timing": {"counters": counters, "extra": {"gh_pages": {"success": True}},
                              "production_sync": {"outcome": "up_to_date"}}}
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    a = am.AlertAnalyzer(report_dir=tmp_path)
    a.analyze(p)
    return a


def _hits(a):
    return [x for x in a.alerts if "yf.download" in x.message]


def _clean_yd():
    return yf_daily.stats()


class TestIdentityAlert:
    def test_clean_is_silent_and_not_skipped(self, tmp_path):
        a = _analyze(tmp_path, {"yf_daily": _clean_yd()})
        assert _hits(a) == []
        assert not any("身份校验" in s for s in a.checks_skipped)

    def test_mismatch_is_p2_naming_the_source_and_symbols(self, tmp_path):
        yd = _clean_yd()
        yd.update(identity_mismatch=2, identity_foreign=2,
                  identity_items=[{"source": "supply_chain", "asked": ["TSM"], "got": ["AMAT"]},
                                  {"source": "market_regime_ma", "asked": ["VZ"], "got": ["SOXX"]}],
                  by_source={"supply_chain": {"identity_mismatch": 1}, "market_regime_ma": {"identity_mismatch": 1}})
        (h,) = _hits(_analyze(tmp_path, {"yf_daily": yd}))
        assert h.level == am.AlertLevel.MEDIUM and "2 次" in h.message
        assert "supply_chain · 请求 TSM 却返回 AMAT" in h.details["明细"]
        assert "market_regime_ma×1" in h.details["按调用点"] and "supply_chain×1" in h.details["按调用点"]

    def test_missing_counter_is_skipped_not_silent(self, tmp_path):
        """旧 scan_timing（没有 identity_mismatch 键）/ 整个 yf_daily 缺失：说「没统计」，不渲染成「没问题」。"""
        old = {k: v for k, v in _clean_yd().items() if not k.startswith("identity_")}
        for counters in ({"yf_daily": old}, {}):
            a = _analyze(tmp_path, counters)
            assert _hits(a) == []
            assert any("身份校验" in s for s in a.checks_skipped), counters


class TestLockTimeoutAlert:
    def test_timeout_is_p2(self, tmp_path):
        yg = dict(yf_gate.stats(), download_lock_timeouts=3, download_lock_wait_s=912.0, download_serialized=40)
        (h,) = _hits(_analyze(tmp_path, {"yf_daily": _clean_yd(), "yfinance": yg}))
        assert h.level == am.AlertLevel.MEDIUM and "超时 3 次" in h.message
        assert "912s / 串行 40 次" in h.details["累计排队"]

    def test_zero_timeouts_silent(self, tmp_path):
        yg = dict(yf_gate.stats(), download_serialized=270, download_lock_wait_s=5.0)
        assert _hits(_analyze(tmp_path, {"yf_daily": _clean_yd(), "yfinance": yg})) == []


class TestSummaryLine:
    def test_serialization_cost_is_visible(self):
        """扫描变慢多少看这行——上线时测不了负载下的耗时，只能让它每天自己报。"""
        c = {"yfinance": dict(yf_gate.stats(), calls=651, download_serialized=270, download_lock_wait_s=412.6)}
        line = scan_timing.summary_line({"phases": {}, "counters": c})
        assert "download串行270次/排队413s" in line
        assert "超时" not in line

    def test_silent_when_nothing_serialized(self):
        c = {"yfinance": dict(yf_gate.stats(), calls=5)}
        assert "download串行" not in scan_timing.summary_line({"phases": {}, "counters": c})
