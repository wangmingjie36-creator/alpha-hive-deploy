"""09-17 完整蜂群顺序重放：用修复后的 momentum/volume 重算 BuzzBee，
让 GuardBeeSentinel/BearBeeContrarian/QueenDistiller 真跑（不是手算），
把每一处会碰到"今天"而非"09-17"的外部依赖钉死在当天存档值上。方法论与
逐条取证见 CHANGELOG v0.45.271、memory `alpha-hive-swarm-sequence-replay.md`。

CONTROL 模式：喂原始（未修复）momentum/volume，验证整条链能不能复现存档的
final_score/direction/dimension_weights——这是相信 CORRECTED 模式结果的前提。

⚠️ **本文件是补丁当时的审计存档，不是可重跑的通用工具**：`MOM`/`VOL` 读的是
会话临时目录里的两份 momentum/volume 映射文件（scratchpad，早已不存在）；
`db_price_at_predict` 之类路径也按当次 session 写死。要在别的日期复用这套
方法，需要重新生成对应的 momentum/volume 映射（见下方 `momentum_signal_from_raw`
之前的逻辑）、并把文件路径换成实际存在的位置——不能直接执行本文件。
"""
import copy
import json
import sys
from unittest.mock import patch

sys.path.insert(0, "/Users/igg/Desktop/Alpha Hive")

from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents.guard_bee import GuardBeeSentinel
from swarm_agents.bear_bee import BearBeeContrarian
from swarm_agents.queen_distiller import QueenDistiller
from swarm_agents.sentiment import _detect_sentiment_price_divergence
from swarm_agents.utils import clamp_score_cfg
from swarm_agents._config import _AS
import market_intelligence
import llm_service
import real_data_sources
from crowding_detector import CrowdingDetector
import advanced_analyzer

ROOT = "/Users/igg/Desktop/Alpha Hive"
SP = "/private/tmp/claude-501/-Users-igg-Desktop-Alpha-Hive/5c10bdb6-88da-4fb0-87bf-e8bab1b53c78/scratchpad"
AS_OF = "2026-09-17"

SWARM = json.load(open(f"{ROOT}/.swarm_results_{AS_OF}.json"))
MOM = json.load(open(f"{SP}/mom_0917_map.json"))
VOL = json.load(open(f"{SP}/vol_0917_map.json"))

_bw = _AS.get("buzz_weights", {})
_vt = _AS.get("volume_thresholds", {})


def db_price_at_predict(ticker):
    import sqlite3
    conn = sqlite3.connect(f"file:{ROOT}/pheromone.db?mode=ro", uri=True)
    row = conn.execute(
        "SELECT price_at_predict FROM predictions WHERE date=? AND ticker=?",
        (AS_OF, ticker)).fetchone()
    conn.close()
    return row[0] if row else None


def momentum_signal_from_raw(m):
    if m is None:
        return 50.0
    m = max(-10, min(10, m))
    return (m + 10) / 20 * 100


def volume_signal_from_ratio(v):
    if v is None:
        return 50
    if v > _vt.get("very_high", 2.0):
        return 80
    if v > _vt.get("high", 1.5):
        return 65
    if v > _vt.get("normal", 1.0):
        return 50
    if v > _vt.get("low", 0.5):
        return 35
    return 20


