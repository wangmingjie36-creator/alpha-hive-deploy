# Tradier API Integration Guide

## Quick Start

### 1. Get API Token (Free)

1. Visit https://developer.tradier.com
2. Create a free developer account (no payment required)
3. Generate an API token in your account dashboard
4. Store token in one of:
   - Environment variable: `export TRADIER_API_TOKEN="your_token_here"`
   - File: Create `~/.alpha_hive_tradier_key` with token as first line

### 2. Import and Use

```python
from tradier_fetcher import TradierFetcher

# Initialize (uses sandbox by default for testing)
fetcher = TradierFetcher(use_sandbox=True)

# Or use production API
fetcher = TradierFetcher(use_sandbox=False)

# Check API connectivity
if not fetcher.health_check():
    print("API token not available")
```

## Core Methods

### Fetch Options Chain with Greeks

```python
# Get all expirations and option data
chain = fetcher.fetch_options_chain("NVDA")

# Get specific expiration
chain = fetcher.fetch_options_chain("NVDA", "2026-04-17")

# Returns:
# {
#   "expirations": ["2026-04-17", "2026-05-16", ...],
#   "quotes": {
#     "2026-04-17": {
#       "calls": [
#         {
#           "symbol": "NVDA260417C00180000",
#           "strike": 180.0,
#           "iv": 0.35,
#           "delta": 0.65,
#           "gamma": 0.012,
#           "theta": -0.08,
#           "vega": 0.25,
#           ...
#         }
#       ],
#       "puts": [...]
#     }
#   }
# }
```

### Get IV for Specific Strike

```python
iv = fetcher.fetch_iv_for_strike(
    ticker="NVDA",
    strike=180.0,
    expiration="2026-04-17",
    option_type="call"  # or "put"
)
# Returns: 0.35 (float) or None
```

### Fetch Historical IV

```python
historical = fetcher.fetch_historical_iv(
    ticker="SPY",
    start_date="2026-01-01",
    end_date="2026-03-27"
)

# Returns:
# [
#   {
#     "date": "2026-03-27",
#     "iv_30d": 0.32,
#     "iv_60d": 0.30,
#     "iv_90d": 0.29,
#     "close": 185.50
#   },
#   ...
# ]
```

### Get Greeks (Full Chain)

```python
greeks_chain = fetcher.fetch_greeks("NVDA", "2026-04-17")
# Same format as fetch_options_chain but ensures Greeks are populated
```

## Cross-Validation

### Compare IV Sources

```python
validation = fetcher.cross_validate_iv(
    ticker="NVDA",
    yf_iv=0.35,        # From yfinance
    tradier_iv=0.34    # From Tradier
)

# Returns:
# {
#   "yf_iv": 0.35,
#   "tradier_iv": 0.34,
#   "diff_abs": 0.01,
#   "diff_pct": 2.86,
#   "reliable_source": "tradier",
#   "confidence": 0.95,
#   "status": "consistent"  # "consistent", "divergent", "highly_divergent", "single_source", "no_data"
# }
```

### Full yfinance Comparison

```python
yf_data = {
    "expirations": ["2026-04-17", "2026-05-16"],
    "iv_30d": 0.35,
    "atm_strike": 180.0
}

report = fetcher.validate_against_yfinance("NVDA", yf_data)

# Returns:
# {
#   "ticker": "NVDA",
#   "validation_date": "2026-03-27",
#   "expirations": [
#     {
#       "expiration": "2026-04-17",
#       "yf_iv_30d": 0.35,
#       "tradier_iv_30d": 0.34,
#       "iv_diff_pct": 2.86,
#       "assessment": "reliable"
#     }
#   ],
#   "overall_correlation": 0.96,
#   "recommendation": "use_tradier"  # "use_tradier", "use_yfinance", "blend"
# }
```

## Caching

- **Live data** (options chains): 5 minutes
- **Historical data** (IV series): 24 hours
- **Cache location**: `~/.cache/tradier/`

Cache is automatically managed. To force refresh, delete cache files:

```bash
rm -rf ~/.cache/tradier/
```

## Error Handling

The fetcher gracefully degrades:

```python
# If no API token, methods return None and log warning
chain = fetcher.fetch_options_chain("NVDA")  # Returns None

# Check token before using
if fetcher._is_token_valid():
    # Safe to use
    chain = fetcher.fetch_options_chain("NVDA")
```

## Integration with Alpha Hive

### In Options Analyzer

```python
from tradier_fetcher import TradierFetcher
from options_analyzer import OptionsAnalyzer

fetcher = TradierFetcher()
analyzer = OptionsAnalyzer()

# Get Tradier Greeks for deeper analysis
tradier_chain = fetcher.fetch_options_chain(ticker, expiration)

# Cross-validate IV
yf_iv = analyzer.get_yfinance_iv(ticker)
tradier_iv = fetcher.fetch_iv_for_strike(ticker, strike, expiration, "call")
validation = fetcher.cross_validate_iv(ticker, yf_iv, tradier_iv)

# Use reliable source recommendation
if validation["reliable_source"] == "tradier":
    # Use Tradier IV in analysis
    iv_to_use = tradier_iv
else:
    # Use yfinance IV
    iv_to_use = yf_iv
```

### In Advanced Analyzer

```python
# Enhance GEX analysis with Tradier Greeks
fetcher = TradierFetcher()
chain = fetcher.fetch_options_chain(ticker)

# Chain already has full Greeks from Tradier
# Can be more accurate than yfinance for American options
```

## API Endpoints Used

- `GET /markets/options/chains` - Option chain with Greeks
- `GET /markets/options/expirations` - Available expiration dates
- `GET /markets/options/historical` - Historical IV data
- `GET /markets/quotes` - Stock quotes (for health_check)

All endpoints require Bearer token in Authorization header.

## Performance Notes

- First call for a ticker: ~1-2 seconds (API call)
- Subsequent calls (within 5 min): ~10ms (cached)
- Historical data (within 24h): ~50ms (cached)

## Troubleshooting

### "Tradier API token not found"
- Set `TRADIER_API_TOKEN` env var, or
- Create `~/.alpha_hive_tradier_key` file with token

### "requests library not available"
- Install: `pip install requests`

### API returns empty data
- Check sandbox vs production mode
- Verify ticker symbol (must be uppercase)
- Confirm API token is valid
- Check market hours (extended hours data may be limited)

### Caching issues
- Delete `~/.cache/tradier/` to force refresh
- Check disk space in home directory

## References

- Tradier Developer Docs: https://documentation.tradier.com
- Sandbox: https://sandbox.tradier.com/v1/
- Production: https://api.tradier.com/v1/
