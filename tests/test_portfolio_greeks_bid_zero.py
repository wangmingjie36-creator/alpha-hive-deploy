"""v0.45.609 bid = 0 的深度虚值腿不再让整个组合 β·$Delta 变 unknown（portfolio_greeks）。

事故：2026-10-21 T 的空头看涨腿（比现价高 12%、bid 0.00 / ask 0.03）`quote_ok` 为假 ⇒ 整行被当「缺报价」、
Greeks 全丢 ⇒ 汇总里 1 行 β·$Delta 缺 ⇒ band_status="unknown" ⇒ 当天整个组合不对冲，而 CBOE 其实给了 delta=0.05。
修法只放宽**对冲 Greeks 汇总**这一处：bid = 0、ask 真实、iv > 0、四个 Greeks 齐全 ⇒ Greeks 照进汇总、行上标
`bid_zero`、mid 仍为 None。卖权账本 / 跨式入场用的 `quote_ok`（25% 点差上限等）一个字不碰——下面有断言。

v0.45.620（二次检查 v0.45.609）：判据收到 `cboe_options.bid_zero_greeks_usable` 一处，对冲账本与 Alpha Bot 跨式页共用；
跨式页账本视图也标 `degraded_legs`；对冲页 / 覆盖行显示 bid_zero 腿数；「只许自用」守卫改为递归扫全仓。

全部离线：价格 / 报价 / β 一律注入。"""

import ast
import collections
import functools
import itertools
import math
import os
import pathlib

import pytest

import cboe_options
import portfolio_greeks as pg
from tests._repo_files import walk_own_python_files
from alphabot import hedge as hedge_page
from alphabot import straddle as sd
from greeks_engine import calculate_single

AS_OF = "2026-09-03"
EXPIRY = "2026-10-02"
S, K, IV = 100.0, 100.0, 0.40
CALL, PUT = "XYZ261002C00100000", "XYZ261002P00100000"


def _position(side="short", contracts=39):
    return {"ticker": "XYZ", "side": side, "entry_date": "2026-09-01", "expiry": EXPIRY, "strike": K,
            "call_symbol": CALL, "put_symbol": PUT, "contracts": contracts, "entry_call": 4.0, "entry_put": 3.8,
            "entry_premium": 7.8, "entry_underlying": 100.0, "earnings_date": "2026-09-20",
            "signal_ratio": 0.5, "label": "rich", "size_usd": 7.8 * 100 * contracts, "last_mark": 7.8,
            "last_mark_date": "2026-09-01", "mark_source": "cboe_mid"}


def _good(cp):
    g = calculate_single(S, K, 29 / 365.0, pg.CONFIG["risk_free"], IV, cp)
    mid = round(g["price"], 4)
    return {"bid": round(mid - 0.05, 4), "ask": round(mid + 0.05, 4), "mid": mid, "iv": IV,
            "delta": g["delta"], "gamma": g["gamma"], "vega": g["vega"], "theta": g["theta"],
            "dte": 29, "quote_ok": True, "role": "held"}


def _bid_zero(**over):
    """CBOE 的深度虚值腿：bid 0.00 / ask 0.03，`_qs_contract` 因此 quote_ok=False、mid=None，Greeks 与 iv 仍在。"""
    q = {"bid": 0.0, "ask": 0.03, "mid": None, "iv": 0.3065, "delta": 0.0519, "gamma": 0.094, "vega": 0.0037,
         "theta": -0.0068, "dte": 29, "quote_ok": False, "role": "held"}
    q.update(over)
    return q


def _rows(call_quote, side="short"):
    table = {CALL: call_quote, PUT: _good("put")}
    return pg.option_exposures(AS_OF, positions=[_position(side)], quotes_fn=lambda tk, syms: {s: table.get(s) for s in syms},
                               closes_fn=lambda tk, d: S, beta_fn=lambda tk, d: (1.0, "ols60"))


def _call(rows):
    return next(r for r in rows if r["cp"] == "call")


