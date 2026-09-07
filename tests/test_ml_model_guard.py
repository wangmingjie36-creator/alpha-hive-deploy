"""ML 模型常数退化闸 + 模型版本快照（v0.45.145）

治的缺陷：模型退化成常数函数时全线无告警——2026-09-04 全部 12 份
`probability` 逐位相同 = `0.5899693787928219`，报告照常印「ML 预测 59.0%」，
退出码 0。

## 为什么每条判定都是**成对**的

只写「喂常数要红」的话，一个 `return True` 的恒真闸门也全绿。
沿用 `tests/test_distribution_invariants.py::TestGuardsHaveTeeth` 的做法：
每个判据都配一条「正常数据必须绿」。

夹具用的是**真实生产取值**（09-04 / 08-28 / 09-02 三天，逐位抄自
`analysis-*-ml-*.json`），不是编的数。
"""

import ast
import json
from pathlib import Path

import pytest

import ml_model_guard as G

REPO = Path(__file__).resolve().parent.parent

# ── 真实生产取值（逐位抄自 analysis-*-ml-*.json）────────────────────
#: 2026-09-04：12 份全部逐位相同 ⇒ 常数函数
PROD_0904_CONSTANT = [0.5899693787928219] * 12

#: 2026-08-28：12 份恒为一个值，另 2 只因 iv_rank/put_call_ratio 为 None
#: 走别的路径 ⇒ distinct=2。**字面 `unique == 1` 抓不到这一天。**
PROD_0828_NEAR_CONSTANT = [0.14901620144018954] * 12 + [0.515218499184382] * 2

#: 2026-09-02：健康日。注意它含一个**合法的三连并列**
#: （0.4805419596430596 × 3，输入各不相同、树模型叶子离散化所致）
#: ⇒ 判据必须取 distinct 而非「有没有并列」。
PROD_0902_HEALTHY = [
    0.43848360875168785, 0.4442265463835876, 0.467320373033367,
    0.4559559275276845, 0.4805419596430596, 0.4805419596430596,
    0.4805419596430596, 0.4498424630427196, 0.4498424630427196,
    0.5191431149294384, 0.4882484892443186, 0.46681781426977026,
]

#: 2026-09-03：健康日，10 个不同值
PROD_0903_HEALTHY = [
    0.49713035571590103, 0.519311622888064, 0.519311622888064,
    0.4645072961962098, 0.4020957068619353, 0.4020957068619353,
    0.3867550568904422, 0.5276722673731774, 0.5218616101352599,
    0.4549251072265234, 0.4045545327712574, 0.5188049045395631,
]


