"""v0.45.677 退役守卫：BullVeto（BearBee 分数 ≥ 阈值 ⇒ 看多降成中性）不许回来。

背景（全文见 CHANGELOG v0.45.677）：
- v0.37.0 上线，读的是 BearBee **反转后的吸引力分**（score = 10 − bear_score），`score ≥ 7` 恰恰等于
  「BearBee 确认看多」⇒ 上线以来 35 次触发全是反向误拦；v0.43.13（08-12）起 `bull_veto_enabled=False` 停用。
  归档实测：最后一次触发 08-11，08-12 起 135 个存档扫描日里 0 次。
- 原定「换字段 + 干净样本重校准」（「9 月中旬大复盘」②）不做：BearBee 分数实测不带信息
  （看空单扣 SPY 命中 50.5%，19 周 p=0.63；置信度是可得性，见 `QueenDistiller.NON_VOTING_AGENTS` 注释）。
  重开还会改变开出哪些单 ⇒ 截断策略层优势预注册的样本。用户 2026-10-29 定：退役。
- 删掉的是停用分支，**生产行为零变化**（不登世代边界）。

守三件事：

1. **行为**：把两个旧开关原样塞回 `BEAR_SCORING_CONFIG`（`bull_veto_enabled=True` / 阈值 7.0），
   看多多数 + BearBee 高分 ⇒ 方向照旧看多、输出里没有 `bull_veto`。变红的变异：恢复 queen_distiller 那段分支。
2. **配置**：`BEAR_SCORING_CONFIG` 里不再有 `bull_veto*` 键（「停用中、留着参照」那种状态不许回来）。
3. **静态**：本仓自己的 .py（`tests/` 除外）里，字符串常量与标识符都不许含 `bull_veto`；
   注释里的历史叙述（「BullVeto」）不在 AST 里，不受影响。自证：扫到了文件，探测器对病灶源码会红。

⚠️ 本文件不用 skip：全部合成数据、零外部依赖。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests import _ast_cache
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__
TOKEN = "bull_veto"
BEAR = "BearBeeContrarian"


def _r(source, direction, confidence=0.6, score=5.0):
    return {"source": source, "direction": direction, "confidence": confidence,
            "score": score, "dimension": "x", "discovery": f"test {source}",
            "data_quality": {"test": "real"}, "details": {}}


def _bullish_majority():
    names = ["ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon", "RivalBeeVanguard"]
    dirs = ["bullish"] * 4 + ["neutral"]
    return [_r(n, d, 0.8) for n, d in zip(names, dirs)]


@pytest.fixture
def queen(board):
    from swarm_agents import QueenDistiller
    q = QueenDistiller(board)
    q.ml_feedback_enabled = False
    q.ml_adjustments = {}
    return q


def _vote(queen, results):
    return queen._compute_direction_vote("NVT", results, results, 6.0)


class TestBehavior:

    @pytest.mark.parametrize("bear_score", [7.0, 8.0, 10.0])
    def test_old_switches_do_nothing(self, queen, monkeypatch, bear_score):
        # ⚠️ 必须在 `queen` 实例化之后 patch：QueenDistiller.__init__ 会 reload(config)，之前 patch 会静默失效
        import config
        monkeypatch.setitem(config.BEAR_SCORING_CONFIG, "bull_veto_enabled", True)
        monkeypatch.setitem(config.BEAR_SCORING_CONFIG, "bull_veto_bear_score", 7.0)
        base = _bullish_majority()
        assert _vote(queen, base)["rule_direction"] == "bullish", "前提不成立：不含 BearBee 时应为看多"
        dv = _vote(queen, base + [_r(BEAR, "bearish", 0.97, score=bear_score)])
        assert dv["rule_direction"] == "bullish", f"BearBee 分数 {bear_score} 把看多压成了 {dv['rule_direction']}：BullVeto 回来了"
        assert TOKEN not in json.dumps(dv, default=str), "投票输出里又出现了 bull_veto 记录"


class TestConfig:

    def test_no_bull_veto_keys(self):
        import config
        left = sorted(k for k in config.BEAR_SCORING_CONFIG if TOKEN in k)
        assert not left, f"BEAR_SCORING_CONFIG 里又有 BullVeto 开关：{left}（v0.45.677 已退役，勿留「停用中」的键）"


def _hits(tree: ast.AST) -> list[str]:
    """字符串常量 / 名字 / 属性 / 函数与参数名里含 TOKEN 的位置（大小写不敏感）。docstring 不算（v0.45.686：
    写「旧的 bull_veto_enabled 开关已删除」是文档不是引用，误报会逼人去改冻结的历史叙述）。"""
    doc_ids = {id(n.body[0].value) for n in ast.walk(tree)
               if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
               and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for node in ast.walk(tree):
        texts = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids:
            texts.append(node.value)
        elif isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)
        elif isinstance(node, ast.arg):
            texts.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            texts.append(node.arg)
        if any(TOKEN in t.lower() for t in texts):
            out.append(f"L{getattr(node, 'lineno', '?')}: {texts[0][:60]!r}")
    return out


class TestStatic:

    def _files(self):
        files, how = own_python_files(REPO_ROOT)
        rels = [p for p in files if p.exists() and p.relative_to(REPO_ROOT).parts[0] != "tests"]
        return rels, how

    def test_no_production_reference(self):
        files, how = self._files()
        bad = {}
        for p in files:
            h = _hits(_ast_cache.parse_file(p))
            if h:
                bad[str(p.relative_to(REPO_ROOT))] = h
        assert not bad, f"BullVeto 已退役（v0.45.677），生产代码又引用了它（清单口径 {how}）：{bad}"

    def test_scan_saw_the_scoring_code(self):
        """自证 ①：扫描清单不是空的，且确实包含 BullVeto 原来所在的两个文件。"""
        files, _ = self._files()
        rels = {p.relative_to(REPO_ROOT).as_posix() for p in files}
        assert {"swarm_agents/queen_distiller.py", "config.py"} <= rels, "扫描清单里没有 queen_distiller / config —— 守卫是空转的"

    @pytest.mark.parametrize("lesion", [
        'if _BSC4.get("bull_veto_enabled", False):\n    pass\n',
        'conflict_info["bull_veto"] = {}\n',
        'BEAR = {"bull_veto_bear_score": 7.0}\n',
        'def apply_bull_veto(x):\n    return x\n',
        'Bull_Veto_On = True\n',
    ])
    def test_detector_catches_lesion(self, lesion):
        """自证 ②：探测器对病灶源码会红（含改大小写、改成函数名）。"""
        assert _hits(ast.parse(lesion)), f"探测器漏掉了：{lesion!r}"

    def test_detector_ignores_prose(self):
        """历史叙述（注释、CamelCase 的「BullVeto」、docstring）不算引用——否则得去改 `_COHORT_HISTORY` 的冻结文本。"""
        src = ('"""模块说明：旧的 bull_veto_enabled 开关已删除。"""\n# BullVeto 已退役\nNOTE = "BullVeto 当前 config 停用"\n'
               'def f():\n    """旧的 bull_veto_bear_score 阈值不再读。"""\n    return 1\n')
        assert not _hits(ast.parse(src))
