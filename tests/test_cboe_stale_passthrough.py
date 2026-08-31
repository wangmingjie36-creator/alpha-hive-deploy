"""CBOE vintage 陈旧信号透传 + 盘中冻结告警守卫（v0.45.88）。

背景（实测，非推测）
--------------------
2026-08-31 云端快照 TMO 失败，manifest 记的是
`RuntimeError: CBOE payload 为空（网络/403/符号问题）`，而真实原因是
CBOE CDN 的 TMO 文件停在 08-28（实测 last_trade_time=2026-08-28T15:59:59，
同批 JNJ/ABBV 均为 08-31）。两个后果：

1. **补抓从未触发**。陈旧在抓取层表现为 `_fetch_cboe_payload` 返回 `None`，
   与网络故障同形 → `_fetch_one_ticker` 抛通用 RuntimeError →
   `except StaleVintageError` 捕不到 → 落进 `failed` 而进不了 `stale` →
   `if stale and not abort_reason:` 的补抓循环空转。
   实测佐证：当天日志无「🔁 补抓」行，manifest 的 vintage_stale=[]。

2. **盘中冻结记录静默通过**。vintage 闸门比的是日期字符串，同日内冻在
   什么时刻一概放行。8/28 的 TMO 冻在 09:45:27（其余 29 只 15:59~16:00），
   存下 price_at_fetch=626.325 标 price_source=cboe_close、vintage_status=ok，
   而 CBOE 事后回填的 8/28 真实收盘是 622.18（差 $4.15 / 0.67%）。

守什么
------
1. `raise_on_stale=True` 时陈旧抛 `CBOEStaleVintageError`，且**不被重试循环吞掉**
2. 默认 `raise_on_stale=False` 行为一字不改（另外四个消费点靠 None 走降级链）
3. 陈旧标的必须进 `stale` 列表 → 补抓能认得它 → 滞后型可自愈
4. 同日但冻在盘中的记录必须被标记（告警，**不拒绝落盘**）
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


def _patch_net(monkeypatch, body, counter=None):
    def _open(*a, **k):
        if counter is not None:
            counter.append(1)
        return _Resp(body)
    monkeypatch.setattr(co.urllib.request, "urlopen", _open)


# ══════════════════════════════════════════════════════════════════
# 1. 陈旧信号透传
# ══════════════════════════════════════════════════════════════════

def test_raise_on_stale_surfaces_typed_error(monkeypatch):
    """TMO 实测场景：CDN 停在 8/28，应有 8/31 → 抛出可识别的类型。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"))
    with pytest.raises(co.CBOEStaleVintageError) as ei:
        co._fetch_cboe_payload("TMO", 15, raise_on_stale=True)
    err = ei.value
    assert (err.ticker, err.got, err.expected) == ("TMO", "2026-08-28", "2026-08-31")
    # 错误信息必须自证根因，不能再是「网络/403/符号问题」那种误导性描述
    assert "CDN" in str(err) and "2026-08-28" in str(err)


def test_default_still_returns_none(monkeypatch):
    """回归守卫：options_analyzer ×3 / data_pipeline ×1 全靠 None 走降级链。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"))
    assert co._fetch_cboe_payload("TMO", 15) is None
    assert co._vintage_stats["stale"] == 1


def test_stale_error_not_swallowed_by_retry_loop(monkeypatch):
    """陈旧是判定结果不是瞬时故障。

    若被通用 `except Exception` 接住，会重试 3 次同一份 CDN 文件后
    `return None` —— 开关等于白开，且白烧两次网络请求。
    """
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    calls = []
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"), counter=calls)
    with pytest.raises(co.CBOEStaleVintageError):
        co._fetch_cboe_payload("TMO", 15, raise_on_stale=True)
    assert len(calls) == 1, f"陈旧被当成瞬时故障重试了 {len(calls)} 次"


def test_stale_not_cached_even_when_raising(monkeypatch):
    """抛异常路径同样不许写缓存，否则补抓清缓存前拿回的还是那份陈旧数据。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-28T15:59:59"))
    with pytest.raises(co.CBOEStaleVintageError):
        co._fetch_cboe_payload("TMO", 15, raise_on_stale=True)
    assert "TMO" not in co._payload_cache


def test_fresh_payload_unaffected(monkeypatch):
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-31")
    _patch_net(monkeypatch, _raw("2026-08-31T15:59:59"))
    d = co._fetch_cboe_payload("NVDA", 15, raise_on_stale=True)
    assert d["current_price"] == 100.0
    assert "NVDA" in co._payload_cache


def test_two_gates_share_one_except_clause():
    """两道闸门口径不同但对调用方是同一件事，必须一个 except 全接住。"""
    assert issubclass(csf.StaleVintageError, co.CBOEStaleVintageError)


# ══════════════════════════════════════════════════════════════════
# 2. 陈旧标的进 stale 列表 → 补抓认得它
# ══════════════════════════════════════════════════════════════════

