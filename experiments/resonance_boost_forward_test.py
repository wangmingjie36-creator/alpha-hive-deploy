#!/usr/bin/env python3
"""共振加成前瞻检验（预注册，v0.45.242，2026-09-14）

问题
----
`QueenDistiller._compute_weighted_score` 里 `rule_score = adjusted × (1 + confidence_boost/100)`
把「≥3 个维度同向」这个共振判定乘进了分数，而且不分方向（看空共振也把分数往上抬）。
**去掉它**（分数不再乘加成，共振照常检测、照常进报告），`final_score` 对 T+7 超额收益的
横截面排序会不会更好？

为什么是前瞻而不是直接改
------------------------
样本内证据见 `experiments/resonance_boost_insample_report.md`。一句话：按现行权重重算后，
共振加成是评分链里唯一测得出损失排序信息的一步，去掉它 ΔIC 为正、两语料同号；但那是在
「让加成分方向」的四个方案被否掉**之后**才做的事后分析，没过多重比较校正，而且要推翻一条
定档（auto-memory「删 queen 层共振加成：不要删」）。用户决定：**前瞻确认后再删。**

────────────────────────────────────────────────────────────────────
预注册（2026-09-14 写死，早于任何前瞻样本；改动须在 CHANGELOG 标「事后」并写理由）
────────────────────────────────────────────────────────────────────
样本  业务日 >= FORWARD_START（2026-09-15，登记次日）的 `analysis-*-ml-*.json`（`PATHS.home`），
      与 `predictions`（`PATHS.db`）按 (date, ticker) 联表。
      T+7 超额 = (close_t7 − 入场价) / 入场价 × 100 − spy_return_t7；入场价取
      price_at_predict_raw，缺则 price_at_predict。未到期（close_t7 / spy_return_t7 为空）不进 IC。
      不用 return_t7（方向单是钳位收益，见 auto-memory return-t7-clamp）。
变体  B0 = 生产现状（重放）；B3 = 同一条链，唯一差别是加权分不乘共振加成。
      链路全部调真实代码：共振检测 → 三重惩罚 → 方向投票 → GEX 政体调整 → F&G 调整。
自证  （前提，不满足即「无法判定」exit 3）B0 重放的 (final_score, direction) 与记录值一致
      （分数容差 0.011）的行占比 >= 95%；不一致的行两个变体一并剔除并计数。
      ⇒ 生产若改了评分链（包括有人**提前删了加成**），这里会红，而不是静默算出一个没意义的数。
统计量 每个扫描日（该日已到期 >= MIN_CS=5 只）算 Spearman IC(B0) 与 IC(B3)，
      ΔIC = IC(B3) − IC(B0)；同一 ISO 周的 ΔIC 取均值 → 对周序列做单侧 t 检验（H1：均值 > 0）。
      不池化（auto-memory cross-sectional-pooling）。
检视  成组序贯两次，按**时间顺序最先攒到的**前 N 个合格周计算（与哪天运行本脚本无关）：
        中期  N=10  单侧 p < 0.01   → 确认
        终期  N=20  单侧 p < 0.045  → 确认；否则 → 未确认（结案）
      两次合计单侧 α ≈ 0.05。N 的依据：样本内现行规则重演，周 ΔIC 标准差 0.050，
      检出 0.03 约需 17 周（80% 功效）。
盲化  未到检视点时只报进度（合格周数、自证率），**不输出任何效应量**；中期未过线同样
      只报「未过中期界，继续」。想提前看数必须改代码 —— 那会留在 git 里。
结论  确认 → 删分数加成（照 v0.45.235 流程：失败测试先行、世代边界、等价重放）。
      未确认 → 保留加成，登记结案，勿再拿样本内证据提议删除。
附带  （不参与判定）按行数加权 ΔIC、纸面组合入场资格变化行数（闸门读
      `paper_portfolio.CONFIG`）、检验期内 `ic_rerun_readiness._COHORT_HISTORY` 新增的世代边界条数。

样本内复核：`--insample` 在 FORWARD_START 之前的样本上，用**现行权重**重算加权分后跑同一条链
（加权分自证：用当时记录的权重应能复现当时记录的基础分）。它就是生成假设的那份数据，
**只供复核报告数字，不能拿来确认。**

用法
----
    /usr/local/bin/python3 experiments/resonance_boost_forward_test.py            # 前瞻（默认）
    /usr/local/bin/python3 experiments/resonance_boost_forward_test.py --json
    /usr/local/bin/python3 experiments/resonance_boost_forward_test.py --insample

承载物：`ic_rerun_readiness.py` 每周被只读诊断任务调用，其摘要行会带上本检验的进度
（`status_line`），攒够周数时显著提示来跑本脚本。

退出码：0 = 已到检视点并给出结论 / 1 = 未就绪（正常）/ 3 = 无法判定
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import logging
import math
import re
import sqlite3
import sys
import threading
import types
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 代码锚点：import 仓内模块

# ── 预注册常量（改动 = 事后，须在 CHANGELOG 说明；测试钉住）──────────────────
FORWARD_START = "2026-09-15"
MIN_CS = 5
LOOKS: Tuple[Tuple[int, float, str], ...] = ((10, 0.01, "中期"), (20, 0.045, "终期"))
SELFPROOF_MIN_RATE = 0.95
SCORE_TOL = 0.011

_FNAME = re.compile(r"^analysis-([A-Z0-9.\-]+)-ml-(\d{4}-\d{2}-\d{2})\.json$")
_REQUIRED = ("agent_details", "data_quality", "base_score_before_resonance", "gex_regime_mod",
             "final_score", "direction")


# ── 取数 ──────────────────────────────────────────────────────────────────────
def load_reports(home: Path, since: Optional[str] = None, before: Optional[str] = None) -> Dict:
    """读 analysis JSON。返回 {"rows": [...], "skipped": Counter}。

    缺字段的文件**计数**而不是静默跳过 —— 字段改名会让样本悄悄归零，计数让它看得见。
    """
    rows, skipped = [], collections.Counter()
    for f in sorted(Path(home).glob("analysis-*-ml-*.json")):
        m = _FNAME.match(f.name)
        if not m:
            skipped["文件名不合规（如 _backfilled）"] += 1
            continue
        date = m.group(2)
        if (since and date < since) or (before and date >= before):
            continue
        try:
            sr = json.loads(f.read_text(encoding="utf-8")).get("swarm_results") or {}
        except (OSError, ValueError, AttributeError):
            skipped["JSON 不可读"] += 1
            continue
        missing = [k for k in _REQUIRED if k not in sr]
        if missing or not isinstance(sr.get("agent_details"), dict) or not sr["agent_details"] \
                or not isinstance(sr.get("data_quality"), dict):
            skipped["缺字段:" + ",".join(missing or ["agent_details/data_quality 为空"])] += 1
            continue
        rows.append({"date": date, "ticker": sr.get("ticker") or m.group(1), "sr": sr})
    return {"rows": rows, "skipped": skipped}


def load_excess_returns(db_path: str) -> Dict[Tuple[str, str], float]:
    """(date, ticker) → T+7 超额收益（%）。未到期的不出现在结果里。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        q = ("SELECT date, ticker, close_t7, price_at_predict_raw, price_at_predict, spy_return_t7 "
             "FROM predictions")
        out = {}
        for d, t, close7, p_raw, p, spy in con.execute(q):
            entry = p_raw or p
            if close7 is None or spy is None or not entry or entry <= 0:
                continue
            out[(str(d)[:10], t)] = (close7 - entry) / entry * 100.0 - spy
        return out
    finally:
        con.close()


