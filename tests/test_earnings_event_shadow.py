"""v0.45.454 财报隐含事件波动：算不出写 None 不写 0；远期 IV 法影子字段 `shadow_event`。每组回答一句「谁会红」：

  · 触底（事件² ≤ 0）：`implied_event_move_pct` / `ratio` / `raw_label` 是 None，不是 0.0 / "cheap"；
    仍「不可交易」；实际波动照样回填（`TestFloorIsNoneNotZero`）；
  · 影子数学：手算值逐项对上；财报前 / 边界到期日被排除；点不够 / 远期方差非正 → `available=False`+原因，
    数值是 None 不是 0（`TestEventShadowMath`、`TestEventShadowDegrades`）；
  · **不影响判断**：有 / 没有期限结构，两次 scan 的信号除 `shadow_event` 外逐项相同；影子计算抛异常也不拖死主信号
    （`TestShadowChangesNothing`）；
  · 只有登记过的文件提到 `shadow_event`，且 earnings_vol_signal 里只有记录它的函数读它，含种病灶自证（`TestReaders`）。
全部离线。
"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

import earnings_vol_signal as evs
from tests._repo_files import own_python_files
from tests.test_earnings_vol_signal import AS_OF, UP, _qs, _stats

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def state(tmp_path, monkeypatch):
    sd = tmp_path / "options_paper_state"
    monkeypatch.setattr(evs, "STATE_DIR", sd)
    monkeypatch.setattr(evs, "SIGNALS_FILE", sd / "earnings_signals.jsonl")
    cache = tmp_path / "cache"
    cache.mkdir()
    return cache


def _ts(*pts):
    """期限结构：(expiry, atm_iv) 列表。刻意不给 dte——影子只信自己用 as_of 算的天数。"""
    return {"data_available": True, "source": "cboe",
            "term_structure": [{"expiry": e, "atm_iv": iv} for e, iv in pts]}


# 2026-09-03 → 财报 09-20（盘后，事件在 09-21）；跨式 10%；dte 29
TS_OK = _ts(("2026-09-18", 60.0),      # 财报前：必须排除（它含的是别的波动）
            ("2026-10-02", 40.0),      # t1 = 29
            ("2026-10-23", 36.0))      # t2 = 50


def _sig(rv=20.0, median=5.0):
    return evs.compute_signal("XYZ", AS_OF, _qs(), UP, _stats(median=median), rv_30d=rv)


def _no_nan(obj):
    json.dumps(obj, allow_nan=False)


class TestFloorIsNoneNotZero:

    def test_floor_row_has_none_not_zero(self):
        s = _sig(rv=60.0)                                    # 扩散 16.3 > 跨式 10 → 触底
        assert s["event_move_floor_hit"] is True
        assert s["implied_event_move_pct"] is None and s["ratio"] is None and s["raw_label"] is None
        assert s["eligible"] is True and s["tradeable"] is False and s["label"] == "untradeable"
        assert "floored" in s["untradeable_reason"]
        _no_nan(s)

    def test_floor_row_still_settles_realized_move(self, state):
        """触底行的实际波动是校准研究的样本：implied 为 None 不许让它被 settle 漏掉；realized_ratio 为 None。"""
        evs.STATE_DIR.mkdir(parents=True)
        floored = _sig(rv=60.0)
        assert floored["implied_event_move_pct"] is None
        evs._write_jsonl(evs.SIGNALS_FILE, [floored])
        bars = [{"date": "2026-09-18", "close": 100.0}, {"date": "2026-09-20", "close": 102.0},
                {"date": "2026-09-21", "close": 104.0}]
        assert evs.settle_signals("2026-09-25", bars_fn=lambda tk: bars) == 1
        row = evs.load_signals()[0]
        assert row["realized_abs_move_pct"] == pytest.approx(4.0) and row["realized_ratio"] is None

    def test_non_floor_row_unchanged(self):
        s = _sig(rv=20.0)
        assert s["implied_event_move_pct"] == pytest.approx(9.0384, abs=1e-3)
        assert s["ratio"] == pytest.approx(1.8077, abs=1e-3) and s["raw_label"] == "rich"


class TestEventShadowMath:

    def test_hand_computed_forward_vol_event_move(self):
        # σ_fwd² = (50·36² − 29·40²)/(50−29) = 876.19 → σ_fwd = 29.6005
        # 扩散(29d) = 0.8·0.296005·√(29/365)·100 = 6.67485；窗口(2 交易日) = 2.10962
        # 事件 = √(100 − 44.5536 + 4.4504) = 7.73930；比值 = 7.7393/5.0 = 1.54786 → rich
        sh = evs.event_shadow(_sig(), TS_OK)
        assert sh["method"] == "fwd_iv_v1" and sh["schema_version"] == 1 and sh["available"] is True
        assert sh["fwd_iv_pct"] == pytest.approx(29.6005, abs=1e-3)
        assert sh["fwd_pair"] == ["2026-10-02", "2026-10-23"]
        assert sh["diffusion_move_pct"] == pytest.approx(6.6749, abs=1e-3)
        assert sh["diffusion_window_move_pct"] == pytest.approx(2.1096, abs=1e-3)
        assert sh["implied_event_move_pct"] == pytest.approx(7.7393, abs=1e-3)
        assert sh["ratio"] == pytest.approx(1.5479, abs=1e-3) and sh["raw_label"] == "rich"
        assert sh["event_move_floor_hit"] is False
        _no_nan(sh)

    def test_pre_event_expiries_are_ignored(self):
        """财报前的 60% 那一点若被拿来配对，σ_fwd 会是另一个数（且含事件之前的别的波动）。"""
        with_pre = evs.event_shadow(_sig(), TS_OK)
        without = evs.event_shadow(_sig(), _ts(("2026-10-02", 40.0), ("2026-10-23", 36.0)))
        assert with_pre == without

    def test_boundary_expiry_must_be_two_days_after_earnings(self):
        """财报 09-20 盘后 → 事件在 09-21；到期日 09-21 的那点含事件，09-22 才算「之后」。"""
        s = _sig()
        on_d1 = evs.event_shadow(s, _ts(("2026-09-21", 50.0), ("2026-10-02", 40.0), ("2026-10-23", 36.0)))
        assert on_d1["fwd_pair"] == ["2026-10-02", "2026-10-23"]
        on_d2 = evs.event_shadow(s, _ts(("2026-09-22", 38.0), ("2026-10-02", 40.0), ("2026-10-23", 36.0)))   # 38/40：远期方差为正
        assert on_d2["fwd_pair"] == ["2026-09-22", "2026-10-02"]

    def test_days_come_from_as_of_not_from_term_structure_dte(self):
        """期限结构自带的 dte 与 quote_set 的 selected_dte 差过一天（时钟不同）；喂一个胡写的 dte 不许影响结果。"""
        ts = _ts(("2026-10-02", 40.0), ("2026-10-23", 36.0))
        for x in ts["term_structure"]:
            x["dte"] = 7
        assert evs.event_shadow(_sig(), ts) == evs.event_shadow(_sig(), TS_OK)

    def test_floor_when_forward_diffusion_exceeds_straddle(self):
        sh = evs.event_shadow(_sig(), _ts(("2026-10-02", 80.0), ("2026-10-23", 78.0)))   # 扩散 16.9 > 10
        assert sh["available"] is True and sh["event_move_floor_hit"] is True
        assert sh["implied_event_move_pct"] is None and sh["ratio"] is None and sh["raw_label"] is None
        assert sh["fwd_iv_pct"] == pytest.approx(75.1506, abs=1e-3)
        _no_nan(sh)

    def test_label_thresholds_are_the_main_signals(self):
        """同一套 1.30 / 0.75：改了主信号的阈值，影子跟着变，两边不会各用各的。"""
        sh = evs.event_shadow(_sig(median=20.0), TS_OK)       # 7.74 / 20 = 0.387
        assert sh["raw_label"] == "cheap"
        sh = evs.event_shadow(_sig(median=7.0), TS_OK)        # 7.74 / 7 = 1.106
        assert sh["raw_label"] == "fair"


class TestEventShadowDegrades:
    """算不出 → available=False + reason + 数值 None。从不填 0。"""

    @pytest.mark.parametrize("ts,reason", [
        (None, "term structure unavailable"),
        ({}, "term structure unavailable"),
        ({"term_structure": []}, "term structure unavailable"),
        ({"term_structure": "garbage"}, "term structure unavailable"),
        (_ts(("2026-10-02", 40.0)), "fewer than 2 post-event expiries"),
        (_ts(("2026-09-18", 60.0), ("2026-09-19", 55.0), ("2026-10-02", 40.0)), "fewer than 2 post-event expiries"),
        (_ts(("2026-10-02", 0.0), ("2026-10-23", 36.0)), "fewer than 2 post-event expiries"),      # iv 非正的点不算
        (_ts(("2026-10-02", 40.0), ("2026-10-23", 30.0)), "forward variance non-positive"),     # (50·900 − 29·1600) < 0
        # 两个同一天的到期日：t2 == t1
        (_ts(("2026-10-02", 40.0), ("2026-10-02", 36.0)), "expiry days not increasing"),
    ])
    def test_unavailable(self, ts, reason):
        sh = evs.event_shadow(_sig(), ts)
        assert sh["available"] is False and sh["reason"] == reason
        for k in ("fwd_iv_pct", "implied_event_move_pct", "ratio", "raw_label", "diffusion_move_pct"):
            assert sh[k] is None, k
        _no_nan(sh)

    def test_malformed_points_are_skipped_not_fatal(self):
        ts = {"term_structure": ["x", None, {"expiry": "oops", "atm_iv": 40}, {"expiry": "2026-10-02", "atm_iv": "n/a"},
                                 {"expiry": "2026-10-02", "atm_iv": 40.0}, {"expiry": "2026-10-23", "atm_iv": 36.0}]}
        assert evs.event_shadow(_sig(), ts)["available"] is True

    def test_ineligible_signal_gets_none(self):
        bad = evs.compute_signal("BAD", AS_OF, _qs(put_ok=False), UP, _stats(), None)
        assert bad["eligible"] is False and evs.event_shadow(bad, TS_OK) is None

    def test_exception_is_visible_and_does_not_escape(self, monkeypatch, caplog):
        sig = _sig()                       # 主信号先算好（它自己也用 _raw_label），再让影子这一路炸
        monkeypatch.setattr(evs, "_raw_label", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
        with caplog.at_level(logging.WARNING):
            sh = evs.event_shadow(sig, TS_OK)
        assert sh["available"] is False and sh["reason"].startswith("error:") and "boom" in sh["reason"]
        assert any("影子事件波动计算失败" in r.getMessage() for r in caplog.records), "失败要看得见（谁会红）"


class TestShadowChangesNothing:

    @staticmethod
    def _snap(cache, ts):
        (cache / f"options_snapshot_XYZ_{AS_OF}.json").write_text(
            json.dumps({"ticker": "XYZ", "quote_set": _qs(), "rv_30d": 20.0, "iv_term_structure": ts}), encoding="utf-8")

    def _run(self, cache):
        return evs.scan(AS_OF, cache_dir=cache, stats_fn=lambda tk: _stats(median=5.0),
                        upcoming_fn=lambda tk: dict(UP))

    def test_scan_attaches_shadow_and_everything_else_is_identical(self, state):
        self._snap(state, TS_OK)
        with_ts = self._run(state)[0]
        assert with_ts["shadow_event"]["available"] is True
        self._snap(state, None)
        without = self._run(state)[0]
        assert without["shadow_event"]["available"] is False
        a, b = dict(with_ts), dict(without)
        a.pop("shadow_event"), b.pop("shadow_event")
        a.pop("gex_ctx"), b.pop("gex_ctx")
        assert a == b, "影子只许多一个字段，判断 / 标签 / 开仓用的值不许因它而变"

    def test_a_crashing_shadow_does_not_change_the_main_signal(self, state, monkeypatch):
        self._snap(state, TS_OK)
        ok = self._run(state)[0]
        calls = {"n": 0}

        def flaky(r):      # 第 1 次是主信号（compute_signal）用，第 2 次起是影子用——只让影子炸
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("shadow only")
            return "rich" if r >= 1.30 else "cheap" if r <= 0.75 else "fair"
        monkeypatch.setattr(evs, "_raw_label", flaky)
        hurt = self._run(state)[0]
        assert hurt["shadow_event"]["available"] is False and "shadow only" in hurt["shadow_event"]["reason"]
        for k in ("label", "raw_label", "ratio", "tradeable", "implied_event_move_pct", "eligible"):
            assert hurt[k] == ok[k], k

    def test_ineligible_rows_carry_none(self, state):
        (state / f"options_snapshot_XYZ_{AS_OF}.json").write_text(
            json.dumps({"ticker": "XYZ", "quote_set": _qs(available=False), "iv_term_structure": TS_OK}), encoding="utf-8")
        r = self._run(state)[0]
        assert r["eligible"] is False and r["shadow_event"] is None


class TestReaders:
    """`shadow_event` 是只记录的字段：除登记过的文件外没人提它；earnings_vol_signal 里没有任何函数**读**它
    （写 = 字典字面量的键 / 下标赋值；读 = 其余一切出现）。"""
    ALLOWED_FILES = {"earnings_vol_signal.py"}

    @staticmethod
    def _mentions(root):
        files, _how = own_python_files(root)
        out = set()
        for p in files:
            rel = p.relative_to(root)
            if rel.parts[0] in {"tests", "experiments", ".claude"}:
                continue
            if "shadow_event" in p.read_text(encoding="utf-8", errors="replace"):
                out.add(rel.as_posix())
        return out

    @staticmethod
    def _access(src: str):
        """→ (读它的函数集合, 写它的函数集合)。"""
        tree = ast.parse(src)
        reads, writes = set(), set()
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            write_nodes = set()
            for n in ast.walk(ast.Module(body=fn.body, type_ignores=[])):
                if isinstance(n, ast.Dict):
                    write_nodes |= {id(k) for k in n.keys if isinstance(k, ast.Constant) and k.value == "shadow_event"}
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.slice, ast.Constant) \
                        and n.slice.value == "shadow_event":
                    write_nodes.add(id(n.slice))
            for n in ast.walk(ast.Module(body=fn.body, type_ignores=[])):
                if (isinstance(n, ast.Constant) and n.value == "shadow_event") or (isinstance(n, ast.Name) and n.id == "shadow_event"):
                    (writes if id(n) in write_nodes else reads).add(fn.name)
        return reads, writes

    def test_only_registered_files_mention_it(self):
        assert self._mentions(REPO) == self.ALLOWED_FILES

    def test_no_function_reads_it(self):
        src = (REPO / "earnings_vol_signal.py").read_text(encoding="utf-8")
        reads, writes = self._access(src)
        assert reads == set(), reads
        # v0.45.380：`scan` 拆成转发（scan）+ 本体（_scan_with_info），后者才是写入点
        assert {"_scan_with_info", "compute_signal"} <= writes, "探针要看得见真实的写入点，否则上一条恒真"
        assert "scan" not in writes, "scan 应只是转发；它又开始写 shadow_event 就该回来对一遍白名单"
        assert "shadow_event" not in (REPO / "options_paper_leg.py").read_text(encoding="utf-8")

    def test_probe_has_teeth(self):
        src = (REPO / "earnings_vol_signal.py").read_text(encoding="utf-8")
        planted = src.replace('    sig["raw_label"] = raw\n',
                              '    sig["raw_label"] = raw\n    if (sig.get("shadow_event") or {}).get("raw_label") == "rich":\n        raw = "rich"\n', 1)
        assert planted != src
        assert "compute_signal" in self._access(planted)[0]


class TestAlphaBotPassesTheFlag:
    """页面靠 `event_move_floor_hit` 把触底行显示成「—」（新行存 None，**已落盘的旧行存 0.0**，不能靠值本身判）。
    服务层只透传白名单字段——这个键不在里面，页面那段判断就是死代码。"""

    def test_signals_today_carries_floor_flag_for_legacy_zero_and_new_none_rows(self):
        from alphabot import straddle as ab
        legacy = {"ticker": "OLD", "as_of": "2026-10-09", "eligible": True, "label": "untradeable", "raw_label": "cheap",
                  "ratio": 0.0, "implied_event_move_pct": 0.0, "event_move_floor_hit": True, "tradeable": False}
        new = {"ticker": "NEW", "as_of": "2026-10-09", "eligible": True, "label": "untradeable", "raw_label": None,
               "ratio": None, "implied_event_move_pct": None, "event_move_floor_hit": True, "tradeable": False}
        out = ab.signals_today([legacy, new], "2026-10-09", set(), [])["eligible"]
        assert {r["ticker"]: r["event_move_floor_hit"] for r in out} == {"OLD": True, "NEW": True}
        json.dumps(out, allow_nan=False)


class TestPreregUnitsTreatNoneLikeLegacyZero:
    """v0.45.454 把触底行的 implied 从 0.0 改成 None。预注册检验的「代表行 = 隐含事件波动 > 0 的最早合格行」
    必须对两种写法一视同仁——否则就是悄悄改了检验的单位定义（协议 §8）。已落盘的旧行是 0.0，新行是 None，两者会同时存在。"""

    @pytest.mark.parametrize("floored", [0.0, None])
    def test_floored_row_is_skipped_either_way(self, floored):
        import straddle_gex_prereg as SP
        from tests.test_straddle_gex_shadow import _row
        rows = [_row("MU", "2026-09-04", "2026-09-30", implied=floored, regime="negative_gex"),
                _row("MU", "2026-09-14", "2026-09-30", implied=5.79, regime="positive_gex", realized=3.03)]
        u = SP.units(rows)[0]
        assert u["as_of"] == "2026-09-14" and u["regime"] == "positive_gex" and u["status"] == "ok"

    @pytest.mark.parametrize("floored", [0.0, None])
    def test_only_floored_rows_means_no_usable_signal(self, floored):
        import straddle_gex_prereg as SP
        from tests.test_straddle_gex_shadow import _row
        u = SP.units([_row("MU", "2026-09-04", "2026-09-30", implied=floored)])[0]
        assert u["status"] == "no_usable_signal"