def _stub_market(monkeypatch):
    """挡掉 main() 里的大盘段：真跑要联网 CBOE + yfinance，与本测试无关。"""
    fake_cboe = types.ModuleType("cboe_fetcher")
    fake_cboe.CBOEDailyFetcher = lambda: types.SimpleNamespace(
        fetch_all=lambda: {"vix_term": {"source": "live"}})
    fake_fg = types.ModuleType("fear_greed")
    fake_fg.get_fear_greed = lambda: {"value": 50}
    monkeypatch.setitem(sys.modules, "cboe_fetcher", fake_cboe)
    monkeypatch.setitem(sys.modules, "fear_greed", fake_fg)


def _snap(ticker, lag_suspect=False):
    return {"ticker": ticker, "schema_version": csf.SCHEMA_VERSION,
            "fetched_at_utc": "2026-08-31T21:00:00+00:00",
            "price_at_fetch": 622.18, "price_source": "cboe_close",
            "last_trade_time_et": "2026-08-31T15:59:59",
            "vintage_date": "2026-08-31", "vintage_status": "ok",
            "prev_day_close": 620.0, "last_trade_lag_min": 0.0,
            "intraday_freeze_suspect": lag_suspect,
            "chain": {"calls": []}, "iv_term_structure": None, "full_chain_oi": None}


def test_stale_ticker_lands_in_stale_list_and_retry_recovers(monkeypatch, tmp_path):
    """核心回归：TMO 必须进 vintage_stale，补抓必须跑，滞后型能补回。"""
    _stub_market(monkeypatch)
    attempts = {"TMO": 0}

    def _fake_fetch(ticker, business_date):
        if ticker == "TMO":
            attempts["TMO"] += 1
            if attempts["TMO"] == 1:      # 首轮：CDN 还没刷新
                raise co.CBOEStaleVintageError(
                    "TMO", "2026-08-28", business_date, "2026-08-28T15:59:59")
            return _snap("TMO")           # 补抓时已自愈（TMUS 实测约 20 分钟）
        return _snap(ticker)

    monkeypatch.setattr(csf, "_fetch_one_ticker", _fake_fetch)
    monkeypatch.setattr(sys, "argv",
                        ["x", "--out", str(tmp_path), "--tickers", "JNJ,TMO"])
    csf.main()

    day = csf._business_date()
    man = json.loads((tmp_path / day / "manifest.json").read_text())
    assert attempts["TMO"] == 2, "补抓没有触发 —— 陈旧标的没进 stale 列表"
    assert man["tickers_ok"] == 2 and "TMO" in man["ok"]
    assert man["failed"] == {}
    assert man["vintage_stale"] == []      # 补回后应从 stale 移除


def test_persistently_stale_reported_as_stale_not_network_error(monkeypatch, tmp_path):
    """CDN 卡死（补抓也救不回）时，manifest 必须说是陈旧，而不是网络/403。"""
    _stub_market(monkeypatch)

    def _fake_fetch(ticker, business_date):
        if ticker == "TMO":
            raise co.CBOEStaleVintageError(
                "TMO", "2026-08-28", business_date, "2026-08-28T15:59:59")
        return _snap(ticker)

    monkeypatch.setattr(csf, "_fetch_one_ticker", _fake_fetch)
    monkeypatch.setattr(sys, "argv",
                        ["x", "--out", str(tmp_path), "--tickers", "JNJ,TMO"])
    csf.main()

    man = json.loads((tmp_path / csf._business_date() / "manifest.json").read_text())
    assert man["vintage_stale"] == ["TMO"]
    assert "CBOEStaleVintageError" in man["failed"]["TMO"]
    assert "payload 为空" not in man["failed"]["TMO"], "又退回误导性的网络错描述了"


# ══════════════════════════════════════════════════════════════════
# 3. 盘中冻结告警
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


def test_cross_day_left_to_vintage_gate(monkeypatch):
    """跨日归 vintage 闸门管，冻结检测不重复计数。"""
    _at(monkeypatch, "2026-08-31T17:02:00")
    assert csf._freeze_lag_min("2026-08-28T15:59:59", "2026-08-31") is None


def test_unparseable_input_is_silent(monkeypatch):
    _at(monkeypatch, "2026-08-31T17:02:00")
    for bad in (None, "", "not-a-time", 12345):
        assert csf._freeze_lag_min(bad, "2026-08-31") is None


def test_frozen_ticker_surfaces_in_manifest(monkeypatch, tmp_path):
    """标记要一路走到 manifest —— 消费端只读 manifest，不会去翻 30 个文件。"""
    _stub_market(monkeypatch)
    monkeypatch.setattr(csf, "_fetch_one_ticker",
                        lambda t, d: _snap(t, lag_suspect=(t == "TMO")))
    monkeypatch.setattr(sys, "argv",
                        ["x", "--out", str(tmp_path), "--tickers", "JNJ,TMO"])
    csf.main()

    man = json.loads((tmp_path / csf._business_date() / "manifest.json").read_text())
    assert man["vintage_intraday_freeze_suspect"] == ["TMO"]
    assert man["tickers_ok"] == 2, "冻结是告警，不该拒绝落盘"
