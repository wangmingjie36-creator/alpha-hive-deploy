"""Alpha Hive 蜂群 Agent 系统 - 向后兼容 re-export 层

原 swarm_agents.py（3049 行）已拆分为包（方案五 Step 4）。
这里 re-export 的是**确实有人从包顶层取**的名字（`from swarm_agents import X`），
清单即下面的 `__all__`。

v0.45.302：此前把各子模块的内部名字（`_log`、`_RE_*`、`_yf_cache`、`clamp_score`、
`make_error_result` 等 30 个）也一并 re-export —— 全仓 AST 核对与仓库外
（编排器、定时任务、MCP 服务器）扫描均为**零消费者**，已删。要用它们请直接从
子模块导入（`from swarm_agents.utils import clamp_score`），这本来就是全仓现行写法。

新增一个「从包顶层导入」的用法时，把名字同时加进下面的 import **和** `__all__`：
只加 import 不加 `__all__`，ruff 的 F401 会把它报成未使用。
"""
from swarm_agents.cache import get_cached_stock_data, check_ticker_validity
from swarm_agents.base import BeeAgent, prefetch_shared_data, inject_prefetched
from swarm_agents.sentiment import _get_sentiment_momentum, _detect_sentiment_price_divergence
from swarm_agents.scout_bee import ScoutBeeNova
from swarm_agents.oracle_bee import OracleBeeEcho
from swarm_agents.buzz_bee import BuzzBeeWhisper
from swarm_agents.chronos_bee import ChronosBeeHorizon
from swarm_agents.rival_bee import RivalBeeVanguard
from swarm_agents.guard_bee import GuardBeeSentinel
from swarm_agents.bear_bee import BearBeeContrarian
from swarm_agents.queen_distiller import QueenDistiller

__all__ = [
    "get_cached_stock_data", "check_ticker_validity",
    "BeeAgent", "prefetch_shared_data", "inject_prefetched",
    "_get_sentiment_momentum", "_detect_sentiment_price_divergence",
    "ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon",
    "RivalBeeVanguard", "GuardBeeSentinel", "BearBeeContrarian", "QueenDistiller",
]
