"""生产代码自己不许挪进程的 cwd / sys.path，也不许往 cwd 里建产物（v0.45.230）。

v0.45.224 在**测试侧**把三处损害关住了：conftest `_isolate_cwd_and_sys_path`（每条测试
空目录起跑、结束还原 sys.path）、`test_deep_analysis_prefetch_injection.py` 的 import
隔离块、`test_reads_own_checkout.py` 的 `TestProcessStateStaysPut` / `TestRuntimeLeaksAreUndone`。
那些守的是「泄漏不跨测试」。本文件守**根因**：三处生产代码本身不再泄漏。
两层缺一不可 —— 隔离一在，根因复发就再也没有「跨测试」的症状可红。

⚠️ 每条都要在**任何机器、任何 checkout**上有区分力（「守卫在哪台机器上红？」）：
- `weekly_optimizer`：在主 checkout 里 `ALPHAHIVE_DIR` 恰好等于仓库根，把 insert 改回
  `ALPHAHIVE_DIR` 在那里无害 ⇒ 把它指向仓库外的哨兵目录再测。
- `deep_analysis`：chdir 到仓库根，从仓库根起跑时等于没挪；且它多半已在收集期被
  import 过、再 import 是空操作 ⇒ 在子进程里、从空目录 import。
- `cboe_fetcher`：本条自己 chdir 进空目录再构造，并把 `ALPHA_HIVE_CACHE_DIR` 换成
  本条独有的值（冻在 import 期的默认值跟不上它）。

⚠️ `cboe_fetcher` 的 import 必须留在**模块级**（同 `test_paths_not_frozen_at_import.py`）：
挪进函数体就在 `_isolate_env` 之后才 import，「默认值冻在 import 期」这种复发会照样全绿。
"""

import ast
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import cboe_fetcher  # 模块级：复现「收集期就被 import」（见模块 docstring 末段）

REPO_ROOT = Path(__file__).resolve().parent.parent
_SENTINEL_MAIN_CHECKOUT = Path("/nonexistent/v0.45.230-main-checkout-sentinel")


class TestWeeklyOptimizerImportRoot:
    """`weekly_optimizer` 往 sys.path 插的必须是**本 checkout**，且反复调用不增长。

    v0.45.224 实测：6 处函数体内 `sys.path.insert(0, str(ALPHAHIVE_DIR))`，全套插 40 次、
    从不拿掉；ALPHAHIVE_DIR 写死 `~/Desktop/Alpha Hive` ⇒ worktree 里插的是主 checkout，
    此后函数体内 import 的模块从主 checkout 加载（改坏 worktree 的模块，测试照样绿）。
    """

    # 6 处调用点。空输入下全部早返回：不出网、不写仓库（v0.45.230 逐个实测）。
    CALLS = {
        "_load_close_t7_map": lambda wo, d: wo._load_close_t7_map(),
        "_apply_clean_t7_prices":
            lambda wo, d: wo._apply_clean_t7_prices(types.SimpleNamespace(snapshots=[])),
        "compute_new_weights": lambda wo, d: wo.compute_new_weights(d),
        "compute_new_weights_wls": lambda wo, d: wo.compute_new_weights_wls(d),
        "bootstrap_validate": lambda wo, d: wo.bootstrap_validate(d, dict(wo.DEFAULT_WEIGHTS)),
        "check_ticker_pool_consistency": lambda wo, d: wo.check_ticker_pool_consistency(d),
    }

    @pytest.mark.parametrize("name", sorted(CALLS))
    def test_call_site_puts_only_own_checkout_on_sys_path(self, name, tmp_path, monkeypatch):
        import weekly_optimizer as wo
        monkeypatch.setattr(wo, "ALPHAHIVE_DIR", _SENTINEL_MAIN_CHECKOUT)
        # 仓库根若恰在 sys.path[0]，「已排在最前就不插」会让本条空转 ⇒ 先垫一个别的
        monkeypatch.setattr(sys, "path", ["/nonexistent/v0.45.230-other-first", *sys.path])
        n_before = len(sys.path)

        for _ in range(3):
            self.CALLS[name](wo, tmp_path)

        assert str(_SENTINEL_MAIN_CHECKOUT) not in sys.path, (
            f"`weekly_optimizer.{name}` 把 ALPHAHIVE_DIR（数据根，worktree 里就是主 checkout）"
            "插进了 sys.path。import 根要锚 `__file__`（CLAUDE.md「指向代码还是数据」表）。")
        assert Path(sys.path[0]).resolve() == REPO_ROOT, (
            f"`weekly_optimizer.{name}` 之后 sys.path[0] = {sys.path[0]!r}，不是本 checkout {REPO_ROOT}"
            "（本条先垫了一个假的首项，没插就会停在它身上）")
        assert len(sys.path) - n_before == 1, (
            f"调 3 次 `weekly_optimizer.{name}`，sys.path 增长了 {len(sys.path) - n_before} 项"
            "（应为 1）：v0.45.224 实测全套插了 40 次。")

    def test_sys_path_is_only_mutated_inside_the_helper(self):
        """行为测试只覆盖上面 6 个调用点；**新增**的直接 `sys.path.insert` 靠这条挡。"""
        src = (REPO_ROOT / "weekly_optimizer.py").read_text(encoding="utf-8")
        offenders = []

        def visit(node, func):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = node.name
            touches = (isinstance(node, ast.Attribute) and node.attr in
                       {"insert", "append", "extend", "remove", "pop", "clear"}
                       and ast.unparse(node.value) == "sys.path")
            if touches and func != "_ensure_code_dir_importable":
                offenders.append(f"weekly_optimizer.py:{node.lineno} in {func or '<module>'}")
            for child in ast.iter_child_nodes(node):
                visit(child, func)

        visit(ast.parse(src), None)
        assert not offenders, (
            "sys.path 只许在 `_ensure_code_dir_importable` 里改（它锚 `__file__` 且去重）：\n"
            + "\n".join(offenders))


