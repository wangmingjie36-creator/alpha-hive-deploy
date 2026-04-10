#!/usr/bin/env python3
"""
🐝 Alpha Hive · 混合模式深度报告生成器 · Template C v3.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本地 JSON 读取  +  Claude API 深度推理  →  Template C HTML

用法:
  python3 generate_deep_v2.py --ticker NVDA
  python3 generate_deep_v2.py --ticker NVDA --date 2026-03-10
  python3 generate_deep_v2.py --ticker VKTX --no-llm   # 纯本地模式，跳过 API

输出:
  deep-{TICKER}-{DATE}.html  保存到 Alpha Hive 文件夹

版本历史:
  v3.0  2026-03-12  Template C v3.0 基础版本，7章结构 + LLM推理
  v3.1  2026-03-13  ① Delta分析(昨日JSON对比上下文) ② 链式推理(delta注入swarm/macro prompt)
                    ③ 昨日涨跌箭头(score-card Δ显示) ④ T-7预测闭环widget ⑤ 4-tuple agents修复
  v3.2  2026-03-13  SYSTEM_PROMPT全面升级：期权阈值体系/信号优先级/冲突处理规则/评分解读标准
  v3.3  2026-03-13  真正两步链式推理：Step1分析框架(STEP1_SYSTEM+step1_prompts) → Step2叙事写作
  v3.4  2026-03-13  跨章上下文：extract_thesis()+master_thesis注入CH6/CH7 step1&step2 prompt
  v3.5  2026-03-13  矛盾检测：detect_conflicts()自动检测5类矛盾，强制注入swarm/options prompt
"""

VERSION = "3.5"

import json
import os
import sys
import glob
import argparse
import re
import math
from datetime import datetime, date
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────────────────────────
ALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))
# 支持 VM 路径（Cowork 模式）—— 自动检测当前会话
import glob as _glob_mod
_VM_SESSIONS = sorted(_glob_mod.glob("/sessions/*/mnt/Alpha Hive"), reverse=True)
_VM_PATH = Path(_VM_SESSIONS[0]) if _VM_SESSIONS else Path("/sessions/keen-magical-wright/mnt/Alpha Hive")
try:
    if _VM_PATH.exists():
        ALPHAHIVE_DIR = _VM_PATH
except PermissionError:
    pass
# 默认输出到用户真实桌面的深度报告文件夹（VM 模式优先）
_VM_DEEP_SESSIONS = sorted(_glob_mod.glob("/sessions/*/mnt/深度分析报告/深度"), reverse=True)
_VM_DEEP_DIR = Path(_VM_DEEP_SESSIONS[0]) if _VM_DEEP_SESSIONS else Path("/sessions/keen-magical-wright/mnt/深度分析报告/深度")
try:
    if _VM_DEEP_DIR.exists():
        OUTPUT_DIR = _VM_DEEP_DIR
    else:
        OUTPUT_DIR = Path(os.path.expanduser("~/Desktop/深度分析报告/深度"))
except PermissionError:
    OUTPUT_DIR = Path(os.path.expanduser("~/Desktop/深度分析报告/深度"))
API_KEY_FILE = Path("~/.anthropic_api_key").expanduser()
# 在 VM 中，home 可能映射到不同路径
_VM_API_KEY = Path(str(ALPHAHIVE_DIR / ".anthropic_api_key"))
# Mac 上直接放在项目文件夹里也可以
_MAC_API_KEY = Path("~/Desktop/Alpha Hive/.anthropic_api_key").expanduser()
if not API_KEY_FILE.exists() and _VM_API_KEY.exists():
    API_KEY_FILE = _VM_API_KEY
elif not API_KEY_FILE.exists() and _MAC_API_KEY.exists():
    API_KEY_FILE = _MAC_API_KEY

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def find_latest_json(ticker: str, date_str: str | None = None) -> Path:
    """找到最新的 analysis JSON 文件"""
    pattern = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"找不到 {ticker} 的分析 JSON: {pattern}")
    if date_str:
        target = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-{date_str}.json")
        if os.path.exists(target):
            return Path(target)
        print(f"⚠️  指定日期 {date_str} 文件不存在，使用最新: {files[-1]}")
    return Path(files[-1])


def find_prev_json(ticker: str, current_path: Path, days_back: int = 1) -> Path | None:
    """找到当前文件之前第 N 个交易日的 JSON"""
    pattern = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-*.json")
    files = sorted(glob.glob(pattern))
    try:
        idx = files.index(str(current_path))
    except ValueError:
        idx = len(files)
    target_idx = idx - days_back
    if target_idx >= 0:
        return Path(files[target_idx])
    return None


def extract_thesis(html: str) -> str:
    """从 CH1 HTML 叙事中提取核心论点（首段第一句话，去除HTML标签）"""
    text = re.sub(r'<[^>]+>', '', html).strip()
    m = re.search(r'^(.+?[。！？.!?])', text)
    thesis = m.group(1).strip() if m else text[:100].strip()
    return thesis


def extract_chapter_summary(html: str, max_chars: int = 120) -> str:
    """从章节 HTML prose 中提取首句核心结论，供下一章节滚动注入。去除 HTML 标签后取第一个中文句号前的内容。"""
    text = re.sub(r'<[^>]+>', '', html).strip()
    text = re.sub(r'\s+', ' ', text)
    for sep in ['。', '；']:
        idx = text.find(sep)
        if 0 < idx <= max_chars:
            return text[:idx + 1].strip()
    return (text[:max_chars] + '…').strip() if len(text) > max_chars else text.strip()


def detect_conflicts(ctx: dict) -> list[str]:
    """自动检测蜂群信号中的矛盾对，返回中文描述列表（空列表=无矛盾）"""
    conflicts = []
    score = ctx.get("final_score", 5.0)
    direction = ctx.get("direction", "neutral")
    ml_7d = ctx.get("ml_7d", 0.0)
    flow = ctx.get("flow_direction", "").lower()
    res_detected = ctx.get("resonance", {}).get("resonance_detected", False)

    try:
        pc = float(ctx.get("put_call_ratio", 1.0))
    except (ValueError, TypeError):
        pc = 1.0
    try:
        iv_skew = float(ctx.get("iv_skew", 1.0))
    except (ValueError, TypeError):
        iv_skew = 1.0

    # 1. P/C vs 综合评分
    if pc < 0.80 and score < 4.5:
        conflicts.append(
            f"⚠️ 期权P/C={pc:.2f}（<0.80偏多）↔ 综合评分{score:.1f}（<4.5偏空）"
            f"：smart money买Call但蜂群系统看空，需判断谁领先"
        )
    if pc > 1.20 and score > 6.5:
        conflicts.append(
            f"⚠️ 期权P/C={pc:.2f}（>1.20偏空）↔ 综合评分{score:.1f}（>6.5偏多）"
            f"：机构大量买Put对冲但蜂群系统看多，需判断是对冲还是预警"
        )

    # 2. ML预期 vs 蜂群方向
    if ml_7d > 3.0 and "bear" in direction:
        conflicts.append(
            f"⚠️ ML量化预期7日+{ml_7d:.1f}%（看涨）↔ 蜂群方向看空"
            f"：量化模型与多维信号分歧，关注期权流方向作为裁判"
        )
    if ml_7d < -3.0 and "bull" in direction:
        conflicts.append(
            f"⚠️ ML量化预期7日{ml_7d:.1f}%（看跌）↔ 蜂群方向看多"
            f"：量化模型与多维信号分歧，关注是否有基本面支撑"
        )

    # 3. IV Skew vs P/C（期权内部矛盾）
    if iv_skew > 1.15 and pc < 0.85:
        conflicts.append(
            f"⚠️ IV Skew={iv_skew:.2f}（>1.15下行恐慌溢价）↔ P/C={pc:.2f}（<0.85 Call主导）"
            f"：期权内部矛盾，保护性Put贵但同时也在买Call，可能是双向押注"
        )

    # 4. 期权流向 vs 综合评分
    if "bull" in flow and score < 4.5:
        conflicts.append(
            f"⚠️ 期权流向偏多（{ctx.get('flow_direction','')}）↔ 综合评分{score:.1f}偏空"
            f"：期权流显示买方看多但蜂群量化偏空，注意流向是否为短线投机"
        )
    if "bear" in flow and score > 6.5:
        conflicts.append(
            f"⚠️ 期权流向偏空（{ctx.get('flow_direction','')}）↔ 综合评分{score:.1f}偏多"
            f"：期权流偏空头对冲但蜂群量化看多，需判断是风险管理还是看空押注"
        )

    # 5. 共振激活 vs 中性评分
    if res_detected and 4.0 < score < 6.0:
        res_dir = ctx.get("resonance", {}).get("direction", "N/A")
        conflicts.append(
            f"⚠️ 蜂群共振激活（方向:{res_dir}）↔ 综合评分{score:.1f}处于中性区间（4-6）"
            f"：共振方向与评分未形成合力，需核实共振维度是否与期权流一致"
        )

    return conflicts


def extract_simple(data: dict) -> dict:
    """轻量提取，用于昨日/T-7 对比，只取关键数值"""
    sr  = data.get("swarm_results", {})
    ad  = sr.get("agent_details", {})
    odet = ad.get("OracleBeeEcho", {}).get("details", {})
    scout_det = ad.get("ScoutBeeNova", {}).get("details", {})
    price = data.get("closing_price") or scout_det.get("price", None)
    def _s(key): return float(ad.get(key, {}).get("score", 0) or 0)
    return {
        "final_score": float(sr.get("final_score", 0) or 0),
        "direction":   sr.get("direction", "neutral"),
        "price":       float(price) if price else None,
        "chronos": _s("ChronosBeeHorizon"),
        "rival":   _s("RivalBeeVanguard"),
        "oracle":  _s("OracleBeeEcho"),
        "buzz":    _s("BuzzBeeWhisper"),
        "scout":   _s("ScoutBeeNova"),
        "guard":   _s("GuardBeeSentinel"),
        "bear":    _s("BearBeeContrarian"),
        "put_call_ratio": odet.get("put_call_ratio", None),
        "iv_skew":        odet.get("iv_skew_ratio", None),
        "total_oi":       odet.get("total_oi", None),
        # BUG-F fix: DoD delta for IV and regime requires these fields in prev context
        "iv_current":     float(odet.get("iv_current", 0) or 0),
        "market_regime":  (ad.get("GuardBeeSentinel", {})
                              .get("details", {}) or {})
                              .get("market_regime", {}),
    }


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_api_key() -> str | None:
    if API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text().strip()
        return key if key.startswith("sk-") else None
    return os.environ.get("ANTHROPIC_API_KEY")


def fetch_live_news(ticker: str) -> str:
    """拉取 Finnhub + Alpha Vantage 实时新闻与情绪，返回注入 LLM prompt 的文本块。
    失败时静默返回空字符串，不阻断报告生成。"""
    import urllib.request
    from datetime import timedelta

    def _load_key(*paths: str) -> str:
        """按优先级依次尝试多个路径，返回第一个非空 key"""
        for path in paths:
            try:
                val = Path(path).expanduser().read_text().strip()
                if val:
                    return val
            except Exception:
                pass
        return ""

    # 优先读 Mac home，次选 workspace（Cowork VM 定时任务环境）
    _script_dir = Path(__file__).parent
    finnhub_key = _load_key(
        "~/.alpha_hive_finnhub_key",
        str(_script_dir / ".alpha_hive_finnhub_key"),
    )
    av_key = _load_key(
        "~/.alpha_hive_av_key",
        str(_script_dir / ".alpha_hive_av_key"),
    )

    if not finnhub_key and not av_key:
        return ""

    lines = ["【实时新闻与情绪（Finnhub + Alpha Vantage）】"]
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    # ── Finnhub 公司新闻（最近7天，最多6条）──
    if finnhub_key:
        try:
            url = (
                f"https://finnhub.io/api/v1/company-news"
                f"?symbol={ticker}&from={week_ago}&to={today}&token={finnhub_key}"
            )
            req = urllib.request.urlopen(url, timeout=5)
            news = json.loads(req.read())[:6]
            if news:
                lines.append("近期头条（Finnhub）：")
                for n in news:
                    ts = datetime.fromtimestamp(n.get("datetime", 0)).strftime("%m-%d") if n.get("datetime") else "??"
                    lines.append(f"  · [{ts}] {n.get('headline', '')[:80]}")
        except Exception:
            pass

    # ── Alpha Vantage 新闻情绪打分（最近20条）──
    if av_key:
        try:
            url = (
                f"https://www.alphavantage.co/query"
                f"?function=NEWS_SENTIMENT&tickers={ticker}&apikey={av_key}&limit=20&sort=LATEST"
            )
            req = urllib.request.urlopen(url, timeout=8)
            data = json.loads(req.read())
            feed = data.get("feed", [])
            if feed:
                scores = []
                bull_n = bear_n = neutral_n = 0
                for art in feed:
                    for ts_item in art.get("ticker_sentiment", []):
                        if ts_item.get("ticker") == ticker:
                            try:
                                s = float(ts_item.get("ticker_sentiment_score", 0))
                                scores.append(s)
                                lbl = ts_item.get("ticker_sentiment_label", "Neutral")
                                if "Bullish" in lbl:
                                    bull_n += 1
                                elif "Bearish" in lbl:
                                    bear_n += 1
                                else:
                                    neutral_n += 1
                            except Exception:
                                pass
                if scores:
                    avg = sum(scores) / len(scores)
                    sentiment_str = "偏多" if avg > 0.1 else ("偏空" if avg < -0.1 else "中性")
                    lines.append(
                        f"AV情绪均分: {avg:+.3f}（{sentiment_str}）"
                        f" | 看多:{bull_n} 中性:{neutral_n} 看空:{bear_n}（共{len(scores)}条）"
                    )
        except Exception:
            pass

    return "\n".join(lines) if len(lines) > 1 else ""


def _sf(v, default: float = 0.0) -> float:
    """安全 float 转换：处理 None / 'N/A' / 非数值字符串，返回 default"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_score(v) -> str:
    try:
        return f"{float(v):.1f}"
    except Exception:
        return str(v)


def score_class(v) -> str:
    try:
        f = float(v)
        if f >= 6.5:
            return "bull"
        elif f <= 4.0:
            return "bear"
        return "neutral"
    except Exception:
        return "neutral"


def direction_badge(direction: str) -> str:
    d = str(direction).lower()
    if "bull" in d:
        return "badge-bull"
    elif "bear" in d:
        return "badge-bear"
    return "badge-neutral"


def direction_zh(direction: str) -> str:
    d = str(direction).lower()
    if "bull" in d:
        return "📈 看多"
    elif "bear" in d:
        return "📉 看空"
    return "⚖️ 中性"


# ── 数据提取 ──────────────────────────────────────────────────────────────────

def extract(data: dict) -> dict:
    """从 JSON 中提取所有关键字段，返回结构化 context dict"""
    sr = data.get("swarm_results", {})
    ad = sr.get("agent_details", {})
    ml = data.get("ml_prediction", {})
    cr = data.get("combined_recommendation", {})
    aa = data.get("advanced_analysis", {})

    ticker = data.get("ticker", "???")
    ts_raw = data.get("timestamp", str(date.today()))
    report_date = ts_raw[:10]

    # 蜂群总体
    final_score = sr.get("final_score", 0)
    direction = sr.get("direction", "neutral")
    resonance = sr.get("resonance", {})

    # v0.15.3: Probability Boost 第 6 维融合（从 generate_ml_report.py 移植）
    # 当 swarm final_score 未应用 boost 时（直接读 swarm JSON 而非 ML enhanced），
    # 在此处补算，确保深度报告与 ML 报告评分一致。
    _existing_pb = sr.get("probability_boost") or {}
    if not _existing_pb:
        _prob = aa.get("probability_analysis") or {}
        _win = float(_prob.get("win_probability_pct", 0) or 0)
        _rr  = float(_prob.get("risk_reward_ratio", 0) or 0)
        _old_score = float(final_score or 0)
        _old_dir   = direction
        _bear_str  = float((ad.get("BearBeeContrarian", {}).get("details", {}) or {}).get("bear_score", 0) or 0)
        if _win >= 60.0 and _rr >= 5.0 and _old_dir != "bearish":
            _base_boost = min(2.5, max(0.0, (_win - 50.0) / 10.0))
            _rr_mult = min(1.5, _rr / 5.0)
            _boost = _base_boost * _rr_mult
            _bear_hedge = 0.0
            if _bear_str >= 6.0:
                _bear_hedge = min(_boost * 0.6, (_bear_str - 6.0) * 0.2)
                _boost = max(0.0, _boost - _bear_hedge)
            _new_score = round(max(1.0, min(9.0, _old_score + _boost)), 2)
            _new_dir = "bullish" if (_old_dir == "neutral" and _new_score >= 5.8) else _old_dir
            final_score = _new_score
            direction = _new_dir
            sr["final_score"] = _new_score
            sr["direction"] = _new_dir
            sr["probability_boost"] = {
                "applied": True,
                "old_score": _old_score,
                "new_score": _new_score,
                "boost": round(_boost, 2),
                "win_probability_pct": _win,
                "risk_reward_ratio": _rr,
                "bear_strength": _bear_str,
                "bear_hedge": round(_bear_hedge, 2),
                "old_direction": _old_dir,
                "new_direction": _new_dir,
            }

    # 置信区间 & 维度分散度
    _cc = sr.get("confidence_calibration", {})
    confidence_band   = _cc.get("confidence_band", None)   # [lo, hi]
    band_width        = _cc.get("band_width", None)
    discrimination    = _cc.get("discrimination", None)    # "low"/"medium"/"high"
    dimension_std     = _cc.get("dimension_std", None)

    # 各 Agent
    scout   = ad.get("ScoutBeeNova", {})
    rival   = ad.get("RivalBeeVanguard", {})
    buzz    = ad.get("BuzzBeeWhisper", {})
    chronos = ad.get("ChronosBeeHorizon", {})
    oracle  = ad.get("OracleBeeEcho", {})
    guard   = ad.get("GuardBeeSentinel", {})
    bear    = ad.get("BearBeeContrarian", {})

    # Oracle 详细数据（蜂群快照，可能降级）
    _odet_raw = oracle.get("details", {}) or {}
    # v0.15.1 FIX: 期权字段优先从 advanced_analysis.options_analysis 读，
    # OracleBee 的 details 仅作 fallback。根因：两条路径独立调用 OptionsAgent.analyze()，
    # yfinance 返回不同快照，OracleBee 常拿到降级数据（pc_ratio/GEX None）。
    _oa_opts = aa.get("options_analysis") or {}
    # 构造合并视图：advanced 为主，OracleBee 填补 advanced 没有的字段
    odet = {**_odet_raw, **{k: v for k, v in _oa_opts.items() if v is not None}}
    key_levels = odet.get("key_levels", {}) or _odet_raw.get("key_levels", {})
    unusual = odet.get("unusual_activity", []) or _odet_raw.get("unusual_activity", [])

    # Chronos 催化剂
    cdet = chronos.get("details", {})
    catalysts = cdet.get("catalysts", [])
    # 去重催化剂
    seen_events = set()
    unique_catalysts = []
    for c in catalysts:
        key = (c.get("event", ""), c.get("date", ""))
        if key not in seen_events:
            seen_events.add(key)
            unique_catalysts.append(c)
    catalysts = unique_catalysts[:6]

    analyst_target = cdet.get("analyst_target", None)
    if analyst_target is None:
        for c in cdet.get("catalysts", []):
            if c.get("event", "").startswith("分析师"):
                pass

    # IV Crush（财报历史波动 + 隐含幅度）
    iv_crush = cdet.get("iv_crush", {})
    iv_crush_summary = cdet.get("iv_crush_summary", "")

    # ML 预测
    ml_pred = ml.get("prediction", {})
    ml_7d = ml_pred.get("expected_7d", 0)
    ml_30d = ml_pred.get("expected_30d", 0)
    ml_prob = ml.get("input", {})

    # 宏观指标（BuzzBee）
    bdet = buzz.get("details", {})
    reddit = bdet.get("reddit", {})
    fg_score = None
    # 从 discovery 中提取 F&G
    buzz_disc = buzz.get("discovery", "")
    fg_match = re.search(r'F&G[^\d]*(\d+)', buzz_disc)
    if fg_match:
        fg_score = int(fg_match.group(1))

    # IV Skew + OTM IV + IV Rank
    iv_skew = odet.get("iv_skew_ratio", None)
    iv_skew_signal = odet.get("iv_skew_signal", "neutral")
    iv_skew_detail = odet.get("iv_skew_detail", {})
    otm_put_iv  = iv_skew_detail.get("otm_put_iv", None)   # e.g. 33.33
    otm_call_iv = iv_skew_detail.get("otm_call_iv", None)  # e.g. 26.67
    iv_percentile = odet.get("iv_percentile", None)        # 0–100 百分位

    # IV 期限结构（S15 · OracleBee → OptionsAgent）
    iv_term_structure = odet.get("iv_term_structure", {}) or {}

    # 综合建议
    combined_prob = cr.get("combined_probability", final_score * 10)
    rating = cr.get("rating", "HOLD")
    action = cr.get("action", "观察等待")

    # 价格：优先使用顶层 closing_price（收盘后更新），其次用 ScoutBee 扫描时价格
    scout_det = scout.get("details", {})
    price = data.get("closing_price") or scout_det.get("price", None)
    price_is_close = bool(data.get("closing_price"))

    # 历史准确率（从 bear_discovery 字符串解析）
    bear_disc = bear.get("discovery", "") or ""
    hist_acc_m   = re.search(r'T\+7准确率(\d+)%', bear_disc)
    hist_n_m     = re.search(r'\((\d+)次', bear_disc)
    hist_accuracy = int(hist_acc_m.group(1)) if hist_acc_m else None
    hist_sample_n = int(hist_n_m.group(1)) if hist_n_m else None

    # ML 历史样本量（用于胜率置信度标注）
    aa_hist_sample_n = (aa.get("historical_analysis", {})
                          .get("expected_returns", {})
                          .get("sample_size", None))

    # GEX（Gamma Exposure）：优先读 advanced_analysis.dealer_gex，其次 OracleBee
    _dgex = aa.get("dealer_gex", {}) or {}
    if _dgex and float(_dgex.get("total_gex", 0) or 0) != 0:
        gamma_exposure     = _dgex.get("total_gex", 0)
        _gex_regime        = _dgex.get("regime", "")
        _gex_flip          = _dgex.get("gex_flip")
        _gex_call_wall     = _dgex.get("largest_call_wall")
        _gex_put_wall      = _dgex.get("largest_put_wall")
        gamma_squeeze_risk = ("high"   if _gex_regime == "negative_gex" else
                              "low"    if _gex_regime == "positive_gex" else "medium")
    else:
        gamma_exposure     = odet.get("gamma_exposure", 0)
        gamma_squeeze_risk = odet.get("gamma_squeeze_risk", "")
        _gex_regime        = ""
        _gex_flip          = None
        _gex_call_wall     = None
        _gex_put_wall      = None

    # 逆向蜂信号
    bear_signals = bear.get("details", {}).get("bearish_signals", [])

    # Congress trades（国会交易 - ScoutBee details）
    congress = scout_det.get("congress", {})
    # VIX 期限结构（GuardBee details）
    gdet = guard.get("details", {})
    vix_term = gdet.get("vix_term_structure", {})

    return {
        "ticker": ticker,
        "report_date": report_date,
        "price": price,
        "price_is_close": price_is_close,
        "final_score": final_score,
        "direction": direction,
        "direction_zh": direction_zh(direction),
        "resonance": resonance,
        "combined_prob": combined_prob,
        "rating": rating,
        "action": action,
        # Agents
        "scout": scout,
        "rival": rival,
        "buzz": buzz,
        "chronos": chronos,
        "oracle": oracle,
        "guard": guard,
        "bear": bear,
        # Oracle data
        "put_call_ratio": odet.get("put_call_ratio", "N/A"),
        "total_oi": odet.get("total_oi", 0),
        "iv_skew": iv_skew,
        "iv_skew_signal": iv_skew_signal,
        "otm_put_iv": otm_put_iv,
        "otm_call_iv": otm_call_iv,
        "iv_current": odet.get("iv_current", 0),
        "iv_rank": odet.get("iv_rank", 0),
        "iv_percentile": iv_percentile,
        # IV 期限结构（S15）
        "iv_term_structure": iv_term_structure,
        "options_score": odet.get("options_score", 0),
        "flow_direction": odet.get("flow_direction", "neutral"),
        "signal_summary": odet.get("signal_summary", ""),
        "unusual_activity": unusual,
        "expiration_dates": odet.get("expiration_dates", []),
        "key_levels": key_levels,
        # Catalysts
        "catalysts": catalysts,
        # IV Crush（ChronosBee 财报波动历史）
        "iv_crush": iv_crush,
        "iv_crush_summary": iv_crush_summary,
        # Congress trades（ScoutBee 国会交易）
        "congress": congress,
        # VIX term structure（GuardBee）
        "vix_term_structure": vix_term,
        # ML
        "ml_7d": ml_7d,
        "ml_30d": ml_30d,
        # Macro
        "fg_score": fg_score,
        "reddit": reddit,
        # Bear signals
        "bear_signals": bear_signals,
        # Historical accuracy
        "hist_accuracy": hist_accuracy,
        "hist_sample_n": hist_sample_n,
        "aa_hist_sample_n": aa_hist_sample_n,
        # GEX
        "gamma_exposure": gamma_exposure,
        "gamma_squeeze_risk": gamma_squeeze_risk,
        "gex_regime": _gex_regime,
        "gex_flip": _gex_flip,
        "gex_call_wall": _gex_call_wall,
        "gex_put_wall": _gex_put_wall,
        "flip_acceleration": _dgex.get("flip_acceleration", {}),
        "vanna_stress": _dgex.get("vanna_stress", {}),
        "gex_normalized_pct": _dgex.get("gex_normalized_pct", None),
        # 升级 #1/#4: GEX 政体联动 + 政体权重
        "gex_regime_mod": sr.get("gex_regime_mod", {}),
        "regime_weights_description": sr.get("regime_weights_description", ""),
        "dimension_weights_effective": sr.get("dimension_weights", {}),
        "dimension_weights_base": sr.get("dimension_weights_base", {}),
        # 置信区间 & 维度分散度
        "confidence_band": confidence_band,
        "band_width": band_width,
        "discrimination": discrimination,
        "dimension_std": dimension_std,
        # 维度分解（用于 _build_swarm_narrative 最强/最弱维度分析）
        "dimension_scores": sr.get("dimension_scores", {}),
        # Overview
        "overview": aa.get("overview", ""),
        # ① IV-RV Spread（来自 options_analyzer → OptionsAgent）
        "iv_rv_spread": odet.get("iv_rv_spread", 0.0),
        "iv_rv_signal": odet.get("iv_rv_signal", ""),
        "rv_30d": odet.get("rv_30d", 0.0),
        # ⑤ Gamma 到期日历
        "gamma_calendar": odet.get("gamma_calendar", {}),
        # ② PEAD（来自 chronos_bee details）
        "pead_summary": (chronos.get("details") or {}).get("pead_summary", ""),
        "pead_bias": (chronos.get("details") or {}).get("pead_bias", "neutral"),
        # ③ 时间周期（来自 guard_bee details）
        "cycle_context": (guard.get("details") or {}).get("cycle_context", {}),
        # ④ 市场政体（来自 guard_bee details）
        "market_regime": (guard.get("details") or {}).get("market_regime", {}),
        # ⑦ 信号拥挤度（来自 guard_bee details）
        "signal_crowding": (guard.get("details") or {}).get("signal_crowding", {}),
        # ⑥ 供应链信号（来自 scout_bee details）
        "supply_chain": (scout.get("details") or {}).get("supply_chain", {}),
        # P1: 仓位管理
        "position_management": aa.get("position_management", {}),
        # P2: 历史回测 Analog
        "historical_analogs": (aa.get("historical_analysis", {}) or {}).get("similar_opportunities", []),
        "expected_returns": (aa.get("historical_analysis", {}) or {}).get("expected_returns", {}),
        # P3: Max Pain
        "max_pain": odet.get("max_pain", None),
        # P4: 情绪动量
        "sentiment_pct": bdet.get("sentiment_pct", None),
        "sentiment_momentum": bdet.get("sentiment_momentum", None),
        "sentiment_divergence": bdet.get("sentiment_divergence", None),
        "volume_ratio": bdet.get("volume_ratio", None),
        # P5: 内部人 + 做空
        "insider_trades": (scout.get("details") or {}).get("insider", {}),
        "short_interest": (bear.get("details") or {}).get("short_interest", None),
        # P6: 行业竞争
        "industry_comparison": aa.get("industry_comparison", {}),
        # P7: ML 特征透明
        "ml_3d": ml_pred.get("expected_3d", 0),
        "ml_input": ml.get("input", {}),
        "ml_recommendation": ml.get("recommendation", ""),
        "ml_probability": ml_pred.get("probability", 0),
        # P8: Deep Skew
        "deep_skew": odet.get("deep_skew", {}),
        # V1: 估值数据（来自 RivalBee eps_revision）
        "forward_eps":       float(((rival.get("details") or {}).get("eps_revision", {}) or {}).get("forward_eps", 0) or 0),
        "trailing_eps":      float(((rival.get("details") or {}).get("eps_revision", {}) or {}).get("trailing_eps", 0) or 0),
        "eps_growth":        float(((rival.get("details") or {}).get("eps_revision", {}) or {}).get("eps_growth_proj", 0) or 0),
        "analyst_target":    float(((rival.get("details") or {}).get("eps_revision", {}) or {}).get("target_mean_price", 0) or 0),
        "analyst_consensus": float(((rival.get("details") or {}).get("eps_revision", {}) or {}).get("recommendation_mean", 0) or 0),
        "analyst_count":     int(((rival.get("details") or {}).get("eps_revision", {}) or {}).get("num_analyst_opinions", 0) or 0),
        # v0.15.0: Probability Boost 审计字段（来自 generate_ml_report.py 注入）
        "probability_boost": (data.get("swarm_results") or {}).get("probability_boost", {}),
        "win_probability_pct": float(((data.get("advanced_analysis") or {}).get("probability_analysis") or {}).get("win_probability_pct", 0) or 0),
        "risk_reward_ratio":   float(((data.get("advanced_analysis") or {}).get("probability_analysis") or {}).get("risk_reward_ratio", 0) or 0),
        # Raw JSON for LLM context
        "_raw": data,
    }


# ── Claude API 深度推理 ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 Alpha Hive 首席期权策略师，拥有十年衍生品交易经验，专门解读蜂群量化系统的多维信号并生成可操作的深度分析。

━━ 期权数据解读标准 ━━
P/C比阈值：<0.7 强多头情绪 | 0.7-0.9 偏多 | 0.9-1.1 中性 | 1.1-1.3 偏空 | >1.3 强空头对冲
IV Skew（Put/Call隐波比）：>1.15 市场为下行大幅对冲（恐慌溢价）| 0.95-1.05 平衡 | <0.90 上行投机热
IV Rank：>80 期权昂贵适合卖方策略 | <20 期权便宜适合方向性买入 | 50左右中性
OI集中度：单行权价OI超过总OI的10% → 形成Gamma墙，到期前做市商Delta对冲产生磁吸或压制
异常流解读：Call Sweep主动买入=机构方向性押注看多 | Put Sweep主动买入=对冲需求或做空 | 卖Call=限制上行空间

━━ 信号优先级（从高到低）━━
① 期权异常流（机构smart money，最难伪造）
② ML量化预测（RivalBee，基于历史模型）
③ 蜂群共振（多维度信号同向，置信度加成）
④ 技术面/价格关键位（支撑阻力的OI验证）
⑤ 基本面/催化剂/宏观情绪（辅助背景）

━━ 信号冲突处理规则 ━━
当期权流看多但综合评分偏空：优先相信期权流（smart money通常领先），在叙事中明确点出此矛盾并解释
当ML看涨但期权Skew偏高（>1.1）：期权市场在为下行对冲，谨慎对待ML信号，需说明对冲需求来源
当共振信号存在但评分中性：共振提升置信度，但需核实共振维度与期权流方向是否一致
任何矛盾都必须在分析中明确表态，不得含糊绕过

━━ 蜂群评分解读 ━━
≥7.5 强看多（可考虑进攻性多头仓位）| 6.5-7.4 温和看多 | 4.6-6.4 中性等待
3.6-4.5 温和看空 | ≤3.5 强看空（警惕下行风险）

━━ 写作要求 ━━
- 每章必须在第一句点出本章最关键的1个信号及其含义
- 所有判断必须引用具体数字（不说"较高"，要说"P/C=0.69低于中性阈值0.9"）
- 结论必须可操作：明确看多/看空/等待，并给出触发改变判断的条件
- 字数：每段100-180字，直接输出HTML <p>段落，使用<strong>/<span class="bull-text">/<span class="bear-text">/<span class="highlight">标注关键词
- 语气：专业分析师口吻，不说废话，不重复数据，每句都要推进论点"""

# Step 1：分析框架专用系统提示（不写HTML，只做结构化推理）
STEP1_SYSTEM = """你是量化信号分析引擎。你的唯一任务是在写报告前完成结构化预分析。
输出格式严格如下（纯文字，禁止输出HTML）：
核心论点: [1句话，本章最重要的发现]
关键证据: [最强的2-3个具体数字或信号]
主要矛盾: [信号之间的冲突，或写"无明显矛盾"]
权衡结论: [如何处理矛盾，给出明确方向判断]
置信度: [1-5，5为最高]"""


