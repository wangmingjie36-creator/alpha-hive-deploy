"""`momentum_5d` 回落与主路径量同一个窗口（v0.45.469）

主路径（yfinance）`Close.iloc[-1] / Close.iloc[-5]` = 5 个点、**4 个交易日**的收益；
自攒索引回落 `price_history.momentum_5d` 此前按 ≥5 个交易日挑锚点 ⇒ 同名不同窗。
生产库回放（09-28 ~ 10-09）：锚点取 4 ⇒ 与存档 0.00pp / 符号 100%（210 对），取 5 ⇒ 1.32pp / 88%。

这里的对照不是在测试里再抄一遍 `iloc[-5]`：同一串收盘价分别喂给**真的**
`data_pipeline._fetch_history_metrics`（yfinance 打桩）与**真的** `price_history.momentum_5d`，
要求两者相等——哪一边单方面换窗口都会红。另有一条源码扫描，钉住全仓主路径公式都还是同一个窗口
（改主路径要全改、并追加世代边界；`price.momentum_5d` 在维度 IC 协议 H1 输入层里）。
"""

import re
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

import data_pipeline as dp
import price_history as ph

REPO_ROOT = Path(__file__).resolve().parent.parent

# 9/28(一) … 10/9(五)，10 个连续交易日；每天收盘都不同，窗口差一天结果就不同
_DATES = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
          "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
_CLOSES = [40.0, 41.0, 39.5, 42.0, 35.0, 43.0, 38.0, 44.0, 41.5, 37.0]


