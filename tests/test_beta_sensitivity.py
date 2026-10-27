"""压力网格的 β 窗口对照（v0.45.662，**只展示**）：`tail_budget.beta_sensitivity` + `alphabot/beta_long.py` +
`alphabot/hedge.py::beta_sensitivity` + `AlphaBotService.beta_sensitivity` + `/api/hedge/beta` + `static/pages/hedge_beta.js`。

起因（2026-10-27 只读查证）：压力网格每个标的的现货冲击 = β × 大盘冲击，β 是审计里的 60 日 OLS；XOM / CVX 的 60 日 β 是负的
（250 日也是负的，2 年才翻正），「现货 −10%」那一格把能源股推到上涨 ~10%。**没有哪个窗口是真值**——所以不改网格，只把 250 日 β 的结果并排摆出来。

每组回答一句「谁会红」：
  · 纯函数（`TestWithBeta` / `TestBetaSensitivity`）：用手算得出的玩具账本（线性压力函数）核对每个数；β 换错 / 基准与对照互换 / β·$Delta 不重算都会红；
  · 取数（`TestBetaLong`）：OLS 是定义式、窗口只取最近 N 个**两边都有**的收益、样本不足 ⇒ 判不了而不是拿短样本冒充；取数失败带原因、不连坐；
  · 服务（`TestService`）：真账本夹具经放行闸；缓存 / 重新拉取 / 部分失败 / 全部失败；**只读**（目录指纹不变）；
  · **只展示的边界**（`TestDisplayOnly`）：审计里的压力表 / 告警 / SPY 覆盖仍只用 60 日 β——`beta_sensitivity` 除 Alpha Bot 的展示层外谁都不许引用。
全部离线（假 yfinance 模块、注入的取数函数）。
"""
from __future__ import annotations

import ast
import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import tail_budget as TB
from alphabot import beta_long as BL
from alphabot import hedge as HG
from alphabot import synthetic as syn
from alphabot.service import BETA_TTL_SEC, AlphaBotService
from tests.test_alphabot_hedge import DIRNAME, NOW, _client, _fingerprint, _write

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "alphabot" / "static" / "pages"

# ═══════════════════════════════ 玩具账本：线性压力函数（手算得出）═══════════════════════════════

SPOTS = (-10, -5, 0, 5, 10)
IVS = (0, 10)
NAV = 100_000.0


def lin_stress(rows, spy):
    """pnl(sp, iv) = Σ_{有 β 的行} dollar_delta·β·sp/100 + SPY 覆盖行 dollar_delta·sp/100 − vega_usd·iv。
    β 缺的行不进（partial）——与真压力表对缺 β 的行的处理同形。"""
    cells, miss = [], False
    for sp in SPOTS:
        for iv in IVS:
            p = 0.0
            for r in rows:
                if r.get("kind") == "hedge":
                    p += r["dollar_delta"] * sp / 100.0
                elif r.get("beta") is None:
                    miss = True
                else:
                    p += r["dollar_delta"] * r["beta"] * sp / 100.0 - r.get("vega_usd", 0.0) * iv
            cells.append({"spot_pct": sp, "iv_pts": iv, "pnl": p})
    return {"cells": cells, "partial": miss, "excluded": ["x"] if miss else []}


def row(tk, kind, dd, beta, **kw):
    r = {"kind": kind, "ticker": tk, "dollar_delta": dd, "beta": beta,
         "beta_dollar_delta": dd * beta if beta is not None else None}
    r.update(kw)
    return r


def book():
    """XOM：净空 $30k Delta、β −1.1（60 日）；AAA：多 $20k、β 1.0；SPY 覆盖空 $10k。"""
    return [row("XOM", "option", -30_000.0, -1.1, vega_usd=400.0), row("AAA", "stock", 20_000.0, 1.0),
            row("SPY", "hedge", -10_000.0, 1.0)]


ALT = {"XOM": 0.2, "AAA": 1.0}


def sens(rows=None, alt=ALT, **kw):
    return TB.beta_sensitivity(rows if rows is not None else book(), alt, 700.0, NAV, lin_stress, lambda t: None, None,
                               band_pct=15.0, **kw)


