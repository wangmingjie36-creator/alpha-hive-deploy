"""data_quality 标签全覆盖：每个生产者能写出的标签都必须被 QueenDistiller 分类。

v0.45.314 新增。**这组测试存在的理由是「谁会红？」答不上来。**

`QueenDistiller` 顶上的契约注释早就写着「其他 (0.0)：未分类 — 表示遗漏了分类，
应视为 Bug」，但**没有任何东西在执行这句话**。结果：

  · `peer_read`（v0.45.151，RivalBee **成功**读到 ChronosBee 分数）
  · `quiet`（P1-1，BuzzBee：ApeWisdom 榜单正常、不在前 100 = 真实低热度）

两个**成功**标签被记 0 分 —— 比 API 挂掉的 `fallback`（0.7）还低。
网站「数据真实度」因此常年白扣约 5.3pp（09-22 实测 91.4% → 修后 96.7%），
且 `data_real_pct < 80` 会触发评分压缩，坏日子里这 5.3pp 能把标的推过线。
同期还有两个降级标签（`unreadable` / ChronosBee `failed`）同样未分类，
只是历史上恰好没出现过。

────────────────────────────────────────────────────────────────────────
做法：AST 枚举，不跑蜂
────────────────────────────────────────────────────────────────────────
跑真蜂要网络与板面，而且只能看到**这一次**走到的分支；标签问题恰恰藏在
**没走到**的分支里（`failed` 全史 0 次）。所以静态枚举每个生产者里能流进
`data_quality` 的全部字符串字面量：

  (a) `data_quality=<expr>` 关键字参数
  (b) 字典字面量里的 `"data_quality": <expr>` 键（`real_data_sources` 的写法）
  (c) `<expr>` 里的 Name 解析到同函数内的全部赋值与 `name[...] = ...` 下标赋值，
      并跟进「把它当位置参数传进去」的同模块函数（RivalBee 的 `_cat_source = ...`；
      BearBee 的 `data_sources["x"] = ...` 全在辅助方法里 —— 初版不跟进时，本文件
      其余测试**全绿而 BearBee 的标签一个都没扫到**，是 `SENTINELS` 的 `sec_api` 揪出来的）

IfExp 只取两个分支、**不取条件**（`"不可用" not in news_desc` 里的字符串不是标签）。
解析不了的表达式记为「不透明」并与 `EXPECTED_OPAQUE` **精确相等**比对 ——
新增一处不透明写法会红（逼人来看），删掉一处也会红（防白名单过期）。

静态枚举看不到运行期拼出来的标签，所以还有扫描期那道防线：
`_apply_triple_penalty` 遇未分类标签打 warning 并写入结果的 `dq_unclassified`。
"""
import ast
import logging
from pathlib import Path

from swarm_agents.queen_distiller import QueenDistiller

_ROOT = Path(__file__).resolve().parent.parent

# 生产者：所有会把 data_quality 交给 QueenDistiller 的模块。
# QueenDistiller 自己是消费者（它那几个 "data_quality" 键是汇总，不是标签），排除。
_PRODUCERS = sorted(
    p for p in (_ROOT / "swarm_agents").glob("*.py")
    if p.name not in ("queen_distiller.py", "__init__.py")
) + [_ROOT / "real_data_sources.py"]

# 能被枚举到、但**到不了** `_apply_triple_penalty` 的标签。
# `make_error_result` 的结果带 "error" 键，在 queen_distiller 的
# `valid_results = [r for r in cleaned_results if "error" not in r]` 被滤掉。
# `test_never_reaches_labels_are_really_filtered` 正面核对这个前提。
NEVER_REACHES = {"error", "N/A"}

# 解析不了、但已人工确认其取值被别处覆盖的表达式：(文件名, 行号无关的源码片段)。
# 用源码片段而非行号 —— 行号随无关改动漂移，会让本测试变成噪音。
EXPECTED_OPAQUE = {
    # ScoutBee 透传 real_data_sources 的聚合字典 —— 其键值由规则 (b) 在
    # real_data_sources.py 里直接枚举
    ("scout_bee.py", 'metrics.get("data_quality", {})'),
    # real_data_sources 聚合字典里引用各子取数函数的返回 —— 同上，规则 (b) 覆盖
    ("real_data_sources.py", 'st_data["data_quality"]'),
    ("real_data_sources.py", 'short_data["data_quality"]'),
}


