"""ML 模型产物路径隔离 + 「被夹具覆盖」加载守卫（v0.45.149；缺省路径 v0.45.570 删除，见墓碑 test_ml_model_default_path_retired.py）。

事故复盘见 `tests/conftest.py::_isolate_ml_model_file` 的 docstring 与
CHANGELOG v0.45.148 补记 B。这里只放**会红**的断言。

⚠️ 本文件的每条断言都是**成对**的，成对的那一半不是凑数：
  · 「没写到仓库根」必须配「确实写到了沙箱」—— 否则把 `save_model` 改成
    空函数，前半条照样绿。
  · 「拒绝中毒模型」必须配「放行健康模型」和「放行大样本但 OOS 缺失的模型」
    —— 否则把守卫写成 `raise` 一切，前半条照样绿。
"""

import inspect
import json
import pathlib

import pytest

import ml_predictor
from ml_predictor import (
    HGBModel,
    PoisonedModelError,
    TrainingData,
    _OOS_MIN_SAMPLES,
)


REPO_ROOT = pathlib.Path(ml_predictor.__file__).resolve().parent


def _sample(**kw):
    base = dict(
        ticker="TEST", date="2026-09-04", crowding_score=50.0,
        catalyst_quality="B", momentum_5d=0.0, volatility=30.0,
        market_sentiment=0.0, actual_return_3d=0.0, actual_return_7d=0.0,
        actual_return_30d=0.0, win_3d=False, win_7d=False, win_30d=False,
    )
    base.update(kw)
    return TrainingData(**base)


def _model_json(n_samples, oos, training_accuracy=96.67):
    """一份形状合法的 HGB 模型 JSON（不含 model_bytes，够守卫判了）。"""
    return {
        "model_type": "hgb",
        "feature_count": len(ml_predictor.FEATURE_NAMES),
        "feature_names": ml_predictor.FEATURE_NAMES,
        "is_trained": True,
        "training_accuracy": training_accuracy,
        "oos_accuracy": oos,
        "n_samples_seen": n_samples,
        "feature_stats": {},
        "weights": {},
        "feature_importance": {},
    }


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# ===================== ① 路径：调用时求值 =====================
# v0.45.570：原「① 路径：产物不落仓库根」一组（`train_model()` / 无参 `save_model()` 必须落进沙箱）随缺省路径
# 一起退役——没有缺省路径了，无参调用直接 TypeError、`train_model()` 不写任何文件。
# 那一组的判据（成对：沙箱里确实没新增 + 显式路径确实写得出来）搬进墓碑 `tests/test_ml_model_default_path_retired.py`。

class TestPathResolvedLazily:
    """路径必须**调用时**求值 —— 冻住就等于隔离失效。

    v0.45.149 在修默认参数时当场撞上的第二处、且更严重：
    `MLEnhancedReportGenerator._model_file` 曾是**类属性**
    `PATHS.home / "ml_model_cache.json"`。类体在 import 那一刻求值一次，
    而 `tests/` 里有 7 个模块在**模块级** import 本类，pytest 收集期跑在
    任何 fixture 之前（`ALPHA_HIVE_HOME` 尚未设）⇒ 整个 session 冻成仓库根，
    `_isolate_env` 对它完全无效 ⇒ 跑一次全套测试就写穿了生产真正读的那份模型。

    判据取「改 env 之后值跟不跟着变」而不是「值长得对不对」：
    后者在收集期恰好没被污染的单文件运行下也会绿，分不出冻没冻。
    """

    @staticmethod
    def _resolve(owner, name):
        """绕过实例化取值：property 就调 fget，普通属性就直接返回（= 冻住的证据）。"""
        attr = inspect.getattr_static(owner, name)
        return pathlib.Path(attr.fget(owner) if isinstance(attr, property) else attr)

    def test_ml_report_cache_path_follows_env(self, tmp_path, monkeypatch):
        from generate_ml_report import MLEnhancedReportGenerator as G

        first = self._resolve(G, "_model_file")
        assert first.is_relative_to(tmp_path), f"沙箱外：{first}"

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(elsewhere))
        second = self._resolve(G, "_model_file")

        assert second != first and second.is_relative_to(elsewhere), (
            f"`_model_file` 没跟着 ALPHA_HIVE_HOME 变（{first} -> {second}）"
            " —— 它又被写成了 import 时求值的类属性，测试隔离对它无效。")

