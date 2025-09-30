# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# Order Flow Imbalance Analyzer
#
# USE CASE:
# This script identifies top cryptocurrencies by market capitalization that are also
# tradable on Binance Futures. It then analyzes their raw order book and trade flow
# data, presenting a main summary and then curating specific lists of potential long
# and short trade setups based on the underlying numerical pressure ratios. It is a
# powerful tool for traders to quickly scan for and rank high-probability trading
# opportunities in the most significant assets.
#
# CONCEPTS:
# - Passive Orders (The "Wall"): These are limit orders sitting on the order book,
#   representing where traders WANT to buy or sell. We measure this with the
#   Limit B/S (Buy/Sell) Ratio.
# - Aggressive Orders (The "Action"): These are market orders that execute
#   immediately, consuming liquidity and driving the price. They represent what traders
#   ARE DOING right now. We measure this with the Market B/S Ratio.
# - Combined Ratios: The core metrics for ranking trade setup probability by comparing
#   the relative strength of passive vs. aggressive forces.
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

# Import necessary libraries
import requests      # For making HTTP requests to APIs
import sys           # For interacting with the system (e.g., writing to stderr for errors)
import time          # For timing the script's execution and adding delays
from datetime import datetime
from zoneinfo import ZoneInfo # For timezone-aware datetimes
from concurrent.futures import ThreadPoolExecutor, as_completed # For parallel processing (if needed)

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# API and Data Fetching Functions
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

def read_api_key(filename="CoinMarketCapAPIKey.txt"):
    """
    Reads the CoinMarketCap API key from a local text file.
    This is good practice for security, as it keeps your secret key out of the main script.
    """
    try:
        # Open the file in read mode ('r') using a 'with' statement for safe handling.
        with open(filename, 'r') as f:
            # Read the key and use .strip() to remove any accidental whitespace or newlines.
            key = f.read().strip()
        if not key:
            print(f"Error: The file '{filename}' is empty.")
            return None
        return key
    except FileNotFoundError:
        # If the file doesn't exist, provide a helpful error message to the user.
        print(f"Error: API key file '{filename}' not found.")
        print("Please create this file and paste your CoinMarketCap API key into it.")
        return None

def get_cmc_market_data(api_key, limit):
    """
    Fetches the top cryptocurrencies sorted by market cap from CoinMarketCap.
    This provides the "fundamental" data (market cap) that Binance's trading API lacks,
    allowing us to focus only on the most significant coins.
    """
    print(f"Fetching top {limit} coins from CoinMarketCap...")
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    headers = {'Accepts': 'application/json', 'X-CMC_PRO_API_KEY': api_key}
    params = {'start': '1', 'limit': limit, 'convert': 'USD', 'sort': 'market_cap'}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        # The actual coin data is nested under the 'data' key in the JSON response.
        return response.json().get('data', [])
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Error fetching CoinMarketCap data: {e}\n")
        return None

def get_binance_futures_symbols():
    """
    Fetches a set of all actively traded USDT-margined futures symbols from Binance.
    This gives us the "universe" of coins that we can actually analyze.
    """
    print("Fetching all tradable symbols from Binance Futures...")
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        # We use a set for `symbols` because checking for existence (`in`) is much faster
        # in a set than in a list. This provides a significant performance boost when we
        # match thousands of CMC symbols against the Binance list.
        symbols = {s['symbol'] for s in data['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'}
        print(f"Found {len(symbols)} active symbols on Binance Futures.")
        return symbols
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Error fetching Binance symbols: {e}\n")
        return set()

def api_get(endpoint, params=None):
    """
    Makes a GET request to a specific Binance Futures API endpoint. A generic helper.
    """
    base_url = "https://fapi.binance.com"
    url = base_url + endpoint
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"\nAPI request failed for {url} with params {params}: {e}\n")
        return None

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# Core Analysis Functions
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

def calculate_passive_order_value(symbol):
    """
    Measures passive intent by summing the entire notional value of the order book.
    This function quantifies the "Wall" of buy and sell orders.
    """
    params = {'symbol': symbol, 'limit': 1000}
    response = api_get("/fapi/v1/depth", params)
    if not response: return 0, 0
    total_bids_value = sum(float(p) * float(q) for p, q in response.get('bids', []))
    total_asks_value = sum(float(p) * float(q) for p, q in response.get('asks', []))
    return total_bids_value, total_asks_value

