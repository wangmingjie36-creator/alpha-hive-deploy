"""
`price_history` 的缺省路径必须调用时经 `PATHS` 解析，不许跟 cwd 走（v0.45.463 回归）

事故：`data_pipeline._fill_momentum_from_index` 传 `os.environ.get("ALPHA_HIVE_CACHE_DIR", "cache")`，
`price_history._load_db_prices` 缺省 `db_path="pheromone.db"`——都是 cwd 相对。09-26 数据根迁移、
10-09 生产克隆之后，扫描的 cwd（`~/alpha-hive-prod`）里既没有 `cache/` 也没有 `pheromone.db`
（10-12 实测）⇒ yfinance 失败那天 momentum_5d 的回落恒为 None，且首次调用会在代码克隆里建
`cache/` + 迁移标记。两条失败都长得像「这只票没数据」，所以没人红。

判据：从一个**放了诱饵**的 cwd 调用——cwd 里有 `pheromone.db` 和 `cache/`，价格与沙箱里的不同。
读到诱饵 = 缺省路径跟着 cwd 走；cwd 里多出东西 = 往代码克隆写。

conftest `_isolate_env` 把 `ALPHA_HIVE_HOME` 指向本测试的 tmp_path，`_cwd` 夹具把 cwd 放在
`tmp_path/_cwd`——两者不重合，这正是本文件要区分的。

⚠️ `prod_env` 夹具撤掉 conftest 设的 `ALPHA_HIVE_CACHE_DIR` / `ALPHA_HIVE_DB_PATH`：生产只设
`ALPHA_HIVE_HOME`。留着它们，旧实现 `os.environ.get("ALPHA_HIVE_CACHE_DIR", "cache")` 在测试里
恰好读对，本文件对那条原形就是瞎的（v0.45.463 变异实测）。
"""

import ast
import os
import re
import sqlite3
from pathlib import Path

import pytest

import data_pipeline as dp
import price_history as ph
from hive_logger import PATHS
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent

# 8/7(五) … 8/14(五)（与 test_price_history 同一组日期）。本文件测的是路径不是窗口：
# 8/7 与 8/10 同价，锚点取 4 个交易日（8/10，v0.45.469）或 5 个（8/7）都得 +10%
_DATES = ["2026-08-07", "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
_SANDBOX = [100.0, 100.0, 102.0, 103.0, 104.0, 110.0]   # ⇒ +10%
_DECOY = [100.0, 100.0, 100.0, 100.0, 100.0, 50.0]       # ⇒ −50%


def _write_db(path: Path, ticker: str, closes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE predictions (date TEXT, ticker TEXT, price_at_predict REAL)")
    con.executemany("INSERT INTO predictions VALUES (?,?,?)",
                    [(d, ticker, c) for d, c in zip(_DATES, closes)])
    con.commit()
    con.close()


def _write_index(cache_dir: Path, ticker: str, closes) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"price_history_{ticker}.jsonl").write_text(
        "".join(f'{{"date": "{d}", "close": {c}}}\n' for d, c in zip(_DATES, closes)), encoding="utf-8")
    (cache_dir / f".price_index_migrated_{ticker}").write_text("test\n", encoding="utf-8")


def _tree(root: Path) -> set:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.fixture
def prod_env(tmp_path, monkeypatch):
    """生产的环境形状：只有 `ALPHA_HIVE_HOME`，cwd 是别处（生产 = 代码克隆）。"""
    monkeypatch.delenv("ALPHA_HIVE_CACHE_DIR", raising=False)
    monkeypatch.delenv("ALPHA_HIVE_DB_PATH", raising=False)
    cwd = Path.cwd()
    assert cwd == tmp_path / "_cwd", "conftest 的 _cwd 夹具没生效，本文件作废"
    assert os.environ.get("ALPHA_HIVE_HOME") == str(tmp_path), "conftest 的 _isolate_env 没生效，本文件作废"
    assert Path(PATHS.db) == tmp_path / "pheromone.db" and Path(PATHS.cache_dir) == tmp_path / "cache"
    assert not str(PATHS.cache_dir).startswith(str(cwd)) and not PATHS.db.startswith(str(cwd))
    return cwd


@pytest.fixture
def cwd_with_decoys(prod_env):
    """cwd 里放诱饵（与沙箱同名、价格不同）。"""
    cwd = prod_env
    _write_db(cwd / "pheromone.db", "NVDA", _DECOY)
    _write_index(cwd / "cache", "NVDA", _DECOY)
    return cwd


class TestDefaultsFollowPathsNotCwd:
    def test_load_db_prices_reads_paths_db(self, cwd_with_decoys):
        _write_db(Path(PATHS.db), "NVDA", _SANDBOX)
        assert ph._load_db_prices("NVDA") == dict(zip(_DATES, _SANDBOX)), (
            "`_load_db_prices` 缺省没读 PATHS.db —— 读到的是 cwd 里的 pheromone.db 或什么都没读")

    def test_momentum_reads_sandbox_db_and_creates_nothing_under_cwd(self, cwd_with_decoys):
        """生产形状：价格索引冻在过去，DB 是唯一的新鲜来源（price_history.append_observation 无生产调用方）。"""
        _write_db(Path(PATHS.db), "NVDA", _SANDBOX)
        before = _tree(cwd_with_decoys)
        assert ph.momentum_5d("NVDA") == pytest.approx(10.0, abs=0.01)
        assert _tree(cwd_with_decoys) == before, "往 cwd（生产 = 代码克隆）里写了东西"
        # 迁移标记应写进 PATHS.cache_dir，而不是 cwd
        assert (Path(PATHS.cache_dir) / ".price_index_migrated_NVDA").exists()

    def test_momentum_reads_paths_cache_dir_index(self, cwd_with_decoys):
        _write_index(Path(PATHS.cache_dir), "NVDA", _SANDBOX)
        before = _tree(cwd_with_decoys)
        assert ph.momentum_5d("NVDA") == pytest.approx(10.0, abs=0.01)
        assert _tree(cwd_with_decoys) == before

    def test_data_pipeline_fallback_end_to_end(self, cwd_with_decoys):
        """扫描真正走的那条：yfinance 失败 → `_fill_momentum_from_index`。"""
        _write_db(Path(PATHS.db), "NVDA", _SANDBOX)
        before = _tree(cwd_with_decoys)
        out = dp._fill_momentum_from_index("NVDA", None)
        assert out is not None and out["momentum_source"] == "price_index"
        assert out["momentum_5d"] == pytest.approx(10.0, abs=0.01), "回落读到了 cwd 里的诱饵"
        assert _tree(cwd_with_decoys) == before

    def test_data_pipeline_fallback_reads_paths_cache_index(self, cwd_with_decoys):
        """同上，但只给索引不给库：上一条里沙箱库按日期覆盖了诱饵索引，单靠它认不出
        `data_pipeline` 传了 cwd 相对的缓存目录（v0.45.463 变异实测：恢复旧行它照绿）。"""
        _write_index(Path(PATHS.cache_dir), "NVDA", _SANDBOX)
        out = dp._fill_momentum_from_index("NVDA", None)
        assert out is not None and out["momentum_5d"] == pytest.approx(10.0, abs=0.01), (
            "回落读到了 cwd/cache 里的诱饵索引 —— data_pipeline 传的缓存目录跟着 cwd 走")

    def test_empty_cwd_stays_empty(self, prod_env):
        """没有诱饵时同样不许在 cwd 建 `cache/`（旧实现首次调用会建目录 + 写迁移标记）。"""
        cwd = prod_env
        assert _tree(cwd) == set()
        _write_db(Path(PATHS.db), "NVDA", _SANDBOX)
        assert dp._fill_momentum_from_index("NVDA", None)["momentum_5d"] == pytest.approx(10.0, abs=0.01)
        assert ph.coverage_report() is not None
        assert _tree(cwd) == set()

    def test_cache_dir_env_override_still_honoured(self, cwd_with_decoys, tmp_path, monkeypatch):
        """`ALPHA_HIVE_CACHE_DIR` 的覆盖语义保留（经 PATHS.cache_dir）。"""
        other = tmp_path / "override_cache"
        monkeypatch.setenv("ALPHA_HIVE_CACHE_DIR", str(other))
        _write_index(other, "NVDA", _SANDBOX)
        assert ph.momentum_5d("NVDA") == pytest.approx(10.0, abs=0.01)

    def test_missing_db_is_loud(self, cwd_with_decoys, caplog, monkeypatch):
        """库不存在要 warning：此前它与「这只票没记录」都返回 {}，生产上恒失效两周没人红。"""
        monkeypatch.setattr(ph, "_MISSING_DB_WARNED", set())
        assert not Path(PATHS.db).exists()
        with caplog.at_level("WARNING", logger="alpha_hive.price_history"):
            assert ph._load_db_prices("NVDA") == {}
        assert any(PATHS.db in r.getMessage() for r in caplog.records)

    def test_explicit_paths_still_win(self, tmp_path):
        db = tmp_path / "elsewhere.db"
        _write_db(db, "NVDA", _SANDBOX)
        assert ph._load_db_prices("NVDA", str(db)) == dict(zip(_DATES, _SANDBOX))
        cache = tmp_path / "elsewhere_cache"
        _write_index(cache, "NVDA", _SANDBOX)
        assert ph.load_price_history("NVDA", str(cache))[-1] == ("2026-08-14", 110.0)


class TestReadFailuresAreLoud:
    """「库在但读不了」与「库不存在」同形（都是 {}），v0.45.463 只堵了后者（v0.45.470 二次检查补）。"""

    def test_unreadable_db_warns(self, prod_env, caplog, monkeypatch):
        monkeypatch.setattr(ph, "_MISSING_DB_WARNED", set())
        Path(PATHS.db).write_bytes(b"this is not a sqlite database" * 100)
        with caplog.at_level("WARNING", logger="alpha_hive.price_history"):
            assert ph._load_db_prices("NVDA") == {}
        assert any("权威价库读取失败" in r.getMessage() for r in caplog.records)

    def test_missing_table_warns(self, prod_env, caplog, monkeypatch):
        monkeypatch.setattr(ph, "_MISSING_DB_WARNED", set())
        sqlite3.connect(PATHS.db).close()          # 空库：没有 predictions 表
        with caplog.at_level("WARNING", logger="alpha_hive.price_history"):
            assert ph._load_db_prices("NVDA") == {}
        assert any("OperationalError" in r.getMessage() for r in caplog.records)

    def test_fallback_error_is_not_silent(self, prod_env, caplog, monkeypatch):
        """回落里抛异常（代码 / 环境坏了）≠ 索引里没数据（返回 None），前者必须 warning。

        桩打在 `_default_cache_dir`（路径解析）而不是入口函数：v0.45.465 把 data_pipeline 的入口从
        `momentum_5d` 换成 `momentum_5d_detail` 后，打在旧入口上的桩落空、本条变红——不管调哪个入口，
        缺省路径解析都绕不开它。"""
        def _boom(*_a, **_k):
            raise RuntimeError("PATHS 解析失败")
        monkeypatch.setattr(ph, "_default_cache_dir", _boom)
        with caplog.at_level("WARNING", logger="alpha_hive.data_pipeline"):
            assert dp._fill_momentum_from_index("NVDA", None) is None
        assert any("RuntimeError" in r.getMessage() for r in caplog.records)


# ── 结构守卫：数据文件 / 数据目录的 cwd 相对字面量缺省不许再扩散 ─────────────────────────
#
# 上面的行为测试只钉 price_history 一个模块；同一形状（函数缺省参数或 `os.environ.get(X, "<相对路径>")`
# 写成数据文件名 / 数据目录名）在全仓还有几处。`test_paths_not_frozen_at_import` 的两条物种守卫
# 只认 `PATHS` / `__file__` 标记，裸字面量两边都不含，所以本形状此前没有任何守卫。
#
# 管得到：函数缺省参数（位置 / 仅关键字）与 `os.environ.get` / `os.getenv` 的缺省值，字面量是
#   ① 已知数据目录名（可带子路径），或 ② 以数据文件扩展名结尾。
# 管不到（写明，免得被读成「全仓无此形状」）：函数体里的 `Path("cache")` / `open("x.json")`、
#   模块级常量、拼接出来的字符串、不在下面词表里的目录名。

_DATA_DIR_NAMES = ("cache", "logs", "chroma_db", "report_snapshots", "earnings_cache")
_DATA_EXTS = ("db", "sqlite", "json", "jsonl", "csv", "pkl", "pickle", "parquet", "joblib",
              "npz", "html", "log", "xlsx")
_DATA_LITERAL = re.compile(
    r"^(?:\./)?(?:(?:" + "|".join(_DATA_DIR_NAMES) + r")(?:/.*)?"
    r"|[\w.\-/]*\.(?:" + "|".join(_DATA_EXTS) + r"))$")

# 子集语义：存量只许减。修好一处删一行；下面还有对账断言防过期。
# 每条都核过生产调用方（v0.45.463 / 470）——「在 KNOWN 里」不等于「无害」，理由写在旁边。
KNOWN_CWD_RELATIVE_DEFAULTS = {
    # 无生产调用方 / 只在 __main__ / 研究脚本：
    ("iv_history.py", "_observed_accrual_rate", "cache"),          # 生产调用方 vrp_signal 显式传绝对路径
    ("catalyst_exit_planner.py", "load_catalysts_for_ticker", "catalysts.json"),  # 仅 dynamic_exit_backtest
    ("ml_predictor.py", "save_to_file", "training_data.json"),
    ("ml_predictor_extended.py", "save_to_file", "training_data_extended.json"),
    ("alpha_hive_bot/config.py", "os.environ.get", "./subscribers.db"),  # Telegram bot，独立部署
    ("backtest_engine.py", "__init__", "./report_snapshots"),      # 无调用方
    ("regime_analyzer.py", "__init__", "report_snapshots"),        # 无调用方
    ("feedback_loop.py", "save_accuracy_dashboard", "accuracy_dashboard.html"),  # 仅 __main__
    # 生产调用方全部显式传目录（daily_report / generate_deep_v2 / outcomes_fetcher / weekly_optimizer），
    # 缺省值只在 __main__ 用到：
    ("feedback_loop.py", "save_to_json", "report_snapshots"),
    ("feedback_loop.py", "__init__", "report_snapshots"),          # BacktestAnalyzer
    ("finrl_bridge.py", "prepare_features", "report_snapshots"),
    ("finrl_bridge.py", "generate_rl_insights", "report_snapshots"),
    ("options_backtester.py", "__init__", "report_snapshots"),
    # 字面量相对、但**不跟 cwd 走**：拼到 PATHS.home（earnings_history 调用时求值；
    # earnings_vol_signal 经 BASE_DIR=PATHS.home，import 期冻结、另有 KNOWN 登记）。
    ("earnings_history.py", "earnings_move_stats_detail", "earnings_cache"),
    ("earnings_history.py", "earnings_move_stats", "earnings_cache"),
    ("earnings_vol_signal.py", "scan", "cache"),
    ("earnings_vol_signal.py", "scan", "earnings_cache"),
}

_EXCLUDED_TOP = {"tests", "experiments"}


def _scan_cwd_relative_defaults(root: Path = REPO_ROOT, _stats=None) -> set:
    """文件集走共享实现 `own_python_files`（git ls-files 优先、rglob 回退、滤掉 vendored / 点号目录）。

    非 UTF-8 / 语法错的文件**记账**不跳过：直接跳过 = 把「没扫」渲染成「没有违规」（v0.45.150）。
    """
    files, mode = own_python_files(root)
    found, unreadable = set(), []
    for p in sorted(files):
        rel = p.relative_to(root)
        if rel.parts[0] in _EXCLUDED_TOP:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, SyntaxError, OSError):
            unreadable.append(rel.as_posix())
            continue
        for n in ast.walk(tree):
            cands = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                cands = [(n.name, d) for d in list(a.defaults) + [d for d in a.kw_defaults if d is not None]]
            elif (isinstance(n, ast.Call) and ast.unparse(n.func) in ("os.environ.get", "os.getenv")
                  and len(n.args) == 2):
                cands = [("os.environ.get", n.args[1])]
            for name, d in cands:
                if (isinstance(d, ast.Constant) and isinstance(d.value, str)
                        and not os.path.isabs(d.value) and _DATA_LITERAL.match(d.value)):
                    found.add((rel.as_posix(), name, d.value))
    if _stats is not None:
        _stats.update(mode=mode, n_files=len(files), unreadable=unreadable)
    return found


