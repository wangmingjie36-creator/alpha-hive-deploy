# 卖权行权价 · GEX 环境路由检验 · 预注册（v0.45.333）

**状态**：已登记（2026-09-23）。**登记时间 = 本文件首次提交进 git 的时间**。此后改动任何规则都属「事后」，见 §10。
登记前经过三轮评审与变异检验（2026-09-23、2026-09-24 最终评审、2026-09-26 delta 评审），据此改过的规则逐条列在文末「登记前定稿记录」（改时账本零行，没有可偷看的效应量）。
**机器可读常量**：文末 §13 的 `prereg-constants` 块；代码侧唯一真相是
`sell_strike_ledger.PREREG` 与 `sell_strike_candidates` 的路由常量，由
`tests/test_sell_strike_ledger.py::TestPreregPinned` 解析本文件逐项核对（文档不许漏键、不许多键、值必须相等）。
**执行器**：`sell_strike_ledger.assess()`（就绪闸 + 未就绪不算效应量 + 就绪后由日报钩子跑一次并冻结的检验）；
`sell_strike_ledger.block_permutation_p()`（统计量与置换）。

> 本文件是公开信息研究的方法学记录，**不构成投资建议**。账本只记录与结算，不开仓、不进任何评分、不上公开网站。

---

## 0. 起草人看过什么（盲化披露）

- 账本在登记当天**从零开始**：起草时不存在任何已记录 / 已结算的行，没有可偷看的效应量。
- 看过的只有方法学证据（下节）与 critic 的数值核对（旧 `DealerGEXAnalyzer` 的 flip / vanna / 单位三处问题）。
- 路由规则 v1 的常量（3% 缓冲、0.20 / 0.10 两档）是**事先按证据等级拍定**的，不是在任何本仓数据上调出来的。

## 1. 动机与证据等级

卖期权选行权价，能站得住的是 delta / N(d2) / 期望波动；GEX 最多是「环境过滤器」。本检验只问后者有没有用。

| 用法 | 证据等级 | 本系统怎么用 |
|---|---|---|
| 到期 ITM 概率 = N(d2)（不是 \|Δ\|） | **A**（定价数学） | 梯子按 \|Δ\| 选档，同时记 N(d2) 与 σ 距离；校准见 §8 |
| 卖权溢价长期为正（VRP） | **A**（指数）/ 个股偏弱 | 不检验；它是风险中性概率高估实际 ITM 频率的原因 |
| 做市商净多 gamma → 随后实现波动更低 | **B+**（指数与个股均有，幅度中等） | 路由的理论依据：负 gamma 环境更该退后 |
| zero gamma / flip 作为分界线 | **C**（依赖模型，各厂商口径不一，无独立检验） | 路由用它，所以才需要本检验 |
| call / put wall 是硬支撑阻力 | **D**（无同行评审，厂商统计无基线） | 只展示，不进路由、不进检验 |

⚠️ **个股符号问题**：公开 OI 看不出谁买谁卖。朴素口径（call 记 +、put 记 −，即假设做市商多 call 空 put）
在个股上最弱——Garleanu-Pedersen-Poteshman (2009) 发现终端用户在个股期权上往往是净卖方，
put 侧符号可能整个反了。这正是本检验可能得到零结果的一个先验理由，写在前面。

## 2. 假设

四个检验，每个 **tenor × side** 一个：

| 编号 | tenor | side | H1（单侧） |
|---|---|---|---|
| H1-m-put | monthly | put | flag_put=True 的单位，在**基线档 0.20Δ** 的单腿 short_put `pnl_over_credit` 比 flag_put=False 的**更差** |
| H1-m-call | monthly | call | 同上，short_call、flag_call |
| H1-w-put | weekly | put | 同上 |
| H1-w-call | weekly | call | 同上 |

- 度量 `pnl_over_credit` = 到期每股盈亏 / 收到的权利金（卖按 bid）。1.0 = 全额收下，负值 = 亏损超过权利金的倍数。
  到期盈亏只用**到期日收盘**的内在价值（`sell_strike_candidates.structure_pnl_at_expiry`）。
