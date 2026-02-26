# 🔧 Alpha Hive Phase 2 使用指南

**版本**：5.0
**最后更新**：2026-02-24

---

## 快速开始

### 1. 基础蜂群扫描（自动持久化）
```python
from alpha_hive_daily_report import AlphaHiveDailyReporter

reporter = AlphaHiveDailyReporter()
result = reporter.run_swarm_scan(focus_tickers=["NVDA", "TSLA"])
# ✅ 自动保存到 DB：
#    - agent_memory 表（每个 Agent 的发现）
#    - reasoning_sessions 表（会话汇总）
#    - agent_weights 表（权重）
```

### 2. 查询历史记忆和上下文
```python
from memory_store import MemoryStore
from memory_retriever import MemoryRetriever

ms = MemoryStore()
mr = MemoryRetriever(ms)

# 获取 NVDA 最近 30 天的记忆
memories = ms.get_recent_memories("NVDA", days=30, limit=10)
print(f"✅ 找到 {len(memories)} 条历史记忆")

# 查找相似的历史信号
similar = mr.find_similar("bullish signal earnings", ticker="NVDA", top_k=5)
for item in similar:
    print(f"  - {item['discovery'][:50]}... (相似度 {item['similarity']:.2f})")

# 获取历史上下文摘要（自动注入 Agent）
context = mr.get_context_summary("NVDA", "2026-02-24")
print(f"✅ 历史上下文：{context}")
```

### 3. 查看和调整 Agent 权重
```python
from agent_weight_manager import AgentWeightManager

ms = MemoryStore()
awm = AgentWeightManager(ms)

# 查看当前权重
weights = awm.get_weights()
for agent_id, weight in weights.items():
    print(f"{agent_id}: {weight:.2f}x")

# 获取单个 Agent 权重
nvda_weight = awm.get_weight("ScoutBeeNova")
print(f"ScoutBeeNova weight: {nvda_weight}x")

# 打印权重摘要
awm.print_weight_summary()

# 根据准确率重新计算权重（通常 T+7 后运行）
new_weights = awm.recalculate_all_weights()
print(f"✅ 权重已更新")
```

### 4. 查看 Agent 准确率统计
```python
from memory_store import MemoryStore

ms = MemoryStore()

# 查看单个 Agent 的 T+7 准确率
accuracy = ms.get_agent_accuracy("ScoutBeeNova", period="t7")
print(f"""
ScoutBeeNova T+7 准确率：
  - 准确率：{accuracy['accuracy']:.2%}
  - 样本数：{accuracy['sample_count']}
  - 平均收益：{accuracy['avg_return']:.2%}
""")
```

---

## 核心工作流

### 工作流 A：日常蜂群扫描 → 自动持久化
```
1. 启动蜂群扫描
   reporter.run_swarm_scan(["NVDA", "TSLA"])

2. 6 个 Agent 并行分析
   ├─ ScoutBeeNova（拥挤度）
   ├─ OracleBeeEcho（期权）
   ├─ BuzzBeeWhisper（情绪）
   ├─ ChronosBeeHorizon（催化剂）
   ├─ RivalBeeVanguard（竞争）
   └─ GuardBeeSentinel（共振）

3. 异步持久化（后台线程）
   ├─ Agent 发现 → agent_memory
   ├─ 会话汇总 → reasoning_sessions
   └─ 权重更新 → agent_weights

4. 历史上下文自动注入
   每个 Agent 开头：ctx = retriever.get_context_summary(ticker, date)
```

### 工作流 B：T+7 准确率回看 → 权重调整
```
1. T+7 日期到达
   ├─ 获取 7 天前的预测：SELECT FROM agent_memory WHERE date = T-7
   ├─ 获取实际收益：ticker_price[T] - ticker_price[T-7]
   └─ 判断准确：actual_return 方向 == prediction direction

2. 更新准确结果
   ms.update_memory_outcome(memory_id, "correct", t1=None, t7=0.05, t30=None)

3. 重新计算权重
   awm.recalculate_all_weights()
   ├─ ScoutBeeNova accuracy=0.8 → weight=1.6x
   ├─ OracleBeeEcho accuracy=0.6 → weight=1.2x
   └─ ...

4. 新的蜂群扫描自动使用新权重
   ├─ QueenDistiller.distill() 使用 weighted_average
   └─ Agent 结果加权融合
```

