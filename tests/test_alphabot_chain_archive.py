"""Alpha Bot 当日原始期权链存档（v0.45.815）。

为什么有：盘中快照只存汇总值（Zero Gamma、逐行权价净 GEX……），没有逐合约 OI / IV。2026-11-24 拆解
NVDA「下跌日 Zero Gamma 反而冒到上方」时，昨天的链找不到，OI 这一项只能用排除法推。现在每张落盘的快照
把这一拍用的原始链整份覆盖写到 `intraday/<日期>/<T>.chain.json.gz`，一天留最后一份。

守住的几件事：
  · 存的就是这一拍现算用的那份链（逐合约原样读得回来），同一天后来的拍覆盖前面的；
  · 存档坏了**快照照落**，但这一拍的 `chain_archive` 写明 `failed`、计数、日志 warning（下游看得见）；
  · 读回口 `archived_chain` 把「没有」和「读坏」分开报，不把坏文件当成没有；
  · 存档文件不干扰盘中快照的读者（`intraday` / `intraday_dates`）。
"""
from __future__ import annotations

import copy
import gzip
import json
import logging
from datetime import datetime

import pytest

from alphabot import service as SV
from alphabot import synthetic as syn
from alphabot.service import AlphaBotService, ET

DAY = "2026-09-30"


def _feed(stamps):
    """合成链；每次调用换一个最后成交时刻与现价（否则第二拍按 payload_unchanged 不记）。"""
    calls = {"n": 0}

    def fetch(ticker, *, as_of=None):
        raw, reason = syn.demo_fetch(ticker, as_of=as_of)
        if raw is None:
            return raw, reason
        raw = copy.deepcopy(raw)
        i = min(calls["n"], len(stamps) - 1)
        calls["n"] += 1
        raw["payload_last_trade_time"] = stamps[i]
        raw["underlying_price"] = float(raw["underlying_price"]) + i
        raw["contracts"][0]["oi"] = 1000.0 + i          # 认得出是哪一拍的链
        return raw, reason
    return fetch


def _svc(tmp_path, stamps=("2026-09-30T10:59:00",)):
    return AlphaBotService(fetch_fn=_feed(list(stamps)), state_dir=tmp_path / "st")


def _at(h, m):
    return datetime(2026, 9, 30, h, m, tzinfo=ET)


