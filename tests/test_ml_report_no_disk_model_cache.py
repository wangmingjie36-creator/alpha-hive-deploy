"""墓碑（v0.45.551）：ML 报告生成器**不再读回**磁盘上的 `ml_model_cache.json`；每份报告记下出概率的模型指纹。

为什么有这个文件
----------------
2026-08-28 / 09-04 两天，ML 报告 12/12 份概率逐位相同（0.14901620144018954 / 0.5899693787928219）。
2026-10-19 重查逐位复现了真因：pytest 进程（08-29 03:08、09-04 10:38）经当时 import 期冻结的类属性
把一个 200 样本模型写进生产 `ml_model_cache.json`；生成器的缓存规则是「文件 mtime 是今天（PDT）就照用、
不核来源」⇒ 当天扫描一启动就把它读进类级内存缓存，后来扫描自己把真模型写回磁盘也救不回。

污染入口（冻结路径）v0.45.149 / 150 已堵，但「当天任何进程写的文件都被当成自己的模型」这条信任还在。
它换来的只是省一次训练——而训练确定性（random_state=42）、生产库副本上约 1 秒，两个独立进程训出的
模型字节相同 ⇒ 读回缓存省不下什么。故删掉读回：每个进程自己训一次，文件只写不读（取证 + 快照）。

另一半是可观测性：那两天的归因要翻日志 + 拿库备份按指纹搜训练截止日才查清，因为报告里没记出概率的
是哪个模型。现在 `ml_prediction.model.sha256` 随每份报告落盘，`ml_model_guard` 当日描述里列出指纹。
"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path

import pytest

from tests import _ast_cache, _repo_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__


@pytest.fixture
def gen_cls(monkeypatch):
    """每条测试一个干净的进程内缓存（类级字典跨测试共享）。"""
    from generate_ml_report import MLEnhancedReportGenerator as G
    monkeypatch.setattr(G, "_model_cache", {})
    return G


def _rows(n: int):
    from ml_predictor import TrainingData
    out = []
    for i in range(n):
        win = (i * 7) % 3 != 0
        out.append(TrainingData(
            ticker=f"T{i % 5}", date=f"2026-0{1 + i // 28}-{1 + i % 28:02d}",
            crowding_score=float((i * 13) % 100), catalyst_quality="B+",
            momentum_5d=float((i % 11) - 5), volatility=float(5 + i % 7),
            market_sentiment=float((i * 17) % 200 - 100),
            actual_return_3d=0.0, actual_return_7d=1.0 if win else -1.0, actual_return_30d=0.0,
            win_3d=win, win_7d=win, win_30d=win,
            iv_rank=float((i * 29) % 100), put_call_ratio=0.5 + (i % 9) / 10,
        ))
    return out


def _plant_same_day_model(n: int = 77) -> str:
    """在生产落点放一份「别人当天写的」合法模型：n≥60、oos 有值 ⇒ 过得了 PoisonedModelError，
    样本量 77 可辨认。08-29 / 09-04 进生产的那两份（n=200、oos 42.0 / 56.0）就是这个形状。"""
    from hive_logger import PATHS
    from ml_predictor import HGBModel
    m = HGBModel()
    m.train(_rows(n))
    assert m.oos_accuracy is not None, "夹具前提：要能过 PoisonedModelError（oos 有值）"
    path = Path(PATHS.ml_model_cache)
    m.save_model(str(path))
    assert path.exists()
    return hashlib.sha256(pickle_bytes(m)).hexdigest()[:16]


def pickle_bytes(model) -> bytes:
    import pickle
    return pickle.dumps(model._clf)


class TestNoDiskReadBack:

    def test_same_day_file_written_by_someone_else_is_not_used(self, gen_cls):
        """核心：当天磁盘上已有一份别人写的模型，生成器也必须自己训。旧代码在这里读回它 ⇒ 红。"""
        planted_sha = _plant_same_day_model(77)
        gen = gen_cls()
        assert gen.ml_service.model._n_samples_seen != 77, (
            "生成器用了磁盘上别人写的当日模型（n=77）——「文件日期 = 今天就照用」被接回来了，"
            "这正是 08-28 / 09-04 两天概率逐位相同的机制")
        assert gen._model_fingerprint["sha256"] != planted_sha

    def test_own_model_overwrites_forensic_copy(self, gen_cls):
        """读不回，但照写：落盘的取证副本 = 本进程打分用的那个模型（指纹可对上快照字节）。"""
        from hive_logger import PATHS
        _plant_same_day_model(77)
        gen = gen_cls()
        saved = json.loads(Path(PATHS.ml_model_cache).read_text(encoding="utf-8"))
        sha = hashlib.sha256(base64.b64decode(saved["model_bytes"])).hexdigest()[:16]
        assert sha == gen._model_fingerprint["sha256"], "取证副本不是本进程打分用的那个模型"
        assert saved["n_samples_seen"] == gen.ml_service.model._n_samples_seen

    def test_one_training_per_process(self, gen_cls, monkeypatch):
        """进程内缓存仍在：同一进程两个实例只训一次、共用同一个模型对象与指纹。"""
        from ml_predictor import MLPredictionService
        calls = []
        orig = MLPredictionService.train_model

        def counting(self):
            calls.append(1)
            return orig(self)
        monkeypatch.setattr(MLPredictionService, "train_model", counting)
        a, b = gen_cls(), gen_cls()
        assert len(calls) == 1
        assert a.ml_service.model is b.ml_service.model
        assert a._model_fingerprint == b._model_fingerprint

    def test_training_source_known_for_every_instance(self, gen_cls):
        """旧实现只给第一个实例设 `_training_data_source`，缓存命中的实例恒为 "unknown"（08-28 / 09-04 的记录全是它）。"""
        a, b = gen_cls(), gen_cls()
        assert a._training_data_source in ("real", "sample")
        assert b._training_data_source == a._training_data_source


class TestFingerprintRecorded:

    def test_report_carries_model_fingerprint(self, gen_cls):
        gen = gen_cls()
        gen.analyzer.generate_comprehensive_analysis = lambda ticker, metrics, **kw: {}
        dims = {"signal": 6.0, "catalyst": 5.5, "sentiment": 6.5, "odds": 5.0, "risk_adj": 5.0}
        report = gen.generate_ml_enhanced_report("NVDA", {}, swarm_direction="bullish",
                                                 swarm_dimension_scores=dims)
        recorded = report["ml_prediction"]["model"]
        assert recorded and recorded["sha256"] == gen._model_fingerprint["sha256"]
        assert recorded["n_samples_seen"] == gen.ml_service.model._n_samples_seen
        assert recorded["training_data_source"] in ("real", "sample")

    def test_fingerprint_failure_is_recorded_not_hidden(self):
        """算不出来 ⇒ sha256=None + 写明原因；不能让「没记」看起来像「没有」，也不能拖垮报告。"""
        from generate_ml_report import model_fingerprint

        class Unpicklable:
            _fitted = True
            _clf = staticmethod(lambda: None)   # lambda 不可 pickle
        fp = model_fingerprint(Unpicklable())
        assert fp["sha256"] is None and "fingerprint_error" in fp

    def test_fingerprint_is_deterministic_across_trainings(self):
        """同样的数据训两次 ⇒ 同一指纹（删掉读回的前提：训练是确定性的）。"""
        from generate_ml_report import model_fingerprint
        from ml_predictor import HGBModel
        a, b = HGBModel(), HGBModel()
        a.train(_rows(77))
        b.train(_rows(77))
        assert model_fingerprint(a)["sha256"] == model_fingerprint(b)["sha256"]


class TestGuardReadsFingerprint:

    @staticmethod
    def _write(d: Path, ticker: str, prob: float, sha):
        mlp = {"prediction": {"probability": prob}, "input": {"x": prob}}
        if sha is not None:
            mlp["model"] = {"sha256": sha}
        (d / f"analysis-{ticker}-ml-2026-10-20.json").write_text(
            json.dumps({"ml_prediction": mlp, "swarm_results": {"a": 1}}), encoding="utf-8")

    def test_fingerprints_counted_and_described(self, tmp_path):
        import ml_model_guard as g
        for i, t in enumerate(["AAA", "BBB", "CCC"]):
            self._write(tmp_path, t, 0.4 + i / 10, "aaaa1111")
        self._write(tmp_path, "DDD", 0.71, "bbbb2222")
        self._write(tmp_path, "EEE", 0.72, None)
        v = g.check_day(tmp_path, "2026-10-20")
        assert v.model_fingerprint_counts == {"aaaa1111": 3, "bbbb2222": 1}
        assert v.n_without_fingerprint == 1
        text = v.describe()
        assert "aaaa1111×3" in text and "混用了 2 个模型" in text and "无模型指纹的报告 1/5" in text

    def test_fingerprint_does_not_change_verdict(self, tmp_path):
        """指纹只进描述：同样的概率，有没有指纹判定一样（混用模型不等于退化）。"""
        import ml_model_guard as g
        probs = [0.31, 0.42, 0.53, 0.64, 0.75, 0.36, 0.47, 0.58, 0.69]
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        for i, p in enumerate(probs):
            self._write(a, f"T{i}", p, f"sha{i % 2}")
            self._write(b, f"T{i}", p, None)
        assert g.check_day(a, "2026-10-20").verdict == g.check_day(b, "2026-10-20").verdict


# ───────────────────────────── 静态守卫：生产代码不许读回模型文件

def _own_prod_files():
    files, _how = _repo_files.own_python_files(REPO_ROOT)
    return [p for p in files
            if p.relative_to(REPO_ROOT).parts[0] not in ("tests", "experiments")]


def _scoped(tree):
    """[(限定名, 节点)]：每个节点归到包着它的最内层函数（类方法记 `类.方法`）。"""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, f"{prefix}.{child.name}" if prefix else child.name)
            else:
                out.append((prefix or "<module>", child))
                walk(child, prefix)
    walk(tree, "")
    return out


def _load_model_calls(tree):
    return [n.lineno for _q, n in _scoped(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "load_model"]


def _attr_sites(tree, attr: str):
    return sorted({q for q, n in _scoped(tree) if isinstance(n, ast.Attribute) and n.attr == attr})


class TestNoReadBackStatic:

    def test_no_production_load_model_calls(self):
        """生产代码零个 `.load_model(` 调用：模型只在进程内训出来，不从任何文件读回。
        要恢复读回，先回答「谁写的那份文件、凭什么信它」——见本文件 docstring。"""
        hits = {}
        for p in _own_prod_files():
            calls = _load_model_calls(_ast_cache.parse_file(p))
            if calls:
                hits[str(p.relative_to(REPO_ROOT))] = calls
        assert not hits, f"生产代码又在读回模型文件：{hits}"

    def test_forensic_path_only_reached_by_the_writer(self):
        """`PATHS.ml_model_cache` 只经 `_model_file`，`_model_file` 只被 `_save_model_to_disk` 用。"""
        tree = _ast_cache.parse_file(REPO_ROOT / "generate_ml_report.py")
        assert _attr_sites(tree, "ml_model_cache") == ["MLEnhancedReportGenerator._model_file"]
        assert _attr_sites(tree, "_model_file") == ["MLEnhancedReportGenerator._save_model_to_disk"]
        for p in _own_prod_files():
            if p.name in ("generate_ml_report.py", "hive_logger.py"):
                continue
            assert not _attr_sites(_ast_cache.parse_file(p), "ml_model_cache"), f"{p.name} 引用了 PATHS.ml_model_cache"

    def test_detectors_have_teeth(self):
        """正对照：同一探测法认得出旧写法——否则上面的「没有」可能只是探测器瞎了。"""
        old = ast.parse(
            "class G:\n"
            "    def _load_model_from_disk(self):\n"
            "        self.ml_service.model.load_model(str(self._model_file))\n"
            "    @property\n"
            "    def _model_file(self):\n"
            "        return PATHS.ml_model_cache\n")
        assert _load_model_calls(old) == [3]
        assert _attr_sites(old, "_model_file") == ["G._load_model_from_disk"]
        assert _attr_sites(old, "ml_model_cache") == ["G._model_file"]
        assert len(_own_prod_files()) > 50, "生产文件清单不该这么少——枚举口径坏了"
