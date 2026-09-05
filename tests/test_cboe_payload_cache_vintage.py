"""CBOE payload 缓存按业务日判新鲜（v0.45.123）

固化的事实：`_CACHE_TTL = 120s` 短于单只标的的流水线（~330s），注释里的
「共享一次下载」从未成立——2026-09-04 实测 29 只标的抓了 83 次全链 JSON。

这里守四条：
  1. 同一业务日内，缓存条目**不因为过了 120s 就失效**（这是改动的全部意义）
  2. 业务日翻页（09:30 ET）→ 旧 payload 自动失效、被弃、重抓，并计入 evicted
  3. 年龄上限：vintage 相符也不许端一整天（长驻进程盘中价）
  4. fail-open：日历不可用 / payload 无 vintage → 退回旧的 120s 规则，行为与改动前一致
"""

import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cboe_options as co

DAY = "2026-09-04"
NEXT = "2026-09-05"


def _payload(last_trade=f"{DAY}T15:59:59", price=100.0):
    return {"options": [{"option": "X"}], "current_price": price, "close": price,
            "last_trade_time": last_trade}


def _patch_net(monkeypatch, payload):
    body = json.dumps({"data": payload}).encode()
    calls = {"n": 0}

    def _open(req, timeout=0):
        calls["n"] += 1
        return io.BytesIO(body)
    monkeypatch.setattr(co.urllib.request, "urlopen", _open)
    return calls


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(co, "_SNAPSHOT_PROVIDER", None)
    monkeypatch.setattr(co, "_expected_vintage_date", lambda: DAY)
    co._payload_cache.clear()
    co.reset_payload_stats()
    yield
    co._payload_cache.clear()
    co.reset_payload_stats()


# ───────────────────────────────────────────── 纯函数
class TestVintageDate:
    @pytest.mark.parametrize("raw,want", [
        (f"{DAY}T15:59:59", DAY),
        (f"{DAY}T23:30:00+00:00", DAY),          # UTC 23:30 = ET 19:30 同日
        (f"{NEXT}T02:00:00+00:00", DAY),         # UTC 次日 02:00 = ET 22:00 仍是 DAY
        ("garbage", None), (None, None), (123, None),
    ])
    def test_parse(self, raw, want):
        assert co._payload_vintage_date({"last_trade_time": raw}) == want

    def test_missing_key(self):
        assert co._payload_vintage_date({}) is None and co._payload_vintage_date(None) is None


class TestCacheEntryFresh:
    def test_same_day_old_entry_is_fresh(self):
        """核心：过了 120s、甚至 50 分钟，只要还是同一个业务日就新鲜。"""
        now = time.time()
        assert co._cache_entry_fresh((now - 3000, _payload()), now)

    def test_vintage_rolled_over_is_stale(self, monkeypatch):
        monkeypatch.setattr(co, "_expected_vintage_date", lambda: NEXT)
        now = time.time()
        assert not co._cache_entry_fresh((now - 10, _payload()), now), "翻页后昨天的 payload 必须失效"

    def test_newer_than_expected_still_fresh(self, monkeypatch):
        now = time.time()
        assert co._cache_entry_fresh((now - 10, _payload(f"{NEXT}T09:35:00")), now)

    def test_max_age_caps_same_day_entries(self):
        now = time.time()
        assert not co._cache_entry_fresh((now - co._CACHE_MAX_AGE - 1, _payload()), now)
        assert co._cache_entry_fresh((now - co._CACHE_MAX_AGE + 60, _payload()), now)

    @pytest.mark.parametrize("age,fresh", [(60, True), (200, False)])
    def test_calendar_unavailable_falls_back_to_120s(self, monkeypatch, age, fresh):
        monkeypatch.setattr(co, "_expected_vintage_date", lambda: None)
        now = time.time()
        assert co._cache_entry_fresh((now - age, _payload()), now) is fresh

    @pytest.mark.parametrize("age,fresh", [(60, True), (200, False)])
    def test_payload_without_vintage_falls_back_to_120s(self, age, fresh):
        now = time.time()
        p = _payload(); p.pop("last_trade_time")
        assert co._cache_entry_fresh((now - age, p), now) is fresh


# ───────────────────────────────────────────── 走真实入口
class TestFetchPayloadUsesBusinessDayCache:
    def test_second_call_after_120s_is_a_cache_hit(self, monkeypatch):
        """改动前这里会再抓一次（83/29 的来源）。"""
        calls = _patch_net(monkeypatch, _payload())
        assert co._fetch_cboe_payload("NVDA", 5) is not None
        co._payload_cache["NVDA"] = (time.time() - 3000, co._payload_cache["NVDA"][1])   # 假装过了 50 分钟
        assert co._fetch_cboe_payload("NVDA", 5) is not None
        assert calls["n"] == 1
        assert co.payload_stats()["hits"] == 1 and co.payload_stats()["evicted"] == 0

    def test_rollover_evicts_and_refetches(self, monkeypatch):
        calls = _patch_net(monkeypatch, _payload())
        co._fetch_cboe_payload("NVDA", 5)
        # 业务日翻页，且 CDN 已有新文件
        monkeypatch.setattr(co, "_expected_vintage_date", lambda: NEXT)
        _patch_net(monkeypatch, _payload(f"{NEXT}T15:59:59"))
        d = co._fetch_cboe_payload("NVDA", 5)
        assert d["last_trade_time"].startswith(NEXT)
        assert co.payload_stats()["evicted"] == 1 and co.payload_stats()["fetches"] == 2
        assert calls["n"] == 1, "第一份 urlopen 只被调过一次；第二次走的是新 patch"

    def test_rollover_with_stale_cdn_still_degrades_not_serves_old(self, monkeypatch):
        """翻页了但 CDN 文件还没更新（TMUS 09-04 实况）：不许把昨天的缓存端出来冒充今天，
        必须重抓、判陈旧、返回 None 交给降级链，且陈旧 payload 不回写缓存。"""
        _patch_net(monkeypatch, _payload())
        co._fetch_cboe_payload("TMUS", 5)
        monkeypatch.setattr(co, "_expected_vintage_date", lambda: NEXT)
        assert co._fetch_cboe_payload("TMUS", 5) is None
        assert "TMUS" not in co._payload_cache
        assert co.payload_stats()["evicted"] == 1 and co.payload_stats()["stale"] == 1

    def test_calendar_down_keeps_old_120s_behaviour(self, monkeypatch):
        monkeypatch.setattr(co, "_expected_vintage_date", lambda: None)
        calls = _patch_net(monkeypatch, _payload())
        co._fetch_cboe_payload("NVDA", 5)
        co._payload_cache["NVDA"] = (time.time() - 200, co._payload_cache["NVDA"][1])
        co._fetch_cboe_payload("NVDA", 5)
        assert calls["n"] == 2, "日历不可用时退回 120s 规则：200s 前的条目要重抓"
