# Alpha Hive · 版本变更历史

> 格式：每次 Cowork session 结束后追加一条记录。
> 规范：`Added` 新增 | `Changed` 修改 | `Fixed` Bug 修复 | `Removed` 删除

---

## [0.32.2] — 2026-06-21 — 部署/渲染管线交易日过滤 + 清非交易日幽灵报告（option A 根治）

### Added — 部署/渲染全链交易日过滤（防未来幽灵 + fail-safe）
- 新增 `is_trading_day.filename_is_nontrading_day(name)`：从文件名提取 YYYY-MM-DD 判非交易日。**fail-safe**：提取不出日期 / fromisoformat 抛错 / is_trading_day 抛错 → 返回 False（保留文件），绝不误删合法交易日文件。
- 接入 5 处 `alpha-hive-daily-*.json` / ML glob：① `generate_ml_report._sync_ghpages` 部署 glob ② `report_deployer.deploy_static_to_ghpages` 部署循环 ③ `dashboard_renderer` 历史/趋势序列（line ~1046）④ `dashboard_renderer` Score-Delta 基准日 ⑤ `report_web_assets` RSS 历史条目。周末/假日幽灵不再进部署集合/趋势/差值/RSS。
- ML 链接本就 `.exists()` 门控 → 删文件后重渲染自动无死链。

### Removed — 存量非交易日幽灵报告（02-28/03-01/05-24）
- `git rm` 17 份跟踪 + `rm` 3 份未跟踪：02-28(周六) daily+thread；03-01(周日) 9 ML+daily+md+thread；05-24(周日) 3 ML+3 analysis。两侧相邻交易日（02-27/03-02）报告完整，零数据丢失；过滤+删除后部署集合实测 841 文件 / 70 日期 / 0 非交易日 / 核心齐全。（6/19 已于更早提交清理）

### Fixed — 测试 fixture
- `tests/test_pipeline.py::test_file_filter_excludes_old_ml_reports`：fixture 用 2026-03-01(周日) 做「旧报告应部署」，被新过滤器正确滤掉 → 改为 2026-02-27(周五，交易日)。生产逻辑正确，是 fixture 选错日期。

### 审计 & 已知残留
- 两轮对抗审计（9+9 agent）：本批确认问题全 **P3**（cosmetic/dormant/self-healing），唯一 P2 即上述测试 fixture（已修）。
- **pre-existing 残留（本批未动，宜单独处理）**：`equity_curve` 仍含非交易日点（来自 `predictions` DB 的 entry_date 漂移，约 110 行周日：03-01/04-26/05-03/05-10/05-17），非本次 daily-JSON 路径；过滤会改累计曲线连续性、需与 trading_stats 对账，属 DB 数据质量问题。
- 工作区 index.html/dashboard-data.json 仍引用已删文件 → 下次扫描(6/22)重渲染（`_fnt_hist`+`.exists()`）自动消除；线上 gh-pages 当前内部一致、无死链。
- `test_cleanup_deletes_old_records` / `test_valid_checkpoint` 在 HEAD 上即失败（日期相关 pre-existing flake），与本次无关。

---

## [0.32.1] — 2026-06-21 — 0.32.0 二次对抗审计修复（18 agent / 5 维度）

### Fixed — 审计确认的真实缺陷
- **`is_trading_day.py` 元旦特例 bug（P2，会误跳真实交易日，最危险方向）**：元旦落周六时 `_observed` 错误回滚到前一周五，但 NYSE 规则下 12/31 照常开盘（史实 2021-12-31 标普收 4766.18 正常交易）。改为单独处理 New Year：落周六不回滚、落周日顺延周一（1/2）、周一~五当天休市。验证 2021/2027/2032-12-31 恢复为交易日，2023-01-02 回归保护通过，2026/2027 全 10 假日无损。下次实盘命中 2027-12-31。
- **`generate_ml_report._check_disk_cache` 缓存失效回归（P2，本次 0.32.0 引入）**：缓存键改 `pdt_today()`（PDT）后，line 194 `file_date` 仍按本机上海时区渲染 → 晚间扫描窗口 `file_date != today` 恒成立、磁盘缓存永不命中、每进程重训 ML 模型。改为 `datetime.fromtimestamp(mtime, ZoneInfo("America/Los_Angeles"))`（fallback 裸渲染），与 today 同口径。纯性能修复，结果本就正确。
- **`generate_ml_report.py:387` 残留 `datetime.now()`（P3）**：`_prepare_ml_input` 的 `TrainingData.date`（死字段，不参与下游日期逻辑）改 `_pdt_now().isoformat()`。本文件除 `_pdt_now()` fallback 外已无裸 `datetime.now()`。

### Changed — 文档准确性
- [0.32.0] 措辞「6 处改用 PDT」更正为「5 处既有 datetime.now() 漂移点转 PDT（+ 护栏新增 1 处 pdt_today() 引用）」，与实际枚举对齐。

### 待办（审计发现，需用户确认后再动，本次未改）
- **部署 glob 无交易日过滤**：`generate_ml_report._sync_ghpages` / `report_deployer.deploy_static_to_ghpages` 用正则 glob 工作区所有日期的 ML HTML 部署 → 任意非交易日 ML 文件会被反复 push 到 gh-pages。存量幽灵残留：**2026-03-01（周日，9 份，被 index.html + dashboard-data.json 引用）** + **2026-05-24（周日，3 份，零引用）**。根治 = 两处部署 glob 加 `is_trading_day` 过滤 + 清存量（03-01 需同步清 index/dashboard 引用避免死链，05-24 可直接删）。
- **tzdata 缺失 fallback 风险**：`pdt_today()`/`_pdt_now()` 在无 zoneinfo/tzdata 时回退本机（上海）日期判交易日，假日前夜可能误跳有效交易日。当前两脚本只在有 tzdata 的用户 Mac 跑，不可触发；属健壮性加固项（可考虑 requirements 加 tzdata 或 fallback 改 UTC 换算）。

---

## [0.32.0] — 2026-06-21 — 美股交易日护栏（周末/假日跳过）+ ML 报告日期 PDT 化（根治 +1 漂移）

### Added — 交易日护栏接入 ML / 日报管线
- `generate_ml_report.main()` 与 `alpha_hive_daily_report.main()` 在 parse_args 后接入 `is_trading_day`：以 **PDT 日期**判断，周末 / 美股假日（Juneteenth、Good Friday、感恩节…）直接跳过、不生成当日报告。
- 均新增 `--force` 旗标可强制生成；**fail-open**（交易日检查异常时继续生成，绝不误跳过有效交易日）。
- 日报护栏放行 `--samples-only`（周日 cron 样本积累，不产 dated 报告 / 不部署）与 `--check-earnings`，避免误伤 `alpha-hive-sample-accumulator`。
- 原孤儿模块 `is_trading_day.py`（10 个 NYSE 假日 + Easter/observed 规则）首次接入主管线。
- 验证：美西周六实跑两脚本均干净跳过（退出码 0，不进扫描、零文件生成）；交易日历 6/18 交易 / 6/19 Juneteenth / 6/20-21 周末 / 6/22 交易 逐日正确。

### Fixed — `generate_ml_report.py` 全程 `datetime.now()` 致日期 +1 漂移（幽灵报告根因）
- 用户在中国、Mac 时钟比美西快 ~15h，`datetime.now()` 把交易日整体 +1：周四收盘后跑 → 本机已周五 → 报告错标次日、撞上 6/19 Juneteenth 休市 → 生成 10 份空数据幽灵 ML 报告。
- 5 处既有 `datetime.now()` 漂移点转 PDT：`self.timestamp`→`_pdt_now()`（aware datetime, America/Los_Angeles）；ML 模型缓存键 / `.swarm_results_{date}` 查找 / checkpoint 匹配 / 部署 commit 日期 → `pdt_today()`（另护栏新增 1 处 `pdt_today()` 引用）。
- 连带修复：`.swarm_results_{date}.json` 原按本机 +1 日期查找 → 找不到当日蜂群数据 → 幽灵报告才全是空的；改 PDT 后正确命中（与 [0.31.1] 的 `swarm_source` 歧义同根）。

### Removed — 6/19 幽灵 ML 报告（存量清理）
- `git rm` main + gh-pages 各 10 份 `*-ml-enhanced-2026-06-19.html`（Juneteenth 休市无交易）+ 10 份本地 `analysis-*-2026-06-19.json`。线上实测 6/19→404、6/18→200；index.html / dashboard-data.json 零引用。commit `602dc7d` / `051d54f`。

---

## [0.31.1] — 2026-06-21 — 修复 collect_data 读空 swarm（stale snapshot 事故）

### Fixed — `collect_data.py` 蜂群分恒为 0.0（根因：读错数据源）
- **事故**：`nvda-data-extract` 调度提炼出的 `NVDA_raw.json` 蜂群 `final_score` 全为 `0.0 / neutral`，期权字段全 null，导致误判"数据停在 6/16 / 6/19 是空快照"。实际 6/17、6/18 数据完整存在。
- **根因**：当前管线把蜂群评分写入独立的 `.swarm_results_{date}.json`，而 `analysis-{ticker}-ml-{date}.json` 内 `swarm_results` **恒为空字典**（6/16、6/19 实测均空）。`collect_data.extract_raw` 仍按旧格式读 `data['swarm_results']` → 全 0。属长期静默 bug，非单次事故。
- **修复**：新增 `find_swarm_results(ticker, report_date)` — 选取日期 ≤ report_date 且含该 ticker 的最新 `.swarm_results_*.json`（无则退回含该 ticker 的最新一份）；`main()` 在 `swarm_results` 为空时自动 graft 该 ticker 记录，并在 `_meta.swarm_source` 标注来源文件。`.swarm_results` 的 per-ticker 结构与 `extract_raw` 期望完全兼容（顶层 final_score/direction/resonance/agent_breakdown + agent_details.*.details）。
- **附带修复**：`main()` 打印 `OI: {total_oi:,}` 在 `total_oi=None` 时 `TypeError` 崩溃 → 改 `isinstance` 守卫，None 显示 `—`。
- **验证**：重跑 `collect_data.py NVDA` → `NVDA_raw.json` 补全自 `.swarm_results_2026-06-18.json`，score `5.25 / bullish`，OI 85,200，P/C 0.54，IV rank 53.45，4 笔异常流。
- **未解**：收盘价回填仍需联网的用户 Mac 运行；Cowork VM 屏蔽 Yahoo Finance（403）。`analysis` 文件名比 `.swarm_results` 前移一天（6/19 标签对应 6/18 交易日），属管线既有命名习惯，本次以 swarm_source 显式标注规避歧义。

---

## [0.31.0] — 2026-06-18 — Bot 付费分层（Free / Pro）+ 私下支付宝手动收款

