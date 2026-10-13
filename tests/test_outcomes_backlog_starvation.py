"""T+N 回填队列饿死（v0.45.493）。

旧实现：`_scan_pending` 按文件名（标的在前）排序、取前 50 份；每格都 sleep，本地命中也算在 50 里。
08-10 起 30 只 × 3 个期限每天最多到期 ~90 > 50 ⇒ 每轮从字母表开头重来，T / TMUS / TSLA / VKTX / VZ / WMT / XOM
09 月起一份都没回填过（10-13 待回填 424 份，它们占 238），日志却每轮 `scanned=50 updated=50 errors=0`。

这里的夹具复刻那个形状：30 只标的、待回填份数远超 50，价格全部可由本地快照查到。
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import outcomes_fetcher as of
from outcomes_fetcher import OutcomesFetcher, backlog_summary

TICKERS = ["ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL", "CRM", "CVX", "DE", "DELL", "ENPH",
           "JNJ", "META", "MSFT", "MU", "NEE", "NFLX", "NVDA", "QCOM", "RKLB", "SNOW", "T", "TMO", "TMUS",
           "TSLA", "VKTX", "VZ", "WMT", "XOM"]


def _bdays(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _write(d, ticker, day, price, t1=None, t7=None, t30=None):
    data = {"ticker": ticker, "date": day.isoformat(), "composite_score": 5.0, "direction": "neutral",
            "entry_price": price, "agent_votes": {}, "cs_rank": {"final": 0.5},
            "actual_prices": {"t1": t1, "t7": t7, "t30": t30}, "created_at": f"{day.isoformat()}T14:40:00"}
    (d / f"{ticker}_{day.isoformat()}.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def universe(tmp_path, monkeypatch):
    """30 只 × 最近 15 个工作日的快照（每天都有 entry_price ⇒ T+1 / T+7 全部本地可查）。出网被关掉。"""
    monkeypatch.setattr(of, "_yf", None)
    d = tmp_path / "report_snapshots"
    d.mkdir()
    days = _bdays(date.today() - timedelta(days=24), 15)
    days = [x for x in days if x < date.today()]
    for i, tk in enumerate(TICKERS):
        for j, day in enumerate(days):
            _write(d, tk, day, 100.0 + i + j)
    return d


def _pending_by_ticker(d):
    return {os.path.basename(p).rsplit("_", 1)[0] for p, _, _ in OutcomesFetcher(str(d))._scan_pending()}


def test_fixture_reproduces_the_overload(universe):
    """自证：待回填份数确实远超旧配额 50，且字母表两头的标的都在队列里。"""
    pending = OutcomesFetcher(str(universe))._scan_pending()
    assert len(pending) > 150
    tks = _pending_by_ticker(universe)
    assert {"ABBV", "XOM"} <= tks


def test_one_run_drains_everything_locally_resolvable(universe, monkeypatch):
    """核心回归：一轮之内本地能查到的全部补上——包括字母序最后的 XOM；本地命中不 sleep、不占出网配额。"""
    sleeps = []
    monkeypatch.setattr(of.time, "sleep", lambda s: sleeps.append(s))
    f = OutcomesFetcher(str(universe), rate_limit=0.5, max_network_fetches=50)
    stats = f.run()

    assert stats["errors"] == 0 and stats["network_fetches"] == 0 and sleeps == []
    assert stats["updated"] > 50
    left = OutcomesFetcher(str(universe))._scan_pending()
    # 剩下的只能是本地查不到、又不许出网的格子（窗口里还没有后续快照的最新几天），不能是某只标的整体被跳过
    assert {"XOM", "WMT", "VZ", "TSLA"}.isdisjoint(
        {os.path.basename(p).rsplit("_", 1)[0] for p, _, m in left if 7 in m and len(m) > 1})
    xom = sorted(universe.glob("XOM_*.json"))[0]
    assert json.loads(xom.read_text(encoding="utf-8"))["actual_prices"]["t1"] is not None


def test_scan_orders_by_oldest_due_not_filename(tmp_path, monkeypatch):
    """排队按到期先后：XOM 的老快照排在 ABBV 的新快照前面。"""
    monkeypatch.setattr(of, "_yf", None)
    d = tmp_path / "s"
    d.mkdir()
    today = date.today()
    _write(d, "ABBV", today - timedelta(days=6), 10.0)
    _write(d, "XOM", today - timedelta(days=40), 20.0)
    _write(d, "MSFT", today - timedelta(days=20), 30.0)
    order = [os.path.basename(p).split("_")[0] for p, _, _ in OutcomesFetcher(str(d))._scan_pending()]
    assert order == ["XOM", "MSFT", "ABBV"]


class _FakeYF:
    """记录调用次数；可让前 k 次抛限流。"""

    def __init__(self, rate_limited_first=0):
        self.calls = 0
        self.rl = rate_limited_first

    def Ticker(self, tk):
        outer = self

        class _T:
            def history(self, **kw):
                import pandas as pd
                outer.calls += 1
                if outer.calls <= outer.rl:
                    raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
                return pd.DataFrame({"Close": [42.0]})
        return _T()


def _network_only_world(tmp_path, n):
    """n 份只靠出网才能补的快照：每只标的一份、5~12 天前（只有 t1 到期），入场价 0 ⇒ 本地价格索引里没有它们。
    ⚠️ 别给 t7 / t30 填值来「标记已回填」：_load_price_index 会把已回填的价格反推进本地索引，t1 就从本地命中了。"""
    assert n <= 8
    d = tmp_path / "net"
    d.mkdir()
    for i in range(n):
        _write(d, f"Z{i:02d}", date.today() - timedelta(days=5 + i), 0.0)
    return d


def test_network_budget_caps_only_network_and_defers_the_rest(tmp_path, monkeypatch):
    fake = _FakeYF()
    monkeypatch.setattr(of, "_yf", fake)
    sleeps = []
    monkeypatch.setattr(of.time, "sleep", lambda s: sleeps.append(s))
    d = _network_only_world(tmp_path, 8)
    stats = OutcomesFetcher(str(d), rate_limit=0.5, max_network_fetches=3).run()
    assert fake.calls == 3 and stats["network_fetches"] == 3 and stats["network_deferred"] == 5
    assert len(sleeps) == 3                     # sleep 只跟在出网请求后面
    # 被推迟的是较新的；下一轮最老的先上
    left = [os.path.basename(p) for p, _, _ in OutcomesFetcher(str(d))._scan_pending()]
    assert len(left) == 5 and left == sorted(left, key=lambda n: n.split("_")[1])


def test_rate_limit_streak_stops_network_but_not_local(tmp_path, monkeypatch):
    fake = _FakeYF(rate_limited_first=10)
    monkeypatch.setattr(of, "_yf", fake)
    monkeypatch.setattr(of.time, "sleep", lambda s: None)
    d = _network_only_world(tmp_path, 6)
    # 再放一份本地能查到的：DDD 有当天快照 + 次日快照
    day0 = _bdays(date.today() - timedelta(days=12), 2)
    _write(d, "DDD", day0[0], 50.0)
    _write(d, "DDD", day0[1], 51.0)
    stats = OutcomesFetcher(str(d), rate_limit=0, max_network_fetches=50).run()
    assert fake.calls == 3                       # 连击 3 次后本轮不再出网
    assert stats["network_deferred"] == 6 + 1 and stats["local_hits"] == 1   # 6 份 Z + DDD 第二份的 t1（后面没快照）
    ddd = json.loads((d / f"DDD_{day0[0].isoformat()}.json").read_text(encoding="utf-8"))
    assert ddd["actual_prices"]["t1"] == 51.0
    assert ddd["cs_rank"] == {"final": 0.5}      # 顺带：回填不抹 cs_rank（v0.45.491）


class TestBacklogSummary:
    def test_reports_oldest_overdue(self, tmp_path):
        d = tmp_path / "s"
        d.mkdir()
        today = date(2026, 10, 13)
        _write(d, "XOM", date(2026, 9, 1), 1.0, t1=1.0)           # t7 due 09-14 ⇒ 逾期 29；t30 due 10-16 未到
        _write(d, "ABBV", date(2026, 10, 9), 1.0)                  # t1 due 10-13 ⇒ 逾期 0
        _write(d, "AMD", date(2026, 9, 1), 1.0, t1=1.0, t7=1.0)    # 只缺 t30，未到期 ⇒ 不算
        (d / "broken.json").write_text("{", encoding="utf-8")
        s = backlog_summary(str(d), today=today)
        assert s["available"] is True
        assert (s["pending_files"], s["pending_offsets"]) == (2, 2)
        assert s["oldest_overdue_days"] == 29 and s["oldest_item"] == "XOM_2026-09-01 t7"
        assert s["max_overdue_days"] == of.MAX_OVERDUE_DAYS and s["unreadable_files"] == 1

    def test_missing_dir_is_unavailable_not_zero(self, tmp_path):
        s = backlog_summary(str(tmp_path / "nope"))
        assert s["available"] is False and "不存在" in s["reason"]

    def test_due_date_matches_scan_pending(self, tmp_path, monkeypatch):
        """观测与回填对「到期」必须是同一个定义：backlog 说待回填的份数 == _scan_pending 找到的份数。"""
        monkeypatch.setattr(of, "_yf", None)
        d = tmp_path / "s"
        d.mkdir()
        for k in range(0, 50, 3):
            _write(d, "NVDA", date.today() - timedelta(days=k), 1.0)
        assert backlog_summary(str(d))["pending_files"] == len(OutcomesFetcher(str(d))._scan_pending())


# ── alert_manager ────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alert_manager as am  # noqa: E402


def _status(ob="__absent__", step2="success"):
    extra = {"gh_pages": {"success": True}}
    if ob != "__absent__":
        extra["outcomes_backlog"] = ob
    return {"status": "success", "total_duration_seconds": 2000,
            "steps_result": {"step2_hive_analysis": {"status": step2}},
            "scan_timing": {"extra": extra, "production_sync": {"outcome": "up_to_date"}}}


def _ob(late):
    return {"available": True, "pending_files": 3, "pending_offsets": 4, "oldest_overdue_days": late,
            "oldest_item": "XOM_2026-09-01 t7", "max_overdue_days": of.MAX_OVERDUE_DAYS, "unreadable_files": 0}


def _hits(tmp_path, status):
    a = am.AlertAnalyzer(report_dir=tmp_path)
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status, ensure_ascii=False))
    a.analyze(p)
    return [x for x in a.alerts if "回填" in x.message]


class TestAlert:
    def test_draining_queue_is_quiet(self, tmp_path):
        assert _hits(tmp_path, _status(_ob(late=2))) == []

    def test_stale_backlog_is_p2(self, tmp_path):
        hits = _hits(tmp_path, _status(_ob(late=29)))
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "逾期 29" in hits[0].message

    def test_boundary_is_not_alerted(self, tmp_path):
        assert _hits(tmp_path, _status(_ob(late=of.MAX_OVERDUE_DAYS))) == []

    def test_missing_observation_is_p2_not_silence(self, tmp_path):
        hits = _hits(tmp_path, _status())
        assert [h.level for h in hits] == [am.AlertLevel.MEDIUM] and "未观测" in hits[0].message

    def test_scan_that_did_not_run_is_not_judged(self, tmp_path):
        assert _hits(tmp_path, _status(_ob(late=99), step2="failed")) == []


class TestDailyScanWritesIt:
    """真跑 `main()` 的蜂群路径（报告生成器打桩，同 test_cloud_snapshot_freshness）：outcomes_backlog 写进 scan_timing。"""

    def test_main_writes_outcomes_backlog(self, monkeypatch, tmp_path):
        import alpha_hive_daily_report as adr
        import cloud_snapshot_loader as csl
        import yf_gate

        snaps = tmp_path / "report_snapshots"
        snaps.mkdir()
        _write(snaps, "XOM", date.today() - timedelta(days=40), 1.0)

        class SwarmReporter:
            date_str = "2026-09-14"
            report_dir = tmp_path

            def __init__(self, date_override=None):
                pass

            def run_swarm_scan(self, focus_tickers=None):
                return {"system_status": "✅ 蜂群协作完成", "swarm_metadata": {"tickers_analyzed": 1},
                        "opportunities": [{"ticker": "NVDA"}]}

            def save_report(self, report):
                return "/dev/null"

            def deploy_and_notify(self, report):
                return {"gh_pages": {"success": True}, "deploy_env": "production"}

        written = []
        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", "unset-by-teardown")
        monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SwarmReporter)
        monkeypatch.setattr(yf_gate, "install", lambda: False)
        monkeypatch.setattr(adr._timing, "write", lambda d, extra=None, **k: written.append(extra))
        monkeypatch.setattr(csl, "lag_summary", lambda today, **k: {"available": True})
        monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--swarm", "--no-llm", "--force"])
        adr.main()
        ob = written[0]["outcomes_backlog"]
        assert ob["available"] is True and ob["pending_files"] == 1 and ob["oldest_overdue_days"] > 7, ob
