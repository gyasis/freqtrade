# %%
import pandas as pd
import requests
import yfinance as yf  # Add yfinance import
from eodhd import APIClient
from tqdm import tqdm
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import time
from datetime import datetime, timedelta
from collections import deque
from time import sleep
import logging
import json
import os
import random
import sys
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()
EODHD_API_KEY = os.environ.get("EODHD_API_KEY")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")
if not EODHD_API_KEY:
    raise EnvironmentError("EODHD_API_KEY not set. Add it to your .env file.")

# Set up logging
log_directory = "logs"
if not os.path.exists(log_directory):
    os.makedirs(log_directory)

current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f"{log_directory}/api_responses_{current_time}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

#######################
# 1) Initialize Client and Load Tickers
#######################

client = APIClient(EODHD_API_KEY)
api_token = EODHD_API_KEY

# Load tickers from JSON
JSON_FILE = "symbols.json"

if not os.path.exists(JSON_FILE):
    raise FileNotFoundError(f"Could not find {JSON_FILE} in current directory.")

with open(JSON_FILE, "r") as f:
    data = json.load(f)

# Convert JSON dict to DataFrame
df_symbols = pd.DataFrame.from_dict(data, orient="index")
df_symbols.rename(columns={"ticker": "Ticker"}, inplace=True)

all_tickers = df_symbols["Ticker"].unique().tolist()
logger.info(f"Loaded {len(all_tickers)} tickers from {JSON_FILE}.")

########################
# 2) Yahoo Finance Price Filter with Tenacity
########################

# Create a custom session with connection pooling
def create_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=retry,
        pool_block=False
    )
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, ValueError, IOError))
)
def fetch_chunk_with_retry(chunk):
    """Fetch a chunk of tickers with retry logic"""
    try:
        session = create_session()
        logger.debug(f"\nFetching Yahoo Finance data for chunk: {chunk}")
        
        data = yf.download(
            chunk,
            period="1d",
            interval="1d",
            group_by="ticker",
            progress=False,
            session=session,
            ignore_tz=True
        )
        session.close()
        
        # Log the structure and first few rows of the response
        logger.debug(f"Yahoo Finance Response for chunk:")
        logger.debug(f"Data type: {type(data)}")
        logger.debug(f"Data shape: {data.shape}")
        logger.debug(f"Data columns: {data.columns}")
        logger.debug(f"First few rows:\n{data.head()}")
        
        return data
    except Exception as e:
        logger.error(f"Error downloading chunk: {str(e)}")
        raise

def ensure_cache_dir():
    """Ensure cache directory exists"""
    cache_dir = os.path.join(os.path.dirname(__file__), '.cache')
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

def ensure_files_exist():
    """Ensure all necessary files and directories exist"""
    cache_dir = ensure_cache_dir()
    
    required_files = {
        'failed_tickers.json': {
            'invalid_period': [],
            'delisted': [],
            'other_errors': [],
            'last_updated': None
        },
        'stock_prices_cache.csv': pd.DataFrame(columns=['Ticker', 'Last_Close', 'Timestamp'])
    }
    
    for filename, default_content in required_files.items():
        filepath = os.path.join(cache_dir, filename)
        if not os.path.exists(filepath):
            logger.info(f"Creating {filename} with default content")
            if filename.endswith('.json'):
                with open(filepath, 'w') as f:
                    json.dump(default_content, f, indent=2)
            elif filename.endswith('.csv'):
                default_content.to_csv(filepath, index=False)

def load_failed_tickers(filename='failed_tickers.json'):
    """Load previously saved failed tickers"""
    cache_dir = ensure_cache_dir()
    failed_path = os.path.join(cache_dir, filename)
    
    try:
        if os.path.exists(failed_path):
            with open(failed_path, 'r') as f:
                failed_dict = json.load(f)
            logger.info(f"Loaded failed tickers from {failed_path}")
        else:
            failed_dict = {
                'invalid_period': [],
                'delisted': [],
                'other_errors': [],
                'last_updated': None
            }
            with open(failed_path, 'w') as f:
                json.dump(failed_dict, f, indent=2)
            logger.info(f"Created new failed tickers file at {failed_path}")
        return failed_dict
    except Exception as e:
        logger.warning(f"Error loading failed tickers: {str(e)}")
        return {
            'invalid_period': [],
            'delisted': [],
            'other_errors': [],
            'last_updated': None
        }

