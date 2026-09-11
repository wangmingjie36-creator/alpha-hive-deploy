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


from tests._repo_files import VENDORED, own_python_files

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

    # 只扫**我们自己的**文件。生产 checkout 里 `rglob("*.py")` 会扫到两万多个
    # .py（含 `mcp-servers/*/.venv/**/site-packages` 与 `.claude/worktrees/` 下的
    # 10 个嵌套 worktree），而 git 跟踪的只有 338 个——绝大多数是第三方库和
    # 别的 checkout。拿我们的规范去审计 joblib 既无意义，又会让计数随
    # 「本地装了哪些 venv」漂移（worktree 与主 checkout 报的数会不一样）。
    #
    # v0.45.186：实现已抽到 `tests/_repo_files.py`，本处只剩委托。
    # 抽走的理由不是去重好看——v0.45.176 在 `test_zero_weight_invariant.py` 里
    # 写了第二份（裸 rglob），在 10 个 worktree 里全绿、只在生产 checkout 上红，
    # 而生产 checkout 没人跑测试。共享实现是为了让下一个人**没机会**再写一份。
    _VENDORED = VENDORED

    @staticmethod
    def _own_python_files():
        """委托给共享实现。`REPO_ROOT` 在**调用时**取，故 monkeypatch 仍然生效。"""
        return own_python_files(REPO_ROOT)

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

    def test_git_branch_applies_the_same_filter_as_the_fallback(self, tmp_path):
        """两条分支必须**同口径** —— 用一个真 git 仓库把 git 分支单独钉住。

        没有这一条，`own_python_files` 里 git 分支上那句 `if _is_ours(x)`
        是个**等价变异**：本仓今天没有任何被跟踪的 .py 落在点号目录下，
        删掉它 338 还是 338，全套照绿（v0.45.186 实测）。
        MEMORY 的纪律是「举不出能让它变红的变异就别加」——这里造得出来，
        于是把它造出来，而不是留一行测不到的代码。

        为什么这条口径分歧不是假想：`.claude/` 本身**被跟踪**
        （`.claude/launch.json` 在库里）且**不在 .gitignore**，
        所以哪天有人提交 `.claude/hooks/x.py`，git 分支会扫到、回退分支不会，
        而哪条分支生效取决于「这台机器有没有装 git」。

        变红的变异：把 git 分支的 `if _is_ours(x)` 去掉
        （`return [root / x for x in rels], "git"`）。
        """
        import subprocess

        def git(*args):
            return subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=str(tmp_path), capture_output=True, text=True, timeout=60)

        assert git("init", "-q").returncode == 0, "tmp 仓库没建起来"
        (tmp_path / "ours.py").write_text("x = 1\n", encoding="utf-8")
        hidden = tmp_path / ".claude" / "hooks"
        hidden.mkdir(parents=True)
        (hidden / "theirs.py").write_text("x = 1\n", encoding="utf-8")
        assert git("add", "-A").returncode == 0
        assert git("commit", "-qm", "fixture").returncode == 0

        files, mode = own_python_files(tmp_path)
        assert mode == "git", (
            f"夹具没走到 git 分支（mode={mode}），这条测的就不是它要测的东西")
        rels = sorted(str(f.relative_to(tmp_path)) for f in files)
        # 正面断言夹具确实接上了：两个文件都被 git 跟踪，只是其中一个该被滤掉。
        assert git("ls-files", "*.py").stdout.count(".py") == 2, (
            "夹具本身没造出两个被跟踪的 .py —— 下面那条会恒真地绿")
        assert rels == ["ours.py"], (
            f"git 分支没应用 `_is_ours`，点号目录下的文件漏了进来：{rels}")

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

    # 存量白名单（v0.45.198 清理 `agent_toolbox.ALLOWED_ROOTS` 后实测 **16 处**）。清掉一处就从这里删一行。
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
        # v0.45.198: ("agent_toolbox.py", "ALLOWED_ROOTS") 已摘除 —— `FilesystemTool`
        # 零读者被删，`ALLOWED_ROOTS` 随之消失。⚠️ 摘它**不是**因为有测试变红：
        # 子集语义下清干净不会红，它会静默变成过期项。是按本类 docstring 的对账法
        # （`KNOWN - _scan(marker="__file__")` 非空即过期）手动揪出来的。
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

    # —— 元守卫：先于两条内容守卫回答「这两张表彼此自洽吗」 ——
    # **定义位置就是语义**（同 v0.45.202）：addopts 带 `-x` ⇒ 谁先红谁就是人看到的
    # 那句诊断。两张表不自洽时，下面 `test_no_new_file_derived_paths` 也会红，但它
    # 说的是「**新增**了 `__file__` 派生路径」——真因却是「登记漏了一张表」，正是
    # CLAUDE.md 记的「把一种失败误报成另一种」。故本条必须排在它前面；顺序由
    # `test_meta_guard_speaks_first` 反射核对，挪动即红。
    # （`MUST_STAY_FILE_ANCHORED` 定义在本类稍下方，属性在**调用时**取，不受影响。）

    def test_both_directions_are_guarded(self):
        """元守卫：`MUST_STAY_FILE_ANCHORED ⊆ KNOWN`，且两边都不许空掉。

        ⚠️ v0.45.203 之前这里断言的是**不许重叠**，而且那句写法是**结构性恒真**的：

            overlap = MUST_STAY & {k for k in KNOWN if k not in MUST_STAY}

        推导式先把 `MUST_STAY` 里的元素全滤掉，再与 `MUST_STAY` 求交 ⇒ 对**任意**
        两个集合都恒为空集，与它们的实际内容无关（`x and not x` 的集合版）。
        实测：`A={1,2,3}; B={1,2,3,4,5}` ⇒ 恒真写法得 `set()`，本意写法 `A & B`
        得 `{1,2,3}`。也就是说这条断言从未检查过它 docstring 声称要检查的东西。

        更要命的是：它恒真地断言了**真实不变式的否定**。两条内容守卫是

            G1（子集）`_scan() - KNOWN == ∅`        ⇔ `_scan() ⊆ KNOWN`
            G2（超集）`MUST_STAY - _scan() == ∅`    ⇔ `MUST_STAY ⊆ _scan()`

        合起来 ⇒ `MUST_STAY ⊆ _scan() ⊆ KNOWN` ⇒ **`MUST_STAY ⊆ KNOWN`**。
        所以重叠不是「可以允许」，是**必须成立**：真要求「不许重叠」且 `MUST_STAY`
        非空，G1 与 G2 结构上永远不可能同时为绿。恰恰因为那句恒真，这个矛盾才
        没炸——一条死断言把一个自相矛盾的规格伪装成了已被守住的规格。

        两集合语义不同（`KNOWN` = 子集语义「不许新增」，`MUST_STAY` = 超集语义
        「不许错清」），同一项同时出现在两边完全正常：一处路径既「是已登记的
        `__file__` 派生项」又「必须保持 `__file__` 锚定」并不矛盾。反过来，
        `KNOWN` 可以**严格大于** `MUST_STAY`——多出来的是 C/D 两类（读 HOME 只拿
        `__file__` 兜底、或未清已登记），它们已登记但并不要求永远是 `__file__`。

        此前 `MUST_STAY ⊆ KNOWN` 一直靠巧合成立：没有任何东西断言它。
        """
        assert self.MUST_STAY_FILE_ANCHORED, "超集守卫的集合空了，它恒真"
        assert self.KNOWN, "子集守卫的白名单空了"
        unregistered = self.MUST_STAY_FILE_ANCHORED - self.KNOWN
        assert not unregistered, (
            "这些项要求「必须保持 `__file__` 锚定」，却没登记进 `KNOWN`：\n"
            + "\n".join(f"  - {f}:{n}" for f, n in sorted(unregistered))
            + "\n\n这不是风格问题，是**规格自相矛盾**：\n"
              "  · `test_code_anchored_paths_were_not_wrongly_converted` 要求它出现在"
              " `_scan()` 里；\n"
              "  · `test_no_new_file_derived_paths` 要求 `_scan()` 里的每一项都在"
              " `KNOWN` 里。\n"
              "两者不可能同时满足。改法：把上面这些项补进 `KNOWN`"
              "（注明属于 A/B 哪一类），而不是从 `MUST_STAY_FILE_ANCHORED` 里删掉——"
              "删掉等于放弃「不许被一刀切清理」那个方向的保护。")

    def test_meta_guard_speaks_first(self):
        """`test_both_directions_are_guarded` 必须排在两条内容守卫**之前**。

        `-x` 下只有第一个红点会被人读到。这条元守卫若排在后面，两张表不自洽时
        人看到的是「新增了 `__file__` 派生路径」——指着一个**生产文件**让你判断
        它是代码还是数据，而真因是白名单登记漏了一张表。

        照 v0.45.202 的做法**用反射核对定义顺序**，不靠注释提醒——注释不会变红。

        会变红的变异：把 `test_both_directions_are_guarded` 的整个方法体挪到
        `test_no_new_file_derived_paths` 之后（已实测）。
        """
        names = [k for k, v in vars(type(self)).items()
                 if k.startswith("test_") and callable(v)]
        pos = {n: i for i, n in enumerate(names)}
        assert "test_both_directions_are_guarded" in pos, (
            "`test_both_directions_are_guarded` 改名或被删了 —— 本条顺序守卫"
            "失去了主体。若确实要改名，连同本条一起改，别让它变成 KeyError。")
        for later in ("test_no_new_file_derived_paths",
                      "test_code_anchored_paths_were_not_wrongly_converted"):
            assert later in pos, f"{later} 改名或被删了，本条顺序守卫已失去参照物"
            assert pos["test_both_directions_are_guarded"] < pos[later], (
                f"`test_both_directions_are_guarded` 被挪到了 {later} 之后。\n"
                "addopts 带 `-x`，谁先红谁就是人看到的诊断；两张表不自洽时，"
                f"{later} 会把「登记漏表」误报成「新增/错清了路径」。")

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

    # 「必须**保持** `__file__`」的站点 —— 与 `KNOWN` 是**两个方向**：
    #   `KNOWN`（子集语义）防的是「新增冻结路径」；
    #   本集合（**超集**语义）防的是「把该留的错误地清掉」。
    # 二者缺一不可。v0.45.168 反向 mutation 实测：把这些错改成 `PATHS.home`，
    # **4/5 全绿**（只有 `dashboard_renderer` 因为它的测试真去读 CSS 才红）——
    # 也就是说在此之前，「错误清理」这件事没有任何东西会发现。
    #
    # 为什么它们必须是 `__file__`：这些路径指向**代码**，不是数据。模板、prompt、
    # 随代码发布的只读配置、`sys.path.insert`、`git -C <仓库>` 要的都是
    # 「代码在哪」。改成 `PATHS.home` 后，测试把 `ALPHA_HIVE_HOME` 指向 tmp
    # 就**找不到文件**——那不是修 bug，是造 bug。
    # ⚠️⚠️ **这个集合必须写死，不许改成「从 `_scan()` 派生」。**
    # 同一仓里两条守卫的写法要求是**相反**的，而它们看起来像矛盾：
    #   · 问「表的**当前内容**对不对」的守卫 —— **必须派生**
    #     （写死则每加一项手改一次，改着改着变恒真）
    #   · 问「有没有哪项**消失**了」的守卫 —— **必须写死**
    #     （唯一参照物就是「过去确实有过这项」；派生了就等于拿被测物证明自己）
    # 读过前一条教训的人最可能做的事，正是「顺手把这个也统一成派生」。
    # v0.45.166 那个 session 实测过：改成派生、再删一项 ⇒ **零红**。
    # 这段警告比守卫本身更难重建，所以写在这里而不是 CHANGELOG。
    MUST_STAY_FILE_ANCHORED = {
        # 代码同址资源：随代码发布
        ("dashboard_renderer.py", "_TPL_DIR"),        # templates/
        ("prompt_loader.py", "_PROMPTS_DIR"),         # prompts/
        # 工程根 / sys.path / git 仓库：要的是「代码在哪」
        ("probability_scorecard.py", "ALPHAHIVE_DIR"),
        ("scan_continuity.py", "ALPHAHIVE_DIR"),
        ("ic_rerun_readiness.py", "ALPHAHIVE_DIR"),
        ("health_check.py", "PROJECT"),               # git -C <仓库>
        ("cloud_snapshot_loader.py", "REPO_DIR"),     # git cwd
        ("collect_data.py", "_SCRIPT_DIR"),           # 运行环境探测
        # 仓库内随代码发布的**只读**配置/文档
        ("thesis_breaks.py", "_CONFIG_JSON_PATH"),
        ("market_intelligence.py", "_BASE"),
        ("watchlist_events.py", "EVENTS_FILE"),
    }

    def test_code_anchored_paths_were_not_wrongly_converted(self):
        """这 11 处必须**仍然**是 `__file__` 派生 —— 防「一刀切清理」。

        ⚠️ 这条断言的方向和 `test_no_new_file_derived_paths` **相反**。
        只有子集守卫时，「把模板路径改成 `PATHS.home`」会静默通过，
        而它会在测试隔离下让模板找不到（生产上则要等到 HOME 被设时才炸）。
        """
        still = TestSpeciesDoesNotSpread._scan(marker="__file__")
        converted = sorted(self.MUST_STAY_FILE_ANCHORED - still)
        assert not converted, (
            "这些路径被从 `__file__` 改走了，但它们指向的是**代码**不是数据：\n"
            + "\n".join(f"  - {f}:{n}" for f, n in converted)
            + "\n\n模板 / prompt / 只读配置 / `sys.path` / `git -C` 要的是"
              "「代码在哪」，`PATHS.home` 会跟着 `ALPHA_HIVE_HOME` 跑到 tmp 去。\n"
              "若确实要改（比如该资源真的变成了可写数据），请连同本集合一起改，"
              "并在 CHANGELOG 说明它为什么不再是代码同址资源。")

    @pytest.mark.parametrize("modname,attr", sorted(
        (f[:-3].replace("/", "."), n) for f, n in MUST_STAY_FILE_ANCHORED))
    def test_code_anchored_paths_resolve_inside_the_repo(self, modname, attr):
        """代码同址锚点解出来的东西必须**真的存在**、且**在仓库内**。

        v0.45.170：这条盯的是**值**，前两条盯的是**成员关系**，盲区不一样。
        成员关系守卫（`MUST_STAY_FILE_ANCHORED` / `KNOWN`）对「仍是 `__file__`
        派生、只是指错了地方」结构上够不到。全套实测四种指错：
        `prompt_loader` 指 `prompts_WRONG` / `thesis_breaks` 指 `WRONG.json` /
        `market_intelligence` 上跳一级 —— 三者被各模块自己的测试接住；
        而 **`probability_scorecard.ALPHAHIVE_DIR` 上跳一级全套零红**
        （它只喂 `sys.path.insert`，多一条错路径不影响 import 解析，
        所以没有任何断言依赖它）。

        判据刻意做成**通用**的，不再列「每项应该指向什么」的表（那又是快照）：
          · **存在** —— 指向不存在的目录/文件即红（覆盖 `prompts_WRONG` / `WRONG.json`）
          · **在仓库内** —— 上跳一级会跑到 `…/worktrees/` 去，即红
        两条合起来把上面四种形态全堵住，且新增条目自动纳入。
        """
        import importlib
        mod = importlib.import_module(modname)
        raw = getattr(mod, attr)
        # 取值可能是 list，只查其中落在仓库内的那些。
        # ⚠️ v0.45.198：本条参数化自 `MUST_STAY_FILE_ANCHORED`，而那 11 项**取值全是标量**
        #    ⇒ 下面这个 list 分支目前**一次也没被执行过**，是防御性的。
        #    （原注释举的例子 `agent_toolbox.ALLOWED_ROOTS` 在 `KNOWN` 里、从不在本条参数里，
        #     所以那个例子对本条从一开始就不成立；该符号已随 `FilesystemTool` 一并删除。）
        vals = raw if isinstance(raw, (list, tuple)) else [raw]
        checked = 0
        for v in vals:
            pth = Path(str(v)).resolve()
            if isinstance(raw, (list, tuple)) and REPO_ROOT not in pth.parents and pth != REPO_ROOT:
                continue                       # list 里指向 ~ 之类的项不在本条管辖内
            checked += 1
            assert pth.exists(), (
                f"{modname}.{attr} 指向不存在的位置：{pth}\n"
                "代码同址资源必须真的在那儿——指错目录/文件名时，"
                "成员关系守卫看不见（它仍然是 `__file__` 派生）。")
            assert pth == REPO_ROOT or REPO_ROOT in pth.parents, (
                f"{modname}.{attr} 解到了**仓库之外**：{pth}\n"
                f"（仓库根 = {REPO_ROOT}）多一级 `.parent` 就会跑到 worktrees/ 去。")
        assert checked, f"{modname}.{attr} 没有任何值落在仓库内，本条等于没测"

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