# ===================================================================
# 1. 纯判定谓词 —— 每条都成对
# ===================================================================
class TestPredicateHasTeeth:
    """判据抽成了纯函数，每个形状「退化必红 + 正常必绿」成对断言。"""

    def test_constant_batch_is_flagged(self):
        v = G.evaluate_probabilities(PROD_0904_CONSTANT, date="2026-09-04")
        assert v.verdict == "constant"
        assert v.is_degenerate
        assert v.exit_code == 1
        assert v.distinct == 1

    def test_healthy_batch_is_not_flagged(self):
        """成对的另一半：正常多值必须绿。缺这条，恒真闸门也全绿。"""
        v = G.evaluate_probabilities(PROD_0903_HEALTHY, date="2026-09-03")
        assert v.verdict == "ok"
        assert not v.is_degenerate
        assert v.exit_code == 0
        assert v.distinct == 10

    def test_near_constant_batch_is_flagged(self):
        """08-28 的形状：distinct=2。

        ⚠️ 这一条是**字面 `unique == 1` 会漏掉**的那半边。只写第一条判据
        就是重犯「修一支漏一支」——两次已知事故只会抓到一次。
        """
        v = G.evaluate_probabilities(PROD_0828_NEAR_CONSTANT, date="2026-08-28")
        assert v.verdict == "near_constant"
        assert v.is_degenerate
        assert v.exit_code == 1
        assert v.distinct == 2
        assert v.modal_count == 12

    def test_legitimate_ties_are_not_flagged(self):
        """成对的另一半：合法并列必须绿。

        09-02 有三只共享 0.4805419596430596，但当天 12 份输入各不相同
        （树模型叶子离散化）。若判据取「有没有并列」而非 distinct，
        这一天会误报。
        """
        v = G.evaluate_probabilities(PROD_0902_HEALTHY, date="2026-09-02")
        assert v.verdict == "ok"
        assert v.modal_count == 3, "夹具本身应含一个三连并列，否则这条测试没在测东西"
        assert v.distinct == 9

    def test_single_ticker_is_undetermined_not_healthy(self):
        """n<2 判不了。**必须报 undetermined（3），绝不能报健康（0）**。

        补跑日常有 n=1（生产 77 天里 7 天如此）。把「判不了」写成「健康」
        就是本项目最常见的那个形状：把失败改写成没发生过。
        """
        v = G.evaluate_probabilities([0.5899693787928219], date="2026-08-11")
        assert v.verdict == "undetermined"
        assert not v.is_degenerate
        assert v.exit_code == 3

    def test_two_identical_is_constant_even_though_n_is_small(self):
        """第 1 条判据不设 n 地板：全体逐位相同在任何 n>1 下都不是巧合。"""
        v = G.evaluate_probabilities([0.42, 0.42])
        assert v.verdict == "constant"

    def test_small_n_two_values_is_not_near_constant(self):
        """成对的另一半：第 2 条判据的 n>=8 地板必须真的挡住小样本。

        n=3 且 distinct=2 在生产上是合法的（并列确实会发生），
        没有地板就会天天误报补跑日。
        """
        v = G.evaluate_probabilities([0.41, 0.41, 0.55])
        assert v.n_numeric == 3
        assert v.distinct == 2
        assert v.verdict == "ok", "n=3 不该被 near_constant 判据碰到"

    def test_bool_is_not_treated_as_probability(self):
        """`bool` 是 `int` 的子类，`float(True)` = 1.0 会被当成合法概率。

        仓库同类守卫 5 处全写了 `not isinstance(x, bool)`，这里照抄那一句。
        若不排除，`[True]*11 + [0.5]` 会被读成 distinct=2 的「准常数」，
        或反过来把真退化稀释掉。
        """
        v = G.evaluate_probabilities([True, False] + list(PROD_0903_HEALTHY))
        assert v.n_numeric == len(PROD_0903_HEALTHY), "bool 必须被剔除，不得计入"
        assert v.verdict == "ok"

    def test_nan_does_not_mask_degeneracy(self):
        """NaN != NaN 会把 distinct 撑大 ⇒ 真常数被稀释成「看着有两个值」。

        本项目 NaN 穿透守卫已犯过三次（v0.45.93/97/110）。
        """
        nan = float("nan")
        v = G.evaluate_probabilities([nan] + PROD_0904_CONSTANT)
        assert v.distinct == 1, "NaN 不得计入 distinct"
        assert v.verdict == "constant"

    def test_nan_only_batch_is_undetermined(self):
        """成对：全 NaN 时没有可判的数 ⇒ undetermined，不是「常数」。"""
        nan = float("nan")
        v = G.evaluate_probabilities([nan, nan, nan])
        assert v.verdict == "undetermined"
        assert v.n_numeric == 0


# ===================================================================
# 2. 磁盘读取 —— 成对
# ===================================================================
def _write_day(tmp_path, date, probs, *, broken=None, no_prob=()):
    for i, p in enumerate(probs):
        payload = {"ml_prediction": {"prediction": {"probability": p}}}
        (tmp_path / f"analysis-T{i:02d}-ml-{date}.json").write_text(
            json.dumps(payload), encoding="utf-8")
    for t in no_prob:
        (tmp_path / f"analysis-{t}-ml-{date}.json").write_text(
            json.dumps({"ml_prediction": {"prediction": {}}}), encoding="utf-8")
    if broken:
        (tmp_path / f"analysis-{broken}-ml-{date}.json").write_text(
            "{not json", encoding="utf-8")