def llm_reason(ctx: dict, section: str, api_key: str) -> str:
    """调用 Claude API 对指定章节进行深度推理，返回 HTML 段落文本"""
    try:
        import anthropic
    except ImportError:
        print("  ⚠️  anthropic 库未安装，使用本地文本")
        return _local_fallback(ctx, section)

    ticker = ctx["ticker"]
    score = ctx["final_score"]
    direction = ctx["direction_zh"]
    res = ctx["resonance"]
    res_dims = res.get("resonant_dimensions", [])
    try:
        pc_float = float(ctx['put_call_ratio'])
    except (ValueError, TypeError):
        pc_float = 1.0

    # 链式推理：delta 上下文供各章引用
    _delta = ctx.get("delta_context", "")
    _delta_block = f"\n\n{_delta}" if _delta else ""

    _master = ctx.get("master_thesis", "")
    _master_block = f"\n\n【蜂群整体论点（来自CH1）】{_master}" if _master else ""

    _conflicts = detect_conflicts(ctx)
    _conflict_block = (
        "\n\n【系统检测到以下信号矛盾——必须在分析中明确指出并给出权衡判断，不得绕过】\n"
        + "\n".join(_conflicts)
    ) if _conflicts else ""

    _live_news = ctx.get("live_news_block", "")
    _live_news_block = f"\n\n{_live_news}" if _live_news else ""

    _ff6 = ctx.get("ff6_block", "")
    _ff6_block = f"\n\n{_ff6}" if _ff6 else ""

    _cross = ctx.get("cross_context", "")
    _cross_context_block = f"\n\n【跨章节锚点（请在本章行文中主动呼应相关条目）】\n{_cross}" if _cross else ""

    _prev_chs = ctx.get("prev_chapters", [])
    _prev_block = (
        "\n\n【前序章节核心结论（本章叙事应在此基础上递进深化，不重复，应明确呼应或提出新层次）】\n"
        + "\n".join(_prev_chs)
    ) if _prev_chs else ""

    # ── Step 1：分析框架提示（每章专属，纯分析不写HTML）─────────────────────────
    step1_prompts = {
        "swarm_analysis": f"""分析 {ticker} 蜂群七维信号结构：
综合评分{score}/10 | Scout {fmt_score(ctx['scout'].get('score'))} | Rival {fmt_score(ctx['rival'].get('score'))} ML7d{ctx['ml_7d']:+.1f}%
Buzz {fmt_score(ctx['buzz'].get('score'))} | Chronos {fmt_score(ctx['chronos'].get('score'))} | Oracle {fmt_score(ctx['oracle'].get('score'))} P/C={ctx['put_call_ratio']}
Guard {fmt_score(ctx['guard'].get('score'))} | Bear {fmt_score(ctx['bear'].get('score'))} 信号:{', '.join(ctx['bear_signals'][:2])}{_delta_block}{_conflict_block}{_live_news_block}{_ff6_block}
完成结构化预分析（严格按格式，无HTML）：""",

        "resonance": f"""分析 {ticker} 蜂群共振信号质量：
共振检测:{res.get('resonance_detected')} | 方向:{res.get('direction','N/A')} | 维度:{', '.join(res_dims) if res_dims else '无'}
支持Agent:{res.get('supporting_agents',0)}/7 | 置信度提升:+{res.get('confidence_boost',0)}%
Scout:{fmt_score(ctx['scout'].get('score'))} Rival:{fmt_score(ctx['rival'].get('score'))} Buzz:{fmt_score(ctx['buzz'].get('score'))} Chronos:{fmt_score(ctx['chronos'].get('score'))} Oracle:{fmt_score(ctx['oracle'].get('score'))} Guard:{fmt_score(ctx['guard'].get('score'))} Bear:{fmt_score(ctx['bear'].get('score'))}
期权流:{ctx['flow_direction']} | ML 7d:{ctx['ml_7d']:+.1f}% | 30d:{ctx['ml_30d']:+.1f}%
完成结构化预分析（严格按格式，无HTML）：""",

        "catalyst": f"""分析 {ticker} 催化剂时间线（{len(ctx['catalysts'])}个）：
{json.dumps(ctx['catalysts'][:4], ensure_ascii=False)}
完成结构化预分析（严格按格式，无HTML）：""",

        "options": f"""分析 {ticker} 期权市场结构：
P/C={ctx['put_call_ratio']} | 总OI={ctx['total_oi']:,.0f} | IV Skew={ctx['iv_skew']}({ctx['iv_skew_signal']}) | IV={ctx['iv_current']:.1f}%
IV-RV价差:{ctx.get('iv_rv_spread',0):+.1f}%(期权{'昂贵' if ctx.get('iv_rv_spread',0)>3 else '便宜' if ctx.get('iv_rv_spread',0)<-3 else '合理'},{ctx.get('iv_rv_signal','')}) | HV30={ctx.get('rv_30d',0):.1f}%
流向:{ctx['flow_direction']} | 关键阻力:{json.dumps(ctx['key_levels'].get('resistance',[])[:2],ensure_ascii=False)}
关键支撑:{json.dumps(ctx['key_levels'].get('support',[])[:2],ensure_ascii=False)}
Gamma日历钉子:{ctx.get('gamma_calendar',{}).get('pin_strike','N/A')} | Charm方向:{ctx.get('gamma_calendar',{}).get('charm_direction','N/A')}
异常流:{json.dumps(ctx['unusual_activity'][:3],ensure_ascii=False)}
完成结构化预分析（严格按格式，无HTML）：""",

        "macro": f"""分析 {ticker} 宏观与情绪环境：
F&G指数:{ctx['fg_score']} | Guard:{fmt_score(ctx['guard'].get('score'))}({ctx['guard'].get('direction','neutral')})
宏观发现:{ctx['guard'].get('discovery','')[:150]}
Buzz情绪%:{ctx['buzz'].get('details',{}).get('sentiment_pct','N/A')} | Reddit:{ctx['reddit'].get('rank','N/A')}名{_delta_block}{_live_news_block}
完成结构化预分析（严格按格式，无HTML）：""",

        "scenario": f"""分析 {ticker} 情景推演基础：
价格:{'$'+str(ctx['price']) if ctx['price'] else '市价'} | 综合评分:{score}/10 | ML 7d:{ctx['ml_7d']:+.1f}% 30d:{ctx['ml_30d']:+.1f}%
最大阻力:${ctx['key_levels'].get('resistance',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('resistance') else 'N/A'}
最大支撑:${ctx['key_levels'].get('support',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('support') else 'N/A'}
风险信号:{', '.join(ctx['bear_signals'][:2])}{_master_block}
完成结构化预分析（严格按格式，无HTML）：""",

        "risk": f"""分析 {ticker} 风险信号优先级：
逆向信号:{', '.join(ctx['bear_signals'])} | Bear评分:{fmt_score(ctx['bear'].get('score'))}
F&G:{ctx['fg_score']} | IV Skew:{ctx['iv_skew']} | 宏观:{ctx['guard'].get('discovery','')[:120]}{_master_block}{_ff6_block}
完成结构化预分析（严格按格式，无HTML）：""",
    }

    prompts = {
        "swarm_analysis": f"""
分析 {ticker} 的蜂群七维评分结构：
- 综合评分 {score}/10，方向 {direction}
- Scout(基本面) {fmt_score(ctx['scout'].get('score'))}, 发现: {ctx['scout'].get('discovery','')[:100]}
- Rival(ML辅助) {fmt_score(ctx['rival'].get('score'))}, 7日预期 {ctx['ml_7d']:+.1f}%
- Buzz(情绪) {fmt_score(ctx['buzz'].get('score'))}, 发现: {ctx['buzz'].get('discovery','')[:100]}
- Chronos(催化剂) {fmt_score(ctx['chronos'].get('score'))}
- Oracle(期权) {fmt_score(ctx['oracle'].get('score'))}, P/C={ctx['put_call_ratio']}, OI={ctx['total_oi']:,.0f}
- Guard(宏观) {fmt_score(ctx['guard'].get('score'))}, 发现: {ctx['guard'].get('discovery','')[:100]}
- Bear(逆向) {fmt_score(ctx['bear'].get('score'))}, 信号: {', '.join(ctx['bear_signals'][:2])}

只输出两段连贯叙事 HTML <p> 标签，**严禁 table/tr/td/th/ol/ul/li 标签**，数据必须嵌入散文叙述，不得单独列表。关键词使用 <strong>、<span class="bull-text">、<span class="bear-text">、<span class="highlight"> 标注。{_delta_block}{_conflict_block}{_live_news_block}
若有昨日对比数据，请在第二段末尾用一句话点出最显著的评分变化趋势。
若有实时新闻数据，请在分析中引用1-2条最相关的头条作为信号佐证。""",

        "resonance": f"""
分析 {ticker} 的蜂群共振机制与信号质量：

【共振状态】
- 共振检测: {res.get('resonance_detected', False)} | 方向: {res.get('direction', 'N/A')} | 置信度提升: +{res.get('confidence_boost', 0)}%
- 共振维度: {', '.join(res_dims) if res_dims else '无触发维度'} | 支持Agent: {res.get('supporting_agents', 0)}/7

【七蜂评分参照（用于判断哪些蜂构成共振）】
- Scout(基本面) {fmt_score(ctx['scout'].get('score'))} | Rival(ML) {fmt_score(ctx['rival'].get('score'))} | Buzz(情绪) {fmt_score(ctx['buzz'].get('score'))}
- Chronos(催化剂) {fmt_score(ctx['chronos'].get('score'))} | Oracle(期权) {fmt_score(ctx['oracle'].get('score'))} | Guard(宏观) {fmt_score(ctx['guard'].get('score'))} | Bear(逆向) {fmt_score(ctx['bear'].get('score'))}

- ML 7日预期: {ctx['ml_7d']:+.1f}% | 30日: {ctx['ml_30d']:+.1f}% | 期权流: {ctx['flow_direction']}
- 综合评分: {score}/10，方向: {direction}
{_master_block}{_conflict_block}{_delta_block}

只输出两段连贯叙事 HTML <p> 标签，**严禁 table/tr/td/th/ol/ul/li 标签**，所有数据和维度名称嵌入散文，不得制表。关键词使用 <strong>、<span class="bull-text">、<span class="bear-text">、<span class="highlight"> 标注。
第一段：解释共振维度为何触发——这些维度背后的数据说明了什么市场逻辑？共振是否具有质量（支持Agent≥4为强共振）还是偏脆弱（≤2个Agent支持）？哪些蜂的评分与共振方向最一致？哪些蜂构成了潜在的反向张力？
第二段：共振与整体论点的关系——共振方向是否强化或质疑了蜂群整体论点？共振的"失效条件"是什么——哪个关键维度一旦反转会打破当前共振结构？结合昨日对比（如有）说明共振强度变化趋势。
若检测到信号矛盾，必须在分析中明确指出共振方向与矛盾信号之间的张力及权衡判断。{_prev_block}{_cross_context_block}""",

        "catalyst": f"""
分析 {ticker} 的催化剂时间线：
催化剂列表（最多6个）:
{json.dumps(ctx['catalysts'][:6], ensure_ascii=False, indent=2)}
PEAD历史财报漂移: {ctx.get('pead_summary','暂无历史数据')}（偏向:{ctx.get('pead_bias','neutral')}）

只输出两段连贯叙事 HTML <p> 标签，**严禁 table/tr/td/th/ol/ul/li 标签**，催化剂数据和时间线全部以散文形式写入，不得制表或列举。关键词使用 <strong>、<span class="bull-text">/<span class="bear-text">/<span class="highlight"> 标注。分析催化剂的密度、质量和对股价的潜在影响，特别分析最近3个催化剂的联动效应，并引用PEAD历史漂移数据说明财报后价格动能的统计规律。{_prev_block}{_cross_context_block}""",

        "options": f"""
深度分析 {ticker} 的期权市场结构：
- P/C 比: {ctx['put_call_ratio']}（{'>1 偏空' if pc_float > 1 else '<1 偏多'}）
- 总OI: {ctx['total_oi']:,.0f}
- IV Skew: {ctx['iv_skew']} ({ctx['iv_skew_signal']})
- IV 当前: {ctx['iv_current']:.1f}% | HV30（已实现波动率）: {ctx.get('rv_30d',0):.1f}%
- IV-RV 价差: {ctx.get('iv_rv_spread',0):+.1f}%（正值=期权相对HV偏贵，卖方有优势；负值=期权便宜，方向性买入占优），信号: {ctx.get('iv_rv_signal','')}
- 流向: {ctx['flow_direction']}
- 关键支撑: {json.dumps(ctx['key_levels'].get('support',[])[:3], ensure_ascii=False)}
- 关键阻力: {json.dumps(ctx['key_levels'].get('resistance',[])[:3], ensure_ascii=False)}
- Gamma 到期日历 — Pin Risk钉子位: {ctx.get('gamma_calendar',{}).get('pin_strike','N/A')} | Charm 到期衰减方向: {ctx.get('gamma_calendar',{}).get('charm_direction','N/A')} | 最高OI到期日: {ctx.get('gamma_calendar',{}).get('pin_expiry','N/A')}
- 异常活动（前5）: {json.dumps(ctx['unusual_activity'][:5], ensure_ascii=False)}
- 系统信号: {ctx['signal_summary']}{_conflict_block}

只输出三段连贯叙事 HTML <p> 标签：1)P/C与OI含义及异常流解读（含IV-RV价差对策略选择的影响）, 2)关键位分析与Gamma机制（含Pin Risk和到期日历），3)IV Skew与期限结构解读（含已实现波动率对比）。**严禁 table/tr/td/th/ol/ul/li 标签**，所有数据指标和异常流信息嵌入散文叙述，不得制表。关键词使用 <strong>、<span class="bull-text">/<span class="bear-text">/<span class="highlight"> 标注。{_prev_block}{_cross_context_block}""",

        "macro": f"""
分析 {ticker} 当前宏观与情绪环境：
- Fear & Greed 指数: {ctx['fg_score'] if ctx['fg_score'] else '未知'}
- GuardBee 评分: {fmt_score(ctx['guard'].get('score'))} ({ctx['guard'].get('direction','neutral')})
- 宏观发现: {ctx['guard'].get('discovery','')[:200]}
- 市场政体（Regime）: {ctx.get('market_regime',{}).get('overall_regime','未知')} | 宏观层: {ctx.get('market_regime',{}).get('macro_regime','N/A')} | 板块层: {ctx.get('market_regime',{}).get('sector_regime','N/A')} | 个股层: {ctx.get('market_regime',{}).get('stock_regime','N/A')}
- 时间周期: {ctx.get('cycle_context',{}).get('cycle_regime','normal')} | Opex周: {ctx.get('cycle_context',{}).get('is_opex_week',False)} | 财报后窗口: {ctx.get('cycle_context',{}).get('is_post_earnings_window',False)}
- 供应链信号（TSMC/AMAT/ASML vs {ticker} 5日相对强弱）: {ctx.get('supply_chain',{}).get('supply_chain_note','暂无')}
- Buzz情绪%: {ctx['buzz'].get('details',{}).get('sentiment_pct','N/A')}
- Reddit: {ctx['reddit'].get('rank','N/A')}名, {ctx['reddit'].get('mentions','N/A')}次提及

只输出两段连贯叙事 HTML <p> 标签，**严禁 table/tr/td/th/ol/ul/li 标签**，宏观数据指标嵌入散文，不得制表或列举。关键词使用 <strong>、<span class="bull-text">/<span class="bear-text">/<span class="highlight"> 标注。分析宏观逆风/顺风对该股的影响，结合市场政体（risk_on/risk_off）和时间周期判断短期宏观压力节奏，以及F&G极值下的反向机会。{_delta_block}{_live_news_block}
若有昨日对比，请在分析中引用宏观情绪的变化方向。
若有AV情绪分，请将其与F&G指数对比，说明散户情绪与机构情绪是否一致。{_prev_block}{_cross_context_block}""",

        "scenario": f"""
为 {ticker} 补充情景推演叙事（情景卡片和止盈止损矩阵已由模板生成，**严禁输出任何 table/tr/td/th HTML 标签，严禁重复输出操作建议矩阵或情景列表**）：

【当前量化基础】
- 价格: {'$'+str(ctx['price']) if ctx['price'] else '市价'} | 评分: {score}/10 | 方向: {direction}
- ML 7日预期: {ctx['ml_7d']:+.1f}% | 30日: {ctx['ml_30d']:+.1f}%
- 最大阻力: ${ctx['key_levels'].get('resistance',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('resistance') else 'N/A'}
- 最大支撑: ${ctx['key_levels'].get('support',[{}])[0].get('strike','N/A') if ctx['key_levels'].get('support') else 'N/A'}
- 最近催化剂: {(ctx['catalysts'][0].get('event','无') + '（' + str(ctx['catalysts'][0].get('days_until','?')) + '天后）') if ctx['catalysts'] else '无'}
- IV当前: {ctx['iv_current']:.1f}% | F&G: {ctx['fg_score'] if ctx['fg_score'] else '未知'} | 期权流: {ctx['flow_direction']}
- 全部风险信号: {', '.join(ctx['bear_signals']) or '无'}{_master_block}{_delta_block}

只输出两段 HTML <p> 标签，不得包含任何 table/ol/ul/li 标签：
第一段：情景概率分布的内在逻辑——基准/乐观/悲观情景各自被哪些具体数据驱动？ML预期与期权市场隐含方向是否形成共鸣或分歧？催化剂时间窗口如何影响短期3-5天的概率分布（区分短期与7日/30日预期的差异）？
第二段：交易执行框架——分批建仓的具体触发条件（价格/信号触发点）、仓位管理逻辑、以及**论点失效信号**（必须给出具体数值阈值：价格/IV/P-C等，触及时应立即离场的判断依据）。
用 <strong> 强调关键数字和价位，用 <span class="bull-text">/<span class="bear-text"> 标注正负预期。
若有昨日对比，请说明预期方向较昨日是否发生了实质性变化。{_prev_block}{_cross_context_block}""",

        "risk": f"""
为 {ticker} 生成 3 个深度推理风险卡片。规则引擎已渲染基础风险列表，你只需生成推理层卡片（因果链+放大效应+失效阈值）。

数据：
- 逆向信号: {', '.join(ctx['bear_signals'])}
- Bear评分: {fmt_score(ctx['bear'].get('score'))} ({ctx['bear'].get('direction')})
- F&G: {ctx['fg_score'] if ctx['fg_score'] else '未知'} | IV Skew: {ctx['iv_skew']}
- 催化剂: {len(ctx['catalysts'])} 个，最近: {ctx['catalysts'][0].get('event','无') if ctx['catalysts'] else '无'}
- 宏观: {ctx['guard'].get('discovery','')[:150]}{_master_block}{_prev_block}{_cross_context_block}

⚠️ 输出格式极严：只允许输出以下3张卡片 HTML，禁止任何 p/ol/ul/li/table/div（卡片模板本身除外）、禁止任何解释性文字。

<div class="risk-item risk-high"><div class="risk-badge">HIGH</div><div><div class="risk-title">🔴 [风险标题+核心数据]</div><div class="risk-note">传导链：[信号如何演变为亏损] → [与其他风险的共振放大] → 失效阈值：<strong>[价格/IV/P-C具体数值]</strong>触及立即离场。</div></div></div>
<div class="risk-item risk-med"><div class="risk-badge">MED</div><div><div class="risk-title">🟡 [风险标题+核心数据]</div><div class="risk-note">传导链：[信号如何演变为亏损] → [量化依据] → 警戒线：<strong>[具体数值]</strong>。</div></div></div>
<div class="risk-item risk-low"><div class="risk-badge">LOW</div><div><div class="risk-title">⚪ [风险标题+核心数据]</div><div class="risk-note">传导链：[边际风险逻辑] → 监控指标：<strong>[具体数值]</strong>。</div></div></div>

级别规则：系统性/连锁触发 → risk-high+HIGH+🔴，结构性/定价偏差 → risk-med+MED+🟡，边际/注意事项 → risk-low+LOW+⚪。""",
    }

    prompt = prompts.get(section, "")
    if not prompt:
        return _local_fallback(ctx, section)

    try:
        client = anthropic.Anthropic(api_key=api_key)

        # ── Step 1：分析框架（草稿思考，不输出给用户）────────────────────────────
        step1_framework = ""
        s1_prompt = step1_prompts.get(section, "")
        if s1_prompt:
            try:
                s1_msg = client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=200,
                    system=STEP1_SYSTEM,
                    messages=[{"role": "user", "content": s1_prompt}]
                )
                step1_framework = s1_msg.content[0].text.strip()
                _s1_preview = step1_framework[:60].replace(chr(10), ' ')
                _s1_suffix  = '...' if len(step1_framework) > 60 else ''
                print(f"    📋 Step1框架 ({section}): {_s1_preview}{_s1_suffix}")
            except Exception as e1:
                print(f"    ⚠️  Step1失败({section})，降级为单步: {e1}")

        # ── Step 2：基于分析框架写叙事 HTML ─────────────────────────────────────
        if step1_framework:
            enriched_prompt = (
                f"【第一步分析框架】\n{step1_framework}\n\n"
                f"【第二步任务】基于以上分析框架，{prompt.lstrip()}"
            )
        else:
            enriched_prompt = prompt  # 降级：直接单步

        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": enriched_prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠️  LLM 调用失败 ({section}): {e}")
        return _local_fallback(ctx, section)


def llm_cross_context(ctx: dict, api_key: str) -> str:
    """Phase 1.5: 生成跨章节锚点摘要，供后续各章节引用。
    约 150-200 字的结构化纯文本，不含 HTML。失败时返回空字符串（各章节静默降级）。
    """
    try:
        import anthropic as _ant
    except ImportError:
        return ""

    ticker  = ctx["ticker"]
    score   = ctx["final_score"]
    master  = ctx.get("master_thesis", "")
    fmt     = lambda v: f"{float(v):.1f}" if v not in (None, "") else "N/A"

    # GEX 锚点
    gex     = ctx.get("_raw_data", {}).get("advanced_analysis", {}).get("dealer_gex", {})
    flip    = gex.get("gex_flip", "N/A")
    c_wall  = gex.get("largest_call_wall", "N/A")
    p_wall  = gex.get("largest_put_wall", "N/A")
    gex_str = f"Flip=${flip} | Call Wall=${c_wall} | Put Wall=${p_wall}" if gex else "N/A"

    # 最近催化剂
    cat0    = ctx["catalysts"][0] if ctx.get("catalysts") else {}
    cat_str = f"{cat0.get('event','无')}（{cat0.get('days_until','?')}天{'后' if (cat0.get('days_until') or 0) >= 0 else '前'}）" if cat0 else "无"

    # 七蜂简版
    def sc(key): return fmt(ctx.get(key, {}).get("score"))
    agents_str = (f"Scout={sc('scout')} Rival={sc('rival')} Buzz={sc('buzz')} "
                  f"Chronos={sc('chronos')} Oracle={sc('oracle')} Guard={sc('guard')} Bear={sc('bear')}")

    prompt = f"""为 {ticker} 生成一份供内部各分析章节引用的跨章节锚点摘要。
数据摘要：
- 蜂群评分：{score}/10，方向：{ctx['direction_zh']}，ML 7d：{ctx['ml_7d']:+.1f}%，30d：{ctx['ml_30d']:+.1f}%
- 七蜂：{agents_str}
- 期权：P/C={ctx['put_call_ratio']} | IV={ctx.get('iv_current',0):.1f}% | Skew={ctx['iv_skew']} | 流向={ctx['flow_direction']}
- GEX 结构：{gex_str}
- 最近催化剂：{cat_str}
- 逆向信号：{', '.join(ctx['bear_signals']) or '无'}
- F&G：{ctx['fg_score'] if ctx['fg_score'] else '未知'}
- 蜂群整体论点：{master[:120] if master else '待生成'}

严格按以下4行格式输出，每行以序号开头，不超过200字总计，不含HTML：
1.【信号张力】哪两个维度存在最显著矛盾，其含义是什么（1-2句）
2.【价格锚点】GEX Flip/支撑/阻力的核心含义及其对走势的约束（1句）
3.【催化剂压力】最近催化剂对7日窗口概率分布的具体影响（1句）
4.【跨章一致性】哪几章的结论应当共同指向同一方向/矛盾，需在行文中呼应（1句）"""

    try:
        client = _ant.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=300,
            system="你是量化分析协调器，输出结构化纯文本锚点，供各章节引用。严格按格式，不含HTML。",
            messages=[{"role": "user", "content": prompt}]
        )
        result = msg.content[0].text.strip()
        return result
    except Exception as e:
        print(f"  ⚠️  llm_cross_context 失败: {e}")
        return ""


def llm_scenario_data(ctx: dict, api_key: str) -> dict:
    """调用 LLM 生成结构化情景数据，返回 dict；失败返回空 dict（调用方降级到 ML 值）"""
    try:
        import anthropic as _ant, json as _json, re as _re
    except ImportError:
        return {}

    ticker  = ctx["ticker"]
    price   = float(ctx["price"]) if ctx.get("price") else 0
    direction = ctx["direction_zh"]
    score   = ctx["final_score"]
    master  = ctx.get("master_thesis", "")
    master_block = f"\n【蜂群整体论点】{master}" if master else ""
    resistances = [l for l in ctx.get("key_levels", {}).get("resistance", []) if l.get("strike")]
    supports    = [l for l in ctx.get("key_levels", {}).get("support",    []) if l.get("strike")]
    res_str  = f"${resistances[0]['strike']}" if resistances else "N/A"
    sup_str  = f"${supports[0]['strike']}"    if supports    else "N/A"
    is_bear  = "bear" in ctx.get("direction", "")

    prompt = f"""为 {ticker} 生成情景推演结构化数据（当前价 ${price}，方向 {direction}，评分 {score}/10）。
关键数据：最大阻力 {res_str}，最大支撑 {sup_str}，ML 7日预期 {ctx['ml_7d']:+.1f}%，
催化剂 {len(ctx['catalysts'])} 个，风险信号：{', '.join(ctx['bear_signals'][:3]) or '无'}，
ScoutBee {ctx['scout'].get('score',5)}/10，OracleBee {ctx['oracle'].get('score',5)}/10，BearBee {ctx['bear'].get('score',5)}/10。{master_block}

{'看空视角：止盈为下行目标，止损为上行止损。' if is_bear else '看多视角：止盈为上行目标，止损为下行止损。'}

只输出 JSON，不加任何说明：
{{
  "sc_a": {{"prob": 0.XX, "price_lo": XXX, "price_hi": XXX, "note": "触发条件(15字内)"}},
  "sc_b": {{"prob": 0.XX, "price_lo": XXX, "price_hi": XXX, "note": "触发条件(15字内)"}},
  "sc_c": {{"prob": 0.XX, "price_lo": XXX, "price_hi": XXX, "note": "触发条件(15字内)"}},
  "sc_d": {{"prob": 0.XX, "price_lo": XXX, "price_hi": XXX, "note": "触发条件(15字内)"}},
  "ev_pct": X.X,
  "win_rate": XX.X,
  "tp1": XX.XX, "tp1_pct": X.X, "tp1_action": "减仓1/3",
  "tp2": XX.XX, "tp2_pct": X.X, "tp2_action": "减仓1/3",
  "tp3": XX.XX, "tp3_pct": X.X, "tp3_action": "清仓",
  "sl_conservative": XX.XX,
  "sl_standard": XX.XX,
  "sl_aggressive": XX.XX,
  "hold_days": "5–10天"
}}
要求：四情景概率之和 = 1.0；价格目标基于关键支撑阻力位合理外推；EV 为加权期望收益率（百分比数值）。"""

    try:
        client = _ant.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=500,
            system="你是量化分析师，只输出纯 JSON，不加任何解释文字。",
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        m = _re.search(r'\{[\s\S]+\}', text)
        if m:
            data = _json.loads(m.group(0))
            # 校验概率之和
            total_prob = sum(data.get(f"sc_{k}", {}).get("prob", 0) for k in ["a","b","c","d"])
            if abs(total_prob - 1.0) > 0.05:
                print(f"  ⚠️  LLM 情景概率之和 {total_prob:.2f}，降级到 ML 值")
                return {}
            return data
    except Exception as e:
        print(f"  ⚠️  llm_scenario_data 失败: {e}")
    return {}


def _render_regime_badge(ctx: dict) -> str:
    """渲染 GEX 政体 + 权重调整徽章（CH1 底部）"""
    gex_mod = ctx.get("gex_regime_mod", {})
    regime_desc = ctx.get("regime_weights_description", "")
    gex_regime = gex_mod.get("gex_regime", "unknown")
    has_gex = gex_regime in ("positive_gex", "negative_gex")
    has_regime = regime_desc and regime_desc != "权重未调整（中性环境）"
    if not has_gex and not has_regime:
        return ""

    parts = []
    gex_adj = gex_mod.get("gex_adjustment", 0)
    flip_pct = gex_mod.get("flip_proximity_pct")

    # 只在有明确 GEX 政体时渲染 GEX 徽章
    if has_gex:
        if gex_regime == "negative_gex":
            badge_color = "#ef4444"
            badge_label = "🔴 负GEX（波动放大）"
        else:
            badge_color = "#22c55e"
            badge_label = "🟢 正GEX（波动压缩）"

        parts.append(
            f'<span style="display:inline-block;background:{badge_color}15;border:1px solid {badge_color}40;'
            f'border-radius:6px;padding:2px 8px;font-size:12px;color:{badge_color};margin-right:6px;">'
            f'{badge_label}</span>'
        )

        if flip_pct is not None and flip_pct < 10:
            parts.append(f'<span style="font-size:12px;color:#a3a3a3;">距翻转 {flip_pct:.1f}%</span> ')

        if abs(gex_adj) > 0.01:
            adj_color = "#ef4444" if gex_adj < 0 else "#22c55e"
            parts.append(
                f'<span style="font-size:12px;color:{adj_color};">评分{gex_adj:+.2f}</span> '
            )

    if regime_desc and regime_desc != "权重未调整（中性环境）":
        parts.append(
            f'<span style="display:inline-block;background:#6366f115;border:1px solid #6366f140;'
            f'border-radius:6px;padding:2px 8px;font-size:12px;color:#6366f1;">'
            f'⚖️ {regime_desc}</span>'
        )

    if not parts:
        return ""
    return f'<p style="margin-top:6px;">{"".join(parts)}</p>'


def _build_swarm_narrative(ctx: dict) -> str:
    ticker = ctx["ticker"]; score = ctx["final_score"]; direction = ctx["direction_zh"]
    sc_cls = 'bull-text' if 'bull' in ctx['direction'] else 'bear-text'
    is_bull = 'bull' in ctx['direction']
    # Confidence calibration — ctx 里是平铺字段，不是嵌套 dict
    cb = ctx.get('confidence_band', None) or [score - 1, score + 1]
    cb_lo, cb_hi = float(cb[0]), float(cb[1])
    dim_std = float(ctx.get('dimension_std', 0) or 0)
    disc_label = ctx.get('discrimination', '') or ''
    std_warn = '极高（信号严重分散，方向可信度低）' if dim_std >= 2.5 else ('中等（部分维度存在分歧）' if dim_std >= 1.5 else '低（信号高度一致）')
    # Dimension scores — 从原始 JSON 中读取（extract() 未平铺此字段）
    dim_scores = ctx.get('dimension_scores', {}) or {}
    top_dim = max(dim_scores, key=lambda k: float(dim_scores.get(k,0) or 0)) if dim_scores else ''
    bot_dim = min(dim_scores, key=lambda k: float(dim_scores.get(k,0) or 0)) if dim_scores else ''
    dim_zh = {'catalyst':'催化剂','odds':'期权/赔率','signal':'技术信号','sentiment':'市场情绪','risk_adj':'风险调整'}
    top_lbl = dim_zh.get(top_dim, top_dim); bot_lbl = dim_zh.get(bot_dim, bot_dim)
    top_val = float(dim_scores.get(top_dim, 0)); bot_val = float(dim_scores.get(bot_dim, 0))
    # Bear signals
    bear_sigs = ctx['bear_signals']
    sc_dir = '看空' if not is_bull else '看多'
    score_interp = ('强力' + sc_dir if score >= 7.5 or score <= 2.5 else
                    ('明显' + sc_dir if score >= 6.5 or score <= 3.5 else '偏' + sc_dir + '（信号偏弱）'))
    overview = ctx.get('overview', '')
    overview_para = f'<p><strong>高级综合评估：</strong>{overview[:200]}</p>' if overview else ''

    # P2: Historical Analog 回测卡片
    analogs = ctx.get('historical_analogs', [])
    exp_ret = ctx.get('expected_returns', {})
    analog_html = ""
    if analogs:
        analog_rows = ""
        for a in analogs[:3]:
            evt = a.get('event', '')[:25]
            dt = a.get('date', '')[:10]
            g7 = a.get('gain_7d_pct', 0)
            g30 = a.get('gain_30d_pct', 0)
            mdd = a.get('max_drawdown_pct', 0)
            result = a.get('result', '')
            res_col = 'var(--green2)' if result == 'beat' else ('var(--red2)' if result == 'miss' else 'var(--gold2)')
            analog_rows += (f'<tr style="border-bottom:1px solid var(--border);">'
                           f'<td style="padding:5px 8px;font-size:11px;">{dt}</td>'
                           f'<td style="padding:5px 8px;font-size:11px;">{evt}</td>'
                           f'<td style="padding:5px 8px;text-align:center;color:var(--green2);">{g7:+.1f}%</td>'
                           f'<td style="padding:5px 8px;text-align:center;color:var(--green2);">{g30:+.1f}%</td>'
                           f'<td style="padding:5px 8px;text-align:center;color:var(--red2);">{mdd:+.1f}%</td>'
                           f'<td style="padding:5px 8px;text-align:center;color:{res_col};font-weight:600;">{result}</td></tr>')
        max_dd_mean = (exp_ret.get('max_drawdown') or {}).get('mean', 0)
        sample_n = exp_ret.get('sample_size', 0)
        analog_html = f"""<div style="background:var(--bg3);border-radius:8px;padding:12px;margin-top:12px;border:1px solid var(--border);">
  <div style="font-size:11px;font-weight:700;color:var(--text2);margin-bottom:6px;">📊 历史相似信号回测（{sample_n}次样本，历史平均最大回撤 {max_dd_mean:+.1f}%）</div>
  <table style="width:100%;border-collapse:collapse;font-size:12px;">
    <thead><tr style="color:var(--text3);font-size:10px;border-bottom:1px solid var(--border);">
      <th style="padding:4px 8px;text-align:left;">日期</th>
      <th style="padding:4px 8px;text-align:left;">事件</th>
      <th style="padding:4px 8px;text-align:center;">T+7</th>
      <th style="padding:4px 8px;text-align:center;">T+30</th>
      <th style="padding:4px 8px;text-align:center;">最大回撤</th>
      <th style="padding:4px 8px;text-align:center;">结果</th>
    </tr></thead>
    <tbody>{analog_rows}</tbody>
  </table>
</div>"""

    # P6: Industry Comparison 行业竞争格局
    ic = ctx.get('industry_comparison') or {}
    industry_html = ""
    if ic and ic.get('competitors'):
        comp_str = '、'.join(ic.get('competitors', [])[:4])
        cs = ic.get('comparative_strength', 0)
        advantages = ic.get('competitive_advantages', [])
        threats = ic.get('competitive_threats', [])
        adv_html = ''.join(f'<span style="background:rgba(16,185,129,0.1);padding:2px 6px;border-radius:3px;font-size:10px;color:var(--green2);margin:2px;">{a[:30]}</span>' for a in advantages[:4])
        thr_html = ''.join(f'<span style="background:rgba(239,68,68,0.1);padding:2px 6px;border-radius:3px;font-size:10px;color:var(--red2);margin:2px;">{t[:30]}</span>' for t in threats[:4])
        cs_col = 'var(--green2)' if cs >= 70 else ('var(--gold2)' if cs >= 40 else 'var(--red2)')
        industry_html = f"""<div style="background:var(--bg3);border-radius:8px;padding:12px;margin-top:10px;border:1px solid var(--border);">
  <div style="font-size:11px;font-weight:700;color:var(--text2);margin-bottom:6px;">🏭 行业竞争格局 · 竞争力评分 <span style="color:{cs_col};font-weight:800;">{cs}/100</span></div>
  <div style="font-size:11px;color:var(--text3);margin-bottom:6px;">竞争对手：{comp_str}</div>
  <div style="display:flex;flex-wrap:wrap;gap:2px;margin-bottom:4px;">{adv_html}</div>
  <div style="display:flex;flex-wrap:wrap;gap:2px;">{thr_html}</div>
</div>"""

    # P7: ML 特征透明化
    ml_inp = ctx.get('ml_input') or {}
    ml_rec = ctx.get('ml_recommendation', '')
    ml_prob = ctx.get('ml_probability', 0)
    ml_html = ""
    if ml_inp:
        try:
            prob_pct = float(ml_prob) * 100 if float(ml_prob) < 1 else float(ml_prob)
        except (TypeError, ValueError):
            prob_pct = 0
        prob_col = 'var(--green2)' if prob_pct > 65 else ('var(--red2)' if prob_pct < 40 else 'var(--gold2)')
        features = []
        for fk, fv in ml_inp.items():
            features.append(f'<span style="background:var(--bg2);padding:2px 6px;border-radius:3px;font-size:10px;color:var(--text2);margin:2px;">{fk}={fv}</span>')
        feat_html = ''.join(features)
        ml_html = f"""<div style="background:var(--bg3);border-radius:8px;padding:12px;margin-top:10px;border:1px solid var(--border);">
  <div style="font-size:11px;font-weight:700;color:var(--text2);margin-bottom:6px;">🤖 ML 模型透视 · 推荐 <span style="color:{prob_col};font-weight:800;">{ml_rec}</span> · 概率 {prob_pct:.1f}%</div>
  <div style="display:flex;flex-wrap:wrap;gap:2px;">{feat_html}</div>
  <div style="font-size:10px;color:var(--text3);margin-top:4px;">ML 3日预期 {ctx.get("ml_3d", 0)}% · 7日预期 {ctx.get("ml_7d", 0)}% · 30日预期 {ctx.get("ml_30d", 0)}%</div>
</div>"""

    return (
        f'<p><strong>蜂群综合评分 <span class="{sc_cls}">{score:.2f}/10</span>，方向 {direction}，{score_interp}。</strong>'
        f'置信区间 [{cb_lo:.2f}–{cb_hi:.2f}]（区间宽度 {cb_hi-cb_lo:.2f}，鉴别力 {disc_label}），'
        f'维度分散度 σ={dim_std:.1f}（{std_warn}）。'
        f'期权流 <span class="highlight">{ctx["flow_direction"]}</span>，P/C={ctx["put_call_ratio"]}，总OI {ctx["total_oi"]:,.0f}。</p>'
        f'<p><strong>维度亮点：</strong>'
        f'<span class="bull-text">{top_lbl} {top_val:.1f}/10（最强维度）</span>，'
        f'<span class="bear-text">{bot_lbl} {bot_val:.1f}/10（最弱维度）</span>——'
        f'两者差距 {top_val-bot_val:.1f} 分，是本次信号分散的主要来源。'
        f'{"ML辅助（Rival）与技术（Scout）方向背离，需优先以期权结构为裁判。" if float(ctx.get("rival",{}).get("score",5))>6 and float(ctx.get("scout",{}).get("score",5))<4 else ""}'
        f'{"ScoutBee（基本面/内幕）信号极弱，说明内部人看法偏悲观。" if float(ctx.get("scout",{}).get("score",5))<3 else ""}'
        f'</p>'
        f'<p>{("逆向信号警示：" + "、".join(bear_sigs[:3])) if bear_sigs else "当前无重大逆向信号，蜂群单向性较强。"}'
        f'{"  操作建议：信号极度分散（σ≥2.5），仓位控制在标准的 40-50%，等待共振触发再加仓。" if dim_std >= 2.5 else ""}'
        f'（详见第六章情景推演与目标价推导）</p>'
        f'{_render_regime_badge(ctx)}'
        f'{overview_para}'
        f'{analog_html}{industry_html}{ml_html}'
    )


