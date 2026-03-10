"""
🐝 Alpha Hive - ML 预测系统测试
覆盖：SimpleMLModel / SGDMLModel / create_ml_model 工厂 / MLPredictionService
      v2 12 维特征 / 特征重要性 / 向后兼容
"""

import json
import os
import tempfile
import pytest

from ml_predictor import (
    SimpleMLModel,
    TrainingData,
    HistoricalDataBuilder,
    MLPredictionService,
    FEATURE_NAMES,
    FEATURE_NAMES_V1,
    create_ml_model,
    _extract_features,
    _encode_catalyst,
)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _make_sample(win: bool = True, **overrides) -> TrainingData:
    """快速生成测试样本（支持 v2 新字段）"""
    defaults = dict(
        ticker="TEST",
        date="2024-01-01",
        crowding_score=60.0,
        catalyst_quality="A",
        momentum_5d=3.0,
        volatility=5.0,
        market_sentiment=40.0,
        actual_return_3d=5.0 if win else -5.0,
        actual_return_7d=10.0 if win else -10.0,
        actual_return_30d=15.0 if win else -15.0,
        win_3d=win,
        win_7d=win,
        win_30d=win,
        # v2 新字段
        iv_rank=45.0,
        put_call_ratio=0.85,
        final_score=7.2,
        odds_score=6.5,
        risk_adj_score=7.0,
        agent_agreement=0.7,
        direction_encoded=1.0 if win else -1.0,
    )
    defaults.update(overrides)
    return TrainingData(**defaults)


def _build_training_set(n_pos: int = 5, n_neg: int = 3) -> list:
    """构建正负样本混合训练集（12 维特征均有差异化值）"""
    data = []
    for i in range(n_pos):
        data.append(_make_sample(
            win=True,
            crowding_score=50.0 + i * 2,
            catalyst_quality="A",
            momentum_5d=2.0 + i,
            volatility=4.0 + i * 0.5,
            market_sentiment=30 + i * 5,
            iv_rank=35.0 + i * 3,
            put_call_ratio=0.7 + i * 0.05,
            final_score=7.0 + i * 0.3,
            odds_score=6.0 + i * 0.4,
            risk_adj_score=6.5 + i * 0.3,
            agent_agreement=0.65 + i * 0.05,
            direction_encoded=1.0,
        ))
    for i in range(n_neg):
        data.append(_make_sample(
            win=False,
            crowding_score=80.0 + i * 2,
            catalyst_quality="C",
            momentum_5d=-2.0 - i,
            volatility=10.0 + i,
            market_sentiment=-20 - i * 10,
            iv_rank=70.0 + i * 5,
            put_call_ratio=1.5 + i * 0.2,
            final_score=3.5 - i * 0.3,
            odds_score=3.0 - i * 0.2,
            risk_adj_score=3.5 - i * 0.4,
            agent_agreement=0.35 - i * 0.05,
            direction_encoded=-1.0,
        ))
    return data


# ===========================================================================
# 共享工具函数测试
# ===========================================================================

class TestSharedUtils:
    """测试共享工具函数"""

    def test_encode_catalyst(self):
        assert _encode_catalyst("A+") == 1.0
        assert _encode_catalyst("A") == 0.85
        assert _encode_catalyst("B+") == 0.70
        assert _encode_catalyst("B") == 0.55
        assert _encode_catalyst("C") == 0.40
        assert _encode_catalyst("X") == 0.5  # 未知等级

    def test_extract_features_12d(self):
        """提取 12 维特征向量，值正确映射"""
        sample = _make_sample(
            crowding_score=70, catalyst_quality="B+",
            momentum_5d=4.0, volatility=6.0, market_sentiment=50,
            iv_rank=65.0, put_call_ratio=1.2, final_score=8.0,
            odds_score=7.5, risk_adj_score=6.8,
            agent_agreement=0.85, direction_encoded=1.0,
        )
        features = _extract_features(sample)
        assert len(features) == 12
        # v1 特征
        assert features[0] == 70.0   # crowding
        assert features[1] == 0.70   # B+ → 0.70
        assert features[2] == 4.0    # momentum
        assert features[3] == 6.0    # volatility
        assert features[4] == 50.0   # sentiment
        # v2 新特征
        assert features[5] == 65.0   # iv_rank
        assert features[6] == 1.2    # put_call_ratio
        assert features[7] == 8.0    # final_score
        assert features[8] == 7.5    # odds_score
        assert features[9] == 6.8    # risk_adj_score
        assert features[10] == 0.85  # agent_agreement
        assert features[11] == 1.0   # direction_encoded

    def test_feature_names_12(self):
        """FEATURE_NAMES 应有 12 个元素"""
        assert len(FEATURE_NAMES) == 12
        assert "crowding" in FEATURE_NAMES
        assert "sentiment" in FEATURE_NAMES
        # v2 新特征
        assert "iv_rank" in FEATURE_NAMES
        assert "put_call_ratio" in FEATURE_NAMES
        assert "final_score" in FEATURE_NAMES
        assert "odds_score" in FEATURE_NAMES
        assert "risk_adj_score" in FEATURE_NAMES
        assert "agent_agreement" in FEATURE_NAMES
        assert "direction_encoded" in FEATURE_NAMES

    def test_feature_names_v1(self):
        """V1 向后兼容列表应有 5 个元素"""
        assert len(FEATURE_NAMES_V1) == 5
        assert FEATURE_NAMES_V1 == ["crowding", "catalyst", "momentum", "volatility", "sentiment"]


