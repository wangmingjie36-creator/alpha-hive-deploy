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
import subprocess
from pathlib import Path

import pytest

import data_pipeline as dp
import price_history as ph
from hive_logger import PATHS

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


# ── 结构守卫：数据文件 / 数据目录的 cwd 相对字面量缺省不许再扩散 ─────────────────────────
#
# 上面的行为测试只钉 price_history 一个模块；同一形状（函数缺省参数或 `os.environ.get(X, "<相对路径>")`
# 写成数据文件名 / `cache` 之类）在全仓还有几处。`test_paths_not_frozen_at_import` 的两条物种守卫
# 只认 `PATHS` / `__file__` 标记，裸字面量两边都不含，所以本形状此前没有任何守卫。

_DATA_LITERAL = re.compile(
    r"^(?:\./)?(?:cache|logs|chroma_db|[\w.\-/]*\.(?:db|sqlite|jsonl|json))(?:/.*)?$")

# 子集语义：存量（v0.45.463 实测）只许减。修好一处删一行；下面还有对账断言防过期。
KNOWN_CWD_RELATIVE_DEFAULTS = {
    # CLI / 研究脚本层，不在扫描路径上（v0.45.463 核过调用方）：
    ("iv_history.py", "_observed_accrual_rate", "cache"),          # 生产调用方 vrp_signal 显式传绝对路径
    ("catalyst_exit_planner.py", "load_catalysts_for_ticker", "catalysts.json"),  # 仅 dynamic_exit_backtest
    ("ml_predictor.py", "save_to_file", "training_data.json"),      # 无生产调用方
    ("ml_predictor_extended.py", "save_to_file", "training_data_extended.json"),
    ("alpha_hive_bot/config.py", "os.environ.get", "./subscribers.db"),  # Telegram bot，独立部署
    # 字面量相对、但**不跟 cwd 走**：`_resolve_dir` 拼到 BASE_DIR(=PATHS.home，import 期冻结)。
    # 生产上落在数据根；只是不读 ALPHA_HIVE_CACHE_DIR（BASE_DIR 那一半另有 KNOWN 登记）。
    ("earnings_vol_signal.py", "scan", "cache"),
}


def _scan_cwd_relative_defaults() -> set:
    files = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert len(files) > 100, f"git ls-files 只列出 {len(files)} 个文件，扫描器失明"
    found = set()
    for rel in files:
        if rel.startswith(("tests/", "experiments/")):
            continue
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                defaults = list(a.defaults) + [d for d in a.kw_defaults if d is not None]
                for d in defaults:
                    if (isinstance(d, ast.Constant) and isinstance(d.value, str)
                            and not os.path.isabs(d.value) and _DATA_LITERAL.match(d.value)):
                        found.add((rel, n.name, d.value))
            elif (isinstance(n, ast.Call) and ast.unparse(n.func) in ("os.environ.get", "os.getenv")
                  and len(n.args) == 2 and isinstance(n.args[1], ast.Constant)
                  and isinstance(n.args[1].value, str) and not os.path.isabs(n.args[1].value)
                  and _DATA_LITERAL.match(n.args[1].value)):
                found.add((rel, "os.environ.get", n.args[1].value))
    return found


class TestNoNewCwdRelativeDataDefaults:
    def test_no_new_sites(self):
        new = _scan_cwd_relative_defaults() - KNOWN_CWD_RELATIVE_DEFAULTS
        assert not new, (
            "新增了 cwd 相对的数据路径缺省值：\n  " + "\n  ".join(map(str, sorted(new))) +
            "\n生产扫描的 cwd 是代码克隆 ~/alpha-hive-prod，那里没有数据。缺省写 None，"
            "在函数体里经 hive_logger.PATHS 解析（见 CLAUDE.md「新产物的默认路径不许是相对路径」）。")

    def test_known_list_not_stale(self):
        stale = KNOWN_CWD_RELATIVE_DEFAULTS - _scan_cwd_relative_defaults()
        assert not stale, f"这些存量已修好，从 KNOWN_CWD_RELATIVE_DEFAULTS 删掉：{sorted(stale)}"

    def test_scanner_has_teeth(self):
        """正对照：v0.45.463 修掉的两处原形必须被这个正则认出来，否则上面两条恒绿。"""
        for lit in ("pheromone.db", "cache", "cache/cboe_daily", "./subscribers.db"):
            assert _DATA_LITERAL.match(lit), lit
        for lit in ("NVDA", "utf-8", "yfinance", "/abs/pheromone.db"):
            assert not (_DATA_LITERAL.match(lit) and not os.path.isabs(lit)), lit
