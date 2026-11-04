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
10. 自证 —— v0.45.556 起 `buzz_v1_self_proof()` 每次都跑 `buzz_v1.self_proof`（§13.2）：未通过 / 算不出 ⇒ 进度行 ⚠️；
    检视点上未通过 ⇒「无法判定」（H1 读的生产分是否仍是 buzz_v1 无法确认）。**仍不吸收冻结层边界**（阶段 2 第 3 块）。
11. 输入层代码指纹（v0.45.554，事前实现对齐：§6「按层声明」的机械执行）—— `input_code_guard()` /
    `apply_code_guard()`。输入层代码有未确认的改动（`signal_code_fingerprints.ACKS` 没跟上）⇒ 进度行 ⚠️；
    到检视点仍未确认 ⇒「无法判定」、不出结论。守卫自己坏了同样算不 ok。

修订 4（v0.45.708，协议 §13.13）↔ 实现——**上面第 8、10 条的「H1」此后指描述项 buzz_v1**
-------------------------------------------------------------------------------------
12. H1 对象 buzz_v1_np —— `load_rows()` 从 `signal_archive` 读三个情绪通道挂到每行的 `ch`，`items()` 按
    `H1_NP_WEIGHTS` 的顺序线性加权（无 clamp、无调整）。三个缺一 ⇒ 该行对 H1 无效；`signal_archive` 表不在 ⇒ 无法判定
    （不是「H1 没样本」）。截断集合 = 三个通道，`h1_truncation()` 的层恒为 `input`（H1 没有冻结层）。
13. 描述项 buzz_v1 —— 照旧读生产 sentiment、照旧按输入层 + 冻结层截断；`decide()` 只在协议出结论时给出
    描述项结果（IC / CI / 朴素 p，不进 Holm 族；修订 5 起键名 `descriptive_extra`，见第 18 条）。
14. 自证（第 10 条）此后只关系到描述项 buzz_v1：未通过只在进度行 ⚠️、**不再**把检视点改判「无法判定」。
15. 指纹守卫（第 11 条）只让**信号触及 H1 截断集合**的单元挡检视点；其余单元（行情字段、F&G）的未确认改动
    只在进度行提示（它们只影响描述项 buzz_v1）。

修订 5（v0.45.712，协议 §13.14）↔ 实现
-------------------------------------
16. H1 的通道分两段 —— `_load_channels()`：`H1_ARCHIVE_START` 之前读 `signal_archive` 存档值，并核对
    `H1_PRE_ARCHIVE_SHA256`（对不上 ⇒ 无法判定）；此后由 `buzz_v1.h1_channels()` 从观测档案经冻结通道层重算。
    观测不全 / 没有溯源块 / 冻结层报错的（日，标的）⇒ 该行对 H1 无效（不补值），`run()` 的 `h1_excluded` 照实计数，
    其中冻结层报错的单列（进度行 ⚠️）。`observation_links` 表不在而窗口已过档案起点 ⇒ 无法判定。
17. H1 截断集合 = 取数层伪节点 `obs.*`；指纹守卫只让信号触及它们的单元（`obs.*`、`h1.frozen`）挡检视点。
18. 原 H2 降为描述项 weighted_composite —— `decide()` 只有 H1 一条确认性检验；描述项在协议结束时给出
    `descriptive_extra`（buzz_v1、weighted_composite：IC / CI / 朴素 p，不进 Holm 族）。权重偏离 / 三蜂边界
    只截断该描述项，进度行写成说明。

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

import day_health  # noqa: E402
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
    if P.HYPOTHESES[0][1] != "buzz_v1_np":
        # 执行器只实现了修订 4 的 H1；登记换了对象而执行器没跟 ⇒ 读错量，不能照常算
        raise ProtocolError(f"执行器不认识 H1 对象 {P.HYPOTHESES[0][1]!r}（只实现了修订 4 的 buzz_v1_np）")


def nonprice_composite(ch: Dict[str, float], weights: Dict[str, float]) -> Optional[float]:
    """buzz_v1_np（修订 4）：按 `weights` 的顺序从左往右加。缺一个通道 / 非有限 ⇒ None（该行对 H1 无效，不补值）。"""
    total = 0.0
    for c, w in weights.items():
        v = ch.get(c)
        if not _finite(v):
            return None
        total = total + v * w
    return total


