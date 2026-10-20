#!/usr/bin/env python3
"""
🐝 Alpha Hive — 冻结版 Buzz 评分 buzz_v1（v0.45.556，维度 IC 协议修订 1 的 H1 对象 · 阶段 2 第 2 块）
==================================================================================================
定义（`experiments/dim_ic_preregistration.md` §13.2）：锚点 v0.45.340（边界 2026-09-28）生产 BuzzBee 里
「从 7 个通道值到 score」的全部计算——通道加权合成、clamp、情绪动量调整、情绪-价格背离调整——作用在**归档输入**上。
**定义锚在代码，不锚在实现**：本文件只是它的一个实现，没有自由度；能不能用于检验只看自证（下面 `self_proof`）。

冻结的意思
----------
本文件**不 import 任何生产评分代码，也不读 config**（`tests/test_buzz_v1.py` 的 AST 守卫钉住）：常数与公式逐字抄自
锚点 commit 4f36d527 的 `swarm_agents/buzz_bee.py`（合成、两次 clamp）、`swarm_agents/sentiment.py`
（`_get_sentiment_momentum` / `_detect_sentiment_price_divergence`）、`swarm_agents/utils.clamp_score_cfg` 与
`config.AGENT_SCORING` / `SENTIMENT_MOMENTUM_CONFIG`。生产以后改合成权重 / 调整阈值，这里**不跟**——那正是冻结的意义。
v0.45.556 实测：锚点 → 2026-10-19 HEAD，上述代码与常数逐位未变（config 只删了 Buzz 不用的 `sector_deviation_*`）。

⚠️ 浮点运算顺序照抄生产：合成按书写顺序从左往右加；调整按「clamp → +动量 → +背离 → clamp」；最后 `round(·, 2)`
（生产 `AgentResult(score=round(score, 2))`）。换加法顺序**可能**在第二位小数上翻位——v0.45.556 实测把加法倒过来，
09-28 ~ 10-16 的 450 行仍全等，即本窗口没出现；照抄顺序是为了不留这个可能，不是因为观察到了。
（clamp 的先后则是实打实的定义：去掉内层 clamp，`test_inner_clamp_happens_before_adjustments` 会红。）

输入（全部取自 `pheromone.db` 的 `signal_archive` 表）
-----------------------------------------------------
- 7 个通道 `buzz.comp.*`（v0.45.340 起全精度入档）。
- 背离调整用的**原始** 5 日动量：`price.momentum_5d`（取自 Scout 的 details——与 Buzz 用的是同一份共享行情）。
  v0.45.562 更正：556 写的「450/450 逐位相等」不对——那是拿蜂群结果文件比的，10-05 同日重扫后文件与库不是同一轮。按库里的
  `buzz.comp.momentum_signal` 反推，09-28~10-19 的 480 行里 477 行逐位相等，10-05 的 VKTX / COST / SNOW 三行差约 1e-14
  （Buzz 自己那份动量与 Scout 的在末位不同）。只在恰好落在背离阈值上时才可能改变结果；背离调整在生产里从未触发，
  自证覆盖不到这一项。不在库里 = 当天动量不可得（生产里 `_mom_raw is None` ⇒ 背离「unavailable」、调整 0）——
  库里分不出「Scout 没取到」与「没入档」，所以缺它不计入自证 ②。
- 动量调整的 3 日参照：参照**日期**的选法照锚点代码——该票「日期 ≤ 当日−3 天」的最近一个有 `sentiment.pct` 的归档日，
  且不早于 当日−3−4 天；参照**值**：参照日 ≥ 锚点日 ⇒ 用当天归档通道 × 本文件的冻结权重重算 `int(合成值)`
  （**不直接读 `sentiment.pct`**：否则生产以后改合成权重，会从「历史参照值」这条侧门漏进来）；参照日 < 锚点日 ⇒
  那几天通道只有 0.1 精度、重算不出生产当时的值 ⇒ 读归档的 `sentiment.pct`（那是生产当时的事实，以后也不会变）。
- **不按 `QUARANTINE` 跳过任何输入**：生产当天的分就是用库里那个值算的（哪怕它取错了窗口）；跳过 = 自证 ② 平白多一条缺输入。
  被隔离的观测由执行器按 §3 在 H1 层面剔除，与这里无关。

自证（§13.2；实现可以用于检验的前提，两项都要）
-----------------------------------------------
从锚点日起、到第一条**只点名冻结层**的边界之前的每个扫描日：
  ① 重算值与生产 `predictions.dimension_scores.sentiment` **逐位相等**（记录精度 = 2 位小数，不许放宽容差）；
  ② 生产有分、重算却缺输入的行数为 **0**。
截止条件收窄为「只点名冻结层」（v0.45.556 实现选择，已写进协议 §13.11）：同时点名输入层的边界（如 v0.45.445 新闻换源）
改的是输入，冻结层代码不变、自证照样应当成立；若它其实也夹带了冻结层改动，自证会直接失配变红——宽口径不会漏，窄口径不会误停。

用法
----
    /usr/local/bin/python3 buzz_v1.py                 # 自证（生产库只读）
    /usr/local/bin/python3 buzz_v1.py --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

ANCHOR_VERSION = "v0.45.340"
ANCHOR_COMMIT = "4f36d527"
ANCHOR_DATE = "2026-09-28"           #: 锚点边界日 = 生产首次跑锚点代码的扫描日（`_COHORT_HISTORY`）

# ── 冻结常数（逐字抄自锚点；不读 config）────────────────────────────────────
CHANNELS: Tuple[str, ...] = ("momentum_signal", "volume_signal", "volatility_signal", "reddit_signal",
                             "news_signal", "yahoo_signal", "fear_greed_signal")
W_MOMENTUM, W_VOLUME, W_VOLATILITY, W_REDDIT, W_NEWS, W_YAHOO, W_FEAR_GREED = 0.2, 0.1, 0.05, 0.25, 0.25, 0.05, 0.1
SCORE_MIN, SCORE_MAX = 1.0, 10.0
SURGE, RISE, CRASH, DECLINE = 15, 5, -15, -5                 # 3 日情绪变化阈值
ADJ_SURGE, ADJ_RISE, ADJ_CRASH, ADJ_DECLINE = 0.5, 0.2, -0.5, -0.2
BULL_TRAP_SENTIMENT, HIDDEN_OPP_SENTIMENT, PRICE_THRESHOLD = 65, 35, 3.0
REF_LAG_DAYS, REF_SLACK_DAYS = 3, 4

_TABLE = "signal_archive"
_PCT = "sentiment.pct"
_MOM = "price.momentum_5d"
_COMP = tuple(f"buzz.comp.{c}" for c in CHANNELS)
#: 冻结层信号（与协议 `H1_FROZEN_LAYER_SIGNALS` 同一份，测试钉住）
FROZEN_LAYER_SIGNALS = ("agent.BuzzBeeWhisper.score", "sentiment.pct")


# ────────────────────────────────────────────────────────────────────────────
# 纯函数：通道 → 分
# ────────────────────────────────────────────────────────────────────────────

def clamp(x: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, x))


def composite(ch: Mapping[str, float]) -> float:
    """7 通道加权合成。**书写顺序 = 浮点运算顺序**，与锚点逐字相同。"""
    return (ch["momentum_signal"] * W_MOMENTUM +
            ch["volume_signal"] * W_VOLUME +
            ch["volatility_signal"] * W_VOLATILITY +
            ch["reddit_signal"] * W_REDDIT +
            ch["news_signal"] * W_NEWS +
            ch["yahoo_signal"] * W_YAHOO +
            ch["fear_greed_signal"] * W_FEAR_GREED)


def momentum_adj(delta_3d: Optional[int]) -> float:
    if delta_3d is None:
        return 0.0
    if delta_3d > SURGE:
        return ADJ_SURGE
    if delta_3d > RISE:
        return ADJ_RISE
    if delta_3d < CRASH:
        return ADJ_CRASH
    if delta_3d < DECLINE:
        return ADJ_DECLINE
    return 0.0


def divergence_adj(pct: int, momentum_5d: Optional[float]) -> float:
    if momentum_5d is None:
        return 0.0
    if pct > BULL_TRAP_SENTIMENT and momentum_5d < -PRICE_THRESHOLD:
        severity = min(3, int((pct - BULL_TRAP_SENTIMENT) / 10) + int(abs(momentum_5d) / PRICE_THRESHOLD))
        return round(-0.3 * severity, 2)
    if pct < HIDDEN_OPP_SENTIMENT and momentum_5d > PRICE_THRESHOLD:
        severity = min(3, int((HIDDEN_OPP_SENTIMENT - pct) / 10) + int(momentum_5d / PRICE_THRESHOLD))
        return round(0.3 * severity, 2)
    return 0.0


def score(ch: Mapping[str, float], momentum_5d: Optional[float], ref_pct: Optional[int]) -> Dict:
    """一行的 buzz_v1。`ref_pct` = 3 日参照日的情绪百分比（None = 没有够近的参照）。"""
    comp = composite(ch)
    pct = int(comp)
    s = clamp(comp / 10.0)
    d3 = None if ref_pct is None else pct - int(round(ref_pct))
    m_adj = momentum_adj(d3)
    s += m_adj
    d_adj = divergence_adj(pct, momentum_5d)
    s += d_adj
    s = clamp(s)
    return {"score": round(s, 2), "pct": pct, "delta_3d": d3, "momentum_adj": m_adj, "divergence_adj": d_adj}


# ────────────────────────────────────────────────────────────────────────────
# 归档输入
# ────────────────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    from hive_logger import PATHS        # 只为解析数据路径；不是评分代码
    return Path(PATHS.db)


def _shift(d: str, days: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=days)).isoformat()


class ArchiveInputs:
    """一段日期内的归档输入：{(date, ticker): {signal: value}}，含参照所需的回看天数。"""

    def __init__(self, con: sqlite3.Connection, start: str, end: str):
        lo = _shift(start, -(REF_LAG_DAYS + REF_SLACK_DAYS))
        names = (_PCT, _MOM) + _COMP
        q = (f"SELECT date, ticker, signal, value FROM {_TABLE} WHERE date >= ? AND date <= ? "
             f"AND signal IN ({','.join('?' * len(names))})")
        self.rows: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(dict)
        self.pct_dates: Dict[str, List[str]] = defaultdict(list)
        for d, t, s, v in con.execute(q, (lo, end) + names):
            if v is None:
                continue
            self.rows[(d, t)][s] = v
            if s == _PCT:
                self.pct_dates[t].append(d)
        for t in self.pct_dates:
            self.pct_dates[t].sort()

    def channels(self, d: str, t: str) -> Optional[Dict[str, float]]:
        r = self.rows.get((d, t), {})
        if not all(k in r for k in _COMP):
            return None
        return {c: r[f"buzz.comp.{c}"] for c in CHANNELS}

    def momentum_5d(self, d: str, t: str) -> Optional[float]:
        return self.rows.get((d, t), {}).get(_MOM)

    def ref(self, d: str, t: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """(参照日, 参照 pct, 缺输入原因)。没有够近的参照 ⇒ (None, None, None)——那是正常的「无历史」。"""
        cutoff, oldest = _shift(d, -REF_LAG_DAYS), _shift(d, -(REF_LAG_DAYS + REF_SLACK_DAYS))
        cands = [x for x in self.pct_dates.get(t, ()) if x <= cutoff]
        if not cands or cands[-1] < oldest:
            return None, None, None
        r = cands[-1]
        if r < ANCHOR_DATE:
            return r, int(round(self.rows[(r, t)][_PCT])), None
        ch = self.channels(r, t)
        if ch is None:
            return r, None, f"参照日 {r} 的通道不齐（锚点后的参照要由通道重算）"
        return r, int(composite(ch)), None


def recompute(con: sqlite3.Connection, keys: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict]:
    """对 (date, ticker) 逐行重算。缺输入的行返回 {"missing": 原因}。"""
    keys = sorted(set(keys))
    if not keys:
        return {}
    inp = ArchiveInputs(con, keys[0][0], keys[-1][0])
    out: Dict[Tuple[str, str], Dict] = {}
    for d, t in keys:
        ch = inp.channels(d, t)
        if ch is None:
            out[(d, t)] = {"missing": "当天 7 个通道不齐"}
            continue
        r, ref_pct, why = inp.ref(d, t)
        if why:
            out[(d, t)] = {"missing": why}
            continue
        out[(d, t)] = {**score(ch, inp.momentum_5d(d, t), ref_pct), "ref_date": r}
    return out


# ────────────────────────────────────────────────────────────────────────────
# 自证
# ────────────────────────────────────────────────────────────────────────────

def frozen_only_boundary(history: Optional[list] = None, input_signals: Optional[Tuple[str, ...]] = None) -> Optional[Dict]:
    """锚点日之后第一条**只**点名冻结层（不触及任何输入层信号）的边界——自证窗口到它为止（不含）。

    边界表经 `signal_archive._cohort_history()` 读（v0.45.562）：与执行器的截断判定同一个入口——此前直读
    `ic_rerun_readiness._COHORT_HISTORY`，生产里是同一张表，但替换那个入口的测试 / 视图（如 TestRealCli 的 prelude）
    会让自证与截断看到两张不同的表。"""
    import signal_archive
    if history is None:
        history = signal_archive._cohort_history()
    if input_signals is None:
        input_signals = _protocol().H1_INPUT_SIGNALS
    hits = []
    for entry in history:
        if entry[0] <= ANCHOR_DATE:
            continue
        frozen = signal_archive.generation_boundaries(FROZEN_LAYER_SIGNALS, [entry])
        inputs = signal_archive.generation_boundaries(input_signals, [entry])
        if frozen and not inputs:
            hits.append({"date": entry[0], "version": entry[1]})
    return min(hits, key=lambda h: h["date"]) if hits else None


def _protocol():
    import importlib.util
    path = Path(__file__).resolve().parent / "experiments" / "dim_ic_protocol.py"   # 代码锚点：登记常量
    spec = importlib.util.spec_from_file_location("dim_ic_protocol", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def self_proof(db_path: Optional[str] = None, end: Optional[str] = None,
               history: Optional[list] = None, max_examples: int = 10) -> Dict:
    """§13.2 自证。返回 {"ok", "window", "n", "n_matched", "n_mismatched", "n_missing", "mismatched", "missing",
    "stopped_by"}；`ok` = 有样本 且 ① 全等 且 ② 缺输入 0。读库失败 ⇒ ok=False 并带 `error`（不当作通过）。"""
    db = Path(db_path) if db_path else _db_path()
    stop = frozen_only_boundary(history)
    hi = end or dt.date.today().isoformat()
    if stop:
        hi = min(hi, _shift(stop["date"], -1))
    base = {"window": [ANCHOR_DATE, hi], "stopped_by": stop}
    forward_start = _protocol().FORWARD_START
    if stop and stop["date"] <= forward_start:
        # v0.45.562：窗口起点之前（含当天）的边界不截断 H1（`truncation_point` 只认窗口内的），而它之后生产分已不是
        # buzz_v1 ⇒ H1 整个窗口读的都是别的东西。自证窗口止于它之前，一天 H1 样本都担保不了——不许算通过。
        return {**base, "ok": False, "n": 0,
                "error": (f"只点名冻结层的边界 {stop['version']}（{stop['date']}）不晚于 H1 窗口起点 {forward_start}："
                          "窗口前的边界不截断 H1，H1 读到的生产分自窗口起就不是 buzz_v1，自证担保不了任何一天")}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            prod: Dict[Tuple[str, str], float] = {}
            for d, t, ds in con.execute("SELECT date, ticker, dimension_scores FROM predictions "
                                        "WHERE date >= ? AND date <= ? AND dimension_scores IS NOT NULL",
                                        (ANCHOR_DATE, hi + "~")):
                try:
                    v = (json.loads(ds) or {}).get("sentiment")
                except (json.JSONDecodeError, TypeError, AttributeError):
                    v = None
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    prod[(d[:10], t)] = float(v)
            rec = recompute(con, prod)
        finally:
            con.close()
    except sqlite3.Error as e:
        return {**base, "ok": False, "error": f"{type(e).__name__}: {e}", "n": 0}
    mism, miss = [], []
    for k in sorted(prod):
        r = rec[k]
        if "missing" in r:
            miss.append({"date": k[0], "ticker": k[1], "production": prod[k], "why": r["missing"]})
        elif r["score"] != prod[k]:
            mism.append({"date": k[0], "ticker": k[1], "production": prod[k], "buzz_v1": r["score"],
                         "ref_date": r.get("ref_date")})
    n = len(prod)
    return {**base, "ok": n > 0 and not mism and not miss, "n": n, "n_matched": n - len(mism) - len(miss),
            "n_mismatched": len(mism), "n_missing": len(miss),
            "mismatched": mism[:max_examples], "missing": miss[:max_examples]}


def main() -> int:
    ap = argparse.ArgumentParser(description="buzz_v1 自证（生产库只读）")
    ap.add_argument("--db", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = self_proof(args.db, args.end)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        w = res["window"]
        head = "✅ 自证通过" if res["ok"] else "⚠️ 自证未通过"
        print(f"{head}：{w[0]} ~ {w[1]}，{res.get('n', 0)} 行，逐位一致 {res.get('n_matched', 0)}，"
              f"失配 {res.get('n_mismatched', 0)}，缺输入 {res.get('n_missing', 0)}"
              + (f"；窗口止于 {res['stopped_by']['date']}（{res['stopped_by']['version']}，只点名冻结层）" if res.get("stopped_by") else "")
              + (f"；{res['error']}" if res.get("error") else ""))
        for x in res.get("mismatched", []):
            print(f"   失配 {x['date']} {x['ticker']}: 生产 {x['production']} vs buzz_v1 {x['buzz_v1']}（参照 {x['ref_date']}）")
        for x in res.get("missing", []):
            print(f"   缺输入 {x['date']} {x['ticker']}: {x['why']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # 代码锚点：import 仓内模块
    raise SystemExit(main())
