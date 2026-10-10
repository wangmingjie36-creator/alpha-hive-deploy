"""v0.45.452：ledger_io 第二版——整文件 JSON 文档、失败在源头登记、缓存写入收口、三本账迁入。

每一组都对着一个实测过的病灶写：
  · `meta.json` 是账本不是装饰——v0.45.97 `"cash": NaN` 落盘后被 `json.loads` 照单全收，净值烂了四天；
    跨式腿 / 组合 Greeks 读到坏 meta 时「按新账本处理」⇒ 现金重置并写回；
  · `SafeJSONEncoder` 的清洗只挂在 `encode()` 上，`json.dump` 从来不走 ⇒ `atomic_json_write` 写出 `{"cash": NaN}`；
  · 调用方把账本步骤包在「非致命」except 里 ⇒ 严格读写抛出的异常又被吞成一行 warning。
"""
from __future__ import annotations

import io
import json
import math
import os

import pytest

import ledger_io as L

NAN = float("nan")


@pytest.fixture(autouse=True)
def _clean_failures():
    L.reset_failures()
    yield
    L.reset_failures()


def _strict(text: str):
    def rej(c):
        raise ValueError(c)
    return json.loads(text, parse_constant=rej)


# ─────────────────────────────── 整文件 JSON 文档

class TestDocuments:

    @pytest.mark.parametrize("bad", [{"cash": NAN}, {"x": [math.inf]}, ["not", "an", "object"], {"o": object()}])
    def test_write_json_rejects_and_leaves_the_file_untouched(self, tmp_path, bad):
        d = tmp_path / "ledger"          # tmp_path 根下有 conftest 的沙箱目录，单开一层只看自己的
        d.mkdir()
        p = d / "meta.json"
        p.write_text('{"cash": 100.0}\n', encoding="utf-8")
        before = p.read_bytes()
        with pytest.raises(L.LedgerWriteError):
            L.write_json(p, bad)
        assert p.read_bytes() == before
        assert [x.name for x in d.iterdir()] == ["meta.json"], "临时文件必须清掉"

    @pytest.mark.parametrize("content, why", [
        ('{"cash": NaN}', "NaN 字面量（v0.45.97 原样）"),
        ('{"cash": Infinity}', "Infinity 字面量"),
        ('{"cash": 1', "截断"),
        ('[1, 2]', "顶层不是对象"),
        ('', "空文件"),
    ])
    def test_load_json_is_strict(self, tmp_path, content, why):
        p = tmp_path / "meta.json"
        p.write_text(content, encoding="utf-8")
        with pytest.raises(L.LedgerCorrupt):
            L.load_json(p, default={"fresh": True})      # 坏文档**不**退回缺省值

    def test_missing_document(self, tmp_path):
        assert L.load_json(tmp_path / "nope.json", default=None) is None
        with pytest.raises(FileNotFoundError):
            L.load_json(tmp_path / "nope.json")

    def test_roundtrip_and_format(self, tmp_path):
        p = tmp_path / "d" / "meta.json"
        L.write_json(p, {"b": 1, "a": "现金"}, indent=1, sort_keys=True)
        assert p.read_text(encoding="utf-8") == '{\n "a": "现金",\n "b": 1\n}\n'
        assert L.load_json(p) == {"a": "现金", "b": 1}
        assert oct(p.stat().st_mode & 0o777) == "0o644"


# ─────────────────────────────── 失败在源头登记

