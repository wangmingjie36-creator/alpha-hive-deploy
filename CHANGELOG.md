# Alpha Hive · 版本变更历史

> 格式：每次 Cowork session 结束后追加一条记录。
> 规范：`Added` 新增 | `Changed` 修改 | `Fixed` Bug 修复 | `Removed` 删除

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