- **结果永远取基线档 0.20Δ，不随 route 变**：问的是「被 flag 的环境里，同样的 20Δ 卖权是不是更容易出事」。
  route 只决定展示时取梯子哪一档；因为梯子每档都记了，置换 flag 后的反事实结果可以直接从同一份梯子读出，
  零分布有定义（这是旧设计「按 route 选的行权价比胜率」做不到的）。
- 零假设：同一天里，flag 与结果无关。

## 3. 独立单位与去重

- 候选行：`status == "recorded"` 且 `settle_status == "settled"` 且 route 两侧都不是 `unavailable`
  （含扫描合约数不足 `MIN_SWEEP_CONTRACTS`，见 §9），且报价是**已收盘那一场的收盘后快照**：
  `underlying_price_source` **不在** `excluded_underlying_price_sources = ("cboe_stale_intraday", "cboe_intraday")`
  里、且 `session_live` 不为真。前者是收盘后仍拿到盘中生成的文件（09-22 生产 30 只里 4 只），后者是盘中就读了
  （例如盘中手动跑日报，编排器的 1330 闸管不到）——两者权利金都是盘中报价，而同一份盘中报价只因读取时刻不同
  就一进一出检验是不可接受的。按行判：同单位次日的正常行照样可以当代表行（读取时刻 / 文件陈旧是数据源的属性，
  与结局无关）。这类行照记，`run_for_date` 的
  `per_tenor.*.price_source` 与 `assess()` 的 `progress.price_source` / `n_rows_price_source_excluded` 计数。
- **财报：按单位排除，不按行**。同一 **(ticker, expiry)** 的**所有** recorded 行（不论是否已结算、route 是否可用）
  只要**任一行**的 `earnings_status` 是 `before_expiry`（财报落在记录日与到期日之间）或 `unknown`（查不到 / 判不了），
  **整个单位排除**；两臂同等。`after_expiry` 不在期权存续期内，不污染。
  - 为什么按单位：日报钩子的财报日来自 ChronosBee 催化剂，它用 `datetime.now()` 算 days_until ⇒ 财报**当天**
    （14:00 PT 扫描）得 −1 被丢；该票若还有别的未来催化剂（如分红日），就返回「无财报」⇒ 当天那行判 `none`。
    按行排除时，同单位更早的行都是 `before_expiry` 被排除，「取最早合格行」恰好选中财报当天那行——
    报价是财报前的 IV，到期收盘却含财报跳空，要排除的污染被系统性地选进来。
  - **再按票补一刀**：单位若**在财报当天才第一次被选中**（周度周二、月度周一换到新到期日，恰逢周二 / 周一
    盘后财报——周二是最常见的财报日之一），它唯一的行被误标 `none`，同单位没有更早的 `before_expiry` 行可以连坐它。
    所以另从**该票所有 recorded 行（两个 tenor 都算）**收集已知财报日 D：单位存在 d∈D 使
    `首条记录日 ≤ d ≤ expiry` ⇒ 整单位排除。**防后视**：只收集 `row.date ≤ 该单位首条记录日` 的行里的
    `earnings_date`（X−1 那行记下 earnings_date=X 可以用；首条记录之后才出现的财报日不用）。
  - **不改 ChronosBee**：改它会截断维度 IC 协议（`experiments/dim_ic_preregistration.md`）的 H2。
  - 代价（如实）：跨过财报的单位，财报**之后**记录的干净行也随整单位丢掉。判据只用记录时已知的信息，不看结果。
- **去重**：同一 **(ticker, expiry)** 会被连续多天选中（月度约一周、周度约五天），它们共享同一个到期收盘，
  不是独立观测 ⇒ 只取**最早记录日**的那一行（离到期最远、路由信息最「事前」）。
  先过滤（财报按单位、其余按行）、再去重；过滤条件全是事前可知的，不依赖结果。
- **梯子占档顺序** `LADDER_FILL_ORDER = (0.20, 0.10, 0.16, 0.25, 0.30)`：同一张合约不得占两档，主档 0.20（结果档 /
  base）与远档 0.10（far）先占，辅档撞上已被占的合约让位（记 `same_contract_as_<档>`）。按 |Δ| 升序占档时，
  稀疏链上 0.16 常抢走 0.20 的合约（评审合成链：周度 26.9%、月度 11.9%，低 IV 更甚），而 IV 又与 flag 相关 ⇒
  结果档缺失率在两组间系统性不同。
