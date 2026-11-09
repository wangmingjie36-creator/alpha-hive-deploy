"""
v0.41.5 回归测试：ChronosBee 的 current_price 必须与（CodeExecutorAgent 那半随该蜂 v0.45.750 退役删除）
共享快照价（Scout/Oracle 走的 CBOE-first 链）一致，不能各自单独查 yfinance。

事故：同一次扫描里 Scout/Oracle 用 CBOE 快照价，Chronos 用 yfinance
analyst_price_targets 自带的 "current" 字段，两边对不上，网站同一份报告
出现两个不同的 NVDA 现价（$206.34 vs $207.29）。
"""

from unittest.mock import MagicMock, patch

import pytest

from pheromone_board import PheromoneBoard
from swarm_agents.chronos_bee import ChronosBeeHorizon


@pytest.fixture
def board():
    return PheromoneBoard()


class TestChronosBeeCurrentPriceConsistency:
    def test_analyst_targets_use_snapshot_price(self, board, monkeypatch):
        agent = ChronosBeeHorizon(board)
        # 共享快照价（Scout/Oracle 同源）固定为 207.29
        monkeypatch.setattr(agent, "_get_stock_data", lambda ticker: {"price": 207.29})

        mock_ticker = MagicMock()
        mock_ticker.calendar = None
        mock_ticker.analyst_price_targets = {
            "current": 206.34,  # yfinance 自带的 "current"——刻意设置成不同值
            "low": 180.0, "high": 500.0, "mean": 302.31, "median": 294.0,
        }

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = agent.analyze("NVDA")

        details = result.get("details", {})
        analyst_targets = details.get("analyst_targets", {})
        assert analyst_targets, f"analyst_targets 不应为空，result={result}"
        assert analyst_targets["current_price"] == 207.29
        assert analyst_targets["current_price"] != 206.34
