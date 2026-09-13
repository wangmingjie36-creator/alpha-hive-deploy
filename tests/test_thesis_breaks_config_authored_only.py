"""`thesis_breaks_config.json` 只装**人工编写**的失效条件，不装任何派生数据。

v0.45.215 新增。**这组测试存在的理由是「谁会红？」答不上来。**

P2-⑧ 曾把「误判模式」派生缓存（`auto_misjudgment_patterns` + `_meta` 时间戳）
写进这份手写配置。写入者后来在仓库里只剩函数定义、零调用点，于是 CHANGELOG
v0.45.112/115 断言「生产只读」—— 实际调用者在**另一个仓库**
（`深度分析报告/规则/weekly_analyzer.py`，Cowork 周任务），每周对全部历史非幂等
重放，hits 随运行次数膨胀。前后四周没有任何东西变红，发现它靠的是人肉看
`git status` 里一份 16k 行的脏 diff。

所以这里守的是**文件的形状**，而不是某个写入函数的名字：写入者可以住在任何
仓库、叫任何名字，但只要它往这份配置里塞派生节，在生产 checkout 上跑测试就会红
（这份文件被 git 跟踪，在 worktree / CI 上守的是提交进去的版本）。

形状判据：顶层键要么是**像 ticker 的名字 + 失效条件块**（含 `level_1_warning`
与 `level_2_stop_loss`），要么是已登记的 `_` 元数据键。按形状而不按
`auto_misjudgment_patterns` 这个 token 判，是因为换个名字的派生节同样该红。

⚠️ `_` 元数据键用**子集**语义：我只怕它变大（新的派生时间戳 / 缓存），
不怕它变小（删掉 `_machine_conditions_note` 无害）。

⚠️ 本文件不用任何 skip：配置被 git 跟踪、其余全是合成数据，任何机器都跑得到。
每条断言旁注明了能让它变红的变异。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 配置随代码发布、只读 ⇒ 用 __file__
CONFIG = REPO_ROOT / "thesis_breaks_config.json"

ALLOWED_META_KEYS = {"_all_tickers", "_machine_conditions_note"}
_TICKER = re.compile(r"[A-Z]{1,5}(-[A-Z])?")
_LEVELS = {"level_1_warning", "level_2_stop_loss"}

RETIRED_FUNCTIONS = {"register_misjudgment_pattern", "check_misjudgment_warnings"}
RETIRED_KEY = "auto_misjudgment_patterns"


def config_violations(cfg: dict) -> list[str]:
    """返回不符合「人工编写失效条件」形状的顶层键。"""
    bad = []
    for key, val in cfg.items():
        if key.startswith("_"):
            if key not in ALLOWED_META_KEYS:
                bad.append(key)
        elif not (_TICKER.fullmatch(key) and isinstance(val, dict) and _LEVELS <= set(val)):
            bad.append(key)
    return bad


def retired_symbol_hits(files) -> list[str]:
    """在给定 .py 里找被撤掉的函数定义，或把 `auto_misjudgment_patterns` 当键用的字符串常量。"""
    hits = []
    for path in files:
        try:
            tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in RETIRED_FUNCTIONS:
                hits.append(f"{path}:{node.lineno} def {node.name}")
            elif isinstance(node, ast.Constant) and node.value == RETIRED_KEY:
                hits.append(f"{path}:{node.lineno} '{RETIRED_KEY}'")
    return hits


def _production_files():
    files, _mode = own_python_files(REPO_ROOT)
    return [f for f in files if not ({"tests", "experiments"} & set(Path(f).relative_to(REPO_ROOT).parts))]


def test_real_config_holds_only_authored_blocks():
    # 变异：往配置里加回 auto_misjudgment_patterns 或 _meta.auto_patterns_updated_at ⇒ 红
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert config_violations(cfg) == [], (
        "thesis_breaks_config.json 混进了非人工编写的顶层键。派生数据（缓存 / 时间戳 / "
        "统计）不许写进这份手写配置——v0.45.215 就是这么膨胀出 16k 行脏 diff 的。"
        "真要加新的元数据键，先登记进 ALLOWED_META_KEYS 并写明它不是派生量。")


def test_retired_misjudgment_pattern_code_stays_retired():
    # 变异：在生产代码里恢复 def register_misjudgment_pattern / check_misjudgment_warnings，
    #       或写 cfg['auto_misjudgment_patterns'] ⇒ 红
    files = _production_files()
    assert retired_symbol_hits(files) == [], (
        "P2-⑧ 误判模式预警 v0.45.215 已撤（写入端非幂等 + 无增量预测力证据）。"
        "要重新检验请跑 experiments/misjudgment_pattern_walkforward.py，"
        "过闸前别接回，过闸后也应读时从 pheromone.db 计算、不持久化。")


class TestGuardsHaveTeeth:
    """没有这一组，上面两条的全绿证明不了任何事。"""

    _BLOCK = {"level_1_warning": {"conditions": []}, "level_2_stop_loss": {"conditions": []}}

    def test_derived_section_is_flagged_by_shape_not_by_name(self):
        cfg = {"NVDA": self._BLOCK, "auto_misjudgment_patterns": {"NVDA": {}},
               "misjudgment_cache": {"NVDA": {}}, "_meta": {"auto_patterns_updated_at": "x"}}
        assert sorted(config_violations(cfg)) == ["_meta", "auto_misjudgment_patterns", "misjudgment_cache"]

    def test_legitimate_new_ticker_block_passes(self):
        # 守卫不能挡正常编写：新增标的块、带连字符的 ticker 都应放行
        assert config_violations({"PLTR": self._BLOCK, "BRK-B": self._BLOCK, "_all_tickers": []}) == []

    def test_ticker_shaped_key_without_condition_levels_is_flagged(self):
        assert config_violations({"CACHE": {"NVDA": {}}}) == ["CACHE"]

    def test_scanner_sees_definitions_and_key_literals(self, tmp_path):
        planted = tmp_path / "planted.py"
        planted.write_text(
            "def register_misjudgment_pattern():\n    pass\n"
            "cfg = {}\ncfg['auto_misjudgment_patterns'] = {}\n"
            "# a comment mentioning check_misjudgment_warnings must NOT count\n",
            encoding="utf-8")
        hits = retired_symbol_hits([planted])
        assert len(hits) == 2 and any("def register_misjudgment_pattern" in h for h in hits)

    def test_scanner_actually_enumerates_production_code(self):
        # 正对照：扫描范围里必须真有生产文件，且能看见一个公认活着的函数定义；
        # 否则「零命中」只证明扫描器没扫到东西
        files = _production_files()
        names = {Path(f).name for f in files}
        assert {"feedback_loop.py", "market_intelligence.py", "generate_deep_v2.py"} <= names
        mi = next(f for f in files if Path(f).name == "market_intelligence.py")
        defs = {n.name for n in ast.walk(ast.parse(Path(mi).read_text(encoding="utf-8")))
                if isinstance(n, ast.FunctionDef)}
        assert "check_thesis_breaks" in defs
