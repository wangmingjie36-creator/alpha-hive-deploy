# Alpha Hive — Claude 工作记忆

## 文档分工原则（防陈旧误导，v0.40.3 起）

- **本文件只存指针与不变式，不存易变参数值与统计数字快照**——参数唯一真相在 `config.py` / 各模块 `CONFIG`，指标看 dashboard / `compute_kpis()`，历史改动看 `CHANGELOG.md` 与 MEMORY.md 版本历史表。
- 教训：v0.19 时代的 paper_portfolio 参数快照在本文件停留数月，与 v0.39 现行配置直接矛盾，误导每个新 session。

## 用户偏好

- **⚠️ Python 解释器硬规则：扫描/脚本一律用 `/usr/local/bin/python3`（Python 3.11.1），禁用裸 `python3`**
  - 用户 Mac 有两个 Python：`/usr/bin/python3`=3.9.6（系统自带，**无 sklearn、缺 jinja2、PEP604 `X|None` 注解 import 即崩**）；`/usr/local/bin/python3`=3.11.1（python.org 安装包，软链到 `/Library/Frameworks/Python.framework`，**不是 Homebrew**；**真实环境**：sklearn/jinja2/yfinance 全装，PEP604 合法）
  - ⚠️ 它的包分装两处：numpy / pytest / jinja2 / starlette 等**只**在用户 site（`~/Library/Python/3.11`，按 `$HOME` 解析）⇒ 测试把 HOME 指到 tmp 后再起真解释器，子进程会丢这些包。`tests/conftest.py` 的 `_pin_child_user_site` 已全会话钉住 `PYTHONUSERBASE`（v0.45.426），新测试换 HOME **不用再自己钉**；只有从零构造 env（不继承 `os.environ`）的子进程要自己带上（详见 auto-memory `alpha-hive-environment-facts.md`）
  - 编排器顶部已显式 `PYTHON3="/usr/local/bin/python3"`；**手动/Claude 跑扫描必须同样显式用 `/usr/local/bin/python3 alpha_hive_daily_report.py ...`**，并 `export PATH="/usr/local/bin:$PATH"` 保证内部 spawn 的子 python 也走 3.11
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

### 并发开工必须先占号（2026-08-27 起）

多个 session 同时开工时，**开工第一件事就是占号**，不要等完工再编号：

1. `git fetch origin` 后看 `origin/main` 的 `CHANGELOG.md` 取下一个可用号
2. 在顶部插入占位条目：
   `## [0.45.NN] — YYYY-MM-DD — 占位（进行中：一句话说明在做什么）`
3. **立刻单独提交并推送到 main**（只含这一行改动，不带代码）
4. 完工时把占位标题替换成真标题，正文补齐

⚠️ **第 3 步不能省。** 占位写在自己的 worktree 里，别的 session 看不见 ——
它们读的是主 checkout / `origin/main`。不推送的占号等于没占。

**为什么值得**：2026-08-26~27 两条线并行，`0.45.36–0.45.40` 被双方各用了一遍、
`0.45.47` 与 `0.45.48` 又各撞一次，前后返工三轮（一方整批后移五个号、
另一方让号两次）。占号只要几十秒，撞号的代价是改 CHANGELOG 标题 + 改提交信息
+ 重跑核对。

**撞上了怎么办**：沿用既定原则 —— **先提交（进 git 历史）的占号**，
后者改号。若双方都只在工作区，则**批次大的保留、单条的让**（改号成本低者先动）。

### CHANGELOG 完整性 hook（2026-09-13 起）

`changelog_guard.py` 挂在 pre-commit + pre-push 上跑 `tests/test_changelog_entry_integrity.py`
（冲突标记 / 重号 / 空标题），只挡**动了 CHANGELOG** 的提交与推往 main 的推送。设计与实测全在该文件 docstring。

- ⚠️ **pre-commit 管不到 `git rebase --continue`**（实测只触发 post-rewrite）——解 CHANGELOG 冲突后兜底的是 pre-push，别以为提交成功就代表检查过。
- `.git/hooks` 不被跟踪：重新 clone 或清过之后要重装 `/usr/local/bin/python3 changelog_guard.py --install-hook`，**否则没人会知道它没了**。

## 整套测试怎么跑（2026-10-21 起）

- **开发期只跑改动相关的测试**（`/usr/local/bin/python3 -m pytest tests/test_xxx.py`），**整套留到收尾跑一次**。
  - **开发期选测工具** `tools/select_tests/select_tests.py`（`make test-select` 看方案、`make test-changed` 跑；v0.45.615，只做第一档）：改的全是**测试文件 / 被跟踪的非代码文件** ⇒ 跑「实测必跑清单」（读全部测试文件的元守卫 + 读过被改文件的测试，登记表 `tests/_must_run.json`，由审计钩子整套实测得出）；**命中生产代码 ⇒ 不猜、提示整套**（退出码 3），命中核心文件（`conftest.py` / `tests/_*` / `config.py` / `hive_logger.py` / pytest 配置 / `.github/` / `tools/clockshift|select_tests/`）⇒ 回退整套（退出码 10）；登记表读不了也回退整套。⚠️ 改了测试文件，元守卫就是「相关测试」——别再按文件名 grep 圈（旧 grep 清单多收 35 个、漏 15 个）。**改了登记表里没有读者的非代码文件（新文件 / 漏登）⇒ 回退整套**（v0.45.661）。`--run` 会顺带测量被改测试现在读了什么，**测试读测试漏登 ⇒ 退出码 4**；修法**照提示打印的整条命令跑**（`--files <漏登的读者> --update`，v0.45.667），并把 `tests/_must_run.json` 一起提交——⚠️ 别自己敲裸 `--update`：它只量相对 `origin/main` 改过的测试，**量不到没改过的读者**（现有测试按目录读到新文件）——分支没改测试时什么都不量；改了别的测试时只量那几个、照样提示「已并入」、退出码 0，看起来修好了、漏登还在（v0.45.672）。CI 在整套上带同一个插件做漂移检查——**非代码文件漏登只出 `::warning`、不挡 main**（加了被现有测试读的新静态文件照样要补登，命令就在注记里）。**只是开发期快检**：覆盖的是「当数据读」的守卫，不覆盖 import 耦合的行为测试；收尾和 CI 仍整套。
- **跑整套前先看有没有别的整套 / clockshift 审计在跑**：`pgrep -fl pytest`、`pgrep -fl audit.py`；有就错开。整套是单进程纯 CPU，并发时同一套会慢到近两倍（机制与实测在 auto-memory `alpha-hive-test-suite-speed.md`）——**两轮并发时段不同的整套耗时不可比，别拿它当回归 / 提速证据**。
- 整套开跑时报告头会自动打一行 `concurrency:`（`tests/_concurrency_hint.py`）：选中条数 ≥ `BIG_RUN_ITEMS` 且同时还有别的大会话（含 clockshift 拨钟的子会话）在跑，或登记不可用而无法判断。**只提示，不阻塞、不报红**；⚠️ **不要改成排队锁**——会让一个会话无声卡住，与「失败要可见」冲突，也会和靠真睡 / 靠超时的测试互相干扰（理由全文在该模块 docstring）。守卫 `tests/test_concurrency_hint.py`。
  - **系统 load 也会提示（v0.45.668）**：1 分钟 load ≥ 核数 ⇒ 另打一行 `concurrency: ⚠️ 系统 1 分钟 load N / M 核`，指给你 `ps -Ao pid,ppid,etime,pcpu,comm -r | head` 去看谁在占 CPU（`ppid=1` 且 `etime` 很长的满载进程多半是泄漏的后台负载）。上面那条只数 pytest 会话，**看不见非 pytest 的负载**——10-27 20 个泄漏的 `yes` 把 load 顶到 124、整套被饿到约 8% CPU、跑了 20 分钟以上，当时一个字没提示。读不到 load 时如实说「无法读取」。同样只提示。
  - **要给测试加 CPU 负载（复现「机器忙时偶发失败」）一律用 `tools/burn_cpu.py`，别手写 `yes &`**：`/usr/local/bin/python3 tools/burn_cpu.py --procs 20 --seconds 600 -- <命令>`。负载进程**自带寿命**（到点退出、父进程没了也退出）、退出时逐个核对零残留，有残留退出码 4。手写 shell 在本机（zsh 不拆词、macOS `pgrep` 无 `-c`）上会无声泄漏，细节在 auto-memory `alpha-hive-environment-facts.md`。守卫 `tests/test_burn_cpu.py`。

## 历史改动查询指针

历史改动**不在本文件维护**（v0.40.3 清理了此前 ~75 行 v0.10-0.19 时代的实现细节清单）：
- 版本级摘要 → MEMORY.md 末尾「版本历史」表
- 逐项细节 → `CHANGELOG.md`
- 定时任务（weekly-optimizer / monthly-self-analysis；每日扫描走 launchd `com.alpha.hive.daily`，不走 scheduled-tasks）的调度时刻 → 以 `list_scheduled_tasks` 返回的 `nextRunAt` 为唯一真相，勿引用文档里的旧时刻

## 核心组件指针（只记"在哪、归谁管"，不记参数值）

