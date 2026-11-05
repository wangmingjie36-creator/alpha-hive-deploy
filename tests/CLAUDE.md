# 测试（`tests/`）· 分区工作记忆

> 读 / 改 `tests/` 下任何文件时 Claude Code 自动加载本文件。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来：「用户偏好」的 Python 解释器全文、「整套测试怎么跑」全文、四个硬检查项全文（skip 守卫 / 新产物默认路径 / 夹具日期 / 代码还是数据；标题与根目录同名，根目录只留核心一问与可执行规则）、「环境：iCloud 重名副本」正文。「这个失败，下游怎么知道？」全文仍在根目录；skip 一节开头「与上一节同源」的「上一节」指的就是它。

## Python 解释器硬规则（原文，原在根目录「用户偏好」）

- **⚠️ Python 解释器硬规则：扫描/脚本一律用 `/usr/local/bin/python3`（Python 3.11.1），禁用裸 `python3`**
  - 用户 Mac 有两个 Python：`/usr/bin/python3`=3.9.6（系统自带，**无 sklearn、缺 jinja2、PEP604 `X|None` 注解 import 即崩**）；`/usr/local/bin/python3`=3.11.1（python.org 安装包，软链到 `/Library/Frameworks/Python.framework`，**不是 Homebrew**；**真实环境**：sklearn/jinja2/yfinance 全装，PEP604 合法）
  - ⚠️ 它的包分装两处：numpy / pytest / jinja2 / starlette 等**只**在用户 site（`~/Library/Python/3.11`，按 `$HOME` 解析）⇒ 测试把 HOME 指到 tmp 后再起真解释器，子进程会丢这些包。`tests/conftest.py` 的 `_pin_child_user_site` 已全会话钉住 `PYTHONUSERBASE`（v0.45.426），新测试换 HOME **不用再自己钉**；只有从零构造 env（不继承 `os.environ`）的子进程要自己带上（详见 auto-memory `alpha-hive-environment-facts.md`）
  - 编排器顶部已显式 `PYTHON3="/usr/local/bin/python3"`；**手动/Claude 跑扫描必须同样显式用 `/usr/local/bin/python3 alpha_hive_daily_report.py ...`**，并 `export PATH="/usr/local/bin:$PATH"` 保证内部 spawn 的子 python 也走 3.11
  - 裸 `python3` 会解析成 3.9.6 → ML 降级 SimpleMLModel + PEP604 崩 + 缺 jinja2 崩（2026-06-30 事故根因）
  - 运行测试同理：`/usr/local/bin/python3 -m pytest`

## 整套测试怎么跑（2026-10-21 起）

- **开发期只跑改动相关的测试**（`/usr/local/bin/python3 -m pytest tests/test_xxx.py`），**整套留到收尾跑一次**。
  - **开发期选测工具** `tools/select_tests/select_tests.py`（`make test-select` 看方案、`make test-changed` 跑；v0.45.615，只做第一档）：改的全是**测试文件 / 被跟踪的非代码文件** ⇒ 跑「实测必跑清单」（读全部测试文件的元守卫 + 读过被改文件的测试，登记表 `tests/_must_run.json`，由审计钩子整套实测得出）；**命中生产代码 ⇒ 不猜、提示整套**（退出码 3），命中核心文件（`conftest.py` / `tests/_*` / `config.py` / `hive_logger.py` / pytest 配置 / `.github/` / `tools/clockshift|select_tests/`）⇒ 回退整套（退出码 10）；登记表读不了也回退整套。⚠️ 改了测试文件，元守卫就是「相关测试」——别再按文件名 grep 圈（旧 grep 清单多收 35 个、漏 15 个）。**改了登记表里没有读者的非代码文件（新文件 / 漏登）⇒ 回退整套**（v0.45.661）。`--run` 会顺带测量被改测试现在读了什么，**测试读测试漏登 ⇒ 退出码 4**；修法**照提示打印的整条命令跑**（`--files <漏登的读者> --update`，v0.45.667），并把 `tests/_must_run.json` 一起提交——⚠️ 别自己敲裸 `--update`：它只量相对 `origin/main` 改过的测试，**量不到没改过的读者**（现有测试按目录读到新文件）——分支没改测试时什么都不量；改了别的测试时只量那几个、照样提示「已并入」、退出码 0，看起来修好了、漏登还在（v0.45.672）。CI 在整套上带同一个插件做漂移检查——**非代码文件漏登只出 `::warning`、不挡 main**（加了被现有测试读的新静态文件照样要补登，命令就在注记里）。**只是开发期快检**：覆盖的是「当数据读」的守卫，不覆盖 import 耦合的行为测试；收尾和 CI 仍整套。
