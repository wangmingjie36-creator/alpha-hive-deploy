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
]
DATA_ROOT_EXCLUDED = [
    "logs/scan_timing.json", "logs/alpha_hive.log", "db_backups/pheromone_2026-09-28.db",   # 常驻写入方
    "_git_backup/pheromone/manifest.json", "_archive/pheromone_bak/a.db", "_migration/r.json",
    "_manual_backups/x", ".DS_Store", "__pycache__/x.pyc", "sub/.DS_Store",
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


# ───────────────── 2b. 真实数据根：指纹比对（合成数据根） ─────────────────

@pytest.fixture
def data_root(tmp_path):
    r = tmp_path / "alpha-hive-data"
    for rel, data in {
        "pheromone.db": b"db", "pheromone.db-wal": b"wal", "pheromone.db-shm": b"shm",
        "report_snapshots/a.json": b"{}", "paper_portfolio_state/meta.json": b"{}",
        "logs/alpha_hive.log": b"line\n", "db_backups/pheromone_2026-09-27.db": b"old",
        "_git_backup/x": b"1", "_archive/pheromone_bak/a.db": b"a",
    }.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_bytes(data)
    return r


def _dfp(root):
    return g.fingerprint(str(root), g.data_root_excluded_reason)


@pytest.mark.parametrize("mutate,kind,path", [
    (lambda r: (r / "pheromone.db").write_bytes(b"dbX"), "changed", "pheromone.db"),                # 账本多一字节
    (lambda r: (r / "paper_portfolio_state/meta.json").write_bytes(b"{1}"), "changed", "paper_portfolio_state/meta.json"),
    (lambda r: (r / "report_snapshots/b.json").write_bytes(b"{}"), "added", "report_snapshots/b.json"),
    (lambda r: (r / "brand_new.json").write_bytes(b"x"), "added", "brand_new.json"),                 # 默认拒绝
    (lambda r: (r / "new_dir").mkdir(), "added", "new_dir"),                                         # 只建空目录也算写
    (lambda r: (r / "report_snapshots/a.json").unlink(), "removed", "report_snapshots/a.json"),
    (lambda r: (r / "pheromone.db-wal").unlink(), "removed", "pheromone.db-wal"),
], ids=["append-ledger", "rewrite-state", "new-snapshot", "new-top-level", "empty-dir", "delete", "wal-vanishes"])
def test_data_root_write_shapes_are_caught(data_root, mutate, kind, path):
    before = _dfp(data_root)
    time.sleep(0.01)
    mutate(data_root)
    assert path in g.diff(before, _dfp(data_root)).get(kind, []), g.diff(before, _dfp(data_root))


def test_data_root_benign_activity_is_not_a_write(data_root):
    """正对照：日志轮转、备份轮转、每日备份、迁移留档、读者往 -shm 写标记——都不是测试写穿。"""
    before = _dfp(data_root)
    time.sleep(0.01)
    (data_root / "logs/alpha_hive.log").write_bytes(b"line\nmore\n")
    (data_root / "logs/new.log").write_bytes(b"x")
    (data_root / "db_backups/pheromone_2026-09-28.db").write_bytes(b"new")
    (data_root / "_git_backup/x").write_bytes(b"2")
    (data_root / "_archive/pheromone_bak/b.db").write_bytes(b"b")
    (data_root / "pheromone.db-shm").write_bytes(b"SHM")          # 同大小：读者标记
    (data_root / ".DS_Store").write_bytes(b"finder")
    assert g.diff(before, _dfp(data_root)) == {}


# ─────────────────────────────── 3. 接线（真跑 pytest） ───────────────────────────────

def _make_fake_checkout(root: Path, test_body: str) -> Path:
    (root / "tests").mkdir(parents=True)
    shutil.copy2(_TESTS / "conftest.py", root / "tests" / "conftest.py")
    shutil.copy2(_TESTS / "_root_data_guard.py", root / "tests" / "_root_data_guard.py")
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
    assert "测试往**仓库根**写了非代码内容" in out, out
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
    assert "测试写到了**真实数据根**" in out, out
    expected = "\n".join(["added（3）：", "new_dir", "report_snapshots/b.json", "written_at_collection.json",
                          "changed（1）：", "pheromone.db"])
    normalized = "\n".join(line.strip() for line in out.splitlines())
    assert expected in normalized, f"总闸报出的 diff 与预期不符：\n{out}"


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
