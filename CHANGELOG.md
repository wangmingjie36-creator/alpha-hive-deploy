# Alpha Hive · 版本变更历史

> 格式：每次 Cowork session 结束后追加一条记录。
> 规范：`Added` 新增 | `Changed` 修改 | `Fixed` Bug 修复 | `Removed` 删除

---

## [0.23.3] — 2026-04-17 — sample-accumulator 改周日 18:01（减少 entry_date 漂移）

### Changed

- **scheduled-task `alpha-hive-sample-accumulator` cron: `0 10 * * 6` → `0 18 * * 0`**
  - 原：每周六 PDT 10:02（下次 2026-04-18 周六）
  - 新：**每周日 PDT 18:01**（下次 2026-04-19 周日，距周一开盘 12.5h）
  - **理由**：周六扫描会让 `entry_date` 记为周六，但美股周六休市无法真实交易。T+N 下游日期漂移 2 天（周末摊进去）。改到周日晚后 entry_date=周日 → 最近可交易日=周一（偏差仅 1 天，对统计验证影响微乎其微）
  - Prompt 内已加说明"周日晚 yfinance 返回周五收盘数据 — 这是预期行为"

### Fixed — 文档时间戳精度错误

- **MEMORY.md Scheduled Tasks 表格时间修正**
  - `alpha-hive-daily-scan`: 原记作 "周一~五 PDT 14:03" → 实际是 **21:03 PDT**（收盘后 8 小时）
  - `alpha-hive-weekly-optimizer`: 原记 "02:07" → 实际 **09:07 PDT**
  - `alpha-hive-monthly-self-analysis`: 原记 "03:13" → 实际 **10:13 PDT**
  - 所有时间已用 `list_scheduled_tasks` 返回的 `nextRunAt` UTC 反查 PDT 确认
- **应 2026-04-17 日期事故（用户纠正）** 已在 MEMORY 里加入日期精度硬约束，强制每次提及"明天/今天/周X"前校准

### 影响评估（对昨天结论无影响）

| 结论 | 影响 |
|------|------|
| raw 210 笔 Sharpe +1.10 CI [+0.305, +1.868] 显著为正 | ✅ 不变 |
| 固定 T+7 + SL/TP 熊市最优 | ✅ 不变 |
| 扩样本方案继续运行 | ✅ 时间点改到周日后更精确 |
| 第一次 sample-accumulator 扫描时间 | **2026-04-19 周日 18:01 PDT** |

---

## [0.23.2] — 2026-04-17 — 二次审计：8 Bug 修复 + 发现另一个假 alpha

三个并行审计 Agent（新脚本 / 核心引擎 / 配置部署）找到 18 个问题，本次修复 7 个 P0 + 1 个 P1。

### Fixed — P0 Critical（**挽救 v0.23.1 扩样本失效危机**）

- **#1 `alpha_hive_daily_report.py:2122`** — 主扫描路径未接入 `_resolve_focus_tickers`
  - 旧实现：`focus_tickers = list(WATCHLIST.keys())[:10] if args.all_watchlist else args.tickers`
  - **影响**：v0.23.1 新增的 `--extended-pool` / `--max-tickers` 对主扫描**完全无效**
  - sample-accumulator scheduled-task 原定明天跑 50 只，实际只会跑 10 只 → 样本翻倍计划完全失败
  - 修复：两处 `focus_tickers =` 都改用 `_resolve_focus_tickers(args)`

- **#2 `alpha_hive_daily_report.py:2128-2160`** — `--samples-only` 未在 `save_report` 前短路
  - 旧实现：先跑 `save_report()` → `_save_output_files()` 生成 MD/HTML/PWA/X线程/rss.xml 到 repo 根
  - **影响**：周六扩样本扫描会落盘 50 份 HTML + MD + PWA 文件；下次 daily-scan 的 `auto_commit_and_notify` 会把它们 commit 到 main 污染生产网站
  - 修复：`args.samples_only=True` 时直接 early return，只写最小 JSON

- **#3 `factor_attribution.py:275-282`** — HAC 缺少 n/(n-k) 自由度修正
  - 旧实现：`cov_hac = XtX_inv @ S @ XtX_inv`
  - **影响**：n=36/k=6 下 SE 被系统性低估 ~20% → 高估 t-stat 和显著性
  - 修复：`dof_correction = n/max(n-k, 1)` 并 `cov_hac *= dof_correction`
  - 同时 `portfolio_factor_attribution.py` 加 n<30 闸门，避免小样本 auto-HAC 随机触发

- **#4 `walk_forward_validator.py:164-175`** — `train_pct+test_pct>=1.0` 时 k-fold 失效
  - 旧实现：`available = 1.0-(train+test)`，若默认 0.70+0.30=1.0 → step=0 → **所有 fold 同一窗口**
  - **影响**：用户跑 `--folds 3` 默认参数时完全无 walk-forward，但工具却返回"成功"
  - 修复：默认改为 `train=0.60 test=0.20`；`available<=0` 时 fold>0 直接返回空

- **#5 `swarm_agents/chronos_bee.py:379-396`** — `_dt.now()` 作 entry_date 导致 hold_days 少算 1 天
  - 旧实现：扫描时间（21:03 PDT 收盘后）直接当 entry_date
  - **影响**：真实交易应在下一交易日开盘入场；催化剂距离少算 1 天
  - 修复：找 now() 之后下一个工作日作 entry_date（周五扫 → 周一入场）

- **#6 `portfolio_backtest.py:177` + 149-178** — `horizon=1/30` 下 `spy_return_t7=0.0` 硬编码
  - 旧实现：非 T+7 分支 SPY 收益永远 0
  - **影响**：**导致 v0.22.2 "T+30 α +49%" 严重高估 alpha**（把 SPY 同期涨幅全算成策略 alpha）
  - 修复：预拉一次 SPY 历史，按每笔 entry_date + horizon 天交易日计算真实同期收益
  - **修复后真相**：T+30 策略 PnL -$2,073（-4.15%），SPY 同期 +2.73% → **真实 Alpha -6.88%**（不是报的 +3.00% / +49% α）
  - 附带修复：`exit_date` 解析失败时 drop 该记录而非赋空串（避免 WINDOW_CUTOFF 静默丢 PnL）

- **#7 `bootstrap_ci.py:94-111` + `_quantile`** — PF=inf 被静默丢弃 + 分位数 nearest-rank 偏差
  - 旧实现：`pf=inf` 返回 None → 上游 `samples[k].append(v)` 丢弃 → CI 上限幸存者偏差低估
  - 修复：PF cap 到 999.0（大到显示"极高"但参与统计）；losses 改为 `r < 0` 不含 0；quantile 改线性插值

### Fixed — P1

- **`config.py:358` CRCL sector "Fintech" → "FinTech"** + `get_extended_watchlist()` 加自动 sector alias normalization（Fintech/fintech → FinTech），消除 feedback_loop 三桶冲突（Fintech/FinTech/Financials）
- `get_extended_watchlist()` 明确文档"WATCHLIST 优先，扩展池只补不覆盖"

### Findings — 另一个假 alpha 被揭穿

| 指标 | v0.22.2 报告 | **v0.23.2 真实** | 差距 |
|------|-------------|-----------------|------|
| T+30 策略收益 | -4.15% (对) | -4.15% | — |
| SPY 同期 | 硬编码 **0%** ❌ | **+2.73%** ✅ | +2.73pp |
| **Alpha vs SPY** | **+3.00%** 🔴 误导 | **-6.88%** 🟢 真实 | **-9.88pp** |
| FF α 估计 | +49% | 待重跑（预计仍为正但大幅下修） | — |

**诊断**：v0.22.2 的"T+30 揭示真 Alpha +49%"其实**一半是 SPY 同期上涨被错归为 alpha**。T+30 策略在这 76 笔样本上**跑输 SPY 6.88%**，并非超额收益。

### 验证

所有 7 修复实际跑通：
- Portfolio backtest 默认（T+7 放宽）：38 笔入场 / PnL +$687 / Sharpe +0.18 / Alpha vs SPY -1.36%
- Portfolio backtest T+30：**SPY +2.73% / Alpha -6.88%（真实暴露）**
- Bootstrap raw 210 笔：Sharpe +1.105 CI [+0.305, +1.868] ✓ 仍显著为正（核心结论不变）

### 对昨天结论的影响

| 结论 | 修改前 | 修改后 |
|------|--------|--------|
| raw 210 笔 Sharpe +1.10 CI 显著 | ✅ 成立 | ✅ **仍成立** |
| 系统有 stock-picking edge | ✅ 成立 | ✅ **仍成立** |
| "T+30 α +49% 是真 alpha" | ❓ 可疑 | ❌ **证伪**（一半是 SPY 漂移） |
| 扩样本 sample-accumulator 明天生效 | 🔴 **失效**（bug #1） | ✅ **真的生效** |

---

## [0.23.1] — 2026-04-17 — 混合双轨：零 API 费用的扩样本管道

### 背景

用户确认每日 LLM 模式扫描约 ¥0.2 / ~$0.10-0.20/次，扫 101 只 LLM 会把费用线性放大到 $1-2/天。
设计"混合双轨"方案平衡成本 vs 样本量。

### Added

- **`alpha_hive_daily_report.py --samples-only` flag**
  - 只跑蜂群扫描写 pheromone.db，不生成 HTML 报告 / 不推 GitHub / 不推 Slack
  - 避免扩样本扫描污染 gh-pages 生产网站
  - 配合 `--no-llm` 保证 $0 Anthropic API 费用

- **Scheduled Task `alpha-hive-sample-accumulator`**
  - Cron: `0 10 * * 6`（每周六 PDT 10:00）
  - 命令：`python3 alpha_hive_daily_report.py --swarm --no-llm --extended-pool --max-tickers 50 --samples-only`
  - 执行时长 ~25 分钟，零 API 费用
  - 自动对比扫描前后 pheromone.db 样本数，打印新增数量

### 架构：混合双轨

| 轨道 | 频率 | 命令 | API 费用 | 产出 |
|------|------|------|---------|------|
| **1. 深度日报** | 周一~五 14:10 PDT | `--swarm --use-llm --tickers 10只` | ~$0.10-0.20/天 | 核心 HTML 报告 + Slack 推送 + gh-pages |
| **2. 样本积累** | 周六 10:00 PDT | `--swarm --no-llm --extended-pool --max-tickers 50 --samples-only` | **$0** | 仅写 pheromone.db 用于回测验证 |

### 价值预期

3 个月后对比：

| 方案 | 每月费用 | 3 个月 T+30 样本 |
|------|---------|-----------------|
| 仅日报 10 只 LLM（原状） | ~$3-6 | ~300 笔 |
| **混合双轨** | **~$3-6（不变）** | **~700-900 笔** |
| 每日 101 只 LLM | ~$30-60 | ~1500 笔 |

**混合双轨在不增加任何费用的情况下，样本量翻倍至 2-3 倍**，足以回答 v0.22.2 遗留问题"T+30 α +49% 是运气还是技能"。

---

## [0.23.0] — 2026-04-17 — 动态 Exit + 扩样本 + Newey-West HAC

### Added

#### 🥉 Newey-West HAC 标准误（~1h 完成）
- **`factor_attribution._ols(y, X, hac_lag=None)`** — 支持 Newey-West HAC 方差估计
  - Bartlett kernel 权重 `w_l = 1 - l/(L+1)`
  - 自动 lag 推荐：`floor(4·(n/100)^(2/9))`
  - 修正序列自相关导致的显著性高估
- **`portfolio_factor_attribution._regress` 自动启用 HAC**
  - 残差一阶自相关 |ρ| > 0.15 时自动启用
  - CLI: `--hac-lag N`、`--no-hac`
  - 输出 `regression_method = "OLS+HAC(lag=N)"`

**验证**：T+30 组合归因 OLS vs HAC 对比：
| 方法 | α 年化 | t-stat | p-value | 显著性 |
|------|-------|--------|---------|--------|
| 朴素 OLS | +49.09% | 无 HAC | <0.0001 | *** |
| HAC lag=3 (auto) | +49.09% | +3.13 | 0.0039 | *** |
| HAC lag=5 | +49.09% | +2.80 | 0.0088 | *** |
| HAC lag=10 | +49.09% | +2.56 | 0.0156 | ** |

**结论**：即使修正残差 +0.82 自相关，T+30 α 仍是统计显著的（p<0.016）

#### 🥇 催化剂驱动的动态 Exit（~4h 完成）
- **`catalyst_exit_planner.py`（新文件）** — 事件驱动 exit 规划器
  - 规则：earnings/guidance 前 2d 平仓；fda_approval/product_launch 后 3d 平仓；regulatory 后 1d；无催化剂默认 T+21
  - 硬边界：hold_days ∈ [3, 45]
  - `plan_exit(ticker, entry_date, catalysts) → (hold_days, rationale)`
- **`dynamic_exit_backtest.py`（新文件）** — 历史回测验证
  - 对 pheromone.db 每笔 checked_t7=1 预测，结合 catalysts.json 推断 hold_days
  - yfinance 拉 entry + hold_days 的实际收盘价算 net return
  - 三组对比：固定 T+7（DB）vs 固定 T+21（裸持）vs 动态 Exit
- **`swarm_agents/chronos_bee.py`** — 集成 `plan_exit`
  - details 新增 `recommended_hold_days` 和 `exit_rationale` 字段
  - 未来扫描的 predictions 会自动带这两个字段

**⚠️ 实证结果出乎意料**：

| 策略 | n | Avg Net | WR | Sharpe | $50K·10% PnL |
|------|---|---------|-----|--------|--------------|
| **固定 T+7 + SL/TP** | 210 | **+1.56%** | 55.2% | **+1.11** | **+$16,409** |
| 固定 T+21 裸持 | 181 | -4.28% | 29.8% | -2.99 | -$38,723 |
| 动态 Exit | 185 | -1.39% | 42.7% | -0.77 | -$12,822 |

**关键诊断**：
1. **固定 T+7 + 路径依赖 SL/TP 在 2-4 月样本上实际最优**
   - -5~12% SL 是熊市保护神（3 月下跌期提前止损 avoid -15% 深亏）
   - +10% TP 在 4 月反弹期锁定利润
2. **v0.22.2 "T+30 α +49%" 很可能是 V 型反弹运气**
   - 76 笔 entry 都在 2-3 月初，T+30 正好跨过 3 月底部到 4 月反弹
3. **动态 Exit "财报前 2d 平仓"在熊市中反而是"低点确认亏损"**
   - 大部分 earnings 落在 3 月熊市中段，提前平仓没机会等反弹

**修正后的结论**：
- ChronosBee 已集成 `recommended_hold_days`（未来扫描使用）
- 但**当前样本不支持"固定 T+7 路径依赖 SL 不够好"的结论**
- 真正需要的是 **regime-aware exit**：熊市用 T+7+SL，牛市用 T+30+trailing
- 这需要**更多样本**才能实现（方案 🥈）

#### 🥈 扩样本（~3h 完成）
- **`alpha_hive_daily_report.py` CLI 新增 `--extended-pool` / `--max-tickers`**
  - `--extended-pool`：合并 WATCHLIST (24) + WATCHLIST_EXTENDED (77) = **101 只** 扫描
  - `--max-tickers N`：硬上限（防首次跑太久）
  - `_resolve_focus_tickers(args)`：统一 CLI 解析优先级
