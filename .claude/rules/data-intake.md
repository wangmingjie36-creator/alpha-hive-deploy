---
paths:
  - "ledger_io.py"
  - "earnings_calendar.py"
  - "yf_daily.py"
  - "price_metrics.py"
  - "day_health.py"
  - "scan_coverage_gate.py"
  - "observation_archive.py"
  - "observation_layer.py"
  - "data_pipeline.py"
  - "signal_code_fingerprints.py"
  - "buzz_v1.py"
  - "buzz_v1_channels.py"
  - "swarm_agents/buzz_bee.py"
  - "swarm_agents/chronos_bee.py"
  - "experiments/fingerprint_coverage_audit.py"
  - "experiments/dim_ic_*"
  - "tests/test_ledger*.py"
  - "tests/test_earnings_calendar.py"
  - "tests/test_yf_daily.py"
  - "tests/test_price_metrics.py"
  - "tests/test_price_observations.py"
  - "tests/test_day_health.py"
  - "tests/test_observation_archive.py"
  - "tests/test_signal_code_fingerprints.py"
  - "tests/test_buzz_v1*.py"
---

# 取数唯一入口与观测层：ledger_io / earnings_calendar / yf_daily / day_health / H1 观测档案 · 按路径加载

> 读 / 改上面 `paths:` 匹配的文件时 Claude Code 自动加载。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来（「核心组件指针」）。⚠️ 「新代码一律走它」那几条在根目录各留了一行：**新模块的文件名多半不在上面的 `paths:` 里，触发不了本文件**。

- **账本读写** `ledger_io.py`（v0.45.448；v0.45.452 加整文件文档 `write_json` / `load_json`）：加锁（目录 flock，同进程可重入）+ 原子替换 + 严格序列化（拒 NaN / 非对象）+ 写后回读；读到坏行 / 坏文档抛 `LedgerCorrupt`（外部损坏 ⇒ 停下、从备份恢复，**不跳过、不当新账本**——`meta.json` 里存着现金）。失败在它**源头**登记（`failures()` ⇒ status.json ⇒ P2），调用方吞异常也照红。⚠️ 新账本 / 状态写入**一律走它**，可再生缓存走 `hive_logger.atomic_json_write`；守卫 `tests/test_ledger_writes_go_through_ledger_io.py` 按**写入落点**认（不看函数名），存量 84 处逐条按类别登记在 `WRITERS`，「待迁移」类有上限、只许减
- **财报日唯一入口** `earnings_calendar.py`（v0.45.500）：唯一日历解析器 + 读取入口（读本轮 / 已落盘的 ChronosBee `details.earnings_calendar` 按日观测，**不打网**）；扫描、卖权 / 跨式账本、展示一律经它。`EarningsWatcher.get_earnings_date` 与 `earnings_cache/{T}_date.json` 只服务 `--check-earnings`，**不得被别处读**（守卫 `tests/test_earnings_calendar.py::TestSingleEntry`）。ChronosBee 评分口径（丢当天财报）刻意不动——改它截断维度 IC 协议 H2
- **yfinance 日线唯一入口** `yf_daily.py`（v0.45.652；v0.45.658 起两个 API 都收）：`yf_daily.history(tk, source=, **req)` → `(日线, 最近一场缺了)`；`yf_daily.download(yf, tickers, source=, **req)` → `(帧, {票: 最近一场缺了})`——内部 actions=True 取、去掉「公司行动空行」后**还原成 actions=False 的那一帧**（按日期用的调用方逐位不变）。判据 / 按行号删 / 首末段只有 `price_metrics.event_row_layout` / `drop_positions` 一份（空行只会在首段 / 末段）。末段按「剩下的末根是不是**最近一场已收盘**的交易日」判（`latest_completed_session`，盘前跑除息日照用前一场）；为真 ⇒ 用末根当今天的调用方当取不到。⚠️ **新代码读 yfinance 日线一律走它**（`SOURCES` 登记调用点名、`tests/test_yf_daily.py::POLICY` 逐处写末行策略 refuse / ignore）；`.download(` 直调零例外，`.history(` 直调只许 `EXEMPT` 四类（原样落档 / 645 旧回落 / 要事件列 / 1m 日内，只许减）；**别用 `actions=False` 代替**、调用方别传 `actions` / `keepna`。预取包把「最近一场缺了」的票记在 `history_missing`，Rival / Scout 访问器据此当取不到、不直连重取。H1 三字段四条路径仍是 645 的 `drop_event_only_rows`（末段即缺，用户 10-25 定、冻结层对拍）。Guard 世代印记登记在 `guard_bee.GENERATION_MARKS`，**出错路径也写**（`_with_generation_marks`；新印记只写成功路径 ⇒ 守卫红）。守卫 `tests/test_yf_daily.py`
  - **`yf.download` 进程内串行 + 身份校验**（v0.45.773，评分冻结期真 bug 例外）：yfinance 1.2.0 的 `download()` 把结果攒在模块级全局 `shared._DFS`，并发调用互相串数据（请求 VZ 拿到 SOXX 的完整帧），与限流无关（10-29 串 25 次、429×0）。根因修复 = `yf_gate.install()` 给 `yf.download` 套进程级锁（`_gated(..., serialize=True)`，等锁有上限 `DOWNLOAD_LOCK_TIMEOUT`，只锁 download、`Ticker.history` 仍并行）；`yf_daily.download` 对所有 `source` 统一做身份校验（返回里有没请求的票 ⇒ 丢掉、标缺、计 `identity_mismatch`），`alert_manager._check_yf_download_integrity` P2。装了锁后 `identity_mismatch` 应恒为 0——非 0 就是锁被绕过（闸门没装 / 拿着补丁前的引用直调）或 yfinance 改了实现。⚠️ 并发线程里**不要**绕过闸门直调 `yfinance.download`；单票并发取数优先用 `Ticker.history`（每个实例自己的状态）。守卫 `tests/test_yf_download_serialized.py`（含「不装锁必串」的正对照）/ `tests/test_yf_daily_identity.py` / `tests/test_yf_download_integrity_alert.py`
