.PHONY: test test-cov test-fast lint scan docker-build docker-run clean alphabot alphabot-demo alphabot-app alphabot-stop

# ==================== 测试 ====================
test:
	python3 -m pytest tests/ -v

test-cov:
	python3 -m pytest tests/ -v --cov=. --cov-report=term-missing --cov-report=html:htmlcov

test-fast:
	python3 -m pytest tests/ -x -q --timeout=10

# ==================== 代码质量 ====================
lint:
	python3 -m py_compile config.py
	python3 -m py_compile swarm_agents.py
	python3 -m py_compile alpha_hive_daily_report.py
	python3 -m py_compile pheromone_board.py
	python3 -m py_compile memory_store.py
	python3 -m py_compile hive_logger.py
	python3 -m py_compile resilience.py
	python3 -m py_compile models.py
	python3 -m py_compile metrics_collector.py
	@echo "All core files compile OK"

# ==================== 蜂群扫描 ====================
scan:
	python3 alpha_hive_daily_report.py --swarm --tickers NVDA TSLA VKTX

scan-all:
	python3 alpha_hive_daily_report.py --swarm --all-watchlist

# ==================== Docker ====================
docker-build:
	docker build -t alpha-hive:latest .

docker-run:
	docker run --rm --env-file .env alpha-hive:latest --swarm --tickers NVDA

# ==================== 清理 ====================
clean:
	rm -rf htmlcov .pytest_cache __pycache__ tests/__pycache__
	rm -f .coverage
	find . -name "*.pyc" -delete

# ==================== Alpha Bot（卖权选择器本机前端，v0.45.388） ====================
# 只绑 127.0.0.1。读生产账本需先 export ALPHA_HIVE_HOME=<数据根>（与编排器 / MCP 同一个值）。
alphabot:
	/usr/local/bin/python3 -m alphabot --open

alphabot-demo:
	/usr/local/bin/python3 -m alphabot --demo --open

# 生成 macOS 桌面程序 ~/Applications/Alpha Bot.app（双击启动服务并打开页面；已在跑就只开页面）。
# 数据根：启动器还没配过时顺手记下当前环境的 ALPHA_HIVE_HOME（已配过的不改写，要换用 --home）；都没有就首次双击时选。
# 换 Python：make alphabot-app PYTHON=路径
# .app 跑的代码缺省是生产克隆 ~/alpha-hive-prod（阶段 8）。统一入口：make -C ~/alpha-hive-prod alphabot-app（克隆自己的代码生成）；
# 在别的检出里 make 也会把整次生成交给克隆自己的生成器（v0.45.451 起）；克隆不在就报错。测自己的检出才直接跑 -m alphabot.macos_app --repo .
alphabot-app:
	/usr/local/bin/python3 -m alphabot.macos_app $(if $(PYTHON),--python "$(PYTHON)",)

alphabot-stop:
	/usr/local/bin/python3 -m alphabot.launcher --stop