def _parent_map(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_func(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _values_of(expr, func, src, fname, labels, opaque, _seen=None):
    """收集 expr 可能取到的全部字符串标签；解析不了的记入 opaque。"""
    _seen = set() if _seen is None else _seen
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, str):
            labels.add(expr.value)
        return
    if isinstance(expr, ast.IfExp):
        _values_of(expr.body, func, src, fname, labels, opaque, _seen)
        _values_of(expr.orelse, func, src, fname, labels, opaque, _seen)
        return
    if isinstance(expr, ast.BoolOp):
        for v in expr.values:
            _values_of(v, func, src, fname, labels, opaque, _seen)
        return
    if isinstance(expr, ast.Dict):
        for v in expr.values:
            _values_of(v, func, src, fname, labels, opaque, _seen)
        return
    if isinstance(expr, ast.Name) and func is not None:
        key = (id(func), expr.id)
        if key in _seen:
            return
        _seen.add(key)
        if _resolve_name(expr.id, func, src, fname, labels, opaque, _seen):
            return
    opaque.add((fname, ast.get_source_segment(src, expr)))


def _resolve_name(name, func, src, fname, labels, opaque, _seen):
    """name 在 func 内的全部赋值 + 下标赋值；并跟进把它当位置参数传进去的同模块函数
    （BearBee：`data_sources = {}` 在 analyze 里，`data_sources["x"] = ...` 全在
    `_check_*` 辅助方法里 —— 只看本函数会漏掉 BearBee 的全部标签）。"""
    found = False
    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                hit = (isinstance(t, ast.Name) and t.id == name) or (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name) and t.value.id == name)
                if hit and node.value is not None:
                    found = True
                    _values_of(node.value, func, src, fname, labels, opaque, _seen)
        elif isinstance(node, ast.Call):
            for i, a in enumerate(node.args):
                if not (isinstance(a, ast.Name) and a.id == name):
                    continue
                callee_name = (node.func.attr if isinstance(node.func, ast.Attribute)
                               else getattr(node.func, "id", None))
                for callee in _module_funcs(func, callee_name):
                    params = [p.arg for p in callee.args.args]
                    if params and params[0] in ("self", "cls"):
                        params = params[1:]
                    if i < len(params) and _resolve_name(
                            params[i], callee, src, fname, labels, opaque, _seen):
                        found = True
    return found


def _module_funcs(func, name):
    """与 func 同模块、名为 name 的函数定义（经 func 上挂的模块树查）。"""
    tree = getattr(func, "_module_tree", None)
    if tree is None or name is None:
        return []
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


def scan_labels(paths=_PRODUCERS, sources=None):
    """返回 (labels: {label: {文件名}}, opaque: {(文件名, 源码片段)})。

    `sources` 可传 {文件名: 源码} 直接扫合成源码（给守卫自证用）。
    """
    items = ([(p.name, p.read_text(encoding="utf-8")) for p in paths]
             if sources is None else list(sources.items()))
    labels, opaque = {}, set()
    for fname, src in items:
        tree = ast.parse(src)
        parents = _parent_map(tree)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n._module_tree = tree
        exprs = []
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "data_quality":
                exprs.append(node.value)
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "data_quality":
                        exprs.append(v)
        for e in exprs:
            found = set()
            _values_of(e, _enclosing_func(e, parents), src, fname, found, opaque)
            for lab in found:
                labels.setdefault(lab, set()).add(fname)
    return labels, opaque


def unclassified(labels, real=None, proxy=None):
    real = QueenDistiller.REAL_SOURCES if real is None else real
    proxy = QueenDistiller.PROXY_SOURCES if proxy is None else proxy
    return {lab: files for lab, files in labels.items()
            if lab not in real and lab not in proxy and lab not in NEVER_REACHES}


# ─────────────────────────── 不变式本身 ───────────────────────────

class TestEveryProducedLabelIsClassified:

    def test_no_unclassified_label(self):
        labels, _ = scan_labels()
        bad = unclassified(labels)
        assert not bad, (
            "以下 data_quality 标签没有登记进 QueenDistiller.REAL_SOURCES / "
            "PROXY_SOURCES，会被 `_apply_triple_penalty` 静默按 0 分计入 "
            f"data_real_pct（比降级的 0.7 还低）：{bad}")

    def test_opaque_sites_exactly_as_reviewed(self):
        """精确相等：多一处 = 新写法没人看过；少一处 = 白名单过期。"""
        _, opaque = scan_labels()
        assert opaque == EXPECTED_OPAQUE, (
            f"新增（需人工确认取值被覆盖后登记）：{opaque - EXPECTED_OPAQUE}；"
            f"已消失（从 EXPECTED_OPAQUE 删掉）：{EXPECTED_OPAQUE - opaque}")


# ─────────────────────── 扫描器真的接上了 ───────────────────────
# 「没扫出未分类标签」与「什么都没扫到」长得一模一样 —— 必须正面核对。

