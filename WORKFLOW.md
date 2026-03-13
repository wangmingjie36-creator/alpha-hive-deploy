# Alpha Hive 报告生成流程

## 架构说明

**本地 JSON 数据 + Claude Cowork 推理**，无需独立 API Key。

```
本地 JSON 数据（analysis-{TICKER}-ml-{DATE}.json）
        ↓
generate_deep_v2.py --no-llm     ← 生成 HTML 框架 + 数据图表
        ↓
在 Claude Cowork 里手动推理写叙事   ← 用 Claude Max 订阅
        ↓
inject_v2.py                      ← 把叙事注入 HTML
        ↓
最终报告（deep-{TICKER}-{DATE}.html）
```

---

## 操作步骤

### 第一步：生成 HTML 框架

```bash
cd "Alpha Hive"
python generate_deep_v2.py --ticker NVDA --no-llm
```

输出文件：`deep-NVDA-{DATE}.html`

### 第二步：Claude Cowork 推理

在本对话中，把需要分析的数据/章节交给 Claude 写叙事内容。

### 第三步：注入叙事

```bash
python inject_v2.py --ticker NVDA --date 2026-03-12
```

---

## 文件说明

| 文件 | 版本 | 说明 |
|------|------|------|
| `generate_deep_v2.py` | v3.5 | HTML 模板生成器，含矛盾检测/链式推理 |
| `inject_v2.py` | v1.1 | 叙事注入器，含质量门控 |
| `analysis-{TICKER}-ml-{DATE}.json` | — | 每日蜂群数据源 |

## 版本更新历史（generate_deep_v2.py）

| 版本 | 日期 | 内容 |
|------|------|------|
| v3.0 | 2026-03-12 | Template C 基础版，7章结构 + LLM推理框架 |
| v3.1 | 2026-03-13 | ① Delta分析 ② 链式推理 ③ 昨日涨跌箭头 ④ T-7预测闭环 ⑤ agents 4-tuple修复 |
| v3.2 | 2026-03-13 | SYSTEM_PROMPT升级：期权阈值体系/信号优先级/冲突处理规则 |
| v3.3 | 2026-03-13 | 真正两步链式推理：Step1分析框架 → Step2叙事写作 |
| v3.4 | 2026-03-13 | 跨章上下文：extract_thesis() + master_thesis注入CH6/CH7 |
| v3.5 | 2026-03-13 | 矛盾检测：detect_conflicts() 5类矛盾，强制注入swarm/options prompt |
