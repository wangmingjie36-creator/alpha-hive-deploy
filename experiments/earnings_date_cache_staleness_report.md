# `earnings_cache/{T}_date.json` 为什么停在 09-04（只读调查，2026-10-13）

> 范围：只读。没有改生产数据、没有联网。唯一的执行是在 scratchpad 沙箱里离线复现 `"[]"` 的写入
> （`ALPHA_HIVE_HOME` 指向 scratchpad，yfinance 打桩）。代码基线 `1466911d`（origin/main）。

## 结论（一句话）

**不是写入失败，是没人再去写。** v0.45.104（09-03）起扫描的财报日改从本轮 ChronosBee 催化剂取，
`EarningsWatcher.get_earnings_date` 降级为「蜂没跑出来」时的兜底；而蜂没跑出来的那几天，
期权数据也同时挂了，兜底根本没被调到——**回落链存在但够不着**（本仓第五例）。`_date.json`
因此成了一份**事实上的死缓存**：生产扫描不读旧内容（12 h mtime TTL 挡住），只有绕过 TTL
直接读文件的人（Alpha Bot 溢价页，v0.45.492，尚未合入 main）才会读到它。

## 0. 实测快照（10-13 07:30）

| 项 | 实测 |
|---|---|
| `_date.json` 总数 | 41 |
| 其中不在当前 `config.WATCHLIST`（30 只）里的孤儿 | **11**：AAPL AMD AMGN BIIB COIN GOOGL MSTR PLUG REGN RUN UPST，全部 `fetched_at` 07-20 08:33 |
| 现役 30 只 | 29 只 `fetched_at` 09-04 09:57–09:58（**同一分钟一批**），MU 09-30 14:41 |
| 同目录其他文件 | **没有 `_results.json`**（`fetch_earnings_results` 只在 `--check-earnings` 路径写，生产不跑）；有的是 `earnings_history.py` 的 `_history.json` / `_moves.json` 各 22 份，30 天 TTL，**在正常滚动刷新**（mtime 09-22 / 09-28 / 10-05 / 10-08） |

⇒ 用户看到的「AMD 是 `"[]"`、AAPL / COIN / GOOGL 日期已过」**全是孤儿文件**，生产扫描不会再问这些票。
现役 30 只的问题是另一种：日期还在未来、但已经是 39 天前的**预估值**（见 §3）。

## 1. 生产里谁会调 `get_earnings_date`

| 调用方 | 生产里跑吗 | 09-04 后为什么没写缓存 |
|---|---|---|
| 日报 `_earnings_date_from_swarm` 的 `_fallback`（喂 `earnings_vol_signal.scan` 与 `sell_strike_ledger.run_for_date`） | 每个扫描日 | 只在该票 `ChronosBeeHorizon.details.catalysts` **为空**时才调。正常日 Chronos 有催化剂 ⇒ 不调。见下「够不着」 |
| `earnings_vol_signal.scan` 自带的 `_upcoming` | 日报传了 `upcoming_fn` ⇒ **不走**；只有 CLI `python3 earnings_vol_signal.py` / `options_paper_leg.py`（`signals=None` 且当日无信号时 `_evs.scan(as_of)`）会走 | 生产不跑这两个 CLI |
| `sell_strike_ledger._default_upcoming_fn` | 只在 CLI（未传 `upcoming_fn`）时 | 日报钩子传了 `upcoming_fn` |
| `sell_strike_report` / MCP | `upcoming_fn` 缺省 None，**不查财报** | — |
| `check_earnings_updates` → `check_and_update` → `get_today_earnings` | 只有 `alpha_hive_daily_report.py --check-earnings`；编排器不调 | — |
| `get_catalysts_for_calendar` | 0 调用方（v0.45.40 起） | — |

### 「够不着」的取证

结构化日志（覆盖 09-21 ~ 10-13，INFO 级也在）里：

- `alpha_hive.earnings_watcher` 的日志：**0 条**；兜底失败的 `财报日兜底获取失败`：**0 条**。
- Chronos 整批失败（`催化剂来源全部不可得`）的日子：09-21（4 只）、09-24 / 09-25 / 10-12（各 30 只）。
- 恰恰这几天下游也挂了：`期权纸面腿已更新 … 信号 0`（09-24 / 09-25 / 10-12）；
  卖权账本 10-12 `记录 0 / 不可得 30`。`scan` 只对通过报价预检的票调 `upcoming_fn`，
  卖权账本只对取数成功的票调 ⇒ **兜底一次都没被执行**。