class TestDeepAnalysisImportHasNoSideEffects:
    """import `deep_analysis` 不许挪 cwd、不许加 sys.path；CLI（`main()`）仍切到脚本目录。

    v0.45.224 实测：原先 import 期 `os.chdir(脚本目录)` + `sys.path.insert(0, ".")`，
    收集期被测试模块 import ⇒ 整个 pytest 进程在仓库根跑，从空目录跑全套与从仓库根
    跑一模一样，9 条依赖 cwd 的测试被掩盖。
    """

    @staticmethod
    def _run(code, cwd):
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"子进程失败（rc={r.returncode}）：\n{r.stderr[-2000:]}"
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        assert lines, f"子进程没吐出结果行：\nstdout={r.stdout[-1000:]}\nstderr={r.stderr[-1000:]}"
        return json.loads(lines[-1])

    def test_import_does_not_move_cwd_or_sys_path(self, tmp_path):
        out = self._run(
            "import json, os, sys\n"
            "cwd, path = os.getcwd(), list(sys.path)\n"
            "import deep_analysis\n"
            "print(json.dumps({'cwd': cwd, 'cwd_after': os.getcwd(),\n"
            "                  'added': [p for p in sys.path if p not in path]}))\n",
            cwd=tmp_path)
        assert out["cwd_after"] == out["cwd"], (
            f"import deep_analysis 把 cwd 从 {out['cwd']} 挪到了 {out['cwd_after']}。"
            "CLI 需要的 chdir 放 `main()` 里，别放 import 期。")
        assert out["added"] == [], f"import deep_analysis 往 sys.path 加了 {out['added']}"

    def test_cli_main_still_switches_to_script_dir(self, tmp_path):
        """CLI 语义不变：`--json` 相对路径、报告落盘都按脚本目录。只截获 chdir，不真跑。"""
        out = self._run(
            "import json, os, sys\n"
            "import deep_analysis\n"
            "class _Stop(Exception): pass\n"
            "seen = []\n"
            "def _rec(p):\n"
            "    seen.append(os.path.abspath(p)); raise _Stop\n"
            "os.chdir = _rec\n"
            "sys.argv = ['deep_analysis.py', '--json', 'data.json']\n"
            "err = None\n"
            "try:\n"
            "    deep_analysis.main()\n"
            "except _Stop:\n"
            "    pass\n"
            "except BaseException as e:  # 没 chdir 就会去 cwd 里开 data.json —— 记下来，别让子进程崩成一句 rc=1\n"
            "    err = repr(e)\n"
            "print(json.dumps({'chdir': seen, 'error': err}))\n",
            cwd=tmp_path)
        assert out["chdir"] and Path(out["chdir"][0]).resolve() == REPO_ROOT, (
            f"`deep_analysis.main()` 没先切到脚本目录（截获到 {out['chdir']}，main 抛出 {out['error']}）："
            "`--json data.json` 会改按调用者 cwd 解析、报告写进调用者 cwd —— CLI 行为变了。")


class TestCboeFetcherDefaultCacheDir:
    """`CBOEDailyFetcher()` 无参构造必须落进 `ALPHA_HIVE_CACHE_DIR`，不许在 cwd 建目录。

    v0.45.224 空目录普查实测：原默认值 `"cache/cboe_daily"` 在 pytest 的 cwd 里建目录，
    v0.45.224 之前即建进起 pytest 的那个 checkout（本机 9 个 worktree 各留了一个）。
    """

    def test_no_arg_construction_follows_env_not_cwd(self, tmp_path, monkeypatch):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        monkeypatch.chdir(run_dir)
        cache_root = tmp_path / "only_this_test_cache"
        monkeypatch.setenv("ALPHA_HIVE_CACHE_DIR", str(cache_root))

        fetcher = cboe_fetcher.CBOEDailyFetcher()

        assert sorted(p.name for p in run_dir.iterdir()) == [], (
            "无参构造往 cwd 里建了东西 —— 默认缓存目录又成了 cwd 相对路径")
        assert Path(fetcher.cache_dir) == cache_root / "cboe_daily", (
            f"默认缓存目录 {fetcher.cache_dir} 没跟 ALPHA_HIVE_CACHE_DIR={cache_root} 走："
            "要么是相对路径，要么冻在了 import 期（默认参数 / 模块常量）。")
        assert Path(fetcher.cache_dir).is_dir()

    def test_explicit_cache_dir_still_wins(self, tmp_path):
        target = tmp_path / "explicit"
        fetcher = cboe_fetcher.CBOEDailyFetcher(cache_dir=str(target))
        assert fetcher.cache_dir == str(target) and target.is_dir()
