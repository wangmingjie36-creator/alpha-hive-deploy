# Alpha Hive — Claude 工作记忆

## 用户偏好

- **报告生成模式：Cowork 本地推理，永远不用 Claude API / Opus**
  - 用户使用 Cowork 本地 LLM 推理，不是 Anthropic API
  - `generate_deep_v2.py` 永远跑 `_local_fallback`，禁止调用 `claude-opus-4-6`
  - 任何脚本默认必须是 `--no-llm`，只有用户在终端显式确认才允许 `--use-llm`
  - 禁止在代码里用 `api_key 存在就自动开 LLM` 的逻辑
  - 不要添加"未找到 API Key"警告或提示创建 key 文件
  - 不要自动搜索 `.anthropic_api_key` 文件路径
  - 2026-03-16 事故：`generate_deep_v2.py` opt-out 设计导致 NVDA deep 报告静默消费 $0.47 Opus，已修复为 opt-in

- **图表**：已嵌入 `chart_engine.py`，matplotlib 已安装在用户 Mac

## 版本历史规则

- **每次 session 结束前必须更新 `CHANGELOG.md`**
- 格式：`Added` / `Changed` / `Fixed` / `Removed`，注明文件名和改动摘要
- 版本号：patch（0.x.y+1）= bug fix；minor（0.x+1.0）= 新功能批次

## 已完成的重要改动（勿重复添加）

### market_intelligence.py（新文件，v0.10.0）
- `calculate_iv_rv_spread()` ① — HV30 已实现波动率 vs 隐含波动率价差
- `get_cycle_context()` ③ — Opex周/财报后窗口/FOMC周期/月末
- `detect_market_regime()` ④ — SPX 200MA / SOXX 20MA / 个股三层政体识别
- `calculate_gamma_expiry_calendar()` ⑤ — 到期日 OI 集中度、Pin Risk、Charm 方向
- `get_supply_chain_signals()` ⑥ — TSM/AMAT/ASML/SOXX 相对强弱
- `calculate_signal_crowding()` ⑦ — 信号拥挤度元指数，alpha_decay_factor
- `check_thesis_breaks()` ⑧ — thesis_breaks_config.json 条件触发告警 HTML 卡片
- `_field_vals` 局部变量替代 f-string 嵌套 dict（已修复 SyntaxError）

### pead_analyzer.py（新文件，v0.10.0）
- `get_pead_analysis()` — yfinance 财报历史 → T+1/T+5/T+10/T+20 漂移统计 → 7天 JSON 缓存
- `format_pead_for_chronos()` — 格式化摘要供 discovery 文字使用

### generate_deep_v2.py（v0.10.0）

- `_try_charts(ctx)` — 生成置信区间图 + 期权水位图，base64 嵌入 HTML
- `_try_compute_gex(ctx)` — 报告生成阶段补算 Dealer GEX（当 JSON 里 `dealer_gex` 缺失时）
- `ctx["_raw_data"] = data` — 原始 JSON 注入给 chart_engine 使用
- CH1 嵌入置信区间图，CH4 嵌入期权水位图
- `extract_simple()` — 覆盖全部 7 只蜂（含 BearBeeContrarian）
- OI 日环比 Delta — `oi_delta` / `oi_delta_pct`，对比昨日 JSON，CH4 显示 ▲▼
- **自学习 Gap 1** `_save_report_snapshot()` — 报告写完后保存 ReportSnapshot，供 feedback_loop T+7 回溯
- **自学习 Gap 2** `_run_outcome_backfill()` — 启动时运行 OutcomesFetcher，回填历史 T+1/T+7/T+30 价格
- **自学习 Gap 3** `_load_ticker_accuracy()` + `_render_accuracy_card()` — CH1 显示历史胜率卡片
- `generate_html()` 新增 `accuracy_html` 参数
- **CH4 IV 期限结构卡片** — `iv_term_html`，从 OracleBeeEcho details 提取，6卡 grid 下方渲染
  - 形态配色：Contango（绿）/ Backwardation（红）/ Flat（金）；无数据静默不渲染

