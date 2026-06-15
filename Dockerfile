# Alpha Hive Bot — 专用 Dockerfile（Railway 优先用 Dockerfile，绕过 nixpacks 漏 COPY 问题）
# bot 完全自包含：只依赖 python-telegram-bot + httpx + 标准库，零主项目依赖
FROM python:3.11-slim

WORKDIR /app

# 只装 bot 需要的两个依赖（不装 pandas/numpy/matplotlib 等主项目重包，镜像极小）
RUN pip install --no-cache-dir "python-telegram-bot>=21.0,<22.0" "httpx>=0.25.0"

# 显式 COPY 整个 bot 包（确定性，nixpacks 漏 COPY 的问题根除）
COPY alpha_hive_bot/ ./alpha_hive_bot/

# 默认启动命令（注意：Railway UI 的 Custom Start Command 会覆盖此行，需清空）
CMD ["python", "-m", "alpha_hive_bot.bot"]
