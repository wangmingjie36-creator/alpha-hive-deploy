"""陈旧 payload 的缓存不变式 + 盘中冻结告警（v0.45.88）。

两件事，都由 2026-08-28~31 的 TMO/BILI 事故带出来。

一、`skip_staleness_check` 不得顺手关掉「陈旧绝不入缓存」
--------------------------------------------------------
v0.45.68 把新鲜度**裁决权**让给了判据更准的调用方（`cloud_snapshot_fetch`
按 business_date 比对），这个方向是对的。但它同时让陈旧 payload 照常写进
进程缓存——那是 v0.45.39 立的另一条不变式，理由与裁决权无关：

    写了就等于把陈旧数据在进程内又保鲜 120 秒。

「谁来裁决」和「要不要缓存」是两件独立的事，不该被一个开关一起关掉。
代价为零：调用方拿到陈旧 payload 后立刻抛 StaleVintageError，三个解析调用
根本不会发生，不存在「缓存未命中导致重复下载」。

二、vintage 只比日期，同日冻在盘中的记录会静默通过
--------------------------------------------------
无论由哪一层裁决，判据都是**日期字符串**。8/28 的 TMO 冻在 09:45:27
（其余 29 只都在 15:59~16:00），日期同为 8/28 → 判定新鲜 → 存下
price_at_fetch=626.325 标 price_source=cboe_close、vintage_status=ok。
CBOE 事后回填的 8/28 真实收盘是 622.18，差 $4.15（0.67%）。
`intraday_freeze_suspect` 就是给这种情况留的信号——**告警，不拒绝落盘**。
"""

import datetime as _dt
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cboe_options as co  # noqa: E402
import cloud_snapshot_fetch as csf  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    co.invalidate_payload_cache()
    co._vintage_stats.update({"checked": 0, "stale": 0})
    yield
    co.invalidate_payload_cache()


def _raw(last_trade="2026-08-31T15:59:59"):
    return json.dumps({"timestamp": "2026-08-31 20:41:33", "symbol": "X",
                       "data": {"options": [{"option": "X260904C00100000"}],
                                "current_price": 100.0, "close": 100.0,
                                "prev_day_close": 99.0,
                                "last_trade_time": last_trade}}).encode()


class _Resp:
    def __init__(self, b): self._b = b
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch_net(monkeypatch, body):
    monkeypatch.setattr(co.urllib.request, "urlopen", lambda *a, **k: _Resp(body))


# ══════════════════════════════════════════════════════════════════
# 一、缓存不变式：让权裁决 ≠ 允许缓存陈旧
# ══════════════════════════════════════════════════════════════════

def test_skip_returns_stale_payload_to_caller(monkeypatch):
    """让权的前提：陈旧 payload 必须真的交到调用方手里，否则外层无从裁决。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"))     # TMO 实测场景
    d = co._fetch_cboe_payload("TMO", 15, skip_staleness_check=True)
    assert d is not None and d["last_trade_time"] == "2026-08-28T15:59:59"


def test_skip_still_does_not_cache_stale(monkeypatch):
    """核心守卫：让权裁决不等于放弃「陈旧绝不入缓存」。

    缓存住了就等于在进程内把陈旧数据又保鲜 120 秒——v0.45.39 立这条不变式
    时的原话。它与「谁来裁决新鲜度」正交，不该被同一个开关一起关掉。
    """
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"))
    co._fetch_cboe_payload("TMO", 15, skip_staleness_check=True)
    assert "TMO" not in co._payload_cache, "陈旧 payload 被写进了缓存"


def test_default_rejects_and_does_not_cache(monkeypatch):
    """回归守卫：不传开关时，另外四个消费点看到的仍然是 None + 不入缓存。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"))
    assert co._fetch_cboe_payload("TMO", 15) is None
    assert "TMO" not in co._payload_cache
    assert co._vintage_stats["stale"] == 1