def save_failed_tickers(failed_dict, filename='failed_tickers.json'):
    """Save failed tickers with their error types"""
    cache_dir = ensure_cache_dir()
    failed_path = os.path.join(cache_dir, filename)
    
    if os.path.exists(failed_path):
        with open(failed_path, 'r') as f:
            existing_failed = json.load(f)
    else:
        existing_failed = {
            'invalid_period': [],
            'delisted': [],
            'other_errors': [],
            'last_updated': None
        }
    
    for error_type, tickers in failed_dict.items():
        existing_failed[error_type] = list(set(existing_failed.get(error_type, []) + tickers))
    
    existing_failed['last_updated'] = pd.Timestamp.now().isoformat()
    
    with open(failed_path, 'w') as f:
        json.dump(existing_failed, f, indent=2)
    
    logger.info(f"Saved failed tickers to {failed_path}")

def load_cached_data(cache_file='stock_prices_cache.csv'):
    """Load previously cached stock data"""
    cache_dir = ensure_cache_dir()
    cache_path = os.path.join(cache_dir, cache_file)
    
    if os.path.exists(cache_path):
        cache_age = pd.Timestamp.now() - pd.Timestamp.fromtimestamp(os.path.getmtime(cache_path))
        logger.info(f"Found cache file created {cache_age} ago")
        df = pd.read_csv(cache_path)
        logger.info(f"Cache contains {len(df)} symbols")
        return df
    
    logger.info("No cache file found")
    return pd.DataFrame(columns=["Ticker", "Last_Close", "Timestamp"])

def save_to_cache(df, filename='stock_prices_cache.csv'):
    """Save the current stock data to cache"""
    cache_dir = ensure_cache_dir()
    cache_file = os.path.join(cache_dir, filename)
    
    logger.info(f"Saving data to cache: {cache_file}")
    df.to_csv(cache_file, index=False)

