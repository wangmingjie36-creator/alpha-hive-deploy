# Alpha Hive Bot

对外服务的 Telegram 订阅机器人 (v0.1.0 MVP)。
**invite-only 白名单 + 每日盘后简报推送**，免费阶段无支付组件。

---

## 架构

```
用户 ─/start─▶ Bot ─白名单判断─▶ DB (SQLite)
                                    │
admin /invite <id> ─白名单→ active ─┘
                                    ▼
              每日 PDT 13:30 ──fetch──▶ gh-pages /alpha-hive-daily-{date}.md
                                    │
                              格式化 + 免责声明
                                    │
                            遍历 active 订阅者推送
```

简报来源是 **gh-pages 上的 Markdown 文件**，bot 服务零额外存储依赖。

---

## 快速开始（本地测试）

```bash
cd alpha_hive_bot

# 1. 创建 Telegram Bot
#    → 向 @BotFather 发 /newbot，拿到 BOT_TOKEN

# 2. 查管理员 user_id
#    → 向 @userinfobot 发任意消息，记下你的数字 user_id

# 3. 配置环境变量
cp .env.example .env
nano .env   # 填 BOT_TOKEN 和 ADMIN_USER_IDS

# 4. 安装依赖（虚拟环境推荐）
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. 启动
export $(grep -v '^#' .env | xargs)
python -m alpha_hive_bot.bot
```

然后在 Telegram 找你的 bot 名字发 `/start` 测试。
首次回复会说"未被邀请，user_id=xxx"。
管理员账号发 `/invite <你的user_id>` 加白名单，再发 `/start` 即可激活。

---

## 命令

### 用户命令
- `/start` — 开始 / 激活
- `/status` — 查看订阅状态
- `/unsubscribe` — 取消订阅
- `/help` — 命令列表

### 管理员命令
- `/invite <user_id>` — 邀请用户加白名单
- `/revoke <user_id>` — 撤销访问
- `/list` — 查看订阅者列表（最多 50）
- `/push_now` — 立即推送当日简报（不等定时器）

---

## 部署到 Railway / Render / Fly.io

### Railway（推荐）

1. 在 Railway 新建项目，连接 GitHub 仓库
2. 选 `alpha_hive_bot/` 作为 root（或在仓库根写 `railway.json` 指定）
3. Variables 配置：`BOT_TOKEN`, `ADMIN_USER_IDS`, `PUSH_HOUR_PDT`（可选）
4. 持久化 `subscribers.db`：挂载 Volume 到 `/data`，设 `DB_PATH=/data/subscribers.db`
5. Procfile 已就绪（`worker: python -m alpha_hive_bot.bot`）
6. Deploy

### Render

类似 Railway，选 "Background Worker"，命令 `python -m alpha_hive_bot.bot`，
持久 disk 挂 `/data`。

### Fly.io

```bash
fly launch --no-deploy
# fly.toml 改 [processes] worker = "python -m alpha_hive_bot.bot"
fly volumes create data --size 1
fly deploy
```

---

## 合规

- 每条简报头部和 bot 描述均带免责声明
- **不提供个性化建议**，所有评分仅基于公开信息聚合
- 用户主动 `/unsubscribe` 可随时退订
- bot 不收集除 Telegram user_id / chat_id / username 外的任何信息

---

## 故障排查

**bot 启动失败 `BOT_TOKEN 未设置`** — 检查 `.env` 是否加载，或 Railway 是否设了 Variable。

**用户 `/start` 显示"未被邀请"** — 确认管理员先 `/invite <user_id>` 加白名单。

**定时推送没触发** — 检查 `PUSH_HOUR_PDT` 配置 + 看 worker 日志 `scheduler` 行。

**推送内容是 "扫描未生成"** — 当日扫描被空扫描护栏拦截（v0.27.2），等扫描成功后 `/push_now` 手动触发。

---

## 路线图（v0.2.0 之后）

- on-demand 标的查询 `/scan <TICKER>`
- 告警订阅 `/alert <TICKER> score>7`
- Stripe / Lemon Squeezy 接入（开放对外收费时）
- AI 问答（需评估合规风险）
