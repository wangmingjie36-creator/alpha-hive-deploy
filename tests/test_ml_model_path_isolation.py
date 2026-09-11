"""ML 模型产物路径隔离 + 「被夹具覆盖」加载守卫（v0.45.149）。

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
    MLPredictionService,
    PoisonedModelError,
    TrainingData,
    _OOS_MIN_SAMPLES,
    default_model_path,
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


# ===================== ① 路径：产物不落仓库根 =====================

class TestModelPathIsolation:
    """`train_model()` 的无参 `save_model()` 必须落在沙箱里。"""

    def test_default_path_is_absolute_and_not_cwd_relative(self):
        """默认路径必须是绝对路径 —— 相对路径才是「在哪跑就写到哪」的根因。"""
        p = default_model_path()
        assert pathlib.Path(p).is_absolute(), f"默认模型路径不是绝对路径：{p}"

    def test_train_model_writes_into_sandbox_not_repo_root(self, tmp_path):
        """成对断言：① 仓库根那份没被动；② 沙箱里确实多出了一份。

        只有 ① 的话，把 `save_model` 改成 `pass` 也能绿 —— 那正是我们要
        排除的「什么都没发生也算通过」。
        """
        real = REPO_ROOT / "ml_model.json"
        before = real.read_bytes() if real.exists() else None

        sandbox_model = pathlib.Path(default_model_path())
        assert not sandbox_model.exists(), "前置条件：沙箱里本不该已有模型"

        result = MLPredictionService().train_model()
        assert result.get("status") == "success", (
            f"训练没成功，本测试测不到 save 路径：{result}")

        # ② 沙箱里确实写出来了（判别力的一半）
        assert sandbox_model.exists(), (
            f"train_model() 成功了却没在沙箱落盘：{sandbox_model}")
        assert sandbox_model.is_relative_to(tmp_path), (
            f"落盘位置逃出沙箱：{sandbox_model}")

        # ① 仓库根那份逐字节没变（另一半）
        after = real.read_bytes() if real.exists() else None
        assert after == before, (
            f"跑测试改到了仓库根的 {real} —— 这正是 v0.45.149 要根治的事故")

    @pytest.mark.parametrize("mod_name,cls_name,expected_attr", [
        ("ml_predictor", "SimpleMLModel", "ml_model"),
        ("ml_predictor", "SGDMLModel", "ml_model"),
        ("ml_predictor", "HGBModel", "ml_model"),
        ("ml_predictor_extended", "SimpleMLModel", "ml_model_extended"),
    ])
    def test_no_arg_save_lands_in_sandbox(self, mod_name, cls_name, expected_attr,
                                          tmp_path, monkeypatch):
        """**逐个类**走真实接线：无参 `save_model()` 必须落进沙箱。

        为什么不能只断言 `default_model_path()` 返回绝对路径 —— mutation M7
        证过：把 `save_model` **函数体**里的解析改回相对字面量，那种断言照样
        全绿。它测的是被调函数，不是接线（v0.45.126 的教训）。
        这里只认「调完之后文件出现在哪」。

        `ml_predictor_extended` 是 `ml_predictor` 导入失败时的降级实现，
        同病必须一起治（「改二分支先问另一支是不是同病更重」）。
        """
        import importlib
        from hive_logger import PATHS

        mod = importlib.import_module(mod_name)
        model = getattr(mod, cls_name)()

        target = getattr(PATHS, expected_attr)
        assert target.is_relative_to(tmp_path), f"前置条件坏了：{target} 不在沙箱"
        assert not target.exists(), "前置条件：沙箱里本不该已有模型文件"

        repo_copy = REPO_ROOT / target.name
        before = repo_copy.read_bytes() if repo_copy.exists() else None

        model.save_model()          # ← 无参，正是生产 train_model() 的调用方式

        assert target.exists(), (
            f"{mod_name}.{cls_name}.save_model() 无参调用后，沙箱里没有 {target}"
            " —— 它把文件写到别处去了")
        after = repo_copy.read_bytes() if repo_copy.exists() else None
        assert after == before, f"写到了仓库根的 {repo_copy}"

    def test_extended_default_path_is_absolute(self):
        """helper 本身也断言一次（与上面那条是不同层：一个测接线，一个测取值）。"""
        import ml_predictor_extended as mpe
        p = mpe.default_extended_model_path()
        assert pathlib.Path(p).is_absolute(), f"extended 默认路径不是绝对路径：{p}"


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

    def test_default_model_path_follows_env(self, tmp_path, monkeypatch):
        """`ml_predictor.default_model_path()` 同理：函数而非模块级常量。"""
        first = pathlib.Path(default_model_path())
        elsewhere = tmp_path / "elsewhere2"
        elsewhere.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(elsewhere))
        second = pathlib.Path(default_model_path())

        assert second != first and second.is_relative_to(elsewhere), (
            f"default_model_path() 没跟着环境变量变（{first} -> {second}）"
            " —— 多半被改成了模块级常量，import 时就冻住了。")


# ===================== ② 守卫：拒绝被夹具覆盖的模型 =====================

class TestPoisonedModelGuard:
    """`oos_accuracy is None` + 样本量偏低 = 「被测试夹具覆盖」的机读签名。"""

    def test_rejects_fixture_signature(self, tmp_path):
        """n=30 / oos=None —— 生产 2026-09-07 01:15 实际被写成的那份。"""
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
