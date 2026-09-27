"""Buzz 情绪动量改读归档（v0.45.340，buzz_v1 阶段 1 / 维度 IC 协议 §13.2 锚点）的守卫。

阶段 1 要的是**可复现**：同一业务日、任何时刻重跑，动量调整逐位相同；冻结评分器（阶段 2）读同一张归档表，
所以两边天然一致。这里逐条守：

- 时间基准是 `as_of`（扫描业务日期），不是墙上时钟——把「今天」拨到 2030 年，结果逐位不变。
- 历史只来自 `signal_archive` 的 `sentiment.pct`；取「≤ as_of−N」的最近一行，且不早于 as_of−N−4 天（上限）。
- 读不到归档 ⇒ `history_source="unavailable"` + warning，且**不凭空建库**；表在但没有够近的历史 ⇒ 正常的无历史。
- 扫描路径真的把业务日期传到了 Buzz：报告 → `prefetch_shared_data(target_date=self.date_str)` →
  `inject_prefetched` → `BuzzBeeWhisper._target_date` → `_get_sentiment_momentum(as_of=…)`。
- 通道值全精度入档（冻结评分器要逐位重算）。

全部用 `tmp_path` 里的合成归档或 conftest 沙箱，零外部依赖、不需要 skip。
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import logging
import sqlite3
from pathlib import Path

import pytest

import signal_archive
from swarm_agents import sentiment as S

_ROOT = Path(__file__).resolve().parent.parent
D0 = "2026-10-14"          # 周三


def _d(days: int) -> str:
    return (dt.date.fromisoformat(D0) + dt.timedelta(days=days)).isoformat()


def _archive(tmp_path, rows, name="p.db"):
    """rows: [(date, ticker, pct)] → 真实 schema 的 signal_archive 表（经 `ensure_schema`，不手抄 DDL）。"""
    db = tmp_path / name
    signal_archive.ensure_schema(db)
    with sqlite3.connect(db) as c:
        c.executemany(f"INSERT INTO {signal_archive.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)",
                      [(d, t, "sentiment.pct", float(v)) for d, t, v in rows])
        # 干扰项：别的信号、别的标的，必须不被读到
        c.execute(f"INSERT INTO {signal_archive.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)",
                  (_d(-3), "AAA", "price.momentum_5d", 99.0))
        c.execute(f"INSERT INTO {signal_archive.TABLE} (date,ticker,signal,value) VALUES (?,?,?,?)",
                  (_d(-3), "ZZZ", "sentiment.pct", 1.0))
    return db


def _mom(db, pct=60, ticker="AAA", as_of=D0):
    return S._get_sentiment_momentum(ticker, pct, as_of=as_of, db_path=db)


# ── 1. 回看语义 ──────────────────────────────────────────────────────────────

class TestLookback:
    def test_three_day_delta_drives_the_adjustment(self, tmp_path):
        r = _mom(_archive(tmp_path, [(_d(-3), "AAA", 40)]), pct=60)
        assert r["delta_3d"] == 20 and r["momentum_regime"] == "surging" and r["momentum_score_adj"] == 0.5
        assert r["ref_dates"]["delta_3d"] == _d(-3)
        assert r["as_of"] == D0 and r["as_of_source"] == "scan" and r["history_source"] == "signal_archive"

    @pytest.mark.parametrize("d3,regime,adj", [
        (16, "surging", 0.5), (15, "rising", 0.2), (6, "rising", 0.2), (5, "stable", 0.0),
        (-5, "stable", 0.0), (-6, "declining", -0.2), (-15, "declining", -0.2), (-16, "crashing", -0.5)])
    def test_thresholds_unchanged(self, tmp_path, d3, regime, adj):
        """阈值与分档是冻结层的一部分，本版不改（只改时间基准与来源）。"""
        r = _mom(_archive(tmp_path, [(_d(-3), "AAA", 50)]), pct=50 + d3)
        assert (r["momentum_regime"], r["momentum_score_adj"]) == (regime, adj)

    def test_takes_the_latest_row_not_after_the_cutoff(self, tmp_path):
        """3 日回看：d−2 太近（不许用），d−4 与 d−5 都合格 ⇒ 取较近的 d−4。"""
        r = _mom(_archive(tmp_path, [(_d(-2), "AAA", 10), (_d(-4), "AAA", 45), (_d(-5), "AAA", 30)]), pct=60)
        assert r["ref_dates"]["delta_3d"] == _d(-4) and r["delta_3d"] == 15

    def test_same_day_row_is_never_used(self, tmp_path):
        """归档里已经有当天那一行（重跑发生在归档写入之后）——任何回看都不许读到它。"""
        r = _mom(_archive(tmp_path, [(D0, "AAA", 99)]), pct=60)
        assert r["delta_1d"] is r["delta_3d"] is r["delta_7d"] is None

    @pytest.mark.parametrize("key,n", [("delta_1d", 1), ("delta_3d", 3), ("delta_7d", 7)])
    def test_staleness_cap_is_n_plus_4_days(self, tmp_path, key, n):
        """参照最旧 as_of−N−4（含）；再旧一天 ⇒ 该回看按无历史处理。"""
        ok = _mom(_archive(tmp_path, [(_d(-(n + 4)), "AAA", 50)], "ok.db"), pct=60)
        stale = _mom(_archive(tmp_path, [(_d(-(n + 5)), "AAA", 50)], "stale.db"), pct=60)
        assert ok[key] == 10 and ok["ref_dates"][key] == _d(-(n + 4))
        assert stale[key] is None and key not in stale["ref_dates"]

    def test_stale_three_day_reference_means_no_adjustment(self, tmp_path):
        """判别：扫描断档期「3 日动量」拿 8 天前的值来比——旧口径会给 +0.5，新口径是无历史。"""
        r = _mom(_archive(tmp_path, [(_d(-8), "AAA", 20)]), pct=60)
        assert r["delta_3d"] is None and r["momentum_regime"] == "unknown" and r["momentum_score_adj"] == 0.0

    def test_monday_three_day_lookback_reaches_friday(self, tmp_path):
        mon = "2026-10-12"
        fri = "2026-10-09"
        assert dt.date.fromisoformat(mon).weekday() == 0
        r = _mom(_archive(tmp_path, [(fri, "AAA", 50)]), pct=56, as_of=mon)
        assert r["ref_dates"]["delta_3d"] == fri and r["momentum_score_adj"] == 0.2


# ── 2. 与墙上时钟无关 ───────────────────────────────────────────────────────

class _Frozen2030(dt.date):
    @classmethod
    def today(cls):
        return cls(2030, 1, 1)


class TestNoWallClock:
    def test_result_is_identical_whatever_today_is(self, tmp_path, monkeypatch):
        db = _archive(tmp_path, [(_d(-1), "AAA", 58), (_d(-3), "AAA", 40), (_d(-7), "AAA", 70)])
        before = _mom(db, pct=60)
        monkeypatch.setattr(dt, "date", _Frozen2030)
        after = _mom(db, pct=60)
        assert before == after

    def test_without_as_of_falls_back_to_local_today_and_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dt, "date", _Frozen2030)
        r = S._get_sentiment_momentum("AAA", 60, db_path=_archive(tmp_path, []))
        assert r["as_of"] == "2030-01-01" and r["as_of_source"] == "wall_clock"

    def test_malformed_as_of_raises(self, tmp_path):
        with pytest.raises(ValueError):
            _mom(_archive(tmp_path, []), as_of="10/14/2026")

    def test_source_has_no_wall_clock_or_baseline_reads(self):
        """静态：回看函数里不许再出现 SQLite `now`、`sentiment_baseline`、`_sentiment_db_path`。"""
        src = inspect.getsource(S._get_sentiment_momentum)
        body = src.split('"""', 2)[2]          # 去掉 docstring（它在描述旧实现）
        for bad in ("'now'", "sentiment_baseline", "_sentiment_db_path", "datetime.now"):
            assert bad not in body, f"_get_sentiment_momentum 里出现了 {bad}"