- **Sector 多样化**：14 个 sector 覆盖（Tech 29、Healthcare 13、Financials 9、ETF 9、Consumer 8、Communication 8、Automotive 8、CleanEnergy 5、Industrials 4、Fintech 3、Energy 2、AI 1、Aerospace 1、Other 1）

**价值**：
- 每日扫描从 10 → 50-101 标的（按 `--max-tickers` 控成本）
- 3 个月可累积 T+30 样本从 **76 → 500-900+**
- 真正验证"T+30 α +49% 是运气还是技能"所需

### Changed

- `factor_attribution._ols` 返回值新增字段：`se_ols`、`se_hac`、`method`
- `portfolio_factor_attribution.run_portfolio_attribution` 新增 `hac_lag` 参数

---

## [0.22.2] — 2026-04-17 — T+1 / T+7 / T+30 持仓期对比（延长持仓期发现）

### Added

- **`portfolio_backtest.py` 新增 `--horizon {1,7,30}` CLI 参数**
  - `BacktestConfig.horizon` 字段（默认 7）
  - `load_verified_predictions(horizon)`：按 horizon 从 pheromone.db 读 `return_t{N}` 和 `price_t{N}`
  - 动态用 `trading_costs.apply_costs()` 重算 `net_return_t{N}`（不需改 DB schema）
  - T+7 维持原有路径依赖 SL/TP 逻辑；T+1 / T+30 用简化 T+N_CLOSE（无中途止损）
- **`run_backtest()` all_dates 扩展到 max(exit_date)**
  - 旧实现 last_date = max(entry_dates) → T+30 仓位全被 WINDOW_CUTOFF 吃掉
  - 新实现：all_dates = entry_dates ∪ exit_dates；price_t{N} 是真实已观测价不存在 look-ahead

### Findings — 持仓期大幅影响 α

#### Apples-to-apples（同 76 笔 T+30 可用样本，不同 horizon 结算）

| Horizon | Avg Net | WR | Per-trade Sharpe | PF | FF Jensen α | p-value | IR |
|---------|---------|-----|---|-----|-------------|---------|-----|
| T+1 | +0.36% | 50.0% | +1.64 | 1.37 | — (观测不足) | — | — |
| **T+7** | +1.78% | 55.3% | +1.20 | **2.00** | ✗ 观测不足 (12 天) | — | — |
| **T+30** | +1.08% | **56.6%** | +0.35 | 1.36 | **+49.09%** | **<0.0001 \*\*\*** | **+14.75** |

#### 全样本对比

| Horizon | 样本 | 回测收益 | Alpha vs SPY | FF α | p-value | R² |
|---------|------|---------|-------------|------|---------|-----|
| T+1 (270 笔) | 212 入场 | +4.99% | **+2.25%** | +6.64% | 0.89 ✗ | 58% |
| T+7 (210 笔) | 29 入场 | -1.60% | +0.61% | -25.2% | 0.052 * | 51% |
| T+30 (76 笔) | 9 入场 | -4.15% | -6.88% | **+49.1%** | **<0.0001 \*\*\*** | **11%** |

### 核心洞察

**🎯 固定 T+7 持仓期确实是 edge 被吃掉的主因之一**
- T+30 FF α +49% 高度显著，IR 14.7 极高
- **R² 仅 11%** → 89% 收益来自真正的 stock-picking（不是因子伪装）
- 所有因子 β（smb/hml/mom/qual）在 T+30 都**不显著** → 接近零因子暴露的纯 alpha

**⚠️ 但必须诚实标注风险警告**
- T+30 残差一阶自相关 **+0.82** — 严重违反回归独立性假设
- 76 笔 entry 集中在 2-3 月初，T+30 正好跨过 3 月下跌 + 4 月反弹 U 型
- +49% α 可能**被"好运气捕捉到 V 型反弹"**严重高估
- 真实 Sharpe 可能大幅低于报告值（Newey-West 或 HAC 标准误会收缩）

**📊 相对对比比绝对值更可信**
- T+30 α > T+1 α > T+7 α（filtered）这个排序可能稳健
- 但"具体 +49% 是不是真的"需要更多 entry date 分散的样本验证

### 下一步

1. **优先级最高**：**接入 WATCHLIST_EXTENDED（101 只）扩大样本**
   - 当前 T+30 只有 76 笔 + entry 集中于 2 周，样本偏差严重
   - 扫描扩到 50+ 标的后 3 个月可积累 500+ T+30 样本
2. **实现 PEAD-style 动态 exit**：让 ChronosBee 催化剂驱动 exit（财报后 5d / FDA 后 3d 等），而非一刀切 T+7
3. **Newey-West HAC 标准误**：加到 FF 归因，修正序列自相关高估显著性问题
4. **按 entry regime 分桶**：2-3 月初 entry vs 3 月中旬 entry 的 T+30 表现差异，排除"运气吃 V 型反弹"

---

## [0.22.1] — 2026-04-17 — 方案 A 放宽筛选 + 揭示核心矛盾

### Changed

- **`portfolio_backtest.py BacktestConfig` 默认值放宽**
  - `max_agent_std`: 1.5 → **2.5**（允许分歧信号）
  - `min_score_bull`: 6.5 → **5.5**（不再只收共识最强票）
  - `min_score_bear`: 4.5 → **5.5**（镜像对称）
  - `accept_neutral`: False → **True**（中性 40 笔可能含 alpha）
  - `max_concurrent`: 5 → **15**（gross_exposure 已防杠杆）
- CLI argparse `default` 改为读 `BacktestConfig()` 实例值，避免双入口不一致

### Findings — 放宽后数字对比

| 指标 | 基线 (11 笔) | **放宽 (29 笔)** | raw 基准 (210 笔) |
|------|------------|----------------|-----------------|
| 样本量 | 11 | **29** (+164%) | 210 |
| Sharpe 点估计 | -1.80 | **-0.48** | +1.10 ✓ |
| Sharpe 95% CI | [-9.3, +2.4] (跨零) | **[-3.8, +1.7]** (跨零但收窄 53%) | **[+0.34, +1.86]** ✓ |
| WR | 27.3% | 31.0% (CI [13.8, 48.3] ✓ **显著低于 50**) | 55.2% |
| Alpha vs SPY | +1.08% | +0.61% | — |
| FF Jensen α | N/A (CAPM) | **-25.2%** (p=0.052 *) | **+165%** (p=0.015 **) |
| β_smb | — | **-0.31 ***** | -0.88 * |
| β_mom | — | **+0.23 ***** | +0.72 * |

### 关键矛盾（该是核心议题）

**🚨 "raw 信号有 edge" 与 "组合回测负 alpha" 并存**

- raw 210 笔 Sharpe +1.10 CI [+0.34, +1.86] ✓ 显著正 → **信号本身有 edge**
- 放宽 29 笔 FF 归因 α -25% p=0.05 * → **进入组合执行后 alpha 变负**
- β_smb -0.31 (***) + β_mom +0.23 (***) + β_qual -0.50 (*) + R²=51% → **收益 51% 来自因子暴露，stock-picking 剩下的 49% 是负**

**中间"吃掉 edge"的环节**（按嫌疑度排）：
1. **交易成本吃掉**：双边滑点 ~10bps + 佣金 2bps × 2 side + 借券（空头）= 15-20bps/笔，而原始 edge 可能仅 15-30bps
2. **路径依赖 SL/TP 不对**：-5%~-12% SL 可能在低点止损（8 笔 SL 平均 -7.4%），-10% TP 可能到不了（2 笔 TP 平均 +9.6%）
3. **固定 T+7 持仓期太死**：19 笔 T7_CLOSE 平均 +1.0% — 还在爬升时强平
4. **小样本噪声**：29 笔 Sharpe CI 宽 5.5pp，点估计 -0.48 可能 ±运气

### 下一步优先级调整

**不再继续放宽** — 29 笔的统计显著负 alpha 告诉我们"不是筛选问题"。新方向：

1. **动态持仓期**（~4h）—— 接入 price_t30 / price_t1 数据，让 ChronosBee 催化剂驱动 exit timing
2. **动态 SL/TP**（~3h）—— 按历史波动率（ATR）设 SL，不再固定 %
3. **因子中性化**（~6h）—— 利用 β_mom +0.23 ** 显著 → 做动量对冲，剥离 smb/mom 暴露后的剩余 alpha 是真正 stock-picking
4. **交易成本审视**（~1h）—— 看 15-20bps/笔 是否合理，真实 IBKR 成本可能更低

---

## [0.22.0] — 2026-04-17 — 样本外验证 + FF 因子归因（方向 1+2）

从"原型系统"升级到"真正的量化研究工作流"。4 个新模块让系统具备**统计显著性判断能力**。

### Added

- **`config.WATCHLIST_EXTENDED`（77 只 S&P 500 高流动性 + ETF）+ `get_extended_watchlist()`**
  - 核心 25 只 + 扩展 77 只 = 101 只总池，覆盖 14 个 sector
  - 扫描脚本通过 `--extended-pool` 启用（代码已接入配置，待扫描脚本接入 CLI）
  - **价值**：样本量 10x + sector 多样化，2-3 个月后 Walk-forward/Bootstrap 可产出稳健结论

- **`walk_forward_validator.py`（新文件，方向 1b）**
  - Rolling k-fold 时间序列切分（train/test 按时间顺序，无 lookahead）
  - 区分**过拟合** (train>test) vs **非平稳性** (test>train)
  - Purge gap 支持（训练/测试间隙，防信息泄漏）
  - 当前 3-fold 测试结果：
    - Fold 0: train WR 48.4% → test WR 50.0%（稳定）
    - Fold 1: train WR 48.8% → test WR 64.3%（非平稳）
    - Fold 2: train WR 44.1% → test WR 81.0%（非平稳）
    - 评级：🔴 严重非平稳（max |gap|=37pp），系统 evolve 中 / 4 月行情不同于 3 月
  - CLI: `python3 walk_forward_validator.py --folds 3 --train-pct 0.6 --test-pct 0.2`

- **`bootstrap_ci.py`（新文件，方向 1c）**
  - Efron 非参数 bootstrap：1000-5000 次有放回重采样
  - 输出 Sharpe / WR / PF / Avg Net 的 **95% 置信区间** + 显著性判断（CI 同号）
  - 两种数据源：`--source raw_db`（全 210 笔）vs `--source portfolio_backtest`（筛选后 9 笔）
  - **关键发现（raw 210 笔）**：
    - Sharpe **+1.105**, 95% CI **[+0.34, +1.86]** ✓ **统计显著为正**
    - Profit Factor **+1.63**, CI [1.16, 2.32] ✓ 显著
    - Avg Net **+1.56%**, CI [+0.46, +2.74] ✓ 显著
  - **关键对比（filtered 9 笔）**：
    - Sharpe -1.8, CI **[-9.26, +2.38]** ✗ 跨零，统计无意义
  - **重大洞察**：**portfolio_backtest 的过严筛选把原始信号的 edge 杀掉了**
    - 原始 210 笔 signals 有显著正 Sharpe
    - 筛掉 199 笔后剩 9 笔 → 样本过小，点估计不可信
    - 方向：**放宽筛选阈值，保留更多样本**
  - CLI: `python3 bootstrap_ci.py --n 2000 --source raw_db`

- **`portfolio_factor_attribution.py`（新文件，方向 2）**
  - 组合级 Fama-French 因子归因（`factor_attribution.py` 原只支持单 ticker）
  - 策略日度收益构造：持仓期 $50K × 10% 仓按交易日分摊 net_return_t7
  - 三档模型：FF6（Kenneth French，6 因子）/ ETF5（SPY/IWM/IWD/IWF/MTUM/QUAL 近似实时）/ CAPM（单因子降级）
  - 自动降级：FF6 日期重叠不足 15 天 → 切 ETF5（修复 Kenneth French 1-2 月数据滞后）
  - 输出：Jensen α + t-stat + p-value + IR + 因子暴露 + 残差自相关
  - **首次运行结果（ETF5 / 36 观测日 / raw 210 笔）**：
    - **Jensen α 年化 +166%** (t=+2.58, p=0.015, ** 显著)
    - **IR +7.75**（但样本小需大幅打折）
    - β_smb -0.88 (*) —— **系统性偏向大市值**
    - β_mom +0.72 (*) —— **跟随动量**
    - R² 39%，残差自相关 +0.41（⚠️ 仍有未捕捉因子，可能 IV/行业）
  - **关键诊断**：样本量警告逻辑会对 `n_obs < 60 + |α| > 50%` 输出"关注方向+因子+IR 量级，不要纠结具体 α 数字"
  - CLI: `python3 portfolio_factor_attribution.py --factor-source etf --source all_trades`

### Changed

- **`MEMORY.md` v22.0 记录**：样本外验证 + 因子归因能力上线

### 关键诊断汇总（运营启示）

从这次验证得到的 4 个实锤结论：

1. **原始信号有真 edge**：raw 210 笔 Sharpe +1.10 CI [+0.34, +1.86] 统计显著为正（Bootstrap）
2. **过严筛选反效果**：filtered 9 笔 Sharpe 点估计 -1.8 但 CI 跨零无意义 → 说明 `portfolio_backtest` 的 5-7 重筛选（agent_std + score + macro + concurrent + direction）过于激进，把 edge 筛没了
3. **数据非平稳**：3-fold walk-forward 显示 4 月测试期 WR 远高于 3 月训练期（50% → 64% → 81%），要么系统在 evolve，要么只是运气；无法定论需更多样本
4. **真 Alpha + 真因子暴露**：FF 归因 p=0.015 ** 显著 α > 0；系统**被动地**在做**大市值 + 动量**因子暴露，剥离后仍有 edge

### 下一步建议

- **短期（1 个月）**：放宽 portfolio_backtest 筛选（`max_agent_std` 1.5 → 2.0，`min_score_bull` 6.5 → 5.5），观察样本量能否扩到 30-50 笔
- **中期（2 个月）**：接入 `WATCHLIST_EXTENDED`（需扫描脚本 CLI 改造）
- **长期（3 个月）**：样本达到 100+ 后重跑 walk-forward，再做参数调优，否则都是在"小样本噪声"上调参

---

## [0.21.0] — 2026-04-17 — 18 项深度 Bug 修复 + 去除 look-ahead bias

4 个并行审计 Agent 找出 18 个真实 Bug，全部修复。**去除 look-ahead bias 后，真实回测数字从 "$50,871 / Sharpe 1.11" 归为 "$49,439 / Alpha vs SPY +1.08%"** — 系统仍有选股能力，但远没有之前吹嘘的那么强。

### Fixed — P0 合规 / 资金安全（继承 2026-03-16 事故风险）

- **#1 `alpha_hive_daily_report.py:2029`** — LLM opt-in 修复
  - 旧：`choice != "2"` 默认选 LLM，非交互 stdin 返回空串 → 静默烧钱
  - 新：默认规则引擎；`--use-llm` 仅在 TTY 交互下可确认；cron 环境即使显式指定也降级
- **#2 `alpha_hive_daily_report.py:1240`** — `_compute_cross_ticker` 绕过 opt-in
  - 旧：`_llm_ct.is_available()` 只要 key 存在就调 LLM cross-ticker 分析
  - 新：仅当 `distill_mode == "llm_enhanced"` 已存在时才调用
- **#3 `report_deployer.py:220`** — 生产模式判定
  - 旧：`_using_llm = is_available()` key 存在即视为生产
  - 新：看实际 `distill_mode` 或 swarm 标记
- **#4 `pre_scan_notify.py:346`** — 超时 Bot DM 违反「只 2 类 DM」硬约束
  - 旧：超时发"扫描已跳过" Slack DM
  - 新：仅写本地日志，不打扰用户