- **纸面组合** `paper_portfolio.py`：参数唯一真相 = 模块内 `CONFIG`（v0.39.0 起为回放拐点配置，历史变更查 CHANGELOG）；挂载点 = 日报主流程 `alpha_hive_daily_report._post_scan_notify`（v0.38.0 起，**不再**依赖 generate_deep_v2；v0.45.428 更正：此前这里写的 `_post_scan_enrichment` 不对）；状态文件 `paper_portfolio_state/`（meta.json 的 config_snapshot 自 v0.40.2 每次运行刷新）；KPI 看 `compute_kpis()`
- **权重优化** `weekly_optimizer.py`（Track A）：T+7 回测 → clamp ±10pp → 原子写 config.py，审计日志 `weight_history.jsonl`
- **第三条权重通道，v0.45.176 已断开** `Backtester.adapt_weights()`：曾经每日扫描内调用后经 `QueenDistiller(adapted_weights=...)` 短路掉 `config.EVALUATION_WEIGHTS`（`0.2×config + 0.8×学习值`，即 config 只有两成投票权）。**现降级为只读诊断**，照 v0.44.0 处置 `weekly_optimizer` 的先例：继续算、继续写 `adapted_weights` 表留审计轨迹，**不再进评分**。⚠️ **不要接回去**——它学的是「谁更爱说中性」而非准头（零技能置换检验：5 只蜂 4 只技能 Δ 在 ±2.2pp 内且 CI 全跨 0），且 `max(0.05, acc**2)` 的形式结构上表达不了零权重。理由全文见 `Backtester.adapt_weights` 的 docstring；守卫 `tests/test_zero_weight_invariant.py`（AST 枚举全仓，谁传 `adapted_weights=` 谁红）。详见 auto-memory `alpha-hive-adapted-weights-bypass.md`
- **GEX 直接加减分，v0.45.334 已断开** `gex_regime.GexRegimeModifier`：曾经在 `queen_distiller` 步骤 4.5 直接给 `rule_score` ±0.8（驱动它的 flip 是逐行权价变号，54% 贴在现价 2% 内 ⇒ 系统性扣分；vanna 差 S√T 倍）。**现仅诊断**：照算、落盘 `gex_regime_mod`（`applied: False`），**不进分**。另两条读 OracleBee 主链 `gamma_exposure` 的通道 **v0.45.349 已中性化**：`options_analyzer` 的 `gex_signal` 恒 1.0、BearBee 的 `gex<0` 看空下限已删（该量是截断链上 call−put gamma×OI，实测只携带标的身份、不携带时点信息）。⇒ **规则引擎（生产 `--no-llm`）下 GEX 进评分只剩 `RegimeWeightAdjuster` 全链政体路由**（`confidence_modifier` 只缩放展示用 band_width；LLM 模式下 Oracle / Bear 的 LLM 提示仍带 GEX，未动）。守卫 `tests/test_gex_oracle_bear_neutralized.py`（含 `gamma_exposure`/`gex` 读者白名单 AST）。⚠️ **都不要接回去**；守卫 `tests/test_gex_modifier_disconnected.py`（行为 + AST）。共振加成检验的 replay 按记录的 `applied` 复现（修订 1）。**展示 / 存档读的 GEX（v0.45.362 起）只有一份**：`gex_state`（蒸馏结果 `swarm_results[tk]["gex_state"]`，就是政体路由收到的那份，带 `available`/`reason`）；网站、日报、深度 / ML 报告、MCP、`signal_archive` 的 `gex.*` 都读它，**缺了显示不可得，不回退** Oracle 主链 `gamma_exposure`（另一个量，可与之符号相反）。⚠️ Oracle 的 `gamma_squeeze_risk` 分档方向与展示分档**相反**（正 GEX ⇒ high），只剩 LLM 提示词在读；卖权选择器不共用（冻结预注册读 ≤45 天重定价视图）。理由见 `gex_state` 模块 docstring，守卫 `tests/test_gex_state.py`
- **RivalBee 的 ML 调整项，v0.45.538 已断开** `QueenDistiller._compute_weighted_score`：曾经 `final_score += (rival_score − 5)·0.1·conf·OOS 信任系数`，而 Rival 的概率出自 `ml_predictor_extended` 在 30 条 2023 年硬编码样本上训的模型（已整个退役），信任闸读的却是 `ml_model_cache.json` 真模型的 OOS ⇒ 只向上的抬分。现 Rival 只算 `ml_predictor.expected_returns`（方向不变）、分数 `5.0 + 0.5·tech` 不进分。⚠️ **不要接回**；要让 ML 进分先预注册前瞻检验。守卫 `tests/test_rival_ml_adjustment_retired.py`
- **卖权行权价选择器（对标 GEXBot）** `sell_strike_levels / _candidates / _ledger / _report`（v0.45.333）：CBOE 原始链 → 重定价扫描 zero gamma / 净 GEX majors / DEX / vanna·charm → delta 梯子与六种结构 → 月度 / 周度前向账本（`PATHS.sell_strike_state`）。**不进评分、不上网站**（本地报告 + MCP `alphahive_get_sell_strike_candidates`）；双向 AST 火墙在 `tests/test_sell_strike_integration.py`。检验协议冻结在 `experiments/sell_strike_routing_prereg.md`（首次就绪只跑一次）
- **Alpha Bot（卖权选择器的本机前端，v0.45.388）** `alphabot/`：`/usr/local/bin/python3 -m alphabot`（或 `make alphabot`；`--demo` 合成数据）→ `http://127.0.0.1:8765`，未设 `ALPHA_HIVE_HOME` 时读缺省数据根 `~/alpha-hive-data`（v0.45.422；数据根在别处才要设）。桌面程序：`make alphabot-app` 生成 `~/Applications/Alpha Bot.app`（v0.45.390，壳只 exec `alphabot.launcher`，数据根存 `~/Library/Application Support/Alpha Bot/launcher.json`；壳 cd 进的代码目录缺省是**生产克隆** `~/alpha-hive-prod`（v0.45.436），整次生成（壳、Info.plist、数据根处理）交给克隆**自己的** `main`（v0.45.451；统一入口 `make -C ~/alpha-hive-prod alphabot-app`）、克隆不在就报错不退回（v0.45.439）⇒ 扫描日克隆快进后**重启 Alpha Bot** 才用上新代码；环境里的 `ALPHA_HIVE_HOME` 不改写已配的数据根，要换用 `--home`；要写进 .app / 配置的 `PYTHON` / `--home` / 首次记下的 `ALPHA_HIVE_HOME` 必须是绝对路径，相对或裸名字直接拒（v0.45.456）；make 的 `PYTHON` 只认命令行上给的、环境变量里的忽略，显式空值一律拒，生成时打印写进 .app 的解释器（v0.45.458；推荐入口跑的是**克隆**的 Makefile 与生成器，这些规则在克隆快进到含 458 的版本后才生效）；写进 .app 的解释器须是可执行**文件**（目录不算；生成时与双击时同一判据 `interpreter_problem` / `SHELL_PY_USABLE`），exec 本身失败也弹窗；弹窗里「怎么重新生成」只出自 `macos_app.regen_hint` / `canonical_entry`（帮助页抄后者，守卫 `TestRegenHints`）；环境 `ALPHA_HIVE_HOME` 经 `hive_logger.explicit_data_root` 原样取、`~` 不展开（v0.45.461）；已装 .app 的启动壳过时 ⇒ 启动器每次启动用壳里记下的输入在内存里重生成逐项比（不比版本号），不挡启动地提醒一次（`launcher.check_app_shell` / `macos_app.installed_drift`，v0.45.487）；提醒在启动成功、交出窗口 / 页面时才弹（失败 / 取消不弹不记），给的命令沿用壳里记下的解释器——`regen_hint` 的 `python` 必填（v0.45.498））。GEX 水平（对标 GEXBot Classic）/ 卖权工作台 / 盘中快照 / 账本就绪度 / 程序内「帮助」页（`static/pages/help.js`，v0.45.411；规则常量取自 `/api/method`，守卫 `TestHelpPage`）。⚠️ 与 `alpha_hive_bot/`（Telegram）、Slack「Alpha Hive Bot」无关。不变式：**只绑回环、不上网站**；**只有 `alphabot/service.py` 可 import 卖权模块**（`ALLOWED_IMPORTERS`），对账本只读、从不冻结；盲期按 `sell_strike_report._all_unblinded` 判，历史水平叠其后价格在全部 tenor 冻结前整体锁住；自己的状态只写 `PATHS.alphabot_state`；**不改 levels / candidates 既有输出与版本号**（改了会截断预注册样本，展示用只做纯新增如 `gex_curve`）。守卫 `tests/test_alphabot.py`
  - **「怎么卖」栏（v0.45.515）** `alphabot/howto.py`：「六种结构」下方，点哪行讲哪个结构——下单腿 / 起挂·底线限价 / 止盈·止损买回价 / 标的价位 / 步骤与风险。**纯函数**（不 import 卖权模块，`structure_quote` 与 `BASE_RUNG` 由 `service` 注入）；价位只从同一份报价算，前端只排版。⚠️ 50% 止盈、2× 止损、半价差让价、21 天管理日是**业内经验、未经本系统前向检验**，常量只在 `howto.RULES`；失败写 `howto.available=False` + `stats.howto_failures`。守卫 `tests/test_alphabot_howto.py`
  - **逐票 Delta 对冲（v0.45.526）** `delta_hedge.py`（纯函数决策核心）+ `portfolio_greeks` 接线：每只有跨式腿的标的用**自己的股票**对冲跨式腿净 $Delta（原始值、不乘 β）；带 `delta_hedge_band_pct`（3% NAV）、拉回 0、跨式平仓后清仓、缺数据**逐标的**判。**缺省 `delta_hedge_mode="shadow"`：只算不成交、不写任何账本**（审计 `delta_hedge` 一节 + 对冲页面板）；改 `paper` = 纸面成交（独立账本 `hedge_state/delta_*`，**别复用 `positions.jsonl`**——Alpha Bot `account()` 取第一条当 SPY），要**单独提交**，首日会有约 5 笔逐票成交 + SPY 带联动。⚠️ 3% / 拉回 0 是**主观常数、未经检验**，无点差模型，只对冲 Delta。原因码唯一表 `delta_hedge.REASONS`（前端 `D_REASON` 双向核对）。**成本估算（v0.45.638）**：`delta_hedge_cost_bps`（缺省 5，主观未检验）只给审计 / 对冲页记估算成本、成交名义、笔数与「隐含换手」（`alphabot/hedge.py::implied_turnover`），**不进决策、不扣现金**；shadow 里决策日的名义是「若从空仓开始」的上界；头几个扫描日的回放显示放宽带宽几乎不降成交（样本小）。守卫 `tests/test_delta_hedge.py`、`tests/test_portfolio_greeks_delta_hedge.py`、`tests/test_alphabot_hedge_delta.py`
  - **「不可报价」讲清楚（v0.45.519）**：总览 / 标的页的原因码经 `static/lib.js::reasonInfo` 翻成人话（点差过宽 N% > 上限 / 没有有效 bid …），格子补点差与 OI；`_side_brief` 只纯新增 `ask/mid/oi`。不可报价约一半是**常态**（短腿点差 > 25%，多在低 OI 合约），⚠️ **不放宽 25% 上限**（预注册冻结规则）。新增原因码必须同步登记 `REASON_LABEL`（守卫 `tests/test_alphabot_unquotable.py` 静态双向对账）
  - **「对冲」页（v0.45.459）** `alphabot/hedge.py`：组合 Greeks 的纸面 SPY 覆盖账本（持仓 / 现金 / 成交 / 净值 / 每日带位）。**只读**，数据只经 `portfolio_greeks.ledger_view()`（历史走 `load_history` 放行闸，被拦记录只给计数；本模块不读文件，AST 测试钉住）。守卫 `tests/test_alphabot_hedge.py`
  - **「尾部对冲候选」面板（v0.45.643，Phase A）** `alphabot/tail_hedge.py` + `static/pages/hedge_tail.js` + `GET /api/hedge/tail`：最近一份**放行**审计的压力网格 × SPY 当前链，摆出 6 个**后比例**认沽价差（卖 1 近、买 2 远；**别和卖多买少的前比例混**——那是卖波动率，在最差格里只会加重亏损）的逐格前后对比 / 到期盈亏 / Greeks 变化 / 规模。**纯函数、只读、没有推荐 / 综合分字段、候选按固定网格排不按效果排**；不写账本、不成交。规模**只对下行 / 平盘侧（现货冲击 ≤ 0）最差格定**（认沽管不到上行；审计当天已成交 SPY 时基线最差格会移到上行——按全网格最差格定规模曾把最差候选放大到 −128% NAV；没有现货 ≤ 0 的格就不定规模、**不退回用上行格**），再套未平仓量 2% 与卖出腿名义额 ≤ 100% NAV 两道上限，`offset_achieved` 永远给（2026-10-23：1 单位只抵消下行最差格亏损约 6%–13%，账户相对 SPY 合约太小）。⚠️ 档位 / 1:2 / 45 天 / 50% 抵消目标全是主观常数、**无前向证据**；网格不建模 skew / 时间 / 保证金 / 美式提前指派。⚠️ `portfolio_greeks` 模块头「v1 不用指数认沽」那条决定**未被推翻**——要做纸面成交（Phase C）先写预注册、再改那条。「重新拉取」会清 SPY 的 CBOE 进程缓存（4 小时）；账本读不了报 `ledger_unreadable`、不当成没记录（v0.45.647 二次检查）。守卫 `tests/test_alphabot_tail_hedge.py`
  - **组合尾部预算（v0.45.649，只观测）** `tail_budget.py`（纯函数）+ `portfolio_greeks.compute_day` 接线 + `alert_manager._check_portfolio_tail` + 对冲页「组合尾部预算」面板：压力最差格损失 > 告警线（`tail_budget.CONFIG["alert_pct"]`，20% NAV）⇒ **P2**；逐标的 / 板块的尾部贡献表；**卖跨式影子闸**——今天新开的每笔，按当天实际的账试算加上它之后会不会超 15% / 20% 预算，只记录、**不真跳过**。结果在每天审计文件 `greeks_<日期>.json` 的 `tail` 一节（`price_check.tail` 是小简报）。依据（2026-10-23）：压力最差格约 −23% NAV，几乎全是 6 笔卖跨式（−24.7%），而 gamma / vega 告警按 1% 移动标度、对尾部是盲的。⚠️ 告警线 / 预算都是**主观常数**；影子闸**不是反事实**、评估用当天收盘标记价、**放在 `portfolio_greeks` 而不是 `options_paper_leg` 入场处**（入场代码看不到股票账与 SPY 覆盖）；**真闸会改变哪些单开出来 = 宽跨影子预注册的样本变薄**（起点 10-21，现有 2–3 笔里 15% 档会砍 2 笔），要真拦截先攒影子记录再追加预注册修订。是否响取决于当天最差格是否 > 20%——2026-10-27 首次生产运行（`tail` 一节正常）是 19.66% **没响**（10-26 离线重算 23.2%，差别来自 VZ 的跨式在财报日后一天平仓），**超线触发 P2 的路径在生产里还没真响过**，别当成已验证。**v0.45.655 收口**：告警线 20% = **用户定的风险承受度**（一次「现货 −10%、IV +20 点」最多亏 20% NAV）；**Gamma / Vega 告警线随预算推导**（`tail_budget.derived_greek_lines`：预算对半分 ÷ 压力网格角点冲击 ⇒ gamma 0.10% / vega 0.50% NAV，`portfolio_greeks.CONFIG` 里这两项缺省 `None` = 推导、写数 = 显式覆盖，用 `pg.alert_lines()` / `pg.public_rules()` 取有效值，**别直读 CONFIG**；它们仍只是页面标记、无 alert_manager 路径）；**单仓上限 = 预算 ÷ `max_open`**（3.33% NAV，归因表 `slot`，只展示不拦截；2026-10-23 六笔里五笔超限）；**「压力模型 vs 实际净值」**（`tail_budget.model_check`，只比跨式腿、只比持仓组成不变的相邻审计日，5 对）——结论：压力网格对每日跨式盈亏几乎没有预测力（R² −0.17，因为只建模 β×大盘冲击、抓不到单名事件跳空），Greeks 展开 R² 0.63 但大移动日高估损失（所以派生的 Gamma/Vega 线偏保守）；样本极小，**不能据此调参数**。**β 窗口对照（v0.45.666，只展示）**：压力网格的 β 是审计里的 60 日 OLS，XOM / CVX 的 60 日 β 为**负**（250 日也为负，2 年才翻正）⇒ 「现货 −10%」格把能源股推到上涨 ~10%，下行格里的能源损失是**股价涨**造成的；该格也是 SPY 认沽面板的规模目标。对冲页「压力网格的 β 窗口对照」用 250 日 β 把**最近一份审计**再算一遍并排给出（`tail_budget.beta_sensitivity` 纯函数 + `alphabot/beta_long.py` 取数 + `GET /api/hedge/beta` 单独加载、现取约半分钟、进程内缓存 6 小时）；真实数据 60→250 日：最差格 −23.2→−19.9%（20% 线破→不破）、下行最差 −18.5→−14.1%、β·$Delta −7.2→−17.1%（带内→出带；SPY 覆盖建议吃的那个量）。⚠️ **没有哪个窗口是真值**；**审计里的压力表 / 告警 / SPY 覆盖账仍只用 60 日 β**，对照不落盘、不进告警、不改成交——`beta_sensitivity` 等名字除展示层外谁都不许引用（守卫 `tests/test_beta_sensitivity.py::TestDisplayOnly`）；要换覆盖用的 β 窗口会改变纸面账成交，是另一个要单独决定、写依据的事。守卫 `tests/test_tail_budget.py`、`tests/test_portfolio_greeks_tail.py`、`tests/test_alphabot_tail_budget.py`、`tests/test_beta_sensitivity.py`
  - **bid = 0 的深度虚值腿（v0.45.609）**：`portfolio_greeks.option_exposures` 对「bid = 0、ask 真实、iv > 0、四个 Greeks 齐全」的腿照常取 CBOE Greeks 进汇总，行上标 `bid_zero`、`mid` 仍为 None（压力表因缺 mid 单独剔它并写明 `mid(bid=0)`）；此前这类腿被当「缺报价」、整组 β·$Delta 变 unknown 而不对冲（2026-10-21 T 的 24.5 看涨）。⚠️ 只放宽 Greeks 汇总这一处，`cboe_options` 的 `quote_ok`（卖权 / 跨式入场 25% 点差口径）不动；判据**只有一份** `cboe_options.bid_zero_greeks_usable`（v0.45.620 起对冲账本与跨式页共用，守卫递归扫全仓、除这两处不许再定义 / import）；跨式页账本视图同样标 `degraded_legs`，对冲页 / 日报覆盖行显示 `n_bid_zero`（不进 `gaps`、不告警）。守卫 `tests/test_portfolio_greeks_bid_zero.py`
  - **「溢价」子页（v0.45.492）** `alphabot/premium.py`：单票全链按 IV ÷ 期限匹配 RV 排序（筛选工具，**未经前向检验**）。纯函数（AST 守纯度），外部函数经 `Deps` 由 service 注入；取链只经 `_premium_fetch`、**不清 CBOE 进程缓存**（盘中自动刷新走 `refresh=1` ⇒ 先 `live(force=True)` 再算，v0.45.536）；「首选」卡片 `premium.picks` 在截断前挑、只排除**已知且权威**的风险（财报日 / ≤2 天 / 无 RV），判不了、期限结构鼓包（启发式）、盘中未成交都只标注（v0.45.536 / 539）；前端子页活性 = 路由活 **且** 自己的容器还挂着（「重新拉取」在同一路由里重建子页，旧实例的定时器否则会在后台续拉，v0.45.539）；财报日取卖权账本里最近一次有效扫描的 Chronos 日期，**不读** `earnings_cache` 兜底缓存（实测有日期偏晚、会把「到期前」判成「到期后」）；IV 历史 / VRP 账本只读（`_read_index`、缓存目录不 mkdir）。筛→排→截断都在服务端；原因码唯一表 `REASONS`（AST 双向对账）。守卫 `tests/test_alphabot_premium.py`
  - **对话抽屉（v0.45.614）** `alphabot/chat.py` + `static/chat.js`：Claude Opus 5.5 + 只读工具（只调页面 GET 背后的服务方法 `TOOL_METHODS` ⇒ 盲期锁住的模型也看不到）+ 联网搜索（用户 2026-10-22 定）。**默认关**（设置 `chat_enabled` 只认真布尔；演示模式不开）；每天上限 `DAILY_CAP_USD`，调用前按最坏情况预检、调用后按 `usage`（`usage.iterations` 逐次尝试按各自模型计价、搜索按次）记账到 Alpha Bot 状态目录 `chat/spend.jsonl`（重启不清零）；对话历史只增不改（Opus 5.5 思考块绑定先前对话）；工具结果抹掉本机路径。⚠️ 测试一律假客户端或 httpx MockTransport，**真调 API 前先问用户费用**。守卫 `tests/test_alphabot_chat.py`
  - **yfinance 备用取数源（v0.45.603）** `alphabot/yf_chain.py`：CBOE 报 `stale_vintage` **且**票在 `service.YF_FALLBACK_TICKERS`（目前 NVDL：CBOE 文件盘中停在昨天；BILI（v0.45.662）：10-27 一个时点实测文件盘中不更新、夜里刷新，刷新时刻待验证）才改拉 yfinance，只用于现算页（盘中 / 标的 / 总览），CBOE 恢复自动改回。⚠️ **溢价页、卖权账本、日报仍只认 CBOE**（预注册检验的数据源不许换；守卫 `tests/test_alphabot_yf_chain.py`、`tests/test_alphabot_premium.py::TestProductionFetchPath`）——所以 BILI 在账本里仍常记 `stale_vintage`；没有交易所 Greeks，Yahoo IV 失败值当缺失；**Yahoo 限流（v0.45.675）**：备用源在 Alpha Bot 里常被 429 挡掉（2026-10-27 / 28 清晨 NVDL 备用源共 16 次失败、**全是限流**，每次轮询的第一个请求 `Ticker.options` 被拒、`yf_gate` 冷却 180 秒；同时段新开的进程取同一条链是通的 ⇒ 间歇限流；被拒的那几轮 Alpha Bot 每轮只发了 1 个请求（第一个就被拒；成功的一轮最多 12 个），清晨窗口里没有任何 launchd / 定时任务碰 yfinance，**谁在消耗额度日志里查不出**）⇒ 限流单独成原因码 `yf_rate_limited`（页面写「约 N 秒后再试」、每一轮限流**自动重试一次**，等多久只由闸门冷却决定、不设固定下限、冷却期内不发请求）、β 对照按标的缓存（只补缺的、永久判不了的标的不重取不续命、强制刷新被限流时保留旧值但明说「刷新失败」+ 值的真实年龄）；`yf_gate` 日志带 pid / 命令行 / 连续第几次 429 / 距上次成功多久（**行为一点没改**：429 = 立即停、不重试）；**每张链先过 `oi_staleness`，Yahoo OI 陈旧 / 判不了 ⇒ 整份拒收（`yf_oi_stale` / `yf_oi_unjudgeable`），不当 OI=0 照用**（BILI 在 Yahoo 侧出过坏数据：10-12 OI 清零、put/call 298.46；个别合约漏记 OI 判据抓不到）。
  - **「跨式」页（v0.45.424）** `alphabot/straddle.py`：财报跨式纸面账本的持仓 / 平仓 / 逐仓 Greeks / 盘中参考报价（`cboe_options.quote_held`）。**只读**，不写账本、不拿账本 mark 顶替拿不到的报价；**不显示任何一笔的 `gex_ctx`**（跨式 GEX 预注册的盲期，协议 `experiments/straddle_gex_prereg.md`，冻结只经 `straddle_gex_prereg.py --run-once`）。信号行的 `gex_ctx` 只记录、不进开单（守卫 `tests/test_straddle_gex_shadow.py`、`tests/test_alphabot_straddle.py`）
  - **「事件前被平」读者侧标记（v0.45.606）**：`options_paper_leg.event_missed`（平仓日 ≤ 财报日 ⇒ 没测到事件；缺日期 ⇒ None）+ `split_by_event`，页面 / 日报 / `kpis()["event_split"]` 共用；**账本不存这个字段**，顶层 KPI 仍按全部已平仓（对得上账户已实现），「按方向」只含测到事件的那批
  - **「跨式 vs 宽跨」对比卡（v0.45.584）** `alphabot/strangle_compare.py`：同一份当日快照的 ATM 跨式与 25Δ 宽跨并排（权利金 / 进出成本 / 点差旗标 / 盈亏平衡带 / 历史涨跌落在带外 k/n / 按账本定仓的张数）。⚠️ **只并排事实、没有推荐字段**（系统里已结算事件极少，任何「按标的选结构」都是拟合；测试钉住键名）；账本按权利金定仓 ⇒ 宽跨张数是跨式的数倍，名义风险不一定更小
  - **影子宽跨记录（v0.45.586，协议 `experiments/straddle_strangle_shadow_prereg.md` v1；v0.45.593 修订 2）** `straddle_strangle_shadow.py`：信号行 `strangle_quote` / 持仓 `shadow_strangle` / 平仓记录里的假想盈亏，**只记录、不进任何判断**（守卫 `tests/test_straddle_strangle_shadow.py`：有无记录的账本行为差分 + AST 读者白名单）。⚠️ **盲期：冻结前不得把某笔的影子盈亏与它的跨式盈亏拼在一起看**（含「看看 T 那一笔」；`closed_trades.jsonl` 里两个盈亏都在，是信息论上盲不了、靠纪律）；登记前入场的 8 笔不进检验；**平仓日 ≤ 财报日的单位也不进（`exit_before_event`，修订 2：账本在财报日 = 到期日 − 2 的盘后财报当晚就按 `expiry_buffer` 强平；v0.45.602 起账本入口已不再开这类单——`options_paper_leg.expiry_buffer_hit` 入场与强平共用，信号的 `eligible` 刻意不动，它是 GEX 预注册的样本定义，本排除规则留给存量单）**；进度 `/usr/local/bin/python3 straddle_strangle_shadow.py`，冻结只经 `--run-once`。**冻结后 Alpha Bot 影子面板自动解锁每笔配对 + 冻结结果**（v0.45.599，`straddle_strangle_shadow.reveal`：钥匙只有「冻结文件存在且完好」；冻结前接口里没有 `revealed` 键）。`options_paper_leg.run_for_date` 的返回值 / CLI 回显**剥掉了** `shadow_strangle`（手动补跑平仓日不会把一对盈亏摆在一屏；要看落盘记录是偷看）。记录失败在源头登记（scan_timing `strangle_shadow` → alert_manager P2；`wiring_suspect` = 一整轮 ≥5 个标的 25Δ 腿缺且一个都没取到 ⇒ 也是 P2）。**改度量 / 单位定义 / 排除规则 = 协议变更**（协议 §8、§14 记修订，`PREREG["version"]` 加一）
