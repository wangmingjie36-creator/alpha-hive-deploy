"""
Alpha Hive 测试 fixtures - 共享 mock 数据 + 隔离数据库
"""

import sys
import os
import pytest
import tempfile

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ==================== 环境隔离 ====================

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """所有测试自动使用临时目录，防止污染生产数据库"""
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(tmp_path))
    monkeypatch.setenv("ALPHA_HIVE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ALPHA_HIVE_CHROMA_PATH", str(tmp_path / "test_chroma"))
    monkeypatch.setenv("ALPHA_HIVE_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ALPHA_HIVE_CACHE_DIR", str(tmp_path / "cache"))


# ==================== Mock 股票数据 ====================

MOCK_STOCK_DATA = {
    "NVDA": {
        "price": 142.50,
        "momentum_5d": 3.2,
        "avg_volume": 45_000_000,
        "volume_ratio": 1.4,
        "volatility_20d": 38.5,
    },
    "TSLA": {
        "price": 340.00,
        "momentum_5d": -1.8,
        "avg_volume": 80_000_000,
        "volume_ratio": 0.9,
        "volatility_20d": 52.0,
    },
    "VKTX": {
        "price": 48.20,
        "momentum_5d": 7.5,
        "avg_volume": 3_000_000,
        "volume_ratio": 2.1,
        "volatility_20d": 65.0,
    },
}


@pytest.fixture
def mock_stock_data(monkeypatch):
    """Mock yfinance 数据，避免测试中调用外部 API"""
    def _mock_fetch(ticker):
        return MOCK_STOCK_DATA.get(ticker, MOCK_STOCK_DATA["NVDA"])

    import swarm_agents
    monkeypatch.setattr(swarm_agents, "_fetch_stock_data", _mock_fetch)
    return MOCK_STOCK_DATA


# ==================== PheromoneBoard fixture ====================

@pytest.fixture
def board():
    """创建干净的信息素板"""
    from pheromone_board import PheromoneBoard
    b = PheromoneBoard()
    yield b
    b.clear()


# ==================== Agent fixtures ====================

@pytest.fixture
def all_agents(board, mock_stock_data):
    """实例化 6 个 Agent（使用 mock 数据）"""
    from swarm_agents import (
        ScoutBeeNova, OracleBeeEcho, BuzzBeeWhisper,
        ChronosBeeHorizon, RivalBeeVanguard, GuardBeeSentinel,
    )
    return {
        "scout": ScoutBeeNova(board),
        "oracle": OracleBeeEcho(board),
        "buzz": BuzzBeeWhisper(board),
        "chronos": ChronosBeeHorizon(board),
        "rival": RivalBeeVanguard(board),
        "guard": GuardBeeSentinel(board),
    }


@pytest.fixture
def queen(board):
    """创建 QueenDistiller"""
    from swarm_agents import QueenDistiller
    return QueenDistiller(board)


# ==================== MemoryStore fixture ====================

@pytest.fixture
def memory_store(tmp_path):
    """创建隔离的 MemoryStore（使用临时 DB）"""
    from memory_store import MemoryStore
    return MemoryStore(db_path=str(tmp_path / "test_memory.db"))