class TestNoNewCwdRelativeDataDefaults:
    def test_no_new_sites(self):
        stats = {}
        new = _scan_cwd_relative_defaults(_stats=stats) - KNOWN_CWD_RELATIVE_DEFAULTS
        assert stats["n_files"] > 100, f"只枚举到 {stats['n_files']} 个文件（{stats['mode']}），扫描器失明"
        assert not stats["unreadable"], f"这些自有文件没扫成（非 UTF-8 / 语法错）：{stats['unreadable']}"
        assert not new, (
            "新增了 cwd 相对的数据路径缺省值：\n  " + "\n  ".join(map(str, sorted(new))) +
            "\n生产扫描的 cwd 是代码克隆 ~/alpha-hive-prod，那里没有数据。缺省写 None，"
            "在函数体里经 hive_logger.PATHS 解析（见 CLAUDE.md「新产物的默认路径不许是相对路径」）。")

    def test_known_list_not_stale(self):
        stale = KNOWN_CWD_RELATIVE_DEFAULTS - _scan_cwd_relative_defaults()
        assert not stale, f"这些存量已修好，从 KNOWN_CWD_RELATIVE_DEFAULTS 删掉：{sorted(stale)}"

    def test_scanner_has_teeth(self, tmp_path):
        """正对照走**完整扫描器**（文件枚举 + AST + 正则），不只测正则：
        只测正则时，AST 那半边坏了（比如漏掉仅关键字缺省）本条照样绿（v0.45.470 二次检查）。"""
        root = tmp_path / "repo"
        (root / "pkg").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "mod.py").write_text(
            "import os\n"
            "def a(db_path='pheromone.db'): pass\n"                # 位置参数缺省
            "def b(*, cache_dir='cache/cboe_daily'): pass\n"        # 仅关键字缺省
            "class K:\n"
            "    def __init__(self, d='./report_snapshots'): pass\n"  # 方法
            "X = os.getenv('X', 'state.jsonl')\n"                   # 环境变量缺省
            "def ok(p='/abs/pheromone.db', name='NVDA', enc='utf-8', u=None): pass\n",
            encoding="utf-8")
        (root / "pkg" / "deep.py").write_text("async def c(f='out.csv'): pass\n", encoding="utf-8")
        (root / "tests" / "t.py").write_text("def t(p='pheromone.db'): pass\n", encoding="utf-8")
        (root / "big5.py").write_bytes("# 中文\n".encode("big5"))
        stats = {}
        got = _scan_cwd_relative_defaults(root, _stats=stats)
        assert got == {
            ("mod.py", "a", "pheromone.db"),
            ("mod.py", "b", "cache/cboe_daily"),
            ("mod.py", "__init__", "./report_snapshots"),
            ("mod.py", "os.environ.get", "state.jsonl"),
            ("pkg/deep.py", "c", "out.csv"),
        }, got
        assert stats["unreadable"] == ["big5.py"], "非 UTF-8 文件必须记账，不能静默跳过"
