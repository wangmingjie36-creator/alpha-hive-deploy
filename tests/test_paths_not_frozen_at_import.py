"""守卫：`hive_logger.PATHS.*` 派生的路径不得在 import 期冻成常量（v0.45.150）。

物种描述
--------
`PATHS` 的每个成员都是 property，读一次查一次环境变量——设计本身没问题。
出问题的是**消费方**：把它求值成模块级常量或类属性时，值就冻在 import 那一刻。

`tests/conftest.py::_isolate_env` 是 autouse 的 function-scope fixture，
用 `monkeypatch.setenv("ALPHA_HIVE_HOME", tmp_path)` 做隔离。但 pytest 在
**收集期**就 import 各 test 模块，连带 import 被测生产模块，而那时 fixture
还一次都没跑过 ⇒ 冻住的值 = checkout 根目录 = **生产目录**，此后整个 session
不再变化，隔离对它完全无效。

实证（v0.45.150，收集期探针）：`ALPHA_HIVE_HOME` 在收集结束时确为 `<UNSET>`，
且 14 个候选模块中 10 个已进 `sys.modules`。跑一次全套测试后，checkout 根目录
新增 `pheromone.db` / `ml_model.json` / `ml_model_cache.json` 等 7 个产物。
拿生产 `pheromone.db`（37 MB）的副本实测：内容逐字节未变，但 **mtime 变了、
并留下 `-wal`/`-shm`** ——写通道是全开的，只是那一轮恰好没 INSERT。

⚠️ 本文件的 import 必须留在**模块级**。挪进测试函数体内就会在 `_isolate_env`
   之后才 import，于是即便代码是坏的也照样全绿——测试自己失去判别力。
"""

import os
from pathlib import Path

import pytest

# —— 模块级 import：刻意复现「收集期就被 import」这一条件 ——
import backtester
import memory_store
import vector_memory
from hive_logger import PATHS


REPO_ROOT = Path(__file__).resolve().parent.parent


class TestResolvedAtCallTime:
    """① 改 `ALPHA_HIVE_HOME` / `ALPHA_HIVE_DB_PATH` 后，路径必须跟着变。

    这一组在「冻住」的实现下必红——这正是它存在的理由。
    """

    def test_isolate_env_actually_took_effect(self, tmp_path):
        """元守卫：先证明夹具本身有判别力，否则下面几条证明不了任何事。"""
        assert os.environ.get("ALPHA_HIVE_HOME") == str(tmp_path), (
            "_isolate_env 没生效，本文件其余断言全部作废")
        assert str(tmp_path) in PATHS.db

    def test_backtester_default_db_path_follows_env(self, tmp_path):
        got = backtester.default_db_path()
        assert str(tmp_path) in got, (
            f"backtester 的默认库路径没跟着环境变量走：{got}\n"
            "说明它又被求值成模块级常量了（冻在 import 期），"
            "跑测试会打开**生产** pheromone.db。")

    def test_prediction_store_no_arg_is_isolated(self, tmp_path):
        """无参构造是生产里的真实调用形状（generate_ml_report / swarm_agents.base）。"""
        store = backtester.PredictionStore()
        assert str(tmp_path) in store.db_path, (
            f"PredictionStore() 连到了 {store.db_path}。"
            "默认参数在 def 执行时求值＝import 期，写 `db_path=DB_PATH` 等于换个地方冻。")

    def test_backtester_no_arg_is_isolated(self, tmp_path):
        assert str(tmp_path) in backtester.Backtester().store.db_path

    def test_memory_store_no_arg_is_isolated(self, tmp_path):
        """`alpha_hive_daily_report.py` 就是无参 `MemoryStore()`。"""
        store = memory_store.MemoryStore()
        assert str(tmp_path) in store.db_path, (
            f"MemoryStore() 连到了 {store.db_path}，并会在其上跑 schema_migrate 建表")

    def test_vector_memory_no_arg_is_isolated(self, tmp_path):
        vm = vector_memory.VectorMemory()
        assert str(tmp_path) in str(vm.db_path), (
            f"VectorMemory() 指向 {vm.db_path}，Chroma 会在那里持久化 collection")


