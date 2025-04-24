import requests
import hmac
import hashlib
import time
import urllib.parse
import decimal # For precise quantity calculation
import os
from dotenv import load_dotenv
import math # For grid calculations

# Load environment variables from .env file
load_dotenv()

# --- Get API Keys from Environment Variables ---
API_KEY = os.getenv("ASTER_API_KEY")
SECRET_KEY = os.getenv("ASTER_SECRET_KEY")

# --- Check if keys were loaded ---
if not API_KEY or not SECRET_KEY:
    print("[ERROR] API_KEY or SECRET_KEY not found in environment variables.")
    print("Please ensure you have a .env file with ASTER_API_KEY and ASTER_SECRET_KEY defined.")
    exit(1) # Exit the script if keys are missing
# Removed temporary debug print
# ------------------------------------------

BASE_URL = "https://fapi.asterdex.com"

# --- Precision Settings (CRVUSDT - initial values, should be confirmed/updated) ---
# Price precision (decimal places for price)
PRICE_PRECISION = decimal.Decimal('0.0001') # 4 decimal places
# Quantity precision (decimal places for quantity)
QUANTITY_PRECISION = decimal.Decimal('1') # 0 decimal places (integer)
TICK_SIZE = decimal.Decimal('0.0001') # Smallest price change (matches price precision)
# ----------------------------------------------------------------------------------

def get_server_time():
    """獲取伺服器時間 (用於生成 timestamp)"""
    try:
        response = requests.get(f"{BASE_URL}/fapi/v1/time")
        response.raise_for_status() # 檢查請求是否成功
        return response.json()['serverTime']
    except requests.exceptions.RequestException as e:
        print(f"Error fetching server time: {e}")
        return None

def generate_signature(params_str):
    """生成 HMAC SHA256 簽名"""
    return hmac.new(SECRET_KEY.encode('utf-8'), params_str.encode('utf-8'), hashlib.sha256).hexdigest()

def make_signed_request(method, endpoint, params=None):
    """發送簽名的 API 請求 (Corrected version)"""
    if params is None:
        params = {}

    # Use API server time for the request timestamp for consistency
    api_server_time_ms = get_server_time()
    if api_server_time_ms is None:
        print("[ERROR] Could not get server time from API!")
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
    # print(f"[DEBUG] Generated signature: {signature}") # Optional debug

    # --- 構建最終請求 URL ---
    # 根據 API 文件示例 1，所有參數 (包括簽名) 都放在 query string 中
    final_query_string = f"{query_string_to_sign}&signature={signature}"
    full_url = f"{BASE_URL}{endpoint}?{final_query_string}"
    # print(f"[DEBUG] Full URL with signature: {full_url}") # Optional debug
    # ----------------------------------

    headers = {
        'X-MBX-APIKEY': API_KEY
    }

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
            print(f"Unsupported method: {method}")
            return None

        response.raise_for_status() # 檢查 HTTP 狀態碼
        return response.json()
    except requests.exceptions.RequestException as e:
        # Simplified error printing for grid strategy
        print(f"API Request Error ({method} {endpoint}): {e}")
        if e.response is not None:
            print(f"  Response: {e.response.text}")
        return None

# --- Grid Strategy Specific Functions ---

def get_current_price(symbol):
    """獲取標的的當前市場價格 (使用 ticker price)"""
    endpoint = "/fapi/v1/ticker/price"
    params = {'symbol': symbol}
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params)
        response.raise_for_status()
        data = response.json()
        return decimal.Decimal(data['price'])
    except requests.exceptions.RequestException as e:
        print(f"Error fetching current price for {symbol}: {e}")
        return None
    except KeyError as e:
        print(f"Error parsing price response: Missing key {e}")
        return None

def place_limit_order(symbol, side, quantity, price):
    """下限價單 (Grid uses limit orders)"""
    endpoint = "/fapi/v1/order"

    # Format price and quantity according to precision rules
    formatted_price = price.quantize(PRICE_PRECISION, rounding=decimal.ROUND_DOWN)
    formatted_quantity = quantity.quantize(QUANTITY_PRECISION, rounding=decimal.ROUND_DOWN)

    # Add minimum order checks if needed (minQty, minNotional)
    # minQty = decimal.Decimal('1') # Example for CRV
    # minNotional = decimal.Decimal('5') # Example for CRV
    # if formatted_quantity < minQty:
    #    print(f"Order quantity {formatted_quantity} below minQty {minQty}. Skipping.")
    #    return None
    # if (formatted_quantity * formatted_price) < minNotional:
    #    print(f"Order notional value below minNotional {minNotional}. Skipping.")
    #    return None

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
    return result if result else []

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
    if result and isinstance(result, dict) and result.get('code') == 200:
         print(f"Successfully requested cancellation of all open orders for {symbol}.")
         return True
    else:
         print(f"Failed to cancel all open orders for {symbol}. Response: {result}")
         return False