# ===========================================================================
# TrainingData v2 新字段测试
# ===========================================================================

class TestTrainingDataV2:
    """测试 TrainingData v2 新字段与向后兼容"""

    def test_defaults(self):
        """仅传原 5 字段，新字段自动取默认值"""
        td = TrainingData(
            ticker="TEST", date="2024-01-01",
            crowding_score=60.0, catalyst_quality="A",
            momentum_5d=3.0, volatility=5.0, market_sentiment=40.0,
            actual_return_3d=5.0, actual_return_7d=10.0, actual_return_30d=15.0,
            win_3d=True, win_7d=True, win_30d=True,
        )
        assert td.iv_rank == 50.0
        assert td.put_call_ratio == 1.0
        assert td.final_score == 5.0
        assert td.odds_score == 5.0
        assert td.risk_adj_score == 5.0
        assert td.agent_agreement == 0.5
        assert td.direction_encoded == 0.0

    def test_custom_values(self):
        """全部传入自定义值"""
        td = _make_sample(
            iv_rank=80.0, put_call_ratio=2.1, final_score=9.0,
            odds_score=8.5, risk_adj_score=3.0,
            agent_agreement=0.95, direction_encoded=-1.0,
        )
        assert td.iv_rank == 80.0
        assert td.put_call_ratio == 2.1
        assert td.final_score == 9.0
        assert td.odds_score == 8.5
        assert td.risk_adj_score == 3.0
        assert td.agent_agreement == 0.95
        assert td.direction_encoded == -1.0

    def test_extract_features_with_defaults(self):
        """默认值的 TrainingData 提取 12 维向量"""
        td = TrainingData(
            ticker="X", date="2024-01-01",
            crowding_score=50.0, catalyst_quality="B",
            momentum_5d=0.0, volatility=5.0, market_sentiment=0.0,
            actual_return_3d=0.0, actual_return_7d=0.0, actual_return_30d=0.0,
            win_3d=False, win_7d=False, win_30d=False,
        )
        features = _extract_features(td)
        assert len(features) == 12
        # 默认值应正确出现
        assert features[5] == 50.0   # iv_rank default
        assert features[6] == 1.0    # put_call_ratio default
        assert features[7] == 5.0    # final_score default
        assert features[8] == 5.0    # odds_score default
        assert features[9] == 5.0    # risk_adj_score default
        assert features[10] == 0.5   # agent_agreement default
        assert features[11] == 0.0   # direction_encoded default


# ===========================================================================
# SimpleMLModel 测试
# ===========================================================================

