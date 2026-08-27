"""扫描字段覆盖率闸的回归测试（v0.45.42）

存在理由：2026-08-26 yfinance 全线返回空，27/30 标的丢了 rv_30d/iv_rank，
催化剂 0/30，而**扫描退出码是 0，日报照常上站**。每处降级都被 except 老实
接住了，没有一个是 bug —— 缺的是有人在看"老实降级"发生了多少次。

本文件的每条测试都必须能在把闸拆掉时变红。
"""

import json

import pytest

import scan_coverage_gate as gate


def _mk(tickers, **fields):
    """构造最小 swarm_results：fields 形如 {"OracleBeeEcho": {"rv_30d": 30.0}}"""
    out = {}
    for t in tickers:
        ad = {}
        for agent, det in fields.items():
            ad[agent] = {"details": dict(det)}
        out[t] = {"agent_details": ad}
    return out


FULL = {
    "OracleBeeEcho": {"rv_30d": 30.0, "iv_rank": 45.0, "iv_current": 50.0,
                      "iv_skew_ratio": 1.02, "put_call_ratio": 0.9,
                      "iv_rv_spread": 20.0},
    "ChronosBeeHorizon": {"catalysts": [{"event": "Earnings"}]},
}
T30 = [f"T{i:02d}" for i in range(30)]


def _run(results, tmp_path, date="2026-08-26"):
    p = tmp_path / f".swarm_results_{date}.json"
    p.write_text(json.dumps(results))
    return gate.check(date, p)


class TestHealthyPasses:
    def test_full_coverage_healthy(self, tmp_path):
        res = _run(_mk(T30, **FULL), tmp_path)
        assert res["healthy"] is True
        assert res["degraded_fields"] == []


class TestDegradedGoesRed:
    def test_yfinance_wipeout_detected(self, tmp_path):
        """复刻 8/26：yfinance 派生字段全空，CBOE 字段完好"""
        r = _mk(T30, **FULL)
        for i, t in enumerate(T30):
            if i > 0:                       # 留 1 只成功，与实测 1/30 一致
                d = r[t]["agent_details"]["OracleBeeEcho"]["details"]
                d["rv_30d"] = None
                d["iv_rank"] = None
                r[t]["agent_details"]["ChronosBeeHorizon"]["details"]["catalysts"] = []
        res = _run(r, tmp_path)
        assert res["healthy"] is False
        assert set(res["degraded_fields"]) == {"rv_30d", "iv_rank", "catalysts"}
        # CBOE 侧完好 ⇒ 不该误判成网络层整体故障
        assert res["likely_network_layer"] is False

    def test_multi_source_outage_flagged_as_network(self, tmp_path):
        """yfinance 与 CBOE 同时挂 ⇒ 提示疑为网络/闸门层"""
        r = _mk(T30, **FULL)
        for t in T30:
            d = r[t]["agent_details"]["OracleBeeEcho"]["details"]
            d.update({"rv_30d": None, "iv_rank": None,
                      "iv_current": None, "iv_skew_ratio": None})
        res = _run(r, tmp_path)
        assert res["healthy"] is False
        assert res["likely_network_layer"] is True

    def test_single_ticker_failure_tolerated(self, tmp_path):
        """1/30 个别标的没数据是常态，不该报红"""
        r = _mk(T30, **FULL)
        r[T30[0]]["agent_details"]["OracleBeeEcho"]["details"]["rv_30d"] = None
        assert _run(r, tmp_path)["healthy"] is True

    def test_threshold_boundary(self, tmp_path):
        """恰好压线（70%）算健康，低一个就红"""
        r = _mk(T30, **FULL)
        for t in T30[:9]:                   # 21/30 = 70.0%
            r[t]["agent_details"]["OracleBeeEcho"]["details"]["rv_30d"] = None
        assert _run(r, tmp_path)["healthy"] is True
        r[T30[9]]["agent_details"]["OracleBeeEcho"]["details"]["rv_30d"] = None
        assert _run(r, tmp_path)["healthy"] is False   # 20/30 = 66.7%


class TestPresenceSemantics:
    def test_zero_and_false_count_as_present(self):
        """0 与 False 是合法读数，不是缺失 —— 否则会把真实的 0 误判成故障"""
        assert gate._present(0) is True
        assert gate._present(0.0) is True
        assert gate._present(False) is True

    def test_none_and_empty_containers_absent(self):
        for v in (None, [], {}, ""):
            assert gate._present(v) is False


