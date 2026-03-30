# Alpha Hive v0.12 升级路线图

> 基于当前 v0.11.0 代码审计 + 2026年最新金融工具调研
> 聚焦：**期权分析深度** + **回测自学习** | 生成日期：2026-03-27

---

## 一、必须立即修复的问题（P0）

### 1. feedback_loop.py Sharpe 计算 Bug（第205–221行）

**问题**：当前 Sharpe 计算给每笔预测分配相同的平均收益率，而非使用实际 T+7 收益，导致回测指标虚高，给你错误的信心。

```python
# 当前（错误）：
returns = [r if acc else -r for acc, r in zip(accuracies, [avg_return] * len(accuracies))]
# 应改为：使用每笔快照的实际 T+7 收益率
```

**影响**：如果不修，weekly_optimizer 基于失真数据做权重调整，等于自学习系统建立在沙子上。

### 2. 条件胜率曲线未接入自学习

backtest_engine.py 已经有 `win_rate_by_score_band()` 功能，但 weekly_optimizer 没有调用它。这意味着你不知道"评分 7.5 的报告是否比评分 6.0 的真的更准"。这是验证整个评分体系是否有效的核心指标。

---

## 二、分析深度升级方案（期权为主）

### A. 缺失的 Greeks 计算 — 高 ROI

| Greek | 当前状态 | 升级内容 | 对期权交易的价值 |
|-------|---------|---------|----------------|
| **Delta** | ❌ 完全缺失 | 每个行权价的 BS Delta | 基础中的基础：仓位方向性风险、对冲比率 |
| **Vanna** | ❌ 缺失 | dDelta/dVol 二阶导 | 当 IV 突变5%+时预测哪些行权价 Gamma 会爆 |
| **Charm** | ⚠️ 仅到期日级别 | 每个行权价的 Charm | 日内 Theta 衰减路径 → 精确进出场时机 |
| **Volga** | ❌ 缺失 | dVega/dVol | 识别 Vol-of-Vol 崩塌时哪些行权价最受伤 |

**建议新增模块**：`greeks_engine.py`，Black-Scholes 全链 Greeks 计算，输出注入 CH4 期权叙事。

### B. Dealer GEX 增强 — 中等 ROI

当前 advanced_analyzer.py 的 DealerGEXAnalyzer 存在三个简化问题：

1. **GEX 无归一化**：$250M 对 PLUG 是巨量，对 NVDA 是噪音。需要 `GEX_pct = total_gex / (price × avg_oi) × 100`
2. **GEX Flip 无加速度**：只标记 flip 价位，不知道是快速穿越还是慢速靠近。加 `dGEX/dPrice` 斜率
3. **无 Vanna 压力测试**：Vol 飙升时 Vanna 可以翻转 GEX 符号。加 `GEX_stress = GEX + vanna × vol_move`

### C. SABR 波动率曲面 — 长期升级

当前 IV Term Structure 是逐到期日取 ATM IV 的折线图。升级到 SABR 模型后可以：
- 拟合完整的波动率微笑曲面（Smile）
- 检测 Skew 异常（如 25-delta risk reversal 突变）
- 更精确的 OTM 期权定价

**推荐库**：QuantLib Python 1.40+（生产级）或 pySABR（轻量替代）

---

## 三、可接入的新金融工具和数据源

### 🔥 高优先级（1-2周可完成）

| 工具 | 功能 | 费用 | 接入难度 | 对蜂群的价值 |
|------|------|------|---------|-------------|
| **Unusual Whales API** | 实时期权流、暗池数据、国会议员交易 | 免费层可用；历史数据 $250/月 | 中等（有官方 Python 包 + MCP Server） | Scout 蜂发现层直接注入异常流信号 |
| **Optopsy** | 专门的期权策略回测引擎 | 免费开源 | 中等（新模块） | feedback_loop 验证 Scout/Oracle/Bear 推荐 |
| **CBOE 每日统计** | Put/Call Ratio、SKEW、VVIX 历史数据 | 免费（网页抓取） | 简单（BeautifulSoup） | CH2 宏观卡片增加尾部风险指标 |
| **Tradier API** | 期权报价、Greeks、IV 数据 | 免费层可用 | 简单（REST） | 替代/验证 yfinance 的 IV 数据 |

### ⚡ 中优先级（3-4周）

| 工具 | 功能 | 费用 | 对蜂群的价值 |
|------|------|------|-------------|
| **Quiver Quant** | 国会议员交易追踪、政策信号 | $10/月起（有 Python SDK） | 新维度：立法Alpha → 大科技股仓位 |
| **Alpaca WebSocket** | 实时期权流数据（WebSocket 推送） | 免费（需券商账户） | 替代 yfinance 轮询，realtime_metrics 不再空 |
| **VectorBT** | 向量化回测引擎 + Numba 加速 | 免费（社区版） | weekly_optimizer 回测加速 10-50x |
| **Glassnode** | 比特币链上数据（巨鲸钱包、实体行为） | 免费仪表盘；Pro $20/月 | MSTR/COIN 仓位相关性信号 |

