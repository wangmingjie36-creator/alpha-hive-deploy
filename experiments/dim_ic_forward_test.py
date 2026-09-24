#!/usr/bin/env python3
"""
🐝 Alpha Hive — 维度 IC 证据协议 · 执行器（P2，v0.45.325）
============================================================
**本文件只实现，不定规则。** 规则全文 `experiments/dim_ic_preregistration.md`（v0.45.320 登记，
2026-09-23 11:45 PDT），登记常量 `experiments/dim_ic_protocol.py`。两者与本文件冲突时，以登记为准，
本文件是 bug。

协议 §12 验收清单 ↔ 实现
------------------------
1. 常量全部从 `dim_ic_protocol.py` 读 —— `_protocol()`，调用时加载，本文件不抄任何一个数。
2. p 值用 t(G−1)、SE 用 NW(1) —— `series_test()`；不调用 `ic_diagnostics.normal_two_sided_p`。
3. 结果变量读 `close_t7` —— `load_rows()` 的 SQL 只出现登记的 `OUTCOME_COLUMN`，运行时再核一次它等于
   `ic_diagnostics.FORWARD_CLOSE_COL["t7"]`、不在禁用列里；数据层用 `signal_archive._truncation_share`
   的截断指纹自检（终点列若大面积等于 `exit_price` ⇒ 无法判定）。
4. 截断 —— `truncation_point()`（世代边界，复用 `signal_archive.generation_boundaries()` 的影响面语义）
   与 `weight_change()`（`config.py` 的 git 历史，见下）。
5. 盲化在数据结构上 —— `decide()` 只把**已出结论**的假设放进 `verdicts`；描述项只在协议结束时出现。
6. 进度行 —— `status_line()`，由 `ic_rerun_readiness.dim_ic_forward_status()` 接进 `--quiet` 那一行（第四段）。
7. 判别测试 —— `tests/test_dim_ic_forward_test.py`。

修订 1（v0.45.330，协议 §13）↔ 实现
-----------------------------------
8. H1 对象 buzz_v1 —— **冻结评分器（阶段 2）尚未实现**，所以 H1 读 `H1_PRODUCTION_DIM`（生产 sentiment 维度），
   冻结层边界与输入层边界一律照截断（§13.4 的保守分支）。截断点之前 buzz_v1 ≡ 锚点之后的生产分，这是定义。
   截断结果带 `layer`：`frozen` = 只触及冻结层，评分器自证通过后可解除；`input` = 触及输入层，不可解除。
9. 锚点 —— `anchor_status()`：`H1_ANCHOR_VERSION` 须在边界表里且早于 `FORWARD_START`；到窗口起点仍不满足 ⇒
   H1 回退到原登记对象（行为与第 8 条相同，只是对象名义不同），进度行标 ⚠️；截止前未登记在进度行提示截止日。
10. 自证 —— 阶段 2 实现；本版没有评分器，因此也不吸收任何冻结层边界。

事前实现选择（协议没写到、这里补的；改动须在 CHANGELOG 标「事前实现对齐」或「事后」）
---------------------------------------------------------------------------------
- **「这些周的 T+7 全部结算」**：一个 ISO 周里所有窗口内行都有结果变量，或该周周日之后已过
  `SETTLE_GRACE_DAYS` 天，即视为已结算。没有宽限，一条永久缺 `close_t7` 的行（本仓有前科）会让检视点
  永远不触发。合格周只从**已结算的连续前缀**里数——否则较早的一周晚些时候才结算，会插进检视集合的中间，
  检视用哪些周就不再是固定的。
- **权重偏离的日期**：唯一依据是 `config.py` 的 git 历史——登记日之后逐个提交解析 `EVALUATION_WEIGHTS`
  字面量，第一个与冻结值不同的提交的 committer 日期即截断日（偏保守：可能多丢半天样本）。工作区有未提交的
  偏离 ⇒ 截断日记为运行当天。`weight_history.jsonl` 不收录手工改 config（v0.45.172 就是手改），不用。
  git 不可用或浅克隆 ⇒ **明确报「无法判定」**，不当作「没变」。
- **单日横截面**：分数取值须有 ≥2 个不同值（并列全同的一天没有排序信息），与 `ic_diagnostics` 同一判据。
- **零方差**：周值全同时 NW SE=0 ⇒ 均值≠0 判 p=0、均值=0 判 p=1（同 F&G 检验的退化分支），并在结果里标出。

用法
----
    /usr/local/bin/python3 experiments/dim_ic_forward_test.py            # 进度（检视点前只有进度）
    /usr/local/bin/python3 experiments/dim_ic_forward_test.py --json
    /usr/local/bin/python3 experiments/dim_ic_forward_test.py --today 2027-04-05

退出码：0 = 协议已结束、全部结论已出 / 1 = 未到检视点或进行中（正常）/ 3 = 无法判定
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import importlib.util
import json
import math
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev
from typing import Callable, Dict, List, Optional, Tuple

# 模块级导入：scipy 缺失时，就绪度闸每个交易日加载本脚本都会渲染出「无法判定」那一行，
# 而不是等到检视点当天才炸（同 v0.45.323 对 F&G 检验的处理）。
from scipy import stats as _scipy_stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 代码锚点：import 仓内模块

import ic_diagnostics  # noqa: E402
import signal_archive  # noqa: E402

#: 结算宽限（天）：一周的周日之后过了这么多天，仍缺结果变量的行按缺失处理（见文件头）
SETTLE_GRACE_DAYS = 21
#: 登记窗口起点之后这么多天仍无任何窗口内样本 ⇒ 不是「还在攒」，是扫描停了或路径错了
STALE_DAYS = 21

_GIT_DIR_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


class ProtocolError(Exception):
    """数据或环境不满足协议前提 ⇒ 无法判定（exit 3），不是「没结果」。"""


# ────────────────────────────────────────────────────────────────────────────
# 登记常量
# ────────────────────────────────────────────────────────────────────────────

def _protocol():
    """加载登记常量。**调用时**加载，不在模块级冻一份。"""
    path = Path(__file__).resolve().parent / "dim_ic_protocol.py"  # 代码锚点：与本文件同目录的登记
    spec = importlib.util.spec_from_file_location("dim_ic_protocol", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check_protocol(P) -> None:
    """运行时再核一次结果变量——登记与仓内唯一真相不一致就不算。"""
    want = ic_diagnostics.FORWARD_CLOSE_COL["t7"]
    if P.OUTCOME_COLUMN != want:
        raise ProtocolError(f"登记的结果变量 {P.OUTCOME_COLUMN!r} ≠ ic_diagnostics.FORWARD_CLOSE_COL['t7']={want!r}")
    if P.OUTCOME_COLUMN in P.FORBIDDEN_OUTCOME_COLUMNS:
        raise ProtocolError(f"结果变量 {P.OUTCOME_COLUMN!r} 在登记的禁用列里")
    if P.HYPOTHESES[0][1] != "buzz_v1":
        # 执行器只实现了修订 1 的 H1；登记换了对象而执行器没跟 ⇒ 读错量，不能照常算
        raise ProtocolError(f"执行器不认识 H1 对象 {P.HYPOTHESES[0][1]!r}（只实现了修订 1 的 buzz_v1）")


def items(P) -> Dict[str, Dict]:
    """协议里所有要算 IC 的对象：{名字: {"value": 取值函数, "signals": 截断/隔离用的归档信号}}。"""
    def _dim(d):
        return lambda ds: ds.get(d)

    def _composite(ds):
        if any(ds.get(d) is None for d in P.FROZEN_WEIGHTS):
            return None          # 协议 §3：H2 只用 5 维齐全的标的
        return sum(w * ds[d] for d, w in P.FROZEN_WEIGHTS.items())

    (h1, _h1_obj, h1_sigs), (h2, _h2_obj, h2_sigs) = P.HYPOTHESES
    # H1 = buzz_v1。冻结评分器未实现 ⇒ 读生产维度，冻结层边界照截断（见文件头第 8 条）
    out = {h1: {"value": _dim(P.H1_PRODUCTION_DIM), "signals": h1_sigs},
           h2: {"value": _composite, "signals": h2_sigs}}
    for d in P.DESCRIPTIVE_DIMS:
        out[d] = {"value": _dim(d), "signals": (f"agent.{P.DIM_TO_AGENT[d]}.score",)}
    return out


# ────────────────────────────────────────────────────────────────────────────
# 数据
# ────────────────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    """`pheromone.db`，调用时求值（同 ic_diagnostics / signal_archive）。"""
    from hive_logger import PATHS
    return Path(PATHS.db)


def _iso_week(d: str) -> Tuple[int, int]:
    return tuple(dt.date.fromisoformat(d).isocalendar()[:2])


def _week_end(wk: Tuple[int, int]) -> dt.date:
    return dt.date.fromisocalendar(wk[0], wk[1], 7)


def load_rows(db_path: Path, P, today: str) -> List[Dict]:
    """窗口内（`FORWARD_START` ≤ date ≤ today、周一至周五）的全部预测行。

    `ret` = `OUTCOME_COLUMN / price_at_predict − 1`（%）；未结算为 None。
    **只读登记的结果变量这一列**——SQL 里不出现任何别的终点列。
    """
    _check_protocol(P)
    col = P.OUTCOME_COLUMN
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
        missing = {"date", "ticker", "dimension_scores", "price_at_predict", "checked_t7", col} - cols
        if missing:
            raise ProtocolError(f"predictions 缺列 {sorted(missing)}")
        n_sltp, share = signal_archive._truncation_share(con, col, "checked_t7")
        if n_sltp and share > 0.5:
            raise ProtocolError(f"{col} 的截断指纹异常：SL/TP 行里 {share:.0%}（{n_sltp} 行）恰好等于 exit_price"
                                "——结果变量看起来是离场价，不是 T+7 收盘价")
        raw = con.execute(
            f"SELECT date, ticker, dimension_scores, price_at_predict, checked_t7, {col} AS p_end "
            f"FROM predictions WHERE date >= ? AND dimension_scores IS NOT NULL AND price_at_predict > 0",
            (P.FORWARD_START,)).fetchall()
    finally:
        con.close()

    rows = []
    for r in raw:
        d = r["date"][:10]
        if d > today or dt.date.fromisoformat(d).weekday() >= 5:
            continue
        try:
            ds = json.loads(r["dimension_scores"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(ds, dict):
            continue
        ret = None
        if r["checked_t7"] == 1 and r["p_end"] is not None and r["p_end"] > 0:
            ret = (r["p_end"] / r["price_at_predict"] - 1) * 100.0
        rows.append({"date": d, "ticker": r["ticker"], "ds": ds, "ret": ret})
    return rows


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def weekly_series(rows: List[Dict], value: Callable, signals: Tuple[str, ...], P,
                  truncate_at: Optional[str] = None) -> List[Tuple[Tuple[int, int], float, int]]:
    """日度横截面 Spearman → 同 ISO 周内有效日等权平均。返回 [(周, 周值, 有效日数)]，按周排序。"""
    by_day: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if r["ret"] is None or (truncate_at and r["date"] >= truncate_at):
            continue
        if any(signal_archive.is_quarantined(r["date"], r["ticker"], s) for s in signals):
            continue
        v = value(r["ds"])
        if not _finite(v):
            continue
        by_day[r["date"]].append((float(v), r["ret"]))

    by_week: Dict[Tuple[int, int], List[float]] = defaultdict(list)
    for day, pairs in by_day.items():
        if len(pairs) < P.MIN_WIDTH or len({round(a, 9) for a, _ in pairs}) < 2:
            continue
        ic = ic_diagnostics.spearman([a for a, _ in pairs], [b for _, b in pairs])
        if ic is not None and math.isfinite(ic):
            by_week[_iso_week(day)].append(ic)
    return [(wk, mean(v), len(v)) for wk, v in sorted(by_week.items())]


def settled_prefix(rows: List[Dict], today: str) -> List[Tuple[int, int]]:
    """已结算的**连续前缀**周（见文件头「事前实现选择」）。"""
    scanned = sorted({_iso_week(r["date"]) for r in rows})
    pending = {_iso_week(r["date"]) for r in rows if r["ret"] is None}
    t = dt.date.fromisoformat(today)
    out = []
    for wk in scanned:
        if wk in pending and (t - _week_end(wk)).days < SETTLE_GRACE_DAYS:
            break
        out.append(wk)
    return out


# ────────────────────────────────────────────────────────────────────────────
# 截断
# ────────────────────────────────────────────────────────────────────────────

def truncation_point(signals: Tuple[str, ...], P, history: Optional[list] = None) -> Optional[Dict]:
    """`FORWARD_START` 之后第一条触及这些信号的世代边界（协议 §6）。

    逐条问 `signal_archive.generation_boundaries(signals, [该条])`——影响面闭包、未声明边界按
    「影响全部」处理，都沿用归档那边的唯一实现，本函数不另写一份。
    """
    hist = signal_archive._cohort_history() if history is None else history
    hits = []
    for entry in hist:
        date, version = entry[0], entry[1]
        if date < P.FORWARD_START:
            continue
        if signal_archive.generation_boundaries(signals, [entry]):
            hits.append({"date": date, "version": version})
    return min(hits, key=lambda h: h["date"]) if hits else None


def h1_truncation(P, history: Optional[list] = None) -> Optional[Dict]:
    """H1 的截断点（修订 1）：输入层与冻结层一律截断（评分器未实现，§13.4 保守分支），并标出层。

    `layer="frozen"`：截断点那天只触及冻结层 ⇒ 冻结评分器自证通过后可解除；`"input"`：不可解除。
    """
    cut = truncation_point(P.HYPOTHESES[0][2], P, history)
    if cut is None:
        return None
    by_input = truncation_point(P.H1_INPUT_SIGNALS, P, history)
    if by_input is not None and by_input["date"] <= cut["date"]:
        return {**by_input, "layer": "input"}      # 同一天两层都有 ⇒ 报输入层那条（不可解除的才是真原因）
    return {**cut, "layer": "frozen"}


def anchor_status(P, today: str, history: Optional[list] = None) -> Dict:
    """buzz_v1 的定义锚点（协议 §13.4）。

    `state`：`ok` 锚点边界已登记且早于窗口起点 / `pending` 未满足但还没到窗口起点 /
    `fallback` 到了窗口起点仍未满足 ⇒ H1 回退到原登记对象。`problem` 说明为什么不是 ok。
    """
    hist = signal_archive._cohort_history() if history is None else history
    v = P.H1_ANCHOR_VERSION
    dates = sorted(e[0] for e in hist if v and e[1] == v)
    if dates and dates[0] < P.FORWARD_START:
        return {"state": "ok", "version": v, "date": dates[0]}
    if not v:
        problem = "H1_ANCHOR_VERSION 未登记"
    elif not dates:
        problem = f"{v} 不在 _COHORT_HISTORY 里"
    else:
        problem = f"{v} 的边界日期 {dates[0]} 不早于窗口起点"
    state = "fallback" if today >= P.FORWARD_START else "pending"
    return {"state": state, "version": v, "date": dates[0] if dates else None, "problem": problem}


def _clean_git_env() -> Dict[str, str]:
    """去掉 git 钩子会注入的定位变量（同 F&G 检验 v0.45.308 的防线）。"""
    return {k: v for k, v in os.environ.items() if k not in _GIT_DIR_VARS}


def _git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60,
                           env=_clean_git_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProtocolError(f"git 不可用（{type(e).__name__}: {e}）") from e
    if r.returncode != 0:
        raise ProtocolError(f"git {' '.join(args)} 失败（exit {r.returncode}）："
                            f"{r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout.decode(errors="replace")


def weights_in_source(src: str) -> Optional[Dict[str, float]]:
    """从 config.py 源码里取 `EVALUATION_WEIGHTS` 字面量。不是字面量 / 找不到 ⇒ None。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EVALUATION_WEIGHTS" for t in node.targets):
            try:
                val = ast.literal_eval(node.value)
            except ValueError:
                return None
            return {str(k): float(v) for k, v in val.items()} if isinstance(val, dict) else None
    return None


