import requests
import hmac
import hashlib
import time
import urllib.parse
import decimal # For precise quantity calculation
import os
# import signal # Optional: for graceful shutdown
from dotenv import load_dotenv # Import dotenv
import math # For grid calculations
import sys # For exit

# load_dotenv() # REMOVE THIS - Load conditionally later

# --- Default Strategy Parameters (Internal) ---
# These are used if not overridden by calculation based on USDT amount
DEFAULT_UPPER_PRICE = decimal.Decimal("0.7")
DEFAULT_LOWER_PRICE = decimal.Decimal("0.6")
DEFAULT_NUM_GRIDS = 5
# DEFAULT_ORDER_QTY_PER_GRID = decimal.Decimal("10") # Will be calculated now
DEFAULT_CHECK_INTERVAL_SECONDS = 60
MIN_NOTIONAL_VALUE = decimal.Decimal("5") # Example minimum notional value (check exchange rules!)

# --- Determine Run Mode & Load Params ---
API_KEY = None
SECRET_KEY = None
TARGET_SYMBOL = None
USDT_AMOUNT_STR = None
RUN_MODE = "Unknown"

# Check if run by app.py (presence of VAULT_ vars)
if "VAULT_API_KEY" in os.environ:
    RUN_MODE = "App-Driven"
    print(f"[{RUN_MODE}] Reading parameters from VAULT environment variables...")
    API_KEY = os.environ.get("VAULT_API_KEY")
    SECRET_KEY = os.environ.get("VAULT_SECRET_KEY")
    TARGET_SYMBOL = os.environ.get("VAULT_SYMBOL") # Required
    USDT_AMOUNT_STR = os.environ.get("VAULT_USDT_AMOUNT") # Required
else:
    # Assume Standalone/Debug mode if run directly and VAULT vars missing
    RUN_MODE = "Standalone/Debug"
    print(f"[{RUN_MODE}] VAULT variables not found. Attempting to load from .env...")
    # Load .env file from the project root (one level up from strategies/)
    # Adjust the path if your .env is elsewhere
    dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env') 
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path=dotenv_path)
        print(f"[{RUN_MODE}] Loaded .env file: {dotenv_path}")
        # Use different names if needed, or reuse ASTER_ names like dev version
        API_KEY = os.getenv("ASTER_API_KEY")
        SECRET_KEY = os.getenv("ASTER_SECRET_KEY")
        TARGET_SYMBOL = os.getenv("DEBUG_SYMBOL", "CRVUSDT") # Provide a default
        USDT_AMOUNT_STR = os.getenv("DEBUG_USDT_AMOUNT")
    else:
        print(f"[{RUN_MODE} WARNING] .env file not found at {dotenv_path}. Cannot load debug parameters.")

# --- Validate Core Parameters (Common logic for both modes) ---
if not API_KEY or not SECRET_KEY:
    print(f"[{RUN_MODE} ERROR] API_KEY or SECRET_KEY missing or not loaded.", file=sys.stderr)
    sys.exit(1)
if not TARGET_SYMBOL:
    print(f"[{RUN_MODE} ERROR] TARGET_SYMBOL missing or not loaded.", file=sys.stderr)
    sys.exit(1)
if not USDT_AMOUNT_STR:
    # In debug mode, prompt the user or use a default?
    if RUN_MODE == "Standalone/Debug":
        print(f"[{RUN_MODE} ERROR] DEBUG_USDT_AMOUNT not found in .env file.", file=sys.stderr)
    else: # App-Driven mode
        print(f"[{RUN_MODE} ERROR] VAULT_USDT_AMOUNT missing.", file=sys.stderr)
    sys.exit(1)

try:
    USDT_AMOUNT = decimal.Decimal(USDT_AMOUNT_STR)
    if USDT_AMOUNT <= 0:
        raise ValueError("USDT amount must be positive.")
except (ValueError, decimal.InvalidOperation) as e:
     print(f"[{RUN_MODE} ERROR] Invalid USDT_AMOUNT: {USDT_AMOUNT_STR} - {e}", file=sys.stderr)
     sys.exit(1)