# ── 重放（全部调真实代码）───────────────────────────────────────────────────
def _agent_results(sr: Dict) -> List[Dict]:
    dq = sr.get("data_quality") or {}
    return [{"source": a, "direction": v.get("direction", "neutral"), "confidence": v.get("confidence", 0.5),
             "score": v.get("score"), "dimension": v.get("dimension"), "details": v.get("details") or {},
             "data_quality": dq.get(a, {}), "discovery": v.get("discovery", "")}
            for a, v in (sr.get("agent_details") or {}).items() if isinstance(v, dict)]


def _gex_input(sr: Dict) -> Dict:
    """由记录的 `gex_regime_mod` 反建 GEX 输入（flip 距离按 100 的假股价还原，只需百分比）。"""
    m = sr.get("gex_regime_mod") or {}
    if not m or m.get("gex_regime") in (None, "unknown"):
        return {}
    d = {"regime": m["gex_regime"], "stock_price": 100.0,
         "vanna_stress": {"can_flip_gex": bool(m.get("can_flip_vanna"))}}
    if m.get("flip_proximity_pct") is not None:
        d["gex_flip"] = 100.0 + m["flip_proximity_pct"]
    return d


def _fg_adjustment(fg_value, direction: str) -> float:
    """与 `QueenDistiller.distill` 步骤 4.6 同口径（那段是内联代码，无方法可调；分歧由自证兜住）。"""
    if fg_value is None:
        return 0.0
    import config
    c = getattr(config, "FEAR_GREED_SCORING", {})
    if fg_value < c.get("extreme_fear", 25):
        return (c.get("fear_bearish_boost", 0.3) if direction == "bearish"
                else -c.get("fear_bullish_penalty", 0.4) if direction == "bullish" else 0.0)
    if fg_value > c.get("extreme_greed", 75):
        return (-c.get("greed_bullish_penalty", 0.3) if direction == "bullish"
                else c.get("greed_bearish_boost", 0.2) if direction == "bearish" else 0.0)
    return 0.0