原因是**相关失败**：Chronos 失败的根因（yfinance 限流 / DNS / 10-12 的代理 TLS EOF）同样打掉 CBOE /
yfinance 期权取数；而兜底本身走的又是同一个 yfinance `calendar`。兜底被设计成「蜂失败时救场」，
但蜂失败的那天它既没有调用者，就算被调用也大概率同样失败。

### MU 09-30 那一份是怎么来的（顺带抓到一个 Chronos 缺陷）

MU 财报日 = 09-30。ChronosBee 用 `(datetime.strptime(日期) - datetime.now()).days` 算 `days_until`，
**当天的日期得 −1**（离线实测确认），被 `days_until >= 0` 过滤掉 ⇒ MU 当天 catalysts 为空 ⇒
走兜底 ⇒ watcher 缓存过期 ⇒ 真打 yfinance ⇒ 写下 `2026-09-30`。卖权账本 09-30 两行
`earnings_source = yfinance / before_expiry` 就是它。这次兜底**歪打正着**给出了正确标签。

但如果当天该票还有分红催化剂，Chronos 的 catalysts 非空、只是没有 earnings ⇒
`_earnings_date_from_swarm` 返回 `{"earnings_date": None, "source": "chronos_bee_no_earnings"}` ⇒
卖权 `classify_earnings` 记 **`"none"`**（「视野内确实没有财报」）⇒ 进独立单位。
`classify_earnings` 自己的约定是 `as_of == 财报日 ⇒ before_expiry`，两者矛盾。
目前账本 583 条已记录行里**0 条**踩中（MU 是唯一的财报当天行），但结构上随时会发生。
**今晚就是一个检验点**：JNJ 财报日 10-13（缓存与 10-09 Chronos 一致）——扫描后看 JNJ 两行的
`earnings_status / earnings_source`。（JNJ 通常盘前发布，届时「none」语义上未必错，但与约定不一致；
NFLX 10-20 这类盘后发布才是真风险。发布时段：待验证。）

### 按「这个失败，下游怎么知道？」：缓存不刷新时谁会红？

**没有人。** 且分两层看：

1. **缓存陈旧本身**——对 `get_earnings_date` 的调用者无害：`read_json_cache` 按 mtime 判 12 h，
   过期就当未命中去重抓，**陈旧内容永远不会经它返回**。危险只在绕过 TTL 直接读文件的读者（Alpha Bot 溢价页）。
   没有任何观测点记录「这份缓存多久没被写过」。
2. **兜底失败**——只有一条 `WARNING`（`财报日兜底获取失败`），不进 status.json / alert_manager。
   下游表现：卖权账本记 `"unknown"`（会出现在 `per_tenor.earnings_status` 计数里，可见但不红）；
   `earnings_vol_signal._precheck` 把 `None` 与 `{"earnings_date": None}` 合并成同一句
   `no upcoming earnings date` ⇒ **「取不到」与「确实没有」在信号行里同形**（后果是漏开仓，不是错开仓）。
3. 写缓存失败（`atomic_json_write` 抛 OSError）只打 `debug`，生产日志级别看不到。

## 2. AMD 的 `"[]"` 是怎么写进去的

`earnings_watcher.py:99-122`：

```python
earnings_dates = cal.get("Earnings Date")          # yfinance 给了 []
if earnings_dates and isinstance(earnings_dates, list):   # [] 是假值 ⇒ 不进来，仍是 []
    earnings_dates = earnings_dates[0]
...
if earnings_dates is None:                          # [] 不是 None ⇒ 放行
    return None
date_str = str(earnings_dates)[:10]                 # str([]) == "[]"
```

`cal` 是带其他键（分红、营收预期等）的非空 dict，所以过得了前面的 `not cal`。
scratchpad 沙箱离线复现：`{"Earnings Date": []}` ⇒ 返回并缓存 `earnings_date: "[]"`。
现有测试只覆盖了 `{}`（空日历），没覆盖「有日历、Earnings Date 是空列表」。
同形状的兜底 `str(x)[:10]` 也会把 `NaT`、`Timestamp` 以外的怪对象变成假日期字符串，写入前**没有任何格式校验**。

