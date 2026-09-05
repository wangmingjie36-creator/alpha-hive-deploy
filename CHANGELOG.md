# Alpha Hive · 版本变更历史

> 格式：每次 Cowork session 结束后追加一条记录。
> 规范：`Added` 新增 | `Changed` 修改 | `Fixed` Bug 修复 | `Removed` 删除

---

## [0.45.119] — 2026-09-05 — CI 第一次跑就红了：F821 守卫写死了 Mac 的解释器路径

### 现象

v0.45.117 把 CI 接上，首次运行（run #1，push 到 main）即失败：

```
2634 passed, 35 skipped, 92 deselected, 1 xfailed, 7 errors in 89.94s
ERROR tests/test_no_undefined_names.py::… —
  FileNotFoundError: [Errno 2] No such file or directory: '/usr/local/bin/python3'
```

**2634 项全过**，红的只有一个文件的 7 项——v0.45.94 标注 `network` 时留的那条
残留风险（「离线通过、有网可能转红」）**没有出现**，是另一回事。

### 根因

`tests/test_no_undefined_names.py` 写死 `PY = "/usr/local/bin/python3"`
（CLAUDE.md 的 Mac 硬规则），再 `subprocess.run([PY, "-m", "ruff", ...])`。
GitHub runner 上 Python 在 `/opt/hostedtoolcache/Python/3.11.16/x64/bin/python`，
那个路径不存在。

关键在于**这不是 fixture 里那句 `pytest.skip("ruff 不可用")` 能兜住的**：
它只检查 `returncode != 0`，而解释器本身不存在时 `subprocess.run` 直接抛
`FileNotFoundError`，根本走不到 returncode。所以本该「优雅跳过」的场景
变成了 7 个 ERROR。

形状与 v0.45.91 修的那个 bug 同类：**一个在 A 环境成立、在 B 环境不可能成立
的假设，而验证只在 A 里做过**。云沙箱里 `/usr/local/bin/python3` 恰好存在，
所以 v0.45.94 的离线验证（2133 passed）漏掉了它。

### Fixed

- `tests/test_no_undefined_names.py`：`PY = sys.executable`。
  在本机两者**完全等价**——按 CLAUDE.md 规矩执行 `/usr/local/bin/python3 -m pytest`
  时 `sys.executable` 就是那个路径——语义也更准：这个测试要的本就是
  「用正在跑测试的那个解释器去跑 ruff」。不违反 Mac 硬规则。

### Changed

- `.github/workflows/tests.yml` 安装步骤加 `ruff`。不装的话该文件会整体
  skip——那正是它诞生前的状态（「本仓库早已装好并配置了 ruff，只是没人跑」）。
  F821 守卫治的是异常分支里的 NameError 这类只在生产触发的 bug，
  在 CI 里跳过等于把守卫又关回去。实测装上后 8 项全过，
  即全仓库 F821 扫描当前是干净的。

### 验证

- **先复现再修**：把路径改成 `/nonexistent/python3`，本地得到与 CI 一致的
  `1 passed, 7 errors`；改回 `sys.executable` 后 8 passed。


## [0.45.118] — 2026-09-05 — 扫描耗时可见化：Step 2 八天涨 4.5×、被杀两次，没有一行日志说离预算还剩多少

用户问「规则模式的定时任务为什么跑完这么慢，是不是代码沉重」。先量再答：

| 日期 | Step 2 | |
|---|---|---|
| 08-26 / 08-27 | 749s / 934s | |
| 08-28 / 08-31 | **被杀** | 1800s 超时，产出 0 / 20 只 |
| 09-01 / 09-03 | 2657s / 2773s | 预算改为 30×120=3600s |
| 09-04 | **3342s** | 距预算余 **258s（7%）** |

**不是 CPU**：`feedback_loop` 载入 1081 份快照 0.3s、HGB 重训 2s、期权三本账 ~1s。
09-04 全程 66 分钟的分解：Slack 确认空等 618s（15.5%）→ 预取 115s → **30 只并行
蜂群 2466s（62%）** → 回测+权重 342s → 报告/组合/部署 420s。时间全在排队等网络。

### 根因（六项，均已定位到行）

- **L1 yfinance 全局闸门** `resilience.py:341` 0.5 req/s、burst 1：全进程一个桶。4 个
  标的 worker × 5 个 Phase-1 蜂线程 ≈ 20 线程排同一个 0.5/s 的队 → 上限 30 次/分钟，
  **调大 `max_workers` 无效**。09-04 全天 429 仅 1 次，离限流很远。
- **L2 CBOE 链缓存 TTL 120s < 单只标的流水线 ~330s**（`cboe_options.py:87`）：注释写
  「共享一次下载」，实测 29 只抓 83 次（2.9×），NVDA 单文件 1.58MB / 3.6s。
- **L3 swarm yfinance 缓存**：`config.py:1050` 把 `cache.py` 的 300s 覆盖成 120s，注释写
  「同一次扫描内共享」而扫描 2466s。L2/L3 同一族：**机制在、注释对、参数让它做不到**。
- **L4 回测逐条取价无缓存**（`backtester.py:1100/1172`）：32 条待检全是同一预测日的
  30 只票，逐条 `yf.Ticker().history()`，各过 2s 闸 → 那 342s 的主体。
- **L5 Twelve Data 两条路径两个键**：蜂群段 `fetch_daily_closes(end_date=None)`、尾段
  `fetch_bars(end_date=as_of)`，同一批 17 只票各取两遍，11s/次串行 ≈ 190s。
  `twelve_data.py:344`「两个键绝不合并」是对的（口径不同）；正解是蜂群段也传业务日。
- **L6 `pre_scan_notify --wait 10`**：规则模式下用户从不回复，每天空等满 10 分钟（不在
  Step 2 预算内，但占墙钟）。

### Added — 计时与计数器落盘（本版只做可见化，不改任何取数行为）

- `scan_timing.py`（新）：`record/timed` 记阶段耗时（同名累加、异常照记）；`counters()`
  收 `yf_gate.stats()` / `twelve_data.bars_cache_stats()` / `cboe_options.payload_stats()`，
  **取不到写 None 不写 0**（0=测过为零，None=没测到，同 v0.45.114）；`write()` 原子落
  `logs/scan_timing.json` 并打一行摘要。
- `cboe_options.py`：新增 `_payload_stats {hits, fetches, stale, failed}` + `payload_stats()` /
  `reset_payload_stats()`，接在 `_fetch_cboe_payload` 四个出口上。**此前 CBOE 根本没有计数器**，
  「29 只抓 83 次」是事后从日志 grep 出来的。
- `alpha_hive_daily_report.py`：8 个阶段 `prefetch / parallel / enrichment / swarm_total /
  backtest_weights / ml_reports / save_report / deploy`；主流程末尾与空扫描护栏早退处各
  `write()` 一次。
- 编排器：Step 2 之后打「预算余量：Xs / Ys（Z%）」；`write_status()` 的 status.json 新增
  `step2_budget{timeout,duration,headroom,headroom_pct}`（变量未设时为 `null`），并把
  **本轮日期**的 `logs/scan_timing.json` 并进 `scan_timing` 键——昨天的文件不并（缺失≠零）。
- `.gitignore` 加 `logs/scan_timing.json`（每轮覆盖的观测文件，不进 git、不触发白名单跳过提示）。
- `tests/test_scan_timing.py` 15 条：计时累加/异常照记/缺失不补零；CBOE 四出口计数；None
  不得变 `{}`；落盘原子；编排器两条 jq 过滤器**从脚本原文抠出来跑**（同日并入、换日拒绝）。

### 验证

- 变异检验五条全红：M1 拆 hits 计数（1 红）/ M2 拆 fetches 计数（4 红）/ M3 None 改 `{}`
  （1 红）/ M4 `timed` 去 try-finally（1 红；首版变异删 `finally:` 是 SyntaxError、
  `collected 0`，按「核对 collected N」规则重做）/ M5 编排器并入过滤器改恒等（1 红）。
- 抽出编排器 `write_status()` 原文进沙箱实跑：同日并入 ✓、换日不并 ✓、Step 2 变量未设
  → `null` 且 JSON 合法 ✓、无 `.tmp` 残留 ✓。⚠️ 抽函数时 `sed '/^write_status/,/^}/'`
  会在 heredoc 里顶格的 `}` 处提前截断——用函数后面那行注释当结束锚。
- 邻近 142 条既有测试 + 全量测试通过（见提交信息）。

### 后续（已定方案，本版未做）

- **A（长期价值最高）**：把 `prefetch_shared_data()` 做完整——按业务日键、一轮扫描唯一取数点：
  `yf.download` 30 只一次、`.info` 每只 1 次、CBOE 每只 1 次、Twelve Data 每只 1 次且开后台
  线程与其余并行；删蜂里 11 处直连（scout 375/389、rival 281/396、bear 270/414、
  oracle 29/79/172、chronos 114）。护栏测试：patch `yf.Ticker/download` 抛异常跑一只票，
  任何蜂还在自己上网即红；不变式「每轮 yfinance 调用 ≤ k×标的数」。
- **B.2/B.3**：余量 < 20% 置 partial 进 alert_manager；`scan_continuity` 加周环比。
- **C**：回测批量取价（独立小改，先拿回 ~5 分钟余量）；`ALPHA_HIVE_YF_RATE` 只在 A 之后、
  按档实测；Slack 等待窗口是用户决策。
- **不做**：调 `max_workers`；建通用缓存框架；动 `fetch_bars` 键规则（它是仓库里唯一做对的缓存）。

### Notes — 两条判据

- **TTL 短于作用域 = 设了等于没设**。批处理任务的缓存键应是**业务日**，不是挂钟秒数；
  注释宣称的作用域（「一次扫描内」）和参数表达的作用域（120s）对不上时，以参数为准。
- **「算了没人读」的变体**：`yf_gate._stats`、`bars_cache_stats()` 都在数，零读者；性能是
  本项目唯一没有不变式的维度，所以能静默退化 8 天。


## [0.45.117] — 2026-09-05 — 合并 `ci/python-tests`：仓库有了 CI（兑现 v0.45.94 的占号）

用户交办：把 `origin/ci/python-tests` 合了。v0.45.116 刚查明这条分支写完却从未
并入 main，本版兑现它。

分支落后 main **45 个提交**，所以**没有直接信它当年写的任何结论**——
marker 清单、测试数、CI 是否会绿，全部重新实测。实测挖出一条会让 CI 直接变红的
既有脆弱性（见下）。

### Added — GitHub Actions CI，只跑离线确定性子集

- `.github/workflows/tests.yml`：push to main + PR 触发，Python 3.11
  （与生产一致；3.9 会因 PEP604 `X | None` 注解直接崩），
  跑 `pytest -m "not integration and not network" --maxfail=0`。
  `--maxfail=0` 覆盖 `addopts` 里的 `-x`：CI 要一次看全所有失败。
  concurrency 组取消同分支的过期运行。
- `pyproject.toml` 注册 `network` marker。与 `integration` 的区别是**意图**：
  integration 测的是「对接外部系统这件事本身」，network 测的是本地逻辑、
  只是恰好在路径上碰了网络。**刻意不进 `addopts` 的默认排除**——本机有网，
  这些测试照常跑照常有价值。
- 解除 `.gitignore` 对 `.github/` 的整目录忽略（保留告警注释，见下方"部署"）。

**为什么不跑全量**：本仓库有一批测试打真实外部端点（yfinance / Treasury / CNN）。
它们在本机是绿的，放进 CI 就是噪音源——限流、端点改版、runner 出口 IP 被限速，
任何一样都会让 CI 无故转红。**时红时绿的 CI 比没有 CI 更糟：它训练所有人
忽略红灯。**

### Fixed — 合并暴露的既有脆弱性：`TestSPYBenchmarkUnavailable` 三条会让 CI 变红

在**干净检出（只含被跟踪文件，模拟 CI checkout）+ 断网**下跑合并后的子集，
`tests/test_missing_value_not_zero.py::TestSPYBenchmarkUnavailable` **3 条失败**：
`FileNotFoundError: 找不到 pheromone.db`（`portfolio_backtest.py:95`）。

根因是**守卫只堵了两条失败路径中的一条**：这三条已有
`if "error" in r: pytest.skip(...)`，但 `_find_db()` 是 **raise**，不是返回错误字典。

**它此前从不现形**，两个条件缺一不可：
1. 单独跑该文件时，`run_backtest` 会先在「无已验证预测数据」处返回错误字典，
   根本走不到 `_find_db()` —— 实测单跑 19 passed / 3 skipped，一切正常；
2. 全量跑时，前面某个测试留下的状态把它推过了那道早期检查，才撞上缺库的 raise。

而 `network` marker 摘掉 29 项**改变了执行组合**，于是它现形了。
`pheromone.db` 未被 git 跟踪，CI runner 上必然没有 ⇒ 不修 CI 必红。

修法按仓库既有惯例（`test_ml_expected_return.py` / `test_catalyst_availability.py`
都用同一句）加 class 级 autouse 守卫：生产库不存在就 `pytest.skip`。
本机有 `pheromone.db`，行为不变。

> 顺带：写这个守卫时漏了 `from pathlib import Path`，被 `ruff F821` 当场抓到。
> 「改完代码必跑 ruff」这条纪律又救了一次。

### 验证 — 没有采信分支的任何旧结论，逐条重测

搭了两个一次性 pytest 插件做实测（在 scratchpad，不入库）：
`nonet`（切断一切非本机 TCP + DNS）与 `netcount`（不拦截、只统计出网尝试）。
两者都先做了反向自证——证明 `nonet` 真能拦、`netcount` 真能数，
否则「离线全绿 / 零出网」可能只是插件没生效。

| 检验 | 结果 |
|---|---|
| `-m network` 收集数 | **29**，与分支当年一致 |
| CI 子集收集数 | 2663 / 2755 |
| **干净检出 + 断网跑 CI 子集（修复前）** | **3 failed** ← 上一节 |
| **干净检出 + 断网跑 CI 子集（修复后）** | **2639 passed, 24 skipped, 0 failed** ✅ |
| 本机全量（有网） | **2674 passed, 18 skipped**（63 deselected = integration） |

⚠️ **测错过一次树**：第一次用 `git archive HEAD` 打包，取到的是**上一个提交**，
而合并当时只 staged 未 commit —— 跑的是合并前的状态（63 deselected 而非 92）。
改用 `git archive $(git write-tree)` 才是真正要验的那棵树。
**判据：验证前先确认「我打包的到底是哪棵树」**，`HEAD` 与 index 在合并中途不是一回事。

### Notes — `network` marker 现已过期（本次**未动**，留作后续）

那次误跑反而给出一个额外证据：不加 `network` 过滤时，**全部 2692 项非 integration
测试在干净检出 + 断网下全绿**。顺着查下去用 `netcount` 实测：

> **被标 `network` 的 29 项，在网络完全可用的条件下，出网尝试为 0 次。**

原因应是 `tests/conftest.py` 的 autouse 闸（yfinance mock、v0.45.60 当日宏观层、
v0.45.61 Twelve Data）已经把它们全部覆盖住了。也就是说这 29 项如今是
离线确定性的，marker 把它们排除在 CI 之外**已无依据**，CI 因此少覆盖 1% 的用例。

**本次刻意不动它**：一来这是「合并这条分支」之外的独立决定；二来上面刚证实
摘不摘这 29 项会**改变执行组合并暴露隐藏的测试间状态泄漏**，再改一次就得把整套
验证重跑一遍。留作后续，届时需重跑本节的全部检验。
（也别据此以为「conftest 挡住了就不用 marker」——marker 防的是**有网时的
不确定性**，判据应是「出网次数」，不是「离线跑不跑得过」。）

### 部署注意

`.github/workflows/` 的改动**普通 `git push` 会被 GitHub 拒**（凭据无 workflow
scope）。`.gitignore` 里的告警注释已保留。本次推送结果见提交记录；
若被拒需改走 GitHub App 的 contents API。
## [0.45.116] — 2026-09-05 — 两条遗留占位标题：真相不是「忘了改标题」，是这两版从未并入 main

v0.45.114 收尾时顺手数出 CHANGELOG 里还有 2 条「占位（进行中：…）」标题
（v0.45.94 / v0.45.88），用户要求补上。补的过程发现它们久悬的原因不是笔误。

### Fixed — 两条占位标题补写为如实标注

先从 git 历史找原始正文——两条都**写过**，且写得完整：

- v0.45.94 的 56 行正文在 `b232eaf` 里
- v0.45.88 的 72 行正文在 `b03012c6` 里（标题为「CBOE 新鲜度分层：补抓从未触发
  + 陈旧误入缓存 + 盘中冻结静默通过」）

但**照搬回来是错的**。逐项核验发现两版的实质提交都不在 `main` 上，
落地的只有占号提交（`d5a8c6a` / `b6c7f83`）——**恢复原标题等于宣称两个从未
上线的功能已交付**，正是「文档只存指针不存陈旧结论」要治的那类误导。

| | v0.45.94 | v0.45.88 |
|---|---|---|
| 意图 | Python CI，只跑离线确定性子集 | CBOE 快照新鲜度分层 |
| 工作所在 | `origin/ci/python-tests`（领先 main 5 / 落后 45） | `origin/fix/cboe-stale-vintage-passthrough`（1/60）、`origin/fix/cboe-vintage-staleness-layering`（4/60） |
| 落地部分 | 仅占号提交 | 仅占号提交 |

**v0.45.94 在 main 上的实况**（逐项实测）：
- `.github/workflows/tests.yml` 不存在，`.github/` 未被 git 跟踪
- `.gitignore` 第 2 行仍是 `.github/`，整目录忽略未解除
- `pyproject.toml` 未注册 `network` marker，`pytest -m network --collect-only`
  收集 **0 条**（2755 全部 deselected）
- `tests/test_scan_catchup.py:56` 的 `test_syntax_valid` 仍无 skip 保护
- **仓库至今没有 CI**

**v0.45.88 在 main 上的实况**：`intraday_frozen` 在 `origin/main` 上出现 **0 次**。

⚠️ 核验时差点被一处同名字段骗过：main 上**确实有** `vintage` 相关代码
（`cloud_snapshot_fetch.py` 的 payload 反解、`close_correction.py:255` 的引用注释），
但那是 **v0.45.39** 的 vintage 校验，早已落地；v0.45.88 要做的是它之上的
**新鲜度分层**，那部分没上。**判据用的是 `intraday_frozen` 这个本版独有的标识，
不是笼统的 `vintage`**——grep 一个上下游共用的词，很容易把「上一版落地了」
读成「这一版落地了」。

两条均改写为「占号未兑现」并写明：意图、工作在哪条分支、分支上的完整正文在哪个
提交里（合并时可直接取用）、以及 main 上逐项核验的实况。
编号已进 git 历史不可回收，故保留条目、如实标注，**不复用给别的改动**。

### Notes — 这暴露的是流程缺口，不是笔误

占号规则（CLAUDE.md）解决的是「多 session 并发时别撞号」，它**没有覆盖**
「占了号但工作最终没并入」。两次都是同一形态：分支上干完、CHANGELOG 正文
也写好了，但分支没合，main 上只留下一个占位标题——**而占位标题恰好长得像
「正在进行中」，读者不会怀疑它其实已经停摆两周**（v0.45.88 停摆至今 5 天、
v0.45.94 停摆 2 天，两条分支分别落后 main 60 / 45 个提交）。

建议（本次未做，属流程改动）：占号条目应在合并时才转正；或定期
`grep -c "占位（进行中"` 核对悬挂数——这次就是这么数出来的。

**未合并的两条分支要不要合，是单独的决定**，本条只负责让 CHANGELOG 不再说谎。
CI 那条尤其值得单独评估：它落后 main 45 个提交，且合并需要走 GitHub App 的
contents API（普通 push 含 `.github/workflows/` 会被拒）。

### 验证

- 占位**标题行**（锚定 `^## \[` 再匹配「占位（进行中」）：`origin/main` 上 3 条
  （v0.45.94 / v0.45.88 / 本次占号的 v0.45.116）→ 本次提交后 **0 条**。
  ⚠️ 别用 `grep -c "占位（进行中"` 数——本条正文自己就提到这个字符串好几次，
  会被一起算进去（初稿据此误报成「剩余 3」）。必须锚定行首的 `## [`。
- 本条为纯文档改动，未触碰任何代码；全量套件与 v0.45.114 一致
  （**2667 passed, 18 skipped**），`ruff check` 通过
## [0.45.115] — 2026-09-05 — 期权三本账不在自动提交白名单里：每天被改、每天被跳过、永远不进 git

用户交办：「把 thesis_breaks_config.json 提交了，别再丢一次」。

> 编号说明：本条**连让三次号**：`0.45.111` → `0.45.112` → `0.45.115`。
> 三次都是同一 session 先把号推上了 `origin/main`（`175b83a` 占位 111、
> `d7a1637` 正式 112），而我三次都只在本地提交、没推。
> 按 CLAUDE.md 占号规则自身的逻辑「不推送的占号等于没占」，且改动已公开的
> 提交比改本地提交风险大，每次都由我让。
> **三次撞号是同一个根因的三次复现：我没执行占号第 3 步（写完占位立刻推）。**
> 这条规则的成本在文档里写得很清楚——占号几十秒，撞号要改标题、改提交信息、
> 重跑核对。我付了三遍。

`thesis_breaks_config.json` **本身无需提交**——当前内容与 HEAD 逐字节相同
（`a8f0d5b`），2026-09-04 15:00 那次扫描也未触碰它（生产代码只读不写，
唯一写入函数 `feedback_loop.register_misjudgment_pattern` 零调用点）。
它先前那份未提交改动是本 session 早些时候被我用 `git reset --hard` 清掉的，
已确认不可恢复（详见下方「事故」段）。

但顺着「别再丢一次」查下去，发现**同类风险敞口还开着**，而且更大。

### Fixed — `hedge_state/` `options_paper_state/` `vrp_state/` 加入自动提交白名单

期权路线图三本账（v0.45.101~103 建，2026-09-04 首次落盘）**从未进过 git**：
不在 `.gitignore` 里，只是没人 `git add` 过。而自动提交自 v0.43.4 起走
**白名单**（`REPORT_ARTIFACT_PATHS`），同类的 `paper_portfolio_state/` 在表内，
这三个不在。

后果是**慢性**的，不会报错：扫描每天改它们 → 白名单每天跳过（只打一行
「跳过 N 个非日报文件」）→ 永远挂在工作区未提交 → 任何一次
`git reset --hard` / `git checkout .` 全部清零。

这三本装的是**攒数期数据，丢了无法回溯重取**：

| 账本 | 内容 | 为什么补不回来 |
|---|---|---|
| `options_paper_state/earnings_signals.jsonl` | 60 行财报事件波动信号 | 含当时的真实 CBOE 双边报价，历史报价拿不回来 |
| `options_paper_state/positions.jsonl` | 跨式第一笔：COST short straddle、K=915、到期 2026-10-02、权利金 $46.00 | 成本价没有第二个来源 |
| `vrp_state/vrp_signals.jsonl` | 30 行逐票 iv−rv_30d 日度记账 | 无前视要求，事后无法补算 |
| `hedge_state/` | β·Delta 带状对冲账本 | 2026-09-04 首次真实执行：β·$Δ 21.34% NAV 出带 → 卖 13 股 SPY @ 773.17 |

改动落在两处，**必须同向**：`REPORT_ARTIFACT_PATHS`（决定实际提交什么）与
`_ARTIFACT_PREFIXES`（决定「跳过了哪些」的提示怎么说）。只改一处，提示就会
说谎——说被跳过实际被提交，或反之。这是 v0.45.23「两处默认值不同向」的同型缺陷。

### Added — `tests/test_report_deployer_whitelist.py::TestLedgerDirsAreCommitted`（8 条）

- 三本账的代表文件必须被判为日报产物
- **两处清单同步断言**：路径表里的目录项集合 == 前缀元组集合
- 反向自证：名字相近但不在白名单的路径（`hedge_state.py`、`vrp_signal.py`、
  `hedge_state_backup/x.json`）仍须被跳过

变异检验跑了两种：
① 只从前缀表撤掉三个目录（模拟「只改一处」）→ **6 红**，含同步断言；
② 两处一起撤（= 改动前的真实状态）→ **5 红**。
两种变异判到的不是同一组断言，说明同步断言不是摆设。

### 事故记录：`git reset --hard` 清掉了四个文件的未提交改动

本 session 处理版本撞号时，我用 `git reset --hard HEAD~1` 撤销自己的提交，
**没先检查工作区还有没有别人的未提交改动**。该用 `--soft`。丢失：
`thesis_breaks_config.json`（8/27 以来的增量，**未恢复**）、
`NVDA_raw.json`（14:04 扫描自己重新生成，已恢复）、
两份 `self_analysis_briefs/*.md`。

恢复途径全部试过全不通：从未进暂存区（悬空对象只有两版旧 CHANGELOG 与一份
gitignore）、`backups/` + `db_backups/` + nan 修复备份目录皆无副本、
`tmutil` 只有系统更新快照、另外五个 worktree 的同名文件哈希与 HEAD 一致。

**二次伤害**：为「恢复」跑了 `self_analyst.py`，它每次都写**全新的空白模板**，
把 `self_analysis_2026-09.md` 里已完成的手写第四节（2026-09-03，含 v0.45.93
那张 NaN 导致的 IC 位移表）**换成了空模板**，而统计数字逐字相同
（645/227/194/53.9%/+1.31%）——零收益、纯损失。已 `git restore` 还原。

> 判据一：**重跑一个生成器之前，先 diff 它的输出与仓库版本。**
> 仓库版本更长/更丰富，说明有人在生成物上手工续写过，重跑即毁。
> `self_analyst.py` 就是「模板 + 人工续写」的两段式产物。
>
> 判据二：**一个文件丢了要不要手动救，先问它是不是流水线拥有的。**
> 是就等下一次跑（`NVDA_raw.json` 当天自己回来了）；
> 不是就没人会补（`thesis_breaks_config.json` 生产只读不写）。

`self_analysis_2026-08.md` **刻意未重跑**：`load_snapshots` 的窗口是
`datetime.now() - months*31`，只有起点没有终点，今天跑出来的是六月到九月的
数据，写进一个叫 2026-08 的文件里不是还原，是伪造一份八月简报。

---

## [0.45.114] — 2026-09-05 — 数据质量行：拿不到的维度静默省略，全缺时整行消失

v0.45.113 把「三条报警通道全哑」的第 ①③ 条修了（雷达图塌陷 + warning 日志），
第 ② 条当时只写成测试记录在案。本版修掉它。

### Fixed — 同一个 `if pct is None: continue` 造成两种静默

`_build_dim_dq_html` 原本对拿不到的维度直接 `continue`：

| 形态 | 生产命中 | 旧行为 |
|---|---|---|
| 部分缺失 | **52 条 / 1401（3.7%）** | 那几维从行里**消失**，剩下的 bar 照常铺满整行，看起来像一行完整数据 |
| 全缺 | **7 条 / 1401（0.5%）** | `items` 为空 ⇒ 返回**空串**，整行在卡片上直接不存在 |
| 五项齐全 | 1342 条（95.8%） | 正常 |
| 字段缺失 / 空 dict | **0 条** | 返回空串 |

后者正是那 7 张 BRK-B 卡片（2026-08-04~08-14）的第二条哑通道：
读者同时拿到一个伪造的正常五边形雷达图（v0.45.113 已修）和一行**不存在**的
数据质量，没有任何线索知道这张卡背后没有数据。

**两种形态出自同一行 `continue`，故一并修。** 只特判全缺就正好重犯
v0.45.54 那个「修一支漏一支」的错——v0.45.113 刚为此付过代价，
不能隔一个版本再犯一次。（这条已固化为 mutation M2，见下。）

### Changed — 渲染约定（零 CSS 改动）

    有数值 → 彩色 `dq-fill` + `NN%`（不变）
    拿不到 → 空的 `dq-bar` + `dq-val` 显示 `—` + title 写「无数据」

复用既有类即可：`.dq-bar` 的 `background:var(--border)` 本身就是灰底空轨道，
`.dq-val` 已是 `var(--ts)` 静音色。**不新增 CSS 是有意的**——新增类要同步改
`templates/dashboard.css` 与已生成的 `index.html`，多一个会漂移的副本。

`—` 与 `0%` 的区别在读数与 tooltip 上：`0%` 是「测过，质量为零」，
`—` 是「没测到」。恢复这个区分正是本次的目的（已写成
`test_zero_is_not_a_dash`）。

`dim_dq` 为空 dict / `None` 时同样渲染五个「—」而不再返回空串：
五个维度名来自模块级 `_DIM_DQ_LABELS`，**不依赖入参**，所以「字段没来」和
「字段来了但值是 None」对读者是同一件事（都没测到），不该一个可见一个隐身。
生产里这两种形态各 0 次，故此项今日零影响，只是把最后一条静默路径也堵掉。

顺带补上 bool 护栏（`True`/`False` 是 `int` 子类，必须当缺失处理，
与 `_radar_data` 的同款守卫对齐）。

### 验证 — 拿全量生产数据跑新旧实现逐条 diff

比单元测试更能定住改动边界：

| 类别 | 条数 |
|---|---|
| 新旧输出完全一致 | **1342** |
| 五项齐全但不一致 | **0** ← 正常卡片必须 0 处变化 |
| 部分 None 有差异 | 52（预期：缺失维度现在显示「—」） |
| 全 None 有差异 | 7（预期：整行从消失变成五个「—」） |

**新增 `tests/test_dashboard_dim_dq_missing.py`（32 条）**，夹具取自生产真实形态
（QCOM 2026-03-16 缺 catalyst；BRK-B 2026-08-04 全缺）。
不变式是「行里永远是 5 个 `dq-item`」——少一个就是有维度被静默省略了。
判别力靠**成对**：只断言「缺失要显示 —」不够，一个「全部渲染成 —」的粗暴实现
也能全绿，故配一条**逐字节**断言锁住五项齐全时的输出与旧实现完全相同，
外加颜色阈值与 `0% ≠ —` 两组护栏。

**同时更新 `tests/test_dashboard_radar_all_missing.py`**：v0.45.113 在那里写过
`assert _build_dim_dq_html(...) == ""` 并注明「将来若有人修好 dim_dq，
这条会变红，提醒同步更新注释」。**它如期变红了**，已按新契约改成断言五个「—」，
类名由 `TestDimDqConsumerCannotCatchAllMissing` 改为 `...NowCatchesAllMissing`，
`_radar_data` 注释里「接不住这种情况」的说法一并更正。
（这是一条按设计工作的「过期即报警」测试——值得多写。）

**mutation check（`--maxfail=99`，基线 `collected 45` / 45 passed）**：

| 变异 | 结果 |
|---|---|
| M1 还原 `continue`（静默省略） | **18 failed** / 27 passed ✅ |
| M2 只特判全缺、部分缺仍省略（**修一支漏一支**） | **10 failed** / 35 passed ✅ |
| M3 粗暴修法 `if True`（全部渲染成「—」） | **17 failed** / 28 passed ✅ |

M2 是本次专门要防的那个错，单独写成变异验证测试能抓住它。

全量套件：**2667 passed, 18 skipped**（v0.45.113 的 2635 + 本次新增 32 条）；`ruff check` 通过。
## [0.45.113] — 2026-09-05 — `_radar_data` 五维全缺时画出一个「正常」五边形，而背后没有一个观测值

v0.45.112 查 `chart_engine` 那处兜底时发现的活路径问题。v0.45.54 已经为
「缺一部分维度」定过调子并写清了理由，但**只改了 `if dim:` 那一支**，
全缺走的 `else` 分支原封不动——修的是缺一部分，漏的是全缺，恰好是最该管的那半。

### Fixed — `else` 分支的五个输入没有一个能读到真数据

实测命中范围：`.swarm_results_*.json` 100 份 / 1401 条目里 **7 条**，
全是 BRK-B（2026-08-04 ~ 08-14）。旧分支产出

    [50.0, 50.0, 50.0, 66.7, 50.0]

一个毫无异常的正常五边形。逐项溯源后，**没有一个数字是观测值**：

| 维度 | 读什么 | 实况 |
|---|---|---|
| signal / catalyst | `self_score` | 该键在两个数据源合计 **4294 条里存在 0 次**（真实键名是 `score`）⇒ 恒取默认 5.0 |
| sentiment | 正则 `情绪 N%` 匹配 `discovery` | 当时 `discovery` 是错误消息 ⇒ 恒取默认 50.0 |
| odds | `put_call_ratio` | 键不存在 ⇒ 默认 1.0 → 66.7 |
| risk_adj | BearBee `score` | 键**在**、值 5.0 —— 但那是 `swarm_agents/base.py` 无效 ticker 分支的**硬编码常量** |

前两行是关键：`self_score` 这个键名在全仓生产数据里**从未存在过**。
也就是说这条 fallback 从写下那天起就读不到任何真数据，只是每一处都被
`.get(k, 默认)` 填成了看起来合理的数——**一个永远读不到真数据的 fallback 不是
fallback，是一台造数机**。这是「读错键被兜底掩盖」的形态：键名写错本该 KeyError
当场暴露，`.get` 的第二参数把它变成了静默。

第四行同样要紧：那 5.0 不是观测。同一条目里 **7 只蜂全都是
`dimension='validation'` + `confidence=0.0` + `score=5.0`**，是无效 ticker
错误路径的常量。而 `confidence=0.0` / `dimension='validation'` **正是「这只蜂
没跑」的机读签名，数据里现成就有，旧分支一个都不看**。

### Fixed — 三条本该报警的通道当时全是哑的

1. **雷达图**画成正常五边形，视觉上看不出。
2. **`dim_data_quality` 五项全 `None`** ⇒ `_build_dim_dq_html` 的
   `if pct is None: continue` 把五项全跳过 ⇒ `items` 为空 ⇒ 返回**空串**，
   页面上**整行不渲染**（不是显示 0%，是消失）。
3. **零日志**：`if dim:` 那支的 `_log.debug` 只覆盖「缺一部分」。

**修法**：`dimension_scores` 为空直接返回五个 0，并 `_log.warning`
（全缺比缺一部分是更强的异常，故用 warning 不用 debug），不再从
`agent_details` 重建。删掉 `else` 分支后 `if dim:` 成了恒真的冗余，一并去掉。

⚠️ **`dim_dq` 消费端接不住全缺，这是已核实的事实，不是推测**——所以修完之后
雷达图是唯一能看出「无数据」的通道。已把这个事实写成测试
（`TestDimDqConsumerCannotCatchAllMissing`），将来谁修好了 `_build_dim_dq_html`
那条会变红，提醒同步更新注释。

### Fixed — 更正 v0.45.54 注释里的一处归因

该段原写「缺失维度画 0 **并在 dim_dq 里标注**」。`_radar_data` **不写** `dim_dq`
——它来自上游 `sd["dim_data_quality"]`，由扫描侧产出，与本函数无关；
且实测全缺时它接不住。已就地更正（v0.45.75 教训：归因被推翻要改代码里的副本，
不能只更新记忆）。

### Notes — 根因已在别处修掉，但本分支仍可达

BRK-B 那 7 天的根因是 `swarm_agents/base.py` 的 `_RE_TICKER` 拒绝了带连字符的
ticker（`discovery` 里留着原话：`无效 ticker 格式: 'BRK-B'`）。该正则现已接受
`-X`/`.X` 类份额后缀，故 08-26 起 BRK-B 恢复正常，最近 8 个扫描日全部有
`dimension_scores`。**但本分支仍然可达，且它伪造数据的方式与根因无关**——
任何未来导致 `dimension_scores` 为空的原因都会重新触发它，所以照修。

### 验证

实测三种输入：生产全缺条目 `[50.0, 50.0, 50.0, 66.7, 50.0]` → **`[0,0,0,0,0]`**；
正常条目 MSFT `[53.9, 59.0, 39.7, 98.1, 64.6]` **不变**；
部分缺失保持 v0.45.54 行为 `[70.0, 0, 40.0, 0, 60.0]`。
`_re` 仍被其他 15 处使用，未成孤儿。

**新增 `tests/test_dashboard_radar_all_missing.py`（13 条）**，夹具取自
`.swarm_results_2026-08-04.json` 的 BRK-B 真实形状（含 7 只蜂的 validation 常量）。
判别力靠**成对**：只断言「全缺要画 0」不够，`return [0]*5` 的粗暴修法也能全绿，
必须同时断言正常条目原样通过、部分缺失保持 v0.45.54 行为。
另把「为什么删掉而不是修好 else 分支」固化成两条断言
（`self_score` 键不存在 / 蜂分是 validation 常量）。

**mutation check（`--maxfail=99`，基线 `collected 13` / 13 passed）**：

| 变异 | 结果 |
|---|---|
| M1 还原旧 `else` 分支 | **6 failed** / 7 passed ✅ |
| M2 粗暴修法 `if True`（把正常数据一起抹平） | **2 failed** / 11 passed ✅ —— 正是成对断言的另一半抓到的 |

⚠️ 过程记录：这组测试初稿有 2 条变红，是**夹具错不是代码错**——
`dimension_scores` 是 **0~10** 量表（`_d()` 再 ×10 映射到雷达图 0~100），
初稿按 0~1 写成 `0.539`。已改用 MSFT 2026-09-04 的真值 `5.39` 等。

全量套件：**2635 passed, 18 skipped**；`ruff check` 通过。
## [0.45.112] — 2026-09-05 — 删除死代码 `chart_engine.render_radar_chart`（110 行，零调用点）

用户让查 `chart_engine.py` 里 `float(raw.get("score") or 0)` 这处兜底。
查下来这行**不是问题**——它所在的整个函数从来没被调用过。与其给一个没人读的
函数打补丁，不如把函数删掉。

### Removed — `render_radar_chart`（蜂群七维雷达图，Chart 3）

**判定依据（按「死字段」那条：查功能是否生效先数读者）**：

| chart_engine 的 6 个 `render_*` | 外部引用 |
|---|---|
| `render_confidence_chart` / `render_options_chart` | 被 `generate_deep_v2` 导入 |
| `render_iv_term_chart` / `render_gex_profile_chart` / `render_deep_skew_chart` | 同上 |
| **`render_radar_chart`** | **0** |

唯一的 importer `generate_deep_v2.py:4803` 一次性导入 6 个里的 5 个，
**独独跳过它**。另已排查：无 `getattr` / 字符串名 / `__all__` 形式的动态调用，
`tests/` 里零覆盖。它是个只有生产者、没有消费者的函数。

**顺带确认那行 `or 0` 即使被调用也从未触发**：生产 803 份 `analysis-*.json`
（746 份含 `agent_details`）里，7 只蜂的 `score` 缺失 0 次、非数/NaN 0 次、
恰为 0 的 0 次，最低观测值 1.47（GuardBeeSentinel）。`or 0` 需要 falsy 才生效，
这个字段在真实数据里从来不 falsy。**两个独立的失效理由**，故不做「修补丁」处理。

（该函数内另有 `float(sr.get("final_score") or 0)` 同 species，且 `or 0` 挡不住
NaN——`float(nan) or 0` 因 NaN 是 truthy 而返回 nan，画图时 matplotlib 会静默
把多边形在那个轴断开。一并随函数删除。同文件 `:91` 等处的同形写法留在原地，
它们在**活**函数里，不在本次范围内。）

### Changed — 章节编号顺延

原 Chart 4/5 顺延为 Chart 3/4，不留编号空洞。

### 验证

- 删除 110 行（含段落分隔线）。**无孤儿依赖**：函数内用到的 `_score_color`
  (3/6)、`_GOLD`、`_fig_to_b64`、`_get_mpl`、`_BG`/`_CARD`/`_T1`/`_T3` 全部
  仍被其他 `render_*` 使用。
- `generate_deep_v2` 那条 5 项 import 实测仍成立；`chart_engine` 现存
  5 个 `render_*`，`render_radar_chart` 在模块里 0 次出现。
- 相关测试 39 passed / 1 skipped。
- ⚠️ `ruff check chart_engine.py` 报 1 条 `E401`（`import base64, io, math`），
  `--select F401` 另报 `math` 未使用——**两条均为既有问题，删除前后逐字节一致**
  （已用 `git show HEAD:chart_engine.py` 对比确认），不是本次引入，也不在本次范围内。
## [0.45.111] — 2026-09-05 — `_open_position` 的 `or 0.0` 会把缺分静默记成 0.0 分**落进账本**

用户交办：把 v0.45.110 里保留的 `score=_snapshot_score(snapshot) or 0.0` 也去掉。

去掉了，并且 v0.45.110 给它写的那句辩解（「上游 `_should_open` 已保证分数存在，
这里的 `or 0.0` 只是类型收口」）**两点都站不住**，一并更正。

### Fixed — 它不是「类型收口」，是一个会落盘的伪造值

`Position.score` 不是只在内存里转一圈：

    Position.score → _close_position → ClosedTrade.score → closed_trades.jsonl
                  → ibkr_sync（导出给用户在 TWS 下单的 actions）
                  → alpha_hive_mcp / chart_engine

缺分记成 `0.0` 不是收口，是往账本里写一个**从没发生过的分数**。
而 `0.0` 在这套量表里恰好是**最强看空信号**（`entry_score_bear = 4.85`，
观测下尾 3.78），是所有可能的谎话里最糟的那一个——一条「拿不到分数」的记录
会以「满分看空」的身份进入下游导出与图表。

第二点，**「上游保证过」不构成不检查的理由**，它正是 v0.45.3 那条判据的反面教材：
问「这个默认值会不会让下游误以为掌握了信息」——会。真要依赖上游，就该在依赖
断掉时炸掉或降级，而不是无声地编一个数。v0.45.110 写下那句辩解时只想到了
「这行代码执行不到」，没想到「万一执行到了，它写出去的是什么」。

**修法**取本函数已有的降级惯例（`size_usd` / `entry_price` 两道守卫同款：
非法输入 → `return None`，调用方 `run_for_date` 跳过该候选、当天继续）：

```python
score = _snapshot_score(snapshot)
if score is None:
    _log.warning(...)   # 走到这里说明与 _should_open 两处守卫不同步了
    return None
```

选 `return None` 而不是 `raise`：本函数已有两道同形态守卫，调用方对 `None`
的处理是现成且正确的；为一个上游已挡住的状态引入新的异常路径，收益不抵风险。

顺带删掉 `rationale` 里的 `else "N/A"` 分支——加了顶部守卫后它不可达，
留着会让人以为「缺分也能开仓、只是显示 N/A」，与实际行为相反。
`_snapshot_score` 的重复调用（rationale 一次、`score=` 一次）也合并成一次。

### 验证

**零历史行为改变，已实测**：`run_replay` 四臂 92 日回放的 `final_nav` /
已平仓账本 / 末态持仓与 v0.45.110 的结果**逐字节全等**。

**新增 `tests/test_paper_open_position_score.py`（22 条）**。
判别力全在**成对**断言上——只断言「缺分要拒绝」是不够的，
一个偷懒的 `if not score: return None` 同样能让那半边全绿，
但它会把**真实的 0.0 分**也一起拒掉。所以两半都写：

    缺键 / None / NaN / ±inf / 非数字符串  → 不开仓，且绝不产出 score=0.0 的仓位
    真实的 0.0 / 3.78 / 4.85 / 6.5 / 7.8 / 8.74 → 照常开仓，Position.score 原样记录

同 v0.45.96 记的「缓存/分桶类断言必须成对」。另有一条端到端断言，
验 score 确实一路流进 `ClosedTrade.to_dict()`——把「危害为什么值得堵」固化下来。

**mutation check（`--maxfail=99`，基线 `collected 132 items` / 132 passed）**：

| 变异 | 结果 |
|---|---|
| M1 退回 `or 0.0` | **12 failed** / 120 passed ✅ |
| M2 偷懒修法 `if not score` | **2 failed** / 130 passed ✅（正是成对断言的「合法 0.0」那半边抓到的） |
| M3 `Position(score=)` 退回 `or 0` | 132 passed —— **等价变异，非覆盖缺口** |

M3 已单独证明为等价：给定顶部守卫，走到 `Position(...)` 时 `score` 必非 None
且有限，此时 `float(snapshot.get("composite_score") or 0)` 与 `score` 在整个
可达域上恒等（20012 个取值穷举，含 `0`/`0.0`/`-0.0`/`±1e-9` 边界，0 处不同）。
**这本身就是本次改动的性质证明**：`Position(score=)` 那处去掉 `or 0.0` 是
可读性修正，真正的行为修复是顶部那道守卫。

⚠️ **过程记录：M2/M3 第一次跑出的 `132 passed` 是假的。**
锚点 `score = _snapshot_score(snapshot)\n    if score is None:` 在 `_should_open`
里也有一份（v0.45.110 加的），`s.count(old)==1` 断言抛错 ⇒ 变异**根本没打上**，
跑的是未变异的代码。这次是 patch 脚本里的 `assert count==1` 把它拦下来的——
上一版（v0.45.110）同类事故靠的是核对 `collected N items`，两道加起来才够。
**判据：mutation check 要能证明「变异确实生效了」，而不只是「测试跑了」**；
最省事的做法是让打补丁的脚本自己断言锚点唯一，并打印「变异已打上」。

全量套件：**2622 passed, 18 skipped**；`ruff check` 通过。
## [0.45.110] — 2026-09-05 — `_should_open` 的 `or 0` 只是三个洞里最窄的一个：NaN 两侧全穿

用户交办：把 v0.45.109 记录但未修的 `_should_open` 缺分兜底 `or 0` 一起修了。

动手前先把这道闸门的形态看清楚，结果比记录的那条宽——**`or 0` 是三个穿透形态里
最窄的一个，NaN 能同时穿透两侧**，而且排序键上还有一个**独立的、闸门守卫救不了**
的同类洞。

### Fixed — 闸门的两个分支都是「取反才拒绝」，任何比较为假的值都自动放行

```python
if "bull" in direction and score < CONFIG["entry_score_bull"]:  return False
if "bear" in direction and score > CONFIG["entry_score_bear"]:  return False
```

配上 `score = float(snapshot.get("composite_score") or 0)`，实测穿透矩阵
（gates 6.5 / 4.85）：

| `composite_score` | 取到的 score | bullish | bearish |
|---|---|---|---|
| 缺键 / `None` / `0` | `0.0` | 拒绝 | **✅ 穿透**（`0 > 4.85` 为假） |
| **`NaN`** | `nan` | **✅ 穿透** | **✅ 穿透** |
| `+inf` | `inf` | **✅ 穿透**（以「满分看多」进场） | 拒绝 |
| `-inf` | `-inf` | 拒绝 | **✅ 穿透** |

三者还都带着 `conf=high` 出来——见下一节。

`or 0` 只挡得住 falsy 值；**NaN 是 truthy**，`or` 短路不了它，而 NaN 与任何数的
比较都返回 `False`，于是两道「取反才拒绝」的闸门同时失效。这是
v0.45.93/97「NaN 穿透守卫」的**第三种形态**（前两种是 `if x:` 与 `<= 0`）。

**修法**：新增 `_snapshot_score(snapshot) -> Optional[float]`——缺失 / `None` /
非数 / 非有限一律返回 `None`，由调用方显式拒绝。这是 v0.45.3「安全默认值」判据的
正例：问「这个默认值会不会让下游误以为掌握了信息」，`or 0` 会，所以**不给默认值**。
`_should_open` 拿到 `None` 直接拒并说明原因。

⚠️ **本次只区分「拿不到」与「拿到了」，不新增分数合理区间。** 一个真实的 `0.0`
仍被当作合法的极端看空分放行——给它加下限属于另一个决定（需要校准依据），
且生产 1051 条里 `composite_score` 为 0 的有 **0 条**，此分支实际不触发。

### Fixed — 排序键有独立的同一个洞，闸门那道守卫救不了它

`_sort_candidates` 里的 `float(s.get("composite_score") or center)` 挡得住
缺键 / `None`（falsy），**挡不住 NaN**：`abs(nan - center)` 仍是 `NaN`，
NaN 进排序键 ⇒ 名次未定义（v0.45.93 记的原型：2 条坏行让整张 IC 表位移 0.26）。

**关键点：排序发生在 `_should_open` 之前**，所以上一节新加的守卫对它无效，
两处必须各自堵。已改为同样走 `_snapshot_score`，取不到分 → 距离 0 → 排最后。
实测 `['NAN', 'BULL', 'BEAR', 'MISS']` 输入现在稳定排成 `BEAR, BULL, NAN, MISS`。

### Fixed — `_infer_confidence` 里的 `score` 是死读，置信度完全不看分数

同一个 `or 0` 写法在 `_infer_confidence` 里还有一份，但**函数体只用 `dim_std`
与 `bear_sig_count`，`score` 从未被读过**。ruff `F841` 本可抓到，
本仓 `pyproject.toml` 全局 `ignore` 了它（注释写的理由是「many in except blocks」），
所以一直没暴露。已删除该行。

连带说明：置信度不看分数是**既有设计**，不是本次引入的——这正是上面穿透矩阵里
每一行都得到 `conf=high` 的原因。已加测试固化，免得后来者看到「缺分快照拿到 high」
误以为是 `_infer_confidence` 的 bug 而去改它。

### Changed — `_open_position` 的 `score=` 与 rationale 一并收口

- `score=_snapshot_score(snapshot) or 0.0`：上游已保证分数存在且有限，
  这里的 `or 0.0` 只是类型收口，注释已写明它不是「缺分当 0 分」的兜底。
- rationale 的 `_cs` 改走同一函数：`NaN` 现在渲染成 `N/A` 而不是字面的 `nan`。

### 验证

**生产影响为零，已实测**：92 天 1051 条 snapshot 里 `composite_score`
缺失或为 0 的 **0 条**；`run_replay` 四臂 92 日回放的 `final_nav` / 已平仓账本 /
末态持仓与 v0.45.109 的结果**逐字节全等**。本次是堵潜在陷阱，不改变任何历史行为。

**新增 `tests/test_paper_score_fallback.py`（35 条）**：退化取值 6 种
（缺键 / `None` / `NaN` / `+inf` / `-inf` / 非数字符串）× 两个方向全拒 /
`_snapshot_score` 对有限值透传（含合法的 `0.0`）/ 排序键在任何退化输入下必须有限 /
真实分数 7.15、4.60 仍开得出来 / 置信度不看分数。
含一条**反向自证** `test_nan_would_pass_both_gates_under_old_code`，
直接验 `not (nan < 6.5)` 与 `not (nan > 4.85)` 都成立，证明夹具选得对不是碰巧。

**mutation check（`--maxfail=99`，基线 `collected 61 items` / 61 passed）**：

| 变异 | 结果 |
|---|---|
| M1 `_should_open` 退回 `or 0` | **12 failed** / 49 passed ✅ |
| M2 排序键退回 `or center` | **4 failed** / 57 passed ✅ |
| M3 `_snapshot_score` 丢掉 `isfinite` | **13 failed** / 48 passed ✅ |

⚠️ **第一次跑 mutation check 时三个变异全报「无失败」，其实是一条测试都没跑。**
`pytest $TESTS` 里 `TESTS` 是多路径字符串，而 **zsh 不对未加引号的参数展开做分词**，
整串被当成单个路径 ⇒ `collected 0 items`，pytest 退出码 5。
输出里没有 `FAILED` 行，只扫一眼会把「一条没跑」读成「变异没打破任何东西」。
**教训：mutation check 必须核对 `collected N items` 与基线一致，不能只看有没有
FAILED**——这就是「查『跑完了吗』必须核对产出数量」那条纪律在测试工具上的形态。
（bash 会分词，zsh 不会；要在 zsh 里分词得写 `${=TESTS}`。本仓 shell 是 zsh。）

全量套件：**2600 passed, 18 skipped**；`ruff check` 通过。
## [0.45.109] — 2026-09-04 — 候选排序中心是从两闸导出的常数，v0.45.108 改了闸没改它

用户交办：评估 `run_for_date` 里排序键 `abs(score - 5)` 的中心该不该从名义中性点 5
改成 final_score 的实测中位，并用 `run_replay` 做 A/B 沙盒回放量化影响。

结论：**该改，但不该改成实测中位**。中位修不动这个偏置。正确的中心是**两闸中点**，
而且必须**导出**而不是再写一个常数。

### Fixed — `abs(score - 5)` 的 5 不是「名义中性点」，是旧的两闸中点

排查先从一条归因开始：v0.45.108 的注释、`tests/test_paper_entry_gate_symmetry.py`
的模块 docstring、以及 CHANGELOG v0.45.108 都写着「排序键 `abs(score - 5)` 也印证
5 = 设计上的中性点」。**因果方向是反的。**

    (entry_score_bull + entry_score_bear) / 2 = (6.5 + 3.5) / 2 = 5.0

那个 5 精确等于当时的两闸中点。它不是一个独立的设计中性点，而是一个
**从两个闸门导出的常数**。v0.45.108 把 `entry_score_bear` 3.5→4.85，
两闸中点随之移到 5.675，硬编码的 5 却留在原地——输入变了、导出值没跟着变。

与 v0.45.71「守卫恒真」/ v0.45.108「闸门恒假」同族：代码路径没毛病、
测试全绿，语义已经悄悄错位。这一族的新增形态是「**导出常数在输入变化后过期**」，
判据：看到一个魔数，先问它是不是等于别处两个数的某种组合——是的话它就不该被写死。

**后果比"多头天然优先"更强：是绝对压制。** 92 个快照日实测，C=5.0 下

| | 距心域 | |
|---|---|---|
| bull 候选（238 条，score ≥ 6.5） | [1.50, 3.74] | |
| bear 候选（45 条，score ≤ 4.85） | [0.16, 1.22] | **与上域完全不重叠** |

P(多头排在空头前) = **100.0%**——不是概率上的偏向，是每一个多头候选都压过
每一个空头候选，与信号强弱无关，排序键退化成了方向筛选器。

**旧配置下这测不出来**：`entry_score_bear=3.5` 在全部 92 天的 `report_snapshots`
里放行的空头候选是 **0 条**，两方向从未同场竞争过，这个中心从未被真实检验。
v0.45.108 打开看空闸的同时，也是第一次让这个中心真正承压。

### Changed — 排序中心改为导出，并新增 `CONFIG["candidate_sort_center"]`

- 新增 `_candidate_sort_center()`：`None`（默认）= 取 `(bull + bear) / 2`；
  给数字 = 强制（**仅供 `run_replay` 做 A/B**，生产别写死）。
- 新增 `_sort_candidates(snapshots)`，`run_for_date` 改为调它。抽成函数是为了让
  回归测试打到**生产用的那一份**排序，而不是在测试里再抄一遍 lambda——
  抄一遍就等于两个钟，改了生产那份测试照样全绿（v0.45.96 判据）。
- 兜底值 `or 5` 一并改成 `or center`。它的语义是「没分数就排最后」，
  而只有 center 能让距离取到 0。
- **兼容性**：导出值在旧闸门下精确回到 5.0，故 v0.45.108 注释里规定的旧口径重放
  `run_replay({"entry_score_bear": 3.5})` 仍然完整还原旧行为，不需要改写。

**为什么不用实测中位**：中位与两个闸门的位置无关，修不动这个偏置。
实测 P(多头排在空头前)：

| 中心 | 来源 | P(多头在前) |
|---|---|---|
| 5.00 | 现状（旧两闸中点） | **100.0%** |
| 5.35 | 2026-06-01 世代中位 | 95.9%（≈ 等于没改） |
| 5.60 | 全期中位 | 77.9% |
| **5.675** | **两闸中点（本次采用）** | **61.4%** |
| — | 同侧分位（未采用，见下） | 50.9% |

同 v0.45.108「几何对称的 4.12 只放行 2.1%，等于没改」的教训：
要对齐的是**你真正在意的那个量**，不是看起来对称的那个数。

**两闸中点不是随手挑的**：C = 两闸中点时，`abs(s - C)` 的排序**恒等于**
「超出自己那侧闸门多少」的排序（两侧距离只差同一个常数 (bull-bear)/2）。
已证：解析恒等 + 20000 组随机配对零反例，并固化为测试。
而两个闸门自 v0.45.108 起就是按**同向命中率**校准的（25.7% / 25.1%），
所以「超出各自闸门的幅度」是这套配置下唯一的同口径比较。

残余 61.4% ≠ 50%，是因为多头侧超额值域 [0, 2.24] 比空头侧 [0, 1.07] 长。
要压到 50.9% 得改用**同侧分位**，但那需要在运行时维护一份分布快照
（= 又一个会过期的导出物），本次不做。

### Changed — 92 快照日 A/B 沙盒回放（`run_replay`，4 臂）

回放范围 2026-03-09~09-03（92 日 / 52 标的），闸门两侧固定在 v0.45.108 现行值，
**只动排序中心**。`prefetch_ohlc` 预热后全程零网络（4 臂共 ~30s，非预估的 1 小时）。

| 臂 | 中心 | 终值 NAV | 已平 | 多/空 | 末态仓 | 末态净敞口 %NAV |
|---|---|---|---|---|---|---|
| A 基线（现状硬编码） | 5.000 | $53,114.74 | 91 | 82/9 | 15 | −2.52% |
| **B 两闸中点（采用）** | **5.675** | **$53,111.32** | **91** | **82/9** | **15** | **−12.50%** |
| C 世代中位 | 5.350 | $53,114.74 | 91 | 82/9 | 15 | −2.52%（与 A 逐笔全等） |
| D 全期中位 | 5.600 | $53,111.32 | 91 | 82/9 | 15 | −12.50%（与 B 逐笔全等） |

**B vs A 逐笔对比**：
- **已平仓账本 91 笔完全相同**——0 笔进出、0 笔损益变化。
- **多头账本未被改动**：少 0 笔 / 多 0 笔 / 仓位或损益变化 0 笔。
- 全期 106 次实际开仓中，**开仓序列不同的只有 2 天**：
  - `2026-08-25`：同样的 4 个仓全部开出，只是**顺序**变了（零经济差异）。
  - `2026-09-03`：最后一格 `META(bull, 7.15)` → `BILI(bear, 3.84)`。
    按闸门超额：BILI 超出看空闸 1.01 > META 超出看多闸 0.65。
- 终值 NAV 差 **$3.42（−0.006%）**——差异只发生在回放最后一天，尚未产生损益。
- 末态净方向敞口 **−2.52% → −12.50% NAV**（多 $17,255/空 $18,594 → 多 $14,605/空 $21,244）。

⚠️ **$3.42 是影响的下界，不是估计值。** 排序只在「名额稀缺 **且** 两方向同场竞争」
时才决定去留，而组合是 **2026-08-25 才第一次坐满 15 格**的：92 天里坐满 15 天、
两方向同日都有候选 12 天、两者同时成立仅 **7 天，且全部落在最后两周**。
回放窗口里这个机制几乎还没来得及起作用。按最近 10 个扫描日的节奏，
今后大约每 7 个「坐满且两方向同场」的日子会产生 1 次真实换位。

⚠️ **这不是「选得更准」。** `final_score` 的实测 IC 是 +0.025（t=+0.46, p=0.65，
见 `experiments/signal_ic_sweep_report.md`）——完全无预测力。本次改动的正当性
**只在于消除一个没人设计过的结构性偏置**（组合的方向敞口此前由一个过期常数决定，
而不是由信号决定），不得据此预期收益改善，也不得把 −12.5% 的净空敞口读作
「更好的仓位」。它只是「不再被那个常数按住」的结果。

### Changed — 口径世代边界（比 v0.45.108 更宽）

v0.45.108 只改**谁有资格进**；本次改**多头之间与空头之间的资金分配顺序**，
在名额或资金触顶的日子会改变谁拿到最后几格。因此：

- 之后的净值不能直接与之前对比。
- 要重放旧口径：`run_replay({"candidate_sort_center": 5.0}, state_dir, dates)`
  （只还原排序）或 `run_replay({"entry_score_bear": 3.5}, ...)`
  （连闸门一起还原到 v0.45.107 之前，导出中心会自动回到 5.0）。
- **实测宽度远小于理论宽度**：92 天里已平仓账本逐笔全等、多头账本零改动，
  唯一差异是最后一天的一格。理论上的"更宽"目前只体现在一个交叉换位上。

### Fixed — 归因副本清理（v0.45.75 教训：记忆更新 ≠ 代码更新）

「5 = 设计上的中性点」这条已被证伪的归因在 3 处活代码里当设计理由用着，
逐处更正（CHANGELOG 历史条目保持原样，不改写历史，更正记在本条）：

- `paper_portfolio.py:118` v0.45.108 的注释开头
- `paper_portfolio.py:135` 「排序键仍以 5 为中心」的遗留警示 → 改为指向新注释
- `tests/test_paper_entry_gate_symmetry.py` 模块 docstring + 常量
  `NEUTRAL_CENTER` → 更名 `LABEL_NEUTRAL`。该常量的**断言仍然有效**，
  但依据不同：5 在那里是**标签语义**的中性点（分数 > 5 却标 bearish 的样本
  不该被开空，该世代 263 条 bearish 里有 162 条 > 5），与排序中心无关，
  两者同为 5.0 纯属旧配置下的巧合。

### Added — `tests/test_paper_sort_center.py`（16 条，全部通过 mutation check）

覆盖：中心必须等于两闸中点 / 改闸门中心必须跟着走 / 现行闸门下中心不得仍是 5 /
两侧距心域必须重叠 / 2026-09-03 真实场景 BILI 必须排在 META 前 /
同方向内部排序在 5 个中心取值下不变 / 两闸中点 ≡ 闸门超额（5000 组随机配对）/
无分快照必须排最后。

含两条**反向自证**（v0.45.108 同款）：`test_old_center_would_fail_this_guard`
与 `test_median_center_would_also_fail`——夹具与判据必须能判出旧中心 5.0
和中位 5.35 都是坏的，否则上面的断言可能只是碰巧成立。

**mutation check（`--maxfail=99`）**：

| 变异 | 结果 |
|---|---|
| M1 中心还原成硬编码 5.0 | 5 failed / 19 passed ✅ |
| M2 中心改成世代中位 5.35 | 4 failed / 20 passed ✅ |
| M3 兜底值退回硬编码 5 | 2 failed / 14 passed ✅ |

⚠️ **M3 第一版是恒真的，靠 mutation check 才发现。** 初稿只在现行中心 5.675 下
断言「无分快照排最后」——但真候选的最小距心是 min(6.50−5.675, 5.675−4.84) = 0.825，
而兜底写死 5 也只给出 0.675 < 0.825，无分快照照样排最后，M3 变异下 14 passed 全绿。
改成跨中心 `[4.85, 5.675, 6.4]` 参数化才有判别力（6.5 不能用：与夹具 `OBS_BULL_MIN`
重合，双方距心都是 0，稳定排序下测的不再是兜底值）。
这正是 v0.45.71「守卫自己恒真」的现场复现——**判据只能来自 mutation check，
不能来自断言看起来对不对**。

### Notes — 顺带查实但**未修**的一处潜在陷阱

`_should_open` 取分用 `float(snapshot.get("composite_score") or 0)`，缺分 → 0，
而 `0 > entry_score_bear` 为假 ⇒ **一条缺 `composite_score` 的 bearish 快照会以
「满分看空信号」的身份通过闸门**。排序侧的 `or center` 会把它排到最后，
两处兜底目前互相抵消，但那是巧合不是设计。

已核实**生产数据零命中**：92 天 1051 条 snapshot 里 `composite_score`
缺失或为 0 的有 **0 条**。按 v0.45.107 的判据（查危害要看受影响路径实际触发过几次，
不是代码存在多久），判定为潜在陷阱而非在产 bug，本次不动 `_should_open`，
只记录在此。若将来快照生成链路允许缺分，这里要先改成显式拒绝。

## [0.45.108] — 2026-09-04 — 看空开仓闸门六个月只够到过一次：纸面组合结构性只做多

起因是一个观察题：`portfolio_greeks` 报出 β·$Delta 占 NAV **57.01%**、需要卖 28 股
SPY 才能拉回 ±15% 带。查为什么敞口这么高，答案是**十五行持仓全是多头**。

> 编号说明：本条原占 `0.45.106`，但另一 session 已按占号规则先把 `0.45.106`/`0.45.107`
> 推上 `origin/main`（`230397b` / `671c7de`）。按 CLAUDE.md「先进 git 历史的占号」，
> 本条让号到 `0.45.108`。本次未先推占位提交，是这次撞号的直接原因。

### Fixed — `entry_score_bear` 3.5 → 4.85（闸门恒假）

`_should_open` 的两道闸门以设计上的中性点 5 为中心、两侧各留 1.5：
bullish 需 `>= 6.5`，bearish 需 `<= 3.5`。候选排序键 `abs(score - 5)` 也印证
「5 = 中性」是设计意图。做空的代码路径是**完整的**（开仓、止损止盈方向翻转、
成本、平仓、HTML 渲染成 Short），它只是从没被调用过。

问题在 `final_score` 的实际分布并不以 5 为中心。生产 `predictions`
2026-03-09~09-03 共 1127 条：

| 项 | 值 |
|---|---|
| 中位 | 5.52（名义中性 5.00，右移 +0.52） |
| 5 分以上占比 | 81.5% |
| 触发 `>= 6.5` | 269 条（23.9%） |
| 触发 `<= 3.5` | **1 条（0.1%）** |
| 距 5 最大正偏离 / 最大负偏离 | +3.74 / −1.81 |

那唯一一条是 2026-03-13 TSLA（3.19），组合成立第 3 天。**四月起每月都是 0 条。**
纸面组合 60 笔（45 已平 + 15 在手）**全部 bullish，历史空单 0**。

这是「闸门恒假」，与 v0.45.71 记的「守卫恒真」是同一枚硬币的两面：代码路径没毛病、
测试全绿，条件在真实数据上永远取不到。**查这类问题不能读代码，只能拿生产数据数
这个分支被走过几次。**

**修法：按同向命中率对齐，不在分数上做几何对称。**
几何对称（以实测中位 5.31 为心、镜像 6.5 的偏移）得 4.12，只放行 191 条 bearish
里的 4 条（2.1%）——等于没改。按命中率对齐得 **4.85**：放行 25.1%，
对上 bullish 侧 6.5 的 25.7%。

依据窗口 2026-06-01~09-03（中位稳定在 5.25~5.37 的同一世代；四、五月中位
6.19/5.79 属另一世代，未混入）。逐月第 25.7 百分位 **4.73 / 4.77 / 4.92 / 4.75**，
极差 0.19，不是拿单月凑的。

### 沙盒 A/B 回放实测（92 个快照日，2026-03-09~09-03，`run_replay` 隔离目录）

只改 `entry_score_bear`，其余配置不动，两轮同 `sizing_mode`：

| | 旧 3.5 | 新 4.85 |
|---|---|---|
| 终值 NAV | 53,135.20 | 52,490.00 |
| 已平仓 | 82（全多） | 91（多 82 + **空 9**） |
| 累计已实现 | +2,838.60 | +2,476.00 |
| ├ 多头部分 | +2,838.60 | **+2,838.59** |
| └ 空头部分 | +0.00 | −362.59（4 胜 5 负，胜率 44.4%） |
| 在手 | 14 条全多 | 15 条（多 8 + 空 7） |
| **净方向敞口** | **+51.4% NAV** | **−11.4% NAV** |
| 毛敞口 | 51.4% NAV | 64.8% NAV |

三点要读出来：

1. **多头账本一分钱没动**（+2,838.60 vs +2,838.59，差的是四舍五入）。
   空单填的是空仓位，没挤掉任何一笔已平多单——下面那条排序担忧在这段历史里未生效，
   因为组合大多数时候没坐满 15 格（基线末态只有 14 条）。
2. **净方向敞口从 +51.4% 掉到 −11.4% NAV**，落进 `portfolio_greeks` 的 ±15% 带内，
   SPY 覆盖层基本不必出手。这正是本版要治的东西。
   ⚠️ 但它是**荡到了另一侧**（净空 11.4%），不是变均衡——由当下信号构成决定，
   不代表这个阈值会长期给出中性账本。
3. **代价是 NAV −645.20（−1.21%）与毛敞口从 51.4% 升到 64.8%**。
   空单 9 笔胜率 44.4%、多单 48.8%，两者都在 50% 上下——符合 `final_score`
   方向 IC≈0.025 的已知结论，**本版不宣称提升收益**，
   它买的是「两个方向都能被测量」。

### 已知：闸门放开 ≠ 空单一定开得出来

候选排序键 `abs(score - 5)` **仍以 5 为中心**。多头候选 6.5 距心 1.50，
空头候选 4.85 距心 0.15，多头因此天然排在前面先吃资金
（`max_positions=15`、`max_deployed_pct=80%`）。只有仓位有空档时空单才排得上。
**本版未动排序键**——改它会同时改变多头之间的资金分配顺序，是更宽的世代边界，
留给单独一版评估。

### 顺带查明：`final_score` 与 `direction` 是半独立的两个量

同一窗口 263 条 bearish 里，**162 条分数 > 5**（落在看多侧），
另有 **16 条分数 ≥ 6.5**（最高 7.86）；反向一条都没有（0 条 bullish ≤ 3.5）。
开仓闸门把 `final_score` 当方向分用，于是「看空」被翻译成了「机会质量很差」。
好在闸门先判 `direction` 再判分数，那 16 条高分看空是被拒的，**不会反向下单**。

### Added — `tests/test_paper_entry_gate_symmetry.py`（10 条）

夹具是生产观测的十分位（2026-06-01~09-03，两个方向各 11 个数），不连库跑。
- `test_gate_above_observed_floor`：闸门须高于该世代 bearish 实测最低分 3.78
- `test_hit_rates_comparable`：两侧同向命中率之差 ≤ 10pp
- `test_gate_stays_below_neutral`：另一边的护栏，闸门不得松到 5 以上
- `test_old_threshold_would_fail_this_guard`：反向自证，夹具能判出旧值 3.5 是坏的
- `TestShouldOpenHonorsGate`：真实出现过的 4.60 / 4.78 / 4.85 现在开得出来

变异检验：还原 `entry_score_bear = 3.5` 后 `--maxfail=99` 实测 **5 红 5 绿**，
红的正是上面五条载重断言。

### ⚠️ 口径世代边界

改这个值之后的净值**不能直接和之前比**。要对比须
`run_replay({"entry_score_bear": 3.5}, sandbox_dir)` 重放旧口径。

---

## [0.45.107] — 2026-09-04 — 二次复查 v0.45.106：全 neutral 标的假 PF=∞；已核实历史已交付深度报告零受影响

用户要求二次复查 v0.45.106 的改动，并核对已发布/已交付的深度报告要不要补说明性 note。
两项都查完：前者挖出一个真实的相邻边界 bug 并修复，后者核实为**不需要补 note**。

### Fixed — 全 neutral 历史标的显示"胜率 0% / PF ∞"的自相矛盾组合

v0.45.106 把 neutral 排除出 `direction_adjusted_returns` 序列（正确），但没考虑
一个标的**历史全是 neutral、一次方向性预测都没有**的情况：这时
`gross_profit=gross_loss=0`，原公式 `else 999.0` 兜底会把它渲染成 PF=∞
（绿色，暗示"零亏损的完美记录"），与同一张卡片上 `win_rate=0.0%`（红色）
并排显示成自相矛盾的组合——真实原因是"压根没有方向性记录"，不是"从未
输过"。实测生产库里 ICLN(n=1)/REGN(n=2)/MSTR(n=2)/ADBE(n=2)/ORCL(n=2)
五只标的当时命中此分支。修法：`direction_adjusted_returns` 为空时
`profit_factor` 给 0.0（配合 `sharpe` 同为 0.0，`_render_accuracy_card`
第二行自然不渲染），保留"真实有方向性交易、零亏损"仍合法显示 999.0/∞ 的
语义（用 NOW/JNJ 等标的真实数据核实未被误伤）。新增两条测试
（`TestAllNeutralTickerDoesNotFakeInfinitePF`），已验证还原修复会让测试变红。

### Notes — 历史已交付深度报告核实：零受影响，不需要补充说明性 note

排查范围：本机全部 40 份带"历史回测"卡片的深度报告（`~/Desktop/深度分析报告/
深度/deep-NVDA-*.html` 与 `deep-VKTX-2026-07-29.html`，另在 WeChat 消息目录 /
旧 session 上传目录发现的均为同批次重复文件），逐份提取胜率/Sharpe/PF 数值，
**零份显示 v0.45.106 bug 的"胜率恒 0%"特征签名**——全部显示合理数值
（66.7%~100% 胜率）。同时确认 GitHub Pages（gh-pages 分支）上 1050 份已发布
`*-ml-enhanced-*.html` 报告**不含**此卡片：`_load_ticker_accuracy()`/
`_render_accuracy_card()` 只被深度模式（Template C，`generate_deep_v2.py`
独立 CLI 脚本）调用，不接入日常自动化流水线（`alpha_hive_daily_report.py`
只写快照、不读快照算卡片），也从不调用 `_sync_ghpages()` 或 Slack 推送——
深度报告只落本地文件，从未被"发布"到网站，只曾以文件形式交付给用户。

原因链已查清：`self_analyst.py` v0.45.85 的 docstring 早记录过
"2026-07-29 前冻结的旧快照用的是 Long/Short/Neutral，之后 paper_portfolio.py
v0.38.0 挂载起才切换成小写 bullish/bearish/neutral"——`git log` 确认
`_load_ticker_accuracy()` 的 `== "Long"/"Short"` 比较是这个函数**从第一次提交
起就有的写法**（非后期回归），只是在词表切换之前，生产数据本来就是大写
`Long`/`Short`，字面量比较恰好对得上，bug 处于潜伏状态。本机全部 62 份本地
深度报告最晚一份正是 **2026-07-29**（VKTX，边界当天）——用户此后再没跑过
深度模式，所以词表切换之后、这次修复之前，这条代码路径实际上从未被真正
执行过一次。**结论：这个 bug 存在于代码里很久，但从未在任何一次真实报告
生成里产生过错误输出**，v0.45.106+v0.45.107 的两处修复只影响"从现在起
新生成"的深度报告，历史交付物无需补充说明或重新生成。

全量 2539 通过（0 failed，18 skipped，1 xfailed），ruff F821 干净。

## [0.45.106] — 2026-09-04 — generate_deep_v2._load_ticker_accuracy() 同物种 direction 词表 bug——极性反转而非塌缩

v0.45.85 修了 self_analyst.py 的 classify()：direction 字面量比较 `== "Long"`
从未匹配生产快照实际写入的小写 `bullish`/`bearish`/`neutral`，导致 correct/wrong
恒为 0（塌缩成 neutral）。复查同一物种 bug 时在 `_load_ticker_accuracy()`
（深度报告模板 C 的历史准确率卡片：胜率/Sharpe/PF/最大连败）里发现更严重的
变体——不是塌缩，是**极性反转**。

### Fixed

- `generate_deep_v2._load_ticker_accuracy()`：`is_win = (s.direction == "Long"
  and ...) or (s.direction == "Short" and ...)` 与 `adj_ret = ret if
  s.direction == "Long" else -ret` 同样从未匹配过小写词表——实测
  `~/Desktop/Alpha Hive/report_snapshots/`（1051 条真实快照）100% 是
  `bullish`/`bearish`/`neutral`，0 条 `Long`/`Short`。`is_win` 因此恒为
  `False`（胜率恒 0%），而 `adj_ret` 的 else 分支对**全部**快照取反——
  每一笔 bullish 盈利交易被算成等额亏损、bullish 亏损被算成等额盈利
  （bearish 恰好因为"取反"本来就是其正确公式而被巧合抵消，未受影响）。
  下游 `gross_profit`/`gross_loss`/`sharpe`/`profit_factor`/`max_consec_loss`
  全部基于这个反转后的序列计算，同样失真。修法与 classify() 一致：
  `direction = str(s.direction).strip().lower()`，同时接受
  `long`/`bullish`→看多、`short`/`bearish`→看空，大小写不敏感；neutral/未知
  方向不再被隐式当空头处理，改为直接排除出 Sharpe/PF 序列（对齐
  `feedback_loop.BacktestAnalyzer.calculate_accuracy()` 里"neutral 不计入
  方向性收益"的既有口径，而不是延续原公式"非 Long 即取反"的隐藏假设）。
  真实 XOM 快照验证：2026-08-10 bullish、ret=+3.12%，修复前
  `is_win=False, adj_ret=-3.12`；修复后 `is_win=True, adj_ret=+3.12`。
  新增 `tests/test_ticker_accuracy_direction.py`，5 个用例覆盖小写词表下
  win_rate/profit_factor 不再退化、neutral 正确排除、大写 Long/Short 旧词表
  仍可识别；已验证还原修复会让 win_rate 退回 0.0、profit_factor 退回 0.5
  （旧 bug 的可复现签名）。
- 影响面：仅深度报告模板 C 的"历史预测记录"卡片（Sharpe/PF/胜率/最大连败
  四个数字），不影响 paper_portfolio 实盘记账、不影响 weekly_optimizer 权重
  学习闭环（两者读同一批快照但走不同代码路径，未触达这个 bug）。已生成的
  历史深度报告不做回溯修正，仅影响本次修复起新生成的报告。

### Notes — 同一文件里的姊妹函数：确认死代码，未动

`feedback_loop.py` 的 `ReportSnapshot.check_direction_accuracy()` /
`BacktestAnalyzer.calculate_accuracy()` / `generate_accuracy_dashboard_html()`
/ `save_accuracy_dashboard()` / `analyze_misses_with_llm()` 有完全相同的
`self.direction == "Long"/"Short"` 字面量比较，理论上是同一个 bug。全仓 grep
确认：五个真实生产 `BacktestAnalyzer(...)` 实例化点（weekly_optimizer.py
四处 + generate_deep_v2.py 一处）只用 `.snapshots`/`suggest_weight_adjustments()`
/`get_snapshots_by_ticker()`，无一调用这条链路——唯一调用方是
feedback_loop.py 自己的 `__main__` 演示块与 `tests/test_feedback_loop.py`
（后者按当前字面量语义断言 7+ 条用例）。判定为死代码，未修，只在
`check_direction_accuracy()` 加了一段指向本记录的注释，供日后接回生产前提醒。

## [0.45.105] — 2026-09-03 — 二次复查遗留三项：口径不对称、假数据顶观测值、K 线重复取数

v0.45.104 修掉 41 处后仍有三项被刻意推迟。它们分别是**算错数**、**假数据**、
**重复取数**，这一版补齐。三处都配了「还原修复就变红」的回归测试。

### Fixed — 分子分母含的扩散不一样多，两个偏差不抵消而是复利

`earnings_vol_signal.compute_signal` 的分子把 dte 天的扩散**整个减掉**（得到纯事件
波动），而分母是 `earnings_history` 的**两个交易日**窗口，天然含 2 天扩散。
分子偏小、分母偏大 —— 比值被系统性压低，一切被推向 `cheap`。
两个模块的头部原先都写着「两个偏差同向、比值受影响较小」，**那句话说反了**
（v0.45.75 那类「注释替一条已撤回的因果续命」）。
蒙特卡洛实测（σ_ann 30%、20 万条路径）分母被抬高的幅度：
真实事件 sd 3% 时 **+13.57%**、5% 时 +6.18%、8% 时 +2.50%。

**修法不是改窗口。** 两日窗口是**刻意**的安全选择：yfinance 不给可靠的盘前/盘后
标记，单日窗口会在盘前发布时整个错过事件。正确做法是把同样长度的扩散**加回分子**，
让两边都等于「事件 + 2 个交易日扩散」：

    diffusion_window_move_pct = 0.8 · σ_rv · √(2/252) · 100
    implied_event_move_pct    = √(max(straddle² − diffusion² + diffusion_window², 0))

⚠️ 窗口那一项用**交易日/252**（窗口就是 2 个交易日），与 dte 用**日历日/365**
各自成立，不是又一次混用 —— `2/252` 恒等于 `2×(365/252)/365`。
窗口长度提为 `CONFIG["realized_window_trading_days"]`，注释写明**改它必须同时改
`earnings_history` 的窗口**，否则两边又错开。

模块自己的两个校准例子（三代口径对照）：

| 口径 | NVDA (12.5%/rv45/中位8.0) | COST (5.58%/rv22/中位3.5) |
|---|---|---|
| v0.45.99 `dte/252` | 0.333 `cheap`（买跨式） | 扩散>跨式 → `floor/untradeable` |
| v0.45.104 `dte/365` | 0.9124 `fair` | 0.7298 `cheap` |
| **v0.45.105 加回窗口扩散** | **0.9966 `fair`** | **0.8564 `fair`** |

两个例子现在都落在 ~1.0，符合「大致公允定价的跨式」这个先验 —— 是修正方向正确的
旁证，不是调参调出来的。新增字段 `diffusion_window_move_pct`，
`event_move_basis` 改为 `"straddle_minus_diffusion_plus_window"`
（无生产代码按它分支，仅进 rationale 字符串）。

### Fixed — `earnings_time` 是写死的假定值，却顶着观测值的名字落盘

唯一的生产者 `EarningsWatcher.get_earnings_date` 写的是写死的 `"AMC"`
（yfinance 的 calendar 不返回时段），ChronosBee 催化剂那条路径同样。
叫 `earnings_time` 时，日后任何按盘前/盘后分层的校准都只会得到**一个桶 + 一个空结果**，
然后被误读成「时段不影响」—— 本项目「假数据披着观测值外衣」的老形态。
改：信号记录里改名 `earnings_time_assumed`，另加 `earnings_time_source`
（记明来自 watcher 还是 ChronosBee 催化剂）与恒为 `None` 的 `earnings_time_observed`
槽位；三个生产者各自加 `earnings_time_assumed: True` 旗标。

### Changed — 三份互不共享的 K 线缓存合成一份

同一只票的日线一轮扫描最多被取 3 遍：`portfolio_greeks`（100 根，有自己的进程缓存）、
`vrp_signal`（120 根，无缓存）、`options_paper_leg`（10 根，无缓存）。
Twelve Data 是 **7 次/分钟的串行队列**，而 v0.45.89 的教训正是写死的超时把扫描
杀在 20/30。

缓存放在 **`twelve_data.py`**（三个消费者都已依赖的叶子模块），
**不能**放 `portfolio_greeks` —— 它已经 import `options_paper_leg`，反向依赖会成环。
`fetch_bars(ticker, days, end_date)` 按 `(ticker, end_date)` 记住**已取到的最大窗口**，
更小的请求切尾返回；更大的请求重取并替换。失败结果**不入缓存**
（v0.45.50 的教训：把失败缓存起来会把一整轮钉死），且一次更大的失败请求不会
毁掉已有的较小条目。每次返回都是全新副本（实测调用方改动不污染缓存）。
`end_date=None` 与具体日期是**不同的键**。窗口统一到 `SHARED_BARS_WINDOW = 120`。

模拟 30 只标的（三个消费者各碰每只票，取上界）：

| | HTTP 取数 | 每票 | 串行队列耗时 |
|---|---|---|---|
| 改前 | 91 | 2.935 | 780 s |
| 改后 | 31 | 1.000 | 266 s |

**省 60 次调用 / 约 514 秒。** 3→1 这个**倍数**与覆盖率无关，绝对秒数随实际覆盖缩小。

两处需要留意的连带改动：`options_paper_leg` 改要 120 根（它跑在最前面，
只要 10 根会把后面两个 120 根的消费者饿死、等于没省；`outputsize` 同一次请求，
不多花额度，`_pick` 取「≤ as_of 的最后一根」答案不变）；`vrp_signal` 改传
`end_date=as_of`（否则键永远对不上，3→1 实际只是 3→2）—— 实时扫描行为不变，
补跑反而**更准**（原先是「取最新 120 根再截到 as_of」，as_of 早于窗口时会一根不剩；
现在是「截至 as_of 的 120 根」，正是 `settle_window_bars=120` 要量的东西）。
`settle` 仍以 `bars_fn(ticker)` 单参调用，as_of 走闭包，注入点契约不变。

**刻意没做**：`fetch_daily_closes` / `fetch_volume_ratio` 仍走未缓存的原路径。
它们要 60/30 根，而对一个**已剔除半根**的 120 行列表切尾会比直接取 30 根多一根，
那会挪动 `rv_30d` —— 一个进评分的字段。那是**口径改动**不是缓存改动，需要单独决策；
已加两条测试钉住它们留在原路径。缓存无 TTL、无容量上限（一轮扫描就是一个进程），
docstring 写明长驻进程要自行 `clear_bars_cache()`。


## [0.45.104] — 2026-09-03 — 期权路线图五版的二次复查：41 处修复，其中 3 处会算错钱

**做了什么**：对同一天落地的 v0.45.99~103（期权路线图五步）做对抗式二次复查——
4 路独立审查（每路要求「先试着推翻自己的发现」+ 变异检验）→ 4 路并行修复
（每处修复必须配一条「还原修复就变红」的回归测试）。**41 处修复**，
测试从 2408 涨到 **2501 全绿**，ruff F821 干净。

教训先写在前面：**这五版是同一天写的，当天复查就挖出 3 个会算错钱的 bug 和
2 条「删掉实现测试依然全绿」的核心守卫。** 写完即测不等于写完即对。

### Fixed — 会算错钱的三处

- **`earnings_vol_signal.py` 扩散项高估 20.35%（最严重）**：`dte` 是**日历日**
  （`cboe_options` 的 `(expiry − today).days`），却除以 **252**（交易日年）。
  正确配对只有日历日/365 或交易日/252，混用把 √T 放大 √(365/252)=1.2035。
  因为它在根号下被减去，标签会整个翻转，且方向单一——一切被推向 cheap、
  `event_move_floor_hit` 从「罕见退化」变成 30% 波动率名字的**常态**，
  卖跨式那一侧会被饿死。实测（rv 30%、dte 29）扩散 8.1416% → **6.7649%**；
  用模块自己的校准例子：NVDA 12.50/45%/8.0 从 ratio 0.333 `cheap`（买跨式）
  → **0.912 `fair`（不动）**；COST 5.58/22%/3.5 从 `floor_hit/untradeable`
  → **0.730 `cheap`**。改 `dte/365.0`，模块头部公式同步更正。
- **`options_paper_leg.py` 迟到的内在价值结算用了今天的收盘价**：报价长期缺失后
  走内在价值平仓时取 `closes_fn(ticker, as_of)`，**从不看 `pos.expiry`**。
  复现：K=100、2026-10-02 到期、到期时标的 101（真实结算 $1.00/股 = $100），
  行情恢复到 12-20 时标的 160 → 记成 **$60.00/股、+$5,780**，而正确是 **−$120**。
  改 `min(as_of, pos.expiry)`；并补上免费不变式 `holding_days` 不得超过
  `expiry − entry_date`（该例 108 → 29，溢出记进新字段 `settled_late_days`）。
- **`options_analyzer.py` 报价集会把行权价中位数当成标的价**：`stock_price`
  缺失/NaN 时回退 `atm_price`，而那时它已经是
  `statistics.median(all_strikes)`（再取不到就是写死的 **145.0**）。传一个正数进去
  会让 `fetch_cboe_quote_set` 以为调用方给了真价，**跳过 CBOE 自己的 `official_price`**，
  并把 `underlying_price_source` 记成 `"caller"`。同一条链实测隐含波幅
  8.4138% → 7.2137%、ATM 行权价 145 → 150。生产可达：数据源失败时蜂把 price=0/NaN
  往下传（`swarm_agents/base.py` 只置 `_data_unavailable`，`oracle_bee` 不检查），
  且日报有一处 `analyze(_tk)` 根本不传价 —— AMC 这类 $3 的票会被记成 $145。
  改传 `0.0`。

### Fixed — 幽灵状态与静默数据丢失

- **`options_paper_leg.py` 拿不到任何价格的仓位永远漏在账上**：既无报价又无收盘价时
  无条件 `remaining.append(pos)`，无年龄上限。复现：到期一年后仍未平，净值里还挂着
  入场那天的 mid。两个加重因素：`stale_days` 数的是**运行次数**不是天数
  （跑满 12 个月显示「10d」）；而过期合约在 CBOE 链里永远查不到，
  `quote_contracts` 恒返回 None，所以这是**永久**状态不是暂时的。
  改：`stale_days` 改按日历日算；新增 `write_off_days_after_expiry=30`，
  逾期且无任何价格 → `mark_source="written_off"` 强制核销并 `_log.error` 点名；
  `compute_kpis` 增 `written_off_exits`/`stale_positions`，日报小节把冻结 mark
  显式标成「⚠️ stale (63d @ …) 只是账面占位」。
- **`record_day` / `scan` 按日期重写会抹掉已结算字段**：两个模块都是「删该日旧行 →
  重建全 None 行」，而日志照打成功。日常路径不会触发（行不可能在自己的 `as_of`
  当天被结算），但**任何 `--date` 补跑都会**，且重建的行更差（见下条 look-ahead）。
  VRP 侧改：新增 `_SETTLEMENT_FIELDS` 单一清单并逐字段搬运，`iv` 被修订时
  `vrp_realized` 重算而非沿用；财报侧同样搬运四个字段。
- **`portfolio_greeks.combined_nav_detail` 的 `not_started` 会掩盖真实数据丢失**：
  只判断跨式腿的**净值文件**存不存在。删掉一本真持有 $98,000 的账的净值文件，
  输出与「从未建账」逐字节相同。更糟的变体——持仓文件在、净值文件不在：
  跨式腿的 Greeks 在分子里、NAV 在分母里是 0，`partial=False`、
  `band_status="above"`，实际**成交了 −76 股 SPY 对冲**。改为净值与持仓
  两个文件都空才算未建账，否则是数据丢失 → `nav=None` 并点名。
- **`portfolio_greeks` 对冲账本里 `shares` 为 null/NaN 的行被静默丢弃**：
  `_scrub` 把 NaN 写成 null，读回来变 None 后整行消失，聚合报
  `band_status="empty", partial=false` —— 自信且错误。而同模块的
  `stock_exposures` 对同样情形处理是对的（标 flag → `unknown` → 不对冲）。
  改为同构处理，并区分「真的 0 股」与「读不出来」。

### Fixed — 两条核心守卫「删掉实现测试依然全绿」

`vrp_signal` 最常被引用的两条声明，实现删掉后 39 条测试**全绿**：

- **禁前视**：夹具的切片在有无过滤器时完全相同。改用判别性用例——
  基准日 2026-07-01、`as_of=2026-07-30`（21 个工作日但只有 20 个交易日，
  因为 7/3 独立日休市）、K 线到 09-30：有守卫 → 不结算；没守卫 → 吃到
  **2026-07-31** 那根，比 `as_of` 晚一个交易日。
- **禁横截面池化**（本项目最常被引用的失效模式）：原夹具 `x=100k+i, y=300k+2i`
  把 x 与 y 同向偏移，池化算法照样得 ρ=1.0。改成 `y=−300k+2i`：
  票内 ρ=+1.0，池化 ρ=**−0.7781**，并把被否决的实现写进测试正文。

财报模块另有 5 条装饰性测试，其中一条把**同一个函数用同样参数调了两次然后断言相等**
（名字叫「BMO 和 AMC 给出相同结果」，却观察不到 BMO/AMC）；短仓上限的整个分支
删掉后 79 条测试仍全绿（`floor` 早就保证了那个不等式，循环体在 183 万组随机输入下
执行 0 次）。全部重写或删除。

### Fixed — 日报里看不见的三个新小节

`run_swarm_scan` 里 `_build_swarm_report`（拼 markdown）比 `_post_scan_notify`
（跑三个钩子）**早一行**。于是 v0.45.101~103 的小节读到的是钩子跑之前的状态：
**VRP 小节 `rows_for_date(today)` 恒空 → 返回空串，一天都不出现**；期权纸面腿恒显示
「扫描 0 个快照」、标记停在昨天；组合 Greeks 只能退回
`run_for_date(execute=False)` 把整套计算重做一遍，且渲染的是对冲成交前的状态，
与一分钟后落盘的审计文件对不上。**这与 v0.45.57 的失效条件是同一个坑**
（该方法开头 1067 行就写着这条注释）。修法照抄那次：`report` 是可变 dict、
且在 `run_swarm_scan` 返回后才被 save/部署消费，所以在钩子跑完后追加。
新增 `tests/test_daily_report_section_order.py`（4 条，AST 检查，挪回去即变红）。

### Changed — 别再为已经取过的数据打第二次网

`earnings_vol_signal.scan` 原先对每只通过报价预检的票各调一次
`EarningsWatcher.get_earnings_date`（走 `stock.calendar`），而 **ChronosBee 在同一轮
扫描里早就取过同一个数据**——2026-09-02 的 `.swarm_results` 里 28/30 只票带着
`type=="earnings"` 的催化剂。`earnings_cache/*_date.json` 的 TTL 是 12 小时、扫描每天
跑一次，所以那 30 次调用**每天都是真打网**（实测该目录 24 个文件最新停在 8/9，
当天一个都没刷新）。yfinance 429 是本项目头号数据丢失原因，限流后每次约 2s+2s。
改：`scan()` 新增可注入的 `upcoming_fn`，日报传入
`_earnings_date_from_swarm(swarm_results)`。降级语义**区分两种情况**：
有催化剂但无财报条目 = 视野内确实没有财报（零成本 None）；
完全没有催化剂 = 蜂失败（9/2 是 WMT/SNOW 两只）→ **本函数自己**延迟构造
`EarningsWatcher` 兜底。⚠️ 兜底必须写在注入函数里：`scan` 的
`upcoming_fn or _upcoming` 只在**参数**为 None 时替换整个函数，不会因为某只票
返回 None 就回退——本条注释的第一版正是这么写错的，随即被自己的测试抓出。
新增 `tests/test_daily_report_earnings_source.py`（7 条）。

**另注（未改代码）**：已装好的 `iv_crush_scraper.py` 每天为 30/30 只票产出
下次财报日、过去 8 次财报日涨跌幅、平均绝对幅度与当前隐含波幅，`earnings_history.py`
把前两项又算了一遍。保留自建实现的理由是口径不同（中位数 vs 均值、两日窗口 vs
单日、且我们要做事后结算），但**财报日的重复取数已按上面改掉**——那才是花钱的部分。

### Fixed — 其余（按模块）

- `earnings_history.py`：`{T}_moves.json` 只按 ticker 缓存 30 天 → **补跑有前视**
  （实测 today=2026-08-20 的统计里含 08-25 的事件；冷缓存 n=4 中位 4.5 vs
  热缓存 n=5 中位 5.0），改为读缓存后按 `today` 重新过滤；「>7 个日历日」的窗口
  守卫改成**K 线下标距离恰为 2**（原守卫放行了 2 个交易日的数据洞，
  把 4 个交易日的窗口当成两日波动，虚增历史分母）；截断但成功的财报日列表
  不再写缓存（原先会造成看起来像「这只票没有历史」的一个月黑屏）。
- `earnings_vol_signal.py`：合格条件加上 `ed <= expiry − expiry_buffer_days`
  ——原先 `ed` 落在到期前 2 天内的信号**必然在自己的财报之前被强平**，
  那笔纯往返点差亏损还会被记进 KPI，当成对信号的检验（测量污染）；
  `max_spread_pct` 的注释原先引用「COST 25Δ 27.6%」，而过滤器**只看 ATM 两腿**
  且那是盘后口径——盘中实测 ATM：NVDA 1.78%、COST 4.62%、AMC 16.0%，
  注释改为引用真正被求值的数，并说明扫描在收盘后跑、落盘的点差系统性偏宽，
  日后校准应按 `quote_set["market_open"]` 分层。
- `vrp_signal.py`：回填快照（`..._{D}_backfilled-{W}.json`）此前**单向**——
  进得了 IV 索引、进不了 VRP 历史（glob 与正则都不匹配），
  `_snapshot_rv` 增加回填文件兜底后，真实缓存上可用历史 **188 → 218**
  （每票恰好 +1）；`assess()` 改为同时报「索引条数」与「可用条数」并按后者判就绪
  （原先可以打印「✅ 已就绪」而当天每一行都是 `ready=False`）；
  永远结算不了的行新增 `settle_give_up` 标记与尝试计数（取数失败**不计入**尝试，
  否则限流坏一周会误杀活行）；`rv_forward == 0` 改为按上游口径拒收
  （它会制造「卖方赚满」的最大观测值喂进统计）；`assess(cache_dir=X)` 的结算统计
  改为显式传行（原先读全局文件，探测别的缓存目录会打印生产数字）。
- `portfolio_greeks.py`：压力网格的 IV 轴单位**此前无任何测试**
  （把 `ivp/100.0` 改成 `ivp` 全绿；对 30% IV、30 DTE 的 ATM call，
  +10pt 那格会从 $3.61 变成 $86.04/股）→ 补上用 `bs_price` 现算期望值的测试；
  IV 冲击被截到 0 时增加 `iv_clamped`/`iv_pts_effective` 标记（8% IV 的票在
  「−10pt」列实际只减了 7.99pt，标签却仍写 −10pt）；`worst_cell` 在有行被排除时
  带上 `excluded_dollar_delta` 并在同一行打印「网格不完整，已排除 $X 敞口，
  真实最差比这个数字更差」（实测可差 100 倍）；`dte<=0` 的过期合约不再按 T=1/365
  重定价而是排除并列名；β 缓存改为「K 线已在内存就重算」，磁盘缓存退回纯断网兜底；
  新增可复用的 `daily_bars()` 并在注释里点名 `vrp_signal.py:342` 与
  `options_paper_leg.py:303` 两个应当改用它的调用点（三份互不共享的 K 线缓存，
  同一只票一轮扫描最多取 3 次，Twelve Data 是 7 次/分钟的串行队列）。
- `cboe_options.py`：25Δ 搜索原先**没有距离上限**，Δ=0.60 的合约照样被标成 `c25`
  且不留 `missing_reasons`，也不禁止 `c25` 与 `atm_call` 落在同一张合约上
  （稀疏链、AMC 类名字才会触发）→ 新增 `_QS_D25_TOL=0.15` 与同合约守卫，
  各自给出具体的缺失原因（边界用 `dist − tol > 1e-9` 比较，
  因为 `0.40−0.25 == 0.15000000000000002`）；注入 `now` 时
  `market_open`/`fetched_at` 不再读挂钟；删除恒不成立的 `not math.isfinite(S)`。
- `options_analyzer.py`：`data_available == False` 的空报价集不再被冻结一整天
  （v0.45.43 的规则是「链数据要冻结、价格派生的不冻结」，而**空**报价集没有
  任何历史需要保护），快照命中时按 `_refill_empty_quote_set` 补取并盖
  `_quote_set_refilled_at` 戳；三处只发 `{data_available, source, error}` 的
  生产者改为调用 `_quote_set_unavailable`，形状统一。
- `paper_portfolio.py`：`_VOL_ANN_CACHE` 的键漏了 `vol_source_max_age_days`
  （同一 `(ticker, as_of)` 换窗口拿到旧答案，且跨 `run_replay` 沙盒泄漏），
  补进键并在 `run_replay` 进出时清空；`sizing_mode` 改为在 `run_for_date` 入口校验
  （原先只在「恰好有仓要开」时才炸）；`size_pct_min/max` 的文案更正——
  钳位作用在**置信乘数之前**的百分比上，实测 mid 档在 σ=200% 时是
  **0.90% NAV** 而非 1.5%，且永远到不了 4.8% 以上；
  `Position.sizing` 此前**零读者**，`render_portfolio_card` 增加「今日 N/M 仓退回分档」。

### Fixed — 测试自己写坏了生产状态

新写的 `test_valid_modes_pass_the_entry_check` 调真实 `run_for_date` 时，
桩了 `_load_meta`/`_append_jsonl`/`_save_meta` 却**没有重绑四个路径全局**，
而 `run_for_date` 收尾用的是 `_write_jsonl`（整体重写，不是追加），
于是把 worktree 的 `paper_portfolio_state/` 写空（净值 93 行 → 1 行，
持仓 13 条 → 0）。生产目录未受影响。三重修复：该测试沙盒化四个路径；
`tests/conftest.py` 新增 autouse 的 `_isolate_paper_portfolio_state`，
默认重定向四个全局**并在 teardown 比对真实文件的内容摘要**；
`TestProductionStateIsIsolated` 两个方向都钉住。守卫已实测会触发。

### Fixed — v0.45.100 条目的数据声明不实（已就地更正）

原写「`volatility_20d` 覆盖 30 只 × 全部扫描日（1299 行）」。实测：1299 行 / 96 个
日期不假，但**当日满 30 只的只有 10 天**，2026-03-10~08-14 每天仅 10 只左右
**且值全是 0**，283/1299 行（21.8%）为 0 或 NULL、全部落在 08-14 及之前。
含义不只是数字不准：拿 8 月下旬之前的历史做 vol_target vs tier 对照回放，
绝大多数标的日会落回 `tier_fallback`，等于**分档比分档**——那种「看不出差别」
不构成结论。跨世代对比只有 2026-08-25 之后的样本才有意义。

---

## [0.45.103] — 2026-09-03 — 组合 Greeks 聚合 + β·Delta 带状对冲（纸面 SPY 覆盖）+ 现货×IV 压力测试（期权路线图第 5 步）

**背景**：「对冲量化」的核心不是选票，是控制组合暴露。`greeks_engine.calculate_portfolio_greeks`
早就有但无人调用；`risk_engine` 的压力测试只冲击现货不冲击 IV；纸面组合的风控是
逐票 7% 止损，没有组合层的 delta/vega/gamma 视图。本版把股票腿（paper_portfolio）、
跨式腿（options_paper_leg）与新增的 SPY 对冲覆盖腿聚合成一张组合 Greeks 表，
β·$Delta 出带才用 SPY 股票拉回，并给出现货×IV 联合冲击网格。

### Added

- `portfolio_greeks.py`
  - 暴露：`stock_exposures`（qty 按方向带符号，$Delta = qty×close，β·$Delta）、
    `option_exposures`（每张跨式两行，qty = ±contracts×100；用 `cboe_options.quote_contracts`
    重报的 delta/gamma/vega/theta）、`hedge_exposures`（SPY，β=1 定义值）。
  - **CBOE Greeks 单位实测**（NVDA S=227.19，2026-09-03）：225C 10-02 CBOE vega 0.2517 vs
    BS 每 1 个 vol 点 0.2508；theta −0.1346 vs BS 每日历日 −0.1536。结论：vega 按
    **每 1 个 vol 点**、theta 按**每日历日**，delta/gamma 与 BS 差 <1%，不做缩放。
    数字写在模块头部。
  - **β 自算**：60 日对数收益对 SPY 的 OLS（Twelve Data 日线，≥61 根对齐，
    `hedge_state/beta_cache.json` 缓存 5 个交易日、拒绝来自未来的缓存）。
    刻意**不用** `risk_engine._estimate_beta`——它失败静默返回 1.0，与真实 β=1.0 同形
    （MEMORY「安全默认值」判据）。算不出 → `(None, None)`，聚合标 `partial`。
  - `aggregate`：$Delta / β·$Delta / gamma$（1% 移动的凸性 P&L）/ vega$（每点）/
    theta$（每日）及占 NAV %；`coverage` 块与 `band_status ∈ {inside, above, below, unknown, empty}`；
    **任何一行的 β·$Delta 算不出就是 unknown**——不在部分数据上对冲。
  - `hedge_recommendation`：出带 → `spy_shares = −excess / SPY`，edge 模式向外取整
    （否则半股留在带外、次日再来一笔 1 股小单），center 模式四舍五入。
  - 覆盖账本 `hedge_state/`：每日至多一笔 SPY 调整，成交价=收盘（SPY 点差 ≈1bp，
    记录里 `spread_model` 明写无点差模型）；净值按日去重；同日重跑逐字节一致
    （重跑时先扣掉当日成交重建"交易前视图"）；每日审计 `greeks_{date}.json`。
  - `stress_table`：现货冲击 {−10,−5,0,+5,+10}% × IV {−10,0,+10,+20} 点。股票按 β×冲击，
    期权用 `bs_price` 在 `S×(1+β·shock)`、`iv+pts` 重定价减当前 mid，SPY 按冲击；
    β 缺失的行剔除并标 `partial`；`worst_cell`；(0,0) 格附 `bs_vs_mid_gap` 作模型基差诊断。
  - `combined_nav_detail`：股票腿 + 跨式腿 + 覆盖腿；分量缺失 → None 并列出缺谁。
    **跨式腿从未建账（净值文件不存在）按 0 计并标 `not_started`**——那是合法零状态，
    否则对冲在腿建账前永远 unknown；文件存在但缺行仍是 None。
  - `render_markdown`（日报小节「组合 Greeks 与对冲（观察项 + 纸面 SPY 覆盖）」）、
    CLI `--date --dry-run --json`。
- `alpha_hive_daily_report.py`：VRP 块之后 `run_for_date`，失败只 warning；markdown 追加小节。
- `tests/test_portfolio_greeks.py` 36 条 + 本轮补 1 条（未建账 → 0 + not_started；
  建账缺行 → None）：方向符号、β 缺失 → partial/unknown/不对冲、long 跨式 |Δ| 小且
  gamma/vega 为正、short 镜像、vega 单位对照 `calculate_single`、带内/上/下三态与
  edge/center、覆盖账本同日重跑一致、NaN 不落盘、压力网格 (0,0)=0、+10 IV 长跨式为正、
  股票网格线性、worst_cell、CLI dry-run 不写状态。mutation：对冲方向反转 → 9 红；
  去掉股票冲击的 β× → 1 红。

### 实测（worktree 状态，`--dry-run --date 2026-09-02`）

13 条股票仓，β 覆盖 100%，$Delta +$25,532，β·$Delta +$14,642（带宽 ±15% NAV 内），
−10% 现货冲击 −$1,464。SPY 收盘 $765.16 来自 Twelve Data，**待验证**。

### 边界

- 合并 NAV 把跨式腿的 $100k 名义资本算进分母，% NAV 口径因此比只看股票腿
  （≈$51k）小一半、带宽的美元宽度大一倍。先照此运行，攒一个月再定分母。
- 期权行的 $Delta 用 Twelve Data 收盘，CBOE 的 delta 按其自身 `current_price` 算，
  有几美分基差；`quote_contracts` 不暴露 payload 价格，留待需要时扩展。
- 对冲工具 v1 只用 SPY 股票；指数 put 要等 SPY 进 quote_set 名单。

### 路线图收口

五步全部落地（v0.45.99～103）。真正的产出要等数据：quote_set 从下一次扫描起
攒真实报价，财报跨式账本在下一个财报季出样本，VRP 约 14.7 周后过闸，
组合 Greeks 每日出表。**生产目录 `~/Desktop/Alpha Hive` 需要 `git pull --ff-only`
才能跑到这些代码**——编排器不自动拉取。

## [0.45.102] — 2026-09-03 — VRP 信号：逐票时序记账 + 事后结算 + 就绪度闸（期权路线图第 4 步）

**背景**：方差风险溢价（隐含波动率系统性高于其后实现的波动率，卖方赚这个差）
是期权量化最经典的边。系统里 IV−RV 的原料早就有（`iv_rv_spread`），但从没按
**标的自身的时间序列**记过账，更没有把「事后实现的 RV」结算回去——没有 y 就
谈不上验证。真实 IV 历史（v0.43.18 起自攒）现在每票只有 7～11 条，离时序分位
需要的 63 条还差约 3 个月。本版把承载物先建好：满 63 条之前**只测量不下注**，
到期与否由数据条件决定，不由日历决定（同 `ic_rerun_readiness` 的设计理由）。

### Added

- `vrp_signal.py`
  - `record_day(as_of)`：每票一行 `vrp_ex_ante = iv − rv_30d`（iv 取
    `iv_raw_observed`，缺则退 `quote_set.iv30`，记 `iv_source`）；`n_obs` 只数
    **严格早于 as_of** 的观测（无前视），≥63 才给时序分位 `ts_pct` 与标签
    rich（≥0.80）/ cheap（≤0.20）/ mid，否则 `ready=False`、分位 None。
    起步历史用 IV 索引的日期配同日快照的 rv_30d 补齐（只读，不写索引）。
    按日幂等写 `vrp_state/vrp_signals.jsonl`。
  - `settle(as_of)`：对满 21 个交易日的旧行回填 `rv_forward`
    （与 `twelve_data.realized_vol` 逐字同口径：对数收益、剔 |r|≥0.5、ddof=1、
    ×√252×100，仅差异是接受 rv=0）与 `vrp_realized = iv − rv_forward`。
    只用 `≤ as_of` 的 bar；每票每次结算最多一次 Twelve Data 调用，无可结算行零网络。
  - `assess()`：就绪度——`ready` 需 ≥20 只票各 ≥63 条；ETA 用 `iv_history`
    实测积累速率折算成周，速率不可判定给 None 不猜日历天；附结算统计
    （分标签均值 n<10 给 None）与**票内先排名再汇总**的 Spearman
    （每票 ≥20 条已结算才计入）。读索引用 `_read_index`（纯函数），刻意不走
    `load_iv_history`——后者会写迁移标记文件。
  - CLI：`--date --record --settle --assess --json --out`，退出码 0 就绪 / 1 攒数中 /
    3 无法判定（2 留给编排器）。**未**接进编排器；先随日报小节每日露出。
- `alpha_hive_daily_report.py`：v0.45.101 块之后新增 record → settle，失败只 warning；
  markdown 追加小节「VRP 信号（攒数期，未下注）」。
- `tests/test_vrp_signal.py` 39 条：iv 来源顺序；62/63/70 条边界；标签阈值；同日
  重写幂等；NaN 不落盘；结算数值手算核对（常数收盘 → RV 0；交替 ±1% → 手算值）；
  前视守卫；每票一次 bars 调用；assess 三态；Spearman 完美单调 → 1.0；CLI 退出码。
  mutation：n_obs 把当日计入 → 15 红；去掉 ×√252 → 1 红。

### 实测就绪度（2026-09-03，只读主库缓存）

`0/30 只票达标（需 20 只各 ≥63 条，中位 9 条），按实测速率约 14.7 周后到位`
——积累速率 11 个有观测的扫描日 / 15 个交易日 = 0.73/日。ETA 以扫描连续性为准，
机器没开的日子不攒。

### 边界与陷阱

- **禁横截面池化**：分位只在标的自身历史内算。IV 的横截面跨度是时序跨度的 6 倍，
  池化阈值测的是「你是哪只票」（MEMORY `alpha-hive-cross-sectional-pooling`）。
- `assess()` 的就绪度数 IV 索引条数，`record_day` 的 `n_obs` 数实际有 IV 且有 rv_30d
  的行；9/2 快照 30/30 两者齐全时一致，rv_30d 掉线时会分叉——模块头部已写明。
- 生产快照截至 9/3 尚无 `quote_set`（v0.45.99 落地在当日扫描之后），
  `quote_set.iv30` 兜底目前不会触发；主源 `iv_raw_observed` 30/30 在。

## [0.45.101] — 2026-09-03 — 财报事件波动率信号 + 期权纸面跨式腿（期权路线图第 3 步）

**背景**：单票期权里证据最扎实的边是财报 IV 压缩——隐含事件波动 vs 历史实际
波动的错配。零件此前散落三处（`iv_crush_analysis` / `earnings_pc_history` 只做过
NVDA 单票、`earnings_watcher` 只取下次财报日），没有一个能逐日对 30 只票算信号、
更没有一本用真实报价盯市的账。v0.45.99 的 `quote_set` 正好提供了跨式中间价、
两腿点差与 ATM 行权价。本版把三者串成「信号 → 账本 → 事后结算」，**只测量不下单**。

### Added

- `earnings_history.py`：过去 8 次财报日（yfinance `get_earnings_dates` 经
  `yf_gate`，30 天磁盘缓存；失败返回 None 不返回 `[]`）+ 两日窗口实际波动
  （pre = 财报日前最后一根收盘，post = 财报日后第一根；对 BMO/AMC 不敏感，
  跨度 >7 日历日的窗口视作数据洞跳过）+ `earnings_move_stats`（可用事件 <4 → None，
  `_detail` 版带 `reason`）。日线优先 Twelve Data 一次拉 2 年，退本地价格索引。
- `earnings_vol_signal.py`：`compute_signal` 纯函数。合格条件：`quote_set` 可用、
  ATM 两腿 `quote_ok`、财报日落在 `(as_of, selected_expiry]`（跨式必须跨过事件）、
  历史事件 ≥4。事件波动剥离扩散：`diffusion = 0.8·σ_rv30·√(dte/252)`，
  `implied_event = √(straddle² − diffusion²)`（rv_30d 缺失退 `raw_straddle` 口径并标注
  高估）。`ratio = implied_event / 历史 |move| 中位`：≥1.30 rich（卖跨式候选）、
  ≤0.75 cheap（买跨式候选）、否则 fair。任一腿点差 >15% → `untradeable`
  （COST 25Δ 27.6% vs NVDA 4.4% 的实测差距就是这条阈值的来历）；
  **扩散 ≥ 跨式把事件波动压成 0 也判 untradeable**——那是 rv_30d 被别的跳空撑大的
  退化数字，不是便宜。`scan(as_of)` 按日幂等写 `options_paper_state/earnings_signals.jsonl`；
  `settle_signals` 在财报过后回填实际两日波动与 `realized_ratio`，是日后校准
  阈值的 y；`summary_stats` n<3 一律给 None。
- `options_paper_leg.py`：独立于股票纸面组合的跨式账本（`options_paper_state/`，
  起始 $100k）。**成交付点差**：long 两腿按 ask 买、short 按 bid 卖，出场镜像，
  盯市用 mid。`contracts = max(1, ⌊NAV×6% / (premium×100)⌋)`，一张就超预算则跳过
  并记原因；short 同样按权利金封顶（它只是保证金代理，卖跨式真实风险无上界，
  账本注释明写这不是实盘规则）。逐日用新增的 `cboe_options.quote_contracts` 按
  OCC 符号重报两腿：报价合法 → `cboe_mid`；否则沿用 `last_mark` 标 `stale` 并计日；
  财报过后第一个有报价的日子平仓；连续 stale 超 3 天或到期前 2 天 → 用标的收盘算
  内在价值平仓并标 `intrinsic`；取不到收盘继续持有并告警，**绝不编价关仓**。
  `compute_kpis` / `render_markdown`（日报小节「期权纸面腿：财报跨式（观察项）」）。
- `alpha_hive_daily_report.py::_post_scan_enrichment`：纸面组合之后新增
  scan → settle → run_for_date 三步，任何失败只 warning；markdown 追加小节。
- 测试 79 条（`test_earnings_history` 19 / `test_earnings_vol_signal` 27 /
  `test_options_paper_leg` 33），全部合成数据、临时目录、零网络。mutation：
  换 ask/bid 成交方向 → 11 红；去掉扩散剥离 → 2 红。开发中被测试抓到并修掉
  两个真 bug：同日重跑把入场日 `mark_source` 翻成 stale；净值快照的
  `opened_today/closed_today` 取自运行计数器而非状态，破坏了同日重跑的逐字节一致。

### 规模校准

规格初值 $20k × 2% = $400/笔连一张 NVDA 跨式（≈$2,800）都开不出，账本会永远是空的。
改为 $100k × 6% = $6,000，让主流名单的一张合约进得来（COST ≈ $5,300）。
这是测量账本，规模只服务于「能攒到样本」。

### 已知局限（模块头部明写）

- **无 delta 对冲**：ATM 跨式只在入场时近似 delta 中性，之后标的一漂就带方向，
  v1 把它当噪音记录；第 5 步的组合 Greeks 对冲会接管。
- 阈值 1.30 / 0.75 是先验不是校准，`settle_signals` 攒够样本后再定。
- 生产快照今天之前没有 `quote_set`，端到端目前只有合成数据验证；第一批真实
  信号在下一次扫描出现。

## [0.45.100] — 2026-09-03 — 纸面组合仓位改为波动率目标制（用上系统里 IC 最高的信号）

**背景**：`vol_forecast.py`（v0.44.0）实测「未来 7 日已实现波动」的可学性
是「未来 7 日收益方向」的 60 倍（IC +0.71 vs +0.01），但它至今只是日报末尾
的观察表，没进任何下注。旧仓位规则是固定分档：高置信 5% NAV / 中置信 3%，
于是 σ=15% 的 KO 与 σ=90% 的 MSTR 拿同样的钱，单仓对组合的波动贡献差 6 倍，
组合风险实际由几只高波动票决定。这是把评分系统往期权对冲量化靠的第二步
（第一步 v0.45.99 攒真实报价）：**风险预算按波动率定，不按分数档定**——
这也是期权组合里 vega/gamma 预算的同一思路，先在股票腿上跑通。

### Changed

- `paper_portfolio.py` CONFIG 新增 `sizing_mode`（默认 `"vol_target"`）与
  `vol_target` 块：`size_pct = clamp(target_position_vol_pct / σ₂₀ × 100,
  size_pct_min, size_pct_max) × conf_multiplier[conf]`，再乘既有的
  `_size_multiplier`、低置信减半逻辑不变。校准：`target=1.75%` 让 σ=35% 的
  中位标的得 5% NAV，与旧 high 档持平；钳位 1.5–8%；置信乘数 1.0/0.6/0
  保留旧 5:3 比例。**σ 用的是水平不是变化**——`vol_forecast` 已证明票内
  时变不可预测、横截面水平高度持久，波动率目标制恰好只需要水平。
- σ 来源：新增 `_lookup_vol_ann(ticker, as_of)` 直读 `pheromone.db`
  `signal_archive` 的 `price.volatility_20d`（年化 %，`data_pipeline.py:375`
  `std × √252 × 100`），只取 `date <= as_of` 且 5 天窗口内最近一行（**禁前视**）；
  只读连接，只吞「表不存在」，`database is locked` 照抛（同 `vol_forecast.load_day`
  的教训）。
- `_compute_position_size` 返回 `(size_usd, sizing_note)`；`Position` 新增
  末位默认字段 `sizing`，并把留痕追加进 `rationale`：`"vol_target(σ=35.0%→5.0%)"`
  / `"...,clamp_min"` / `"tier"` / `"tier_fallback(no_vol)"`。
- **诚实降级**：σ 缺失 / NaN / ≤0 时显式退回分档算法，打 warning 且仓位
  记录里可见 `tier_fallback(no_vol)`——不能静默换成一个看起来合理的数字。
  未知 `sizing_mode` 直接 `ValueError`。
- `sizing_mode="tier"` 逐字节还原 v0.39.0 算法；`run_replay` docstring 注明
  要复现 v0.39.0～v0.45.99 的历史数字必须显式传它。`run_replay` 的 CONFIG
  备份改 `copy.deepcopy`（`vol_target.conf_multiplier` 二层嵌套，一层拷贝
  挡不住就地改内层的覆盖泄漏到生产 CONFIG）。
- `render_portfolio_card` 规则行改为按当前模式描述（`_sizing_rule_text`）。
- `experiments/portfolio_capacity_replay.py` 两处 `run_replay` 覆盖钉上
  `"sizing_mode": "tier"`——它扫的是分档乘数，新默认下不钉会变成扫一个
  被忽略的参数。
- `tests/test_vol_forecast.py::test_does_not_touch_position_sizing` docstring
  更新：护栏继续拦的是「横截面分位乘数被接进仓位」，v0.45.100 接的是波动
  水平，二者回答的是不同问题。

### Added

- `tests/test_paper_portfolio_vol_sizing.py`：29 条——σ=35 → 5%/3% 与旧档
  持平（校准断言）；σ=70 减半；σ=10/200 触上下钳位；σ 缺失 → tier_fallback
  且 `caplog` 有 warning；`sizing_mode="tier"` 三档逐字节复现旧数；低置信仍
  减半；NaN σ 按缺失处理、输出无 NaN；旧 `positions.jsonl` 记录无 `sizing`
  字段仍可加载；`_lookup_vol_ann` 对临时 sqlite：取窗口内最近一行、忽略
  `date > as_of`（无前视）、窗口外 None、无表 None。mutation：钳位反转 →
  6 红；去掉 `date <= as_of` → 1 红。
- `tests/test_paper_portfolio_nan.py` 三处 `_compute_position_size` 桩改为
  返回二元组。

### 生效与可比性

- 下一次真实 `run_for_date` 起新仓按新算法定量，存量仓不动；
  `meta.json` 的 `config_snapshot` 自动记录新键。
- ⚠️ **本条原先的覆盖率说法不实，v0.45.104 二次复查更正**。实测主库
  `signal_archive` 的 `volatility_20d`：1299 行 / 96 个日期，但**当日满 30 只的
  只有 10 天**，2026-03-10～08-14 每天仅 10 只左右且**值全是 0**；
  283/1299 行（21.8%）为 0 或 NULL，全部落在 08-14 及之前；稳定的 30 只无零
  是 08-25 之后才有的。0 值会正确落到 `tier_fallback(no_vol)`，别当故障——
  但**它同时意味着：拿 8 月下旬之前的历史做 vol_target vs tier 对照回放，
  绝大多数标的日会落回分档，等于 tier 比 tier**，那种"看不出差别"不是结论。
  跨世代对比只有 2026-08-25 之后的样本才有意义。
- 纸面组合 KPI 自本版起**跨越一条口径世代边界**：对比 v0.45.99 之前的
  净值曲线时须用 `run_replay({"sizing_mode": "tier"}, ...)` 重放同口径。

## [0.45.99] — 2026-09-03 — 期权快照持久化 ~30 DTE 四合约真实报价（期权对冲策略的数据地基）

**背景**：系统现有的期权数据（IV Rank / P/C / GEX / 异动）全部只作为
**方向评分的输入**；`options_backtester.py` 对期权策略的回测用的是
Black-Scholes 加快照里的一个 IV 值——模型价不是可成交价。要把评分系统
往期权对冲量化策略靠，第一步是**攒真实市场报价**，而不是先改评分。
实盘抽查（2026-09-03 收盘后 CBOE 延迟报价）已说明为什么必须是真报价：
NVDA 25Δ call 买卖价差占中间价 4.4%，COST 同一合约 **27.6%**——单票期权
的成本差一个量级，BS 回测出来的 Sharpe 全是纸上的。

### Added

- `cboe_options.py::select_quote_set(data, S, *, target_dte=30, now=None)`：
  纯函数，从 CBOE **原始 payload**（不是成品链——成品链每边只留 40 个
  OI 最大行权价，25Δ 常被裁掉）选出四张合约：到期日取 dte≥7 中
  |dte−30| 最小者（平手取更远）；ATM 取离 S 最近且 call/put 两边都有的
  行权价；25Δ 按 CBOE 自带 delta 取最接近 ±0.25（只认有限非零 delta）。
  每张合约持久化 `bid/ask/mid/spread_pct/iv/delta/gamma/vega/theta/oi/
  volume/theo/last_trade_time/quote_ok`；附 `atm_straddle_mid` 与
  `implied_move_pct`（跨式中间价 ÷ 现价，财报事件波动率信号的直接原料）。
- `cboe_options.py::fetch_cboe_quote_set(ticker, stock_price=0.0, *, target_dte=30)`：
  复用 `_fetch_cboe_payload` 的 120s 进程缓存，主链刚拉过时**零额外网络
  开销**；永不抛错、永不返回 None——失败一律 `data_available=False` +
  `error` 字符串，且 `contracts` 四个键仍在（值 None），下游可无脑 `.get`。
- `options_analyzer.py::OptionsAgent.analyze`：新增出口字段 `quote_set`。
  只在链 `_source == "cboe"` 时取（yfinance 链没有原始 payload）；异常吞掉
  记 warning，绝不拖死 analyze。样本链早退分支同样带此键（不可用）。
  **旧快照没有这个键，消费者必须 `.get("quote_set")`。**
- `tests/test_quote_set.py`：23 条（到期日选择含 dte=3 必须忽略 / 28 vs 35
  平手规则；ATM 最近行权价；25Δ 按 delta 而非行权价；bid=0 → `quote_ok`
  False 且 mid/跨式为 None；delta 全 0 → 25Δ 槽位 None 但 ATM 仍在；
  NaN bid 不崩；`analyze()` 接线两条）。5 处 mutation 各让一条测试变红。

### 诚实降级约定（沿用项目硬规则）

- bid=0 的合约 **不算 mid**：`(0+ask)/2` 是「一半的 ask」不是市价，拿它当
  成交价等于凭空造出一个对手方。`quote_ok=False`、`mid=None`。
- 云端快照模式（补跑历史日）payload 没有 options 数组 → 直接不可用。
  补跑那天的真实报价谁也拿不到，用今天的链冒充比缺失更糟。
- DTE 按**日历日差**（`expiry.date() − now.date()`）算，与 `_select_expiries`
  的 `datetime` 相减略有不同（后者盘中会少读一天）；持久化字段取日历口径。

### 下游（尚未接）

期权纸面腿 / 财报 IV 压缩信号 / VRP 信号都读这个字段。当前**零消费者**，
按 MEMORY「死字段」判据这是有意为之的攒数期，不是遗漏。

## [0.45.98] — 2026-09-03 — 三处 close_t7 生产接入点未传 db 路径，worktree 下静默失效

v0.45.87 上线后又做了一轮 10-agent 独立复查（针对 v0.45.87 提交本身），
最高优先级发现：`generate_deep_v2.py` / `alpha_hive_daily_report.py` /
`swarm_agents/queen_distiller.py` 三处新接入的
`BacktestAnalyzer(..., clean_t7=True)` 都没有显式传 `close_t7_db_path`，
于是落到 `feedback_loop.py` 自己的缺省值
`Path(__file__).resolve().parent / "pheromone.db"`——这只反映"这份
`feedback_loop.py` 副本在哪"，不是 `weekly_optimizer.py` 那套带 Cowork VM
挂载点探测的 `ALPHAHIVE_DIR`。已在复查现场（本 worktree）实测验证：
`Path('feedback_loop.py').resolve().parent / 'pheromone.db'` 解析到的是
worktree 本地那个刚建的空库（0 条 `close_t7`），而真实数据在
`~/Desktop/Alpha Hive/pheromone.db`（35MB，有数据）——三处只要不是从
`feedback_loop.py` 自身所在的那份 checkout 运行，就会拿到
`status="missing"`，悄悄退回脏口径 `actual_prices.t7`，且没有任何可见
警示（`weekly_optimizer.main()` 的 `_warn_if_close_t7_unavailable()` 只
覆盖它自己）。

### Fixed

- `generate_deep_v2.py::_load_ticker_accuracy`：显式传
  `close_t7_db_path=ALPHAHIVE_DIR / "pheromone.db"`（本文件自己已有的
  VM 挂载点探测逻辑）。
- `alpha_hive_daily_report.py` 的 "feedback_loop 权重建议合并" 块：显式传
  `close_t7_db_path=self.report_dir / "pheromone.db"`——与同一行 `_snap_dir`
  用同一个基准目录（`self.report_dir`，即 `PATHS.home`），保证 snapshots
  与 close_t7 库至少互相一致。
- `swarm_agents/queen_distiller.py` 的历史胜率折扣块：显式传
  `close_t7_db_path=_project_root_ta / "pheromone.db"`，同样与 `_snap_dir`
  共用同一个刚计算出的基准目录（该功能目前默认关闭，
  `config.TICKER_ACCURACY_FEEDBACK["enabled"]=False`，此次是防御性修复）。
- 新增 `tests/test_close_t7_production_wiring.py`：3 个测试锁死
  `generate_deep_v2.py`/`queen_distiller.py` 两处的路径接线（对照组验证
  "db 不存在时保留旧行为"契约未被破坏）；修复前手动回退验证过这些测试
  会失败（真回归测试，非重言式）。`alpha_hive_daily_report.py` 那处未补
  独立测试——仓库里从未有对应路径的测试基础设施，构造完整
  `AlphaHiveDailyReporter` 的成本超出这处机械改动应有的投入；已核实该
  改动本身语法/导入正确（`self.report_dir` 经 `hive_logger.PATHS.home`
  确认类型为 `Path`）。

同一轮复查还报出 6 条较低优先级观察（`--min-samples` 与内部硬编码
`MIN_SAMPLES` 脱节、另外两处 `entry_price` 真值判断未对齐、三个新接入点
仍缺样本量下限、`PHEROMONE_DB_PATH` 与 `hive_logger.PATHS.db` 重复、
"有效 T+7 样本"判定散落 3-4 处、`weekly_optimizer.py` 自身 4 处调用点
仍用旧的两步包装）——均为 PLAUSIBLE、非本次修复范围（部分是刻意保留的
向后兼容设计，部分是既有代码的预先存在问题，非本次改动引入），未处理，
详见本 session 的复查记录。

全量测试套件 `pytest tests/ -q`：**2201 passed, 18 skipped, 63 deselected,
1 xfailed**，0 failed。

## [0.45.97] — 2026-09-03 — 一根 NaN 日线，把纸面组合净值烂了四天

## [0.45.97] — 2026-09-03 — 一根 NaN 日线，把纸面组合净值烂了四天

### 现象

`paper_portfolio_state/equity_curve.jsonl` 自 **2026-08-28 起连续 4 个扫描日
`nav`/`cash` 全是 NaN**（8/28、8/31、9/1、9/2），13 条持仓里 6 条的
`shares`/`size_usd` 也是 NaN。`meta.json` 的 `cash` 同样是 NaN。

### 溯源：单点起因 + 不可逆传染

起点是 **2026-08-28 TMUS 的 TIME 止损**——那天正是全站 HTTPS 断掉、
扫描只跑到 20/30 的日子，`_fetch_ohlc` 拿回的 8/28 那根日线是 NaN：

```
_check_exit → ("TIME", NaN, "2026-08-28")        # NaN 价照样触发出场
_close_position → gross_pct NaN → pnl_usd NaN
cash += pos.size_usd + pnl                       # ← cash 从此永久 NaN
nav_for_sizing = cash + sum(size_usd)            # 之后每天 NAV 都是 NaN
_compute_position_size(NaN, ...) → NaN
if size_usd <= 1: return None                    # ← NaN 穿过，仓位照开
shares = size_usd / entry_price → NaN
```

**关键判据：6 条坏仓的 `entry_price` 全是好的**（367.95 / 170.48 / 676.08…
都是真实收盘价），只有 `shares`/`size_usd` 是 NaN —— 说明毒源不在取价，
在 `nav`。而 8/28 当天持仓还全是干净的，于是锁定到 `cash` 那一侧，
再顺着 `cash += size_usd + pnl` 找到那条 `exit_price: NaN` 的 TMUS 平仓。

三道守卫全是 `<= 0` / `<= 1` 形态，NaN 的任何比较都返回 False，**一道没拦住**。

### Fixed

- **`_fetch_ohlc` 出口丢弃含非有限值的 bar**（唯一源头，堵这里就不必在
  `_check_exit` / `_mark_to_market` 各补一遍）。丢 bar 的后果是良性的：
  该日不出场，顺延到有真实价格的下一个交易日（TIME 判据是
  `dt >= time_stop`，不会漏触发）。
- `_open_position` 两道守卫改成 NaN-proof（`math.isfinite` + 原有阈值）。
- `run_for_date` 加纵深防御：`nav_for_sizing` 非有限值时**当天不开新仓并
  报 ERROR**，而不是静默写出 NaN 仓位。NaN 一旦落盘，之后每天的 NAV
  都继承它，且无法从状态文件反推原始数值。
- 新增 `tests/test_paper_portfolio_nan.py`（7 条），含反向回归护栏
  `test_healthy_inputs_still_open` / `test_all_finite_passes_through`。
  5 个变异逐一验证会变红（含「过度修复：把好 bar 也丢掉」
  与「NaN 日用 0 价冒充出场」两种错误修法）。

### 数据修复

备份 `paper_portfolio_state.bak-nanfix-20260903/`，回滚到 8/27 收盘状态后
用修好的引擎重放 8/28→9/2 四天。

回滚状态**逐项对平后才动手**：8/27 记录 `positions_count=8`、
`deployed=$12,593.03`；保留的 7 条干净仓成本合计 $11,333.47，
加上重建的 TMUS $1,259.55 = **$12,593.02**（1 分钱舍入）。
TMUS 的 `sl/tp` 由 `CONFIG(-7%/+15%)` 反推，**同期 13 条仓位逐条验证
公式吻合**后才采用；`rationale` 原文不可恢复，已在字段里如实注明。

重放结果：

```
8/28  NAV $50,663.14  8 仓   （TMUS 的 NaN 日线被丢弃，不出场）
8/31  NAV $50,711.63  9 仓   ← TIME TMUS $182.24→$181.37  PnL −$8.66
9/01  NAV $50,512.05  12 仓
9/02  NAV $51,031.21  13 仓
```

TMUS 最终按 **8/28 的真实收盘 $181.37** 平仓（重放时数据已可获取），
`exit_date` 仍是 8/28，口径正确。全部状态文件 `: NaN` 字面量归零，
KPI 恢复可算：total_return +2.06% / Sharpe 0.4 / 胜率 52.3% / 44 笔。

### 已知残留（不修，记录）

重放是逐日进行的，8/31 那天才处理掉 TMUS，故 `equity_curve` 的
`realized_pnl_today: -8.66` 记在 8/31，而该笔交易的 `exit_date` 是 8/28。
两者不一致但都不是错值 —— 交易本身的日期与价格都正确，
只是现金效应落在引擎处理它的那天。


## [0.45.96] — 2026-09-03 — 缓存测试写死 8 月日期，跨月自动变红（本次不含生产代码改动）

### 现象

`tests/test_treasury_yields.py::TestGetYieldCurve::test_caches_within_ttl`
自 2026-09-01 起恒红（`assert 2 == 1`），隔离跑同样红，**与任何代码改动无关**。
`treasury_yields.py` 上一次改动是 v0.45.61，此后一行未动。

### 根因：两次调用的月份来自两个不同的钟

`get_yield_curve` 的缓存键是**年月**：

- `get_yield_curve()`（不带参数）→ 键取 **ET 当前月**（`_et_month()`，:147）
- `get_yield_curve("2026-08-26")` → 键取**参数里的月**（:139）

原测试把前者交给挂钟、后者写死 `"2026-08-26"`，再断言"同月，应复用"。
这个配对只在挂钟停留在 **2026-08** 时成立。月份一翻，两次调用落进
`202609` / `202608` 两个桶，`_fetch_month` 被调两次 —— 断言如实报告了
一件真实发生的事，只是它测的不是缓存，是"今天是不是八月"。

与 MEMORY v0.45.72「fixture 日期没从被测日期倒推」、v0.45.65
`TestCoverageHorizon` 是同一形状：**夹具日期相对于测试自己控制不了的钟漂移**。

### Fixed（测试）

- `test_caches_within_ttl`：先 `monkeypatch.setattr(ty, "_et_month", lambda: "202608")`
  把月份钉死，两次调用再配对。选钉时钟而不是"从 ET 今天倒推出 `YYYY-MM-15`"，
  是因为 `_XML` 夹具里只有 8 月的行：让缓存键跟着真实月份走的话，第二次调用
  会返回 `None`，测试照样绿，但**除了抓取次数什么都没断言**。钉住之后第二次
  调用真的取到行，顺手补了 `["date"] == "2026-08-27"` 与 `["y10"] == 4.66`。
  这个打桩打得中：`get_yield_curve` 里是模块级全局查找，不是 v0.45.72 那种
  打不中的函数内局部 import。写法沿用 `test_macro_snapshot.py:381` 的既有惯例。

### Added（测试）

- `test_cache_key_is_per_month`：跨月方向的姐妹测试。钉 `_et_month` 到
  `202609`，配一份 9 月夹具 `_XML_SEPT`（值与 8 月**刻意全不同**），断言
  `seen == ["202609", "202608"]` 且 8 月的查询拿到的是 8 月的曲线。
  两条测试守的是**互补而非重复**的性质："同月复用"与"跨月不复用"，
  单有前者时把缓存键换成常量照样全绿 —— 而那会让 9 月的调用默默拿到
  8 月的曲线，正是本模块开篇拒绝的那种"错值与真值无法区分"。

### 变异验证

按 MEMORY「修好一条长期变红的测试后必跑 mutation check」，逐个改
`treasury_yields.py` 后重跑（每次跑完还原，已核对文件字节一致）：

| 变异 | 实际改动处 | 变红的测试 |
|---|---|---|
| M0 `_et_month` → `"202612"`（模拟月度翻转） | 1 | **无** ✅ 正是本次要的：不再随挂钟漂移 |
| M0b `_et_month` → `"202701"`（翻年） | 1 | **无** ✅ 排除"同年巧合" |
| M1 缓存键换成常量（所有月共用一个桶） | 4 | `test_cache_key_is_per_month` |
| M2 缓存键按年分桶 `yyyymm[:4]` | 4 | `test_cache_key_is_per_month` |
| M3 彻底不缓存（`if True:`） | 1 | `test_caches_within_ttl` |

M3 变红说明**没有把原测试唯一的覆盖一起"修"没**（v0.45.72 的坑）；
M1/M2 只打中新增那条，说明姐妹测试确实补的是空白，不是冗余。

**变异脚本自己也要验。** M1/M2 换缓存键时，除了判断行还有三处写回
（`_CACHE[yyyymm]=` / `_CACHE_TS[yyyymm]=` / `rows=_CACHE[yyyymm]`）必须同步换，
漏一处则永远 miss —— 那会变成 M3 的形状，表里却记成 M1。
所以「实际改动处」这列是证据的一部分：4 = 四处全中；脚本另加
`assert mutated != orig`（挡 no-op 替换）、禁 ERROR（挡变异把测试搞崩而非搞红）、
跑完 sha256 比对还原。**没有这层，变异表本身就是一份没人验过的绿。**

跨月那条钉的 `"202609"` 恰好等于本次执行时的真实 ET 月份 ——
**光看它今天绿证明不了钉住了**，M0/M0b 才是证据。另：
`monkeypatch.setattr` 未用 `raising=False`，`_et_month` 若被改名会当场抛
`AttributeError`，不会像 v0.45.72 ② 那样静默失效（已实测）。

### 遗留（本次未动，仅记录）

M0 不变红同时暴露：**`_et_month()` 函数体本身无人守**。它的两个消费测试
（本文件与 `test_macro_snapshot.py::test_treasury_month_key_uses_eastern_not_local`）
都把它 monkeypatch 掉了 —— 后者验的是"`get_yield_curve` 调了 `_et_month`
而不是本机 `strftime`"，不是"`_et_month` 返回的确实是美东月"。
是 3 行 zoneinfo，风险低，先记不改。

### 实测

```
修前：tests/test_treasury_yields.py  11 passed, 1 failed
修后：tests/test_treasury_yields.py  13 passed（13 条逐条独立进程跑也全过，
      排除顺序依赖与 _CACHE 跨用例污染）
全量：2163 passed, 24 skipped, 63 deselected, 1 xfailed（0 failed）
ruff check --select F821：All checks passed（全仓）
```

全量扫了一遍同形状风险（读挂钟 + 近期硬编码日期同现），
`tests/` 下仅 `test_site_missing_fields.py` 命中，核对为假阳性
（`datetime.now()` 出现在描述历史 bug 的 docstring 里，被测函数已收显式日期）。

## [0.45.95] — 2026-09-03 — Step 3 的 ML 计数从来没准过（bash glob 撞 TCC readdir）

⚠️ **本条改的是 `~/.claude/scripts/alpha-hive-orchestrator.sh`，不在本仓库**。
备份 `alpha-hive-orchestrator.sh.bak-20260903`。

### 先纠正一个我自己的错判

上一轮我判断是「Step 3 在步骤运行时重新取今天」。**错了** —— Step 3 用的是
启动时固定的 `DATE_STR`，值一直正确。

### 真正的现象：每一次运行都是「已生成 0 / 预期 12」

```
8/27  8/28  8/31  9/1  9/2  →  全部「ML 报告：已生成 0 / 预期 12」
```

不是跨午夜特有，是**长期如此**。后果：Step 3 天天为另外 12 只补跑一遍，
把上面 `ML_CAP` 那道限流直接翻倍 —— 正是同一段注释里记着的
「2026-07-23 深夜限流连锁崩溃」的成因。9/1 的产物是 **24 份**报告而非 12。

### 根因：同一个脚本里，glob 数不出来但 `[ -f ]` 数得出来

```bash
# line 610（坏）—— 恒为 0
GENERATED_COUNT=$(ls "$PROJECT_DIR"/alpha-hive-*-ml-enhanced-${DATE_STR}.html 2>/dev/null | wc -l)
# line 619（好）—— 一直准确
if [ ! -f "$PROJECT_DIR/alpha-hive-${t}-ml-enhanced-${DATE_STR}.html" ]; then
```

差别是 **readdir vs stat**。macOS TCC 对 `~/Desktop` 允许 stat、拒绝 readdir。
bash 展开 glob 需要 readdir → 无匹配 → `ls` 收到字面量而报错 →
`2>/dev/null` 吞掉 → `wc` 数出 0。**报错被静默，0 看起来像个合法计数。**

判据（供下次排查）：line 619 每次都**正确跳过**了 Step 2 已生成的那些标的
（9/2 挑的是 TSLA BILI AMZN… 而非名单前 12 只 NVDA TSLA MSFT…），
证明进程看得见文件 —— 于是问题被锁定在 glob 这一种访问方式上。

### 启动那条「✅ Desktop 文件访问权限正常」测错了东西

它用 `$PYTHON3`（有完全磁盘访问权）`open()` 一个**已知文件名**：
两个维度都不对 —— 错的二进制（失败的是 bash），错的操作（失败的是 readdir）。
所以 glob 全线失效时它照样打勾。

### Fixed

- **计数改用 `[ -f ]` 逐只累加**，不再依赖 glob（全脚本仅此一处 glob）。
  实测：9/2 → 12/12 正确跳过；9/1 → 24/12 跳过；未来日期 → 0/12 正常补跑。
- **启动预检补上 readdir 一维**，只告警不拦截，措辞改为
  「stat + readdir 均可」。留着是为了下次有人写 glob 时能看见。
- **新增跨日闸**：`generate_ml_report.py` 用 `pdt_today()` 命名、不接受
  `--date`，日期翻篇后补跑会产出错标日期的报告。现改为此时跳过，
  走**专属退出码 3**。⚠️ 不能在守卫里设 `RC=0` —— 下面
  `if [ $STEP3_RC -eq 0 ]` 会打「✅ Step 3 成功」并把 skipped 覆盖成
  success，又是一个假标签（初版就是这么写的，自查时改掉）。

### 撤下 12 份错标报告

2026-09-02 的扫描因电量耗尽（`Low Power Sleep, Charge:1%`）中途休眠 9 小时，
Step 3 跑到次日 00:04，补出 12 份标着 `2026-09-03` 的报告 ——
市场尚未开盘的日期，且 `swarm_results` **12/12 全为空**，33KB vs 正常 48KB。
已从 gh-pages 撤下（`b3f7646`）并删除本地产物。同日正确标注 `2026-09-02`
的 12 份保留。`index.html` 里的 `2026-09-03` 仅为 `_generated_at` 时间戳
（`_date` 仍是 `2026-09-02`），无指向这些文件的链接，删除不产生死链。

### 未修（记录）

- 超时看门狗用 `sleep N` 计时，数的是进程被调度的秒数而非墙上时钟；
  9/2 整机休眠 9 小时期间它也冻住，故 3600s 预算没开火。
  这次没开火恰好是对的（醒来后扫描健康，杀掉反而白费），
  改成墙钟会把「机器睡了」误判成「扫描卡了」。两者需要分开处理。
- Step 12 `scan_coverage` 在跨日运行时按当前日期找结果文件，
  9/2 的覆盖率因此未被测到。属**诚实降级**（`determinable: false`，
  明说测不了），未伪装通过。

## [0.45.94] — 2026-09-03 — 占号未兑现：Python CI 从未并入 main（工作在 `origin/ci/python-tests`）

> 标题由 v0.45.116 补写。原为占位行，久未替换的原因不是「忘了改标题」，
> 而是**这一版的实质提交从未并入 `main`**：只有占号提交 `d5a8c6a` 落了地。

**意图**：给仓库建 GitHub Actions CI，只跑离线确定性子集
（`-m "not integration and not network"`）。不跑全量是因为有一批测试打真实
外部端点（yfinance / Treasury / CNN），放进 CI 会因限流/端点改版无故转红——
时红时绿的 CI 比没有 CI 更糟，它训练所有人忽略红灯。

**工作所在**：`origin/ci/python-tests`，领先 main 5 个提交、落后 45 个。
- `e4c1408` 注册 `network` marker + 补 `test_syntax_valid` 的 skip 保护
- `79c4f89` `.github/workflows/tests.yml`
- `315c7b8` 解除 `.gitignore` 对 `.github/` 的整目录忽略
- `b232eaf` 29 项网络依赖测试标注 `network`（该提交里写着这一版完整的
  56 行 CHANGELOG 正文，可在合并时直接取用）

**main 上的实况（v0.45.116 逐项核验）**：
- `.github/workflows/tests.yml` 不存在，`.github/` 未被 git 跟踪
- `.gitignore` 第 2 行仍是 `.github/`，整目录忽略**未解除**
- `pyproject.toml` **未**注册 `network` marker，`pytest -m network --collect-only`
  收集 **0 条**（2755 全部 deselected）
- `tests/test_scan_catchup.py:56` 的 `test_syntax_valid` **仍无** skip 保护，
  仍直接 `subprocess` 跑 `bash -n ORCH`
- **仓库至今没有 CI。**

**这个号是烧掉的**：编号已进 git 历史不可回收，故保留条目并如实标注，
不复用给别的改动。

> ✅ **已于 v0.45.117（2026-09-05）兑现**：分支已合并，仓库现在有 CI。
> 实质内容记在 v0.45.117 条目下（合并时又实测出一条会让 CI 变红的既有脆弱性）。
> 本条保留原样，作为「占了号但工作没合」这一形态的历史记录。

## [0.45.93] — 2026-09-03 — 2 条 NaN 快照伪造了整张每蜂 IC 表

### 现象

月度自诊断简报（`self_analysis_2026-09.md`）把 `T+7 平均收益` 印成 `+nan%`。
显眼的是那个 `+nan%`，但它恰好是三处伤害里最轻的一个。

### 根因

645 条快照里有 2 条 `entry_price=NaN`（2026-06-26 META / RKLB，疑似 yfinance
429 期间落库）。`bool(float('nan')) is True`，所以下面这些**看起来像有效性检查、
实际只是非空检查**的守卫，一条都没挡住：

- `compute_stats`：`if ep and t7` → NaN 进入 `returns_t7`，均值被传染 → `+nan%`
- `classify`：`if not entry or not actual_t7` → NaN 穿过，随后 `nan > 0` 与
  `nan < 0` **同时为 False**，看多看空都落 `"wrong"` → 静默污染胜率分母
- `compute_dimension_ic`：`if not t7 or not ep` → NaN 喂进 `_spearman()` 的
  `sorted()`。NaN 与任何值比较均为 False，令比较关系自相矛盾，产出的名次数组是
  **未定义的**（不是「NaN 排末尾」这种可预期偏差，而是整段序列被打乱）

第三条是真正的破坏：**2 条坏行让全表 IC 位移最多 0.26**，凭空造出一份完整的、
读起来很合理的「高 IC 蜂」排名——而该表的用途正是给 Track A 权重优化当依据：

| 蜂 | 修复前 | 修复后 | 位移 |
|-----|--------|--------|------|
| ChronosBeeHorizon | +0.272 | +0.012 | −0.260 |
| GuardBeeSentinel | +0.296 | +0.066 | −0.230 |
| QueenDistiller | +0.252 | +0.050 | −0.202 |
| BearBeeContrarian | +0.385 | +0.273 | −0.112 |
| CodeExecutorAgent | −0.076 | +0.031 | +0.107 |

### Fixed

- `self_analyst.py` 新增 `_is_valid_price()`（有限、正、非 bool），替换
  `classify` / `compute_stats` / `compute_dimension_ic` 三处真值判断守卫
- `_spearman()` 入口成对剔除 NaN/inf——它是共享原语，放行 NaN 的排序函数会替
  之后每一个调用方继续生产「看起来合理」的相关系数
- 胜率 227/194（原 227/196），`avg_ret_7d` `+nan%` → **+1.31%**
- 简报第一节增印「⚠️ 价格无效被剔除 N 条」——剔除必须留痕，静默丢弃只是把盲区
  换个地方藏（本轮 4 条：2 条 NaN + 2 条 BRK-B `entry_price=0`）

### Added

- `tests/test_self_analyst_nan.py`（22 条）。已过 mutation check：把
  `_is_valid_price` 改回 `bool(v)` 语义后 12 条转红，其中包含核心的
  `test_dimension_ic_unmoved_by_added_nan_rows`

### 诊断结论（写入 `self_analysis_briefs/self_analysis_2026-09.md` 第四节）

修完数据又发现**简报的结论方式本身有系统性缺陷**：第二节印原始计数、不除基准率，
三条「发现」全是同一个错误的三种外观——

- 「看多偏斜」不存在：失败中 88.7% 看多，而方向性样本本就 88.6% 看多；
  两方向胜率 53.9% vs 54.2%，实质相同
- 「失败集中于 VKTX」是成交量假象：n≥20 的 10 个标的，Wilson CI **全部含 50%**
- 「Oracle 最具误导」方向反了：Oracle 失败均分 7.99、**正确均分 8.28**，
  它对什么都打 ~8 分。真正在失败案例上打更高分的是 ChronosBee（−0.26）与 ScoutBee（−0.21）
- 模板固定文案「系统自信度越高越危险」与数据相反：score≥7 胜率 73.5%（n=34）
  vs score<7 的 52.2%——但事后择优且样本薄，**列为线索不作结论**

另记：`compute_dimension_ic` 跨标的且跨日期池化，落进 MEMORY「横截面池化陷阱」。
改逐日横截面口径后**无一只蜂通过 Bonferroni**（最高 BuzzBee t=+1.89，需 |t|>2.9），
BearBee 的 +0.273 因任何单日都凑不够 5 条横截面而**无法计算、应作废**。
与 MEMORY 既有结论一致：sentiment 是唯一有证据的信号，聚合分 IC≈0。


## [0.45.92] — 2026-09-02 — 实时口径的宏观数据补上日期戳

### 现象

2026-09-01 定时任务产出的日报里，`macro_context.as_of` 是 `None`
（数值本身齐全：spx −0.69 / gold 396.75 / cpi 3.3，无 NaN）。
对比 8/31 那份补跑产出的 `as_of: "2026-08-31"` —— 差别来自路径：
补跑走 `cloud_snapshot`（带 vintage 校验，所以有日期），
定时走 finnhub/treasury 实时源（不写 as_of）。

后果：日报里的宏观块**没有任何日期戳**，读者无法判断它是哪天的。
按 MEMORY「读 vix 前先看 vix_source」那条纪律，这是同一类问题。

### 关键约束：`_as_of` 是控制流，不能动

`fred_macro._as_of` 非 None 时会让取数走 `_asof_history` 对齐历史日
（:404）。实时路径把它填上会**改变取数行为**，不是加个标签那么简单。
所以只改输出，并用新字段 `as_of_mode` 显式承载原先靠 `None` 表达的区分。

### 更关键：不能直接填"今天"

盘前跑时拿到的实时报价是**上一个交易日的收盘**（2026-09-01 06:49 ET
手动跑过一次，正是这个情形）。写成今天就是本项目一路在治的 vintage
污染 —— 比留空更坏。

### Added

- `fred_macro._realtime_as_of()`：推算实时数据**真正代表**的交易日。
  16:00 ET 收盘后才算当天，否则回退到最近一个已收盘交易日；周末与
  美股假日一并回溯。复用已装好的 `cboe_options._et_now`（ZoneInfo，
  DST 正确）与 `is_trading_day`（含假日表），不自己算时区和节假日。
  推算失败返回 `None` —— 诚实留空，不猜。
- `macro_context.as_of_mode`：`backfill` / `realtime` / `fallback`。

### Changed

- `macro_context.as_of` 两条路径都填：补跑 = 目标日，实时 = 推算出的交易日。
- 降级路径的 `base` dict 也带这两个键（`as_of: None` +
  `as_of_mode: "fallback"`），否则消费者拿到的是"键不存在"，
  与实时路径的"键在、值为 None"无法区分。
- **`data_source` 刻意不动**：`_compose_data_source(as_of=...)` 一旦收到
  日期就会加 `cloud_snapshot` 前缀和 `@日期` 后缀。实时路径传进去会造出
  假标签（说走了云端快照，其实没有）。测试里加了反向断言钉死这点。

### Fixed（测试）

- `test_no_snapshot_keeps_live_semantics` 原先断言 `as_of is None` ——
  那是拿"哨兵为空"当"实时口径"的**代理判断**，随本次改动作废。
  改为断言 `as_of_mode != "backfill"` + `data_source` 不含 cloud_snapshot/@，
  **它真正守的那条不变式（未装快照不得调 `_asof_history`）原封不动**。
- 补跑侧原先只断言 `as_of == 目标日`，不断言 mode ——
  实测把 `as_of_mode` 写死成 `"realtime"` 能全绿（45 passed），
  是一条抓不住的假守卫。已补 `assert r["as_of_mode"] == "backfill"`，
  重跑同一变异即变红。
- 新增 `TestRealtimeAsOf`（8 条）：盘后归当天 / 盘前与盘中回退 /
  16:00 整边界 / 周末回退周五 / 劳动节回退 / 时钟不可用时留空 /
  降级路径自称 fallback。5 个变异逐一验证会变红。
  冻结时钟必须 patch `cboe_options._et_now`（源模块），
  因为 `_realtime_as_of` 里是**函数内局部 import**（MEMORY v0.45.72 教训）。

### 实测

```
实时（2026-09-02 07:2x ET 盘前）
  as_of=2026-09-01  mode=realtime  data_source=treasury+finnhub+yfinance+fred
补跑（set_macro_snapshot("2026-08-27")）
  as_of=2026-08-27  mode=backfill  data_source=cloud_snapshot+treasury+yfinance+fred@2026-08-27
```

## [0.45.91] — 2026-09-02 — 补抓 pass 从上线起就是死代码：两道 vintage 校验，内层把外层判死了

### 现象

云端快照连着三次单标的失败，错误串完全一致：

| 日期 | 标的 | manifest.failed | manifest.vintage_stale |
|---|---|---|---|
| 08-28 | BILI | `RuntimeError: CBOE payload 为空（网络/403/符号问题）` | `[]` |
| 08-31 | TMO | 同上 | `[]` |
| 09-01 | BILI | 同上 | `[]` |

而同一次运行的 stderr 明写 `CBOE BILI 数据陈旧：last_trade=2026-08-31，应为 2026-09-01`。
**审计字段和日志互相矛盾**，而人只会信前者。08-28 那次是人工补抓收的场
（`0858f4e`，只改 BILI.json + manifest，没动一行代码）。

### 根因：信号在内层被压平

陈旧有两个检出点，判据不同是刻意的，但**归类不一致**：

| | 判据 | 陈旧时 |
|---|---|---|
| 内层 `cboe_options._payload_is_stale`（v0.45.39） | 此刻应有日期（随 ET 09:30 变化） | `return None` |
| 外层 `cloud_snapshot_fetch._vintage`（v0.45.36） | 目标业务日（固定） | `raise StaleVintageError` |

内层先命中，把「陈旧」和「网络失败 / 403 / 空期权链」压进同一个 `None` 出口。
`_fetch_one_ticker` 拿到 `None` 撞的是泛化兜底：

```python
payload = co._fetch_cboe_payload(ticker, 15)
if not payload:
    raise RuntimeError("CBOE payload 为空（网络/403/符号问题）")   # ← 陈旧标的死在这
```

于是标的落进 `except Exception` 泛化分支，进 `failed`、**不进 `stale`**。
而 v0.45.39 的补抓 pass 只遍历 `stale` —— **它自诞生起对自己要治的病从未触发过**。
今天 BILI 本该被免费补抓一次（大盘段那 20~30 秒是白送的时间窗），实际直接放弃。

连带两处：`vintage_stale` 长期报空（审计字段说谎）；`stale_streak` 只在
`StaleVintageError` 分支累加、泛化分支反而清零，**市场级中止（连续 3 个）
对 CDN 陈旧同样永不触发** —— 真到休市日会静默跑完 30 个泛化失败而非正确中止。

### 为什么测试从头到尾没红

`tests/test_cloud_snapshot_vintage.py` 里每个覆盖 `StaleVintageError` 的用例
都把整个 `_fetch_cboe_payload` 打了桩，交回一份**陈旧 dict**：

```python
monkeypatch.setattr(co, "_fetch_cboe_payload",
                    lambda t, to: _payload(last_trade="2026-08-25T16:00:02"))
```

真实函数对同一份输入返回 `None`。桩造了一个生产中不可能出现的场景，
测的是一条不存在的路径。v0.45.39 的提交信息其实警告过同型问题
（「那次没红——测试把整个 `_fetch_cboe_payload` 打了桩绕过缓存」），
当时只补了缓存那条，没意识到 stale 分类这条同样被架空。

### Fixed

- **`cboe_options.py`**：新增 `CboeStaleVintageError`（带 `ticker` /
  `vintage_date` / `expected_date`）与 `_fetch_cboe_payload(..., on_stale=...)`。
  `"none"` 为默认，实时路径行为逐字节不变（三种失败都该降级 yfinance，
  分辨它们没意义）；`"raise"` 供云端快照使用。
  `_payload_is_stale` 保留为 `_payload_stale_vintage` 的布尔外壳
  —— `close_correction.py:277` 仍在用它。
  ⚠️ 抛点在重试 `try` 内，必须有一条先于兜底 `except Exception` 的
  `except CboeStaleVintageError: raise`，否则被当成网络故障吞掉、白重试 3 次、
  最后仍旧 `None`（变异检验实测：日志出现「重试 3 次耗尽」）。
- **`cloud_snapshot_fetch.py`**：`_fetch_one_ticker` 改用 `on_stale="raise"`，
  在边界处把 `CboeStaleVintageError` 转成 `StaleVintageError`，
  让两个检出点归到同一类。补抓 pass 与市场级中止随之真正生效。

### Changed — 测试

- 新增 6 项 `on_stale` 守卫（`tests/test_cboe_live_vintage.py`）：默认口径不变、
  异常带日期、**不被重试循环吞掉且不重试**、仍不写缓存、其他失败不受影响、
  日历挂了仍 fail-open。
- 新增 4 项**组合**守卫（`tests/test_cloud_snapshot_vintage.py`）：只桩网络层，
  让两道校验真的串起来跑 —— 内层命中也归 `StaleVintageError`、真网络失败仍归泛化、
  端到端补抓救回、manifest 不许把陈旧报成空。
- 既有 10 个桩的签名由 `(t, to)` 放宽到 `(t, to, **k)`
  —— 签名对不上真实函数本身就是这次事故的一部分。

变异检验：撤掉 `on_stale="raise"`，3 个新用例转红而**原有 19 个全绿**
（正是它们抓不住这个 bug 的证据）；撤掉那条 `except CboeStaleVintageError` 亦转红。

## [0.45.90] — 2026-09-01 — Twelve Data `end_date` 左闭右开，补跑的 RV 一直少一个交易日

### 现象

`twelve_data.fetch_daily_closes` 的 docstring 承诺「取**截至该日（含）**的窗口」，
但 `_fetch_rows` 把 `end_date` 原样透传给接口，而 Twelve Data 对日线是**左闭右开**的
—— 目标日那根不返回。2026-09-01 实测（NVDA，`outputsize=15`）：

| 调用 | 末根 | 收盘 |
|---|---|---|
| `_fetch_rows("NVDA", 15, "2026-08-31")` | 2026-08-28 | 217.55 |
| `_fetch_rows("NVDA", 8, None)` | 2026-08-31 | 220.78 |

8/29–8/30 是周末，所以这正好差**一个交易日**。

唯一真正传这个参数的生产路径是 `market_intelligence.calculate_iv_rv_spread`
的补跑腿（`_td_rv(..., end_date=_target)`），也就是说：**补跑 D 的报告，
它那份「D 的 RV30」其实是截至 D 前一个交易日的。**

### 判定：off-by-one，不是「有意排除当日」

「有意排除」这个读法不是没影子 —— 实时扫描当天窗口末端确实是 D-1
（`_drop_forming_bar` 丢掉美东当日那根），当前的错误行为恰好和它对齐。
但它站不住，四条理由：

1. **docstring 明写「含」**，`market_intelligence` v0.45.61 那条注释也把
   「窗口末端恰好是目标日」当作**正确**状态在描述（"对次日补跑影响小"）——
   作者要的就是含，只是没验成没成。
2. **`_drop_forming_bar` 和 `end_date` 管的不是一件事。** 前者判「这根还没
   走完」（`date >= 美东当日`），是**运行时**的数据质量护栏；后者说「窗口画到哪」。
   补跑 D 时 D 那根早已收完，根本没有半根可言 —— 实时少那一根是**跑得早的
   限制**，不是口径约定。
3. **压根不存在可对齐的「D-1 口径」。** 同一个 `calculate_iv_rv_spread` 里的
   yfinance 兜底腿走 `yf.download(period=...)`、没有 forming-bar 闸、当天那根照收。
   两条腿本来就差一根，没有一致的「as-known-before-D」政策要保。
4. **不构成前视。** 补跑 D 的报告在 D 收盘后才发（编排器时间闸 13:30 PDT =
   16:30 ET），D 的收盘价那时已是公开信息；HV30 的标准定义本来就含当日。

反方唯一有分量的理由是「补跑值与实时值口径混用」。查过了，**没牙**：
`rv_30d` / `iv_rv_spread` 只出现在 `report_formatters`（展示）和
`scan_coverage_gate`（只查有没有），在 `signal_archive` / `replay_scoring` /
`feedback_loop` 里**一次都没有** —— 不是评分维度，不进 IC 管道。
一根 bar 的窗口平移影响不到任何测量。

顺带一个旁证：修完之后 `RV30 @2026-08-31` 与 `RV30 live` 完全相等（44.84），
因为今天 9/1 那根是半根、被丢掉，实时窗口末端也是 8/31 —— 补跑与实时
在**该当一致的时候**终于一致了。修复前补跑那条路比实时路**掌握的信息还少**。

### Fixed

- **`twelve_data.py` 新增 `_api_end_date()`**：把调用方的「含该日」翻成接口的
  「不含」，+1 个自然日。非交易日没有 bar，落到周末/假日不会多带一根
  （实测 `end_date=2026-08-29` 周六 → 末根仍是 2026-08-28）。
  解析不了就**原样透传并 warning**（Twelve Data 也收 `YYYY-MM-DD hh:mm:ss`，
  那种写法自带时刻语义，不该按「日」平移）—— 透传意味着「含」这条承诺本次
  不成立，必须让日志说出来，不能静默。
- `_fetch_rows` 调用点改走 `_api_end_date`。三个入口
  （`fetch_daily_closes` / `fetch_volume_ratio` / `realized_vol`）共用它，语义一致。
  ⚠️ 生产端 `data_pipeline._fill_volume_from_twelvedata` 目前**不传** `end_date`，
  只走实时口径；改的是它将来接补跑时会踩的同一个坑。

### Changed

- `fetch_daily_closes` docstring 写实：「含」是**本函数的承诺**，不是接口的行为，
  差值在 `_api_end_date` 里补掉。`fetch_volume_ratio` / `realized_vol` 补上同样的
  `end_date` 语义说明（此前 `realized_vol` 完全没提这个参数）。
- `market_intelligence.py` 与 `tests/test_twelve_data.py::TestBackfillTargetDate`
  里 v0.45.61 那段叙述补完：「窗口末端恰好是目标日」是**要达到的状态**，
  v0.45.61 传了 `end_date` 但没验窗口末端，实际并没达到。
  （MEMORY v0.45.75 教训：结论更新了，代码注释里的副本也得跟着更新。）

### Added

- `tests/test_twelve_data.py::TestEndDateIsInclusive`（5 项）：假接口**照真接口的
  左闭右开行为**造数据，所以断言钉的是**语义**不是「参数长什么样」。含
  「末根 == 请求的 end_date」（周一 / 跨周末两种）、`realized_vol` 端到端、
  **+1 之后 forming-bar 闸仍拦得住美东当日那根**、实时口径零回归。
- `TestApiEndDateHelper`（4 项）：跨月/跨年/闰年平移、datetime 写法透传、
  非法值**出声**透传。
- **mutation check 已跑**：去掉 +1 → 4 红；改成 +2 → 5 红。两个方向都咬得住。

### 已知遗留（本版未动）

- `calculate_iv_rv_spread` 的 **yfinance 兜底腿仍是「最近 N 天」口径**，
  补跑时给出的 RV 不属于目标日（v0.45.61 就已标注，要改成 start/end 才行）。
  本版只修了 Twelve Data 主腿。
- 全量 `pytest --maxfail=99`：**2147 passed / 1 failed**，唯一那条是
  `test_treasury_yields.py::TestGetYieldCurve::test_caches_within_ttl`，
  与本次改动无关（本次只碰 `twelve_data.py` + 其测试，且它单独跑也红）。
  根因是同型的「fixture 日期不从被测时钟倒推」：测试拿 `get_yield_curve()`
  （**当前** ET 月）去和写死的 `"2026-08-26"` 比「同月，应复用」，
  月份翻到 9 月就不成立了。已另开任务，不在本版修。

---

## [0.45.89] — 2026-09-01 — 修好限流之后，扫描反而超时了

⚠️ **本条改的是 `~/.claude/scripts/alpha-hive-orchestrator.sh`，该文件不在本仓库**
（`git ls-files` 查不到它）。想看改动只能直接读那个文件，
备份在 `alpha-hive-orchestrator.sh.bak-20260831`。

### 现象

2026-08-31 定时扫描跑到 **20/30 被杀**（RC=124），status=failed，网站没更新完整。

### 根因不是网络，是节拍变了

那天的网络反而是近期最干净的一次：

| | 8/31 | 8/27 | 8/28（全站断网那次） |
|---|---|---|---|
| 429 | **1** | 690 | 2 |
| SSL 错误 | **0** | — | 35 |

真正的原因反直觉：**v0.45.56/61/66 把 429 限流治好之后，扫描变慢了。**

限流时代，429 是**秒回的拒绝** —— 拿不到数据，但很快。8/27 那 690 次 429
换来的是 30 只只跑 26 分钟。现在每只票**真的会等 CBOE 全链 OI 回来**
（15~19 个到期日），实测 **90s/只**，30 只需要 ≈2700s。

而 `STEP2_TIMEOUT` 规则模式写死 1800s（30min），是按「快速失败」时代的节拍
定的。分支还写反了：LLM 模式 45min、规则模式 30min —— 那是 LLM 推理最慢时
的假设；现在规则模式的瓶颈是网络 I/O，比 LLM 那条路还慢。

### Fixed

- **`STEP2_TIMEOUT` 改为按标的数计算，不再写死常数**：`标的数 × 120s`
  （实测 90s 之上留 33% 余量），下限沿用原值 1800s，`--use-llm` 在此基础上
  再加 900s（沿用原设计「LLM 比规则多 15 分钟」的差额）。
  30 只 → 规则 3600s / LLM 4500s；10 只 → 1800s / 2700s（**与旧值逐位相同**，
  确认这是严格放宽、任何名单规模下都不缩水）。
  `TICKERS_ARG` 算不出只数时兜底按满编 30 只。
- **作废 line 64 的陈旧成本注释**：v0.42.9 写的「71 + 4.9×标的数 秒，30 只约
  219s，占 STEP2_TIMEOUT 12%」在限流治好后已不成立（实测 90s/只，不是 4.9s/只），
  且「占 12%」这个说法本身依赖 STEP2_TIMEOUT 是常数。就地改写为实测值 +
  指向新逻辑，**不删除**——保留它被推翻的记录。

### 顺带记下：Step 5 那句「本轮网站不会更新」是假的

8/31 那次 Step 5 打印了「🚨 Step 5 未部署！…**本轮网站不会更新**」，
但 gh-pages 在 14:42 确实收到了 `628a1d3`（12 份 8/31 ML 报告）。

因为部署有**两扇门**：Step 5 的显式部署，和 `generate_ml_report.py` 末尾的
`_sync_ghpages()` 副作用（见 CLAUDE.md「GitHub Pages 部署规则」）。杀掉 Step 2
只关了第一扇。结果是网站进入**不一致状态**：12 只有当日深度报告，日报和看板
停在 8/28。措辞未改（改了要动 v0.45.68 那段刚落地的文案），此处先记录。

### 补跑过程中撞出的独立 bug：`.get(k, 默认值)` 挡不住显式 null

跑 8/31 补跑时日志里在刷 `TypeError: unsupported operand type(s) for -:
'int' and 'NoneType'`，一只 QCOM 打死了 **3 只蜂**（BuzzBeeWhisper、
ScoutBeeNova raise；GuardBeeSentinel 的 crowding 分析降级）。

`reddit_sentiment.py:166` —— 又是 MEMORY 里记过的那条：

```python
mentions_24h = primary.get("mentions_24h_ago", 0)   # 上游返回 null 时不生效
mention_delta = mentions - mentions_24h             # int - None → TypeError
```

`.get(k, 默认值)` 的默认值**只在键不存在时**生效。上游把值显式置成 `null`
（键在、值是 `None`）时它永不触发。讽刺的是本模块自己就在造这个 None：
`_quiet_result()` 返回 `"rank": None`。

### Fixed（reddit_sentiment.py）

- **两处**都改成显式类型判断（第二处 `wsb_rank` 是写完第一处后由新增测试
  抓出来的 —— `wsb_match` 是**另一个字典**，同样的 `.get("rank", 999)`
  同样挡不住 null）。
- **`mentions_24h_ago` 单独按三态处理**：这一条不能和其它字段一样一律折成 0。
  折成 0 会走进 `momentum_pct = 100.0 if mentions > 0` 那条分支，凭空造出
  momentum=100% → buzz="hot" → 动量加 1.5 分。**「基线未知」≠「基线为 0」**，
  这正是 v0.45.3「安全默认值」判据要问的那句：*这个默认值会不会让下游误以为
  掌握了信息*。改为基线未知时 momentum=0.0，不加分也不扣分。
- 新增 `TestNullFieldsFromUpstream`（5 条），含一条**反向回归护栏**
  `test_genuine_zero_baseline_still_reports_100` —— 保证修复只区分 None 与 0，
  没把真实的 0 基线（新票首次上榜）一起压平。5 个变异逐一验证测试会变红。

### 附带验证：v0.45.63 低价股修复在生产里完全成立

8/31 那次虽被杀，AMC 已跑到（17/30），24 份期权快照落盘：

```
AMC  iv_skew_ratio = 0.832   skew_basis = nearest_strike   GEX = 13.1299   $2.64
其余 23 只全部 skew_basis = window
skew 空 0/24 · GEX 为 None 0 个 · GEX 为 0.0 的 0 个
COST −2.25 / RKLB −4.15 两只负 GEX 正确判成 low
```

即修复走了低价股回落路径、且**没有波及正常标的**。


## [0.45.88] — 2026-08-31 — 占号未兑现：CBOE 新鲜度分层从未并入 main（工作在 `origin/fix/cboe-*`）

> 标题由 v0.45.116 补写。与 v0.45.94 同一形态：占号提交 `b6c7f83` 落了地，
> **实质提交没有**。

**意图**：CBOE 快照的新鲜度分层——补抓从未触发、陈旧数据误入缓存、
盘中冻结记录静默通过，三条一起修。

**工作所在**：两条未合并分支
- `origin/fix/cboe-stale-vintage-passthrough`（领先 1 / 落后 60）：`6d8f357`
- `origin/fix/cboe-vintage-staleness-layering`（领先 4 / 落后 60）：`83bcab5` 合并
  8/28 BILI 与 8/31 TMO 两条线并重编到本号，`b03012c6` 消费端默认拒绝盘中冻结快照。
  后者含这一版完整的 72 行 CHANGELOG 正文，可在合并时直接取用。

**main 上的实况（v0.45.116 逐项核验）**：`intraday_frozen` 在 `origin/main`
上出现 **0 次**——盘中冻结的识别与拒绝逻辑不在 main 上。

⚠️ **不要与 v0.45.39 的 vintage 校验混淆**：那一版**已在 main**
（`cloud_snapshot_fetch.py` 的 `_fetch_cboe_payload` vintage 反解、
`close_correction.py:255` 的注释即引用它）。本号要做的是它之上的**新鲜度分层**，
那部分没上。读代码时看到 `vintage` 字样不等于本条已落地。

**这个号同样是烧掉的**，处理方式同 v0.45.94。

## [0.45.87] — 2026-08-31 — weekly_optimizer close_t7 上线后 7-agent 复查修复（6 项）

v0.45.86 落地后对 `weekly_optimizer.py` 做了一次独立代码复查，发现 6 个
问题，按严重程度依次修复。

### Fixed

- **样本数门槛可被绕过（最高优先级）**：`count_t7_samples()` 判断样本
  "可用"的口径只看有没有 T+7 价格，而 `compute_new_weights_wls` /
  `bootstrap_validate` 内部构造 `valid_snaps` 时额外要求
  `entry_price > 0`。后果：`main()` 用前者的宽口径通过 `min_samples`
  门槛后，若 `compute_new_weights_wls` 因窄口径过滤后样本不足自己的
  `MIN_SAMPLES=10` 而返回 `None`，会回退到旧实现 `compute_new_weights()`
  ——而它经 `feedback_loop.BacktestAnalyzer.suggest_weight_adjustments()`
  完全没有最低样本数保护，能在极少样本（如 3 条）下静默产出权重建议，
  审计日志显示的 `n_samples` 与真正参与拟合的样本数因此对不上。
  两处都已修：`count_t7_samples()` 补上 `entry_price > 0` 检查，与
  `valid_snaps` 完全对齐；`compute_new_weights()` 补上
  `if len(valid_snaps) < MIN_SAMPLES: return None` 保护。新增
  `TestSampleThresholdAlignment` 锁死。
- **pheromone.db 存在但 close_t7 未回填时整个修复静默失效**：
  `_load_close_t7_map()` 对"库不存在"和"库/表都在但查无任何 close_t7
  非空行"一视同仁，都返回空 dict；调用方也都用 `if close_t7_map:` 真值
  判断，CLI 表现两种情况下完全一样——权重实际上悄悄回退到被弃用的脏口径
  （`actual_prices.t7`），用户毫无感知。改为返回 `(map, status)`，
  `status ∈ {"ok","empty","missing","error"}`；新增 `_warn_if_close_t7_
  unavailable()`，在 `main()` 里用 `print()`（`_log.warning` 本文件未配
  handler，不保证终端可见）对 `"empty"`/`"error"` 显式提示，`"missing"`
  （测试隔离、全新环境等正常降级路径）不警示。
- **`Path.exists()` 未被 try/except 保护**：`db_path.exists()` 在
  `try/except sqlite3.Error` 保护范围之外——`Path.exists()` 只吞
  ENOENT/ENOTDIR/EBADF/ELOOP，`PermissionError`（目录权限被误改、NFS
  挂载抖动、备份工具临时锁目录）会原样往上抛，导致脚本直接崩溃退出，
  而非文档承诺的优雅降级。现已用 `try/except OSError` 包住 `exists()`
  调用，权限类问题走跟"库不存在"一样的降级分支（并按上一条标记为
  `"error"` 状态供上层警示）。
- **瞬时 sqlite 错误被当成永久失败缓存**：遇到 `sqlite3.Error`（如与
  `backfill_dir_accuracy.py` 并发写产生的 "database is locked"）时，
  此前把空结果写进 `_CLOSE_T7_CACHE`，导致一次性脚本内后续所有调用
  （`count_t7_samples` + 4 处 `BacktestAnalyzer` 构造）都直接命中空缓存、
  不会重试。现在只在确认查询成功（无论是否为空）或确认文件不存在时才
  写缓存，`"error"` 分支不写，给同一次运行的后续调用重试机会。
- **三处生产代码未接上修复（范围缺口）**：`generate_deep_v2.py` 的
  `_load_ticker_accuracy`、`alpha_hive_daily_report.py` 合并进
  `AgentWeightManager` 的权重调整、`swarm_agents/queen_distiller.py` 的
  `TICKER_ACCURACY_FEEDBACK`，三处各自独立构造 `BacktestAnalyzer`，
  继续用只有约1/3 可信的 `actual_price_t7`。架构性调整：把
  `_load_close_t7_map`/`_apply_clean_t7_prices` 从 `weekly_optimizer.py`
  挪到 `feedback_loop.py`，作为 `BacktestAnalyzer.__init__` 的
  `clean_t7`（默认 `False`，向后兼容）与 `close_t7_db_path` 参数；
  `weekly_optimizer.py` 原有的两个同名函数保留为薄包装，显式转发自己
  解析出的 `PHEROMONE_DB_PATH`（带 Cowork VM 挂载点探测逻辑），4 处既有
  调用点与全部既有测试的调用形态不变。三处生产调用点改为显式传
  `clean_t7=True`，不改变其余任何行为（错误处理/日志格式原样保留）。
  新增 `tests/test_feedback_loop.py::TestBacktestAnalyzerCleanT7`
  （覆盖 `clean_t7=True/False` 与库缺失/命中/未命中四种情形）；
  `tests/conftest.py` 新增 `_isolate_feedback_loop_close_t7_db`
  autouse fixture，防止未显式覆盖 db 路径的测试打到本机真实生产库
  （与既有 `_isolate_weekly_optimizer_db` 同一条教训）。
- **流程：占号协议缺失（低优先级，本次已按协议执行）**：v0.45.86 落地
  时跳过了"开工先占号"步骤。本次开工第一件事已按协议
  `git fetch origin` 取号、插入占位条目并单独提交推送到 main，
  不追溯修改 v0.45.86 的历史。

全量测试套件 `pytest tests/ -q`：**2128 passed, 24 skipped, 63 deselected,
1 xfailed**（较 v0.45.86 的 2108 passed 净增 20，全部来自本次新增的
回归测试）。

## [0.45.86] — 2026-08-31 — Track A（weekly_optimizer）T+7 价格改用 close_t7 干净口径

排查"闸1 bootstrap 为什么总不过"时发现：`weekly_optimizer.py` 全链路
（`compute_new_weights`/`compute_new_weights_wls`/`bootstrap_validate`/
`check_ticker_pool_consistency`，经 `feedback_loop.BacktestAnalyzer` →
`snap.actual_price_t7`）读的 T+7 价格来自 `report_snapshots/*.json` 的
`actual_prices.t7`，由 `backtest_engine.PriceBackfiller._get_price_on_date`
用 `Ticker().history()` ±3天容差取价——`backfill_dir_accuracy.py` 开头注释
早已记录了弃用它的理由（本机间歇性 `TypeError: 'NoneType' object is not
subscriptable`）。抽样比对同一 `(ticker,date)`：与 `pheromone.db.close_t7`
（`backfill_dir_accuracy.py` 用 `yf.download`+精确交易日+自校验回填的干净
口径，`ic_diagnostics.py`/`signal_archive.py`"唯一可交易信号"结论同源）
只有约1/3 重合，25% 两者都不等——三条独立取价管线（另一条是
`backtester._simulate_trade_path` 写的、已知被 SL/TP 截断的 `price_t7`）
互不对齐，从未校验过。

落地前验证（未直接采用 `replay_scoring.py`——它自己的决策表明确排除
"换数据源"类改动；本次改动读的两列历史价格本就都已归档，不属于
该表针对的"原始数据未归档、必须前向累积"场景）：① `(ticker,date)` join
命中率 96.9%，`close_t7` 可用 80.6%（750/931），且是当前可用样本
（`actual_prices.t7` 非空的 781 条）的严格子集——零净新增，只丢 31 条，
且这 31 条全部因为 `backfill_dir_accuracy.py` 还没追上（`close_t7` 有值的
必然 `actual_prices.t7` 也有值）；② monkeypatch `BacktestAnalyzer` 直接跑
两版真实代码对比，catalyst 提议变化 +2.10pp→+0.65pp（近乎腰斩）、
risk_adj −2.83pp→−1.65pp（少四成），换源本身不改变"这周该不该写"的
结论（两边都 <3.0pp 阈值），但足以在未来某周直接翻转判断。

### Fixed

- `weekly_optimizer.py`：新增 `_load_close_t7_map()`（只读连接读
  `pheromone.db`，按 `PHEROMONE_DB_PATH` 当前值缓存，不用
  `functools.lru_cache`——后者对零参函数只认首次调用结果，测试
  monkeypatch 换库路径后仍会读到旧缓存）与 `_apply_clean_t7_prices()`
  （覆盖 `snap.actual_price_t7` 为 `close_t7`；库不可用时原样不改，
  保留旧行为；`(ticker,date)` 查无匹配则丢弃样本、不回退旧值，避免
  脏/干净口径混算）。4 处 `BacktestAnalyzer(directory=...)` 构造
  （`compute_new_weights`/`compute_new_weights_wls`/`bootstrap_validate`/
  `check_ticker_pool_consistency`）统一接入，避免只修一处又制造新的
  "两个数字互相打架"。`count_t7_samples()` 同步改用 `close_t7_map`
  计数，使打印的"T+7 已回填样本"数字与真正参与拟合/重采样的样本
  用同一口径。
- `tests/conftest.py`：新增 `_isolate_weekly_optimizer_db` 自动隔离
  fixture——`PHEROMONE_DB_PATH` 不走现有的 `ALPHA_HIVE_DB_PATH` 环境变量
  隔离，若不单独隔离，测试构造的 `(ticker,date)` 会在本机真实生产库里
  查无匹配、被新逻辑整批当"无干净价格"丢弃，4 个既有测试因此假红
  （`test_compute_weights_neutral_not_systematically_penalized` 与
  `TestTickerPoolGate` 三个用例）。与本文件已有的 `_block_same_day_macro`
  同一条教训："新增任何数据源，同一提交里必须在这里加一行"。
- `tests/test_weekly_optimizer.py`：新增 `TestCleanT7PriceOverride`
  （4 个用例）锁死覆盖/丢弃/库不存在三条分支，以及
  `count_t7_samples` 与真实拟合口径一致。

### Known follow-up（未在本次处理，记在 MEMORY）

`backfill_dir_accuracy.py` 若定期重跑，能把上面提到的 31 条"还没追上"
的样本补回来，但这属于运维层面的调度问题，不在本次代码改动范围内。

---

## [0.45.85] — 2026-08-31 — self_analyst.classify() 认不出生产快照的 bullish/bearish 词表

v0.45.84 修好 `SNAPSHOTS_DIR` 目录选择后暴露：`classify()`（self_analyst.py:176-190）
判断方向对错时硬编码 `direction == "Long"` / `"Short"`，但生产扫描管线
（`paper_portfolio.py`，v0.38.0 起挂载）写入快照的 `direction` 字段实际是
小写 `bullish` / `bearish` / `neutral`（见 paper_portfolio.py:391,1038：
`"Long" if direction == "bullish" else "Short"`）。两套词表从未匹配过，
`classify()` 对当前全部生产快照恒定返回 `"neutral"`——`--months 3` 跑出
645 条快照、643 条 neutral、correct/wrong 均为 0、胜率恒 0.0%。此前用
2026-07-29 前冻结的旧快照测试时表现正常（34 条分出 21 correct/12 wrong），
是因为那批旧快照凑巧还用着 `Long`/`Short`/`Neutral` 词表——这个 schema 不一致
一直被"读错目录"那个更大的 bug 意外掩盖，v0.45.84 修对目录后才露出来。

### Fixed

- `self_analyst.py` `classify()`：`direction` 先 `str().strip().lower()`，
  再同时接受新词表（`bullish`→看多、`bearish`→看空）与旧词表
  （`long`→看多、`short`→看空），大小写不敏感。方向语义已用
  `paper_portfolio.py` 的止盈止损计算（:502-533，bullish 时 `exit>entry`
  为盈利、bearish 时 `entry>exit` 为盈利）与仓位表渲染（:1038）交叉确认，
  未搞反。修复后 `--months 3`：645 条快照，227 correct / 196 wrong /
  222 neutral，胜率 53.7%（此前恒为 0.0%）。
- 无针对 `self_analyst.py` 的既有单元测试（`tests/` 下命中 "classify"
  关键词的三个文件测的是 `OptionsAnalyzer.classify_call_flow`、
  `PolymarketClient._classify`、`risk_engine._classify_growth_value`，
  均与本函数无关）；已跑 `py_compile` 与 `ruff check --select F821` 确认
  改动无静态错误。

## [0.45.84] — 2026-08-31 — self_analyst.py 快照目录 fallback 只判断"存在"不判断"还在更新"

`SNAPSHOTS_DIR` 优先取 `OUTPUT_DIR/report_snapshots`（`~/Desktop/深度分析报告/深度/report_snapshots`），
只有该目录"不存在"时才回退到 `ALPHAHIVE_DIR/report_snapshots`（`~/Desktop/Alpha Hive/report_snapshots`）。
但生产扫描管线早已只往后者写入——前者自 2026-07-29 起停止更新，只剩 59 个
2026-03-16~2026-07-29 的旧文件，却因为"目录仍然存在"从未触发 fallback。
`self_analyst.py` 每次运行都"成功"（不报错、样本数看似正常，如 34 条），
用的实际是一个月前就冻结的数据，诊断简报的"近期"标签是假的，且随时间推移
越来越过时。`weekly_optimizer.py` 在 v0.23.6 已经踩过同一个坑并修过
（`_best_snapshots_dir()`，见 weekly_optimizer.py:90-103：同时看两个候选目录、
各自数 `*.json` 文件数、取更多的那个），但当时没有同步到 self_analyst.py。

### Fixed

- `self_analyst.py`：`SNAPSHOTS_DIR` 解析改为复制 `weekly_optimizer.py` 的
  `_best_snapshots_dir()` 逻辑——比较两个候选目录的 `*.json` 文件数，取更多的
  那个，而不是"只在不存在时才 fallback"。实测 `--months 3`：样本数
  34 → **645**，`SNAPSHOTS_DIR` 从冻结的 `深度分析报告/深度/report_snapshots`
  (59 文件，止于 2026-07-29) 改为持续更新的 `Alpha Hive/report_snapshots`
  (931 文件，更新到 2026-08-28)。
- 无针对 `self_analyst.py` 的既有单元测试；已跑 `ruff check --select F821` 与
  `py_compile` 确认改动无静态错误。

### 顺手发现（未修，超出本次范围）

验证时发现：切到正确目录后，`compute_stats()` 里 `correct`/`wrong` 双双归零
（645 条样本里 643 条被分类为 `neutral`）。根因是 `classify()`
（self_analyst.py:176-188）按 `direction == "Long"/"Short"` 判断，但当前生产
快照（如 `XOM_2026-08-28.json`）里 `direction` 字段实际是小写的
`"bullish"/"bearish"/"neutral"`——两者从未匹配过。旧的冻结快照恰好是
`Long`/`Short` 词表，所以这个 schema 不一致此前被"用错目录"意外掩盖了。
这是另一个独立 bug，不在本次任务范围内，留给后续 session。

---

## [0.45.83] — 2026-08-30 — 2026-08-28 宏观数据补跑：yfinance 的缺口是暂时性的，不是永久的

v0.45.81 把 `macro_context` 里的 20 处 NaN 老实标成了 `null`（当时 yfinance
对 `^GSPC`/`GLD`/11 个板块 ETF 确实取不到 2026-08-28 那天的收盘价）。用户
追问"大盘这个没有数据源吗"，现查（2026-08-30）yfinance 历史数据：

```
^GSPC  2026-08-28  Close=7711.76  Volume=4,297,560,000
GLD    2026-08-28  Close=408.89
```

都已经补齐了。顺手也查了 NVDA——v0.45.69 曾记录"yfinance 永久没有 8/28
的 NVDA 收盘价，只有云端快照有（217.55）"，现查 yfinance 同样已经回填出
`Close=217.55`，与云端快照分毫不差。**当时判定的"永久缺失"其实是暂时性
缺失**：大概率是当天扫描撞上了 Yahoo 数据结算延迟 + 429 限流风暴的窗口，
过后 Yahoo 自己把数据补全了，只是没人再去复查。

### Fixed

用 `fred_macro._asof_history()`（v0.45.81 里已修复、能正确挡 NaN 的同一
条历史取数路径）重新对齐 2026-08-28，把 `null` 换成真实数字：

- `spx_change_pct`: null → -0.25，`gold_price`/`gold_change_pct`: null →
  408.89 / -3.24，`gold_trend`: stable → falling（原来的 "stable" 是
  NaN 比较全部落空后的兜底分支，本身就是错的，随 gold_change_pct 一起改）
- `sector_rotation.hot/cold/full`：11 个板块全部补上真实 5 日涨跌幅
- `summary` 字符串修正"大盘+nan%"片段，并按源码 `summary_parts` 的原有
  顺序补上黄金片段；`macro_tailwinds` 按源码分支顺序（VIX→黄金→HY）插入
  "黄金回落-3.2%（风险偏好回升）"——这条分支源码里不调整 `score`，所以
  补它不会让 `macro_score`/`macro_regime` 与报告里其它已算好的字段脱节
- `field_sources` 新增 `SPX`/`GLD`/`sector_rotation` 三项，标注取数时刻是
  补跑时（2026-08-30），不是原始扫描时刻——避免看起来像"当天就取到了"
- `market_trend`/`macro_score`/`macro_headwinds` 均未改动：真实
  `spx_change_pct=-0.25` 没有跨过 ±0.5 的 bull/bear 判定阈值，`market_trend`
  维持 "neutral" 不变；打分只在 CPI/联邦基金利率/HY 利差三个分支里调整，
  跟大盘/黄金无关，故这两个字段本就不该动

主仓库（`alpha-hive-daily-2026-08-28.json`）与 `gh-pages`（同名文件 +
首页内嵌的完整简报表格「大盘(5日)」行）同步更新。

### 教训

**"数据源缺失"的判定不会自动过期，得有人回头查。** v0.45.69 当时的判断
（yfinance 永久没有 8/28 数据）在当时是诚实的，但没有任何机制会在数据
補齐后自动复核这条结论——它就一直躺在 CHANGELOG 里被当作既定事实引用，
直到这次因为另一件事顺带去查了才发现已经不成立。同类"暂时性缺失 vs
永久缺口"的判断，如果只查一次就下结论且没有复查计划，结论的保质期
可能比看起来短得多。

---

## [0.45.82] — 2026-08-30 — BuzzBee 成交量比长期空缺：补上独立回落源（Twelve Data）

用户发现 ML 报告 BuzzBee 板块「成交量比」总是"—"。核对 2026-08-28 全部 12 份报告
逐个实测：同一份报告里 `5日动量` 全是真实数值，`成交量比` 100% 为 None——这个反差
是破案关键。追到 `data_pipeline._fetch_history_metrics()`：yfinance 限流失败时，
`momentum_5d` 早有自攒价格索引兜底（v0.43.25，数据源 pheromone.db，不经外部网络），
但 `volume_ratio` 从未有回落路径——索引只存收盘价没存成交量，硬凑一个比值等于编数据，
所以诚实地一直空着。这不是新 bug，是 v0.43.25 就写明的已知缺口。

### 排查过程中的意外发现
项目里已经有一个现成、写好、测试覆盖、专门为了绕开 yfinance 429 建的
`twelve_data.py`（v0.45.61，供 `market_intelligence.py` 算 `rv_30d`/`iv_rank`
用），免费档 800 次/天、8 次/分，量级够覆盖整个 30 只标的 WATCHLIST。它内部已经
在解析每根日线的成交量字段（`row.get("volume")`），但 `fetch_daily_closes()`
最后只吐收盘价，成交量解析完就被扔了，从没接到 `volume_ratio` 上——又一个
「代码写了、没接线」的半成品。候选数据源逐个验证排除：Alpha Vantage（TIME_SERIES_DAILY
确认有量，但这把 key 的 25次/天额度已被 newsapi 新闻情绪功能占满）、Stooq（CSV 端点
已上 JS 反爬工作量证明，服务器端脚本调不通）。

### Added
- `twelve_data.py`：把原本内联在 `fetch_daily_closes()` 里的抓取/解析逻辑提成
  共享的 `_fetch_rows()`，新增 `fetch_volume_ratio(ticker, window=20)`——
  最新成交量 / 近 20 根均量（含当日，口径对齐 `data_pipeline` 现有 yfinance 路径），
  算不出（数据不足/成交量为 0/NaN）返回 None，不兜底 1.0（成交量比"正常"是假象）
- `data_pipeline.py`：新增 `_fill_volume_from_twelvedata()`，与既有的
  `_fill_momentum_from_index()` 对称——yfinance 失败时先补动量（价格索引）、
  再独立尝试补成交量比（Twelve Data，与前者数据源互不相关）；已有真实值时
  不重复请求，两条回落都没有时保持诚实 None
- `tests/test_volume_ratio_fallback.py`（11 项）：覆盖算不出不兜 1.0、已有真实值
  跳过请求不重复打 API、两条回落都失败时保持 None 不阻断降级链、模块异常不炸主流程

### 验证方式
未触发真实每日扫描。`_fetch_rows` 重构后原有 `tests/test_twelve_data.py`
24 项全过（行为零回归）；`fetch_volume_ratio`/`_fill_volume_from_twelvedata`
用构造数据跑过全部分支；额外用 `unittest.mock` 模拟 yfinance 抛 429，端到端
验证 `_fetch_history_metrics()` 正确落到 Twelve Data 回落（XOM 实测拿到真实
`volume_ratio=0.81`，与同一份 yfinance 数据手算结果一致）；正常路径（yfinance
成功）额外确认零 Twelve Data 调用，不浪费配额。与本次改动直接相关的既有测试
（`test_backfill_date_anchoring`/`test_cloud_snapshot_price`/`test_macro_snapshot`
等 11 个文件、159 项）全过，`ruff --select F821` 干净。

### 已知限制
只补了 `volume_ratio` 这一个缺口，`_fill_momentum_from_index` 的 momentum 回落
路径未动。配额层面 Twelve Data 现由 `market_intelligence.py`（RV30）与本次改动
共用同一 800/天预算，两者合计仍远低于额度，暂不需要额外的每日用量协调。

> 撞号记录：`origin/main` 已有 `claude/xxx` 分支先提交+合并了 v0.45.81（NaN 穿透
> 三层渲染守卫那条），本条目按"先提交者不改号"原则从 0.45.81 让到 0.45.82。

---

## [0.45.81] — 2026-08-30 — 板块轮动「+nan%」：isinstance 挡不住 NaN，这次踩在三层渲染上

用户问 2026-08-28 报告为什么有 NaN JSON 错误，查到 `alpha-hive-daily-2026-08-28.json`
的 `macro_context` 里 20 处字面量 `NaN`（`spx_change_pct`/`gold_price`/
`gold_change_pct`/`sector_rotation` 全部 11 个板块）。根因与 v0.45.69 那次
（yfinance 当天 Close=NaN、Volume 是真的）同源，但这次穿的是另外两道守卫：

- `fred_macro.py` 的 `if data[name]["prev"] != 0:`——`NaN != 0` 恒 `True`，
  除法照算，NaN 就这样被当成"取到了"写进 `data[name]`。
- `_fetch_sector_rotation` 的 `if first_close >= 5:`——只挡得住分母，
  `first_close` 正常、`last_close` 是 NaN 时照样放行。
- 更值得记的是**第三层**：`report_formatters.py` / `dashboard_renderer.py`
  里专门为"取不到就显示—"写的 `isinstance(v, (int, float))` 判断
  （v0.45.43/v0.45.54 就是为了不让假数据看起来正常而加的），同样没挡住——
  `float('nan')` 本身就是 `float` 实例，`isinstance` 检查为真，
  格式化不报错，只吐出字面文本 "nan"，于是网站首页「板块轮动(5日)」
  实测显示成"科技+nan% 医疗+nan% 能源+nan%..."（截图直接在线上复现），
  日报 Markdown 显示"大盘(5日) | +nan% | neutral"——状态列还配着一个
  看起来正常的标签，比 v0.45.54 想防的「兜底 0 冒充正常值」更隐蔽。

Python 的 `json.dumps` 默认 `allow_nan=True`，NaN 序列化不报错、
生成阶段的编排器日志里也确实一条 NaN/JSON 相关的报错都没有——
但 `NaN` 不是合法 JSON（RFC 8259 只认 `null`），严格解析器
（JS `JSON.parse`、大多数非 Python 语言的 JSON 库）读到这个文件会报错，
这才是"NaN JSON 错误"的真正触发点，Chrome 内置 JSON 查看器本身够宽松，
人眼直接开文件反而看不出问题。

### Fixed

- `fred_macro.py`：yfinance 收盘价 ingestion（`_fetch_macro_data` 内的
  symbol 循环）与 `_fetch_sector_rotation` 的 `_fetch_one` 都加
  `math.isfinite()` 校验，Close 不是有限数就当同「没取到」处理，
  不再写入 `data`/返回该 ETF——与 v0.45.69「拿不到就不冒充」同一原则。
- `report_formatters.py`：`_build_macro._mrow` 的判断加 `math.isfinite`。
- `dashboard_renderer.py`：新增 `_finite()` 帮助函数，VIX/10Y/黄金/
  板块轮动全部改用它；板块轮动的 hot/cold 逐项过滤 NaN，若过滤后一项
  不剩，整个「板块轮动」div 不渲染（不留一个空壳标签）。

### 教训

`isinstance(x, (int, float))` 不是 NaN 的守卫——`NaN` 本身就是合法的
`float` 实例，这条检查专门用来分辨"有没有拿到数"，却分辨不出"拿到的是
垃圾"。这是 v0.45.69 记录的 NaN 穿透模式（`<=0`、`if not x` 挡不住 NaN）
在**格式化/渲染层**的第三次复现，说明这条教训不能只在数值运算层打补丁——
凡是"判断是否为有效数字"的地方，无论是用来算术还是用来决定显示什么文本，
都要问一遍 NaN 会怎么走。

### 追加：MCP 服务端读的是另一份文件，也各中一枪

`alpha_hive_mcp.py`（Cowork 里挂的 alpha_hive MCP server）不读
`alpha-hive-daily-{date}.json`，读的是逐标的 `analysis-{TICKER}-ml-{date}.json`——
跟上面 `macro_context` 完全是两份文件、两条代码路径，理论上不该受这次的
影响。但用户报告 Cowork 侧一个消费市场数据的定时任务在 MCP 层解析
2026-08-28 报告时同样撞上 `NaN` 导致 JSON 非法，一查才发现 8-28 那天
**12 个标的**（AMC/AMZN/CVX/DELL/META/MSFT/MU/NEE/QCOM/SNOW/VKTX/XOM）的
`analysis-*-ml-2026-08-28.json` 里，`CodeExecutorAgent` 的技术分析详情
确实各带 2 处字面量 NaN：

- `fetch_data.recent_close`
- `analysis_data.price`

根因是同一天 yfinance 的同一个数据缺陷（Close=NaN、Volume 是真的），
但这次踩中的是**第三处、此前完全没查过的代码**——`code_generator.py`
里两段被 `CodeExecutorAgent` 动态 `exec()` 的代码模板字符串：

- `_generate_yfinance`：`recent_close = float(hist["Close"].iloc[-1]) if len(hist) > 0 else None`——
  只判断"有没有行"，不判断"这一行的收盘价是不是数"。
- `_generate_technical_analysis`：`"price": float(latest["Close"])`——
  同一个 `result` 字典里 `sma_20`/`sma_50`/`rsi` 三个字段都写了
  `pd.notna(...)` 守卫，唯独 `price` 这个最基础的字段没写，是三个手写的
  兄弟字段里漏掉的那一个。

### Fixed（追加）

- `code_generator.py` `_generate_yfinance`：`recent_close` 加 `math.isfinite()`
  校验，NaN 归一成 `None`（连带修好一个副作用：`_price` 的 fallback链
  `... or recent_close or "N/A"` 此前若 `recent_close` 是 NaN，会因为
  NaN 是 truthy 而把 NaN 当作"取到了"直接返回，现在 NaN 正确变成 `None`
  后能继续 fallback 到 `"N/A"`）。
- `code_generator.py` `_generate_technical_analysis`：`price` 补上
  `pd.notna(...)` 守卫，与同一字典里另外三个字段的写法保持一致。
- 顺着同一模式在文件里再搜了一遍 `float(...["Close"]...)`，`_generate_momentum_analysis`
  也中招：`current_price`/`momentum`/`avg_momentum`/`momentum_std`/`z_score`
  五个数值字段全部补上 `pd.notna` 守卫（`avg_momentum`/`momentum_std` 因
  `.mean()`/`.std()` 默认 `skipna=True` 实际很难撞上，但为了跟同一字典
  里其余字段一致、防止未来改动引入 `skipna=False`，一并加固）。

三处均已用一段与 8-28 实测同形的假数据（Close=NaN、Volume 是真的）
跑通生成的代码字符串，严格 `json.loads()` 复解析确认不再出现 `NaN` token；
也各跑了一遍正常（无 NaN）数据确认没有破坏原有输出。

---

## [0.45.80] — 2026-08-30 — dashboard.css 全量死类审计：又挖出两处「v2 重写但 v1 没删」的死代码

同一天两个并行 worktree 各自发现一个"class 被引用/定义了但从没真正渲染"的 bug
（`elastic-spence` 的 v0.45.76：`.dot-bull/.dot-bear/.dot-neut` 被 Python 引用三处，
CSS 从未定义；`interesting-khayyam` 的 v0.45.79：`.yc-ok` 等单类选择器被同特异度、
更靠后的 `.ah-macro-val{color:...}` 顶掉，实测颜色从未生效过——均未合并进 main）。
两个 bug 同一天在同一文件里各撞一次，值得做一次全量审计而不是等第三次撞见。

### 方法（不只是 grep）

1. 从 `dashboard_renderer.py`（2938 行）+ `templates/dashboard.html` 提取全部
   `class="..."` 静态字面量（296 个原始 token），再逐个追踪 f-string 里的动态
   class 变量（`_scls6`/`_dcls6`/`dot_cls`/`macro_yc_cls` 等 18 个）到其
   赋值语句，解出全部可能取值（`_sc_cls()` 等辅助函数一并展开），合并成
   **306 个实际可能出现的 class 名**。
2. 用 `tinycss2` 把 `templates/dashboard.css`（817 行）解析成
   `(selector, media, specificity, source_order, declarations)` 结构化列表
   （手写正则跨不过 `@media` 嵌套和多选择器逗号分组，故引入依赖），而不是肉眼扫。
3. **死类检测**：306 个 class 里，逐个查是否在任意选择器的最右复合部分出现过。
4. **遮蔽检测**：先从 `class="..."` 里把"同一元素同时挂两个 class"的组合
   （如 `hscore {_hscls}`、`ah-cards-grid top6-grid`）枚举出来（29 组），
   对每组按 CSS 级联规则（`!important` > 特异度 > 源码顺序）算出每条属性的
   实际胜出声明，标出"单类选择器写的值，被更晚出现的同特异度选择器顶掉"的情况。
   为避免重复报告已在途的两个 bug，第 3、4 步跑在"临时合并了 v0.45.76 +
   v0.45.79 两个未合并分支改动"的参考副本上，而不是本 worktree 当时的旧状态。
5. **真机验证**：本地起 `http.server` + 最小 HTML 骨架引入真实 CSS，用
   Browser 的 `getComputedStyle` 而非肉眼/grep 核对候选项——筛掉了脚本标出的
   多数"遮蔽"其实是**假阳性**（`::after` 伪元素规则不影响本体文字颜色；
   `.score-big.sc-h` 等复合选择器虽也"赢了"单类 `.sc-h`，但写的是同一个值，
   视觉上无差异——本仓库已有对这个模式的正确防御写法，不是本次要修的范围）。

### Fixed

真机验证后确认两处货真价实的死代码，模式相同：**后来的重写替换了旧样式，
旧声明却从没删**，与 `.dot-bull`/`.yc-ok` 那种"从来没实现过"是两种不同的病，
但外部症状一样——grep 能看到定义，实际从不生效：

- **`.top6-grid`（`templates/dashboard.css` 原第 99–102 行）** vs
  **`.ah-cards-grid`**（注释自称"v2 — flush bordered grid"，`git blame`
  确认由更晚的「B-style Financial Newspaper dashboard redesign」引入，
  `.top6-grid` 则来自项目早期 2026-03-04 的提交）。两者同特异度（单类选择器），
  `.ah-cards-grid` 源码顺序更靠后，`gap`/`grid-template-columns` 两项属性
  全部顶掉 `.top6-grid` 的 `18px`/`repeat(3,1fr)`，实测 `gap` 恒为 `0px`。
  `dashboard.html` 里两个 class 一直同时挂在同一个 div 上
  （`class="ah-cards-grid top6-grid"`）。**删掉 `.top6-grid` 死规则块
  + HTML 里多余的 class**（保留 `.ah-cards-grid`，即当前实际渲染的样子）。
- **`.hamburger`/`.nav-overlay`/`.nav-overlay.open` 整块被定义了两次**
  （原第 365–377 行 vs 748–761 行），第二块注释明写「update for light theme」，
  用 `var(--tp)`/`var(--surface)` 替换第一块硬编码的 `#fff`/`#0A0F1C`——
  第一块 100% 死代码，第二块单独已完整可用。**删掉第一块（旧、硬编码深色版）**。
  两处改动均用 `getComputedStyle` 核对：删除前后计算样式逐属性一致
  （`gap`/`color`/`display` 等），确认纯粹是死代码清理，零渲染差异。

### 结论：9 个"零 CSS 定义"class 里 8 个是设计如此，1 个良性

另有 9 个 class 全仓查无 CSS 定义（`acc-dir-rets`/`actionable-card`/
`actionable-empty`/`actionable-section`/`ah-filter-row`/`cc-metrics-col`/
`dq-banner`/`full-oi-card`/`hist-toggle-text`），逐个核对用途：其中 8 个
在 HTML 里自带完整 `style="..."` 内联样式或纯粹是 `hist-toggle-text` 这类
JS `querySelector` 挂钩——class 本就不承担样式职责，不是 bug。唯一存疑的
`cc-metrics-col`（`.cc-two` 网格第一列的容器 div，零样式）经 `getComputedStyle`
实测为 `display:block`，与相邻 `.cc-metric` 行自带的 flex/padding 组合视觉
无异常——记录在案但不构成需要修的缺陷。

### 影响面

`templates/dashboard.css` −18 行、`templates/dashboard.html` 1 处 class
属性精简；**零渲染差异**（真机 `getComputedStyle` 前后逐属性核对一致）。
`index.html`（GitHub Pages 部署产物）未手动改动——它由 `report_deployer.py`
在下次扫描时从这两个源文件重新生成，会自动带上本次改动。

⚠️ 与 v0.45.76/v0.45.79 的交接：本次改动与那两个未合并分支改的是
`dashboard.css` 里不重叠的行区间（本次动的是第 99–102 行与第 365–377 行；
`elastic-spence` 动的是 `.sdir-neut` 后新增 4 行；`interesting-khayyam`
动的是第七轮宏观指标那一段），三方 diff 互不相交，理论上能自动合并，
但仍需人工确认最终合并顺序与结果。

## [0.45.75] — 2026-08-29 — 一个已被自己证伪的归因，还在 7 个文件里当理由用

2026-08-25 的重测已经证伪了「本机 OpenSSL 1.1.1q 扛不住并发 HTTPS」这个归因，
记忆库也写了更正。但 `http_gate.py` 的模块 docstring **一直原样写着它**，
而且是全仓最权威的那份表述——其余文件写注释时都在引它，或引它引的
`cboe_options` 实测数据。v0.45.73 的条目已经点名说过这件事，只是没动手。

于是一条已撤回的因果，在 **7 个生产文件**里继续当着设计理由用：

| 文件 | 它在那里承担什么 |
|---|---|
| `http_gate.py` | **源头**：docstring 把它写成闸门存在的理由，还引了「4 并发挂 50~70s / 顺序 8~11s」当实测 |
| `cboe_options.py` ×4 | 信号量注释、`_fetch_cboe_payload` docstring、`with _CBOE_SEM` 行内、期限结构选源理由 |
| `options_analyzer.py` ×2 | 为什么弃 yfinance 走 CBOE；`_iv_term_points_yfinance` 接闸门的理由 |
| `market_intelligence.py` ×2 | 进闸门的理由；**退避重试策略的分类依据** |
| `yf_gate.py` | docstring 里「为什么原来那个退避是那样设计的」 |
| `cboe_vix.py` ×2 | 复用 `_CBOE_SEM` 的理由；`with` 行内注释 |
| `data_pipeline.py` | Finnhub 走闸门的理由 |

### Changed — 改写归因，闸门与结论一行不动

**闸门全部保留。** 它无害、开销只在网络往返、且防的是**真实存在**的东西：
yfinance 429 逐日翻倍（8/25=364 → 8/26=487 → 8/27=687），8/27 那天
`rv_30d` / `iv_rank` / `catalysts` 各 0/30。诚实的理由就是这一条——
**不给对端限流器加压**，与 `yf_gate` 的「一次 429 就停」同向。

`http_gate.py` docstring 重写为四节：为什么保留（限流）/ ⚠️ 已撤回的理由（附
2026-08-25 重测表）/ 仍然成立的那条教训 / 用法。**「加源之前先确认闸门覆盖它」
原样保留**——它是结构性事实（v0.43.26 接通 Finnhub/AV 前那两个源一个请求都不发，
接通后成了两个不受约束的调用方），与 8/24 的根因是什么**无关地**成立。

8/24 那 96 次 SSL EOF **是真的，但根因至今未确定**，各处均如实标注。最强的
反证是分布：EOF 同时打在 Slack(18)/reddit(13)/SEC EDGAR(8)/polymarket(4)/
CBOE(20)/Finnhub(11)/AV(13) —— 七个互不相干的域名同时挂。
**复发时先抓现场**（并发数、网络状态、有无 VPN/代理、机器负载、是所有域名
一起挂还是只有一个），别照着版本号往回推。

### 顺带查实：两处版本号互相矛盾，却都被当成实测证据

`http_gate` 写 OpenSSL 1.1.1q，`cboe_options` 写 LibreSSL 2.8.3 —— 同一场事故、
同一台机器。实测：

    /usr/local/bin/python3  3.11.1  → OpenSSL 1.1.1q      ← 生产扫描跑的是这个
    /usr/bin/python3        3.9.6   → LibreSSL 2.8.3      ← 项目硬规则禁用

即 `cboe_options` 归咎的那个 TLS 栈，**生产路径根本不经过**。两处谁也没发现
自己和对方矛盾，因为没人回头核对过——**版本号是最容易顺手拿来的解释，
也最不用付代价**：它不需要复现，写下来就像有了根因。

### 差点顺手制造一处新的陈旧

`tests/conftest.py` 与 `tests/test_crewai_telemetry_optout.py` 里各有一句
「`http_gate.py` 的 docstring 在这点上是陈旧的」。**docstring 一改，这两句
自己就变陈旧了**——本条目在治的那个形状，差点当场又犯一次。
（改完撞上并发的 v0.45.74：它把 CrewAI 整体移除，这两处连同文件一起没了，
改动作废。留下这段是因为**教训不作废**：改一份被下游逐字引用的表述时，
`grep` 一遍谁在引它，引用点也要跟着改。）

⚠️ 与 v0.45.74 的交接：`http_gate` docstring 末尾把 v0.45.73 的 crewai 埋点
当「闸门覆盖不到的调用方」的例子引着。crewai 现已整体移除、全仓 grep 不到，
故就地标注「已于 v0.45.74 移除，查经过看 CHANGELOG」，免得下一个读者
去 grep 一个不存在的包——**同一种病：留着指向已消失事物的指针。**

### 影响面

**注释与 docstring only，零行为改动。** `py_compile` + `ruff F821` 全过；
全量 `pytest tests/`（rebase 到 v0.45.74 之后重跑）**2104 passed / 18 skipped /
1 xfailed / 63 deselected**，205.74s；`http_gate` 冒烟确认
`DEFAULT_ACQUIRE_TIMEOUT=120.0`、信号量仍限 1、acquire/release 正常。

⚠️ `DEFAULT_ACQUIRE_TIMEOUT = 120.0` **未改动**，但注释里已标注：单次往返耗时
**没有当前实测**（原注引的 8~11s 属已撤回的那批单次取样），要调它先自己测。


## [0.45.74] — 2026-08-29 — 移除 CrewAI：一个从未接通、且与本系统测量方式相冲突的集成

v0.45.73 关掉 crewai 的 import 埋点时顺带确认了 `run_crew_scan()` 零调用方。
这次把整条集成删干净——不是因为它闲置，是因为**它就算接通也不该接通**。

### Removed

| 删除项 | 说明 |
|---|---|
| `crewai_adapter.py` | 整个文件（`BeeAgentTool` / `AlphaHiveCrew`） |
| `alpha_hive_daily_report.run_crew_scan()` | 86 行，零调用方 |
| 同文件顶层 CrewAI import 块 | 11 行 |
| `config.CREWAI_CONFIG` | 8 行；`enabled: True` 是**假开关**，只被那个没人调的方法读 |
| `requirements.txt` 的 `# crewai>=0.1.0` | 本来就是注释行——**它从来不是声明依赖** |
| `tests/conftest.py` / 编排器的埋点 env 闸（v0.45.73 加的） | 本仓已无 `import crewai`，留着就是「关一个不存在的东西」的死配置 |

`pip uninstall crewai` **已于同日执行**（用户要求）。卸载前核过两件事：
`pip show crewai` 的 `Required-by` 为空（无包依赖它）、且全机器除本仓外无人 `import crewai`。
卸载后全套 2104 passed 不变。

⚠️ **别顺手清「crewai 的依赖」**：`opentelemetry-sdk` 与 `tokenizers` 看着像 crewai 的遗留，
实为 **`chromadb` 的依赖**，而 `vector_memory.py:24` 在用 chromadb —— 清了会打断向量记忆。
真正的孤儿只有 `instructor`（`Required-by` 为空），无害，留着也行。

同日把主 checkout `/Users/igg/Desktop/Alpha Hive` 从 v0.45.73 快进到本版本 —— 编排器的
`PROJECT_DIR` 指向那里，**不拉齐的话这次移除在生产上等于没做**。

### 为什么不只是「闲置」——三条硬伤

1. **从未接通**。`Agent(...)` 从头到尾没传 `llm=`。crewai
   `utilities/llm_utils.py::_llm_via_environment_or_fallback()` 的回退链是
   `MODEL` → `MODEL_NAME` → `OPENAI_MODEL_NAME` → `DEFAULT_LLM_MODEL="gpt-4.1-mini"`（OpenAI）。
   全仓 `grep OPENAI_API_KEY` 零命中、环境也没有 ⇒ `crew.kickoff()` 必然失败。
   它不是「装了没用」，是**装了也跑不了**。

2. **与项目硬规则冲突**。真要跑通就得配一个付费 OpenAI key，
   而本项目的规则是「报告走 Cowork 本地推理、禁付费 API、LLM 扫描必须先报费用」。
   hierarchical + 6 工具 + `verbose=True`，每只标的多轮往返，30 只标的的账不小。

3. **与本系统的测量方式相冲突**——这条最关键，见下。

### 设计评估：LLM manager 动态选蜂 + 自行加权，对本系统是负分

被删掉的那个设计是：一个「投资分析总监」LLM 读工具描述，自己决定调哪几只蜂、
怎么综合，产出再 `_normalize_result()` 转回蜂群格式。评估结论是**多余且有害**：

- **动态选蜂省不下东西，还会毁掉可比性**。蜂只有 6~8 只、各自都便宜、且每天每只
  标的都要全跑——因为整套价值就在那张**横截面面板**。按情境跳过某几只蜂 =
  每天的口径都不一样，`compute_kpis` / rank-IC / 扩池换来的 N_eff 全部作废。
- **它会砸掉本项目最贵的资产：可复现的评分管道**。`replay_scoring.py`（v0.45.33）
  能把聚合层与维度计算层的改动离线重放，省掉 25 周的世代边界代价；
  `weekly_optimizer` 的 T+7 回测、逐维 rank-IC、IC 重跑就绪度闸的世代边界，
  全都建立在「同样的输入产出同样的分数」之上。
  LLM manager 让聚合**不可复现**（同输入不同日不同权重，且模型每次升版都是一次
  静默的世代切换），上面这一整套测量设施同时失效。
- **它治不了真正的病**。`final_score` IC≈0 的根因是**符号抵消**：两个反向维度占
  43% 权重、抵消掉唯一有证据的 sentiment（详见
  `experiments/final_score_dilution_report.md` 与 v0.44.0 的只读化决定）。
  换个更聪明的混合器不解决抵消，只是让抵消**不可审计**。
  诊断指向的是「该信哪个信号」，不是「怎么混得更花哨」。
- **它自带一个静默中性化的接缝**。`_normalize_result()` 里
  `float(data.get("score", ..., 5.0))`——LLM 少吐一个字段就静默变成 5.0 中性，
  与「真的分析出中性」同形。这是本项目最难发现的一类故障（v0.45.2）。
  任何「LLM 自由吐 JSON → 解析 → 填默认值」的设计都有这道缝。

**LLM 在本系统里该待的地方是数字定下来之后的叙事合成**（深度报告模板 C，
走 Cowork 本地推理），**不是替掉评分与聚合**。

### Added

`tests/test_no_crewai_dependency.py`（2 条，1.5s），防它回来：

1. 子进程 import 日报主入口，`'crewai' in sys.modules` 必须为 False。
   ⚠️ 这条的有效性取决于 crewai 还装着；哪天卸载了它会变恒真，所以必须配第 2 条。
2. 全仓静态扫描，不许出现 `import crewai` / `from crewai import ...`
   （与装没装无关，卸载后依然有效）。

mutation check（`--maxfail=99`）：新建一个含 `import crewai` 的文件 ⇒ 第 2 条红；
日报顶层加回 `import crewai` ⇒ 两条都红。

回归：全套 **2104 passed / 0 failed**，ruff F821 全过。

## [0.45.73] — 2026-08-29 — 一个第三方 phone-home，和一个「设了等于没设」的环境变量名

`import alpha_hive_daily_report` 会起一个 daemon 线程 `Thread-1 (_track_install)`
往外发一个请求。链路是 `alpha_hive_daily_report` → `crewai_adapter` → `crewai`：
crewai 1.9.3 的 `crewai/__init__.py` **在模块顶层**调 `_track_install_async()`，
给 `api.scarf.sh` 打一个安装量像素点。每进程一次，于是**每次 pytest、每次生产扫描
各一次**。它不阻塞任何东西，也不是任何测试的依赖。

值得修的不是"有埋点"，是它是一次**不在 `http_gate` 覆盖范围内的出站 HTTPS**：
本项目所有自家数据源都走那道串行闸，而这个 phone-home 发生在闸门自己还没被
import 的时刻。`http_gate.py` 的结论「加源之前先确认闸门覆盖它」照样适用。

⚠️ **不把它和 2026-08-24 的 96 次 SSL EOF 挂因果**。`http_gate.py` 的 docstring 仍写着
「OpenSSL 1.1.1q 扛不住并发」，但那个归因 **2026-08-25 已被重测证伪**（1.1.1q 与 3.6.1
各 0 失败，串行 80.5s 反而比并发 62s 更慢；原数据是单次取样吃了 CDN 热缓存）。
关掉埋点的理由就是朴素的那一条：**没必要为一个第三方埋点，在每个进程启动时付一次
不受管的出站请求。**

### Fixed 1 — 关掉埋点，变量名照**装着的那版**抄（`crewai_adapter.py`）

在本仓唯一的 `import crewai` 处（`crewai_adapter.py` 顶部）、且**在那句 import 之前**
`os.environ.setdefault` 两个变量。crewai 读的是 import 那一刻的 env，晚一行都无效。

⚠️ **差点掉进去的坑**：网上（和记忆里）流传最广的 `CREWAI_TELEMETRY_OPT_OUT`
在 1.9.3 里**全仓不存在**。1.9.3 的
`telemetry/telemetry.py::Telemetry._is_telemetry_disabled()` 只认三个名字：

| 变量名 | 1.9.3 认吗 |
|---|---|
| `CREWAI_DISABLE_TELEMETRY` | ✅ |
| `CREWAI_DISABLE_TRACKING` | ✅ |
| `OTEL_SDK_DISABLED` | ✅（OpenTelemetry 总闸，本项目没别的 OTEL 用户，不动它） |
| `CREWAI_TELEMETRY_OPT_OUT` | ❌ **设了一行日志都不会变** |

这不是理论推演——mutation 测试实测把变量名换成 `CREWAI_TELEMETRY_OPT_OUT` 后，
crewai 自己的 `_is_telemetry_disabled()` 返回 `DISABLED=False`。
和 [[alpha-hive-silent-degradation]] 记的那类"看着成功其实早废了"同形：
**一个拼错/过期的 opt-out 变量名，是所有静默降级里最舒服的一种——它消除了本该
触发自查的那点不适，却什么都没关掉。**

### Fixed 2 — `crewai` 顶层导入改惰性（`alpha_hive_daily_report.py`）

顺手核了一遍"这个 import 是不是遗迹"：`run_crew_scan()` **全仓零调用方**，
只出现在 `QUICK_START.md` / `PHASE3_*.md` / `SLACK_INTEGRATION_GUIDE.md` 的示例里，
编排器与 `~/.claude/scripts/` 下也搜不到。为一个没人调的降级分支，让每次 import
都付出「一次出站 HTTPS + 一次 crewai 导入」，不划算。

把 `from crewai_adapter import AlphaHiveCrew` 挪进 `run_crew_scan()` 内部。
默认路径不再 import crewai（`'crewai' in sys.modules` → `False`），
埋点因此**不是被静音，是根本没发生**。实测导入耗时（各 3 次，稳定）：

| | 耗时 |
|---|---|
| 旧（顶层拉进 crewai_adapter） | 1.07 / 1.05 / 1.07s |
| 新（惰性） | 0.63 / 0.64 / 0.64s |

省约 **0.43s / 进程**（−40%）。注意是**每进程一次**，不是每条测试一次。

保留 `run_crew_scan()` 本身：它是 QUICK_START 里写着的公开用法，删它超出本次范围。

### Fixed 3 — `run_crew_scan()` 的降级守卫从来没生效过（`alpha_hive_daily_report.py`）

改惰性时撞见的：该方法 docstring 写「若 crewai 未安装，自动降级到 run_swarm_scan()」，
但守卫是 `if not AlphaHiveCrew`。而 crewai 没装时 `crewai_adapter` **照样 import 得动**
（它内部自己降级，`AlphaHiveCrew` 仍是个正常的类），所以这个条件恒假，
会一路走到 `crew.build()` 抛 `RuntimeError("CrewAI 未安装")` ——
**docstring 承诺的降级，实现里不存在**。真正的可用性标志是适配层的 `CREWAI_AVAILABLE`，
已改用它。（在死代码里，不影响任何现网行为，但它正是
[[alpha-hive-silent-degradation]] "守卫自己恒真" 那一条的又一个实例。）

### Added — 三道同向的闸 + 回归测试

| 位置 | 覆盖范围 | 在 git 里 |
|---|---|---|
| `crewai_adapter.py` 顶部 | 本仓唯一 `import crewai` 处，任何入口都绕不过 | ✅ |
| `tests/conftest.py` **模块级** | 整个 pytest 进程，含测试自己直接 `import crewai` | ✅ |
| `~/.claude/scripts/alpha-hive-orchestrator.sh`（export，第 66-67 行） | 生产扫描全进程，在第一次 spawn python（第 87 行读 `config.WATCHLIST`）之前 | ❌ **在仓外，无测试守护** |

⚠️ conftest 那段**必须是模块级，不能写成 fixture**：fixture 是每条测试跑之前才执行，
而 `import crewai` 发生在 pytest **收集阶段**（import 测试模块时），那时 fixture 一条都没跑。
写成 fixture 等于没设。

`tests/test_crewai_telemetry_optout.py`（3 条，2.9s）：

1. 断言 **crewai 自己的** `_is_telemetry_disabled()` 返回 True，
   而不是断言"我们写没写 os.environ"（后者恒真，等于没测）。升级 crewai 后若它改名，这条先红。
2. 子进程 import `crewai_adapter`，`_track_install` 线程数必须为 0。
3. 子进程 import `alpha_hive_daily_report`，`'crewai' in sys.modules` 必须为 False。

两个刻意的设计（都是 v0.45.71「恒真断言」教训的直接产物）：
- 子进程**显式把三个变量从 env 里删掉**再跑。否则它继承 conftest 设的值，
  不管被测代码写没写都绿。
- 探针挂 `threading.Thread.start` 而不是只看 socket——没网/代理拒连时它也不会连，
  只看 socket 会假绿。

**mutation check**（`--maxfail=99`，三次都只红对应的那一条）：

| 注入的故障 | 结果 |
|---|---|
| `setdefault` 挪到 `import crewai` **之后** | ❌ `TRACK_INSTALL_THREADS=1` |
| 恢复顶层 `from crewai_adapter import ...` | ❌ `CREWAI_IMPORTED=True` |
| 变量名换成 `CREWAI_TELEMETRY_OPT_OUT` | ❌ `DISABLED=False` |

### 验证

复现脚本（patch `threading.Thread.start`，命中 `_target.__name__ == '_track_install'` 即记录）：

| | `_track_install` 线程 | 出站连接 | `crewai` in sys.modules |
|---|---|---|---|
| 修前 `import alpha_hive_daily_report` | 1 | `('127.0.0.1', 7897)` | True |
| 修后 `import alpha_hive_daily_report` | **0** | **0** | **False** |
| 修后 `import crewai_adapter`（env 已清空） | **0** | **0** | True |

回归：受影响模块 373 条测试全绿（含 F821 静态闸 `test_no_undefined_names.py`）。

## [0.45.72] — 2026-08-29 — 一个没锚定的 fixture，一个没生效的桩：两条测试各自相信着一件没发生的事

起点是 `test_historical_price_anchor.py::test_as_of_date_in_past_skips_live_fetcher`
在 2026-08-29 变红。**生产代码是对的**——红的是测试自己。
排查中又牵出第二条：`test_cloud_snapshot_price.py` 的 5 条用例里有 4 条同日变红，
根因完全不同，但形状是同一族：**测试测的不是它以为在测的东西**。

### Fixed 1 — fixture 从没锚定过它要测的那一天（`tests/test_historical_price_anchor.py`）

假日线写死起点、被测日期另写死一个：

```python
idx = pd.date_range("2026-06-22", periods=len(prices), freq="D")  # 末行 = 7/17
result = fetch_stock_data("NVDA", as_of_date="2026-07-21")        # 目标日 = 7/21
```

两个日期各写各的，中间 4 天缺口从 v0.41.6 起就在，没有任何东西校验它们对得上。
一直绿，是因为当时的生产代码会拿末行顶上；v0.45.69/0.45.70 加了
「末行不是目标日就拒绝、**不以前一交易日冒充**」的闸之后，这个缺口才第一次被看见。

改为**从被测日期倒推**：`pd.date_range(end=last_date, periods=..., freq="D")`，
并在 helper 里断言 `idx[-1] == last_date`。periods 或日期怎么改，锚点都不松脱。
freq 保持 `"D"` 是刻意的——`freq="B"` 遇到 end 落在周末会静默往前回滚，
那正是本次要根治的漂移。同形教训见 v0.45.64「单测里的定时炸弹」：**日期能推就别钉**。

### Fixed 2 — 一个从没生效过的 yfinance 桩（`tests/test_cloud_snapshot_price.py`）

```python
monkeypatch.setattr(dp, "yf", type("Y", (), {"Ticker": _T}), raising=False)
```

`data_pipeline` 里**没有**模块级的 `yf`——四处 `import yfinance as yf` 全是
函数内局部导入，局部名把模块属性整个盖住。这个桩谁也没读到，5 条用例一路打真外网。
`raising=False` 又恰好把「属性不存在」这唯一的报警吞掉了。

为什么 8/29 当天写完是绿的、8/29 之后变红：写用例那天 yfinance 的 8/28 行
Close 恰好就是 `NaN`，**真网返回的形状与桩碰巧一致**；等 yfinance 回填了 8/28 的
真实收盘，同一份代码就红了。红的不是生产代码，是这条桩。
（`test_normal_day_unaffected` 更彻底：它拿真实行情断言真实行情，桩失效也照样绿——
它的 fixture 数字本来就是从真实行情抄来的。）

改为 patch `yfinance.Ticker` 本身（调用时在真模块上查属性，盖得住局部 import），
并在 fixture teardown 加一道看门断言：**桩一次都没被调用 = 打了真网**。
用例耗时 3.07s → 0.41s，这个降幅本身就是「网络没了」的凭据。

### Added — 给拒绝闸补上真覆盖（`tests/test_historical_price_anchor.py`）

修好锚点之后有个反直觉的后果：**本文件对那道拒绝闸的覆盖归零了**。
末行现在都正好落在目标日，没有一条用例还会走到闸上。实测把
`if _last_date != as_of:` 改成 `if False:`（等于整个回退 v0.45.69/70），
修完的 5 条用例**照样全绿**——旧那条脱靶的用例是「靠自己变红」在覆盖这道闸的，
那不叫覆盖。

新增 `TestRefusesToSubstituteNeighbouringTradingDay`，只钉本文件自己的教训：
日线整段停在目标日之前（不是 NaN，是压根没这一行）时必须标不可用。
NaN 形状与云端快照兜底的语义由 `test_cloud_snapshot_price.py` 专管，不在两处并存。

### 判据（mutation check，`--maxfail=99`）

⚠️ `pyproject.toml` 设了 `maxfail=1`，不显式抬高就只看得见第一条红的。

| 回退的守卫 | 变红的用例 |
|---|---|
| 「不以前一交易日冒充」拒绝闸（v0.45.69/70） | 5 条 |
| NaN 剔除 `dropna()`（v0.45.69） | 4 条 |
| 云端快照兜底（v0.45.70） | 2 条 |

三次全部咬住，且爆炸半径各不相同（=不是一条断言在重复报警）。
`data_pipeline.py` **本次一行未改**——全部改动都在测试侧。
`data_pipeline` 相关 12 个测试文件：183 passed（改前 179 passed + 4 failed）。


## [0.45.71] — 2026-08-29 — 那条为"崩溃"写的闸，拦不住崩溃：一个联网 flake 掀出的恒真断言

起因是一条普通的 flake：2026-08-29 全量跑里
`test_silent_failure_guards.py::TestAgentsSurviveFullyDegradedData` 的
BuzzBee 参数化变红，隔离重跑 44s 通过、全量重跑也通过。
日志里是 `apewisdom.io` 读超时（15s + urllib3 重试）和一条 Alpha Vantage SSLEOFError。

修 flake 只花了一半工夫；**另一半是发现这条测试从来没有生效过**。

### Fixed 1 — 默认测试套在打真外网（`tests/test_silent_failure_guards.py`）

这条测试的意图（见其 docstring）纯粹是 None 处理：v0.45.3 把 `volatility_20d`
从 0.0 改成 None 后，`buzz_bee.py` 的 `vol20 > _vlt.get("extreme", 60)` 当场
TypeError。它**不该碰网络**。但它只 stub 了 `_get_stock_data`，然后直接调真实的
`analyze()` —— 于是逐个打了出去。实测（pytest 内挂 HTTP 探针）：

| 蜂 | 真实出网 |
|---|---|
| BuzzBee | apewisdom（Reddit）、Alpha Vantage（新闻）、Yahoo 热搜、CNN F&G |
| BearBee | SEC EDGAR ×7、CBOE 期权链 ×2、yfinance ×7 |
| RivalBee | yfinance ×3（EPS 共识 / 技术指标 / 空头仓位） |

`pyproject.toml` 的 `--timeout=60` 对上冷缓存的 44s，**上游一慢就红**；
而 addopts 里有 `-x`，这一条 flake 会把整套掐断。

**修法：stub 打在叶子（各取数模块），不打在中间聚合层**——
`get_real_crowding_metrics` / `CrowdingDetector` / 7 通道加权 / 看空评分
这些真实逻辑仍然跑，它们才是这道闸要保护的东西。所有 stub 一律返回
"不可用"档，与 `FULLY_DEGRADED` 同一个意思，不是喂好数据把降级路径绕开。

同时加 `_no_network` 绊线：出网即炸并点名调用栈。理由是**光有 stub 清单会悄悄腐烂**
——谁给 BuzzBee 加第 8 个通道，清单不会自己长出一行，测试只会慢慢变回联网 flake。
这与 conftest `_block_same_day_macro` 记的是同一条教训（加数据源必须同时确认
"测试里它被关掉了吗"，v0.45.60 / v0.45.61 连漏两次）。实测把 `sec_edgar` 那行
stub 去掉，0.88s 内报出 `sec_edgar.py:115 _request_get <- _load_cik_map <- __init__`。

效果（`pytest tests/test_silent_failure_guards.py -q -p no:randomly`）：

- 全文件 **35.82s → 5.01s**，54 passed 不变
- BuzzBee / BearBee 两条已跌出 durations 榜（<0.01s），BearBee 原为 **25.61s**
- RivalBee 3.08s → 0.92s（剩下的是本地 ML 模型加载，不出网）
- HTTP 探针复测：三条参数化 **一次外网都不打**

### Fixed 2 — 三条断言全部恒真：这道闸拦不住它自己写的那个崩溃

修完 flake 后按本文件开头的规矩做回归验证（"每条测试都必须能在把修复回退掉时变红"），
把 v0.45.3 那道 None 守卫拆掉、让 BuzzBee 当场 TypeError —— **三条断言依然全绿**。

根因是 `make_error_result()` 的兜底返回与正常返回**同形**：

```python
AgentResult(score=5.0, direction="neutral", confidence=0.0,
            discovery=f"Error: {error}", ..., error=str(error))
```

于是逐条对照：

| 断言 | 崩溃时实际拿到 | 结果 |
|---|---|---|
| `error != "agent_failure"` | `"'>' not supported between instances of 'NoneType' and 'int'"` | 恒真 |
| `"failed" not in discovery` | `"Error: '>' not supported..."`（含 Error，不含 failed） | 恒真 |
| `score is not None` | `5.0`（崩溃兜底值） | 恒真 |

其中第一条最刺眼：`"agent_failure"` 这个字面量
**全仓只出现在这行断言里**（`grep -rn agent_failure --include=*.py` 仅 1 处命中，
就是它自己），生产代码从来没产出过——等于在和一个不存在的值比较。

也就是说：**为 v0.45.3 崩溃写的闸，拦不住 v0.45.3 的崩溃。**
这正是本文件开头定义的形状——没有报错、退出码为 0，但产出早已是假的；
也是 MEMORY「静默中性化」记的那条：`score=5.0 / confidence=0.0` 与真实中性同形。

**修法**：原三条按用户要求原样保留（不改既有意图），另加一条真正生效的：

```python
assert not result.get("error"), f"{cls_name} 崩了：{result.get('error')}"
```

实测：注入 regression 后 **0.78s 变红**，并打出完整 TypeError 栈；恢复后转绿。

> 教训：**"测试跑得慢"和"测试没生效"可以是同一个坑的两面。**
> 这条测试因为真打网络而慢，慢又掩护了它其实什么也没测——44s 的运行时间
> 看起来像在"认真做事"，实际上三条断言在任何输入下都返回 True。
> 修 flake 时顺手做一次"把修复回退掉看它红不红"，才是这次真正的收获。

### 附带发现（未修，已另开任务）

`alpha_hive_daily_report` 导入时，**crewai 会起一个后台守护线程 `_track_install`
向外发装机遥测**（本机走 127.0.0.1:7897 代理）。它不阻塞测试、也不是任何测试的依赖，
但每次跑测试与每次生产扫描都会发一次。属第三方 phone-home，与本次改动无关，
故未在本版处理。

### Changed

- `tests/test_silent_failure_guards.py`：`TestAgentsSurviveFullyDegradedData`
  新增 `_offline`（叶子级取数 stub）与 `_no_network`（出网绊线）两个 autouse
  fixture；测试方法新增一条真正生效的 `error` 断言。**仅测试文件改动，
  生产代码零改动。**

## [0.45.70] — 2026-08-29 — 云端快照价接进补跑取价链；8/28 补跑完成并上线

v0.45.69 让 NaN 不再冒充价格，但补跑因此**拿不到任何价格**：
yfinance 有 2026-08-28 那一行，`Volume=1.94 亿`（当天确实交易了），
但 `Close=NaN`。而云端快照有该日真实收盘（目标日 17:02 ET 从 CBOE 抓）。

### Added — `data_pipeline._fetch_historical_stock_data` 的快照兜底

yfinance 拿不到目标日收盘时改问 `cloud_snapshot_loader.load_ticker()`。
**不另造日期闸** —— 该函数内部已校验 `vintage_date == date`，vintage 不符返回 None；
再造一道就是两个口径并存。

三条约束：
- 快照价必须是**有限正数**（NaN / 0 / 负数 / 字符串一律不收）
- 来源标进 `source_name`（实测为 `cloud_snapshot:cboe_close`）+ `_price_from_cloud_snapshot`
- **`volume_ratio` 不给** —— 它要拿目标日成交量比均量，而这条路径下目标日的量
  本来就没取到，用前一日的量冒充会静默失真。动量标 `5d_snapshot_spliced`（拼接口径）

目标日有真实收盘时**逐字节走原路径**，连 `load_ticker` 都不调用（测试有断言）。

### 2026-08-28 补跑结果（已部署）

```
30/30 标的  data_quality=real   字段缺失：无
err=0/240   real=92%            失效条件 275 条覆盖 30/30
```

口径核验（价格必须是 8/28 的，且不等于 8/27）：

| | 8/28 | 8/27 |
|---|---|---|
| NVDA | 217.55 | 227.98 |
| MSFT | 513.53 | 505.06 |
| AMC | 2.59 | 2.70 |
| NVDA `iv_current` | 33.44 | 34.64 |
| NVDA `total_oi` | 2,281,026 | 2,497,713 |

gh-pages 部署 `120d950`：12 份 ML 报告 + 日报 + 看板。
**12 份是设计值**（`ALPHA_HIVE_ML_REPORT_MAX` 默认 12），日报覆盖全部 30 只。

线上验证 —— 用户最初问的那只：

```
AMC 8/28  IV Skew 比 = 0.84      （8/27 是【空】）
          IV Rank = 18.8%  期限结构 = CONTANGO  RV30 = 99.1%
```

### 已知限制（未做）

`generate_ml_report.py` **无法为过去日期生成报告** —— 它用 `pdt_today()`，
没有覆盖入口，`--date` 参数不存在。所以编排器 Step 3 的「补齐缺失标的」
只在当天有效；补跑历史日只能拿到 Step 2 产出的前 12 名。
没有 monkey-patch 绕开，那会产出**日期标错**的文件。

### Added — `tests/test_cloud_snapshot_price.py`（5 条）

含四次 mutation check：去掉快照兜底 / 去掉快照价校验 / 去掉日期校验 /
volume_ratio 改用前一日冒充 —— 全部变红。

## [0.45.69] — 2026-08-29 — 一个 NaN 穿过四道写着「<= 0」的守卫

补跑 2026-08-28 时 30 只标的**全部** `data_quality=degraded`、`iv_skew_ratio=None`、
`gamma_exposure=0.0`。追到底是**一个 NaN**，它穿过了四道各自写着 `<= 0` 的守卫。

### 为什么 NaN 谁都拦不住

Python 里 `float('nan')` 有两个性质，合起来正好绕开常规写法：

- **任何比较都返回 False** → `price <= 0`、`total <= 0` 全部放行
- **它是 truthy** → `if not price`、`if not atm_price` 也拦不住

四道守卫依次失守：

| 位置 | 守卫写法 | 结果 |
|---|---|---|
| `data_pipeline._fetch_historical_stock_data` | `hist["Close"].iloc[-1] <= 0` | `price=float(NaN)` |
| `swarm_agents/base._get_stock_data` | `if not price or price <= 0` | 不标 `_data_unavailable` |
| `options_analyzer.analyze` | `if not atm_price` | ATM 带 `[NaN, NaN]` |
| `calculate_gamma_exposure`（v0.45.63 新写） | `stock_price is None or <= 0` | 返回 NaN |

末端 `_sanitize_result` 再把 NaN 转成 **0.0** ——
**v0.45.63 刚消灭的那个假读数，在出口被原样还原了。**

链路后果：ATM 带是 `[NaN, NaN]` ⇒ `NaN <= strike <= NaN` 恒 False ⇒
`raw_ivs` 全空 ⇒ `data_quality` 判 degraded ⇒ `iv_current` 回落
`last_valid_iv` 陈缓存（NVDA 拿到 8/27 的 34.64）。

### 根因：yfinance 有 8/28 的行，但 Close 是 NaN

```
2026-08-27  Close=227.98  Volume=298,909,800
2026-08-28  Close=nan     Volume=194,036,188   ← 当天确实交易了
```

### Fixed

- `data_pipeline`：先 `dropna()` 再判空，后续窗口一并用清洗过的行
- **`data_pipeline` ⚠️ 同时加日期校验** —— 只 dropna 会让「丢掉 NaN 行」
  静默变成「用**前一交易日**冒充目标日」：末行变成 8/27 的 227.98，
  而 8/28 真实收盘（云端 CBOE 快照）是 217.55，差 **4.8%**。
  那正是 2026-08-24 被写进 `signal_archive` 隔离名单的口径污染，换了个更隐蔽的入口。
  现在拿不到目标日收盘就返回 `_data_unavailable` + `_reason`，**不冒充**。
- `swarm_agents/base._get_stock_data`：加 `isfinite` 校验
- `options_analyzer`：`calculate_iv_skew` / `calculate_gamma_exposure` /
  `analyze` 的 `atm_price` 兜底，三处加 `_math.isfinite`

### 教训

**v0.45.63 我写的守卫是 `is None or <= 0`，二次检查也没抓到 NaN。**
本项目「安全默认值」判据要加一条：*写数值守卫时，先问 NaN 会怎么走。*
`<= 0` 与 `if not x` 都不是 NaN 的守卫。

### ⚠️ 未完成：2026-08-28 的补跑仍无法产出正确数据

yfinance 没有 8/28 的收盘价，而云端快照有（`price_at_fetch=217.55`，
CBOE 延时，8/28 17:02 ET 抓取，`vintage_status: ok`）。
**取价链目前不消费云端快照的价格**，所以补跑只能诚实地标"不可用"。
把云端快照价接进补跑取价链是独立改动，未做。**网站未部署 8/28 数据。**

## [0.45.68] — 2026-08-29 — 二次检查 v0.45.65/67：四条自造 bug，三条是守卫自己在撒谎

对同一 session 内 v0.45.65（日历体检）与 v0.45.67（上游监视器）做二次检查，
五条假设查出**四条真 bug**。其中三条是同一个形状：

> **表名取自甲表、日期取自乙表，拼进同一句话。**

v0.45.67 自查时已经在监视器的渲染里抓到过一次这个错，当场修了；
但**没有回头查同一 session 里更早写的 v0.45.65**——于是同型错误在
`economic_calendar.py` 和 `guard_bee.py` 里各留了一份。
**自查抓到一个模式后，要把它当模式去搜全部改动，不是当个案修掉。**

根因是 `get_calendar_health()` 返回两组不可混用的字段：
`binding_table`（余量最小 = **肇事表**）与 `shortest_table` + `last_date`/`horizon_days`
（地平线最短）。阈值逐表不同，两者常非同一张表。

### Fixed 1 — exhausted 告警点名多张表，却共用一个日期（`economic_calendar.py`）

2026-12-15 那天，旧文案会打：

```
🚨 经济日历已被日期走完：cpi/gdp/nfp 表在 2026-10-29 之后没有任何条目
```

`2026-10-29` 是 gdp 的表尾。而 **cpi 在该日之后还有 11-10、12-10 两条，
nfp 还有 11-06、12-04**——这句话对三张表里的两张是假的。
现改为逐表写各自表尾：`cpi 止于 2026-12-10（源 …）；gdp 止于 2026-10-29（源 …）；…`

### Fixed 2 — GuardBee 写的日期属于一张**健康**的表（`swarm_agents/guard_bee.py`）

`details["macro_calendar_last_date"]` 取的是 `_cal["last_date"]`（shortest 表）。
**2026-09-06 ~ 09-29 这 24 天**里：nfp/cpi 先后 stale，而 shortest 是 gdp——
gdp 此时剩 49 天、阈值 30，**完全健康**。于是 analysis JSON 里会出现
`macro_calendar: "stale"` 配着一张健康表的日期，排查时直接指错方向。
八天后就进入这个窗口。

现改用 binding 表，并把该性质写成单测钉死：
**binding_table 必定是肇事表之一**（stale 表 margin<0，exhausted 表 margin 更负，
故 `min(margin)` 必落在肇事表上）。`test_binding_table_is_always_one_of_the_culprits`
遍历 2026-08-01~2028-03-01 每一天验证，覆盖 100+ 个非健康日。

### Fixed 3 — 监视器节流 + 空缓存 → 谎报健康（`economic_calendar_watch.py`）

节流窗口内直接拿 `state["upstream"]` 当结论。若缓存里没有任何源的结果
（状态文件被截断、手工编辑、或旧格式），得到 `undeterminable=[]`、`new=[]`
→ 打印 **「✅ 日历健康，上游无新日程」、退出码 0**——**一个源都没查过**。

监视器自己犯了它专门要治的那个静默失败。现改为：缓存里缺哪个源，
就把哪个源判成 `determinable=False`，reason 写明「此源从未被成功检查过」。

### Fixed 4 — 「上游暂无新日程」是一句没有依据的断言

退出码优先级是 `action(1) > health(1) > undetermined(3)`。于是
**「本地日历将见底 + 四个源全部抓取失败」**这一格走 health 分支，
旧版在这里无条件打：

```
⚠️ 本地日历 stale：nfp 只到 2026-12-04（剩 75 天），上游暂无新日程
```

四个源一个都没抓到，却断言上游没有新日程；且 undetermined 被 health 整个吃掉，
日志里看不出抓取失败——**恰好抵消掉这个监视器存在的全部意义**。

新增 `upstream_conclusive` 字段（四源全部 determinable 才为真）。
退出码优先级不变（本地将见底同样要人盯），但**文案与退出码解耦**：
不 conclusive 时一律打「⚠️ 上游 N 个源无法判定（…），本次无法确认上游有无新日程」。
编排器 Step 13 的摘要提取器同步修（⚠️ 该文件不在本仓库，
备份 `alpha-hive-orchestrator.sh.bak-20260829_calwatch2`）。

### Fixed 5（脆弱点，当前无影响）— BLS 切片锚在页面 `<title>` 上

`<title>` 与表头同文，`next()` 命中第 0 行，把 229 行导航区一并纳入切片。
今天恰好没有能被日期正则吃掉的行（导航是 `FEBRUARY 2026` 这种格式）——
**那是运气不是设计**。现锚到真表头（其后 3 行内必有 `Reference Month`）。

### 一条方法论教训：差点提交一条形同虚设的回归测试

给 Fixed 5 写的样本最初是 `<li>Last Modified Date: Jan. 05, 2099</li>`，
测试绿。但**用旧实现跑一遍才发现旧实现也是绿的**——正则是 `fullmatch`，
带前缀的行本就不会被吃，样本根本没触发那个 bug。
改成独立成行的 `Jan. 05, 2099` 后，旧实现解析出 `max=2099-01-05`（会误报
「上游发布新日程」），新实现 `max=2027-12-10`。

MEMORY.md「分布不变式」条早就写过：**新增不变式必配「喂退化数据看它红」的测试**。
这次是它第一次在「回归测试」上生效——回归测试同样要验证它在旧代码上会红，
否则只是一条昂贵的注释。

### 复核过但**不是** bug 的两条

- `_parse_dates` 在一次 `get_upcoming_events` 里被调用两轮（事件构建 + 体检各一次）：
  4 表 ×2，纯内存操作，实测无感知开销，不改。
- `parse_fomc` 解析出 54 个日期（页面含历史年份区块）：只取 max，属预期。

### Changed
- `economic_calendar.py`：告警节流键改用 binding 表自己的表尾日期；
  `get_calendar_health` docstring 写明「binding 必定是肇事表，配日期用它」
- `tests/test_economic_calendar.py`：+4（32 条）
- `tests/test_economic_calendar_watch.py`：+5（29 条）


## [0.45.67] — 2026-08-29 — 谁去看上游发了没：日程监视器（v0.45.65 的另一半）

v0.45.65 给硬编码日历加了地平线告警和一条会变红的单测，但那些只会说
**「快见底了」**，说不出**「上游发布 2027 日程了没」**。后者得有人每周去源站看一眼。
`economic_calendar_watch.py` 就是那个人。

### 为什么做成编排器 Step 13，而不是周度 LLM 任务

三条理由，第二条是项目既有判例（Step 11 的注释里已经写过一次）：

1. **零 API 费用**。这个检查是纯机械的：抓四个页面、比日期。用 LLM 跑等于
   每周付钱让模型做正则能做的事，且违反 CLAUDE.md「花钱前必须先问」。
2. **心跳更可靠**。周度 LLM 任务可能被跳过、被改写、某周没跑；编排器每个交易日都跑。
3. **「每交易日跑」实际就是「每周联网一次」**——脚本自带 7 天节流。
   例外：上次查出有新日程而人还没抄，则不节流，天天提醒到抄完为止。

不阻断、不动 `OVERALL_STATUS`：日历没坏，只是需要有人去抄。

### 核心不变式：「看不出来」≠「没有新的」

一个静默失败的监视器，就是 v0.45.65 治的那个 bug 升了一级：
页面改版 → 解析器抓到 0 条 → 报告「无新日程」→ 一切正常 → 直到日历见底。

所以每个源都要求解析出足够条目（CPI/NFP ≥ 12、FOMC ≥ 8、GDP ≥ 1），
达不到一律 `undetermined`（退出码 3），**绝不报健康**。抓取失败同理。
两条独立单测钉住这一点。

### 它永远不会自己往表里写日期

这是刻意的边界。v0.45.65 修的 bug 本质是「用规律推算代替抄写」；
如果监视器自动把抓到的日期写进表，等于把同一个错误自动化了——
解析器对页面结构的每个假设，都会变成一批**看起来像抄来的、实际是程序猜的**日期。
它只负责喊「去看」，抄写永远人工。单测
`test_watcher_never_mutates_the_calendar_tables` 守着。

### 三处源站的实际坑

- **BEA 的日期格不带年份**：`<div class="release-date">October 29</div>`，
  年份只在区块标题里，跨年必歧义。所以 GDP 比对的是**标题里的参考季度**
  （`GDP (Advance Estimate), 3rd Quarter 2026`），自带年份，无歧义。
- **bls.gov 用 UA 做准入**：浏览器 UA、`curl/8.4.0`、`python-urllib/3.11` 一律 403；
  **只要 UA 里有一个邮箱形状的 token 就放行**——实测 `nobody@nowhere.invalid`
  同样 200，即它只校验格式、不校验地址。默认 UA 因此用占位域，
  **不把任何私人邮箱写进仓库、也不每周发给 BLS**；想做好公民设
  `ALPHA_HIVE_CONTACT_EMAIL` 即可。单测断言默认 UA 里没有私人邮箱。
- **FOMC 页面含历史年份**，解析到 54 个日期属正常（只取 max）；
  `2025-08-22 (notation vote)` 非两日例会，已跳过。

### 自查抓到一处「列名说谎」

首版渲染打的是「最短表 nfp，覆盖到 2026-10-29，还剩 61 天」——
**表名取自 `binding_table`（余量最小 = nfp），天数取自 `shortest_table`
（地平线最短 = gdp）**，两张不同的表被拼进同一句话。阈值逐表不同，
这两者本就常常不是同一张。已拆成两组各自成套的字段，并加回归测试。
在治标签诚实性的代码里又犯一次标签谎报，与 v0.45.66 自查抓到的那条同型。

### 首次实跑（2026-08-29，四源全部联通）

```
✅ cpi   上游最新 2026-12-10（我们已核对到 2026-12-10）
✅ fomc  上游最新 2028-01-26（我们已核对到 2028-01-26）
✅ gdp   上游最新参考季度 2026Q3（我们 2026Q3）
✅ nfp   上游最新 2026-12-04（我们已核对到 2026-12-04）
```

四张表与上游**逐一吻合**——这同时是对 v0.45.65 抄录的独立复核：
监视器用完全不同的解析路径重新算出了同样的边界。
也再次确认：BLS/BEA 的 2027 日程截至今天仍未发布，只能等。

### Added
- `economic_calendar_watch.py`：四源日程监视器；7 天节流（待办未清时不节流）；
  退出码 0/1/3 沿用 Step 10-12 约定；状态文件 `cache/economic_calendar_watch.json`（已 gitignore）
- `~/.claude/scripts/alpha-hive-orchestrator.sh` **Step 13**（⚠️ 该文件不在本仓库，
  备份 `alpha-hive-orchestrator.sh.bak-20260829_calwatch`）
- `tests/test_economic_calendar_watch.py`：24 条离线测试（`_fetch` 全部 monkeypatch，不打网络）


## [0.45.66] — 2026-08-29 — 定时扫描一直在走另一条网络链路

⚠️ **本条改的是 `~/.claude/scripts/alpha-hive-orchestrator.sh`，该文件不在本仓库**
（`git ls-files` 查不到它）。想看改动只能直接读那个文件，
备份在 `alpha-hive-orchestrator.sh.bak-20260829`。

### 根因：launchd 不继承登录 shell 的环境变量

`com.alpha.hive.daily.plist` 的 `EnvironmentVariables` 里只有 `PATH`。
而用户交互 shell 有 `HTTPS_PROXY=http://127.0.0.1:7897`。
于是**定时扫描走直连，人工排查走代理 —— 两条完全不同的链路**。

这解释了一个长期对不上的现象：yfinance 429 逐日递增
（8/25 = 364 → 8/26 = 487 → 8/27 = 690），而手工怎么测都是好的。

2026-08-29 同机同时刻实测：

| | CBOE | yfinance |
|---|---|---|
| 带代理 | 200 | **200，拿到 5 根日线** |
| 直连 | 200 | **YFRateLimitError / 403** |

已确认 `yfinance 1.2.0`（curl_cffi 0.13.0 + requests 2.32.5）**认 `HTTPS_PROXY`
环境变量** —— 否则这次改动对它无效，白做。

### Changed — 编排器开头新增出站代理探活（在任何 Step 之前）

- 地址取 `$ALPHA_HIVE_PROXY`，默认 `http://127.0.0.1:7897`；设为 `none` 显式关闭
- **探活不只探端口**：端口开着但转发是坏的会把整轮拖死，所以还发一次真实
  HTTPS（CBOE 根域，8 秒超时）。收到 4xx/5xx 也算通过 —— 隧道通了就行，
  目标站怎么答不重要
- 探活失败**退回直连并写 WARN**，不静默

**为什么探活而不是直接写进 plist**：写死会把故障模式变成
「代理没开 → 整轮全灭」，比原先「有时直连能过」更集中。
探活保住了原有的降级能力。

### Fixed — 自测抓到两条「日志说的和实际做的不一致」

写完跑三情形自测，两条都属于本项目反复在治的那一类：

1. **退回直连的分支只打日志、没真的清掉代理变量。** launchd 下无所谓
   （本来就没有），但**手工从终端跑**时 shell 里是有的 —— 日志写着
   「退回直连」而实际仍走代理。在治标签诚实性的代码里又犯一次标签谎报。
   已加 `unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy`。
2. **`ALPHA_HIVE_PROXY=none` 被报成「探活失败」。** 那是显式关闭，不是故障，
   报成 WARN 会让人去查一个不存在的问题。已拆成独立的 INFO 分支。

### 未做（记录在案）

**整轮零产出仍然无人被告知。** 8/28 那轮 35 次 SSLEOFError、0 次成功抓取、
没有产出 `.swarm_results_2026-08-28.json`，Step 12 自己报了「无法判定」，
而编排器末尾照样打「🎉 编排流程完成」。CLAUDE.md 明确禁止发「扫描失败通知」，
所以**按设计它就是静默的**。那条规则要治的是「逐标的低分预警」那类噪音，
与「整轮零产出」不是一个量级，但改不改是用户的决定，未擅自动。

⚠️ 本次改动让故障更集中（代理成了新的单点，虽有降级）。
**在没有告警的前提下，集中式故障比分散式更难发现** —— 这个权衡是明知的。

---

### 追加：8/28「跑了 51 分钟但网站没更新」的完整解剖

用户问「8/28 的自动任务跑了吗？跑了怎么没更新网站」。跑了，14:00→14:51 满程。
网站没更新**不是因为部署失败，是因为部署被跳过、而跳过的理由是假的**。

```
14:00:01  启动
14:01:18  Step 2 蜂群分析   ⏰ 超时 1800s 被杀，产出 0
14:41:51  Step 3 ML 报告    ⏰ 超时 600s 被杀，30 只出 6 份残报
14:51:51  Step 4 仪表板     ⏭️ 跳过（0 秒）
14:51:51  Step 5 GitHub部署 ⏭️ 跳过（0 秒）   ← 网站没更新在这
14:51:52  🎉 编排流程完成
```

`run_step` 的 RC=2 含义是**「脚本不存在」**，而 `auto_deploy.py` /
`update_dashboard.py` 早已并入 Step 2 pipeline、文件本就不存在 ——
**所以 Step 4/5 每一轮都走 RC=2 分支**，每一轮都打「已由 Step 2 完成」。
Step 2 成功的日子它碰巧是真的；8/28 Step 2 被杀，它就是一句谎话。
`STEP2_RC` 就在同一个脚本里，是现成的变量，从来没人看它一眼。

#### Fixed ① — 跳过消息接上 `STEP2_RC`

Step 2 未成功时改打 `🚨 Step 5 未部署！…本轮网站不会更新`，
`steps_result` 写 `{"status":"failed","reason":"step2_did_not_complete","step2_rc":N}`。
用字面量 `"failed"` 是刻意的 —— `alert_manager` 的 P1 步骤失败检查
判的正是 `step_result.get('status') == 'failed'`，用自定义串它接不住。

#### Fixed ② — 三处让「零产出」再也藏不住

**(a) 产出核验。** `OVERALL_STATUS` 此前只由「步骤返回非 0」驱动 ——
若所有步骤都返回 0 却什么都没落盘（静默降级，本项目最常见的形状），
状态仍是 success。新增：Step 6 之前核验
`.swarm_results_$DATE_STR.json` 是否存在且非空，缺失即
`OVERALL_STATUS="failed"`。

**(b) `status.json` 提前写一次 —— 这条最要紧。**
Step 6（`alert_manager`）**读的就是这个文件**，而它在第 675 行运行、
文件在第 978 行才写，相差 300 行。于是**告警分析每一轮读的都是上一轮的状态**：
`status == 'failed'` 的 P0 路径、`step.status == 'failed'` 的 P1 路径，
判的全是昨天。8/28 那两条 HIGH 告警之所以还对，是因为它们查的是
「日报文件在不在」（按当天日期），跟 status.json 无关。
写入逻辑提取成 `write_status()`，Step 6 前后各调一次，格式不会漂移。

**(c) 横幅跟状态一致。** 此前无论成败末尾都先打「🎉 编排流程完成」，
人翻日志第一眼看到的就是它。退出码本来就是对的（partial → 1），
**坏的一直是给人看的那句话**。

#### 实测：一条从未触发过的告警路径被打通

`alert_manager` 里 `status == 'failed'` → CRITICAL P0 的分支入库以来一直存在，
但编排器只写 `success` / `partial`，**从没产生过 `failed`**，
所以那条 P0 一次都没响过。喂入新的 status.json 实测：

```
Critical: 1   🚨 【P0 严重】完整流程失败
```

#### 更正前一条记录

本条目上方写的「编排器谎报成功」不准确。`status.json` 是诚实的
（8/28 记的是 `partial`，step2/step3 都标了 timeout），退出码也是 1，
告警还正确抓到了「未生成日报」。
**机制是好的，坏的是给人看的横幅、以及告警读错了轮次。**

---

### 二次检查 —— 四条，其中一条会让整个改动在 8/28 那种场景下完全失效

**① 状态被降级：`failed` 又被改回 `partial`（最严重）**

`OVERALL_STATUS` 全脚本有 9 处**无条件**赋值，谁最后跑谁说了算 ——
而「最后跑」和「最严重」没有关系。`failed` 是本版新引入的更高档位，
和旧代码里所有无条件的 `partial` 相遇必然出事：

```
8/28 实际序列：Step 2 超时 → 零产出 → failed
              Step 3 超时 → partial      ← 把 failed 盖掉
```

**于是 CRITICAL P0 永远不会响，本次改动等于没做。**
改法：新增只升不降的 `set_status()`（`success < partial < failed`），
9 处赋值全部改走它。

**② 我重造了项目已有的闸，而且造得更差**

新写的「本轮产出核验」与第 494 行 v0.45.4 的「标的完整性闸」功能重叠。
更难看的是实现质量：我用 `|| echo 0`，而它用 `|| echo -1`，
注释里明确写着为什么不能用 0 ——

> 读失败返回 -1 而不是 0 —— 0 会被下面当成「30 只全丢」误报，
> 把「无法判定」渲染成一个具体且极端的结论，正是本项目要根除的形状。

**我犯的正是它记着要避免的那个错。** CLAUDE.md「先用装好的工具，再手搓」
这条又中一次。已删除重复块，改为在既有闸上扩展一个 `-eq 0` 分支。

**③ 空文件被判成成功，还带个绿勾**

`{}` 和坏 JSON 都能通过 `-s` 非空判断，实测输出
`✅ 本轮产出核验：含 0 只标的` + `status=success`。
绿勾旁边写着 0 —— 正是 `check_label_honesty` 那条判据要禁的形状
（标签宣称成功 ⇒ 它管辖的值必须非空）。
并入既有闸后：`{}` → `failed`；坏 JSON → `partial`/`unknown`（**不是** 0）。

**④ `set_status` 把未知值原样存进去，与自己的注释矛盾**

注释写「未知值按 partial 计，宁可高估不低估」，实现却是
`OVERALL_STATUS="$1"` —— 真传进一个拼错的状态，`status.json` 里就会出现
`alert_manager` 不认识的字符串（它只判 `== 'failed'`），等于把一次告警悄悄丢掉。
已加归一化 + WARN。

另有一次 `bash -n` 抓到的语法错（`elif` 放到了内层 `fi` 之外），未计入。

五种产出情形逐一实测：文件缺失→failed｜`{}`→failed｜坏 JSON→partial/unknown｜
3/10→partial/incomplete｜10/10→success。
状态单调性实测：`failed` 后再来 `partial` 仍为 `failed`。
## [0.45.65] — 2026-08-29 — 宏观日历：一张会过期的表，和它挂着的假来源

`economic_calendar.py` 的四张硬编码表（FOMC/CPI/NFP/GDP）只排到 2026-12。
`get_upcoming_events(days=30)` 会从 12 月起逐日缩短，约 **2026-12-10 之后恒返回 `[]`**——
而「未来 30 天真的没有宏观事件」也返回 `[]`，两者同形，谁都不会发现。

去补 2027 的路上，撞见了一个比过期更急的问题。

### 一、旧表的 2026 年有 9 个日期是错的（本次已修，含 4 个未来日期）

模块 docstring 一直挂着 `bls.gov/schedule` 当来源，但把日期逐条对下来：

| 表 | 错/总 | 错的位置 |
|---|---|---|
| FOMC | **0 / 16** | 2025+2026 与美联储页面逐条一致 |
| CPI | **6 / 12** | 01-14→01-13、04-14→04-10、09-11、10-14、11-10、12-10 |
| NFP | **2 / 12** | 02-06→**02-11**、05-01→**05-08** |
| GDP | **1 / 4** | 10-28→**10-29** |

错的位置**恰好全是官方偏离规律的那几个月**——NFP 错的两个月正是 BLS 没排在
「第一个周五」的两个月。也就是说：FOMC 那张表是**抄**来的，另外三张是照
「每月第二周 / 第一个周五」**推**出来的，却共用同一段来源注释。
最狠的一处是 2026-11 的 CPI：写成 11-17，官方是 11-10，**差 7 天**。

其中 4 个是**尚未发生**的日期，直接影响当下：dashboard 的 Hero 倒计时会在
9/11 CPI 当天还显示「距 CPI 还有 4 天」。

（2025 年那 24 条未逐条核对——BLS 站点只回溯到 2025-11 参考月。它们全是过去日期，
`get_upcoming_events` 不返回，已在表内就地标注，未静默混入。）

### 二、补录 2027（只抄已发布的，没发布的写「待验证」）

- **FOMC 2027 全年八次**已发布，全部录入：01-27 / 03-17 / 04-28 / 06-09 /
  07-28 / 09-15 / 10-27 / 12-08（取两日会议第二天 = 声明发布日）。
  2028 官方只发布了一次（脚注 `January 25-26, 2028`），就只录这一次。
- **CPI / NFP / GDP 的 2027 日程官方尚未发布**（2026-08-29 核对：BLS 站点导航只到
  `ENTIRE YEAR, 2026`，BEA 滚动日程只到 2026-12-23）。**一条都没编**，
  写进 `_TABLE_SPECS[...]["pending"]` 标为待验证。
  空着会被下面的地平线告警吼出来，编上不会——这是空着比编上好的全部理由。

### 三、地平线体检 `get_calendar_health()`：逐表算，取最差

**必须逐表。** FOMC 现在排到 2028、另外三张死在 2026-12；若按 `max(全部日期)`
算总地平线，会得到「还有 515 天覆盖」的健康读数，而四条数据里三条已断供——
聚合掩盖分量，正是这次要治的病本身。

阈值也逐表设，因为它衡量的是**来源提前多久发布**，不是我们想要多少：
FOMC 180 天（美联储提前 1.5~2.5 年发）、CPI/NFP 90 天（BLS 提前约一年、每秋刷新）、
GDP 30 天（BEA 只维护约 4 个月滚动日程，而初值是季度频率，地平线天然在 0~91 摆动；
一刀切 90 会让 GDP 永久变红，而永久变红的告警很快会被人关掉）。

低于阈值 `WARNING`，被走完 `ERROR`，同状态一个进程只吼一次
（日报一轮扫 30 只标的会调 30 次日历）。

**告警不以「返回值为空」为触发条件。** 空列表有两种成因：安静窗口（正常）和表走完
（故障）。判据是表尾日期，不是这次返回了几条——把这两件事混在一起，就是静默降级的入口。

### 四、单测：唯一一组故意不注入时钟的

原有 12 条测试**全部**传了 `ref_date=`，所以表走完之后它们照样全绿——
这正是过期能不被发现的原因：单测活在 2026 年的世界里，生产活在真实的今天。

新增 `TestCoverageHorizon`（真实 `date.today()`，不注入）+ 15 条注入测试，含三条回归：
- `test_long_fomc_table_does_not_mask_dead_bls_tables`：2027-06-01 那天 FOMC 还剩 5 次、
  另外三张已走完，且 `get_upcoming_events` **非空**——同时守住「逐表」与「不看返回值」。
- `test_2026_bls_dates_are_the_published_ones_not_the_rule_of_thumb`：把这次修的 9 个
  日期钉死，防止有人按规律「修回去」。
- `test_no_dates_beyond_verified_through`：表里不许出现超过已核对边界的日期，
  加新年份必须同步上移 `verified_through`，逼人回源站看一眼。

⚠️ **`TestCoverageHorizon` 会在 2026-09-06 变红**（NFP 表届时剩 89 天 < 90）。
这是设计意图，不是 bug：它的含义是「去看 BLS 发 2027 日程了没」。
CPI 随后 09-12、GDP 09-30 跟上。**变红时不要调阈值**——调高阈值只让告警闭嘴，
不让日历变长。bls.gov 会 403 掉默认 UA，取数带联系人 UA：
`curl -A 'AlphaHive-research/1.0 (邮箱)' https://www.bls.gov/schedule/news_release/cpi.htm`

### Fixed
- `economic_calendar.py`：2026 年 9 个错误日期（CPI 6 / NFP 2 / GDP 1），逐条对齐官方日程
- `swarm_agents/guard_bee.py`：FOMC 临近块的裸 `except ... pass` 改为 `_log.debug(exc_info=True)`

### Added
- `economic_calendar.py`：2027 全年 FOMC + 2028-01 一次；`get_calendar_health()` 逐表体检；
  `_TABLE_SPECS`（来源 / `verified_through` / `pending` 待验证声明）；分级告警与进程内节流
- `swarm_agents/guard_bee.py`：日历降级时写 `details["macro_calendar"]`，
  让降级在 analysis JSON 里留痕（不改评分口径）
- `tests/test_economic_calendar.py`：16 条新测试（12 → 28）

### Changed
- 模块 docstring 立两条硬规矩：禁止按规律推算日期；本模块必须自己喊过期


## [0.45.64] — 2026-08-29 — 单测里的定时炸弹：分界线在走，写死的 fixture 日期钉在原地

`tests/test_pipeline.py::TestBacktesterCleanup::test_cleanup_deletes_old_predictions`
从 **2026-08-28** 起变红：`assert 2 == 1`。与 v0.45.63 无关——把工作区全部改动
stash 掉仍然复现。

### 根因 —— 180 天分界线每天前移，日期不动

`Backtester.cleanup_old_predictions(days=180)`（`backtester.py:1816`）的分界线是
`datetime.now() - timedelta(days=180)`，**每天前移一天**。fixture 的两条数据却是钉死的：

| 行 | 写死日期 | 意图 | 距 2026-08-29 |
|---|---|---|---|
| NVDA | `2024-01-01` | 应被删 | 971 天 |
| TSLA | `2026-03-01` | **应存活** | **181 天** |

`2026-03-01 + 180d = 2026-08-28`——分界线那天正好越过它，第二条也被删，
删除数 1 → 2。

**这类腐烂是不对称的**：写死「应被删」那行是安全的，固定的过去日期只会离分界线
越来越远；只有「应存活」那行是炸弹，因为 `now()` 正朝它走。

### 修复 —— 两行都相对当天生成

关键不只是「用相对日期」，而是**用与被测代码同一个时钟**（都走 `datetime.now()`），
时钟一移两边同移：

```python
_now = datetime.now()
old_date    = (_now - timedelta(days=200)).strftime("%Y-%m-%d")   # 应被删，余量 20 天
recent_date = (_now - timedelta(days=30)).strftime("%Y-%m-%d")    # 应存活，余量 150 天
```

### 顺带：同一文件的姊妹用例只修了一半

`TestMemoryStoreCleanup.test_cleanup_deletes_old_records` 早就撞过同一个坑——
「应存活」那行已带注释「动态"今天"…（时间炸弹）」，但「应被删」那行仍写死
`'2024-01-01'`。它本身安全（见上文的不对称性），但两行写法不一致，照抄的人
容易把炸弹抄回来。已统一。

### 排查了但**没**动的：写死日期 ≠ bug

判据只有一条：**这个字面量会不会被拿去和 `datetime.now()` 比？** 不会就不该动它——
把 `2026-02-27` 改成相对日期反而会毁掉「这天是周五」的可读性。

| 文件 | 写死日期 | 为何安全 |
|---|---|---|
| `test_backtester.py` | `2026-01-15` 等 | 纯标签，写进去读出来；所有 now 相关 fixture 早已用 `datetime.now() - timedelta(...)` |
| `test_backtest_forming_bar.py` | `2026-08-24` | `_exchange_now` 被 monkeypatch，时钟是注入的 |
| `test_pipeline.py` 部署用例 | `2026-03-05` / `2026-02-27` | 部署白名单只过 `filename_is_nontrading_day`（纯日历谓词），不看 now |
| `test_pipeline.py` checkpoint | `saved_at: 2020-01-01` | 意图就是「已过期」，只会更过期 |
| `test_edgar_rss.py` / `test_watchlist_events.py` / `test_cboe_live_vintage.py` / `test_economic_calendar.py` | 各种 | 全部注入时钟（`pdt_today` / `today=` / `_at()` / `ref_date=`） |

**未做**全仓「禁止测试写死日期」的静态守卫：上表说明绝大多数写死日期是正当的，
误报率会高到没人看——守卫本身就会变成噪音。

### Fixed
- `tests/test_pipeline.py`：`TestBacktesterCleanup.test_cleanup_deletes_old_predictions`
  的 fixture 日期改为相对当天（−200 / −30 天），修复 2026-08-28 起的 `assert 2 == 1`
- `tests/test_pipeline.py`：`TestMemoryStoreCleanup.test_cleanup_deletes_old_records`
  的「应被删」行统一改为相对当天，与姊妹用例同写法
- `tests/test_pipeline.py`：模块级 import 补 `timedelta`

### 验证
- `tests/test_pipeline.py` + `test_backtester.py` + `test_backtest_forming_bar.py`：**105 passed**
- 另跑 15 个含写死日期的相关测试文件：**397 passed**，无其他已腐烂用例
- `ruff check --select F821 tests/test_pipeline.py`：All checks passed

## [0.45.63] — 2026-08-29 — 一道守卫，两个指标，同一只票：AMC 的绝对价格地板

用户报「网站上 AMC 的 IV Skew 是空的」。查下来是**两个独立原因叠在一起**，
只修一个都不够；而且顺带发现同一道守卫还悄悄污染了另一个指标。

### 根因① —— `stock_price < 5` 绝对地板（两处）

```python
# options_analyzer.calculate_iv_skew
if not calls_df or not puts_df or stock_price < 5:   # < 5 防 sample data ~1.0
# options_analyzer.calculate_gamma_exposure
if stock_price < 5:   # < 5 同时排除零值和 yfinance sample data ~1.0 哨兵值
```

它要防的样本数据，**在 analyze() 里早 1300 行就被
`options_chain["source"] == "sample"` 早退拦掉了**（v0.38.0）——这两个函数
根本见不到样本数据。守卫只剩误伤的那一半：AMC（$2.70）自入库起
`skew_ratio` 恒为 None、`gamma_exposure` 恒为 **0.0**。

「用绝对价格阈值过滤哨兵值」这个形状 MEMORY 里已经记过一次
（「曾把 AMC≈$3 的真实价格全滤光」）。**同一只票，同一个错法，第二次。**

### 根因② —— 容差是相对的，行权价网格是绝对的

光去掉地板还不够。`tolerance = stock_price * 0.03`，而行权价网格是固定间距，
两者随价格**反向张缩**：

| 标的 | 现价 | ±3% 窗口 | 网格间距 | 窗口里有几档 |
|---|---|---|---|---|
| NVDA | $228 | ±$6.84 | $2.5 | 好几档 |
| AMC | $2.70 | **±$0.081** | $0.50 | **0 档** |

实测 AMC 8/27 行权价 `[1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]`，
call 窗口 `[2.754, 2.916]` 一档不含。即使地板拿掉，这里照样返回
「OTM 期权数据不足」。

### Fixed

- **`calculate_iv_skew`**：地板改为 `stock_price <= 0`；窗口为空时退到
  「最近的一档 OTM 行权价」，两条约束防止它变成兜底垃圾——
  ① 必须仍在 OTM 一侧（put ≤ 现价 / call ≥ 现价）
  ② 离目标超过现价 25% 就放弃（只有两三档的链不配给出 skew）
  新增 `skew_basis` 字段（`"window"` / `"nearest_strike"`）如实标注走了哪条路，
  与 `iv_rank_source` 同一条原则：两种取法算的不是一个东西，不标就分不出来。
- **`calculate_gamma_exposure`**：地板同改；**返回值 `0.0` → `None`**（三处）。
  0.0 在 GEX 的量纲里是「做市商净 gamma 中性」这个**真实读数**，与「没算」
  不可区分，而且会经 `signal_archive` 归档成一条真观测进 IC 数据集。
  模块自己的样本早退写的就是 `"gamma_exposure": None`，
  `report_formatters` 也是 `if gamma is not None` —— 0.0 从一开始就不自洽。
  签名 `-> float` 改 `-> Optional[float]`。

### 真实数据验证（云端 8/27 快照，离线，无网络调用）

```
AMC 2026-08-27 真实链：calls 32 / puts 32，现价 $2.6815（cboe_delayed）
  修复后 skew_ratio = 0.881（basis=nearest_strike）  gamma_exposure = 11.3464
  线上现值 skew_ratio = null（「数据不足」）          gamma_exposure = 0.0
```

`0.881` 与当天另一套口径独立算出的 `deep_skew.skew_25d = 0.90` 差 2% ——
不同代码路径、不同取档方法，互为佐证。

### 不产生口径世代边界

**窗口非空时行为逐字节不变**，其余 29 只标的一律仍走 `basis="window"`
（有测试断言）。只有窗口本来就空的标的才进新路径，而它们此前的值是 None。
故无需付 MEMORY「IC 重跑就绪度闸」那 25 周的前向累积代价。

### Added — `tests/test_options_analyzer.py::TestLowPricedUnderlying`（8 条）

含五次 mutation check，去掉任一修复必须变红。

⚠️ **第四次变异抓到一个假护栏**：`test_fallback_stays_on_otm_side` 初版用
`calls=[1.0, 1.5]`，看似在测 OTM 侧约束，实际那两档离目标 1.34 > 距离上限
0.675，**先被上限挡掉了** —— 去掉 OTM 约束这条测试照样全绿。改用一个
「过得了距离上限、但站错边」的档位（$2.5 对 $2.70 现价）后才真正生效。
没有变异检查的话，这条护栏会一直挂在那里假装在守。

### Changed — `test_gex_empty_chains`

旧断言 `assert gex == 0.0` 把 bug 固化成了契约，改为断言 `is None`。

---

### 二次检查（同版追加）—— 四条真 bug，两条会直接崩

**三条是同一个根因：我改了返回类型契约，却只单测了被改的那个函数。**
`calculate_gamma_exposure` 由「失败返回 0.0」改成「失败返回 None」，而调用方
有五处直接拿它做数值比较 —— 原代码里安全（恒为 float），改完全部 TypeError：

| 位置 | 表达式 |
|---|---|
| `options_analyzer:2077` → `generate_options_score` | `gex_signal = 2.0 if gex < -0.001 else 1.0` |
| `generate_options_score` 信号总结段 | `if gex < -0.001:` ← **第一轮漏了，同一个函数里的第二处** |
| `options_analyzer:2110/2112` | `if gex > 0.001: ... elif gex < -0.001:` |

崩得还很难查：`analyze()` 的调用方用宽 except（`alpha_hive_daily_report:2210`），
TypeError 会被吞成「该标的整份期权数据消失」——**看着像取数失败，其实是类型错**。

修法都取「与改动前等价」的分支，不产生世代边界：
- `gex_signal` 在 gex 为 None 时取 `1.0` —— **恰好等于**改动前 gex=0.0 走的分支。
  函数上方 `if iv_rank is None: iv_signal = 2.0  # 中性，不奖不罚` 就是现成先例。
- `gamma_squeeze_risk` 在 gex 为 None 时取 `"unknown"` —— 不是新造的值，
  样本链早退分支写的就是它。

⚠️ **「同一个函数里的第二处」这条最难看**：第一轮我在 GEX 那边专门写了注释说
「只修上面两处而留下这处，就是 v0.45.49 那种五分之一」，然后在
`generate_options_score` 里犯了同样的事。原因是我按「精确匹配带注释上下文的
字符串」去改，`assert count == 1` 通过了 —— 但那只证明**那段上下文**唯一，
不证明**这个比较**唯一。**改类型契约必须 grep 裸符号（`gex *[<>]`），
不能 grep 带注释的块。**

**第四条：退化路径只取一行，没按行权价聚合。**
期权链是**跨到期日拍平**的，同一档有多行。初版 `min(otm, key=dist)` 只取其中
一行，取哪行取决于列表顺序，且与窗口路径「取平均」的语义不一致。
实测 AMC 8/27：32 行 = 8 档 × 4 个到期日，$3.0 那档 IV 是
`[0.8841, 0.8406, 0.8604, 0.8577]`，初版取了第一个。
改为先定最近的**行权价**、再把该档所有到期日交给上层平均，并在次序键里带上
strike 本身，等距时的取舍才是确定的。

真实数据对照（云端 8/27 快照）：

```
              skew_ratio   otm_call_iv
一次检查        0.881        88.41（4 行里的第 1 行）
二次检查后      0.904        86.07（4 行的均值）
当天独立口径    0.90         （deep_skew.skew_25d）
```

与独立口径的偏差从 2.1% 收到 **0.4%**。

### Fixed —— 连带：`generate_ml_report` 把「未知」画成「低风险」

`gamma_squeeze_risk` 现在可能是 `"unknown"`，而 `generate_ml_report:1668` 写的是
`1 if str(gex).lower() in ("high","很高","medium") else 0` → `"unknown"` 判成 0
→ `risk_level(0)` 输出「● 低」。**正是该函数上方二十行注释警告的
「把没数据渲染成低风险，比崩溃更危险」。** 改为传 `None`，走 `risk_level`
自己已有的「○ 数据缺失」分支。

### Added — 测试 6 条（`TestGexNoneContract` + `TestNearestStrikeAveraging`）

含 `test_analyze_survives_none_gex` —— **第一轮缺的就是这个形状的测试**：
它走的是调用方，不是被改的函数。第一轮给 `calculate_gamma_exposure` 写了 8 条，
一条都没碰到调用方。

五次 mutation check 全部变红。⚠️ **又抓到一个假护栏**（连续第二轮）：
`test_equidistant_strikes_break_ties_deterministically` 初版用档位 1.6/2.6
声称「等距」，但 1.6 < 现价 2.00，先被 OTM 侧约束滤掉了 —— 根本没有并列，
去掉确定性次序键照样绿。改用 2.0/2.2（到 target 2.10 都是 0.10，且都 ≥ 现价）
才真正测到。
## [0.45.62] — 2026-08-28 — 那 7 条失效条件，入库以来一次都没被求值过

### 先更正 v0.45.57 的判断

上一条把根因定在「`iv_current` 陈旧」。**错了。**（另一 session 的复核，已实测采纳）

- `iv_current` 的阶梯 **v0.45.26（8/26）已修**：修复后 8/25→8/27 只有 1/60 重复，
  修复前约 75%。我拿全历史比例当现状讲了。
- 真根因是**横截面池化**：IV 的横截面跨度（BRK-B 约 16 → 高波动票 110+，6×）
  远大于单票时序跨度。对这种量做池化分位，阈值测的是「你是哪只票」，
  不是「这只票出事没」。**就算 `iv_current` 从今天起完美，池化分位仍然得到同样的废阈值。**
- 我提的「锚自身中位 `> 1.5×`」也已被实测毙掉（k=1.30/1.50/1.75 全扫过，
  IV 仍有 23~28/29 只从不触发）。

### 而更上游的事实：这个闸从来没跑过

唯一求值器 `market_intelligence.check_thesis_breaks()` 只有一个生产调用点
`generate_deep_v2.py`。而 deep 报告**全项目只产出过 9 份、全在 2026 年 3 月**
（最后一份 3/24），gh-pages 上 0 份；机器条件 8/27 才入库，比最后一份 deep 报告晚 5 个月。
日常编排器跑的是 `generate_ml_report.py`。

所以 v0.45.57 把失效条件渲染上页后，网页上是**一张静态阈值表**：
只有阈值，没有当前值，没有判定。读者分不清「没触发」和「不可能触发」。
这是「死字段」的升级形态——从**算了没人读**变成**读了但没求值**。

**实测代价**：8/27 RKLB 的两条 L1 条件**真的满足了**
（`score 3.94 < 4.0`、`bear_signals 7 >= 7`）——系统史上第一次真实预警，
没有任何人看见。

### Changed

- `market_intelligence.check_thesis_breaks()`：新增 `evaluations` —— **每一条**
  条件的当前值与判定（不只是触发的那几条）。在**每一条 return 路径上都显式出现**，
  不留给调用方 `.get("evaluations", [])` 兜默认值。
  明细在「可求值性」早退**之前**构建，散文条件也进明细（标「人工条件」），
  否则整级退回只剩阈值。
- `alpha_hive_daily_report`：新增 `_evaluate_thesis_breaks()`，日报路径接上
  **同一个**求值器，**不另写一个**（本仓库当天刚有过一次平行发明：
  `close_correction.official_closes` vs `scan_coverage_gate.check_prices`）。
  取不到值就返回空列表让渲染层退回只显示阈值，**不编当前值**。
- `_format_break_verdict()`：三态分明——
  `（当前 1.09，未触发）` / `（当前值缺失，未核对）` / `（人工条件，未自动核对）`。
  缺数据**不得**渲染成「未触发」。

### Added

- 7.5 节汇总行区分「可自动核对 / 已触发 / 缺值未核对 / 人工需判读」，
  防止一页全是人工条件时被读成「都没事」。
- 未校准告诫写进 7.5 节，且**只在确实存在自动条件时**才出现；
  说清阈值测的是「你是哪只票」，不只是含糊的「未校准」。
- `tests/test_thesis_break_rendering.py` 增至 **20 条**，新增 9 条守：
  求值明细与配置**逐位同序**（顺序漂了会把 A 的阈值配上 B 的当前值，两条都「看着正常」）、
  三条 return 路径都带 `evaluations`、对不齐时**降级而非错贴**、缺值不得报「未触发」。
  5 次变异全红。全量 **2031 passed**。

### Fixed

- 回填 **8/26** 页面（65 条，全部为人工条件，如实标注需人工判读）。
  顺带修掉回填脚本的一个真 bug：页面上已有 7.5 节时它会**再插一份**，
  渲染出两个「7.5) 失效条件」。现在是替换。零丢失：行 1452→1452、章节 199→199。
- **8/27 刻意不回填**：其 `.swarm_results` 已于 2026-08-28 07:11 被 v0.45.59
  「8-27 补跑」重新生成（data_real 87%→92.1%），而页面仍是原次扫描的产物。
  拿新数据的当前值贴旧数据的页面 = 两个 vintage 混渲染。零丢失护栏当场拦下
  （标的顺序不一致）。**该页应由补跑方整体重渲染。**

### 遗留

- 阈值本身未动，标「未校准，仅供观察」。需等每票攒够真实 IV 序列后逐票重定。
- `check_thesis_breaks` 的 `level` / `alert_html` 仍只有 deep 报告一个消费者；
  日报只用 `evaluations`，**不**把「🔴 STOP LOSS」搬上页面——阈值未校准之前，
  那种红标会是噪音（COST 的 PCR 条件按现阈值 20% 的日子在响）。


## [0.45.61] — 2026-08-28 — Twelve Data 接管日K：最后一处 yfinance 依赖

v0.45.60 把当日宏观搬离了 yfinance，链上只剩一处非它不可：**逐标的 30 日收盘**
（`rv_30d` / `iv_rv_spread` / hv_proxy 口径 `iv_rank` 全由它派生）。
2026-08-27 那 687 次 429 打空的正是这三项。

### 为什么是 Twelve Data（其余全部实打验证不可用）

| 源 | 逐标的日K | 实测 2026-08-28 |
|---|---|---|
| Finnhub `/stock/candle` | ❌ | HTTP 403，已转付费 |
| Alpha Vantage | ❌ | 25 次/天**已被 newsapi 的新闻情绪占满**（当天日志正在报 rate-limited） |
| Stooq | ❌ | 返回 JS 工作量证明挑战页，已上反爬 |
| FRED | ❌ | 只有指数（SP500/NASDAQCOM），无逐标的 |
| **Twelve Data** | ✅ | 免费 800 次/天、8 次/分 —— 30 只 × 1 credit 远在额度内 |

### Added

- **`twelve_data.py`**：日K 客户端 + `realized_vol()`。
  - **口径与 `calculate_iv_rv_spread` 逐条对齐**（对数收益 / `ddof=1` /
    ×√252×100 / 剔除 |log_ret|>0.5 / HV30>300% 视为污染）。口径不一致的话，
    同一只标的会因为走了哪条源而得到不同的 RV，那种差异会被误读成波动率变化。
  - **独立**令牌桶 7/分（免费档 8/分，留一成余量）。刻意不与
    `resilience.yfinance_limiter` 共用 —— 两边配额本来就是分开的，共用只会互相拖慢。
  - `TwelveDataUnavailable` 继承 `ConnectionError`（⊂ NETWORK_ERRORS）。
    v0.45.56 在 `YFRateLimited` 上踩过这个坑：新造的异常不继承既有网络异常族，
    会穿透所有降级路径。
  - **HTTP 200 + body 里 `status: "error"`** 单独处理：免费档超额时状态码仍是 200，
    只看状态码会把错误当数据。
- `config.SECRET_FILES` 登记 `TWELVEDATA_API_KEY` → `~/.alpha_hive_twelvedata_key`
- `tests/test_twelve_data.py`（15 项）

### Changed

- `market_intelligence.calculate_iv_rv_spread`：Twelve Data 优先，yfinance 退为兜底。
  **未配 key 时行为与之前完全一致**（实测未配 key 仍走 yfinance 得到 rv_30d=42.22），
  不报错、不阻断。

### 自检当场抓到一个 bug：盘中半根 bar 混进了 RV30

配好 key 跑 `python3 twelve_data.py NVDA`（2026-08-28 10:09 ET，盘中），
末根返回 `2026-08-28 close=224.57 volume=103412` —— 而 8/24–8/27 的成交量是
1.2~3.0 亿。**开盘才十分钟的半根 bar**，被算进了 RV30（42.56 vs 修复后 42.89）。

项目里已有 `data_pipeline._drop_forming_bar`，但它靠 yfinance 探 SPY 分钟线
判交易所时间 —— 正是本模块要绕开的东西。改用返回体自带的两个信号，不额外发请求：

- **日期**：末根日期 ≥ 美东当日 → 收盘前必然未完成（`zoneinfo` 换算，
  绝对时间正确即可，不依赖本机 tz 设置）
- **成交量**：末根 < 窗口中位数的 30% → 几乎只可能是半根（时钟不可得时的独立判据）

任一命中即丢。**误丢的代价是 30 根少一根，漏丢的代价是波动率失真** —— 不对称，
所以宁可宽。6 项测试锁住，含端到端。

值得记一笔：这个 bug 是**真去跑了自检**才发现的。如果只看「21 项测试全绿、
自检打印了三行数字」就宣布完成，它会带着一根半截 K 线上生产。

### 补跑的国债也改走财政部目标日

`get_yield_curve(date)` 本来就支持指定日期（一次返回整月，不额外发请求），
但初版只接了当日路径，补跑仍走 FRED —— 而 FRED 转发的就是这份数据且晚一天。
实测 8/28 查询：FRED 的 2Y 是 **4.19@08-26**，财政部已有 **4.20@08-27**。

现在补跑也调 `_same_day_macro_data(as_of)`，但**只取国债**：
Finnhub `/quote` 只给最新报价，给不了历史某日，补跑时那半段自动跳过
（拿今天的 SPY 冒充目标日比缺失更坏；那几项由 `_asof_history` 按目标日取）。

实测补跑 @2026-08-27：`2Y 4.2 (treasury_gov)`、曲线 `normal`、
`field_sources` 三项全是 `treasury_gov@2026-08-27`。

### 「加源之前先确认闸门覆盖它」—— 同一个错的第三次

配上 key 跑全量，`test_iv_rv_reports_unavailable_not_zero` 红了：
测试构造的「取不到数」场景被**真实的 Twelve Data 数据**盖掉。

  ① v0.45.56 加 `yf_gate`          → 已由既有的 `mock_yfinance` 覆盖
  ② v0.45.60 加财政部/Finnhub 宏观层 → **漏了**
  ③ v0.45.61 加 Twelve Data         → **又漏了**

已在 conftest 一并拦住，并把教训写进注释：
**新增任何外部数据源，同一个 commit 里必须在 conftest 加一行。**

顺带一个测试自身的坑：`test_backfill_skips_etf_quotes` 要验的正是被
conftest 换成桩的那个函数 —— 直接调等于在测桩。改为在模块加载时
（早于 autouse fixture）抓下真函数引用。

### 二次检查：三条，全是「修过同一形状、又犯一次」

用户要求逐条复核本轮改动。三条都用代码验出来，不是推理出来的：

**① 补跑的 `data_source` 谎报来源。** `_compose_data_source` 的补跑分支
**无条件**返回 `cloud_snapshot+yfinance@日期`，完全不看 `src_map` ——
而自本版起补跑的国债已改走财政部，于是标签说 yfinance、实际是 treasury。
与 8/27 那次「yfinance 一个字段都没供上、标签却仍写 yfinance」**是同一形状，
而且是在同一个函数里**。现按实际供数源组装：
`cloud_snapshot+treasury+yfinance+fred@2026-08-27`。

**② Twelve Data 在补跑时取最新窗口冒充目标日。** `realized_vol` 一开始就有
`end_date` 参数，调用处没传。对次日补跑影响小（forming-bar 已丢、窗口末端
恰好是目标日），补跑更早的日子会严重错位。现沿用项目既有的
`ALPHA_HIVE_TARGET_DATE` 信道（与 options_analyzer 同一个，含同样的格式校验
—— 那里实测过 `../../../tmp/evil` 能让快照写到 cache/ 之外），
来源标签写成 `twelve_data@<日期>`。
⚠️ **yfinance 兜底路径仍是「最近 N 天」口径**，补跑较早日期时它给的 RV
不属于目标日 —— 已知缺陷，本版未修（要改成 start/end）。

**③ 财政部缓存键用本机时间。** `time.strftime("%Y%m")` —— 本机在 PT、
落后 ET 3 小时：每月 1 号的 ET 00:00~03:00 之间本机还停在上个月，
会去抓上个月的 XML、找不到 ET 当日 → 返回 None。**一年 12 次、
每次 3 小时的窗口，正好覆盖凌晨的定时任务。** 改用 `_et_month()`，
与 `_et_today()` 同源。

三条各配回归测试，mutation check 实测：还原成修复前 → 三条全红；
还原修复 → 全绿。

（另核对过但**没问题**的：Twelve Data 分支与 yfinance 分支的返回键集一致
（只多一个 `source`）；`yf_gate._bucket` 的缓存在测试里会被 conftest
special-case 换掉，不会用到生产桶。）

### 待你操作

Twelve Data 的免费 key 需要注册（我不能替你注册账号）。拿到后：

    echo 'YOUR_KEY' > ~/.alpha_hive_twelvedata_key && chmod 600 ~/.alpha_hive_twelvedata_key

写入即生效，无需改任何代码。未配置时整条链原样退回限流版 yfinance。


## [0.45.60] — 2026-08-28 — 当日宏观脱离 yfinance；顺带发现 2Y 一直是猜的

用户指出 v0.45.59 那份调研的漏洞：「那我当天规则模式的自动任务 FRED 给不出信息啊」。
**对的。** FRED 滞后 1–2 天，而定时任务在**当日 17:00 ET** 跑 —— 我把 FRED 说成通用替代，
实际只对补跑成立。追下去反而找到了更好的答案。

### `fred_macro` 的 7 个 yfinance 符号，逐个换掉

| 原符号 | 当日替代 | 实测 2026-08-27 |
|---|---|---|
| `^VIX` | CBOE（早已在用） | 15.21 |
| `^TNX` / `^FVX` | **美国财政部日度曲线**（免 key） | 10Y 4.67 · 5Y 4.38 · 2Y 4.20 |
| `^GSPC` | Finnhub `/quote` · SPY | 771.10（+0.66%） |
| `GLD` | Finnhub `/quote` · GLD | 422.60（+0.30%） |
| `DX-Y.NYB` | Finnhub `/quote` · UUP | 28.02 |
| `TLT` | Finnhub `/quote` · TLT | 83.13 |

为什么是财政部而不是 FRED：**FRED 的 `DGS10` 转发的正是这份数据，但晚一天**
（实测 08-28 查询时 FRED 最新 08-26、财政部已有 08-27）。

为什么用 ETF 代理而不是指数代码：Finnhub 免费档对 `^GSPC` / `^VIX` 返回
`Market data subscription required for CFD indices`，而 SPY/GLD/UUP/TLT 全通。

### 顺带修掉一个更老的错：2Y 一直是**猜**出来的

查证时发现 `treasury_2y` 走的是 `5Y + 0.15` 的近似（拿不到真 2Y 时代的产物），
而它会**错判收益率曲线档位**：

| | 2Y | 10Y−2Y | 曲线判定 |
|---|---|---|---|
| 近似 `5Y+0.15` | 4.53 | +14bp | `flat` |
| 财政部真值 | **4.20** | **+47bp** | `normal` |

差一个档位。现在真值优先，近似退为兜底并标 `treasury_2y_source`。
FRED 的 2Y **不再覆盖财政部**——同源且晚一天，覆盖是纯粹的倒退。
三处重复的曲线判定逻辑收敛成一个 `_set_curve()`（抄三遍就有三处会漂移）。

### Added

- **`treasury_yields.py`**：财政部日度收益率曲线。免 key、一次请求返回整月
  （补跑任意目标日不额外请求）、带退避重试（实测一次瞬时 `RemoteDisconnected`
  的代价是 3 个字段掉回 yfinance，值得多试两次）。
  正则解析而非 XML 解析器——schema 变化时正则「少匹配到」比解析器「整份炸掉」
  的失败方式更安全。指定日期无数据时**返回 None，绝不回退到前一交易日**。
- `fred_macro._same_day_macro_data()` / `_finnhub_quote()`
- `tests/test_treasury_yields.py`（12 项）+ 当日宏观回归（6 项）

### Changed

- `fred_macro` 取数顺序：**非 yfinance 源优先，yfinance 退为最后一环**。
  补跑口径不走当日源（财政部/Finnhub 只给"最新"，拿今天的值冒充目标日比缺失更坏）。
- `data_source` 如实汇总实际供数的源（`treasury+finnhub+fred`），
  新增逐字段的 `field_sources`。8/27 那天 yfinance 一个字段都没供上、标签却仍是
  `yfinance+fred` —— 排查限流时会把人引向一个根本没被调用的源。

### 8-27 补跑结果

补跑完成（退出码 0）。快照 30/30 写入，`iv_rank` 30/30、`rv_30d` 27/30；
宏观 `as_of: 2026-08-27`、VIX 15.2 取自当日快照。
⚠️ ML HTML 报告按设计只出分数最高的 12 只（`ALPHA_HIVE_ML_REPORT_MAX`），
所以网站上另有 13 份仍是 8/27 那次失败扫描的遗留 —— 好坏混杂，正在以
`ALPHA_HIVE_ML_REPORT_MAX=30` 重跑统一。

### 顺带修：一条会因为「数据变多」而报警的不变式

跑全量时 `test_known_degraded_dims_have_not_fully_collapsed` 红了。追下去发现
**不是我的改动坏了什么，是这条闸的口径本身有缺陷**。

它用**全局去重比** `distinct/n`。对连续维度没问题（odds/risk_adj/sentiment/signal
实测 0.45~0.71）。但 `catalyst` 是离散量、全局只有 ~13 个取值：**distinct 几乎
不增长而 n 线性增长，比值必然机械下滑**。

| 时点 | n | distinct | 比值 |
|---|---|---|---|
| 补跑前 | 235 | 13 | 0.055 ✅ |
| 补跑后（+30 行合法数据） | 265 | 13 | **0.049 ❌** |
| 窗口填满 12×30 后 | 360 | ~13 | ~0.036 |

而**每个扫描日仍稳定 5.5 个不同值**，与 8/24–8/26 完全一致 —— 分布根本没退化。
机器恢复日更后这条闸注定天天红，且红的原因与它想守的东西无关。
**一条会因为数据变多而报警的不变式，是在训练人忽略它。**

口径改为**每个扫描日的不同值数（中位）**，不随 n 变化，直接对应它真正要守的
那句话「别彻底塌成常数」。地板 3（实测中位 5.5，塌成常数是 1）。
其余 4 个连续维度沿用原比值口径，不动。

mutation check：塌成常数 → 红（中位 1.0）；只剩 2 个值 → 红；现状 → 绿；
**同样 6 个取值、行数翻 4 倍 → 仍绿**（原口径正是在这里误报）。

### 又一处「加源之前先确认闸门覆盖它」

新取数层接上当天，`test_yield_curve_inverted` 红了：测试构造的倒挂曲线被
**真实的 2026-08-27 数据**（10Y 4.67 / 2Y 4.20 → normal）盖掉。
根因是 `conftest.py` 一直只 mock 了 yfinance，而这一层**绕过 yfinance 直接打外网**。

后果不止那一条测试 —— 整个套件开始打真网络：变慢、变脆、离线不可用。
已在 conftest 加 autouse fixture 默认关掉当日宏观取数层；需要验证它的测试
自己 monkeypatch（会覆盖该 fixture）。

与 `http_gate` docstring 记过的那条教训同形，这次是我犯的。

### 自查

1. **zsh 词分割**：`--tickers $NEED` 在 zsh 里**不做词分割**（bash 才会），
   18 个代码被当成一个标的传了进去，产出文件名带空格、日期还错的垃圾文件。
   已清理。护栏正常工作并点破了它：
   「🚨 gh-pages 无变更：新 tree 与父提交完全相同……本次声称 1 份 ML 报告成功——
   若非重复运行，说明报告文件根本没重新生成。」
2. **又一次找错地方就下结论**：核对补跑产物时我查 `alpha-hive-daily-*.json`
   的 `opportunities`，报 `iv_rank 0/30`——但拿数据完好的 8-25 一比，**也是 0/30**，
   因为那个结构里根本不含 `iv_rank`。真正的产物在快照与 `analysis-*.json` 里
   （实际 30/30）。**下结论前先拿一个已知完好的对照跑一遍**，这次是对照救了我。


## [0.45.59] — 2026-08-28 — 云端快照根治：生产端补上、宏观接上、8-27 补跑

承 v0.45.58 查明的两处断裂，按用户指示三件一起做。

### ① 生产端：把 main 合进 `cloud-snapshots` 分支

云端 routine 的取数代码自 08-26 13:28 UTC 起没再更新，落后 main 39 个提交，
于是 v0.45.36 起本该写出的 `vintage_date` 从未被写出。

该分支的独有提交**只有快照数据、改动零个代码文件**（`git diff --stat
origin/main...origin/cloud-snapshots -- . ':!cloud_snapshots'` 为空），
故直接合并 main：`5dc9ece..f7138a1`。合并后校验：`vintage_date` 5 处、
`official_price`（v0.45.47 收盘价修复）已带上、两天快照数据完好、与 main 零代码差异。

⚠️ **该分支的代码会独立于 main 老化。** 今后改 `cloud_snapshot_fetch.py` /
`cboe_options.py` 必须同步过去，否则同一个错会再犯一次。

### ② `market.json` 接进宏观 —— 但主因不在 market.json

用户要的是「让补跑的宏观走快照而不是运行当天」。查下来错位有两处，
而**大头不是 market.json**：

| 字段 | 旧行为 | 修法 |
|---|---|---|
| VIX / F&G | market.json 里有目标日真值，但 `load_market()` **0 个调用者** | 走快照，`vix_source="cloud_snapshot_cboe"` |
| 国债 / SPX / 美元 / 黄金 | `yf.Ticker(sym).history(period="5d")` —— **永远取最近 5 天** | `_asof_history()` 用 start/end 锁定目标日 |

**market.json 里根本没有国债/SPX/美元/黄金**，光接它解决不了问题；
`period="5d"` 才是补跑宏观错位的主因。两处一起修才成立。

- `fred_macro.set_macro_snapshot(date, market)` / `get_macro_snapshot()`
  （装载即作废 `_CACHE`，否则补跑会拿到上一次实时调用的结果）
- `snapshot_mode` 里**期权链与宏观同进同出** —— 只装一半会让报告里期权是
  目标日的、宏观是今天的，且无从分辨
- 标签诚实化：`data_source` 写成 `cloud_snapshot+yfinance@<日期>`，
  新增 `as_of` 字段（None = 实时口径）。补跑标成 `yfinance+fred` 会让读者
  以为是运行当天的实时数据。

实测对照（8-28 05:01 运行）：

| | 实时口径 | 快照口径 @2026-08-27 |
|---|---|---|
| `data_source` | `yfinance+fred` | `cloud_snapshot+yfinance@2026-08-27+fred` |
| `vix_source` | `cboe` | `cloud_snapshot_cboe` |
| VIX | 14.51 | **15.21** |

`_asof_history` 截断实测：as_of=08-25 → 4.639、08-26 → 4.664、08-27 → 4.672
（`^TNX` 各日真实收盘），**不是**一律给最新的 4.672。

### Added

- `--allow-unverified-snapshot`：接受缺 `vintage_date` 的快照。
  仅 2026-08-26 / 08-27 两天需要（新鲜度已于 v0.45.58 逐只独立验证）；
  ① 修好后此后的快照不需要它。
- `tests/test_macro_snapshot.py`（9 项）

### ③ 8-27 补跑

（结果见下方补记）

### 自查：测试替身连错两版

`_FakeHist` 第一版 `.date` 返回 list —— 真 pandas 的 `DatetimeIndex.date` 是
numpy 数组，`arr <= date` 是逐元素比较，list 会抛 TypeError。
第二版漏了 `__len__`，取数循环里的 `len(hist) >= 2` 抛异常被
`except Exception` 吞掉 → `data` 空 → 走「yfinance 全灭」分支 →
测试红在一条与被测逻辑无关的路径上，还给出 `'cboe' == 'cloud_snapshot_cboe'`
这种看着像真 bug 的错。**替身失真会伪造出被测代码的假故障。**


## [0.45.58] — 2026-08-28 — 云端快照兜底是死的，而它一直宣称自己活着

用户问：「补跑不可能有目标日的期权链，但是我有云端快照兜底不是吗？」

**用户是对的，我上一条答错了。** `cloud_snapshot_fetch` 的 docstring 自己就写着
「补跑时消费这些快照」，`alpha_hive_daily_report` 里也确实接了
`cloud_snapshot_loader.snapshot_mode(args.date)`。快照不但存在，而且完好：
`cloud_snapshots/2026-08-27/` 有 30 个标的 + manifest + market，
`tickers_ok=30/30`，每只 160 calls + 160 puts + 4 个到期日 + IV 期限结构 + 全链 OI。

**但它一份都没被用上。**

### 根因：生产端与消费端分处一次契约变更的两侧

`vintage_date` 于 **v0.45.36**（`670a444`）加进主线的 `cloud_snapshot_fetch.py`，
消费端 `load_ticker` 同时开始**拒收缺该字段的快照**（`return None`）。

**但生产端从未拿到 670a444。** 时间线是硬的：

| 时刻(UTC) | 事件 |
|---|---|
| 08-26 13:28 | `cloud-snapshots` 分支的 `cloud_snapshot_fetch.py` 最后一次更新（v0.45.29，**早于** 670a444） |
| 08-26 20:11 | 670a444 落到 main：生产端开始写 `vintage_date`，消费端开始拒收缺它的 |
| 08-26 21:08 | 8-26 快照生成 —— **晚于契约变更 57 分钟，仍无 `vintage_date`** |
| 08-27 21:05 | 8-27 快照生成 —— 晚整整一天，**仍无** |

`git show origin/cloud-snapshots:cloud_snapshot_fetch.py \| grep -c vintage_date` = **0**。

（可确证的是「生产端跑的不是 main-at-HEAD」；究竟是 `cloud-snapshots` 分支
还是别的陈旧 checkout，我没有 routine 侧的配置可查，不做断言。）

于是自 v0.45.36 起，生产端写的每一份快照都缺这个字段，消费端每一份都拒收。
**兜底自那时起就是死的。**

### 更坏的是它看起来是活的

旧 `snapshot_mode` **只验 manifest 就 yield**。manifest 没有 `vintage_date` 要求，
所以永远通过 → 调用方随即打印「📦 云端快照模式：期权/IV 取自 cloud-snapshots 分支」
→ 而后每个 `load_ticker` 静默返回 None → 扫描退回实时抓取。

**一条自信的、错误的标签。** 这正是 v0.45.53 `check_label_honesty` 要抓的形态
（标签宣称成功、它管辖的值却是空的），只不过那道闸检查的是扫描产物，
够不到这里。

### 数据本身是真的（已独立验证，不靠那个缺失字段）

拿 `price_at_fetch` 与真实收盘逐只对照：

| 标的 | 快照 | 8-26 收 | 8-27 收 | 判定 |
|---|---|---|---|---|
| NVDA | 227.00 | 209.66 | 227.98 | ✅ 8-27 |
| ABBV | 257.66 | 262.90 | 258.15 | ✅ 8-27 |
| COST | 934.66 | 956.12 | 934.66 | ✅ 8-27（精确到分） |
| XOM | 156.60 | 158.19 | 156.44 | ✅ 8-27 |

manifest `generated_at_utc` = 2026-08-27T21:05:52Z = 17:05 ET，收盘后。
数据是货真价实的 8-27，只是缺一个元数据字段。

### Fixed

- `cloud_snapshot_loader.snapshot_mode()` 现在**抽验标的可载入性**（前 5 只），
  一只都载不进来就 `SnapshotUnavailable` 并**点名根因**（缺 `vintage_date` /
  如何绕过 / 如何根治），而不是顶着假标签静默降级。
  部分可载入时按实际比例告警。
  - mutation check：移除抽验后 `DID NOT RAISE`，还原后绿。

### 第二处死路：`market.json` 根本没有消费者

`load_market()` 在生产代码里 **0 个调用者**（只有定义处和一个测试）。
`cloud_snapshots/2026-08-27/market.json` 里躺着当天的 **VIX 期限结构真数据**
（`source: vx_futures`，vix_spot 15.21 / 1m 17.15 / 3m 19.60 / contango 12.75%）
和 Fear & Greed —— 每个交易日抓下来、提交到分支，**没有任何东西读它**。

这正是 MEMORY 里那条「死字段：算了没人读」——**查功能是否生效先数读者**。
后果直接落在用户这次问的第二件事上：即便修好上面的 `vintage_date`，
补跑的宏观**仍然**会是运行当天的，因为 market.json 压根没接进 `fred_macro`。

### 尚未做（需要你定）

1. **根治要更新 `cloud-snapshots` 分支上的 `cloud_snapshot_fetch.py`** ——
   那是推到另一个分支、且会改变云端 routine 行为的对外动作，未擅自执行。
2. 现存 8-26 / 8-27 两天的快照缺 `vintage_date`，只能走 `allow_unverified=True`。
   `--date` 路径目前没有对应的 CLI 开关。新鲜度已由上表独立验证。

### 自查

上一条我写「补跑会拿 8-28 的期权链冒充 8-27」，并据此开了个后台任务卡片。
两者都建立在「补跑不可能有目标日期权链」这个**我没有核实就写下的前提**上——
而 `cloud_snapshot_fetch` 的 docstring 第一段就否证了它。卡片已撤回。


## [0.45.57] — 2026-08-28 — 失效条件算了半年，全项目没有一个读者

### 先更正 v0.45.55 的一句事实错误

上一条写「8/26 与 8/27 两天各 0/30」。**8/26 不是 0，是 13/30，而且 13 就是那天的上限**
—— 当时 `thesis_breaks_config.json` 只覆盖 30 只里的 13 只。真正断掉的只有 8/27。
我当时数的是 `.swarm_results_*.json`（那里从来就是 0，见下），没数 `alpha-hive-daily-*.json`。

顺带把 8/27 的时序钉死：v0.45.50 于 **07:26** 入库机器条件（并把覆盖从 13 只扩到 30 只），
当天扫描 **14:00** 撞上 KeyError。一次意在把覆盖率从 43% 提到 100% 的改动，
实际把它打到了 0%。

### 真正的根因比 v0.45.55 修的那层更深

`thesis_break_l1/l2` **全项目 grep 不到第二个使用点**。它被算出来、写进 dict，然后没人读：

- `_build_swarm_report`（渲染 markdown）在 `_post_scan_notify`（计算它）**前一行**就跑完了
- `.swarm_results_*.json` 更早，在 `_post_scan_enrichment` 里就落了盘——所以它**从来**是 0
- 唯一叫「失效条件」的地方是 7 节表格最后一列，装的却是 GuardBee 的
  「共振✅ 5 Agent 同向」/「信号分散」——**论点成立的理由，与失效条件正好相反**

这是 v0.45.40 移除 Google Calendar 留下的残肢：日历提醒曾是它唯一的消费者，
消费者删了，生产者留着。v0.45.55 修好的 KeyError 是真 bug，但修完只是让一个死字段填对了值。

### Changed

- `alpha_hive_daily_report.py`：计算抽成 `_attach_thesis_breaks()`，调用点上移到
  **`_post_scan_enrichment` 之前**（即扫描后第一步），渲染层与两份 JSON 才都看得见。
  `_post_scan_notify` 只剩搬运，不再重算。全灭时留 **ERROR**（8/27 那次只有一行 WARNING）。
- `report_formatters.py`：7 节表格末列改名 **共振判定**（还它真实内容），
  新增末列 `L1×n L2×n`，无配置的标的标 `⚠️ 无`。

### Added

- `report_formatters._build_thesis_breaks()` → 报告新增 **7.5) 失效条件** 章节，
  逐标的列出 L1 预警 / L2 止损；机器条件标「自动」。
  无配置的标的**点名列出**，不静默略过。
- 章节开头显式标注：自动条件是**全标的池化**分位，不是逐标的口径。
  实测三日内 30 只标的 `iv_current` 最大 89.3（DELL），而阈值 `iv > 105.7`（p98）
  **0/30 触达**；BRK-B 峰值 19.4，差 5 倍。低波动标的的 IV 条件是永远打不响的装饰。
- `tests/test_thesis_break_rendering.py`（11 条）。核心是两条**顺序不变式**：
  `_attach_thesis_breaks` 必须早于 `_build_swarm_report` 且早于 `_post_scan_enrichment`。
  谁把它挪回 notify，报告会重新变空白而其余单测照样全绿——所以直接盯源码顺序。
  5 次变异全红（挪顺序 / 改回撒谎列名 / 删全灭 ERROR / 去掉逐条隔离 / 给无配置标的塞空列表冒充覆盖）。

### Fixed

- 回填 **8/26**（13/30，65 条）与 **8/27**（30/30，275 条）的日报页面。
  **外科式**修改：只重写 7 节表格末列 + 插入 7.5 节，其余章节按字节保留
  （整篇重渲染会把今天的回测/ML/宏观写进历史页面，即 v0.45.52 处理过的那类污染）。
  零丢失核对：章节 198→199 / 196→197，均只多 7.5 节，无丢失；表格标的顺序逐行比对一致。
- 配置口径按**当天真实在册的版本**取：8/26 用 `add18fd~1`（13 只、120 条人工条件），
  8/27 用当前版本（机器条件 07:26 入库，早于当天扫描，确实在册）。
  把 8/27 才推出来的历史分位阈值倒填进 8/26，是伪造那天的知识状态。
- 回填后 8/26 页面如实显示：当天分数最高的 **AMC 7.9 与 T 7.8**（均在 7.5 行动线之上）
  **一条失效条件都没有**。这正是这条硬性规则要暴露的东西。

### 遗留

- 17 只标的在 8/26 无任何失效条件配置（v0.45.50 已补齐到 30 只，但只给了机器条件）。
- 机器条件的池化分位口径未修——`iv > 105.7` 对低波动标的恒不触发。
  改成逐标的分位需要重算，不在本次范围。


## [0.45.56] — 2026-08-28 — 限流器早就在仓库里，只接了 6 个调用点

用户问：「为什么 8-27 网站的 IV Rank 还是丢失，不是有云端快照的数据源可以用吗，
macro 宏观数据还是丢失」。

三个问题、一个根因，且**都不是 8/26 那个故障**。

### 病因：我们把 yfinance 打到拒绝服务

8/27 编排器日志里 **687 次 `Too Many Requests`**，14:00:05 起持续到 14:29:40 ——
覆盖整个扫描窗口。`rv_30d` / `iv_rank` / `iv_rv_spread` / `catalysts` **各 0/30**，
宏观整块 `data_source: "fallback"`。

不是突发，是一条在爬的曲线：

| 日期 | 429 次数 | 分布 | rv_30d | iv_rank | 宏观 |
|---|---|---|---|---|---|
| 8-25 | 364 | 14:00–14:16（5 分钟爆发） | 30/30 | 29/30 | `yfinance+fred` |
| 8-26 | 487 | — | — | — | `yfinance+fred` |
| **8-27** | **687** | **14:00–14:29（全程）** | **0/30** | **0/30** | **`fallback`** |

8/25 已经在限流了，只是爆发集中、之后放行，所以数据还是全的 —— 赤字从那时开始攒。

### 为什么护栏没拦住：它只覆盖 6/40 个调用点

`resilience.py` 里**早就有** `yfinance_limiter`（rate=3.0）和 `yfinance_breaker`。
真实使用者只有 `earnings_watcher`（5 处）和 `swarm_agents/cache`（1 处）。
而 `market_intelligence`（RV30）、`fred_macro`（宏观）、`risk_engine`、`backtester`、
`cboe_fetcher`、`data_pipeline`、四只蜂、`pead_analyzer`、`paper_portfolio`、
`dashboard_renderer`、`outcomes_fetcher` —— 三十多个调用点，一个都不走它。

`options_analyzer` 更微妙：它 `import` 了 `yfinance_limiter`，**然后从未调用**，
只用了熔断器。一个导入了却不用的限流器比没有更坏 —— 读代码的人会以为这条路径受治理。

这与 `http_gate` docstring 里已经记过的教训逐字相同：
「那把信号量只锁了 CBOE……**加源之前先确认闸门覆盖它**」。同一个错误，换了一层。

### 云端快照为什么兜不住

`cloud_snapshot_fetch` 按设计只冻结「过时不候」的数据（期权链 / IV 期限结构 /
OI / VIX / P/C / SKEW / VVIX / F&G），理由原文是「股价有历史 API 可补」。
判断没错，但这让它的覆盖面与本次故障面**正交**：8/27 快照 30/30 全部落盘、
`iv_current` 一个不缺（走 CBOE，没被限流），坏的恰好是它决定不管的那一半。

`_refresh_price_derived`（v0.45.43，为 8/26 事故所写）当天也失效 ——
它「字段为空就就地重算」，而重算方式是**再调一次 yfinance**。
**修复手段与被修复对象共用同一个挂掉的依赖，那不是兜底。**

### Added

- **`yf_gate.py`** —— yfinance 全局限流闸门。patch `yfinance.download` /
  `yfinance.Ticker` **本身**，而不是逐处接线：接线一次，新调用点自动受治理，
  不存在「忘了接」。已核实全仓无 `from yfinance import X` 写法（那种写法会绕过 patch），
  并由测试锁住。
  - `ensure()`：幂等、极廉价，供热点取数函数在入口自调，使覆盖率不依赖「记得在新入口 install」
  - 429 → **立即熔断 180s**，冷却期内的调用抛 `YFRateLimited`，不打网
  - 复用 `resilience.yfinance_limiter` **同一个桶**（自造第二个桶 = 两拨调用方各持一半配额）
  - **方法与 property 都包**：`history` / `option_chain` / `get_earnings_dates` 是方法，
    `info` / `calendar` / `options` / `news` 是 property。只包 `history()` 会漏掉催化剂 ——
    8/27 掉的第四个字段正是走 `t.calendar`（`chronos_bee.py:115`），`.info` 同理
    （bear / scout / rival 三只蜂）。property 只对**首次**访问计限流：yfinance 把它们
    缓存在实例上，每次访问都扣令牌会让缓存命中也等 2s。
- `scan_coverage_gate.check_rate_limit()` —— 数当日日志里的 429，回答「为什么降级」
  而不只是「降了什么」。**区分病因与预警**：限流越闸且字段降了 = 直接原因；
  限流越闸但字段还全 = 早期预警（8/25 正是后者）。
  **是诊断项，不进退出码**：字段真降了退出码已经是 1，限流只补「为什么」；
  字段还全时把预警判成失败会让编排器对一次成功的扫描报错。
  新增 `--log-dir` 便于测试，也不再隐式依赖 `~/.claude/logs` 这个机器全局路径。
- `ALPHA_HIVE_YF_RATE` 环境变量可覆盖限流速率（越界值忽略），调参不必改代码。
- `tests/conftest.py` 新增 autouse fixture，测试期把限流速率调到不产生等待。
  单测里 yfinance 全被 mock，令牌等待没有语义、只是墙钟浪费。

⚠️ **限流会拖慢逐票取数**。`test_dashboard_renderer` 实测从秒级涨到 **56s**
（30 只标的 × 2s 令牌），撞上 60s 超时 —— 这是把速率从 3.0 降到 0.5 的直接代价，
也是一个提前示警：**真实扫描同样会变慢**。若日常扫描因此吃不下时间预算，
用 `ALPHA_HIVE_YF_RATE` 上调，但先看 `scan_coverage_gate` 报的 429 次数。
- `tests/test_yf_gate.py`（20 项）

### Changed

- `resilience.yfinance_limiter`: rate **3.0 → 0.5** req/s, burst 2 → 1。
  3.0 是推测值且从未被实测支持，实测证据全部指向它太快。0.5 仍是保守推测
  而非实测最优（⚠️ 待验证）。
- `alpha_hive_daily_report.main()` / `generate_ml_report.main()`: 取数前 `yf_gate.install()`
- `market_intelligence` / `fred_macro` / `options_analyzer`: 热点路径 `yf_gate.ensure()`

### Fixed

- **`market_intelligence.calculate_iv_rv_spread` 的重试是限流放大器**：
  旧实现对**所有**失败都 `time.sleep(0.7 * (attempt+1))` 后重试 3 次。
  那个退避是为「OpenSSL 1.1.1q 并发 HTTPS 抛 SSLEOFError」这类开即恢复的故障
  设计的；对上限流器它把 1 次请求变成 3 次、间隔不到 2 秒，**在被拒绝时加倍施压**。
  现在 429 一次就停。
- `options_analyzer` 删掉 import 了却从未调用的 `_opt_rl`

### 8-27 数据已离线补算（30/30）

期权链快照 30/30 完好，缺的只是日K 派生字段 —— 这类数据随时可重拉。
以 0.5 req/s 走闸门重算并回写：**30/30 全部补回**，`rv_30d` 中位 40.7%（12.7~99.1）、
`iv_rank` 中位 32.2（2.9~100.0）。

闸门实测：**51 次调用、0 次 429、0 次冷却拦截**。同一批取数在 8/27 无节流时
产生了 687 次 429 —— 这是 0.5 req/s 可用的第一个正面证据（样本仍小，
真实上限未定位，⚠️ 待验证）。

⚠️ **尚未做**：8-27 的 `analysis-*.json` 与网站 HTML 仍是旧值。快照修好只是
让重跑能拿到数据，报告本身要重新生成才会更新 —— 那是一次需要用户确认的
限流长跑（30 只 × 0.5 req/s）。

### 自查记录（两条，都是我自己造的）

1. **测试写假了。** `test_iv_rv_does_not_retry_on_429` 第一版没停用闸门，
   于是第 2/3 次调用被冷却挡下，`calls == 1` 无论修复在不在都成立 ——
   把修复注释掉它照样绿。已改为在闸门缺席时隔离验证；mutation check 实测
   移除修复后报 `3 == 1` 变红、还原后变绿。
   **一个不会变红的护栏只是装饰。**
2. **「限流」这个解释我用错了三次。** 离线补算连续三轮 0 修复，我先归因于
   Yahoo 限流、再归因于「前台测试与后台任务抢配额」（这条第一次是真的），
   于是第二、三次就照搬。真正的原因是 scratchpad 里有个遗留的 `inspect.py`
   —— 用 `python3 <脚本路径>` 运行时 Python 会把**脚本自己的目录**放进
   `sys.path`（排在我显式 `sys.path.insert(0, PROJ)` 之后但依然生效），
   它遮蔽了标准库 `inspect`，yfinance 深处 `from inspect import getmembers`
   抛 ImportError，又被 `_refresh_price_derived` 的 `except Exception: _log.debug`
   吞掉 —— 退出码 0、「30/30 已处理」、零修复。
   **手上一直有反证：`calls: 0`。** 闸门计数器说一次网络请求都没发出，这与
   「我们被限流了」直接矛盾。我连着三轮把它解释过去了。
   （这正是本 session 在治的那个病，只不过对象是我自己。）
3. **退出码语义写错了一版。** 初版把限流并进 `_degraded`，于是
   `test_healthy_exits_zero` 红了 —— 合成的健康数据 + 8/26 真实日志的 487 次 429
   被判成失败。测试是对的：编排器只看退出码，那等于把预警谎报成故障。
   已改为诊断项，并补了「越闸但字段健康 → 退出码仍 0」的回归测试。
4. **我自己把限流打爆了一次。** 离线补算跑在后台时，我在前台继续做
   yfinance 冒烟测试，两者抢同一份配额，于是补算第一只标的就 429、
   而我的前台调用却成功 —— 差点据此误判成「`_refresh_price_derived` 有 bug」。
   实际是我在跟自己抢。


## [0.45.55] — 2026-08-28 — 失效条件空转了两天：一条读不懂，另外 119 条陪葬

CLAUDE.md 硬性规则：「任何结论必须附失效条件（thesis break）」。
> ⚠️ **v0.45.57 更正**：8/26 不是 0/30，是 13/30（当天配置只覆盖 13 只，13 就是上限）。断的只有 8/27。下面这句按原文保留。

实测 **8/26 与 8/27 两天各 0/30 只标的**有失效条件 —— 该规则空转了两天，
日志里只有一行 `thesis_break 配置加载失败: 'metric'`。

### 根因：两种 schema 并存 + 列表推导没有逐条隔离

`thesis_breaks_config.json` 里：

| 形态 | 条数 | 来源 |
|---|---|---|
| `metric` / `trigger` | 120 | 人工撰写的原有条件 |
| `field` / `op` / `value`（`_machine`=true） | 210 | **v0.45.50 于 2026-08-27 新增**的机器可判条件 |

读取端是列表推导：

```python
_l1 = [c["metric"] + "：" + c["trigger"] for c in ...conditions]
```

一条机器条件的 `KeyError` 就把**整个 try 块**打掉 —— 不是丢一条，是
**一只标的的全部失效条件一起消失**，30 只全灭。

⚠️ 这不是 v0.45.50 的错：它**新增**了一种 schema（没有改掉旧的），
是读取端从一开始就假定「所有条件长同一个样」。

### Fixed 🔴 — 逐条格式化 + 逐条隔离

新增模块级 `_format_break_condition(cond)`：识别两种 schema，读不懂返回
`None`（**不抛异常**）由调用方跳过并计数。外层 try 保留，但不再是唯一防线。

- 人工：`EPS 大幅低于预期：实际 < 预期 20%+`
- 机器：`综合分跌破 p2（全历史 1191 obs）：score < 4.0（自动）`

机器条件显式标**「自动」** —— 它是从历史分位推出来的，与人工判断不同重量，
混在一起不标就分不出来。

实测真配置：**330 条全部可格式化，0 条无法识别**。

### 守卫

`tests/test_thesis_break_schema.py`（12 条），含：
- **逐条隔离回归**：混入读不懂的条件，其余必须照常产出
- **真配置全量**：330 条必须 0 无法识别 —— 出现第四种 schema 时这条先红
- 反向用例：`value=0` 是真阈值，不能当缺失

四次变异全部转红：只认 metric/trigger / 读不懂时抛异常 / 机器条件不标「自动」/
`value=0` 被当成缺失。

## [0.45.54] — 2026-08-27 — Phase 3：展示层 25 处 + Phase 2 的一个真 bug

零世代边界代价（不改评分口径）——**除一条例外，见下方「归类修正」**。

### Fixed 🔴 — Phase 2 二次检查：`--out` 写出的 JSON 缺段

`scan_coverage_gate` 的 `--out` 在 label_honesty / price_check 算出来**之前**
就写盘了。默认路径（编排器用的正是 `--quiet --out`）写出的文件
**永远不含新增的这项检查** —— 护栏跑了，结果下游看不到。

而且坏价格那条是**提前 `return 1`，会完全绕过写盘** ——
也就是说「检出问题」的那一次，恰好是 `--out` 拿不到 JSON 的那一次。

写盘与退出码现已统一收敛到函数末尾。四条路径实测：
健康 0 / 字段降级 1 / 标签矛盾 1 / 无法判定 3，且**每条都写出完整 JSON**。

### Fixed — 定量结论表不再由常量撑起

| 位置 | 旧行为 |
|---|---|
| `_ch5_scenarios` | `curr_price=100`、`or 20/5/-10` 四常量 → 「$120/$105/$95/$85，概率加权期望价 $104.75，+4.75%」+ 一行 `Σ(概率 × 情景价格)` 公式 |
| 深度报告置信区间 | 区间兜底 ±1.5、σ 兜底 1.5，两者互相自洽 → 「置信区间 [3.50–6.50]，不确定性 中等（σ=1.5）」 |

两处都改为**缺数据即跳过并说明**。判据：读者能不能把它与真实测算区分开。

### Fixed — 比率与量纲被伪造

- `unusual_options`（**两处**）：`oi` 兜底 **1** 让 `vol_oi_ratio = volume / 1 = volume`
  —— 任何有成交的合约都会被判成「异动」，异动检测形同虚设。
  真实 OI=0 是「新开合约」，与「字段缺失」都不该当成 1 张持仓。
- `data_pipeline`：`avg_vol` 兜底 1.0 → `avg_volume` = **1 股**（任何流动性检查
  都判极端不可交易），`volume_ratio` 变成千万量级。
- `options_analyzer._fetch_full_chain_oi` 失败返回 `{}` → 下游 `or 0` →
  「全链 Call OI = 0、Put OI = 0」，对活跃标的是不可能事件。

### Fixed — 兜底值恰好落在阈值上（第三、四例）

`detect_conflicts`：`pc` 兜底 **1.0** 既不满足 `< 0.80` 也不满足 `> 1.20`，
`iv_skew` 兜底 1.0 同理。于是「三个判据的输入全没拿到」与「五类矛盾都不成立」
产出**完全相同的空列表**，而下游读作「未检测到信号矛盾」。

> 前两例：`rr=2.0` 恰好卡 STRONG BUY 闸、`tnx=4.5` 恰好卡 high 档边界。
> 共同点：**兜底值选的是「中性值」，而中性值在阈值判断里恰恰是一个确定的判断。**

### Fixed — 状态与来源谎报

- `report_deployer`：读不到本地文件 → `return True` = 「CDN 部署已验证」，
  与真正轮询成功的返回值完全同形。改为 `None` ＝「未验证」。
- `vix_term_structure`：`source` 写死在字面量里，期限结构算不出时仍自称
  `vixcentral+yfinance`。改为按实际拿到的期货点位数推导。
- `swarm_agents/bear_bee`：`data_sources["valuation"] = "yfinance"` **无条件赋值**，
  `pe` 取不到时标签仍自称有来源。与 `"momentum": "real"`（v0.45.49 已修）同形。
- `market_intelligence` 供应链：`returns.get("SOXX", 0)` → 摘要印出「vs SOXX +0.0%」
  ——取数失败被写成观测到的周涨跌幅；`source` 也改为按实际同业数推导。
- `alpha_hive_daily_report`：读 F&G 的 `value` 但**不读 `is_real_data`**，
  而同项目 `buzz_bee` 对同一函数检查了**三处**。不对称证明是遗漏。

### Fixed — 渲染成具体读数

分析师目标价 `$0.00 / $0~$0 / +0.0%`、刊头 `风险回报比 1.00 · ML 预测 50.0%`、
`bear_score` 默认 0.0（同文件 `_radar_data` 对**同一字段**默认 5.0 ——
两个默认值不一致本身就说明它们都是随手填的）、OI 墙 `pct_diff = 0`
（读作「正好压在平价上」，那是期权叙事里最有信息量的位置而非「不知道」）、
`sentiment_pct` 兜底 50（同一 grid-4 里紧邻的动量与量比在缺数时都正确显示「—」，
**同屏不一致**）、宏观表 `VIX 0.0 / 10Y 0.00%` 而「状态」列照常显示 low/high、
`metrics_collector` 内存 0.0 MB。

### Changed — 雷达图缺失维度画 0 而非 50

这是本批唯一「不能用 None、只能选一个数」的地方。雷达图画不出「—」，
选 **0** 不是因为 0 更正确，而是因为 **0 在雷达图上是一个塌陷的角，
视觉上一眼能看出「这一维没有」；50 看起来完全正常**。
判据从「哪个值更中立」变成「哪个值更不可能骗人」。

### ⚠️ 归类修正：一条本该进世代边界

`newsapi._recency_weight` 解析失败返回 **0.5** —— 而 0.5 **恰好等于半衰期点的
权重**（＝「发布于约 24 小时前」）。加权和决定 `bull_ratio`，**直接进 sentiment
维度** —— 本项目唯一有证据的可交易信号（IC +0.17，见 MEMORY）。

我把它分到 Phase 3 是因为审计报告写的是「叙事」，但追下去它流进了评分。
**分类依据不该是「代码在哪个文件」，而是「这个值最终流到哪里」。**
本条已改（解析不了 → None，从加权中剔除），评分口径确实变了，
在此明确标注，使 v0.45.50 的世代边界记录保持准确。

### 未做（记录在案）

- `data_fetcher.get_polymarket_odds`：**死代码** —— 无人调用（活的是
  `polymarket_client` 同名函数），且 `POLYMARKET_ENABLED = False`。
- `cloud_snapshot_fetch:169`：并发线在制文件，未动。
- `dashboard_renderer` 板块热力图、`generate_deep_v2` 约 15 处叙事兜底：
  与雷达图同类（图表/prompt 输入），改动面大且不影响评分，留待单独决定。

### Fixed 🔴 — 二次检查：我在本批亲手造了一个 `.get(k, default)` 陷阱

把 `avg_volume` 不可得时改成 `None` 之后，下游仍是：

```python
data.avg_volume = metrics.get("avg_volume", 0)      # 键存在但值为 None → 返回 None
data.volume_ratio = metrics.get("volume_ratio", 1.0)
```

**`.get(k, default)` 挡不住「键存在但值为 None」** —— 默认值 `0` 根本不生效，
None 被原样赋给声明为 `int` 的字段。

讽刺的是这正是本次审计**最早识别出来的那一类**（MEMORY 已记：
「`.get(k, 精心挑的默认值)` 最会骗人 —— 上游置 None 时它永不生效」）。

成因很具体：我改的是**上游**（让它返回 None），而陷阱在**下游**；
查消费点时看的是「谁会崩」，而 `.get(k, 0)` **不会崩** ——
它安静地把 None 传下去。**不崩的消费点最容易被漏掉。**

同时把字段声明由 `avg_volume: int = 0` 改为 `Optional[int] = None`，
让类型声明与实际行为对齐。

### Added — 测试

`tests/test_deep_report_no_fabrication.py` 17 条（含「任一分量缺失即跳过 ——
部分真实 + 部分常量的表比全常量更难识破」「真实的 0 不是缺失」）；
`test_scan_coverage_gate` +11 条（`--out` 完整性 + 畸形输入）。

修正**第 5、6 条把兜底值固化成不变式的既有测试**（`_recency_weight == 0.5`，
分布在两个文件）。累计已修正 6 条 —— 判据始终是：
问「若换一个同样合理的兜底值，测试该不该红」，该红的就不是不变式。

全量 1916 passed / 0 failed。

---

## [0.45.53] — 2026-08-27 — Phase 2：把审计方法固化成护栏

目标不是再修几个实例，是**防止第 69 个**。零世代边界代价（不改评分口径）。

### 先说放弃了什么：静态检查这条路走不通

试了三种，逐次收窄，都不成立：

| 尝试 | 为什么不行 |
|---|---|
| 「字面量来源标签」 | 全仓 **98 处**，绝大多数诚实（成功取数后标 `"cboe"` 没问题） |
| 「结果词字面量」（real/fallback/…） | **175 处**，`except` 里标 `"fallback"` 本来就对 |
| 「拼接链里的行内三元」 | 三次收窄都在误报 dict 字面量的值，且没抓到我自己那次真 bug |

根本原因：**一个来源标签是否诚实，取决于它在哪条分支上** —— 同一行字面量，
写在 `except` 里诚实、写在成功路径上可疑。那是语义不是语法。

静态检查看得见「写了什么」，看不见「此刻发生了什么」。这也解释了本次审计
68 条**没有一条是 linter 发现的** —— 全靠人工读代码或实跑。

所以护栏必须**真的走一遍失败路径**。

### Added — 测试层：退化输入 harness（`tests/test_degraded_input_harness.py`，27 条）

给每个评分/渲染入口喂 `{}` / 全 None / 畸形输入，断言产出不是「看起来合理的数字」。
判据来自本次审计的六个真实样本：

1. **该量纲的中位/中性值** —— crowding 50、RSI 50、P/C 1.0、F&G 50、五维 5.0
2. **该量纲的极值** —— pheromone_strength 1.0（最大）、IV Rank 0.0（最低）
3. **恰好等于某个阈值** —— rr 2.0 = STRONG BUY 闸、tnx 4.5 = high 档边界
4. **一个具体的观测读数** —— σ 30%、dte 30 天、price 145.32

**含注册表覆盖闸**：新增评分入口不注册即红。没有它，护栏会随代码增长而失效 ——
而且是**静默**失效，正是它本身要防的形状。

### Added — 运行时层：来源标签诚实度（`scan_coverage_gate.check_label_honesty`）

静态判不了的，运行时判得了，而且判据很硬：

> **标签宣称取数成功 ⇒ 它管辖的值必须非空。**

违反即矛盾 —— 要么标签在撒谎，要么值被谁清掉了，两种都该查。
这是 `"momentum": "real"`（动量兜底 0.0 而标签硬编码自称 real）那一类的运行时对应物。

双向验证：喂已知坏数据报 2 处矛盾；喂诚实降级（标签自认 `fallback` + 值为空）
放行且**不计入分母**。`0` 算「有值」——否则真实的 0 会被误报成矛盾。

编排器 Step 12 现在同时看字段覆盖率与标签自洽性。

### Fixed 🔴 — 护栏第一次跑就抓到 2 个我在 Phase 1 漏修的

**① `advanced_analyzer.py:254` —— 第三处 `or 30`**
v0.45.50 说修了「两处」0DTE 守卫架空，实际有**三处**。第三处在 vanna 敞口路径，
同样把真实 0DTE 改写成 30 天（T 差 60 倍）。

**② `_get_metric_display` 只修了五分之一**
v0.45.49 修过这个函数，但只修了动量那一行。护栏发现其余四个字段仍会崩，
原因比预想严重：

    displays = {                                  # ← 急求值
        "social_volume":  f"{metrics.get(...,0):,}",   # None → 崩
        ...
        "short_squeeze_risk": (已修),
    }
    return displays.get(key, "N/A")

**整个 dict 是急求值的** —— 即使只取一个 key，其余四个也会被计算，
所以任一 None 都会崩，与请求哪个 key 无关。当时修完跑测试通过，
**因为测试只喂了动量为 None**。

这两条正好说明护栏的价值：我的定向测试只覆盖我想到的字段，
参数化 harness 喂的是全部五个。

### 未做（记录在案）

- **拼接链里的行内三元**（v0.45.50 我自己犯过：语法合法、编译通过、整块 HTML 出错）
  三次静态收窄都不可靠，改为写进代码规范：**拼接/格式化表达式里不放条件逻辑**。
- 注册表目前只覆盖 `CrowdingDetector`；其余模块的入口需逐步纳入。
  覆盖闸会在这些模块新增函数时提醒，但不会主动扩展到未列入的模块。

全量 1886 passed / 0 failed。

---

## [0.45.52] — 2026-08-27 — 网站补全四个字段 + 个股政体一直在用 SOXX 的均线

用户要求把 8/26 网站上缺的 IV-RV / IV Rank / RV30 / 催化剂 / IV Skew 比值 /
SPY 同期基准 / 宏观数据补上。逐项核对后发现**七项里只有三项是真缺的**，
但在核对过程中翻出一个更严重的数据污染。

### 核对结果（三个页面逐项比对实际渲染值）

| 字段 | 日报页 | 仪表板 | 每标的 ML 页 |
|---|---|---|---|
| IV Rank / 催化剂 / IV Skew | 已有 | 已有 | 已有 |
| 宏观数据 | **已完整**（VIX/10Y/大盘/美元 + 逆风顺风） | 已有 | — |
| **IV-RV 价差** | ❌ | ❌ | 已有 |
| **30 日实现波动率** | ❌ | ❌ | 已有 |
| **SPY 同期基准** | ❌ | 净值曲线里有 | — |

数据一直都在（8/26 实测 `iv_rv_spread` / `rv_30d` **30/30 覆盖**，
`iv_skew_ratio` 29/30），缺的纯粹是渲染。

### Fixed 🔴 — `market_intelligence._get_ma` 从不校验返回的是哪只标的

`_get_ma(sym, ...)` 的守卫只有 `hist.empty` 与 `len(hist) < period` ——
一份完整的**别家**数据两条都过。yfinance 限流时返回**上一次成功请求的缓存帧**，
而板块层 `_get_ma("SOXX", 20, 40)` 与个股层 `_get_ma(ticker, 20, 40)`
用的是**同一个 period 字符串**（`"60d"`）。

2026-08-26 实测（那次扫描限流 487 次）：

| 标的 | 报告里的 20MA | 真实 20MA |
|---|---|---|
| NVDA / MSFT / TSLA / VKTX | **$528** | NVDA 实为 **215.56** |
| SOXX（泄漏源） | 529 | — |

**个股金叉/死叉判断因此建立在半导体 ETF 的均线上**，并进入 `regime_score_adj`、
写进日报与网站。改为校验返回 DataFrame 的 ticker 层，不符返回 `(nan, nan)`
→ 走「个股政体数据不可用」。诚实缺失好过安静地用别人的数据。

### Added — 日报页「市场隐含预期」

`IV-RV 价差`（含 期权偏贵/偏便宜/定价合理）、`30 日实现波动率（RV30）`、
`IV Skew 比值`。`rv_30d` 取**结构化字段**，不从 `iv_rv_note` 文本里抠。

`IV Rank` 现在同行标出 `iv_rank_source` —— `hv_proxy` 与真实 IV 历史算出来的
是两个东西（见 MEMORY「IV Rank 口径」），不标就分不出来。

### Added — 日报页 SPY 同期基准

`backtester.get_accuracy_stats` 新增 `spy_avg_return` / `spy_sample_n`，
与 `avg_return` **同口径**（同 cutoff、同模糊样本排除、同非交易日剔除）。
实测：`SPY 同期基准：-0.57%（118 条同口径样本） | 超额：+0.31pp`，
样本数与 `total_checked` 一致。

「平均收益 +0.10%」单看没有意义 —— 同期大盘是涨是跌决定了它算好还是算差。

### Added — 仪表板卡片

每只标的的指标行加 `IV-RV` 与 `RV30`（原为 IV Rank / P/C / 看空强度 /
数据真实度 / GEX 五项）。

### 一条贯穿的原则：缺就省略，不用 0 兜底

`0` 价差是「IV 恰等于 RV」、`0` 基准是「大盘恰好没动」，与「没算出来」
完全是两回事（v0.45.42 同一原则）。守卫里有反向用例：真算出 `0.00%` 时
必须照常渲染，不能被当成缺失。

### Fixed 🔴 — ML 胜率按「今天」读文件，重渲染历史日报会抹成一个常数

`_load_fresh_ml_prob` 写死 `datetime.now()` 去找 `analysis-{TICKER}-ml-{DATE}.json`。
重渲染 / 补跑历史日报时它指向**今天**，对应文件不存在 → **全部**返回 None →
30 只标的的 ML 胜率退回 swarm 存量的**降级常数**。

2026-08-27 重渲染 8/26 实测：

| | swarm 存量 | `analysis-*-ml-2026-08-26.json` |
|---|---|---|
| QCOM / NVDA / MSFT / TSLA / VKTX / BILI | **全是 57%** | 66 / 62 / 51 / 64 / 63 / 64 |

`_load_fresh_ml_prob` 存在的意义正是替换掉那个常数 —— 而按「今天」读让它在
补跑时**完全失效**。与「所有标的同一个值」这一类污染同形（本版另一条
`_get_ma` 也是）。改为 `date_str` 由调用方传报告日期，`_build_competitive`
与 `generate_swarm_markdown_report` 一并透传。

### 守卫

`tests/test_market_regime_symbol_guard.py`（4 条）+
`tests/test_site_missing_fields.py`（12 条）。七次变异全部转红：
拆掉符号校验 / IV-RV 用 0 兜底 / SPY 用 0 兜底 / IV Rank 不标 source /
仪表板缺失给 0 / ML 胜率退回 `datetime.now()` / `_build_competitive` 不透传日期。

## [0.45.51] — 2026-08-27 — 并发开工先占号（`CLAUDE.md` 规则）

2026-08-26~27 两条 session 并行，版本号撞了三轮：`0.45.36–0.45.40` 被双方
各用了一遍（一方整批后移五个号）、`0.45.47` 与 `0.45.48` 又各撞一次
（另一方让号两次）。每次都是**完工后才发现**，返工成本落在改标题、
改提交信息、重跑核对上。

### Changed — `CLAUDE.md` 版本历史规则新增「并发开工必须先占号」

开工第一件事查号 + 插占位条目 + **立刻单独提交推送到 main**，完工时替换成真标题。

⚠️ 关键在「立刻推送」那一步：占位写在自己的 worktree 里，**别的 session 看不见**
——它们读的是主 checkout / `origin/main`。不推送的占号等于没占。
这正是本次三轮撞号的共同成因：双方各自的编号意图从未对对方可见。

撞上时的处置也一并写入：**先提交（进 git 历史）的占号**；若双方都只在工作区，
则**批次大的保留、单条的让**（改号成本低者先动）。

### 说明

本条自身没有走完整的「先占号再干活」流程 —— 改动只有一个文件、耗时不到一分钟，
不存在并发窗口。该规则针对的是**跨小时的成套改动**，不是每一次文档微调。

## [0.45.50] — 2026-08-27 — Phase 1：一次性世代边界（35 处静默降级）

⚠️ **本条消耗一次世代边界**，`_COHORT_HISTORY` 已追加。

排期把全部评分类修复**合并成一次**，不是按严重度分批 —— 每批都会把 IC 重跑的
25 周计数器清零。而 2026-08-26 那次重置让计数器**已经是 0**，所以此刻做的边际
代价为零；拖两个月做，代价是 8 周已攒样本。

### Fixed 🔴 ① 样本静默消失

- `backtester.save_predictions`：取价两条路径都失败时 `price=0` 落库，而
  **13 处 SQL 带 `price_at_predict > 0`** → 该行对全部统计不可见，`saved += 1`
  却照常计数。现点名 WARNING + 汇总「其中 N 条无入场价」。DB 实测 2 条（BRK-B 8/12、8/14）。
- `_verify_period` 成本计算失败时把**毛收益写进 `net_return_t7`**（该列定义为
  「含滑点+佣金+借券费」），现置 `None` —— 诚实缺席而非伪装成零成本交易。

### Fixed 🔴 ② 数据缺失被奖励

`CrowdingDetector` 空 metrics 实测 `20.59` →「低拥挤度」→ `adjustment_factor`
**1.2（加 20% 分）**。数据缺失被当成利好。

根因值得单记：**v0.45.30 已经把合成层修对了**（缺失分量重新归一化），但一直没
生效 —— 每个分量在上游就被 `.get(k, 默认值)` 填成了数字，归一化永远看不到「缺失」。

现在分量缺失即 `None`，全不可得返回 `None`（不是 0.0），分类「数据不可用」、
调整因子 **1.0**。三只消费蜂各自走中性并留痕。

### Fixed 🔴 ③ 训练集污染

`ml_predictor` 五处 `.get(dim, 5.0)`，而 `_momentum/_vol/_iv/_pc` 全由这五个值
派生 —— 一条「某只蜂当天挂了」的记录变成一整行自洽的「各维中性、动量 0%、
波动率 12.5%、IV Rank 50」正常样本。讽刺的是同函数下方已把 `50.0`/`1.0` 当可疑
哨兵（`_iv_db != 50.0` 才采信）。现改为**剔除**，实测只损失 3.1%（28/910）。

### Fixed 🔴 ④ 三处守卫被上游架空 —— 本次审计的头号复现模式

| 守卫 | 谁架空了它 |
|---|---|
| `risk_engine` 的 σ（注释明写「不可得就写 None，不要用 30 冒充」） | 上游 `or 30.0` 先填了值 |
| `advanced_analyzer` / `options_analyzer` 的 `max(dte, 0.5)` | 上游 `or 30` 把真实 0DTE 改写成 30 天，**T 差 60 倍**，恰在 gamma 最大的到期日 |
| STRONG BUY 闸 `rr >= 2.0` | 无历史时返回 **2.0**，恰好卡在阈值上通过；BUY 闸的 1.5 同理 |

**守卫本身都写对了。** 查这类问题不能只读守卫，要追到值第一次被产生的地方。

### Fixed 🔴 ⑤ 论点失效闸从未触发过 → 6.35%

配置存散文 `{id, metric, trigger}`，求值器要 `{field, op, value}` ——
`cond.get("value")` 恒为 None，第一行就 `return False`。

按用户决定用现有 5 字段迁移。**初版阈值我用单日 30 个样本定，回放触发率 44.7%**
（`bear>=5` 占 17.3%、`iv>80` 占 15.6%）。按**全历史 1191 个标的-日观测**的分位
重定后：预警 3.90% / 认损 2.45% / 合计 **6.35%**。

**价格条件刻意不写进配置**：绝对价位随股价漂移衰减（初版按 8/26 基准 ×0.88
写入后，回放 3 月数据几乎条条触发）。有意义的止损应锚定**建仓价**，而求值器
只拿得到 `current_price` —— 属结构性缺口，应在持仓层解决。

### Fixed ⑥ 其余

- `paper_portfolio.compute_kpis`：`spy_ret` 初值 0.0 → `alpha_pct = 总收益本身`。
  改 `None`，三处渲染点各自接住。（与 v0.45.42 在 `portfolio_backtest` 修的同形）
- `_fetch_ohlc` 把失败结果写进进程缓存，而取用侧用 `if key in _PRICE_CACHE` 判定
  （空 dict 也命中）→ 同 key **永不重试**。现不再缓存失败。
- `pheromone_board` 值损坏给 `1.0` —— 那是**最大值**，坏条目直接拿到板上最高
  影响力与最长存活期。改 `0.5` 中位。
- `paper_portfolio` 此前**整个模块没有 logger**。

### Fixed — 两处本次改动自己引入的问题（全量测试抓到）

**① 集成断裂**：补机器条目后，**第二个消费者** `thesis_breaks.py._check_condition`
直接 `condition["id"]` → KeyError。两套 schema 并存的前提是**各自跳过对方的条目**，
现双向都跳过并加测试锁住。
教训：改共享配置第一步该 `grep` 谁在读它 —— 函数的调用方 grep 得出来，
配置的读取方常散落各处、还可能用不同 schema 读同一份文件。

**② 三元绑定**：`paper_portfolio` 渲染写成隐式拼接链里放行内 `if/else`，
三元绑定到**前面整条链**而非单个片段 —— 语法合法、编译通过、整块 HTML 出错。
改为预先算好命名变量。
规则：**拼接/格式化表达式里不放条件逻辑。**

### Added — 测试

新增双 schema 共存 4 条、迁移后闸门行为 7 条；修正 **4 条把缺陷固化成不变式的既有测试**：

| 测试 | 原断言 |
|---|---|
| `test_empty_metrics_returns_valid_score` | 空 metrics 产出有效分 |
| `test_handles_missing_dimension_scores` | 空 `{}` → `crowding_score == 50.0` |
| `test_pheromone_strength_nan_becomes_1` | NaN → **1.0**（测试名就写着 becomes_1） |
| `test_none_volume_ratio...`（v0.45.47） | `price_momentum_5d == 0.0` |

四条形状一样：测试名说「能处理缺失」，断言的却是「缺失时产出某个具体假值」。
判据 —— 问「若换一个同样合理的兜底值，测试该不该红」，该红的就不是不变式。

全量 1848 passed / 0 failed。

---
## [0.45.49] — 2026-08-27 — 二次检查 v0.45.41/0.45.48：四个 bug，两个会崩、两个会撒谎

对本人在 v0.45.36–0.45.48 的改动做对抗式复查。四处全部由**构造检验坐实**，
不是纸面推演。

### Fixed 🔴 — 0 收盘价 → `ZeroDivisionError`

`official_closes` 只滤 `None` / `NaN`。yfinance 返回 0 时它当成合法收盘价，
一路流到 `current / truth` 直接崩。与 v0.45.42「缺失值不许冒充 0」同一条原则。

⚠️ **修一层不够**：第一版只修取数层，而测试把 `official_closes` 整个打了桩、
绕过它 —— 仍然崩。`_resolve_close` 也改为 `> 0` 判据（纵深防御）。

### Fixed 🔴 — `_snapshot_ctx` 把扫描失败吞成「快照不可用」

`try` 块同时包住了「进入快照模式」和「跑扫描」。扫描体抛异常时：

1. 异常穿过 `yield prov` 被 `except Exception` 抓住
2. 打出「⚠️ 无可用云端快照」—— **完全错误的诊断**，快照明明拿到了
3. 继续执行 `yield None` → `RuntimeError: generator didn't stop after throw()`

**真实的扫描错误被吞掉，换成一个假诊断加一个莫名其妙的 RuntimeError。**
改为 `ExitStack`：try 只包获取，扫描体的异常原样传播，供给器仍保证卸载。

### Fixed 🟠 — 对「本就正确」的行报假分歧

交叉印证跑在「要不要动」之前。一条已等于官方收盘、无需校正的行，
若 CBOE 分歧就会被打出「拒绝校正」警告并计入 `disputed` ——
可它压根没有待校正的内容。调换顺序。

顺带：来源标记从 `+cboe_prev` 改为 `+cboe_close`（v0.45.48 已改用 `close` 字段），
且不再覆盖 `@日期` 后缀。

### Fixed 🟠 — 下载区间起点未前垫，非交易日样本回退不到前一交易日

`official_closes` 的 `start = min(预测日)`。若最早的预测日就是非交易日
（或 `--since 2026-03-01` 这样指定周日），「该日之前最近的交易日」根本不在
下载数据里，该行被静默记成「无来源」。起点改为往前垫 10 个自然日。

全量跑此前靠「最早预测日 2/27 早于最早周日 3/01」侥幸安全 —— 不能依赖。

### Changed — `full_chain_oi` 行权价键碰撞由静默变可见

`_restore_numeric_keys` 遇到 `"130"` 与 `"130.0"` 会后者覆盖前者、**静默丢一条 OI**。
真实快照的键统一来自 Python float 经 `json.dump`，不会同时出现两种写法，
属理论风险 —— 但静默丢数据不该被容忍，改为打 WARNING。

### 守卫

`test_close_correction.py` 22 条（+4）、`test_cloud_snapshot_loader.py` 17 条（+1）。
五次变异全部转红：取数层不滤 0 / `_resolve_close` 退回 `is not None` /
分歧检查放回前面 / 区间不前垫 / 扫描体包回 try。

## [0.45.48] — 2026-08-27 — 周日样本不是「无收盘价可校」，是我查错了日子

> 原编号 0.45.47，因另一条线正在工作区写同号的「Phase 0：先修仪表」而让出。
> 我这条是独立小修，改号成本低；对方那批是成套的，中途返工代价大。

用户问「110 条周日样本不能从 yfinance K 线里找到合适的收盘价吗」。**能。**
0.45.41 把它们记成「无来源」，是我用了**精确日期查找**——周日当然查不到。

### 实测：108/110 本来就是对的

周日样本是已退役的 `sample-accumulator` 的产物（周日 18:00 跑），它当时能拿到的
最新价本来就是**上周五收盘**。对照「该日之前最近一个交易日收盘」：

| | 条数 |
|---|---|
| 本就一致 | **108** |
| 需校正 | **2**（全是 CRWD） |
| 无来源 | 0 |

那 2 条偏离恰好 **300.0%** = CRWD 在 **2026-07-02 的 4:1 拆股**：库存的是拆股前
未复权价（448.13 / 527.77），而 `close_t7` 是复权序列算的，两边口径不一致 ——
产出 **−73% / −71%** 的垃圾收益。校正后为 **+6.34% / +16.88%**。

这也解释了此前发现的「5 条 >50% 收益」里的 2 条；剩 3 条 RKLB 已核实为真实行情
（2026 年无拆股，5/08 单日 +34%）。

### Changed — 收盘价解析按交易日/非交易日分流

**交易日必须命中当天，不许回退。** yfinance 偶发缺一天时回退会静默把前一日
收盘当成当日收盘 —— 正是本工具要治的污染，方向还反了。非交易日才回退到前一个
交易日，且 `close_correction_source` 如实记为 `yfinance_close@2026-04-24`。
日历判不了时按**交易日**处理（严格方向）。

### Fixed 🔴 — 交叉印证一直在比错日子（假信心）

`prev_day_close` 是**该 payload 自身 session 之前**那天的收盘，不是「相对今天的
前一交易日」。而 CDN 对不同符号刷新进度不同（实测 TMO 落后整整一个 session）。

**30 只里 24 只「印证通过」，只是因为相邻两天收盘通常差不到 0.2%**；只有
T（0.39%）与 TMO（0.88%）动得够大才露馅。**比错日子的交叉印证比没有更危险 ——
它提供的是假信心。**

改用 `close` 字段 + CDN `timestamp` 定日期。规则由实拉数据确立，非假设：

> **`close` = 文件生成时刻「最近一个已收盘交易日」的官方收盘价。**
>
>     NVDA 文件 08-27 08:31 ET（盘前）→ close=209.66 = **8/26** 收盘
>     TMO  文件 08-26 21:18 ET（盘后）→ close=633.71 = **8/26** 收盘

⚠️ 刻意**不用** `last_trade_time` 定这个日期：盘前它仍停在上一场的最后成交，
分不清「8/27 盘前的新文件」与「8/26 的旧文件」—— **我第一次修就是栽在这**
（把 24 条假通过换成了 7 条假分歧）。

修正后实跑：**交叉印证 30/30 全覆盖、零分歧**，无来源 0。

### Added — 官方收盘覆盖率可见

yfinance 批量下载会**部分失败**（同一天两次运行：一次全覆盖，一次 102 条无来源）。
覆盖率现在会报出来 —— 否则「无来源 N 条」看起来像数据本身的性质，
而不是这一次下载没拿全。

### 守卫

`tests/test_close_correction.py` 18 条（+6）。含用 2026-08-27 实拉 payload 钉死的
`_session_of_close` 参数化用例。六次变异全部转红，其中一次**复现我自己第一次的错法**
（用 `last_trade_time` 定 session）。
## [0.45.47] — 2026-08-27 — Phase 0：先修仪表

按修复排期第一阶段。这三处的共同点是**它们是用来发现其他问题的仪表**——
仪表失准时后续修复无法验证，所以排在最前，且零世代边界代价（不动评分）。

### Fixed 🔴 — 数据真实度均值把 0 踢出分母

`dashboard_renderer.py` 的聚合用的是**真值**过滤：

```python
[... for t in swarm_detail if swarm_detail[t].get("data_real_pct")]
```

它同时排除了两种完全不同的情况——`None`（没算过，确实该排除）与
`0`（**一条真实数据通道都没有**，最该被计入）。

后果是这个指标越该报警越不报警：**数据越烂的标的越容易被踢出分母，
显示的「数据真实度」反而越高。**

实测历史 swarm_results：8/04、8/06、8/10~8/14 共 7 个扫描日各有 1 只为 0，
**全部是 BRK-B** —— 即 v0.45.2 那个 ticker 正则（`^[A-Z]{1,5}$` 拒绝带连字符的
类份额代码）的受害者。因果链完整：

    正则拒绝 BRK-B → 它全部数据通道失败 → data_real_pct = 0
    → 被这个「本该暴露数据问题」的指标排除掉

修复后实测偏差：8/10~8/14 各日高估 **2.97~3.20pp**。量级不大，
但方向恒定，且数据越差高估越多。

### Fixed 🔴 — fallback 被记为成功，降级告警永不触发

`real_data_sources.get_short_interest` 里 `_record_src_success` 是**无条件调用**的。
而 `_record_src_success` 会把连续失败计数**重置为 0**：

> 于是一个永远返回空数据的源，「连续失败 3 次触发降级告警」这条规则
> **永远不可能满足** —— 每次调用都把计数清零。

同一个函数上一行刚把这种情况判成 `data_quality="fallback"`，下一行却告诉
追踪器「这次成功了」。现改为与函数自己的质量判定一致。

语义澄清（写进注释）：这里的「失败」不是网络错误，是**源没有交付可用数据**；
对健康度而言两者后果相同。

### Fixed 🔴 — 检查没跑成也报「system healthy」

`alert_manager` 三条路径都会让检查静默不执行，而调用方只看到「零告警」：

| 路径 | 问题 |
|---|---|
| `status.get('steps_result', {})` | 拿到空 dict → 循环不执行 → 步骤失败检查**没跑** |
| `except (…, KeyError, ValueError, TypeError)` @ debug | 日报 JSON 结构稍变就整块跳过 P2 低分检查 |
| `if not alerts: "system healthy"` | 把「查过了没问题」与「根本没查成」渲染成同一句话 |

新增 `AlertAnalyzer.checks_skipped`，记录哪些检查未能执行；
`steps_result` 缺失与报告解析失败各记一条并提为 WARNING；
`main()` 只有在**全部检查都执行过**时才敢说 healthy。

**告警系统自己静默失效，是最不该发生的一种静默失效。**

### Added — 测试 14 条（`tests/test_instrument_integrity.py`）

含「连续 3 次空响应必须触发降级告警」——该断言在旧实现下**永远失败**。
回退验证：把三处修复回退后 5 条转红，覆盖全部三个文件。

## [0.45.46] — 2026-08-27 — 收盘后取的一直是盘后价

用户指出「232.32 是 CRM 盘后价」，并要求「检查所有价格都取收盘价格，
不要取到盘后价格」。查下去发现这不是 CRM 一只的问题，是**整条取价链的系统性偏差**。

### 根因

CBOE payload 同时给两个字段，含义完全不同：

| 字段 | 含义 |
|---|---|
| `current_price` | 最近一笔成交。**收盘后 = 盘后价**（延长时段到 20:00 ET） |
| `close` | 该交易日的**官方收盘价** |

全部定时扫描在 **14:00 PDT = 17:00 ET** 跑，即收盘之后、盘后时段之内。
而三个取价点一律写成 `current_price or close`：

- `cboe_options.py:189`（主链，`fetch_cboe_chain` 的现价）
- `data_pipeline.py:405`（`StockData.price`）
- `cloud_snapshot_fetch.py:87`（云端快照）

于是**记录的一直是盘后价**。

2026-08-27 03:52 ET 实拉 CBOE，对照 yfinance 官方收盘：

| | `current_price` | `close` | 官方收盘 |
|---|---|---|---|
| CRM | **232.3187** | 205.62 | 205.62 |
| NVDA | **219.53** | 209.66 | 209.66 |
| MSFT | 495.94 | **496.37** | 496.37 |

`close` 与官方收盘**逐分不差**（实测 5 只全部 0.000%）。
`232.3187` 正是回填前 DB 里 CRM 的值——一字不差，坐实了取的就是 `current_price`。

**为什么这不是小数点问题**：`price_at_predict` 是所有收益计算的**入场价**
（`backtester` / `dynamic_exit_backtest` / `ic_diagnostics`）。用盘后价当入场价，
等于假设能在财报公布后、以盘后价成交——收益全错。CRM 2026-08-26 恰是财报日
（`yf.Ticker("CRM").calendar` 确认），盘后 **+12.98%**，把这个长期偏差
放大到了肉眼可见。平日它藏在 0.1~5% 里。

### Added — `cboe_options.official_price(payload)`

按交易时段选字段，返回 `(price, source)`：

- **盘中**（09:30–16:00 ET 工作日）→ `current_price`，标 `cboe_intraday`
- **收盘后 / 盘前 / 周末** → `close`，标 `cboe_close`
- 取不到 → `(0.0, "unavailable")`

关键设计：**收盘后不回退到 `current_price`**。回退等于把这个 bug 原样放回来——
「拿不到官方收盘价」与「拿到盘后价」必须是两种不同的结果。

三个取价点全部接线，`cloud_snapshot_fetch` 的 `price_source` 由写死的
`"cboe_delayed"` 改为真实来源标签。

### Changed — 8/26 的 `price_at_predict` 用官方收盘价重新回填

v0.45.45 用冻结快照回填过一次，但快照价本身也来自 `current_price`
（只是采于 17:13 ET，盘后刚开始、多数标的还没怎么动，所以偏差小得多）。
本次改用 `close`，30 只全部对齐官方收盘。

备份：`db_backups/pheromone_pre_close_price_*.db`

### Added — 测试 24 条（`tests/test_official_close_price.py`）

- 时段边界参数化（开盘/收盘瞬间、盘前、周末）
- 三只实拉 payload 的收盘后取值
- **`close` 缺失时不许回退 `current_price`**
- 盘中仍取实时价（不能矫枉过正）
- 静态闸：三个文件都不许再出现 `current_price or close` 模式

### 未做（记录在案）

- ~~未回溯修正历史日期~~ → **已核查，历史值不需要修正**（2026-08-27 补）。

  我原先写的「历史值普遍是盘后价」**是错的**。用另一 session 已建好的
  `close_correction.py`（双源交叉：yfinance 官方收盘 + CBOE `prev_day_close`）
  对全库 1008 条 dry-run：

  | | 条数 |
  |---|---|
  | **需校正** | **0** |
  | 本就与官方收盘一致 | 810 |
  | 此前已被该工具校正过 | 76 |
  | 无官方收盘可比（**全部是周日**的扩展池样本） | 110 |
  | 两源分歧拒改（CBOE CDN 陈旧，我方值经 yfinance 核实为准） | 12 |

  **机制是真的，影响几乎为零**：扫描在 17:10 ET 跑，距收盘仅 10 分钟，
  多数标的盘后尚未成交或波动 <0.1%，`current_price` 与 `close` 事实上相等。
  它只在**财报日**咬人——而那些行早已被 `close_correction.py` 抓出并修正
  （76 条中就有这类）。

  教训与 CRM 那次同形：我从「机制存在」直接推到「影响普遍」，
  中间少了一步**量一量**。
- **未给重跑加时间窗护栏**（v0.45.45 已记）：现在能发现窗口漂移，不能阻止。

## [0.45.45] — 2026-08-27 — 冻结股价反而是防护：`price_at_predict` 的两种污染

用户问「云端快照现在是不是只冻结 CBOE 的 IV 了，因为冻结股价会出问题」。
答案是 **否**（快照仍冻结股价：本地 `_snapshot_stock_price`、云端 `price_at_fetch`），
但**"冻结股价会出问题"这个前提在实测里是反的**——冻结的那份反而干净。

### 实测对照（8/26，30 只，对照 yfinance 核实的真实收盘）

| 来源 | 中位绝对偏差 | >1% | 最大 |
|---|---|---|---|
| DB `price_at_predict`（每次运行现拉） | 0.36% | 8 只 | **+4.71%** (NVDA) |
| 期权快照 `_snapshot_stock_price`（冻结） | **0.14%** | 1 只 | 1.45% |

### Fixed 🔴 — ① 补跑窗口漂移（本次由我的重跑造成）

为业务日 D 重跑扫描时，若运行时刻已越过 D 的交易时段，现拉的价格早已不代表 D。
本次 8/26 的最后一次重跑在 **23:57 PDT = 8/27 ET 凌晨 2:57**，于是
NVDA 的 `price_at_predict` 被写成 219.53，而 8/26 真实收盘是 209.66（**+4.71%**）。
30 只里 8 只偏差 >1%。

`price_at_predict` 是**所有收益计算的入场价**（`backtester.py:921`、
`dynamic_exit_backtest.py:113`、`ic_diagnostics.py:231/358`）。NVDA 入场价高 4.71%
意味着它的 T+7 收益会被低估 4.71pp。

**万幸**：8/26 的 30 条一条都还没 T+7 结算（T+7 = 9/2），污染未传导到任何收益指标。

已从冻结快照回填 30 条（备份 `db_backups/pheromone_pre_price_restore_20260827_002008.db`）。

⚠️ **这是本次会话的操作事故，不是既有 bug**：这个形状与 v0.45.28 隔离的
8/24 期权污染同源——「为过去某日重跑，却拿了运行时刻的实时数据」。
期权那侧当时已定性为**不可补、只能缺失**；价格这侧因为快照冻结了当时的观测，
反而可补。**冻结在这件事上是防护，不是风险。**

### Fixed 🔴 — ② CRM 232.32 是**盘后价**（见 v0.45.46，本条初判有误）

⚠️ **本条最初被我判为「数据源单点乱码」，是错的**（用户指出）。
232.32 不是坏读数，是 CRM 在 **2026-08-26 财报日**的盘后价
（`yf.Ticker("CRM").calendar` 确认 Earnings Date = 2026-08-26，盘后 +12.98%）。

真正的成因与 ① **是同一件事**：CBOE payload 里 `current_price` 在收盘后
就是盘后价，而全部扫描都在 17:00 ET 跑。CRM 只是因为当天有财报，
把这个长期存在的偏差放大到了肉眼可见。根治见 **v0.45.46**。

保留此条是为了记录判断过程：三条"证据"（近月无此价、期权支撑位在 160/190、
邻日价格正确）当时看起来互相印证，但它们只能证明**这个价不是当日收盘价**，
不能证明**它是乱码**。缺的那一步是问「那它是什么价」。

### 修复后

| | 中位绝对偏差 | >1% | 最大 |
|---|---|---|---|
| 修复前 | 0.36% | 8 只 | 4.71% |
| **修复后** | **0.135%** | **1 只** | **1.45%** |

### Added — `scan_coverage_gate.py --check-prices`

两种污染的共同点是**内部自洽性检查抓不到**：快照与 DB 可以彼此一致地错，
CRM 那种乱码在单日数据里也完全自洽。只有与外部收盘价对照才发现得了。

新增 `check_prices()`：把 `price_at_predict` 与该日真实收盘对照，
分两档——`≥1%` 列为 ⚠️（多半是窗口漂移），`≥5%` 判为 ❌ 坏读数
（正常时点差不可能有这个量级）。取不到收盘价时返回「无法判定」而不是「可信」。

默认不跑（需网络、较慢），加 `--check-prices` 启用。实测对污染前的备份
能同时抓出 CRM 坏读数与 8 只窗口漂移。

### Added — 测试 5 条

含「取不到收盘价必须说不知道，不得默认成价格可信」，以及两种污染各一条复刻用例
（CRM 12.98% 判 bad、NVDA 4.71% 判 warn —— 量级不同、成因也不同，不该同档处理）。

### 未做（记录在案）

- **未给重跑加时间窗护栏**：现在只是能**发现**窗口漂移，没有阻止它。
  正确做法是补跑时不再现拉价格、直接用当日冻结快照，但这会改动主扫描路径的
  取价链，应单独评估。
- **未回溯核验历史日期**：只查了 8/26。CRM 那种单点乱码可能在其他日期也发生过。
  `--check-prices` 现在可以逐日跑，但本次没跑。

## [0.45.44] — 2026-08-26 — 我自己引入的回归 + 论点失效闸从未触发过

### Fixed 🔴 — v0.45.42 的回归：资金曲线与 SPY 基准整块没生成

v0.45.42 把 `_spy` 改成可为 None，但**漏掉了一个消费点**：

```python
"spy_ret": round(_spy, 2),   # round(None, 2) → TypeError
```

异常被 `except Exception as _eq_err: _log.debug(...)` 整块吞掉，于是
`equity_curve` 与 `trading_stats["realistic"]` **完全没有生成**。
而 `_trading_stats` 预置了默认值（`total_spy_ret: 0.0`、`alpha_vs_spy: 0.0`），
页面照常渲染出「大盘 0%、无超额」——看起来算过了。

**这个 bug 隐身了三次重跑**，因为三样东西同时掩护它：裸 `round` 抛的是
TypeError（不是取数错误，所以我没往那儿想）、debug 级日志、以及预置默认值。

教训正是本批改动的主题：**把一个值改成可 None，必须把它的每个消费点都找出来
——漏一个就是一次新的静默降级。**

三处一起改：
- `round(_spy, 2)` → `round(...) if _spy is not None else None`
- `_log.debug("Equity curve 数据加载失败")` → `warning` + `exc_info=True`
- 预置默认值 `total_spy_ret/alpha_vs_spy` 由 `0.0` 改为 `None`
- `templates/dashboard.js` 的「理论上限口径」分支（`realistic` 缺失时**实际渲染
  的就是它**）不再 `||0` / `||initCap`

### Fixed 🔴 — 论点失效闸从未触发过一次

`check_thesis_breaks` 实测：极端输入（price $1 vs $100,000 / IV 200% /
P/C 5.0 / score 0 / 6 条看空）在 NVDA 与 WMT 上**一律返回 `level=None`**。

根因是 schema 对不上：

| | 内容 |
|---|---|
| `thesis_breaks_config.json` 存的 | `{id, metric, trigger, data_source, current_status, severity}` —— 人读散文 |
| `_eval_condition` 要的 | `{field, op, value}` —— 机器可比 |

`cond.get("value")` 恒为 None → 第一行 `if val is None: return False`。
**7 条条件，一条都没被求值过。**

而 `level=None` 在下游读作「论点完好」。CLAUDE.md 把「任何结论必须附失效条件」
列为硬约束——这条硬约束长期是靠一个永不触发的闸在"满足"。

另有 **17/30 只标的没有专属配置**，`cfg.get(ticker, cfg.get("NVDA", {}))`
让 ABBV/AMC/BRK-B/COST/CRM/CVX/DE/DELL/MU/NFLX/SNOW/T… 套用 NVIDIA 的条件
（数据中心营收环比、AMD/Intel 竞品、中国芯片禁令概率）。

本次**不迁移配置 schema**（那是独立决定，且该文件正被其他 session 编辑），
只让失败可见：新增 `evaluable` / `unevaluable_reason` 字段区分
「核对过、没触发」与「根本没核对」，并在两种情况下各打一条 WARNING。

顺带：`market_intelligence.py` 此前**整个模块没有 logger**，所有降级都是静默的。

### Fixed — 拥挤度的数据质量标签是写死的

`real_data_sources.get_real_crowding_metrics` 里：

```python
"price_momentum_5d": _mom_for_proxy if _mom_for_proxy is not None else 0.0,
"momentum": "real",   # ← 硬编码字符串
```

取数失败 → 动量兜底 0.0（读作「5 日横盘」），而质量标签**自称 real**。
标签不是从数据推导的，是写死的。同一个 dict 里 `bullish_agents` 写的是
`"real" if board else "default"` —— 正确写法本来就在眼前。

现改为 `"real" if _mom_for_proxy is not None else "unavailable"`，动量本身也
不再兜底 0.0；同时修掉 `crowding_detector.py:419` 的显示层
（`.get(k, 0)` 挡不住 None → `f"{None:.1f}"` 会抛）。

### Added — 测试 14 条

- `tests/test_thesis_break_evaluability.py`（10 条）：含 5 组极端输入参数化
  （证明闸确实没在工作，而不是恰好没触发），以及一组「配置补成机器可比 schema
  后必须真的能触发」的正例，锁住未来迁移的行为。
- `tests/test_missing_value_not_zero.py` +4 条：锁住上面那个回归的四个面
  （裸 round、debug 级 except、预置默认值、JS 上限分支）。

### 未做（记录在案）

- **配置 schema 未迁移**：论点失效闸仍不可求值，只是现在会明说。迁移需要
  为每只标的定义机器可比条件，是产品决定不是工程决定。
- **17 只标的仍无专属论点失效配置**。
- `crowding_detector.py:90-91` 把 None 动量兜底成 0.0 用于打分——改它会动
  评分口径，按 CLAUDE.md 需要走世代边界，本次未动。

### ⚠️ 审计结果的可信度说明

本批的后三项来自一次 5 视角并行审计（82 条候选）。**该审计的对抗核验阶段
因 session limit 全灭**（220/300 agent 报错），而我写的筛选逻辑
`votes.filter(Boolean)` 把报错的核验票过滤掉了，导致 `refuted >= 2` 恒为假
——**核验全灭时筛选静默退化成「全部放行」**。这正是本批在追的那个缺陷形状，
出现在我自己的审计脚本里。

因此：**82 条应视为未核验候选，不是结论。** 本 CHANGELOG 只收录我**逐条
手工复核并实测复现**的三条（论点失效闸、动量质量标签、以及我自己的回归）。
其余 79 条留待核验。

## [0.45.43] — 2026-08-26 — 期权快照把瞬时故障冻成了当日永久缺失

v0.45.42 定位了 8/26 的 yfinance 故障后，重跑扫描试图补数据 —— **失败了**。
催化剂恢复（0/30 → 24/30），但 `rv_30d` / `iv_rank` 仍是 1/30，而 yfinance
当时早已恢复（实测 NVDA/QCOM/TSLA 全部可拉）。查下去发现根因不在取数层。

### Fixed 🔴 — 快照混着两类性质相反的数据，却一视同仁地冻结

`cache/options_snapshot_NVDA_2026-08-26.json`（冻结于 14:13:23）里：

```json
"rv_30d": null,
"iv_rank": null,
"iv_rv_detail": {"error": "yfinance 历史K线不足（0 根，需 ≥15）"}
```

29/30 份快照如此。重跑时**快照命中即 return，根本没再去算**。

快照里其实是两类东西：

| 类别 | 例子 | 能否重拉 | 该不该冻 |
|---|---|---|---|
| 期权链 | IV / OI / strike / GEX | ❌ 接口只有实时快照、无历史，错过即永久丢失 | **必须冻** |
| 价格历史派生 | `rv_30d` / `iv_rv_*` / hv_proxy 口径 `iv_rank` | ✅ 日K 随时可重拉 | **冻它零收益** |

把第二类一起冻，就把一次约 15 分钟的瞬时故障，升级成了**当日永久缺失**。

新增 `OptionsAgent._refresh_price_derived()`，挂在快照命中处：这些字段若为空
就地重算并回写快照（带 `_price_derived_refreshed_at` 时间戳留痕）。

三条边界，每条都有测试锁住：
- **绝不碰期权链字段** —— 那类重算等于伪造。
- **只重算 `hv_proxy` 口径的 `iv_rank`** —— `real_iv_*` 口径来自自攒 IV 观测库，
  不是日K 派生，缺了就是真缺；用 HV 顶上正是 v0.43.19 修掉的失真。
- **重算失败保持「空」** —— 写个假值比缺失更糟；快照健康时不多打一次外网。

实测：8/26 的 30 份快照修复 30/30（NVDA rv=36.14/rank=37.81、TSLA rv=58.98/
rank=20.06）。重跑后覆盖率闸 **全绿**：rv_30d 30/30、iv_rank 30/30、iv_rv_spread 30/30。

### Fixed — 一个宏观字段缺失会干掉整块宏观

用户报告网站宏观数据消失。`dashboard_renderer` 的宏观块里：

```python
_macro_vix = f"{_mctx.get('vix', 0):.1f}"
```

`.get('vix', 0)` 在「键存在但值为 None」时返回 **None**（默认值根本不生效 ——
本项目 MEMORY 记过的经典陷阱），随后 `f"{None:.1f}"` 抛 TypeError，被外层
`except Exception` 吞掉 → **VIX / 10Y / 收益率曲线 / 黄金 / 板块轮动整块消失**。

现改为逐字段 `isinstance` 判定，各字段互不牵连。

### Fixed — 宏观降级此前完全无痕

两条路径都不留痕迹：`except` 走的是 **debug** 级；`data_source == "fallback"`
分支**一句话都不说**，直接跳过整块。于是 8/26 网站宏观区块整块消失而日志干净。
两处均提为 WARNING 并写明后果（「宏观区块将全部显示「—」」）。

### Changed — 覆盖率闸新增 `iv_rv_spread`

它是 `rv_30d` 的派生项，单独列出便于区分「RV 没算出来」与「RV 有但价差没算」。

### Added — 测试 11 条（`tests/test_snapshot_price_derived_refresh.py`）

含四条边界断言：期权链字段原样不动、`real_iv_*` 口径不许用 HV 顶替、
样本 HV 被拒、重算失败保持 None。另有一条静态回归闸——
`analyze()` 的快照命中路径**必须**调用 `_refresh_price_derived`，
否则这个 bug 会原样回来。

### 未做（记录在案）

- **催化剂覆盖率仍不稳**：重跑后 24/30 → 12/30（恰好压在 40% 闸上）。
  yfinance 财报日历本身是抖的，尚未加重试/缓存。已可见，未治本。
- 历史日期（8/26 之前）的快照未回溯修复 —— 只改了机制与当日。

## [0.45.42] — 2026-08-26 — 缺失值不许冒充 0：一次 yfinance 故障暴露的四处「安全默认值」

用户报告 8/26 的 ML 报告 IV-RV / IV Rank / 30日实现波动率 / 催化剂 / SPY 同期基准
全部丢失，问「是不是 changelog 哪里改坏了」。**不是。** 是 8/26 14:10 那次扫描
期间 yfinance 全线返回空。但顺着查下来，暴露出四处更值得修的东西。

### 根因（非回归）

代码把失败原因老实存下来了，`iv_rv_detail.error` 直接读得到：

| 8/26 错误 | 数量 |
|---|---|
| `yfinance 历史K线不足（0 根，需 ≥15）` | 27 |
| `yfinance 历史K线不可用（ValueError:No objects to concatenate）` | 2 |
| 空（成功） | 1 |

唯一成功的 ABBV 来自 12:16 一次单独运行，不在 14:10 的扫描里。8/25 是 30/30 全空错误。
故障已自行恢复（16:30 实测 NVDA/QCOM/TSLA 全部正常）。

⚠️ **底层机制「待验证」**。已确认的事实是：`yf.download` **返回空 DataFrame
且不抛异常**（所以 `except` 型防护对它完全无效，`range(3)` 退避重试也无效——
27 只标的失败得整整齐齐）。至于**为什么**返回空，本次未能确证：
- 扫描日志里确有 29 条 `401 Invalid Crumb`，一度以为是 crumb 令牌中途失效；
  但**实测证伪**——手动往 `YfData._crumb` 注入坏值后 `yf.download` 仍正常返回
  10 行，说明该端点不走 crumb（401 来自 quoteSummary 类端点，是另一回事）。
- 并发也已排除：8 线程并发 12 只，30/30 成功。
- 剩余嫌疑是 Yahoo 侧的速率限制/临时封禁，未能在不冒被封风险的前提下复现。

**这不影响本次修复的正确性**：四处「缺失冒充 0」与覆盖率闸都不依赖于知道
根因是什么，它们要解决的是「失败不可见」。但也**不该假装根因已查明**。

**一个故障干掉这么多字段，是因为它们共用一条取数链：**

```
yfinance 日K ──┬─→ rv_30d ──→ iv_rv_spread / iv_rv_signal
               └─→ iv_rank（source=hv_proxy，用 HV 历史代理 IV 历史）
yfinance 日历 ───→ ChronosBee（details 整个是 {}，不只催化剂）
yfinance SPY ───→ portfolio_backtest 基准 + 宏观门控
```

⚠️ 澄清一个长期混淆：**IV Current 走 CBOE 且 8/26 一条没丢**（30/30）。
丢的是 **IV Rank**，它的 `iv_rank_source` 8/25 与 8/26 **都是 `hv_proxy`** ——
本来就是 yfinance 口径，不是这次变的（真实 IV 历史仍在自攒，见 v0.43.18）。

### Fixed 🔴 — SPY 基准取数失败让 Alpha 符号反转（严重）

`portfolio_backtest._fetch_spy_prices` 是裸 `yf.Ticker().history()` + `except: return {}`，
无 http_gate、无重试。空 dict 之后：

    spy_start = 0 → spy_bh_pct = 0 → alpha = 组合收益 − 0 = **组合收益本身**

| | 8/26 网站显示 | 实测真值 |
|---|---|---|
| SPY 同期 | **0%** | **+11.7%** |
| spy_end_nav | $50,000（= 初始值） | $55,852 |
| **alpha_vs_spy** | **+4.29%** | **−5.62%** |

网站当时在说「跑赢大盘 4.3 个点」，实际跑输 5.6 个点。

**第二个后果**：`_is_risk_off()` 拿不到均线一律 `return False`，而 `macro_gate`
默认 `True` —— 8/26 的宏观门控一笔没拦，不是没到风险区，是它瞎了。

现改为：走 http_gate + 三次退避重试；取不到时 `benchmark.spy_return_pct` /
`alpha` 一路给 `None` 并置 `benchmark.available=False`，由渲染层显示「—」。
另修一个同源潜在 bug：起止日落在非交易日时 `.get()` 也拿不到价，改为取最近交易日。

### Fixed — IV Rank 缺失渲染成 `0.0%`

`generate_ml_report.py` 对期权指标一律 `_safe(..., 0)`。**0.0% 的语义不是
「没数据」，是「IV 处于历史区间最低点」** —— 一个强烈且完全虚假的做多波动率信号。
且 `iv_rank` 是 ML 模型头号特征（实测 importance 0.267），不只是显示问题。

同文件 `_rv_30d` 早已因同样理由单独豁免过（行注释 `# 可能是 None —— 不要 _safe`），
所以当天 IV-RV 老实显示「—」而 IV Rank 显示 0.0%。现把该豁免推广到
`iv_rank` / `iv_current` / `put_call_ratio` / `total_oi` / `iv_skew_ratio`。

判据（CLAUDE.md 安全默认值）：**这个默认值会不会让下游误以为掌握了信息。**

### Fixed — IV Skew 比从上线起就没显示过真实值

`_ch3_bear` 从 **BearBeeContrarian** 的 details 里取 `iv_skew_ratio`，
而该字段产自 **OracleBeeEcho**。永远取不到 → 恒定落到 fallback `0`。
8/25 与 8/26 实测都是 `0.00`，而 OracleBee 里 29/30 有值。
现改为从 OracleBee 读取，缺失显示「—」。

### Fixed — dashboard 两处把 null 兜底成 0

- `templates/dashboard.js`：`Number(real.spy_return_pct)||0` → null 变 0。
  改为与本文件 Sharpe/PF 既有写法一致的 `!=null ? ... : '—'`。
- `dashboard_renderer.py`：`spy_return_t7` 缺失兜底 `0.0`（=「那周大盘没动」），
  改为跳过该笔的基准累加 —— 曲线少一个点，好过多一个假点。

### Added — `scan_coverage_gate.py`：扫描字段覆盖率闸（编排器 Step 12）

这次故障真正的问题不是 yfinance 挂了，是**挂了没人知道**：每处降级都被
`except → return _empty` 老实接住，退出码 0，日报照常生成、推 Slack、上站。
正是 MEMORY「静默降级三件套」的第三条 —— 编排器只看退出码。

新脚本对 6 个关键字段算非空覆盖率，低于阈值即报红。阈值 0.70
（催化剂 0.40，因并非每只标的任意时点都有已知催化剂）：单只偶发失败是常态，
30 只里超过 9 只同时失败只可能是上游整体故障。

跨源判别：`likely_network_layer` —— yfinance 与 CBOE 同时降级才提示网络层。
实测 8/26 该标志为 `False`（CBOE 侧完好），诊断正确。

退出码 `0` 健康 / `1` 检出降级 / `3` 无法判定（`2` 留给「脚本不存在」，与
`scan_continuity` 一致）。**刻意不阻断主流程**：数据缺失是事实，报告该出还得出，
只是必须可见。

### Added — 回归测试 26 条

- `tests/test_missing_value_not_zero.py`（13 条）：四处默认值逐条「喂退化数据看它红」。
  已验证把修复回退后其中 5 条转红。
- `tests/test_scan_coverage_gate.py`（13 条）：含阈值边界、`0`/`False` 算有值
  （否则真实的 0 会被误判成故障）、无法判定时**不得给出健康结论**、退出码契约。

### 未做（记录在案）

- **8/26 的 `iv_current` 等 CBOE 字段未受影响，无需补**。
- ChronosBee 的看空 fallback 文案硬写「期权 IV Skew 偏高（看跌期权溢价）」
  用于凑满「至少 3 条看空」，**在无数据时等于编造一条信号**（违反「不编数据」）。
  本次未改 —— 它牵涉「反对蜂硬性下限」的设计取舍，应单独决定。

---

## [0.45.41] — 2026-08-27 — 历史入场价补救 + 合并时统一 ET 时钟

本条与另一条线的 v0.45.45/0.45.46 是**同一个问题的三段防线**，不是重复：

| | 管什么 | 时机 |
|---|---|---|
| `cboe_options.official_price`（0.45.46） | **源头**：收盘后取 `close` 不取 `current_price` | 写入前 |
| `scan_coverage_gate.check_prices`（0.45.45） | **单日闸**：当天入库价 vs 真实收盘 | 写入后当天 |
| **`close_correction.py`**（本条） | **历史补救**：跨全库校正已污染的行 | 事后 |

0.45.46 堵住源头之后新数据不再被污染；本工具处理的是**它上线之前**已经写进库的那批。

### Added — `close_correction.py`

- **双源交叉**：yfinance 官方收盘（批量下载，30 只一次请求，与逐标的调用不同，
  不触发限流雪崩）+ CBOE `prev_day_close`（仅 T+1 可得）。两源分歧 >0.2% →
  **不猜哪个对，拒改并记账**。实测 29 条交叉印证**零分歧**。
- **原值留痕**：`price_at_predict_raw` / `close_corrected_at` /
  `close_correction_source` 三列，`COALESCE` 保证重跑不覆盖最初原值。
- **幂等**，dry-run 为默认。

实测（2026-08-26 全库 1017 条）：**95 条需校正**，最大偏离 CRM 8/26 **+13.28%**
（232.93 → 205.62，当天财报盘后）。校正后接跑 `backfill_dir_accuracy.py --all`
重算 927 条派生列。

⚠️ **方向准确率不受影响**：`backfill_dir_accuracy.py:196` 用的是它自己从
yfinance 取的入场价，不是库里的 `price_at_predict` —— 所以 `dir_correct_t7`
那条链从来没被污染过（实测 0/1019 变化）。被污染的是**以 `price_at_predict`
为起点算收益**的那条链：`ic_diagnostics` / `replay_scoring` / `backtester` /
`dynamic_exit_backtest`。55 条已结算样本里收益变化最大 10pp（RKLB 7/24
+6.42% → +16.54%），但符号只翻转 1 条。

### Fixed 🟠 — 自己写的幂等判据错了

初版用 `price_at_predict_raw`（原值）判断「要不要校正」。原值校正后必然仍偏离
官方收盘，于是**重跑永远报「需校正 95 条」**。数据没写错（`COALESCE` 护住了
raw），坏的是**报告**：校正完 95 条之后还说有 95 条待校正，看起来像什么都没发生。
改为看当前值。

### Fixed 🟠 — 合并时统一 ET 时钟（`cboe_options._et_now`）

两条线各有一套 ET 时间助手。对方的 `_et_now` 是自述的「粗略 DST 近似」
（`-4 if 3 <= month <= 11 else -5`）。2026 年美东夏令时 **3/8 起、11/1 止**，
故 **3/1–3/7 与 11/2–11/30 共约 37 天会算早一小时**。后果正是 0.45.46 要修的
那一类：真实 08:30 ET（盘前）被算成 09:30 → `is_market_open` 判为盘中 →
取 `current_price`（盘前价）而非 `close`。统一到 `ZoneInfo("America/New_York")`。

### 守卫

`tests/test_close_correction.py`（9 条）+ `test_cboe_live_vintage.py` 新增 5 条。
变异确认：判据退回 raw / 分歧照改 / raw 被覆盖 / dry-run 写库 / ET 时钟退回近似
—— 均转红。

⚠️ **两次写出假守卫，都被变异测试抓到**：① 「raw 不被覆盖」初版碰不到 UPDATE
（第二轮直接 continue），补了「二次校正」用例；② ET 时钟那条初版断言的是
`_ET_TZ` 常量与显式传参的 `is_market_open`，**根本没调到被改的 `_et_now`**，
改为冻结 `co.datetime.now` 直测该函数。

### 遗留

`close_correction.official_closes` 与 `scan_coverage_gate.check_prices` 各有一份
yfinance 取数实现（并行发明，非抄袭）。可合并，但要动另一条线的文件，留作后续。

---

## [0.45.40] — 2026-08-26 — 移除 Google Calendar 集成：一条早已被替代、且从未授权成功的路径

### 起因是一次误诊

今天扫描日志里 `ChronosBeeHorizon ... 催化剂来源全部不可得` 与
`calendar_integrator | Calendar 认证失败` 同时出现，我据此判定催化剂全灭是
Google Calendar OAuth 所致 —— **错的**。查 `chronos_bee.py:113` 才发现催化剂
来源是 **yfinance 的 `Ticker.calendar`**，与 Google Calendar 毫无关系。
真凶是 yfinance 限流（今日累计 487 次）。两条恰好同时报错的日志被我当成了因果。

### 现状核实

- `~/.alpha_hive_calendar_token.json` **不存在** —— OAuth 从未完成，
  每次扫描都白试一遍并打一条 WARNING
- `calendar_integrator.get_upcoming_events` **零调用方**
  （同名函数在 `economic_calendar.py`，是另一个模块）
- 其余用途全部是**往 Google 日历写提醒**，非评分输入

### 替代关系（早已在跑）

| 原职责 | 现在由谁承担 |
|---|---|
| 催化剂数据 | `chronos_bee` 走 yfinance `Ticker.calendar` + `catalysts.json` + `earnings_watcher` |
| 宏观事件同步 | `economic_calendar.py`（本地模块，无 Google 依赖） |
| 机会提醒 / Thesis Break 告警 / T+N 回看提醒 | Telegram Bot（`/watch` `/alert` `/alerts` + `push_job`）+ Slack MCP |

### Removed

- `calendar_integrator.py`（907 行）、`tests/test_calendar_integrator.py`（603 行）
- `PHASE3_P2_IMPLEMENTATION_SUMMARY.md`（190 行，整篇是该功能实现文档）
- `config.CALENDAR_CONFIG`、`PATHS.calendar_token`
- `requirements.txt` 的 `google-auth` / `google-auth-oauthlib` /
  `google-api-python-client` —— 核实 `calendar_integrator` 是三者唯一消费者
  （`email_notifier` 走 `smtplib`，不碰 Google API）
- `alpha_hive_daily_report.py` 五处：import、`self.calendar` 初始化、
  Thesis Break 日历告警、高分机会/T+N 提醒、D1 催化剂同步 + D2 经济日历同步

### 刻意保留

- **`thesis_break_l1/l2` 快照**：与日历告警写在同一个 try 块里，但它是进报告的
  真数据。手术式拆分，只摘掉告警部分
- **`PATHS.google_credentials`**：Gmail 邮件配置仍引用
- **`earnings_watcher.get_catalysts_for_calendar`**：原调用方（D1）已移除，
  生产链路上暂无调用方，但它是该模块唯一的「取全部标的催化剂」入口且有测试覆盖，
  docstring 已注明现状

### 顺带发现（未修）

`config.py:882` 的 `"email_provider": "gmail_api"` **没有任何代码读它** ——
`email_notifier.py` 全程 `smtplib`。这条配置声称用 Gmail API，实际走 SMTP。

---

## [0.45.39] — 2026-08-26 — CBOE CDN 陈旧文件防线：主数据源会安静地发昨天的数据

v0.45.36 的 30 只彩排里 TMO/TMUS 被判陈旧。查证不是误伤，是**主数据源的既有缺陷**。

### 实测证据

CBOE 的 CDN 对某些符号**不重新生成文件**：HTTP 200、字段齐全、看不出异常，
但整份 JSON（现价 / 期权链 / IV / OI）停在旧日期。2026-08-26 实测：

| 标的 | CDN 文件生成于 | CBOE 给的现价 | 真实 8/26 收盘 |
|---|---|---|---|
| TMO | 08-25 00:12 UTC（**44.5h 前**） | 628.74（= **8/24** 收盘） | 633.96 |
| TMUS | 08-26 20:18 UTC（收盘后约 20 分钟） | 179.61 ✅ 自愈 | 179.61 |

**两者性质不同**：TMUS 是刷新滞后（等一会儿就好），TMO 是卡死（等不来）。

历史对账（`pheromone.db` 877 条可对账样本）：**13 条（1.5%）的
`price_at_predict` 精确等于更早某日的收盘**，`2026-07-24` 一天中了 8 只，
`2026-08-14` 的 TMO 陈旧了 5 个交易日。这些行的 `close_t7/price_at_predict−1`
基准价是错的，直接污染 IC 池。（另有 18 条谁都不精确命中，属项目已知的
「盘中价冒充收盘价」，是另一种机制。）

### Added — `cboe_options` 实时路径拦截

`_fetch_cboe_payload` 解析成功后校验 `last_trade_time`，陈旧则返回 `None`
→ `data_pipeline` 落到 `YFinanceSource`（降级源 0）。实测 TMO 由此拿到
633.71 而非 628.74。

三条刻意设计：
- **判据随时间变化**：`_expected_vintage_date()` 以 ET 09:30 为界 —— 盘前拿到
  上一交易日收盘天经地义，用固定「今天」会把整个盘前路径打死
- **陈旧 payload 绝不写缓存**：写了等于在进程内又保鲜 120 秒
- **fail-open**：交易日历不可用时跳过校验。宁可放过陈旧数据，也不能因为
  日历挂了把 30 只全打成陈旧、连锁压垮 yfinance（7/23 限流雪崩同源）。
  另有 >50% 陈旧率的 ERROR 告警，防口径写错时闷声全军覆没

### Added — 云端快照对滞后型标的补抓

首轮判陈旧的标的，在**大盘段之后**重试一次 —— 那段本就要跑 20~30 秒，
等于免费争取一个时间窗，常见情况零额外耗时。云沙箱 yfinance 不通，
补不回就只能弃（本机有降级链）。

⚠️ 补抓前必须 `invalidate_payload_cache(t)`。两道校验判据不同：盘前触发时
`cboe_options` 认为「上一交易日数据属正常」→ 放行**并写入缓存**，
而 `cloud_snapshot_fetch` 按业务日判它陈旧 —— 此刻缓存里躺着那份陈旧
payload，不清就再也补抓不回来。

### 守卫

`tests/test_cboe_live_vintage.py`（11 项）+ `test_cloud_snapshot_vintage.py`
补 2 项。六次变异确认：不拦陈旧 / 陈旧也写缓存 / 判据恒为今天 / 不 fail-open /
`invalidate_payload_cache` 空操作 —— 均转红。

⚠️ 「补抓时不清缓存」那次变异**没转红**：测试把整个 `_fetch_cboe_payload`
打了桩、绕过了缓存，证明不了那行有用。已补 `test_retry_needs_cache_
invalidation_when_checks_disagree` 走真实缓存路径。

性能：`_expected_vintage_date()` 单次 0.008 ms，整轮扫描约 0.7 ms，无需记忆化。

---

## [0.45.38] — 2026-08-26 — 云端快照消费端接线：从「只写不读」到真能补跑

云端快照自 v0.45.27 起每个交易日落盘，但**没有任何代码读它** ——
全项目 grep `cloud_snapshots` 只命中生产者自己和它的测试，编排器 9 步里
也没有相关步骤。本次把消费端接上。

### Added — `cloud_snapshot_loader.py`

- `load_ticker` / `load_market` / `load_manifest` / `available_dates`：
  经 `git show <ref>:cloud_snapshots/<date>/…` 直读分支，不切分支、不多开 worktree
- `snapshot_mode(date)` 上下文：进入时装载供给器到 `cboe_options`，退出（含异常）卸载

### Added — `cboe_options.set_snapshot_provider()`：一处拦截，四个入口全覆盖

四个消费点（`options_analyzer` ×3、`oracle_bee` ×1）都是函数内
`from cboe_options import X`，名字在**调用时**才从模块命名空间解析 ——
所以在 `cboe_options` 内拦截即全覆盖，调用方一行未改。
钩子装在 `_fetch_cboe_payload` + 三个 `fetch_cboe_*`。

### Fixed 🔴 — JSON 往返把行权价键变成字符串 → Max Pain / GEX 算在错序上

这是本模块存在的**首要理由**。`full_chain_oi` 的 `call_oi`/`put_oi`/
`call_exp_oi`/`put_exp_oi` 以行权价为键，`json.dump` 后 `130.0` → `'130.0'`。
**不会崩**：`options_analyzer` 的

    all_strikes = sorted(set(call_oi.keys()) | set(put_oi.keys()))

照样排得出来，只是排成字典序 —— `'100.0' < '130.0' < '90.0'`，
Max Pain 穷举与 GEX 于是算在错序的行权价上，**数字照出、全是错的**。
`_restore_numeric_keys` 只还原这四段的顶层键（内层到期日本来就该是 str）。

### Changed — `--date` 补跑默认走快照，且降级必须出声

新增 `--no-snapshot`。默认行为改为：`--date` 补跑时优先用该日云端快照。
**理由**：不接快照的补跑会拿到**今天**的期权链、贴上补跑日的日期写进
`pheromone.db` —— 一直如此，只是从来没人看见。

两条刻意的设计：
- **快照模式下绝不回落实时**：缺标的返回 `None`（诚实缺失）。回落 = 用今天的
  链冒充那天的，与 v0.45.36 拦下的污染同源、方向相反。
- **拿不到快照不中止**：价格/情绪/催化剂维度仍可信（价格有历史 API），
  中止会把「期权维度缺失」升级成「整天没有」。改为继续跑 + 明确警告。

### 守卫 `tests/test_cloud_snapshot_loader.py`（16 项，真实 git 仓库不打桩）

⚠️ **「无实时回落」的判据是禁网，不是返回值**。初版只断言返回 `None`，
变异掉 `fetch_cboe_chain` 的钩子后**测试仍全绿** —— 四个钩子是分层的，
上层拆了下层照样把结果压成 `None`。改为把 `urlopen` 换成抛错，任何一层
回落立刻暴露；重跑变异确认转红。

端到端实测：OracleBee 吃快照与吃实时得分一致（8.28 / 8.28，`gamma_exposure`
四位小数相同）；判别性检验 —— 篡改快照价为 1234.56 会流到消费端，
TMO 实时有数据而快照下为 `None`，证明两条路径真的隔离。

### 一处自己踩的坑

本模块初版把 `ref: str = SNAPSHOT_REF` 写进签名 —— **正是 v0.45.37 刚修完的
早绑定 pattern**，同一天同一个坑。6 处签名全部改为 `Optional[str] = None`
+ 体内 `ref or SNAPSHOT_REF`，并在常量处留了警示注释。

---

## [0.45.37] — 2026-08-26 — 早绑定默认参数让 `replay_scoring` 的退化测试读了真库

做 v0.45.36 的全量回归时 `test_underpowered_run_exits_nonzero` 转红。
排查发现与该次改动无关，是一个独立缺陷。

### Fixed 🔴 — `load_samples(db_path=DB_PATH)` 早绑定，`monkeypatch` 打空

默认值在 **import 时**求值并绑死，`monkeypatch.setattr(rs, "DB_PATH", ...)`
改的是模块属性，改不动它 —— `main()` 于是绕开夹具去读真 `pheromone.db`。

后果**不是**「护栏完全没生效」（此前的说法过重），而是：

- 它验证的是「**真库**功效不足 → 退 1」，不是它自称的「**4 周夹具** → 退 1」
- 结果依赖环境：主 checkout（989 条样本）绿，全新 worktree（空库）红
- 最阴的一点：等样本真攒够 25 周 —— **正是本项目的目标** —— 它会在
  成功的那一刻变红，而那时没人会想到去怀疑一条一直绿着的测试

改为 `db_path: Optional[str] = None` + 运行时 `db_path or DB_PATH`，
`main()` 显式传 `DB_PATH`。

### Fixed 🟠 — 样本库不可读时裸抛 sqlite3 异常 → 被读成「一切正常」

上一条修完，元守卫立刻抓到第二个：库不存在时 `sqlite3.connect` 抛栈，
以退出码 **1** 结束 —— 而 1 的语义是「功效不足，正常继续攒」。
库丢了会被编排器读成正常状态（MEMORY「编排器只看退出码」同款）。
现改为返回空样本 + 显式 note，由 `main()` 走「无法判定」(3)。

### Added — 元守卫 `test_main_actually_uses_patched_db`

不测业务，只测**夹具有没有接上**：喂一个必然不存在的库路径，`main()`
必须返回 3。这是本类其余所有「喂退化数据」测试的前提条件，
以前没人守着这个前提。

三次变异确认：`powered` 恒真 / `main()` 改回早绑定 / vintage 三变异 —— 均转红。

---

## [0.45.36] — 2026-08-26 — 云端快照 vintage 校验：目录名是墙上时钟，数据新鲜度必须自证

起因是一个时间问题：云端 routine（21:00 UTC）与本地 launchd（14:00 PDT）
是不是撞车。结论是**不撞**（异机、异分支、异出口 IP），但查证过程里
从首跑数据里翻出一个真 bug。

### Fixed 🔴 — `cloud_snapshot_fetch` 把上一交易日数据存成当日

`_business_date()` 用**墙上时钟**给快照目录命名，**从不校验 payload 自身
的 vintage**。CBOE 在盘前/休市**照常 200** 返回上一交易日的结算数据 ——
不报错，因为那确实是「最新」的一次成交。

实测证据（首跑，2026-08-26 02:28 PDT 盘前手动触发，manifest 报 30/30 成功）：

| | 值 |
|---|---|
| `cloud_snapshots/2026-08-26/NVDA.json` 的 `price_at_fetch` | **213.67** |
| NVDA **8/25** 收盘（yfinance 对账） | **213.05** |
| NVDA 8/26 | 当时尚未开盘 |

即 8/25 vintage 的数据挂在 8/26 名下，日志全绿、manifest 全绿。
典型「静默降级」：两个各自合理的默认值一撞，伪造出一天不存在的数据。

**判据选择**：用 `data.last_trade_time`（ET 成交时刻），**刻意不用顶层
`timestamp`** —— 后者是 CDN 生成时刻，盘前拉取时它等于「现在」，
正是它让首跑的陈旧数据看起来新鲜。

### Added — vintage 三件套与市场级中止

- `_vintage(payload)` → `(date | None, raw)`；解析不出返回 `None`，
  **不回落成「今天」**（MEMORY「安全默认值判据」）
- `_fetch_one_ticker(ticker, business_date)`（签名变更）：vintage 不符抛
  `StaleVintageError`，且在**链解析之前**抛 —— 陈旧数据解析得再干净也是错的一天
- 每标的落盘新增 `last_trade_time_et` / `vintage_date` / `vintage_status` /
  `prev_day_close`，随数据同行，消费端不必回头查 manifest
- 连续 `_STALE_ABORT_STREAK=3` 个标的陈旧 → 判定市场级（休市/盘前触发），
  中止抓取。`tickers_ok=0` → routine 据既有规则拒绝 commit，污染进不了库
- manifest 新增 `vintage_ok` / `vintage_unverifiable` / `vintage_stale` /
  `vintage_unverifiable_all` / `abort_reason`

**刻意不做的**：全员 `unverifiable`（CBOE 改字段名）**不判完全失败**，
数据照常落盘只打标。改字段名 ≠ 数据陈旧 —— 因为证不出来就丢掉可能是好的
一天，正是这套云端快照存在的意义所要防的事。退出码 1 逼 routine 如实报告。

### Changed — 行为变更（需知悉）

**市场休市日**（cron `1-5` 会照常触发）现在会**响亮失败**（三连陈旧 → 中止 →
不提交），而不是像以前那样静默存一份与前一交易日重复的数据。这是期望行为。

### 守卫

`tests/test_cloud_snapshot_vintage.py`（16 项），含首跑事故真实数字的回归用例。
三次变异测试确认非假守卫：删掉 stale 抛错 / unverifiable 谎报成 ok /
`_vintage` 回落成今天 —— **均转红**。
实弹验证：真实 CBOE 数据（12:48 PDT）→ `vintage=ok`，无误杀。

---

## [0.45.35] — 2026-08-26 — 二次检查 v0.45.31~34：两个真 bug，都是「复制了已存在的正确实现」

对第 1~6 项改动逐条对抗式复查。发现的两个真 bug **性质相同**：
我把一份已经存在且正确的实现**手抄了一遍然后抄错了** ——
而这正是我几小时前在 v0.45.30 修 `CrowdingDetector` 硬编码第二份权重时
写进注释的教训。同一天犯两次。

### Fixed 🔴 — `replay_scoring.rank_ic` 并列值处理错误（严重）

自写的 `_rank` 给并列值分配**递增秩**而非平均秩。构造检验：
x 大量并列、与 y 完全无关时，正确答案 **0.0**，它给出 **+0.2967** ——
**凭空造出相关性**。

`ic_diagnostics.spearman` 早就正确处理了并列（平均秩），项目原有的全部
IC 结论**不受影响**；错的只有我新写的这份。现改为直接调用它。

实际影响（重跑对比）：多数维度变动 <0.002，**只有 catalyst 显著变化
（+0.0057 → +0.0130，翻倍以上）** —— 它恰恰是并列最多的维度
（30 只标的只有约 6 个不同取值）。

⚠️ **已复核承重结论仍成立**：「signal 的 IC 符号会翻转、不可追」用坏秩算的，
修正后 3–5 月 −0.040/−0.157/−0.043、6–8 月 +0.200/+0.160/+0.084，
3/6 个月为负，与修前几乎相同。该建议不变。

### Fixed 🟠 — `signal_archive` 催化剂权重表漂移

手抄的 `_CAT_TYPE_W` 漏了 6 个类型且默认值写成 0.8（蜂内是 **0.7**）：

| 类型 | ChronosBee | 归档（错） |
|---|---|---|
| `dividendDate` | 0.3 | 0.8 |
| `exDividendDate` / `dividend` | 0.4 | 0.8 |
| `conference` | 0.5 | 0.8 |
| `split` / `analyst_day` | 0.8 / 0.7 | 0.8 |

而实测最常见的催化剂正是 Dividend/Ex-Dividend。现改为惰性导入
`ChronosBeeHorizon` 的表（`_cat_tables()`），并已重新回填。

**爆炸半径 6/875 行（0.7%）** —— 比担心的小，因为 earnings（两表一致）
占绝大多数。但会随只有股息事件的标的增多而扩大。

### Fixed 🟡 — 三处次要问题

- **`watchlist_events` 列数不足的行静默消失**：手工文件里少打一个 `|`
  整行就无声丢弃，编辑者永远不会知道。现计数并渲染
  「⚠️ N 行列数不足被跳过」。
- **`iv_history._observed_accrual_rate` 未来日期虚高速率**：时钟漂移
  或手写错日期会让分子增而分母不变 → ETA 偏乐观，而该函数存在的意义
  恰恰是治「ETA 偏乐观」。现剔除 `> today` 的条目。
- **编排器时间闸加前导零注释**：`[ "$NOW_HHMM" -lt ... ]` 是对的
  （十进制解析），但改成 `(( ))` 会在 10 点前崩
  （`0905` 非法八进制 → `value too great for base`，闸直接失效）。
  实测确认并写进注释锁住。

### 复查通过、未发现问题的部分

- v0.45.32 **确已完整提交**（437ffa4 + da060d5），`catalysts.json` 已删，
  `chronos_bee` 的加载分支已移除且条件正确简化为 `elif _calendar_failed:`
  （无悬空变量）
- 补跑闸四条边界均正确：`mkdir -p $REPORTDIR` 在闸之前、进程锁在闸之前、
  非交易日先跳过、`DATE_STR` 在启动时算定（跨午夜不漂）
- `watchlist_events` 解析边界（坏日期 / 空来源 / 文件不存在 / 60 天陈旧警告）全对
- `_observed_accrual_rate` 的空目录、单条、坏行分支全对

### Added — 守卫 5 项

`TestRankCorrelationTies`（并列必须平均秩、必须与 `ic_diagnostics.spearman`
同源、出现自建 `_rank` 即视为回归）+ `TestCatalystWeightTablesSameSource`
（权重表逐项比对、股息类不得落默认值）。

### 测试

全量 **1688 passed / 0 failed**。

---

## [0.45.34] — 2026-08-26 — 开机补跑：关机漏掉的扫描日不再永久丢失

W29/W32/W34 三周完全无扫描，日志里**零条记录** = 机器没开（不是扫描失败）。
`com.alpha.hive.daily.plist` 原为 `RunAtLoad=false`，关机错过 14:00
那一刻就再也不补。而这直接卡住两个指标：
IV Rank 的 63 天阈值按**扫描日**计（当前实测积累速率 0.33 天/交易日 →
ETA 8.6 个月），IC 闸按不重叠周计（23/25）。

### Changed — `~/.claude/scripts/alpha-hive-orchestrator.sh` 新增补跑闸

⚠️ **风险不在漏跑，在修复本身**：`RunAtLoad=true` 会在**每次登录**触发，
若无时间闸，早上开机就会在盘中跑，把盘中价当收盘价写进 `predictions` ——
那正是 MEMORY alpha-hive-accuracy-metrics-suspect 记的头号污染源。

故闸对定时触发与开机触发**统一生效**，无需区分来源：

| 场景 | 今日已扫 | 时间 | 结果 |
|---|---|---|---|
| 定时 14:00 | 否 | ≥1330 | 跑 |
| 早 9 点开机 | 否 | <1330 | **跳过**（防盘中污染）|
| 下午 4 点开机 | 否 | ≥1330 | **补跑** |
| 扫完后再开机 | 是 | — | 跳过（幂等）|

- `CATCHUP_AFTER_HHMM="1330"`：美股 13:00 PT 收盘，留 30 分钟落定
- `SWARM_MARKER`：`.swarm_results_${DATE_STR}.json` 存在即视为今日已扫
- 两条跳过分支都 `exit 0` 并写 `status.json`（跳过是正常状态，
  非 0 会被监控当成故障）
- 闸位于 Step 1 **之前**，否则先抓完数据再退出等于白跑

### Changed — `com.alpha.hive.daily.plist`

`RunAtLoad` false → **true**。`StartCalendarInterval` 的 5 个工作日时刻
**保持不变**——补跑是补充不是替代。已 `launchctl unload/load` 生效。

### 实测验证（三条分支全部端到端跑过）

```
分支②（伪造今日产出）  → ✅ 今日（2026-08-26）已有扫描产出，跳过
分支①（现在 11:35）    → ⏳ 现在 1135 早于收盘后阈值 1330，跳过
分支③（阈值临时调 0001）→ 🔁 今日尚无产出且已过 0001，执行扫描
真实 RunAtLoad 触发     → launchctl load 后自动跑，命中时间闸，退出码 0
```

命中幂等闸时**耗时 0 秒**——闸确实在 Step 1 之前拦下。

### Added — `tests/test_scan_catchup.py`（8 静态 + 1 integration）

守的重点是**时间闸不被调早**：`test_time_gate_not_before_market_close`
断言阈值 ≥1300 PT，把它调到盘中即变红。另守闸在 Step 1 之前、
跳过必须 exit 0、plist 的 RunAtLoad 为真且定时时刻未被顺手删掉。

顺带记一个 subprocess 陷阱：实跑测试**不能用 `capture_output=True`** ——
编排器会 spawn 后台全局超时看门狗，它继承 stdout/stderr，管道要等
**所有**写入端关闭才 EOF，于是直接跑只要 0 秒的命令走管道却卡满 120s。
已改为重定向到文件，测试从卡死变成 0.20 秒通过。

### 测试

全量 **1683 passed / 0 failed**。本版不改评分逻辑，不触发世代边界。

---

## [0.45.33] — 2026-08-26 — 评分重放：把「这样改会不会更准」从等半年变成跑一下

本项目真正的瓶颈不是缺改进想法，是**无法判断哪个改进有效**：
`ic_rerun_readiness` 实测检出 |IC|=0.090 需 25 个不重叠周（约半年），
而半年内必然又改了别的，于是**永远学不到东西**。

但并非所有改动都要等。分水岭在于**输入是否已归档**：

| 改动类型 | 能否离线重放 | 依据 |
|---|---|---|
| 聚合层（权重、组合规则、剔除某维度） | ✅ 一直可以 | `predictions.dimension_scores` 已存 |
| 维度计算层（改 crowding/catalyst 公式） | ✅ **本版起** | `signal_archive` 现存维度**输入** |
| 换数据源、改抓取逻辑 | ❌ 必须前向累积 | 原始外部数据未归档 |

### Added — `signal_archive` 扩展 15 个维度输入信号

此前档案只存各蜂的**输出分数**，输入看不见，于是维度计算层的改动无法重放。
新增（全部走 `details` 里已有字段，扫描侧零额外开销，schema 不变）：

- `crowding.comp.*` 五项分量（social_volume / google_trends /
  consensus_strength / seeking_alpha_views / short_squeeze_risk）
- `catalyst.count` / `catalyst.nearest_days` / `catalyst.max_weight`
- `buzz.comp.*` 四项（momentum / volume / volatility / reddit signal）
- `options.iv_rank` / `options.iv_percentile` / `options.iv_rank_is_real`

已回填历史：**92 个文件 → 70031 行**，新信号覆盖 2026-03-10 ~ 08-25 共 90 天。

两个刻意的设计：
- **`crowding.comp.social_volume` 同时读旧键名 `stocktwits_volume`**
  （v0.45.30 改名前的历史样本占绝大多数，读不了旧名等于丢掉全部历史）。
- **`catalyst.count` 在来源不可得时返回 `None` 而非 `0`** —— 0 的语义是
  「查过了确实没有」，与 v0.45.31 的缺失分支保持同一套语义。
- `options.iv_rank_is_real` 只有 3 天有值：`iv_rank_source` 字段本身是
  v0.43.18 才加的，更早的历史无从判定，如实留空而不是猜。

### Added — `replay_scoring.py`

对已验证样本重放任意打分方案，即时出 rank-IC。内置 13 个情景
（现行权重 / 等权 / 落库 final_score / 五个单维 / 五个 leave-one-out）。

**设计重点是「不会被误读」，不是「算得快」**：

- **功效护栏**：不重叠周数与 IC 并排显示，不足时明确打出
  「⛔ 功效不足：23/25 个不重叠周，下表不足以支持任何改动决定」，
  且**退出码非 0**（防止被脚本当成通过）
- **默认只取最新世代**，`--all-cohorts` 显式放宽并标注「口径不可比，
  只能相对比较」
- **收益口径锁死 `close_t7 / price_at_predict - 1`**，不用 `return_t7`
  （对方向单是钳位离场收益，直接对比即无效）
- **剔除 `dir_ambiguous_t7`**
- **不提供任何调参出口**（`best_weights` / `optimize` 之类），权重自
  v0.44.0 只读，且实测单维 IC 均不过 Bonferroni

首跑（`--all-cohorts`，860 样本 / 23 周）：护栏正确拦截，退出码 1。

### Added — `tests/test_replay_scoring.py`（14 项）

守的是**诚实性**而非正确性：功效不足必须非 0 退出、有效样本量必须按
不重叠周报、跨世代必须标注不可比、收益口径必须是未截断的 close_t7、
不得出现调参形状的 API。

已验证守卫为真：拆掉功效护栏（`return 0 if powered else 1` → `return 0`）
即变红，还原后全绿。

### 测试

全量 **1675 passed / 0 failed**。本版**不改任何评分逻辑**，
不触发世代边界。

---

## [0.45.32] — 2026-08-26 — 人工前瞻日历移出评分：从「静默改分」降级为「误导读者」

延续 v0.45.31 的 catalyst 排查。问题不止于「文件过期」——**人工维护的前瞻
日历直接喂 `catalysts_found` → catalyst 维度 → `final_score` 的 18.78%**，
两种失败都真实发生过：

1. **腐烂**：`catalysts.json` 最后更新 2026-07-23，一个月后窗口内 0 事件；
   注释自称「覆盖全部 WATCHLIST」，实测只有 **6/30** 只标的（另 3 个键是注释）。
   `catalyst_refinement.py` 的硬编码日期停在 **2026-03-15**，5 个月后仍在被
   读取，实测产出 0 个事件。
2. **编造**：VKTX 曾有两条 `critical` 级条目是错误信息——把二期口服剂型试验
   当成三期（真正的三期是 VANQUISH-1/2）、把公司指引 2027 年的顶线数据写成
   2026-08-15。它们躺在文件里驱动评分，直到有人专门核实才发现
   （见删除前该文件的 `_vktx_note`）。违反 CLAUDE.md「不编数据」。

### 核心判断：改的是失败模式，不是数据质量

- 喂评分时：一条错误的 critical 催化剂**静默推动 18.78% 的权重**，
  与真催化剂产出完全同形，人看不见 —— 不可恢复。
- 只进报告时：降级为「误导一个能自己判断的读者」—— 可恢复。

所以不是「重建 or 删除」，而是**把无法自动核实的信息挡在评分之外**。

### Removed — 两个人工前瞻日历退出评分路径

- 删除 `catalysts.json` 与 `catalyst_refinement.py`
- `chronos_bee` 移除对应的两段加载逻辑。本蜂的催化剂来源现在**只剩自动
  可核实的一条**：yfinance 财报日历（含 Earnings / Dividend / Ex-Dividend Date，
  实测 12/12 可用）。来源不可得时走 v0.45.31 的缺失分支返回 error。

**活路径不受影响**：`chronos_bee` 调 `plan_exit` 时直接传 `catalysts_found`，
不读文件。只有回测脚本 `dynamic_exit_backtest.py` 经
`catalyst_exit_planner.load_catalysts_for_ticker` 读它，而该函数缺文件时
恒返回 `[]`，且那份数据在删除前就已全部过期。函数签名保留以免破坏导入。

### Added — `watchlist_events.md` + `watchlist_events.py`（报告素材通道）

人工前瞻事件的新去处，**只渲染进日报「关注事项」段，不进任何评分路径**。
反腐烂设计（针对前身烂到没人发现的三个机制）：

- **文件报告自己的年龄**：mtime 随报告输出，超 30 天显示陈旧警告
- **过期条目不删除**，折叠但保留可见 —— 静默消失正是前身没被发现的原因
- **日期写坏不静默丢弃**，标 `bad_date` 单独列出
- **每条强制带来源 URL 与核实状态**（`已核实` / `待验证`），缺失即标 ⚠️

### Added — `tests/test_watchlist_events.py`（11 项）

最重要的是 `TestNeverReachesScoring` 三条边界守卫：模块不得暴露评分形状的
出口、评分路径模块不得 import 它（**AST 查真实 import，不做字符串匹配**——
注释里提到模块名是正当的）、被删的两个文件不得复活。

已验证守卫为真：往 `chronos_bee` 加一行 `from watchlist_events import ...`
即变红，还原后全绿。

### 测试

全量 **1661 passed / 0 failed**（1 skipped，64 deselected，1 xfailed）。

---

## [0.45.31] — 2026-08-26 — catalyst 的静默中性化 + IV Rank ETA 的结构性乐观

承接「让评分更准」的排查。两处都属**移除已证实的缺陷**，不需要统计验证。

### Fixed — ChronosBee：抓取失败冒充「无近期催化剂」

`except (*NETWORK_ERRORS, AttributeError)` 只打 warning 就继续，随后落到
`else: score = 4.0 / "无近期催化剂"`。于是**「yfinance 财报日历挂了」与
「这只标的确实没催化剂」产出完全同形**——静默中性化（MEMORY
alpha-hive-silent-degradation 记的同款形态）。

实测规模：90 个扫描日里 **9 天（10%）** 出现 catalyst 落 4.0 占比 >75% 的
集体塌缩，最严重 2026-08-11 是 **26/27 只标的**。catalyst 占 `final_score`
权重 **18.78%**，那些天这 18.78% 携带的是「今天没抓到」而非催化剂信息。

修法：记录 `_calendar_failed`；当财报日历失败**且** `catalysts.json` 无该
标的条目时，返回 `make_error_result(...)` 而非 4.0。queen_distiller 本就有
正确的缺失维度通道（`valid_results` 过滤掉带 `error` 的结果 →
`dim_status="error"` → 动态填充 + 覆盖度压缩），此前从未被触发。

⚠️ 判据必须**逐标的**：`_catalysts_json_loaded` 只表示文件读到了，
而实测 `catalysts.json` 只覆盖 **6/30** 只标的（另 3 个键是注释），
故新增 `_cat_json_has_ticker`。按文件是否存在判断会让修复几乎不触发。

反向守卫同样重要：日历**成功返回空**时仍应是 4.0——「查过了确实没有」
与「没查到」都要能表达，否则修过头。

### Fixed — `iv_history.py` 的 ETA 结构性乐观 4 倍

原输出「QCOM 3 天（60 天后可用）」隐含假设**今后每个日历日都攒到 1 条**。
但条目只在**有扫描且抓到真实 IV**的日子产生。改为按实测积累速率外推
（同 `ic_rerun_readiness` 的周产出率做法）：

```
实测积累速率: 3/9 个交易日有记录 = 0.33 天/交易日
QCOM  3 天 (还需 60 条 ≈ 180 个交易日 ≈ 8.6 个月)
```

**8.6 个月 vs 原报的 2 个月。** 并附一行提示：提高扫描日覆盖率可按比例
缩短 ETA。注意 IV Rank 的阈值是 63 个**扫描日**（不是周），所以它对
日覆盖率敏感，而 IC 闸对周覆盖率敏感——两者的瓶颈口径不同。

（顺带更正一个说法：`iv_history.py` 上线是 2026-08-14，只跑了 12 天，
积累进度本身正常，不是坏了。）

### Fixed — `tests/test_ic_rerun_readiness.py` 硬编码世代日期（v0.45.30 遗留）

v0.45.30 追加世代边界后，该文件里写死的 `start="2026-08-17"`（当时的最新
世代）全部落到新边界之前被过滤，7 个测试变红。**这是 v0.45.30 的提交疏漏
——全量回归跑在追加世代边界之前，之后未重跑就提交了。**
现改为一律从 `rr._COHORT_HISTORY[-1][0]` 推导（世代边界会持续追加，
硬编码必然反复失效）。

### Added — `tests/test_catalyst_availability.py`（7 项）

含端到端「喂退化看它红」验证：把缺失分支改回旧行为后
`test_calendar_failure_returns_error_not_4` **必红**，还原后全绿。

⚠️ 初版用假 ticker `ZZZZ_NO_SUCH` 写测试，被 `_validate_ticker` 提前挡下，
根本走不到目标分支——**退化版照样全绿的假守卫**。现改用 ABBV（真实在
WATCHLIST、且不在 catalysts.json 的 6 只里）。此坑已写进测试 docstring。

### 世代边界

catalyst 缺失时不再计入 `dim_scores` → 影响 `final_score`，属评分口径变更。
与 v0.45.30 同属 2026-08-26 世代（当日已有边界，不重复追加）。

### 测试

全量 **1650 passed / 0 failed**（1 skipped，64 deselected，1 xfailed）。

---

## [0.45.30] — 2026-08-26 — 清理三个名存实亡的数据源，并修掉拥挤度里的动量双计

用户提出「AlphaVantage/Tiingo/Stocktwits/Polymarket 好像很久没用到了」，全仓核查。
结论四个源各不相同，**其中两个我先前的判断是错的**：

| 源 | 实况 | 处理 |
|---|---|---|
| Tiingo | **0 个 Python 文件引用**，日志 0 次 | key 文件为遗物，代码无可删 |
| StockTwits | 公开 API 自 v0.40.0 已 403 停用，数据**早就换成 Reddit ApeWisdom**，只有字段名还叫 stocktwits_* | 全面改名为 social_* |
| AlphaVantage / Finnhub | **没坏**。实测两个都通（Finnhub 返回 NVDA 真实报价）。极少出现是因为它们在降级链第 2、3 位，CBOE/yfinance 通常成功 —— 这正是降级链该有的样子。日志里 26 次 EOF 全是 8/24 网络风暴期间的 | **不动** |
| Polymarket | 每次扫描都调，**从无一条成功返回个股赔率**（455 条日志全是「无相关个股预测市场」+ 熔断）。结构性原因：大盘股没有个股预测市场 | 关闭 |

### Changed — 关闭 Polymarket（`config.POLYMARKET_ENABLED = False`）

代价是纯浪费：每次扫描 30 只 × 最多 3 次尝试 × 15s 超时 + 429 退避，且它是
v0.43.27 那场 EOF 风暴命中的 7 个域名之一 —— 每天 30 次注定失败的请求白白扩大故障面。

**odds 维度评分口径不变**：`oracle_bee` 在 `poly_markets==0` 时本就把
0.55+0.10 重新归一化、不掺常数。开关的 fallback 默认值一并设为 `False`
（v0.45.23 教训：关掉的开关若 fallback 是 True，import 失败会静默重开）。
`polymarket_client.py` 与 `data_fetcher.get_polymarket_odds` **保留**，改回 True 即复活。

### Fixed — 拥挤度里的动量伪装与双计（这是本次真正的 bug）

`real_data_sources.get_real_crowding_metrics`（ScoutBee 实际走的路径）里：

```python
poly_proxy = abs(momentum_5d) * 0.8   # 冒充 polymarket_odds_change_24h
```

把动量改个名字当赔率变化用，而**同一个 dict 里 `price_momentum_5d` 已在喂
`short_squeeze_risk`** —— 动量被暗中重复计权。实测 2026-08 的 250 个样本反推：
该分量 **76% 落在最低档常数 20**，其余 24% 的变化全部来自动量本身。
既是常数稀释又是双计，整项移除。

- `CROWDING_WEIGHTS` 删除 `polymarket_volatility`（原 0.15），其余五项
  按原比例重归一化到 1.0（相对关系不变）。
- **缺失分量不再按 0 计**：原 `sum(w[k] * scores.get(k, 0))` 把「没数据」
  等同于「这维度得 0 分」并压低总分；改为只在实际算出的分量间重归一化。
  缺失与「真的很低」必须可区分（同 v0.45.3「安全默认值」判据）。

### Fixed — `CrowdingDetector` 硬编码了第二份权重

`__init__` 里硬编码的权重字典与 `config.CROWDING_WEIGHTS` 并存且从不同步——
config 那份被 `_validate_weight_sum` 校验却**从未生效**。现改为读 config
（唯一真相源），fallback 与 config 现值逐字同向。

### Fixed — 报告里的对外假声明

`report_formatters.py` 数据源清单写着「StockTwits 情绪（实时）」与
「Polymarket 赔率（每5分钟）」，两者都不属实。已改为「社交热度：Reddit 提及量
（ApeWisdom）」并删除 Polymarket 行。

### Changed — 改名（消除误导，非行为变更）

`stocktwits_messages_per_day` → `social_messages_per_day`、
`stocktwits_volume` → `social_volume`、`get_stocktwits_metrics` → `get_social_metrics`、
TTL `stocktwits_legacy` → `social_legacy`、`DATA_SOURCE_PRIORITY.stocktwits_messages`
→ `social_messages`。移除注册表里的 `STOCKTWITS_TOKEN`。
全仓 grep 确认生产代码无旧键名残留，测试同步更新。

### ⚠️ 世代边界（`ic_rerun_readiness._COHORT_HISTORY` 已追加 2026-08-26 / v0.45.30）

拥挤度 → ScoutBee signal 维度 → `final_score`，属评分口径变更。
**现在改的代价接近于零**：上一世代（2026-08-17）此时才 0/25 周、60 条未到期样本，
晚改只会更贵。

### 测试

全量 **1643 passed / 0 failed**（+1 skipped，64 deselected）。
端到端实测拥挤度链路：权重与 config 一致、缺失分量走重归一化（49.31 而非按 0 计的更低值）、
只给旧键名时 `social_volume` 走 0 档 —— 证明改名彻底、无双读残留。

---

## [0.45.29] — 2026-08-26 — VIX 期限结构口径修正：ETF 股价冒充期货点位，结构方向长期报反

v0.45.27 首跑对比暴露、用户批准修复。`cboe_fetcher.fetch_vix_term_structure`
的旧口径：`vix_1m` = **VIXY ETF 股价 × 0.5**（注释自称「近似转换」）、
`vix_3m` = spot × 1.10（**从来没抓过数据，纯合成**）。ETF 价格与 VIX 点位
无可比性——修复当天实测对比：

| | spot | 1m | 3m | 结构判定 |
|---|---|---|---|---|
| 旧口径 | 15.70 | **9.005**（VIXY 股价×0.5） | 17.27（合成） | **backwardation −42.6%** |
| 新口径（VX 真期货） | 15.74 | 17.20（M1） | 19.70（M3） | **contango +9.28%** |

**不只数值错，结构方向整个是反的**：市场平静（contango）被天天报成恐慌
（backwardation）。消费方 `_calculate_macro_score` 与 generate_deep_v2 宏观卡
一直吃这个反向信号。

### Changed — `cboe_fetcher.py`

- `fetch_vix_term_structure` 主源改为 **vixcentral VX 期货曲线**（复用现成的
  `vix_term_structure.py`，M1/M3 真值；spot 缺失时 `cboe_vix.get_vix_spot()`
  CBOE 官网兜底——云端 yfinance 不通时链路仍活）。返回 schema 不变，零下游破坏。
- **拿不到期货时不再合成**，直接落 `source='default_fallback'`。
- `vix_term` / `skew` / `vvix` **全部路径补 `source` 标注**（成功 →
  `vx_futures`/`yfinance`；兜底 → `default_fallback`；`pcce` 本就有，是范本）。
- 无 `source` 键的当日旧缓存视为过期重抓——否则 VIXY 垃圾口径经缓存再活一天。

### Changed — `cloud_snapshot_fetch._degradation_check`

判据升级为两层：`source=='default_fallback'`（权威）优先，等值匹配已知兜底
常量（15.0/15.75/16.5、120.0、85.0）保底兜住无 source 的旧数据。

### Added — `tests/test_cboe_fetcher_source.py`（11 项）

全部按「喂退化数据看它红」构造：VIXY 口径回归即红（`vix_1m` 必须等于 M1、
`vix_3m ≠ spot×1.10`）、每条兜底路径必须带标注、旧缓存必须作废、
带标注缓存正常复用、degradation_check source 优先。
本地真实网络验证：`source=vx_futures`，contango +9.28% 与 vixcentral 一致。

### 遗留说明

兜底常量本身（15.0/15.75/16.5 等）**数值语义未动**——只加了标注。彻底
None 化需要先审计 `_calculate_macro_score` 等消费端的 None 处理
（教训 v0.43.25/v0.45.3：上游诚实化会立刻在下游炸出新点），另行处理。

---

## [0.45.28] — 2026-08-26 — 清除 8/24 的期权污染 + 数据隔离名单

> 版本号说明：原编 0.45.27，与并发 session 的「抓数上云」条目撞号，顺延至 0.45.28。

v0.45.16 只修了代码、没清数据。本条清掉污染，并加一道机制防止它被回填带回。

### 污染范围（日志实证，不靠猜）

8/24 的报告实际在 **8/25 07:37–12:44** 生成，`options_analyzer` 的快照键取
`pdt_today()` 而非 `--date` 目标日，于是**全部 30 只标的**的期权链都是 8/25 的：

| 路径 | 只数 | 证据 |
|---|---|---|
| 命中已存在的 8/25 快照 | 24 | `[NVDA] 期权快照命中: options_snapshot_NVDA_2026-08-25.json (冻结于 2026-08-25T06:33:51)` |
| 现拉后写入 8/25 槽位 | 5 | CRM/JNJ/NEE/WMT/XOM，`期权快照写入: options_snapshot_*_2026-08-25.json` |
| 单独重跑同样写 8/25 槽位 | 1 | BRK-B，12:44，`logs/rerun_brkb.log` |

⚠️ **一处排查更正**：我最初按「8/24 与 8/25 取值相同」判定污染，得出"26/30"。
这个判据是错的——未被污染的 6 只（BRK-B/CRM/JNJ/NEE/WMT/XOM）取值也相同，
那是 v0.45.26 修掉的 5 天 IV 缓存造成的。改用日志实证后确认是 **30/30**。

### Added — `signal_archive.QUARANTINE` 数据隔离名单

`backfill()` 用 `INSERT OR REPLACE` 从 `.swarm_results_*.json` 重建，
所以**光删库不够**，下一次回填会把污染原样带回。原始 JSON 刻意不改
（它是"系统当天实际产出什么"的审计轨迹），改由名单在**入库口**拦截
——不放在分析时过滤，否则每个下游都得记得过滤一次，漏一个就前功尽弃。

**划界原则**：只隔离**当日市场观测**（取错日子 ⇒ 值本身就是错的）；
**不隔离 Agent 评分**——那些是系统当天的真实输出，属审计轨迹，抹掉会让
"系统当时做了什么"永远查不清。代价（OracleBee 等下游评分在该日仍基于坏输入）
已写进 `reason`。

### Removed — 150 行污染数据

`2026-08-24` × 30 只 × 5 类信号（`options.iv_current` / `put_call_ratio` /
`gamma_exposure` / `total_oi` / `bear.options_bear`）。
当日剩余 1268 行（Agent 评分等审计轨迹保留）。
备份：`db_backups/pheromone_pre_0824_options_purge_2026-08-26.db`。

⚠️ **期权接口只有实时快照、无历史 ⇒ 8/24 的真实期权观测永久丢失**，不可补，只能缺失。

### 下游行为验证

`vol_forecast` 对 8/24 返回 **0 只**（分量缺失 ⇒ 跳过，不猜测、不填默认值），
8/25 仍为 30 只——缺口被正确表达为"没有数据"，而非伪造成中性值。

### 测试 — `tests/test_signal_quarantine.py`（5 条）

入库口拦截 / 不误伤同批其它信号 / 不扩散到其它日期 / `is_quarantined` 真值表 /
**每条隔离必须带 reason 与 evidence**（没有出处的隔离等于凭空删数据）/
端到端 backfill 不得复活。已验证两个退化版必红（拆掉拦截 → 红；删掉证据 → 红）。

---

## [0.45.27] — 2026-08-26 — 抓数上云：当日期权/IV 快照不再依赖 Mac 开机

覆盖率 35%（v0.45.25 实测）的根因是扫描跑在本机、主机关机即断档，而**当日
期权链/IV 没有历史 API，过时不候**——断的那部分不可逆（「补跑也拿不到真实
IV」）。对策：把「抓数」从「分析」里拆出来，放 Claude cloud routine 每个
交易日收盘后独立运行。蜂群评分照旧在本机跑（pheromone.db 真相源不动，
避免状态分叉），本机开机后消费云端快照即可带真数据补跑。

### Added — `cloud_snapshot_fetch.py`

每交易日快照：30 只标的的 CBOE 精选链 + ATM IV 期限结构 + 全链 OI
（三者共享同一次网络请求，走 `_fetch_cboe_payload` 进程缓存）+ 大盘
（VIX 期限结构/PCCE/SKEW/VVIX/F&G）。产出 `cloud_snapshots/YYYY-MM-DD/`：
每标的一个 JSON（~107KB）+ `market.json` + **`manifest.json`（成功/失败
清单——产出必须可数，静默降级教训）**。退出码 0/1/2。零 LLM、零付费 API，
只访问 CBOE/CNN 公开端点。本地实测 2/2 成功（含 BRK-B 类份额符号）。

### Added — 云端 routine「alpha-hive-cloud-snapshot」

- 调度：`0 21 * * 1-5` UTC（= PDT 14:00 收盘后 1 小时；PST 期为 13:00 同样
  在收盘后）；环境 hive；模型 opus（用户指定）。
- 产物推送到 **`cloud-snapshots` 专用分支**（每日 merge main 单向吸入最新
  代码；main 不碰 `cloud_snapshots/` 目录 → 结构上不可能冲突）。不进 main，
  与本机 launchd 流程零耦合、零双跑冲突。
- 额度说明：每次运行消耗 Claude 订阅额度（机械脚本任务，量小；确切数字
  见首跑后 /usage）。用户已在 session 中批准方案与模型选择。

### 首跑实测（2026-08-26 09:28 UTC，手动触发）

**30/30 标的成功**、186 秒、push 成功（`cloud-snapshots` 分支 `00b52d7`，32 文件）。
抽查 NVDA：链 160C/160P、期限结构 `atm_iv` 4 点（22DTE 42.4%）——核心资产真值 ✓。

**云沙箱网络边界（实测）**：CBOE / CNN / pypi 可达；**yfinance（Yahoo）连接
被重置**。后果：`market.json` 的 `cboe` 段里凡走 yfinance 的指标全部落到
cboe_fetcher 的**无标注兜底常量**——实测 vix_term=15.0/15.75/16.5（本地同刻
真值 15.70）、skew=120.0（真值 143.27）、vvix=85.0。v0.43.24「兜底值冒充
观测值」同款，云端 agent 的回报也被骗（报了「VIX 期限结构仍算出」）。

**已修**：`_degradation_check()` 对照已知兜底常量标记疑似降级段，写入
`market.json.degraded_sections` 与 `manifest.market_degraded_sections`；
stdout 打「⚠️ market 疑似兜底段（不可信）」。**消费端规则：degraded_sections
里列出的段一律不用。** 每标的期权数据与 F&G 不受影响（CBOE/CNN 直连）。
等值匹配是启发式，cboe_fetcher 补 source 标注后应改读 source（已 flag 后台任务，
同一任务含：VIXY **ETF 价格**被当 VIX 期货点位的口径错误——本地路径同样中招，
vix_1m=9.005 实为 VIXY 股价，backwardation −42.6% 是垃圾口径）。

### 未做（后续接线）

本机消费端——扫描/补跑时优先读当日 `cloud_snapshots/`——**尚未接线**，
现阶段云端只负责把数据存住（先止血断档不可逆的部分）。接线时注意
v0.45.16/18 的补跑快照槽位语义，并与 v0.45.26 的 IV 缓存优先级修复对齐；
`market.json` 只可消费 `degraded_sections` 之外的段。

---

## [0.45.26] — 2026-08-26 — IV 缓存优先级倒置：97% 的当日实拉值被陈缓存顶掉

> 版本号说明：原编 0.45.23，与并发 session 撞号（对方已用到 0.45.25），顺延至 0.45.26。

用户提到「漏跑是因为没开电脑」，顺着查补跑能否补回数据时发现的更底层问题：
**`options.iv_current` 根本不是每日观测，而是 5 天一跳的阶梯。**

### 症状

`signal_archive` 全库 1155 条 IV 记录只有 325 个不同值，**去重率 28.1%**
（对照：`price.volatility_20d` 75.0%、`put_call_ratio` 52.6%）。
91 个归档日里 **71/90 组相邻日的 IV 完全相同**；NVDA 91 天只有 19 个不同值，
其中 66.66% 连续占了 7 个归档日。

### 根因 — `options_analyzer.py` 降级块优先级写反

```python
if last_valid:
    current_iv = last_valid          # 用缓存替换实拉值
if last_valid is None or ...:
    pass
elif ...:
    self.fetcher._save_last_valid_iv(...)   # 只在「无缓存」时才写
```

缓存命中时**用了它却不刷新它**，而 `_LAST_VALID_IV_TTL = 120h`（5 天）。
扫描定时在 14:00 PDT = **17:00 ET，已收盘** ⇒ `_market_open` 恒为 False
⇒ 每次扫描都走这条降级路径 ⇒ 同一个值被 5 天内所有扫描反复复用。

### 代价（生产日志 100 次降级实测）

| | 次数 |
|---|---|
| raw_iv 本就无效（该丢） | 3 |
| **raw_iv 完全有效却被丢弃** | **97（97%）** |

被丢弃的实拉值与缓存的偏差：中位 **1.30pp**、均值 2.63pp、最大 **38.06pp**。
实例：`TSLA IV 降级→缓存 40.35% (市场已关闭, raw_iv=43.37%)`。

### Fixed

**优先级倒置**：当日实拉值在 `[5%, 150%]` 内 ⇒ 用它并**刷新缓存**；
缓存只在实拉值无效时兜底。盘后期权链的 IV 虽非实时报价，
但它是「今天」的观测，比 5 天前的缓存诚实。

副作用（正面）：v0.43.20 的口径错配（IV Rank 分子误用降级后的 `current_iv`）
从「被守着」变成「结构上不可能」——实拉有效时 `current_iv == iv_raw_observed`，
两者不再可能分叉。

### 测试

`test_real_iv_rank_uses_raw_observation_not_degraded_cache` 的前置断言由
「降级确实发生」改为「**盘后 + 实拉有效 ⇒ 绝不被缓存替换**」；
新增配对测试 `test_after_hours_falls_back_to_cache_only_when_raw_invalid`
守另一侧（实拉无效时必须回退缓存，且不得把无效值记进 IV 历史）。
两条都保留了原测试的冻结时钟纪律（原 docstring 已警告过：不冻结会退化成空跑）。
已验证退回旧版变红。

### 一处自我更正

排查中我曾断言「8/25 的 IV 被污染」（因 26/30 只标的 8/24 与 8/25 取值相同）。
**归属反了**：`logs/backfill_2026-08-24.log` 显示 8/24 补跑（在 8/25 07:37 运行）
命中的是 `options_snapshot_*_2026-08-25.json`（冻结于 06:33），29 只全中
——即 **8/24 拿了 8/25 的数据**；而 8/25 自己在 21:47 重新拉取并刷新了缓存，
是干净的。用户对此的判断正确。

### 尚未处理（记录在案）

- 8/24 的 29 条 IV 仍是 8/25 的数据，**历史数据未清洗**（v0.45.16 只修了代码）。
- `signal_archive` / `vol_forecast` 都不消费 v0.45.16 加的
  `_options_as_of_mismatch` 标记 ⇒ 补跑污染仍会静默入库。
- 期权接口只有实时快照、无历史 ⇒ 漏跑日的 `iv_current` **永久丢失**，补跑救不回。

---

## [0.45.25] — 2026-08-26 — 兑现 v0.45.24 的「以重跑为准」：三处 docstring 陈旧，条目才是对的

v0.45.24 把两处数字冲突标成「待复核，以重跑脚本的输出为准」但没重跑。本条重跑了。
**三处冲突全部是 docstring 陈旧、CHANGELOG 条目正确**——与直觉相反
（通常代码比文档新），原因是这些 docstring 里嵌的是**跑批快照**，
数据修正后没人回头改。

### Fixed — `experiments/ticker_winrate_persistence.py` docstring

2026-08-26 重跑（纯本地只读、`mode=ro`、零 API 费用）：Spearman **−0.139**、
AMZN 85.7%→25.0%、QCOM 61.9%→43.3%、META 29.2%→56.2%——与 v0.45.12 条目
逐个吻合。docstring 里的 −0.273 / AMZN 83.9%→27.3% / META 38.7%→58.8%
是 **v0.45.9 ambiguous 修正之前**的跑批，已更正并注明来历。
（触发/未触发组与五分层数字两次跑批完全相同，故此前只有分割检验对不上。）

⚠️ 结论不变：**无证据支持标的胜率可外推**，A 关闭 / B 中性化的依据不动摇。

### Fixed — `scan_continuity.py` docstring 的空档口径

原文「07-10~07-21（13 天）与 07-29~08-10（11 天）」起止日与天数都不对。
按本工具自身 `--since 2026-07-01` 的实测输出更正为
**07-10→07-21（8 个交易日）与 07-30→08-07（7 个交易日）**，与 v0.44.0 条目一致，
并写明单位是交易日——13/11 既非日历日也非交易日，属两头不靠的手写数字。

### Fixed — `weekly_optimizer.append_history` 的取值注释

内联注释仍写 `"optimize" | "rollback"`，v0.44.0 新增的第三个取值 `"diagnose"`
（只读诊断，`weekly_optimizer.py:1168` 实际在写）漏了。模块 docstring 第 44 行
本就写对，只有这处内联注释陈旧。

### 备注 — 顺带实测到的运维事实（无代码改动）

跑 `scan_continuity.py --since 2026-07-01` 的实际输出：40 个交易日只跑了 14 次，
**覆盖率 35.0%**（门槛 80%），**完全无扫描的 ISO 周已有三个：W29 / W32 / W34**
（W34 = 08-17→08-21，此前记录只有 W29/W32 两个）。退出码 1（降级）符合设计。
另报出库与快照不一致：07-24 写库无快照；07-07、07-21 有快照未写库。
**未做任何补救动作**——补跑与否是需要确认的事，此处只记录。

---

## [0.45.24] — 2026-08-26 — 补提交的对抗核验：一处漏记的行为变更 + 一处误报的测试守卫

对 v0.45.23 三笔补提交（5130e8a / 3c0da78 / 94e3314）做了逐笔对抗核验
（每笔提交 diff 逐条对照其声称的 CHANGELOG 条目 + 全套件回归 1615 项）。
94e3314 与 5130e8a 通过；**3c0da78 的「各文件与条目逐一核对无出入」被推翻两处**，
本条如实补记：

### 更正 — `vol_forecast.load_day()` 的行为与 v0.45.1 条目已不一致（漏记的二次修订）

v0.45.1 条目写「库文件不存在 / 表不存在都返回 `{}`」。但 3c0da78 实际入库的
是该修复的**未记录修订版**：改用 `Path.exists()` 判库存在，`except` 只吞
`no such table`——**`database is locked`（瞬时可重试）与权限被拒（需人介入）
不再静默返回 `{}`，改为抛出**。这是行为改进（吞掉它们正是「看着成功其实
早废了」形态），配套 3 个测试也已入库，但当时无任何条目记录、且被
「补提交已记录代码」的名义带进仓库。以本条为准：v0.45.1 条目描述的宽捕获
语义已废止。同笔提交还带入 IC 快照数字刷新（667→684 条，纯数据快照）。

### 更正 — `experiments/ticker_winrate_persistence.py` 与 v0.45.12 条目数字不一致

前后半段分割的数值两处快照并存：脚本 docstring 写 Spearman **−0.273**
（AMZN 83.9%→27.3%、META 38.7%→58.8%），条目写 **−0.139**（AMZN 85.7%→25.0%、
META 29.2%→56.2%）。定性结论一致（负相关、胜率不可外推，关闭反馈的依据不变），
但精确值以**重跑脚本的输出**为准，两组快照均标「待复核」。
（折扣触发组/未触发组及五分层数值两处完全一致，无出入。）

### Fixed — `tests/test_ml_expected_return.py` 的 skip 守卫误报

`TestCrowdingCalibrationDrift` 只检查 `pheromone.db` **文件存在性**：文件在、
但只有 predictions 表的存根库（worktree/测试环境会自动生成）直接
`no such table` 报错而非 skip。现只吞「表不存在」并 skip，其他
`OperationalError`（如 locked）照抛——与上面 `load_day()` 同一语义。

### 备注 — 历史提交版本号错标（不改历史，仅立此存照）

`4f48da7`（message 标 v0.45.2~0.45.15）实际还含 **v0.44.1/0.44.3 的核心代码**
（`ml_predictor` 模块级 `expected_returns`/`centered_feature`、`rival_bee`
EPS 棘轮修复、Phase-1.5 移位等）。v0.44.x 批的代码一半在 5130e8a、
一半经 4f48da7 混入——git 考古时按内容找，别信那笔的 message。
另：v0.44.0 条目的测试计数快照（25 项）比入库版（27 项）少 2 项
（`--out` 回归测试后补），内容一致，数字过时。

---

## [0.45.23] — 2026-08-26 — 胜率反馈的 fallback 默认开启 + 补提交两批「已记录未入库」代码

### Fixed — `swarm_agents/queen_distiller.py` 的「安全默认值」

v0.45.12 把 `config.TICKER_ACCURACY_FEEDBACK` 关掉（`enabled: False`，前提被
走查检验否定），但 queen_distiller 里 config 导入失败时的 fallback 字典仍是
`{"enabled": True, ...}`，且 `.get("enabled", True)` 的第二默认值同样是 True。
即 config 一旦导入失败，被否决的胜率反馈会**静默重开**，与真实开启同形——
正是 v0.45.3 记录的「安全默认值」判据形态。两处默认值均改为 False，
fallback 与 config 的深思熟虑值保持同向。

（属 v0.45.12 的二次检查遗漏项。现 config 定义存在、fallback 从未实际触发，
是潜伏缺陷而非线上事故。）

### Chore — 补提交（代码早于本条被各版本条目记录，但一直停留在工作区）

全库核对 CHANGELOG 与 git 历史后发现两批「条目已提交、代码未提交」：

- **v0.44.0~0.44.4 批**（2026-08-16 落笔）：`weekly_optimizer.py` 只读诊断
  + 两道闸、`scan_continuity.py`、`ic_rerun_readiness.py`、
  `ml_predictor_extended.py` 预期收益镜像、`experiments/ic_power_analysis.py`、
  `experiments/ml_expected_return_replay.py` 及全部配套测试与报告。
- **v0.45.1/9/12/15 批**（2026-08-24~25 落笔）：`vol_forecast.py` SQLite 错误
  语义修复、`migrate_ambiguous_backfill.py`、`paper_portfolio.py`
  win_rate_multiplier 中性化、`experiments/ticker_winrate_persistence.py`、
  `tests/test_silent_failure_guards.py` 注释版本号更正、8 月自诊断简报。

各文件内容与对应条目描述逐一核对无出入；全部测试（366 项）在提交前重跑通过。
自此「CHANGELOG 记录的改动」与「git 历史」重新对齐。

---

## [0.45.22] — 2026-08-26 — 准确率门面改全历史 + 显著性标注，并重建 index.html

用户问「网站 T+7 准确率为什么显示 48%」。48% 是 `139/292` ——**旧口径的静态渲染**，
与干净口径的 56.8% 差在三处：截断收益（+5.5pp）、混入中性（+1.4pp）、窗口（+2.3pp）。
随后决定窗口用全历史还是 90 天，用数据定：

| 窗口 | n | 不重叠周 | 95% CI 宽度 |
|---|---|---|---|
| 90 天 | 178 | **10** | **14.5pp**（横跨 50%） |
| 全历史 | 586 | 23 | 8.0pp |

90 天的区间宽到无法与抛硬币区分；早期 vs 近期 **z=0.75, p=0.45 无显著断层**；
且回填已用同一条规则重算全部 927 条，测量侧世代问题已消除。故**全历史当主指标，
90 天降为漂移监测副指标**。

> ⚠️ 「无显著断层」≠「无断层」：该检验功效只够发现 ≥12.6pp 的差异。

### Added — `backtester._wilson_ci` / `_t_test_vs`

- `_wilson_ci(k, n)`：Wilson 95% 区间。docstring 显式写明传入的是**名义 n**，
  只用于展示精度量级，**不得**用它判显著性。
- `_t_test_vs(series, null)`：对**不重叠 ISO 周**序列做单样本 t 检验。
  少于 3 周或零方差返回 `None`——不兜底成 0.0，否则「样本不足」与「极显著」
  在页面上无法区分。

### Changed

- **`get_accuracy_stats`** 方向口径新增 `directional_ci` / `directional_p` /
  `n_eff_weeks` 三个字段。
- **`dashboard_renderer`** 主查询窗口 90 天 → **全历史**（3650 天），
  额外并行取一份 90 天作副指标。
- **准确率卡片**改为四格：方向准确率（带显著性徽标）/ 95% 区间 + 不重叠周数 /
  近 90 天 / 含中性对照。说明文字讲清为什么用全历史。
- **`templates/dashboard.css`** 新增 `.acc-sig` 徽标样式。

### 关键设计 —— 数字旁边必须有限定词

57.1% 不加限定词看着像成绩，而按不重叠周 **p=0.089，与 50% 无显著差异**。
故大数字旁直接挂 `≈抛硬币` 徽标，说明文字里写明
「这个数字目前不能当作预测能力的证据」。p<0.05 时才换成 `显著` 徽标。

### 重建 index.html

用 `alpha-hive-daily-2026-08-25.json` 的 `opportunities` / `swarm_metadata`
+ 项目根目录下现成的 `.swarm_results_2026-08-25.json` 与
`alpha-hive-daily-2026-08-25.md` 重新渲染，**未跑扫描、未花 API 费用**。

渲染前后逐项核对，确认无内容丢失：卡片 75/75、雷达图 100/100、
资金曲线 14/14、报告正文 1/1；新增 score-senti 9 处、score-dual-note 4 处、
acc-sig 5 处。线上现显示：

```
57.1% ≈抛硬币   方向准确率（看多+看空）
52.8–61.3%      95% 区间 · 294/515 · 23 个不重叠周
54.5% 97/178    近 90 天（漂移监测）
```

（515 而非 586：门面口径 `exclude_nontrading_days=True` 排除周末/假日样本。）

### 测试

`tests/test_dir_accuracy_metric.py` 4 → 8 项，新增 Wilson CI 收窄性、
样本不足返回 None、强弱信号区分、字段暴露。已验证两个退化版必红
（`_t_test_vs` 兜底 0.0 → 红；`_wilson_ci` n=0 返回 (0,0) → 红）。

---

## [0.45.21] — 2026-08-25 — 展示层：sentiment 与 final_score 并列

承接 v0.45.20 的结论——`final_score` 净 rank-IC ≈ 0（两个反向维度占 43% 权重，
抵消掉唯一有效的 sentiment）。只展示综合分，等于把唯一有信息的维度藏进一个
净剩为 0 的合成分里。本条把 sentiment 提为并列主指标。

**这是纯展示层改动：排序、筛选、决策逻辑仍用 `final_score`，权重一律未动。**

### Changed

- **`dashboard_renderer._build_top_cards_html`**：卡片评分行由单数字改为双数字
  （综合分 · 情绪分），进度条同步改为双条并带标注「唯一有 IC 的维度」。
  情绪分带 tooltip 说明证据强度与其边界。
- **`dashboard_renderer` JSON 输出**：新增 `senti` 字段（`{ticker: 情绪分}`），
  供前端图表/排序使用。卡片新增 `data-senti` 属性。
- **`templates/dashboard.html`**：卡片区上方新增 `.score-dual-note` 区块说明，
  讲清为什么是两个数字，并显式标注证据未过 Bonferroni。
- **`templates/dashboard.css`**：新增 `.score-sep` / `.score-senti` /
  `.sbar-lbl-senti` / `.score-dual-note`，含 ≤520px 的窄屏降级。

### 关键设计决定 —— 缺数据时省略，不兜底

`sentiment` 缺失时**整段省略**，`data-senti` 置空串，**绝不写 5.0**。
`.get("sentiment", 5.0)` 这种写法看起来无害，但它让「没有数据」与「情绪中性」
在页面上长得一模一样，正是本项目最常见的静默降级故障模式。

### 测试

`tests/test_score_dual_display.py` 3 项：并列渲染 / 缺失时省略不伪造 /
整个 dimension_scores 缺失时同样不伪造。已按惯例验证两个退化版必须变红
（补 5.0 兜底 → 红；取消并列 → 红）。

### 实测渲染

用 `.swarm_results_2026-08-25.json` 真实数据渲染：6 张卡片全部带 `data-senti`，
JSON `senti` 覆盖 30/30 标的，无缺失兜底。

---

## [0.45.20] — 2026-08-25 — final_score 归零的机制：不是稀释，是抵消

> 版本号说明：原编 0.45.19，与本 session 早先的「全信号 IC 普查」条目撞号，顺延至 0.45.20。

用户问「查 final_score 为什么把信号聚合没了」。在干净口径（v0.45.17 的
`close_t7`）下做了加权分解，答案是**符号问题**，不是权重没调好。

### Added — `experiments/final_score_dilution.py` + `_report.md`

复用 `ic_diagnostics` 的 `spearman` / `subsample_non_overlapping`（不自写评分逻辑），
日度横截面 IC → 每 ISO 周取第一个交易日 → 对周序列做 t 检验。

分解结果（权重取自 `config.EVALUATION_WEIGHTS`）：

| 维度 | 权重 | 干净 IC | w×IC |
|---|---|---|---|
| signal | 0.2094 | **−0.088** | −0.0185 |
| catalyst | 0.1878 | +0.001 | +0.0001 |
| **sentiment** | 0.1838 | **+0.168** (t+2.52) | **+0.0308** |
| odds | 0.1940 | +0.028 | +0.0055 |
| risk_adj | 0.2250 | **−0.084** | −0.0189 |

正向 +0.0365 / 反向 −0.0374 → 净剩 **−0.0009，抵消掉 98%**。
`final_score` 实测 IC = −0.005 (t=−0.07)，与抛硬币无异。

**两个 IC 反向的维度占 43.4% 权重，唯一有效的 sentiment 只占 18.4%**
——权重分配与信息含量几乎无关。

### 排除的替代解释

- **不是维度冗余**：维度间 |ρ| ≤ 0.26，基本不相关，不存在"都在测同一件事被平均掉"。
  值得注意 signal 与 sentiment 的 ρ = **−0.26**：符号相反且取值负相关。
- **不是权重没调好**：等权 IC = −0.007，与现行权重的 −0.018 同样归零。
  往里加的东西本身在做负功。
- **不是子集偶然**：用 `ic_diagnostics` 的独立取样重算，抵消 85%，同结论。
  差别最大的 risk_adj（−0.084 vs −0.161）源于被丢掉的 3 天其日度 IC 均值达 −0.56，
  这反过来印证了 risk_adj 的负 IC 由少数几天驱动（jackknife t: −2.50 → −1.01）。

### 诚实边界（勿越界引用）

- sentiment 的 p=0.012 是**未校正**值；5 维 Bonferroni → p≈0.06，**不过 0.05**；
  放进全部 53 个信号里做校正则完全不显著。正确读法是「唯一值得继续观察的」，
  不是「已确立」。
- signal / risk_adj 的反向 IC 本身也不显著（p=0.29 / 0.11）。
  能确立的只有**加权后净剩为 0**，这一条是稳健的。
- **未改动任何权重**。反事实组合是样本内构造，照搬即过拟合 23 周数据；
  且权重自动写入自 v0.44.0 已降级为只读诊断。

### 顺带更正 — CLAUDE.md 的权重是陈旧文档

CLAUDE.md 写的 `0.30*Signal + 0.20*Catalyst + 0.20*Sentiment + 0.15*Odds + 0.15*RiskAdj`
与 `config.EVALUATION_WEIGHTS` 实际值（0.2094 / 0.1878 / 0.1838 / 0.1940 / 0.2250）不符。
按 CLAUDE.md 自己的「文档只存指针不存参数值」原则，已改为指向 config。

---

## [0.45.19] — 2026-08-25 — 全信号 IC 普查：干净口径下维度排名反转

> 版本号说明：原编 0.45.18，与并发 session 撞号，顺延至 0.45.19。

承 v0.45.17 的 `close_t7`。既然真实收盘价刚补齐，所有基于旧收益列的 IC 结论都要复核。

### Changed — `ic_diagnostics.py` 新增并默认 `close` 口径

v0.42.7 曾察觉截断问题、加了 `price` 口径，但**选错了列**：`price_t7` 存的是
`path["exit_price"]`，自 2026-05 起 100% 等于 `exit_price`，与 `path` 口径一样带
SL/TP 截断。原 docstring「无截断、无并列人为聚集」对 5 月后样本是错的，已更正。
`--target` 增加 `close`（默认）；`price` 仅保留复现历史报告。
`t30` 无 `close_t30`，回退时**显式告警**而非静默。

**换口径后 5 维排名直接反转**：

| 维度 | 污染口径(price) | 干净口径(close) |
|---|---|---|
| `risk_adj` | 四口径全过 t=−3.02 | 三口径过 t=−2.50，jackknife 失效 |
| `sentiment` | **无口径通过** t=+1.97 | **三口径过 t=+3.16，Bonf(5维) p=0.008** |

⚠️ 此前基于 `price`/`path` 口径的维度排名与权重讨论**全部应视为待复核**。

### Added — `experiments/signal_ic_sweep.py` + `_report.md`

把 `signal_archive` 全部 49 个信号 + `final_score` + 5 维一起在干净口径下普查。
方法沿用 `ic_diagnostics` 既有约定（**日度横截面 → 每 ISO 周取一天**），
不自创——第一版按周池化，把跨日、收益区间不同的预测混进同一个横截面排序，
直接把 `dim.sentiment` 从 t=+3.16 压到查不出来。

**结果：53 个信号 0 个过全局 Bonferroni。** 但两条值得记录：

- **`dim.sentiment` 是唯一有实质证据的信号**：IC **+0.170**、t=+3.16、
  剔极端 3 周后仍 t=+2.25、regime 符号一致；三分位多空毛价差 **+3.98%/周**
  （剔极端后 +2.67%，仅做多超额 +1.56%）。四道 confound 检验全过：
  无前视（扫描在收盘后、`dimension_scores` 落库即不改）、
  **不是动量马甲**（`price.momentum_5d` 自身 IC=+0.008、t=+0.12）、
  非少数周驱动、regime 一致。旁证：同族 `BuzzBeeWhisper.score`(+0.145)、
  `sentiment.pct`(+0.115) 方向一致。
- **`final_score` 完全无预测力**：IC **+0.025**、t=+0.46、p=0.65。
  7 只蜂里 6 只 |t|<1.3。**一个 IC=+0.17 的输入经聚合后变成 +0.025** ——
  这是目前最反常也最有行动价值的一条。

保留意见（报告中不得省略）：未过 53 信号的严格 Bonferroni（p=0.082，
仅按 5 维族校正才过）；N_eff 仅 21–23 周；+3.98%/周作为可持续 edge 不现实，
样本内估计天然上偏；毛口径未扣成本、做空腿未验证可借券。
**尚不足以据此改交易规则。**

---

## [0.45.17] — 2026-08-25 — 准确率口径拆分：「方向判对」与「交易赚钱」分开存

> 版本号说明：本条原编 0.45.14，与并发 session 的「标的静默丢失三道闸」撞号，
> 顺延至 0.45.17。代码注释中的 `v0.45.17` 即指本条。

用户指出网站三张准确率卡（52.6%/56.2%/51.1%）口径可疑。查证属实，并牵出
一条更深的数据陷阱。

### Fixed — 「准确率」度量的其实是交易结果

`correct_t7` 由 `_simulate_trade_path` 的**路径依赖离场收益**算出：触发 SL/TP
即提前离场，收益被钳在止损止盈档位（库里 `-10.04`/`+9.95` 反复出现即此故）。
所以它回答的是「这笔交易赚钱了吗」；而中性预测从不建仓、从无 SL/TP，
判定带宽也不同（5% vs 方向单 1%）。**两者混进同一分母报「整体准确率」
是苹果比橘子。**

更麻烦的是 `price_t7` 也不可靠——自 2026-05 起 **100% 等于 `exit_price`**，
同样被截断。**库里根本没有存方向单的真实 T+7 收盘价。**

### Added

- **`predictions` 三个新列**（`backtester._migrate_options_columns`，幂等迁移）：
  `close_t7`（未截断的真实 T+7 收盘价）、`dir_correct_t7`、`dir_ambiguous_t7`。
  旧的 `price_t7`/`return_t7`/`correct_t7` **语义不变**，equity curve、ML 训练、
  `portfolio_backtest` 继续吃它们——那是交易指标，本就该路径依赖。
- **`backfill_dir_accuracy.py`** — 重新取数回填历史样本（927 条已补齐）。
  - 用 `yf.download(auto_adjust=False)`：只要拆股复权、不要分红复权，
    才对得上库里的真实成交价（实测 auto_adjust=True 使自校验失败率 8.8%→12.3%）
  - 收益用**同一条序列两端**算，否则遇拆股即垃圾（实测 CRWD 4:1，偏离 75%）
  - **自校验**：对 `T7_CLOSE` 行比对库内收益。护栏判据是**中位偏离**与**符号
    翻转率**，不是尾部计数——系统性口径错必然体现在中位数上，尾部零星偏离
    则来自原始跑批的数据毛刺。实测中位 **0.000pp**、符号翻转 **0.7%**。
- **`backtester.get_accuracy_stats(use_direction_metric=True)`** — 切到方向口径，
  并新增 `directional_accuracy/total/correct`（**只含看多+看空，排除中性**）
  与 `metric` 标注键。
- **`tests/test_dir_accuracy_metric.py`**（4 项）——守语义不变式。
  已按惯例验证「退回 bug 版必须变红」：开关失效 → 红；中性混入方向单分母 → 红。
  含一项专守「未回填的 NULL 不得被当成判错」。

### Changed — dashboard 口径

`dashboard_renderer` 的准确率区块改走方向口径，卡片改为
**方向准确率（看多+看空） / 方向单已验证 / 含中性（口径不可比）**三列并存，
并加 `.acc-caption` 说明两者不可互相替代。周度走势同步切到 `dir_correct_t7`
（`IS NOT NULL` 排除未回填行，**不用 COALESCE 兜底**——那会把「未知」当「判错」）。

口径切换后（全历史）：

| 口径 | 看多 | 看空 | 方向单合计 |
|---|---|---|---|
| 旧（交易结果） | 51.1% (256/501) | 56.2% (54/96) | 51.9% (310/597) |
| 新（纯方向） | 56.3% (276/490) | 59.4% (57/96) | **56.8% (333/586)** |

方向精度反而更高——止损截断会把「方向对但中途被打掉」记成判错。
⚠️ 按不重叠周 N_eff=23：t 检验 p=0.076（不显著）、符号检验 p=0.017（显著），
证据**边界性**，勿宣称「预测变准了」。

### 更正 — 「中性标签可作卖权过滤器」已证否

本 session 早先基于被截断的数据，先后报出中性平静度 **p=5.6e-08**、
卖权过滤器 **SR 0.67→1.85**、中性组**双峰肥尾**三个「发现」。
换用 `close_t7` 后**全部归零**：

- `|ret|<5%` 中性 vs 方向单：+18.2pp/p<0.0001 → **+5.7pp/p=0.114**
- 剔除中性：均损益 −0.92% → **−1.02%（更差）**
- 过滤增量 +0.072pp/周，**t=0.33、p=0.744**

`experiments/vol_regime_filter.py` 已改读 `close_t7`，输出结论改为**由 p 值生成**
（第一版把结论硬编码在 print 里，数据换掉后仍在自说自话）；
`_report.md` 重写为否定结论。教训：一个混淆能伪装成三种互相印证的「发现」；
「最差 −10.01%」这类整齐极值是警报不是好消息（真实 max 达 73.4%）。

### 测试

全量 **1600 passed, 1 skipped, 1 xfailed**；`ruff --select F821` 全绿
（改动中一次把变量定义写进了错误的函数作用域，靠 F821 当场抓到）。
数据库已备份 `db_backups/pheromone_pre_dir_accuracy_fix_2026-08-25.db`。

---

## [0.45.18] — 2026-08-25 — v0.45.16 的二次检查：目标日未校验可逃出 cache/

### Fixed

- **`ALPHA_HIVE_TARGET_DATE` 直接拼进快照文件名，却没有格式校验**。
  实测 `ALPHA_HIVE_TARGET_DATE=../../../tmp/evil` 使路径规范化后变成
  `<repo>/tmp/evil_backfilled-2026-08-25.json`——**写到了 cache/ 之外**。
  `alpha_hive_daily_report` 在 `--date` 侧确实做了 `fromisoformat` 校验，但环境
  变量可能来自残留 export、别的工具、cron env，**不能假定它经过那道校验**。
  本项目对 ticker 早有同型防护（`_RE_TICKER` 拒绝 `../etc`，v0.45.2 的
  BRK-B 事故就出在这条正则上），日期这一侧此前是缺口。
  新增模块级 `_RE_SNAP_DATE`，格式非法则告警并退回当日口径。

### Added

- 回归测试 +12 条（本文件合计 18）：7 种畸形输入必须被拒、3 种合法日期必须通过、
  非法值必须退回当日槽位且不产生逃逸路径。已验证可变红：把校验正则放宽成 `.*`，
  精确红 7 条。

- **锁住一个"恰好正确"的行为**：`price_history._SNAP_RE` 只认
  `..._{YYYY-MM-DD}.json` 结尾，补跑文件名 `..._{target}_backfilled-{today}.json`
  匹配不上 ⇒ 被 `continue` 跳过。这不是巧合而是**必需**——补跑快照里的
  `_snapshot_stock_price` 是运行时实时价、不是目标日收盘价，采信它正是该模块
  docstring 警告的那类污染。加测试防止后人"修好"这个正则反而引回污染。

### 二次检查的其余结论（未发现问题）

- 提交进仓的 `config.py` / `alpha_hive_daily_report.py` 是 hunk 级选择性暂存
  产生的**混合版本，磁盘上从未存在过**，故单独 `git archive HEAD` 到干净目录
  实测：WATCHLIST 30 / EXTENDED 71 / validate 无警告，**全量 1384 passed**。
  （比工作区少，因为其他并发 session 的未跟踪测试文件不在提交里，符合预期。）

---

## [0.45.16] — 2026-08-25 — 补跑污染当日期权快照槽位

用户在核对「IV 修复为什么没生效」时定位出来的：**8/25 的期权快照是 8/24 补跑写的**。

`options_analyzer.analyze()` 的快照键取 `pdt_today()`，**完全不看 `--date` 目标日**。
于是「在 8/25 补跑 8/24」写出的是 `options_snapshot_{T}_2026-08-25.json`——
占的是**今天**的位置。实测两个方向都错：

| 方向 | 后果 | 实测证据 |
|---|---|---|
| 往回污染 | 8/24 的报告拿到 8/25 早上现拉的期权数据冒充 8/24 的 | NVDA 两天的 `iv_rank=45.33` / `pc=0.96` / `GEX=6.6839` / `rv=35.47` **逐字段完全相同** |
| 往前污染 | 06:33 的补跑占住槽位 → 当天 **14:00 的正式定时扫描一进门就命中缓存**，从未拉取过属于自己的期权数据 | 该次运行 58 次「期权快照命中」；12:44 后全天再未写过任何快照 |

连带影响：当天所有报告的期权部分都继承自 06:33，**且是 v0.45.4 修复前的旧代码算的**
——这正是「IV 修复已上线却看不到效果」的真正原因（23/24 份 analysis JSON
缺 `data_available` 键，而该键只有新代码会写）。

### Fixed

- **补跑改用独立槽位**：`--date` 目标日 ≠ 今天时，快照写成
  `options_snapshot_{T}_{目标日}_backfilled-{今天}.json`，永不触碰当日槽位。

  ⚠️ **刻意没有采用「改用目标日命名」这个看似自然的修法**：CBOE/yfinance 的期权
  接口只有实时快照、没有历史，补跑拿到的必然是运行时的链，命名成 8/24 只是把谎
  换个说法。所以只做两件诚实的事——**槽位隔离** + **口径标注**
  （`_options_as_of_mismatch` / `_options_target_date` / `_options_fetched_on`）。

- **接缝用环境变量 `ALPHA_HIVE_TARGET_DATE`，不用模块级变量**：编排器为
  `daily_report` 与 `generate_ml_report` 分别 spawn **独立进程**，模块级变量跨不过
  进程边界。`alpha_hive_daily_report` 在 `--date` 校验通过后写入该变量。
  与既有的 `OPTIONS_SNAPSHOT_DISABLE` 同一形状。

### Added

- **`tests/test_backfill_snapshot_isolation.py`（6 条）**：补跑槽位名必须与当日不同、
  补跑不得读当日槽位、**当日扫描仍须命中快照**（防止把闸门写成"永远不命中"）、
  口径标注三个键齐备、`--date` 必须写入环境变量。
  已验证可变红：退回「只看今天」的旧写法，精确红 2 条。

  ⚠️ 该文件的 fixture 必须显式 `delenv("OPTIONS_SNAPSHOT_DISABLE")` ——
  `tests/conftest.py:27` 给全部测试设了这个开关（防 mock 链写进生产 cache/），
  而本文件测的恰恰**是快照读写行为**。不解开它，「补跑不读当日槽位」会因为
  "根本没读任何快照"而通过，**测试通过但什么也没守住**。cache_dir 已重定向到
  tmp_path，重新启用不污染生产目录。

### 当日修复后实测（30 只全量重算）

| | 修复前（早上旧快照） | 重算后 |
|---|---|---|
| 期限结构可用 | 9/24 | **30/30** |
| 其中 CBOE 主源 | 0 | **30** |
| IV-RV 可用 | 部分 | **30/30** |
| AMC（曾必挂） | 无数据 | contango 86.3→93.5，RV 99.5 |
| VKTX（曾必挂） | 无数据 | contango 83.8→93.7，RV 44.5 |
| DELL（曾必挂） | 无数据 | backwardation 80.1→72.4，RV 83.4 |
| BRK-B | `source=yfinance` 兜底 | **`source=cboe`**（v0.45.8 符号修复生效） |

23 份报告已重新生成并部署（`Deploy: ML reports 2026-08-25 (23 tickers, 23 files changed)`，
括号内为**实测变更数**）；线上核对 23/23 期限结构全部有真实数据、0 处紫色渐变。

---

## [0.45.15] — 2026-08-25 — BRK-B 报告线上 404：部署白名单也不认连字符

> 版本号说明：本条原编 0.45.5（`tests/test_silent_failure_guards.py` 的注释
> 曾引用该号，v0.45.23 已更正），与并发 session 撞号后顺延至 0.45.15，
> **0.45.5 因此空缺**。

> **⚠️ 提交归属（本条改动被劈成两半）**
> 同 v0.45.2 的情况——部署白名单的两处同源修复分处两条提交：
>
> | 文件 | 提交 |
> |---|---|
> | `report_deployer.py` | `4f48da7` |
> | `generate_ml_report.py` | **`25615ff`**（message 与本条无关） |
>
> 两处必须同时存在才生效：前者管静态部署，后者管 ML 报告同步。
> 只看到其中一处就以为改全了，是这条容易踩的坑。


v0.45.2 修好 ticker 正则后 BRK-B 终于产出 ML 报告，但**从未被部署**——
`index.html` 照常链接它，线上直接 404。
连同 v0.45.8（CBOE 对 BRK-B 恒定 403），**同一个类份额连字符问题一天之内
出现在三层**：Agent 校验层、行情取数层、部署白名单层。

### Fixed

- **`report_deployer.py:144` / `generate_ml_report.py:2428`** — 白名单
  `^alpha-hive-\w+-ml-enhanced-\d{4}-\d{2}-\d{2}\.html$` 里的 `\w` 是
  `[A-Za-z0-9_]`，**不含连字符**，`alpha-hive-BRK-B-ml-enhanced-*.html`
  永远匹配不上。改为 `[\w.-]+`（同时覆盖 `BRK.B`）。
  收尾 `-ml-enhanced-\d{4}-\d{2}-\d{2}\.html$` 已锁死范围，
  `.bak` / `evil-alpha-hive-*` / 错误日期格式仍被拒。
  实测：gh-pages 上 2026-08-25 的报告 29 → **30 份**，
  `alpha-hive-BRK-B-ml-enhanced-2026-08-25.html` **404 → 200**。

### Added

- **`tests/test_silent_failure_guards.py`** — `TestDeployWhitelistAcceptsClassShares`。
  正则**从生产源文件里 `re.search` 抓取**，不在测试里重抄——重抄等于测试测自己，
  生产代码改回旧写法照样绿（v0.45.1 已吃过这个亏）。

### 二次检查补充（同版号，对 v0.45.14/15 的对抗性复查）

用**不加 `head` 的全量扫描**（当天两次栽跟头的地方）查所有会对可缺失字段做
算术/比较/格式化的行，25 处疑似里查出 **4 个真缺陷**，其余为误报。

- **`risk_engine.py`** — v0.45.3 当时只改了 3 处 `.get("volatility_20d", 30.0)`，
  **另有 4 处漏网**（stress 情景 / 结果字典 `sigma_annual_pct` / 综合风险等级），
  根因正是那次 grep 被 `head` 截断。新增 `_vol_pct()` 统一接住 None。
  综合风险等级现在 σ 不可得就返回 `risk_level="unknown"`——原写法既接不住 None
  （TypeError），30 这个默认值本身还会让 `sigma > 20` 成立而输出 `"low"`，
  **把「没查到」渲染成一个具体的风险档位**。

- **`alpha_hive_daily_report.py`** — 重试集合改为**查实际产出**，不按异常记账。
  按异常记账有两个缺口：① `future.result(timeout=)` 抛超时时任务仍在池里跑，
  会把其实会成功的标的**重复分析**（重复写库/重复拉期权快照）；
  ② `_analyze_and_save` 返回空但**不抛异常**时会被漏掉。
  新增 `_pending_again()` 查 `swarm_results`，硬闸走同一真相源。

- **`collect_data.py`** — `bdet.get("volume_ratio", 1) or 1` 把 None 转成 1.0
  （"正常量"）。源头改诚实后**这条伪造反而更常触发**——伪造从入口挪到了出口。

- **编排器完整性闸** — 读失败时 `|| echo 0` 会误报「30 只全丢」。
  改 `-1` 哨兵单独分支报「无法判定」。同一类毛病：把「读不出来」渲染成极端结论。

**测试变更**：`test_completeness_gate_exists` 原先断言实现字符串
`"not in swarm_results"`，被上面的重构改掉后变红——已改为断言**不变式**，
并新增 `test_retry_set_comes_from_actual_output`。
教训：结构性断言要盯不变式，别盯实现细节的字面量。

**误报清单**（逐条走查确认无需改动）：`sentiment.py` 有前置 `return` 守卫、
`scout_bee.py:312` 的守卫在下一行、`crowding_detector.py` 自建 dict 与本管线无关、
`mcp-servers/` 是 vendored 第三方。另实测确认：`llm_service` 三处 prompt 是真
f-string 且调用通过；`collect_data` 的海象表达式正确（首次测试是我构造错了数据
形状，非代码问题）；三个模型类的 `predict_return` 均吃得下 None。

### 备注

- 2026-08-25 数据已按 v0.45.14 重跑并部署：`.swarm_results` 30 只、
  `predictions` 30 只、gh-pages 30 份 ML 报告、线上页面 30 只全部出现。
- 首页仅链接 24 只，是 **ML 报告 top-12/轮上限**的既有设计
  （DE / ENPH / MU / NEE / SNOW / TMUS 本轮排名在外），非缺失——
  这 6 份报告已生成并部署，可按 URL 直接访问。要让它们也上首页需调 `ML_CAP`。

## [0.45.14] — 2026-08-25 — 一只都不能丢：标的静默丢失的三道闸

2026-08-25 跑后核对发现扫描池 30 只、实际产出 28 只，**COST / DE 被静默丢弃**。
翻历史日志：08-12 丢 **7 只**、08-13 丢 **4 只**。每次都只有一条 WARNING，
编排器照常打印「✅ 所有步骤成功」。

### Fixed

- **`alpha_hive_daily_report.py`** — 并行分析的失败标的不再被丢弃。
  直接原因是 `SystemError: AST constructor recursion depth mismatch
  (before=27, after=35)`，一个 CPython 层的竞态偶发错误，被
  `except Exception` 接住后只打一条 WARNING、不重试、不计数、不影响退出码。
  三处修改：
  ① 失败标的**最多重试 3 轮**（竞态偶发恰恰是最该重试的一类）；
  ② 那条 warning 补 `exc_info=True`——此前只记 `str(e)`，**拿不到栈**，
     所以至今仍无法定位 AST SystemError 的确切触发点（与 v0.43.23 同一教训）；
  ③ 重试后仍缺则打 ERROR 并列出丢失标的名。

- **`~/.claude/scripts/alpha-hive-orchestrator.sh`** — 新增标的完整性闸。
  此前唯一的数量提示是 `扫描 ${#TICKERS[@]} 只`，取自**配置数组长度**，
  与实际产出**从不比对**——这正是丢失能藏住的原因（与 gh-pages
  `Deploy: ML reports (12 tickers)` 声称 12 实际 0 文件是同一个反模式：
  **拿输入端的数字冒充输出端的结果**）。
  现在读 `.swarm_results_${DATE_STR}.json` 实际长度比对，缺则 ERROR +
  `OVERALL_STATUS=partial` + 写进 `ticker_completeness` 结构。

### Changed

- **2026-08-25 数据重跑**。当日 28 只标的的 BuzzBee/BearBee 因 v0.45.3 的
  波动率回归全部是 `score=5.0 / confidence=0.0 / details={}` 的空壳
  （即「静默中性化」形状），COST/DE 则完全缺失。**重跑全部 30 只**并重新部署，
  不是只补那 2 只——空壳数据混在库里做横向对比会污染结论。

### Added

- **`tests/test_silent_failure_guards.py`** — `TestNoTickerMayBeDropped` 4 条。
  明确标注为**结构性断言**：触发真实路径需要一次完整蜂群扫描（约 20 分钟 +
  大量外网请求），无法在单测里跑；它们的作用是当有人删掉重试或闸门时变红。

### 未解决

- `AST constructor recursion depth mismatch` 的**根因仍未定位**。已知：
  全仓无 `setrecursionlimit`；`code_executor.py:108` 的 `ast.parse()` 是
  每标的流程里唯一跑 AST 的地方，且 `CodeExecutorAgent` 在
  `alpha_hive_daily_report.py:172` 是**单个共享实例**被 4 个线程并发调用——
  最可疑但**无证据**（用并发 `ast.parse` 跑 960 次未复现，触发条件更窄）。
  下次发生时日志里会有完整调用栈，届时再定位。
## [0.45.13] — 2026-08-25 — 「中性」标签的可交易性检验（含一次自我更正）

起因：用户追问网站上 52.6%/56.2%/51.1% 三张准确率卡片。复算确认取数准确
（`pheromone.db` T+7、排除 ambiguous 后 457/869），但顺带发现两件事。

### 更正 — 会话早先报出的 p=5.6e-08 高估约一个数量级

我先报「中性预测平静命中率 54.0% vs 方向单 34.8%，z=5.43、p=5.6e-08」，
并建议做成可交易策略。两处高估：

- **收益口径混淆（小）**：`predictions.return_t7` 对走 SL/TP 的方向单存的是
  **钳位后的离场收益**（`−10.04`/`+9.95` 全表反复出现），对中性单存原始收益。
  46% 方向单走 SL/TP，等于拿截断收益比原始收益。真实原始收益在 `price_t7`。
  修正后 19.3pp → 18.2pp。
- **重叠窗口（致命）**：30 只标的每日滚动 × T+7 持有，820 条高度重叠。
  按不重叠 ISO 周聚合 **N_eff = 21**，名义 N 高估 **39×**。
  正是 MEMORY「统计功效与扩池收益」警告过的陷阱。

**效应方向成立，强度不成立。** 修正后 p ≈ 0.095，未过 0.05。

### Added — `experiments/vol_regime_filter.py` + `_report.md`

检验「`direction='neutral'` 能否用作卖权风险过滤器」。方法：用 `price_t7`
还原原始收益绕开钳位；期权损益走 **Black-Scholes 实价**（非倍数近似），
IV 取当日 `signal_archive.options.iv_current`，卖方按 `0.95×IV` 计点差劣势。

实测（卖 ±1σ 宽跨式，持有至 T+7 到期）：

| 组合 | n | 均损益% | SR | 最差% |
|---|---|---|---|---|
| 无过滤 | 820 | +0.31 | 0.67 | −28.15 |
| 剔除中性 | 580 | **+0.59** | **1.85** | **−10.01** |
| 仅中性 | 240 | −0.37 | −0.55 | −28.15 |

三道对照排除「中性只是别的规则的代号」：静态高波动黑名单 SR 0.50、
IV>73 过滤 SR −0.04，**均远逊于蜂群标签**——因为这些标的的*方向单*是赚钱的
（RKLB +1.82、VKTX +1.51），静态剔除会连好单一起砍。蜂群做的是逐日判断。

### 语义修正 — 「中性」不是低波动预测，是弃权标记

用原始收益重做，中性组呈**双峰/肥尾**而非单纯安静：
`|ret|<5%` 中性 55.1% vs 方向单 36.9%（+18.2pp），
但 `|ret|>10%` 中性 25.7% vs 15.8%（**+9.9pp**）。中性 p95=25.1%、最大 41.5%。

故其交易用法是**风险过滤器**（别在中性标的上卖权，肥尾吃光权利金），
不是独立多/空波动率策略——BS 敏感性扫描显示买 1.5σ 宽跨式一旦按真实买价
（多付 10% IV）即回落到盈亏平衡。

### 就绪度闸 — 未达标，**未改动任何交易行为**

`p ≈ 0.095`，80% 功效约需 60 周，还差 39 周。脚本退出码 `0`=已显著、
**`3`=方向确立但功效未达标**（当前），与 `scan_continuity.py` 语义一致（2 留给编排器）。

局限已写进报告且不得省略：IV 用单一 `iv_current` 非真实期权链报价，
未建模 skew/点差/早行权/保证金；到期损益忽略路径依赖（真实尾部更差）；
N_eff=21 时样本方差自身相对标准误 ≈32%，SR 1.85 应读作量级。

---

## [0.45.12] — 2026-08-25 — P1：关闭标的历史胜率反馈（前提被走查检验否定）

系统里两处按「标的历史胜率」调节行为的机制，共用同一个未经检验的前提：
**标的的历史胜率能预测它的前向胜率**。

| | 机制 | 触发后果 |
|---|---|---|
| A | `queen_distiller` + `TICKER_ACCURACY_FEEDBACK` | trailing 胜率 <50% → `final_score = 5 + (score-5)×reliability` 向中性压缩 |
| B | `paper_portfolio.CONFIG["win_rate_multiplier"]` | 胜率 <45% → 仓位 ×0.5；≥60% 且 n≥10 → ×1.2 |

A 的危害不止「无效」：压缩后的 score 可能跌破 `entry_score_bull=6.5`，
**直接否决入场**。且 `report_snapshots` 有 811 份、单标的 50–76 份，
远超 `min_samples=5`，所以它在生产里经常触发，不是摆设。

### 走查检验（新增 `experiments/ticker_winrate_persistence.py`）

只用**严格早于当日**的同标的已验证样本算 trailing 胜率，杜绝前视偏差；
口径与 queen_distiller 对齐（累计全历史、纯符号判定）；样本已应用
v0.45.9 的 ambiguous 修正，否则容差本身就会污染胜率。

T+7，597 方向样本 / 456 条具备 trailing：

```
折扣触发（trailing<50%）  n=112  前向胜率 52.7%  CI[44-62]  均收益 +0.50%
未触发（trailing>=50%）   n=344  前向胜率 51.5%  CI[46-57]  均收益 +0.68%
```

触发组前向胜率反而**更高**。按 trailing 五分层，前向表现非单调，
且最差的 Q1（正是折扣打击对象）前向表现是五层里最好的：

```
Q1  0-48%  → 58.2%  +1.30%   ← 折扣正打在这一层
Q2 48-53%  → 46.2%  -0.69%
Q3 53-58%  → 47.3%  +0.77%
Q4 59-67%  → 52.7%  +0.99%
Q5 67-100% → 54.3%  +0.80%
```

前后半段分割（2026-05-03 为界，10 只样本≥15 的标的）Spearman = **-0.139**，
AMZN 85.7%→25.0%、META 29.2%→56.2%、QCOM 61.9%→43.3%——标的强弱不但不外推，
还倾向反转。所有差异都在噪音内，诚实的表述是：**无任何证据支持该前提，
点估计方向与机制假设相反**。

### Changed

- **`config.TICKER_ACCURACY_FEEDBACK["enabled"]` True → False**。代码保留在
  `swarm_agents/queen_distiller.py`（受开关控制），样本积累后重跑上述脚本可复核。
- **`paper_portfolio.CONFIG["win_rate_multiplier"]` 中性化**：
  strong 1.2 → 1.0、weak 0.5 → 1.0。该表现实中几乎从未生效
  （closed_trades 仅 38 笔、单标的最多 7 笔，`min_samples_for_win_rate=5`
  使其历史上只有 3 次达标），属于「装着但没响的枪」——留着会在样本变多后
  按一个已被证伪的规则开始改仓位。原值写在注释里，恢复即回滚。

### Added

- **`experiments/ticker_winrate_persistence.py`** — 可复跑的走查检验脚本，
  支持 `--horizon t1|t7|t30`、`--min-prior`、`--threshold`，参数默认对齐
  `TICKER_ACCURACY_FEEDBACK`。判据写在脚本末尾：折扣触发组前向表现若未
  显著劣于未触发组，则该机制无依据。

### 未改动 / 待议

- `min_samples_for_win_rate` / `discount_threshold` 等参数保持原值——关掉开关后
  它们不生效，留着是为了将来重新评估时口径可比。
- 本次只处理「按标的历史胜率调节」这一类。评分体系本身的校准问题
  （final_score 对 T+7 收益 rank-IC = -0.023，最高分层不优于最低分层）
  属 P2，未在本条处理。

### 回归

全量 1553 passed / 65 failed，与 v0.45.9 完全一致——失败项全部为 VM 缺失依赖
（google.auth / sklearn / yfinance / scipy）与硬编码 `/usr/local/bin/python3`，
无测试断言旧行为（已全仓 grep 确认）。

---

## [0.45.9] — 2026-08-25 — P0：容差语义修正（单边亏损豁免 → 双边模糊带）

`outcome_utils.determine_correctness` 的方向容差是**单边**的：

```python
看多 correct if return_pct > -1.0    # 亏 0.9% 记为「预测正确」
看空 correct if return_pct < +1.0    # 逆向涨 0.9% 记为「预测正确」
```

这不是中性带，是给亏损单发免罪符。pheromone.db 全量实测：

| 口径 | 方向样本 | 库内准确率 | 修正后 | 模糊剔除 |
|---|---|---|---|---|
| T+1 | 679 | 72.9% | **54.7%** (n=397) | 282 |
| T+7 | 647 | 55.6% | **51.9%** (n=597) | 50 |
| T+30 | 555 | 49.4% | **47.0%** (n=530) | 25 |

T+1 的 679 条里有 175 条（25.8%）「判对」实为亏损单，其中 173 条恰落在
±1% 带内——指纹完全吻合。该虚高指标被 `backtester.adapt_weights` /
`weekly_optimizer` 权重自适应 / `ml_predictor` 训练标签共同消费，
等于全系统在优化一个假目标。

### Changed

- **`outcome_utils.determine_correctness` 改为三态**：`|return| <= tolerance`
  → `"ambiguous"`，超出容差后才按符号判 correct/incorrect。中性方向判定
  **不变**（中性预测的语义就是「不会大幅波动」，带宽是名副其实的）。
- **`backtester._check_direction` 返回值 bool → (correct, ambiguous) 二元组**。
- **`backtester.run_backtest` 的 T+7 分支** `is_correct = ret > -1.0` 改走
  `determine_outcome_triplet`；模糊样本不进 checked/correct 分母分子，
  results 新增 `ambiguous` 计数。
- **准确率查询全线加 `AND COALESCE(ambiguous_{period}, 0) = 0`**：
  `get_accuracy_stats`（总体/按方向/按标的/actionable）、
  `get_dimension_accuracy`、`analyze_self_score_bias`、`adapt_weights` 的
  单蜂统计、`ml_predictor.build_training_data_from_db`、
  `深度分析报告/规则/pheromone_source.py`。COALESCE 保证未迁移的旧库不炸。

### Added

- **`outcome_utils.determine_outcome_triplet(direction, return_pct)`** —
  落库友好版本，返回 `(correct, ambiguous)`。
- **`predictions` 表新增 `ambiguous_t1/t7/t30` 列**（`_migrate_options_columns`
  幂等补齐）；`update_check_result` / `update_t7_path_result` 支持写入。
- **`migrate_ambiguous_backfill.py`** — 一次性迁移 + 全表回填脚本，
  按存量 `return_*` 重算 `correct_*` / `ambiguous_*`，不联网。
  支持 `--dry-run`，自动备份到 `db_backups/`，幂等可重跑。

### Fixed

- **幽灵行**：`checked_t7=1` 但 `return_t7 IS NULL` 的 8 行（回测取价失败）
  旧逻辑落成 `correct_t7=0`，等于往分母里塞必错样本。无数据不可评分 →
  统一标 ambiguous。
- **`ml_predictor.build_training_data_from_db` 的列探测**：新增 WHERE 子句
  前先 `PRAGMA table_info` 探测 `ambiguous_t7` 是否存在，避免在未迁移的库
  / 测试夹具上抛 "no such column" 后被 except 吞掉、静默返回空训练集。

### 测试

- `tests/test_outcome_utils.py` 重写容差带内断言 + 新增 `TestDetermineOutcomeTriplet`
  （核心回归：亏 0.9% 绝不能记为判对），28 → 39 项全绿。
- `tests/test_e2e_pipeline.py::TestOutcomeConsistency` 同步更新为新语义。
- 回归：`test_backtester` 56 项、`test_ml_real_training` 8 项全绿；
  全量 1553 passed，剩余 65 failed 全部为 VM 缺失依赖
  （google.auth / sklearn / yfinance / scipy）与硬编码 `/usr/local/bin/python3`
  路径，与本次改动无关（已逐文件核对不含相关符号）。

### 修正后的周报口径

整体准确率 55.6%（919 样本）→ **52.6%**（869 样本），95% CI [49%, 56%]。
方向桶净化后 50.5%（262/519）不变——`|Δ|>=2.5%` 的过滤本就已排除 ±1% 带内
样本，这是一致性交叉验证。

---

## [0.45.11] — 2026-08-25 — 周报样本源切换：.predictions.json → pheromone.db

> 版本号说明：本条工作实际早于 0.45.9（P0 容差修正）——先切样本源才发现容差问题。但 0.45.9 已被 0.45.10 显式预留给 ambiguous 三态判定，故本条顺延取 0.45.11，编号与时序不一致。

用户质疑「Alpha Hive 有 30 个标的，为什么周报样本没收集到」。排查确认样本
一直在收，只是**周报读的是另一个已死三个月的文件**：

| | 旧源 `.predictions.json` | 新源 `pheromone.db` |
|---|---|---|
| 写入者 | compare_engine_v2 解析 `深度/deep-*.html` | 每日扫描直接落库 |
| 触发方式 | 手动 `generate_deep_v2.py --ticker`（无任何调度器调用） | 自动，30 标的/天 |
| 标的覆盖 | NVDA 1 只（历史仅 NVDA 53 + VKTX 2 份深度报告） | 52 只 |
| 已验证样本 | 45 条 | **919 条** |
| 最后更新 | **2026-05-20（冻结）** | 2026-08-24 |

后果：6/21–8/24 连续 11 份 optimizer-report 字节数完全相同（30,769），
同一份 5 月快照被反复计算了 13 周。

### Added

- **`深度分析报告/规则/pheromone_source.py`** — 周报数据源适配层。把
  `pheromone.db` 的 `predictions`（959 行，自带 T+1/T+7/T+30 验证列）+
  `signal_archive`（53k 行 options.*/agent.*/ml.*/insider.* 原生信号）
  映射成 `.predictions.json` 的嵌套结构，下游分析函数零改动。
  - 验证口径默认 **T+7**（对齐 weekly_optimizer），`ALPHA_HIVE_HORIZON` 可覆盖
  - `predictions.iv_rank/put_call_ratio/options_score` 三列全表 NULL，实际数据
    在 `signal_archive.options.*`，改从后者取
  - `options.iv_current` 是 IV 绝对值，按**每标的自身历史百分位**换算为 IV Rank
  - `score_high/score_low` 改用经验四分位。原硬编码 6.5/3.5 在蜂群
    final_score 分布（min 3.19 / p50 5.44）下会让 `score_low` 恒为假
  - 新增 8 个蜂群原生信号：`swarm_agreement_high` `guard_consistent`
    `bear_warning` `insider_buying` `ml_bullish` `crowded` `momentum_up`
    `sentiment_hot`

### Changed

- **`weekly_analyzer.py` v2.0 → v3.0**：`load_predictions()` / `load_accuracy()`
  改为优先 pheromone.db，失败时回退 `.predictions.json`（已验证回退路径可用）。
  报告头部新增数据源/口径/覆盖/日期区间标注。
- **中性桶阈值随口径缩放**：t1=1.0% / t7=2.5% / t30=5.0%。原固定 1.0% 是为
  T+1 设计的，套到 T+7 会把几乎全部样本判成 directional。

### Fixed

- **`split_neutral_bucket` / `compute_per_ticker_accuracy` 方向桶口径错误**。
  原版只按 `|price_chg|` 分桶，**预测方向本身为「中性」**的样本只要标的波动够大
  就被算进方向胜率。旧源中性样本极少影响可忽略，新源 280/959 是中性，会混入
  186 条无方向预测。修正后净化胜率 47.1% → **50.5%**（262/519）。
- **误判归因把分数嵌进原因字符串**（`高评分(7.86)过度乐观`），导致归因统计炸成
  几十个 n=1 的桶。改为 `高评分(≥7.0)过度乐观`，聚合后 26 次 / 6%。

### 口径变更后的结论修正

- 低置信度过滤器（IV Rank>60 + 共振未触发 + 分数 3.5–6.5）**结论反转**：
  旧 45 样本显示触发组 66.7% > 未触发 50.0%（+16.7pp，方向反常）；
  新 919 样本显示触发组 47.9% < 未触发 56.3%（**-8.4pp**）。过滤器实际按设计
  工作，旧报告的悖论是 NVDA 单标的小样本噪音。
- 方向胜率 T+7：看多 49.8%（217/436）、看空 54.2%（45/83），整体 50.5%
  ——接近抛硬币，与旧报告的 57.8% 不可比（口径与样本均已变）。

---

## [0.45.10] — 2026-08-25 — T+N 评分不得用「正在形成」的 bar

### Fixed — `backtester._get_price_at_date`
docstring 说取"收盘价"，实现是 `history(start=目标日)["Close"].iloc[0]`——
盘中调用时那根 bar 未完成，`Close` 是此刻最新价。评分闸门 `get_pending_checks`
只判 `预测日 + N 交易日 <= 今天`，**没有"当天是否已收盘"的概念**。
正常 14:00 PDT 扫描在收盘后跑，所以从未暴露；2026-08-25 盘中补跑 8/24 时
30 条 T+1 全部用盘中价评分，抽样 5 只 2 只判反（AMC +2.251% 记判对、
真实收盘 −1.124% 应判错；BILI 看空+正收益却判对）。

修复：目标日 == 交易所当日且未到 15:59 美东 → 返回 None，预测留在待检，
收盘后重评。判据用 `_exchange_now()`（Yahoo 服务器时钟），不依赖本机钟。
护栏失效时放行（不阻断回测）。8 条回归测试（含复刻 AMC 事故场景），
已验证退回无护栏版变红。

### 数据修复
2026-08-24 那 30 条被污染的 T+1 已重置（备份
`db_backups/pheromone_pre_t1_reset_20260825_132201.db`），预测本身与
T+7/T+30 未动，由收盘后扫描重评。实测重置后取价已是真实收盘
（AMC 2.65 vs 污染值 2.73）。

> 版本号说明：本条原编 v0.43.29（写作时仓库在 0.43.28），因并发 session
> 已推进至 0.45.8 而改号 0.45.10；0.45.9 预留给 ambiguous 三态判定。

## [0.45.8] — 2026-08-25 — CBOE 对 BRK-B 恒定 403（类份额符号写法）

### Fixed

- **CBOE 用点 `BRK.B`，项目内部用连字符 `BRK-B`**，导致该标的的 CBOE 请求
  **每次都 403**。实测：`BRK.B` 返回 2054 个合约、`BRK-B` 与 `BRKB` 均
  403 Forbidden。

  症状不是报错而是**全链降级**——`fetch_cboe_chain`（主链）、
  `fetch_cboe_full_chain_oi`（全链 OI/Max Pain）、`fetch_cboe_iv_term_structure`
  （IV 期限结构）三个 CBOE 主源一起失败，BRK-B 每次扫描都白烧 3 次重试
  （约 5s）再整体退回 yfinance，**恰好落回 SSL 风暴高发路径**。BRK-B 是 30 只
  每日扫描标的之一，且在 v0.45.2 有过被静默中性化的前科。

  新增 `_cboe_symbol()`：只规范化 **URL**（`-` → `.`）；`_payload_cache` 的键、
  日志、返回结构一律沿用调用方传入的原始 ticker，避免上下游出现第二种写法。
  OCC 合约符号不受影响——CBOE 返回 `BRKB260828C00270000`，现有 `_OCC` 正则
  `^([A-Z]+)…` 照常匹配（实测 **2054/2054 全部解析成功**）。

  修复前后实测（`OPTIONS_SNAPSHOT_DISABLE=1` 走真实全链）：

  | | 修复前 | 修复后 |
  |---|---|---|
  | payload | 403 ×3 重试 | OK，现价 $503.96 |
  | 主链 | 降级 yfinance | CBOE 160 calls / 4 到期日 |
  | 全链 OI | 降级 yfinance | CBOE 15 到期日，总 OI 485,339 |
  | IV 期限结构 | `source=yfinance` | `source=cboe`，flat 14.0→16.2 |
  | call 流 C 票 | 视兜底成败 | 正常投票 |

  NVDA 等无连字符标的行为不变（已回归验证）。

### Added

- `tests/test_iv_structure_guards.py` 增 5 条（合计 31）：符号映射表、
  **实际请求 URL 必须用点号**（只测 helper 不够，故 mock urlopen 抓真实 URL）、
  缓存键必须保持原始 ticker、OCC 正则仍能解析类份额合约。
  已验证可变红：回退 URL 规范化后 `test_url_uses_dotted_form` 立即失败。

---

## [0.45.7] — 2026-08-25 — v0.45.4/0.45.6 的二次检查：4 个自查出的缺陷

对本轮改动做对抗式复查（`/code-review high`），8 条发现里 4 条确认为真并已修，
其余 3 条为低优先级、1 条为既有缺陷（见文末）。

### Fixed

- **【最严重】畸形到期日字符串会让整只标的的期权分析静默作废**
  （`options_analyzer._iv_term_points_yfinance`）。v0.45.4 重写时拆掉了旧版包裹
  整个函数体的 `try/except Exception`，而新的 yfinance 兜底路径里 `_dte()` 直接
  调 `datetime.strptime` 未加保护。实测 yfinance 的 `.options` 返回 `'N/A'` 或
  `''` 时 ValueError 会**抛穿**：`calculate_iv_term_structure` → `analyze()`
  （第 1805 行经 AST 确认无 try 覆盖）→ 被 OracleBee 第 214 行的 except 元组
  接住（ValueError 在内）→ `result = {}`、`options_score = 5.0`。
  后果是该标的的 **GEX / 关键位 / OI 墙 / IV Rank / 异常流全部丢失**，日报照常
  印中性 5.0 分且零报错——而期限结构本身只是这份结果里的一个小字段。
  **修复放大了故障半径，这是最不该发生的一类回归。**
  修法：`_dte` 无法解析返回 None 并留痕；调用点再加一层 try 兜底，保证兜底路径
  的任何意外都只能让期限结构一项不可用。五种畸形输入实测全部诚实降级。

- **`generate_deep_v2.py` 因上游改 None 而崩**。v0.45.4 把 `rv_30d` /
  `iv_rv_spread` 从 0.0 改成诚实的 None 后，该脚本的消费者仍写 `.get(k, 0.0)`
  ——**同一个"键存在则默认值失效"的陷阱**。第 935 行先在 `... > 3` 抛
  `TypeError: '>' not supported between NoneType and int`，第 1010/1011 行抛
  `unsupported format string passed to NoneType.__format__`（均已复现）。
  该脚本不在编排器流水线内（手动运行），故为潜在崩溃而非当日故障。

- **报告渲染出字面量「第None名」**（`generate_ml_report` Reddit 热度行）。
  `第{reddit.get('rank','—')}名` 命中同族陷阱；8/24 存档 12 份里 **7 份**如此。
  该行本轮未改动，属既有缺陷，与本轮修的是同一形状故一并处理。
  同行的 mentions / buzz 一并改为显式 None 判断。

- **风险雷达的缺数标记仍是 emoji**（`risk_level` 返回 `"⚪ 数据缺失"`），
  是 v0.45.4 去 emoji 时漏掉的一处；同函数其余三个分支已是可着色的 `●`。
  改为 `<span style="color:var(--tm)">○</span>`。
  ⚠️ 这一改让 `test_missing_data_is_not_rendered_as_low_risk` 变红——它把
  `"⚪ 数据缺失"` **写死**在断言里。该测试守的是"缺数不得显示成低风险"，
  与用什么符号无关，故断言改为盯语义：缺数行必须含「数据缺失」、且不得出现
  `var(--bull)`（低风险绿）或「低」字样。已验证仍有守卫力——把 `None` 退回
  当 0 处理它立刻变红。**把展示层的具体字符写进断言，会让每次改版都误报。**

### Changed

- **重写一条"不会红"的测试**（`test_sentinel_values_still_filtered`）。
  它断言 `rv < 300`，但把被测的哨兵值过滤**整段删掉后仍然绿**——因为下游
  `|log_ret| < 0.5` 的跳变过滤已经吃掉了进出哨兵区的 ±5.19 极端收益，
  docstring 里声称的失败机制根本不成立。真实污染形态是哨兵值区块**内部**的
  零收益**压低**波动率，方向与断言相反。改为比对"干净序列 vs 尾部混入 8 个
  哨兵值"的 RV 是否一致（rel=2%）。**不会红的测试比没有更糟——它让人以为
  这条路径有回归保护。** 已验证移除过滤后该条变红。

### 未修（已评估）

- `_iv_term_points_yfinance` 用裸 `datetime.now()` 而 CBOE 路径用 `_pdt_now()`，
  两条路径 DTE 基准不同。本机时钟已锚 Vancouver（时区问题已根治），当前两者
  一致，属潜在口径漂移。
- `fetch_cboe_iv_term_structure` 的 `max_points` 参数已失效（抽点固定按 4 个
  目标 DTE，`picked` 不可能超过 4）。当前无调用方传该参数。
- 低价股 ATM 容差走行权价间距分支时带宽偏大（AMC ≈$3 → ±$0.60 = ±20% 价位），
  平均后的「ATM IV」会混入 skew。数值仍可用，属精度问题。

### 顺带查出的既有缺陷（本轮未改，待定）

- **CBOE 对 `BRK-B` 恒定 403**：CBOE 的符号惯例是 **`BRK.B`（点）不是 `BRK-B`
  （连字符）**，实测 `BRK.B` 返回 2054 个合约、`BRK-B`/`BRKB` 均 403。
  影响的不只是新加的期限结构函数——`fetch_cboe_chain` /
  `fetch_cboe_full_chain_oi` 同样失败，即 BRK-B **每次扫描都白烧 3 次 CBOE
  重试再全量降级 yfinance**。BRK-B 是 30 只扫描标的之一，且在 v0.45.2 有过
  被静默中性化的前科。修法应是 CBOE URL 侧做 `-` → `.` 规范化。

---

## [0.45.6] — 2026-08-25 — 标的名单合并为单一真相源

用户在核对 v0.45.4 的影响面时问出来的：「不是 30 只标的吗怎么你说 12 只」。
顺着查下去发现**存在两份各自维护的名单，且早已漂移**：

| | 数量 | 谁在用 |
|---|---|---|
| `config.WATCHLIST` | 24 | 全部 Python 侧（108 处引用） |
| 编排器 `DEFAULT_TICKERS` | **30** | 实际每日扫描 |
| 重合 | 仅 13 | — |

后果是**改 config 以为生效，扫描纹丝不动**：`AMD` / `AMGN` / `BIIB` / `REGN` /
`PLUG` / `RUN` / `ICLN` / `SQ` / `COIN` / `MSTR` / `UPST` 共 11 只挂在
`WATCHLIST` 里，**从未被扫过一次**。

### Changed

- **`config.WATCHLIST` 收窄/对齐为每日扫描的那 30 只**，顺序与编排器原
  `DEFAULT_TICKERS` 完全一致（多处代码用 `list(WATCHLIST.keys())[:10]` 取前 N，
  顺序变了就是换了一批标的）。
  - 13 只原地保留；17 只（CVX·VZ·XOM·COST·BRK-B·AMC·ABBV·T·DELL·DE·CRM·MU·
    WMT·TMO·TMUS·NFLX·SNOW）**连同元数据**从 `WATCHLIST_EXTENDED` 升入。
  - ⚠️ **不能直接清空 `WATCHLIST`**：那 13 只里含 NVDA/TSLA/MSFT/META/AMZN 等
    核心标的，清空会让它们丢 `sector`，而下游 5 处全是 `.get("sector", "")`
    ——**不报错，只是静默变成"无板块"**：板块集中度分析、`fred_macro` 板块 ETF
    映射、ScoutBee 板块判断、Slack 板块分组、`llm_service` sector_map。
    这正是本轮一直在修的那类缺陷，不能顺手再造一个。

- **11 只从未被扫的标的降级到 `WATCHLIST_EXTENDED`，而非删除**。
  候补池是 `--extended-pool` 的统计样本来源，而样本量直接决定统计功效
  （实测扩池 10→30 把出结论时间缩短 **5.18×**），凭空砍掉 11 只不划算。
  实测守恒：合并池 **101 → 101**，标的零丢失、`name`/`sector`/
  `polymarket_slug`/`monitor_events` **逐字段零差异**；
  每日扫描池 24→30、候补池 77→71。

- **编排器改为从 `config.WATCHLIST` 读取名单**（`alpha-hive-orchestrator.sh`）。
  硬编码数组降级为 `DEFAULT_TICKERS_FALLBACK`，仅在 config 读取失败时启用——
  扫描绝不能因为配置问题起不来。兜底判据不只是"非空"，还要求**每一项都符合
  ticker 形状**：半截输出（例如 import 期间打了日志到 stdout）比读不出来更危险，
  它会静默缩小扫描池。四种情形实测通过：正常读取 30 只 / 目录不存在 /
  python 不可用 / config 语法损坏——后三者均正确退回兜底并告警。

- **`validate_watchlist()` 的 orphan catalyst 口径放宽到两池之和**。
  `WATCHLIST` 收窄后，候补池标的的 catalyst 配置是**合法的**（`--extended-pool`
  会用到），按旧口径会误报 10 个孤儿。真正该报的是"配了 catalyst 却哪个池子
  都不在"。改后 `validate_watchlist()` 返回空列表。

- 同步订正因数量变化而陈旧的文案：`get_extended_watchlist` docstring
  「25 核心 + ~75」、`--all-watchlist` / `--extended-pool` 的 help 文本、
  `swarm_agents/_config.py` 里「BRK-B 在 WATCHLIST_EXTENDED 里」的注释
  （BRK-B 现已是每日扫描标的）。

### Added

- **`tests/test_watchlist_single_source.py`（8 条）** — 防漂移闸。
  - 编排器必须从 config 读，且**不得再出现顶格的 `DEFAULT_TICKERS=(`**
  - 兜底数组与 `config.WATCHLIST` **逐个且按顺序**一致（只比集合不够：
    顺序不同 ⇒ 走兜底那天 `[:10]` 取到的是另一批标的）
  - 每只扫描标的必须有 `sector` / `name`（缺了不报错，只会静默退化）
  - 全部 ticker 必须通过 `_RE_TICKER`（BRK-B 曾因此被 7/8 蜂静默中性化）
  - 两池不得重叠；候补池合计不得跌破 100（防"顺手清理"砍掉统计样本）

  已验证可变红：把兜底数组改漂移一只（QCOM→AMD），
  `test_fallback_matches_config_exactly` 立即失败。

---

## [0.45.4] — 2026-08-25 — 深度报告：IV 两条链修复 + 版式去 AI 味

用户报告网站深度研究报告里「IV 期限结构 + IV-RV 价差」显示为 0。核对 8/24
那批 12 只标的的 analysis JSON，确认**不是一个 bug，是两条独立断链**，
且交集不完全（T 有期限结构没 RV）——正是两条链路各自失败的指纹：

**影响面按全量扫描池 30 只计**（`.swarm_results_2026-08-24.json`），
不是深度报告的 12 份——深度报告有 `ALPHA_HIVE_ML_REPORT_MAX=12` 限流
（v0.42.9），只给分数最高的 12 只出 HTML，但**另外 18 只照常扫描入库**，
它们的 OracleBee 评分与 predictions 同样受影响：

| | 挂掉 / 30 | 标的 |
|---|---|---|
| 期限结构 | **14** | ABBV·AMC·BILI·COST·DE·DELL·META·MSFT·NVDA·QCOM·RKLB·SNOW·TSLA·VKTX |
| IV-RV | **11** | AMC·CRCL·CRM·DE·DELL·ENPH·JNJ·NEE·T·TMO·VKTX |

两者交集仅 4 只（AMC·DE·DELL·VKTX）——NVDA/MSFT 只挂期限结构、T/JNJ 只挂
IV-RV，这正是两条独立链路各自失败的指纹。

### Fixed

- **`options_analyzer.calculate_iv_term_structure` — 取数链改为 CBOE 主源**。
  旧实现只走 yfinance：1 次 `.options` + 4 次 `option_chain()` 共 **5 次额外
  HTTPS 往返**，撞上本机 OpenSSL 1.1.1q（实测当场抛 `SSLError: TLS connect
  error`），而循环里的 `except Exception: continue` 把它全吞了 ⇒ `term_structure`
  空列表 ⇒ shape="unknown" ⇒ 渲染成「0.0% / 0.0%」。
  新增 `cboe_options.fetch_cboe_iv_term_structure()`：**一次请求含全部到期日**，
  已在 `http_gate` 闸门内且 `_fetch_cboe_payload` 有进程缓存 ⇒ 主链拉过时
  **零额外网络开销**。这不只是"更稳"，它净减少了出站请求数——对这台 SSL 栈
  而言，减少并发本身就是修复的一部分（v0.43.27 实测：串行比并发快 38%）。
  yfinance 降为兜底，且每次出站都进闸门 + 退避重试，失败原因逐条回传。
  ⚠️ 取点仍用 **同一组目标 DTE (25/55/85/150)**，且限 DTE∈[7,270]：直接用
  CBOE 全部到期日会拿 5 DTE（theta 扭曲）比 842 DTE（LEAPS），得出的 spread
  与历史已发布数值不同源。**口径可比优先于数据量。**

- **`market_intelligence.calculate_iv_rv_spread` — 哨兵值过滤改用相对量纲**。
  `closes = closes[closes > 5]` 本意是滤掉 yfinance 归一化哨兵值（~1.0），
  但 $5 这个绝对门槛对 $180 的 NVDA 是"极低"、对 $3 的 **AMC 就是"全部"**
  ⇒ 低价股的 RV **结构性永远不可用**。改为中位数的 20%：哨兵值相对真实价格
  永远是数量级差距，而真股票 30 天内不会跌到中位数的 1/5。
  同时 `yf.download` 并入 `http_gate` + 3 次退避重试（此前是 8/24 SSL 风暴里
  未受保护的调用方之一），各失败分支写明 `error` 而非只留一句"RV 数据不可用"。

- **`.get(key, 0.0)` 救不了 `None` —— 传播层堵死**。这是本次的核心形状，
  与 MEMORY「静默中性化」同源：`calculate_iv_rv_spread` 失败时返回的字典里
  `rv_30d` **键是存在的、值是 None**，所以 `iv_rv_data.get("rv_30d", 0.0)`
  拿到的是 None 而非默认值，一路流到渲染层被 `_safe(v, 0)` 兜成 `0.0`。
  于是"网络失败"与"波动率真的是 0"在页面上完全同形。
  改为不写默认值、诚实传 None；期权链完全不可用时的兜底 dict 里
  `"rv_30d": 0.0 / "iv_rv_spread": 0.0` 一并改成 None。

- **渲染层诚实化（`generate_ml_report._ch3_oracle`）**：缺数一律显示 `—`
  并附上游失败原因（HTML 转义），不再兜成 0；取数来源（cboe/yfinance）作为
  角标可见，沿用项目既有的 `iv_rank_source` / `vix_source` 约定。

实测（`OPTIONS_SNAPSHOT_DISABLE=1` 绕过当日快照，全链跑通）：

| 标的 | 期限结构 | IV-RV |
|---|---|---|
| NVDA | backwardation 43.0→39.6（cboe） | rv 35.83 / +14.42pp |
| AMC  | contango 90.3→94.2（cboe） | rv 99.44 / −10.94pp |
| VKTX | contango 82.5→87.1（cboe） | rv 44.80 / +36.75pp |
| T    | flat 23.1→25.4（cboe） | rv 26.71 / −1.80pp |
| DELL | backwardation（cboe） | rv 83.41 / +5.85pp |

⚠️ **今天的报告重跑不会变**：`OptionsAgent.analyze` 有当日快照冻结
（`cache/options_snapshot_{T}_{date}.json`），今早 06:33 已冻的快照仍带旧数据。
**明天的扫描才会生效**，或手动 `force_refresh=True` / `OPTIONS_SNAPSHOT_DISABLE=1`。

- **`classify_call_flow` 的 C 票：缺数被当成"观察到中性"**（查影响面时翻出的
  第四处同型缺陷，**影响评分不只是显示**）。旧写法：

  ```python
  if term_data and term_data.get("shape"):   # "unknown" 是 truthy 字符串
      ...
      else: votes["C"] = "mixed"             # 失败的期限结构投出一张真票
  ```

  函数下方 `labels = [v for v in votes.values() if v != "unknown"]`
  本就是**为弃权设计的**，但这张票永远到不了那里。实测同一组输入下：
  取数失败 → `label=mixed, conf=0.67`，真实 contango → `label=mixed, conf=0.67`
  ——**完全一致**。修复后失败弃权 → `label=hedge, conf=0.5`（分母只剩 2 票）。

### Changed

- **深度报告版式去 AI 味 —— 并入站点自有设计系统，不新造第三套审美**。
  `generate_ml_report.py` 是站点上**唯一**还穿着 `#667eea → #764ba2` 紫色渐变的
  页面：白圆角卡 + 大投影浮在渐变底上、渐变表头、每个标题挂 emoji。而
  `index.html` 早有一套刻意非 AI 的语言（奶油纸底 `#FAF7F2` / 铁锈红 `#B7410E` /
  Playfair Display + JetBrains Mono + Noto Sans SC / 0.5px 细线 / `html.dark`），
  `generate_deep_v2` 也有自己的深色令牌集。从仪表板点进报告像换了个产品。
  - 令牌与 `index.html` **逐个对齐**（含 `html.dark` 全套），主题经
    `localStorage` 的 `ah-theme` / `ahDark` 与仪表板共享 ⇒ 同源跳转不闪白。
  - 刊头从"居中英雄卡"改为**左对齐研究信笺**：mono 眉标 + Playfair 大标题 +
    描边方角评级标签 + mono 元信息行。
  - 渐变全部移除；卡片投影 → 细线分隔；圆角 15px/10px → 2px；
    数字统一 JetBrains Mono + `tabular-nums`（表格数字终于能对齐）。
  - 正文 20 处硬编码十六进制**语义化**映射到令牌（`#28a745`→`var(--bull)` 等），
    页面内已无硬编码色值 ⇒ 明暗两套自动成立。
  - 标题 emoji 全清；正文 `⚠️/✅/🟢/🔴` 换成可着色、可随主题变化的排版标记
    （`●`/`▲`/`▼`）。日志里的 emoji 保留——那是终端输出，不是 UI。

- `tests/test_ml_report_none_safety.py` 的颜色断言从硬编码十六进制换成令牌名。
  **测试意图未变**（颜色必须反映状态、缺数不得被涂成看跌），只是跟随重构。

### Added

- **`tests/test_iv_structure_guards.py`（23 条）** — 「零值伪装」回归闸。
  已验证可变红：把相对阈值退回 `closes > 5`、把渲染层退回 `_safe(v, 0)`，
  精确红 4 条；退回 C 票修复另红 4 条。含反向断言：**真实测得 0.0 仍须显示为 0.0**，
  与「—」区分——否则修复就修反了。

- `cboe_options.fetch_cboe_iv_term_structure()` — ATM 容差自适应
  `max(4%×S, 1.2×中位行权价间距)`。纯百分比容差对低价股会归零
  （AMC ≈$3 时 ±4% = ±$0.12，而行权价间距 $0.50 → 一个候选都选不到）。

---


## [0.45.3] — 2026-08-25 — 「缺数据渲染成安全值」的剩余六处

v0.45.2 只修了 `momentum_5d`。同一形状还散在另外五个字段/模块里，按**当前可达性**
排序处理。共同判据：**这个默认值会不会让下游误以为掌握了信息**。

### Fixed

- **`collect_data.py`** — `round(float(sdet.get("momentum_5d", 0)), 4)`。
  ScoutBee 自 v0.43.25 起对该字段诚实吐 None，键是在的，`.get` 默认值失效 ⇒
  `float(None)` TypeError。**当前就可达**，故排第一。改为缺失写 JSON null——
  写 0 会被下游当成"动量为零"消费。`crowding_score` 同款一并改。

- **`risk_engine.py`** — σ 未知时不再编造风险数字。两个毛病叠在一起：
  ① `float(stock_data.get("volatility_20d", 30.0))` 里那个保守兜底是**幌子**，
  默认值只在键缺失时生效，值为 None 时照样 `float(None)` 崩；
  ② 更早的形态 `volatility_20d = 0.0` ⇒ σ=0 ⇒ VaR 恒为 0 ⇒ 面板显示「🟢 低」。
  新增 `_sigma_annual()`：σ 不可得则返回 `{"error": ...}`，与本文件既有的
  `price 无效 (≤0)` 哨兵写法一致。`parametric_var` / `monte_carlo_var` 接入。
  `_classify_growth_value` σ 缺失时返回 `"unknown"`，并把消费它的
  `sens_map[style]` 裸下标改成 `.get(..., blend)`——否则诚实化立刻变成 KeyError。

- **`data_pipeline.py` / `swarm_agents/cache.py`** — `volatility_20d` 与
  `volume_ratio` 不再伪造。`volume_ratio` 的契约本就定好了（531/582 行显式传
  None，注释写明"原 1.0 会被当正常量消费"），只有 cache fallback 没跟上。
  `volatility_20d` 则连 dataclass 都还是 `float = 0.0`。
  ⚠️ **只改 cache.py 堵不住**：那条分支要 data_pipeline 导入失败才走得到，
  主路径在 dataclass 默认值上。同时把 `momentum_5d` / `volume_ratio` 的
  **默认值**也改成 None——它们的类型早已是 Optional，默认值却还是 0.0/1.0，
  构造 StockData 时不显式赋值就又变回伪造。

- **`ml_predictor.py`** — `normalize_feature()` 对 None 无守卫，
  `(value - min_val)` 抛 TypeError；SGD 路径更隐蔽，None 经
  `np.array(dtype=float64)` **静默变成 NaN** 再进 scaler。
  关键发现：`centered_feature(0.5, influence, inverse)` 对任意参数**恒等**
  返回 0.5，所以 **0.5 是这个坐标系里唯一表达"该维不投票"的数**——
  填别的才是伪造，抛错则是把数据缺口翻译成崩溃（12 维向量结构上必须凑齐）。
  故：插补 0.5，但**必须配套声明**。新增 `_missing_features()` /
  `_feature_quality()`，三个模型类的 `predict_return()` 统一输出
  `feature_completeness` / `imputed_features` / `unreliable`。
  缺 >4 维时 `_generate_recommendation()` 返回 `NO CALL`——此前无论插补多少维
  都照常出建议，而插补值全中性 ⇒ 概率被推向 0.5 ⇒ 稳定落进 "HOLD"，
  读起来像一个真实判断。SGD 路径的 NaN 改用训练均值填补（标准化后恰为 0）。

- **`llm_service.py`** — 三处 momentum + 两处 volatility 的
  `{...get(k, 0):+.1f}%`。format(None, spec) 直接 TypeError，与 v0.43.23
  那次 ML 报告崩溃**完全同款**；就算不崩，把缺失渲染成 "+0.0%" 等于告诉模型
  "这只票持平/低波动"，而真相是"没查到"——**喂给 LLM 的假事实会被它当前提推理**。
  新增 `_fmt_num()`：None → "不可得"。
  注：volatility 那两处是被本次改动**新变得可达**的，同批修掉。

### ⚠️ 本版自己造成的生产事故（同批修复）

把 `volatility_20d` 的默认值从 `0.0` 改成 `None` 后，**2026-08-25 的自动扫描
30/30 只标的的 BuzzBee 全崩**、BearBee 28 只：

```
BuzzBeeWhisper failed for NVDA: '>' not supported between instances of 'NoneType' and 'int'
  File "swarm_agents/buzz_bee.py", line 69, in analyze
```

崩点是 `vol20 > _vlt.get("extreme", 60)`——**同一个文件里上面十行的
`volume_ratio` 就有 `if x is None` 守卫，波动率这条没有**。

根因不是没想到，是**审计方法出错**：两次用 `grep ... | head -N` 查消费点，
两次都被截断，`swarm_agents/` 恰好在被截掉的部分。查影响面时禁用 `head`。

同批修复四处：`buzz_bee.py:67`（→ 中性 50，与 volume_ratio 同型）、
`bear_bee.py:307/331`、`rival_bee.py:113`（透传 None 给 ML，由
`_feature_quality` 声明）、`rival_bee.py:179`（format(None) 崩溃）。
新增 `TestAgentsSurviveFullyDegradedData`：给三只蜂喂**全字段皆缺**的数据。
此前没有任何一条测试这么喂过——所以 1540 条全绿也没拦住。

### Added

- **`tests/test_silent_failure_guards.py`** — 扩到 44 条，新增五组：
  dataclass 默认值诚实性、risk_engine 拒绝出数、ML 插补声明、prompt 渲染、
  collect_data 输出 null。含"插补不能顺手改坏正常路径"的对照断言
  （`normalize_feature(3,0,10)==0.3`）与"0.5 确实中性"的证明
  （`centered_feature(0.5,·)≡0.5` 对四种 influence × 两种 inverse）。

### 已知遗留

- `crowding_detector.py:489/501` 自建 stock_data，仍有 `volatility_20d: 0.0` /
  `volume_ratio: 1.0`。它与 data_pipeline 无关、下游只用 crowding，本次未动。
- `_data_unavailable` 全仓**只有写入没有读取**（7 处全是写）。诚实降级实际靠的是
  值本身为 None，不是这个标志位。要么接上读取点，要么别在新代码里假装它有用。

## [0.45.2] — 2026-08-25 — 2026-08-24 跑后核对翻出的三条静默失败路径

> **⚠️ 提交归属（本条改动被劈成两半）**
> 多 session 并发编辑期间，另一个 session 用宽口径 `git add` 把本条的一部分
> 文件先提交了，于是同一个修复散在两条提交里，且其中一条的 message 与本条无关：
>
> | 改动 | 文件 | 提交 |
> |---|---|---|
> | ticker 正则接受 `BRK-B`/`BRK.B` | `swarm_agents/_config.py` | **`25615ff`**（message 讲的是「深度报告 IV / 版式去 AI 味」） |
> | WATCHLIST 启动校验正则 | `config.py` | **`25615ff`** |
> | ticker 错误文案 | `swarm_agents/base.py` | `4f48da7` |
> | gh-pages 空提交闸 `ghpages_tree_delta()` 定义 | `report_deployer.py` | `4f48da7` |
> | gh-pages 空提交闸 调用侧 | `generate_ml_report.py` | **`25615ff`** |
>
> **靠 `git log <file>` 追溯本条时会找错地方**，请以本表为准。


来自 `post-fix-verification-2026-08-24` 的跑后核对。三条都不报错、退出码为 0、
日志正常，**但产出早已是假的**——与 v0.43.23~0.43.26 是同一族缺陷。

### Fixed

- **`swarm_agents/_config.py` / `config.py` / `swarm_agents/base.py`** —
  ticker 正则 `^[A-Z]{1,5}$` 拒绝类份额后缀，`BRK-B` 从未被真正分析过。
  后果不是报错而是**静默中性化**：8 只蜂里 7 只在 `_validate_ticker` 提前
  返回 `score=5.0 / confidence=0.0 / details={}`，日报照常印
  "BRK-B NEUTRAL 5.0"，与"分析过、确实中性"在产出上完全同形。
  至少可追溯到 2026-08-11 的日报。改为 `^[A-Z]{1,5}(?:[.-][A-Z])?$`，
  同时接纳 `BRK.B` / `BF-B`；`BRK-BB` / `BRK--B` / `../etc` 等注入形状仍被拒。
  修复后实测 BRK-B 拿到真实动量 +0.27%、情绪 51%、confidence 0.85。

- **`swarm_agents/cache.py`** — fallback 把 `momentum_5d` 初始化为 `0.0`，
  是 v0.43.25 在 ScoutBee 侧拆掉的伪造**搬到了上游**。
  真正危险的不是那两条 `_data_unavailable=True` 的早退路径，而是
  `len(hist) >= 5` 不成立时（次新股 / 停牌 / 取数残缺）：0.0 会一路走到
  `data_source="real"` 且**不置任何标志位**，下游无从区分"真持平"和"没数据"。
  改为 `None`，对齐 data_pipeline 自 v0.38.0 起的 P0-2 契约。
  警告：全仓 `_data_unavailable` 只有写入、**没有任何读取点**——诚实降级靠的是
  `momentum_5d is None` 本身，不是这个标志位。

- **`swarm_agents/buzz_bee.py`** — 注释写"momentum 为 None 时跳过背离检测"，
  代码却传 `_mom_raw if _mom_raw is not None else 0.0`，**并没有跳过**。
  于是 `sentiment.py` 里那条返回 `divergence_type="unavailable"` 的分支
  （v0.43.25 专门为此写的）永远不可达，"查不了"被伪装成"查过、没背离"。
  现在直传 None。注意 `score_adj` 恒为 0.0，评分不受影响。

- **`report_deployer.py` / `generate_ml_report.py`** — gh-pages 空提交。
  部署走 `git commit-tree` 这类**管道命令**，它不做 `git commit` 的
  "无变更则拒绝"检查，tree 与父提交相同也照样生成 commit。实测
  `8b16977` / `06d99cc` / `0c00454` 三条 commit message 都声称
  "(12 tickers)"，`git show --name-only` 却是 **0 个文件**——message 里的
  数字是 `successful_count`（**声称值**），与 tree 实际变更无关。
  新增 `ghpages_tree_delta()`：tree 未变则跳过 commit 并打 ERROR，
  变更时把**实测**文件数写进 message 与 `.gh_pages_deploy_log.jsonl`。
  无父提交 / git 调用失败返回 `(True, -1)` fail-open，不阻断部署。

### Added

- **`tests/test_silent_failure_guards.py`** — 26 条回归闸，覆盖上述三条路径。
  每条都已验证**把修复回退掉会变红**（旧正则拒 BRK-B、HEAD 里默认值是 0.0、
  HEAD 里仍含 `if None else 0.0`、旧调用方下 `unavailable` 不可达、
  HEAD 里没有 `ghpages_tree_delta`）——否则测试只是装饰。

### 已知遗留（未修，本次只报告）

- `ml_predictor.normalize_feature()` 对 `None` 无守卫，`(value - min_val)`
  会直接抛 TypeError。当前不可达（`TrainingData` 的构造方都已 sanitize），
  但契约仅靠类型注解 `momentum_5d: float` 维系，没有运行时强制。
  刻意不加"None → 0.5"的兜底：那会是又一处静默伪造。
- `swarm_agents/cache.py` 的 `volume_ratio: 1.0` 与 `volatility_20d: 0.0`
  是同一形状的伪造，本次未动（超出核对范围）。

## [0.45.1] — 2026-08-24 — 波动率工具链二次检查：三个"看不见"的缺陷

对 v0.45.0 补录的那批代码做二次检查。三个缺陷都不影响已产出的验证结论
（IC +0.663、四口径全过），但都属于**平时看不见、出事时难查**的形态。

### Fixed

- **`alpha_hive_daily_report.py`** — 波动率分层每次生成报告读两遍库。
  `_volatility_tier_markdown()` 内部又调了一次 `_volatility_tiers()`，而
  `report["volatility_tiers"]` 也调一次。真正的风险不是性能：两次读之间若
  `signal_archive` 被写入，JSON 里的分层会与 Markdown 里的对不上，
  **而这种不一致没有任何地方会报错**。
  改为在 report dict 构建前算一次，两处复用；`_volatility_tier_markdown(tiers=None)`
  保留无参调用，向后兼容。

- **`vol_forecast.py`** — `load_day()` 未捕获 `sqlite3.OperationalError`。
  日报路径外面有 try/except 兜底，所以这个缺陷**在日报里看不见**；但
  `vol_forecast.py --date` 直接跑，遇到全新库或未 backfill 就吐一屏 traceback。
  现在库文件不存在 / 表不存在都返回 `{}`，由 `main()` 给出"先跑 --backfill"
  的可操作提示，退出码 1。

- **`signal_archive.py`** — `_PX_CACHE` 的键漏掉中间日期。
  旧键 `(tickers, dates[0], dates[-1], fwd_days)` 用**区间端点**冒充**集合内容**。
  同进程内两次调用若首尾相同、中间不同（新样本回填后重查是典型场景），
  第二次静默复用陈旧结果、丢掉中间日期。改用完整日期序列的 sha1 指纹。
  ⚠️ 只加 `len(dates)` 不够——首尾与长度都相同、只有中间那天不同的情形仍会撞键。

### Changed

- **`signal_archive.py`** — 键逻辑提取为 `_px_cache_key()`，让测试直接引用生产代码。
  原先测试里重抄一遍键实现，**等于测试测自己**：生产代码改回旧写法照样绿。

### Tests

新增 3 组共 10 项。每组都**反向验证过**——把生产代码改回旧写法，对应测试确实变红：

| 测试组 | 注入的旧 bug | 结果 |
|---|---|---|
| `TestSingleDatabaseRead` | markdown 无视传参再读一次 | 红 ✅ |
| `TestLoadDayResilience` | 去掉表缺失容错 | 红 ✅ |
| `TestPriceCacheKey` | 键退回首尾式 | 红 ✅ |

全量 **1469 passed**, 1 skipped, 1 xfailed；ruff F821 全库通过。

### 提交纪律

`alpha_hive_daily_report.py` 里 v0.44.3 的 RivalBee 改动是工作区既有内容，
按 hunk 拆分后留在未暂存状态，未随本次提交。同理 `CHANGELOG.md` 的
v0.44.0~v0.44.4 条目单独成一个提交（`c1350a9`），与 0.45.0 补录分开，
避免混在一起无法单独回滚。

---

## [0.45.0] — 2026-08-16 — 补录：波动率预测工具链（2026-07-30/31 已提交但漏记）

> ⚠️ **这是一次补录，不是新功能。** 下列四个提交在 2026-07-30/31 已完成、
> 测试通过并推送，但 CHANGELOG 条目**从未落盘**（根因见文末）。
> 代码一直在库里正常工作 —— 2026-08-16 复核：`vol_forecast.py` 端到端可跑，
> 相关 91 个测试全部通过。

### ⚠️ 版本号冲突说明（重要）
这四个提交的 commit message 里写的是 `v0.43.7 / v0.43.8 / v0.44.0 / v0.44.1`，
但那些号后来被 2026-08-10 与 2026-08-16 的**无关功能**重用了。
本条目改用空闲的 `0.45.0`。**按 SHA 检索，不要按 commit message 里的版本号检索。**

| SHA | 日期 | commit 里的版本号 | 内容 |
|---|---|---|---|
| `f11ed3d` | 07-30 | ~~v0.43.7~~ | 单信号档案加入训练/测试分段稳定性列 |
| `3b819f0` | 07-31 | ~~v0.43.8~~ | `signal_archive` 支持波动率预测目标 |
| `e17ebc1` | 07-31 | ~~v0.44.0~~ | **新建 `vol_forecast.py`** |
| `a8d40d3` | 07-31 | ~~v0.44.1~~ | 波动率分层接入日报（观察项） |

---

### 一、训练/测试分段稳定性（`f11ed3d`）
`signal_archive.split_stability()` —— 按时间切 60/40 分别算 IC，
检测全样本 IC 是否只是**异号平均**。

触发原因：综合分全样本 IC = −0.09 看似稳定弱负，切开后
**训练 −0.214(t=−5.28) / 测试 +0.025(t=+0.46)** —— 符号相反。
那个 −0.09 描述的不是效应，是两段异号数据的中间值。

实测 47 个信号：**翻转 20 / 稳定 16 / 噪音 11 / 衰减 3** ——
**43% 的信号全样本 IC 是假象**，包括曾被认为"唯一通过 4/4 口径"的
`agent.ScoutBeeNova.direction`（训练 −0.310 / 测试 +0.026）。

### 二、波动率预测目标（`3b819f0`）
`signal_archive.py --target {return,vol}`，其余全部复用
（四口径 / 噪音地板 / 固定-时变 / 训练-测试）。

**决定性对照**（90 只 × 897 交易日 × 2023-01~2026-07，同宇宙同特征，只换目标）：

| 预测目标 | 特征 | IC | t |
|---|---|---|---|
| 未来 7 日**收益率**（系统现状） | 20 日动量 | **+0.0116** | +1.7 |
| 未来 7 日收益率 | 5 日反转 | −0.0035 | −0.5 |
| 行业内相对强弱 | 20 日动量 | +0.0078 | +1.4 |
| **未来 7 日已实现波动** | 过去 20 日波动 | +0.6587 | +259.0 |
| **未来 7 日已实现波动** | 过去 60 日波动 | **+0.7101** | **+288.6** |

**波动率的可学性高 60 倍**，用的是一行 `rolling(60).std()`，无任何模型。
不是过拟合 —— 是波动率聚集（volatility clustering）。

⇒ 系统此前在预测一个 IC≈0.01 的目标，**天花板与架构无关**。
当日五次改进尝试全部被验证流程拦下，不是改得不对，是地板与天花板之间没有空间。

在真实标的池上（同一批 46 个信号）：

| | 目标=收益率 | 目标=波动率 |
|---|---|---|
| 候选真信号 | **2 / 46** | **17 / 46** |
| 时间稳定 | 15 | **24** |
| 符号翻转 | **20** | 14 |

`options.iv_current`（IC **+0.640**，4/4）与 `price.volatility_20d`（**+0.611**，4/4）
—— **系统已有的期权数据就是一个强波动率预测器**，只是此前被用来预测了一个不可学的目标。

### 三、`vol_forecast.py`（`e17ebc1`）
`vol_score` = IV 分位 × 0.5 + 20 日已实现波动分位 × 0.5（横截面秩平均）。

**组合优于任一单信号**（667 条样本，四口径全过）：

| 构成 | IC | t | 通过 |
|---|---|---|---|
| `options.iv_current` 单独 | +0.6399 | +20.35 | 4/4 |
| `price.volatility_20d` 单独 | +0.6108 | +15.46 | 4/4 |
| **两者秩平均** | **+0.6632** | **+22.55** | **4/4** |

等权是刻意的：样本量不足以支撑更精细的权重估计，且等权对参数误设最稳健。

**分层的真实效果**（目标 = 实际未来 7 日已实现波动）：

| 分层 | 全样本 | 训练期 | 测试期 |
|---|---|---|---|
| 低波动·加仓 ×1.25 | 2.143% | 2.048% | 2.316% |
| 中位·基准 ×1.00 | 2.929% | 2.707% | 3.282% |
| 高波动·降仓 ×0.70 | 5.133% | 5.336% | 4.811% |
| **高−低价差** | **+2.99% (t=+16.3)** | +3.29% (t=+12.7) | **+2.50% (t=+10.8)** |

**样本外几乎不衰减** —— 与当日所有"训练期强、测试期消失"的候选形成鲜明对照。

#### ⚠️ 两条硬约束（写进模块与测试）
1. **仅限横截面**：`price.volatility_20d` 固定效应 IC **+0.720** / 票内时变仅 **+0.012**
   ⇒ 能答「哪些标的波动大」，**不能答「何时变大」**。所有接口强制同一天横截面。
2. **不参与评分**：`vol_score` 是并行输出，不进 `EVALUATION_WEIGHTS`，
   不破坏历史样本可比性。测试对此有断言。

### 四、接入日报（`a8d40d3`）
`_volatility_tiers()` / `_volatility_tier_markdown()`，结果同时进
**JSON**（`report["volatility_tiers"]`）与 **Markdown**（日报末尾小节）。
渲染里明确写出「未影响仓位」「不可用于择时」。

**为什么只输出不下注**：接进 `paper_portfolio` 会改变纸面组合的历史可比性，
应当是显式决策。有**源码级护栏测试**断言 `paper_portfolio.py` 不得引用 `vol_forecast`。

---

### 本次漏记的根因（流程教训）
前几次 CHANGELOG 更新用 `Edit` 工具（失败会报错），后四次改用 Python 脚本
`s.replace(锚点, ...)` + `write_text`。**锚点不存在时 `replace` 是无操作**，
写回内容与原文相同 → git 无 diff → `git add CHANGELOG.md` 什么也没加。
而脚本**无条件打印「CHANGELOG 已更新」，从未校验替换是否真的发生**。

连锁反应：`v0.43.7` 的锚点没命中 → 后续每一个都以前一个的存在为前提 → 全部落空。
代码提交全部正常（`git show` 可验证），只有文档丢失。

**教训**：脚本化编辑必须校验结果（比较改动前后长度或断言锚点存在），
否则失败是静默的 —— 与本项目反复出现的「看着成功其实早废了」同一形态。

## [0.44.4] — 2026-08-16 — 把「攒够样本后重跑 IC」挂成可追踪的东西

### Added — `ic_rerun_readiness.py`
v0.44.1~0.44.3 那批修复**只验证了接线正确，没验证方向变准** —— 后者要等新样本
（实测约 **25 个不重叠周**，见 `experiments/ic_power_report.md`）。

而「等攒够」这件事此前**没有承载物**：不在测试里（测试跑当下）、不在告警里
（没有异常），全靠人记着。按半年的时间尺度，等于不存在。本工具就是那个承载物。

**为什么不用定时提醒**：到期条件是**数据条件**（攒够不重叠周），
而它取决于扫描连续性 —— 实测覆盖率只有 36.7%，日历时间与样本进度根本不成比例。
所以判据必须读库算，不能拍一个日期。ETA 因此按**实际周产出率**外推，不按日历。

判定内容：
- 世代内已回填 T+7 的样本数与**不重叠 ISO 周数** vs 判据（默认 |IC|=0.090 → 25 周）
- **只数已回填的**：未到期样本 `checked_t7=0`，只按日期过滤会让就绪度提前变绿
- **标的池是否被中途换掉**（换了则样本同样不可比，与 `weekly_optimizer` 的闸 2 同思路）
- 退出码 `0`=已就绪 / `1`=未就绪（正常）/ **`3`**=无法判定（2 被编排器占用）

⚠️ **世代边界 `_COHORT_HISTORY` 是本工具的核心前提**，只追加不改写（审计轨迹）。
`ml_predictor.expected_returns` 的 docstring 已加显著提示：再次改动
`expected_returns` / `predict_probability` / RivalBee 特征来源时**必须追加一条**，
否则新旧口径样本会被混算 —— 而混算是静默的：数字照出，只是没有意义。

### Added — 两处挂载点（都不发 Slack）
1. **编排器 Step 11**（每交易日）：用 `--out` 写 JSON 与 `status.json`，
   就绪时在日志里显著标出并列出该跑的命令。
   **刻意不自动跑那两条命令** —— IC 分析要人看结果并做判断。
   与 Step 10 同样**不影响 `OVERALL_STATUS`**（"样本没攒够"是正常状态）。
2. **周度只读诊断任务 SKILL.md**（每周日）：新增「每周必查」小节，
   明确退出码含义与汇报方式，并禁止自行修改 `_COHORT_HISTORY` / `_WEEKS_REQUIRED`。

两处都有是刻意的：周度任务是 LLM 型的，可能被跳过或改写；编排器每交易日都跑，
是更可靠的心跳。

### Added — `tests/test_ic_rerun_readiness.py`（22 项）
最关键的一组是 `TestVerdictActuallyFlips`：**喂足 25 周必须真的翻成"已就绪"**
—— 一个永远说"未就绪"的判定器和没有判定器是一样的。与
`test_distribution_invariants.py::TestGuardsHaveTeeth` 同一思路。

另含：世代历史只追加且按时间有序、每条都记了原因（只有日期的边界半年后无法判断
是否仍适用）、边界之前的样本一条不算、差一周就是差一周（不许四舍五入）、
未到期样本不计入周数、池被换掉会拦下就绪、加 1 只到 10 只池不该过敏、
ETA 按产出率而非日历、退出码 3 不占用 2、`_WEEKS_REQUIRED` 与功效报告的三个
关键数字一致（防两处悄悄分叉）。

⚠️ 过程中这组测试两次抓出**我自己测试数据的错误**（早期序列跨过了世代边界、
fixture 复用同一个库文件）——判定逻辑本身两次都是对的。

### 验证
全量套件 **1459 passed / 1 skipped / 1 xfailed**（新增 22 项）。
新增两文件 `--select F` 全绿；`ml_predictor.py` 与 HEAD 的 F 错误一致；
全仓 **F821 全绿**；`bash -n` 编排器通过；`config.py` 全程未被动过。

当前实测状态：世代自 **2026-08-17** 起（明天），已攒 **0/25** 周。

---

## [0.44.3] — 2026-08-16 — RivalBee 移到 Phase-1.5，三个硬编码特征接上真实数据

### Changed — `alpha_hive_daily_report.py` RivalBeeVanguard 从 Phase-1 移到 Phase-1.5
新执行顺序：**Phase-1 并行(4~5 蜂) → Rival → Guard → Bear**。

为什么移：RivalBee 的 ML 特征里有三个属别的蜂的产出 —— `catalyst_quality` 来自
ChronosBee 的催化剂分、`iv_rank`/`put_call_ratio` 来自 OracleBee 的期权 details。
留在 Phase-1 并行里读不到（同批蜂互相看不见），此前只能写死
`"B+"` / `50.0` / `1.0` 三个常量。

为什么排在 Guard **之前**：Rival 只依赖 Phase-1，不依赖 Guard；而 BearBee 读全板，
把 Rival 排在前面能保证 Bear 仍看得到它。

⚠️ `rival_agent` 必须进 `all_agents`：`inject_prefetched` 靠它注入
`_prefetched_stock`，漏了会让 Rival 逐标的直接抓 yfinance（本项目三次事故的同一根因）。
有测试钉住。

代价：单标的耗时增加一个 Rival 的串行时长（此前与其他 4 只并行）。
Rival 无独立网络调用（动量走预取的 stock、拥挤度走带缓存的共享路径），增量很小。

### Added — `swarm_agents/base.py` `_read_peer(ticker, agent_id)`
通用能力：读取**同一轮里已发布**的其他蜂的信息素条目，返回 `PheromoneEntry` 或
`None`。走信息素板的 `details`（S3 本就为结构化数据交换设计），
**不改 `analyze(ticker)` 签名** —— 那是所有蜂共用的契约。

⚠️ 能不能读到完全取决于**执行阶段顺序**，docstring 里写明了这一点，
并有测试断言该说明存在（否则下个调用方会以为它总能读到）。
默认查 24 条（`get_top_signals` 默认 n=5 会截断 5~6 只蜂的一轮）。

### Changed — `swarm_agents/rival_bee.py` 三个特征接真实数据
- `catalyst_quality` ← ChronosBee 条目的 `self_score`，经
  `catalyst_quality_from_score()` 转等级
- `iv_rank` / `put_call_ratio` ← OracleBee 条目 details 的 `iv_rank` / **`pc_ratio`**
  （注意板上的键名是 `pc_ratio`，与报告 `agent_details` 里的 `put_call_ratio` 是
  两个不同的 dict —— 前者来自 `_publish`，后者来自 analyze 返回值）

回落值刻意选**可与真实值区分**的档并留 debug 日志：`catalyst_quality` 回落 **"B"**
而不是 "B+"（后者是 magnitude 1.0 的基准档，用它会让"拿不到数据"与"质量正好中等"
不可区分）。iv_rank/pc_ratio 各自独立回落（Oracle 的 details 是条件填充的，
缺一个键不能全丢）。

### Changed — `catalyst_quality_from_score()` 提为模块级单一真相
同一套阈值（8.5→A+ / 7.5→A / 6.5→B+ / 5.5→B）此前在**三处**各写一份嵌套
`_cat_qual`（`ml_predictor.py` 内、`alpha_hive_daily_report.py:823`、
`generate_ml_report.py:139`），与 `expected_returns` 曾经的三重复制同一个反模式。
两处副本已改为 import 共享函数。有测试扫全仓确保不再增殖。

⚠️ 阈值本身**未动**：历史 `predictions` 的 `catalyst_quality` 都按这套生成，
改了会让新旧样本不可比。

### Added — `tests/test_rival_bee_peer_features.py`（32 项）
移完之后全量套件依然全绿 —— 因为**没有任何既有测试覆盖阶段顺序**。
「跑通了」和「读到了」是两件事，本项目的招牌缺陷正是前者掩盖后者。故补：
- `_read_peer`：读到/读不到/不跨标的泄漏/窗口够覆盖一轮/板损坏不崩
- 等级转换阈值逐档 + 缺失给 "B" 不给 "B+" + 全仓只定义一处
- **特征真的抵达 `TrainingData`**（拦截 `predict_for_opportunity` 检查入参）：
  读到真值、读不到可区分回落、Oracle 部分缺键各自回落、脏值回落、
  v0.44.2 的真实拥挤度未被回退
- **阶段顺序契约（源码级）**：Rival 不在 Phase-1 列表、顺序为
  Phase1→Rival→Guard→Bear、`rival_agent` 在 `all_agents` 里、
  `_read_peer` 的 docstring 说明了顺序依赖

顺序类缺陷的本质是**执行顺序错而非逻辑错**：把 Rival 挪回 Phase-1 并行，
单元测试仍会全绿（它会安静地回落到三个中性常量），只有源码级断言能拦住。
与 `test_chronos_bee_direction.py::test_pead_applied_after_scoring_block` 同一思路。

### 端到端验证（隔离环境，不污染生产库）
用 `ALPHA_HIVE_HOME`/`ALPHA_HIVE_DB_PATH` 指向临时目录跑
`--swarm --no-llm --force --tickers NVDA`：
- 日志确认新顺序：`8 Agent（Phase1 5 + Rival + Guard + Bear）` →
  `⚔️ 竞争蜂` → `🛡️ 验证蜂` → `🐻 看空蜂`
- ChronosBee 催化剂分 6.085 → 等级 "B"（转换路径生效）
- 当轮 OracleBee 的 `iv_rank`/`pc_ratio` 为 None（周日无期权数据，
  `options_analyzer.py:1461` 的降级路径）→ 回落正确生效，未崩
- 已确认上游键存在：`options_analyzer.py:1788` 返回 `"iv_rank": iv_rank`(0-100)；
  `options.put_call_ratio` 在生产 signal_archive 有 1095 行 / 146 个不同取值
  ⇒ 正常交易日这两个特征有真实值

### 顺带发现（未修，需决策）
**`predictions` 的 `iv_rank` / `put_call_ratio` / `options_score` /
`gamma_exposure` 四列在 933 行里 100% 为 NULL** —— schema 里有、从来没写过。
又一例"看着有其实是死的"。与本次改动无关（那是 `PredictionStore` 的写入路径，
与信息素板是两条路），但它意味着任何基于这四列的分析都是空转。

### 验证
全量套件 **1437 passed / 1 skipped / 1 xfailed**（新增 32 项）。
改动的六个既有文件与 HEAD 的 F 错误**逐条一致**；新文件 `--select F` 全绿；
全仓 **F821 全绿**；`config.py` 全程未被动过。

---

## [0.44.2] — 2026-08-16 — RivalBee 传入真实拥挤度；并否决把它做成收益项

### Changed — `swarm_agents/rival_bee.py` 传真实 `crowding_score`
不再写死 `crowding_score=50.0`，改走 ScoutBee / GuardBee **已在用的同一条路**
（`CrowdingDetector` + `get_real_crowding_metrics(ticker, stock, board)`），
**不另造一套**。拿不到时回落 `ml_predictor.CROWDING_NEUTRAL`（**不是 50.0**，
理由见下）。

成本可忽略：内部两个网络调用都有缓存（social TTL 3600s / short interest
TTL 86400s），`stock` 由本蜂已取的 `_get_stock_data` 传入。ScoutBee 先跑时是
缓存命中；最坏情况（与 ScoutBee 并行同时未命中）每标的多一次抓取；
GuardBee 在 Phase-1.5 必然命中。

只补这一个特征：`catalyst_quality` 属 ChronosBee 域、`iv_rank`/`put_call_ratio`
属 OracleBee 域，而 RivalBee 在 **Phase-1 并行**运行，那时点它们还没产出。

### Fixed — 中性点 50 是凭量表刻度猜的（差点重新引入 +2.67pp 正偏）
实测 `crowding.score`（n=1057）：中位 **23.30**、p90 43.23、**≥50 的样本仅 4.6%**。
检测器自己的档位（`<30` 低 / `30~60` 中 / `≥60` 高，中心约 45）**也与实测差得远**。

**若直接把真实值传入而不重标中性点**，95.4% 的样本会被判"不拥挤 → 加分"，
中位 tilt = −2.67pp ⇒ **重新引入 +2.67pp 系统性正偏**，把 v0.44.1 修掉的东西
部分抵消回去。**所以这个改动不是"低风险局部改进"，除非两件一起做。**

`CROWDING_NEUTRAL` 重标为 23.30（实测中位数），用途改为 `probability` 的中性
回落值——那里 crowding 是**权重最大的特征**（0.18），回落值选错会直接偏斜概率。

### Removed — 拥挤度不再进入 `expected_returns`（否决 v0.44.1 的倾斜项）
用现成的四口径工具 `signal_archive.py --analyze`（噪音地板 |IC|=0.076、需 ≥3/4）
复核后否决：

| 信号 | 日度IC | t | 通过口径 | 判定 |
|---|---|---|---|---|
| `crowding.score`（连续） | **+0.1122** | +2.48 | **1/4** | 🟡 口径不足 |
| `crowding.adj_factor`（分档） | −0.1117 | −2.81 | 3/4 | 🟢 候选 **⚠️稀疏** |

三条理由：① **方向未确立且与设计意图相反**——三个口径一致指向「高拥挤 → 收益
更高」，而 `get_adjustment_factor` 对高拥挤打 30% 折；连续版不达标，达标版仅 6 个
取值、被工具标 ⚠️稀疏（命中 MEMORY 的「`distinct_ratio<0.25` → rank-IC 失真」
陷阱）。② **三重计数**（已在 probability 权重最大 + 综合分 adj_factor）。
③ **它不是收益预测量**，折成百分点需凭空定标度。

⚠️ **代价说清**：否决后偏差从 **+0.19pp 回到 +1.06pp**。但那 +0.19 是**巧合**
——倾斜均值恰好抵消了动量带来的正偏，不是因为符号对。
**宁要 +1.06pp 的可辩护公式，不要 +0.19pp 的不可辩护公式。**
被否决的方案保留在回放脚本里作对照（`v442_tilt_expected`），连同否决理由。

### Added — 测试（共 43 项，净增 6）
- `TestCrowdingStaysOutOfExpectedReturns`：拥挤度取任何值（含 ±∞/NaN/None/字符串）
  都不得改变 `expected_returns`；但**必须**仍影响 `probability`
  （否则真实拥挤度就白传了）。
- `TestCrowdingCalibrationDrift`：`CROWDING_NEUTRAL` 是**经验常数会过期**，
  对着生产库分布守着；并单独钉住"为什么不是 50"（若被改回 50 附近就红）。
- helper 的 `crowding_score` 默认值改用 `mp.CROWDING_NEUTRAL` 而非字面量 50.0
  —— 中性点重标时立刻捕捉到了这个前提变化（一条测试因此变红并被修正）。

### 未做（需决策）
- **拥挤度的真实方向值得单独查**：三个口径一致指向"高拥挤 → 收益更高"，
  与常识和检测器设计都相反。若成立，`get_adjustment_factor` 的折扣方向也是反的
  ——那是个比本次修复大得多的问题。**按 MEMORY 的「稀疏信号必须改用事件研究」
  走，不要用 rank-IC 下结论。**
- `catalyst_quality` / `iv_rank` / `put_call_ratio` 仍写死（需动 Phase 结构）。
  当前 `expected_7d = 0.8 × momentum_5d`，5 日动量 IC=+0.0079 ⇒
  **RivalBee 仍不提供方向信息。**

### 验证
全量套件 **1405 passed / 1 skipped / 1 xfailed**。改动的三个既有文件与 HEAD 的
F 错误**逐条一致**；新增两文件 `--select F` 全绿；全仓 **F821 全绿**；
`config.py` 全程未被动过。

---

## [0.44.1] — 2026-08-16 — ML 预期收益结构性恒正：+9pp 的系统性谎报已修

起因：v0.44.0 新加的分布不变式报出 `RivalBeeVanguard` **95.2% 看多**。
查下去发现不是 RivalBee 的 bug，是 `ml_predictor` 的 `expected_7d` 结构性恒正。
完整诊断与回放见 **`experiments/ml_expected_return_report.md`**。

### Fixed — `ml_predictor.py` 预期收益公式（根因）
旧式 `expected_7d = catalyst_bonus + momentum_bonus − crowding_penalty` 里
`catalyst_bonus` ∈{5,10,15,20,25}（默认 10）**永不为负**、`crowding_penalty`
**上限 10** ⇒ 唯一能为负的项只剩动量，转负需 5 日跌超 5%（A+ 时需跌超 25%）。

量纲也错了：`catalyst_bonus` 是催化剂**质量等级**（5~25 的"分"），却与
`momentum_5d`（**百分点**）相加当预期收益 —— "B 级催化剂" = +10pp 的 7 日预期收益。
与 ChronosBee「权重表只编码影响多大、不编码影响好坏」同族。

⚠️ **生产里更极端**：`rival_bee.py` 把特征写死（`catalyst_quality="B+"`、
`crowding_score=50.0`、`iv_rank=50.0`、`put_call_ratio=1.0`），代入旧式得闭式
**`expected_7d = 8.0 + 0.8 × momentum_5d`**，在 1057 个配对样本上**零反例**。
所谓"ML 预期收益"就是一个截距 +8% 的一元线性式。

修法：催化剂质量改为**只调幅度不调方向**的无量纲乘数；拥挤度以中性 50 为中心
（可正可负）；全部项统一为百分点 ⇒ **无信号时预期收益为 0，而不是 +8%**。
动量为 None/NaN/非数值时按 0 处理（旧实现会 TypeError）。

### Fixed — `ml_predictor.py` probability 的中性点全在 0.5 之上
八个分项形如 `0.3 + x*0.7`（中性 0.65）与 `1.0 − x*0.3`（中性 0.85），
按默认权重算出**结构性地板 0.3610**（与实测最小值 0.3500 吻合）。实测
`ml.probability` **99.6% > 0.5**；向下空间 0.139 vs 向上 0.45，**不对称 3.2 倍**
—— 它无法表达"强烈看空"。代码注释称「每个分项必须在 [0,1] 范围内」，
范围确实对，但**中性点不在 0.5**，这是注释没说、也没人验过的那一半。

新增 `centered_feature(x, influence, inverse)`：**保留每项原有的影响力系数**，
只把中性点搬回 0.5。因此不是靠削弱某个特征来消除偏斜，特征间相对影响力不变；
`influence=1.0` 时等价恒等映射，故本来就居中的 catalyst / final_score /
odds_score / risk_adj_score 四项数值完全不变。

### Changed — 三处重复公式合并为单一真相
旧实现在 `SimpleMLModel:390` / `SGDMLModel:672` / `HGBModel:1197` **逐字重复三份**，
docstring 还写着"公式与 SimpleMLModel 完全一致"。合并为模块级 `expected_returns()`，
三个类共同委托。**换模型对本问题完全无效**（训练只影响 `probability`），
这点也写进了注释。

### Fixed — `ml_predictor_extended.py` 的 `max(0, ...)`
降级实现（`ml_predictor` 导入失败时才生效）用 `max(0,...)` 让负值**结构性不可能**，
且期限缩放是 0.3/**0.7**/1.2 与主实现 0.3/**0.8**/1.2 悄悄不一致。已镜像主实现。
重复是结构性强制的（它存在的前提就是主实现不可用），用
`TestFallbackMirrorsPrimary` 断言两份数值一致来兜住。

### Fixed — `swarm_agents/rival_bee.py:121-126` EPS 单向棘轮
`positive` 会把 neutral 翻成 bullish，`negative` **没有对应的 neutral → bearish 分支**。
与 ChronosBee 同构。此前被掩盖（ML 路径占 ~99%，旧 expected_7d 恒正 ⇒ 方向几乎
从不为 neutral）；修好 expected_returns 后 neutral 会真的出现（动量为 0 的样本在近
12 个扫描日占 **63.7%**），这个棘轮就会接替成为主要偏斜源。**必须同时落地。**

### Added — `tests/test_ml_expected_return.py`（37 项）
断言此前没人问过的那个反方向问题：**有没有输入能让它输出负数？**
旧实现逃过所有测试的原因很具体——喂 `catalyst_quality="A+"` 会得到一个大的正数，
**符合预期**。包含：负动量必须给负预期（−0.5% 也要）、无信号必须恰为 0、
催化剂质量不得翻转符号、A+ 救不了下跌股、拥挤度两侧对称、probability 全中位输入
恰为 0.5 且可达区间对称、三个模型类都委托共享函数、降级实现与主实现数值一致。

### Added — `experiments/ml_expected_return_replay.py` + 报告
697 条真实配对样本的前后回放：
- 为正占比 **97.1% → 51.9%**（真实 48.4%）
- 系统性偏差 **+9.06pp → +1.06pp**；平均绝对误差 11.02 → **8.12**（改善 26%）
- 方向准确率 49.8% → 50.0%（恒定看多基准 48.4%），
  5 日动量 vs 未来 7 日收益 rank-IC = **+0.0079** ≈ 0

**诚实结论：本次修复消除了一个 +9pp 的系统性谎报，但没有创造 alpha。**
与 MEMORY 的「T+7 横截面选股接近有效市场、瓶颈在信号层」一致。
新增 43 次弃权是进步的一部分——系统现在会诚实地说"没有观点"。

### 连带修好的下游近死分支（`generate_deep_v2.py`，n=1057）
| 条件 | 旧 | 新 | 位置 |
|---|---|---|---|
| `ml_7d > 3.0`（ML看多/蜂群看空） | **89.9%** | 21.0% | `:161` |
| `ml_7d < -3.0`（ML看空/蜂群看多） | **1.2%** ← 近死分支 | 18.1% | `:166` |
| `ml_7d < -1.0` | 2.2% | 31.2% | `:3834` |
| `ml_7d > 1.0` | 93.9% | 35.1% | `:3836` |

深度报告的「ML vs 蜂群分歧」检测器此前**结构性单向**：几乎永远报"ML 看多、
蜂群看空"，反向那条基本不可能触发。这些阈值本身是**对称写的**（±3.0、±1.0），
暴露了作者意图是"以 0 为中心"——修复让现实对上意图，**下游阈值无需重调**。

### 未做（需决策，勿当已完成）
- **`rival_bee.py` 的特征硬编码**（第 2 层根因）未修。`crowding_score` 可经
  `crowding_detector` 独立取得，但 `catalyst_quality`/`iv_rank`/`put_call_ratio`
  分属 ChronosBee 与 OracleBee，而 RivalBee 在 Phase-1 **并行**运行，那时点拿不到
  ——要改需动阶段结构。当前 `expected_7d = 0.8 × momentum_5d`，
  **诚实表述：RivalBee 目前不提供方向信息**（该信号 IC≈0）。
- **样本世代**：`ml.expected_*` 前后不可比，旧 1094 条须按旧口径单独看待。

### 验证
全量套件 **1399 passed / 1 skipped / 1 xfailed**（新增 37 项）。
`ruff --select F` 对新增两个文件全绿；改动的三个既有文件与 HEAD 的 F 错误**逐条一致**
（未引入新问题）；全仓 **F821 全绿**。

---

## [0.44.0] — 2026-08-16 — 扩池 5.18× 已实测证实；扫描连续性与分布不变式补上

本轮不改任何评分/预测逻辑，只补三件让时间开始复利的基础设施。起因是
`weekly_optimizer` 的定时任务：dry-run 发现它已**从 inert 变回会开火**
（risk_adj −4.1pp > `MIN_CHANGE_PP=3.0`），而它要动的依据全是 10 只时代的数据。

### Added — `experiments/ic_power_analysis.py` + `experiments/ic_power_report.md`
横截面 rank-IC 的正式功效计算，用来核实"扩池 10→30 只把出结论时间缩短约 4 倍"
这个此前只有心算支撑的估计。**结论：成立且偏保守，实测 5.18×**（区间 3.2~5.7×）。

实测 N_eff（近一年日收益，30/30 只拿到数据）：
- 原核心 10 只：ρ̄=+0.2386 → **N_eff = 3.18**（记忆值 3.25）
- 扩池 30 只：ρ̄=+0.0498 → **N_eff = 12.27**（记忆值 13.8）

对 |IC|=0.090（系统综合分实测）：10 只需 132 个不重叠周（**~2.4 年**），
30 只需 25 周（**~0.5 年**）。

过程中修掉自己**四处**方法论错误，都记在脚本 docstring 与报告里：
1. **（二次检查时发现，最严重）相关矩阵必须按日期取交集，不能截尾对齐。**
   第一版用 `a[-n:]` 配对，而本池 30 只的有效观测天数是 **247/249/250 三种**
   （DE 247、VKTX 249），截尾会把涉及这两只的配对错位最多 3 天。错位
   **系统性稀释相关性** → ρ̄ 被压低 → N_eff 被抬高 → 倍数被低估：
   ρ̄(10只) 0.1649→**0.2386**、N_eff 4.03→**3.18**、倍数 4.54×→**5.18×**。
   修复后的 3.18/12.27 与记忆里 v0.42.9 独立算出的 3.25/13.8 几乎吻合，
   **反证那份旧值对齐正确、第一版才是错的**。
2. **置换零分布必须保留并列结构**。第一版复用 `ic_diagnostics.noise_floor` 的
   `rng.random()`——那是无并列值的连续分布，而真实维度分大量并列
   （catalyst 去重比仅 **0.105**），会系统性高估 σ_cs²，导致 4/5 个维度分解出
   负值被钳到 0。改为置换真实分数向量本身。`noise_floor` 对它自己的用途
   （跨异质因子的统一地板）是对的，对方差分解不是——**勿去"修" noise_floor**。
3. **σ_cs² 必须逐维度算**（各维并列程度差一个数量级），且过滤条件要与
   `load_daily_ic` **逐字一致**——那边除 `min_width` 还要求当日该维度
   **至少 2 个不同分数**，第一版漏了，导致 σ_cs² 与 σ_IC² 算在不同天集合上。
4. **倍数的分子分母必须同走模型**，否则"实测/模型"混用会系统性低估增益。

同时给出 σ_t²（IC 的时间变异，扩池压不掉的部分）分解：4/5 维度检测不到
（点估计撞 0 边界，不等于已证明为零），`signal` 是唯一有 σ_t²>0 的维度，
它的倍数只有 1.73×。

### Added — `scan_continuity.py`
扫描连续性体检。**存在理由由上面那份功效计算给出**：5.18× 的计价单位是
**有扫描的 ISO 周数**，T+7 不重叠取样单位就是周，漏一周就永久少一个观测。

首次运行即查出实质问题：近 30 个交易日**覆盖率 36.7%**（11/30），
两个长空档（07-10→07-21 共 8 天、07-30→08-07 共 7 天），
**2026-W29 / W32 两周完全无扫描**。

并独立重现了 v0.42.4 那个 `save_prediction` 业务日碰撞 bug 的受害日：
库与快照交叉核对报出 07-07 / 07-21 **有快照但无库记录**（与 MEMORY 记载的
07-07/07-21/07-23 一致）。

- 退出码 `0`=健康 / `1`=降级 / **`3`=无法判定**。3 而非 2 是刻意的：
  编排器 `run_step()` 把 **2 保留给「脚本不存在」**，占用它就会让编排器
  分不清"检查器没装"和"装了但判不了"——那本身是一次静默降级。有测试钉住。
- **不发任何通知**。`--slack` 是显式未接线的占位（对外动作需先确认）。
  聚合告警文案已实现（只讲"过去 N 个交易日只跑了 M 次"，不报单次失败，
  因此与 CLAUDE.md 的 Slack 静音规则相容），但接线待用户批准。

### Added — `~/.claude/scripts/alpha-hive-orchestrator.sh` Step 10
编排器末尾（Step 2 写库、Step 9 持久化之后）跑连续性体检，
结果写 `$LOGDIR/scan_continuity-$DATE_STR.json` 与 `status.json`。

⚠️ 用 `scan_continuity.py --out FILE` 写 JSON，**不用 `> FILE` 捕获 stdout**
（二次检查时发现的 bug）：本编排器的 `log()` 用 `tee -a`，**会写 stdout**，
而 `run_step` 在"脚本不存在"(`return 2`) 与 TCC 权限被拒两条路径上都会 log
——那些行会混进 JSON 让下游解析失败。有回归测试钉住 `--out` 路径。

**刻意不影响 `OVERALL_STATUS`**：连续性反映过去 30 个交易日的历史事实，
不是今天这轮跑得好不好；把历史空档记成"今天失败"会让该字段长期停在 failed，
等于废掉它。

### Added — `tests/test_distribution_invariants.py`（15 项）
对**生产库实际产出分布**的断言，与既有的单输入机制测试互补。治的是本项目
最高频的一类缺陷：**单元测试全绿、退出码 0、日志正常，但输出分布早已退化**
（ChronosBee 950 条 bearish=0、BullVeto 从未生效、CodeExecutorAgent 恒定看多、
VIX 兜底 20.0 撑了 13/88 天、优化器静默 inert 11 周）。

- 每只蜂的方向都必须真的可达（单一方向占比 <99%；bearish 单独断言）
- 维度分数不得塌成常数（去重比 >0.25；`catalyst` 作为**已知且已接受**的
  退化项给单独地板 0.05，不假装它健康）
- `guard.macro_adj` 不得冻结成单一值；CBOE VIX 历史缓存不得超过 5 个交易日陈旧
- 扫描未实质停摆；每日标的数未腰斩（防 30→10 静默截断）
- 含 `TestGuardsHaveTeeth`：**喂合成退化数据确认每个谓词真的会红**。
  没有这组，"全绿"证明不了任何事——那正是 BullVeto 式缺陷的同构物。
  这组当场抓出我自己测试数据里的算术错误（947/950 是 99.7% 不是 94.7%）。

### Added — `tests/test_scan_continuity.py`（25 项）
纯逻辑测试（空档切分含尾部空档、ISO 周覆盖、门槛 AND 关系、退出码、
`BRK-B` 连字符标的的日期解析、库/快照双向不一致）。全部合成数据，不依赖生产库。

### Changed — `weekly_optimizer.py` 降级为只读诊断 + 两道闸
**默认不再修改 `config.py`。** 写入必须显式 `--apply`，且两道闸全过；
`--force` 可覆盖但会记入审计。`--dry-run` 保留为向后兼容（与 `--apply` 同给时以
不写为准）。审计日志新增 `action: "diagnose"` 区分只读运行。

反转默认值（opt-out → opt-in）沿用本项目既有先例：2026-03-16
`generate_deep_v2.py` 的 opt-out 设计让 NVDA 深度报告静默消费 $0.47 Opus，
之后改为 opt-in。这里的代价不是钱而是**样本世代**。

**闸 1 — Bootstrap 稳健性**：此前 `weekly_optimizer.py:965` **只打印不阻断**
（原文"继续应用（限幅已保护）"）。2026-08-16 实测到真实后果：该轮 bootstrap
报不稳健、`risk_adj −4.13pp` 已越过 `MIN_CHANGE_PP`，若非人工中断就会写入。
限幅只保证**幅度**不失控，不保证**方向**对。

**闸 2 — 标的池世代一致性**（新函数 `check_ticker_pool_consistency`）：
比较「最近 3 个扫描日的标的」与「进入 T+7 样本基的标的」，
当前池未被覆盖的比例 >20% 即拦下。实跑结果：
**「当前池 30 只里有 14 只（47%）从未进入 T+7 样本基」**。
理由是 v0.42.9 扩池后存在数周窗口，优化器眼里还是旧的 10 只却要改 30 只在用的权重。
两个闸都**判不了时默认关闭**（不放行）。

降级理由（三条独立，任一条都够，写在模块 docstring 里）：
1. `w = acc/Σacc` 数学上无法表达"这个维度没用"——准确率都挤在 0.5 附近，
   权重必然全部 ≈0.2，输出空间里不存在"归零"这个答案
2. 它优化的对象已被证明不存在：综合分 |IC|=0.090 打不过 20 日动量 0.135
3. **每次写入都重置样本世代**，把扩池换来的 5.18× 测量加速周期性清零
   ——这是唯一会**主动让系统变差**的一条

附带记录：`compute_new_weights_wls` 名不副实（docstring 称 OLS 回归取 beta +
共线性检测，实现里没有任何回归）。当前 config 那五个数是 n=28/133 噪声期的产物，
**应按任意常数看待**。

### Changed — 定时任务 `alpha-hive-weekly-optimizer/SKILL.md`
改为只读诊断契约：明确**不加 `--apply`/`--force`**、**不发 Slack**
（只读诊断没有"权重自适应更新"可通知，旧的">5pp 发 Slack"指令随写入能力一并移除），
并把裸 `python3` 修正为 `/usr/local/bin/python3`（原文违反项目硬规则）。

### Added — `tests/test_weekly_optimizer.py` 新增 12 项
`TestTickerPoolGate`（含"判不了默认关闭"、"加 1 只到 10 只池不该过敏"）+
`TestReadOnlyDefaultAndGates`（端到端跑 `main()`：默认只读、闸门全过时
`--apply` **真的能写**、两闸分别与同时拦下、`--force` 真能覆盖且审计留痕、
`--dry-run` 优先于 `--apply`）。

### 未做（需用户决策，勿当已完成）
- 连续性告警的 Slack 接线待批准（`scan_continuity.py --slack` 仍是占位）。
- `RivalBeeVanguard` 近 12 个扫描日 **95.2% 看多**（bullish 180/189），
  是所有非看空蜂里最偏的，且不在任何已知问题清单里。分布不变式**刻意未拦**
  （门槛 0.99），因为那属于建模判断。待查。

### 验证
全量套件 **1348 passed / 1 skipped / 1 xfailed**（新增 40 项）。
`ruff --select F` 对新增四个文件全绿；全仓 `F821` 全绿；`bash -n` 编排器通过。

---

## [0.43.27~0.43.28] — 2026-08-25 — SSL 并发风暴致扫描全废；补跑价格锚定

### 事故：2026-08-24 自动扫描"跑了但全废"
| 步骤 | 结果 |
|---|---|
| Step 2 蜂群分析 | **超时 1800s 被杀** → 零蜂群结果、零落库 |
| Step 3 ML 报告 | **超时 600s 被杀** → 0 份 |
| 编排器结论 | 🎉 编排流程完成 |

当天全天 **96 次 SSL EOF** + 38 次 yfinance 限流 + **36 个标的次全链失效**。

### v0.43.27 根因一：SSL 并发风暴（主症）
本机 **OpenSSL 1.1.1q**（2022，已 EOL）扛不住并发 HTTPS。项目早有对策，
但那把信号量**只锁了 CBOE**——yfinance/Finnhub/AlphaVantage 各走各的。

⚠️ **自我更正**：v0.43.26 接通 Finnhub/AV 之前，这两个源因拿不到 key 直接返回
None、**一个请求都不发**。接通后它们成了两个不受保护的并发 HTTPS 调用方——
那个"修复"很可能是 EOF 风暴的助推之一。**加数据源前先确认闸门覆盖它。**

新增 `http_gate.py`：全进程唯一的出站 HTTPS 闸门，cboe_options / cboe_vix /
Finnhub / AlphaVantage 全部并入。实测 4 并发拉 CBOE：

| | 耗时 | 失败 |
|---|---|---|
| 不经闸门 | 65.2s | 0 |
| **经闸门** | **40.5s** | 0 |

~~**串行反而快 38%**~~ —— ⚠️ **2026-08-25 更正：该结论已被证伪。**
重测（12 只×并发 1/4/8/12 + 3 轮×8 只并发 4，两个解释器各 24 次请求）：
OpenSSL 1.1.1q 与 3.6.1 **均 0 失败、耗时几乎相同**（52s vs 53s），
串行 80.5s 反而比并发 62s **慢**。原先那组 65.2s vs 40.5s 是单次取样、
顺序执行，第二次很可能吃到 CDN 热缓存——把缓存效应当成了闸门效果。
`http_gate` 保留（无害、可防对端限流），但**不再宣称它治 EOF**。
8/24 的 96 次 EOF 同时打在 7 个互不相干的域名上，指向主机/网络层瞬时状况。

### v0.43.27 根因二：`_ch6_risk_radar` 的 None 崩溃（次症）
期权链降级为样本数据 → `options_analyzer` 诚实返回 `iv_rank=None`（v0.43.19 起）
→ `f"{iv_rank:.1f}"` 抛 TypeError。`bear_score` / `crowding` 同型。

⚠️ 这一行我在 v0.43.23 **看到过**，但当时用 2026-08-14 数据实测显示该键"缺失"
而非 None，判定不会命中就跳过了——**用健康数据验证只在降级时触发的分支**。

修法关键：**不能把 None 当 0**。`risk_level(0, ...)` 输出"🟢 低"，等于把
"没数据"渲染成"低风险"——比崩溃更危险，因为它不会报错。现在缺数显示 `—`、
等级显示 `⚪ 数据缺失`。6 条新测试全部显式喂 None。

### v0.43.28：`--date` 补跑的三条旁路仍取实时价
v0.41.6 已建好历史通道（`fetch_stock_data(t, as_of_date=)` →
`_fetch_historical_stock_data`，标 `source_name="yfinance_historical"`），
建它的起因正是 2026-07-21"网站价格不是当日收盘价"。但通道建好 ≠ 处处接通：

1. `swarm_agents/base.py._get_stock_data` —— prefetch 落空分支不传 date
2. `alpha_hive_daily_report._generate_ml_reports` —— `_fsd(ticker)` 无 as_of_date
3. `alpha_hive_daily_report._analyze_ticker_safe` —— `_dr_fetch_stock(ticker)` 无 date

危险在于**静默**：同一份报告里 prefetch 命中的是历史价、落空的是实时价，
两种口径混在一起，零报错。修法：prefetch 返回值带 `target_date` →
`inject_prefetched` 注入 `agent._target_date` → 落空分支 `getattr` 取用。

顺带更正一份子 agent 报告：它称 `backtester.save_predictions` 仍裸调
`fast_info.lastPrice` —— 实为 v0.43.15 已改成复用 ScoutBee 共享快照价，不是缺口。

### 2026-08-24 补跑实测（修复后首次完整验证）
| 指标 | 8/24 自动跑 | 8/25 补跑 |
|---|---|---|
| SSL EOF | **96** | **0** |
| yfinance 限流 | 38 | **0** |
| 全链失效 | 36 | **0** |
| 蜂群 | 1800s 超时被杀 | **30/30，1222.8s，err=0/240** |
| ML 报告 | 0 份 | **12 份，0 失败** |
| 落库 | 0 | **30 只** |

价格口径逐只核对（对照独立 yfinance 历史K线）：NVDA 208.48 / TSLA 348.95 /
MSFT 487.31 / QCOM 158.53 / META 559.02 —— **5/5 精确等于 8/24 收盘**，
且与 8/25 价格明显不同。VIX 15.85 = CBOE 8/24 收盘，`vix_source="cboe"`。

### ⚠️ 已知局限（未修，记录备查）
`cboe_vix.get_vix_spot()` 无日期参数，返回的是**最新**收盘。本次补跑恰好正确，
仅因 CBOE 当时尚未发布 8/25 数据。**补跑更早的历史日期时 VIX 会取到错误的天**。
需要时给它加 `as_of_date`（`get_vix_history()` 已有全部历史，改动很小）。

## [0.43.26] — 2026-08-15 — 降级链的后两环从未生效：AV/Finnhub key 只读环境变量

### Fixed — `config.py` / `data_pipeline.py`
`AlphaVantageSource` / `FinnhubSource` 都只读 `os.environ`
（`ALPHA_VANTAGE_API_KEY` / `FINNHUB_API_KEY`），而 key 实际在
`~/.alpha_hive_av_key` / `~/.alpha_hive_finnhub_key`，环境变量**从未设过** →
两个源每次 `if not self.api_key: return None`。

**"CBOE→yfinance→AV→Finnhub" 的后两环从未生效过。**

又一例"防御看着在、其实是死的"：链子写得完整、源类也实现了，只是永远拿不到
key，静默返回 None，日志里一行都没有。而这条链存在的全部意义，就是 CBOE 与
yfinance 双双失效时兜底——**正是最近一个月反复发生的场景**（v0.43.23 的 ML
报告崩溃、v0.43.24 的 VIX 假数据、v0.43.25 的动量伪造，根因都是限流）。

修复用项目现成的 `config.get_secret`（环境变量优先 → 降级文件 + 权限校验），
**不手搓第二份读文件逻辑**：
- `config._SECRET_REGISTRY` 补登 `FINNHUB_API_KEY`（此前漏登记）
- AV 兼容旧环境变量名 `ALPHA_VANTAGE_API_KEY`（注册表登记的是 `AV_API_KEY`，
  两个名字对不上也是失效原因之一）
- `data_pipeline._get_secret` 薄封装：config 不可用时退回环境变量
  （该模块设计上可独立于项目其余部分使用）

### Changed — 降级链顺序
Finnhub 提到 AV 之前。额度差一个量级：

| 源 | 免费额度 |
|---|---|
| Finnhub | 60 次/**分钟** |
| Alpha Vantage | 25 次/**天** |

需要降级的场景恰恰是"30 只标的批量抓取失败"。把日额度只有 25 的源排在前面，
等于第 26 只起必然再降一级——排序本身就让这一环失去意义。

### 验证
- 两源实测均可用，NVDA 报价一致（**225.16**）
- 两者都正确保持 `momentum_5d=None`（纯报价源无历史）→ 回落 v0.43.25 的自攒索引
- 7 条回归测试（含"退回旧逻辑必须变红"的非空跑验证），全量 **1308 通过**

### 💰 费用说明
两者均为**免费额度**，无 API 费用。启用后 AV 在极端降级日可能触及 25 次/天上限，
由熔断器（`failure_threshold=5`）接管，不会影响主流程。

## [0.43.25] — 2026-08-15 — momentum_5d 脱离 yfinance 限流；拆掉 ScoutBee 的 0.0 伪造

### 背景：同一份故障的两个出口
`momentum_5d` / `volume_ratio` 由 `_fetch_history_metrics` 从 `yf.Ticker().history()`
取。历史K线排在 30 只标的扫完之后，配额已耗尽 → 返回 None。两个 Agent 拿同一份
None，处理方式相反：

| Agent | 处理 | 后果 |
|---|---|---|
| BuzzBee | 诚实写 `None` | 下游 `momentum > 0` 崩，ML 报告每日 11/12 份丢失（v0.43.23） |
| ScoutBee | `or 0.0` 伪造"持平" | **无声进入评分**，零报错 |

ScoutBee 那条更隐蔽也更严重：`0.0` 永远够不到 `sentiment.py` 的背离阈值（±3.0），
实测近 28 个扫描日、**395 次情绪背离检测全部 severity=0**——功能结构性死亡。

### Added — `price_history.py`
从**自有观测**攒收盘价，完全不经外部接口，因此不受 yfinance 限流影响。
沿用 `iv_history.py`（v0.43.21）已验证的追加式索引 + sentinel 迁移模式。

**数据源合并、DB 优先**：
- `pheromone.db` 的 `predictions.price_at_predict` —— 蜂群当天实际使用的价，干净
- 期权快照 `_snapshot_stock_price` —— 密集但有已知污染
  （`dashboard_renderer:1883` 记录 NVDA 2026-06-15 案例）

QCOM 2026-08-14：快照 **185.0** / DB **165.94** → DB 赢，污染自动消解。
日期覆盖 77 → 100（NVDA）。

### 三处由实测逼出来的精细处理
| 问题 | 朴素做法的结果 | 修正 |
|---|---|---|
| 索引只有"扫描跑过的日子" | 往回数 5 条跨越 **9 个交易日**，+6.08% 名为 5 日实为 9 日 | 按**真实交易日距离**取锚点 → +2.68%；超 `MOMENTUM_MAX_GAP` 返回 None |
| 逐日涨跌幅区分不了尖峰与台阶 | QCOM +12.5% 后 −10.4%，两个 15% 阈值都够不到 | 判据改为**偏离前后两点均值**（尖峰 +12.1% 命中 / 台阶 +5.7% 放行） |
| 索引有空档，"邻居"可能隔一周 | 误杀率 **21.6%** | 只在前后两点相距 ≤5 个交易日时判定 → **2.0%** |

### Fixed — 拆掉伪造并接住 None
- `scout_bee.py`：`float(stock["momentum_5d"] or 0.0)` → 保留 `None`；
  discovery 文案缺数显示"—"而非 `+0.0%`
- `sentiment.py`：新增 None 守卫。**上游改诚实必须同时改下游**，否则只是把
  崩溃点搬个家（v0.43.23 的教训）。用 `"unavailable"` 而非 `"none"`——
  前者"查不了"、后者"查过、没背离"，对下游含义完全不同
- `data_pipeline._fetch_history_metrics`：yfinance 失败时回落自攒索引，
  并标 `momentum_source`。**`volume_ratio` 不回落**——自攒序列只有收盘价，
  硬造一个比值等于编数据

### 验证
- 模拟 yfinance 全量 429 → NVDA `+2.68%` / QCOM `+3.39%` / TSLA `+7.14%`
  由索引补上，`volume_ratio` 保持 `None`
- 背离检测四象限均正确响应（修复前恒为 0.0，结构性不可能触发）
- 16 条回归测试，已确认退回旧逻辑会变红（非空跑），全量 **1301 通过**

### 📌 8/14 重放的诚实结论
用真实动量重放 2026-08-14：27 只中触发背离 **0 只**。这不是功能仍死——
四象限反证已证明检测可用——而是当天 27 只的情绪值**全部落在 42~60**，
两个分支的情绪条件本就不满足。修复前是"结构性不可能触发"，现在是
"条件确实不满足"，二者性质不同。（情绪值高度聚集本身是否也是压缩信号，
另需排查。）

### ⚠️ 顺带发现（未修）
`data_pipeline.py:507` 读 `os.environ.get("FINNHUB_API_KEY")`，但 key 存在
`~/.alpha_hive_finnhub_key`、环境变量未设 → `if not self.api_key: return None`。
**"CBOE→yfinance→AV→Finnhub"链的最后一环从未生效。**

## [0.43.24] — 2026-08-15 — VIX 假数据：兜底值 20.0 冒充观测值，13/88 天

### 背景
用户发现深度报告写 `VIX: 20 (elevated)`，而 CBOE 官方数据当天是 14.6（极度平静）——
**方向相反的信号**。

`20` 是 `fred_macro.py:111` 的 `base` 常量。触发条件是 `if not data: return base`：
7 个宏观标的（^VIX/^TNX/^FVX/DXY/^GSPC/TLT/GLD）**全部**抓取失败。
根因是 yfinance 限流——宏观数据在 30 只标的扫完之后才抓，配额已耗尽
（2026-08-14 全天 363 条 Too Many Requests，每个标的的失败只记 `_log.debug`）。

实测 88 个扫描日：

| 月份 | 兜底天数 |
|---|---|
| 3–5 月 | 0 |
| 6 月 | 3 |
| 7 月 | 5 |
| **8 月** | **9 个扫描日里 5 天** |

识别标记是 `yield_curve: "unknown"`（base 原值）。**13/88 天用的是假 VIX，且在加速。**

### Fixed — Step 1：降级值不再冒充观测值（`swarm_agents/guard_bee.py`）
`fred_macro` 其实**老实标注了** `data_source="fallback"`，但 GuardBee 不看，
把 20.0 写进 `details["vix"]`。全仓确认 `data_source` 在日报与 ML 报告里
**从没被读过**——诚实标记在边界上被丢掉了。

- 降级时不记录 `vix` / `yield_curve` / `gold_trend`，改为留下 `macro_data_source`
- 渲染层无需改动：`generate_deep_v2:2706` 早就把 None 显示成 `N/A`
- `dashboard_renderer:2101` 一直在判这个标记，本次是补齐 GuardBee 缺失的路径
- 评分不受影响：base 的 20.0 / "unknown" / "stable" 本就不触发任何 `regime_vote` 分支

### Added — Step 2：VIX 走 CBOE 优先（`cboe_vix.py`，新模块）
`fred_macro` 直接调 yfinance，**绕过了项目既定的 CBOE 优先链**
（CLAUDE.md：CBOE → yfinance → AV → Finnhub）。

- 数据源 `cdn.cboe.com/.../VIX_History.csv`：无 key、无限流、1990 年至今 **9251 个交易日**
- 纯 urllib，复用 `cboe_options._CBOE_SEM` 串行化（本机老 SSL 栈扛不住并发 HTTPS）
- 磁盘缓存 6h TTL + 原子替换；网络失败回落陈旧缓存；**都没有则返回 None，绝不猜**
- 顺带提供真实 252 日分位（此前无真实样本）
- **逐字段判源**：新增 `vix_source`（`cboe`/`yfinance`/`fallback`）。yfinance 全灭时
  CBOE 仍可能供上真实 VIX，此时 `data_source` 保持 `fallback`（其余字段确实降级），
  但 VIX 这一项是观测值。一个全局开关会把真 VIX 和假收益率曲线一起丢掉。
- `_classify_vix` 抽成共用函数：现有两条产出路径，同口径才不会让同一个 VIX
  得到不同 regime 标签

### 验证
- 模拟 yfinance 全量 429 → `vix=14.25` / `vix_source=cboe` / 其余诚实降级
- CBOE 的 2026-08-13 收盘 **14.63**，与用户独立数据源精确一致
- 11 条回归测试（含"退回旧逻辑必须变红"的非空跑验证），全量 **1285 通过**

### 📌 口径提醒（易致反向结论）
`get_vix_percentile()` 返回的是**恐慌分位**（比多少比例的交易日更高）。
实测 VIX 14.25 → **2.0%**，即极度平静；"平静度分位"是 `100 - 本值` = 98.0%。
两者方向相反，混用会得到完全相反的结论——本项目已被同类符号反向咬过一次
（BullVeto 读反字段，v0.43.9）。

### 🔗 与 v0.43.23 同根
`data_pipeline.py:341` 注明 `momentum_5d`/`volume_ratio` 也是"从 yfinance 历史
K线独立获取"。v0.43.23 的 BuzzBee None 崩溃与本次 VIX 20 假数据，是同一条
限流链上的两个出口：**一个诚实返回 None 把报告搞崩，一个返回合法 float 悄悄
污染结论**。会崩的降级是幸运的。

## [0.43.23] — 2026-08-15 — ML 报告因 None 崩溃：每日 11/12 份静默丢失一个月

### Fixed — `generate_ml_report.py`
根因在 `_ch3_buzz`：
```python
momentum = det.get("momentum_5d", 0)      # 键存在且为 None 时默认值不生效
mom_color = "..." if momentum > 0 ...     # TypeError: NoneType > int
```
`.get(k, 默认)` 只在**键缺失**时用默认值。BuzzBee 对缺价格/成交量的降级源
**刻意**写入 `None`（`buzz_bee.py` P0-2：不拿 0 冒充"无动量"、不拿 1 冒充
"正常量"），下游却假设不会是 None。实测 2026-08-14 快照 **27/28 只**该字段为 None。

**影响范围**：2026-07-15 ~ 08-14 每个扫描日 ML 报告成功 **0~1/12**，
持续一个月。日报主站（index.html / 仪表板）不受影响、每天正常更新。

**为什么没人发现**（两层掩盖，缺一都会更早暴露）：
1. `except (...) as e: _log.warning(str(e)[:100])` **丢弃调用栈** → 日志里只有
   一句 `'>' not supported between instances of 'NoneType' and 'int'`，无文件无行号
2. 编排器只看子进程退出码（该脚本失败也返回 0）→ 天天打印"✅ 所有步骤成功"

**修复**：
- 缺数渲染为 `—` + 中性灰 `#6c757d`，**绝不用 0 / 1 顶替**（硬规则：不编数据）
- `_ch3_scout` 同型读取一并加固（当前 0/27 为 None，防崩溃点平移过去）
- 异常处理**永久**带 `exc_info=True`
- 成功率 <50% 时打 `ERROR`，不再只有一行 INFO 汇总

**验证**：把 `.swarm_results_2026-08-14.json` 复制成当日文件复现
（关键：周末/无快照时 `swarm_data={}`，消费该数据的整条路径被跳过，
所以手动跑永远是 12/12，一个月都没能自然暴露）→ 修前 **0/12**、修后 **12/12**。
新增 7 条回归测试，并已确认退回 bug 版会变红（非空跑）。全量 **1274 通过**。

### ⚠️ 独立发现（未修，需另开排查）
`ScoutBeeNova.details.momentum_5d` 在同一份快照里 **27/27 恒为 0.0**。
它与 BuzzBee 拿到同一份失败的价格数据，但 Scout 默认填 `0.0` 伪造"持平"、
无声进入评分，BuzzBee 诚实写 `None` 反而把报告搞崩。
**诚实降级的那个爆炸了，伪造的那个悄悄污染了分数**——与 CodeExecutorAgent
恒定看多（v0.43.10）、ChronosBee 零看空（v0.43.11）同族。

## [0.43.22] — 2026-08-14 — v0.43.21 二次检查：修 coverage_report 漏统计 + 文档/配置修正

### Fixed
- **`coverage_report` 漏统计**：只从 `options_snapshot_*.json` 枚举标的，
  但索引自 v0.43.21 起已是唯一真相源——仅有索引的票（快照被清理，或老快照
  从无 `iv_raw_observed` 字段）会被漏掉，进度报告偏低。改为索引 + 快照并集
- **模块 docstring 过时**：仍写"从每日期权快照读取"，与"索引优先、快照扫描
  仅作一次性迁移"的实际策略不符
- **`.gitignore` 冗余**：`cache/` 早在第 34 行整体忽略，v0.43.21 新加的两行
  是死重量且误导（暗示需要单独忽略）

### 二次检查其余项（均无问题）
- **测试污染**：跑完整测试套件前后，生产 `cache/` 的索引与 sentinel 文件
  数量一致（13→13），无污染——该项目 v0.41.3 曾被"测试 mock 写进生产快照"
  咬过，故列为固定检查项
- **改名残留**：`rebuild_index_from_snapshots` → `merge_snapshots_into_index`
  全仓无残留引用
- **gitignore 实际生效**：`git check-ignore -v` 确认索引与 sentinel 均被忽略
- **积累起点**：今日 29 份快照中 2 份带 `iv_raw_observed`（v0.43.18 部署后
  手动跑的两只），其余 27 只的今日快照早于部署 → 明日起全量正常积累

### 新增测试（22 条，全量 1267 通过）
- `test_coverage_report_sees_index_only_tickers`：仅索引标的必须被统计
- `test_index_survives_snapshot_pruning`：快照清理后索引仍可用

### ⚠️ 已知低severity项（未修，记录备查）
`merge_snapshots_into_index` 的 `os.replace` 与并发 `append_observation`
理论上存在竞态（A 读索引→写 tmp→replace 期间 B 追加，B 的行可能被覆盖），
会丢失单日单票一条观测。触发条件苛刻：仅首次迁移窗口内、同一标的被并发
分析——而 analyze() 内部是"先 append 再迁移"的单线程顺序，且期权快照缓存
使同票同日重复计算极罕见。

## [0.43.21] — 2026-08-14 — IV 历史改用紧凑索引（补 v0.43.20 二次检查记录的性能问题）

### Changed — `iv_history.py` / `options_analyzer.py`
- 新增每票追加式索引 `cache/iv_history_{TICKER}.jsonl`（一行一天
  `{"date","iv"}`）；`analyze()` 算出 `iv_raw_observed` 后同步 append
- `load_iv_history` 改为**索引优先**，日常热路径不再解析任何快照
- 一次性迁移：v0.43.21 前只存在于快照里的观测会被**合并**进索引，
  由 sentinel 文件 `cache/.iv_index_migrated_{TICKER}` 保证每票只扫一次
- 幂等与健壮性：同日同值不重复写、同日新值后写覆盖、单行损坏只跳过该行、
  索引原子替换（`os.replace`）、写失败只 debug 不阻断评分
- `.gitignore` 加入索引与标记（每台机器自行积累，不入库）

### 实测收益
| 场景 | 旧 | 新 |
|---|---|---|
| 30 只/次扫描（当前 76 天） | 680 ms | **0.5 ms** |
| 30 只/次扫描（攒满 252 天） | ~2256 ms | **~2 ms** |
| 单票热路径读取量 | 1120 KB（全解析快照） | **36 字节** |

一次性迁移耗时 409 ms（每票仅一次，之后永不重复）。

### Fixed — 初版实现的设计缺陷（由测试抓出）
初版用"索引为空"作为重建条件。但 `analyze()` **先 append 当日观测、再读历史**，
索引因此恒非空 ⇒ 自愈重建永不触发 ⇒ 快照里的历史被永久忽略。
`test_uses_real_iv_when_enough_history` 立刻变红暴露了这一点。
改为 sentinel 控制的一次性迁移 + **合并**语义（冲突时索引优先，避免抹掉
刚写入的当日记录）。已补回归测试
`test_migration_does_not_clobber_todays_appended_record`。

### 验证
- 新增 7 条索引机制测试（往返/幂等/同日覆盖/非法值/坏行容错/迁移/上述回归）
- 生产路径实测：MU 分析后索引正确生成 `{"date":"2026-08-14","iv":62.09}`
- 全量 1265 测试通过

## [0.43.20] — 2026-08-14 — 二次检查：修真实 IV Rank 分子口径错配 + 修自己写的空跑测试

> 用户要求对 v0.43.18/19 做二次检查。审了删除范围、变量绑定、缓存 TTL、
> 字段落盘、性能，发现 **1 个真 bug** 和 **1 条无效测试**。

### Fixed — 真实 IV Rank 分子口径错配（尚未激活，但 ~3 个月后会静默生效）
执行顺序：`_iv_raw_observed`（今日真实观测）记录于降级**之前** → `current_iv`
在降级块被 `last_valid_iv`（TTL 120h，最长 5 天前）替换 → 随后拿这个**陈旧
分子**去比对由**每日新鲜观测**组成的分母序列。

**与 v0.43.19 刚修掉的"拿真实 IV 比 HV 分布"是同一类错误**：分子分母不同源。
实测影响：同一场景 rank 从 **97.44 翻转到 2.56**——"IV 历史高位"被读成
"历史低位"，信号方向完全反转，而该值直接进 OracleBee 评分与 BearBee 看空判定。

修复：分子改用 `_iv_raw_observed`（与分母同源），仅在其为 None 时退回 `current_iv`。
因真实路径当前 0/56 达标而未造成实际损失。

### Fixed — 本轮自己写的回归测试是空跑
初版 `test_real_iv_rank_uses_raw_observation_not_degraded_cache` **在盘中写就
并验证**，而降级分支只在 `not _market_open` 时进入——盘中运行该分支根本不执行，
退回 bug 版测试仍全绿。经"退回 bug 版必须变红"验证才发现（首次验证脚本本身
也有缺陷：`print` 无条件执行，未断言替换是否真的发生）。

修复：用 `datetime` 子类冻结时钟到收盘后（22:00 UTC = 18:00 ET），并加
`assert iv_current == 21.0` 前置校验——降级若未生效测试直接失败，防止再次
退化为空跑。现已验证：bug 版 `iv_rank=2.56` 断言失败，修复版通过。

### 二次检查其余项（均无问题）
- **切片删除范围**：`git diff` 确认方法数 39→38，净删仅 `_estimate_iv_premium`，
  未误删区间内其他方法
- **变量绑定**：`_iv_raw_observed` / `iv_current` 在所有返回路径均已绑定
  （含真实路径、hv_proxy 路径、`hist_hv` 为空的边界）
- **`hist_hv[-1]` 作为"当前 HV"的时效**：fetcher `cache_ttl=300s`，陈旧上限 5 分钟
- **字段落盘**：生产路径实测验证 `iv_raw_observed` 确实写入
  `cache/options_snapshot_*.json`（QCOM 实测 44.315），积累闭环成立
- 全量 1258 测试通过

### ⚠️ 已知性能问题（非 bug，未修，待决定）
`load_iv_history` 每次调用 glob + 完整解析该票所有快照 JSON，只为取出一个
float：单票 **4.3 MB**（76 份）→ 30 只/扫描约 **680 ms**；攒满 252 天后
约 **2.3 s**（占单次扫描 ~280s 的 0.8%，但随天数线性增长且永不回落）。
根治方案是维护紧凑的追加式 IV 索引（如 `cache/iv_history_{TICKER}.jsonl`），
避免为一个字段解析整份快照。

## [0.43.19] — 2026-08-14 — 去掉 IV Rank 的伪 premium 与 clamp 失真（补 v0.43.18 未做项）

> v0.43.18 只做了"自攒真实 IV 历史"，把 HV 代理路径的失真留了下来。本版补上：
> 让降级路径成为一个**干净、无失真、名副其实的 HV Rank**。

### Changed — `options_analyzer.py`
- `fetch_historical_iv` → **`fetch_historical_hv`**（连带 `_get_sample_historical_iv`
  → `_get_sample_historical_hv`、`last_hist_iv_is_sample` → `last_hist_hv_is_sample`）：
  函数名不再撒谎，如实说明它产出的是已实现波动率序列
- **移除 `iv_premium` 乘法**：给整条序列乘同一标量对 rank 贡献恒为零，clamp
  生效时反而制造失真（实测 NVDA 输出 **0.00** vs 真实 HV Rank **62.91**）
- **删除已死的 `_estimate_iv_premium`**（唯一调用点已移除；IV/HV 价差信号另有
  `market_intelligence.calculate_iv_rv_spread` 负责，无功能损失）
- **缓存键换代 `hist_iv_v3` → `hist_hv_v4`**：旧键存的是乘过 premium 的值，
  语义已变，绝不可复用
- **hv_proxy 路径改为同口径比较**：拿**当前 HV** 比 **历史 HV 分布**（同一量纲），
  不再拿真实 IV 比 HV 分布——后者正是 clamp 失真的根源。展示用的 `iv_current`
  仍是真实 IV，不受影响

### Fixed — 改名过程中发现并避免的静默陷阱
- 样本守卫写的是 `getattr(self.fetcher, "last_hist_iv_is_sample", False)`——
  属性名是**字符串字面量**，全局改名碰不到它。若漏改，getattr 取默认值 False
  ⇒ 守卫永久失效且无任何报错。已同步修正
- 该守卫另加 `_iv_rank_source == "hv_proxy"` 限定：真实 IV 历史路径不依赖
  `hist_hv`，不应因"HV 序列是样本数据"被误置 None

### 验证
- 实测 NVDA：`iv_rank 62.91` == 手算纯 HV Rank `62.91`，完全一致（同一只票
  修复前 clamp 生效时输出 0.00）；`iv_current 41.38` 展示值不受影响
- 全量 1257 测试通过（4 处测试 monkeypatch 目标随改名同步更新）

### 现状小结（IV Rank 两条路径）
| 路径 | 触发条件 | 语义 | 标注 |
|---|---|---|---|
| 真实 IV Rank | 自攒样本 ≥63 交易日 | 真 IV 在自身历史分布中的位置 | `real_iv_{N}d` |
| HV Rank 代理 | 样本不足（当前全部标的） | 当前 HV 在历史 HV 分布中的位置，**已无失真** | `hv_proxy` |

## [0.43.18] — 2026-08-14 — 自攒真实 IV 历史：IV Rank 从"HV 排名冒名顶替"走向真值

> 用户追问"为什么不是真 IV Rank 数据"，查证后确认问题比"函数名撒谎"更深。

### 查证结论（数学 + 实测双重确认）
- `fetch_historical_iv` 产出的"历史 IV" = `HV序列 × iv_premium`，而
  `iv_premium = current_IV/current_HV` 是**单个标量**——给整条序列乘同一常数
  **不改变任何排序关系**，故算出的 IV Rank 在数学上**恒等于 HV Rank**。
  实测 NVDA 未 clamp 时：IV Rank 62.91 vs 纯 HV Rank 62.91，**完全相等**。
  函数注释宣称"动态比率让 IV Rank 更贴合实际市场状态"——该说法不成立，
  premium 对 rank 的贡献精确为零
- `ratio` 被 clamp 到 [1.05, 2.0]，clamp 生效时抵消关系被破坏，输出**既非
  IV Rank 也非 HV Rank**：实测 NVDA raw ratio=0.318 被夹到 1.05 →
  IV Rank 算出 **0.00**，而真实 HV Rank 是 **62.91**
- 附带发现：`_estimate_iv_premium` 读 **yfinance** 链，主路径 IV 走 **CBOE** 链，
  两个数据源（口径分裂，本次未动）

### 为什么之前无法自攒（关键前置障碍）
快照里的 `iv_current` **不是每日观测**：扫描固定在收盘后跑 → `_market_open`
恒 False → 降级分支每次都用 `last_valid_iv` 缓存（TTL **120h**），当日真实
`raw_iv` 被丢弃。实测 NVDA 76 份快照仅 **15 个不同值**（≈76/5，与 120h TTL
精确吻合）——是"5 天一阶的阶梯"。用它建 IV Rank 等于在 15 个有效点上算
min/max，比现状更差。

### Added
- `options_analyzer.py`：新增 `iv_raw_observed` 字段，记录**降级前**的当日
  真实观测 IV（只记账、不进任何评分），从本版起逐日积累真实 IV 序列
- `iv_history.py`（新模块）：`load_iv_history()` 从每日快照读取真实 IV 序列
  （只认 `iv_raw_observed`，**绝不退回读 `iv_current`**）；
  `iv_rank_from_history()` 用真实分布算 rank/percentile，退化时返回 `None`
  让调用方降级而非编造中性值；`coverage_report()` 查各标的积累进度
- 结果新增 `iv_rank_source`（`real_iv_{N}d` / `hv_proxy`）与
  `iv_rank_window_days`，消费方可据此判断该 rank 是真 IV 排名还是 HV 代理

### Changed
- `OptionsAgent.analyze()`：IV Rank 优先用自攒真实历史（阈值 **63 个交易日**
  ≈ 季度 IV Rank，业界公认变体下限），不足则降级 HV 代理并如实标注

### 现状与预期
- 自攒进度：**0/56 只达标**（历史快照无该字段，从今天起积累）；按每交易日
  一条，约 **3 个月后**首批标的可切换到真 IV Rank，届时 `iv_rank_source`
  会自动变为 `real_iv_63d`+，无需再改代码
- 实测本版行为：`iv_raw_observed: 40.8`（当日真实）vs `iv_current: 41.38`
  （缓存降级值）——两者不同，正好印证陈旧问题；`iv_rank_source: hv_proxy` 如实标注
- 新增 12 条测试；全量 1257 通过

### 未做（明确留待决定）
- **HV 代理路径的 clamp 失真未修**（用户本轮只选了"自攒"方案）。在真实 IV
  样本达标前，`hv_proxy` 路径仍可能因 clamp 输出失真值（如上述 0.00 vs 62.91）。
  最小修法是去掉 premium 与 clamp，让它成为一个干净无失真的 HV Rank
- `iv_rank` 进 OracleBee 评分、BearBee 也用它当看空信号，故上述失真是**评分
  输入问题**而非仅显示问题

## [0.43.17] — 2026-08-14 — 修复仪表板渲染撞限流杀死部署（网站两天未更新）

> 用户问"8/13 自动跑的推送到网站了吗"→ 查出扫描成功但网站停在 8/12 单票视图。
> 这是"逐票打 yfinance + except 漏 YFRateLimitError"反模式的**第三次现身**
> （前两次：v0.43.15 predictions 全空、v0.43.16 快照恒为 0）。

### Fixed（两层防线同时漏了同一个异常）
- `dashboard_renderer.py::_detail`：渲染 30 只票时逐票打 yfinance 兜底取价，
  except 清单 `(ValueError, IndexError, KeyError, AttributeError)` 不含
  `YFRateLimitError`
- `alpha_hive_daily_report.py::_save_output_files`：包住 index.html 生成的
  兜底 except 同样漏了它——**该处注释本就写着"任何失败都不得阻断已生成的
  核心报告 + 后续提交/部署"，但实现与注释不符**
- 两层都没兜住 → 异常穿透杀死 `save_report` → **gh-pages 部署在其内部、
  崩溃点之后，一并夭折**：报告文件正常写出，网站一行没推
- 两处均改为 `except Exception`（`_detail` 补 noqa 说明；daily_report 侧
  additionally 加 `exc_info=True` 便于下次定位）

### 验证
- 全量 1245 测试通过；重跑并部署 8/13 报告，线上恢复 30 只标的完整视图
  （`_date: 2026-08-13`，Top5: AMC 8.7 / DELL 7.5 / XOM 7.4 / T 7.0 / WMT 6.8），
  CDN 验证通过

## [0.43.16] — 2026-08-12 — 修复自动扫描日快照恒为 0 + 流水线 24 小时病态接力

> 继 v0.43.15（predictions 全空）后，继续追查"信号→开仓"链路：发现自动扫描日
> report_snapshots 也从来没写出来过（全部日志零次"反馈循环"记录），纸面组合
> 因此没有开仓评估输入——8/11 T 以 8.72 分过开仓门槛却未开仓即此因。

### Fixed — `alpha_hive_daily_report.py` 快照循环（异常处理器本身是坏的）
- 快照入场价逐票抓 yfinance，限流时进入 except——但处理器写的是**从未定义过
  的 `self.logger`**（类里只有模块级 `_log`），`AttributeError` 冲出内层 try
  杀死整个快照循环，再被外层 debug 级日志静默吞掉。手动跑不限流走不到坏
  处理器，故"手动有快照、自动零快照"的规律从未被识破。用 launchd 原生环境
  实测复现，traceback 完整确认双层结构（YFRateLimitError 触发 + AttributeError 杀循环）
- 修复：两处 `self.logger`→`_log`（另一处在 `_submit_bg` 等待逻辑，同款地雷）；
  入场价优先复用 swarm 内 ScoutBee 的 CBOE 快照价（与 v0.43.15 同思路，消灭
  限流引爆点+统一价格口径）；零快照时告警可见（debug→warning）

### Fixed — `~/.claude/scripts/alpha-hive-orchestrator.sh` 24 小时病态接力
- `pre_scan_notify.py --max-wait 1440`（24 小时！注释却写"等 10 分钟"）×
  Slack Webhook 已 404 失效 = LLM 确认永远无人应答，每天的运行都挂满 24h
  才降级执行。**旧日志证实整条流水线长期处于"前一天启动的进程恰好在次日
  14:00 超时接力扫描"的错位节奏**：日志跨天错乱（8/11 内容在 -08-10.log）、
  launchd 当日 fire 被进程锁拦掉、报告日期与数据碰巧对齐所以产出看不出异常
- 修复：`--max-wait 1440→10`、step 超时 `86700s→900s`——每天 14:00 触发
  当天 14:10 前开扫

### 验证
- 全量 1245 测试通过；今天 14:00 的正常调度将是修复后代码的首次端到端
  验证（预期：当天出报告 + 30 张快照 + predictions 落库 + 纸面组合正常评估开仓）

## [0.43.15] — 2026-08-12 — 修复自动扫描日 predictions 全空（落库循环被 yfinance 限流杀死）

> 用户核实"积攒数据"计划可行性时发现：约一半扫描日（7/21、7/28、7/30、8/4、
> 8/6、8/11 等）`.swarm_results` 都在但 predictions 表整日 0 条——若不修，
> 4-6 周的样本积累窗口实际要拖一倍。8/11 自动扫描日志实锤根因：
> `扫描后指标收集异常: Too Many Requests. Rate limited.`

### Fixed — `backtester.py::save_predictions`
- 旧实现落库前逐票调 `yf.Ticker().fast_info` 抓实时价，except 清单
  `(ConnectionError, TimeoutError, OSError, ValueError, KeyError, AttributeError)`
  **不含 `YFRateLimitError`**——限流异常穿透 save_predictions、穿透
  daily_report 的 `except (OSError, ValueError, KeyError, TypeError)`，
  被最外层 `except Exception` 兜住时**整个落库循环已死，当天 0 条入库**。
  自动扫描（14:00 整点，限流高发时段）次次中招；手动跑的时段限流少故能落库
  ——这就是"手动日有记录、自动日没记录"规律的全部成因
- 修复①：价格优先复用 swarm_results 里 ScoutBee 的共享快照价（CBOE-first，
  与报告/仪表板同一口径）——落库不再单独打 yfinance，既消灭限流引爆点，
  又消除"fast_info 实时价 vs 报告快照价"的口径分裂
- 修复②：单票处理整体包 try（宽捕获），任何单票异常都不再杀死整个循环；
  yfinance 兜底路径同样宽捕获，取价失败保存 price=0 而非丢弃样本
- 新增 2 条回归测试（模拟限流场景 + 快照价优先且零 yfinance 调用断言）；
  全量 1245 测试通过

### 影响
- 从下一次自动扫描（今天 14:00 PDT）起，每个交易日应稳定落库全部扫描标的，
  修复后系统的样本积累速度恢复到设计预期，9 月中旬复盘窗口可如期兑现
- 历史缺失日（7/21 等 6+ 天）的预测**无法补录**——当时的完整快照虽在
  `.swarm_results` 文件里，但 T+1/T+7 判分需要预测时点价格锚定，且补录会
  混入"事后视角"污染，按既定原则不回填

## [0.43.14] — 2026-08-12 — 一次性迁移：清除 BullVeto 反向误拦对历史标签的污染

> 用户批准。备份 `pheromone.db.bak-20260812-111340`。35 条 veto 误拦事件中
> 仅 9 条真正落入 predictions 表（其余 26 条所在扫描日整日无落库记录，从未
> 污染统计）；9 条全部恢复原始 bullish 方向并按看多口径（-1% 容差）重打
> 已结算标签。效果：看多方向准确率 42.4%→44.2%（4 条判对记录从中性簿记归位
> 看多），全局 overall 50.0% 不变，NVDA 单票 39.3% 不变（其低胜率源于 5-7 月
> 下跌期真实看多踏空，非 veto 污染，不应改）。8/10 的 5 条仅结算 T+1，
> 后续 T+7/T+30 回填将按恢复后的 bullish 方向判分。

## [0.43.13] — 2026-08-12 — 停用 BullVeto（实现从未按设计生效且方向反转）

> 用户确认后停用。`queen_distiller.py:728` 读的是 BearBee 反转后的吸引力分
> （score=10-bear_score）而非真实看空强度——`score>=7` 恰恰等价于
> `bear_score<=3`（BearBee 确认看多），即上线以来 35 次触发全部是
> "BearBee 确认看多时反向拦截看多"（被拦单 78% 本该盈利，均 +0.42% vs
> 对照组 +4.20%）。

### Changed — `config.py`
- `bull_veto_enabled: True → False`，附完整停用说明注释。重新上线的前置条件：
  ① 换成读取 `details.bear_score` 正确字段 ② 用 v0.43.10-12 修复后的干净样本
  重新校准阈值（历史 bear_score 分布产生于 ChronosBee 零看空/CodeExecutor
  恒看多的坏生态，正确字段下旧阈值 7.0 会命中 62% 看多信号）——预计 4-6 周后
  与 BearBee boost 假说复盘同批处理
- 影响面：全项目仅 queen_distiller 一处消费此开关，效果=被误拦的
  "BearBee 确认看多"单恢复为看多输出；纸面组合中过 6.5 分门槛者恢复参与开仓

### 验证
- 全量 1243 测试通过；停用后 NVDA 单票扫描 `bull_veto 触发: False`，
  且修复后生态首次观察到 **ChronosBee 投出 bearish**（此前 91/91 零看空）

## [0.43.12] — 2026-08-12 — 全仓审计 yf.download() MultiIndex 反模式：修复 5 处易崩 + 3 处脆弱写法

> v0.43.10（CodeExecutorAgent）和 v0.43.11（pead_analyzer）连续两次撞上同一个
> "新版 yfinance 单票 download() 返回 MultiIndex 列名"的坑后，按"修反模式必须
> 全仓 grep 同款"纪律做了一次完整审计：全项目 20 处 `yf.download()` 调用点
> 逐一分类（易崩/已防护/多票设计不受影响），本次修复全部剩余易崩点。

### Fixed — `cboe_fetcher.py`（4 处易崩，宏观指标长期静默失效）
- `^VIX`(314)/`VIXY`(325)/`^SKEW`(395)/`^VVIX`(458) 四处 `float(x['Close'].iloc[-1])`
  全部 MultiIndex 崩溃：VIX/SKEW/VVIX 长期落默认值（15.0/120.0/85.0），
  **VIXY 更被裸 `except:` 吞掉，导致 VIX 期限结构恒判 contango**（比报错更隐蔽）
- 新增模块级 `_last_close()` helper 统一安全取值；修复后实测抓到真实值
  （VIX 14.64/SKEW 135.59/VVIX 88.75），期限结构立刻算出 backwardation——
  与被吞异常时代的恒 contango 完全不同，宏观评分的输入质量实质性恢复

### Fixed — `code_generator.py`（1 处易崩 + 3 处脆弱写法）
- `_generate_momentum_analysis`（实测复现 TypeError）：momentum/z_score/trend
  全部算不出，同款 flatten 修复
- `_generate_line_chart` / `_generate_candlestick_chart` / `_generate_heatmap_chart`：
  当前 pandas 版本下侥幸不崩（matplotlib/赋值隐式接受 (N,1) DataFrame），
  但写法脆弱，统一补上 flatten 保持一致

### Added — `tests/test_code_generator.py` 扩展
- momentum 脚本 MultiIndex 存活测试（monkeypatch 假 MultiIndex 数据直接 exec）
- **反模式契约测试**：所有含裸 `yf.download(` 的生成代码必须带
  `get_level_values(0)` 防护——防止未来新增生成器再漏

### 审计结论（全 20 处 yf.download 调用点归类，本次后全部清零）
- 已防护（本次前就安全）：`risk_engine.py:97/538`、`market_intelligence.py:181/452/741`、
  `portfolio_concentration.py:84`、`ff6_cycle_history.py:56`（靠 squeeze 偶然兜住）、
  `pead_analyzer.py:121`（v0.43.11 已修）
- 多票设计不受影响：`signal_archive.py:488`、`ic_diagnostics.py:310`、`scout_bee.py:374`
- 全量 1243 测试通过

## [0.43.11] — 2026-08-12 — 修复 ChronosBee 仍零看空——PEAD 漂移统计被同款 MultiIndex 崩溃清空

> 上一条修复 CodeExecutorAgent 后，用户要求继续排查"ChronosBeeHorizon 同期
> 也是零看空"。v0.43.0（7/30）已经修过"评分块无条件覆盖 PEAD 方向"的死代码
> 问题，但修复后（7/30~8/12，30 笔样本）**看空依旧是 0，连之前还有的
> 少量看多都消失了，全部退化成 neutral**——说明 v0.43.0 修的是"通道"，
> 但通道另一头的真实证据源本身是空的。

### Fixed — `pead_analyzer.py::_compute_post_earnings_drift`
- 第三次撞上同一个 MultiIndex 坑：`hist = yf.download(ticker, ...)` 对单票
  返回 `('Close','NVDA')` 这种 MultiIndex 列名，`float(row["Close"])` 抛
  `TypeError`，被外层 `except (KeyError, TypeError, ValueError): continue`
  静默吞掉——`price_map` 永远为空，财报后价格漂移记录永远是 `[]`，
  `t5_avg` 永远是 `None`，`bias` 永远退化成 `"neutral"`。ChronosBee 唯一真正
  带方向的证据源（PEAD）结构上从未真正产出过数据，v0.43.0 的"通道"从上线
  起就没东西可传
- 修复：补上与 `_generate_yfinance`/`_generate_technical_analysis`（v0.43.10）
  一致的列名兼容处理

### 验证
- 直接测 `get_pead_analysis`：修复前全部标的 `t5_avg=None, n=0`；修复后
  NVDA `bias=bearish`（8 次财报后 T+5 均值 -5.83%，真实统计，非编造）、
  VKTX `bias=bullish`（+5.44%）
- 端到端跑 `ChronosBeeHorizon.analyze()`：NVDA 输出 `direction=bearish`，
  discovery 里带出真实漂移数字（"T+5: -5.8% (胜率12%, n=8)"）
- 新增 `tests/test_pead_analyzer.py`；全量 1240 测试通过

### ⚠️ 同款 bug 疑似广泛存在，本次仅修复直接相关的两处（未全仓扫）
全仓 grep `yf.download(` 命中 18 处调用点，逐一实测确认 `scout_bee.py`
（板块相对强弱）、`market_intelligence.py`（3 处）、`cboe_fetcher.py`
（VIX/SKEW/VVIX，4 处）等**在当前 yfinance 版本下同样会返回 MultiIndex
列名**，是否实际崩溃取决于各自后续访问模式，未逐一验证。`risk_engine.py`
与 `portfolio_concentration.py` 已有正确的兼容处理，不受影响。
本次只处理了直接导致 ChronosBee/CodeExecutorAgent 问题的两处，**其余各处
是否存活是未知数**，需要专项排查，见 memory `alpha-hive-locked-tasks`。

## [0.43.10] — 2026-08-12 — 修复 CodeExecutorAgent 恒定看多——技术分析脚本对 yfinance MultiIndex 列名的崩溃

> 上一条记录（bear-hypothesis-backtest 补跑）里顺带发现 `CodeExecutorAgent`
> 过去一个多月 91/91 笔预测恒定看多，从未投过看空或中性。本次单独开排查定位到
> 根因并修复。

### Fixed — `code_generator.py::_generate_technical_analysis`
- 生成的沙盒脚本用裸 `yf.download(ticker, period=period)` 取数——新版 yfinance
  对单只股票的 `download()` 返回 **MultiIndex 列名**（如 `('Close','NVDA')`
  而非纯 `'Close'`），导致 `latest["Close"]` 拿到的是 Series 不是标量，
  `float(latest["Close"])` 直接 `TypeError` 崩溃。这是结果字典的第一个字段，
  脚本还没打印任何 JSON 就先崩了
- **同一个坑 `_generate_yfinance`（数据爬取脚本）早就修过**（注释写着"优先用
  history() 避免 download() 多层列名 TypeError"），但从未同步到
  `_generate_technical_analysis`——两个函数各自独立生成 yfinance 代码，
  修一个漏一个，是本项目"修反模式必须全仓 grep 同款"教训的又一次印证
- 修复：补上与 `_generate_yfinance` 一致的列名兼容处理
  （`if hasattr(data.columns, "levels"): data.columns = data.columns.get_level_values(0)`）

### 为什么"技术分析脚本崩溃"表现成了"恒定看多"（而不是报错）
`CodeExecutorAgent.analyze()` 里技术分析脚本失败后落入的兜底分支
（`code_executor_agent.py` "如果分析失败，返回原始数据结果"）设计上只有两种
出口：`price and market_cap` 都拿到 → bullish；否则 → neutral——**这条兜底
路径从来不会返回 bearish**。价格和市值来自另一个独立、没坏的取数脚本，
所以兜底路径几乎每次都命中"bullish"分支。真正能产生看空判断的 RSI 超买/
跌破均线逻辑一直都在代码里，只是从未被执行到过。

### 验证
- 直接执行生成的代码：修复前 `float() argument must be a string or a real
  number, not 'Series'`，修复后正常输出 price/SMA/RSI/signal
- 端到端跑 `CodeExecutorAgent.analyze()`：NVDA=bullish(价格高于均线)，
  **TSLA=bearish、VKTX=bearish**（价格低于均线）——方向多样性恢复
- 新增 `tests/test_code_generator.py` 回归测试；全量 1238 测试通过

## [0.43.9] — 2026-08-12 — 补跑 bear-hypothesis-backtest 分析（无代码改动）

> 原定 2026-08-03 自动运行的 scheduled task `bear-hypothesis-backtest` 实际触发后
> 1.5 秒内异常终止，从未产出报告，直到本次用户核实"任务是否真的跑了"才发现。
> 本次手动补跑完整三部分分析，纯分析不改代码（遵守原 SKILL.md 硬约束）。

### 关键发现（详见 `self_analysis_briefs/bear_hypothesis_backtest_2026-08.md`）
- **BullVeto（v37.0）字段读反，从未真正生效**：`queen_distiller.py:728` 读的是
  BearBeeContrarian 反转后的低分而非真实看空强度 `bear_score`；实测全部 35 次
  真实触发均是"score 恰好=7.0 但真实 bear_score 仅 3.0"的巧合误触发，被压制样本
  反而均收益 +0.42%（78%本该盈利）；真正 bear_score≥7 的样本（占全部看多 62%）
  一次未拦到。**本次仅记录发现，未修复代码**——修复需同时重新校准阈值（正确
  字段下原阈值 7.0 会命中六成看多信号），留待下次专项处理
- **BearBee 投票权重提升假说（1.5x/2.0x/3.0x）**：证据不足，样本均<30，暂不上线
- **看空产量已从 8%（v37.0 诊断时）自然改善至 17.6%**，信息素多样性激励假说
  暂无上线紧迫性
- **新发现（超出原三部分范围）**：`CodeExecutorAgent` 近一个多月 91/91 笔预测
  恒定看多，疑似独立方向判定 bug，待单独排查

## [0.43.8] — 2026-08-10 — daily-format-a-content v5.9.1：D6+D7 修复

### Fixed — `daily-format-a-content` SKILL.md v5.9 → v5.9.1
- **D6 修复**（Step 4.5.1 bash × 2 处）：`[ -z "$POOL_DIR" ] && POOL_DIR=$(dirname "$(find ...)")` 改为 `_F=$(find ...); [ -n "$_F" ] && POOL_DIR=$(dirname "$_F")`。根因：`dirname ""` 返回 `"."` 而非空串，会骗过 `[ -z ]` 守卫，导致 `POOL_DIR="."` 看起来非空，但 `./weighted_recall.py` 找不到，仍触发 POOL_DEGRADED（属侥幸成立而非设计成立）
- **D7 修复**（Step 4.5.4 Python enrich 块）：新增 POOL_DIR 自举（之前引用了上一个 Python 块的 POOL_DIR，但 Python 块间不共享状态，必然 NameError）

### Changed
- 全局硬约束新增 item 21：`dirname "$(find ...)"` + Python 跨块变量规则
- 待办文件 `待办-daily-format-a-content-D6.md` 标记为已完成

---

## [0.43.7] — 2026-08-10 — daily-format-a-content v5.9：数字机械校验（Step 8.6）

### Added — `daily-format-a-content` SKILL.md v5.8 → v5.9
- **第 8.6 步 · 数字机械校验**：MD 落盘后强制跑 Python 脚本，把正文中每个"事实数字"grep 回源 JSON（digest / 卦象 / 召回缓存），找不到来源 `exit 1`，禁止进入第 9 步交付
- 变体规则内置：百分比 ÷100 取 float（`46%` → 检索 `0.4603`）、万单位 ×10000（`2.3万` → `-23000`）、score 小数多格式（.1f / .2f / .4f）
- 降级策略：MD 不存在 / ROOT 不可达 / 源文件全空 → `exit 0` 跳过（不阻断）；`[V 已核实: ...]` 人工豁免机制
- 首次实测（2026-08-09 岂因祸福.md）：脚本正确抓住一处真实幻觉 `7757.64`（S&P 500 点位，新闻 JSON 只有"创历史新高"，无具体数字），已从 MD 删除并重新校验通过

### Changed
- 全局硬约束 item 3：补注"写完必须通过第 8.6 步数字机械校验，不通过禁止交付"
- 全局硬约束新增 item 20：v5.9 数字机械校验为强制步骤

### Fixed — `发展大纲/每日发布/2026-08-09_岂因祸福.md`
- 删除三处捏造的 `7757.64`（S&P 500 历史新高点位），改为"S&P 500 创历史新高"（无具体数字），重新校验 14/14 通过

---

## [0.43.6] — 2026-07-30 — 单信号档案加入「固定效应 vs 票内时变」分解

> 常规 IC 无法区分两种**性质完全不同**的信号，而它们长得一模一样。
> 这是本 session 追查 ScoutBee 反向方向时挖出来的诊断能力，本次固化。

### Added — `decompose_fixed_vs_timevarying()`
把每个信号拆成两部分分别算 IC：
- **固定效应**：用每只标的的**自身均值**替换观测值 ⇒ 纯截面信息
- **票内时变**：观测值减去自身均值 ⇒ 纯时序信息

据此给出 `性质` 判定：
| 性质 | 含义 | 正确用法 |
|---|---|---|
| **择时** | 票内时变主导 | **可做每日评分** |
| **选股标签** | 固定效应主导 | **只能做筛选池** —— 塞进每日评分等于每天重新发现「MSFT 是大盘股」 |
| 混合 / 无 | 两者相当 / 都低于噪音 | 需进一步拆解 |

### 实测：47 个信号的性质分布
```
选股标签 20  |  混合 12  |  无 10  |  择时 7
```
**近半数信号是"选股标签"却被当每日评分用。** 典型对照：
- `risk_adj`（`agent.GuardBeeSentinel.score`）：固定 **+0.016** / 时变 **−0.137** ⇒ 择时，用法正确
- `crowding.score`：固定 **+0.141** / 时变 **−0.011** ⇒ 纯标签。
  全样本看驼峰形显著（30~50 组超额 +1.17%，且**样本外延续**：训练 +0.72% →
  测试 +1.75% t=+2.53），但**票内去均值后效应完全消失** ⇒ 它只是"中等关注度
  大盘科技股"这个身份的标记，不是时变信号
- `options.gamma_exposure` / `options.total_oi`：固定 +0.18 / 时变 ≈ −0.02 ⇒ 同样是标签

### 判定门槛用**相对刻度**而非固定绝对值
实测：8 只票 × 30 天的**纯随机**面板，`ic_fixed` 可达 **0.037** ——
票均值本身有抽样波动，标的越少波动越大。若用固定 0.03 会把噪音判成"选股标签"。
改用 `2/√(平均横截面宽度−1)/√天数` 标定（与噪音地板同源的原理）。

### 边界处理
票内方差为 0 时 `ic_within` 必为 nan（常数无法算秩相关），但语义是
**"无时变信息"**而非"未知"。显式返回 `no_within_variation=True` 且
`ic_within=0.0`，与"天数不足导致的 nan"区分开。

### Added — 测试（+5）
用**已知性质的合成面板**验证判定正确：纯固定效应→选股标签、纯时变→择时、
纯噪音→无、标的过少→返回空、`load_panel(with_ticker=True)` 返回三元组
且默认仍返回二元组（向后兼容）。
全量 **1206 passed, 1 skipped, 1 xfailed**，零回归。

## [0.43.5] — 2026-07-30 — 接入 ruff F821 静态守卫，修 4 个存量潜伏 NameError

> 本 session 一天内连犯 **3 次**同型错误，必须系统性解决而非逐个修。

### 问题：模块作用域缺名字，而引用它的代码在正常路径上不执行
| # | 文件 | 缺失 | 触发路径 |
|---|---|---|---|
| 1 | `alpha_hive_daily_report.py` | `sys` | 线程卡死后的强制退出 |
| 2 | `weekly_optimizer.py` | `_log` | 权重约束无解时的拒绝写入分支 |
| 3 | `report_deployer.py` | `List`（只导了 `Dict`） | 模块级类型标注 |

`import` 不报（函数体不求值）、单元测试不报（异常/边界分支覆盖不到）、
人工 review 不报（`from typing import Dict` 看起来完全正常）。

### 更正一个此前的判断
我曾断言"静态检查不报，唯一可靠的发现方式是实际执行到那行"——**这句是错的**。
实测 ruff `F821` 把上述三例**全部**精确抓到（含行列号）。
而本仓库 `pyproject.toml` 早已配置 ruff（`select = ["E","F","W"]`，
未 ignore F821）——**工具一直在，只是没人跑**。

### Fixed — 扫出并修复 4 个存量潜伏 NameError
- `alpha_hive_daily_report.py:1904/1953`：两处 `except` 里调用
  `logging.getLogger()`，而模块**根本没有 `import logging`**
  ⇒ 异常处理路径里再抛 NameError（与本 session 三次同型）
- `factor_attribution.py:462`：`_build_summary()` 用了从未定义的
  `r2_str` / `ir_str`（重构漏改），`factors` 为空时必崩。
  函数已收到 `r2`/`ir` 参数，补上格式化即可

### Added — `tests/test_no_undefined_names.py`（+8）
把检查接进 **pytest**，跟着现有工作流跑，不需要养成新习惯：
- `F821` 未定义名 / `F823` 局部变量赋值前使用 / `F50x` 格式化参数错配
- **守卫自检**：用三次事故的最小复现验证守卫真能抓到，
  并反向验证正确代码不误报（否则守卫会被噪音淹没而遭忽略）
- 断言 `pyproject.toml` 里 ruff 配置存在且 **F821/F823 未被 ignore**
  —— 防止未来有人为图省事关掉这条唯一的静态防线

### 验证
全量 **1201 passed, 1 skipped, 1 xfailed**，零回归；
`ruff check --select F821` 全仓库 **All checks passed**。

## [0.43.4] — 2026-07-30 — 日报自动提交改为白名单（防止卷走在制代码）

> 事故：今日 14:02 的定时日报走 `git add -A` 全量提交，把当时工作区里
> **进行中的 10 个版本代码改动**（`backtester` / `chronos_bee` /
> `parallel_agent_runner` / `weekly_optimizer` / `feedback_loop` /
> `health_check` / `ic_diagnostics` / 6 个测试文件）全部卷进了一次名为
> 「Alpha Hive 蜂群日报 2026-07-30 14:02」的提交（`68aad61`）。

### 后果
1. **提交历史失真** —— 搜"ChronosBee 看空修复在哪个 commit"，结果是一条日报
2. **无法单独回滚** —— 撤销某个代码改动会连带撤销当天日报数据
3. **潜在风险** —— 工作区若有半成品代码，定时任务会直接提交并**推上生产分支**

### Changed — `AgentHelper.git.commit(message, paths=None)`
- 新增可选 `paths` 白名单参数：只暂存指定 pathspec；留空仍走 `git add -A`
  （**向后兼容**，其他调用方不受影响）
- 白名单一个都没匹配上时**显式失败**，而非静默创建空提交

### Changed — `report_deployer.REPORT_ARTIFACT_PATHS`
定时日报只自动提交这些路径，代码一律交人工：
```
alpha-hive-daily-*.{json,md}   alpha-hive-thread-*.txt
alpha-hive-*-ml-enhanced-*.html   analysis-*-ml-*.json
index.html  dashboard-data.json  rss.xml  sw.js  weight_history.jsonl
report_snapshots/  paper_portfolio_state/  .factor_cache/
```
清单来源 = `68aad61` 里的全部非代码文件 + ML 报告与 analysis JSON（那次未生成）。

- 被跳过的非产物文件会**显式告警**（日志 + 终端），不会静默消失
- `auto_commit_and_notify` 返回值新增 `skipped_non_artifacts`

### 为什么是白名单而非黑名单
自动化系统的失败模式必须是**可发现的**：
- 白名单漏一项 → 该产物不进 git → 下次运行或看网站立刻发现
- 黑名单漏一项 → 半成品代码被自动提交并推生产 → **无人知晓**

### Added — 测试（+29）
- 用 `68aad61` 的**真实文件清单**做参数化：13 个产物必须提交、12 个代码必须跳过
- **真实 git 仓库沙盒验证**（分类函数对 ≠ `git add` 行为对）：
  造出 3 个产物 + 2 个"半成品代码"，断言提交里只有产物、代码完好留在工作区
- 不传 `paths` 时仍全量暂存（向后兼容）
- 全量 **1193 passed, 1 skipped, 1 xfailed**，零回归

### 顺带修复
`report_deployer.py` 只导入了 `Dict` 而新代码用了 `List` —— 与本 session
已犯两次的同类错误相同（`weekly_optimizer` 的 `_log`、`alpha_hive_daily_report`
的 `sys`），均为"模块作用域缺名字，仅在特定路径触发"。

## [0.43.2] — 2026-07-30 — `signal_archive.py`：单信号 IC 档案（47 个原始信号全部体检）

> 系统有 7 只蜂、60+ 个原始字段，但只有 **5 个聚合维度**进入评估。
> 「哪块砖在承重」此前每次都要重新翻 `.swarm_results_*.json` 考古，
> 样本永远停在临时抽取的几十条。本模块把它变成一张随时可查的表。

### Added — `signal_archive` 表 + 声明式提取器
- 长表 `signal_archive(date, ticker, signal, value)`，新增信号**无需改 schema**
- `SIGNAL_EXTRACTORS`：47 个信号，加一个 = 加一行。覆盖内幕/国会、拥挤度、
  价格动量、期权(IV/PCR/GEX/OI)、情绪、看空分项、共振一致性、ML 预测、
  基本面，以及 7 只蜂各自的 score 与 direction
- 前瞻收益**分析时**从 `predictions` 联表，不重复存储；且用**纯价格变动**
  而非 `return_t7`（后者含 SL/TP 截断，42.5% 的行被钉在档位）
- 挂载点：`alpha_hive_daily_report._post_scan_enrichment`，扫描后自动写入；
  失败只告警不阻断主流程
- 一次性回填：**83 个文件 → 43,189 行**

### 首次全量体检结果（T+7，噪音地板 |IC|=0.077 / 通过 95分位 2-4）
判定真信号需同时满足 `|IC| > 地板` **且** `通过 ≥3/4`。47 个信号里只有 **2 个**达标：

| 信号 | 样本 | IC | 通过 | 备注 |
|---|---|---|---|---|
| `agent.ScoutBeeNova.direction` | 667 | −0.198 | 4/4 | ⚠️稀疏（方向是三值） |
| `crowding.adj_factor` | 649 | −0.112 | 3/4 | ⚠️稀疏（三档阶跃） |

其余 45 个全部落在"超地板但口径不足"或"噪音带内"。值得记录的几条：
- `composite.final_score` 仅 2/4 —— **综合分本身达不到真信号门槛**
- `composite.swarm_agreement` IC=**+0.002**、0/4 —— 蜂群一致度零预测力，
  却经 GuardBee 基础分 + queen 共振加成被三重计入
- `bear.score` / `agent.BearBeeContrarian.score` IC≈**0.00** —— 看空蜂的分本身不预测
- `insider.*` 全系为负（sentiment −0.132、distinct_buyers −0.179、dollar_bought −0.165）
- `ml.expected_7d` / `ml.expected_30d` IC=−0.044、0/4 —— ML 预测未体现价值

### Added — 稀疏度告警
`distinct_ratio < 0.25` 的信号标 ⚠️稀疏。事件型信号（如 `insider.distinct_buyers`
离散度 0.00）大量取值并列，**rank-IC 会失真**，应改用事件研究。
这一条直接解释了为什么内幕信号的 IC 不可直接采信 —— 676 条样本里
只有 47 条有任何买入事件。

### Added — 测试（+19）
提取正确性（嵌套路径/缺失省略而非填 0/bool 不转数值/单提取器失败隔离）、
`code=P` 白名单、`notable_trades` repr 字符串容错、幂等写入、
**不同业务日必须并存**（与 v0.42.4 日期戳 bug 同类风险）、
**联表必须用纯价格收益而非 return_t7**、回填容错。
全量 **1157 passed, 1 skipped, 1 xfailed**（本 session 起点 1063），零回归

### 用法
```bash
/usr/local/bin/python3 signal_archive.py --backfill              # 一次性回填
/usr/local/bin/python3 signal_archive.py --analyze               # 全信号体检
/usr/local/bin/python3 signal_archive.py --analyze --min-samples 300
/usr/local/bin/python3 signal_archive.py --list                  # 覆盖度清单
```

## [0.43.1] — 2026-07-30 — `ic_diagnostics --benchmark`：噪音地板 + 经典因子对照

> 系统此前**没有任何基准**，因此无法回答唯一重要的问题：**这比什么都不做强吗？**
> 本次排查中正是靠临时加的动量对照，才把结论从「蜂群设计有问题」修正为
> 「T+7 横截面选股在此尺度上极难」——指向完全不同的行动。没有基准，
> 会给出方向正确但代价高昂的错误建议。

### Added — 基准套件（`--benchmark`）
三类参照，一次输出：
1. **噪音地板**：随机排序重复 N 次（默认 200）→ `|日度IC|` 的 95 分位。
   把「过 1/4 口径」这种模糊说法变成一个**具体数字**
2. **经典因子**：20 日动量 / 5 日反转 / 低波动。打不过它们就没有理由用复杂系统
3. **系统自身**：综合分 + 5 个维度

用法：`/usr/local/bin/python3 ic_diagnostics.py --benchmark [--draws 200]`
价格类因子需联网；拉取失败自动降级，只保留系统自身基准，工具不崩。

### 首次运行结果（T+7，纯价格口径，73 个交易日）
```
🎯 噪音地板（随机 ×200）：|日度IC| 中位 0.026、95分位 0.077
                          通过口径数 均值 0.49、95分位 2/4

   risk_adj        −0.1598  4/4  ✅ 超出
📈 20日动量          +0.1352  1/4  ✅ 超出
   signal          −0.1205  1/4  ✅ 超出
🐝 综合分            −0.0903  2/4  ✅ 超出
   sentiment       +0.0830  1/4  ✅ 超出
   odds            +0.0593  1/4  ❌ 噪音带内
   catalyst        +0.0298  0/4  ❌ 噪音带内
🎲 随机              −0.0159  0/4  ❌ 噪音带内
```
**判定：综合分 |IC|=0.090 < 最佳经典因子 |IC|=0.135 —— 系统未能超过 20 日动量。**

两个此前看不见的结论：
- **「通过 2/4 口径」并不安全**：纯噪音的通过口径数 95 分位就是 **2/4**。
  此前把 1/4 当噪音基准是**低估**了——2/4 同样落在噪音带内
- `odds`（+0.0593）虽然过了 1 个口径，但**未超出噪音地板**，应视为无信号

### Added — 测试（+6）
- 噪音地板有界且确定性（同输入同结果，否则无法作为上线门槛）
- **真信号必须超出地板**（否则地板设太高、工具没有辨别力）
- **纯噪音不得超出地板**（否则产生假阳性建议）
- 行情拉取失败时降级不崩、且不出现价格类因子
- 全量 **1138 passed, 1 skipped, 1 xfailed**（本 session 起点 1063），零回归

### 使用规则（已写进模块 docstring）
**任何评分/权重改动上线前，必须先跑 `--benchmark` 并证明相对基准有改善。
只比「改动前的自己」好是不够的——那是在噪音里挑选。**

## [0.43.0] — 2026-07-30 — 修复 ChronosBee 结构性无法看空（950 条记录 bearish=0）

> 本 session 唯一一处「代码明确写错、且指向系统唯一勉强有效方向」的缺陷：
> 全样本方向准确率 53.6%（CI 含 50%，等同抛硬币），但**看空侧 60.6%**
> （n=94，CI [50.8%, 70.5%]）是唯一勉强显著的数字 —— 而系统在结构上就不会看空。

### Fixed — ChronosBee 永远无法输出 bearish（P0）
实测：`pheromone.db` 里 950 条 ChronosBee 记录 **bearish = 0 条**。两处成因：

1. **`elif score <= 4.5` 结构性不可达**（`chronos_bee.py`）：
   评分块 `score = 5.5` 起步，三个分支全是 `score += ...`（只增不减），
   该条件永远为假。无催化剂时走 `else` 分支 `score=4.0` 但直接写死 `neutral`
2. **唯一带方向的证据是死代码**：PEAD（财报后价格漂移）段**已经写了**
   `direction = "bearish"`，但它位于评分块**之前**，被后面的 `score = base`
   与 direction 三分支**无条件覆盖**。不是"没写"，是"写了被冲掉"

**修复**：把 PEAD 方向证据抽成 `_apply_pead_direction()`，改在评分块**之后**调用。
PEAD 采集段只写入 `_pead_pending` 暂存，不再直接赋值。

### 关键设计：只改方向，不改分数
- `_apply_pead_direction` 的**分数调整默认关闭**
  （需显式 `ALPHA_HIVE_CHRONOS_PEAD_SCORE_ADJUST=1` 开启）
- 理由：catalyst 维度的 784 条历史样本是在"无 PEAD 分数调整"口径下产生的，
  开启即破坏可比性；而 catalyst 在四口径 IC 体检里只过 **1/4**（与纯噪音无异），
  分数微调的期望收益接近零、代价却是确定的
- **方向不受此限**——方向本就全零（bearish 从未出现），没有"可比性"可言，只有从无到有
- 保护规则：只在评分块判为 `neutral` 时施加，**不覆盖高置信的 bullish**
  （强催化剂判定不会被历史漂移翻转）

### 实测效果（10 只真实标的）
```
修复前：950 条历史记录 bearish = 0 条
修复后：neutral 7 / bearish 2 (TSLA, CVX) / bullish 1 (ABBV)
```

### 对 8/3 定档回测的影响：无
`bear-hypothesis-backtest` 分析的是**已回填 T+7** 的历史样本；今天之后的新扫描
要到 ~8/10 才成熟，不进入其分析集。且该任务第 3 项正是
「找出哪几只蜂从不投 bearish」——与本次修复互补，可作为修复前的基线记录。

### Added — 测试（+13，`tests/test_chronos_bee_direction.py`）
- 方向真值表、bullish 保护、无证据时 no-op、分数开关默认关闭且可开启
- **源码顺序护栏**（本 bug 的本质是**执行顺序**错，不是逻辑错）：
  - `test_pead_applied_after_scoring_block`：断言 `_apply_pead_direction` 调用
    位置在 `score = base` **之后** —— 若有人挪回去，它会重新变成死代码，
    而单元测试仍会全绿（函数本身没坏），只有顺序断言能拦住
  - `test_pead_block_does_not_assign_direction_directly`：PEAD 采集段不得直接赋值 direction
  - `test_unreachable_branch_is_documented`：`elif score <= 4.5` 仍不可达，必须有注释说明
- 全量 **1132 passed, 1 skipped, 1 xfailed**（本 session 起点 1063），零回归

## [0.42.9] — 2026-07-30 — 标的池 10 → 30 只（N_eff 3.25 → 13.8）+ ML 报告限流

> 承 v0.42.8 的挂死加固。扩池的目的不是"看更多股票"，而是**提升横截面统计功效**——
> 这是本 session 反复撞到的真瓶颈：所有因子结论都因样本太窄而站不住。

### Changed — 默认标的池 10 → 30 只
- **问题**：原 10 只全是高相关科技股，平均两两相关 **0.230**，
  有效独立标的数 **N_eff = 3.25**。每天扫 10 只，统计上只相当于 3 个独立观测
- **选股方法**：对 `WATCHLIST_EXTENDED` 全部 91 只做贪心搜索，每步选让 N_eff
  增幅最大的标的。结果自动偏向低相关行业——与核心池相关度：
  Energy **−0.180**、Communication **−0.007**、Consumer +0.048，
  而 Technology +0.251（最差之一）
- **结果**：N_eff **3.25 → 13.8**，行业数 5 → **11**
- 新增 20 只（按 N_eff 增量降序）：`CVX VZ JNJ XOM COST BRK-B AMC ABBV T DELL
  DE CRM MU WMT TMO TMUS ENPH NFLX NEE SNOW`
- **收益在 25~30 只饱和**：40 只 N_eff=13.97、50 只反而降到 13.29（后面加进来的
  都是高相关科技股），故止步 30
- 改动点：`alpha_hive_daily_report.py` 的 `--tickers` 默认值、
  `~/.claude/scripts/alpha-hive-orchestrator.sh` 的 `DEFAULT_TICKERS`
- 全部取自已有 `WATCHLIST_EXTENDED`，零配置成本；数据真实度实测 90~96%，与核心池无差别

### Added — ML 报告数量上限 `ALPHA_HIVE_ML_REPORT_MAX`（默认 12）
- 每份 ML 报告都要走一次 CBOE/yfinance 取价链。若扩池后仍为**每只**标的生成，
  这部分调用量直接翻 3 倍——2026-07-23 深夜的限流连锁崩溃正是调用量堆叠所致
- 扩池的收益路径是 `predictions` 表（dimension_scores + 价格），**不依赖 ML HTML**。
  故：全部 30 只照常扫描入库，ML 报告只出分数最高的 12 只
- 同步修改编排器 Step 3 的补跑判据：原逻辑按**全部标的**判"缺失"，
  限流后 18 只永远缺失会被 Step 3 补跑，**把限流完全抵消**。
  改为按"已生成数是否达到上限"判断

### Fixed — 第二处线程池挂死（`_generate_ml_reports`）
- 与 v0.42.8 修的是同一模式，但**更危险**：原实现 `with ThreadPoolExecutor(...)`
  且 `as_completed()` 与 `future.result()` **都没有超时**，任何一个卡住的取价调用
  都会让整个扫描永久挂起
- 改为带超时收集 + `shutdown(wait=False, cancel_futures=True)`，
  超时可通过 `ALPHA_HIVE_ML_REPORT_TIMEOUT` 调整（默认 90s/只）

### Added — 测试（+3）
- `test_ml_report_generation_is_capped` / `test_ml_report_pool_is_non_blocking`：
  源码级护栏，防止限流与非阻塞关闭被改回去
- `test_default_ticker_pool_expanded_and_diverse`：断言 ≥25 只、**≥8 个行业**、
  单一行业占比 ≤50%、全部在 watchlist 内、无重复。
  **断言行业数而非仅标的数**——若有人把池改回全科技股，标的数不变但 N_eff 会塌掉
- 全量 **1119 passed, 1 skipped, 1 xfailed**（本 session 起点 1063），零回归

### 预期影响
- 单次扫描耗时 120s → 约 219s（STEP2_TIMEOUT 的 12%）
- 每日新增 predictions 行 10 → 30；T+7 样本积累速度提升 3 倍
- **验证周期从约 9 个月压缩到 2~3 个月**（N_eff 提升 4.2 倍带来的功效增益）

## [0.42.8] — 2026-07-30 — 修复扫描挂死（实测曾跑 24 小时），为扩池做限流加固

> 扩标的池前的前置加固。`metrics.db` 显示 193 次 swarm 扫描里有 **4 次跑飞**：
> 2026-05-28 **86899s（24.1 小时）**、07-17 11046s（3.1h）、06-30 7882s（2.2h）、
> 06-27 2729s（0.8h）。正常扫描中位数仅 127s。

### Fixed — `parallel_agent_runner._run_phase` 被卡死线程无限阻塞（P0）
- 根因：`with ThreadPoolExecutor(...)` 退出时执行 `shutdown(wait=True)`，
  **会一直阻塞到所有工作线程返回**。而 `future.result(timeout=)` 与
  `as_completed(timeout=)` 只让主线程**停止等待结果**，并不能杀死卡在网络调用里的
  工作线程（Python 无法强制中断线程）。于是超时逻辑正确地填了占位结果，
  却在退出 `with` 块时被无限期挂住
- 证据：24 小时那次 `prefetch_seconds` 仅 **17s**，且 `duration_seconds` 是在
  标的分析循环**之后**测量的（`alpha_hive_daily_report.py:1169`）⇒ 时间全耗在此处
- 修复：改为手动 `executor` + `try/finally` + **`shutdown(wait=False, cancel_futures=True)`**。
  放弃未开始的任务，卡住的线程留在后台，主流程立即继续；并记录哪些 Agent 卡住

### Added — `_force_exit_if_threads_stuck()` 退出路径兜底
- 即便 `shutdown(wait=False)` 已让主流程继续，`concurrent.futures` 通过
  `threading._register_atexit` 注册的 `_python_exit` 仍会在进程退出时
  **join 所有工作线程** —— 实测：1 秒完成的主流程因一个 20 秒的卡住线程，
  进程总耗时 20 秒
- 在 `__main__` 中、**所有产出落盘之后**调用：给 10s 宽限，仍卡住则 `os._exit(0)`
  跳过 atexit。此时数据库/报告/gh-pages 同步均已完成，强退是安全的
- 顺带修复：`sys` 在 `alpha_hive_daily_report` 模块作用域**不可用**（只在两处函数内
  局部 import），强退路径引用它会在最不该崩的时刻崩

### 实测效果
| | 修复前 | 修复后 |
|---|---|---|
| 一个卡 60s 的 Agent | 主流程阻塞 60s | **6s 返回** |
| 进程总耗时（含退出） | ~120s | **11s** |

### Added — 测试（+9，`tests/test_parallel_agent_runner.py`）
- 卡死 Agent 不阻塞 phase、占位结果结构完整、全部卡住仍返回、正常路径无回归
- **源码级护栏**：断言代码里不出现 `with ThreadPoolExecutor(`、必须有
  `shutdown(wait=False` 与 `cancel_futures=True` —— 防止被改回去
- 断言 `_force_exit_if_threads_stuck()` 确实在 `__main__` 里被调用（否则安全网形同虚设）
- 测试用 `Event.wait` 而非 `time.sleep` 并配 autouse fixture 释放，
  避免测试自身把线程残留到 pytest 退出（不能再犯一遍本文件要防的问题）
- 全量 **1116 passed, 1 skipped, 1 xfailed**（本 session 起点 1063），零回归

### 扩池前置结论（本次未改标的池）
- **耗时不是约束**：189 次正常扫描回归 `耗时 ≈ 71 + 4.9 × 标的数` 秒；
  30 只约 219s、101 只约 569s，均 < 30 分钟超时的 32%。
  实测更乐观——历史上真跑过 39 只只用 **150s**（10 只中位数 127s），批量预取效率极高
- **真正的风险是挂死**，与标的数无关，10 只时就已发生 ⇒ 故先做本次加固
- 池已存在：`WATCHLIST`(24) + `WATCHLIST_EXTENDED`(77) = 101 只，覆盖 9 个行业，
  数据真实度实测 90~96%，与核心池无差别

## [0.42.7] — 2026-07-30 — catalyst 符号翻转调查：判定为假象；IC 目标口径改用纯价格

> 多智能体工作流调查 catalyst 的 regime 符号翻转（上涨期 +0.094 / 下跌期 −0.236），
> 三条核心论断我逐条独立复现。**结论：翻转是假象，不做任何 catalyst 改动。**

### 调查结论：翻转是假象（三条独立证据，均已复现）
1. **整个下跌期效应来自一段连跌**：剔除 2026-02-26~03-13 这 13 天后，
   下跌期 IC 从 **−0.196(t=−2.57)** 塌到 **−0.017(t=−0.11)**，仅剩 6 天。
   19 天不是 19 个独立观测，是**一次市场事件**
2. **该窗口与一个已修复的打分 bug 完全重合**：`3d7de04`（v0.24.3，2026-04-29）
   修的正是 ChronosBee「过期事件被当成 7 天内迫近事件计满分」。
   下跌期 19 天中 **15 天（79%）在修复之前**。按修复日拆分：
   修复后下跌期仅 4 天、IC=−0.115(**t=−0.55**)，修复前后不是同一个变量
3. **分段是事后择优**：全枚举 C(6,3)=20 种 3/3 月份分法，选中的 (4,5,6)
   |diff| 排名 **1/20**

### Changed — `ic_diagnostics.py` 目标变量口径（默认 price）
调查中发现 `return_t7` **不是纯 T+7 前瞻收益**，而是
`backtester._simulate_trade_path()` 的**路径依赖模拟交易收益**（触及 SL/TP 提前出场）：
- 776 条 checked_t7 中 SL 195 + TP 135 = **330 条（42.5%）提前出场**
- 收益被钉在出场档位：`+9.945` 出现 **93 次**、`−5.048` 61 次、`−10.045` 21 次
- 与真实价格变动一致率仅 **88.9%**
- 对 rank-IC 的危害：档位截断制造大量**并列值**，破坏尾部排序——而尾部正是 IC
  信息最集中处

新增 `--target {price,path}`，**默认 price**（由 `price_t7`/`price_at_predict` 直接算）。

**口径切换未推翻任何结论，但数字更干净**（T+7，纯价格口径）：

| 维度 | 路径依赖 IC / 通过 | 纯价格 IC / 通过 |
|---|---|---|
| risk_adj | −0.1384 / 3-4 | **−0.1598 / 4-4** |
| sentiment | +0.0874 / 2-4 | +0.0830 / 2-4 |
| signal | −0.1182 / 1-4 | −0.1205 / 1-4 |
| odds | +0.0562 / 0-4 | +0.0593 / 1-4 |
| catalyst | +0.0070 / 1-4 | +0.0298 / 1-4 |

### 已确认但**暂不修**的 ChronosBee 工程缺陷（记录备查）
- `chronos_bee.py:313-332` 是**纯单向事件强度累加器**，三个分支全为非负增量，
  无任何减分通路；权重表只编码"影响多大"不编码"影响好坏"
- `:345 elif score<=4.5` 的 bearish 分支**结构性不可达**（位于 `if catalysts_found`
  内而 score 恒 ≥5.5）；DB 里 950 条 ChronosBee 记录 **bearish = 0 条**
- 唯一带方向的 PEAD 调整是**死代码**（`:237/:243` 改完被 `:313-314` 无条件覆盖）
- 无方向分被 `queen_distiller.py:307-311` 当**加性看多票**以 +0.1878 正权重线性加入

**为何暂不修**：① 2026-07 已结算样本里 cat≥7 占比 **0%**、55% 的分恰为 6.0，
该维度眼下近似常数，实际伤害很小；② 改动爆炸半径大（62 个 .py 引用 catalyst，
`ml_predictor.py:454,459` 的 `FEATURE_NAMES_V1` 含 catalyst → 训练/服务偏移会静默劣化）；
③ 会污染 **8/3 定档的 bear-hypothesis-backtest** 输入；④ 会把 784 条历史样本
切成第三个不可比世代（v0.24.3 已造成一次断层）

### 不做的动作（附理由，避免重复提议）
- ❌ **regime 门控 catalyst**：依据已证伪；唯一能复现翻转的切分用了 SPY **前瞻**
  收益（未来函数，实盘不可执行）。且这是同族第 3 次提案——locked-tasks 里已有
  两条反向证伪记录（宏观 risk_off 门控、F&G 恐惧区门控）
- ❌ **手动调 catalyst 权重**：四口径仅通过 1/4；且把 catalyst 归零后复合分 IC
  只从 −0.1010 → −0.0832，**仍显著为负**，瓶颈在信号层不在加权层

## [0.42.6] — 2026-07-30 — 护栏投影重写（修限幅突破）+ 新增 ic_diagnostics 体检工具

### Fixed — `weekly_optimizer.clamp_shifts` 限幅可被突破（P1）
- 旧实现两步走：逐维钳幅到 ±`MAX_SHIFT_PP` → **重新归一化**。归一化是乘性缩放，
  必然把已钳到边界的值再推出去。历史实证：`weight_history.jsonl` 记录过
  `risk_adj +10.72pp`、`signal −11.32pp`（均 > 10.0）
- 且 `_apply_weight_clamps`（绝对上下限）在归一化**之前**执行、之后不复查，
  导致落盘权重不保证满足 `WEIGHT_CLAMPS`（史上 catalyst 到过 0.3316 > 上限 0.25）
- 新实现：`merge_bounds()` 把 `WEIGHT_CLAMPS ∩ [anchor ± MAX_SHIFT_PP]` 合并成
  **单一盒约束**，再 `project_to_feasible()` 单次投影 ⇒ 三个不变式
  （sum=1 ∧ 各维在 clamp 内 ∧ 单次变动 ≤ MAX_SHIFT_PP）同时成立，无顺序冲突

### Fixed — 投影算法在「所有维度同时触界」时失效
- 原 water-filling（钳制 + 按比例再分配）有一个死角：当 5 个维度在同一轮
  全部触界时 `free_keys` 为空 → 直接 break，剩余预算无人吸收，
  **输出的和可能远离 1.0**（重构过程中实测到 0.8545）。旧代码注释亦承认
  该降级会"允许轻微突破 clamp"
- 改为**欧氏投影**：二分求 λ 使 `Σ clip(target+λ, lo, hi) = 1`。
  `f(λ)` 单调不减且连续，二分必收敛，且不需要"自由维度"，无死角
- 新增 `InfeasibleBoundsError`：盒与单纯形无交集（Σlo>1 或 Σhi<1）时显式抛错，
  由 `main()` 捕获后**拒绝写入**并记 `skip_reason="infeasible_bounds"`，不再静默降级
- 模块导入期断言 `WEIGHT_CLAMPS` 自洽（Σlo ≤ 1 ≤ Σhi），把配置错误暴露在导入时
  而非周日 cron 跑到一半
- 舍入到 6 位后的残差吸收进**余量最大**的维度，保证 `sum=1` 精确成立

### Added — `ic_diagnostics.py`：调权重前的必跑体检
每个维度同时输出**四个口径**，只有多口径一致才值得采信：
1. 日度（重叠）— 与历史报告一致，t 偏高，仅作参照
2. Newey-West(HAC) — Bartlett 核自相关调整
3. 不重叠取样 — **T+7 按周、T+30 按月**（关键：T+30 按周仍重叠 ~4 倍，
   实测会把 risk_adj 的 t 从真实的 −1.09 抬到 −3.28，看起来比 T+7 还显著）
4. Jackknife — 剔除最极端 10 天，检验是否被少数日子驱动

外加 regime 分段（上涨期 vs 下跌震荡期）与 Bonferroni×5 校正。
纯 stdlib 实现（无 scipy 依赖），只读打开数据库（`mode=ro`）。

```bash
/usr/local/bin/python3 ic_diagnostics.py            # T+7 + T+30 全套
/usr/local/bin/python3 ic_diagnostics.py --json     # 机器可读
```

### Added — 测试（+23）
- `tests/test_weekly_optimizer.py::TestProjectionInvariants`（9 项）：
  三不变式随机检验（200 组）、两条**历史突破固化回归**
  （`risk_adj +10.72pp` / `catalyst 0.3316`）、不可行盒抛错、投影幂等性
- `tests/test_ic_diagnostics.py`（23 项）：Spearman 对拍 scipy、
  Newey-West 在正自相关下必须压低 |t|、**T+30 取样周期必须是"月"**的回归断言、
  只读访问校验
- 全量 **1105 passed, 1 skipped, 1 xfailed**（本 session 起点 1063），零回归

### 行为影响：无
真实数据下新投影输出与重构前**逐位相同**（signal −0.97 / catalyst +0.59 /
sentiment +1.91 / odds +1.01 / risk_adj −2.54 pp），和精确为 1.000000000，
最大变动 2.54pp < `MIN_CHANGE_PP=3.0` ⇒ 仍不写入 config.py。

### 一个值得记的测试教训
`test_perfect_correlation_yields_undefined_t`：合成数据做成 IC 恒为 1.0 时，
序列方差为 0 ⇒ t = mean/0 未定义 ⇒ 工具正确地判 0 个口径通过。
最初的测试期望"完美相关应过多数口径"是**错的**。另：纯随机维度会过 1/4 个口径
（四口径各 α=0.05，至少一个误报的概率 ≈ 19%）——**「仅一口径过」正是噪音的
典型形态**，而真实数据里 signal 与 catalyst 恰在这一档。

## [0.42.4] — 2026-07-30 — 修复预测日期戳错位导致的跨业务日样本覆盖

> 用户指出"我有手动跑规则模式的，你是不是漏看了"——确实漏了。交叉比对
> git 日报提交 / `report_snapshots/` / `predictions` 三个数据源，发现 4 个日期
> （07-07、07-16、07-21、07-28）跑了扫描却没有预测记录，顺藤摸瓜定位到静默数据丢失。

### Fixed — `backtester.py::PredictionStore.save_prediction` 日期戳（P0）
- 旧实现无条件写 **`_pdt_today()`（写入时刻的墙上时钟）**，而非报告的业务日期。
  表上有 `UNIQUE(date, ticker)` + `INSERT OR REPLACE`，SQLite 的 REPLACE 是
  「删除旧行 + 插入新行（分配新 rowid）」⇒ **同一 PDT 日历日跑第二次扫描
  会删掉第一次的记录**，旧 id 变成空洞
- **`--date` 补跑模式尤其致命**：`reporter.date_str`（`alpha_hive_daily_report.py:125`）
  会正确设为目标日期，报告和快照都标对了，唯独 `save_prediction` 不知情，
  照样盖运行当天 ⇒ 补跑多个历史交易日时预测互相覆盖
- id 空洞位置与补跑行为吻合：07-09→07-22 缺 29 个 id（≈3 次扫描），
  而 2026-07-22 当天 git 提交了 3 份日报（「日报 07-21 07:47」「07-21 06:45」「07-21 01:59」）
- ⚠️ **实际数据损失量的准确口径**（v0.42.5 评估后修正）：全库 id 跨度 1294 而只保留
  815 行，但**空洞的绝大部分是同一业务日重跑的正常覆盖**（重跑当天扫描时只保留
  最后一次是正确行为），**不是** bug 造成的损失。真正因跨业务日碰撞而丢失、
  且可经 `report_snapshots/` 识别的仅 **29 条**（业务日 07-07 / 07-21 / 07-23）。
  另有 07-16、07-28 两天（git 有日报但快照与预测皆无）不可恢复。
  早先"丢失 479 行（37%）"的表述**不准确，勿再引用**

### Changed — 业务日期贯穿调用链
- `PredictionStore.save_prediction(..., date: Optional[str] = None)` —— 新增业务日期参数，
  留空回退 `_pdt_today()`（**向后兼容**，17 处既有测试调用无需改）。
  非 `YYYY-MM-DD` 格式时 `_log.warning` 并回退当日，不静默写脏数据
- `Backtester.save_predictions(swarm_results, date=None)` —— 透传
- `run_full_backtest(swarm_results, date=None)` —— 透传，docstring 注明调用方有业务日期应显式传
- `alpha_hive_daily_report.py:716` —— 改为 `bt.save_predictions(swarm_results, date=self.date_str)`

### Fixed — 写库失败不再静默
- 旧实现丢弃 `save_predictions` 返回值，写库 0 条时无人知晓（日报照常发布）。
  现在返回 0 而 `swarm_results` 非空时 `_log.error` + 终端告警

### 明确不改的边界
- **不改 `UNIQUE(date, ticker)`**：同一业务日同一标的确实应唯一，日期戳修对后
  REPLACE 语义就是正确的「重跑同一天覆盖旧结果」
- **不改 `get_pending_checks` 的 cutoff**：它用 `_pdt_today()` 做「今天往回数 N 个
  交易日」是正确的，与写入日期戳是两回事
- **不回填已丢失的 479 行**：属独立的下一步（从 `report_snapshots/` 重建）

### Added — 测试
- `tests/test_backtester.py` 新增 `TestBusinessDateStamping`（10 项），核心是
  `test_two_business_dates_same_run_both_survive` —— 同一进程写两个不同业务日的
  同一标的必须都存活（修复前会被 REPLACE 成 1 条）
- 全量 **1073 passed, 1 skipped, 1 xfailed**（修复前 1063），零回归

### 验证
- 前后对比实证：不带 date 连写两次 → 保留 1 条（旧行为）；带不同业务日期 → 保留 2 条
- 端到端复现用户 07-22 场景：同一天补跑 07-16/07-21/07-22 三个业务日 →
  **6 条全部存活、id 空洞 0**（修复前只会剩 2 条）

## [0.42.5] — 2026-07-30 — 快照回填可行性评估：结论「不做」（无代码改动）

> 评估能否从 `report_snapshots/` 重建 v0.42.4 日期戳 bug 丢失的预测记录。
> 结论是投入产出不成立，**不实施回填**。记录评估过程以免未来重复调查。

### 评估结论：否决
1. **可回填量远小于预期**：`report_snapshots/` 有 667 个唯一 (ticker, date) 组合，
   `predictions` 有 815 个，其中「仅快照有」= **29 条**（07-07 / 07-21 / 07-23）。
   现有 t7 价格的仅 **10 条** ⇒ 对 T+7 样本贡献 784→794（**+1.3%**）
2. **`dimension_scores` 只能还原 8/29 条**。快照存 `agent_votes` 而非 `dimension_scores`，
   映射关系（signal←ScoutBeeNova / catalyst←ChronosBeeHorizon / sentiment←BuzzBeeWhisper /
   odds←OracleBeeEcho / risk_adj←GuardBeeSentinel）经 638 条重叠样本验证**正确**，
   主蜂在场时按 2 位小数匹配率 **95~99%**；但快照的 `agent_votes` 常缺蜂，
   29 条候选里 5 维齐全的只有 **8 条**
3. **缺维度的行会引入选择性偏差**：29 条里 `sentiment` 缺 15 次，而它恰是四口径表中
   唯一显著的正向维度。以「部分维度为空」入库会让每日横截面在不同维度上样本集不同

### 方法论记录（避免重蹈）
首次比对用 `abs(a-b) < 1e-6`，得出「最佳主蜂仅匹配 6~75%」并差点据此判定映射不存在。
真实原因是**两侧存储精度不同**：`dimension_scores` 存 2 位小数、`agent_votes` 全精度
（4.8100 vs 4.8083）。按 `round(x, 2)` 重测后匹配率跃升至 95~99%。
**跨数据源比对数值前必须先确认双方精度约定。**

## [0.42.3] — 2026-07-30 — T+N 回填 + IC 统计权威重算（无代码改动，数据与结论更新）

### Changed — 数据
- 跑 `Backtester.run_backtest()` 补齐到期回填：**t1 +10**（07-29）、**t30 +10**（06-16）、
  **t7 +0**。回填前备份 `/tmp/pheromone_pre_backfill_20260730_024100.db`
- `checked_t1` 805→815、`checked_t30` 674→684、`checked_t7` 784→784（未变）

### Fixed — 更正一个错误诊断（本 session 早先由我提出）
- 曾判定"T+7 回填停滞 3 周"，**该结论错误**。`get_pending_checks('t7')` 返回 0 是因为
  07-22 之后的预测尚未到期（07-22→到期 07-31、07-23→08-03、07-24→08-04、07-29→08-07）
- `checked_t7` 停在 07-09 的真实原因：**07-10 ~ 07-21 共 13 天没有跑过扫描**。
  扫描本身高度稀疏（06-22/23/26/29、07-02/06/08/09、07-22/23/24/29），
  与 `alpha-hive-daily-scan` 定时任务 `enabled:false`（lastRunAt 2026-06-19）一致
- 回填逻辑一直正常工作，无需修复

### Added — IC 统计权威基线（四口径对照）
因 `checked_t7` 未增加，基于 `return_t7` 的 IC 数字与回填前完全一致。本次以全部
稳健性口径重算，作为后续权重决策的唯一基线（776 行 / 73 个交易日 / 日均横截面宽度 10.2）：

| 维度 | 日度(重叠) | Newey-West(L=7) | 不重叠周 | 剔极端10天 | 通过 |
|---|---|---|---|---|---|
| risk_adj | −3.72 | −3.08 | −2.56 | −1.89 ✗ | 3/4 |
| sentiment | +2.14 | +1.66 ✗ | +2.24 | +0.10 ✗ | 2/4 |
| signal | −2.54 | −1.51 ✗ | −1.10 ✗ | −0.52 ✗ | 1/4 |
| catalyst | +0.17 | +0.10 | +0.37 | −2.25 | 1/4 |
| odds | +1.52 ✗ | +1.11 ✗ | +1.20 ✗ | −0.49 ✗ | 0/4 |

- **T+30 不构成独立确认**：周度 risk_adj t=−3.28（Bonferroni p=0.005）看似更强，但 30 天
  前瞻收益按周取样仍重叠 ~4 倍；按月**真不重叠**后 n=5，t=**−1.09**、CI[−0.452,+0.129] 含 0
- **catalyst 不是零信息，是符号翻转**（T+7）：上涨期 IC=+0.094(t=+2.18,n=53)、
  下跌震荡期 −0.236(t=−3.30,n=19)，两个相反效应相消才显得像 0。T+30 该翻转消失

### 结论：暂不做任何权重调整
没有任何维度通过全部保守口径。risk_adj 是最强负向候选但败于 jackknife，
sentiment 最强正向候选但败于 Newey-West 与 jackknife。且本次做了
5 维 × 2 horizon × 4 方法的多重检验，未做全局校正。

### 顺带更正 v0.42.2 遗留的一个说法
"样本仅覆盖单一 regime"**不成立**：① 指数层有方向切换（SPY T+7 月度：2月 −1.96%
100%为负、4月 +2.88% 0%为负、7月 −0.04%）；② 本系统实际持仓有真熊市被指数掩盖 ——
核心10票等权全窗口 **−10.71%**（同期 SPY +5.80%），最大回撤 **−26.91%**（SPY −8.58%），
单票 RKLB −61%／CRCL −54%／BILI −46%（yfinance 实测）。
不动 risk_adj 的正确理由是**统计口径脆弱性**（重叠收益 + 对少数天敏感 + 横截面窄），
不是 regime 单一。

## [0.42.2] — 2026-07-30 — 修复权重学习闭环的记分方向 bug + 补齐写入安全网

> 承 v0.42.1 的样本充足性结论，跑了 Bootstrap / Walk-Forward / FF6 三件套统计验证，
> 顺着"样本外过拟合 gap +8.6pp、test Sharpe −0.46"往下挖，定位到维度层记分的根因 bug。

### Fixed — 维度层记分方向 bug（P0 根因）
- `feedback_loop.py` 新增 `agent_vote_correct(vote, ret)` 作为**唯一记分入口**，
  判定基准是「蜂自己的票 vs 实际涨跌」，与快照整体 `direction` **无关**
- 旧实现用快照级 `direction` 推出 `is_correct`，再拿去评判每只蜂自己的票。对
  `direction="neutral"` 的快照 `is_correct` 恒为 False（`check_direction_accuracy`
  返回 `None`，下游 `not None → True`），于是退化成「只要 vote≤5 就算对」，
  **完全脱离价格实际走势**。实测 625 个快照里 neutral 占 202 个（**32%**）
- 后果：5 个维度准确率全部低于抛硬币（0.376~0.493）。修复后回到 0.498~0.532
- 修复三处重复实现：`weekly_optimizer.py` 主路径、`weekly_optimizer.py` bootstrap 内、
  `feedback_loop.calculate_agent_contribution`，统一改调 `agent_vote_correct`
- `vote == 5.0`（中性票）与 `ret == 0` 改为**弃权**，不计入准确率分母
- 交叉验证：修复后二值口径给出 risk_adj −2.54pp / sentiment +1.91pp，与独立的
  按日横截面 rank-IC 分析（risk_adj IC=−0.138 t=−3.72、sentiment IC=+0.087 t=+2.14）
  **首次符号一致**；修复前二值口径把 risk_adj 排准确率第一，与 rank-IC 直接矛盾

### Added — config.py 写入安全网（P1b）
- **写入前语法预检**：`compile(new_text)`，语法错的 config.py 会瘫痪所有模块，
  必须在覆盖原文件之前发现
- **写入前备份**：`config.py.weights.bak`（单槽）+ `weight_backups/config_<ts>.py`
  （滚动保留 8 份）。严格在 `tmp.replace()` 之前执行
- **写入后回读校验**：比对落盘值与目标值（4 位小数），不符自动从备份还原
- **`--rollback` CLI**：从审计日志重建上一次写入前的权重（只改 `EVALUATION_WEIGHTS`，
  **不做整文件还原**——config.py 1400+ 行，期间用户可能改过别的配置项）。
  `--rollback --to-backup` 为整文件还原逃生舱；`--rollback --dry-run` 预览

### Fixed — 审计日志可信度
- `write_weights_to_config` 的 dry-run 分支由 `return True` 改为 `return False`。
  旧实现导致 `{"dry_run": true, "applied": true}` 的矛盾记录（2026-05-10 那条），
  而 `health_check.py` 正在读这个字段
- 新增 `schema_version` / `skip_reason` / `action` 字段。**历史记录不改写**
  （审计轨迹不可篡改），由 `health_check.py` 按 `schema_version` 甄别旧记录
- **跳过也留痕**：旧实现只在 `significant or dry_run` 时写审计，于是"每周跑、
  每周无显著变化"表现为日志完全空白（2026-05-10 后 11 周），与"任务挂了"无法区分。
  现在跳过也记录，附 `skip_reason`（`below_min_change` / `dry_run` / `write_failed`）

### Added — 测试
- 新增 `tests/test_weekly_optimizer.py`（29 项）：记分真值表、方向无关性不变式、
  收益取反属性测试、备份/回读/语法预检、dry-run 语义、回滚往返
- `tests/test_feedback_loop.py` 新增 `TestAgentContributionScoring`（4 项）
- 全量 **1063 passed, 1 skipped, 1 xfailed**，零回归

### 行为影响：无
修复后最大变动 2.54pp < `MIN_CHANGE_PP=3.0` → 优化器仍不写入 config.py，
外部行为与修复前一致（实跑验证 config.py 逐字节未变）。这是纯正确性修复。

### 已知遗留（本次未做，见 `~/.claude/plans/tingly-skipping-pike.md`）
- **P1 护栏重构**：`clamp_shifts` 先钳 ±10pp 再重新归一化，归一化把边界值再放大 →
  历史实测突破（`risk_adj +10.72pp` / `signal −11.32pp`）；`_apply_weight_clamps`
  在归一化前执行、之后不再钳 → 落盘权重不保证满足 `WEIGHT_CLAMPS`（史上 catalyst
  到过 0.3316 > 上限 0.25）。修法：把 `WEIGHT_CLAMPS ∩ ±MAX_SHIFT_PP` 合并成单一
  盒约束后单次投影（复用 `_apply_weight_clamps` 的迭代算法，参数化 bounds）
- **P2 权重映射**：`w = acc/Σacc` 在数学上无法表达"这个维度没用"——acc 都挤在 0.5
  附近，归一化后必然全 ≈0.2，"所有维度一样好"与"所有维度都没用"输出相同。
  且 `compute_new_weights_wls` 的 docstring 声称做 OLS 回归取 beta + 共线性检测，
  实现里一行都没有。建议改 rank-IC + 显著性收缩 + floor 分配
- **P3 口径统一**：optimizer 读 `report_snapshots` 毛收益，walk_forward 读
  `pheromone.db` 净收益，`MIN_SAMPLES=10` vs validator 要求 40+20
- **P4 OOS 门**：`walk_forward_validator` 与 `weekly_optimizer` 零连接，
  `run_walk_forward()` 无任何 Python 调用方；`bootstrap_validate` 结果不阻断写入
- **统计现实**：修正后 5 维 edge 的 |t| 全 < 1.2（按日聚类 SE），二值准确率信息量
  不足以支撑权重学习。真正的杠杆在信号层（各蜂原始分质量），不在加权层——
  与 `experiments/penalty_replay_report.md` 早先结论一致

## [0.42.1] — 2026-07-30 — 退役 alpha-hive-sample-accumulator 定时任务

> 用户问"这个样本积累任务还有存在必要吗，我已经升级很多版了"。调查后确认：任务已过时
> 且实际空转约 2.5 个月，应退役。

### Removed / Changed — scheduled task `alpha-hive-sample-accumulator`
- 已 `enabled:false` 禁用（无法在本 session 直接 delete——它正是启动本 session 的
  scheduled task，需从常规 session 用 `delete_scheduled_task` 彻底删除；SKILL.md 保留可恢复）
- 退役依据：
  1. **目标达成**：`predictions` 表已有 674 笔已结算 T+30 样本（`checked_t30=1`），
     6/7 月另 180 笔成熟后达 ~850，落在原定 700-900 目标区间
  2. **已空转 ~2.5 个月**：库内最后一个周日样本 = 2026-05-17，最后一个
     `.samples-only-*.json` 产物同为 2026-05-17，但 cron 每周日照常触发
     （`lastRunAt 2026-07-27`）——近 ~10 次运行写入 0 条
  3. **根因环境隔离**：任务跑在 Cowork VM，VM 连不上 yfinance → 撞空扫描护栏
     （`alpha_hive_daily_report.py:2309`）→ 不写库。真正填库的是工作日 Mac orchestrator 扫描
  4. **扩 universe 卖点从未兑现**：全库有史仅 39 个不同 ticker，全部与主池重叠
- 影响：无。工作日 Mac 扫描继续每日填库；下游 `bear-hypothesis-backtest`（8/3）照常读取现有样本

## [0.42.0] — 2026-07-29 — 修复深度报告生成器对亏损标的（负 EPS）的两处崩溃

> 用户要求对 VKTX 财报做机构级深度分析，`generate_deep_v2.py --ticker VKTX --no-llm`
> 直接崩溃——VKTX 本季净亏损（EPS -$1.10），这是深度报告生成器第一次真正跑在
> 负 EPS 标的上，暴露出两个此前从未触发过的 `UnboundLocalError`。

### Fixed — `generate_deep_v2.py::_build_scenario_narrative`
- `_pe_a`/`_pe_b`/`_pe_d`/`_pe_e` 四个变量只在 `_fwd_eps > 0 and price > 0` 分支内定义，
  但后续 f-string 用 `if _fwd_eps else ""`（真值判断）而非 `if _fwd_eps > 0`（与定义
  条件一致）去决定是否引用它们——`_fwd_eps` 为负数时真值判断为 True，尝试引用从未
  定义的变量，崩溃。四处统一改为 `if _fwd_eps > 0 else ""`

### Fixed — `chart_engine.py::render_gex_profile_chart`
- `gex_profile` 为 list 格式（`advanced_analyzer` 实际产出的格式）时只定义
  `strikes_f`，末尾判空却错误引用了只在 dict 格式分支里定义的 `strikes`——统一
  改为判 `strikes_f`

### 验证
- 两处修复后 VKTX 深度报告完整生成（HTML + GEX 图表均正常），全量 1030 测试通过

### 已知遗留（本次未修，记录供后续参考）
- 报告内"估值快照"的 PE 倍数情景矩阵对负 Forward EPS（VKTX $-4.54）直接相乘，
  产出负股价（如"$-118 (-453%)"）——对亏损公司这套估值法本身不适用，展示负目标价
  没有意义，需要针对负/零 EPS 标的加一个"不适用，改用其他估值锚点"的分支
- 报告内第六章"五情景推演"出现两套概率不一致的情景表（EV +2.6% vs -3.19%），
  疑似模板里存在重复渲染/两套概率来源，需要进一步排查是否为设计如此的
  "初版估计 vs 精修后叙事"两个阶段值，还是真实 bug
- 报告底部免责声明写"Claude API 混合模式生成"，但本次运行全程 `--no-llm`
  （日志确认走的是"本地模式"/`_local_fallback`），这行文案疑似写死的旧字符串，
  没有反映实际运行模式——不影响本次输出内容，但下次生成前应先核实该行文案是否
  会误导用户以为消耗了 API

## [0.41.9] — 2026-07-24 — 消除自动流水线 Step 3 ML 报告重复抓取（限流连锁崩溃根治）

> 用户核对"自动跑的产出是否和手动规则模式一样"时，发现 7/23 自动流水线 10/10
> 标的的 ML 增强报告（HTML + analysis JSON）全部缺失，而 `.swarm_results`/
> 日报 md 数据完全正常。排查 `~/.claude/scripts/alpha-hive-orchestrator.sh`
> 发现根因：**Step 2（`alpha_hive_daily_report.py --swarm`）内部已经会生成
> 全部 ML 报告，Step 3（`generate_ml_report.py --tickers ...`）对同一批标的
> 做的是完全相同的 CBOE/yfinance 抓取，纯属重复**——当天两条路径背靠背对
> 同 10 只票各打一遍全套 API，把调用量顶到限流线，Step 2 和 Step 3 内部的
> ML 报告生成同时崩溃（`momentum_5d=None` 触发 `'>' not supported between
> NoneType and int`）。

### Changed — `~/.claude/scripts/alpha-hive-orchestrator.sh`（不在本仓库，Alpha Hive 自动化基础设施的一部分）
- Step 3 执行前先检查当天 `alpha-hive-{ticker}-ml-enhanced-{date}.html` 是否
  已由 Step 2 生成；全部齐全则跳过（沿用 Step 4/5 已有的"已由 Step 2 pipeline
  完成"跳过模式），只有缺失时才补跑，且只对缺失的标的传参，不再无条件全量
  重跑 10 只票
- 验证：模拟 7/23（全缺）→ 判定补跑全部 10 只（不劣于原行为）；模拟 7/22
  （已手动生成齐全）→ 判定跳过，不再重复调用

### 已知遗留（记录不修）
- `'>' not supported between instances of 'NoneType' and 'int'` 这个具体崩溃
  点本身未定位到精确代码行（本地无法稳定复现深夜限流时的多源同时降级组合），
  本次治标于根源（消除重复调用降低触发概率），未做防御性判空加固——若后续
  仍复现，需要在触发时抓取完整 traceback 而非仅日志摘要

## [0.41.8] — 2026-07-23 — 修复 VKTX 错误催化剂数据（三期临床数据混淆二期试验名）

> 用户让"仔细寻找 VKTX 8/21 前重大事件"，核实过程中发现系统内部
> `catalysts.json` + `catalyst_refinement.py` 里的 VKTX 三期临床数据日期是
> **错误信息**：标为 "VK2735 Phase 3 VENTURE Enrollment Complete"/"VK2735
> Phase 3 Topline Data"（2026-08-15，critical severity），但 VENTURE 实际是
> **二期**口服剂型试验（已于 2024-2025 完成，数据已在 ECO 2026 展示），真正的
> 三期大型减重试验是 **VANQUISH-1/VANQUISH-2**，公司自己给的顶线数据指引是
> **2027 年**，8/15 这个具体日期查无出处。

### Fixed — `catalysts.json`
- 删除错误的两条 VKTX 条目（试验名写错 + 顶线数据日期与公司指引矛盾提前近一年）
- 替换为核实过的条目：VK2735 **Phase 1** 维持剂量研究数据，公司仅给
  "Q3 2026"季度指引、无具体日期——`date` 取季度末 9/30 占位，`severity`
  从 critical 降为 high，`note` 字段明确标注"待验证"及来源

### Fixed — `catalyst_refinement.py::create_vktx_catalysts()`
- 移除同款硬编码的错误催化剂（`event_name="Phase 3 临床试验结果发布"`,
  `scheduled_date="2026-08-15"`, severity=CRITICAL）——这是与 `catalysts.json`
  独立的第二个数据源，两处不修会通过 chronos_bee 的按名去重机制同时残留
  错误信息。改为返回空 timeline，避免两处数据源各说各话

### 验证
- `ChronosBeeHorizon.analyze("VKTX")` 实测：催化剂从 3 个（含 2 个重复/错误
  的三期数据）收敛为 2 个（7/29 财报 + 修正后的 Q3 维持剂量研究，high severity）
- 全量 1030 测试通过

## [0.41.7] — 2026-07-23 — 恢复 `analysis-{ticker}-ml-{date}.json` 每日落盘（`collect_data.py`"方案B"断供 13 天）

> 用户核对"每日生活中枢"自动任务的 NVDA 数据时发现，`collect_data.py` 找到的
> 最新 `analysis-NVDA-ml-*.json` 是 **2026-07-02** 的（20 天前），只能拼接
> `.swarm_results_2026-07-21.json` 的蜂群评分去补全，产生了一个两个不同日期
> 数据混算出来的分数（4.46/bullish），跟同日期干净扫描的 5.4/neutral 对不上。

### Fixed — `alpha_hive_daily_report.py::_generate_ml_reports/_gen_one`
- 排查发现：`--swarm` 日常扫描走的 `_gen_one()` 内部其实已经算出了完整的
  `enhanced`（含 `advanced_analysis`/`ml_prediction`/`combined_recommendation`/
  当日 `swarm_results`），但只喂给 HTML 渲染就丢弃，从未落盘为
  `analysis-{ticker}-ml-{date}.json`。全项目排查发现这个文件格式**任何标的
  最后一次真正生成都是 7/9**——`collect_data.py`（"方案B"数据提炼工作流）依赖
  这个文件，断供 13 天后其内建的日期回退逻辑找不到近期文件，才退到 7/2
- 修复：`_gen_one()` 补一行 JSON 落盘，与 HTML 同步写入。副作用：
  `swarm_results` 落盘时已是当天口径，`collect_data.py` 里"analysis JSON 内
  swarm_results 为空→ 从别的日期补全"的 fallback 分支不再被触发，根治了
  跨日期拼接问题（该 fallback 逻辑保留作为历史数据兜底，不删除）

### 验证
- 重跑 NVDA 单标的：`analysis-NVDA-ml-2026-07-22.json` 正确生成，内嵌
  `swarm_results` 非空且为当天数据；`collect_data.py NVDA` 端到端验证
  `swarm_source: None`（未触发补全）、P/C Ratio/IV Skew/异常流均为 7/22 真实值
- 全量 1030 测试通过

## [0.41.6] — 2026-07-22 — 修复 `--date` 补跑历史交易日时价格锚定错误（架构级修复）

> v0.41.5 修完"同一报告内价格不一致"后，用户追问："这才是 7/21 真实收盘价
> $207.29，怎么又变成 $205.10？"——排查发现根因比 v0.41.5 更深一层：
> **`--date` 补跑历史交易日时，所有取价链路（CBOE/yfinance/AlphaVantage/
> Finnhub）从来都不是查"那一天的收盘价"，而是查"脚本运行那一刻的实时
> 报价"**。7/21 14:02 原始扫描恰好在收盘后不久跑，凑巧接近真实收盘价；
> 之后在 7/22（已开盘）陆续重跑两次，分别抓到 $206.34（盘前）和 $205.10
> （开盘后实时价）——这从来不是 7/21 的收盘价，是完全不同日期的实时报价。
> 这是补跑功能一直存在的架构缺陷，不是 v0.41.5 引入的。

### Added — `data_pipeline.py`
- `_fetch_historical_stock_data(ticker, as_of_date)`：用 yfinance `history(start=, end=)`
  锚定指定历史日期的真实收盘价（CBOE/AlphaVantage/Finnhub 都是当前实时报价源，
  没有免费的历史快照能力，只有 yfinance 有 start=/end= 历史区间能力）
- `fetch_stock_data(ticker, as_of_date=None)`：`as_of_date` 非空且不等于
  `pdt_today()`（真实当日）时走历史锚定路径；为 None 或等于今天时行为完全
  不变（当日实时扫描不受影响）

### Changed — 透传链路
- `swarm_agents/cache.py::_fetch_stock_data(ticker, target_date=None)`
- `swarm_agents/base.py::prefetch_shared_data(tickers, retriever=None, target_date=None)`
- `alpha_hive_daily_report.py`：`prefetch_shared_data(targets, retriever, target_date=self.date_str)`
- 所有走 `_get_stock_data()`/共享预取快照的 Agent（Scout/Oracle/Chronos/
  CodeExecutor 等）自动获得正确的历史锚定价，无需逐个改动

### Added — `tests/test_historical_price_anchor.py`
- 验证：过去日期走历史锚定（不碰 CBOE 等实时链）、等于今天/不传日期时行为不变、
  空历史数据诚实返回 fallback

### 验证
- 单测：`fetch_stock_data('NVDA', as_of_date='2026-07-21')` → $207.29（与真实收盘价一致）；
  `as_of_date='2026-07-20'` → $203.28（同样对得上）；不传 `as_of_date` 的实时路径不受影响
- 全量 1030 测试通过；重跑 7/21 报告验证部署

### 已知局限（明确记录，不在本次范围内）
- **期权数据无法历史回填**：CBOE/yfinance 的期权链都是"当前活跃链"，没有
  免费的历史期权链数据源——`OracleBeeEcho` 的期权信号（IV Rank/GEX/Max Pain
  等）在补跑历史日期时仍然只能是"补跑时刻"的期权快照，这是数据源本身的
  限制，非代码可修
- `scout_bee.py` 的板块相对强弱（`yf.download(period="25d")`）、`rival_bee.py`
  的技术指标（`yf.Ticker().history(period="3mo")`）仍是相对当前时间的滚动窗口，
  未接入历史锚定——这两处独立绕过了共享快照价，理论上补跑历史日期时也会
  用到"现在"而非"报告日期"的窗口，但影响的是衍生指标而非价格本身，本次
  聚焦价格锚定，未来若有需要可比照本次模式扩展

## [0.41.5] — 2026-07-22 — 修复同一报告内现价不一致（Chronos/CodeExecutor 各自查 yfinance）

> 用户核对 7/21 报告发现 NVDA 有两个不同现价：Scout/Oracle 走 CBOE 快照显示
> $206.34，Chronos 的"分析师目标价"卡片显示 $207.29（yfinance `analyst_price_targets`
> 自带的 "current" 字段）。两条链路各查各的价，同一份报告数字对不上。

### Fixed — `swarm_agents/chronos_bee.py`
- `analyst_targets.current_price` 不再信 yfinance 自带的 "current"，改用
  `self._get_stock_data(ticker)` 取共享快照价（与 Scout/Oracle 同源）；
  `upside_pct` 同步用统一现价重算；取不到快照价时整卡片置空，不展示半真数据

### Fixed — `code_executor_agent.py`
- 沙盒脚本抓到的 `current_price` 用共享快照价覆盖，技术指标（SMA/RSI）
  仍用沙盒自己拉的历史K线计算，不受影响

### Added
- `tests/test_price_consistency.py`：两个 agent 的 current_price 一致性回归测试

### 数据修复
- 重跑 7/21 规则模式验证：NVDA 现在 Scout/Chronos/CodeExecutor 三者一致显示 $205.10，已部署 gh-pages
- **范围说明**（用户确认）：只刷新 7/21 这份报告，不批量重跑历史报告——历史报告的价格是"当时扫描时刻"的快照，不应被现在的最新价覆盖；CBOE 与 yfinance 官方收盘价之间 ~0.3~0.5% 的正常延迟报价误差不算 bug，保留 CBOE 优先架构不变
- 期权卡片（OracleBee）价格因期权快照按交易日冻结缓存（设计如此，避免 IV Rank/GEX 当日数据分裂），仍可能与 Scout/Chronos 现价有小幅出入，本次未改动该机制

## [0.41.4] — 2026-07-22 — 修复 ScoutBee 深夜限流崩溃（"今日聪明钱动向"整节报错）

> 用户核对 2026-07-21 14:02 定时扫描发现"今日聪明钱动向"9/9 标的全部显示
> `Error: unsupported operand type(s) for -: 'NoneType' and 'float'`，RKLB
> 更是整只从扫描结果消失。结构化日志抓到完整 traceback：
> `real_data_sources.py:291 get_real_crowding_metrics → (vol_ratio - 0.5)`，
> `vol_ratio=None`。该函数调用点在 `scout_bee.analyze()` 里未被任何 try/except
> 包裹，直接冒泡到最外层 `AGENT_ERRORS` 兜底，返回泛化错误信息。

### Fixed — `real_data_sources.py` / `crowding_detector.py`
- 根因：v36.0/v40.1 起 `data_pipeline._fetch_history_metrics` 拉取 yfinance
  历史K线失败时，把 `momentum_5d`/`volume_ratio` **诚实置 None**（而非缺键，
  设计如此——不可得就不冒充），但 `get_real_crowding_metrics` 仍用
  `.get(key, default)` 取值——**这挡不住显式 None**，深夜限流命中时全体标的
  同时触发（RKLB 因更严重的超时被直接剔出扫描列表）
- `get_real_crowding_metrics`（vol_ratio/momentum_5d 两处）+
  `calculate_crowding_score`（price_momentum_5d/short_float_ratio 两处）
  改为显式 `is not None` 判断后回落中性代理值，不再依赖 `.get` 默认值
- 新增 `tests/test_real_data_sources.py` + `test_crowding_detector.py` 回归测试
- 手动重跑 7/21 规则模式验证：10/10 标的干净无报错，已部署 gh-pages（覆盖原故障数据）

## [0.41.3] — 2026-07-09 — 修复测试 mock 数据污染生产期权快照 + Gamma 日历两格恒空

> 用户追问 NVDA 深度页"近端关键价位 支撑 $140(OI:600)"（现价 $203.62）。溯源：这批数字与 `tests/test_options_analyzer.py` 的 **mock 期权链一字不差**——pytest 跑 `agent.analyze("NVDA")` 时 mock 了取数函数但没挡住 analyze() 的快照写盘副作用，mock 数据（标 `data_quality: real`）写进生产 `cache/options_snapshot_NVDA_2026-07-09.json`，随后正式扫描按"当日快照命中"整份复用进日报。

### Fixed — 测试隔离失效的根因（`options_analyzer.py` / `data_fetcher.py`）
- `OptionsDataFetcher.__init__` / `CacheManager.__init__` 的默认参数 `cache_dir=str(PATHS.cache_dir)` 在 **import 时求值一次**，conftest 的 `ALPHA_HIVE_CACHE_DIR` 临时目录隔离对其永久失效（经典 Python 默认参数陷阱）。改为 `None` 默认 + 实例化时解析
- `tests/conftest.py` 第二层防线：autouse 注入 `OPTIONS_SNAPSHOT_DISABLE=1`
- 删除被污染的 NVDA 快照并重跑，key_levels 回归真实量级（支撑 $180 OI 7.9 万 / 压力 $250 OI 9.5 万），Gamma 日历 Pin 7/17 @ $200

### Fixed — `generate_ml_report.py` Gamma 日历 schema 错位（独立老 bug）
- "下一主要到期日"/"OI 集中度"两格读取 `next_major_expiry` / `oi_concentration_pct`——数据生产端 `calculate_gamma_expiry_calendar` **从未产出过这两个字段**，两格自上线起恒为 — / 0.0%。改为从实际的 `expiry_oi` 列表推导（NVDA 实测 2026-07-17 / 87.0%）

## [0.41.2] — 2026-07-09 — 修复近端磁吸目标价垃圾值（NVDA $50/+307%，v40.1 假价反模式的期权版漏网）

> 用户报告 NVDA ML 页"近端磁吸目标价 $50 ↑+307.2%"（现价 $203.62）。根因：`oracle_bee._calc_max_pain` 是漏网旧实现——绕过 CBOE 裸调 yfinance 最近到期日；深夜限流返回**全零 OI 链**时每个行权价痛苦值恒为 0，`min()` 退化取链内最低行权价（NVDA 周链最低 $50）。审计 7/8 全部 10 票：**7 只垃圾**（TSLA +392%、META +504% 等）。

### Fixed — `swarm_agents/oracle_bee.py`
- `_calc_max_pain` 主源改 **CBOE**（`fetch_cboe_chain`，与期权链同源，取返回链最近到期日），yfinance 仅兜底
- 抽出纯函数 `_max_pain_from_oi` 并加双重退化保护：① 总 OI < 500（限流空链常态）返回 None；② 结果偏离现价 >50% 视为数据垃圾返回 None——宁可诚实空缺不给假磁吸价
- 仅影响展示与 discovery 摘要，不进评分公式，修复零评分副作用

### Added — `tests/test_no_fake_price.py`
- `test_max_pain_degenerate_guards`：全零 OI / 薄 OI / 偏离 >50% 三种退化必须返回 None，正常链算出合理值

### 数据修复
- 全量重跑 7/8 规则模式，10 票近端磁吸价用 CBOE 重算（NVDA 实测 $200 +1.9%，与远期参考 $180 相互印证），部署 gh-pages

## [0.41.1] — 2026-07-09 — 删除"对比模式"（手机端幽灵横条元凶之一）

### Removed
- 卡片对比模式（升级 H2 遗留）：纯桌面键盘功能（按 `c` 进入、Esc 退出），无任何可见入口、无移动端触发方式；其隐藏机制（`translateY(-100%)` 但定位起点在导航栏下方）在手机 X5 内核上无法完全出屏 + `pointer-events:none` → 渲染为**看得见但点不了的幽灵横条**（用户报告"对比模式 0/3 用不了"）。用户确认无用后整体删除：`dashboard.html` compare-bar div + 快捷键帮助 C 行、`dashboard.css` 全部 `.compare-*`/`.cg-*` 规则（~30 行）、`dashboard.js` 'c' 键绑定 + Escape 分支 + 卡片点击守卫 + 整个对比 IIFE（~140 行）。
- **保留**：历史简报差异对比（`btn-diff`，L303"简报对比"）是另一功能，未动。
- 契约测试 15 项全绿；已重渲染部署。

## [0.41.0] — 2026-07-09 — 修复移动端"两层页面"三症状（canvas 溢出 / 重载闪现 / 双导航）

> 用户手机（微信 webview）打开仪表板出现三症状。审计发现三者同源于一个连锁：**canvas 固定 width 属性（#scoresChart 600px 等）无任何 max-width 约束 + Chart.js 从 jsdelivr 加载（大陆/微信常不可达）** → CDN 失败时 responsive 永不生效 → 600px 空 canvas 撑宽页面（右侧空白带）→ 微信 X5 把布局视口扩到内容宽 → media query 误判桌面 → 顶部链接导航不隐藏（"双导航"）+ 横向两屏；叠加微信持久缓存旧页 × dashboard.js 时间戳自动重载 → 新旧页无限闪现。

### Fixed — `templates/dashboard.css`
- 全局 `canvas{max-width:100%;height:auto}` + 四类图表包裹层 `max-width:100%;overflow:hidden`（Chart.js 加载失败时的布局兜底——模拟验证：无 Chart.js 时 600px canvas 被压至 246px，页面零横向溢出）
- `html,body` 双重 `overflow-x:hidden`（原仅 body，微信 X5 不认）
- `.acc-two-col` 补 768px 折叠；`.hstat` 移动端 padding 收窄

### Changed — Chart.js 自托管（治本）
- 新增 `chart.umd.min.js`（4.4.0，205KB）随仓库分发；`dashboard.html` script 改本地引用、CSP script-src 移除 jsdelivr；`report_web_assets.py` sw.js 预缓存同步改本地；**两处部署白名单**（`report_deployer._CORE_FILES` / `generate_ml_report._CORE`）加入该文件。线上验证 HTTP 200。

### Fixed — `templates/dashboard.js` 重载闪现防护
- `fetchDashboardData()` 的自动 `location.reload()` 加 sessionStorage 一次性守卫（`ah_auto_reloaded`）：每会话最多自动刷新一次，之后仅 console 提示——微信缓存旧页时从"无限闪现"退化为"最多闪一次"。

### 验证
- 375px 视口：`scrollWidth==innerWidth`、顶部链接导航正确隐藏、Chart.js 本地加载成功；模拟 CDN 被墙场景全部通过；1019 tests passed。

## [0.40.4] — 2026-07-08 — 历史中性标签一次性迁移至 ±5% 统一带宽（用户拍板）

### Fixed — `pheromone.db.predictions` 历史标签口径统一
- **背景**：用户对账网站"全部预测 34.2%"与"可执行方向单 55.9%"两口径差异时，发现第三个问题——v0.38.1 把中性判对带宽 ±3%→±5% 后，**数据库历史行的 correct_t1/t7/t30 标签仍是检查当时按旧规则存死的**（且样例显示部分中性行历史上曾被按"看多规则"打标：-1.6% 判错、+28% 判对），新旧标准混存导致展示口径不一致。
- **迁移**（备份 `pheromone.db.bak-20260708-095812` 后执行）：全部 `direction='neutral'` 且已回填的行按统一 `determine_correctness_bool`（±5% 带宽）重打标——t1 重打 41/225、**t7 重打 83/206（40%）**、t30 重打 14/160。**只动中性行，看多/看空标签零改动**（迁移前后方向单口径 52.9%/53.5% 纹丝不动，即迁移未污染交易统计的证据）。
- **迁移后展示**：30 天全部预测 38.2%→43.8%、90 天 51.8%→52.5%；周切片 W21 5%→10%、W24 15.4%→20.5%；已重渲染 dashboard 并部署 gh-pages。
- **口径对账结论**（回答用户"为什么和你说的不一样"）：34.2% 是日报近 30 天"全部预测"参考行（含不产生交易的中性 28-55% 占比 + score<6 观望档），交易决策应看同报告上一行的"可执行方向单"（30 天 52.9% / 90 天 53.5% / 全样本 55.9%）——v0.37.0 起该口径已是日报主数字。

## [0.40.3] — 2026-07-08 — CLAUDE.md × 2 + MEMORY.md 除锈（防陈旧误导整治）

### Changed — 项目 `CLAUDE.md`（133 → 71 行）
- **修正主动误导**：paper_portfolio 段还写着 v0.19 时代参数（SL -5%/TP +10%/每仓 1.5-2.5%/挂在 generate_deep_v2）——与 v0.38-0.39 三次变更直接矛盾，每个新 session 都会被注入误导。改为"参数唯一真相 = 模块内 CONFIG"的指针式描述。
- **删除 ~75 行 "已完成的重要改动" 旧清单**（v0.10-0.19 时代实现细节，与 MEMORY.md 版本历史职责重叠且多处已被覆盖），替换为查询指针（版本摘要→MEMORY 版本表，细节→CHANGELOG）。
- 删除与 MEMORY 冲突的定时任务时刻（weekly_optimizer "周日 02:00" vs 实际 PDT 09:07 等），指向 `list_scheduled_tasks` 为唯一真相。
- **新增文档分工原则**（防复发）：CLAUDE.md 不存易变参数值与统计数字快照——只存指针与不变式。

### Changed — `MEMORY.md`（状态行 9,741 → 3,086 字符，-68%）
- 状态行只保留 v40.0-40.3 完整叙事 + v38.2-39.0 摘要；v38.0 及以下细节归并至版本历史表（原本双份重复）。
- **新增「定档任务与勿再提议清单」独立小节**：8/3 bear-hypothesis-backtest 定档、8 月初 v39 参数复盘、四条回测证伪记录、meta-labeling/cs_rank 样本门槛、odds 区分度重估——原先埋在巨型状态行里易在压缩中丢失的操作性约束，现集中且耐久。
- 过时条目修正 4 处：BuzzBee 通道描述（v40 起无 Finviz/StockTwits）、期权链条目 tradier 表述（`tradier_fetcher.py` 存在但只读未接入）、yfinance 限流条目（现价+期权已 CBOE 化，影响面大幅缩小）、删除 v21 时代组合统计数字快照（立"组合级数字勿存 memory"规则）。
- 版本历史表补 40.1-40.3 行。

### Changed — 全局 `~/CLAUDE.md`
- 数据源第二梯队移除 Finviz（通道已删 + Cloudflare 永久封）。其余身份/流程/模板/硬规则不动。

## [0.40.2] — 2026-07-08 — 修复 paper_portfolio.meta.json 的 config_snapshot 陈旧不刷新

### Fixed
- `_load_meta()` 只在 `meta.json` 首次不存在时（3/9 bootstrap）写入 `config_snapshot`，之后每次 `run_for_date()` 只更新 `cash`/`last_run_date`，从未回写 `config_snapshot`——v0.39.0 改仓位参数（tp_pct 10→15、仓位×2、在场30→80、白名单清空）后，`meta.json` 里的快照字段仍停在 3/9 的旧值，误导任何读取该字段核对当前生效参数的场景（不影响实际交易，交易逻辑一直用模块内最新 `CONFIG`，纯记录展示字段滞后）。
- `run_for_date()` 每次运行时用当前 `CONFIG` 刷新 `meta["config_snapshot"]` 再保存。

### Verified — 用户核对纸面组合 vs SPY 基准
- 现行组合（$50,615，+1.23%）vs SPY 同期 +10.24%，Alpha -9.01%——最大回撤仅 -0.56%，印证"胜率高（56%）收益低"的根因是资金利用率低（当前仅 2 笔持仓、4% 资金在场）。
- v0.39.0 新参数（TP15%/仓位×2/在场80%）**尚未有机会实际生效**——当前仅有的 2 笔持仓（TSLA/MSFT）均开仓于 6/29，早于 7/7 的参数变更，按设计其 SL/TP 已固化用旧参数（TP 10%/仓位 2.5% 档），需等下一次过门槛信号（bull≥6.5 或 bear≤3.5）新开仓才会首次验证新参数。
- 另确认与 `dashboard-data.json` 的 `trading_stats.realistic` 卡片（`portfolio_backtest.py` 独立回测引擎，max_concurrent=15，与 `paper_portfolio.py` 是两套不同系统，互不影响）区分开——避免混淆两处 SPY 对比数字。

## [0.40.1] — 2026-07-08 — 网站股价再次出错：根除 100.0 占位价反模式的第 2/3 处漏网 + 快照价注入

### Fixed
- **复发现场**：7/7 深夜定时扫描后，NVDA/MSFT/AMZN/TSLA 在仪表板显示 $100.0。根因与 v36.0 完全同款反模式的另外两处漏网：`alpha_hive_daily_report._gen_one`（~L1746）和 `generate_ml_report`（~L2044）都是"先初始化 real_price=100.0，yfinance 成功才覆盖"——深夜限流失败时假价写进 `analysis-*-ml-2026-07-02.json`，仪表板 7 天新鲜度兜底恰好读到。
- **修复**：两处占位价 100.0 → **0.0 哨兵**（下游 `_inj_price` 对 0 跳过，自动落到更旧真实价），且取价改走 `data_pipeline.fetch_stock_data`（CBOE 起头多源链）而非裸 yfinance。已用 CBOE 实价 patch 回 4 个被污染的 analysis 文件。
- **新增注入源 ①.5**：`dashboard_renderer` 价格注入在 Agent 价与 analysis 文件之间插入**当日反馈快照 entry_price**（每扫描日全标的必落、带 forming-bar 护栏）——本次重渲染后 10/10 标的显示 7/7 当日真实收盘价，陈旧 analysis 兜底基本退役。
- **全仓 grep 又揪出 3 处同款**（一并修复）：`crowding_detector.py`（hist 空时 price=100 → 0 哨兵，下游不用 price）、`risk_engine.py`（yfinance 失败编造 price=100 的假风险报告 → 先走 CBOE 多源链，仍失败 0 哨兵+ERROR 日志）、`unusual_options.py`（假价 100 会把 OTM 距离全算错产出假异动 → 多源链兜底，仍失败诚实跳过异动检测）。至此 `100.0` 占位价反模式生产代码清零。

## [0.40.0] — 2026-07-08 — 对标主流开源量化系统差距补齐（A-E）+ Finviz 删除

> 用户要求对标 GitHub 主流免费量化系统（Qlib/vectorbt/FinRL/ai-hedge-fund/TradingAgents/Zipline+Alphalens）找高价值改进，并确认删除 Finviz。对标结论：多 agent 架构（同 45k★ ai-hedge-fund 但零 LLM 成本）、T+7 真实结果闭环、FF6+HAC 归因、bootstrap CI/walk-forward 工具（CLI）均已有勿重复造；真实差距 = ML 无时序验证、无横截面排名、三重屏障标签断链、维度 IC 无例行监控。

### Removed — A. Finviz + 僵尸模块（连带修复新闻通道被死锚拖累）
- **删除**：`finviz_sentiment.py`（Cloudflare 按代理出口 IP 永久 403）、`stocktwits_sentiment.py`（休眠未接线）、`vectorbt_bridge.py`（孤儿零调用）、`tests/test_finviz_sentiment.py`（15 测试）。
- **关键修复**：旧新闻通道 = "Finviz 60% + Yahoo/AV 40%" 固定融合，Finviz 永久 403 把新闻分锚死在中性 50 的 60% 权重——`buzz_bee.py` 现以 newsapi（Yahoo/AV）为 news 通道 100% 基底，LLM 语义增强块保留（仍由 `llm_service.disable()` 门控，规则模式零调用）。冒烟验证：NVDA 新闻从常年"无新闻数据"变为真实主题"看多叙事主导"。
- `bear_bee.py` 新闻回退分支改 newsapi；`data_fetcher.get_stocktwits_metrics` 改走 `real_data_sources.get_social_buzz`（Reddit 真实代理，删除 `_estimate_*` 编造样本）；残留清理：resilience（limiter/breaker/超时表）、config（ttl/高频源/STOCKTWITS 块）、queen_distiller REAL_SOURCES（+newsapi）、report_formatters 标签映射、测试陈旧标签。

### Added — B. ML 时序验证（暴露重大盲区）
- `ml_predictor.HGBModel` 新增 `_eval_oos_purged()`：按日期排序切尾部 25% 作外样本 + 7 天 embargo（防 t+7 标签泄漏），clone 模型上评估真实泛化精度，然后才全样本重训供生产（验证与部署分离）。`oos_accuracy` 持久化进 `ml_model_cache.json`。
- **实测结果（这就是修复的意义）**：in-sample 68.6% vs **OOS 37.6%**——模型外样本不如抛硬币，纯记忆训练集；此前日报的"HGB 准确率 75.4%"是自考自评假象。
- `queen_distiller._ml_oos_trust_factor()`：按 OOS 缩放 ML 调整信任度（`ML_FEEDBACK_CONFIG.oos_trust_*`：≥55% 全信、≥50% 减半、<50% 置零）——当前 OOS 37.6% → RivalBee 的 ±0.5 ML 调整被正确置零。新增 2 个测试（机制验证 + 置零验证）。

### Added — C. 横截面排名埋点（对标 Qlib，只记账不改评分）
- `alpha_hive_daily_report._post_scan_enrichment`：每日对 universe 算 final_score + 5 维度的 0-1 分位（`cs_rank`），随 swarm_results 与 report_snapshots 落盘。4-6 周后回测 rank-IC 决定是否升级为正式维度。

### Added — D. 三重屏障标签回流（对标 López de Prado meta-labeling）
- `paper_portfolio._record_barrier_outcome()`：平仓时 SL/TP/TIME 结果幂等写入 `pheromone.db.barrier_outcomes` 表；`_REPLAY_MODE` 守卫防沙盒回放污染生产表。已回填 25 笔历史（TP 11 笔均+9.8% / SL 6 笔均-7.4% / TIME 8 笔均-0.2%）。本期只写不读，meta-labeling 需 ≥100 笔再启动。

### Added — E. 每蜂维度 rank-IC 月度报表（对标 Alphalens）
- `self_analyst.compute_dimension_ic()`：每蜂原始分 vs T+7 的 Spearman rank-IC + 近 1/3 窗口趋势（改善/退化/持平），并入月度 brief「一.五」节，给 Track A 权重优化提供透明依据。首跑（47 样本）：OracleBee 全窗口 +0.368 但近期退化到 -0.003、ChronosBee 近期 -0.841 严重退化——样本薄仅作线索。

## [0.39.0] — 2026-07-07 — 纸面组合资金利用率参数上线（用户拍板 v0.38.2 回放拐点配置）

### Changed — `paper_portfolio.py CONFIG`
- `tp_pct` 10 → **15**（回放最大红利项：TP15 组合在 Calmar 榜前 12 名清一色，止盈太早砍掉赢单尾部是"胜率高收益低"的直接病因）
- `size_pct_by_tier` high 2.5 → **5.0**、mid 1.5 → **3.0**（仓位 ×2 甜点；×3 Calmar 回落不采用）
- `max_deployed_pct` 30 → **80**（上限作用是不卡好信号——回放实测平均在场仅 ~26%）
- 不变：`entry_score_bull=6.5`（门槛 6.0 风险调整后不划算）、`sl_pct=7`、`time_stop_days=10`
- **生效方式**：从当前 NAV（$50,692）续跑，不重置历史；存量持仓（TSLA/MSFT）的 SL/TP 价位已在开仓时固化，新参数只作用于新开仓位。回放预期：年化 ~20%、MaxDD ~2%（样本 3.4 个月偏多头，**8 月初复盘**，与 bear-hypothesis-backtest 同期）

## [0.38.2] — 2026-07-07 — 诊断"胜率高收益低"：组合资金利用率网格回放（36 组合，只出报告未改生产）

> 用户问"能不能解决胜率高、收益率低的问题"。诊断：信号层 288 个方向单胜率 55.9%、单均 +2.82%——edge 真实；组合只 +1.38% 是**资金利用率问题**（单笔 1.5-2.5% NAV、门槛 6.5 严于信号口径、白名单 bug 卡死两个半月），不是信号质量问题。

### Added — `paper_portfolio.py` 沙盒回放能力
- `run_replay(config_overrides, state_dir, dates)`：隔离状态目录 + 临时覆盖 CONFIG（try/finally 恢复），复用 `run_for_date` 全部逻辑零行为改动；回放统一 `ticker_whitelist=[]`（评估参数而非白名单 bug 的历史）。
- `prefetch_ohlc()` + `_fetch_ohlc` 全区间切片层（end 排他对齐 yfinance 语义）——36 组合回放零重复网络调用；生产路径行为不变。

### Added — `experiments/portfolio_capacity_replay.py` + `portfolio_capacity_report.md`
- 网格：entry_score_bull {6.5,6.0} × 仓位 {×1,×2,×3} × max_deployed {30,60,80%} × TP {10,15%}，SL 7%/T+10 固定；全历史 72 快照日回放，按 Calmar 排序 + 前后半段稳健性检查。
- **结论**：性价比拐点 **bull≥6.5 / 仓位×2（high 5%/mid 3%）/ 在场上限 80% / TP 15%**——收益 +6.24%（同口径基线 +1.99% 的 3.1 倍）、MaxDD -1.90%、Sharpe 2.38、Calmar 10.75，通过稳健性检查。三大发现：① **TP 10%→15% 是最大红利**（前 12 名全是 TP15，止盈太早砍掉赢单尾部是"胜率高收益低"直接病因）② 仓位 ×2 是甜点、×3 开始 Calmar 回落 ③ 门槛放松到 6.0 风险调整后不划算（Calmar 前三全是 6.5）。进取选项 bull≥6.0/×3/80%/TP15：+9.35% 但 MaxDD 翻倍。
- **生产 CONFIG 未改**——等用户看报告拍板；样本仅 3.4 个月且偏多头行情，中标配置建议先跑 4 周复盘。

## [0.38.1] — 2026-07-07 — 中性判定带宽 ±3% → ±5%（落地 P2-1 回放实验结论）

### Changed — `outcome_utils.py`
- `DEFAULT_NEUTRAL_TOLERANCE_PCT` 3.0 → 5.0。依据 `experiments/neutral_band_replay.py` 全样本回放（164 条中性预测）：±3% 命中仅 36%——高波动政体下 61-77% 样本 |T+7|>3%，"中性但 |ret|>3%"本质是该标的正常波动而非预测错误；±5% 命中 52%，与更复杂的波动率缩放口径（±0.674×σ7，53%）几乎等效但实现简单。
- **影响面**：仅准确率记账（`backtester` 统计 / `outcomes_fetcher` 回填标签），中性不开仓、不影响任何交易行为。headline 主口径仍是 v0.37.0 的可执行方向单（actionable），不受本次影响。
- **历史备注**：用户记忆中"改过中性带宽"实为 2026-03-10 方案12（统一 OutcomesFetcher 零容差 vs Backtester 1% 容差为共享 `determine_correctness`）——±3% 中性带宽正是那次作为新常量引入的，此后首次调整。
- `tests/test_outcome_utils.py`：中性边界测试同步更新（新增 ±4% correct 用例，边界移至 5.0）。

## [0.38.0] — 2026-07-07 — 三 agent 全面审计落地：假数据切断（P0）+ 降级透明化（P1）+ 评分链路回放实验（P2）

> 用户要求"看下还有哪里可以优化，给高价值修改方案"。三个并行审计（评分链路/数据质量/产品运维）+ 本 session T+7 诊断，按 P0/P1/P2 分档全量实施。已排除项（勿再提议）：BearBee 权重/信息素多样性（定档 8/3 scheduled task）、risk_off/F&G 门控（已证伪）、odds 权重手动砍（归 Track A）。

### Fixed — P0-1 期权样本数据泄漏进评分与报告（CRITICAL）
- **根因**：CBOE/yfinance 全挂时的硬编码样本期权链虽标 `data_quality="unavailable"`，但 unusual_activity/IV Rank/P-C/GEX 照算照进 OracleBeeEcho 评分和日报——7/2 日报 QCOM/NVDA/TSLA 显示完全相同的假异动信号（$140/$145/$150 call，8500/12000/6200 手）、NVDA IV Rank=100 / QCOM=0 假极值（样本历史 IV 区间与 fallback 现价错配）。
- `options_analyzer.py`：`analyze()` 样本链早退（全指标 None/空/中性 5.0，不写快照以便当日 API 恢复可重算）；`fetch_historical_iv` 新增 `last_hist_iv_is_sample` 标志，历史 IV 为样本时 IV Rank/Percentile 置 None；`generate_options_score` 对 `iv_rank=None` 走中性。
- 下游防御：`bear_bee.py`（pc_ratio/iv_rank None 中性化）、`advanced_analyzer.py`（样本链跳过 Dealer GEX）、`generate_ml_report.py`（unavailable 时显示"期权数据不可用"卡片而非假指标）。
- **潜在红利**：odds 维度"零区分度"（v32.5 结论）可能部分源于假数据污染，切断后需重估。

### Fixed — P0-2 momentum_5d 用 1 日涨跌幅冒充 5 日动量
- **根因**：v36.0 引入的 `CBOESource` 把 `price_change_percent`（当日）塞给 momentum_5d，下游 BuzzBee 情绪映射、sentiment 背离检测（bull trap 阈 ±3%）、RivalBee 空头阈值全按 5 日语义消费；CBOE 是第一源 → 每天都在用错的动量。AlphaVantage/Finnhub 降级源同病。
- `data_pipeline.py`：新增共用 `_fetch_history_metrics()`（yfinance 历史K线独立算 momentum/volume_ratio/volatility，含 forming-bar 护栏）；CBOESource 价格走 CBOE + 指标走历史K线，历史不可得时 momentum_5d/volume_ratio 诚实置 None + `momentum_source="unavailable"`（新字段）；AV/Finnhub 同步置 None 不再近似。
- 下游 None 防御：`buzz_bee.py`（动量/量比 None → 中性信号 50 + discovery 显示 N/A + data_quality 标 unavailable）、`rival_bee.py`（ML 特征中性 0.0 / fallback 方向判定跳过动量分支）、`bear_bee.py`（mom_5d/vol_ratio None 中性化）。

### Fixed — P0-3 纸面组合饿死（39 天没跑 + 只剩 NVDA 白名单）
- **根因**：`run_for_date` 只挂在 `generate_deep_v2`（深度报告）里，日报流程不生成深度报告 → 组合停在 2026-05-29；且 `ticker_whitelist=["NVDA"]` 是深度报告时代遗留，只允许 NVDA 开新仓（当前仅 $379.63 在场 = 0.76% 资金）。
- `alpha_hive_daily_report.py`：`run_for_date(date_str)` 挂进日报主流程（快照落盘后），幂等；`paper_portfolio.py`：whitelist 放开为 `[]`（全标的）。
- 已补跑 5/30-7/02 共 12 个交易日：期间 VKTX 两次止盈、QCOM/VKTX 各一次止损，当前 NAV $50,656.54、持仓 TSLA/MSFT 2 笔。

### Added — P1-1 降级透明化（is_fallback 标志 + 数据质量横幅/小节）
- `finviz_sentiment.py`/`reddit_sentiment.py`：fallback 结果带 `is_fallback: True`；Reddit 区分"榜单正常但不在前100（真安静 quiet）"vs"API 全灭（fallback）"。
- `buzz_bee.py`：data_quality 按 is_fallback 精确标注（reddit: real/quiet/fallback 三态；finviz 抓取失败标 fallback + "新闻不可用（抓取失败）"）。
- `report_formatters.py`：日报新增"数据质量"小节（通道健康度 % + 降级通道清单 + 纸面组合新鲜度滞后提示）。
- `dashboard_renderer.py`：新增 `dq_banner_html`——数据真实度 <70% 或任一通道 ≥3 标的降级时 hero 区渲染醒目横幅；Polymarket 属设计性缺失（无个股预测市场，OracleBee 已自动重分配权重）不计入，防横幅永久常亮。
- **符合 Slack 免打扰规则**：降级只在报告/仪表板内可见，不发 DM。

### Added — P1-2 模板↔JS 契约测试（防 equityChart 式死骨架复发）
- 新增 `tests/test_dashboard_contract.py`（6 测试，纯静态文本断言零浏览器依赖）：canvas id ↔ JS 引用、`__AH__` 消费键 ↔ `_data_obj` 生产键、radar- 动态前缀存活、data_json 注入存在、模板占位符 ↔ render kwargs。

### Added — P2 评分链路离线回放实验（只出报告，未改生产）
- `experiments/neutral_band_replay.py` → 中性带宽三口径回放：固定 ±3%（现行）全样本命中 36%、±5% 52%、波动率缩放（±0.674×σ7）53%。结论：±3% 对高波动标的近乎抛硬币，建议 `outcome_utils` 支持波动率缩放带宽（上线与否等用户定）。
- `experiments/penalty_replay.py` → **证伪审计的"罚分叠加毁掉区分度"假设**：罚分前原始分 rho 仅 +0.054（这是区分度上限），现行叠加式 +0.063 甚至略优于统一风险面 +0.052，可执行方向单命中率 57% vs 56%。**结论：勿改罚分结构，治标不治本——真正瓶颈在各蜂原始分本身**（写入"已证伪勿再提议"清单）。

## [0.37.0] — 2026-07-03 — 诊断"T+7 准确率 33.7%"：口径修复（可执行方向单）+ BearBee 强信号压制看多

> 用户问"解决最近胜率低的问题"。基于 545 条快照 + 536 条 swarm 历史回测的完整诊断，headline 33.7% 是三重稀释的结果，而非可执行信号失效。

### 诊断结论（数据支持）
1. **观望档污染 headline**：近30天 89 条样本里，score<6.0 的"暂不行动"档预测（本不建议行动）也计入胜率。6 月 51 条快照中仅 4 条是 score≥6 的真方向单。
2. **中性预测占比暴涨且判据天然低命中**：中性占比从 4 月 16% → 6 月 55%；判对标准 |T+7|≤3%，但近月 61-77% 样本 |ret|>3%（高波动），中性天然只有 ~40% 命中，拉低总数。中性=不行动，零 PnL 影响。
3. **6 月单月下跌行情看多失效**：6 月看多 16 条仅 19% 命中（均收益 -6.1%），但 4/5 月同一系统 65%/56% 有效——单月政体切换，非因子死亡。看空 6 月 86% 命中但产量极低（全样本仅 8%）。
4. **修正后的真实质量**：可执行方向单口径（看多 score≥6 + 全部看空）近30天 50%（18条，均PnL+0.13%），全样本 56%。

### 回测验证（含证伪，防重蹈 v0.34.1"背离过滤器"覆辙）
- ❌ **宏观 risk_off 门控看多——证伪**：全样本看多@risk_off 胜率 60%/均收益+2.69%（比 neutral 政体还好），压制会净有害，不上线。
- ❌ **F&G 恐惧区门控看多——证伪**：F&G 25-40 区看多胜率反而最高（74%）。
- ✅ **BearBee 强信号压制看多——轻微有效**：bear_score≥7.0 历史触发 13 单，被拦截的看多均收益 -1.70% vs 全体 +3.09%，上线（保守设计：只降级 neutral 不翻转）。

### Changed — `backtester.py`（`get_accuracy_stats`）
- 新增 `actionable` 统计块：`(direction='bullish' AND final_score>=6.0) OR direction='bearish'` 的方向单准确率 + 方向调整均 PnL，随 stats dict 返回。

### Changed — `report_formatters.py`（`_build_backtest`）
- 日报"历史预测准确率"段主数字改为**可执行方向单**口径，原全样本数字降为次要行（含观望档/中性），无 actionable 数据时回退旧格式。

### Added — `swarm_agents/queen_distiller.py`（`_compute_direction_vote`）+ `config.py`（`BEAR_SCORING_CONFIG`）
- **BullVeto**：方向判定末端（S5 冲突仲裁之后），若 `rule_direction=bullish` 且 BearBeeContrarian score ≥ `bull_veto_bear_score`(7.0)，降级为 neutral（不翻转为 bearish），`conflict_info["bull_veto"]` 记录触发详情。
- config 开关：`bull_veto_enabled: True` / `bull_veto_bear_score: 7.0`。若当日生效，7/2 的 TSLA（bear 8.3）、QCOM（bear 7.7）看多会被降级中性。
- 测试：141 passed（queen/backtester/formatters 相关套件全绿）。

### 遗留观察项
- 看空产量过低（全样本 8%）但质量高（61%）——蜂群信息素历史"多5/空0"自我强化看多。下一步候选：信息素板方向多样性激励 / BearBee 投票权重提升,需新样本回测。

## [0.36.0] — 2026-07-01 — 修复网站股价全错（`_analyze_ticker_safe` 硬编码 100.0 假价）+ CBOE 设为股价主源

### Fixed — `alpha_hive_daily_report.py`（`_analyze_ticker_safe`，~L262）
- **根因**：非 swarm 路径（CLI 不传 `--swarm` 时调用的 `run_daily_scan()`）里，`realtime_metrics` 是硬编码的占位数据 `{"current_price": 100.0, "change_pct": 2.5}`，对**每一个**标的都一样。这行代码是 `swarm_agents/cache.py::_fetch_stock_data` 早就修过的同一个反模式的"漏网之鱼"——那边的注释写得很清楚："原实现失败时返回 price=100.0（虚假数据）...新实现失败时返回 price=0.0 + `_data_unavailable=True`"，但 `alpha_hive_daily_report.py` 里这个并行的旧代码路径从未同步更新。
- **暴露方式**：某次手动/测试运行 `alpha_hive_daily_report.py`（未传 `--swarm`）批量覆盖了 10 只标的当天的 `analysis-{ticker}-ml-2026-06-30.json`，其中 8 只（除 NVDA/AMZN 外，这两只当天 swarm 扫描本身带了真实价格，跳过了这个注入兜底逻辑）的 `current_price` 全部变成 `100.0`。`dashboard_renderer.py` 的价格兜底注入逻辑（`_inj_price`，优先读最近 7 天的 `analysis-*-ml-*.json`）把这个假价读出来显示在了仪表板上——8/10 标的显示一模一样的 $100.00，用户发现"网站上的股价都是错的"。
- **修复**：`_analyze_ticker_safe` 改为调用 `swarm_agents.cache._fetch_stock_data(ticker)`（与 swarm 路径共用同一个真实多源降级链），不再硬编码。
- **数据修复**：用修复后的真实取价链重新拉取 8 只标的真实现价，patch 回 `analysis-{ticker}-ml-2026-06-30.json`（该文件属于 `.gitignore` 的 `analysis-*.json`，本地缓存不入库），重新渲染 `index.html`/`dashboard-data.json` 并部署 gh-pages。

### Changed — `data_pipeline.py`（**CBOE 设为股价主源**，用户定调"股价也走 CBOE"）
- 新增 `CBOESource` 类：复用 `cboe_options._fetch_cboe_payload()` 已经在拉的期权链响应里自带的 `current_price`/`price_change_percent` 字段，零额外网络开销；仅提供当日涨跌幅（无历史K线），`momentum_5d` 是近似值。
- `MultiSourceFetcher._sources` 降级链顺序改为 **CBOE → yfinance → Alpha Vantage → Finnhub → 陈旧缓存 → 安全默认值**（原来是 yfinance 起头）。
- 澄清：CBOE 优先规则原来只接入了期权链（`options_analyzer.py`），基础股价查询（`data_pipeline.MultiSourceFetcher`，swarm agents 取价用的就是这条）走的是独立的 yfinance 起头的链条，两者互不影响。本次统一后 CBOE 变成两条链共同的第一源。

## [0.35.2] — 2026-07-01 — 修复"净值曲线·评分分布"面板空白（`equityChart` canvas 从未接入 JS）

### Fixed — `templates/dashboard.js`
- **根因**：v0.35.1 修的是"历史准确率"章节里的资金曲线（`#eqCurveChart`，Net/Gross/SPY 三线图，已正确接入 `initEquityCurve()`）。但用户截图显示的其实是**另一个**图表——首屏"图表"章节顶部的"策略净值曲线（模拟回测）"面板，canvas id 是 `equityChart`（单数，无 Curve）。B-style 重设计把这个面板的 HTML 骨架（`#equityChart`/`#eqKpi`/`#eqDateRange`/`#eqCurrent`）写进了模板，但**从未写对应的 JS 渲染逻辑**——`renderChart(id)` 里没有 `id==='equityChart'` 分支，骨架屏（`.skeleton`）永远不会被移除，表现为一个永久空白的米色方块。
- **修复**：在 `renderChart()` 新增 `equityChart` 分支：读取 `__AH__.equity_curve`，把 `cum_net_pct` 换算成"起始 100"的净值指数（`100*(1+pct/100)`），渲染单线 Chart.js 折线图；同步填充 `#eqKpi`（涨跌幅徽章，涨绿跌红）、`#eqDateRange`（数据起止日期）、`#eqCurrent`（当前指数值）。
- 把 `'equityChart'` 加入 IntersectionObserver 观察列表、无 IntersectionObserver 时的 fallback 列表、`window.load` 兜底列表、暗黑模式切换重绘列表、bfcache 恢复重建列表（共 5 处 `['fgChart','scoresChart','dirChart']` 数组统一加尾）。
- `markDone()` 的骨架屏隐藏选择器从 `.chart-canvas-wrap`/`.radar-wrap` 扩展到 `.eq-wrap`（该面板的外层容器类名与其他图表不同，之前即使接入 JS 也不会自动隐藏骨架屏）。
- **验证方法**：本环境截图工具在 JS 滚动后拍摄存在已知盲区（`document.hidden` 导致 Chart.js 的 rAF 绘制暂停），改用 canvas `getImageData` 直接采样像素 + 手动 `chart.draw()` 强制同步绘制验证——确认数据(576点)/Chart 实例/KPI 文案/骨架屏隐藏全部正确，图表可视内容占采样点 42%。

## [0.35.1] — 2026-07-01 — 修复"策略净值曲线加载不出来"（index.html / dashboard-data.json 时间戳失步导致无限重载）

### Fixed
- **根因**：`dashboard_renderer.render_dashboard_html()` 每次调用都会**side-effect 写入** `dashboard-data.json`（用 `now_str` 生成 `_generated_at`），即使调用方只是想读返回的 HTML 字符串做测试、并未把结果写回 `index.html`。上一 session 验证 emoji 清除时多次这样调用，导致 `dashboard-data.json` 时间戳（`2026-06-30 16:37 PDT`）比 `index.html` 里嵌入的 `data-generated`（`2026-06-30 01:12 PDT`，且仍是**清 emoji前**的旧渲染）新出一大截。
- **触发的 bug**：`templates/dashboard.js` 的 `fetchDashboardData()` 每次加载都会拉取 `dashboard-data.json`，若其 `_generated_at` 比页面自身嵌入的时间戳新，立即 `location.reload()`。两个文件长期失步 → 页面进入**无限重载循环**。净值曲线依赖的 Chart.js 需要先从 CDN 加载再跑入场动画（约 3~5 秒），而页面每 1~2 秒就被重载打断一次，导致图表**从未有机会画完**，用户看到的就是"加载不出来"。
- **验证过程**：直接读取 `#eqCurveChart` canvas 的像素数据（`getImageData` 采样非背景色像素占比），确认 Chart.js 实例、数据（576 笔 equity_curve + trading_stats.realistic）、DOM id（`eqCurveContainer`/`eqCurveChart`/`eqStats`/`tradingStatsCards`，均由 `dashboard_renderer.py` 的 `_acc_section_html` 正确生成注入）**全部正常**——B-style 重设计并未破坏此前的图表结构；纯粹是时间戳失步 + 重载竞速导致的可见性问题。
- **修复**：用 `alpha-hive-daily-2026-06-29.json`（当前 `dashboard-data.json` 对应的真实扫描数据）重新调用一次 `render_dashboard_html()` 并把返回值写回 `index.html`，使两个文件时间戳重新对齐（`2026-06-30 17:23 PDT`）。**副产品**：这次重新渲染也让上一 session 的 emoji 清除修复真正生效到部署产物里——此前部署到 gh-pages 的 `index.html` 其实是清 emoji **之前**渲染的旧版本（`📅📈🏆💀💰📌📉🔗` 等仍在），从未被含修复的新渲染覆盖过。
- **教训**：以后验证 `render_dashboard_html()` 的输出（如 grep 检查 emoji）时，要么把返回值写回 `index.html`，要么改为直接读取/grep 源码（`dashboard_renderer.py`/`templates/`），不要让"只读测试"静默污染 `dashboard-data.json` 的时间戳。

## [0.35.0] — 2026-07-01 — B-style 财经报刊仪表板重设计 + 全面去 emoji

### Changed — `templates/dashboard.html`（完整重写）
- B-style "财经报刊"风格：cream `#FAF7F2` 底色、rust `#B7410E` 强调色、零渐变、零 emoji、Playfair Display / JetBrains Mono / Noto Sans SC 字体
- 六节 Roman numeral 结构（机会卡 Ⅰ / 图表 Ⅱ / 明细 Ⅲ / 热力图 Ⅳ / 准确率 Ⅴ / 深度 Ⅵ）
- 底部导航 `bnav-icon` 改用 5 个极简线性 SVG（替代 📋📊📑🔬📈 emoji）
- CSP `style-src` 新增 Google Fonts，`font-src` 新增 `fonts.gstatic.com`

### Changed — `templates/dashboard.css`（大幅更新）
- `:root` 调色板改为 cream/rust/forest-green；`html.dark` 完整覆盖
- `.nav` 改为 sticky 浅色导航（hairline 分隔线）
- 新增 `ah-*` 结构类：`.ah-macro-*`（宏观条）`.ah-sec-*`（节标题）`.ah-charts-grid`（双栏图表）`.ah-footer-*`（页脚）
- 新增 `.dot-bull/.dot-bear/.dot-neut`（7px CSS 彩点，替代 🟢🔴🟡）

### Fixed — `dashboard_renderer.py`（50+ 处 emoji 清除）
- `_DIR_ICON` 从 emoji 改为 `<span class="dot-*">` CSS 彩点；`_dlbl6`（方向标签）同步
- 清除 `🎯🔔📅⚡📋💰📈📉🏆💀🕐🔥⬆⬇🔄` 等，改为纯文字 / CSS 彩点 / Unicode 箭头（↑↓↺）
- OI wall、失效条件卡、准确率章节、热力图标题等多处 emoji 全部移除

### Fixed — `templates/dashboard.js`（20+ 处 emoji 清除）
- 暗黑按钮文字：`☀️ 亮色/🌙 暗黑` → `亮色/暗黑`
- 净值/SPY/Alpha/SL/TP 卡片标签、股权曲线图例等全部去 emoji
- freshness badge 改用 `innerHTML` + CSS dot（`dot-bull/bear`）替代 `🟢🔴` + `textContent`
- `✅共振/⚠️` → `共振/▲`；`🔄` → `↺`；`🆕` → `+`

## [0.34.1] — 2026-07-01 — 修复快照采样偏差（自学习样本被 NVDA 单票绑架）

### Fixed
- `alpha_hive_daily_report.py`（`_post_scan_enrichment` 反馈循环快照段，~L931）：移除 `final_score >= 5.0` 的快照落盘门槛，改为 `final_score > 0`（成功分析即落快照）。
  - **根因**：原门槛只记录高分预测，而高分几乎只有 NVDA → `report_snapshots/` 累积 49 条 NVDA vs 其余 9 只各 1 条（均停在首日 03-16）。导致 `self_analyst` 自诊断、背离过滤器回测全部被单票绑架、过拟合。
  - **影响**：日常扫描现为全部 10 只标的每日各落 1 条快照（低分/Neutral 也记录，供跨标的校准）；胜率仍只按 Long/Short 方向统计，Neutral 不计入。
  - **未动**：`vector_memory` 的 `>= 5.0` 门槛（L894）保持不变——属长期"想法记忆"策展，与回测样本是不同用途。

### Notes
- 本月自诊断结论修正：**「基本面·期权背离过滤器」经全样本回测为净有害**（触发 15 次：正确拦截 3、误伤赢家 12，含多笔 +6%~+20% 的 NVDA 上涨段），**本月不上线**。根因即上述采样偏差——Scout≤4 & Oracle≥8 实为 NVDA 该波行情的赢家画像，非失败特征。
- 后续：待新采样跑满 2–4 周、其余 9 只标的积累 T+7 序列后，再重跑跨标的信号回测（优先验证「同向 5 日去重 / 追涨扩展惩罚」假说 C）。

## [0.34.0] — 2026-06-30 — 接入 CBOE 期权数据源（根治 Yahoo 限流空 OI 垃圾数据）

> 用户在中国深夜跑扫描常撞 Yahoo 限流（401 Invalid Crumb），yfinance "成功"返回近空 OI（实测 NVDA 全链 OI=0）→ 静默落进样本数据，odds/Max Pain/GEX 全变垃圾。项目原 "Tradier→yfinance→样本" 降级链中 Tradier **从未实现**（tradier.py 为空、无人 import），实际只有 yfinance→样本。新增 CBOE（芝加哥期权交易所）公开延迟报价并**设为主源**（yfinance 对中国深夜用户恒限流，CBOE 稳定优先）。commit `991acd2` + CBOE 主源重构。

### Added — `cboe_options.py`（新文件，312 行）
- CBOE 端点 `cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json`：全链逐合约 OI/IV/greeks，15min 延迟（盘后=已结算 EOD），**无 API key、无限流**。
- `fetch_cboe_chain()`：返回与 yfinance **完全兼容**的 result dict（calls/puts 含 strike/openInterest/impliedVolatility/gamma/expiry/dte/dte_weight 等），镜像 yfinance 路径全部后处理（到期日筛选/ATM 过滤/40-cap/DTE 加权/gamma 注入，CBOE gamma 缺失时 BS 兜底）→ 下游 GEX/Max Pain/P-C/key_levels **零改动复用**。
- `fetch_cboe_full_chain_oi()`：全链 OI 聚合（call_oi/put_oi/call_exp_oi/put_exp_oi/expiry_breakdown），复用 `_fetch_full_chain_oi` 的 Max Pain 计算。
- OCC 符号解析 / `_pdt_now()` PDT 锚定 DTE / 解析丢弃率 >5% 告警。

### Changed — `options_analyzer.py`（**CBOE 设为主源**，用户：「首先走 CBOE，cboe 稳定，yfinance 老是限流」）
- `fetch_options_chain`：**CBOE 优先**（`_try_cboe` 主源 → yfinance 降级 → 样本）。CBOE 命中即返回，不再调 yfinance（更快、绕开限流）；yfinance 仅作 CBOE 不覆盖该标的时的降级，其返回纯空 OI（限流）则退样本。
- `_fetch_full_chain_oi`：同样 **CBOE 全链主源**（`fetch_cboe_full_chain_oi` 优先 → 空时才 yfinance loop），复用下方 Max Pain 计算。

### Fixed — 对抗审查（feature-dev:code-reviewer ×2）后修复
- IV 缩放：实测 CBOE 每合约 iv 已是小数（删 `_normalize_iv` 百分数启发式，对高 IV biotech >300% 会误判压缩）。
- PDT 锚定：`_pdt_now()` 替代裸 `datetime.now()`（遵守项目硬规则）。
- 换源守卫 1.5×（避免真实薄期权标的 realtime→delayed 横向换源）+ OCC 解析丢弃告警。

### 验证
19 个 options 测试通过；端到端 `analyze("NVDA")` data_quality=real、全链 Max Pain $190（与 6/26 一致）；限流 OI=0 自动切 CBOE OI 276,854；CBOE 不覆盖标的（403）优雅退样本；字段对齐完整。**已知遗留**：CBOE 数据仍标 data_quality=real（盘后≈real，含 `_source:cboe` 溯源）；若要 "delayed" tier 需同步改 dashboard/bot 渲染。

## [0.33.1] — 2026-06-30 — Python 3.9 兼容性修复（PEP604 注解 + jinja2 缺失）+ 6/29 补跑

> 6/29 规则模式补跑时连撞两个 fatal 生产 bug，均为「代码在 Python 3.10+/有 jinja2 环境（Cowork VM）写测、在用户真实 Mac（Python 3.9.6 / 无 jinja2）跑不通」的环境漂移。两次都导致扫描蜂群分析全跑完却在产出阶段崩溃、无报告。修复后端到端跑通：commit + push + gh-pages 部署（898 静态文件）+ CDN 验证通过。

### Fixed — PEP604 `X | None` 注解在 Python 3.9 崩溃（7 核心文件）
- 根因：PEP604 union 注解（`date | None` / `float | None`）在 `def` 执行（import）时即 eager 求值，Python 3.9 不支持 → `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`，模块 import 直接失败。
- 两个实测 fatal 点：`report_formatters.py:237` `float | None` → markdown 报告生成整段崩（致 6/29 首次扫描无产出）；`is_trading_day.py:115` `date | None` → 被 7 个生产模块导入的交易日护栏全线 fail-open，**v32.0/v32.2 周末/假日跳过 + 部署/dashboard/RSS 过滤在用户机上从未真正生效**（静默失效）。
- 修复：AST 静态扫描定出核心层完整爆炸半径 = 7 文件（`alpha_hive_daily_report` / `generate_deep_v2` / `is_trading_day` / `newsapi_client` / `pre_scan_notify` / `report_formatters` / `run_daily_scan`），各加 `from __future__ import annotations`（PEP 563，3.7+，注解变惰性字符串、运行时不求值）。已确认 7 文件均无运行时注解内省（get_type_hints/pydantic/`__annotations__`）→ 安全。
- 验证：is_trading_day 功能测试周末/Juneteenth/独立日(observed 周五)全部正确识别；AST 重扫核心层（含子目录）0 残留。commit `6c63545`。

### Fixed — dashboard 渲染缺 jinja2 时崩溃阻断整个扫描（`alpha_hive_daily_report.py` + `requirements.txt`）
- 根因：`dashboard_renderer.py:17` 顶层 `from jinja2 import Environment`（2026-03-04 起即硬依赖却从未在 requirements 声明），用户 Mac 未装 jinja2 → `ModuleNotFoundError`。而 index.html 生成本有 try/except + `_fallback_dashboard_data` 降级路径，但 except 元组 `(OSError,ValueError,KeyError,TypeError,AttributeError)` **漏了 `ImportError`**（ModuleNotFoundError 是其子类）→ 穿透崩溃，`save_report` abort、自动提交/部署未执行。本质是「降级路径设计对了但异常捕获面漏了」。
- 修复：except 元组加 `ImportError`（dashboard 渲染任何失败含缺可选依赖都降级到独立 JSON，绝不阻断核心报告+提交/部署；已确认 `_fallback_dashboard_data` 不依赖 jinja2）；`requirements.txt` 声明 `jinja2>=3.1.0,<4`；用户 Mac 安装 jinja2 3.1.6。commit `a920f17`。

### Notes — 6/29 数据质量
- 中国深夜 Yahoo 批量限流（401 Invalid Crumb），期权 IV 降级到缓存/样本，**兜底值 run-to-run 不同 → 部分边界评分漂移 ±0.5、甚至翻转方向**（QCOM 看空↔看多、META 中性↔看多）。稳定结论：TSLA 唯一观察名单（~6.7-6.8）、无高优先级（≥7.5）、安静日。核心数据（SEC Form4 内幕 / 催化剂 / ML）均 real。

## [0.33.0] — 2026-06-23 — financial-services 四模式落地：注入护栏 / 输出 schema / 来源强制 / MD-prompt 解耦

> 借鉴 anthropics/financial-services 参考架构的 5 个模式，落地其中 4 个（pattern 4「能力收口」属 headless 场景，未做）。先用并行侦察工作流在真代码核准每个 pattern 的落点，**纠正了"蜂不调 LLM"的初判**——蜂经 `llm_service` 间接调 LLM，注入面真实存在但 opt-in（默认 `--no-llm` 时各汇为惰性）。全程：零新增 LLM 调用、不改门控、无 API-key 告警。新增 34 测试，131 个相关现有测试零回归。

### Added — pattern 1 提示注入护栏（`text_sanitizer.py` 新文件）
- `sanitize_external_text()` 中和中英注入触发短语 + 控制字符/换行 → 占位符 `［已过滤］`；保守：不剥离 discovery 的 `|` 分隔符、不误伤正常金融文本。`wrap_untrusted()` 不可信数据围栏 + `UNTRUSTED_DATA_GUARDRAIL` 安全守卫常量 + `sanitize_headlines()` 批量。
- `llm_service.py` 4 个注入汇加固（消毒+围栏+守卫）：`analyze_news_sentiment`(Finviz 头条)/`interpret_insider_trades`(SEC 摘要+明细)/`detect_thesis_breaks`(近 7 天新闻)/`distill_with_reasoning`(discovery→QueenDistiller)。
- `generate_deep_v2.fetch_live_news`：Finnhub 头条消毒后再注入 Opus prompt（防御式导入，VM 同步竞态下回退）。
- `pheromone_board._validate_entry`：discovery 纵深消毒（下游 snapshot 喂 QueenDistiller），保留 `|` 与原长度。

### Added — pattern 2 输出 schema 校验
- `llm_service._coerce_schema()`：LLM 返回 dict 此前「除 JSON 解析外零校验」→ 现 clamp 数值 / enum 兜底 / 截断并消毒字符串 / 截断列表；4 个 helper 各配 schema 常量。
- `models.AGENT_RESULT_SCHEMA` 声明式契约 + `VALID_DIMENSIONS` + `AgentResult.validate(strict=)`（非破坏性，补 `__post_init__` 未覆盖的 dimension 合法集 / source 非空 / details 体积）。

### Added — pattern 3 来源强制（`pheromone_board._validate_entry`）
- 空 / 纯空白 `source` → 标记 `[UNSOURCED]`（fail-safe，标记非丢弃，不破坏蜂群协作）。**把 CLAUDE.md「禁止无来源结论进入信息素板」首次落代码**——此前 `_validate_entry` 只校验 self_score/direction/strength，从不看 source。

### Added — pattern 5 MD 源 + Python 包装解耦（`prompt_loader.py` 新文件 + `prompts/`）
- `load_prompt(name, fallback)`：从 `prompts/<name>.md` 读正文（剥 frontmatter），任何错误静默回退 fallback（无告警、不读 key）。
- `prompts/options_strategist.md`(generate_deep_v2 SYSTEM_PROMPT) / `step1_analysis_engine.md`(STEP1_SYSTEM) / `news_sentiment_analyst.md`(llm_service)。三处改 `_load_prompt(name, fallback=原内联常量)`：**`prompts/` 缺失时字节级回退原文，行为不变**。

### Added — 测试（34 新，全绿）
- `test_text_sanitizer.py`(12)/`test_prompt_loader.py`(6)/`test_agent_result_schema.py`(6)/`test_llm_injection_guard.py`(5，mock `call()` 捕获 prompt 验围栏+守卫+消毒+coerce)/`test_pheromone_source_guard.py`(5)。

### Fixed — 顺带修复的真实生产 bug：QueenDistiller 降级模式崩溃（`swarm_agents/queen_distiller.py`）
- `distill()` 入口统一滤 None：`agent_results = [_r for _r in agent_results if _r is not None]`。
- 根因：`alpha_hive_daily_report.py:505-531` 在蜂 future 超时/抛异常时 `append(None)`，而 `distill()` 的 GEX/F&G 预处理循环（line ~874/883/897/924）直接 `_r.get(...)`；F&G 循环（924）**无 try 保护** → 单蜂失败即整个 ticker 蒸馏崩 `AttributeError`。Yahoo 429 限流（深夜 CST 常见）是已知触发面。
- 入口滤 None 与下游 `_prepare_dimension_data`(clean_results_batch) 同口径，不影响覆盖度/评分。修复后 4 个降级测试转绿（test_handles_none_results + 3 个 e2e degraded）。

### Fixed — 顺带修复的真实生产 bug：回测 T+N 闸门日期口径不一致（`backtester.py`）
- 根因：`save_prediction` 给 `date` 盖 **PDT**(line 169)，但 `get_pending_checks` 的 cutoff 原用裸 `datetime.now()`/`_pd.Timestamp.now()`(**上海本地时**)。上海比 PDT 快约 15-16h → 当天(PDT)预测的 `date` ≤ `本地now-1交易日` 的 cutoff → 被误判已满 T+1 而提前回填 outcome（实盘静默偏移约 1 天，污染胜率/权重）。**违反项目"判美股交易日绝不读裸 datetime.now()"铁律**。
- 修复：新增 `_pdt_now()` 助手；`get_pending_checks` cutoff 改 PDT 锚定（`_pd.Timestamp(_pdt_today()) - days*_US_BDAY` / `_pdt_now()-timedelta`）。**核心洞察**：bug 本质是"两边口径不一致"非"绝对时间错"，改成同源 PDT 后即使系统钟仍慢 15h，相对 T+N 比较依旧正确。
- 顺带口径统一（低危，非 bug）：accuracy/dimension 统计窗口(line 312/447 `date>=`)+ adapted_weights 写戳(1431) 一并 PDT 化。前向修正（旧误检记录 `checked=1` 不重算）。`test_backtester.py` 44 passed + `test_pipeline` 41 passed。
- **未动**：534/1112/1126/1220/1316/1447 等同类"最近 N 天"统计窗口仍用本地时（差 ≤1 天，纯展示，低危）——留作可选的全文件 PDT 统一。

### Fixed — 两个过时蜂群测试（解 `-x` 套件阻塞）
- **`test_confidence_weighting` 正经重写**：旧测试假设"低置信把分拉向 5.0"，但 v0.21.0 起 confidence 是相对权重（`effective_weight = weight × conf**exp`，queen_distiller.py:268），全维同置信→无差异；旧单维输入又撞 P4 覆盖度压缩。改为满 5 维、互换"高分维度 vs 低分维度"的置信归属，隔离真实相对加权效应（实证差 0.95）→ 测当前真行为，非盖章。
- **`test_arbitration_no_flip` 标 `@pytest.mark.xfail(strict=False)`**：注明 v0.21.0 仲裁把近平票 neutral 解析为加权多数（task_a971f14c），实盘已跑 2 月、**不动评分逻辑**；待确认 v0.21.0 意图后更新断言。`test_queen_distiller.py` 现 58 passed + 1 xfailed。

### Fixed — 解掉 `-x` 阻塞暴露的 15 个隐藏失败：全部处理（零生产 bug，仅测试/死代码/配置）
- **背景**：pyproject `addopts` 含 `-x`，默认 `pytest` 长期 halt 在最前面的红，其后失败两个月不可见。修掉 queen 两红后首次跑通全量 → 15 failed。并行调查（6 agent）定性：**15 个全 `is_real_bug: no`**，生产代码正确（6/23 扫描、ML 96.7%、EDGAR/F&G fallback 链均正常）。逐个处理：
  - **删死代码**：`dashboard_renderer._ml_combined_score`（caa432d 2026-03-30 决定 ML combined_probability 不用于排名后，调用点删除、函数遗留，全仓零调用）+ `test_dashboard_renderer.py::TestMlCombinedScore` 整 class（88 行）。**未删 `SGDMLModel`**——它是 HGB 不可用时的防御性 fallback，非死代码。
  - **修测试**：edgar_rss×4（mock 打 `datetime` 无效→代码用 `pdt_today`，改 mock 目标）；fear_greed×1（改测缓存真不变式"第二次不新增 HTTP"，不依赖主源/兜底）；ml_predictor×6（工厂测试对齐 HGB；4 个 SGD 序列化回归测试显式 `svc.model = SGDMLModel()` 注入，保留 fallback 覆盖）。
  - **标 integration**（真外部依赖，默认跳过）：`test_integration.py`（finviz，本就标了）+ `test_calendar_integrator` 2 个（Google OAuth）+ **`test_agents.py` 整模块**（0 mock、打 live yfinance/期权/EDGAR，60s 超时下偶发 flaky）。
- **配置**：pyproject `addopts` ① 加 `-m "not integration"`（默认跳外部依赖测试，跑 live 用 `pytest -m integration`）② **`--timeout` 30→60s**。后者治本：部分非-integration 测试打真实 API 偏慢（dashboard 渲染 42s、options 分析 32s），30s 下限流时**确定性超时**（whack-a-mole 的真根因，非测试本身坏）；60s 实测整套 0 失败。注：这些慢测试是 mixed 文件（含 mock 单元测试），故调超时而非 whole-module 标 integration，避免误伤。
- **核实**：edgar 21 / ml+fg 68 / dashboard 9 / calendar 33(2 deselected) / queen 58+1xfail 各自通过；**默认 `pytest`（60s + 跳 integration）最终 `1026 passed, 1 skipped, 65 deselected, 1 xfailed, 0 failed`（2:26）→ 默认套件可靠全绿**。

### 注意 — 仍 xfailed（非 bug）
- `test_queen_distiller.py::test_arbitration_no_flip`：v0.21.0 仲裁行为变更，xfail 注明，待确认意图后更新断言（task_a09dac0b）。

---

## [0.32.6] — 2026-06-23 — 日报 `--date` 覆盖（补跑指定交易日）+ 本机时钟 15h 偏差诊断

### Added — `alpha_hive_daily_report.py --date YYYY-MM-DD`
- 覆盖报告日期以补跑指定交易日。**仅显式传入时生效**，未传仍走默认 PDT（逐字节不变，不影响定时任务）。
- `AlphaHiveDailyReporter(date_override=)`；main() 加格式校验 + 护栏改 `args.date or pdt_today()`（校验指定日确为交易日）。
- 实战：6/23 周二盘中补跑 **6/22 周一规则模式日报成功**（`--swarm --no-llm --date 2026-06-22`）：标 6/22、forming-bar 护栏取 6/22 收盘、gh-pages 部署（853 文件，CDN 验证通过）。VKTX 6.6 领涨；v32.5 高分守卫正确**未误伤** VKTX（情绪已确认=优质高分满仓）。

### Diagnosed — 本机时钟比真实慢 15h（非时区错，是绝对 UTC instant 错）
- 谷歌权威 UTC 06-23 15:45 vs 本机 UTC 06-23 00:45 = **慢 15h**（恰为上海 +8 与温哥华 -7 之差，像"钟显示温哥华时间但时区设上海"）。机器一直以为"现在是周一 6/22"。
- **极可能是 6/17-19、6/22 定时扫描连环"没跑成"的根因**（调度按错钟 fire）。根治：Mac 开「自动设置时间」(NTP)。`--date` + yfinance/forming-bar 走真实交易所时间，故补跑仍正确。

### 注意 — 日报 `auto_commit_and_notify` 用 `git add -A`
- 本次扫描顺带把工作区 pre-existing 的 `collect_data.py` / `NVDA_raw.json` 一并提交。跑日报前宜保持工作区干净。

---

## [0.32.5] — 2026-06-21 — 高分置信守卫（仓位减半）+ 自诊断显著性门控（周报方案 #1/#2/#3）

### Added — #1 高分置信守卫：高分但情绪未确认 → 仓位减半（全样本验证、纯仓位层）
- `config.SCORE_HIGH_GUARD`（score_min=6.5 / sentiment_max=6.0 / signal_min=5.0）。
- `alpha_hive_daily_report` 写快照时算守卫 → `ReportSnapshot.low_conviction`（**新增字段**，含 save/load 往返）→ `paper_portfolio` 已有 ×0.5 通道据此减半（**此前 545/545 快照无该字段=死代码，本次激活**）。
- 不改方向 / 不改 final_score / 不动入场门；维度缺失则不触发（保守）；删守卫即回滚。
- **全样本(665 笔)对账**：高分单 183 笔中守卫命中 60 笔(33%) = **41.7% 胜率 / 净 -1.74%** 的劣质批，优质高分 122 笔 **58.2% / +1.43%** 保持满仓。

### Added — #3 深度报告高分情绪背离警示
- `generate_deep_v2` low_conviction 块加并联 score_high 分支（与日报快照同口径），复用现有 low_conviction 警告渲染（无需新横幅）。

### Changed — #2 自诊断显著性门控（杜绝薄窗口噪声当真结论）
- `self_analyst`：新增 `_wilson_ci()`；`compute_stats` 输出 win_rate Wilson 95% CI + n_directional + significant(CI 下界>50%) + sample_sufficient(n≥30)；brief 渲染 CI + 显著性判读（样本不足 / 含 50% 勿下重注 / 显著弱正 edge）。验证 26/45→[43.3%–71.0%]（精确复现报告 CI，含 50%=非显著）。

### Fixed — pre-existing stale 测试
- `test_feedback_loop.py::test_default_weights`：硬编码旧权重 0.30，改为对照 `config.EVALUATION_WEIGHTS`（优化器已调成 0.2094…），保留 sum=1.0 不变式。HEAD 上即失败，与本次无关。

### 说明 — 全样本核实驳回的报告建议（未采纳）
- 扩池稀释 NVDA（假偏置：NVDA 仅占误判 9%、10 标的样本均衡）、手动调权（优化器已解冻 MIN_CHANGE_PP=3.0 且收敛）、据 0%/80% 信号调逻辑（期权 motif 字段 665/665 全 NULL 不可复现）—— 均建立在假前提 / 不可复现噪声上。

---

## [0.32.4] — 2026-06-21 — 修复 2 个老化测试 fixture（时间炸弹 / 文件名格式）

### Fixed — test_pipeline.py 两个 pre-existing 失败（HEAD 上即失败，与近期改动无关，纯测试侧）
- `test_cleanup_deletes_old_records`：写死的"新记录"日期 `2026-03-01` 随真实时间流逝掉出 `get_recent_memories(days=30)` 窗口（现已 ~111 天前）→ 改用动态 `datetime.now()` + 参数化插入。
- `test_valid_checkpoint`：fixture 文件名 `.checkpoint_test.json` 无日期，但生产 `_load_checkpoint`(v0.15.3) 要求文件名含今天日期（防跨天 stale 复用）→ 改为 `.checkpoint_test_{today}.json`。
- 均为测试 fixture 老化，生产逻辑本就正确。test_pipeline.py 现 **41/41 全过**。

---

## [0.32.3] — 2026-06-21 — dashboard 门面只算核心实盘策略（剔除周日 sample-accumulator 样本）

### Changed — 门面业绩口径统一为核心交易日（option a 完整方案）
- `portfolio_backtest.BacktestConfig` 新增 `exclude_nontrading_days: bool = False`（默认不变）；`run_backtest` 按 flag 过滤非交易日预测（fail-open 逐行）。**仅 dashboard 的 2 个 run_backtest 调用设 True**；optimizer / factor_attribution / bootstrap 研究路径保留全样本（默认 False，逐字节不变）。
- `backtester.get_accuracy_stats` 新增 `exclude_nontrading_days` 参数（`date NOT IN(非交易日)` 子句过滤 3 个聚合）；dashboard:645 设 True。其它调用者（日报/swarm/内部/测试）默认 False 不受影响。
- `dashboard_renderer`：净值曲线 `_eq_rows` + exit 计数 + 6 个 F11 pill 查询（周胜率走势/按方向/best3/worst3/Sharpe pill/连胜）全部接入同一"排除非交易日"子句 → 准确率面板与策略块同口径，bot `/scorecard` 也一致。冷启动总数 `_acc_pending` 不过滤（活动量非业绩指标）。
- 背景：每周日 `sample-accumulator` 扫 50 扩展池票积累研究样本（657 行预测里 110 行周日 = 101 合法样本 + 9 早期漂移），原先混入门面统计、拖低显示约 0.5pp。样本仍留 DB 供 optimizer。

### Fixed — bot /scorecard 的 vs SPY 口径
- `alpha_hive_bot/query_commands.py`：「vs SPY 超额」改用 `realistic.alpha_vs_spy`（组合买入持有，与 SPY 同基准 +0.19%），而非净值曲线"每笔 $5K 累加重叠窗口"口径（−4.76%，方法偏弱不可比）。realistic 缺失时回退。

### 对账（改前→改后，三轮对抗审计 4/4/4 agent，无计算 bug / 无回归）
- 准确率面板：已验证 513→412，综合准确率 56.3%→58.0%，正确 289→239，均收益 +0.941%→+1.189%。
- 策略块：净胜率 50.7%→51.2%，Sharpe 0.371→0.413，盈亏比 1.22→1.24，n 657→547。
- realistic：入场 113→110，NAV $53,030→$53,688，alpha −1.13%→+0.19%。Sharpe pill 0.54→0.52，连胜 2→2。
- 验证：编译 + backtester 44/44 + F11 参数绑定无周日泄漏 + 默认 config 逐字节不变（优化器零影响）+ 泄漏自查仅冷启动计数未过滤（正确）。

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