- **账本读写** `ledger_io.py`（v0.45.448；v0.45.452 加整文件文档 `write_json` / `load_json`）：加锁（目录 flock，同进程可重入）+ 原子替换 + 严格序列化（拒 NaN / 非对象）+ 写后回读；读到坏行 / 坏文档抛 `LedgerCorrupt`（外部损坏 ⇒ 停下、从备份恢复，**不跳过、不当新账本**——`meta.json` 里存着现金）。失败在它**源头**登记（`failures()` ⇒ status.json ⇒ P2），调用方吞异常也照红。⚠️ 新账本 / 状态写入**一律走它**，可再生缓存走 `hive_logger.atomic_json_write`；守卫 `tests/test_ledger_writes_go_through_ledger_io.py` 按**写入落点**认（不看函数名），存量 84 处逐条按类别登记在 `WRITERS`，「待迁移」类有上限、只许减
- **财报日唯一入口** `earnings_calendar.py`（v0.45.500）：唯一日历解析器 + 读取入口（读本轮 / 已落盘的 ChronosBee `details.earnings_calendar` 按日观测，**不打网**）；扫描、卖权 / 跨式账本、展示一律经它。`EarningsWatcher.get_earnings_date` 与 `earnings_cache/{T}_date.json` 只服务 `--check-earnings`，**不得被别处读**（守卫 `tests/test_earnings_calendar.py::TestSingleEntry`）。ChronosBee 评分口径（丢当天财报）刻意不动——改它截断维度 IC 协议 H2
- **yfinance 日线唯一入口** `yf_daily.py`（v0.45.652；v0.45.658 起两个 API 都收）：`yf_daily.history(tk, source=, **req)` → `(日线, 最近一场缺了)`；`yf_daily.download(yf, tickers, source=, **req)` → `(帧, {票: 最近一场缺了})`——内部 actions=True 取、去掉「公司行动空行」后**还原成 actions=False 的那一帧**（按日期用的调用方逐位不变）。判据 / 按行号删 / 首末段只有 `price_metrics.event_row_layout` / `drop_positions` 一份（空行只会在首段 / 末段）。末段按「剩下的末根是不是**最近一场已收盘**的交易日」判（`latest_completed_session`，盘前跑除息日照用前一场）；为真 ⇒ 用末根当今天的调用方当取不到。⚠️ **新代码读 yfinance 日线一律走它**（`SOURCES` 登记调用点名、`tests/test_yf_daily.py::POLICY` 逐处写末行策略 refuse / ignore）；`.download(` 直调零例外，`.history(` 直调只许 `EXEMPT` 四类（原样落档 / 645 旧回落 / 要事件列 / 1m 日内，只许减）；**别用 `actions=False` 代替**、调用方别传 `actions` / `keepna`。预取包把「最近一场缺了」的票记在 `history_missing`，Rival / Scout 访问器据此当取不到、不直连重取。H1 三字段四条路径仍是 645 的 `drop_event_only_rows`（末段即缺，用户 10-25 定、冻结层对拍）。Guard 世代印记登记在 `guard_bee.GENERATION_MARKS`，**出错路径也写**（`_with_generation_marks`；新印记只写成功路径 ⇒ 守卫红）。守卫 `tests/test_yf_daily.py`
- **日健康判定** `day_health.py`（v0.45.598）：同一数据源族（yfinance / CBOE）≥2 个 Step 12 字段同时低于覆盖线 ⇒ `source_outage`；纯函数、读 `.swarm_results_<日期>.json`，**不落盘新产物**。扫描末尾写 `scan_timing.extra.day_health` → `alert_manager._check_day_health` 每个故障源一条 P1（没统计 / 取值不认识 ⇒ P2）；两份前瞻检验 `run()` 显示故障日，并列报告「去掉 source_outage 日」的**敏感性分析**（v0.45.605 修订 3，定义指纹冻结在 `day_health.REGISTERED_FINGERPRINT`（含 `MIN_TICKERS=10` 小样本下限，v0.45.608 同日勘误）；不触发动作、主分析不变；2026-10-14 经用户决定**不排除**；改 `scan_coverage_gate.FIELDS` 会让指纹变、守卫红——要新开修订）。⚠️ alert_manager（Step 6）先于 Step 12 跑，想让后面步骤的结果进告警必须由扫描进程自己算进 `scan_timing.extra`。故障日 ≠ 协议排除日（`FORWARD_TEST_EXCLUDED_DAYS`），排不排是协议修订。守卫 `tests/test_day_health.py`
- **H1 原始观测档案** `observation_archive.py`（v0.45.568，buzz_v1 根治阶段 1）：喂 Buzz 四个情绪通道的原始取数（Reddit 两张榜 / 新闻条目 / 热搜表 / F&G）在取数处**内容寻址**落档（`PATHS.observation_archive`），每个分数经 `details.observations` → `observation_links` 表（与 `buzz.comp.*` 同一事务）指出它用的是哪一份。只在扫描进程 `enable()` 后写盘，**只存、不改值**；缺链接 / 不在档 / 落盘失败 ⇒ P2。⚠️ 给 H1 通道加取数源 = 在 `KINDS` 登记 + 取数处 `observe`，否则阶段 2 的冻结通道层重算不了。守卫 `tests/test_observation_archive.py`
  - **观测层** `observation_layer.py`（v0.45.580 起，取数与解释分家的第一块）：行情日线（`yf_history`）与交易所时钟（`spy_last_minute`）从这里取、取到即落档、返回内容不变；`data_pipeline` 三条路径把用了哪几份登记进 `StockData.observations`（回落补上的登 None = 用了但没存）。版本号 `observation_archive.LAYER_VERSION`；⚠️ **改它的源 / 参数 / 兜底顺序 = 新版本**（buzz_v1 固定用旧版本，双版本接线在「观测层版本化」那步做），在那之前它整份在输入层指纹 `price.fields` 里。守卫 `tests/test_price_observations.py`
  - **取数缓存存原始观测**（v0.45.591，观测层版本化第一块）：新闻 / F&G 缓存存「取数结局」（种类 + 原始内容 + 取数时刻 + 新闻主源结局，`observation_layer.cache_*`，文件名带观测层版本），命中时用与新取数同一个解释函数重算并补档；Reddit 单票派生缓存已删。⇒ MCP / 手动跑先填了缓存，扫描命中照样档案完整、不再白丢桥接日。⚠️ 情绪取数模块加缓存**只许存原始观测**（守卫 `tests/test_observation_archive.py::test_sentiment_modules_only_cache_raw_content`）
  - **输入层指纹守卫** `signal_code_fingerprints.py`（v0.45.554；v0.45.671 起覆盖范围**自动推导**）：单元指纹 = 定位代码 + 依赖闭包（跨文件追引用，config 项精确到读过的键，缺省一律算进去；v0.45.676 起 `self.方法` 按继承链解析、config 值里的名字与写在任何位置的就地修改也追；v0.45.679 起类内给 `self.x` 的赋值也算，只有从类外注入的实例属性推不出来）⇒ H1 相关代码**新读一个配置项 / 改一个被调用的辅助函数**也会红、要用户判；不改值的基础设施只经 `EXEMPT_DEPS`（必须写理由、过期即红）豁免，**别往里塞来消红**。看某单元盯着什么：`--deps <单元>`；定义一变要在窗口内各扫描日提交上重算审计（`experiments/fingerprint_coverage_audit.py`）。守卫 `tests/test_signal_code_fingerprints.py`
  - **冻结通道层** `buzz_v1_channels.py`（v0.45.583，根治阶段 2）：原始观测 → 7 个通道，逐字抄窗口起点 10-12 的代码（纯 Python、不 import 生产代码 / config / pandas，数值逐位复刻 numpy 成对求和）；`buzz_v1.py --bridge` 逐日核对它与入档通道逐位相等，结果进维度 IC 进度行（阶段 4 前只是进度）。⚠️ 照抄的含义是**不修**窗口起点的怪行为（新闻时效加权死代码、F&G 兜底加密指数……），要修 = 新定义重新登记；阶段 4 前生产改了这些通道算法 / 常数 ⇒ `tests/test_buzz_v1_channels.py` 的常数对照与差分对拍开发期红
