"""财报跨式的 GEX 影子记录（v0.45.424）与预注册检验模块的守卫。每组回答一句「谁会红」：

  · 影子记录如实：可得照抄、不可得写原因、数值不填 0、记下 captured_on（`TestGexCtx`）；
  · **不影响开单**：开 / 关 gex_fn 两次跑出的信号（除 gex_ctx 外）与账本开单逐项相同（`TestShadowChangesNothing`）；
    全仓只有登记过的三个文件提到 gex_ctx，且 earnings_vol_signal 里只在记录它的三个函数里出现（`TestReaders`，含种病灶自证）；
  · 同日重跑以第一次可得记录为准；补跑旧日期拿到的记录不进检验（`TestSameDayCapture`）；
  · 检验协议：代表行规则、先定代表行再看 GEX、进度只给计数、未就绪拒跑、只冻结一次、分块置换有牙（`TestPrereg`）。
全部离线。
"""
from __future__ import annotations

import ast
import json
import math
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import earnings_vol_signal as evs
import straddle_gex_prereg as SP
from tests._repo_files import own_python_files
from tests.test_earnings_vol_signal import AS_OF, EARN, _qs, _stats, _write_snap

REPO = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
POST_CLOSE = datetime(2026, 9, 3, 18, 0, tzinfo=ET)      # 2026-09-03 周四，收盘后（生产扫描的时刻）
INTRADAY = datetime(2026, 9, 3, 11, 0, tzinfo=ET)        # 同一天盘中（有人手动跑日报）
POS_STATE = {"schema_version": 1, "available": True, "reason": None, "regime": "positive_gex", "total_gex": 12.5,
             "gex_flip": 95.0, "largest_call_wall": 110.0, "largest_put_wall": 90.0, "gex_normalized_pct": 0.8,
             "stock_price": 100.0, "chain_view": "cboe_full_expiries", "n_expiries": 9, "routing_applied": True}


@pytest.fixture
def state(tmp_path, monkeypatch):
    sd = tmp_path / "options_paper_state"
    monkeypatch.setattr(evs, "STATE_DIR", sd)
    monkeypatch.setattr(evs, "SIGNALS_FILE", sd / "earnings_signals.jsonl")
    monkeypatch.setattr(evs, "_today_pdt", lambda: AS_OF)
    monkeypatch.setattr(evs, "_now_et", lambda: POST_CLOSE)     # 钉死记录时刻：结果不能随跑测试的时钟变
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


def _scan(cache, gex_fn):
    return evs.scan(AS_OF, cache_dir=cache, stats_fn=lambda tk: _stats(),
                    upcoming_fn=lambda tk: {"earnings_date": EARN, "earnings_time": "AMC"}, gex_fn=gex_fn)


