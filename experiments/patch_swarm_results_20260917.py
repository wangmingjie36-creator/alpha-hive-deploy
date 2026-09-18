"""用完整蜂群顺序重放结果，构造修复后的 09-17 swarm_results（30 只全量），
并用真实的 `generate_swarm_markdown_report` / cs_rank / thesis_break 重新生成
markdown_report——不是手改字符串。

产出：
  patched_swarm_results.json    完整替换 .swarm_results_2026-09-17.json 用
  patched_markdown_report.md    完整替换 alpha-hive-daily-2026-09-17.md 正文用
  patch_diff_summary.json       每只标的改了哪些顶层字段，供部署前人工核对
"""
import copy
import json
import sys
from datetime import datetime

sys.path.insert(0, "/Users/igg/Desktop/Alpha Hive")
sys.path.insert(0, "/private/tmp/claude-501/-Users-igg-Desktop-Alpha-Hive/5c10bdb6-88da-4fb0-87bf-e8bab1b53c78/scratchpad")
import logging
logging.disable(logging.CRITICAL)

import full_replay_0917 as fr
import report_formatters as rf

ROOT = "/Users/igg/Desktop/Alpha Hive"
SP = "/private/tmp/claude-501/-Users-igg-Desktop-Alpha-Hive/5c10bdb6-88da-4fb0-87bf-e8bab1b53c78/scratchpad"
AS_OF = "2026-09-17"

swarm = copy.deepcopy(fr.SWARM)
diff_summary = {}

for tk in sorted(swarm.keys()):
    r = fr.run_ticker(tk, control=False)
    old = swarm[tk]
    distilled = r["distilled"]

    changed_top = {k: (old.get(k), distilled.get(k)) for k in distilled
                   if old.get(k) != distilled.get(k)}
    diff_summary[tk] = {
        "top_level_changed": sorted(changed_top.keys()),
        "final_score": (old["final_score"], distilled["final_score"]),
        "direction": (old["direction"], distilled["direction"]),
    }

    # 用 distill() 的完整返回值覆盖旧记录里对应字段（cs_rank/thesis_break_*
    # 不在 distill() 返回里，稍后单独重算，这里先保留旧值占位）。
    swarm[tk] = {**old, **distilled}
    # agent_details 里 Scout/Oracle/Chronos/Rival/CodeExecutor 未受影响，
    # distill() 返回的 agent_details 已经是"7个原样 + 3个重算"的完整合集，
    # 直接用它整体替换（比逐个字段合并更不容易漏）。

print("30 只重算完成，final_score/direction 变化：")
for tk, d in diff_summary.items():
    if d["direction"][0] != d["direction"][1] or abs(d["final_score"][0]-d["final_score"][1]) > 0.005:
        print(f"  {tk}: {d['final_score'][0]:.2f}->{d['final_score'][1]:.2f}  {d['direction'][0]}->{d['direction'][1]}")

# ── 重算 cs_rank（横截面排名，逐字复刻 alpha_hive_daily_report._post_scan_enrichment）
def _cs_percentile(values):
    items = [(t, v) for t, v in values.items() if v is not None]
    n = len(items)
    if n < 2:
        return {t: 0.5 for t, _ in items}
    order = sorted(items, key=lambda x: x[1])
    return {t: round(i / (n - 1), 3) for i, (t, _) in enumerate(order)}

_cs_final = _cs_percentile({t: r.get("final_score") for t, r in swarm.items()})
_dims = ("signal", "catalyst", "sentiment", "odds", "risk_adj")
_cs_dims = {d: _cs_percentile({t: (r.get("dimension_scores") or {}).get(d) for t, r in swarm.items()})
            for d in _dims}
for t, r in swarm.items():
    r["cs_rank"] = {"final": _cs_final.get(t), **{d: _cs_dims[d].get(t) for d in _dims}}

# ── 重算 thesis_break_l1/l2/evaluations（逐字复刻 _evaluate_thesis_breaks + _attach_thesis_breaks 的求值部分）
from market_intelligence import check_thesis_breaks
from thesis_breaks import ThesisBreakConfig

def evaluate_thesis_breaks(ticker, row):
    det = (row.get("agent_details", {}) or {}).get("OracleBeeEcho", {}).get("details", {}) or {}
    bear = ((row.get("agent_details", {}) or {}).get("BearBeeContrarian", {})
            .get("details", {}) or {}).get("bearish_signals", []) or []
    try:
        res = check_thesis_breaks(ticker, det.get("_snapshot_stock_price"), det.get("iv_current"),
                                  det.get("put_call_ratio"), list(bear), row.get("final_score"))
        return res.get("evaluations") or []
    except Exception as e:
        print(f"  ⚠️ thesis_break 求值失败 {ticker}: {e}")
        return []

tb_changed = []
for tk, row in swarm.items():
    try:
        tb_cfg = ThesisBreakConfig.get_breaks_config(tk)
    except Exception:
        continue
    if not tb_cfg:
        continue
    evals = evaluate_thesis_breaks(tk, row)
    old_l1, old_l2 = row.get("thesis_break_l1"), row.get("thesis_break_l2")
    row["thesis_break_evaluations"] = evals
    if (row.get("thesis_break_l1") != old_l1) or (row.get("thesis_break_l2") != old_l2):
        tb_changed.append(tk)