- **跑整套前先看有没有别的整套 / clockshift 审计在跑**：`pgrep -fl pytest`、`pgrep -fl audit.py`；有就错开。整套是单进程纯 CPU，并发时同一套会慢到近两倍（机制与实测在 auto-memory `alpha-hive-test-suite-speed.md`）——**两轮并发时段不同的整套耗时不可比，别拿它当回归 / 提速证据**。
- 整套开跑时报告头会自动打一行 `concurrency:`（`tests/_concurrency_hint.py`）：选中条数 ≥ `BIG_RUN_ITEMS` 且同时还有别的大会话（含 clockshift 拨钟的子会话）在跑，或登记不可用而无法判断。**只提示，不阻塞、不报红**；⚠️ **不要改成排队锁**——会让一个会话无声卡住，与「失败要可见」冲突，也会和靠真睡 / 靠超时的测试互相干扰（理由全文在该模块 docstring）。守卫 `tests/test_concurrency_hint.py`。
  - **系统 load 也会提示（v0.45.668）**：1 分钟 load ≥ 核数 ⇒ 另打一行 `concurrency: ⚠️ 系统 1 分钟 load N / M 核`，指给你 `ps -Ao pid,ppid,etime,pcpu,comm -r | head` 去看谁在占 CPU（`ppid=1` 且 `etime` 很长的满载进程多半是泄漏的后台负载）。上面那条只数 pytest 会话，**看不见非 pytest 的负载**——10-27 20 个泄漏的 `yes` 把 load 顶到 124、整套被饿到约 8% CPU、跑了 20 分钟以上，当时一个字没提示。读不到 load 时如实说「无法读取」。同样只提示。
  - **要给测试加 CPU 负载（复现「机器忙时偶发失败」）一律用 `tools/burn_cpu.py`，别手写 `yes &`**：`/usr/local/bin/python3 tools/burn_cpu.py --procs 20 --seconds 600 -- <命令>`。负载进程**自带寿命**（到点退出、父进程没了也退出）、退出时逐个核对零残留，有残留退出码 4。手写 shell 在本机（zsh 不拆词、macOS `pgrep` 无 `-c`）上会无声泄漏，细节在 auto-memory `alpha-hive-environment-facts.md`。守卫 `tests/test_burn_cpu.py`。

## 硬检查项：`skip` 守卫要问「X 在哪些环境里存在？」（2026-09-07 起）

写 `if not X.exists(): pytest.skip()` 之前先回答一句：**X 在哪些环境里存在？**
若答案是「只有一台机器上的一个目录」，那这条测试**等于没有**——
**「加一个 skip 守卫」和「让这条测试在任何地方都跑不到」之间，只隔着一个未被 git 跟踪的文件。**

判据：依赖若由测试自己构造，守卫该是断言不是 skip，且要正面核对夹具真的接上了（不能只断言「没读生产库」）；同样的条件性写在 marker 上可见、写在 skip 里不可见，真需要外部系统的测试标 marker 别写 skip；数一数还有多少条挂在同一个 X 上，但别一刀切——生产数据本身退化的测试该标 marker 不是补桩。

与上一节同源：`skip` 把「这条没验」渲染成「这条没问题」，都答不出「谁会红？」。实测案例（`TestSPYBenchmarkUnavailable`/`pheromone.db`）+ WAL 边车取证陷阱见 auto-memory `alpha-hive-failure-propagation.md`。

v0.45.165 普查全仓（20 条，5 个文件）后补三条：

1. **模块级 `pytestmark = skipif(...)` 会连坐**整个文件，包括与该条件毫无关系的测试。
   实测最刺眼的一处：`TestGuardsHaveTeeth`（纯合成数据、零外部依赖，docstring 写着
   「没有这一组，本文件的全绿证明不了任何事」）跟着一起恒 skip ——
   **证明守卫有牙的那把尺子，自己从未被拿出来过。** 条件性要挂在**用到它的那个类**上。
2. **「数一数还有多少条挂在同一个 X 上」要按后果数，不能按 token 数。** 同样的恒 skip
   还能由**测试隔离泄漏进子进程**造出来：`_isolate_env` 把 `ALPHA_HIVE_HOME` 指向沙箱，
   `subprocess.run(..., cwd=_ROOT)` 继承了它 ⇒ CLI 永远读空库 ⇒ 输出恒为「无可用样本」
   ⇒ skip 恒中，**在每一台机器上，生产机也不例外**。那条测试里一个产物名 token 都没有。
3. **判据不是「不许 skip」，是「谁会红？」。** 反例：`git check-ignore` 返回 128 时
   skip 是正当的 —— 「git 仓库在哪些环境里存在」答「开发检出/worktree/CI 全都是」，
   条件为真的是全集减一个人造特例。⚠️ 顺带：`git check-ignore` 退出码有**三**个含义
   （0=忽略 / 1=未忽略 / **128=这里不是 git 仓库**），把 128 揉进另外两个就是
   「把一种失败误报成另一种」，正是本节要治的形状。