def _discover_path_resolvers():
    """结构性枚举**所有**「零参数的 `PATHS.*` 路径解析器」。

    v0.45.160（承 v0.45.146/152 session 的判据）：**按名字匹配的检测器，只能证明
    「匹配到的是对的」，不能证明「没匹配到的是错的」。** 实证就在本仓：这类解析器
    有 27 个，**名字互不相同**（`default_db_path` / `_cache_path` / `_base_dir` /
    `DB_PATH` / `default_model_path` / `_sentiment_db_path` / `default_path` …）。
    列一张名单去测，只会覆盖到你想起来的那几个；本函数改为**从 AST 枚举**，
    新增的解析器**自动**进入下面的行为测试，不必有人记得来加一行。

    判据（全部结构性，不看名字）：
      · 零必需参数（能直接调用）
      · 每一条 `return` 的表达式要么含 `PATHS.`，要么是从 `PATHS.` 派生的局部变量，
        要么是覆盖钩子形态 `Path(<全大写名>)`（本仓 v0.45.150/160 的统一写法）
      · 函数体 ≤ 14 条语句（排除顺手引用了 PATHS 的业务函数）
    """
    import ast
    import subprocess
    files, _mode = TestSpeciesDoesNotSpread._own_python_files()
    found = []
    for p in sorted(files):
        rel = p.relative_to(REPO_ROOT)
        if any(x in rel.parts for x in ("tests", "experiments", "__pycache__")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.FunctionDef) or len(n.body) > 14:
                continue
            a = n.args
            required = (len(a.posonlyargs) + len(a.args) - len(a.defaults)
                        + len([k for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]))
            if required:
                continue
            derived = {t.id for st in ast.walk(n) if isinstance(st, ast.Assign)
                       for t in st.targets
                       if isinstance(t, ast.Name) and "PATHS." in ast.unparse(st.value)}
            rets = [st for st in ast.walk(n) if isinstance(st, ast.Return) and st.value]
            if not rets or not any("PATHS." in ast.unparse(st) for st in ast.walk(n)):
                continue

            def _is_hook(expr):
                """覆盖钩子形态：`Path(HOOK)` / `str(HOOK)` / `os.fspath(HOOK)`。"""
                return (isinstance(expr, ast.Call) and len(expr.args) == 1
                        and isinstance(expr.args[0], ast.Name)
                        and expr.args[0].id.isupper())

            if all("PATHS." in ast.unparse(r.value)
                   or any(d in ast.unparse(r.value) for d in derived)
                   or _is_hook(r.value)
                   for r in rets):
                # 顺带带出「覆盖钩子」的名字（`if HOOK is not None: return Path(HOOK)`），
                # 行为测试要先把它清成 None 再验 env——否则 autouse fixture 设过的
                # 覆盖会让断言失败，而那恰恰是**正确行为**（v0.45.160 实测
                # `feedback_loop` 就这么假红了一次）。
                hook = next((h.args[0].id for r in rets if _is_hook(r.value)
                             for h in [r.value]), None)
                found.append((str(rel)[:-3].replace("/", "."), n.name, hook))
    return sorted(set(found))