class TestBidZeroLegEntersGreeks:
    def test_row_is_not_quote_missing_and_keeps_delta(self):
        c = _call(_rows(_bid_zero()))
        assert c["quote_missing"] is False and c["bid_zero"] is True
        assert c["mid"] is None                                   # bid=0 的 mid 是 ask 的一半，不是市价——不编
        qty = -3900.0
        assert c["qty"] == qty
        assert c["dollar_delta"] == pytest.approx(qty * 0.0519 * S, abs=0.01)
        assert c["vega_dollar_per_pt"] == pytest.approx(qty * 0.0037, abs=0.01)
        assert c["theta_dollar_per_day"] == pytest.approx(qty * -0.0068, abs=0.01)
        assert c["gamma_dollar_per_1pct"] == pytest.approx(0.5 * qty * 0.094 * (0.01 * S) ** 2, abs=0.01)
        assert c["iv"] == pytest.approx(0.3065)

    def test_band_no_longer_unknown_and_sums_include_the_leg(self):
        rows = _rows(_bid_zero())
        agg = pg.aggregate(rows, nav=150_000.0)
        assert agg["coverage"]["n_quote_missing"] == 0 and agg["band_status"] != "unknown"
        put = next(r for r in rows if r["cp"] == "put")
        assert agg["sums"]["dollar_delta"] == pytest.approx(_call(rows)["dollar_delta"] + put["dollar_delta"], abs=0.02)

    def test_price_check_lists_the_bid_zero_leg_without_calling_it_a_gap(self):
        rows = _rows(_bid_zero())
        agg = pg.aggregate(rows, nav=150_000.0)
        rec = pg.hedge_recommendation(agg, 700.0, 150_000.0)
        chk = pg._price_check(AS_OF, rows, {"price": 700.0}, rec)
        assert chk["n_bid_zero"] == 1 and chk["bid_zero"] == [CALL]
        assert chk["n_quote_missing"] == 0 and chk["gaps"] == []   # 不是缺：不进告警清单

    def test_normal_rows_carry_no_bid_zero_key(self):
        rows = _rows(_good("call"))
        assert all("bid_zero" not in r for r in rows)
        assert pg._price_check(AS_OF, rows, {"price": 700.0}, None)["n_bid_zero"] == 0

    def test_stress_still_excludes_it_and_says_why(self):
        """压力表的基准价是 mid；bid=0 没有 mid ⇒ 这一行仍被剔（网格 partial、不编一个 mid），但标签说清是 bid=0。"""
        st = pg.stress_table(_rows(_bid_zero()))
        assert st["partial"] is True
        assert any(CALL in e and "mid(bid=0)" in e for e in st["excluded"])
        assert st["excluded_dd_unknown"] == 0                      # 现在 $Delta 是知道的，剔除规模按毛额记账
        assert st["excluded_dollar_delta"] == pytest.approx(abs(_call(_rows(_bid_zero()))["dollar_delta"]), abs=0.02)


class TestStillMissingWhenGreeksCannotBeTrusted:
    """只放宽「bid=0 且 Greeks 可信」这一种；其他不合格的报价照旧判缺。"""

    @pytest.mark.parametrize("over", [
        {"iv": 0.0},                       # iv ≤ 0：CBOE 的全零占位，不是观测值
        {"iv": None},
        {"ask": 0.0},                      # 没有真实卖价
        {"ask": None},
        {"delta": None},
        {"theta": float("nan")},
        {"bid": 0.05, "ask": 0.03},        # 倒挂（bid>0 的不合格报价不在放宽范围内）
        {"bid": 0.02, "ask": 0.40},        # bid>0 但 quote_ok 被判假（例如点差过宽的其他原因）：不放宽
        {"bid": None},
    ])
    def test_unusable_bid_zero_variants_stay_missing(self, over):
        c = _call(_rows(_bid_zero(**over)))
        assert c["quote_missing"] is True and c["dollar_delta"] is None and "bid_zero" not in c

    def test_none_quote_stays_missing(self):
        assert _call(_rows(None))["quote_missing"] is True