### 工作流 C：发现历史模式 → 改进未来预测
```
1. 发现有趣信号
   similar = mr.find_similar("AI chip shortage", ticker="NVDA", top_k=5)

2. 分析历史背景
   for item in similar:
       - 相似的过去预测是什么？
       - 那次的结果如何？
       - 当时的权重是多少？

3. 调整策略
   - 如果历史相似预测准确率高 → 提升该 Agent 权重
   - 如果其他 Agent 也预测到 → 增加共振检测灵敏度
```

---

## 数据库查询参考

### 查看所有历史记忆
```sql
SELECT
    memory_id, ticker, agent_id, direction, self_score,
    discovery, actual_outcome, created_at
FROM agent_memory
ORDER BY created_at DESC
LIMIT 20;
```

### 查看某 Agent 的准确率
```sql
SELECT
    agent_id,
    COUNT(*) as total,
    SUM(CASE WHEN actual_outcome = 'correct' THEN 1 ELSE 0 END) as correct_count,
    ROUND(100.0 * SUM(CASE WHEN actual_outcome = 'correct' THEN 1 ELSE 0 END) / COUNT(*), 2) as accuracy_pct,
    ROUND(AVG(outcome_return_t7), 4) as avg_return_t7
FROM agent_memory
WHERE actual_outcome IS NOT NULL
GROUP BY agent_id;
```

### 查看所有会话记录
```sql
SELECT
    session_id, date, run_mode,
    json_extract(tickers, '$[0]') as first_ticker,
    json_extract(tickers, '$.') as ticker_count,
    top_opportunity_ticker, top_opportunity_score, total_duration_seconds
FROM reasoning_sessions
ORDER BY created_at DESC
LIMIT 10;
```

### 查看权重演变
```sql
SELECT
    agent_id, base_weight, adjusted_weight, accuracy_t7, sample_count, last_updated
FROM agent_weights
ORDER BY adjusted_weight DESC;
```

### 查看特定标的的所有 Agent 信号
```sql
SELECT
    agent_id, direction, self_score, discovery, source, created_at
FROM agent_memory
WHERE ticker = 'NVDA'
ORDER BY created_at DESC
LIMIT 30;
```

---

## 高级用法

### 自定义检索查询
```python
from memory_retriever import MemoryRetriever
from memory_store import MemoryStore

ms = MemoryStore()
mr = MemoryRetriever(ms, cache_ttl_seconds=600)

# 查找高置信度的历史看多信号
bullish_signals = []
for day_offset in range(7):
    query = f"bullish signal day_{day_offset}"
    results = mr.find_similar(query, ticker="NVDA", top_k=3, min_similarity=0.3)
    bullish_signals.extend(results)

# 按相似度排序并去重
unique = {r['memory_id']: r for r in bullish_signals}
sorted_signals = sorted(unique.values(), key=lambda x: x['similarity'], reverse=True)

for signal in sorted_signals[:5]:
    print(f"相似度 {signal['similarity']:.2f}: {signal['discovery']}")
```

### 加权投票的自定义逻辑
```python
from agent_weight_manager import AgentWeightManager
from memory_store import MemoryStore

ms = MemoryStore()
awm = AgentWeightManager(ms)

# 获取权重
weights = awm.get_weights()

# 自定义融合逻辑（而不仅仅是 weighted_average）
agent_results = [
    {"source": "ScoutBeeNova", "score": 7.5, "direction": "bullish"},
    {"source": "OracleBeeEcho", "score": 6.0, "direction": "neutral"},
    {"source": "BuzzBeeWhisper", "score": 8.0, "direction": "bullish"},
]

# 按方向分组加权
bullish_scores = [
    r['score'] * weights.get(r['source'], 1.0)
    for r in agent_results if r['direction'] == 'bullish'
]
bullish_weight = sum(bullish_scores) / sum(weights.get(r['source'], 1.0) for r in agent_results if r['direction'] == 'bullish')

print(f"加权看多强度：{bullish_weight:.2f}/10")
```

