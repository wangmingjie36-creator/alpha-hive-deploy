#!/usr/bin/env python3
"""
🐝 Alpha Hive · 数据收集器 · 方案B专用
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
把 analysis JSON 提炼成精简的 raw JSON，
粘贴到 Claude 对话里做深度推理 + 生成 Template C v2.0 HTML

用法:
  python3 collect_data.py NVDA
  python3 collect_data.py NVDA --date 2026-03-10
  python3 collect_data.py VKTX --all   # 扫描所有可用日期

输出:
  {TICKER}_raw.json  （保存到 Alpha Hive 文件夹）
  同时打印到终端 → 直接复制粘贴到 Claude 对话
"""

import json
import os
import sys
import glob
import argparse
import re
from datetime import datetime, date
from pathlib import Path

# ── 路径 ─────────────────────────────────────────────────────────────────────
ALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))
# 自动检测 Cowork VM session 路径
_SCRIPT_DIR = Path(__file__).resolve().parent
if _SCRIPT_DIR.name == "Alpha Hive" and str(_SCRIPT_DIR).startswith("/sessions/"):
    ALPHAHIVE_DIR = _SCRIPT_DIR
else:
    _VM_PATH = Path("/sessions/keen-magical-wright/mnt/Alpha Hive")
    if _VM_PATH.exists():
        ALPHAHIVE_DIR = _VM_PATH


def find_json(ticker: str, date_str: str = None) -> Path:
    pattern = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"找不到 {ticker} 的分析文件: {pattern}")
    if date_str:
        target = ALPHAHIVE_DIR / f"analysis-{ticker}-ml-{date_str}.json"
        if target.exists():
            return target
        print(f"⚠️  {date_str} 不存在，使用最新: {Path(files[-1]).name}")
    return Path(files[-1])


def find_daily_json(date_str: str) -> dict:
    """读取当日宏观总报（可选）"""
    path = ALPHAHIVE_DIR / f"alpha-hive-daily-{date_str}.json"
    if path.exists():
        with open(path) as f:
            d = json.load(f)
        return {
            "fear_greed": d.get("macro_context", {}).get("fear_greed_index"),
            "opportunities_count": len(d.get("opportunities", [])),
            "top_opportunities": [
                {"ticker": o.get("ticker"), "score": o.get("score"), "direction": o.get("direction")}
                for o in d.get("opportunities", [])[:5]
            ],
            "macro_summary": d.get("macro_context", {}).get("summary", ""),
        }
    return {}


