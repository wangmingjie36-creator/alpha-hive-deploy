# @AlphaHiveDailyBot · Telegram 命令菜单（BotFather /setcommands）

> ⚠️ **关键坑：`/setcommands` 是「整表覆盖」，不是「追加」。**
> 每次加新命令，必须把**下面整段完整列表**重贴一次——只贴新命令会让现有命令从菜单消失。

## 规则
- BotFather `/setcommands` 设的是**全局菜单**（所有用户看到同一份）。
- **管理员命令不放进菜单**：`/invite` `/revoke` `/list` `/push_now` `/grant`
  （advertise 给所有人无意义，非管理员点了也无效；你自己手敲即可）。
- `/trend` `/movers` 虽是 Pro 功能仍放——免费用户点了收到升级提示，是转化入口。
- 命令名小写、无斜杠，描述 ≤256 字。
- 想做到「管理员命令仅对你可见」需代码里 `setMyCommands` + `BotCommandScopeChat`（invite-only 小用户量不值当，未做）。

## 操作
`@BotFather` → 发 `/setcommands` → 选 `@AlphaHiveDailyBot` → 把下面代码块**整段**粘贴发送。

## 粘贴内容（用户 / 查询 / 付费命令，已排除 5 个管理员命令）
```
start - 开始/激活订阅
scan - 单标的分析，如 /scan NVDA
top - 当日机会榜
swarm - 七蜂分歧（Pro）
trend - 综合分历史走势（Pro）
movers - 分数变动榜（Pro）
scorecard - 系统历史战绩
fg - 市场恐惧贪婪指数
watch - 添加关注
unwatch - 移除关注
mywatch - 我的关注列表
alert - 订阅阈值告警
alerts - 我的告警规则
unalert - 删除告警
upgrade - 升级 Pro 会员
mytier - 我的会员等级
status - 我的订阅状态
unsubscribe - 取消订阅
help - 命令帮助
```

> 加新用户命令时：在上面列表插入一行 `命令名 - 描述`，然后**整段**重贴给 BotFather。
