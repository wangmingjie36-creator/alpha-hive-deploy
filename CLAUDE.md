# Alpha Hive — Claude 工作记忆

## 文档分工原则（防陈旧误导，v0.40.3 起）

- **本文件只存指针与不变式，不存易变参数值与统计数字快照**——参数唯一真相在 `config.py` / 各模块 `CONFIG`，指标看 dashboard / `compute_kpis()`，历史改动看 `CHANGELOG.md` 与 MEMORY.md 版本历史表。
- 教训：v0.19 时代的 paper_portfolio 参数快照在本文件停留数月，与 v0.39 现行配置直接矛盾，误导每个新 session。
- **分区（v0.45.727 起）**：本文件曾涨到 88KB（约 2.8 万 token，每个会话都付）。细节放进按需加载的文件，这里每块只留「是什么 + 何时去读哪个」：
  - 子目录 `CLAUDE.md`（`alphabot/`、`tests/`、`experiments/`）：读 / 改该目录下的文件时自动加载
  - `.claude/rules/*.md`：frontmatter `paths:` 匹配的文件被读 / 改时自动加载（根目录模块按领域分）
  - 项目 skill `alpha-hive-brief`（`.claude/skills/`）：投研人设与简报模板，按投研指令加载
  - ⚠️ **只有 Read / Edit / Write 碰到匹配的文件才触发**（实测：Bash 里 grep / sed 读不触发，用 Write 新建 `tests/` 下的文件会触发）；规划阶段还什么都没加载，新文件也多半匹配不到 `paths:`——照下面的指针自己打开。新细节写进分区文件，这里只加指针；守卫 `tests/test_claude_md_partitions.py`（`paths:` 失效、指针缺失、本文件超预算都会红）

## 用户偏好

- **⚠️ Python 解释器硬规则：扫描/脚本一律用 `/usr/local/bin/python3`（Python 3.11.1），禁用裸 `python3`**
  - 裸 `python3` 会解析成系统自带 3.9.6（`/usr/bin/python3`：无 sklearn、缺 jinja2、PEP604 `X|None` 注解 import 即崩）⇒ ML 降级 SimpleMLModel（2026-06-30 事故根因）
  - 手动 / Claude 跑扫描：先 `export PATH="/usr/local/bin:$PATH"`（内部 spawn 的子 python 也走 3.11），再 `/usr/local/bin/python3 alpha_hive_daily_report.py ...`；测试同理 `/usr/local/bin/python3 -m pytest`
  - 两个 Python 的来历、包分装两处（用户 site）与测试换 HOME 的陷阱 → `tests/CLAUDE.md`

- **报告生成模式：Cowork 本地推理，永远不用 Claude API / Opus**
  - 用户使用 Cowork 本地 LLM 推理，不是 Anthropic API
  - 深度报告生成器 `generate_deep_v2.py` 已于 v0.45.724 退役（最后产物 2026-07-29）；要恢复先问用户，且仍只许 opt-in 调 LLM
  - 任何脚本默认必须是 `--no-llm`，只有用户在终端显式确认才允许 `--use-llm`
  - 禁止在代码里用 `api_key 存在就自动开 LLM` 的逻辑
  - 不要添加"未找到 API Key"警告或提示创建 key 文件
  - 不要自动搜索 `.anthropic_api_key` 文件路径
  - 2026-03-16 事故：`generate_deep_v2.py` opt-out 设计导致 NVDA deep 报告静默消费 $0.47 Opus，已修复为 opt-in

- **图表**：深度报告的 `chart_engine.py` 随其 v0.45.724 退役；matplotlib 仍在用户 Mac（`code_generator` 在用）

- **knowledge-pool 召回规则**：
  - 召回必须调真实引擎 `weighted_recall.py`（CLI 或 `WeightedRecall().recall()`），**禁止手动简化算分**
  - 2026-05-05 事故：`_today_recall.json` 缓存 motifs 为空时，用手动粗糙算法得到 4.485 而非真实的 123.80，导致误判"召回质量差"
  - 缓存失效时的正确降级链：① 读 `_today_recall.json` → ② `python3 daily_recall_runner.py` → ③ `python3 weighted_recall.py --motifs "..." --today YYYY-MM-DD` → ④ `WeightedRecall().recall(motifs)` 直接调用；**任何一步都不允许自写评分逻辑**

