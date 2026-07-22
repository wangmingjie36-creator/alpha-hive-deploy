"""
v0.41.5 回归测试：ChronosBee / CodeExecutorAgent 的 current_price 必须与
共享快照价（Scout/Oracle 走的 CBOE-first 链）一致，不能各自单独查 yfinance。

事故：同一次扫描里 Scout/Oracle 用 CBOE 快照价，Chronos 用 yfinance
analyst_price_targets 自带的 "current" 字段，两边对不上，网站同一份报告
出现两个不同的 NVDA 现价（$206.34 vs $207.29）。
"""

import json

from unittest.mock import MagicMock, patch

import pytest

from pheromone_board import PheromoneBoard
from swarm_agents.chronos_bee import ChronosBeeHorizon
from code_executor_agent import CodeExecutorAgent


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


class _StubExecutor:
    """execute_python 返回预置结果，跳过真实沙盒执行"""

    def execute_python(self, code):
        if "sma_20" in code:
            return {"success": True, "stdout": json.dumps({"sma_20": 200.0, "signal": "中性"}), "stderr": ""}
        # 数据抓取阶段：yfinance 自己抓到的现价，刻意设置成与快照价不同
        return {"success": True, "stdout": json.dumps({"current_price": 206.34, "market_cap": 5e12}), "stderr": ""}


class TestCodeExecutorAgentCurrentPriceConsistency:
    def test_current_price_overridden_by_snapshot(self, board, monkeypatch):
        agent = CodeExecutorAgent(board, executor=_StubExecutor())
        monkeypatch.setattr(agent, "_get_stock_data", lambda ticker: {"price": 207.29})

        result = agent.analyze("NVDA")

        assert result["details"]["price"] == 207.29
        assert result["details"]["fetch_data"]["current_price"] == 207.29