class TestStrictQuoteOkRuleUntouched:
    """卖权 / 跨式入场共用的 `quote_ok`（bid > 0 …）一个字不动：这次只在 portfolio_greeks 的 Greeks 汇总里放宽。"""

    def test_qs_contract_still_rejects_bid_zero(self):
        q = cboe_options._qs_contract({"bid": 0.0, "ask": 0.03, "iv": 0.3, "delta": 0.05, "gamma": 0.09,
                                       "vega": 0.004, "theta": -0.007}, role="held", cp="C", strike=24.5,
                                      expiry="2026-10-30", dte=8)
        assert q["quote_ok"] is False and q["mid"] is None and q["spread_pct"] is None

    def test_predicate_has_one_definition_in_cboe_options(self):
        """判据只有一份：cboe_options 里定义，portfolio_greeks / alphabot.straddle 用的是同一个函数对象。"""
        assert pg.bid_zero_greeks_usable is cboe_options.bid_zero_greeks_usable
        assert sd.bid_zero_greeks_usable is cboe_options.bid_zero_greeks_usable
        assert not hasattr(pg, "_bid_zero_greeks_usable")

    def test_no_second_copy_of_the_predicate_anywhere(self):
        """递归扫全仓（含 alphabot/ 等子目录）：除 cboe_options 外，没有别处再定义 / 复制这个判据。
        上一版只 glob 顶层 *.py，alphabot/ 里复制一份它也是绿的。iCloud 重名副本（`xxx 2.py` / `xxx 2/`）不计。"""
        defs, importers = {}, {}
        for rel, tree in _parsed_repo():
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and "bid_zero_greeks_usable" in n.name:
                    defs.setdefault(rel, []).append(n.name)
                if isinstance(n, ast.ImportFrom) and n.module == "cboe_options" and \
                        any(a.name == "bid_zero_greeks_usable" for a in n.names):
                    importers[rel] = n.lineno
        assert set(defs) == {"cboe_options.py"}, defs
        assert set(importers) == {"portfolio_greeks.py", "alphabot/straddle.py"}, importers

    def test_no_renamed_copy_reads_the_same_quote_fields(self):
        """按**读了哪几个字段**认，不按名字认：生产代码里任何一个函数同时读 bid / ask / iv 三个字符串键，就是在
        重新解释一张期权报价——要么用 `bid_zero_greeks_usable`，要么登记在 `_QUOTE_FIELD_READERS`（写明它为什么合法）。
        上一版按函数名认，而最初那份重复恰恰是改了名的副本。键是**限定名**（`Class.method` / `outer.<locals>.inner`，
        同一文件里重名再加 `#2`），否则已登记文件里的同名新函数会被合并掉。
        ⚠️ 这是静态指纹，只抓「同一个函数里读齐三个键」的拷贝；拆成两个函数的拷贝（straddle 当年的 `_ask_ok` +
        `_greeks_usable`）靠下面 `TestConsumersAgreeOnEveryQuote` 的行为对拍抓。"""
        found = {f"{rel}::{q}" for rel, tree in _parsed_repo() for q in _quote_field_readers(tree)}
        new = sorted(found - set(_QUOTE_FIELD_READERS))
        assert not new, (f"新的报价字段读者 {new}：若是在重新判断「这张腿的 Greeks 能不能用」，改用 "
                         "cboe_options.bid_zero_greeks_usable；否则登记进 _QUOTE_FIELD_READERS 并写明理由")
        stale = sorted(set(_QUOTE_FIELD_READERS) - found)
        assert not stale, f"登记过但已不存在 / 不再读这三个键：{stale}（清单只许与现实一致）"

    def test_scan_actually_sees_subpackages(self):
        """守卫自证：扫描列表里要有 alphabot/ 下的文件（上一版的盲区），否则上面那条是假绿。"""
        rels = {rel for rel, _ in _parsed_repo()}
        assert "alphabot/straddle.py" in rels and "portfolio_greeks.py" in rels


