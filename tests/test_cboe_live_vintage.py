"""实时扫描路径的 CBOE 陈旧文件防线守卫（v0.45.39）。

背景（实测，非推测）
--------------------
CBOE 的 CDN 对某些符号**不重新生成文件**：HTTP 200、字段齐全、看不出异常，
但整份 JSON 停在旧日期。2026-08-26 实测：

    TMO   CDN 生成于 08-25 00:12 UTC（44.5h 前），current_price=628.74 = 8/24 收盘
    TMUS  滞后约 20 分钟后自愈
    真实 8/26 收盘：TMO 633.96 / TMUS 179.61

历史对账：pheromone.db 877 条可对账样本里 13 条（1.5%）的 price_at_predict
精确等于更早某日的收盘，2026-07-24 一天中了 8 只。

守什么
------
1. 陈旧 payload 必须被拦下（→ data_pipeline 落到 YFinanceSource）
2. **陈旧 payload 绝不许进缓存** —— 进了就等于在进程内再保鲜 120 秒
3. 判据随时间变化：开盘前拿到上一交易日数据是正常的，不许误杀
4. fail-open：交易日历不可用时跳过校验，绝不因日历挂了把全部标的打成陈旧
   （那会连锁压垮 yfinance —— 7/23 限流雪崩同源）
"""

import datetime as _dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cboe_options as co  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    co.invalidate_payload_cache()
    co._vintage_stats.update({"checked": 0, "stale": 0})
    yield
    co.invalidate_payload_cache()