def build_buzz_result(ticker, control: bool):
    """返回完整 BuzzBeeWhisper AgentResult 风格 dict（含 source/data_quality）。
    control=True 时用存档原始 momentum_5d/volume_ratio（复现验证用）；
    control=False 时用 v0.45.270 修复后的真实回落值。
    """
    rec = SWARM[ticker]
    orig = copy.deepcopy(rec["agent_details"]["BuzzBeeWhisper"])
    det = orig["details"]
    comp = det["components"]

    if control:
        new_mom = det["momentum_5d"]
        new_vol = det["volume_ratio"]
    else:
        new_mom = MOM.get(ticker, {}).get("momentum_5d")
        new_vol = (VOL.get(ticker) or {}).get("volume_ratio")

    new_mom_sig = momentum_signal_from_raw(new_mom)
    new_vol_sig = volume_signal_from_ratio(new_vol)

    sentiment_composite = (
        new_mom_sig * _bw.get("momentum", 0.20) +
        new_vol_sig * _bw.get("volume", 0.10) +
        comp["volatility_signal"] * _bw.get("volatility", 0.05) +
        comp["reddit_signal"] * _bw.get("reddit", 0.25) +
        comp["news_signal"] * _bw.get("news", 0.25) +
        comp["yahoo_signal"] * _bw.get("yahoo", 0.05) +
        comp["fear_greed_signal"] * _bw.get("fear_greed", 0.10)
    )
    score = clamp_score_cfg(sentiment_composite / 10.0)

    sm = det["sentiment_momentum"]
    score += sm["momentum_score_adj"]

    sdiv = _detect_sentiment_price_divergence(int(sentiment_composite), new_mom, ticker)
    score += sdiv["score_adj"]
    score = clamp_score_cfg(score)

    bullish_pct = int(sentiment_composite)
    if sentiment_composite > _AS.get("direction_bullish_min", 60):
        direction = "bullish"
    elif sentiment_composite < _AS.get("direction_bearish_max", 40):
        direction = "bearish"
    else:
        direction = "neutral"

    new_det = copy.deepcopy(det)
    new_det["sentiment_pct"] = bullish_pct
    new_det["momentum_5d"] = new_mom
    new_det["volume_ratio"] = new_vol
    new_det["components"]["momentum_signal"] = round(new_mom_sig, 2)
    new_det["components"]["volume_signal"] = new_vol_sig
    new_det["sentiment_divergence"] = sdiv

    dq = (rec.get("data_quality") or {}).get("BuzzBeeWhisper", {}) or {}
    dq = dict(dq)
    dq["momentum"] = "real" if new_mom is not None else "unavailable"
    dq["volume"] = "real" if new_vol is not None else "unavailable"

    discovery_parts = [
        f"情绪 {bullish_pct}%",
        f"动量 {new_mom:+.1f}%" if new_mom is not None else "动量 N/A",
        f"量比 {new_vol:.1f}x" if new_vol is not None else "量比 N/A",
    ]
    # 保留原 discovery 里 reddit/news/yahoo/F&G 片段（在"量比"之后的部分)
    tail = orig["discovery"].split("|")
    tail = [t.strip() for t in tail[2:]] if len(tail) > 2 else []
    discovery = " | ".join(discovery_parts + tail)

    result = {
        "score": round(score, 2), "direction": direction,
        "confidence": orig["confidence"], "discovery": discovery,
        "source": "BuzzBeeWhisper", "dimension": "sentiment",
        "data_quality": dq, "details": new_det,
    }
    if orig.get("error") is not None:
        result["error"] = orig["error"]
    return result


def build_static_result(ticker, agent_name):
    """未受影响的蜂：原样复用存档（补 source/data_quality）。"""
    rec = SWARM[ticker]
    d = copy.deepcopy(rec["agent_details"][agent_name])
    d["source"] = agent_name
    d["data_quality"] = (rec.get("data_quality") or {}).get(agent_name, {}) or {}
    if d.get("error") is None:
        d.pop("error", None)
    return d