def _same_weights(a: Optional[Dict], b: Dict) -> bool:
    return a is not None and set(a) == set(b) and all(abs(a[k] - b[k]) < 1e-9 for k in b)


def weight_change(P, today: str, repo_root: Optional[Path] = None) -> Dict:
    """`config.EVALUATION_WEIGHTS` 何时偏离登记的冻结值（协议 §6，H2 截断）。

    返回 `{"date": None}`（未偏离）、`{"date": 日期, "source": ...}`，或 `{"unknown": 原因}`。
    """
    repo = repo_root or Path(__file__).resolve().parent.parent  # 代码锚点：代码仓库本身
    try:
        if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
            return {"unknown": "浅克隆，登记之后的 config.py 历史可能不全"}
        log = _git(repo, "log", "--reverse", f"--since={P.REGISTERED_ON} 00:00",
                   "--format=%H %cs", "--", "config.py")
        for line in log.splitlines():
            sha, day = line.split()
            if not _same_weights(weights_in_source(_git(repo, "show", f"{sha}:config.py")),
                                 P.FROZEN_WEIGHTS):
                return {"date": max(day, P.REGISTERED_ON), "source": f"提交 {sha[:8]}"}
    except ProtocolError as e:
        return {"unknown": str(e)}
    try:
        wt = weights_in_source((repo / "config.py").read_text(encoding="utf-8"))
    except OSError as e:
        return {"unknown": f"读不到 config.py（{e}）"}
    if not _same_weights(wt, P.FROZEN_WEIGHTS):
        return {"date": today, "source": "工作区未提交的改动"}
    return {"date": None}


