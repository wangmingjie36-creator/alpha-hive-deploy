"""Alpha Hive 蜂群 Agent 系统 - 向后兼容 re-export 层

原 swarm_agents.py（3049 行）已拆分为包（方案五 Step 4）。
所有公开名称在此 re-export，现有 import 无需修改。
"""
from swarm_agents._config import (
    _log, _RE_TICKER, _RE_INSIDER_SELL, _RE_INSIDER_BUY,
    _RE_PC_RATIO, _RE_PC_SHORT, _RE_IV_RANK, _RE_SENTIMENT, _AS,
)
from swarm_agents.cache import (
    _safe_score, _evict_oldest, _fetch_stock_data, get_cached_stock_data,
    check_ticker_validity,
    _yf_cache, _yf_cache_ts, _yf_lock, _MAX_CACHE_SIZE, _YF_CACHE_TTL,
)
from swarm_agents.base import (
    BeeAgent, prefetch_shared_data, inject_prefetched,
)
from swarm_agents.utils import (
    clamp_score, clamp_score_cfg, make_error_result,
    build_confidence, append_context, blend_with_llm,
    AGENT_ERRORS, LLM_ERRORS,
)
from swarm_agents.sentiment import (
    _sentiment_db_path, _init_sentiment_db, _upsert_sentiment,
    _get_sentiment_baseline, _get_sentiment_momentum,
    _detect_sentiment_price_divergence, _check_sentiment_spike,
)
from swarm_agents.scout_bee import ScoutBeeNova
from swarm_agents.oracle_bee import OracleBeeEcho
from swarm_agents.buzz_bee import BuzzBeeWhisper
from swarm_agents.chronos_bee import ChronosBeeHorizon
from swarm_agents.rival_bee import RivalBeeVanguard
from swarm_agents.guard_bee import GuardBeeSentinel
from swarm_agents.bear_bee import BearBeeContrarian
from swarm_agents.queen_distiller import QueenDistiller