class TestSimpleMLModel:
    """测试 SimpleMLModel（无 sklearn 依赖）"""

    def test_init(self):
        model = SimpleMLModel()
        assert not model.is_trained
        assert model.training_accuracy == 0.0
        assert len(model.weights) == 12
        assert model.feature_stats == {}

    def test_predict_untrained_no_crash(self):
        """未训练的 SimpleMLModel 调用 predict_probability 不应崩溃"""
        model = SimpleMLModel()
        prob = model.predict_probability(_make_sample())
        assert 0.0 <= prob <= 1.0

    def test_train(self):
        model = SimpleMLModel()
        data = HistoricalDataBuilder().get_training_data()
        result = model.train(data)
        assert result["status"] == "success"
        assert result["samples"] == len(data)
        assert model.is_trained
        assert model.training_accuracy > 0

    def test_train_empty(self):
        model = SimpleMLModel()
        result = model.train([])
        assert result["status"] == "error"

    def test_predict_probability(self):
        model = SimpleMLModel()
        data = HistoricalDataBuilder().get_training_data()
        model.train(data)
        prob = model.predict_probability(data[0])
        assert 0.0 <= prob <= 1.0

    def test_predict_return(self):
        model = SimpleMLModel()
        data = HistoricalDataBuilder().get_training_data()
        model.train(data)
        ret = model.predict_return(data[0])
        assert "probability" in ret
        assert "expected_3d" in ret
        assert "expected_7d" in ret
        assert "expected_30d" in ret

    def test_save_and_load(self):
        model = SimpleMLModel()
        data = HistoricalDataBuilder().get_training_data()
        model.train(data)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        try:
            model.save_model(path)

            model2 = SimpleMLModel()
            result = model2.load_model(path)
            assert result is True
            assert model2.is_trained
            assert model2.weights == model.weights
            assert model2.training_accuracy == model.training_accuracy
        finally:
            os.unlink(path)

    def test_load_nonexistent(self):
        model = SimpleMLModel()
        result = model.load_model("/tmp/nonexistent_model_xyz.json")
        assert result is False

    def test_encode_catalyst_quality(self):
        model = SimpleMLModel()
        assert model.encode_catalyst_quality("A+") == 1.0
        assert model.encode_catalyst_quality("C") == 0.40

    def test_normalize_feature(self):
        model = SimpleMLModel()
        assert model.normalize_feature(5, 0, 10) == 0.5
        assert model.normalize_feature(0, 0, 10) == 0.0
        assert model.normalize_feature(10, 0, 10) == 1.0
        assert model.normalize_feature(5, 5, 5) == 0.5  # min == max

    def test_12_features_predict(self):
        """SimpleMLModel 应能使用 12 维数据正确训练和预测"""
        model = SimpleMLModel()
        data = _build_training_set(n_pos=5, n_neg=3)
        result = model.train(data)
        assert result["status"] == "success"

        # 预测正样本和负样本
        pos_prob = model.predict_probability(data[0])
        neg_prob = model.predict_probability(data[-1])
        assert 0.0 <= pos_prob <= 1.0
        assert 0.0 <= neg_prob <= 1.0

    def test_feature_importance(self):
        """SimpleMLModel 应有 get_feature_importance() 方法"""
        model = SimpleMLModel()
        # 未训练时
        imp = model.get_feature_importance()
        assert isinstance(imp, dict)

        # 训练后
        data = _build_training_set()
        model.train(data)
        imp = model.get_feature_importance()
        assert len(imp) == 12
        for name, info in imp.items():
            assert name in FEATURE_NAMES
            assert "weight" in info


# ===========================================================================
# SGDMLModel 测试
# ===========================================================================