# --- Constants & Precision (Keep as before, maybe fetch dynamically later) ---
BASE_URL = "https://fapi.asterdex.com"
PRICE_PRECISION = decimal.Decimal('0.0001')
QUANTITY_PRECISION = decimal.Decimal('1')
TICK_SIZE = decimal.Decimal('0.0001')

# --- API Functions (Keep as is) ---
def get_server_time():
    """獲取伺服器時間 (用於生成 timestamp)"""
    try:
        response = requests.get(f"{BASE_URL}/fapi/v1/time")
        response.raise_for_status() # 檢查請求是否成功
        return response.json()['serverTime']
    except requests.exceptions.RequestException as e:
        print(f"Error fetching server time: {e}", file=sys.stderr)
        return None

def generate_signature(params_str):
    """生成 HMAC SHA256 簽名"""
    # Ensure SECRET_KEY is available
    if not SECRET_KEY:
         print("[ERROR] SECRET_KEY not loaded for signature generation.", file=sys.stderr)
         # This should not happen if the check above passes, but defensive coding
         return None # Or raise an exception
    return hmac.new(SECRET_KEY.encode('utf-8'), params_str.encode('utf-8'), hashlib.sha256).hexdigest()

def make_signed_request(method, endpoint, params=None):
    """發送簽名的 API 請求 (Corrected version)"""
    if params is None:
        params = {}

    # Use API server time for the request timestamp for consistency
    api_server_time_ms = get_server_time()
    if api_server_time_ms is None:
        print("[ERROR] Could not get server time from API!", file=sys.stderr)
        return None

    # NOTE: The original `params` dict only contains the non-signature parameters at this point
    params_for_signing = params.copy() # Create a copy for signing
    params_for_signing['timestamp'] = int(api_server_time_ms)
    params_for_signing['recvWindow'] = 5000 # 設置請求有效時間窗口 (毫秒)

    # --- 生成待簽名字符串 (基於原始參數，按字母排序 - 正確方式) ---
    query_string_to_sign = urllib.parse.urlencode(sorted(params_for_signing.items()))
    # --- END ---

    # print(f"[DEBUG] String to sign: {query_string_to_sign}") # Optional debug

    # 生成簽名
    signature = generate_signature(query_string_to_sign)
    if signature is None: # Check if signature generation failed
        return None
    # print(f"[DEBUG] Generated signature: {signature}") # Optional debug

    # --- 構建最終請求 URL ---
    # 根據 API 文件示例 1，所有參數 (包括簽名) 都放在 query string 中
    final_query_string = f"{query_string_to_sign}&signature={signature}"
    full_url = f"{BASE_URL}{endpoint}?{final_query_string}"
    # print(f"[DEBUG] Full URL with signature: {full_url}") # Optional debug
    # ----------------------------------

    headers = {
        'X-MBX-APIKEY': API_KEY # Ensure API_KEY is available
    }
    if not API_KEY:
        print("[ERROR] API_KEY not loaded for request.", file=sys.stderr)
        return None

    try:
        # Important: Pass the manually constructed full_url
        # Do NOT use the 'params' argument in requests for signed requests now
        if method.upper() == 'GET':
            # print(f"[DEBUG] Sending GET request to: {full_url}") # Optional debug
            response = requests.get(full_url, headers=headers)
        elif method.upper() == 'POST':
            # print(f"[DEBUG] Sending POST request to: {full_url}") # Optional debug
            # POST request body should be empty as all params are in query string per Example 1
            response = requests.post(full_url, headers=headers)
        elif method.upper() == 'DELETE':
            # print(f"[DEBUG] Sending DELETE request to: {full_url}") # Optional debug
            response = requests.delete(full_url, headers=headers)
        else:
            print(f"Unsupported method: {method}", file=sys.stderr)
            return None

        response.raise_for_status() # 檢查 HTTP 狀態碼
        return response.json()
    except requests.exceptions.RequestException as e:
        # Simplified error printing for grid strategy
        print(f"API Request Error ({method} {endpoint}): {e}", file=sys.stderr)
        if e.response is not None:
            print(f"  Response: {e.response.text}", file=sys.stderr)
        return None

# --- Grid Strategy Specific Functions ---

