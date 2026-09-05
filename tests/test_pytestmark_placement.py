"""`pytestmark` 不得插在类 docstring 之前（v0.45.129 回归）

v0.45.127 给 `TestMLPredictionService` 加类级 `pytestmark` 时插到了
docstring **上面**：

    class TestMLPredictionService:
        pytestmark = pytest.mark.timeout(300)

        \"\"\"测试 MLPredictionService\"\"\"      ← 不再是第一条语句

三引号字符串一旦不是类体的第一条语句，就从 docstring 退化成一个
**求值即丢弃的表达式**，`__doc__` 静默变成 `None`。

为什么单测/静态检查都抓不到它：
  · 语法完全合法
  · 不改变任何运行时行为 ⇒ 测试全绿、mutation check 也无从下手
  · `ruff`（本仓 select=E,F,W）不报这一类

发现它靠的是**与兄弟类对照**——同文件另外 8 个类 `__doc__` 都在，只有它是 None。
本测试把这条对照固化成守卫：全仓测试类里，凡是**源码中写了 docstring 字面量**
的类，`__doc__` 必须非 None。
"""

import ast
import pathlib

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _classes_with_source_docstring(path: pathlib.Path):
    """返回 (类名, 是否为合法 docstring)。

    判据取自 AST 而不是 `__doc__`：要区分「本来就没写 docstring」
    与「写了但被挤掉」，只看 `__doc__` 是分不出来的。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # 类体里所有「裸字符串表达式」——写了 docstring 的意图
        str_exprs = [i for i, st in enumerate(node.body)
                     if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                     and isinstance(st.value.value, str)]
        if not str_exprs:
            continue                      # 本来就没写，不管
        yield node.name, (str_exprs[0] == 0), path


ALL = [c for p in sorted(TESTS_DIR.glob("test_*.py"))
       for c in _classes_with_source_docstring(p)]


class TestDocstringNotDisplaced:
    def test_fixture_is_not_empty(self):
        """护栏：夹具为空时下面那条会恒真。"""
        assert len(ALL) > 50, f"只扫到 {len(ALL)} 个带 docstring 的测试类，扫描逻辑可能坏了"

    @pytest.mark.parametrize("name,is_first,path",
                             ALL, ids=[f"{p.stem}::{n}" for n, _, p in ALL])
    def test_docstring_is_first_statement(self, name, is_first, path):
        assert is_first, (
            f"{path.name}::{name} 写了 docstring 字面量，但它不是类体第一条语句"
            f"（多半是 `pytestmark` / 类属性插到了它前面）⇒ __doc__ 会静默变成 None"
        )


class TestGuardItselfWorks:
    """反向自证：判据必须能判出坏形态，否则它可能只是恒真。"""

    def test_detects_displaced_docstring(self, tmp_path):
        f = tmp_path / "test_bad.py"
        f.write_text('class TestX:\n    pytestmark = 1\n\n    """doc"""\n', encoding="utf-8")
        got = list(_classes_with_source_docstring(f))
        assert got and got[0][1] is False, "坏形态没被判出来"

    def test_accepts_correct_order(self, tmp_path):
        f = tmp_path / "test_good.py"
        f.write_text('class TestX:\n    """doc"""\n\n    pytestmark = 1\n', encoding="utf-8")
        got = list(_classes_with_source_docstring(f))
        assert got and got[0][1] is True, "正确形态被误判"

    def test_ignores_class_without_docstring(self, tmp_path):
        f = tmp_path / "test_none.py"
        f.write_text('class TestX:\n    pytestmark = 1\n', encoding="utf-8")
        assert not list(_classes_with_source_docstring(f)), "没写 docstring 的类不该被管"
