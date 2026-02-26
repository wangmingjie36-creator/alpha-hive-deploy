FROM python:3.11-slim

# 元数据
LABEL maintainer="Alpha Hive"
LABEL description="Decentralized swarm-intelligence investment research agent"

WORKDIR /app

# 系统依赖（仅编译必需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 先安装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir pytest pytest-cov pytest-timeout

# 应用代码
COPY *.py ./
COPY tests/ tests/

# 环境变量默认值
ENV ALPHA_HIVE_HOME=/app
ENV ALPHA_HIVE_LOGS_DIR=/app/logs
ENV ALPHA_HIVE_CACHE_DIR=/app/cache
ENV ALPHA_HIVE_DB_PATH=/app/data/pheromone.db
ENV ALPHA_HIVE_CHROMA_PATH=/app/data/chroma_db
ENV ALPHA_HIVE_LOG_LEVEL=INFO

# 创建目录并设置权限
RUN mkdir -p logs cache data reports reddit_cache finviz_cache stocktwits_cache && \
    chmod 755 logs cache data reports

# Health check：验证核心模块可导入
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python3 -c "from pheromone_board import PheromoneBoard; from fear_greed import get_fear_greed; print('OK')" || exit 1

# 非 root 用户运行
RUN adduser --disabled-password --gecos '' hiveuser && \
    chown -R hiveuser:hiveuser /app
USER hiveuser

# 默认入口：蜂群扫描
ENTRYPOINT ["python3", "alpha_hive_daily_report.py"]
CMD ["--swarm", "--tickers", "NVDA"]