## 版本历史规则

- **每次 session 结束前必须写好自己那一版的条目**：v0.45.728 起写在 `changelog.d/<版本>.md`（一个版本一个文件，格式、示例、流程见 `changelog.d/README.md`）。`CHANGELOG.md` 已冻结为档案，**不许再加版本标题**（提交 / 推送 / 全量测试都会红）；冻结前在里面占的号就地完成
- 格式：`Added` / `Changed` / `Fixed` / `Removed`，注明文件名和改动摘要
- 版本号：patch（0.x.y+1）= bug fix；minor（0.x+1.0）= 新功能批次
- 最大号 / 下一个号 / 谁还挂着占位：`/usr/local/bin/python3 changelog_fragments.py status`；合并视图 `... render`（生成结果不提交）

### 并发开工必须先占号（2026-08-27 起；v0.45.728 起占号 = 往 main 推一个碎片）

多个 session 同时开工时，**开工第一件事就是占号**，不要等完工再编号：

`/usr/local/bin/python3 changelog_fragments.py claim "一句话说明在做什么" --push`

它在 `origin/main` 上另建一个只含 `changelog.d/<下一个号>.md` 占位碎片的提交、推上 main 并用 `ls-remote` 核实（被抢了自动取下一个号），再把这个提交接进当前分支——不用先切分支、不用清工作区（暂存区有东西时拒绝接入，照提示做，**别用裸 `git stash`**）。完工时只改碎片的标题和正文，**认领行 `<!-- 认领：… -->` 不许改**。

- ⚠️ **别手写碎片、别省 `--push`。** 认领行里的随机串让每次占号内容都不同——内容逐字相同的两个碎片，git 会无声合并成一个，撞号就看不见了；不推送的占号别的 session 看不见，等于没占。
- **为什么值得**：2026-08-26~27 两条线并行，`0.45.36–0.45.40` 被双方各用了一遍、`0.45.47` 与 `0.45.48` 又各撞一次，前后返工三轮。改成碎片后，不同的号是不同的文件，合并不再互相冲突；只有同一个号才冲突——冲突本身就是撞号。
- **撞上了怎么办**：**先进 main 的号算数**。后者 rebase 时在 `changelog.d/<号>.md` 上撞 add/add 冲突 ⇒ 保留 main 那份（`git checkout origin/main -- changelog.d/<号>.md`）、`rebase --continue`，再 `claim` 一个新号、把条目搬过去。占了不用的号：标题改成 `占位关闭：<原因>` 并写一句正文，**不删文件**（推往 main 时删已有碎片会被拦）。

### CHANGELOG 完整性 hook（2026-09-13 起；v0.45.728 起含碎片检查）

`changelog_guard.py` 挂在 pre-commit + pre-push 上，只挡**动了 `CHANGELOG.md` 或 `changelog.d/`** 的提交与推往 main 的推送。设计与实测全在该文件 docstring。

- 动了档案：跑 `tests/test_changelog_entry_integrity.py`（冲突标记 / 重号 / 空标题 / 冻结：加号、删条目、改顶号都红）。
- 动了碎片：跑 `changelog_fragments.py` 的检查（文件名 = 标题里的号、认领行恰好一行、日期、冲突标记、占位以外要有正文、号要大于档案顶号）。pre-commit 读**暂存区**、不读工作区；pre-push 读**被推的那个提交**并对照远端 main——已在 main 上的碎片不许删、认领行不许改，算不出远端 main 的基准就拦。
- ⚠️ **pre-commit 管不到 `git rebase --continue`**（实测只触发 post-rewrite）——解冲突后兜底的是 pre-push，别以为提交成功就代表检查过。
- `.git/hooks` 不被跟踪：重新 clone 或清过之后要重装 `/usr/local/bin/python3 changelog_guard.py --install-hook`，**否则没人会知道它没了**。钩子壳没变，碎片检查随代码合入即生效，已装的钩子不用重装。

## 整套测试怎么跑（2026-10-21 起）