class TestExplicitPathStillWins:
    """② 成对的另一半：显式传入的路径必须照常生效。

    少了这一组，「把参数整个忽略、永远返回 PATHS.x」这种偷懒修法会全绿。
    """

    def test_prediction_store_honours_explicit(self, tmp_path):
        explicit = str(tmp_path / "explicit.db")
        assert backtester.PredictionStore(explicit).db_path == explicit

    def test_backtester_honours_explicit(self, tmp_path):
        explicit = str(tmp_path / "explicit_bt.db")
        assert backtester.Backtester(explicit).store.db_path == explicit

    def test_memory_store_honours_explicit(self, tmp_path):
        explicit = str(tmp_path / "explicit_ms.db")
        assert memory_store.MemoryStore(explicit).db_path == explicit

    def test_vector_memory_honours_explicit(self, tmp_path):
        explicit = str(tmp_path / "explicit_chroma")
        assert vector_memory.VectorMemory(explicit).db_path == explicit


class TestProductionArtifactsNotTouched:
    """③ 直接盯产物：无参构造前后，生产产物的指纹必须一模一样。

    ①是「路径对不对」，③是「盘上有没有被动过」。①绿而③红是可能的
    （比如某条绕过默认路径、显式传入冻结值的写入），所以两层都要。

    ⚠️ 判据是**指纹比对**，不是 `not exists()`。第一版就写成了后者，只因为
       开发用的 worktree 里恰好没有这些文件才全绿——而在**主 checkout** 上
       `pheromone.db`(37 MB) / `chroma_db/`(20 MB) / `metrics.db` 本来就该存在，
       那一版会对所有人恒红。「这些文件不该存在」是当时环境的偶然，不是不变式。
    """

    @pytest.mark.parametrize("artifact", ["pheromone.db", "chroma_db", "metrics.db"])
    def test_no_arg_construction_does_not_touch_production(self, artifact, tmp_path,
                                                            artifact_signature):
        target = REPO_ROOT / artifact
        before = artifact_signature(str(target))

        backtester.PredictionStore()
        memory_store.MemoryStore()
        vector_memory.VectorMemory()

        assert artifact_signature(str(target)) == before, (
            f"无参构造动了 checkout 根目录的 {artifact}（指纹变了）。"
            "在主 checkout 上这就是生产数据本身。"
            f"\n  before={before[:80]}")

    def test_signature_helper_has_teeth(self, tmp_path, artifact_signature):
        """反向自证：指纹函数必须真能分辨「碰过」和「没碰过」。

        它若对任何输入都返回同一个值，上面三条就是恒真的装饰品。
        """
        probe = tmp_path / "probe.db"
        assert artifact_signature(str(probe)) == "MISSING"
        probe.write_bytes(b"x")
        sig1 = artifact_signature(str(probe))
        assert sig1 != "MISSING"
        probe.write_bytes(b"xy")
        assert artifact_signature(str(probe)) != sig1, "指纹分不出内容变化"