def get_current_price(symbol):
    """獲取標的的當前市場價格 (使用 ticker price)"""
    endpoint = "/fapi/v1/ticker/price"
    params = {'symbol': symbol}
    try:
        # Use a non-signed request for public data
        response = requests.get(f"{BASE_URL}{endpoint}", params=params)
        response.raise_for_status()
        data = response.json()
        return decimal.Decimal(data['price'])
    except requests.exceptions.RequestException as e:
        print(f"Error fetching current price for {symbol}: {e}", file=sys.stderr)
        return None
    except KeyError as e:
        print(f"Error parsing price response: Missing key {e}", file=sys.stderr)
        return None
    except Exception as e: # Catch other potential errors like JSON decode
        print(f"Unexpected error fetching price for {symbol}: {e}", file=sys.stderr)
        return None


def place_limit_order(symbol, side, quantity, price):
    """下限價單 (Grid uses limit orders)"""
    endpoint = "/fapi/v1/order"

    try:
        # Format price and quantity according to precision rules
        formatted_price = price.quantize(PRICE_PRECISION, rounding=decimal.ROUND_DOWN)
        formatted_quantity = quantity.quantize(QUANTITY_PRECISION, rounding=decimal.ROUND_DOWN)
    except (decimal.InvalidOperation, TypeError) as e:
         print(f"[ERROR] Invalid price ({price}) or quantity ({quantity}) for formatting: {e}", file=sys.stderr)
         return None


    # Add minimum order checks if needed (minQty, minNotional)
    # minQty = decimal.Decimal('1') # Example for CRV
    # minNotional = decimal.Decimal('5') # Example for CRV
    # if formatted_quantity < minQty:
    #    print(f"Order quantity {formatted_quantity} below minQty {minQty}. Skipping.")
    #    return None
    # try:
    #     notional = formatted_quantity * formatted_price
    #     if notional < minNotional:
    #         print(f"Order notional value ({notional}) below minNotional {minNotional}. Skipping.")
    #         return None
    # except (decimal.InvalidOperation, TypeError):
    #      print(f"[WARN] Could not calculate notional value for order check.")


    params = {
        'symbol': symbol,
        'side': side.upper(),
        'type': 'LIMIT',
        'quantity': str(formatted_quantity),
        'price': str(formatted_price),
        'timeInForce': 'GTC'  # Good Till Cancel for grid orders
    }
    print(f"Attempting to place LIMIT {side} order: {formatted_quantity} {symbol.replace('USDT', '')} at {formatted_price}")
    result = make_signed_request('POST', endpoint, params)
    return result

def get_open_orders(symbol):
    """獲取指定交易對的當前掛單"""
    endpoint = "/fapi/v1/openOrders"
    params = {'symbol': symbol}
    # print(f"Fetching open orders for {symbol}...") # Optional debug
    result = make_signed_request('GET', endpoint, params)
    # Return an empty list if request fails or no orders
    return result if isinstance(result, list) else [] # Ensure it returns a list on error too


def cancel_order(symbol, order_id):
    """取消指定訂單"""
    endpoint = "/fapi/v1/order"
    params = {
        'symbol': symbol,
        'orderId': str(order_id)
    }
    print(f"Attempting to cancel order ID: {order_id} for {symbol}")
    result = make_signed_request('DELETE', endpoint, params)
    return result

def cancel_all_open_orders(symbol):
    """取消指定交易對的所有掛單"""
    endpoint = "/fapi/v1/allOpenOrders"
    params = {'symbol': symbol}
    print(f"Attempting to cancel ALL open orders for {symbol}...")
    result = make_signed_request('DELETE', endpoint, params)
    # Check for success based on common API responses (adjust if needed)
    if result and isinstance(result, dict) and result.get('code') == 200: # Example success code
         print(f"Successfully requested cancellation of all open orders for {symbol}.")
         return True
    elif isinstance(result, list) and len(result) == 0: # Another possible success response (empty list)
        print(f"Successfully cancelled all open orders for {symbol} (or none existed).")
        return True
    else:
         print(f"Failed to cancel all open orders for {symbol}. Response: {result}", file=sys.stderr)
         return False

