"""
🐝 Alpha Hive - 机器学习预测系统
使用历史数据训练模型，优化概率计算和涨跌预测
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List
import statistics
from dataclasses import dataclass

_log = logging.getLogger("alpha_hive.ml_predictor")


@dataclass
class TrainingData:
    """训练数据结构"""
    ticker: str
    date: str
    crowding_score: float
    catalyst_quality: str  # A+, A, B+, B, C
    momentum_5d: float  # 5 日动量 (%)
    volatility: float  # 历史波动率
    market_sentiment: float  # -100 到 +100

    # 目标变量
    actual_return_3d: float  # 实际 3 日收益
    actual_return_7d: float  # 实际 7 日收益
    actual_return_30d: float  # 实际 30 日收益
    win_3d: bool  # 3 日是否赚钱
    win_7d: bool  # 7 日是否赚钱
    win_30d: bool  # 30 日是否赚钱

    # === v2 新特征（均有默认值，旧代码无需改动）===
    iv_rank: float = 50.0             # 0-100, IV 百分位
    put_call_ratio: float = 1.0       # Put/Call ratio
    final_score: float = 5.0          # 蜂群综合分 (0-10)
    odds_score: float = 5.0           # 赔率维度分 (0-10)
    risk_adj_score: float = 5.0       # 风险调整分 (0-10)
    agent_agreement: float = 0.5      # Agent 共识度 (0-1)
    direction_encoded: float = 0.0    # bullish=1, neutral=0, bearish=-1


class HistoricalDataBuilder:
    """构建训练数据集"""

    def __init__(self):
        # 收集的历史交易记录
        self.historical_records: List[TrainingData] = [
            # NVDA 记录
            TrainingData(
                ticker="NVDA", date="2023-10-18",
                crowding_score=68.0, catalyst_quality="A",
                momentum_5d=5.2, volatility=4.8, market_sentiment=45,
                actual_return_3d=8.5, actual_return_7d=18.9, actual_return_30d=32.1,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=62.0, put_call_ratio=0.8, final_score=8.2,
                odds_score=7.5, risk_adj_score=6.8, agent_agreement=0.85, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="NVDA", date="2023-04-19",
                crowding_score=72.0, catalyst_quality="A",
                momentum_5d=3.8, volatility=5.1, market_sentiment=35,
                actual_return_3d=12.8, actual_return_7d=22.3, actual_return_30d=18.5,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=55.0, put_call_ratio=0.75, final_score=7.8,
                odds_score=7.0, risk_adj_score=7.2, agent_agreement=0.71, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="NVDA", date="2024-01-24",
                crowding_score=75.0, catalyst_quality="A+",
                momentum_5d=6.5, volatility=6.1, market_sentiment=55,
                actual_return_3d=5.2, actual_return_7d=15.6, actual_return_30d=38.9,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=70.0, put_call_ratio=0.65, final_score=8.8,
                odds_score=8.0, risk_adj_score=7.5, agent_agreement=0.86, direction_encoded=1.0,
            ),
            # VKTX 记录
            TrainingData(
                ticker="VKTX", date="2023-06-15",
                crowding_score=58.0, catalyst_quality="A+",
                momentum_5d=2.1, volatility=12.3, market_sentiment=60,
                actual_return_3d=42.1, actual_return_7d=38.5, actual_return_30d=22.3,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=78.0, put_call_ratio=0.55, final_score=8.5,
                odds_score=7.8, risk_adj_score=5.5, agent_agreement=0.71, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="VKTX", date="2023-11-22",
                crowding_score=42.0, catalyst_quality="A",
                momentum_5d=1.5, volatility=8.9, market_sentiment=40,
                actual_return_3d=8.2, actual_return_7d=12.5, actual_return_30d=15.8,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=65.0, put_call_ratio=0.90, final_score=7.5,
                odds_score=6.5, risk_adj_score=6.0, agent_agreement=0.57, direction_encoded=1.0,
            ),
            # TSLA 记录
            TrainingData(
                ticker="TSLA", date="2024-01-17",
                crowding_score=71.0, catalyst_quality="B+",
                momentum_5d=4.2, volatility=7.8, market_sentiment=30,
                actual_return_3d=12.3, actual_return_7d=18.2, actual_return_30d=12.5,
                win_3d=True, win_7d=True, win_30d=True,
                iv_rank=58.0, put_call_ratio=1.10, final_score=7.2,
                odds_score=6.0, risk_adj_score=5.8, agent_agreement=0.57, direction_encoded=1.0,
            ),
            # 负例（失败的交易）
            TrainingData(
                ticker="NVDA", date="2023-08-01",
                crowding_score=82.0, catalyst_quality="B",
                momentum_5d=8.5, volatility=7.2, market_sentiment=70,
                actual_return_3d=-2.3, actual_return_7d=1.2, actual_return_30d=-5.8,
                win_3d=False, win_7d=False, win_30d=False,
                iv_rank=85.0, put_call_ratio=1.35, final_score=6.0,
                odds_score=4.5, risk_adj_score=4.0, agent_agreement=0.43, direction_encoded=1.0,
            ),
            TrainingData(
                ticker="VKTX", date="2023-09-15",
                crowding_score=65.0, catalyst_quality="C",
                momentum_5d=-3.2, volatility=11.5, market_sentiment=-20,
                actual_return_3d=-8.5, actual_return_7d=-12.3, actual_return_30d=-18.9,
                win_3d=False, win_7d=False, win_30d=False,
                iv_rank=72.0, put_call_ratio=1.50, final_score=4.8,
                odds_score=3.5, risk_adj_score=3.2, agent_agreement=0.29, direction_encoded=1.0,
            ),
        ]

    def get_training_data(self) -> List[TrainingData]:
        """获取所有训练数据"""
        return self.historical_records

    def add_record(self, record: TrainingData):
        """添加新的交易记录"""
        self.historical_records.append(record)

    def save_to_file(self, filename: str = "training_data.json"):
        """保存训练数据到文件"""
        data = [
            {
                "ticker": r.ticker,
                "date": r.date,
                "crowding_score": r.crowding_score,
                "catalyst_quality": r.catalyst_quality,
                "momentum_5d": r.momentum_5d,
                "volatility": r.volatility,
                "market_sentiment": r.market_sentiment,
                "actual_return_3d": r.actual_return_3d,
                "actual_return_7d": r.actual_return_7d,
                "actual_return_30d": r.actual_return_30d,
                "win_3d": r.win_3d,
                "win_7d": r.win_7d,
                "win_30d": r.win_30d,
                # v2 新特征
                "iv_rank": r.iv_rank,
                "put_call_ratio": r.put_call_ratio,
                "final_score": r.final_score,
                "odds_score": r.odds_score,
                "risk_adj_score": r.risk_adj_score,
                "agent_agreement": r.agent_agreement,
                "direction_encoded": r.direction_encoded,
            }
            for r in self.historical_records
        ]

        with open(filename, "w") as f:
            json.dump(data, f, indent=2)


class SimpleMLModel:
    """简单机器学习模型（不依赖 sklearn）"""

    def __init__(self):
        self.weights = {
            "crowding": 0.18,
            "catalyst": 0.15,
            "momentum": 0.12,
            "volatility": 0.09,
            "sentiment": 0.06,
            # v2 新特征
            "iv_rank": 0.10,
            "put_call_ratio": 0.08,
            "final_score": 0.07,
            "odds_score": 0.05,
            "risk_adj_score": 0.04,
            "agent_agreement": 0.04,
            "direction_encoded": 0.02,
        }
        self.is_trained = False
        self.training_accuracy = 0.0
        self.feature_stats: Dict = {}

    def encode_catalyst_quality(self, quality: str) -> float:
        """编码催化剂质量"""
        mapping = {"A+": 1.0, "A": 0.85, "B+": 0.70, "B": 0.55, "C": 0.40}
        return mapping.get(quality, 0.5)

    def normalize_feature(
        self, value: float, min_val: float, max_val: float
    ) -> float:
        """特征归一化"""
        if max_val == min_val:
            return 0.5
        return (value - min_val) / (max_val - min_val)

    def train(self, training_data: List[TrainingData]) -> Dict:
        """训练模型"""
        if not training_data:
            return {"status": "error", "message": "no training data"}

        _log.debug("开始训练 ML 模型...")
        _log.debug("训练样本数：%s", len(training_data))

        # 提取特征
        crowding_scores = [d.crowding_score for d in training_data]
        catalyst_qualities = [
            self.encode_catalyst_quality(d.catalyst_quality) for d in training_data
        ]
        momentums = [d.momentum_5d for d in training_data]
        volatilities = [d.volatility for d in training_data]
        sentiments = [d.market_sentiment for d in training_data]
        iv_ranks = [d.iv_rank for d in training_data]
        put_call_ratios = [d.put_call_ratio for d in training_data]
        final_scores = [d.final_score for d in training_data]
        odds_scores = [d.odds_score for d in training_data]
        risk_adj_scores = [d.risk_adj_score for d in training_data]
        agent_agreements = [d.agent_agreement for d in training_data]
        direction_encodeds = [d.direction_encoded for d in training_data]
        win_7d = [d.win_7d for d in training_data]  # 目标：7 日是否赚钱

        def _safe_stats(vals, name):
            return {"min": min(vals), "max": max(vals),
                    "mean": statistics.mean(vals)} if vals else {"min": 0, "max": 1, "mean": 0.5}

        # 计算特征的统计信息
        self.feature_stats = {
            "crowding": _safe_stats(crowding_scores, "crowding"),
            "catalyst": {"min": 0.4, "max": 1.0, "mean": 0.7},
            "momentum": _safe_stats(momentums, "momentum"),
            "volatility": _safe_stats(volatilities, "volatility"),
            "sentiment": _safe_stats(sentiments, "sentiment"),
            # v2
            "iv_rank": _safe_stats(iv_ranks, "iv_rank"),
            "put_call_ratio": _safe_stats(put_call_ratios, "put_call_ratio"),
            "final_score": _safe_stats(final_scores, "final_score"),
            "odds_score": _safe_stats(odds_scores, "odds_score"),
            "risk_adj_score": _safe_stats(risk_adj_scores, "risk_adj_score"),
            "agent_agreement": _safe_stats(agent_agreements, "agent_agreement"),
            "direction_encoded": _safe_stats(direction_encodeds, "direction_encoded"),
        }

        # 计算每个特征与目标的相关性（简单相关系数）
        correlations = self._calculate_correlations(
            training_data, win_7d
        )

        # 更新权重基于相关性
        total_corr = sum(abs(c) for c in correlations.values())
        if total_corr > 0:
            for key in correlations:
                self.weights[key] = abs(correlations[key]) / total_corr

        _log.info("权重更新：%s", self.weights)

        # 计算训练准确率
        predictions = [self.predict_probability(d) for d in training_data]
        correct = sum(
            1 for pred, actual in zip(predictions, win_7d)
            if (pred > 0.5) == actual
        )
        self.training_accuracy = correct / len(win_7d) * 100

        _log.debug("训练准确率：%.1f%%", self.training_accuracy)

        self.is_trained = True

        return {
            "status": "success",
            "samples": len(training_data),
            "accuracy": self.training_accuracy,
            "weights": self.weights,
        }

    def _calculate_correlations(self, data: List[TrainingData], target: List[bool]) -> Dict:
        """计算特征与目标的相关性"""
        target_numeric = [1.0 if x else 0.0 for x in target]

        feature_vals = {
            "crowding": [d.crowding_score for d in data],
            "catalyst": [self.encode_catalyst_quality(d.catalyst_quality) for d in data],
            "momentum": [d.momentum_5d for d in data],
            "volatility": [d.volatility for d in data],
            "sentiment": [d.market_sentiment for d in data],
            # v2
            "iv_rank": [d.iv_rank for d in data],
            "put_call_ratio": [d.put_call_ratio for d in data],
            "final_score": [d.final_score for d in data],
            "odds_score": [d.odds_score for d in data],
            "risk_adj_score": [d.risk_adj_score for d in data],
            "agent_agreement": [d.agent_agreement for d in data],
            "direction_encoded": [d.direction_encoded for d in data],
        }

        return {name: self._simple_correlation(vals, target_numeric)
                for name, vals in feature_vals.items()}

    def _simple_correlation(self, x: List[float], y: List[float]) -> float:
        """计算简单皮尔逊相关系数"""
        n = len(x)
        if n < 2:
            return 0.0

        mean_x = statistics.mean(x)
        mean_y = statistics.mean(y)

        numerator = sum(
            (x[i] - mean_x) * (y[i] - mean_y) for i in range(n)
        )
        denominator_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
        denominator_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5

        if denominator_x == 0 or denominator_y == 0:
            return 0.0

        return numerator / (denominator_x * denominator_y)

    def predict_probability(self, data: TrainingData) -> float:
        """预测赚钱概率（0-1）"""

        def _norm(feat_name, raw_val):
            stats = self.feature_stats.get(feat_name, {"min": 0, "max": 1})
            return self.normalize_feature(raw_val, stats["min"], stats["max"])

        crowding_norm = _norm("crowding", data.crowding_score)
        catalyst_norm = _norm("catalyst", self.encode_catalyst_quality(data.catalyst_quality))
        momentum_norm = _norm("momentum", data.momentum_5d)
        volatility_norm = _norm("volatility", data.volatility)
        sentiment_norm = _norm("sentiment", data.market_sentiment)
        iv_rank_norm = _norm("iv_rank", data.iv_rank)
        pcr_norm = _norm("put_call_ratio", data.put_call_ratio)
        final_score_norm = _norm("final_score", data.final_score)
        odds_norm = _norm("odds_score", data.odds_score)
        risk_adj_norm = _norm("risk_adj_score", data.risk_adj_score)
        agreement_norm = _norm("agent_agreement", data.agent_agreement)
        direction_norm = _norm("direction_encoded", data.direction_encoded)

        # 加权求和（各特征方向性处理）
        # 注意：每个分项必须在 [0, 1] 范围内，权重总和 = 1.0，
        # 这样加权和自然在 [0, 1]，不需要截断。
        probability = (
            self.weights.get("crowding", 0) * (1.0 - crowding_norm * 0.3)
            + self.weights.get("catalyst", 0) * catalyst_norm
            + self.weights.get("momentum", 0) * (0.3 + momentum_norm * 0.7)  # 正动量加分，范围 [0.3, 1.0]
            + self.weights.get("volatility", 0) * (1.0 - volatility_norm * 0.5)
            + self.weights.get("sentiment", 0) * (0.3 + sentiment_norm * 0.7)  # 正情绪加分，范围 [0.3, 1.0]
            # v2 新特征
            + self.weights.get("iv_rank", 0) * (1.0 - iv_rank_norm * 0.3)  # 高IV→略降
            + self.weights.get("put_call_ratio", 0) * (1.0 - pcr_norm * 0.4)  # 高P/C→看空
            + self.weights.get("final_score", 0) * final_score_norm
            + self.weights.get("odds_score", 0) * odds_norm
            + self.weights.get("risk_adj_score", 0) * risk_adj_norm
            + self.weights.get("agent_agreement", 0) * (0.3 + agreement_norm * 0.7)  # 高共识加分，范围 [0.3, 1.0]
            + self.weights.get("direction_encoded", 0) * (0.3 + direction_norm * 0.7)  # 看多方向加分，范围 [0.3, 1.0]
        )

        return max(0.0, min(1.0, probability))

    def predict_return(self, data: TrainingData) -> Dict:
        """预测收益"""
        probability = self.predict_probability(data)

        # 基于催化剂质量和其他因素预测收益
        catalyst_bonus = {
            "A+": 25,
            "A": 20,
            "B+": 15,
            "B": 10,
            "C": 5,
        }.get(data.catalyst_quality, 10)

        momentum_bonus = data.momentum_5d  # 动量直接加到收益
        crowding_penalty = data.crowding_score * 0.1  # 拥挤度降低预期收益

        # 预测 3 日、7 日、30 日收益
        expected_7d = catalyst_bonus + momentum_bonus - crowding_penalty

        return {
            "probability": probability,
            "expected_3d": expected_7d * 0.3,
            "expected_7d": expected_7d * 0.8,
            "expected_30d": expected_7d * 1.2,
        }

    def get_feature_importance(self) -> dict:
        """获取特征重要度（基于相关性权重）"""
        if not self.is_trained:
            return {}
        total = sum(abs(v) for v in self.weights.values())
        if total == 0:
            return {}
        importance = {}
        for name, w in self.weights.items():
            importance[name] = {
                "weight": round(abs(w) / total, 4),
                "coefficient": round(w, 4),
                "direction": "positive" if w > 0 else ("negative" if w < 0 else "neutral"),
            }
        return dict(sorted(importance.items(), key=lambda x: -x[1]["weight"]))

    def save_model(self, filename: str = "ml_model.json"):
        """保存模型（JSON 格式，安全序列化）"""
        model_data = {
            "weights": self.weights,
            "feature_stats": self.feature_stats,
            "training_accuracy": self.training_accuracy,
            "is_trained": self.is_trained,
        }

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(model_data, f, ensure_ascii=False, indent=2)

        _log.info("模型已保存：%s", filename)

    def load_model(self, filename: str = "ml_model.json"):
        """加载模型（JSON 格式，安全反序列化）"""
        # 兼容旧版 pickle 文件
        if filename.endswith(".pkl") and not os.path.exists(filename):
            filename = filename.replace(".pkl", ".json")
        try:
            with open(filename, "r", encoding="utf-8") as f:
                model_data = json.load(f)

            self.weights = model_data["weights"]
            self.feature_stats = model_data["feature_stats"]
            self.training_accuracy = model_data["training_accuracy"]
            self.is_trained = model_data["is_trained"]

            _log.info("模型已加载：%s", filename)
            return True
        except FileNotFoundError:
            _log.warning("模型文件不存在：%s", filename)
            return False


# ---------------------------------------------------------------------------
# 特征名定义（SGDMLModel 与 SimpleMLModel 共享）
# ---------------------------------------------------------------------------
FEATURE_NAMES = [
    "crowding", "catalyst", "momentum", "volatility", "sentiment",  # v1: 原始 5 维
    "iv_rank", "put_call_ratio", "final_score",                     # v2: DB 直取
    "odds_score", "risk_adj_score",                                  # v2: dimension_scores
    "agent_agreement", "direction_encoded",                          # v2: 派生
]
FEATURE_NAMES_V1 = ["crowding", "catalyst", "momentum", "volatility", "sentiment"]


def _encode_catalyst(quality: str) -> float:
    """编码催化剂质量（共享工具函数）"""
    return {"A+": 1.0, "A": 0.85, "B+": 0.70, "B": 0.55, "C": 0.40}.get(quality, 0.5)


def _extract_features(data: TrainingData) -> list:
    """从 TrainingData 提取 12 维特征向量"""
    return [
        data.crowding_score,
        _encode_catalyst(data.catalyst_quality),
        data.momentum_5d,
        data.volatility,
        data.market_sentiment,
        # v2 新特征
        data.iv_rank,
        data.put_call_ratio,
        data.final_score,
        data.odds_score,
        data.risk_adj_score,
        data.agent_agreement,
        data.direction_encoded,
    ]


class SGDMLModel:
    """
    sklearn SGDClassifier 在线学习模型（JSON 序列化，无 pickle）。

    支持：
    - train(data)           全量训练（多轮 partial_fit 收敛）
    - incremental_train(data) 增量学习（仅新数据 partial_fit 1 轮）
    - predict_probability(data) → float 0~1
    - predict_return(data) → dict（公式与 SimpleMLModel 一致）
    - save_model / load_model（JSON 往返，向下兼容旧格式）
    """

    def __init__(self):
        from sklearn.linear_model import SGDClassifier
        from sklearn.preprocessing import StandardScaler

        self._clf = SGDClassifier(
            loss="log_loss",
            penalty="l2",
            alpha=0.001,
            learning_rate="optimal",
            random_state=42,
        )
        self._scaler = StandardScaler()
        self._scaler_fitted = False
        self._clf_fitted = False
        self._n_samples_seen = 0

        # 兼容 SimpleMLModel 接口
        self.is_trained = False
        self.training_accuracy = 0.0
        self.feature_stats: Dict = {}

    # ---- 属性兼容 SimpleMLModel ----
    @property
    def weights(self) -> dict:
        """将 SGD 系数绝对值归一化为权重 dict"""
        n = len(FEATURE_NAMES)
        uniform = 1.0 / n
        if not self._clf_fitted:
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        raw = self._clf.coef_[0]
        if len(raw) != n:
            # 维度不匹配（旧模型），返回均匀权重
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        abs_sum = sum(abs(w) for w in raw)
        if abs_sum == 0:
            return dict(zip(FEATURE_NAMES, [uniform] * n))
        return {name: abs(raw[i]) / abs_sum for i, name in enumerate(FEATURE_NAMES)}

    @weights.setter
    def weights(self, value):
        """允许外部赋值（加载旧格式时兼容）"""
        pass  # SGD 权重由 coef_ 驱动，忽略手动设置

    # ---- 训练 ----
    def train(self, training_data: List[TrainingData]) -> Dict:
        """全量训练（多轮 partial_fit 直到收敛）"""
        import numpy as np

        if len(training_data) < 2:
            _log.warning("SGD 训练样本不足 (%d)，跳过", len(training_data))
            return {"status": "error", "message": "need >= 2 samples"}

        X = np.array([_extract_features(d) for d in training_data], dtype=np.float64)
        y = np.array([1 if d.win_7d else 0 for d in training_data])

        # 拟合 scaler
        self._scaler.fit(X)
        self._scaler_fitted = True
        X_scaled = self._scaler.transform(X)

        # 多轮 partial_fit 收敛
        classes = np.array([0, 1])
        for _ in range(20):
            self._clf.partial_fit(X_scaled, y, classes=classes)
        self._clf_fitted = True
        self._n_samples_seen = len(training_data)

        # 计算特征统计（兼容 SimpleMLModel）
        self.feature_stats = {
            name: {
                "min": float(X[:, i].min()),
                "max": float(X[:, i].max()),
                "mean": float(X[:, i].mean()),
            }
            for i, name in enumerate(FEATURE_NAMES)
        }

        # 计算训练准确率
        preds = self._clf.predict(X_scaled)
        self.training_accuracy = float((preds == y).mean() * 100)
        self.is_trained = True

        _log.info(
            "SGD 训练完成：%d 样本，准确率 %.1f%%，权重 %s",
            len(training_data),
            self.training_accuracy,
            {k: f"{v:.2f}" for k, v in self.weights.items()},
        )

        return {
            "status": "success",
            "samples": len(training_data),
            "accuracy": self.training_accuracy,
            "weights": self.weights,
        }

    def incremental_train(self, new_data: List[TrainingData]) -> Dict:
        """增量学习：仅对新数据 partial_fit 一次"""
        import numpy as np

        if not new_data:
            return {"status": "skip", "message": "no new data"}

        if not self._scaler_fitted:
            # 冷启动：降级为全量训练
            return self.train(new_data)

        X = np.array([_extract_features(d) for d in new_data], dtype=np.float64)
        y = np.array([1 if d.win_7d else 0 for d in new_data])

        # 增量更新 scaler（partial_fit）
        self._scaler.partial_fit(X)
        X_scaled = self._scaler.transform(X)

        classes = np.array([0, 1])
        self._clf.partial_fit(X_scaled, y, classes=classes)
        self._clf_fitted = True
        self._n_samples_seen += len(new_data)

        # 更新准确率（仅在新数据上评估）
        preds = self._clf.predict(X_scaled)
        batch_acc = float((preds == y).mean() * 100)

        _log.info(
            "SGD 增量学习：+%d 样本（累计 %d），本批准确率 %.1f%%",
            len(new_data), self._n_samples_seen, batch_acc,
        )

        return {
            "status": "success",
            "new_samples": len(new_data),
            "total_samples": self._n_samples_seen,
            "batch_accuracy": batch_acc,
        }

    # ---- 预测 ----
    def predict_probability(self, data: TrainingData) -> float:
        """预测赚钱概率 (0~1)，含小样本校准"""
        import numpy as np

        if not self._clf_fitted:
            return 0.5  # 未训练时返回默认值

        X = np.array([_extract_features(data)], dtype=np.float64)
        X_scaled = self._scaler.transform(X)
        prob = self._clf.predict_proba(X_scaled)[0]

        # prob 是 [P(class=0), P(class=1)]
        raw_prob = float(prob[1]) if len(prob) > 1 else float(prob[0])

        # --- 小样本校准（防止极端概率 0%/100%）---
        # 样本 < MIN_CONFIDENT 时，按比例混合先验 50%
        # 随着样本增加，逐步信任模型原始输出
        MIN_CONFIDENT = 100
        confidence_ratio = min(self._n_samples_seen / MIN_CONFIDENT, 1.0)
        calibrated = raw_prob * confidence_ratio + 0.5 * (1 - confidence_ratio)

        # 硬裁剪：永不超过 [5%, 95%] 区间（即使 100+ 样本也不该过度自信）
        calibrated = max(0.05, min(0.95, calibrated))

        return calibrated

    def predict_return(self, data: TrainingData) -> Dict:
        """预测收益（公式与 SimpleMLModel 完全一致）"""
        probability = self.predict_probability(data)

        catalyst_bonus = {
            "A+": 25, "A": 20, "B+": 15, "B": 10, "C": 5,
        }.get(data.catalyst_quality, 10)

        momentum_bonus = data.momentum_5d
        crowding_penalty = data.crowding_score * 0.1
        expected_7d = catalyst_bonus + momentum_bonus - crowding_penalty

        return {
            "probability": probability,
            "expected_3d": expected_7d * 0.3,
            "expected_7d": expected_7d * 0.8,
            "expected_30d": expected_7d * 1.2,
        }

    # ---- 序列化（JSON，无 pickle）----
    def save_model(self, filename: str = "ml_model.json"):
        """保存模型到 JSON"""
        import numpy as np

        model_data: Dict = {
            "model_type": "sgd",
            "feature_count": len(FEATURE_NAMES),
            "feature_names": FEATURE_NAMES,
            "is_trained": self.is_trained,
            "training_accuracy": self.training_accuracy,
            "n_samples_seen": self._n_samples_seen,
            "feature_stats": self.feature_stats,
            "weights": self.weights,
            "feature_importance": self.get_feature_importance(),
        }

        # SGD 分类器参数
        if self._clf_fitted:
            model_data["sgd"] = {
                "coef": self._clf.coef_.tolist(),
                "intercept": self._clf.intercept_.tolist(),
                "classes": self._clf.classes_.tolist(),
                "t": float(getattr(self._clf, "t_", 0)),
            }

        # StandardScaler 参数
        if self._scaler_fitted:
            model_data["scaler"] = {
                "mean": self._scaler.mean_.tolist(),
                "var": self._scaler.var_.tolist(),
                "scale": self._scaler.scale_.tolist(),
                "n_samples_seen": int(self._scaler.n_samples_seen_),
            }

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(model_data, f, ensure_ascii=False, indent=2)

        _log.info("SGD 模型已保存：%s", filename)

    def load_model(self, filename: str = "ml_model.json"):
        """从 JSON 加载模型（兼容旧 SimpleMLModel 格式）"""
        import numpy as np

        if filename.endswith(".pkl") and not os.path.exists(filename):
            filename = filename.replace(".pkl", ".json")

        try:
            with open(filename, "r", encoding="utf-8") as f:
                model_data = json.load(f)
        except FileNotFoundError:
            _log.warning("模型文件不存在：%s", filename)
            return False

        # 检测格式类型
        if model_data.get("model_type") == "sgd":
            # SGD 格式
            self.training_accuracy = model_data.get("training_accuracy", 0.0)
            self._n_samples_seen = model_data.get("n_samples_seen", 0)
            self.feature_stats = model_data.get("feature_stats", {})

            # 检测特征维度兼容性
            n_expected = len(FEATURE_NAMES)
            sgd_data = model_data.get("sgd", {})
            saved_coef = sgd_data.get("coef", [[]])
            saved_n = len(saved_coef[0]) if saved_coef and saved_coef[0] else 0

            if saved_n > 0 and saved_n != n_expected:
                _log.info(
                    "模型特征维度不匹配 (saved=%d, expected=%d)，需重新训练",
                    saved_n, n_expected,
                )
                self.is_trained = False  # 强制重训
                return True

            self.is_trained = model_data["is_trained"]

            if "sgd" in model_data and saved_n == n_expected:
                sgd = model_data["sgd"]
                self._clf.coef_ = np.array(sgd["coef"])
                self._clf.intercept_ = np.array(sgd["intercept"])
                self._clf.classes_ = np.array(sgd["classes"])
                if "t" in sgd:
                    self._clf.t_ = sgd["t"]
                self._clf_fitted = True

            if "scaler" in model_data:
                sc = model_data["scaler"]
                sc_mean = np.array(sc["mean"])
                if len(sc_mean) == n_expected:
                    self._scaler.mean_ = sc_mean
                    self._scaler.var_ = np.array(sc["var"])
                    self._scaler.scale_ = np.array(sc["scale"])
                    # partial_fit 需要 numpy 类型（有 .shape 属性），不能用纯 int
                    self._scaler.n_samples_seen_ = np.int64(sc.get("n_samples_seen", 1))
                    self._scaler.n_features_in_ = n_expected  # sklearn 1.x 兼容
                    self._scaler_fitted = True
                else:
                    _log.info("Scaler 维度不匹配，跳过加载")

            _log.info("SGD 模型已加载：%s", filename)
            return True
        else:
            # 旧格式（SimpleMLModel）—— 加载基础属性，需要重新训练 SGD
            _log.info("检测到旧格式模型，加载基础信息后需重新训练 SGD")
            self.feature_stats = model_data.get("feature_stats", {})
            self.training_accuracy = model_data.get("training_accuracy", 0.0)
            # 标记为未训练，让 Service 自动重训
            self.is_trained = False
            return True

    def get_feature_importance(self) -> dict:
        """获取特征重要度（基于 SGD 系数绝对值归一化）

        Returns:
            {feature_name: {"weight": float, "coefficient": float, "direction": str}}
            按重要度降序排列；未训练时返回空 dict。
        """
        if not self._clf_fitted:
            return {}
        raw = self._clf.coef_[0]
        n = len(FEATURE_NAMES)
        if len(raw) != n:
            return {}
        abs_vals = [abs(w) for w in raw]
        total = sum(abs_vals)
        if total == 0:
            return {name: {"weight": 0.0, "coefficient": 0.0, "direction": "neutral"}
                    for name in FEATURE_NAMES}
        importance = {}
        for i, name in enumerate(FEATURE_NAMES):
            importance[name] = {
                "weight": round(abs_vals[i] / total, 4),
                "coefficient": round(float(raw[i]), 4),
                "direction": "positive" if raw[i] > 0 else ("negative" if raw[i] < 0 else "neutral"),
            }
        return dict(sorted(importance.items(), key=lambda x: -x[1]["weight"]))

    def encode_catalyst_quality(self, quality: str) -> float:
        """兼容 SimpleMLModel 接口"""
        return _encode_catalyst(quality)


def build_training_data_from_db(
    db_path: str = None,
    min_samples: int = 30,
    max_rows: int = 500,
) -> List[TrainingData]:
    """从 backtester.predictions 表构建真实训练数据

    仅使用 checked_t7=1 且 return_t7 IS NOT NULL 的已验证记录。
    样本不足 min_samples 时返回空列表（调用方降级到硬编码数据）。

    Args:
        db_path: SQLite 数据库路径（默认使用 config.PATHS.db）
        min_samples: 最少样本数
        max_rows: 最多训练行数

    Returns:
        TrainingData 列表（可能为空）
    """
    import sqlite3

    if db_path is None:
        try:
            from hive_logger import PATHS
            db_path = str(PATHS.db)
        except (ImportError, AttributeError):
            _log.debug("build_training_data_from_db: 无法获取 DB 路径")
            return []

    if not os.path.exists(db_path):
        return []

    def _cat_qual(v: float) -> str:
        if v >= 8.5: return "A+"
        if v >= 7.5: return "A"
        if v >= 6.5: return "B+"
        if v >= 5.5: return "B"
        return "C"

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM predictions "
            "WHERE checked_t7 = 1 AND return_t7 IS NOT NULL "
            "ORDER BY date DESC LIMIT ?",
            (max_rows,),
        )
        rows = cursor.fetchall()
        conn.close()
    except (sqlite3.Error, OSError) as e:
        _log.debug("build_training_data_from_db 查询失败: %s", e)
        return []

    if len(rows) < min_samples:
        _log.debug("真实数据不足: %d < %d", len(rows), min_samples)
        return []

    direction_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
    result = []
    for r in rows:
        try:
            ds = json.loads(r["dimension_scores"] or "{}")
            ad = json.loads(r["agent_directions"] or "{}")

            _dir = r["direction"] or "neutral"
            if ad:
                _majority = sum(1 for d in ad.values() if d == _dir)
                _agree = _majority / len(ad)
            else:
                _agree = 0.5

            return_t7 = float(r["return_t7"])
            # 使用 backtester 的方向感知正确性标记（非简单 return>0）
            is_correct = bool(r["correct_t7"]) if r["correct_t7"] is not None else (return_t7 > 0)

            result.append(TrainingData(
                ticker=r["ticker"],
                date=r["date"],
                crowding_score=ds.get("signal", 5.0) * 10,
                catalyst_quality=_cat_qual(ds.get("catalyst", 5.0)),
                momentum_5d=0.0,
                volatility=5.0,
                market_sentiment=(ds.get("sentiment", 5.0) - 5) * 20,
                actual_return_3d=return_t7 * 0.4,
                actual_return_7d=return_t7,
                actual_return_30d=return_t7 * 2.5,
                win_3d=is_correct,
                win_7d=is_correct,
                win_30d=is_correct,
                iv_rank=float(r["iv_rank"]) if r["iv_rank"] is not None else 50.0,
                put_call_ratio=float(r["put_call_ratio"]) if r["put_call_ratio"] is not None else 1.0,
                final_score=float(r["final_score"]) if r["final_score"] is not None else 5.0,
                odds_score=ds.get("odds", 5.0),
                risk_adj_score=ds.get("risk_adj", 5.0),
                agent_agreement=_agree,
                direction_encoded=direction_map.get(_dir, 0.0),
            ))
        except (KeyError, ValueError, TypeError) as e:
            _log.debug("build_training_data_from_db: 跳过行: %s", e)
            continue

    _log.info("build_training_data_from_db: 成功构建 %d 条真实训练数据", len(result))
    return result


def create_ml_model():
    """工厂函数：优先使用 SGDMLModel，sklearn 不可用时降级到 SimpleMLModel"""
    try:
        from sklearn.linear_model import SGDClassifier  # noqa: F401
        return SGDMLModel()
    except ImportError:
        _log.info("sklearn 不可用，降级使用 SimpleMLModel")
        return SimpleMLModel()


class MLPredictionService:
    """ML 预测服务"""

    def __init__(self):
        self.model = create_ml_model()
        self.data_builder = HistoricalDataBuilder()

    def train_model(self) -> Dict:
        """训练模型 — 优先使用真实数据，不足时降级到硬编码"""
        real_data = []
        try:
            from config import ML_TRAINING_CONFIG as _MTC
            if _MTC.get("use_real_data", True):
                real_data = build_training_data_from_db(
                    min_samples=_MTC.get("min_real_samples", 30),
                    max_rows=_MTC.get("max_training_rows", 500),
                )
        except (ImportError, OSError) as e:
            _log.debug("真实训练数据加载失败: %s", e)

        if real_data:
            _log.info("使用 %d 条真实数据训练 ML 模型", len(real_data))
            training_data = real_data
        else:
            training_data = self.data_builder.get_training_data()
            _log.info("真实数据不足，使用 %d 条硬编码数据", len(training_data))

        result = self.model.train(training_data)

        # 保存模型
        if result.get("status") == "success":
            self.model.save_model()

        return result

    def incremental_train(self, new_data: List[TrainingData]) -> Dict:
        """增量学习（仅 SGDMLModel 支持，SimpleMLModel 降级为全量重训）"""
        if hasattr(self.model, "incremental_train"):
            return self.model.incremental_train(new_data)
        # SimpleMLModel fallback: 追加数据后全量重训
        self.data_builder.historical_records.extend(new_data)
        return self.model.train(self.data_builder.get_training_data())

    def predict_for_opportunity(self, data: TrainingData) -> Dict:
        """为某个机会预测"""
        if not self.model.is_trained:
            self.train_model()

        prediction = self.model.predict_return(data)

        return {
            "ticker": data.ticker,
            "date": datetime.now().isoformat(),
            "input": {
                "crowding_score": data.crowding_score,
                "catalyst_quality": data.catalyst_quality,
                "momentum_5d": data.momentum_5d,
                "volatility": data.volatility,
                "market_sentiment": data.market_sentiment,
            },
            "prediction": prediction,
            "recommendation": self._generate_recommendation(prediction),
        }

    def _generate_recommendation(self, prediction: Dict) -> str:
        """生成推荐"""
        prob = prediction["probability"]

        if prob >= 0.75:
            return "STRONG BUY - 高概率机会"
        elif prob >= 0.65:
            return "BUY - 值得参与"
        elif prob >= 0.50:
            return "HOLD - 等待更好机会"
        else:
            return "AVOID - 风险大于收益"

    def get_model_info(self) -> Dict:
        """获取模型信息"""
        return {
            "is_trained": self.model.is_trained,
            "training_accuracy": self.model.training_accuracy,
            "weights": self.model.weights,
            "training_samples": len(self.data_builder.get_training_data()),
        }


# ==================== 脚本示例 ====================
if __name__ == "__main__":
    print("🤖 Alpha Hive ML 预测系统")
    print("=" * 60)

    # 创建服务
    service = MLPredictionService()

    # 训练模型
    print("\n📚 第 1 步：训练模型")
    print("-" * 60)
    result = service.train_model()
    print(json.dumps(result, indent=2))

    # 为新的机会做预测
    print("\n\n🔮 第 2 步：预测新机会")
    print("-" * 60)

    # 模拟一个新的交易机会
    new_opportunity = TrainingData(
        ticker="NVDA",
        date="2026-02-23",
        crowding_score=63.5,
        catalyst_quality="A",
        momentum_5d=6.8,
        volatility=4.8,
        market_sentiment=45,
        actual_return_3d=0,  # 未来数据
        actual_return_7d=0,
        actual_return_30d=0,
        win_3d=False,
        win_7d=False,
        win_30d=False,
    )

    prediction = service.predict_for_opportunity(new_opportunity)
    print(json.dumps(prediction, indent=2, default=str))

    # 显示模型信息
    print("\n\n📊 第 3 步：模型性能")
    print("-" * 60)
    info = service.get_model_info()
    print(f"训练状态：{'已训练' if info['is_trained'] else '未训练'}")
    print(f"训练准确率：{info['training_accuracy']:.1f}%")
    print(f"训练样本数：{info['training_samples']}")
    print(f"\n特征权重：")
    for feature, weight in info['weights'].items():
        print(f"  • {feature}: {weight:.1%}")

    print("\n" + "=" * 60)
    print("✅ ML 预测演示完成！")
    print("=" * 60)