### Changed — Pro 简报推送改分多条（完整内容，`push_job.py` + `bot.py`）
- **背景**：完整简报 26KB 远超单条 Telegram 4096 上限，旧 Pro 版单条截到 ~3000 字符（约 8 只标的处断）
- `format_pro_messages(md, date, max_messages=3)` + `_paginate_lines()`：按行边界（绝不切断单行）贪心分块，Pro 最多 **3 条**（实测 26KB→3024/2912/3239 字符，覆盖摘要 + 全 10 只聪明钱 + 市场隐含预期），首条主标题/续条「续 k/n」/末条免责声明 + dashboard；超 3 条则末条标注「后续章节见 dashboard」
- 免费版**保持单条摘要**（`format_for_telegram(tier='free')` 不变）
- `push_to_all` 改 `paid_text:str` → `paid_texts:list`：抽 `_send_one()` 含 RetryAfter 重试；逐订阅者发多条，Forbidden 中途屏蔽即停发该用户后续分条 + 退订；返回新增 `parts_sent`（总消息条数）；`sent`=收到≥1条的订阅者数
- `cmd_preview` 同步：Pro 多条逐发（标注「共 N 条」），免费单条
- 测试：26KB→3 条均 ≤4096 且 `<b>` 平衡、内容无损（前 3 块拼接=body 前缀，10/10 标的覆盖）、短/空简报降级 1 条、分层投递 + Forbidden 中断 + parts_sent 全过

### Added — `alpha_hive_bot/` 会员分层（月 ¥128 / 年 ¥998，私下支付宝，管理员手动开通）

**数据层**（`subscriber_db.py`）：
- `_migrate()` — `subscribers` 表 `ALTER ADD` 三列 `tier`(default 'free') / `tier_expires_at` / `trial_used`，`PRAGMA table_info` 检测幂等，对现有订阅数据零影响（`CREATE TABLE IF NOT EXISTS` 不会给已存在表加列，故用 ALTER）
- tier 方法：`get_tier`（paid 过期按 UTC 字符串字典序比较自动→free）/ `get_tier_info` / `set_tier` / `has_used_trial` / `mark_trial_used`
- `list_active_subscribers()` — 含 user_id 的 active 订阅者，供分层推送按 tier 取文案

**命令层**（`query_commands.py`）：
- 网关助手：`_effective_tier`（管理员恒为 paid）/ `_require_paid`（Pro-only 守卫）/ `_watch_cap` / `_alert_cap`
- 命令分层：`/scan`（免费=综合分+方向；Pro=+5维雷达+7蜂投票+ML链接）、`/top`（免费=Top3；Pro=全榜+共振+方向分布）
- **新 Pro-only 命令**：`/swarm`（七蜂分歧）、`/trend <代码>`（综合分历史走势 sparkline）、`/movers`（较上一交易日分数变动榜+方向翻转）
- 额度上限按 tier：关注 免费 3 / Pro 30；告警 免费 1 / Pro 20（`cmd_watch` / `cmd_alert` 命中免费上限提示升级）
- **付费命令**：`/upgrade`（展示价格+支付宝引导+回显 user_id，并 DM 通知管理员）、`/mytier`（查当前等级+到期）、管理员 `/grant <user_id> <月数>`（收款后手动开通，月数 1~60，目标不在库则自动加白名单）
- **行为锚定试用**：免费用户的告警在 `evaluate_alerts` 边沿命中 → 自动解锁 7 天 Pro 体验（每人一次）。仅限"从未有过任何 Pro 窗口"的纯免费用户（`effective!='paid'` ∧ `trial_used=0` ∧ `expires is None`），杜绝流失付费者/已用试用者重复领取，管理员不触发

**推送层**（`push_job.py`）：
- `format_for_telegram(md, date, tier)` — 免费层短预算（900 字符）+ 升级 CTA；Pro 完整版（3000）
- `push_to_all` 支持分层投递（`free_text` / `paid_text` / `cfg`），按每个订阅者有效 tier 选文案（管理员→paid，过期 paid→free），保留单文案模式向后兼容
- `run_daily_push` 同时构建免费/Pro 两版

**文案**（`config.py`）：`HELP` 重写，按 🆓/💎Pro 标注各命令权限 + Pro 会员说明 + `/grant` 管理员命令；保留"研究数据访问、不构成投资建议"合规口径

### Fixed — 两轮对抗审计共修复 6 项

**首轮（单 agent 对抗评审）3 项：**
- **P0 试用泄漏**：原 trial 守卫仅查 `get_tier != 'paid'`，导致流失付费用户（real `/grant` 后过期）告警命中时仍能白嫖 7 天试用 → 改为 `effective != 'paid' ∧ not trial_used ∧ expires is None` 三重守卫（仅纯免费用户）
- **P1 额度未在评估期生效**：`_watch_cap`/`_alert_cap` 仅在 add 时拦截，Pro 过期后旧的 20 条告警仍永久触发 → `evaluate_alerts` 新增按当前有效 tier 的逐用户额度（最早创建优先 `sorted(id)[:cap]`），降级后只评估免费额度内规则
- **P1 `cmd_top` 越界**：`dir_counts` 短数组（1~2 元素）→ `dc[2]` IndexError 致 Pro 用户 `/top` 崩溃 → 改 `(list(...)+[0,0,0])[:3]` 补齐

**二轮（13 agent / 6 维并行评审 + 逐条对抗验证）3 项 P2：**
- **`search_index` 坏元素崩溃**：`/scan //top //swarm //mywatch` 用 `{x.get("ticker"):x for x ...}` 无 `isinstance` 守卫（同文件 scores/fg_history 等字段均有）→ 远程 gh-pages JSON 含非 dict 元素时 4 命令静默失败 → 抽 `_index_by_ticker(data)` helper 加 `if isinstance(x, dict)`，4 处统一
- **推送无转义后长度钳制**：`MAX_MESSAGE_CHARS=3800` 死代码从未生效；`format_for_telegram` 仅在 escape **前**按 3000 截断，`html.escape` 膨胀（`&`→`&amp;`）后极端高特殊字符简报可超 Telegram 4096 → BadRequest 整条丢弃 → 新增 `_clamp_html()` 转义后二次钳制（保实体/标签边界 + 补齐未闭合 `<b>`），实测全 `&`/全 `<`/混合简报转义后均 ≤4096（正常简报实测最长 3367，不触发）
- **告警推送失败错失一次性试用**：边沿 `set_alert_state` 在 try **外**，`TelegramError` 时仍写 `last_state=1` 消费边沿 → 纯免费用户错失 7 天试用 → 重构为推送成功后才在 try **内** 提交 `last_state=1`；`true→false` 复位走 `elif` 总是写库

### 测试
- DB tier free/paid/过期/trial + 迁移幂等（重复 init 不崩）
- 38 项行为测试全过：网关分层、/scan·/top 免费 vs Pro 输出差异、/swarm·/trend·/movers Pro-only、额度上限、行为试用、/upgrade·/mytier·/grant（含非管理员忽略 + 参数校验）
- 分层推送：免费版含 CTA 且 <4096、Pro 完整版、过期自动降级、单文案向后兼容
- 首轮审计修复回归：trial 仅给纯免费用户（流失付费/流失试用/管理员均不触发）、eval-time 额度（Pro 期 checked=3 → 过期 checked=1）、短 dir_counts 不崩
- 二轮审计修复回归：4 命令对坏 search_index（list/str/None 元素）不崩有回复；全 `&`/全 `<`/混合/真实简报转义后 free+paid 均 ≤4096 且 `<b>` 平衡；告警推送失败保持 `last_state=0` 下轮重试且不错失试用、成功后才授予、`true→false` 复位总写库
- 集成：`build_application()` 注册 24 命令无冲突；HELP HTML 标签平衡

### Fixed — 定时推送从不触发（`bot.py` `_scheduler_loop`）
- **现象**：6/16、6/17 收盘后未给任何订阅者推送（gh-pages 上 6-16/6-17 简报均存在 HTTP 200，排除缺数据）
- **根因（两重）**：① 推送窗口设在 PDT 13:30（收盘后 30 分），但扫描在 **PDT 21:03**（收盘后 8h）才生成当日简报 → 13:30 fetch `daily-{today}.md` 恒 404；② skip 后仍把 `last_pushed_date` 标成今天并睡到次日 → 当日简报生成后也不再重试 → **定时推送实际从未成功过**（此前唯一送达的是手动 `/push_now` fallback）
- **修复**：重写 `_scheduler_loop` 为「轮询直到就绪」——抽纯函数 `_scheduler_decision()`（窗口前 sleep / 已推 sleep 到次日 / 否则 push）；**仅在真正推送成功后才标记 `last_pushed_date`**；简报未就绪则 30 分钟后重试，跨午夜 `today` 翻页自然停止当日重试（无简报的周末/假日不会误推）。8 场景单测全过
- **可选优化**：Railway 设 `PUSH_HOUR_PDT=20`（默认 13）可把轮询起点挪到接近扫描时间，减少无效轮询

### Added — 管理员 `/preview`（仅给自己发简报预览）
- 新增管理员命令 `/preview`：拉最近一份简报，**只发给调用者本人**（免费层+Pro 两个版本对照），不推给其他订阅者；先自查内容再决定是否 `/push_now` 广播
- `bot.py` 注册 + `config.HELP` 管理员段补充；10 项单测全过（仅发自己 chat / 非管理员忽略 / 无简报友好提示）

### Added — `alpha_hive_bot/BOTFATHER_COMMANDS.md`（命令菜单清单 + 坑记录）
- 新增可直接粘贴给 `@BotFather /setcommands` 的完整命令清单（19 条用户/查询/付费命令，排除 5 个管理员命令 `/invite /revoke /list /push_now /grant`）
- **⚠️ 记录关键坑：`/setcommands` 整表覆盖（非追加）** —— 每次加新命令必须重贴整段，否则现有命令从菜单消失
- BotFather 仅设全局菜单；管理员命令不 advertise；`/trend //movers` 保留作免费→Pro 转化入口

### 部署
- 待 push + Railway Redeploy 生效（`_migrate` 首次连接自动 ALTER 加列）
- ⚠️ 收款流程：用户 `/upgrade` → 私下支付宝付款 → 把 user_id 发管理员 → 管理员 `/grant <user_id> <月数>`
- （可选）`@BotFather /setcommands` 同步命令菜单，清单见 `BOTFATHER_COMMANDS.md`

---

## [0.30.0] — 2026-06-17 — Bot v0.3：个人关注列表 + 阈值告警

### Added — `alpha_hive_bot/`（6 新命令，限 active 订阅者）

**个人关注列表**（SQLite `watchlist` 表，上限 30）：
- `/watch <代码>` / `/unwatch <代码>` / `/mywatch`（带当日分数 + 方向徽章，未在当日扫描标注）

**阈值告警**（SQLite `alert_rules` 表，上限 20，边沿触发）：
- `/alert <代码> score>7` — 支持 `> < >= <=`，score 0~10；`_parse_alert_spec` 解析 `NVDA score>7`/`NVDA >7`/`nvda<4` 等多格式
- `/alerts`（查看规则含编号）/ `/unalert <编号>`
- **边沿触发**（`last_state`）：false→true 才推，持续满足不重推，true→false 复位后再满足可再推 —— 杜绝每日 spam

