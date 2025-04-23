import requests
import hmac
import hashlib
import time
import urllib.parse
import decimal # For precise quantity calculation
import os
from dotenv import load_dotenv

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
else:
    # --- TEMPORARY DEBUG PRINT --- #
    print("[TEMP DEBUG] Loaded Keys:")
    print(f"  API_KEY:    {API_KEY[:5]}...{API_KEY[-5:]}") # Print first/last 5 chars
    print(f"  SECRET_KEY: {SECRET_KEY[:5]}...{SECRET_KEY[-5:]}") # Print first/last 5 chars
    # --- REMOVE AFTER CHECKING --- #
# ------------------------------------------

BASE_URL = "https://fapi.asterdex.com"
# Define precision for CRVUSDT - based on /fapi/v1/exchangeInfo
# Price precision (decimal places for price)
PRICE_PRECISION = decimal.Decimal('0.001') # 4 decimal places
# Quantity precision (decimal places for quantity)
QUANTITY_PRECISION = decimal.Decimal('1') # 0 decimal places (integer)
TICK_SIZE = decimal.Decimal('0.001') # Smallest price change (matches price precision)

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
    """發送簽名的 API 請求"""
    if params is None:
        params = {}

    # --- Timestamp Check --- 
    api_server_time_ms = get_server_time() 
    if api_server_time_ms is None:
        print("[ERROR] Could not get server time from API!")
        return None # 無法獲取伺服器時間
    
    local_time_ms = int(time.time() * 1000)
    time_diff_ms = local_time_ms - api_server_time_ms

    print(f"[DEBUG] Timestamp Check:")
    print(f"  Local System Time (ms): {local_time_ms}")
    print(f"  API Server Time (ms):   {api_server_time_ms}")
    print(f"  Difference (Local - API): {time_diff_ms} ms")

    # Check if the difference is too large (e.g., more than a few seconds)
    max_allowed_diff_ms = 5000 # Allow 5 seconds difference, adjust as needed
    if abs(time_diff_ms) > max_allowed_diff_ms:
        print(f"[WARNING] Significant time difference detected ({time_diff_ms} ms)! Check system clock synchronization.")
        # You might choose to exit or use API time strictly here, depending on policy
    # ----------------------- 

    # Use API server time for the request timestamp for consistency
    # NOTE: The original `params` dict only contains the non-signature parameters at this point
    params_for_signing = params.copy() # Create a copy for signing
    params_for_signing['timestamp'] = int(api_server_time_ms)
    params_for_signing['recvWindow'] = 5000 # 設置請求有效時間窗口 (毫秒)

    # --- 生成待簽名字符串 (基於原始參數，按字母排序 - 正確方式) ---
    query_string_to_sign = urllib.parse.urlencode(sorted(params_for_signing.items()))
    # --- END --- 

    print(f"[DEBUG] String to sign: {query_string_to_sign}")

    # 生成簽名
    signature = generate_signature(query_string_to_sign)
    print(f"[DEBUG] Generated signature: {signature}")
    # params['signature'] = signature # 將簽名加入參數 (REMOVED - DO NOT ADD TO DICT)

    # --- 構建最終請求 URL 或 Body --- 
    # 根據 API 文件示例 1，所有參數 (包括簽名) 都放在 query string 中
    final_query_string = f"{query_string_to_sign}&signature={signature}"
    full_url = f"{BASE_URL}{endpoint}?{final_query_string}"
    print(f"[DEBUG] Full URL with signature: {full_url}") # Debug the final URL
    # -----------------------------------

    headers = {
        'X-MBX-APIKEY': API_KEY
    }
    # url = f"{BASE_URL}{endpoint}" (REMOVED - Use full_url)

    try:
        # Important: Pass the manually constructed full_url
        # Do NOT use the 'params' argument in requests for signed requests now
        if method.upper() == 'GET':
            print(f"[DEBUG] Sending GET request to: {full_url}")
            response = requests.get(full_url, headers=headers)
        elif method.upper() == 'POST':
            print(f"[DEBUG] Sending POST request to: {full_url}")
            # POST request body should be empty as all params are in query string per Example 1
            response = requests.post(full_url, headers=headers)
        elif method.upper() == 'DELETE':
            print(f"[DEBUG] Sending DELETE request to: {full_url}")
            response = requests.delete(full_url, headers=headers)
        else:
            print(f"Unsupported method: {method}")
            return None

        response.raise_for_status() # 檢查 HTTP 狀態碼
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"API Request Error ({method} {full_url}): {e}")
        if e.response is not None:
            print(f"Response Body: {e.response.text}")
        return None