# --- Grid Calculation Logic ---
def calculate_grid_levels(upper_price, lower_price, num_grids):
    """計算網格價格水平 (等比/對數網格)"""
    upper_price = decimal.Decimal(str(upper_price))
    lower_price = decimal.Decimal(str(lower_price))

    if upper_price <= lower_price or num_grids < 1:
        print("[ERROR] Invalid grid parameters for logarithmic grid.")
        return []

    if lower_price <= 0:
        print("[ERROR] Lower price must be positive for logarithmic grid.")
        return []

    try:
        # Calculate the constant ratio between grid levels
        # r = (upper / lower)^(1 / num_grids)
        # Use Decimal's power function for precision
        ratio = (upper_price / lower_price) ** (decimal.Decimal(1) / decimal.Decimal(str(num_grids)))

        # Generate levels: L, L*r, L*r^2, ..., L*r^N (=U)
        levels = [lower_price * (ratio ** decimal.Decimal(str(i))) for i in range(num_grids + 1)]

    except (decimal.InvalidOperation, OverflowError) as e:
        print(f"[ERROR] Could not calculate logarithmic grid levels: {e}")
        print("Check if upper/lower prices and num_grids are reasonable.")
        return []

    # Format levels according to price precision
    formatted_levels = [lvl.quantize(PRICE_PRECISION, rounding=decimal.ROUND_DOWN) for lvl in levels]

    # Return unique, sorted levels (highest first)
    # Use a tolerance for uniqueness check due to potential floating point inaccuracies even with Decimal
    unique_levels = []
    tolerance = PRICE_PRECISION / 2 # Define a small tolerance
    temp_sorted = sorted(formatted_levels, reverse=True)
    if temp_sorted:
        unique_levels.append(temp_sorted[0])
        for i in range(1, len(temp_sorted)):
            # Only add if the difference is larger than tolerance
            if abs(temp_sorted[i] - temp_sorted[i-1]) > tolerance:
                unique_levels.append(temp_sorted[i])

    # Ensure the number of levels is still close to expected N+1
    if len(unique_levels) < num_grids: # Check if too many levels merged
         print(f"[WARNING] Number of unique grid levels ({len(unique_levels)}) is significantly less than expected ({num_grids + 1}). Price range might be too narrow or num_grids too high for the given precision.")


    return unique_levels # Already sorted descending

