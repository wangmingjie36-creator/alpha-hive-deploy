# 🚀 Alpha Hive Phase 3 P1：代码执行 & 调试技能

**预计工作量**：2-3 天
**优先级**：P1（立即）
**目标**：Agent 自动写代码、自己跑、自己调试（安全沙箱版）

---

## 📋 需求分析

### 核心目标
让 Alpha Hive 中的 Agent 能够：
1. 自动生成 Shell/Python 代码
2. 在安全沙箱中执行代码
3. 实时捕获输出 + 错误处理
4. 自动解析结果 + 写入报告

### 典型用途场景
```
场景 1：自动数据爬取
  Agent 发现需要爬取 SEC 数据
  → 自动生成爬取脚本
  → 在沙箱执行
  → 解析结果，保存到数据库

场景 2：实时市场数据采集
  Agent 需要获取期权链数据
  → 生成 yfinance 脚本
  → 执行并获取数据
  → 特征工程处理

场景 3：可视化生成
  Agent 需要生成图表
  → 生成 matplotlib/plotly 代码
  → 执行脚本
  → 保存图像文件

场景 4：A/B 测试
  Agent 需要验证某个策略
  → 生成测试脚本
  → 运行测试 + 统计分析
  → 输出结果报告
```

---

## 🏗️ 架构设计

### 三个实现方案对比

| 方案 | 技术 | 优点 | 缺点 | 推荐度 |
|------|------|------|------|--------|
| **A（推荐）** | Python subprocess + 沙箱 | 无依赖、易控制、安全 | 功能相对基础 | ⭐⭐⭐⭐⭐ |
| **B** | Terminal MCP | 完整、强大 | 需要 Node.js | ⭐⭐⭐ |
| **C** | Docker 容器 | 隔离完整 | 复杂、慢 | ⭐⭐ |

### 推荐实施：方案 A（Python subprocess + 沙箱）

**原因**：
- ✅ 无新依赖（仅用 subprocess）
- ✅ 快速启动（毫秒级）
- ✅ 易于集成到现有系统
- ✅ 安全控制灵活
- ✅ Phase 2 基础上的自然演进

---

## 🔧 详细实现方案

### 模块 1：CodeExecutor（代码执行器）

```python
# code_executor.py - 300 行

class CodeExecutor:
    """安全的代码执行引擎"""
    
    def __init__(self, max_timeout=30, max_memory=512, sandbox_dir=None):
        """
        Args:
            max_timeout: 最大执行时间（秒）
            max_memory: 最大内存使用（MB）
            sandbox_dir: 沙箱目录（只能在此目录读写）
        """
        self.max_timeout = max_timeout
        self.max_memory = max_memory
        self.sandbox_dir = sandbox_dir or "/tmp/alpha_hive_sandbox"
        self._init_sandbox()
    
    def execute_python(self, code: str, return_output=True) -> Dict:
        """
        执行 Python 代码
        
        Returns:
            {
                "success": bool,
                "stdout": str,
                "stderr": str,
                "return_value": Any,
                "execution_time": float,
                "exit_code": int
            }
        """
        pass
    
    def execute_shell(self, command: str) -> Dict:
        """执行 Shell 命令"""
        pass
    
    def execute_file(self, file_path: str) -> Dict:
        """执行脚本文件"""
        pass
```

### 模块 2：CodeGenerator（代码生成器）

```python
# code_generator.py - 200 行

class CodeGenerator:
    """代码生成助手"""
    
    def generate_data_fetch(self, source: str, params: Dict) -> str:
        """
        生成数据爬取脚本
        
        Args:
            source: 数据源（"yfinance", "sec", "polymarket", "x_api"）
            params: 参数字典
        
        Returns:
            Python 代码字符串
        """
        pass
    
    def generate_analysis(self, analysis_type: str, data_source: str) -> str:
        """生成数据分析脚本"""
        pass
    
    def generate_visualization(self, chart_type: str, data: Dict) -> str:
        """生成可视化代码"""
        pass
```

### 模块 3：Debugger（调试器）

```python
# debugger.py - 150 行

class Debugger:
    """代码调试与错误处理"""
    
    def parse_error(self, stderr: str) -> Dict:
        """
        解析错误信息
        
        Returns:
            {
                "error_type": str,
                "line_number": int,
                "message": str,
                "suggestion": str
            }
        """
        pass
    
    def suggest_fix(self, error: Dict, code: str) -> str:
        """建议修复方案"""
        pass
    
    def auto_retry(self, code: str, max_attempts=3) -> Dict:
        """自动重试机制"""
        pass
```

### 模块 4：CodeExecutorAgent（Agent 集成）

```python
# code_executor_agent.py - 250 行

class CodeExecutorAgent(BeeAgent):
    """能够执行代码的 Agent"""
    
    def __init__(self, board, retriever=None, executor=None):
        super().__init__(board, retriever)
        self.executor = executor or CodeExecutor()
    
    def analyze(self, ticker: str) -> Dict:
        """
        示例：通过代码执行进行分析
        
        流程：
        1. 生成数据爬取脚本
        2. 执行获取数据
        3. 进行分析
        4. 发布结果
        """
        pass
    
    def auto_debug(self, code: str) -> str:
        """自动调试失败的代码"""
        pass
```