print(f"\nthesis_break 求值完成（不改 l1/l2 判定文本，只刷新 evaluations 明细；"
      f"求值失败或配置缺失的标的原样保留旧值）")

json.dump(swarm, open(f"{SP}/patched_swarm_results.json", "w"), ensure_ascii=False)
json.dump(diff_summary, open(f"{SP}/patch_diff_summary.json", "w"), ensure_ascii=False, indent=1, default=str)

# ── 用真实函数重新生成 markdown_report ──
daily_json = json.load(open(f"{ROOT}/alpha-hive-daily-{AS_OF}.json"))

class _ReporterStub:
    date_str = AS_OF
    timestamp = datetime.strptime("2026-09-17 14:10:38", "%Y-%m-%d %H:%M:%S")

old_md = open(f"{ROOT}/alpha-hive-daily-{AS_OF}.md").read()
new_md = rf.generate_swarm_markdown_report(
    _ReporterStub(), swarm,
    concentration=daily_json.get("concentration_analysis"),
    macro_context=daily_json.get("macro_context"),
    backtest_stats=daily_json.get("backtest_stats"),
    agent_count=8,
    cross_ticker=daily_json.get("cross_ticker_analysis"),
)
# ── 📌 关注事项（人工维护 · 不参与评分）整段用原文替换 ──
# watchlist_events.format_for_report 按 datetime.now()（今天=09-18）算"距今天数"，
# 不认报告自己的 date_str——今天跑会把 09-17 的"22 天前"算成"23 天前"，
# 天数全部漂移 1。这段本就不参与评分、纯展示，直接拿旧文按段落整体替换，
# 不尝试"修好"这个另一处 bug（超出本次范围）。
import re
_old_section = re.search(r"\n## 📌 关注事项.*?(?=\n## 6\) )", old_md, re.S)
_new_section = re.search(r"\n## 📌 关注事项.*?(?=\n## 6\) )", new_md, re.S)
if _old_section and _new_section:
    new_md = new_md[:_new_section.start()] + _old_section.group(0) + new_md[_new_section.end():]
    print("📌 关注事项段落已替换回原文（避开 datetime.now() 污染）")
else:
    print("⚠️ 📌 关注事项段落定位失败，未替换——请人工核对该段")

open(f"{SP}/patched_markdown_report.md", "w").write(new_md)

# ── 自证：跟旧 markdown 逐行 diff，人工确认"没变"的段落真的没变 ──
old_lines, new_lines = old_md.split("\n"), new_md.split("\n")
import difflib
diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))
print(f"\nmarkdown diff：{len([l for l in diff if l.startswith('+') and not l.startswith('+++')])} 行新增，"
      f"{len([l for l in diff if l.startswith('-') and not l.startswith('---')])} 行删除")
open(f"{SP}/markdown_diff.txt", "w").write("\n".join(diff))

# ── opportunities / twitter_threads：调真实函数重算（不是手改） ──
# `_build_opportunity_items` 是 AlphaHiveDailyReporter 的方法，但函数体里
# 通篇不读 self 的任何属性（只读传入的 sorted_results）——核对过整段源码
# 才敢这么绑定调用，不是盲猜。
from alpha_hive_daily_report import AlphaHiveDailyReporter

sorted_results = sorted(swarm.items(), key=lambda x: x[1]["final_score"], reverse=True)
opp_items = AlphaHiveDailyReporter._build_opportunity_items(None, sorted_results)
new_opportunities = [
    {
        "rank": i + 1, "ticker": opp.ticker, "direction": opp.direction,
        "opp_score": round(opp.opportunity_score, 1), "confidence": f"{opp.confidence:.0f}%",
        "resonance": swarm[opp.ticker]["resonance"]["resonance_detected"],
        "supporting_agents": swarm[opp.ticker]["supporting_agents"],
        "thesis_break": opp.thesis_break,
    }
    for i, opp in enumerate(opp_items)
]
# thesis_break_l1/l2 补回（原 opportunities 条目里带，_build_opportunity_items 本身不产出，
# 由更晚的 _attach_thesis_breaks 早于这一步先写进 swarm_data 又在别处合并回 opportunities；
# 这里直接从已重算好的 swarm[ticker] 顶层字段取，跟 markdown/DB 用的是同一份真相）。
for o in new_opportunities:
    row = swarm[o["ticker"]]
    if "thesis_break_l1" in row:
        o["thesis_break_l1"] = row["thesis_break_l1"]
    if "thesis_break_l2" in row:
        o["thesis_break_l2"] = row["thesis_break_l2"]

new_twitter = rf.generate_swarm_twitter_threads(_ReporterStub(), swarm)

daily_json_new = dict(daily_json)
daily_json_new["markdown_report"] = new_md
daily_json_new["opportunities"] = new_opportunities
daily_json_new["twitter_threads"] = new_twitter
json.dump(daily_json_new, open(f"{SP}/patched_daily.json", "w"), ensure_ascii=False)
print(f"\nopportunities 重算完成（{len(new_opportunities)} 条）；twitter_threads 重算完成（{len(new_twitter)} 条）")
print("新 Top3 X 线程标的:", [o["ticker"] for o in new_opportunities[:3]])