- 某侧在 0.20 档不可报价（容差内无合约 / 缺腿 / bid=0 / 点差 > 25%）的单位，该侧结果为缺失，**不进该侧检验**。
  规则两臂同等；但缺失**率**仍可能随 IV / 行权价间距与 flag 相关——`assess()` 的 `progress.per_side.*.skipped`
  按侧记缺失原因计数，就绪时一并报告（§11）。

## 4. 统计量与置换

- 统计量 `T = mean(outcome | flag) − mean(outcome | normal)`，H1 下 T < 0。
- **按记录日分块置换**：只在同一记录日内打乱 flag 标签，保留每日的 flag 数；
  `p = (1 + #{T_null ≤ T_obs}) / (1 + n_perm)`，并列算作「≤」（保守）。
- **为什么分块**：GEX 政体与击穿都是**全市场相关**的——同一天的票一起负 gamma、一起跌穿。
  跨日打乱会把日期效应算成 flag 效应，p 偏小（本仓「横截面池化」教训：可比的量不蕴含可池化的相关性）。
  全天同一标签的日子对零分布无贡献（保守）。`tests/test_sell_strike_ledger.py` 用「flag 只与坏日子相关、日内无关」
  的合成数据证明：分块置换 p 不小，全局置换 p 很小。
- `n_perm = 5000`，`seed = 20260923`（numpy `default_rng`；块按日期字符串排序后依次消耗随机数，结果可复现）。

## 5. α 与多重检验

- 族 α = 0.05，4 个检验 Bonferroni ⇒ **α_each = 0.0125**。
- 判定：`p ≤ 0.0125` ⇒ `reject_h0`，否则 `fail_to_reject_h0`。**只在就绪后检验一次**（§6），不做中期偷看。
- **「只一次」的机制（首次就绪即冻结，唯一写者 = 日报钩子）**：`assess(freeze=True)` 从账本读行、**第一次**满足就绪闸时跑检验，把
  `{ready_date, tenor, prereg_version, unit_keys: [(ticker, expiry, date)…], per_side: {observed, p, alpha_each,
  decision, n_flagged, n_normal, …}}` 原子写进 `<state>/<tenor>/prereg_result.json`（`os.link` 先到者赢，
  不覆盖已存在的文件）。此后每次 assess **只读这份、不重算**——后来的数据改不了它，闸门后来跌回也改不了它。
  - 为什么需要：日报钩子每天经本地报告调一次 assess、MCP 也会调；不冻结就是每天在更多数据上重跑一次检验
    （可选停止）。评审在 H0 下模拟：名义 α_each 0.0125，就绪后 40 天里任一天出现拒绝的比例 0.060，膨胀约 5 倍。
  - **唯一写者**：只有日报钩子（`write_local_report(freeze=True)` → `render_markdown` → `assess(freeze=True)`）会冻结。
    MCP 工具与 CLI `--assess` 走 `freeze=False`：就绪但未冻结时显示「已就绪，等待日报冻结」，**不跑检验、不写文件**。
  - `ready_date = min(as_of, 数据视界)`，数据视界 = 账本里 as_of 当时已知的最晚日期（记录 / 结算 / 放弃日）；
    `as_of=None` 时就是数据视界。写路径（`run_for_date` / `settle` / 冻结 / CLI `--date`）拒绝晚于 PDT 今天的日期：
    一个手误写进去的未来 `ready_date` 是永久的（到那天之前日报一直不给检验结果，改它只能删冻结文件 = 协议变更）。
    `assess(as_of=D)` 在 D 早于 `ready_date` 时不给检验结果、也不重算。
  - `assess(as_of=D)` 只用 D 那天已知的信息：`settled_on`（放弃为 `settle_give_up_on`）晚于 D 的行按 pending 算。
    否则同一个 D，今天判与一个月后判结果不同，冻结日也无从回放。
  - 删改 `prereg_result.json` = 协议变更（§10）。测试 / 探针用 `rows=` 注入时不读不写该文件。

## 6. 就绪闸（每个 tenor 独立）

