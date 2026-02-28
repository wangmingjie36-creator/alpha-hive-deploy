"""
🐝 Alpha Hive - 配置管理
存储 API 密钥、数据源配置、缓存策略
"""

import os
from datetime import datetime
from pathlib import Path

from hive_logger import PATHS, get_logger

_log = get_logger("config")

# ==================== API 配置 ====================
API_KEYS = {
    # Polymarket API（无需认证，公开数据）
    "POLYMARKET": {
        "base_url": "https://clob.polymarket.com",
        "endpoints": {
            "markets": "/markets",
            "prices": "/prices",
        }
    },

    # StockTwits API
    "STOCKTWITS": {
        "base_url": "https://api.stocktwits.com/api/2",
        "endpoints": {
            "streams": "/streams/symbols/{symbol}.json",
        }
    },

    # Yahoo Finance（通过 yfinance 库）
    "YAHOO_FINANCE": {
        "use_library": True,  # 使用 yfinance 库而不是直接 API
    },

    # Google Trends（通过 pytrends 库）
    "GOOGLE_TRENDS": {
        "use_library": True,  # 使用 pytrends 库
    },

    # SEC EDGAR
    "SEC_EDGAR": {
        "base_url": "https://www.sec.gov/cgi-bin",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; AlphaHive/1.0)"
        }
    },

    # Yahoo Finance（期权数据通过 yfinance 库获取）
    "YAHOO_FINANCE_OPTIONS": {
        "enabled": True,
        "description": "使用 yfinance 库获取期权数据（免费、无需 API Token）"
    },
}

# ==================== 缓存配置 ====================
CACHE_CONFIG = {
    "enabled": True,
    "cache_dir": str(PATHS.cache_dir),
    "ttl": {  # 缓存过期时间（秒）
        "stocktwits": 3600,  # 1 小时
        "polymarket": 300,   # 5 分钟（频繁变化）
        "yahoo_finance": 300,  # 5 分钟
        "google_trends": 86400,  # 24 小时
        "seeking_alpha": 86400,  # 24 小时
        "sec_edgar": 604800,  # 7 天
    }
}

