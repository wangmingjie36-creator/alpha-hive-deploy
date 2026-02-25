.PHONY: test test-cov test-fast lint scan docker-build docker-run clean

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
