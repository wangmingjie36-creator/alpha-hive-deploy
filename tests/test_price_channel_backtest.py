"""Buzz 价格通道长历史回测（v0.45.697）的守卫：常量与预注册一致、通道映射照生产、开发期不碰留出期、留出期只跑一次。全部离线合成数据。"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "experiments"))
import price_channel_backtest as pcb  # noqa: E402


def _doc_constants():
    text = pcb.PREREG_DOC.read_text(encoding="utf-8")
    block = re.search(r"```prereg-constants\n(.*?)```", text, re.S).group(1)
    out = {}
    for line in block.strip().splitlines():
        k, v = (x.strip() for x in line.split("=", 1))
        out[k] = v
    return out


class TestPreregPinned:
    def test_keys_and_values_match(self):
        doc = _doc_constants()
        assert set(doc) == set(pcb.PREREG)
        for k, v in pcb.PREREG.items():
            if isinstance(v, list):
                assert doc[k].split(",") == v, k
            elif isinstance(v, float):
                assert float(doc[k]) == v, k
            elif isinstance(v, int):
                assert int(doc[k]) == v, k
            else:
                assert doc[k] == v, k

    def test_bonferroni_consistent(self):
        p = pcb.PREREG
        # 留出期至多 4 + 1 个检验
        assert p["alpha"] == pytest.approx(0.05 / (len(p["confirmatory"]) + 1))
        assert p["explore_alpha"] == pytest.approx(0.05 / len(p["exploratory"]), abs=1e-6)

    def test_holdout_ends_before_h1_window(self):
        assert pcb._d(pcb.PREREG["holdout_last_outcome"]) < dt.date(2026, 10, 12)


class TestChannelsMatchBuzzBee:
    def test_thresholds_read_from_config(self):
        import config
        sc = config.AGENT_SCORING
        assert pcb.volume_channel(2.01) == 80 and pcb.volume_channel(1.0) == 35 and pcb.volume_channel(None) == 50
        assert pcb.volatility_channel(61) == 25 and pcb.volatility_channel(20) == 75 and pcb.volatility_channel(None) == 50
        assert pcb.momentum_channel(15) == 100 and pcb.momentum_channel(-15) == 0 and pcb.momentum_channel(0) == 50
        assert set(sc["buzz_weights"]) >= {"momentum", "volume", "volatility"}

    def test_buzz_bee_still_uses_same_shape(self):
        """生产映射若改了（档位 / 截断 / 缺省 50），这里红——回测就不再是「生产口径」，要走预注册 §9。"""
        src = (_ROOT / "swarm_agents" / "buzz_bee.py").read_text(encoding="utf-8")
        for needle in ('momentum_pct = max(-10, min(10, _mom_raw))',
                       'momentum_sentiment = (momentum_pct + 10) / 20 * 100',
                       'volume_signal = 80', 'volume_signal = 65', 'volume_signal = 35', 'volume_signal = 20',
                       'vol_sentiment = 25', 'vol_sentiment = 40', 'vol_sentiment = 60', 'vol_sentiment = 75'):
            assert needle in src, needle

    def test_composite_weights(self):
        w = {"momentum": 0.2, "volume": 0.1, "volatility": 0.05}
        assert pcb.composite(100, 0, 0, w) == pytest.approx(100 * 0.2 / 0.35)


class TestStats:
    def test_nw_lag0_is_plain(self):
        xs = [0.1, -0.05, 0.2, 0.0, 0.15]
        mu, se, t = pcb.nw_t(xs, 0)
        n = len(xs)
        var = sum((x - mu) ** 2 for x in xs) / n
        assert se == pytest.approx((var / n) ** 0.5)

    def test_nw_positive_autocorr_widens_se(self):
        xs = [1, 1, 1, -1, -1, -1] * 10
        assert pcb.nw_t(xs, 2)[1] > pcb.nw_t(xs, 0)[1]

    def test_tercile_ties_do_not_peek_at_returns(self):
        rows = [(f"T{i}", {"X": 50.0}, float(i)) for i in range(15)]
        mean, _, n = pcb.tercile_ls({dt.date(2020, 1, 6): rows}, "X", 15)
        # 全并列：按票名排，结果与收益顺序无关（T0..T14 名字序 ≠ 收益序时不会恒为正）
        names = sorted(r[0] for r in rows)
        hi = [int(x[1:]) for x in names[-5:]]
        lo = [int(x[1:]) for x in names[:5]]
        assert mean == pytest.approx(100 * (sum(hi) / 5 - sum(lo) / 5)) and n == 1


def _synthetic_frames(n_tickers=16, start="2018-06-01", end="2020-03-31"):
    days = pd.bdate_range(start, end)
    frames = {}
    for k in range(n_tickers):
        close = [100 * (1 + 0.001 * ((i * (k + 3)) % 17 - 8)) ** (i % 40) for i in range(len(days))]
        frames[f"T{k}"] = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                                        "Volume": [1e6 + 1e4 * ((i + k) % 9) for i in range(len(days))]}, index=days)
    return frames


class TestBlinding:
    def test_dev_never_reads_holdout_prices(self, monkeypatch):
        frames = _synthetic_frames()
        seen = []
        real = pcb.build_panel

        def spy(fr, days, cal, h, last):
            seen.append((max(days) if days else None, last))
            return real(fr, days, cal, h, last)

        monkeypatch.setattr(pcb, "build_panel", spy)
        monkeypatch.setitem(pcb.PREREG, "dev_start", "2019-01-01")
        pcb.run_dev(frames)
        (max_day, last), = seen
        assert last == dt.date(2019, 12, 31) and max_day <= last

    def test_holdout_refuses_second_run(self, tmp_path, monkeypatch):
        res = tmp_path / "h.json"
        res.write_text("{}")
        monkeypatch.setattr(pcb, "HOLDOUT_RESULT", res)
        fetched = []
        monkeypatch.setattr(pcb, "fetch", lambda *a, **k: fetched.append(1))
        assert pcb.main(["--holdout"]) == 2 and not fetched

    def test_holdout_requires_dev_selection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pcb, "HOLDOUT_RESULT", tmp_path / "none.json")
        monkeypatch.setattr(pcb, "DEV_SELECTION", tmp_path / "none_sel.json")
        monkeypatch.setattr(pcb, "fetch", lambda *a, **k: pytest.fail("不该取数"))
        assert pcb.main(["--holdout"]) == 2

    def test_signals_use_only_past(self):
        frames = _synthetic_frames(n_tickers=1)
        df = frames["T0"]
        i = 300
        a = pcb.signals_at(df, i)
        df2 = df.copy()
        df2.iloc[i + 1:, df2.columns.get_loc("Close")] *= 3     # 改未来
        assert pcb.signals_at(df2, i) == a
