"""补跑时链若取自目标日的云端快照，数据就属于目标日（v0.45.497）。

背景：10-12 扫描全程 TLS EOF（v0.45.496），期权快照 0 份；当天的 CBOE 链在 `origin/cloud-snapshots` 里。
次日开盘后补跑（此刻会话 = 10-13 ≠ 目标日 10-12）时，`analyze` 原本按「补跑 ⇒ 链是运行时现拉的」处理：
  · IV 观测按 `_snap_session`（10-13）记进 `iv_history` 索引 —— 10-12 的 IV 被记到 10-13 名下；
  · 当日实拉 IV 刷进「最后有效 IV」缓存 —— 那是给**此刻**降级用的，塞进的却是昨天的值；
  · 快照标 `_options_as_of_mismatch: True` —— 链明明就是目标日的。
三条只在「补跑 + 云端快照」时成立；实时链（含链缓存里的旧实时链）仍按原口径（下方两组对照）。

全部离线：链来自内存里的快照供给器 / 桩，yfinance、CBOE payload 都钉死。
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import cboe_options as C
import options_analyzer as oa
from options_analyzer import OptionsAgent, OptionsAnalyzer

_NOW = datetime(2026, 10, 13, 17, 30, tzinfo=ZoneInfo("America/New_York"))   # 此刻会话 = 10-13
_SESSION = "2026-10-13"
_TARGET = "2026-10-12"


def _chain():
    calls = [{"strike": k, "openInterest": 3000, "impliedVolatility": 0.34, "gamma": 0.05,
              "volume": 50} for k in (280.0, 300.0, 320.0)]
    puts = [{"strike": k, "openInterest": 3000, "impliedVolatility": 0.38, "gamma": 0.04,
             "volume": 50} for k in (280.0, 300.0, 320.0)]
    return {"calls": calls, "puts": puts, "expirations": ["2026-11-20"], "source": "real",
            "_source": "cboe"}


@pytest.fixture
def agent(monkeypatch, tmp_path, stub_cboe_payload, stub_yfinance):
    import market_intelligence
    monkeypatch.setattr(OptionsAnalyzer, "_iv_term_points_yfinance",
                        staticmethod(lambda ticker, stock_price: ([], ["offline"])))
    monkeypatch.setattr(market_intelligence, "calculate_iv_rv_spread", lambda *_a, **_k: {})
    # conftest 全局关了快照写入；cache_dir 已指向 tmp_path，打开才能断言快照内容
    monkeypatch.delenv("OPTIONS_SNAPSHOT_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_HIVE_TARGET_DATE", _TARGET)
    monkeypatch.setattr(oa, "_snapshot_now", lambda: _NOW)
    a = OptionsAgent()
    monkeypatch.setattr(a.fetcher, "cache_dir", str(tmp_path))
    monkeypatch.setattr(a.fetcher, "fetch_historical_hv", lambda t: [0.25 + i * 0.02 for i in range(20)])
    a.saved_last_valid = []
    monkeypatch.setattr(a.fetcher, "_save_last_valid_iv", lambda t, iv: a.saved_last_valid.append((t, iv)))
    monkeypatch.setattr(a.fetcher, "_read_last_valid_iv", lambda t: None)
    oa.reset_snapshot_slot_stats()
    yield a
    C.set_snapshot_provider(None)
    oa.reset_snapshot_slot_stats()


# analyze 内部判开市用的墙钟（与 `_snapshot_now` 不是一只钟）。两只都钉，读几只钉几只。
_MARKET_OPEN_WALL = datetime(2026, 10, 13, 11, 0, tzinfo=ZoneInfo("America/New_York"))
_MARKET_CLOSED_WALL = datetime(2026, 10, 13, 17, 30, tzinfo=ZoneInfo("America/New_York"))


def _pin_wall_clock(monkeypatch, wall):
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall.astimezone(tz) if tz else wall.astimezone().replace(tzinfo=None)
    monkeypatch.setattr(oa, "datetime", _Fixed)


def _install_cloud(vintage):
    snap = {"vintage_date": vintage, "chain": _chain(), "price_at_fetch": 300.0}
    C.set_snapshot_provider(lambda t: snap)
    return snap


def _index_dates(tmp_path, ticker="NVDA"):
    p = tmp_path / f"iv_history_{ticker}.jsonl"
    return [json.loads(ln)["date"] for ln in p.read_text().splitlines()] if p.exists() else []


def _only_snapshot(tmp_path):
    files = list(tmp_path.glob("options_snapshot_*.json"))
    assert len(files) == 1, files
    return files[0].name, json.loads(files[0].read_text())


class TestCloudChainOfTargetDay:
    def test_iv_observation_goes_to_target_day(self, agent, tmp_path):
        """变异：append_observation 改回 `_snap_session` ⇒ 记成 10-13，红。"""
        _install_cloud(_TARGET)
        r = agent.analyze("NVDA", stock_price=300.0)
        assert r["iv_raw_observed"] is not None
        assert _index_dates(tmp_path) == [_TARGET]

    @pytest.mark.parametrize("wall", [_MARKET_OPEN_WALL, _MARKET_CLOSED_WALL], ids=["open", "closed"])
    def test_does_not_seed_this_moments_last_valid_cache(self, agent, tmp_path, monkeypatch, wall):
        """变异：删掉任一处 `if not _chain_is_target` ⇒ 对应那条参数红。开市 / 收市分支由 analyze 里
        `datetime.now(timezone.utc)` 决定 —— 不钉墙钟的话，这条测试在一天里总有一段时间测不到其中一处。"""
        _pin_wall_clock(monkeypatch, wall)
        _install_cloud(_TARGET)
        agent.analyze("NVDA", stock_price=300.0)
        assert agent.saved_last_valid == []

    def test_snapshot_is_labelled_cloud_not_mismatch(self, agent, tmp_path):
        """变异：标签分支删掉 ⇒ 回到 mismatch=True，红。"""
        _install_cloud(_TARGET)
        agent.analyze("NVDA", stock_price=300.0)
        name, snap = _only_snapshot(tmp_path)
        assert name == f"options_snapshot_NVDA_{_TARGET}_backfilled-{_SESSION}.json"
        assert snap["_options_as_of_mismatch"] is False
        assert snap["_options_source"] == "cloud_snapshot"
        assert snap["_options_target_date"] == _TARGET

    def test_chain_is_tagged_without_mutating_provider_copy(self):
        """`fetch_cboe_chain` 的快照分支打标是浅拷贝：供给器缓存里的原件不被改（同进程别的读者还在读它）。"""
        snap = _install_cloud(_TARGET)
        try:
            chain = C.fetch_cboe_chain("NVDA")
            assert chain["_from_snapshot"] is True and chain["_snapshot_vintage"] == _TARGET
            assert "_from_snapshot" not in snap["chain"]
        finally:
            C.set_snapshot_provider(None)


class TestControlsKeepOldSemantics:
    @pytest.mark.parametrize("wall", [_MARKET_OPEN_WALL, _MARKET_CLOSED_WALL], ids=["open", "closed"])
    def test_cloud_chain_of_another_day_is_still_this_moment(self, agent, tmp_path, monkeypatch, wall):
        """快照的场次与目标日对不上 ⇒ 不认作目标日（只认「就是那一场」，不认「来自云端」）。
        两种墙钟各跑一次：证明上面那组「不刷缓存」在两条分支上都是被守卫挡住的，不是分支本身不刷。"""
        _pin_wall_clock(monkeypatch, wall)
        _install_cloud("2026-10-09")
        agent.analyze("NVDA", stock_price=300.0)
        assert _index_dates(tmp_path) == [_SESSION]
        assert agent.saved_last_valid, "非目标日的链：缓存照旧刷新"
        _, snap = _only_snapshot(tmp_path)
        assert snap["_options_as_of_mismatch"] is True
        assert "_options_source" not in snap

    def test_live_chain_during_backfill_keeps_mismatch(self, agent, tmp_path, monkeypatch):
        """没有供给器（实时链 / 链缓存）⇒ 数据属于此刻会话：索引记 10-13、标 mismatch。
        变异：判据改成「设了目标日就算目标日」⇒ 本条红。"""
        monkeypatch.setattr(agent.fetcher, "fetch_options_chain", lambda t: _chain())
        agent.analyze("NVDA", stock_price=300.0)
        assert _index_dates(tmp_path) == [_SESSION]
        assert agent.saved_last_valid
        _, snap = _only_snapshot(tmp_path)
        assert snap["_options_as_of_mismatch"] is True