对照：ChronosBee 同一个字段写的是 `isinstance(earnings_date, list) and earnings_date`，空列表正确地被当成「没有」。

### 全仓谁会怎么读 `"[]"`（以及其他非日期字符串）

| 读者 | 遇到 `"[]"` | 判定 |
|---|---|---|
| `sell_strike_candidates.classify_earnings` | `date.fromisoformat` 失败 ⇒ `"unknown"`（被独立单位排除） | ✅ 正确失败；但 `sell_strike_ledger.build_tenor_row` 会把原样的 `"[]"` 存进账本 `earnings_date` 列 |
| `earnings_vol_signal._precheck` | **字符串比较** `as_of < ed <= last_ok`；`'[' (0x5B) > '2'` ⇒ 不成立 ⇒ 拒绝，原因串 `earnings [] not within …` | ⚠️ **碰巧**安全（靠 ASCII 序），且原因被写成「不在窗口内」而不是「日期非法」 |
| `earnings_vol_signal._iso_date`（事件影子） | `None` | ✅ |
| `get_today_earnings`（`--check-earnings`） | 与今天 / 昨天字符串不等 ⇒ 静默当「今天没财报」 | ⚠️ 静默，但生产不跑 |
| `get_catalysts_for_calendar` | 会产出 `scheduled_date: "[]"` 的催化剂 | ❌ 当真日期，但 0 调用方 |
| Alpha Bot 溢价页 `earnings_state`（v0.45.492，未合入） | 按内容判有效：解析失败 ⇒ 未知 | ✅ |

因为 AMD 不在 WATCHLIST 且 TTL 12 h，那一份 `"[]"` 本身是惰性的；**但产生它的机制还在**：
任何一只 Yahoo 暂时给出空 `Earnings Date` 列表的现役票，都会被兜底写成 `"[]"`（mtime 新鲜、TTL 内会被原样返回）。

## 3. 下游在日期过期 / 为 `"[]"` 时的实际行为

**生产链路上 `get_earnings_date` 不会返回超过 12 h 的内容**，所以「过期」只可能来自 yfinance 本身
（财报刚过、Yahoo 还没换成下一季）或直接读文件的人。分读者：

- **扫描 / 跨式账本（`earnings_vol_signal` → `options_paper_leg`）**：过去的日期 ⇒ `as_of < ed` 不成立 ⇒ 不出信号；
  `"[]"` 同上。⇒ **永远是漏开仓方向，不会错开仓**。Chronos 主路径自己过滤 `days_until >= 0`，过去日期进不来。
- **卖权账本**：过去的日期 ⇒ `"none"`（被当成「视野内没有财报」进独立单位）；`"[]"` ⇒ `"unknown"`（排除）。
  过去日期 ⇒ none 的近似在「刚发完财报、下一季在 ~90 天后」时多数成立，但不是保证。
- **Alpha Bot 溢价页**（直接读文件）：v0.45.492 的「可解析、不早于今天、缓存 ≤ 45 天」能挡掉 `"[]"` 与已过日期，
  **挡不掉「日期在未来但预估已被改过」**。把现役 30 只缓存与 10-09 扫描时 Chronos 拿到的日期逐只对：

  | 状态 | 只数 | 票 |
  |---|---|---|
  | 一致 | 21 | — |
  | 缓存日期已过 | 2 | COST（缓存 09-24 / 新 12-10）、MU（09-30 / 12-23） |
  | 预估被改，缓存**更早**（保守方向） | 4 | ABBV 10-29→10-30、QCOM 10-29→11-04、TMUS 10-22→10-28、VZ 10-20→10-26 |
  | 预估被改，缓存**更晚**（危险方向：会把窗口内的财报看成窗口外） | 2 | **NEE 10-27→10-21**、**CRCL 11-11→11-04** |
  | 10-09 Chronos 无此票 | 1 | BILI（当天取数不可得） |

  哪一边是真的需对照公司 IR 公告（**待验证**）；两边都是 yfinance，但 Chronos 的是 4 天前的、缓存是 39 天前的。
  例：一张 10-23 到期的周度卖权，按缓存 NEE 是「财报在到期后」，按新值是「到期前」。
  另外缓存要 ≤ 45 天才算有效，09-04 那批会在 **10-19** 起整体失效 ⇒ 溢价页陆续显示「未知」（与 v0.45.492 已知一致）。