同时满足才 `ready`：

| 条件 | 值 |
|---|---|
| 独立单位数 | ≥ 60 |
| 不同到期日数 | ≥ 12 |
| 每侧 flag 组与 normal 组（在 0.20 档有结果、且落在**信息块**里的单位） | 各 ≥ 10 |

**信息块**（`per_group_scope = "informative_blocks"`）：同一记录日（= 置换的块，§4）里 flag 与 normal 两种标签都有的块。
分块置换只从信息块取信息，全天同一标签的块对零分布没有贡献；按全体计数时，「两周全市场负 gamma（全员 flag）、
其余十周无人被 flag」的账本三道闸全过、信息块 0、p 恒为 1——而检验只跑一次（§5），这个没有功效的结果会被冻结。
`assess()` 的 `progress.per_side.*` 同时给出全体与信息块内的计数，`progress.n_informative_blocks` 按侧给信息块数。

这些门槛**不是功效计算的结果**，是「够让置换分布不那么抖」的启发式下限。
**`ready` 只代表可以开始检验，不代表已验证。**

## 7. 盲化：代码只保证「不算、不显示」

**如实写清（2026-09-24 最终评审）**：本协议的盲化**只有一条代码保证**——`assess()` 在未就绪时**根本不调用**
置换检验，返回值里没有任何效应量 / p 值 / 判定键（`sell_strike_ledger.BLINDED_KEYS`）；本地报告与 MCP 工具
只转述 `assess()`，所以也不显示效应量。冻结之前，没有任何代码路径会算出或显示两组的结果差。

**信息论上盲不了**：route flag 与现价是本产品每天的输出，收盘价是公开行情。任何人把某单位首条记录日的
route flag + 0.20 档 strike / bid（MCP 按日期读账本、本地报告）与到期日的收盘（到期日那天记录的行的
`underlying_price`、本地报告里的 `S=`、或任何行情源）拼在一起，就能算出每个单位的 `pnl_over_credit` 并按 flag 分组。
MCP 行视图在冻结前省掉结算字段（`sell_strike_report.BLINDED_ROW_FIELDS`，标 `settlement_blinded`）只是少递一把刀，
**不等于**盲化，此前「MCP / 本地报告已盲化」的说法作废。

**所以规则是行为规则**：检验冻结之前，**从任何出口**（MCP 按日期读账本、本地报告、账本 jsonl 原文件、公开行情）
重建单位结果、按 flag 比较 = **偷看** = 协议变更（§10），须记修订号并声明截断。
进度（行数、独立单位数、各组与信息块计数、结算 / 放弃 / 待结算数、各侧缺失原因、报价来源分布）任何时候可看：
它们只是样本量与标签计数，不含结果的数值。检验冻结之后三个出口全部解盲（MCP 返回结算字段）。

## 8. 描述性分析（不触发任何动作）

- **按档校准**：各档已结算单腿的**实际到期 ITM 频率** vs 平均 **N(d2)**（风险中性）。不分 flag、不破盲。
  预期：因 VRP，实际频率**低于**风险中性概率；若高于，先查数据（结算口径 / 报价时点），不是先改路由。
- 覆盖率：不可得原因分布（stale_vintage / vintage_mismatch / payload_unavailable …）、route unavailable 比例、
  财报排除比例、结算放弃原因。

## 9. 冻结的路由规则 v1（`ROUTE_RULE_VERSION = 1`，读 `le_45dte` 视图）

zero gamma 由**重定价扫描**得到：每张合约固定自身 IV，在现价 ±20% 的 81 点网格上用 BS 重算 γ(p)，
`total(p) = Σ sign·γ(p)·OI·100·p²·0.01` 的过零点（不是逐行权价净 GEX 变号）。

**先看样本、再看符号、再看距离**：

1. 扫描无可用合约（`insufficient_contracts`）或 `sign_at_spot` 为空 ⇒ put / call 都 `unavailable`（不进检验）。
2. 扫描实际用到的合约数（`le_45dte` 视图里 IV>0 且 OI>0 的合约，`zero_gamma.n_contracts`）
   **< `MIN_SWEEP_CONTRACTS = 20`**（或缺失）⇒ put / call 都 `unavailable`，reason `sweep_too_few_contracts:<n>`
   （不进检验）。几张合约算出的符号不是政体：评审探针里 244 张合约只剩 2 张有 IV，照样判出 all_positive / base。
   账本行的 `env.zg_n_contracts` / `zg_excluded_no_iv` / `zg_excluded_no_oi` 记下每行扫描实际用了多少合约，事后可查。