def calculate_active_trade_value(symbol):
    """
    Measures aggressive action by summing the notional value of recent trades.
    This function quantifies the "Action" or "Flow" of the market.
    """
    params = {'symbol': symbol, 'limit': 1000}
    response = api_get("/fapi/v1/aggTrades", params)
    if not response: return 0, 0
    total_taker_buy_value, total_taker_sell_value = 0, 0
    for trade in response:
        notional_value = float(trade['p']) * float(trade['q'])
        if not trade['m']: # If buyer was NOT the maker, they were the Taker (aggressive buy).
            total_taker_buy_value += notional_value
        else: # If buyer WAS the maker, the seller must have been the Taker (aggressive sell).
            total_taker_sell_value += notional_value
    return total_taker_buy_value, total_taker_sell_value

def fetch_and_process_symbol(symbol):
    """
    The main "unit of work" for a single coin. It orchestrates the fetching,
    calculation, and analysis for one symbol and returns a structured data dictionary.
    """
    # Fetch passive data (order book).
    limit_buy_value, limit_sell_value = calculate_passive_order_value(symbol)
    # Add a small, crucial delay between the two heavy API calls. This prevents sending a
    # rapid burst of requests and makes the script much safer from rate-limiting.
    time.sleep(0.2)
    # Fetch active data (trade flow).
    market_buy_value, market_sell_value = calculate_active_trade_value(symbol)
    
    # Ratios standardize the raw dollar values into a comparable metric of strength.
    # We handle division-by-zero cases by returning infinity ('inf').
    limit_bs_ratio = limit_buy_value / limit_sell_value if limit_sell_value > 0 else float('inf')
    market_bs_ratio = market_buy_value / market_sell_value if market_sell_value > 0 else float('inf')
    
    # Return a dictionary containing all the calculated data.
    return {
        "coin": symbol, "limitBuy": int(round(limit_buy_value)),
        "limitSell": int(round(limit_sell_value)), "marketBuy": int(round(market_buy_value)),
        "marketSell": int(round(market_sell_value)), "limit_bs_ratio": limit_bs_ratio,
        "market_bs_ratio": market_bs_ratio
    }

# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=
# Presentation Functions
# =-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=

def display_summary_table(results):
    """
    Prints the initial, formatted table of overall analysis results, sorted by market cap.
    This gives a high-level overview of the market conditions.
    """
    print("\n\n--- Market Pressure Analysis ---")
    header = (f"{'Coin':<12} | {'Limit Buy ($)':>20} | {'Limit Sell ($)':>20} | {'Market Buy ($)':>20} | "
              f"{'Market Sell ($)':>20} | {'Limit B/S':>10} | {'Market B/S':>11}")
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    for data in results:
        display_coin = data['coin'].replace('USDT', '')
        row = (f"{display_coin:<12} | {data['limitBuy']:>20,} | {data['limitSell']:>20,} | "
               f"{data['marketBuy']:>20,} | {data['marketSell']:>20,} | "
               f"{data['limit_bs_ratio']:>10.2f} | {data['market_bs_ratio']:>11.2f}")
        print(row)
    print("-" * len(header))