def _raw(last_trade="2026-08-26T15:59:59"):
    return json.dumps({"timestamp": "2026-08-26 20:41:33", "symbol": "X",
                       "data": {"options": [{"option": "X260828C00100000"}],
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
# _expected_vintage_date —— 判据随时间变化
# ══════════════════════════════════════════════════════════════════

class _FakeDT:
    """只替换 now()，其余委托给真 datetime（fromisoformat 还要用）。"""
    target = None

    @classmethod
    def now(cls, tz=None):
        return cls.target.astimezone(tz) if tz else cls.target

    @staticmethod
    def fromisoformat(s):
        return _dt.datetime.fromisoformat(s)


def _at(monkeypatch, iso_et):
    _FakeDT.target = _dt.datetime.fromisoformat(iso_et).replace(tzinfo=co._ET_TZ)
    monkeypatch.setattr(co, "datetime", _FakeDT)


def test_after_open_expects_today(monkeypatch):
    _at(monkeypatch, "2026-08-26T17:00:00")      # 周三 17:00 ET = 收盘后
    assert co._expected_vintage_date() == "2026-08-26"


def test_before_open_expects_previous_trading_day(monkeypatch):
    """盘前拿到上一交易日收盘天经地义 —— 误杀这里就等于把整个盘前路径打死。"""
    _at(monkeypatch, "2026-08-26T05:28:00")      # 08:28 ET 之前
    assert co._expected_vintage_date() == "2026-08-25"


def test_weekend_expects_last_friday(monkeypatch):
    _at(monkeypatch, "2026-08-29T12:00:00")      # 周六
    assert co._expected_vintage_date() == "2026-08-28"


def test_calendar_failure_fails_open(monkeypatch):
    """日历不可用 → None → 调用方跳过校验。绝不能把全部标的打成陈旧。"""
    import builtins
    real = builtins.__import__

    def _boom(name, *a, **k):
        if name == "is_trading_day":
            raise ImportError("simulated")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert co._expected_vintage_date() is None
    assert co._payload_is_stale("X", {"last_trade_time": "1999-01-04T16:00:00"}) is False


# ══════════════════════════════════════════════════════════════════
# _fetch_cboe_payload —— 拦截与缓存
# ══════════════════════════════════════════════════════════════════

def test_stale_payload_rejected(monkeypatch):
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    _patch_net(monkeypatch, _raw("2026-08-24T15:59:59"))     # TMO 实测场景
    assert co._fetch_cboe_payload("TMO", 15) is None
    assert co._vintage_stats["stale"] == 1


def test_stale_payload_not_cached(monkeypatch):
    """陈旧 payload 进了缓存 = 在进程内又保鲜 120 秒，比不拦还糟。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    _patch_net(monkeypatch, _raw("2026-08-24T15:59:59"))
    co._fetch_cboe_payload("TMO", 15)
    assert "TMO" not in co._payload_cache, "陈旧 payload 被写进了缓存"


def test_fresh_payload_passes_and_caches(monkeypatch):
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    _patch_net(monkeypatch, _raw("2026-08-26T15:59:59"))
    d = co._fetch_cboe_payload("NVDA", 15)
    assert d["current_price"] == 100.0
    assert "NVDA" in co._payload_cache
    assert co._vintage_stats["stale"] == 0


def test_newer_than_expected_not_rejected(monkeypatch):
    """判据是 `早于`应有日期才拦。比预期新（盘中先于日历翻页）不该误杀。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-25")
    _patch_net(monkeypatch, _raw("2026-08-26T15:59:59"))
    assert co._fetch_cboe_payload("NVDA", 15) is not None


def test_invalidate_cache(monkeypatch):
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    _patch_net(monkeypatch, _raw())
    co._fetch_cboe_payload("NVDA", 15)
    assert "NVDA" in co._payload_cache
    co.invalidate_payload_cache("NVDA")
    assert "NVDA" not in co._payload_cache


def test_missing_timestamp_does_not_reject(monkeypatch):
    """证不出陈旧就不拦 —— 校验只做减法，不许因为字段缺失误杀主源。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    body = json.loads(_raw())
    body["data"].pop("last_trade_time")
    _patch_net(monkeypatch, json.dumps(body).encode())
    assert co._fetch_cboe_payload("NVDA", 15) is not None


def test_retry_needs_cache_invalidation_when_checks_disagree(monkeypatch):
    """两道 vintage 校验判据不同，分歧处**必须**清缓存才能补抓。

    - `cboe_options._payload_is_stale` 比的是「本时点应有日期」（随时间变化）
    - `cloud_snapshot_fetch._vintage` 比的是「目录日期」（固定为业务日）

    收盘后两者一致，陈旧 payload 被前者拦下、根本不入缓存。
    但**盘前触发**时前者认为「上一交易日数据属正常」→ 放行并**写入缓存**，
    后者却按业务日判它陈旧。此刻缓存里躺着那份陈旧 payload，
    不清缓存就再也补抓不回来（补抓拿回的还是它）。
    """
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-25")  # 盘前
    bodies = [_raw("2026-08-25T16:00:00"), _raw("2026-08-26T15:59:59")]
    monkeypatch.setattr(co.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(bodies[min(len(bodies) - 1, _n["i"])]))
    _n = {"i": 0}

    first = co._fetch_cboe_payload("TMUS", 15)
    assert first["last_trade_time"] == "2026-08-25T16:00:00"
    assert "TMUS" in co._payload_cache, "构造失效：这份 payload 本应被缓存"

    _n["i"] = 1                                  # CDN 此时已刷新
    again = co._fetch_cboe_payload("TMUS", 15)
    assert again["last_trade_time"] == "2026-08-25T16:00:00", \
        "构造失效：不清缓存就该拿回旧的"

    co.invalidate_payload_cache("TMUS")
    fresh = co._fetch_cboe_payload("TMUS", 15)
    assert fresh["last_trade_time"] == "2026-08-26T15:59:59", \
        "清了缓存仍拿回旧数据 —— invalidate_payload_cache 失效"


# ══════════════════════════════════════════════════════════════════
# on_stale —— 陈旧信号透传（v0.45.91）
# ══════════════════════════════════════════════════════════════════
# v0.45.39 只做到「拦下」，陈旧与网络失败共用 `return None` 一个出口。
# 实时路径无所谓（都降级 yfinance），云端快照却要靠这个分类决定补不补抓，
# 于是补抓 pass 对 CDN 陈旧从未触发过：08-28 BILI / 08-31 TMO / 09-01 BILI
# 三次都以泛化 `RuntimeError: CBOE payload 为空` 落进普通失败。

def test_on_stale_default_returns_none(monkeypatch):
    """默认口径必须与 v0.45.39 逐字节一致 —— 实时路径不许因本次改动改行为。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    _patch_net(monkeypatch, _raw("2026-08-24T15:59:59"))
    assert co._fetch_cboe_payload("TMO", 15) is None


def test_on_stale_raise_surfaces_dates(monkeypatch):
    """`on_stale="raise"` → 抛异常，且带上两个日期供调用方审计。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    _patch_net(monkeypatch, _raw("2026-08-24T15:59:59"))
    with pytest.raises(co.CboeStaleVintageError) as ei:
        co._fetch_cboe_payload("TMO", 15, on_stale="raise")
    assert ei.value.ticker == "TMO"
    assert ei.value.vintage_date == "2026-08-24"
    assert ei.value.expected_date == "2026-08-26"


def test_on_stale_raise_not_swallowed_by_retry_loop(monkeypatch):
    """回归：抛点在重试 `try` 内，兜底 `except Exception` 会把它吞成 None。

    这是本次改动最容易写错的地方——吞掉后行为与改动前完全一样，
    参数形同虚设，而所有只断言「返回 None」的旧测试仍然全绿。
    顺带守住「不重试」：同一份 CDN 文件重拉几次还是同一天的。
    """
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    hits = {"n": 0}

    def _count(*a, **k):
        hits["n"] += 1
        return _Resp(_raw("2026-08-24T15:59:59"))

    monkeypatch.setattr(co.urllib.request, "urlopen", _count)
    with pytest.raises(co.CboeStaleVintageError):
        co._fetch_cboe_payload("TMO", 15, on_stale="raise")
    assert hits["n"] == 1, f"陈旧不该重试，实际拉了 {hits['n']} 次"


def test_on_stale_raise_still_does_not_cache(monkeypatch):
    """两种口径都不许把陈旧 payload 写进缓存（v0.45.39 的不变式）。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    _patch_net(monkeypatch, _raw("2026-08-24T15:59:59"))
    with pytest.raises(co.CboeStaleVintageError):
        co._fetch_cboe_payload("TMO", 15, on_stale="raise")
    assert "TMO" not in co._payload_cache


def test_on_stale_raise_leaves_other_failures_alone(monkeypatch):
    """只有陈旧改走异常；空期权链等真失败仍旧 return None，不许被顺手改掉。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: "2026-08-26")
    body = json.loads(_raw())
    body["data"]["options"] = []
    _patch_net(monkeypatch, json.dumps(body).encode())
    assert co._fetch_cboe_payload("X", 15, on_stale="raise") is None


def test_on_stale_raise_fail_open_when_calendar_down(monkeypatch):
    """日历挂了仍旧 fail-open —— 不许因为改了出口就把 30 只全打成陈旧。"""
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: None)
    _patch_net(monkeypatch, _raw("1999-01-04T16:00:00"))
    assert co._fetch_cboe_payload("X", 15, on_stale="raise") is not None


# ══════════════════════════════════════════════════════════════════
# ET 时钟：合并 v0.45.46 时统一到 ZoneInfo（v0.45.41）
# ══════════════════════════════════════════════════════════════════

def _freeze_utc(monkeypatch, utc_dt):
    """冻结 `co.datetime.now`，语义与真 datetime 一致：now(tz) = 该瞬间在 tz 下的表示。

    必须打在 `co.datetime` 上而不是断言 `_ET_TZ` —— 要测的是 `_et_now()`
    这个函数，不是它用到的常量。（初版就栽在这：断言常量，变异 `_et_now`
    后测试全绿。）
    """
    class _FrozenDT:
        @staticmethod
        def now(tz=None):
            return utc_dt.astimezone(tz) if tz else utc_dt.replace(tzinfo=None)

        @staticmethod
        def fromisoformat(x):
            return _dt.datetime.fromisoformat(x)
    monkeypatch.setattr(co, "datetime", _FrozenDT)


@pytest.mark.parametrize("utc_moment,expect_et", [
    ("2026-03-04T13:30:00+00:00", "08:30"),   # EST（UTC−5）；近似算法会给 09:30
    ("2026-03-04T14:30:00+00:00", "09:30"),
    ("2026-07-01T13:30:00+00:00", "09:30"),   # EDT（UTC−4），两种算法一致
    ("2026-11-10T13:30:00+00:00", "08:30"),   # 已回 EST；近似算法会给 09:30
])
def test_et_now_uses_real_dst(monkeypatch, utc_moment, expect_et):
    """`_et_now` 必须走真 DST，不能用「3–11 月一律 −4」的近似。

    2026 年美东夏令时 3/8 起、11/1 止 —— 近似算法在 3/1–3/7 与 11/2–11/30
    （约 37 天）算**早一小时**。后果正是 v0.45.46 要修的那一类：
    真实 08:30 ET（盘前）被算成 09:30 → `is_market_open` 判为盘中
    → 取 `current_price`（盘前价）而不是 `close`。
    """
    _freeze_utc(monkeypatch, _dt.datetime.fromisoformat(utc_moment))
    assert co._et_now().strftime("%H:%M") == expect_et


def test_premarket_in_est_not_reported_open(monkeypatch):
    """端到端：3 月初盘前时刻，`is_market_open()` 不传参时也必须判为收盘。"""
    _freeze_utc(monkeypatch, _dt.datetime.fromisoformat("2026-03-04T13:30:00+00:00"))
    assert co.is_market_open() is False, \
        "08:30 ET 是盘前，判成盘中会导致取 current_price（盘前价）而非 close"


def test_on_stale_rejects_typo():
    """拼错不许静默退回旧行为——那正是 v0.45.91 修的那种失败形态。"""
    with pytest.raises(ValueError, match="on_stale"):
        co._fetch_cboe_payload("X", 15, on_stale="Raise")
