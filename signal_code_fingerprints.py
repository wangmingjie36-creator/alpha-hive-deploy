#!/usr/bin/env python3
"""
🐝 Alpha Hive — 输入层代码指纹登记表（v0.45.554，buzz_v1 阶段 2 第 1 块）
==========================================================================
维度 IC 协议修订 1（`experiments/dim_ic_preregistration.md` §6「按层声明」、§13.2）：H1 检验冻结版 Buzz 评分
buzz_v1。冻结评分器挡得住「通道值 → score」这一层，挡不住通道本身——通道代码一改，H1 必须在那天截断，
前提是改的人在 `ic_rerun_readiness._COHORT_HISTORY` 登记边界、并在 `signal_archive.COHORT_SIGNAL_SCOPE`
点名对应的输入层信号。

v0.45.330 的 `_buzz_scope_gaps` 只守一个方向：「点名了通道，必须连带点名分」。**反方向没人守**：改了通道代码，
却只点名分（或干脆没登记）——H1 会把一次输入层改动当成冻结层改动吸收掉。窗口已开（2026-10-12），这种错不可逆。

本模块把「哪些代码决定 H1 的输入层」写成可算的指纹（`UNITS`），并要求**每次指纹变化都有一条确认**（`ACKS`）：

  · 确认 = `_COHORT_HISTORY` 里的一个 version，且它的影响面（经依赖边闭包）点名了该单元的信号 ⇒ H1 随之截断；
  · 确认 = ``measurement_fix``：修的是原始输入的**测量误差**（判据同 `signal_archive` 世代边界注释：改的不是
    「产生该值的函数」）；已知坏行走 `signal_archive.QUARANTINE`；
  · 确认 = ``no_value_change``：重构 / 清理，产出值逐位不变；
  · ``baseline``：每个单元的第一条，记窗口起点时的代码。

两种非边界确认都必须写明凭什么，进 git 历史、可审。**判断交给人，守卫只保证「没人能忘了判断」。**

指纹
----
去掉 docstring 后 `ast.unparse` 的 sha256 前 16 位：注释、空行、docstring 不影响；任何代码改动都会变。
单元的定位器见 `_LOCATORS`；定位不到（文件 / 函数 / 赋值不见了）⇒ 抛 `FingerprintError`，不当作「没变」。

⚠️ 已知不管的（写在这里，免得被当成「守住了」）
----------------------------------------------
- **取数细节**：行情的数据源优先级、重试、限流、forming-bar 护栏等不在指纹里——只有「提到三个行情字段的
  定义 / 函数」在（`mentions_price_fields`）。那一层的改动按测量误差处理、坏日子走 QUARANTINE。
- **LLM 新闻增强**（`llm_service.analyze_news_sentiment`）：只在 LLM 可用时生效，生产恒 `--no-llm`；
  切模式不经代码改动，指纹管不到。
- **数据本身**：上游 API 自己改了口径（如 F&G 源站改算法），代码没动，指纹也不会变。
- **Python 小版本升级**可能改 `ast.unparse` 的输出 ⇒ 全部单元同时失配；核实是升级所致后，整批登记
  ``no_value_change``（写明升级前后版本）。

谁会红
------
- 开发期：`tests/test_signal_code_fingerprints.py`（CI 每次推 main 都跑）。
- 运行期：`experiments/dim_ic_forward_test.py` 的进度行（就绪度闸 `--quiet` 第四段）——有未确认的改动 ⇒ ⚠️；
  到检视点时仍未确认 ⇒ 「无法判定」，不出结论。

用法
----
    /usr/local/bin/python3 signal_code_fingerprints.py          # 各单元当前指纹 vs 登记
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

#: 三个行情字段：Buzz 的动量 / 量比 / 波动率通道由它们映射而来（`price.*` 也取自 Buzz 的 details）
PRICE_FIELDS = frozenset({"momentum_5d", "volume_ratio", "volatility_20d"})

_BUZZ_CHANNELS: Tuple[str, ...] = (
    "buzz.comp.momentum_signal", "buzz.comp.volume_signal", "buzz.comp.volatility_signal",
    "buzz.comp.reddit_signal", "buzz.comp.news_signal", "buzz.comp.yahoo_signal",
    "buzz.comp.fear_greed_signal",
)

#: 单元名 → {"code": ((相对仓库根的文件, 定位器), …), "signals": 该单元决定的归档信号}
#: 信号并集必须恰好等于维度 IC 协议的 `H1_INPUT_SIGNALS`（测试钉住：多了会误要求确认，少了会漏守）
UNITS: Dict[str, Dict] = {
    # Buzz 自己算通道的那段：analyze() 里合成之前的全部语句 + 模块级辅助函数；通道读的阈值；缩放用的 _safe_score
    "buzz.channels": {
        "code": (("swarm_agents/buzz_bee.py", "buzz_channel_block"),
                 ("swarm_agents/cache.py", "def:_safe_score"),
                 ("config.py", "agent_scoring:volume_thresholds"),
                 ("config.py", "agent_scoring:volatility_thresholds")),
        "signals": _BUZZ_CHANNELS,
    },
    "buzz.reddit": {"code": (("reddit_sentiment.py", "module"),),
                    "signals": ("buzz.comp.reddit_signal",)},
    # 新闻主源切换（v0.45.445 AV → Massive，量纲随之变）就发生在这个文件里
    "buzz.news": {"code": (("newsapi_client.py", "module"),),
                  "signals": ("buzz.comp.news_signal",)},
    "buzz.yahoo": {"code": (("yahoo_trending.py", "module"),),
                   "signals": ("buzz.comp.yahoo_signal",)},
    "buzz.fear_greed": {"code": (("fear_greed.py", "module"),),
                        "signals": ("buzz.comp.fear_greed_signal", "market.fear_greed")},
    # 三个行情字段的公式：所有提到这三个字段名的顶层定义（自动识别——新增一条算动量的路径，指纹就变）
    "price.fields": {
        "code": (("data_pipeline.py", "mentions_price_fields"),
                 ("swarm_agents/cache.py", "mentions_price_fields"),
                 ("price_history.py", "mentions_price_fields"),
                 ("twelve_data.py", "mentions_price_fields"),
                 # v0.45.580：观测层（行情日线 / 交易所时钟的取数）整份算进来——它不提字段名，按名字认不出，
                 # 但它决定三个字段的原始输入；「观测层版本化」接上双版本之前，改它一律要确认
                 ("observation_layer.py", "module"),
                 # v0.45.639：三个字段的公式收成唯一实现（此前 data_pipeline 三份 + cache.py 一份副本），搬出去就必须跟进来，
                 # 否则改公式不会红
                 ("price_metrics.py", "mentions_price_fields")),
        "signals": ("price.momentum_5d", "price.volume_ratio", "price.volatility_20d",
                    "buzz.comp.momentum_signal", "buzz.comp.volume_signal", "buzz.comp.volatility_signal"),
    },
}

#: 确认记录，**只追加**。每条 = (单元, 指纹, 确认方式, 凭什么)。每个单元的最后一条必须等于当前指纹。
#: 确认方式：``baseline`` / ``measurement_fix`` / ``no_value_change`` / `_COHORT_HISTORY` 的 version（如 "v0.45.600"）
#:
#: v0.45.554 建表时的窗口审计：在每个扫描日实际跑的 commit 上重算指纹（commit 由生产日志 `alpha_hive.code_version`
#: 反推：10-09 acae617f / 10-12 0993efde / 10-13 c39baa27 / 10-14·15 fb3a8d46 / 10-16 54c3d411）。
#: 窗口内只有 price.fields 在 10-16 变过一次（v0.45.505）；其余五个单元 10-12 至今逐位未变。
_BASELINE_NOTE = ("窗口起点：维度 IC 协议窗口首个扫描日 2026-10-12 实际运行的代码（0993efde，生产日志 "
                  "alpha_hive.code_version 核实），H1 的全部窗口样本都出自它或其后已确认的改动")
ACKS: List[Tuple[str, str, str, str]] = [
    ("buzz.channels",   "38057d4d25d00bd1", "baseline", _BASELINE_NOTE),
    ("buzz.reddit",     "1cd1f6b26fac0af6", "baseline", _BASELINE_NOTE),
    ("buzz.news",       "d8380c2905cfb846", "baseline", _BASELINE_NOTE),
    ("buzz.yahoo",      "09ad9a7de4afb6d7", "baseline", _BASELINE_NOTE),
    ("buzz.fear_greed", "f3c8b454753d5cd5", "baseline", _BASELINE_NOTE),
    ("price.fields",    "fa516967f8a39012", "baseline",
     _BASELINE_NOTE + "；10-09→10-12 的变化（当天上午 v0.45.462~477 动量 / 量比回落收口）在首个窗口扫描之前，属起点"),
    ("price.fields",    "5695d133951a5c2d", "measurement_fix",
     "v0.45.505（10-16 扫描起）：Twelve Data 量比兜底只在末根 = 最近一场已收盘时采用。收盘后扫描里 TD 止于 D-1，"
     "旧逻辑把前一交易日的比值记成当天——用错场次的数据是测量误差，修法是拒收（值变 None、通道回中性 50），"
     "不是换函数。窗口内被旧兜底污染的 10-14 共 29 行 price.volume_ratio 已由 v0.45.508 隔离（用户 10-16 批准）；"
     "10-12/13/15/16 已按生产日志逐次扫描核实（v0.45.554 后续）：TD 兜底在窗口内只用过两次——10-12 一只（NFLX，该日整天按修订 2 排除）、10-14 三十只（29 只已隔离、JNJ 未入档）；10-13/15/16 为 0，无其余污染。同 v0.45.463/465 先例不登记边界——若改登记，"
     "它点名 price.volume_ratio（H1 输入层），会在 10-16 截断 H1、截断前仅 1 个合格周 ⇒ 协议直接判无法检验。"
     "505 当时写「待用户确认」，本条把该判断登记在案；**用户 2026-10-19 在对话里确认按测量误差修正**（v0.45.556 补记）"),
    ("price.fields",    "a88fcacdf09e1ead", "measurement_fix",
     "v0.45.557（10-20 推送 ⇒ 下一个扫描日起）：yfinance 缺 volatility_20d 时，用 Twelve Data adjust=all（分红复权，"
     "与 yfinance history 缺省 auto_adjust=True 同口径）定稿日线 + 源核实的当场官方收盘、同窗口 (S−1月, S]、同公式补上；"
     "窗口末端对不上 / 判不了就拒收。是同一个量、只是故障日从缺失（通道 50）变成有值：10-19 影子对比归档 926 行，正常日 "
     "742/742 同档、中位差 0、最大 0.062（VZ/T 复权因子略异），DELL 10-16 真实金值差 3e-7；已知分歧只在「故障 × 除息日」"
     "那一格（生产 yfinance 除息当晚按未复权，影子跨档 1/6）。窗口内波动率只在 10-14 缺过（该日 H1 已无效）。三个源同时"
     "补了 volatility_source 标签（不改值）。同 v0.45.463/465/505 先例不登记边界——**用户 2026-10-20 在对话里确认按测量误差修正**"),
    ("price.fields",    "61f89fa927efe733", "measurement_fix",
     "v0.45.562（557 二次检查，同一条波动率回落只收紧拒收规则）：① 先按日历数窗口交易日，< 20 就不发请求——一个月只有 19 个"
     "交易日的那些天（约每年 15 天）yfinance 主路径本就不出值、557 也是取回来再按根数拒收，值不变；② 复权口径字面量 adjust=\"all\" "
     "改由本函数传入，值不变（让守卫看得见口径）；③ 接核实收盘那天变动 > 30% 拒收（疑似 TD 未计入当天拆股），值只可能从错值变 None——"
     "与 505 同类的「拒收错口径数据」。557 的回落在生产里尚未产生过值（推送后首个扫描日即含本条）。沿用用户 2026-10-20 对这条回落"
     "「按测量误差修正」的确认；不登记边界"),
    ("price.fields",    "7b8e3c98ddf1e08c", "measurement_fix",
     "v0.45.564（10-20 推送 ⇒ 下一个扫描日起）：volatility_20d 门槛 20 → 19 根（yfinance 主路径三处 + swarm_agents/cache.py 回落 + "
     "Twelve Data 回落同改）。period=1mo 的窗口有时只有 19 个交易日（维度 IC 窗口一年内 17 天：2027-01-04 / 01-11 / 01-19 / 01-25、"
     "03-01~03-12、06-21 / 06-28、07-12），门槛 20 让那些天所有标的整天缺、通道回 50（2026-03-10/13、06-22、07-06 归档全是旧哨兵 0.0 为证）。"
     "19 根 = 同一个估计量少一个观测，只有这些天从缺变有值、其余日子逐位不变；窗口内这 17 天都还没到，不改变任何已入窗样本。"
     "仍不覆盖的：18 个交易日的窗口（2020~2035 共 5 天，窗口内没有）。**用户 2026-10-20 在对话里选「门槛从 20 根降到 19 根」、"
     "按测量误差修正（性质同 557：故障 / 缺口日从缺失变有值）**；不登记边界"),
    ("price.fields",    "e46cac678b378802", "measurement_fix",
     "v0.45.566（10-20 推送 ⇒ 下一个扫描日起）：`--date` 补跑路径（_fetch_historical_stock_data）的动量 / 量比 / 波动率先截到与实时扫描"
     "同一个窗口 (as_of−1 个日历月, as_of] 再算；窗口左端抽成唯一实现 _volatility_20d_window_lo（Twelve Data 回落同用，回落的值逐位不变）。"
     "此前补跑直接用多拉的 60 天（约 40 根）算波动率，与实时（约 21 根）不是同一个量——2026-08-27 归档 30/30 符合 60 天窗口（次日补跑）。"
     "改后补跑 = 实时：NVDA 10-16 用真实日线补跑得 24.6525、等于那天实时入档值；同一份日线两条路径四个字段逐位一致。只影响补跑出来的日子，"
     "维度 IC 窗口内目前没有（10-12~10-19 正常日都是实时扫描、符合一个月窗口），不改变任何已入窗样本。"
     "**用户 2026-10-20 在对话里说「现在修补跑路径的 60 天窗口」**（对应我的建议：改成与实时同一窗口、按测量误差修正）；不登记边界"),
    # v0.45.568（buzz_v1 根治阶段 1，原始观测入档）：四个通道模块只加「把取到的原始内容交给 observation_archive」与结果里的
    # `observation` 溯源键，评分路径一个字节没动。证据同一份，四条各写一遍（每条都要能单独读懂）。
    ("buzz.reddit",     "ed4f8f7798e1f52b", "no_value_change",
     "v0.45.568：_fetch_ranking 每次真取数后 observation_archive.observe / 失败 fetch_failed，get_ticker_sentiment 给结果加 "
     "observation={两张榜的内容地址}（安静结果同加）；评分算式与去向不变。改动前（origin/main 原文）与改动后在同一批随机榜单上"
     "逐字段比对 4500 个结果全同（档案关 / 开两遍，去掉时间戳与新键），真实 ApeWisdom 榜单重放 3 只全同。"
     "**用户 2026-10-20 在对话里选「按 no_value_change 登记」**；不登记边界"),
    ("buzz.news",       "b8ca2ec2930beb38", "no_value_change",
     "v0.45.568：Massive / AV / Yahoo 三个取数函数拿到原始条目后 observation_archive.observe（AV 的 apikey 不进请求记录），"
     "结果加 observation 键（进派生缓存）；get_ticker_news 在主源 / Yahoo 没拿到内容时记失败事件。清洗、去重、打标、算分与降级去向不变。"
     "改动前后在 600 例随机新闻（含限速 / 空 feed / 去重 / 两种主源）上逐字段比对全同（含主源结局计数），真实 Massive 3 只重放全同。"
     "**用户 2026-10-20 在对话里选「按 no_value_change 登记」**；不登记边界"),
    ("buzz.yahoo",      "ceec8fceb4921eae", "no_value_change",
     "v0.45.568：get_trending_tickers 真取数后 observe（存的就是缓存里那张代码表）/ 失败记事件，get_ticker_attention 用 reuse "
     "给结果加 observation 键（缓存命中也补档）；名次 → 关注度的算式不变。改动前后 3000 个随机结果逐字段全同、真实热搜表重放 3 只全同。"
     "**用户 2026-10-20 在对话里选「按 no_value_change 登记」**；不登记边界"),
    ("buzz.fear_greed", "4da4f4f1e1317e2f", "no_value_change",
     "v0.45.568：CNN / alternative.me 取到后 observe（CNN 存 fear_and_greed 对象，不存历史序列）、结果加 observation 键，"
     "两源都失败时各记一条失败事件；URL 字面量改成同值常量。取值、取整（round half even）、兜底顺序不变。改动前后 400 例随机读数"
     "（含解析失败 / CNN 挂 / 双挂）逐字段全同，真实 CNN 重放全同。**用户 2026-10-20 在对话里选「按 no_value_change 登记」**；不登记边界"),
    # v0.45.580（buzz_v1 根治阶段 1b，行情原始日线入档）：data_pipeline 的 yfinance 日线 / SPY 时钟改经新的 observation_layer 取
    # （取到即原样落档、返回内容不变），三条路径登记用了哪几份观测、回落补上的字段登记「用了但没存」；observation_layer.py 整份并入本单元
    ("price.fields",    "ec921fd7cee79071", "no_value_change",
     "v0.45.580：只多存、不改值。改动前（origin/main 原文）与改动后在同一批随机输入上逐字段比对——CBOE 胜出时的 _fetch_history_metrics 800 例、"
     "YFinanceSource.fetch 800 例（含 session_close）、--date 补跑 300 例（含云端快照拼接那一支）、MultiSourceFetcher 出口回落 200 例（含 "
     "history_fill 计数），档案关 / 开两遍全部相同；真实 yfinance 数据（10-21 盘中、末根正在形成、被丢弃）用档案里的日线 + 时钟重跑 33 项全同。"
     "**用户 2026-10-21 在对话里选「按 no_value_change 登记」**；不登记边界"),
    # v0.45.591（观测层版本化第一块，取数缓存存原始观测）：新闻 / F&G 的缓存改存「取数结局」（种类 + 原始内容 + 取数时刻 + 新闻主源结局），
    # 命中时用与新取数同一个解释函数重算并补档；Reddit 单票派生缓存删除；缓存读写收进 observation_layer。证据同一份，四条各写一遍
    ("buzz.reddit",     "620b3abcf7ca477e", "no_value_change",
     "v0.45.591：删掉单票派生缓存（<票>_sentiment.json，10 分钟），每次都从两张榜（本就是原始内容的内存 / 磁盘缓存）算；评分算式一字未动。"
     "旧 / 新代码在同一批随机榜单上首调 800 组、新实例二次调用 800 组（每组 4 只票）除 timestamp 外逐字段相同；生产档案 10-20/21 真实榜单 4×4 组合 × 30 只票 "
     "480 例全同、新代码的地址与档案一致。唯一差别：跨进程命中时 timestamp 变成这次算的时刻（无读者）；另一进程先算过的票若榜单缓存已过期，"
     "新代码重取榜（取的是哪一份，不是怎么算），桥接按链接的那份重算、不受影响。**用户 2026-10-22 在对话里选「四个都按 no_value_change」**；不登记边界"),
    ("buzz.news",       "e345ed268cf41cd6", "no_value_change",
     "v0.45.591：Massive / AV / Yahoo 三段解释拆成 _from_massive / _from_alpha_vantage / _from_yahoo（新取数与缓存命中共用），缓存改存取数结局"
     "（cache/news/<票>_news.obs-1.json），命中时重算、还原当初的 timestamp 与主源结局。旧 / 新代码在 800 例随机输入（含限速 / 拒绝 / 空 feed / "
     "去重 / 三种主源 / 解析失败，时钟在命中前拨快）上新取数、缓存命中、主源结局计数三项逐字段全同；生产档案 54 份真实 Massive 结果重放全同、地址一致。"
     "**用户 2026-10-22 在对话里选「四个都按 no_value_change」**；不登记边界"),
    ("buzz.fear_greed", "e0989b9da49b66f7", "no_value_change",
     "v0.45.591：CNN / alternative.me 的换算拆成 _from_cnn / _from_alternative_me（新取数与缓存命中共用），缓存改存取数结局"
     "（cache/fear_greed.obs-1.json），命中时重算、timestamp 沿用取数时刻（BuzzBee 记成 fetched_at）。取值、取整、兜底顺序不变；"
     "旧 / 新代码 800 例随机读数（含解析失败 / CNN 挂 / 双挂 / 空 data）新取数与命中逐字段全同，生产档案 2 份真实 CNN 重放全同、地址一致。"
     "**用户 2026-10-22 在对话里选「四个都按 no_value_change」**；不登记边界"),
    ("price.fields",    "ee56f994493ebb89", "no_value_change",
     "v0.45.591：observation_layer.py 整份在本单元里，这次只新增取数缓存的 cache_path / cache_read / cache_write / _cache_doc_ok 与两个常量"
     "（情绪通道用）；行情日线 / 交易所时钟的 7 个函数 AST 逐个比对一字未改，行情路径不调用新函数。"
     "**用户 2026-10-22 在对话里选「四个都按 no_value_change」**；不登记边界"),
    # v0.45.601（修 CI 专属红：buzz_v1 价格差分对拍）：data_pipeline 三处 `pct_change()` 写明 `fill_method=None`
    ("price.fields",    "e512d9978a3c1a91", "no_value_change",
     "v0.45.601：_fetch_history_metrics / YFinanceSource.fetch / --date 补跑 _fetch_historical_stock_data 三处 hist['Close'].pct_change() "
     "写明 fill_method=None。生产解释器 /usr/local/bin/python3（编排器 PYTHON3 即它）的 pandas 是 3.0.1（pandas-3.0.1.dist-info 的 "
     "METADATA mtime 2026-02-24 22:27，早于 H1 窗口起点 2026-10-12），该版本 inspect.signature(pd.Series.pct_change) 的缺省就是 "
     "fill_method=None ⇒ 显式化在生产上是空操作。改动前（origin/main 原文）与改动后在同一批随机日线上逐字段比对：实时 800 份日线（108 份含 NaN 收盘）"
     "各过 _fetch_history_metrics 与 YFinanceSource.fetch、补跑 300 份（40 份含 NaN 收盘），共 1900 次比对，pandas 3.0.1 下全部相同；同一比对在 pandas 2.3.3 下出差异"
     "（正对照：2.x 缺省先前向填充，NaN 那天收益记 0）。冻结通道层 buzz_v1_channels.price_fields 本就按不填充定义；"
     "CI 照 requirements.txt 装 pandas<3，差分对拍在那里红的就是这个。"
     "**用户 2026-10-22 在对话里选「按 no_value_change 登记」**（经发起本会话的会话转述原话）；不登记边界"),
    ("price.fields",    "100be9b271010efa", "measurement_fix",
     "v0.45.639：三个字段的公式收成唯一实现 price_metrics.py（并入本单元）。此前 data_pipeline 三份（YFinanceSource.fetch / "
     "_fetch_history_metrics / _fetch_historical_stock_data）+ swarm_agents/cache.py 旧回落一份副本，Twelve Data 回落的列表版公式也逐字搬过去。"
     "主路径逐位不变：origin/main 原文的 data_pipeline 与改后在同一批随机日线（含 NaN 收盘 / NaN 成交量 / 零成交量）上真函数对拍——"
     "_fetch_history_metrics 800、YFinanceSource.fetch 800、补跑 300，共 1900 次全部相同；列表版 500 组逐位相同。"
     "值只在 cache.py 旧回落（data_pipeline 不可用时才走）上变：均量 NaN / ≤0 时不再以 1.0 作分母（此前 volume_ratio = 当天原始成交量、"
     "avg_volume = 1——主路径 v0.45.54 / 473 修过的同一个 bug）、补上 fill_method=None——都是测量误差修正，且生产全史 Buzz details 里"
     "量比 > 1000 / 三字段 NaN 均 0 次（旧回落未见走过的痕迹）。主路径的 NaN 行为刻意未动（冻结层 buzz_v1_channels 原样复刻它）。"
     "二次检查同版补两处（仍只在旧回落上）：初始 avg_volume 0（「0 股」）→ None 与主路径一致；旧回落计数加评分时快照（不进公式）。"
     "**用户 2026-10-24 在对话里选「measurement_fix」**；不登记边界"),
]

NON_BOUNDARY_KINDS = frozenset({"baseline", "measurement_fix", "no_value_change"})
MIN_NOTE_CHARS = 20


class FingerprintError(Exception):
    """定位不到要取指纹的代码 ⇒ 守卫无法判断，不是「没变」。"""


# ────────────────────────────────────────────────────────────────────────────
# 定位器：源码文本 → 归一化文本
# ────────────────────────────────────────────────────────────────────────────

def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _parse(src: str, rel: str) -> ast.Module:
    try:
        return _strip_docstrings(ast.parse(src))
    except SyntaxError as e:
        raise FingerprintError(f"{rel} 解析失败：{e}") from e


def _top_defs(tree: ast.Module):
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _mentions_fields(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and n.value in PRICE_FIELDS:
            return True
        if isinstance(n, ast.Attribute) and n.attr in PRICE_FIELDS:
            return True
        if isinstance(n, ast.Name) and n.id in PRICE_FIELDS:
            return True
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(f in n.name for f in PRICE_FIELDS):
            return True
    return False


def _loc_module(tree: ast.Module, rel: str, _arg: str) -> str:
    return ast.unparse(tree)


def _loc_def(tree: ast.Module, rel: str, name: str) -> str:
    for n in _top_defs(tree):
        if n.name == name:
            return ast.unparse(n)
    raise FingerprintError(f"{rel} 里找不到顶层定义 {name}")


def _loc_mentions(tree: ast.Module, rel: str, _arg: str) -> str:
    hits = sorted((n for n in _top_defs(tree) if _mentions_fields(n)), key=lambda n: n.name)
    if not hits:
        raise FingerprintError(f"{rel} 里没有任何提到 {sorted(PRICE_FIELDS)} 的顶层定义——结构变了，守卫需要重新定位")
    return "\n".join(f"# {n.name}\n{ast.unparse(n)}" for n in hits)


def _is_composite_assign(stmt: ast.stmt) -> bool:
    return (isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "sentiment_composite" for t in stmt.targets))


def _loc_buzz_channels(tree: ast.Module, rel: str, _arg: str) -> str:
    """模块级辅助函数 + `BuzzBeeWhisper.analyze` 里 `sentiment_composite = …` 之前的全部语句
    （合成本身与之后的调整属于冻结层，不在这里——改它们不该要求输入层确认）。"""
    helpers = sorted((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                     key=lambda n: n.name)
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BuzzBeeWhisper"), None)
    fn = next((n for n in (cls.body if cls else []) if isinstance(n, ast.FunctionDef) and n.name == "analyze"), None)
    if fn is None:
        raise FingerprintError(f"{rel} 里找不到 BuzzBeeWhisper.analyze")
    pre: List[ast.stmt] = []
    found = False
    for stmt in fn.body:
        if not any(_is_composite_assign(s) for s in ast.walk(stmt) if isinstance(s, ast.stmt)):
            pre.append(stmt)
            continue
        if not isinstance(stmt, ast.Try):
            raise FingerprintError(f"{rel}：sentiment_composite 的赋值不在 analyze 的 try 块里——结构变了")
        for s in stmt.body:
            if _is_composite_assign(s):
                found = True
                break
            pre.append(s)
        break
    if not found:
        raise FingerprintError(f"{rel}：analyze 里找不到 `sentiment_composite = …`——结构变了，守卫需要重新定位")
    parts = [f"# helper {n.name}\n{ast.unparse(n)}" for n in helpers]
    parts.append("# analyze before composite\n" + "\n".join(ast.unparse(s) for s in pre))
    return "\n".join(parts)


def _loc_agent_scoring(tree: ast.Module, rel: str, key: str) -> str:
    for n in tree.body:
        if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "AGENT_SCORING" for t in n.targets)
                and isinstance(n.value, ast.Dict)):
            for k, v in zip(n.value.keys, n.value.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    try:
                        return json.dumps(ast.literal_eval(v), sort_keys=True, ensure_ascii=False)
                    except ValueError as e:
                        raise FingerprintError(f"{rel}：AGENT_SCORING[{key!r}] 不是字面量（{e}）") from e
            raise FingerprintError(f"{rel}：AGENT_SCORING 里没有 {key!r}")
    raise FingerprintError(f"{rel} 里找不到字面量字典 AGENT_SCORING")


_LOCATORS: Dict[str, Callable[[ast.Module, str, str], str]] = {
    "module": _loc_module, "def": _loc_def, "mentions_price_fields": _loc_mentions,
    "buzz_channel_block": _loc_buzz_channels, "agent_scoring": _loc_agent_scoring,
}


# ────────────────────────────────────────────────────────────────────────────
# 指纹
# ────────────────────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    """代码锚点（不是数据）：要的就是「源码在哪」。调用时求值。"""
    return Path(__file__).resolve().parent


def _default_read(rel: str) -> str:
    path = _repo_root() / rel
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise FingerprintError(f"读不到 {path}：{e}") from e


def unit_fingerprint(unit: str, read: Optional[Callable[[str], str]] = None) -> str:
    """单元的指纹。`read(相对路径) -> 源码文本`，缺省读本仓文件（历史审计时传 `git show`）。"""
    read = read or _default_read
    parsed: Dict[str, ast.Module] = {}
    h = hashlib.sha256()
    for rel, locator in UNITS[unit]["code"]:
        kind, _, arg = locator.partition(":")
        if kind not in _LOCATORS:
            raise FingerprintError(f"未知定位器 {locator!r}")
        if rel not in parsed:
            parsed[rel] = _parse(read(rel), rel)
        text = _LOCATORS[kind](parsed[rel], rel, arg)
        h.update(f"{rel}::{locator}\n{text}\n\0".encode("utf-8"))
    return h.hexdigest()[:16]


def last_ack(unit: str, acks: Optional[list] = None) -> Optional[Tuple[str, str, str, str]]:
    rows = [a for a in (ACKS if acks is None else acks) if a[0] == unit]
    return rows[-1] if rows else None


def check(read: Optional[Callable[[str], str]] = None, acks: Optional[list] = None) -> Dict:
    """各单元当前指纹 vs 最后一条确认。`ok` 只有在全部单元都 ok 时为真。

    单元状态：`ok` / `unacknowledged`（代码变了、没有确认）/ `error`（算不出指纹——同样不是 ok）。
    """
    units: Dict[str, Dict] = {}
    for unit in UNITS:
        ack = last_ack(unit, acks)
        try:
            fp = unit_fingerprint(unit, read)
        except FingerprintError as e:
            units[unit] = {"state": "error", "error": str(e), "acknowledged": ack[1] if ack else None}
            continue
        state = "ok" if ack is not None and ack[1] == fp else "unacknowledged"
        units[unit] = {"state": state, "current": fp, "acknowledged": ack[1] if ack else None}
    return {"ok": all(u["state"] == "ok" for u in units.values()), "units": units}


def registry_problems(acks: Optional[list] = None, history: Optional[list] = None,
                      scope: Optional[Dict] = None) -> List[str]:
    """`ACKS` 本身的毛病（空列表 = 没毛病）。

    - 单元必须已知、指纹 16 位十六进制、每个单元至少一条且第一条（且只有第一条）是 ``baseline``；
    - 非边界确认的说明 ≥ `MIN_NOTE_CHARS` 字；
    - 边界确认：version 必须在 `_COHORT_HISTORY` 里、在 `COHORT_SIGNAL_SCOPE` 里**声明过**（未声明边界按「影响全部」
      处理，证明不了它是冲着这个单元去的），且影响面经依赖边闭包确实触及该单元的信号——
      判定复用 `signal_archive.generation_boundaries()`，不另写一份。
    """
    import re

    import signal_archive
    if history is None:
        import ic_rerun_readiness
        history = ic_rerun_readiness._COHORT_HISTORY
    scope = signal_archive.COHORT_SIGNAL_SCOPE if scope is None else scope
    acks = ACKS if acks is None else acks
    by_version = {v: (d, v, r) for d, v, r in history}
    out: List[str] = []
    seen: Dict[str, int] = {}
    for i, (unit, fp, kind, note) in enumerate(acks):
        tag = f"ACKS[{i}] {unit}"
        if unit not in UNITS:
            out.append(f"{tag}：未知单元")
            continue
        if not re.fullmatch(r"[0-9a-f]{16}", fp or ""):
            out.append(f"{tag}：指纹 {fp!r} 不是 16 位十六进制")
        first = unit not in seen
        seen[unit] = seen.get(unit, 0) + 1
        if kind == "baseline" and not first:
            out.append(f"{tag}：baseline 只能是该单元的第一条")
        if kind != "baseline" and first:
            out.append(f"{tag}：每个单元的第一条必须是 baseline")
        if kind in NON_BOUNDARY_KINDS:
            if len((note or "").strip()) < MIN_NOTE_CHARS:
                out.append(f"{tag}：{kind} 必须写清凭什么（≥{MIN_NOTE_CHARS} 字）")
        elif re.fullmatch(r"v\d+\.\d+\.\d+(~\d+\.\d+\.\d+)?", kind or ""):
            if kind not in by_version:
                out.append(f"{tag}：{kind} 不在 _COHORT_HISTORY 里")
            elif kind not in scope:
                out.append(f"{tag}：{kind} 没在 COHORT_SIGNAL_SCOPE 声明影响面——证明不了它点名了这个单元")
            else:
                with _patched_scope(signal_archive, scope):
                    hit = signal_archive.generation_boundaries(UNITS[unit]["signals"], [by_version[kind]])
                if not hit:
                    out.append(f"{tag}：{kind} 的影响面没触及该单元的任何信号 {list(UNITS[unit]['signals'])}")
        else:
            out.append(f"{tag}：未知确认方式 {kind!r}")
    out += [f"单元 {u} 没有任何确认" for u in UNITS if u not in seen]
    return out


class _patched_scope:
    """让 `generation_boundaries` 用传入的影响面表（测试传合成表；缺省就是真表本身）。"""

    def __init__(self, mod, scope):
        self.mod, self.scope = mod, scope

    def __enter__(self):
        self.saved = self.mod.COHORT_SIGNAL_SCOPE
        self.mod.COHORT_SIGNAL_SCOPE = self.scope

    def __exit__(self, *exc):
        self.mod.COHORT_SIGNAL_SCOPE = self.saved
        return False


def how_to_acknowledge(unit: str, fp: str) -> str:
    """指纹失配时给改代码的人看的说明（测试与执行器共用）。"""
    sigs = ", ".join(UNITS[unit]["signals"])
    return (f"输入层代码单元 {unit!r} 变了（当前指纹 {fp}），它决定 H1 的输入：{sigs}。\n"
            f"三选一，然后在 signal_code_fingerprints.ACKS 末尾追加一条 ({unit!r}, {fp!r}, <确认方式>, <凭什么>)：\n"
            f"  ① 改了产生这些值的函数 ⇒ 在 ic_rerun_readiness._COHORT_HISTORY 登记边界，并在 "
            f"signal_archive.COHORT_SIGNAL_SCOPE 点名上面至少一个信号（会截断维度 IC 协议的 H1）；确认方式 = 该 version\n"
            f"  ② 修的是原始输入的测量误差（不是换函数）⇒ 确认方式 = 'measurement_fix'，坏日子另走 QUARANTINE\n"
            f"  ③ 重构 / 清理，产出值逐位不变 ⇒ 确认方式 = 'no_value_change'\n"
            f"②③ 要写清凭什么（≥{MIN_NOTE_CHARS} 字）。拿不准就选 ①——漏截断不可逆，多截断只损失样本。")


def main() -> int:
    res = check()
    for unit, u in res["units"].items():
        if u["state"] == "error":
            print(f"⚠️ {unit:<16} 无法计算：{u['error']}")
        elif u["state"] == "ok":
            print(f"✅ {unit:<16} {u['current']}（与最后一条确认一致）")
        else:
            print(f"⚠️ {unit:<16} 当前 {u['current']} ≠ 登记 {u['acknowledged']}\n" + how_to_acknowledge(unit, u["current"]))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