class TestSpeciesDoesNotSpread:
    """④ 结构守卫：不许**新增**「PATHS 派生值求值成模块级常量/类属性」的点。

    判据取 **AST** 不取子串——子串守卫会被本文件自己的说明文字触发
    （v0.45.129 / v0.45.143 同款教训）。

    语义是**子集**不是相等：清单里的存量可以逐步减少（修好一处不会让本条变红），
    但清单外冒出新的一处必红。这条守的是「物种不要继续扩散」。
    """

    # 存量清单（v0.45.150 实测）。修好一处就从这里删掉一行。
    # 高危三处（backtester / memory_store / vector_memory）已在 v0.45.150 修掉，
    # 故不在此列——它们若复发，会被本文件 ① 组直接抓住。
    KNOWN = {
        # 归 v0.45.149（`ml_model_cache.json`，另一 session 在改）
        ("generate_ml_report.py", "_model_file"),
        # 中危：缓存目录，会往 checkout 根目录写缓存
        ("earnings_watcher.py", "CACHE_DIR"),
        ("sec_edgar.py", "CACHE_DIR"),
        ("polymarket_client.py", "CACHE_DIR"),
        ("newsapi_client.py", "_CACHE_DIR"),
        ("edgar_rss.py", "_CACHE_PATH"),
        ("pead_analyzer.py", "_CACHE_DIR"),
        # 中高危：这四个 BASE_DIR 派生出 vrp_state/ options_paper_state/ hedge_state/
        # ——是**账本**不是缓存（见 MEMORY.md v0.45.111）。
        ("vrp_signal.py", "BASE_DIR"),
        ("options_paper_leg.py", "BASE_DIR"),
        ("portfolio_greeks.py", "BASE_DIR"),
        ("earnings_vol_signal.py", "BASE_DIR"),
        # config.py：多为 `str(...)` 快照，消费方多数自行重解析，危害待判
        ("config.py", "CACHE_CONFIG"), ("config.py", "RUNTIME_CONFIG"),
        ("config.py", "ALERT_CONFIG"), ("config.py", "METRICS_CONFIG"),
        ("config.py", "PHEROMONE_CONFIG"), ("config.py", "MEMORY_CONFIG"),
        ("config.py", "VECTOR_MEMORY_CONFIG"), ("config.py", "CODE_EXECUTION_CONFIG"),
        ("config.py", "_OVERRIDE_YAML"), ("config.py", "_OVERRIDE_JSON"),
    }

    @staticmethod
    def _scan():
        """列出所有「import 期求值的 PATHS 派生赋值」。

        会下钻到 Try/If/With/For 体内——那些同样在 import 期执行。
        窄版（只看 tree.body 与 ClassDef.body）会漏掉 `pead_analyzer.py:20`，
        它就藏在一个 try 里。
        """
        import ast
        compound = (ast.Try, ast.If, ast.With, ast.For, ast.While)
        found = set()

        def bodies(s):
            for f in ("body", "orelse", "finalbody"):
                yield from getattr(s, f, []) or []
            for h in getattr(s, "handlers", []) or []:
                yield from h.body

        def walk(body, rel, depth=0):
            for s in body:
                if isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value:
                    if "PATHS" in ast.unparse(s.value):
                        tgts = [s.target] if isinstance(s, ast.AnnAssign) else s.targets
                        for t in tgts:
                            if isinstance(t, ast.Name):
                                found.add((rel, t.id))
                if isinstance(s, ast.ClassDef):
                    walk(s.body, rel, depth + 1)
                elif isinstance(s, compound) and depth < 6:
                    walk(list(bodies(s)), rel, depth + 1)

        for p in sorted(REPO_ROOT.rglob("*.py")):
            r = p.relative_to(REPO_ROOT)
            if any(x in r.parts for x in (".git", "tests", "__pycache__", ".claude",
                                          "experiments", "node_modules")):
                continue
            try:
                walk(ast.parse(p.read_text(encoding="utf-8")).body, str(r))
            except SyntaxError:
                continue
        return found

    def test_scanner_has_teeth(self):
        """反向自证：扫描器必须真能扫到东西。

        返回空集时下面那条断言恒真——那不是「干净」，是「扫描器坏了」。
        同款教训：v0.45.129 的 `len(ALL) > 50` 护栏。
        """
        assert len(self._scan()) >= 15, (
            "扫描器只找到极少数命中，几乎肯定是它自己坏了（路径过滤器写错、"
            "或 rglob 没匹配到），而不是仓库突然干净了")

    def test_no_new_frozen_paths_site(self):
        new = self._scan() - self.KNOWN
        assert not new, (
            "新增了「PATHS 派生值在 import 期被求值成常量/类属性」的点：\n"
            + "\n".join(f"  - {f}:{n}" for f, n in sorted(new))
            + "\n\n这类值会冻在 import 那一刻，pytest 收集期 import ⇒ "
              "conftest 的环境隔离对它无效，跑测试会写到生产路径。\n"
            "改法：改成 property / 函数（调用时求值）；"
            "若是默认参数，写 `= None` 再在函数体里解析——"
            "`= 某常量` 同样在 def 执行时（import 期）就冻住了。")

    def test_high_risk_trio_stays_fixed(self):
        """v0.45.150 修掉的三处不许回退成类属性/模块常量。"""
        regressed = {(f, n) for f, n in self._scan()
                     if (f, n) in {("backtester.py", "DB_PATH"),
                                   ("memory_store.py", "DB_PATH"),
                                   ("vector_memory.py", "DEFAULT_DB_PATH")}}
        assert not regressed, (
            f"v0.45.150 修掉的高危处又被写回成 import 期常量：{sorted(regressed)}")