class TestDiskGate:
    def test_degenerate_day_on_disk_is_flagged(self, tmp_path):
        _write_day(tmp_path, "2026-09-04", PROD_0904_CONSTANT)
        v = G.check_day(tmp_path, "2026-09-04")
        assert v.verdict == "constant"
        assert v.n_files == 12 and v.n_numeric == 12

    def test_healthy_day_on_disk_is_green(self, tmp_path):
        _write_day(tmp_path, "2026-09-03", PROD_0903_HEALTHY)
        v = G.check_day(tmp_path, "2026-09-03")
        assert v.verdict == "ok"
        assert v.exit_code == 0

    def test_other_days_are_not_mixed_in(self, tmp_path):
        """按日期切分：昨天的健康数据不得把今天的退化稀释掉。"""
        _write_day(tmp_path, "2026-09-03", PROD_0903_HEALTHY)
        _write_day(tmp_path, "2026-09-04", PROD_0904_CONSTANT)
        assert G.check_day(tmp_path, "2026-09-04").verdict == "constant"
        assert G.check_day(tmp_path, "2026-09-03").verdict == "ok"

    def test_missing_and_broken_are_reported_not_silently_dropped(self, tmp_path):
        """v0.45.114：跳过缺失项＝把缺失渲染成不存在。

        `n_files` 与 `n_numeric` 都要留着，且坏文件要单独点名——
        否则「12 份里 10 份没有 probability」会长得像「只有 2 份报告」。
        """
        _write_day(tmp_path, "2026-09-04", PROD_0903_HEALTHY,
                   broken="BAD", no_prob=("NOPE",))
        v = G.check_day(tmp_path, "2026-09-04")
        assert v.n_files == 14, "坏文件与无概率文件都必须计入 n_files"
        assert v.n_numeric == 12
        names = " ".join(v.tickers_without_probability)
        assert "NOPE" in names and "BAD" in names
        assert "BAD" in v.describe()

    def test_empty_day_is_undetermined(self, tmp_path):
        v = G.check_day(tmp_path, "2026-09-04")
        assert v.verdict == "undetermined"
        assert v.exit_code == 3


class TestEnforceDay:
    def test_raises_when_degenerate(self, tmp_path):
        _write_day(tmp_path, "2026-09-04", PROD_0904_CONSTANT)
        with pytest.raises(G.MLModelDegenerateError):
            G.enforce_day(tmp_path, "2026-09-04")

    def test_does_not_raise_when_healthy(self, tmp_path):
        """成对：健康日不得抛，否则这个闸每天都在拦正常运行。"""
        _write_day(tmp_path, "2026-09-03", PROD_0903_HEALTHY)
        v = G.enforce_day(tmp_path, "2026-09-03")
        assert v.verdict == "ok"

    def test_non_raising_mode_still_reports_degenerate(self, tmp_path):
        """Step 2 主路径用的模式：不抛，但判决必须原样返回。

        ⚠️ `_generate_ml_reports` 的调用点只兜
        (OSError, ValueError, KeyError, TypeError)，抛 RuntimeError 会穿透并
        连带杀掉 index.html 与 gh-pages 部署（v0.43.17 同款）。
        """
        _write_day(tmp_path, "2026-09-04", PROD_0904_CONSTANT)
        v = G.enforce_day(tmp_path, "2026-09-04", raise_on_degenerate=False)
        assert v.is_degenerate and v.exit_code == 1

    def test_error_is_logged_when_degenerate(self, tmp_path, caplog):
        """「谁会红」的答案之一：ERROR 级日志。降级到 info/debug 就是没人红。"""
        _write_day(tmp_path, "2026-09-04", PROD_0904_CONSTANT)
        with caplog.at_level("INFO"):
            G.enforce_day(tmp_path, "2026-09-04", raise_on_degenerate=False)
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "退化必须落 ERROR"

    def test_no_error_logged_when_healthy(self, tmp_path, caplog):
        """成对：健康日不得报 ERROR，否则告警会被习惯性忽略。"""
        _write_day(tmp_path, "2026-09-03", PROD_0903_HEALTHY)
        with caplog.at_level("INFO"):
            G.enforce_day(tmp_path, "2026-09-03")
        assert not [r for r in caplog.records if r.levelname == "ERROR"]


