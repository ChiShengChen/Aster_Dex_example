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

def place_market_order(symbol, side, quantity):
    """下市價單"""
    endpoint = "/fapi/v1/order"
    params = {
        'symbol': symbol,
        'side': side.upper(),
        'type': 'MARKET', # Changed from LIMIT
        'quantity': str(quantity), # Use quantity directly
    }
    # Market orders don't need price or timeInForce
    print(f"Attempting to place MARKET {side} order: {quantity} {symbol.replace('USDT', '')}")
    result = make_signed_request('POST', endpoint, params)
    return result

def get_order_status(symbol, order_id):
    """查詢指定訂單的狀態"""
    endpoint = "/fapi/v1/order"
    params = {
        'symbol': symbol,
        'orderId': str(order_id)
    }
    print(f"Polling status for order ID: {order_id}")
    result = make_signed_request('GET', endpoint, params)
    return result

# --- Removed place_limit_order --- 
# --- Removed cancel_order --- 
# --- Removed get_open_orders --- 
# --- Removed get_book_ticker --- 

# --- 主要執行部分 ---
if __name__ == "__main__":
    # Check if API keys are loaded properly
    if not API_KEY or not SECRET_KEY:
        print("[ERROR] API_KEY or SECRET_KEY not found in environment variables.")
        print("Please ensure you have a .env file with ASTER_API_KEY and ASTER_SECRET_KEY defined.")
        exit(1)

    # --- Strategy Parameters ---
    target_symbol = "CRVUSDT"
    order_quantity = 10      # Fixed quantity (e.g., 10 CRV, known to be > 5 USDT notional)
    iterations = 5
    delay_seconds = 1        # Delay between buy/sell cycles
    poll_interval_seconds = 0.5 # How often to check order status
    max_poll_attempts = 20   # Max times to check status before giving up (20 * 0.5s = 10s timeout)
    # -------------------------

    print(f"Starting Market Order Wash Trading Strategy for {target_symbol}...")
    print(f"Order Quantity per trade: {order_quantity} {target_symbol.replace('USDT','')}")
    print(f"Running for {iterations} iterations with {delay_seconds}s delay between cycles.")

    for i in range(iterations):
        print(f"\n--- Cycle {i+1}/{iterations} --- commencing --- ")
        buy_order_filled = False
        sell_order_filled = False
        buy_order_id = None
        sell_order_id = None

        # --- 1. Place Market Buy Order ---
        buy_place_result = place_market_order(target_symbol, 'BUY', order_quantity)
        
        if buy_place_result and 'orderId' in buy_place_result:
            buy_order_id = buy_place_result['orderId']
            print(f"Market BUY order placed successfully. ID: {buy_order_id}, Status: {buy_place_result.get('status')}")
            
            # --- 2. Poll for Buy Order Fill ---
            for attempt in range(max_poll_attempts):
                time.sleep(poll_interval_seconds)
                order_status_result = get_order_status(target_symbol, buy_order_id)
                
                if order_status_result:
                    status = order_status_result.get('status')
                    print(f"  Poll attempt {attempt+1}/{max_poll_attempts}: Buy Order {buy_order_id} status = {status}")
                    if status == 'FILLED':
                        print(f"BUY Order {buy_order_id} confirmed FILLED.")
                        buy_order_filled = True
                        break
                    elif status in ['CANCELED', 'EXPIRED', 'REJECTED']:
                        print(f"[ERROR] BUY Order {buy_order_id} failed or was canceled. Status: {status}")
                        break # Exit polling loop on failure
                else:
                    print(f"  Poll attempt {attempt+1}/{max_poll_attempts}: Failed to get status for Buy Order {buy_order_id}.")
                    # Continue polling in case of temporary network issue
            
            if not buy_order_filled:
                print(f"[ERROR] BUY Order {buy_order_id} did not fill after {max_poll_attempts} attempts. Skipping sell.")
                # Optional: Attempt to cancel the potentially stuck order here if needed
                # cancel_order(target_symbol, buy_order_id)
                continue # Skip to next iteration

        else:
            print(f"[ERROR] Failed to place Market BUY order. Response: {buy_place_result}")
            continue # Skip to next iteration

        # --- 3. Place Market Sell Order (only if buy filled) ---
        if buy_order_filled:
            sell_place_result = place_market_order(target_symbol, 'SELL', order_quantity)

            if sell_place_result and 'orderId' in sell_place_result:
                sell_order_id = sell_place_result['orderId']
                print(f"Market SELL order placed successfully. ID: {sell_order_id}, Status: {sell_place_result.get('status')}")

                # --- 4. Poll for Sell Order Fill ---
                for attempt in range(max_poll_attempts):
                    time.sleep(poll_interval_seconds)
                    order_status_result = get_order_status(target_symbol, sell_order_id)

                    if order_status_result:
                        status = order_status_result.get('status')
                        print(f"  Poll attempt {attempt+1}/{max_poll_attempts}: Sell Order {sell_order_id} status = {status}")
                        if status == 'FILLED':
                            print(f"SELL Order {sell_order_id} confirmed FILLED.")
                            sell_order_filled = True
                            break
                        elif status in ['CANCELED', 'EXPIRED', 'REJECTED']:
                            print(f"[ERROR] SELL Order {sell_order_id} failed or was canceled. Status: {status}")
                            break # Exit polling loop on failure
                    else:
                         print(f"  Poll attempt {attempt+1}/{max_poll_attempts}: Failed to get status for Sell Order {sell_order_id}.")
                         # Continue polling
                
                if not sell_order_filled:
                     print(f"[ERROR] SELL Order {sell_order_id} did not fill after {max_poll_attempts} attempts.")
                     # Optional: Attempt to cancel

            else:
                 print(f"[ERROR] Failed to place Market SELL order. Response: {sell_place_result}")

        # --- End of Cycle --- 
        print(f"--- Cycle {i+1}/{iterations} completed. Waiting {delay_seconds}s --- ")
        time.sleep(delay_seconds)

    print("\nStrategy finished.")

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