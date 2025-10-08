# =============================================================================
# === Library Imports: The Tools for the Job ===
# =============================================================================
# These libraries provide the necessary functions for web requests, time handling,
# console output, and high-performance parallel processing.

import requests  # The primary library for making HTTP requests to the Binance API.
import datetime  # Used for getting the current timestamp for found signals.
import time      # Used to measure the script's total runtime and to add small delays.
import pytz      # Handles timezone conversions, ensuring the output time is in IST.
import sys       # Provides access to system-specific parameters and functions, used here for dynamic console text.
import threading # Essential for managing concurrent operations safely, specifically the console output lock.
from concurrent.futures import ThreadPoolExecutor # A high-level interface for running tasks in parallel using threads.
from itertools import repeat # A utility to provide the same argument to all function calls in the executor map.

# =============================================================================
# === Configuration & Global State ===
# =============================================================================
# These variables control the script's behavior and store state that is shared
# across different threads.

# The market timeframe to analyze. '2h' means each candle represents 2 hours.
TIMEFRAME = '2h'
# The number of concurrent threads to run. A higher number speeds up the scan
# but increases the risk of being rate-limited by the Binance API.
MAX_WORKERS = 3
# A small delay added to each thread's execution. This helps to prevent
# overwhelming the API with too many requests in a very short time.
SLEEP_DURATION = 0.4

TIME_BETWEEN_SCANS_IN_MINUTES = 3

# --- Global variables for tracking progress across all threads ---
# A counter for the number of symbols processed so far. Must be global to be
# shared by all threads.
processed_count = 0
# A threading.Lock object. This is CRITICAL for preventing a "race condition"
# where multiple threads try to write to the console at the same time,
# resulting in garbled text. Only one thread can "hold" the lock at a time.
lock = threading.Lock()

# =============================================================================
# === Core Functions ===
# =============================================================================

def countdown_timer(minutes):
    """Displays a countdown timer for the specified number of minutes."""
    total_seconds = minutes * 60
    for i in range(total_seconds, -1, -1):
        mins, secs = divmod(i, 60)
        timer_display = f"Next result in {mins:02d}:{secs:02d}"
        #print(timer_display, end='\r')
        time.sleep(1)
    print("\n\n" + "-" * 65 + "\n")

def get_futures_symbols():
    """
    Fetches all actively trading USDT-paired PERPETUAL futures symbols.
    """
    print("Fetching all available USDT perpetual futures symbols...")
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    try:
        response = requests.get(url, timeout=10)
        # Raise an error if the API returns a non-200 status (e.g., 404, 500).
        response.raise_for_status()
        data = response.json()

        symbols = [
            s['symbol'] for s in data['symbols']
            # 1. Checks that the pair is priced in USDT.
            if s['quoteAsset'] == 'USDT'
            # 2. Ensures the symbol is actively trading.
            and s['status'] == 'TRADING'
            # 3. Ensures fetching only perpetual contracts.
            and s['contractType'] == 'PERPETUAL'
        ]
        return symbols
    except requests.exceptions.RequestException as e:
        # Handle potential network errors or API downtime gracefully.
        print(f"Error fetching symbols: {e}")
        return []

def get_candle_data(symbol, interval='2h'):
    """
    Fetches the latest OHLC (Open, High, Low, Close) data for a single symbol.
    It requests the last 4 candles to ensure we have the necessary data points
    for our 3-candle pattern analysis.
    """
    # 'limit=4' retrieves the last 4 candles. This includes the current, live, unclosed
    # candle and the three fully closed candles that preceded it.
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=4"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException:
        # Return None if a specific symbol fails, allowing the scanner to continue.
        return None

