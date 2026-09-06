#!/usr/bin/env python3
"""🐝 Alpha Hive — 概率记分卡 (v0.45.134 Step 3)

回答一个问题：**我们印出去的那个概率，准不准？**

为什么需要它
------------
v0.45.134 之前，报告里的「赚钱概率」是一条 base 0.55 加常数的公式，803 份生产
报告里 81% 恒为 65.0。它错得很离谱（样本外校准误差 11.8pp），但**没有任何东西
会因此变红**——没有记分规则，一个概率模型就是不可证伪的。换成真实历史频率之后
这个洞依然在：新的数不一定更准，而如果没人记分，「更准」就只是一句话。

所以本模块存在的意义不是算一个漂亮的指标，是**给概率装一个会红的观测点**：
`--walk-forward` 在新估计器劣于它替换掉的那个常数时**退出码 1**。

两种记分，互补
--------------
1. `--walk-forward`（回溯，今天就能跑）
   按时点重建估计量，与两条基线对比。它回答「换掉常数是不是真的更好」。
   ⚠️ 时点隔离是这件事的全部：T+7 是 **7 个交易日**（backtester 用 US_BDAY），
   最长约 11 个自然日，故默认 embargo **14 个自然日**——在日期 D 做的估计，
   只许看 `date <= D - 14` 且 close_t7 已知的样本。放松它就是数据泄漏，
   而泄漏出来的记分卡会**好看**，正是最难被发现的那种错。

2. `--published`（前向，需要账本攒数）
   读 `probability_scorecard_state/published.jsonl`——那里记的是**当天真正
   印出去的数**，不是今天重算的数。回溯重建假设估计量是 DB 的纯函数；
   一旦哪天换了估计量或补跑了历史，这个假设就破了。账本不受影响。

口径（与 `advanced_analyzer.HistoricalAnalyzer` 严格一致，否则是在给另一个
定义打分）：
  · 收益 = `close_t7 / price_at_predict − 1`（干净收盘口径）
  · 命中 = 方向调整后收益 **> 0**；恰好为 0 计入分母算未命中
  · 只用 bullish / bearish；neutral 没有方向命中率可言

用法
----
    /usr/local/bin/python3 probability_scorecard.py --walk-forward
    /usr/local/bin/python3 probability_scorecard.py --published
    /usr/local/bin/python3 probability_scorecard.py --walk-forward --json

退出码
------
    0 = 记分完成，且新估计器不劣于被它替换的常数
    1 = **新估计器劣于旧常数**（Brier 更高）—— 这就是那个会红的观测点
    3 = 无法判定（库缺失 / 样本不足）
        ⚠️ 3 而非 2：编排器 `run_step()` 把 2 保留给「脚本不存在」
"""

from __future__ import annotations

import argparse
import json
import logging as _logging
import math
import sqlite3
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_log = _logging.getLogger("alpha_hive.probability_scorecard")

ALPHAHIVE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

STATE_DIR = ALPHAHIVE_DIR / "probability_scorecard_state"
LEDGER_PATH = STATE_DIR / "published.jsonl"

#: 时点隔离：T+7 = 7 个交易日，最长约 11 个自然日；14 天是安全余量。
#: **不要为了多凑样本调小它**——泄漏会让记分卡变好看，那是最难发现的错。
EMBARGO_DAYS = 14

#: 够不够算一个分票分方向的频率。与 HistoricalAnalyzer.MIN_SAMPLE 同源：
#: 少于这个数，频率本身的抽样误差比它想测的差异还大。
MIN_SAMPLE = 20

#: v0.45.134 删掉的那个常数（base 0.55 + 拥挤度 0.08 + 催化剂 0.02）。
#: 生产 803 份报告里 81% 就是它，作为对照行保留。
LEGACY_CONSTANT_PCT = 65.0

