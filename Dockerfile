# Alpha Hive Bot — 专用 Dockerfile（Railway 优先用 Dockerfile，绕过 nixpacks 漏 COPY 问题）
# bot 完全自包含：只依赖 python-telegram-bot + httpx + 标准库，零主项目依赖
FROM python:3.11-slim

WORKDIR /app

# bot 依赖 + tzdata（slim 镜像无 IANA 时区库，否则 ZoneInfo("America/Los_Angeles")
# 抛错回退容器本地时间 → pdt_today() 算错日期 → 拉错日期简报 skipped）
RUN pip install --no-cache-dir "python-telegram-bot>=21.0,<22.0" "httpx>=0.25.0" tzdata

# 显式 COPY 整个 bot 包（确定性，nixpacks 漏 COPY 的问题根除）
COPY alpha_hive_bot/ ./alpha_hive_bot/

# 默认启动命令（注意：Railway UI 的 Custom Start Command 会覆盖此行，需清空）
CMD ["python", "-m", "alpha_hive_bot.bot"]
