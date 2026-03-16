# %%
import os
import pandas as pd
from eodhd import APIClient
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
EODHD_API_KEY = os.environ.get("EODHD_API_KEY")
if not EODHD_API_KEY:
    raise EnvironmentError("EODHD_API_KEY not set. Add it to your .env file.")

# %%
client = APIClient(EODHD_API_KEY)

# %%
# SINGLE FILTER SCREENER

mktcap_screener_json = client.stock_market_screener(sort = 'market_capitalization.desc', filters = '[["exchange","=","us"]]', limit = 10, offset = 0)
mktcap_screener_df = pd.DataFrame(mktcap_screener_json['data']).drop(['exchange','currency_symbol','last_day_data_date'], axis = 1)
mktcap_screener_df
# %%
# MULTIPLE FILTERS SCREENER

mf_screener_json = client.stock_market_screener(sort = 'earnings_share.desc', filters = '[["sector","=","Technology"],["market_capitalization",">",100000000000]]', limit = 10, offset = 0)
mf_screener_df = pd.DataFrame(mf_screener_json['data']).drop(['currency_symbol','last_day_data_date'], axis = 1)
mf_screener_df
# %%
newlow_screener_json = client.stock_market_screener(filters = '[["exchange","=","us"]]', signals = '200d_new_lo', limit = 10, offset = 0)
newlow_screener_df = pd.DataFrame(newlow_screener_json['data']).drop(['exchange','currency_symbol','last_day_data_date'], axis = 1)
newlow_screener_df
# %%
wshigh_screener_json = client.stock_market_screener(filters = '[["sector","=","Financial Services"]]', signals = 'wallstreet_hi', limit = 10, offset = 0)
wshigh_screener_df = pd.DataFrame(wshigh_screener_json['data']).drop(['currency_symbol','last_day_data_date'], axis = 1)
wshigh_screener_df
# %%
import pandas as pd
from eodhd import APIClient
import json

def fetch_top_stocks(api_token, exchange="us", limit=50):
    client = APIClient(EODHD_API_KEY)

    # Fetch stocks based on market cap
    screener_json = client.stock_market_screener(
        sort="market_capitalization.desc",
        filters=f'[["exchange","=","{exchange}"]]',
        limit=limit,
        offset=0
    )
    stocks_df = pd.DataFrame(screener_json['data'])
    return stocks_df[['code', 'name', 'sector', 'industry', 'market_capitalization']]

def fetch_technical_indicators(api_token, code, from_date="2023-01-01", to_date="2024-12-01"):
    
    client = APIClient(EODHD_API_KEY)
# %%
import pandas as pd
import requests
from eodhd import APIClient
from tqdm import tqdm
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import time  # Import tqdm for progress notifications
from datetime import datetime, timedelta
from collections import deque
from time import sleep
import logging
from datetime import datetime
import json
import os

# Set up logging
log_directory = "logs"
if not os.path.exists(log_directory):
    os.makedirs(log_directory)

current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f"{log_directory}/api_responses_{current_time}.log"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler()  # This will still print to console via tqdm.write
    ]
)
logger = logging.getLogger(__name__)

#######################
# 1) Initialize Client
#######################

client = APIClient(EODHD_API_KEY)
api_token = EODHD_API_KEY

########################
# 2) Screener
########################
raw_screener_json = client.stock_market_screener(
    filters='[["exchange","=","us"], ["market_capitalization", ">", 10000000000]]',
    sort='market_capitalization.desc',
    limit=50,
    offset=0
)

screener_df = pd.DataFrame(raw_screener_json['data'])
screener_df = screener_df[['code', 'name', 'market_capitalization']]

###############################
# 3) Helper to Fetch Indicators
###############################

