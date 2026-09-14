"""期权快照槽位按「数据所属交易会话」分，不按太平洋墙钟日期（v0.45.238）

事故：`OptionsAgent.analyze()` 的槽位是 `pdt_today()`，命中校验是
`_snapshot_timestamp.startswith(pdt_today())`——两边是同一个墙钟，跨午夜写入会自证通过。
09-02 14:00 起跑的扫描拖到 09-03 00:07，午夜后写出的 `options_snapshot_{T}_2026-09-03.json`
装的是 09-02 盘后的数据；09-03 14:00 的正式扫描 42 次命中、只写 6 次，
`_snapshot_stock_price` 24/30 等于 09-02 官方收盘。09-09 同形（45 次命中，26/30）。

本文件钉三件事：
  ① 跨午夜的调用落回前一会话的槽位（不再占次日的位置）
  ② 旧代码留下的跨午夜文件（naive 时间戳、无 `_snapshot_session`）在次日被**拒收**
  ③ 命中一份收盘前冻结的快照、而此刻已收盘时，计数 + WARNING（「下游怎么知道？」）

全部离线：`fetch_options_chain` 换成抛异常的探针——走到它就说明没命中快照。
"""

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import options_analyzer as oa
from options_analyzer import OptionsAgent

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")


class _Fetched(RuntimeError):
    """探针：快照未命中后的第一个取数动作。"""


@pytest.fixture
def pacific_local_tz():
    """naive `_snapshot_timestamp` 按本机时区解释；生产机是太平洋时间，测试机未必是。"""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.fixture
def agent(tmp_path, monkeypatch):
    # conftest 给全体测试设了 OPTIONS_SNAPSHOT_DISABLE=1；本文件测的正是快照读写，
    # 不解开的话「不命中」会因为「根本没读」而通过。cache_dir 指向 tmp_path，不碰生产。
    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    monkeypatch.delenv("ALPHA_HIVE_TARGET_DATE", raising=False)
    oa.reset_snapshot_slot_stats()
    a = OptionsAgent()
    monkeypatch.setattr(a.fetcher, "cache_dir", str(tmp_path))

    def _probe(*_a, **_k):
        raise _Fetched

    monkeypatch.setattr(a.fetcher, "fetch_options_chain", _probe)
    # 命中路径会顺手补算价格派生字段 / 空报价集——与本文件无关，钉成 no-op
    monkeypatch.setattr(a, "_refresh_price_derived", lambda *x, **k: False)
    monkeypatch.setattr(a, "_refill_empty_quote_set", lambda *x, **k: False)
    return a


def _at(monkeypatch, dt):
    monkeypatch.setattr(oa, "_snapshot_now", lambda: dt)


def _write(tmp_path, ticker, slot, **fields):
    p = tmp_path / f"options_snapshot_{ticker}_{slot}.json"
    p.write_text(json.dumps({"iv_rank": 42.0, **fields}))
    return p


def _hit(agent, ticker="NVDA"):
    """True = 命中快照；False = 走到了取数探针。"""
    try:
        return agent.analyze(ticker, stock_price=100.0).get("iv_rank") == 42.0
    except _Fetched:
        return False


# ────────────────────────── 会话判据本身 ──────────────────────────

