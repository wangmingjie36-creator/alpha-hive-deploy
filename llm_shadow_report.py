#!/usr/bin/env python3
"""LLM 影子 A/B 的读者（v0.45.555）：读 `<影子根>/shadow_status/*.json`，先描述、样本量达标后才做预注册检验。

协议冻结在 `experiments/llm_shadow_prereg.md`（看数据之前写的）。这个文件只实现它。

两条臂是什么（⚠️ 不要拿影子扫描里的 `rule_score` 当规则臂）
-----------------------------------------------------
· **LLM 臂** = 影子扫描的 `final_score` / `direction`。
· **规则臂** = **生产当天自己的规则引擎结果**：生产库 `predictions` 里同日同标的的 `final_score` / `direction`
  （生产永远 `--no-llm`；它收盘后 14:00 才跑、影子紧随其后，价格静止，两边输入一致）。
· 影子 `swarm_results` 里的 `rule_score` **不是**规则臂：它由各蜂的维度分算出，而蜂（例如 OracleBee 以 0.6 权重混入 LLM
  的 `smart_money_score` 并可改写方向）在 LLM 模式下已经把 LLM 混进了自己的分数；Queen 的 LLM 只是再加一层。
  所以 `rule_score` 只用于**描述性分解**：Queen 层的作用 = `final_score − rule_score`；
  蜂层的作用（外加两次扫描的输入噪声）≈ `rule_score − 生产 final_score`。分解不参与检验。

做什么
------
· **描述**随时可看：跑了几天、几天健康、成对样本数、LLM 与规则臂的分差、方向翻转率、上述分解。这些**不含**任何
  「谁更准」的信息，所以不算偷看。
· **谁更准**只有在 `healthy_days >= MIN_HEALTHY_DAYS` 且「两臂方向不同、且 T+7 结果已出」的成对数
  `>= MIN_DISCORDANT` 时才**计算**——没到之前这段代码根本不执行，不是算了不显示。检验用 McNemar 精确检验
  （只看两臂判断不同的那些标的日：一边对一边错）。
· 结果（T+7 收盘）与规则臂都从生产库**只读**取（`mode=ro`）。取不到（生产没有该日该标的 / 未到期 / 缺价）的标的日
  不进检验、也不被当成「两边都错」，缺了多少会如实报出来。

只读：不写任何东西。
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

STATUS_DIRNAME = "shadow_status"
MIN_HEALTHY_DAYS = 20          # 预注册：至少 20 个健康的 LLM 日
MIN_DISCORDANT = 60            # 预注册：至少 60 个「两臂方向不同、且 T+7 结果已出」的标的日
DIRECTIONS = ("bullish", "bearish", "neutral")


def load_days(shadow_root: Path, mode: str = "run") -> List[Dict]:
    """读全部 `mode == <mode>` 的状态文件（缺省 "run"；"control" = LLM 关的输入噪声基线），按日期升序。
    坏文件抛 `LedgerCorrupt`（不跳过——少一天会让样本悄悄变少）。"""
    import ledger_io
    d = Path(shadow_root) / STATUS_DIRNAME
    if not d.is_dir():
        return []
    out = []
    # run 与 control 分文件（v0.45.560）：`<日期>.json` / `<日期>.control.json`
    pattern = "????-??-??.json" if mode == "run" else "????-??-??.control.json"
    for p in sorted(d.glob(pattern)):
        doc = ledger_io.load_json(p)
        if doc.get("mode") == mode:
            out.append(doc)
    return out


def generation_of(doc: Dict) -> str:
    """状态文件属于哪一「代」。v0.45.592 前的文件没有这个字段 ⇒ 按模型名记成 `legacy:<model>`（即第 1 代）。"""
    return doc.get("generation") or f"legacy:{doc.get('model')}"


def current_generation(days: Sequence[Dict]) -> Optional[str]:
    """最新一天所属的代（`days` 按日期升序）。没有 LLM 日 ⇒ None。"""
    return generation_of(days[-1]) if days else None


def missing_run_days(prod_db: Path, run_dates: Sequence[str], since: Optional[str], today: str) -> List[str]:
    """生产有扫描、但这一代没有 LLM 日记录的日子（`since` 起，不含 `today`——今天的还在路上）。

    自动跑起来之后，「哪天没跑成」不能只靠看日志：launchd 没触发 / 等生产超时 / 休眠错过，都会让样本悄悄变少。
    """
    if not since or not Path(prod_db).exists():
        return []
    con = sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True)
    try:
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM predictions WHERE date >= ? AND date < ? ORDER BY date", (since, today))]
    finally:
        con.close()
    have = set(run_dates)
    return [d for d in dates if d not in have]


def _num(x) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def llm_rows(days: Sequence[Dict]) -> List[Dict]:
    """健康日里 LLM 臂的逐只记录：只收最终分与方向都齐全的标的。`stage_rule_score` 仅用于描述性分解。"""
    rows = []
    for day in healthy_days(days):
        for tk, v in (day.get("per_ticker") or {}).items():
            if not isinstance(v, dict) or not v.get("present"):
                continue
            ls, ld = _num(v.get("final_score")), v.get("direction")
            if ls is None or ld not in DIRECTIONS:
                continue
            rows.append({"date": day["date"], "ticker": tk, "llm_score": ls, "llm_dir": ld,
                         "stage_rule_score": _num(v.get("rule_score"))})
    return rows


def healthy_days(days: Sequence[Dict]) -> List[Dict]:
    return [d for d in days if (d.get("quality") or {}).get("healthy") is True]


def join_production(prod_db: Path, rows: Sequence[Dict]) -> Tuple[List[Dict], int]:
    """给每条 LLM 记录配上规则臂（生产同日同标的的 final_score / direction）与 T+7 结果。

    返回 (成对记录, 生产里找不到该日该标的的条数)。生产库只读。`ret` 是 T+7 原始涨跌幅 %（收盘价口径），
    缺价 / 未到期 ⇒ None。
    """
    pairs: List[Dict] = []
    missing = 0
    con = sqlite3.connect(f"file:{prod_db}?mode=ro", uri=True)
    try:
        for r in rows:
            row = con.execute(
                "SELECT final_score, direction, price_at_predict, close_t7 FROM predictions "
                "WHERE date=? AND ticker=? ORDER BY id LIMIT 1", (r["date"], r["ticker"])).fetchone()
            if not row or _num(row[0]) is None or row[1] not in DIRECTIONS:
                missing += 1
                continue
            ret = None
            if row[2] and row[3] and row[2] > 0:
                ret = (row[3] - row[2]) / row[2] * 100.0
            pairs.append({**r, "prod_score": float(row[0]), "prod_dir": row[1], "ret": ret})
    finally:
        con.close()
    return pairs, missing


def describe(days: Sequence[Dict], pairs: Sequence[Dict], n_missing: int) -> Dict:
    """描述性统计（不含「谁更准」）。"""
    good = healthy_days(days)
    n = len(pairs)
    diffs = [p["llm_score"] - p["prod_score"] for p in pairs]
    flips = sum(1 for p in pairs if p["llm_dir"] != p["prod_dir"])
    queen = [p["llm_score"] - p["stage_rule_score"] for p in pairs if p["stage_rule_score"] is not None]
    bee = [p["stage_rule_score"] - p["prod_score"] for p in pairs if p["stage_rule_score"] is not None]

    def _mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "run_days": len(days),
        "healthy_days": len(good),
        "unhealthy_days": len(days) - len(good),
        "retried_days": sum(1 for d in days if d.get("previous_attempts")),     # 前面有过零花费失败、同日重跑成功的天
        "pairs": n,
        "missing_in_production": n_missing,
        "mean_score_diff": _mean(diffs),
        "mean_abs_score_diff": _mean([abs(x) for x in diffs]),
        "direction_flip_rate": round(flips / n, 4) if n else None,
        "mean_queen_layer_effect": _mean(queen),       # final_score − 影子 rule_score
        "mean_bee_layer_effect": _mean(bee),           # 影子 rule_score − 生产 final_score（含两次扫描的输入噪声）
        "spend_usd": round(sum((d.get("usage") or {}).get("total_cost_usd") or 0.0 for d in days), 4),
    }


def discordant_decided(pairs: Sequence[Dict]) -> List[Dict]:
    """两臂方向不同、且 T+7 结果已出的成对记录（还没判谁对谁错——只数样本量用）。"""
    return [p for p in pairs if p["llm_dir"] != p["prod_dir"] and p["ret"] is not None]


def mcnemar_exact_p(b: int, c: int) -> float:
    """McNemar 精确检验（双侧）：b、c 是两种不一致格的个数，n = b + c，H0：各 1/2。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def score_discordant(pairs: Sequence[Dict]) -> Dict:
    """只在样本量达标后才被调用。方向不同的标的日里：LLM 对 / 规则对 / 两边都不算数（都错或模糊）。"""
    from outcome_utils import determine_outcome_triplet
    llm_only = rule_only = both_wrong = ambiguous = 0
    for p in discordant_decided(pairs):
        l_ok, l_amb = determine_outcome_triplet(p["llm_dir"], p["ret"])
        r_ok, r_amb = determine_outcome_triplet(p["prod_dir"], p["ret"])
        if l_amb or r_amb:
            ambiguous += 1
        elif l_ok and not r_ok:
            llm_only += 1
        elif r_ok and not l_ok:
            rule_only += 1
        else:
            both_wrong += 1
    return {"llm_only_correct": llm_only, "rule_only_correct": rule_only, "both_wrong": both_wrong,
            "ambiguous_excluded": ambiguous, "mcnemar_p": round(mcnemar_exact_p(llm_only, rule_only), 6)}


