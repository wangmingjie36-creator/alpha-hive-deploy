"""`tests/_ast_cache.py` 只省时间、不改语义 —— 对账测试（v0.45.524）。

缓存一旦给出与 `ast.parse` / `ast.get_source_segment` 不同的结果，接入它的全仓守卫
就会在**全绿**的同时看错东西。所以这里拿真实仓库文件逐个对账，并钉住三条边界：
文件改了不能拿到旧树、仓库外的合成树不进缓存、SyntaxError 不被缓存吞掉。
"""
from __future__ import annotations

import ast
import os

import pytest

from tests import _ast_cache as C
from tests._repo_files import own_python_files


def _dump(t):
    return ast.dump(t, include_attributes=True)


class TestParseCacheIsExact:
    def test_cached_tree_equals_fresh_parse_for_real_files(self):
        files, _ = own_python_files(C.REPO)
        # 挑最大的几个 + 本文件：越大的文件越能暴露切分 / 编码差异
        files = sorted(files, key=lambda p: p.stat().st_size)[-5:]
        assert files, "仓库里一个 .py 都没找到 —— 对账是空的"
        for p in files:
            src = C.read_text(p)
            assert _dump(C.parse(src)) == _dump(ast.parse(src)), p

    def test_repeat_call_is_a_hit_and_returns_same_object(self):
        src = C.read_text(C.REPO / "tests" / "_ast_cache.py")
        a = C.parse(src)
        before = C.stats()["hit"]
        assert C.parse(src) is a
        assert C.stats()["hit"] == before + 1

    def test_changed_file_is_a_miss_not_a_stale_tree(self, monkeypatch):
        """同一路径、内容变了（同样长度、同一 mtime 也一样）⇒ 必须拿到新树。

        模拟方式：让 `Path.read_text` 对同一路径先后返回两份等长内容（不真改仓库文件）。
        mtime/size 键在这里会撞，全文键不会。"""
        from pathlib import Path
        p = C.REPO / "tests" / "_ast_cache.py"
        texts = iter(["x_changed_1 = 1\n", "y_changed_2 = 2\n"])
        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None, errors=None: next(texts))
        t1 = C.parse_file(p)
        t2 = C.parse_file(p)
        assert t1 is not t2
        assert t2.body[0].targets[0].id == "y_changed_2"

    def test_text_edited_after_read_bypasses(self):
        """读进来再改过的串（变异样本的常见做法）不是登记过的全文 ⇒ 直通。"""
        src = C.read_text(C.REPO / "tests" / "_ast_cache.py")
        mutated = src + "\nzz_mutant = 1\n"
        b0 = C.stats()["bypass"]
        C.parse(mutated)
        assert C.stats()["bypass"] == b0 + 1

    def test_paths_outside_repo_bypass_the_cache(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("a = 1\n", encoding="utf-8")
        n0 = C.stats()["entries"]
        t1 = C.parse_file(f)
        t2 = C.parse_file(f)
        assert t1 is not t2, "仓库外的合成树进了缓存"
        assert C.stats()["entries"] == n0

    def test_unregistered_string_bypasses_the_cache(self):
        n0 = C.stats()["entries"]
        assert C.parse("a = 1\n") is not C.parse("a = 1\n")
        assert C.stats()["entries"] == n0

    def test_syntax_error_is_raised_and_not_cached(self, monkeypatch):
        from pathlib import Path
        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None, errors=None: "def (:\n")
        n0 = C.stats()["entries"]
        for _ in range(2):
            with pytest.raises(SyntaxError):
                C.parse_file(C.REPO / "tests" / "_ast_cache.py")
        assert C.stats()["entries"] == n0

    def test_decode_error_still_raises(self, tmp_path):
        f = tmp_path / "big5.py"
        f.write_bytes("# 中文\n".encode("big5"))
        with pytest.raises(UnicodeDecodeError):
            C.parse_file(f)


class TestSourceSegmentsMatchesStdlib:
    def _check(self, src):
        seg = C.SourceSegments(src)
        tree = ast.parse(src)
        n = 0
        for node in ast.walk(tree):
            assert seg.get(node) == ast.get_source_segment(src, node), (
                type(node).__name__, getattr(node, "lineno", None))
            n += 1
        return n

    def test_every_node_of_real_files(self):
        """真实文件的**每个**节点逐字节对账（含多字节中文：col_offset 是 UTF-8 字节偏移）。"""
        here = os.path.dirname(__file__)
        n = 0
        for name in ("test_no_invisible_prod_data_skips.py", "_ast_cache.py",
                     "_repo_files.py"):
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                n += self._check(fh.read())
        assert n > 1000, f"只对账了 {n} 个节点 —— 样本太小，证明不了什么"

    def test_crlf_cr_and_form_feed(self):
        src = "a = 1\r\nif a:\r    b = (1,\n  2)\n\x0cc = '中文'  \n"
        assert self._check(src) > 5

    def test_nodes_without_location(self):
        seg = C.SourceSegments("x = 1\n")
        assert seg.get(ast.Load()) is None
        node = ast.parse("x = 1\n").body[0]
        node.end_lineno = None
        assert seg.get(node) is None