class TestScannerSeesTheProducers:

    # 每条都是生产代码里真实存在、且走不同解析路径的标签
    SENTINELS = {
        "peer_read": "rival_bee.py",        # Name → 同函数赋值（规则 c）
        "unreadable": "rival_bee.py",       # 同上，另一赋值
        "quiet": "buzz_bee.py",             # 嵌套 IfExp 的 orelse
        "failed": "chronos_bee.py",         # 嵌套 IfExp 的 body
        "sec_api": "bear_bee.py",           # name[...] = ... 下标赋值（规则 c）
        "proxy_social": "real_data_sources.py",  # "data_quality": 键（规则 b）
        "fallback_momentum": "rival_bee.py",
    }

    def test_sentinels_found_where_expected(self):
        labels, _ = scan_labels()
        missing = {lab: f for lab, f in self.SENTINELS.items()
                   if f not in labels.get(lab, set())}
        assert not missing, f"扫描器没找到这些已知标签（解析路径断了）：{missing}"

    def test_every_producer_contributes(self):
        labels, _ = scan_labels()
        contributing = set().union(*labels.values())
        expected = {"bear_bee.py", "buzz_bee.py", "chronos_bee.py", "guard_bee.py",
                    "oracle_bee.py", "rival_bee.py", "real_data_sources.py",
                    "utils.py"}
        assert expected <= contributing, f"没贡献任何标签的生产者：{expected - contributing}"

    def test_condition_strings_are_not_labels(self):
        """IfExp 的条件不是取值：buzz_bee 的 `"不可用" not in news_desc` 不能被当成标签。"""
        labels, _ = scan_labels()
        assert "不可用" not in labels


# ─────────────────────── 守卫有牙（变异自证） ───────────────────────

class TestGuardHasTeeth:

    def test_removing_a_registered_label_turns_red(self):
        labels, _ = scan_labels()
        real = QueenDistiller.REAL_SOURCES - {"quiet"}
        assert "quiet" in unclassified(labels, real=real)

    def test_new_literal_label_in_synthetic_producer_is_caught(self):
        src = (
            "def analyze(ok):\n"
            "    mode = 'brand_new_label' if ok else 'fallback'\n"
            "    return AgentResult(data_quality={'x': mode, 'y': 'real'})\n"
        )
        labels, opaque = scan_labels(sources={"fake_bee.py": src})
        assert set(unclassified(labels)) == {"brand_new_label"}
        assert not opaque

    def test_unresolvable_expression_is_reported_as_opaque(self):
        src = "def analyze(r):\n    return AgentResult(data_quality={'x': r.label()})\n"
        _, opaque = scan_labels(sources={"fake_bee.py": src})
        assert opaque == {("fake_bee.py", "r.label()")}


# ─────────────────────── NEVER_REACHES 的前提 ───────────────────────

def test_never_reaches_labels_are_really_filtered():
    """NEVER_REACHES 的豁免只在「带 error 键的结果会被滤掉」时成立 —— 正面核对。"""
    from swarm_agents.utils import make_error_result
    r = make_error_result("ScoutBeeNova", "signal", "boom")
    assert set(r["data_quality"].values()) <= NEVER_REACHES
    assert "error" in r and r["error"]
    src = (_ROOT / "swarm_agents" / "queen_distiller.py").read_text(encoding="utf-8")
    assert 'if "error" not in r]' in src, "valid_results 的 error 过滤没了 ⇒ NEVER_REACHES 豁免失效"


# ─────────────────────── 扫描期观测点 ───────────────────────

def _penalty(dq_by_agent):
    qd = QueenDistiller.__new__(QueenDistiller)
    results = [{"source": ag, "dimension": "x", "score": 5.0, "direction": "neutral",
                "data_quality": dq} for ag, dq in dq_by_agent.items()]
    return qd._apply_triple_penalty("TEST", 5.0, results)


class TestRuntimeObservationPoint:

    def test_success_labels_now_score_full(self):
        tp = _penalty({"RivalBeeVanguard": {"catalyst_quality": "peer_read"},
                       "BuzzBeeWhisper": {"reddit": "quiet"}})
        assert tp["data_real_pct"] == 100.0
        assert tp["dq_unclassified"] == []

    def test_unknown_label_is_surfaced_not_silent(self, caplog):
        with caplog.at_level(logging.WARNING):
            tp = _penalty({"FakeBee": {"chan": "totally_new", "ok": "real"}})
        assert tp["data_real_pct"] == 50.0          # 仍按契约记 0 分，不改评分
        assert tp["dq_unclassified"] == ["FakeBee.chan='totally_new'"]
        assert "未分类标签" in caplog.text