def display_trade_setups(all_coin_data):
    """
    Filters, sorts, and displays the curated high-probability setup tables.
    This is the core of turning raw data into actionable trading intelligence.
    """
    # Step 1: Enrich the data with the combined ratios for ranking. These are the key
    # metrics for determining the probability of a setup.
    for coin in all_coin_data:
        # Ratio for ranking support strength vs. selling attack (for Potential Shorts/Reversals)
        if coin['market_bs_ratio'] > 0:
            coin['limit_market_ratio'] = coin['limit_bs_ratio'] / coin['market_bs_ratio']
        else:
            coin['limit_market_ratio'] = float('inf')
        
        # Ratio for ranking buying attack strength vs. resistance (for Potential Longs/Breakouts)
        if coin['limit_bs_ratio'] > 0:
            coin['market_limit_ratio'] = coin['market_bs_ratio'] / coin['limit_bs_ratio']
        else:
            coin['market_limit_ratio'] = float('inf')

    # --- Table 1: Potential Shorts (based on "Support Attack" scenario) ---
    # This table identifies coins where a passive buy wall is being attacked by aggressive sellers.
    potential_shorts = [c for c in all_coin_data if c['limit_bs_ratio'] > 1 and c['market_bs_ratio'] < 1]
    # We sort DESCENDING by the Limit/Market Ratio. Based on market observation, a very high
    # ratio (e.g., > 5) implies the buy wall is a dangerously large and crowded liquidity pool
    # that aggressive sellers are targeting. This indicates a high probability of a stop-loss
    # cascade and a sharp BREAKDOWN if the level fails, making it a potential short setup.
    # A lower ratio (closer to 1) indicates a more balanced fight.
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

    # --- Table 2: Potential Longs (based on "Resistance Attack" scenario) ---
    # This table identifies coins where aggressive buyers are attacking a passive sell wall.
    potential_longs = [c for c in all_coin_data if c['limit_bs_ratio'] < 1 and c['market_bs_ratio'] > 1]
    # We sort DESCENDING by the Market/Limit Ratio. A higher ratio (e.g., > 5) means the
    # aggressive buying power is overwhelmingly stronger than the passive resistance. This
    # indicates a high probability of a successful BREAKOUT and a potential short squeeze,
    # making it a strong long candidate.
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
    start_time = time.time()

    # Step 1: Read the CoinMarketCap API key.
    api_key = read_api_key()
    if not api_key: return

    # Step 2: Define the number of coins to analyze. This value is now hardcoded.
    FINAL_COIN_COUNT = 200 # You can change this value to 50, 150, etc.
    print(f"Analysis configured to run for {FINAL_COIN_COUNT} coins.")
    
    # Step 3: Fetch the initial lists of coins from both APIs.
    cmc_fetch_limit = FINAL_COIN_COUNT + 200
    cmc_data = get_cmc_market_data(api_key, cmc_fetch_limit)
    binance_symbols_set = get_binance_futures_symbols()

    if not cmc_data or not binance_symbols_set:
        print("Could not fetch all required data. Exiting.")
        return

    # Step 4: Create the final, curated list of coins to analyze by finding the intersection.
    print("\nMatching top market cap coins with Binance Futures symbols...")
    coins_to_analyze = []
    # Iterate through the CMC list, which is already sorted by market cap.
    for coin_data in cmc_data:
        symbol = coin_data.get('symbol', '')
        binance_symbol = symbol.upper() + "USDT"
        # If the CMC symbol is also tradable on Binance Futures, add it to our list.
        if binance_symbol in binance_symbols_set:
            coins_to_analyze.append(binance_symbol)
        # Stop once we have found the exact number of coins the user requested.
        if len(coins_to_analyze) == FINAL_COIN_COUNT:
            break
            
    if not coins_to_analyze:
        print("No matching coins found between CoinMarketCap and Binance Futures.")
        return
    
    print(f"Found {len(coins_to_analyze)} matching coins to analyze.")

    # Step 5: Execute the heavy analysis on our final list.
    all_coin_data = []
    total_symbols = len(coins_to_analyze)
    # This script is set to run sequentially (max_workers=1) to be safe with API limits.
    # The delays inside and after fetch_and_process_symbol are crucial for this.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future_to_symbol = {executor.submit(fetch_and_process_symbol, symbol): symbol for symbol in coins_to_analyze}
        
        for i, future in enumerate(as_completed(future_to_symbol), 1):
            symbol = future_to_symbol[future]
            try:
                result = future.result()
                if result:
                    all_coin_data.append(result)
                # Add a delay between processing each coin to stay well within rate limits.
                time.sleep(0.2)
            except Exception as exc:
                sys.stderr.write(f"\n{symbol} generated an exception: {exc}\n")
            
            # This creates the dynamic, single-line progress indicator.
            progress_text = f"Processing ({i}/{total_symbols}): {symbol}".ljust(60)
            sys.stdout.write(f"\r{progress_text}")
            sys.stdout.flush()

    print()
    
    # Step 6: Sort the main result list by its original market cap rank for the summary table.
    all_coin_data.sort(key=lambda x: coins_to_analyze.index(x['coin']))
    
    # --- Step 7: Display all generated tables ---
    # display_summary_table(all_coin_data)
    display_trade_setups(all_coin_data)
    
    end_time = time.time()
    print(f"\nTotal execution time: {end_time - start_time:.2f} seconds")
    
    # Add a newline for spacing.
    print()
    # Get the current time in UTC and convert it to the IST timezone.
    utc_now = datetime.now(ZoneInfo("UTC"))
    ist_now = utc_now.astimezone(ZoneInfo("Asia/Kolkata"))
    # Format the time into the exact string requested.
    time_str = ist_now.strftime('%Y-%m-%d %H:%M:%S')
    # Print the final formatted string.
    print(f"Analysis Time (IST): {time_str}")
    
    # --- Final Warning Message ---
    print("\nIMPORTANT: To avoid potential IP bans from the API for excessive requests, it is recommended to either wait a few minutes before running the script again or restart your Wi-Fi router.\n\n")

if __name__ == "__main__":
    main()