def build_report(shadow_root: Path, prod_db: Path, today: Optional[str] = None) -> Dict:
    all_days = load_days(shadow_root)
    gen = current_generation(all_days)
    # 预注册：提示词 / 模型 / max_tokens / 精简规则 / 覆盖范围任一改动 ⇒ 新一代、重新计数，旧代不与新代合并
    days = [d for d in all_days if generation_of(d) == gen]
    rows = llm_rows(days)
    pairs, missing = join_production(prod_db, rows) if (rows and Path(prod_db).exists()) else ([], len(rows))
    desc = describe(days, pairs, missing)
    # 输入噪声基线：LLM 关的影子扫描（--control）与生产的逐只分差——两次扫描之间的取数 / 时点差异，
    # LLM 臂的分差要和它比才知道有多少是 LLM 带来的。
    cdays = load_days(shadow_root, "control")
    cpairs, _cmiss = join_production(prod_db, llm_rows(cdays)) if (cdays and Path(prod_db).exists()) else ([], 0)
    cdiffs = [abs(p["llm_score"] - p["prod_score"]) for p in cpairs]
    desc["generation"] = gen
    desc["other_generation_days"] = len(all_days) - len(days)
    desc["missing_run_days"] = missing_run_days(
        prod_db, [d["date"] for d in days], days[0]["date"] if days else None,
        today or date.today().isoformat())
    desc["noise_control_days"] = len(cdays)
    desc["noise_control_pairs"] = len(cpairs)
    desc["noise_mean_abs_score_diff"] = round(sum(cdiffs) / len(cdiffs), 4) if cdiffs else None
    desc["noise_direction_flip_rate"] = (round(sum(1 for p in cpairs if p["llm_dir"] != p["prod_dir"]) / len(cpairs), 4)
                                         if cpairs else None)
    n_dis = len(discordant_decided(pairs))
    ready = desc["healthy_days"] >= MIN_HEALTHY_DAYS and n_dis >= MIN_DISCORDANT
    rep = {"describe": desc, "discordant_decided": n_dis,
           "thresholds": {"min_healthy_days": MIN_HEALTHY_DAYS, "min_discordant": MIN_DISCORDANT},
           "ready": ready, "result": None}
    if ready:                                    # 没到门槛之前，这一行根本不执行
        rep["result"] = score_discordant(pairs)
    return rep


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LLM 影子 A/B 报告（只读；协议见 experiments/llm_shadow_prereg.md）")
    ap.add_argument("--shadow-root", default="", help="影子数据根（缺省生产根的同级 alpha-hive-llm-shadow）")
    args = ap.parse_args(argv)
    import hive_logger
    from llm_shadow_scan import default_shadow_root
    root = Path(args.shadow_root) if args.shadow_root else default_shadow_root(hive_logger.resolve_data_root())
    rep = build_report(root, Path(hive_logger.PATHS.db))
    d = rep["describe"]
    print(f"影子根 {root}")
    print(f"当前代 {d['generation']}（另有 {d['other_generation_days']} 个旧代 LLM 日不计入）")
    print(f"LLM 日 {d['run_days']}（健康 {d['healthy_days']} / 不健康 {d['unhealthy_days']}）| 成对标的日 {d['pairs']} | "
          f"累计花费 ${d['spend_usd']:.2f}")
    if d["retried_days"]:
        print(f"ℹ️ 有 {d['retried_days']} 天先失败（零花费）后同日重跑，旧记录在该日状态的 previous_attempts 里")
    if d["missing_run_days"]:
        print(f"⚠️ 生产有扫描、这一代却没有 LLM 记录的日子（{len(d['missing_run_days'])}）："
              f"{' '.join(d['missing_run_days'])}——自动任务没跑成？")
    print(f"生产里找不到的标的日 {d['missing_in_production']}")
    print(f"分差（LLM−生产规则）均值 {d['mean_score_diff']} | 均绝对值 {d['mean_abs_score_diff']} | 方向翻转率 {d['direction_flip_rate']}")
    print(f"输入噪声基线（--control 与生产，{d['noise_control_days']} 天 {d['noise_control_pairs']} 对）："
          f"均绝对分差 {d['noise_mean_abs_score_diff']} | 方向翻转率 {d['noise_direction_flip_rate']}")
    print(f"分解（描述性，不参与检验）：Queen 层 {d['mean_queen_layer_effect']} | 蜂层（含输入噪声）{d['mean_bee_layer_effect']}")
    t = rep["thresholds"]
    print(f"预注册门槛：健康日 ≥ {t['min_healthy_days']}（现 {d['healthy_days']}）；"
          f"方向不一致且 T+7 已出的标的日 ≥ {t['min_discordant']}（现 {rep['discordant_decided']}）")
    if not rep["ready"]:
        print("未达预注册样本量——不计算、不报告两臂命中率（避免边攒边偷看）。")
        return 0
    r = rep["result"]
    print(f"方向不一致的标的日：只有 LLM 对 {r['llm_only_correct']} | 只有规则对 {r['rule_only_correct']} | "
          f"都错 {r['both_wrong']} | 模糊已剔除 {r['ambiguous_excluded']}")
    print(f"McNemar 精确检验 p = {r['mcnemar_p']}（双侧）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