class TestFailuresAreRecordedAtTheSource:
    """调用方吞掉异常也没用：ledger_io 自己记下了，扫描收尾进 status.json ⇒ P2。"""

    def test_swallowed_corrupt_read_is_still_counted(self, tmp_path):
        p = tmp_path / "x.jsonl"
        p.write_text('{"a": 1}\n{"a": ', encoding="utf-8")
        try:
            L.load_jsonl(p)
        except Exception:  # noqa: BLE001 - 模拟日报的「非致命」吞法
            pass
        f = L.failures()
        assert f["n"] == 1 and f["items"][0]["kind"] == "LedgerCorrupt" and f["items"][0]["op"] == "load"
        assert f["items"][0]["path"] == str(p)

    def test_rejected_write_and_nested_append_count_once_each(self, tmp_path):
        with pytest.raises(L.LedgerWriteError):
            L.write_json(tmp_path / "m.json", {"cash": NAN})
        with pytest.raises(L.LedgerWriteError):
            L.append_jsonl(tmp_path / "a.jsonl", {"v": NAN})
        f = L.failures()
        assert f["n"] == 2, f                     # write_json → atomic_write_text 嵌套，不能记两次
        assert [i["op"] for i in f["items"]] == ["write", "append"]

    def test_failure_deep_inside_nested_calls_counts_once(self, tmp_path, monkeypatch):
        """上一条的 NaN 在外层序列化就被拒、根本没进内层；真正会重复登记的是**最里层**出错：
        write_json → atomic_write_text 两层 `_observed` 都接得到同一个异常（还有 append 的三层）。"""
        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            L.write_json(tmp_path / "m.json", {"a": 1})
        with pytest.raises(OSError):
            L.append_jsonl(tmp_path / "a.jsonl", {"a": 1})
        f = L.failures()
        assert f["n"] == 2 and [i["kind"] for i in f["items"]] == ["OSError", "OSError"], f

    def test_lock_timeout_is_counted(self, tmp_path):
        import threading
        d = tmp_path / "ledger"
        hold, release = threading.Event(), threading.Event()

        def holder():
            with L.locked(d):
                hold.set()
                release.wait(5)
        t = threading.Thread(target=holder)
        t.start()
        hold.wait(5)
        try:
            with pytest.raises(L.LedgerLockTimeout):
                with L.locked(d, timeout=0.1):
                    pass
        finally:
            release.set()
            t.join()
        assert L.failures()["n"] == 1 and L.failures()["items"][0]["op"] == "lock"

    def test_success_and_missing_files_are_not_failures(self, tmp_path):
        L.write_json(tmp_path / "m.json", {"a": 1})
        L.append_jsonl(tmp_path / "a.jsonl", {"a": 1})
        L.load_jsonl(tmp_path / "missing.jsonl")
        L.load_json(tmp_path / "missing.json", default=None)
        assert L.failures() == {"n": 0, "items": []}

    def test_items_are_capped_but_the_count_is_not(self, tmp_path):
        for i in range(L._FAILURES_MAX_ITEMS + 5):
            with pytest.raises(L.LedgerWriteError):
                L.write_json(tmp_path / f"{i}.json", {"x": NAN})
        f = L.failures()
        assert f["n"] == L._FAILURES_MAX_ITEMS + 5 and len(f["items"]) == L._FAILURES_MAX_ITEMS

    def test_reaches_status_and_raises_p2(self, tmp_path):
        import alert_manager as am
        import scan_timing as stt
        with pytest.raises(L.LedgerWriteError):
            L.write_json(tmp_path / "meta.json", {"cash": NAN})
        c = stt.counters()
        assert c["ledger_io"]["n"] == 1
        status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"counters": {"ledger_io": c["ledger_io"]}, "extra": {"gh_pages": {"success": True}},
                                  "production_sync": {"outcome": "up_to_date"}}}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a.analyze(p)
        hits = [x for x in a.alerts if "账本 / 状态文件读写失败" in x.message]
        assert len(hits) == 1 and hits[0].level == am.AlertLevel.MEDIUM, [x.message for x in a.alerts]
        assert "meta.json" in hits[0].details["明细"]
        assert "账本读写失败 1 次(meta.json)" in stt.summary_line({"phases": {}, "counters": {"ledger_io": c["ledger_io"]}})

    @pytest.mark.parametrize("counter, alerted, skipped", [({"n": 0, "items": []}, False, False), (None, False, True)])
    def test_clean_is_silent_and_missing_is_skipped(self, tmp_path, counter, alerted, skipped):
        import alert_manager as am
        status = {"status": "success", "steps_result": {"step2_hive_analysis": {"status": "success"}},
                  "scan_timing": {"counters": {"ledger_io": counter}, "extra": {"gh_pages": {"success": True}},
                                  "production_sync": {"outcome": "up_to_date"}}}
        p = tmp_path / "status.json"
        p.write_text(json.dumps(status), encoding="utf-8")
        a = am.AlertAnalyzer(report_dir=tmp_path)
        a.analyze(p)
        assert any("账本 / 状态文件读写失败" in x.message for x in a.alerts) is alerted
        assert any("账本读写失败检查" in s for s in a.checks_skipped) is skipped


# ─────────────────────────────── SafeJSONEncoder / atomic_json_write