守卫落地在 `tests/test_no_invisible_prod_data_skips.py`（元守卫，命中即红）。

## 硬检查项：新产物的默认路径不许是相对路径（2026-09-07 起）

给任何会落盘的产物写默认位置前，先回答一句：
**这个默认值在「不是从仓库根跑」的时候会写到哪？**
答案若是「跟着 cwd 走」，它就是一颗定时炸弹——**测试的 cwd 不受控**。

**文件名唯一真相在 `hive_logger.PATHS`**（与 `PATHS.db` / `PATHS.chroma_db` 同族）。
新产物在那里加一个 property；函数默认值写 `None`，在函数体里解析——**不要**写成
模块级常量或类属性（那会在 pytest **收集期**——早于任何 fixture、任何 env 隔离——
被 import 时就地冻死，`_isolate_env` 类的隔离对它完全无效）。

判据：路径要集中，但**文件不能合并**（两个同族文件恰好不同名，才使某次事故实际损害为 0）；
只跑单文件验证不了这类冻结 bug（ordering-dependent），必须在没有该产物的干净目录跑全套看它
有没有凭空造出来；新增产物必须在 `tests/conftest.py` 配两道防线——① setup 正面核对默认位置
确实在沙箱内，② teardown 比对仓库根与 cwd 的真身指纹（②专门抓①会漏掉的那类 bug）。

与上一节同源：把路径冻成模块级常量，等于把「隔离失效」改写成了「隔离生效」，同样答不出「谁会红？」。
全套判据、事故取证（v0.45.149/150/160，含 `__file__` 派生一族）见 auto-memory `alpha-hive-test-writes-production.md`。

**裸字面量缺省是另一族（v0.45.463）**：`db_path="pheromone.db"` / `os.environ.get(X, "cache")` 不含 `PATHS` / `__file__`，上面两条物种守卫都看不见；生产 cwd 是代码克隆，它们在那里恒读空且不报错。守卫 `tests/test_price_history_default_paths.py::TestNoNewCwdRelativeDataDefaults`（存量只许减）。

## 硬检查项：夹具里的日期会不会和真实钟比？（2026-10-13 起）

会 ⇒ **被测路径读几只钟就钉几只**（`cboe_options._pdt_now`、各 `_snapshot_now`……；钉一只漏一只正是 v0.45.489 那颗）；
没有注入接缝 ⇒ 在测试体内按真实今天算相对日期。写完跑一次可执行审计，别靠 grep 日期字面量：
`/usr/local/bin/python3 tools/clockshift/audit.py --days 7 --days 45 -- tests/test_xxx.py`（CI 的 `clockshift.yml` 在 push 与每日定时跑全量）。
harness 的不变式「拨整个世界、不拨一只钟」与已知漏点见 `tools/clockshift/clockshift.py` docstring；历次炸弹与教训见 auto-memory `alpha-hive-time-bomb-audit.md`。

## 硬检查项：这个路径指向「代码」还是「数据」？（2026-09-08 起）

**数据根规则（v0.45.422）**：唯一实现 `hive_logger.resolve_data_root`——`ALPHA_HIVE_HOME` 非空就用它，否则
`$HOME/alpha-hive-data`（**不再**兜底到代码检出：阶段 5 后那里只剩冻结旧数据，交互 shell 手动跑会静默读旧数据）。
测试会话在 `tests/conftest.py::pytest_configure` **收集之前**设会话级 `ALPHA_HIVE_HOME` 沙箱，并记下调用时环境给
「生产在哪」的守卫用——别把这个沙箱挪进 session 夹具（太晚，收集期冻住的常量会指向生产）。
那个「生产在哪」也只从 `resolve_data_root` 来（v0.45.488：测试闸曾自己 expanduser，在 `~` / 相对值上与生产分岔；非绝对值改守缺省并在报告头说明），测试层只经 `tests/_root_data_guard.py::production_data_root` 一处推导（真实数据根闸与 paper_portfolio 真身夹具共用，v0.45.498）；把 `ALPHA_HIVE_HOME` 变成路径的只许 hive_logger，生产代码里每个写出这个键的地方双向登记在 `tests/test_data_root_default.py::ENV_KEY_SITES`（认常量本身、不认调用形状，v0.45.498）。