def check_conditions(symbol, total_symbols):
    """
    This is the heart of the scanner. For a given symbol, it fetches candle data,
    applies the pattern-matching logic, and prints a formatted alert if all
    conditions are met.
    """
    global processed_count

    # --- Dynamic Status Update ---
    # This block provides the real-time "Processing (X/Y) SYMBOL" feedback.
    # The 'with lock:' statement ensures that only one thread can execute this
    # block at a time, preventing jumbled output.
    with lock:
        processed_count += 1
        # The '\r' (carriage return) character moves the cursor to the beginning
        # of the line without moving down. This allows the next write to
        # overwrite the current line, creating a dynamic updating effect.
        # The padding `{symbol:<15}` ensures the line is long enough to
        # overwrite previous, shorter symbol names completely.
        status_message = f"Processing ({processed_count}/{total_symbols}) {symbol:<15}\r"
        #sys.stdout.write(status_message)
        #sys.stdout.flush() # Forces the output to be written to the console immediately.

    time.sleep(SLEEP_DURATION)
    klines = get_candle_data(symbol, TIMEFRAME)

    # --- Data Validation ---
    # If the API call failed or the symbol is too new to have 3 candles, skip it.
    if not klines or len(klines) < 3:
        return

    # --- Candle Unpacking and Preparation ---
    # The script now extracts the OHLC data for the last three available candles.
    # Binance API format: [timestamp, open, high, low, close, volume, ...]
    try:
        # klines[-1]: The last element, representing the CURRENT, LIVE, UNCLOSED candle.
        current_candle = [float(p) for p in klines[-1][1:5]]
        # klines[-2]: The second to last element, the MOST RECENTLY CLOSED candle.
        mid_candle = [float(p) for p in klines[-2][1:5]]
        # klines[-3]: The third to last, the SECOND MOST RECENTLY CLOSED candle.
        left_candle = [float(p) for p in klines[-3][1:5]]
    except (ValueError, IndexError):
        # If there's an issue with the data format, skip this symbol.
        return

    # Assigning OHLC values to named variables for better readability, mirroring
    # the Pine Script logic where [0] is current, [1] is previous, etc.
    o_curr, h_curr, l_curr, c_curr = current_candle
    o_mid,  h_mid,  l_mid,  c_mid  = mid_candle
    o_left, h_left, l_left, c_left = left_candle

    # --- Pattern Logic: The "Swing High Rejection" translated to Python ---
    # Each rule checks a specific piece of the 3-candle story.

    # Rule 1: The current, live candle must be bearish. This is the trigger.
    rule1 = c_curr < o_curr
    # Rule 2: The sequence must start with a bullish candle. This shows initial buying interest.
    rule2 = c_left > o_left
    # Rule 3: The middle candle must make a higher high. This is the "Swing High" formation,
    # often interpreted as a "liquidity grab" or "stop hunt".
    rule3 = h_left < h_mid
    # Rule 4: The middle candle's body must be rejected back below the left candle's high.
    # This is a powerful sign of failure; the market couldn't sustain the new high.
    mid_candle_body_top = max(o_mid, c_mid)
    rule4 = mid_candle_body_top < h_left
    # Rule 5: The middle candle's low must remain above the left candle's open.
    rule5 = o_left < l_mid
    # Rule 6: The current candle's high must be lower than the middle candle's high, confirming
    # that the downward momentum is continuing.
    rule6 = h_curr < h_mid
    # Rule 7: The middle candle's body must be smaller than the left candle's body. This signals
    # indecision or weakness at the peak, despite the higher high.
    mid_candle_body_size = abs(o_mid - c_mid)
    left_candle_body_size = abs(o_left - c_left)
    rule7 = mid_candle_body_size < left_candle_body_size

    # --- Signal Confirmation and Output ---
    # The `all()` function returns True only if every single rule in the list is True.
    if all([rule1, rule2, rule3, rule4, rule5, rule6, rule7]):
        # The live price is simply the close of the current (unclosed) candle.
        current_price = c_curr
        # Get the current time and format it for the IST timezone.
        ist_tz = pytz.timezone('Asia/Kolkata')
        ist_time = datetime.datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S IST')

        # This block ensures the alert printout doesn't clash with the status line.
        with lock:
            # First, overwrite the "Processing..." line with blank spaces to clear it.
            sys.stdout.write(" " * 60 + "\r")
            sys.stdout.flush()

            # Then, print the formatted alert.
            chart_url = f"https://binance.com/en/futures/{symbol}"
            print(f"Coin: {symbol}")
            print(f"Price: {current_price}")
            print(f"Time: {ist_time}")
            print(f"URL: {chart_url}")
            print("\n")

def main():
    """
    The main function that orchestrates the entire scanning process. It sets up
    the environment, starts the timer, creates the thread pool, and prints the
    final report.
    """
    print("\nSwing High Rejection Detector - 2H Candlestick Chart\n")
    '''
    while True:
        try:
            prompt = "Specify the time to wait between scans (minutes, 2-45): "
            time_between_scans = int(input(prompt))
            if 2 <= time_between_scans <= 45: break
            else: print("Error: Please enter a number between 2 and 45.")
        except ValueError: print("Error: Invalid input.")
    '''

    while True:
        # --- Initialization ---
        start_time = time.time() # Record the script's start time for runtime calculation.
        global processed_count
        processed_count = 0

        print("Fetching all Binance Futures symbols...")
        symbols = get_futures_symbols()

        if not symbols:
            print("Could not retrieve symbols. Exiting.")
            return

        total_symbols = len(symbols)
        print(f"Processing {total_symbols} coins on the {TIMEFRAME} timeframe, please wait...")
        print("\n--- Coin List ---\n")

        # --- Concurrent Execution ---
        # The ThreadPoolExecutor manages a pool of worker threads.
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # `executor.map` is a powerful function that applies `check_conditions`
            # to every symbol in the `symbols` list. It automatically distributes
            # the work among the available threads in the pool.
            # `repeat(total_symbols)` is used to pass the same `total_symbols` value
            # as the second argument to every call of `check_conditions`.
            executor.map(check_conditions, symbols, repeat(total_symbols))

        # --- Finalization and Reporting ---
        end_time = time.time()
        total_seconds = end_time - start_time
        # Convert total seconds into a more readable minutes and seconds format.
        minutes = int(total_seconds // 60)
        seconds = int(total_seconds % 60)

        # Clear the final "Processing..." line before printing the runtime.
        sys.stdout.write(" " * 60 + "\r")
        sys.stdout.flush()

        print("Scan completed.")
        print(f"Runtime: {minutes} minutes {seconds} seconds\n")

        countdown_timer(TIME_BETWEEN_SCANS_IN_MINUTES)


if __name__ == "__main__":
    # This standard Python construct ensures that the `main()` function is
    # called only when the script is executed directly.
    main()


