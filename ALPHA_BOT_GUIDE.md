# Alpha Bot 使用说明与功能简介

> 本文是仓库内**速览**：第 1 节「它是什么」、第 2 节「怎么启动」、第 7 节「排错」写全（按 v0.45.612 核对过）；其余各节只给去处——逐页、逐概念的说明在程序内帮助页（v0.45.613 瘦身：此前那几节是帮助页的一份旧副本，停在 v0.45.407，漏了跨式 / 对冲 / 溢价等页）。本文写的是代码里**实际存在**的行为；页面上的数值口径、冻结规则常量以「口径」页为准（它直接读代码里那一份，不在前端抄第二份）。
> 下面的命令都带 `make -C ~/alpha-hive-prod` 或先 `cd ~/alpha-hive-prod`：在哪个目录敲都能跑（守卫 `tests/test_alphabot_launcher.py::TestMakeCommandsNameTheirDirectory`）。
> ⚠️ 与 `alpha_hive_bot/`（Telegram）、Slack 里的「Alpha Hive Bot」**无关**。

---

> **程序内也有这份说明（更详细）**：顶栏「帮助」，或各页页头的「怎么读这页 ?」。本文是仓库内速览；逐列、逐控件的解释、理由 / 不可报价代码释义、排错表以程序内帮助页为准（规则常量从服务端取，不会过期）。

## 1. 它是什么

Alpha Bot 是 **卖权行权价选择器的本机前端**，对标 GEXBot Classic，但更偏「卖权工作台」：

- **看 GEX 水平**：Zero Gamma、净 Major+/−、Gamma 曲线、DEX / Vanna / Charm、IV 微笑、期限结构；
- **做卖权决策**：按 delta 梯子列出候选行权价，给六种结构的报价与到期盈亏，并告诉你「环境路由」建议取哪一档；
- **看盘中变化**：关注列表在交易时段定时拍快照，回看水平怎么移动；
- **看账本进度**：每日收盘后记录的前向账本攒到多少样本、预注册检验什么时候能跑；
- **看纸面账本**：财报跨式、组合对冲的持仓与风险（只读）。

它**不产生评分**：不进 Alpha Hive 的 `final_score`，不上网站，不发 Slack。路由只决定「取梯子的哪一档」。

### 三条不变式（你不用操心，但值得知道）

| 不变式 | 含义 |
|---|---|
| 只绑回环 | 只监听 `127.0.0.1`；给别的 `--host` 直接退出码 2。账本是私有数据 |
| 账本只读 | 对卖权账本只读、从不冻结；冻结检验的唯一写者仍是日报钩子 |
| 自己的状态只写一处 | 设置与盘中快照写在 `PATHS.alphabot_state`，不碰别的目录 |

---

## 2. 怎么启动

### 方式 A：桌面程序（推荐日常使用）

首次生成（只需一次）：

```bash
make -C ~/alpha-hive-prod alphabot-app                        # 用生产克隆自己的代码生成
make -C ~/alpha-hive-prod alphabot-app PYTHON=/绝对路径        # 换 .app 用的 Python 时
```

`PYTHON` 只认命令行上写的（环境变量里的会被忽略），相对路径或只写名字会被拒。.app 跑的是生产克隆 `~/alpha-hive-prod`：
每个扫描日扫描前自动更新，关掉 Alpha Bot 再打开即用上新代码，**代码更新不用重新生成**。只有 .app 自己的启动壳变了才要重跑——那时打开 Alpha Bot 会弹一次提醒并给出命令。

生成 `~/Applications/Alpha Bot.app`，双击即用：

1. 首次启动会弹窗让你**选一次数据根**（生产为 `~/alpha-hive-data`），或选「本次演示模式」；选了之后记在 `~/Library/Application Support/Alpha Bot/launcher.json`。
   原因：GUI 程序拿不到 shell 里 `export` 的 `ALPHA_HIVE_HOME`，不选就会静默读错目录。
2. 启动器在后台起服务（端口 `8765`），等就绪后**开原生窗口**（pywebview / WKWebView）。
3. **只开一个窗口**：已有窗口时再双击，只会把它调到前面。
4. **关窗口（或 ⌘Q）= 停掉由 .app 起的服务**。终端里起的服务关窗不会停。
5. 没有装 pywebview 时自动退回浏览器（原因写进日志）。