class TestSGDMLModel:
    """测试 SGDMLModel（需要 sklearn）"""

    @pytest.fixture
    def sgd_model(self):
        from ml_predictor import SGDMLModel
        return SGDMLModel()

    @pytest.fixture
    def training_data(self):
        return _build_training_set(n_pos=5, n_neg=3)

    def test_init(self, sgd_model):
        assert not sgd_model.is_trained
        assert sgd_model.training_accuracy == 0.0
        # 未训练时 weights 应返回均匀分布 (1/12)
        w = sgd_model.weights
        assert len(w) == 12
        expected = 1.0 / 12
        assert all(abs(v - expected) < 0.01 for v in w.values())

    def test_train(self, sgd_model, training_data):
        result = sgd_model.train(training_data)
        assert result["status"] == "success"
        assert result["samples"] == 8
        assert sgd_model.is_trained
        assert sgd_model.training_accuracy > 0
        assert len(sgd_model.weights) == 12
        # 权重之和应约为 1.0
        assert abs(sum(sgd_model.weights.values()) - 1.0) < 0.01

    def test_train_insufficient_samples(self, sgd_model):
        result = sgd_model.train([_make_sample()])
        assert result["status"] == "error"

    def test_predict_probability(self, sgd_model, training_data):
        sgd_model.train(training_data)
        prob = sgd_model.predict_probability(training_data[0])
        assert 0.0 <= prob <= 1.0

    def test_predict_probability_untrained(self, sgd_model):
        prob = sgd_model.predict_probability(_make_sample())
        assert prob == 0.5

    def test_predict_return(self, sgd_model, training_data):
        sgd_model.train(training_data)
        ret = sgd_model.predict_return(training_data[0])
        assert "probability" in ret
        assert "expected_3d" in ret
        assert "expected_7d" in ret
        assert "expected_30d" in ret

    def test_incremental_train(self, sgd_model, training_data):
        # 先全量训练
        sgd_model.train(training_data)

        # 增量学习
        new_data = [_make_sample(win=True, crowding_score=45, catalyst_quality="A+")]
        result = sgd_model.incremental_train(new_data)
        assert result["status"] == "success"
        assert result["new_samples"] == 1
        assert result["total_samples"] == 9

    def test_incremental_train_cold_start(self, sgd_model, training_data):
        """冷启动：未训练过直接 incremental_train 应降级为全量训练"""
        result = sgd_model.incremental_train(training_data)
        assert result["status"] == "success"
        assert sgd_model.is_trained

    def test_save_and_load(self, sgd_model, training_data):
        from ml_predictor import SGDMLModel
        sgd_model.train(training_data)

        sample = training_data[0]
        prob_original = sgd_model.predict_probability(sample)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        try:
            sgd_model.save_model(path)

            # 验证 JSON 内容
            with open(path) as f:
                saved = json.load(f)
            assert saved["model_type"] == "sgd"
            assert "sgd" in saved
            assert "scaler" in saved
            # v2 新增字段
            assert saved.get("feature_count") == 12
            assert saved.get("feature_names") == FEATURE_NAMES
            assert "feature_importance" in saved

            # 加载到新实例
            model2 = SGDMLModel()
            result = model2.load_model(path)
            assert result is True
            assert model2.is_trained
            assert model2._clf_fitted
            assert model2._scaler_fitted

            # 预测结果应一致
            prob_loaded = model2.predict_probability(sample)
            assert abs(prob_loaded - prob_original) < 1e-6
        finally:
            os.unlink(path)

    def test_save_load_12_features_roundtrip(self, sgd_model, training_data):
        """12 维模型 JSON 往返：coef 长度、scaler 维度、权重个数均为 12"""
        from ml_predictor import SGDMLModel
        sgd_model.train(training_data)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        try:
            sgd_model.save_model(path)
            with open(path) as f:
                saved = json.load(f)

            # coef 长度
            assert len(saved["sgd"]["coef"][0]) == 12
            # scaler mean/scale 长度
            assert len(saved["scaler"]["mean"]) == 12
            assert len(saved["scaler"]["scale"]) == 12

            # 加载后维度一致
            model2 = SGDMLModel()
            model2.load_model(path)
            assert len(model2.weights) == 12
        finally:
            os.unlink(path)

    def test_load_old_5_feature_model(self, sgd_model):
        """加载旧 5 维 SGD 模型 → is_trained=False → 触发重训"""
        old_model = {
            "model_type": "sgd",
            "sgd": {
                "coef": [[0.1, 0.2, -0.1, 0.3, -0.05]],
                "intercept": [0.0],
                "classes": [0, 1],
                "n_iter": 10,
            },
            "scaler": {
                "mean": [60.0, 0.7, 2.0, 5.0, 30.0],
                "scale": [15.0, 0.2, 3.0, 2.0, 30.0],
            },
            "is_trained": True,
            "training_accuracy": 75.0,
            "total_samples": 50,
            "feature_count": 5,
        }
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(old_model, f)
            path = f.name

        try:
            result = sgd_model.load_model(path)
            assert result is True
            # 维度不匹配 → 标记为未训练
            assert not sgd_model.is_trained
        finally:
            os.unlink(path)

    def test_load_legacy_format(self, sgd_model):
        """加载旧 SimpleMLModel 格式：不崩溃，标记为需重训"""
        legacy = {
            "weights": {"crowding": 0.3, "catalyst": 0.25, "momentum": 0.2,
                        "volatility": 0.15, "sentiment": 0.1},
            "feature_stats": {"crowding": {"min": 42, "max": 88, "mean": 65}},
            "is_trained": True,
            "training_accuracy": 87.5,
        }
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(legacy, f)
            path = f.name

        try:
            result = sgd_model.load_model(path)
            assert result is True
            # 旧格式加载后标记为未训练（需 SGD 重训）
            assert not sgd_model.is_trained
            assert sgd_model.feature_stats.get("crowding", {}).get("min") == 42
        finally:
            os.unlink(path)

    def test_load_nonexistent(self, sgd_model):
        result = sgd_model.load_model("/tmp/nonexistent_sgd_xyz.json")
        assert result is False

    def test_weights_setter_ignored(self, sgd_model):
        """weights setter 应允许赋值但不影响实际权重"""
        sgd_model.weights = {"crowding": 0.99}
        # 未训练时仍返回均匀分布 (1/12)
        expected = 1.0 / 12
        assert abs(sgd_model.weights["crowding"] - expected) < 0.01

    def test_encode_catalyst_quality(self, sgd_model):
        assert sgd_model.encode_catalyst_quality("A") == 0.85

    # --- 特征重要性测试 ---

    def test_feature_importance_trained(self, sgd_model, training_data):
        """训练后 get_feature_importance() 返回 12 项，按绝对值降序"""
        sgd_model.train(training_data)
        imp = sgd_model.get_feature_importance()
        assert len(imp) == 12

        # 所有特征名都在
        for name in FEATURE_NAMES:
            assert name in imp
            assert "weight" in imp[name]
            assert "coefficient" in imp[name]
            assert "direction" in imp[name]
            assert imp[name]["direction"] in ("positive", "negative", "neutral")

        # 权重之和应约为 1.0
        total_weight = sum(info["weight"] for info in imp.values())
        assert abs(total_weight - 1.0) < 0.01

        # 按绝对值降序排列
        weights = [info["weight"] for info in imp.values()]
        assert weights == sorted(weights, reverse=True)

    def test_feature_importance_untrained(self, sgd_model):
        """未训练时返回空 dict"""
        imp = sgd_model.get_feature_importance()
        assert imp == {}

    def test_incremental_12_features(self, sgd_model, training_data):
        """全量训练 + 增量训练均使用 12 维数据"""
        sgd_model.train(training_data)

        # 增量学习使用完整 12 维样本
        new_data = [
            _make_sample(
                win=True, crowding_score=48, catalyst_quality="A+",
                iv_rank=30.0, put_call_ratio=0.6, final_score=8.5,
                odds_score=8.0, risk_adj_score=7.5,
                agent_agreement=0.9, direction_encoded=1.0,
            ),
            _make_sample(
                win=False, crowding_score=85, catalyst_quality="C",
                iv_rank=80.0, put_call_ratio=2.0, final_score=3.0,
                odds_score=2.5, risk_adj_score=2.0,
                agent_agreement=0.3, direction_encoded=-1.0,
            ),
        ]
        result = sgd_model.incremental_train(new_data)
        assert result["status"] == "success"
        assert result["new_samples"] == 2
        assert result["total_samples"] == 10

        # 训练后权重仍为 12 维
        assert len(sgd_model.weights) == 12