### Fixed — P0 学习闭环

- **#5 `weekly_optimizer.py:91` `_apply_weight_clamps`** — **迭代 clamp 算法**
  - 旧实现 "先 clamp 再归一化" 数学不一致，归一化后可突破 clamp 上限
  - **实证**：config.py 当前 `catalyst=0.3316`（>0.25 上限）就是此 bug 后果
  - 新算法：循环钳制 + 分配 slack 给未钳制维度，严格保证 `lo ≤ w[k] ≤ hi` 且 sum=1.0
  - 新增 `AGENT_TO_DIM` 统一映射（所有学习路径唯一入口）
- **#6 `feedback_loop.py:295`** — BearBeeContrarian 纳入学习闭环
  - 旧：agent_scores 字典只有 6 只蜂，BearBee 被排除
  - 新：BearBee 纳入 risk_adj 维度；BearBee 预警正确时不再被系统"忽视"
- **#7 `feedback_loop.py:346` + `weekly_optimizer.py:186,320`** — 按维度内 Agent 平均而非累加
  - 旧：signal 维度 = Scout + Rival 两蜂准确率相加 → 结构性高于单蜂维度
  - 新：signal 维度 = avg(Scout, Rival)，与其他维度口径一致
- **#8 全局 Sharpe `periods_per_year`** — T+7 周期基准
  - 旧：多处用 52（周/年）作为 T+7 采样频率，高估 Sharpe ~20% (√52/√36=1.2)
  - 新：252 交易日 / 7 交易日采样 = **36 次/年**
  - 涉及文件：`portfolio_backtest.py:421` `trading_costs.py:114,141` `paper_portfolio.py:29` `dashboard_renderer.py:787`

### Fixed — P0 回测 look-ahead bias（让数字真实）

- **#12 `backtester.py:875`** — Gap-aware exit_px
  - 旧：gap down 穿透 SL 时 `exit_px = sl_price`，低估真实亏损
  - 新：`fill_price = min(open, sl_price)`（看多 SL）/ `max(open, sl_price)`（看空 SL）
- **#13 `backtester.py:897`** — Direction 白名单
  - 旧：`elif _dir not in ("bullish","bearish")` 吞掉 `None/""/unknown` 所有异常值
  - 新：`_dir_normalized = _dir if _dir in {bullish, bearish, neutral} else "neutral"`
- **#14 `backtester.py:848`** — 交易日过滤而非 head(N)
  - 旧：`hist.head(days_ahead)` 按行数，停牌/假日可能 holding<7 却落 T7_CLOSE
  - 新：过滤 NaT 索引后再截断
- **#15 `portfolio_backtest.py:315`** — NAV mark-to-market + 总敞口保护
  - 旧：`nav_est = cash + sum(size_usd)` 用建仓成本当 NAV，复利下仓位占比漂移
  - 新：`nav_est = initial_capital + cum_realized`；增加 `gross_exposure > nav × 1.0` 检查，防 bear 12% × 10 仓 = 120% 杠杆
- **#16 `portfolio_backtest.py:383`** — 回测末尾强平 look-ahead 消除
  - 旧：用预计算 `net_return_pct`（完整 T+7 到期收益）结算未到期仓位 → virtualize final_nav
  - 新：未到期仓位 PnL=0（`WINDOW_CUTOFF`），**严格无未来信息泄漏**
- **#17 `trading_costs.py:96`** — Borrow 按自然日
  - 旧：`borrow_pct = annual × trading_days / 365`，低估 30-40%
  - 新：自然日换算（× 1.4 系数），可选 `holding_calendar_days` 参数

### Fixed — P0 Agent 崩溃路径

- **#9 `swarm_agents/oracle_bee.py:162`** — `result` 前置初始化 + 扩 except 元组
  - 旧：`except (ImportError, ConnectionError, ValueError, KeyError, TypeError)` 漏 `OSError/URLError/AttributeError` → yfinance 抛 OSError 时 result 未定义 → 下游 NameError → OracleBee 整个返回 5.0
  - 新：try 前 `result = {}`；except 加 `OSError, AttributeError`
- **#10 `swarm_agents/scout_bee.py:40`** — `insider_data=None` 降级守卫
  - 旧：`insider_data.get()` 在 `get_insider_trades` 返回 None 时抛 AttributeError → ScoutBee 整体回 5.0
  - 新：`if insider_data and isinstance(insider_data, dict):` 守卫 + 扩 except 元组
- **#11 `swarm_agents/oracle_bee.py:244`** — 方向判定改具体词组
  - 旧：`"多" in signal_summary` 命中"多头/很多/许多空头"等歧义词
  - 新：`_bull_keywords = ("看多","看涨","多头","走高","上行")` + `_bear_keywords` 计数投票

### Fixed — P1 零散修补

- **#18 `swarm_agents/queen_distiller.py:78`** — `importlib.reload(config)` 实现真正热加载
- **#19 `swarm_agents/queen_distiller.py:255`** — 缺失维度不再注入中性假值，改为仅保留已覆盖维度加权
- **#20 `paper_portfolio.py:143-170`** — `_atomic_write_text()` 原子写（tempfile + fsync + os.replace）替换 `open("w")`
- **#21 `report_deployer.py:182`** — gh-pages push 结果写入 `.gh_pages_deploy_log.jsonl` 持久化 queue
- **#22 `outcomes_fetcher.py:147`** — T+30 回填余量从 `+2 days` 改为 `× 1.4 + 3 days`

### Changed — 真实回测结果（Plan C 修复后）

| 指标 | v0.20.0 宣称 | v0.21.0 真实 | 说明 |
|------|------------|-------------|-----|
| Final NAV | $50,871 (+1.74%) | **$49,439 (-1.12%)** | #16 消除未来信息泄漏 |
| Sharpe | 1.106 | **-1.804** | #8 周期基准修正 + 样本仅 11 笔统计不稳 |
| Win Rate | 52.9% (9/17) | **27.3% (3/11)** | #15 NAV MTM 后入场门槛变紧 |
| Bull WR | 60% | **20%** (2/10) | 看多能力被高估 |
| Bear WR | — | **100%** (1/1) | 看空仍准（样本少） |
| SPY 基准 | — | **-2.21%** | 回测期大盘下跌 |
| **Alpha vs SPY** | — | **+1.08%** | **真实跑赢大盘 1%** |
| Max Drawdown | — | -1.31% | |

**诚实反思**：v0.20.0 的"优秀数字"主要来自三个 look-ahead bias（#8 Sharpe 周期 + #15 NAV 漂移 + #16 末尾强平虚增），去除后数字回到现实。系统**确实有选股能力**（Alpha +1.08% vs SPY），但远没达到 "Sharpe 1.11 投资级" 的水平。

---

## [0.20.0] — 2026-04-15 — $50K 回测 + 5 项数据驱动升级

### Added

- **`portfolio_backtest.py`（新文件）** — $50K 组合级别回测脚本
  - 从 pheromone.db 读取 191 条已验证 T+7 预测，模拟真实组合运营
  - 支持 CLI 参数：`--capital`、`--max-pos`、`--max-std`、`--no-macro-gate`、`--bull-size`、`--bear-size`
  - 输出：按方向/退出类型/标的/月度分维度统计 + equity curve + 每笔交易明细
  - 口径说明：股票现货策略（非期权），含双边滑点+佣金+借券费

- **升级1: Agent 共识硬门控**（`portfolio_backtest.py`）
  - 新增 `max_agent_std` 参数（默认 1.5），从 dimension_scores 计算 5 维标准差
  - std ≥ 1.5 的信号跳过入场（数据：std<1.5 胜率 71% vs ≥1.5 仅 29%）

- **升级4: 宏观政体门控**（`portfolio_backtest.py`）
  - SPY 20MA 计算 + risk-off 判断（SPY < 20MA × (1-3%)）
  - risk-off 期间禁止看多入场

- **升级5: Catalyst 权重 clamp**（`weekly_optimizer.py`）
  - 新增 `WEIGHT_CLAMPS` dict，限制每个维度权重范围
  - Catalyst 上限 25%（原被 optimizer 推到 33%，导致高分看多反而亏钱）

### Changed

- **升级2: Per-ticker 自适应止损**（`config.py` + `backtester.py`）
  - `TRADING_EXITS_CONFIG` 新增 `sl_overrides` dict
  - 大盘蓝筹 5%，TSLA/QCOM 6-7%，BILI/RKLB 10%，CRCL/VKTX 12%
  - 结果：SL 触发率从 27.7% → 15.2%，TP 从 12% → 15.7%，准确率 53.4% → 60.2%

- **升级3: 放大看空信号**（`portfolio_backtest.py`）
  - `min_score_bear` 默认从 3.5 → 4.5（放宽看空入场门槛）
  - 看多仓位缩小 6% NAV / 看空仓位放大 12% NAV（方向不对称）

- **中性方向 SL 保护**（`backtester.py`）
  - 中性不再免于止损，设 15% 宽松下跌保护
  - 修复 CRCL 中性 -30% 无止损灾难（现被 -15.5% SL 拦截）

- **`backfill_trading_costs.py`** 新增 `--force` 参数，支持重算所有已验证记录

### 回测对比（$50K，29 个交易日）

| 指标 | 升级前 | 升级后 |
|------|--------|--------|
| PnL | +$253 (+0.51%) | **+$871 (+1.74%)** |
| Sharpe | 0.424 | **1.106** |
| Win Rate | 41.7% | **52.9%** |
| Profit Factor | 1.181 | **1.471** |
| Alpha vs SPY | +5.18% | **+6.42%** |
| 看多胜率 | 36.4% | **60.0%** |
| SL 触发率 | 25.0% | **5.9%** |

---

## [0.19.1-param-opt] — 2026-04-15 — SL 参数优化 + 参数优化器

### Added

- **`param_optimizer.py`（新文件）** — SL/TP/Deploy 网格搜索工具
  - 12 精选组合（`--quick`）或 48 全量组合（SL×TP×Deploy）
  - 自动 backup/restore 原始状态，幂等运行
  - 多目标排名：Alpha 40% + Sharpe 25% + PF 20% + WinRate 15%
  - HTML 报告：推荐参数卡片 + NAV 曲线 SVG + SL×TP Alpha 热力图 + Top 15 排行榜
  - CLI：`python3 param_optimizer.py --quick / --html`

### Changed

- **`paper_portfolio.py` 两层模式（bootstrap 全标的 / 实时白名单）**
  - CONFIG 新增 `live_start_date: "2026-04-16"` 和 `ticker_whitelist: ["NVDA"]`
  - `live_start_date` 之前：bootstrap 回放所有 ticker，建立历史 KPI 基准和胜率统计
  - `live_start_date` 之后：只对 `ticker_whitelist` 里的 ticker 开新仓，与实际生成报告的标的对齐
  - `ticker_whitelist` 留空 `[]` 恢复全标的模式
  - `_should_open()` 新增 `as_of` 参数，白名单过滤仅在实时阶段激活

- **`paper_portfolio.py` CONFIG `sl_pct`: 5.0 → 7.0**
  - 参数优化结果：SL -7% 胜率从 33%→50%（+17pp），Sharpe 从 1.27→2.73
  - 原因：NVDA/VKTX 等高波动票日内 5% 回撤为正常噪声，原 SL 过紧
  - TP/Deploy 不变（10% / 30% 已是最优）

### Fixed（v0.19.0 bug 修正，随此版本入库）

- `paper_portfolio._close_position` SL 滑点反向 bug：`extra_slip=2.0`（2bp）< 默认 10bp，已修为 `20.0`
- `paper_portfolio._open_position` rationale f-string 当 `composite_score=None` 崩溃，已修为 None→"N/A"
- `paper_portfolio.compute_kpis` daily_rets 单位错误（小数 vs 百分比）导致 Sharpe=-213，已修为 `×100`
- `ibkr_sync.reconcile` IBKR datetime 格式兼容（`20260415;140000` / `2026-04-15 14:00:00` 双模式）

---

## [0.19.0-paper-portfolio] — 2026-04-15 — $50K 策略模拟组合 + IBKR Paper Account 桥接

### Added — v0.19.0 · Phase 1 PaperPortfolio

- **`paper_portfolio.py`（新文件，~660 行）** — $50,000 透明模拟账户，按 Alpha Hive 策略信号自动开/平仓
  - 资金规则：每仓 `high=2.5%` / `mid=1.5%` / `low=0%` NAV × ticker win_rate 乘数（strong 1.2 / normal 1.0 / weak 0.5）
  - 限制：最大 15 仓位，最大部署 30% NAV，其余作现金缓冲
  - 出场：SL -5% / TP +10% / 时间止损 T+10 天（同日 SL+TP 同触发按保守取 SL）
  - 入场门槛：bull score ≥ 6.5、bear score ≤ 3.5、置信 ≥ mid
  - 状态文件：`paper_portfolio_state/{positions,closed_trades,equity_curve}.jsonl + meta.json`
  - 成本：集成 `trading_costs.apply_costs()`（滑点 + 佣金 + 借券费）
  - 回放：`bootstrap_from_history()` 从 2026-03-09 起逐日回放（受限于 report_snapshots 最早日期，非用户最初要求的 2026-01-02）
  - CLI：`bootstrap / run / kpi / card / reset`
  - HTML 卡片：KPI grid（NAV/SPY/Sharpe/MDD/胜率）+ SVG sparkline + 持仓表 + 近 5 笔平仓

### Added — v0.19.0 · Phase 2 IBKR 桥接

- **`ibkr_sync.py`（新文件）** — JSON 导出 + CSV 导入 + 对账
  - `export_daily_actions(date)` → `paper_account/actions/actions_YYYY-MM-DD.json`（symbol/side/qty/limit/tif 格式，IBKR TWS 手动或 ibapi 自动下单）
  - `import_ibkr_statement(csv_path)` → 解析 Trade Confirmation CSV 追加 `real_fills.jsonl`
  - `reconcile(date)` → 比较本地模拟 vs IBKR 真实成交，输出 slippage / fill diff 报告到 `reconcile/reconcile_*.json`
  - CLI：`export / import / reconcile`
  - 仅 JSON+CSV IO，不连 IBKR API（用户手动/半自动对接）

### Changed

- **`generate_deep_v2.py`** — `generate_html()` 顶部新增 `portfolio_card_html`，每次报告生成时自动 `paper_portfolio.run_for_date(report_date)` 幂等调用 + 渲染卡片，插入在 `exec_summary_html` 之前

### Known Limitations

- Cowork VM 内 yfinance 联网失败，bootstrap 只能创建仓位但无 mark-to-market / 出场触发
- 用户 Mac 端运行时 yfinance 恢复联网，将自动补回历史 OHLC、触发 SL/TP/Time 出场
- Sharpe 返回 None（<2 样本或方差=0）时 fallback 为 0.0

---

## [0.18.0-strategy] — 2026-04-15 — CH4 期权策略建议卡片 + bug 修复三连

### Added — v0.18.0 · CH4 期权策略建议卡片（启发式决策树）

- **`generate_deep_v2.py` 新增 `_recommend_strategy(ctx)`**：IV Rank × 方向三档决策树，9 个核心场景映射到期权结构
  - IV Rank <30：Long Call / Long Put / Long Straddle
  - IV Rank 30–70：Bull Call Spread / Bear Put Spread / Iron Condor
  - IV Rank >70：Bull Put Spread / Bear Call Spread / Iron Condor（收 Premium）
