"""`weekly_optimizer` / `self_analyst` 的数据路径跟 `ALPHA_HIVE_HOME` 走、代码路径跟检出走
（数据根迁移阶段 5 前置）。

两个模块此前都写死 `ALPHAHIVE_DIR = ~/Desktop/Alpha Hive`，并在 import 期把
`report_snapshots/`、`weight_history.jsonl`、`pheromone.db`、`self_analysis_briefs/`
冻成常量。阶段 5 把数据根迁到 `~/alpha-hive-data` 之后，紧接着跑的两个定时任务
（weekly_optimizer 09-27、self_analyst 10-01）会继续读代码检出里改名/过期的旧数据
——没有任何东西会报错，只是算的是旧数据。

本文件钉三件事：
1. 数据：`ALPHA_HIVE_HOME` 指向哪，解析器就指向哪，且是**调用时**求值（换 env 立即跟上）。
2. 代码：`CONFIG_PATH` 与它的两份备份仍在**本检出**里，不跟数据根跑。
   （反方向同样会出事：备份跟数据根走，回滚要 `cp` 回来时就找不到旁边的 config.py。）
3. 静态：两个模块里不许再出现家目录绝对路径字面量 / `expanduser` / Cowork VM glob，
   覆盖钩子在模块级必须是 `None`（写成 `PATHS.home / …` 或解析器调用 = 冻结复发）。
"""
from __future__ import annotations

import ast
import io
import json
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import pytest

import self_analyst as sa
import weekly_optimizer as wo

REPO_ROOT = Path(__file__).resolve().parent.parent
# 家目录绝对路径 / Cowork VM 挂载点（用正则而非字面前缀：本仓 tests/ 的
# 家目录路径检测器会把字面量 "/Users/" 本身当成违规）
_ABS_HOME_OR_VM = re.compile(r"^/(?:Users|home|sessions)/")

WO_DATA_HOOKS = ("ALPHAHIVE_DIR", "SNAPSHOTS_DIR", "HISTORY_FILE", "PHEROMONE_DB_PATH")
SA_DATA_HOOKS = ("ALPHAHIVE_DIR", "SNAPSHOTS_DIR", "BRIEFS_DIR")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """数据根指向一个**全新**的 tmp 目录，覆盖钩子全部回到生产缺省（None）。

    conftest 的 `_isolate_weekly_optimizer_db` 会把 `wo.PHEROMONE_DB_PATH` 钩子
    设成一个假路径——那样测的就是钩子而不是缺省解析，这里显式清掉。
    """
    h = tmp_path / "data_root"
    h.mkdir()
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(h))
    monkeypatch.delenv("ALPHA_HIVE_DB_PATH", raising=False)
    for name in WO_DATA_HOOKS:
        monkeypatch.setattr(wo, name, None)
    for name in SA_DATA_HOOKS:
        monkeypatch.setattr(sa, name, None)
    return h


# ───────────────────────────────────────────── 1. 数据跟 ALPHA_HIVE_HOME
class TestDataFollowsHome:
    def test_weekly_optimizer_resolvers_point_into_home(self, home):
        assert wo._snapshots_dir() == home / "report_snapshots"
        assert wo._history_file() == home / "weight_history.jsonl"
        assert Path(wo._pheromone_db_path()) == home / "pheromone.db"

    def test_self_analyst_resolvers_point_into_home(self, home):
        assert sa._snapshots_dir() == home / "report_snapshots"
        assert sa._briefs_dir() == home / "self_analysis_briefs"

    def test_pheromone_db_honors_db_path_override(self, home, tmp_path, monkeypatch):
        """与扫描主流程读同一个库：`PATHS.db` 额外尊重 `ALPHA_HIVE_DB_PATH`。"""
        db = tmp_path / "elsewhere" / "p.db"
        monkeypatch.setenv("ALPHA_HIVE_DB_PATH", str(db))
        assert Path(wo._pheromone_db_path()) == db

    def test_resolvers_are_evaluated_at_call_time(self, home, tmp_path, monkeypatch):
        """换 env 之后立即跟上 —— 冻在 import 期的实现在这里必红。"""
        other = tmp_path / "other_root"
        other.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(other))
        assert wo._snapshots_dir() == other / "report_snapshots"
        assert wo._history_file() == other / "weight_history.jsonl"
        assert Path(wo._pheromone_db_path()) == other / "pheromone.db"
        assert sa._snapshots_dir() == other / "report_snapshots"
        assert sa._briefs_dir() == other / "self_analysis_briefs"

    def test_hooks_still_override(self, home, tmp_path, monkeypatch):
        """既有测试靠 `monkeypatch.setattr(wo, "HISTORY_FILE", tmp)` 隔离，钩子必须优先。"""
        monkeypatch.setattr(wo, "HISTORY_FILE", tmp_path / "h.jsonl")
        monkeypatch.setattr(wo, "SNAPSHOTS_DIR", tmp_path / "s")
        monkeypatch.setattr(wo, "PHEROMONE_DB_PATH", tmp_path / "p.db")
        monkeypatch.setattr(sa, "BRIEFS_DIR", tmp_path / "b")
        assert wo._history_file() == tmp_path / "h.jsonl"
        assert wo._snapshots_dir() == tmp_path / "s"
        assert wo._pheromone_db_path() == tmp_path / "p.db"
        assert sa._briefs_dir() == tmp_path / "b"