# ===========================================================================
# 工厂函数测试
# ===========================================================================

class TestCreateMLModel:
    """测试 create_ml_model() 工厂"""

    def test_returns_sgd_when_sklearn_available(self):
        from ml_predictor import SGDMLModel
        model = create_ml_model()
        assert isinstance(model, SGDMLModel)

    def test_model_has_required_interface(self):
        model = create_ml_model()
        assert hasattr(model, "train")
        assert hasattr(model, "predict_probability")
        assert hasattr(model, "predict_return")
        assert hasattr(model, "save_model")
        assert hasattr(model, "load_model")
        assert hasattr(model, "is_trained")
        assert hasattr(model, "training_accuracy")
        assert hasattr(model, "weights")
        assert hasattr(model, "feature_stats")
        assert hasattr(model, "get_feature_importance")


# ===========================================================================
# MLPredictionService 测试
# ===========================================================================

class TestMLPredictionService:
    """测试 MLPredictionService"""

    def test_init(self):
        svc = MLPredictionService()
        assert svc.model is not None
        assert svc.data_builder is not None

    def test_train_model(self):
        svc = MLPredictionService()
        result = svc.train_model()
        assert result["status"] == "success"
        assert svc.model.is_trained

    def test_predict_for_opportunity(self):
        svc = MLPredictionService()
        sample = _make_sample()
        prediction = svc.predict_for_opportunity(sample)
        assert "ticker" in prediction
        assert "prediction" in prediction
        assert "recommendation" in prediction

    def test_predict_auto_trains(self):
        """首次预测时应自动训练模型"""
        svc = MLPredictionService()
        assert not svc.model.is_trained
        prediction = svc.predict_for_opportunity(_make_sample())
        assert svc.model.is_trained
        assert "prediction" in prediction

    def test_get_model_info(self):
        svc = MLPredictionService()
        svc.train_model()
        info = svc.get_model_info()
        assert info["is_trained"]
        assert info["training_accuracy"] > 0
        assert info["training_samples"] > 0
        assert "weights" in info

    def test_incremental_train(self):
        svc = MLPredictionService()
        svc.train_model()
        new_data = [_make_sample(win=True, crowding_score=55)]
        result = svc.incremental_train(new_data)
        # SGDMLModel 有 incremental_train 方法
        assert result.get("status") == "success"

    def test_prediction_structure(self):
        """验证预测返回的完整结构"""
        svc = MLPredictionService()
        svc.train_model()
        pred = svc.predict_for_opportunity(_make_sample())
        # 顶层字段
        assert "ticker" in pred
        assert "date" in pred
        assert "input" in pred
        assert "prediction" in pred
        assert "recommendation" in pred
        # prediction 子字段
        p = pred["prediction"]
        assert "probability" in p
        assert "expected_3d" in p
        assert "expected_7d" in p
        assert "expected_30d" in p
        assert 0.0 <= p["probability"] <= 1.0