def get_book_ticker(symbol):
    """獲取指定交易對的最佳買賣價"""
    endpoint = "/fapi/v1/ticker/bookTicker"
    params = {'symbol': symbol}
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params)
        response.raise_for_status()
        data = response.json()
        # Return prices as Decimals for precision
        return {
            'bidPrice': decimal.Decimal(data['bidPrice']),
            'askPrice': decimal.Decimal(data['askPrice'])
        }
    except requests.exceptions.RequestException as e:
        print(f"Error fetching book ticker for {symbol}: {e}")
        return None
    except KeyError as e:
        print(f"Error parsing book ticker response: Missing key {e}")
        return None

# --- 刷量策略的核心邏輯 ---

def place_limit_order(symbol, side, usdt_amount, price_offset_ticks=0, base_ticker=None):
    """
    根據 USDT 金額下限價單，可選擇價格偏移。
    Args:
        symbol (str): 交易對
        side (str): 'BUY' or 'SELL'
        usdt_amount (float/Decimal): 目標 USDT 金額
        price_offset_ticks (int): 從 best bid/ask 偏移的 tick 數量。
                                  正數表示對 BUY 更高的價格，對 SELL 更低的價格。
                                  (通常做市策略用正數，掛單在 spread 內側)
        base_ticker (dict): 可選的預先獲取的 ticker 信息，避免重複請求。
    """
    ticker = base_ticker if base_ticker else get_book_ticker(symbol)
    if not ticker:
        print("Could not get ticker, skipping order placement.")
        return None

    bid_price = ticker['bidPrice']
    ask_price = ticker['askPrice']

    # Calculate base price and apply offset
    if side.upper() == 'BUY':
        # Buy order: base price is best bid, offset increases price
        target_price = bid_price + price_offset_ticks * TICK_SIZE
        # Safety check: Adjusted buy price should not be >= ask price
        if target_price >= ask_price:
             print(f"Warning: Adjusted BUY price ({target_price}) >= ask price ({ask_price}). Placing at best bid ({bid_price}) instead.")
             target_price = bid_price
    elif side.upper() == 'SELL':
        # Sell order: base price is best ask, offset decreases price (positive offset moves towards mid-price)
        target_price = ask_price - price_offset_ticks * TICK_SIZE
        # Safety check: Adjusted sell price should not be <= bid price
        if target_price <= bid_price:
            print(f"Warning: Adjusted SELL price ({target_price}) <= bid price ({bid_price}). Placing at best ask ({ask_price}) instead.")
            target_price = ask_price
    else:
        print(f"Invalid side: {side}")
        return None

    # Calculate quantity based on USDT amount and the *final* target price
    if target_price <= 0:
        print(f"Invalid target price ({target_price}) for calculation, skipping order.")
        return None

    # Use Decimal for calculation
    dec_usdt_amount = decimal.Decimal(str(usdt_amount))
    quantity_unrounded = dec_usdt_amount / target_price

    # Round quantity and price to the required precision
    quantity = quantity_unrounded.quantize(QUANTITY_PRECISION, rounding=decimal.ROUND_DOWN)
    formatted_price = target_price.quantize(PRICE_PRECISION, rounding=decimal.ROUND_HALF_UP) # Use HALF_UP for price maybe?

    # Ensure minimum order size check
    if quantity <= 0:
        print(f"Calculated quantity ({quantity}) is too small for {usdt_amount} USDT at price {formatted_price}, skipping order.")
        return None

    endpoint = "/fapi/v1/order"
    params = {
        'symbol': symbol,
        'side': side.upper(),
        'type': 'LIMIT',
        'quantity': str(quantity),           # 數量需為字串
        'price': str(formatted_price),       # 價格需為字串
        'timeInForce': 'GTC'                 # Good Till Cancel
    }
    print(f"Attempting to place {side} order: {quantity} {symbol.replace('USDT', '')} at {formatted_price} USDT (Offset: {price_offset_ticks} ticks, Value: ~{usdt_amount} USDT)")
    result = make_signed_request('POST', endpoint, params)
    return result

