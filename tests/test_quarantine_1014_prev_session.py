"""2026-10-14 取错日观测的隔离（v0.45.508，维度 IC 协议 §13.8）。

10-14 生产扫描 yfinance 全挡：29 只的 `price.volume_ratio` 来自 Twelve Data 兜底、末根是 10-13（v0.45.505 起这种值拒收），
BILI 的 `price.momentum_5d` 来自自攒索引、窗口末端是 10-13。用户 2026-10-16 选定按协议 §3 已登记的排除口径
（`QUARANTINE`）只隔离观测层。本文件钉四件事：

1. 名单本身：恰好这 29 只 × 量比、BILI × 动量；JNJ（当天未入档）与其余 29 只的动量不在内；
2. 后果：维度 IC 的 H1（信号表含输入层）当天只剩 JNJ ⇒ 低于 `MIN_WIDTH` ⇒ 当天无效；**删掉条目该日回来**（对照，
   证明丢天来自名单而不是夹具碰巧不够宽）；
3. 范围：H2 不读 `price.*`，照算；派生的 `buzz.comp.*` 通道与 Agent 评分不隔离（§13.2 自证 ② 要它们在）；
4. 登记早于该日结果：10-14 的 T+7 用 backtester 的交易日历算，晚于登记日。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pandas as pd

import signal_archive

_ROOT = Path(__file__).resolve().parent.parent
_DAY = "2026-10-14"
_NEXT = "2026-10-15"
_REGISTERED = "2026-10-16"
_VOLUME_PREV_SESSION = ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL", "CRM", "CVX", "DE",
                        "DELL", "ENPH", "META", "MSFT", "MU", "NEE", "NFLX", "NVDA", "QCOM", "RKLB",
                        "SNOW", "T", "TMO", "TMUS", "TSLA", "VKTX", "VZ", "WMT", "XOM")
_UNIVERSE = _VOLUME_PREV_SESSION + ("JNJ",)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load("dim_ic_forward_test_q1014", "experiments/dim_ic_forward_test.py")
P = _load("dim_ic_protocol_q1014", "experiments/dim_ic_protocol.py")


def _entries():
    return [q for q in signal_archive.QUARANTINE if q["date"] == _DAY]


def _rows(date):
    """生产形状的一天：30 只真实代码，5 维齐全，已结算。"""
    out = []
    for i, tk in enumerate(_UNIVERSE):
        ds = {"signal": (i * 7) % 30, "catalyst": (i * 3) % 30, "sentiment": i,
              "odds": (i * 5) % 30, "risk_adj": (i * 11) % 30}
        close = 100.0 * (1 + 0.001 * (i + 1))
        out.append((date, tk, json.dumps(ds), 100.0, 1, close, close, 0.0, 99.0, "TIME"))
    return out


def _db(tmp_path, days):
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT, dimension_scores TEXT, "
                "price_at_predict REAL, checked_t7 INTEGER, close_t7 REAL, price_t7 REAL, "
                "return_t7 REAL, exit_price REAL, exit_reason TEXT)")
    for d in days:
        con.executemany("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?)", _rows(d))
    con.commit()
    con.close()
    return db


def _series(tmp_path, monkeypatch, name, days):
    # 本文件测隔离名单，不测协议排除日——清空后者，免得两份名单的效果混在一起
    monkeypatch.setattr(signal_archive, "FORWARD_TEST_EXCLUDED_DAYS", {})
    rows = fx.load_rows(_db(tmp_path, days), P, "2026-12-31")
    it = fx.items(P)[name]
    return fx.weekly_series(rows, it["value"], it["signals"], P)


class TestRegistry:
    def test_volume_entry_exactly_the_29(self):
        vol = [q for q in _entries() if "price.volume_ratio" in q["signals"]]
        assert len(vol) == 1
        assert set(vol[0]["tickers"]) == set(_VOLUME_PREV_SESSION)
        assert len(vol[0]["tickers"]) == 29 and "JNJ" not in vol[0]["tickers"]
        assert vol[0]["signals"] == ("price.volume_ratio",)

    def test_momentum_entry_only_bili(self):
        mom = [q for q in _entries() if "price.momentum_5d" in q["signals"]]
        assert len(mom) == 1
        assert mom[0]["tickers"] == ("BILI",) and mom[0]["signals"] == ("price.momentum_5d",)

    def test_entries_carry_reason_and_evidence(self):
        for q in _entries():
            assert q["reason"] and q["evidence"] and "v0.45.508" in q["reason"]

    def test_observation_layer_only(self):
        """派生的通道值、Agent 评分、合成值都不隔离：§13.2 自证 ② 要它们在，QUARANTINE 也不隔离评分。"""
        for tk in _UNIVERSE:
            for sig in ("buzz.comp.volume_signal", "buzz.comp.momentum_signal", "agent.BuzzBeeWhisper.score",
                        "sentiment.pct", "price.volatility_20d", "market.fear_greed"):
                assert not signal_archive.is_quarantined(_DAY, tk, sig), (tk, sig)
        assert not signal_archive.is_quarantined(_DAY, "JNJ", "price.volume_ratio")
        for tk in _VOLUME_PREV_SESSION:
            assert signal_archive.is_quarantined(_DAY, tk, "price.volume_ratio"), tk
            assert signal_archive.is_quarantined(_DAY, tk, "price.momentum_5d") == (tk == "BILI"), tk

    def test_not_a_forward_test_excluded_day(self):
        """走的是 §3 的隔离，不是修订 2 的整天排除——后者三份检验共用，会连带 F&G 敞口门。"""
        assert not signal_archive.forward_test_excluded(_DAY)

    def test_registered_before_outcome(self):
        """10-14 的 T+7（backtester 判到期用的交易日历）必须晚于登记日，否则隔离可能受结果影响。"""
        import backtester
        assert backtester._BDAY_AVAILABLE
        t7 = (pd.Timestamp(_DAY) + 7 * backtester._US_BDAY).strftime("%Y-%m-%d")
        assert t7 == "2026-10-23"
        assert _REGISTERED < t7


class TestDimIcEffect:
    def test_h1_loses_the_day(self, tmp_path, monkeypatch):
        assert _series(tmp_path, monkeypatch, "H1", [_DAY]) == []

    def test_h1_keeps_the_neighbour_day(self, tmp_path, monkeypatch):
        s = _series(tmp_path, monkeypatch, "H1", [_DAY, _NEXT])
        assert len(s) == 1 and s[0][2] == 1          # 同一周只有 10-15 一个有效日

    def test_control_entries_removed_brings_it_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(signal_archive, "QUARANTINE",
                            [q for q in signal_archive.QUARANTINE if q["date"] != _DAY])
        s = _series(tmp_path, monkeypatch, "H1", [_DAY])
        assert len(s) == 1 and s[0][2] == 1

    def test_h2_untouched(self, tmp_path, monkeypatch):
        s = _series(tmp_path, monkeypatch, "H2", [_DAY])
        assert len(s) == 1 and s[0][2] == 1
