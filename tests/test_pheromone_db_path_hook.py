"""`feedback_loop.PHEROMONE_DB_PATH` 是覆盖钩子，消费方必须调 `_db_path()`（v0.45.171）

2026-09-08 定时任务**零产出、网站未更新**，Step 2 启动 2 秒即崩：

    advanced_analyzer.py:559  self.db_path = Path(db_path)
    TypeError: expected str, bytes or os.PathLike object, not NoneType

根因链：v0.45.160 把 `PHEROMONE_DB_PATH` 从 `Path(__file__).parent/"pheromone.db"`
改成**默认 `None` 的覆盖钩子** + 新增 `_db_path()` 运行时解析。改得对，但
**`_db_path()` 当时零调用者**——三处消费方（含 `feedback_loop` 自己的缺省分支）
仍在读原始常量，于是生产上全部拿到 `None`。

为什么整套测试是绿的：`tests/conftest.py::_isolate_feedback_loop_close_t7_db` 是
**autouse**，每个测试都把这个钩子设成一个真实 tmp 路径 ⇒ **没有任何一条测试见过
生产值 `None`**。夹具做的正是它该做的隔离，代价是把默认分支变成了不可达代码。
⇒ 判据：**给常量加「默认 None + 运行时解析」时，必须有一条测试把它设回 None**，
否则 autouse 夹具会替你把这条路径永久遮住。

这里守四条：
  1. 钩子取生产值 `None` 时，六个消费方都能解析且不抛（解析到 `PATHS.db`）
  2. 钩子非 None 时**仍然优先**（conftest 与 weekly_optimizer 的 monkeypatch 依赖它）
  3. 生产代码不许再读原始钩子（静态）
  4. `_db_path()` 不许是死代码（它当初就是死的）
本文件不出网、不碰生产库：`_isolate_env` 已把 `PATHS.db` 指进沙箱。
"""

import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedback_loop  # noqa: E402
from hive_logger import PATHS  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def production_hook(monkeypatch):
    """把钩子设回**生产真实值** `None`——即撤销 autouse 夹具的隔离赋值。

    安全性不靠夹具留下的那个 tmp 路径，靠 `_isolate_env`：它把
    `ALPHA_HIVE_HOME` 指向沙箱，于是 `PATHS.db` 本身就在 tmp 里。
    """
    monkeypatch.setattr(feedback_loop, "PHEROMONE_DB_PATH", None)
    return Path(PATHS.db)


# ───────────────────────────────────────────── 1. 生产值下六个消费方都活着
class TestProductionDefaultResolves:
    def test_sandbox_precondition(self, production_hook):
        """前置条件：解析结果必须在沙箱里——否则下面几条会去读真生产库。"""
        assert production_hook.is_absolute()
        assert REPO not in production_hook.parents, (
            f"解析到了仓库内 {production_hook}，本测试会碰生产库，先修隔离再跑")

    def test_db_path_resolver(self, production_hook):
        assert feedback_loop._db_path() == production_hook

    def test_load_close_t7_map(self, production_hook):
        rows, status = feedback_loop._load_close_t7_map()
        assert rows == {} and status == "missing"      # 沙箱里没有库 ⇒ 正常降级

    def test_backtest_analyzer_clean_t7(self, production_hook):
        # v0.45.87 的五个消费者全走这里；崩在 None.exists()（AttributeError 不是
        # OSError，那个 except 接不住）
        feedback_loop.BacktestAnalyzer(clean_t7=True)

    def test_historical_analyzer(self, production_hook):
        from advanced_analyzer import HistoricalAnalyzer
        assert HistoricalAnalyzer().db_path == production_hook

    def test_advanced_analyzer_construction(self, production_hook):
        """09-08 真正崩的那一步：Step 2 构造 AdvancedAnalyzer 就抛。"""
        from advanced_analyzer import AdvancedAnalyzer
        assert AdvancedAnalyzer().history.db_path == production_hook

    def test_probability_scorecard(self, production_hook):
        import probability_scorecard
        rows, status = probability_scorecard.load_outcomes()
        assert rows == [] and status == "missing"