---

## 📊 实现时间表

### Day 1（8h）：核心引擎
- [ ] CodeExecutor 基础框架（100 行）
- [ ] 沙箱初始化 + 资源限制（60 行）
- [ ] Python/Shell 执行方法（80 行）
- [ ] 错误捕获 + 日志记录（60 行）
- [ ] 单元测试（60 行）

### Day 2（8h）：生成器 + Agent 集成
- [ ] CodeGenerator 数据爬取模板（80 行）
- [ ] CodeGenerator 分析模板（60 行）
- [ ] CodeGenerator 可视化模板（50 行）
- [ ] CodeExecutorAgent 集成（100 行）
- [ ] 集成测试（80 行）

### Day 3（8h）：调试 + 文档
- [ ] Debugger 错误解析（80 行）
- [ ] Debugger 自动重试（70 行）
- [ ] 完整文档（100 行）
- [ ] 性能优化 + 微调（60 行）
- [ ] 系统测试（40 行）

---

## 🔒 安全性设计

### 1. 沙箱隔离
```
/tmp/alpha_hive_sandbox/
├── 2026-02-24/
│   ├── session_1/
│   │   ├── scripts/
│   │   ├── data/
│   │   └── output/
```

### 2. 资源限制
- CPU 时间：最多 30 秒（可配）
- 内存：最多 512 MB（可配）
- 文件大小：最多 100 MB
- 网络：仅允许 HTTPS（可配）

### 3. 白名单控制
```python
ALLOWED_MODULES = {
    'yfinance', 'pandas', 'numpy', 'matplotlib', 
    'requests', 'sqlite3', 'json', 'datetime', 
    'statistics', 'csv', 're', 'collections'
}

BLOCKED_MODULES = {
    'os', 'sys', 'subprocess', 'socket', 'shutil',
    '__import__', 'exec', 'eval', 'open'  # 严格模式
}
```

### 4. 审计日志
```
2026-02-24 14:30:45 | ScoutBeeNova | EXECUTE_PYTHON | OK | 0.52s
2026-02-24 14:31:20 | OracleBeeEcho | EXECUTE_SHELL | ERROR | timeout
```

---

## ✅ 验证用例

### 用例 1：爬取 SEC Form 4
```python
code = """
import yfinance as yf

# 获取 NVDA 最近交易
nvda = yf.Ticker("NVDA")
info = nvda.info
print(f"价格: {info['currentPrice']}")
print(f"52周高: {info['fiftyTwoWeekHigh']}")
"""

result = executor.execute_python(code)
# 预期：成功，输出价格信息
```

### 用例 2：数据分析
```python
code = """
import pandas as pd
import numpy as np

# 计算移动平均
data = [10, 12, 11, 13, 15, 14, 16, 18]
ma_3 = pd.Series(data).rolling(3).mean()
print(ma_3.tolist())
"""

result = executor.execute_python(code)
# 预期：成功，返回 MA 序列
```

### 用例 3：可视化
```python
code = """
import matplotlib.pyplot as plt

data = [10, 12, 11, 13, 15, 14, 16, 18]
plt.figure(figsize=(10, 6))
plt.plot(data)
plt.savefig('/tmp/alpha_hive_sandbox/2026-02-24/session_1/output/chart.png')
print("Chart saved successfully")
"""

result = executor.execute_python(code)
# 预期：成功，生成图表文件
```

---

## 🎯 成功指标

| 指标 | 目标 | 验证方式 |
|------|------|---------|
| 代码执行成功率 | > 99% | 100 个测试用例 |
| 执行延迟 | < 1s（简单脚本） | 性能基准测试 |
| 沙箱隔离 | 100% 安全 | 渗透测试 |
| 错误恢复 | > 95% | 故意触发错误 |
| 代码行数 | < 1000 行 | 代码统计 |

---

## 📚 文档要求

- [ ] `code_executor.py`：核心引擎（300 行）
- [ ] `code_generator.py`：代码生成（200 行）
- [ ] `debugger.py`：调试工具（150 行）
- [ ] `code_executor_agent.py`：Agent 集成（250 行）
- [ ] `PHASE3_P1_IMPLEMENTATION.md`：实现细节
- [ ] `test_code_executor.py`：测试脚本（200 行）

**总计**：~1150 行代码 + 文档

---

## 🚀 启动指令

```bash
# Step 1: 实现核心模块
python3 << 'IMPL'
# 编写 code_executor.py, code_generator.py, debugger.py
IMPL

# Step 2: 运行测试
python3 test_code_executor.py

# Step 3: 集成到 Agent
# 修改 swarm_agents.py，添加 CodeExecutorAgent

# Step 4: 完整验证
python3 alpha_hive_daily_report.py --enable-code-execution
```

---

## ⚠️ 风险与缓解

| 风险 | 概率 | 缓解方案 |
|------|------|---------|
| 恶意代码执行 | 低 | 白名单 + 沙箱 + 审计日志 |
| 性能下降 | 中 | 异步执行 + 资源限制 |
| 内存泄漏 | 低 | 定期清理 + 监控 |
| 文件系统满 | 低 | 配额限制 + 自动清理 |

---

**版本**：Phase 3 P1
**完成时间**：预计 2026-02-27
**状态**：📋 规划完成，待实施