- **7 条修正器（override）**：
  - (1) 催化剂 ≤ 5 天 + IV > 60 + Long Premium → 强制改用 Spread/Sell Premium
  - (2) 事件窗口 + 高 IV → IV Crush 风险警告
  - (3) GEX negative_gex → DTE 缩短到 14–21 天；positive_gex + 强方向 → 延长到 45–60 天
  - (4) 催化剂覆盖：DTE ≥ cat_days + 7 天缓冲
  - (5) Skew > 1.15 + 看多 + Long Call → 备选 Bull Call Spread
  - (6) Skew > 1.15 + 看空 + Long Put → Put 溢价警告
  - (7) 低置信 + 裸 Premium → 强制 Defined Risk + 减仓
- **行权价保守表达**：只给 ATM±% 百分比（ATM / ATM+5% / ATM−5% / ATM+10%），不给具体 strike 数字
- **仓位建议**：`pct_nav = conf_base(1.0/0.6/0.3) × risk_mult(1.0/0.5) × 0.8`，最大 0.8% 账户净值
- **`_render_strategy_card()`**：渐变紫色卡片（区别于其他 CH4 元素），含结构/DTE/行权价/推理链/备选/禁忌/仓位/输入审计
- **集成点**：`generate_html()` 中 `strategy_card_html = _render_strategy_card(_recommend_strategy(ctx))`，插入 CH4 末尾 `<div class="prose">` 后

### Fixed — v0.17.4 Bug 三连

- **ML 胜率小数长尾**（`generate_deep_v2.py:3052, 3760`）：`{ml7}%` → `{ml7:+.1f}%`，`18.507527010901935%` → `+18.5%`
- **催化剂日期 `+-32天`**（`generate_deep_v2.py:1552-1562`）：硬编码 `+` 号导致负数显示异常，改为条件渲染（未来 `+N天` / 过期 `N天前`）
- **明日任务追踪过期财报**（`generate_deep_v2.py:5031`）：`_cats[0]` → `next(c for c in _cats if days_until >= 0)`，跳过已过期催化剂

### Fixed — v0.17.3 二次审计修复

- **P1 `score` NameError**（`generate_deep_v2.py:3445-3450, 3470-3477`）：未定义的 `score` → `_score`
- **P1 + P2 F&G falsy-zero bug**（`3424-3428, 1625-1634`）：`ctx.get('fg_score') or ... or 50` 丢失 valid 0 值 → 显式 None 检查
- **P0 Oracle key 不匹配**（`3621`）：`ctx.get('agents').get('OracleBee')` → `ctx.get('oracle')`（文件其他处统一路径，否则永远回退 5.0）
- **P2 BearBee key 不匹配**（`1618-1623`）：`ctx.get('agents')` key 不存在 → 改为 `ctx.get('bear').get('score')`

### Added — v0.17.4 回测口径 disclaimer（Option A）

- **`generate_deep_v2.py` 历史回测卡片**：加入黄色警示框说明"股票现货策略 vs 期权合约未建仓"的口径差异
- 消除用户将 Net +9.39% 误读为"期权净收益"的最大风险

---

## [0.18.0] — 2026-04-15 — Sprint 1: 真实策略回测（v16.0 起步）

### Added — P0-1 路径依赖退出（intraday 止损止盈）

- **`backtester.py` 新增 `_simulate_trade_path()`**（行 ~640）
  - 拉 T+0 → T+N 每日 OHLC，逐日检查 SL (-5%) / TP (+10%) 是否触发
  - 触发后按阈值价 + 退出滑点（5bp）平仓，返回 `exit_reason` ∈ {TP, SL, T7_CLOSE}
  - 看多：`Low ≤ sl_price` 止损 / `High ≥ tp_price` 止盈
  - 看空：`High ≥ sl_price` 空头止损 / `Low ≤ tp_price` 空头止盈
  - 同日同时触发 SL+TP 时保守假设先 SL（对策略更严格）
- **`run_backtest()` 改造**：T+7 使用路径依赖，T+1/T+30 沿用旧 close-to-close

### Added — P0-2 交易成本 + 借券费模型

- **新增 `trading_costs.py`**：`apply_costs(gross_return_pct, direction, ticker, holding_days)` 一次性扣减滑点（双边）+ 佣金 + 借券费（仅 short）
- **`config.py` 新增 `TRADING_COSTS_CONFIG`**：
  - `slippage_bps_by_ticker`：NVDA 3bp / BILI 15bp / CRCL 25bp 等分档
  - `borrow_rates`：VKTX 15% / CRCL 8% / BILI 4% 等（年化 %）
  - `commission_pct_per_side`：0.01%
- **新增 `sharpe_ratio()`**：年化 Sharpe，T+7 策略 periods_per_year=52
- 自测验证：BILI 空头 +11% gross → net 10.67%（扣 0.39% 成本）

### Added — P0-3 复利 Equity Curve + SPY 基准

- **`dashboard_renderer.py::_load_accuracy_data()` 重写 Equity Curve**（行 655+）
  - 三条曲线：Gross（不扣成本参考）/ Net（真实可交易）/ SPY（买入持有）
  - 复利：每笔 $100k × 10% 仓位（`PORTFOLIO_CONFIG.position_size_pct`）
  - `trading_stats` 输出 Sharpe / Profit Factor / Max DD / Alpha vs SPY / Win Rate
- **`templates/dashboard.js` 新增 `initTradingStats()` + 3 条曲线渲染**
  - 12 个真实交易指标卡片（Net/Gross/SPY 收益、Sharpe、PF、SL/TP 统计等）
  - 曲线 tooltip 显示具体成交原因（SL/TP 触发）

### Changed — DB schema 迁移（幂等 ALTER）

- `predictions` 表新增 7 列：`net_return_t7` / `exit_reason` / `exit_date` / `exit_price` / `holding_days` / `cost_breakdown`（JSON）/ `spy_return_t7`
- `PredictionStore.update_t7_path_result()` 一次性写入所有新字段

### Added — 历史数据回填

- **新增 `backfill_trading_costs.py`**：对 191 条 T+7 已验证记录重新路径模拟 + 扣成本
- **回填结果**（2026-04-15）：
  - 53 笔（27.7%）触发 -5% 止损，23 笔（12.0%）触发 +10% 止盈，115 笔（60.3%）持有到 T+7
  - 真实准确率：**53.4%**（旧"T+7收盘胜率"约 67% 是纸面幻觉）
  - Net 累计：**+9.39%**（6周），SPY 同期 **-12.78%**，**Alpha +22.18%**
  - Sharpe (Net) 0.37，Profit Factor 1.19 — 策略微盈利但波动大
  - 13 笔原"T+7 方向正确"记录被 SL 打断 → 证明之前指标虚高

### 方法学免责声明（UI 文字）

- 网站新增明确标注："Gross 曲线不扣成本（参考），Net 曲线 = 真实可拿收益"
- 每笔按 $100k × 10% 仓位建仓，-5% 硬止损 / +10% 止盈，扣滑点 + 佣金 + 借券费

---

## [0.17.3] — 2026-04-15

### Added — Executive Summary 多因素裁决引擎（P0）

- **`_build_executive_summary()` confidence_score 计算**（行 3564-3591）
  - 公式：`score - 0.8×dim_std - 0.6×bear_sig_count - 0.5×ml_swarm_gap + 0.3×flow_align`
  - 结果 clamp 到 [0, 10]，替代原单变量 verdict switch
  - dim_std 惩罚分歧度、bear_sigs 惩罚反向信号、ml_swarm_gap 惩罚时序×截面矛盾、flow_align 奖励期权流一致性

- **三档置信度标识**（行 3593-3610）
  - ⭐⭐⭐ 高置信（绿）：违反 0 项
  - ⭐⭐ 中置信（橙）：违反 1 项
  - ⚠️ 低置信（红）：违反 ≥2 项
  - 违反条件：dim_std ≥ 1.5 / bear_sigs 激活 / ml_swarm_gap > 0.5

- **三对矛盾检测告警卡片**（行 3612-3633）
  - 红条：OracleBee 看多（≥6.0）vs BearBee 激活反向信号
  - 黄条：Options Flow 看多 vs GEX 正 Gamma 抑制（或看空 vs 负 Gamma 放大）
  - 黄条：Swarm vs ML 7d 方向分歧（时序×截面）
  - HTML 渲染：彩色左边框 + 浅色底，内联置信 tier 胶囊

### Added — Risk Narrative 正向支撑盘点（P1）

- **`_build_risk_narrative()` fallback 重写**（行 3415-3456）
  - 无风险时不再输出泛泛 "当前无高优先级风险"
  - 改为按优先级提取 Top 3 正向支撑（GEX > ML/Swarm 共振 > IV > F&G > Flow > 催化剂缓冲期）
  - 6 个评估维度：GEX 环境 + Call Wall / ML-Swarm 同向共振 / IV Rank 中性或偏低 / F&G 正常区间 / Flow 与 Swarm 一致 / 7 天内无催化剂
  - 输出为有序列表，每条引用具体数值

### Added — Catalyst Narrative 追加 3 个交叉维度（P2）

- **`_build_catalyst_narrative()` 新增 E/F/G 条件**（行 1694-1725）
  - (E) 催化剂 × BearBee：<4.0 防守 → 下行风险被忽视；>6.5 无信号 → 尾部风险被低估
  - (F) 催化剂 × F&G：<25 恐慌 → 反转行情非对称上行；>75 贪婪 → "不及预期"即回调
  - (G) 催化剂 × 信号拥挤度：decay < 0.8 → 符合一致预期时 alpha 迅速衰减
  - 上下文新增读取：`bear_signals`、`agents.BearBee.score`、`fg_score`、`signal_crowding.alpha_decay_factor`

### Changed — 版本号

- 文件头 `VERSION = "0.17.3"`（第 25 行）

---

## [0.17.2] — 2026-04-15

### Fixed — P6 逐到期日推理去模板化 + Bug 修复

- **`generate_deep_v2.py` `_build_options_narrative()` P6 重写**（行 1948-2354）
  - 根因：原 P6 每个到期日输出相同的 "Call 触及阻力位"、"正 Gamma 需超大成交量"、"Ex-Div 催化剂" 三段，只换数字，用户反映 "量化分析作用不足"
  - 循环前预计算 6 个跨期排名：总溢价 / Put 笔数 / 单笔集中度 / Strike 宽度（最窄/最宽）/ 平均 OTM
  - 六层差异化推理结构：Layer 1 独特身份（主战场 / 对冲集中 / 鲸鱼押注 / 窄带信念 / 分散投机 / 彩票型）— Layer 2 Call/Put $ 比具体倍数 — Layer 3 vs 前一到期日 delta — Layer 4 DTE 维度（≤7/≤21/≤45/>45 四档）— Layer 5 集中度（阈值 50%）— Layer 6 支撑阻力（仅 Call 溢价 #1 触发）
  - 移除逐到期日循环中的全局重复：GEX 政体评论移至跨期综合段、宽松催化剂匹配改为严格 0-5 天匹配
  - 跨期综合段增加 "GEX 政体为 {regime}，详见 P3" 避免漏信息

### Fixed — P6 二次审计修复的 6 处真实 bug

- **空 `all_strikes` 列表崩溃**：Layer 1 tightest/widest 分支添加 `_exp_metrics[_exp_date]['all_strikes']` 非空 + `len(_sorted_exps) > 1` 双重守护，避免单到期日或无 strike 数据时 `min([])` ValueError
- **`put_count` 排名触发空数据**：添加 `put_count > 0 and bear_flows` 守护，避免全部到期日无 Put 时输出 "Put 笔数 0 笔" 无意义文本
- **`bear_flows` sum 除零**：`_total_bear_prem_all` 预计算并守护 `> 0`，避免 `/sum()` 除零错误
- **cp_ratio 异常哨兵值**：Layer 2 添加 `_exp_total_prem > 0` 前置守护 + 分离"仅 Call" / "仅 Put"分支，避免 cp_ratio=1.0 默认值落入错误分支、999 哨兵值输出 "999.0x" 丑陋文本
- **Layer 3 ratio 变化语义错误**：添加 `0 < _cp_ratio < 900` 有效区间过滤 + 修正描述逻辑（0.3→1.0 不再误报 "更集中看多"）
- **Layer 6 `_is_call_leader` 全触发**：原逻辑当所有 `bull_prem=0` 时每个到期日都判定为 leader（所有到期日都贴"核心阻力突破"，退化回模板病），添加 `_exp_bull_prem > 0` 前置守护

### Fixed — v0.17.1 的 11 处 KeyError 风险（旧代码）

- 将所有外部 JSON 数据的 `dict['key']` 取值改为 `.get('key', default)` 安全访问
- 覆盖：`_build_options_narrative()` / `_build_scenario_narrative()` / `_build_risk_narrative()` / `_build_executive_summary()` / IV term structure 渲染 / scenario card HTML / LLM prompt 构建
- 防止某些票数据缺字段时报告崩溃

### Changed — 语义/措辞

- Layer 3 "较近月" → "较前一到期日"（更准确，防止 idx>=2 时误导）
- Layer 3 ratio 变化描述双向化：既区分 "多头倾向更强 / Put 主导度减弱"，也区分 "对冲增强 / 看涨信念减弱"

---

## [0.17.0] — 2026-04-10

### Added（v0.14.0 复盘后 6 项高价值改进）

- **`generate_deep_v2.py` 情景E卡片渲染（P0）**
  - scenario-grid 从4卡→5卡，新增「💥 情景E · 强势看跌」HTML卡片
  - 卡片数据：`sc_e_lo/hi` 回退公式 = max_sup_price × 0.72~0.85
  - 修正卡片C名称：「温和看跌」→「区间震荡」以匹配概率表
  - 所有 probs 列表统一 5 元素，LLM 路径支持 `sc_e` 可选字段

- **`generate_deep_v2.py` OI 异常波动告警（P0）**
  - 日环比 >50% 时生成红色告警卡片（`oi_anomaly` / `oi_anomaly_msg`）
  - 告警嵌入 CH4 期权市场结构章节顶部
  - 提示可能原因：期权到期日结算、数据源范围变更、流动性异常

- **`options_analyzer.py` OI 稳定性修复 — Opex 周跳变根治**
  - 根因：旧策略取 DTE≥7 的前 3 个到期日，Opex 周到期日脱落导致 OI 骤降 60%+
  - 到期日选择：DTE≥3 的前 4 个（扩大覆盖面），标记 DTE<7 为 `near_expiry_set`
  - `total_oi` 双口径：`total_oi`（stable，排除 DTE<7）+ `total_oi_raw`（原始）
  - 稳定口径用于日环比对比，避免虚假异常告警
  - 新增 `OptionsAgent._calc_total_oi()` 静态方法

- **`generate_ml_report.py` 估值快照 + Top-3 Pills（P1）**
  - 新增 `_build_valuation_pills()` 方法，CH1 之后渲染
  - 估值快照：PE(TTM)/Forward PE/PEG/分析师目标价
  - Top-3 Pills：期权/估值/逆向/ML/情绪 5 维度按权重取 Top-3

### Changed

- **`generate_deep_v2.py` So What 推理链增强（P1）**
  - 交易含义新增（2）ML 7日预期 + 蜂群评分 + 信号方向判断
  - 新增（3）历史同类信号胜率（需 ≥5 样本），显示统计优势评估
  - IV-RV 交易含义追加 ML/蜂群评分括号注释
  - `_load_ticker_accuracy` 结果注入 ctx（`aa_hist_win_rate/n/avg_ret_7d`）