def _build_resonance_narrative(ctx: dict) -> str:
    resonance = ctx["resonance"]; res_dims = resonance.get("resonant_dimensions", [])
    detected = resonance.get('resonance_detected', False)
    boost = resonance.get('confidence_boost', 0); count = resonance.get('supporting_agents', 0)
    dim_zh = {"catalyst":"催化剂","ml_auxiliary":"ML辅助","odds":"期权/赔率",
              "risk_adj":"风险调整","signal":"技术信号","sentiment":"情绪","contrarian":"逆向"}
    dims_str = "、".join([dim_zh.get(d, d) for d in res_dims]) or "无触发维度"
    guard_det = ctx.get('guard',{}).get('details',{}) or {}
    consistency = guard_det.get('consistency', 0)
    if isinstance(consistency, float) and consistency < 1:
        consistency = int(consistency * 100)
    try:
        pcr = float(ctx['put_call_ratio'])
    except (TypeError, ValueError):
        pcr = 1.0
    ml7 = ctx['ml_7d']; ml30 = ctx['ml_30d']
    ml_cls = 'bull-text' if ml7 > 0 else 'bear-text'
    if detected:
        res_interp = (f'历史数据显示共振信号胜率比单维信号高约 18%，当前 +{boost}% 信心提升已计入置信区间。'
                      f'共振触发（{count}维同向）是本次最重要的正面信号，操作上可适当扩大仓位至标准的 80%。')
    else:
        res_interp = (f'共振未触发意味着无「多维度放大效应」。一致性指数 {consistency}% 低于触发阈值，'
                      f'信号仍在分散态——操作上建议仓位轻量（标准的 40-60%），等待更多维度收敛后再加仓。')
    return (
        f'<p><strong>{"⚡ "+str(count)+"维共振已触发，信心提升 +"+str(boost)+"%" if detected else "○ 共振未触发（信号分散）"}</strong></p>'
        f'<p>触发维度：{dims_str}。蜂群一致性指数 {consistency}%，调整系数 {guard_det.get("adjustment_factor",0.95):.2f}。{res_interp}</p>'
        f'<p>ML 模型预测：7日 <span class="{ml_cls}">{ml7:+.1f}%</span>，30日 <span class="{ml_cls}">{ml30:+.1f}%</span>。'
        f'P/C={ctx["put_call_ratio"]}（{"Call偏多" if pcr<0.9 else "中性" if pcr<1.1 else "Put偏空"}），'
        f'IV Skew {ctx["iv_skew"]}（{"下行对冲溢价偏高，市场有隐性担忧" if _sf(ctx["iv_skew"])>1.2 else "中性，多空未显著分化"}）。</p>'
    )


def _build_catalyst_narrative(ctx: dict) -> str:
    cats = ctx.get('catalysts', [])
    near = [c for c in cats if isinstance(c.get('days_until'), (int,float)) and c['days_until'] <= 14]
    chronos_sc = float(ctx.get('chronos',{}).get('score',5))
    signal_summary = ctx.get('signal_summary', '')
    pead_bias = ctx.get('pead_bias', 'neutral')
    pead_summary = ctx.get('pead_summary', '')
    cat_lines = []
    for c in cats[:5]:
        days = c.get('days_until','?'); ev = c.get('event', c.get('name',''))[:35]
        sev = c.get('severity','medium'); sev_icon = '🔴' if sev=='critical' else ('🟡' if sev=='high' else '⚪')
        cat_lines.append(f'{sev_icon} <strong>{ev}</strong>（+{days}天）')
    cats_html = ''.join(f'<li>{l}</li>' for l in cat_lines)

    # IV Crush 段落
    ivc = ctx.get('iv_crush', {}) or {}
    ivc_para = ''
    if ivc and ivc.get('avg_abs_move', 0):
        avg_move   = ivc.get('avg_abs_move', 0)
        up_c       = ivc.get('up_count', 0)
        down_c     = ivc.get('down_count', 0)
        total_c    = up_c + down_c
        win_pct    = int(up_c / total_c * 100) if total_c > 0 else 0
        imp_move   = ivc.get('current_implied_move')
        exp_date   = ivc.get('next_earnings_date', '未知')
        exp_days   = ivc.get('next_earnings_days')
        imp_str    = f'当前隐含幅度 <strong>{imp_move:.1f}%</strong>，' if imp_move else ''
        exp_str    = f'下次财报 {exp_date}（{exp_days}天后）' if exp_days is not None else f'下次财报 {exp_date}'
        crush_warn = ''
        if imp_move and imp_move > avg_move * 1.15:
            crush_warn = f'<span class="bear-text">⚠️ 隐含幅度 {imp_move:.1f}% 高于历史均值 {avg_move:.1f}%，IV Crush 风险较高——事件后期权 IV 或大幅缩水。</span>'
        elif imp_move and imp_move < avg_move * 0.85:
            crush_warn = f'<span class="bull-text">✅ 隐含幅度 {imp_move:.1f}% 低于历史均值 {avg_move:.1f}%，期权定价偏便宜，适合方向性买入。</span>'
        ivc_para = (
            f'<p><strong>📊 IV Crush 历史分析：</strong>{imp_str}'
            f'历史财报平均波动 <strong>{avg_move:.1f}%</strong>，'
            f'{exp_str}，过去 {total_c} 次财报中上涨 {up_c} 次（{win_pct}%）、下跌 {down_c} 次。'
            f'{crush_warn}</p>'
        )

    # PEAD 段落（财报后漂移）
    pead_para = ''
    if pead_bias and pead_bias != 'neutral':
        pead_color = 'bull-text' if pead_bias == 'bullish' else 'bear-text'
        pead_label = '看涨' if pead_bias == 'bullish' else '看跌'
        pead_para = (
            f'<p><strong>📈 财报后漂移（PEAD）分析：</strong>'
            f'历史数据显示本股票财报后 T+1/T+5/T+20 的漂移偏向 <span class="{pead_color}">{pead_label}</span>，'
            f'{pead_summary[:100] if pead_summary else "建议关注事件后3周内的价格传导。"}。</p>'
        )

    return (f'<p><strong>ChronosBee 评分 {chronos_sc:.1f}/10，检测到 {len(cats)} 个催化剂，{len(near)} 个在14天内。</strong>'
            f'{" " + signal_summary if signal_summary else ""}</p>'
            f'<ul style="margin:8px 0 8px 16px;line-height:1.8">{cats_html}</ul>'
            f'<p>{"⚠️ 关键窗口：近期催化剂密度极高，财报/重大事件前期权隐含波动率（IV）通常显著上升，建议关注 IV crush 风险。" if len(near)>=2 else "催化剂相对分散，短期波动性压力适中。"}'
            f'期权到期日集中于 {", ".join(ctx.get("oracle",{}).get("details",{}).get("expiration_dates",[])[:3] or ["近期"])}，'
            f'催化剂与到期日重合度高，期权博弈激烈。（IV Crush 风险亦影响第六章情景推演的期权策略选择）</p>'
            f'{ivc_para}'
            f'{pead_para}')


def _build_options_narrative(ctx: dict) -> str:
    # ── 基础字段 ──
    pcr = ctx['put_call_ratio']; oi = float(ctx.get('total_oi', 0) or 0); iv = ctx['iv_current']
    _ivr_raw = ctx.get('iv_rank', 50)
    iv_rank = float(_ivr_raw) if _ivr_raw is not None else 50.0
    skew = ctx['iv_skew']; flow = ctx['flow_direction']
    sups = ctx['key_levels'].get('support',[])[:2]
    ress = ctx['key_levels'].get('resistance',[])[:2]
    unusual = ctx.get('unusual_activity',[])
    bull_flows = [u for u in unusual if u.get('bullish')]
    bear_flows = [u for u in unusual if not u.get('bullish')]
    flow_cls = 'bull-text' if flow=='bullish' else ('bear-text' if flow=='bearish' else 'neutral-text')
    try:
        pcr_f = float(pcr)
    except (TypeError, ValueError):
        pcr_f = 1.0
    try:
        skew_f = float(skew)
    except (TypeError, ValueError):
        skew_f = 1.0

    # ── v0.10.0 新增字段 ──
    iv_rv      = float(ctx.get('iv_rv_spread', 0) or 0)
    iv_rv_sig  = ctx.get('iv_rv_signal', '')
    rv_30d     = float(ctx.get('rv_30d', 0) or 0)
    gc         = ctx.get('gamma_calendar', {}) or {}
    pin_strike = gc.get('pin_strike', '')
    pin_expiry = gc.get('pin_expiry', '')
    charm_dir  = gc.get('charm_direction', '')
    gex_regime = ctx.get('gex_regime', '')
    gex_flip   = ctx.get('gex_flip', '')
    gex_cw     = ctx.get('gex_call_wall', '')
    gex_pw     = ctx.get('gex_put_wall', '')
    try:
        gamma_exp = float(ctx.get('gamma_exposure', 0) or 0)
    except (TypeError, ValueError):
        gamma_exp = 0.0
    squeeze    = ctx.get('gamma_squeeze_risk', '')
    regime     = ctx.get('market_regime', {}) or {}
    overall_rg = regime.get('overall_regime', '')
    crowding   = ctx.get('signal_crowding', {}) or {}
    decay_f    = float(crowding.get('alpha_decay_factor', 1.0) or 1.0)
    ivts       = ctx.get('iv_term_structure', {}) or {}
    ivts_shape = ivts.get('shape', '')

    # IV rank interpretation
    iv_interp = ('高（期权较贵，适合卖方策略）' if iv_rank > 70 else
                 ('低（期权便宜，适合买方策略）' if iv_rank < 30 else '中等'))
    # Key level details
    sup_parts = [f'${s["strike"]}（{s.get("oi",0)/1e3:.0f}K OI）' for s in sups] if sups else ['N/A']
    res_parts = [f'${r["strike"]}（{r.get("oi",0)/1e3:.0f}K OI）' for r in ress] if ress else ['N/A']

    # ── P1: 市场结构总览 ──
    options_score = ctx.get('options_score', 0)
    iv_skew_signal = ctx.get('iv_skew_signal', '')
    otm_put_iv = ctx.get('otm_put_iv', 0)
    otm_call_iv = ctx.get('otm_call_iv', 0)
    p1 = (
        f'<p><strong>期权市场结构：P/C={pcr}（{"Call偏多，买方主导，看多氛围浓" if pcr_f<0.9 else "Put偏多，下行对冲需求强" if pcr_f>1.1 else "多空均衡"}），'
        f'总OI {oi:,.0f}，IV Current {iv:.1f}%，IV Rank {iv_rank:.0f}%（{iv_interp}），'
        f'IV Skew {skew}（{"Put溢价偏高，尾部风险定价显著" if skew_f>1.2 else "Skew中性，定价均衡" if skew_f>0.8 else "Call溢价偏高，上行投机情绪浓"}）。</strong>'
        f'{"期权综合评分 " + f"{options_score:.1f}/10，{iv_skew_signal}；" if options_score else ""}'
        f'当前市场{"处于 " + overall_rg + " 政体，" if overall_rg else ""}'
        f'{"信号拥挤度偏高（衰减因子 " + f"{decay_f:.2f}" + "），alpha 衰减风险需关注；" if decay_f < 0.8 else ""}'
        f'期权流方向为<span class="{flow_cls}">{"净看涨流" if flow=="bullish" else "净看跌流" if flow=="bearish" else "中性流"}</span>，'
        f'{len(bull_flows)}笔看涨异动、{len(bear_flows)}笔看跌异动。'
        # N1: So What 推理
        f'<strong>交易含义：</strong>'
        f'{"IV Rank 偏低 + Call 流主导 = 买方成本低且方向明确，适合定向做多（long call / bull call spread）。" if iv_rank < 30 and pcr_f < 0.9 else ""}'
        f'{"IV Rank 偏高 + Put Skew 显著 = 市场恐慌定价充分，卖方策略（iron condor / short put）的 theta 衰减收益丰厚。" if iv_rank > 70 and skew_f > 1.15 else ""}'
        f'{"多空流方向分歧 → 短期波动放大概率高，优先选择跨式策略（straddle/strangle）或观望等方向明确。" if abs(len(bull_flows) - len(bear_flows)) <= 1 and len(bull_flows) + len(bear_flows) >= 4 else ""}'
        f'{"IV Rank 中性区间，期权既不便宜也不贵——优先选价差策略（spread）控制 vega 暴露。" if 30 <= iv_rank <= 70 and not (iv_rank > 70 and skew_f > 1.15) and not (iv_rank < 30 and pcr_f < 0.9) else ""}'
        f'</p>'
    )

    # ── P2: IV-RV 价差与波动率深度分析 ──
    if iv_rv != 0 or rv_30d != 0:
        iv_rv_interp = ('期权相对历史波动率显著偏贵，做市商定价中包含较高风险溢价，卖方策略（如 short strangle、iron condor）统计优势明显'
                        if iv_rv > 5 else
                        '期权相对历史波动率偏贵，隐含波动率高于实际波动，卖方有一定统计优势'
                        if iv_rv > 3 else
                        '期权定价合理，IV 与 HV 基本匹配，无明显错价'
                        if iv_rv > -3 else
                        '期权相对历史波动率偏便宜，方向性买入（long call/put）有统计优势，隐含波动率被低估')
        skew_breakdown = f'OTM Put IV {otm_put_iv:.1f}% vs OTM Call IV {otm_call_iv:.1f}%，' if otm_put_iv and otm_call_iv else ''
        p2 = (
            f'<p><strong>波动率深度：</strong>'
            f'IV {iv:.1f}% vs HV30（30日已实现波动率）{rv_30d:.1f}%，'
            f'IV-RV 价差 <strong>{iv_rv:+.1f}%</strong>（{iv_rv_sig}）——'
            f'{iv_rv_interp}。'
            f'{skew_breakdown}'
        )
        # IV 期限结构
        if ivts_shape:
            shape_interp = {
                'contango': '正常 Contango（远月>近月），市场预期短期平稳、远期不确定性更高',
                'backwardation': 'Backwardation（近月>远月），市场定价近期事件风险，可能存在财报/重大事件前的 IV 跳升',
                'flat': 'Flat（各期限 IV 接近），市场对短中期波动预期一致',
            }
            p2 += (f'IV 期限结构呈 <strong>{ivts_shape}</strong> 形态'
                   f'（{shape_interp.get(ivts_shape, ivts_shape)}）。')
        # N1: So What — IV-RV 交易含义
        _near_cat = next((c for c in ctx.get('catalysts', []) if 0 <= (c.get('days_until') or 99) <= 14), None)
        _cat_name = _near_cat.get('event', '') if _near_cat else ''
        if iv_rv > 5:
            _iv_excess_pct = iv_rv / rv_30d * 100 if rv_30d > 0 else 0
            p2 += (f' <strong>交易含义：</strong>期权隐含恐慌超出实际波动 {_iv_excess_pct:.0f}%，卖方策略期望值为正'
                   f'{"；但距 " + _cat_name + " 较近，建议等催化剂落地后 IV Crush 窗口再入场" if _near_cat else ""}。')
        elif iv_rv < -5:
            p2 += f' <strong>交易含义：</strong>IV 被低估，方向性买入成本偏低，若配合信号共振可积极建仓。'
        p2 += '</p>'
    else:
        p2 = ''

    # ── P3: GEX（Gamma Exposure）与做市商行为分析 ──
    _mp_raw = ctx.get('max_pain')
    _mp_val = None
    if isinstance(_mp_raw, dict):
        _mp_val = _mp_raw.get('max_pain')
    elif isinstance(_mp_raw, (int, float)):
        _mp_val = _mp_raw
    try:
        _mp_val = float(_mp_val) if _mp_val is not None else None
    except (TypeError, ValueError):
        _mp_val = None
    max_pain_note = f'Max Pain 位于 <strong>${_mp_val:.0f}</strong>，做市商磁吸效应显著；' if _mp_val else ''
    p3_parts = []
    if gex_regime or gamma_exp:
        gex_interp = ''
        if gex_regime == 'positive_gamma':
            gex_interp = '做市商持有正 Gamma，价格波动时其对冲行为会抑制波动（买跌卖涨），市场倾向于区间震荡'
        elif gex_regime == 'negative_gamma':
            gex_interp = '做市商持有负 Gamma，价格波动时其对冲行为会放大波动（追涨杀跌），趋势行情一旦启动难以遏制'
        elif gex_regime == 'positive_gex':
            gex_interp = '做市商净正 Gamma，对冲行为抑制波动（买跌卖涨），价格倾向区间震荡'
        elif gex_regime == 'negative_gex':
            gex_interp = '做市商净负 Gamma，对冲行为放大波动（追涨杀跌），趋势行情难以遏制'
        else:
            gex_interp = '中性 Gamma 区域' if gex_regime else 'GEX 数据可用'
        p3_parts.append(
            f'<strong>Dealer GEX 分析：</strong>'
            f'{max_pain_note}'
            f'总 Gamma 暴露 {gamma_exp:+,.0f}，'
            f'GEX 政体为 <strong>{gex_regime or "未知"}</strong>——{gex_interp}。'
        )
        if gex_flip:
            p3_parts.append(f'GEX 翻转点位于 <strong>${gex_flip}</strong>，价格跌破此水位将从正 Gamma 切换到负 Gamma 区域，波动性骤增。')
        if gex_cw or gex_pw:
            p3_parts.append(
                f'做市商 Call Wall（最大阻力）：${gex_cw or "N/A"}，Put Wall（最大支撑）：${gex_pw or "N/A"}。'
            )
        if squeeze:
            squeeze_interp = ('⚠️ Gamma Squeeze 风险较高，大量做市商 short gamma 头寸面临被迫追涨平仓压力' if squeeze == 'high'
                              else 'Gamma Squeeze 风险中等，需关注成交量突增信号' if squeeze == 'medium'
                              else 'Gamma Squeeze 风险较低')
            p3_parts.append(f'{squeeze_interp}。')
    p3 = f'<p>{" ".join(p3_parts)}</p>' if p3_parts else ''

    # ── P4: Gamma 日历与 Pin Risk ──
    p4 = ''
    if pin_strike or charm_dir:
        p4_parts = []
        if pin_strike:
            p4_parts.append(
                f'<strong>Gamma 到期日历：</strong>'
                f'最高 OI 钉子位（Pin Strike）为 <strong>${pin_strike}</strong>'
                f'{" @ " + pin_expiry if pin_expiry else ""}，'
                f'到期日前做市商 gamma 对冲力量将把价格向此水位"吸引"，偏离越远反弹/回落概率越高'
            )
        if charm_dir:
            if charm_dir in ('positive', 'bullish'):
                charm_interp = '到期日临近时 delta 衰减偏正，做市商被动买入标的，形成向上支撑力'
            elif charm_dir in ('negative', 'bearish'):
                charm_interp = '到期日临近时 delta 衰减偏负，做市商被动卖出标的，形成向下压力'
            else:
                charm_interp = '到期日前 delta 衰减方向中性'
            p4_parts.append(f'Charm 到期衰减方向为 <strong>{charm_dir}</strong>——{charm_interp}')
        p4 = f'<p>{"；".join(p4_parts)}。</p>'

    # ── P5: 异常流与关键水位 ──
    ua_notes = []
    for ua in unusual[:3]:  # 最多展示3笔
        ua_vol = ua.get('volume', 0)
        ua_str = ua.get('strike', 0)
        ua_type = ua.get('type', '')
        ua_cls = 'bull-text' if ua.get('bullish') else 'bear-text'
        ua_label = '看涨' if ua.get('bullish') else '看跌'
        ua_notes.append(f'<span class="{ua_cls}">{ua_type} ${ua_str}（{ua_vol/1e3:.0f}K 手，{ua_label}）</span>')
    ua_html = ''
    if ua_notes:
        ua_html = f'异常流明细：{"、".join(ua_notes)}。'
        if bull_flows and not bear_flows:
            ua_html += '大资金单边押注上行，关注做空阻力区的 Call 卖方头寸是否被迫平仓（gamma squeeze 预警）。'
        elif bear_flows and not bull_flows:
            ua_html += '大资金单边下行对冲，关注支撑区是否有同步 Put 保护堆积。'
        elif bull_flows and bear_flows:
            ua_html += '多空双方均有大单博弈，方向分歧明显，短期可能放大波动。'

    p5 = (
        f'<p><strong>关键水位与异常流：</strong>'
        f'Put 支撑——{"、".join(sup_parts)}（高 OI 钉住，做市商在此有 gamma 对冲买盘支撑）；'
        f'Call 阻力——{"、".join(res_parts)}（做市商 short gamma 区，靠近时面临系统性卖压）。'
        f'{ua_html}'
        f'{"价格突破最大阻力需超大成交量配合，短期内概率低。" if ress else ""}'
        f'（以上支撑/阻力位将作为第六章情景推演中止损与目标价的参考基准）</p>'
    )

    return p1 + p2 + p3 + p4 + p5


def _build_macro_narrative(ctx: dict) -> str:
    fg = ctx.get('fg_score'); guard_sc = float(ctx.get('guard',{}).get('score',5))
    guard_disc = ctx.get('guard',{}).get('discovery','')
    buzz_det = ctx.get('buzz',{}).get('details',{}) or {}
    reddit = ctx.get('reddit',{}) or {}
    sentiment = buzz_det.get('sentiment_pct', 0)
    vol_ratio = buzz_det.get('volume_ratio', 1.0)
    momentum = buzz_det.get('momentum_5d', 0)
    fg_label = '极度恐惧' if fg and fg<=25 else ('恐惧' if fg and fg<=45 else ('中性' if fg and fg<=55 else ('贪婪' if fg and fg<=75 else '极度贪婪')))

    # VIX 期限结构段落
    vix_term = ctx.get('vix_term_structure', {}) or {}
    vix_para = ''
    if vix_term and vix_term.get('structure') not in ('', 'unknown', None):
        structure  = vix_term.get('structure', 'unknown')
        spot_vix   = vix_term.get('spot_vix')
        m1         = vix_term.get('m1')
        m2         = vix_term.get('m2')
        spread     = vix_term.get('m1_m2_spread')
        signal     = vix_term.get('signal', '')
        struct_zh  = {'contango': '正价差（Contango）', 'backwardation': '逆价差（Backwardation）', 'flat': '平坦结构'}.get(structure, structure)
        struct_color = 'bull-text' if structure == 'contango' else ('bear-text' if structure == 'backwardation' else 'highlight')
        _spot_str   = f"{spot_vix:.1f}" if spot_vix is not None else "N/A"
        _m1_str     = f"{m1:.1f}"     if m1      is not None else "N/A"
        _m2_str     = f"{m2:.1f}"     if m2      is not None else "N/A"
        _spread_str = f"{spread:+.2f}" if spread  is not None else "N/A"
        vix_para = (
            f'<p><strong>📈 VIX 期限结构：</strong>'
            f'当前 VIX <strong>{_spot_str}</strong>，M1={_m1_str} → M2={_m2_str}（价差 {_spread_str}），'
            f'结构为 <span class="{struct_color}">{struct_zh}</span>。'
            f'{"Contango 结构代表市场情绪平稳，做市商对冲成本低，月期权卖方策略有利。" if structure == "contango" else ("⚠️ Backwardation 恐慌结构——近月 VIX 溢价高于远月，市场短期恐慌信号明确，期权买方成本大幅上升。" if structure == "backwardation" else "VIX 期限结构平坦，市场对近远期风险判断趋于一致。")}'
            f'{(" | " + signal[:80]) if signal else ""}</p>'
        )

    # 国会交易段落
    congress = ctx.get('congress', {}) or {}
    congress_para = ''
    if congress and (congress.get('buy_count', 0) + congress.get('sell_count', 0)) > 0:
        buy_c  = congress.get('buy_count', 0)
        sell_c = congress.get('sell_count', 0)
        c_sc   = congress.get('congress_score', 0)
        c_sum  = congress.get('summary', '')
        c_top  = congress.get('top_signal', '')
        net    = congress.get('net_amount_est', 0)
        net_str = f'净买入 ${abs(net)/1e6:.1f}M' if net > 0 else (f'净卖出 ${abs(net)/1e6:.1f}M' if net < 0 else '净额持平')
        c_color = 'bull-text' if buy_c > sell_c else 'bear-text'
        congress_para = (
            f'<p><strong>🏛️ 国会交易信号（90天）：</strong>'
            f'买入 <span class="{c_color}">{buy_c} 次</span> / 卖出 {sell_c} 次，{net_str}，'
            f'国会信号评分 {c_sc}/10。'
            f'{c_top[:60] + "。" if c_top else ""}'
            f'{c_sum[:80] if c_sum else ""}</p>'
        )

    # 供应链段落
    supply_chain = ctx.get('supply_chain', {}) or {}
    supply_chain_para = ''
    if supply_chain:
        sc_note = supply_chain.get('supply_chain_note', '')
        sc_signal = supply_chain.get('supply_chain_signal', '')
        sc_peers = supply_chain.get('peers', [])
        peers_text = ''
        if sc_peers and len(sc_peers) >= 2:
            peers_text = f'主要同行业玩家：{sc_peers[0]} 与 {sc_peers[1]} 等。'
        supply_chain_para = (
            f'<p><strong>🔗 供应链信号：</strong>'
            f'{sc_note[:100] if sc_note else ""}，'
            f'信号判断为 <strong>{sc_signal}</strong>。'
            f'{peers_text}'
            f'（供应链信号亦影响第一章基本面维度评分）</p>'
        ) if sc_note or sc_signal else ''

    # P4: 情绪动量数据（字段可能是 dict 或标量）
    sent_pct = ctx.get('sentiment_pct')
    _sm_raw = ctx.get('sentiment_momentum')
    _sd_raw = ctx.get('sentiment_divergence')
    vol_ratio_ctx = ctx.get('volume_ratio')
    sent_parts = []
    if sent_pct is not None:
        sent_parts.append(f'舆情情绪 <strong>{sent_pct}%</strong>')
    if _sm_raw is not None:
        try:
            if isinstance(_sm_raw, dict):
                _mom_regime = _sm_raw.get('momentum_regime', '')
                _mom_d1 = _sm_raw.get('delta_1d', 0)
                mom_dir = '上升' if _mom_regime == 'rising' or float(_mom_d1) > 0 else '下降'
                mom_col = 'var(--green2)' if mom_dir == '上升' else 'var(--red2)'
                sent_parts.append(f'情绪动量 <span style="color:{mom_col};font-weight:700;">{mom_dir}（1d={_mom_d1:+d}，3d={_sm_raw.get("delta_3d",0):+d}）</span>')
            else:
                _mom_f = float(_sm_raw)
                mom_dir = '上升' if _mom_f > 0 else '下降'
                mom_col = 'var(--green2)' if _mom_f > 0 else 'var(--red2)'
                sent_parts.append(f'情绪动量 <span style="color:{mom_col};font-weight:700;">{mom_dir}（{_mom_f:+.1f}）</span>')
        except (TypeError, ValueError):
            pass
    if _sd_raw is not None:
        try:
            if isinstance(_sd_raw, dict):
                _div_type = _sd_raw.get('divergence_type', 'none')
                _div_sev = float(_sd_raw.get('severity', 0) or 0)
                if _div_type != 'none' and _div_sev > 0.1:
                    div_label = '看多背离（价跌情绪升）' if 'bull' in _div_type else '看空背离（价涨情绪降）'
                    div_col = 'var(--green2)' if 'bull' in _div_type else 'var(--red2)'
                    sent_parts.append(f'<span style="color:{div_col};">⚠️ {div_label}（强度{_div_sev:.1f}）</span>')
            else:
                div_f = float(_sd_raw)
                if abs(div_f) > 0.1:
                    div_label = '看多背离（价跌情绪升）' if div_f > 0 else '看空背离（价涨情绪降）'
                    div_col = 'var(--green2)' if div_f > 0 else 'var(--red2)'
                    sent_parts.append(f'<span style="color:{div_col};">⚠️ {div_label}</span>')
        except (TypeError, ValueError):
            pass
    if vol_ratio_ctx is not None:
        try:
            vr = float(vol_ratio_ctx)
            vr_label = '放量' if vr > 1.5 else ('缩量' if vr < 0.7 else '正常')
            sent_parts.append(f'成交量比 {vr:.2f}x（{vr_label}）')
        except (TypeError, ValueError):
            pass
    sent_html = '<p><strong>情绪动量信号：</strong>' + '；'.join(sent_parts) + '。</p>' if sent_parts else ''

    return (f'<p><strong>宏观环境：F&G指数 {fg if fg else "N/A"}（{fg_label}），GuardBee {guard_sc:.1f}/10。</strong>'
            f'{guard_disc[:200]}</p>'
            f'<p>情绪面：看多情绪 {sentiment}%（{"偏多" if sentiment>55 else ("偏空" if sentiment<45 else "中性")}），'
            f'5日动量 {momentum:+.2f}%，成交量比 {vol_ratio:.2f}x（{"放量" if vol_ratio>1.2 else ("缩量" if vol_ratio<0.8 else "正常")}）。'
            f'Reddit 排名 #{reddit.get("rank","N/A")}，提及 {reddit.get("mentions",0)} 次。</p>'
            f'{sent_html}'
            f'<p>{"⚠️ 当前极度恐惧市场环境增加了短期波动性，建议降低仓位或等待恐慌情绪缓和再入场。" if fg and fg<=25 else ("市场情绪相对中性，系统性风险较低，以个股信号为主要决策依据。" if fg and fg<=55 else "市场情绪偏乐观，注意过热风险，控制追高仓位。")}</p>'
            f'{vix_para}'
            f'{congress_para}'
            f'{supply_chain_para}')


