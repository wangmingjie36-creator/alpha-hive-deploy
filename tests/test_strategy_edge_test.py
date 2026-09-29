"""策略层优势检验执行器（v0.45.373）：预注册常量对钉 + 盲化 + 「只看前 N 周」。

全部合成数据，不出网、不读生产库。每条断言附了能让它变红的变异。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "experiments" / "strategy_edge_prereg.md"


def _load():
    spec = importlib.util.spec_from_file_location(
        "strategy_edge_test", _ROOT / "experiments" / "strategy_edge_test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def se():
    return _load()


_COHORT = {"date": "2026-01-05", "version": "vTEST"}   # 周一
_AS_OF = dt.date(2027, 12, 31)                          # 远晚于所有合成周 ⇒ 全部已结清


def _trades(n_weeks: int, ret_fn, start="2026-01-05", per_week=3):
    d0 = dt.date.fromisoformat(start)
    out = []
    for w in range(n_weeks):
        for k in range(per_week):
            day = d0 + dt.timedelta(weeks=w, days=k)
            out.append({"entry": day.isoformat(), "gross_pct": ret_fn(w, k),
                        "exit_reason": "T7_CLOSE"})
    return out


# ── 常量对钉 ──────────────────────────────────────────────────────────────────
def _parse_block() -> dict:
    text = _DOC.read_text(encoding="utf-8")
    m = re.search(r"```prereg-constants\n(.*?)```", text, re.S)
    assert m, "预注册文档里没有 prereg-constants 块"
    out = {}
    for line in m.group(1).strip().splitlines():
        k, v = (x.strip() for x in line.split("=", 1))
        if v in ("True", "False"):
            out[k] = v == "True"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def _flatten(d: dict, prefix="") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


class TestPreregPinned:
    def test_doc_block_equals_code(self, se):
        """变异：改 PREREG 任一值 / 加减键而不改文档 ⇒ 红。"""
        assert _parse_block() == _flatten(se.PREREG)

    def test_dashboard_tests_the_registered_strategy(self, se, monkeypatch):
        """网站实际传给回测的实参 == 预注册冻结的实参。

        变异：网站启用 risk_per_trade_pct（或改回扣成本）而预注册没改 ⇒ 红
        ——此时要显式决定：重新登记，还是不改网站（预注册 §3 / §8）。
        """
        import dashboard_renderer as dr
        import portfolio_backtest as pb

        seen = []
        orig = pb.BacktestConfig

        def _spy(**kw):
            seen.append(kw)
            return orig(**kw)

        monkeypatch.setattr(pb, "BacktestConfig", _spy)
        monkeypatch.setattr(pb, "run_backtest", lambda *a, **k: {"error": "合成：只取实参"})
        dr._load_accuracy_data()
        assert seen, "网站没有调用 BacktestConfig —— 这条守卫空转了"
        assert seen[0] == se.PREREG["backtest_kwargs"]


# ── 盲化 ──────────────────────────────────────────────────────────────────────
class TestBlinding:
    def test_not_ready_returns_counts_only(self, se):
        """变异：未就绪也调用 evaluate ⇒ 出现 mean_pct/t/p ⇒ 红。"""
        n = se.PREREG["n_weeks"] - 1
        r = se.assess(trades=_trades(n, lambda w, k: 5.0), cohort=_COHORT, as_of=_AS_OF)
        assert r["status"] == "not_ready"
        assert r["n_units"] == n
        assert not (se.BLINDED_KEYS & set(r)), f"未就绪却泄露了：{se.BLINDED_KEYS & set(r)}"

    def test_ready_exposes_result(self, se):
        """反向自证：就绪时这些键确实会出现，否则上一条恒真。"""
        n = se.PREREG["n_weeks"]
        r = se.assess(trades=_trades(n, lambda w, k: 1.0 + (w % 3)), cohort=_COHORT, as_of=_AS_OF)
        assert r["status"] == "ready"
        assert se.BLINDED_KEYS <= set(r)


# ── 只看前 N 周：就绪后再跑多少次都一样 ─────────────────────────────────────
class TestFirstNWeeksOnly:
    def test_later_weeks_do_not_change_answer(self, se):
        """变异：evaluate 用全部单位而非前 n_weeks ⇒ 第 53 周起的 −50% 把结论翻掉 ⇒ 红。"""
        n = se.PREREG["n_weeks"]
        base = _trades(n, lambda w, k: 1.0 + (w % 4))
        more = base + _trades(20, lambda w, k: -50.0,
                              start=(dt.date(2026, 1, 5) + dt.timedelta(weeks=n)).isoformat())
        a = se.assess(trades=base, cohort=_COHORT, as_of=_AS_OF)
        b = se.assess(trades=more, cohort=_COHORT, as_of=_AS_OF)
        for k in ("first_week", "last_week", "n_units", "n_trades", "mean_pct", "t", "p", "decision"):
            assert a[k] == b[k], k
        assert b["n_units"] == n and b["n_trades"] == n * 3

    def test_unsettled_weeks_are_not_counted(self, se):
        """结清宽限：as_of 太近 ⇒ 最近几周不入样本。变异：删掉 _week_settled 判断 ⇒ 红。"""
        n = se.PREREG["n_weeks"]
        trades = _trades(n, lambda w, k: 1.0)
        last_entry = dt.date.fromisoformat(trades[-1]["entry"])
        just_after = last_entry + dt.timedelta(days=7)
        r = se.assess(trades=trades, cohort=_COHORT, as_of=just_after)
        assert r["status"] == "not_ready" and r["n_units"] < n


# ── 样本边界 ─────────────────────────────────────────────────────────────────
class TestUnitDefinition:
    def test_pre_cohort_and_cutoff_are_excluded(self, se):
        """变异：不按世代过滤 / 收 WINDOW_CUTOFF ⇒ 单位数或笔数多出来 ⇒ 红。"""
        trades = _trades(3, lambda w, k: 1.0)
        trades += _trades(2, lambda w, k: 9.0, start="2025-12-01")            # 世代之前
        trades.append({"entry": "2026-01-06", "gross_pct": 0.0, "exit_reason": "WINDOW_CUTOFF"})
        units = se.weekly_units(trades, _COHORT["date"], _AS_OF)
        assert [u["n"] for u in units] == [3, 3, 3]
        assert all(u["mean_pct"] == pytest.approx(1.0) for u in units)

    def test_weekly_mean_not_trade_pooled(self, se):
        """单位是周均值，不是逐笔：一周 1 笔 +10、一周 9 笔 0 ⇒ 两个单位 10 与 0。"""
        trades = [{"entry": "2026-01-05", "gross_pct": 10.0, "exit_reason": "TP"}]
        trades += [{"entry": "2026-01-12", "gross_pct": 0.0, "exit_reason": "T7_CLOSE"}] * 9
        units = se.weekly_units(trades, _COHORT["date"], _AS_OF)
        assert [(u["n"], u["mean_pct"]) for u in units] == [(1, 10.0), (9, 0.0)]


# ── 判定 ─────────────────────────────────────────────────────────────────────
class TestDecision:
    def test_clear_positive_edge_passes(self, se):
        n = se.PREREG["n_weeks"]
        r = se.assess(trades=_trades(n, lambda w, k: 2.0 + (w % 3) - 1), cohort=_COHORT, as_of=_AS_OF)
        assert r["p"] < se.PREREG["alpha"] and r["decision"].startswith("检出优势")

    def test_zero_mean_does_not_pass(self, se):
        """变异：单侧写成双侧 / 忘了判 p ⇒ 零均值也「检出」⇒ 红。"""
        n = se.PREREG["n_weeks"]
        r = se.assess(trades=_trades(n, lambda w, k: (-1.0) ** w), cohort=_COHORT, as_of=_AS_OF)
        assert r["mean_pct"] == pytest.approx(0.0)
        assert r["decision"].startswith("未检出")

    def test_negative_edge_does_not_pass(self, se):
        """单侧：强烈为负绝不能读成「检出优势」。变异：alternative 用双侧 ⇒ 红。"""
        n = se.PREREG["n_weeks"]
        r = se.assess(trades=_trades(n, lambda w, k: -2.0 - (w % 3)), cohort=_COHORT, as_of=_AS_OF)
        assert r["p"] > 0.5 and r["decision"].startswith("未检出")


class TestUndetermined:
    def test_backtest_crash_is_undetermined_not_not_ready(self, se, monkeypatch):
        """回测抛错 ⇒ status=undetermined（退出码 3），不许渲染成「未就绪，继续攒」。"""
        import portfolio_backtest as pb

        def _boom(*a, **k):
            raise FileNotFoundError("找不到 pheromone.db")

        monkeypatch.setattr(pb, "run_backtest", _boom)
        r = se.assess(cohort=_COHORT, as_of=_AS_OF)
        assert r["status"] == "undetermined" and "pheromone.db" in r["reason"]