# ==================== 监控标的 ====================
WATCHLIST = {
    # 科技板块 (Technology) - 5 个
    "NVDA": {
        "name": "NVIDIA Corporation",
        "sector": "Technology",
        "polymarket_slug": "nvidia-q1-2026-revenue",
        "monitor_events": ["earnings", "product_launch", "china_sanctions"],
    },
    "TSLA": {
        "name": "Tesla Inc",
        "sector": "Automotive",
        "polymarket_slug": "tesla-delivery-forecast",
        "monitor_events": ["earnings", "production_update", "regulatory"],
    },
    "MSFT": {
        "name": "Microsoft Corporation",
        "sector": "Technology",
        "polymarket_slug": "microsoft-cloud-growth",
        "monitor_events": ["earnings", "azure_adoption", "ai_partnership"],
    },
    "AMD": {
        "name": "Advanced Micro Devices",
        "sector": "Technology",
        "polymarket_slug": "amd-market-share",
        "monitor_events": ["earnings", "product_launch", "supply"],
    },
    "QCOM": {
        "name": "Qualcomm Inc",
        "sector": "Technology",
        "polymarket_slug": "qualcomm-5g-adoption",
        "monitor_events": ["earnings", "flagship_launch"],
    },

    # 生物医药 (Healthcare/Biotech) - 5 个
    "VKTX": {
        "name": "Viking Therapeutics",
        "sector": "Healthcare",
        "polymarket_slug": "viking-therapeutics-fda-approval",
        "monitor_events": ["trial_results", "fda_decision"],
    },
    "AMGN": {
        "name": "Amgen Inc",
        "sector": "Healthcare",
        "polymarket_slug": "amgen-oncology-pipeline",
        "monitor_events": ["trial_results", "fda_approval"],
    },
    "BIIB": {
        "name": "Biogen Inc",
        "sector": "Healthcare",
        "polymarket_slug": "biogen-alzheimers",
        "monitor_events": ["clinical_trial", "regulatory_approval"],
    },
    "JNJ": {
        "name": "Johnson & Johnson",
        "sector": "Healthcare",
        "polymarket_slug": "jnj-pharma-pipeline",
        "monitor_events": ["earnings", "clinical_trial_results"],
    },
    "REGN": {
        "name": "Regeneron Pharmaceuticals",
        "sector": "Healthcare",
        "polymarket_slug": "regn-obesity-drug",
        "monitor_events": ["clinical_data", "fda_decision"],
    },

    # 清洁能源 (Clean Energy) - 5 个
    "PLUG": {
        "name": "Plug Power Inc",
        "sector": "CleanEnergy",
        "polymarket_slug": "plug-hydrogen-adoption",
        "monitor_events": ["supply_deal", "partnership_announcement"],
    },
    "RUN": {
        "name": "Sunrun Inc",
        "sector": "CleanEnergy",
        "polymarket_slug": "sunrun-irs-credits",
        "monitor_events": ["policy_change", "installation_growth"],
    },
    "NEE": {
        "name": "NextEra Energy",
        "sector": "CleanEnergy",
        "polymarket_slug": "nextEra-renewable-expansion",
        "monitor_events": ["earnings", "capacity_expansion"],
    },
    "ICLN": {
        "name": "iClean Energy ETF",
        "sector": "CleanEnergy",
        "polymarket_slug": "clean-energy-policy",
        "monitor_events": ["legislation", "irs_guidance"],
    },
    "ENPH": {
        "name": "Enphase Energy",
        "sector": "CleanEnergy",
        "polymarket_slug": "enphase-battery-sales",
        "monitor_events": ["earnings", "product_launch"],
    },

    # 金融科技 (FinTech) - 3 个
    "SQ": {
        "name": "Block Inc",
        "sector": "FinTech",
        "polymarket_slug": "square-btc-adoption",
        "monitor_events": ["earnings", "product_launch"],
    },
    "COIN": {
        "name": "Coinbase Global",
        "sector": "FinTech",
        "polymarket_slug": "coinbase-btc-price",
        "monitor_events": ["earnings", "regulatory_approval"],
    },
    "MSTR": {
        "name": "MicroStrategy Inc",
        "sector": "FinTech",
        "polymarket_slug": "mstr-bitcoin-reserve",
        "monitor_events": ["btc_purchase", "quarterly_earnings"],
    },

    # 人工智能 (AI) - 2 个
    "UPST": {
        "name": "Upstart Holdings",
        "sector": "AI",
        "polymarket_slug": "upstart-ai-lending",
        "monitor_events": ["earnings", "partnership"],
    },

    # 用户自选标的 (User Watchlist)
    "META": {
        "name": "Meta Platforms Inc",
        "sector": "Technology",
        "polymarket_slug": "meta-ai-revenue",
        "monitor_events": ["earnings", "ai_product_launch", "regulatory"],
    },
    "RKLB": {
        "name": "Rocket Lab USA",
        "sector": "Aerospace",
        "polymarket_slug": "rocket-lab-launch",
        "monitor_events": ["launch_success", "contract_award", "earnings"],
    },
    "BILI": {
        "name": "Bilibili Inc",
        "sector": "Technology",
        "polymarket_slug": "bilibili-user-growth",
        "monitor_events": ["earnings", "monthly_active_users", "regulatory"],
    },
    "AMZN": {
        "name": "Amazon.com Inc",
        "sector": "Technology",
        "polymarket_slug": "amazon-aws-revenue",
        "monitor_events": ["earnings", "aws_growth", "prime_day", "regulatory"],
    },
    "CRCL": {
        "name": "Circle Internet Financial",
        "sector": "Fintech",
        "polymarket_slug": "circle-ipo",
        "monitor_events": ["ipo", "earnings", "usdc_growth", "regulatory", "crypto_policy"],
    },
}

# ==================== 数据源优先级 ====================
DATA_SOURCE_PRIORITY = {
    "stocktwits_messages": 1,  # 可靠性最高
    "polymarket_odds": 2,
    "sec_filings": 2,
    "google_trends": 3,
    "seeking_alpha": 3,
    "twitter_sentiment": 4,
}

# ==================== 运行配置 ====================
RUNTIME_CONFIG = {
    "debug": True,
    "log_file": str(PATHS.logs_dir / "data_fetcher.log"),
    "max_retries": 3,
    "timeout": 10,  # 请求超时（秒）
    "rate_limit_delay": 1,  # 请求间延迟（秒）
}