class RateLimiter:
    def __init__(self, max_requests, time_window):
        self.max_requests = max_requests
        self.time_window = time_window  # in seconds
        self.requests = deque()
        self.total_weight = 0
        
        # Updated API weights according to documentation
        self.api_weights = {
            # Technical API endpoints (5 calls each)
            'rsi': 5,
            'atr': 5,
            'macd': 5,
            'sma': 5,
            'ema': 5,
            'bbands': 5,
            'stoch': 5,
            
            # Fundamental/Options API endpoints (10 calls each)
            'fundamentals': 10,
            'options': 10,
            'bonds': 10,
            
            # Bulk API endpoints
            'bulk_exchange': 100,  # Base cost for entire exchange
            
            # Default for standard endpoints
            'default': 1
        }

    def get_weight(self, function: str, num_symbols: int = 1) -> int:
        """
        Get the API weight cost for a specific function
        
        Args:
            function: The API function being called
            num_symbols: Number of symbols in the request (default=1)
        """
        base_weight = self.api_weights.get(function.lower(), self.api_weights['default'])
        
        # For bulk exchange requests with specific symbols
        if function == 'bulk_exchange' and num_symbols > 0:
            return base_weight + num_symbols
        
        # For multi-symbol requests
        return base_weight * num_symbols

    def countdown(self, seconds: int, desc: str):
        """Display a countdown timer in the tqdm output"""
        for remaining in range(seconds, 0, -1):
            tqdm.write(f"{desc}: {remaining}s remaining...")
            sleep(1)

    def wait_if_needed(self, function: str, num_symbols: int = 1):
        now = datetime.now()
        weight = self.get_weight(function, num_symbols)
        
        # Remove requests older than time_window
        while self.requests and (now - self.requests[0][0]) > timedelta(seconds=self.time_window):
            _, expired_weight = self.requests.popleft()
            self.total_weight -= expired_weight
        
        # If adding this request would exceed the limit, pause for 60 seconds
        if self.total_weight + weight >= self.max_requests:
            pause_msg = f"⚠️ Rate limit approaching (Current: {self.total_weight}, Adding: {weight}) - Pausing"
            tqdm.write(pause_msg)
            self.countdown(60, "Rate limit pause")
            self.requests.clear()
            self.total_weight = 0
        
        # Add current request
        self.requests.append((now, weight))
        self.total_weight += weight
        return f"API weight: {self.total_weight}/{self.max_requests}"

# Initialize rate limiter (850 requests per minute to be safe)
rate_limiter = RateLimiter(max_requests=850, time_window=60)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, ValueError))
)
def get_technical_indicator(ticker: str, function: str, params: dict, api_token: str) -> pd.DataFrame:
    # Wait if we're approaching rate limit, passing the function type
    rate_limiter.wait_if_needed(function)
    
    base_url = f"https://eodhd.com/api/technical/{ticker}"
    default_params = {
        'api_token': api_token,
        'function': function,
        'order': 'd',
        'fmt': 'json'
    }
    default_params.update(params)
    
    try:
        r = requests.get(base_url, params=default_params)
        r.raise_for_status()
        
        # Log request details
        logger.info(f"\nRequest for {ticker} - {function}:")
        logger.info(f"URL: {r.url}")
        
        # Log response headers
        logger.info("Response Headers:")
        logger.info(json.dumps(dict(r.headers), indent=2))
        
        data_json = r.json()
        
        # Log response data
        logger.info("Response Data:")
        logger.info(json.dumps(data_json, indent=2))
        
        if not data_json or (isinstance(data_json, list) and len(data_json) == 0):
            logger.warning(f"Empty response for {ticker} ({function})")
            return pd.DataFrame()
        
        if not isinstance(data_json, list):
            logger.warning(f"Unexpected response format for {ticker} ({function})")
            return pd.DataFrame()
            
        return pd.DataFrame(data_json)
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            logger.error(f"Rate limit exceeded! Response: {e.response.text}")
            rate_limiter.countdown(60, "Rate limit exceeded pause")
            raise
        else:
            logger.error(f"HTTP Error for {ticker}: {str(e)}")
            logger.error(f"Response: {e.response.text}")
            raise
    except Exception as e:
        logger.error(f"Error processing {ticker}: {str(e)}")
        raise

###############################
# 4) Fetch RSI, ATR, MACD Data
###############################
results = []
progress_bar = tqdm(screener_df.iterrows(), total=screener_df.shape[0], desc="Fetching Technical Indicators")