# --- Grid Calculation Logic ---
def calculate_grid_levels(upper_price, lower_price, num_grids):
    """計算網格價格水平 (等差網格)"""
    # Input validation already happens before call in __main__ ideally
    # but keep basic checks
    if not isinstance(upper_price, decimal.Decimal) or not isinstance(lower_price, decimal.Decimal):
         print("[ERROR] Prices must be Decimal objects for calculation.", file=sys.stderr)
         return []
    if upper_price <= lower_price or num_grids < 1:
        print(f"[ERROR] Invalid grid parameters: upper={upper_price}, lower={lower_price}, num_grids={num_grids}", file=sys.stderr)
        return []

    try:
        grid_step = (upper_price - lower_price) / decimal.Decimal(str(num_grids))
        if grid_step <= 0:
            print(f"[ERROR] Calculated grid step is zero or negative. Check parameters.", file=sys.stderr)
            return []

        levels = [lower_price + i * grid_step for i in range(num_grids + 1)]

        # Format levels according to price precision
        formatted_levels = [lvl.quantize(PRICE_PRECISION, rounding=decimal.ROUND_DOWN) for lvl in levels]

        # Remove duplicates and sort descending
        unique_sorted_levels = sorted(list(set(formatted_levels)), reverse=True)

        # Sanity check number of levels
        if len(unique_sorted_levels) < 2: # Need at least upper and lower bound
             print("[WARNING] Too few unique grid levels generated. Price range might be too narrow for the precision.", file=sys.stderr)
             # Decide whether to return empty list or the few levels found
             # return []
        elif len(unique_sorted_levels) < num_grids / 2: # Arbitrary check for significant reduction
            print(f"[WARNING] Number of unique levels ({len(unique_sorted_levels)}) significantly less than expected ({num_grids+1}).", file=sys.stderr)


        return unique_sorted_levels
    except (decimal.InvalidOperation, OverflowError, TypeError) as e:
        print(f"[ERROR] Error during grid level calculation: {e}", file=sys.stderr)
        return []


# --- Optional: Graceful Shutdown ---
# shutdown_requested = False
# def handle_shutdown(signum, frame):
#     global shutdown_requested
#     print(f"Shutdown signal ({signum}) received. Requesting graceful exit...")
#     shutdown_requested = True

# Setup signal handling
# signal.signal(signal.SIGTERM, handle_shutdown)
# signal.signal(signal.SIGINT, handle_shutdown)
# ------------------------------------