# ────────────────────────────────────────────────────────────────────────────
# 统计
# ────────────────────────────────────────────────────────────────────────────

def nw_se(vals: List[float], lag: int) -> float:
    """Newey-West（Bartlett 核）标准误。与 `ic_diagnostics.newey_west_t` 同一方差式
    （均值 / 本值 == 那边的 t），这里单独拿出 SE 是为了算 CI。方差 ≤0 ⇒ 0.0。"""
    n = len(vals)
    m = mean(vals)
    d = [v - m for v in vals]
    var = sum(x * x for x in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - L / (lag + 1)) * sum(d[i] * d[i - L] for i in range(L, n)) / n
    return math.sqrt(var / n) if var > 0 else 0.0


def series_test(vals: List[float], alpha: float, P) -> Dict:
    """两侧单样本检验（协议 §4）：t = 均值 / NW(lag) SE，参考 t(G−1)。"""
    G = len(vals)
    if G < 3:
        return {"G": G, "testable": False, "reason": f"合格周只有 {G} 个"}
    m = mean(vals)
    se = nw_se(vals, P.NW_LAG)
    df = G - 1
    crit95 = float(_scipy_stats.t.ppf(0.975, df))
    sd = stdev(vals)
    out = {"G": G, "testable": True, "ic": m, "se_nw": se, "df": df, "alpha": alpha,
           "t_iid": (m / (sd / math.sqrt(G)) if sd > 0 else None)}
    if se == 0:
        out.update({"t": None, "p": 0.0 if m != 0 else 1.0, "ci95": (m, m), "degenerate": True})
    else:
        t = m / se
        out.update({"t": t, "p": float(2 * _scipy_stats.t.sf(abs(t), df)),
                    "ci95": (m - crit95 * se, m + crit95 * se)})
    out["reject"] = out["p"] < alpha
    return out