### 集成
- `subscriber_db.py`：加 `watchlist` / `alert_rules` 表（`CREATE TABLE IF NOT EXISTS` 免迁移，对现有 DB 安全）+ 8 方法
- `bot.py` scheduler：定时推送后调 `evaluate_alerts`（**仅定时跑，不在 `/push_now`**，避免手动重复触发）
- `evaluate_alerts(bot, cfg, db)`：读 gh-pages `dashboard-data.json` 的 scores，逐规则边沿评估推送
- `config.HELP`：加「📌 关注列表」「🔔 阈值告警」两组

### 测试
- DB 方法全过（watchlist add/dup/get/remove；alert add/dup/list/state/remove；list_active_alerts 只含 active）
- `/alert` 解析 8 例（含 >10 越界 / 无效 / 缺参 → None）
- **边沿触发实测**：分数序列 5.5→7.2→7.5→6.0→7.1 推送 [0,1,0,0,1] 精确（仅两次跨越推送）
- HELP HTML 标签配对 905b <4096；register 注册 11 个 handler；bot 模块导入无循环

### 部署
- 代码已 push；Railway Dockerfile 自动包含;需 Redeploy 生效（subscriber_db 新表首次连接自动建）

---

## [0.29.4] — 2026-06-17 — 盘中 forming-bar 护栏（确保取已收盘日线价）

### Fixed — `data_pipeline.py` `YFinanceSource.fetch()`

- **用户报**：6-16 NVDA 应显示收盘价 207.41，dashboard 却显示 206.72/207.34
- **根因（时区错位 → 盘中抓价）**：
  - 用户在温哥华(PDT)，但 Mac 系统时区误设为 Asia/Shanghai(UTC+8，偏移 15h) → 机器时钟整体快 15 小时
  - 系统按 PDT 锁 date_str=6-16（正确），但**实际运行时刻是美股 6-17 盘中**（美东 13:xx，市场开着）
  - `t.history(period="1mo")["Close"].iloc[-1]` 在盘中返回的是"当日正在形成"的 6-17 盘中 bar（实时变动 206.72→207.34），而非 6-16 已收盘的 207.41
- **修复**：
  - `_exchange_now()` — 用 SPY 分钟数据末时间戳判断交易所真实时间（美东 tz，来自 Yahoo 服务器，**不依赖本机错钟**），整进程缓存只探一次
  - `_drop_forming_bar()` — 末根日线日期 == 交易所真实当日 且 当前 < 15:59 收盘 → 判为盘中 forming → 丢弃；下游 price/momentum/volume 全用已收盘日线
  - 整段 try/except 全包，探测失败/异常一律退回原 `iloc[-1]`，**零回归风险**
- **验证**：5 场景单测全过（盘中丢弃 / 收盘后保留 / 历史保留 / 探测失败原样 / len<3 不动）；真实 NVDA 盘中 fetch 精确得 6-16 收盘 207.41
- **Ops**：清 6-16 脏数据 + 重跑（护栏生效）→ 部署 gh-pages，线上 dashboard + bot `/scan` 全部修正为 NVDA 207.41 等精确收盘价

### Note

- 这是代码层兜底（盘中跑也取收盘价）。**根治仍需用户把 Mac 时区从 Asia/Shanghai 改为 America/Vancouver**，让定时扫描在美股盘后正确时间运行。

### 二次检查补全（同日）

二次审计发现 v0.29.4 初版只覆盖 `data_pipeline` 一处，其余直连 yfinance 取价点盘中仍抓盘中价（实证：TSLA 快照 entry 403.4 ≠ dashboard 404.66）。补全 5 处（全部复用 `_drop_forming_bar`，inline import + try/except 包裹零回归）：
- `alpha_hive_daily_report.py:938` 快照 entry_price（`period 1d→5d`，feeds 回测 + v0.29.3 基准）
- `alpha_hive_daily_report.py:1673` ML 报告 real_price
- `alpha_hive_daily_report.py:1795` scout 价回退（直接 feeds dashboard）
- `data_fetcher.py:216` fallback price（`period 2d→5d`）
- `crowding_detector.py:480` crowding price
- 审计验证：6 类单测（缓存语义/tz 一致/下游裁剪一致/边界 15:59/异常安全/多标的回归）全过；3 文件无循环导入

---

## [0.29.1] — 2026-06-16 — yfinance 限流崩溃修复

### Fixed — `generate_ml_report.py`

- `main()` 取价 `except` 子句原先只捕获 `ConnectionError/TimeoutError/OSError/ValueError/KeyError/IndexError`，**漏掉 `YFRateLimitError`**，导致 Yahoo 限流时整份 ML 报告直接 traceback 崩溃（线索：line 2006 `_t.history(period="5d")`）。
- 改为 `except Exception`，并新增磁盘降级：yfinance 取价失败时读 `{ticker}_raw.json` 的 `_meta.price`（及 `fundamentals.momentum_5d`）复用最近一次真实价格，替代原先写死的 `100.0` dummy 价。

### Fixed — `resilience.py`（根因级修复）

- `NETWORK_ERRORS` 元组原先不含 `yfinance.exceptions.YFRateLimitError`，导致 Yahoo 429 限流穿透所有 `except (*NETWORK_ERRORS, ...)` 子句（`options_analyzer` 拉期权链 line 144 `stock.options` 即崩）。
- 动态追加 `YFRateLimitError`（`try import` 包裹，yfinance 缺失/旧版本安全降级），一次性覆盖 `options_analyzer` / `bear_bee` / `cache` / `cboe_fetcher` 等所有引用 `NETWORK_ERRORS` 的入口。限流时统一降级为样本/缓存期权数据而非崩溃。

### Changed — `outcomes_fetcher.py`（自学习回填限流熔断）

- `process()` 回填循环原先对每个历史快照逐个硬刚 yfinance，限流时刷屏 50+ 条 WARNING（`NVDA_2026-04-07 … 处理失败: Too Many Requests`）且无意义。
- 新增连续限流熔断：检测到 `YFRateLimitError` / "Rate limited" / "Too Many Requests" 连续 3 次即 `break` 中止本次回填，剩余快照下次运行再补；成功一个则重置连击计数。回填为自学习可选步骤，中止不影响当日报告生成。

### Changed — `outcomes_fetcher.py`（回填改本地快照优先，基本不再联网）

- 新增 `_load_price_index()` / `_lookup_local_price()`：用 `report_snapshots/{ticker}_*.json` 的 `entry_price` 拼出按日期可查的本地收盘价序列，`_fetch_price()` 改为**本地优先、yfinance 仅兜底**。回填 T+1/T+7/T+30 不再逐日打 yfinance。
- 空洞修复：`entry_price=0.0` 的坏数据日（如 `NVDA_2026-03-25`）用其他快照已回填的 `actual_prices`（如 `NVDA_2026-03-24` 的 t1=03-25 收盘）反推目标交易日补齐。验证：03-16/03-17 的 T+1/T+7/T+30 本地取值与原 yfinance 记录逐一吻合（如 03-25=178.68）。
- 仅当目标日超出最新快照覆盖（未来尚未发生）时返回 None，留待后续快照生成后再补——此场景 yfinance 同样无数据。
- 补 `import json`（模块此前未导入，新方法读快照需要）。

## [0.29.3] — 2026-06-16 — Dashboard 价格污染修复（scout 价缺失时优先当天 Agent 价）

### Fixed — `dashboard_renderer.py` `render_dashboard_html()` 价格补注

- **用户报**：6-15 dashboard NVDA 显示 $145 实为 $212；排查发现 **9 标的中 6 只价格错误**（TSLA $189→$411、CRCL $114→$83 等）
- **根因（两 bug 叠加）**：
  1. 6-15 扫描期间 Yahoo 401 Invalid Crumb → 全标的 `ScoutBeeNova.price=None`
  2. 旧降级链 scout 价 None → 读"最新 ML 文件"，但 6-15 无当日 ML 文件 → 回退到 3 周前 `analysis-*-2026-05-29.json` 的陈旧价（NVDA 还带着 5-24 的 `dealer_gex.stock_price=145.32` 污染值）
  - 真实价 $212.45 明明在当天 swarm_results 的 Chronos/RivalBee/CodeExecutor `current_price` 里，dashboard 却没用
- **修复**：
  - ① scout 价缺失时**优先用当天 swarm_results 可靠 Agent 价**（`analyst_targets`/`eps_revision`/CodeExecutor 的 `current_price`），刻意排除 `OracleBee._snapshot_stock_price`（期权快照，污染源）
  - ② ML 文件回退加 **7 天新鲜度护栏**，超期旧文件不当当日价
- **验证**：修复后 9 标的价格与 `report_snapshots/{ticker}_2026-06-15.json` 权威 `entry_price` **完全一致**
- **Ops**：重生 6-15 dashboard + 部署 gh-pages，线上 NVDA $145→$212 / TSLA $189→$411 等 6 只已纠正（bot `/scan` 同步修正，因同读 dashboard-data.json）

---

## [0.29.2] — 2026-06-16 — Bot v0.2 查询命令（/scan /top /swarm /scorecard /fg）

### Added — `alpha_hive_bot/query_commands.py`（新模块）

5 个查询命令，全部只读 gh-pages `dashboard-data.json`（零实时扫描，仅 httpx+stdlib），限 active 订阅者：
- `/scan <代码>` — 单标的：综合分 + 5 维雷达 + 蜂群投票 + ML 报告链接
- `/top [N]` — 当日机会榜：分数降序 + 方向徽章 + ⚡共振标记 + 方向分布
- `/swarm <代码>` — 7 蜂逐票 + 共识度 + 共振 + 分歧 std/spread
- `/scorecard` — 方向准确率 + 近 8 周（诚实含 W21 5%/W22 30% 翻车周）+ $50K 模拟组合（含 vs SPY -5.1%）
- `/fg` — 恐惧贪婪指数 + 14 日 sparkline

### 工程要点
- `_gate()` 限 active 订阅者；HTML parse mode + `html.escape`；统一 DISCLAIMER 尾
- `_fmt_num()` 防 NaN/inf/None（`trading_stats.realistic.spy_*=NaN` 不泄漏，用 top-level `alpha_vs_spy`）
- `fetch_dashboard()` 失败优雅降级；ticker 归一化（去 `$`/大写）；无效代码列出当日可用标的
- `bot.py` `register(app)` 注册；`config.HELP` 列出新命令；合规措辞"研究输出，非买卖建议"

### 二次检查
- 10 命令测试 + 边界（无效代码/缺参/小写$/非订阅者拦截/NaN/inf/fetch 失败）全通过
- 真实 6-15 数据渲染均 HTML 合法、<4096 字符；**无 P0/P1 bug**

### 部署
- 代码已 push，Railway Dockerfile 自动包含新模块；需 Redeploy 生效

---

## [0.29.0] — 2026-06-16 — Alpha Hive Bot（对外 Telegram 订阅机器人）上线 + Railway 部署