缺省的代码目录（生产克隆）与数据根都不在「桌面」/「文稿」下，macOS 不会为它们弹访问授权；只有开发时用 `--repo` 把 .app 指到桌面上的检出，首次运行才会问一次，选允许。

### 方式 B：终端

```bash
make -C ~/alpha-hive-prod alphabot          # 等价于在该目录里 /usr/local/bin/python3 -m alphabot --open
```

没设 `ALPHA_HIVE_HOME` 时读缺省数据根 `~/alpha-hive-data`（v0.45.422 起；与编排器同址，就是生产数据），启动时 stderr 会说读的是哪个目录；数据根在别处才需要先 `export ALPHA_HIVE_HOME=<数据根>`（与编排器 / MCP 同一个值）。

### 方式 C：演示模式（不依赖真实数据）

```bash
make -C ~/alpha-hive-prod alphabot-demo
```

用合成期权链，**不读账本、不写真实状态目录**，页面顶部有「演示模式」横幅。适合第一次熟悉界面、或休市时看看长什么样。

### 常用命令

| 目的 | 命令 |
|---|---|
| 停止服务 | 页脚「停止服务」按钮，或 `make -C ~/alpha-hive-prod alphabot-stop` |
| 忘掉已选的数据根 | `cd ~/alpha-hive-prod && /usr/local/bin/python3 -m alphabot.launcher --reset` |
| 换端口 / 不跑盘中定时快照 | `cd ~/alpha-hive-prod && /usr/local/bin/python3 -m alphabot --port 9000`（加 `--no-poll` 不跑定时快照） |
| 日志 | `~/Library/Logs/Alpha Bot/launcher.log`、`server.log`（>5MB 轮转） |

> 解释器一律用 `/usr/local/bin/python3`，不要用裸 `python3`。.app 换过解释器的，以 .app 用的那个为准——程序内帮助页「排错」里的命令自动写成运行中的那个。
> `-m alphabot…` 要在代码目录里跑：不先 `cd` 就是 `No module named alphabot`。

---

## 3. 页面导览

顶栏的每个页面一行；逐列、逐控件的说明点页头「怎么读这页 ?」，或在 Alpha Bot 里打开表中的帮助地址。
这张表有守卫：顶栏加了页面却没写进来、或帮助章节改了名，`tests/test_alphabot.py::TestHelpPage` 会红。

| 页面 | 干什么 | 帮助 |
|---|---|---|
| 总览 | 账本某一日全部标的的一屏表（收盘后记录的行，不逐只现拉）；点行进卖权工作台 | `#/help/overview` |
| 标的 | 单票现算：水平、希腊值与期限、卖权工作台（含「怎么卖」栏）、溢价、价格等子页 | `#/help/levels` 起，各子页页头直达对应章节 |
| 盘中 | 关注列表在交易时段定时拍快照，看水平怎么移动（只看单日） | `#/help/intraday` |
| 跨式 | 财报跨式纸面账本：持仓、平仓、逐仓风险、盘中参考报价（只读） | `#/help/straddle` |
| 对冲 | 组合 Greeks 的纸面 SPY 覆盖账本（只读） | `#/help/hedge` |
| 账本 | 卖权前向账本的预注册检验就绪度（只看样本量与计数，不看结果） | `#/help/ledger` |
| 结果 | 预注册检验冻结后的结果；冻结前只说为什么锁着、还差多少 | `#/help/results` |
| 口径 | 单位、符号约定、证据等级、路由冻结规则、收益口径、局限 | `#/help/limits` |
| 帮助 | 程序内的完整说明 | `#/help` |

顶栏最右的「对话」不是页面，是任何页面都能打开的右侧抽屉：用 Claude 问 Alpha Bot 的数据（可联网搜索）。**默认关、每问都产生 API 费用**（每天有上限），你的问题与取到的数据会发给 Anthropic——开启前先读 `#/help/chat`。

---

## 4. 关键概念速查

