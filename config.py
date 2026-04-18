"""
🐝 Alpha Hive - 配置管理
存储 API 密钥、数据源配置、缓存策略

支持热更新：将 watchlist_override.yaml 放在项目目录下，
调用 reload_config() 即可热加载 WATCHLIST/CATALYSTS，无需重启。
"""

import os
import threading

from hive_logger import PATHS, get_logger

_log = get_logger("config")


# ==================== 环境变量分层辅助函数 ====================

def _env_int(name: str, default: int) -> int:
    """读取环境变量为 int；缺失或非法时返回 *default*。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        _log.warning("env %s=%r 非有效 int，使用默认值 %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    """读取环境变量为 float；缺失或非法时返回 *default*。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        _log.warning("env %s=%r 非有效 float，使用默认值 %.2f", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读取环境变量为 bool（1/true/yes/on → True）；缺失时返回 *default*。"""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


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
            "User-Agent": "AlphaHive igg wangmingjie36@gmail.com"
        }
    },

    # Yahoo Finance（期权数据通过 yfinance 库获取）
    "YAHOO_FINANCE_OPTIONS": {
        "enabled": True,
        "description": "使用 yfinance 库获取期权数据（免费、无需 API Token）"
    },
}

# ==================== 服务配置（避免硬编码散落各模块） ====================

# Slack 频道 ID（#alpha-hive）
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0AGUUWJXJS")
# Slack 用户 DM 降级目标（当 bot 不在频道时自动 DM）
SLACK_DM_FALLBACK = os.environ.get("SLACK_DM_FALLBACK", "U0AGQK74NKV")

# SEC EDGAR 要求的 User-Agent（SEC 政策要求包含联系方式）
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "AlphaHive research@alphahive.dev"
)

# ==================== 集中 Key/Secret 管理 ====================

# 注册表：env_var → 降级文件路径
_SECRET_REGISTRY = {
    "ANTHROPIC_API_KEY": "~/.anthropic_api_key",
    "SLACK_WEBHOOK_URL": "~/.alpha_hive_slack_webhook",
    "SLACK_BOT_TOKEN": "~/.alpha_hive_slack_bot_token",
    "SLACK_USER_TOKEN": "~/.alpha_hive_slack_user_token",
    "FRED_API_KEY": "~/.alpha_hive_fred_key",
    "AV_API_KEY": "~/.alpha_hive_av_key",
    "ALPHA_VANTAGE_KEY": None,
    "STOCKTWITS_TOKEN": None,
    "GMAIL_APP_PASSWORD": None,
    "TRADIER_API_KEY": "~/.alpha_hive_tradier_key",
}


def get_secret(name: str) -> str:
    """
    集中获取 API key / secret（优先环境变量 → 降级文件）

    Args:
        name: 注册表中的 key 名称（如 "ANTHROPIC_API_KEY"）
    Returns:
        key 值，未找到返回空字符串
    """
    import os as _os
    # 1. 环境变量优先
    val = _os.environ.get(name, "").strip()
    if val:
        return val
    # 2. 降级到文件
    file_path = _SECRET_REGISTRY.get(name)
    if not file_path:
        return ""
    expanded = _os.path.realpath(_os.path.expanduser(file_path))
    try:
        st = _os.stat(expanded)
        if st.st_mode & 0o077:
            _log.warning("Secret file %s has insecure permissions, skipping", expanded)
            return ""
        if st.st_size > 10240:
            _log.warning("Secret file %s too large (%d bytes), skipping", expanded, st.st_size)
            return ""
        with open(expanded) as f:
            return f.read(10240).strip()
    except (OSError, UnicodeDecodeError):
        return ""


# ==================== 缓存配置 ====================
CACHE_CONFIG = {
    "enabled": True,
    "cache_dir": str(PATHS.cache_dir),
    "ttl": {  # 缓存过期时间（秒）— 所有模块从此处读取，避免硬编码
        # 高频数据源（5~15 分钟）
        "stocktwits": 300,        # 5 分钟
        "polymarket": 900,        # 15 分钟
        "polymarket_macro": 1800, # 30 分钟
        "yahoo_finance": 300,     # 5 分钟
        "yahoo_trending": 900,    # 15 分钟
        "unusual_options": 300,   # 5 分钟
        "reddit": 600,            # 10 分钟（磁盘）
        "reddit_memory": 300,     # 5 分钟（内存）
        "finviz": 900,            # 15 分钟
        "edgar_rss": 900,         # 15 分钟
        # 中频数据源（1~24 小时）
        "stocktwits_legacy": 3600,  # 1 小时（data_fetcher 旧路径）
        "google_trends": 86400,   # 24 小时
        "seeking_alpha": 86400,   # 24 小时
        "sec_cik": 86400,         # 24 小时
        "fred_macro": 1800,       # 30 分钟
        "earnings_date": 43200,   # 12 小时
        # 低频数据源（7 天+）
        "sec_edgar": 604800,      # 7 天
        "earnings_results": 1800,    # 30 分钟
    }
}

def get_cache_ttl(source: str) -> int:
    """获取缓存 TTL（交易时段感知：非交易时段延长高频源 TTL）"""
    base_ttl = CACHE_CONFIG["ttl"].get(source, 300)
    # 高频源在非交易时段无需频繁刷新
    _HIGH_FREQ_SOURCES = {"stocktwits", "yahoo_finance", "unusual_options", "reddit_memory", "finviz", "edgar_rss"}
    if source not in _HIGH_FREQ_SOURCES:
        return base_ttl
    try:
        from datetime import datetime as _dt, timezone, timedelta as _td, time as _dtime
        _utc = _dt.now(timezone.utc)
        _et = _utc + _td(hours=-4 if 3 <= _utc.month <= 11 else -5)
        _market_open = _et.weekday() < 5 and _dtime(9, 30) <= _et.time() < _dtime(16, 0)
        if not _market_open:
            return max(base_ttl, 3600)  # 非交易时段至少 1 小时
    except (ImportError, ValueError):
        pass
    return base_ttl


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
        "sector": "FinTech",
        "polymarket_slug": "circle-ipo",
        "monitor_events": ["ipo", "earnings", "usdc_growth", "regulatory", "crypto_policy"],
    },
}

# ══════════════════════════════════════════════════════════════════════════════
# v0.22.0: 扩展池（S&P 500 高流动性 + 期权活跃核心 ~75 只）
# 方向1a：扩 watchlist 以增加样本量（原 25 只 → 100 只）
# 用法：扫描脚本按需合并 WATCHLIST + WATCHLIST_EXTENDED（CLI --all-watchlist 或 --extended-pool）
# ══════════════════════════════════════════════════════════════════════════════
WATCHLIST_EXTENDED = {
    # ── 大型科技（Mega-cap Tech）──
    "AAPL": {"name": "Apple Inc", "sector": "Technology", "monitor_events": ["earnings", "product_launch"]},
    "GOOGL": {"name": "Alphabet Inc", "sector": "Technology", "monitor_events": ["earnings", "ai_product", "antitrust"]},
    "AVGO": {"name": "Broadcom Inc", "sector": "Technology", "monitor_events": ["earnings", "vmware_integration"]},
    "ORCL": {"name": "Oracle Corp", "sector": "Technology", "monitor_events": ["earnings", "cloud_growth"]},
    "CRM": {"name": "Salesforce Inc", "sector": "Technology", "monitor_events": ["earnings", "ai_agentforce"]},
    "ADBE": {"name": "Adobe Inc", "sector": "Technology", "monitor_events": ["earnings", "creative_cloud"]},
    "NOW": {"name": "ServiceNow Inc", "sector": "Technology", "monitor_events": ["earnings"]},
    "PANW": {"name": "Palo Alto Networks", "sector": "Technology", "monitor_events": ["earnings", "security_platform"]},
    "CRWD": {"name": "CrowdStrike Holdings", "sector": "Technology", "monitor_events": ["earnings", "incident_response"]},
    "SNOW": {"name": "Snowflake Inc", "sector": "Technology", "monitor_events": ["earnings"]},
    "INTC": {"name": "Intel Corp", "sector": "Technology", "monitor_events": ["earnings", "foundry"]},
    "MU": {"name": "Micron Technology", "sector": "Technology", "monitor_events": ["earnings", "hbm_shipments"]},
    "SMCI": {"name": "Super Micro Computer", "sector": "Technology", "monitor_events": ["earnings", "ai_server"]},
    "ARM": {"name": "Arm Holdings", "sector": "Technology", "monitor_events": ["earnings", "ai_licensing"]},
    "DELL": {"name": "Dell Technologies", "sector": "Technology", "monitor_events": ["earnings", "ai_server"]},
    "HPE": {"name": "Hewlett Packard Enterprise", "sector": "Technology", "monitor_events": ["earnings"]},
    "ASML": {"name": "ASML Holding", "sector": "Technology", "monitor_events": ["earnings", "euv_orders"]},
    "TSM": {"name": "Taiwan Semiconductor", "sector": "Technology", "monitor_events": ["earnings", "monthly_revenue"]},
    # ── 消费（Consumer）──
    "COST": {"name": "Costco Wholesale", "sector": "Consumer", "monitor_events": ["monthly_sales", "earnings"]},
    "WMT": {"name": "Walmart Inc", "sector": "Consumer", "monitor_events": ["earnings", "comp_sales"]},
    "HD": {"name": "Home Depot", "sector": "Consumer", "monitor_events": ["earnings", "housing_starts"]},
    "NKE": {"name": "Nike Inc", "sector": "Consumer", "monitor_events": ["earnings", "china_sales"]},
    "MCD": {"name": "McDonald's Corp", "sector": "Consumer", "monitor_events": ["earnings", "same_store_sales"]},
    "SBUX": {"name": "Starbucks Corp", "sector": "Consumer", "monitor_events": ["earnings", "china_traffic"]},
    "LULU": {"name": "Lululemon Athletica", "sector": "Consumer", "monitor_events": ["earnings", "international_growth"]},
    # ── 金融（Financials）──
    "JPM": {"name": "JPMorgan Chase", "sector": "Financials", "monitor_events": ["earnings", "nii_guidance"]},
    "BAC": {"name": "Bank of America", "sector": "Financials", "monitor_events": ["earnings", "nii"]},
    "GS": {"name": "Goldman Sachs", "sector": "Financials", "monitor_events": ["earnings", "trading_revenue"]},
    "MS": {"name": "Morgan Stanley", "sector": "Financials", "monitor_events": ["earnings", "wealth_mgmt"]},
    "V": {"name": "Visa Inc", "sector": "Financials", "monitor_events": ["earnings", "payment_volume"]},
    "MA": {"name": "Mastercard Inc", "sector": "Financials", "monitor_events": ["earnings", "cross_border"]},
    "BRK-B": {"name": "Berkshire Hathaway", "sector": "Financials", "monitor_events": ["13f", "annual_meeting"]},
    # ── 医药（Healthcare - 补充）──
    "LLY": {"name": "Eli Lilly", "sector": "Healthcare", "monitor_events": ["earnings", "glp1_sales"]},
    "NVO": {"name": "Novo Nordisk", "sector": "Healthcare", "monitor_events": ["earnings", "wegovy_demand"]},
    "UNH": {"name": "UnitedHealth Group", "sector": "Healthcare", "monitor_events": ["earnings", "mlr"]},
    "ABBV": {"name": "AbbVie Inc", "sector": "Healthcare", "monitor_events": ["earnings", "skyrizi_rinvoq"]},
    "PFE": {"name": "Pfizer Inc", "sector": "Healthcare", "monitor_events": ["earnings", "pipeline"]},
    "MRK": {"name": "Merck & Co", "sector": "Healthcare", "monitor_events": ["earnings", "keytruda"]},
    "TMO": {"name": "Thermo Fisher", "sector": "Healthcare", "monitor_events": ["earnings"]},
    "ISRG": {"name": "Intuitive Surgical", "sector": "Healthcare", "monitor_events": ["earnings", "procedure_volumes"]},
    # ── 能源 / 工业 ──
    "XOM": {"name": "Exxon Mobil", "sector": "Energy", "monitor_events": ["earnings", "oil_prices"]},
    "CVX": {"name": "Chevron Corp", "sector": "Energy", "monitor_events": ["earnings", "permian_output"]},
    "CAT": {"name": "Caterpillar Inc", "sector": "Industrials", "monitor_events": ["earnings", "dealer_inventory"]},
    "BA": {"name": "Boeing Co", "sector": "Industrials", "monitor_events": ["earnings", "737max_deliveries", "defense"]},
    "GE": {"name": "GE Aerospace", "sector": "Industrials", "monitor_events": ["earnings", "services_orders"]},
    "DE": {"name": "Deere & Co", "sector": "Industrials", "monitor_events": ["earnings", "ag_equipment"]},
    # ── 通讯 / 媒体 ──
    "NFLX": {"name": "Netflix Inc", "sector": "Communication", "monitor_events": ["earnings", "subscriber_adds", "ad_tier"]},
    "DIS": {"name": "Walt Disney Co", "sector": "Communication", "monitor_events": ["earnings", "parks", "dtc_profitability"]},
    "T": {"name": "AT&T Inc", "sector": "Communication", "monitor_events": ["earnings", "postpaid_adds"]},
    "VZ": {"name": "Verizon Communications", "sector": "Communication", "monitor_events": ["earnings", "postpaid_net_adds"]},
    "CMCSA": {"name": "Comcast Corp", "sector": "Communication", "monitor_events": ["earnings", "broadband_subs"]},
    "TMUS": {"name": "T-Mobile US", "sector": "Communication", "monitor_events": ["earnings", "postpaid_net_adds"]},
    # ── 中概 / 其它高波动 ──
    "BABA": {"name": "Alibaba Group", "sector": "Technology", "monitor_events": ["earnings", "cloud_revenue"]},
    "PDD": {"name": "PDD Holdings", "sector": "Technology", "monitor_events": ["earnings", "temu_gmv"]},
    "JD": {"name": "JD.com Inc", "sector": "Technology", "monitor_events": ["earnings", "618_promotion"]},
    "NIO": {"name": "NIO Inc", "sector": "Automotive", "monitor_events": ["deliveries", "earnings"]},
    "XPEV": {"name": "XPeng Inc", "sector": "Automotive", "monitor_events": ["deliveries", "earnings"]},
    "LI": {"name": "Li Auto Inc", "sector": "Automotive", "monitor_events": ["deliveries", "earnings"]},
    # ── ETF 做 sector-hedge / regime 参考 ──
    "SPY": {"name": "SPDR S&P 500 ETF", "sector": "ETF", "monitor_events": ["macro", "fomc"]},
    "QQQ": {"name": "Invesco QQQ Trust", "sector": "ETF", "monitor_events": ["tech_earnings"]},
    "IWM": {"name": "iShares Russell 2000 ETF", "sector": "ETF", "monitor_events": ["small_cap_earnings"]},
    "SOXX": {"name": "iShares Semiconductor ETF", "sector": "ETF", "monitor_events": ["chip_earnings"]},
    "XLE": {"name": "Energy Select Sector SPDR", "sector": "ETF", "monitor_events": ["oil_prices"]},
    "XLF": {"name": "Financial Select Sector SPDR", "sector": "ETF", "monitor_events": ["bank_earnings"]},
    "XLV": {"name": "Health Care Select SPDR", "sector": "ETF", "monitor_events": ["healthcare_earnings"]},
    "XLK": {"name": "Technology Select Sector SPDR", "sector": "ETF", "monitor_events": ["tech_earnings"]},
    "VXX": {"name": "iPath VIX Short-Term Futures", "sector": "ETF", "monitor_events": ["vix_spike"]},
    # ── 高 beta / meme（波动信号用）──
    "GME": {"name": "GameStop Corp", "sector": "Consumer", "monitor_events": ["earnings", "retail_sentiment"]},
    "AMC": {"name": "AMC Entertainment", "sector": "Communication", "monitor_events": ["box_office", "earnings"]},
    "PLTR": {"name": "Palantir Technologies", "sector": "Technology", "monitor_events": ["earnings", "government_contracts"]},
    "SOFI": {"name": "SoFi Technologies", "sector": "Financials", "monitor_events": ["earnings", "member_adds"]},
    "HOOD": {"name": "Robinhood Markets", "sector": "Financials", "monitor_events": ["earnings", "monthly_active_users"]},
    "RIVN": {"name": "Rivian Automotive", "sector": "Automotive", "monitor_events": ["deliveries", "earnings"]},
    "LCID": {"name": "Lucid Group", "sector": "Automotive", "monitor_events": ["deliveries", "earnings"]},
    "DKNG": {"name": "DraftKings Inc", "sector": "Communication", "monitor_events": ["earnings", "sports_launches"]},
    "F": {"name": "Ford Motor Co", "sector": "Automotive", "monitor_events": ["earnings", "ev_losses"]},
    "GM": {"name": "General Motors", "sector": "Automotive", "monitor_events": ["earnings", "ev_output"]},
}


def get_extended_watchlist() -> dict:
    """返回合并后的 WATCHLIST（25 核心 + 扩展池 ~75）。
    调用方：`python3 alpha_hive_daily_report.py --extended-pool`

    v0.23.2 修复：语义明确为"WATCHLIST 优先"（核心标的的 catalyst 配置不被扩展池覆盖）。
    如需覆盖核心标的，直接改 WATCHLIST 而非往 WATCHLIST_EXTENDED 加同名。

    Sector 自动规范化：Fintech/fintech → FinTech；保持其它 sector 原值
    """
    _SECTOR_ALIAS = {"Fintech": "FinTech", "fintech": "FinTech"}
    merged = {}
    for tk, meta in WATCHLIST.items():
        m = dict(meta)
        sec = m.get("sector")
        if sec in _SECTOR_ALIAS:
            m["sector"] = _SECTOR_ALIAS[sec]
        merged[tk] = m
    # WATCHLIST_EXTENDED 只补不覆盖
    for tk, meta in WATCHLIST_EXTENDED.items():
        if tk not in merged:
            m = dict(meta)
            sec = m.get("sector")
            if sec in _SECTOR_ALIAS:
                m["sector"] = _SECTOR_ALIAS[sec]
            merged[tk] = m
    return merged


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
    "debug": _env_bool("ALPHA_HIVE_DEBUG", True),
    "log_file": str(PATHS.logs_dir / "data_fetcher.log"),
    "max_retries": _env_int("ALPHA_HIVE_MAX_RETRIES", 3),
    "timeout": 15,  # 请求超时（秒）— 全局统一 15s
    "rate_limit_delay": 1,  # 请求间延迟（秒）
}

# ==================== 网络请求统一配置 ====================
HTTP_TIMEOUT = _env_int("ALPHA_HIVE_HTTP_TIMEOUT", 15)  # 秒（全局默认）
HTTP_TIMEOUT_BY_SOURCE = {
    "sec_edgar": 15,
    "polymarket": 10,
    "yfinance": 12,
    "edgar_rss": 10,
    "default": 15,
}

def get_http_timeout(source: str = "default") -> int:
    """获取指定数据源的 HTTP 超时时间"""
    return HTTP_TIMEOUT_BY_SOURCE.get(source, HTTP_TIMEOUT)

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
# 此处是权重唯一入口；QueenDistiller.DEFAULT_WEIGHTS 是本配置的硬编码备份（ImportError 时使用）
# 注意键名：risk_adj（不是 risk_adjustment）
#
# 架构说明：
#   - 5 个主维度参与加权平均（权重和 = 1.0）
#   - Options 数据 → OracleBeeEcho 内部融合（55% options + 35% polymarket + 10% unusual）→ odds 维度
#   - ML 预测 → RivalBeeVanguard → dimension="ml_auxiliary" → 不参与主公式
#     作为独立调整项附加：final_score += (ml_score - 5.0) * 0.1 * ml_confidence（最大 ±0.5 分）
#   - 两者不存在双重计算
EVALUATION_WEIGHTS = {
    "signal":    0.1132,   # ScoutBeeNova: SEC 披露 + 聪明钱 + 拥挤度
    "catalyst":    0.3316,   # ChronosBeeHorizon: 催化剂与时间线
    "sentiment":    0.2764,   # BuzzBeeWhisper: 情绪与叙事
    "odds":    0.1657,   # OracleBeeEcho: 期权 IV(55%) + Polymarket(35%) + 异动(10%)
    "risk_adj":    0.1132,   # GuardBeeSentinel: 交叉验证 + 风险调整
    # ml_auxiliary: 不在此处（RivalBeeVanguard 作为 ±0.5 独立调整项）
}

# ML 概率上界（防止小样本过度自信，统一源，避免魔术数字散落各处）
ML_PROB_CAP = 0.95

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

# ==================== WATCHLIST 验证 ====================
def validate_watchlist():
    """启动时验证 WATCHLIST 与 CATALYSTS 结构一致性，返回警告列表"""
    import re
    warnings = []
    _required_fields = {"name", "sector", "monitor_events"}

    for ticker, cfg in WATCHLIST.items():
        # ticker 格式：1~5 位大写字母
        if not re.match(r'^[A-Z]{1,5}$', ticker):
            warnings.append(f"WATCHLIST ticker 格式异常: {ticker!r}（需 1~5 位大写字母）")

        # 必填字段检查
        missing = _required_fields - set(cfg.keys())
        if missing:
            warnings.append(f"WATCHLIST[{ticker}] 缺少必填字段: {missing}")

        # monitor_events 必须为非空列表
        evts = cfg.get("monitor_events")
        if not isinstance(evts, list) or len(evts) == 0:
            warnings.append(f"WATCHLIST[{ticker}].monitor_events 为空或非列表")

    # CATALYSTS 中有但 WATCHLIST 中没有的 ticker
    orphan_catalysts = set(CATALYSTS.keys()) - set(WATCHLIST.keys())
    if orphan_catalysts:
        warnings.append(f"CATALYSTS 中有 {orphan_catalysts} 不在 WATCHLIST 中")

    # WATCHLIST 中没有 CATALYSTS 配置的 ticker（仅 info 级别）
    missing_catalysts = set(WATCHLIST.keys()) - set(CATALYSTS.keys())
    if missing_catalysts:
        _log.info("以下 ticker 尚无 CATALYSTS 配置（不影响运行）: %s", sorted(missing_catalysts))

    for w in warnings:
        _log.warning("[CONFIG] %s", w)
    return warnings


# ==================== 权重验证 ====================

def _validate_weight_sum(name: str, weights: dict, target: float = 1.0, tol: float = 0.01) -> list:
    """验证权重 dict 的和是否等于 *target*（容差 *tol*）"""
    total = sum(weights.values())
    if abs(total - target) > tol:
        return [f"{name} 权重和 {total:.4f} != {target} (容差 {tol})"]
    return []


def validate_weights() -> list:
    """验证所有须归一的权重 dict。返回警告列表（空 = 全部通过）。"""
    warnings: list = []
    warnings += _validate_weight_sum("EVALUATION_WEIGHTS", EVALUATION_WEIGHTS)
    warnings += _validate_weight_sum("CROWDING_WEIGHTS", CROWDING_WEIGHTS)
    warnings += _validate_weight_sum("AGENT_SCORING.buzz_weights",
                                     AGENT_SCORING.get("buzz_weights", {}))
    oracle_w = {
        "options": AGENT_SCORING.get("oracle_options_weight", 0),
        "poly": AGENT_SCORING.get("oracle_poly_weight", 0),
        "unusual": AGENT_SCORING.get("oracle_unusual_weight", 0),
    }
    warnings += _validate_weight_sum("AGENT_SCORING.oracle_weights", oracle_w)
    for w in warnings:
        _log.warning("[CONFIG] %s", w)
    return warnings


# ==================== 诊断摘要 ====================

def get_config_summary() -> dict:
    """返回当前配置诊断摘要（用于日志/调试，自动过滤敏感 key）。"""
    return {
        "watchlist_count": len(WATCHLIST),
        "watchlist_tickers": sorted(WATCHLIST.keys()),
        "catalysts_count": sum(len(v) for v in CATALYSTS.values()),
        "http_timeout": HTTP_TIMEOUT,
        "debug": RUNTIME_CONFIG["debug"],
        "max_retries": RUNTIME_CONFIG["max_retries"],
        "llm_enabled": LLM_CONFIG["enabled"],
        "llm_model": LLM_CONFIG["model"],
        "llm_budget_usd": LLM_CONFIG["daily_budget_usd"],
        "evaluation_weights": dict(EVALUATION_WEIGHTS),
        "weight_validation": validate_weights(),
        "env_overrides": {
            k: v for k, v in os.environ.items()
            if k.startswith("ALPHA_HIVE_")
            and "KEY" not in k and "TOKEN" not in k and "SECRET" not in k
        },
    }


# ==================== 初始化缓存目录 ====================
def init_cache():
    """初始化缓存目录 + 验证 WATCHLIST + 验证权重"""
    cache_dir = CACHE_CONFIG["cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(os.path.dirname(RUNTIME_CONFIG["log_file"]), exist_ok=True)
    validate_watchlist()
    validate_weights()

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
    "retention_days": _env_int("ALPHA_HIVE_PHEROMONE_RETENTION_DAYS", 30),
    "decay_rate": _env_float("ALPHA_HIVE_PHEROMONE_DECAY_RATE", 0.1),
    "accuracy_tracking": {
        "enable_t1_tracking": True,      # T+1 准确率回看
        "enable_t7_tracking": True,      # T+7 准确率回看
        "enable_t30_tracking": True,     # T+30 准确率回看
    },
    # ── 内存板衰减调优 ──
    "board_decay_rates": {
        "fresh_minutes": 5,       # 小于此分钟数使用 fresh_decay
        "fresh_decay": 0.05,      # <5min 条目每次 publish 衰减量
        "medium_decay": 0.1,      # 5-30min 条目衰减量
        "old_decay": 0.15,        # >30min 条目衰减量
    },
    "board_ticker_scoped_decay": True,  # 仅衰减同 ticker 条目（防止跨 ticker 误杀）
}

# ==================== T+N 实际价格回填配置 (Phase 6 闭环) ====================
OUTCOMES_CONFIG = {
    "enabled": True,
    "rate_limit_seconds": 0.5,        # yfinance 请求间隔（秒）
    "max_snapshots_per_run": 50,      # 单次最多处理快照数
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
    # 回测提醒 (Phase 3 P2+)
    "add_feedback_reminders": True,       # T+1/T+7/T+30 回测提醒
    # 经济日历同步
    "sync_economic_calendar": True,       # 宏观事件同步到 Calendar
    "economic_calendar_days_ahead": 60,   # 前瞻天数
    # Thesis Break 紧急提醒
    "thesis_break_calendar_alerts": True, # Thesis Break 触发时创建日历事件
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
    "enable_network": True,      # BUG FIX: CodeExecutorAgent 需要网络才能抓取 yfinance 数据，原 False 导致永久 ConnectionError
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
    "score_blend_ratio": 0.5,           # 规则引擎 vs LLM 混合比：0.5 = 规则 50% + LLM 50%
    "daily_budget_usd": _env_float("ALPHA_HIVE_LLM_BUDGET_USD", 1.0),
    "api_key_file": "~/.anthropic_api_key",  # API Key 文件路径
    # 降级策略
    "fallback_on_error": True,          # API 失败时降级到规则引擎
    "fallback_on_budget": True,         # 超预算时降级到规则引擎
}

# ==================== Agent 评分阈值（提取自 swarm_agents.py）====================
AGENT_SCORING = {
    # ── 缓存 TTL ──
    "yfinance_cache_ttl": 120,           # yfinance 数据缓存（秒）
    "ticker_validity_ttl": 3600,         # ticker 有效性检查缓存（秒）
    "penny_stock_threshold": 0.10,       # 极低价告警阈值（美元）

    # ── ScoutBeeNova 权重 ──
    "scout_insider_weight": 0.6,         # 内幕交易评分权重
    "scout_crowding_weight": 0.4,        # 拥挤度评分权重
    "scout_rss_boost": 0.5,              # RSS 新鲜 Form4 每份加分
    "scout_min_insider_records": 2,      # insider 记录最少样本量

    # ── OracleBeeEcho 权重 ──
    "oracle_options_weight": 0.55,       # 期权信号权重
    "oracle_poly_weight": 0.35,          # Polymarket 信号权重
    "oracle_unusual_weight": 0.10,       # 异常流向权重

    # ── BuzzBeeWhisper 7 通道权重 ──
    "buzz_weights": {
        "momentum": 0.20,
        "volume": 0.10,
        "volatility": 0.05,
        "reddit": 0.25,
        "news": 0.25,
        "yahoo": 0.05,
        "fear_greed": 0.10,
    },

    # ── BuzzBeeWhisper 信号阈值 ──
    "volume_thresholds": {
        "very_high": 2.0,    # > 2.0x → 80
        "high": 1.5,         # > 1.5x → 65
        "normal": 1.0,       # > 1.0x → 50
        "low": 0.5,          # > 0.5x → 35
    },
    "volatility_thresholds": {
        "extreme": 60,       # > 60% → 恐慌
        "high": 40,          # > 40% → 紧张
        "moderate": 20,      # > 20% → 正常
    },

    # ── 方向判断阈值 ──
    "direction_bullish_min": 60,         # sentiment_composite > 60 → bullish
    "direction_bearish_max": 45,         # sentiment_composite < 45 → bearish（原 40）

    # ── 拥挤度阈值 ──
    "crowding_high": 70,                 # 拥挤度 > 70 → bearish
    "crowding_low": 30,                  # 拥挤度 < 30 → bullish
    "crowding_sell_neutral": 35,         # 卖出但拥挤度 < 35 → 计划性减持（neutral）（原 50）

    # ── RivalBee 动量方向阈值 ──
    "rival_bearish_momentum": -1.5,      # mom < -1.5 → bearish（原 -2.0 硬编码）
    "rival_bullish_momentum": 2.0,       # mom > 2.0 → bullish（不变）

    # ── OracleBee 数值方向回退 ──
    "oracle_bearish_score_threshold": 4.0,  # score < 4.0 → bearish（新增数值回退）
    "oracle_bullish_score_threshold": 6.5,  # score > 6.5 → bullish（新增数值回退）

    # ── 评分边界 ──
    "score_min": 1.0,
    "score_max": 10.0,
}

# ==================== v15.0 评分引擎升级配置 ====================

# 升级 2: 置信度幂次衰减（替代原 min(1.0, conf*2) 线性公式）
CONFIDENCE_WEIGHTING = {
    "exponent": 1.5,   # 幂次：越大惩罚越重（1.0=线性, 2.0=二次方）
    "floor": 0.3,      # 最低有效权重（防止完全忽略某维度）
}

# 升级 3: Fear & Greed 政体评分调整
FEAR_GREED_SCORING = {
    "extreme_fear": 25,          # F&G 值低于此 = 极度恐惧
    "extreme_greed": 75,         # F&G 值高于此 = 极度贪婪
    "fear_bearish_boost": 0.3,   # 恐惧 + 看空 → 加分
    "fear_bullish_penalty": 0.4, # 恐惧 + 看多 → 惩罚（别抄底）
    "greed_bullish_penalty": 0.3,# 贪婪 + 看多 → 惩罚（别追高）
    "greed_bearish_boost": 0.2,  # 贪婪 + 看空 → 加分（逆向机会）
}

# 升级 4: 看多不对称门槛（看多需要更强共识，因历史看多胜率仅 46.8%）
BULLISH_GATE_CONFIG = {
    "min_weight_pct": 0.50,          # 加权投票占比（原 0.40）
    "min_agents": 3,                  # 最少 Agent 数（原 2）
    "extreme_greed_threshold": 75,    # F&G > 75 时进一步加严
    "extreme_greed_weight_pct": 0.60, # 极度贪婪时看多需要 60% 票
}

# 升级 5: 历史胜率反馈折扣（低胜率标的分数压缩向中性）
TICKER_ACCURACY_FEEDBACK = {
    "enabled": True,
    "min_samples": 5,           # 需要 5+ 笔 T+7 验证才启用
    "discount_threshold": 0.50, # 胜率低于 50% 触发折扣
    "min_reliability": 0.5,     # 最低可靠性系数（BILI 33% → 0.66）
}

# 升级 6: 方向惯性平滑（减少无效翻转）
DIRECTION_STABILITY = {
    "enabled": True,
    "inertia_bonus": 0.1,      # 窄边际时昨日方向加权
    "narrow_margin": 0.15,     # 投票边际 < 15% 视为窄边际
}

# ==================== Sprint 1 (v16.0): 真实策略回测 ====================
# P0-1: 路径依赖退出（intraday 止损止盈）
TRADING_EXITS_CONFIG = {
    "enabled": True,
    "stop_loss_pct": 5.0,        # -5% 默认硬止损（大盘蓝筹）
    "take_profit_pct": 10.0,     # +10% 止盈（方向调整后）
    "slippage_on_exit_bps": 5,   # 触发后按当日开盘价+5bp滑点退出（模拟盘中成交）
    # 升级2: Per-ticker 自适应止损（高波动标的放宽，避免频繁被打出）
    "sl_overrides": {
        "NVDA": 5.0, "MSFT": 5.0, "AMZN": 5.0, "META": 5.0,   # 大盘蓝筹
        "TSLA": 7.0, "QCOM": 6.0,                                # 中等波动
        "BILI": 10.0, "RKLB": 10.0, "CRCL": 12.0, "VKTX": 12.0, # 高波动
    },
    # 中性方向宽松止损（防止 CRCL -30% 类灾难，不设止盈）
    "neutral_sl_pct": 15.0,
}

# P0-2: 交易成本 + 借券费模型
TRADING_COSTS_CONFIG = {
    "enabled": True,
    # 滑点（每边，bp = 0.01%），按标的覆盖；未知走 default
    "slippage_bps_default": 10,
    "slippage_bps_by_ticker": {
        "NVDA": 3, "MSFT": 3, "META": 4, "AMZN": 4, "TSLA": 5,
        "QCOM": 6, "BILI": 15, "VKTX": 18, "RKLB": 12, "CRCL": 25,
    },
    # 佣金（美元，按 IB 标准 ~$0.005/股 × 假设 100 股 = $0.5，折成 pct 按 $10000 仓位约 0.005%）
    "commission_pct_per_side": 0.01,   # 双边总成本（进+出），已按 0.5bp/边换算
    # 借券费（年化 %），仅看空承担
    "borrow_rate_default": 3.0,
    "borrow_rates": {
        # 大盘蓝筹极低
        "NVDA": 0.5, "MSFT": 0.25, "AMZN": 0.25, "META": 0.3, "TSLA": 1.0,
        "QCOM": 0.75,
        # 中盘波动
        "BILI": 4.0, "RKLB": 3.5, "CRCL": 8.0,
        # 生物科技/热门空头
        "VKTX": 15.0,
    },
    # 无风险利率（benchmark 超额用）
    "risk_free_rate_pct": 4.5,
}

# P0-3: 仓位 + 复利 Equity Curve
PORTFOLIO_CONFIG = {
    "initial_capital": 100000.0,    # 起始 $100k
    "position_size_pct": 0.10,      # 每笔 10% 名义本金
    "max_concurrent_positions": 10, # 最多同时 10 笔
    "benchmark_ticker": "SPY",      # 基准 = SPY 买入持有
}

# ==================== 情绪关键词（统一词库，newsapi + finviz 共用）====================
SENTIMENT_KEYWORDS = {
    "bullish": {
        "surge", "soar", "rally", "beat", "record", "upgrade", "buy", "growth",
        "profit", "expand", "win", "strong", "bullish", "upbeat", "exceeds",
        "outperform", "positive", "breakthrough", "gains", "rises", "jumped",
        "climbed", "boosted", "optimistic", "raised", "accelerates", "momentum",
        "breakout", "record high", "strong buy", "raise", "boost", "expansion",
        "top pick", "upside", "higher", "gain", "exceed", "blowout",
        "impressive", "robust", "beats", "surges", "soars", "rallies",
        "upgrades",
    },
    "bearish": {
        "drop", "fall", "miss", "downgrade", "sell", "loss", "weak", "decline",
        "cut", "warning", "layoff", "recession", "bearish", "disappoints",
        "underperform", "negative", "crash", "plunge", "fell", "tumbled",
        "slumped", "fears", "risk", "concern", "lowered", "slowdown", "probe",
        "selloff", "slash", "contraction", "headwind", "decelerate",
        "breakdown", "lawsuit", "investigation", "recall",
        "misses", "falls", "drops", "crashes", "downgrades", "cuts",
        "slashed", "pessimistic", "losses",
    },
}

# ==================== 噪声过滤配置 ====================
NEWS_FILTER_CONFIG = {
    "dedup_jaccard_threshold": 0.5,     # 标题 Jaccard 相似度 ≥ 0.5 视为重复
    "recency_half_life_hours": 24.0,    # 时效衰减半衰期（小时）
    "min_articles_for_recency": 3,      # 文章数 < 3 时不做时效衰减（样本太少）
}

# ==================== 情绪动量配置 ====================
SENTIMENT_MOMENTUM_CONFIG = {
    "surge_threshold": 15,              # 3d delta > 15 ppt = surging（情绪急升）
    "rise_threshold": 5,                # 3d delta > 5 ppt = rising（情绪上升）
    "crash_threshold": -15,             # 3d delta < -15 ppt = crashing（情绪急跌）
    "decline_threshold": -5,            # 3d delta < -5 ppt = declining（情绪下降）
    "divergence_bull_trap_sentiment": 65,   # 情绪 > 65% 且价格跌 → 看多陷阱
    "divergence_hidden_opp_sentiment": 35,  # 情绪 < 35% 且价格涨 → 隐藏机会
    "divergence_price_threshold": 3.0,      # 价格变动阈值 (%)
    # 跨标的情绪传染
    "sector_deviation_high": 15,            # 偏离板块 > 15 ppt = overheating/undervalued
    "sector_deviation_mid": 8,              # 偏离板块 > 8 ppt = above/below_sector
    # 冲突驱动增强
    "conflict_heavy_min_agents": 2,         # 多空双方各 ≥ 2 Agent = 重度冲突
    "conflict_dq_resolve_threshold": 0.55,  # DQ 加权占比 ≥ 55% 才能解决方向冲突
    "conflict_discount_factor": 0.3,        # 冲突折扣系数（× 冲突比例）
}

# ==================== 冲突仲裁配置 (QueenDistiller Enhancement A) ====================
CONFLICT_ARBITRATION_CONFIG = {
    "close_vote_threshold": 0.15,   # margin 低于此值触发仲裁
    "dissent_boost": 1.5,           # GuardBee/BearBee 异议权重倍数
    "dissent_agents": ["GuardBeeSentinel", "BearBeeContrarian"],
}

# ==================== 置信度校准配置 (QueenDistiller Enhancement B) ====================
CONFIDENCE_CALIBRATION_CONFIG = {
    "std_multiplier": 0.3,          # 维度 std → band_width 的系数
    "low_coverage_threshold": 3,    # 覆盖维度 < 此值时放大不确定性
    "coverage_amplifier": 1.5,      # 低覆盖放大系数
    "conflict_amplifier": 1.3,      # 冲突放大系数
    "max_band": 2.0,                # band_width 上限
}

# ==================== ML 反馈权重配置 (QueenDistiller Enhancement C) ====================
ML_FEEDBACK_CONFIG = {
    "min_adjustment": 0.5,          # 维度调整因子下限
    "max_adjustment": 2.0,          # 维度调整因子上限
    "enable_dimension_weighting": True,   # 是否启用维度权重调整
    "enable_vote_boosting": True,         # 是否启用 Agent 投票置信度调整
}

# ==================== ML HGB 树模型配置（v15.0 替代 SGD）====================
ML_HGBC_CONFIG = {
    "max_iter": 200,              # 最大迭代轮数
    "max_depth": 4,               # 树深度（防过拟合，小样本用 3-4）
    "learning_rate": 0.05,        # 学习率（越小越稳，需更多轮数）
    "min_samples_leaf": 5,        # 叶节点最少样本（防止稀疏分裂）
    "l2_regularization": 1.0,     # L2 正则化（强正则防过拟合）
    "max_features": 0.8,          # 特征子采样 80%（每棵树只看 80% 特征）
    "validation_fraction": 0.15,  # 验证集比例（early stopping 用）
    "n_iter_no_change": 15,       # 连续 15 轮无改善则停止
}

# ==================== ML 真实数据训练配置 ====================
ML_TRAINING_CONFIG = {
    "use_real_data": True,             # 优先使用真实数据训练
    "min_real_samples": 30,            # 真实数据最少样本数
    "daily_retrain": True,             # 每日重训
    "max_training_rows": 500,          # 训练数据行数上限
    "blend_hardcoded_when_sparse": True,  # 真实数据不足时混入硬编码数据
}

# ==================== BearBee 看空评分配置 ====================
BEAR_SCORING_CONFIG = {
    # ── 评分算法 ──
    "max_signal_boost_weight": 0.3,        # 最强信号提升权重（0=纯均值, 1=纯最大值）
    "breadth_bonus_per_dim": 0.3,          # 每多一个活跃维度的加分（最多+0.9）
    # ── 方向判定阈值 ──
    "direction_bearish_min": 5.5,          # bear_score >= 5.5 → bearish（原 6.5）
    "direction_neutral_min": 3.5,          # bear_score >= 3.5 → neutral（原 4.5）
    # ── 无信号默认分 ──
    "no_signal_positive_mom": 3.0,
    "no_signal_negative_mom": 2.0,
    # ── 投票聚合 ──
    "voting_bearish_min_agents": 1,        # 看空最低 Agent 数（原 2）
    "voting_bearish_min_weight_pct": 0.25, # 看空最低权重比例（原 0.40）
    # ── Bear Cap ──
    "bear_cap_trigger_threshold": 5.0,     # 触发阈值（原 7.0）
    "bear_cap_slope": 0.5,                 # 上限衰减斜率
}

# ==================== Dashboard 颜色方案 ====================
COLOR_SCHEME = {
    "bullish": "#28a745",
    "bearish": "#dc3545",
    "neutral": "#ffc107",
    "score_high_threshold": 7.0,         # >= 此值用 bullish 色
    "score_low_threshold": 5.5,          # < 此值用 bearish 色
}


# ==================== 配置热更新 ====================

class ConfigLoader:
    """支持从外部 YAML/JSON 文件热加载 WATCHLIST 和 CATALYSTS。

    外部文件优先于内置 Python dict；文件不存在时保持内置值。
    使用 .clear() + .update() 就地修改全局 dict，确保已有的
    ``from config import WATCHLIST`` 引用自动获得新数据。
    """

    _OVERRIDE_YAML = str(PATHS.home / "watchlist_override.yaml")
    _OVERRIDE_JSON = str(PATHS.home / "watchlist_override.json")
    _last_mtime: float = 0.0
    _lock = threading.Lock()

    @classmethod
    def _find_override_file(cls):
        for path in (cls._OVERRIDE_YAML, cls._OVERRIDE_JSON):
            if os.path.isfile(path):
                return path
        return None

    @classmethod
    def _load_file(cls, path: str) -> dict:
        """加载 YAML 或 JSON 文件，返回原始 dict"""
        if path.endswith((".yaml", ".yml")):
            try:
                import yaml
            except ImportError:
                _log.warning("watchlist_override.yaml 存在但 PyYAML 未安装，跳过热加载")
                return {}
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        else:
            import json
            with open(path, encoding="utf-8") as f:
                return json.load(f)

    @classmethod
    def _reload_inner(cls) -> dict:
        """内部重载逻辑 —— 调用者必须持有 _lock。"""
        path = cls._find_override_file()
        if not path:
            return {"watchlist_count": len(WATCHLIST),
                    "catalysts_count": len(CATALYSTS),
                    "source": "builtin"}
        try:
            mtime = os.path.getmtime(path)
            data = cls._load_file(path)
        except (OSError, ValueError) as exc:
            _log.error("配置热加载失败 (%s): %s", path, exc)
            return {"watchlist_count": len(WATCHLIST),
                    "catalysts_count": len(CATALYSTS),
                    "source": "builtin (load error)"}

        new_wl = data.get("watchlist") or data.get("WATCHLIST") or {}
        new_cat = data.get("catalysts") or data.get("CATALYSTS") or {}

        if new_wl:
            WATCHLIST.clear()
            WATCHLIST.update(new_wl)
            _log.info("WATCHLIST 热更新: %d 个标的 ← %s", len(WATCHLIST), path)
        if new_cat:
            CATALYSTS.clear()
            CATALYSTS.update(new_cat)
            _log.info("CATALYSTS 热更新: %d 个催化剂 ← %s", len(CATALYSTS), path)

        cls._last_mtime = mtime
        return {"watchlist_count": len(WATCHLIST),
                "catalysts_count": len(CATALYSTS),
                "source": os.path.basename(path)}

    @classmethod
    def reload(cls) -> dict:
        """热加载外部配置文件，就地更新 WATCHLIST 和 CATALYSTS。

        Returns:
            {"watchlist_count": int, "catalysts_count": int, "source": str}
        """
        with cls._lock:
            return cls._reload_inner()

    @classmethod
    def reload_if_changed(cls) -> bool:
        """仅当外部文件 mtime 变化时才重载（适合定期调用）。

        整个 mtime 检查 + 重载操作在同一把锁内完成，防止并发双重重载。

        Returns:
            True 如果发生了重载
        """
        with cls._lock:
            path = cls._find_override_file()
            if not path:
                return False
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                return False
            if mtime <= cls._last_mtime:
                return False
            cls._reload_inner()
            return True


def reload_config() -> dict:
    """便捷函数：热加载外部 WATCHLIST/CATALYSTS 配置"""
    return ConfigLoader.reload()


if __name__ == "__main__":
    init_cache()
    result = reload_config()
    _log.info("配置已加载 | 标的 %d | 催化剂 %d | HOME=%s | source=%s",
              len(WATCHLIST), sum(len(v) for v in CATALYSTS.values()),
              PATHS.home, result["source"])