- **开发期只跑改动相关的测试**（`/usr/local/bin/python3 -m pytest tests/test_xxx.py`；选测：`make test-select` 看方案、`make test-changed` 跑），**整套留到收尾跑一次**
- **跑整套前先看有没有别的整套 / clockshift 审计在跑**：`pgrep -fl pytest`、`pgrep -fl audit.py`，有就错开（并发时同一套慢近两倍，耗时不可比）
- 给测试加 CPU 负载一律用 `tools/burn_cpu.py`，别手写 `yes &`
- 选测退出码与登记表、`concurrency:` / load 提示的全文 → `tests/CLAUDE.md`

## 历史改动查询指针

历史改动**不在本文件维护**（v0.40.3 清理了此前 ~75 行 v0.10-0.19 时代的实现细节清单）：
- 版本级摘要 → MEMORY.md 末尾「版本历史」表
- 逐项细节 → `changelog.d/<版本>.md`（v0.45.728 起）+ 冻结档案 `CHANGELOG.md`；合并视图 `/usr/local/bin/python3 changelog_fragments.py render`
- 定时任务（weekly-optimizer / monthly-self-analysis；每日扫描走 launchd `com.alpha.hive.daily`，不走 scheduled-tasks）的调度时刻 → 以 `list_scheduled_tasks` 返回的 `nextRunAt` 为唯一真相，勿引用文档里的旧时刻

## 核心组件指针（只记"在哪、归谁管"，不记参数值）

细节不在这里：读 / 改到对应文件时，箭头后的文件会自动加载；动手前（尤其新建文件时）照箭头自己打开。

- **纸面组合** `paper_portfolio.py`：参数唯一真相 = 模块内 `CONFIG`（v0.39.0 起为回放拐点配置，历史变更查 CHANGELOG）；挂载点 = 日报主流程 `alpha_hive_daily_report._post_scan_notify`（v0.38.0 起，**不再**依赖 generate_deep_v2；v0.45.428 更正：此前这里写的 `_post_scan_enrichment` 不对）；状态文件 `paper_portfolio_state/`（meta.json 的 config_snapshot 自 v0.40.2 每次运行刷新）；KPI 看 `compute_kpis()`
- **权重优化** `weekly_optimizer.py`（Track A，自动写入未解锁、现为只读诊断）→ `.claude/rules/scoring-weights.md`
- **月度自诊断** `self_analyst.py`（Track B）：输出 `self_analysis_briefs/YYYY-MM.md`，含每蜂维度 rank-IC 小节（v0.40.0）
- **IBKR 桥接** `ibkr_sync.py`：手动流程（export actions → 用户 TWS 下单 → import CSV → reconcile），状态在 `paper_account/`
- **三条已断开的进分通道——都不要接回**：`Backtester.adapt_weights()`（v0.45.176，守卫 `tests/test_zero_weight_invariant.py`）、GEX 直接加减分（v0.45.334；规则引擎下 GEX 进分只剩 `RegimeWeightAdjuster` 政体路由，展示只读 `gex_state`）、RivalBee 的 ML 调整项（v0.45.538）→ `.claude/rules/scoring-weights.md`
- **期权纸面账本**：卖权行权价选择器 `sell_strike_*`、财报跨式 `options_paper_leg` 与影子宽跨 `straddle_strangle_shadow`、接货型卖 put `sell_put_wheel`——**预注册盲期勿偷看、冻结只走登记入口**（通则 `experiments/CLAUDE.md`）→ `.claude/rules/options-ledgers.md`
- **组合 Greeks / 对冲**：`portfolio_greeks`、逐票 `delta_hedge`（缺省 shadow，改 paper 要单独提交）、尾部预算 `tail_budget`（只观测）、bid = 0 虚值腿判据 `cboe_options.bid_zero_greeks_usable` → `.claude/rules/portfolio-hedging.md`
- **Alpha Bot** `alphabot/`（卖权选择器等的本机前端，`make alphabot`）：**只绑回环、不上网站**；**只有 `alphabot/service.py` 可 import 卖权模块**；**不改 levels / candidates 既有输出与版本号**；对话抽屉默认关、**真调 API 前先问用户费用**；与 `alpha_hive_bot/`（Telegram）无关 → `alphabot/CLAUDE.md`
- **取数唯一入口——新代码一律走它们**（新模块多半匹配不到这些规则的 `paths:`，所以写在这里）→ `.claude/rules/data-intake.md`
  - 新账本 / 状态写入走 `ledger_io`（可再生缓存走 `hive_logger.atomic_json_write`）；读到坏行抛 `LedgerCorrupt`，**不跳过、不当新账本**
  - 财报日只经 `earnings_calendar`（`earnings_cache/{T}_date.json` 不得被别处读）；yfinance 日线只经 `yf_daily`（**别用 `actions=False` 代替**）
  - 给 H1 通道加取数源 = 在 `observation_archive.KINDS` 登记 + 取数处 `observe`；输入层指纹守卫的 `EXEMPT_DEPS` **别拿来消红**；日健康 `day_health` 的故障日只告警 + 敏感性分析、不排除，改 `scan_coverage_gate.FIELDS` 会让冻结指纹变