def holm(pvals: Dict[str, float]) -> Dict[str, float]:
    """Holm 逐步校正后的 p（单调化）。"""
    order = sorted(pvals, key=pvals.get)
    m, running, out = len(order), 0.0, {}
    for i, k in enumerate(order):
        running = max(running, min(1.0, (m - i) * pvals[k]))
        out[k] = running
    return out


_ACTION = {  # 协议 §7，原样
    ("H1", "significant_positive"): "「sentiment 有预测力」升级为「已确认（预注册）」；权重调整幅度另行决策",
    ("H1", "significant_negative"): "sentiment 权重进复审，默认置 0 并排查原因；不自动取反",
    ("H1", "not_significant"): "撤回「唯一可交易信号」，改称「未证实」；现行权重不因此自动改",
    ("H2", "significant_positive"): "现行加权方案获确认（预注册）；权重调整幅度另行决策",
    ("H2", "significant_negative"): "现行加权方案进复审",
    ("H2", "not_significant"): "现行加权方案「未证实」；不因此自动改权重",
}


def _verdict(hid: str, s: Dict, look: str) -> Dict:
    if not s["testable"]:
        return {"result": "untestable", "look": look, "reason": s["reason"], "action": "未能检验，不做推断"}
    res = ("not_significant" if not s["reject"]
           else "significant_positive" if s["ic"] > 0 else "significant_negative")
    return {"result": res, "look": look, **s, "action": _ACTION[(hid, res)]}


