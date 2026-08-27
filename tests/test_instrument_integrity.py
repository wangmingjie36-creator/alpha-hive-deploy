"""Phase 0 —— 仪表本身不许撒谎（v0.45.47）

这三处的共同点：它们是**用来发现其他问题的仪表**。仪表失准时，
后面所有修复都无法验证，所以在修复排期里排在第一位（零世代边界代价）。

  ① dashboard_renderer  数据真实度均值把 0 踢出分母 → 数据越烂显示越高
  ② real_data_sources   fallback 记为成功 → 降级告警永不触发
  ③ alert_manager       检查没跑成也报 "system healthy"
"""

import json

import pytest


# ─────────── ① 数据真实度：0 必须进分母 ───────────

class TestRealPctAverage:
    """实测：8/04、8/06、8/10~8/14 共 7 个扫描日各有 1 只 data_real_pct=0，
    全部是 BRK-B（v0.45.2 ticker 正则的受害者）—— 也就是说这个指标
    **恰好排除了唯一一只有彻底数据问题的标的**。"""

    @staticmethod
    def _avg(detail):
        """复刻 dashboard_renderer 的聚合逻辑"""
        rp = [detail[t].get("data_real_pct") for t in detail]
        vals = [v for v in rp if isinstance(v, (int, float))]
        return (sum(vals) / len(vals)) if vals else None

    def test_zero_counted_in_denominator(self):
        d = {"A": {"data_real_pct": 100}, "B": {"data_real_pct": 0}}
        assert self._avg(d) == 50.0, "0 必须计入 —— 它是最该被反映的那种"

    def test_old_truthy_filter_would_inflate(self):
        """回归对照：旧写法给出 100%，掩盖了一半标的完全没数据"""
        d = {"A": {"data_real_pct": 100}, "B": {"data_real_pct": 0}}
        old = [d[t].get("data_real_pct", 0) for t in d if d[t].get("data_real_pct")]
        assert sum(old) / len(old) == 100.0        # 旧行为
        assert self._avg(d) == 50.0                # 新行为

    def test_none_still_excluded(self):
        """None = 没算过，与 0 = 算过且为零，必须区别对待"""
        d = {"A": {"data_real_pct": 80}, "B": {"data_real_pct": None}, "C": {}}
        assert self._avg(d) == 80.0

    def test_all_missing_returns_none(self):
        assert self._avg({"A": {}, "B": {"data_real_pct": None}}) is None

    def test_source_has_no_truthy_filter(self):
        import inspect

        import dashboard_renderer
        src = inspect.getsource(dashboard_renderer)
        assert 'for t in swarm_detail if swarm_detail[t].get("data_real_pct")]' not in src, \
            "真值过滤会把 0 踢出分母"
        assert 'isinstance(v, (int, float))' in src


# ─────────── ② 健康追踪：fallback 不算成功 ───────────

class TestSourceHealthTracking:
    """_record_src_success 会把连续失败计数**重置为 0**，
    所以无条件调用它 = 降级告警永远不可能触发。"""

    @pytest.fixture(autouse=True)
    def _reset(self):
        import real_data_sources as r
        r._src_fail_counts.clear(); r._src_degraded.clear()
        yield
        r._src_fail_counts.clear(); r._src_degraded.clear()

    def _run(self, monkeypatch, short_ratio, short_pct):
        import real_data_sources as r
        monkeypatch.setattr(r, "_read_cache", lambda *a, **k: None)
        monkeypatch.setattr(r, "_write_cache", lambda *a, **k: None)

        class _T:
            info = {"shortRatio": short_ratio, "shortPercentOfFloat": short_pct}
        monkeypatch.setitem(__import__("sys").modules, "yfinance",
                            type("M", (), {"Ticker": staticmethod(lambda t: _T())})())
        return r.get_short_interest("TEST"), r

    def test_empty_response_counts_as_degradation(self, monkeypatch):
        res, r = self._run(monkeypatch, 0.0, 0.0)
        assert res["data_quality"] == "fallback"
        assert r._src_fail_counts.get("yfinance_short_interest") == 1, \
            "空数据必须计入失败，否则降级告警永不触发"

    def test_real_response_resets_counter(self, monkeypatch):
        import real_data_sources as r
        r._src_fail_counts["yfinance_short_interest"] = 2
        res, r = self._run(monkeypatch, 3.5, 0.08)
        assert res["data_quality"] == "real"
        assert r._src_fail_counts["yfinance_short_interest"] == 0

    def test_three_empty_responses_trigger_alert(self, monkeypatch):
        """核心不变式：连续 3 次空响应必须触发降级告警"""
        import real_data_sources as r
        for _ in range(3):
            self._run(monkeypatch, 0.0, 0.0)
        assert r._src_degraded.get("yfinance_short_interest") is True, \
            "旧实现下这个断言永远失败 —— 每次都被 success 清零"


# ─────────── ③ 告警系统：没查成 ≠ 健康 ───────────

class TestAlertManagerHonesty:
    @staticmethod
    def _analyze(tmp_path, status):
        from alert_manager import AlertAnalyzer
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status))
        a = AlertAnalyzer()
        return a, a.analyze(p)

    def test_missing_steps_result_is_recorded_as_skipped(self, tmp_path):
        a, alerts = self._analyze(tmp_path, {"status": "success"})
        assert a.checks_skipped, "steps_result 缺失时必须记为「检查未执行」"
        assert any("步骤失败检查" in s for s in a.checks_skipped)

    def test_empty_steps_result_also_skipped(self, tmp_path):
        a, _ = self._analyze(tmp_path, {"status": "success", "steps_result": {}})
        assert a.checks_skipped

    def test_present_steps_result_not_skipped(self, tmp_path):
        a, _ = self._analyze(tmp_path, {"status": "success",
                                        "steps_result": {"s1": {"status": "ok"}}})
        assert not any("步骤失败检查" in s for s in a.checks_skipped)

    def test_failed_step_still_alerts(self, tmp_path):
        """修复不能削弱原有能力"""
        _, alerts = self._analyze(tmp_path, {"status": "success",
                                             "steps_result": {"s1": {"status": "failed"}}})
        assert any("步骤失败" in al.message for al in alerts)

    def test_main_does_not_claim_healthy_when_checks_skipped(self):
        import inspect

        import alert_manager
        src = inspect.getsource(alert_manager)
        i = src.find("if not alerts:")
        blk = src[i:i + 600]
        assert "checks_skipped" in blk, "宣布 healthy 前必须先看有没有检查被跳过"
        assert "不能判定为健康" in blk

    def test_report_parse_failure_is_warning_not_debug(self):
        import inspect

        import alert_manager
        src = inspect.getsource(alert_manager)
        assert '_log.debug("JSON report parsing skipped' not in src
        assert "低分与数据质量检查未执行" in src