# ===========================================================================
# 增量学习闭环集成测试
# ===========================================================================

class TestIncrementalTrainingLoop:
    """测试完整闭环：DB 验证数据 → TrainingData → incremental_train → save/load"""

    def test_full_loop_train_incremental_save_load(self):
        """全量训练 → 增量学习 → 序列化 → 反序列化 → 预测一致"""
        import tempfile

        svc = MLPredictionService()
        svc.train_model()  # 8 个硬编码样本

        # 模拟 5 条新验证数据
        verified = [
            _make_sample(win=True, crowding_score=50 + i, momentum_5d=2.0 + i)
            for i in range(5)
        ]
        result = svc.incremental_train(verified)
        assert result["status"] == "success"
        assert result["total_samples"] == 8 + 5

        # 保存
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            svc.model.save_model(path)

            # 加载到新实例并验证
            from ml_predictor import SGDMLModel
            model2 = SGDMLModel()
            model2.load_model(path)
            assert model2.is_trained

            # 预测结果应一致
            sample = _make_sample(win=True, crowding_score=60)
            prob1 = svc.model.predict_probability(sample)
            prob2 = model2.predict_probability(sample)
            assert abs(prob1 - prob2) < 1e-6
        finally:
            os.unlink(path)

    def test_incremental_improves_or_maintains(self):
        """增量学习后模型应保持功能正常（不崩溃，能预测）"""
        svc = MLPredictionService()
        svc.train_model()

        # 增量学习 3 批
        for batch in range(3):
            data = [_make_sample(
                win=batch % 2 == 0,
                crowding_score=55 + batch * 5,
                catalyst_quality="A" if batch % 2 == 0 else "C",
            )]
            svc.incremental_train(data)

        # 仍能正常预测
        pred = svc.predict_for_opportunity(_make_sample())
        assert "prediction" in pred
        assert 0.0 <= pred["prediction"]["probability"] <= 1.0

    def test_db_row_to_training_data_v2_conversion(self):
        """测试 DB 行 → TrainingData v2 转换（含新字段 + agent_agreement 计算）"""
        import json as _json

        def _cat_qual(v):
            if v >= 8.5: return "A+"
            if v >= 7.5: return "A"
            if v >= 6.5: return "B+"
            if v >= 5.5: return "B"
            return "C"

        direction_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}

        # 模拟 DB 行（含 v2 新列）
        db_row = {
            "ticker": "NVDA",
            "date": "2026-02-20",
            "dimension_scores": _json.dumps({
                "signal": 7.5, "catalyst": 8.2, "sentiment": 6.0,
                "odds": 7.8, "risk_adj": 6.5,
            }),
            "iv_rank": 42.0,
            "put_call_ratio": 0.75,
            "final_score": 8.1,
            "direction": "bullish",
            "agent_directions": _json.dumps({
                "ScoutBee": "bullish", "OracleBee": "bullish",
                "BuzzBee": "bearish", "ChronosBee": "bullish",
                "RivalBee": "bullish",
            }),
            "return_t7": 12.5,
            "correct_t7": 1,
        }

        ds = _json.loads(db_row["dimension_scores"])
        _ad = _json.loads(db_row.get("agent_directions") or "{}")
        _dir = db_row.get("direction", "neutral")

        # 计算 agent_agreement
        if _ad:
            _majority = sum(1 for d in _ad.values() if d == _dir)
            _agree = _majority / len(_ad)
        else:
            _agree = 0.5

        td = TrainingData(
            ticker=db_row["ticker"], date=db_row["date"],
            crowding_score=ds.get("signal", 5.0) * 10,
            catalyst_quality=_cat_qual(ds.get("catalyst", 5.0)),
            momentum_5d=0.0, volatility=5.0,
            market_sentiment=(ds.get("sentiment", 5.0) - 5) * 20,
            actual_return_3d=float(db_row["return_t7"]) * 0.4,
            actual_return_7d=float(db_row["return_t7"]),
            actual_return_30d=float(db_row["return_t7"]) * 2.5,
            win_3d=bool(db_row["correct_t7"]),
            win_7d=bool(db_row["correct_t7"]),
            win_30d=bool(db_row["correct_t7"]),
            # v2 新字段
            iv_rank=float(db_row.get("iv_rank") or 50.0),
            put_call_ratio=float(db_row.get("put_call_ratio") or 1.0),
            final_score=float(db_row.get("final_score") or 5.0),
            odds_score=ds.get("odds", 5.0),
            risk_adj_score=ds.get("risk_adj", 5.0),
            agent_agreement=_agree,
            direction_encoded=direction_map.get(_dir, 0.0),
        )

        # 验证原有字段
        assert td.ticker == "NVDA"
        assert td.crowding_score == 75.0  # 7.5 * 10
        assert td.catalyst_quality == "A"  # 8.2 → A
        assert td.market_sentiment == 20.0  # (6.0 - 5) * 20
        assert td.actual_return_7d == 12.5
        assert td.win_7d is True

        # 验证 v2 新字段
        assert td.iv_rank == 42.0
        assert td.put_call_ratio == 0.75
        assert td.final_score == 8.1
        assert td.odds_score == 7.8
        assert td.risk_adj_score == 6.5
        assert td.direction_encoded == 1.0  # bullish

        # agent_agreement: 4/5 bullish = 0.8
        assert abs(td.agent_agreement - 0.8) < 0.01

    def test_db_row_missing_new_columns(self):
        """旧 DB 行缺失新列 → 默认值"""
        import json as _json

        direction_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}

        db_row = {
            "ticker": "AAPL",
            "date": "2025-12-01",
            "dimension_scores": _json.dumps({"signal": 6.0, "catalyst": 7.0}),
            "return_t7": 5.0,
            "correct_t7": 1,
            # 缺少: iv_rank, put_call_ratio, final_score, direction, agent_directions
        }

        ds = _json.loads(db_row["dimension_scores"])
        _ad = _json.loads(db_row.get("agent_directions") or "{}")
        _dir = db_row.get("direction", "neutral")

        if _ad:
            _majority = sum(1 for d in _ad.values() if d == _dir)
            _agree = _majority / len(_ad)
        else:
            _agree = 0.5

        td = TrainingData(
            ticker=db_row["ticker"], date=db_row["date"],
            crowding_score=ds.get("signal", 5.0) * 10,
            catalyst_quality="B+",
            momentum_5d=0.0, volatility=5.0,
            market_sentiment=0.0,
            actual_return_3d=2.0, actual_return_7d=5.0, actual_return_30d=12.5,
            win_3d=True, win_7d=True, win_30d=True,
            iv_rank=float(db_row.get("iv_rank") or 50.0),
            put_call_ratio=float(db_row.get("put_call_ratio") or 1.0),
            final_score=float(db_row.get("final_score") or 5.0),
            odds_score=ds.get("odds", 5.0),
            risk_adj_score=ds.get("risk_adj", 5.0),
            agent_agreement=_agree,
            direction_encoded=direction_map.get(_dir, 0.0),
        )

        # 缺失列应落入默认值
        assert td.iv_rank == 50.0
        assert td.put_call_ratio == 1.0
        assert td.final_score == 5.0
        assert td.odds_score == 5.0      # dimension_scores 中无 "odds"
        assert td.risk_adj_score == 5.0   # dimension_scores 中无 "risk_adj"
        assert td.agent_agreement == 0.5  # 空 agent_directions → 默认
        assert td.direction_encoded == 0.0  # neutral → 0.0

    def test_agent_agreement_calculation(self):
        """agent_directions JSON → 共识比例计算正确"""
        import json as _json

        # 全部一致
        ad_all = {"A": "bullish", "B": "bullish", "C": "bullish"}
        majority = sum(1 for d in ad_all.values() if d == "bullish")
        assert majority / len(ad_all) == 1.0

        # 部分一致
        ad_partial = {"A": "bullish", "B": "bearish", "C": "bullish", "D": "neutral"}
        majority = sum(1 for d in ad_partial.values() if d == "bullish")
        assert abs(majority / len(ad_partial) - 0.5) < 0.01

        # 无共识（都不同方向对准 final direction）
        ad_none = {"A": "bearish", "B": "neutral", "C": "bearish"}
        majority = sum(1 for d in ad_none.values() if d == "bullish")
        assert majority / len(ad_none) == 0.0

        # 空 dict → 默认 0.5
        ad_empty = {}
        agree = 0.5 if not ad_empty else sum(1 for d in ad_empty.values() if d == "bullish") / len(ad_empty)
        assert agree == 0.5