def extract_raw(data: dict) -> dict:
    """提炼出所有关键字段，压缩成 Claude 友好的 JSON"""
    sr = data.get("swarm_results", {})
    ad = sr.get("agent_details", {})
    ml = data.get("ml_prediction", {})
    cr = data.get("combined_recommendation", {})
    aa = data.get("advanced_analysis", {})

    ticker = data.get("ticker", "???")
    report_date = data.get("timestamp", "")[:10]

    # ── 蜂群总体 ──────────────────────────────────────────────────────────────
    final_score = sr.get("final_score", 0)
    direction = sr.get("direction", "neutral")
    res = sr.get("resonance", {})

    # ── 各 Agent 核心数据 ─────────────────────────────────────────────────────
    def agent_core(bee_key):
        b = ad.get(bee_key, {})
        return {
            "score": round(float(b.get("score", 0)), 2),
            "direction": b.get("direction", "neutral"),
            "confidence": round(float(b.get("confidence", 0)), 2),
            "discovery": b.get("discovery", "")[:200],
        }

    # ── Oracle 期权数据 ────────────────────────────────────────────────────────
    oracle = ad.get("OracleBeeEcho", {})
    odet = oracle.get("details", {})
    key_levels = odet.get("key_levels", {})

    def fmt_levels(levels):
        return [
            {"strike": l["strike"], "oi": l["oi"], "iv_pct": round(l.get("iv", 0) * 100, 1)}
            for l in levels[:4]
        ]

    unusual = []
    for u in odet.get("unusual_activity", [])[:10]:
        unusual.append({
            "type": u.get("type", ""),
            "strike": u.get("strike"),
            "volume": int(u.get("volume", 0)),
            "bullish": u.get("bullish", False),
            "ratio": round(float(u.get("ratio", 0)), 2) if u.get("ratio") else None,
        })

    # ── Chronos 催化剂 ────────────────────────────────────────────────────────
    chronos = ad.get("ChronosBeeHorizon", {})
    cdet = chronos.get("details", {})
    seen = set()
    catalysts = []
    for c in cdet.get("catalysts", []):
        key = (c.get("event", ""), c.get("date", ""))
        if key not in seen:
            seen.add(key)
            catalysts.append({
                "event": c.get("event", ""),
                "date": c.get("date", ""),
                "days_until": c.get("days_until", 0),
                "importance": c.get("importance", "medium"),
                "description": c.get("description", "")[:100],
            })
    catalysts = catalysts[:6]

    # ── Buzz 情绪 ─────────────────────────────────────────────────────────────
    buzz = ad.get("BuzzBeeWhisper", {})
    bdet = buzz.get("details", {})
    fg_match = re.search(r'F&G[^\d]*(\d+)', buzz.get("discovery", ""))
    fg_score = int(fg_match.group(1)) if fg_match else None

    # ── Scout 基本面 ──────────────────────────────────────────────────────────
    scout = ad.get("ScoutBeeNova", {})
    sdet = scout.get("details", {})
    price = sdet.get("price")

    # ── Bear 逆向 ─────────────────────────────────────────────────────────────
    bear = ad.get("BearBeeContrarian", {})
    bear_signals = bear.get("details", {}).get("bearish_signals", [])

    # ── Guard 宏观 ────────────────────────────────────────────────────────────
    guard = ad.get("GuardBeeSentinel", {})

    # ── ML 预测 ───────────────────────────────────────────────────────────────
    ml_pred = ml.get("prediction", {})

    # ── 组装输出 ──────────────────────────────────────────────────────────────
    raw = {
        # 元信息
        "_meta": {
            "ticker": ticker,
            "report_date": report_date,
            "price": round(float(price), 2) if price else None,
            "generated_by": "collect_data.py · 方案B",
        },

        # 蜂群总览
        "swarm": {
            "final_score": round(float(final_score), 2),
            "direction": direction,
            "resonance": {
                "detected": res.get("resonance_detected", False),
                "dimensions": res.get("resonant_dimensions", []),
                "count": res.get("cross_dim_count", 0),
                "confidence_boost": res.get("confidence_boost", 0),
                "direction": res.get("direction", ""),
            },
            "agent_vote": sr.get("agent_breakdown", {}),
        },

        # 各蜂评分
        "agents": {
            "ChronosBee":  agent_core("ChronosBeeHorizon"),
            "RivalBee":    agent_core("RivalBeeVanguard"),
            "OracleBee":   agent_core("OracleBeeEcho"),
            "BuzzBee":     agent_core("BuzzBeeWhisper"),
            "ScoutBee":    agent_core("ScoutBeeNova"),
            "GuardBee":    agent_core("GuardBeeSentinel"),
            "BearBee":     agent_core("BearBeeContrarian"),
        },

        # 期权结构（核心）
        "options": {
            "put_call_ratio": odet.get("put_call_ratio"),
            "total_oi": odet.get("total_oi"),
            "iv_rank": odet.get("iv_rank"),
            "iv_current_pct": odet.get("iv_current"),
            "iv_skew_ratio": odet.get("iv_skew_ratio"),
            "iv_skew_signal": odet.get("iv_skew_signal"),
            "flow_direction": odet.get("flow_direction"),
            "options_score": odet.get("options_score"),
            "signal_summary": odet.get("signal_summary", ""),
            "gamma_squeeze_risk": odet.get("gamma_squeeze_risk"),
            "support_levels": fmt_levels(key_levels.get("support", [])),
            "resistance_levels": fmt_levels(key_levels.get("resistance", [])),
            "unusual_activity": unusual,
            "expiration_dates": odet.get("expiration_dates", [])[:3],
        },

        # 催化剂
        "catalysts": {
            "count": len(catalysts),
            "chronos_score": round(float(chronos.get("score", 0)), 2),
            "events": catalysts,
            "analyst_target": cdet.get("analyst_target"),
        },

        # ML 预测
        "ml": {
            "probability_pct": round(float(ml.get("input", {}).get("market_sentiment", 0) or
                                          ml_pred.get("probability", 0.5) * 100), 1),
            "expected_3d_pct": round(float(ml_pred.get("expected_3d", 0)), 2),
            "expected_7d_pct": round(float(ml_pred.get("expected_7d", 0)), 2),
            "expected_30d_pct": round(float(ml_pred.get("expected_30d", 0)), 2),
            "recommendation": ml.get("recommendation", ""),
        },

        # 宏观 / 情绪
        "macro": {
            "fear_greed_index": fg_score,
            "guard_score": round(float(guard.get("score", 0)), 2),
            "guard_direction": guard.get("direction", ""),
            "guard_discovery": guard.get("discovery", "")[:250],
            "sentiment_pct": bdet.get("sentiment_pct"),
            "reddit_rank": bdet.get("reddit", {}).get("rank"),
            "reddit_mentions": bdet.get("reddit", {}).get("mentions"),
            "volume_ratio": round(float(bdet.get("volume_ratio", 1)), 2),
            "buzz_discovery": buzz.get("discovery", "")[:200],
        },

        # 基本面
        "fundamentals": {
            "scout_score": round(float(scout.get("score", 0)), 2),
            "scout_direction": scout.get("direction", ""),
            "scout_discovery": scout.get("discovery", "")[:200],
            "crowding_score": round(float(sdet.get("crowding_score", 0)), 1),
            "momentum_5d": round(float(sdet.get("momentum_5d", 0)), 4),
            "overview": aa.get("overview", "")[:150],
        },

        # 逆向信号
        "bearish": {
            "bear_score": round(float(bear.get("score", 0)), 2),
            "bear_direction": bear.get("direction", ""),
            "bear_signals": bear_signals[:5],
            "bear_discovery": bear.get("discovery", "")[:200],
        },

        # 综合建议
        "recommendation": {
            "combined_probability_pct": cr.get("combined_probability"),
            "rating": cr.get("rating"),
            "action": cr.get("action"),
            "reasoning": cr.get("reasoning", ""),
        },
    }
    return raw


