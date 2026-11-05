---
paths:
  - "alert_manager.py"
  - "tests/test_slack_send_whitelist.py"
  - "tests/test_step7_push_report_retired.py"
  - "tests/test_orchestrator_never_enables_llm.py"
  - "tests/fixtures/slack_report_notifier.py.frozen"
---

# Slack 推送规则原文 · 按路径加载

> 读 / 改上面 `paths:` 匹配的文件时 Claude Code 自动加载。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来「硬性规则 → Slack 推送规则」全文；根目录保留同名标题与全部「别做什么」的精简版，「Slack 通知精简规则」全文仍在根目录。

### Slack 推送规则
- **频道推送只走 Claude Code 内置 Slack MCP 工具**（`slack_send_message`）→ **#alpha-hive 频道**（Channel ID: `C0AGUUWJXJS`）；现在只有月度自诊断定时任务在用。
  ⚠️ **每日富文本日报目前没有任何自动推送**（2026-10-21 核：频道里只有 09-03 / 10-03 两条月度自诊断，`alpha-hive-daily-scan` 定时任务未注册）；Bot 侧的 `push_report_to_slack.py` 已于 v0.45.571 随编排器 Step 7 删除（不传 `--force` 恒 exit 2，从没推过）。**用户 2026-10-21 定：现在不需要每日日报推送**——别再提议恢复，用户主动要再做。
- **扫描确认通知**（"是否启用 LLM？"）：**已删除**（v0.45.510 停用、v0.45.514 删 `pre_scan_notify.py`）。编排器永远 `--no-llm`（`tests/test_orchestrator_never_enables_llm.py` 钉住），墓碑 `tests/test_slack_send_whitelist.py::TestPreScanNotifyStaysRetired`；要跑 LLM 扫描只能由人在终端显式 `--use-llm`，且仍须先问费用。
- 分工：Slack MCP 负责频道推送；**仓库里没有任何 Slack 发送代码**（`slack_report_notifier.py` 连同 `resilience.slack_breaker`、`config.SLACK_CHANNEL_ID` 于 v0.45.575 删除，冻结副本只留作测试的正对照 `tests/fixtures/slack_report_notifier.py.frozen`）。
- Bot Token 文件 `~/.alpha_hive_slack_bot_token` 仍在本机，但仓库里已无代码读它（`config._SECRET_REGISTRY` 与 `scan_secrets` 只是凭证登记 / 泄密扫描清单）。