# ── 3. 读不到归档要看得见 ───────────────────────────────────────────────────

class TestUnavailableIsVisible:
    def test_missing_db_is_unavailable_and_not_created(self, tmp_path, caplog):
        db = tmp_path / "absent.db"
        with caplog.at_level(logging.WARNING):
            r = _mom(db)
        assert r["history_source"] == "unavailable" and r["momentum_score_adj"] == 0.0
        assert not db.exists(), "读归档不许凭空建库（那会把「路径错了」伪装成「没有历史」）"
        assert any("读归档" in m and "失败" in m for m in caplog.messages)

    def test_db_without_archive_table_is_unavailable(self, tmp_path):
        db = tmp_path / "empty.db"
        sqlite3.connect(db).close()
        assert _mom(db)["history_source"] == "unavailable"

    def test_table_present_but_no_history_is_ordinary_no_history(self, tmp_path):
        r = _mom(_archive(tmp_path, []), ticker="NEW")
        assert r["history_source"] == "signal_archive" and r["momentum_regime"] == "unknown"
        assert r["delta_1d"] is r["delta_3d"] is r["delta_7d"] is None


# ── 4. 与归档的约定 ─────────────────────────────────────────────────────────

class TestArchiveContract:
    def test_table_and_signal_names_match_signal_archive(self):
        assert S._ARCHIVE_TABLE == signal_archive.TABLE
        assert S._ARCHIVE_PCT_SIGNAL in signal_archive.SIGNAL_EXTRACTORS

    def test_archived_pct_is_the_value_buzz_compares_against(self):
        """归档的 `sentiment.pct` 取自 Buzz 的 `details.sentiment_pct`（= int(合成值)），
        正是 Buzz 传进来的 current_pct 的同一个量——两边比的是同一件事。"""
        tr = {"agent_details": {"BuzzBeeWhisper": {"details": {"sentiment_pct": 57}}}}
        assert signal_archive.extract(tr)["sentiment.pct"] == 57.0

    def test_default_db_is_paths_db_at_call_time(self):
        from hive_logger import PATHS
        assert S._archive_db_path() == Path(PATHS.db)