- **LLM 影子 A/B**（`llm_shadow_*`）：生产永远 `--no-llm`；影子由 launchd `com.alpha.hive.llmshadow` 每个工作日**自动付费**跑，**全仓只有 `llm_shadow_daily.py` 替用户设 `ALPHA_HIVE_LLM_SHADOW_SPEND`**；首次真跑必须先问用户（费用 / 上限常量在 `llm_shadow_scan.py`）；停：`touch ~/alpha-hive-llm-shadow/DISABLED` 或 `launchctl bootout gui/$(id -u)/com.alpha.hive.llmshadow` → `.claude/rules/llm-shadow.md`
- **生产代码 = 独立克隆 `~/alpha-hive-prod`**（`~/Desktop/Alpha Hive` 是开发检出）：⚠️ **不要在生产克隆里开会话、改文件、提交**；改代码 = 开发检出开 worktree → 推 origin/main → 下一个扫描日自动快进。判「下一轮生产跑哪版」：`git -C ~/alpha-hive-prod fetch -q origin && git -C ~/alpha-hive-prod rev-list --left-right --count main...origin/main`（refs 不与开发仓库共享）→ `.claude/rules/production-ops.md`
- **编排器**：唯一真相 = 仓库 `scripts/alpha-hive-orchestrator.sh`；launchd 跑的是部署副本 `~/.claude/scripts/alpha-hive-orchestrator.sh`——⚠️ **不许直接改部署副本**；合入 origin/main 后下一扫描日自动部署、从第二个扫描日起执行（与 Python 耦合的改动要兼容一轮）；**回滚 = 在 main 上 revert 那次合入** → `.claude/rules/production-ops.md`

## GitHub Pages 部署规则（永久设置）

- **GitHub Pages 从 `gh-pages` 分支部署**，不是 `main`
- `report_deployer.deploy_and_notify`（v0.45.402 前叫 `auto_commit_and_notify`）：生产模式（LLM 或蜂群）只**同步 gh-pages**；**不再提交、不再推代码仓库 main**（数据根迁移阶段 6：生产数据在 `$ALPHA_HIVE_HOME`、代码仓库不跟踪数据；异地副本靠编排器 Step 14 的私有备份）。⚠️ **勿重建日报提交 / 推送链**（`production_sync.push_main`、日报白名单都已删，墓碑测试 `tests/test_git_failures_are_visible.py::TestCommitPushChainStaysRetired`）；`gh_pages` 结局键必须始终写进 `scan_timing.extra`（编排器拿它当「主流程已跑完」的证据）。非生产扫描不部署；v0.45.210 撤掉的 test remote 推送分支同样**勿恢复**（勿往 `GitHubTool` 白名单加 `checkout`/`reset`）
- **非蜂群扫描已退役（v0.45.213）**：`alpha_hive_daily_report.py` 不带 `--swarm` 直接 exit 2，**勿重建 `run_daily_scan`**——它在扫描中途就写概率账本（先写者占位），在 save_report 前短路管不到；理由在 `main()` 该闸注释，守卫 `tests/test_non_swarm_scan_retired.py`
- `generate_ml_report.py`：末尾调用 `_sync_ghpages()`，每次生成 ML 报告后自动同步 gh-pages
- **禁止**只推 main 不推 gh-pages，否则网站不更新

