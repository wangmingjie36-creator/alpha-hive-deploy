"""资金曲线与「真实策略回测」卡片必须同源（v0.45.179）。

治的形状：**同一页上同一个量出现两个不同的数**。三处实例：

1. 资金曲线走「固定 $5,000/笔、不复利」独立累加，卡片走 `portfolio_backtest`
   （NAV×8%多/12%空/10%中、复利、并发≤15、现金约束）。同一批笔末值差 0.81pp
   （+1.85% vs +1.04%），差额全部来自仓位权重 —— 而 `dashboard_renderer` 与
   `templates/dashboard.js` 两处注释都写着「曲线 = 卡片」。
2. 曲线的 SPY 线标着「买入持有」，实际是「每笔 7 日 SPY 收益 × $5K 累加」，
   会随交易笔数放大（实测 +19.33% vs 真买入持有 +15.73%）。
3. `portfolio_backtest` 取数失败时，曲线**静默换成另一套模型**（把全部候选都算进去，
   实测 Gross 从 +5.14% 跳到 +20.09%），页面照常渲染、日志只有 debug ——
   CLAUDE.md「这个失败，下游怎么知道？」那条：没人会红。

夹具全部合成、SPY 价格被 monkeypatch，**不出网**。每条断言都附了能让它变红的变异。
"""

from __future__ import annotations

import json
import sqlite3

import pytest