# ===================================================================
# 3. 模型版本快照
# ===================================================================
@pytest.fixture
def snapshots_enabled(monkeypatch):
    """显式开启快照，让本组测试真的走生产那条路径。

    conftest 与 `_snapshot_disabled` 两层默认在 pytest 里关掉它
    ——测试夹具模型混进 `ml_model_history/` 会被自动提交成「生产模型」。
    """
    monkeypatch.setenv(G.SNAPSHOT_DISABLE_ENV, "0")


class TestSnapshotIsolation:
    """成对：pytest 下默认不写 + 显式开启才写。

    只写后半边的话，隔离层被删掉也全绿——而那意味着夹具模型进 git。
    """

    def test_disabled_under_pytest_even_without_the_env_var(self, tmp_path, monkeypatch):
        """**先 delenv**，否则测到的是 conftest 那一层、不是这一层。

        `_snapshot_disabled` 有两支：显式环境变量优先，没设时看
        `PYTEST_CURRENT_TEST`。conftest 会把变量设成 "1"，于是第二支
        **永远不会被求值** —— 不 delenv 的话这条测试就是个从未证明会触发的
        守卫（BullVeto 同款）。删掉变量才把第二层单独暴露出来。
        """
        monkeypatch.delenv(G.SNAPSHOT_DISABLE_ENV, raising=False)
        assert "PYTEST_CURRENT_TEST" in __import__("os").environ, (
            "这条测试的前提是 pytest 会设 PYTEST_CURRENT_TEST；没设就说明前提没了"
        )
        src = tmp_path / "ml_model.json"
        src.write_text('{"model_type": "fixture"}', encoding="utf-8")
        assert G.snapshot_model_file(src, date_str="2026-09-07") is None
        assert not (tmp_path / G.HISTORY_DIRNAME).exists(), (
            "测试夹具模型不得进快照目录 —— 那是会被自动提交的假证据"
        )

    def test_env_var_layer_wins_when_set(self, tmp_path, monkeypatch):
        """成对：显式设了变量就听它（这是 conftest 那一层的机制）。"""
        monkeypatch.setenv(G.SNAPSHOT_DISABLE_ENV, "1")
        src = tmp_path / "ml_model.json"
        src.write_text('{"model_type": "fixture"}', encoding="utf-8")
        assert G.snapshot_model_file(src, date_str="2026-09-07") is None

    def test_enabled_when_explicitly_turned_on(self, tmp_path, snapshots_enabled):
        src = tmp_path / "ml_model.json"
        src.write_text('{"model_type": "fixture"}', encoding="utf-8")
        assert G.snapshot_model_file(src, date_str="2026-09-07") is not None

    def test_conftest_disables_it(self):
        """conftest 的那道 autouse 闸被改名/删掉时这条要红。

        隔离层有两道，这条盯着 conftest 那道；`_snapshot_disabled` 那道由
        `test_disabled_under_pytest_by_default` 盯着（它把环境变量当不存在处理）。
        """
        import os
        assert os.environ.get("ALPHA_HIVE_MODEL_SNAPSHOT_DISABLE") == "1"