## Memory 2.0 自动更新规则

- **Auto Memory 路径**：`~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/MEMORY.md`（旧路径 `~/.claude/projects/-Users-igg/memory/` 已弃用，勿再写入）。改了代码 / 新增模块 / 修了 bug ⇒ 自动更新 MEMORY.md 对应章节；控制在 200 行以内
- **索引行只写「这条是什么 + 什么时候该打开它」，单条 ≤150 字符**，细节一律进 topic 文件；**裁之前先把索引行里的标志物逐个 grep topic 文件，缺的先搬回去再裁**（2026-09-11：有些教训只活在索引行里）
- 守卫 `check_index_line_length.py`（memory 仓 pre-commit，v0.45.208）：**动了 `MEMORY.md` 才拦、只检测绝不自动裁**；钩子不进 git，接手 / 重装（必须写全路径）：`/usr/local/bin/python3 ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/check_index_line_length.py --install-hook`
- 全文（事故数字与每条规则的理由）→ `.claude/rules/session-closing.md`（读 / 改 `CHANGELOG.md` 或 `changelog.d/` 时自动加载）

### memory 目录已纳入 git，**每次 session 收尾必须提交并推送**（2026-09-11 起提交；2026-09-18 起加推送）

独立 git 仓库（分支 `main`，远端 `origin` = 私有仓库 `wangmingjie36-creator/alpha-hive-memory`），没有任何东西会替你提交——与「写好自己的版本条目」（`changelog.d/<版本>.md`）并列为收尾项：

```bash
git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory add -A && git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory commit -m "<本次记了什么>" && git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory push origin main
```

- **没有改动就不提交**；⚠️ 多个 session 并发写，`add -A` 捎上别人的编辑时 **commit message 必须如实说明哪些文件不是本次改的**
- **推送已获用户明确批准为默认行为**（2026-09-18），不必每次问；内容明显敏感时推送前仍先问；以后**新建远端**要重新先问用户

## 硬检查项：「这个失败，下游怎么知道？」（2026-09-05 起）

写下任何一段「A 失败了、B 接着跑」的代码前，先回答一句：
**A 失败这件事，B（以及看输出的人）从哪里能看出来？**
答不上来就是缺一个观测点——那不叫「已经容错了」，那叫把失败改写成了「没发生过」。

判据：
1. 写 `except` / `or 默认值` / `if 取不到: continue` 时，问「**谁会红？**」——没人就补一个会红的观测点（断言、计数、探针）。
2. 写守卫先问「失败时是返回还是抛」，两条路径各堵一次（只判返回值接不住 `raise`；只判异常接不住 `return None`）。
3. **「这个测试文件是离线的」是需要被执行的断言，不是注释**——核法是 socket 探针，且探针本身要先用一个真出网的 canary 反向自证。

同一形状在 2026-09-05~07 四天内出现四次，横跨三个 session 与一个自动流程，表层原因毫无关系、机制完全相同（含 v0.45.91/119/121/124 逐版本细节）。完整取证见 auto-memory `alpha-hive-failure-propagation.md`。

## 硬检查项：`skip` 守卫要问「X 在哪些环境里存在？」（2026-09-07 起）

写 `if not X.exists(): pytest.skip()` 之前先回答一句：**X 在哪些环境里存在？** 若只是「一台机器上的一个目录」，这条测试**等于没有**——`skip` 把「这条没验」渲染成「这条没问题」，判据仍是「谁会红？」。依赖由测试自己构造 ⇒ 守卫写断言不写 skip，且要正面核对夹具真的接上了（不能只断言「没读生产库」）；真需要外部系统 ⇒ 标 marker；模块级 `pytestmark = skipif` 会连坐整个文件（**证明守卫有牙的那把尺子，自己从未被拿出来过**）。元守卫 `tests/test_no_invisible_prod_data_skips.py`。v0.45.165 普查三条、隔离泄漏进子进程造出的恒 skip、`git check-ignore` 三义退出码（把一种失败误报成另一种）全文 → `tests/CLAUDE.md`

