"""
Alpha Hive 升级 — Phase 1.3: 回测补全 + 标准风控指标
=====================================================
问题：
  1. report_snapshots/ 中大部分 actual_prices.t7/t30 为 null
  2. 缺少 Sharpe/Sortino/最大回撤等投行标准指标
  3. 按评分区间的胜率曲线未计算

解决方案：
  1. backfill_prices() — 补全所有历史预测的实际价格
  2. AccuracyEngine — 完整的胜率/收益统计引擎
  3. RiskMetrics — Sharpe/Sortino/MaxDrawdown/Calmar

用法：
  python backtest_engine.py --backfill        # 补全价格
  python backtest_engine.py --report          # 生成报告
  python backtest_engine.py --score-curve     # 评分-胜率曲线

  或在代码中：
  from backtest_engine import BacktestEngine
  engine = BacktestEngine("./report_snapshots")
  engine.backfill_prices()
  report = engine.generate_accuracy_report()
"""

import os
import json
import math
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict

_log = logging.getLogger("alpha_hive.backtest")


# ==================== 数据结构 ====================

@dataclass
class Prediction:
    """单条预测记录"""
    ticker: str
    date: str
    composite_score: float
    direction: str
    entry_price: float
    agent_votes: Dict[str, float] = field(default_factory=dict)
    weights_used: Dict[str, float] = field(default_factory=dict)
    actual_prices: Dict[str, Optional[float]] = field(default_factory=dict)

    @property
    def return_t1(self) -> Optional[float]:
        p = self.actual_prices.get("t1")
        if p and self.entry_price > 0:
            return round((p / self.entry_price - 1) * 100, 4)
        return None

    @property
    def return_t7(self) -> Optional[float]:
        p = self.actual_prices.get("t7")
        if p and self.entry_price > 0:
            return round((p / self.entry_price - 1) * 100, 4)
        return None

    @property
    def return_t30(self) -> Optional[float]:
        p = self.actual_prices.get("t30")
        if p and self.entry_price > 0:
            return round((p / self.entry_price - 1) * 100, 4)
        return None

    def is_correct(self, horizon: str = "t7") -> Optional[bool]:
        """预测是否正确（方向匹配）。neutral 返回 None（不纳入胜率统计）"""
        ret = getattr(self, f"return_{horizon}", None)
        if ret is None:
            return None
        if self.direction == "bullish":
            return ret > 0
        elif self.direction == "bearish":
            return ret < 0
        else:  # neutral = 不交易 → 不计入胜率
            return None


# ==================== 价格补全引擎 ====================

class PriceBackfiller:
    """补全历史预测的实际价格"""

    def __init__(self):
        self._yf_cache: Dict[str, object] = {}

    def backfill(self, predictions: List[Prediction],
                 save_callback: Optional[callable] = None) -> Dict:
        """
        补全所有缺失的 actual_prices
        
        Returns:
            {"filled": int, "skipped": int, "failed": int, "details": [...]}
        """
        stats = {"filled": 0, "skipped": 0, "failed": 0, "details": []}

        for pred in predictions:
            for horizon, days in [("t1", 1), ("t7", 7), ("t30", 30)]:
                if pred.actual_prices.get(horizon) is not None:
                    stats["skipped"] += 1
                    continue

                target_date = self._add_business_days(pred.date, days)
                if target_date > datetime.now().strftime("%Y-%m-%d"):
                    stats["skipped"] += 1
                    continue  # 还没到日期

                price = self._get_price_on_date(pred.ticker, target_date)
                if price is not None and price > 0:
                    pred.actual_prices[horizon] = price
                    stats["filled"] += 1
                    stats["details"].append({
                        "ticker": pred.ticker,
                        "pred_date": pred.date,
                        "horizon": horizon,
                        "target_date": target_date,
                        "price": price,
                    })
                    if save_callback:
                        save_callback(pred)
                else:
                    stats["failed"] += 1

        _log.info(
            "[Backfill] 完成: filled=%d, skipped=%d, failed=%d",
            stats["filled"], stats["skipped"], stats["failed"]
        )
        return stats

    def _get_price_on_date(self, ticker: str, date_str: str) -> Optional[float]:
        """获取指定日期的收盘价（±2 交易日容差）"""
        try:
            import yfinance as yf
            if ticker not in self._yf_cache:
                self._yf_cache[ticker] = yf.Ticker(ticker)

            t = self._yf_cache[ticker]
            target = datetime.strptime(date_str, "%Y-%m-%d")
            start = target - timedelta(days=5)
            end = target + timedelta(days=5)

            hist = t.history(start=start.strftime("%Y-%m-%d"),
                           end=end.strftime("%Y-%m-%d"))
            if hist.empty:
                return None

            # 找最接近目标日期的交易日
            best_price = None
            best_diff = float("inf")
            for idx in hist.index:
                idx_date = idx.strftime("%Y-%m-%d")
                diff = abs((datetime.strptime(idx_date, "%Y-%m-%d") - target).days)
                if diff < best_diff:
                    best_diff = diff
                    best_price = float(hist.loc[idx, "Close"])

            return best_price if best_diff <= 3 else None

        except Exception as e:
            _log.debug("Price fetch failed for %s on %s: %s", ticker, date_str, e)
            return None

    @staticmethod
    def _add_business_days(date_str: str, days: int) -> str:
        """增加 N 个工作日"""
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        added = 0
        while added < days:
            dt += timedelta(days=1)
            if dt.weekday() < 5:  # Mon-Fri
                added += 1
        return dt.strftime("%Y-%m-%d")


