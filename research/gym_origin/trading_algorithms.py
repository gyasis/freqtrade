import numpy as np
from tqdm import tqdm
import logging
import seaborn as sns
import matplotlib.pyplot as plt
import pandas as pd
import talib
import plotly.graph_objects as go
from dash import Dash, dcc, html


# Set up logging
logger = logging.getLogger(__name__)


class TradingAlgorithm:
    """
    A class to implement different trading algorithms.

    Attributes:
        env (TradingEnv): The trading environment.
    """

    def __init__(self, env):
        """
        Initialize the trading algorithm class.

        Parameters:
            env (TradingEnv): The trading environment.
        """
        self.env = env
        self.market_entered = False  # Initialize the attribute here

    def calculate_indicators(self, data, period=20):
        """
        Calculate all relevant indicators using TA-Lib.

        Parameters:
            data (pd.DataFrame): The price data.
            period (int): The period for calculating indicators.

        Returns:
            dict: A dictionary of calculated indicators.
        """
        indicators = {}

        # Calculate ATR
        indicators["ATR"] = talib.ATR(
            data["High"], data["Low"], data["Close"], timeperiod=period
        )

        # Calculate RSI
        indicators["RSI"] = talib.RSI(data["Close"], timeperiod=14)

        # Calculate Cycle Indicators
        indicators["CyclePeriod"] = talib.HT_DCPERIOD(data["Close"])
        indicators["CyclePhase"] = talib.HT_DCPHASE(data["Close"])
        indicators["TrendMode"] = talib.HT_TRENDMODE(data["Close"])

        # Calculate Moving Averages
        indicators["SMA"] = talib.SMA(data["Close"], timeperiod=period)
        indicators["EMA"] = talib.EMA(data["Close"], timeperiod=period)

        # Calculate Bollinger Bands
        upperband, middleband, lowerband = talib.BBANDS(
            data["Close"], timeperiod=period, nbdevup=2, nbdevdn=2
        )
        indicators["UpperBand"] = upperband
        indicators["MiddleBand"] = middleband
        indicators["LowerBand"] = lowerband

        # Calculate Standard Deviation
        indicators["StdDev"] = talib.STDDEV(data["Close"], timeperiod=period, nbdev=1)

        return indicators

    def handle_market_entry(self, action, shares_to_trade, multiplier=2):
        """
        Handle the market entry logic, applying a multiplier to the first buy.

        Parameters:
            action (int): The action to take (0=Buy, 1=Sell, 2=Hold).
            shares_to_trade (int): The number of shares to trade based on the chosen method.
            multiplier (float): The multiplier to apply on the first buy.

        Returns:
            int: Adjusted number of shares to trade.
        """

        if action == 0 and not self.market_entered:  # First buy action
            self.market_entered = True

            shares_to_trade = int(shares_to_trade * multiplier)
            logger.info(
                f"Market entry with a multiplier of {multiplier}. Shares to trade: {shares_to_trade}"
            )
        return shares_to_trade

    def random(self):
        """
        Execute a random trading algorithm.

        Returns:
            float: The total reward obtained.
        """
        obs = self.env.reset()
        done = False
        total_reward = 0
        with tqdm(total=len(self.env.data) - 1, desc="Trading Progress") as pbar:
            while not done:
                action = self.env.action_space.sample()  # Random action
                if action == 0:  # Buy
                    max_shares_can_buy = int(
                        self.env.balance
                        // self.env.data.iloc[self.env.current_step]["Close"]
                    )
                    shares_to_trade = (
                        np.random.randint(1, max_shares_can_buy + 1)
                        if max_shares_can_buy > 0
                        else 0
                    )
                elif action == 1:  # Sell
                    shares_to_trade = (
                        np.random.randint(1, self.env.shares_held + 1)
                        if self.env.shares_held > 0
                        else 0
                    )
                else:  # Hold
                    shares_to_trade = 0

                obs, reward, done, shares_traded = self.env.step(
                    action, shares_to_trade
                )
                total_reward += reward
                pbar.update(1)
                logger.info(
                    f"Day: {self.env.current_step}, Action: {['Buy', 'Sell', 'Hold'][action]}, Shares Traded: {shares_traded}, Price: {self.env.data.iloc[self.env.current_step]['Close']:.2f}, Shares Held: {self.env.shares_held}, Balance: {self.env.balance:.2f}, Total Value: {self.env.net_worth:.2f}, Reward: {reward:.2f}"
                )
                if self.env.balance <= 0:
                    logger.info("Balance has reached zero. Stopping trading.")
                    done = True
        return total_reward

    def fixed_percentage_of_capital(self, percentage, default_percentage=0.3):
        """
        Allocate a fixed percentage of the remaining capital.

        Parameters:
            percentage (float or None): The percentage to allocate. If None, default_percentage will be used.
            default_percentage (float): The default percentage to use if percentage is None.

        Returns:
            int: The maximum number of shares that can be bought.
        """
        if percentage is None:
            logger.warning(
                f"Percentage is None. Using default percentage: {default_percentage}"
            )
            percentage = default_percentage

        max_shares_can_buy = int(
            (self.env.balance * percentage)
            // self.env.data.iloc[self.env.current_step]["Close"]
        )
        return max_shares_can_buy

    def fixed_number_of_shares(self, fixed_shares):
        """Trade a fixed number of shares."""
        return fixed_shares

    def dynamic_allocation_based_on_grid_distance(self, grid_level, total_grids):
        """Allocate more shares closer to the midprice."""
        remaining_levels = total_grids - grid_level
        max_shares_can_buy = int(
            self.env.balance // self.env.data.iloc[self.env.current_step]["Close"]
        )
        return max(1, int(max_shares_can_buy * (remaining_levels / total_grids)))

    def proportional_allocation_based_on_remaining_capital(self, remaining_levels):
        """Allocate a proportion of the remaining capital based on grid levels."""
        max_shares_can_buy = int(
            (self.env.balance // remaining_levels)
            // self.env.data.iloc[self.env.current_step]["Close"]
        )
        return max_shares_can_buy

    def volatility_based_allocation(
        self, volatility_threshold, high_volatility_reduction=0.5
    ):
        """Reduce shares traded based on volatility."""
        volatility = (
            self.env.data["Close"]
            .pct_change()
            .rolling(window=20)
            .std()
            .iloc[self.env.current_step]
        )
        max_shares_can_buy = int(
            self.env.balance // self.env.data.iloc[self.env.current_step]["Close"]
        )
        if volatility > volatility_threshold:
            return int(max_shares_can_buy * high_volatility_reduction)
        else:
            return max_shares_can_buy

    def execute_with_rules(self, rules):
        """
        Execute a trading algorithm based on provided rules.

        Parameters:
            rules (dict): A dictionary of trading rules.

        Returns:
            float: The total reward obtained.
        """
        obs = self.env.reset()
        done = False
        total_reward = 0
        with tqdm(total=len(self.env.data) - 1, desc="Trading Progress") as pbar:
            while not done:
                current_price = self.env.data.iloc[self.env.current_step]["Close"]
                action = np.random.choice(
                    [0, 1, 2],
                    p=[
                        rules.get("buy", {}).get("probability", 0.0),
                        rules.get("sell", {}).get("probability", 0.0),
                        rules.get("hold", {}).get(
                            "probability",
                            1.0
                            - rules.get("buy", {}).get("probability", 0.0)
                            - rules.get("sell", {}).get("probability", 0.0),
                        ),
                    ],
                )

                if action == 0:  # Buy
                    max_shares_can_buy = int(self.env.balance // current_price)
                    shares_to_trade = (
                        np.random.randint(
                            int(max_shares_can_buy * 0.5), max_shares_can_buy + 1
                        )
                        if max_shares_can_buy > 0
                        else 0
                    )
                elif action == 1:  # Sell
                    shares_to_trade = (
                        np.random.randint(
                            int(self.env.shares_held * 0.5), self.env.shares_held + 1
                        )
                        if self.env.shares_held > 0
                        else 0
                    )
                else:  # Hold
                    shares_to_trade = 0

                obs, reward, done, shares_traded = self.env.step(
                    action, shares_to_trade
                )
                total_reward += reward
                pbar.update(1)
                logger.info(
                    f"Day: {self.env.current_step}, Action: {['Buy', 'Sell', 'Hold'][action]}, Shares Traded: {shares_traded}, Price: {self.env.data.iloc[self.env.current_step]['Close']:.2f}, Shares Held: {self.env.shares_held}, Balance: {self.env.balance:.2f}, Total Value: {self.env.net_worth:.2f}, Reward: {reward:.2f}"
                )
                if self.env.balance <= 0:
                    logger.info("Balance has reached zero. Stopping trading.")
                    done = True
        return total_reward

    def bollinger_bands_mean_reversion(
        self, use_volume_filter=True, volume_threshold=100000
    ):
        """
        Execute a Bollinger Bands Mean Reversion trading algorithm with volume filtering.

        Parameters:
            use_volume_filter (bool): Whether to use volume filtering.
            volume_threshold (int): The volume threshold for the volume filter.

        Returns:
            float: The total reward obtained.
        """
        # Calculate Bollinger Bands using TA-Lib
        data = self.env.data
        close = data["Close"].values
        upper, middle, lower = talib.BBANDS(
            close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0
        )

        data["UpperBand"] = upper
        data["MiddleBand"] = middle
        data["LowerBand"] = lower

        obs = self.env.reset()
        done = False
        total_reward = 0
        with tqdm(total=len(self.env.data) - 1, desc="Trading Progress") as pbar:
            while not done:
                current_price = self.env.data.iloc[self.env.current_step]["Close"]
                current_volume = self.env.data.iloc[self.env.current_step]["Volume"]

                # Determine the action
                if current_price < data["LowerBand"].iloc[self.env.current_step]:
                    action = 0  # Buy
                elif current_price > data["UpperBand"].iloc[self.env.current_step]:
                    action = 1  # Sell
                else:
                    action = 2  # Hold

                # Apply volume filter if enabled
                if use_volume_filter and current_volume < volume_threshold:
                    action = 2  # Hold

                if action == 0:  # Buy
                    max_shares_can_buy = int(self.env.balance // current_price)
                    shares_to_trade = max_shares_can_buy
                elif action == 1:  # Sell
                    shares_to_trade = self.env.shares_held
                else:  # Hold
                    shares_to_trade = 0

                obs, reward, done, shares_traded = self.env.step(
                    action, shares_to_trade
                )
                total_reward += reward
                pbar.update(1)
                logger.info(
                    f"Day: {self.env.current_step}, Action: {['Buy', 'Sell', 'Hold'][action]}, Shares Traded: {shares_traded}, Price: {self.env.data.iloc[self.env.current_step]['Close']:.2f}, Shares Held: {self.env.shares_held}, Balance: {self.env.balance:.2f}, Total Value: {self.env.net_worth:.2f}, Reward: {reward:.2f}"
                )
                if self.env.balance <= 0:
                    logger.info("Balance has reached zero. Stopping trading.")
                    done = True
        return total_reward

    def plot_chart(self, grid_lines=None, volumes=None):
        """
        Plot and display an interactive chart using Dash Plotly.

        The chart shows price history, shares in possession, balance, net worth, decision points, rewards, and optionally, grid lines and volume histogram.

        Parameters:
            grid_lines (list or None): A list of grid line values to plot (if applicable).
            volumes (list or None): A list of trading volumes to plot as a histogram (if applicable).
        """
        # Extract data from history
        (
            days,
            prices,
            shares_held,
            balances,
            net_worths,
            actions,
            shares_traded,
            rewards,
        ) = zip(*self.env.history)

        # Create a DataFrame for easier plotting
        df = pd.DataFrame(
            {
                "Day": days,
                "Price": prices,
                "Shares Held": shares_held,
                "Balance": balances,
                "Net Worth": net_worths,
                "Action": actions,
                "Shares Traded": shares_traded,
                "Reward": rewards,
            }
        )

        # Create the Dash app
        app = Dash(__name__)

        # Create the plotly figure
        fig = go.Figure()

        # Add price line
        fig.add_trace(
            go.Scatter(
                x=df["Day"],
                y=df["Price"],
                mode="lines",
                name="Price",
                line=dict(color="blue"),
            )
        )

        # Add shares held line
        fig.add_trace(
            go.Scatter(
                x=df["Day"],
                y=df["Shares Held"],
                mode="lines",
                name="Shares Held",
                line=dict(color="green"),
            )
        )

        # Add balance line
        fig.add_trace(
            go.Scatter(
                x=df["Day"],
                y=df["Balance"],
                mode="lines",
                name="Balance",
                line=dict(color="red"),
            )
        )

        # Add net worth line
        fig.add_trace(
            go.Scatter(
                x=df["Day"],
                y=df["Net Worth"],
                mode="lines",
                name="Net Worth",
                line=dict(color="purple"),
            )
        )

        # Add buy, sell, and hold markers
        fig.add_trace(
            go.Scatter(
                x=df[df["Action"] == 0]["Day"],
                y=df[df["Action"] == 0]["Price"],
                mode="markers",
                name="Buy",
                marker=dict(color="green", symbol="triangle-up"),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=df[df["Action"] == 1]["Day"],
                y=df[df["Action"] == 1]["Price"],
                mode="markers",
                name="Sell",
                marker=dict(color="red", symbol="triangle-down"),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=df[df["Action"] == 2]["Day"],
                y=df[df["Action"] == 2]["Price"],
                mode="markers",
                name="Hold",
                marker=dict(color="orange", symbol="circle"),
            )
        )

        # Plot grid lines and midpoint line
        if hasattr(self, "grid") and self.grid is not None:
            midpoint_index = len(self.grid) // 2
            for i, grid_line in enumerate(self.grid):
                line_width = (
                    3 if i == midpoint_index else 1
                )  # Thicker line for midpoint
                fig.add_shape(
                    type="line",
                    x0=df["Day"].min(),
                    y0=grid_line,
                    x1=df["Day"].max(),
                    y1=grid_line,
                    line=dict(color="yellow", width=line_width),
                )

        # Add volume histogram if volumes are provided
        if volumes:
            fig.add_trace(
                go.Bar(
                    x=df["Day"],
                    y=volumes,
                    name="Volume",
                    marker_color="lightgray",
                    opacity=0.6,
                    yaxis="y2",
                )
            )

            # Add a secondary y-axis for volume
            fig.update_layout(
                yaxis2=dict(
                    title="Volume",
                    overlaying="y",
                    side="right",
                    showgrid=False,
                    rangemode="nonnegative",
                )
            )

        # Layout settings
        fig.update_layout(
            title="Trading Simulation History",
            xaxis_title="Day",
            yaxis_title="Value",
            legend_title="Metrics",
            template="plotly_dark",
            barmode="overlay",
        )

        # Add annotations for rewards
        for i, row in df.iterrows():
            if row["Reward"] != 0:
                fig.add_annotation(
                    x=row["Day"],
                    y=row["Price"],
                    text=f"{row['Reward']:.2f}",
                    showarrow=True,
                    arrowhead=1,
                    ax=0,
                    ay=-40,
                )

        # Layout the Dash app
        app.layout = html.Div(
            [html.H1("Trading Simulation Interactive Chart"), dcc.Graph(figure=fig)]
        )

        # Run the Dash app with debug=False
        app.run_server(debug=False)

    # grid trading STRATEGY BEGIN

    def calculate_midprice(
        self, method="market_price", period=20, auto_adjust=False, grid=None
    ):
        """
        Calculate the midprice based on the selected method.

        Parameters:
            method (str): The method to calculate the midprice.
                        Options: "market_price", "moving_average", "mid_high_low", "vwap"
            period (int): The period to consider for calculations (used for moving averages, midpoints, and VWAP).
            auto_adjust (bool): Whether to adjust the midprice based on grid midpoint.
            grid (np.ndarray or None): The grid lines, if auto-adjusted.

        Returns:
            float: The calculated midprice.
        """
        try:
            if auto_adjust and grid is not None:
                # Set the midprice to the midpoint of the generated grid
                midprice = grid[len(grid) // 2]
                logger.info(f"Midprice set to the midpoint of the grid: {midprice}")
            else:
                # Calculate midprice based on the selected method
                if method == "market_price":
                    if self.env.current_step >= period:
                        midprice = self.env.data.iloc[period - 1]["Close"]
                    else:
                        raise ValueError(
                            f"Current step ({self.env.current_step}) is less than the period ({period})."
                        )
                elif method == "moving_average":
                    midprice = (
                        self.env.data["Close"]
                        .rolling(window=period, min_periods=1)
                        .mean()
                        .iloc[self.env.current_step]
                    )
                elif method == "mid_high_low":
                    midprice = (
                        self.env.data["High"]
                        .rolling(window=period, min_periods=1)
                        .max()
                        .iloc[self.env.current_step]
                        + self.env.data["Low"]
                        .rolling(window=period, min_periods=1)
                        .min()
                        .iloc[self.env.current_step]
                    ) / 2
                elif method == "vwap":
                    price = self.env.data["Close"]
                    volume = self.env.data["Volume"]
                    midprice = (price * volume).rolling(
                        window=period, min_periods=1
                    ).sum().iloc[self.env.current_step] / volume.rolling(
                        window=period, min_periods=1
                    ).sum().iloc[
                        self.env.current_step
                    ]
                else:
                    raise ValueError(
                        f"Unknown method {method} for calculating midprice."
                    )

                if pd.isna(midprice):
                    raise ValueError(
                        "Calculated midprice is NaN. Check your data and method parameters."
                    )

        except Exception as e:
            logger.error(f"Error calculating midprice: {e}")
            # Fallback to the initial market price if there's an issue
            midprice = self.env.data.iloc[0]["Close"]

        return midprice

    def grid_trading(
        self,
        grid_distance=0.01,
        grid_range=0.2,
        method="moving_average",
        period=20,
        dynamic_midprice=True,
        auto_adjust=False,
        allocation_strategy="fixed_percentage",
        allocation_param=0.3,
        volatility_threshold=0.02,
        entry_multiplier=2,
        exit_threshold=0.05,
        recoup_cooldown_period=5,
        midprice_adjust_period=5,
        reevaluate_grid=False,
        range_days=None,  # New parameter to specify the number of days to hold after entering
        range_hold=False,  # New flag to extend holding if the reward is positive after the range period
        deviation_threshold=0.5,
        # Add this argument
        # New parameter to define the allowed deviation from midprice
    ):

        total_reward = 0
        entry_day = None  # To track the day when market entry happens
        upward_momentum = False  # Initialize upward momentum

        # Reset necessary variables for each cycle
        self.market_entered = False
        recoup_mode = False
        cooldown_steps = 0
        last_buy_price = None
        previous_price = None  # To track previous price for crossing checks

        # Calculate midprice and generate grid lines at the start of the cycle
        if auto_adjust:
            grid_range, grid_distance = self.auto_set_grid_parameters(period=period)
            # Ensure midprice is set by calculating it based on the chosen method
            grid = self.generate_grid(None, grid_distance, grid_range, auto_adjust=True)
            midprice = grid[len(grid) // 2]  #
        else:
            midprice = self.calculate_midprice(
                method=method, period=period, auto_adjust=False
            )
            grid = self.generate_grid(
                midprice, grid_distance, grid_range, auto_adjust=False
            )

        # Generate grid lines
        grid = self.generate_grid(
            midprice=midprice,
            grid_distance=grid_distance,
            grid_range=grid_range,
            auto_adjust=auto_adjust,
        )

        self.grid = grid
        logger.info(f"Generated grid lines: {grid}")

        obs = self.env.reset()
        done = False

        # Check the first day's price against the lowest grid line
        first_day_price = self.env.data.iloc[0]["Close"]
        if first_day_price < grid[0]:
            logger.info(
                f"First day's price {first_day_price:.2f} is below the lowest grid line {grid[0]:.2f}. Not entering the market."
            )
            return total_reward  # Exit if the first day's price is below the lowest grid line

        with tqdm(total=len(self.env.data) - 1, desc="Trading Progress") as pbar:
            while not done:
                current_step = self.env.current_step
                current_price = self.env.data.iloc[self.env.current_step]["Close"]
                # Add this line to log the current step number
                logger.info(f"Current Step: {self.env.current_step}")
                current_date = self.env.data.index[current_step].strftime("%Y-%m-%d")
                logger.info(f"Processing step {current_step} on date: {current_date}")
                logger.info(f"Current Step: {current_step}")
                # Log the market entered flag every day
                logger.info(f"Market Entered Flag: {self.market_entered}")

                # Skip until sufficient data exists for the midprice calculation
                if self.env.current_step < period:
                    logger.info(
                        f"Skipping trading logic. Current step ({self.env.current_step}) is less than the period ({period})."
                    )
                    self.env.current_step += 1
                    pbar.update(1)
                    continue

                # Calculate momentum (e.g., short-term moving average)
                if self.env.current_step >= period:
                    short_term_ma = (
                        self.env.data["Close"]
                        .rolling(window=period)
                        .mean()
                        .iloc[self.env.current_step]
                    )
                    upward_momentum = current_price > short_term_ma
                    logger.info(f"Upward momentum: {upward_momentum}")

                # Initialize action and shares_to_trade with default values
                action = 2  # Default to 'Hold'
                shares_to_trade = 0  # Default to no shares being traded

                # Log midprice and grid levels
                logger.info(
                    f"Day: {self.env.current_step + 1}, Midprice: {midprice:.2f}, Current Price: {current_price:.2f}"
                )
                logger.info(f"Grid Levels: {grid}")

                # Optionally update the midprice and grid based on dynamic_midprice and auto_adjust flags
                if dynamic_midprice and (
                    self.env.current_step % midprice_adjust_period == 0
                    or abs(current_price - midprice) > deviation_threshold * grid_range
                ):  # Adjust based on user-defined period or significant price deviation
                    logger.info(
                        "Significant price deviation detected. Recalculating midprice and grid."
                    )

                    if auto_adjust:
                        # Automatically adjust grid and midprice based on historical data
                        grid_range, grid_distance = self.auto_set_grid_parameters(
                            period=period
                        )
                        grid = self.generate_grid(
                            None, grid_distance, grid_range, auto_adjust=True
                        )
                        midprice = grid[
                            len(grid) // 2
                        ]  # Set midprice to the midpoint of the grid
                    else:
                        # Recalculate midprice and regenerate grid based on the chosen method
                        midprice = self.calculate_midprice(method=method, period=period)
                        grid = self.generate_grid(
                            midprice, grid_distance, grid_range, auto_adjust=False
                        )

                    logger.info(f"Updated grid: {grid}, Midprice: {midprice}")

                # Hard market entry logic: Trigger buy as soon as price is below the midpoint
                if not self.market_entered and current_price < midprice:
                    action = 0
                    shares_to_trade = self.fixed_percentage_of_capital(allocation_param)
                    self.market_entered = True
                    last_buy_price = current_price
                    entry_day = self.env.current_step

                    logger.info(
                        f"Hard market entry triggered at price {current_price:.2f}"
                    )

                # Normal trading logic once the market has been entered
                if self.market_entered:
                    if self.env.shares_held == 0:
                        self.market_entered = (
                            False  # Reset market_entered if no shares held
                        )
                        logger.info("Market exited due to no shares held.")

                    days_held = self.env.current_step - entry_day

                    # Range-hold logic only kicks in on the last day
                    if range_days is not None and days_held >= range_days:
                        logger.info(
                            f"Range logic active after holding for {days_held} days."
                        )
                        if range_hold:
                            logger.info("Range-hold condition triggered.")
                            if reward <= 0 and self.env.shares_held > 0:
                                action = 1  # Sell if reward turns negative or zero
                                shares_to_trade = self.env.shares_held
                                logger.info(
                                    f"Range-hold selling due to negative/zero reward at price {current_price:.2f} after holding for {days_held} days with negative or zero reward."
                                )
                        else:
                            if self.env.shares_held > 0:
                                action = 1  # Sell immediately after range period
                                shares_to_trade = self.env.shares_held
                                logger.info(
                                    f"Selling after range period at price {current_price:.2f}. Held for {days_held} days."
                                )

                    else:
                        if cooldown_steps > 0:
                            cooldown_steps -= 1
                            logger.info(
                                f"Cooldown period active. Steps remaining: {cooldown_steps}"
                            )
                        elif current_price < grid[0] * (1 - exit_threshold):
                            action = 1  # Sell all held shares
                            shares_to_trade = self.env.shares_held
                            recoup_mode = True
                            self.market_entered = False
                            logger.info(
                                f"Hard exit triggered: Price {current_price:.2f} is significantly below the lowest grid line {grid[0]:.2f}. Exiting market."
                            )
                        else:
                            logger.info(
                                f"Evaluating grid levels for buy/sell opportunities at price {current_price:.2f}"
                            )
                            for i, grid_line in enumerate(grid):
                                logger.debug(
                                    f"Checking if current price {current_price:.2f} is within range of grid line {grid_line:.2f} +/- {grid_distance / 2:.2f}"
                                )

                                # Check if price is within the buffer around the grid line
                                if (
                                    current_price <= grid_line + grid_distance / 2
                                    and current_price >= grid_line - grid_distance / 2
                                ):
                                    logger.info(
                                        f"Price {current_price:.2f} is within range of grid line {grid_line:.2f}"
                                    )

                                    if current_price < midprice:
                                        logger.info(
                                            f"Price {current_price:.2f} is below midprice {midprice:.2f}, considering buying."
                                        )
                                        if self.env.balance >= current_price:
                                            action = 0  # Buy
                                            shares_to_trade = (
                                                self.fixed_percentage_of_capital(
                                                    allocation_param
                                                )
                                            )
                                            last_buy_price = current_price
                                            logger.info(
                                                f"Triggered Buy at grid line {grid_line:.2f}. Current price: {current_price:.2f}"
                                            )
                                        else:
                                            logger.info(
                                                f"Buy could not be triggered at grid line {grid_line:.2f} due to insufficient balance. Current price: {current_price:.2f}, Balance: {self.env.balance:.2f}"
                                            )
                                    else:
                                        if self.env.shares_held > 0:
                                            logger.info(
                                                f"Price {current_price:.2f} is above midprice {midprice:.2f}, considering selling."
                                            )
                                            if (
                                                last_buy_price
                                                and current_price > last_buy_price
                                            ):
                                                if (
                                                    current_price < midprice
                                                    and upward_momentum
                                                ):
                                                    action = 2  # Hold, anticipating further gains
                                                    logger.info(
                                                        f"Holding position at price {current_price:.2f} for potential gains"
                                                    )
                                                else:
                                                    action = 1  # Sell if already profitable and conditions met
                                                    shares_to_trade = (
                                                        self.env.shares_held
                                                    )
                                                    logger.info(
                                                        f"Selling at price {current_price:.2f} after holding period"
                                                    )
                                            else:
                                                action = 1  # Sell
                                                shares_to_trade = self.env.shares_held
                                                logger.info(
                                                    f"Triggered Sell at grid line {grid_line:.2f}. Current price: {current_price:.2f}"
                                                )
                                        else:
                                            logger.info(
                                                f"Sell could not be triggered at grid line {grid_line:.2f} due to no shares held. Current price: {current_price:.2f}, Shares Held: {self.env.shares_held}"
                                            )
                                    break  # Exit the loop once an action is decided

                                # New Logic: Detect if price has crossed the grid line (without necessarily being in the buffer zone)
                                elif previous_price is not None:
                                    if (
                                        previous_price < grid_line
                                        and current_price > grid_line
                                    ):
                                        logger.info(
                                            f"Price crossed above grid line {grid_line:.2f}. Considering buy."
                                        )
                                        if (
                                            current_price < midprice
                                            and self.env.balance >= current_price
                                        ):
                                            action = 0  # Buy
                                            shares_to_trade = (
                                                self.fixed_percentage_of_capital(
                                                    allocation_param
                                                )
                                            )
                                            last_buy_price = current_price
                                            logger.info(
                                                f"Triggered Buy after crossing grid line {grid_line:.2f}. Current price: {current_price:.2f}"
                                            )
                                        elif self.env.shares_held > 0:
                                            action = 1  # Sell
                                            shares_to_trade = self.env.shares_held
                                            logger.info(
                                                f"Selling after price crossed grid line {grid_line:.2f}. Current price: {current_price:.2f}"
                                            )
                                    elif (
                                        previous_price > grid_line
                                        and current_price < grid_line
                                    ):
                                        logger.info(
                                            f"Price crossed below grid line {grid_line:.2f}. Considering sell."
                                        )
                                        if (
                                            current_price < midprice
                                            and self.env.shares_held > 0
                                        ):
                                            action = 1  # Sell
                                            shares_to_trade = self.env.shares_held
                                            logger.info(
                                                f"Triggered Sell after crossing grid line {grid_line:.2f}. Current price: {current_price:.2f}"
                                            )
                                    break  # Exit the loop once an action is decided

                            # Update the previous price after the loop for future comparison
                            previous_price = current_price

                # Execute the action and decide the number of shares to trade based on the selected strategy
                if action == 0:  # Buy
                    if allocation_strategy == "fixed_percentage":
                        shares_to_trade = self.fixed_percentage_of_capital(
                            allocation_param
                        )
                    elif allocation_strategy == "fixed_number":
                        shares_to_trade = self.fixed_number_of_shares(allocation_param)
                    elif allocation_strategy == "dynamic_allocation":
                        shares_to_trade = (
                            self.dynamic_allocation_based_on_grid_distance(i, len(grid))
                        )
                    elif allocation_strategy == "proportional_allocation":
                        shares_to_trade = (
                            self.proportional_allocation_based_on_remaining_capital(
                                len(grid) - i
                            )
                        )
                    elif allocation_strategy == "volatility_based":
                        shares_to_trade = self.volatility_based_allocation(
                            volatility_threshold
                        )

                    # Apply market entry logic
                    shares_to_trade = self.handle_market_entry(
                        action, shares_to_trade, multiplier=entry_multiplier
                    )

                obs, reward, done, shares_traded = self.env.step(
                    action, shares_to_trade
                )
                total_reward += reward

                # Log after the step to ensure the environment is updating
                logger.info(
                    f"After Step: Current Step: {self.env.current_step}, Done: {done}"
                )

                pbar.update(1)

                # Update the current step after each action
                self.env.current_step += 1

                if done or self.env.current_step >= len(self.env.data) - 1:
                    logger.info(
                        "End of date range or `done` reached. Exiting trading series."
                    )
                    break  # Break the loop once done is True or data ends

                # Detailed logging
                logger.info(
                    f"Day: {self.env.current_step}, Action: {['Buy', 'Sell', 'Hold'][action]}, "
                    f"Shares Traded: {shares_traded}, Total Shares Held: {self.env.shares_held}, "
                    f"Dollar Amount: ${shares_traded * current_price:.2f}, "
                    f"Price: {current_price:.2f}, Balance: {self.env.balance:.2f}, "
                    f"Total Value: {self.env.net_worth:.2f}, Reward: {reward:.2f}"
                )

                if self.env.balance <= 0:
                    logger.info("Balance has reached zero. Stopping trading.")
                    done = True

            if self.env.current_step >= len(self.env.data) - 1:
                logger.info("End of date range reached. Exiting trading series.")
                done = True

        return total_reward

    def generate_logarithmic_grid(
        self, midprice, historical_high, historical_low, base=10
    ):
        log_high = np.log10(historical_high)
        log_low = np.log10(historical_low)
        log_mid = np.log10(midprice)

        log_grid = np.linspace(log_low, log_high, num=10)
        grid = np.power(base, log_grid)

        logger.info(f"Generated logarithmic grid lines: {grid}")
        return grid

    def generate_grid(
        self,
        midprice,
        grid_distance,
        grid_range,
        auto_adjust=False,
        use_indicators=False,
        period=20,
    ):
        """
        Generate grid lines above and below the midprice.

        Parameters:
            midprice (float): The central price around which to generate the grid.
            grid_distance (float): The distance between each grid line.
            grid_range (float): The total range above and below the midprice to cover with the grid.
            auto_adjust (bool): Whether to automatically adjust grid lines based on historical data.
            use_indicators (bool): Whether to adjust grid parameters using indicators (ATR, RSI, etc.)
            period (int): The period for calculating indicators if `use_indicators` is True.

        Returns:
            np.ndarray: An array of grid lines.
        """
        logger.debug(
            f"Generating grid with midprice={midprice}, distance={grid_distance}, range={grid_range}, auto_adjust={auto_adjust}, use_indicators={use_indicators}"
        )

        if use_indicators:
            # Automatically adjust grid parameters using indicators
            grid_range, grid_distance = self.auto_set_grid_parameters(period=period)
            midprice = self.calculate_midprice(method="moving_average", period=period)

        if auto_adjust:
            min_price = self.env.data["Low"].min()
            max_price = self.env.data["High"].max()

            # Logarithmic scaling for grid lines
            log_start = np.log10(min_price)
            log_end = np.log10(max_price)
            grid = np.logspace(log_start, log_end, num=10)

            logger.info(
                f"Auto-adjusted grid using logarithmic scaling: start={min_price}, end={max_price}, grid={grid}"
            )
        else:
            # Linear grid scaling around the midprice
            grid_start = midprice - grid_range / 2
            grid_end = midprice + grid_range / 2
            grid = np.arange(grid_start, grid_end, grid_distance)

        if len(grid) == 0:
            logger.error(
                "No grid lines were generated. Check your grid distance and range values."
            )

        return grid

    def auto_set_grid_parameters(
        self, period=20, selected_indicators=None, auto_set_period=False
    ):
        """
        Automatically set the grid range and grid distance based on selected indicators and historical data.
        The period will be auto-set if `auto_set_period` is True.

        Parameters:
            period (int): The period to consider for historical analysis.
            selected_indicators (list): A list of indicators to apply for grid adjustment.
            auto_set_period (bool): Whether to auto-set the period using cycle analysis.

        Returns:
            tuple: A tuple containing the calculated grid range and grid distance.
        """
        data = self.env.data

        # Auto-set the period using cycle analysis if enabled
        if auto_set_period:
            period = self.auto_set_period(data)

        data["Return"] = data["Close"].pct_change()

        # Default calculations based on historical high and low prices
        historical_volatility = data["Return"].rolling(window=period).std().iloc[-1]
        historical_high = data["High"].rolling(window=period).max().iloc[-1]
        historical_low = data["Low"].rolling(window=period).min().iloc[-1]

        grid_range = historical_high - historical_low
        log_high = np.log10(historical_high)
        log_low = np.log10(historical_low)
        grid_distance = (log_high - log_low) / 10  # Scaling for grid distance

        # Apply indicators if provided
        if selected_indicators:
            indicator_values = []

            if "ATR" in selected_indicators:
                atr = talib.ATR(
                    data["High"], data["Low"], data["Close"], timeperiod=period
                )
                indicator_values.append(atr.mean())

            if "RSI" in selected_indicators:
                rsi = talib.RSI(data["Close"], timeperiod=period)
                indicator_values.append(rsi.mean())

            if "CyclePeriod" in selected_indicators:
                cycle_period = talib.HT_DCPERIOD(data["Close"])
                indicator_values.append(cycle_period.mean())

            if "CyclePhase" in selected_indicators:
                cycle_phase = talib.HT_DCPHASE(data["Close"])
                indicator_values.append(cycle_phase.mean())

            if "TrendMode" in selected_indicators:
                trend_mode = talib.HT_TRENDMODE(data["Close"]).mean()
                indicator_values.append(trend_mode)

            if "BollingerBands" in selected_indicators:
                upperband, middleband, lowerband = talib.BBANDS(
                    data["Close"], timeperiod=period, nbdevup=2, nbdevdn=2
                )
                indicator_values.append(
                    (upperband.mean() - lowerband.mean()) / middleband.mean()
                )

            if "SMA" in selected_indicators:
                sma = talib.SMA(data["Close"], timeperiod=period)
                indicator_values.append(sma.mean())

            if "EMA" in selected_indicators:
                ema = talib.EMA(data["Close"], timeperiod=period)
                indicator_values.append(ema.mean())

            if "StdDev" in selected_indicators:
                stddev = talib.STDDEV(data["Close"], timeperiod=period, nbdev=1)
                indicator_values.append(stddev.mean())

            # Combine indicator values (e.g., average or weighted average)
            if indicator_values:
                combined_value = np.mean(indicator_values)  # Simple average for now
                grid_range *= (
                    1 + combined_value / 100
                )  # Example influence on grid range
                grid_distance *= (
                    1 + combined_value / 100
                )  # Example influence on grid distance

        logger.info(
            f"Auto-set grid parameters using indicators and period={period}: grid_range={grid_range:.2f}, grid_distance={grid_distance:.4f}"
        )

        return grid_range, 10**grid_distance

    def auto_set_period(self, data):
        """
        Automatically set the period based on the dominant cycle period using TA-Lib.

        Parameters:
            data (pd.DataFrame): Historical stock data.

        Returns:
            int: The automatically determined period based on the cycle indicator.
        """
        cycle_period = talib.HT_DCPERIOD(data["Close"]).mean()
        period = max(
            3, int(cycle_period)
        )  # Set a minimum period of 10 to avoid too small values

        logger.info(f"Auto-set period based on cycle period: {period}")
        return period

    if __name__ == "__main__":
        parser = argparse.ArgumentParser(description="Grid Trading Example with Gym")
        # (other arguments remain the same)

        parser.add_argument(
            "--auto_period",
            action="store_true",
            help="If set, automatically adjust the period based on the dominant cycle period.",
        )

        args = parser.parse_args()

        # Load data with optional period and history_lookback
        data = load_data(
            args.symbol,
            args.start_date,
            args.end_date,
            args.period,
            args.history_lookback,
        )

        if data.empty:
            logger.error("No data retrieved. Exiting.")
        else:
            # Create the Gym environment
            env = TradingEnv(data)
            algo = TradingAlgorithm(env)

            # Automatically set the period if auto_period is enabled
            if args.auto_period:
                period = algo.auto_set_period(data)
            else:
                period = args.period

            # Automatically set grid parameters if auto_adjust is enabled
            if args.auto_adjust:
                grid_range, grid_distance = algo.auto_set_grid_parameters(period=period)
            else:
                grid_range, grid_distance = args.grid_range, args.grid_distance

            total_reward = algo.grid_trading(
                grid_distance=grid_distance,
                method=args.method,
                grid_range=grid_range,
                period=period,  # Use the calculated or user-defined period
                dynamic_midprice=args.dynamic_midprice,
                midprice_adjust_period=args.midprice_adjust_period,
                auto_adjust=args.auto_adjust,
                allocation_strategy=args.allocation_strategy,
                allocation_param=args.allocation_param,
                volatility_threshold=args.volatility_threshold,
                entry_multiplier=args.entry_multiplier,
                range_days=args.range,
                range_hold=args.range_hold,
                deviation_threshold=args.deviation_threshold,
            )

            logger.info(f"Total reward: {total_reward:.2f}")

            # Display the interactive chart if requested
            if args.plot_chart:
                algo.plot_chart(volumes=env.data["Volume"].tolist())
