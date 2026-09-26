"""v0.45.334 断线守卫：`GexRegimeModifier` 的调整值不许再进 rule_score / final_score。

背景：GEX 此前经**三条**通道进评分 —— ① `RegimeWeightAdjuster` 按三值 regime 偏移五维权重；
② `GexRegimeModifier` 在方向投票之后把 ±0.8 直接加进 rule_score；③ OracleBee `options_score` 里的
`gex_signal`（`options_analyzer.py`：主链 `gamma_exposure` 为负得 2.0、否则 1.0，经 odds 维进加权分）。
v0.45.197 的世代边界只记了 ①。v0.45.334 **只断开 ②**，① ③ 照旧（③ 本文件不守 —— 夹具里 Oracle
直接给分，不经 `options_analyzer`）：`QueenDistiller` 步骤 4.5 仍计算、落盘为诊断值（`gex_regime_mod.applied=False`），
不再施加。理由与幅度见 `queen_distiller.py` 步骤 4.5 注释与 `ic_rerun_readiness._COHORT_HISTORY`
的 v0.45.334 条。

这里守三件事，照 `tests/test_zero_weight_invariant.py`（v0.45.176 断开 adapted_weights 旁路）的写法：

1. **行为**：夹具让五维分全相等 ⇒ 权重怎么偏移都动不了加权分；于是任何政体下 rule_score /
   final_score 都必须等于 regime=unknown 的基线。同一夹具上 `dimension_weights` 必须**随政体变**
   （① 仍接着 —— 断的是 ②，不是把 GEX 整个拔了）。自证：同一夹具上 `compute()` 真的给出非零、
   且足以改分的调整值（否则「分数没变」是因为本来就是 0）。
2. **标记**：新记录必须带 `applied=False`（含 compute 抛异常那条路）—— 共振检验的 replay、
   深度报告徽章、`ic_rerun_readiness` 的边界判别器都靠「缺键 ⇒ 旧记录」区分两代。
3. **静态**：生产代码（跳过 tests/、experiments/）里 `gex_adj*` 不得出现在 `+`/`-`/`+=`/`-=` 里。
   枚举驱动、带「确实扫到东西」自证与 tmp 树病灶夹具。

⚠️ 本文件不用 skip / 模块级 pytestmark：全部合成数据、零外部依赖。
每条断言旁注明能让它变红的变异。
"""
from __future__ import annotations

import ast
import copy
import importlib
from pathlib import Path

import pytest

from gex_regime import GexRegimeModifier
from pheromone_board import PheromoneBoard, PheromoneEntry
from swarm_agents.queen_distiller import QueenDistiller
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

#: 注入权重：五维全非零、**故意不等于**真 config（真 config 里 signal/risk_adj 为 0），
#: 好让「夹具没接上」看得见；全非零也让政体偏移在五维上都有地方落。
_CFG_INJECTED = {"signal": 0.2, "catalyst": 0.2, "sentiment": 0.2, "odds": 0.2, "risk_adj": 0.2}


def _pin_config(monkeypatch, weights=None):
    """同 `tests/test_zero_weight_invariant.py::_pin_config`：钉住 EVALUATION_WEIGHTS 并让它活过
    `QueenDistiller.__init__` 里的 `importlib.reload(config)`（reload 会冲掉 monkeypatch）。"""
    import config as _cfg
    monkeypatch.setattr(importlib, "reload", lambda m: m)
    monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS", dict(weights or _CFG_INJECTED), raising=False)


_DIMS = {"ScoutBeeNova": "signal", "OracleBeeEcho": "odds", "BuzzBeeWhisper": "sentiment",
         "ChronosBeeHorizon": "catalyst", "GuardBeeSentinel": "risk_adj",
         "RivalBeeVanguard": "ml_auxiliary", "CodeExecutorAgent": "technical",
         "BearBeeContrarian": "contrarian"}
_VOTERS = ("ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon")
#: 五个核心维度同分 ⇒ 加权分与权重无关。6.3 离 [0,10] 的边足够远，±0.8 不会被夹掉。
_CORE_SCORE = 6.3