- **`generate_deep_v2.py` 情景概率历史校准（P2）**
  - Probability Engine 新增 Bayesian blend 步骤
  - 历史胜率 ≥65% 时微调 pa+，≤35% 时微调 pe+（最大 ±1.2pp，需 n≥10）

- **`generate_deep_v2.py` Charm 方向陈旧检测（P2）**
  - 启动时回溯 5 天 JSON 收集 charm_direction 历史
  - 连续 ≥3 天方向不变时在 CH4 显示⚠️黄色提示

### Fixed

- **v0.14.0 复盘报告已生成** → `v0.14.0-复盘报告-2026-04-10.md`

---

## [0.17.1] — 2026-04-13

### Changed（全报告推理引擎重写：模板填空 → 多维交叉推理）

- **`generate_deep_v2.py` 全 7 个推理函数重写**
  - 核心变更：消灭单维 if/else 固定句式，改为多条件叠加 + 跨章数据引用 + 矛盾检测
  - 每个函数现在引用全局 ctx 中其他章节的数据（蜂群/期权/宏观/催化剂/风险）做交叉验证

- **CH1 `_build_swarm_narrative()`**：维度交叉推理
  - 最强/最弱维度差距分析（分裂 vs 一致 vs 严重分裂）
  - RivalBee vs ScoutBee 矛盾检测，OracleBee vs 实时流方向检测
  - BearBee × 宏观 F&G 共振检测，ChronosBee × IV Rank 定价检测

- **CH2 `_build_resonance_narrative()`**：7 维度叠加推理 + 动态仓位
  - (A) 共振 × 维度离散度，(B) 共振 × GEX 政体，(C) 共振 × IV 环境
  - (D) 共振 × 宏观情绪矛盾，(E) 共振 × 催化剂窗口，(F) 逆向信号对冲，(G) 拥挤度
  - 仓位建议从二元（80% vs 40-60%）改为多因子校准（base ± 调整因子列表）
  - ML/蜂群方向一致性检测 + P/C Skew 矛盾检测

- **CH3 `_build_catalyst_narrative()`**：跨章引用 IV + 异常流
  - (A) 催化剂密度 × IV Rank 状态交叉推理
  - (B) 异常流到期日是否精确覆盖催化剂窗口
  - (C) IV 期限结构 × 催化剂（Backwardation 印证 vs Contango 低估）
  - (D) GEX 政体 × 催化剂波动放大/抑制

- **CH4 `_build_options_narrative()` P6**：按到期日多维推理
  - 6 推理维度：(A) Strike vs 支撑/阻力位，(B) IV 环境 × 时间，(C) GEX 政体
  - (D) 催化剂窗口匹配，(E) 溢价集中度，(F) 跨到期日方向对比
  - 跨到期日综合研判段（近多远空 / 全线看涨 / GEX 矛盾/共振）

- **CH5 `_build_macro_narrative()`**：6 条件叠加推理
  - (A) F&G × 蜂群方向矛盾/印证（恐慌+看涨=买入窗口 vs 恐慌+看跌=双重压制）
  - (B) F&G × VIX 期限结构交叉（恐慌+Backwardation=实质危机 vs 恐慌+Contango=情绪驱动）
  - (C) 情绪动量 × 期权流方向矛盾检测（散户乐观+机构对冲=期权市场常对）
  - (D) Reddit 热度 × 成交量交叉，(E) 国会交易 × 蜂群方向，(F) 催化剂 × 宏观环境

- **CH6 `_build_scenario_narrative()` 决策树动态化**
  - 看多/看跌路径从 3 条固定规则 → 3-4 条动态规则（基于实际异常流到期日/strike/溢价）
  - 引用 GEX 翻转点、实际 Call Wall、异常流兑现窗口
  - 催化剂窗口内的规则引用具体事件名和天数

- **CH7 `_build_risk_narrative()`**：7 条风险并行检测（非互斥）
  - 旧：if/elif/else 4 条互斥分支只输出 1 段 → 新：7 条独立检测全部命中即输出
  - (1) 宏观恐慌×蜂群方向，(2) Skew×异常流印证，(3) 逆向信号×方向对冲
  - (4) GEX 政体×翻转点位置，(5) 催化剂×IV×异常流覆盖，(6) 拥挤度，(7) 支撑位×GEX 联动
  - 警戒线新增动态项（异常流缩减预警、GEX 翻转点）

### Added

- **新函数 `_build_cross_chapter_synthesis()`**——跨章综合研判面板
  - 信号一致性评分：7 维信号（蜂群/ML/期权流/P&C/异常流资金/F&G/逆向）→ 方向分类 → 一致性标签
  - 信号方向 Pill 可视化（▲看涨 / ▼看跌 / ●中性）
  - 矛盾检测引擎（蜂群 vs F&G / 蜂群 vs 期权流 / ML vs 蜂群），每对矛盾独立卡片输出
  - 时间维度对齐（异常流到期日 vs 催化剂窗口重合度 / IV 期限结构 vs 异常流分布）
  - 信号权重优先级判断（GEX 政体→期权权重 / 催化剂 5 天内→事件驱动优先 / 拥挤度→打折）
  - 渲染位置：Executive Summary 下方、CH1 上方

- **`options_analyzer.py` 异常流检测 5 条件 + 多到期日扫描 + 无截断**
- **`unusual_options.py` 移除 `[:5]` 截断**
- **`generate_deep_v2.py` CH4 异常流渲染按到期日分组**

---

## [0.16.0] — 2026-04-09

### Removed（Probability Boost 禁用）

- **`generate_ml_report.py` + `generate_deep_v2.py` Probability Boost 评分加成已禁用**
  - 根因审计发现 `probability_analysis` 数据源不可靠：
    - `risk_reward_ratio=9.0` 来自仅 1 条 `similar_opportunity`（sample_size=2），统计上是噪声
    - `win_probability_pct=65.0` 是硬编码启发式公式（base 55% + 拥挤度 ± 催化剂），无实际新信息
    - 两个值连续两天（4/8、4/9）完全相同，证明 boost 只是固定偏移量而非市场信号
  - 影响：NVDA 4/9 评分从 9.0（撞天花板）回归蜂群原始 7.53
  - 保留审计字段 `probability_boost.disabled=True`，报告卡片可展示"未启用"状态
  - TODO: 待 `probability_analysis` 改用真实贝叶斯模型（sample_size≥30 + 动态校准）后重新启用

### Changed

- **`generate_ml_report.py:1543` checkpoint 恢复加日期校验**
  - glob `.checkpoint_*.json` 现在双保险校验（文件名日期 + saved_at），防止跨天 stale 复用

---

## [0.15.3] — 2026-04-08

### Changed（Checkpoint 日期隔离 — 上游根治）

- **`alpha_hive_daily_report.py` checkpoint 文件名加日期后缀**
  - 旧：`.checkpoint_{session_id}.json` → 新：`.checkpoint_{session_id}_{YYYY-MM-DD}.json`
  - 跨天自然隔离：今天的进程根本不会打开昨天的文件，从物理层消灭 stale Oracle details（2026-04-06 timestamp 事故根因）
  - 启动时自动清理同 session 的历史日期 checkpoint，避免 report/ 目录累积
- **`_load_checkpoint()` 双保险日期校验**
  - 除原有 `saved_at` 内容字段校验外，新增文件名日期匹配检查
  - 任一不匹配即丢弃结果、从头运行
- 与 v0.15.2 OptionsSnapshot 形成完整闭环：上游 checkpoint 隔日 + 下游 snapshot 日内共享，两层防御 swarm 数据错位

---

## [0.15.2] — 2026-04-08

### Added（期权快照根治方案）

- **`options_analyzer.py` OptionsAgent.analyze() 新增 per-ticker-per-date 冻结快照**
  - 根治 v0.15.1 发现的两条路径期权数据分裂问题——从渲染层 fallback 升级为数据层统一
  - 入口读取：`cache/options_snapshot_{TICKER}_{YYYY-MM-DD}.json`，命中则直接返回
  - 出口写入：首次计算后将完整 result dict 连同 `_snapshot_timestamp/_snapshot_ticker/_snapshot_stock_price` 持久化
  - 跨进程共享：`alpha_hive_daily_report.py`（swarm）和 `generate_ml_report.py`（advanced）两个独立进程通过文件系统共享同一快照，首个调用者"冻结"当日数据
  - 跨午夜保护：校验 `_snapshot_timestamp` 日期与当前日期一致，过期则忽略并重算
  - 旁路机制：
    - `OptionsAgent.analyze(ticker, stock_price, force_refresh=True)` 强制重算
    - 环境变量 `OPTIONS_SNAPSHOT_DISABLE=1` 全局禁用快照
  - 失败降级：JSON 读写异常时自动 fallback 到重新计算，不阻塞主流程

### Changed

- **OracleBee / advanced_analyzer / BearBee 自动受益**：所有调用 `OptionsAgent.analyze()` 的模块无需修改代码，自动共享同一快照视图
- **v0.15.1 的 extract() fallback 合并逻辑保留**：双保险设计，即使快照失效也能从 advanced_analysis 兜底

---

## [0.15.1] — 2026-04-08

### Fixed（期权数据源分裂）

- **深度报告 vs GitHub ML 报告期权数据不一致**
  - 问题：同一份 JSON，两份报告显示完全不同的期权数值（IV Rank 29.6 vs 55.95、P/C None vs 0.79、GEX None vs 215.9、unusual_activity 2 条 vs 10 条）
  - 根因：`swarm_agents/oracle_bee.py` 和 `advanced_analyzer.py` 分别独立调用 `OptionsAgent.analyze()`，发生在不同时刻的不同进程，yfinance 返回两个不同的期权链快照。OracleBee 经常拿到降级数据（字段缺失）
  - 证据：hist_iv cache [min=23.93, max=57.45]，current_iv=42.69 → iv_rank=55.95；current_iv=33.85 → iv_rank=29.60。两条路径的 current_iv 相差 9 个点
  - 修复：`generate_deep_v2.py` extract() 第 405 行，期权字段优先从 `advanced_analysis.options_analysis` 读取，OracleBee.details 仅作 fallback。合并逻辑：`odet = {**_odet_raw, **{k:v for k,v in _oa_opts.items() if v is not None}}`
  - 影响字段：iv_rank / iv_current / put_call_ratio / total_oi / iv_skew / flow_direction / options_score / unusual_activity / key_levels / gamma_exposure
  - 验证：NVDA 2026-04-08 所有期权字段现与 GitHub ML 报告一致

---

## [0.15.0] — 2026-04-08

### Added（第 6 维融合：Probability Boost）

- **核心修复：两条评分路径分裂**
  - 问题：`swarm_results.final_score`（蜂群 5 维加权）和 `advanced_analysis.probability_analysis`（Kelly 胜率/赔率）互不相通，导致深度报告（4.85 中性）与 GitHub ML 报告（65.8% BUY）结论分裂
  - 方案：在 `generate_ml_report.py` 合并 swarm_data 时注入 Probability Boost，把 probability_analysis 作为"第 6 维"对 swarm final_score 后处理加成

- **`generate_ml_report.py` ~line 1622 新增 Probability Boost 逻辑**
  - 触发条件：`win_prob ≥ 60%` 且 `risk_reward ≥ 5` 且 `direction != bearish`
  - 公式：
    - base_boost = `min(2.5, (win_prob - 50) / 10)`  — 60%→1.0, 65%→1.5, 75%→2.5 cap
    - rr_mult = `min(1.5, rr / 5)`  — rr 5→1.0x, 7.5→1.5x cap
    - raw_boost = base × mult
    - bear_hedge = `min(raw × 0.6, (bear_strength - 6) × 0.2)` when bear ≥ 6
    - final_boost = raw - hedge, clamp [0, score clamp [1,9]]
  - 方向翻转：若 old_dir=neutral 且 new_score ≥ 5.8 → bullish
  - 审计字段：`swarm_results.probability_boost` 记录 win_prob/rr/boost/before/after/reason

- **`generate_deep_v2.py` extract() 新增 3 字段**
  - `probability_boost`（审计 dict）
  - `win_probability_pct` / `risk_reward_ratio`（从 advanced_analysis 直读）

- **`_build_odds_boost_card()` 新函数**
  - 4 格 grid：胜率 / 赔率 / 加成 / 评分 before→after
  - 高 bear_strength 时显示"bear X.X 对冲XX%"标签
  - 方向翻转时显示 "→ bullish" 绿色标签
  - 未触发时渲染灰色 dashed 卡片说明原因
  - 嵌入 Executive Summary 底部

- **验证案例：NVDA 2026-04-08**
  - 输入：win=65% rr=9.0x bear=7.61 old_score=4.85 neutral
  - 计算：base 1.50 × mult 1.50 = 2.25 − bear hedge 0.32 = **+1.93**
  - 输出：**4.85 → 6.78 bullish** ✅（成功抵消 Scout 3.42 + Guard 3.37 的拖累）

---

## [0.14.0] — 2026-04-04

### Added（估值分析 + 叙事升级 7 项）

- **V1: 估值快照卡片**（`generate_deep_v2.py` `extract()` + `_build_valuation_card()`）
  - `extract()` 新增 6 个估值字段：forward_eps / trailing_eps / eps_growth / analyst_target / analyst_consensus / analyst_count（来自 RivalBee eps_revision）
  - 新函数 `_build_valuation_card(ctx)`：4 格 grid（PE TTM / PE Forward / PEG / 分析师目标价）+ PE 倍数情景矩阵（5 档：深度衰退 18x → 泡沫 35x）
  - PEG 颜色分级：<1 绿色（低估）/ 1-2 金色（合理）/ >2 红色（偏贵）
  - 分析师共识映射：1-1.5 强烈看多 / 1.5-2.5 看多 / 2.5-3.5 中性 / 3.5-4.5 看空 / 4.5+ 强烈看空

- **V2: 情景价格锚定至 PE 倍数**（`generate_deep_v2.py` `_build_scenario_narrative()`）
  - 5 个情景的收益率改为 Forward EPS × PE 倍数计算（有 forward_eps 时优先）
  - 方向感知 PE 区间：看多 32/26/18/14x，看空 28/24/16/12x
  - 情景表格新增 "PE×EPS→$xxx" 价格标注
  - 估值卡片嵌入 CH6 情景推演章节顶部

- **N1: "So What" 推理链升级**（`generate_deep_v2.py` `_build_options_narrative()`）
  - P1 期权结构段末新增交易含义推理（基于 IV Rank + P/C Ratio + 异常流方向）
  - P2 IV-RV 段末新增恐慌超额/方向性机会判断（IV-RV > 5 / < -5 分支）
  - 催化剂窗口联动：自动关联最近催化事件

- **N2: Top-3 核心论点提炼**（`generate_deep_v2.py` `_build_executive_summary()`）
  - 从 7 只蜂中提取 thesis 候选（期权/估值/催化剂/GEX/看空/情绪）
  - 按信号强度排序取 Top-3，渲染为彩色标签 pills
  - 嵌入 Executive Summary 底部

### Fixed

- **B1: GEX 政体重复文案**（`_build_options_narrative()` ~line 1453）
  - 新增 `positive_gex` / `negative_gex` 专用解释文案，消除 "GEX 政体为 X——GEX 政体为 X" 重复
- **B2: Charm 方向重复文案**（`_build_options_narrative()` ~line 1488）
  - 新增 `bullish` / `bearish` 分支（与 `positive` / `negative` 并列），消除 Charm 重复