class TestQuoteFieldFingerprint:
    """指纹本身（`_qualified_functions` / `_quote_field_readers`）：同名函数不许被合并。"""

    SRC = (
        "def _leg(q):\n    return q['bid'], q['ask'], q['iv']\n"
        "class A:\n    def _leg(self, q):\n        return q['bid'], q['ask'], q['iv']\n"
        "class B:\n    def _leg(self, q):\n        return q['bid'], q['ask'], q['iv']\n"
        "def outer():\n    def inner(q):\n        return q['bid'], q['ask'], q['iv']\n    return inner\n"
        "def _leg(q):\n    return q['bid'], q['ask'], q['iv']\n"
        "def unrelated(q):\n    return q['bid']\n"
    )

    def test_same_name_in_different_scopes_gets_distinct_keys(self):
        got = _quote_field_readers(ast.parse(self.SRC))
        assert got == ["_leg", "A._leg", "B._leg", "outer", "outer.<locals>.inner", "_leg#2"], got

    def test_a_new_same_named_function_in_a_registered_file_is_reported(self):
        """已登记 `_leg` 的文件里再加一个 `Shadow._leg`：以前按裸函数名建键、被合并掉，现在是新键。"""
        registered = {"x.py::_leg"}
        found = {f"x.py::{q}" for q in _quote_field_readers(ast.parse(
            "def _leg(q):\n    return q['bid'], q['ask'], q['iv']\n"
            "class Shadow:\n    def _leg(self, q):\n        return q['bid'], q['ask'], q['iv']\n"))}
        assert found - registered == {"x.py::Shadow._leg"}

    def test_a_function_missing_one_of_the_three_keys_is_not_a_reader(self):
        assert _quote_field_readers(ast.parse("def f(q):\n    return q['bid'], q['ask']\n")) == []