def _build_odds_boost_card(ctx: dict) -> str:
    """v0.15.0: Odds Boost 第 6 维融合卡片 — 展示 swarm final_score 的概率加成审计"""
    pb = ctx.get("probability_boost") or {}
    win = ctx.get("win_probability_pct", 0) or pb.get("win_probability_pct", 0)
    rr  = ctx.get("risk_reward_ratio", 0) or pb.get("risk_reward_ratio", 0)
    if not win and not rr:
        return ""
    applied = pb.get("applied", False)
    if applied:
        before = pb.get("score_before", 0)
        after  = pb.get("score_after", 0)
        boost  = pb.get("boost_value", 0)
        dir_b  = pb.get("direction_before", "")
        dir_a  = pb.get("direction_after", "")
        bear_s = pb.get("bear_strength", 0)
        bear_tag = (f'<span style="color:var(--red);font-size:10px;">bear {bear_s:.1f} 对冲50%</span>'
                    if bear_s >= 7 else "")
        dir_tag = (f'<span style="color:var(--green2);font-weight:700;">→ {dir_a}</span>'
                   if dir_a != dir_b else f'<span style="color:var(--text2)">{dir_a}</span>')
        return (
            f'<div style="background:linear-gradient(135deg,var(--bg3),var(--bg2));'
            f'border:1px solid var(--green2);border-radius:12px;padding:14px;margin:12px 0;">'
            f'<div style="font-size:13px;font-weight:700;color:var(--green2);margin-bottom:8px;">'
            f'⚡ Odds Boost 第6维融合（probability_analysis）</div>'
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;font-size:12px;">'
            f'<div><div style="color:var(--text2)">胜率</div>'
            f'<div style="font-size:18px;font-weight:700;color:var(--green2)">{win:.0f}%</div></div>'
            f'<div><div style="color:var(--text2)">赔率</div>'
            f'<div style="font-size:18px;font-weight:700;color:var(--accent)">{rr:.1f}x</div></div>'
            f'<div><div style="color:var(--text2)">加成</div>'
            f'<div style="font-size:18px;font-weight:700;color:var(--green2)">+{boost:.2f}</div>'
            f'{bear_tag}</div>'
            f'<div><div style="color:var(--text2)">评分</div>'
            f'<div style="font-size:16px;font-weight:700;color:var(--text1)">'
            f'{before:.2f} → <span style="color:var(--green2)">{after:.2f}</span></div>'
            f'{dir_tag}</div>'
            f'</div></div>')
    else:
        reason = pb.get("reason", f"win {win:.0f}% / rr {rr:.1f}x 未达阈值")
        return (
            f'<div style="background:var(--bg3);border:1px dashed var(--border);'
            f'border-radius:12px;padding:12px;margin:12px 0;font-size:12px;color:var(--text2);">'
            f'⚪ Odds Boost: 未触发 — {reason}</div>')


def _build_valuation_card(ctx: dict) -> str:
    """V1: 估值快照卡片 — PE/PEG/分析师共识 + PE倍数情景矩阵"""
    fwd_eps  = ctx.get("forward_eps", 0)
    ttm_eps  = ctx.get("trailing_eps", 0)
    eps_grw  = ctx.get("eps_growth", 0)
    tgt      = ctx.get("analyst_target", 0)
    cons     = ctx.get("analyst_consensus", 0)
    n_anal   = ctx.get("analyst_count", 0)
    price    = float(ctx.get("price") or 0)

    if not fwd_eps or not price:
        return ""  # 无数据则静默不渲染

    pe_ttm   = price / ttm_eps if ttm_eps else None
    pe_fwd   = price / fwd_eps if fwd_eps else None
    peg      = pe_fwd / eps_grw if (pe_fwd and eps_grw) else None
    upside   = (tgt / price - 1) * 100 if (tgt and price) else None

    # 分析师共识映射
    _cons_map = {1: "强烈看多", 2: "看多", 3: "中性", 4: "看空", 5: "强烈看空"}
    cons_label = _cons_map.get(round(cons), f"{cons:.1f}/5") if cons else "N/A"
    if cons and cons < 1.5:
        cons_label = "强烈看多"
    elif cons and cons < 2.5:
        cons_label = "看多"
    elif cons and cons < 3.5:
        cons_label = "中性"
    elif cons and cons < 4.5:
        cons_label = "看空"
    elif cons:
        cons_label = "强烈看空"

    # PEG 颜色与解读
    if peg is not None:
        if peg < 0.5:
            peg_color, peg_label = "var(--green2)", "极度低估"
        elif peg < 1.0:
            peg_color, peg_label = "var(--green2)", "低估"
        elif peg < 1.5:
            peg_color, peg_label = "#e8a838", "合理"
        else:
            peg_color, peg_label = "var(--red2)", "偏贵"
    else:
        peg_color, peg_label = "var(--text2)", "N/A"

    # PE 倍数情景矩阵
    pe_scenarios = [(18, "深度衰退"), (22, "保守"), (26, "基准"), (30, "乐观"), (35, "泡沫")]
    scenario_rows = ""
    for pe_m, label in pe_scenarios:
        pt = pe_m * fwd_eps
        chg = (pt / price - 1) * 100
        bar_w = max(5, min(95, 50 + chg / 3))  # 粗略映射为条宽
        color = "var(--green2)" if chg > 0 else "var(--red2)"
        marker = " ← BofA 参考" if 24 <= pe_m <= 26 else ""
        scenario_rows += (
            f'<div style="display:grid;grid-template-columns:70px 40px 1fr 70px;align-items:center;gap:6px;font-size:12px;line-height:2;">'
            f'<span style="color:var(--text2)">{label}</span>'
            f'<span style="font-weight:600">{pe_m}x</span>'
            f'<div style="background:var(--bg2);border-radius:4px;height:14px;overflow:hidden;">'
            f'<div style="width:{bar_w:.0f}%;height:100%;background:{color};border-radius:4px;"></div></div>'
            f'<span style="font-weight:600;color:{color}">${pt:.0f} ({chg:+.0f}%){marker}</span>'
            f'</div>'
        )

    # Build metric cards
    _cards = []
    if pe_ttm:
        _cards.append(
            f'<div style="text-align:center;padding:10px;background:var(--bg2);border-radius:8px;">'
            f'<div style="font-size:11px;color:var(--text2)">PE (TTM)</div>'
            f'<div style="font-size:18px;font-weight:700;color:var(--text1)">{pe_ttm:.1f}x</div></div>')
    if pe_fwd:
        _cards.append(
            f'<div style="text-align:center;padding:10px;background:var(--bg2);border-radius:8px;">'
            f'<div style="font-size:11px;color:var(--text2)">PE (Forward)</div>'
            f'<div style="font-size:18px;font-weight:700;color:var(--accent)">{pe_fwd:.1f}x</div></div>')
    if peg is not None:
        _cards.append(
            f'<div style="text-align:center;padding:10px;background:var(--bg2);border-radius:8px;">'
            f'<div style="font-size:11px;color:var(--text2)">PEG</div>'
            f'<div style="font-size:18px;font-weight:700;color:{peg_color}">{peg:.2f}</div>'
            f'<div style="font-size:10px;color:{peg_color}">{peg_label}</div></div>')
    if tgt:
        _up_col = "var(--green2)" if upside and upside > 0 else "var(--red2)"
        _cards.append(
            f'<div style="text-align:center;padding:10px;background:var(--bg2);border-radius:8px;">'
            f'<div style="font-size:11px;color:var(--text2)">分析师目标价</div>'
            f'<div style="font-size:18px;font-weight:700;color:{_up_col}">${tgt:.0f}</div>'
            f'<div style="font-size:10px;color:var(--text2)">{n_anal}人共识 · {cons_label}</div></div>')
    _cards_html = "".join(_cards)

    return (
        f'<div style="background:var(--bg3);border:1px solid var(--border);border-radius:12px;padding:16px;margin:16px 0;">'
        f'<div style="font-size:13px;font-weight:700;color:var(--text1);margin-bottom:12px;">📊 估值快照</div>'
        f'<div style="display:grid;grid-template-columns:repeat({len(_cards)},1fr);gap:10px;margin-bottom:14px;">'
        f'{_cards_html}</div>'
        f'<div style="font-size:11px;font-weight:600;color:var(--text2);margin-bottom:6px;">PE 倍数情景矩阵（Forward EPS ${fwd_eps:.2f}）</div>'
        f'{scenario_rows}'
        f'</div>'
    )


def _build_scenario_narrative(ctx: dict) -> str:
    """CH6: 五情景推演 + 期望值 + If-Then 决策树（本地 fallback）"""
    import math as _math

    score    = float(ctx["final_score"])
    direction = ctx["direction"]
    is_bull  = 'bull' in direction

    ress = ctx['key_levels'].get('resistance', [])
    sups = ctx['key_levels'].get('support', [])
    price  = float(ctx.get('price') or 0)
    ml7    = ctx.get('ml_7d', 50)
    ml30   = ctx.get('ml_30d', 0)

    res1 = float(ress[0]['strike']) if ress else None
    res2 = float(ress[1]['strike']) if len(ress) > 1 else res1
    sup1 = float(sups[0]['strike']) if sups else None
    sup2 = float(sups[1]['strike']) if len(sups) > 1 else sup1

    res1_s = f"${res1:.0f}" if res1 else 'N/A'
    res2_s = f"${res2:.0f}" if res2 else 'N/A'
    sup1_s = f"${sup1:.0f}" if sup1 else 'N/A'
    sup2_s = f"${sup2:.0f}" if sup2 else 'N/A'

    skew_f   = _sf(ctx['iv_skew'], 1.0)
    iv_rank  = float(ctx.get('iv_rank', 50) or 50)
    iv_rv    = float(ctx.get('iv_rv_spread', 0) or 0)
    iv_curr  = float(ctx.get('iv_current', 0) or 0)

    # Confidence band
    cb      = ctx.get('confidence_band') or [max(0, score - 1.5), min(10, score + 1.5)]
    cb_lo   = float(cb[0])
    cb_hi   = float(cb[1])
    band_w  = float(ctx.get('band_width') or (cb_hi - cb_lo))
    dim_std = float(ctx.get('dimension_std') or 1.5)

    # Gamma calendar
    gc        = ctx.get('gamma_calendar') or {}
    pin       = gc.get('pin_strike')
    charm_dir = gc.get('charm_direction', '')

    # Crowding / regime
    decay_f  = float((ctx.get('signal_crowding') or {}).get('alpha_decay_factor', 1.0) or 1.0)
    regime   = (ctx.get('market_regime') or {}).get('overall_regime', '')

    # PEAD
    pead_bias    = ctx.get('pead_bias', 'neutral')
    pead_summary = ctx.get('pead_summary', '')

    # Catalysts
    cats     = ctx.get('catalysts', [])
    near_cat = next((c for c in cats if 0 <= (c.get('days_until') or 99) <= 14), None)
    near_cat_name = near_cat.get('event', '') if near_cat else ''
    near_cat_days = near_cat.get('days_until', 0) if near_cat else None

    # ── PROBABILITY ENGINE ──────────────────────────────────────────
    # Base probabilities from score bands (5-scenario: A=strong bull, B=mild bull, C=neutral, D=mild bear, E=strong bear)
    if score >= 7.5:
        pa, pb, pc, pd, pe = 35, 25, 20, 12, 8
    elif score >= 6.0:
        pa, pb, pc, pd, pe = 20, 25, 30, 16, 9
    elif score >= 5.0:
        pa, pb, pc, pd, pe = 12, 18, 35, 22, 13
    elif score >= 3.5:
        pa, pb, pc, pd, pe = 9, 16, 30, 25, 20
    else:
        pa, pb, pc, pd, pe = 6, 12, 22, 28, 32

    # ML adjustment
    try:
        ml7_f = float(ml7)
        if ml7_f > 70:
            pa = min(45, pa + 5); pe = max(3, pe - 3)
        elif ml7_f < 40:
            pe = min(42, pe + 5); pa = max(3, pa - 3)
    except Exception:
        pass

    # Catalyst window adds tail risk
    if near_cat:
        pc = max(5, pc - 5)
        pa += 3; pe += 2

    # Crowding adds reversal risk
    if decay_f < 0.82:
        if is_bull:
            pe = min(40, pe + 4)
        else:
            pa = min(40, pa + 4)

    # PEAD adjustment
    if pead_bias == 'bullish':
        pa = min(45, pa + 3); pe = max(3, pe - 2)
    elif pead_bias == 'bearish':
        pe = min(40, pe + 3); pa = max(3, pa - 2)

    # Normalize to 100
    total = pa + pb + pc + pd + pe
    scale = 100 / total
    pa = round(pa * scale)
    pb = round(pb * scale)
    pc = round(pc * scale)
    pd = round(pd * scale)
    pe = 100 - pa - pb - pc - pd

    # ── RETURN ESTIMATES ─────────────────────────────────────────────
    # V2: 优先用 PE 倍数 × Forward EPS 锚定情景价格，无 EPS 时 fallback 技术面
    _fwd_eps = float(ctx.get("forward_eps", 0) or 0)
    def _safe_ret(target, base, pct_fallback):
        if target and base and base > 0:
            return (target - base) / base * 100
        return pct_fallback

    if _fwd_eps > 0 and price > 0:
        # PE 倍数锚定：根据方向调整基准
        _pe_a = 32 if is_bull else 28      # 强势多头
        _pe_b = 26 if is_bull else 24      # 温和看涨
        _pe_d = 18 if is_bull else 16      # 温和看跌
        _pe_e = 14 if is_bull else 12      # 强势看跌
        ret_a = (_fwd_eps * _pe_a / price - 1) * 100
        ret_b = (_fwd_eps * _pe_b / price - 1) * 100
        ret_c = 0.3
        ret_d = (_fwd_eps * _pe_d / price - 1) * 100
        ret_e = (_fwd_eps * _pe_e / price - 1) * 100
    else:
        ret_a = _safe_ret(res1, price, 10.0)
        ret_b = ret_a * 0.4 if ret_a > 0 else _safe_ret(res1, price, 4.0) * 0.4
        ret_c = 0.3
        ret_d = _safe_ret(sup1, price, -5.0) * 0.4
        ret_e = _safe_ret(sup1, price, -12.0)

    # ── EXPECTED VALUE ────────────────────────────────────────────────
    ev = (pa * ret_a + pb * ret_b + pc * ret_c + pd * ret_d + pe * ret_e) / 100
    ev_color = 'var(--green2)' if ev > 1.0 else ('var(--red2)' if ev < -1.0 else 'var(--gold2)')

    # Risk-Reward
    rr_num = abs(ret_a) / abs(ret_e) if ret_e != 0 else 2.0
    rr_str = f"{rr_num:.1f}:1"

    # Uncertainty
    uncertainty = "高" if band_w > 3 or dim_std > 2.5 else ("中等" if band_w > 1.5 else "低")

    # ── OPTIONS STRATEGY ──────────────────────────────────────────────
    if iv_rank < 30:
        strategy = "Long Call / Bull Call Spread（IV偏低，期权买方成本合理）" if is_bull else "Long Put / Bear Put Spread（IV偏低，方向性买入优先）"
    elif iv_rank > 70:
        strategy = "Cash-Secured Put / Bull Put Spread（高IV，卖方收权利金）" if is_bull else "Bear Call Spread（高IV，卖出Call策略）"
    else:
        strategy = "Bull Call Debit Spread（中性IV，控制成本）" if is_bull else "Bear Put Debit Spread（中性IV，控Vega暴露）"
    if skew_f > 1.2 and is_bull:
        strategy += "；Put溢价偏高，做多优先选 Call Spread 降本"
    cat_window_note = f"⚠️ 距 {near_cat_name} 约 {near_cat_days} 天，催化剂窗口期优先价差策略控制 IV Crush 风险。" if near_cat else ""
    pin_note = f"（第四章 GEX Pin Strike {res1_s} 附近为做市商磁吸区，可作入场参考）" if pin else ""

    # ── PARAGRAPH 1: Core thesis + EV ────────────────────────────────
    p1 = (
        f'<p><strong>综合研判（结合第一章蜂群评分、第三章催化剂、第四章期权结构）：</strong>'
        f'蜂群评分 <strong>{score:.2f}/10</strong>，ML 7日信号 <strong>{ml7}%</strong>，'
        f'置信区间 [{cb_lo:.2f}–{cb_hi:.2f}]，信号不确定性 <strong>{uncertainty}</strong>（σ={dim_std:.1f}）。'
        f'{"市场政体 <strong>" + regime + "</strong>，" if regime else ""}'
        f'{"信号拥挤度偏高（α衰减=" + f"{decay_f:.2f}" + "），alpha 可能已被部分定价；" if decay_f < 0.85 else ""}'
        f'{"PEAD 财报后漂移偏向 <strong>" + pead_bias + "</strong>，" if pead_bias not in ("neutral","") else ""}'
        f'五情景加权期望收益 <strong><span style="color:{ev_color};">{ev:+.2f}%</span></strong>，'
        f'风险回报比约 <strong>{rr_str}</strong>。</p>'
    )

    # ── PARAGRAPH 2: Five-scenario probability table ──────────────────
    p2 = f"""<p><strong>五情景概率矩阵（数据驱动动态概率，非固定分档）：</strong></p>
<div style="overflow-x:auto;margin:8px 0 14px;">
<table style="width:100%;border-collapse:collapse;font-size:12px;">
<thead>
<tr style="background:var(--bg3);color:var(--text2);">
  <th style="padding:6px 10px;text-align:left;border-radius:4px 0 0 0;">情景</th>
  <th style="padding:6px 8px;text-align:center;">概率</th>
  <th style="padding:6px 8px;text-align:center;">预期收益</th>
  <th style="padding:6px 10px;text-align:left;border-radius:0 4px 0 0;">触发条件 &amp; 传导路径</th>
</tr>
</thead>
<tbody>
<tr style="border-bottom:1px solid var(--border);">
  <td style="padding:7px 10px;color:var(--green2);font-weight:700;">A · 强势多头</td>
  <td style="padding:7px 8px;text-align:center;font-weight:700;color:var(--green2);">{pa}%</td>
  <td style="padding:7px 8px;text-align:center;color:var(--green2);">{ret_a:+.1f}%</td>
  <td style="padding:7px 10px;color:var(--text2);font-size:11px;">{"PE " + str(_pe_a) + "x×EPS→$" + f"{_fwd_eps*_pe_a:.0f}" + " | " if _fwd_eps else ""}催化剂超预期 → 突破 {res1_s} Call Wall → gamma squeeze → 目标 {res2_s}</td>
</tr>
<tr style="border-bottom:1px solid var(--border);">
  <td style="padding:7px 10px;color:#4ade80;font-weight:600;">B · 温和看涨</td>
  <td style="padding:7px 8px;text-align:center;font-weight:700;color:#4ade80;">{pb}%</td>
  <td style="padding:7px 8px;text-align:center;color:#4ade80;">{ret_b:+.1f}%</td>
  <td style="padding:7px 10px;color:var(--text2);font-size:11px;">{"PE " + str(_pe_b) + "x×EPS→$" + f"{_fwd_eps*_pe_b:.0f}" + " | " if _fwd_eps else ""}基本面稳健 → {sup1_s}–{res1_s} 震荡偏多 → 分批减仓</td>
</tr>
<tr style="border-bottom:1px solid var(--border);">
  <td style="padding:7px 10px;color:var(--gold2);font-weight:600;">C · 区间震荡</td>
  <td style="padding:7px 8px;text-align:center;font-weight:700;color:var(--gold2);">{pc}%</td>
  <td style="padding:7px 8px;text-align:center;color:var(--gold2);">{ret_c:+.1f}%</td>
  <td style="padding:7px 10px;color:var(--text2);font-size:11px;">多空信号拉锯 → 成交量萎缩 → {res1_s} 阻力未突破，{sup1_s} 支撑未失守 → Iron Condor 策略受益</td>
</tr>
<tr style="border-bottom:1px solid var(--border);">
  <td style="padding:7px 10px;color:#f87171;font-weight:600;">D · 温和看跌</td>
  <td style="padding:7px 8px;text-align:center;font-weight:700;color:#f87171;">{pd}%</td>
  <td style="padding:7px 8px;text-align:center;color:#f87171;">{ret_d:+.1f}%</td>
  <td style="padding:7px 10px;color:var(--text2);font-size:11px;">{"PE " + str(_pe_d) + "x×EPS→$" + f"{_fwd_eps*_pe_d:.0f}" + " | " if _fwd_eps else ""}宏观逆风 → Put OI 堆积 → {sup1_s} 支撑承压 → 做市商对冲加剧下行</td>
</tr>
<tr>
  <td style="padding:7px 10px;color:var(--red2);font-weight:700;">E · 强势看跌</td>
  <td style="padding:7px 8px;text-align:center;font-weight:700;color:var(--red2);">{pe}%</td>
  <td style="padding:7px 8px;text-align:center;color:var(--red2);">{ret_e:+.1f}%</td>
  <td style="padding:7px 10px;color:var(--text2);font-size:11px;">{"PE " + str(_pe_e) + "x×EPS→$" + f"{_fwd_eps*_pe_e:.0f}" + " | " if _fwd_eps else ""}逆向信号兑现 → {sup1_s} 失守 → 止损盘触发 → 负Gamma区追空 → 目标 {sup2_s}</td>
</tr>
</tbody>
<tfoot>
<tr style="background:var(--bg3);">
  <td colspan="4" style="padding:6px 10px;font-size:11px;color:var(--text3);">
    加权期望值 EV = <strong style="color:{ev_color};">{ev:+.2f}%</strong> &nbsp;·&nbsp;
    风险回报比 ≈ <strong>{rr_str}</strong> &nbsp;·&nbsp;
    ML 7日胜率 <strong>{ml7}%</strong> &nbsp;·&nbsp;
    置信区间 [{cb_lo:.2f}–{cb_hi:.2f}]
  </td>
</tr>
</tfoot>
</table>
</div>"""

    # ── PARAGRAPH 3: If-Then Decision Tree ───────────────────────────
    p3 = f"""<div style="background:var(--bg3);border-radius:10px;padding:16px;margin:8px 0 14px;border:1px solid var(--border);">
  <div style="font-size:12px;font-weight:700;color:var(--text1);margin-bottom:10px;">🌳 操作决策树（结合第四章水位 + 第七章警戒线）</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px;line-height:1.9;">
    <div style="border-left:3px solid var(--green2);padding-left:10px;">
      <div style="font-weight:700;color:var(--green2);margin-bottom:4px;">▶ 看多执行路径</div>
      <div style="color:var(--text2);">如果 <strong>价格突破 {res1_s}</strong>（CH4 Call Wall）</div>
      <div style="color:var(--text3);margin-left:12px;">→ <strong style="color:var(--green2);">加仓至半仓</strong>，止损上移至 {sup1_s}</div>
      <div style="color:var(--text2);margin-top:4px;">如果 <strong>ML持续 &gt;65% 且评分 &gt;6.5</strong></div>
      <div style="color:var(--text3);margin-left:12px;">→ <strong style="color:var(--green2);">维持持仓</strong>，{sup1_s} 为硬止损</div>
      <div style="color:var(--text2);margin-top:4px;">如果 <strong>财报超预期 &gt;5%</strong></div>
      <div style="color:var(--text3);margin-left:12px;">→ <strong style="color:var(--green2);">突破加仓</strong>，目标 {res2_s}</div>
    </div>
    <div style="border-left:3px solid var(--red2);padding-left:10px;">
      <div style="font-weight:700;color:var(--red2);margin-bottom:4px;">▶ 止损/翻空路径</div>
      <div style="color:var(--text2);">如果 <strong>价格跌破 {sup1_s}</strong>（CH7 警戒线一）</div>
      <div style="color:var(--text3);margin-left:12px;">→ <strong style="color:var(--red2);">减仓 50%</strong>，等待企稳信号</div>
      <div style="color:var(--text2);margin-top:4px;">如果 <strong>蜂群评分连续 2 日 &lt; 4.0</strong></div>
      <div style="color:var(--text3);margin-left:12px;">→ <strong style="color:var(--red2);">清仓观望</strong>，等待信号收敛</div>
      <div style="color:var(--text2);margin-top:4px;">如果 <strong>IV Rank 急升至 80th+</strong>（CH4预警）</div>
      <div style="color:var(--text3);margin-left:12px;">→ <strong style="color:var(--red2);">期权多头平仓</strong>，改用价差控Vega</div>
    </div>
  </div>
</div>"""

    # ── PARAGRAPH 4: Options strategy matching ────────────────────────
    p4 = (
        f'<p><strong>期权策略匹配（基于第四章 IV Rank={iv_rank:.0f}%、Skew={ctx.get("iv_skew") or "N/A"}、IV-RV={iv_rv:+.1f}%）：</strong>'
        f'{strategy}。{cat_window_note}{pin_note}</p>'
    )

    # ── PARAGRAPH 5: Position Management ───────────────────────────────
    pm = ctx.get('position_management') or {}
    p5 = ""
    if pm:
        sl = pm.get('stop_loss', {})
        tp = pm.get('take_profit', {})
        oht = pm.get('optimal_holding_time', {})
        holding_days = oht.get('recommended_holding_days', 'N/A')
        holding_range = oht.get('holding_time_range', {})
        sl_cons = f"${sl.get('conservative', 0):.2f}" if sl.get('conservative') else 'N/A'
        sl_mod  = f"${sl.get('moderate', 0):.2f}" if sl.get('moderate') else 'N/A'
        sl_agg  = f"${sl.get('aggressive', 0):.2f}" if sl.get('aggressive') else 'N/A'

        tp_rows = ""
        for lvl_key in ['level_1', 'level_2', 'level_3']:
            lvl = tp.get(lvl_key, {})
            if lvl:
                tp_price = f"${lvl.get('price', 0):.2f}" if lvl.get('price') else 'N/A'
                tp_ratio = f"{int(lvl.get('sell_ratio', 0) * 100)}%"
                tp_reason = lvl.get('reason', '')
                tp_rows += (f'<tr style="border-bottom:1px solid var(--border);">'
                           f'<td style="padding:5px 8px;color:var(--green2);font-weight:600;">{tp_price}</td>'
                           f'<td style="padding:5px 8px;text-align:center;">{tp_ratio}</td>'
                           f'<td style="padding:5px 8px;color:var(--text3);font-size:11px;">{tp_reason}</td></tr>')

        p5 = f"""<div style="background:var(--bg3);border-radius:10px;padding:16px;margin:8px 0 14px;border:1px solid var(--border);">
  <div style="font-size:12px;font-weight:700;color:var(--text1);margin-bottom:10px;">📐 仓位管理 · 出场计划</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;font-size:12px;">
    <div>
      <div style="font-weight:600;color:var(--red2);margin-bottom:6px;">止损位（Stop Loss）</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <span style="background:rgba(239,68,68,0.1);padding:3px 8px;border-radius:4px;color:var(--red2);">保守 {sl_cons}</span>
        <span style="background:rgba(239,68,68,0.15);padding:3px 8px;border-radius:4px;color:var(--red2);font-weight:700;">中等 {sl_mod}</span>
        <span style="background:rgba(239,68,68,0.2);padding:3px 8px;border-radius:4px;color:var(--red2);">激进 {sl_agg}</span>
      </div>
      <div style="margin-top:8px;color:var(--text3);font-size:11px;">建议持仓 <strong style="color:var(--text1);">{holding_days}天</strong>（范围 {holding_range.get('minimum','?')}–{holding_range.get('maximum','?')} 天）</div>
    </div>
    <div>
      <div style="font-weight:600;color:var(--green2);margin-bottom:6px;">分批止盈（Take Profit）</div>
      <table style="width:100%;border-collapse:collapse;">
        <thead><tr style="color:var(--text3);font-size:10px;">
          <th style="padding:3px 8px;text-align:left;">目标价</th>
          <th style="padding:3px 8px;text-align:center;">减仓比例</th>
          <th style="padding:3px 8px;text-align:left;">理由</th>
        </tr></thead>
        <tbody>{tp_rows}</tbody>
      </table>
    </div>
  </div>
</div>"""

    # ── V1: 估值卡片 ──────────────────────────────────────────────────
    val_card = _build_valuation_card(ctx)

    return val_card + p1 + p2 + p3 + p4 + p5


def _build_risk_narrative(ctx: dict) -> str:
    """本地模式 fallback：输出推理层散文叙事（3段 <p>），不输出卡片 HTML（卡片已由 risk_items_html 渲染）"""
    ticker    = ctx.get('ticker', '该股')
    bear_sigs = ctx['bear_signals']
    bear_sc   = float(ctx.get('bear', {}).get('score', 5))
    skew      = ctx['iv_skew']
    skew_f    = _sf(skew)
    fg        = ctx.get('fg_score', 50)
    price     = ctx.get('price') or 0
    guard_det = ctx.get('guard', {}).get('details', {}) or {}
    consistency = guard_det.get('consistency', 0)
    if isinstance(consistency, float) and consistency < 1:
        consistency = int(consistency * 100)

    sup_list = (ctx.get('key_levels') or {}).get('support', [])
    s1_price = sup_list[0].get('strike') if sup_list else None
    pc = ctx.get('put_call_ratio')
    try:
        pc_f = float(pc)
    except (TypeError, ValueError):
        pc_f = None
    iv_pct = ctx.get('iv_percentile')
    iv_f = float(iv_pct) if iv_pct is not None else None
    cats = ctx.get('catalysts', [])
    nearest_cat = cats[0].get('event', '') if cats else ''

    # v0.10.0 新增数据
    regime = ctx.get('market_regime', {}).get('overall_regime', '未知')
    cycle  = ctx.get('cycle_context', {}).get('cycle_regime', 'normal')
    iv_rv  = ctx.get('iv_rv_spread', 0)
    crowding = ctx.get('signal_crowding', {}).get('alpha_decay_factor', 1.0)

    paras = []

    # P5: 内部人交易 + 做空利息（字段均为 dict）
    insider = ctx.get('insider_trades') or {}
    si_raw = ctx.get('short_interest')
    insider_si_parts = []
    if insider and isinstance(insider, dict):
        _ins_sent = insider.get('sentiment', '')
        _ins_bought = float(insider.get('dollar_bought', 0) or 0)
        _ins_sold = float(insider.get('dollar_sold', 0) or 0)
        _ins_filings = insider.get('filings', 0)
        if _ins_bought or _ins_sold or _ins_filings:
            _net = _ins_bought - _ins_sold
            net_label = '净买入' if _net > 0 else '净卖出'
            ins_col = 'var(--green2)' if _net > 0 else 'var(--red2)'
            _net_str = f"${abs(_net)/1e6:.1f}M" if abs(_net) >= 1e6 else f"${abs(_net):,.0f}"
            insider_si_parts.append(
                f'内部人 <span style="color:{ins_col};font-weight:700;">{net_label} {_net_str}</span>'
                f'（{_ins_filings}笔申报，倾向{_ins_sent}）')
    if si_raw is not None:
        try:
            if isinstance(si_raw, dict):
                si_f = float(si_raw.get('short_pct_float', 0) or 0)
                dtc = si_raw.get('days_to_cover')
            else:
                si_f = float(si_raw) if float(si_raw) > 1 else float(si_raw) * 100
                dtc = None
            si_col = 'var(--red2)' if si_f > 10 else ('var(--gold2)' if si_f > 5 else 'var(--text2)')
            si_label = '高空头压力' if si_f > 10 else ('中等空头' if si_f > 5 else '空头正常')
            dtc_note = f'，空头回补天数 {dtc:.1f}' if dtc else ''
            insider_si_parts.append(f'做空比率 <span style="color:{si_col};font-weight:700;">{si_f:.1f}%</span>（{si_label}{dtc_note}）')
        except (TypeError, ValueError):
            pass
    if insider_si_parts:
        paras.append('<p><strong>内部人 &amp; 空头信号：</strong>' + '；'.join(insider_si_parts) + '。（见第一章蜂群评分 BearBee 逆向维度）</p>')

    # ── 段落1：核心风险因果链 ──────────────────────────────────
    if fg and fg <= 25:
        threshold = f'收盘跌破 <strong>${s1_price:.0f}</strong>' if s1_price else 'VIX 继续上行'
        paras.append(
            f'<p>当前最大风险来自宏观层面的极度恐慌情绪。Fear & Greed 指数仅 <strong>{fg}</strong>，'
            f'处于深度恐惧区间，资金倾向无差别撤离高 Beta 标的。{ticker} 历史 Beta 约 1.8–2.5x，'
            f'意味着大盘每下跌 1%，{ticker} 可能承受 1.8–2.5% 的跌幅。'
            f'市场政体当前为 <span class="bear-text">{regime}</span>，'
            f'若与 FOMC 不确定性或月末再平衡窗口共振，冲击将成倍放大。'
            f'关键失效阈值：{threshold}，届时多头防线全面瓦解。</p>'
        )
    elif skew_f > 1.2:
        skew_detail = (f'P/C 比 {pc_f:.2f} 显示多空双方同时活跃，净方向存在较大不确定性'
                       if pc_f else 'Put 端溢价持续走高，下行对冲成本显著上升')
        paras.append(
            f'<p>期权市场发出结构性风险信号：IV Skew 达 <strong>{skew}</strong>，'
            f'表明机构正在为下行保护支付超额溢价。{skew_detail}。'
            f'IV-RV 价差为 <strong>{iv_rv:+.1f}%</strong>，'
            f'{"期权相对历史波动率偏贵，卖方占优" if iv_rv > 3 else "期权定价合理" if iv_rv > -3 else "期权偏便宜，方向性买入有统计优势"}。'
            f'失效阈值：Skew 持续高于 <strong>1.3</strong> 且 OI 继续向 Put 端累积，'
            f'则下行加速信号确认。</p>'
        )
    elif bear_sigs:
        top_sig = bear_sigs[0][:60]
        paras.append(
            f'<p>BearBee 逆向信号是当前首要风险来源，评分 <strong>{bear_sc:.1f}/10</strong>。'
            f'核心触发信号：{top_sig}。逆向信号触发后通常在 5–10 个交易日内出现价格压力，'
            f'而蜂群一致性仅 {consistency}%，多空分歧较大进一步放大了不确定性。'
            f'传导路径：逆向信号 → 短期情绪恶化 → 期权 IV 扩张 → Theta 损耗加速。'
            f'若信号持续 3 天以上未收敛，则当前多头假设需重新评估。</p>'
        )
    else:
        paras.append(
            f'<p>当前结构性风险处于可控区间。BearBee 评分 <strong>{bear_sc:.1f}/10</strong>，'
            f'未触发高优先级逆向警报。市场政体为 <strong>{regime}</strong>，'
            f'蜂群一致性 {consistency}%，信号噪声比尚可。'
            f'但需密切关注边际变化——低风险状态往往在突发事件面前迅速恶化，'
            f'尤其是当前 IV Skew {skew} 暗示部分机构已在悄然增加保护。</p>'
        )

    # ── 段落2：催化剂与波动率风险 ─────────────────────────────
    if nearest_cat:
        days = cats[0].get('days_until', 0) if cats else 0
        timing = f'{abs(days)} 天前触发' if days < 0 else f'{days} 天内' if days <= 7 else f'{days} 天后'
        iv_warn = (f'当前 IV Rank <strong>{iv_f:.0f}th</strong> 处于高位，'
                   f'期权买方需承担显著 Theta 损耗' if iv_f and iv_f > 60
                   else '波动率水平尚可，但催化剂窗口内可能急剧扩张')
        paras.append(
            f'<p>催化剂维度的核心风险点是 <strong>{nearest_cat}</strong>（{timing}）。'
            f'事件窗口内 IV 通常先扩张后骤降（IV Crush 幅度 -30%~-50%），'
            f'期权多头即使方向判断正确也可能因 Vega 损失而亏损。{iv_warn}。'
            f'时间周期层面，当前处于 <strong>{cycle}</strong> 阶段'
            f'{"，Opex 周的 Charm/Vanna 效应可能压制短期波动幅度" if ctx.get("cycle_context", {}).get("is_opex_week") else ""}。'
            f'建议：正股持有者穿越事件；期权多头等 Crush 后再建仓或使用价差策略控制 Vega 暴露。</p>'
        )
    elif iv_f and iv_f > 60:
        paras.append(
            f'<p>波动率层面需要警惕：IV Rank <strong>{iv_f:.0f}th</strong> 处于中高位，'
            f'IV-RV 价差 <strong>{iv_rv:+.1f}%</strong>'
            f'{"表明期权相对实际波动偏贵" if iv_rv > 3 else ""}。'
            f'期权买方在当前水平建仓将承受较高的 Theta 时间损耗，'
            f'失效阈值：IV Rank 升破 <strong>80th</strong> 后，卖方策略具备更强的统计优势。'
            f'时间周期当前为 <strong>{cycle}</strong>。</p>'
        )
    else:
        s1_note = (f'关键支撑 <strong>${s1_price:.0f}</strong>（距当前 {(s1_price - price) / price * 100:+.1f}%）'
                   f'同时是做市商 Delta 对冲触发点，跌穿将引发机械性卖压级联'
                   if s1_price and price else '当前无显著的单一支撑触发点，需综合监控多层位置')
        paras.append(
            f'<p>{s1_note}。市场政体 <strong>{regime}</strong>，'
            f'时间周期 <strong>{cycle}</strong>。'
            f'波动率当前处于适中水平，但需关注任何催化剂事件可能触发的 IV 急升。</p>'
        )

    # ── 段落3：信号拥挤度与明日警戒线 ────────────────────────
    warn_items = []
    if s1_price and price:
        warn_items.append(f'价格跌破 ${s1_price:.0f}（最强支撑）')
    if iv_f:
        thr = 70 if iv_f >= 60 else 60
        warn_items.append(f'IV Rank 升过 {thr}th（当前 {iv_f:.0f}th）')
    if pc_f and pc_f < 1.0:
        warn_items.append(f'P/C 比反转升过 1.2（当前 {pc_f:.2f}）')
    if fg and fg <= 30:
        warn_items.append(f'F&G 继续下行低于 15（当前 {fg}）')

    warn_str = '；'.join(warn_items) if warn_items else '维持现有风控计划'
    crowding_note = (f'值得注意的是，信号拥挤度衰减因子为 <strong>{crowding:.2f}</strong>，'
                     f'{"同向信号过多可能导致 alpha 衰减，需警惕拥挤交易反转" if crowding < 0.85 else "信号拥挤度在正常范围"}。'
                     ) if crowding != 1.0 else ''
    paras.append(
        f'<p>{crowding_note}'
        f'综合以上风险因子，明日需重点追踪的警戒线包括：{warn_str}。'
        f'任一阈值触发应收紧止损或降低仓位，等待蜂群信号重新收敛后再考虑加仓。'
        f'风险管理的核心原则：在不确定性扩大时缩减暴露，而非试图预测方向。</p>'
    )

    return '\n'.join(paras)


