#!/usr/bin/env python3
"""
🐝 Alpha Hive — IC 重跑就绪度 (v0.44.4)
========================================
回答一个问题：**v0.44.1~0.44.3 那批修复之后，攒够样本可以重跑 IC 了吗？**

为什么需要它
------------
v0.44.1~0.44.3 修了 ML 预期收益的结构性看多偏斜、并把 RivalBee 的三个硬编码特征
接上真实数据。**接线正确性已验证（单测 + 隔离环境 E2E），但方向是否变准没有验证**
—— 那需要新样本。

问题是"等攒够"这件事**没有承载物**：它不在任何测试里（测试跑的是当下），
不在任何告警里（没有异常），全靠人记着。而按 `experiments/ic_power_report.md`
的实测，攒够要 **~25 个不重叠周**（30 只标的、|IC|=0.090、80% 功效），
折算日历时间约半年 —— **半年后没人会记得这件事。**

本工具就是那个承载物。挂在每周的只读诊断任务上（见
`~/.claude/scheduled-tasks/alpha-hive-weekly-optimizer/SKILL.md`），
每周报一次"还差多少"，够了就明说该跑什么。

为什么不用定时任务/提醒
----------------------
"到期提醒"要求事先知道日期，而这里的到期条件是**数据条件**（攒够不重叠周），
它取决于扫描连续性 —— 而扫描覆盖率实测只有 36.7%，日历时间和样本进度根本不成比例。
所以判据必须读库算，不能拍一个日期。

⚠️ 世代边界（`_COHORT_HISTORY`）是本工具的核心前提：
**任何再次改动 `expected_returns` / `predict_probability` / RivalBee 特征来源的
改动，都必须往 `_COHORT_HISTORY` 追加一条**，否则新旧口径样本会被混算，
而这种混算是静默的 —— 数字照出，只是没有意义。

用法
----
    /usr/local/bin/python3 ic_rerun_readiness.py
    /usr/local/bin/python3 ic_rerun_readiness.py --json
    /usr/local/bin/python3 ic_rerun_readiness.py --target-ic 0.135   # 只想检出更强的信号

退出码
------
    0 = 已就绪（该重跑 IC 了）
    1 = 未就绪（正常状态，继续攒）
    3 = 无法判定（找不到库等）
        ⚠️ 3 而非 2：编排器 `run_step()` 把 2 保留给"脚本不存在"。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Optional, Set

ALPHAHIVE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ALPHAHIVE_DIR))

DB_PATH = ALPHAHIVE_DIR / "pheromone.db"

# ── 样本世代边界 ────────────────────────────────────────────────────────────
# 每条 = (首个受影响的业务日, 版本, 改了什么)。**只追加，不改写**（审计轨迹）。
# 判据取最后一条：它之前的样本与现在的口径不可比。
#
# ⚠️ 再次改动 expected_returns / predict_probability / RivalBee 特征来源时
# **必须追加一条**。漏了会让新旧口径样本被混算，而混算是静默的。
_COHORT_HISTORY = [
    ("2026-08-17", "v0.44.1~0.44.3",
     "expected_returns 去偏 + probability 居中 + RivalBee 三特征接真实数据"),
    ("2026-08-26", "v0.45.30",
     "拥挤度口径变更：删除 polymarket_volatility（原 15% 权重，实测 76% 为常数 20、"
     "其余变化来自 |momentum_5d|*0.8 的动量伪装，属常数稀释+暗中双计），"
     "其余五项按原比例重归一化；缺失分量改为在现存分量间重归一化而非按 0 计。"
     "拥挤度 → ScoutBee signal 维度 → final_score，故为世代边界"),
    ("2026-08-27", "v0.45.50",
     "Phase 1 静默降级批量修复（**一次性合并**，避免分批各消耗一次世代边界）："
     "① 拥挤度全分量不可得时返回 None 而非 0.0 —— 旧行为 score=20.59 →「低拥挤」"
     "→ adjustment_factor **1.2 加分**，即数据缺失被当成利好；三只消费蜂改走中性。"
     "② 训练集剔除维度缺失样本（此前补 5.0 派生出整行自洽的假样本；实测损失 3.1%）。"
     "③ σ / 0DTE 两处守卫解除上游架空：risk_engine 不再 `or 30.0`，"
     "advanced_analyzer 与 options_analyzer 不再把真实 0DTE 用 `or 30` 改写成 30 天"
     "（T 差 60 倍，恰在 gamma 最大的到期日）。"
     "④ STRONG BUY / BUY 评级闸不再因 rr 缺失而通过（旧默认值 2.0/1.5 恰好等于两档阈值）。"
     "⑤ 论点失效闸 schema 迁移 —— 此前配置全是人读散文、求值器要机器可比字段，"
     "**从未触发过一次**；按全历史 1191 观测的分位重新校准后触发率 6.35%。"
     "⑥ paper_portfolio alpha 不再等于组合收益本身；信息素坏值由 1.0 最大值改 0.5 中位。"
     "以上均改变 final_score / dimension_scores / 训练样本，故为世代边界"),
    ("2026-09-05", "v0.45.128",
     "① Oracle 期限结构 / 25Δ skew 的 IV 来源由 yfinance 期权链换为 OptionsAgent 的 CBOE 结果"
     "（档位与分值不变，只换源）。2026-09-04 全量核对：yfinance 近月 ATM IV 中位 15.6%、"
     "9/29 只 <8%（NVDA 4.44% vs CBOE 33.4%），skew 系统性偏高（~1.2 vs ~1.0）→ "
     "options_score Δ 中位 +0.30、范围 [-0.5, +1.7]、21/30 非零。"
     "② Bear 估值项 P/E 复活：fast_info.pe_ratio 在 yfinance 1.2.0 恒 None，该项自 v0.45.54 "
     "起 496 条零命中；改读 .info trailingPE，档位 35/50/80 不变，09-05 实测 6/30 只命中。"
     "两项均改变 options_score / Bear 分 → final_score，故为世代边界。"
     "边界取部署日 2026-09-05（下一次定时扫描 2026-09-08，09-07 Labor Day 休市）：任何自此起的扫描"
     "都是新口径，含手动补跑——边界若写成 09-08，中间手动跑出的样本会混进旧世代"),
    ("2026-09-07", "v0.45.151",
     "RivalBee 的 catalyst_quality 来源修复，两处："
     "① `PheromoneBoard` 新增不受 MAX_ENTRIES 溢出淘汰影响的定点索引，`_read_peer` 改走它——"
     "旧行为下 `_entries` 溢出按 `nlargest(80, key=(self_score,...))` 截断、**先扔分最低的**，"
     "而 MAX_ENTRIES=80 是按注释里「9 只标的」的年代定的、watchlist 现为 30 只（一轮约 210 条），"
     "于是 ChronosBee「无近期催化剂」恒落的 4.0 在 RivalBee（Phase-1.4）读到之前就被挤出去了。"
     "② 真读不到时 `catalyst_quality` 由众数哨兵 \"B\" 改为 `None`（→ `_encode_catalyst` → NaN）。"
     "实测（当前世代 188 份生产 JSON，用 `expected_returns` 闭式反解出实际用过的等级）："
     "真值≠\"B\" 的 39 份里 27 份被记成 \"B\"，流向**全部**是 C→B，即 magnitude 0.7 当成 0.9。"
     "改前/改后全量对照 n=173：**probability 变化 0/173 ⇒ final_score / ml_adjustment 位移恒为 0**，"
     "`expected_30d` 变化 24/173、最大 |Δ| 6.13 百分点。"
     "⚠️ 之所以 final_score 位移为 0 仍登记边界：`ml.expected_7d` / `ml.expected_30d` 进 "
     "`signal_archive`（本工具收尾推荐的 `signal_archive.py --analyze` 正是分析它们），"
     "且 Δ=0 是**当前 HGB 模型**的性质（catalyst 置换重要度 0.0、B/C 落同一叶）而非定义性质——"
     "降级链上的 SimpleMLModel 给 catalyst 0.25 权重，同一改动在它上面 Δ≠0。定义变了就登记。"
     "边界取部署日 2026-09-07（上一条边界 09-05 至今 `predictions` 内 0 条样本，"
     "下一次定时扫描 09-08）⇒ 本次追加**不作废任何已累积样本**"),
    ("2026-09-07", "v0.45.156",
     "`PheromoneBoard.detect_resonance` 改读不受 MAX_ENTRIES 溢出淘汰影响的定点视图"
     "（`_live_agent_entries`，复用 v0.45.151 的 `_latest_by_agent`）。"
     "旧行为：共振直读 `_entries`，而它溢出时按 `nlargest(80, key=(self_score,...))` "
     "截断、**先扔分最低的**；MAX_ENTRIES=80 是按「9 只标的」的年代定的，"
     "watchlist 现 30 只、一轮满编 9 条/标的 ≈ 270 条。"
     "实测（生产 `pheromone_compact` 即共振同一次 distill 内的板快照，"
     "712/725 = 98.21% 逐字段复现生产 `resonance`，另有两条独立自证）："
     "当前世代 191 份丢 628/1719 = 36.53% 条目，且**缺失与方向强相关**——"
     "bearish 丢 50.7% vs bullish 丢 12.6%（4.0×）⇒ "
     "共振方向翻转 **29/191 = 15.2%**，`resonance_detected` 翻转 **40/191 = 20.9%**"
     "（生产判共振 81 次、真值 61 次），`consistency` 被高估 59.2%（均值 +0.301），"
     "其中 **39/191 = 20.4%** 被记成 1.0「完全一致」而那轮其实有条目被挤掉。"
     "共振同时驱动两处 `final_score`：① GuardBeeSentinel 的 risk_adj 维分"
     "（`7.0 + consistency*2.0` vs `avg_score*0.8`，其自身那次共振判定实测翻转 13.1%）、"
     "② QueenDistiller 的 `confidence_boost`（`rule_score = adjusted_score*(1+boost/100)`）。"
     "**MAX_ENTRIES 的值未动**（改它会一次性改变每个板消费方）；"
     "`get_top_signals` / `snapshot` / `compact_snapshot` 仍受截断，待独立测量"
     "（其中 GuardBee 那条通道已由下一条边界 v0.45.163 量测并处理）。"
     "边界取部署日 2026-09-07：上一条边界（同日 v0.45.151）至今 `predictions` 内 "
     "**0 条样本**（`ic_rerun_readiness` 输出「世代内还没有扫描产出」），"
     "下一次定时扫描 09-08 ⇒ 本次追加**不作废任何已累积样本**，"
     "且不新开空分区（与 v0.45.151 同日，**扩展**该标签而非另起）"),
    ("2026-09-07", "v0.45.163",
     "GuardBeeSentinel 的 risk_adj 维分改走**普查**读法 `PheromoneBoard.get_live_signals`，"
     "不再用 `get_top_signals(ticker, n=5)`。旧行为把**排行榜**当**普查**用："
     "`avg_score`（窗口均分）与 `consistency`（`max(bull,bear)/total`）两个量都从那个 n=5 "
     "窗口导出，而它们决定 risk_adj（共振时 `7.0+consistency*2.0`，否则 `avg_score*0.8*adj_factor`）；"
     "`dimension_scores.risk_adj == agent_details.GuardBeeSentinel.score` 实测 193/193 逐份相等，"
     "risk_adj 权重实测 ≈0.20 ⇒ 直接进 `final_score`。"
     "两个缺陷叠加：① `_entries` 溢出按 `nlargest(80, key=(self_score,...))` **先扔分最低的** "
     "⇒ 幸存者均值按构造偏高；② `n=5` 本身——`alpha_hive_daily_report.py:442` 的顺序契约让 "
     "Guard 之前恒有 6 只蜂发布，窗口 **191/191 = 100%** 装不下。"
     "仪器：`details.top_signals_count` 是生产自己写的直接观测量（`guard_bee.py:214`），"
     "用它 + consistency + adjustment_factor + 落盘的全部下游修正逐份重算 GuardBee.score，"
     "**191/191 命中**；第二条自证 `agent_details[蜂].score` vs `pheromone_compact.s` "
     "780 对上 / 16 对不上，16 条**全部**是 CodeExec 占位顶掉真实结论的同一形状 ⇒ 0 条无法解释。"
     "实测（当前世代 191 份）：窗口取不满 5 条 **91/191 = 47.6%**（旧世代 ≤16 只标的时 1.8%）；"
     "按窗口条数分层 Δrisk_adj 单调 —— n=1 **−1.480** / n=2 −1.090 / n=3 −0.804 / n=4 −0.565 / "
     "n=5 −0.124（填满仍 70/100 偏高）；合计 risk_adj **185/191 = 96.9%** 与真值不同，"
     "Δ 均值 **−0.485**（生产偏高 160 / 偏低 25），|Δ|≥1.0 有 27 份、≥2.0 有 3 份；"
     "GuardBee 方向 **50/191 = 26.2%** 不一致，bearish 32→**63**；"
     "`consistency` 被高估 67.5%（均值 +0.116），生产记成 1.0「完全一致」的 35 份**无一为真**。"
     "传导到 final_score（Δrisk_adj × risk_adj 权重）：|Δ| 均值 0.089，67/191 ≥0.1，6 份 ≥0.3，最大 0.404。"
     "同源子通道一并修好（无需另改）：`guard_bee.py:52` 把窗口里的 `bull` 覆盖进 "
     "`real_metrics[\"bullish_agents\"]`，而 `crowding_detector.py:84` 算 `bullish_agents/6*100` —— "
     "分母写死 6、分子被窗口截断，改普查后分子回到真值（测试实测变异前后 1 → 2）。"
     "⚠️ `details.top_signals_count` 的**语义**在本边界改变（排行榜窗口条数 ≤5 → 本轮发布过的蜂数），"
     "键名保留（`details.top_signals_count` 不改名），新增相邻的 `details.census_source` "
     "（`live_agent_view` / `top_signals_fallback` / `unavailable`）供机读区分两段口径。"
     "⚠️ **v0.45.182 更正**：本条当初写的「键名保留以免断掉 `signal_archive` 的时间序列，"
     "口径由 `census_source` 机读区分」——**后半句在归档里不成立**。`signal_archive` 只抽 "
     "`guard.top_signals_count`，从不抽 `census_source`（实测归档里 0 行），而 `analyze()` "
     "既不按日期也不按本表切片 ⇒ 两段定义被池化，时间序列不是「保住了」而是被**静默重新定义**了"
     "（逐扫描日实测 count 3.43~4.33 → 恒 6.00、consistency 0.50~0.72 → 0.47~0.49）。"
     "v0.45.182 改为**改名**：`guard.consistency` → `guard.consistency_census`，"
     "`guard.top_signals_count` 摘除并改挂分布不变式。判据：判别器要放在做聚合的那一层，"
     "不是产生数据的那一层。"
     "**MAX_ENTRIES 的值仍未动**；`get_top_signals` 的排行榜语义未动（另 3 个调用方各自待测）；"
     "`snapshot` / `compact_snapshot` 仍受截断——其中 `alpha_hive_daily_report.py:1171` 的 "
     "`agent_votes` 实测当前世代 300 份**只有 1 份**凑齐 8 只蜂（中位 3 只，BearBee 缺席 97.3%），"
     "但它只进逐蜂归因（weekly_optimizer / self_analyst / feedback_loop）**不进 final_score**，"
     "且成因里混着 3600s 墙钟过期未分离，故不在本边界内、另行处理。"
     "边界取部署日 2026-09-07：上两条边界（同日 v0.45.151 / v0.45.156）至今 `predictions` 内 "
     "**0 条样本**（本次改动前实跑 `ic_rerun_readiness.py` 确认「世代内还没有扫描产出」），"
     "下一次定时扫描 09-08 ⇒ 本次追加**不作废任何已累积样本**，且不新开空分区"
     "（与前两条同日，**扩展**该标签而非另起）"),
    ("2026-09-09", "v0.45.172",
     "`config.EVALUATION_WEIGHTS` 改写（用户明确决策，非 weekly_optimizer 自动写入）："
     "signal / risk_adj 两维归零，catalyst/sentiment/odds 按原比例重归一化 "
     "（0.1878/0.1838/0.1940 → 0.3320/0.3250/0.3430）。依据 "
     "`experiments/final_score_dilution_report.md`：干净口径下这两维 IC 为负"
     "（-0.088 / -0.084）且合计占旧权重 43.4%，抵消掉唯一方向一致的 sentiment"
     "（IC +0.168）；采用的是报告里已实测过的「仅剔除负向维度」反事实（IC +0.106，"
     "t=+1.23, p=0.219），不是另发明未测过的分配。"
     "⚠️ 证据强度如实记录：sentiment 的 p=0.012 未过 Bonferroni 校正（5 维校正后 "
     "p≈0.06），两反向维自身负 IC 也不显著（p=0.29 / 0.11）；报告第 6 节原文标题"
     "是「不建议现在改权重」，本次是用户在看过完整证据后的主动覆盖，不代表已达到"
     "项目一贯要求的显著性门槛。"
     "权重是纯聚合层改动（不改任何维度自身怎么算），可用 `replay_scoring.py` 对"
     "已累积样本离线验证，无需再等前向累积。"
     "⚠️ 边界代价：`predictions` 内 2026-09-08 已有 **30 条**样本在旧权重下产生"
     "（v0.45.163 世代内唯一一天的扫描产出），本次边界作废它们——与此前几条"
     "「0 条样本、不作废」不同，这次是真实成本，如实记录不美化。"
     "边界取部署日 2026-09-09（下一次定时扫描同日 14:00）⇒ 此刻起的任何扫描"
     "（含手动补跑）均为新权重口径"),
    ("2026-09-10", "v0.45.176",
     "⚠️ **上一条（09-09 / v0.45.172）的最后一句不成立，权重改动实际落在今天。** "
     "本条不改写上一条（审计轨迹只追加），在此如实记录：09-09 那天的 30 条样本"
     "**不是**用 config 权重打的分。原因是 `Backtester.adapt_weights()` 这条"
     "此前未被任何文档记录的通道 —— 它按 `smoothed = 0.2×config + 0.8×学习值` "
     "混合后整体顶掉 config，再经 ML 反馈乘法与政体调整逐标的改写。"
     "09-09 实测生效权重（`.swarm_results` 的 `dimension_weights`，30 只全带）"
     "signal≈0.207 / risk_adj≈0.198，config 里这两维明明已归零；"
     "落库的 `adapted_weights` 行是 0.141/0.182（config 只兑现了 **20%**），"
     "生产实际比它还高是因为 ML 反馈层又把 signal 乘了回去。"
     "v0.45.176 断开该通道（`adapt_weights` 降级为只读诊断，照 v0.44.0 处置 "
     "`weekly_optimizer` 的先例），并修掉 `gex_regime.RegimeWeightAdjuster` 的 "
     "`max(0.02, ·)` 地板（相对偏移下 w=0 恒不动，但地板把零复活成 2%，"
     "即哪怕修好上游、决策也只能兑现 ~94%）。"
     "⚠️ 边界代价：作废 09-09 的 **30 条**样本（本代唯一一天的产出）。"
     "为什么仍要加 —— 世代边界看的是**数据**不是意图：09-09 的分是 signal≈0.207 打的、"
     "09-10 起是 0，两者不可比。**不要**把本条读成「上一条白加了」："
     "09-09 权重确实变了（signal 0.28→0.207，那是 20% 兑现的部分），只是没变成它声称的值。"
     "⚠️ 同版另修 `replay_scoring.py::evaluate` 的池化口径 —— 它此前把跨日期的行"
     "摊平成一个大 Spearman，把 risk_adj 算成 **+0.047**（横截面口径是 −0.060，符号相反）。"
     "本表上一条的依据之一正是 risk_adj 的负 IC，故**用 v0.45.176 之前的 "
     "`replay_scoring` 复核过的任何聚合层结论都需要重跑**。"),
    ("2026-09-11", "v0.45.197",
     "Dealer GEX 换取数视图：由与 IV/skew/期限结构共用的截断主链"
     "（`cboe_options._select_expiries`，DTE≥7 的前 4 个到期日，且那个 DTE 因 "
     "`today` 带时分秒而恒少一天 ⇒ 实为「≥8 个日历日」）改为同一份 CBOE payload 的"
     "**全到期日视图**（`fetch_cboe_chain_for_gex`，日历口径、未到期全要、上限 24）。"
     "⚠️ 这是**换数据源**不是聚合层改动 —— 历史上没存过近月合约的 gamma/OI，"
     "`replay_scoring` 离线重放**做不到**，只能前向累积。"
     "依据：26 只标的实测「取最近 K 个到期日捕获的 net GEX 占全链百分比」"
     "K=4 → 中位 63.9%/最低 **−73.3%**（22/26 只 <90%）；K=8 → 73.6%/−29.8%；"
     "K=12 → 96.3%/20.6%；K=16 → 100%/72.3%；K=24 → 100%/99.4%。"
     "负百分比 = 部分和与全链**符号相反** ⇒ `total_gex` 只有在全链上才良定义，"
     "旧口径不是「偏小」而是**符号不可靠**。2026-09-11 端到端实测 27 只可比标的"
     "符号翻转 2 只（CRM neg→pos、NEE pos→neg），量级 NVDA 11.52→535.90。"
     "⚠️ 影响面：GEX 只经 `gex_regime.RegimeWeightAdjuster` 的三值 `regime` 进评分，"
     "249 条归档反事实测得翻转一次的 `|Δfinal_score|` 中位 0.050 / 最大 0.127、"
     "跨决策阈值 2/249 —— **不要把本条读成「评分会变好」**，它修的是「这个数等于"
     "它声称的东西」。代价：CBOE 当日陈旧的标的 GEX 变为不可得（不回退截断链，"
     "照 v0.45.188 `_calc_max_pain` 的先例），`regime=\"unknown\"` ⇒ 不做权重偏移，"
     "是安全降级；频次见 `scan_timing.counters().gex_view`。"
     "⚠️ **本条日期是写入时的预判，不是已核实的事实。** 写入时生产 checkout "
     "（`~/Desktop/Alpha Hive`）尚未合入本次改动，而它不自动 pull ⇒ 首个真正受影响的"
     "业务日可能晚于 2026-09-11。本表只追加不改写，若实测更晚**请追加一条更正**"
     "（先例：v0.45.176 就是这么更正 v0.45.172 的）。"
     "判别方法已做成可执行的：`cohort_boundary_evidence()` 读 `analysis-*-ml-*.json` 的 "
     "`advanced_analysis.dealer_gex.chain_view`，报出首次出现 `cboe_full_expiries` 的日期，"
     "CLI 每次都会印一行。加这个是因为本仓记过同一处栽跟头 —— 此前几条边界"
     "「核过了」其实零判别力：世代内 0 条样本时，日期写对写错的输出一模一样。"),
]

# 达到 80% 功效所需的不重叠周数（30 只标的口径，实测见 experiments/ic_power_report.md）
# key = 真实 |IC|，value = 所需不重叠周数
_WEEKS_REQUIRED = {
    0.050: 82,
    0.077: 35,    # 噪音地板
    0.090: 25,    # 系统综合分实测 —— 默认判据
    0.135: 11,    # 20 日动量基准
    0.200: 5,
}
DEFAULT_TARGET_IC = 0.090

# 标的池漂移容忍：与世代内首批扫描相比，当前池新增标的占比超过此值即视为
# 世代被打断（与 weekly_optimizer.check_ticker_pool_consistency 同一思路）
MAX_POOL_DRIFT = 0.20


def cohort_start() -> Dict:
    """当前有效的世代边界。"""
    date, version, reason = _COHORT_HISTORY[-1]
    return {"date": date, "version": version, "reason": reason,
             "n_generations": len(_COHORT_HISTORY)}


def _iso_weeks(dates) -> Set:
    out = set()
    for d in dates:
        try:
            out.add(dt.date.fromisoformat(str(d)[:10]).isocalendar()[:2])
        except (ValueError, TypeError):
            continue
    return out


def assess(db_path: Path = DB_PATH, target_ic: float = DEFAULT_TARGET_IC,
           max_pool_drift: float = MAX_POOL_DRIFT,
           today: Optional[str] = None) -> Dict:
    """就绪度判定。纯函数，便于测试。"""
    cohort = cohort_start()
    boundary = cohort["date"]
    required = _WEEKS_REQUIRED.get(
        target_ic, _WEEKS_REQUIRED[DEFAULT_TARGET_IC])

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # 世代内**已回填 T+7** 的样本 —— 只有这些能进 IC 计算
        ripe = con.execute(
            "SELECT date, ticker FROM predictions "
            "WHERE date >= ? AND checked_t7 = 1 AND price_t7 IS NOT NULL "
            "  AND price_at_predict > 0",
            (boundary,),
        ).fetchall()
        # 世代内**全部**样本（含未到期）—— 用于看进度与池漂移
        allrows = con.execute(
            "SELECT date, ticker FROM predictions WHERE date >= ?",
            (boundary,),
        ).fetchall()
    finally:
        con.close()

    ripe_dates = [r[0] for r in ripe]
    weeks_ripe = _iso_weeks(ripe_dates)
    weeks_scanned = _iso_weeks(r[0] for r in allrows)

    # 池漂移：世代内最早 3 个扫描日的标的集合 vs 最近 3 个
    by_date: Dict[str, Set[str]] = {}
    for d, t in allrows:
        by_date.setdefault(str(d)[:10], set()).add(t)
    dates_sorted = sorted(by_date)
    pool_drift, pool_note = 0.0, None
    if len(dates_sorted) >= 2:
        early: Set[str] = set()
        for d in dates_sorted[:3]:
            early |= by_date[d]
        late: Set[str] = set()
        for d in dates_sorted[-3:]:
            late |= by_date[d]
        if late:
            pool_drift = len(late - early) / len(late)
            if pool_drift > max_pool_drift:
                pool_note = (
                    f"当前池 {len(late)} 只里有 {len(late - early)} 只"
                    f"（{pool_drift:.0%}）在世代开始时不在池中 —— "
                    f"样本世代已被标的池变动打断，需重设世代边界"
                )

    n_weeks = len(weeks_ripe)
    ready = n_weeks >= required and pool_note is None

    # ETA：按世代内的实际周产出速度外推（不是按日历）
    today_d = dt.date.fromisoformat(today) if today else dt.date.today()
    boundary_d = dt.date.fromisoformat(boundary)
    calendar_weeks = max(1, ((today_d - boundary_d).days // 7) or 1)
    # 已扫描的周 / 已过的日历周 = 有效产出率（T+7 到期滞后不算在内）
    weeks_rate = len(weeks_scanned) / calendar_weeks if calendar_weeks else 0.0
    remaining = max(0, required - n_weeks)
    if weeks_rate > 0:
        eta_cal_weeks = remaining / weeks_rate
        eta_date = (today_d + dt.timedelta(weeks=eta_cal_weeks)).isoformat()
    else:
        eta_cal_weeks, eta_date = float("inf"), None

    return {
        "cohort": cohort,
        "target_ic": target_ic,
        "weeks_required": required,
        "weeks_accrued": n_weeks,
        "weeks_remaining": remaining,
        "n_ripe_samples": len(ripe),
        "n_all_samples": len(allrows),
        "scan_weeks_in_cohort": len(weeks_scanned),
        "calendar_weeks_elapsed": calendar_weeks,
        "weeks_per_calendar_week": round(weeks_rate, 3),
        "eta_calendar_weeks": (None if eta_cal_weeks == float("inf")
                               else round(eta_cal_weeks, 1)),
        "eta_date": eta_date,
        "pool_drift": round(pool_drift, 4),
        "pool_note": pool_note,
        "ready": ready,
        "next_step": (
            "/usr/local/bin/python3 experiments/ml_expected_return_replay.py "
            "&& /usr/local/bin/python3 signal_archive.py --analyze"
        ),
    }


def summary_line(res: Dict) -> str:
    """一行摘要，供周度任务直接引用。"""
    if res["pool_note"]:
        return f"⚠️ IC 重跑就绪度：世代已被打断 —— {res['pool_note']}"
    if res["ready"]:
        return (f"✅ IC 重跑已就绪：世代内已攒 {res['weeks_accrued']}/"
                f"{res['weeks_required']} 个不重叠周（{res['n_ripe_samples']} 条已回填样本）"
                f"，该重跑了")
    eta = (f"，按当前节奏约 {res['eta_calendar_weeks']} 个日历周后到位"
           f"（≈{res['eta_date']}）" if res["eta_date"] else
           "，但世代内还没有扫描产出 —— 先看扫描连续性")
    return (f"⏳ IC 重跑未就绪：{res['weeks_accrued']}/{res['weeks_required']} "
            f"个不重叠周{eta}")


def cohort_boundary_evidence(home: Path) -> dict:
    """边界日期写对没写对，从**数据**上回答，而不是从意图上。

    v0.45.197 的口径切换在归档里留了个可判别的印记：
    `analysis-<TK>-ml-<DATE>.json` 的 `advanced_analysis.dealer_gex.chain_view`
    自该口径起为 `"cboe_full_expiries"`，此前的记录没有这个键。
    本函数报出它**首次出现**的日期，与 `_COHORT_HISTORY` 最后一条的日期比对。

    ⚠️ 为什么要有这个：本仓记过同一处栽跟头 —— 此前几条世代边界「核过了」其实
    **零判别力**：世代内 0 条样本时，日期写对和写错的输出一模一样
    （见 auto-memory `alpha-hive-failure-propagation` 的「安全性论证与可观测性是
    同一个事实的两面」）。所以判据必须挂在一个**新旧可区分**的印记上。

    返回 `{"marker_first_seen": 日期或 None, "boundary": 边界日期,
           "verdict": "matches"|"boundary_too_early"|"boundary_too_late"|"no_evidence_yet"}`。
    取不到归档时返回 `no_evidence_yet` —— **不返回 "matches"**，
    「还没有证据」和「证据说对了」必须可区分。

    ⚠️ `home` **是必传的，不给默认值**，这是初版的 bug 改出来的：初版默认
    `ALPHAHIVE_DIR`（= 代码所在目录），而归档是**数据**、在生产目录里。
    在 worktree 里跑它永远扫到一个没有归档的目录 ⇒ 恒返回 `no_evidence_yet` ⇒
    **一个永远说「还没证据」的判别器，和没有判别器是一回事** —— 正是本函数
    要防的那种失效。调用方（`main()`）传 `--db` 所在目录，与库同一个安装。
    """
    root = Path(home)
    boundary = _COHORT_HISTORY[-1][0]
    first = None
    try:
        for f in sorted(root.glob("analysis-*-ml-*.json")):
            m = re.search(r"-ml-(\d{4}-\d{2}-\d{2})\.json$", f.name)
            if not m:
                continue
            date = m.group(1)
            if first is not None and date >= first:
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    d = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            view = ((d.get("advanced_analysis") or {}).get("dealer_gex") or {}).get("chain_view")
            if view == "cboe_full_expiries":
                first = date if first is None else min(first, date)
    except OSError:
        first = None

    if first is None:
        verdict = "no_evidence_yet"
    elif first == boundary:
        verdict = "matches"
    elif first > boundary:
        verdict = "boundary_too_early"      # 边界之后、印记之前的样本是旧口径 ⇒ 会被混算
    else:
        verdict = "boundary_too_late"       # 边界之前就已是新口径 ⇒ 白白丢掉一些新样本
    return {"marker_first_seen": first, "boundary": boundary, "verdict": verdict}


_BOUNDARY_VERDICT_TEXT = {
    "matches": "✅ 与归档印记一致",
    "boundary_too_early": "🚨 边界写早了 —— 边界至印记之间的样本是旧口径，会被混算，请追加一条更正",
    "boundary_too_late": "⚠️ 边界写晚了 —— 印记之前已是新口径，白丢了一些新样本",
    "no_evidence_yet": "⏳ 归档里还没有该口径的印记（生产尚未跑到这一版，或归档不可读）",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="IC 重跑就绪度")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--target-ic", type=float, default=DEFAULT_TARGET_IC,
                    choices=sorted(_WEEKS_REQUIRED),
                    help=f"要检出的真实 |IC|（默认 {DEFAULT_TARGET_IC}=系统综合分实测）")
    ap.add_argument("--today", help="覆盖今天的日期（测试用，YYYY-MM-DD）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help=(
        "把 JSON 结果写到该文件（供编排器读）。"
        "刻意提供此参数而不让调用方重定向 stdout："
        "编排器的 log() 用 `tee -a` **会写 stdout**，"
        "`> file` 捕获会在脚本缺失/权限被拒等路径上把日志行混进 JSON。"
        "（与 scan_continuity.py 同一理由）"
    ))
    ap.add_argument("--quiet", action="store_true", help="只输出一行摘要")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"❌ 找不到 {db} —— 无法判定", file=sys.stderr)
        return 3

    res = assess(db_path=db, target_ic=args.target_ic, today=args.today)

    if args.out:
        try:
            Path(args.out).write_text(
                json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            # 写不出去不改变判定 —— 判定在写盘之前就完成了
            print(f"⚠️  无法写入 {args.out}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res["ready"] else 1
    if args.quiet:
        print(summary_line(res))
        return 0 if res["ready"] else 1

    c = res["cohort"]
    print("━" * 72)
    print("🐝 Alpha Hive · IC 重跑就绪度")
    print("━" * 72)
    print(f"  样本世代: 自 {c['date']} 起（{c['version']}）")
    print(f"            {c['reason']}")
    print(f"            世代总数 {c['n_generations']}（只追加，不改写）")
    print()
    print(f"  判据: 检出 |IC|={res['target_ic']:.3f} 需 "
          f"**{res['weeks_required']} 个不重叠周**（80% 功效，30 只标的口径）")
    print("        来源 experiments/ic_power_report.md")
    print()
    print(f"  世代内已回填 T+7 样本: {res['n_ripe_samples']} 条"
          f"（世代内总样本 {res['n_all_samples']} 条，其余未到期）")
    print(f"  已攒不重叠周:          {res['weeks_accrued']} / "
          f"{res['weeks_required']}   还差 {res['weeks_remaining']}")
    # 归档与 DB 同处一个安装 ⇒ 用 --db 的所在目录，别用代码目录（见函数 docstring）
    _ev = cohort_boundary_evidence(db.parent)
    print(f"  边界日期的数据证据:    {_BOUNDARY_VERDICT_TEXT[_ev['verdict']]}"
          + (f"（印记首见 {_ev['marker_first_seen']}）" if _ev["marker_first_seen"] else ""))
    print(f"  世代内有扫描的周:      {res['scan_weeks_in_cohort']}"
          f"（已过 {res['calendar_weeks_elapsed']} 个日历周，"
          f"产出率 {res['weeks_per_calendar_week']:.2f} 周/周）")
    if res["eta_date"]:
        print(f"  按当前节奏预计到位:    ≈{res['eta_date']}"
              f"（{res['eta_calendar_weeks']} 个日历周后）")
    print()
    if res["pool_note"]:
        print(f"  ⚠️ {res['pool_note']}")
        print()
    print("━" * 72)
    print(summary_line(res))
    if res["ready"]:
        print()
        print("  该跑:")
        print(f"    {res['next_step']}")
    print("━" * 72)
    return 0 if res["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
