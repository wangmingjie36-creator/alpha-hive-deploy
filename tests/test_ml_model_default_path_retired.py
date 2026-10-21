"""墓碑（v0.45.570）：ML 模型的缺省落盘路径 `ml_model.json` 删除——别让它悄悄回来。

来源：v0.45.543 读者登记表（`tests/test_persistent_writes_have_readers.py`）里最后一条文件类 PENDING_DEAD。
`MLPredictionService.train_model()` 训完**无参** `save_model()`，落到缺省路径 `PATHS.ml_model`（数据根 `ml_model.json`）：

- 全仓零个读者（无参 `load_model` 调用点从 2026-02-24 导入模板模块那天起就是 0）；
- 生产唯一走到它的是 `generate_ml_report.__init__`，而它紧接着把**同一个模型**显式写进取证副本
  `ml_model_cache.json`——生产数据根 10-19 / 10-20 两份文件 sha256 逐字节相同，`ml_model_history/` 每天各快照一份相同的。

用户 2026-10-21 选「删掉缺省落盘」：`save_model` / `load_model` 的 `filename` 必填、`default_model_path()` 与
`PATHS.ml_model` 删除、`train_model()` 不再存盘、备份导出清单改为写明排除理由。读者登记表随之按设计变红点名
⇒ 删条目、`CEILING_PENDING_DEAD` 3 → 2。

顺带根治 v0.45.149 那类事故：缺省路径一旦存在，`tests/` 里十来处无参 `train_model()` 就会写到「缺省指向的地方」——
那条路径指错一次（相对路径、冻结常量、与 `ml_model_cache` 合并）就写穿生产。**没有缺省，就没有可以指错的东西。**
生产数据根里旧的 `ml_model.json` 原样留着（删 = 改生产数据，未做）。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import ml_predictor
from hive_logger import PATHS, _HivePaths
from ml_predictor import MLPredictionService, TrainingData
from tests import _persistent_io_scan as S

RETIRED_NAME = "ml_model.json"
MODEL_CLASSES = ("SimpleMLModel", "SGDMLModel", "HGBModel")

#: 生产源码里**允许**以完整字符串出现 `ml_model.json` 的作用域（双向：多了是写入方回来了，少了是登记过期）。
#: 都只是在**列举**数据根里有过的文件，没有一处拿它当路径写：
ALLOWED_MENTIONS = {
    "data_backup/export.py::<module>",            # EXCLUDED_FROM_THIS_PASS 的键：写明不再导出的理由
    "data_backup/migrate_data_root.py::<module>",  # 阶段 5 一次性迁移的 MOVE 清单（历史：迁移那天它确实在）
}


def _mentions(facts: dict, name: str) -> set:
    """作用域里以**完整文件名**出现的字符串（不含 docstring；f-string 占位已渲染）。

    比完整文件名而非片段：片段 `ml_model` 会误中 `ml_model_cache.json`、`ml_model_history`、`ml_model_guard`。
    """
    return {f"{rel}::{q}" for rel, ff in facts.items() for q, s in ff.scopes.items()
            if any(t == name or t.endswith("/" + name) for t in s["texts"])}


def _training_records(n: int = 8) -> list:
    return [TrainingData(ticker=f"S{i}", date="2024-01-01", crowding_score=30.0 + 5 * i,
                         catalyst_quality="AB"[i % 2], momentum_5d=float(i - 4), volatility=5.0,
                         market_sentiment=0.0, actual_return_3d=0.0, actual_return_7d=0.0,
                         actual_return_30d=0.0, win_3d=i % 3 != 0, win_7d=i % 3 != 0, win_30d=i % 3 != 0)
            for i in range(n)]


def _files_under(root: Path) -> set:
    """数据根里的文件（相对路径），不含 `logs/`——训练会打日志，日志不是产物。"""
    if not root.exists():
        return set()
    rels = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    return {r for r in rels if not r.startswith("logs/")}


class TestDefaultPathStaysRetired:

    def test_no_default_path_helper_or_paths_attribute(self):
        assert not hasattr(ml_predictor, "default_model_path"), (
            "ml_predictor.default_model_path 又回来了——缺省落点 ml_model.json 零读者（v0.45.570 删除）")
        assert inspect.getattr_static(_HivePaths, "ml_model", None) is None, (
            "PATHS.ml_model 又回来了——它只服务于已删的缺省落点")

    @pytest.mark.parametrize("cls_name", MODEL_CLASSES)
    @pytest.mark.parametrize("method", ("save_model", "load_model"))
    def test_filename_is_required(self, cls_name, method):
        param = inspect.signature(getattr(getattr(ml_predictor, cls_name), method)).parameters["filename"]
        assert param.default is inspect.Parameter.empty, (
            f"{cls_name}.{method} 的 filename 又有了缺省值 {param.default!r}——有缺省，无参调用的测试就写得到它")

    @pytest.mark.parametrize("cls_name", MODEL_CLASSES)
    def test_no_arg_save_raises_and_explicit_save_writes(self, cls_name, tmp_path):
        model = getattr(ml_predictor, cls_name)()
        with pytest.raises(TypeError):
            model.save_model()
        # 正对照：显式路径照样写得出来（否则把 save_model 改成空函数，上面那条也绿）
        target = tmp_path / "explicit.json"
        model.save_model(str(target))
        assert target.is_file()

    def test_retired_filename_is_not_a_path_anywhere_in_production(self):
        hits = _mentions(S.repo_facts(), RETIRED_NAME)
        assert hits == ALLOWED_MENTIONS, (
            f"生产源码里以完整文件名出现 {RETIRED_NAME} 的作用域变了：多出 {sorted(hits - ALLOWED_MENTIONS)}"
            f"（写入方回来了？真要恢复，先在 test_persistent_writes_have_readers.py 登记读者）；"
            f"少了 {sorted(ALLOWED_MENTIONS - hits)}（那就从 ALLOWED_MENTIONS 删掉，别留过期放行）")


class TestTrainModelWritesNothing:

    def test_train_model_leaves_the_data_root_untouched(self, tmp_path):
        home = Path(PATHS.home)
        assert home.is_relative_to(tmp_path), f"前置条件：数据根 {home} 不在本测试沙箱"
        before = _files_under(home)

        svc = MLPredictionService()
        svc.fallback_records = _training_records()
        result = svc.train_model()
        assert result.get("status") == "success", f"训练没成功，测不到「训完存不存盘」：{result}"
        assert _files_under(home) == before, (
            f"train_model() 在数据根里新写了文件：{sorted(_files_under(home) - before)}——"
            "v0.45.570 起训完不存盘，要留档的调用方显式传路径")

        # 正对照：同一个探测器认得出真实写入（报告生成器那条显式路径）——否则「没新增」可能只是没看见
        svc.model.save_model(str(PATHS.ml_model_cache))
        assert _files_under(home) - before == {Path(PATHS.ml_model_cache).relative_to(home).as_posix()}


class TestTombstoneHasTeeth:

    def test_mentions_sees_a_revived_writer_and_ignores_docstrings(self):
        src = ('from hive_logger import PATHS\n'
               'def save(m):\n'
               '    """以前写 `ml_model.json`——docstring 里提到不算。"""\n'
               '    open(PATHS.home / "ml_model.json", "w")\n'
               'def save2(home):\n'
               '    open(f"{home}/ml_model.json", "w")\n'
               'def other():\n'
               '    return "ml_model_cache.json"\n')
        facts = {"x.py": S.scan_source(src, "x.py")}
        assert _mentions(facts, RETIRED_NAME) == {"x.py::save", "x.py::save2"}

    def test_mentions_sees_the_live_sibling_in_the_real_repo(self):
        # 正对照：仍在写的取证副本，在真仓库上认得出来（判据不是对着空气恒绿）
        assert "hive_logger.py::_HivePaths.ml_model_cache" in _mentions(S.repo_facts(), "ml_model_cache.json")