### Added — `alpha_hive_bot/`（新组件，invite-only MVP，无支付）

- `bot.py` — Telegram 命令路由 + asyncio 定时器
  - 用户：`/start` `/status` `/unsubscribe` `/help`
  - 管理员：`/invite <id>` `/revoke <id>` `/list` `/push_now`
  - 每日 PDT `PUSH_HOUR_PDT`:30 自动推送（默认 13:30，约北京 04:30）
- `subscriber_db.py` — SQLite 状态机：whitelisted → active → unsubscribed/revoked
- `push_job.py` — fetch gh-pages `/alpha-hive-daily-{date}.md` → HTML 格式化 → 遍历 active 推送
- `config.py` — 环境变量解析 + 合规免责声明文案（HTML）
- `Dockerfile` / `requirements.txt` / `.env.example` / `README.md` — Railway 部署就绪

### Fixed — 部署期 4 个根因（Railway 实战逐个排查）

1. **nixpacks 漏 COPY `alpha_hive_bot/`**（Console 确认 `/app` 缺该目录，`.dockerignore`/`.gitignore` 均未排除，文件在 origin/main）→ 改用**专用 Dockerfile** 显式 `COPY alpha_hive_bot/`，根治
2. **legacy Markdown 解析崩溃**（`user_id` 单下划线被当斜体 → `BadRequest: Can't parse entities` → handler 抛错 → bot 不回复）→ 全部改 **HTML parse mode**，动态内容 `html.escape`，报告先 escape 整个 body 再安全美化
3. **slim 镜像缺 tzdata**（`ZoneInfo("America/Los_Angeles")` 抛错回退容器本地时间 → `pdt_today()` 算错日期 → 拉错日期简报 `skipped`）→ Dockerfile 装 `tzdata`
4. **日期边界健壮性** → `fetch_latest_md()` 当日缺失时回退最近一份可用简报（≤7 天）；`/push_now` 用 `fallback=True`，定时任务保持 `fallback=False`（不重复推旧报）

### Ops

- Railway 项目 `hospitable-flow`，service `worker`，US West，Volume `/data`（`DB_PATH=/data/subscribers.db`）
- Builder 自动检测 Dockerfile；Variables：`BOT_TOKEN` / `ADMIN_USER_IDS=8624907971` / `PUSH_HOUR_PDT=13` / `DB_PATH`
- Bot：`@AlphaHiveDailyBot`；端到端验收通过（`/start`→`/invite`→`/start`→`/push_now sent=1 date=2026-06-15`，收到 HTML 简报）
- **⚠️ 安全待办**：`BOT_TOKEN` 曾在对话明文出现，需 `@BotFather /revoke` 换新 token 并更新 Railway Variable

---

## [0.28.0] — 2026-06-09 — 全项目 PDT 日期统一审计 + 6 P0 + 4 P1 修复

### Added

- **`hive_logger.py`** — 新增全局 `pdt_today()` helper（模块末尾）
  - 返回美股交易日 PDT 字符串（`America/Los_Angeles` 时区）
  - 使用 `zoneinfo`，tzdata 缺失时回退本地
  - 抽统一 helper 避免每个模块重复定义（v0.27.3/0.27.4 历史）

### Fixed (P0: 写入存储 / 影响逻辑)

- **`options_analyzer.py:1430`** `_snap_date` — options_snapshot 文件命名（已实证：6-9 扫描产出 `_2026-06-10.json` 错位）
- **`vector_memory.py:118`** `"date"` 字段 — 向量内存 date 跨午夜偏移
- **`swarm_agents/base.py:83`** — `retriever.get_context_summary(date)` 召回日期匹配
- **`swarm_agents/rival_bee.py:36`** `date=` — TrainingData date 字段
- **`paper_portfolio.py:983`** `as_of` — CLI 默认 `--date` 美股交易日
- **`tradier_fetcher.py:488`** `validation_date` — JSON 字段时效性标识

### Fixed (P1: 查询参数 / 比较边界)

- **`newsapi_client.py:65`** AV 配额计数 `today` key（加注释说明 AV 实际 reset 时区不确定）
- **`edgar_rss.py:212+218`** `today` Form 4 过滤（加注释说明 SEC 实际 ET 时区差 3h）
- **`push_report_to_slack.py:44`** `--date` CLI 默认值
- **`backtest_engine.py:112`** `target_date > today` 比较边界

### Note (未修，设计上保留本地时间)

以下 P2 用途为"何时跑/生成"语义，本地时间合理：
- `code_executor.py:96` 执行日志 timestamp
- `self_analyst.py:223` brief 生成时间戳
- `vectorbt_bridge.py:492` HTML report generated 字段
- backtester.py 其他 11 处 cutoff 计算（覆盖范围宽 1 天，不致错）

### History

- v0.27.3：`alpha_hive_daily_report.date_str` + `backtester._pdt_today`
- v0.27.4：`pheromone_board._pdt_today` + `generate_ml_report` None safety
- v0.28.0：统一抽到 `hive_logger.pdt_today` + 全项目 P0/P1 共 10 处修复
- v0.27.3/0.27.4 的本地 helper 保留（功能等价，避免破坏现有 commit；下一次可统一迁移）

---

## [0.27.4] — 2026-06-09 — ML 报告 None safety + agent_memory.date 锁 PDT（跨午夜 2 个回归 bug）

### Fixed

- **`generate_ml_report.py:275`** — ML 报告 P0：链式 `dict.get(...)` None safety
  - 旧：`advanced_analysis.get("dealer_gex", {}).get("stock_price")` 在 `dealer_gex=None` 时崩
  - 新：`(advanced_analysis.get("dealer_gex") or {}).get("stock_price")` + 同款修 `realtime_metrics.sources.yahoo_finance`
  - 触发：6-9 扫描 **10/10 ML 报告全部失败**（log `'NoneType' object has no attribute 'get'`），dashboard "ML 详情"链接全 404
  - 与 v0.27.1 `_ch3_oracle` 同类 bug，漏修了 `generate_ml_enhanced_report`

- **`pheromone_board.py:203`** — agent_memory.date 跨午夜偏移
  - 旧：`'date': datetime.now().strftime("%Y-%m-%d")` 用本地 CST，跨午夜写成次日
  - 新：模块级 `_pdt_today()` helper（与 backtester.py 同模式），写 PDT
  - 触发：6-9 扫描时本地 CST `2026-06-10 00:50`，100 行 agent_memory 错写 6-10
  - v0.27.3 漏修项：当时只修 `reporter.date_str` 和 `backtester.save_predictions`，pheromone_board caller 未覆盖

### Ops（6-9 数据归位）

- 备份 `pheromone.db.bak_before_69fix_*`
- SQL UPDATE：`agent_memory` 100 行 6-10 → 6-9（DB 修复）
- 补生成 10 个 ML 报告 HTML（`generate_ml_enhanced_report` + `generate_html_report` 直接调用，无需重跑全扫描）
- 重生 `index.html`（让"ML 详情"链接 detection 重跑 → 显示链接）
- 重推 gh-pages（`516529d → a3e3b5a`，CDN 验证 39s 通过）
- **最终验证**：13 个 6-9 文件 / predictions 10 行 6-9 无重复 / agent_memory 100 行 6-9 无残留 / 线上 dashboard `_date=2026-06-09` 10 标的 / 3 个 ML 报告抽样 HTTP 200

### Lessons

- v0.27.3 PDT patch 应该全栈扫描所有写 date 的位置，不只是 reporter + backtester。这次 pheromone_board.py 漏网是因为 caller 调用 memory_store 时自己构造 entry dict，传 date 字段，不在我搜的范围
- 项目里**所有 `datetime.now().strftime("%Y-%m-%d")` 都应该是嫌疑犯**。下次审计应该 grep 全项目这个 pattern，逐一确认是 PDT 还是 local 语义

---

## [0.27.3] — 2026-06-06 — date_str 强制锁定 PDT（解决跨时区午夜偏移）

### Fixed

- **根因**：`reporter.date_str` 和 `predictions.date` 都用 `datetime.now()` 取**本地**时间。当用户电脑时区设为 CST/北京（UTC+8）且 PDT 美股交易日仍在进行时（如本地 6-6 凌晨 2:14 = PDT 6-5 11:14），date 字段会比美股实际交易日**多 1 天**，与 dashboard 显示口径错位。

- **`alpha_hive_daily_report.py:__init__`** — `self.date_str` 强制使用 `America/Los_Angeles` 时区
  ```python
  self.date_str = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
  ```
  zoneinfo 不可用时静默回退 local（向后兼容）

- **`backtester.py`** — 新增模块级 `_pdt_today()` helper，`save_predictions` 改用此函数写 `predictions.date`，与 `reporter.date_str` 口径一致

### Ops（6-5 日期归位）

- 6-5 22:27 那次扫描 yfinance 429 → 空扫描护栏正确拦截（v0.27.2 生效，未污染线上）
- 6-6 02:14 重跑（限流已解除）：
  - 本地 CST `2026-06-06 02:14` → PDT 6-5 `date_str=2026-06-05` ✓
  - 13 个 6-5 文件全部产出 / gh-pages `4f43c17` 部署成功 / 线上 dashboard `_date: 2026-06-05` 10 标的
  - **但 DB 表 patch 前已写错**：备份 DB 后执行 SQL UPDATE：predictions 10 行 `2026-06-06 → 2026-06-05`，agent_memory 100 行同步；清理 6-4 残留 agent_memory 40 行（来自 6-4 那次 429 失败扫描）

### Note

- `options_snapshot` 文件名仍用本地时间（如 `options_snapshot_VKTX_2026-06-06.json`），不影响 dashboard 显示，仅文件命名口径。后续可统一升级。
- `memory_store.py` 的 agent_memory date 来自 caller 传入，未修；本次通过 SQL UPDATE 修复历史，下次扫描需观察 caller 是否仍传本地日期。

---

## [0.27.2] — 2026-05-27 — 空扫描部署护栏 + 5-27 空 dashboard 事故回滚

### Fixed

- **`alpha_hive_daily_report.py main()`** — 新增「空扫描护栏」（save_report + auto_commit_and_notify 之前）
  - 根因：2026-05-27 20:38 daily-scan 期间 **Yahoo Finance 返回 HTTP 429（限流）**，`[CB-yfinance] closed → open` 断路器熔断 → 后续所有标的拉取被切断 → `tickers_analyzed=0` / `opportunities=0` 空报告
  - 旧行为：空报告照常 `save_report`（生成空 dashboard）+ `auto_commit_and_notify`（force-push gh-pages），**用空数据覆盖了 5-21 的好 dashboard**
  - 新行为：当 `swarm_metadata.tickers_analyzed == 0` 且 `opportunities` 为空时，跳过 save_report + 部署，保留线上上一份有效快照，仅记录 ERROR 日志
  - 与已有 `--samples-only` 短路并列，置于其后

### Ops（事故回滚，无代码）