@pytest.mark.usefixtures("snapshots_enabled")
class TestSnapshot:
    def _model(self, tmp_path, name="ml_model_cache.json", n=30):
        p = tmp_path / name
        p.write_text(json.dumps(
            {"model_type": "hgb", "is_trained": True, "n_samples_seen": n,
             "training_accuracy": 96.7, "oos_accuracy": None}), encoding="utf-8")
        return p

    def test_snapshot_is_written_with_date_and_manifest(self, tmp_path):
        src = self._model(tmp_path)
        dest = G.snapshot_model_file(src, date_str="2026-09-07")
        assert dest is not None and dest.name == "ml_model_cache-2026-09-07.json"
        assert dest.read_bytes() == src.read_bytes()
        lines = (tmp_path / G.HISTORY_DIRNAME / G.MANIFEST_NAME).read_text().strip().split("\n")
        rec = json.loads(lines[-1])
        assert rec["source"] == "ml_model_cache.json"
        assert rec["n_samples_seen"] == 30 and rec["model_type"] == "hgb"
        assert len(rec["sha256"]) == 64

    def test_same_day_identical_content_is_not_rewritten(self, tmp_path):
        src = self._model(tmp_path)
        assert G.snapshot_model_file(src, date_str="2026-09-07") is not None
        assert G.snapshot_model_file(src, date_str="2026-09-07") is None
        # 但 manifest 仍记了两次 —— 日内是否换过模型看 manifest
        lines = (tmp_path / G.HISTORY_DIRNAME / G.MANIFEST_NAME).read_text().strip().split("\n")
        assert len(lines) == 2

    def test_same_day_changed_content_overwrites_and_manifest_shows_both(self, tmp_path):
        src = self._model(tmp_path, n=30)
        G.snapshot_model_file(src, date_str="2026-09-07")
        src.write_text(json.dumps({"model_type": "hgb", "n_samples_seen": 45}),
                       encoding="utf-8")
        dest = G.snapshot_model_file(src, date_str="2026-09-07")
        assert dest is not None
        assert json.loads(dest.read_text())["n_samples_seen"] == 45
        hashes = {json.loads(l)["sha256"] for l in
                  (tmp_path / G.HISTORY_DIRNAME / G.MANIFEST_NAME).read_text().strip().split("\n")}
        assert len(hashes) == 2, "日内换过模型必须能从 manifest 的 sha256 看出来"

    def test_retention_prunes_oldest_only(self, tmp_path):
        src = self._model(tmp_path)
        for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
            src.write_text(json.dumps({"d": d}), encoding="utf-8")
            G.snapshot_model_file(src, date_str=d, retain=2)
        hist = tmp_path / G.HISTORY_DIRNAME
        kept = sorted(p.name for p in hist.glob("ml_model_cache-*.json"))
        assert kept == ["ml_model_cache-2026-09-02.json", "ml_model_cache-2026-09-03.json"]

    def test_retention_never_touches_foreign_files(self, tmp_path):
        """成对的另一半：裁剪只能删自己那套命名，别人的文件一个不许动。"""
        src = self._model(tmp_path)
        hist = tmp_path / G.HISTORY_DIRNAME
        hist.mkdir(exist_ok=True)
        (hist / "README.md").write_text("keep me", encoding="utf-8")
        (hist / "ml_model_cache-notadate.json").write_text("{}", encoding="utf-8")
        (hist / "other_model-2026-01-01.json").write_text("{}", encoding="utf-8")
        for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
            src.write_text(json.dumps({"d": d}), encoding="utf-8")
            G.snapshot_model_file(src, date_str=d, retain=1)
        assert (hist / "README.md").exists()
        assert (hist / "ml_model_cache-notadate.json").exists()
        assert (hist / "other_model-2026-01-01.json").exists()

    def test_missing_source_returns_none_without_raising(self, tmp_path):
        assert G.snapshot_model_file(tmp_path / "nope.json") is None

    def test_verdict_reports_snapshot_presence(self, tmp_path):
        """快照在不在，要出现在每日那条日志里——这是快照失败的观测点。"""
        _write_day(tmp_path, "2026-09-07", PROD_0903_HEALTHY)
        assert G.check_day(tmp_path, "2026-09-07").snapshot_present is False
        G.snapshot_model_file(self._model(tmp_path), date_str="2026-09-07")
        v = G.check_day(tmp_path, "2026-09-07")
        assert v.snapshot_present is True
        assert "已留存" in v.describe()