# ===========================================================================
# HistoricalDataBuilder 测试
# ===========================================================================

class TestHistoricalDataBuilder:
    """测试 HistoricalDataBuilder"""

    def test_get_training_data(self):
        builder = HistoricalDataBuilder()
        data = builder.get_training_data()
        assert len(data) == 8  # 8 个硬编码样本

    def test_samples_have_v2_fields(self):
        """硬编码样本应包含 v2 新字段"""
        builder = HistoricalDataBuilder()
        data = builder.get_training_data()
        for td in data:
            # v2 字段应有非默认值（至少某些样本）
            assert hasattr(td, "iv_rank")
            assert hasattr(td, "put_call_ratio")
            assert hasattr(td, "final_score")
            assert hasattr(td, "odds_score")
            assert hasattr(td, "risk_adj_score")
            assert hasattr(td, "agent_agreement")
            assert hasattr(td, "direction_encoded")
            # 值范围校验
            assert 0 <= td.iv_rank <= 100
            assert td.put_call_ratio > 0
            assert 0 <= td.final_score <= 10
            assert 0 <= td.agent_agreement <= 1

    def test_add_record(self):
        builder = HistoricalDataBuilder()
        n_before = len(builder.get_training_data())
        builder.add_record(_make_sample())
        assert len(builder.get_training_data()) == n_before + 1

    def test_save_to_file(self):
        builder = HistoricalDataBuilder()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            builder.save_to_file(path)
            with open(path) as f:
                saved = json.load(f)
            assert len(saved) == 8
            assert saved[0]["ticker"] == "NVDA"
            # v2 新字段应序列化
            assert "iv_rank" in saved[0]
            assert "put_call_ratio" in saved[0]
            assert "final_score" in saved[0]
            assert "odds_score" in saved[0]
            assert "risk_adj_score" in saved[0]
            assert "agent_agreement" in saved[0]
            assert "direction_encoded" in saved[0]
        finally:
            os.unlink(path)