def _build_executive_summary(ctx: dict) -> str:
    """报告顶部 Executive Summary — 3-5句综合结论摘要"""
    score       = float(ctx.get("final_score") or 0)
    ticker      = ctx.get("ticker", "")
    direction   = ctx.get("direction", "neutral")
    direction_zh = ctx.get("direction_zh", "中性")
    price       = ctx.get("price")
    ml7         = ctx.get("ml_7d", 50)
    fg          = ctx.get("fg_score")
    regime      = (ctx.get("market_regime") or {}).get("overall_regime", "")
    bear_sigs   = ctx.get("bear_signals", [])
    decay_f     = float((ctx.get("signal_crowding") or {}).get("alpha_decay_factor", 1.0) or 1.0)
    cats        = ctx.get("catalysts", [])
    near_cat    = next((c for c in cats if 0 <= (c.get("days_until") or 99) <= 14), None)
    ress        = ctx.get("key_levels", {}).get("resistance", [])
    sups        = ctx.get("key_levels", {}).get("support", [])
    res1_s      = f"${ress[0]['strike']:.0f}" if ress else "N/A"
    sup1_s      = f"${sups[0]['strike']:.0f}" if sups else "N/A"

    # Confidence
    cb    = ctx.get("confidence_band") or [max(0, score - 1.5), min(10, score + 1.5)]
    cb_lo = float(cb[0]); cb_hi = float(cb[1])

    # Top dimension
    dim_scores = ctx.get("dimension_scores") or {}
    dim_zh_map = {"catalyst": "催化剂", "odds": "期权/赔率", "signal": "技术信号",
                  "sentiment": "市场情绪", "risk_adj": "风险调整"}
    top_dim = max(dim_scores, key=lambda k: float(dim_scores.get(k) or 0), default="") if dim_scores else ""
    top_dim_zh = dim_zh_map.get(top_dim, top_dim)

    score_col = "var(--green2)" if score >= 6 else ("var(--red2)" if score <= 4 else "var(--gold2)")
    border_col = "#10b981" if "bull" in direction else ("#ef4444" if "bear" in direction else "#f59e0b")

    # Verdict
    if score >= 6.5:
        verdict = f'信号偏向<strong style="color:var(--green2);">看涨</strong>'
        sup2_s = f"${sups[1]['strike']:.0f}" if len(sups) > 1 else "N/A"
        action  = f'目标阻力 {res1_s}，止损 {sup1_s}'
    elif score <= 3.5:
        verdict = f'信号偏向<strong style="color:var(--red2);">看跌</strong>'
        sup2_s = f"${sups[1]['strike']:.0f}" if len(sups) > 1 else "N/A"
        action  = f'关注 {sup1_s} 支撑，破位下看 {sup2_s}'
    else:
        verdict = f'信号处于<strong style="color:var(--gold2);">中性拉锯</strong>'
        action  = f'建议控制仓位，区间观望（{sup1_s}–{res1_s}）'

    cat_note      = f"距 <strong>{near_cat['event']}</strong> 约 {near_cat['days_until']} 天，" if near_cat else ""
    risk_note     = f"BearBee 逆向信号活跃，" if bear_sigs else ""
    crowding_note = f"信号拥挤度偏高（α={decay_f:.2f}），" if decay_f < 0.85 else ""
    regime_note   = f"宏观处于 {regime} 政体，" if regime else ""
    fg_note       = f"极度恐惧（F&G={fg}）压制上行弹性，" if (fg and fg <= 25) else ""

    price_str = f"${price:.2f}" if price else "N/A"

    # ── N2: Top-3 核心论点提炼 ──────────────────────────────────────
    _thesis_items = []
    # 期权信号
    _oracle_sc = float((ctx.get("oracle") or {}).get("score", 5) or 5)
    _iv_rank_v = float(ctx.get("iv_rank", 50) or 50)
    _pcr_v = ctx.get("put_call_ratio", 1.0)
    try: _pcr_v = float(_pcr_v)
    except (TypeError, ValueError): _pcr_v = 1.0
    if _oracle_sc >= 7:
        _thesis_items.append(("期权", abs(_oracle_sc - 5), f"期权结构强看涨（OracleBee {_oracle_sc:.1f}/10），IV Rank {_iv_rank_v:.0f}%，P/C {_pcr_v:.1f}"))
    elif _oracle_sc <= 3:
        _thesis_items.append(("期权", abs(_oracle_sc - 5), f"期权结构偏空（OracleBee {_oracle_sc:.1f}/10），Put 端保护需求旺盛"))
    # 估值信号
    _fwd_eps_s = float(ctx.get("forward_eps", 0) or 0)
    _pe_fwd_s = (float(price) / _fwd_eps_s) if (_fwd_eps_s > 0 and price) else None
    _peg_s = _pe_fwd_s / float(ctx.get("eps_growth", 1) or 1) if (_pe_fwd_s and float(ctx.get("eps_growth", 0) or 0) > 0) else None
    if _peg_s is not None and _peg_s < 0.5:
        _thesis_items.append(("估值", 3.0, f"Forward PE {_pe_fwd_s:.1f}x vs EPS Growth {ctx.get('eps_growth',0):.0f}% → PEG {_peg_s:.2f} 极度低估"))
    elif _peg_s is not None and _peg_s > 2.0:
        _thesis_items.append(("估值", 2.5, f"Forward PE {_pe_fwd_s:.1f}x vs EPS Growth {ctx.get('eps_growth',0):.0f}% → PEG {_peg_s:.2f} 偏贵"))
    # 催化剂
    if near_cat:
        _thesis_items.append(("催化剂", 2.0, f"距 {near_cat['event']} {near_cat['days_until']} 天，催化窗口期 IV Crush 机会"))
    # GEX
    _gex_reg = ctx.get("gex_regime", "")
    if _gex_reg in ("positive_gex", "positive_gamma"):
        _mp = ctx.get("max_pain")
        _mp_str = ""
        if isinstance(_mp, dict): _mp_str = f"Max Pain ${_mp.get('max_pain',0):.0f} 磁吸"
        elif isinstance(_mp, (int, float)): _mp_str = f"Max Pain ${_mp:.0f} 磁吸"
        _thesis_items.append(("GEX", 1.8, f"正 Gamma 政体 + {_mp_str} → 短期下行有底"))
    elif _gex_reg in ("negative_gex", "negative_gamma"):
        _thesis_items.append(("GEX", 2.2, f"负 Gamma 政体——做市商追涨杀跌放大波动"))
    # 逆向信号
    if bear_sigs:
        _bear_sc = float((ctx.get("bear") or {}).get("score", 5) or 5)
        _thesis_items.append(("逆向", abs(_bear_sc - 5), f"BearBee {_bear_sc:.1f}/10，{bear_sigs[0][:40]}"))
    # 情绪
    if fg and fg <= 25:
        _thesis_items.append(("情绪", 2.5, f"极度恐惧 F&G={fg}，系统性抛压风险高"))

    # 排序取 Top-3
    _thesis_items.sort(key=lambda x: x[1], reverse=True)
    _top3 = _thesis_items[:3]
    _thesis_html = ""
    if _top3:
        _thesis_lines = "".join(
            f'<div style="display:flex;gap:8px;align-items:baseline;margin:3px 0;">'
            f'<span style="background:var(--accent);color:white;font-size:10px;font-weight:700;'
            f'padding:1px 6px;border-radius:3px;white-space:nowrap;">{t[0]}</span>'
            f'<span style="font-size:12px;color:var(--text1);">{t[2]}</span></div>'
            for t in _top3
        )
        _thesis_html = (
            f'<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);">'
            f'<div style="font-size:10px;font-weight:700;color:var(--text3);margin-bottom:6px;">🎯 核心论点 Top-3</div>'
            f'{_thesis_lines}</div>'
        )

    odds_boost_html = _build_odds_boost_card(ctx)

    return f"""
<div id="exec-summary" style="background:linear-gradient(135deg,var(--bg2) 0%,var(--bg3) 100%);
     border-radius:12px;padding:18px 22px;margin-bottom:20px;
     border-left:4px solid {border_col};">
  <div style="font-size:10px;font-weight:700;letter-spacing:2px;color:var(--text3);
              text-transform:uppercase;margin-bottom:8px;">📋 Executive Summary</div>
  <div style="font-size:13px;line-height:1.8;color:var(--text1);">
    <strong>{ticker}</strong> 当前报告时价 <strong>{price_str}</strong>，
    蜂群综合评分 <strong style="color:{score_col};font-size:17px;">{score:.2f}</strong>/10，
    {verdict}，ML 7日胜率 <strong>{ml7}%</strong>，
    置信区间 [{cb_lo:.2f}–{cb_hi:.2f}]。
    {cat_note}{risk_note}{crowding_note}{regime_note}{fg_note}
    最强驱动维度：<strong>{top_dim_zh or "N/A"}</strong>。
    {action}。
  </div>
  {odds_boost_html}
  {_thesis_html}
</div>"""


def _local_fallback(ctx: dict, section: str) -> str:
    """当 LLM 不可用时的本地生成文本"""
    ticker = ctx["ticker"]
    score = ctx["final_score"]
    direction = ctx["direction_zh"]
    resonance = ctx["resonance"]
    res_dims = resonance.get("resonant_dimensions", [])

    fallbacks = {
        "exec_summary": _build_executive_summary(ctx),
        "swarm_analysis": _build_swarm_narrative(ctx),
        "resonance": _build_resonance_narrative(ctx),
        "catalyst": _build_catalyst_narrative(ctx),
        "options": _build_options_narrative(ctx),
        "macro": _build_macro_narrative(ctx),
        "scenario": _build_scenario_narrative(ctx),
        "risk": _build_risk_narrative(ctx),
    }
    return fallbacks.get(section, "<p>数据处理中...</p>")


# ── HTML 生成 ─────────────────────────────────────────────────────────────────

def _try_compute_gex(ctx: dict) -> None:
    """
    在报告生成阶段补算 Dealer GEX。
    当 JSON 中 dealer_gex 缺失或 total_gex=0 时（常见原因：采集时 yfinance 未返回价格），
    利用已知的 ctx['price'] 实时重新计算，更新 ctx 中的 GEX 相关字段。
    失败时静默跳过，不影响其他报告内容。
    """
    # 已有非零 GEX 数据 → 无需重算
    if float(ctx.get("gamma_exposure", 0) or 0) != 0:
        return

    price = ctx.get("price")
    if not price or float(price) <= 0:
        return

    ticker = ctx["ticker"]
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from advanced_analyzer import DealerGEXAnalyzer
        gex = DealerGEXAnalyzer()
        result = gex.analyze(ticker, float(price))
        if not result or float(result.get("total_gex", 0) or 0) == 0:
            return
        ctx["gamma_exposure"]     = result["total_gex"]
        ctx["gex_regime"]         = result.get("regime", "")
        ctx["gex_flip"]           = result.get("gex_flip")
        ctx["gex_call_wall"]      = result.get("largest_call_wall")
        ctx["gex_put_wall"]       = result.get("largest_put_wall")
        ctx["gamma_squeeze_risk"] = (
            "high"   if result.get("regime") == "negative_gex" else
            "low"    if result.get("regime") == "positive_gex" else "medium"
        )
        print(f"   🎯 GEX 实时补算: {result['total_gex']:+.2f}M$ | "
              f"regime={result.get('regime','')} | "
              f"flip=${result.get('gex_flip') or 0:.0f}")
    except Exception as _e:
        print(f"   ⚠️  GEX 补算跳过: {_e}")


def _try_charts(ctx: dict) -> tuple:
    """尝试生成5种图表，返回 (conf, opts, iv_term, gex_profile, deep_skew)。"""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from chart_engine import (render_confidence_chart, render_options_chart,
                                   render_iv_term_chart,
                                   render_gex_profile_chart,
                                   render_deep_skew_chart)
        ticker      = ctx["ticker"]
        report_date = ctx["report_date"]
        raw_data    = ctx.get("_raw_data")
        if raw_data is None:
            return "", "", "", "", ""
        price = float(ctx.get("price") or 0)

        _img_style = ('style="width:100%;border-radius:8px;margin-top:18px;'
                      'border:1px solid var(--border);"')

        def _wrap(b64, alt):
            return (f'<img src="data:image/png;base64,{b64}" {_img_style} alt="{alt}">'
                    if b64 else "")

        conf_b64        = render_confidence_chart(raw_data, ticker, report_date)
        opts_b64        = render_options_chart(raw_data, ticker, report_date, price)
        iv_term_b64     = render_iv_term_chart(raw_data, ticker, report_date)
        gex_profile_b64 = render_gex_profile_chart(raw_data, ticker, report_date, price)
        skew_b64        = render_deep_skew_chart(raw_data, ticker, report_date)

        return (_wrap(conf_b64, "置信区间图表"), _wrap(opts_b64, "期权水位图表"),
                _wrap(iv_term_b64, "IV期限结构图"),
                _wrap(gex_profile_b64, "GEX Profile图"),
                _wrap(skew_b64, "IV Skew Smile"))
    except Exception as _e:
        import traceback as _tb
        print(f"  ⚠️  chart_engine 跳过: {_e}")
        _tb.print_exc()
        return "", "", "", "", ""