- **估值卡片 f-string 条件拼接 bug**
  - `f'...' if cond else '' f'...'` 模式导致仅渲染首个卡片；重构为 list append + join

---

## [0.13.0] — 2026-03-28

### Added（深度报告 8 项功能升级）

- **P1: 仓位管理出场计划**（`generate_deep_v2.py` `_build_scenario_narrative()`）
  - 新增 position_management 字段提取（stop_loss/take_profit/optimal_holding_time）
  - CH6 P5 卡片：止损位（保守/中等/激进）+ 分批止盈表格（目标价/减仓比例/理由）
  - 建议持仓天数范围显示

- **P2: 历史回测 Analog 相似机会**（`generate_deep_v2.py` `_build_swarm_narrative()`）
  - 新增 historical_analogs + expected_returns 字段提取
  - CH1 analog_html 卡片：历史相似信号回测表（日期/事件/T+7/T+30/最大回撤/结果）
  - 样本统计：样本量、平均最大回撤率

- **P3: Max Pain 做市商磁吸位**（`generate_deep_v2.py` `_build_options_narrative()`）
  - 新增 max_pain 字段提取
  - CH4 P3 GEX 段落注入：Max Pain 价位显著提升可信度

- **P4: 情绪动量与背离信号**（`generate_deep_v2.py` `_build_macro_narrative()`）
  - 新增 sentiment_pct/sentiment_momentum/sentiment_divergence/volume_ratio 字段提取
  - CH5 新增 sent_html 卡片：舆情情绪%、动量方向（上升/下降）、看多/看空背离、成交量比
  - 背离检测：价跌情绪升（看多背离）或价涨情绪降（看空背离）自动标记 ⚠️

- **P5: 内部人交易 + 做空比率**（`generate_deep_v2.py` `_build_risk_narrative()`）
  - 新增 insider_trades + short_interest 字段提取
  - CH7 风险章插入内部人信息：净买入/卖出、交易笔数、做空比率等级（高/中等/正常）
  - 颜色映射：做空>10%（红）、5-10%（金）、<5%（灰）

- **P6: 行业竞争格局评分**（`generate_deep_v2.py` `_build_swarm_narrative()`）
  - 新增 industry_comparison 字段提取（竞争对手、竞争力评分、优势/威胁）
  - CH1 industry_html 卡片：竞争力评分（0-100）+ 竞争对手列表 + 优势/威胁标签云

- **P7: ML 特征透明化**（`generate_deep_v2.py` `_build_swarm_narrative()`）
  - 新增 ml_input/ml_recommendation/ml_probability/ml_3d 字段提取
  - CH1 ml_html 卡片：推荐方向（bold）、概率%（含颜色）、特征列表（标签云）、预期收益（3/7/30d）

- **P8: Deep Skew IV 微笑曲线**（`chart_engine.py` 新增 `render_deep_skew_chart()`）
  - 新增 `render_deep_skew_chart(data, ticker, date_str) → base64 PNG` 函数
  - 数据源：`oracle_bee.details.deep_skew`（dict of {delta:iv} 或 list of {delta,iv}）
  - 曲线图：Delta vs IV，带 ATM 标记虚线、曲线下填充
  - 可用性：深度 skew 数据不足时静默返回 None

### Changed

- **`extract()` 函数（generate_deep_v2.py line ~600）**：新增 8 个 P1-P8 字段到返回 dict
- **`_build_swarm_narrative()` 返回值**：拼接 analog_html + industry_html + ml_html 三张卡片
- **`_build_macro_narrative()` 返回值**：插入 sent_html 情绪动量卡片
- **`_build_risk_narrative()` 前导**：添加 insider_si_parts 段落

---

## [0.12.1] — 2026-03-27

### Added（Dashboard 高价值可视化增强）

- **Equity Curve 权益曲线**（`dashboard_renderer.py` + `index.html` + `templates/dashboard.js` + `templates/dashboard.css`）
  - `_load_accuracy_data()` 新增 `equity_curve` 字段 — 从 backtester SQLite 查询全部 T+7 验证记录
  - 方向调整收益：bearish 预测自动取反收益，计算真实策略 P&L
  - 累计收益曲线 + 回撤阴影（Chart.js line chart，双数据集）
  - 分段着色：正收益区间绿色，负收益区间红色（`segment.borderColor` 回调）
  - 统计面板：累计收益、最大回撤、方向胜率、平均单笔、已验证笔数
  - Cold state：T+7 数据未就绪时显示等待提示，backfill 后自动激活
  - bfcache 恢复兼容（`pageshow` 事件重建图表）
  - 当前数据：145 笔交易，累计 +204.68%，最大回撤 58.72%，胜率 59.3%

- **蜂群分歧度分析（Swarm Divergence）**（`dashboard_renderer.py` + `index.html` + `templates/`）
  - `render_dashboard_html()` 新增蜂群分歧度计算模块
  - 对 7 只核心蜂（Scout/Rival/Oracle/Chronos/Buzz/Guard/Bear）逐标的统计：
    - 评分标准差（σ）、极差（spread）、共识度（majority%）
    - 方向投票分布（bullish/bearish/neutral 计数）
    - 每只蜂的评分 + 方向柱状图
  - `swarm_divergence` 字段写入 `dashboard-data.json`
  - 可视化卡片：按共识度升序排列（低共识 = 需关注的标的优先展示）
  - 三级共识标签：高共识（≥75%，绿）/ 中等共识（≥55%，橙）/ 低共识（<55%，红⚠️）
  - 方向颜色映射：bull→绿 / bear→红 / neut→橙（修复了初始 `dir[0]` 歧义 bug）

### Fixed

- 蜂群分歧度方向映射 `dir` 字段从 `"b"/"n"` 改为 `"bull"/"bear"/"neut"`，避免 bullish/bearish 首字母 `"b"` 碰撞

---

## [0.12.0] — 2026-03-27

### Added（期权策略回溯测试框架：验证 Scout/Oracle/Bear 推荐）

- **`options_backtester.py`**（新文件）
  - **`OptionsBacktester`** 类 — 回溯测试主框架，从 report_snapshots 加载历史推荐信号
    - `__init__(snapshots_dir)` — 初始化并加载全部快照 JSON
    - `_load_snapshots()` — 从 report_snapshots/ 读取 64+ 份历史记录
  - **策略定义** — 6 种期权策略回溯
    - `StrategyType` enum: `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread`, `iron_condor`, `straddle`
    - `StrategyResult` dataclass — 单笔交易详情（入场价、出场价、DTE、IV、P&L%、最大回撤、政体）
    - `StrategyBacktestResult` dataclass — 策略汇总统计（胜率、平均收益、夏普比、最大回撤、利润因子）
  - **核心方法**
    - `backtest_strategy(strategy, predictions, horizon)` → `StrategyBacktestResult` — 单策略回溯
    - `backtest_all_strategies(predictions, horizon)` → dict — 6 策略全部回溯
    - `find_best_strategy_by_regime(predictions)` → dict{regime → best_strategy} — 按政体优化推荐
    - `generate_strategy_report(horizon)` → formatted string — 完整报告生成
    - `inject_strategy_results_to_report(report_dict, horizon)` → enhanced report — 与 feedback_loop 集成（注入 CH6 场景推荐）
  - **Black-Scholes 期权定价**
    - `estimate_option_pnl(entry_price, exit_price, strike, dte_entry, dte_exit, iv_entry, iv_exit, option_type)` — 单腿期权P&L估算
    - `estimate_spread_pnl(...)` — 价差策略净P&L估算
    - 集成 `greeks_engine.py` 的 `bs_price()`；无法导入时回退到简化版本
    - 支持 call/put 两种期权
  - **市场政体分类** — 5 大政体
    - `MarketRegime` enum: `low_iv_bull`, `low_iv_bear`, `high_iv_bull`, `high_iv_bear`, `neutral`
    - `_classify_regime(snapshot)` — 根据 composite_score 和 direction 推导政体
  - **信号-策略映射**
    - `_map_signal_to_strategy(snapshot)` — 评分阈值映射至推荐策略
      - score > 7.5 + bullish → bull_call_spread
      - score < 4.0 + bearish → bear_put_spread
      - score 5-6 + 高IV → iron_condor
      - 其他 → long_call / long_put
    - `_estimate_strikes_from_price(stock_price, strategy)` — ATM + OTM 行权价自动推导
  - **性能指标计算**
    - 胜率 (win_rate) — 盈利笔数 / 总笔数
    - 平均收益 (avg_return) — 单笔收益百分比均值
    - 夏普比 (sharpe_ratio) — 年化收益 / 年化波动（假设 252 交易日）
    - 最大回撤 (max_drawdown) — 回溯期间最大负P&L
    - 利润因子 (profit_factor) — 总盈利 / 总亏损
  - **演示脚本** (`if __name__ == "__main__"`)
    - 加载 64 份快照，演示 6 策略全部回溯
    - 按政体分类输出最优策略
    - 生成并保存 `strategy_backtest_report.txt`
  - **测试数据**：基于真实报告快照，long_put 策略表现最佳（56.82% 胜率，5.48 夏普比）

### Integration Points（集成点）

- **`feedback_loop.py`** — ReportSnapshot 加载器，提供历史价格数据（actual_prices.t1/t7/t30）
- **`generate_deep_v2.py`** — CH6 "五情景推演" 可调用 `OptionsBacktester.inject_strategy_results_to_report()` 注入最优策略建议
- **`greeks_engine.py`** — Black-Scholes 定价，无法导入时使用内建简化模型
- **`report_snapshots/`** — 数据源（64+ JSON 快照，包含历史推荐和实现价格）

### Added（分析深度升级 + 新数据源 + RL 桥接）

- **`vol_surface.py`**（新文件，~1004 行）
  - `sabr_implied_vol()` — Hagan 2002 SABR 波动率曲面模型
  - `_nelder_mead_minimize()` — 纯 Python Nelder-Mead 优化器（无需 scipy）
  - `SABRCalibrator` 类 — SABR 参数校准 + smile 生成 + skew 异常检测
  - `VolSurface` 类 — 多到期日曲面构建、25Δ Risk Reversal / Butterfly 计算、曲面异常检测
  - `format_surface_for_report()` / `format_skew_alert()` — CH4 HTML 卡片输出

- **`cboe_fetcher.py`**（新文件，~350 行）
  - `CBOEDailyFetcher` 类 — 5 个 CBOE 市场指标
    - `fetch_equity_putcall_ratio()` — 股票期权看跌/看涨比
    - `fetch_vix_term_structure()` — VIX 期限结构（Contango/Backwardation）
    - `fetch_skew_index()` — CBOE SKEW 尾部风险指数
    - `fetch_vvix()` — VIX 的波动率（波动率之波动率）
    - `fetch_all()` — 一键获取全部指标
  - 智能缓存：盘中 30 分钟 / 盘后 4 小时 TTL
  - `format_cboe_for_macro_card()` — 宏观情绪 HTML 卡片

- **`quiver_fetcher.py`**（新文件）
  - `QuiverFetcher` 类 — 国会议员交易信号
  - `calculate_congressional_signal()` — 政客加权买卖信号（Pelosi 2x 权重）
  - `calculate_policy_alpha()` — 交易(60%) + 合同(40%) 复合政策 alpha
  - `format_congressional_card_html()` — Scout 蜂发现层 HTML 卡片
  - 4 小时/24 小时分级缓存

- **`finrl_bridge.py`**（新文件，~767 行）
  - `SimpleQTable` — 纯 Python Q-learning 表格式 RL
  - `FinRLBridge` 类 — 三层降级架构：FinRL+SB3 → Q-learning → 等权重默认
  - `train_weight_policy()` — 从 report_snapshots 训练权重策略
  - `compare_rl_vs_current()` — RL 建议 vs 当前权重对比
  - `detect_regime_shift_rl()` — 基于 RL 的市场政体转换检测
  - 最低 30 份快照才启动训练，仅输出建议不自动覆写

### Changed（回测与自学习系统升级）

- **`feedback_loop.py`**
  - **[P0 关键修复]** Sharpe 比率从虚假平均值改为真实逐笔收益计算
  - 新增 `direction_adjusted_returns` 列表收集实际 T+7 逐笔收益
  - 新增 `_calculate_sharpe()` — 使用真实收益 + 252/7 年化周期
  - 新增 `_calculate_profit_factor()` — 总盈利 / 总亏损
  - 新增 `_calculate_information_ratio()` — vs SPY 基准超额收益 / 跟踪误差
  - 新增 `_calculate_max_consecutive_losses()` — 连续亏损计数器
  - Dashboard HTML 新增 Profit Factor 和 Max Consecutive Losses 卡片

- **`weekly_optimizer.py`**
  - 新增 `compute_new_weights_wls()` — WLS 加权最小二乘法 + 指数时间衰减 `exp(-days_ago/30)`
  - 新增 `bootstrap_validate()` — 500 次 Bootstrap 重采样验证，95% CI 稳定性检查
  - `main()` 优先 WLS → 标准方法回退 → Bootstrap 验证

- **`advanced_analyzer.py`**
  - 新增 `_calculate_flip_acceleration()` — GEX 翻转加速度（dGEX/dPrice 斜率 + urgency 分级）
  - 新增 `_vanna_stress_test()` — Vanna 压力测试（vol shock → GEX 偏移 → 翻转概率判断）
  - GEX 归一化 `gex_normalized_pct` — 占 OI 名义值百分比，跨标的可比
  - `analyze()` 返回 `flip_acceleration` + `vanna_stress` 新字段

### Changed（报告流程集成 · 6 模块接入 generate_deep_v2.py）

- **`generate_deep_v2.py`** — main() 新增 6 个数据丰富步骤（2a-2 ~ 2a-6）：
  - 2a-2: `vol_surface.py` SABR 曲面分析 → CH4 嵌入曲面卡片 + Skew 异常警报
  - 2a-3: `cboe_fetcher.py` CBOE 市场指标 → CH5 嵌入宏观情绪卡片
  - 2a-4: `quiver_fetcher.py` 国会交易补充 → 当 Scout 蜂未提供时自动回退 Quiver API
  - 2a-5: `finrl_bridge.py` RL 权重建议 → CH1 嵌入建议卡片（advisory, ≥30 快照才启动）
  - 2a-6: `options_backtester.py` 策略回测 → CH6 嵌入按政体推荐最优策略表格
  - 全部步骤 try/except 包裹，失败静默跳过不影响报告生成
- **`generate_deep_v2.py`** — extract() 新增 `flip_acceleration` / `vanna_stress` / `gex_normalized_pct` 字段
- **`generate_deep_v2.py`** — generate_html() CH4 新增 GEX 增强卡片（翻转加速度 + Vanna 压力 + 归一化%）
- **`generate_deep_v2.py`** — `_load_ticker_accuracy()` 新增 Sharpe / Profit Factor / 最大连败计算
- **`generate_deep_v2.py`** — `_render_accuracy_card()` 第二行新增 Sharpe / PF / 最大连败展示

### Fixed（Bug 修复）