# ==================== 催化剂日期 ====================
CATALYSTS = {
    # 科技
    "NVDA": [
        {
            "event": "Q4 FY2026 Earnings",
            "scheduled_date": "2026-03-15",
            "scheduled_time": "16:00",
            "time_zone": "US/Eastern",
        },
        {
            "event": "Computex 2026",
            "scheduled_date": "2026-05-28",
            "scheduled_time": "09:00",
            "time_zone": "Asia/Taipei",
        },
    ],
    "MSFT": [
        {
            "event": "Q3 FY2026 Earnings",
            "scheduled_date": "2026-04-23",
            "scheduled_time": "16:30",
            "time_zone": "US/Eastern",
        },
    ],
    "AMD": [
        {
            "event": "Q1 2026 Earnings",
            "scheduled_date": "2026-05-07",
            "scheduled_time": "17:00",
            "time_zone": "US/Eastern",
        },
    ],
    "QCOM": [
        {
            "event": "Q2 FY2026 Earnings",
            "scheduled_date": "2026-04-29",
            "scheduled_time": "16:45",
            "time_zone": "US/Eastern",
        },
    ],

    # 生物医药
    "VKTX": [
        {
            "event": "Phase 3 Trial Results",
            "scheduled_date": "2026-08-15",
            "scheduled_time": "08:30",
            "time_zone": "US/Eastern",
        },
    ],
    "AMGN": [
        {
            "event": "Q1 2026 Earnings",
            "scheduled_date": "2026-04-28",
            "scheduled_time": "16:30",
            "time_zone": "US/Eastern",
        },
    ],
    "BIIB": [
        {
            "event": "Q1 2026 Earnings",
            "scheduled_date": "2026-05-13",
            "scheduled_time": "16:00",
            "time_zone": "US/Eastern",
        },
    ],
    "JNJ": [
        {
            "event": "Q1 2026 Earnings",
            "scheduled_date": "2026-04-14",
            "scheduled_time": "07:00",
            "time_zone": "US/Eastern",
        },
    ],
    "REGN": [
        {
            "event": "Q1 2026 Earnings",
            "scheduled_date": "2026-05-06",
            "scheduled_time": "08:00",
            "time_zone": "US/Eastern",
        },
    ],

    # 清洁能源
    "PLUG": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-03-10",
            "scheduled_time": "17:00",
            "time_zone": "US/Eastern",
        },
    ],
    "RUN": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-02-24",
            "scheduled_time": "17:00",
            "time_zone": "US/Eastern",
        },
    ],
    "NEE": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-02-25",
            "scheduled_time": "08:00",
            "time_zone": "US/Eastern",
        },
    ],
    "ENPH": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-02-26",
            "scheduled_time": "16:30",
            "time_zone": "US/Eastern",
        },
    ],

    # FinTech
    "SQ": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-03-17",
            "scheduled_time": "17:00",
            "time_zone": "US/Eastern",
        },
    ],
    "COIN": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-03-04",
            "scheduled_time": "17:00",
            "time_zone": "US/Eastern",
        },
    ],
    "MSTR": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-02-26",
            "scheduled_time": "17:00",
            "time_zone": "US/Eastern",
        },
    ],

    # AI
    "UPST": [
        {
            "event": "Q4 2025 Earnings",
            "scheduled_date": "2026-02-24",
            "scheduled_time": "16:30",
            "time_zone": "US/Eastern",
        },
    ],
    "TSLA": [
        {
            "event": "Q1 2026 Earnings",
            "scheduled_date": "2026-04-22",
            "scheduled_time": "16:00",
            "time_zone": "US/Pacific",
        },
    ],
}

# ==================== 评分权重（5维评估）====================
# 键名必须与 QueenDistiller.DEFAULT_WEIGHTS 一致（risk_adj 不是 risk_adjustment）
# OracleBeeEcho 的期权信号已归入 odds 维度，无需单独 options 维度
EVALUATION_WEIGHTS = {
    "signal":    0.30,   # ScoutBeeNova: SEC 披露 + 聪明钱 + 拥挤度
    "catalyst":  0.20,   # ChronosBeeHorizon: 催化剂与时间线
    "sentiment": 0.20,   # BuzzBeeWhisper: 情绪与叙事
    "odds":      0.15,   # OracleBeeEcho: 期权 IV + 市场赔率
    "risk_adj":  0.15,   # GuardBeeSentinel: 交叉验证 + 风险调整
}

# ==================== 期权评分阈值 ====================
OPTIONS_SCORE_THRESHOLDS = {
    "iv_rank_neutral_min": 30,      # IV Rank < 30 视为低 IV
    "iv_rank_neutral_max": 70,      # IV Rank > 70 视为高 IV
    "put_call_bullish": 0.7,        # P/C < 0.7 看多信号强
    "put_call_bearish": 1.5,        # P/C > 1.5 看空信号强
    "unusual_volume_ratio": 5,      # volume/OI > 5 视为异动
    "options_score_threshold": 6.0, # 期权综合评分 >= 6.0 为正信号
}