# ===================================================================
# 4. 接线（AST）—— 测被调函数 ≠ 测接线（v0.45.126 教训）
# ===================================================================
def _fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _called_names(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


class TestSnapshotWiring:
    """每个 `def save_model` 都必须调 `_snapshot_saved_model`。

    判据取 **AST 不取子串**：子串守卫会被解释它的注释自己触发
    （v0.45.135 教训）。新增第 5 个 save_model 忘了钩 → 这条变红。
    """

    @pytest.mark.parametrize("mod", ["ml_predictor.py", "ml_predictor_extended.py"])
    def test_every_save_model_snapshots(self, mod):
        tree = ast.parse((REPO / mod).read_text(encoding="utf-8"))
        found = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "save_model"]
        assert found, f"{mod} 里找不到 save_model —— 守卫失去了对象，先查是不是改名了"
        for node in found:
            assert "_snapshot_saved_model" in _called_names(node), (
                f"{mod}:{node.lineno} 的 save_model 没有留版本快照。"
                "模型原地覆盖 = 下次退化无法事后归因（2026-09-04 就是这么丢的）"
            )


class TestGateWiring:
    """闸装在了生产**批量收尾点**上，而不是一个没人调的死函数里。"""

    def test_daily_report_step2_calls_the_gate(self):
        """`_generate_ml_reports` 是当日 analysis JSON 的主力生产者。"""
        tree = ast.parse((REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        node = _fn(tree, "_generate_ml_reports")
        assert node is not None
        assert "_enforce_day" in _called_names(node), (
            "Step 2 主路径没接闸。只接 generate_ml_report.main() 等于只堵了 Step 3 补跑"
        )

    def test_daily_report_gate_must_not_raise(self):
        """必须显式 raise_on_degenerate=False。

        改成抛 → RuntimeError 穿透 `_save_output_files` 只兜四种异常的 except
        → 连带杀掉 index.html 生成与 gh-pages 部署（v0.43.17 同款事故）。
        """
        tree = ast.parse((REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        node = _fn(tree, "_generate_ml_reports")
        calls = [n for n in ast.walk(node)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_enforce_day"]
        assert calls, "找不到 _enforce_day 调用"
        for c in calls:
            kw = {k.arg: k.value for k in c.keywords}
            assert "raise_on_degenerate" in kw, "必须显式传，别靠默认值"
            assert isinstance(kw["raise_on_degenerate"], ast.Constant)
            assert kw["raise_on_degenerate"].value is False

    def test_daily_report_verdict_reaches_the_report_dict(self):
        """判决要挂到 `report` 上——那正是 main() 返回、__main__ 据以出码的对象。

        ⚠️ 成功路径与 ImportError 兜底路径**各断一次**。
        只断「这个键在函数里出现过」是不够的：兜底分支里也有一份，
        摘掉成功路径的赋值照样全绿（mutation M13 实测漏网后补的这条）。
        与 v0.45.114「改二分支只盯显眼的那一支」同源。
        """
        tree = ast.parse((REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        node = _fn(tree, "_generate_ml_reports")
        tries = [n for n in ast.walk(node)
                 if isinstance(n, ast.Try) and "_enforce_day" in _called_names(n)]
        assert len(tries) == 1, "找不到唯一那个装闸的 try 块"
        t = tries[0]

        def assigns_guard(stmts):
            for stmt in stmts:
                for n in ast.walk(stmt):
                    if not isinstance(n, ast.Assign):
                        continue
                    for tgt in n.targets:
                        if (isinstance(tgt, ast.Subscript)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "report"
                                and isinstance(tgt.slice, ast.Constant)
                                and tgt.slice.value == "ml_model_guard"):
                            return True
            return False

        assert assigns_guard(t.body), "成功路径没把判决挂到 report 上，__main__ 收不到"
        assert any(assigns_guard(h.body) for h in t.handlers), (
            "ImportError 兜底路径没挂判决 —— 「闸装不上」会被读成健康"
        )

    def test_daily_report_main_exits_nonzero(self):
        """__main__ 里必须有「退化 → 非零退出码」这条路径。

        编排器只看退出码；只写日志＝没人红（v0.43.23 记过同一件事）。
        """
        src = (REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8")
        tail = src[src.rindex('if __name__ == "__main__":'):]
        tree = ast.parse(tail.replace('if __name__ == "__main__":', "if True:", 1))
        consts = {n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "constant" in consts and "near_constant" in consts, (
            "__main__ 没有按判决出码"
        )
        assert "exit" in _called_names(tree)

    def test_force_exit_carries_the_code(self):
        """强退路径原本硬写 `os._exit(0)`：不带上退出码，
        「线程卡死」会把「ML 退化」悄悄改写成成功——正是本版要治的形状。"""
        tree = ast.parse((REPO / "alpha_hive_daily_report.py").read_text(encoding="utf-8"))
        node = _fn(tree, "_force_exit_if_threads_stuck")
        assert node is not None
        assert any(a.arg == "exit_code" for a in node.args.args + node.args.kwonlyargs)
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "_exit":
                assert n.args and isinstance(n.args[0], ast.Name), \
                    "os._exit 不得再硬写字面量"

    def test_generate_ml_report_main_calls_gate_and_exits(self):
        tree = ast.parse((REPO / "generate_ml_report.py").read_text(encoding="utf-8"))
        node = _fn(tree, "main")
        assert node is not None
        names = _called_names(node)
        assert "_enforce_day" in names, "Step 3 补跑路径没接闸"
        assert "exit" in names, "闸响了必须出非零退出码，只写日志＝没人红"


# ===================================================================
# 5. 新产物目录的「三件事」（v0.45.111 教训）
# ===================================================================
class TestArtifactPlumbing:
    def test_history_dir_in_both_whitelists(self):
        import report_deployer as rd
        assert f"{G.HISTORY_DIRNAME}/" in rd.REPORT_ARTIFACT_PATHS
        assert f"{G.HISTORY_DIRNAME}/" in rd._ARTIFACT_PREFIXES
        assert rd._is_report_artifact(f"{G.HISTORY_DIRNAME}/ml_model-2026-09-07.json")

    def test_gitignore_negation_after_the_broad_rule(self):
        """`.gitignore` 的 `ml_model*.json` 会连快照一起吞掉（无斜杠模式匹配
        任意层级的 basename）。反向规则必须存在，且**必须在那条之后**
        ——gitignore 是后者胜，顺序反了等于没写。
        """
        lines = [l.strip() for l in (REPO / ".gitignore").read_text(
            encoding="utf-8").splitlines()]
        assert "ml_model*.json" in lines
        neg = f"!{G.HISTORY_DIRNAME}/*.json"
        assert neg in lines, f".gitignore 缺 {neg}，快照会每天写、每天不进库"
        assert lines.index(neg) > lines.index("ml_model*.json")

    def test_history_dir_is_tracked(self):
        """目录本身要有个被跟踪的文件，否则空目录不进 git。"""
        assert (REPO / G.HISTORY_DIRNAME / "README.md").is_file()


# ===================================================================
# 6. CLI 退出码（沿用 Step 10/11/12 约定）
# ===================================================================
class TestCLI:
    def test_exit_1_on_degenerate(self, tmp_path, capsys):
        _write_day(tmp_path, "2026-09-04", PROD_0904_CONSTANT)
        assert G.main(["--date", "2026-09-04", "--dir", str(tmp_path)]) == 1

    def test_exit_0_on_healthy(self, tmp_path, capsys):
        _write_day(tmp_path, "2026-09-03", PROD_0903_HEALTHY)
        assert G.main(["--date", "2026-09-03", "--dir", str(tmp_path)]) == 0

    def test_exit_3_when_undetermined(self, tmp_path, capsys):
        """2 被编排器 run_step 占用（脚本不存在），本模块不得使用。"""
        assert G.main(["--date", "2026-09-04", "--dir", str(tmp_path)]) == 3
