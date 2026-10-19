"""墓碑（v0.45.547）：扫描进程里 ML 模型只训一次——归报告生成器；扫描开头的重训与扫描后的「增量学习」都已删。

- 扫描开头：`_init_scan_context` 曾读 ml_model_cache.json 再 `MLPredictionService().train_model()` 重训，交给蜂后算影子乘数。
  与报告生成器 __init__ 那次同一个库、同一个 random_state ⇒ 训出来逐位相同（`experiments/ml_dedupe_proof_v0547.py` 在生产库
  副本上实测：OOS / 特征重要性 / 样本预测概率 / 蜂后影子乘数全部相等）。现在直接交 `self.ml_generator.ml_service.model`。
- 扫描后：「ML 增量学习」用 50 条编造特征（momentum 恒 0、volatility 恒 5.0）调 HGB `incremental_train`，而它只看列表非空、
  随即从库里全量重训并覆盖 ml_model_cache.json——次日按缓存日期规则照样重训，产物只给当天之后的进程用，还把当天取证快照换成
  一个没打过分的模型；缓存缺失时更会直接 `train()` 在编造特征上。随之删掉唯一调用方的 `PredictionStore.get_recently_verified_t7`。
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests import _ast_cache

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
DAILY = REPO_ROOT / "alpha_hive_daily_report.py"
RETRAIN_CALLS = {"train_model", "incremental_train", "get_recently_verified_t7"}


def _calls(tree, names) -> list[tuple[str, int]]:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name in names:
                out.append((name, n.lineno))
    return out


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"找不到函数 {name}")


class TestSingleTrainingOwner:

    def test_daily_report_never_trains(self):
        assert not _calls(_ast_cache.parse_file(DAILY), RETRAIN_CALLS | {"MLPredictionService"})

    def test_queen_gets_the_generator_model(self):
        fn = _func(_ast_cache.parse_file(DAILY), "_init_scan_context")
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert "ml_generator" in attrs, "蜂后的模型应取自 self.ml_generator"
        assert not _calls(fn, {"load_model"}), "扫描开头不该再自己读缓存"

    def test_generator_still_owns_training(self):
        """正对照：同一探测法认得出生成器里那一次训练——否则上面两条的「没有」可能只是探测器瞎了。"""
        assert _calls(_ast_cache.parse_file(REPO_ROOT / "generate_ml_report.py"), {"train_model"})

    def test_retired_api_and_config_key_are_gone(self):
        from backtester import PredictionStore
        import config
        assert not hasattr(PredictionStore, "get_recently_verified_t7")
        assert "daily_retrain" not in config.ML_TRAINING_CONFIG


class TestDetectorHasTeeth:

    def test_call_detector(self):
        assert _calls(ast.parse("ml_svc.model.incremental_train(new_training)\n"), RETRAIN_CALLS)
        assert _calls(ast.parse("_retrain = _ml_svc_tmp.train_model()\n"), RETRAIN_CALLS)
        assert _calls(ast.parse("rows = bt.store.get_recently_verified_t7(limit=50)\n"), RETRAIN_CALLS)
        assert not _calls(ast.parse("bt.adapt_weights(min_samples=10)\n"), RETRAIN_CALLS)