def cancel_order(symbol, order_id):
    """取消指定訂單"""
    endpoint = "/fapi/v1/order"
    params = {
        'symbol': symbol,
        'orderId': str(order_id) # 訂單 ID 需為字串
    }
    print(f"Attempting to cancel order ID: {order_id} for {symbol}")
    result = make_signed_request('DELETE', endpoint, params)
    return result

def get_open_orders(symbol=None):
    """獲取當前掛單 (可選按 symbol 過濾)"""
    endpoint = "/fapi/v1/openOrders"
    params = {}
    if symbol:
        params['symbol'] = symbol
    print(f"Fetching open orders{f' for {symbol}' if symbol else ''}...")
    result = make_signed_request('GET', endpoint, params)
    return result

# --- 測試下單接口 --- (Removed test function)

# --- 主要執行部分 ---
if __name__ == "__main__":
    # Check if API keys are loaded properly (moved check inside main)
    if not API_KEY or not SECRET_KEY:
        print("[ERROR] API_KEY or SECRET_KEY not found in environment variables.")
        print("Please ensure you have a .env file with ASTER_API_KEY and ASTER_SECRET_KEY defined.")
        exit(1) # Exit the script if keys are missing
    # else block removed as the check inside main handles the 'else' logic

    # --- Strategy Parameters --- 
    target_symbol = "CRVUSDT" # Changed from BTCUSDT
    target_usdt_value = 7      # Changed from 5 to 7 to ensure notional > 5 after rounding
    price_offset_ticks = 1     # Keep offset at 1 tick
    iterations = 5
    delay_seconds = 2
    monitor_orders = True
    # -------------------------

    print(f"Starting enhanced order placement and cancellation strategy for {target_symbol}...")
    print(f"Target USDT value per order: {target_usdt_value}")
    print(f"!!! WARNING: Target value {target_usdt_value} USDT is likely BELOW the minimum required order value (typically 5 USDT or more). Orders might fail due to MIN_NOTIONAL filter. !!!")
    print(f"Price offset: {price_offset_ticks} ticks ({price_offset_ticks * TICK_SIZE} USDT)")
    print(f"Running for {iterations} iterations with {delay_seconds}s delay.")

    for i in range(iterations):
        print(f"--- Iteration {i+1}/{iterations} ---")

        # --- Get Ticker Info --- (Fetch once per iteration)
        current_ticker = get_book_ticker(target_symbol)
        if not current_ticker:
            print("Failed to get ticker info, skipping iteration.")
            time.sleep(delay_seconds)
            continue
        print(f"Current Ticker: Bid={current_ticker['bidPrice']}, Ask={current_ticker['askPrice']}")

        buy_order_id = None
        sell_order_id = None

        # --- 1. Place Limit Buy Order (slightly above bid) ---
        buy_place_result = place_limit_order(
            symbol=target_symbol,
            side='BUY',
            usdt_amount=target_usdt_value,
            price_offset_ticks=price_offset_ticks,
            base_ticker=current_ticker # Pass ticker to avoid re-fetching
        )
        if buy_place_result and 'orderId' in buy_place_result:
            print(f"BUY Placement Response: {buy_place_result}")
            if buy_place_result.get('status') in ['NEW', 'PARTIALLY_FILLED']:
                buy_order_id = buy_place_result['orderId']
                print(f"BUY Order placed successfully (ID: {buy_order_id}).")
            else:
                 print(f"BUY Order placed but status is '{buy_place_result.get('status')}'. Order ID: {buy_place_result['orderId']}")
        elif buy_place_result:
             print(f"BUY Placement failed: {buy_place_result}")
        else:
            print("BUY Placement request failed (Network/Signature issue?).")

        # --- 2. Place Limit Sell Order (slightly below ask) ---
        sell_place_result = place_limit_order(
            symbol=target_symbol,
            side='SELL',
            usdt_amount=target_usdt_value,
            price_offset_ticks=price_offset_ticks,
            base_ticker=current_ticker # Pass ticker to avoid re-fetching
        )
        if sell_place_result and 'orderId' in sell_place_result:
            print(f"SELL Placement Response: {sell_place_result}")
            if sell_place_result.get('status') in ['NEW', 'PARTIALLY_FILLED']:
                sell_order_id = sell_place_result['orderId']
                print(f"SELL Order placed successfully (ID: {sell_order_id}).")
            else:
                print(f"SELL Order placed but status is '{sell_place_result.get('status')}'. Order ID: {sell_place_result['orderId']}")
        elif sell_place_result:
             print(f"SELL Placement failed: {sell_place_result}")
        else:
            print("SELL Placement request failed (Network/Signature issue?).")


        # --- 3. (Optional) Monitor Open Orders --- (Before Cancellation)
        if monitor_orders:
            open_orders = get_open_orders(target_symbol)
            if open_orders is not None:
                print(f"\nCurrent Open Orders for {target_symbol}:")
                if open_orders:
                    for order in open_orders:
                        print(f"  - ID: {order['orderId']}, Side: {order['side']}, Price: {order['price']}, Qty: {order['origQty']}, Status: {order['status']}")
                else:
                    print("  (None)")
            else:
                print("Failed to fetch open orders.")
            print("") # Newline for separation

        # Give exchange a moment
        time.sleep(0.5)

        # --- 4. Cancel Placed Orders ---
        if buy_order_id:
            print(f"Attempting to cancel BUY order ID: {buy_order_id}")
            cancel_buy_result = cancel_order(target_symbol, buy_order_id)
            if cancel_buy_result:
                print(f"BUY Cancellation Response: {cancel_buy_result}")
            else:
                print(f"BUY Cancellation request failed for ID: {buy_order_id}")

        if sell_order_id:
            print(f"Attempting to cancel SELL order ID: {sell_order_id}")
            cancel_sell_result = cancel_order(target_symbol, sell_order_id)
            if cancel_sell_result:
                print(f"SELL Cancellation Response: {cancel_sell_result}")
            else:
                print(f"SELL Cancellation request failed for ID: {sell_order_id}")

        # --- End Iteration --- 
        print(f"--- End Iteration {i+1}/{iterations} ---")
        time.sleep(delay_seconds)

    print("Strategy finished.")

    # --- 刷量策略的其他步驟 --- (Now more integrated)
    # 1. 獲取當前市場價格 - Done via get_book_ticker
    # 2. 根據市價計算買賣價格 (e.g., 略高於買一價，略低於賣一價) - Done in place_limit_order with offset
    # 3. 下買單 - Implemented
    # 4. 下賣單 - Implemented
    # 5. (可選) 監控訂單狀態 - Implemented get_open_orders and optional call
    # 6. (可選) 如果訂單未成交或市場變動，取消訂單 - Implemented immediate cancel_order, advanced logic can be added
    # 7. 控制頻率，避免觸發 API 頻率限制 - Added delay_seconds
    # 8. 處理錯誤和異常 - Basic error handling improved slightly
    # --------------------------- 