class TestUndeterminable:
    def test_missing_file_is_code_3_not_healthy(self, tmp_path):
        """文件不存在必须是「无法判定」，绝不能默认成健康"""
        res = gate.check("2026-01-01", tmp_path / "nope.json")
        assert res["determinable"] is False
        assert "healthy" not in res, "无法判定时不得给出健康结论"

    def test_corrupt_file_undeterminable(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert gate.check("2026-01-01", p)["determinable"] is False

    def test_empty_results_undeterminable(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("{}")
        assert gate.check("2026-01-01", p)["determinable"] is False


class TestExitCodes:
    """退出码契约：编排器依赖它，改动必须过这一关"""

    @pytest.mark.parametrize("results,expected", [
        (_mk(T30, **FULL), 0),
    ])
    def test_healthy_exits_zero(self, results, expected, tmp_path, monkeypatch, capsys):
        p = tmp_path / ".swarm_results_2026-08-26.json"
        p.write_text(json.dumps(results))
        monkeypatch.setattr("sys.argv", ["x", "--date", "2026-08-26",
                                         "--file", str(p), "--quiet"])
        assert gate.main() == expected

    def test_degraded_exits_one(self, tmp_path, monkeypatch):
        r = _mk(T30, **FULL)
        for t in T30:
            r[t]["agent_details"]["OracleBeeEcho"]["details"]["rv_30d"] = None
        p = tmp_path / ".swarm_results_2026-08-26.json"
        p.write_text(json.dumps(r))
        monkeypatch.setattr("sys.argv", ["x", "--date", "2026-08-26",
                                         "--file", str(p), "--quiet"])
        assert gate.main() == 1

    def test_undeterminable_exits_three_not_two(self, tmp_path, monkeypatch):
        """3 而非 2 —— 编排器把 2 留给「脚本不存在」"""
        monkeypatch.setattr("sys.argv", ["x", "--date", "2026-01-01",
                                         "--file", str(tmp_path / "nope.json"), "--quiet"])
        assert gate.main() == 3


# ─────────── 价格可信度交叉核验（v0.45.45）───────────

class TestPriceCheck:
    """2026-08-26 暴露两种独立的 price_at_predict 污染，都只能靠与外部收盘价
    对照发现——内部自洽检查抓不到：

      ① 补跑窗口漂移：为业务日 D 重跑扫描，运行时刻已越过 D 的交易时段，
         现拉价格不再代表 D（NVDA +4.71%，30 只里 8 只 >1%）。
         期权快照因为**冻结**反而干净（中位 0.14% vs DB 0.36%）。
      ② 数据源单点乱码：CRM 写进 232.93，其近月最高仅 209.17，
         且同快照的期权支撑位在 160/190（对应 ~$205）。8/24、8/25 都正确。
    """

    @staticmethod
    def _db(tmp_path, rows, date="2026-08-26"):
        import sqlite3
        p = tmp_path / "t.db"
        cn = sqlite3.connect(p)
        cn.execute("CREATE TABLE predictions (date TEXT, ticker TEXT, price_at_predict REAL)")
        cn.executemany("INSERT INTO predictions VALUES (?,?,?)",
                       [(date, t, v) for t, v in rows.items()])
        cn.commit(); cn.close()
        return str(p)

    @staticmethod
    def _fake_close(monkeypatch, closes, date="2026-08-26"):
        """把 yfinance 换成固定收盘价表"""
        import pandas as pd
        idx = pd.to_datetime([date])
        df = pd.DataFrame({t: [v] for t, v in closes.items()}, index=idx)
        fake = pd.concat({"Close": df}, axis=1)
        monkeypatch.setitem(__import__("sys").modules, "yfinance",
                            type("M", (), {"download": staticmethod(lambda *a, **k: fake)})())

    def test_clean_prices_healthy(self, tmp_path, monkeypatch):
        self._fake_close(monkeypatch, {"AAA": 100.0, "BBB": 50.0})
        db = self._db(tmp_path, {"AAA": 100.1, "BBB": 49.95})
        r = gate.check_prices("2026-08-26", db)
        assert r["determinable"] and r["healthy"]
        assert r["bad"] == []

    def test_bad_reading_flagged(self, tmp_path, monkeypatch):
        """复刻 CRM：+12.98% —— 不可能是正常时点差"""
        self._fake_close(monkeypatch, {"CRM": 205.62, "AAA": 100.0})
        db = self._db(tmp_path, {"CRM": 232.32, "AAA": 100.0})
        r = gate.check_prices("2026-08-26", db)
        assert r["healthy"] is False
        assert [x["ticker"] for x in r["bad"]] == ["CRM"]

    def test_window_drift_warns_not_fails(self, tmp_path, monkeypatch):
        """复刻补跑漂移：+4.71% 进 warn，不算坏读数（量级不同，成因也不同）"""
        self._fake_close(monkeypatch, {"NVDA": 209.66})
        db = self._db(tmp_path, {"NVDA": 219.53})
        r = gate.check_prices("2026-08-26", db)
        assert r["healthy"] is True
        assert [x["ticker"] for x in r["warn"]] == ["NVDA"]

    def test_no_close_is_undeterminable_not_healthy(self, tmp_path, monkeypatch):
        """取不到收盘价必须说「不知道」，绝不能默认成价格可信"""
        self._fake_close(monkeypatch, {"AAA": 100.0}, date="2026-01-01")
        db = self._db(tmp_path, {"AAA": 100.0})
        r = gate.check_prices("2026-08-26", db)
        assert r["determinable"] is False
        assert "healthy" not in r

    def test_missing_db_undeterminable(self):
        r = gate.check_prices("2026-08-26", "/nonexistent/x.db")
        assert r["determinable"] is False


# ─────────── 来源标签诚实度（Phase 2 运行时护栏，v0.45.53）───────────

class TestLabelHonesty:
    """静态检查判不了「标签是不是在撒谎」—— `"data_quality": "fallback"`
    写在 except 里诚实、写在成功路径上可疑，同一行字面量，
    诚实与否取决于它在哪条分支上。实测：全仓 98 处字面量来源标签、
    175 处结果词字面量，静态筛完全是噪音。

    运行时的判据很硬：**标签宣称成功 ⇒ 它管辖的值必须非空**。
    """

    @staticmethod
    def _mk(tmp_path, details, name="s.json"):
        p = tmp_path / name
        p.write_text(json.dumps(
            {"NVDA": {"agent_details": {"OracleBeeEcho": {"details": details}}}}))
        return p

    def test_success_label_with_empty_value_is_contradiction(self, tmp_path):
        """复刻 2026-08-26：标签自称有来源，值却为空"""
        p = self._mk(tmp_path, {"iv_rank_source": "yfinance", "iv_rank": None,
                                "data_quality": "real", "iv_current": None})
        r = gate.check_label_honesty("2026-08-26", p)
        assert r["healthy"] is False
        fields = {c["value_field"] for c in r["contradictions"]}
        assert "OracleBeeEcho.iv_rank" in fields
        assert "OracleBeeEcho.iv_current" in fields

    def test_honest_degradation_passes(self, tmp_path):
        """标签自认 fallback + 值为空 = 诚实，不得报警"""
        p = self._mk(tmp_path, {"iv_rank_source": "unavailable", "iv_rank": None,
                                "data_quality": "fallback", "iv_current": None})
        r = gate.check_label_honesty("2026-08-26", p)
        assert r["healthy"] is True
        assert r["checked"] == 0, "标签自认降级时不该被计入「宣称成功」"

    def test_success_label_with_real_value_passes(self, tmp_path):
        p = self._mk(tmp_path, {"iv_rank_source": "yfinance", "iv_rank": 37.8,
                                "data_quality": "real", "iv_current": 48.6,
                                "put_call_ratio": 0.91})
        r = gate.check_label_honesty("2026-08-26", p)
        assert r["healthy"] is True and r["checked"] == 3

    def test_zero_counts_as_present(self, tmp_path):
        """0 是合法读数，不是缺失 —— 否则真实的 0 会被误报成矛盾"""
        p = self._mk(tmp_path, {"data_quality": "real", "iv_current": 0.0,
                                "put_call_ratio": 0.0})
        r = gate.check_label_honesty("2026-08-26", p)
        assert r["healthy"] is True

    def test_missing_file_undeterminable(self, tmp_path):
        r = gate.check_label_honesty("2026-08-26", tmp_path / "nope.json")
        assert r["determinable"] is False
        assert "healthy" not in r, "无法判定时不得给出健康结论"

    def test_real_scan_results_are_honest(self):
        """对真实扫描结果跑一遍 —— 现网不该有矛盾"""
        import pathlib
        files = sorted(pathlib.Path(".").glob(".swarm_results_*.json"))
        if not files:
            pytest.skip("无扫描结果")
        r = gate.check_label_honesty("x", files[-1])
        if not r.get("determinable"):
            pytest.skip(r.get("reason"))
        assert r["healthy"], f"现网存在标签矛盾：{r['contradictions'][:3]}"