class TestArchiveWrite:
    def test_snapshot_archives_the_chain_it_used(self, tmp_path):
        svc = _svc(tmp_path)
        assert svc.take_snapshot("NVDA", now=_at(11, 0))["recorded"] is True
        f = tmp_path / "st" / "intraday" / DAY / f"NVDA{SV.CHAIN_ARCHIVE_SUFFIX}"
        assert f.is_file()
        got = svc.archived_chain("NVDA", DAY)
        assert got["available"] is True and got["reason"] is None
        want, _ = syn.demo_fetch("NVDA")
        assert len(got["raw"]["contracts"]) == len(want["contracts"]) > 0
        assert got["raw"]["contracts"][0]["oi"] == 1000.0
        assert {"strike", "cp", "iv", "oi", "expiry", "dte"} <= set(got["raw"]["contracts"][0])
        snap = svc.intraday("NVDA", DAY)["snapshots"][-1]
        assert snap["chain_archive"] == "ok"
        assert svc.stats["chain_archive_failures"] == 0

    def test_later_snapshot_overwrites_so_the_day_keeps_the_last_chain(self, tmp_path):
        svc = _svc(tmp_path, stamps=("2026-09-30T10:59:00", "2026-09-30T15:58:00"))
        assert svc.take_snapshot("NVDA", now=_at(11, 0))["recorded"] is True
        assert svc.take_snapshot("NVDA", now=_at(15, 59))["recorded"] is True
        got = svc.archived_chain("NVDA", DAY)
        assert got["raw"]["payload_last_trade_time"] == "2026-09-30T15:58:00"
        assert got["raw"]["contracts"][0]["oi"] == 1001.0
        files = sorted(p.name for p in (tmp_path / "st" / "intraday" / DAY).iterdir())
        assert files == ["NVDA.chain.json.gz", "NVDA.jsonl"], f"不该留临时文件或多份存档：{files}"

    def test_unchanged_payload_writes_neither_snapshot_nor_archive(self, tmp_path):
        svc = _svc(tmp_path)                             # 同一个时刻 ⇒ 第二拍 payload_unchanged
        svc.take_snapshot("NVDA", now=_at(11, 0))
        f = tmp_path / "st" / "intraday" / DAY / "NVDA.chain.json.gz"
        before = f.stat().st_mtime_ns
        assert svc.take_snapshot("NVDA", now=_at(11, 10))["reason"] == "payload_unchanged"
        assert f.stat().st_mtime_ns == before

    def test_nan_in_chain_is_stored_as_valid_json(self, tmp_path):
        def fetch(ticker, *, as_of=None):
            raw, reason = syn.demo_fetch(ticker, as_of=as_of)
            raw = copy.deepcopy(raw)
            raw["contracts"][0]["iv"] = float("nan")
            return raw, reason
        svc = AlphaBotService(fetch_fn=fetch, state_dir=tmp_path / "st")
        svc.take_snapshot("NVDA", now=_at(11, 0))
        f = tmp_path / "st" / "intraday" / DAY / "NVDA.chain.json.gz"
        text = gzip.decompress(f.read_bytes()).decode("utf-8")
        assert "NaN" not in text
        assert json.loads(text)["contracts"][0]["iv"] is None

    def test_archive_failure_does_not_block_snapshot_but_is_visible(self, tmp_path, monkeypatch, caplog):
        def boom(path, obj):
            raise OSError("disk full")
        monkeypatch.setattr(SV, "_atomic_write_gzip_json", boom)
        svc = _svc(tmp_path)
        with caplog.at_level(logging.WARNING):
            assert svc.take_snapshot("NVDA", now=_at(11, 0))["recorded"] is True
        snap = svc.intraday("NVDA", DAY)["snapshots"][-1]
        assert snap["chain_archive"] == "failed"
        assert svc.stats["chain_archive_failures"] == 1
        assert any("原始链存档" in r.getMessage() and "disk full" in r.getMessage() for r in caplog.records)
        assert svc.archived_chain("NVDA", DAY)["reason"] == "missing"

    def test_no_raw_chain_is_recorded_as_such(self, tmp_path):
        svc = _svc(tmp_path)
        assert svc._archive_chain("NVDA", None, DAY) == "no_raw"
        assert not (tmp_path / "st" / "intraday" / DAY / "NVDA.chain.json.gz").exists()


class TestArchiveRead:
    def test_missing_and_corrupt_are_different_answers(self, tmp_path):
        svc = _svc(tmp_path)
        assert svc.archived_chain("NVDA", DAY) == {"ticker": "NVDA", "date": DAY, "available": False,
                                                   "reason": "missing", "raw": None}
        d = tmp_path / "st" / "intraday" / DAY
        d.mkdir(parents=True)
        (d / "NVDA.chain.json.gz").write_bytes(b"not gzip at all")
        bad = svc.archived_chain("NVDA", DAY)
        assert bad["available"] is False and bad["reason"] == "corrupt" and bad["error"]
        (d / "NVDA.chain.json.gz").write_bytes(gzip.compress(b"[1, 2]"))
        assert svc.archived_chain("NVDA", DAY)["reason"] == "corrupt"

    def test_bad_date_is_rejected(self, tmp_path):
        with pytest.raises(SV.BadRequest):
            _svc(tmp_path).archived_chain("NVDA", "../../etc")

    def test_archive_does_not_disturb_intraday_readers(self, tmp_path):
        svc = _svc(tmp_path)
        svc.take_snapshot("NVDA", now=_at(11, 0))
        assert svc.intraday("NVDA", DAY)["n_bad_lines"] == 0
        assert len(svc.intraday("NVDA", DAY)["snapshots"]) == 1
        assert svc.intraday_dates("NVDA")["dates"] == [DAY]
        assert svc.intraday_dates("TSLA")["dates"] == []
