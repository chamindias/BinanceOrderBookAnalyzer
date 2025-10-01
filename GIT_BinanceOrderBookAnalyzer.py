# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# GitHub Code
# 
# This file is intended for the GitHub Actions workflow.
# 
# Binance Futures Order Book Ratio Analyzer
#
# USE CASE:
# This script identifies top cryptocurrencies by market cap and analyzes their
# market pressure in a continuous, automated loop, presenting a summary and
# two curated lists of high-probability trade setups (Long/Short).
#
# FLOW OF LOGIC:
# 1.  USER INPUT: The user specifies how many coins to track, the depth of the
#     order book to analyze, and the refresh rate in minutes.
# 2.  DATA GATHERING:
#     - Fetches a list of top cryptocurrencies from CoinMarketCap.
#     - Fetches a list of all tradable futures contracts from Binance.
#     - Creates a final target list by finding coins that exist in both lists.
# 3.  ANALYSIS LOOP (for each coin in the target list):
#     - Fetches order book data (limit orders) to gauge PASSIVE pressure.
#     - Fetches recent trades data (market orders) to gauge AGGRESSIVE pressure.
#     - Calculates the Buy/Sell ratio for both passive and aggressive orders.
# 4.  SETUP IDENTIFICATION:
#     - It specifically looks for divergences between passive and aggressive behavior.
#     - POTENTIAL SHORTS: Identifies coins where passive sentiment is bullish
#       (high limit buy orders) but is being overwhelmed by aggressive sellers
#       (high market sell orders). The thesis is that the "buy wall" is failing.
#     - POTENTIAL LONGS: Identifies coins where passive sentiment is bearish
#       (high limit sell orders) but is being overwhelmed by aggressive buyers
#       (high market buy orders). The thesis is that the "sell wall" is being absorbed.
# 5.  PRESENTATION:
#     - Displays the identified Long and Short setups in sorted, easy-to-read tables.
#     - Waits for the user-defined interval and repeats the analysis.
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

# Import necessary libraries
import requests      # For making HTTP requests to external APIs (CoinMarketCap, Binance).
import sys           # For interacting with the system, specifically for writing progress/error messages to the console.
import time          # For timing script execution and implementing sleep delays to manage API rate limits and loop frequency.
from datetime import datetime
from zoneinfo import ZoneInfo # For handling timezone-aware datetimes, ensuring timestamps are consistent.
from concurrent.futures import ThreadPoolExecutor, as_completed # For managing concurrent API requests to improve performance.

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# API and Data Fetching Functions
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

def read_api_key(filename="CoinMarketCapAPIKey.txt"):
    """
    Reads the CoinMarketCap API key from a local text file.
    This is a good practice for security as it avoids hardcoding sensitive keys directly in the script.
    """
    try:
        # Open the specified file in read mode.
        with open(filename, 'r') as f:
            # Read the key and remove any leading/trailing whitespace.
            key = f.read().strip()
        # Check if the key is an empty string after stripping.
        if not key:
            print(f"Error: The file '{filename}' is empty.")
            return None
        return key
    # Handle the case where the file does not exist.
    except FileNotFoundError:
        print(f"Error: API key file '{filename}' not found.")
        print("Please create this file and paste your CoinMarketCap API key into it.")
        return None

def get_cmc_market_data(api_key, limit):
    """
    Fetches the top cryptocurrencies sorted by market cap from the CoinMarketCap (CMC) API.
    This provides the initial, broad list of coins to consider for analysis.
    """
    print(f"Fetching top {limit} coins from CoinMarketCap...")
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    # The header must include the API key for authentication.
    headers = {'Accepts': 'application/json', 'X-CMC_PRO_API_KEY': api_key}
    # Parameters specify the number of coins to fetch and to sort by market cap.
    params = {'start': '1', 'limit': limit, 'convert': 'USD', 'sort': 'market_cap'}
    try:
        # Make the GET request with a 10-second timeout.
        response = requests.get(url, headers=headers, params=params, timeout=10)
        # Raise an exception for bad status codes (e.g., 401 Unauthorized, 404 Not Found).
        response.raise_for_status()
        # Return the list of coin data from the JSON response.
        return response.json().get('data', [])
    # Catch any network-related errors during the request.
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Error fetching CoinMarketCap data: {e}\n")
        return None