class TestSessionDate:
    @pytest.mark.parametrize("local, expected", [
        (datetime(2026, 9, 2, 14, 0, tzinfo=PT), "2026-09-02"),   # 周三收盘后
        (datetime(2026, 9, 3, 0, 7, tzinfo=PT), "2026-09-02"),    # 跨午夜 —— 本 bug
        (datetime(2026, 9, 3, 6, 29, tzinfo=PT), "2026-09-02"),   # 09:29 ET 盘前
        (datetime(2026, 9, 3, 6, 30, tzinfo=PT), "2026-09-03"),   # 09:30 ET 开盘翻页
        (datetime(2026, 9, 5, 12, 0, tzinfo=PT), "2026-09-04"),   # 周六 → 周五
        (datetime(2026, 9, 8, 5, 0, tzinfo=PT), "2026-09-04"),    # 劳工节后周二盘前 → 上周五
    ])
    def test_session_of_instant(self, local, expected):
        assert oa._snapshot_session(local) == (expected, True)

    def test_calendar_failure_falls_back_to_weekdays_not_wall_clock(self, monkeypatch):
        """日历挂了也不许退回太平洋日期——那正是本 bug。退回周一至周五规则并如实报告。"""
        import cboe_options
        monkeypatch.setattr(cboe_options, "session_date_at", lambda ts: None)
        assert oa._snapshot_session(datetime(2026, 9, 3, 0, 7, tzinfo=PT)) == ("2026-09-02", False)
        assert oa._snapshot_session(datetime(2026, 9, 5, 12, 0, tzinfo=PT)) == ("2026-09-04", False)

    def test_calendar_fallback_is_counted(self, agent, monkeypatch, tmp_path):
        import cboe_options
        monkeypatch.setattr(cboe_options, "session_date_at", lambda ts: None)
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        _hit(agent)
        assert oa.snapshot_slot_stats()["calendar_fallback"] == 1


# ────────────────────────── ① 跨午夜写回前一会话 ──────────────────────────

class TestCrossMidnightStaysInSession:
    def test_after_midnight_reuses_previous_session_slot(self, agent, monkeypatch, tmp_path):
        """同一轮扫描跨过午夜：00:07 的调用应命中 14:30 写的那份，而不是开一个次日槽位。"""
        _write(tmp_path, "NVDA", "2026-09-02",
               _snapshot_timestamp="2026-09-02T14:30:00-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 0, 7, tzinfo=PT))
        assert _hit(agent)

    def test_after_midnight_never_reads_next_day_slot(self, agent, monkeypatch, tmp_path):
        """次日槽位即便存在也不该被午夜后的调用读到（说明槽位名本身换了）。"""
        _write(tmp_path, "NVDA", "2026-09-03",
               _snapshot_timestamp="2026-09-03T00:05:00-07:00", _snapshot_session="2026-09-03")
        _at(monkeypatch, datetime(2026, 9, 3, 0, 7, tzinfo=PT))
        assert not _hit(agent)


# ────────────────────────── ② 旧代码留下的毒文件被拒收 ──────────────────────────

class TestLegacyPoisonedSlotRejected:
    def test_incident_2026_09_03_naive_timestamp(self, agent, monkeypatch, tmp_path, pacific_local_tz):
        """实测形状逐字复现：文件名 09-03、naive 时间戳 09-03T00:07:11、无会话字段。
        旧判据 `startswith("2026-09-03")` 放行；新判据推出它属于 09-02 会话 ⇒ 拒收。"""
        _write(tmp_path, "TSLA", "2026-09-03", _snapshot_timestamp="2026-09-03T00:07:11.123456")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 11, tzinfo=PT))
        assert not _hit(agent, "TSLA")
        st = oa.snapshot_slot_stats()
        assert st["session_mismatch"] == 1 and st["hits"] == 0

    def test_same_session_after_close_still_hits(self, agent, monkeypatch, tmp_path, pacific_local_tz):
        """别把闸写成永不命中：同会话、收盘后冻结的旧快照照常复用。"""
        _write(tmp_path, "TSLA", "2026-09-03", _snapshot_timestamp="2026-09-03T14:05:00")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 40, tzinfo=PT))
        assert _hit(agent, "TSLA")
        st = oa.snapshot_slot_stats()
        assert (st["hits"], st["session_mismatch"], st["hits_before_close"]) == (1, 0, 0)

    def test_unparseable_timestamp_is_rejected(self, agent, monkeypatch, tmp_path):
        _write(tmp_path, "NVDA", "2026-09-03", _snapshot_timestamp="")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        assert not _hit(agent)
        assert oa.snapshot_slot_stats()["session_mismatch"] == 1

    def test_stored_session_field_wins_over_timestamp(self, agent, monkeypatch, tmp_path):
        """新快照的 `_snapshot_session` 是取数前算的；写入时刻可能晚于 09:30 ET 翻页。"""
        _write(tmp_path, "NVDA", "2026-09-02",
               _snapshot_timestamp="2026-09-03T06:31:00-07:00", _snapshot_session="2026-09-02")
        _at(monkeypatch, datetime(2026, 9, 3, 6, 29, tzinfo=PT))
        assert _hit(agent)


