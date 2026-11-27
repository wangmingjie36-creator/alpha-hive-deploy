# 评分冻结期队列（v0.45.757 起）

冻结声明与守卫：`ic_rerun_readiness._SCORING_FREEZE` / `_FREEZE_EXCEPTIONS`，测试
`tests/test_ic_rerun_readiness.py::TestScoringFreeze`。冻结起点 2026-11-09（v0.45.750），
到 IC 重跑出结果为止（闸门 2026-11-10 估 ≈2027-05-05，缺扫描的周顺延）。解冻由用户决定。

## 什么进这里

在 `final_score` 上游、**作废不为 0 条**、又**不是「当前数据是错的」**的改动——删通道、改权重、
退役 / 新增某蜂、换数据源但旧源没错、改聚合规则。这类改动冻结期内不进 main，记在下面，解冻后
**一次合入、只开一个世代**。

不进这里的：
- 不在 `final_score` 上游（账本、Alpha Bot、展示、期权页）——照常做；
- 能证明作废 0 条——照常做，并进当前世代起点当日（起点从 2026-11-09 出发，获批例外或日期更正会把它挪后；等价判据做法同 10-19 那批）；
- 真 bug（当前写进归档的数据是错的）——先问用户，点头后在 `_FREEZE_EXCEPTIONS` 登记并写明代价
  （「X/25 周 → 0/25，到期日 A → B」）。

## 条目格式

```
### <一句话>（登记日 YYYY-MM-DD，登记者 session / 分支）
- 改什么、动哪些文件：
- 为什么不能证明作废 0 条：
- 预期影响（哪些归档信号后移）：
- 分支 / 草稿位置（若已写好）：
```

## 队列

### 收盘后读到 CBOE 盘中陈旧文件的票，期权链改用 Yahoo（登记日 2026-11-24，登记者 session 07f3aaaf / main；用户当日定「登记冻结队列，解冻后合入」）
- 改什么、动哪些文件：`cboe_stale_intraday`（`cboe_options.official_price` 判出、`data_pipeline` 已拒收其标的价改取 yfinance）的票，其期权链派生量（P/C、`total_oi`、Max Pain、`options_score`、IV 期限结构、`quote_set`）目前仍读同一份陈旧 CBOE 文件。方案：这类票改取 Yahoo 链，OracleBee 与 BearBee 的 `options_bear` 随之变。预计动 `cboe_options.py`、`options_analyzer.py`、`data_pipeline.py`；**只限日报主流程，卖权账本 / 溢价页 / Alpha Bot 账本路径仍只认 CBOE**（预注册数据源，见 auto-memory `alpha-hive-alphabot-yf-fallback.md`）。
- 为什么不能证明作废 0 条：改的是 Oracle 的输入，11-20 起陈旧票每个扫描日 9~16 只（共 30 只），输出的 `options_score` ⇒ `final_score` 与 `agent.OracleBeeEcho.*` / `bear.options_bear` 信号都会变。旧 CBOE 链并非「错」（与同一份文件里的价格是同一时刻），只是不是收盘口径，所以不满足「真 bug 例外」的「当前数据是错的」。
- 预期影响：`signal_archive.COHORT_SIGNAL_SCOPE` 需新增一代，范围同 v0.45.765（`agent.OracleBeeEcho.*` + `bear.options_bear`）；这些探索信号的分信号世代起点后移到合入首跑日。
- 开工前必须先核实的风险（均**待验证**）：① Yahoo 链在扫描里受 429 限流（11-03 走代理仍在 281 次后被掐），16 只票约多 25 秒/只 ≈ 7 分钟；② Yahoo OI / IV 口径与 CBOE 不同，同一只票序列前后不可比，需要用 `oi_staleness.assess_frames` 的 Alpha Bot 同款护栏；③ 另一条路是「次日补取 CBOE」（不换源），先看今晚 20:05 ET 后源站是否补刷再比较。
- 分支 / 草稿位置：无，未写代码。证据与成因见 auto-memory `alpha-hive-cboe-stale-intraday.md`。

## IC 重跑时须处理的已知口径混合（不是待合入的改动，不开世代）

### `final_score` 跨 11-20 ~ 12 月初混了两种 iv_rank 口径（登记日 2026-11-27，登记者分支 claude/iv-rank-real-gate / v0.45.821；用户当日定「逐行闸，不重置」）
- 发生了什么：v0.43.18 起自攒的真实 IV 历史（`iv_history.IV_RANK_MIN_DAYS` = 63 条）陆续攒满，`options_analyzer` 按票**自动**从 `hv_proxy`（当前 HV 在 HV 历史里的排名）切到 `real_iv_{N}d`（今日 IV 在自攒 IV 历史里的排名）。没有代码改动，是数据条件触发。生产库只读计数（`options.iv_rank_is_real` = 1 的票数）：11-20 1 只、11-23 4、11-24 11、11-25 22；剩下 8 只（TMUS / BILI / ENPH / NEE / NFLX / SNOW / TMO 差 1~2 条，VKTX 差 4 条）约 12 月初切完。
- 为什么影响 `final_score`：iv_rank 经 `options_analyzer` 的 iv_signal（0~3 分档：<20 / <40 / ≤70 / ≤85 / 其余）进 OracleBee `options_score` ⇒ `final_score`；BearBee 也读它。同一只票切换前后档位口径不同；同一天不同票口径也可能不同。
- 为什么不登日期边界：切换按票陆续发生，没有单一边界日；在冻结期登边界会把 IC 闸 2/25 → 0/25（到期日 ≈2027-05-05 顺延），且边界后几天仍混着 hv_proxy 行。用户 11-27 选不重置。
- IC 重跑时怎么做：主结果照常用当前世代全部样本；**另跑一份敏感性分析**——按同一 (日期, 标的) 的 `options.iv_rank_is_real` 切开（0 / 1 / 缺），看 `final_score` IC 在两种口径下是否同号、量级是否相近。结论不一致时如实报告两种口径，不挑一个。
- 已做的部分：`options.iv_rank` / `options.iv_percentile` 自身的探索性 IC 已由 `signal_archive.REAL_OBSERVATION_GATES` 逐行只用真实 IV 口径（v0.45.821）。
- 各口径在 iv_signal 档位上实际差多少：**待验证**（真实口径生效那天 hv_proxy 值没算，不能逐行对照；要量化须用同一天的 HV 历史重算 hv_proxy，只读、不写回）。