class TestWithBeta:
    def test_replaces_beta_and_recomputes_beta_dollar_delta(self):
        out, used, kept = TB.with_beta(book(), ALT)
        x = next(r for r in out if r["ticker"] == "XOM")
        assert x["beta"] == 0.2 and x["beta_dollar_delta"] == pytest.approx(-6_000.0)
        assert used == ["AAA", "XOM"] and kept == []

    def test_the_hedge_row_and_the_input_are_untouched(self):
        rows = book()
        before = copy.deepcopy(rows)
        out, _, _ = TB.with_beta(rows, {"SPY": 5.0, **ALT})
        assert rows == before, "不改入参"
        assert next(r for r in out if r["kind"] == "hedge")["beta"] == 1.0, "SPY 覆盖行 β 恒为 1"

    def test_missing_alt_beta_keeps_the_old_one_and_is_reported(self):
        out, used, kept = TB.with_beta(book(), {"XOM": 0.2})
        assert used == ["XOM"] and kept == ["AAA"]
        assert next(r for r in out if r["ticker"] == "AAA")["beta"] == 1.0

    def test_a_row_without_beta_is_not_rescued(self):
        """本来没 β 的行在压力表里被剔除；借替代 β 把它救回来会让两套结果不是同一批行。"""
        rows = book() + [row("ZZZ", "stock", 5_000.0, None)]
        out, used, kept = TB.with_beta(rows, {**ALT, "ZZZ": 1.3})
        assert next(r for r in out if r["ticker"] == "ZZZ")["beta"] is None and "ZZZ" not in used + kept

    def test_non_finite_alt_beta_counts_as_missing(self):
        _, used, kept = TB.with_beta(book(), {"XOM": float("nan"), "AAA": 1.0})
        assert used == ["AAA"] and kept == ["XOM"]