logger.info("\n=== Starting New Data Collection Run ===")
logger.info(f"Total tickers to process: {len(screener_df)}")

def get_date_range(days_back: int = 30) -> tuple:
    """Calculate start and end dates for the data range"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    return (
        start_date.strftime('%Y-%m-%d'),
        end_date.strftime('%Y-%m-%d')
    )

# Get date range once before the loop
start_date, end_date = get_date_range(days_back=30)
logger.info(f"Fetching data from {start_date} to {end_date}")

for i, row in progress_bar:
    ticker = row['code']
    mktcap = row['market_capitalization']
    
    try:
        logger.info(f"\nProcessing ticker: {ticker}")
        logger.info(f"Market Cap: {mktcap}")
        
        # Update progress bar with current ticker and weight info
        progress_bar.set_description(f"Processing {ticker}")
        
        # RSI
        weight_info = rate_limiter.wait_if_needed('rsi')
        rsi_df = get_technical_indicator(
            ticker, 
            'rsi', 
            {
                'period': 14,
                'from': start_date,
                'to': end_date
            }, 
            api_token
        )
        progress_bar.set_postfix_str(weight_info)
        
        if not rsi_df.empty:
            logger.info(f"RSI Data Shape: {rsi_df.shape}")
            logger.info(f"Latest RSI: {rsi_df.iloc[0].to_dict()}")
        
        # ATR
        weight_info = rate_limiter.wait_if_needed('atr')
        atr_df = get_technical_indicator(
            ticker, 
            'atr', 
            {
                'period': 14,
                'from': start_date,
                'to': end_date
            }, 
            api_token
        )
        progress_bar.set_postfix_str(weight_info)
        
        if not atr_df.empty:
            logger.info(f"ATR Data Shape: {atr_df.shape}")
            logger.info(f"Latest ATR: {atr_df.iloc[0].to_dict()}")
        
        # MACD
        weight_info = rate_limiter.wait_if_needed('macd')
        macd_df = get_technical_indicator(
            ticker, 
            'macd', 
            {
                'fast_period': 12,
                'slow_period': 26,
                'signal_period': 9,
                'from': start_date,
                'to': end_date
            }, 
            api_token
        )
        progress_bar.set_postfix_str(weight_info)
        
        if not macd_df.empty:
            logger.info(f"MACD Data Shape: {macd_df.shape}")
            logger.info(f"Latest MACD: {macd_df.iloc[0].to_dict()}")
        
        # Skip if any are empty
        if rsi_df.empty or atr_df.empty or macd_df.empty:
            continue
        
        # Grab the last row (index 0, since order='d')
        latest_rsi = rsi_df.iloc[0].get('rsi', None)
        latest_atr = atr_df.iloc[0].get('atr', None)
        latest_macd_row = macd_df.iloc[0]
        
        results.append({
            'ticker': ticker,
            'market_cap': mktcap,
            'latest_rsi': latest_rsi,
            'latest_atr': latest_atr,
            'macd_value': latest_macd_row.get('macd', None),
            'macd_signal': latest_macd_row.get('signal', None),
            'macd_divergence': latest_macd_row.get('divergence', None)
        })
        
    except Exception as e:
        logger.error(f"Error processing {ticker}: {str(e)}")
        continue

logger.info("\n=== Data Collection Run Completed ===")

technical_df = pd.DataFrame(results)

#################################
# 5) Filtering and Top 20
#################################
# Example thresholds
RSI_MAX = 30     # oversold level
ATR_MIN = 1.0    # example threshold
# We want MACD line > Signal line for a bullish signal

def filter_stocks(df):
    cond_rsi = (df['latest_rsi'] < RSI_MAX)
    cond_atr = (df['latest_atr'] > ATR_MIN)
    cond_macd = (df['macd_value'] > df['macd_signal'])
    return df[cond_rsi & cond_atr & cond_macd]

filtered_df = filter_stocks(technical_df)

# Sort by market cap descending, top 20
filtered_df = filtered_df.sort_values('market_cap', ascending=False).head(20)

print(filtered_df)



# %%