# --- Main Execution ---
if __name__ == "__main__":
    # --- Grid Strategy Parameters ---
    TARGET_SYMBOL = "CRVUSDT"
    UPPER_PRICE = 0.70     # Example upper bound
    LOWER_PRICE = 0.60     # Example lower bound
    NUM_GRIDS = 10         # Number of grid intervals (will create NUM_GRIDS+1 levels)
    ORDER_QTY_PER_GRID = 10 # Quantity for each buy/sell order (e.g., 10 CRV)

    CHECK_INTERVAL_SECONDS = 60 # How often to check and maintain the grid

    # --- Initial Setup ---
    print("--- Logarithmic Grid Strategy Initializing ---") # <-- Updated print
    print(f"Symbol: {TARGET_SYMBOL}")
    print(f"Grid Range: {LOWER_PRICE} - {UPPER_PRICE}")
    print(f"Number of Grids: {NUM_GRIDS}")
    print(f"Order Quantity per Grid: {ORDER_QTY_PER_GRID} {TARGET_SYMBOL.replace('USDT','')}")

    # --- Precision Settings Check (Load from exchange info is better) ---
    # It's highly recommended to fetch these dynamically using /fapi/v1/exchangeInfo
    # For now, using the hardcoded values for CRVUSDT
    print(f"Using Precisions: Price={PRICE_PRECISION}, Qty={QUANTITY_PRECISION}, Tick={TICK_SIZE}")
    # Ensure order quantity meets minimum requirements
    # minQty = decimal.Decimal('1') # Example for CRV
    # minNotional = decimal.Decimal('5') # Example for CRV
    # if ORDER_QTY_PER_GRID < minQty:
    #    print(f"[ERROR] ORDER_QTY_PER_GRID {ORDER_QTY_PER_GRID} is less than minQty {minQty}.")
    #    exit()
    # Check notional at lowest price
    # if (decimal.Decimal(str(ORDER_QTY_PER_GRID)) * decimal.Decimal(str(LOWER_PRICE))) < minNotional:
    #     print(f"[WARNING] Order notional value might be below minNotional at the lower price range.")

    # --- Calculate Grid Levels ---
    print("Calculating Logarithmic Grid Levels...") # <-- Updated print
    grid_levels = calculate_grid_levels(UPPER_PRICE, LOWER_PRICE, NUM_GRIDS)
    if not grid_levels:
        print("[ERROR] Failed to calculate grid levels. Check parameters.")
        exit()
    print("Calculated Grid Levels:", [float(lvl) for lvl in grid_levels]) # Print as float for readability
    print(f"Number of levels generated: {len(grid_levels)}")

    # --- Optional: Cancel existing orders before starting ---
    # cancel_all_open_orders(TARGET_SYMBOL)
    # time.sleep(2) # Give time for cancellation to process

    # --- Main Loop ---
    print("\n--- Starting Grid Maintenance Loop ---")
    while True:
        try:
            print(f"\n--- Checking Grid ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---")
            current_price = get_current_price(TARGET_SYMBOL)
            if current_price is None:
                print("Failed to get current price. Retrying next cycle.")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue
            print(f"Current Market Price: {current_price}")

            open_orders_list = get_open_orders(TARGET_SYMBOL)
            open_order_prices = {
                'BUY': set(),
                'SELL': set()
            }
            if open_orders_list: # Check if list is not None and not empty
                 for order in open_orders_list:
                     # Ensure price is treated as Decimal
                     try:
                         order_price = decimal.Decimal(order['price']).quantize(PRICE_PRECISION)
                         open_order_prices[order['side']].add(order_price)
                     except (KeyError, decimal.InvalidOperation) as e:
                         print(f"Warning: Could not parse price for order {order.get('orderId')}: {e}")

            print(f"Open Orders Found: BUYs at {sorted([float(p) for p in open_order_prices['BUY']])}, SELLs at {sorted([float(p) for p in open_order_prices['SELL']])}")

            # --- Grid Maintenance Logic ---
            placed_orders_this_cycle = 0
            for level_price in grid_levels:
                 # --- Buy Side Logic ---
                 if level_price < current_price:
                     # Should have a BUY order, unless it's already open
                     # Use quantize comparison to match formatted levels
                     level_quantized = level_price.quantize(PRICE_PRECISION)
                     if level_quantized not in open_order_prices['BUY']:
                         print(f"Missing BUY order at {level_quantized}. Placing...")
                         place_result = place_limit_order(TARGET_SYMBOL, 'BUY', decimal.Decimal(str(ORDER_QTY_PER_GRID)), level_quantized) # Use quantized price
                         if place_result and 'orderId' in place_result:
                              print(f"  Successfully placed BUY order {place_result['orderId']} at {level_quantized}")
                              placed_orders_this_cycle += 1
                              time.sleep(0.2) # Small delay between placements
                         else:
                              print(f"  Failed to place BUY order at {level_quantized}. Response: {place_result}")
                              # Consider adding retry logic or error tracking here

                 # --- Sell Side Logic ---
                 elif level_price > current_price:
                      # Should have a SELL order, unless it's already open
                      # Use quantize comparison to match formatted levels
                      level_quantized = level_price.quantize(PRICE_PRECISION)
                      if level_quantized not in open_order_prices['SELL']:
                           print(f"Missing SELL order at {level_quantized}. Placing...")
                           place_result = place_limit_order(TARGET_SYMBOL, 'SELL', decimal.Decimal(str(ORDER_QTY_PER_GRID)), level_quantized) # Use quantized price
                           if place_result and 'orderId' in place_result:
                                print(f"  Successfully placed SELL order {place_result['orderId']} at {level_quantized}")
                                placed_orders_this_cycle += 1
                                time.sleep(0.2) # Small delay between placements
                           else:
                                print(f"  Failed to place SELL order at {level_quantized}. Response: {place_result}")
                                # Consider adding retry logic or error tracking here

                 # --- Remove Orders On Wrong Side (Optional Cleanup) ---
                 # Check if there's a BUY order above current price that shouldn't be there
                 # Check if there's a SELL order below current price that shouldn't be there
                 # This requires iterating through open_orders_list again and canceling specific orderIds
                 # Basic version omits this for simplicity

            print(f"Grid check complete. Placed {placed_orders_this_cycle} new orders.")

        except Exception as e:
            print(f"An error occurred in the main loop: {e}")
            # Add more robust error handling / logging here if needed

        print(f"Waiting for {CHECK_INTERVAL_SECONDS} seconds until next check...")
        time.sleep(CHECK_INTERVAL_SECONDS) 