def reconstruct_pub_details(agent_name, full_details):
    """每只蜂 `_publish()` 发到板上的 S3 details 是一份**窄字典**，与它自己
    `AgentResult.details`（存档里那份，供报告展示用）完全是两份不同的东西
    （ChronosBee 存档 details 有 catalysts/analyst_targets/iv_crush/pead，
    S3 只有 catalyst_count/nearest_days/...）——存档只留了宽的那份，S3 窄字典
    要从宽字典逐字段重建，字段名与来源见各蜂 `_publish(..., details=...)` 调用点。
    """
    fd = full_details or {}
    if agent_name == "ScoutBeeNova":
        ins = fd.get("insider") or {}
        return {
            "crowding_score": fd.get("crowding_score"),
            "insider_sold_usd": ins.get("dollar_sold", 0) or 0,
            "insider_bought_usd": ins.get("dollar_bought", 0) or 0,
            "insider_sentiment": ins.get("score", 5.0),
        }
    if agent_name == "OracleBeeEcho":
        ts = fd.get("term_structure") or {}
        ds = fd.get("deep_skew") or {}
        mp = fd.get("max_pain") or {}
        out = {"pc_ratio": fd.get("put_call_ratio"), "iv_rank": fd.get("iv_rank"),
               "gex": fd.get("gamma_exposure")}
        if fd.get("iv_skew_ratio") is not None:
            out["iv_skew"] = fd["iv_skew_ratio"]
        if ts.get("structure") is not None:
            out["term_structure"] = ts["structure"]
            out["term_spread"] = ts.get("spread", 0)
        if ds.get("skew_25d") is not None:
            out["skew_25d"] = ds["skew_25d"]
        if mp.get("max_pain") is not None:
            out["max_pain"] = mp["max_pain"]
            out["max_pain_dist_pct"] = mp.get("distance_pct", 0)
        out["unusual_activity"] = (fd.get("unusual_activity") or [])[:15]
        return out
    if agent_name == "ChronosBeeHorizon":
        cats = fd.get("catalysts") or []
        at = fd.get("analyst_targets") or {}
        return {
            "catalyst_count": len(cats),
            "nearest_days": cats[0].get("days_until") if cats else None,
            "catalyst_types": list({c.get("type", "") for c in cats}),
            "analyst_upside_pct": at.get("upside_pct"),
            "analyst_mean_target": at.get("target_mean"),
        }
    if agent_name == "RivalBeeVanguard":
        return {
            "ml_probability": fd.get("ml_probability"),
            "expected_7d": fd.get("expected_7d"),
            "expected_30d": fd.get("expected_30d"),
            "momentum_5d": fd.get("momentum_5d"),
        }
    if agent_name == "BuzzBeeWhisper":
        out = {"sentiment_score": fd.get("sentiment_pct")}
        rm = fd.get("reddit_momentum")
        if rm is not None:
            out["reddit_momentum"] = rm
        return out
    if agent_name == "BearBeeContrarian":
        return {
            "bear_score": fd.get("bear_score"),
            "signal_count": len(fd.get("bearish_signals") or []),
            "top_risk": (fd.get("bearish_signals") or [""])[0][:60] if fd.get("bearish_signals") else "",
        }
    return fd  # GuardBee/CodeExecutorAgent: 板上就是全量 details，原样用


def publish(board, agent_name, ticker, d):
    board.publish(PheromoneEntry(
        agent_id=agent_name, ticker=ticker, discovery=d.get("discovery", ""),
        source=d.get("source", agent_name), self_score=d.get("score", 5.0),
        direction=d.get("direction", "neutral"),
        details=reconstruct_pub_details(agent_name, d.get("details", {}) or {}),
    ))