class TestSafeEncoderCoversDump:

    def _payload(self):
        import pandas as pd
        import numpy as np
        return {"cash": NAN, "inf": [math.inf], "np": np.float64("nan"), "series": pd.Series([1.0, NAN])}

    def test_json_dump_no_longer_writes_nan_literals(self):
        from hive_logger import SafeJSONEncoder
        buf = io.StringIO()
        json.dump(self._payload(), buf, cls=SafeJSONEncoder)
        assert _strict(buf.getvalue()) == {"cash": None, "inf": ["Inf"], "np": None, "series": {"0": 1.0, "1": None}}

    def test_dump_and_dumps_agree(self):
        from hive_logger import SafeJSONEncoder
        buf = io.StringIO()
        json.dump(self._payload(), buf, cls=SafeJSONEncoder, sort_keys=True)
        assert buf.getvalue() == json.dumps(self._payload(), cls=SafeJSONEncoder, sort_keys=True)

    def test_atomic_json_write_output_is_strict_json(self, tmp_path):
        from hive_logger import atomic_json_write
        p = tmp_path / "new_dir" / "c.json"          # 缺目录自己建（此前抛 FileNotFoundError 被吞成缓存静默失效）
        atomic_json_write(p, self._payload())
        assert _strict(p.read_text(encoding="utf-8"))["cash"] is None
        assert oct(p.stat().st_mode & 0o777) == "0o644"

    def test_atomic_json_write_leaves_no_temp_on_any_failure(self, tmp_path, monkeypatch):
        from hive_logger import atomic_json_write
        d = tmp_path / "cache"
        d.mkdir()
        p = d / "c.json"
        p.write_text('{"old": 1}', encoding="utf-8")

        def boom(*a, **k):
            raise RuntimeError("replace 失败")
        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(RuntimeError):
            atomic_json_write(p, {"new": 1})
        assert sorted(x.name for x in d.iterdir()) == ["c.json"] and _strict(p.read_text()) == {"old": 1}


# ─────────────────────────────── meta.json 是账本

class TestMetaIsALedger:

    def test_paper_portfolio_will_not_read_a_nan_cash(self, monkeypatch, tmp_path):
        import paper_portfolio as pp
        monkeypatch.setattr(pp, "META_FILE", tmp_path / "meta.json")
        pp.META_FILE.write_text('{\n  "cash": NaN,\n  "last_run_date": "2026-08-28"\n}', encoding="utf-8")  # 08-28 原样
        with pytest.raises(L.LedgerCorrupt):
            pp._load_meta()

    def test_paper_portfolio_will_not_write_a_nan_cash(self, monkeypatch, tmp_path):
        import paper_portfolio as pp
        monkeypatch.setattr(pp, "META_FILE", tmp_path / "meta.json")
        pp._save_meta({"cash": 1000.0})
        before = pp.META_FILE.read_bytes()
        with pytest.raises(L.LedgerWriteError):
            pp._save_meta({"cash": NAN})
        assert pp.META_FILE.read_bytes() == before

    def test_straddle_leg_corrupt_meta_stops_instead_of_resetting_cash(self, monkeypatch, tmp_path):
        import options_paper_leg as opl
        sd = tmp_path / "options_paper_state"
        for name, fn in (("STATE_DIR", sd), ("POSITIONS_FILE", sd / "positions.jsonl"),
                         ("CLOSED_FILE", sd / "closed_trades.jsonl"), ("EQUITY_FILE", sd / "equity_curve.jsonl"),
                         ("META_FILE", sd / "meta.json")):
            monkeypatch.setattr(opl, name, fn)
        sd.mkdir()
        opl.META_FILE.write_text('{"cash": 7321.5, "starting_date": "2026-09-01"', encoding="utf-8")   # 截断
        before = opl.META_FILE.read_bytes()
        with pytest.raises(L.LedgerCorrupt):
            opl.run_for_date("2026-10-09", quotes_fn=lambda t, s: {}, signals=[], closes_fn=lambda t, d: None)
        assert opl.META_FILE.read_bytes() == before, "坏 meta 不许被「新账本」覆盖（现金会被重置成起始资金）"
        assert not opl.EQUITY_FILE.exists()

    def test_greeks_corrupt_meta_stops_instead_of_resetting_cash(self, monkeypatch, tmp_path):
        import portfolio_greeks as pg
        monkeypatch.setattr(pg, "META_FILE", tmp_path / "meta.json")
        pg.META_FILE.write_text('{"cash": -5123.4', encoding="utf-8")
        with pytest.raises(L.LedgerCorrupt):
            pg._load_meta()

    @pytest.mark.parametrize("modname", ["options_paper_leg", "portfolio_greeks"])
    def test_scrubbing_modules_write_null_not_nan(self, monkeypatch, tmp_path, modname):
        """这两个模块的既定口径是落盘前 `_scrub`（NaN → None 并 error 日志），迁移不改口径，只保证落盘是合法 JSON。"""
        import importlib
        mod = importlib.import_module(modname)
        monkeypatch.setattr(mod, "META_FILE", tmp_path / "meta.json")
        mod._save_meta({"cash": NAN, "starting_date": None})
        assert _strict(mod.META_FILE.read_text(encoding="utf-8"))["cash"] is None