def _idx(cache_dir: Path, ticker: str, rows):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"price_history_{ticker}.jsonl").write_text(
        "".join(f'{{"date": "{d}", "close": {c}}}\n' for d, c in rows), encoding="utf-8")
    (cache_dir / f".price_index_migrated_{ticker}").write_text("test\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """只看索引，隔离 pheromone.db"""
    monkeypatch.setattr(ph, "_load_db_prices", lambda t, db_path=None: {})


@pytest.fixture
def fake_yf(monkeypatch):
    """yfinance 打桩：`Ticker(t).history()` 返回给定收盘价的日线（美东时区索引，同真实形状）。"""
    frames = {}

    class _T:
        def __init__(self, t):
            self.t = t

        def history(self, period=None, **_kw):
            return frames[self.t]

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_T))
    # 这些日期都不是「交易所今天」，但别让结论依赖本机钟
    monkeypatch.setattr(dp, "_exchange_now", lambda: None)

    def _set(ticker, dates, closes):
        idx = pd.DatetimeIndex(pd.to_datetime(dates)).tz_localize("America/New_York")
        frames[ticker] = pd.DataFrame({"Close": closes, "Volume": [1_000_000.0] * len(closes)},
                                      index=idx)
    return _set


def _main_path(ticker):
    out = dp._fetch_history_metrics(ticker)
    assert out is not None and out.get("momentum_5d") is not None, "主路径桩没接上"
    return out["momentum_5d"]


class TestFallbackMatchesMainPath:
    @pytest.mark.parametrize("n_days", [5, 6, 8, 10])
    def test_same_closes_same_momentum(self, tmp_path, fake_yf, n_days):
        """同一串连续交易日收盘：回落 == 主路径（逐位，非近似）。"""
        dates, closes = _DATES[:n_days], _CLOSES[:n_days]
        fake_yf("VZ", dates, closes)
        _idx(tmp_path, "VZ", list(zip(dates, closes)))
        main = _main_path("VZ")
        fb = ph.momentum_5d("VZ", str(tmp_path))
        assert fb is not None, f"{n_days} 个交易日主路径算得出，回落却给 None"
        assert fb == pytest.approx(round(main, 4), abs=1e-9), (
            f"回落 {fb} ≠ 主路径 {main:.4f}——两边窗口不一致")

    def test_fixture_discriminates_windows(self):
        """夹具自证：4 日与 5 日窗口在这串价格上符号都相反，上面那条相等不是巧合。"""
        four = _CLOSES[-1] / _CLOSES[-5] - 1
        five = _CLOSES[-1] / _CLOSES[-6] - 1
        assert four * five < 0, (four, five)

    def test_shortest_history_both_sides_agree_on_availability(self, tmp_path, fake_yf):
        """4 个交易日的历史：主路径要 5 根才算（返回里没有 momentum_5d），回落也必须给 None。"""
        dates, closes = _DATES[:4], _CLOSES[:4]
        fake_yf("VZ", dates, closes)
        _idx(tmp_path, "VZ", list(zip(dates, closes)))
        out = dp._fetch_history_metrics("VZ")
        assert out is not None and out.get("momentum_5d") is None
        assert ph.momentum_5d("VZ", str(tmp_path)) is None


class TestMissedScanTolerance:
    """索引只有扫描跑过的日子：漏跑 ≤2 个交易日仍给值（锚点退到更早的一天），再多宁可 None。"""

    def _rows(self, drop):
        return [(d, c) for d, c in zip(_DATES, _CLOSES) if d not in drop]

    def test_exact_anchor_when_no_gap(self, tmp_path):
        _idx(tmp_path, "VZ", self._rows(()))
        # 10/9 往回 4 个交易日 = 10/5
        assert ph.momentum_5d("VZ", str(tmp_path)) == pytest.approx(round((37.0 / 43.0 - 1) * 100, 4))

    def test_two_missed_scans_still_answer(self, tmp_path):
        # 10/5、10/2 都没扫 ⇒ 第一个 ≥4 的锚点是 10/1（6 个交易日）
        _idx(tmp_path, "VZ", self._rows(("2026-10-05", "2026-10-02")))
        assert ph.momentum_5d("VZ", str(tmp_path)) == pytest.approx(round((37.0 / 42.0 - 1) * 100, 4))

    def test_three_missed_scans_give_none(self, tmp_path):
        # 再漏 10/1 ⇒ 最近锚点 9/30 距 7 个交易日 > MOMENTUM_MAX_GAP
        _idx(tmp_path, "VZ", self._rows(("2026-10-05", "2026-10-02", "2026-10-01")))
        assert ph.momentum_5d("VZ", str(tmp_path)) is None


class TestMainPathFormulasShareOneWindow:
    """全仓主路径公式都是同一个窗口，且回落常量与之对应。

    改一处就成了第三种口径——要换窗口就全换，并在 `ic_rerun_readiness._COHORT_HISTORY` 追加世代边界。
    """

    _FULL = re.compile(r'hist\["Close"\]\.iloc\[-1\]\s*/\s*hist\["Close"\]\.iloc\[-(\d+)\]')
    # 云端快照拼接：快照价当最新一点，`_cl.iloc[-k]` 当基准 ⇒ 窗口 = k 个交易日
    _SPLICED = re.compile(r'_base\s*=\s*float\(_cl\.iloc\[-(\d+)\]\)')

    def _scan(self, pat):
        hits = []
        for p in REPO_ROOT.rglob("*.py"):
            rel = p.relative_to(REPO_ROOT)
            if rel.parts[0] in {"tests", "experiments", ".claude"} or "worktrees" in rel.parts:
                continue
            for m in pat.finditer(p.read_text(encoding="utf-8", errors="replace")):
                hits.append((str(rel), int(m.group(1))))
        return hits

    def test_full_history_formulas(self):
        hits = self._scan(self._FULL)
        # 至少 data_pipeline 3 处 + cache / risk_engine / crowding_detector——扫描器自己没死
        assert len(hits) >= 6, hits
        bad = [h for h in hits if h[1] != ph.MOMENTUM_LOOKBACK + 1]
        assert not bad, f"主路径公式窗口与回落 MOMENTUM_LOOKBACK={ph.MOMENTUM_LOOKBACK} 不一致: {bad}"

    def test_spliced_formula(self):
        hits = self._scan(self._SPLICED)
        assert hits, "没找到 5d_snapshot_spliced 的基准取法——改名了就更新这条正则"
        bad = [h for h in hits if h[1] != ph.MOMENTUM_LOOKBACK]
        assert not bad, f"快照拼接窗口与回落 MOMENTUM_LOOKBACK={ph.MOMENTUM_LOOKBACK} 不一致: {bad}"

    def test_max_gap_keeps_two_day_slack(self):
        assert ph.MOMENTUM_MAX_GAP == ph.MOMENTUM_LOOKBACK + 2
