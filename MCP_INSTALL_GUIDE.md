# Alpha Hive · MCP 服务器安装指南
> 生成时间：2026-03-14  |  适用平台：macOS（你的真实 Mac）

---

## 总览：5 个已下载的 MCP 服务器

| # | 服务器 | 路径 | 需要 API Key | 状态 |
|---|--------|------|-------------|------|
| 1 | mcp-optionsflow | mcp-servers/mcp-optionsflow/ | ❌ 无需 | ✅ 代码已下载 |
| 2 | yahoo-finance-mcp | mcp-servers/yahoo-finance-mcp/ | ❌ 无需 | ✅ 代码已下载 |
| 3 | finance-tools-mcp | mcp-servers/finance-tools-mcp/ | 🟡 FRED（免费） | ✅ 代码已下载 |
| 4 | shareseer-mcp-server | 远程 URL | 🟡 ShareSeer（免费） | ✅ 注册后即用 |
| 5 | maverick-mcp | mcp-servers/maverick-mcp/ | 🟡 Tiingo（免费） | ✅ 代码已下载 |

所有代码位置：`~/Desktop/Alpha Hive/mcp-servers/`

---

## 在你的 Mac 上激活步骤

### 前置条件（Mac 上执行一次）

```bash
# 1. 安装 Python 3.12（MaverickMCP 需要）
brew install python@3.12

# 2. 安装 uv（快速包管理器）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. 安装 TA-Lib（技术指标库，MaverickMCP 需要）
brew install ta-lib
```

---

### #1 mcp-optionsflow（期权链 + Greeks）

**特点：无需任何 API Key，纯 yfinance**

```bash
cd ~/Desktop/Alpha\ Hive/mcp-servers/mcp-optionsflow
pip3 install -r requirements.txt
```

加入 `claude_desktop_config.json`：
```json
"optionsflow": {
  "command": "python3",
  "args": ["/Users/你的用户名/Desktop/Alpha Hive/mcp-servers/mcp-optionsflow/optionsflow.py"]
}
```

**能为 Alpha Hive 提供：**
- 完整期权链 delta/gamma/theta/vega 计算
- CSP / CC / CCS / PCS 四种策略评估
- 胜率概率、最大损失、仓位建议

---

### #2 yahoo-finance-mcp（期权/内部人/新闻）

**特点：无需任何 API Key，数据最全**

```bash
cd ~/Desktop/Alpha\ Hive/mcp-servers/yahoo-finance-mcp
pip3 install "mcp[cli]>=1.6.0" yfinance
```

加入 `claude_desktop_config.json`：
```json
"yahoo-finance": {
  "command": "python3",
  "args": ["/Users/你的用户名/Desktop/Alpha Hive/mcp-servers/yahoo-finance-mcp/server.py"]
}
```

**能为 Alpha Hive 提供：**
- `get_option_chain` → OracleBee 期权数据
- `get_holder_info` → ScoutBee 机构持仓/内部人交易
- `get_yahoo_finance_news` → BuzzBee 新闻数据
- `get_financial_statement` → ScoutBee 基本面

---

### #3 finance-tools-mcp（Fear & Greed + FRED 宏观）

**特点：直接拿到 CNN F&G 数字，不用再靠正则解析**

**Step 1：申请免费 FRED API Key**
→ 访问 https://fredaccount.stlouisfed.org/login/secure/
→ 注册后在 API Keys 页面生成（免费无限制）

```bash
cd ~/Desktop/Alpha\ Hive/mcp-servers/finance-tools-mcp
pip3 install finance-tools-mcp
```

加入 `claude_desktop_config.json`：
```json
"finance-tools": {
  "command": "uvx",
  "args": ["finance-tools-mcp"],
  "env": {
    "FRED_API_KEY": "你的FRED_KEY"
  }
}
```

**能为 Alpha Hive 提供：**
- `get_fear_greed_index` → 直接数字，GuardBee 宏观数据
- `get_fred_series` → 利率/CPI/GDP → GuardBee
- 技术指标（ta-lib）

---

### #4 ShareSeer MCP（内部人交易 + SEC 实时）

**特点：纯远程 URL，零安装**

**Step 1：注册免费账号**
→ https://shareseer.com/signup（免费层：10次/小时，50次/天）

**Step 2：在 Claude Desktop 添加集成**
→ 设置 → Search & Tools → Add Integration URL：
```
https://shareseer.com/mcp?api_key=sk-shareseer-你的KEY
```

**能为 Alpha Hive 提供：**
- `get_insider_transactions` → BearBee 逆向信号（内部人卖出）
- `get_largest_daily_transactions` → 当日最大内部人交易
- `get_company_filings` → SEC 8-K/10-Q 实时监控

---

### #5 MaverickMCP（39+ 工具全套）

**特点：最强大，但需要 Python 3.12 和 TA-Lib**

**Step 1：申请免费 Tiingo API Key**
→ https://www.tiingo.com/account/api/token（免费，日 5000 次）

```bash
cd ~/Desktop/Alpha\ Hive/mcp-servers/maverick-mcp
uv sync          # 自动创建虚拟环境并安装全部依赖
cp .env.example .env
# 编辑 .env，填入：TIINGO_API_KEY=你的KEY
make dev         # 启动服务器（默认 http://localhost:8003）
```

加入 `claude_desktop_config.json`：
```json
"maverick-mcp": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "http://localhost:8003/sse/"]
}
```

**能为 Alpha Hive 提供：**
- RSI / MACD / 布林带 / 支撑阻力
- `get_news_sentiment` 新闻情绪打分
- 20+ 策略回测（VectorBT）
- 510只 S&P 500 股票推荐

---

## claude_desktop_config.json 完整示例

文件位置：`~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "optionsflow": {
      "command": "python3",
      "args": ["/Users/igg/Desktop/Alpha Hive/mcp-servers/mcp-optionsflow/optionsflow.py"]
    },
    "yahoo-finance": {
      "command": "python3",
      "args": ["/Users/igg/Desktop/Alpha Hive/mcp-servers/yahoo-finance-mcp/server.py"]
    },
    "finance-tools": {
      "command": "uvx",
      "args": ["finance-tools-mcp"],
      "env": {
        "FRED_API_KEY": "填入你的FRED_KEY"
      }
    },
    "maverick-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8003/sse/"]
    }
  }
}
```

> ShareSeer 直接在 Claude Desktop 界面里加，不走 config 文件。

---

## 给 Alpha Hive 各模块的数据对应表

| Alpha Hive 模块 | 原来数据来源 | 新 MCP 数据来源 | 提升效果 |
|----------------|------------|----------------|---------|
| OracleBee 期权 | options_analyzer.py 样本数据 | #1 optionsflow + #2 yahoo-finance | 真实 Greeks，精准 GEX |
| ScoutBee 基本面 | yfinance（有时断网） | #2 yahoo-finance（内部人/机构） | 内部人实时数据 |
| BearBee 逆向 | 解析文本 | #4 ShareSeer SEC | 实时 SEC 内部人卖出 |
| GuardBee 宏观 | 正则提取 F&G | #3 finance-tools F&G | 直接数字，无需解析 |
| ChronosBee 催化剂 | Chronos 手动 | #5 MaverickMCP 经济日历 | 自动获取事件时间表 |
| 报告叙事 | 本地 fallback | 所有 MCP 数据注入 | 更多真实数据支撑 |