class TestBetaSensitivity:
    def test_every_number_matches_the_hand_calculation(self):
        r = sens()
        assert r["available"] is True and r["n_alt"] == 2 and r["missing"] == [] and r["partial"] is False
        # 现状：Σβ·$Δ = 33000 + 20000 − 10000 = 43000 ⇒ pnl = 430·sp − 400·iv，最差格 (−10, +10) = −8300
        assert r["base"]["loss_pct_nav"] == pytest.approx(8.3) and r["base"]["worst"]["pnl"] == pytest.approx(-8300.0)
        # 250 日：−6000 + 20000 − 10000 = 4000 ⇒ pnl = 40·sp − 400·iv，最差格 (−10, +10) = −4400
        assert r["alt"]["loss_pct_nav"] == pytest.approx(4.4) and r["alt"]["worst"]["pnl"] == pytest.approx(-4400.0)
        assert r["downside_ratio"] == pytest.approx(0.53, abs=0.005)
        assert (r["base"]["downside_worst"]["spot_pct"], r["base"]["downside_worst"]["iv_pts"]) == (-10.0, 10.0)

    def test_beta_dollar_delta_and_band_follow_the_alternative_beta(self):
        """SPY 覆盖建议吃的那个量：现状 +43% NAV（出带）⇒ 250 日 +4%（带内）。变红的变异：with_beta 不重算 beta_dollar_delta。"""
        r = sens()
        assert r["base"]["beta_dd_pct"] == pytest.approx(43.0) and r["base"]["band_status"] == "above"
        assert r["alt"]["beta_dd_pct"] == pytest.approx(4.0) and r["alt"]["band_status"] == "inside"

    def test_alert_line_is_judged_on_each_side(self):
        r = sens(alert_pct=5.0)
        assert r["base"]["breach"] is True and r["alt"]["breach"] is False and r["alert_pct"] == 5.0
        r = sens(alert_pct=20.0)
        assert r["base"]["breach"] is False and r["alt"]["breach"] is False

    def test_base_and_alt_are_not_swapped(self):
        """基准永远是审计里的 β；对照永远是传进来的那套。传入的 β 与审计的相同 ⇒ 两边逐项相等。"""
        same = {"XOM": -1.1, "AAA": 1.0}
        r = sens(alt=same)
        assert r["base"]["loss_pct_nav"] == r["alt"]["loss_pct_nav"] and r["downside_ratio"] == 1.0
        assert r["flips"] == [] and r["base"]["cells"] == r["alt"]["cells"]

    def test_flips_list_sign_changes_and_big_gaps_only(self):
        rows = book() + [row("BBB", "stock", 8_000.0, 1.0), row("CCC", "stock", 4_000.0, 0.3)]
        r = sens(rows, {**ALT, "BBB": 1.7, "CCC": 0.5})        # BBB 差 0.7（≥0.5）、CCC 差 0.2（不列）、XOM 符号相反
        by = {f["ticker"]: f for f in r["flips"]}
        assert set(by) == {"XOM", "BBB"} and by["XOM"]["sign_flip"] is True and by["BBB"]["sign_flip"] is False
        assert by["XOM"]["net_dollar_delta_pct_nav"] == pytest.approx(-30.0)
        assert [f["ticker"] for f in r["flips"]] == ["XOM", "BBB"], "按持仓净 $Delta 的绝对值降序"

    def test_per_ticker_contributions_add_up_in_both_sides(self):
        r = sens()
        t = {x["ticker"]: x for x in r["by_ticker"]}
        # 现状最差格 (−10, +10)：XOM = 3300·(−1)… 手算 −7300；AAA −2000；SPY +1000；合计 −8300
        assert t["XOM"]["base_at_worst_pct"] == pytest.approx(-7.3) and t["AAA"]["base_at_worst_pct"] == pytest.approx(-2.0)
        assert t["SPY"]["base_at_worst_pct"] == pytest.approx(1.0)
        assert sum(x["base_at_worst_pct"] for x in t.values()) == pytest.approx(-8.3)
        assert t["XOM"]["alt_at_worst_pct"] == pytest.approx(-3.4) and sum(x["alt_at_worst_pct"] for x in t.values()) == pytest.approx(-4.4)
        assert (t["XOM"]["base_beta"], t["XOM"]["alt_beta"]) == (-1.1, 0.2)

    def test_missing_alt_beta_makes_it_a_mixed_basis_and_says_so(self):
        r = sens(alt={"XOM": 0.2})
        assert r["available"] is True and r["missing"] == ["AAA"] and r["partial"] is True and r["n_alt"] == 1

    def test_inputs_are_not_mutated(self):
        rows = book()
        before = copy.deepcopy(rows)
        sens(rows)
        assert rows == before

    @pytest.mark.parametrize("kw,reason", [
        ({"alt": {}}, "alt_beta_unavailable"),
        ({"alt": {"XOM": None, "AAA": float("nan")}}, "alt_beta_unavailable"),
        ({"rows": []}, "no_rows"),
    ])
    def test_unavailable_has_a_named_reason(self, kw, reason):
        r = sens(**kw)
        assert r["available"] is False and r["reason"] == reason and r["reason_zh"] == TB.REASONS[reason]

    def test_nav_missing_and_a_dead_base_are_reported_not_raised(self):
        assert TB.beta_sensitivity(book(), ALT, 700.0, None, lin_stress, lambda t: None)["reason"] == "nav_unavailable"
        dead = lambda rows, spy: {"cells": [], "partial": True, "excluded": []}  # noqa: E731
        assert TB.beta_sensitivity(book(), ALT, 700.0, NAV, dead, lambda t: None)["reason"] == "base_unavailable"

    def test_the_result_carries_no_recommendation(self):
        """没有哪套 β 是真值：不给推荐 / 综合分 / 结论字段（同尾部对冲候选面板的约定）。"""
        def keys(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield str(k)
                    yield from keys(v)
            elif isinstance(o, list):
                for v in o:
                    yield from keys(v)
        bad = [k for k in keys(sens()) if any(w in k.lower() for w in ("recommend", "score", "best", "verdict", "should", "suggest", "winner"))]
        assert bad == []

    def test_reason_codes_are_registered_in_the_single_table(self):
        for code in ("alt_beta_unavailable", "no_rows", "base_unavailable"):
            assert code in TB.REASONS and TB.REASONS[code]


# ═══════════════════════════════ 长窗口 β 取数 ═══════════════════════════════

def _dates(n, end="2026-10-26"):
    d, out = date.fromisoformat(end), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def _bench_closes(days, seed=7):
    import random
    rnd = random.Random(seed)
    px, out = 400.0, {}
    for d in days:
        px *= 1.0 + rnd.gauss(0.0004, 0.01)
        out[d] = px
    return out


def _follow(bench_closes, beta, noise=0.0):
    """收益 = beta × 基准收益（+ 噪声）的收盘序列。"""
    rets = BL.returns_from_closes(bench_closes)
    days = sorted(bench_closes)
    px, out = 100.0, {days[0]: 100.0}
    for d in days[1:]:
        px *= 1.0 + beta * rets[d] + noise
        out[d] = px
    return out


class TestBetaLong:
    def test_returns_skip_a_bad_close_and_do_not_bridge_over_it(self):
        r = BL.returns_from_closes({"2026-10-20": 100.0, "2026-10-21": float("nan"), "2026-10-22": 110.0, "2026-10-23": 121.0, "2026-10-24": -5.0})
        assert r == {"2026-10-23": pytest.approx(0.10)}, "坏点两侧各自断开：不拿 10-20 → 10-22 当一天的收益"

    def test_ols_recovers_the_true_beta(self):
        days = _dates(260)
        bench = _bench_closes(days)
        b, n, why = BL.ols_beta(BL.returns_from_closes(_follow(bench, 1.7)), BL.returns_from_closes(bench), days[-1])
        assert why is None and n == 250 and b == pytest.approx(1.7, abs=1e-6)
        b, _, _ = BL.ols_beta(BL.returns_from_closes(_follow(bench, -0.9)), BL.returns_from_closes(bench), days[-1])
        assert b == pytest.approx(-0.9, abs=1e-6), "符号照实保留（能源股的负 β）"

    def test_only_the_last_window_of_returns_counts(self):
        """前 100 天斜率 5、后 250 天斜率 2 ⇒ 250 日 β = 2。变红的变异：窗口取全部样本。"""
        days = _dates(351)
        bench = _bench_closes(days)
        br = BL.returns_from_closes(bench)
        px, out = 100.0, {days[0]: 100.0}
        for i, d in enumerate(days[1:], start=1):
            px *= 1.0 + (5.0 if i <= 100 else 2.0) * br[d]
            out[d] = px
        b, n, _ = BL.ols_beta(BL.returns_from_closes(out), br, days[-1])
        assert n == 250 and b == pytest.approx(2.0, abs=1e-6)

    def test_returns_after_as_of_are_ignored(self):
        """最后 40 天斜率 10、之前斜率 1；as_of 在那 40 天之前 ⇒ β = 1。变红的变异：as_of 之后的收益也用（β 会被拉向 10）。"""
        days = _dates(320)
        bench = _bench_closes(days)
        br = BL.returns_from_closes(bench)
        px, out = 100.0, {days[0]: 100.0}
        for i, d in enumerate(days[1:], start=1):
            px *= 1.0 + (10.0 if i > len(days) - 41 else 1.0) * br[d]
            out[d] = px
        sr = BL.returns_from_closes(out)
        b, n, why = BL.ols_beta(sr, br, days[-41])
        assert why is None and n == 250 and b == pytest.approx(1.0, abs=1e-6)
        b_all, _, _ = BL.ols_beta(sr, br, days[-1])
        assert b_all > 1.5, "对照：把那 40 天算进去，β 明显被拉高——上面那条的 1.0 不是巧合"

    def test_too_short_a_sample_is_not_passed_off_as_250_days(self):
        days = _dates(150)
        bench = _bench_closes(days)
        b, n, why = BL.ols_beta(BL.returns_from_closes(_follow(bench, 1.0)), BL.returns_from_closes(bench), days[-1])
        assert b is None and why == "insufficient_overlap" and n == 149

    def test_flat_benchmark_has_no_beta(self):
        days = _dates(260)
        flat = {d: 100.0 for d in days}
        b, _, why = BL.ols_beta(BL.returns_from_closes(_follow(_bench_closes(days), 1.0)), BL.returns_from_closes(flat), days[-1])
        assert b is None and why == "zero_variance"


class FakeYF:
    """`yfinance` 的最小替身：`Ticker(t).history(**kw)` ⇒ 日线 DataFrame（带公司行动列，好让 yf_daily 认得出空行）。"""

    def __init__(self, series, boom=(), trailing_event_row=False):
        self.series, self.boom, self.trailing = series, set(boom), trailing_event_row
        self.asked = []

    def Ticker(self, t):                                         # noqa: N802
        outer = self

        class _T:
            ticker = t

            def history(self, **kw):
                outer.asked.append((t, kw))
                if t in outer.boom:
                    raise TimeoutError("x")
                s = outer.series.get(t)
                if s is None:
                    return pd.DataFrame({"Open": [], "High": [], "Low": [], "Close": [], "Volume": [], "Dividends": [], "Stock Splits": []})
                idx = pd.to_datetime(sorted(s))
                c = [s[d] for d in sorted(s)]
                df = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000.0, "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
                if outer.trailing:                               # 末行：公司行动空行（价格全空、成交量 0、分红非零）
                    extra = pd.DataFrame({"Open": [float("nan")], "High": [float("nan")], "Low": [float("nan")], "Close": [float("nan")],
                                          "Volume": [0.0], "Dividends": [0.5], "Stock Splits": [0.0]}, index=[idx[-1] + pd.Timedelta(days=1)])
                    df = pd.concat([df, extra])
                return df
        return _T()


class TestFetchLongBetas:
    def _series(self):
        days = _dates(300)
        bench = _bench_closes(days)
        return days, {"SPY": bench, "XOM": _follow(bench, -0.7), "NVDA": _follow(bench, 1.9)}

    def test_fetches_each_ticker_and_the_benchmark_once_through_the_single_entry(self):
        days, series = self._series()
        fy = FakeYF(series)
        betas, info = BL.fetch_long_betas(["XOM", "NVDA", "SPY"], days[-1], yf_module=fy)
        assert betas["XOM"] == pytest.approx(-0.7, abs=1e-6) and betas["NVDA"] == pytest.approx(1.9, abs=1e-6)
        assert "SPY" not in betas, "基准自己不进结果"
        assert sorted(t for t, _ in fy.asked) == ["NVDA", "SPY", "XOM"]
        assert all(kw == {"period": "2y", "auto_adjust": True} for _, kw in fy.asked)
        assert info["XOM"] == {"n": 250, "reason": None}

    def test_one_failing_ticker_does_not_take_the_others_down(self):
        days, series = self._series()
        betas, info = BL.fetch_long_betas(["XOM", "NVDA"], days[-1], yf_module=FakeYF(series, boom=["XOM"]))
        assert betas["XOM"] is None and info["XOM"]["reason"] == "exception:TimeoutError"
        assert betas["NVDA"] == pytest.approx(1.9, abs=1e-6) and info["NVDA"]["reason"] is None

    def test_missing_benchmark_fails_every_ticker_with_a_reason(self):
        days, series = self._series()
        for fy in (FakeYF({k: v for k, v in series.items() if k != "SPY"}), FakeYF(series, boom=["SPY"])):
            betas, info = BL.fetch_long_betas(["XOM", "NVDA"], days[-1], yf_module=fy)
            assert betas == {"XOM": None, "NVDA": None}
            assert {i["reason"] for i in info.values()} <= {"bench_unavailable", "exception:TimeoutError"} and all(i["reason"] for i in info.values())

    def test_a_trailing_corporate_action_row_does_not_poison_the_fit(self):
        days, series = self._series()
        betas, _ = BL.fetch_long_betas(["XOM"], days[-1], yf_module=FakeYF(series, trailing_event_row=True))
        assert betas["XOM"] == pytest.approx(-0.7, abs=1e-6)

    def test_never_raises_even_when_the_module_cannot_be_used(self):
        betas, info = BL.fetch_long_betas(["XOM"], "2026-10-26", yf_module=object())
        assert betas == {"XOM": None} and info["XOM"]["reason"]


# ═══════════════════════════════ 视图层与服务层 ═══════════════════════════════

def _real_rows():
    return syn.demo_beta_rows(778.57)


@pytest.fixture
def root_with_rows(tmp_path):
    """真账本夹具（经 `load_history` 放行闸），最近一份审计带逐行暴露与真压力表。"""
    import portfolio_greeks as pg
    root = _write(tmp_path / "data")
    f = root / DIRNAME / "greeks_2026-10-09.json"
    a = json.loads(f.read_text(encoding="utf-8"))
    a["rows"] = _real_rows()
    a["stress"] = pg.stress_table(a["rows"], a["spy_price"])
    f.write_text(json.dumps(a), encoding="utf-8")
    return root


def _svc(root, fn, **kw):
    return AlphaBotService(fetch_fn=syn.demo_fetch, straddle_root=root, now_fn=lambda: NOW, beta_long_fn=fn, **kw)


LONG = {"XOM": 0.2, "NVDA": 1.85, "AMZN": 1.4}


class TestHedgeView:
    def test_beta_audit_is_the_newest_audit_with_rows_and_a_stress_grid(self):
        audits = {"2026-10-07": {"rows": [{"a": 1}]}, "2026-10-08": {"rows": [{"a": 1}], "stress": {"cells": [{"x": 1}]}},
                  "2026-10-09": {"rows": [], "stress": {"cells": [{"x": 1}]}}}
        assert HG.beta_audit(audits)[0] == "2026-10-08"
        assert HG.beta_audit({}) == (None, None) and HG.beta_audit(None) == (None, None)

    def test_beta_tickers_are_the_rows_that_have_a_beta_and_not_the_hedge_row(self):
        a = {"rows": book() + [row("ZZZ", "stock", 1.0, None)]}
        assert HG.beta_tickers(a) == ["AAA", "XOM"]

    def test_view_reports_what_could_not_be_fetched(self):
        audits = {"2026-10-26": {"rows": book(), "stress": lin_stress(book(), 1.0), "spy_price": 700.0,
                                 "aggregate": {"nav": NAV, "band": {"band_pct": 15.0}}}}
        r = HG.beta_sensitivity(audits, {"XOM": 0.2, "AAA": None}, lin_stress, info={"AAA": {"n": 80, "reason": "insufficient_overlap"}})
        assert r["date"] == "2026-10-26" and r["available"] is True
        assert r["fetch"] == {"n_requested": 2, "n_ok": 1, "failed": [{"ticker": "AAA", "reason": "insufficient_overlap"}]}
        assert r["missing"] == ["AAA"] and r["partial"] is True and r["band_pct"] == 15.0

    def test_no_stress_function_is_a_named_unavailable(self):
        audits = {"2026-10-26": {"rows": book(), "stress": lin_stress(book(), 1.0), "aggregate": {"nav": NAV}}}
        assert HG.beta_sensitivity(audits, ALT, None)["reason"] == "stress_unavailable"
        assert HG.beta_sensitivity({}, ALT, lin_stress)["reason"] == "no_rows"


class TestService:
    def test_end_to_end_on_a_real_ledger_fixture(self, root_with_rows):
        calls = []

        def fn(tickers, day):
            calls.append((list(tickers), day))
            return LONG, {t: {"n": 250, "reason": None} for t in tickers}
        r = _svc(root_with_rows, fn).beta_sensitivity()
        assert r["available"] is True and r["date"] == "2026-10-09" and r["n_alt"] == 3
        assert calls == [(["AMZN", "NVDA", "XOM"], "2026-10-09")], "只给「本来有 β 的」标的取"
        assert r["alt"]["downside_worst"]["pnl"] > r["base"]["downside_worst"]["pnl"], "XOM 的 β 由负转正：下行格的亏损变小"
        assert [f["ticker"] for f in r["flips"]][:1] == ["XOM"] and r["flips"][0]["sign_flip"] is True
        assert r["meta"]["alt_label"] == "250 日" and r["meta"]["reasons"]["alt_beta_unavailable"]
        assert r["fetch"]["n_ok"] == 3 and r["demo"] is False and r["cache_age_sec"] == 0.0

    def test_the_audit_itself_is_untouched_and_the_endpoint_is_read_only(self, root_with_rows):
        before = _fingerprint(root_with_rows)
        _svc(root_with_rows, lambda t, d: (LONG, {})).beta_sensitivity()
        assert _fingerprint(root_with_rows) == before, "不写审计 / 账本 / 任何文件"

    def test_cached_per_audit_day_and_force_refetches(self, root_with_rows):
        n = []
        t = [0.0]
        svc = AlphaBotService(fetch_fn=syn.demo_fetch, straddle_root=root_with_rows, now_fn=lambda: NOW, clock=lambda: t[0],
                              beta_long_fn=lambda tk, d: (n.append(1) or LONG, {}))
        svc.beta_sensitivity()
        t[0] = 60.0
        r = svc.beta_sensitivity()
        assert len(n) == 1 and r["cache_age_sec"] == 60.0
        svc.beta_sensitivity(force=True)
        assert len(n) == 2
        t[0] = 60.0 + BETA_TTL_SEC + 1
        svc.beta_sensitivity()
        assert len(n) == 3, "超过 TTL 重新取"

    def test_a_partial_fetch_is_shown_as_a_mixed_basis(self, root_with_rows):
        r = _svc(root_with_rows, lambda t, d: ({"XOM": 0.2, "NVDA": None, "AMZN": 1.4},
                                              {"NVDA": {"n": 0, "reason": "exception:TimeoutError"}})).beta_sensitivity()
        assert r["available"] is True and r["partial"] is True and r["missing"] == ["NVDA"]
        assert r["fetch"]["failed"] == [{"ticker": "NVDA", "reason": "exception:TimeoutError"}]

    def test_nothing_fetched_is_unavailable_with_a_reason_and_is_not_cached(self, root_with_rows):
        n = []
        svc = _svc(root_with_rows, lambda t, d: (n.append(1) or {x: None for x in t}, {x: {"n": 0, "reason": "bench_unavailable"} for x in t}))
        r = svc.beta_sensitivity()
        assert r["available"] is False and r["reason"] == "alt_beta_unavailable" and r["reason_zh"]
        assert {f["reason"] for f in r["fetch"]["failed"]} == {"bench_unavailable"}
        svc.beta_sensitivity()
        assert len(n) == 2, "全失败的结果不缓存：下次打开再试"

    def test_a_throwing_fetch_function_is_a_named_failure_not_a_500(self, root_with_rows):
        def boom(t, d):
            raise RuntimeError("down")
        svc = _svc(root_with_rows, boom)
        r = svc.beta_sensitivity()
        assert r["available"] is False and r["reason"] == "alt_beta_unavailable"
        assert {f["reason"] for f in r["fetch"]["failed"]} == {"exception:RuntimeError"} and svc.stats["beta_sens_failures"] >= 1

    def test_an_unreadable_ledger_is_not_reported_as_no_audit(self, tmp_path):
        d = tmp_path / "data" / DIRNAME
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{not json", encoding="utf-8")
        r = _svc(tmp_path / "data", lambda t, x: (LONG, {})).beta_sensitivity()
        assert r["available"] is False and r["reason"] in ("ledger_unreadable", "no_audit_rows")
        assert r["reason_zh"]

    def test_the_ledger_without_row_level_audits_says_so(self, tmp_path):
        r = _svc(_write(tmp_path / "data"), lambda t, d: (LONG, {})).beta_sensitivity()      # 夹具审计没有压力表
        assert r["available"] is False and r["reason"] == "no_audit_rows" and r["reason_zh"]

    def test_demo_mode_has_a_working_comparison(self, tmp_path):
        svc = AlphaBotService(demo=True, state_dir=tmp_path / "st", now_fn=lambda: NOW)
        r = svc.beta_sensitivity()
        assert r["available"] is True and r["demo"] is True and r["flips"][0]["ticker"] == "XOM"
        assert r["base"]["downside_worst"]["pnl"] < r["alt"]["downside_worst"]["pnl"]

    def test_route(self, root_with_rows):
        c = _client(_svc(root_with_rows, lambda t, d: (LONG, {})))
        r = c.get("/api/hedge/beta")
        assert r.status_code == 200 and r.json()["available"] is True and r.json()["meta"]["base_label"] == "60 日"
        assert c.get("/api/hedge/beta?force=1").status_code == 200
        assert c.post("/api/hedge/beta").status_code in (403, 404, 405)


# ═══════════════════════════════ 只展示：别处谁都不许引用 ═══════════════════════════════

def _prod_py():
    skip = {"tests", "experiments", "_archive"}
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if skip & set(rel.parts) or any(part.startswith(".") for part in rel.parts):
            continue
        yield rel.as_posix(), p


class TestDisplayOnly:
    def test_only_the_alpha_bot_display_layer_refers_to_the_sensitivity(self):
        """审计 / 告警 / SPY 覆盖账 / 编排器 都不许 import 或调用它：它只是一张并排的对照表。变红的变异：谁把它接进了决定。"""
        import re
        pat = re.compile(r"\b(beta_sensitivity|beta_long|with_beta|fetch_long_betas)\b")
        users = {rel for rel, p in _prod_py() if pat.search(p.read_text(encoding="utf-8"))}
        # 纯函数 / 展示整理 / 取数 / 服务 / 路由；yf_daily 只是调用点注册表里的一行字符串
        assert users == {"tail_budget.py", "alphabot/hedge.py", "alphabot/service.py", "alphabot/beta_long.py",
                         "alphabot/server.py", "yf_daily.py"}, users

    def test_portfolio_greeks_still_uses_the_60_day_beta_and_stores_no_alternative(self):
        import portfolio_greeks as pg
        assert pg.CONFIG["beta_window"] == 60
        src = (ROOT / "portfolio_greeks.py").read_text(encoding="utf-8")
        assert "250" not in src.split("def _default_beta")[1].split("\ndef ")[0], "`_default_beta` 只算 60 日"
        assert "beta_sens" not in src and "beta_long" not in src, "审计文件里不写对照"

    def test_the_pure_module_stays_pure(self):
        tree = ast.parse((ROOT / "tail_budget.py").read_text(encoding="utf-8"))
        mods = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
        assert mods <= {"__future__", "math", "datetime", "typing"}, mods

    def test_the_only_yfinance_reader_is_the_registered_call_site(self):
        src = (ROOT / "alphabot" / "beta_long.py").read_text(encoding="utf-8")
        assert 'source="alphabot_beta_long"' in src and ".history(" in src
        import yf_daily
        assert "alphabot_beta_long" in yf_daily.SOURCES

    def test_the_hedge_view_does_not_fetch_anything(self):
        """`alphabot/hedge.py` 是纯整理层：取数在 beta_long / service，这里不许出现联网相关的 import。"""
        tree = ast.parse((ROOT / "alphabot" / "hedge.py").read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        assert not ({"yfinance", "requests", "urllib", "httpx"} & {x.split(".")[0] for x in names})


# ═══════════════════════════════ 前端 ═══════════════════════════════

class TestFrontend:
    JS = (STATIC / "hedge_beta.js").read_text(encoding="utf-8")
    HEDGE = (STATIC / "hedge.js").read_text(encoding="utf-8")

    def test_it_is_mounted_on_the_hedge_page_between_the_budget_panel_and_the_candidates(self):
        assert 'import { renderBetaSens } from "./hedge_beta.js"' in self.HEDGE
        assert "renderBetaSens(betaHost, alive)" in self.HEDGE
        order = [self.HEDGE.index(x) for x in ("    tailBudgetEl,", "    betaHost,", "    tailHost,")]
        assert order == sorted(order)

    def test_it_loads_lazily_from_its_own_endpoint_and_a_failure_does_not_take_the_page_down(self):
        assert '"/api/hedge/beta"' in self.JS and "errorBox" in self.JS and "try {" in self.JS and "alive()" in self.JS

    def test_it_says_no_window_is_the_truth_and_that_it_is_display_only(self):
        assert "没有哪个窗口是真值" in self.JS and "不进审计、不进告警、不改 SPY 覆盖的任何决定" in self.JS

    def test_numbers_come_from_the_server_not_recomputed_in_the_browser(self):
        for banned in ("Math.log", "reduce((a, b) => a + b", ".sort((a, b) => b.pnl", "stress_table", "polyfit"):
            assert banned not in self.JS
        assert "R.downside_ratio" in self.JS and "B.beta_dd_pct" in self.JS and "A.beta_dd_pct" in self.JS and "R.flips" in self.JS

    def test_mixed_basis_and_failures_are_shown(self):
        assert "混合口径" in self.JS and "R.missing" in self.JS and "f.failed" in self.JS

    def test_the_help_page_explains_it(self):
        help_js = (STATIC / "help.js").read_text(encoding="utf-8")
        assert "β 窗口对照" in help_js and "250 日" in help_js and "没有哪个窗口是真值" in help_js