_PATH_RESOLVERS = _discover_path_resolvers()


class TestEveryResolverFollowsEnv:
    """**每一个**路径解析器都必须跟着 `ALPHA_HIVE_HOME` 走（v0.45.160）。

    结构守卫（`TestSpeciesDoesNotSpread` / `TestFileDerivedSpeciesDoesNotSpread`）
    能证明「它不是 import 期常量」，**证明不了「它跟着 env 走」**——
    比如哪天有人给某个解析器加上 `@lru_cache`，结构守卫不会红（它只看
    `Assign`/`AnnAssign`，看不见装饰器），只有行为测试会。

    参数化列表来自 AST **枚举**而非人工名单，所以新增解析器自动被覆盖。
    """

    # 必须在枚举里的最小集合。**这不是冗余**：参数化测试有个隐蔽的规避路径——
    # 把某个解析器改成不再引用 `PATHS.`（比如冻回 `Path(__file__)…`），它就
    # **不再被枚举到**，那条用例直接消失，`collected` 少 1、**没有任何红**。
    # 实测（v0.45.160 mutation M22）：56 → 55 passed，静默失去覆盖。
    # 这是「跳过缺失项＝把缺失渲染成不存在」的 parametrize 版本。
    # ⚠️⚠️ **这个集合必须写死，不许改成「从 `_scan()` 派生」。**
    # 同一仓里两条守卫的写法要求是**相反**的，而它们看起来像矛盾：
    #   · 问「表的**当前内容**对不对」的守卫 —— **必须派生**
    #     （写死则每加一项手改一次，改着改着变恒真）
    #   · 问「有没有哪项**消失**了」的守卫 —— **必须写死**
    #     （唯一参照物就是「过去确实有过这项」；派生了就等于拿被测物证明自己）
    # 读过前一条教训的人最可能做的事，正是「顺手把这个也统一成派生」。
    # v0.45.166 那个 session 实测过：改成派生、再删一项 ⇒ **零红**。
    # 这段警告比守卫本身更难重建，所以写在这里而不是 CHANGELOG。
    MUST_BE_ENUMERATED = {
        ("backtester", "default_db_path"), ("signal_archive", "_db_path"),
        ("vol_forecast", "_db_path"), ("ic_diagnostics", "_db_path"),
        ("close_correction", "_db_path"), ("replay_scoring", "_db_path"),
        ("feedback_loop", "_db_path"), ("backfill_dir_accuracy", "_db_path"),
        ("paper_portfolio", "_pheromone_db_path"), ("ibkr_sync", "_base_dir"),
        ("param_optimizer", "_base_dir"), ("risk_engine", "_cache_dir"),
        ("risk_engine", "_snapshots_dir"), ("factor_attribution", "_cache_dir"),
        ("reddit_sentiment", "_cache_dir"), ("real_data_sources", "_cache_dir"),
        ("fear_greed", "_cache_path"), ("cboe_vix", "_cache_path"),
        ("yahoo_trending", "_cache_path"), ("vix_term_structure", "_cache_path"),
        ("congress_trades_scraper", "_cache_path"),
        ("economic_calendar_watch", "_state_path"),
    }

    def test_discovery_has_teeth(self):
        """反向自证：枚举必须真找到东西，且**已知的那些一个都不能少**。"""
        assert len(_PATH_RESOLVERS) >= 15, (
            f"只枚举到 {len(_PATH_RESOLVERS)} 个解析器——几乎肯定是枚举器坏了")
        names = {n for _, n, _ in _PATH_RESOLVERS}
        assert len(names) >= 8, (
            f"解析器名字只有 {len(names)} 种，与「名字互不相同」的实测不符，枚举可疑")
        missing = self.MUST_BE_ENUMERATED - {(m, f) for m, f, _ in _PATH_RESOLVERS}
        assert not missing, (
            f"这些解析器**从枚举里消失了**：{sorted(missing)}\n"
            "多半是它不再引用 `PATHS.`（被冻回 `Path(__file__)…` 或写死了路径）。"
            "消失＝那条行为测试静默不再运行，比它变红更危险。")

    @pytest.mark.parametrize("modname,fnname,hook", _PATH_RESOLVERS,
                             ids=[f"{m}.{f}" for m, f, _ in _PATH_RESOLVERS])
    def test_resolver_follows_env(self, modname, fnname, hook, tmp_path, monkeypatch):
        """改 `ALPHA_HIVE_HOME` 后，解析出来的路径必须落进新目录。

        ⚠️ 先把该模块的**覆盖钩子清成 `None`**：`conftest` 里有 autouse fixture
        （如 `_isolate_feedback_loop_close_t7_db`）会把钩子指向别处，那是**正确行为**，
        不清掉就会假红。本条要验的是「**没有覆盖时**它跟不跟 env」。
        """
        import importlib
        mod = importlib.import_module(modname)
        if hook is not None and hasattr(mod, hook):
            monkeypatch.setattr(mod, hook, None)
        fn = getattr(mod, fnname)
        fn = fn.fget if isinstance(fn, property) else fn

        sandbox = tmp_path / "envprobe"
        sandbox.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(sandbox))
        monkeypatch.delenv("ALPHA_HIVE_DB_PATH", raising=False)
        monkeypatch.delenv("ALPHA_HIVE_CHROMA_PATH", raising=False)
        monkeypatch.delenv("ALPHA_HIVE_CACHE_DIR", raising=False)
        monkeypatch.delenv("ALPHA_HIVE_LOGS_DIR", raising=False)

        got = str(fn())
        assert str(sandbox) in got, (
            f"{modname}.{fnname}() 返回 {got}，没落进 ALPHA_HIVE_HOME。\n"
            "要么它把值冻住了（模块级常量 / 默认参数 / `@lru_cache`），"
            "要么它读的是别的锚点（`__file__`）。两者都会让测试隔离失效。")


