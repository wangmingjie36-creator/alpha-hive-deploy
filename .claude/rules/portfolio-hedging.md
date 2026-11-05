---
paths:
  - "portfolio_greeks.py"
  - "delta_hedge.py"
  - "tail_budget.py"
  - "cboe_options.py"
  - "alphabot/hedge.py"
  - "alphabot/tail_hedge.py"
  - "alphabot/beta_long.py"
  - "alphabot/static/pages/hedge*.js"
  - "tests/test_portfolio_greeks*.py"
  - "tests/test_delta_hedge.py"
  - "tests/test_tail_budget.py"
  - "tests/test_beta_sensitivity.py"
  - "tests/test_hedge_history_gate.py"
  - "tests/test_alphabot_hedge*.py"
  - "tests/test_alphabot_tail_*.py"
---

# 组合 Greeks 与对冲：逐票 Delta 对冲 / SPY 覆盖账 / 尾部预算 / bid = 0 腿 · 按路径加载

> 读 / 改上面 `paths:` 匹配的文件时 Claude Code 自动加载。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来（原 Alpha Bot 子条），根目录只留一行指针。

- **逐票 Delta 对冲（v0.45.526）** `delta_hedge.py`（纯函数决策核心）+ `portfolio_greeks` 接线：每只有跨式腿的标的用**自己的股票**对冲跨式腿净 $Delta（原始值、不乘 β）；带 `delta_hedge_band_pct`（3% NAV）、拉回 0、跨式平仓后清仓、缺数据**逐标的**判。**缺省 `delta_hedge_mode="shadow"`：只算不成交、不写任何账本**（审计 `delta_hedge` 一节 + 对冲页面板）；改 `paper` = 纸面成交（独立账本 `hedge_state/delta_*`，**别复用 `positions.jsonl`**——Alpha Bot `account()` 取第一条当 SPY），要**单独提交**，首日会有约 5 笔逐票成交 + SPY 带联动。⚠️ 3% / 拉回 0 是**主观常数、未经检验**，无点差模型，只对冲 Delta。原因码唯一表 `delta_hedge.REASONS`（前端 `D_REASON` 双向核对）。**成本估算（v0.45.638）**：`delta_hedge_cost_bps`（缺省 5，主观未检验）只给审计 / 对冲页记估算成本、成交名义、笔数与「隐含换手」（`alphabot/hedge.py::implied_turnover`），**不进决策、不扣现金**；shadow 里决策日的名义是「若从空仓开始」的上界；头几个扫描日的回放显示放宽带宽几乎不降成交（样本小）。守卫 `tests/test_delta_hedge.py`、`tests/test_portfolio_greeks_delta_hedge.py`、`tests/test_alphabot_hedge_delta.py`
- **「对冲」页（v0.45.459）** `alphabot/hedge.py`：组合 Greeks 的纸面 SPY 覆盖账本（持仓 / 现金 / 成交 / 净值 / 每日带位）。**只读**，数据只经 `portfolio_greeks.ledger_view()`（历史走 `load_history` 放行闸，被拦记录只给计数；本模块不读文件，AST 测试钉住）。守卫 `tests/test_alphabot_hedge.py`
- **「尾部对冲候选」面板（v0.45.643，Phase A）** `alphabot/tail_hedge.py` + `static/pages/hedge_tail.js` + `GET /api/hedge/tail`：最近一份**放行**审计的压力网格 × SPY 当前链，摆出 6 个**后比例**认沽价差（卖 1 近、买 2 远；**别和卖多买少的前比例混**——那是卖波动率，在最差格里只会加重亏损）的逐格前后对比 / 到期盈亏 / Greeks 变化 / 规模。**纯函数、只读、没有推荐 / 综合分字段、候选按固定网格排不按效果排**；不写账本、不成交。规模**只对下行 / 平盘侧（现货冲击 ≤ 0）最差格定**（认沽管不到上行；审计当天已成交 SPY 时基线最差格会移到上行——按全网格最差格定规模曾把最差候选放大到 −128% NAV；没有现货 ≤ 0 的格就不定规模、**不退回用上行格**），再套未平仓量 2% 与卖出腿名义额 ≤ 100% NAV 两道上限，`offset_achieved` 永远给（2026-10-23：1 单位只抵消下行最差格亏损约 6%–13%，账户相对 SPY 合约太小）。⚠️ 档位 / 1:2 / 45 天 / 50% 抵消目标全是主观常数、**无前向证据**；网格不建模 skew / 时间 / 保证金 / 美式提前指派。⚠️ `portfolio_greeks` 模块头「v1 不用指数认沽」那条决定**未被推翻**——要做纸面成交（Phase C）先写预注册、再改那条。「重新拉取」会清 SPY 的 CBOE 进程缓存（4 小时）；账本读不了报 `ledger_unreadable`、不当成没记录（v0.45.647 二次检查）。守卫 `tests/test_alphabot_tail_hedge.py`
- **组合尾部预算（v0.45.649，只观测）** `tail_budget.py`（纯函数）+ `portfolio_greeks.compute_day` 接线 + `alert_manager._check_portfolio_tail` + 对冲页「组合尾部预算」面板：压力最差格损失 > 告警线（`tail_budget.CONFIG["alert_pct"]`，20% NAV）⇒ **P2**；逐标的 / 板块的尾部贡献表；**卖跨式影子闸**——今天新开的每笔，按当天实际的账试算加上它之后会不会超 15% / 20% 预算，只记录、**不真跳过**。结果在每天审计文件 `greeks_<日期>.json` 的 `tail` 一节（`price_check.tail` 是小简报）。依据（2026-10-23）：压力最差格约 −23% NAV，几乎全是 6 笔卖跨式（−24.7%），而 gamma / vega 告警按 1% 移动标度、对尾部是盲的。⚠️ 告警线 / 预算都是**主观常数**；影子闸**不是反事实**、评估用当天收盘标记价、**放在 `portfolio_greeks` 而不是 `options_paper_leg` 入场处**（入场代码看不到股票账与 SPY 覆盖）；**真闸会改变哪些单开出来 = 宽跨影子预注册的样本变薄**（起点 10-21，现有 2–3 笔里 15% 档会砍 2 笔），要真拦截先攒影子记录再追加预注册修订。是否响取决于当天最差格是否 > 20%——2026-10-27 首次生产运行（`tail` 一节正常）是 19.66% **没响**（10-26 离线重算 23.2%，差别来自 VZ 的跨式在财报日后一天平仓），**超线触发 P2 的路径在生产里还没真响过**，别当成已验证。**v0.45.655 收口**：告警线 20% = **用户定的风险承受度**（一次「现货 −10%、IV +20 点」最多亏 20% NAV）；**Gamma / Vega 告警线随预算推导**（`tail_budget.derived_greek_lines`：预算对半分 ÷ 压力网格角点冲击 ⇒ gamma 0.10% / vega 0.50% NAV，`portfolio_greeks.CONFIG` 里这两项缺省 `None` = 推导、写数 = 显式覆盖，用 `pg.alert_lines()` / `pg.public_rules()` 取有效值，**别直读 CONFIG**；它们仍只是页面标记、无 alert_manager 路径）；**单仓上限 = 预算 ÷ `max_open`**（3.33% NAV，归因表 `slot`，只展示不拦截；2026-10-23 六笔里五笔超限）；**「压力模型 vs 实际净值」**（`tail_budget.model_check`，只比跨式腿、只比持仓组成不变的相邻审计日，5 对）——结论：压力网格对每日跨式盈亏几乎没有预测力（R² −0.17，因为只建模 β×大盘冲击、抓不到单名事件跳空），Greeks 展开 R² 0.63 但大移动日高估损失（所以派生的 Gamma/Vega 线偏保守）；样本极小，**不能据此调参数**。**β 窗口对照（v0.45.666，只展示）**：压力网格的 β 是审计里的 60 日 OLS，XOM / CVX 的 60 日 β 为**负**（250 日也为负，2 年才翻正）⇒ 「现货 −10%」格把能源股推到上涨 ~10%，下行格里的能源损失是**股价涨**造成的；该格也是 SPY 认沽面板的规模目标。对冲页「压力网格的 β 窗口对照」用 250 日 β 把**最近一份审计**再算一遍并排给出（`tail_budget.beta_sensitivity` 纯函数 + `alphabot/beta_long.py` 取数 + `GET /api/hedge/beta` 单独加载、现取约半分钟、进程内缓存 6 小时）；真实数据 60→250 日：最差格 −23.2→−19.9%（20% 线破→不破）、下行最差 −18.5→−14.1%、β·$Delta −7.2→−17.1%（带内→出带；SPY 覆盖建议吃的那个量）。⚠️ **没有哪个窗口是真值**；**审计里的压力表 / 告警 / SPY 覆盖账仍只用 60 日 β**，对照不落盘、不进告警、不改成交——`beta_sensitivity` 等名字除展示层外谁都不许引用（守卫 `tests/test_beta_sensitivity.py::TestDisplayOnly`）；要换覆盖用的 β 窗口会改变纸面账成交，是另一个要单独决定、写依据的事。守卫 `tests/test_tail_budget.py`、`tests/test_portfolio_greeks_tail.py`、`tests/test_alphabot_tail_budget.py`、`tests/test_beta_sensitivity.py`
- **bid = 0 的深度虚值腿（v0.45.609）**：`portfolio_greeks.option_exposures` 对「bid = 0、ask 真实、iv > 0、四个 Greeks 齐全」的腿照常取 CBOE Greeks 进汇总，行上标 `bid_zero`、`mid` 仍为 None（压力表因缺 mid 单独剔它并写明 `mid(bid=0)`）；此前这类腿被当「缺报价」、整组 β·$Delta 变 unknown 而不对冲（2026-10-21 T 的 24.5 看涨）。⚠️ 只放宽 Greeks 汇总这一处，`cboe_options` 的 `quote_ok`（卖权 / 跨式入场 25% 点差口径）不动；判据**只有一份** `cboe_options.bid_zero_greeks_usable`（v0.45.620 起对冲账本与跨式页共用，守卫递归扫全仓、除这两处不许再定义 / import）；跨式页账本视图同样标 `degraded_legs`，对冲页 / 日报覆盖行显示 `n_bid_zero`（不进 `gaps`、不告警）。守卫 `tests/test_portfolio_greeks_bid_zero.py`