def _agent_results(direction):
    out = []
    for agent, dim in _DIMS.items():
        d = direction if agent in _VOTERS else "neutral"
        # Rival 取 5.0 ⇒ ml_adjustment = 0（不去碰 ML 信任度那条读盘路径的数值）
        score = 5.0 if agent in ("RivalBeeVanguard", "CodeExecutorAgent", "BearBeeContrarian") else _CORE_SCORE
        details = {}
        if agent == "GuardBeeSentinel":
            details = {"macro_regime": "neutral"}       # 宏观政体钉成中性：只让 GEX 政体偏移权重
        elif agent == "OracleBeeEcho":
            details = {"iv_rank": None}                 # 不触发 IV 档的权重偏移
        out.append({"source": agent, "dimension": dim, "direction": d, "score": score,
                    "confidence": 0.7, "discovery": f"{agent} 合成", "data_quality": {"x": "real"},
                    "details": details})
    return out


def _dealer_gex(regime, flip_pct=None, can_flip=False):
    if regime == "unknown":
        return {"regime": "unknown"}          # 非空 ⇒ distill 不会去现场算 DealerGEX（不出网）
    return {"regime": regime, "stock_price": 100.0, "gex_flip": 100.0 * (1 + flip_pct / 100.0),
            "total_gex": 1.0 if regime == "positive_gex" else -1.0,
            "vanna_stress": {"can_flip_gex": can_flip}}


def _distill(direction, gex):
    results = _agent_results(direction)
    board = PheromoneBoard()
    for r in results:
        board.publish(PheromoneEntry(agent_id=r["source"], ticker="SYN", discovery=r["discovery"],
                                     source="test", self_score=float(r["score"]),
                                     direction=r["direction"], details={}))
    q = QueenDistiller(board, enable_llm=False, ml_model=None)
    assert q.DIMENSION_WEIGHTS == pytest.approx(_CFG_INJECTED), "夹具没接上：读到的是真 config"
    return q.distill("SYN", copy.deepcopy(results), dealer_gex=copy.deepcopy(gex))


_REGIMES = ("positive_gex", "negative_gex")
_FLIPS = (1.0, 4.0, 10.0)
_CAN_FLIP = (True, False)
_DIRECTIONS = ("bullish", "bearish", "neutral")


def _clamp_add(score, g):
    return round(max(0.0, min(10.0, score + g)), 2)


class TestGexModifierDoesNotMoveScores:

    @pytest.mark.parametrize("direction", _DIRECTIONS)
    def test_scores_equal_unknown_baseline_under_every_regime(self, monkeypatch, direction):
        """任何政体 × flip 距离 × vanna 可翻转，rule_score / final_score / 最终方向都等于 unknown 基线。

        变红的变异：把 `queen_distiller` 步骤 4.5 的 `rule_score += _gex_adj`（或任何把
        `gex_adjustment` 加回 rule_score 的写法）接回去 —— 本条会报出全部「咬得到」的组合；
        或在步骤 5 之后按调整值改 `final_direction`（如负调整把看多压成中性）—— 分数全不动，
        只有 `direction` 那一项红。
        """
        _pin_config(monkeypatch)
        base = _distill(direction, _dealer_gex("unknown"))
        assert base["rule_direction"] == direction, (
            f"前提：夹具投出的方向应是 {direction}，实得 {base['rule_direction']} —— 枚举没覆盖到该方向")
        assert 0.8 < base["rule_score"] < 9.2, "前提：基线离 [0,10] 的边够远，±0.8 不会被夹掉"

        mismatches, biting, signs = [], 0, set()
        for regime in _REGIMES:
            for flip in _FLIPS:
                for can_flip in _CAN_FLIP:
                    gex = _dealer_gex(regime, flip, can_flip)
                    out = _distill(direction, gex)
                    mod = out["gex_regime_mod"]
                    g = GexRegimeModifier().compute(gex, direction=direction)["gex_adjustment"]
                    # 诊断值照算照记（审计轨迹），且与 compute 同值 —— 断的是「加」，不是「算」
                    assert mod["gex_regime"] == regime and mod["gex_adjustment"] == g, mod
                    assert mod["applied"] is False, mod
                    assert out["rule_direction"] == direction
                    # 加权分与权重无关（五维同分）：这是「分数不变」能归因到断线而非巧合的前提
                    assert out["base_score_before_resonance"] == pytest.approx(
                        base["base_score_before_resonance"], abs=1e-9)
                    if abs(g) > 0.01 and _clamp_add(base["rule_score"], g) != base["rule_score"]:
                        biting += 1
                        signs.add(g > 0)
                    for key in ("rule_score", "final_score"):
                        if out[key] != pytest.approx(base[key], abs=1e-9):
                            mismatches.append((regime, flip, can_flip, key, base[key], out[key], g))
                    # 最终方向也不许动：诊断值只落盘，不能借「负 GEX 抑制看多」之类的写法改方向
                    # （那不经 rule_score，上面两个键全绿也拦不住）
                    if out["direction"] != base["direction"]:
                        mismatches.append((regime, flip, can_flip, "direction",
                                           base["direction"], out["direction"], g))
        assert not mismatches, (
            f"GEX 政体改动了分数或方向（方向 {direction}）：{mismatches[:6]} —— "
            "GexRegimeModifier 的调整值又被接回了评分链（v0.45.334 已断开，见步骤 4.5 注释）")
        # 前提：基线的最终方向就是投出来的方向 —— 否则看多/看空被改成中性这类变异，
        # 在一个本来就是中性的基线上看不出来
        assert base["direction"] == direction, base["direction"]
        # 自证：同一夹具上旧 compute 真的给出足以改分的调整 —— 否则上面的「没变」恒真
        assert biting >= 8, f"只有 {biting} 个组合的调整值咬得到分数，夹具太弱"
        assert False in signs, "没有负向调整的组合 —— 负 GEX 分支没被覆盖"
        if direction == "bearish":
            assert True in signs, "看空 + 正 GEX 的 +0.1 分支没被覆盖"

    def test_regime_weight_adjuster_is_still_wired(self, monkeypatch):
        """断的是 ②（直接加减分），不是 ①（政体偏移权重）：`dimension_weights` 必须随 regime 变。

        变红的变异：把步骤 0 的 `_gex_regime_str` 恒写成 "unknown"，或整个跳过 RegimeWeightAdjuster
        （那是另一个决定，要另开世代边界）。
        """
        _pin_config(monkeypatch)
        w = {regime: _distill("neutral", _dealer_gex(regime, 4.0, False))["dimension_weights"]
             for regime in ("unknown",) + _REGIMES}
        assert w["unknown"] == pytest.approx(_CFG_INJECTED), "unknown 政体下不该有任何偏移"
        assert w["positive_gex"] != w["unknown"]
        assert w["negative_gex"] != w["unknown"]
        assert w["positive_gex"] != w["negative_gex"]