### 🧪 探索性（1-2个月）

| 工具 | 功能 | 费用 | 对蜂群的价值 |
|------|------|------|-------------|
| **FinRL** | 金融强化学习框架 | 免费开源 | 下一代 RivalBee 权重优化（RL替代OLS） |
| **Microsoft Qlib** | AI量化平台（完整 ML 流水线） | 免费开源 | 端到端 ML 管线替代手动信号管道 |
| **QuantLib SABR** | 生产级波动率曲面拟合 | 免费开源 | advanced_analyzer.py Smile 建模升级 |

---

## 四、回测与自学习框架增强

### A. 关键缺失指标

| 指标 | 当前 | 建议添加 | 价值 |
|------|------|---------|------|
| **Profit Factor** | ❌ | 赢钱总和 / 亏钱总和，目标 >2.0 | 核心：系统是否"赢大亏小" |
| **Information Ratio** | ❌ | (年化收益 - 基准) / 跟踪误差 | 风险调整后的超额表现 |
| **条件胜率** | ⚠️ 仅在 backtest_engine | 按评分区间 + 按 IV 环境分层 | 验证评分系统有效性 |
| **最大连续亏损** | ❌ | 追踪连续错误序列 | "系统能撑过连续3次看错吗？" |
| **Regime 条件准确率** | ❌ | 高IV vs 低IV / 牛市 vs 熊市 | 发现系统是否严重依赖特定市场环境 |

### B. weekly_optimizer 算法升级

当前算法是简单的准确率归一化（proportional normalization）。建议：

1. **加权最小二乘法（WLS）**：用 OLS 回归 `agent_vote ~ composite_score`，提取 beta 作为隐含重要性
2. **时间衰减权重**：近期快照权重 > 远期，`weight_i = exp(-(today - date)/30)`
3. **共线性检测**：如果两个 Agent 始终同向移动（r > 0.8），不要同时提升两者权重
4. **Bootstrap 验证**：重采样历史准确率 N 次，验证权重变动的稳健性

### C. 新增模块建议：`options_backtester.py`

用 Optopsy 框架构建期权策略回测：
- 输入：Scout/Oracle/Bear 历史推荐 + 3年期权链数据
- 回测：单腿买权、垂直价差、Iron Condor 等策略
- 输出：每种策略的 Sharpe、最大回撤、胜率
- 反馈：哪种策略在哪种 IV 环境下表现最好 → 注入 CH6 情景推演

---

## 五、实施路线图

```
第1周 ─── P0 紧急修复 ───────────────────────────
  ├─ 修复 feedback_loop.py Sharpe Bug
  ├─ 接入 win_rate_by_score_band → weekly_optimizer
  └─ 新增 Profit Factor + Information Ratio 指标

第2周 ─── Greeks 引擎 + CBOE 数据 ──────────────
  ├─ 创建 greeks_engine.py（Delta + Vanna + Charm + Volga）
  ├─ CBOE 每日 Put/Call + SKEW 抓取器 → CH2 宏观卡片
  └─ GEX 归一化 + Flip 加速度

第3-4周 ─── 外部数据接入 + 回测升级 ──────────────
  ├─ Unusual Whales API 接入 → Scout 蜂发现层
  ├─ Tradier API 接入 → IV 数据交叉验证
  ├─ Optopsy 期权回测模块
  └─ weekly_optimizer WLS + 时间衰减升级

第5-6周 ─── 进阶功能 ──────────────────────────
  ├─ Quiver Quant 国会交易信号
  ├─ VectorBT 集成加速回测
  ├─ Regime 条件准确率分析
  └─ Bootstrap 权重验证

第7-8周 ─── 波动率曲面 + ML ──────────────────
  ├─ QuantLib/pySABR 波动率微笑拟合
  ├─ Vanna 压力测试注入 GEX
  └─ FinRL 探索性实验（self_analyst 月报增强）
```

---

## 六、预期收益评估

| 升级项 | 投入时间 | 预期效果 |
|--------|---------|---------|
| Sharpe Bug 修复 | 0.5天 | 消除虚假回测信心，自学习系统基础修正 |
| Delta/Vanna Greeks | 2天 | 期权进场精度提升，对冲比率可视化 |
| CBOE SKEW + VVIX | 1天 | 尾部风险预警，CH5 宏观章节信息密度翻倍 |
| Unusual Whales 接入 | 3天 | 暗池 + 异常流信号，Scout 蜂发现能力质的飞跃 |
| Optopsy 回测 | 3天 | 从"方向预测"升级到"策略验证"，闭环反馈 |
| WLS 权重优化 | 1天 | 更科学的权重自适应，减少过拟合风险 |
| Regime 条件分析 | 1天 | 发现系统盲区（可能低IV环境准确率仅45%） |

---

*此路线图基于 v0.11.0 代码审计和 2026年3月最新工具调研生成。建议按周执行，每周末更新 CHANGELOG.md。*