3. 现价处净 gamma **为负**（含全段为负 `all_negative`）⇒ put = far、call = far，`flag_put = flag_call = True`。
4. 现价处净 gamma 为正或零：
   - 下方最近过零点距离 `zg_below_pct ≤ 3.0`（%）⇒ put = far，`flag_put = True`；否则 put = base，`flag_put = False`；
   - call 一律 base，`flag_call = False`。

`base → 0.20Δ`，`far → 0.10Δ`，`unavailable → 无`。旧规则只看「离 flip 多远」，会把远离零点的负 gamma 判成安全——
那正是负 gamma 放大波动、最该退后的情形。

## 10. 什么算协议变更

以下任何一项改动都是**协议变更**，必须：记修订号（本文件追加「修订 N」节，写日期、版本、理由、改动前是否看过效应量），
并声明**截断**（变更之前记录的行是否还进检验；默认不进）：

- 改路由规则或其常量（`FLIP_BUFFER_PCT`、`BASE_RUNG`、`FAR_RUNG`、`ROUTE_VIEW`、`MIN_SWEEP_CONTRACTS`、规则文字）——须同时加 `ROUTE_RULE_VERSION`；
- 改度量（`pnl_over_credit`、基线档 0.20、到期收盘口径）、改统计量或置换方式、改 α / 检验个数；
- 改独立单位的定义（去重键、取最早、财报排除集合与「按单位」判据、梯子选档规则 `LADDER_DELTAS` / `LADDER_FILL_ORDER` /
  `DELTA_TOL` / 报价闸）；
- 改就绪闸门槛或其计数口径（`per_group_scope`）；改盲化（任何让未就绪状态计算或吐出效应量的代码改动）；
- 改冻结的写者（让 MCP / CLI 也能冻结）或 `ready_date` 的取法；
- 删除、改写或重新生成 `prereg_result.json`；冻结之前**从任何出口**重建单位结果并按 flag 比较（§7）。

修 bug（实现与本文件描述不符）不是协议变更，但必须在修订节里记录，并说明受影响的已记录行。

## 11. 已知局限

- OI 是 t−1 的；朴素 OI 符号（§1）；只判到期收盘是否 ITM、不判存续期内触及；
- 报价取 t 日收盘后快照，实际最早 t+1 成交 ⇒ 绝对收益乐观（两臂同等，不影响比较，但别拿绝对值当可实现收益）；
- N(d2) 是风险中性概率；
- 个股之间同日结果相关，分块置换只处理了「日」这一层；同一到期周的不同记录日之间仍有重叠（去重只到 (ticker, expiry)）；
- 0.20 档结果缺失的**规则**两臂同等，缺失**率**未必同等（稀疏行权价 / 低 IV 与政体相关）；就绪时按侧报告缺失原因计数；
- 财报按单位（及按票补的那一刀）排除会丢掉跨财报单位里财报之后的干净行（§3），样本攒得更慢；
- 非收盘后报价（`cboe_stale_intraday` / `cboe_intraday` / `session_live`）行不进检验（§3），CBOE 文件陈旧频繁、
  或有人盘中手动跑日报的日子样本变少；这是数据源 / 读取时刻的属性，两臂同等；
- 就绪闸只数信息块（§6），市场政体长期一边倒时够得更慢——那是如实的「功效不够」，不是放宽闸门的理由；
- flag 频率由市场政体决定，若某 tenor 长期几乎全是同一标签，就绪闸可能很久够不着——那是如实的「无法检验」，不是放宽闸门的理由。

## 12. 运行

每日扫描收尾由 `alpha_hive_daily_report._post_scan_notify` 调 `sell_strike_ledger.run_for_date`（记录 + 结算），
失败非致命；随后 `sell_strike_report.write_local_report(date, freeze=True)` 写本地报告，首次就绪时在这里跑检验并冻结
（全仓唯一的冻结写者）。手动：`/usr/local/bin/python3 sell_strike_ledger.py --assess`（只读就绪度，**不冻结**，
退出码 0 / 1 / 3；`--date` 晚于 PDT 今天 ⇒ 退出码 1）。