- **`vol_surface.py`** — SABR z 参数公式错误：`z_denominator` 多乘了一个 `alpha`，导致 IV 计算偏移；已修正为 `fk_mid` 独立计算
- **`vol_surface.py`** — 浮点 sqrt 防护：`disc < 0` / `denom_chi ≈ 0` / `arg ≤ 0` 三重 guard，防止 `math.sqrt` 和 `math.log` 崩溃
- **`vol_surface.py`** — D 变量死代码：else 分支中 D 被赋值两次，第二次覆盖第一次；已清除冗余计算
- **`advanced_analyzer.py`** — `_vanna_stress_test` 签名新增 `total_gex` 参数，`can_flip_gex` 从 `!= 0`（几乎永真）改为 `abs(vanna_impact) > abs(total_gex) * 0.5`（语义正确的翻转判断）
- **`feedback_loop.py`** — **[P0]** Sharpe 比率使用 `[avg_return] * N` 重复同一值，导致标准差趋近 0、Sharpe 虚高；已改为逐笔真实收益
- **`generate_deep_v2.py`** — `build_surface()` 接口不匹配：无参调用 → 从 JSON options_chain 提取数据传入，返回值 `None` → 改用 `_vs.slices` 属性检查
- **`generate_deep_v2.py`** — `options_backtester.to_dict()` key 不匹配：`"avg_return"` → `"avg_return_pct"`，修复前策略均收列永远显示 0%
- **`generate_deep_v2.py`** — `flip_acceleration` key 不匹配：`"slope"` → `"acceleration"`，修复前翻转加速度永远显示 0

---

## [0.11.0] — 2026-03-26

### Added（投行级报告全面升级：执行摘要 / 五情景引擎 / 三图表 / 交叉引用）

- **`generate_deep_v2.py`**
  - **`_build_executive_summary(ctx)`**（新函数）— 渐变卡片，含最终评分、裁决词、ML7 置信区间、催化剂/风险/拥挤度摘要、最强维度、操作建议；注入 CH1 之前
  - **`_build_scenario_narrative(ctx)`**（全部重写）— 三段改五情景：
    - 动态概率引擎：评分段 → ML7 调整 → 催化剂调整 → 拥挤度调整 → PEAD 调整 → 归一化至 100%
    - 五情景 HTML 概率表（大牛/牛/中性/熊/大熊），含因果链描述
    - 回报区间使用真实 key_levels 行权价计算
    - 期望值（EV）公式：`ev = (pa·ra + pb·rb + ... + pe·re) / 100`
    - If-Then 双列决策树 div（绿色多头路径 / 红色止损路径）
    - 期权策略匹配（基于 IV Rank / Skew / IV-RV）
  - **`_try_charts(ctx)`**（更新）— 返回 5 元组，新增 radar / iv_term / gex_profile 三图
  - **`generate_html()`**（更新）：
    - 解包 5 图：`conf / opts / radar / iv_term / gex_profile`
    - 注入 `exec_summary_html`（gen-notice 后）
    - 注入 `dod_delta_html`（DoD 跟踪：评分 Δ / IV Current Δ / P/C Δ / 政体变化）
    - CH1 插入雷达图；CH4 插入 IV 期限结构图 + GEX Profile 图
    - 导航栏新增 `📋 摘要` 锚点；CH6 标题改为"五情景推演"
  - **交叉章节引用**：所有 `_build_*` 函数末尾注入 `(见第X章...)` 显式引用链
  - **26 闲置 ctx 字段分配**：
    - CH1：`overview`、`hist_accuracy`
    - CH2：`signal_summary`、`supply_chain`
    - CH3：`pead_summary`、`pead_bias`
    - CH4：`iv_crush_summary`、`otm_put_iv`、`otm_call_iv`、`iv_skew_signal`、`options_score`
    - CH5：`signal_crowding`（crowding badge）、`cycle_context`、`market_regime`
    - CH6：`band_width`（置信区间宽度）
    - CH7：`regime` + `gex_regime` 跨维交叉
  - **DoD Delta 扩展**：新增 IV Current 日环比 Δ、P/C Ratio 日环比 Δ、政体变化检测

- **`chart_engine.py`**
  - **`render_radar_chart(data, ticker, date_str)`** — 极坐标蜘蛛图，7只蜂归一化 [0,10] 分，金色参考环标注 final_score
  - **`render_iv_term_chart(data, ticker, date_str)`** — IV 期限结构折线图，形态配色（Contango=绿/Backwardation=红/Flat=金），IV Current 参考线
  - **`render_gex_profile_chart(data, ticker, date_str, current_price)`** — GEX 分布条形图，±30% 价格区间过滤，绿正红负，含当前价 + GEX flip 标记线

### Fixed（`_build_options_narrative()` 5项 Bug 修复）

- **`generate_deep_v2.py`**
  - **BUG-A**：`iv_rank=0` 被 `or 50` 短路为 50 → 改为 `if _ivr_raw is not None` 显式判断
  - **BUG-B**：`gamma_exposure='N/A'`（字符串）传入 `{:+,.0f}` 格式化崩溃 → `try/except float()` 包裹
  - **BUG-C**：`charm_interp` 末尾含 `。`，外层拼接再加 `。` 导致双句号 → 去掉 charm_interp 内部结尾标点
  - **BUG-D**：`flow='neutral'` 误用 `bear-text` CSS 类 → 三路判断：bull / bear / neutral-text
  - **BUG-E**：`total_oi` 可能为字符串类型 → `float(ctx.get('total_oi', 0) or 0)` 兜底

---

## [0.10.6] — 2026-03-20

### Changed（FF6 归因接入 Claude 连贯推理）

- **`generate_deep_v2.py`**
  - FF6 归因计算从步骤 3.5（LLM 后）**前移到步骤 2.6**（LLM 前），结果存入 `ctx["ff6_block"]`
  - `_ff6_block` 注入 **`swarm_analysis`（CH1）** 和 **`risk`（CH7）** 的 step1 prompt
  - CH1 建立 `master_thesis` 时已含 FF6 结论，后续 CH2~CH7 通过 `_master_block` 链式继承
  - 格式：`【FF6 因子归因（244日）】Alpha年化+31.6%(t=+1.4,不显著) | R²=73.7% | β_Mkt-RF=+1.56*** ...`
  - 归因失败时 `ctx["ff6_block"]=""` 静默跳过，不影响报告生成

---

## [0.10.5] — 2026-03-20

### Changed（FF6 归因集成到深度报告）

- **`generate_deep_v2.py`**
  - `generate_html()` 新增 `attribution_html: str = ""` 参数
  - 步骤 3.5 调用 `compute_factor_attribution(ticker, 252)`，失败时静默跳过（不中断报告）
  - HTML 模板新增 **CH8 · 第八章 · Fama-French 6 因子 Alpha 归因**，位于 CH7 风险章节之后、免责声明之前
  - CH8 节点条件渲染：`attribution_html` 为空时完全隐藏，不影响现有报告结构

- **`factor_attribution.py`** — Bug 修复（5 项）
  - **[高]** `_get_stock_returns`：`tz_localize(None)` → 改为 `pd.Timestamp(d.date())` 重建索引，修复时区偏移导致与 FF6 日期对不上（交叉日为 0）的关键 bug
  - **[高]** `_ols` 兜底路径：`math.erf()` → `scipy.special.erf()`，修复 numpy 数组传入标量函数的 TypeError
  - **[高]** `_build_summary`：加 `if not factors:` 守卫，修复 `max({}.items())` 空序列 ValueError
  - **[高]** `_download_ff5/mom`：加 `threading.Lock` + double-check，修复多线程并发写 parquet 缓存冲突
  - **[中]** MOM 列名大小写：`Mom` → rename to `MOM`（已在 v0.10.4 修复，此处补记）

---

## [0.10.4] — 2026-03-20

### Added（因子归因引擎）

- **`factor_attribution.py`**（新文件，项目根目录）— Fama-French 6 因子 Alpha 归因
  - 数据源：Kenneth French Data Library（直接 HTTP 下载 ZIP，24h 本地缓存 `.factor_cache/`）
  - FF6 = FF5（Mkt-RF / SMB / HML / RMW / CMA）+ MOM（动量因子），日频
  - OLS 时间序列回归：`β=(X'X)⁻¹X'y`，纯 numpy，t 统计量用 `scipy.stats.t`
  - 输出：Jensen Alpha（年化）/ 6因子暴露 / t统计量 / p值 / R² / Adj R² / IR / 追踪误差
  - `compute_factor_attribution(ticker, lookback_days=252)` — 单标的
  - `batch_attribution(tickers, lookback_days)` — ThreadPoolExecutor 并行
  - `format_attribution_html(result)` — 暗色主题 HTML 卡片（含因子暴露条形图）
  - 修复：MOM 列名 `Mom` vs `MOM` 大小写不一致
  - 验证：NVDA β_mkt=1.56/HML=-1.28/MOM=+0.63，批量3标的耗时1.1s

---

## [0.10.3] — 2026-03-19

### Added (风险量化引擎)

- **`risk_engine.py`**（新文件，项目根目录）— 完整蒙特卡洛 VaR + 压力测试引擎
  - **Layer 1 历史模拟 VaR**：从 `report_snapshots/` 实际 T+1 收益，回退到 yfinance 日收益×√T
  - **Layer 2 参数法 VaR**（Delta-Normal）：`volatility_20d` + 动量调整，解析 CVaR 公式
  - **Layer 3 蒙特卡洛 VaR**：GBM 解析解 `S_T=S₀×exp((μ-½σ²)T+σ√T·Z)`，1万次模拟，向量化
  - **组合 VaR**：Cholesky 相关矩阵分解，等权默认，输出多元化收益
  - **5大压力情景**：VIX飙升 / 利率冲击(+100bps) / 板块崩盘(-25%) / COVID型崩盘 / 流动性危机
  - **Beta 估算**：OLS 60日 vs SPY/板块ETF，24h 文件缓存（`.risk_cache/`）
  - **`format_risk_html()`**：暗色主题 HTML 卡片，含 VaR 三法对比表、价格目标、压力测试柱状图
  - **CLI**：`python risk_engine.py NVDA [--portfolio NVDA TSLA MSFT] [--json] [--sims N] [--horizon D]`
  - 烟雾测试通过：NVDA 单股 2.3s，三标的组合 1.7s

---

## [0.10.2] — 2026-03-19

### Added (Phase 1 模块)

- **`data_pipeline.py`**（新文件，项目根目录）— 多源数据降级链
  - `YFinanceSource` / `AlphaVantageSource` / `FinnhubSource` 三源适配器
  - `ObservableCircuitBreaker` 熔断器（每源独立，带指标暴露）
  - `MultiSourceFetcher`：yfinance → Alpha Vantage → Finnhub → 陈旧缓存 → 安全默认值
  - 失败返回 `price=0.0 + _data_unavailable=True`，彻底消灭虚假 `price=100.0`
  - LRU + 分级TTL缓存（real=5min / degraded=2min / stale=1h）

- **`parallel_agent_runner.py`**（新文件，项目根目录）— Agent 并行化执行引擎
  - `ParallelAgentRunner` 两阶段并行：5工蜂完全并行 → Guard+Bear 并行
  - 每 Agent 独立超时（60s）+ 全局超时兜底
  - `get_timing_report()` 输出加速比、最慢/最快 Agent 名称

- **`backtest_engine.py`**（新文件，项目根目录）— 独立回测引擎（可按需单独运行）
  - 从 `report_snapshots/` 读历史快照，计算 T+1/T+7/T+30 收益
  - 输出 Sharpe / MaxDrawdown / WinRate 标准指标，不影响任何现有文件

### Changed

- **`swarm_agents/cache.py`** — `_fetch_stock_data()` 接入多源降级链
  - 优先委托 `data_pipeline.fetch_stock_data`（三源降级 + 分级TTL）
  - `data_pipeline` 不可用时自动回退原 yfinance 逻辑（零风险降级）
  - fallback `price` 从虚假 `100.0` 改为 `0.0`，与 WARN-3 标记配合

- **`alpha_hive_daily_report.py`** — `_analyze_single_ticker()` Guard+Bear 并行
  - Guard + Bear 由串行改为并行（两者均只读信息素板，PheromoneBoard 已有 RLock）
  - `ImportError` 时自动回退串行执行，零风险降级

---

## [0.10.1] — 2026-03-19

### Added (Phase 2 v4 补丁)

- **`swarm_agents/rival_bee.py`** — `_calc_technical_indicators()` 新方法
  - 计算 RSI-14 / MACD(12/26/9) Histogram+金死叉 / Bollinger Band% 三个技术指标
  - ML 不可用时：替代简单动量评分，方向判断更有区分度
  - ML 可用时：权重减半作为辅助微调（±0.5 → ±0.25）
  - 结果存入 `details.technical_indicators`

- **`swarm_agents/guard_bee.py`** — `_calc_macro_adjustment()` 新方法
  - 统一宏观 regime 投票（VIX + 收益率曲线 + 黄金 + FOMC + VIX期限结构 + 板块轮动）
  - 取代原 P5a~P5f 共 65 行零散 if-else（最坏叠加 -3.1 → 有上限 ±1.5）
  - 返回 regime / score_adj / signals / macro_summary / details / regime_votes 完整字典

- **`swarm_agents/base.py`** — `_get_stock_data()` WARN-3 保护
  - 当 price<=0 时设置 `_data_unavailable=True` 标记
  - 下游 Agent 可检查该标记提前返回安全结果，避免 ZeroDivisionError

### Changed

- **`swarm_agents/rival_bee.py`** — `analyze()` 两处集成
  - ML 可用分支：`discovery` 后追加 `tech['summary']`，评分叠加 `tech_score_adj * 0.5`
  - ML 不可用分支：已使用 `_calc_technical_indicators` 增强（上次 session 已完成）
  - `return AgentResult` details 新增 `technical_indicators` 字段

- **`swarm_agents/guard_bee.py`** — `analyze()` 宏观段精简
  - P5~P5f 65 行替换为 `macro_result = self._calc_macro_adjustment(ticker)` 共 6 行
  - `vix_term` 变量兼容保留（`= macro_result["details"]`）
  - `details` 新增 `macro_regime` / `macro_signals` / `macro_regime_votes` 字段

---

## [0.10.0] — 2026-03-18

### Added (新架构模块)

- **`market_intelligence.py`**（新文件）— 8 大高价值框架中央模块
  - `calculate_iv_rv_spread()` ① — HV30 已实现波动率 vs IV 价差，判断期权定价贵/便宜
  - `get_cycle_context()` ③ — Opex周/财报后窗口/FOMC周期/月末时间标注
  - `detect_market_regime()` ④ — SPX 200MA / SOXX 20MA / 个股 20MA vs 50MA 三层政体识别
  - `calculate_gamma_expiry_calendar()` ⑤ — 按到期日拆分 OI 集中度、Pin Risk 钉子位、Charm 衰减方向
  - `get_supply_chain_signals()` ⑥ — TSM/AMAT/ASML/SOXX 与标的 5日相对强弱
  - `calculate_signal_crowding()` ⑦ — Reddit排名+分析师共识+期权流对齐→alpha_decay_factor
  - `check_thesis_breaks()` ⑧ — 读取 `thesis_breaks_config.json`，条件触发后生成 HTML 告警卡片

- **`pead_analyzer.py`**（新文件）— ② PEAD 历史量化分析器
  - `get_pead_analysis()` — yfinance 获取历史财报日期，计算 T+1/T+5/T+10/T+20 价格漂移
  - `format_pead_for_chronos()` — 漂移统计格式化供 ChronosBee discovery 使用
  - 7 天 JSON 缓存，bias 判定（bullish/bearish/neutral）

### Changed (蜂群集成)

- **`options_analyzer.py`** — `OptionsAgent.analyze()` 新增两项输出字段
  - 调用 `calculate_iv_rv_spread()` → 输出 `rv_30d`、`iv_rv_spread`、`iv_rv_signal`、`iv_rv_detail`
  - 调用 `calculate_gamma_expiry_calendar()` → 输出 `gamma_calendar`（含到期日 OI 分布、Pin Risk 钉子位、Charm 方向）

