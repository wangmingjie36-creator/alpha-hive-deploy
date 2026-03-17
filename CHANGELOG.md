# Alpha Hive · 版本变更历史

> 格式：每次 Cowork session 结束后追加一条记录。
> 规范：`Added` 新增 | `Changed` 修改 | `Fixed` Bug 修复 | `Removed` 删除

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