- **gh-pages 回滚**：`056cd58`（5-27 空部署，search_index=0）→ `f78756d`（5-24 ML reports，含 5-21 好 dashboard，search_index=10），force-push 恢复线上
- **本地清理**：删除空的 `.swarm_results_2026-05-27.json`（2 字节 `{}`）；从 f78756d `git checkout` 恢复本地 `index.html` / `dashboard-data.json` / `manifest.json`
- **验证**：线上 dashboard-data.json `search_index` 恢复为 10 标的（QCOM/RKLB/VKTX/AMZN/CRCL/BILI/META/TSLA/MSFT/NVDA）

### Note

- 本次故障**与规则模型（--no-llm）无关**，纯属 Yahoo 429 瞬时限流。护栏确保后续此类瞬时故障不会再污染线上。

### Ops 追加（2026-05-28 日期归位）

- 5-27 重跑成功（Yahoo 限流已解除）拿到真实 10 标的数据，但因用户电脑当时时间设错为 5-27，`reporter.date_str` 锁定 5-27，dashboard 展示标签错为 5-27
- 5-28 系统时间校正后：
  1. **预清理**：备份 pheromone.db；删本次写入痕迹（`predictions date=2026-05-28` 10 行 / `agent_memory date=2026-05-27` 120 行 / `reasoning_sessions date=2026-05-27` 2 行）；保留历史 `predictions.exit_date=2026-05-27` 17 行（回测数据未动）；删除所有 5-27 错误标签文件（swarm/daily/ml-enhanced×10/report_snapshots×9）
  2. **重跑**：`--swarm --no-llm`，`date_str` 正确锁定 `2026-05-28`，期权快照命中复用（省 yfinance 请求），0 个 429
  3. **验证全栈一致**：13 个 5-28 文件 / predictions 10 行无重复 / agent_memory 5-27 残留 0 / gh-pages `ab50506 Deploy: Alpha Hive static 2026-05-28 21:38` / **线上 dashboard 10 标的 `_date: 2026-05-28`**

---

## [0.27.1] — 2026-05-19 — v0.27.0 二次审计 P0 修复（None safety）

### Fixed

- **`generate_ml_report.py` `_ch3_oracle()`** — P0：`dict.get(key, default)` 在 `key` 存在但 `value=None` 时**不会**返回 default，导致 `unusual[:5]` / `key_levels.get(...)` 崩溃
  - 旧：`unusual = opts.get("unusual_activity", [])` → 当字段 `=None` 时返回 None，slice 失败
  - 新：`unusual = opts.get("unusual_activity") or []`（4 处：unusual / key_levels / support / resist）
- 触发条件：options_analysis 字段在 yfinance 完全失败时全为 None（非 missing key）
- **回归验证**：NVDA 5-18 完整数据渲染 6923b 不变；全 None 数据从崩溃 → 1111b 优雅降级

### Audit (10 项边界测试)

- 测试 1（空数据）✓ 返回空字符串
- 测试 2（全 None）✗ → ✓ 修复后正常
- 测试 3（current_price=0 + call_exp_oi）✓ 正确跳过近端墙
- 测试 4（max_pain 纯数字）✓ 识别
- 测试 5（max_pain dict 缺字段，4 case）✓ 全部正确不渲染
- 测试 6（top_call_oi 含 None/字符串/缺字段）✓ 过滤
- 测试 7（iv_term_structure 字段不全，3 case）✓ 全通过
- 测试 8（gamma_calendar pin_strike 各类型，4 case）✓ 全通过
- 测试 9（call_exp_oi 含无效 expiry）✓ 仅有效项参与聚合
- 测试 10（discovery 含 HTML）⚠ 未转义，但**全项目一致行为**，不在 v0.27.x 范围内修复

---

## [0.27.0] — 2026-05-19 — ML 增强报告 OracleBee 板块扩充为完整期权视图

### Added

- **`generate_ml_report.py` `_ch3_oracle()`** — 重写期权章节，与 dashboard `#/deep` 和 generate_deep_v2 CH4 对齐
  - **头部 hero 卡片** — 新增"近端磁吸目标价（距现价 ±x%）"，从 oracle.max_pain dict 提取（NVDA = $225）
  - **新章节 1：全链 OI 结构** — Max Pain 远期参考 / 全链 P/C / 总 OI / Call+Put 拆分 + Top5 Call 阻力 + Top5 Put 支撑（含距现价百分比 + 主导到期日 badge）
  - **新章节 2：近 30 天到期 OI 墙现场聚合** — 当 JSON 含 `call_exp_oi`/`put_exp_oi` 矩阵时启用，遍历 strike × expiry 仅累加 `0 ≤ days_to ≤ 30`，输出近端 P/C + Top3 Call/Put 墙
  - **新章节 3：IV 期限结构 + IV-RV 价差** — shape 标签（Contango绿/Backwardation红/Flat金）+ 近月/远月 IV + IV-RV pp 价差 + 30日实现波动率 + 形态解读 + cheap/rich 信号
  - **新章节 4：Gamma 到期日历** — 下一主要到期日 / Pin Risk 行权价 / OI 集中度 / Charm 方向

### Changed

- **`generate_ml_report.py` `generate_html_report()` 第1410行** — `_ch3_oracle()` 调用增加 `current_price` 参数（从 `analysis.current_price` 或 Scout details 兜底）

### Compatibility

- 旧 JSON（5-18 之前，无 `call_exp_oi`）— 自动跳过近端墙章节，其他 4 块正常渲染
- 新 JSON（v0.26.4 起，5-19 daily-scan 后）— 4 块完整展示

### Validation

- 端到端测试通过：
  - 5-18 NVDA JSON 渲染 6923 字节 HTML，近端磁吸 $225 (-1.2%)、全链 6 到期日、IV Flat、Gamma 日历齐全
  - 注入伪造 `call_exp_oi` 验证近端墙：P/C 计算正确（56000/94000=0.60）、Top3 行渲染

---

## [0.26.4] — 2026-05-18 — Dashboard 近端 OI 墙现场聚合（解决"全链墙偏远"问题）

### Added

- **`options_analyzer.py` `_fetch_full_chain_oi()`** — 暴露 strike × expiry OI 矩阵
  - `max_expirations: int = 12 → 24`（覆盖更多 LEAPS 到期日）
  - 新增 `_serialize_exp_oi(exp_map)` 辅助函数：把 `{float_strike: {expiry: oi}}` 序列化为 `{str_strike: {YYYY-MM-DD: int_oi}}`，写入 JSON 友好
  - 返回 dict 新增 `call_exp_oi` / `put_exp_oi` 两个矩阵字段，供下游现场聚合任意时间窗

- **`dashboard_renderer.py` `_aggregate_near(exp_map_dict)`** — 近端 30 天 OI 现场聚合
  - 遍历 strike × expiry 矩阵，仅累加 `0 <= days_to_expiry <= 30` 的 OI
  - 返回 `{float_strike: total_near_oi}` 用于生成近端 Top3 Call/Put 墙
  - 边界保护：try/except 全包，无效数据静默跳过

### Changed

- **`dashboard_renderer.py` `_build_deep_analysis_html()` OI 墙渲染逻辑**
  - 优先级：若 `near_call_walls` 或 `near_put_walls` 非空 → 标签 `近 30 天到期`
  - Fallback：旧 JSON 缺 `call_exp_oi` 矩阵时退化为全链聚合 + 标签 `全链聚合`
  - 解决用户反馈"全链主力墙 OI 怎么会那么少" —— NVDA 主力 42% 集中在 8-21 月度 LEAPS，掩盖了近端真实墙位

### Audit

- 二次审计跑了 4 项边界测试，均通过：
  - `_aggregate_near` 当日/明天/月底/月初下月/季度边界 ✓
  - `near_pc` None safety（put OI=0 / 全空 fallback）✓
  - `max_expirations=24` 性能（NVDA 实测 0.09s/单次，24 个 ~2s）✓
  - `_wall_summary.pct_diff` 边界（cur_price=0 / strike=None / oi=None）✓
- 结论：**无 P0 critical bug**，可放心 ship

### Cost

- JSON 单 ticker 体积 +30~50KB（`call_exp_oi` + `put_exp_oi` 矩阵），10 ticker × 30 天 ≈ +15MB 历史快照增量，可接受

---

## [0.26.3] — 2026-05-18 — 近端 Max Pain 区分（区分近端 vs 全链磁吸目标价）

### Fixed

- **`dashboard_renderer.py` `_build_deep_analysis_html()` Max Pain 渲染单元**
  - **根因**：v0.26.2 把全链 Max Pain（$210，含 LEAPS 聚合）作为唯一展示，但用户问"近期的磁吸目标价还有吗" —— LEAPS 含权时间太长，对短期价格无磁吸意义
  - **修复**：主显示改为 `oracle.max_pain` dict（基于近端 3 个到期日的 Max Pain，NVDA = $225），全链 Max Pain（$210）降为"远期参考"小字
  - 标注口径明确：近端磁吸目标价 vs 远期参考，避免误读

---

## [0.26.2] — 2026-05-18 — Dashboard 全链 OI + P/C ratio 展示

### Added

- **`dashboard_renderer.py` `_detail()` 新增字段提取**
  - `full_chain_oi`：从 oracle.details 提取，包含 total_call_oi / total_put_oi / pc_ratio / max_pain / top_call_walls / top_put_walls
  - 解决用户反馈：dashboard `#/deep` 板块期权信息仅显示异常流 + 近端 P/C，缺全链聚合视图

- **`dashboard_renderer.py` `_build_deep_analysis_html()` 全链 OI 卡片**
  - 新增 `_full_oi_html` 块：Max Pain / 全链 P/C / Top3 Call 墙 / Top3 Put 墙 / Call OI / Put OI / 总 OI
  - 渲染位置：异常流面板下方，与近端 P/C 并列展示

---

## [0.26.1] — 2026-05-18 — 全链数据污染防御（系统性 yfinance sample data 加固）

### Fixed

- **`swarm_agents/scout_bee.py` `_assess_sector_relative_strength()`** — P0 修复
  - 根因：`yf.download([ticker, sector_etf], period="25d")` 返回 sample data 时，价格序列头部 ~1.0，`(_stk.iloc[-1] / _stk.iloc[0] - 1) * 100` 计算出虚假 23000%+ 涨跌，`rs = 23408%` 写入 discovery 文字和评分
  - 修复：计算前加 `_stk.min() < 5 or _etf.min() < 5` → 直接 `return result`（跳过本次评估）；再加 `abs(stock_ret) > 200` 二重保险

- **`options_analyzer.py` `calculate_gamma_exposure()`** — P0 修复
  - `stock_price <= 0` → `stock_price < 5`；sample data 价格 ~1.0 导致 GEX 差 235 倍

- **`options_analyzer.py` `calculate_iv_skew()`** — P0 修复
  - 同上，`stock_price <= 0` → `stock_price < 5`；~1.0 价格下 IV Skew 查不到任何行权价，静默返回"数据不足"

- **`market_intelligence.py` `calculate_iv_rv_spread()`** — P1 升级
  - `closes > 0` → `closes > 5`；`> 0` 无法过滤 ~1.0 哨兵值，`> 5` 完全排除 sample data 典型区间

