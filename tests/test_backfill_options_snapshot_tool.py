"""`experiments/backfill_options_snapshot_from_cloud.py` 的防线（v0.45.497）。

10-12 真实 dry-run 里「置空」一次都没触发（29 只 RV 全是 twelve_data@2026-10-12）⇒ 那条分支在真数据上从没被执行过，
只能在这里执行。离线：只测纯函数与临时目录。
"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "backfill_tool", _ROOT / "experiments" / "backfill_options_snapshot_from_cloud.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

D = "2026-10-12"


class TestHonestFixups:
    def test_rv_from_other_source_is_nulled(self):
        """变异：判据改成只看 `rv_30d is not None` 或接受 twelve_data（不带 @D）⇒ 红。"""
        for src in ("yfinance", "twelve_data", "twelve_data@2026-10-13"):
            snap = {"rv_30d": 30.0, "iv_rv_spread": 1.0, "iv_rv_signal": "fair", "iv_rv_detail": {"source": src}}
            nulled = bf._honest_fixups(snap, D)
            assert snap["rv_30d"] is None and snap["iv_rv_spread"] is None and snap["iv_rv_signal"] == "unknown", src
            assert len(nulled) == 1 and src in nulled[0]

    def test_rv_of_target_day_is_kept(self):
        snap = {"rv_30d": 30.0, "iv_rv_spread": 1.0, "iv_rv_signal": "fair",
                "iv_rv_detail": {"source": f"twelve_data@{D}"}}
        assert bf._honest_fixups(snap, D) == [] and snap["rv_30d"] == 30.0

    def test_term_structure_not_from_snapshot_is_nulled(self):
        snap = {"iv_term_structure": {"data_available": True, "source": "yfinance", "front_iv": 30.0}}
        nulled = bf._honest_fixups(snap, D)
        assert snap["iv_term_structure"]["data_available"] is False and snap["iv_term_structure"]["front_iv"] is None
        assert len(nulled) == 1
        snap = {"iv_term_structure": {"data_available": True, "source": "cboe", "front_iv": 30.0}}
        assert bf._honest_fixups(snap, D) == [] and snap["iv_term_structure"]["front_iv"] == 30.0


class TestSeedIvIndex:
    def test_only_rows_strictly_before_target(self, tmp_path):
        """IV Rank 的分母只能是目标日之前的观测（目标日自己由 analyze 追加）。变异：`<` 改 `<=` 或不过滤 ⇒ 红。"""
        prod, tmp = tmp_path / "prod", tmp_path / "tmp"
        prod.mkdir(), tmp.mkdir()
        rows = [{"date": d, "iv": 30.0 + i} for i, d in enumerate(["2026-10-08", "2026-10-09", D, "2026-10-13"])]
        (prod / "iv_history_NVDA.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        assert bf._seed_iv_index(prod, tmp, "NVDA", D) == 2
        got = [json.loads(ln)["date"] for ln in (tmp / "iv_history_NVDA.jsonl").read_text().splitlines()]
        assert got == ["2026-10-08", "2026-10-09"]
        assert bf._index_has(prod, "NVDA", D) and not bf._index_has(prod, "NVDA", "2026-10-07")


class TestAsOfYfinance:
    def test_history_is_cut_at_target_and_other_attrs_forward(self):
        idx = pd.DatetimeIndex([datetime(2026, 10, d) for d in (9, 12, 13)], tz="America/New_York")

        class _T:
            def __init__(self, t):
                self.ticker = t

            def history(self, *a, **k):
                return pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=idx)

        class _YF:
            Ticker = _T
            download = "real-download"

        proxy = bf._as_of_yfinance(_YF, D)
        h = proxy.Ticker("NVDA").history(period="1y")
        assert [ts.date().isoformat() for ts in h.index] == ["2026-10-09", D]
        assert proxy.Ticker("NVDA").ticker == "NVDA"
        assert proxy.download == "real-download"