def main():
    parser = argparse.ArgumentParser(description="Alpha Hive 数据收集器 · 方案B")
    parser.add_argument("ticker", help="股票代码，如 NVDA、VKTX")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD（默认最新）")
    parser.add_argument("--all", action="store_true", help="列出所有可用日期")
    parser.add_argument("--quiet", action="store_true", help="不打印到终端，只保存文件")
    args = parser.parse_args()

    ticker = args.ticker.upper()

    # 列出所有可用日期
    if args.all:
        pattern = str(ALPHAHIVE_DIR / f"analysis-{ticker}-ml-*.json")
        files = sorted(glob.glob(pattern))
        if not files:
            print(f"❌ 没有找到 {ticker} 的数据文件")
            sys.exit(1)
        print(f"\n📅 {ticker} 可用日期：")
        for f in files:
            fname = Path(f).stem
            date_part = fname.replace(f"analysis-{ticker}-ml-", "")
            print(f"  {date_part}")
        return

    print(f"\n🐝 Alpha Hive 数据收集器 · 方案B")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        json_path = find_json(ticker, args.date)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print(f"📂 读取: {json_path.name}")
    with open(json_path) as f:
        data = json.load(f)

    raw = extract_raw(data)

    # 尝试补充宏观日报数据
    daily_data = find_daily_json(raw["_meta"]["report_date"])
    if daily_data and daily_data.get("fear_greed") and not raw["macro"]["fear_greed_index"]:
        raw["macro"]["fear_greed_index"] = daily_data["fear_greed"]
    if daily_data.get("macro_summary"):
        raw["macro"]["daily_macro_summary"] = daily_data["macro_summary"][:200]

    # 保存到文件
    out_path = ALPHAHIVE_DIR / f"{ticker}_raw.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    json_str = json.dumps(raw, ensure_ascii=False, indent=2)
    char_count = len(json_str)

    print(f"✅ 提炼完成!")
    print(f"   评分: {raw['swarm']['final_score']} | 方向: {raw['swarm']['direction']} | 共振: {'✅' if raw['swarm']['resonance']['detected'] else '○'}")
    print(f"   P/C: {raw['options']['put_call_ratio']} | OI: {raw['options']['total_oi']:,} | Skew: {raw['options']['iv_skew_ratio']}")
    print(f"   催化剂: {raw['catalysts']['count']} 个 | 异常流: {len(raw['options']['unusual_activity'])} 笔")
    print(f"   文件大小: {char_count:,} 字符 (~{char_count//4} tokens)")
    print(f"\n📁 已保存: {out_path}")

    if not args.quiet:
        print(f"\n{'━'*50}")
        print(f"📋 复制以下内容粘贴到 Claude 对话：")
        print(f"{'━'*50}\n")
        # 打印精简版（如果太大就只打印关键字段）
        if char_count > 8000:
            print(f"[文件较大，请直接上传 {out_path.name} 到 Claude 对话]")
            print(f"\n或者用以下精简版：")
            compact = json.dumps(raw, ensure_ascii=False, separators=(',', ':'))
            print(compact)
        else:
            print(json_str)
        print(f"\n{'━'*50}")
        print(f"💬 然后在对话里说：")
        print(f'   "请用以上数据生成 {ticker} 的 Template C v2.0 深度报告"')
        print(f"{'━'*50}\n")


if __name__ == "__main__":
    main()
