"""按止损距离定仓（v0.45.370）。

治的形状：仓位只看方向、不看止损距离 ⇒ CRCL（12% 止损）与 NVDA（5%）同仓位，
单次止损亏损差 2.4 倍；中性单 15% 止损 × 10% 仓位 ⇒ 一次亏 1.5% NAV。

夹具全部合成、SPY 被 monkeypatch，不出网。每条断言附了能让它变红的变异。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

_DATES = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
_SPY = {d: 100.0 + i for i, d in enumerate(_DATES)}
# 三只票止损距离各不相同：NVDA 在 sl_overrides（5%），CRCL（12%），中性走 neutral_sl_pct（15%）
_ROWS = [("NVDA", "bullish", 8.0), ("CRCL", "bullish", 8.0), ("MSFT", "neutral", 5.0),
         ("META", "bearish", 3.0)]


def _seed(db_path: str) -> None:
    from backtester import PredictionStore

    PredictionStore(db_path=db_path)
    rows = [(_DATES[0], t, sc, d, 100.0, 1.0, 1.0, 1, "T7_CLOSE", _DATES[2], 101.0, 2,
             json.dumps({}), 0.5) for t, d, sc in _ROWS]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO predictions (date,ticker,final_score,direction,price_at_predict,"
            "return_t7,net_return_t7,checked_t7,exit_reason,exit_date,exit_price,"
            "holding_days,cost_breakdown,spy_return_t7)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()


@pytest.fixture()
def bt(tmp_path, monkeypatch):
    import portfolio_backtest as pb

    db = tmp_path / "pheromone_risk.db"
    _seed(str(db))
    monkeypatch.setattr(pb, "_fetch_spy_prices", lambda *a, **k: dict(_SPY))
    monkeypatch.setattr(pb, "_find_db", lambda: db)

    def run(**kw):
        base = dict(macro_gate=False, exclude_nontrading_days=False, max_agent_std=0)
        base.update(kw)
        r = pb.run_backtest(pb.BacktestConfig(**base))
        return r, {t["ticker"]: t["size_usd"] for t in r["all_trades"]}

    return run


class TestStopLossSingleSource:
    def test_helper_reads_config(self):
        """变异：helper 对中性也走 sl_overrides / 写死 5% ⇒ 红。"""
        import config
        from backtester import stop_loss_pct_for

        cfg = config.TRADING_EXITS_CONFIG
        assert stop_loss_pct_for("CRCL", "bullish") == cfg["sl_overrides"]["CRCL"]
        assert stop_loss_pct_for("CRCL", "bearish") == cfg["sl_overrides"]["CRCL"]
        assert stop_loss_pct_for("ZZZZ", "bullish") == cfg["stop_loss_pct"]
        assert stop_loss_pct_for("CRCL", "neutral") == cfg["neutral_sl_pct"]
        assert stop_loss_pct_for("CRCL", "garbage") == cfg["neutral_sl_pct"]

    def test_exit_simulation_uses_helper(self):
        """出场模拟与定仓必须同源：出场代码里不许再有自己读 sl_overrides 的写法。"""
        import inspect

        import backtester

        src = inspect.getsource(backtester.Backtester)
        assert src.count("stop_loss_pct_for(") >= 2
        assert '"sl_overrides"' not in src and '"neutral_sl_pct"' not in src


class TestRiskBasedSizing:
    def test_fixture_enters_all_four(self, bt):
        """反向自证：四笔都入场，否则下面按票核对的断言会静默少核。"""
        _, sizes = bt()
        assert set(sizes) == {t for t, _, _ in _ROWS}

    def test_default_off_keeps_direction_sizing(self, bt):
        """默认关闭 ⇒ 仓位与旧逻辑逐笔一致（变异：默认值改成 0.004 ⇒ 红）。"""
        import portfolio_backtest as pb

        d = pb.BacktestConfig()
        assert d.risk_per_trade_pct is None
        r, sizes = bt()
        nav = d.initial_capital
        assert sizes["NVDA"] == pytest.approx(nav * d.bull_size_pct)
        assert sizes["CRCL"] == pytest.approx(nav * d.bull_size_pct)
        assert sizes["MSFT"] == pytest.approx(nav * d.position_size_pct)
        assert sizes["META"] == pytest.approx(nav * d.bear_size_pct)
        assert r["config"]["risk_per_trade_pct"] is None

    def test_size_scales_inversely_with_stop_distance(self, bt):
        """开启 ⇒ 仓位 = min(风险预算 / 止损距离, 方向上限)。

        变异：去掉 min 上限 ⇒ NVDA 0.005/0.05=10% > 8% ⇒ 红；
              止损距离取错（如中性走 5%）⇒ MSFT 尺寸不符 ⇒ 红。
        """
        import portfolio_backtest as pb
        from backtester import stop_loss_pct_for

        risk = 0.005
        r, sizes = bt(risk_per_trade_pct=risk)
        d = pb.BacktestConfig()
        nav = d.initial_capital
        cap = {"bullish": d.bull_size_pct, "bearish": d.bear_size_pct,
               "neutral": d.position_size_pct}
        for t, direction, _ in _ROWS:
            want = nav * min(risk / (stop_loss_pct_for(t, direction) / 100), cap[direction])
            assert sizes[t] == pytest.approx(want, abs=0.01), t
        # 每次打到止损的名义亏损（仓位 × 止损距离）不超过预算
        for t, direction, _ in _ROWS:
            assert sizes[t] * stop_loss_pct_for(t, direction) / 100 <= nav * risk + 0.01
        assert sizes["NVDA"] == pytest.approx(nav * d.bull_size_pct)   # 撞上限
        assert sizes["CRCL"] < sizes["NVDA"]                            # 宽止损 ⇒ 小仓
        assert r["config"]["risk_per_trade_pct"] == risk

    @pytest.mark.parametrize("bad", [0, -0.01, 1.0, 5])
    def test_invalid_budget_raises(self, bad):
        """0 会让每笔 $0 仍「入场」—— 必须抛，不许静默跑完。"""
        import portfolio_backtest as pb

        with pytest.raises(ValueError):
            pb.BacktestConfig(risk_per_trade_pct=bad)