class TestGexCtx:
    def test_available_state_is_copied(self):
        c = evs.gex_ctx(lambda t: POS_STATE, "XYZ", AS_OF, captured_on=AS_OF, captured_at=POST_CLOSE)
        assert c["available"] is True and c["reason"] is None and c["regime"] == "positive_gex"
        assert c["total_gex"] == 12.5 and c["captured_on"] == AS_OF and c["as_of"] == AS_OF
        assert c["schema_version"] == evs.GEX_CTX_SCHEMA == 2 and "routing_applied" not in c
        assert c["captured_at"] == "2026-09-03T18:00:00-04:00" and c["session_live"] is False

    @pytest.mark.parametrize("when,live", [(INTRADAY, True), (POST_CLOSE, False),
                                           (datetime(2026, 9, 3, 9, 0, tzinfo=ET), False),          # 开盘前
                                           (datetime(2026, 9, 7, 11, 0, tzinfo=ET), False),         # 劳动节休市
                                           (datetime(2026, 11, 27, 13, 30, tzinfo=ET), False)])     # 感恩节次日 13:00 提前收盘
    def test_session_live_follows_the_calendar(self, when, live):
        assert evs.session_live_at(when) is live

    def test_session_unknown_when_calendar_breaks(self, monkeypatch):
        import is_trading_day as itd
        monkeypatch.setattr(itd, "is_trading_day", lambda d: (_ for _ in ()).throw(RuntimeError("cal")))
        assert evs.session_live_at(POST_CLOSE) is None

    @pytest.mark.parametrize("fn,why", [(None, "gex_fn_not_provided"), (lambda t: None, "no_gex_state"),
                                        (lambda t: (_ for _ in ()).throw(ValueError("x")), "exception:ValueError")])
    def test_missing_is_unavailable_with_reason(self, fn, why):
        c = evs.gex_ctx(fn, "XYZ", AS_OF, captured_on=AS_OF)
        assert c["available"] is False and c["reason"] == why

    def test_unavailable_state_has_no_numbers(self):
        st = {**POS_STATE, "available": False, "reason": "non_finite_total_gex", "total_gex": 0.0}
        c = evs.gex_ctx(lambda t: st, "XYZ", AS_OF, captured_on=AS_OF)
        assert c["available"] is False and c["reason"] == "non_finite_total_gex"
        assert all(c[k] is None for k in ("total_gex", "regime", "gex_flip")), "不可得时 0.0 是哨兵值，不能当数记"

    def test_signal_records_entry_net_delta(self):
        s = evs.compute_signal("XYZ", AS_OF, _qs(), {"earnings_date": EARN}, _stats(), None)
        assert s["straddle_net_delta"] == pytest.approx(0.0)          # 夹具两腿 Δ = +0.5 / −0.5
        qs = _qs()
        qs["contracts"]["atm_put"]["delta"] = None
        assert evs.compute_signal("XYZ", AS_OF, qs, {"earnings_date": EARN}, _stats(), None)["straddle_net_delta"] is None


class TestShadowChangesNothing:
    def test_signals_identical_except_gex_ctx(self, state, tmp_path, monkeypatch):
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        _write_snap(state, "ABC", AS_OF, _qs(c=(2.0, 2.2), p=(2.0, 2.2)), rv=20.0)
        off = _scan(state, None)
        monkeypatch.setattr(evs, "SIGNALS_FILE", tmp_path / "other.jsonl")
        on = _scan(state, lambda t: POS_STATE)
        strip = lambda rows: [{k: v for k, v in r.items() if k != "gex_ctx"} for r in rows]  # noqa: E731
        assert strip(off) == strip(on)
        assert {r["gex_ctx"]["available"] for r in on} == {True} and {r["gex_ctx"]["available"] for r in off} == {False}

    def test_ledger_opens_the_same_positions(self, state, tmp_path, monkeypatch):
        import options_paper_leg as opl
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        sigs = {}
        for name, fn in (("off", None), ("on", lambda t: POS_STATE)):
            monkeypatch.setattr(evs, "SIGNALS_FILE", tmp_path / f"{name}.jsonl")
            sigs[name] = _scan(state, fn)
        opened = {}
        for name in ("off", "on"):
            d = tmp_path / f"ledger_{name}"
            for attr, fname in (("STATE_DIR", ""), ("POSITIONS_FILE", "positions.jsonl"), ("CLOSED_FILE", "closed.jsonl"),
                                ("EQUITY_FILE", "equity.jsonl"), ("META_FILE", "meta.json")):
                monkeypatch.setattr(opl, attr, d / fname if fname else d)
            res = opl.run_for_date(AS_OF, quotes_fn=lambda t, syms: {}, signals=sigs[name], closes_fn=lambda t, d: None)
            opened[name] = res["opened_today"]
        assert opened["off"] and opened["off"] == opened["on"], "影子记录改变了开单"


