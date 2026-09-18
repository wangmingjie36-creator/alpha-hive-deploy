"""experiments 里两个「跨世代混算」脚本的默认拒绝护栏（v0.45.290）

背景
----
`signal_archive.analyze()` 自 v0.45.265 起每个信号只用**当前世代**的样本，
`ic_rerun_readiness._COHORT_HISTORY` 是边界表。但 `experiments/signal_ic_sweep.py`
（全信号 IC 普查）与 `experiments/final_score_dilution.py`（final_score 稀释分解）
读的仍是整张 `signal_archive` / `predictions`，**完全不知道世代**：

  · 它们的输出（`signal_ic_sweep_report.md` / `final_score_dilution_report.md`）
    是 v0.45.172 权重决策的证据来源，重跑并引用混算数字不会让任何东西变红；
  · 就绪度闸放行后指向的是 `signal_archive.py --analyze`，不是这两个脚本。

⚠️ 为什么只加护栏、不「接边界」（详见 CHANGELOG v0.45.290）
  · 接上后可检验信号 69→27，6 个决策相关信号全部「不可检验(<8 周)」，要到 10 月底~11 月中
    才攒够；
  · 两个脚本的 p 值用正态近似 `erfc(|t|/√2)`，周度 IC 均值该用 t(n−1)：n=26、t≈3.5 时低估
    约 4 倍，切到 8~16 周的小样本区间会低估 3~80 倍。在这种口径上加「分母固定」的 Bonferroni
    是调错旋钮（分母只影响 2.6 倍）——统计有效性要在 `ic_diagnostics` 一处修，不是在这里；
  · sweep 只留下过一次运行产物（08-25 的报告），且已有更全的后继（`analyze()` 的 `diagnose()`
    已含同类方法，并已按世代切）。

发现的顺带问题：`signal_ic_sweep.py` 自 09-15 的路径收口（v0.45.260）起按文档方式
（`python3 experiments/signal_ic_sweep.py`）根本跑不起来——它新加的
`from hive_logger import PATHS` 没有配套的 `sys.path` 注入。零测试、零调用方，所以三天没人知道。
本文件用**真子进程**跑（cwd 与仓库根无关、清掉 PYTHONPATH），正好把它钉住。
"""

import os
import random
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["signal_ic_sweep.py", "final_score_dilution.py"]
# 结果确实被算出来的标志（各脚本自己的标题行）
RESULT_MARK = {"signal_ic_sweep.py": "全信号 IC 普查",
               "final_score_dilution.py": "final_score 稀释分解"}


