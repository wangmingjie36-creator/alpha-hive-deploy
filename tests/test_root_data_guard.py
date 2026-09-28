"""仓库根「默认拒绝」总闸自己的测试（v0.45.233，数据根迁移阶段 0.3）。

三层，缺一层就证明不了闸有牙：

1. **判据**：哪些算代码、哪些算数据——用真实仓库里出现过的路径形状逐条钉死，
   含旧清单 `_GUARDED_PRODUCTION_ARTIFACTS` 必须是覆盖面子集（谁把其中一项划进「代码」谁红）。
2. **指纹比对**：合成目录树上逐种写入形状（写一字节 / 新建空目录 / -shm 读标记 / 改代码）。
3. **接线**：子进程里真跑一轮 pytest，用的是**原样拷贝的 conftest**——
   只测 helper 证明不了 conftest 真的调用了它、也证明不了「之前」取在收集期之前
   （MEMORY `alpha-hive-test-writes-production`：测 helper ≠ 测接线）。
   红组必须红、对照组必须绿；对照组不绿，红组的红就说明不了任何事。
"""
from __future__ import annotations

import ast
import os
import shutil
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


def _legacy_guarded_names() -> tuple[str, ...]:
    """从 conftest 源码里读出旧清单（conftest 不可 import）。改成非字面量会在这里红——那也该红。"""
    tree = ast.parse((_TESTS / "conftest.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_GUARDED_PRODUCTION_ARTIFACTS" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("conftest 里找不到 _GUARDED_PRODUCTION_ARTIFACTS")


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


@pytest.mark.parametrize("name", _legacy_guarded_names())
def test_legacy_artifact_list_is_subset_of_coverage(name, tmp_path):
    """旧 6 项必须既过判据、又真出现在指纹里（目录形态的给它一个子文件）。"""
    assert g.excluded_reason(name) is None
    p = tmp_path / name
    if "." in name:            # pheromone.db / metrics.db
        p.write_bytes(b"x")
    else:                      # chroma_db / *_state
        p.mkdir()
        (p / "f.jsonl").write_bytes(b"x")
    fp = g.fingerprint(str(tmp_path))
    assert name in fp


def test_legacy_list_is_pinned():
    """纵深防御的旧闸别被顺手删掉（任务要求保留）。原 6 项 + v0.45.333 追加的 `sell_strike_state`。
    写成精确相等而不是子集：加一项也得来这里改一行——增删都是有意的动作，不是顺手。"""
    assert _legacy_guarded_names() == (
        "pheromone.db", "metrics.db", "chroma_db", "vrp_state", "options_paper_state", "hedge_state",
        "sell_strike_state")


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


def _run_inner_pytest(root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 生产模块从真仓库 import（conftest 的 autouse fixture 要 import llm_service 等）；
    # 被守的根目录却是这个假 checkout——conftest 按自己的 __file__ 定根。
    env["PYTHONPATH"] = str(_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # --tb=native：默认的 long traceback 会把 conftest 断言的**源码**整段打出来，
    # 源码里就有报错文案与路径字样 ⇒ 下面的子串断言会被源码满足、证明不了运行时真报了什么。
    # native 只打语句首行，剩下的文字只可能来自运行时消息。
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header", "--tb=native",
         "tests/test_inner.py"],
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
