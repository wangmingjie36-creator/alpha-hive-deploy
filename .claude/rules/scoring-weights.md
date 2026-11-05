---
paths:
  - "config.py"
  - "swarm_agents/**"
  - "gex_regime.py"
  - "gex_state.py"
  - "backtester.py"
  - "weekly_optimizer.py"
  - "options_analyzer.py"
  - "feedback_loop.py"
  - "ml_predictor.py"
  - "tests/test_zero_weight_invariant.py"
  - "tests/test_gex_*.py"
  - "tests/test_rival_ml_adjustment_retired.py"
  - "tests/test_queen_ml_feedback_disabled.py"
  - "tests/test_weekly_optimizer*.py"
  - "tests/test_agent_weight_manager_retired.py"
---

# 评分权重与已断开的进分通道 · 按路径加载

> 读 / 改上面 `paths:` 匹配的文件时 Claude Code 自动加载。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来：「核心组件指针」的权重优化与三条已断开的通道，和文末「评分与决策规则」的完整原文（含权重沿革）。根目录只留「勿接回」一行、五维含义与决策阈值。

## 核心组件指针里的四条

- **权重优化** `weekly_optimizer.py`（Track A）：T+7 回测 → clamp ±10pp → 原子写 config.py，审计日志 `weight_history.jsonl`
- **第三条权重通道，v0.45.176 已断开** `Backtester.adapt_weights()`：曾经每日扫描内调用后经 `QueenDistiller(adapted_weights=...)` 短路掉 `config.EVALUATION_WEIGHTS`（`0.2×config + 0.8×学习值`，即 config 只有两成投票权）。**现降级为只读诊断**，照 v0.44.0 处置 `weekly_optimizer` 的先例：继续算、继续写 `adapted_weights` 表留审计轨迹，**不再进评分**。⚠️ **不要接回去**——它学的是「谁更爱说中性」而非准头（零技能置换检验：5 只蜂 4 只技能 Δ 在 ±2.2pp 内且 CI 全跨 0），且 `max(0.05, acc**2)` 的形式结构上表达不了零权重。理由全文见 `Backtester.adapt_weights` 的 docstring；守卫 `tests/test_zero_weight_invariant.py`（AST 枚举全仓，谁传 `adapted_weights=` 谁红）。详见 auto-memory `alpha-hive-adapted-weights-bypass.md`
- **GEX 直接加减分，v0.45.334 已断开** `gex_regime.GexRegimeModifier`：曾经在 `queen_distiller` 步骤 4.5 直接给 `rule_score` ±0.8（驱动它的 flip 是逐行权价变号，54% 贴在现价 2% 内 ⇒ 系统性扣分；vanna 差 S√T 倍）。**现仅诊断**：照算、落盘 `gex_regime_mod`（`applied: False`），**不进分**。另两条读 OracleBee 主链 `gamma_exposure` 的通道 **v0.45.349 已中性化**：`options_analyzer` 的 `gex_signal` 恒 1.0、BearBee 的 `gex<0` 看空下限已删（该量是截断链上 call−put gamma×OI，实测只携带标的身份、不携带时点信息）。⇒ **规则引擎（生产 `--no-llm`）下 GEX 进评分只剩 `RegimeWeightAdjuster` 全链政体路由**（`confidence_modifier` 只缩放展示用 band_width；LLM 模式下 Oracle / Bear 的 LLM 提示仍带 GEX，未动）。守卫 `tests/test_gex_oracle_bear_neutralized.py`（含 `gamma_exposure`/`gex` 读者白名单 AST）。⚠️ **都不要接回去**；守卫 `tests/test_gex_modifier_disconnected.py`（行为 + AST）。共振加成检验的 replay 按记录的 `applied` 复现（修订 1）。**展示 / 存档读的 GEX（v0.45.362 起）只有一份**：`gex_state`（蒸馏结果 `swarm_results[tk]["gex_state"]`，就是政体路由收到的那份，带 `available`/`reason`）；网站、日报、深度 / ML 报告、MCP、`signal_archive` 的 `gex.*` 都读它，**缺了显示不可得，不回退** Oracle 主链 `gamma_exposure`（另一个量，可与之符号相反）。⚠️ Oracle 的 `gamma_squeeze_risk` 分档方向与展示分档**相反**（正 GEX ⇒ high），只剩 LLM 提示词在读；卖权选择器不共用（冻结预注册读 ≤45 天重定价视图）。理由见 `gex_state` 模块 docstring，守卫 `tests/test_gex_state.py`
- **RivalBee 的 ML 调整项，v0.45.538 已断开** `QueenDistiller._compute_weighted_score`：曾经 `final_score += (rival_score − 5)·0.1·conf·OOS 信任系数`，而 Rival 的概率出自 `ml_predictor_extended` 在 30 条 2023 年硬编码样本上训的模型（已整个退役），信任闸读的却是 `ml_model_cache.json` 真模型的 OOS ⇒ 只向上的抬分。现 Rival 只算 `ml_predictor.expected_returns`（方向不变）、分数 `5.0 + 0.5·tech` 不进分。⚠️ **不要接回**；要让 ML 进分先预注册前瞻检验。守卫 `tests/test_rival_ml_adjustment_retired.py`