- **月度自诊断** `self_analyst.py`（Track B）：输出 `self_analysis_briefs/YYYY-MM.md`，含每蜂维度 rank-IC 小节（v0.40.0）
- **LLM 影子 A/B（v0.45.555）** `llm_shadow_scan.py`（入口）+ `llm_shadow_report.py`（读者）+ `experiments/llm_shadow_prereg.md`（预注册，**看数据之前写的**）：生产永远 `--no-llm`，想知道「LLM 开是否更准」只能前瞻——每天紧随生产 14:00 扫描之后，在**独立数据根**（缺省生产根的同级 `alpha-hive-llm-shadow`）里开着 LLM 再跑一遍，**只记录、不进评分**。不变式：① **两进程**——`config.py` 在 import 期固化 `PATHS.*`，不能在已 import 的进程里改数据根，扫描必须在以影子根起的新子进程里跑（子进程开扫前核对 config 无任何值指向生产根）；② 缺省 `--plan` 不花钱，`--run` 还须环境变量 `ALPHA_HIVE_LLM_SHADOW_SPEND=1`，**首次真跑必须先问用户**（费用 / 上限常量在 `llm_shadow_scan.py`）；③ ⚠️ **影子里的 `rule_score` 不是规则臂**（蜂层面的 LLM 已混进维度分，只隔离 Queen 那层）——规则臂 = **生产同日同标的的结果**；④ 不健康的一天（预算 / 定价拒绝、截断 / JSON 失败率超阈值、蒸馏没走 LLM）整天不入比较；⑤ 提示词 / 模型 / `max_tokens` / 精简规则任一改动 ⇒ 新一代、重新计数；⑥（v0.45.560）**一天只许付费跑一次**——$3 上限只在进程内计，跨进程靠 `shadow_status/<日期>.json` 挡（子进程起飞前先落占位；`--control` 写 `<日期>.control.json` 分文件）；v0.45.633 起锁的是**花过钱或花费未知**（`day_lock`：占位 / 读不了 / 任何花费或调用迹象 ⇒ 锁；完整写完且花费全 0 的失败 ⇒ 允许同日重跑，旧记录并进 `previous_attempts`）。`llm_service` 的相关护栏：未登记定价的模型一律拒绝、5.5 代模型不传 `temperature`、成本公式里 `usage.input_tokens` **不含**缓存 token。（v0.45.673）模型自己中途停笔（`stop_reason=refusal`，10-27 NFLX Queen 418 字符处断掉）单独计数 `model_refusals`、写进状态文件，**只计数不重试、不进健康判定**；JSON 失败日志带 stop_reason / `stop_details`（有就记，API 是否给待验证）/ 块类型 / 解析错误及其相对原文的偏移（v0.45.680 更正：此前是相对被解析片段的）/ 出错处附近 / 前 2000 字符。⑦（v0.45.592）**第 2 代 = Sonnet 5.5、30 只全跑**（用户 2026-10-22 定；v0.45.594 起影子里**关掉历史类比**——`agent_memory` 的唯一读者，只在 LLM 模式生效、只改分不改方向；真实扫描入口里打补丁，触发了会判当天不健康）；状态文件带 `generation`，报告只数最新一代，并列出「生产有扫描而这一代没跑」的缺口日。**每日自动跑**：`llm_shadow_daily.py`（plist 标签 `com.alpha.hive.llmshadow`，工作日 14:45，跑生产克隆里的脚本；等生产写完、**开跑前先探活 3 个数据源的 HTTPS（v0.45.633，≥2 个通；10-23 全站 TLS EOF 丢了一天的教训）**才开跑，等不到 / 网络一直不通 / 太晚 / `<影子根>/DISABLED` 存在 ⇒ 跳过不花钱）——**全仓只有它替用户设 `ALPHA_HIVE_LLM_SHADOW_SPEND`**（守卫 `tests/test_llm_shadow_daily.py`）；⚠️ **仍没接编排器**（独立 launchd 任务）。停：`touch ~/alpha-hive-llm-shadow/DISABLED` 或 `launchctl bootout gui/$(id -u)/com.alpha.hive.llmshadow`。接入细节读 auto-memory `alpha-hive-llm-ab-pilot.md`。守卫 `tests/test_llm_service_5_5_models.py`、`tests/test_llm_shadow_scan.py`、`tests/test_llm_shadow_report.py`、`tests/test_llm_shadow_daily.py`
- **IBKR 桥接** `ibkr_sync.py`：手动流程（export actions → 用户 TWS 下单 → import CSV → reconcile），状态在 `paper_account/`
- **生产代码 = 独立克隆 `~/alpha-hive-prod`（阶段 8，v0.45.431）**，不是 `~/Desktop/Alpha Hive`（那里是**开发检出**，worktree 都挂在它下面）。克隆只由扫描前 `production_sync` 快进；它的 `.git/hooks` 拒绝一切让 main 偏离 origin/main 的写入（`production_clone.py`，守卫每轮自动补齐、不合格进 P1）。⚠️ **不要在生产克隆里开会话、改文件、提交**；改代码 = 开发检出开 worktree → 推 origin/main → 下一个扫描日自动进生产。判「下一轮生产跑哪版」：`git -C ~/alpha-hive-prod fetch -q origin && git -C ~/alpha-hive-prod rev-list --left-right --count main...origin/main`（**refs 不再与开发仓库共享**，在开发 worktree 里查到的 main 不是生产的）。体检：`/usr/local/bin/python3 production_clone.py check`。守卫 `tests/test_production_clone.py`、`tests/test_orchestrator_runs_production_clone.py`。开发检出根启动的会话（含周 / 月定时任务）由 SessionStart 钩子先快进 main（`scripts/dev_checkout_sync.py`，配置 `.claude/settings.json`；干净且只落后才动、merge 从不被杀，给出 CLAUDE.md / `.claude/` 的变化，v0.45.467）；克隆里的 `origin/cloud-snapshots` 由读者自己 `cloud_snapshot_loader.refresh()`，每日扫描后台观测落后交易日数 → P2（v0.45.466）
- **编排器（v0.45.353 起受版本控制）**：唯一真相 = 仓库 `scripts/alpha-hive-orchestrator.sh`；launchd 跑的是**部署副本** `~/.claude/scripts/alpha-hive-orchestrator.sh`（路径不变：Desktop 有 TCC 限制）。⚠️ **不许直接改部署副本**——改仓库那份 → 合入 origin/main → **下一个扫描日自动部署**（v0.45.370 阶段 3：production_sync 结局 OK 后调 `deploy_orchestrator.py --ref HEAD`；关卡 / 漂移拒绝 / 原子替换在工具里；同步没成功那轮 skipped；结局在 status.json `steps_result.orchestrator_deploy`，记录 `~/.claude/logs/orchestrator_deploy.json`）。⚠️ 部署那轮仍跑旧编排器——新编排器从合入后**第二个**扫描日起执行，与 Python 耦合的改动要兼容一轮。首次上线 / 急用才手动：`/usr/local/bin/python3 deploy_orchestrator.py --ref origin/main --dry-run`，再去掉 `--dry-run` 加 `--out ~/.claude/logs/orchestrator_deploy.json`。**回滚 = 在 main 上 revert 那次合入**（下一扫描日自动部署）；手动部署旧提交只管一轮、下一轮会被自动部署盖回 main 最新版。⚠️ 坏编排器若在部署块**之前**就崩（如同步前的裸变量），它部署不了自己的修复 ⇒ 手动部署修好的 main。一致性守卫（漂移 / 该部署没部署；合入后待下一轮且 ≤6 天不算红）= `tests/test_orchestrator_deployed_matches_repo.py`；读编排器原文的测试一律经 `tests/_orchestrator.py`，CI 上也跑