def generate_html(ctx: dict, reasoning: dict, accuracy_html: str = "",
                  attribution_html: str = "") -> str:
    """组装完整的 Template C v3.0 HTML 报告"""
    ticker = ctx["ticker"]
    report_date = ctx["report_date"]
    score = ctx["final_score"]
    direction = ctx["direction"]
    direction_zh_str = ctx["direction_zh"]
    res = ctx["resonance"]
    price = ctx.get("price")
    price_str = f"${price:.2f}" if price else "N/A"
    price_label = "收市价" if ctx.get("price_is_close") else "扫描时价格"

    # ── 图表生成（可选，matplotlib 未安装时静默跳过）────────────────────────────
    _conf_chart_html, _opts_chart_html, _iv_term_chart_html, _gex_profile_chart_html, _skew_chart_html = _try_charts(ctx)

    # ── Executive Summary ──────────────────────────────────────────────────────
    exec_summary_html = _build_executive_summary(ctx)

    # ── Day-over-Day Delta Widget (P5) ─────────────────────────────────────────
    _prev = ctx.get("prev")
    dod_delta_html = ""
    if _prev:
        _dod_parts = []
        # Score delta
        try:
            _score_diff = score - float(_prev.get("final_score") or score)
            if abs(_score_diff) >= 0.1:
                _arrow = "↑" if _score_diff > 0 else "↓"
                _col   = "#10b981" if _score_diff > 0 else "#ef4444"
                _dod_parts.append(f'综合评分 <span style="color:{_col};font-weight:700;">{_arrow}{abs(_score_diff):.2f}</span>')
        except Exception:
            pass
        # IV delta
        try:
            _iv_now  = float(ctx.get("iv_current") or 0)
            _iv_prev = float(_prev.get("iv_current") or 0)
            if _iv_now and _iv_prev and abs(_iv_now - _iv_prev) >= 0.5:
                _d = _iv_now - _iv_prev
                _col = "#ef4444" if _d > 0 else "#10b981"
                _dod_parts.append(f'IV <span style="color:{_col};font-weight:700;">{_d:+.1f}%</span>')
        except Exception:
            pass
        # P/C delta
        try:
            _pc_now  = float(ctx.get("put_call_ratio") or 0)
            _pc_prev = float(_prev.get("put_call_ratio") or 0)
            if _pc_now and _pc_prev and abs(_pc_now - _pc_prev) >= 0.05:
                _d = _pc_now - _pc_prev
                _col = "#ef4444" if _d > 0 else "#10b981"
                _dod_parts.append(f'P/C比 <span style="color:{_col};font-weight:700;">{_d:+.2f}</span>')
        except Exception:
            pass
        # Regime change
        try:
            _rg_now  = (ctx.get("market_regime") or {}).get("overall_regime", "")
            _rg_prev = (_prev.get("market_regime") or {}).get("overall_regime", "")
            if _rg_now and _rg_prev and _rg_now != _rg_prev:
                _dod_parts.append(f'政体 <span style="color:#f59e0b;font-weight:700;">{_rg_prev}→{_rg_now}</span>')
        except Exception:
            pass
        if _dod_parts:
            dod_delta_html = (
                f'<div style="background:var(--bg3);border-radius:8px;padding:8px 14px;'
                f'margin-bottom:12px;font-size:12px;color:var(--text2);'
                f'border:1px solid var(--border);display:flex;align-items:center;gap:6px;flex-wrap:wrap;">'
                f'<span style="font-weight:700;color:var(--text3);margin-right:4px;">📅 日环比变化：</span>'
                + " &nbsp;·&nbsp; ".join(_dod_parts) +
                f'</div>'
            )

    res_detected = res.get("resonance_detected", False)
    res_dims = res.get("resonant_dimensions", [])
    res_boost = res.get("confidence_boost", 0)
    res_count = res.get("cross_dim_count", 0)
    res_label = f"{res_count}维共振" if res_detected else "无共振"

    # 各蜂评分（name, score, dim_label, prev_key）
    agents = [
        ("ChronosBee", ctx["chronos"].get("score", 0), "催化剂",  "chronos"),
        ("RivalBee",   ctx["rival"].get("score", 0),   "ML辅助",  "rival"),
        ("OracleBee",  ctx["oracle"].get("score", 0),  "期权/赔率","oracle"),
        ("BuzzBee",    ctx["buzz"].get("score", 0),    "消息/舆情","buzz"),
        ("ScoutBee",   ctx["scout"].get("score", 0),   "基本面",  "scout"),
        ("GuardBee",   ctx["guard"].get("score", 0),   "宏观/情绪","guard"),
        ("BearBee",    ctx["bear"].get("score", 0),    "逆向",    "bear"),
    ]

    # 蜂群评分卡 HTML（含昨日 Δ 箭头）
    _prev = ctx.get("prev")
    score_cards = ""
    for name, val, dim, pkey in agents:
        cls = score_class(val)
        # 计算与昨日的差值
        delta_html = ""
        if _prev:
            try:
                prev_val = float(_prev.get(pkey, 0) or 0)
                cur_val  = float(val or 0)
                diff = cur_val - prev_val
                if abs(diff) >= 0.1:
                    arrow = "↑" if diff > 0 else "↓"
                    d_color = "#10b981" if diff > 0 else "#ef4444"
                    delta_html = (f'<div class="delta" style="font-size:10px;color:{d_color};'
                                  f'margin-top:2px;">{arrow}{abs(diff):.1f}</div>')
            except Exception:
                pass
        score_cards += f"""
        <div class="score-card {cls}">
          <div class="bee">{name}</div>
          <div class="val">{fmt_score(val)}</div>
          <div class="dim">{dim}</div>{delta_html}
        </div>"""

    # 蜂群投票统计 + 综合评分卡
    try:
        agent_votes_bull = sum(1 for n, v, d, pk in agents if v and float(v) >= 6.5)
        agent_votes_bear = sum(1 for n, v, d, pk in agents if v and float(v) <= 3.5)
        agent_votes_neut = len(agents) - agent_votes_bull - agent_votes_bear
    except Exception:
        agent_votes_bull = agent_votes_neut = agent_votes_bear = 0
    score_color = "var(--green2)" if "bull" in direction else ("var(--red2)" if "bear" in direction else "var(--gold2)")

    # ⑤ T-7 预测闭环 widget
    t7_widget_html = ""
    _t7 = ctx.get("t7")
    _cur_price = ctx.get("price")
    if _t7 and _t7.get("price") and _cur_price:
        try:
            t7_price   = float(_t7["price"])
            cur_price  = float(_cur_price)
            t7_dir     = _t7.get("direction", "neutral")
            pct_change = (cur_price - t7_price) / t7_price * 100
            actual_dir = "bull" if pct_change >= 1.0 else ("bear" if pct_change <= -1.0 else "neutral")
            # 严格命中：预判方向必须与实际方向完全一致
            is_correct = (t7_dir == actual_dir)
            # T-7方向中文 & 颜色（基于T-7预判，而非今日）
            t7_dir_zh   = {"bull":"看多","bear":"看空","neutral":"中性"}.get(t7_dir, t7_dir)
            t7_dir_color= {"bull":"#10b981","bear":"#ef4444","neutral":"#f59e0b"}.get(t7_dir, "var(--text2)")
            chg_color   = "#10b981" if pct_change >= 0 else "#ef4444"
            result_icon = "✅" if is_correct else "❌"
            result_txt  = "方向命中" if is_correct else "方向未中"
            result_color= "#10b981" if is_correct else "#ef4444"
            t7_widget_html = (
                f'<div class="t7-verify" style="margin-top:10px;padding:8px 12px;'
                f'background:var(--bg3);border-radius:8px;border:1px solid var(--border);'
                f'font-size:11px;color:var(--text3);display:flex;align-items:center;gap:10px;">'
                f'<span style="font-weight:600;color:var(--text2);">T-7 回测</span>'
                f'<span>7日前预判: <strong style="color:{t7_dir_color};">{t7_dir_zh}</strong></span>'
                f'<span>实际涨跌: <strong style="color:{chg_color};">{pct_change:+.1f}%</strong></span>'
                f'<span style="font-weight:700;color:{result_color};">{result_icon} {result_txt}</span>'
                f'</div>'
            )
        except Exception:
            pass

    # ── 置信区间 widget ──────────────────────────────────────────
    _cb   = ctx.get("confidence_band")
    _bw_raw = ctx.get("band_width")
    _bw     = float(_bw_raw) if _bw_raw is not None else None   # BUG-H: may be None
    _bw_str = f"{_bw:.2f}" if _bw is not None else "N/A"        # BUG-H: safe string
    _disc = ctx.get("discrimination", "")
    _dstd = ctx.get("dimension_std")
    if _cb and len(_cb) == 2:
        _cb_lo, _cb_hi = float(_cb[0]), float(_cb[1])
        # 把 [0,10] 区间映射到 100% 宽度
        _bar_lo  = _cb_lo / 10 * 100
        _bar_w   = (_cb_hi - _cb_lo) / 10 * 100
        _mark_x  = score / 10 * 100
        # 分散度标签
        _std_label = (f"⚠️ 极高分散 ({_dstd:.1f})" if _dstd and float(_dstd) >= 2.5
                      else (f"中等分散 ({_dstd:.1f})" if _dstd and float(_dstd) >= 1.5
                            else (f"低分散 ({_dstd:.1f})" if _dstd else "")))
        _std_color = ("var(--red2)" if _dstd and float(_dstd) >= 2.5
                      else ("var(--gold2)" if _dstd and float(_dstd) >= 1.5 else "var(--green2)"))
        _disc_label = {"low":"低区分度","medium":"中区分度","high":"高区分度"}.get(_disc, _disc)
        confidence_band_html = f"""
      <div style="margin-top:10px;padding:8px 10px;background:var(--bg2);border-radius:8px;border:1px solid var(--border);">
        <div style="font-size:11px;color:var(--text2);margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;">
          <span>📏 置信区间 <strong style="color:var(--text1);">[{_cb_lo:.2f} – {_cb_hi:.2f}]</strong>（宽度 {_bw_str}，{_disc_label}）</span>
          <span style="color:{_std_color};font-weight:700;">{_std_label}</span>
        </div>
        <div style="position:relative;height:8px;background:var(--bg3);border-radius:4px;overflow:visible;">
          <div style="position:absolute;left:{_bar_lo:.1f}%;width:{_bar_w:.1f}%;height:100%;background:rgba(139,92,246,0.35);border-radius:4px;"></div>
          <div style="position:absolute;left:{_mark_x:.1f}%;transform:translateX(-50%);top:-2px;width:3px;height:12px;background:{score_color};border-radius:2px;" title="当前评分 {score:.2f}"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text3);margin-top:3px;">
          <span>0</span><span style="color:{score_color};font-weight:600;">▲ {score:.2f}</span><span>10</span>
        </div>
      </div>"""
    else:
        confidence_band_html = ""

    score_summary_html = f"""<div class="score-summary">
      <div class="score-summary-row">
        <div class="final-score-big" style="color:{score_color};">{score:.2f}</div>
        <span class="stat-pill bull-pill">看多 {agent_votes_bull}</span>
        <span class="stat-pill neut-pill">中性 {agent_votes_neut}</span>
        <span class="stat-pill bear-pill">看空 {agent_votes_bear}</span>
      </div>{t7_widget_html}{confidence_band_html}
    </div>"""

    # 条形图 HTML
    bar_rows = ""
    for name, val, _, _pk in sorted(agents, key=lambda x: -float(x[1]) if x[1] else 0):
        try:
            pct = min(100, max(0, float(val) * 10))
        except Exception:
            pct = 0
        cls = score_class(val)
        color_map = {"bull": "var(--green2)", "bear": "var(--red2)", "neutral": "var(--gold2)"}
        color = color_map.get(cls, "var(--gold2)")
        bar_rows += f"""
        <div class="bar-row">
          <span class="bar-label">{name.replace('Bee','')}</span>
          <div class="bar-track"><div class="bar-fill" style="width:{pct:.0f}%;background:{color};"></div></div>
          <span class="bar-val" style="color:{color}">{fmt_score(val)}</span>
        </div>"""

    # 共振徽章
    res_dims_html = "".join([f'<span class="resonance-dim">{d}</span>' for d in res_dims])
    if not res_dims_html:
        res_dims_html = '<span class="resonance-dim">无共振维度</span>'

    # 催化剂时间线
    dot_class_map = {"high": "dot-high", "critical": "dot-critical", "medium": "dot-medium"}

    # 智能重要性升级：JSON 数据常把关键事件标为 medium，根据关键词+天数本地修正
    _CRITICAL_KW = ['财报', 'earnings', 'result', 'revenue', 'q4', 'q3', 'q2', 'q1']
    _HIGH_KW     = ['gtc', 'conference', 'keynote', '大会', '发布会', '会议',
                    'fed', 'fomc', 'cpi', 'opex', 'gdp', 'nonfarm', '非农']
    def smart_importance(ev_name: str, days_until: int, original: str) -> str:
        ev_l = ev_name.lower()
        if days_until is not None and 0 <= days_until <= 10:
            if any(kw in ev_l for kw in _CRITICAL_KW):
                return "critical"
        if days_until is not None and 0 <= days_until <= 14:
            if any(kw in ev_l for kw in _HIGH_KW):
                return "high"
        return original

    timeline_html = ""
    today_str = str(date.today())
    for c in ctx["catalysts"]:
        days = c.get("days_until", 0)
        ev = c.get("event", "催化剂事件")
        ev_date = c.get("date", "")
        importance = c.get("importance") or c.get("severity", "medium")
        # 智能升级
        eff_importance = smart_importance(ev, days, str(importance).lower())
        dot_cls = dot_class_map.get(eff_importance, "dot-medium")
        # timeline-item 附加 CSS 类
        is_past = days is not None and days < 0
        item_extra = " item-past" if is_past else (f" item-{eff_importance}" if eff_importance in ("high", "critical") else "")
        if days == 0:
            label = f"今日 · {ev_date}"
        elif is_past:
            label = f"已过 {abs(days)} 天 · {ev_date}"
        else:
            label = f"T+{days} · {ev_date}"
        desc = c.get("description", "")
        timeline_html += f"""
        <div class="timeline-item{item_extra}">
          <div class="timeline-dot {dot_cls}"></div>
          <div class="timeline-label">{label}</div>
          <div class="timeline-event">{ev}</div>
          {'<div class="timeline-meta">' + desc + '</div>' if desc else ''}
        </div>"""

    # 期权关键位
    supports = ctx["key_levels"].get("support", [])
    resistances = ctx["key_levels"].get("resistance", [])

    support_rows = ""
    for s in supports[:4]:
        oi = s.get("oi", 0)
        iv = s.get("iv", 0)
        support_rows += f"""
        <div class="level-row">
          <span class="level-strike">${s['strike']:.0f}</span>
          <span class="level-meta">OI {oi:,.0f} · IV {iv*100:.0f}%</span>
        </div>"""

    resistance_rows = ""
    for r in resistances[:4]:
        oi = r.get("oi", 0)
        iv = r.get("iv", 0)
        resistance_rows += f"""
        <div class="level-row">
          <span class="level-strike">${r['strike']:.0f}</span>
          <span class="level-meta">OI {oi:,.0f} · IV {iv*100:.0f}%</span>
        </div>"""

    # 异常流表格 + unusual-list
    flow_rows = ""
    unusual_items_html = ""
    for u in ctx["unusual_activity"][:8]:
        u_type = u.get("type", "")
        strike = u.get("strike", 0)
        volume = u.get("volume", 0)
        bullish = u.get("bullish", False)
        is_call = bullish or "call" in u_type.lower()
        direction_word = "CALL" if is_call else "PUT"
        cls_word = "flow-call" if is_call else "flow-put"
        signal_desc = ("看涨" if bullish else "看跌") + (" Sweep" if "sweep" in u_type.lower() else " 大量")
        ratio = u.get("ratio", "")
        ratio_str = f" · 量OI比{ratio:.1f}x" if ratio else ""
        flow_rows += f"""
        <tr>
          <td class="{cls_word}">{direction_word}</td>
          <td>${strike:.0f}</td>
          <td>{volume:,.0f}</td>
          <td style="font-size:11px;color:var(--text3)">{signal_desc}{ratio_str}</td>
        </tr>"""
        item_cls = "bull-item" if is_call else "bear-item"
        val_color = "var(--green2)" if is_call else "var(--red2)"
        unusual_items_html += f"""
        <div class="unusual-item {item_cls}">
          <strong style="color:{val_color};">{direction_word} ${strike:.0f}</strong>
          <span style="font-size:11px;color:var(--text2);"> · {volume:,.0f} 手 · {signal_desc}{ratio_str}</span>
        </div>"""
    if not unusual_items_html:
        unusual_items_html = '<div style="color:var(--text3);font-size:12px;">暂无异常期权流数据</div>'

    # 到期日标签行（在异常流列表标题旁展示）
    exp_dates = ctx.get("expiration_dates", [])
    if exp_dates:
        exp_tags = "".join(
            f'<span style="background:var(--bg4);border:1px solid var(--border);border-radius:4px;'
            f'font-size:10px;font-weight:600;padding:2px 7px;color:var(--text2);">{d}</span>'
            for d in exp_dates[:4]
        )
        expiry_row_html = f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">' \
                          f'<span style="font-size:10px;color:var(--text3);align-self:center;">到期日：</span>' \
                          f'{exp_tags}</div>'
    else:
        expiry_row_html = ""

    # ── IV 期限结构卡片（CH4 · OracleBee S15）─────────────────────────────────
    _ivts       = ctx.get("iv_term_structure", {}) or {}
    _ivts_shape = _ivts.get("shape", "unknown")
    if _ivts_shape not in ("contango", "backwardation", "flat"):
        iv_term_html = ""
    else:
        _shape_labels = {
            "contango":      "正向 Contango",
            "backwardation": "倒挂 Backwardation",
            "flat":          "平坦 Flat",
        }
        _shape_colors = {
            "contango":      ("var(--green2)", "rgba(16,185,129,0.10)", "rgba(16,185,129,0.35)"),
            "backwardation": ("var(--red2)",   "rgba(239,68,68,0.10)",  "rgba(239,68,68,0.35)"),
            "flat":          ("var(--gold2)",  "rgba(245,158,11,0.10)", "rgba(245,158,11,0.35)"),
        }
        _sc, _sbg, _sborder = _shape_colors[_ivts_shape]
        _spread   = _ivts.get("iv_spread")
        _signal   = _ivts.get("signal", "")
        _pts      = _ivts.get("term_structure", [])

        # 每个到期点：ATM IV + DTE + 月日
        _pts_html = ""
        for _i, _pt in enumerate(_pts):
            _connector = (
                f'<div style="font-size:16px;color:var(--text3);align-self:center;padding:0 4px;">→</div>'
                if _i < len(_pts) - 1 else ""
            )
            _pts_html += (
                f'<div style="text-align:center;min-width:54px;">'
                f'<div style="font-size:14px;font-weight:700;color:{_sc};">{_pt["atm_iv"]:.1f}%</div>'
                f'<div style="font-size:10px;color:var(--text3);">{_pt["dte"]}天</div>'
                f'<div style="font-size:10px;color:var(--text4);">{_pt["expiry"][5:]}</div>'
                f'</div>{_connector}'
            )

        _spread_str = f"{_spread:+.1f}pp" if _spread is not None else "N/A"
        iv_term_html = (
            f'<div style="margin-bottom:16px;padding:12px 16px;'
            f'background:{_sbg};border:1px solid {_sborder};border-radius:8px;">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">'
            f'<span style="font-size:11px;font-weight:700;color:var(--text2);">📐 IV 期限结构</span>'
            f'<span style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;'
            f'background:{_sborder};color:{_sc};">{_shape_labels[_ivts_shape]}</span>'
            f'<span style="font-size:11px;color:var(--text3);margin-left:auto;">前后利差 {_spread_str}</span>'
            f'</div>'
            f'<div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin-bottom:8px;">'
            f'{_pts_html}'
            f'</div>'
            f'<div style="font-size:11px;color:var(--text2);line-height:1.5;">{_signal}</div>'
            f'</div>'
        )

    # ── GEX 增强卡片（Flip Acceleration + Vanna Stress）──────────────────────
    _gex_enhance_html = ""
    _flip_acc = ctx.get("flip_acceleration", {})
    _vanna_st = ctx.get("vanna_stress", {})
    _gex_norm = ctx.get("gex_normalized_pct")
    _gex_enh_parts = []

    if _flip_acc and _flip_acc.get("urgency"):
        _fa_urgency = _flip_acc.get("urgency", "low")
        _fa_slope = _flip_acc.get("acceleration", 0)
        _fa_colors = {"critical": "var(--red2)", "high": "#f97316", "medium": "var(--gold2)", "low": "var(--green2)"}
        _fa_labels = {"critical": "极高", "high": "高", "medium": "中", "low": "低"}
        _fa_c = _fa_colors.get(_fa_urgency, "var(--text3)")
        _gex_enh_parts.append(
            f'<div class="opt-card oc-neut">'
            f'<div class="oc-label">GEX 翻转加速度</div>'
            f'<div class="oc-val" style="color:{_fa_c};font-size:14px;">{_fa_labels.get(_fa_urgency, "N/A")}</div>'
            f'<div style="font-size:10px;color:var(--text3);margin-top:2px;">斜率 {_fa_slope:+.4f}</div>'
            f'<div class="oc-sub">{"⚠️ 翻转迫近" if _fa_urgency in ("critical","high") else "翻转压力" + _fa_labels.get(_fa_urgency, "")}</div>'
            f'</div>'
        )

    if _vanna_st and _vanna_st.get("total_vanna_gex_shift"):
        _vs_shift = _vanna_st.get("total_vanna_gex_shift", 0)
        _vs_flip = _vanna_st.get("can_flip_gex", False)
        _vs_shock = _vanna_st.get("vol_shock", 0.05)
        _vs_c = "var(--red2)" if _vs_flip else "var(--green2)"
        _gex_enh_parts.append(
            f'<div class="opt-card oc-neut">'
            f'<div class="oc-label">Vanna 压力测试</div>'
            f'<div class="oc-val" style="color:{_vs_c};font-size:14px;">{_vs_shift:+.2f}M$</div>'
            f'<div style="font-size:10px;color:var(--text3);margin-top:2px;">IV+{_vs_shock*100:.0f}% 冲击</div>'
            f'<div class="oc-sub">{"⚠️ 可翻转GEX" if _vs_flip else "GEX 稳定"}</div>'
            f'</div>'
        )

    if _gex_norm is not None:
        _gn_c = "var(--gold2)"
        _gex_enh_parts.append(
            f'<div class="opt-card oc-neut">'
            f'<div class="oc-label">GEX / OI 归一化</div>'
            f'<div class="oc-val" style="color:{_gn_c};font-size:14px;">{_gex_norm:.2f}%</div>'
            f'<div class="oc-sub">跨标的可比</div>'
            f'</div>'
        )

    if _gex_enh_parts:
        _gex_enhance_html = (
            '<div class="opt-grid" style="margin-bottom:14px;">'
            + "".join(_gex_enh_parts)
            + '</div>'
        )

    # ── Vol Surface + Skew Alerts（来自 main() 阶段预计算）──────────────────
    _vol_surface_html = ctx.get("vol_surface_html", "")
    _skew_alerts_html = ctx.get("skew_alerts_html", "")

    # F&G 显示
    fg = ctx.get("fg_score")
    if fg is not None:
        if fg <= 20:
            fg_color = "var(--red2)"
            fg_label = "极度恐慌"
        elif fg <= 40:
            fg_color = "var(--red)"
            fg_label = "恐慌"
        elif fg <= 60:
            fg_color = "var(--gold2)"
            fg_label = "中性"
        elif fg <= 80:
            fg_color = "var(--green)"
            fg_label = "贪婪"
        else:
            fg_color = "var(--green2)"
            fg_label = "极度贪婪"
    else:
        fg = "N/A"
        fg_color = "var(--text3)"
        fg_label = "未知"

    # 期权流方向颜色
    flow_dir = ctx.get("flow_direction", "neutral")
    flow_badge = "badge-bull" if flow_dir == "bullish" else ("badge-bear" if flow_dir == "bearish" else "badge-neutral")

    # 宏观小卡 GuardBee共振
    guard_res = ctx["guard"].get("details", {}).get("resonance", {})
    guard_res_detected = guard_res.get("resonance_detected", False)
    guard_res_boost = guard_res.get("confidence_boost", 0)

    # 总 OI 显示（万 = ×10,000；不使用"百万"，避免单位混淆）
    total_oi = ctx.get("total_oi", 0)
    if total_oi >= 100_000_000:
        oi_str = f"{total_oi/100_000_000:.1f}亿"
    elif total_oi >= 10_000:
        oi_str = f"{total_oi/10_000:.0f}万"
    else:
        oi_str = f"{total_oi:,}"

    # OI 日环比 Delta 子文本
    _oi_delta     = ctx.get("oi_delta")
    _oi_delta_pct = ctx.get("oi_delta_pct")
    if _oi_delta is not None and _oi_delta_pct is not None:
        _oi_arrow = "▲" if _oi_delta > 0 else ("▼" if _oi_delta < 0 else "─")
        _oi_d_color = "var(--green2)" if _oi_delta > 0 else ("var(--red2)" if _oi_delta < 0 else "var(--text3)")
        _oi_delta_abs = abs(_oi_delta)
        if _oi_delta_abs >= 10_000:
            _oi_d_str = f"{_oi_delta_abs/10_000:.1f}万"
        else:
            _oi_d_str = f"{_oi_delta_abs:,}"
        oi_delta_sub = (
            f'未平仓合约 <span style="color:{_oi_d_color}">'
            f'{_oi_arrow}{_oi_d_str}({_oi_delta_pct:+.1f}%日环比)</span>'
        )
    else:
        oi_delta_sub = "未平仓合约"

    # 异常流数量统计
    bullish_unusual = sum(1 for u in ctx["unusual_activity"] if u.get("bullish"))
    bearish_unusual = len(ctx["unusual_activity"]) - bullish_unusual

    # 头部 verdict
    verdict_color = "#10b981" if "bull" in direction else ("#ef4444" if "bear" in direction else "#f59e0b")
    verdict_bg = f"rgba({'16,185,129' if 'bull' in direction else ('239,68,68' if 'bear' in direction else '245,158,11')},0.15)"
    verdict_border = f"rgba({'16,185,129' if 'bull' in direction else ('239,68,68' if 'bear' in direction else '245,158,11')},0.35)"

    # ── 情景推演：优先 LLM 数据，降级到 ML 规则引擎 ─────────────────
    _llm_sc = ctx.get("llm_scenario", {})
    _use_llm_sc = bool(_llm_sc and _llm_sc.get("sc_a"))

    if ctx.get("price"):
        p = float(ctx["price"])
        max_res_price = resistances[0]["strike"] if resistances else p * 1.10
        max_sup_price = supports[0]["strike"] if supports else p * 0.90

        if _use_llm_sc:
            # ── LLM 情景数据（float() 保护防止 JSON 返回字符串） ──
            sc_a_lo = float(_llm_sc["sc_a"]["price_lo"]); sc_a_hi = float(_llm_sc["sc_a"]["price_hi"])
            sc_b_lo = float(_llm_sc["sc_b"]["price_lo"]); sc_b_hi = float(_llm_sc["sc_b"]["price_hi"])
            sc_c_lo = float(_llm_sc["sc_c"]["price_lo"]); sc_c_hi = float(_llm_sc["sc_c"]["price_hi"])
            sc_d_lo = float(_llm_sc["sc_d"]["price_lo"]); sc_d_hi = float(_llm_sc["sc_d"]["price_hi"])
            probs = [float(_llm_sc["sc_a"]["prob"]), float(_llm_sc["sc_b"]["prob"]),
                     float(_llm_sc["sc_c"]["prob"]), float(_llm_sc["sc_d"]["prob"])]
            ev_pct = float(_llm_sc.get("ev_pct") or 0)
            ev_str = f"{ev_pct:+.1f}%"
        else:
            # ── ML 规则引擎降级 ──
            sc_a_lo = round(max_res_price * 1.02, 0); sc_a_hi = round(max_res_price * 1.12, 0)
            sc_b_lo = round(p * 1.03, 0);             sc_b_hi = round(max_res_price * 0.99, 0)
            sc_c_lo = round(max_sup_price * 0.99, 0); sc_c_hi = round(p * 0.98, 0)
            sc_d_lo = round(max_sup_price * 0.85, 0); sc_d_hi = round(max_sup_price * 0.95, 0)
            if "bull" in direction:
                probs = [0.25, 0.35, 0.28, 0.12]
                returns = [+0.15, +0.05, -0.07, -0.18]
            elif "bear" in direction:
                probs = [0.12, 0.28, 0.35, 0.25]
                returns = [+0.15, +0.05, -0.07, -0.18]
            else:
                probs = [0.20, 0.30, 0.30, 0.20]
                returns = [+0.12, +0.04, -0.06, -0.15]
            ev_pct = sum(pr * r for pr, r in zip(probs, returns)) * 100
            ev_str = f"{ev_pct:+.1f}%"

        # 情景卡片 note（LLM 提供则用，否则用默认文案）
        sc_a_note = _llm_sc["sc_a"].get("note", f"{res_label}触发，做市商 Delta 对冲加速上涨。") if _use_llm_sc else f"{res_label}触发，阻力位被突破后做市商Delta对冲形成加速上涨。"
        sc_b_note = _llm_sc["sc_b"].get("note", f"期权流{ctx['flow_direction']}，近端支撑稳固，温和上涨定价合理。") if _use_llm_sc else f"期权流{ctx['flow_direction']}，近端支撑稳固，温和上涨定价合理。"
        _sc_c_oi = (f"{supports[0]['oi']:,.0f}") if supports else 'N/A'
        sc_c_note = _llm_sc["sc_c"].get("note", f"OI {_sc_c_oi} 提供缓冲，量能萎缩限制下行弹性。") if _use_llm_sc else f"OI {_sc_c_oi} 提供缓冲，量能萎缩限制下行弹性。"
        sc_d_note = _llm_sc["sc_d"].get("note", f"风险：{', '.join(ctx['bear_signals'][:2]) or 'IV Skew偏高, 宏观不确定'}。深部支撑防线。") if _use_llm_sc else f"风险：{', '.join(ctx['bear_signals'][:2]) or 'IV Skew偏高, 宏观不确定'}。深部支撑防线。"
    else:
        sc_a_lo, sc_a_hi = 0, 0
        sc_b_lo, sc_b_hi = 0, 0
        sc_c_lo, sc_c_hi = 0, 0
        sc_d_lo, sc_d_hi = 0, 0
        ev_str = "N/A"
        probs = [0.25, 0.35, 0.28, 0.12]
        sc_a_note = sc_b_note = sc_c_note = sc_d_note = ""

    # ── 止盈/止损：优先 LLM 数据 ──────────────────────────────────
    _wr_fallback = (probs[0] + probs[1]) * 100
    win_rate  = float(_llm_sc.get("win_rate") or _wr_fallback) if _use_llm_sc else _wr_fallback
    hold_days = _llm_sc.get("hold_days") or "5–10天"

    # ③ 综合胜率免责标注（样本量不足时显示）
    _aa_n = ctx.get("aa_hist_sample_n")
    if _aa_n is not None and int(_aa_n) < 10:
        win_rate_caveat = f'<span style="font-size:10px;color:var(--text3);margin-left:4px;">(n={_aa_n}, 仅供参考)</span>'
    else:
        win_rate_caveat = ""
    is_bear_dir = "bear" in direction
    tp_color  = "var(--red2)"   if is_bear_dir else "var(--green2)"
    sl_color  = "var(--green2)" if is_bear_dir else "var(--red2)"
    tp_sign   = "-" if is_bear_dir else "+"
    sl_sign   = "+" if is_bear_dir else "-"

    if price and price > 0:
        _llm_tp1 = _llm_sc.get("tp1"); _llm_tp2 = _llm_sc.get("tp2"); _llm_tp3 = _llm_sc.get("tp3")
        _llm_sl1 = _llm_sc.get("sl_conservative"); _llm_sl2 = _llm_sc.get("sl_standard"); _llm_sl3 = _llm_sc.get("sl_aggressive")
        if _use_llm_sc and all(v is not None for v in [_llm_tp1, _llm_tp2, _llm_tp3, _llm_sl1, _llm_sl2, _llm_sl3]):
            tp1     = float(_llm_tp1); tp2     = float(_llm_tp2); tp3     = float(_llm_tp3)
            sl_cons = float(_llm_sl1); sl_std  = float(_llm_sl2); sl_aggr = float(_llm_sl3)
            tp1_action = _llm_sc.get("tp1_action") or "减仓 1/3"
            tp2_action = _llm_sc.get("tp2_action") or "减仓 1/3"
            tp3_action = _llm_sc.get("tp3_action") or "清仓"
        else:
            # ML 降级
            if is_bear_dir:
                tp1 = price * 0.965; tp2 = price * 0.93; tp3 = price * 0.874
                sl_cons = price * 1.03; sl_std = price * 1.055; sl_aggr = price * 1.085
            else:
                tp1 = price * 1.025; tp2 = price * 1.05; tp3 = price * 1.128
                sl_cons = price * 0.97; sl_std = price * 0.945; sl_aggr = price * 0.915
            tp1_action = tp2_action = "减仓 1/3"; tp3_action = "清仓"
        tp1_pct_show = abs(tp1/price - 1) * 100
        tp2_pct_show = abs(tp2/price - 1) * 100
        tp3_pct_show = abs(tp3/price - 1) * 100
        sl_std_pct   = abs(sl_std/price - 1) * 100
        risk_reward  = tp2_pct_show / sl_std_pct if sl_std_pct > 0 else 0
    else:
        tp1 = tp2 = tp3 = 0
        sl_cons = sl_std = sl_aggr = 0
        tp1_pct_show = tp2_pct_show = tp3_pct_show = 0
        risk_reward = 0
        tp1_action = tp2_action = "减仓 1/3"; tp3_action = "清仓"

    # 止盈/止损 HTML 块
    tp_title = "📉 止盈计划（下行目标）" if "bear" in direction else "📈 止盈计划"
    sl_title = "🛡️ 止损计划（上行止损）" if "bear" in direction else "🛡️ 止损计划"
    if price and price > 0:
        trade_grid_html = f"""<div class="trade-grid">
      <div class="tp-box">
        <div class="tb-title">{tp_title}</div>
        <div class="tp-row">
          <span class="tr-price">${tp1:.2f}</span>
          <span class="tr-pct" style="color:{tp_color}">{tp_sign}{tp1_pct_show:.1f}%</span>
          <span style="font-size:11px;color:var(--text3)">{tp1_action}</span>
        </div>
        <div class="tp-row">
          <span class="tr-price">${tp2:.2f}</span>
          <span class="tr-pct" style="color:{tp_color}">{tp_sign}{tp2_pct_show:.1f}%</span>
          <span style="font-size:11px;color:var(--text3)">{tp2_action}</span>
        </div>
        <div class="tp-row">
          <span class="tr-price">${tp3:.2f}</span>
          <span class="tr-pct" style="color:{tp_color}">{tp_sign}{tp3_pct_show:.1f}%</span>
          <span style="font-size:11px;color:var(--text3)">{tp3_action}</span>
        </div>
        <div style="margin-top:8px;font-size:11px;color:var(--text3)">综合胜率 {win_rate:.1f}%{win_rate_caveat} · 持仓建议 {hold_days}</div>
      </div>
      <div class="sl-box">
        <div class="tb-title">{sl_title}</div>
        <div class="sl-row">
          <span class="sr-label">保守止损</span>
          <span class="sr-price" style="color:{sl_color}">${sl_cons:.2f}</span>
        </div>
        <div class="sl-row">
          <span class="sr-label">标准止损</span>
          <span class="sr-price" style="color:{sl_color}">${sl_std:.2f}</span>
        </div>
        <div class="sl-row">
          <span class="sr-label">激进止损</span>
          <span class="sr-price" style="color:{sl_color}">${sl_aggr:.2f}</span>
        </div>
        <div style="margin-top:8px;font-size:11px;color:var(--text3)">建议标准止损 ${sl_std:.2f} · 风险/收益比 ≈ 1:{risk_reward:.1f}</div>
      </div>
    </div>"""
    else:
        trade_grid_html = ""

    # 风险列表 —— 基于实际数据构建情景化风险项
    bear_signals = ctx.get("bear_signals", [])
    smart_risks = []   # list of (level_cls, badge_label, icon, title, note)

    # R1: 催化剂事件触发窗口
    cat_days = None
    for ev in ctx.get("catalysts", []):
        d = ev.get("days_until")
        if d is not None and d >= 0:
            cat_days = d if cat_days is None else min(cat_days, d)
    if cat_days is not None and cat_days <= 7:
        cat_name = next((e.get("event","催化剂") for e in ctx.get("catalysts",[]) if e.get("days_until") == cat_days), "关键催化剂事件")
        smart_risks.append(("risk-high", "HIGH", "📅",
            f"催化剂窗口 · {cat_name}（{cat_days}天后）",
            f"{cat_name} 即将在 {cat_days} 天内触发。财报/会议期间 IV 通常先扩张后骤降（IV Crush），"
            f"期权多头即使方向正确也可能因 Vega 损失而亏损；"
            f"事件冲击往往在盘前/盘后成交稀薄时放大，建议避免追价入场。"))
    elif cat_days is not None and cat_days <= 14:
        cat_name = next((e.get("event","催化剂") for e in ctx.get("catalysts",[]) if e.get("days_until") == cat_days), "催化剂事件")
        smart_risks.append(("risk-med", "MED", "📅",
            f"催化剂事件临近（{cat_days}天）",
            f"{cat_name} 将在 {cat_days} 天内发生。短期 IV 可能随事件临近持续攀升，"
            f"事件前后波动率变化将影响期权定价，需关注 IV 曲线结构变化。"))

    # R2: IV Skew / Vol Crush 风险
    iv_sk = ctx.get("iv_skew")
    if iv_sk and float(iv_sk) > 1.15:
        smart_risks.append(("risk-high", "HIGH", "📊",
            f"IV Skew 偏高 · Vol Crush 风险（Skew={iv_sk:.2f}）",
            f"Put/Call IV Skew {iv_sk:.2f} 显示市场对下行保护需求强烈。"
            f"若催化剂结果超预期，IV 将大幅下降（通常 -30%~-50%），"
            f"导致期权多头即使方向正确也因 Vega 损失而亏损。正股持有者无此风险。"))
    elif iv_sk and float(iv_sk) > 1.05:
        smart_risks.append(("risk-med", "MED", "📊",
            f"IV Skew 温和偏高（Skew={iv_sk:.2f}）",
            f"当前 IV Skew {iv_sk:.2f}，看跌期权溢价略高于中性水平。"
            f"市场存在一定的下行保护需求，期权策略需注意 Vega 敞口。"))

    # R3: ML 内部模型分歧
    ml7 = ctx.get("ml_7d", 0)
    ml_raw = ctx.get("ml_raw_7d")   # 如有根层预测
    if ml_raw is not None and abs(float(ml7) - float(ml_raw)) > 10:
        smart_risks.append(("risk-med", "MED", "🤖",
            f"ML 内部模型分歧（代理层 {ml7:+.1f}% vs 根层 {ml_raw:+.1f}%）",
            f"RivalBee 代理层预测 {ml7:+.1f}%，但底层模型输出 {ml_raw:+.1f}%，两者分歧显著。"
            f"分歧通常源于底层特征归一化异常（如 crowding_score 极端值）。"
            f"代理层输出更稳定可信，但内部分歧本身代表模型不确定性上升。"))

    # R4: 宏观情绪极端（使用独立变量名，避免覆盖后续 fg 显示变量）
    fg_raw = ctx.get("fg_score")
    if fg_raw is not None:
        fg_int = int(fg_raw)
        if fg_int <= 20:
            smart_risks.append(("risk-med", "MED", "😱",
                f"宏观极度恐慌（F&G={fg_int}）",
                f"恐慌贪婪指数 {fg_int}（极度恐慌区间 ≤25）。极度恐慌通常是买入信号，"
                f"但也可能意味着系统性风险尚未出清（关税、美联储路径、地缘政治）。"
                f"若宏观继续恶化，个股催化剂效应可能被市场整体情绪压制。"))
        elif fg_int >= 75:
            smart_risks.append(("risk-med", "MED", "🤑",
                f"宏观极度贪婪（F&G={fg_int}）",
                f"恐慌贪婪指数 {fg_int}（极度贪婪区间 ≥75）。市场已充分定价乐观预期，"
                f"任何负面意外都可能触发更强烈的获利了结。高位追涨的风险/收益比不佳。"))

    # R5: IV Rank 数据质量
    iv_pct_raw = ctx.get("iv_percentile")
    if iv_pct_raw is not None and float(iv_pct_raw) == 0:
        smart_risks.append(("risk-med", "MED", "📡",
            "IV Rank 数据缺失",
            "本次运行 IV Rank/Percentile 数据显示为 0（非真实 0 值，系采集异常）。"
            "期权定价难以准确评估当前 IV 是否偏贵或偏便宜。"
            "建议通过 Thinkorswim / Tastytrade 实时确认 IV 水平，避免基于过时数据入场。"))

    # R6: 历史样本量不足
    hist_acc = ctx.get("hist_accuracy")
    hist_n   = ctx.get("hist_sample_n")
    if hist_n is not None and int(hist_n) <= 5:
        smart_risks.append(("risk-low", "LOW", "📉",
            f"历史样本偏小（{hist_n} 次，准确率 {hist_acc or 'N/A'}%）",
            f"Alpha Hive 在类似评分形态下仅有 {hist_n} 次历史记录，"
            f"样本量不足以产生统计显著性，历史胜率参考意义有限，"
            f"主要依赖当前实时信号质量而非历史回测进行判断。"))

    # 如果智能风险项不足 3 条，用原始 bear_signals 补充
    if len(smart_risks) < 3 and bear_signals:
        fallback_icons  = ["⚡", "💧", "🏛️"]
        fallback_titles = ["波动性风险", "流动性风险", "监管风险"]
        for j, sig in enumerate(bear_signals[:max(0, 3 - len(smart_risks))]):
            lvl = "risk-med"
            smart_risks.append((lvl, "MED", fallback_icons[j % 3],
                fallback_titles[j % 3], sig))

    risk_items_html = ""
    for (lvl_cls, badge_txt, icon, title, note) in smart_risks:
        risk_items_html += f"""
        <div class="risk-item {lvl_cls}">
          <div class="risk-badge">{badge_txt}</div>
          <div>
            <div class="risk-title">{icon} {title}</div>
            <div class="risk-note">{note}</div>
          </div>
        </div>"""

    # ── 明日追踪任务 ─────────────────────────────────────────
    _track_tasks = []
    _px = float(ctx.get("price") or 0)
    _sup_list = (ctx.get("key_levels") or {}).get("support", [])
    _res_list = (ctx.get("key_levels") or {}).get("resistance", [])
    if _px and _sup_list:
        _s1 = _sup_list[0].get("strike") if _sup_list else None
        if _s1:
            _track_tasks.append(
                f"<strong>价位警戒</strong>：若收盘跌破 <strong>${_s1:.0f}</strong>（最强支撑 / 当前 ${_px:.1f}），多头假设需重新评估")
    if _px and _res_list:
        _r1 = _res_list[0].get("strike") if _res_list else None
        if _r1:
            _track_tasks.append(
                f"<strong>突破确认</strong>：若价格有效收盘上穿 <strong>${_r1:.0f}</strong> 阻力（当前距离 {(_r1-_px)/_px*100:+.1f}%），可考虑加仓")
    _iv_pct = ctx.get("iv_percentile")
    if _iv_pct is not None:
        _ivf = float(_iv_pct)
        if _ivf < 50:
            _track_tasks.append(
                f"<strong>IV 追踪</strong>：当前 IV Rank {_ivf:.0f}th（低位），若升至 <strong>50th+</strong> 注意期权成本上升 / 可能有催化剂预期")
        elif _ivf >= 70:
            _track_tasks.append(
                f"<strong>IV Crush 警戒</strong>：当前 IV Rank {_ivf:.0f}th（高位），事件后 IV 可能骤降 ≥20%，持有期权需设定 Vega 止损")
    _cats = ctx.get("catalysts") or []
    if _cats:
        _nc = _cats[0]
        _nc_title = (_nc.get("event") or _nc.get("title") or "")[:45]
        _nc_date  = _nc.get("date", "")
        if _nc_title:
            _track_tasks.append(
                f"<strong>催化剂追踪</strong>：{_nc_title}（{_nc_date}）—— 事件前注意成交量和 IV 异动")
    if ctx.get("bear_signals"):
        _track_tasks.append(
            f"<strong>空头信号监控</strong>：{ctx['bear_signals'][0][:60]} —— 若信号持续升级或新增共振，减仓止损")

    if _track_tasks:
        _task_items_html = "".join(
            f'<li style="padding:7px 0;border-bottom:1px solid var(--border2);'
            f'color:var(--text2);font-size:13px;line-height:1.6;">'
            f'<span style="color:var(--gold2);margin-right:8px;font-size:15px;">☐</span>{t}</li>'
            for t in _track_tasks
        )
        tracking_tasks_html = (
            f'<div style="margin-top:22px;">'
            f'<div style="font-size:12px;font-weight:700;color:var(--text3);letter-spacing:.1em;'
            f'text-transform:uppercase;margin-bottom:10px;">📋 明日追踪任务</div>'
            f'<ul style="list-style:none;margin:0;padding:0;">{_task_items_html}</ul>'
            f'</div>'
        )
    else:
        tracking_tasks_html = ""

    # ── 主 HTML ──────────────────────────────────────────────
    # 安全转换 put_call_ratio（可能为 'N/A' 字符串）
    try:
        pc_float = float(ctx['put_call_ratio'])
    except (ValueError, TypeError):
        pc_float = 1.0   # 无法解析时视为中性
    iv_skew_display = f"{ctx['iv_skew']:.2f}" if ctx.get("iv_skew") else "N/A"
    bull_unusual_count = bullish_unusual
    bear_unusual_count = bearish_unusual
    # OTM IV 副文本（在 IV Skew 卡里显示）
    otm_p = ctx.get("otm_put_iv")
    otm_c = ctx.get("otm_call_iv")
    if otm_p and otm_c:
        otm_sub = f"Put {otm_p:.0f}% / Call {otm_c:.0f}%"
    else:
        otm_sub = ctx['iv_skew_signal']
    # IV Rank 显示（用 iv_percentile 百分位）
    iv_pct = ctx.get("iv_percentile")
    if iv_pct is not None and float(iv_pct) > 0:
        iv_rank_display = f"{iv_pct:.0f}th"
        iv_rank_color   = "var(--red2)" if float(iv_pct) >= 70 else ("var(--green2)" if float(iv_pct) <= 30 else "var(--gold2)")
        iv_rank_sub     = "高波动区间" if float(iv_pct) >= 70 else ("低波动区间" if float(iv_pct) <= 30 else "中等波动")
    else:
        iv_rank_display = "—"
        iv_rank_color   = "var(--text3)"
        iv_rank_sub     = "数据待更新"

    # ④ GEX（Gamma Exposure）
    _gex_val    = ctx.get("gamma_exposure", 0)
    _gex_squeeze = ctx.get("gamma_squeeze_risk", "")
    _gex_regime  = ctx.get("gex_regime", "")
    _gex_flip    = ctx.get("gex_flip")
    _gex_cwall   = ctx.get("gex_call_wall")
    _gex_pwall   = ctx.get("gex_put_wall")
    try:
        _gex_float = float(_gex_val)
    except (TypeError, ValueError):
        _gex_float = 0.0
    if _gex_float == 0.0:
        gex_display = "N/A"
        gex_color   = "var(--text3)"
        gex_sub     = "⚠️ 数据采集失败"
        gex_card_class = "oc-neut"
        gex_extra_html = ""
    else:
        # 主数值显示：百万美元单位
        gex_display = f"{_gex_float:+.1f}M$"
        # regime 颜色：negative_gex（放大波动）红，positive_gex（压制波动）绿
        if _gex_regime == "negative_gex":
            gex_color = "var(--red2)";  gex_card_class = "oc-bear"
            gex_sub   = "⚠️ 负GEX·放大波动"
        elif _gex_regime == "positive_gex":
            gex_color = "var(--green2)"; gex_card_class = "oc-bull"
            gex_sub   = "✅ 正GEX·压制波动"
        else:
            gex_color = "var(--gold2)"; gex_card_class = "oc-neut"
            gex_sub   = "Gamma敞口"
        # flip point & 最大call/put墙 额外行
        _extra_parts = []
        if _gex_flip:
            _extra_parts.append(f"Flip ${_gex_flip:.0f}")
        if _gex_cwall:
            _extra_parts.append(f"Call墙 ${_gex_cwall:.0f}")
        if _gex_pwall:
            _extra_parts.append(f"Put墙 ${_gex_pwall:.0f}")
        gex_extra_html = (f'<div style="font-size:10px;color:var(--text3);margin-top:2px;">'
                          + " · ".join(_extra_parts) + "</div>") if _extra_parts else ""

    # ── IV Crush 面板（CH3 催化剂章节内嵌）──────────────────────────────────────
    _ivc = ctx.get("iv_crush", {}) or {}
    if _ivc and _ivc.get("avg_abs_move", 0):
        _ivc_avg    = _ivc.get("avg_abs_move", 0)
        _ivc_imp    = _ivc.get("current_implied_move")
        _ivc_up     = _ivc.get("up_count", 0)
        _ivc_down   = _ivc.get("down_count", 0)
        _ivc_total  = _ivc_up + _ivc_down
        _ivc_win    = int(_ivc_up / _ivc_total * 100) if _ivc_total > 0 else 0
        _ivc_ed     = _ivc.get("next_earnings_date", "—")
        _ivc_days   = _ivc.get("next_earnings_days")
        _ivc_days_str = f" ({_ivc_days}天后)" if _ivc_days is not None else ""
        _imp_vs_hist = ""
        if _ivc_imp:
            _ratio = _ivc_imp / _ivc_avg if _ivc_avg else 1
            if _ratio > 1.15:
                _imp_vs_hist = f'<span style="color:var(--red2);font-size:10px;font-weight:700">⚠️ 隐含幅度偏贵 (+{(_ratio-1)*100:.0f}%)</span>'
            elif _ratio < 0.85:
                _imp_vs_hist = f'<span style="color:var(--green2);font-size:10px;font-weight:700">✅ 隐含幅度偏便宜 ({(_ratio-1)*100:.0f}%)</span>'
            else:
                _imp_vs_hist = f'<span style="color:var(--gold2);font-size:10px">合理定价</span>'
        iv_crush_html = f"""<div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:14px;">
  <div style="font-size:11px;font-weight:700;color:var(--cyan2);margin-bottom:10px;">📊 IV Crush 历史分析（财报波动）</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px;">
    <div style="text-align:center;background:var(--bg4);border-radius:6px;padding:8px;">
      <div style="font-size:10px;color:var(--text3)">历史平均波动</div>
      <div style="font-size:20px;font-weight:800;color:var(--gold2)">{_ivc_avg:.1f}%</div>
      <div style="font-size:10px;color:var(--text3)">过去 {_ivc_total} 次财报</div>
    </div>
    <div style="text-align:center;background:var(--bg4);border-radius:6px;padding:8px;">
      <div style="font-size:10px;color:var(--text3)">当前隐含幅度</div>
      <div style="font-size:20px;font-weight:800;color:var(--blue2)">{f"{_ivc_imp:.1f}%" if _ivc_imp else "N/A"}</div>
      <div style="font-size:10px">{_imp_vs_hist}</div>
    </div>
    <div style="text-align:center;background:var(--bg4);border-radius:6px;padding:8px;">
      <div style="font-size:10px;color:var(--text3)">上涨胜率</div>
      <div style="font-size:20px;font-weight:800;color:var(--green2)">{_ivc_win}%</div>
      <div style="font-size:10px;color:var(--text3)">{_ivc_up}涨 / {_ivc_down}跌</div>
    </div>
    <div style="text-align:center;background:var(--bg4);border-radius:6px;padding:8px;">
      <div style="font-size:10px;color:var(--text3)">下次财报</div>
      <div style="font-size:13px;font-weight:700;color:var(--text)">{_ivc_ed}</div>
      <div style="font-size:10px;color:var(--text3)">{_ivc_days_str}</div>
    </div>
  </div>
</div>"""
    else:
        iv_crush_html = ""

    # ── VIX 期限结构卡片（CH5 宏观章节内嵌）──────────────────────────────────────
    _vt = ctx.get("vix_term_structure", {}) or {}
    if _vt and _vt.get("structure") not in ("", "unknown", None):
        _vt_struct   = _vt.get("structure", "unknown")
        _vt_spot     = _vt.get("spot_vix")
        _vt_m1       = _vt.get("m1")
        _vt_m2       = _vt.get("m2")
        _vt_spread   = _vt.get("m1_m2_spread")
        _vt_zh       = {"contango": "Contango（平静）", "backwardation": "Backwardation（恐慌）", "flat": "平坦结构"}.get(_vt_struct, _vt_struct)
        _vt_color    = "var(--green2)" if _vt_struct == "contango" else ("var(--red2)" if _vt_struct == "backwardation" else "var(--gold2)")
        _vt_sub      = "卖方有利" if _vt_struct == "contango" else ("⚠️ 恐慌结构" if _vt_struct == "backwardation" else "观望")
        vix_card_html = f"""<div class="opt-card oc-neut">
          <div class="oc-label">VIX 期限结构</div>
          <div class="oc-val" style="color:{_vt_color};font-size:14px;font-weight:800">{_vt_zh}</div>
          <div style="font-size:10px;color:var(--text3);margin-top:3px">VIX={f"{_vt_spot:.1f}" if _vt_spot else "N/A"} M1→M2={f"{_vt_m1:.1f}→{_vt_m2:.1f}" if _vt_m1 and _vt_m2 else "N/A"}</div>
          <div class="oc-sub">{_vt_sub}</div>
        </div>"""
    else:
        vix_card_html = ""

    # ── 国会交易卡片（CH5）──────────────────────────────────────────────────────
    _cg = ctx.get("congress", {}) or {}
    if _cg and (_cg.get("buy_count", 0) + _cg.get("sell_count", 0)) > 0:
        _cg_buy  = _cg.get("buy_count", 0)
        _cg_sell = _cg.get("sell_count", 0)
        _cg_sc   = _cg.get("congress_score", 0)
        _cg_net  = _cg.get("net_amount_est", 0)
        _cg_net_str = f"+${abs(_cg_net)/1e6:.1f}M" if _cg_net > 0 else (f"-${abs(_cg_net)/1e6:.1f}M" if _cg_net < 0 else "0")
        _cg_color = "var(--green2)" if _cg_buy > _cg_sell else "var(--red2)"
        congress_card_html = f"""<div class="opt-card oc-info">
          <div class="oc-label">🏛️ 国会交易（90天）</div>
          <div class="oc-val" style="color:{_cg_color}">{_cg_buy}买/{_cg_sell}卖</div>
          <div style="font-size:10px;color:var(--text3);margin-top:2px">净额 {_cg_net_str} · 评分 {_cg_sc}/10</div>
          <div class="oc-sub">{"内部人买入信号" if _cg_buy > _cg_sell else "内部人卖出信号"}</div>
        </div>"""
    else:
        congress_card_html = ""


    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{ticker} · Alpha Hive 深度分析 · {report_date}</title>