### chart_engine.py（新文件）
- `render_confidence_chart(data, ticker, date_str)` → base64 PNG
- `render_options_chart(data, ticker, date_str, current_price)` → base64 PNG
- 使用 `matplotlib.use("Agg")` 非交互后端，安全嵌入 HTML

### advanced_analyzer.py
- 新增 `DealerGEXAnalyzer` 类（BS gamma 计算真实 GEX）
- `run_analysis()` 里 step 6 计算并存储 `analysis["dealer_gex"]`

### swarm_agents/bear_bee.py
- 新增 `_assess_short_interest()` 维度，权重 `"short_int": 0.18`
- `si_pct = si_raw * 100.0 if si_raw <= 1.0 else float(si_raw)` — 处理 yfinance 0-1 小数

### swarm_agents/scout_bee.py
- 新增 `_assess_sector_relative_strength()` 维度
- `discovery` 拼接用 `f"{discovery} | {rs_text}"`（非 `parts.append`）
- v0.10.0：新增 2d 块，调用 `get_supply_chain_signals()` ⑥，supply_chain 注入 discovery + details

### swarm_agents/rival_bee.py
- 新增 `_assess_eps_revision()` 维度
- `elif rec_mean >= 4.2` 在 `>= 3.5` 之前（死代码 bug 已修）

### options_analyzer.py
- 新增 `calculate_iv_term_structure()` 方法（S15）
- 输出 `iv_term_structure` 字段存入 OptionsAgent 结果 dict
- v0.10.0：`OptionsAgent.analyze()` 调用 `calculate_iv_rv_spread()` ①，输出 `rv_30d`/`iv_rv_spread`/`iv_rv_signal`/`iv_rv_detail`
- v0.10.0：调用 `calculate_gamma_expiry_calendar()` ⑤，输出 `gamma_calendar`
- `iv_term_structure` 通过 `oracle_bee → details` 传递至报告层

### fred_macro.py
- 新增 HY Spread 信号（BAMLH0A0HYM2），`limit=2` 取日环比，pct→bp `*100`
- 三档评分阈值：>600 / >400 / >300bp；末尾 `max(1.0, min(10.0, score))` clamp

### weekly_optimizer.py（新文件）
- Track A 自动权重优化器，每周日 02:00 运行（定时任务已创建）
- 从 report_snapshots 读 T+7 回测 → `suggest_weight_adjustments()` → clamp ±10pp → 原子写入 config.py
- `weight_history.jsonl` 审计日志

### self_analyst.py（新文件）
- Track B 月度自我诊断，每月 1 日 03:00 运行（定时任务已创建）
- 生成 `self_analysis_briefs/YYYY-MM.md`，无需 API Key，供 Cowork Claude 阅读分析

### GitHub Pages 部署规则（永久设置）

- **GitHub Pages 从 `gh-pages` 分支部署**，不是 `main`
- `report_deployer.py`：`_deploy_ghpages = _deploy_production`（生产模式 = LLM 或蜂群，均同步 gh-pages）
- `generate_ml_report.py`：末尾调用 `_sync_ghpages()`，每次生成 ML 报告后自动同步 gh-pages
- **禁止**只推 main 不推 gh-pages，否则网站不更新

## 已知问题 / 注意事项

- `realtime_metrics` 在部分 JSON 里是空字典 `{}`，导致 `current_price = 0`
  - 修复：`_try_compute_gex` 在报告生成时用 Scout 价格补算
- GEX 在 Cowork VM 里用样本数据（yfinance 无法联网），在用户 Mac 上用真实数据
- `BearBeeContrarian` 不在 `feedback_loop.calculate_agent_contribution()` 的 5 维映射中——设计如此，Bear 是元蜂不直接对应评估维度
