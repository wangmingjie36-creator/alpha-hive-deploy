"""仓库根「默认拒绝」总闸自己的测试（v0.45.233，数据根迁移阶段 0.3）。

三层，缺一层就证明不了闸有牙：

1. **判据**：哪些算代码、哪些算数据——用真实仓库里出现过的路径形状逐条钉死。
   （v0.45.409 起旧清单 `_GUARDED_PRODUCTION_ARTIFACTS` 已删：数据搬到数据根后它盯的仓库根路径都不存在。）
2. **指纹比对**：合成目录树上逐种写入形状（写一字节 / 新建空目录 / -shm 读标记 / 改代码）。
3. **接线**：子进程里真跑一轮 pytest，用的是**原样拷贝的 conftest**——
   只测 helper 证明不了 conftest 真的调用了它、也证明不了「之前」取在收集期之前
   （MEMORY `alpha-hive-test-writes-production`：测 helper ≠ 测接线）。
   红组必须红、对照组必须绿；对照组不绿，红组的红就说明不了任何事。

v0.45.409（阶段 6 ⑤）追加**真实数据根闸**的同三层：判据（`data_root_excluded_reason` / `real_data_root`）、
合成数据根上的指纹比对、子进程接线（含「没有数据根 ⇒ header 明说 INACTIVE」——它在 CI / 干净克隆上什么都不保护，
所以牙只能靠这里的合成测试证明）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import site
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent
sys.path.insert(0, str(_TESTS))
import _root_data_guard as g  # noqa: E402


# ─────────────────────────────── 1. 判据 ───────────────────────────────

GUARDED = [
    # 旧清单那 6 项之外、真实出过事或真实存在的数据形状
    "probability_scorecard_state/published.jsonl",   # 旧清单漏掉的那本账（v0.45.135 夹具写进过 XOM）
    "paper_portfolio_state/equity_curve.jsonl",      # v0.45.104 被重写 93→1 行
    "ml_model.json", "ml_model_cache.json",          # v0.45.149 被夹具模型覆盖
    "cache/options_snapshot_NVDA_2026-09-11.json",   # v0.41.3 应用缓存被当真数据复用
    ".factor_cache/ff5_daily.parquet",               # 点号开头的**应用**缓存不是工具缓存
    "report_snapshots/NVDA_2026-09-11.json",
    "alpha-hive-daily-2026-09-11.md", "alpha-hive-daily-2026-09-11.html", "index.html",  # 根目录报告是数据
    "self_analysis_briefs/self_analysis_2026-09.md", "experiments/final_score_dilution_report.md",
    "logs/scan_timing.json", "pheromone.db-wal", "sentiment_baseline.db",
    "QUICK_START.md",                                # 陈年文档不豁免：测试期间变了就该红
    "tests/some_output.json",                        # tests/ 下的非代码产物
    "weight_backups", "config.py.weights.bak", ".alpha_hive_av_key", "brand_new_dir/f.bin",
    # v0.45.253：生产运行时从仓库根读的热加载覆盖文件——不管谁写的，测试写它就是改生产 WATCHLIST
    "watchlist_override.yaml", "watchlist_override.json",
]
EXCLUDED = [
    "alpha_hive_daily_report.py", "swarm_agents/cache.py", "tests/conftest.py", "run_alpha_hive_daily.sh",
    "templates/report.html", "templates/report.css", "prompts/bear.md", ".github/workflows/ci.yml",
    "CHANGELOG.md", "CLAUDE.md", "README.md", "requirements.txt", ".gitignore", "pyproject.toml",
    "__pycache__/config.cpython-311.pyc", "tests/__pycache__/conftest.cpython-311-pytest-9.0.2.pyc",
    ".pytest_cache/v/cache/lastfailed", ".ruff_cache/0.1/x",
    ".git", ".git/index",                            # worktree 里 .git 是文件
    ".claude", ".claude/worktrees/w1/pheromone.db",  # 嵌套 worktree 里的库不是本 checkout 的（v0.45.189）
    "alpha-hive-web/node_modules/next/package.json",
    ".DS_Store", "report_snapshots/.DS_Store", ".fuse_hidden0000000400000001",
]


@pytest.mark.parametrize("rel", GUARDED)
def test_data_shapes_are_guarded(rel):
    assert g.excluded_reason(rel) is None, f"{rel} 被划成了「{g.excluded_reason(rel)}」，但它是数据"


@pytest.mark.parametrize("rel", EXCLUDED)
def test_code_and_tooling_are_excluded(rel):
    assert g.excluded_reason(rel) is not None, f"{rel} 不该受闸"


def test_code_root_files_only_apply_at_root():
    """同名文件放进子目录就不是仓库元文件了（`experiments/README.md` 是实验产物）。"""
    assert g.excluded_reason("README.md") == "code-root-file"
    assert g.excluded_reason("experiments/README.md") is None
    assert g.excluded_reason("db_snapshots/README.md") is None


def test_code_dirs_only_apply_at_top_level():
    """`templates` 只在顶层是代码资源；数据目录里恰好叫这个名字的子目录照样受闸。"""
    assert g.excluded_reason("templates/x.html") == "code-dir:templates"
    assert g.excluded_reason("report_snapshots/templates/x.json") is None


# ───────────────── 1b. 真实数据根：判据（v0.45.409，阶段 6 ⑤） ─────────────────

DATA_ROOT_GUARDED = [
    "pheromone.db", "pheromone.db-wal", "metrics.db", "chroma_db/chroma.sqlite3",
    "report_snapshots/NVDA_2026-09-11.json", "paper_portfolio_state/meta.json",
    "ml_model_history/manifest.jsonl", "cache/options_snapshot_NVDA_2026-09-11.json",
    "index.html", "alpha-hive-daily-2026-09-11.md", "weight_history.jsonl",
    "sell_strike_state/monthly/2026-09.jsonl", "alphabot_state/settings.json",
    "self_analysis_briefs/self_analysis_2026-10.md", "brand_new_dir/f.bin",   # 默认拒绝：新产物也盯
    # v0.45.427：`_git_backup` 是 `_` 元目录里唯一测试碰得到的（`PATHS.data_backup_repo`）——10-03 夹具写进的就是它
    "_git_backup/pheromone/manifest.json", "_git_backup/bk_src", "_git_backup/bk_src/paper_portfolio_state/meta.json",
]
DATA_ROOT_EXCLUDED = [
    "logs/scan_timing.json", "logs/alpha_hive.log", "db_backups/pheromone_2026-09-28.db",   # 常驻写入方
    "_archive/pheromone_bak/a.db", "_migration/r.json",
    "_manual_backups/x", ".DS_Store", "__pycache__/x.pyc", "sub/.DS_Store",
    "_git_backup/.git/index", "_git_backup/bk_src/.git/HEAD",   # git 自己的簿记（SKIP_DIR_NAMES，任意深度）
]


@pytest.mark.parametrize("rel", DATA_ROOT_GUARDED)
def test_data_root_shapes_are_guarded(rel):
    assert g.data_root_excluded_reason(rel) is None, rel


@pytest.mark.parametrize("rel", DATA_ROOT_EXCLUDED)
def test_data_root_volatile_and_meta_are_excluded(rel):
    assert g.data_root_excluded_reason(rel) is not None, rel


def test_data_root_exclusions_only_apply_at_top_level():
    """`logs` / `_x` 只在数据根顶层豁免；嵌套的同名目录里的产物仍受闸（否则 report_snapshots/logs/ 成了盲区）。"""
    assert g.data_root_excluded_reason("report_snapshots/logs/x.json") is None
    assert g.data_root_excluded_reason("cache/_tmp/x.json") is None


def test_real_data_root_resolution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    (home / "alpha-hive-data").mkdir(parents=True)
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()
    # 环境变量优先
    assert g.real_data_root({"ALPHA_HIVE_HOME": str(explicit)}, str(home), str(repo)) == str(explicit)
    # 没设 ⇒ 生产默认位置
    assert g.real_data_root({}, str(home), str(repo)) == str(home / "alpha-hive-data")
    # 目录不存在 ⇒ None（CI / 干净克隆）
    assert g.real_data_root({}, str(tmp_path / "nohome"), str(repo)) is None
    assert g.real_data_root({"ALPHA_HIVE_HOME": str(tmp_path / "gone")}, str(home), str(repo)) is None
    # 与仓库根相同 ⇒ None（那由仓库根闸管，不重复盯）
    assert g.real_data_root({"ALPHA_HIVE_HOME": str(repo)}, str(home), str(repo)) is None


# ─────────────────────────────── 2. 指纹比对 ───────────────────────────────

@pytest.fixture
def tree(tmp_path):
    r = tmp_path / "repo"
    for rel, data in {
        "probability_scorecard_state/published.jsonl": b"{}\n",
        "report_snapshots/a.json": b"{}",
        "pheromone.db": b"db", "pheromone.db-wal": b"wal", "pheromone.db-shm": b"shm",
        "mod.py": b"x = 1\n", "templates/t.html": b"<p>", "CHANGELOG.md": b"# c\n",
        ".claude/worktrees/w1/pheromone.db": b"nested",
    }.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_bytes(data)
    return r


def _bump_mtime(p: Path):
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_one_byte_append_to_ledger_is_caught(tree):
    before = g.fingerprint(str(tree))
    with open(tree / "probability_scorecard_state/published.jsonl", "ab") as f:
        f.write(b"x")
    assert g.diff(before, g.fingerprint(str(tree))) == {"changed": ["probability_scorecard_state/published.jsonl"]}


def test_same_size_rewrite_is_caught_by_mtime(tree):
    """stat 口径：内容等长替换、大小不变，靠 mtime 抓（sqlite 读写打开也是这个形状）。"""
    before = g.fingerprint(str(tree))
    _bump_mtime(tree / "report_snapshots/a.json")
    assert g.diff(before, g.fingerprint(str(tree))) == {"changed": ["report_snapshots/a.json"]}


def test_new_empty_directory_is_caught(tree):
    before = g.fingerprint(str(tree))
    (tree / "backups").mkdir()
    assert g.diff(before, g.fingerprint(str(tree))) == {"added": ["backups"]}


def test_new_directory_with_file_is_caught(tree):
    before = g.fingerprint(str(tree))
    (tree / "brand_new").mkdir()
    (tree / "brand_new/f.bin").write_bytes(b"x")
    assert g.diff(before, g.fingerprint(str(tree))) == {"added": ["brand_new", "brand_new/f.bin"]}


def test_deletion_is_caught(tree):
    before = g.fingerprint(str(tree))
    (tree / "report_snapshots/a.json").unlink()
    assert g.diff(before, g.fingerprint(str(tree))) == {"removed": ["report_snapshots/a.json"]}


def test_sqlite_sidecar_appearing_is_caught(tree):
    """v0.45.150 的证据形状：测试读写打开生产库留下 -wal/-shm。"""
    (tree / "pheromone.db-wal").unlink()
    (tree / "pheromone.db-shm").unlink()
    before = g.fingerprint(str(tree))
    (tree / "pheromone.db-wal").write_bytes(b"")
    (tree / "pheromone.db-shm").write_bytes(b"s" * 32768)
    assert g.diff(before, g.fingerprint(str(tree))) == {"added": ["pheromone.db-shm", "pheromone.db-wal"]}


def test_shm_reader_marks_are_not_a_write(tree):
    """纯读者顶 -shm 的 mtime（常驻只读 MCP 进程）不算写；-wal 的 mtime 变了照样红。"""
    before = g.fingerprint(str(tree))
    _bump_mtime(tree / "pheromone.db-shm")
    assert g.diff(before, g.fingerprint(str(tree))) == {}
    _bump_mtime(tree / "pheromone.db-wal")
    assert g.diff(before, g.fingerprint(str(tree))) == {"changed": ["pheromone.db-wal"]}


def test_code_edits_and_tool_caches_are_not_writes(tree):
    before = g.fingerprint(str(tree))
    (tree / "mod.py").write_bytes(b"x = 2  # edited during the run\n")
    (tree / "templates/t.html").write_bytes(b"<p>edited")
    (tree / "CHANGELOG.md").write_bytes(b"# c\n## new entry\n")
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__/mod.cpython-311.pyc").write_bytes(b"pyc")
    (tree / ".pytest_cache").mkdir()
    (tree / ".DS_Store").write_bytes(b"finder")
    (tree / ".claude/worktrees/w1/pheromone.db").write_bytes(b"nested worktree wrote its own db")
    assert g.diff(before, g.fingerprint(str(tree))) == {}



# ───────────────── 1c. 「测试碰不到」写成断言（v0.45.427） ─────────────────
#
# 闸对 `_` 元目录的豁免，理由曾只是一句注释「不是测试能碰到的」。v0.45.408 加 `PATHS.data_backup_repo` 那天它就不成立了
# （指向 `_git_backup`），注释不会执行、没人红；10-03 夹具就是经它写进了生产备份仓库。这里把前提变成断言：
# 枚举 `PATHS` 每个属性在沙箱数据根下解析到哪，落在豁免区的必须**恰好**是下面这几个（等式：多了 = 新的盲区，少了 = 白名单过期）。

#: PATHS 属性落在数据根豁免区、但隔离另有人负责：属性名 → 谁负责。往这里加之前先问：它真的不能受闸吗？
PATHS_IN_EXEMPT_AREAS_OK = {
    "logs_dir": "日志常驻有写入方（MCP / 扫描），无法指纹；测试隔离靠 `_isolate_env` 的 ALPHA_HIVE_LOGS_DIR + v0.45.239 日志隔离守卫",
    "production_sync": "落在 logs/ 下，同上",
}

#: 会把 PATHS 属性挪出缺省布局的覆盖变量（conftest 逐条设成沙箱）——看缺省布局要先清掉。
_PATHS_OVERRIDE_VARS = ("ALPHA_HIVE_LOGS_DIR", "ALPHA_HIVE_CACHE_DIR", "ALPHA_HIVE_DB_PATH", "ALPHA_HIVE_CHROMA_PATH")


def _paths_relpaths_under(root: str) -> dict:
    from hive_logger import PATHS
    out = {}
    for name, attr in vars(type(PATHS)).items():
        if not isinstance(attr, property):
            continue
        v = os.path.realpath(str(getattr(PATHS, name)))
        if v == root or v.startswith(root + os.sep):
            out[name] = os.path.relpath(v, root)
    return out


def test_paths_reaching_exempt_areas_are_exactly_the_known_ones(tmp_path, monkeypatch):
    root = tmp_path / "dr"
    root.mkdir()
    monkeypatch.setenv("ALPHA_HIVE_HOME", str(root))
    for k in _PATHS_OVERRIDE_VARS:
        monkeypatch.delenv(k, raising=False)
    rel = _paths_relpaths_under(os.path.realpath(root))
    assert {"db", "data_backup_repo", "logs_dir"} <= set(rel), f"枚举没接上 PATHS：{rel}"
    exempt = g.exempt_reachable_paths(rel)
    assert set(exempt) == set(PATHS_IN_EXEMPT_AREAS_OK), (
        f"PATHS 落在闸豁免区的属性 = {exempt}，白名单 = {sorted(PATHS_IN_EXEMPT_AREAS_OK)}。\n"
        "多出来的：测试经它写进真实数据根，闸看不见——让它受闸（见 `DATA_ROOT_WATCHED_META`），别往白名单加；"
        "少了的：白名单过期，删掉那一项。")


def test_exempt_reachable_check_has_teeth():
    """对照组：判据真分得开「受闸」与「豁免」，上一条的等式才说明得了什么。"""
    assert g.exempt_reachable_paths({
        "a": "_archive/x", "b": "_git_backup", "c": "cache/y", "d": ".", "e": "logs/z", "f": "_git_backup/.git",
    }) == {"a": "meta-dir:_archive", "e": "volatile:logs", "f": "skip-dir:.git"}


# ───────────────── 2b. 真实数据根：指纹比对（合成数据根） ─────────────────

@pytest.fixture
def data_root(tmp_path):
    r = tmp_path / "alpha-hive-data"
    for rel, data in {
        "pheromone.db": b"db", "pheromone.db-wal": b"wal", "pheromone.db-shm": b"shm",
        "report_snapshots/a.json": b"{}", "paper_portfolio_state/meta.json": b"{}",
        "logs/alpha_hive.log": b"line\n", "db_backups/pheromone_2026-09-27.db": b"old",
        "_git_backup/x": b"1", "_git_backup/.git/HEAD": b"ref: refs/heads/main\n", "_archive/pheromone_bak/a.db": b"a",
    }.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_bytes(data)
    return r


def _dfp(root):
    return g.fingerprint(str(root), g.data_root_excluded_reason)


def _move_fixture_repo_into_backup(r):
    """10-03 的原形：带 `.git` 的夹具仓库被 `shutil.move` 到一个**已存在**的 `_git_backup` ⇒ 被搬**进去**成了子目录。"""
    src = r.parent / "bk_src"
    (src / ".git").mkdir(parents=True)
    (src / ".git/HEAD").write_bytes(b"ref: refs/heads/main\n")
    (src / "paper_portfolio_state").mkdir()
    (src / "paper_portfolio_state/meta.json").write_bytes(b'{"version": "test"}')
    shutil.move(str(src), str(r / "_git_backup"))
    assert (r / "_git_backup/bk_src/.git").is_dir(), "前提：shutil.move 进已存在目录 ⇒ 嵌进去"


@pytest.mark.parametrize("mutate,kind,path", [
    (lambda r: (r / "pheromone.db").write_bytes(b"dbX"), "changed", "pheromone.db"),                # 账本多一字节
    (lambda r: (r / "paper_portfolio_state/meta.json").write_bytes(b"{1}"), "changed", "paper_portfolio_state/meta.json"),
    (lambda r: (r / "report_snapshots/b.json").write_bytes(b"{}"), "added", "report_snapshots/b.json"),
    (lambda r: (r / "brand_new.json").write_bytes(b"x"), "added", "brand_new.json"),                 # 默认拒绝
    (lambda r: (r / "new_dir").mkdir(), "added", "new_dir"),                                         # 只建空目录也算写
    (lambda r: (r / "report_snapshots/a.json").unlink(), "removed", "report_snapshots/a.json"),
    (lambda r: (r / "pheromone.db-wal").unlink(), "removed", "pheromone.db-wal"),
    (_move_fixture_repo_into_backup, "added", "_git_backup/bk_src"),                                 # 10-03 事故原形
    (lambda r: (r / "_git_backup/x").write_bytes(b"2"), "changed", "_git_backup/x"),
], ids=["append-ledger", "rewrite-state", "new-snapshot", "new-top-level", "empty-dir", "delete", "wal-vanishes",
        "fixture-repo-into-backup", "rewrite-backup-file"])
def test_data_root_write_shapes_are_caught(data_root, mutate, kind, path):
    before = _dfp(data_root)
    time.sleep(0.01)
    mutate(data_root)
    assert path in g.diff(before, _dfp(data_root)).get(kind, []), g.diff(before, _dfp(data_root))


def test_data_root_benign_activity_is_not_a_write(data_root):
    """正对照：日志轮转、备份轮转、备份仓的 git 簿记、迁移留档、读者往 -shm 写标记——都不是测试写穿。
    （v0.45.427 前这里把「`_git_backup/x` 被改写」也当良性——那正是 10-03 夹具写进生产备份仓库的形状。）"""
    before = _dfp(data_root)
    time.sleep(0.01)
    (data_root / "logs/alpha_hive.log").write_bytes(b"line\nmore\n")
    (data_root / "logs/new.log").write_bytes(b"x")
    (data_root / "db_backups/pheromone_2026-09-28.db").write_bytes(b"new")
    (data_root / "_git_backup/.git/index").write_bytes(b"idx")    # git 簿记：备份仓的 .git 不看
    (data_root / "_archive/pheromone_bak/b.db").write_bytes(b"b")
    (data_root / "pheromone.db-shm").write_bytes(b"SHM")          # 同大小：读者标记
    (data_root / ".DS_Store").write_bytes(b"finder")
    assert g.diff(before, _dfp(data_root)) == {}


# ─────────────────────────────── 3. 接线（真跑 pytest） ───────────────────────────────

def _make_fake_checkout(root: Path, test_body: str) -> Path:
    (root / "tests").mkdir(parents=True)
    shutil.copy2(_TESTS / "conftest.py", root / "tests" / "conftest.py")
    for helper in _TESTS.glob("_*.py"):   # conftest import 的辅助模块全带上（v0.45.573：写死清单每加一个就坏一次）
        shutil.copy2(helper, root / "tests" / helper.name)
    (root / "probability_scorecard_state").mkdir()
    (root / "probability_scorecard_state/published.jsonl").write_bytes(b"{}\n")
    (root / "report_snapshots").mkdir()
    (root / "report_snapshots/a.json").write_bytes(b"{}")
    (root / "mod.py").write_bytes(b"x = 1\n")
    (root / "templates").mkdir()
    (root / "templates/t.html").write_bytes(b"<p>")
    (root / "tests" / "test_inner.py").write_text(textwrap.dedent(test_body), encoding="utf-8")
    return root


def _run_inner_pytest(root: Path, extra_env: dict | None = None, header: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 默认让内层 pytest **看不到**真实数据根：HOME 指向空目录、不带 ALPHA_HIVE_HOME。
    # 否则在这台机器上，内层的真实数据根闸会去盯 ~/alpha-hive-data（生产数据，可能正被扫描写着）——
    # 既会让这些仓库根用例随生产状态忽红忽绿，也等于让测试去碰真数据。
    env.pop("ALPHA_HIVE_HOME", None)
    env["PYTHONUSERBASE"] = site.getuserbase()       # 换了 HOME 之后用户级 site-packages（pytest 在那）要靠它找回
    env["HOME"] = str(root.parent / "empty_home")
    (root.parent / "empty_home").mkdir(exist_ok=True)
    env.update(extra_env or {})
    # 生产模块从真仓库 import（conftest 的 autouse fixture 要 import llm_service 等）；
    # 被守的根目录却是这个假 checkout——conftest 按自己的 __file__ 定根。
    env["PYTHONPATH"] = str(_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # --tb=native：默认的 long traceback 会把 conftest 断言的**源码**整段打出来，
    # 源码里就有报错文案与路径字样 ⇒ 下面的子串断言会被源码满足、证明不了运行时真报了什么。
    # native 只打语句首行，剩下的文字只可能来自运行时消息。
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *(["-v"] if header else ["-q", "--no-header"]),
         "--tb=native", "tests/test_inner.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=45)


def test_session_gate_is_red_on_real_writes(tmp_path):
    """红组：收集期写一个文件 + 测试里往账本追加一字节 + 改一个未列出的数据文件 + 新建目录。"""
    root = _make_fake_checkout(tmp_path / "checkout", """
        import pathlib
        ROOT = pathlib.Path(__file__).resolve().parents[1]
        (ROOT / "written_at_collection.json").write_text("x")   # 收集期（模块 import 时）落盘

        def test_writes_into_checkout():
            with open(ROOT / "probability_scorecard_state/published.jsonl", "ab") as f:
                f.write(b"x")
            with open(ROOT / "report_snapshots/a.json", "ab") as f:
                f.write(b"x")
            (ROOT / "brand_new_dir").mkdir()
            (ROOT / "brand_new_dir/f.bin").write_bytes(b"x")
    """)
    r = _run_inner_pytest(root)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "1 passed, 1 error" in out, f"应是测试本身通过、总闸在 teardown 报错：\n{out}"
    assert "本轮期间**仓库根**的非代码内容被改动了" in out, out
    # 逐行核对运行时 diff（顺序 + 内容），而不只是「路径字样出现过」。
    # 去掉行首尾空白再比：native traceback 会给消息续行多缩进两格。
    expected = "\n".join(["added（3）：", "brand_new_dir", "brand_new_dir/f.bin", "written_at_collection.json",
                          "changed（2）：", "probability_scorecard_state/published.jsonl",
                          "report_snapshots/a.json"])
    normalized = "\n".join(line.strip() for line in out.splitlines())
    assert expected in normalized, f"总闸报出的 diff 与预期不符：\n{out}"
    assert "sessionstart 未生效" not in out, "「之前」没取在收集期之前——收集期写入会被算进基线"


def test_session_gate_is_green_on_benign_run(tmp_path):
    """对照组：只写 tmp_path、改代码、改模板、产生工具缓存——必须绿。不绿则红组的红说明不了任何事。"""
    root = _make_fake_checkout(tmp_path / "checkout", """
        import pathlib
        ROOT = pathlib.Path(__file__).resolve().parents[1]

        def test_benign(tmp_path):
            (tmp_path / "sandboxed.json").write_text("fine")
            with open(ROOT / "mod.py", "a") as f:
                f.write("# edited by a developer mid-run\\n")
            with open(ROOT / "templates/t.html", "a") as f:
                f.write("<!-- edited -->")
            (ROOT / "__pycache__").mkdir(exist_ok=True)
            (ROOT / "__pycache__/x.pyc").write_bytes(b"pyc")
    """)
    t0 = time.time()
    r = _run_inner_pytest(root)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 passed" in out, out
    assert time.time() - t0 < 45


# ───────────────── 3b. 真实数据根：接线（真跑 pytest，原样拷贝的 conftest） ─────────────────

def _make_data_root(path: Path) -> Path:
    for rel, data in {"pheromone.db": b"db", "report_snapshots/a.json": b"{}",
                      "paper_portfolio_state/meta.json": b"{}", "logs/alpha_hive.log": b"l\n",
                      "_archive/a": b"a"}.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(data)
    return path


def test_real_data_root_gate_is_red_on_escaping_writes(tmp_path):
    """红组：测试绕过隔离、写到真实数据根（追加账本 + 新建产物 + 新建目录）；收集期还写一个文件。
    数据根路径经 `INNER_DATA_ROOT` 传入（内层 `_isolate_env` 会把 ALPHA_HIVE_HOME 改成沙箱，读不到它）。"""
    data_root = _make_data_root(tmp_path / "alpha-hive-data")
    root = _make_fake_checkout(tmp_path / "checkout", """
        import os, pathlib
        DR = pathlib.Path(os.environ["INNER_DATA_ROOT"])
        (DR / "written_at_collection.json").write_text("x")      # 收集期落盘

        def test_escapes_the_sandbox():
            with open(DR / "pheromone.db", "ab") as f:
                f.write(b"x")
            (DR / "report_snapshots/b.json").write_text("{}")
            (DR / "new_dir").mkdir()
    """)
    r = _run_inner_pytest(root, {"ALPHA_HIVE_HOME": str(data_root), "INNER_DATA_ROOT": str(data_root)})
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "1 passed, 1 error" in out, f"应是测试本身通过、总闸在 teardown 报错：\n{out}"
    assert "本轮期间**真实数据根**" in out and "若是测试写的" in out, out
    expected = "\n".join(["added（3）：", "new_dir", "report_snapshots/b.json", "written_at_collection.json",
                          "changed（1）：", "pheromone.db"])
    normalized = "\n".join(line.strip() for line in out.splitlines())
    assert expected in normalized, f"总闸报出的 diff 与预期不符：\n{out}"


def test_real_data_root_gate_is_red_on_fixture_repo_moved_into_backup(tmp_path):
    """10-03 事故原形端到端（v0.45.427）：测试把带 `.git` 的夹具仓库 `shutil.move` 到**已存在**的 `_git_backup`
    ⇒ 被搬进去成了子目录。v0.45.427 前闸整个豁免 `_git_backup`，这一轮是绿的（夹具随后被每日备份提交进备份历史）。"""
    data_root = _make_data_root(tmp_path / "alpha-hive-data")
    (data_root / "_git_backup/.git").mkdir(parents=True)
    (data_root / "_git_backup/MANIFEST.json").write_bytes(b"{}")
    root = _make_fake_checkout(tmp_path / "checkout", """
        import os, pathlib, shutil
        DR = pathlib.Path(os.environ["INNER_DATA_ROOT"])

        def test_moves_a_fixture_repo_into_the_backup(tmp_path):
            src = tmp_path / "bk_src"
            (src / ".git").mkdir(parents=True)
            (src / "paper_portfolio_state").mkdir()
            (src / "paper_portfolio_state/meta.json").write_text('{"version": "test"}')
            shutil.move(str(src), str(DR / "_git_backup"))
    """)
    r = _run_inner_pytest(root, {"ALPHA_HIVE_HOME": str(data_root), "INNER_DATA_ROOT": str(data_root)})
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "1 passed, 1 error" in out, f"应是测试本身通过、总闸在 teardown 报错：\n{out}"
    expected = "\n".join(["added（3）：", "_git_backup/bk_src", "_git_backup/bk_src/paper_portfolio_state",
                          "_git_backup/bk_src/paper_portfolio_state/meta.json"])
    normalized = "\n".join(line.strip() for line in out.splitlines())
    assert expected in normalized, f"总闸报出的 diff 与预期不符（夹具的 .git 应被跳过）：\n{out}"


def test_real_data_root_gate_is_green_on_benign_run(tmp_path):
    """对照组：只写 tmp_path、写日志与元目录、读真实数据根——必须绿；且 header 明说闸在生效。
    不绿则红组的红说明不了任何事。"""
    data_root = _make_data_root(tmp_path / "alpha-hive-data")
    root = _make_fake_checkout(tmp_path / "checkout", """
        import os, pathlib
        DR = pathlib.Path(os.environ["INNER_DATA_ROOT"])

        def test_benign(tmp_path):
            (tmp_path / "sandboxed.json").write_text("fine")
            assert (DR / "pheromone.db").read_bytes() == b"db"            # 只读
            with open(DR / "logs/alpha_hive.log", "a") as f:               # 常驻写入方的地盘
                f.write("more\\n")
            (DR / "_archive/b").write_bytes(b"b")
    """)
    r = _run_inner_pytest(root, {"ALPHA_HIVE_HOME": str(data_root), "INNER_DATA_ROOT": str(data_root)}, header=True)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 passed" in out, out
    assert f"real-data-root guard: active on {data_root}" in out, f"header 没说闸在生效：\n{out}"


def test_real_data_root_gate_says_inactive_when_there_is_no_data_root(tmp_path):
    """没有真实数据根（CI / 干净克隆）：不报错，但 header 必须明说 INACTIVE——不许把「没保护」渲染成「保护着」。"""
    root = _make_fake_checkout(tmp_path / "checkout", """
        def test_nothing_to_guard():
            assert True
    """)
    r = _run_inner_pytest(root, header=True)        # 默认 HOME 是空目录、无 ALPHA_HIVE_HOME
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "real-data-root guard: INACTIVE" in out, out


# ───────────────── 4. Alpha Bot 盘中追加：只改措辞、不改红绿（v0.45.453） ─────────────────

_ET = dt.timezone(dt.timedelta(hours=-4))


def _snap(ts: dt.datetime, **extra) -> dict:
    rec = {"ts": ts.isoformat(timespec="seconds"), "payload_last_trade_time": "2026-10-09T15:50:00",
           "underlying_price": 100.0, "underlying_price_source": "cboe_intraday", "session_live": True,
           "iv30": 0.3, "views": {}, "route": {}}
    rec.update(extra)
    return rec


def _line(rec) -> bytes:
    return (json.dumps(rec) + "\n").encode()


class TestAlphabotAppendClassifier:
    """归因要逐项核实：九种「看起来像、其实不是」都必须落进「其余」（宁可报成测试写的）。"""

    @staticmethod
    def _setup(tmp_path, *, with_file=True, tz=_ET):
        root = tmp_path / "dr"
        now = dt.datetime.now(tz)
        rel = f"alphabot_state/intraday/{now.date().isoformat()}/NVDA.jsonl"
        (root / "alphabot_state/intraday").mkdir(parents=True)    # 生产里早就有（每天的日期目录建在它下面）
        (root / "pheromone.db").write_bytes(b"db")
        if with_file:
            (root / rel).parent.mkdir(parents=True)
            (root / rel).write_bytes(_line(_snap(now - dt.timedelta(hours=1))))
        before = g.fingerprint(str(root), g.data_root_excluded_reason)
        # 截止日按目录那天算（不用本机日期）：UTC+13/+14 的机器上，本机「今天 − 1」会晚于 UTC−12 目录的日期
        baseline = g.alphabot_baseline(str(root), before, now.date())
        started = dt.datetime.now(dt.timezone.utc)
        # 用例追加的快照一律用这个 now：再取一次「现在」，跨美东午夜时日期就和目录对不上（偶发红）
        return root, rel, before, baseline, started, now

    @staticmethod
    def _explain(root, before, baseline, started):
        time.sleep(0.01)    # mtime_ns 要能分辨
        changes = g.diff(before, g.fingerprint(str(root), g.data_root_excluded_reason))
        return changes, g.explain_alphabot_appends(str(root), changes, before, baseline, started,
                                                   dt.datetime.now(dt.timezone.utc))

    def test_append_by_alphabot_is_explained(self, tmp_path):
        root, rel, before, baseline, started, now = self._setup(tmp_path)
        assert rel in baseline, "夹具前提：起点哈希记下了当天的盘中文件"
        with open(root / rel, "ab") as f:
            f.write(_line(_snap(now)) + _line(_snap(now)))
        changes, v = self._explain(root, before, baseline, started)
        assert changes == {"changed": [rel]}
        assert v["explained"] == [rel] and v["records"] == 2 and v["unexplained"] == {} and v["why"] == {}

    def test_first_batch_of_a_new_day_is_explained(self, tmp_path):
        """开盘第一批：日期目录与文件都是新建的。"""
        root, rel, before, baseline, started, now = self._setup(tmp_path, with_file=False)
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_bytes(_line(_snap(now)))
        changes, v = self._explain(root, before, baseline, started)
        day = str(Path(rel).parent)
        assert changes == {"added": sorted([day, rel])}
        assert v["explained"] == sorted([day, rel]) and v["unexplained"] == {}

    @pytest.mark.parametrize("case", ["rewrite_prefix", "ts_outside_window", "extra_key", "not_json",
                                      "partial_line", "no_baseline", "outside_intraday", "removed", "truncated",
                                      "demo_source"])
    def test_lookalikes_are_not_explained(self, tmp_path, case):
        root, rel, before, baseline, started, now = self._setup(tmp_path)
        p = root / rel
        target = rel
        if case == "rewrite_prefix":
            p.write_bytes(_line(_snap(now - dt.timedelta(hours=2))) + _line(_snap(now)))
        elif case == "ts_outside_window":
            with open(p, "ab") as f:
                f.write(_line(_snap(now - dt.timedelta(minutes=30))))
        elif case == "extra_key":
            with open(p, "ab") as f:
                f.write(_line(_snap(now, written_by="a test")))
        elif case == "not_json":
            with open(p, "ab") as f:
                f.write(b"hello\n")
        elif case == "partial_line":
            with open(p, "ab") as f:
                f.write(json.dumps(_snap(now)).encode())
        elif case == "no_baseline":
            baseline = {}
            with open(p, "ab") as f:
                f.write(_line(_snap(now)))
        elif case == "outside_intraday":
            target = "alphabot_state/settings.json"
            (root / target).write_text("{}")
        elif case == "removed":
            p.unlink()
        elif case == "truncated":
            p.write_bytes(b"")
        elif case == "demo_source":
            # v0.45.455/460：用演示服务**真写**一行（与 test_alphabot 的 IntradayPoller 用例同一条路径：真时钟、
            # 合成数据），而不是手写——手写的行证明不了真写出来的长这样。写进 tmp 的 state_dir，再接到夹具文件后面。
            from alphabot.service import AlphaBotService
            import alphabot.synthetic as syn
            svc = AlphaBotService(fetch_fn=syn.demo_fetch, state_dir=tmp_path / "ab_state")
            assert svc.state_dir() == tmp_path / "ab_state", "夹具前提：写进 tmp，不经 PATHS"
            assert svc.take_snapshot("NVDA", now=now)["recorded"] is True
            written = (tmp_path / "ab_state/intraday" / now.date().isoformat() / "NVDA.jsonl").read_bytes()
            rec = json.loads(written)
            assert set(rec) == g.ALPHABOT_SNAPSHOT_KEYS and rec["underlying_price_source"] == g.ALPHABOT_DEMO_SOURCE, \
                f"夹具前提：演示服务真写出的行键集合对得上、来源是 demo——否则这条测的不是那个缺口：{rec}"
            with open(p, "ab") as f:
                f.write(written)
        _changes, v = self._explain(root, before, baseline, started)
        assert v["explained"] == [], f"{case}：被错误归因成 Alpha Bot"
        assert any(target in items for items in v["unexplained"].values()), v
        if case not in ("removed",):
            assert target in v["why"], f"{case}：Alpha Bot 目录下没能归因的项要给出原因"

    def test_snapshot_date_must_match_the_directory(self, tmp_path):
        """时刻在窗口内、但按记录自己的时区落在另一天（-12 与 +14 相差 26 小时，日期必不同）。"""
        root, rel, before, baseline, started, now = self._setup(tmp_path, tz=dt.timezone(dt.timedelta(hours=-12)))
        with open(root / rel, "ab") as f:
            f.write(_line(_snap(dt.datetime.now(dt.timezone(dt.timedelta(hours=14))))))
        _changes, v = self._explain(root, before, baseline, started)
        assert v["explained"] == [] and "目录日期" in v["why"][rel], v

    def test_mixed_changes_name_the_rest(self, tmp_path):
        root, rel, before, baseline, started, now = self._setup(tmp_path)
        with open(root / rel, "ab") as f:
            f.write(_line(_snap(now)))
        with open(root / "pheromone.db", "ab") as f:
            f.write(b"x")
        _changes, v = self._explain(root, before, baseline, started)
        assert v["explained"] == [rel] and v["unexplained"] == {"changed": ["pheromone.db"]}

    def test_without_a_session_start_nothing_is_explained(self, tmp_path):
        root, rel, before, baseline, _started, now = self._setup(tmp_path)
        with open(root / rel, "ab") as f:
            f.write(_line(_snap(now)))
        _changes, v = self._explain(root, before, baseline, None)
        assert v["explained"] == [] and v["unexplained"] == {"changed": [rel]}

    def test_append_landing_between_fingerprint_and_baseline_is_still_explained(self, tmp_path):
        """二次检查发现的竞态：会话开始时先拍指纹、后算起点哈希，Alpha Bot 恰在两者之间追加一行。
        旧实现按指纹里的旧大小比前缀 ⇒ 哈希必不符 ⇒ 一次真的 Alpha Bot 写入被说成「原有内容被改写」。"""
        root = tmp_path / "dr"
        now = dt.datetime.now(_ET)
        rel = f"alphabot_state/intraday/{now.date().isoformat()}/NVDA.jsonl"
        (root / rel).parent.mkdir(parents=True)
        (root / rel).write_bytes(_line(_snap(now - dt.timedelta(hours=1))))
        before = g.fingerprint(str(root), g.data_root_excluded_reason)        # ① 指纹
        with open(root / rel, "ab") as f:                                      # Alpha Bot 恰在 ①② 之间写
            f.write(_line(_snap(now)))
        baseline = g.alphabot_baseline(str(root), before, now.date())          # ② 起点哈希
        started = dt.datetime.now(dt.timezone.utc)
        with open(root / rel, "ab") as f:                                      # 会话期间再一批
            f.write(_line(_snap(now)))
        _changes, v = self._explain(root, before, baseline, started)
        assert v["explained"] == [rel] and v["records"] == 2 and v["why"] == {}, v

    def test_deeply_nested_line_is_unexplained_not_a_crash(self, tmp_path):
        root, rel, before, baseline, started, now = self._setup(tmp_path)
        with open(root / rel, "ab") as f:
            f.write(b"[" * 100_000 + b"\n")                                 # json.loads 抛 RecursionError
        _changes, v = self._explain(root, before, baseline, started)
        assert v["explained"] == [] and v["why"][rel] == "新增行不是 JSON", v

    def test_keys_match_the_writer(self):
        """键集合必须就是 Alpha Bot 写的那一份——它改了字段而这里没跟，归因就会恒失败（退回原措辞，不会误放）。"""
        from alphabot.service import AlphaBotService
        assert set(AlphaBotService.snapshot_from({}, "2026-10-09T15:50:35-04:00")) == g.ALPHABOT_SNAPSHOT_KEYS


def _make_data_root_with_alphabot(path: Path) -> tuple[Path, str]:
    data_root = _make_data_root(path)
    now = dt.datetime.now(_ET)
    rel = f"alphabot_state/intraday/{now.date().isoformat()}/NVDA.jsonl"
    (data_root / rel).parent.mkdir(parents=True)
    (data_root / rel).write_bytes(_line(_snap(now - dt.timedelta(hours=1))))
    return data_root, rel


_INNER_ALPHABOT_APPEND = """
    import datetime as dt, json, os, pathlib
    DR = pathlib.Path(os.environ["INNER_DATA_ROOT"])
    REL = os.environ["INNER_ALPHABOT_REL"]

    def _append_like_alphabot():
        et = dt.timezone(dt.timedelta(hours=-4))
        now = dt.datetime.now(et)
        day = REL.split("/")[2]
        if now.date().isoformat() != day:            # 外层建目录后跨过了美东午夜：贴着目录那天的末尾记
            now = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(23, 59, 59), tzinfo=et)
        rec = {"ts": now.isoformat(timespec="seconds"), "payload_last_trade_time": "t", "underlying_price": 1.0,
               "underlying_price_source": "cboe_intraday", "session_live": True, "iv30": 0.3, "views": {}, "route": {}}
        with open(DR / REL, "a", encoding="utf-8") as f:     # 模拟测试期间 Alpha Bot 落盘的一批
            f.write(json.dumps(rec) + "\\n")
