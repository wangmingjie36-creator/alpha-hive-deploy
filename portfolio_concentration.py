"""
🐝 Alpha Hive - 投资组合集中度分析 (P4)
扫描完成后检测高分标的的板块/因子重叠风险

功能：
- 30 日价格相关性矩阵（>0.7 = 高度相关警告）
- 板块集中度检测（>60% 同一板块 = 风险）
- 因子暴露分析（动量/波动率聚类）
- 给出分散化建议

免费数据：yfinance 历史价格 + config.WATCHLIST 板块信息
"""

import logging
import math
from typing import Dict, List, Optional

_log = logging.getLogger("alpha_hive.portfolio_concentration")


def analyze_concentration(
    swarm_results: Dict,
    watchlist: Dict,
    threshold_score: float = 6.0,
    top_n: int = 8,
) -> Dict:
    """
    分析高分标的的集中度风险

    Args:
        swarm_results: run_swarm_scan() 返回的蜂群结果 dict
        watchlist: config.WATCHLIST
        threshold_score: 最低分数过滤线
        top_n: 最多分析几个标的

    Returns:
        {
            "concentration_risk": "low"/"medium"/"high",
            "risk_score": float (0-10),
            "sector_breakdown": dict,
            "correlation_warnings": list,
            "factor_clusters": dict,
            "recommendations": list[str],
            "summary": str,
        }
    """
    # 过滤高分标的
    high_score = {
        t: d for t, d in swarm_results.items()
        if d.get("final_score", 0) >= threshold_score
    }
    if not high_score:
        return {
            "concentration_risk": "low",
            "risk_score": 0.0,
            "sector_breakdown": {},
            "correlation_warnings": [],
            "factor_clusters": {},
            "recommendations": ["暂无高分标的，无需评估集中度"],
            "summary": "无高分标的（分数 < 6.0）",
        }

    tickers = list(high_score.keys())[:top_n]

    # ---- 1. 板块集中度 ----
    sector_map = {}
    for t in tickers:
        sector = watchlist.get(t, {}).get("sector", "Unknown")
        sector_map.setdefault(sector, []).append(t)

    total = len(tickers)
    sector_breakdown = {s: {"tickers": ts, "pct": round(len(ts)/total*100, 1)}
                        for s, ts in sector_map.items()}

    max_sector_pct = max((v["pct"] for v in sector_breakdown.values()), default=0)
    max_sector = max(sector_breakdown, key=lambda s: sector_breakdown[s]["pct"], default="")

    # ---- 2. 价格相关性矩阵（使用 yfinance 30日收益率）----
    correlation_warnings = []
    returns_map = {}

    try:
        import yfinance as yf
        import yf_daily
        # v0.45.658：经唯一入口（一个月收益相关性，按日期对齐：帧与 actions=False 时逐位相同）
        data = yf_daily.download(yf, tickers, source="concentration", period="1mo", interval="1d",
                                 auto_adjust=True, progress=False)[0]
        if not data.empty:
            # pandas DataFame: columns may be MultiIndex or single
            if hasattr(data.columns, "levels"):
                close = data["Close"] if "Close" in data.columns.get_level_values(0) else data
            else:
                close = data

            pct_returns = close.pct_change().dropna()

            for i, t1 in enumerate(tickers):
                for t2 in tickers[i+1:]:
                    if t1 in pct_returns.columns and t2 in pct_returns.columns:
                        s1 = pct_returns[t1].dropna()
                        s2 = pct_returns[t2].dropna()
                        common = s1.index.intersection(s2.index)
                        if len(common) < 5:
                            continue
                        v1 = list(s1[common])
                        v2 = list(s2[common])
                        corr = _pearson(v1, v2)
                        if corr is not None and corr >= 0.70:
                            correlation_warnings.append({
                                "pair": f"{t1}/{t2}",
                                "correlation": round(corr, 2),
                                "risk": "high" if corr >= 0.85 else "medium",
                            })

            # 提取动量数据用于因子聚类
            for t in tickers:
                if t in pct_returns.columns:
                    series = pct_returns[t].dropna()
                    if len(series) >= 5:
                        returns_map[t] = {
                            "momentum_30d": round(float(series.sum() * 100), 2),
                            "volatility": round(float(series.std() * (252**0.5) * 100), 2),
                        }
    except Exception as e:
        _log.debug("价格相关性计算失败: %s", e)

    # ---- 3. 因子聚类（简单分组：高动量 / 低动量 / 高波动）----
    factor_clusters = {"高动量(>5%)": [], "低动量(<0%)": [], "高波动(>60%年化)": [], "稳定": []}
    for t, r in returns_map.items():
        mom = r.get("momentum_30d", 0)
        vol = r.get("volatility", 0)
        if vol > 60:
            factor_clusters["高波动(>60%年化)"].append(t)
        elif mom > 5:
            factor_clusters["高动量(>5%)"].append(t)
        elif mom < 0:
            factor_clusters["低动量(<0%)"].append(t)
        else:
            factor_clusters["稳定"].append(t)
    factor_clusters = {k: v for k, v in factor_clusters.items() if v}

    # ---- 4. 综合风险评分 ----
    risk_score = 0.0

    # 板块集中度贡献（0-5 分）
    if max_sector_pct >= 70:
        risk_score += 5.0
    elif max_sector_pct >= 50:
        risk_score += 3.0
    elif max_sector_pct >= 35:
        risk_score += 1.5

    # 相关性警告贡献（每对高相关 +1，最多 5 分）
    high_corr = [w for w in correlation_warnings if w["risk"] == "high"]
    risk_score += min(5.0, len(high_corr) * 1.5 + len(correlation_warnings) * 0.5)
    risk_score = min(10.0, risk_score)

    if risk_score >= 7:
        concentration_risk = "high"
    elif risk_score >= 4:
        concentration_risk = "medium"
    else:
        concentration_risk = "low"

    # ---- 5. 建议 ----
    recommendations = []
    if max_sector_pct >= 60:
        others = [s for s in sector_breakdown if s != max_sector]
        recommendations.append(
            f"⚠ {max_sector}板块占 {max_sector_pct:.0f}%（{sector_map[max_sector]}），建议补充{'/'.join(others[:2]) or '其他板块'}标的"
        )
    if high_corr:
        pairs = [w["pair"] for w in high_corr[:3]]
        recommendations.append(
            f"⚠ 高相关对：{', '.join(pairs)}（相关系数≥0.85），实际风险敞口可能少于表面标的数量"
        )
    if len(factor_clusters.get("高波动(>60%年化)", [])) >= 3:
        recommendations.append("⚠ 多个高波动标的同时高分，建议控制单票仓位上限")
    if not recommendations:
        recommendations.append("✅ 板块分布合理，相关性风险可控")

    summary_parts = [f"{total}个高分标的"]
    if max_sector:
        summary_parts.append(f"{max_sector}占{max_sector_pct:.0f}%")
    if correlation_warnings:
        summary_parts.append(f"{len(correlation_warnings)}对高相关")
    summary = " | ".join(summary_parts)

    return {
        "concentration_risk": concentration_risk,
        "risk_score": round(risk_score, 1),
        "sector_breakdown": sector_breakdown,
        "correlation_warnings": sorted(correlation_warnings,
                                       key=lambda x: x["correlation"], reverse=True)[:6],
        "factor_clusters": factor_clusters,
        "recommendations": recommendations,
        "summary": summary,
        "tickers_analyzed": tickers,
        "returns_data": returns_map,
    }


def _pearson(x: List[float], y: List[float]) -> Optional[float]:
    """计算皮尔森相关系数"""
    n = len(x)
    if n < 3:
        return None
    try:
        mx = sum(x) / n
        my = sum(y) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        dx = math.sqrt(sum((xi - mx)**2 for xi in x))
        dy = math.sqrt(sum((yi - my)**2 for yi in y))
        if dx * dy == 0:
            return None
        return num / (dx * dy)
    except (ZeroDivisionError, ValueError):
        return None