- **`fred_macro.py` `_fetch_sector_rotation()`** — P1 修复
  - `if first_close > 0` → `if first_close >= 5`：ETF 真实价格均 > $5，< 5 视为污染跳过
  - 新增 `if abs(chg) > 50: chg = 0.0`：5 日 ±50% 以上二重保险，归零保守处理

### Unchanged (P2 可接受)

- `rival_bee.py` `_calc_technical_indicators()` RSI：RSI 计算结果天然有界 0~100，sample data 最多误推 RSI→100（超买信号），不会产生爆炸值，保持现状

---

## [0.26.0] — 2026-05-18 — HV30 计算修复（数据污染防御 + Sanity Check）

### Fixed

- **`market_intelligence.py` `calculate_iv_rv_spread()`** — HV30 在 Cowork VM 中返回 1000%+ 的根因修复

  **根因**：yfinance 在无网络的 Cowork VM 中可能返回 sample/缓存数据，价格序列头部为归一化的 ~1.0，尾部跳升到真实价格（如 $235），产生 `log(235/1) ≈ 5.46` 的虚假日收益，`np.std()` 被爆破，乘以 `√252 × 100` 后得到 1065%+。

  **修复内容（4 层防御）**：
  1. **MultiIndex columns 兼容**：`hist["Close"]` 在 yfinance ≥ 0.2.49 单 ticker 场景可能为 DataFrame，改为 `iloc[:, 0]` 显式取列
  2. **过滤零/负价格**：`closes = closes[closes > 0]`，去除 sample data 中的哨兵值
  3. **过滤日涨跌异常点**：`log_rets[np.abs(log_rets) < 0.5]`（单日 |对数收益| > 0.5 ≈ 65% 涨跌，视为数据污染，真实股票不可能）
  4. **Sanity check**：`rv_annual > 300%` 时返回 `_empty` + 明确提示信息，不再用错误数据生成误导性结论

  **同步修复**：`np.std()` 加 `ddof=1`（样本标准差，学术标准），最少有效点从 `lookback//2` 细化为过滤后 ≥ 5 条

  **验证**：污染数据旧逻辑 HV30 = 1533% → 新逻辑 32.9%，正常数据无影响

---

## [0.25.9] — 2026-05-18 — Bug修复批次（综合研判 + 格式 + 近端P/C标注）

### Fixed

- **`generate_deep_v2.py` synthesis 层1** — 删除 `_to_pwall` 死代码（计算后从未被引用，无 crash 风险但增加噪音）

- **`generate_deep_v2.py` synthesis 层3** — `gex_cw or _fc_top_c` 从 `or "N/A"` 改为条件 `:.0f` 格式化
  - 旧行为：当 `_fc_top_c=250.0` 时输出 `Call 墙$250.0`（含小数点）
  - 新行为：输出 `Call 墙$250`；两者均为空时显示 `N/A`

- **`generate_deep_v2.py` CH1 P1 综合评分段（line 1374）** — `P/C=` 改为 `近端P/C=`
  - 避免与 CH4 全链P/C（0.646）混淆，明确标注近端4个到期日口径

- **`generate_deep_v2.py` CH6 情景B卡片（line 6727）** — `P/C=` 改为 `近端P/C=`
  - 情景B"温和看涨"支持依据来自近端 OracleBee P/C，标注 `近端` 使口径明确

---

## [0.25.8] — 2026-05-16 — 跨到期日综合研判升级（全链 OI + 异常流 + GEX + IV 四层分析）

### Changed

- **`generate_deep_v2.py` `_build_options_narrative()` 跨到期日综合研判块**（完全重写）
  - **旧版**：仅根据异常流方向（bull/bear/mixed）+ 一句 GEX 环境注释，约 80 字，信息片面
  - **新版**：四层递进分析，约 250-300 字
    - 层1（OI结构基础面）：全链 P/C 定性（Call主导/Put主导/均衡）+ Max Pain 磁力方向（相对现价 ±5% 阈值）+ 全链最大 Call/Put 阻力墙位置及现价距离
    - 层2（异常流共识）：跨期方向分类 → 近多远空 / 近空远多 / 压倒性偏多 / 全面偏空 / 方向分歧，附实际美元溢价量（如 \$106.5M vs \$29.3M）
    - 层3（GEX × 流共振/矛盾）：四种组合路径 — 正GEX+多/负GEX+多/正GEX+空/负GEX+空，输出波动率含义和关键价位（翻转点/Call墙）
    - 层4（IV结构补充）：仅在 Backwardation+多/Contango+低IVR 时触发，提示策略调整（远月替代近月等）
  - 标题从"跨到期日综合研判"更名为"跨到期日综合研判（全链视角）"

---

## [0.25.7] — 2026-05-16 — Top10 OI 主力到期日标签

### Fixed

- **`options_analyzer.py` `_fetch_full_chain_oi()`** — Top10 Call/Put OI 现在附带"主力到期日"
  - 根因：全链 OI 跨期聚合后，NVDA 八月月度到期日 OI 巨大（备兑开仓 + 机构 LEAPS 尾险），Top10 行权价全被 Aug 仓位占满，用户看不出来 OI 来自哪个月份
  - 修复：聚合时同步维护 `call_exp_oi[strike][expiry]` / `put_exp_oi[strike][expiry]` 字典，记录每个行权价在每个到期日的分开 OI
  - 新增 `_dominant_exp(strike, exp_map)` → 返回该行权价 OI 最大的到期日（格式 `MM/DD`，如 `08/15`）
  - `_fmt()` 输出字典新增 `"dom_exp"` 字段

- **`generate_deep_v2.py` `_oi_rows()`** — Top10 表格每行行权价旁增加主力到期日徽章
  - 样式：灰底小圆角标签 `08/15`，字号 10px，不抢主要信息视觉焦点
  - 用户现在可以区分 `$250 [08/15]` 和 `$260 [06/20]`，了解 OI 主力所在月份

---

## [0.25.6] — 2026-05-16 — 全链 OI 日环比追踪（期权结构日变化卡）

### Added

- **`generate_deep_v2.py` CH4 "📅 期权结构日变化"卡片**（v0.25.6 新增）
  - 前提：`full_chain_oi` 在昨日和今日 JSON 中均存在时自动渲染，否则静默跳过
  - 2×2 网格布局：**Call OI 变化** / **Put OI 变化** / **全链 P/C 位移** / **Max Pain 位移**
  - Call/Put OI 格子：绿▲/红▼方向 + 万手格式绝对量 + 百分比 + 横向进度条（每15%=100%条宽）
  - 全链 P/C 格子：`旧值 → 新值`，自动判断语义（看空压力增 / 小幅偏空 / 看多信号增 / 小幅偏多 / 基本持平）
  - Max Pain 位移格子：`$旧 → $新`，注释"向上漂移/做市商磁吸上移"或"向下漂移"
  - 插入位置：`full_chain_oi_html`之后、`_gex_enhance_html`之前

### Changed

- **`generate_deep_v2.py` `extract_simple()`** — 新增 4 个全链字段
  - `fc_call_oi`、`fc_put_oi`：全链 Call/Put OI 绝对量（int）
  - `fc_pc`：全链 P/C ratio（float）
  - `fc_max_pain`：Max Pain 行权价（float）
  - 旧格式 JSON（无 `full_chain_oi`）优雅降级为 0/0.0，不报错

- **`generate_deep_v2.py` delta 计算块** — 新增全链 OI delta 计算
  - `ctx["fc_call_delta"]` / `ctx["fc_call_delta_pct"]`：全链 Call OI 日环比绝对量和百分比
  - `ctx["fc_put_delta"]` / `ctx["fc_put_delta_pct"]`：全链 Put OI 日环比
  - `ctx["fc_pc_delta"]`：全链 P/C ratio 位移（+正=偏空加剧）
  - `ctx["fc_mp_delta"]`：Max Pain 行权价位移（+正=上移）
  - `extras` 日志追加 4 行全链 OI delta 摘要，供 delta_context LLM 推理使用
  - 昨日无 `full_chain_oi` 数据时整块跳过，不影响现有逻辑

---

## [0.25.5] — 2026-05-16 — CH4 期权板块信息架构重构（P1+P2+P3）

### Added

- **`iv_crush_analysis.py`**（新脚本，独立运行工具）
  - 完全离线（无需 yfinance 网络），基于 8 个已知 NVDA 财报历史数据点
  - 财报前 Pre-IV：ATM 跨式近似公式 `IV = implied_pct / (0.8 × sqrt(DTE/365)) × 100`
  - 财报后 Post-HV30：解析估算 `sqrt((actual_move² + 29 × daily_base_var) / 30 × 252) × 100`，NVDA 基础 HV45%
  - 统计结果：平均 Pre-IV 57.8%，平均 Post-HV30 54.3%，平均压缩 -3.5pp（-6%），卖方胜率 50%（4/8）
  - 输出 `output/iv_crush_analysis.html` + 嵌入 matplotlib PNG

### Changed

- **`generate_deep_v2.py` CH4 布局优化（P1 策略结论前置）**
  - `{strategy_card_html}` 移至 `<div class="section-body">` 第一个元素（原在底部）
  - 打开期权板块第一眼即见"买方/卖方/方向中性"判断，无需下滑

- **`generate_deep_v2.py` CH4 删除冗余 Key Levels 面板（P2）**
  - 移除整个 `<div class="levels-grid">` 近端支撑/阻力 HTML 块
  - 原因：全链 OI Top10 Call = 阻力位，Top10 Put = 支撑位，双重展示信息冗余

- **`generate_deep_v2.py` CH4 异常期权流改为 Top5 默认展示 + 全列表折叠（P3）**
  - 新增 `_all_ua_by_prem`（按 dollar_premium 降序排列）、`_top5_html`、`_total_ua_count`、`_has_more_ua`
  - 默认仅显示溢价最高的 Top 5 条目
  - 超过 5 条时，完整列表（按到期日分组，原有 `unusual_items_html`）收入 `<details><summary>▸ 展开全部 N 条（按到期日分组）...</summary>` 折叠块
  - 无需 JavaScript，纯 HTML 实现渐进式披露

---

## [0.25.4] — 2026-05-14 — 深度报告期权章节升级为全链 OI 结构

### Added

- **`options_analyzer.py` `OptionsAgent._fetch_full_chain_oi()`**（v0.25.4 新增）
  - 下载全部可用到期日（最多 12 个）完整期权链，聚合所有行权价 OI
  - 过滤范围：当前价 ±40%
  - Max Pain 穷举法计算（同 oi_wall.py 算法）
  - 输出字段：`total_call_oi` / `total_put_oi` / `full_pc_ratio` / `max_pain` / `top_call_oi`（Top10） / `top_put_oi`（Top10） / `expiry_breakdown`（按到期日分布） / `oi_by_strike_call/put`
  - 失败静默返回 `{}`，不影响主分析流程
  - 结果存入 `OptionsAgent.analyze()` 返回 dict 的 `"full_chain_oi"` 字段，并写入期权快照 JSON