## 评分与决策规则（原文）
### 候选机会综合分（Opportunity Score）
`Opportunity Score = Σ wᵢ × 维度分`，五个维度：signal / catalyst / sentiment / odds / risk_adj。

**`config.EVALUATION_WEIGHTS` 是权重的「配置意图」唯一真相，不是「生产实际生效值」唯一真相——二者自 v0.45.175 起已知不同，见下条。**（本文件此前硬写的 0.30/0.20/0.20/0.15/0.15 与 config 实际值长期不符，属「文档只存指针不存参数值」原则要治的那类陈旧误导；现在连 config 本身是否生效都不能想当然。）

⚠️ **v0.45.172（2026-09-09）起权重已改**——原诊断（干净口径下加权后净 IC≈0，两反向维度占 43% 权重抵消掉唯一有效的 sentiment，详见 `experiments/final_score_dilution_report.md`）本身**从未过 Bonferroni 校正**，报告第 6 节原文标题是「不建议现在改权重」。这次是用户在看过完整证据强度后的**主动决定**，不是证据新近达标——不要把权重已改这件事本身读成"已验证有效"。改动理由与代价的完整记录见 `config.py` 的 `EVALUATION_WEIGHTS` 上方注释与 `ic_rerun_readiness._COHORT_HISTORY` 2026-09-09 条（含它作废了哪些已累积样本）。`weekly_optimizer.py` 自身的自动写入机制**未解锁**，仍是只读诊断——本次是直接改 `config.py`，走的不是它的路径。

🚨 **v0.45.175 发现该决策从未在生产实际生效；v0.45.176（2026-09-10）已断开旁路，自此 config 才真是唯一真相。** 旁路是 `Backtester.adapt_weights()`（第三条通道，见上文「核心组件指针」），config 只拿到两成投票权；下游 `gex_regime.RegimeWeightAdjuster` 的 `max(0.02, ·)` 地板还会把零复活成 2%，两处同版修完。全链路实测（含最不利政体分支）signal=risk_adj=**0.0000**，对比 09-09 生产实际的 0.2059/0.1982。

⚠️ **仍然：判断「生产实际用了哪套权重」看 `swarm.dimension_weights`，不看 config。** 政体调整（相对偏移）依然逐标的改写权重——设计内、保零。**蜂后 ML 反馈（维度权重乘数 + 方向票重缩放）v0.45.537 已停用**：没有 OOS 闸（8/25 天模型 OOS < 50% 照改）、重要性取自训练集、按特征数取均值的形式只抬得动 sentiment/signal，且把分数已归零的 Scout 的票放大到 4 倍；现只记影子 `ml_weight_adjustments_shadow`。⚠️ **勿接回，也勿把 `ML_FEEDBACK_CONFIG` 的 enable_* 改成 False**（共振加成检验重放旧记录靠它们）。守卫 `tests/test_queen_ml_feedback_disabled.py`。扫描期观测点：`_assert_config_zeros_survive`（零维非零）+ `_assert_base_weights_equal_config`（基准权重逐维 = config，v0.45.537），不变式守卫在 `tests/test_zero_weight_invariant.py`。世代边界 2026-09-10 / v0.45.176（作废 09-09 的 30 条样本）。

说明：
- Signal: 披露与基本面共振强度
- Catalyst: 未来 1~8 周可触发事件清晰度
- Sentiment: 舆情方向与动量质量
- Odds: 市场赔率是否存在错配
- RiskAdj: 回撤风险、流动性与拥挤度调整

决策阈值（默认）：
- `>= 7.5`：高优先级，进入主简报
- `6.0 ~ 7.4`：观察名单，需补证据
- `< 6.0`：不行动，仅归档