# ── 5. 扫描路径：业务日期真的传到了 Buzz ───────────────────────────────────

class TestScanPassesTheBusinessDate:
    def test_daily_report_prefetches_with_its_date_str(self):
        tree = ast.parse((_ROOT / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", None)) == "prefetch_shared_data"]
        assert calls, "找不到 prefetch_shared_data 调用"
        for c in calls:
            kw = {k.arg: k.value for k in c.keywords}
            v = kw.get("target_date")
            assert isinstance(v, ast.Attribute) and v.attr == "date_str", \
                "扫描必须把业务日期 self.date_str 作为 target_date 传下去"

    def test_inject_prefetched_sets_target_date(self):
        from swarm_agents.base import inject_prefetched

        class _A:
            pass
        a = _A()
        inject_prefetched([a], {"target_date": "2026-10-14"})
        assert a._target_date == "2026-10-14"


# ── 6. BuzzBee 本身：as_of 透传 + 通道全精度 ────────────────────────────────

_ODD = {"reddit": 6.2371, "news": 5.43219, "yahoo": 3.14159, "fg": 4.56789, "mom5d": 1.2345}


@pytest.fixture
def buzz(all_agents, monkeypatch):
    """Buzz + 四个外部通道打桩成「不整齐」的值：取整了就对不上。"""
    import fear_greed
    import newsapi_client
    import reddit_sentiment
    import yahoo_trending
    from swarm_agents import cache as _cache
    monkeypatch.setattr(reddit_sentiment, "get_reddit_sentiment", lambda t: {
        "sentiment_score": _ODD["reddit"], "reddit_buzz": "hot", "mentions": 10, "rank": 7})
    monkeypatch.setattr(newsapi_client, "get_ticker_news", lambda t, max_articles=10: {
        "is_real_data": True, "total_articles": 5, "sentiment_score": _ODD["news"],
        "dominant_theme": "t", "articles": [], "source": "stub"})
    monkeypatch.setattr(yahoo_trending, "get_ticker_attention", lambda t: {
        "is_real_data": True, "attention_score": _ODD["yahoo"], "description": "d"})
    monkeypatch.setattr(fear_greed, "get_fear_greed", lambda: {
        "is_real_data": True, "sentiment_score": _ODD["fg"], "value": 46, "classification": "Fear",
        "source": "cnn"})
    stock = {"price": 100.0, "momentum_5d": _ODD["mom5d"], "volume_ratio": 1.2, "volatility_20d": 30.0}
    monkeypatch.setattr(_cache, "_fetch_stock_data", lambda t, target_date=None: dict(stock))
    b = all_agents["buzz"]
    b._prefetched_stock = {}
    return b


class TestBuzzBee:
    def test_as_of_is_the_injected_scan_date(self, buzz, monkeypatch):
        seen = {}
        real = S._get_sentiment_momentum

        def spy(ticker, pct, as_of=None, db_path=None):
            seen["as_of"] = as_of
            return real(ticker, pct, as_of=as_of, db_path=db_path)
        import swarm_agents.buzz_bee as bb
        monkeypatch.setattr(bb, "_get_sentiment_momentum", spy)
        buzz._target_date = D0
        r = buzz.analyze("NVDA")
        assert "error" not in r, r
        assert seen["as_of"] == D0
        sm = r["details"]["sentiment_momentum"]
        assert sm["as_of"] == D0 and sm["as_of_source"] == "scan"

    def test_scan_momentum_reads_the_archive(self, buzz, monkeypatch, tmp_path):
        """端到端：沙箱归档里 3 天前的 sentiment.pct ⇒ Buzz 的 delta_3d 与参照日。"""
        db = _archive(tmp_path, [(_d(-3), "NVDA", 10)])
        monkeypatch.setattr(S, "_archive_db_path", lambda: db)
        buzz._target_date = D0
        r = buzz.analyze("NVDA")
        sm = r["details"]["sentiment_momentum"]
        assert sm["ref_dates"]["delta_3d"] == _d(-3)
        assert sm["delta_3d"] == r["details"]["sentiment_pct"] - 10

    def test_components_are_full_precision(self, buzz):
        buzz._target_date = D0
        c = buzz.analyze("NVDA")["details"]["components"]
        assert c["momentum_signal"] == pytest.approx((_ODD["mom5d"] + 10) / 20 * 100, abs=1e-12)
        assert c["reddit_signal"] == pytest.approx(_ODD["reddit"] * 10, abs=1e-12)
        assert c["news_signal"] == pytest.approx(_ODD["news"] * 10, abs=1e-12)
        assert c["yahoo_signal"] == pytest.approx(_ODD["yahoo"] * 10, abs=1e-12)
        assert c["fear_greed_signal"] == pytest.approx(_ODD["fg"] * 10, abs=1e-12)

    def test_score_is_reproducible_from_the_recorded_details(self, buzz):
        """阶段 2 的前提在阶段 1 就要成立：由 details 里的通道 + 两项调整能**逐位**重算 score。"""
        from swarm_agents._config import _AS
        from swarm_agents.utils import clamp_score_cfg
        buzz._target_date = D0
        r = buzz.analyze("NVDA")
        det, w = r["details"], _AS.get("buzz_weights", {})
        c = det["components"]
        comp = (c["momentum_signal"] * w.get("momentum", 0.20) + c["volume_signal"] * w.get("volume", 0.10)
                + c["volatility_signal"] * w.get("volatility", 0.05) + c["reddit_signal"] * w.get("reddit", 0.25)
                + c["news_signal"] * w.get("news", 0.25) + c["yahoo_signal"] * w.get("yahoo", 0.05)
                + c["fear_greed_signal"] * w.get("fear_greed", 0.10))
        s = clamp_score_cfg(comp / 10.0) + det["sentiment_momentum"]["momentum_score_adj"]
        s = clamp_score_cfg(s + det["sentiment_divergence"]["score_adj"])
        assert round(s, 2) == r["score"]
        assert det["sentiment_pct"] == int(comp)