- **`generate_deep_v2.py` CH4 全链 OI 结构卡片**
  - 从 `ctx["full_chain_oi"]` 读取数据，有数据才渲染（无数据静默跳过）
  - 显示：总 OI（全链）/ 全链 P/C 比（附看涨/中性/看空标签）/ Max Pain 及其相对于现价的方向 / Call-Put OI 拆分
  - Top 10 Call OI + Top 10 Put OI 双列表，含行权价、OI、ITM/OTM位置、比例条形图
  - 到期日 OI 分布（Top6）：绿色=Call / 红色=Put 横向堆叠条
  - 插入位置：IV 期限结构卡片之后、GEX 增强之前
  - `ctx["full_chain_oi"]` 注入路径：`odet.get("full_chain_oi", {})` → `build_context()`

### Changed

- **`options_analyzer.py` `OptionsAgent.analyze()`**
  - 主流程调用 `_fetch_full_chain_oi()`，日志记录全链 OI 总量 / 到期日数 / Max Pain / P/C

---

## [0.25.3] — 2026-05-14 — NVDA 历史 P/C 分析强化 + 完整 OI 墙工具

### Added

- **`oi_wall.py`**（新脚本）
  - yfinance 下载 NVDA 全部到期日（22个可用，取前12个）完整期权链
  - 按行权价聚合 Call/Put OI，过滤至当前价 ±40% 区间
  - Max Pain 精确计算（穷举法：所有行权价作为到期价格，最小化买方总损失）
  - 生成 matplotlib 蝶形 OI 墙（上Call / 下Put）+ 净 OI 图，base64 嵌入 HTML
  - 输出 `output/oi_wall.png` + `output/oi_wall.html`
  - 实测结果：总 OI 9,460,107 手，当前价 $235.08，Max Pain $190，最大单笔到期 6/18 月度（3.22M OI），5/15 明日到期 2.19M OI

### Changed

- **`earnings_pc_history.py`**（完全重写）
  - 原版：设计用于对比 8 个财报日 P/C 历史，但无免费历史数据来源
  - 新版：
    - 明确呈现 2 个真实数据点（5/20/26=0.38，2/25/26=0.70）
    - 其余 6 个财报日标注 N/A + 数据限制说明（Barchart/MarketChameleon 需订阅）
    - 新增 8 个财报日实际涨跌幅 vs 隐含涨跌幅对比 Chart.js 图表
    - 新增分析卡：期权卖方 8 期胜率 75%（6/8 次实际涨跌 < 隐含波动）
    - 结论：当前 IV Rank=12（历史分位极低），期权便宜 → 买方相对有利

---

## [0.25.2] — 2026-05-06 — BearBee P2-⑨ 卡片评分永远为 0 的 Bug 修复

### Fixed

- **`generate_deep_v2.py` `_build_adversarial_bear_card()`**
  - 根因：原代码从 `raw["swarm_analysis"]` 取 BearBeeContrarian，但 JSON 顶层根本没有 `swarm_analysis` key（数据在 `swarm_results.agent_details`），导致 `bear={}` → `score=0`
  - 修复：改为直接使用 `ctx["bear"]`（在 `build_context()` 里已正确赋值），并以 `_raw_data.swarm_results.agent_details` 作为兜底降级链
  - 效果：P2-⑨ 自我对抗卡片现在正确显示真实 BearBee 评分（如 2.75/10）和 discovery 文本

---

## [0.25.1] — 2026-05-03 — 机构对冲过滤层三层增强（误判根因细分）

### Changed

- **`compare_engine_v2.py` `_apply_hedge_filter()` v0.21.0**（规则/compare_engine_v2.py）
  - 原有：仅识别 bear regime（≤1/3 MA 上方）一种对冲形态
  - 新增 **Layer B：OTM Call 尾部对冲**
    - call 加权行权价 >7% 高于现价 → 识别为空头锁定上行风险的保险流，方向→中性
    - 纯结构分析，无需 yfinance 网络调用（最快层）
  - 新增 **Layer C：备兑开仓 / Covered Call 特征**
    - `call_dominant(≥65%) + iv_elevated(≥60)` 且非纯 bull regime → 机构卖 Call 收权利金，方向→中性
    - bull regime + 非 score_high 时豁免（保留真实方向性买盘）
    - `score_high + iv_elevated + call_dominant` 额外标注"评分被卖方成交量抬高"
  - `filter_meta` 新增字段：`hedge_type`（OTM_TAIL_HEDGE / COVERED_CALL / BEAR_REGIME）、`otm_pct`

- **`compare_engine_v2.py` `archive_today_prediction()`**
  - 新增信号字段：`covered_call_pattern`（iv_elevated + call_dominant）、`call_otm_bias`（偏离≥7%）
  - 预测记录新增：`call_otm_pct`（call 加权行权价偏离度）、`hedge_type`（对冲类型标签）
  - `_apply_hedge_filter` 调用传入 `current_price` 和 `call_flows`

- **`weekly_analyzer.py` `classify_misjudgments()`**
  - 原有：9 次误判全部归为"Call主导+看多但实际下跌（机构对冲 vs 方向性）"
  - 新版：细分为 4 个子类（OTM尾部对冲 / 备兑开仓卖Call / Bear Regime宏观压制 / iv_suppressed方向性误判）
  - 向后兼容：历史无新字段的记录通过 `iv_elevated + call_dominant` 旧信号推断子类型
  - 效果：5 次历史误判被识别为"备兑开仓/卖Call特征"，4 次识别为"iv_suppressed 方向性误判"

---

## [0.25.0] — 2026-05-01 — Guard 底线否决机制（月度自诊断驱动）

### Added

- **`generate_deep_v2.py` Guard 底线否决机制**（`extract()` 函数末尾，~Line 564）
  - 来源：2026-05 月度自诊断——4/4 失败案例 Guard < 3.5，是唯一覆盖全部失败的共同特征
  - 规则一：`guard_score < 3.0` → 完全封锁信号（`direction → neutral`，`final_score → 5.0`）
  - 规则二：`guard_score < 3.5 AND direction == bull` → 置信度向 5.0 折半（`max(4.0, (score+5)/2)`）
  - 空头方向在 Guard 极低时不触发 bull 否决（避免误封），保持原有行为
  - `ctx` 新增 `guard_veto`（bool）和 `guard_veto_note`（str）两个字段
  - CH1 顶部新增红色警告横幅，当 `guard_veto=True` 时渲染否决原因
  - 验证：3 个 mock 场景（完全封锁 / 折半压低 / 不触发）全部通过

- **`self_analysis_briefs/self_analysis_2026-05.md`**（月度自诊断简报）
  - `self_analyst.py --months 3` 自动生成，分析 33 条快照，胜率 86.2%
  - Claude 推理结果（第五节）：根因分析、信号盲区、3 个新信号假说、优先级排序

---

## [0.24.1] — 2026-04-28 — VIX 数据静默丢失修复

### Fixed

- **`generate_deep_v2.py` VIX 期限结构字段名不匹配**
  - 根因：`guard_bee._calc_macro_adjustment()` 把宏观 details 整体存入 `vix_term_structure`，
    其 key 为 `vix`（数值）和 `vix_term`（字符串）；而 `generate_deep_v2.py` 下游读取时
    期望 `spot_vix` 和 `structure` 两个 key，两者不一致导致条件判断恒为 False，
    VIX 段落在 CH5 宏观章节和 F&G 交叉分析中静默不渲染。
  - 修复位置一（~Line 529）：在 ctx 构建阶段做一次规范化，将旧格式
    `{vix, vix_term, ...}` → 合并 `{spot_vix, structure, ...}`，下游三处读取自动生效。
  - 修复位置二（~Line 2443）：保留第二层 remap 作冗余保险，防止其他路径写入旧格式。
  - 新格式（`vix_term_structure.py` 直接调用路径）不受影响，条件 `not vix_term.get('structure')` 保护。
  - 验证：`analysis-NVDA-ml-2026-04-28.json` 中 `vix = 17.83`、`vix_term = contango`，
    修复后可正常渲染为「VIX 17.8，Contango 结构」段落。

---

## [0.24.0] — 2026-04-26 — 周报驱动的 9 项升级（拆 NVDA 单标的偏置 + Call 流分类 + 自我对抗）

> **背景**：2026-04-26 周报显示整体胜率 63.0%，10/10 误判全部集中在 NVDA、其中 8 次为「看多但跌」、5 次为「call_dominant + 看多」。诊断指向系统性多头偏置 + Call 主导信号被机构对冲流污染。本次升级覆盖 P0/P1/P2 三层共 9 个改动。

### Added

- **`compare_engine_v2.py:_apply_hedge_filter()` ① 机构对冲过滤层**
  - 新增 `_fetch_trend_state()` 取 SPX 200MA + SOXX 20MA + 标的 50MA 三层趋势
  - bear regime（3/3 跌破均线）下 call_dominant + 看多 → 自动翻转为「中性」
  - 写入 `predictions[ticker][date]['hedge_filter']` 元数据，可审计
  - session 级缓存避免重复 yfinance 调用

- **`weekly_analyzer.py:split_neutral_bucket()` ② 中性桶剔除**
  - |price_chg| < 1% 的样本不计入方向准确率（剔除噪音）
  - `compute_directional_accuracy()` 在净化后样本上重算 overall / bull / bear 胜率
  - 周报新增「P0-② 净化样本」KPI 卡片，与原始指标并排

- **`.tracked_deep_tickers.json` ⑩ 扩大跟踪标的池**
  - 跟踪池从 NVDA 扩到 7 只：NVDA, TSLA, AMD, SMCI, TSM, MSFT, QCOM
  - `min_samples_per_ticker = 30`，未达阈值不下结论
  - 周报新增「P0-⑩ 标的池覆盖率」进度条卡片

- **`weekly_analyzer.py:compute_per_ticker_accuracy()` ④ 单标的偏置告警**
  - 单标的胜率 < 整体均值 − 15pp 且样本 ≥ 5 → 触发 ⚠️ BIAS 警报
  - 周报新增「P1-④ 单标的胜率追踪」表格，含 Wilson CI

- **`options_analyzer.py:OptionsAnalyzer.classify_call_flow()` ⑤ Call 流分类引擎**
  - 三票制判定：A. 期限 OI 集中度（长端 > 60% → hedge）｜B. IV Skew（>1.3 → hedge，<0.8 → directional）｜C. IV 期限结构（backwardation → directional）
  - 输出 `{label, confidence, votes, reasoning}` 注入 `OptionsAgent.analyze` 结果
  - 报告层可读取 `call_flow_classification` 区分方向性 vs 对冲

- **`generate_deep_v2.py:_build_reverse_scenario_card()` ⑥ 反向情景反思**
  - 在 CH3 后插入「为什么这次可能错」卡片，4 条以内 bullets
  - 看多时列举：宏观压制 / Call 对冲嫌疑 / PEAD 反向漂移 / 样本量警示
  - 看空时列举：宏观顺风 / 短期反弹催化剂 / PEAD 正向漂移
  - 数据缺失时退化为通用模板，不阻塞报告生成