# ==================== 风控指标计算 ====================

class RiskMetrics:
    """投行标准风控指标"""

    @staticmethod
    def sharpe_ratio(returns: List[float], risk_free_annual: float = 0.05,
                     period_days: int = 7) -> Optional[float]:
        """
        Sharpe Ratio = (E[R] - Rf) / σ(R)，年化
        
        Args:
            returns: 收益率列表（百分比）
            risk_free_annual: 年化无风险利率
            period_days: 每条收益的持有期天数（日度=1, T+7=7, T+30=30）
        """
        if len(returns) < 5:
            return None
        periods_per_year = 252 / period_days  # T+7 → 36, 日度 → 252
        rf_per_period = risk_free_annual / periods_per_year
        excess = [r / 100 - rf_per_period for r in returns]  # returns 是百分比
        mean_excess = sum(excess) / len(excess)
        std = math.sqrt(sum((x - mean_excess) ** 2 for x in excess) / (len(excess) - 1))
        if std == 0:
            return None
        return round(mean_excess / std * math.sqrt(periods_per_year), 3)

    @staticmethod
    def sortino_ratio(returns: List[float], risk_free_annual: float = 0.05,
                      period_days: int = 7) -> Optional[float]:
        """
        Sortino Ratio = (E[R] - Rf) / σ_down(R)，年化
        只用下行波动率
        """
        if len(returns) < 5:
            return None
        periods_per_year = 252 / period_days
        rf_per_period = risk_free_annual / periods_per_year
        excess = [r / 100 - rf_per_period for r in returns]
        mean_excess = sum(excess) / len(excess)
        downside = [min(0, x) ** 2 for x in excess]
        downside_std = math.sqrt(sum(downside) / len(downside))
        if downside_std == 0:
            return None
        return round(mean_excess / downside_std * math.sqrt(periods_per_year), 3)

    @staticmethod
    def max_drawdown(returns: List[float]) -> Optional[float]:
        """最大回撤（百分比）— 使用乘法累计（几何收益）"""
        if len(returns) < 2:
            return None
        # 乘法累计净值：(1+r1)(1+r2)...
        cumulative = [1.0]
        for r in returns:
            cumulative.append(cumulative[-1] * (1.0 + r / 100.0))

        peak = cumulative[0]
        max_dd = 0.0
        for val in cumulative:
            if val > peak:
                peak = val
            if peak > 0:
                dd = (peak - val) / peak  # 回撤百分比
                max_dd = max(max_dd, dd)

        return round(max_dd * 100, 2)

    @staticmethod
    def calmar_ratio(returns: List[float], risk_free_annual: float = 0.05,
                     period_days: int = 7) -> Optional[float]:
        """Calmar Ratio = 年化收益 / 最大回撤"""
        if len(returns) < 10:
            return None
        # 乘法累计总收益
        cumulative = 1.0
        for r in returns:
            cumulative *= (1.0 + r / 100.0)
        total_return = cumulative - 1.0
        # 年化：按实际持有期计算
        periods_per_year = 252 / period_days
        n_years = len(returns) / periods_per_year
        annual_return = (1.0 + total_return) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0
        max_dd = RiskMetrics.max_drawdown(returns)
        if max_dd is None or max_dd == 0:
            return None
        return round((annual_return - risk_free_annual) / (max_dd / 100), 3)

    @staticmethod
    def win_rate_by_score_band(predictions: List[Prediction],
                                horizon: str = "t7",
                                bands: List[Tuple[float, float]] = None) -> List[Dict]:
        """
        按评分区间统计胜率曲线
        这是投行最关注的指标：你的评分体系有没有区分度？
        """
        if bands is None:
            bands = [
                (0.0, 4.0),    # 低分
                (4.0, 5.5),    # 偏低
                (5.5, 6.5),    # 中等
                (6.5, 7.5),    # 偏高
                (7.5, 8.5),    # 高分
                (8.5, 10.1),   # 极高分
            ]

        results = []
        for lo, hi in bands:
            in_band = [
                p for p in predictions
                if lo <= p.composite_score < hi and p.is_correct(horizon) is not None
            ]
            if not in_band:
                results.append({
                    "band": f"{lo:.1f}-{hi:.1f}",
                    "count": 0,
                    "win_rate": None,
                    "avg_return": None,
                    "avg_score": None,
                })
                continue

            correct = sum(1 for p in in_band if p.is_correct(horizon))
            returns = [
                getattr(p, f"return_{horizon}") for p in in_band
                if getattr(p, f"return_{horizon}") is not None
            ]

            results.append({
                "band": f"{lo:.1f}-{hi:.1f}",
                "count": len(in_band),
                "win_rate": round(correct / len(in_band) * 100, 1),
                "avg_return": round(sum(returns) / len(returns), 2) if returns else None,
                "avg_score": round(sum(p.composite_score for p in in_band) / len(in_band), 2),
            })

        return results