## 13. 机器可读常量

每行 `名字 = Python 字面量`。改这里而不改代码（或反之）⇒ `TestPreregPinned` 红。

```prereg-constants
ledger.PREREG.version = 1
ledger.PREREG.registered = "2026-09-23"
ledger.PREREG.primary_rung = 0.20
ledger.PREREG.sides = ("put", "call")
ledger.PREREG.tenors = ("monthly", "weekly")
ledger.PREREG.metric = "pnl_over_credit"
ledger.PREREG.n_tests = 4
ledger.PREREG.alpha_family = 0.05
ledger.PREREG.alpha_each = 0.0125
ledger.PREREG.n_perm = 5000
ledger.PREREG.seed = 20260923
ledger.PREREG.min_independent_per_tenor = 60
ledger.PREREG.min_distinct_expiries = 12
ledger.PREREG.min_per_group = 10
ledger.PREREG.eligible_earnings_status = ("none", "after_expiry")
ledger.PREREG.per_group_scope = "informative_blocks"
ledger.PREREG.excluded_underlying_price_sources = ("cboe_stale_intraday", "cboe_intraday")
candidates.ROUTE_RULE_VERSION = 1
candidates.ROUTE_VIEW = "le_45dte"
candidates.FLIP_BUFFER_PCT = 3.0
candidates.BASE_RUNG = 0.20
candidates.FAR_RUNG = 0.10
candidates.LADDER_DELTAS = (0.10, 0.16, 0.20, 0.25, 0.30)
candidates.LADDER_FILL_ORDER = (0.20, 0.10, 0.16, 0.25, 0.30)
candidates.MIN_SWEEP_CONTRACTS = 20
candidates.DELTA_TOL = 0.05
candidates.MAX_SPREAD_PCT = 0.25
candidates.WING_WIDTH_SIGMA = 0.5
candidates.TENORS = {"monthly": {"dte_lo": 21, "dte_hi": 45, "hi_inclusive": True, "target_dte": 30}, "weekly": {"dte_lo": 7, "dte_hi": 21, "hi_inclusive": False, "target_dte": 14}}
levels.zero_gamma_sweep.band_pct = 0.20
levels.zero_gamma_sweep.grid_points = 81
levels.T_FLOOR_DAYS = 0.5
levels.RISK_FREE_RATE = 0.045
ledger.PREREG_RESULT_NAME = "prereg_result.json"
```

## 登记前定稿记录（2026-09-23）

登记前的一轮评审（数学统计 / 失败传导 / 生产安全三路，每条都有探针实测）与变异检验之后，由主会话拍板并入协议 v1
（不另起修订号）。**改动时账本零行、没有任何已结算样本，起草与修改都没有看过效应量。**