def _resonance(ticker: str, results: List[Dict]) -> Dict:
    from pheromone_board import PheromoneBoard
    board = PheromoneBoard.__new__(PheromoneBoard)
    board._lock = threading.RLock()
    entries = [types.SimpleNamespace(agent_id=r["source"], direction=r["direction"]) for r in results]
    board._live_agent_entries = lambda _t, _e=entries: _e
    return board.detect_resonance(ticker)


def replay(sr: Dict, drop_boost: bool, base: Optional[float] = None) -> Dict:
    """共振 → 三重惩罚 → 投票 → GEX → F&G。`drop_boost=True` 即 B3。抛异常由调用方计数。"""
    from gex_regime import GexRegimeModifier
    from swarm_agents.queen_distiller import QueenDistiller

    ticker = sr.get("ticker", "")
    results = _agent_results(sr)
    base = sr["base_score_before_resonance"] if base is None else base
    z = _resonance(ticker, results)
    boost = z["confidence_boost"] if z["resonance_detected"] else 0
    pre = base if drop_boost else base * (1 + boost / 100.0)
    pre = round(max(0.0, min(10.0, pre)), 2)

    q = QueenDistiller.__new__(QueenDistiller)  # 不走 __init__：它会 reload(config)
    q.ml_feedback_enabled = bool(sr.get("ml_feedback_enabled"))
    q.ml_adjustments = sr.get("ml_weight_adjustments") or {}
    q.board = types.SimpleNamespace(_entries=[])
    tp = q._apply_triple_penalty(ticker, pre, results)
    vote = q._compute_direction_vote(ticker, results, results, tp["rule_score"],
                                     fg_value=sr.get("fear_greed_value"))
    score, direction = vote["rule_score"], vote["rule_direction"]
    g = GexRegimeModifier().compute(_gex_input(sr), direction=direction)["gex_adjustment"]
    if abs(g) > 0.01:
        score = round(max(0.0, min(10.0, score + g)), 2)
    fa = _fg_adjustment(sr.get("fear_greed_value"), direction)
    if abs(fa) > 0.01:
        score = round(max(0.0, min(10.0, score + fa)), 2)
    return {"final": score, "direction": direction, "resonance": bool(z["resonance_detected"]),
            "resonance_direction": z["direction"], "boost": boost}


