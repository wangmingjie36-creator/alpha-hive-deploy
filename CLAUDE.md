# Alpha Hive — Claude 工作记忆

## 文档分工原则（防陈旧误导，v0.40.3 起）

- **本文件只存指针与不变式，不存易变参数值与统计数字快照**——参数唯一真相在 `config.py` / 各模块 `CONFIG`，指标看 dashboard / `compute_kpis()`，历史改动看 `CHANGELOG.md` 与 MEMORY.md 版本历史表。
- 教训：v0.19 时代的 paper_portfolio 参数快照在本文件停留数月，与 v0.39 现行配置直接矛盾，误导每个新 session。

## 用户偏好

- **⚠️ Python 解释器硬规则：扫描/脚本一律用 `/usr/local/bin/python3`（Python 3.11.1），禁用裸 `python3`**
  - 用户 Mac 有两个 Python：`/usr/bin/python3`=3.9.6（系统自带，**无 sklearn、缺 jinja2、PEP604 `X|None` 注解 import 即崩**）；`/usr/local/bin/python3`=3.11.1（Homebrew，**真实环境**：sklearn/jinja2/yfinance 全装，PEP604 合法）
  - 编排器 `~/.claude/scripts/alpha-hive-orchestrator.sh:54` 已显式 `PYTHON3="/usr/local/bin/python3"`；**手动/Claude 跑扫描必须同样显式用 `/usr/local/bin/python3 alpha_hive_daily_report.py ...`**，并 `export PATH="/usr/local/bin:$PATH"` 保证内部 spawn 的子 python 也走 3.11
  - 裸 `python3` 会解析成 3.9.6 → ML 降级 SimpleMLModel + PEP604 崩 + 缺 jinja2 崩（2026-06-30 事故根因）
  - 运行测试同理：`/usr/local/bin/python3 -m pytest`

- **报告生成模式：Cowork 本地推理，永远不用 Claude API / Opus**
  - 用户使用 Cowork 本地 LLM 推理，不是 Anthropic API
  - `generate_deep_v2.py` 永远跑 `_local_fallback`，禁止调用 `claude-opus-4-6`
  - 任何脚本默认必须是 `--no-llm`，只有用户在终端显式确认才允许 `--use-llm`
  - 禁止在代码里用 `api_key 存在就自动开 LLM` 的逻辑
  - 不要添加"未找到 API Key"警告或提示创建 key 文件
  - 不要自动搜索 `.anthropic_api_key` 文件路径
  - 2026-03-16 事故：`generate_deep_v2.py` opt-out 设计导致 NVDA deep 报告静默消费 $0.47 Opus，已修复为 opt-in

- **图表**：已嵌入 `chart_engine.py`，matplotlib 已安装在用户 Mac

- **knowledge-pool 召回规则**：
  - 召回必须调真实引擎 `weighted_recall.py`（CLI 或 `WeightedRecall().recall()`），**禁止手动简化算分**
  - 2026-05-05 事故：`_today_recall.json` 缓存 motifs 为空时，用手动粗糙算法得到 4.485 而非真实的 123.80，导致误判"召回质量差"
  - 缓存失效时的正确降级链：① 读 `_today_recall.json` → ② `python3 daily_recall_runner.py` → ③ `python3 weighted_recall.py --motifs "..." --today YYYY-MM-DD` → ④ `WeightedRecall().recall(motifs)` 直接调用；**任何一步都不允许自写评分逻辑**

## 版本历史规则

- **每次 session 结束前必须更新 `CHANGELOG.md`**
- 格式：`Added` / `Changed` / `Fixed` / `Removed`，注明文件名和改动摘要
- 版本号：patch（0.x.y+1）= bug fix；minor（0.x+1.0）= 新功能批次

## 历史改动查询指针

历史改动**不在本文件维护**（v0.40.3 清理了此前 ~75 行 v0.10-0.19 时代的实现细节清单）：
- 版本级摘要 → MEMORY.md 末尾「版本历史」表
- 逐项细节 → `CHANGELOG.md`
- 定时任务（daily-scan / weekly-optimizer / self-analysis / sample-accumulator）的调度时刻 → 以 `list_scheduled_tasks` 返回的 `nextRunAt` 为唯一真相，勿引用文档里的旧时刻

## 核心组件指针（只记"在哪、归谁管"，不记参数值）

- **纸面组合** `paper_portfolio.py`：参数唯一真相 = 模块内 `CONFIG`（v0.39.0 起为回放拐点配置，历史变更查 CHANGELOG）；挂载点 = 日报主流程 `alpha_hive_daily_report._post_scan_enrichment`（v0.38.0 起，**不再**依赖 generate_deep_v2）；状态文件 `paper_portfolio_state/`（meta.json 的 config_snapshot 自 v0.40.2 每次运行刷新）；KPI 看 `compute_kpis()`
- **权重优化** `weekly_optimizer.py`（Track A）：T+7 回测 → clamp ±10pp → 原子写 config.py，审计日志 `weight_history.jsonl`
- **月度自诊断** `self_analyst.py`（Track B）：输出 `self_analysis_briefs/YYYY-MM.md`，含每蜂维度 rank-IC 小节（v0.40.0）
- **IBKR 桥接** `ibkr_sync.py`：手动流程（export actions → 用户 TWS 下单 → import CSV → reconcile），状态在 `paper_account/`

## GitHub Pages 部署规则（永久设置）

- **GitHub Pages 从 `gh-pages` 分支部署**，不是 `main`
- `report_deployer.py`：`_deploy_ghpages = _deploy_production`（生产模式 = LLM 或蜂群，均同步 gh-pages）
- `generate_ml_report.py`：末尾调用 `_sync_ghpages()`，每次生成 ML 报告后自动同步 gh-pages
- **禁止**只推 main 不推 gh-pages，否则网站不更新

## Memory 2.0 自动更新规则

- **Auto Memory 路径**：`~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/MEMORY.md`
- 每次 session 修改了代码/新增模块/修复 bug 后，Claude 必须自动更新 MEMORY.md 对应章节
- 控制在 200 行以内；超出时压缩旧版本历史或移除已被代码覆盖的实现细节
- 旧记忆路径 `~/.claude/projects/-Users-igg/memory/` 已弃用，勿再写入

## 已知问题 / 注意事项（长期有效项）

- `realtime_metrics` 在部分 JSON 里是空字典 `{}`，导致 `current_price = 0`
  - 修复：`_try_compute_gex` 在报告生成时用 Scout 价格补算
- GEX 在 Cowork VM 里用样本数据（yfinance 无法联网），在用户 Mac 上用真实数据
- `BearBeeContrarian` 不在 `feedback_loop.calculate_agent_contribution()` 的 5 维映射中——设计如此，Bear 是元蜂不直接对应评估维度