class TestRepoWalk:
    """`_repo_py_files` 本身：剪枝（不进 .git / .claude 等目录）、不计 iCloud 重名副本。"""

    def _tree(self, tmp_path):
        for rel in ("a.py", "pkg/b.py", "pkg/b 2.py", "c 13.py", ".claude/worktrees/w/d.py", ".git/e.py",
                    "tests/f.py", "node_modules/g.py", "__pycache__/h.py", "_retired_pre_phase5/i.py",
                    "pkg/.hidden/j.py", "data 2/k.py", "alphabot 2/straddle.py", "pkg/sub 13/m.py"):
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x = 1\n", encoding="utf-8")
        return tmp_path

    def test_skips_copies_hidden_and_vendored_dirs(self, tmp_path):
        """iCloud 冲突既复制文件（`b 2.py`）也复制**目录**（`alphabot 2/`）：两种都不进扫描列表。"""
        got = {str(p.relative_to(tmp_path)) for p in _repo_py_files(self._tree(tmp_path))}
        assert got == {"a.py", "pkg/b.py"}

    def test_pruned_directories_are_never_entered(self, tmp_path, monkeypatch):
        """剪枝要发生在遍历时：先全量 rglob 再过滤的写法会把 .git 和每个 worktree 都走一遍（本机约 4.2 万个 .py）。"""
        root = self._tree(tmp_path)
        visited = []
        real_walk = os.walk

        def spy(top, *a, **k):
            for dp, dn, fn in real_walk(top, *a, **k):
                visited.append(os.path.relpath(dp, top))
                yield dp, dn, fn

        monkeypatch.setattr(os, "walk", spy)
        list(_repo_py_files(root))
        bad = [v for v in visited if v.split(os.sep)[0] in {".git", ".claude", "node_modules", "__pycache__",
                                                             "_retired_pre_phase5", "tests"} or ".hidden" in v]
        assert not bad, bad

    def test_icloud_copy_of_a_guarded_module_does_not_trip_the_guard(self, tmp_path):
        """`portfolio_greeks 2.py` 之类的副本：不进扫描列表 ⇒ 不会把「只有两个 importer」判成三个。"""
        (tmp_path / "portfolio_greeks.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "portfolio_greeks 2.py").write_text("x = 1\n", encoding="utf-8")
        assert [p.name for p in _repo_py_files(tmp_path)] == ["portfolio_greeks.py"]

    def test_icloud_copy_of_a_guarded_package_does_not_trip_the_guard(self, tmp_path):
        """`alphabot 2/straddle.py`（整个目录的副本）：同样不进扫描列表——上一版只认文件、目录副本仍被扫到，
        「只有两个 importer」会被判成三个。"""
        for rel in ("alphabot/straddle.py", "alphabot 2/straddle.py"):
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("from cboe_options import bid_zero_greeks_usable\n", encoding="utf-8")
        assert [str(p.relative_to(tmp_path)) for p in _repo_py_files(tmp_path)] == ["alphabot/straddle.py"]

    def test_parse_is_shared_between_the_scan_tests(self):
        """三条扫描测试共用一次遍历 + 解析（`_parsed_repo` 缓存），不各走一遍全仓。"""
        assert _parsed_repo() is _parsed_repo()


class TestSharedPredicate:
    """cboe_options.bid_zero_greeks_usable 本身（原先在 portfolio_greeks 里，判据逐字不变）。"""

    def test_accepts_the_incident_shape(self):
        assert cboe_options.bid_zero_greeks_usable(_bid_zero()) is True

    def test_rejects_non_dict_and_positive_bid(self):
        assert cboe_options.bid_zero_greeks_usable(None) is False
        assert cboe_options.bid_zero_greeks_usable(_good("call")) is False      # bid>0：走严格口径，不在这类里

    def test_straddle_page_uses_it_unchanged(self):
        for over in ({}, {"iv": 0.0}, {"ask": 0.0}, {"delta": None}, {"bid": 0.02}):
            q = _bid_zero(**over)
            assert sd._greeks_usable(q) == cboe_options.bid_zero_greeks_usable(q)

    @pytest.mark.parametrize("over", [{"bid": False}, {"ask": True}, {"iv": True}, {"delta": True},
                                      {"gamma": False}, {"vega": True}, {"theta": False}])
    def test_booleans_are_not_numbers(self, over):
        """`float(False)` 是 0.0：`bid: false` 不能被读成「买价为 0」。前身（各自的 `_num`）本来就拒布尔，
        收成一份时一度丢了（v0.45.620 二次检查）。"""
        assert cboe_options.bid_zero_greeks_usable(_bid_zero(**over)) is False
        assert sd._greeks_usable(_bid_zero(**over)) is False


class TestConsumersAgreeOnEveryQuote:
    """行为对拍：同一张报价喂给三处——判据本身、跨式页 `_greeks_usable`、对冲账本 `option_exposures`——结论必须一致。
    原始 CBOE 行经真正的 `_qs_contract` 整理（含 `quote_ok`），不手写报价形状。拆成两个函数的拷贝、或哪天某处
    悄悄放宽 / 收紧一个条件，静态指纹抓不到，这里抓。
    ⚠️ 布尔值进不了这张网格：`_qs_contract` 在整理时已把 `False` 转成 0.0，下游两处看到的都是数。布尔拒绝只能在
    判据被**直接调用**时出现，由 `TestSharedPredicate.test_booleans_are_not_numbers` 守。"""

    BIDS = (0.0, 0.01, -1.0, None, "x")      # -0.0 与 0.0 等价、不算新点；负买价必须两处都拒
    ASKS = (0.03, 0.0, None, float("nan"))
    IVS = (0.3, 0.0, None, -1.0)
    DELTAS = (0.05, None, float("nan"), 0.0)

    def _quote(self, bid, ask, iv, delta):
        raw = {"option": CALL, "bid": bid, "ask": ask, "iv": iv, "delta": delta, "gamma": 0.09, "vega": 0.004,
               "theta": -0.007, "open_interest": 100, "volume": 1}
        return cboe_options._qs_contract(raw, role="held", cp="C", strike=K, expiry=EXPIRY, dte=29)

    def test_grid(self):
        n = 0
        for bid, ask, iv, delta in itertools.product(self.BIDS, self.ASKS, self.IVS, self.DELTAS):
            q = self._quote(bid, ask, iv, delta)
            shared = cboe_options.bid_zero_greeks_usable(q)
            assert sd._greeks_usable(q) == shared, (bid, ask, iv, delta)
            greeks_ok = all(isinstance(q.get(k), float) and math.isfinite(q[k])
                            for k in ("delta", "gamma", "vega", "theta"))
            strict_ok = bool(q["quote_ok"]) and q["mid"] is not None and greeks_ok
            row = _call(_rows(q))
            assert (not row["quote_missing"]) == (strict_ok or shared), (bid, ask, iv, delta, row["quote_missing"])
            assert bool(row.get("bid_zero")) == (shared and not q["quote_ok"]), (bid, ask, iv, delta)
            n += 1
        assert n == len(self.BIDS) * len(self.ASKS) * len(self.IVS) * len(self.DELTAS)


class TestLedgerViewMarksDegradedLeg:
    """跨式页账本视图（v0.45.620）：hedge_state/greeks_<日期>.json 里 bid_zero 的腿照进合计，并标 degraded_legs。"""

    def _view(self, call_row, put_row):
        pos = _position("short", 39)
        legs = {CALL: call_row, PUT: put_row}
        return sd.position_view(pos, as_of=AS_OF, today=AS_OF, sig_today={"underlying_price": S, "as_of": AS_OF},
                                sig_entry=None, greek_legs=legs)

    def _rows(self, call_quote):
        rows = _rows(call_quote)
        return sd._greek_legs({"rows": rows})

    def test_greek_legs_carry_bid_zero_marker(self):
        legs = self._rows(_bid_zero())
        assert legs[CALL]["bid_zero"] is True and legs[PUT].get("bid_zero") is None

    def test_degraded_leg_named_when_its_numbers_are_used(self):
        legs = self._rows(_bid_zero())
        g = self._view(legs[CALL], legs[PUT])["greeks"]
        assert g["complete"] is True and g["degraded_legs"] == ["call"]
        assert g["dollar_delta"] is not None

    def test_no_degraded_note_for_normal_legs(self):
        legs = self._rows(_good("call"))
        assert self._view(legs[CALL], legs[PUT])["greeks"]["degraded_legs"] == []

    def test_no_degraded_note_when_other_leg_missing(self):
        """另一条腿缺 ⇒ 合计全 None ⇒ 页面不许再声称「含 call 腿的模型值」（与盘中页同一规则）。"""
        legs = self._rows(_bid_zero())
        g = self._view(legs[CALL], {**legs[PUT], "quote_missing": True})["greeks"]
        assert g["dollar_delta"] is None and g["degraded_legs"] == []

    def test_live_view_and_ledger_view_agree_on_the_same_leg(self):
        q = {"available": True, "quotes": {CALL: _bid_zero(), PUT: _good("put")}, "underlying_price": S}
        live = sd.live_view(_position("short", 39), q, {}, now_et=sd.datetime(2026, 9, 3, 12, 0))["greeks"]
        legs = self._rows(_bid_zero())
        led = self._view(legs[CALL], legs[PUT])["greeks"]
        assert live["degraded_legs"] == led["degraded_legs"] == ["call"]


class TestCoverageAndHedgePage:
    def test_aggregate_coverage_counts_bid_zero_legs(self):
        agg = pg.aggregate(_rows(_bid_zero()), nav=150_000.0)
        assert agg["coverage"]["n_bid_zero"] == 1
        assert pg.aggregate(_rows(_good("call")), nav=150_000.0)["coverage"]["n_bid_zero"] == 0

    def test_hedge_page_brief_carries_the_count_and_names(self):
        rows = _rows(_bid_zero())
        agg = pg.aggregate(rows, nav=150_000.0)
        rec = pg.hedge_recommendation(agg, 700.0, 150_000.0)
        chk = pg._price_check(AS_OF, rows, {"price": 700.0}, rec)
        b = hedge_page.audit_brief(AS_OF, {"aggregate": agg, "price_check": chk, "recommendation": rec})
        assert b["price_check"]["n_bid_zero"] == 1 and b["price_check"]["bid_zero"] == [CALL]
        # 老审计文件（没有这两个键）读出来是 0 / []，不崩
        old = hedge_page.audit_brief(AS_OF, {"aggregate": {}, "price_check": {"n_stale": 0, "gaps": []}})
        assert old["price_check"]["n_bid_zero"] == 0 and old["price_check"]["bid_zero"] == []

    def test_hedge_page_script_shows_the_notice(self):
        js = (_ROOT / "alphabot" / "static" / "pages" / "hedge.js").read_text(encoding="utf-8")
        import re
        # 条件与动作要连在一起：只查字符串存在，`if (false)` 照样过
        assert re.search(r"if \(pc\.n_bid_zero\)\s*warns\.push\([^;]*买价为 0 的腿", js), "hedge.js 缺 bid_zero 提示"

    def test_markdown_coverage_line_mentions_bid_zero_legs(self):
        rows = _rows(_bid_zero())
        agg = pg.aggregate(rows, nav=150_000.0)
        res = {"as_of": AS_OF, "aggregate": agg, "rows": rows,
               "recommendation": pg.hedge_recommendation(agg, 700.0, 150_000.0),
               "stress": pg.stress_table(rows), "executed": False}
        text = pg.render_markdown(AS_OF, res)
        assert "买价为 0 的腿" in text


_ROOT = pathlib.Path(pg.__file__).resolve().parent
#: 同一个函数里同时读 bid / ask / iv 三个键的生产函数——都是在**解析或构造**报价（不是在判「Greeks 能不能用」）。
#: 键 = `相对路径::限定名`（`Class.method` / `outer.<locals>.inner`，同文件重名加 `#2`）。
#: 新增一个要先问：它是不是在重新实现 `cboe_options.bid_zero_greeks_usable`？
_QUOTE_FIELD_READERS = {
    "cboe_options.py::fetch_cboe_raw_contracts": "CBOE payload → 原始合约行",
    "cboe_options.py::fetch_cboe_chain": "CBOE payload → 链结构",
    "cboe_options.py::_qs_contract": "原始行 → 持久化报价形状（quote_ok 的定义处）",
    "cboe_options.py::bid_zero_greeks_usable": "判据本身",
    "earnings_vol_signal.py::_leg": "财报跨式信号的腿报价整理（点差口径）",
    "sell_strike_candidates.py::_leg": "卖权候选的腿报价整理（25% 点差预注册口径）",
    "alphabot/synthetic.py::demo_payload": "演示用合成 payload",
    "alphabot/synthetic.py::_held": "演示用合成持仓报价",
    "alphabot/yf_chain.py::_rows": "yfinance 备用源 → 原始行",
    "alphabot/premium.py::_contract_row": "溢价页的合约行整理",
}
#: 这条守卫额外不扫的目录（第三方 / 点号目录 / iCloud 副本的口径在共享件 `tests/_repo_files.py`，不自抄）
_SKIP_DIRS = frozenset({"__pycache__", "_retired_pre_phase5", "tests", "venv"})


def _repo_py_files(root=None):
    """仓库里的生产 .py（遍历时剪枝，见 `tests/_repo_files.py::walk_own_python_files`）。"""
    return walk_own_python_files(root or _ROOT, skip_dirs=_SKIP_DIRS).files


@functools.lru_cache(maxsize=1)
def _parsed_repo():
    """`[(相对路径, 语法树)]`——整个测试会话只遍历、解析一次，三条扫描测试共用。读不了的目录 ⇒ 断言失败。"""
    res = walk_own_python_files(_ROOT, skip_dirs=_SKIP_DIRS)
    assert not res.errors, f"遍历读不了这些目录，守卫的覆盖不完整：{res.errors[:5]}"
    out = []
    for path in res.files:
        try:
            out.append((path.relative_to(_ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"))))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return tuple(out)


def _qualified_functions(tree):
    """`[(限定名, 节点)]`：类方法 `A.m`、嵌套函数 `f.<locals>.g`；同一文件里限定名重复的加 `#2` / `#3`。"""
    out, seen = [], collections.Counter()

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + child.name
                seen[q] += 1
                out.append((q if seen[q] == 1 else f"{q}#{seen[q]}", child))
                visit(child, q + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _quote_field_readers(tree):
    """语法树里同时读 `bid` / `ask` / `iv` 三个字符串键的函数的限定名。"""
    return [q for q, n in _qualified_functions(tree)
            if {"bid", "ask", "iv"} <= {c.value for c in ast.walk(n)
                                        if isinstance(c, ast.Constant) and isinstance(c.value, str)}]