## 硬检查项：新产物的默认路径不许是相对路径（2026-09-07 起）

给任何会落盘的产物写默认位置前先回答：**这个默认值在「不是从仓库根跑」的时候会写到哪？** 跟着 cwd 走就是定时炸弹——测试的 cwd 不受控。**文件名唯一真相在 `hive_logger.PATHS`**：新产物在那里加一个 property；函数默认值写 `None`、在函数体里解析，**不要**写成模块级常量或类属性（pytest 收集期 import 时就地冻死，`_isolate_env` 类隔离对它无效）。新增产物必须在 `tests/conftest.py` 配两道防线；裸字面量缺省（`db_path="pheromone.db"`）是另一族，存量只许减。全文 → `tests/CLAUDE.md`，事故取证 → auto-memory `alpha-hive-test-writes-production.md`

## 硬检查项：夹具里的日期会不会和真实钟比？（2026-10-13 起）

会 ⇒ **被测路径读几只钟就钉几只**；没有注入接缝 ⇒ 在测试体内按真实今天算相对日期。写完跑一次可执行审计 `tools/clockshift/audit.py`（命令、harness 不变式与历次炸弹 → `tests/CLAUDE.md`）。

## 硬检查项：这个路径指向「代码」还是「数据」？（2026-09-08 起）

**数据根规则（v0.45.422）**：唯一实现 `hive_logger.resolve_data_root`——`ALPHA_HIVE_HOME` 非空就用它，否则 `$HOME/alpha-hive-data`（**不再**兜底到代码检出）；把 `ALPHA_HIVE_HOME` 变成路径的只许 hive_logger。

改任何 `Path(__file__).parent / …` 之前先回答一句：**它指向代码还是数据？**

| 指向 | 正确锚点 | 典型 |
|---|---|---|
| **数据**（库 / 账本 / 状态 / 缓存 / 产物） | `PATHS.*`，**调用时求值** | `pheromone.db`、`*_state/`、`cache/` |
| **代码**（随代码发布、或就是要「代码在哪」） | **`__file__` 才对** | `templates/`、`prompts/`、只读配置、`sys.path.insert`、`git -C <仓库>` |

⚠️ **`__file__` 不是错的写法，是用错地方的写法。一刀切会制造新 bug。** 两个方向各有一条断言（`tests/test_paths_not_frozen_at_import.py` 的 `KNOWN` 子集 / `MUST_STAY_FILE_ANCHORED` 超集）——写白名单时问一句「我怕它变大，还是也怕它变小？」。测试会话的数据根沙箱、内核写沙箱、真实数据根闸的豁免与事故全文 → `tests/CLAUDE.md`

## 环境：`~/Desktop` 在 iCloud 同步下会造「重名副本」（2026-09-07 起）

只影响**开发检出**（阶段 8 起生产代码与数据都不在 iCloud 同步里）。macOS「桌面与文稿同步 iCloud」持续产出 `xxx 2.py` / `settings.local 2.json` 式副本（空格 + 数字）。**已知故障**：`git fetch` 报 `fatal: bad object refs/heads/... 2`——实际是 refname 含空格非法；**诊断** `find .git/refs -type f -name "* *"`。⚠️ **清理前必读** auto-memory `alpha-hive-environment-facts.md`（副本可能含未提交内容，不能无脑 `rm`）。**根治**是把项目移出 iCloud 同步——用户的机器设置，**由用户决定，不要自行更改**。全文 → `tests/CLAUDE.md`

## 已知问题 / 注意事项（长期有效项）

- `realtime_metrics` 在部分 JSON 里是空字典 `{}`，导致 `current_price = 0`
  - 修复：`_try_compute_gex` 在报告生成时用 Scout 价格补算
- GEX 在 Cowork VM 里用样本数据（yfinance 无法联网），在用户 Mac 上用真实数据
- `BearBeeContrarian` 不在 `feedback_loop.calculate_agent_contribution()` 的 5 维映射中——设计如此，Bear 是元蜂不直接对应评估维度

---

# 蜂群规格（原 ~/CLAUDE.md，2026-08-11 并入）

