"""墓碑（v0.45.550）：死链清理第二批——三个「写了没人读」的写入方，别让它们悄悄回来。

来源：v0.45.543 读者登记表（`tests/test_persistent_writes_have_readers.py`）普查出的 PENDING_DEAD，用户 2026-10-19 指示直接删代码：

1. `run_daily_scan._write_status` → `logs/last_run_status.json`（Sprint 1.4）：全仓零读者；只有手动跑的 `run_daily_scan.py` 写它，
   编排器不调。成功 / 失败的结局本来就在日志与终端打印里。
2. `CrowdingDetector.save_to_json` → cwd 下 `crowding_<T>.json`：全仓零调用者（`.save_to_json` 的调用点全是 `ReportSnapshot` 的）。
3. `HistoricalDataBuilder.save_to_file` → cwd 下 `training_data.json`：生产零调用者，只有一条测试调它（随之删除）。

删除后读者登记表的守卫按设计变红、点名「删条目、上限减三」⇒ `CEILING_PENDING_DEAD` 7 → 4；ledger 守卫的 `WRITERS` 同步删三条。
生产数据根里若有旧的 `last_run_status.json` 原样留着（删 = 改生产数据，未做）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests import _ast_cache

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

#: (文件, 类名或 None, 成员名)
RETIRED_MEMBERS = (
    ("run_daily_scan.py", None, "_write_status"),
    ("crowding_detector.py", "CrowdingDetector", "save_to_json"),
    ("ml_predictor.py", "HistoricalDataBuilder", "save_to_file"),
)
#: 这三个产物的**完整文件名**不许再以字符串常量出现在这三个文件里。f-string 的占位渲染成 `{}` 再比——
#: 只比片段会误中 logger 名 `alpha_hive.crowding_detector`、docstring 里的 `build_training_data_from_db`（首版就这么假红过）。
RETIRED_LITERALS = {
    "run_daily_scan.py": "last_run_status.json",
    "crowding_detector.py": "crowding_{}.json",
    "ml_predictor.py": "training_data.json",
}


def _has_member(rel: str, cls, name: str) -> bool:
    tree = _ast_cache.parse_file(REPO_ROOT / rel)
    scope = tree.body
    if cls is not None:
        found = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls]
        assert found, f"{rel} 里找不到类 {cls}——改名了？墓碑要跟着改，别让它对着不存在的类恒绿"
        scope = found[0].body
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name for n in scope)


def _string_constants(rel: str) -> list:
    """字符串常量与 f-string（占位渲染成 `{}`）；f-string 的常量片段不单独再算。"""
    return string_constants(_ast_cache.parse_file(REPO_ROOT / rel))


def string_constants(tree) -> list:
    nodes = list(ast.walk(tree))
    parts = {id(v) for n in nodes if isinstance(n, ast.JoinedStr) for v in n.values}
    out = []
    for n in nodes:
        if isinstance(n, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in parts:
            out.append(n.value)
    return out


class TestRetiredWritersStayRetired:

    def test_members_are_gone(self):
        back = [f"{rel}::{cls + '.' if cls else ''}{name}" for rel, cls, name in RETIRED_MEMBERS
                if _has_member(rel, cls, name)]
        assert not back, (f"这些写入方又回来了：{back}。它们写的文件全仓零读者（v0.45.550 删除）——"
                          "真要恢复，先在 tests/test_persistent_writes_have_readers.py 登记读者")

    def test_artifact_names_are_gone(self):
        back = {rel: [s for s in _string_constants(rel) if name in s]
                for rel, name in RETIRED_LITERALS.items()}
        back = {k: v for k, v in back.items() if v}
        assert not back, f"已删产物的文件名又出现在字符串常量里：{back}"


class TestTombstoneHasTeeth:
    """正对照：判据认得出活着的同类成员与字面量，免得对着空气恒绿。"""

    def test_member_check_sees_live_members(self):
        assert _has_member("feedback_loop.py", "ReportSnapshot", "save_to_json")
        assert _has_member("run_daily_scan.py", None, "main")
        assert _has_member("ml_predictor.py", "HistoricalDataBuilder", "add_record")

    def test_literal_check_sees_live_literals(self):
        assert any(".samples-only-" in s for s in _string_constants("alpha_hive_daily_report.py"))

    def test_fstring_filenames_render_to_the_registered_shape(self):
        tree = ast.parse('def f(t):\n    open(f"crowding_{t}.json", "w")\n    log = "alpha_hive.crowding_detector"\n')
        consts = string_constants(tree)
        assert "crowding_{}.json" in consts
        assert not any("crowding_{}.json" in c for c in consts if c != "crowding_{}.json")
