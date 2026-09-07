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

    # 存量清单（v0.45.150 实测，20 处）。**修好一处就从这里删掉一行。**
    # 高危三处（backtester / memory_store / vector_memory）已在 v0.45.150 修掉，
    # 故不在此列——它们若复发，会被本文件 ① 组直接抓住。
    # `generate_ml_report._model_file` 已由 v0.45.149 改成 property，同样不在此列。
    #
    # ⚠️ 子集语义有个副作用：**修好存量不会让本条变红**，于是过期项会悄悄留下。
    #    定期对账：把 `_scan()` 的结果与本集合相减，`KNOWN - actual` 非空即是过期项。
    KNOWN = {
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

    # 只扫**我们自己的**文件。生产 checkout 里 `rglob("*.py")` 会扫到 15349 个
    # .py（含 `mcp-servers/*/.venv/**/site-packages`），而 git 跟踪的只有 327 个
    # ——98% 是第三方库。拿我们的规范去审计 joblib 既无意义，又会让计数随
    # 「本地装了哪些 venv」漂移（worktree 与主 checkout 报的数会不一样）。
    _VENDORED = ("node_modules", "site-packages", "vendor", "third_party")

    @staticmethod
    def _own_python_files():
        """本仓自己的 .py 清单 + 用的是哪种口径。

        首选 `git ls-files`：天然排除未跟踪与 vendored 内容，且**口径可复现**
        （别人复跑对得上）。但本仓 CI 自检会用 `git archive` 导出**没有 .git**
        的干净检出，那里 git 口径不可用，故必须有回退。

        ⚠️ 子进程有**两条**失败路径，各堵一次（v0.45.117/119 同款教训）：
           `git` 不存在会**抛** `FileNotFoundError`，仓库不可用会**返回**非零。
           只判返回值的守卫接不住抛，只判异常的接不住返回。
        """
        import subprocess
        try:
            r = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=str(REPO_ROOT),
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip("\x00").strip():
                rels = [x for x in r.stdout.split("\x00") if x]
                return [REPO_ROOT / x for x in rels], "git"
        except (OSError, subprocess.SubprocessError):
            pass                                    # 落到 rglob 回退
        files = []
        for p in REPO_ROOT.rglob("*.py"):
            rel = p.relative_to(REPO_ROOT)
            if any(x in rel.parts for x in TestSpeciesDoesNotSpread._VENDORED):
                continue
            if any(part.startswith(".") for part in rel.parts):   # .venv/.git/.claude…
                continue
            files.append(p)
        return files, "rglob"

    @staticmethod
    def _scan(_stats=None, marker="PATHS"):
        """列出所有「import 期求值的 PATHS 派生赋值」。

        会下钻到 Try/If/With/For 体内——那些同样在 import 期执行。
        窄版（只看 tree.body 与 ClassDef.body）会漏掉 `pead_analyzer.py:20`，
        它就藏在一个 try 里。
        """
        import ast
        compound = (ast.Try, ast.If, ast.With, ast.For, ast.While)
        found = set()
        undecodable, unparsable = [], []

        def bodies(s):
            for f in ("body", "orelse", "finalbody"):
                yield from getattr(s, f, []) or []
            for h in getattr(s, "handlers", []) or []:
                yield from h.body

        def walk(body, rel, depth=0):
            for s in body:
                if isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value:
                    if marker in ast.unparse(s.value):
                        tgts = [s.target] if isinstance(s, ast.AnnAssign) else s.targets
                        for t in tgts:
                            if isinstance(t, ast.Name):
                                found.add((rel, t.id))
                if isinstance(s, ast.ClassDef):
                    walk(s.body, rel, depth + 1)
                elif isinstance(s, compound) and depth < 6:
                    walk(list(bodies(s)), rel, depth + 1)

        files, mode = TestSpeciesDoesNotSpread._own_python_files()
        for p in sorted(files):
            r = p.relative_to(REPO_ROOT)
            if any(x in r.parts for x in (".git", "tests", "__pycache__", ".claude",
                                          "experiments", "node_modules")):
                continue
            try:
                src = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # 非 UTF-8 源文件。**记账**再跳过——直接 continue 就是
                # 「跳过缺失项＝把缺失渲染成不存在」（v0.45.114）；
                # 直接让它抛则是「守卫死掉」冒充「没有违规」。
                undecodable.append(str(r)); continue
            except OSError:
                undecodable.append(str(r)); continue
            try:
                walk(ast.parse(src).body, str(r))
            except SyntaxError:
                unparsable.append(str(r)); continue
        if _stats is not None:
            _stats.update(mode=mode, n_files=len(files),
                          undecodable=undecodable, unparsable=unparsable)
        return found

    def test_scanner_survives_non_utf8_source(self, tmp_path, monkeypatch):
        """非 UTF-8 源文件必须被**记账跳过**，而不是让整条守卫崩掉。

        v0.45.150 事故：本守卫在 worktree 全绿、在**主 checkout 三条全红**——
        `UnicodeDecodeError` 不是 `SyntaxError`，原来的 `except SyntaxError`
        接不住它，扫描器就地抛死。元凶是 vendored 进来的
        `…/site-packages/joblib/test/test_func_inspect_special_encoding.py`
        （joblib 自带的 big5 编码夹具，全仓唯一一个非 UTF-8 的 .py）。

        ⚠️ 守卫**崩掉**和**没有违规**是两回事，但在测试结果上都表现为一个红点，
        所以必须单独钉住这条路径。下面用同样的 big5 字节复现。

        ⚠️ 本条刻意**驱动真的 `_scan()`**（改 `REPO_ROOT` 指向 tmp 树），
        不在测试里重抄一遍读文件的循环——抄一遍就变成「测 helper 不测接线」：
        把 `_scan` 里的 `except UnicodeDecodeError` 删掉，抄版照样全绿
        （v0.45.126 同款教训）。
        """
        import sys
        (tmp_path / "big5_fixture.py").write_bytes(
            b"# -*- coding: big5 -*-\n"
            b"# Traditional Chinese: \xa4@\xa8\xc7\xa4\xa4\xa4\xe5\n"
            b"X = 1\n")
        (tmp_path / "offender.py").write_text(
            "from hive_logger import PATHS\nBAD_DIR = PATHS.home / 'x'\n", encoding="utf-8")
        (tmp_path / "broken_syntax.py").write_text("def (\n", encoding="utf-8")

        monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
        stats = {}
        found = self._scan(stats)          # ← 真的 _scan，不抛才算过

        assert stats["undecodable"] == ["big5_fixture.py"], (
            f"非 UTF-8 文件没被记账（实际 {stats.get('undecodable')}）——"
            "要么它抛穿了整轮扫描，要么被静默吞掉")
        assert stats["unparsable"] == ["broken_syntax.py"], (
            f"语法错文件没被记账（实际 {stats.get('unparsable')}）")
        assert ("offender.py", "BAD_DIR") in found, (
            "坏文件之后的好文件没被扫到——一个坏文件带走了整轮扫描")

    def test_scanner_does_not_audit_vendored_code(self, tmp_path, monkeypatch):
        """vendored 第三方代码里的同款写法**不得**被报成本仓违规。

        主 checkout 里 `rglob("*.py")` 命中 15349 个（含
        `mcp-servers/*/.venv/**/site-packages`），git 跟踪的只有 327 个
        ——98% 是第三方库。扫进去有两重害处：拿本仓规范审计 joblib，
        且**计数随本地装了哪些 venv 漂移**（worktree 与主 checkout 报的数不一样）。

        ⚠️ 本条造一棵**含 vendored 目录**的 tmp 树并驱动真的 `_scan()`。
        早期版本改成断言 `_own_python_files()` 的返回值，结果 mutation
        「把 `_scan` 里的 `files, mode = _own_python_files()` 换回全量 rglob」
        **在 worktree 里全绿**——因为 worktree 根本没有 `.venv` 可撞。
        既是「测 helper 不测接线」，也是「在没有病灶的环境里测防御」。
        """
        import sys
        (tmp_path / "ours.py").write_text(
            "from hive_logger import PATHS\nOURS_DIR = PATHS.home / 'x'\n", encoding="utf-8")
        # 三类必须各覆盖一次，否则 mutation 分不出是哪道过滤器在起作用：
        #   ① 点号开头目录（`.venv`）—— 由「隐藏目录」那道过滤拦下
        #   ② `node_modules` —— **两道**过滤里都有它，单删一道属等价变异
        #   ③ 非点号、非 node_modules 的 vendored（`libs/site-packages`、
        #      `vendor`、`third_party`）—— **只有** `_VENDORED` 那道拦得住，
        #      少了这类样本，「删掉 _VENDORED」会全绿（v0.45.150 实测 M11）
        for vendored in (".venv/lib/python3.12/site-packages/thirdparty",
                         "mcp-servers/x/.venv/site-packages/lib",
                         "node_modules/pkg",
                         "libs/site-packages/pkg",
                         "vendor/dep",
                         "third_party/dep",
                         # ④ 隐藏目录但**名字不含任何 vendored 关键字**——
                         #    只有「隐藏目录」那道过滤拦得住它。少了这类样本，
                         #    「删掉隐藏目录过滤」是等价变异（实测 M12 全绿）
                         ".cache/build",
                         ".tox/py311"):
            d = tmp_path / vendored
            d.mkdir(parents=True, exist_ok=True)
            (d / "vendored_mod.py").write_text(
                "from hive_logger import PATHS\nTHEIR_DIR = PATHS.home / 'y'\n",
                encoding="utf-8")

        monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
        found = self._scan()

        assert ("ours.py", "OURS_DIR") in found, "本仓自己的违规反而没扫到"
        # **精确相等**，不是「不含 site-packages 之类关键字」的黑名单。
        # 黑名单版实测漏过 M12：`.cache/build/…` 不含任何关键字，删掉「隐藏目录」
        # 那道过滤后它照样泄漏、断言却仍全绿。凡是「只有这一个才对」的场合，
        # 写等号比写黑名单可靠。
        assert {f for f, _ in found} == {"ours.py"}, (
            "扫描器把本仓之外的文件当成违规报出来了："
            f"{sorted(f for f, _ in found if f != 'ours.py')}\n"
            "这会让计数随本地装了哪些 venv / 缓存目录漂移，"
            "也等于拿本仓规范去审计第三方库。")

    @pytest.mark.parametrize("failure", ["raises", "returns_nonzero"])
    def test_falls_back_to_rglob_when_git_unavailable(self, failure, monkeypatch):
        """git 口径不可用时必须回退到 rglob，而不是交出空清单。

        本仓 CI 自检会用 `git archive` 导出**没有 .git** 的干净检出
        （v0.45.117 的做法），那里 `git ls-files` 必然失败。若此时返回空清单，
        `_scan()` 就恒返回空集 —— **一个恒真的守卫**，比没有守卫更糟。

        ⚠️ 子进程有**两条**失败路径，各测一次：
          `raises`          —— git 不存在，`subprocess.run` 抛 `FileNotFoundError`
          `returns_nonzero` —— 不是仓库，返回码 128
        只堵一条的守卫会被另一条穿过（v0.45.117/119 同款）。
        """
        import subprocess as _sp

        real_run = _sp.run

        def fake_run(cmd, *a, **k):
            if cmd[:2] == ["git", "ls-files"]:
                if failure == "raises":
                    raise FileNotFoundError("git not found")
                return _sp.CompletedProcess(cmd, 128, stdout="", stderr="not a repo")
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(_sp, "run", fake_run)
        files, mode = self._own_python_files()
        assert mode == "rglob", f"git 不可用时没走回退（mode={mode}）"
        assert len(files) > 50, (
            f"回退只找到 {len(files)} 个 .py —— 空/近空清单会让 _scan() 恒返回空集，"
            "变成一个永远不会红的假守卫")

    def test_own_file_list_is_sane(self):
        """扫描范围的量级护栏：本仓自己的 .py 是几百量级。"""
        files, mode = self._own_python_files()
        assert 50 < len(files) < 2000, (
            f"口径={mode} 扫到 {len(files)} 个 .py。"
            "过多＝混进了第三方库（生产 checkout 全量 rglob 是 15349 个）；"
            "过少＝清单机制自己坏了")

    def test_scan_reports_no_undecodable_among_own_files(self):
        """本仓自己的 .py 应当全是 UTF-8；若某天不是，要**看得见**而不是静默跳过。"""
        stats = {}
        self._scan(stats)
        assert stats["undecodable"] == [], (
            f"本仓有非 UTF-8 源文件，已被跳过（因而不受本守卫保护）：{stats['undecodable']}")
        assert stats["unparsable"] == [], (
            f"本仓有语法不可解析的 .py，已被跳过：{stats['unparsable']}")

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


class TestFileDerivedSpeciesDoesNotSpread:
    """不许**新增**「`__file__` 派生的数据路径常量」（v0.45.160）。

    这一族比 `PATHS` 冻结**更严重**：`Path(__file__).parent / "pheromone.db"`
    压根不读任何环境变量，`ALPHA_HIVE_HOME` / `ALPHA_HIVE_DB_PATH` 设成什么都无效，
    **连改成懒求值都救不了**——只能显式改去读 `PATHS.*`。

    ⚠️ 但 `__file__` **本身不是错的**，一刀切会制造新 bug。它是**代码同址资源**的
    正确锚点：模板、prompt、随代码发布的只读配置、`sys.path.insert`、`git -C <仓库>`
    ——这些要的就是「代码在哪」，改成 `PATHS.home` 会在测试把 HOME 指向 tmp 后
    直接找不到文件。判据是**这个路径指向「代码」还是「数据」**：
    数据（库/账本/状态/缓存/产物）→ 必须走 `PATHS.*`；代码同址资源 → `__file__` 正确。

    与 `TestSpeciesDoesNotSpread` 一样是**子集**语义：清掉存量不会变红，新增必红。
    """

    # 存量白名单（v0.45.160 清理后实测 **17 处**）。清掉一处就从这里删一行。
    # ⚠️ 子集语义的副作用：**清干净了也不会变红**，过期项会悄悄留下。
    #    定期对账：`KNOWN - _scan(marker="__file__")` 非空即是过期项
    #    （本版就这么揪出 2 条已清却还挂着的）。
    # 分三类，都是**经判定后确认应当保留**或已另有覆盖的：
    KNOWN = {
        # ── A. 代码同址资源 / 工程根：`__file__` 是**正确**锚点，不该改 ──
        ("dashboard_renderer.py", "_TPL_DIR"),        # templates/，随代码发布
        ("prompt_loader.py", "_PROMPTS_DIR"),         # prompts/，随代码发布
        ("probability_scorecard.py", "ALPHAHIVE_DIR"),  # sys.path.insert
        ("scan_continuity.py", "ALPHAHIVE_DIR"),        # sys.path.insert
        ("ic_rerun_readiness.py", "ALPHAHIVE_DIR"),     # sys.path.insert
        ("health_check.py", "PROJECT"),               # git -C <仓库>
        ("cloud_snapshot_loader.py", "REPO_DIR"),     # git cwd
        ("collect_data.py", "_SCRIPT_DIR"),           # 运行环境探测
        # ── B. 仓库内随代码发布的**只读**配置/文档 ──
        ("thesis_breaks.py", "_CONFIG_JSON_PATH"),    # thesis_breaks_config.json，生产只读
        ("market_intelligence.py", "_BASE"),          # 同上
        ("watchlist_events.py", "EVENTS_FILE"),       # watchlist_events.md
        # ── C. 已读 `ALPHA_HIVE_HOME`、`__file__` 只作兜底（另一个子物种，冻在 import 期）──
        ("agent_toolbox.py", "ALLOWED_ROOTS"),        # 沙箱白名单；冻结只会更保守不会越权
        ("gui/app.py", "_PROJECT_ROOT"),              # sys.path
        ("scheduler.py", "_PROJECT_ROOT"),            # scheduler.log
        # ── D. 未清，已登记（读多写少 / 牵动面大）──
        ("pead_analyzer.py", "_CACHE_DIR"),           # try 分支已走 PATHS，这是 except 兜底
        ("push_report_to_slack.py", "PROJECT_DIR"),   # 读报告 json（CLI 脚本）
        ("scan_coverage_gate.py", "ROOT"),            # 读 .swarm_results_*.json
    }

    def test_scanner_has_teeth(self):
        """反向自证：换成 `__file__` 标记后扫描器必须仍能扫到东西。"""
        found = TestSpeciesDoesNotSpread._scan(marker="__file__")
        assert len(found) >= 10, (
            f"只找到 {len(found)} 处——几乎肯定是扫描器坏了，而不是仓库突然干净了")

    def test_no_new_file_derived_paths(self):
        new = TestSpeciesDoesNotSpread._scan(marker="__file__") - self.KNOWN
        assert not new, (
            "新增了「`__file__` 派生的路径常量」：\n"
            + "\n".join(f"  - {f}:{n}" for f, n in sorted(new))
            + "\n\n先判它指向**代码**还是**数据**：\n"
              "  · 数据（库/账本/状态/缓存/产物）⇒ 必须改成运行时读 `PATHS.*`，"
              "`__file__` 派生值完全无视环境变量，测试隔离对它无效；\n"
              "  · 代码同址资源（模板/prompt/只读配置/sys.path/git -C）⇒ `__file__` 正确，"
              "把它加进本类的 KNOWN 白名单并注明属于哪一类。")

    def test_detection_actually_finds_a_synthetic_offender(self, tmp_path, monkeypatch):
        """正向对照：往一棵 tmp 树里放一个真违规，检测机制必须发现它。

        没有这条的话，`test_no_new_file_derived_paths` 只证明了「现在没有新增」，
        证明不了「新增了能被发现」——扫描器若某天不再匹配 `__file__` 形态，
        它会安静地永远绿（v0.45.71「守卫自己恒真」）。

        ⚠️ 说明一下 mutation 的边界：把上面那条断言直接改成 `new = set()`
        （恒真）**没有任何测试抓得住**——那是「变异到断言自身」，是 mutation
        测自己断言的固有上限，不是覆盖缺口。本条能挡的是另一件事：
        **检测机制**（文件集 + 下钻 + 标记匹配）失效。
        """
        import sys
        (tmp_path / "offender.py").write_text(
            "from pathlib import Path\n"
            'DB_PATH = Path(__file__).parent / "pheromone.db"\n', encoding="utf-8")
        # 嵌套在 try 里的违规——**加宽扫描的全部意义就在这里**：当初正是靠下钻
        # 复合语句才发现 `pead_analyzer.py` 那处藏在 try 里的。夹具若只放模块级的，
        # 「关掉下钻」这个变异会全绿（v0.45.160 实测 M20）。
        (tmp_path / "nested_offender.py").write_text(
            "from pathlib import Path\n"
            "try:\n"
            '    NESTED_DIR = Path(__file__).parent / "state"\n'
            "except ImportError:\n"
            "    NESTED_DIR = None\n", encoding="utf-8")
        (tmp_path / "innocent.py").write_text(
            "from hive_logger import PATHS\n"
            "def _db():\n    return PATHS.db      # 调用时求值，不该被报\n",
            encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

        found = TestSpeciesDoesNotSpread._scan(marker="__file__")
        assert ("offender.py", "DB_PATH") in found, (
            "检测机制没发现明摆着的 `Path(__file__).parent / \"pheromone.db\"`——"
            "扫描器坏了，上面那条「无新增」断言因此是恒真的")
        assert ("nested_offender.py", "NESTED_DIR") in found, (
            "藏在 `try:` 里的违规没被发现——扫描器不再下钻复合语句了。"
            "那些语句在 import 期同样执行，漏掉它们等于普查是低估的")
        assert ("innocent.py", "_db") not in found, (
            "把「函数内调用时求值」也报成违规了——会逼着人把正确写法加进白名单")

    def test_cleaned_modules_stay_clean(self):
        """v0.45.160 清掉的那些不许回退成 `__file__` 派生常量。"""
        cleaned = {
            ("signal_archive.py","DB_PATH"), ("vol_forecast.py","DB_PATH"),
            ("ic_diagnostics.py","DB_PATH"), ("close_correction.py","DB_PATH"),
            ("backfill_dir_accuracy.py","DB"), ("replay_scoring.py","DB_PATH"),
            ("feedback_loop.py","PHEROMONE_DB_PATH"),
            ("cboe_vix.py","_CACHE_PATH"), ("congress_trades_scraper.py","_CACHE_PATH"),
            ("fear_greed.py","_CACHE_PATH"), ("vix_term_structure.py","_CACHE_PATH"),
            ("yahoo_trending.py","_CACHE_PATH"), ("real_data_sources.py","CACHE_DIR"),
            ("reddit_sentiment.py","CACHE_DIR"), ("factor_attribution.py","_CACHE_DIR"),
            ("risk_engine.py","_SNAPSHOTS_DIR"), ("risk_engine.py","_CACHE_DIR"),
            ("ibkr_sync.py","BASE"), ("param_optimizer.py","BASE_DIR"),
        }
        regressed = TestSpeciesDoesNotSpread._scan(marker="__file__") & cleaned
        assert not regressed, (
            f"v0.45.160 清掉的又被写回成 `__file__` 派生常量：{sorted(regressed)}")