# ───────────────────────────────────────────── 2. 覆盖钩子仍然优先
class TestOverrideStillWins:
    def test_hook_beats_paths_db(self, tmp_path, monkeypatch):
        override = tmp_path / "override.db"
        monkeypatch.setattr(feedback_loop, "PHEROMONE_DB_PATH", override)
        assert feedback_loop._db_path() == override
        from advanced_analyzer import HistoricalAnalyzer
        assert HistoricalAnalyzer().db_path == override
        import probability_scorecard
        assert probability_scorecard.load_outcomes(db_path=override)[1] == "missing"

    def test_explicit_argument_beats_both(self, tmp_path, monkeypatch):
        monkeypatch.setattr(feedback_loop, "PHEROMONE_DB_PATH", tmp_path / "hook.db")
        explicit = tmp_path / "explicit.db"
        from advanced_analyzer import HistoricalAnalyzer
        assert HistoricalAnalyzer(explicit).db_path == explicit


# ───────────────────────────────────────────── 3+4. 静态守卫
def _production_py_files():
    out = []
    for p in sorted(REPO.glob("*.py")) + sorted(REPO.glob("swarm_agents/*.py")):
        if p.name.startswith("test_"):
            continue
        out.append(p)
    return out


class TestNoRawHookReads:
    def test_no_module_imports_the_raw_hook(self):
        """`from feedback_loop import PHEROMONE_DB_PATH` 拿到的是钩子不是路径。

        ⚠️ 不能按裸名字 grep：`weekly_optimizer` 有**自己的**同名常量
        （`ALPHAHIVE_DIR / "pheromone.db"`，是真路径、显式传给 feedback_loop），
        按名字数会把它误报。只认「从 feedback_loop 取这个名字」这一种形态。
        """
        offenders = []
        for p in _production_py_files():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "feedback_loop":
                    for a in node.names:
                        if a.name == "PHEROMONE_DB_PATH":
                            offenders.append(f"{p.name}:{node.lineno}")
                elif (isinstance(node, ast.Attribute)
                      and node.attr == "PHEROMONE_DB_PATH"
                      and isinstance(node.value, ast.Name)
                      and node.value.id in ("feedback_loop", "fl", "_fl")):
                    offenders.append(f"{p.name}:{node.lineno}")
        assert not offenders, (
            f"这些地方读的是覆盖钩子（生产上是 None），应改调 feedback_loop._db_path()：{offenders}")

    def test_feedback_loop_own_default_uses_the_resolver(self):
        """模块自己的缺省分支——v0.45.160 漏掉的正是这一处。"""
        src = (REPO / "feedback_loop.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_load_close_t7_map")
        assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name) and t.id == "db_path"]
        assert assigns, "找不到 db_path 的缺省赋值，锚点失效"
        for a in assigns:
            assert not (isinstance(a.value, ast.Name) and a.value.id == "PHEROMONE_DB_PATH"), \
                "缺省分支又读回了原始钩子"

    def test_resolver_is_not_dead_code(self):
        """`_db_path()` 当初写出来就零调用者——死解析器是这次事故的必要条件之一。"""
        callers = set()
        for p in _production_py_files():
            src = p.read_text(encoding="utf-8")
            if p.name != "feedback_loop.py" and "feedback_loop" not in src:
                continue
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = (f.id if isinstance(f, ast.Name)
                        else f.attr if isinstance(f, ast.Attribute) else None)
                if name in ("_db_path", "_fl_db_path"):
                    callers.add(p.name)
        assert "feedback_loop.py" in callers, "feedback_loop 自己没调 _db_path()"
        assert len(callers) >= 3, f"解析器调用者过少（{sorted(callers)}）——消费方可能又在读常量"
