"""
Alpha Hive - 真实数据源层
替换 ScoutBeeNova 中所有伪造的拥挤度指标

数据源：
- 社交热度：Reddit ApeWisdom（StockTwits 公开 API 自 v0.40.0 已 403 停用）
- yfinance short interest（机构持仓+做空比例）
- 信息素板动态 bullish_agents 计数
"""

import json
import logging as _logging
import time
import threading
from pathlib import Path
from typing import Dict, Optional

from hive_logger import atomic_json_write

_log = _logging.getLogger("alpha_hive.real_data_sources")

try:
    from resilience import NETWORK_ERRORS
except ImportError:
    NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError, ValueError, KeyError)

try:
    import requests
except ImportError:
    requests = None

# v0.45.160：`CACHE_DIR` 现在是**覆盖钩子**，默认 `None` ⇒ 运行时解析 `PATHS.*`。
# 原本是 `Path(__file__).parent / "data_cache"` —— 它**压根不读任何环境变量**，
# 比「模块级常量冻在 import 期」更彻底：`ALPHA_HIVE_HOME` / `ALPHA_HIVE_CACHE_DIR`
# 设成什么都无效，`tests/conftest.py::_isolate_env` 对它完全无效。
# 保留这个名字是因为 `tests/` 有 `monkeypatch.setattr(<mod>, "CACHE_DIR", ...)` 依赖它。
CACHE_DIR = None


def _cache_dir() -> Path:
    """本模块的缓存落点。**调用时求值**（别求值成模块级常量或默认参数）。"""
    if CACHE_DIR is not None:
        return Path(CACHE_DIR)
    from hive_logger import PATHS
    d = Path(PATHS.home) / "data_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d

_lock = threading.Lock()

# ── 数据源健康追踪（#7）──
_HEALTH_FAIL_THRESHOLD = 3
_src_fail_counts: Dict[str, int] = {}
_src_degraded: Dict[str, bool] = {}


def _record_src_failure(source: str):
    """记录数据源连续失败，达到阈值时写 WARNING 日志（不发 Slack）。

    v0.45.339：此前阈值处还调 `_try_src_slack_alert` → `send_risk_alert`。
    本机有 Bot Token ⇒ `enabled` 恒真；Bot 不在频道 ⇒ 降级成**私信用户**
    （生产日志实测：v0.45.131 修好测试隔离之后仍有 yfinance_short_interest 的
    真实私信）。CLAUDE.md「Slack 通知精简规则」明令禁止「数据质量降级预警」，
    故该函数整个删除，原 Slack 正文里唯一多出的一句（数据质量受影响）并进下面
    这行日志。守卫 `tests/test_slack_send_whitelist.py`。
    """
    _src_fail_counts[source] = _src_fail_counts.get(source, 0) + 1
    count = _src_fail_counts[source]
    if count == _HEALTH_FAIL_THRESHOLD:
        _log.warning("⚠️ 数据源 [%s] 连续失败 %d 次，触发降级告警：进入降级模式，数据质量受影响",
                     source, count)
        _src_degraded[source] = True
    elif count > _HEALTH_FAIL_THRESHOLD and count % 5 == 0:
        _log.warning("⚠️ 数据源 [%s] 持续降级，累计失败 %d 次", source, count)


def _record_src_success(source: str):
    """记录数据源成功，重置计数器"""
    prev = _src_fail_counts.get(source, 0)
    if prev >= _HEALTH_FAIL_THRESHOLD:
        _log.info("✅ 数据源 [%s] 已恢复（之前连续失败 %d 次）", source, prev)
    _src_fail_counts[source] = 0
    _src_degraded[source] = False


def _read_cache(name: str, ttl: int = 3600) -> Optional[Dict]:
    """读磁盘缓存"""
    path = _cache_dir() / f"{name}.json"
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < ttl:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                _log.debug("缓存读取失败 %s: %s", name, exc)
    return None


def _write_cache(name: str, data: Dict):
    """写磁盘缓存"""
    try:
        atomic_json_write(_cache_dir() / f"{name}.json", data)
    except (OSError, TypeError) as exc:
        _log.debug("缓存写入失败 %s: %s", name, exc)


# ==================== 社交热度（Reddit ApeWisdom + 成交量代理）====================

