"""国会交易源暂停（v0.45.748，2026-11-06 用户定）。

背景：Quiver 端点 2026-06 起要凭证（401），06-23 之后零数据；ScoutBee 所在的 signal 维度权重 2026-09-10 起为 0。
暂停 = `get_congress_trades` 直接返回 []，不发请求、不读写缓存。

本文件钉住：
1. 生产缺省就是暂停（墓碑：恢复取数会改 Scout 分 ⇒ 先登世代边界，再改这条）。
2. 暂停时取数三件套（`_load_cached` / `_fetch_raw` / `_save_cache`）一个都不碰，碰了就红；传输层另有整套的
   出网闸（`tests/conftest.py::_offline_transport`）兜着。
3. **与旧失败路径逐位相同**：暂停时 `get_congress_trades_for_ticker` 的产出，与取数失败（`_fetch_raw` 回 []，
   即此前每天 401 的那条路）时的产出逐键相同、连类型都相同 ⇒ Scout 的 details.congress、signal_archive 的
   congress.score 都不变，所以不用登世代边界。
4. 观测点：暂停时每个进程记一行 INFO，只记一次（Scout 每轮按名单每只票各调一次）。
"""
from __future__ import annotations

import logging

import pytest

import congress_trades_scraper as cts

_IO = ("_load_cached", "_fetch_raw", "_save_cache")


def _boom(*_a, **_k):
    raise AssertionError("暂停时不该碰取数 / 缓存")


@pytest.fixture
def paused_no_io(monkeypatch):
    monkeypatch.setattr(cts, "SOURCE_PAUSED", True)
    for name in _IO:
        monkeypatch.setattr(cts, name, _boom)


def test_production_default_is_paused():
    assert cts.SOURCE_PAUSED is True, (
        "国会交易源 v0.45.748 起暂停。恢复取数（如换源）会改 Scout 分：先在 "
        "ic_rerun_readiness._COHORT_HISTORY 登世代边界，再改这条断言（见 congress_trades_scraper 的暂停说明）")


def test_paused_touches_no_fetch_or_cache(paused_no_io):
    assert cts.get_congress_trades() == []
    assert cts.get_congress_trades(tickers=["NVDA"], days_back=90, force_refresh=True) == []
    out = cts.get_watchlist_congress_summary(["NVDA", "TSLA"])
    assert set(out) == {"NVDA", "TSLA"}
    assert all(v["buy_count"] == 0 and v["sell_count"] == 0 and v["trades"] == [] for v in out.values())


def test_paused_output_identical_to_old_failure_path(monkeypatch):
    # 旧失败路径：缓存过期 ⇒ 取数 ⇒ 401 被 `_fetch_raw` 吞成 [] ⇒ 写回空缓存
    monkeypatch.setattr(cts, "SOURCE_PAUSED", False)
    monkeypatch.setattr(cts, "_load_cached", lambda force_refresh: None)
    monkeypatch.setattr(cts, "_fetch_raw", lambda: [])
    monkeypatch.setattr(cts, "_save_cache", lambda data: None)
    before = cts.get_congress_trades_for_ticker("TSLA", days_back=90)

    monkeypatch.setattr(cts, "SOURCE_PAUSED", True)
    for name in _IO:
        monkeypatch.setattr(cts, name, _boom)
    after = cts.get_congress_trades_for_ticker("TSLA", days_back=90)

    assert after == before
    assert {k: type(v) for k, v in after.items()} == {k: type(v) for k, v in before.items()}
    # 生产 11-06 落盘的 TSLA details.congress 就是这几个值；Scout 只在 trades 非空时才用国会分 ⇒ 国会项恒为中性 5.0
    assert after["trades"] == [] and after["congress_score"] == 0.0 and after["top_signal"] == ""
    assert isinstance(after["congress_score"], float)   # 落盘是 0.0 不是 0（signal_archive 的 congress.score 原样存）
    assert after["summary"] == "【国会议员交易 · TSLA】近 90 天无披露记录"


def test_paused_logs_once_per_process(monkeypatch, paused_no_io):
    monkeypatch.setattr(cts, "_paused_logged", False)
    records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level=logging.DEBUG)
    old_level = cts._log.level
    cts._log.addHandler(handler)
    cts._log.setLevel(logging.DEBUG)
    try:
        for _ in range(3):
            cts.get_congress_trades_for_ticker("NVDA")
    finally:
        cts._log.removeHandler(handler)
        cts._log.setLevel(old_level)
    paused = [r for r in records if "已暂停" in r.getMessage()]
    assert len(paused) == 1, [r.getMessage() for r in records]
    assert paused[0].levelno == logging.INFO