def rebase_with_current_weights(sr: Dict, use_recorded_weights: bool = False) -> Optional[float]:
    """仅 `--insample`：用现行权重重算加权分（+ 记录的 ML 调整）。

    `use_recorded_weights=True` 用当时记录的 `dimension_weights` —— 那应当复现当时记录的
    `base_score_before_resonance`，是本函数的自证。
    """
    import config
    from gex_regime import RegimeWeightAdjuster
    from swarm_agents.queen_distiller import QueenDistiller

    ds, dc = sr.get("dimension_scores") or {}, sr.get("dimension_confidence") or {}
    if not ds:
        return None
    q = QueenDistiller.__new__(QueenDistiller)
    q.board = types.SimpleNamespace(detect_resonance=lambda _t: {"resonance_detected": False})
    if use_recorded_weights:
        weights = dict(sr.get("dimension_weights") or {})
        q.DIMENSION_WEIGHTS = dict(sr.get("dimension_weights_base") or weights)
        if not weights:
            return None
    else:
        w = dict(QueenDistiller.DEFAULT_WEIGHTS)
        w.update({k: v for k, v in config.EVALUATION_WEIGHTS.items() if k in w})
        mfc = getattr(config, "ML_FEEDBACK_CONFIG", {})
        if sr.get("ml_feedback_enabled") and sr.get("ml_weight_adjustments") \
                and mfc.get("enable_dimension_weighting", True):
            for d, f in sr["ml_weight_adjustments"].items():
                if d in w:
                    w[d] *= f
            tot = sum(w.values())
            if tot > 0:
                w = {k: round(v / tot, 4) for k, v in w.items()}
        q.DIMENSION_WEIGHTS = dict(w)
        oracle = (sr.get("agent_details") or {}).get("OracleBeeEcho") or {}
        weights, _ = RegimeWeightAdjuster().adjust_weights(
            base_weights=dict(w), macro_regime=sr.get("macro_regime") or "neutral",
            gex_regime=(sr.get("gex_regime_mod") or {}).get("gex_regime") or "unknown",
            iv_rank=(oracle.get("details") or {}).get("iv_rank"))
    present = [d for d in q.DIMENSION_WEIGHTS if d in ds]
    cov = round(len(present) / len(q.DIMENSION_WEIGHTS) * 100, 1)
    ws = q._compute_weighted_score(sr.get("ticker", ""), {d: ds[d] for d in present},
                                   {d: dc.get(d, 0.5) for d in present}, cov, len(present), [],
                                   override_weights=weights)
    return ws["base_score"] + (sr.get("ml_adjustment") or 0.0)


# ── 统计 ──────────────────────────────────────────────────────────────────────
def _avg_ranks(v: List[float]) -> List[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    ranks = [0.0] * len(v)
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x: List[float], y: List[float]) -> Optional[float]:
    if len(x) < 2 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    rx, ry = _avg_ranks(x), _avg_ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def one_sided_t(values: List[float]) -> Dict:
    """H1: 均值 > 0。"""
    from scipy import stats
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": mean, "t": None, "p": 1.0}
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    if sd == 0:
        return {"n": n, "mean": mean, "t": None, "p": 0.0 if mean > 0 else 1.0}
    t = mean / (sd / math.sqrt(n))
    return {"n": n, "mean": mean, "t": t, "p": float(stats.t.sf(t, n - 1))}


def weekly_deltas(pairs: Dict[Tuple[str, str], Tuple[float, float]],
                  excess: Dict[Tuple[str, str], float]) -> List[Dict]:
    """pairs: (date, ticker) → (B0 分, B3 分)。返回按时间排序的合格周列表。"""
    by_day = collections.defaultdict(list)
    for key, (s0, s3) in pairs.items():
        if key in excess:
            by_day[key[0]].append((s0, s3, excess[key]))
    by_week = collections.OrderedDict()
    for day in sorted(by_day):
        xs = by_day[day]
        if len(xs) < MIN_CS:
            continue
        ic0 = spearman([a for a, _, _ in xs], [r for _, _, r in xs])
        ic3 = spearman([b for _, b, _ in xs], [r for _, _, r in xs])
        if ic0 is None or ic3 is None:
            continue
        wk = dt.date.fromisoformat(day).isocalendar()[:2]
        by_week.setdefault(wk, []).append({"date": day, "n": len(xs), "ic_b0": ic0, "ic_b3": ic3})
    return [{"week": f"{w[0]}-W{w[1]:02d}", "days": days,
             "delta": sum(d["ic_b3"] - d["ic_b0"] for d in days) / len(days)}
            for w, days in by_week.items()]