## GitHub Pages 部署规则（永久设置）

- **GitHub Pages 从 `gh-pages` 分支部署**，不是 `main`
- `report_deployer.deploy_and_notify`（v0.45.402 前叫 `auto_commit_and_notify`）：生产模式（LLM 或蜂群）只**同步 gh-pages**；**不再提交、不再推代码仓库 main**（数据根迁移阶段 6：生产数据在 `$ALPHA_HIVE_HOME`、代码仓库不跟踪数据；异地副本靠编排器 Step 14 的私有备份）。⚠️ **勿重建日报提交 / 推送链**（`production_sync.push_main`、日报白名单都已删，墓碑测试 `tests/test_git_failures_are_visible.py::TestCommitPushChainStaysRetired`）；`gh_pages` 结局键必须始终写进 `scan_timing.extra`（编排器拿它当「主流程已跑完」的证据）。非生产扫描不部署；v0.45.210 撤掉的 test remote 推送分支同样**勿恢复**（勿往 `GitHubTool` 白名单加 `checkout`/`reset`）
- **非蜂群扫描已退役（v0.45.213）**：`alpha_hive_daily_report.py` 不带 `--swarm` 直接 exit 2，**勿重建 `run_daily_scan`**——它在扫描中途就写概率账本（先写者占位），在 save_report 前短路管不到；理由在 `main()` 该闸注释，守卫 `tests/test_non_swarm_scan_retired.py`
- `generate_ml_report.py`：末尾调用 `_sync_ghpages()`，每次生成 ML 报告后自动同步 gh-pages
- **禁止**只推 main 不推 gh-pages，否则网站不更新

