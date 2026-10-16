"""全仓 AST 守卫共用的会话级解析缓存 —— **只省时间，不改语义**（v0.45.524）。

为什么要有它
────────────
全仓 AST 守卫（`test_paths_not_frozen_at_import` / `test_gex_oracle_bear_neutralized` /
`test_sell_strike_integration` …）每条测试各自把全仓 .py 读一遍、`ast.parse` 一遍。
全仓一次解析本机约 2–3 秒，`_scan()` 一个文件里就被调了 18 次 —— 这类守卫的耗时
随**代码量**而不是测试条数增长，代码越写越多，整套越跑越慢。

键是什么、为什么这样就不会给出陈旧的树
──────────────────────────────────────
键 = **源码全文**，不是 mtime/size。每次照旧从盘上读（读盘全仓 0.03 秒，便宜；
贵的只有解析），读用 `read_text()`、解析用 `parse()`（或二合一 `parse_file()`）。
同一份全文解析出的树必然相同，所以全文键**不可能**给出陈旧的树。

* 文件在会话里被改过（哪怕同一秒、同样大小）⇒ 全文不同 ⇒ 未命中、重新解析。
  mtime 键在 Linux 粗粒度时钟下会撞（MEMORY `alpha-hive-test-guard-failures`：
  Linux mtime 粗于 time.time），全文键没有这个窗口。
* 只缓存经 `read_text()` 从**仓库根之内**读到的全文。夹具在 `tmp_path` 里搭的合成树、
  有牙测试手写的源码串、变异样本一律**直通** `ast.parse`，不进缓存、不读缓存 ——
  有牙测试测的是守卫本身，不该经过一层它不认识的东西。这也让各守卫里
  「吃源码串」的辅助函数不用改签名：生产文件走缓存、手写样本直通，自动分流。
* `SyntaxError` 不缓存，原样抛给调用方（各守卫自己的「记账再跳过」逻辑不变）。

⚠️ 返回的树是**共享的**：同一会话里别的守卫会拿到同一个对象。**只读**——
不许 `NodeTransformer`、不许给节点挂属性、不许 `fix_missing_locations`。
迄今接入的守卫都只 `ast.walk` / `ast.unparse` / 读属性。

`SourceSegments`
────────────────
`ast.get_source_segment(source, node)` 每调一次都把**整份源码**重新按行切一遍
（3.11 的 `_splitlines_no_ff` 是逐字符 Python 循环）。对每个函数调一次就是平方级：
`test_no_invisible_prod_data_skips` 单条 27.7 秒里 96% 花在这里。
`SourceSegments` 切一次、按节点取段，结果与 `ast.get_source_segment` 逐字节相同
（`tests/test_ast_cache.py` 对全仓真实文件的每个节点对账）。
"""
from __future__ import annotations

import ast
from pathlib import Path

#: 仓库根（本文件在 tests/ 下）。**代码锚点**，用 `__file__` 是对的——缓存只认
#: 「本仓源码」，要的就是代码在哪（CLAUDE.md「这个路径指向代码还是数据」）。
REPO = Path(__file__).resolve().parent.parent

_TREES: dict[str, ast.Module] = {}          # 源码全文 → 树
_REPO_TEXTS: set[str] = set()               # 经 read_text() 从仓库内文件读到的全文
_STATS = {"hit": 0, "miss": 0, "bypass": 0}


def _inside_repo(path) -> bool:
    try:
        Path(path).resolve().relative_to(REPO)
    except (OSError, ValueError):
        return False
    return True


def read_text(path, encoding: str = "utf-8", errors: str | None = None) -> str:
    """`Path(path).read_text(encoding=..., errors=...)`，异常原样抛（UnicodeDecodeError / OSError）。

    仓库根之内的文件，读到的全文会被**登记**：之后 `parse()` 拿到这份全文才走缓存。
    仓库外（tmp 合成树）不登记 ⇒ 它们的解析永远直通 `ast.parse`。
    """
    text = Path(path).read_text(encoding=encoding, errors=errors)
    if _inside_repo(path):
        _REPO_TEXTS.add(text)
    return text


def parse(source: str) -> ast.Module:
    """`ast.parse(source)` 的缓存版。只缓存 `read_text()` 从仓库内文件读到的全文；
    其余（有牙测试手写的样本、夹具写的 tmp 文件、读完又改过的串）一律直通。"""
    if source not in _REPO_TEXTS:
        _STATS["bypass"] += 1
        return ast.parse(source)
    tree = _TREES.get(source)
    if tree is None:
        _STATS["miss"] += 1
        tree = ast.parse(source)          # SyntaxError 原样抛出，不缓存
        _TREES[source] = tree
    else:
        _STATS["hit"] += 1
    return tree


def parse_file(path, encoding: str = "utf-8", errors: str | None = None) -> ast.Module:
    """`ast.parse(Path(path).read_text(encoding=..., errors=...))` 的缓存版，异常口径相同。"""
    return parse(read_text(path, encoding, errors))


def stats() -> dict:
    return dict(_STATS, entries=len(_TREES))


class SourceSegments:
    """对同一份源码反复取 `ast.get_source_segment(source, node)`，只切一次行。

    只实现 `padded=False`（接入的调用方都只用缺省）。
    """

    def __init__(self, source: str):
        self.source = source
        # 用标准库自己的切行函数：切法与 get_source_segment 逐字符一致
        #（\r\n 合一、form feed 不算换行）。它是私有名，哪天改名了
        # tests/test_ast_cache.py 的对账测试会红。
        self._lines = ast._splitlines_no_ff(source)

    def get(self, node):
        try:
            if node.end_lineno is None or node.end_col_offset is None:
                return None
            lineno = node.lineno - 1
            end_lineno = node.end_lineno - 1
            col_offset = node.col_offset
            end_col_offset = node.end_col_offset
        except AttributeError:
            return None
        lines = self._lines
        if end_lineno == lineno:
            return lines[lineno].encode()[col_offset:end_col_offset].decode()
        first = lines[lineno].encode()[col_offset:].decode()
        last = lines[end_lineno].encode()[:end_col_offset].decode()
        return "".join([first, *lines[lineno + 1:end_lineno], last])