def decide(weeks: List[Dict], looks=LOOKS) -> Dict:
    """成组序贯判定。**未到结论时返回值里不含任何效应量**（盲化在数据结构上，不在打印上）。"""
    k = len(weeks)
    for n_req, alpha, label in looks:
        if k < n_req:
            break
        st = one_sided_t([w["delta"] for w in weeks[:n_req]])
        if st["p"] < alpha:
            return {"status": "confirmed", "look": label, "alpha": alpha, "stats": st,
                    "weeks_used": [w["week"] for w in weeks[:n_req]]}
        if (n_req, alpha, label) == looks[-1]:
            return {"status": "not_confirmed", "look": label, "alpha": alpha, "stats": st,
                    "weeks_used": [w["week"] for w in weeks[:n_req]]}
    passed = [lab for n_req, _, lab in looks if k >= n_req]
    nxt = next(n_req for n_req, _, _ in looks if k < n_req)
    return {"status": "not_ready", "weeks": k, "next_look_at": nxt,
            "looks_passed_without_verdict": passed}


# ── 编排 ──────────────────────────────────────────────────────────────────────
def evaluate(rows: List[Dict], excess: Dict, *, insample: bool = False,
             replay_fn: Callable = replay, rebase_fn: Callable = rebase_with_current_weights) -> Dict:
    # 重放每行要走两遍 Queen 链路，它的 INFO 日志（仲裁翻转、冲突折扣…）会按行灌进
    # 生产日志；WARNING 及以上照常放行。
    _prev_disable = logging.root.manager.disable
    logging.disable(logging.INFO)
    try:
        return _evaluate(rows, excess, insample=insample, replay_fn=replay_fn, rebase_fn=rebase_fn)
    finally:
        logging.disable(_prev_disable)


def _evaluate(rows, excess, *, insample, replay_fn, rebase_fn) -> Dict:
    selfproof = collections.Counter()
    fail_examples: List[str] = []
    pairs: Dict[Tuple[str, str], Tuple[float, float]] = {}
    gate_rows: Dict[Tuple[str, str], Tuple[Dict, Dict]] = {}
    for row in rows:
        sr, key = row["sr"], (row["date"], row["ticker"])
        selfproof["total"] += 1
        try:
            base = None
            if insample:
                rec = rebase_fn(sr, use_recorded_weights=True)
                if rec is not None and abs(round(rec, 2) - sr["base_score_before_resonance"]) <= SCORE_TOL:
                    selfproof["base_reproduced"] += 1
                base = rebase_fn(sr)
                if base is None:
                    selfproof["no_dimension_scores"] += 1
                    continue
            b0 = replay_fn(sr, drop_boost=False, base=base)
            b3 = replay_fn(sr, drop_boost=True, base=base)
        except Exception as e:  # 计数，不吞：自证率会因此下降并在输出里看得见
            selfproof["exception"] += 1
            if len(fail_examples) < 5:
                fail_examples.append(f"{key}: {type(e).__name__}: {e}")
            continue
        if not insample:
            if abs(b0["final"] - float(sr["final_score"])) > SCORE_TOL or b0["direction"] != sr.get("direction"):
                selfproof["mismatch"] += 1
                if len(fail_examples) < 5:
                    fail_examples.append(f"{key}: 重放 {b0['final']}/{b0['direction']} vs 记录 "
                                         f"{sr['final_score']}/{sr.get('direction')}")
                continue
        selfproof["reproduced"] += 1
        pairs[key] = (b0["final"], b3["final"])
        gate_rows[key] = (b0, b3)

    total = selfproof["total"]
    rate = selfproof["reproduced"] / total if total else None
    out = {"mode": "insample" if insample else "forward", "n_reports": total,
           "selfproof": dict(selfproof), "selfproof_rate": rate, "fail_examples": fail_examples}
    if insample:
        base_rate = selfproof["base_reproduced"] / total if total else None
        out["base_selfproof_rate"] = base_rate
        if base_rate is None or base_rate < SELFPROOF_MIN_RATE:
            return {**out, "status": "cannot_judge",
                    "reason": f"加权分自证率 {base_rate} < {SELFPROOF_MIN_RATE}：现行权重重算不可信"}
    elif total == 0:
        return {**out, "status": "not_ready", "weeks": 0, "next_look_at": LOOKS[0][0],
                "looks_passed_without_verdict": [], "reason": f"尚无 {FORWARD_START} 起的前瞻样本"}
    elif rate < SELFPROOF_MIN_RATE:
        return {**out, "status": "cannot_judge",
                "reason": (f"B0 重放复现生产记录仅 {selfproof['reproduced']}/{total}（< {SELFPROOF_MIN_RATE:.0%}）"
                           "——评分链已被改动（含共振加成是否已被删改），本检验的前提不成立")}

    weeks = weekly_deltas(pairs, excess)
    out["n_matured_pairs"] = sum(1 for k in pairs if k in excess)
    if insample:  # 样本内不盲化、不走检视：它是生成假设的数据
        used = [d for w in weeks for d in w["days"]]
        return {**out, "status": "insample", "weeks": len(weeks),
                "stats_all_weeks": one_sided_t([w["delta"] for w in weeks]) if weeks else None,
                "pooled_delta_by_rows": (sum((d["ic_b3"] - d["ic_b0"]) * d["n"] for d in used)
                                         / sum(d["n"] for d in used)) if used else None,
                "mean_ic_b0": _mean([d["ic_b0"] for d in used]), "mean_ic_b3": _mean([d["ic_b3"] for d in used]),
                "gate_changes": _gate_changes(gate_rows)}
    verdict = decide(weeks)
    out.update(verdict)
    if verdict["status"] in ("confirmed", "not_confirmed"):
        used_weeks = set(verdict["weeks_used"])
        used = [d for w in weeks if w["week"] in used_weeks for d in w["days"]]
        out["pooled_delta_by_rows"] = sum((d["ic_b3"] - d["ic_b0"]) * d["n"] for d in used) / sum(d["n"] for d in used)
        out["mean_ic_b0"] = _mean([d["ic_b0"] for d in used])
        out["mean_ic_b3"] = _mean([d["ic_b3"] for d in used])
        out["gate_changes"] = _gate_changes(gate_rows)
        out["cohort_boundaries_during_test"] = _cohort_boundaries_since(FORWARD_START)
    return out