def _run(script, args, home, cwd):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)          # 不能让环境替脚本补上它自己该注入的 sys.path
    # `PATHS.db` 先读 ALPHA_HIVE_DB_PATH，而 conftest 的 `_isolate_env` 会把它设成别处，
    # 子进程照单全收——只设 ALPHA_HIVE_HOME 会让脚本去开一个不存在的库（第一版栽在这）。
    # 同时把日志/缓存/chroma 钉进临时目录，保证不写到真实数据根或仓库里。
    env["ALPHA_HIVE_HOME"] = str(home)
    env["ALPHA_HIVE_DB_PATH"] = str(home / "pheromone.db")
    env["ALPHA_HIVE_LOGS_DIR"] = str(home / "logs")
    env["ALPHA_HIVE_CACHE_DIR"] = str(home / "cache")
    env["ALPHA_HIVE_CHROMA_PATH"] = str(home / "chroma_db")
    return subprocess.run(
        [sys.executable, str(ROOT / "experiments" / script), *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=180)


def _make_db(path: Path, weeks: int = 12, tickers: int = 8) -> None:
    """合成库：predictions（含 5 维分与已成熟的 close_t7）+ signal_archive。
    固定种子；每周一个交易日 × 8 只标的，足以过两个脚本的 MIN_WEEKS / MIN_WIDTH。"""
    rng = random.Random(20260918)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE predictions (date TEXT, ticker TEXT, price_at_predict REAL,
                   close_t7 REAL, final_score REAL, dimension_scores TEXT, checked_t7 INTEGER)""")
    con.execute("CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL)")
    d0 = date(2026, 3, 2)                                     # 周一
    dims = ["signal", "catalyst", "sentiment", "odds", "risk_adj"]
    import json
    for w in range(weeks):
        day = (d0 + timedelta(weeks=w)).isoformat()
        for k in range(tickers):
            tk = f"T{k}"
            ds = {d: round(rng.uniform(2, 9), 3) for d in dims}
            ret = 0.01 * (ds["sentiment"] - 5.5) + rng.gauss(0, 0.03)
            con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,1)",
                        (day, tk, 100.0, 100.0 * (1 + ret), round(rng.uniform(3, 8), 3), json.dumps(ds)))
            con.execute("INSERT INTO signal_archive VALUES (?,?,?,?)",
                        (day, tk, "sentiment.pct", round(rng.uniform(20, 80), 2)))
    con.commit()
    con.close()


# ══════════════════════════════════════════════════════════════════════════
# 默认：拒绝，且在开库之前
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("script", SCRIPTS)
def test_default_run_refuses_before_touching_any_data(script, tmp_path):
    home = tmp_path / "empty_home"                    # 故意没有 pheromone.db
    home.mkdir()

    r = _run(script, [], home, tmp_path)

    assert r.returncode == 2, (
        f"{script} 默认应拒绝（退出码 2），实得 {r.returncode}。stderr 尾部：\n{r.stderr[-600:]}")
    assert "--pool-generations" in r.stderr, "拒绝信息要告诉人怎么显式放行"
    assert "signal_archive.py --analyze" in r.stderr, "拒绝信息要指向按世代切的正路"
    assert r.stdout == "", "拒绝时不该有任何结果输出到 stdout（否则会被当成结果贴走）"
    assert "Traceback" not in r.stderr, "拒绝不是崩溃"
    assert not (home / "pheromone.db").exists(), "护栏必须先于开库"


@pytest.mark.parametrize("script", SCRIPTS)
def test_default_run_refuses_even_when_data_is_present(script, tmp_path):
    """反向自证：上一条「没库所以拒绝」不算数——有数据、能算出结果时，默认也必须拒绝，
    否则那条测试里的红/绿可能只是「库不存在」造成的。"""
    home = tmp_path / "home"
    _make_db(home / "pheromone.db")

    r = _run(script, [], home, tmp_path)

    assert r.returncode == 2
    assert RESULT_MARK[script] not in r.stdout, "有数据时默认也不许算出结果"


# ══════════════════════════════════════════════════════════════════════════
# 显式放行：能跑、先打横幅、横幅在结果之前
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("script", SCRIPTS)
def test_opt_in_runs_and_prints_the_banner_before_any_result(script, tmp_path):
    home = tmp_path / "home"
    _make_db(home / "pheromone.db")

    r = _run(script, ["--pool-generations"], home, tmp_path)

    assert r.returncode in (0, 3), (                  # sweep：0=有幸存者 / 3=无；dilution：0
        f"{script} --pool-generations 应能跑完，实得 {r.returncode}。stderr 尾部：\n{r.stderr[-800:]}")
    out = r.stdout
    assert RESULT_MARK[script] in out, "放行后结果应当被算出来（合成库足够）"
    assert "跨世代混算" in out, "放行也要带横幅：只作对照，勿据此下结论"
    assert "正态近似" in out, "横幅要交代 p 值口径（n=26、t≈3.5 时低估约 4 倍）"
    assert out.index("跨世代混算") < out.index(RESULT_MARK[script]), "横幅必须在结果之前"


# ══════════════════════════════════════════════════════════════════════════
# 产物的读者也需要护栏：两份历史报告顶部的补注
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("report", ["signal_ic_sweep_report.md", "final_score_dilution_report.md"])
def test_historical_reports_carry_the_pooling_note(report):
    head = "\n".join((ROOT / "experiments" / report).read_text(encoding="utf-8").splitlines()[:14])
    assert "跨世代混算" in head, f"{report} 顶部缺跨世代混算补注（引用其结论的人会看不到）"
    assert "正态近似" in head, f"{report} 顶部缺 p 值口径补注"
