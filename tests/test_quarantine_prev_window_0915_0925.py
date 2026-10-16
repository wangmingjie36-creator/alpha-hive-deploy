"""09-15 / 09-24 / 09-25 已证实取错窗口的观测（v0.45.517）。

v0.45.508 复核 09-15 QCOM / NVDA 时查出：09-15 两只的量比是 Twelve Data 兜底「最新一根 = 09-14」的值、动量是 465 前自攒索引
「09-04 → 09-14」的窗口；09-24 / 09-25 两个全源故障日的动量也来自那条回落，窗口止于 09-22。用户 2026-10-16 批准「全部已证实的」：
逐位 / 万分之一内复现出错误窗口的才登，09-24 T 复现不出、不登。本文件钉：

1. 名单恰好是这些 (日期, 标的, 信号)：09-24 不含 T，09-25 含 T（30 只）；只隔离观测层 `price.*`；
2. 入库口真的挡（`archive()` 不写这些、同批其它信号照写）——证明条目接上了机制，不是只躺在列表里；
3. 都在维度 IC 窗口之前（`FORWARD_START`），不碰 H1。
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest

import signal_archive as sa

_ROOT = Path(__file__).resolve().parent.parent
_UNIVERSE = ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL", "CRM", "CVX", "DE",
             "DELL", "ENPH", "JNJ", "META", "MSFT", "MU", "NEE", "NFLX", "NVDA", "QCOM",
             "RKLB", "SNOW", "T", "TMO", "TMUS", "TSLA", "VKTX", "VZ", "WMT", "XOM")
_EXPECTED = {
    ("2026-09-15", "price.volume_ratio"): {"NVDA", "QCOM"},
    ("2026-09-15", "price.momentum_5d"): {"NVDA", "QCOM"},
    ("2026-09-24", "price.momentum_5d"): set(_UNIVERSE) - {"T"},
    ("2026-09-25", "price.momentum_5d"): set(_UNIVERSE),
}


def _entries():
    return [q for q in sa.QUARANTINE if re.match(r"v0\.45\.517", q["reason"])]


class TestRegistry:
    def test_exactly_the_proven_cells(self):
        got = {}
        for q in _entries():
            assert len(q["signals"]) == 1 and q.get("tickers") is not None
            got.setdefault((q["date"], q["signals"][0]), set()).update(q["tickers"])
        assert got == _EXPECTED

    def test_is_quarantined_matches_the_table(self):
        """按 `is_quarantined` 的真值表再核一遍（含反面）：名单外的标的 / 信号 / 日子都不挡。"""
        for (d, sig), tks in _EXPECTED.items():
            for t in _UNIVERSE:
                assert sa.is_quarantined(d, t, sig) == (t in tks), (d, t, sig)
        assert not sa.is_quarantined("2026-09-24", "T", "price.momentum_5d")     # 复现不出 ⇒ 不登
        assert not sa.is_quarantined("2026-09-15", "NVDA", "price.volatility_20d")
        assert not sa.is_quarantined("2026-09-16", "NVDA", "price.momentum_5d")
        assert not sa.is_quarantined("2026-09-24", "NVDA", "price.volume_ratio")

    def test_observation_layer_only(self):
        for d in ("2026-09-15", "2026-09-24", "2026-09-25"):
            for t in _UNIVERSE:
                for sig in ("buzz.comp.momentum_signal", "buzz.comp.volume_signal", "agent.BuzzBeeWhisper.score",
                            "agent.ScoutBeeNova.score", "sentiment.pct"):
                    assert not sa.is_quarantined(d, t, sig), (d, t, sig)

    def test_entries_carry_reason_and_evidence(self):
        for q in _entries():
            assert q["evidence"] and "CHANGELOG.md#v0.45.517" in q["evidence"]

    def test_changelog_entry_is_written(self):
        heads = re.findall(r"^## \[0\.45\.517\] — [^\n]*", (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
        assert len(heads) == 1 and "占位" not in heads[0]

    def test_all_before_dim_ic_window(self):
        spec = importlib.util.spec_from_file_location("dim_ic_protocol_q517", _ROOT / "experiments" / "dim_ic_protocol.py")
        P = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(P)
        assert all(q["date"] < P.FORWARD_START for q in _entries())


class TestWritePath:
    @pytest.fixture()
    def db(self, tmp_path):
        p = tmp_path / "p.db"
        sa.ensure_schema(p)
        return p

    @staticmethod
    def _swarm(tickers):
        return {t: {"agent_details": {
            "ScoutBeeNova": {"details": {"momentum_5d": -8.42}},
            "BuzzBeeWhisper": {"details": {"volume_ratio": 1.01, "sentiment_pct": 55.0}},
        }} for t in tickers}

    def _rows(self, db, date):
        con = sqlite3.connect(db)
        try:
            return {(t, s) for t, s in con.execute(
                "SELECT ticker, signal FROM signal_archive WHERE date=?", (date,))}
        finally:
            con.close()

    def test_0915_named_cells_never_enter(self, db):
        sa.archive(self._swarm(("NVDA", "QCOM", "MSFT")), "2026-09-15", db)
        rows = self._rows(db, "2026-09-15")
        for t in ("NVDA", "QCOM"):
            assert (t, "price.momentum_5d") not in rows and (t, "price.volume_ratio") not in rows
            assert (t, "sentiment.pct") in rows                        # 同批其它信号照写
        assert {("MSFT", "price.momentum_5d"), ("MSFT", "price.volume_ratio")} <= rows

    def test_0924_t_still_enters(self, db):
        sa.archive(self._swarm(("T", "NVDA")), "2026-09-24", db)
        rows = self._rows(db, "2026-09-24")
        assert ("T", "price.momentum_5d") in rows and ("NVDA", "price.momentum_5d") not in rows
        assert ("NVDA", "price.volume_ratio") in rows                  # 那天只隔离动量