class TestAppliedMarkerIsAlwaysWritten:
    """`applied=False` 是新旧两代记录的唯一判别物：读它的有共振检验 replay、深度报告徽章、
    `ic_rerun_readiness` 的边界判别器。新记录缺这个键会被三处一起误认成「旧记录、当时施加过」。"""

    def test_normal_path_writes_marker_readable_by_boundary_evidence(self, monkeypatch):
        """写端 ↔ 读端契约：distill 的输出装进归档形状后，边界判别器认得出。

        变红的变异：删掉 `distill()` 里的 `_gex_mod_result["applied"] = False`。
        """
        import ic_rerun_readiness as rr
        _pin_config(monkeypatch)
        out = _distill("bullish", _dealer_gex("negative_gex", 1.0, True))
        assert out["gex_regime_mod"]["applied"] is False
        assert rr._marker_gex_modifier_not_applied({"swarm_results": out}) is True

    def test_marker_written_even_when_compute_raises(self, monkeypatch):
        """compute 抛异常 ⇒ 落盘的是步骤 0 的默认值，那条路也必须带标记。

        变红的变异：把 `_gex_mod_result["applied"] = False` 挪进步骤 4.5 的 try 里。
        """
        _pin_config(monkeypatch)

        def boom(self, *_a, **_k):
            raise RuntimeError("compute 故障（测试注入）")
        monkeypatch.setattr(GexRegimeModifier, "compute", boom)
        out = _distill("bullish", _dealer_gex("negative_gex", 1.0, True))
        mod = out["gex_regime_mod"]
        assert mod["gex_regime"] == "unknown" and mod["gex_adjustment"] == 0.0, "前提：走的是默认值那条路"
        assert mod["applied"] is False, mod


