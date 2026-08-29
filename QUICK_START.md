# 🚀 Alpha Hive 快速开始指南

## 5 分钟快速开始

### 1️⃣ 验证 Slack 连接
```bash
python3 /Users/igg/.claude/reports/slack_report_notifier.py
```
✅ 如看到"Slack 消息发送成功"，表示 Slack 已连接

### 2️⃣ 运行 NVDA 演示
```bash
python3 /Users/igg/.claude/reports/test_nvda_full_workflow.py
```
✅ Slack 频道会收到 5 条消息

### 3️⃣ 集成到日常工作
```python
from alpha_hive_daily_report import AlphaHiveDailyReporter

reporter = AlphaHiveDailyReporter()
report = reporter.run_swarm_scan(['NVDA', 'TSLA'])
# 报告自动推送到 Slack ✅
```

---

## 常用命令速查

### 蜂群扫描（Phase 2）
```python
reporter = AlphaHiveDailyReporter()
report = reporter.run_swarm_scan(['NVDA', 'TSLA', 'MSFT'])
```
- 6 个 Agent 并行分析
- 实时推送到 Slack
- 返回结构化报告

### 手动 Slack 通知
```python
from slack_report_notifier import SlackReportNotifier

notifier = SlackReportNotifier()

# 机会告警
notifier.send_opportunity_alert(
    ticker="NVDA",
    score=8.5,
    direction="看多",
    discovery="AI 需求强劲",
    risks=["竞争加剧"]
)

# 风险告警
notifier.send_risk_alert(
    alert_title="市场告警",
    alert_message="VIX 突破 25",
    severity="HIGH"  # CRITICAL/HIGH/MEDIUM/LOW
)
```

---

## 📊 文件位置

| 文件 | 用途 |
|------|------|
| `alpha_hive_daily_report.py` | 日报生成器 |
| `slack_report_notifier.py` | Slack 通知器 |
| `code_executor.py` | 代码执行引擎（AST 安全） |
| `config.py` | 全局配置 |
| `test_nvda_full_workflow.py` | NVDA 演示 |

---

## 🔧 常见问题

### Q: Slack 消息为什么没有收到？
A: 检查：
```bash
cat ~/.alpha_hive_slack_webhook  # 确认 URL 有效
python3 slack_report_notifier.py  # 运行测试
```

### Q: 如何修改通知内容？
A: 编辑 `slack_report_notifier.py` 中的 Block 模板

### Q: 支持哪些 Slack 功能？
A:
- ✅ 富文本格式（Markdown）
- ✅ 分段显示（Sections）
- ✅ 字段并列（Fields）
- ✅ 颜色标注
- ✅ Emoji 表情
- ✅ 分割线
- ✅ 上下文信息

### Q: 能否禁用 Slack 通知？
A: 移除 `~/.alpha_hive_slack_webhook` 文件，或编辑代码中的 `enabled` 字段

---

## 📈 每日使用流程

```
07:00 - Cron 定时触发
  ↓
08:00 - 蜂群启动，6 个 Agent 并行工作
  ↓
08:15 - 报告生成，自动推送 Slack
  ↓
08:20 - 查看 Slack 频道，获取投资机会
  ↓
09:00 - (可选) 复制 X 线程内容到 Twitter 发布
```

---

## 🎯 典型场景

### 场景 1: 每天早上 8 点自动报告
```bash
# 添加到 crontab
0 8 * * * python3 /Users/igg/.claude/reports/alpha_hive_daily_report.py run_swarm_scan
```

### 场景 2: 监控特定标的
```python
from alpha_hive_daily_report import AlphaHiveDailyReporter

reporter = AlphaHiveDailyReporter()
report = reporter.run_swarm_scan(['NVDA'])  # 只关注 NVDA

# 自动推送高分机会到 Slack
for opp in report['opportunities']:
    if opp.opportunity_score >= 8.0:
        print(f"🚨 高分机会：{opp.ticker}")
```

### 场景 3: 生成 X 线程
```python
from slack_report_notifier import SlackReportNotifier

notifier = SlackReportNotifier()
x_thread = """【Alpha Hive 日报】
1️⃣ NVDA 看多 8.5/10
2️⃣ 财报指引乐观
3️⃣ 机构持仓增加"""

notifier.send_x_thread(x_thread, "2026-02-24")
# 从 Slack 复制到 X 发布
```

---

## 💡 最佳实践

✅ **推荐**
- 每天固定时间运行扫描
- 定期检查 Slack 频道
- 关注高分（>7.5）机会
- 使用多个标的对比分析

❌ **避免**
- 频繁运行导致告警泛滥
- 忽视风险提示
- 依赖单一信息源
- 过度交易

---

## 🔐 安全提示

```bash
# 保护 Slack Webhook URL
chmod 600 ~/.alpha_hive_slack_webhook

# 定期检查审计日志
tail -20 /tmp/alpha_hive_sandbox/2026-02-24/audit.log

# 不在代码中硬编码 Webhook
# ✅ 正确: 从文件读取
# ❌ 错误: webhook_url = "https://..."
```

---

## 📞 获取帮助

| 问题类型 | 查看文件 |
|---------|--------|
| 详细使用 | `SLACK_INTEGRATION_GUIDE.md` |
| 技术细节 | `PHASE3_IMPLEMENTATION_COMPLETE.md` |
| 完整摘要 | `PHASE3_COMPLETION_SUMMARY.txt` |
| 源代码 | `slack_report_notifier.py` |

---

## 🚀 一行启动

```bash
# 快速测试
python3 -c "from slack_report_notifier import SlackReportNotifier; SlackReportNotifier().test_connection()"

# 快速演示
python3 test_nvda_full_workflow.py

# 快速扫描
python3 -c "from alpha_hive_daily_report import AlphaHiveDailyReporter; AlphaHiveDailyReporter().run_swarm_scan(['NVDA'])"
```

---

**最后更新**: 2026-02-24 21:00 UTC
**维护者**: Alpha Hive 🐝
**版本**: 3.0 + Slack P6