#: 生产当前真正用的是哪个候选量（v0.45.138 起 = 全书池化基准率）。
#: **改生产估计量时必须同时改这里**，否则告警会盯着一个没人在用的行——
#: 一个不对应实际产品的红，比没有红更糟：它训练人忽略告警。
PRODUCTION_MODEL = "base_rate"

#: 告警判据：**有没有别的候选量明显打得过生产用的那个**。
#: 「生产 vs 旧常数」是个差判据——基准率几乎必然赢过一个固定常数，
#: 那样的告警永远不会红，等于没装。这里问的是「就在旁边有没有更好的」。
BRIER_MARGIN = 0.002

#: 生产当前的融合权重（generate_ml_report._combine_recommendations：
#: combined = w·前瞻 + (1−w)·ML）。v0.45.139 实测 w∈[0.7, 1.0] 的 Brier 极差
#: 0.0007 < BRIER_MARGIN ⇒ **不改数，只监控**——在噪声里调参是假优化，
#: 而改了会破坏前向账本的可比性。**改生产权重时必须同时改这里。**
PRODUCTION_BLEND_W = 0.7
BLEND_GRID = tuple(i / 10 for i in range(11))

_EPS = 1e-9


# ══════════════════════════════════════════════════════════════════════
# 指标
# ══════════════════════════════════════════════════════════════════════
def brier(preds: List[float], ys: List[int]) -> float:
    """Brier 分数（越低越好）。preds 为 [0,1] 概率，ys 为 0/1。"""
    if not ys:
        raise ValueError("empty sample")
    return sum((p - y) ** 2 for p, y in zip(preds, ys)) / len(ys)


def log_loss(preds: List[float], ys: List[int]) -> float:
    """对数损失（越低越好）。preds 会被夹到 (0,1) 内避免 log(0) 爆炸。"""
    if not ys:
        raise ValueError("empty sample")
    tot = 0.0
    for p, y in zip(preds, ys):
        p = min(max(p, _EPS), 1 - _EPS)
        tot += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return tot / len(ys)


def calibration_error_pp(preds: List[float], ys: List[int]) -> float:
    """|平均预测 − 实际频率|，单位 pp。测的是「说 70% 的那些真赢了 70% 吗」。"""
    if not ys:
        raise ValueError("empty sample")
    return abs(sum(preds) / len(preds) - sum(ys) / len(ys)) * 100.0


def _score_block(preds: List[float], ys: List[int]) -> Dict:
    return {
        "n": len(ys),
        "brier": round(brier(preds, ys), 4),
        "log_loss": round(log_loss(preds, ys), 4),
        "calibration_error_pp": round(calibration_error_pp(preds, ys), 1),
        "mean_pred_pct": round(sum(preds) / len(preds) * 100, 1),
        "observed_pct": round(sum(ys) / len(ys) * 100, 1),
    }