## Memory 2.0 自动更新规则

- **Auto Memory 路径**：`~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/MEMORY.md`
- 每次 session 修改了代码/新增模块/修复 bug 后，Claude 必须自动更新 MEMORY.md 对应章节
- 控制在 200 行以内；超出时压缩旧版本历史或移除已被代码覆盖的实现细节
- 旧记忆路径 `~/.claude/projects/-Users-igg/memory/` 已弃用，勿再写入
- **索引行只写「这条是什么 + 什么时候该打开它」，单条 ≤150 字符**；细节一律进 topic 文件
  （2026-09-11 实测教训：索引曾涨到 40KB、单条 2054 字符，且**有些教训只活在索引行里、
  topic 文件一个字都没有** ⇒ 照字数裁会无声丢知识。裁之前必须先把索引行里的标志物
  逐个 grep topic 文件，缺的先搬回去再裁。）
- **这条上限自 v0.45.208 起有会红的守卫**：memory 仓的 `check_index_line_length.py`
  （pre-commit）。**动了 `MEMORY.md` 才拦，没动只吵** —— 例行拦无关提交会把人
  养成 `--no-verify` 的习惯。**只检测，绝不自动裁**：裁哪里是语义判断，
  自动裁剪器正是上一条要防的东西。接手 / 重装（幂等，钩子不进 git、clone 之后就没了
  且无人知晓；**必须写全路径**，从本仓 worktree 执行相对路径是 exit 2）：
  `/usr/local/bin/python3 ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/check_index_line_length.py --install-hook`

### memory 目录已纳入 git，**每次 session 收尾必须提交并推送**（2026-09-11 起提交；2026-09-18 起加推送）

`~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/` 是一个独立的 git 仓库
（分支 `main`）。**2026-09-18 起已有远端** `origin` → 私有仓库
`wangmingjie36-creator/alpha-hive-memory`（用户在对话里明确批准后新建并首推；
建仓前已扫过当前内容 + 全部 git 历史，零命中密钥形状字符串）。没有任何东西会
替你提交，所以这是 session 收尾清单的一项，与「更新 CHANGELOG.md」并列：

```bash
git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory add -A && git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory commit -m "<本次记了什么>" && git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory push origin main
```

- **没有改动就不提交**，别造空提交（自然也不用 push）。
- ⚠️ **这个目录会被多个 session 并发写**。`add -A` 可能捎上别人未提交的编辑——
  记忆是追加性的，捎上比让它一直裸着好，**但 commit message 必须如实说明
  哪些文件不是本次改的**，否则历史会张冠李戴（同「并发开工必须先占号」那节的教训）。
- **推送已获用户明确批准为默认行为，不必每次重新问**（2026-09-18 用户在对话里
  明确要求「以后每次收尾都自动推」）——这条本身就是「durable instruction」，
  区别于一次性的「这一次可以推」。若某次改动的内容明显敏感（本仓迄今未出现过），
  推送前仍应停下来问。
- 加远端本身（第一次从无到有）属对外动作，当时已经问过用户；**这条历史决策
  不需要每次复述**，以后新建远端才要重新走「先问用户」。

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

写 `if not X.exists(): pytest.skip()` 之前先回答一句：**X 在哪些环境里存在？**
若答案是「只有一台机器上的一个目录」，那这条测试**等于没有**——
**「加一个 skip 守卫」和「让这条测试在任何地方都跑不到」之间，只隔着一个未被 git 跟踪的文件。**

判据：依赖若由测试自己构造，守卫该是断言不是 skip，且要正面核对夹具真的接上了（不能只断言「没读生产库」）；同样的条件性写在 marker 上可见、写在 skip 里不可见，真需要外部系统的测试标 marker 别写 skip；数一数还有多少条挂在同一个 X 上，但别一刀切——生产数据本身退化的测试该标 marker 不是补桩。

与上一节同源：`skip` 把「这条没验」渲染成「这条没问题」，都答不出「谁会红？」。实测案例（`TestSPYBenchmarkUnavailable`/`pheromone.db`）+ WAL 边车取证陷阱见 auto-memory `alpha-hive-failure-propagation.md`。

v0.45.165 普查全仓（20 条，5 个文件）后补三条：

1. **模块级 `pytestmark = skipif(...)` 会连坐**整个文件，包括与该条件毫无关系的测试。
   实测最刺眼的一处：`TestGuardsHaveTeeth`（纯合成数据、零外部依赖，docstring 写着
   「没有这一组，本文件的全绿证明不了任何事」）跟着一起恒 skip ——
   **证明守卫有牙的那把尺子，自己从未被拿出来过。** 条件性要挂在**用到它的那个类**上。
2. **「数一数还有多少条挂在同一个 X 上」要按后果数，不能按 token 数。** 同样的恒 skip
   还能由**测试隔离泄漏进子进程**造出来：`_isolate_env` 把 `ALPHA_HIVE_HOME` 指向沙箱，
   `subprocess.run(..., cwd=_ROOT)` 继承了它 ⇒ CLI 永远读空库 ⇒ 输出恒为「无可用样本」
   ⇒ skip 恒中，**在每一台机器上，生产机也不例外**。那条测试里一个产物名 token 都没有。
3. **判据不是「不许 skip」，是「谁会红？」。** 反例：`git check-ignore` 返回 128 时
   skip 是正当的 —— 「git 仓库在哪些环境里存在」答「开发检出/worktree/CI 全都是」，
   条件为真的是全集减一个人造特例。⚠️ 顺带：`git check-ignore` 退出码有**三**个含义
   （0=忽略 / 1=未忽略 / **128=这里不是 git 仓库**），把 128 揉进另外两个就是
   「把一种失败误报成另一种」，正是本节要治的形状。

守卫落地在 `tests/test_no_invisible_prod_data_skips.py`（元守卫，命中即红）。

## 硬检查项：新产物的默认路径不许是相对路径（2026-09-07 起）

给任何会落盘的产物写默认位置前，先回答一句：
**这个默认值在「不是从仓库根跑」的时候会写到哪？**
答案若是「跟着 cwd 走」，它就是一颗定时炸弹——**测试的 cwd 不受控**。

**文件名唯一真相在 `hive_logger.PATHS`**（与 `PATHS.db` / `PATHS.chroma_db` 同族）。
新产物在那里加一个 property；函数默认值写 `None`，在函数体里解析——**不要**写成
模块级常量或类属性（那会在 pytest **收集期**——早于任何 fixture、任何 env 隔离——
被 import 时就地冻死，`_isolate_env` 类的隔离对它完全无效）。

判据：路径要集中，但**文件不能合并**（两个同族文件恰好不同名，才使某次事故实际损害为 0）；
只跑单文件验证不了这类冻结 bug（ordering-dependent），必须在没有该产物的干净目录跑全套看它
有没有凭空造出来；新增产物必须在 `tests/conftest.py` 配两道防线——① setup 正面核对默认位置
确实在沙箱内，② teardown 比对仓库根与 cwd 的真身指纹（②专门抓①会漏掉的那类 bug）。

与上一节同源：把路径冻成模块级常量，等于把「隔离失效」改写成了「隔离生效」，同样答不出「谁会红？」。
全套判据、事故取证（v0.45.149/150/160，含 `__file__` 派生一族）见 auto-memory `alpha-hive-test-writes-production.md`。

**裸字面量缺省是另一族（v0.45.463）**：`db_path="pheromone.db"` / `os.environ.get(X, "cache")` 不含 `PATHS` / `__file__`，上面两条物种守卫都看不见；生产 cwd 是代码克隆，它们在那里恒读空且不报错。守卫 `tests/test_price_history_default_paths.py::TestNoNewCwdRelativeDataDefaults`（存量只许减）。

## 硬检查项：夹具里的日期会不会和真实钟比？（2026-10-13 起）

会 ⇒ **被测路径读几只钟就钉几只**（`cboe_options._pdt_now`、各 `_snapshot_now`……；钉一只漏一只正是 v0.45.489 那颗）；
没有注入接缝 ⇒ 在测试体内按真实今天算相对日期。写完跑一次可执行审计，别靠 grep 日期字面量：
`/usr/local/bin/python3 tools/clockshift/audit.py --days 7 --days 45 -- tests/test_xxx.py`（CI 的 `clockshift.yml` 在 push 与每日定时跑全量）。
harness 的不变式「拨整个世界、不拨一只钟」与已知漏点见 `tools/clockshift/clockshift.py` docstring；历次炸弹与教训见 auto-memory `alpha-hive-time-bomb-audit.md`。