class TestReaders:
    ALLOWED = {"earnings_vol_signal.py", "straddle_gex_prereg.py", "alphabot/straddle.py"}
    WRITERS = {"gex_ctx", "_usable_capture", "scan", "shadow_summary"}     # 记录 / 同日取舍 / 只数个数

    @staticmethod
    def _mentions(root):
        files, _how = own_python_files(root)
        out = set()
        for p in files:
            rel = p.relative_to(root)
            if rel.parts[0] in {"tests", "experiments"}:
                continue
            if "gex_ctx" in p.read_text(encoding="utf-8", errors="replace"):
                out.add(rel.as_posix())
        return out

    def test_only_registered_files_mention_gex_ctx(self):
        assert self._mentions(REPO) == self.ALLOWED

    @staticmethod
    def _funcs_reading(src: str) -> set:
        tree = ast.parse(src)
        hits = set()
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            body = ast.Module(body=fn.body, type_ignores=[])
            for n in ast.walk(body):
                if isinstance(n, ast.Constant) and n.value == "gex_ctx" or (isinstance(n, ast.Name) and n.id == "gex_ctx"):
                    hits.add(fn.name)
        return hits

    def test_decisions_never_read_it(self):
        src = (REPO / "earnings_vol_signal.py").read_text(encoding="utf-8")
        assert self._funcs_reading(src) <= self.WRITERS, self._funcs_reading(src)
        assert "scan" in self._funcs_reading(src), "探针要看得见真实的写入点，否则上一条恒真"
        assert "gex_ctx" not in (REPO / "options_paper_leg.py").read_text(encoding="utf-8")

    def test_has_teeth(self):
        src = (REPO / "earnings_vol_signal.py").read_text(encoding="utf-8")
        planted = src.replace('    sig["raw_label"] = raw\n',
                              '    sig["raw_label"] = raw\n    if (sig.get("gex_ctx") or {}).get("regime") == "negative_gex":\n        raw = "fair"\n', 1)
        assert planted != src
        assert "compute_signal" in self._funcs_reading(planted)