# ────────────────────────────────────────────────────────────────────────────
# 检视与固定序列（纯函数：输入周序列，输出盲化后的结果）
# ────────────────────────────────────────────────────────────────────────────

def decide(h1_weeks: List[Tuple], h2_weeks: List[Tuple], P, *,
           h1_capacity: Optional[int] = None,
           h2_truncated: bool = False,
           h2_unknown: Optional[str] = None,
           descriptive: Optional[Dict[str, List[Tuple]]] = None) -> Dict:
    """协议 §5 的两次检视 + §2 的固定序列。

    `h1_weeks` / `h2_weeks`：**已结算前缀内**的 [(周, 周值, 日数)]，按周排序。
    `h1_capacity`：H1 被截断时，截断前最多可能有几个合格周（含未结算）；None = 未截断。
    `h2_unknown`：H2 的截断日无法判定（如权重历史不可读）⇒ 轮到 H2 时判「无法检验」。

    **盲化**：`verdicts` 里只放已出结论的假设；描述项只在协议结束（`concluded`）时给出。
    """
    (h1, *_), (h2, *_) = P.HYPOTHESES
    looks = P.LOOKS
    state = {h1: "open", h2: "open"}
    verdicts: Dict[str, Dict] = {}
    looks_done: List[str] = []
    cut = None

    def _concluded() -> bool:
        return state[h1] == "done" or (state[h1] == "rejected" and state[h2] == "done")

    for n_req, alpha, label in looks:
        if _concluded():
            break               # 中检就全部出结论 ⇒ 协议结束，不再推进检视点（描述项截止于结论那次检视）
        if len(h1_weeks) < n_req:
            if h1_capacity is not None and h1_capacity < n_req and state[h1] == "open":
                verdicts[h1] = {"result": "untestable", "look": label,
                                "reason": f"H1 已截断，截断前最多 {h1_capacity} 个合格周 < {n_req}",
                                "action": "未能检验（定义已变），不做推断；要继续须重新登记"}
                state[h1] = "done"
            break
        looks_done.append(label)
        cut = h1_weeks[n_req - 1][0]
        if state[h1] == "open":
            s = series_test([v for _, v, _ in h1_weeks[:n_req]], alpha, P)
            if s["reject"]:
                verdicts[h1] = _verdict(h1, s, label)
                state[h1] = "rejected"
        if state[h1] == "rejected" and state[h2] == "open":
            vals = [v for wk, v, _ in h2_weeks if wk <= cut]
            if h2_unknown:
                verdicts[h2] = {"result": "untestable", "look": label, "reason": h2_unknown,
                                "action": "未能检验，不做推断"}
                state[h2] = "done"
            elif h2_truncated and len(vals) < n_req:
                verdicts[h2] = {"result": "untestable", "look": label,
                                "reason": f"H2 已截断，截断前 {len(vals)} 个合格周 < {n_req}",
                                "action": "未能检验（定义已变），不做推断；要继续须重新登记"}
                state[h2] = "done"
            else:
                s2 = series_test(vals, alpha, P)
                if s2["reject"] or not s2["testable"]:
                    verdicts[h2] = _verdict(h2, s2, label)
                    state[h2] = "done"
        if (n_req, alpha, label) == looks[-1]:
            if state[h1] == "open":
                verdicts[h1] = _verdict(h1, series_test([v for _, v, _ in h1_weeks[:n_req]], alpha, P), label)
                state[h1] = "done"
            if state[h2] == "open":
                if state[h1] == "rejected":
                    verdicts[h2] = _verdict(h2, series_test([v for wk, v, _ in h2_weeks if wk <= cut],
                                                            alpha, P), label)
                else:
                    verdicts[h2] = {"result": "not_tested", "look": label,
                                    "reason": "固定序列：H1 未被拒绝，H2 不检验", "action": "无"}
                state[h2] = "done"

    concluded = _concluded()
    if concluded and state[h2] == "open":
        verdicts[h2] = {"result": "not_tested", "look": verdicts[h1].get("look"),
                        "reason": "固定序列：H1 未被拒绝，H2 不检验", "action": "无"}

    nxt = next((n for n, _, _ in looks if len(h1_weeks) < n), None)
    out = {"status": "concluded" if concluded else ("in_progress" if looks_done else "not_ready"),
           "h1_weeks": len(h1_weeks), "next_look_at": None if concluded else nxt,
           "looks_done": looks_done, "verdicts": verdicts}
    if concluded and descriptive is not None:
        tests = {d: series_test([v for wk, v, _ in w if cut is None or wk <= cut], 0.05, P)
                 for d, w in descriptive.items()}
        adj = holm({d: t["p"] for d, t in tests.items() if t["testable"]})
        out["descriptive"] = {d: {**t, "p_holm": adj.get(d)} for d, t in tests.items()}
    return out