def get_social_buzz(ticker: str) -> Dict:
    """
    获取社交热度数据（基于 Reddit ApeWisdom 提及量）

    StockTwits 公开 API 已停用(403)，改用 Reddit 提及量作为社交热度代理。
    提及量映射为等效 messages_per_day，便于 CrowdingDetector 使用。

    返回:
        {
            "messages_per_day": int,     # Reddit 提及量映射为等效日消息量
            "bullish_pct": float,        # 看涨比例 0-100
            "bearish_pct": float,        # 看跌比例 0-100
            "data_quality": "real" | "fallback",
            "source": "reddit_apewisdom"
        }
    """
    cache_key = f"social_{ticker}"
    cached = _read_cache(cache_key, ttl=3600)
    if cached:
        return cached

    fallback = {
        "messages_per_day": 0,
        "bullish_pct": 50.0,
        "bearish_pct": 50.0,
        "data_quality": "fallback",
        "source": "none",
    }

    try:
        from reddit_sentiment import get_reddit_sentiment
        reddit = get_reddit_sentiment(ticker)

        mentions = reddit.get("mentions", 0)
        score = reddit.get("sentiment_score", 5.0)
        rank = reddit.get("rank")

        # 将 Reddit 提及量映射为等效 StockTwits 日消息量
        # Reddit 前 10 名 ≈ 30k-100k StockTwits 消息
        # Reddit 前 50 名 ≈ 5k-30k
        # 不在榜 ≈ 0
        if rank and rank <= 5:
            equiv_messages = max(mentions * 200, 50000)
        elif rank and rank <= 15:
            equiv_messages = max(mentions * 100, 20000)
        elif rank and rank <= 50:
            equiv_messages = max(mentions * 50, 5000)
        elif mentions > 0:
            equiv_messages = mentions * 30
        else:
            equiv_messages = 0

        # Reddit sentiment_score (1-10) → bullish_pct
        bullish_pct = min(100.0, max(0.0, (score - 1) / 8 * 100))

        result = {
            "messages_per_day": equiv_messages,
            "bullish_pct": round(bullish_pct, 1),
            "bearish_pct": round(100.0 - bullish_pct, 1),
            "data_quality": "real",  # API 成功返回即为真实数据，不在榜≠降级
            "source": "reddit_apewisdom",
        }

        _write_cache(cache_key, result)
        _record_src_success("reddit_apewisdom")
        return result

    except (*NETWORK_ERRORS, ImportError) as exc:
        _record_src_failure("reddit_apewisdom")
        _log.debug("get_social_buzz 降级为 fallback (%s): %s", ticker, exc)
        return fallback


# ==================== Short Interest (yfinance) ====================

def get_short_interest(ticker: str) -> Dict:
    """
    从 yfinance 获取做空比例数据

    返回:
        {
            "short_ratio": float,        # 做空天数比率（空头回补天数）
            "short_pct_float": float,    # 做空比例占流通股
            "data_quality": "real" | "fallback"
        }
    """
    cache_key = f"short_{ticker}"
    cached = _read_cache(cache_key, ttl=86400)  # 做空数据日更，缓存 24h
    if cached:
        return cached

    fallback = {
        "short_ratio": 0.0,
        "short_pct_float": 0.05,
        "data_quality": "fallback",
    }

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info

        short_ratio = info.get("shortRatio", 0.0) or 0.0
        short_pct = info.get("shortPercentOfFloat", 0.0) or 0.0

        # yfinance 返回的 shortPercentOfFloat 可能是百分比或小数
        if short_pct > 1.0:
            short_pct = short_pct / 100.0

        result = {
            "short_ratio": round(float(short_ratio), 2),
            "short_pct_float": round(float(short_pct), 4),
            "data_quality": "real" if (short_ratio > 0 or short_pct > 0) else "fallback",
        }

        _write_cache(cache_key, result)
        # ── v0.45.47：健康追踪必须与本函数自己的质量判定一致 ──
        # 旧写法**无条件** _record_src_success。而 _record_src_success 会把
        # 连续失败计数**重置为 0** —— 于是一个永远返回空数据的源，
        # 「连续失败 3 次」的降级告警**永远不可能触发**，每次调用都清零。
        # 上面第 217 行刚把这种情况判成 data_quality="fallback"，
        # 下一行却告诉追踪器「这次成功了」，两者自相矛盾。
        #
        # 注意语义：这里的「失败」不是网络错误，是**源没有交付可用数据**。
        # 对健康度而言两者后果相同 —— 拿不到就是拿不到。
        if result["data_quality"] == "real":
            _record_src_success("yfinance_short_interest")
        else:
            _log.debug("yfinance_short_interest %s 返回空（short_ratio=%s pct=%s），"
                       "记为一次降级而非成功", ticker, short_ratio, short_pct)
            _record_src_failure("yfinance_short_interest")
        return result

    except (*NETWORK_ERRORS, TypeError, AttributeError) as exc:
        _record_src_failure("yfinance_short_interest")
        _log.debug("get_short_interest 降级为 fallback (%s): %s", ticker, exc)
        return fallback


