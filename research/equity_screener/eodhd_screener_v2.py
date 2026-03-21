import os
import sys
import json
import logging
from datetime import datetime, timedelta
from collections import deque
import pandas as pd
import requests
import yfinance as yf
from tqdm import tqdm
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ============================
# Utility Functions
# ============================

def ensure_cache_dir():
    """Ensure cache directory exists"""
    cache_dir = os.path.join(os.path.dirname(__file__), '.cache')
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

def load_failed_tickers(failed_path='failed_tickers.json'):
    """Load failed tickers from a JSON file"""
    if os.path.exists(failed_path):
        try:
            with open(failed_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading failed tickers: {e}")
            return {'invalid_period': [], 'delisted': [], 'other_errors': []}
    return {'invalid_period': [], 'delisted': [], 'other_errors': []}

def save_failed_tickers(failed_dict, failed_path='failed_tickers.json'):
    """Save failed tickers to a JSON file"""
    try:
        with open(failed_path, 'w') as f:
            json.dump(failed_dict, f, indent=2)
        logging.info(f"Saved failed tickers to {failed_path}")
    except Exception as e:
        logging.error(f"Error saving failed tickers: {e}")

def load_tickers(filename='symbols.json'):
    """Load ticker symbols from JSON file"""
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading tickers from {filename}: {str(e)}")
        return []

def load_cached_data(cache_file='stock_prices_cache.csv'):
    """Load previously cached stock data"""
    cache_dir = ensure_cache_dir()
    cache_path = os.path.join(cache_dir, cache_file)
    
    if os.path.exists(cache_path):
        try:
            cache_age = pd.Timestamp.now() - pd.Timestamp.fromtimestamp(os.path.getmtime(cache_path))
            logging.info(f"Found cache file created {cache_age} ago")
            df = pd.read_csv(cache_path)
            logging.info(f"Cache contains {len(df)} symbols")
            return df
        except Exception as e:
            logging.error(f"Error loading cache: {e}")
            return pd.DataFrame(columns=["Ticker", "Last_Close", "Timestamp"])
    
    logging.info("No cache file found")
    return pd.DataFrame(columns=["Ticker", "Last_Close", "Timestamp"])

def save_to_cache(df, filename='stock_prices_cache.csv'):
    """Save the current stock data to cache"""
    cache_dir = ensure_cache_dir()
    cache_file = os.path.join(cache_dir, filename)
    
    try:
        logging.info(f"Saving data to cache: {cache_file}")
        df.to_csv(cache_file, index=False)
    except Exception as e:
        logging.error(f"Error saving to cache: {e}")

# ============================
# Logging Configuration
# ============================

def setup_logging():
    """Configure logging to both file and console with different levels"""
    cache_dir = ensure_cache_dir()
    logs_dir = os.path.join(cache_dir, 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    log_filename = f"eohd_screeener_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_filepath = os.path.join(logs_dir, log_filename)
    
    # Create formatters
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_formatter = logging.Formatter('%(message)s')  # Simplified format for console
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers
    if logger.hasHandlers():
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

# ============================
# Data Fetching Functions
# ============================

def get_missing_tickers(cached_df, all_tickers):
    """Compare cached tickers with all tickers to find missing ones"""
    if cached_df.empty:
        logging.info("Cache is empty, all tickers will be fetched")
        return all_tickers
    
    cached_tickers = set(cached_df['Ticker'])
    all_tickers_set = set(all_tickers)
    missing_tickers = list(all_tickers_set - cached_tickers)
    
    if missing_tickers:
        logging.info(f"Found {len(missing_tickers)} missing symbols: {', '.join(missing_tickers[:5])}...")
        if len(missing_tickers) > 5:
            logging.info(f"... and {len(missing_tickers) - 5} more")
    else:
        logging.info("Cache is complete - no missing symbols")
    
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

def fetch_latest_closes_yf(tickers, chunk_size=50, cache_filename='stock_prices_cache.csv'):
    """Fetch latest closing prices from Yahoo Finance with error handling and caching"""
    logger = logging.getLogger()
    logger.info(f"Fetching data for {len(tickers)} symbols in chunks of {chunk_size}")
    
    # Load cache and failed tickers
    cached_df = load_cached_data(cache_filename)
    previous_failed = load_failed_tickers()
    current_time = pd.Timestamp.now()
    
    # Identify which tickers actually need to be fetched
    cached_tickers = set(cached_df[
        cached_df['Timestamp'].notna() & 
        (pd.to_datetime(cached_df['Timestamp']) > (current_time - pd.Timedelta(days=1)))
    ]['Ticker'])
    
    delisted_tickers = set(previous_failed.get('delisted', []))
    invalid_period_tickers = set(previous_failed.get('invalid_period', []))
    other_error_tickers = set(previous_failed.get('other_errors', []))
    
    skip_tickers = cached_tickers | delisted_tickers | invalid_period_tickers | other_error_tickers
    tickers_to_fetch = [t for t in tickers if t not in skip_tickers]
    
    if not tickers_to_fetch:
        logger.info("✓ No new tickers to fetch - using existing data")
        return cached_df
    
    logger.info(f"➜ Fetching {len(tickers_to_fetch)} new tickers")
    logger.debug(f"First few tickers to fetch: {', '.join(tickers_to_fetch[:5])}...")
    
    # Initialize results and failed tracking
    results = []
    failed_dict = {
        'invalid_period': list(invalid_period_tickers),
        'delisted': list(delisted_tickers),
        'other_errors': list(other_error_tickers)
    }
    
    # Process in chunks
    chunks = [tickers_to_fetch[i:i + chunk_size] for i in range(0, len(tickers_to_fetch), chunk_size)]
    
    for chunk_idx, chunk in enumerate(tqdm(chunks, desc=f"Fetching {len(tickers_to_fetch)} missing tickers")):
        try:
            data = yf.download(
                chunk,
                period="1d",
                interval="1d",
                group_by="ticker",
                progress=False,
                ignore_tz=True
            )
            
            # Process each ticker in the chunk
            for ticker in chunk:
                try:
                    if len(chunk) == 1:  # Single ticker case
                        ticker_data = data
                    else:
                        ticker_data = data[ticker]
                    
                    if not isinstance(ticker_data, pd.DataFrame) or ticker_data.empty:
                        raise ValueError("No data found")
                    
                    close_price = ticker_data['Close'].iloc[-1]
                    
                    if pd.notna(close_price):
                        results.append({
                            'Ticker': ticker,
                            'Last_Close': float(close_price),
                            'Timestamp': current_time
                        })
                    else:
                        failed_dict['other_errors'].append(ticker)
                        logger.warning(f"No 'Close' data for {ticker}")
                
                except Exception as e:
                    error_str = str(e)
                    if "Period '1d' is invalid" in error_str:
                        failed_dict['invalid_period'].append(ticker)
                    elif "No data found" in error_str or "Could not find a valid URL" in error_str:
                        failed_dict['delisted'].append(ticker)
                    else:
                        failed_dict['other_errors'].append(ticker)
                    logger.error(f"Error processing {ticker}: {error_str}")
            
            # Save progress after each chunk
            if results:
                temp_df = pd.DataFrame(results)
                combined_df = pd.concat([cached_df, temp_df]).drop_duplicates('Ticker', keep='last')
                save_to_cache(combined_df, cache_filename)
                logger.debug(f"Saved progress: {len(results)} new tickers processed")
                results.clear()  # Clear results after saving
        
        except Exception as e:
            logger.error(f"Chunk {chunk_idx + 1} failed: {str(e)}")
            failed_dict['other_errors'].extend(chunk)
    
    # Combine all results
    final_df = pd.concat([cached_df, pd.DataFrame(results)]).drop_duplicates('Ticker', keep='last')
    
    # Save updated failed tickers
    save_failed_tickers(failed_dict)
    
    # Final status report
    logger.info("\n✓ Final results:")
    logger.info(f"  - Total symbols: {len(final_df)}")
    logger.info(f"  - New symbols added: {len(results)}")
    logger.info(f"  - Failed downloads: {sum(len(v) for v in failed_dict.values())}")
    
    return final_df

# ============================
# Symbol Processing Functions
# ============================

def process_symbols(df, num_symbols=None):
    """Process selected number of symbols"""
    total_available = len(df)
    logger = logging.getLogger()
    logger.info(f"\nTotal price-filtered symbols available: {total_available}")
    
    if num_symbols is None:
        user_input = input("Enter number of symbols to process (press Enter for all, or enter a number): ").strip()
        if user_input:
            try:
                num_symbols = int(user_input)
                if num_symbols > total_available:
                    logger.info(f"Requested {num_symbols} but only {total_available} available. Using all symbols.")
                    num_symbols = total_available
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
    user_input = input("\nPress Enter to continue or 'q' to quit: ").strip().lower()
    if user_input == 'q':
        logger.info("Exiting program.")
        sys.exit()
    
    return selected_df

# ============================
# Technical Indicators Fetching
# ============================

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

def process_technical_indicators(df_filtered):
    """Process technical indicators for selected symbols"""
    logger = logging.getLogger()
    logger.info("\n=== Starting New Data Collection Run ===")
    logger.info(f"Total tickers to process: {len(df_filtered)}")
    
    rate_limiter = RateLimiter(max_requests=850, time_window=60)
    
    results = []
    progress_bar = tqdm(df_filtered.iterrows(), total=df_filtered.shape[0], desc="Fetching Technical Indicators")
    
    for idx, row in progress_bar:
        ticker = row['Ticker']
        price = row['Last_Close']
        logger.info(f"\nProcessing ticker: {ticker}")
        logger.info(f"Last Close: ${price:.2f}")
        
        try:
            # Example parameters, adjust as needed
            api_token = "YOUR_API_TOKEN"
            rsi_data = get_technical_indicator(ticker, 'rsi', {'period': 14}, api_token)
            atr_data = get_technical_indicator(ticker, 'atr', {'period': 14}, api_token)
            macd_data = get_technical_indicator(ticker, 'macd', {}, api_token)
            
            # Extract latest values
            latest_rsi = rsi_data['rsi'].iloc[0] if not rsi_data.empty else None
            latest_atr = atr_data['atr'].iloc[0] if not atr_data.empty else None
            latest_macd = macd_data['macd'].iloc[0] if not macd_data.empty else None
            latest_macd_signal = macd_data['signal'].iloc[0] if not macd_data.empty else None
            latest_macd_hist = macd_data['histogram'].iloc[0] if not macd_data.empty else None
            
            results.append({
                'Ticker': ticker,
                'Last_Close': price,
                'latest_rsi': latest_rsi,
                'latest_atr': latest_atr,
                'latest_macd': latest_macd,
                'latest_macd_signal': latest_macd_signal,
                'latest_macd_hist': latest_macd_hist
            })
            
        except Exception as e:
            logger.error(f"Error processing technical indicators for {ticker}: {str(e)}")
            results.append({
                'Ticker': ticker,
                'Last_Close': price,
                'latest_rsi': None,
                'latest_atr': None,
                'latest_macd': None,
                'latest_macd_signal': None,
                'latest_macd_hist': None
            })
    
    # Create final DataFrame
    final_df = pd.DataFrame(results)
    
    # Save results
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cache_dir = ensure_cache_dir()
    output_file = os.path.join(cache_dir, f'analysis_results_{timestamp}.csv')
    final_df.to_csv(output_file, index=False)
    logger.info(f"\nResults saved to: {output_file}")
    
    return final_df

# ============================
# Interactive Price Filtering
# ============================

def price_filter_loop(cache_df):
    """Interactive price filtering loop"""
    filtered_df = cache_df.copy()
    
    while True:
        # Show current data stats
        total_symbols = len(filtered_df)
        price_min = filtered_df['Last_Close'].min()
        price_max = filtered_df['Last_Close'].max()
        price_avg = filtered_df['Last_Close'].mean()
        price_median = filtered_df['Last_Close'].median()
        
        print(f"\nTotal symbols available: {len(cache_df)}")
        print(f"Currently filtered symbols: {total_symbols}")
        print(f"Current price range: ${price_min:.2f} - ${price_max:.2f}")
        print(f"Average price: ${price_avg:.2f}")
        print(f"Median price: ${price_median:.2f}")
        
        print("\nPrice Filter Options:")
        print("1. Use default filter ($5 - $45)")
        print("2. Set custom price range")
        print("3. Return with current selection")
        print("4. Reset filters")
        print("5. Exit")
        
        choice = input("\nEnter your choice (1-5): ").strip()
        
        if choice == '1':
            min_price = 5
            max_price = 45
            new_filtered = cache_df[
                (cache_df['Last_Close'] >= min_price) & 
                (cache_df['Last_Close'] <= max_price)
            ]
            logger = logging.getLogger()
            logger.info(f"Applied default price filter: ${min_price} - ${max_price}")
        
        elif choice == '2':
            try:
                min_price = float(input("Enter minimum price: $"))
                max_price = float(input("Enter maximum price: $"))
                new_filtered = cache_df[
                    (cache_df['Last_Close'] >= min_price) & 
                    (cache_df['Last_Close'] <= max_price)
                ]
                logger = logging.getLogger()
                logger.info(f"Applied custom price filter: ${min_price} - ${max_price}")
            except ValueError:
                print("Invalid input! Please enter valid numbers.")
                continue
                
        elif choice == '3':
            if filtered_df.empty:
                print("No symbols selected. Please apply a filter first.")
                continue
            else:
                return filtered_df
        
        elif choice == '4':
            filtered_df = cache_df.copy()
            logger = logging.getLogger()
            logger.info("Filters reset to original data.")
            print("Filters have been reset to include all symbols.")
            continue
        
        elif choice == '5':
            print("Exiting program.")
            sys.exit()
        
        else:
            print("Invalid choice! Please try again.")
            continue
        
        # Show results of new filter
        filtered_count = len(new_filtered)
        print(f"\nFilter resulted in {filtered_count} symbols.")
        print(f"Price range after filter: ${new_filtered['Last_Close'].min():.2f} - ${new_filtered['Last_Close'].max():.2f}")
        
        # Show price distribution
        percentiles = [0, 25, 50, 75, 100]
        price_dist = new_filtered['Last_Close'].describe(percentiles=[p/100 for p in percentiles])
        print("\nPrice Distribution:")
        for p in percentiles:
            print(f"{p}th percentile: ${price_dist[f'{p}%']:.2f}")
        
        # Confirm filter
        while True:
            confirm = input("\nAccept this filter? (y/n): ").lower().strip()
            if confirm == 'y':
                filtered_df = new_filtered
                print(f"Filter accepted. {filtered_count} symbols selected.")
                break
            elif confirm == 'n':
                print("Filter not accepted. Returning to filter options.")
                break
            else:
                print("Please enter 'y' or 'n'.")

# ============================
# Main Function
# ============================

def main():
    """Main function"""
    # Setup logging first
    logger = setup_logging()
    logger.info("Starting EOH Screener...")
    
    # Load all tickers first
    all_tickers = load_tickers()
    logger.info(f"Loaded {len(all_tickers)} tickers from symbols.json")
    
    if not all_tickers:
        logger.error("No tickers loaded. Exiting.")
        sys.exit()
    
    # Load cached data
    cache_df = load_cached_data()
    
    # Check cache validity
    if is_cache_valid(cache_df):
        logger.info("Cache is valid (less than 24 hours old)")
        missing_tickers = get_missing_tickers(cache_df, all_tickers)
        
        if missing_tickers:
            logger.info(f"Fetching only {len(missing_tickers)} missing tickers")
            new_data = fetch_latest_closes_yf(missing_tickers)
            if not new_data.empty:
                cache_df = pd.concat([cache_df, new_data]).drop_duplicates('Ticker', keep='last')
                save_to_cache(cache_df)
            else:
                logger.info("No new data fetched.")
        else:
            logger.info("Using complete cached data - no API calls needed")
    else:
        logger.info("Cache is invalid or too old - fetching all data...")
        cache_df = fetch_latest_closes_yf(all_tickers)
    
    logger.info(f"Fetched Yahoo prices for {len(cache_df)} tickers.")
    
    # Interactive price filtering
    filtered_df = price_filter_loop(cache_df)
    
    if filtered_df is None:
        logger.info("Exiting...")
        sys.exit()
    
    if filtered_df.empty:
        logger.error("No symbols found in the specified price range!")
        sys.exit()
        
    # Process symbols
    selected_df = process_symbols(filtered_df)
    if selected_df is not None and not selected_df.empty:
        # Continue with technical analysis
        process_technical_indicators(selected_df)
    else:
        logger.error("No symbols selected for processing.")

if __name__ == "__main__":
    main()