# ===================== ② 守卫：拒绝被夹具覆盖的模型 =====================

class TestPoisonedModelGuard:
    """`oos_accuracy is None` + 样本量偏低 ⇒ 拒绝加载（小样本、无外样本验证）。

    ⚠️ 2026-10-19 更正：v0.45.149 称之为「被测试夹具覆盖」的机读签名，不成立——n=30 / oos None
    是 v0.45.538 前每次生产扫描 RivalBee 都写的合成模型；污染 08-28 / 09-04 报告的是 n=200、oos 有值的模型。
    """

    def test_rejects_fixture_signature(self, tmp_path):
        """n=30 / oos=None —— 生产 2026-09-07 01:15 实际被写成的那份（pytest 里跑 RivalBee 的测试写的合成模型）。"""
        path = _write(tmp_path / "poisoned.json", _model_json(30, None))
        with pytest.raises(PoisonedModelError):
            HGBModel().load_model(path)

    def test_rejection_does_not_depend_on_accuracy(self, tmp_path):
        """夹具的 accuracy 比真模型**更好看**（100.0 vs 71.63）。

        这条锁死判据：拿 accuracy 判会把最该拦的那个当「训练得好」放行。
        真模型 71.63% 必须放行，假模型 100.0% 必须拒绝 —— 若守卫改用
        accuracy 阈值，这两条里必有一条红。
        """
        fake = _write(tmp_path / "fake.json", _model_json(35, None, training_accuracy=100.0))
        with pytest.raises(PoisonedModelError):
            HGBModel().load_model(fake)

        real = _write(tmp_path / "real.json",
                      _model_json(497, 44.354838709677416, training_accuracy=71.6297786720322))
        assert HGBModel().load_model(real) is True, "真实生产模型被误拒"

    def test_accepts_large_sample_with_missing_oos(self, tmp_path):
        """样本够却 oos 缺失 = OOS 验证自己失败了，不是中毒 —— 必须放行。

        少了这一条，把守卫写成「oos 缺失就拒」也能全绿，而那会顺手废掉
        一个合法的大样本模型。
        """
        path = _write(tmp_path / "oos_eval_failed.json",
                      _model_json(_OOS_MIN_SAMPLES, None))
        assert HGBModel().load_model(path) is True, (
            f"n={_OOS_MIN_SAMPLES}（>= 闸门）且 oos 缺失被误判为中毒")

    def test_boundary_is_derived_from_train_gate(self, tmp_path):
        """边界必须**紧贴** `train()` 里那个同名闸门，不能是另写的魔数。"""
        below = _write(tmp_path / "below.json", _model_json(_OOS_MIN_SAMPLES - 1, None))
        with pytest.raises(PoisonedModelError):
            HGBModel().load_model(below)

    def test_unusable_n_samples_is_rejected(self, tmp_path):
        """`n_samples_seen` 不是可用数值时按 0 处理 = 拒绝（安全方向）。

        `bool` 是 `int` 子类、NaN 对任何比较都返回 False —— 类型闸照抄
        本仓 `usable_dim`，不自己发明（v0.45.121）。
        """
        for bad in (None, True, float("nan"), "497"):
            path = _write(tmp_path / "bad.json", _model_json(bad, None))
            with pytest.raises(PoisonedModelError):
                HGBModel().load_model(path)

    def test_error_is_a_valueerror_subclass(self):
        """生产三个 load_model 调用点的 except 都写的是 ValueError。

        换个基类，拒绝就从「走既有重训分支」变成「打断整条流水线」。
        """
        assert issubclass(PoisonedModelError, ValueError)
