---
paths:
  - "sell_strike_*.py"
  - "sell_put_wheel.py"
  - "options_paper_leg.py"
  - "straddle_*.py"
  - "alphabot/straddle.py"
  - "alphabot/strangle_compare.py"
  - "alphabot/wheel*.py"
  - "alphabot/static/pages/straddle.js"
  - "alphabot/static/pages/wheel*.js"
  - "experiments/sell_strike_routing_prereg.md"
  - "experiments/sell_put_wheel_prereg.md"
  - "experiments/straddle_*_prereg.md"
  - "tests/test_sell_strike_*.py"
  - "tests/test_sell_put_wheel.py"
  - "tests/test_options_paper_leg*.py"
  - "tests/test_straddle_*.py"
  - "tests/test_alphabot_straddle.py"
  - "tests/test_alphabot_strangle_compare.py"
  - "tests/test_alphabot_wheel*.py"
---

# 期权纸面账本：卖权选择器 / 跨式与影子宽跨 / 接货型卖 put · 按路径加载

> 读 / 改上面 `paths:` 匹配的文件时 Claude Code 自动加载。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来（「核心组件指针」的顶层条目与原 Alpha Bot 子条），根目录只留一行指针。预注册通则与协议登记表见 `experiments/CLAUDE.md`。

- **卖权行权价选择器（对标 GEXBot）** `sell_strike_levels / _candidates / _ledger / _report`（v0.45.333）：CBOE 原始链 → 重定价扫描 zero gamma / 净 GEX majors / DEX / vanna·charm → delta 梯子与六种结构 → 月度 / 周度前向账本（`PATHS.sell_strike_state`）。**不进评分、不上网站**（本地报告 + MCP `alphahive_get_sell_strike_candidates`）；双向 AST 火墙在 `tests/test_sell_strike_integration.py`。检验协议冻结在 `experiments/sell_strike_routing_prereg.md`（首次就绪只跑一次）
- **接货型卖 put 纸面账本（v0.45.720）** `sell_put_wheel.py`（独立账本 `PATHS.sell_put_state`，规则冻结在 `experiments/sell_put_wheel_prereg.md`）：用户 2026-11-04 定「不做双卖、只卖 put（参考愿意接货的做法）」，名单 XOM / WMT / NVDA；一只一个循环——卖 put（**v2 / v0.45.742，用户 11-06 定**：30~50 天窗口内所有到期日 × 不高于接货价的合格档、IV÷RV30 ≥ 1，取「年化 × IV/RV」最高的那张，额度不够时也按它排；RV 走 `yf_daily` 分红复权、取不到 ⇒ `no_rv` 不猜、整批取不到 ⇒ P2；新开仓记 `rule_version`，缺 = v1；**接货价现在是占位 = 现价 × 90%**，不是估值判断）→ 到期收盘 < 行权价则接货 → 卖 covered call（行权价 ≥ 接货价 − put 权利金、|delta| 最接近 0.25）→ 被叫走一圈结束；没有止损止盈。成交一律按 bid，结算按到期那一场的官方收盘（场次不符 ⇒ 延期、绝不拿前一天的价顶替），到期日当天不结。**测量账本、不是检验**（样本小到证明不了优势）；**不并入**卖权选择器预注册 / 跨式账本、不进评分 / 对冲 / 网站；与卖权选择器的 50% 止盈 / 2× 止损是两套东西（这里被行权是目的）。失败在源头登记（`stats()` → scan_timing `sell_put` → `alert_manager._check_sell_put`：整轮抛 / 全部标的取不到链 / 结算延期 ≥ 3 天 ⇒ P2）。美式提前行权、保证金、分红都不建模。守卫 `tests/test_sell_put_wheel.py`
- **「接货 · 筛选」子页（v0.45.722/723）** `alphabot/wheel_screen.py`（纯函数）+ `wheel_prices.py`（日线，经 `yf_daily`、限流后不再逐只撞）+ `pages/wheel_screen.js`（`#/wheel/screen`）：三层——离线层读卖权账本最近一日的 put 梯子（**只读梯子与财报日，不碰结算 / 路由**，盲期）、行情层（回撤 / 波动 / 21 日窗口 / 权利金重放，6 小时缓存）、取链层（**直接调 `sell_put_wheel.pick_put`**，选合约规则只有账本那一份；v0.45.742 起要 RV ⇒ 没先「取行情统计」时报 `no_rv`；综合分只在规则内部用、取链层输出不带）。名单 / 接货价存 Alpha Bot 设置（`wheel_watch` / `wheel_accept`），**只供显示，不改接货账本**；**只并排事实，没有排名 / 推荐 / 综合分字段**（测试按键名钉住；排序只按单个事实列）；重放偏乐观（过去两年多数标的在涨、30 天窗口互不重叠样本约二十来个），页面首行写明。守卫 `tests/test_alphabot_wheel_screen.py`
- **「跨式」页（v0.45.424）** `alphabot/straddle.py`：财报跨式纸面账本的持仓 / 平仓 / 逐仓 Greeks / 盘中参考报价（`cboe_options.quote_held`）。**只读**，不写账本、不拿账本 mark 顶替拿不到的报价；**不显示任何一笔的 `gex_ctx`**（跨式 GEX 预注册的盲期，协议 `experiments/straddle_gex_prereg.md`，冻结只经 `straddle_gex_prereg.py --run-once`）。信号行的 `gex_ctx` 只记录、不进开单（守卫 `tests/test_straddle_gex_shadow.py`、`tests/test_alphabot_straddle.py`）
- **跨式账本入场时点（v0.45.693，v0.45.696 放宽，v0.45.702 改回，用户 11-03 定）**：只在**财报前最后一个交易日**开（`options_paper_leg.entry_max_sessions_before_event = 0` / `entry_window_miss`，NYSE 日历判，判不了不开；窗口 0 时 9 个可交易事件只开得出 4 个、1 时 5 个）、**不开买跨式**（`trade_cheap=False`）。依据：此前提前 2~4 周开、10 笔 3 胜，卖方押的事件判断 6/6 对、亏在事件前普通涨跌。⚠️ 改这两个数 = 改账本测什么，且影子宽跨预注册修订 5 的 `entry_window_sessions` 同值（守卫 `TestEntryWindow` / `TestRevision3EntryWindow`）
- **「事件前被平」读者侧标记（v0.45.606）**：`options_paper_leg.event_missed`（平仓日 ≤ 财报日 ⇒ 没测到事件；缺日期 ⇒ None）+ `split_by_event`，页面 / 日报 / `kpis()["event_split"]` 共用；**账本不存这个字段**，顶层 KPI 仍按全部已平仓（对得上账户已实现），「按方向」只含测到事件的那批
- **「跨式 vs 宽跨」对比卡（v0.45.584）** `alphabot/strangle_compare.py`：同一份当日快照的 ATM 跨式与 25Δ 宽跨并排（权利金 / 进出成本 / 点差旗标 / 盈亏平衡带 / 历史涨跌落在带外 k/n / 按账本定仓的张数）。⚠️ **只并排事实、没有推荐字段**（系统里已结算事件极少，任何「按标的选结构」都是拟合；测试钉住键名）；账本按权利金定仓 ⇒ 宽跨张数是跨式的数倍，名义风险不一定更小
- **影子宽跨记录（v0.45.586，协议 `experiments/straddle_strangle_shadow_prereg.md` v1；v0.45.593 修订 2；v0.45.693 修订 3：起点 11-03、旧规则单位不进；v0.45.696 修订 4：窗口 0 → 1；v0.45.702 修订 5：窗口 1 → 0）** `straddle_strangle_shadow.py`：信号行 `strangle_quote` / 持仓 `shadow_strangle` / 平仓记录里的假想盈亏，**只记录、不进任何判断**（守卫 `tests/test_straddle_strangle_shadow.py`：有无记录的账本行为差分 + AST 读者白名单）。⚠️ **盲期：冻结前不得把某笔的影子盈亏与它的跨式盈亏拼在一起看**（含「看看 T 那一笔」；`closed_trades.jsonl` 里两个盈亏都在，是信息论上盲不了、靠纪律）；登记前入场的 8 笔不进检验；**平仓日 ≤ 财报日的单位也不进（`exit_before_event`，修订 2：账本在财报日 = 到期日 − 2 的盘后财报当晚就按 `expiry_buffer` 强平；v0.45.602 起账本入口已不再开这类单——`options_paper_leg.expiry_buffer_hit` 入场与强平共用，信号的 `eligible` 刻意不动，它是 GEX 预注册的样本定义，本排除规则留给存量单）**；进度 `/usr/local/bin/python3 straddle_strangle_shadow.py`，冻结只经 `--run-once`。**冻结后 Alpha Bot 影子面板自动解锁每笔配对 + 冻结结果**（v0.45.599，`straddle_strangle_shadow.reveal`：钥匙只有「冻结文件存在且完好」；冻结前接口里没有 `revealed` 键）。`options_paper_leg.run_for_date` 的返回值 / CLI 回显**剥掉了** `shadow_strangle`（手动补跑平仓日不会把一对盈亏摆在一屏；要看落盘记录是偷看）。记录失败在源头登记（scan_timing `strangle_shadow` → alert_manager P2；`wiring_suspect` = 一整轮 ≥5 个标的 25Δ 腿缺且一个都没取到 ⇒ 也是 P2）。**改度量 / 单位定义 / 排除规则 = 协议变更**（协议 §8、§14 记修订，`PREREG["version"]` 加一）
