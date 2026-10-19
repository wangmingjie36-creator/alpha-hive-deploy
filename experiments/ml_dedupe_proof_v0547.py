#!/usr/bin/env python3
"""v0.45.547：扫描开头那次 ML 重训与报告生成器训出来的模型是否逐位相同（只读生产库，写沙箱）。

旧：`_init_scan_context` 读 ml_model_cache.json，再 `MLPredictionService().train_model()` 重训，把它交给蜂后。
新：直接交报告生成器 `MLEnhancedReportGenerator().ml_service.model`（__init__ 里已训好 / 读好当天模型）。
蜂后自 v0.45.537 起只用它算影子乘数 ⇒ 判据：两种取法在**同一份生产库副本**上的
特征重要性、OOS、对样本的预测概率、蜂后影子乘数逐位相等。

用法：/usr/local/bin/python3 experiments/ml_dedupe_proof_v0547.py <沙箱目录>
（同一进程内先后训练两次——生产里两次也在同一进程、相隔约 1 秒、中间无写库。）
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

SANDBOX = Path(sys.argv[1]).resolve()
SANDBOX.mkdir(parents=True, exist_ok=True)
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 生产库只读拷进沙箱（sqlite 在线备份，不碰生产文件）
from data_backup.sqlite_readonly import read_only_connect  # noqa: E402
src = read_only_connect(Path.home() / "alpha-hive-data" / "pheromone.db")
dst = sqlite3.connect(str(SANDBOX / "pheromone.db"))
src.backup(dst)
dst.close()
src.close()

os.environ["ALPHA_HIVE_HOME"] = str(SANDBOX)
os.environ["ALPHA_HIVE_MODEL_SNAPSHOT_DISABLE"] = "1"
assert not (SANDBOX / "ml_model_cache.json").exists(), "沙箱里不该有旧缓存（否则生成器会读缓存而不是训练）"

from generate_ml_report import MLEnhancedReportGenerator  # noqa: E402
from ml_predictor import MLPredictionService, TrainingData  # noqa: E402
from pheromone_board import PheromoneBoard  # noqa: E402
from swarm_agents import QueenDistiller  # noqa: E402

new_model = MLEnhancedReportGenerator().ml_service.model       # 新取法
old_svc = MLPredictionService()                                 # 旧取法：再训一遍
old_res = old_svc.train_model()
old_model = old_svc.model
assert old_res.get("status") == "success" and old_model.is_trained and new_model.is_trained

probe = [TrainingData(ticker=t, date="2026-10-16", crowding_score=c, catalyst_quality=q, momentum_5d=m, volatility=v,
                      market_sentiment=s, actual_return_3d=0, actual_return_7d=0, actual_return_30d=0,
                      win_3d=False, win_7d=False, win_30d=False)
         for t, c, q, m, v, s in [("NVDA", 62, "A", -5.4, 3.1, -10), ("VKTX", 35, "C", -11.2, 6.0, 20),
                                  ("TSLA", 80, "B", -6.2, 4.4, 0), ("ABBV", 50, "B", 2.0, 1.5, 5)]]


def facts(m):
    return {
        "oos_accuracy": m.oos_accuracy,
        "feature_importance": m.get_feature_importance(),
        "probabilities": [m.predict_probability(d) for d in probe],
        "queen_shadow": QueenDistiller(PheromoneBoard(), enable_llm=False, ml_model=m).ml_adjustments_shadow,
    }


a, b = facts(old_model), facts(new_model)
same = {k: json.dumps(a[k], sort_keys=True) == json.dumps(b[k], sort_keys=True) for k in a}
print(json.dumps({"same": same, "old": a, "new": b}, ensure_ascii=False, indent=1, default=str))
print("VERDICT", "PASS" if all(same.values()) else "FAIL")
sys.exit(0 if all(same.values()) else 1)