# ────────────────────────── ③ 观测点：命中收盘前冻结的快照 ──────────────────────────

class TestFrozenBeforeCloseIsVisible:
    def test_intraday_snapshot_hit_after_close_is_counted_and_warned(
            self, agent, monkeypatch, tmp_path, caplog):
        _write(tmp_path, "NVDA", "2026-09-03",
               _snapshot_timestamp="2026-09-03T10:05:00-07:00")      # 13:05 ET，盘中
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))     # 17:00 ET，已收盘
        with caplog.at_level(logging.WARNING):
            assert _hit(agent), "只观测不拒收——拒收会让同一轮扫描前后拿到两份链"
        assert oa.snapshot_slot_stats()["hits_before_close"] == 1
        assert any("收盘前" in r.getMessage() for r in caplog.records)

    def test_stored_incomplete_flag_is_honoured(self, agent, monkeypatch, tmp_path):
        _write(tmp_path, "NVDA", "2026-09-03", _snapshot_session="2026-09-03",
               _snapshot_session_complete=False,
               _snapshot_timestamp="2026-09-03T13:01:00-07:00")      # 写入晚于收盘，但取数早于收盘
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        assert _hit(agent)
        assert oa.snapshot_slot_stats()["hits_before_close"] == 1

    def test_early_close_day_uses_13_00(self):
        """感恩节次日 13:00 ET 收盘：13:30 ET 冻结的快照是完整会话，不该被记成盘中。"""
        assert oa._session_close("2026-11-27") == datetime(2026, 11, 27, 13, 0, tzinfo=ET)
        assert oa._session_close("2026-09-03") == datetime(2026, 9, 3, 16, 0, tzinfo=ET)

    def test_intraday_hit_while_still_intraday_is_not_counted(self, agent, monkeypatch, tmp_path):
        """此刻也在盘中 ⇒ 没有更完整的数据可拿，不算事故。"""
        _write(tmp_path, "NVDA", "2026-09-03",
               _snapshot_timestamp="2026-09-03T10:05:00-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 11, 0, tzinfo=PT))
        assert _hit(agent)
        assert oa.snapshot_slot_stats()["hits_before_close"] == 0


# ────────────────────────── v0.45.249：按份去重 ──────────────────────────

def _warnings(caplog, needle):
    return [r for r in caplog.records if r.levelno >= logging.WARNING and needle in r.getMessage()]


class TestReportedOncePerSnapshot:
    """一只标的一轮扫描调 3~4 次 analyze()。v0.45.238 按调用计，一份盘中快照报 3~4 遍。"""

    CALLS = 4

    def test_intraday_hit_warns_and_counts_once(self, agent, monkeypatch, tmp_path, caplog):
        _write(tmp_path, "NVDA", "2026-09-03", _snapshot_timestamp="2026-09-03T10:05:00-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        with caplog.at_level(logging.DEBUG):
            for _ in range(self.CALLS):
                assert _hit(agent)
        st = oa.snapshot_slot_stats()
        assert st["hits"] == self.CALLS, "hits 仍是调用数（分母）"
        assert st["hits_before_close"] == 1, "按份计：同一份快照只计一次"
        assert len(_warnings(caplog, "收盘前")) == 1, "只警告一次，其余降为 DEBUG"

    def test_mismatch_warns_and_counts_once_when_refetch_keeps_failing(
            self, agent, monkeypatch, tmp_path, caplog):
        """重取失败 ⇒ 毒文件没被重写 ⇒ 每次调用都再读到它。"""
        _write(tmp_path, "TSLA", "2026-09-03", _snapshot_timestamp="2026-09-03T00:07:11-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 11, tzinfo=PT))
        with caplog.at_level(logging.DEBUG):
            for _ in range(self.CALLS):
                assert not _hit(agent, "TSLA")
        assert oa.snapshot_slot_stats()["session_mismatch"] == 1
        assert len(_warnings(caplog, "会话不匹配")) == 1

    def test_distinct_snapshots_each_count(self, agent, monkeypatch, tmp_path, caplog):
        """去重键是「这份快照」，不是「这个事件类型」——两只标的各记一次。"""
        for t in ("NVDA", "TSLA"):
            _write(tmp_path, t, "2026-09-03", _snapshot_timestamp="2026-09-03T10:05:00-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        with caplog.at_level(logging.WARNING):
            for t in ("NVDA", "TSLA", "NVDA", "TSLA"):
                assert _hit(agent, t)
        assert oa.snapshot_slot_stats()["hits_before_close"] == 2
        assert len(_warnings(caplog, "收盘前")) == 2

    def test_rewritten_snapshot_is_reported_again(self, agent, monkeypatch, tmp_path, caplog):
        """同一路径被重写成另一份快照（时间戳变了）后再出事，不能被旧记录吞掉。"""
        _write(tmp_path, "NVDA", "2026-09-03", _snapshot_timestamp="2026-09-03T10:05:00-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        with caplog.at_level(logging.WARNING):
            assert _hit(agent)
            _write(tmp_path, "NVDA", "2026-09-03", _snapshot_timestamp="2026-09-03T11:30:00-07:00")
            assert _hit(agent)
        assert oa.snapshot_slot_stats()["hits_before_close"] == 2
        assert len(_warnings(caplog, "收盘前")) == 2

    def test_calendar_fallback_warns_once_per_session(self, agent, monkeypatch, tmp_path, caplog):
        import cboe_options
        monkeypatch.setattr(cboe_options, "session_date_at", lambda ts: None)
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        with caplog.at_level(logging.WARNING):
            for _ in range(self.CALLS):
                _hit(agent)
        assert oa.snapshot_slot_stats()["calendar_fallback"] == 1
        assert len(_warnings(caplog, "交易日历不可用")) == 1

    def test_reset_clears_dedup_memory(self, agent, monkeypatch, tmp_path):
        """reset 之后同一份快照应重新可报——否则测试之间会互相吞掉事件。"""
        _write(tmp_path, "NVDA", "2026-09-03", _snapshot_timestamp="2026-09-03T10:05:00-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=PT))
        _hit(agent)
        oa.reset_snapshot_slot_stats()
        _hit(agent)
        assert oa.snapshot_slot_stats()["hits_before_close"] == 1


# ────────────────────────── 与补跑槽位（v0.45.16）的交互 ──────────────────────────

class TestBackfillInteraction:
    def test_backfill_slot_suffix_is_session(self, agent, monkeypatch, tmp_path):
        _write(tmp_path, "NVDA", "2026-08-24_backfilled-2026-09-03",
               _snapshot_timestamp="2026-09-03T14:00:00-07:00")
        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", "2026-08-24")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 30, tzinfo=PT))
        assert _hit(agent)

    def test_target_equal_to_session_uses_plain_slot(self, agent, monkeypatch, tmp_path):
        """周一盘前「补跑」上周五：此刻数据本就属于上周五会话，走正常槽位（可复用真快照）。"""
        _write(tmp_path, "NVDA", "2026-09-11", _snapshot_timestamp="2026-09-11T14:10:00-07:00")
        monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", "2026-09-11")
        _at(monkeypatch, datetime(2026, 9, 14, 5, 0, tzinfo=PT))
        assert _hit(agent)


# ────────────────────────── 计数真的进了 status.json ──────────────────────────

class TestCountersReachScanTiming:
    def test_scan_timing_carries_snapshot_slot_stats(self, agent, monkeypatch, tmp_path):
        import scan_timing
        _write(tmp_path, "NVDA", "2026-09-03", _snapshot_timestamp="2026-09-03T00:07:11-07:00")
        _at(monkeypatch, datetime(2026, 9, 3, 14, 11, tzinfo=PT))
        _hit(agent)
        got = scan_timing.counters()["options_snapshot"]
        assert got is not None and got["session_mismatch"] == 1