# ══════════════════════════════════════════════════════════════════════
# 前向账本
# ══════════════════════════════════════════════════════════════════════
def record_published(
    report_date: str,
    ticker: str,
    direction: Optional[str],
    hit_rate_pct: Optional[float],
    basis: Optional[str],
    sample_size: Optional[int],
    ledger_path: Optional[Path] = None,
    forward_estimate_pct: Optional[float] = None,
    forward_sample_size: Optional[int] = None,
    ml_probability_pct: Optional[float] = None,
) -> bool:
    """把**当天真正印出去的**两个数记进账本；返回是否写入。

    v0.45.138 起记两个口径，因为报告里就是两个：
      · `hit_rate_pct`        —— 描述量，本标的本方向的历史频率
      · `forward_estimate_pct`—— 前瞻量，全书池化，**评级与融合用的就是它**
    记分（`score_published`）打的是**前瞻量**——那才是被当作预测印出去的数。
    描述量一并记下，是为了日后能回答「当初若用描述量会怎样」而不必重建历史。

    幂等：同一 (report_date, ticker) 已存在则不重复追加（补跑扫描不会污染账本）。
    两个数为 None 也要记 —— 「这天没印出概率」本身是需要被记住的事实，
    不记就没法回答「覆盖率是多少」，而覆盖率正是换源的主要代价。
    """
    path = Path(ledger_path) if ledger_path else LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    key = (str(report_date), str(ticker))
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if (row.get("date"), row.get("ticker")) == key:
                    return False
    rec = {
        "date": str(report_date),
        "ticker": str(ticker),
        "direction": direction,
        "hit_rate_pct": hit_rate_pct,
        "basis": basis,
        "sample_size": sample_size,
        "forward_estimate_pct": forward_estimate_pct,
        "forward_sample_size": forward_sample_size,
        "ml_probability_pct": ml_probability_pct,     # v0.45.139：融合权重扫描的第二个输入
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def load_ledger(ledger_path: Optional[Path] = None) -> Tuple[List[Dict], str]:
    """读账本；返回 (rows, status)。status ∈ {ok, missing, error}"""
    path = Path(ledger_path) if ledger_path else LEDGER_PATH
    if not path.exists():
        return [], "missing"
    rows: List[Dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    _log.warning("账本有坏行，已跳过：%.80s", line)
    except OSError as e:
        _log.warning("读账本失败 %s: %s", path, e)
        return [], "error"
    return rows, "ok"


# ══════════════════════════════════════════════════════════════════════
# 实现值
# ══════════════════════════════════════════════════════════════════════
def load_outcomes(db_path: Optional[Path] = None) -> Tuple[List[Dict], str]:
    """从 pheromone.db 读已核对 T+7 的方向性预测。返回 (rows, status)。

    口径与 HistoricalAnalyzer 逐字一致：收益 = close_t7/price_at_predict − 1，
    命中 = 方向调整后 > 0（恰好为 0 计入分母算未命中）。
    """
    if db_path is None:
        from feedback_loop import PHEROMONE_DB_PATH  # 路径唯一真相在 feedback_loop
        db_path = PHEROMONE_DB_PATH
    db_path = Path(db_path)
    if not db_path.exists():
        _log.warning("[scorecard] %s 不存在，记分不可得", db_path)
        return [], "missing"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            raw = con.execute(
                "SELECT date, ticker, direction, price_at_predict, close_t7 "
                "FROM predictions WHERE close_t7 IS NOT NULL AND price_at_predict > 0"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as e:
        _log.warning("[scorecard] 读取 %s 失败: %s", db_path, e)
        return [], "error"

    out: List[Dict] = []
    skipped = 0
    for d, tk, direction, p0, c7 in raw:
        if direction not in ("bullish", "bearish"):
            continue
        try:
            p0 = float(p0)
            c7 = float(c7)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not (math.isfinite(p0) and math.isfinite(c7)) or p0 <= 0:
            skipped += 1
            continue
        ret = (c7 / p0 - 1) * 100.0
        adj = ret if direction == "bullish" else -ret
        out.append({"date": str(d), "ticker": str(tk), "direction": direction,
                    "ret_pct": ret, "hit": 1 if adj > 0 else 0})
    if skipped:
        _log.info("[scorecard] 跳过 %d 条坏行", skipped)
    out.sort(key=lambda r: r["date"])
    return out, "ok"


def load_ml_probabilities(
    reports_dir: Optional[Path] = None,
) -> Tuple[Dict[Tuple[str, str], float], str]:
    """从 analysis-*-ml-*.json 读每天**印出去的** ML 概率（百分数）。

    键 = (文件名日期, ticker)。用文件名日期而非 `timestamp`：后者是生成时刻，
    次日重跑会晚一天——2026-09-06 核对 110 份重跑，间隔恒为 1 天、同模型，无泄漏。
    v0.45.139 起账本也记 `ml_probability_pct`，日后可改读账本；历史只有 JSON 里有。
    """
    d = Path(reports_dir) if reports_dir else ALPHAHIVE_DIR
    files = sorted(d.glob("analysis-*-ml-*.json"))
    if not files:
        return {}, "no_reports"
    out: Dict[Tuple[str, str], float] = {}
    bad = 0
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bad += 1
            continue
        m = (data.get("combined_recommendation") or {}).get("ml_probability")
        if not isinstance(m, (int, float)) or isinstance(m, bool) or not math.isfinite(m):
            continue
        fdate = p.name.split("-ml-")[-1][:10]
        tk = data.get("ticker")
        if not tk:
            continue
        out[(fdate, str(tk))] = float(m)
    if bad:
        _log.warning("[scorecard] %d 份报告 JSON 读取失败，已跳过", bad)
    return out, "ok"


# ══════════════════════════════════════════════════════════════════════
# 回溯记分（时点隔离）
# ══════════════════════════════════════════════════════════════════════
def _parse_day(s: str) -> Optional[_date]:
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def walk_forward(
    rows: Optional[List[Dict]] = None,
    embargo_days: int = EMBARGO_DAYS,
    min_sample: int = MIN_SAMPLE,
    db_path: Optional[Path] = None,
) -> Dict:
    """按时点重建三个估计量并记分。

    在日期 D 的估计只许看 `date <= D - embargo_days` 且已知结果的样本。
    三个估计量：
      · `legacy_constant` —— v0.45.134 删掉的那个 65.0
      · `base_rate`       —— 时点池化基准率（零区分度的诚实答案）
      · `hit_rate`        —— 分票分方向频率，样本不足退回 base_rate
    """
    status = "ok"
    if rows is None:
        rows, status = load_outcomes(db_path)
    if status != "ok" or not rows:
        return {"status": status if status != "ok" else "no_samples",
                "embargo_days": embargo_days, "min_sample": min_sample}

    dated = [(r, _parse_day(r["date"])) for r in rows]
    dated = [(r, d) for r, d in dated if d is not None]
    dated.sort(key=lambda t: t[1])

    preds: Dict[str, List[float]] = {"legacy_constant": [], "base_rate": [],
                                     "per_ticker_hit_rate": []}
    ys: List[int] = []
    covered = 0          # hit_rate 真的用上了分票频率的次数
    first_scored: Optional[str] = None

    for r, d in dated:
        cutoff = d - timedelta(days=embargo_days)
        hist = [h for h, hd in dated if hd <= cutoff]
        if not hist:
            continue  # 最早那批没有可用历史，无法给出任何估计 —— 不进分母
        base = sum(h["hit"] for h in hist) / len(hist)
        same = [h for h in hist
                if h["ticker"] == r["ticker"] and h["direction"] == r["direction"]]
        if len(same) >= min_sample:
            hr = sum(h["hit"] for h in same) / len(same)
            covered += 1
        else:
            hr = base
        preds["legacy_constant"].append(LEGACY_CONSTANT_PCT / 100.0)
        preds["base_rate"].append(base)
        preds["per_ticker_hit_rate"].append(hr)
        ys.append(r["hit"])
        if first_scored is None:
            first_scored = r["date"]

    if not ys:
        return {"status": "no_samples_after_embargo",
                "embargo_days": embargo_days, "min_sample": min_sample,
                "total_rows": len(dated)}

    models = {k: _score_block(v, ys) for k, v in preds.items()}
    best = min(models, key=lambda k: models[k]["brier"])
    prod = models[PRODUCTION_MODEL]["brier"]
    beaten_by = {k: round(prod - m["brier"], 4) for k, m in models.items()
                 if prod - m["brier"] > BRIER_MARGIN}
    return {
        "status": "ok",
        "production_model": PRODUCTION_MODEL,
        "best_model": best,
        "beaten_by": beaten_by,
        "production_is_beaten": bool(beaten_by),
        "embargo_days": embargo_days,
        "min_sample": min_sample,
        "total_rows": len(dated),
        "scored": len(ys),
        "date_range": [first_scored, dated[-1][0]["date"]],
        "per_ticker_coverage_pct": round(covered / len(ys) * 100, 1),
        "models": models,
    }


# ══════════════════════════════════════════════════════════════════════
# 融合权重扫描（时点隔离）
# ══════════════════════════════════════════════════════════════════════
def blend_scan(
    rows: Optional[List[Dict]] = None,
    ml: Optional[Dict[Tuple[str, str], float]] = None,
    embargo_days: int = EMBARGO_DAYS,
    db_path: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
    grid: Sequence[float] = BLEND_GRID,
) -> Dict:
    """扫 w ∈ grid：combined = w·时点基准率 + (1−w)·ML，给每个 w 记分。

    这是 v0.45.139 把「0.7/0.3」从魔数变成受监控量的承载物。判据与
    walk_forward 同型：**网格上有没有 w 明显打得过生产在用的 PRODUCTION_BLEND_W**。
    实测（2026-09-06，n=393）曲线在 [0.7, 1.0] 是平的——那正是「不该调」的证据；
    等 ML 真有区分度那天，曲线会向左倾斜、这里会红。
    """
    status = "ok"
    if rows is None:
        rows, status = load_outcomes(db_path)
    if status != "ok" or not rows:
        return {"status": status if status != "ok" else "no_samples",
                "embargo_days": embargo_days}
    if ml is None:
        ml, mstatus = load_ml_probabilities(reports_dir)
        if mstatus != "ok":
            return {"status": mstatus, "embargo_days": embargo_days}

    dated = [(r, _parse_day(r["date"])) for r in rows]
    dated = [(r, d) for r, d in dated if d is not None]
    dated.sort(key=lambda t: t[1])

    recs: List[Tuple[int, float, float]] = []   # (y, base_pit, ml)
    first: Optional[str] = None
    for r, d in dated:
        key = (r["date"], r["ticker"])
        if key not in ml:
            continue
        cutoff = d - timedelta(days=embargo_days)
        hist = [h for h, hd in dated if hd <= cutoff]
        if not hist:
            continue
        base = sum(h["hit"] for h in hist) / len(hist)
        recs.append((r["hit"], base, ml[key] / 100.0))
        if first is None:
            first = r["date"]
    if not recs:
        return {"status": "no_joined_rows", "embargo_days": embargo_days,
                "outcome_rows": len(dated), "ml_rows": len(ml)}

    ys = [y for y, _, _ in recs]
    grid_out = []
    for w in grid:
        ps = [w * b + (1 - w) * m for _, b, m in recs]
        grid_out.append({"w": round(w, 3), **_score_block(ps, ys)})
    best = min(grid_out, key=lambda g: g["brier"])
    prod = next((g for g in grid_out if abs(g["w"] - PRODUCTION_BLEND_W) < 1e-9), None)
    if prod is None:   # 生产权重不在网格上 → 单独算一次，别拿最近的格点冒充
        ps = [PRODUCTION_BLEND_W * b + (1 - PRODUCTION_BLEND_W) * m for _, b, m in recs]
        prod = {"w": PRODUCTION_BLEND_W, **_score_block(ps, ys)}
    gap = round(prod["brier"] - best["brier"], 4)
    beaten = gap > BRIER_MARGIN
    return {
        "status": "ok",
        "embargo_days": embargo_days,
        "n": len(ys),
        "date_range": [first, recs and dated[-1][0]["date"]],
        "grid": grid_out,
        "production_model": f"blend_w={PRODUCTION_BLEND_W}",
        "production_w": PRODUCTION_BLEND_W,
        "production_brier": prod["brier"],
        "best_w": best["w"],
        "best_brier": best["brier"],
        "gap": gap,
        "beaten_by": ({f"blend_w={best['w']}": gap} if beaten else {}),
        "production_is_beaten": beaten,
    }


# ══════════════════════════════════════════════════════════════════════
# 前向记分
# ══════════════════════════════════════════════════════════════════════
def score_published(
    ledger_path: Optional[Path] = None, db_path: Optional[Path] = None
) -> Dict:
    """账本 ⋈ 实现值：给**真正印出去的**那些数打分。"""
    led, lstatus = load_ledger(ledger_path)
    if lstatus != "ok":
        return {"status": f"ledger_{lstatus}"}
    out, ostatus = load_outcomes(db_path)
    if ostatus != "ok":
        return {"status": f"outcomes_{ostatus}"}
    realized = {(r["date"], r["ticker"]): r["hit"] for r in out}

    preds: List[float] = []
    ys: List[int] = []
    printed = matched = 0
    for row in led:
        # v0.45.138：记分打**前瞻量**——那才是被当作预测印出去的数。
        # ⚠️ 不要 `row.get("forward_estimate_pct") or row.get("hit_rate_pct")`：
        # 那会让新旧两个口径的行混进同一张记分卡，而它们测的不是同一件事。
        # v0.45.138 之前的行没有这个键 → 一律跳过，宁可样本少也不混算。
        hr = row.get("forward_estimate_pct")
        if not isinstance(hr, (int, float)) or isinstance(hr, bool) or not math.isfinite(hr):
            continue  # 那天没印出前瞻概率 —— 计入覆盖率，不计入记分
        printed += 1
        y = realized.get((row.get("date"), row.get("ticker")))
        if y is None:
            continue  # T+7 还没到 / 还没回填
        matched += 1
        preds.append(hr / 100.0)
        ys.append(y)

    res = {
        "status": "ok" if ys else "no_matured_rows",
        "ledger_rows": len(led),
        "with_probability": printed,
        "coverage_pct": round(printed / len(led) * 100, 1) if led else 0.0,
        "matured": matched,
    }
    if ys:
        res["published"] = _score_block(preds, ys)
        res["legacy_constant"] = _score_block([LEGACY_CONSTANT_PCT / 100.0] * len(ys), ys)
        res["worse_than_legacy"] = (
            res["published"]["brier"] > res["legacy_constant"]["brier"]
        )
    return res


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════
def _fmt(res: Dict, title: str) -> str:
    bar = "━" * 72
    lines = [bar, f"🐝 Alpha Hive · 概率记分卡 — {title}", bar]
    if res.get("status") != "ok":
        lines += [f"  状态：{res.get('status')}（无法判定）", bar]
        return "\n".join(lines)
    if "models" in res:
        lines += [
            f"  样本 {res['scored']} / {res['total_rows']} 条"
            f"（embargo {res['embargo_days']} 天，{res['date_range'][0]} → {res['date_range'][1]}）",
            f"  分票分方向频率可用率：{res['per_ticker_coverage_pct']}%"
            f"（不足 n≥{res['min_sample']} 时退回基准率）",
            "",
            f"  {'估计量':22}{'Brier↓':>9}{'LogLoss↓':>10}{'校准误差':>10}{'平均预测':>10}",
        ]
        names = {"legacy_constant": "v0.45.134 前的常数 65.0",
                 "base_rate": "时点池化基准率", "per_ticker_hit_rate": "分票分方向频率"}
        for k in ("legacy_constant", "base_rate", "per_ticker_hit_rate"):
            m = res["models"][k]
            tag = " ← 生产在用" if k == res.get("production_model") else ""
            lines.append(f"  {names[k]:22}{m['brier']:>9.4f}{m['log_loss']:>10.4f}"
                         f"{m['calibration_error_pp']:>9.1f}pp{m['mean_pred_pct']:>9.1f}%{tag}")
        lines += ["", f"  实际命中率：{res['models']['base_rate']['observed_pct']}%"]
    elif "grid" in res:
        lines += [
            f"  样本 {res['n']} 条（embargo {res['embargo_days']} 天，"
            f"{res['date_range'][0]} → {res['date_range'][1]}）",
            "",
            f"  {'w(前瞻权重)':>11}{'Brier↓':>9}{'LogLoss↓':>10}{'校准误差':>10}",
        ]
        for g in res["grid"]:
            tag = ""
            if abs(g["w"] - res["production_w"]) < 1e-9:
                tag += " ← 生产在用"
            if abs(g["w"] - res["best_w"]) < 1e-9:
                tag += " ← Brier 最优"
            lines.append(f"  {g['w']:>11.1f}{g['brier']:>9.4f}{g['log_loss']:>10.4f}"
                         f"{g['calibration_error_pp']:>9.1f}pp{tag}")
        lines += ["", f"  生产 w={res['production_w']} 与最优 w={res['best_w']} 的 Brier 差 {res['gap']:.4f}"
                      f"（告警阈 {BRIER_MARGIN}）"]
    else:
        lines += [
            f"  账本 {res['ledger_rows']} 行，其中印出概率 {res['with_probability']} 行"
            f"（覆盖率 {res['coverage_pct']}%），已到期 {res['matured']} 行",
        ]
        if "published" in res:
            p, l0 = res["published"], res["legacy_constant"]
            lines += ["",
                      f"  实际印出去的：Brier {p['brier']:.4f} | 校准误差 {p['calibration_error_pp']:.1f}pp",
                      f"  旧常数对照：  Brier {l0['brier']:.4f} | 校准误差 {l0['calibration_error_pp']:.1f}pp"]
    lines.append(bar)
    if res.get("production_is_beaten"):
        _by = "、".join(f"{k}（Brier 低 {d:.4f}）" for k, d in res["beaten_by"].items())
        lines.append(f"❌ 生产在用的 `{res.get('production_model')}` 被别的候选打过：{_by}")
    elif res.get("worse_than_legacy"):
        lines.append("❌ 已印出去的概率 Brier 劣于它替换掉的常数 —— 去查")
    else:
        lines.append(f"✅ 生产在用的 `{res.get('production_model', '?')}` 未被任何候选量明显打过")
    lines.append(bar)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="概率记分卡：印出去的概率准不准")
    ap.add_argument("--walk-forward", action="store_true",
                    help="回溯记分（时点隔离），今天就能跑")
    ap.add_argument("--published", action="store_true",
                    help="前向记分：只给账本里真正印出去的数打分")
    ap.add_argument("--blend-scan", action="store_true",
                    help="扫融合权重 w∈[0,1]（时点隔离），判 PRODUCTION_BLEND_W 是否被打过")
    ap.add_argument("--reports-dir", default=None,
                    help="analysis-*-ml-*.json 所在目录（默认模块目录）")
    ap.add_argument("--embargo-days", type=int, default=EMBARGO_DAYS)
    ap.add_argument("--min-sample", type=int, default=MIN_SAMPLE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=None,
                    help="pheromone.db 路径（默认取 feedback_loop.PHEROMONE_DB_PATH）")
    ap.add_argument("--ledger", default=None, help="前向账本路径")
    args = ap.parse_args(argv)

    if not (args.walk_forward or args.published or args.blend_scan):
        args.walk_forward = True

    _db = Path(args.db) if args.db else None
    if args.blend_scan:
        res = blend_scan(embargo_days=args.embargo_days, db_path=_db,
                         reports_dir=Path(args.reports_dir) if args.reports_dir else None)
        title = "融合权重扫描（时点隔离）"
    elif args.walk_forward:
        res = walk_forward(embargo_days=args.embargo_days, min_sample=args.min_sample,
                           db_path=_db)
        title = "回溯（时点隔离）"
    else:
        res = score_published(ledger_path=Path(args.ledger) if args.ledger else None,
                              db_path=_db)
        title = "前向（已印出的）"
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else _fmt(res, title))

    if res.get("status") != "ok":
        return 3
    if res.get("production_is_beaten") or res.get("worse_than_legacy"):
        return 1
    return 0


if __name__ == "__main__":
    _logging.basicConfig(level=_logging.INFO,
                         format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                         datefmt="%H:%M:%S")
    sys.exit(main())