class TestScalerSerializationBug:
    """回归测试：save → load → incremental_train 不因 n_samples_seen_ 类型丢失而崩溃
    根因：JSON 反序列化后 n_samples_seen_ 是纯 int，sklearn partial_fit 需要 numpy 类型（.shape）"""

    def test_load_then_incremental_train_no_crash(self):
        """save → load → incremental_train 应正常工作（不抛 'int has no attribute shape'）"""
        import tempfile
        from ml_predictor import SGDMLModel, MLPredictionService

        svc = MLPredictionService()
        svc.train_model()

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            svc.model.save_model(path)

            # 加载到新实例（此时 n_samples_seen_ 来自 JSON 反序列化）
            model2 = SGDMLModel()
            model2.load_model(path)

            # 增量学习：这一步以前会崩溃
            new_data = [_make_sample(win=True, crowding_score=70, momentum_5d=3.0)]
            result = model2.incremental_train(new_data)
            assert result["status"] == "success"
            assert result["new_samples"] == 1

            # 预测仍正常
            prob = model2.predict_probability(_make_sample())
            assert 0.0 <= prob <= 1.0
        finally:
            os.unlink(path)

    def test_scaler_n_samples_seen_is_numpy_after_load(self):
        """加载后 scaler.n_samples_seen_ 应为 numpy 类型（有 .shape 属性）"""
        import tempfile
        import numpy as np
        from ml_predictor import SGDMLModel, MLPredictionService

        svc = MLPredictionService()
        svc.train_model()

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            svc.model.save_model(path)

            model2 = SGDMLModel()
            model2.load_model(path)

            # 验证类型：必须有 .shape 属性
            assert hasattr(model2._scaler.n_samples_seen_, "shape"), \
                f"n_samples_seen_ type {type(model2._scaler.n_samples_seen_)} 缺少 .shape"
            # 验证值正确
            assert int(model2._scaler.n_samples_seen_) == int(svc.model._scaler.n_samples_seen_)
        finally:
            os.unlink(path)

    def test_multiple_save_load_incremental_cycles(self):
        """多轮 save→load→incremental→save 循环不退化"""
        import tempfile
        from ml_predictor import SGDMLModel, MLPredictionService

        svc = MLPredictionService()
        svc.train_model()

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            for cycle in range(3):
                svc.model.save_model(path)

                model2 = SGDMLModel()
                model2.load_model(path)

                new_data = [_make_sample(
                    win=cycle % 2 == 0,
                    crowding_score=50 + cycle * 10,
                )]
                result = model2.incremental_train(new_data)
                assert result["status"] == "success", f"cycle {cycle} failed"

                # 替换为更新后的模型继续下一轮
                svc.model = model2
        finally:
            os.unlink(path)