# ────────────────────────────────────────────────────────────────────────────
# 编排
# ────────────────────────────────────────────────────────────────────────────

def run(db_path: Optional[str] = None, today: Optional[str] = None,
        repo_root: Optional[Path] = None, history: Optional[list] = None) -> Dict:
    P = _protocol()
    today = today or dt.date.today().isoformat()
    base = {"forward_start": P.FORWARD_START, "today": today}
    # 窗口开始前也照常走完：登记日到窗口起点之间改了 config 权重，同样作废 H2（权重检查从登记日算起）
    db = Path(db_path) if db_path else _db_path()
    if not db.exists():
        return {**base, "status": "cannot_judge", "reason": f"找不到 {db}"}
    try:
        rows = load_rows(db, P, today)
    except (ProtocolError, sqlite3.Error) as e:
        return {**base, "status": "cannot_judge", "reason": f"{type(e).__name__}: {e}"}

    its = items(P)
    h1, h2 = P.HYPOTHESES[0][0], P.HYPOTHESES[1][0]
    trunc = {k: truncation_point(v["signals"], P, history) for k, v in its.items()}
    trunc[h1] = h1_truncation(P, history)          # 同一个截断日，多标一个层（修订 1）
    anchor = anchor_status(P, today, history)
    wchg = weight_change(P, today, repo_root)
    h2_cut_dates = [d for d in ((trunc[h2] or {}).get("date"), wchg.get("date")) if d]
    h2_cut = min(h2_cut_dates) if h2_cut_dates else None

    prefix = set(settled_prefix(rows, today))
    series = {}
    for k, v in its.items():
        cut = h2_cut if k == h2 else (trunc[k] or {}).get("date")
        series[k] = [w for w in weekly_series(rows, v["value"], v["signals"], P, cut) if w[0] in prefix]

    h1_cut = (trunc[h1] or {}).get("date")
    h1_capacity = None
    if h1_cut:
        h1_capacity = len({_iso_week(r["date"]) for r in rows if r["date"] < h1_cut})

    res = decide(series[h1], series[h2], P, h1_capacity=h1_capacity,
                 h2_truncated=h2_cut is not None,
                 h2_unknown=(f"权重历史无法判定：{wchg['unknown']}" if "unknown" in wchg else None),
                 descriptive={d: series[d] for d in P.DESCRIPTIVE_DIMS})
    stale = (not rows and (dt.date.fromisoformat(today) - dt.date.fromisoformat(P.FORWARD_START)).days
             >= STALE_DAYS)
    return {**base, **res, "truncation": {h1: trunc[h1], h2: trunc[h2]},
            "weight_change": wchg, "stale": stale, "h1_anchor": anchor}