## 硬检查项：这个路径指向「代码」还是「数据」？（2026-09-08 起）

**数据根规则（v0.45.422）**：唯一实现 `hive_logger.resolve_data_root`——`ALPHA_HIVE_HOME` 非空就用它，否则
`$HOME/alpha-hive-data`（**不再**兜底到代码检出：阶段 5 后那里只剩冻结旧数据，交互 shell 手动跑会静默读旧数据）。
测试会话在 `tests/conftest.py::pytest_configure` **收集之前**设会话级 `ALPHA_HIVE_HOME` 沙箱，并记下调用时环境给
「生产在哪」的守卫用——别把这个沙箱挪进 session 夹具（太晚，收集期冻住的常量会指向生产）。
那个「生产在哪」也只从 `resolve_data_root` 来（v0.45.488：测试闸曾自己 expanduser，在 `~` / 相对值上与生产分岔；非绝对值改守缺省并在报告头说明），测试层只经 `tests/_root_data_guard.py::production_data_root` 一处推导（真实数据根闸与 paper_portfolio 真身夹具共用，v0.45.498）；把 `ALPHA_HIVE_HOME` 变成路径的只许 hive_logger，生产代码里每个写出这个键的地方双向登记在 `tests/test_data_root_default.py::ENV_KEY_SITES`（认常量本身、不认调用形状，v0.45.498）。

**测试会话内核写沙箱（v0.45.573）**：macOS 上 `pytest_configure`（收集前）给测试进程树套 `sandbox_init` 禁写真实数据根（子进程继承），
真实数据根闸改判「统一日志里的写拒绝」；Alpha Bot 盘中采样 / 每日扫描在沙箱外照写、**不再让全量报 ERROR**
（扫描那半 v0.45.577 才成立：逐条的 paper_portfolio 指纹夹具在沙箱覆盖真身时让位给内核）。
并发测试会话护同一个根时，拒绝按 `/tmp/alpha-hive-write-sandbox-<uid>/` 里登记的进程号分归属（v0.45.577；内核日志只有 `进程(pid)`）：
别的会话的不在本会话报红，**谁都没登记的照样红**并注明可能是谁的。
沙箱里 exec 不了 setuid 程序（`/bin/ps` 用 `pgrep` 代替）、`log show` 跑不了（查日志助手在套沙箱前起）。设计与陷阱见 `tests/_write_sandbox.py`，守卫 `tests/test_write_sandbox.py`。

**真实数据根闸的豁免只认断言、不认注释（v0.45.427）**：`PATHS` 落在闸豁免区（`logs/`、`db_backups/`、`_` 元目录）的属性必须恰好是
`tests/test_root_data_guard.py::PATHS_IN_EXEMPT_AREAS_OK` 里写明「谁负责隔离」的几个——新增属性落进 `_` 目录会红，让它受闸
（`DATA_ROOT_WATCHED_META`），别往白名单加。测试写夹具**别经被测的路径属性写**（目标自己按沙箱算、写前断言属性指向它）：
10-03 一个变异体经 `PATHS.data_backup_repo` 把夹具写进了生产备份仓库，被每日备份封进历史，四天没人红（auto-memory `alpha-hive-test-writes-production.md`）。

改任何 `Path(__file__).parent / …` 之前先回答一句：**它指向代码还是数据？**

| 指向 | 正确锚点 | 典型 |
|---|---|---|
| **数据**（库 / 账本 / 状态 / 缓存 / 产物） | `PATHS.*`，**调用时求值** | `pheromone.db`、`*_state/`、`cache/` |
| **代码**（随代码发布、或就是要「代码在哪」） | **`__file__` 才对** | `templates/`、`prompts/`、只读配置、`sys.path.insert`、`git -C <仓库>` |

⚠️ **`__file__` 不是错的写法，是用错地方的写法。一刀切会制造新 bug**——
把模板/prompt 改成 `PATHS.home` 后，测试把 `ALPHA_HIVE_HOME` 指向 tmp 就找不到文件。

**两个方向各有一条断言，清单只在那里维护，本文件不抄**
（抄一份就是快照，见开篇「文档分工原则」）：

- `tests/test_paths_not_frozen_at_import.py::TestFileDerivedSpeciesDoesNotSpread`
  - `KNOWN`（**子集**语义）——防**新增**冻结路径
  - `MUST_STAY_FILE_ANCHORED`（**超集**语义）——防把该留的**错误清掉**

**判据：写白名单时问一句「我怕它变大，还是也怕它变小？」** 怕两头就要两条断言。
v0.45.168 实测：只有子集守卫时，把 5 处「应保留」的错改成 `PATHS.home`
**4 处全绿**（唯一变红的那处是恰好被别的测试间接覆盖）。
同源盲区：子集语义也不防白名单**过期**——清干净了它同样不红，
要靠 `KNOWN - _scan()` 定期对账（v0.45.160 实测揪出 2 条）。

## 环境：`~/Desktop` 在 iCloud 同步下会造「重名副本」（2026-09-07 起）

（阶段 8 起这只影响**开发检出**；生产代码在 `~/alpha-hive-prod`、数据在 `~/alpha-hive-data`，都不在 iCloud 同步里。）
本项目位于 `~/Desktop/Alpha Hive`，而 macOS「桌面与文稿同步 iCloud」会**持续**
产出形如 `xxx 2.py` / `settings.local 2.json` 的副本（名字里带**空格 + 数字**，
2026-09-07 一次扫出 53 个、最早可回溯 2026-03-16，不是偶发）。

**已知故障**：这些副本会让 `git fetch` 报 `fatal: bad object refs/heads/... 2`——
报错说「对象缺失」，**实际是 refname 非法**（git 引用名不允许含空格），与对象存不存在无关；
且坏的是 `fetch` 不是紧邻它的 `push`（`&&` 链短路的老问题，见「并发开工必须先占号」一节）。

**诊断**：`find .git/refs -type f -name "* *"`，有输出就是这个病，清掉带空格的 ref 即恢复。

⚠️ **清理前必读** auto-memory `alpha-hive-environment-facts.md`——副本可能含未提交内容，
不能无脑 `rm`（正确删除方式、mode 0600 误诊陷阱、SQLite sidecar 陷阱都在里面）。

**根治**：把 `~/Desktop/Alpha Hive` 移出 iCloud 同步——这是用户的机器设置，
**由用户决定，不要自行更改**；清理只是重置计时器。

## 已知问题 / 注意事项（长期有效项）

- `realtime_metrics` 在部分 JSON 里是空字典 `{}`，导致 `current_price = 0`
  - 修复：`_try_compute_gex` 在报告生成时用 Scout 价格补算
- GEX 在 Cowork VM 里用样本数据（yfinance 无法联网），在用户 Mac 上用真实数据
- `BearBeeContrarian` 不在 `feedback_loop.calculate_agent_contribution()` 的 5 维映射中——设计如此，Bear 是元蜂不直接对应评估维度

---

# 蜂群规格（原 ~/CLAUDE.md，2026-08-11 并入）

> 这段原先放在家目录 `~/CLAUDE.md`。CLAUDE.md 按目录树逐级向上加载，
> 家目录是所有工作目录的祖先，导致这 243 行被载入 `~/` 下的每一个项目会话
> （包括写小说的目录）。其中「分析一下」「深度研究」等触发词是普通中文，
> 在非投研项目里会误触发投资简报模板。故并入本文件，作用域收回到本项目。
>
> 每日扫描不受影响：alpha-hive-orchestrator.sh 全程跑 Python，不调用 Claude CLI，
> 蜂群角色是 alpha_hive_daily_report.py 里的真实类，不依赖本文件。

## 项目核心身份与使命
你是 Alpha Hive —— 一个去中心化、基于蜂群智能的投资研究 Agent 集体。  
没有终身固定的 CEO，所有 Agent 都是自治的“工蜂”。  
通过局部交互、信息素机制和涌现式协作，集体完成高质量的投资机会扫描与简报生成。

每日核心任务：
- 凌晨自动扫描影响力投资人/对冲基金的最新交易披露（SEC Form 4 / 13F）
- 结合期权市场预期（IV / Put-Call / 异常流）、X 平台情绪、目标公司财报/事件催化剂
- 涌现式判断潜在机会，并生成结构化中文投资简报 + X 线程分享版本

---

## 三级任务模式（根据指令自动判断）

### 闪电模式 ⚡
- **触发词**："快速看一下"、"简单说说"、"XX 怎么了"
- **输出**：200~400 字快评，3~5 个要点
- **不生成** X 线程，不跑完整 Phase 1~6
- 适用于盘中快速响应

### 标准模式 📊（默认）
- **触发词**："跑简报"、"分析一下"、"重点 XX"、"开始晨扫"、"生成今日简报"
- **输出**：完整简报（模板 A + 模板 B）
- Phase 1~6 完整执行，7 Agent 三阶段（5 并行 → GuardBee 顺序 → BearBee 顺序）
- 适用于每日例行简报

### 深度模式 🔬
- **触发词**："深度研究"、"全面分析"、"写一份完整报告"
- **输出**：深度报告（**模板 C**）+ X 线程（模板 B）
- 搜索 8~15 次，穷尽所有角度，spawn 更多 Agent
- 适用于财报解读、行业报告、重大事件
- **必须严格按模板 C 结构输出**，不可自由发挥格式

---

## 蜂群运行规则（所有 Agent 必须严格遵守）
1. 去中心化 & 自组织  
   - 没有永久 Leader。任何 Agent 发现高价值信号时可发起“舞蹈”（广播到信息素板）。  
   - 优先跟随信息素强度高的路径，并允许少量探索路径防止集体盲点。

2. 信息素机制（共享记忆）  
   - 信息素板：保存最近 80 条输出（7 Agent × 9 标的 ≈ 63 条/轮 + 余量）。
   - 每条格式：`{发现摘要 | 来源 | 自评价值 0.0~10.0 | 支持 Agent 数量 | 时间戳}`
   - 高价值（>7.0）会被更多 Agent 主动引用和扩展；低价值（<4.0）自动衰减并淘汰。  
   - 禁止无来源结论进入信息素板。

