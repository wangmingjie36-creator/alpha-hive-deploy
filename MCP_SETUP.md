# Alpha Hive MCP Server — 安装说明

让 Claude Desktop（Cowork 模式）直接调用你的 Alpha Hive 分析数据、
yfinance 实时行情和 SEC EDGAR 内幕交易记录。

---

## 第一步：安装依赖

```bash
pip install "mcp[cli]" yfinance httpx pydantic
```

> 如果 `mcp` 包找不到，也可以用：
> ```bash
> pip install mcp
> ```

---

## 第二步：确认文件路径

MCP server 文件已经保存在：

```
~/Desktop/Alpha Hive/alpha_hive_mcp.py
```

服务器默认读取以下两个目录（在脚本顶部可修改）：

| 常量 | 路径 |
|------|------|
| `_HIVE_DIR` | `~/Desktop/Alpha Hive/` — JSON 分析文件 |
| `_DEEP_DIR` | `~/Desktop/深度分析报告/深度/` — HTML 报告 |

---

## 第三步：配置 Claude Desktop

打开（或创建）配置文件：

```
~/.claude/claude_desktop_config.json
```

添加以下内容（如果文件已有其他 server，在 `mcpServers` 里追加）：

```json
{
  "mcpServers": {
    "alpha_hive": {
      "command": "python3",
      "args": ["/Users/igg/Desktop/Alpha Hive/alpha_hive_mcp.py"]
    }
  }
}
```

> **注意**：路径里的空格需要保留在 `args` 数组里，不要用引号转义整个路径，
> JSON 数组格式会自动处理空格。

---

## 第四步：重启 Claude Desktop

保存配置文件后，完全退出并重新打开 Claude Desktop。
在 Cowork 会话里，Claude 就可以直接调用以下工具：

---

## 可用工具一览

| 工具名 | 功能 | 数据源 |
|--------|------|--------|
| `alphahive_list_reports` | 列出本地所有分析报告 | 本地 JSON |
| `alphahive_get_analysis` | 获取某 ticker 完整分析 JSON | 本地 JSON |
| `alphahive_get_swarm_scores` | 蜂群评分 + 置信区间摘要 | 本地 JSON |
| `alphahive_get_gex` | Dealer GEX 快照（含 regime/flip/墙位） | 本地 JSON |
| `alphahive_get_options_snapshot` | 实时 P/C 比率、最高 OI 行权价 | yfinance 网络 |
| `alphahive_get_quote` | 实时股价 + 基本面 | yfinance 网络 |
| `alphahive_get_price_history` | OHLCV K 线历史 | yfinance 网络 |
| `alphahive_get_insider_trades` | 近期 Form 4 内幕交易 | SEC EDGAR 网络 |

---

## 使用示例

安装后，你可以直接在 Cowork 对话里说：

- "帮我看看今天 NVDA 的蜂群评分和置信区间"
- "NVDA 的 GEX 数据现在是什么 regime？"
- "拉一下 AAPL 近三个月的股价走势"
- "最近 90 天 NVDA 有没有内幕卖出？"
- "对比一下 NVDA 和 AMD 的实时 P/C 比率"

Claude 会自动调用对应工具，无需手动跑脚本。

---

## 验证安装

在终端运行以下命令，如果无报错则说明依赖正常：

```bash
python3 ~/Desktop/"Alpha Hive"/alpha_hive_mcp.py --help
```

或者直接测试服务器启动：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | \
  python3 ~/Desktop/"Alpha Hive"/alpha_hive_mcp.py
```

---

## 常见问题

**Q: `ModuleNotFoundError: No module named 'mcp'`**
```bash
pip install "mcp[cli]"
```

**Q: `No analysis reports found` — 工具找不到 JSON**
先运行数据采集：
```bash
cd ~/Desktop/"Alpha Hive"
python3 alpha_hive.py --ticker NVDA
```

**Q: yfinance 工具超时**
检查网络连接。yfinance 需要访问 `finance.yahoo.com`。

**Q: SEC EDGAR 返回空列表**
EDGAR 索引更新有延迟（通常 24 小时内）。增大 `days_back` 参数到 180 再试。