class TestDeepReportBadgeIsHonest:
    """`generate_deep_v2._render_regime_badge` 的「评分±x」：只在调整真的施加了时才印。"""

    @staticmethod
    def _ctx(**mod):
        return {"gex_regime_mod": {"gex_regime": "negative_gex", "gex_adjustment": -0.5,
                                   "flip_proximity_pct": 2.0, **mod},
                "regime_weights_description": "权重未调整（中性环境）"}

    def test_new_record_shows_no_score_delta(self):
        """变红的变异：删掉 `gex_mod.get("applied", True) is not False` 那个条件（徽章在说谎）。"""
        from generate_deep_v2 import _render_regime_badge
        html = _render_regime_badge(self._ctx(applied=False))
        assert "负GEX" in html, "前提：徽章本身照常渲染"
        assert "评分" not in html, html

    def test_legacy_record_still_shows_score_delta(self):
        """旧记录（缺 applied）当时确实施加了 ⇒ 历史报告的展示不变。

        变红的变异：把缺省值改成 `get("applied", False)`。
        """
        from generate_deep_v2 import _render_regime_badge
        assert "评分-0.50" in _render_regime_badge(self._ctx())


class TestNoGexAdjustmentArithmeticInProduction:
    """静态守卫：生产代码里 `gex_adj*` 不许进 `+`/`-`/`+=`/`-=`。

    行为测试只盯 `QueenDistiller.distill` 一条路；这条盯的是「任何生产文件里有人把这个诊断值
    加到某个分数上」。它抓不住换了名字的写法（`x = mod["gex_adjustment"]; s += x`）——
    那由上面的行为测试兜（对 distill 而言），其余生产文件由下面 `TestGexAdjustmentKeyReadersAreKnown`
    兜（键名是字符串常量，换变量名换不掉它）。三层各管一块，缺一不可。

    判定：`+`/`-` 的任一操作数子树里出现 ① 标识符 / 属性名含 `gex_adj`（`gex_adjustment`、
    `_gex_adj`、`gex_adj` …），或 ② 字符串常量恰为 `"gex_adjustment"`（`d["gex_adjustment"]`、
    `.get("gex_adjustment")`）。比较（`abs(g) > 0.01`）、格式化（f-string）不算。
    """

    #: 跳过的顶层目录：`tests/`、`experiments/` 是离线代码（共振检验的 replay 就是要
    #: 在旧记录上把 GEX 加回去 —— 那是它复现旧生产链的正当用途）。判据是「会不会进每日扫描」。
    _SKIP_TOP = {"tests", ".git", "experiments"}
    _TOKEN = "gex_adj"
    _KEY = "gex_adjustment"

    @classmethod
    def _mentions(cls, node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and cls._TOKEN in n.id:
                return True
            if isinstance(n, ast.Attribute) and cls._TOKEN in n.attr:
                return True
            if isinstance(n, ast.Constant) and n.value == cls._KEY:
                return True
        return False

    @classmethod
    def _scan(cls, root=None):
        """返回 (违规 [(相对路径, 行号)], 扫到的文件相对路径集合, {相对路径: 提及次数})。"""
        root = Path(root) if root is not None else REPO_ROOT
        hits, scanned, mentions = [], set(), {}
        for py in own_python_files(root)[0]:
            rel = py.relative_to(root)
            if rel.parts[0] in cls._SKIP_TOP:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            scanned.add(rel.as_posix())
            n_mention = 0
            for node in ast.walk(tree):
                if isinstance(node, (ast.Name, ast.Attribute, ast.Constant)) and cls._mentions(node):
                    n_mention += 1
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                    operands = (node.left, node.right)
                elif isinstance(node, ast.AugAssign) and isinstance(node.op, (ast.Add, ast.Sub)):
                    operands = (node.target, node.value)
                else:
                    continue
                if any(cls._mentions(op) for op in operands):
                    hits.append((rel.as_posix(), node.lineno))
            if n_mention:
                mentions[rel.as_posix()] = n_mention
        return sorted(set(hits)), scanned, mentions

    def test_enumeration_actually_found_something(self):
        """先自证探针有效：扫到了生产文件，且匹配器在生产代码里**认得出**这个 token。

        没有这条，下一条会在「一个文件都没扫到」或「匹配器写错了」时恒真地绿。
        变红的变异：把 `_TOKEN` 写成 `"gex_adjX"`，或把枚举换成一个空列表。
        """
        _hits, scanned, mentions = self._scan()
        assert len(scanned) > 50, f"只扫到 {len(scanned)} 个生产文件 —— 枚举坏了"
        assert "swarm_agents/queen_distiller.py" in scanned
        assert mentions.get("swarm_agents/queen_distiller.py", 0) > 0, (
            "queen_distiller 里明明有 `_gex_adj` / \"gex_adjustment\"（诊断日志），匹配器没认出来")

    def test_no_production_arithmetic_on_gex_adjustment(self):
        """变红的变异：在 `queen_distiller` 步骤 4.5 写回
        `rule_score = round(max(0.0, min(10.0, rule_score + _gex_adj)), 2)`（v0.45.333 及以前的写法）。
        """
        hits, _scanned, _m = self._scan()
        assert not hits, (
            f"生产代码把 GexRegimeModifier 的调整值用进了加减运算：{hits}。"
            "v0.45.334 起它只是诊断值（applied=False）；要接回评分先过前瞻检验并登记世代边界。")

    @staticmethod
    def _plant_pathology(root):
        """一棵带病灶的树：两处生产违规 + 三处应被排除的「同形但不算」。"""
        root.mkdir(parents=True, exist_ok=True)
        (root / "swarm_agents").mkdir()
        (root / "swarm_agents" / "queen_distiller.py").write_text(
            "def f(rule_score, _gex_mod_result):\n"
            "    _gex_adj = _gex_mod_result['gex_adjustment']\n"
            "    if abs(_gex_adj) > 0.01:\n"                       # 比较：不算
            "        rule_score += _gex_adj\n"                     # 违规 ①
            "    return rule_score\n", encoding="utf-8")
        (root / "scoring.py").write_text(
            "def g(score, mod):\n"
            "    label = f'{mod[\"gex_adjustment\"]:+.2f}'\n"      # 格式化：不算
            "    return round(score - mod.get('gex_adjustment', 0.0), 2), label\n",   # 违规 ②
            encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("s = 1.0 + mod['gex_adjustment']\n", encoding="utf-8")
        (root / "experiments").mkdir()
        (root / "experiments" / "replay.py").write_text("score = score + g_gex_adj\n", encoding="utf-8")
        nested = root / ".claude" / "worktrees" / "stale"
        nested.mkdir(parents=True)
        (nested / "queen_distiller.py").write_text("rule_score += _gex_adj\n", encoding="utf-8")

    def test_pathology_is_caught_and_exclusions_hold(self, tmp_path):
        """两处生产违规都抓到（Name 形与字符串键形），比较/格式化/tests/experiments/嵌套副本都不报。

        变红的变异：删掉 `_mentions` 里的 `ast.Constant` 分支（`scoring.py` 漏报）；
        或把 `_SKIP_TOP` 清空（tests/、experiments/ 误报）。
        """
        self._plant_pathology(tmp_path)
        hits, scanned, _m = self._scan(tmp_path)
        assert hits == [("scoring.py", 3), ("swarm_agents/queen_distiller.py", 4)], hits
        assert not any(s.startswith(".claude") for s in scanned), scanned

    def test_pathology_fixture_actually_bites(self, tmp_path):
        """反向自证：被排除的三处**确实**长着同形病灶 —— 否则上一条的「不报」恒真。"""
        self._plant_pathology(tmp_path)
        naive = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.py")
                       if "+ " in p.read_text(encoding="utf-8") or "+= " in p.read_text(encoding="utf-8"))
        assert naive == [".claude/worktrees/stale/queen_distiller.py", "experiments/replay.py",
                         "swarm_agents/queen_distiller.py", "tests/test_x.py"], naive


class TestGexAdjustmentKeyReadersAreKnown:
    """换名接回的静态兜底：生产代码里谁碰 `"gex_adjustment"` 这个键，名单钉死。

    上一类只认 `gex_adj*` 标识符 / 键进 `+`/`-`，抓不住
    `a = sr["gex_regime_mod"]["gex_adjustment"]; s = s + a`（运算里只剩 `a`）；行为测试只盯
    `QueenDistiller.distill` 一条路。这里补「任何生产文件里出现这个键」：键名是字符串常量，
    换变量名换不掉它。判定：`ast.Constant` 的值**恰为** `"gex_adjustment"`（docstring 里顺带提到
    不算 —— 那是整段文本）。跳过的目录同上一类（tests/、experiments/ 是离线代码，
    共振检验的 replay 就是要在旧记录上把它加回去）。

    名单**双向**钉（怕它变大，也怕它过期）：
      · 多出来 ⇒ 新读者：先回答「它是诊断展示，还是要进分」。前者写明用途加进名单；
        后者先过前瞻检验并登记世代边界（v0.45.334 断开的理由见 `queen_distiller` 步骤 4.5）。
      · 少了 ⇒ 名单里有人已不再读它：删掉那一项，别让名单替一个不存在的读者担保。
    名单内的文件本身不在这条的管辖内 —— `queen_distiller` 由行为测试兜，其余两处是生产者 / 展示。
    """

    #: 相对仓库根 → 为什么允许碰这个键
    ALLOWED = {
        "gex_regime.py": "生产者：`GexRegimeModifier.compute` / `_neutral_result` 的返回字典",
        "swarm_agents/queen_distiller.py": "步骤 0 的默认值 + 步骤 4.5 的诊断日志（applied=False 落盘）",
        "generate_deep_v2.py": "深度报告徽章：只在记录的 applied 不为 False 时印「评分±x」",
    }
    _KEY = "gex_adjustment"

    @classmethod
    def _scan(cls, root=None):
        """返回 ({相对路径: 该键出现次数}, 扫到的文件相对路径集合)。"""
        root = Path(root) if root is not None else REPO_ROOT
        found, scanned = {}, set()
        for py in own_python_files(root)[0]:
            rel = py.relative_to(root)
            if rel.parts[0] in TestNoGexAdjustmentArithmeticInProduction._SKIP_TOP:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            scanned.add(rel.as_posix())
            n = sum(1 for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and node.value == cls._KEY)
            if n:
                found[rel.as_posix()] = n
        return found, scanned

    def test_readers_match_the_allow_list(self):
        """变红的变异：在任一名单外的生产文件里写
        `a = sr["gex_regime_mod"]["gex_adjustment"]; s = s + a`（上一类的运算守卫对它全绿）。
        """
        found, scanned = self._scan()
        assert len(scanned) > 50, f"只扫到 {len(scanned)} 个生产文件 —— 枚举坏了"
        extra = sorted(set(found) - set(self.ALLOWED))
        stale = sorted(set(self.ALLOWED) - set(found))
        assert not extra, (
            f"名单外的生产文件读了 `gex_adjustment`：{ {k: found[k] for k in extra} }。"
            "v0.45.334 起它只是诊断值（applied=False）；是展示就写明用途加进 ALLOWED，"
            "要进分先过前瞻检验并登记世代边界。")
        assert not stale, f"名单里的文件已不再碰这个键：{stale} —— 删掉那一项"

    @staticmethod
    def _plant(root):
        """一棵带病灶的树：一处名单外的换名接回 + 五处应被排除的「同形但不算」。"""
        root.mkdir(parents=True, exist_ok=True)
        (root / "paper_portfolio.py").write_text(
            "def f(sr, s):\n"
            "    a = sr['gex_regime_mod']['gex_adjustment']\n"
            "    s = s + a\n"
            "    return s\n", encoding="utf-8")
        (root / "generate_deep_v2.py").write_text(            # 名单内
            "def badge(m):\n    return m.get('gex_adjustment', 0)\n", encoding="utf-8")
        (root / "notes.py").write_text(                        # docstring 顺带提到：不算
            '"""这里提到 gex_adjustment 只是文字。"""\n', encoding="utf-8")
        for sub in ("tests", "experiments"):
            (root / sub).mkdir()
            (root / sub / "x.py").write_text("s = 1.0 + m['gex_adjustment']\n", encoding="utf-8")
        nested = root / ".claude" / "worktrees" / "stale"
        nested.mkdir(parents=True)
        (nested / "paper_portfolio.py").write_text("a = m['gex_adjustment']\n", encoding="utf-8")

    def test_renamed_reconnection_is_caught_where_the_arithmetic_guard_is_blind(self, tmp_path):
        """病灶夹具：换名接回被本条抓到，而上一类的运算守卫**确实**抓不到（否则本类是冗余的）；
        名单内 / docstring / tests / experiments / 嵌套副本都不报。

        变红的变异：把 `node.value == cls._KEY` 改成 `cls._KEY in str(node.value)`（notes.py 误报）；
        或把 `_SKIP_TOP` 的引用换成空集合（tests/、experiments/ 误报）。
        """
        self._plant(tmp_path)
        found, scanned = self._scan(tmp_path)
        assert set(found) - set(self.ALLOWED) == {"paper_portfolio.py"}, found
        assert found.get("generate_deep_v2.py") == 1, "前提：名单内那份确实被扫到并认出"
        assert "notes.py" in scanned and "notes.py" not in found, found
        assert not any(s.startswith(".claude") for s in scanned), scanned
        blind, _s, _m = TestNoGexAdjustmentArithmeticInProduction._scan(tmp_path)
        assert "paper_portfolio.py" not in {h[0] for h in blind}, (
            "前提失效：运算守卫已能抓到换名接回，本类的夹具没在测它补的那一块")
