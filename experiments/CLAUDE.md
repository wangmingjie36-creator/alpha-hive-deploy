# 实验与预注册（`experiments/`）· 分区工作记忆

> 读 / 改 `experiments/` 下任何文件时 Claude Code 自动加载本文件（v0.45.727 新建）。本目录放预注册协议（`*prereg*.md`、`dim_ic_preregistration.md`）与回放 / 前瞻检验 / 一次性修复脚本。
> 下面的通则**摘自**根目录 `CLAUDE.md` 原文与各协议现行写法，不是新规则；**协议文件本身永远优先**（写了「本文与代码不一致时以代码为准」的协议，从其约定）。

## 通则（各预注册的共同写法）

- **先登记、后看数据**：多数协议 §0 写明「起草人看过什么（盲化披露）」。
- **盲期不偷看**：冻结前从任何出口把某笔的影子量和它其后的盈亏 / 波动拼在一起看 = 偷看 = 协议变更，「随手看看几个例子」「T 那一笔」也算（影子宽跨协议 §8 原文）；进度只看计数。
- **冻结 / 首次检验只走登记的入口、只跑一次**：跨式 GEX 与影子宽跨走 `straddle_gex_prereg.py --run-once` / `straddle_strangle_shadow.py --run-once`；卖权路由检验首次就绪即冻结，唯一写者是日报钩子（`sell_strike_report.write_local_report(date, freeze=True)`）。
- **改度量 / 单位定义 / 排除规则 = 协议变更**：在协议里记修订、`PREREG["version"]` 加一（影子宽跨协议 §8、§14，现到修订 5）。
- **故障日 ≠ 协议排除日**：`day_health` 判出的故障日只进敏感性分析，排不排是协议修订（细节在 `.claude/rules/data-intake.md`）。
- **测量账本不是检验**：接货型卖 put 账本是「规则登记」，样本小到证明不了优势（`sell_put_wheel_prereg.md`）。

## 协议登记表（新增预注册时在这里加一行；路径一律相对仓库根）

| 协议 | 对应代码 | 细节在哪（按路径加载的规则 / auto-memory） |
|---|---|---|
| `sell_strike_routing_prereg.md`（v0.45.333） | `sell_strike_*.py` | `.claude/rules/options-ledgers.md`；`alpha-hive-sell-strike-selector.md` |
| `straddle_gex_prereg.md`（v0.45.424） | `straddle_gex_prereg.py`、`options_paper_leg.py` | `.claude/rules/options-ledgers.md`（「跨式」页条）；`alpha-hive-straddle-gex-prereg.md` |
| `straddle_strangle_shadow_prereg.md`（v1 v0.45.586，修订 2–5） | `straddle_strangle_shadow.py` | `.claude/rules/options-ledgers.md`；`alpha-hive-strangle-compare.md` |
| `sell_put_wheel_prereg.md`（v0.45.720，规则登记） | `sell_put_wheel.py` | `.claude/rules/options-ledgers.md`；`alpha-hive-sell-put-research.md` |
| `llm_shadow_prereg.md`（v0.45.555） | `llm_shadow_scan.py`、`llm_shadow_report.py` | `.claude/rules/llm-shadow.md`；`alpha-hive-llm-ab-pilot.md` |
| `dim_ic_preregistration.md`（维度 IC，P1） | `experiments/dim_ic_protocol.py`、`experiments/dim_ic_forward_test.py`、根目录 `buzz_v1*.py` | `.claude/rules/data-intake.md`；`alpha-hive-locked-tasks.md`、`alpha-hive-buzz-frozen-scorer.md` |
| `price_channel_backtest_prereg.md`（v0.45.697） | `experiments/price_channel_backtest.py` | `alpha-hive-tradeable-signal.md` |
| `strategy_edge_prereg.md`（v0.45.376） | `experiments/strategy_edge_test.py` | 协议本身 |
