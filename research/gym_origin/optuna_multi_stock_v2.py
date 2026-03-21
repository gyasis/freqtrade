import os
import pandas as pd
import numpy as np
import gymnasium as gym
import optuna
from tqdm import tqdm
import logging
from logging.handlers import RotatingFileHandler
from alpha_vantage.timeseries import TimeSeries
from trading_algorithms import TradingAlgorithm
import json

# Setup logging
log_filename = "optuna_trading.log"
if os.path.exists(log_filename):
    os.remove(log_filename)  # Remove the log file if it exists

file_handler = RotatingFileHandler(
    log_filename, maxBytes=10 * 1024 * 1024, backupCount=5
)
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)

# Add a NullHandler to suppress console output
null_handler = logging.NullHandler()
logger.addHandler(null_handler)

logger.info("Logging setup complete. Logs will be saved to optuna_trading.log.")


# Save data to avoid multiple downloads
def download_data(symbol, start_date, end_date, api_key):
    file_path = f"data/{symbol}_{start_date}_{end_date}.csv"

    if os.path.exists(file_path):
        logger.info(f"Loading saved data for {symbol} from {file_path}")
        data = pd.read_csv(file_path, index_col=0, parse_dates=True)
    else:
        logger.info(f"Downloading data for {symbol} from Alpha Vantage API")
        ts = TimeSeries(key=api_key, output_format="pandas")
        data, _ = ts.get_daily(symbol=symbol, outputsize="full")

        # Filter the data to the desired date range
        data = data[(data.index >= start_date) & (data.index <= end_date)]

        os.makedirs("data", exist_ok=True)  # Ensure the directory exists
        data.to_csv(file_path)

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
    return data.sort_index()


# Define the Trading Environment
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

        # Log the internal state
        logger.debug(
            f"Step: {self.current_step}, Action: {action}, Price: {current_price}, "
            f"Balance: {self.balance}, Shares Held: {self.shares_held}, Net Worth: {self.net_worth}, "
            f"Reward: {reward}"
        )

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


def objective(trial, symbol, year, api_key):
    try:
        # Suggest hyperparameters
        grid_distance = trial.suggest_float("grid_distance", 0.1, 20)
        grid_range = trial.suggest_float("grid_range", 0.50, 30)
        history_lookback = trial.suggest_int("history_lookback", 10, 90)
        method = trial.suggest_categorical(
            "method", ["market_price", "moving_average", "mid_high_low", "vwap"]
        )
        period = trial.suggest_int("period", 10, 30)
        dynamic_midprice = trial.suggest_categorical("dynamic_midprice", [True, False])
        midprice_adjust_period = trial.suggest_int("midprice_adjust_period", 3, 30)
        auto_adjust = trial.suggest_categorical("auto_adjust", [True, False])
        allocation_strategy = trial.suggest_categorical(
            "allocation_strategy",
            [
                "fixed_percentage",
                "dynamic_allocation",
                "proportional_allocation",
                "volatility_based",
            ],
        )
        allocation_param = (
            trial.suggest_float("allocation_param", 0.3, 1)
            if allocation_strategy == "fixed_percentage"
            else None
        )
        volatility_threshold = trial.suggest_float("volatility_threshold", 0.1, 0.5)
        entry_multiplier = trial.suggest_int("entry_multiplier", 1, 5)
        range_days = trial.suggest_int("range_days", 5, 30)
        range_hold = trial.suggest_categorical("range_hold", [True, False])
        deviation_threshold = trial.suggest_float("deviation_threshold", 0.5, 2.0)

        # Define available indicators
        indicators = {
            "ATR": trial.suggest_categorical("ATR", [True, False]),
            "RSI": trial.suggest_categorical("RSI", [True, False]),
            "BollingerBands": trial.suggest_categorical(
                "BollingerBands", [True, False]
            ),
            "CyclePeriod": trial.suggest_categorical(
                "CyclePeriod", [True, False]
            ),  # HT_DCPERIOD
            "CyclePhase": trial.suggest_categorical(
                "CyclePhase", [True, False]
            ),  # HT_DCPHASE
            "TrendMode": trial.suggest_categorical(
                "TrendMode", [True, False]
            ),  # HT_TRENDMODE
            "SMA": trial.suggest_categorical("SMA", [True, False]),
            "EMA": trial.suggest_categorical("EMA", [True, False]),
            "StdDev": trial.suggest_categorical("StdDev", [True, False]),
        }

        # Additional auto parameters
        auto_set_grid = trial.suggest_categorical("auto_set_grid", [True, False])
        auto_set_period = trial.suggest_categorical("auto_set_period", [True, False])
        recoup_cooldown_period = trial.suggest_int("recoup_cooldown_period", 1, 10)
        exit_threshold = trial.suggest_float("exit_threshold", 0.01, 0.10)
        reevaluate_grid = trial.suggest_categorical("reevaluate_grid", [True, False])

        # Log the parameters including selected indicators
        selected_indicators = [
            name for name, selected in indicators.items() if selected
        ]
        logger.info(
            f"Running trial with parameters: grid_distance={grid_distance}, grid_range={grid_range}, "
            f"history_lookback={history_lookback}, method={method}, period={period}, dynamic_midprice={dynamic_midprice}, "
            f"midprice_adjust_period={midprice_adjust_period}, auto_adjust={auto_adjust}, "
            f"allocation_strategy={allocation_strategy}, allocation_param={allocation_param}, "
            f"volatility_threshold={volatility_threshold}, entry_multiplier={entry_multiplier}, "
            f"range_days={range_days}, range_hold={range_hold}, deviation_threshold={deviation_threshold}, "
            f"indicators={selected_indicators}"
        )

        # Set the start and end dates based on the year
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31" if year < 2024 else "2024-08-01"

        # Load historical data
        data = download_data(symbol, start_date, end_date, api_key)

        # Create Gymnasium environment and trading algorithm
        env = TradingEnv(data)
        algo = TradingAlgorithm(env)

        grid_range, grid_distance = algo.auto_set_grid_parameters(
            period=period,
            selected_indicators=selected_indicators,
            auto_set_period=auto_set_period,
        )

        # number of rounds in Gym we are running only 1 round but can change this later with more parameters
        num_episodes = 1
        total_rewards = []

        for episode in tqdm(
            range(num_episodes),
            desc=f"Running trial {trial.number} for {symbol} in {year}",
        ):
            total_reward = algo.grid_trading(
                grid_distance=grid_distance,
                method=method,
                grid_range=grid_range,
                period=period,
                dynamic_midprice=dynamic_midprice,
                midprice_adjust_period=midprice_adjust_period,
                auto_adjust=auto_adjust,
                allocation_strategy=allocation_strategy,
                allocation_param=allocation_param,
                volatility_threshold=volatility_threshold,
                entry_multiplier=entry_multiplier,
                range_days=range_days,
                range_hold=range_hold,
                deviation_threshold=deviation_threshold,
            )
            total_rewards.append(total_reward)

        avg_reward = sum(total_rewards) / num_episodes
        return avg_reward

    except Exception as e:
        # Log the failure with the parameters
        logger.error(
            f"Trial failed with parameters: grid_distance={grid_distance}, grid_range={grid_range}, "
            f"history_lookback={history_lookback}, method={method}, period={period}, dynamic_midprice={dynamic_midprice}, "
            f"midprice_adjust_period={midprice_adjust_period}, auto_adjust={auto_adjust}, "
            f"allocation_strategy={allocation_strategy}, allocation_param={allocation_param}, "
            f"volatility_threshold={volatility_threshold}, entry_multiplier={entry_multiplier}, "
            f"range_days={range_days}, range_hold={range_hold}, deviation_threshold={deviation_threshold}, "
            f"indicators={selected_indicators}",
            exc_info=True,
        )
        raise optuna.TrialPruned(f"Pruning trial due to error: {e}")