v0.45.727 起，投研人设、三级任务模式（闪电 / 标准 / 深度）、蜂群运行规则、Phase 1~6、输出模板 A / B / C 与执行口令改放项目 skill **`alpha-hive-brief`**（`.claude/skills/alpha-hive-brief/SKILL.md`）。**收到「开始晨扫」「生成今日简报」「跑简报」「分析一下」「深度研究」等投研指令时先加载它。** 每日扫描不受影响：编排器全程跑 Python，蜂群角色是 `alpha_hive_daily_report.py` 里的真实类。以下几节始终生效、留在本文件：

- 信息素板：禁止无来源结论进入信息素板（`pheromone_board.py` 注释引用此条）

## 评分与决策规则
### 候选机会综合分（Opportunity Score）
`Opportunity Score = Σ wᵢ × 维度分`，五个维度：signal / catalyst / sentiment / odds / risk_adj。**`config.EVALUATION_WEIGHTS` 是权重的「配置意图」唯一真相**；判断「生产实际用了哪套权重」看 `swarm.dimension_weights`，不看 config。v0.45.172 起权重已改——用户看过完整证据强度后的主动决定，**不要读成「已验证有效」**；v0.45.176 断开 `adapted_weights` 旁路后 config 才真是唯一真相；蜂后 ML 反馈 v0.45.537 已停用，**勿接回，也勿把 `ML_FEEDBACK_CONFIG` 的 enable_* 改成 False**（共振加成检验重放旧记录靠它们）。沿革、扫描期观测点、世代边界 → `.claude/rules/scoring-weights.md`

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

## 风控与合规硬约束
- 不提供个性化投资建议，不替代持牌投顾服务。
- 不编造数据；无法核实的信息必须显式标注”待验证”。
- 对高波动/低流动性标的，必须添加风险提示与仓位上限建议。
- 任何结论必须附”失效条件”（thesis break）。
- **反对蜂硬性下限**：每份标准/深度简报必须包含 **至少 3 条** 看空/风险/反对观点（由 GuardBeeSentinel 或反对蜂视角提供），防止确认偏误。不足 3 条时不得发布。
- **时效性原则**：优先引用最近 7 天的数据和新闻；超过 30 天的数据必须标注时间。

## 硬性规则（永久，不可覆盖）

### LLM 扫描必须用户确认
- **禁止私自运行 LLM 模式扫描**（`--use-llm` / 任何调用 Claude API 的扫描）。
- 每次需要 LLM 扫描前，必须明确告知用户并等待批准：
  「需要跑 LLM 扫描，预计费用约 $X，是否确认？」
- 测试与调试始终使用 `--no-llm`，不消耗 API 费用。

### Slack 推送规则
- **频道推送只走 Claude Code 内置 Slack MCP 工具**（`slack_send_message`）→ **#alpha-hive 频道**（Channel ID: `C0AGUUWJXJS`）；现在只有月度自诊断定时任务在用
- ⚠️ **每日富文本日报目前没有任何自动推送**；**用户 2026-10-21 定：现在不需要每日日报推送**——别再提议恢复，用户主动要再做
- 扫描确认通知（"是否启用 LLM？"）**已删除**；编排器永远 `--no-llm`，要跑 LLM 扫描只能由人在终端显式 `--use-llm`，且仍须先问费用
- **仓库里没有任何 Slack 发送代码**；Bot Token 文件 `~/.alpha_hive_slack_bot_token` 仍在本机，但无代码读它。删除记录、墓碑测试与冻结副本 → `.claude/rules/slack.md`

### Slack 通知精简规则（减少噪音）
- Bot **不发任何消息**（`tests/test_slack_send_whitelist.py` 白名单为空）。原第一类「富文本日报」`push_report_to_slack.py` v0.45.571 删除（墓碑 `tests/test_step7_push_report_retired.py`），原第二类「扫描前 LLM 模式确认」v0.45.514 删除。加回任何一类先由用户改本条规则。
- **禁止发送**以下类型的 Slack DM：扫描开始/完成通知、SLO 违规告警、权重自适应更新、数据质量降级预警、逐标的高分机会/低分预警、扫描失败通知。
- 这些信息仅写入本地日志文件，不打扰用户。