- **`swarm_agents/guard_bee.py`** — 新增 P6/P7 两个分析块
  - P6：调用 `get_cycle_context()` ③ + `detect_market_regime()` ④，Regime risk_off/risk_on 评分修正 ±0.5，Opex周额外 -0.3；cycle_label/is_opex_week 注入 discovery
  - P7：调用 `calculate_signal_crowding()` ⑦，alpha_decay < 0.85 时乘数折扣 score
  - `details` dict 新增 `cycle_context`、`market_regime`、`signal_crowding` 三字段

- **`swarm_agents/scout_bee.py`** — 新增 2d 供应链信号块
  - 调用 `get_supply_chain_signals()` ⑥，供应链顺风/逆风影响 score ±3%，summary 注入 discovery
  - `details` dict 新增 `supply_chain` 字段

- **`swarm_agents/chronos_bee.py`** — 新增 1d PEAD 块
  - 调用 `get_pead_analysis()` ②，PEAD bias 微调 score ±0.3，`_pead_text` 注入 discovery
  - `details` dict 新增 `pead`、`pead_summary`、`pead_bias` 三字段

- **`generate_deep_v2.py`** — 全面扩展 ctx 字段和 LLM 提示词
  - `extract()` 新增提取：`iv_rv_spread`、`iv_rv_signal`、`rv_30d`、`gamma_calendar`、`pead_summary`、`pead_bias`、`cycle_context`、`market_regime`、`signal_crowding`、`supply_chain`（共 10 个新字段）
  - `main()` 新增 `check_thesis_breaks()` ⑧ 调用（2b-⑧ 步骤），论点失效时生成 HTML 告警卡
  - `ctx["thesis_break_html"]` 注入 CH1 section body（`{accuracy_html}` 之后）
  - **CH3 catalyst Step2 prompt** 新增 PEAD 历史漂移数据，要求引用财报后统计规律
  - **CH4 options Step1 prompt** 新增 IV-RV 价差/HV30/Gamma 日历钉子位/Charm 方向
  - **CH4 options Step2 prompt** 新增完整 IV-RV 价差解读逻辑和 Gamma 到期日历，第1段范围扩展含 IV-RV 策略影响，第2段含 Pin Risk 和到期日历，第3段含 HV30 对比
  - **CH5 macro Step2 prompt** 新增市场政体（Regime）、时间周期（Cycle）、供应链信号（Supply Chain），要求结合 risk_on/risk_off 和时间节奏分析宏观压力

---

## [未发布] — 进行中

---

## [0.9.6] — 2026-03-17

### Added
- **`generate_deep_v2.py`** — **Phase 1.5 跨章节锚点上下文** (`llm_cross_context()`)
  - 新增函数：Phase 1（swarm + master_thesis）完成后，生成 150-200 字结构化纯文本摘要
  - 4 行锚点格式：① 信号张力（多空拉力与 GEX 区间）② 价格锚点（Flip/Call/Put Wall）③ 催化剂压力（最近事件标题 + DTE）④ 跨章一致性（哪些蜂构成共振、哪些反向）
  - `_cross_context_block` 注入 6 章 Step2 prompt（resonance/catalyst/options/macro/scenario/risk），解决定时任务7章独立 API 调用无法跨章节引用的问题
  - no-llm 模式：`ctx["cross_context"] = ""` 静默跳过

### Changed
- **`generate_deep_v2.py`** — **CH2 resonance prompt 全面加强**（Step1 + Step2）
  - Step1：加入七蜂全评分 `Scout/Rival/Buzz/Chronos/Oracle/Guard/Bear` 数值，分析框架中明确指向哪些蜂构成共振主力
  - Step2：注入 `_master_block`（主论点）、`_conflict_block`（矛盾信号）、`_delta_block`（昨日变化）、`_cross_context_block`（跨章锚点）
  - 要求第一段分析共振质量与反向张力、第二段写共振与整体论点关系及失效条件

- **`generate_deep_v2.py`** — **CH5 scenario prompt 全面加强**（Step2）
  - 新增注入：`days_until`（催化剂距今天数）、IV 当前值、F&G 数值、期权流方向、全部 bear signals
  - 注入 `_master_block`、`_delta_block`、`_cross_context_block`
  - 要求短期 3-5 天分布分析（概率+幅度）和具体数值失效阈值

### Fixed
- **`generate_deep_v2.py`** — **CH1 催化剂图标全显示灰点 bug**
  - 原因：`c.get("importance", "medium")` 但 JSON 字段名为 `severity`
  - 修复：`c.get("importance") or c.get("severity", "medium")` 双字段兜底

- **`generate_deep_v2.py`** — **`fetch_live_news()` 在 VM 定时任务中找不到 key 文件**
  - 原因：VM 的 `~` ≠ Mac 的 `~`，单路径查找失败
  - 修复：`_load_key(*paths)` 多路径优先级查找（Mac home → workspace script dir），两个环境均能找到

### Added (files)
- **`Alpha Hive/.alpha_hive_finnhub_key`** — Finnhub API key 文件（workspace 路径，供 VM 定时任务使用）
- **`Alpha Hive/.alpha_hive_av_key`** — Alpha Vantage API key 文件（workspace 路径）
- **`Alpha Hive/.gitignore`** — 新增两条 key 文件排除规则（防止 key 提交到 git）

---

## [0.9.4] — 2026-03-16

### Fixed
- **`swarm_agents/chronos_bee.py`** — 催化剂归零 bug：`ctx = self._get_history_context()` 返回字符串，但 IV Crush 段落误用 `ctx["iv_crush"] = ...` 和 `ctx.setdefault(...)` 把它当 dict 操作，触发 `AttributeError` → `AGENT_ERRORS` 捕获 → 整个 ChronosBee 返回错误结果，`details={}` 催化剂清零
  - 修复：引入独立本地变量 `_iv_crush_data` / `_iv_crush_summary` 存储 IV Crush 数据，不再写入 `ctx`；`details` 返回值改用本地变量

### Changed
- **`generate_deep_v2.py`** — `_build_risk_narrative()` 本地 fallback 从输出 HTML 卡片改为输出两段叙事 `<p>` prose，与 LLM risk prompt 格式保持一致，消除 CH7 `<div class="prose">` 里出现重复卡片的问题

---

## [0.9.3] — 2026-03-16

### Changed
- **`generate_deep_v2.py`** — CH7 `risk` LLM prompt 从"输出 HTML 风险卡片"改为"输出叙事分析 prose"
  - 第一段：风险优先级诊断——最关键信号、与多头论点的冲突逻辑、共振放大效应
  - 第二段：失效条件与明日警戒线——具体数字阈值（价位/IV/P-C比），区别于规则引擎卡片的模板化表达
  - 明确禁止输出卡片 HTML（`**禁止输出风险卡片列表**`），消除与 `smart_risks` 规则卡片的重复

### Added
- **`generate_deep_v2.py`** — CH7 新增**明日追踪任务**小节（`tracking_tasks_html`）
  - 数据驱动自动生成：价位警戒（最强支撑/阻力 + 当前价距离）、IV Rank 监控（低位升级/高位 Crush）、催化剂追踪（下一个事件标题+日期）、空头信号监控（首条 bear_signal）
  - 复选框样式（☐），注入在 `<div class="prose">` 之后、section 结束前
  - 无数据时（`_track_tasks` 为空）静默不渲染

---

## [0.9.2] — 2026-03-15

### Added
- **`generate_deep_v2.py`** — CH4 新增 **IV 期限结构卡片**（S15 功能补全）
  - `extract_ctx`：从 `OracleBeeEcho.details.iv_term_structure` 提取数据写入 ctx
  - `generate_html`：构建 `iv_term_html`，在 6卡 opt-grid 与 levels-grid 之间渲染
  - 形态自动配色：Contango（绿）/ Backwardation（红）/ Flat（金）
  - 展示内容：形态徽章、前后利差（pp）、逐到期点箭头链（ATM IV % / DTE / 月日）、signal 信号文本
  - 无数据时（shape=unknown）静默不渲染，零副作用

### Fixed
- **`generate_deep_v2.py`** — 删除 `iv_term_html` 构建块中的死代码变量 `_front_iv` / `_back_iv`（赋值后从未使用）

---

## [0.9.1] — 2026-03-14

### Added
- **`generate_deep_v2.py`** — 三个自学习闭环 Gap 实现
  - **Gap 1** `_save_report_snapshot()`：每次报告写完后保存 `ReportSnapshot` 到 `report_snapshots/`，供 `feedback_loop` T+7 回溯
  - **Gap 2** `_run_outcome_backfill()`：启动时运行 `OutcomesFetcher`，回填历史快照的 T+1/T+7/T+30 实际价格
  - **Gap 3** `_load_ticker_accuracy()` + `_render_accuracy_card()`：读取该 ticker 历史胜率，在 CH1 渲染准确率小卡（方向胜率 + 平均 T+7 收益）
  - `generate_html()` 新增 `accuracy_html` 参数，注入 CH1 section-body

- **`weekly_optimizer.py`** — 新文件，Track A 自动权重优化器
  - 每周日 02:00 自动运行（已创建定时任务 `alpha-hive-weekly-optimizer`）
  - 从 `report_snapshots/` 读取 T+7 回测数据，调用 `BacktestAnalyzer.suggest_weight_adjustments()`
  - `clamp_shifts()`：单次变化限制 ±10pp，归一化后写入 `config.py`（原子写入 `.py.tmp` → rename）
  - `weight_history.jsonl`：追加审计日志，记录每次权重变化前后值和变化量
  - CLI 支持 `--dry-run`、`--min-samples`、`--min-change`

- **`self_analyst.py`** — 新文件，Track B 月度自我诊断
  - 每月 1 日 03:00 自动运行（已创建定时任务 `alpha-hive-monthly-self-analysis`）
  - 生成 `self_analysis_briefs/YYYY-MM.md`，包含：准确率统计、失败模式分析、最近 10 条案例、Cowork Claude 分析任务清单
  - 无需 API Key，直接输出 Markdown 供 Cowork Claude 阅读推理

### Fixed
- **`generate_deep_v2.py`**
  - `_save_report_snapshot()`：`agent_votes` 补入缺失的第 7 只蜂 `BearBeeContrarian`（原来只有 6 只）
  - `_render_accuracy_card()`：`ar_color` 条件由 `ar > 0`（0.0 显示红色）改为 `ar >= 0`

- **`weekly_optimizer.py`**（Python 3.9 兼容性，实际运行在 3.10 但提前修复）
  - `str | None` / `dict | None` union 类型写法 → `from __future__ import annotations` + `Optional[dict]`

- **`self_analyst.py`**
  - 移除未使用的 `import sys`
  - `str | None` / `list[dict]` → `from __future__ import annotations` + `Optional[str]`
  - `if s.get("composite_score")` 将 `0.0` 判为 falsy 导致漏过 → 改为 `if s.get("composite_score") is not None`
  - `sorted(glob("*.json"))` 按文件名字母排序，`[-10:]` 取到的是字母末尾的 ticker 而非最近日期 → 改为 `results.sort(key=lambda x: x.get("date", ""))`

---

## [0.9.0] — 2026-03-13（Batch 6 · 蜂群能力扩展）

### Added
- **`swarm_agents/bear_bee.py`** — 新增 `_assess_short_interest()` 维度
  - `_weights` 中加入 `"short_int": 0.18`，相应缩减其他权重保持总和 1.0
  - `dim_scores` 写入 `"short_int": short_bear`
  - `details` 写入 `"short_int_bear"` 和 `"short_interest"`
  - `si_pct = si_raw * 100.0 if si_raw <= 1.0 else float(si_raw)` 处理 yfinance 0-1 小数格式

- **`swarm_agents/scout_bee.py`** — 新增 `_assess_sector_relative_strength()` 维度
  - 计算个股相对行业 ETF 的 20 日 RS，写入 `details["sector_relative_strength"]`
  - 结果拼接到 `discovery` 字符串（`discovery = f"{discovery} | {rs_text}"`）

- **`swarm_agents/rival_bee.py`** — 新增 `_assess_eps_revision()` 维度
  - 通过 yfinance 拉取分析师 EPS 预期修正方向
  - 结果拼接到 `discovery` 字符串

- **`options_analyzer.py`** — 新增 `calculate_iv_term_structure()` 方法（S15）
  - 逐到期日取 ATM IV（±4% 容差），覆盖 25/55/85/150 DTE 四个目标点
  - 判断 Contango / Backwardation / Flat（利差阈值 ±3pp）
  - 输出 `iv_term_structure` 字段存入 OptionsAgent 结果
  - `math.isfinite()` + `0.02 < iv_raw < 2.0` 过滤异常值

- **`fred_macro.py`** — 新增高收益债利差（HY Spread）信号
  - 拉取 BAMLH0A0HYM2（`limit=2` 取日环比变化）
  - `* 100` 转换 pct → bp
  - 三档阈值评分：>600bp / >400bp / >300bp，触发 `headwinds.append()`
  - `score = max(1.0, min(10.0, score))` 末尾 clamp

- **`generate_deep_v2.py`** — 多项功能升级
  - `chart_engine.py`（新文件）生成置信区间图 + 期权水位图，base64 嵌入 HTML
  - `_try_charts(ctx)` CH1 嵌入置信区间图，CH4 嵌入期权水位图
  - `_try_compute_gex(ctx)` 报告生成阶段补算 Dealer GEX（JSON 缺失时用 Scout 价格补算）
  - `ctx["_raw_data"] = data` 原始 JSON 注入供 chart_engine 使用
  - `extract_simple()` 新增 `"bear": _s("BearBeeContrarian")` — 7 只蜂全部覆盖
  - OI 日环比 Delta（`oi_delta` / `oi_delta_pct`）：对比昨日 JSON，在 CH4 总 OI 卡片显示 ▲▼ 变化

### Fixed
- **`swarm_agents/rival_bee.py`** — `elif rec_mean >= 4.2` 被上方 `elif rec_mean >= 3.5` 提前拦截（死代码）→ 交换两个分支顺序
- **`swarm_agents/scout_bee.py`** — `parts.append(rs_text)` 在 `discovery` 已拼接完成后调用（结果丢弃）→ 改为 `discovery = f"{discovery} | {rs_text}"`

---

## [0.8.x] — 2026-02 ～ 2026-03-12（Phase 2 & Phase 3，历史归档）

> 详见 `PHASE2_COMPLETION_SUMMARY.md`、`PHASE3_COMPLETION_SUMMARY.txt`、`PHASE3_IMPLEMENTATION_COMPLETE.md`

### 主要里程碑
- Phase 2：蜂群架构重构，7 只蜂独立模块化，PheromoneBoard 信息素机制，`models.py` AgentResult 标准化
- Phase 3 P1：`advanced_analyzer.py` DealerGEXAnalyzer（BS gamma 真实 GEX）
- Phase 3 P2：`feedback_loop.py` ReportSnapshot + BacktestAnalyzer，`outcomes_fetcher.py` T+1/T+7/T+30 价格回填，`alpha_hive_daily_report.py` 完整自学习闭环
- `resilience.py` 断路器 + 限流器（yfinance / FRED / options）
- `vix_term_structure.py` VIX 期限结构（GuardBee 宏观信号）
- `generate_deep_v2.py` Template C v3.0 HTML 报告框架

---

*最后更新：2026-03-15*