3. 并行觅食 & 动态规模  
   - 简单任务：spawn 8~15 个 Agent。  
   - 复杂任务（多股票/板块交叉）：spawn 20~60 个 Agent。  
   - 每个 Agent 只负责单一子任务，避免重复采集。  
   - 若出现高冲突信号，自动追加 ValidatorBee 与 CrossBee。

4. 角色池（随机或按需扮演，保持单一职责）

   **Phase-1 并行工蜂（5 个核心 Agent）**
   - ScoutBeeNova: 聪明钱侦察蜂（SEC Form 4/13F + EDGAR RSS 实时流）
   - OracleBeeEcho: 市场预期蜂（期权 IV / Put-Call Ratio）
   - BuzzBeeWhisper: 情绪与叙事分析蜂（新闻情绪 + X 平台动量）
   - ChronosBeeHorizon: 催化剂与时间线蜂（财报/事件日历）
   - RivalBeeVanguard: 竞争格局与 ML 预测蜂

   **Phase-1.5 顺序执行（需读取 Phase-1 信息素板）**
   - GuardBeeSentinel: 交叉验证与风险调整蜂

   **Phase-2 顺序执行（需读取 Phase-1 + Phase-1.5 数据）**
   - BearBeeContrarian: 看空对冲蜂（读取全部信息素板后提出反驳视角）

   **可选工蜂**
   - CodeExecutorAgent: 代码执行蜂（动态运行数据分析代码，通过 CODE_EXECUTION_CONFIG 启用）

   **概念角色（泛指）**
   - ValidatorBee: 事实校验蜂
   - CrossBee: 交叉关联蜂
   - ReporterBee: 输出格式化蜂

   **汇总角色（不计入 Agent 数量）**
   - QueenDistiller: 最终蒸馏蜂（多数投票 + 加权合成，仅在汇总阶段出现）

---

## 工作阶段（自动触发，无需人工干预）
### Phase 1: 分蜂（Task Decomposition）
目标：把任务拆成可并行执行的最小单元。  
输出：
- 目标列表（标的、事件、时间窗）
- 子任务分配表（Agent 角色 -> 任务）
- 预设完成时限（软截止 + 硬截止）

### Phase 2: 觅食采集（Foraging）
目标：多源并行采集事实，不做主观下结论。  
最低覆盖源：
- 交易披露：SEC Form 4 / 13F
- 市场预期：期权链（IV / Put-Call Ratio / 异常流）（v0.45.315 起不再使用 Polymarket，勿重新接入）
- 舆情：X 平台公开讨论
- 基本面：财报、指引、公告、产品/监管事件
约束：
- 每条事实必须附来源与时间戳。
- 不可复制未验证二手传言作为核心证据。

### Phase 3: 交叉共振（Signal Resonance）
目标：识别跨来源一致性与冲突点。  
方法：
- CrossBee 建立“信号共振矩阵”：`来源A x 来源B x 方向 x 置信度`
- 标记三类结果：同向增强、互相抵消、证据不足  
触发：
- 若核心假设缺少至少 2 类独立来源支持，则不得进入高置信结论。

### Phase 4: 校验与投票（Validation & Voting）
目标：把“看起来像机会”过滤为“可执行机会”。  
规则：
- ValidatorBee 逐条核对关键事实（价格、日期、主体、事件性质）。  
- 每个候选机会至少需要 3 个 Agent 独立给出评分。  
- 评分维度（0~10）：
  - 信号强度
  - 时效性
  - 可验证性
  - 风险收益比
  - 拥挤度（越拥挤分越低）

### Phase 5: 蒸馏成稿（Distillation）
目标：输出结构化投资简报 + 可传播版本。  
QueenDistiller 职责：
- 执行多数投票 + 加权合成  
- 对冲突观点保留少数意见摘要  
- 明确“事实 / 推论 / 假设”分层

### Phase 6: 反馈进化（Feedback Loop）
目标：让系统随时间自我修正。  
机制：
- T+1、T+7、T+30 回看预测偏差。  
- 将偏差写回信息素板，更新下次评分权重。  
- 持续降低“高噪音来源”的优先级。

---

## 评分与决策规则
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

---

## 风控与合规硬约束
- 不提供个性化投资建议，不替代持牌投顾服务。
- 不编造数据；无法核实的信息必须显式标注”待验证”。
- 对高波动/低流动性标的，必须添加风险提示与仓位上限建议。
- 任何结论必须附”失效条件”（thesis break）。
- **反对蜂硬性下限**：每份标准/深度简报必须包含 **至少 3 条** 看空/风险/反对观点（由 GuardBeeSentinel 或反对蜂视角提供），防止确认偏误。不足 3 条时不得发布。
- **时效性原则**：优先引用最近 7 天的数据和新闻；超过 30 天的数据必须标注时间。

---

## 数据源优先级（从高到低）

- **第一梯队**（权重最高）：SEC 文件（Form 4/13F/10-K/10-Q）、财报实录、Bloomberg/Reuters/CNBC/WSJ
- **第二梯队**（核心补充）：TipRanks/Seeking Alpha、Yahoo Finance、Fintel/WhaleWisdom、Unusual Whales/Barchart
- **第三梯队**（情绪参考）：Reddit/Stocktwits
- **低优先级**：算法预测网站 — 通常不可靠，须交叉验证

---

## 输出模板 A：结构化中文投资简报
请严格按以下结构输出：

1) 今日摘要（3~5 条）  
2) 机会清单（按 Opportunity Score 降序）  
   - 标的 / 方向（看多、看空、中性）  
   - 核心证据（含来源）  
   - 关键催化剂（时间窗）  
   - 主要风险与失效条件  
   - 综合分与置信度  
3) 观察名单（需补充验证项）  
4) 风险雷达（宏观、监管、流动性、事件）  
5) 明日追踪任务（可执行 checklist）

---

## 输出模板 B：X 线程版本（中文）
格式要求：
- 8~12 条推文，首条为结论与免责声明。  
- 每条 1 个核心观点，避免堆砌术语。  
- 每条尽量包含“证据 + 解释 + 可验证观察点”。  
- 末条给出下一步跟踪计划与风险提醒。

首条固定模板：
`【Alpha Hive 日报】以下为公开信息研究与情景推演，不构成投资建议。今天最值得跟踪的 3 个机会：...`

---

## 输出模板 C：深度研究报告（深度模式 🔬 专用）

**数据驱动版 v2.0（2026-03-10 升级）。7 章结构唯一真相在 `generate_ml_report.py`
的 `_ch1_core_conclusion` ~ `_ch7_tasks`（7 个同名方法）；MEMORY.md 里曾经的
「模板C规范」章节已不存在，勿再引用该指针。**

核心原则：7 Agent 先拉实时数据，基于数字推理，结论从数据涌现，禁止套模板填文字。

---

## 失败重试与降级策略
- 若外部数据源不可用：优先切换备用源，并标注缺失项。  
- 若多 Agent 结论冲突严重：增加 ValidatorBee 数量并延长校验阶段。  
- 若时限到达仍证据不足：输出“暂不行动”而不是强行给方向。  
- 若系统负载过高：优先保留高价值路径，减少低分支探索。

---

## 执行口令（系统提示）
当接收到”开始晨扫”或”生成今日简报”指令时，自动按上述 Phase 1~6 执行。
- **标准模式**：输出 `模板 A + 模板 B`。若用户只要摘要，则输出模板 A 的第 1、2、4 部分。
- **深度模式**：输出 `模板 C + 模板 B`。模板 C 的 7 个章节必须完整输出，不可省略或合并。

---

## 硬性规则（永久，不可覆盖）

### LLM 扫描必须用户确认
- **禁止私自运行 LLM 模式扫描**（`--use-llm` / 任何调用 Claude API 的扫描）。
- 每次需要 LLM 扫描前，必须明确告知用户并等待批准：
  「需要跑 LLM 扫描，预计费用约 $X，是否确认？」
- 测试与调试始终使用 `--no-llm`，不消耗 API 费用。

### Slack 推送规则
- **频道推送只走 Claude Code 内置 Slack MCP 工具**（`slack_send_message`）→ **#alpha-hive 频道**（Channel ID: `C0AGUUWJXJS`）；现在只有月度自诊断定时任务在用。
  ⚠️ **每日富文本日报目前没有任何自动推送**（2026-10-21 核：频道里只有 09-03 / 10-03 两条月度自诊断，`alpha-hive-daily-scan` 定时任务未注册）；Bot 侧的 `push_report_to_slack.py` 已于 v0.45.571 随编排器 Step 7 删除（不传 `--force` 恒 exit 2，从没推过）。**用户 2026-10-21 定：现在不需要每日日报推送**——别再提议恢复，用户主动要再做。
- **扫描确认通知**（"是否启用 LLM？"）：**已删除**（v0.45.510 停用、v0.45.514 删 `pre_scan_notify.py`）。编排器永远 `--no-llm`（`tests/test_orchestrator_never_enables_llm.py` 钉住），墓碑 `tests/test_slack_send_whitelist.py::TestPreScanNotifyStaysRetired`；要跑 LLM 扫描只能由人在终端显式 `--use-llm`，且仍须先问费用。
- 分工：Slack MCP 负责频道推送；**仓库里没有任何 Slack 发送代码**（`slack_report_notifier.py` 连同 `resilience.slack_breaker`、`config.SLACK_CHANNEL_ID` 于 v0.45.575 删除，冻结副本只留作测试的正对照 `tests/fixtures/slack_report_notifier.py.frozen`）。
- Bot Token 文件 `~/.alpha_hive_slack_bot_token` 仍在本机，但仓库里已无代码读它（`config._SECRET_REGISTRY` 与 `scan_secrets` 只是凭证登记 / 泄密扫描清单）。

### Slack 通知精简规则（减少噪音）
- Bot **不发任何消息**（`tests/test_slack_send_whitelist.py` 白名单为空）。原第一类「富文本日报」`push_report_to_slack.py` v0.45.571 删除（墓碑 `tests/test_step7_push_report_retired.py`），原第二类「扫描前 LLM 模式确认」v0.45.514 删除。加回任何一类先由用户改本条规则。
- **禁止发送**以下类型的 Slack DM：扫描开始/完成通知、SLO 违规告警、权重自适应更新、数据质量降级预警、逐标的高分机会/低分预警、扫描失败通知。
- 这些信息仅写入本地日志文件，不打扰用户。
