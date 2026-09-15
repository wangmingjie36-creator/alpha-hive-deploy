"""param_optimizer.run_grid() 跑完必须把 paper_portfolio.CONFIG 还原到运行前的
真实状态，不能用硬编码字面量"恢复"（那是 v0.39.0 前的旧默认值，早就跟不上生产
CONFIG，v0.45.261 修复）。

_run_one_combo() 真实实现要跑 pp.bootstrap_from_history()，需要完整历史快照
数据，不适合单测——这里用 stub 顶替，只保留它对 CONFIG 的读写副作用。
"""

import copy

import param_optimizer
import paper_portfolio as pp


def _stub_run_one_combo(sl, tp, deploy):
    """模拟真实 _run_one_combo 对 CONFIG 的覆写，跳过真实 bootstrap。"""
    pp.CONFIG["sl_pct"] = sl
    pp.CONFIG["tp_pct"] = tp
    pp.CONFIG["max_deployed_pct"] = deploy
    pp.CONFIG["ticker_whitelist"] = []
    pp.CONFIG["live_start_date"] = ""
    return param_optimizer.RunResult(
        sl=sl, tp=tp, deploy=deploy,
        nav=50_000.0, total_return_pct=0.0, spy_return_pct=0.0, alpha_pct=0.0,
        sharpe=0.0, mdd_pct=0.0, win_rate_pct=0.0,
        trades_total=0, trades_wins=0, avg_pnl_per_trade=0.0, profit_factor=0.0,
        equity_curve=[],
    )


def test_run_grid_restores_actual_pre_run_config_not_stale_literals(monkeypatch):
    # 运行前把被 mutate 的键设成哨兵值——不等于旧硬编码值(10.0/30.0/["NVDA"]/
    # "2026-04-16")，也不等于当前生产默认值，专门用来证伪"用字面量覆盖回去"。
    pp.CONFIG["sl_pct"] = 6.25
    pp.CONFIG["tp_pct"] = 12.34
    pp.CONFIG["max_deployed_pct"] = 55.5
    pp.CONFIG["ticker_whitelist"] = ["SENTINEL"]
    pp.CONFIG["live_start_date"] = "2099-01-01"
    snapshot_before = copy.deepcopy(pp.CONFIG)

    monkeypatch.setattr(param_optimizer, "_run_one_combo", _stub_run_one_combo)

    param_optimizer.run_grid(quick=True)

    assert pp.CONFIG == snapshot_before, (
        "run_grid() 跑完后 CONFIG 必须逐键还原到运行前的真实状态，"
        f"实际 = {pp.CONFIG}"
    )
    # 显式钉住旧 bug 的复发形状：不能是任何一版硬编码字面量。
    assert pp.CONFIG["tp_pct"] == 12.34
    assert pp.CONFIG["max_deployed_pct"] == 55.5
    assert pp.CONFIG["ticker_whitelist"] == ["SENTINEL"]