# --- Main Execution ---
if __name__ == "__main__":
    # Parameters are now read from environment variables at the top

    # --- Use Internal Default Parameters --- 
    upper_p = DEFAULT_UPPER_PRICE
    lower_p = DEFAULT_LOWER_PRICE
    num_g = DEFAULT_NUM_GRIDS
    check_interval = DEFAULT_CHECK_INTERVAL_SECONDS
    # --------------------------------------

    # --- Calculate Order Quantity based on USDT_AMOUNT --- 
    avg_price = (lower_p + upper_p) / 2
    if avg_price <= 0 or num_g <= 0:
        print(f"[ERROR] Cannot calculate order quantity: Invalid avg price ({avg_price}) or num grids ({num_g}).", file=sys.stderr)
        sys.exit(1)
        
    # Simple calculation: Distribute USDT evenly across potential grid slots (buy+sell ~ num_grids)
    # Adjust if only funding buys etc.
    estimated_qty_per_grid = (USDT_AMOUNT / decimal.Decimal(num_g)) / avg_price
    
    # Apply quantity precision
    calculated_order_qty = estimated_qty_per_grid.quantize(QUANTITY_PRECISION, decimal.ROUND_DOWN)

    print(f"[INFO] Based on USDT_AMOUNT={USDT_AMOUNT}, AvgPrice={avg_price:.4f}, NumGrids={num_g} -> Calculated QtyPerGrid={calculated_order_qty}")

    # --- Sanity Check Calculated Quantity --- 
    if calculated_order_qty <= 0:
         print(f"[ERROR] Calculated order quantity is zero or negative ({calculated_order_qty}). USDT amount might be too low for the grid settings and precision.", file=sys.stderr)
         sys.exit(1)
         
    # Check Minimum Notional Value (using lowest price for worst case)
    min_notional_check = calculated_order_qty * lower_p
    if min_notional_check < MIN_NOTIONAL_VALUE:
        print(f"[ERROR] Calculated order notional value ({min_notional_check:.4f} USDT at lowest price) is below minimum required ({MIN_NOTIONAL_VALUE} USDT). Increase USDT amount or adjust grid.", file=sys.stderr)
        sys.exit(1)
        
    ORDER_QTY_PER_GRID = calculated_order_qty # Use the calculated quantity
    # -------------------------------------------

    # --- Initial Setup Output --- 
    print("--- Normal Grid Strategy Initializing (from Vault) ---")
    print(f"Symbol: {TARGET_SYMBOL}")
    print(f"USDT Amount: {USDT_AMOUNT}")
    print(f"Using Default Grid Range: {lower_p} - {upper_p}")
    print(f"Using Default Number of Grids: {num_g}")
    print(f"==> Calculated Order Quantity per Grid: {ORDER_QTY_PER_GRID} {TARGET_SYMBOL.replace('USDT','')}")
    print(f"Using Precisions: Price={PRICE_PRECISION}, Qty={QUANTITY_PRECISION}, Tick={TICK_SIZE}")

    # --- Calculate Grid Levels --- 
    print("Calculating Arithmetic Grid Levels...")
    grid_levels = calculate_grid_levels(upper_p, lower_p, num_g)
    if not grid_levels:
        print("[ERROR] Failed to calculate grid levels. Exiting.", file=sys.stderr)
        sys.exit(1)
    print("Calculated Grid Levels:", [float(lvl) for lvl in grid_levels])
    print(f"Number of levels generated: {len(grid_levels)}")


    # --- Optional: Cancel existing orders before starting ---
    # print("Attempting to cancel any existing open orders...")
    # cancel_all_open_orders(TARGET_SYMBOL)
    # time.sleep(2) # Give time for cancellation to process

    # --- Main Loop ---
    print("--- Starting Grid Maintenance Loop ---")
    while True:
        # --- Optional: Check for shutdown request ---
        # if shutdown_requested:
        #     print("Shutdown requested during main loop. Cancelling orders...")
        #     cancel_all_open_orders(TARGET_SYMBOL)
        #     time.sleep(5) # Allow time for cancellation
        #     print("Exiting strategy.")
        #     break
        # ------------------------------------------

        try:
            print(f"--- Checking Grid ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---")
            current_price = get_current_price(TARGET_SYMBOL)
            if current_price is None:
                print("Failed to get current price. Retrying next cycle.")
                time.sleep(check_interval)
                continue # Skip this cycle

            if not isinstance(current_price, decimal.Decimal):
                print(f"Current price is not a Decimal ({type(current_price)}). Skipping cycle.")
                time.sleep(check_interval)
                continue

            # Calculate the number of decimal places from PRICE_PRECISION
            num_decimal_places = abs(PRICE_PRECISION.as_tuple().exponent) 
            print(f"Current Market Price: {current_price:.{num_decimal_places}f}") # Format price

            open_orders_list = get_open_orders(TARGET_SYMBOL)
            open_order_prices = {
                'BUY': set(),
                'SELL': set()
            }
            # Keep track of order IDs for potential cancellation later
            open_order_ids = {'BUY': {}, 'SELL': {}} # {price: orderId}

            if isinstance(open_orders_list, list): # Ensure we got a list
                 for order in open_orders_list:
                     try:
                         # Ensure price is treated as Decimal and quantized consistently
                         order_price_str = order.get('price')
                         order_side = order.get('side')
                         order_id = order.get('orderId')

                         if order_price_str and order_side and order_id:
                            order_price = decimal.Decimal(order_price_str).quantize(PRICE_PRECISION)
                            order_side = order_side.upper()
                            if order_side in open_order_prices:
                                open_order_prices[order_side].add(order_price)
                                open_order_ids[order_side][order_price] = order_id # Store ID
                            else:
                                print(f"Warning: Unknown order side '{order_side}' for order {order_id}")
                         else:
                            print(f"Warning: Incomplete order data found: {order}")

                     except (KeyError, decimal.InvalidOperation, TypeError) as e:
                         print(f"Warning: Could not parse open order {order.get('orderId', 'N/A')}: {e}")
            else:
                 print(f"[Warning] Failed to get open orders or received unexpected format: {open_orders_list}")


            print(f"Open Orders Found: BUYs at {sorted([float(p) for p in open_order_prices['BUY']])}, SELLs at {sorted([float(p) for p in open_order_prices['SELL']])}")

            # --- Grid Maintenance Logic ---
            placed_orders_this_cycle = 0
            # Use a copy of grid levels if you modify it during iteration
            for level_price in grid_levels:
                 # Ensure level_price is Decimal
                 if not isinstance(level_price, decimal.Decimal):
                      print(f"Warning: Skipping invalid level price type {type(level_price)}")
                      continue

                 level_quantized = level_price.quantize(PRICE_PRECISION) # Use quantized level for comparisons

                 # --- Buy Side Logic ---
                 # Place BUY if level is below current price AND no BUY order exists at this level
                 if level_quantized < current_price:
                     if level_quantized not in open_order_prices['BUY']:
                         print(f"Missing BUY order at {level_quantized}. Placing...")
                         place_result = place_limit_order(TARGET_SYMBOL, 'BUY', ORDER_QTY_PER_GRID, level_quantized)
                         if place_result and isinstance(place_result, dict) and 'orderId' in place_result:
                              print(f"  Successfully placed BUY order {place_result['orderId']} at {level_quantized}")
                              placed_orders_this_cycle += 1
                              time.sleep(0.2) # Small delay between placements
                         else:
                              print(f"  Failed to place BUY order at {level_quantized}. Response: {place_result}")
                              # Consider adding retry logic or error tracking here

                 # --- Sell Side Logic ---
                 # Place SELL if level is above current price AND no SELL order exists at this level
                 elif level_quantized > current_price:
                      if level_quantized not in open_order_prices['SELL']:
                           print(f"Missing SELL order at {level_quantized}. Placing...")
                           place_result = place_limit_order(TARGET_SYMBOL, 'SELL', ORDER_QTY_PER_GRID, level_quantized)
                           if place_result and isinstance(place_result, dict) and 'orderId' in place_result:
                                print(f"  Successfully placed SELL order {place_result['orderId']} at {level_quantized}")
                                placed_orders_this_cycle += 1
                                time.sleep(0.2) # Small delay between placements
                           else:
                                print(f"  Failed to place SELL order at {level_quantized}. Response: {place_result}")
                                # Consider adding retry logic or error tracking here

                 # --- (Optional) Remove Orders On Wrong Side ---
                 # Cancel BUY orders found ABOVE current price
                 # if level_quantized > current_price and level_quantized in open_order_prices['BUY']:
                 #      order_id_to_cancel = open_order_ids['BUY'].get(level_quantized)
                 #      if order_id_to_cancel:
                 #          print(f"Found BUY order {order_id_to_cancel} at {level_quantized} which is above current price {current_price}. Cancelling...")
                 #          cancel_order(TARGET_SYMBOL, order_id_to_cancel)
                 #          time.sleep(0.2)
                 # Cancel SELL orders found BELOW current price
                 # elif level_quantized < current_price and level_quantized in open_order_prices['SELL']:
                 #      order_id_to_cancel = open_order_ids['SELL'].get(level_quantized)
                 #      if order_id_to_cancel:
                 #           print(f"Found SELL order {order_id_to_cancel} at {level_quantized} which is below current price {current_price}. Cancelling...")
                 #           cancel_order(TARGET_SYMBOL, order_id_to_cancel)
                 #           time.sleep(0.2)
                 # ---------------------------------------------

            print(f"Grid check complete. Placed {placed_orders_this_cycle} new orders.")

        except KeyboardInterrupt:
             print("Keyboard interrupt received. Stopping strategy...")
             # handle_shutdown(signal.SIGINT, None) # Call handler manually if not using signals
             # cancel_all_open_orders(TARGET_SYMBOL) # Ensure cancellation
             break # Exit the main loop
        except Exception as e:
            print(f"An error occurred in the main loop: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc() # Print full traceback for debugging
            # Add more robust error handling / logging here if needed
            # Decide if the error is critical and should stop the strategy

        # print(f"Waiting for {check_interval} seconds until next check...")
        # Use a loop for sleep to check shutdown flag more often if needed
        # for _ in range(check_interval):
        #      if shutdown_requested: break
        #      time.sleep(1)
        # if shutdown_requested: break # Exit outer loop too
        # Without frequent check:
        time.sleep(check_interval)

    print("Strategy execution finished.") 