def items(P) -> Dict[str, Dict]:
    """协议里所有要算 IC 的对象：{名字: {"value": 取值函数（收一整行）, "signals": 截断/隔离用的归档信号[, "quarantine": 查隔离用的信号]}}。"""
    def _dim(d):
        return lambda r: r["ds"].get(d)

    def _composite(r):
        ds = r["ds"]
        if any(ds.get(d) is None for d in P.FROZEN_WEIGHTS):
            return None          # 协议 §3：（原 H2）只用 5 维齐全的标的
        return sum(w * ds[d] for d, w in P.FROZEN_WEIGHTS.items())

    ((h1, _h1_obj, h1_sigs),) = P.HYPOTHESES
    v1_name, v1_sigs = P.H1_V1_DESCRIPTIVE
    wc_name, wc_sigs = P.H2_DESCRIPTIVE
    # H1：截断集合 = 登记的 `obs.*`（修订 5）；隔离名单按通道名登记 ⇒ 查隔离用三个通道名（`quarantine`，缺省同 signals）
    out = {h1: {"value": lambda r: nonprice_composite(r.get("ch") or {}, P.H1_NP_WEIGHTS), "signals": h1_sigs,
                "quarantine": P.H1_CHANNEL_SIGNALS},
           # 描述项 buzz_v1（修订 4）：读生产维度，冻结层与输入层边界照截断（文件头第 8、13 条）
           v1_name: {"value": _dim(P.H1_PRODUCTION_DIM), "signals": v1_sigs},
           # 描述项 weighted_composite（修订 5，原 H2）：冻结权重的五维合成，三蜂边界 / 权重偏离照截断（第 18 条）
           wc_name: {"value": _composite, "signals": wc_sigs}}
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

    ch_by_key, excluded = _load_channels(db_path, P, today)
    rows = []
    for r in raw:
        d = r["date"][:10]
        if d > today or dt.date.fromisoformat(d).weekday() >= 5:
            continue
        # 修订 2（v0.45.484，事后）：整天由事后重放补算的日子不进检验（`signal_archive.FORWARD_TEST_EXCLUDED_DAYS`）。
        # 被排除的日子由 run() 的 `forward_test_excluded_days` 报出来，不是静默少行。
        if signal_archive.forward_test_excluded(d):
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
        key = (d, r["ticker"])
        why = excluded.get(key)
        if why is None and d >= P.H1_ARCHIVE_START and key not in ch_by_key:
            why = "没有溯源块（生产有分，却没写原始观测链接）"
        rows.append({"date": d, "ticker": r["ticker"], "ds": ds, "ret": ret,
                     "ch": ch_by_key.get(key, {}), "h1_excluded": why})
    return rows