<style>
  :root {{
    --bg: #0a0e1a; --bg2: #111827; --bg3: #1a2234; --bg4: #1e2a3e;
    --border: #2a3a52; --border2: #3a4e6a;
    --gold: #f59e0b; --gold2: #fbbf24;
    --green: #10b981; --green2: #34d399;
    --red: #ef4444; --red2: #f87171;
    --blue: #3b82f6; --blue2: #60a5fa;
    --purple: #8b5cf6; --purple2: #a78bfa;
    --cyan: #06b6d4; --cyan2: #22d3ee;
    --text: #e2e8f0; --text2: #94a3b8; --text3: #64748b;
    --bullish: #10b981; --bearish: #ef4444; --neutral: #f59e0b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, 'PingFang SC', 'Helvetica Neue', sans-serif; line-height: 1.6; }}
  .header {{ background: linear-gradient(135deg, #0d1b2e 0%, #1a2840 50%, #0d1b2e 100%); border-bottom: 1px solid var(--border2); padding: 28px 24px 22px; position: relative; overflow: hidden; }}
  .header::before {{ content: ''; position: absolute; top: -40px; right: -40px; width: 200px; height: 200px; background: radial-gradient(circle, rgba(245,158,11,0.12) 0%, transparent 70%); pointer-events: none; }}
  .header-top {{ display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px; }}
  .ticker-block {{ display: flex; align-items: center; gap: 14px; }}
  .ticker-logo {{ width: 48px; height: 48px; background: linear-gradient(135deg, #3b82f6, #1d4ed8); border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 900; color: white; letter-spacing: -1px; }}
  .ticker-info h1 {{ font-size: 28px; font-weight: 800; letter-spacing: -0.5px; }}
  .ticker-info .company {{ font-size: 13px; color: var(--text2); margin-top: 3px; font-weight: 500; }}
  .ticker-info .subtitle {{ font-size: 11px; color: var(--text3); margin-top: 2px; }}
  .price-block {{ text-align: right; }}
  .price {{ font-size: 32px; font-weight: 800; color: var(--gold2); }}
  .price-sub {{ font-size: 12px; color: var(--text3); margin-top: 2px; }}
  .verdict-banner {{ margin-top: 18px; background: {verdict_bg}; border: 1px solid {verdict_border}; border-radius: 10px; padding: 14px 18px; display: flex; align-items: center; gap: 14px; }}
  .verdict-score {{ font-size: 42px; font-weight: 900; color: {verdict_color}; line-height: 1; }}
  .verdict-text {{ flex: 1; }}
  .verdict-title {{ font-size: 16px; font-weight: 700; color: {verdict_color}; }}
  .verdict-sub {{ font-size: 13px; color: var(--text2); margin-top: 3px; }}
  .resonance-badge {{ background: rgba(139,92,246,0.2); border: 1px solid rgba(139,92,246,0.5); border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: 700; color: var(--purple2); white-space: nowrap; }}
  .nav {{ background: var(--bg2); border-bottom: 1px solid var(--border); padding: 0 16px; display: flex; overflow-x: auto; gap: 0; scrollbar-width: none; }}
  .nav::-webkit-scrollbar {{ display: none; }}
  .nav a {{ display: flex; align-items: center; gap: 6px; padding: 12px 14px; font-size: 12px; font-weight: 600; color: var(--text3); text-decoration: none; white-space: nowrap; border-bottom: 2px solid transparent; transition: all 0.2s; }}
  .nav a:hover {{ color: var(--text); border-color: var(--blue); }}
  .main {{ max-width: 900px; margin: 0 auto; padding: 20px 16px 60px; }}
  .section {{ margin-bottom: 24px; background: var(--bg2); border: 1px solid var(--border); border-radius: 14px; overflow: hidden; }}
  .section-header {{ padding: 16px 20px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; }}
  .section-icon {{ font-size: 18px; }}
  .section-title {{ font-size: 15px; font-weight: 700; }}
  .section-badge {{ margin-left: auto; font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 20px; }}
  .badge-bull {{ background: rgba(16,185,129,0.2); color: var(--green2); border: 1px solid rgba(16,185,129,0.3); }}
  .badge-bear {{ background: rgba(239,68,68,0.2); color: var(--red2); border: 1px solid rgba(239,68,68,0.3); }}
  .badge-neutral {{ background: rgba(245,158,11,0.2); color: var(--gold2); border: 1px solid rgba(245,158,11,0.3); }}
  .badge-resonance {{ background: rgba(139,92,246,0.25); color: var(--purple2); border: 1px solid rgba(139,92,246,0.4); }}
  .section-body {{ padding: 18px 20px; }}
  .score-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 12px; }}
  .score-card {{ background: var(--bg3); border: 1px solid var(--border); border-radius: 10px; padding: 12px 10px; text-align: center; }}
  .score-card .bee {{ font-size: 11px; color: var(--text3); margin-bottom: 4px; }}
  .score-card .val {{ font-size: 26px; font-weight: 800; line-height: 1; }}
  .score-card .dim {{ font-size: 10px; color: var(--text3); margin-top: 3px; }}
  .score-card.bull {{ border-color: rgba(16,185,129,0.35); }}
  .score-card.bull .val {{ color: var(--green2); }}
  .score-card.bear {{ border-color: rgba(239,68,68,0.35); }}
  .score-card.bear .val {{ color: var(--red2); }}
  .score-card.neutral {{ border-color: rgba(245,158,11,0.35); }}
  .score-card.neutral .val {{ color: var(--gold2); }}
  .resonance-box {{ background: linear-gradient(135deg, rgba(139,92,246,0.1), rgba(245,158,11,0.08)); border: 1px solid rgba(139,92,246,0.35); border-radius: 10px; padding: 16px; margin-bottom: 16px; }}
  .resonance-title {{ font-size: 13px; font-weight: 700; color: var(--purple2); margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }}
  .resonance-dims {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .resonance-dim {{ background: rgba(139,92,246,0.2); border: 1px solid rgba(139,92,246,0.35); border-radius: 6px; padding: 3px 10px; font-size: 11px; font-weight: 700; color: var(--purple2); }}
  .levels-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .level-block {{ background: var(--bg3); border-radius: 10px; padding: 14px; }}
  .level-block h4 {{ font-size: 11px; font-weight: 700; margin-bottom: 10px; }}
  .level-block.support h4 {{ color: var(--green2); }}
  .level-block.resistance h4 {{ color: var(--red2); }}
  .level-row {{ display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; }}
  .level-row:last-child {{ border-bottom: none; }}
  .level-strike {{ font-weight: 700; font-size: 14px; }}
  .support .level-strike {{ color: var(--green2); }}
  .resistance .level-strike {{ color: var(--red2); }}
  .level-meta {{ color: var(--text3); font-size: 11px; }}
  .flow-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .flow-table th {{ text-align: left; padding: 7px 10px; background: var(--bg3); color: var(--text3); font-weight: 600; font-size: 11px; border-bottom: 1px solid var(--border); }}
  .flow-table td {{ padding: 7px 10px; border-bottom: 1px solid var(--border); }}
  .flow-table tr:last-child td {{ border-bottom: none; }}
  .flow-call {{ color: var(--green2); font-weight: 700; }}
  .flow-put {{ color: var(--red2); font-weight: 700; }}
  .timeline {{ position: relative; padding-left: 24px; }}
  .timeline::before {{ content: ''; position: absolute; left: 7px; top: 4px; bottom: 4px; width: 2px; background: linear-gradient(to bottom, var(--gold), var(--blue)); border-radius: 1px; }}
  .timeline-item {{ position: relative; margin-bottom: 14px; }}
  .timeline-dot {{ position: absolute; left: -20px; top: 4px; width: 10px; height: 10px; border-radius: 50%; border: 2px solid; }}
  .dot-critical {{ background: var(--red); border-color: var(--red2); }}
  .dot-high {{ background: var(--gold); border-color: var(--gold2); }}
  .dot-medium {{ background: var(--blue); border-color: var(--blue2); }}
  .timeline-label {{ font-size: 10px; color: var(--text3); font-weight: 600; }}
  .timeline-event {{ font-size: 13px; font-weight: 700; color: var(--text); margin: 1px 0; }}
  .timeline-meta {{ font-size: 11px; color: var(--text3); }}
  .timeline-item.item-critical .timeline-event {{ color: var(--red2); }}
  .timeline-item.item-critical .timeline-label {{ color: var(--red); }}
  .timeline-item.item-high .timeline-event {{ color: var(--gold2); }}
  .timeline-item.item-high .timeline-label {{ color: var(--gold); }}
  .timeline-item.item-past {{ opacity: 0.45; }}
  .scenario-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .scenario-card {{ background: var(--bg3); border-radius: 10px; padding: 14px; border: 1px solid var(--border); }}
  .scenario-card .s-label {{ font-size: 10px; font-weight: 700; color: var(--text3); text-transform: uppercase; margin-bottom: 6px; }}
  .scenario-card .s-title {{ font-size: 13px; font-weight: 700; margin-bottom: 4px; }}
  .scenario-card .s-price {{ font-size: 18px; font-weight: 800; }}
  .scenario-card .s-prob {{ font-size: 11px; color: var(--text2); margin-top: 4px; }}
  .scenario-card .s-desc {{ font-size: 11px; color: var(--text3); margin-top: 6px; line-height: 1.5; }}
  .scenario-bull {{ border-color: rgba(16,185,129,0.3); }}
  .scenario-bull .s-price {{ color: var(--green2); }}
  .scenario-bear {{ border-color: rgba(239,68,68,0.3); }}
  .scenario-bear .s-price {{ color: var(--red2); }}
  .metrics-row {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); gap: 10px; }}
  .metric-card {{ background: var(--bg3); border-radius: 8px; padding: 12px 10px; text-align: center; border: 1px solid var(--border); }}
  .metric-card .m-label {{ font-size: 10px; color: var(--text3); margin-bottom: 4px; }}
  .metric-card .m-val {{ font-size: 18px; font-weight: 800; }}
  .metric-card .m-sub {{ font-size: 10px; color: var(--text3); margin-top: 2px; }}
  .prose {{ font-size: 13px; color: var(--text2); line-height: 1.75; }}
  .prose p {{ margin-bottom: 10px; }}
  .prose strong {{ color: var(--text); }}
  .prose .highlight {{ color: var(--gold2); font-weight: 600; }}
  .prose .bull-text {{ color: var(--green2); font-weight: 600; }}
  .prose .bear-text {{ color: var(--red2); font-weight: 600; }}
  .risk-list {{ list-style: none; }}
  .risk-list li {{ display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px; color: var(--text2); }}
  .risk-list li:last-child {{ border-bottom: none; }}
  .risk-icon {{ font-size: 14px; flex-shrink: 0; }}
  .ev-box {{ background: linear-gradient(135deg, rgba(16,185,129,0.12), rgba(59,130,246,0.08)); border: 1px solid rgba(16,185,129,0.3); border-radius: 10px; padding: 16px; margin-top: 14px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
  .ev-left .label {{ font-size: 11px; color: var(--text3); }}
  .ev-left .big {{ font-size: 28px; font-weight: 900; color: var(--green2); }}
  .ev-right {{ font-size: 12px; color: var(--text2); max-width: 300px; }}
  .disclaimer {{ background: var(--bg3); border: 1px solid var(--border); border-radius: 10px; padding: 14px; font-size: 11px; color: var(--text3); line-height: 1.6; margin-top: 24px; }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 12px; }}
  .bar-label {{ width: 80px; color: var(--text2); flex-shrink: 0; font-size: 11px; }}
  .bar-track {{ flex: 1; height: 6px; background: var(--bg4); border-radius: 3px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 3px; }}
  .bar-val {{ width: 36px; text-align: right; font-weight: 700; font-size: 12px; }}
  .divider {{ height: 1px; background: var(--border); margin: 14px 0; }}
  .gen-notice {{ background: rgba(59,130,246,0.08); border: 1px solid rgba(59,130,246,0.25); border-radius: 8px; padding: 10px 14px; font-size: 12px; color: var(--blue2); margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
  /* ── Rich template additions ─────────────────────────────── */
  .ch-num {{ font-size:11px; color:var(--text3); background:var(--bg3); border:1px solid var(--border); border-radius:4px; padding:2px 7px; font-weight:600; }}
  .score-summary {{ background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-top:12px; }}
  .score-summary-row {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
  .final-score-big {{ font-size:44px; font-weight:900; line-height:1; }}
  .final-score-big.bear-score {{ color:var(--red2); }} .final-score-big.bull-score {{ color:var(--green2); }} .final-score-big.neutral-score {{ color:var(--gold2); }}
  .final-score-label {{ font-size:11px; color:var(--text3); margin-top:3px; }}
  .score-divider {{ width:1px; height:44px; background:var(--border); align-self:stretch; }}
  .stat-pill {{ background:var(--bg4); border-radius:6px; padding:7px 14px; text-align:center; }}
  .stat-pill .sp-label {{ font-size:10px; color:var(--text3); }}
  .stat-pill .sp-val {{ font-size:16px; font-weight:800; }}
  .bull-pill .sp-val {{ color:var(--green2); }} .bear-pill .sp-val {{ color:var(--red2); }} .neut-pill .sp-val {{ color:var(--gold2); }}
  .opt-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(140px,1fr)); gap:10px; margin-bottom:14px; }}
  .opt-card {{ background:var(--bg3); border:1px solid var(--border); border-radius:8px; padding:14px; }}
  .opt-card .oc-label {{ font-size:11px; color:var(--text3); margin-bottom:4px; }}
  .opt-card .oc-val {{ font-size:24px; font-weight:800; }}
  .opt-card .oc-sub {{ font-size:11px; color:var(--text3); margin-top:3px; }}
  .oc-bull .oc-val {{ color:var(--green2); }} .oc-bear .oc-val {{ color:var(--red2); }} .oc-neut .oc-val {{ color:var(--gold2); }} .oc-info .oc-val {{ color:var(--blue2); }}
  .macro-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px; margin-bottom:14px; }}
  .unusual-list {{ display:flex; flex-direction:column; gap:6px; margin:12px 0; }}
  .unusual-item {{ display:flex; align-items:center; gap:10px; background:var(--bg3); border-radius:6px; padding:8px 12px; border-left:3px solid; }}
  .unusual-item.bull-item {{ border-color:var(--green); }} .unusual-item.bear-item {{ border-color:var(--red); }}
  .unusual-item .ui-type {{ font-size:10px; font-weight:700; width:56px; flex-shrink:0; }}
  .bull-item .ui-type {{ color:var(--green2); }} .bear-item .ui-type {{ color:var(--red2); }}
  .unusual-item .ui-strike {{ font-size:13px; font-weight:700; color:var(--text); width:52px; }}
  .unusual-item .ui-vol {{ font-size:12px; color:var(--text2); flex:1; }}
  .unusual-item .ui-signal {{ font-size:11px; font-weight:700; }}
  .bull-item .ui-signal {{ color:var(--green2); }} .bear-item .ui-signal {{ color:var(--red2); }}
  .sc-card {{ background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:14px; }}
  .sc-card .sc-name {{ font-size:12px; font-weight:700; margin-bottom:5px; }}
  .sc-card .sc-prob {{ font-size:11px; color:var(--text2); margin-bottom:8px; }}
  .sc-card .sc-price {{ font-size:22px; font-weight:800; margin-bottom:3px; }}
  .sc-card .sc-gain {{ font-size:13px; font-weight:700; }}
  .sc-card .sc-note {{ font-size:11px; color:var(--text2); margin-top:7px; line-height:1.6; }}
  .sc-bull .sc-name, .sc-bull .sc-price, .sc-bull .sc-gain {{ color:var(--green2); }}
  .sc-meh .sc-name, .sc-meh .sc-price, .sc-meh .sc-gain {{ color:var(--gold2); }}
  .sc-bear .sc-name, .sc-bear .sc-price, .sc-bear .sc-gain {{ color:var(--red2); }}
  .risk-list {{ display:flex; flex-direction:column; gap:8px; }}
  .risk-item {{ background:var(--bg3); border:1px solid var(--border); border-radius:8px; padding:10px 14px; display:flex; gap:12px; align-items:flex-start; }}
  .risk-badge {{ font-size:10px; font-weight:700; padding:3px 8px; border-radius:4px; white-space:nowrap; flex-shrink:0; margin-top:2px; }}
  .risk-high .risk-badge {{ background:rgba(239,68,68,.2); color:var(--red2); border:1px solid rgba(239,68,68,.4); }}
  .risk-med .risk-badge {{ background:rgba(245,158,11,.2); color:var(--gold2); border:1px solid rgba(245,158,11,.4); }}
  .risk-low .risk-badge {{ background:rgba(59,130,246,.2); color:var(--blue2); border:1px solid rgba(59,130,246,.4); }}
  .risk-title {{ font-size:13px; font-weight:600; color:var(--text); }}
  .risk-note {{ font-size:12px; color:var(--text2); margin-top:3px; line-height:1.5; }}
  /* ── TRADE GRID (止盈/止损) ── */
  .trade-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }}
  .tp-box, .sl-box {{ background:var(--bg3); border:1px solid var(--border); border-radius:10px; padding:14px; }}
  .tp-box {{ border-color:rgba(34,197,94,0.3); }}
  .sl-box {{ border-color:rgba(239,68,68,0.3); }}
  .tb-title {{ font-size:12px; font-weight:700; margin-bottom:10px; }}
  .tp-box .tb-title {{ color:var(--green2); }}
  .sl-box .tb-title {{ color:var(--red2); }}
  .tp-row {{ display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px solid var(--border); font-size:12px; }}
  .tp-row:last-of-type {{ border:none; }}
  .tp-row .tr-price {{ font-weight:700; color:var(--text1); }}
  .tp-row .tr-pct {{ color:var(--green2); font-weight:600; }}
  .sl-row {{ display:flex; justify-content:space-between; align-items:center; padding:5px 0; border-bottom:1px solid var(--border); font-size:12px; }}
  .sl-row:last-of-type {{ border:none; }}
  .sl-row .sr-label {{ color:var(--text2); }}
  .sl-row .sr-price {{ font-weight:700; color:var(--red2); }}
  @media (max-width: 600px) {{
    .levels-grid, .scenario-grid, .trade-grid {{ grid-template-columns: 1fr; }}
    .verdict-banner {{ flex-wrap: wrap; }}
    .price {{ font-size: 24px; }}
    .verdict-score {{ font-size: 32px; }}
  }}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div class="header-top">
    <div class="ticker-block">
      <div class="ticker-logo">{ticker[:2]}</div>
      <div class="ticker-info">
        <h1>{ticker}</h1>
        <div class="company">{ctx.get('overview','') if ctx.get('overview') else 'Alpha Hive · 深度分析'}</div>
        <div class="subtitle">📅 {report_date} · Alpha Hive 深度分析 v3.0</div>
      </div>
    </div>
    <div class="price-block">
      <div class="price">{price_str}</div>
      <div class="price-sub">NASDAQ · {price_label}</div>
    </div>
  </div>
  <div class="verdict-banner">
    <div class="verdict-score">{score:.2f}</div>
    <div class="verdict-text">
      <div class="verdict-title">{direction_zh_str} · {res_label}</div>
      <div class="verdict-sub">
        {res_count}维共振{'触发' if res_detected else '未触发'} ·
        期权流{ctx['flow_direction']} ·
        P/C = {ctx['put_call_ratio']}
      </div>
    </div>
    <div class="resonance-badge">{'⚡ 共振已触发' if res_detected else '○ 无共振'}</div>
  </div>
</div>

<!-- NAV -->
<nav class="nav">
  <a href="#exec-summary">📋 摘要</a>
  <a href="#ch1">🐝 蜂群评分</a>
  <a href="#ch2">⚡ 共振分析</a>
  <a href="#ch3">📅 催化剂</a>
  <a href="#ch4">📊 期权结构</a>
  <a href="#ch5">🌍 宏观/情绪</a>
  <a href="#ch6">🎯 情景推演</a>
  <a href="#ch7">⚠️ 风险</a>
</nav>

<div class="main">

  <div class="gen-notice">
    🤖 <strong>混合模式 · Template C v3.0</strong> —— 本地 JSON 数据 + Claude API 深度推理生成 · {report_date}
  </div>

  {exec_summary_html}
  {dod_delta_html}

  <!-- CH1 -->
  <div class="section" id="ch1">
    <div class="section-header">
      <span class="ch-num">CH1</span>
      <span class="section-icon">🐝</span>
      <span class="section-title">第一章 · 蜂群七维评分</span>
      <span class="section-badge {direction_badge(direction)}">综合 {score:.2f} · {'看多' if 'bull' in direction else ('看空' if 'bear' in direction else '中性')}</span>
    </div>
    <div class="section-body">
      {score_summary_html}{_conf_chart_html}
      <div class="score-grid" style="margin-top:14px;">{score_cards}</div>
      <div class="divider"></div>
      <div class="prose">{reasoning.get('swarm_analysis', '<p>分析生成中...</p>')}</div>
      <div style="margin-top:14px;">{bar_rows}</div>
      {accuracy_html}
      {ctx.get('rl_insight_html', '')}
      {ctx.get('thesis_break_html', '')}
    </div>
  </div>

  <!-- CH2 -->
  <div class="section" id="ch2">
    <div class="section-header">
      <span class="ch-num">CH2</span>
      <span class="section-icon">⚡</span>
      <span class="section-title">第二章 · 蜂群共振分析</span>
      <span class="section-badge badge-resonance">{res_label} · +{res_boost}% 信心提升</span>
    </div>
    <div class="section-body">
      <div class="resonance-box">
        <div class="resonance-title">{'⚡ 跨维度共振已触发' if res_detected else '○ 本次无共振触发'}</div>
        <div class="resonance-dims">{res_dims_html}</div>
        <div style="margin-top:10px;font-size:12px;color:var(--text2);">
          {res_count} 个跨维度{'看涨' if 'bull' in direction else '看空'}信号同时触发 ·
          置信度自动提升 +{res_boost}% ·
          历史回测显示共振信号胜率高于单维信号约 18%
        </div>
      </div>
      <div class="prose">{reasoning.get('resonance', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH3 -->
  <div class="section" id="ch3">
    <div class="section-header">
      <span class="ch-num">CH3</span>
      <span class="section-icon">📅</span>
      <span class="section-title">第三章 · 催化剂时间线</span>
      <span class="section-badge badge-bull">{len(ctx['catalysts'])} 个催化剂 · {fmt_score(ctx['chronos'].get('score'))}/10</span>
    </div>
    <div class="section-body">
      <div class="timeline">{timeline_html}</div>
      <div class="divider"></div>
      {iv_crush_html}
      <div class="prose">{reasoning.get('catalyst', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH4 -->
  <div class="section" id="ch4">
    <div class="section-header">
      <span class="ch-num">CH4</span>
      <span class="section-icon">📊</span>
      <span class="section-title">第四章 · 期权市场结构</span>
      <span class="section-badge {flow_badge}">P/C={ctx['put_call_ratio']} · OI={oi_str} · {bull_unusual_count}个看涨异动</span>
    </div>
    <div class="section-body">
      {_opts_chart_html}{_iv_term_chart_html}{_skew_chart_html}{_gex_profile_chart_html}
      <div class="opt-grid" style="margin-bottom:16px;">
        <div class="opt-card oc-bull">
          <div class="oc-label">Put/Call 比</div>
          <div class="oc-val" style="color:{'var(--green2)' if pc_float < 1 else 'var(--red2)'}">
            {ctx['put_call_ratio']}
          </div>
          <div class="oc-sub">{'Call主导' if pc_float < 1 else 'Put主导'}</div>
        </div>
        <div class="opt-card oc-info">
          <div class="oc-label">总开仓量</div>
          <div class="oc-val" style="color:var(--cyan2)">{oi_str}</div>
          <div class="oc-sub">{oi_delta_sub}</div>
        </div>
        <div class="opt-card oc-neut">
          <div class="oc-label">IV Skew</div>
          <div class="oc-val" style="color:var(--gold2)">{iv_skew_display}</div>
          <div class="oc-sub">{otm_sub}</div>
        </div>
        <div class="opt-card oc-bull">
          <div class="oc-label">看涨异动</div>
          <div class="oc-val" style="color:var(--green2)">{bull_unusual_count}</div>
          <div class="oc-sub">vs {bear_unusual_count} 看跌</div>
        </div>
        <div class="opt-card oc-info">
          <div class="oc-label">IV Rank</div>
          <div class="oc-val" style="color:{iv_rank_color}">{iv_rank_display}</div>
          <div class="oc-sub">{iv_rank_sub}</div>
        </div>
        <div class="opt-card {gex_card_class}">
          <div class="oc-label">GEX</div>
          <div class="oc-val" style="color:{gex_color};font-size:18px;">{gex_display}</div>
          <div class="oc-sub">{gex_sub}</div>
          {gex_extra_html}
        </div>
      </div>

      {iv_term_html}
      {_gex_enhance_html}
      {_vol_surface_html}
      {_skew_alerts_html}

      <div class="levels-grid" style="margin-bottom:16px;">
        <div class="level-block support">
          <h4>📗 支撑位（高OI Put）</h4>
          {support_rows or '<div class="level-row"><span class="level-meta">暂无数据</span></div>'}
        </div>
        <div class="level-block resistance">
          <h4>📕 阻力位（高OI Call）</h4>
          {resistance_rows or '<div class="level-row"><span class="level-meta">暂无数据</span></div>'}
        </div>
      </div>

      <div style="margin-bottom:14px;">
        <div style="font-size:12px;font-weight:700;color:var(--text2);margin-bottom:8px;">异常期权流（成交量排名）</div>
        {expiry_row_html}
        <div class="unusual-list">{unusual_items_html}</div>
      </div>

      <div class="prose">{reasoning.get('options', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH5 -->
  <div class="section" id="ch5">
    <div class="section-header">
      <span class="ch-num">CH5</span>
      <span class="section-icon">🌍</span>
      <span class="section-title">第五章 · 宏观环境与情绪面</span>
      <span class="section-badge {'badge-bear' if fg != 'N/A' and int(fg) < 30 else 'badge-neutral'}">
        F&G = {fg} · {fg_label}
      </span>
    </div>
    <div class="section-body">
      <div class="macro-grid" style="margin-bottom:16px;">
        <div class="opt-card oc-neut">
          <div class="oc-label">Fear & Greed</div>
          <div class="oc-val" style="color:{fg_color}">{fg}</div>
          <div class="oc-sub">{fg_label}</div>
        </div>
        <div class="opt-card oc-bull">
          <div class="oc-label">GuardBee</div>
          <div class="oc-val" style="color:{'var(--green2)' if float(ctx['guard'].get('score',5)) > 5.5 else 'var(--red2)'}">
            {fmt_score(ctx['guard'].get('score'))}
          </div>
          <div class="oc-sub">{'宏观顺风' if float(ctx['guard'].get('score',5)) > 5.5 else '宏观阻力'}</div>
        </div>
        <div class="opt-card oc-info">
          <div class="oc-label">共振加成</div>
          <div class="oc-val" style="color:{'var(--purple2)' if res_detected else 'var(--text3)'}">
            {'+' + str(res_boost) + '%' if res_detected else '0%'}
          </div>
          <div class="oc-sub">置信提升</div>
        </div>
        <div class="opt-card oc-neut">
          <div class="oc-label">Reddit 热度</div>
          <div class="oc-val" style="color:var(--blue2)">
            {ctx['reddit'].get('rank', 'N/A')}
          </div>
          <div class="oc-sub">{ctx['reddit'].get('mentions','N/A')}次提及</div>
        </div>
        {vix_card_html}
        {congress_card_html}
      </div>
      {ctx.get("cboe_card_html", "")}
      <div class="prose">{reasoning.get('macro', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH6 -->
  <div class="section" id="ch6">
    <div class="section-header">
      <span class="ch-num">CH6</span>
      <span class="section-icon">🎯</span>
      <span class="section-title">第六章 · 五情景推演与期望值</span>
      <span class="section-badge {'badge-bull' if '+' in ev_str else 'badge-bear'}">EV = {ev_str} · {'偏多' if '+' in ev_str else '偏空'}</span>
    </div>
    <div class="section-body">
      <div class="scenario-grid">
        <div class="sc-card sc-bull">
          <div class="sc-name">🚀 情景A · 最强看涨</div>
          <div class="sc-prob">突破阻力 + 催化剂超预期 · 概率 {probs[0]*100:.0f}%</div>
          <div class="sc-price">${sc_a_lo:.0f}–${sc_a_hi:.0f}</div>
          <div class="sc-gain" style="color:var(--green2)">ML 7日预期 {ctx['ml_7d']:+.1f}%</div>
          <div class="sc-note">{sc_a_note}</div>
        </div>
        <div class="sc-card sc-bull">
          <div class="sc-name">📈 情景B · 温和看涨</div>
          <div class="sc-prob">催化剂达预期 · 概率 {probs[1]*100:.0f}%</div>
          <div class="sc-price">${sc_b_lo:.0f}–${sc_b_hi:.0f}</div>
          <div class="sc-gain" style="color:var(--green2)">P/C={ctx['put_call_ratio']} 支持</div>
          <div class="sc-note">{sc_b_note}</div>
        </div>
        <div class="sc-card sc-meh">
          <div class="sc-name">📉 情景C · 温和看跌</div>
          <div class="sc-prob">催化剂不达预期 · 概率 {probs[2]*100:.0f}%</div>
          <div class="sc-price">${sc_c_lo:.0f}–${sc_c_hi:.0f}</div>
          <div class="sc-gain" style="color:var(--gold2)">支撑 ${supports[0]['strike'] if supports else 'N/A'}</div>
          <div class="sc-note">{sc_c_note}</div>
        </div>
        <div class="sc-card sc-bear">
          <div class="sc-name">💥 情景D · 极端风险</div>
          <div class="sc-prob">催化剂暴雷 + 宏观恶化 · 概率 {probs[3]*100:.0f}%</div>
          <div class="sc-price">${sc_d_lo:.0f}–${sc_d_hi:.0f}</div>
          <div class="sc-gain" style="color:var(--red2)">高风险情景</div>
          <div class="sc-note">{sc_d_note}</div>
        </div>
      </div>

      {trade_grid_html}

      <div class="ev-box">
        <div class="ev-left">
          <div class="label">期望收益率（EV）</div>
          <div class="big">{ev_str}</div>
          <div style="font-size:11px;color:var(--text2);margin-top:3px;">综合胜率 {win_rate:.1f}%{win_rate_caveat}</div>
        </div>
        <div class="ev-right">
          <strong>ML 预测：</strong>7日 {ctx['ml_7d']:+.1f}% · 30日 {ctx['ml_30d']:+.1f}%<br><br>
          {'正期望值在共振触发环境中出现，进一步确认信号有效性。' if '+' in ev_str else '负期望值提示谨慎，建议小仓位或观望。'}
        </div>
      </div>

      {ctx.get('strategy_backtest_html', '')}

      <div class="prose" style="margin-top:14px;">{reasoning.get('scenario', '<p>分析生成中...</p>')}</div>
    </div>
  </div>

  <!-- CH7 -->
  <div class="section" id="ch7">
    <div class="section-header">
      <span class="ch-num">CH7</span>
      <span class="section-icon">⚠️</span>
      <span class="section-title">第七章 · 主要风险清单</span>
      <span class="section-badge badge-neutral">{len(smart_risks)} 项风险信号</span>
    </div>
    <div class="section-body">
      <div class="risk-list">
        {risk_items_html}
      </div>
      <div class="divider"></div>
      <div class="prose">{reasoning.get('risk', '')}</div>
      {tracking_tasks_html}
    </div>
  </div>

  <!-- CH8 · FF6 因子归因（有数据时显示，无数据时节点隐藏）-->
  {'<div class="section" id="ch8"><div class="section-header"><span class="ch-num">CH8</span><span class="section-icon">📐</span><span class="section-title">第八章 · Fama-French 6 因子 Alpha 归因</span></div><div class="section-body">' + attribution_html + '</div></div>' if attribution_html else ''}

  <div class="disclaimer">
    ⚠️ <strong>免责声明：</strong>本报告由 Alpha Hive 量化蜂群系统 + Claude API 混合模式生成，仅供研究参考，不构成投资建议。期权交易存在归零风险，所有交易决策需自行判断和风控。<br>
    <span style="color:var(--text3);font-size:10px;margin-top:4px;display:block;">
      生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · Template C v3.0 · 混合模式 · 数据来源：本地 JSON + Claude API
    </span>
  </div>

</div>
</body>
</html>"""
    return html


# ── 自学习辅助函数 ───────────────────────────────────────────────────────────────

def _save_report_snapshot(ctx: dict, ticker: str, report_date: str, out_dir: Path) -> None:
    """保存报告快照供 feedback_loop 回溯学习（Gap 1）"""
    try:
        from feedback_loop import ReportSnapshot
        snap_dir = str(out_dir / "report_snapshots")
        snap = ReportSnapshot(ticker, report_date)
        snap.composite_score = float(ctx.get("final_score", 0.0) or 0.0)
        _dir = (ctx.get("direction") or "neutral").lower()
        snap.direction = "Long" if "bull" in _dir else ("Short" if "bear" in _dir else "Neutral")
        snap.entry_price = float(ctx.get("price", 0.0) or 0.0)
        snap.agent_votes = {
            "ScoutBeeNova":       float(ctx["scout"].get("score",   0) or 0),
            "BuzzBeeWhisper":     float(ctx["buzz"].get("score",    0) or 0),
            "OracleBeeEcho":      float(ctx["oracle"].get("score",  0) or 0),
            "ChronosBeeHorizon":  float(ctx["chronos"].get("score", 0) or 0),
            "RivalBeeVanguard":   float(ctx["rival"].get("score",   0) or 0),
            "GuardBeeSentinel":   float(ctx["guard"].get("score",   0) or 0),
            "BearBeeContrarian":  float(ctx["bear"].get("score",    0) or 0),
        }
        fname = snap.save_to_json(snap_dir)
        print(f"📸 预测快照已保存: {Path(fname).name}")
    except Exception as e:
        print(f"⚠️  快照保存失败（不影响报告）: {e}")


def _run_outcome_backfill(out_dir: Path) -> None:
    """回填历史预测的实际价格（T+1/T+7/T+30）（Gap 2）"""
    try:
        from outcomes_fetcher import OutcomesFetcher
        snap_dir = str(out_dir / "report_snapshots")
        if not Path(snap_dir).exists():
            return
        fetcher = OutcomesFetcher(snapshots_dir=snap_dir)
        stats = fetcher.run()
        if stats.get("updated", 0) > 0:
            print(f"🔄 价格回填: {stats['updated']}/{stats['scanned']} 个快照已更新")
    except Exception as e:
        print(f"⚠️  价格回填失败（不影响报告）: {e}")


def _load_ticker_accuracy(ticker: str, out_dir: Path) -> dict:
    """读取该 ticker 的历史预测准确率（Gap 3），含 Sharpe / PF / 最大连败"""
    try:
        from feedback_loop import BacktestAnalyzer
        snap_dir = str(out_dir / "report_snapshots")
        analyzer = BacktestAnalyzer(directory=snap_dir)
        snaps = analyzer.get_snapshots_by_ticker(ticker)
        if not snaps:
            return {}
        t7_snaps = [s for s in snaps if s.actual_price_t7 is not None and s.entry_price]
        if not t7_snaps:
            return {"n_snapshots": len(snaps), "pending": True}
        wins = 0
        total_ret = 0.0
        direction_adjusted_returns = []
        gross_profit = 0.0
        gross_loss = 0.0
        max_consec_loss = 0
        cur_consec_loss = 0
        n = len(t7_snaps)
        for s in t7_snaps:
            ret = (s.actual_price_t7 - s.entry_price) / s.entry_price * 100
            total_ret += ret
            is_win = (s.direction == "Long" and ret > 0) or (s.direction == "Short" and ret < 0)
            if is_win:
                wins += 1
                cur_consec_loss = 0
            else:
                cur_consec_loss += 1
                max_consec_loss = max(max_consec_loss, cur_consec_loss)
            # Direction-adjusted return
            adj_ret = ret if s.direction == "Long" else -ret
            direction_adjusted_returns.append(adj_ret)
            if adj_ret > 0:
                gross_profit += adj_ret
            else:
                gross_loss += abs(adj_ret)
        # Sharpe ratio (annualized, 252/7 periods)
        sharpe = 0.0
        if len(direction_adjusted_returns) >= 2:
            periods_per_year = 252 / 7
            rf_per_period = 0.05 / periods_per_year
            excess = [r / 100.0 - rf_per_period for r in direction_adjusted_returns]
            mean_ex = sum(excess) / len(excess)
            var_ex = sum((x - mean_ex) ** 2 for x in excess) / (len(excess) - 1)
            std_ex = var_ex ** 0.5
            if std_ex > 0:
                sharpe = round((mean_ex / std_ex) * (periods_per_year ** 0.5), 3)
        # Profit Factor
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0
        return {
            "n": n,
            "win_rate": round(wins / n * 100, 1),
            "avg_ret_7d": round(total_ret / n, 2),
            "sharpe": sharpe,
            "profit_factor": profit_factor,
            "max_consec_loss": max_consec_loss,
        }
    except Exception:
        return {}


def _render_accuracy_card(accuracy: dict) -> str:
    """渲染历史准确率卡片 HTML（Gap 3），含 Sharpe / PF / 最大连败"""
    if not accuracy:
        return ""
    if accuracy.get("pending"):
        n = accuracy.get("n_snapshots", 0)
        return (
            '<div style="margin-top:12px;padding:10px 14px;background:var(--bg2);'
            'border-radius:8px;border:1px solid var(--border1);font-size:12px;color:var(--text2);">'
            f'📚 历史预测记录：{n} 条 · T+7 实际价格待回填</div>'
        )
    n   = accuracy["n"]
    wr  = accuracy["win_rate"]
    ar  = accuracy["avg_ret_7d"]
    sharpe = accuracy.get("sharpe", 0)
    pf = accuracy.get("profit_factor", 0)
    mcl = accuracy.get("max_consec_loss", 0)
    wr_color = "var(--green2)" if wr >= 60 else ("var(--gold2)" if wr >= 50 else "var(--red2)")
    ar_color = "var(--green2)" if ar >= 0 else "var(--red2)"
    sh_color = "var(--green2)" if sharpe > 0.5 else ("var(--gold2)" if sharpe > 0 else "var(--red2)")
    pf_color = "var(--green2)" if pf > 1.5 else ("var(--gold2)" if pf > 1.0 else "var(--red2)")
    mcl_color = "var(--green2)" if mcl <= 3 else ("var(--gold2)" if mcl <= 5 else "var(--red2)")
    # 第二行：新增指标
    _row2 = ""
    if sharpe != 0 or pf != 0:
        _pf_str = f"{pf:.2f}" if pf < 100 else "∞"
        _row2 = (
            '<div style="display:flex;gap:16px;margin-top:4px;align-items:center;">'
            f'<span style="font-size:11px;">Sharpe <strong style="color:{sh_color};">{sharpe:.2f}</strong></span>'
            f'<span style="font-size:11px;">PF <strong style="color:{pf_color};">{_pf_str}</strong></span>'
            f'<span style="font-size:11px;">最大连败 <strong style="color:{mcl_color};">{mcl}</strong></span>'
            '</div>'
        )
    return (
        '<div style="margin-top:12px;padding:10px 14px;background:var(--bg2);'
        'border-radius:8px;border:1px solid var(--border1);">'
        f'<span style="font-size:11px;color:var(--text3);font-weight:600;">'
        f'📚 历史回测（{n} 份报告 · T+7）</span>'
        '<div style="display:flex;gap:20px;margin-top:6px;align-items:center;">'
        f'<span style="font-size:13px;">方向胜率 <strong style="color:{wr_color};">{wr}%</strong></span>'
        f'<span style="font-size:13px;">平均收益 <strong style="color:{ar_color};">{ar:+.2f}%</strong></span>'
        f'<span style="font-size:11px;color:var(--text3);">{n} 样本</span>'
        f'</div>{_row2}</div>'
    )


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Alpha Hive · 混合模式深度报告生成器 · Template C v3.0")
    parser.add_argument("--ticker", required=True, help="股票代码，如 NVDA、VKTX")
    parser.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD（默认最新）")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM，使用本地生成文本")
    parser.add_argument("--use-llm", action="store_true", help="显式启用 LLM（claude-opus-4-6），需要 API Key，预计 $0.3~0.6/次")
    parser.add_argument("--out-dir", default=None, help="输出目录（默认 Alpha Hive 文件夹）")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    out_dir = Path(args.out_dir) if args.out_dir else OUTPUT_DIR

    print(f"\n🐝 Alpha Hive · generate_deep v{VERSION} 混合报告生成器")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"📊 标的: {ticker}")

    # 0. 回填历史预测实际价格（Gap 2 · 自学习闭环）
    _run_outcome_backfill(out_dir)

    # 1. 加载 JSON
    try:
        json_path = find_latest_json(ticker, args.date)
        print(f"📂 数据文件: {json_path.name}")
        data = load_json(json_path)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    # 1b. 数据新鲜度保护：JSON 超过 1 个交易日则拒绝生成，防止覆盖已有的好报告
    from datetime import datetime as _dt_freshness, timedelta as _td_freshness
    try:
        _json_date_str = json_path.stem.split("-ml-")[1]  # e.g. "2026-03-20"
        _json_date = _dt_freshness.strptime(_json_date_str, "%Y-%m-%d").date()
        _today = _dt_freshness.now().date()
        _stale_days = (_today - _json_date).days
        # 允许 1 天容差（周一用周五数据）；周末最多差 3 天
        _is_weekend_grace = _today.weekday() == 0 and _stale_days <= 3  # 周一容忍周五数据
        if _stale_days > 1 and not _is_weekend_grace:
            print(f"⚠️  JSON 数据已过期 {_stale_days} 天（{_json_date_str}），跳过生成以避免覆盖已有报告。")
            print(f"   请先运行蜂群扫描获取今天的数据：python3 run_daily_scan.py && python3 generate_ml_report.py")
            sys.exit(0)
    except (IndexError, ValueError):
        pass  # 文件名解析失败时不拦截

    # 2. 提取数据
    print("🔍 提取结构化数据...")
    ctx = extract(data)
    ctx["_raw_data"] = data   # 保留原始 JSON 供 chart_engine 使用
    print(f"   评分: {ctx['final_score']:.2f} | 方向: {ctx['direction_zh']} | 共振: {'✅' if ctx['resonance'].get('resonance_detected') else '○'}")

    # 2b-pre. GEX 补算：JSON 采集时若 yfinance 未返回价格导致 dealer_gex 缺失，
    #         在报告生成阶段用 ctx['price'] 实时重算，保证 CH4 GEX 卡片有数据
    print("📐 检查 GEX 数据...")
    _try_compute_gex(ctx)

    # 2a-2. SABR 波动率曲面分析（CH4 增强）
    ctx["vol_surface_html"] = ""
    ctx["skew_alerts_html"] = ""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from vol_surface import VolSurface, format_surface_for_report, format_skew_alert
        print("   📐 SABR 波动率曲面...", end="", flush=True)
        # 从 JSON 原始数据中提取期权链，构造 build_surface 所需格式
        _raw = ctx.get("_raw_data") or ctx.get("_raw") or {}
        _odet_raw = (_raw.get("swarm_results", {}).get("agent_details", {})
                     .get("OracleBeeEcho", {}).get("details", {}))
        _chain_raw = _odet_raw.get("options_chain", [])
        if _chain_raw:
            _vs = VolSurface(ctx["ticker"])
            _vs.build_surface(_chain_raw)  # 返回 None，数据存在 _vs.slices
            if _vs.slices:
                ctx["vol_surface_html"] = format_surface_for_report(_vs)
                _anomalies = _vs.detect_surface_anomalies()
                if _anomalies:
                    _alert_parts = [format_skew_alert(a) for a in _anomalies[:3]]
                    ctx["skew_alerts_html"] = "\n".join(
                        f'<div style="margin-bottom:8px;padding:8px 12px;background:rgba(239,68,68,0.08);'
                        f'border:1px solid rgba(239,68,68,0.25);border-radius:6px;font-size:11px;'
                        f'color:var(--red2);">⚠️ {a}</div>' for a in _alert_parts if a
                    )
                    print(f" ✅  ({len(_vs.slices)} 到期日, {len(_anomalies)} 异常)")
                else:
                    print(f" ✅  ({len(_vs.slices)} 到期日, 无异常)")
            else:
                print(" ⏭  SABR 校准失败")
        else:
            print(" ⏭  无期权链数据")
    except Exception as _e_vs:
        print(f" ⏭  跳过: {_e_vs}")

    # 2a-3. CBOE 市场指标（CH5 增强）
    ctx["cboe_card_html"] = ""
    try:
        from cboe_fetcher import CBOEDailyFetcher, format_cboe_for_macro_card
        print("   📊 CBOE 市场指标...", end="", flush=True)
        _cboe = CBOEDailyFetcher()
        _cboe_data = _cboe.fetch_all()
        if _cboe_data and any(v for v in _cboe_data.values() if v):
            ctx["cboe_card_html"] = format_cboe_for_macro_card(_cboe_data)
            _cboe_parts = []
            if _cboe_data.get("put_call_ratio"):
                _cboe_parts.append(f"P/C={_cboe_data['put_call_ratio'].get('value', 'N/A')}")
            if _cboe_data.get("skew"):
                _cboe_parts.append(f"SKEW={_cboe_data['skew'].get('value', 'N/A')}")
            if _cboe_data.get("vvix"):
                _cboe_parts.append(f"VVIX={_cboe_data['vvix'].get('value', 'N/A')}")
            print(f" ✅  ({' · '.join(_cboe_parts)})")
        else:
            print(" ⏭  无数据")
    except Exception as _e_cboe:
        print(f" ⏭  跳过: {_e_cboe}")

    # 2a-4. Quiver 国会交易补充（如 Scout 蜂未提供 congress 数据）
    if not ctx.get("congress") or (ctx["congress"].get("buy_count", 0) + ctx["congress"].get("sell_count", 0)) == 0:
        try:
            from quiver_fetcher import QuiverFetcher
            print("   🏛️ Quiver 国会交易...", end="", flush=True)
            _qf = QuiverFetcher()
            _cong_sig = _qf.calculate_congressional_signal(ticker)
            if _cong_sig and (_cong_sig.get("buy_count", 0) + _cong_sig.get("sell_count", 0)) > 0:
                ctx["congress"] = _cong_sig
                print(f" ✅  ({_cong_sig.get('buy_count',0)}买/{_cong_sig.get('sell_count',0)}卖)")
            else:
                print(" ⏭  无交易")
        except Exception as _e_qv:
            print(f" ⏭  跳过: {_e_qv}")

    # 2a-5. FinRL 权重建议（CH1 增强，advisory only）
    ctx["rl_insight_html"] = ""
    try:
        from finrl_bridge import FinRLBridge
        _snap_dir = str(out_dir / "report_snapshots")
        if Path(_snap_dir).exists():
            print("   🤖 FinRL 权重建议...", end="", flush=True)
            _rl = FinRLBridge()
            _rl_features = _rl.prepare_features(_snap_dir)
            if _rl_features and len(_rl_features.get("dates", [])) >= 30:
                _rl_policy = _rl.train_weight_policy(_rl_features)
                _rl_suggested = _rl_policy.get("suggested_weights", {})
                _rl_conf = _rl_policy.get("confidence", 0)
                _rl_method = _rl_policy.get("method", "unknown")
                if _rl_suggested and _rl_conf > 0.3:
                    # 找出偏差最大的 agent
                    _rl_diffs = []
                    for _agent, _w in sorted(_rl_suggested.items(), key=lambda x: x[1], reverse=True):
                        _rl_diffs.append(f"{_agent.replace('Bee','')}: {_w:.0%}")
                    _rl_top3 = " · ".join(_rl_diffs[:3])
                    _rl_conf_color = "var(--green2)" if _rl_conf > 0.6 else "var(--gold2)"
                    ctx["rl_insight_html"] = (
                        f'<div style="margin-top:12px;padding:10px 14px;background:rgba(59,130,246,0.08);'
                        f'border:1px solid rgba(59,130,246,0.25);border-radius:8px;">'
                        f'<span style="font-size:11px;font-weight:700;color:var(--blue2);">🤖 RL 权重建议（{_rl_method}）</span>'
                        f'<div style="font-size:12px;color:var(--text2);margin-top:4px;">{_rl_top3}</div>'
                        f'<div style="font-size:10px;color:var(--text3);margin-top:3px;">'
                        f'信心度 <span style="color:{_rl_conf_color};font-weight:700;">{_rl_conf:.0%}</span> '
                        f'· 仅供参考，不自动应用 · 基于 {len(_rl_features["dates"])} 份快照</div>'
                        f'</div>'
                    )
                    print(f" ✅  (信心{_rl_conf:.0%}, {_rl_method})")
                else:
                    print(f" ⏭  信心不足({_rl_conf:.0%})")
            else:
                _n_snaps = len(_rl_features.get("dates", [])) if _rl_features else 0
                print(f" ⏭  快照不足({_n_snaps}/30)")
    except Exception as _e_rl:
        print(f" ⏭  跳过: {_e_rl}")

    # 2a-6. 期权策略回测（CH6 增强）
    ctx["strategy_backtest_html"] = ""
    try:
        from options_backtester import OptionsBacktester
        _snap_dir = str(out_dir / "report_snapshots")
        if Path(_snap_dir).exists():
            print("   📋 期权策略回测...", end="", flush=True)
            _bt = OptionsBacktester(snapshots_dir=_snap_dir)
            _bt_results = _bt.backtest_all_strategies(horizon="t7")
            _bt_regime = _bt.find_best_strategy_by_regime()
            if _bt_results:
                # 找当前政体的最优策略
                _cur_regime = (ctx.get("market_regime") or {}).get("overall_regime", "neutral")
                _regime_key = _cur_regime.lower().replace(" ", "_")
                _best_strat = _bt_regime.get(_regime_key, {})
                _best_name = _best_strat.get("strategy", "")
                _best_wr = _best_strat.get("win_rate", 0)
                _best_ret = _best_strat.get("avg_return_pct", 0)
                # 生成全策略概览 HTML
                _strat_rows = ""
                for _sn, _sr in _bt_results.items():
                    _sr_d = _sr.to_dict() if hasattr(_sr, 'to_dict') else _sr
                    _s_wr = _sr_d.get("win_rate", 0) * 100
                    _s_ret = _sr_d.get("avg_return_pct", _sr_d.get("avg_return", 0))
                    _s_n = _sr_d.get("num_trades", 0)
                    _s_pf = _sr_d.get("profit_factor", 0)
                    if _s_n == 0:
                        continue
                    _is_best = (_sn == _best_name)
                    _row_bg = "rgba(16,185,129,0.08)" if _is_best else "transparent"
                    _strat_rows += (
                        f'<tr style="background:{_row_bg};">'
                        f'<td style="padding:4px 8px;font-size:11px;color:var(--text2);font-weight:{"700" if _is_best else "400"};">'
                        f'{"⭐ " if _is_best else ""}{_sn}</td>'
                        f'<td style="padding:4px 8px;font-size:11px;text-align:center;color:{"var(--green2)" if _s_wr >= 50 else "var(--red2)"}">{_s_wr:.0f}%</td>'
                        f'<td style="padding:4px 8px;font-size:11px;text-align:center;color:{"var(--green2)" if _s_ret >= 0 else "var(--red2)"}">{_s_ret:+.1f}%</td>'
                        f'<td style="padding:4px 8px;font-size:11px;text-align:center;color:var(--text3);">{_s_pf:.2f}</td>'
                        f'<td style="padding:4px 8px;font-size:11px;text-align:center;color:var(--text3);">{_s_n}</td>'
                        f'</tr>'
                    )
                if _strat_rows:
                    _regime_note = (
                        f'当前政体 <strong style="color:var(--blue2);">{_cur_regime}</strong>'
                        f' → 最优: <strong style="color:var(--green2);">{_best_name}</strong>'
                        f' (胜率{_best_wr*100:.0f}%, 均收{_best_ret:+.1f}%)'
                    ) if _best_name else "无政体匹配"
                    ctx["strategy_backtest_html"] = (
                        f'<div style="margin-top:14px;padding:12px 14px;background:var(--bg2);'
                        f'border:1px solid var(--border);border-radius:8px;">'
                        f'<div style="font-size:11px;font-weight:700;color:var(--text2);margin-bottom:8px;">'
                        f'📋 期权策略回测（T+7, 历史快照）</div>'
                        f'<div style="font-size:11px;color:var(--text2);margin-bottom:8px;">{_regime_note}</div>'
                        f'<table style="width:100%;border-collapse:collapse;border-spacing:0;">'
                        f'<tr style="border-bottom:1px solid var(--border);">'
                        f'<th style="padding:4px 8px;font-size:10px;color:var(--text3);text-align:left;">策略</th>'
                        f'<th style="padding:4px 8px;font-size:10px;color:var(--text3);">胜率</th>'
                        f'<th style="padding:4px 8px;font-size:10px;color:var(--text3);">均收</th>'
                        f'<th style="padding:4px 8px;font-size:10px;color:var(--text3);">PF</th>'
                        f'<th style="padding:4px 8px;font-size:10px;color:var(--text3);">样本</th>'
                        f'</tr>{_strat_rows}</table>'
                        f'</div>'
                    )
                    print(f" ✅  ({len(_bt_results)} 策略, 最优: {_best_name})")
                else:
                    print(" ⏭  无交易记录")
            else:
                print(" ⏭  回测无结果")
    except Exception as _e_bt:
        print(f" ⏭  跳过: {_e_bt}")

    # 2b. 加载昨日 JSON + T-7 JSON，构建 Delta 上下文
    prev_path = find_prev_json(ticker, json_path, days_back=1)
    t7_path   = find_prev_json(ticker, json_path, days_back=7)
    ctx["prev"] = extract_simple(load_json(prev_path)) if prev_path else None
    ctx["t7"]   = extract_simple(load_json(t7_path))   if t7_path   else None

    # 计算 delta_context 供 LLM 推理使用
    delta_context = ""
    if ctx["prev"]:
        prev = ctx["prev"]
        score_diff = ctx["final_score"] - prev["final_score"]
        bee_map = [
            ("ChronosBee", float(ctx["chronos"].get("score",0) or 0), prev["chronos"]),
            ("RivalBee",   float(ctx["rival"].get("score",0)   or 0), prev["rival"]),
            ("OracleBee",  float(ctx["oracle"].get("score",0)  or 0), prev["oracle"]),
            ("BuzzBee",    float(ctx["buzz"].get("score",0)    or 0), prev["buzz"]),
            ("ScoutBee",   float(ctx["scout"].get("score",0)   or 0), prev["scout"]),
            ("GuardBee",   float(ctx["guard"].get("score",0)   or 0), prev["guard"]),
            ("BearBee",    float(ctx["bear"].get("score",0)    or 0), prev["bear"]),
        ]
        delta_lines = [f"  {n}: {ps:.1f} → {ts:.1f} ({ts-ps:+.1f})"
                       for n, ts, ps in bee_map if abs(ts - ps) >= 0.3]
        extras = []
        try:
            if prev.get("put_call_ratio") and ctx.get("put_call_ratio") not in (None, "N/A"):
                pd = float(ctx["put_call_ratio"]) - float(prev["put_call_ratio"])
                extras.append(f"  P/C: {float(prev['put_call_ratio']):.2f} → {float(ctx['put_call_ratio']):.2f} ({pd:+.2f})")
        except Exception:
            pass
        try:
            if prev.get("iv_skew") and ctx.get("iv_skew"):
                sd = float(ctx["iv_skew"]) - float(prev["iv_skew"])
                extras.append(f"  IV Skew: {float(prev['iv_skew']):.2f} → {float(ctx['iv_skew']):.2f} ({sd:+.2f})")
        except Exception:
            pass
        try:
            _iv_now  = float(ctx.get("iv_current") or 0)
            _iv_prev = float(prev.get("iv_current") or 0)
            if _iv_now and _iv_prev and abs(_iv_now - _iv_prev) >= 0.5:
                d = _iv_now - _iv_prev
                extras.append(f"  IV Current: {_iv_prev:.1f}% → {_iv_now:.1f}% ({d:+.1f}%)")
        except Exception:
            pass
        try:
            _rg_now  = (ctx.get("market_regime") or {}).get("overall_regime", "")
            _rg_prev = (prev.get("market_regime") or {}).get("overall_regime", "")
            if _rg_now and _rg_prev and _rg_now != _rg_prev:
                extras.append(f"  政体变化: {_rg_prev} → {_rg_now} ⚠️")
        except Exception:
            pass
        try:
            _oi_now  = ctx.get("total_oi") or 0
            _oi_prev = prev.get("total_oi") or 0
            if _oi_now and _oi_prev and _oi_prev > 0:
                ctx["oi_delta"]     = int(_oi_now - _oi_prev)
                ctx["oi_delta_pct"] = round((_oi_now / _oi_prev - 1) * 100, 2)
                extras.append(
                    f"  总OI: {_oi_prev:,.0f} → {_oi_now:,.0f} "
                    f"({ctx['oi_delta']:+,}, {ctx['oi_delta_pct']:+.1f}%)"
                )
        except Exception:
            pass
        delta_context = (
            f"【昨日对比】综合评分 {prev['final_score']:.2f} → {ctx['final_score']:.2f} ({score_diff:+.2f})\n"
            + ("\n".join(delta_lines) if delta_lines else "  各蜂评分变化均 <0.3，基本持平")
            + ("\n" + "\n".join(extras) if extras else "")
        )
        print(f"   📊 Delta: {score_diff:+.2f} | 昨日文件: {prev_path.name if prev_path else '无'}")
    ctx["delta_context"] = delta_context

    # 2b-⑧. Thesis Break 闭环检查
    ctx["thesis_break_html"] = ""
    ctx["thesis_break_data"] = {}
    try:
        from market_intelligence import check_thesis_breaks
        _tb_pcr = ctx.get("put_call_ratio", 1.0)
        try:
            _tb_pcr = float(_tb_pcr)
        except (ValueError, TypeError):
            _tb_pcr = 1.0
        _tb_result = check_thesis_breaks(
            ticker=ticker,
            current_price=float(ctx.get("price") or 0.0),
            iv_current=float(ctx.get("iv_current") or 0.0),
            put_call_ratio=_tb_pcr,
            bear_signals=ctx.get("bear_signals", []),
            swarm_score=float(ctx.get("final_score") or 5.0),
        )
        ctx["thesis_break_data"] = _tb_result
        ctx["thesis_break_html"] = _tb_result.get("alert_html", "")
        if _tb_result.get("level"):   # "warning" or "stop_loss"
            _nconds = len(_tb_result.get("triggered_conditions", []))
            print(f"   ⚠️  Thesis Break: {_nconds} 个触发条件 → {_tb_result['level']}")
    except Exception as _e_tb:
        print(f"   ℹ️  Thesis Break 检查跳过: {_e_tb}")

    # 2c. 读取历史准确率（Gap 3 · 自学习反馈）
    _accuracy    = _load_ticker_accuracy(ticker, out_dir)
    accuracy_html = _render_accuracy_card(_accuracy)
    if _accuracy and not _accuracy.get("pending"):
        print(f"   📚 历史胜率: {_accuracy['win_rate']}% | 平均T+7收益: {_accuracy['avg_ret_7d']:+.2f}% ({_accuracy['n']} 样本)")

    print(f"   OracleBee: P/C={ctx['put_call_ratio']}, OI={ctx['total_oi']:,}, Skew={ctx['iv_skew']}")
    print(f"   催化剂: {len(ctx['catalysts'])} 个 | 异常流: {len(ctx['unusual_activity'])} 笔")
    _pre_conflicts = detect_conflicts(ctx)
    if _pre_conflicts:
        print(f"   ⚡ 检测到 {len(_pre_conflicts)} 个信号矛盾：" + " | ".join(
            c[2:c.index('↔')].strip() if '↔' in c else c[2:30] for c in _pre_conflicts
        ))

    # 2.5 实时新闻与情绪注入（Finnhub + Alpha Vantage）
    print(f"   📰 实时新闻数据...", end="", flush=True)
    ctx["live_news_block"] = fetch_live_news(ticker)
    if ctx["live_news_block"]:
        _news_lines = ctx["live_news_block"].count("\n")
        print(f" ✅  ({_news_lines} 行)")
    else:
        print(f" ⏭  跳过（无 Finnhub/AV Key 或网络不可用）")

    # 2.6 FF6 因子归因（提前计算，注入 ctx 供 CH1 prompt 读取）
    attribution_html = ""
    ctx["ff6_block"] = ""
    try:
        from factor_attribution import compute_factor_attribution, format_attribution_html as _fmt_attr
        print("   📐 FF6 因子归因...", end="", flush=True)
        _attr = compute_factor_attribution(ticker, lookback_days=252)
        attribution_html = _fmt_attr(_attr)
        if "error" not in _attr:
            _f = _attr.get("factors", {})
            _sig_factors = [
                f"β_{k}={v['loading']:+.2f}{v['sig']}"
                for k, v in _f.items() if abs(v["loading"]) > 0.2
            ]
            ctx["ff6_block"] = (
                f"【FF6 因子归因（{_attr['n_obs']}日）】"
                f"Alpha年化{_attr['alpha_annual']*100:+.1f}%"
                f"(t={_attr['alpha_t']:+.1f}{'，显著' if _attr['alpha_p']<0.05 else '，不显著'}) | "
                f"R²={_attr['r2']:.1%} | "
                f"{' '.join(_sig_factors[:4])} | "
                f"IR={_attr['information_ratio']:+.2f} | "
                f"风险类型:{_attr['risk_level']}"
            )
            print(f" ✅  Alpha={_attr['alpha_annual']*100:+.1f}% R²={_attr['r2']:.1%}")
        else:
            print(f" ⚠️ {_attr['error']}")
    except Exception as _e_attr:
        print(f" ⚠️ FF6 归因跳过: {_e_attr}")

    # 3. LLM 深度推理
    sections = ["swarm_analysis", "resonance", "catalyst", "options", "macro", "scenario", "risk"]
    reasoning = {}

    api_key = get_api_key()
    # ── LLM 模式必须显式 --use-llm，防止 API Key 存在就自动消费 Opus ──
    if args.no_llm:
        use_llm = False
    elif args.use_llm:
        if not api_key:
            print("❌ --use-llm 需要 ~/.anthropic_api_key，但未找到，降级为本地模式")
            use_llm = False
        else:
            est_cost = 0.45
            print(f"\n⚠️  即将调用 claude-opus-4-6，预计费用约 ${est_cost:.2f}")
            confirm = input("确认继续？[y/N] ").strip().lower()
            use_llm = (confirm == "y")
            if not use_llm:
                print("已取消，使用本地模式")
    else:
        # 默认：本地模式（不消费 API）
        use_llm = False

    if use_llm:
        print(f"\n🤖 Claude API 深度推理中（两步链式 + 跨章上下文）...")
        ctx["master_thesis"] = ""   # 初始化，CH1跑完后填入
        ctx["cross_context"] = ""   # 初始化，Phase 1.5 跑完后填入
        ctx["prev_chapters"] = []   # 滚动注入：每章跑完后追加摘要

        # Phase 1：CH1 先跑，提取核心论点作为后续章节的上下文
        print(f"   ✍️  swarm_analysis (Phase 1)...", end="", flush=True)
        reasoning["swarm_analysis"] = llm_reason(ctx, "swarm_analysis", api_key)
        ctx["master_thesis"] = extract_thesis(reasoning["swarm_analysis"])
        _thesis_preview = ctx["master_thesis"][:50]
        _thesis_suffix  = "..." if len(ctx["master_thesis"]) > 50 else ""
        print(f" ✅  → 论点: {_thesis_preview}{_thesis_suffix}")

        # Phase 1.5：生成跨章节锚点上下文（读全量 JSON 原始数据，直接推理）
        print(f"   🔗 cross_context (Phase 1.5)...", end="", flush=True)
        ctx["cross_context"] = llm_cross_context(ctx, api_key)
        if ctx["cross_context"]:
            _cx_preview = ctx["cross_context"][:60].replace("\n", " ")
            print(f" ✅  → {_cx_preview}...")
        else:
            print(" ⚠️  生成失败，各章节独立推理")

        # Phase 2：顺序执行，滚动注入前序章节摘要
        _ch_labels = {
            "resonance": "CH2共振", "catalyst": "CH3催化剂",
            "options": "CH4期权",   "macro":    "CH5宏观",
            "scenario": "CH6情景",  "risk":     "CH7风险",
        }
        for sec in ["resonance", "catalyst", "options", "macro", "scenario", "risk"]:
            print(f"   ✍️  {sec}（前序摘要 {len(ctx['prev_chapters'])} 条）...", end="", flush=True)
            reasoning[sec] = llm_reason(ctx, sec, api_key)
            # 提取本章首句结论，注入下一章上下文
            _summary = extract_chapter_summary(reasoning[sec])
            ctx["prev_chapters"].append(f"{_ch_labels[sec]}：{_summary}")
            print(" ✅")

        # Phase 3：情景结构化数据（JSON）
        print(f"   📊 scenario_data (JSON)...", end="", flush=True)
        ctx["llm_scenario"] = llm_scenario_data(ctx, api_key)
        print(f" ✅  ({len(ctx['llm_scenario'])} 字段)" if ctx["llm_scenario"] else " ⚠️ 降级到ML")
    else:
        ctx["master_thesis"] = ""
        ctx["cross_context"] = ""
        ctx["prev_chapters"] = []
        print(f"\n📝 本地叙事生成（本地模式）...")
        for sec in sections:
            reasoning[sec] = _local_fallback(ctx, sec)
        print("   ✅ 全部章节完成")

    # 4. 生成 HTML
    print("\n📄 渲染 Template C v3.0 HTML...")
    html = generate_html(ctx, reasoning, accuracy_html=accuracy_html,
                         attribution_html=attribution_html)

    # 5. 保存
    report_date = ctx["report_date"]
    out_file = out_dir / f"deep-{ticker}-{report_date}.html"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")

    # 6. 保存预测快照（Gap 1 · 供 feedback_loop 回溯学习）
    _save_report_snapshot(ctx, ticker, report_date, out_dir)

    print(f"\n✅ 报告已生成！")
    print(f"📁 {out_file}")
    print(f"🌐 用浏览器打开即可查看\n")


if __name__ == "__main__":
    main()