**测试会话内核写沙箱（v0.45.573）**：macOS 上 `pytest_configure`（收集前）给测试进程树套 `sandbox_init` 禁写真实数据根（子进程继承），
真实数据根闸改判「统一日志里的写拒绝」；Alpha Bot 盘中采样 / 每日扫描在沙箱外照写、**不再让全量报 ERROR**
（扫描那半 v0.45.577 才成立：逐条的 paper_portfolio 指纹夹具在沙箱覆盖真身时让位给内核）。
并发测试会话护同一个根时，拒绝按 `/tmp/alpha-hive-write-sandbox-<uid>/` 里登记的进程号分归属（v0.45.577；内核日志只有 `进程(pid)`）：
别的会话的不在本会话报红，**谁都没登记的照样红**并注明可能是谁的。
沙箱里 exec 不了 setuid 程序（`/bin/ps` 用 `pgrep` 代替）、`log show` 跑不了（查日志助手在套沙箱前起）。设计与陷阱见 `tests/_write_sandbox.py`，守卫 `tests/test_write_sandbox.py`。

**真实数据根闸的豁免只认断言、不认注释（v0.45.427）**：`PATHS` 落在闸豁免区（`logs/`、`db_backups/`、`_` 元目录）的属性必须恰好是
`tests/test_root_data_guard.py::PATHS_IN_EXEMPT_AREAS_OK` 里写明「谁负责隔离」的几个——新增属性落进 `_` 目录会红，让它受闸
（`DATA_ROOT_WATCHED_META`），别往白名单加。测试写夹具**别经被测的路径属性写**（目标自己按沙箱算、写前断言属性指向它）：
10-03 一个变异体经 `PATHS.data_backup_repo` 把夹具写进了生产备份仓库，被每日备份封进历史，四天没人红（auto-memory `alpha-hive-test-writes-production.md`）。

改任何 `Path(__file__).parent / …` 之前先回答一句：**它指向代码还是数据？**

| 指向 | 正确锚点 | 典型 |
|---|---|---|
| **数据**（库 / 账本 / 状态 / 缓存 / 产物） | `PATHS.*`，**调用时求值** | `pheromone.db`、`*_state/`、`cache/` |
| **代码**（随代码发布、或就是要「代码在哪」） | **`__file__` 才对** | `templates/`、`prompts/`、只读配置、`sys.path.insert`、`git -C <仓库>` |

⚠️ **`__file__` 不是错的写法，是用错地方的写法。一刀切会制造新 bug**——
把模板/prompt 改成 `PATHS.home` 后，测试把 `ALPHA_HIVE_HOME` 指向 tmp 就找不到文件。

**两个方向各有一条断言，清单只在那里维护，本文件不抄**
（抄一份就是快照，见开篇「文档分工原则」）：

- `tests/test_paths_not_frozen_at_import.py::TestFileDerivedSpeciesDoesNotSpread`
  - `KNOWN`（**子集**语义）——防**新增**冻结路径
  - `MUST_STAY_FILE_ANCHORED`（**超集**语义）——防把该留的**错误清掉**

**判据：写白名单时问一句「我怕它变大，还是也怕它变小？」** 怕两头就要两条断言。
v0.45.168 实测：只有子集守卫时，把 5 处「应保留」的错改成 `PATHS.home`
**4 处全绿**（唯一变红的那处是恰好被别的测试间接覆盖）。
同源盲区：子集语义也不防白名单**过期**——清干净了它同样不红，
要靠 `KNOWN - _scan()` 定期对账（v0.45.160 实测揪出 2 条）。

## 环境：iCloud 重名副本（原文，原在根目录；`tests/_repo_files.py` 处理的就是这类副本）

（阶段 8 起这只影响**开发检出**；生产代码在 `~/alpha-hive-prod`、数据在 `~/alpha-hive-data`，都不在 iCloud 同步里。）
本项目位于 `~/Desktop/Alpha Hive`，而 macOS「桌面与文稿同步 iCloud」会**持续**
产出形如 `xxx 2.py` / `settings.local 2.json` 的副本（名字里带**空格 + 数字**，
2026-09-07 一次扫出 53 个、最早可回溯 2026-03-16，不是偶发）。

**已知故障**：这些副本会让 `git fetch` 报 `fatal: bad object refs/heads/... 2`——
报错说「对象缺失」，**实际是 refname 非法**（git 引用名不允许含空格），与对象存不存在无关；
且坏的是 `fetch` 不是紧邻它的 `push`（`&&` 链短路的老问题，见「并发开工必须先占号」一节）。

**诊断**：`find .git/refs -type f -name "* *"`，有输出就是这个病，清掉带空格的 ref 即恢复。

⚠️ **清理前必读** auto-memory `alpha-hive-environment-facts.md`——副本可能含未提交内容，
不能无脑 `rm`（正确删除方式、mode 0600 误诊陷阱、SQLite sidecar 陷阱都在里面）。

**根治**：把 `~/Desktop/Alpha Hive` 移出 iCloud 同步——这是用户的机器设置，
**由用户决定，不要自行更改**；清理只是重置计时器。
