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
# 换 .app 用的 Python：make alphabot-app PYTHON=/绝对路径（相对路径 / 裸名字会被拒：make -C 先切了目录，双击时 cwd / PATH 也不同）
# .app 跑的代码缺省是生产克隆 ~/alpha-hive-prod（阶段 8）。统一入口：make -C ~/alpha-hive-prod alphabot-app（克隆自己的代码生成）；
# 在别的检出里 make 也会把整次生成交给克隆自己的生成器（v0.45.451 起）；克隆不在就报错。测自己的检出才直接跑 -m alphabot.macos_app --repo .
# 谁跑生成器（v0.45.607）：命令行上的 PYTHON 是绝对路径且是可执行文件 ⇒ 就由它跑（换解释器常常正是因为缺省坏了）；
# 否则用 ALPHABOT_GEN_PY（缺省 /usr/local/bin/python3；测试换它）——PYTHON 还没装好也照样生成、只警告（v0.45.458 的宽容）。
# 「可执行文件」与 .app 壳里双击时同一判据（macos_app.SHELL_PY_USABLE，守卫逐字核对）。规则只在这里：提示 / 帮助页
# 只写 PYTHON=X 就都跑得通——v0.45.600 把 ALPHABOT_GEN_PY=X 写进提示文字，帮助页那处就漏了。路径可带空格（全程加引号）
# ALPHABOT_GEN_PY 只是「缺省 runner」：PYTHON 可用时它就不用，即便也写在命令行上（v0.45.611 写明）。给了 PYTHON 却
# 退回缺省时打印一行（v0.45.611）：否则缺省也坏时只见报错里的缺省路径，看不出为什么没用自己给的 PYTHON
# PYTHON 只认**命令行上**给的（$(origin) = command line）：make 会把同名环境变量导入成 make 变量，而 PYTHON 是常见
# 环境变量（node-gyp 等要求 export PYTHON=python3）——v0.45.456 起它会因「须绝对路径」让推荐入口无故失败（v0.45.458）。
# 「命令行」含外层 make 的命令行：在 `make X PYTHON=…` 的配方里再调本目标，PYTHON 经 MAKEFLAGS 传下来、origin 也是
# command line（make 3.81 实测）——那同样是人写的值，照收照核（裸名字照样拒），不当环境变量忽略（v0.45.461）
ALPHABOT_GEN_PY ?= /usr/local/bin/python3
alphabot-app:
	$(if $(filter environment%,$(origin PYTHON)),$(info 忽略环境变量 PYTHON=$(PYTHON)：.app 用的解释器只认这条命令行上的 PYTHON=/绝对路径))
	@PY=$(if $(filter command line,$(origin PYTHON)),"$(PYTHON)",""); GEN="$(ALPHABOT_GEN_PY)"; \
	case "$$PY" in /*) if [ -f "$$PY" ] && [ -x "$$PY" ]; then GEN="$$PY"; fi;; esac; \
	if [ -n "$$PY" ] && [ "$$GEN" != "$$PY" ]; then \
	  echo "PYTHON=$$PY 现在不能拿来跑生成器（须是可执行文件的绝对路径）⇒ 生成器改由 $$GEN 跑" >&2; fi; \
	"$$GEN" -m alphabot.macos_app $(if $(filter command line,$(origin PYTHON)),--python "$(PYTHON)",)

alphabot-stop:
	/usr/local/bin/python3 -m alphabot.launcher --stop