def _mean(v):
    return sum(v) / len(v) if v else None


def _gate_changes(gate_rows) -> Dict:
    try:
        from paper_portfolio import CONFIG
        bull, bear = float(CONFIG["entry_score_bull"]), float(CONFIG["entry_score_bear"])
    except Exception as e:
        return {"unavailable": f"{type(e).__name__}: {e}"}

    def gate(v):
        return (v["direction"] == "bullish" and v["final"] >= bull) or (v["direction"] == "bearish" and v["final"] <= bear)

    c = collections.Counter()
    for b0, b3 in gate_rows.values():
        if gate(b0) != gate(b3):
            c[("失去" if gate(b0) else "新获") + "·" + (b0 if gate(b0) else b3)["direction"]] += 1
    return dict(c)


def _cohort_boundaries_since(date: str):
    try:
        import ic_rerun_readiness as rr
        return [f"{d} {v}" for d, v, _ in rr._COHORT_HISTORY if d >= date]
    except Exception as e:
        return [f"不可用：{type(e).__name__}: {e}"]


STALE_DAYS = 21  # 登记后这么多天仍无前瞻样本 ⇒ 不是「还在攒」，是扫描停了或路径错了


def run(home: Optional[Path] = None, db_path: Optional[str] = None, insample: bool = False,
        today: Optional[str] = None) -> Dict:
    from hive_logger import PATHS
    home = Path(home) if home is not None else PATHS.home
    db_path = db_path if db_path is not None else PATHS.db
    # 「永远说还没样本」的检验和没有检验是一回事（同 ic_rerun_readiness.cohort_boundary_evidence
    # 的教训）：目录里连一份历史报告都没有，只能是路径错了，不能报「0 周，继续攒」。
    if not any(Path(home).glob("analysis-*-ml-*.json")):
        return {"status": "cannot_judge", "reason": f"{home} 下没有任何 analysis-*-ml-*.json（路径错了？）"}
    loaded = (load_reports(home, before=FORWARD_START) if insample else load_reports(home, since=FORWARD_START))
    rows = loaded["rows"]
    if not insample and not rows:  # 没有前瞻样本时不碰库：库在测试夹具里可能没有这些列
        res = evaluate([], {})
        days = (dt.date.fromisoformat(today) if today else dt.date.today()) - dt.date.fromisoformat(FORWARD_START)
        if days.days > STALE_DAYS:
            res["stale"] = True
            res["reason"] = f"登记后 {days.days} 天仍无前瞻样本 —— 扫描停了，或报告不在 {home}"
    else:
        if not Path(db_path).exists():
            return {"status": "cannot_judge", "reason": f"找不到数据库 {db_path}", "skipped": dict(loaded["skipped"])}
        res = evaluate(rows, load_excess_returns(db_path), insample=insample)
    res["skipped_files"] = dict(loaded["skipped"])
    return res