# ── 合成夹具 ────────────────────────────────────────────────────────────────
_DATES = [f"2026-03-{d:02d}" for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
# 一个**晚于所有成交结算日**的日期。用途见 _seed 里那条 TAIL 记录：
# 没有它，`last_date`（= max(最后预测日, 最后 exit)）恰好等于最后一笔结算日，
# 「基准量到哪一天」的两种写法数值相同 —— 夹具区分不了，断言就是假的。
_TAIL_DATE = "2026-03-18"
# SPY 单调上行；买入持有的涨幅只取决于起止两点，与交易笔数无关。
_SPY = {d: 100.0 + i * 2.0 for i, d in enumerate(_DATES)}
_SPY[_TAIL_DATE] = 130.0   # 与 _DATES[-1]=118 拉开，让「多量一天」看得见


def _seed(db_path: str, tickers_per_day: int, net_ret: float = 1.0) -> None:
    """每个日期开 *tickers_per_day* 只票，T+2 结算。"""
    from backtester import PredictionStore

    PredictionStore(db_path=db_path)
    rows = []
    for i, d in enumerate(_DATES[:-2]):
        for k in range(tickers_per_day):
            rows.append((
                d, f"T{i}_{k}", 8.0, "bullish", 100.0,
                net_ret + 0.2, net_ret, 1, "T7_CLOSE", _DATES[i + 2], 101.0, 2,
                json.dumps({}), 0.5,
            ))
        # 每天再放两只**分数低于 min_score_bull(5.5) 的看多票**，它们会被
        # skipped_score_filter 拒掉。没有这批，「候选 > 入场」不成立，
        # 下面那两条「只数入场的笔」的断言就是在 24 == 24 上恒真。
        for k in range(2):
            rows.append((
                d, f"REJ{i}_{k}", 5.0, "bullish", 100.0,
                -9.0, -9.2, 1, "SL", _DATES[i + 2], 91.0, 2,
                json.dumps({}), 0.5,
            ))
    # 一条**永远不会入场**（分数不达标）但 exit_date 落在所有成交之后的候选。
    # 它把 `last_date` 推到 _TAIL_DATE，从而让「基准终点 = 最后一笔结算日」与
    # 「基准终点 = last_date」两种写法产生不同的数 —— 变异 N2 才抓得住。
    rows.append((
        _DATES[-1], "TAILREJ", 5.0, "bullish", 100.0,
        -9.0, -9.2, 1, "SL", _TAIL_DATE, 91.0, 2, json.dumps({}), 0.5,
    ))
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO predictions (date,ticker,final_score,direction,price_at_predict,"
            "return_t7,net_return_t7,checked_t7,exit_reason,exit_date,exit_price,"
            "holding_days,cost_breakdown,spy_return_t7)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()


@pytest.fixture()
def bt(tmp_path, monkeypatch):
    """返回一个 run(tickers_per_day) -> result 的闭包；SPY 走合成价，不出网。"""
    import portfolio_backtest as pb

    monkeypatch.setattr(pb, "_fetch_spy_prices", lambda *a, **k: dict(_SPY))

    def run(tickers_per_day=3, **cfg_kw):
        db = tmp_path / f"pheromone_{tickers_per_day}.db"
        if not db.exists():
            _seed(str(db), tickers_per_day)
        monkeypatch.setattr(pb, "_find_db", lambda: db)
        kw = dict(macro_gate=False, exclude_nontrading_days=False)
        kw.update(cfg_kw)
        return pb.run_backtest(pb.BacktestConfig(**kw))

    return run


@pytest.fixture()
def dash(tmp_path, monkeypatch):
    """跑真正的 `dashboard_renderer._load_accuracy_data()`，但库与 SPY 全是合成的。"""
    import backtester
    import portfolio_backtest as pb

    db = tmp_path / "pheromone_dash.db"
    _seed(str(db), tickers_per_day=3)

    def _run():
        monkeypatch.setattr(pb, "_fetch_spy_prices", lambda *a, **k: dict(_SPY))
        monkeypatch.setattr(pb, "_find_db", lambda: db)
        monkeypatch.setattr(backtester, "default_db_path", lambda: str(db))
        _orig = pb.BacktestConfig

        def _cfg(**kw):
            kw.setdefault("macro_gate", False)
            kw["exclude_nontrading_days"] = False   # 夹具日期不走交易日历
            return _orig(**kw)

        monkeypatch.setattr(pb, "BacktestConfig", _cfg)
        import dashboard_renderer as dr
        return dr._load_accuracy_data()

    return _run


# ── ① 曲线终点 == 卡片 ──────────────────────────────────────────────────────
class TestCurveEndpointEqualsCard:
    def test_fixture_actually_trades(self, bt):
        """先证明夹具真的产生了入场 —— 否则下面的断言在空列表上恒真。

        变异：把 seed 的 final_score 改到门槛以下 ⇒ 红。
        """
        r = bt()
        assert r.get("trade_stats", {}).get("total_trades", 0) >= 5, r.get("filter_stats")
        assert len(r["equity_curve"]) >= 5

    def test_curve_last_point_equals_final_nav(self, bt):
        """曲线末点的 nav / nav_pct 必须逐位等于卡片的 final_nav / total_return_pct。

        变异：给曲线换个仓位口径（例如把 pnl 改成固定 `5000 * net/100`）⇒ 红。
        """
        r = bt()
        last = r["equity_curve"][-1]
        p = r["portfolio"]
        assert last["nav"] == pytest.approx(p["final_nav"], abs=0.51), (
            "资金曲线终点 != 卡片 final_nav —— 又变成两套口径了")
        assert last["nav_pct"] == pytest.approx(p["total_return_pct"], abs=0.02)

    def test_curve_points_equal_entered_trades(self, bt):
        """曲线点数 == 实际入场笔数（不是候选预测数）。

        变异：让曲线遍历全部候选、未入场的贡献 0 ⇒ 点数变成候选数 ⇒ 红。
        """
        r = bt()
        assert len(r["equity_curve"]) == r["trade_stats"]["total_trades"]
        assert r["filter_stats"]["total_predictions"] > r["trade_stats"]["total_trades"], (
            "夹具没有产生「候选 > 入场」，这条断言证明不了什么")


# ── ② SPY 是买入持有，不随交易笔数放大 ──────────────────────────────────────
class TestSpyLineIsBuyAndHold:
    def test_spy_endpoint_matches_benchmark_card(self, bt):
        """曲线 SPY 末点 == 卡片「SPY 同期基准」。

        变异：把卡片基准终点改回 `last_date` ⇒ 两者错开一天 ⇒ 红。
        """
        r = bt()
        assert r["equity_curve"][-1]["spy_nav_pct"] == pytest.approx(
            r["benchmark"]["spy_return_pct"], abs=0.02)

    def test_benchmark_ends_at_last_settlement_not_window_end(self, bt):
        """基准区间终点 = **最后一笔结算日**，不是回测窗口末日 `last_date`。

        两者不同时（夹具里 TAILREJ 把窗口推到 _TAIL_DATE），用 last_date 量 SPY
        会让卡片「SPY 同期」比曲线 SPY 末点多走几天 —— 同页两个「SPY 基准」又对不上。
        变异：`spy_end = _nearest_close(last_date, "back")` ⇒ 红。
        """
        r = bt()
        last_settle = max(p["date"] for p in r["equity_curve"])
        assert r["benchmark"]["period_end"] == last_settle
        assert r["period"]["end"] > last_settle, (
            "夹具没让窗口末日晚于最后结算日，这条断言证明不了什么")

    def test_spy_does_not_scale_with_trade_count(self, bt):
        """**买入持有不依赖交易笔数。** 每天开 3 只 vs 每天开 9 只，
        SPY 线终点必须一模一样。

        变异：改回「每笔 7 日 SPY 收益 × $5K 累加」⇒ 笔数翻三倍、SPY 也跟着涨 ⇒ 红。
        这条是本文件里唯一能抓住「口径冒充买入持有」的断言 ——
        只比对末点数值抓不住它（那只说明两处用了同一个错口径）。
        """
        r3, r9 = bt(3), bt(9)
        assert r9["trade_stats"]["total_trades"] > r3["trade_stats"]["total_trades"], (
            "两组笔数没拉开，这条断言证明不了什么")
        assert r3["equity_curve"][-1]["spy_nav_pct"] == pytest.approx(
            r9["equity_curve"][-1]["spy_nav_pct"], abs=0.02)

    def test_spy_missing_stays_none_never_zero(self, bt, monkeypatch):
        """取不到 SPY 时是 None，不是 0（0 读作「大盘没动」）。

        变异：把 `spy_nav_pct` 的兜底写成 0.0 ⇒ 红。
        """
        import portfolio_backtest as pb

        monkeypatch.setattr(pb, "_fetch_spy_prices", lambda *a, **k: {})
        r = bt(3, initial_capital=50_000.0)
        assert all(pt["spy_nav_pct"] is None for pt in r["equity_curve"])
        assert r["benchmark"]["spy_return_pct"] is None
        assert r["alpha"] is None


# ── ③ 回测失败 = 没有曲线，不是换一套模型 ───────────────────────────────────
class TestNoSilentModelSwap:
    def test_preset_defaults_are_all_none(self):
        """`_trading_stats` 的预置值一律 None —— 0 / 100000 在这些位置都是
        「合法可解读的假读数」，会让「没算出来」长得和「算出来了」一样。

        变异：把任一预置值改回 0.0 或 100000.0 ⇒ 红。
        """
        import ast
        import pathlib

        src = pathlib.Path("dashboard_renderer.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                    and node.target.id == "_trading_stats" and node.value is not None):
                found = node.value
                break
        assert isinstance(found, ast.Dict), "没找到 _trading_stats 的预置字典"
        bad = [k.value for k, v in zip(found.keys, found.values)
               if not (isinstance(v, ast.Constant) and v.value is None)]
        assert not bad, f"这些预置值不是 None，会在回测失败时冒充结果：{bad}"

    def test_backtest_error_yields_no_curve_and_no_fake_stats(self, monkeypatch):
        """run_backtest 返回 error 时：曲线为空、没有 realistic、没有编出来的数字。

        变异：把 `raise RuntimeError(...)` 换成 `pass`（继续往下用空 dict 填数）
        ⇒ realistic 会被塞进去 ⇒ 红。
        """
        import dashboard_renderer as dr
        import portfolio_backtest as pb

        monkeypatch.setattr(pb, "run_backtest",
                            lambda *a, **k: {"error": "合成故障：无已验证预测数据"})
        d = dr._load_accuracy_data()
        assert d["equity_curve"] == [], "回测失败却仍画出了一条曲线"
        ts = d["trading_stats"]
        assert "realistic" not in ts, "回测失败却给出了 realistic 卡片数字"
        for key in ("exit_sl_count", "exit_tp_count", "exit_close_count",
                    "avg_cost", "net_win_rate", "max_dd_net_pct",
                    "total_spy_ret", "alpha_vs_spy", "initial_capital"):
            assert ts.get(key) is None, f"{key}={ts.get(key)!r} —— 失败时不许有数字"


# ── ④ 卡片区块里的计数只数入场的笔 ──────────────────────────────────────────
class TestExitCountsCoverEnteredTradesOnly:
    def test_backtest_exit_counts_sum_to_entered(self, bt):
        """回测层：by_exit_reason 的合计 == 入场笔数。"""
        r = bt()
        total = sum(v["count"] for v in r["by_exit_reason"].values())
        assert total == r["trade_stats"]["total_trades"]
        assert total < r["filter_stats"]["total_predictions"], (
            "夹具没有产生「候选 > 入场」，这条断言证明不了什么")

    def test_dashboard_exit_counts_sum_to_entered(self, dash):
        """**渲染层**：三张卡的合计 == realistic.trades_entered。

        ⚠️ 只在回测返回上核对是不够的 —— 计数是 `dashboard_renderer` 自己拼的，
        变异发生在那一层时，回测层的断言全绿（实测 N7 正是如此）。
        变异：把 `_by_exit` 改成在全部候选上统计 ⇒ 红。
        """
        d = dash()
        ts = d["trading_stats"]
        real = ts["realistic"]
        total = (ts["exit_sl_count"] + ts["exit_tp_count"] + ts["exit_close_count"])
        assert total == real["trades_entered"] == len(d["equity_curve"])
        assert real["predictions_total"] > total, (
            "夹具没有产生「候选 > 入场」，这条断言证明不了什么")

    def test_dashboard_curve_endpoint_equals_card(self, dash):
        """渲染层同样要满足「曲线末点 == 卡片」——这是用户实际看到的那两个数。"""
        d = dash()
        last = d["equity_curve"][-1]
        real = d["trading_stats"]["realistic"]
        assert last["cum_net_pct"] == pytest.approx(real["total_return_pct"], abs=0.02)
        assert last["cum_spy_pct"] == pytest.approx(real["spy_return_pct"], abs=0.02)