# ─────────────────────────────── 三本账：严格读 + 读-改-写持锁

class TestMigratedLedgers:

    @pytest.mark.parametrize("modname", ["options_paper_leg", "vrp_signal", "earnings_vol_signal"])
    def test_corrupt_line_is_no_longer_skipped(self, tmp_path, modname):
        import importlib
        mod = importlib.import_module(modname)
        p = tmp_path / "x.jsonl"
        p.write_text('{"date": "2026-10-08"}\n{"date": "2026-10-09", "iv": 0.3\n', encoding="utf-8")
        with pytest.raises(L.LedgerCorrupt, match="第 2 行"):
            mod._load_jsonl(p)

    @pytest.mark.parametrize("modname", ["vrp_signal", "earnings_vol_signal"])
    def test_writers_refuse_nan(self, tmp_path, modname):
        import importlib
        mod = importlib.import_module(modname)
        p = tmp_path / "x.jsonl"
        with pytest.raises(L.LedgerWriteError):
            mod._write_jsonl(p, [{"iv": NAN}])
        assert not p.exists()

    def test_straddle_closed_trade_append_goes_through_ledger_io(self, tmp_path):
        import options_paper_leg as opl
        p = tmp_path / "closed_trades.jsonl"
        p.write_text('{"a": 1}', encoding="utf-8")         # 末行缺换行（手改过）：不许粘行
        opl._append_jsonl(p, {"b": NAN})                   # _scrub ⇒ null
        assert L.load_jsonl(p) == [{"a": 1}, {"b": None}]

    @pytest.mark.parametrize("modname, fn", [("vrp_signal", "record_day"), ("vrp_signal", "settle"),
                                             ("earnings_vol_signal", "scan"),
                                             ("earnings_vol_signal", "settle_signals")])
    def test_read_modify_write_entrypoints_hold_the_lock(self, monkeypatch, tmp_path, modname, fn):
        import importlib
        mod = importlib.import_module(modname)
        sig = tmp_path / "state" / "signals.jsonl"
        monkeypatch.setattr(mod, "SIGNALS_FILE", sig)
        seen = []
        real = mod._load_jsonl

        def spy(path):
            seen.append(L.is_locked_here(sig.parent))
            return real(path)
        monkeypatch.setattr(mod, "_load_jsonl", spy)
        f = getattr(mod, fn)
        assert getattr(f, "__ledger_locked__", False)
        try:
            if fn == "scan":
                f("2026-10-09", cache_dir=tmp_path / "no_cache", upcoming_fn=lambda t: None)
            elif fn == "record_day":
                f("2026-10-09", cache_dir=tmp_path / "no_cache")
            else:
                f("2026-10-09", bars_fn=lambda t: None)
        except Exception:  # noqa: BLE001 - 只关心读账本那一刻有没有持锁
            pass
        assert seen and all(seen), (fn, seen)
        assert not L.is_locked_here(sig.parent)

    def test_straddle_run_holds_the_lock(self, monkeypatch, tmp_path):
        import options_paper_leg as opl
        monkeypatch.setattr(opl, "POSITIONS_FILE", tmp_path / "s" / "positions.jsonl")
        seen = []
        monkeypatch.setattr(opl, "_run_for_date",
                            lambda *a, **k: seen.append(L.is_locked_here(tmp_path / "s")) or {})
        opl.run_for_date("2026-10-09")
        assert seen == [True] and not L.is_locked_here(tmp_path / "s")