GEX、Zero Gamma、净 Major±、DEX、基线档 / 远档、环境路由、flag、证据等级的定义见帮助页「概念速查」（`#/help/concepts`）与「环境路由」（`#/help/route`）。
路由的冻结数字取自代码（经 `/api/method`），本文不抄——抄一份就是会过期的快照。

---

## 5. 盲期：为什么有些东西不给看

几份前向检验还在攒样本，冻结之前 Alpha Bot 刻意不显示某些对比，页面上会写锁着什么、还差多少。
这是**行为规则**：数据都在本机，信息论上盲不了；自己把冻结前的东西拼起来比较就是偷看，会作废那次检验。
锁什么、为什么，见帮助页「盲期与检验」（`#/help/blind`，卖权路由）与「跨式账本」（`#/help/straddle`，跨式 × GEX、影子宽跨），以及各自的协议：

| 检验 | 协议 |
|---|---|
| 卖权环境路由 | `experiments/sell_strike_routing_prereg.md` |
| 跨式 × GEX | `experiments/straddle_gex_prereg.md` |
| 影子宽跨 | `experiments/straddle_strangle_shadow_prereg.md` |

---

## 6. 使用建议

见帮助页「使用建议」（`#/help/workflow`）。页面上的数字**不是投资建议**；局限与免责见「局限与免责」（`#/help/limits`）。

---

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| 双击后弹「服务启动失败」 | 看弹窗里的日志尾巴；日志在 `~/Library/Logs/Alpha Bot/`。常见：缺依赖（`starlette` / `uvicorn`），装进 .app 用的解释器：`cd ~/alpha-hive-prod && /usr/local/bin/python3 -m pip install -r requirements.txt`（换过解释器就换成那个；帮助页「排错」给出按本机实际解释器的整条命令） |
| 提示端口被占 | 8765 上是别的程序，弹窗会给 `lsof` 命令 |
| 总览 / 账本 / 结果显示「没找到卖权账本目录」 | 数据根选错了。`--reset` 后重选（启动器不核对所选目录里有没有账本） |
| 关窗后服务还在 | 升级到 v0.45.407 之前起的服务不带 `--from-app`，关窗不会停它；页脚「停止服务」或 `make -C ~/alpha-hive-prod alphabot-stop` 停一次，之后双击起的就按新规则走 |
| 「价格」页日线不可得 | 没配 Twelve Data key；设计如此，不换源 |
| 某标的「现在拿不到期权链」 | 页面会写具体原因（CBOE 取数失败等）；点「重新拉取」重试 |
| 换了 Python | `make -C ~/alpha-hive-prod alphabot-app PYTHON=/绝对路径`（先用新解释器装好依赖，见第一行） |
| 代码更新了要不要重新生成 | 不用：.app 跑生产克隆，扫描日扫描前自动更新，关掉 Alpha Bot 再打开即用上；只有启动壳变了，打开时会弹一次提醒并给出命令 |
| 弹窗说「生产克隆丢了或不完整」 | 照弹窗在开发检出里跑 `production_clone.py setup` 重建它（每日扫描也靠它）；重建后不用重新生成 .app |
| 想知道服务版本 / 要停服务 | 页脚显示版本号与「停止服务」按钮；盘中快照最近一轮的结果见盘中页底部说明与 `/api/meta` |

---

## 8. 文件地图（想看实现时）

- **前端**：顶栏每个页面一个 `alphabot/static/pages/<页面>.js`（帮助页是 `pages/help.js`），共用组件在 `static/lib.js`。
- **后端**：`alphabot/service.py`（业务层；**全仓唯一可 import 卖权模块的包**）、`alphabot/server.py`（接口与安全中间件）、
  `alphabot/synthetic.py`（演示模式的合成期权链）。
- **桌面程序**：`alphabot/launcher.py`（启动器）、`alphabot/macos_app.py`（.app 生成器）、`alphabot/window.py`（原生窗口）。
- **守卫**：`tests/test_alphabot*.py`；谁能 import 卖权模块的双向火墙在 `tests/test_sell_strike_integration.py`。

逐个文件不在这里列（列了就会像此前那样停在某一版）；设计与版本历史见 `CHANGELOG.md`。