def pre_archive_digest(rows: List[List]) -> str:
    """`H1_PRE_ARCHIVE_SHA256` 的算法（协议 §13.14）：行 = [date, ticker, signal, value]，按 (date, ticker, signal) 排序，紧凑 JSON。"""
    import hashlib
    body = json.dumps(sorted(rows, key=lambda r: (r[0], r[1], r[2])), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _load_channels(db_path: Path, P, today: str) -> Tuple[Dict[Tuple[str, str], Dict[str, float]], Dict[Tuple[str, str], str]]:
    """H1 读的三个情绪通道：({(日期, 标的): {通道: 值}}, {(日期, 标的): 排除原因})。

    修订 5（§13.14）分两段：
    - `H1_ARCHIVE_START` 之前：`signal_archive` 的存档值；这段的全部值与登记的 `H1_PRE_ARCHIVE_SHA256` 不一致 ⇒
      `ProtocolError`（同日重扫 / 回填改写了过去的值，H1 读的已不是登记的那些数）。表不在同样无法判定。
    - 此后：`buzz_v1.h1_channels` 由冻结通道层从观测档案重算；排除的格连同原因交回。`observation_links` 表不在 ⇒
      `sqlite3.OperationalError` 由 `run()` 判无法判定。
    """
    names = {f"buzz.comp.{c}": c for c in P.H1_NP_WEIGHTS}
    pre_end = min(P.H1_ARCHIVE_START, (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat())
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_archive'").fetchone():
            raise ProtocolError("pheromone.db 里没有 signal_archive 表——H1（buzz_v1_np）档案起点之前那段读不到通道值")
        q = ",".join("?" * len(names))
        raw = con.execute(f"SELECT date, ticker, signal, value FROM signal_archive "
                          f"WHERE signal IN ({q}) AND date >= ? AND date < ?",
                          (*names, P.FORWARD_START, pre_end)).fetchall()
    finally:
        con.close()
    pre = [[str(d)[:10], t, sig, v] for d, t, sig, v in raw
           if v is not None and not signal_archive.forward_test_excluded(str(d)[:10])]
    if today >= P.H1_ARCHIVE_START:
        # 档案起点之前那段已经完整：逐值核对登记的哈希（today 早于档案起点时这段还在长，没有可比的定值）
        got = pre_archive_digest(pre)
        if len(pre) != P.H1_PRE_ARCHIVE_ROWS or got != P.H1_PRE_ARCHIVE_SHA256:
            raise ProtocolError(f"H1 档案起点（{P.H1_ARCHIVE_START}）之前的存档通道值与登记不符：{len(pre)} 个值、"
                                f"sha256 {got[:16]}…，登记 {P.H1_PRE_ARCHIVE_ROWS} 个、{P.H1_PRE_ARCHIVE_SHA256[:16]}…"
                                "——同日重扫或回填改写了过去的值（协议 §13.14）")
    out: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(dict)
    for d, t, sig, v in pre:
        out[(d, t)][names[sig]] = float(v)
    excluded: Dict[Tuple[str, str], str] = {}
    if today >= P.H1_ARCHIVE_START:
        try:
            import buzz_v1                      # 调用时导入：它坏了 ⇒ 无法判定（H1 取不到值），不当作没样本
        except Exception as e:  # noqa: BLE001
            raise ProtocolError(f"冻结通道层加载失败：{type(e).__name__}: {e}") from e
        for key, rec in buzz_v1.h1_channels(str(db_path), P.H1_ARCHIVE_START, today,
                                            channels=tuple(P.H1_NP_WEIGHTS)).items():
            if "channels" in rec:
                out[key] = dict(rec["channels"])
            else:
                excluded[key] = rec["excluded"]
    return out, excluded


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
        v = value(r)
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
    """H1 的截断点，并标出层。修订 4 起 H1 的截断集合全在输入层（修订 5 起 = 取数层伪节点 `obs.*`）⇒ 层恒为 `input`。

    （修订 1 时期 H1 = buzz_v1，还有冻结层：`layer="frozen"` = 只触及冻结层、冻结评分器自证通过后可解除。
    那条分支留着：截断集合里若再出现非输入层信号，它照旧能标出来。）
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
    """`config.EVALUATION_WEIGHTS` 何时偏离登记的冻结值（协议 §6：截断描述项 weighted_composite，修订 5 前为 H2）。

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
}  # 修订 5（§13.14）：原 H2 的三行动作随 H2 降为描述项一起撤掉——描述项不触发任何动作


def _verdict(hid: str, s: Dict, look: str) -> Dict:
    if not s["testable"]:
        return {"result": "untestable", "look": look, "reason": s["reason"], "action": "未能检验，不做推断"}
    res = ("not_significant" if not s["reject"]
           else "significant_positive" if s["ic"] > 0 else "significant_negative")
    return {"result": res, "look": look, **s, "action": _ACTION[(hid, res)]}


# ────────────────────────────────────────────────────────────────────────────
# 检视与固定序列（纯函数：输入周序列，输出盲化后的结果）
# ────────────────────────────────────────────────────────────────────────────

def decide(h1_weeks: List[Tuple], P, *,
           h1_capacity: Optional[int] = None,
           descriptive: Optional[Dict[str, List[Tuple]]] = None,
           extra: Optional[Dict[str, List[Tuple]]] = None) -> Dict:
    """协议 §5 的两次检视。修订 5 起只有 H1 一条确认性检验（原 H2 降为描述项）。

    `h1_weeks`：**已结算前缀内**的 [(周, 周值, 日数)]，按周排序。
    `h1_capacity`：H1 被截断时，截断前最多可能有几个合格周（含未结算）；None = 未截断。
    `descriptive`：Holm 族（协议 §2 的 4 个维度）。`extra`：不进 Holm 族的描述项（buzz_v1、weighted_composite）——
    只在协议结束时报 IC / CI / 朴素 p（`descriptive_extra`）。

    **盲化**：`verdicts` 里只放已出结论的假设；描述项只在协议结束（`concluded`）时给出。
    """
    ((h1, *_),) = P.HYPOTHESES
    looks = P.LOOKS
    state = "open"
    verdicts: Dict[str, Dict] = {}
    looks_done: List[str] = []
    cut = None

    for n_req, alpha, label in looks:
        if state != "open":
            break               # 中检就出结论 ⇒ 协议结束，不再推进检视点（描述项截止于结论那次检视）
        if len(h1_weeks) < n_req:
            if h1_capacity is not None and h1_capacity < n_req:
                verdicts[h1] = {"result": "untestable", "look": label,
                                "reason": f"H1 已截断，截断前最多 {h1_capacity} 个合格周 < {n_req}",
                                "action": "未能检验（定义已变），不做推断；要继续须重新登记"}
                state = "done"
            break
        looks_done.append(label)
        cut = h1_weeks[n_req - 1][0]
        s = series_test([v for _, v, _ in h1_weeks[:n_req]], alpha, P)
        if s["reject"] or (n_req, alpha, label) == looks[-1]:
            verdicts[h1] = _verdict(h1, s, label)
            state = "done"

    concluded = state == "done"
    nxt = next((n for n, _, _ in looks if len(h1_weeks) < n), None)
    out = {"status": "concluded" if concluded else ("in_progress" if looks_done else "not_ready"),
           "h1_weeks": len(h1_weeks), "next_look_at": None if concluded else nxt,
           "looks_done": looks_done, "verdicts": verdicts}
    if concluded and descriptive is not None:
        tests = {d: series_test([v for wk, v, _ in w if cut is None or wk <= cut], 0.05, P)
                 for d, w in descriptive.items()}
        adj = holm({d: t["p"] for d, t in tests.items() if t["testable"]})
        out["descriptive"] = {d: {**t, "p_holm": adj.get(d)} for d, t in tests.items()}
    if concluded and extra is not None:
        out["descriptive_extra"] = {d: series_test([v for wk, v, _ in w if cut is None or wk <= cut], 0.05, P)
                                    for d, w in extra.items()}
    return out


# ────────────────────────────────────────────────────────────────────────────
# 输入层代码指纹守卫（v0.45.554，协议 §6「按层声明」的机械执行、§12 第 11 条）
# ────────────────────────────────────────────────────────────────────────────

def input_code_guard(h1_signals: Optional[Tuple[str, ...]] = None) -> Dict:
    """`signal_code_fingerprints.check()` 的摘要：{"ok", "unacknowledged": [单元], "errors": {单元: 原因},
    "descriptive_only": {"unacknowledged": [...], "errors": {...}}}。

    H1 是否该截断，取决于「输入层代码的每次改动都登记了」——这一前提由指纹守卫核对。
    修订 4：只有信号触及 H1 截断集合（`h1_signals`，缺省取登记）的单元算进 `ok`；其余单元（行情字段、F&G）
    只影响描述项 buzz_v1，它们的问题放进 `descriptive_only`，只提示、不挡检视点。
    守卫自己加载 / 计算失败，同样当作不 ok（渲染成 ⚠️），不当作「没改动」。
    """
    try:
        import signal_code_fingerprints as scf   # 调用时导入：它坏了只影响这一项，不拖垮整个执行器
        if h1_signals is None:
            h1_signals = _protocol().HYPOTHESES[0][2]
        res = scf.check()
        gating = {u for u, spec in scf.UNITS.items() if set(spec["signals"]) & set(h1_signals)}
    except Exception as e:  # noqa: BLE001 —— 任何失败都必须变成可见的 ⚠️
        return {"ok": False, "unacknowledged": [], "errors": {"*": f"{type(e).__name__}: {e}"},
                "descriptive_only": {"unacknowledged": [], "errors": {}}}
    if not gating:
        # 没有任何单元守着 H1 的截断集合 = 守卫形同虚设，不能当作通过
        return {"ok": False, "unacknowledged": [], "errors": {"*": "没有任何指纹单元触及 H1 的截断信号"},
                "descriptive_only": {"unacknowledged": [], "errors": {}}}
    units = res["units"]

    def _pick(keep):
        return ([u for u, x in units.items() if keep(u) and x["state"] == "unacknowledged"],
                {u: x["error"] for u, x in units.items() if keep(u) and x["state"] == "error"})

    unack, errs = _pick(lambda u: u in gating)
    d_unack, d_errs = _pick(lambda u: u not in gating)
    return {"ok": all(units[u]["state"] == "ok" for u in gating if u in units) and gating <= set(units),
            "unacknowledged": unack, "errors": errs,
            "descriptive_only": {"unacknowledged": d_unack, "errors": d_errs}}


def _guard_problem(guard: Dict) -> str:
    parts = []
    if guard.get("unacknowledged"):
        parts.append(f"输入层代码有未确认的改动（{'、'.join(guard['unacknowledged'])}）")
    if guard.get("errors"):
        parts.append("指纹守卫算不出：" + "；".join(f"{u}: {e}" for u, e in guard["errors"].items()))
    return "；".join(parts) or "指纹守卫未通过"


def apply_code_guard(res: Dict, guard: Dict) -> Dict:
    """检视点（in_progress / concluded）上守卫不 ok ⇒ 改判「无法判定」、不出任何结论（盲化不变）。

    未到检视点时只附上守卫状态，由进度行渲染成 ⚠️——还有时间去登记，不必提前作废什么。
    """
    if guard.get("ok") or res.get("status") not in ("in_progress", "concluded"):
        return {**res, "input_code_guard": guard}
    keep = {k: res[k] for k in ("forward_start", "today", "forward_test_excluded_days") if k in res}
    return {**keep, "status": "cannot_judge", "input_code_guard": guard,
            "reason": (f"{_guard_problem(guard)}——H1 该不该截断无法判定（协议 §6 按层声明），先跑 "
                       "`signal_code_fingerprints.py` 按提示登记，再重跑本脚本")}


# ────────────────────────────────────────────────────────────────────────────
# buzz_v1 自证（v0.45.556，协议 §13.2、§12 第 10 条）
# ────────────────────────────────────────────────────────────────────────────

#: `excluded_days` / `n_excluded`（v0.45.564）：自证跳过的协议排除日与行数——跳过了什么照样报出来，不静默
_SELF_PROOF_KEYS = ("ok", "window", "n", "n_matched", "n_mismatched", "n_missing", "stopped_by", "error",
                    "excluded_days", "n_excluded")


def buzz_v1_self_proof(db: Path, today: str) -> Dict:
    """冻结评分器自证的摘要（只含计数与窗口，不含任何收益 / 效应量）。算不出同样算未通过。"""
    try:
        import buzz_v1                      # 调用时导入：它坏了只影响这一项
        r = buzz_v1.self_proof(str(db), end=today)
    except Exception as e:  # noqa: BLE001 —— 任何失败都必须变成可见的 ⚠️
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {k: r[k] for k in _SELF_PROOF_KEYS if k in r}


#: 桥接核对摘要（v0.45.583，根治阶段 2）只留计数与状态——不含任何收益 / 效应量
_BRIDGE_KEYS = ("ok", "ready", "end", "required_days", "error", "note")
_BRIDGE_GROUP_KEYS = ("n_pass", "n_fail", "n_incomplete", "n_vacuous", "ready", "first_fail")


def buzz_v1_bridge(db: Path, today: str) -> Dict:
    """冻结通道层桥接核对的摘要（`buzz_v1.bridge`）。算不出同样记 ok=False（可见），不当作通过。

    **只是进度**：阶段 4（协议修订 3）之前 H1 仍读入档通道，桥接失败不改 H1 的状态，只在进度行标 ⚠️、外壳出一条。
    """
    try:
        import buzz_v1
        r = buzz_v1.bridge(str(db), end=today)
    except Exception as e:  # noqa: BLE001 —— 任何失败都必须变成可见的 ⚠️
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out = {k: r[k] for k in _BRIDGE_KEYS if k in r}
    out["groups"] = {g: {k: G.get(k) for k in _BRIDGE_GROUP_KEYS} for g, G in (r.get("groups") or {}).items()}
    out["score"] = dict(r.get("score") or {})
    out["n_errors"] = len(r.get("errors") or [])
    return out


def _bridge_text(br: Dict) -> str:
    if br.get("error"):
        return f"buzz_v1 桥接核对算不出（{br['error']}）"
    if br.get("note"):
        return f"buzz_v1 桥接核对：{br['note']}"
    names = {"sentiment": "情绪", "price": "行情"}
    parts = [f"{names.get(g, g)} {G.get('n_pass', 0)}/{br.get('required_days')}"
             + (f"（失败 {G['n_fail']}，首次 {G.get('first_fail')}）" if G.get("n_fail") else "")
             for g, G in (br.get("groups") or {}).items()]
    sc = br.get("score") or {}
    tail = f"，整分逐位 {sc.get('n_match', 0)}/{sc.get('n', 0)}" if sc.get("n") else ""
    tail += f"，冻结层报错 {br['n_errors']} 次" if br.get("n_errors") else ""
    return f"buzz_v1 桥接核对 {'；'.join(parts)} 天通过{tail}"


def _self_proof_problem(sp: Dict) -> str:
    if sp.get("error"):
        return f"buzz_v1 自证算不出（{sp['error']}）"
    return (f"buzz_v1 自证未通过（{sp.get('n_matched', 0)}/{sp.get('n', 0)} 逐位一致，失配 {sp.get('n_mismatched', 0)}、"
            f"缺输入 {sp.get('n_missing', 0)}）")


def apply_self_proof(res: Dict, sp: Dict) -> Dict:
    """附上自证状态。修订 4 起自证只关系到描述项 buzz_v1（H1 = buzz_v1_np 直接由归档通道算，不经冻结评分器）⇒
    未通过只在进度行 ⚠️，**不**改判检视结论（修订 1 时期这里会把检视点改判「无法判定」）。"""
    return {**res, "buzz_v1_self_proof": sp}


# ────────────────────────────────────────────────────────────────────────────
# 编排
# ────────────────────────────────────────────────────────────────────────────

def h1_exclusions(rows: List[Dict]) -> Dict:
    """修订 5（§13.14）：H1 排除了哪些（日，标的）——只有计数与原因，不含收益。冻结层报错的单列（那是要查的 bug）。"""
    import buzz_v1
    hit = [r for r in rows if r.get("h1_excluded")]
    by_day: Dict[str, int] = defaultdict(int)
    for r in hit:
        by_day[r["date"]] += 1
    frozen_err = [r for r in hit if r["h1_excluded"].startswith(buzz_v1.H1_FROZEN_ERROR_PREFIX)]
    return {"n": len(hit), "by_day": dict(sorted(by_day.items())), "n_frozen_errors": len(frozen_err),
            "examples": [{"date": r["date"], "ticker": r["ticker"], "why": r["h1_excluded"]}
                         for r in (frozen_err + [x for x in hit if x not in frozen_err])[:10]]}


def sensitivity_block(dh: Dict, rows: List[Dict], decide_on: Callable[[List[Dict]], Dict], *, revealed: bool) -> Dict:
    """修订 3 的敏感性分析：同一条判定链跑在「去掉 `source_outage` 日」的样本上（实现在 `day_health.sensitivity_block`，两份检验共用）。

    **不改主分析、不触发任何动作**。盲化期只露计数（去掉哪几天、少几行、H1 合格周数）；`status` / `verdicts` / `descriptive`
    都只在 `revealed`（主分析本次已给出结论）时露出——`status` 本身就是检视结论（concluded），未露出时放进去是盲化旁路。
    """
    def _reveal(r: Dict) -> Dict:
        out = {"status": r["status"], "verdicts": r["verdicts"]}
        for k in ("descriptive", "descriptive_extra"):
            if k in r:
                out[k] = r[k]
        return out
    return day_health.sensitivity_block(dh, rows, decide_on, revealed=revealed,
                                        counts=lambda r, kept: {"h1_weeks": r["h1_weeks"]}, reveal=_reveal)


def run(db_path: Optional[str] = None, today: Optional[str] = None,
        repo_root: Optional[Path] = None, history: Optional[list] = None,
        code_guard: Optional[Dict] = None, self_proof: Optional[Dict] = None,
        bridge: Optional[Dict] = None) -> Dict:
    P = _protocol()
    today = today or dt.date.today().isoformat()
    base = {"forward_start": P.FORWARD_START, "today": today,
            # 修订 2（v0.45.484）：协议排除日照实报出（只列窗口内、已到 today 的）
            "forward_test_excluded_days": sorted(d for d in signal_archive.FORWARD_TEST_EXCLUDED_DAYS
                                                 if P.FORWARD_START <= d <= today)}
    # 窗口开始前也照常走完：登记日到窗口起点之间改了 config 权重，同样截断描述项 weighted_composite（权重检查从登记日算起）
    db = Path(db_path) if db_path else _db_path()
    if not db.exists():
        return {**base, "status": "cannot_judge", "reason": f"找不到 {db}"}
    try:
        rows = load_rows(db, P, today)
    except (ProtocolError, sqlite3.Error) as e:
        return {**base, "status": "cannot_judge", "reason": f"{type(e).__name__}: {e}"}

    its = items(P)
    h1 = P.HYPOTHESES[0][0]
    v1_name, wc_name = P.H1_V1_DESCRIPTIVE[0], P.H2_DESCRIPTIVE[0]
    trunc = {k: truncation_point(v["signals"], P, history) for k, v in its.items()}
    trunc[h1] = h1_truncation(P, history)          # 同一个截断日，多标一个层（修订 1）
    anchor = anchor_status(P, today, history)
    wchg = weight_change(P, today, repo_root)
    wc_cut_dates = [d for d in ((trunc[wc_name] or {}).get("date"), wchg.get("date")) if d]
    wc_cut = min(wc_cut_dates) if wc_cut_dates else None
    extra_names = (v1_name, wc_name)

    def _decide_on(rows_: List[Dict]) -> Dict:
        """同一条判定链跑在给定样本上。主分析用全部 `rows`；敏感性分析（修订 3）用去掉故障日后的子集——链本身一字不改。"""
        prefix = set(settled_prefix(rows_, today))
        series = {}
        for k, v in its.items():
            cut = wc_cut if k == wc_name else (trunc[k] or {}).get("date")
            series[k] = [w for w in weekly_series(rows_, v["value"], v.get("quarantine", v["signals"]), P, cut)
                         if w[0] in prefix]

        h1_cut = (trunc[h1] or {}).get("date")
        h1_capacity = None
        if h1_cut:
            h1_capacity = len({_iso_week(r["date"]) for r in rows_ if r["date"] < h1_cut})

        r = decide(series[h1], P, h1_capacity=h1_capacity,
                   descriptive={d: series[d] for d in P.DESCRIPTIVE_DIMS},
                   extra={k: series[k] for k in extra_names})
        if "unknown" in wchg and "descriptive_extra" in r:
            # 权重历史判不了 ⇒ weighted_composite 截没截断不知道，不报一个可能混了两套权重的数
            r["descriptive_extra"][wc_name] = {"testable": False, "reason": f"权重历史无法判定：{wchg['unknown']}"}
        return r

    res = _decide_on(rows)
    stale = (not rows and (dt.date.fromisoformat(today) - dt.date.fromisoformat(P.FORWARD_START)).days
             >= STALE_DAYS)
    out = {**base, **res, "truncation": {k: trunc[k] for k in (h1,) + extra_names},
           "weight_change": wchg, "stale": stale, "h1_anchor": anchor,
           "h1_excluded": h1_exclusions(rows)}
    out = apply_code_guard(out, code_guard if code_guard is not None else input_code_guard(P.HYPOTHESES[0][2]))
    out = apply_self_proof(out, self_proof if self_proof is not None else buzz_v1_self_proof(db, today))
    # v0.45.583：桥接核对只是进度；修订 5（§13.14）起 H1 就是冻结通道层的值，桥接只是监测生产与它对不对得上，无论上面判成什么都附上
    out["buzz_v1_bridge"] = bridge if bridge is not None else buzz_v1_bridge(db, today)
    # v0.45.598：窗口内哪些日子是「数据源整体降级的故障日」——**只显示**：rows / series / decide 都在上面算完了，
    # 这里不回头改任何一行样本，也不影响 status / verdicts（口径变更要走协议修订，不能顺手做）。
    out["day_health"] = day_health.summarize_days({r["date"] for r in rows})
    # 修订 3（2026-10-22）：敏感性分析——并列报告「去掉 source_outage 日」的结果。主分析（上面的 `out`）不动；
    # 结论只在主分析也给出结论（`verdicts` 非空）时才露出，盲化口径与主分析一致。
    out["sensitivity_excl_outage_days"] = sensitivity_block(out["day_health"], rows, _decide_on,
                                                            revealed=bool(out.get("verdicts")))
    return out


#: 描述项 buzz_v1 在结果里的键名（与 `dim_ic_protocol.H1_V1_DESCRIPTIVE[0]` 同一个，测试钉住）
P_V1_NAME = "buzz_v1"
#: 描述项 weighted_composite（原 H2）在结果里的键名（与 `dim_ic_protocol.H2_DESCRIPTIVE[0]` 同一个，测试钉住）
P_WC_NAME = "weighted_composite"


def status_line(res: Dict) -> str:
    """给 `ic_rerun_readiness --quiet` 的一段。图标约定同 F&G 那段：⚠️ 要显著标出 / ⏳ 正常 / 🔔 已到检视点。"""
    tag = "维度 IC 协议（v0.45.320 预注册·修订 5：H1 = buzz_v1_np，通道由观测层重算）"
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
    # 修订 5（§13.14）：原 H2 降为描述项 weighted_composite ⇒ 它的截断 / 权重偏离只是说明，不影响 H1
    wct = tr.get(P_WC_NAME) or {}
    if wct.get("date"):
        notes.append(f"描述项 weighted_composite 已于 {wct['date']}（{wct['version']}）截断（不影响 H1）")
    wc = res.get("weight_change") or {}
    if wc.get("date"):
        notes.append(f"config 权重已偏离冻结值（{wc['date']}，{wc.get('source')}）⇒ 描述项 weighted_composite 截断（不影响 H1）")
    if "unknown" in wc:
        notes.append(f"权重历史无法判定（{wc['unknown']}；只影响描述项 weighted_composite）")
    ex = res.get("h1_excluded")
    if not isinstance(ex, dict):
        # 读不到排除计数 ⇒ 观测不全 / 冻结层报错的行会无声消失；与 `_dim_ic_attention` 的 h1_excluded_unknown 同判
        warns.append(f"读不到 H1 排除计数（h1_excluded={ex!r}）")
        ex = {}
    if ex.get("n_frozen_errors"):
        warns.append(f"H1 冻结通道层报错 {ex['n_frozen_errors']} 行（已排除、不补值）——跑 `buzz_v1.py --bridge` 查，"
                     "这是要修的 bug，不是数据缺失")
    if ex.get("n"):
        notes.append(f"H1 排除 {ex['n']} 行（观测不全 / 无溯源块，按 §13.14 不补值）")
    if res.get("stale"):
        warns.append(f"登记窗口起点 {res.get('forward_start')} 后仍无任何样本——扫描停了或路径错了")
    guard = res.get("input_code_guard")
    if guard is not None and not guard.get("ok"):
        warns.append(f"{_guard_problem(guard)}——跑 signal_code_fingerprints.py 按提示登记；"
                     "未登记前 H1 截断与否无法判定，到检视点将不出结论")
    d_only = (guard or {}).get("descriptive_only") or {}
    if d_only.get("unacknowledged") or d_only.get("errors"):
        notes.append(f"描述项 buzz_v1：{_guard_problem(d_only)}（不影响 H1）")
    sp = res.get("buzz_v1_self_proof")
    if sp is not None and not sp.get("ok"):
        warns.append(f"{_self_proof_problem(sp)}——跑 buzz_v1.py 看明细；只影响描述项 buzz_v1，不影响 H1")
    v1t = tr.get(P_V1_NAME) or {}
    if v1t.get("date"):
        notes.append(f"描述项 buzz_v1 已于 {v1t['date']}（{v1t['version']}）截断（不影响 H1）")
    br = res.get("buzz_v1_bridge")
    if br is not None:
        if br.get("ok"):
            notes.append(_bridge_text(br))
        else:
            warns.append(f"{_bridge_text(br)}——跑 `buzz_v1.py --bridge` 看失配明细（修订 5 起只是监测，不影响 H1）")
    tail = ("；⚠️ " + "；".join(warns)) if warns else ""
    tail += "".join(f"；{n}" for n in notes)
    if st == "concluded":
        v = res.get("verdicts", {})
        parts = [f"{h}={r['result']}" for h, r in v.items()]
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
    print("🐝 Alpha Hive · 维度 IC 证据协议（v0.45.320 预注册 · v0.45.330 修订 1 · v0.45.708 修订 4 · v0.45.712 修订 5，执行器 v0.45.325/330/708/712）")
    print("━" * 72)
    print(status_line(res))
    if res.get("status") == "cannot_judge":
        return
    if res.get("day_health"):
        print("  " + day_health.describe(res["day_health"]))
    sb = res.get("sensitivity_excl_outage_days")
    for line in day_health.describe_sensitivity(sb):
        print("  " + line)
    for h, v in ((sb or {}).get("verdicts") or {}).items():
        ic = f"  IC {v['ic']:+.4f}  p={v['p']:.4f}" if "ic" in v else ""
        print(f"    【{h}】{v['result']}（{v.get('look')}检视）{ic}")
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
    ex = res.get("h1_excluded") or {}
    if ex.get("n"):
        print(f"\n  H1 排除 {ex['n']} 行（其中冻结层报错 {ex.get('n_frozen_errors', 0)}）；按日：{ex.get('by_day')}")
        for e in ex.get("examples") or []:
            print(f"    {e['date']} {e['ticker']}：{e['why']}")
    extra = res.get("descriptive_extra") or {}
    if extra:
        print("\n  描述项 buzz_v1 / weighted_composite（修订 4 / 5 前的 H1 / H2 对象；不进 Holm 族、不触发动作）")
        for name, t in extra.items():
            if t["testable"]:
                print(f"    {name:<18} IC {t['ic']:+.4f}  G={t['G']}  p={t['p']:.4f}（朴素，未校正）")
            else:
                print(f"    {name:<18} {t['reason']}")


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