def fetch_latest_closes_yf(tickers, chunk_size=50, cache_filename='stock_prices_cache.csv'):
    """Fetch latest closing prices from Yahoo Finance with error handling and caching"""
    # Load cache and failed tickers first
    cached_df = load_cached_data(cache_filename)
    previous_failed = load_failed_tickers()
    current_time = pd.Timestamp.now()
    
    # First, check if we have valid cached data
    if not cached_df.empty:
        cache_age = current_time - pd.to_datetime(cached_df['Timestamp']).max()
        logger.info(f"Cache age: {cache_age}")
        
        if cache_age < pd.Timedelta(days=1):
            logger.info("✓ Using recent cache (less than 24 hours old)")
            return cached_df
    
    # Get already processed tickers with valid data
    valid_cached = set(cached_df[
        cached_df['Timestamp'].notna() & 
        (pd.to_datetime(cached_df['Timestamp']) > (current_time - pd.Timedelta(days=1)))
    ]['Ticker'])
    
    # Get known problematic tickers
    delisted = set(previous_failed.get('delisted', []))
    invalid_period = set(previous_failed.get('invalid_period', []))
    other_errors = set(previous_failed.get('other_errors', []))
    
    # Skip all problematic tickers
    skip_tickers = valid_cached | delisted | invalid_period | other_errors
    
    # Get only the tickers we actually need to process
    tickers_to_fetch = [t for t in tickers if t not in skip_tickers]
    
    if not tickers_to_fetch:
        logger.info("\n✓ No new tickers to fetch - using existing data")
        logger.debug(f"Cache contains {len(valid_cached)} valid tickers")
        logger.debug(f"Known delisted: {len(delisted)}")
        logger.debug(f"Known invalid period: {len(invalid_period)}")
        logger.debug(f"Known other errors: {len(other_errors)}")
        return cached_df
    
    logger.info(f"\n➜ Fetching {len(tickers_to_fetch)} new tickers")
    logger.debug(f"First few to fetch: {', '.join(tickers_to_fetch[:5])}...")
    
    # Initialize results and failed tracking
    results = []
    failed_dict = {
        'invalid_period': list(invalid_period),  # Maintain existing failed tickers
        'delisted': list(delisted),
        'other_errors': list(other_errors)
    }
    
    # Process in chunks
    chunks = [tickers_to_fetch[i:i + chunk_size] for i in range(0, len(tickers_to_fetch), chunk_size)]
    
    for chunk_idx, chunk in enumerate(tqdm(chunks, desc=f"Fetching {len(tickers_to_fetch)} new tickers")):
        try:
            data = yf.download(
                chunk,
                period="1d",
                interval="1d",
                group_by="ticker",
                progress=False,
                ignore_tz=True
            )
            
            # Process chunk results
            for ticker in chunk:
                try:
                    if len(chunk) == 1:  # Single ticker case
                        if isinstance(data, pd.DataFrame) and not data.empty and 'Close' in data.columns:
                            results.append({
                                'Ticker': ticker,
                                'Last_Close': float(data['Close'].iloc[-1]),
                                'Timestamp': current_time
                            })
                            continue
                    
                    # Multiple tickers case
                    if ticker in data.columns.levels[1]:
                        close_price = data['Close'][ticker].iloc[-1]
                        if pd.notna(close_price):
                            results.append({
                                'Ticker': ticker,
                                'Last_Close': float(close_price),
                                'Timestamp': current_time
                            })
                        else:
                            failed_dict['other_errors'].append(ticker)
                    else:
                        failed_dict['other_errors'].append(ticker)
                
                except Exception as e:
                    error_str = str(e)
                    if "Period '1d' is invalid" in error_str:
                        failed_dict['invalid_period'].append(ticker)
                    elif "possibly delisted" in error_str:
                        failed_dict['delisted'].append(ticker)
                    else:
                        failed_dict['other_errors'].append(ticker)
            
            # Save progress
            if results:
                temp_df = pd.DataFrame(results)
                combined_df = pd.concat([cached_df, temp_df]).drop_duplicates('Ticker', keep='last')
                save_to_cache(combined_df, cache_filename)
                logger.debug(f"Saved progress: {len(results)}/{len(tickers_to_fetch)} new tickers processed")
        
        except Exception as e:
            logger.error(f"Chunk {chunk_idx + 1} failed: {str(e)}")
            failed_dict['other_errors'].extend(chunk)
    
    # Create final DataFrame
    final_df = cached_df.copy()
    if results:
        new_df = pd.DataFrame(results)
        final_df = pd.concat([final_df, new_df]).drop_duplicates('Ticker', keep='last')
    
    # Save updated failed tickers
    save_failed_tickers(failed_dict)
    
    # Final status report
    logger.info(f"\n✓ Final results:")
    logger.info(f"  - Total symbols: {len(final_df)}")
    logger.info(f"  - New symbols added: {len(results)}")
    logger.info(f"  - Failed downloads: {sum(len(v) for v in failed_dict.values())}")
    
    return final_df

# Fetch all prices first
df_prices = fetch_latest_closes_yf(all_tickers, chunk_size=50)
logger.info(f"Fetched Yahoo prices for {len(df_prices)} tickers.")

########################
# 3) Price Filter
########################
MIN_PRICE = 5
MAX_PRICE = 45

df_filtered = df_prices[
    (df_prices["Last_Close"] > MIN_PRICE) &
    (df_prices["Last_Close"] < MAX_PRICE)
].copy()

df_filtered.reset_index(drop=True, inplace=True)
total_filtered = len(df_filtered)
logger.info(f"After price filtering (${MIN_PRICE} - ${MAX_PRICE}), we have {total_filtered} tickers.")