def build_bear_result(ticker, price, stock, guard_res, bear_si):
    """BearBeeContrarian 重算——**不**靠板子重读（板子淘汰时序难以复原，见
    alpha-hive-board-eviction.md）。改为逐子项按当天存档的 `data_sources`
    标签判定"那天这一路到底读到了没有"：

    - insider/options/catalyst/ml：无论那天走的是板读还是 API 兜底，两条路径
      的输入都与 momentum/volume 无关——**原样复用存档子分**（29/30 实测两条
      路径分桶公式不同，槽位互换不能用同一公式重算，必须整段复用）。
    - news：`data_sources.news=='real'` 时那天真读到了 BuzzBee 的板条目——
      用修复后的 sentiment_pct/direction 重算；否则那天走的是 newsapi 兜底，
      与我的修复无关，原样复用。
    - guard：`data_sources.guard=='real'` 时那天真读到了 GuardBeeSentinel——
      用本次重算的 Guard 结果（已验证 29/30 逐字节复现存档）重算；否则原样复用
      （那天这一路本来就是 0，读不到）。
    - valuation/momentum：从不经过板子，直接读 `stock` dict——始终用修复后的
      momentum_5d/volume_ratio 重算。
    - short_interest：与 momentum/volume 无关，原样复用存档 short_int_bear。
    """
    archived = SWARM[ticker]["agent_details"]["BearBeeContrarian"]["details"]
    ds = archived.get("data_sources", {})
    mom_5d = stock.get("momentum_5d")
    mom_5d_eff = mom_5d if mom_5d is not None else 0
    vol_ratio = stock.get("volume_ratio")

    bear_agent = BearBeeContrarian(PheromoneBoard())
    bear_agent._prefetched_market = {"info": {ticker: {"trailingPE": None}}}

    # 存档 bearish_signals 是一条不分类别的扁平列表，按各分支 f-string 的固定
    # 前缀分类挑出"这条属于哪个子项"——只留住 insider/options/catalyst/ml
    # 这四个**从不重算**的类别；momentum/valuation 永远重算，news/guard
    # 命中板读那天也重算，对应旧文本一律丢弃、换成新算出的信号。
    # 逐字核对 bear_bee.py 全部 bearish_signals.append(f"...") 调用点（含板读
    # 与各自 API 兜底两条路径各自的文案变体）得到的完整前缀表——2026-09-18
    # 首版漏了兜底路径独有的"内幕人净卖出"，靠 30 只全量控制组复现核对时
    # 抓出（ABBV 7.0→2.0 直接把一条真实信号丢没了）。改前缀表本身不能替代
    # 那次全量核对，因为漏项不会让脚本报错，只会让分数悄悄错。
    _PREFIXES = {
        "insider": ("内幕大额抛售", "内幕卖多买少", "内幕净卖出", "内幕人净卖出", "Scout 内幕信号看空"),
        "options": ("P/C Ratio", "IV Rank", "IV Skew", "GEX 负值", "Oracle 期权信号看空"),
        "chronos": ("催化剂", "分析师目标价下方"),
        "ml": ("ML模型看空", "ML预测7日回报"),
        "news": ("市场情绪", "Reddit 动量", "Buzz 情绪分析看空", "新闻情绪偏空", "新闻略偏空", "负面新闻主导"),
        "guard": ("信号一致性极低", "GuardBee检测到重大冲突"),
        "short_int": ("空头仓位", "回补天数"),
        # valuation/momentum 从不复用旧文案（永远重算），这两组前缀只用来让下面
        # 那道"全部条目都能归类"的自证过关，从不被 `_archived_signals_for` 调用。
        "valuation": ("5日暴涨", "5日涨幅过大", "5日涨幅 ", "P/E "),
        "momentum": ("5日下跌", "动量转弱", "近期小幅回调", "量能萎缩", "放量下跌", "量增价跌", "高波动率"),
    }
    # 自证：其余每一条存档信号都必须能归到某个类目（含 valuation/momentum，
    # 它们注定被丢弃重算，但"能识别出它属于哪类"本身就是核对前缀表完整性的
    # 手段）；"当前价 $...建议止损" 是空信号分支自己的输出，不该出现在存档里。
    # 归不到就说明前缀表本身漏了一种文案变体——上次漏"内幕人净卖出"就是这样
    # 悄悄丢分的，这道断言把同类问题从"跑出脏分数"变成"直接崩溃报错"。
    _ALL_PREFIXES = tuple(p for ps in _PREFIXES.values() for p in ps)
    for _s in (archived.get("bearish_signals") or []):
        if not _s.startswith(_ALL_PREFIXES) and not _s.startswith("当前价 $"):
            raise AssertionError(
                f"{ticker}: 存档 bearish_signal 无法归类，前缀表可能漏项: {_s!r}")

    def _archived_signals_for(cat):
        prefixes = _PREFIXES[cat]
        return [s for s in (archived.get("bearish_signals") or []) if s.startswith(prefixes)]

    bearish_signals = []
    dim_scores = {
        "insider": archived["insider_bear"],
        "options": archived["options_bear"],
        "chronos": archived.get("chronos_bear", 0.0),
        "ml": archived.get("ml_bear", 0.0),
        "guard": archived.get("guard_bear", 0.0),
        "short_int": archived.get("short_int_bear", 0.0),
    }
    for k in ("insider", "options", "chronos", "ml", "short_int"):
        bearish_signals.extend(_archived_signals_for(k))

    if ds.get("news") == "real":
        # 那天真读到 BuzzBee 的板条目——用修复后的 sentiment_pct/direction
        # 重算，文案逐字照抄 `_assess_news_sentiment` 的 f-string。
        news_bear = 0.0
        buzz_det = stock.get("_buzz_details_for_bear") or {}
        sentiment_pct = buzz_det.get("sentiment_pct")
        buzz_direction = buzz_det.get("direction")
        reddit_mom = buzz_det.get("reddit_momentum")
        if sentiment_pct is not None:
            if sentiment_pct < 30:
                news_bear = 7.5
                bearish_signals.append(f"市场情绪极度悲观 {sentiment_pct}%")
            elif sentiment_pct < 40:
                news_bear = 6.0
                bearish_signals.append(f"市场情绪偏空 {sentiment_pct}%")
            elif sentiment_pct < 45:
                news_bear = 4.0
                bearish_signals.append(f"市场情绪略偏谨慎 {sentiment_pct}%")
        if reddit_mom is not None and reddit_mom < -2:
            news_bear = max(news_bear, 6.0)
            bearish_signals.append(f"Reddit 动量急跌 {reddit_mom:+.1f}（社区看空转向）")
        elif reddit_mom is not None and reddit_mom < -0.5:
            news_bear = max(news_bear, 4.5)
            bearish_signals.append(f"Reddit 动量走弱 {reddit_mom:+.1f}")
        if buzz_direction == "bearish" and news_bear < 5.0:
            news_bear = max(news_bear, 5.5)
            _buzz_score = buzz_det.get("score", 5.0)
            bearish_signals.append(f"Buzz 情绪分析看空（{_buzz_score:.1f}分）")
        dim_scores["news"] = news_bear
    else:
        # 那天走的是 newsapi 兜底，与本次修复无关——原样复用存档数值与文案。
        dim_scores["news"] = archived.get("news_bear", 0.0)
        bearish_signals.extend(_archived_signals_for("news"))

    if ds.get("guard") == "real":
        # 那天真读到 GuardBeeSentinel 的板条目——用本次重算的 Guard 结果
        # （已验证逐字节复现存档）重算，文案照抄 `_assess_signal_consistency`。
        gd = guard_res.get("details", {})
        consist = gd.get("consistency")
        guard_bear = 0.0
        if consist is not None and consist < 0.4:
            guard_bear = 5.5
            bearish_signals.append(f"信号一致性极低({consist:.0%})，方向不确定")
        conflict = gd.get("llm_conflict_type", "")
        if conflict in ("major_conflict", "direction_conflict"):
            guard_bear = max(guard_bear, 6.0)
            bearish_signals.append(f"GuardBee检测到重大冲突: {conflict}")
        dim_scores["guard"] = guard_bear
    else:
        dim_scores["guard"] = archived.get("guard_bear", 0.0)
        bearish_signals.extend(_archived_signals_for("guard"))

    overval_bear = bear_agent._assess_valuation(
        ticker, stock, mom_5d_eff, price or 0, bearish_signals, {})
    momentum_bear = bear_agent._assess_momentum_decay(
        stock, mom_5d_eff, bearish_signals, {})
    dim_scores["valuation"] = overval_bear
    dim_scores["momentum"] = momentum_bear

    rule_bear_score = bear_agent._compute_bear_score(dim_scores, bearish_signals, price or 0, mom_5d_eff)
    final_bear_score = rule_bear_score  # LLM 已钉 False，final==rule，逐字照抄 analyze() 尾段

    from swarm_agents.utils import clamp_score as _clamp_score
    score = _clamp_score(10.0 - final_bear_score)

    try:
        from config import BEAR_SCORING_CONFIG as _BSC2
    except ImportError:
        _BSC2 = {}
    _bear_thresh = _BSC2.get("direction_bearish_min", 5.5)
    _neut_thresh = _BSC2.get("direction_neutral_min", 3.5)
    if final_bear_score >= _bear_thresh:
        direction = "bearish"
    elif final_bear_score >= _neut_thresh:
        direction = "neutral"
    else:
        direction = "bullish"

    discovery = " | ".join(bearish_signals[:6]) if bearish_signals else "未发现显著看空信号"
    confidence = min(1.0, 0.3 + len(bearish_signals) * 0.1)
    board_sources = sum(1 for v in ds.values() if v == "real")
    confidence = min(1.0, confidence + board_sources * 0.1)

    return {
        "score": round(score, 2), "direction": direction,
        "confidence": round(confidence, 2), "discovery": discovery,
        "source": "BearBeeContrarian", "dimension": "contrarian",
        "data_quality": dict(ds), "details": {
            "bear_score": round(final_bear_score, 2), "rule_bear_score": round(rule_bear_score, 2),
            "bearish_signals": bearish_signals, "insider_bear": dim_scores["insider"],
            "overval_bear": dim_scores["valuation"], "options_bear": dim_scores["options"],
            "momentum_bear": dim_scores["momentum"], "news_bear": dim_scores["news"],
            "short_int_bear": dim_scores["short_int"], "data_sources": ds,
        },
    }