- **`weekly_analyzer.py:promote_demote_combos()` + `.combo_pools.json` ⑦ 组合自动晋级**
  - CI 下沿 ≥ 60% → 金牌池（weekly_optimizer 自动 +5pp 权重）
  - CI 上沿 ≤ 40% → 黑名单（−10pp）
  - 周报新增「P2-⑦ 信号组合自动晋级」并排卡片

- **`feedback_loop.py:register_misjudgment_pattern()` + `check_misjudgment_warnings()` ⑧ 误判模式自动回写**
  - 每条误判按 (direction, primary_reason, signal_keys) 哈希为 pattern_key
  - 写入 `thesis_breaks_config.json:auto_misjudgment_patterns[ticker][pattern_key]`
  - hits ≥ 3 自动激活；hits ≥ 5 升级为 HIGH 严重度
  - generate_deep_v2 顶部插入「P2-⑧ 误判模式预警横幅」，命中已激活模式时高亮

- **`generate_deep_v2.py:_build_adversarial_bear_card()` ⑨ 自我对抗式生成**
  - 强制 BearBee 反方推理与主流程并排呈现
  - 分歧检测：方向相反或评分差距 ≥ 3 → ⚠️ 严重分歧
  - 严重分歧时建议把仓位减半或要求额外催化剂确认

### Changed

- **`compare_engine_v2.py:archive_today_prediction()` 写入字段扩展**
  - `direction` 现为过滤后方向，新增 `direction_raw` 保留原始结论
  - 新增 `hedge_filter` 字段记录是否触发对冲过滤层

- **`weekly_analyzer.py:classify_misjudgments()` 末尾自动回写 thesis_breaks**
  - 每次运行周报时把误判模式同步注册到 thesis_breaks_config，无需手工维护

- **`generate_deep_v2.py:generate_html()` 新增 4 个 HTML 块**
  - `misjudgment_banner_html`（顶部）｜`reverse_scenario_html`（CH3 后）｜`adversarial_bear_html`（CH3 后）｜净化样本 KPI 卡片

### 验证

- `weekly_analyzer.py` 重跑 2026-04-26 数据 → 报告体积 19,603 → 22,226 bytes（+2.6KB 新卡片）
- thesis_breaks_config 自动写入 9 条 NVDA 误判模式（hits=1~2，未达 active 阈值）
- 全部模块 `python3 -c "import ..."` 通过

### 预期效果

- 整体胜率 63% → 72%~78%（Wilson CI 下沿 ≥ 60%）
- 看多胜率 55.6% → 65%+（hedge 过滤层 + 反向反思 + 误判预警三重折扣）
- NVDA 单标的偏置通过扩池稀释 + bias_alert 显式标记
- 高胜率组合（score_low+看空 等）通过金牌池自动加权进入精选

### Fixed（二次审计后立即修复）

- **`weekly_analyzer.py:classify_misjudgments` 硬编码 session ID** — `/sessions/vibrant-bold-tesla/mnt/Alpha Hive` 在新 Cowork session 会失效（违反 MEMORY.md v23.4 教训）。改为 `glob('/sessions/*/mnt/Alpha Hive') + ALPHA_HIVE_DIR + ~/Desktop/Alpha Hive` 三档兜底
- **`feedback_loop.register_misjudgment_pattern` 非原子写入 race condition** — 改用 `atomic_json_write(...)` 替代 `open()/json.dump()`，避免并行 weekly_analyzer + generate_deep_v2 同时写 thesis_breaks_config 时丢更新
- **`generate_deep_v2.misjudgment_banner_html` 信号阈值与 compare_engine_v2 不对齐** — 原代码 `pc<0.7→call_dominant` 与 archive 端 `call_pct>=65（≈pc<=0.54）` 错位，导致预警横幅可能漏触发。修复为对齐 9 项信号布尔（call_dominant / put_dominant / pc_bullish / pc_bearish / iv_elevated / iv_suppressed / score_high / score_low / resonance_active）
- **`_fetch_trend_state` MA 窗口与 bear regime 阈值** — 320 天 → 340 天（200MA 留 30 天缓冲）；bear 定义从 `n_above==0` 放宽到 `n_above<=1`，捕获 SOXX 短期反弹但 SPX/标的仍跌的混合下跌场景

### 二次验证

- 4 项修复后 weekly_analyzer 第 3 次跑：thesis_breaks 累计 30 命中 / **9 个模式全部进入 active 状态**
- 强模式 `看多但大跌+call_dominant+iv_elevated+score_high` hits=6（用户原描述的「5 次 call_dominant+看多但跌」核心模式被精确捕获）
- 全模块 `python3 -c "import ..."` + `weekly_analyzer.py` 端到端运行通过

---

## [0.23.5] — 2026-04-22 — cboe_fetcher 合成 P/C Ratio（替代 Yahoo 下架的 ^PCCE）

### Fixed

- **`cboe_fetcher.py:fetch_equity_putcall_ratio()` 因 Yahoo 下架 ^PCCE 报 404**
  - 症状：2026-04-22 运行 `generate_deep_v2.py --ticker NVDA` 时 stderr 刷 `HTTP Error 404: Quote not found for symbol: ^PCCE` + `possibly delisted; no price data found`
  - 根因：Yahoo 在 2026-04 前后清理 CBOE 官方 P/C Ratio 系列符号，`^PCCE` / `^CPCE` / `^CPC` / `^PCR` / `^PCE` 全部返回 `No data found, symbol may be delisted`（已在 `/v8/finance/chart` 端点验证全部 DEAD，仅 `^VIX` 存活）
  - CBOE 官方 CDN（`cdn.cboe.com/.../CPCE_History.csv`）也已锁定，带 UA 仍返回 403 AccessDenied
  - 修复：放弃依赖任何外部 P/C Ratio 符号，改为从 Yahoo 期权链 volume 合成

### Changed

- **`cboe_fetcher.py` 重写 `fetch_equity_putcall_ratio()` 为合成实现**
  - 新增常量 `_SYNTHETIC_PC_TICKERS = ("SPY", "QQQ", "IWM")` + `_SYNTHETIC_PC_EXPIRIES = 3`
  - 逻辑：对每个 ETF 取最近 3 个到期日的期权链，汇总 `calls.volume` / `puts.volume`，P/C = put_vol / call_vol
  - 输出新增字段 `source`（"synthetic_yf_options" / "default_fallback"）和 `tickers_used`
  - 未来 Yahoo 若再下架个别 ETF 期权数据，只需修改常量列表

- **`cboe_fetcher.py:_calculate_macro_score()` PCCE 阈值上调**
  - 原阈值（针对 CBOE 官方 PCCE，历史中位数 ~0.75）：>1.2 / >0.9 / >0.7 / >0.5
  - 新阈值（针对 ETF 合成 P/C，历史中位数 ~0.95，系统性偏高 0.2-0.3）：>1.3 / >1.0 / >0.8 / >0.6
  - 默认值从 0.75 改为 0.95（ETF 合成基线）

### Removed

- `yf.download('^PCCE')` 直接调用 — 符号已被 Yahoo 下架

### 用户侧清理步骤（需在 Mac 上手动执行一次）

```bash
rm -f ~/Desktop/Alpha\ Hive/cache/cboe_daily/pcce.json
```

清理后下次运行会重新合成并缓存。Cowork VM sandbox 无写权限，未在代码中自动清理。

---

## [0.23.4] — 2026-04-19 — weekly_optimizer / self_analyst VM 路径 bug + confirmation 周 gate

### Fixed

- **`weekly_optimizer.py` L31-45 VM 路径硬编码 bug**
  - 旧实现：`_VM_PATH = Path("/sessions/keen-magical-wright/mnt/Alpha Hive")` 硬编码旧 session
  - **影响**：Cowork 启新 session 后（当前：`ecstatic-sleepy-babbage`）脚本完全找不到 `SNAPSHOTS_DIR`，周日定时任务在 Cowork VM 里会静默空跑——看似在学习，实际没读到任何样本
  - 修复：移植 `generate_deep_v2.py:41-57` 已验证的 `glob("/sessions/*/mnt/Alpha Hive")` 动态扫描 pattern + `try/except PermissionError` 兜底
  - 额外兜底：VM 里 `深度分析报告/深度/` 目录常为空，增加回退到 `ALPHAHIVE_DIR/report_snapshots` 的逻辑（generate_deep_v2.py 的实际写入位置）
  - 验证：当前 session 路径正确解析，`SNAPSHOTS_DIR.exists()=True`，找到 169 个快照、104 条 T+7 已回填

- **`self_analyst.py` L29-40 同类 VM 路径硬编码 bug**
  - 完全相同的根因（copy-paste 自 weekly_optimizer.py 的老版本）
  - **影响**：月度 self-analysis briefing（下次 2026-05-01 03:00）在 Cowork VM 里会生成失败
  - 修复：应用与 weekly_optimizer 相同的 glob 扫描 pattern

### Changed

- **`weekly_optimizer.py:MIN_CHANGE_PP` 3.0 → 11.0（临时 confirmation 周 gate）**
  - 背景：修完路径 bug 后 `--dry-run` 揭示优化器建议
    - `signal +9.0pp`, `catalyst -10.5pp`, `sentiment -10.0pp`, `risk_adj +9.5pp`, `odds +1.9pp`
    - 3 个维度撞上 `MAX_SHIFT_PP=10` 单次限幅（意味着真实意图幅度更大）
    - Bootstrap 稳健性验证触发警告："权重可能不稳健"
    - 样本数 n=104，超过 MIN_SAMPLES=10，本可立即写入
  - 决策：本周日不让定时任务写入 `config.py`，等 2026-04-26 再攒一周 T+7 样本后复跑
    - 若方向收敛到同一侧 → 恢复 `MIN_CHANGE_PP=3.0` 放行
    - 若反向翻转 → 说明 104 条样本上过拟合，本次调权是噪音
  - 机制：`MIN_CHANGE_PP=11.0 > MAX_SHIFT_PP=10.0`，clamp 后的单次变化永远 ≤10pp，等价于**冻结写入**
  - ⚠️ 恢复条件已写入代码注释，需 2026-04-26 人工审查 dry-run 后 revert 为 3.0

### 决策背景（来自本次 Cowork session）

- 用户触发 `alpha-hive-weekly-optimizer` 定时任务，脚本因路径 bug 失败 → 回退生成增强版周报
- 周报发现：本周 T+1 68.8% (n=16, Wilson CI 44–86%)，上周 T+1 62.5% (n=40)，上周 T+7 70% (n=20)
- 硬误判率（反向 >3%）仅 3.6% — 系统基线健康
- 决定不做补丁类升级（composite 4-6 归 neutral / bear 阈值放宽 / 组合加成等），所有改动都是噪音或过拟合风险
- 仅做两件确定性零风险改动：修路径 bug + 加 confirmation gate

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
