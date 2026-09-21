"""`_isolate_paper_portfolio_state` 对「生产真身」的判定不依赖导入顺序（v0.45.303）。

**缺陷形状**（配对对照实测，不是推理）：夹具原先用 `paper_portfolio.STATE_DIR` 当「生产真身」，
可它是 import 期常量、`_base_dir()` 读 `ALPHA_HIVE_HOME`。若本进程里 `paper_portfolio` 的**首次导入**
恰好发生在夹具内——此刻 `_isolate_env` 已把 HOME 指向 tmp——`STATE_DIR` 就被绑成沙箱，
「真身」与随后新建的沙箱成了**同一个目录**：

  * 单条测试往沙箱写状态文件 ⇒ teardown 误报「写到了**生产** paper_portfolio_state/」；
  * 更隐蔽：守卫**名义上在、实际盯的是沙箱自己**，测试真写穿生产它也看不见。

整套/整文件跑不触发（约 10 个模块顶层就导入 `paper_portfolio`，收集期环境还是真的），
只在「单条/子集 + 夹具内首次导入」时露出。

**为什么必须子进程 + 原样拷贝的 conftest**（沿用 `test_root_data_guard.py` 的接线测试）：

  * 「首次导入发生在夹具内」是**进程级**状态：本进程收集期早已导入过 `paper_portfolio`，
    进程内怎么造都造不出这个前提；
  * 只测某个 helper 证明不了 conftest 真的用了它（MEMORY `alpha-hive-test-writes-production`：
    测 helper ≠ 测接线）。

**2×2 矩阵**——红组必须红、对照组必须绿，缺一格都证明不了守卫有牙：

  ┌────────────────────┬────────────────────────┬────────────────────────────┐
  │ 首次导入发生在      │ 测试写沙箱（合法）      │ 测试写真身（违法）          │
  ├────────────────────┼────────────────────────┼────────────────────────────┤
  │ 夹具内（缺陷触发）  │ 必须绿（旧：1 error）   │ 必须红（旧：守卫失明、照绿） │
  │ 收集期（常态）      │ 必须绿                  │ 必须红                      │
  └────────────────────┴────────────────────────┴────────────────────────────┘

**前提自证**：「夹具内首次导入」这一格若哪天不再成立（比如有人让 conftest 顶层提前 import 了
`paper_portfolio`），整列测试会**悄悄变成恒绿**而没人知道。所以子进程里挂一个探针，
在夹具收尾之后读 `paper_portfolio` 的 import 期 `STATE_DIR`：夹具内首次导入 ⇒ 它落在沙箱里；
收集期导入 ⇒ 它就是真身。前提不成立就在这里红，而不是让测试失去意义。
收集期那一格里同时核对「夹具对真身的推导 == `paper_portfolio` 自己的推导」，防两边漂移。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent

# 假 checkout 根上的探针 conftest（先于 tests/conftest.py 加载）。
#   · `pytest_collection_finish`：收集结束时 paper_portfolio 是否已被导入（＝「收集期导入」这一格的判据）
#   · `pytest_runtest_logfinish`：在该条测试**所有夹具都收尾之后**读 `STATE_DIR`。此时夹具的
#     monkeypatch 已撤销，读到的就是 **import 期绑定值**——量错时机（夹具还活着时读）会得到沙箱路径，
#     误以为「真身没被冻」（v0.45.300 排查时踩过同一个坑）。
_PROBE_CONFTEST = '''
import json, os, sys

_S = {"imported_at_collection": None, "import_time_state_dir_after_teardown": []}


def pytest_collection_finish(session):
    _S["imported_at_collection"] = "paper_portfolio" in sys.modules


def pytest_runtest_logfinish(nodeid, location):
    pp = sys.modules.get("paper_portfolio")
    _S["import_time_state_dir_after_teardown"].append(str(pp.STATE_DIR) if pp is not None else None)


def pytest_sessionfinish(session):
    with open(os.environ["PROBE_OUT"], "w", encoding="utf-8") as f:
        json.dump(_S, f)
'''

_IN_FIXTURE_HEADER = '''
import os
from pathlib import Path
# 故意不在模块顶层导入 paper_portfolio：它的首次导入只能发生在 conftest 的夹具里。
'''

_AT_COLLECTION_HEADER = '''
import os
from pathlib import Path
import paper_portfolio  # noqa: F401 — 收集期导入（此刻环境还是真的）
'''

# 合法：写夹具重绑后的路径。顺带断言重绑本身生效（防线①）。
_WRITE_SANDBOX = '''
def test_inner():
    import paper_portfolio as pp
    sandbox_home = Path(os.environ["ALPHA_HIVE_HOME"])
    assert sandbox_home in pp.CLOSED_FILE.parents, f"夹具没把 CLOSED_FILE 重绑进沙箱：{pp.CLOSED_FILE}"
    pp.CLOSED_FILE.write_text("{}\\n", encoding="utf-8")
'''

# 违法：绕开模块常量，直接写「生产」状态目录（子进程里那是 INNER_REAL_HOME 下的一个哨兵目录，
# 永远不是真生产——这是本测试能安全「造一个故意写真身的测试」的前提）。
_WRITE_REAL = '''
def test_inner():
    real = Path(os.environ["INNER_REAL_HOME"]) / "paper_portfolio_state"
    real.mkdir(exist_ok=True)
    (real / "closed_trades.jsonl").write_text("written past the sandbox\\n", encoding="utf-8")
'''


def _run_inner(tmp_path: Path, *, first_import: str, writes: str):
    """在假 checkout 里子进程真跑一轮 pytest，返回 (CompletedProcess, 探针 dict, 输出文本, inner_basetemp, real_home)。"""
    assert first_import in ("in_fixture", "at_collection") and writes in ("sandbox", "real")
    root = tmp_path / "checkout"
    (root / "tests").mkdir(parents=True)
    shutil.copy2(_TESTS / "conftest.py", root / "tests" / "conftest.py")          # 原样拷贝
    shutil.copy2(_TESTS / "_root_data_guard.py", root / "tests" / "_root_data_guard.py")
    (root / "conftest.py").write_text(_PROBE_CONFTEST, encoding="utf-8")
    header = _IN_FIXTURE_HEADER if first_import == "in_fixture" else _AT_COLLECTION_HEADER
    body = _WRITE_SANDBOX if writes == "sandbox" else _WRITE_REAL
    (root / "tests" / "test_inner.py").write_text(
        textwrap.dedent(header) + textwrap.dedent(body), encoding="utf-8")

    real_home = tmp_path / "real_home"            # 子进程眼里的「生产数据根」（哨兵）
    real_home.mkdir()
    probe_out = tmp_path / "probe.json"
    inner_basetemp = tmp_path / "inner_basetemp"

    # 显式构造 env，不继承外层 `_isolate_env` 灌进来的沙箱值：子进程的「生产 HOME」必须是我们指定的哨兵。
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ALPHA_HIVE_") and not k.startswith("PYTEST_")
           and k not in ("ANTHROPIC_API_KEY", "PYTHONPATH")}
    env.update(
        # 生产模块从真仓库 import；被守的根目录是假 checkout（conftest 按自己的 __file__ 定根）。
        PYTHONPATH=str(_ROOT),
        ALPHA_HIVE_HOME=str(real_home),
        INNER_REAL_HOME=str(real_home),
        PROBE_OUT=str(probe_out),
        PYTHONDONTWRITEBYTECODE="1",
    )
    # --tb=native：默认 long/short traceback 会把 conftest 断言的**源码**整段打出来，源码里就有报错文案，
    # 子串断言会被源码满足、证明不了运行时真报了什么（`test_root_data_guard.py` 记过）。
    # --rootdir/--confcutdir：无 ini 文件时 rootdir 会退到参数的公共祖先 `tests/`，根上的探针 conftest 就不加载了。
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header", "--tb=native",
         "-o", "addopts=", f"--rootdir={root}", f"--confcutdir={root}", f"--basetemp={inner_basetemp}",
         "tests/test_inner.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert probe_out.exists(), f"探针没落盘——根上的探针 conftest 没加载，或子进程在收集前就崩了：\n{out}"
    probe = json.loads(probe_out.read_text(encoding="utf-8"))
    return r, probe, out, inner_basetemp, real_home


def _assert_premise(first_import, probe, inner_basetemp, real_home, out):
    """前提自证：这一格真的是它声称的那一格。"""
    assert len(probe["import_time_state_dir_after_teardown"]) == 1, probe
    frozen = probe["import_time_state_dir_after_teardown"][0]
    assert frozen is not None, f"子进程里 paper_portfolio 根本没被导入，本格什么也没测：\n{out}"
    if first_import == "in_fixture":
        assert probe["imported_at_collection"] is False, (
            "回归场景不再成立：收集结束时 paper_portfolio 已被导入（conftest 顶层或别的 import 提前导入了它），"
            "「首次导入发生在夹具内」造不出来，本列测试会悄悄变成恒绿。\n"
            "若这是有意为之的结构性修法（例如让 conftest 提前导入它），请同步改写本测试，而不是删掉它。")
        # import 期绑定值落在沙箱里，且不是真身 ⇒ 这正是缺陷的触发条件
        assert Path(frozen).resolve().is_relative_to(inner_basetemp.resolve()), (
            f"夹具内首次导入应把 STATE_DIR 冻成沙箱路径，实际是 {frozen}——触发条件不成立")
        assert not Path(frozen).resolve().is_relative_to(real_home.resolve()), frozen
    else:
        assert probe["imported_at_collection"] is True, "收集期导入这一格没造出来"
        # 收集期（真环境）导入的 STATE_DIR 就是真身；夹具对真身的推导必须与它一致（防漂移）
        assert frozen == str(real_home / "paper_portfolio_state"), (
            f"收集期导入的 paper_portfolio.STATE_DIR = {frozen}，与哨兵真身 {real_home / 'paper_portfolio_state'} 不符")


@pytest.mark.parametrize("first_import", ["in_fixture", "at_collection"])
def test_legit_sandbox_write_is_green(tmp_path, first_import):
    """对照组：测试只写夹具重绑后的沙箱路径——两种导入时机下都必须干净通过。

    「夹具内首次导入」这一格是缺陷的原始形状：旧夹具在这里 `1 passed, 1 error`（把沙箱当生产）。
    """
    r, probe, out, basetemp, real_home = _run_inner(tmp_path, first_import=first_import, writes="sandbox")
    _assert_premise(first_import, probe, basetemp, real_home, out)
    assert r.returncode == 0, f"合法的沙箱写入被守卫误报：\n{out}"
    assert "1 passed" in out and "error" not in out.lower().replace("0 error", ""), out
    assert "测试写到了**生产**" not in out, out


@pytest.mark.parametrize("first_import", ["in_fixture", "at_collection"])
def test_real_write_is_caught_in_both_import_orders(tmp_path, first_import):
    """红组：测试绕开模块常量、直接写「生产」状态目录——两种导入时机下守卫都必须在 teardown 报错。

    「夹具内首次导入」这一格是缺陷的**隐蔽**形状：旧夹具的「真身」＝沙箱，守卫盯着沙箱自己，
    这里的写入它**看不见**、整轮照绿。这一格若不红，「不再误报」只是把守卫削成了恒绿。
    """
    r, probe, out, basetemp, real_home = _run_inner(tmp_path, first_import=first_import, writes="real")
    _assert_premise(first_import, probe, basetemp, real_home, out)
    assert r.returncode != 0, f"守卫没抓住往真身目录的写入（失明）：\n{out}"
    assert "1 passed, 1 error" in out, f"应是测试本身通过、守卫在 teardown 报错：\n{out}"
    # 运行时消息（native traceback 不回显断言源码）：点名了被改的那个文件
    assert "测试写到了**生产** paper_portfolio_state/：['closed_trades.jsonl']" in out, out
    # 写入确实落在了哨兵真身目录，而不是别处——否则红只是别的原因造成的
    assert (real_home / "paper_portfolio_state" / "closed_trades.jsonl").read_text(encoding="utf-8").startswith("written past")


def test_real_state_dir_is_never_the_sandbox(_real_paper_portfolio_state_dir, tmp_path):
    """进程内直接钉缺陷的**签名**：夹具认定的「真身」不许落在本条测试的沙箱里，也不许等于重绑后的 STATE_DIR。

    旧夹具在「夹具内首次导入」的进程里恰恰是这两条同时成立（真身＝沙箱）。
    本条在整套里恒绿（收集期导入，真身没被冻），单条跑时是它的直接观测；
    子进程那组才是端到端的证据，这里只是最便宜的一道。
    """
    import paper_portfolio as pp
    real = _real_paper_portfolio_state_dir
    assert tmp_path.resolve() not in real.resolve().parents, f"真身 {real} 落进了本条测试的沙箱 {tmp_path}"
    assert real != pp.STATE_DIR, f"真身 == 重绑后的 STATE_DIR（{pp.STATE_DIR}）：守卫盯着沙箱自己"
    assert real.name == "paper_portfolio_state"