def status_line(res: Dict) -> str:
    """给 `ic_rerun_readiness --quiet` 的一段。图标约定同 F&G 那段：⚠️ 要显著标出 / ⏳ 正常 / 🔔 已到检视点。"""
    tag = "维度 IC 协议（v0.45.320 预注册·修订 1）"
    st = res.get("status")
    if st == "cannot_judge":
        return f"⚠️ {tag}无法判定：{res.get('reason')}"
    warns, notes = [], []
    tr = res.get("truncation") or {}
    if (tr.get("H1") or {}).get("date"):
        h = tr["H1"]
        undo = "；只触及冻结层，冻结评分器自证通过后可解除" if h.get("layer") == "frozen" else ""
        warns.append(f"H1 已于 {h['date']}（{h['version']}{undo}）截断")
    anc = res.get("h1_anchor") or {}
    if anc.get("state") == "fallback":
        warns.append(f"H1 锚点：{anc['problem']} ⇒ 已回退到原登记对象（生产 sentiment，协议 §13.4）")
    elif anc.get("state") == "pending":
        notes.append(f"H1 锚点待登记（{anc['problem']}；须早于 {res.get('forward_start')}，否则回退）")
    if (tr.get("H2") or {}).get("date"):
        warns.append(f"H2 已于 {tr['H2']['date']}（{tr['H2']['version']}）截断")
    wc = res.get("weight_change") or {}
    if wc.get("date"):
        warns.append(f"config 权重已偏离冻结值（{wc['date']}，{wc.get('source')}）⇒ H2 截断")
    if "unknown" in wc:
        warns.append(f"权重历史无法判定（{wc['unknown']}）")
    if res.get("stale"):
        warns.append(f"登记窗口起点 {res.get('forward_start')} 后仍无任何样本——扫描停了或路径错了")
    tail = ("；⚠️ " + "；".join(warns)) if warns else ""
    tail += "".join(f"；{n}" for n in notes)
    if st == "concluded":
        v = res.get("verdicts", {})
        parts = [f"{h}={v[h]['result']}" for h in ("H1", "H2") if h in v]
        return (f"🔔 {tag}已出结论：{'，'.join(parts)} —— 跑 `experiments/dim_ic_forward_test.py` 看全文"
                f"{tail}")
    if st == "in_progress":
        shown = [f"{h}={r['result']}" for h, r in res.get("verdicts", {}).items()]
        body = (f"已过 {'/'.join(res['looks_done'])} 检视" + (f"（{'，'.join(shown)}）" if shown else "，未过界，继续"))
        return f"{'⚠️' if warns else '🔔'} {tag}{body}；H1 合格周 {res['h1_weeks']}/{res['next_look_at']}{tail}"
    head = "⚠️" if warns else "⏳"
    return (f"{head} {tag}：H1 已结算合格周 {res.get('h1_weeks', 0)}/{res.get('next_look_at')}"
            f"（窗口自 {res.get('forward_start')}）{tail}")