# ==================== 完整报告引擎 ====================

class BacktestEngine:
    """完整回测分析引擎"""

    def __init__(self, snapshots_dir: str = "./report_snapshots"):
        self.snapshots_dir = Path(snapshots_dir)
        self.predictions: List[Prediction] = []
        self.backfiller = PriceBackfiller()
        self._load_all_snapshots()

    def _load_all_snapshots(self):
        """加载所有 report_snapshots/*.json"""
        self.predictions.clear()
        if not self.snapshots_dir.exists():
            _log.warning("Snapshots dir not found: %s", self.snapshots_dir)
            return

        for f in sorted(self.snapshots_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                pred = Prediction(
                    ticker=data.get("ticker", ""),
                    date=data.get("date", ""),
                    composite_score=data.get("composite_score", 0),
                    direction=data.get("direction", "neutral"),
                    entry_price=data.get("entry_price", 0),
                    agent_votes=data.get("agent_votes", {}),
                    weights_used=data.get("weights_used", {}),
                    actual_prices={
                        "t1": data.get("actual_prices", {}).get("t1"),
                        "t7": data.get("actual_prices", {}).get("t7"),
                        "t30": data.get("actual_prices", {}).get("t30"),
                    },
                )
                if pred.ticker and pred.date and pred.entry_price > 0:
                    self.predictions.append(pred)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                _log.debug("Failed to load %s: %s", f, e)

        _log.info("Loaded %d predictions from %s", len(self.predictions), self.snapshots_dir)

    def backfill_prices(self) -> Dict:
        """补全所有缺失的实际价格"""
        def save_callback(pred: Prediction):
            """保存更新后的 snapshot"""
            fname = f"{pred.ticker}_{pred.date}.json"
            fpath = self.snapshots_dir / fname
            if fpath.exists():
                data = json.loads(fpath.read_text())
                data["actual_prices"] = pred.actual_prices
                fpath.write_text(json.dumps(data, indent=2, default=str))

        return self.backfiller.backfill(self.predictions, save_callback)

    def generate_accuracy_report(self) -> Dict:
        """
        生成完整准确率报告（对标投行标准）
        
        包含：
        1. 总体胜率（T+1/T+7/T+30）
        2. 按方向分拆胜率
        3. 按评分区间的胜率曲线（区分度检验）
        4. 风控指标（Sharpe/Sortino/MaxDrawdown/Calmar）
        5. 按 Agent 分拆的准确率（找出最强/最弱 Agent）
        6. 数据完整度统计
        """
        report = {
            "generated_at": datetime.now().isoformat(),
            "total_predictions": len(self.predictions),
        }

        # 1. 数据完整度
        completeness = self._calc_completeness()
        report["data_completeness"] = completeness

        # 2. 总体胜率
        for horizon in ["t1", "t7", "t30"]:
            stats = self._calc_overall_stats(horizon)
            report[f"overall_{horizon}"] = stats

        # 3. 按方向分拆
        for horizon in ["t7"]:
            dir_stats = self._calc_direction_stats(horizon)
            report[f"direction_{horizon}"] = dir_stats

        # 4. 评分-胜率曲线（核心指标）
        for horizon in ["t7"]:
            curve = RiskMetrics.win_rate_by_score_band(self.predictions, horizon)
            report[f"score_curve_{horizon}"] = curve

        # 5. 风控指标
        risk = self._calc_risk_metrics()
        report["risk_metrics"] = risk

        # 6. 按 Agent 分拆
        agent_stats = self._calc_agent_stats()
        report["agent_performance"] = agent_stats

        # 7. 关键发现
        report["key_findings"] = self._generate_findings(report)

        return report

    def _calc_completeness(self) -> Dict:
        """数据完整度统计"""
        total = len(self.predictions)
        if total == 0:
            return {"total": 0}

        t1_filled = sum(1 for p in self.predictions if p.actual_prices.get("t1") is not None)
        t7_filled = sum(1 for p in self.predictions if p.actual_prices.get("t7") is not None)
        t30_filled = sum(1 for p in self.predictions if p.actual_prices.get("t30") is not None)

        return {
            "total": total,
            "t1_filled": t1_filled,
            "t1_pct": round(t1_filled / total * 100, 1),
            "t7_filled": t7_filled,
            "t7_pct": round(t7_filled / total * 100, 1),
            "t30_filled": t30_filled,
            "t30_pct": round(t30_filled / total * 100, 1),
            "unique_tickers": len(set(p.ticker for p in self.predictions)),
            "date_range": {
                "start": min(p.date for p in self.predictions),
                "end": max(p.date for p in self.predictions),
            },
        }

    def _calc_overall_stats(self, horizon: str) -> Dict:
        """总体胜率统计"""
        valid = [p for p in self.predictions if p.is_correct(horizon) is not None]
        if not valid:
            return {"count": 0, "win_rate": None}

        correct = sum(1 for p in valid if p.is_correct(horizon))
        returns = [
            getattr(p, f"return_{horizon}") for p in valid
            if getattr(p, f"return_{horizon}") is not None
        ]

        return {
            "count": len(valid),
            "win_rate": round(correct / len(valid) * 100, 1),
            "avg_return": round(sum(returns) / len(returns), 3) if returns else None,
            "median_return": round(sorted(returns)[len(returns) // 2], 3) if returns else None,
            "best_return": round(max(returns), 3) if returns else None,
            "worst_return": round(min(returns), 3) if returns else None,
            "positive_pct": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 1) if returns else None,
        }

    def _calc_direction_stats(self, horizon: str) -> Dict:
        """按方向分拆的胜率"""
        result = {}
        for direction in ["bullish", "bearish", "neutral"]:
            preds = [
                p for p in self.predictions
                if p.direction == direction and p.is_correct(horizon) is not None
            ]
            if not preds:
                result[direction] = {"count": 0, "win_rate": None}
                continue

            correct = sum(1 for p in preds if p.is_correct(horizon))
            returns = [
                getattr(p, f"return_{horizon}") for p in preds
                if getattr(p, f"return_{horizon}") is not None
            ]

            result[direction] = {
                "count": len(preds),
                "win_rate": round(correct / len(preds) * 100, 1),
                "avg_return": round(sum(returns) / len(returns), 3) if returns else None,
            }

        return result

    def _calc_risk_metrics(self) -> Dict:
        """计算标准风控指标"""
        # 使用 T+7 收益率序列
        returns_t7 = [
            p.return_t7 for p in self.predictions
            if p.return_t7 is not None
        ]

        if len(returns_t7) < 5:
            return {"note": "数据不足（需至少 5 条有 T+7 价格的预测）"}

        return {
            "sharpe_ratio": RiskMetrics.sharpe_ratio(returns_t7),
            "sortino_ratio": RiskMetrics.sortino_ratio(returns_t7),
            "max_drawdown_pct": RiskMetrics.max_drawdown(returns_t7),
            "calmar_ratio": RiskMetrics.calmar_ratio(returns_t7),
            "total_return_pct": round(sum(r for r in returns_t7), 2),
            "avg_return_pct": round(sum(returns_t7) / len(returns_t7), 3),
            "return_std_pct": round(
                math.sqrt(sum((r - sum(returns_t7) / len(returns_t7)) ** 2
                              for r in returns_t7) / (len(returns_t7) - 1)),
                3
            ),
            "n_observations": len(returns_t7),
        }

    def _calc_agent_stats(self) -> Dict:
        """按 Agent 分析预测能力（哪个蜂最准？）"""
        agent_results = defaultdict(lambda: {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0})

        for pred in self.predictions:
            for agent_name, vote_score in pred.agent_votes.items():
                # 根据 vote_score 推断 Agent 方向
                if vote_score >= 6.5:
                    agent_dir = "bullish"
                elif vote_score <= 4.0:
                    agent_dir = "bearish"
                else:
                    agent_dir = "neutral"

                is_correct = pred.is_correct("t7")
                if is_correct is None:
                    continue

                key = agent_name
                agent_results[key]["total"] += 1
                if (agent_dir == "bullish" and pred.return_t7 and pred.return_t7 > 0) or \
                   (agent_dir == "bearish" and pred.return_t7 and pred.return_t7 < 0):
                    agent_results[key]["correct"] = agent_results[key].get("correct", 0) + 1

        result = {}
        for agent, stats in agent_results.items():
            total = stats["total"]
            correct = stats.get("correct", 0)
            result[agent] = {
                "total": total,
                "correct": correct,
                "accuracy": round(correct / total * 100, 1) if total > 0 else 0,
            }

        # 按准确率排序
        return dict(sorted(result.items(), key=lambda x: x[1]["accuracy"], reverse=True))

    def _generate_findings(self, report: Dict) -> List[str]:
        """基于报告数据生成关键发现"""
        findings = []

        # 评分区分度检验
        curve = report.get("score_curve_t7", [])
        if len(curve) >= 2:
            high_bands = [b for b in curve if b["count"] > 0 and b["band"].startswith(("7.5", "8.5"))]
            low_bands = [b for b in curve if b["count"] > 0 and b["band"].startswith(("0.0", "4.0"))]

            if high_bands and low_bands:
                high_wr = sum(b["win_rate"] or 0 for b in high_bands) / len(high_bands)
                low_wr = sum(b["win_rate"] or 0 for b in low_bands) / len(low_bands)
                diff = high_wr - low_wr
                if diff > 15:
                    findings.append(f"✅ 评分体系有区分度：高分段胜率 {high_wr:.1f}% vs 低分段 {low_wr:.1f}%（差 {diff:.1f}pp）")
                elif diff > 5:
                    findings.append(f"⚠️ 评分体系区分度一般：高分段 {high_wr:.1f}% vs 低分段 {low_wr:.1f}%（差 {diff:.1f}pp，目标 >15pp）")
                else:
                    findings.append(f"❌ 评分体系无区分度：高分段 {high_wr:.1f}% vs 低分段 {low_wr:.1f}%（差仅 {diff:.1f}pp，需重构权重）")

        # 风控指标
        risk = report.get("risk_metrics", {})
        sharpe = risk.get("sharpe_ratio")
        if sharpe is not None:
            if sharpe > 1.5:
                findings.append(f"✅ Sharpe Ratio {sharpe}（优秀，>1.5）")
            elif sharpe > 0.5:
                findings.append(f"⚠️ Sharpe Ratio {sharpe}（一般，目标 >1.5）")
            else:
                findings.append(f"❌ Sharpe Ratio {sharpe}（较差，<0.5，信号噪比不足）")

        max_dd = risk.get("max_drawdown_pct")
        if max_dd is not None:
            if max_dd < 10:
                findings.append(f"✅ 最大回撤 {max_dd}%（控制良好）")
            elif max_dd < 20:
                findings.append(f"⚠️ 最大回撤 {max_dd}%（可接受，关注尾部风险）")
            else:
                findings.append(f"❌ 最大回撤 {max_dd}%（过大，需加强风控）")

        # 数据完整度
        comp = report.get("data_completeness", {})
        t7_pct = comp.get("t7_pct", 0)
        if t7_pct < 50:
            findings.append(f"❌ T+7 价格补全率仅 {t7_pct}%，需运行 backfill_prices()")
        elif t7_pct < 80:
            findings.append(f"⚠️ T+7 价格补全率 {t7_pct}%，建议定期运行 backfill")

        # 方向偏差
        dir_stats = report.get("direction_t7", {})
        bull_wr = (dir_stats.get("bullish", {}).get("win_rate") or 0)
        bear_wr = (dir_stats.get("bearish", {}).get("win_rate") or 0)
        if bull_wr > 0 and bear_wr > 0:
            if bull_wr > bear_wr + 20:
                findings.append(f"⚠️ 看多信号胜率 ({bull_wr}%) 显著高于看空 ({bear_wr}%)，系统可能存在看多偏差")
            elif bear_wr > bull_wr + 20:
                findings.append(f"⚠️ 看空信号胜率 ({bear_wr}%) 显著高于看多 ({bull_wr}%)，系统可能过于保守")

        return findings

    def print_report(self):
        """打印格式化报告"""
        report = self.generate_accuracy_report()
        print("\n" + "=" * 60)
        print("  Alpha Hive 回测报告")
        print("=" * 60)

        comp = report["data_completeness"]
        print(f"\n📊 数据概览")
        print(f"  总预测数: {comp['total']}")
        print(f"  覆盖标的: {comp.get('unique_tickers', '?')}")
        print(f"  时间范围: {comp.get('date_range', {}).get('start', '?')} → {comp.get('date_range', {}).get('end', '?')}")
        print(f"  T+1 补全: {comp.get('t1_pct', 0)}% | T+7: {comp.get('t7_pct', 0)}% | T+30: {comp.get('t30_pct', 0)}%")

        for horizon in ["t1", "t7", "t30"]:
            stats = report.get(f"overall_{horizon}", {})
            if stats.get("count", 0) > 0:
                print(f"\n📈 {horizon.upper()} 总体")
                print(f"  有效预测: {stats['count']}")
                print(f"  胜率: {stats['win_rate']}%")
                print(f"  平均收益: {stats['avg_return']}%")
                print(f"  最佳/最差: {stats['best_return']}% / {stats['worst_return']}%")

        print(f"\n🎯 评分-胜率曲线 (T+7)")
        curve = report.get("score_curve_t7", [])
        for band in curve:
            wr = f"{band['win_rate']}%" if band['win_rate'] is not None else "N/A"
            ar = f"{band['avg_return']}%" if band['avg_return'] is not None else "N/A"
            bar = "█" * int((band['win_rate'] or 0) / 5) if band['win_rate'] else ""
            print(f"  [{band['band']}] n={band['count']:3d} | 胜率={wr:>6s} | 均收益={ar:>7s} | {bar}")

        risk = report.get("risk_metrics", {})
        if risk.get("sharpe_ratio") is not None:
            print(f"\n🛡 风控指标")
            print(f"  Sharpe Ratio:  {risk['sharpe_ratio']}")
            print(f"  Sortino Ratio: {risk['sortino_ratio']}")
            print(f"  最大回撤:      {risk['max_drawdown_pct']}%")
            print(f"  Calmar Ratio:  {risk['calmar_ratio']}")

        print(f"\n💡 关键发现")
        for finding in report.get("key_findings", []):
            print(f"  {finding}")

        print("\n" + "=" * 60)


# ==================== CLI 入口 ====================

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Alpha Hive 回测引擎")
    parser.add_argument("--dir", default="./report_snapshots", help="snapshots 目录")
    parser.add_argument("--backfill", action="store_true", help="补全缺失价格")
    parser.add_argument("--report", action="store_true", help="生成准确率报告")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    engine = BacktestEngine(args.dir)

    if args.backfill:
        result = engine.backfill_prices()
        print(json.dumps(result, indent=2, default=str))

    if args.report or not args.backfill:
        if args.json:
            report = engine.generate_accuracy_report()
            print(json.dumps(report, indent=2, default=str))
        else:
            engine.print_report()