if __name__ == "__main__":
    api_key = "J1KYL6ZGONER2A62"
    stocks = [
        "NVDA",
        "AAPL",
        "GOOGL",
        "TSLA",
        "AMZN",
        "F",
        "GPRO",
        "MSFT",
        "NFLX",
    ]  # Add the list of stocks you want to test
    years = [2021, 2022, 2023, 2024]  # The years to run experiments for
    n_trials = 300  # Number of trials for each stock per year

    # Create a study for each stock and year
    results = {}
    best_results = []  # List to store best results and rewards

    # Specify the storage location for the study
    storage_name = "sqlite:///optuna_study.db"

    with tqdm(
        total=len(stocks) * len(years),
        desc="Overall Progress",
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} tasks completed",
    ) as stock_bar:
        for symbol in stocks:
            for year in years:
                # Create or load the study
                study_name = f"{symbol}_{year}_study"
                study = optuna.create_study(
                    study_name=study_name,
                    storage=storage_name,  # Save the study to the SQLite database
                    direction="maximize",
                    load_if_exists=True,  # Load the study if it already exists
                    pruner=optuna.pruners.MedianPruner(),
                )
                study.optimize(
                    lambda trial: objective(trial, symbol, year, api_key),
                    n_trials=n_trials,
                )

                # Save the best parameters and reward for each stock and year
                results[f"{symbol}_{year}"] = {
                    "best_params": study.best_params,
                    "best_value": study.best_value,
                }

                # Append the best results and rewards to the list
                best_results.append(
                    {
                        "symbol": symbol,
                        "year": year,
                        "best_params": study.best_params,
                        "best_value": study.best_value,
                    }
                )

                # Log the results for each stock and year
                logger.info(
                    f"Best parameters for {symbol} in {year}: {study.best_params}"
                )
                print(f"Best parameters for {symbol} in {year}: {study.best_params}")
                logger.info(f"Best reward for {symbol} in {year}: {study.best_value}")
                print(f"Best reward for {symbol} in {year}: {study.best_value}")

                # Update progress bar
                stock_bar.update(1)

    # Save all results to a JSON file for further analysis
    with open("optuna_results.json", "w") as f:
        json.dump(results, f, indent=4)

    logger.info("Optimization completed for all stocks and periods.")

    # Print out the best results and rewards for each stock and year
    print("\nBest Results and Rewards Analysis:")
    for result in best_results:
        print(f"Stock: {result['symbol']}, Year: {result['year']}")
        print(f"Best Parameters: {result['best_params']}")
        print(f"Best Reward: {result['best_value']}\n")