# ==================== 动态 Bullish Agents 计数 ====================

#: Phase-1 同伴（不含 ScoutBeeNova 自己）——与
#: `alpha_hive_daily_report.py::_init_scan_context` 的 `phase1_agents` 列表
#: 及 `signal_archive._PHASE1` 同源，三处各自维护、含义必须一致。
#: `parallel_agent_runner.PHASE_1_AGENTS`（含 RivalBeeVanguard）是未接入生产
#: 主链的旧模块，口径不同，不能拿来对齐。
_PHASE1_PEERS = frozenset({
    "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon", "CodeExecutorAgent",
})


def get_bullish_agents_detail(ticker: str, board) -> Optional[Dict]:
    """
    读板一次，同时给出「谁在场」与「谁看多」——口径标记（caliber marker），
    不只是一个数字。

    v0.45.279 新增。不是为了替代 `signal_archive.SIGNAL_UPSTREAM` 里
    `crowding.comp.consensus_strength` 依赖 phase1 方向的那条边（那条边结构上
    是对的：这个数字的含义确实会随同伴的方向定义改变而改变）——是为了让
    「当时到底数到了谁」变成可核查的观测量，类似 `guard_bee` 的
    `top_signals_count`/`census_source`，以后不用再翻代码猜。

    Returns:
        `{"peers_live": [...], "peers_bullish": [...]}`，两个列表都已排序、
        只含 `_PHASE1_PEERS` 里的 agent_id；`board` 缺失或读取异常时返回 `None`。
    """
    if board is None:
        return None
    try:
        live = [s for s in board.get_live_signals(ticker) if s.agent_id in _PHASE1_PEERS]
        return {
            "peers_live": sorted(s.agent_id for s in live),
            "peers_bullish": sorted(s.agent_id for s in live if s.direction == "bullish"),
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        _log.debug("get_bullish_agents_detail 读板失败 (%s): %s", ticker, exc)
        return None


def get_bullish_agents_count(ticker: str, board) -> Optional[int]:
    """
    从信息素板动态计算「看涨的 Phase-1 同伴数」（0-4）。

    v0.45.279：此前用 `board.get_top_signals(ticker, n=10)`（排行榜、且不按
    身份过滤）——与 v0.45.151/156/163 修过的三处是同一个「排行榜当普查用」
    缺陷：`_entries` 满时按 `nlargest(MAX_ENTRIES, key=self_score)` 全局淘汰，
    先扔分最低的，缺失与被测量的量反相关；且未按身份过滤时，board 上任何
    非 Phase-1 同伴的高分条目都会被误计入。改用不受淘汰影响的 `get_live_signals`
    （普查视图），并按 `_PHASE1_PEERS` 精确过滤——与 `get_bullish_agents_detail`
    读同一份数据，只是只要计数时不必再算一次 detail。
    量测局限：这是 race-condition 依赖的读取，没有历史快照能重建 Scout 读板
    那一刻的真实板面（`pheromone_compact` 拍得太晚），故本次只证明机制、
    不给生产翻转率——那个数字不存在，不编（详见
    `tests/test_bullish_agents_census_eviction.py` 模块 docstring）。

    Args:
        ticker: 股票代码
        board: PheromoneBoard 实例

    Returns:
        看涨的 Phase-1 同伴数（0-4）；`board` 缺失或读取异常时返回 `None`
        （诚实缺失，交给 `crowding_detector` 的缺失分量重归一化通路处理，
        不得编一个看似合理的数字——旧的硬编码 `3` 正是这个反面教材）。
    """
    detail = get_bullish_agents_detail(ticker, board)
    return None if detail is None else len(detail["peers_bullish"])


# ==================== 统一拥挤度指标获取 ====================

def get_real_crowding_metrics(ticker: str, stock_data: Dict, board=None) -> Dict:
    """
    构建真实的拥挤度指标（替代 ScoutBeeNova 中的伪造数据）

    Args:
        ticker: 股票代码
        stock_data: 来自 _fetch_stock_data() 的 yfinance 数据
        board: PheromoneBoard 实例（用于动态 bullish_agents）

    Returns:
        {
            "social_messages_per_day": int,        # Reddit ApeWisdom 代理（非 StockTwits）
            "google_trends_percentile": float,     # 暂用成交量百分位代替（标记降级）
            "bullish_agents": int,                 # 信息素板动态计数
            "seeking_alpha_page_views": int,       # 暂用成交量比率代替（标记降级）
            "short_float_ratio": float,            # 真实 yfinance 做空数据
            "price_momentum_5d": float,            # 真实 yfinance 动量
            "data_quality": {...}                  # 每个指标的数据质量标记
        }
    """
    # 1. 社交热度（Reddit ApeWisdom 提及量）
    st_data = get_social_buzz(ticker)

    # 2. Short Interest（真实 yfinance）
    short_data = get_short_interest(ticker)

    # 3. 动态 bullish_agents（真实信息素板）
    census = get_bullish_agents_detail(ticker, board)
    bullish = None if census is None else len(census["peers_bullish"])

    # 4. Google Trends — 暂不接入 pytrends（高频使用会被封 IP），
    #    改用成交量异动百分位作为"关注度"代理指标
    # v0.41.4: .get(key, default) 对显式 None 值无效（v36/v40.1 起 yfinance
    # 历史K线拉取失败时 momentum_5d/volume_ratio 被诚实置 None，而非缺键）——
    # 深夜限流命中时 vol_ratio=None 直接与 0.5 相减崩溃，ScoutBee 全体标的
    # 报错（2026-07-21 14:02 定时扫描事故）。此处显式判 None 回落中性代理值。
    vol_ratio = stock_data.get("volume_ratio")
    vol_ratio = vol_ratio if vol_ratio is not None else 1.0
    # volume_ratio → 百分位映射: 0.5x=20, 1.0x=50, 2.0x=80, 3.0x=95
    google_proxy = min(100, max(0, (vol_ratio - 0.5) / 2.5 * 80 + 20))

    # 5. （v0.45.30 删除）Polymarket 赔率变化速度代理。
    #    原实现 poly_proxy = |momentum_5d| * 0.8 —— 把动量改个名字冒充赔率变化，
    #    而同一 dict 里 price_momentum_5d 已在喂 short_squeeze_risk，等于动量被
    #    重复计权。实测 8 月 250 个样本：该分量 76% 落在最低档常数 20，
    #    其余 24% 的变化全部来自动量本身。既是常数稀释又是暗中双计，故整项移除。
    _mom_for_proxy = stock_data.get("momentum_5d")

    # 6. Seeking Alpha — 无免费 API，
    #    改用社交热度消息量 * 2 作为"页面浏览"代理
    sa_proxy = st_data["messages_per_day"] * 2

    metrics = {
        "social_messages_per_day": st_data["messages_per_day"],
        "google_trends_percentile": round(google_proxy, 1),
        "bullish_agents": bullish,
        # v0.45.279：口径标记（谁在场、谁看多），见 `get_bullish_agents_detail`。
        "consensus_census": census,
        "seeking_alpha_page_views": sa_proxy,
        "short_float_ratio": short_data["short_pct_float"],
        # v0.45.44：动量取不到时给 None，不再兜底 0.0（读作「5 日横盘」）
        "price_momentum_5d": _mom_for_proxy,
        "data_quality": {
            "social_buzz": st_data["data_quality"],         # Reddit ApeWisdom 真实数据
            "google_trends": "proxy_volume",                # 成交量代理指标
            # v0.45.279：原为 `"real" if board else "default"`——只看 board 是否
            # 传了，不看真读出来的值是不是 None（board 传了但读取异常时，标签
            # 仍会自称 real）。改按 bullish 本身是否为 None 推导，与旁边的
            # momentum 同一写法——那条的注释早就点名过这个反例。
            "bullish_agents": "real" if bullish is not None else "unavailable",
            "seeking_alpha": "proxy_social",                # 社交热度代理指标
            "short_interest": short_data["data_quality"],   # yfinance 真实数据
            # v0.45.44：这里原是**硬编码字符串** "real" —— 质量标签不是从数据
            # 推导出来的，是写死的。取数失败时 price_momentum_5d 兜底成 0.0，
            # 而标签仍自称 real，下游无法与「真的横盘」区分。
            "momentum": "real" if _mom_for_proxy is not None else "unavailable",
        }
    }

    return metrics