def get_binance_futures_symbols():
    """
    Fetches a set of all actively traded USDT-margined futures symbols from Binance.
    This is crucial for filtering the CMC list to only coins that are actually tradable on Binance Futures.
    Using a set provides fast lookups (O(1) average time complexity).
    """
    print("Fetching all tradable symbols from Binance Futures...")
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        # Use a set comprehension for efficient creation.
        # It filters for pairs quoted in USDT and with a 'TRADING' status.
        symbols = {s['symbol'] for s in data['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'}
        print(f"Found {len(symbols)} active symbols on Binance Futures.")
        return symbols
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Error fetching Binance symbols: {e}\n")
        # Return an empty set on failure so the program can handle it gracefully.
        return set()

def api_get(endpoint, params=None):
    """
    A generic helper function to make GET requests to the Binance Futures API.
    This centralizes the request logic, making the code cleaner and easier to maintain.
    """
    base_url = "https://fapi.binance.com"
    url = base_url + endpoint
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        # Log errors to standard error without crashing the program.
        sys.stderr.write(f"\nAPI request failed for {url} with params {params}: {e}\n")
        return None

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# Core Analysis Functions
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

def calculate_passive_order_value(symbol, depth_limit):
    """
    Measures passive market intent by analyzing the order book.
    "Passive" refers to limit orders waiting to be filled.
    It sums the notional value (price * quantity) of bids and asks up to a specified depth.
    A high "total_bids_value" suggests strong passive buying support (a "buy wall").
    A high "total_asks_value" suggests strong passive selling pressure (a "sell wall").
    """
    params = {'symbol': symbol, 'limit': depth_limit}
    response = api_get("/fapi/v1/depth", params)
    if not response: return 0, 0
    # Calculate the total value of all buy orders (bids) in the book.
    total_bids_value = sum(float(p) * float(q) for p, q in response.get('bids', []))
    # Calculate the total value of all sell orders (asks) in the book.
    total_asks_value = sum(float(p) * float(q) for p, q in response.get('asks', []))
    return total_bids_value, total_asks_value

def calculate_active_trade_value(symbol):
    """
    Measures aggressive market action by analyzing recent trades.
    "Active" or "aggressive" refers to market orders that execute immediately against the order book.
    It sums the notional value of recent trades, separating them into taker buys and taker sells.
    - Taker Buy: A market buy order that consumes liquidity from the ask side.
    - Taker Sell: A market sell order that consumes liquidity from the bid side.
    The Binance API's 'm' flag in the trade data is `True` if the buyer is the maker, meaning it was a taker sell.
    It is `False` if the seller is the maker, meaning it was a taker buy.
    """
    # Uses a fixed limit of 1000 (the maximum allowed) to get a robust sample of recent activity.
    params = {'symbol': symbol, 'limit': 1000}
    response = api_get("/fapi/v1/aggTrades", params)
    if not response: return 0, 0
    total_taker_buy_value, total_taker_sell_value = 0, 0
    for trade in response:
        notional_value = float(trade['p']) * float(trade['q'])
        # if 'm' is False, the aggressor was a buyer.
        if not trade['m']:
            total_taker_buy_value += notional_value
        # if 'm' is True, the aggressor was a seller.
        else:
            total_taker_sell_value += notional_value
    return total_taker_buy_value, total_taker_sell_value

def fetch_and_process_symbol(symbol, order_book_depth):
    """
    The main "unit of work" for a single cryptocurrency.
    It orchestrates the fetching and initial calculation for one symbol.
    """
    # Get the passive buy and sell pressure from the order book.
    limit_buy_value, limit_sell_value = calculate_passive_order_value(symbol, order_book_depth)
    # This delay is CRITICAL. Making rapid, back-to-back API calls can easily trigger
    # rate limits or IP bans. A small pause prevents this.
    time.sleep(0.2)
    # Get the aggressive buy and sell pressure from recent trades.
    market_buy_value, market_sell_value = calculate_active_trade_value(symbol)
    
    # Calculate the Limit Order Buy/Sell Ratio.
    # A value > 1 means more money in buy orders than sell orders (bullish passive bias).
    # A value < 1 means more money in sell orders than buy orders (bearish passive bias).
    limit_bs_ratio = limit_buy_value / limit_sell_value if limit_sell_value > 0 else float('inf')
    
    # Calculate the Market Order Buy/Sell Ratio.
    # A value > 1 means more aggressive buying than selling (bullish active pressure).
    # A value < 1 means more aggressive selling than buying (bearish active pressure).
    market_bs_ratio = market_buy_value / market_sell_value if market_sell_value > 0 else float('inf')
    
    # Return a structured dictionary with all the calculated data.
    return {
        "coin": symbol, "limitBuy": int(round(limit_buy_value)),
        "limitSell": int(round(limit_sell_value)), "marketBuy": int(round(market_buy_value)),
        "marketSell": int(round(market_sell_value)), "limit_bs_ratio": limit_bs_ratio,
        "market_bs_ratio": market_bs_ratio
    }

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# Presentation Functions
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

def display_summary_table(results, current_time_str):
    """
    Prints the initial, formatted table of overall analysis results.
    This gives a raw data dump of all analyzed coins for a general market overview.
    (Note: This function call is commented out in main(), but is kept for potential future use).
    """
    print("\n\n--- Market Pressure Analysis ---")
    header = (f"{'Coin':<12} | {'Limit Buy ($)':>20} | {'Limit Sell ($)':>20} | {'Market Buy ($)':>20} | "
              f"{'Market Sell ($)':>20} | {'Limit B/S':>10} | {'Market B/S':>11}")
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    for data in results:
        # Cleans up the symbol name for better readability (e.g., 'BTCUSDT' -> 'BTC').
        display_coin = data['coin'].replace('USDT', '')
        # Formats the numerical data for alignment and readability with commas.
        row = (f"{display_coin:<12} | {data['limitBuy']:>20,} | {data['limitSell']:>20,} | "
               f"{data['marketBuy']:>20,} | {data['marketSell']:>20,} | "
               f"{data['limit_bs_ratio']:>10.2f} | {data['market_bs_ratio']:>11.2f}")
        print(row)
    print("-" * len(header))

def display_trade_setups(all_coin_data):
    """
    Filters, sorts, and displays the curated high-probability setup tables.
    This is the core output of the tool, where raw data is translated into actionable insights.
    """
    # First, calculate the ratio of ratios, which is the key metric for finding divergences.
    for coin in all_coin_data:
        # For shorts, we want to see how much larger the passive buy support is compared to the active sell pressure.
        if coin['market_bs_ratio'] > 0:
            coin['limit_market_ratio'] = coin['limit_bs_ratio'] / coin['market_bs_ratio']
        else:
            coin['limit_market_ratio'] = float('inf') # Avoid division by zero
        
        # For longs, we want to see how much larger the active buy pressure is compared to the passive sell pressure.
        if coin['limit_bs_ratio'] > 0:
            coin['market_limit_ratio'] = coin['market_bs_ratio'] / coin['limit_bs_ratio']
        else:
            coin['market_limit_ratio'] = float('inf') # Avoid division by zero

    # --- SHORT SETUP LOGIC ---
    # The condition `c['limit_bs_ratio'] > 1` means there are more limit buys than sells (a "buy wall").
    # The condition `c['market_bs_ratio'] < 1` means there are more market sells than buys (aggressive selling).
    # COMBINED: This setup finds coins where a supposed "buy wall" is actively being eaten away by sellers.
    # This could signal that the support level is about to fail, making it a good short opportunity.
    potential_shorts = [c for c in all_coin_data if c['limit_bs_ratio'] > 1 and c['market_bs_ratio'] < 1]
    # Sort by the `limit_market_ratio` descending, to show the most extreme examples first.
    potential_shorts.sort(key=lambda x: x['limit_market_ratio'], reverse=True)

    print("\n\n--- Table: Potential Shorts ---")
    header = f"{'Coin':<12} | {'Limit B/S':>15} | {'Market B/S':>15} | {'Limit/Market Ratio':>20}"
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    if not potential_shorts:
        print("No candidates found for this setup.")
    else:
        for data in potential_shorts:
            display_coin = data['coin'].replace('USDT', '')
            row = (f"{display_coin:<12} | {data['limit_bs_ratio']:>15.2f} | {data['market_bs_ratio']:>15.2f} | "
                   f"{data['limit_market_ratio']:>20.2f}")
            print(row)
    print("-" * len(header))

    # --- LONG SETUP LOGIC ---
    # The condition `c['limit_bs_ratio'] < 1` means there are more limit sells than buys (a "sell wall").
    # The condition `c['market_bs_ratio'] > 1` means there are more market buys than sells (aggressive buying).
    # COMBINED: This setup finds coins where a supposed "sell wall" is actively being absorbed by buyers.
    # This could signal that the resistance level is about to break, making it a good long opportunity.
    potential_longs = [c for c in all_coin_data if c['limit_bs_ratio'] < 1 and c['market_bs_ratio'] > 1]
    # Sort by the `market_limit_ratio` descending to prioritize the strongest absorption signals.
    potential_longs.sort(key=lambda x: x['market_limit_ratio'], reverse=True)

    print("\n\n--- Table: Potential Longs ---")
    header = f"{'Coin':<12} | {'Market B/S':>15} | {'Limit B/S':>15} | {'Market/Limit Ratio':>20}"
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    if not potential_longs:
        print("No candidates found for this setup.")
    else:
        for data in potential_longs:
            display_coin = data['coin'].replace('USDT', '')
            row = (f"{display_coin:<12} | {data['market_bs_ratio']:>15.2f} | {data['limit_bs_ratio']:>15.2f} | "
                   f"{data['market_limit_ratio']:>20.2f}")
            print(row)
    print("-" * len(header))

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# Main Program Logic
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

def main():
    """The main entry point and controller of the script."""
    api_key = read_api_key()
    if not api_key: return # Exit if the API key could not be read.

    '''
    # --- USER INPUT SECTION ---
    # These loops robustly handle user input, ensuring the values are valid before proceeding.
    while True:
        try:
            prompt = "Enter the number of coins to analyze (between 1 and 400): "
            FINAL_COIN_COUNT = int(input(prompt))
            if 1 <= FINAL_COIN_COUNT <= 400: break
            else: print("Error: The number must be between 1 and 400.")
        except ValueError: print("Error: Invalid input. Please enter a whole number.")
    
    valid_depths = [5, 10, 20, 50, 100, 500, 1000]
    while True:
        try:
            prompt = f"How many order book levels to analyze? Choose from {valid_depths}: "
            ORDER_BOOK_DEPTH = int(input(prompt))
            if ORDER_BOOK_DEPTH in valid_depths: break
            else: print(f"Error: '{ORDER_BOOK_DEPTH}' is not a valid depth. Please choose from the list.")
        except ValueError: print("Error: Invalid input. Please enter a whole number.")

    while True:
        try:
            prompt = "Enter the time to wait between cycles (in minutes, 1-30): "
            WAIT_MINUTES = int(input(prompt))
            if 1 <= WAIT_MINUTES <= 30: break
            else: print("Error: The number must be between 1 and 30.")
        except ValueError: print("Error: Invalid input. Please enter a whole number.")
    
    '''
    
    # --- HARDCODED CONFIGURATION ---
    print("\n--- GitHub Actions Configuration ---")
    FINAL_COIN_COUNT = 100
    ORDER_BOOK_DEPTH = 500
    print(f"Target Coins: {FINAL_COIN_COUNT}")
    print(f"Order Book Depth: {ORDER_BOOK_DEPTH}")
    print()
    # --- PREPARATION PHASE ---
    start_time = time.time()
    
    # --- INITIAL DATA FETCHING ---
    # Fetch more coins than needed from CMC as a buffer, because not all top CMC coins
    # will have a corresponding futures market on Binance.
    cmc_fetch_limit = FINAL_COIN_COUNT + 200
    cmc_data = get_cmc_market_data(api_key, cmc_fetch_limit)
    binance_symbols_set = get_binance_futures_symbols()

    if not cmc_data or not binance_symbols_set:
        print("Could not fetch all required data. Exiting.")
        return

    # --- DATA RECONCILIATION ---
    # Create the final list of coins to analyze by matching the CMC data with tradable Binance symbols.
    print("\nMatching top market cap coins with Binance Futures symbols...")
    coins_to_analyze = []
    for coin_data in cmc_data:
        symbol = coin_data.get('symbol', '')
        binance_symbol = symbol.upper() + "USDT" # Format to match Binance's standard (e.g., "BTC" -> "BTCUSDT").
        if binance_symbol in binance_symbols_set:
            coins_to_analyze.append(binance_symbol)
        # Stop once the desired number of coins is reached.
        if len(coins_to_analyze) == FINAL_COIN_COUNT:
            break
            
    if not coins_to_analyze:
        print("No matching coins found between CoinMarketCap and Binance Futures.")
        return
    
    print(f"Found {len(coins_to_analyze)} matching coins to analyze.")

    '''
    # --- MAIN ANALYSIS LOOP ---
    try:
        is_first_run = True
        while True: # This loop runs indefinitely until the user stops it (Ctrl+C).
            current_loop_start_time = time.time()
            if not is_first_run:
                print("\n\n")

            current_run_data = []
            total_symbols = len(coins_to_analyze)
            # Using ThreadPoolExecutor to manage the execution of API calls.
            # NOTE: `max_workers=1` makes it run sequentially. This is safer for avoiding
            # strict API rate limits but slower. Increasing this number (e.g., to 5 or 10)
            # would process coins in parallel, speeding up the data gathering phase significantly,
            # but it increases the risk of hitting API rate limits if not managed carefully.
            with ThreadPoolExecutor(max_workers=1) as executor:
                # Create a mapping of future objects to their corresponding symbol.
                future_to_symbol = {executor.submit(fetch_and_process_symbol, symbol, ORDER_BOOK_DEPTH): symbol for symbol in coins_to_analyze}
                
                # `as_completed` yields futures as they finish, allowing for real-time progress updates.
                for i, future in enumerate(as_completed(future_to_symbol), 1):
                    symbol = future_to_symbol[future]
                    try:
                        result = future.result()
                        if result:
                            current_run_data.append(result)
                        time.sleep(0.05) # Small sleep to be gentle on the CPU and API.
                    except Exception as exc:
                        sys.stderr.write(f"\n{symbol} generated an exception: {exc}\n")
                    
                    # This creates a dynamic, single-line progress bar in the console.
                    progress_text = f"Processing ({i}/{total_symbols}): {symbol}".ljust(60)
                    sys.stdout.write(f"\r{progress_text}")
                    sys.stdout.flush()

            print() # Move to the next line after the progress bar is complete.

            # Sort the results to match the original market cap order for consistency.
            current_run_data.sort(key=lambda x: coins_to_analyze.index(x['coin']))
            
            # Get the current timestamp in a user-friendly timezone (IST).
            utc_now = datetime.now(ZoneInfo("UTC"))
            ist_now = utc_now.astimezone(ZoneInfo("Asia/Kolkata"))
            time_str = ist_now.strftime('%Y-%m-%d %H:%M:%S')

            # The main output function call that presents the actionable trade setups.
            # display_summary_table(current_run_data, time_str) # This is disabled by default.
            display_trade_setups(current_run_data)

            is_first_run = False
            
            # --- WAIT/SLEEP LOGIC ---
            # Calculate how long the analysis took.
            elapsed_time = time.time() - current_loop_start_time
            sleep_seconds = WAIT_MINUTES * 60
            # Calculate the remaining time to wait to meet the user's desired cycle time.
            remaining_seconds = max(0, int(sleep_seconds - elapsed_time))

            print(f"\nTotal execution time: {elapsed_time:.2f} seconds")
            print(f"\nAnalysis Time (IST): {time_str}")
            
            # A friendly warning to the user about responsible API usage.
            print("\nIMPORTANT: To avoid potential IP bans from the API for excessive requests, it is recommended to either wait a few minutes before running the script again or restart your Wi--Fi router.\n")
            
            # Display a countdown timer for the waiting period.
            for total_sec in range(remaining_seconds, 0, -1):
                minutes = total_sec // 60
                seconds = total_sec % 60
                timer_text = f"Next result in {minutes:02d}:{seconds:02d}".ljust(50)
                sys.stdout.write(f"\r{timer_text}")
                sys.stdout.flush()
                time.sleep(1)
            
            # Clear the timer line before the next cycle starts.
            sys.stdout.write("\r" + " " * 50 + "\r")
            sys.stdout.flush()

    # Allows the user to stop the script gracefully with Ctrl+C.
    except KeyboardInterrupt:
        print("\n\nMonitor stopped by user. Exiting.")
    '''
    # --- SINGLE ANALYSIS RUN (GitHub Action Workflow) ---
    try:
        current_run_data = []
        total_symbols = len(coins_to_analyze)
        
        # Using max_workers=1 for sequential processing to avoid API bans in CI/CD.
        # Increase cautiously if GHA execution time is too long.
        with ThreadPoolExecutor(max_workers=1) as executor:
            future_to_symbol = {executor.submit(fetch_and_process_symbol, symbol, ORDER_BOOK_DEPTH): symbol for symbol in coins_to_analyze}
            
            print("\nProcessing symbols (this may take a few minutes)...")
            count = 0
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                count += 1
                try:
                    result = future.result()
                    if result:
                        current_run_data.append(result)
                    
                    # Simple progress log specifically for GHA (avoids \r issues)
                    if count % 10 == 0 or count == total_symbols:
                        print(f"Progress: {count}/{total_symbols} completed.")
                        
                    # Sleep to manage rate limits
                    time.sleep(0.01) 
                except Exception as exc:
                    # Log error but continue processing others
                    sys.stderr.write(f"Warning: {symbol} generated an exception: {exc}\n")

        print("\nData gathering complete. Generating report now...")

        # Sort results by original CMC market cap order
        current_run_data.sort(key=lambda x: coins_to_analyze.index(x['coin']))
        
        # Get timestamp
        try:
            utc_now = datetime.now(ZoneInfo("UTC"))
            ist_now = utc_now.astimezone(ZoneInfo("Asia/Kolkata"))
            time_str = ist_now.strftime('%Y-%m-%d %H:%M:%S %Z')
        except Exception:
            # Fallback if timezone setup fails in GHA
            time_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

        # Display Results
        display_summary_table(current_run_data, time_str)
        display_trade_setups(current_run_data)

        # --- FINALIZATION ---
        elapsed_time = time.time() - start_time
        print(f"\nJob finished successfully in {elapsed_time:.2f} seconds.")

    # Catches any other unexpected errors and prints them.
    except Exception as e:
        sys.stderr.write(f"\n\nAn unexpected error occurred: {e}\n")

# This is a standard Python convention. The code inside this block will only run
# when the script is executed directly (not when it's imported as a module).
if __name__ == "__main__":
    main()