def _print_report(res: Dict) -> None:
    print("━" * 72)
    print("🐝 Alpha Hive · 维度 IC 证据协议（v0.45.320 预注册 · v0.45.330 修订 1，执行器 v0.45.325/330）")
    print("━" * 72)
    print(status_line(res))
    if res.get("status") == "cannot_judge":
        return
    for h, v in res.get("verdicts", {}).items():
        print(f"\n  【{h}】{v['result']}（{v.get('look')}检视）")
        if "ic" in v:
            lo, hi = v["ci95"]
            print(f"    IC {v['ic']:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]  G={v['G']}  "
                  f"t={v['t'] if v['t'] is None else round(v['t'], 3)}  df={v['df']}  p={v['p']:.4f}  α={v['alpha']}")
        if v.get("reason"):
            print(f"    {v['reason']}")
        print(f"    动作（协议 §7）：{v['action']}")
    if "descriptive" in res:
        print("\n  描述项（不触发动作；Holm 族 = 4）")
        for d, t in res["descriptive"].items():
            if t["testable"]:
                print(f"    {d:<9} IC {t['ic']:+.4f}  G={t['G']}  p={t['p']:.4f}  p_holm={t['p_holm']:.4f}")
            else:
                print(f"    {d:<9} {t['reason']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="维度 IC 证据协议执行器（检视点前只报进度）")
    ap.add_argument("--db", default=None, help="pheromone.db 路径（默认 PATHS.db）")
    ap.add_argument("--today", default=None, help="覆盖今天的日期（YYYY-MM-DD）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run(db_path=args.db, today=args.today)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=list))
    else:
        _print_report(res)
    return {"concluded": 0, "cannot_judge": 3}.get(res.get("status"), 1)


if __name__ == "__main__":
    raise SystemExit(main())
