"""`paper_portfolio` 不许在 import 期建目录（v0.45.394，数据根迁移阶段 6）。

阶段 6 之前 `paper_portfolio_state/` 被 git 跟踪，仓库根里本来就有，模块级
`STATE_DIR.mkdir(exist_ok=True)` 从未做过任何事。解除跟踪后，干净检出里它在 pytest 收集期
（早于任何 env 隔离）把空目录建进仓库根，根总闸 teardown 才第一次报 added。
"""
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _run(code, home, cwd):
    env = {k: v for k, v in os.environ.items() if k != "ALPHA_HIVE_HOME"}
    env["ALPHA_HIVE_HOME"] = str(home)
    env["PYTHONPATH"] = str(_ROOT)
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


def test_import_creates_no_state_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    r = _run("import paper_portfolio", home, cwd)
    assert r.returncode == 0, r.stderr[-800:]
    assert not (home / "paper_portfolio_state").exists(), "import 期建了数据根下的状态目录"
    assert not (cwd / "paper_portfolio_state").exists(), "import 期建了 cwd 下的状态目录"


def test_writers_create_missing_state_dir_themselves(tmp_path):
    """不在 import 期建目录的代价由写入方承担：全新数据根里第一次写也得成功。"""
    home = tmp_path / "home"
    home.mkdir()
    code = (
        "import paper_portfolio as p, pathlib\n"
        "assert not p.STATE_DIR.exists(), p.STATE_DIR\n"
        "p._append_jsonl(p.EQUITY_FILE, {'a': 1})\n"
        "assert p.EQUITY_FILE.is_file()\n"
        "import shutil; shutil.rmtree(p.STATE_DIR)\n"
        "p._save_meta({})\n"
        "assert p.META_FILE.is_file()\n"
        "print(p.STATE_DIR)\n"
    )
    r = _run(code, home, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr[-800:]
    assert Path(r.stdout.strip()).parent == home