def status_line(res: Dict) -> str:
    """一行进度，供 `ic_rerun_readiness` 摘要行引用。不含效应量。"""
    s = res.get("status")
    if s == "cannot_judge":
        return f"⚠️ 共振加成前瞻检验无法判定：{res.get('reason')}"
    if s == "not_ready":
        extra = f"（中期未过界，继续攒到 {res['next_look_at']} 周）" if res.get("looks_passed_without_verdict") else ""
        rate = res.get("selfproof_rate")
        proof = f"，自证 {rate:.0%}" if rate is not None else ""
        why = f"（{res['reason']}）" if res.get("reason") else ""
        icon = "⚠️" if res.get("stale") else "⏳"
        return (f"{icon} 共振加成前瞻检验：{res['weeks']}/{res['next_look_at']} 个合格周"
                f"（前瞻报告 {res.get('n_reports', 0)} 份）{extra}{proof}{why}")
    if s in ("confirmed", "not_confirmed"):
        return (f"🔔 共振加成前瞻检验已到{res['look']}检视点 —— 跑 "
                "`/usr/local/bin/python3 experiments/resonance_boost_forward_test.py` 看结论（需人判断，勿自动改评分）")
    return f"⚠️ 共振加成前瞻检验状态未知：{s}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="共振加成前瞻检验（预注册）")
    ap.add_argument("--insample", action="store_true", help="样本内复核（生成假设的数据，不能用来确认）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--home", help="analysis JSON 所在目录（默认 PATHS.home）")
    ap.add_argument("--db", help="pheromone.db 路径（默认 PATHS.db）")
    ap.add_argument("--today", help="覆盖今天的日期（测试用，YYYY-MM-DD）")
    args = ap.parse_args(argv)
    res = run(home=args.home, db_path=args.db, insample=args.insample, today=args.today)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(res)
    return {"confirmed": 0, "not_confirmed": 0, "insample": 0, "not_ready": 1}.get(res.get("status"), 3)


def _print_human(res: Dict) -> None:
    print("━" * 72)
    print(f"🐝 共振加成前瞻检验（{res.get('mode', '?')}）")
    print("━" * 72)
    print(f"  报告份数 {res.get('n_reports')}｜自证 {res.get('selfproof')}｜跳过文件 {res.get('skipped_files')}")
    for ex in res.get("fail_examples") or []:
        print(f"    不一致样例：{ex}")
    s = res.get("status")
    if s in ("confirmed", "not_confirmed"):
        st = res["stats"]
        print(f"  {res['look']}检视（前 {st['n']} 个合格周，单侧 α={res['alpha']}）："
              f"周均 ΔIC {st['mean']:+.4f}  t={st['t'] if st['t'] is None else round(st['t'], 2)}  p={st['p']:.4f}")
        print(f"  附带：按行数 ΔIC {res['pooled_delta_by_rows']:+.4f}｜IC 均值 B0 {res['mean_ic_b0']:+.4f} → "
              f"B3 {res['mean_ic_b3']:+.4f}｜入场资格变化 {res['gate_changes']}")
        print(f"  检验期内新增世代边界：{res['cohort_boundaries_during_test'] or '无'}")
        print("  结论：" + ("✅ 确认 —— 按预注册删分数加成（失败测试先行、世代边界、等价重放）"
                         if s == "confirmed" else "❌ 未确认 —— 保留加成，登记结案"))
    elif s == "insample":
        st = res["stats_all_weeks"]
        print(f"  样本内（仅复核，不能确认）：{res['weeks']} 周，周均 ΔIC {st['mean']:+.4f} t={st['t']:.2f} "
              f"单侧 p={st['p']:.4f}｜按行数 {res['pooled_delta_by_rows']:+.4f}｜"
              f"IC B0 {res['mean_ic_b0']:+.4f} → B3 {res['mean_ic_b3']:+.4f}｜加权分自证 {res['base_selfproof_rate']:.1%}")
        print(f"  入场资格变化 {res['gate_changes']}")
    else:
        print("  " + status_line(res))


if __name__ == "__main__":
    sys.exit(main())