# ───────────────────────────────────────────── 1b. 行为：真正的读写落在数据根
class TestEndToEndUsesHome:
    def test_weekly_optimizer_main_reads_snapshots_from_home(self, home, monkeypatch):
        (home / "report_snapshots").mkdir()
        monkeypatch.setattr(sys, "argv", ["weekly_optimizer.py"])   # 默认只读诊断
        buf = io.StringIO()
        with redirect_stdout(buf):
            wo.main()
        out = buf.getvalue()
        assert f"快照目录: {home / 'report_snapshots'}" in out, out
        assert f"config:   {REPO_ROOT / 'config.py'}" in out, out
        # v0.45.335：定时任务 agent 按这行的绝对路径去读「最新一条」，不许再猜相对路径
        assert f"审计日志: {home / 'weight_history.jsonl'}" in out, out

    def test_weekly_optimizer_reads_history_from_home(self, home):
        rec = {"action": "optimize", "applied": True, "dry_run": False,
               "schema_version": 2, "marker": "from-tmp-home"}
        (home / "weight_history.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        got = wo._last_applied_record()
        assert got is not None and got.get("marker") == "from-tmp-home", (
            "`_last_applied_record` 没读数据根下的 weight_history.jsonl")

    def test_self_analyst_main_reads_and_writes_under_home(self, home, monkeypatch):
        snaps = home / "report_snapshots"
        snaps.mkdir()
        now = datetime.now()
        for i, (d, entry, t7) in enumerate([("bullish", 100.0, 110.0),
                                            ("bearish", 100.0, 110.0)]):
            (snaps / f"T{i}.json").write_text(json.dumps({
                "ticker": f"T{i}", "date": now.strftime("%Y-%m-%d"),
                "created_at": now.isoformat(), "direction": d,
                "entry_price": entry, "actual_prices": {"t7": t7},
            }), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["self_analyst.py"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            sa.main()
        out = buf.getvalue()
        assert f"快照目录: {snaps}" in out, out
        assert "加载快照: 2 条" in out, out
        briefs = list((home / "self_analysis_briefs").glob("self_analysis_*.md"))
        assert len(briefs) == 1, f"简报没写进数据根：{out}"
        assert str(briefs[0]) in out, "简报的绝对路径必须印出来（定时任务 agent 照它去读）"


# ───────────────────────────────────────────── 2. 代码跟检出
@pytest.fixture
def wo_fresh(home):
    """在 `ALPHA_HIVE_HOME=<tmp>` 下**重新执行一遍** weekly_optimizer 的模块体。

    `CONFIG_PATH` / `BACKUP_*` 是模块级常量，在 import 那一刻求值；而收集期 import
    时 `ALPHA_HIVE_HOME` 往往未设、`PATHS.home` 缺省恰好就是仓库根 ⇒ 若它们被错接到
    数据根上，直接读 `wo.CONFIG_PATH` 也会「碰巧相等」（实测：该变异下旧写法全绿）。
    所以要在数据根≠检出的条件下重跑模块体再看。另起模块名，不动 `sys.modules` 里那份。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_wo_fresh_under_tmp_home", REPO_ROOT / "weekly_optimizer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCodeStaysWithCheckout:
    def test_config_path_is_this_checkouts_config(self, wo_fresh):
        assert wo_fresh.CONFIG_PATH == REPO_ROOT / "config.py"
        assert wo_fresh.CONFIG_PATH.is_file()

    def test_config_backups_live_next_to_config(self, wo_fresh):
        assert wo_fresh.BACKUP_DIR == REPO_ROOT / "weight_backups"
        assert wo_fresh.BACKUP_LATEST == REPO_ROOT / "config.py.weights.bak"

    def test_code_paths_do_not_follow_home(self, wo_fresh, home):
        for p in (wo_fresh.CONFIG_PATH, wo_fresh.BACKUP_DIR, wo_fresh.BACKUP_LATEST):
            assert home not in p.parents, f"{p} 跟着数据根跑了，它是代码（或代码的备份）"

    def test_fresh_module_data_resolvers_still_follow_home(self, wo_fresh, home):
        """对照：同一份重跑出来的模块，数据解析器确实指向 tmp 数据根——
        证明上面几条是在「数据根≠检出」的条件下测的，不是空转。"""
        assert wo_fresh._history_file() == home / "weight_history.jsonl"


# ───────────────────────────────────────────── 3. 静态：硬编码数据路径不许回来
def _module_tree(name):
    return ast.parse((REPO_ROOT / name).read_text(encoding="utf-8"))


def _docstring_nodes(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


@pytest.mark.parametrize("modfile", ["weekly_optimizer.py", "self_analyst.py"])
class TestNoHardcodedDataRoot:
    def test_no_home_literal_or_expanduser(self, modfile):
        tree = _module_tree(modfile)
        docs = _docstring_nodes(tree)
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                v = node.value
                if "Desktop" in v or v.startswith("~") or _ABS_HOME_OR_VM.match(v):
                    bad.append(f"{modfile}:{node.lineno} 字面量 {v!r}")
            if isinstance(node, ast.Attribute) and node.attr in ("expanduser", "home") \
                    and ast.unparse(node.value) in ("os.path", "Path"):
                bad.append(f"{modfile}:{node.lineno} {ast.unparse(node)}")
        assert not bad, (
            "数据路径又被写死了（数据根迁移后会读代码检出里的旧数据）：\n  "
            + "\n  ".join(bad)
            + "\n数据走 `hive_logger.PATHS`（调用时求值），代码锚 `__file__`。")

    def test_data_hooks_default_to_none_at_module_level(self, modfile):
        hooks = WO_DATA_HOOKS if modfile == "weekly_optimizer.py" else SA_DATA_HOOKS
        tree = _module_tree(modfile)
        seen = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in hooks:
                        seen.setdefault(t.id, []).append(node)
        missing = [h for h in hooks if h not in seen]
        assert not missing, f"{modfile} 缺少覆盖钩子 {missing}（既有测试 monkeypatch 它们）"
        bad = [f"{h} = {ast.unparse(n.value)}" for h, ns in seen.items() for n in ns
               if not (isinstance(n.value, ast.Constant) and n.value.value is None)]
        assert not bad, (
            f"{modfile} 的数据覆盖钩子在模块级被赋了非 None 值——等于在 import 期冻结：{bad}")

    def test_guard_has_teeth(self, modfile, tmp_path, monkeypatch):
        """把旧写法喂回扫描器，两条静态断言都必须能抓到。"""
        src = (REPO_ROOT / modfile).read_text(encoding="utf-8")
        src += ('\nimport os\nALPHAHIVE_DIR = Path(os.path.expanduser("~/Desktop/Alpha Hive"))\n'
                'SNAPSHOTS_DIR = ALPHAHIVE_DIR / "report_snapshots"\n')
        fake_root = tmp_path / "repo"
        fake_root.mkdir()
        (fake_root / modfile).write_text(src, encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", fake_root)
        with pytest.raises(AssertionError):
            self.test_no_home_literal_or_expanduser(modfile)
        with pytest.raises(AssertionError):
            self.test_data_hooks_default_to_none_at_module_level(modfile)