- **日健康判定** `day_health.py`（v0.45.598）：同一数据源族（yfinance / CBOE）≥2 个 Step 12 字段同时低于覆盖线 ⇒ `source_outage`；纯函数、读 `.swarm_results_<日期>.json`，**不落盘新产物**。扫描末尾写 `scan_timing.extra.day_health` → `alert_manager._check_day_health` 每个故障源一条 P1（没统计 / 取值不认识 ⇒ P2）；两份前瞻检验 `run()` 显示故障日，并列报告「去掉 source_outage 日」的**敏感性分析**（v0.45.605 修订 3，定义指纹冻结在 `day_health.REGISTERED_FINGERPRINT`（含 `MIN_TICKERS=10` 小样本下限，v0.45.608 同日勘误）；不触发动作、主分析不变；2026-10-14 经用户决定**不排除**；改 `scan_coverage_gate.FIELDS` 会让指纹变、守卫红——要新开修订）。⚠️ alert_manager（Step 6）先于 Step 12 跑，想让后面步骤的结果进告警必须由扫描进程自己算进 `scan_timing.extra`。故障日 ≠ 协议排除日（`FORWARD_TEST_EXCLUDED_DAYS`），排不排是协议修订。守卫 `tests/test_day_health.py`
- **H1 原始观测档案** `observation_archive.py`（v0.45.568，buzz_v1 根治阶段 1）：喂 Buzz 四个情绪通道的原始取数（Reddit 两张榜 / 新闻条目 / 热搜表 / F&G）在取数处**内容寻址**落档（`PATHS.observation_archive`），每个分数经 `details.observations` → `observation_links` 表（与 `buzz.comp.*` 同一事务）指出它用的是哪一份。只在扫描进程 `enable()` 后写盘，**只存、不改值**；缺链接 / 不在档 / 落盘失败 ⇒ P2。⚠️ 给 H1 通道加取数源 = 在 `KINDS` 登记 + 取数处 `observe`，否则阶段 2 的冻结通道层重算不了。守卫 `tests/test_observation_archive.py`
  - **观测层** `observation_layer.py`（v0.45.580 起，取数与解释分家的第一块）：行情日线（`yf_history`）与交易所时钟（`spy_last_minute`）从这里取、取到即落档、返回内容不变；`data_pipeline` 三条路径把用了哪几份登记进 `StockData.observations`（回落补上的登 None = 用了但没存）。版本号 `observation_archive.LAYER_VERSION`；⚠️ **改它的源 / 参数 / 兜底顺序 = 新版本**（buzz_v1 固定用旧版本，双版本接线在「观测层版本化」那步做），在那之前它整份在输入层指纹 `price.fields` 里。守卫 `tests/test_price_observations.py`
  - **取数缓存存原始观测**（v0.45.591，观测层版本化第一块）：新闻 / F&G 缓存存「取数结局」（种类 + 原始内容 + 取数时刻 + 新闻主源结局，`observation_layer.cache_*`，文件名带观测层版本），命中时用与新取数同一个解释函数重算并补档；Reddit 单票派生缓存已删。⇒ MCP / 手动跑先填了缓存，扫描命中照样档案完整、不再白丢桥接日。⚠️ 情绪取数模块加缓存**只许存原始观测**（守卫 `tests/test_observation_archive.py::test_sentiment_modules_only_cache_raw_content`）
  - **输入层指纹守卫** `signal_code_fingerprints.py`（v0.45.554；v0.45.671 起覆盖范围**自动推导**）：单元指纹 = 定位代码 + 依赖闭包（跨文件追引用，config 项精确到读过的键，缺省一律算进去；v0.45.676 起 `self.方法` 按继承链解析、config 值里的名字与写在任何位置的就地修改也追；v0.45.679 起类内给 `self.x` 的赋值也算，只有从类外注入的实例属性推不出来）⇒ H1 相关代码**新读一个配置项 / 改一个被调用的辅助函数**也会红、要用户判；不改值的基础设施只经 `EXEMPT_DEPS`（必须写理由、过期即红）豁免，**别往里塞来消红**。看某单元盯着什么：`--deps <单元>`；定义一变要在窗口内各扫描日提交上重算审计（`experiments/fingerprint_coverage_audit.py`）。守卫 `tests/test_signal_code_fingerprints.py`
  - **冻结通道层** `buzz_v1_channels.py`（v0.45.583，根治阶段 2）：原始观测 → 7 个通道，逐字抄窗口起点 10-12 的代码（纯 Python、不 import 生产代码 / config / pandas，数值逐位复刻 numpy 成对求和）；`buzz_v1.py --bridge` 逐日核对它与入档通道逐位相等，结果进维度 IC 进度行（阶段 4 前只是进度）。⚠️ 照抄的含义是**不修**窗口起点的怪行为（新闻时效加权死代码、F&G 兜底加密指数……），要修 = 新定义重新登记；阶段 4 前生产改了这些通道算法 / 常数 ⇒ `tests/test_buzz_v1_channels.py` 的常数对照与差分对拍开发期红