########################################
# Rate Limiter Class
########################################
class RateLimiter:
    """
    Basic rate limiter to ensure we don't exceed a certain # of requests per minute.
    We track 'API weight' rather than raw request count (the EODHD Tech API calls cost 5 each).
    """
    def __init__(self, max_requests=850, time_window=60):
        """
        :param max_requests: maximum weight capacity within time_window
        :param time_window: time window in seconds
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()  # stores (time, weight)
        self.total_weight = 0

        # EODHD weighting:
        # - Tech API (rsi, atr, macd, etc.) => 5 calls each
        # - default => 1
        self.api_weights = {
            'rsi': 5,
            'atr': 5,
            'macd': 5,
            'default': 1
        }

    def get_weight(self, function: str, num_symbols=1):
        return self.api_weights.get(function.lower(), self.api_weights['default']) * num_symbols

    def countdown(self, seconds: int, desc: str):
        for s in range(seconds, 0, -1):
            tqdm.write(f"{desc}: {s}s remaining...")
            sleep(1)

    def wait_if_needed(self, function: str, num_symbols=1):
        now = datetime.now()
        weight = self.get_weight(function, num_symbols)

        # Remove expired from the deque
        while self.requests and (now - self.requests[0][0]).total_seconds() > self.time_window:
            _, expired_weight = self.requests.popleft()
            self.total_weight -= expired_weight

        if self.total_weight + weight >= self.max_requests:
            pause_msg = (
                f"⚠️ Rate limit approaching (Current: {self.total_weight}, "
                f"Adding: {weight}) => Pausing 60s"
            )
            tqdm.write(pause_msg)
            self.countdown(60, "Rate limit pause")
            # Reset
            self.requests.clear()
            self.total_weight = 0

        self.requests.append((now, weight))
        self.total_weight += weight
        return f"API Weight: {self.total_weight}/{self.max_requests}"

# Initialize the rate limiter
rate_limiter = RateLimiter(max_requests=850, time_window=60)

########################################
# Tenacity-wrapped API call
########################################
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, ValueError))
)
def get_technical_indicator(ticker: str, function: str, params: dict, api_token: str) -> pd.DataFrame:
    """
    Fetch the specified technical indicator from EODHD for `ticker`.
    Uses a rate limiter and auto-retry if HTTP error or ValueError occur.
    """
    base_url = f"https://eodhd.com/api/technical/{ticker}"
    default_params = {
        "api_token": api_token,
        "function": function,
        "order": "d",  # descending
        "fmt": "json"
    }
    default_params.update(params)

    r = requests.get(base_url, params=default_params)
    r.raise_for_status()
    data_json = r.json()

    if not isinstance(data_json, list):
        raise ValueError(f"Unexpected response for {ticker} ({function}): {data_json}")

    if len(data_json) == 0:
        return pd.DataFrame()

    return pd.DataFrame(data_json)

###############################
# 5) Fetch Technical Indicators
###############################
results = []
# progress_bar = tqdm(df_filtered.iterrows(), total=df_filtered.shape[0], desc="Fetching Technical Indicators")

logger.info("\n=== Starting New Data Collection Run ===")
logger.info(f"Total tickers to process: {len(df_filtered)}")

def get_date_range(days_back: int = 30) -> tuple:
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    return (
        start_date.strftime('%Y-%m-%d'),
        end_date.strftime('%Y-%m-%d')
    )

start_date, end_date = get_date_range(days_back=30)
logger.info(f"Fetching data from {start_date} to {end_date}")

def analyze_and_display_results(df, top_n=5):
    """Analyze and display technical indicator results"""
    print("\n" + "="*50)
    print("ANALYSIS RESULTS")
    print("="*50)
    
    if df.empty:
        print("No data available for analysis.")
        return
    
    # RSI Analysis
    print("\n🔸 RSI Analysis:")
    print(f"Top {top_n} Oversold Stocks (RSI < 30):")
    oversold = df[df['latest_rsi'] < 30].sort_values('latest_rsi')
    if not oversold.empty:
        print(oversold[['ticker', 'price', 'latest_rsi']].head(top_n).to_string(index=False))
    else:
        print("No oversold stocks found.")
    
    print(f"\nTop {top_n} Overbought Stocks (RSI > 70):")
    overbought = df[df['latest_rsi'] > 70].sort_values('latest_rsi', ascending=False)
    if not overbought.empty:
        print(overbought[['ticker', 'price', 'latest_rsi']].head(top_n).to_string(index=False))
    else:
        print("No overbought stocks found.")
    
    # MACD Analysis
    print("\n🔸 MACD Analysis:")
    print(f"Top {top_n} Bullish MACD Crossovers:")
    bullish_macd = df[df['latest_macd_hist'] > 0].sort_values('latest_macd_hist', ascending=False)
    if not bullish_macd.empty:
        print(bullish_macd[['ticker', 'price', 'latest_macd', 'latest_macd_signal', 'latest_macd_hist']].head(top_n).to_string(index=False))
    else:
        print("No bullish MACD crossovers found.")
    
    print(f"\nTop {top_n} Bearish MACD Crossovers:")
    bearish_macd = df[df['latest_macd_hist'] < 0].sort_values('latest_macd_hist')
    if not bearish_macd.empty:
        print(bearish_macd[['ticker', 'price', 'latest_macd', 'latest_macd_signal', 'latest_macd_hist']].head(top_n).to_string(index=False))
    else:
        print("No bearish MACD crossovers found.")
    
    # ATR Analysis (Volatility)
    print("\n🔸 ATR Analysis:")
    print(f"Top {top_n} Most Volatile Stocks:")
    high_atr = df.sort_values('latest_atr', ascending=False)
    if not high_atr.empty:
        print(high_atr[['ticker', 'price', 'latest_atr']].head(top_n).to_string(index=False))
    else:
        print("No volatile stocks found.")
    
    # Combined Signals
    print("\n🔸 Combined Signal Analysis:")
    potential_buys = df[
        (df['latest_rsi'] < 40) &  # Oversold
        (df['latest_macd_hist'] > 0)  # Bullish MACD
    ].sort_values('latest_rsi')
    
    if not potential_buys.empty:
        print(f"\nPotential Buy Signals (Oversold + Bullish MACD) - Top {top_n}:")
        print(potential_buys[['ticker', 'price', 'latest_rsi', 'latest_macd_hist']].head(top_n).to_string(index=False))
    else:
        print("No potential buy signals found.")
    
    potential_sells = df[
        (df['latest_rsi'] > 60) &  # Overbought
        (df['latest_macd_hist'] < 0)  # Bearish MACD
    ].sort_values('latest_rsi', ascending=False)
    
    if not potential_sells.empty:
        print(f"\nPotential Sell Signals (Overbought + Bearish MACD) - Top {top_n}:")
        print(potential_sells[['ticker', 'price', 'latest_rsi', 'latest_macd_hist']].head(top_n).to_string(index=False))
    else:
        print("No potential sell signals found.")

def process_technical_indicators(df, rate_limiter, min_price, max_price):
    """Process technical indicators for the selected dataframe"""
    results = []
    
    for idx, row in tqdm(df.iterrows(), total=len(df), desc="Processing Tickers"):
        ticker = row['Ticker']
        
        try:
            logger.info(f"\nProcessing ticker: {ticker}")
            logger.info(f"Last Close: ${row['Last_Close']:.2f}")
            
            rate_limiter.wait_if_needed('default')
            
            ticker_results = {
                'ticker': ticker,
                'price': row['Last_Close'],
                'latest_rsi': None,
                'latest_atr': None,
                'latest_macd': None,
                'latest_macd_signal': None,
                'latest_macd_hist': None
            }
            
            # Get MACD
            macd_data = fetch_technical_data(ticker, 'macd')
            if not macd_data.empty:
                if 'macd' in macd_data.columns:
                    ticker_results['latest_macd'] = macd_data['macd'].iloc[0]
                if 'signal' in macd_data.columns:
                    ticker_results['latest_macd_signal'] = macd_data['signal'].iloc[0]
                if 'divergence' in macd_data.columns:
                    ticker_results['latest_macd_hist'] = macd_data['divergence'].iloc[0]
            
            # Get RSI
            rsi_data = fetch_technical_data(ticker, 'rsi', {'period': 14})
            if not rsi_data.empty and 'rsi' in rsi_data.columns:
                ticker_results['latest_rsi'] = rsi_data['rsi'].iloc[0]
            
            # Get ATR
            atr_data = fetch_technical_data(ticker, 'atr', {'period': 14})
            if not atr_data.empty and 'atr' in atr_data.columns:
                ticker_results['latest_atr'] = atr_data['atr'].iloc[0]
            
            results.append(ticker_results)
            logger.info(f"Successfully processed {ticker}")
            
        except Exception as e:
            logger.error(f"Error processing {ticker}: {str(e)}")
            results.append(ticker_results)  # Add even if there's an error
            continue
    
    results_df = pd.DataFrame(results)
    
    # Save results with filter information in the filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cache_dir = ensure_cache_dir()
    output_file = os.path.join(cache_dir, f'analysis_results_{timestamp}_filter_{min_price}-{max_price}.csv')
    results_df.to_csv(output_file, index=False)
    logger.info(f"\nResults saved to: {output_file}")
    
    # Analyze and display results
    logger.info("\nAnalyzing results...")
    analyze_and_display_results(results_df)
    
    return results_df

def analyze_stocks(df_filtered):
    """Analyze stocks with technical indicators"""
    rate_limiter = RateLimiter(max_tokens=850, tokens_per_second=1)
    results = []
    
    total = len(df_filtered)
    for idx, row in tqdm(df_filtered.iterrows(), total=total, desc="Processing", 
                        unit="stock", dynamic_ncols=True):
        ticker = row['Ticker']
        price = row['Last_Close']
        
        logger.info(f"\nProcessing ticker: {ticker}")
        logger.info(f"Last Close: ${price:.2f}")
        
        # Get technical indicators
        tech_data = process_technical_indicators(ticker, rate_limiter)
        
        # Combine price and technical data
        stock_data = pd.concat([
            pd.Series({'Ticker': ticker, 'Last_Close': price}),
            tech_data.drop('ticker', errors='ignore')
        ])
        
        results.append(stock_data)
        
    # Create final DataFrame
    final_df = pd.DataFrame(results)
    
    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cache_dir = ensure_cache_dir()
    output_file = os.path.join(cache_dir, f'analysis_results_{timestamp}.csv')
    final_df.to_csv(output_file, index=False)
    logger.info(f"\nResults saved to: {output_file}")
    
    return final_df

def display_analysis(df):
    """Display analysis results with formatting"""
    if df.empty:
        print("No analysis results available!")
        return
        
    # Format numeric columns
    formatted_df = df.copy()
    formatted_df['Last_Close'] = formatted_df['Last_Close'].map('${:,.2f}'.format)
    formatted_df['latest_rsi'] = formatted_df['latest_rsi'].map('{:.2f}'.format)
    formatted_df['latest_atr'] = formatted_df['latest_atr'].map('{:.2f}'.format)
    formatted_df['latest_macd'] = formatted_df['latest_macd'].map('{:.2f}'.format)
    
    print("\nAnalysis Results:")
    print(formatted_df.to_string(index=False))

def display_saved_analysis():
    """Display saved analysis results"""
    cache_dir = ensure_cache_dir()
    files = [f for f in os.listdir(cache_dir) if f.startswith('analysis_results_') and f.endswith('.csv')]
    
    if not files:
        print("No saved analysis results found.")
        return
    
    print("\nAvailable Analysis Results:")
    for i, file in enumerate(files, 1):
        print(f"{i}. {file}")
    
    choice = input("\nEnter the number of the file you want to view (or 'q' to cancel): ").strip()
    
    if choice.lower() == 'q':
        return
    
    try:
        file_index = int(choice) - 1
        if 0 <= file_index < len(files):
            file_path = os.path.join(cache_dir, files[file_index])
            df = pd.read_csv(file_path)
            print(f"\nDisplaying results from {files[file_index]}:")
            
            # Ask user for the number of top results to display
            top_n = input("Enter the number of top results to display for each category: ").strip()
            try:
                top_n = int(top_n)
            except ValueError:
                print("Invalid input. Displaying top 5 by default.")
                top_n = 5
            
            # Display analysis results
            analyze_and_display_results(df, top_n)
        else:
            print("Invalid choice. Please try again.")
    except ValueError:
        print("Invalid input. Please enter a number.")

def price_filter_loop(cache_df):
    """Interactive price filtering loop"""
    filtered_df = None
    min_price, max_price = 5, 45  # Default filter values
    
    while True:
        print("\n" + "="*50)
        print("PRICE FILTER MENU")
        print("="*50)
        
        # Show current data stats
        total_symbols = len(cache_df)
        if filtered_df is not None:
            filtered_symbols = len(filtered_df)
            current_df = filtered_df
        else:
            filtered_symbols = total_symbols
            current_df = cache_df
            
        print(f"\nCurrent Statistics:")
        print(f"Total available symbols: {total_symbols}")
        print(f"Filtered symbols: {filtered_symbols}")
        print(f"Current price range: ${current_df['Last_Close'].min():.2f} - ${current_df['Last_Close'].max():.2f}")
        print(f"Average price: ${current_df['Last_Close'].mean():.2f}")
        print(f"Median price: ${current_df['Last_Close'].median():.2f}")
        
        print("\nOptions:")
        print("1. Use default filter ($5 - $45)")
        print("2. Set custom price range")
        print("3. Take random sample")
        print("4. Process all filtered stocks")
        print("5. Reset filters")
        print("6. View saved analysis results")
        print("7. Exit")
        
        choice = input("\nEnter your choice (1-7): ").strip()
        
        if choice == '1':
            min_price, max_price = 5, 45
            filtered_df = cache_df[
                (cache_df['Last_Close'] >= min_price) & 
                (cache_df['Last_Close'] <= max_price)
            ]
            print(f"\nFiltered to {len(filtered_df)} symbols between ${min_price} and ${max_price}")
            
        elif choice == '2':
            try:
                min_price = float(input("Enter minimum price: $"))
                max_price = float(input("Enter maximum price: $"))
                filtered_df = cache_df[
                    (cache_df['Last_Close'] >= min_price) & 
                    (cache_df['Last_Close'] <= max_price)
                ]
                print(f"\nFiltered to {len(filtered_df)} symbols between ${min_price} and ${max_price}")
            except ValueError:
                print("Invalid input! Please enter valid numbers.")
                continue
                
        elif choice == '3':
            if filtered_df is None:
                print("Please apply a price filter first!")
                continue
                
            try:
                sample_size = input("Enter number of symbols to randomly select: ").strip()
                num_symbols = int(sample_size)
                if num_symbols > len(filtered_df):
                    print(f"Requested {num_symbols} but only {len(filtered_df)} available. Using all symbols.")
                    return filtered_df, min_price, max_price
                else:
                    sampled_df = filtered_df.sample(n=num_symbols)
                    print(f"\nRandomly selected {num_symbols} symbols.")
                    return sampled_df, min_price, max_price
            except ValueError:
                print("Invalid input! Please enter a valid number.")
                continue
                
        elif choice == '4':
            if filtered_df is not None and not filtered_df.empty:
                return filtered_df, min_price, max_price
            else:
                print("No valid filter applied yet!")
                continue
                
        elif choice == '5':
            filtered_df = None
            min_price, max_price = 5, 45  # Reset to default
            print("Filters reset.")
            continue
        
        elif choice == '6':
            display_saved_analysis()
            continue
            
        elif choice == '7':
            return None, None, None
            
        else:
            print("Invalid choice! Please try again.")
            continue

def get_missing_tickers(cached_df, all_tickers):
    """Compare cached tickers with all tickers to find missing ones"""
    if cached_df.empty:
        logger.info("Cache is empty, all tickers will be fetched")
        return all_tickers
    
    cached_tickers = set(cached_df['Ticker'])
    all_tickers_set = set(all_tickers)
    missing_tickers = list(all_tickers_set - cached_tickers)
    
    if missing_tickers:
        logger.info(f"Found {len(missing_tickers)} missing symbols: {', '.join(missing_tickers[:5])}...")
        if len(missing_tickers) > 5:
            logger.info(f"... and {len(missing_tickers) - 5} more")
    else:
        logger.info("Cache is complete - no missing symbols")
    
    return missing_tickers

def is_cache_valid(cache_df):
    """Check if cache is valid (less than 24 hours old) and complete"""
    if cache_df.empty:
        return False
    
    try:
        cache_df['Timestamp'] = pd.to_datetime(cache_df['Timestamp'])
        most_recent = cache_df['Timestamp'].max()
        return (pd.Timestamp.now() - most_recent) < pd.Timedelta(hours=24)
    except (KeyError, ValueError):
        return False

def setup_logging():
    """Configure logging to both file and console with different levels"""
    cache_dir = ensure_cache_dir()
    log_filename = f"screener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_filepath = os.path.join(cache_dir, 'logs', log_filename)
    
    # Create logs directory if it doesn't exist
    os.makedirs(os.path.join(cache_dir, 'logs'), exist_ok=True)
    
    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    console_formatter = logging.Formatter(
        '%(message)s'  # Simplified format for console
    )
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers
    logger.handlers.clear()
    
    # File handler - captures everything (DEBUG and above)
    file_handler = logging.FileHandler(log_filepath)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    # Console handler - only shows INFO and above
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    return logger

def load_tickers(filename='symbols.json'):
    """Load ticker symbols from JSON file"""
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading tickers from {filename}: {str(e)}")
        return []

def process_symbols(df, num_symbols=None):
    """Process selected number of symbols"""
    total_available = len(df)
    logger.info(f"\nTotal price-filtered symbols available: {total_available}")
    
    if num_symbols is None:
        user_input = input("Enter number of symbols to process (press Enter for all, or enter a number): ").strip()
        if user_input:
            try:
                num_symbols = int(user_input)
                if num_symbols > total_available:
                    num_symbols = total_available
                    logger.info(f"Adjusted to maximum available symbols: {num_symbols}")
            except ValueError:
                logger.error("Invalid input. Processing all symbols.")
                num_symbols = total_available
        else:
            num_symbols = total_available
    
    if num_symbols < total_available:
        selected_df = df.sample(n=num_symbols)
        logger.info(f"Randomly selected {num_symbols} symbols for processing.")
    else:
        selected_df = df
    
    logger.info(f"Will process {len(selected_df)} symbols.")
    
    # Ask for confirmation
    user_input = input("\nPress Enter to continue or 'q' to quit: ")
    if user_input.lower() == 'q':
        return None
    
    return selected_df

########################
# Main Execution Flow
########################

def main():
    """Main function"""
    logger = setup_logging()
    logger.info("Starting EOH Screener...")
    
    # 1. Load and validate data
    all_tickers = load_tickers()
    logger.info(f"Loaded {len(all_tickers)} tickers from symbols.json")
    
    # 2. Get price data
    cache_df = load_cached_data()
    if not is_cache_valid(cache_df):
        logger.info("Cache is invalid or too old - fetching all data...")
        cache_df = fetch_latest_closes_yf(all_tickers)
    
    # 3. Apply initial price filter
    df_filtered = cache_df[
        (cache_df["Last_Close"] > MIN_PRICE) &
        (cache_df["Last_Close"] < MAX_PRICE)
    ].copy()
    df_filtered.reset_index(drop=True, inplace=True)
    total_filtered = len(df_filtered)
    logger.info(f"After price filtering (${MIN_PRICE} - ${MAX_PRICE}), we have {total_filtered} tickers.")
    
    # Initialize rate limiter
    rate_limiter = RateLimiter(max_requests=850, time_window=60)
    
    # 4. Start interactive menu
    selected_df, min_price, max_price = price_filter_loop(df_filtered)
    
    if selected_df is None:
        logger.info("Exiting...")
        return
    
    if selected_df.empty:
        logger.error("No symbols selected!")
        return
    
    # 5. Process technical indicators for selected symbols
    if selected_df is not None and not selected_df.empty:
        process_technical_indicators(selected_df, rate_limiter, min_price, max_price)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, ValueError))
)
def fetch_technical_data(ticker: str, function: str, params: dict = None) -> pd.DataFrame:
    """Fetch technical indicator data from EODHD API with retry logic"""
    base_url = f"https://eodhd.com/api/technical/{ticker}"
    
    # Default parameters
    request_params = {
        "api_token": api_token,
        "function": function,
        "order": "d",  # descending
        "fmt": "json"
    }
    
    # Update with any additional parameters
    if params:
        request_params.update(params)
    
    try:
        logger.debug(f"\nFetching {function} for {ticker}")
        logger.debug(f"URL: {base_url}")
        logger.debug(f"Params: {request_params}")
        
        r = requests.get(base_url, params=request_params)
        r.raise_for_status()
        data = r.json()
        
        # Debug log the raw MACD data
        if function == 'macd':
            logger.info(f"\nMACD Raw Response for {ticker}:")
            logger.info(json.dumps(data[:2] if isinstance(data, list) else data, indent=2))
        
        df = pd.DataFrame(data)
        
        # Debug log the DataFrame columns and first row
        if function == 'macd':
            logger.info(f"MACD DataFrame columns for {ticker}: {df.columns.tolist()}")
            logger.info(f"First row of MACD data:\n{df.iloc[0].to_dict()}")
        
        return df
        
    except Exception as e:
        logger.error(f"Error fetching {function} for {ticker}: {str(e)}")
        return pd.DataFrame()

if __name__ == "__main__":
    main()