## 4. 修法选项

| # | 做什么 | 治什么 | 代价 / 风险 |
|---|---|---|---|
| **A（推荐，必做）** | `get_earnings_date` 解析收紧：`Earnings Date` 为空列表 / 解析不出 `YYYY-MM-DD` ⇒ 不写缓存、返回 `None`（或显式 `{"earnings_date": None, "reason": …}`）；写入前 `date.fromisoformat` 校验。补「非空日历 + 空列表」测试，并做「删掉校验 ⇒ 测试变红」的变异 | `"[]"` 机制本身 | 小；只动 watcher。`earnings_vol_signal._precheck` 顺手改为先解析日期再比较，别靠 ASCII 序 |
| **B（推荐）** | Alpha Bot 溢价页的财报日**不读 `_date.json`**，改读最近一次扫描里 Chronos 给出的日期（卖权账本 `earnings_date` 列 / swarm 结果快照），注明「截至 YYYY-MM-DD 扫描」 | 溢价页 10-19 后变「未知」、以及 NEE/CRCL 那种危险方向的旧预估 | 中；数据来源与扫描单一真相对齐。需要在 v0.45.492 那条线上做（协调另一个 session） |
| C | ChronosBee 的 `days_until` 改用日期算术（`(d - today).days`），当天财报不再被丢 | 财报当天被记成 `none` 的结构风险 | ⚠️ **会改 catalyst 维度分数** ⇒ 触发 IC 世代边界、要在 `ic_rerun_readiness._COHORT_HISTORY` 登记；也会影响维度 IC 协议的窗口。替代：**纯新增**一个不进评分的 `earnings_dates_raw` 细节字段，让 `_earnings_date_from_swarm` 读它，Chronos 分数不变 |
| D | 兜底失败可见化：`_fallback` 失败 / 返回 None 计数写进 `scan_timing.extra`，非零进 P2；`earnings_vol_signal` 把「取不到」与「确实没有」分成两个原因串 | 「谁会红」答案从「没人」变成 P2 | 小 |
| E | 承认 `_date.json` 是「兜底专用」缓存：在模块 docstring 写明「本缓存只在兜底时刷新，**不得直接读文件**」，并加一条 AST 守卫禁止 `earnings_watcher` 以外的模块拼 `_date.json` 路径 | 防止再出现第二个直接读文件的读者 | 小；与 B 配套 |
| F | 清理 11 个孤儿 `_date.json` | 外观 | **数据动作，需用户批准**；它们是可再生缓存（`data_backup/export.py` 标 rebuildable），生产不读，不清也无害 |

**不推荐**：让扫描每天把 Chronos 的日期回写进 `_date.json`（双写 ⇒ 两份真相，且回写本身又是一个「失败谁知道」的新出口）；
或把 TTL 调长让旧值「多活几天」（正是 §3 那种陈旧预估被当真的来源）。

建议顺序：A + D（一个小版本）→ B + E（与 v0.45.492 协调）→ C 走「纯新增字段」方案，避免动评分。

## 结局（v0.45.500，同日）

采用「财报日单一真相」：新模块 `earnings_calendar.py`（唯一解析器 + 唯一读取入口）；ChronosBee 纯新增
`details["earnings_calendar"]`（未过滤原始日期，评分口径不动）；日报 / 跨式 / 卖权 CLI 改接线、不再打网兜底；
判不了的票数进 `scan_timing.extra.earnings_dates` ⇒ P2。卖权预注册按「修 bug、不截断」记录（用户拍板）。
§4 的 A（解析收紧）、C（纯新增字段方案）、D（可见化）、E（唯一入口守卫）都已包含；B 由溢价页那条线自行完成
（改读卖权账本行，即本入口的产出）；F（清孤儿文件）未做，待用户决定。详见 CHANGELOG 0.45.500。