class TestNoCachedResolvers:
    """路径解析器不许被缓存装饰器修饰（v0.45.160）。

    `@lru_cache` / `@cache` / `@cached_property` 修饰的解析器**首次调用后冻住**——
    不是 import 期冻，但一样穿透逐测试隔离：第一个用到它的测试决定了整个 session
    的落点。而 `TestSpeciesDoesNotSpread._scan()` 只看 `Assign`/`AnnAssign`，
    **结构上看不见函数装饰器**，所以这条要单列。

    由 v0.45.146/152 session 提出；实测本仓当前 **0 处**，本条是防复发。
    """

    @staticmethod
    def _cached_resolvers():
        import ast
        files, _ = TestSpeciesDoesNotSpread._own_python_files()
        bad = []
        for p in sorted(files):
            rel = p.relative_to(REPO_ROOT)
            if any(x in rel.parts for x in ("tests", "experiments", "__pycache__")):
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for n in ast.walk(tree):
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not n.decorator_list:
                    continue
                body = "\n".join(ast.unparse(s) for s in n.body)
                if "PATHS." not in body and "__file__" not in body:
                    continue
                # **白名单**，不是黑名单：按名字匹配的黑名单会被别名绕过——
                # `from functools import lru_cache as _lc` + `@_lc(maxsize=1)`
                # 的 `ast.unparse` 是 `_lc(maxsize=1)`，不含 "lru_cache"
                # （v0.45.160 mutation M23 实测绕过）。所以改成
                # 「除这几个之外的装饰器一律报出来，由人判断」。
                ALLOWED = {"property", "staticmethod", "classmethod",
                           "overload", "abstractmethod"}
                offending = [d for d in n.decorator_list
                             if ast.unparse(d).split("(")[0].split(".")[-1] not in ALLOWED]
                if offending:
                    bad.append((str(rel), n.name, [ast.unparse(d) for d in offending]))
        return bad

    def test_detector_has_teeth(self, tmp_path, monkeypatch):
        """正向对照：造一个被 `@lru_cache` 修饰的解析器，检测器必须发现。"""
        import sys
        (tmp_path / "cached_offender.py").write_text(
            "from functools import lru_cache\n"
            "from hive_logger import PATHS\n"
            "@lru_cache(maxsize=1)\n"
            "def _db():\n    return PATHS.db\n", encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
        hits = {(f, n) for f, n, _ in self._cached_resolvers()}
        assert ("cached_offender.py", "_db") in hits, (
            "检测器没发现被 @lru_cache 修饰的解析器——它是恒真的")

    def test_no_unexpected_decorators_on_resolvers(self):
        bad = self._cached_resolvers()
        assert not bad, (
            "路径解析器带了非白名单装饰器（缓存类会让它首次调用后冻住）：\n"
            + "\n".join(f"  - {f}:{n} {d}" for f, n, d in bad)
            + "\n\n缓存会让它**首次调用后冻住**：第一个用到它的测试决定整个 session "
              "的落点，逐测试隔离失效。要缓存就缓存**解析之后的内容**，别缓存路径本身。")