# ==================== yFinance 期权数据源 ====================
# 使用 yfinance 库获取期权数据（免费、无需 API Token）
YFINANCE_OPTIONS_CONFIG = {
    "enabled": True,
    "cache_ttl": 300,  # 5 分钟缓存
    "description": "Yahoo Finance 期权数据（通过 yfinance 库）"
}

# ==================== 拥挤度权重 ====================
CROWDING_WEIGHTS = {
    "stocktwits_volume": 0.25,
    "google_trends": 0.15,
    "consensus_strength": 0.25,
    "polymarket_volatility": 0.15,
    "seeking_alpha_views": 0.10,
    "short_squeeze_risk": 0.10,
}

# ==================== 失效条件阈值 ====================
THESIS_BREAK_THRESHOLDS = {
    "revenue_decline_pct": 5,
    "eps_miss_pct": 20,
    "polymarket_probability": 60,
    "crowding_score": 75,
}

# ==================== 初始化缓存目录 ====================
def init_cache():
    """初始化缓存目录"""
    cache_dir = CACHE_CONFIG["cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(RUNTIME_CONFIG["log_file"]), exist_ok=True)

# ==================== 告警配置 (Phase 2) ====================
ALERT_CONFIG = {
    # Slack 通知配置
    "slack_enabled": True,  # ✅ 已启用 Slack 通知
    "slack_webhook": None,   # Webhook URL 从 ~/.alpha_hive_slack_webhook 文件读取

    # 邮件通知配置 - Gmail API
    "email_enabled": True,  # 改为 True 后启用邮件通知
    "email_provider": "gmail_api",  # 使用 Gmail API 而不是 SMTP
    "email_config": {
        "sender_email": os.environ.get("ALPHA_HIVE_EMAIL_SENDER", ""),
        "recipient_emails": [e.strip() for e in os.environ.get("ALPHA_HIVE_EMAIL_RECIPIENTS", "").split(",") if e.strip()],
        "credentials_file": PATHS.google_credentials
    },

    # 告警阈值
    "performance_baseline_seconds": 5.0,  # 性能基线
    "performance_degradation_threshold": 1.5,  # 150% = 高于基线 50% 触发告警

    # 告警规则
    "alert_rules": {
        "enable_critical_alerts": True,      # P0: 系统失败
        "enable_high_alerts": True,          # P1: 步骤失败、性能异常
        "enable_medium_alerts": True,        # P2: 低分报告
        "low_score_threshold": 6.0,          # 低于此分数触发告警
        "no_report_alert": True,             # 无报告生成时告警
        "deployment_failure_alert": True,    # GitHub 部署失败时告警
    },

    # 告警输出
    "save_alerts_json": True,  # 保存告警到 JSON 文件
    "alerts_log_dir": str(PATHS.logs_dir),
}

# ==================== 性能监控配置 (Phase 2) ====================
METRICS_CONFIG = {
    "enabled": True,
    "db_path": str(PATHS.home / "metrics.db"),
    "retention_days": 90,  # 保留 90 天数据
    "collect_metrics": {
        "execution_time": True,
        "memory_usage": True,
        "file_sizes": True,
        "report_quality": True,
        "deployment_status": True,
    }
}

# ==================== 信息素板持久化配置 (Phase 2) ====================
PHEROMONE_CONFIG = {
    "enabled": True,
    "db_path": PATHS.db,
    "retention_days": 30,  # 保留 30 天信息素数据
    "decay_rate": 0.1,     # 每日衰减 10%
    "accuracy_tracking": {
        "enable_t1_tracking": True,      # T+1 准确率回看
        "enable_t7_tracking": True,      # T+7 准确率回看
        "enable_t30_tracking": True,     # T+30 准确率回看
    }
}

# ==================== 动态蜂群配置 (Phase 2) ====================
SWARM_CONFIG = {
    "enabled": True,
    "adaptive_spawning": {
        "base_agents": 10,
        "min_agents": 8,
        "max_agents": 100,
        "complexity_factors": {
            "us_market": 1.0,
            "hk_market": 1.2,
            "cn_market": 1.5,
            "crypto": 1.8,
        }
    },
    "system_monitoring": {
        "cpu_threshold": 80,     # CPU 使用率超过 80% 时缩减 agent
        "memory_threshold": 85,  # 内存使用率超过 85% 时缩减 agent
    }
}

# ==================== 持久化记忆配置 (Phase 2) ====================
MEMORY_CONFIG = {
    "enabled": True,
    "db_path": PATHS.db,
    "agent_memory": {
        "retention_days": 90,  # 保留 90 天历史记忆
        "max_similar_results": 5,  # 检索时返回最多 5 条相似记忆
    },
    "retriever": {
        "cache_ttl_seconds": 300,  # 检索缓存 5 分钟
        "min_similarity": 0.1,  # 相似度最低阈值
        "top_k": 5,  # 默认返回 top 5
    },
    "weight_manager": {
        "min_weight": 0.3,  # 权重下限
        "max_weight": 3.0,  # 权重上限
        "min_samples_for_dynamic": 10,  # 样本不足时保持平等权重
        "accuracy_weight": 2.0,  # 准确率对权重的影响系数
    },
    "session_tracking": {
        "enable_session_save": True,  # 自动保存会话聚合
        "async_io": True,  # 后台异步写入 DB
    }
}

# ==================== Google Calendar 配置 (Phase 3 P2) ====================
CALENDAR_CONFIG = {
    "enabled": True,
    "credentials_file": PATHS.google_credentials,
    "token_file": PATHS.calendar_token,
    "calendar_id": "primary",
    "sync_catalysts_on_startup": True,   # 每次日报运行时同步 CATALYSTS
    "add_opportunity_reminders": True,   # 高分机会自动添加提醒
    "opportunity_score_threshold": 7.5,  # 触发提醒的分数阈值
    "reminder_advance_minutes": 30,      # 事件前多少分钟提醒
    "upcoming_days_context": 7,          # 注入 Agent 的未来几天事件
}

# ==================== 向量记忆配置 (Phase 3 内存优化) ====================
VECTOR_MEMORY_CONFIG = {
    "enabled": True,
    "db_path": PATHS.chroma_db,
    "retention_days": 90,          # 长期记忆保留 90 天
    "short_term_window": 20,       # 短期记忆：PheromoneBoard 最多 20 条
    "max_context_chars": 200,      # Agent 注入上下文最大字符数
    "max_cache_tickers": 50,       # LRU 缓存最多 50 个 ticker
    "cleanup_on_startup": True,    # 启动时自动清理过期记忆
}

# ==================== 代码执行配置 (Phase 3 P1) ====================
CODE_EXECUTION_CONFIG = {
    "enabled": True,
    "max_timeout": 30,           # 单次执行超时（秒）
    "max_retries": 3,            # 自动调试最大重试次数
    "sandbox_dir": str(PATHS.sandbox_dir),
    "enable_network": False,     # 禁止网络访问
    "enable_file_write": True,   # 允许写入沙箱目录
    "add_to_swarm": True,        # 是否将 CodeExecutorAgent 加入蜂群
}

# ==================== CrewAI 多 Agent 配置 (Phase 3 P5) ====================
CREWAI_CONFIG = {
    "enabled": True,  # CrewAI 框架启用（需先 pip install crewai）
    "process_type": "hierarchical",  # hierarchical 或 sequential
    "manager_verbose": True,
    "timeout_seconds": 300,  # 单个分析超时
}

# ==================== 财报自动监控配置 ====================
EARNINGS_WATCHER_CONFIG = {
    "enabled": True,
    "auto_update_report": True,       # 财报发布后自动更新当日简报
    "check_times_et": ["07:00", "17:30", "19:00"],  # ET 时间检查点
    "data_source": "yfinance",        # 主数据源
    "cache_ttl_results": 1800,        # 财报结果缓存 30 分钟
    "cache_ttl_dates": 43200,         # 财报日期缓存 12 小时
    "slack_notify_on_update": True,   # 更新后发送 Slack 通知
}

# ==================== LLM 智能层配置 (Phase 1) ====================
LLM_CONFIG = {
    "enabled": True,                    # 总开关（False = 完全规则引擎模式）
    "model": "claude-haiku-4-5-20251001",  # 默认模型（最低成本）
    "max_tokens_distill": 512,          # QueenDistiller 蒸馏 max_tokens
    "max_tokens_news": 256,             # 新闻情绪分析 max_tokens
    "temperature": 0.3,                 # 推理温度
    "score_blend_ratio": 0.6,           # 规则引擎 vs LLM 混合比：0.6 = 规则 60% + LLM 40%
    "daily_budget_usd": 1.0,            # 每日 token 预算上限（美元）
    "api_key_file": "~/.anthropic_api_key",  # API Key 文件路径
    # 降级策略
    "fallback_on_error": True,          # API 失败时降级到规则引擎
    "fallback_on_budget": True,         # 超预算时降级到规则引擎
}

if __name__ == "__main__":
    init_cache()
    _log.info("配置已加载 | 标的 %d | 催化剂 %d | HOME=%s",
              len(WATCHLIST), sum(len(v) for v in CATALYSTS.values()), PATHS.home)
