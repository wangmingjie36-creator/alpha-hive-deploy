# Alpha Hive Reddit 推广文案

> 发布频率：每周 1 篇
> 目标 Subreddit：r/algotrading, r/investing, r/MachineLearning, r/SideProject
> 调性：技术深度 + 开源精神 + 不卖东西
> 策略：先建立技术信誉，自然引流

---

## #1 r/algotrading 技术架构帖（第 1 周发）

### Title: I built a swarm intelligence system with 7 AI agents for investment research - here's the architecture

Hey r/algotrading,

I've been working on an investment research system called Alpha Hive that uses 7 autonomous AI agents running in parallel, inspired by bee swarm intelligence. Thought I'd share the architecture.

**The Problem**

Most "AI stock analysis" tools are just one LLM prompt. That's like asking one analyst to be an expert in insider trading, options flow, social sentiment, and macro — all at once. The output is shallow.

**My Approach: Specialized Agent Swarm**

Instead of one monolithic LLM, each agent handles exactly one dimension:

| Agent | Data Source | Weight in Score |
|-------|-----------|-----------------|
| ScoutBee | SEC Form 4/13F insider trades | 0.30 |
| OracleBee | Options (IV, Put/Call, Gamma) | 0.15 |
| BuzzBee | StockTwits/Reddit/News sentiment | 0.20 |
| ChronosBee | Earnings calendar & events | 0.20 |
| GuardBee | Cross-validation & risk | 0.15 |
| BearBee | Mandatory contrarian analysis | N/A (qualitative) |
| Queen | Majority vote synthesis | Final output |

**The "Pheromone Board" — How Agents Communicate**

Inspired by real bee behavior (waggle dance):

1. Each agent independently analyzes its data source
2. Findings are posted to a shared "Pheromone Board" (thread-safe, max 20 entries)
3. When other agents corroborate a finding, its pheromone strength increases
4. Signals need confirmation from ≥3 different dimensions to trigger "Resonance"
5. Unconfirmed signals decay automatically (strength -0.1 per cycle)

This naturally filters noise without needing explicit rules.

**Scoring Formula**

```
Opportunity Score = 0.30×Signal + 0.20×Catalyst + 0.20×Sentiment + 0.15×Odds + 0.15×RiskAdj
```

Decision thresholds:
- ≥ 7.5: High priority
- 6.0-7.4: Watchlist
- < 6.0: No action

**Mandatory Bear Case**

Every report MUST include ≥3 bearish/risk arguments. This is hardcoded — the system won't publish without it. Dramatically reduced false positives in practice.

**Tech Stack**

- Python 3.9+, pandas, yfinance, BeautifulSoup
- SQLite for pheromone persistence
- Claude Haiku for optional LLM enhancement (60% rule-engine, 40% LLM)
- ChromaDB for long-term vector memory (optional)
- Total running cost: ~$30/month

**What I Learned After 3 Months**

1. **Data quality > model sophistication.** Garbage in, garbage out. Most of my effort went into resilient data pipelines, not fancy models.
2. **ML win rate is only ~17%.** Rule-based scoring with cross-validation consistently outperforms pure ML predictions for this use case.
3. **The pheromone decay mechanism is surprisingly effective.** It naturally eliminates stale signals without manual cleanup.
4. **Mandatory bear case analysis cut false positives by roughly 30%.** Forcing the system to argue against itself is powerful.
5. **Circuit breakers are essential.** SEC rate limits (10 req/sec), yfinance instability, and API failures happen daily. Without graceful degradation, the system would be unusable.

**Accuracy Tracking**

The system tracks its own predictions at T+1, T+7, and T+30 intervals. Weight adjustments happen automatically based on historical accuracy.

Happy to discuss architecture decisions, trade-offs, or answer questions. Built this as a solo developer over ~6 months.

⚠️ Not financial advice. This is a data analysis tool.

---

## #2 r/investing 数据帖（第 3 周发）

### Title: SEC Form 4 insider buying data this week — tracked by AI, no opinions attached

Hey r/investing,

I run an automated system that scans SEC Form 4 filings daily. Here's what insiders were buying this week (just the data, no recommendations):

**Significant Insider Purchases (>$500K) — Week of [DATE]**

| Company | Insider | Role | Shares | Value | Date |
|---------|---------|------|--------|-------|------|
| [TICKER] | [Name] | [Title] | [#] | $[Amount] | [Date] |
| [TICKER] | [Name] | [Title] | [#] | $[Amount] | [Date] |

**Notes:**
- Filtered for open-market purchases only (excluded 10b5-1 plans and option exercises)
- Data source: SEC EDGAR Form 4 filings
- This is raw data, not investment advice

**Context that might be relevant (but draw your own conclusions):**
- [TICKER]: Earnings on [date], IV Rank at [X]
- [TICKER]: Ex-dividend [date]

If people find this useful I can make it a weekly thing. The system (Alpha Hive) runs this scan automatically every morning.

⚠️ Not financial advice. Just sharing publicly available SEC data in a readable format.

---

## #3 r/SideProject 产品故事帖（第 2 周发）

### Title: I'm a solo developer running a "one-person AI company" — 7 AI agents do the work, I do the thinking

Hey r/SideProject,

Six months ago I started building Alpha Hive — a swarm intelligence system for investment research. Today it's my one-person company registered in Shanghai, China.

**What it does:**
7 AI agents analyze stocks in parallel (SEC filings, options, sentiment, catalysts), then vote on opportunities. Output: a structured daily briefing in Chinese + English.

**The numbers:**
- Running cost: ~$30/month (Claude Haiku API + free data sources)
- Lines of code: ~30,000 across 59 Python files
- Daily operation: 30 min of my time (checking + iterating)
- Revenue target: 30-50 paying subscribers at $15-45/month

**What I learned about building a one-person AI company:**
1. Don't build an app first. I started with a WeChat group + daily PDF. Validate demand before building infrastructure.
2. AI costs are surprisingly low. $1/day for what would take a human analyst 4-6 hours.
3. The hardest part isn't AI — it's data pipelines. SEC rate limiting, API failures, data quality issues eat 70% of dev time.
4. Be transparent about limitations. I publish accuracy scorecards (hits AND misses). Trust > hype.

**Tech stack:** Python, pandas, yfinance, SQLite, Claude Haiku, BeautifulSoup

Happy to answer questions about solo AI product development or the technical architecture.

---

## #4 r/MachineLearning 技术讨论帖（备用）

### Title: Swarm intelligence vs. single-agent LLM for financial data analysis — lessons from building a multi-agent system

[深度讨论多 Agent 架构 vs 单 Agent 的工程权衡]
[分享信息素衰减机制的数学模型]
[讨论 rule-based vs ML 在金融场景的实际对比]

⚠️ Not financial advice. Academic/engineering discussion only.