| # | 改了什么 | 为什么 | 落在哪 |
|---|---|---|---|
| 1 | 梯子占档顺序由 \|Δ\| 升序改为 `LADDER_FILL_ORDER = (0.20, 0.10, 0.16, 0.25, 0.30)`；辅档撞上已占合约 ⇒ `same_contract_as_<档>` | 升序占档时 0.16 常抢走 0.20 的合约（周度 26.9% / 月度 11.9%，低 IV 更甚），结果档缺失率与 IV（进而与 flag）相关 ⇒ 选择偏差；route=base 的行基线结构也跟着缺腿 | §3、§13；`sell_strike_candidates.build_ladder` |
| 2 | 路由加扫描样本门槛 `MIN_SWEEP_CONTRACTS = 20`，不足 ⇒ 两侧 `unavailable`（`sweep_too_few_contracts:<n>`）；账本 env 记扫描合约数 | 只剩两三张有 IV 的合约照样判出政体、作为正常样本进检验，事后查不出也剔不掉 | §9、§13；`sell_strike_candidates.route`、`sell_strike_ledger.build_tenor_row` |
| 3 | 检验首次就绪即冻结进 `prereg_result.json`，此后只读不重算 | 原实现每次 assess 都在更多数据上重跑检验（日报每天一次）⇒ 可选停止：评审 H0 模拟里单个检验名义 α 0.0125，就绪后 40 天内任一天拒绝的比例 0.060（约 5 倍） | §5、§13；`sell_strike_ledger.assess` |
| 4 | `assess(as_of)` 把 as_of 之后才结算 / 放弃的行按 pending 算（放弃记 `settle_give_up_on`） | 原实现会用到 as_of 之后的到期收盘，「复现某日判定」不成立，冻结日也无法回放 | §5；`sell_strike_ledger._as_known_on` |
| 5 | 财报排除由「按行」改为「按 (ticker, expiry) 单位，任一行 before_expiry / unknown 即整单位排除」 | ChronosBee 用 `datetime.now()` 把财报当天丢成 −1，当天行误标 none，而「取最早合格行」恰好选中它（报价在财报前、结算含跳空）；不改 ChronosBee 以免截断维度 IC 协议 H2 | §3；`sell_strike_ledger.independent_units` |
| 6 | 盲化扩到 MCP 行视图（未就绪剔除结算字段），并如实写明原始 jsonl 可读 = 改代码偷看 | 原「结构性盲化」只罩住 assess 返回值，MCP 按日期逐票返回 flag + 梯子 + 到期收盘，不改一行代码就能算出效应量 | §7、§10；`sell_strike_report._row_view` |

同批的非协议改动。**只增加可观测性、不改任何判定**的：账本行增加 `payload_last_trade_time`（payload 自带的最后成交时刻，
区别于调用时刻 `fetched_at`）、`session_live`、`iv30`（百分数原样）、`fetch_counts`（取数层原始合约数 + 三个丢弃计数）；
`scan_timing` 计数加 `cboe_raw`；本地报告与 run_for_date 显示
「待结算 N（其中已过期超过 5 个日历日 M）」，M>0 打 warning；数据根迁移表登记 `sell_strike_state`。
**会改判定、按修 bug 记录的**（实现与本意不符，§10 末段）：取数层的休市日判据改为「开市钟点**且** payload 是今天的」——
劳动节盘中不再把上一交易日已到期的合约当 dte=0 留在链里、也不再把上一交易日的盘后价当现价、标成 `cboe_intraday`（改取该场 close）；这会改变休市日当天
进扫描的合约集合。改时账本零行，没有受影响的已记录行。

## 登记前定稿记录（续，2026-09-24）

来源：**2026-09-24 最终评审**（两位评审者，每条都有探针实测：`advrev_cs/probe_earn.py`、`probe_blind.py`、
`probe_gate.py`、`probe_freeze.py`，`advrev/p3_stale_intraday.py`、`p4_mcp_writes.py`）。由主会话拍板并入协议 v1
（不另起修订号）。**改动时账本仍是零行、没有任何已结算样本，没有看过效应量。**