### 定期权重回顾（周期性任务）
```python
import schedule
from datetime import datetime
from memory_store import MemoryStore
from agent_weight_manager import AgentWeightManager

def weekly_weight_review():
    """每周一 00:00 UTC 运行"""
    ms = MemoryStore()
    awm = AgentWeightManager(ms)

    print(f"⏱️  开始周期权重回顾 ({datetime.now().isoformat()})")

    # 重新计算权重
    new_weights = awm.recalculate_all_weights()

    # 打印摘要
    awm.print_weight_summary()

    print(f"✅ 权重回顾完成")

# 配置调度
schedule.every().monday.at("00:00").do(weekly_weight_review)

# 在后台运行
# while True:
#     schedule.run_pending()
#     time.sleep(60)
```

---

## 故障排除

### Q: 内存存储初始化失败，如何恢复？
A: 系统会自动降级：
```
⚠️ MemoryStore schema_migrate 失败，但继续运行（memory_store=None）
```
主蜂群功能 100% 可用，只是无持久化。重启后会重试初始化。

### Q: 检索速度慢（> 50ms）？
A: 检查缓存状态：
```python
mr.invalidate_cache()  # 清除缓存
mr.invalidate_cache(ticker="NVDA")  # 清除特定 ticker 缓存
```
通常首次查询会慢一点（TF-IDF 构建），后续缓存命中应 < 5ms。

### Q: 权重没有变化，为什么？
A: 检查样本数量：
```python
accuracy = ms.get_agent_accuracy("ScoutBeeNova", period="t7")
if accuracy['sample_count'] < 10:
    print("⚠️ 样本数不足 10，权重保持 1.0x")
```

### Q: 如何手动更新准确率？
A: 使用 `update_memory_outcome()`：
```python
ms.update_memory_outcome(
    memory_id="2026-02-24_NVDA_ScoutBeeNova_123456",
    outcome="correct",
    t1=0.02,      # T+1 收益率
    t7=0.05,      # T+7 收益率
    t30=0.15      # T+30 收益率
)
```

---

## 性能优化建议

### 1. 缓存策略
```python
# 使用更长的 TTL 以减少 TF-IDF 重建
mr = MemoryRetriever(ms, cache_ttl_seconds=3600)  # 1 小时

# 预热缓存（在低谷时段）
for ticker in WATCHLIST:
    mr.find_similar("warmup", ticker=ticker, top_k=1)
```

### 2. 批量操作
```python
# ❌ 不好：逐条查询
for ticker in ["NVDA", "TSLA", "AMD"]:
    memories = ms.get_recent_memories(ticker)

# ✅ 好：批量查询并缓存
all_memories = ms.get_recent_memories("NVDA", days=30, limit=100)
# 单次大查询通常比多次小查询快
```

### 3. 异步权重更新
```python
from threading import Thread

def update_weights_async():
    awm = AgentWeightManager(ms)
    awm.recalculate_all_weights()

# 在后台线程运行，不阻塞主扫描
Thread(target=update_weights_async, daemon=True).start()
```

---

## 配置参数

在 `config.py` 中调整 `MEMORY_CONFIG`：
```python
MEMORY_CONFIG = {
    "enabled": True,  # 禁用持久化：False

    "agent_memory": {
        "retention_days": 90,  # 增加/减少历史窗口
        "max_similar_results": 5,  # 检索返回数量
    },

    "retriever": {
        "cache_ttl_seconds": 300,  # 缓存过期时间
        "min_similarity": 0.1,  # 相似度阈值
        "top_k": 5,  # 默认返回数量
    },

    "weight_manager": {
        "min_weight": 0.3,  # 权重下限
        "max_weight": 3.0,  # 权重上限
        "min_samples_for_dynamic": 10,  # 动态权重最小样本
        "accuracy_weight": 2.0,  # 准确率对权重影响
    },
}
```

---

## 下一步计划

- 【Phase 3】Polymarket 实时赔率集成
- 【Phase 3】SEC Form 4/13F 自动追踪
- 【Phase 4】向量数据库（ChromaDB）集成
- 【Phase 4】多市场支持（HK、CN、Crypto）

---

**版本**：5.0
**最后更新**：2026-02-24
**状态**：✅ 就绪使用