def guard_archived_macro(ticker):
    gd = SWARM[ticker]["agent_details"]["GuardBeeSentinel"]["details"]
    return {
        "regime": gd["macro_regime"], "score_adj": gd["macro_adj"],
        "signals": gd["macro_signals"], "macro_summary": gd["macro_desc"],
        "details": gd["vix_term_structure"], "regime_votes": gd["macro_regime_votes"],
    }


def run_ticker(ticker: str, control: bool):
    rec = SWARM[ticker]
    # v0.45.270 根因取证：09-17 开局约 2 分钟全源故障，30/30 标的在
    # `_get_stock_data` 的 prefetch 阶段拿到的是 FALLBACK（price=0.0）——
    # 这份快照冻结后全天复用，Guard/Bear 那天真正看到的 stock["price"] 就是 0，
    # 不是后来补写进 predictions 表的 `price_at_predict`（那是更晚的独立修正，
    # 与 Guard/Bear 打分时用的价完全是两回事，同源于 09-17 这次事故本身——
    # `_assess_valuation` P/E 门槛 30/30 "unavailable"、`_compute_bear_score`
    # 空信号分支 price>0 判负，都是这条 price=0 的直接后果）。
    price = 0.0
    bear_si = rec["agent_details"]["BearBeeContrarian"]["details"].get("short_interest") or {}

    buzz_res = build_buzz_result(ticker, control)
    scout_res = build_static_result(ticker, "ScoutBeeNova")
    oracle_res = build_static_result(ticker, "OracleBeeEcho")
    chronos_res = build_static_result(ticker, "ChronosBeeHorizon")
    rival_res = build_static_result(ticker, "RivalBeeVanguard")
    code_res = None
    if "CodeExecutorAgent" in rec["agent_details"]:
        code_res = build_static_result(ticker, "CodeExecutorAgent")

    board = PheromoneBoard()
    for d in (scout_res, oracle_res, buzz_res, chronos_res, rival_res) + ((code_res,) if code_res else ()):
        publish(board, d["source"], ticker, d)

    stock = {
        "price": price, "momentum_5d": buzz_res["details"]["momentum_5d"],
        "volume_ratio": buzz_res["details"]["volume_ratio"], "volatility_20d": None,
        "avg_volume": None,
        "_buzz_details_for_bear": {
            "sentiment_pct": buzz_res["details"]["sentiment_pct"],
            "direction": buzz_res["direction"],
            "score": buzz_res["score"],
            "reddit_momentum": buzz_res["details"].get("reddit_momentum"),
        },
    }

    guard_agent = GuardBeeSentinel(board)
    guard_agent._prefetched_stock = {ticker: dict(stock)}
    guard_agent._prefetched_market = {"info": {ticker: {"trailingPE": None}}}

    macro_val = guard_archived_macro(ticker)
    cycle_val = rec["agent_details"]["GuardBeeSentinel"]["details"]["cycle_context"]
    regime_val = rec["agent_details"]["GuardBeeSentinel"]["details"]["market_regime"]
    archived_adj_factor = rec["agent_details"]["GuardBeeSentinel"]["details"]["adjustment_factor"]

    with patch.object(GuardBeeSentinel, "_calc_macro_adjustment", lambda self, tk: macro_val), \
         patch.object(market_intelligence, "get_cycle_context", lambda *a, **k: cycle_val), \
         patch.object(market_intelligence, "detect_market_regime", lambda *a, **k: regime_val), \
         patch.object(llm_service, "is_available", lambda: False), \
         patch.object(real_data_sources, "get_real_crowding_metrics",
                      lambda *a, **k: {"social_messages_per_day": 0, "google_trends_percentile": 50,
                                       "bullish_agents": 0, "seeking_alpha_page_views": 0,
                                       "short_float_ratio": 0, "price_momentum_5d": 0, "data_quality": {}}), \
         patch.object(CrowdingDetector, "get_adjustment_factor", lambda self, crowd: archived_adj_factor):
        guard_res = guard_agent.analyze(ticker)

    publish(board, "GuardBeeSentinel", ticker, guard_res)

    bear_res = build_bear_result(ticker, price, stock, guard_res, bear_si)
    # BearBee 在生产里也会 `_publish()` 自己（"bear_contrarian"），
    # `distill()` 开头的 `self.board.detect_resonance(ticker)` 读的正是这一条——
    # 漏发会让共振少算一个跨维度支持者（contrarian 维度），CRCL/T 两只标的
    # 曾因此整段共振判定翻转（resonance_detected True→False，confidence_boost
    # 15→0，直接把 final_score 拉低将近 1 分——这不是近似误差，必须发布）。
    publish(board, "BearBeeContrarian", ticker, bear_res)

    agent_results = [scout_res, oracle_res, buzz_res, chronos_res, rival_res, guard_res, bear_res]
    if code_res:
        agent_results.append(code_res)

    queen = QueenDistiller(board, enable_llm=False)
    # __init__ 只在传了 ml_model= 时才做「乘 ml_adjustments 再归一化」这步
    # （见 queen_distiller.py:161-178）；这里事后补上同一段逻辑，逐字复刻，
    # 保证 DIMENSION_WEIGHTS（喂进 RegimeWeightAdjuster 的基准权重）与生产一致。
    queen.ml_adjustments = dict(rec["ml_weight_adjustments"])
    queen.ml_feedback_enabled = True
    try:
        from config import ML_FEEDBACK_CONFIG as _MFC
    except ImportError:
        _MFC = {}
    if _MFC.get("enable_dimension_weighting", True):
        for _dim, _factor in queen.ml_adjustments.items():
            if _dim in queen.DIMENSION_WEIGHTS:
                queen.DIMENSION_WEIGHTS[_dim] *= _factor
        _total = sum(queen.DIMENSION_WEIGHTS.values())
        if _total > 0:
            queen.DIMENSION_WEIGHTS = {k: round(v / _total, 4) for k, v in queen.DIMENSION_WEIGHTS.items()}

    with patch.object(advanced_analyzer.DealerGEXAnalyzer, "analyze", lambda self, tk, px: {}):
        distilled = queen.distill(ticker, agent_results, dealer_gex={})

    return dict(
        buzz=buzz_res, guard=guard_res, bear=bear_res, distilled=distilled,
        price=price,
    )


if __name__ == "__main__":
    import sys as _sys
    tickers = _sys.argv[1:] or ["QCOM"]
    for tk in tickers:
        rec = SWARM[tk]
        ctrl = run_ticker(tk, control=True)
        d = ctrl["distilled"]
        print(f"\n=== {tk} CONTROL ===")
        print(f"  archived final_score={rec['final_score']:.2f} dir={rec['direction']}")
        print(f"  replay   final_score={d['final_score']:.2f} dir={d['direction']}")
        print(f"  archived dimension_weights={rec['dimension_weights']}")
        print(f"  replay   dimension_weights={d['dimension_weights']}")
        print(f"  archived guard score={rec['agent_details']['GuardBeeSentinel']['score']} dir={rec['agent_details']['GuardBeeSentinel']['direction']}")
        print(f"  replay   guard score={ctrl['guard']['score']} dir={ctrl['guard']['direction']}")
        print(f"  archived bear  score={rec['agent_details']['BearBeeContrarian']['score']} dir={rec['agent_details']['BearBeeContrarian']['direction']}")
        print(f"  replay   bear  score={ctrl['bear']['score']} dir={ctrl['bear']['direction']}")