def test_fresh_payload_still_cached_under_skip(monkeypatch):
    """正路不误伤：新鲜数据在 skip 模式下照常入缓存，共享一次下载的收益还在。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-31T15:59:59"))
    d = co._fetch_cboe_payload("NVDA", 15, skip_staleness_check=True)
    assert d is not None
    assert "NVDA" in co._payload_cache, "新鲜 payload 不该被误伤，否则每标的多拉两次"


def test_stale_then_recovered_not_served_from_cache(monkeypatch):
    """补抓要真的重新下载。若首轮把陈旧写进了缓存，补抓会原样拿回那份旧的。

    这里刻意**不调** invalidate_payload_cache —— 证明不变式本身就够，
    不依赖调用方记得清缓存。
    """
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"))
    co._fetch_cboe_payload("TMO", 15, skip_staleness_check=True)
    _patch_net(monkeypatch, _raw("2026-08-31T16:00:00"))     # CDN 追上了
    d = co._fetch_cboe_payload("TMO", 15, skip_staleness_check=True)
    assert d["last_trade_time"] == "2026-08-31T16:00:00", "补抓拿回了缓存里的陈旧数据"


# ══════════════════════════════════════════════════════════════════
# 二、盘中冻结告警
# ══════════════════════════════════════════════════════════════════

def _at(monkeypatch, et_now: str):
    """把 cloud_snapshot_fetch 眼里的「现在」钉死（ET）。"""
    real = csf.datetime
    fixed = _dt.datetime.fromisoformat(et_now).replace(tzinfo=csf._ET)

    class _DT(real):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed
    monkeypatch.setattr(csf, "datetime", _DT)


def test_freeze_lag_flags_the_828_tmo_case(monkeypatch):
    """8/28 TMO：09:45:27 距 16:00 收盘 375 分钟 → 必须报。"""
    _at(monkeypatch, "2026-08-28T17:02:00")
    lag = csf._freeze_lag_min("2026-08-28T09:45:27", "2026-08-28")
    assert lag is not None and round(lag) == 375
    assert lag >= csf._FREEZE_LAG_WARN_MIN


def test_normal_close_record_not_flagged(monkeypatch):
    """15:59:59 是正常收盘记录，绝不能误报。"""
    _at(monkeypatch, "2026-08-31T17:02:00")
    lag = csf._freeze_lag_min("2026-08-31T15:59:59", "2026-08-31")
    assert lag is not None and lag < csf._FREEZE_LAG_WARN_MIN


def test_thin_liquidity_within_tolerance_not_flagged(monkeypatch):
    """8/31 实测 DE=14:20(100min) / CVX=14:30(90min)：薄流动性正常值，不该卷进来。"""
    _at(monkeypatch, "2026-08-31T17:02:00")
    for raw in ("2026-08-31T14:20:20", "2026-08-31T14:30:58"):
        assert csf._freeze_lag_min(raw, "2026-08-31") < csf._FREEZE_LAG_WARN_MIN


def test_no_flag_before_close(monkeypatch):
    """盘中跑：`早于收盘`是常态，此时判断无意义 → None。"""
    _at(monkeypatch, "2026-08-31T11:30:00")
    assert csf._freeze_lag_min("2026-08-31T11:06:56", "2026-08-31") is None


def test_after_hours_trade_clamps_to_zero(monkeypatch):
    """盘后成交（>16:00）不是冻结，负 lag 归零而不是报警。"""
    _at(monkeypatch, "2026-08-31T18:00:00")
    assert csf._freeze_lag_min("2026-08-31T17:30:00", "2026-08-31") == 0.0


def test_cross_day_left_to_vintage_check(monkeypatch):
    """跨日归 vintage 校验管，冻结检测不重复计数。"""
    _at(monkeypatch, "2026-08-31T17:02:00")
    assert csf._freeze_lag_min("2026-08-28T15:59:59", "2026-08-31") is None


@pytest.mark.parametrize("bad", [None, "", "not-a-time", 12345])
def test_unparseable_input_is_silent(monkeypatch, bad):
    _at(monkeypatch, "2026-08-31T17:02:00")
    assert csf._freeze_lag_min(bad, "2026-08-31") is None


def test_frozen_ticker_surfaces_in_manifest(monkeypatch, tmp_path):
    """标记要一路走到 manifest —— 消费端只读 manifest，不会去翻 30 个文件。"""
    fake_cboe = types.ModuleType("cboe_fetcher")
    fake_cboe.CBOEDailyFetcher = lambda: types.SimpleNamespace(
        fetch_all=lambda: {"vix_term": {"source": "live"}})
    fake_fg = types.ModuleType("fear_greed")
    fake_fg.get_fear_greed = lambda: {"value": 50}
    monkeypatch.setitem(sys.modules, "cboe_fetcher", fake_cboe)
    monkeypatch.setitem(sys.modules, "fear_greed", fake_fg)

    def _snap(t, d):
        return {"ticker": t, "schema_version": csf.SCHEMA_VERSION,
                "fetched_at_utc": "2026-08-31T21:00:00+00:00",
                "price_at_fetch": 622.18, "price_source": "cboe_close",
                "last_trade_time_et": "2026-08-31T15:59:59",
                "vintage_date": d, "vintage_status": "ok",
                "prev_day_close": 620.0,
                "last_trade_lag_min": 293.1 if t == "TMO" else 0.0,
                "intraday_freeze_suspect": t == "TMO",
                "chain": {"calls": []}, "iv_term_structure": None,
                "full_chain_oi": None}

    monkeypatch.setattr(csf, "_fetch_one_ticker", _snap)
    monkeypatch.setattr(sys, "argv",
                        ["x", "--out", str(tmp_path), "--tickers", "JNJ,TMO"])
    csf.main()

    man = json.loads((tmp_path / csf._business_date() / "manifest.json").read_text())
    assert man["vintage_intraday_freeze_suspect"] == ["TMO"]
    assert man["tickers_ok"] == 2, "冻结是告警，不该拒绝落盘"
    assert man["failed"] == {}