"""


def _gate_plugin_env(tmp_path: Path) -> tuple[dict, Path]:
    """让内层 pytest 加载 pre-push 契约闸**原样**的报告插件——真 conftest 挂的归因要能被真插件读到。"""
    sys.path.insert(0, str(_ROOT))
    import changelog_guard as cg
    plug = tmp_path / "gate_plugin"
    plug.mkdir()
    (plug / "_alpha_hive_contract_gate_report.py").write_text(cg._REPORT_PLUGIN, encoding="utf-8")
    report = tmp_path / "gate_report.json"
    return ({"PYTHONPATH": str(plug), "PYTEST_ADDOPTS": "-p _alpha_hive_contract_gate_report",
             "ALPHA_HIVE_CONTRACT_GATE_REPORT": str(report)}, report)


def test_real_data_root_gate_names_alphabot_but_stays_red(tmp_path):
    """端到端：变化只有 Alpha Bot 式追加 ⇒ 照样红，但说清是 Alpha Bot、劝别 --no-verify，且不再说「测试写穿」。"""
    data_root, rel = _make_data_root_with_alphabot(tmp_path / "alpha-hive-data")
    root = _make_fake_checkout(tmp_path / "checkout", _INNER_ALPHABOT_APPEND + """
    def test_alphabot_writes_meanwhile():
        _append_like_alphabot()
    """)
    gate_env, report = _gate_plugin_env(tmp_path)
    r = _run_inner_pytest(root, {"ALPHA_HIVE_HOME": str(data_root), "INNER_DATA_ROOT": str(data_root),
                                 "INNER_ALPHABOT_REL": rel, **gate_env})
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"判定被改了：只有 Alpha Bot 追加也必须红\n{out}"
    assert "1 passed, 1 error" in out, out
    assert "**全部**是 Alpha Bot 盘中采样的追加" in out and "1 张快照" in out, out
    assert "不要** `git push --no-verify`" in out, out
    assert "若是测试写的" not in out and "本轮期间**真实数据根**" not in out, \
        f"已核实是 Alpha Bot，却仍用怀疑测试写穿的措辞\n{out}"
    # 真 conftest → 真报告插件 → 真判定函数：pre-push 会据此改提示（不劝 --no-verify）
    import changelog_guard as cg
    rep = json.loads(report.read_text(encoding="utf-8"))
    assert rep["real_data_root_verdict"] == {"all_alphabot_appends": True, "alphabot_files": 1,
                                             "alphabot_records": 1, "unexplained": 0}, rep
    assert cg._alphabot_only_red(rep, [f"失败：{n}" for n in rep["failed"]]) is not None, rep


def test_real_data_root_gate_keeps_blaming_tests_when_anything_else_changed(tmp_path):
    """端到端：Alpha Bot 追加 + 测试写穿另一个文件 ⇒ 原措辞，且把「不能归因」的那项单独点名。"""
    data_root, rel = _make_data_root_with_alphabot(tmp_path / "alpha-hive-data")
    root = _make_fake_checkout(tmp_path / "checkout", _INNER_ALPHABOT_APPEND + """
    def test_alphabot_writes_and_a_test_escapes():
        _append_like_alphabot()
        with open(DR / "pheromone.db", "ab") as f:
            f.write(b"x")
    """)
    gate_env, report = _gate_plugin_env(tmp_path)
    r = _run_inner_pytest(root, {"ALPHA_HIVE_HOME": str(data_root), "INNER_DATA_ROOT": str(data_root),
                                 "INNER_ALPHABOT_REL": rel, **gate_env})
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "本轮期间**真实数据根**" in out and "若是测试写的" in out, out
    rep = json.loads(report.read_text(encoding="utf-8"))
    assert rep["real_data_root_verdict"]["all_alphabot_appends"] is False, rep
    assert "**全部**是 Alpha Bot" not in out, out
    normalized = "\n".join(line.strip() for line in out.splitlines())
    assert "其余这些不能归因于它，问题在这里**：\nchanged（1）：\npheromone.db" in normalized, out


# ───────────── 5. session 级闸逐文件标出「写入时刻在跑哪条测试」（v0.45.455） ─────────────
#
# 10-09：连续三轮红在同一条测试上，被当成确定性泄漏去查——那只是收集序最后一条，写入者是 Alpha Bot。
# 指纹里本来有 mtime；这里证明它被对到了时间线上，且措辞只说「时间重合」不说「是它写的」。

class TestTimelineLocate:
    TL = [(10.0, 11.0, "t.py::a", "setup"), (11.0, 12.0, "t.py::a", "call"),
          (12.0, 13.0, "t.py::a", "teardown"), (14.0, 15.0, "t.py::b", "call")]

    @pytest.mark.parametrize("t, expected", [
        (None, "定位不了"),
        (4.0, "早于本轮开始"),
        (7.0, "收集期"),
        (11.5, "t.py::a 的 call 阶段"),
        (13.5, "两段之间（紧接 t.py::a 的 teardown 之后）"),
        (14.5, "t.py::b 的 call 阶段"),
        (16.0, "最后一段记录之后——即 t.py::b 的 teardown 阶段"),
    ])
    def test_positions(self, t, expected):
        assert expected in g.locate(t, self.TL, session_start=5.0)

    def test_no_timeline(self):
        assert "收集期或会话收尾" in g.locate(7.0, [], session_start=5.0)
        assert "定位不了" in g.locate(7.0, [], session_start=None)

    def test_unknown_session_start_does_not_claim_collection(self):
        """v0.45.460：开始时刻未知时，第一条测试之前的写入不许断言是「收集期」（分不出）。"""
        label = g.locate(7.0, self.TL, session_start=None)
        assert "收集期" not in label and "开始时刻未知" in label
        assert "t.py::a 的 call 阶段" in g.locate(11.5, self.TL, session_start=None)

    # ── v0.45.507：mtime 比 `time.time()` 粗（Linux 内核按 tick 盖时间戳），真实写入时刻在 [t, t + lag] ──
    def test_lag_window_inside_one_segment_names_only_it(self):
        label = g.locate(11.5, self.TL, session_start=5.0, lag=0.004)
        assert label == "t.py::a 的 call 阶段", label

    def test_lag_window_straddling_setup_and_call_names_both(self):
        """CI 上的那一幕：call 开头的写入，mtime 落回 setup 末尾——不许只报 setup。"""
        label = g.locate(10.998, self.TL, session_start=5.0, lag=0.004)
        assert "t.py::a 的 setup 阶段" in label and "t.py::a 的 call 阶段" in label, label
        assert "分不开" in label and "4 ms" in label, label

    def test_lag_window_keeps_segments_in_the_middle(self):
        """窗口跨三段：中间那段不许因为只看两端而丢掉。"""
        label = g.locate(10.9, self.TL, session_start=5.0, lag=1.2)
        for when in ("setup", "call", "teardown"):
            assert f"t.py::a 的 {when} 阶段" in label, label

    def test_lag_window_spanning_many_segments_is_abbreviated(self):
        label = g.locate(10.5, self.TL, session_start=5.0, lag=10.0)
        assert label.startswith("t.py::a 的 setup 阶段") and "中间 4 段" in label and "最后一段记录之后" in label, label

    def test_lag_window_at_session_start_is_not_claimed_as_old_mtime(self):
        """本轮开始之后一个 tick 内写的文件，mtime 可能早于开始时刻——不许只报「保留了旧 mtime」。"""
        label = g.locate(4.999, self.TL, session_start=5.0, lag=0.004)
        assert "早于本轮开始" in label and "收集期" in label, label
        assert g.locate(4.0, self.TL, session_start=5.0, lag=0.004).startswith("早于本轮开始")
        assert g.locate(4.999, [], session_start=5.0, lag=0.004).startswith("本轮没有任何测试报告")

    def test_zero_lag_keeps_point_semantics_on_shared_boundaries(self):
        """lag=0 与 v0.45.455 逐点规则逐字一致：相邻两段共用的端点归前一段，本轮开始时刻归收集期。"""
        tl = [(10.0, 11.0, "t.py::a", "setup"), (11.0, 12.0, "t.py::a", "call")]
        assert g.locate(11.0, tl, session_start=5.0) == "t.py::a 的 setup 阶段"
        assert g.locate(5.0, tl, session_start=5.0).startswith("收集期")

    def test_format_timeline_applies_this_machines_mtime_lag(self, monkeypatch):
        """报错里那一节确实按 `mtime_lag()` 放宽——只测 `locate` 接不住「调用方忘了传 lag」。"""
        sig = {"x.json": f"1:{int(10.998 * 1e9)}"}
        monkeypatch.setattr(g, "mtime_lag", lambda: 0.004)
        both = g.format_timeline("/nonexistent", {"changed": ["x.json"]}, sig, self.TL, 5.0)
        assert "t.py::a 的 setup 阶段" in both and "t.py::a 的 call 阶段" in both, both
        monkeypatch.setattr(g, "mtime_lag", lambda: 0.0)
        assert "t.py::a 的 call 阶段" not in g.format_timeline("/nonexistent", {"changed": ["x.json"]}, sig, self.TL, 5.0)

    def test_mtime_lag_bounds_this_machine(self, tmp_path):
        """`mtime_lag` 的前提实测（它是放宽多少的唯一依据）：本机写文件，mtime 不早于写前读的 `time.time()` 超过它。
        v0.45.455 只写了一句「与文件 mtime 同为墙钟」，没人量过——Linux 上不成立，两条时间线测试在 CI 恒红。"""
        lag, probe, worst = g.mtime_lag(), tmp_path / "probe", 0.0
        for _ in range(500):
            before = time.time()
            with open(probe, "ab") as f:
                f.write(b"x")
            worst = max(worst, before - os.stat(probe).st_mtime_ns / 1e9)
        assert worst <= lag + 1e-6, f"mtime 比写前的 time.time() 早了 {worst * 1000:.3f} ms，超出 mtime_lag()={lag * 1000:.3f} ms"

    def test_change_mtime_reads_file_signature_and_stats_dirs(self, tmp_path):
        (tmp_path / "d").mkdir()
        assert g.change_mtime(str(tmp_path), "f", "3:1500000000000000000") == 1.5e9
        assert g.change_mtime(str(tmp_path), "d", "dir") == os.lstat(tmp_path / "d").st_mtime_ns / 1e9
        assert g.change_mtime(str(tmp_path), "gone", None) is None


def _labels(out: str) -> dict[str, str]:
    """报错里「逐项写入时刻」那几行 → {路径: 位置}。"""
    res = {}
    for line in out.splitlines():
        line = line.strip()
        if "  @" in line and "  → " in line:
            path, rest = line.split("  @", 1)
            res[path] = rest.split("  → ", 1)[1]
    return res


# 内层测试里每次写入前后各停这么久（v0.45.507）：mtime 精度（Linux 上 `mtime_lag()` 为几到几十毫秒）内的相邻几段
# `locate` 会照实说「分不开」——而内层这几条测试整轮跑完都不到 8 ms。要断言「落在哪一段」，写入离段边界必须远于精度。
_SETTLE = 0.05


def _assert_settle_exceeds_mtime_lag():
    assert g.mtime_lag() < _SETTLE, (
        f"内层写入前后停 {_SETTLE}s 已不大于本机 mtime 精度 {g.mtime_lag()}s：「落在哪一段」的断言前提没了，调大 _SETTLE")


def test_repo_root_gate_places_each_write_on_the_timeline(tmp_path):
    """收集期写 / 第一条测试写 / 最后一条什么都不写 / session fixture 收尾时写 / 保留旧 mtime / 删除——各归其位，
    最后一条测试**不**被当成写入时刻所在（它就是 pytest 记这条 error 的那个名字）。"""
    _assert_settle_exceeds_mtime_lag()
    root = _make_fake_checkout(tmp_path / "checkout", f"""
        import os, pathlib, pytest, time
        ROOT = pathlib.Path(__file__).resolve().parents[1]
        (ROOT / "at_collection.json").write_text("x")
        time.sleep({_SETTLE})

        @pytest.fixture(scope="session")
        def writes_at_session_end():
            yield
            time.sleep({_SETTLE})
            (ROOT / "at_session_teardown.json").write_text("x")

        def test_a_writes(writes_at_session_end):
            time.sleep({_SETTLE})
            with open(ROOT / "report_snapshots/a.json", "ab") as f:
                f.write(b"x")
            (ROOT / "old_mtime.json").write_text("x")
            os.utime(ROOT / "old_mtime.json", (1_000_000_000, 1_000_000_000))   # copy2 / 解包那种
            (ROOT / "probability_scorecard_state/published.jsonl").unlink()
            time.sleep({_SETTLE})

        def test_z_last_does_nothing():
            pass
    """)
    r = _run_inner_pytest(root)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "2 passed, 1 error" in out, out
    assert "pytest 把它记在**收集序最后一条测试**名下" in out, out
    assert "只说明时间重合，不等于是它写的" in out, out
    assert "同一文件被写多次只看得到最后一次" in out, "mtime 是最后一次修改——不说清会让人以为没有更早的写入者"
    lab = _labels(out)
    assert "收集期" in lab["at_collection.json"], lab
    assert lab["report_snapshots/a.json"] == "tests/test_inner.py::test_a_writes 的 call 阶段", lab
    assert "早于本轮开始" in lab["old_mtime.json"], lab
    assert "定位不了" in lab["probability_scorecard_state/published.jsonl"], lab
    assert lab["at_session_teardown.json"].startswith("最后一段记录之后"), lab
    assert not any("test_z_last_does_nothing 的 call" in v for v in lab.values()), \
        f"最后一条什么都没写，却被标成写入时刻所在：\n{lab}"


def test_real_data_root_gate_names_the_test_running_when_a_concurrent_writer_wrote(tmp_path):
    """并发写入者（这里用线程模拟）在一条无关测试期间写：标出那条测试的阶段，措辞仍是「时间重合」。
    测试自己的写入归到它自己。"""
    _assert_settle_exceeds_mtime_lag()
    data_root = _make_data_root(tmp_path / "alpha-hive-data")
    root = _make_fake_checkout(tmp_path / "checkout", f"""
        import os, pathlib, threading, time
        DR = pathlib.Path(os.environ["INNER_DATA_ROOT"])
        GO = threading.Event()

        def _writer():
            GO.wait(20)
            time.sleep({_SETTLE})
            (DR / "report_snapshots/by_writer.json").write_text("{{}}")
            time.sleep({_SETTLE})

        T = threading.Thread(target=_writer, daemon=True)

        def test_a_escapes():
            T.start()
            time.sleep({_SETTLE})
            with open(DR / "pheromone.db", "ab") as f:
                f.write(b"x")
            time.sleep({_SETTLE})

        def test_b_unrelated():
            GO.set()
            T.join(20)
    """)
    r = _run_inner_pytest(root, {"ALPHA_HIVE_HOME": str(data_root), "INNER_DATA_ROOT": str(data_root)})
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "2 passed, 1 error" in out, out
    lab = _labels(out)
    assert lab["pheromone.db"] == "tests/test_inner.py::test_a_escapes 的 call 阶段", lab
    assert lab["report_snapshots/by_writer.json"] == "tests/test_inner.py::test_b_unrelated 的 call 阶段", lab
    assert "只说明时间重合，不等于是它写的" in out and "并发写入者" in out, out