class TestWiringIsVisible:
    """v0.45.428：接线断了要有人红。日报钩子必须把 gex_fn 传给 scan、并把可得数打进日志；结果文件走 PATHS。"""

    @staticmethod
    def _hook_calls(src: str):
        tree = ast.parse(src)
        # 跨式账本那一段在 `_post_scan_notify` 里（不是 `_post_scan_enrichment`——v0.45.428 首版写错，
        # 而且下面的「有牙自证」当时在原文上也是空集、恒绿：自证必须先过正对照）
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_post_scan_notify")
        scan_kw, summary = set(), False
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr == "scan":
                    scan_kw |= {k.arg for k in n.keywords}
                summary |= n.func.attr == "shadow_summary"
        return scan_kw, summary

    def test_daily_hook_passes_gex_fn_and_logs_the_count(self):
        scan_kw, summary = self._hook_calls((REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        assert "gex_fn" in scan_kw and "upcoming_fn" in scan_kw, scan_kw
        assert summary, "日报钩子没有把影子记录的可得数打进日志"

    def test_wiring_guard_has_teeth(self):
        src = (REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        assert "gex_fn" in self._hook_calls(src)[0], "正对照：原文上必须先看得见 gex_fn，否则下面的「看不见」恒真"
        cut = src.replace(",\n                                    gex_fn=self._gex_state_from_swarm(swarm_results))", ")", 1)
        assert cut != src and "gex_fn" not in self._hook_calls(cut)[0]
        assert self._hook_calls(src.replace("_evs.shadow_summary(", "_evs.not_summary(", 1))[1] is False

    def test_shadow_summary_counts_and_reasons(self):
        ok = {"gex_ctx": evs.gex_ctx(lambda t: POS_STATE, "A", AS_OF, captured_on=AS_OF, captured_at=POST_CLOSE)}
        live = {"gex_ctx": evs.gex_ctx(lambda t: POS_STATE, "B", AS_OF, captured_on=AS_OF, captured_at=INTRADAY)}
        none = {"gex_ctx": evs.gex_ctx(None, "C", AS_OF, captured_on=AS_OF, captured_at=POST_CLOSE)}
        s = evs.shadow_summary([ok, live, none, {}], AS_OF)
        assert s == {"n": 4, "usable": 1, "reasons": {"intraday_capture": 1, "gex_fn_not_provided": 1, "no_gex_ctx": 1}}

    def test_result_path_is_the_paths_property(self, tmp_path):
        from hive_logger import PATHS
        assert SP._result_path() == PATHS.straddle_prereg_result
        assert SP._result_path().is_relative_to(tmp_path), "冻结结果的缺省位置逃出了测试沙箱"


class TestSameDayCapture:
    def test_first_available_capture_wins(self, state):
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        _scan(state, lambda t: POS_STATE)
        neg = {**POS_STATE, "regime": "negative_gex"}
        _scan(state, lambda t: neg)
        assert evs.load_signals()[0]["gex_ctx"]["regime"] == "positive_gex"

    def test_intraday_capture_gives_way_to_post_close(self, state, monkeypatch):
        """修订 1：盘中手动跑先记了一条，收盘后的生产扫描必须能替换它；之后再重跑，以第一条收盘后记录为准。"""
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        neg = {**POS_STATE, "regime": "negative_gex"}
        monkeypatch.setattr(evs, "_now_et", lambda: INTRADAY)
        first = _scan(state, lambda t: POS_STATE)[0]["gex_ctx"]
        assert first["session_live"] is True and SP._ctx_problem(first) == "intraday_capture"
        monkeypatch.setattr(evs, "_now_et", lambda: POST_CLOSE)
        _scan(state, lambda t: neg)
        assert evs.load_signals()[0]["gex_ctx"]["regime"] == "negative_gex", "盘中记录锁住了当天政体"
        _scan(state, lambda t: POS_STATE)
        assert evs.load_signals()[0]["gex_ctx"]["regime"] == "negative_gex"

    def test_scan_side_and_prereg_side_agree(self):
        """`_usable_capture`（扫描侧取舍）与 `_ctx_problem`（检验侧判据）是同一条规则的两份实现——逐个变体对齐。"""
        base = evs.gex_ctx(lambda t: POS_STATE, "XYZ", AS_OF, captured_on=AS_OF, captured_at=POST_CLOSE)
        variants = [base, {**base, "schema_version": 1}, {**base, "available": False}, {**base, "captured_on": "2026-09-04"},
                    {**base, "session_live": True}, {**base, "session_live": None}, {**base, "regime": "unknown"}, None, {}]
        for v in variants:
            assert evs._usable_capture(v, AS_OF) is (SP._ctx_problem(v) is None), v

    def test_backfill_capture_does_not_count(self, state, monkeypatch):
        monkeypatch.setattr(evs, "_today_pdt", lambda: "2026-09-20")      # 补跑：记录日 ≠ 信号日
        _write_snap(state, "XYZ", AS_OF, _qs(), rv=20.0)
        row = _scan(state, lambda t: POS_STATE)[0]
        assert row["gex_ctx"]["captured_on"] == "2026-09-20" and not SP._ctx_ok(row["gex_ctx"])
        assert SP.units([row])[0]["status"].startswith("gex_unusable")


def _row(tk, as_of, ed, *, implied=4.0, regime="positive_gex", realized=None, ok=True, captured=None, live=False):
    ctx = {"schema_version": 2, "available": ok, "regime": regime if ok else None, "as_of": as_of,
           "captured_on": captured or as_of, "session_live": live, "reason": None if ok else "state_unavailable"}
    return {"ticker": tk, "as_of": as_of, "earnings_date": ed, "eligible": True, "implied_event_move_pct": implied,
            "ratio": 1.0, "realized_abs_move_pct": realized, "gex_ctx": ctx}


class TestPrereg:
    def test_representative_row_skips_floored_rows(self):
        rows = [_row("MU", "2026-09-04", "2026-09-30", implied=0.0, regime="negative_gex"),
                _row("MU", "2026-09-14", "2026-09-30", implied=5.79, regime="positive_gex", realized=3.03)]
        u = SP.units(rows)[0]
        assert u["as_of"] == "2026-09-14" and u["regime"] == "positive_gex" and u["status"] == "ok"
        assert u["y"] == pytest.approx(math.log(3.03 / 5.79))

    @pytest.mark.parametrize("change,want", [
        ({"schema_version": 1}, "gex_unusable:schema:1"),
        ({"session_live": True}, "gex_unusable:intraday_capture"),
        ({"session_live": None}, "gex_unusable:session_unknown"),
        ({"captured_on": "2026-09-20"}, "gex_unusable:captured_on≠as_of"),
        ({"regime": "unknown"}, "gex_unusable:regime:unknown"),
        ({"available": False, "reason": "non_finite_total_gex"}, "gex_unusable:unavailable:non_finite_total_gex"),
    ])
    def test_status_names_the_actual_reason(self, change, want):
        """v0.45.428：此前可得但不合格的记录一律报成 captured_on≠as_of——排查的人会去查一个不存在的日期问题。"""
        r = _row("A", "2026-09-01", "2026-09-10")
        r["gex_ctx"].update(change)
        assert SP.units([r])[0]["status"] == want

    def test_gex_is_checked_after_picking_the_row(self):
        rows = [_row("T", "2026-09-29", "2026-10-21", ok=False), _row("T", "2026-09-30", "2026-10-21")]
        assert SP.units(rows)[0]["status"].startswith("gex_unusable"), "不许往后找一条 GEX 可用的行顶上"

    def test_progress_is_counts_only(self):
        rows = [_row("A", "2026-09-01", "2026-09-10", realized=3.0), _row("B", "2026-09-01", "2026-09-11", regime="negative_gex", realized=5.0)]
        p = SP.progress(rows, root=Path("/nonexistent-root"))
        assert not (SP.BLINDED_KEYS & set(json.dumps(p).replace('"', " ").split())) and "y" not in p
        assert p["n_informative"] == {"positive_gex": 1, "negative_gex": 1, "blocks": 1} and p["ready"] is False

    def _ready_rows(self):
        rng = random.Random(7)
        rows = []
        for w in range(10):                                  # 10 个财报周，每周 4 正 4 负
            ed = f"2026-{1 + w // 4:02d}-{5 + 7 * (w % 4):02d}"
            for i in range(8):
                rows.append(_row(f"T{w}{i}", "2025-12-20", ed, regime="positive_gex" if i < 4 else "negative_gex",
                                 realized=4.0 * math.exp(rng.gauss(0, 0.3))))
        return rows

    def test_run_once_refuses_until_ready_then_freezes_once(self, tmp_path):
        assert SP.run_once(self._ready_rows()[:10], root=tmp_path)["reason"] == "not_ready"
        assert not list(tmp_path.rglob(SP.RESULT_NAME))
        first = SP.run_once(self._ready_rows(), root=tmp_path, today="2026-12-01")
        assert first["testable"] and "p_value" in first
        again = SP.run_once(self._ready_rows()[:5], root=tmp_path)
        assert again["already_frozen"] and again["p_value"] == first["p_value"]

    def test_blocking_has_teeth(self):
        """政体只与「坏周」相关、周内无关：分块置换 p 不小，全局置换 p 很小（横截面池化陷阱的合成证明）。"""
        rng = random.Random(3)
        us = []
        for w in range(12):
            calm = w % 2 == 0                                 # 平静周：多数正 gamma、实际波动小
            for i in range(10):
                pos = i < (8 if calm else 2)
                y = (-0.6 if calm else 0.4) + rng.gauss(0, 0.2)
                us.append({"block": f"2026-W{w:02d}", "regime": "positive_gex" if pos else "negative_gex", "y": y})
        blocked = SP.blocked_permutation_test(us, n_perm=2000, seed=1)["p_value"]
        ys = [u["y"] for u in us]
        labels = [u["regime"] for u in us]
        obs = (sum(y for y, r in zip(ys, labels) if r == "positive_gex") / labels.count("positive_gex")
               - sum(y for y, r in zip(ys, labels) if r == "negative_gex") / labels.count("negative_gex"))
        n_le = 0
        for _ in range(2000):
            rng.shuffle(labels)
            t = (sum(y for y, r in zip(ys, labels) if r == "positive_gex") / labels.count("positive_gex")
                 - sum(y for y, r in zip(ys, labels) if r == "negative_gex") / labels.count("negative_gex"))
            n_le += t <= obs
        global_p = (1 + n_le) / 2001
        assert global_p < 0.01 and blocked > 0.1, (global_p, blocked)
