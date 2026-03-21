import gymnasium as gym
import pandas as pd
import numpy as np
from tqdm import tqdm
import logging
from alpha_vantage.timeseries import TimeSeries
import argparse
from trading_algorithms import TradingAlgorithm
import os
import time  # Import the time module for delay

from logging.handlers import RotatingFileHandler

# Set up logging
log_filename = "grid_trading.log"

# check if the file exists and if so delete it
if os.path.exists(log_filename):
    os.remove(log_filename)

# Create a rotating file handler to save logs to a file
file_handler = RotatingFileHandler(
    log_filename, maxBytes=10 * 1024 * 1024, backupCount=5
)
file_handler.setLevel(logging.DEBUG)

# Create a logging format
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

# Get the root logger and configure it
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)

# Optionally, you can also print logs to the console by adding a console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(
    logging.DEBUG
)  # Change to DEBUG to log everything to the console as well
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

logger.info("Logging setup complete. Logs will be saved to grid_trading.log.")


# Define a simple trading environment
class TradingEnv(gym.Env):
    """
    A custom trading environment for reinforcement learning.
    """

    def __init__(self, data):
        """
        Initialize the trading environment.

        Parameters:
            data (pd.DataFrame): Historical stock data.
        """
        super(TradingEnv, self).__init__()
        self.data = data
        self.action_space = gym.spaces.Discrete(3)  # Buy, Sell, Hold
        self.observation_space = gym.spaces.Box(
            low=0, high=1, shape=(5,), dtype=np.float32
        )
        self.reset()

    def reset(self):
        """
        Reset the environment to the initial state.
        """
        self.current_step = 0
        self.balance = 4000  # Starting cash balance
        self.shares_held = 0
        self.net_worth = self.balance  # Initial net worth is the starting cash balance
        self.history = []  # History of steps for charting
        return self._get_obs()

    def step(self, action, shares_to_trade=0):
        """
        Take a step in the environment based on the given action.
        """
        current_price = self.data.iloc[self.current_step]["Close"]
        previous_net_worth = self.net_worth

        # Execute the trade
        shares_traded = 0
        if action == 0 and self.balance >= current_price:  # Buy
            shares_to_trade = min(int(self.balance // current_price), shares_to_trade)
            self.balance -= shares_to_trade * current_price
            self.shares_held += shares_to_trade
            shares_traded = shares_to_trade
        elif action == 1 and self.shares_held > 0:  # Sell
            shares_to_trade = min(self.shares_held, shares_to_trade)
            self.balance += shares_to_trade * current_price
            self.shares_held -= shares_to_trade
            shares_traded = shares_to_trade

        # Move to the next timestep
        self.current_step += 1
        done = self.current_step >= len(self.data) - 1 or self.balance <= 0

        # Calculate the new net worth
        self.net_worth = self.balance + self.shares_held * current_price

        # Calculate reward as the change in net worth
        reward = self.net_worth - previous_net_worth

        # Log history for charting
        self.history.append(
            (
                self.current_step,
                current_price,
                self.shares_held,
                self.balance,
                self.net_worth,
                action,
                shares_traded,
                reward,
            )
        )
        # Introduce a 2-second delay after each step
        # time.sleep(8)

        return self._get_obs(), reward, done, shares_traded

    def _get_obs(self):
        """
        Get the current observation.
        """
        return np.array(
            [
                self.balance / 10000,
                self.shares_held / 100,
                self.data.iloc[self.current_step]["Open"] / self.data.iloc[0]["Open"],
                self.data.iloc[self.current_step]["High"] / self.data.iloc[0]["Open"],
                self.data.iloc[self.current_step]["Low"] / self.data.iloc[0]["Open"],
            ]
        )


def load_data(symbol, start_date, end_date, period=None, history_lookback=None):
    """
    Load historical stock data from Alpha Vantage with optional adjusted start date.

    Parameters:
        symbol (str): The stock symbol.
        start_date (str): The start date for trading (YYYY-MM-DD).
        end_date (str): The end date for data (YYYY-MM-DD).
        period (int or None): The period for calculations. Optional.
        history_lookback (int or None): The number of days for historical context. Optional.

    Returns:
        pd.DataFrame: The historical stock data.
    """
    # Convert dates to datetime objects
    start_date = pd.to_datetime(start_date)
    end_date = pd.to_datetime(end_date)

    # Calculate the adjusted start date if period or history_lookback is needed
    if period or history_lookback:
        adjusted_start_date = start_date - pd.Timedelta(
            days=max(period or 0, history_lookback or 0)
        )
        logger.info(f"Original start date: {start_date}, End date: {end_date}")
        logger.info(f"Adjusted start date: {adjusted_start_date.strftime('%Y-%m-%d')}")
    else:
        adjusted_start_date = start_date  # No need to pull extra historical data

        logger.info(
            f"No adjustment to start date. Using start date: {start_date.strftime('%Y-%m-%d')}"
        )

    api_key = "J1KYL6ZGONER2A62"  # Replace with your API key
    ts = TimeSeries(key=api_key, output_format="pandas")
    data, meta_data = ts.get_daily(symbol=symbol, outputsize="full")

    # Log the full range of data pulled from Alpha Vantage
    logger.info(f"Data available from {data.index.min()} to {data.index.max()}")

    # Filter data between adjusted start date and end date
    data = data[(data.index >= adjusted_start_date) & (data.index <= end_date)]

    # Log the final date range being used for calculations
    logger.info(f"Data filtered from {data.index.min()} to {data.index.max()}")

    # Rename and sort columns
    data.rename(
        columns={
            "1. open": "Open",
            "2. high": "High",
            "3. low": "Low",
            "4. close": "Close",
            "5. volume": "Volume",
        },
        inplace=True,
    )
    data = data.sort_index()  # Ensure data is sorted by date

    # Log the final range of data pulled
    logger.info(f"Data pulled from {data.index.min()} to {data.index.max()}")

    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grid Trading Example with Gym")

    # Existing flags
    parser.add_argument(
        "--symbol", type=str, default="F", help="Stock symbol to trade (default: F)"
    )
    parser.add_argument(
        "--start_date", type=str, help="Start date for data (format: YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end_date", type=str, help="End date for data (format: YYYY-MM-DD)"
    )
    parser.add_argument(
        "--grid_distance",
        type=float,
        default=0.10,
        help="Distance between grid lines (default: 0.005)",
    )
    parser.add_argument(
        "--grid_range",
        type=float,
        default=0.1,
        help="Total grid range above and below the midpoint (default: 0.1)",
    )
    parser.add_argument(
        "--history_lookback",
        type=int,
        default=30,
        help="Number of days to look back for historical price analysis when auto-adjusting grid (default: 30 days)",
    )
    parser.add_argument(
        "--method",
        type=str,
        default="market_price",
        help="Method to calculate the midprice (options: 'market_price', 'moving_average', 'mid_high_low', 'vwap')",
    )
    parser.add_argument(
        "--period",
        type=int,
        default=20,
        help="Period for midprice calculation methods that require it (default: 20)",
    )
    parser.add_argument(
        "--dynamic_midprice", action="store_true", help="If set, use a dynamic midprice"
    )
    parser.add_argument(
        "--midprice_adjust_period",
        type=int,
        default=5,
        help="Number of steps (days) after which the midprice is dynamically adjusted (default: 5)",
    )
    parser.add_argument(
        "--auto_adjust",
        action="store_true",
        help="If set, automatically adjust grid lines based on historical high/low prices.",
    )
    parser.add_argument(
        "--allocation_strategy",
        type=str,
        default="fixed_percentage",
        choices=[
            "fixed_percentage",
            "fixed_number",
            "dynamic_allocation",
            "proportional_allocation",
            "volatility_based",
        ],
        help="Choose the allocation strategy for trading.",
    )
    parser.add_argument(
        "--allocation_param",
        type=float,
        help="Parameter for the selected allocation strategy (e.g., percentage for 'fixed_percentage').",
    )
    parser.add_argument(
        "--volatility_threshold",
        type=float,
        default=0.02,
        help="Threshold for volatility-based allocation (default: 0.02).",
    )
    parser.add_argument(
        "--entry_multiplier",
        type=float,
        default=0.7,
        help="Multiplier for the first buy (market entry). Default is 2.",
    )
    parser.add_argument(
        "--range",
        type=int,
        help="Number of days to hold after entering the market before triggering a sell action.",
    )
    parser.add_argument(
        "--range_hold",
        action="store_true",
        help="If set, hold the stock after the range period if the reward remains positive until it turns negative or zero.",
    )
    parser.add_argument(
        "--plot_chart",
        action="store_true",
        help="If set, generates and displays an interactive chart of the trading simulation.",
    )
    parser.add_argument(
        "--deviation_threshold",
        type=float,
        default=0.5,
        help="Threshold for deviation from the midprice that triggers recalculation (default: 0.5).",
    )

    # New flags related to auto-adjust
    parser.add_argument(
        "--auto_adjust_cycle_period",
        action="store_true",
        help="Automatically adjust the grid based on the dominant cycle period using HT_DCPERIOD.",
    )
    parser.add_argument(
        "--auto_adjust_indicators",
        action="store_true",
        help=(
            "Automatically adjust the grid based on technical indicators, "
            "including the following: "
            "1. ATR (Average True Range): Used to measure volatility and adjust grid distance accordingly. "
            "Higher volatility results in wider grid lines, while lower volatility results in narrower grid lines. "
            "2. RSI (Relative Strength Index): A momentum oscillator that measures the speed and change of price movements. "
            "RSI can help identify overbought or oversold conditions, adjusting grid placement based on these conditions. "
            "3. Bollinger Bands: A volatility indicator using a moving average and standard deviation. "
            "The upper and lower bands can influence the grid by dynamically adjusting the grid range around price levels. "
            "4. Cycle Indicators (HT_DCPERIOD and HT_DCPHASE): Hilbert Transform cycle indicators to identify dominant cycles "
            "in price movement. These are used to fine-tune grid parameters to align with the detected market cycles. "
            "5. SMA (Simple Moving Average) and EMA (Exponential Moving Average): These are used to identify trends and smooth "
            "price data. The grid can be adjusted to follow the general trend identified by these moving averages. "
            "6. Standard Deviation: Measures the dispersion of price data. Higher standard deviations indicate more volatile periods, "
            "and the grid can expand or contract based on this volatility measure."
        ),
    )
    parser.add_argument(
        "--auto_grid_scaling",
        action="store_true",
        help="Use logarithmic scaling for grid lines when auto-adjusting the grid.",
    )
    parser.add_argument(
        "--dynamic_deviation_threshold",
        action="store_true",
        help="Dynamically adjust the deviation threshold for midprice and grid recalculation based on market volatility.",
    )
    parser.add_argument(
        "--indicator_adjust_period",
        type=int,
        default=5,
        help="Number of steps after which indicators-based grid adjustments are applied (default: 5 steps).",
    )

    args = parser.parse_args()

    # Pass period and history_lookback to load_data
    data = load_data(
        args.symbol, args.start_date, args.end_date, args.period, args.history_lookback
    )

    if data.empty:
        logger.error("No data retrieved. Exiting.")
    else:
        # Create the Gym environment
        env = TradingEnv(data)
        algo = TradingAlgorithm(env)

        # Automatically set period based on cycle if auto_period is enabled
        if args.auto_adjust_cycle_period:
            period = algo.auto_set_period(data)
        else:
            period = args.period

        if args.auto_adjust:
            # Automatically set grid parameters based on historical data
            grid_range, grid_distance = algo.auto_set_grid_parameters(period=period)
            grid = algo.generate_grid(None, grid_distance, grid_range, auto_adjust=True)
            midprice = grid[len(grid) // 2]  # Set midprice to the midpoint of the grid
        else:
            # Ensure midprice is calculated before generating the grid
            midprice = algo.calculate_midprice(
                method=args.method, period=period, auto_adjust=False
            )

            grid_distance = args.grid_distance
            grid_range = args.grid_range

            grid = algo.generate_grid(
                midprice, grid_distance, grid_range, auto_adjust=False
            )
        # Now `grid` and `midprice` should be properly defined
        logger.info(f"Calculated midprice: {midprice}")
        logger.info(f"Generated grid lines: {grid}")

        # Use logarithmic scaling if auto_grid_scaling is enabled
        if args.auto_grid_scaling:
            grid = algo.generate_logarithmic_grid(
                midprice, historical_high, historical_low
            )
        else:
            # Generate the grid based on the midprice, grid distance, and range
            grid = algo.generate_grid(midprice, grid_distance, grid_range)

        total_reward = algo.grid_trading(
            grid_distance=grid_distance,
            method=args.method,
            grid_range=grid_range,
            period=period,
            dynamic_midprice=args.dynamic_midprice,
            midprice_adjust_period=args.midprice_adjust_period,
            auto_adjust=args.auto_adjust,
            allocation_strategy=args.allocation_strategy,
            allocation_param=args.allocation_param,
            volatility_threshold=args.volatility_threshold,
            entry_multiplier=args.entry_multiplier,
            range_days=args.range,  # Pass the range parameter
            range_hold=args.range_hold,  # Pass the range-hold flag
            deviation_threshold=args.dynamic_deviation_threshold,  # Dynamic deviation threshold flag
        )

        logger.info(f"Total reward: {total_reward:.2f}")

        # Display the interactive chart if requested
        if args.plot_chart:
            algo.plot_chart(volumes=env.data["Volume"].tolist())