| # | 改了什么 | 为什么 | 落在哪 |
|---|---|---|---|
| 7 | 财报排除按票补一刀：该票所有 recorded 行（两个 tenor）里、记录日 ≤ 单位首条记录日的行给出的财报日 d，落在 [首条记录日, expiry] ⇒ 整单位排除 | 第 5 条漏掉了它自己的目标：周度周二 / 月度周一换到期日，恰逢当天盘后财报时，新单位的第一行（也是唯一一行）被误标 none，同单位没有更早的 before_expiry 行可连坐（探针：`weekly UNITS: 2026-10-13 2026-10-30 none`）。只用首条记录当时已知的财报日，不后视 | §3；`sell_strike_ledger._earnings_tainted_units`、`assess` 读两个 tenor |
| 8 | §7 如实改写：代码只保证未就绪时不算、不显示效应量；冻结前从任何出口重建结果 = 偷看（协议变更）；删掉「MCP / 本地报告已盲化」 | 到期日当天记录的行的 underlying_price 就是到期收盘，两次 MCP 调用即可精确拼出单位结果（探针：拼出 −4.714…，与账本真值相同）；route flag 与现价是产品输出、收盘价是公开行情，信息论上盲不了。第 6 条的 MCP 剔除结算字段保留，但不再称为盲化 | §7、§10；`sell_strike_report.BLINDED_ROW_FIELDS` 注释 |
| 9 | 就绪闸的 flag / normal 计数只数**信息块**（同一记录日两种标签都有）里的单位；`progress` 加 `n_informative_blocks` | 分块置换只从信息块取信息；按全体计数时「两周全员 flag、十周无人 flag」闸门全过、信息块 0、p=1.0，而检验只跑一次，无功效的结果会被冻结（探针 probe_gate） | §6、§13（`per_group_scope`）；`sell_strike_ledger._group_counts`、`assess` |
| 10 | 冻结的 `ready_date = min(as_of, 数据视界)`；写路径（run_for_date / settle / 冻结 / CLI）拒绝晚于 PDT 今天的日期 | `--assess --date 2027-12-31`（手误）曾把 ready_date 永久写成未来，此后日报一直不给检验结果，改它只能删冻结文件（探针 probe_freeze）；`--settle --date <未来>` 会把 settled_on 盖成未来 | §5、§12；`sell_strike_ledger._check_not_future`、`assess` |
| 11 | `underlying_price_source == cboe_stale_intraday`（收盘后读到的仍是盘中文件）的行不进检验；按来源计数进 per_tenor / progress / 钩子日志 | 这类行权利金是盘中报价、不是收盘后快照，却照常 recorded、进检验，且没有任何输出数它（09-22 生产 4/30；探针 p3_stale_intraday） | §3、§11、§13（`excluded_underlying_price_sources`）；`sell_strike_ledger._eligible_except_earnings` |
| 12 | 冻结只有一个写者：`assess(freeze=False)` 为缺省，只有日报钩子经 `write_local_report(freeze=True)` 传 True；MCP / CLI 就绪但未冻结时显示「已就绪，等待日报冻结」、不写文件 | MCP 工具标了 readOnly、文档写「never writes」，实际首次就绪时会跑检验并写冻结文件（探针 p4_mcp_writes：调用前 False、调用后 True）；一次不可逆的写有两个写者 | §5、§10、§12；`sell_strike_ledger.assess`、`sell_strike_report`、`alpha_hive_daily_report` 卖权钩子 |

**第 5 条的补充**：第 5 条（按单位排除）保留，第 7 条在其上加严。**第 6 条的更正**：MCP 行视图剔除结算字段照旧，
但它不是盲化（第 8 条）。

## 登记前定稿记录（续，2026-09-26）

来源：**2026-09-26 delta 评审**（对 2026-09-24 那批修复的复核；探针 `rv7/p6b_live_intraday_dte0.py`、`p7_session_edges.py`）。
由主会话拍板并入协议 v1（不另起修订号）。**改动时账本仍是零行（代码尚未上线），没有看过效应量。**

| # | 改了什么 | 为什么 | 落在哪 |
|---|---|---|---|
| 13 | 第 11 条的判据从「标签是 `cboe_stale_intraday`」改为「报价不是已收盘那一场的收盘后快照」：`excluded_underlying_price_sources` 加 `cboe_intraday`，且 `session_live is True` 的行也不进检验 | 标签取决于**读取时刻**而不是报价本身：同一份盘中 payload，收盘后读判 `cboe_stale_intraday`（排除），盘中读判 `cboe_intraday`（照进检验），且盘中读还会把当天到期合约以 dte=0 留在路由扫描里 ⇒ route flag 出自另一个合约集合（探针 p6b：同一 payload 11:30 ET 读 eligible=True、17:35 ET 读 eligible=False） | §3、§11、§13；`sell_strike_ledger.PREREG`、`_eligible_except_earnings`、`assess` 计数 |

同批**会改判定、按修 bug 记录的**（§10 末段）：取数层 `session_live` 的收盘时刻改为 `is_trading_day.session_close_et`
（半日市 13:00）——半日市 13:00–16:00 ET 的读取按「该场已收盘」处理（当天到期合约排除、现价取该场 close、标签
`cboe_close`），此前按 16:00 收盘会判成盘中。这会改变半日市当天那一时段读到的行进扫描的合约集合与标签；
账本记录钩子在 17:00 ET 跑，不受影响；改时账本零行，没有受影响的已记录行。
