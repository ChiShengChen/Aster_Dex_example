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
import sys

# load_dotenv() # REMOVE THIS - Load conditionally later

# --- Default Strategy Parameters (Internal) ---
DEFAULT_UPPER_PRICE = decimal.Decimal("0.7")
DEFAULT_LOWER_PRICE = decimal.Decimal("0.6")
DEFAULT_NUM_GRIDS = 5
# DEFAULT_ORDER_QTY_PER_GRID = decimal.Decimal("10") # Calculated
DEFAULT_CHECK_INTERVAL_SECONDS = 60
MIN_NOTIONAL_VALUE = decimal.Decimal("5") # Example minimum

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
    dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path=dotenv_path)
        print(f"[{RUN_MODE}] Loaded .env file: {dotenv_path}")
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

# --- Constants & Precision --- 
BASE_URL = "https://fapi.asterdex.com"
PRICE_PRECISION = decimal.Decimal('0.0001')
QUANTITY_PRECISION = decimal.Decimal('1')
TICK_SIZE = decimal.Decimal('0.0001')

# --- API Functions (Identical to normal grid) ---
def get_server_time():
    try:
        response = requests.get(f"{BASE_URL}/fapi/v1/time")
        response.raise_for_status()
        return response.json()['serverTime']
    except Exception as e:
        print(f"Error getting server time: {e}", file=sys.stderr)
        return None

def generate_signature(params_str):
    if not SECRET_KEY:
        print("[ERROR] No secret key.", file=sys.stderr)
        return None
    return hmac.new(SECRET_KEY.encode('utf-8'), params_str.encode('utf-8'), hashlib.sha256).hexdigest()

def make_signed_request(method, endpoint, params=None):
    if params is None:
        params = {}
    t = get_server_time()
    if t is None:
        return None
    pfs = params.copy()
    pfs['timestamp']=int(t)
    pfs['recvWindow']=5000
    qs = urllib.parse.urlencode(sorted(pfs.items()))
    sig = generate_signature(qs)
    if sig is None:
        return None
    fqs = f"{qs}&signature={sig}"
    url = f"{BASE_URL}{endpoint}?{fqs}"
    hdrs = {'X-MBX-APIKEY': API_KEY}
    if not API_KEY:
        print("[ERROR] No API key.", file=sys.stderr)
        return None
    try:
        if method.upper()=='GET':
            r=requests.get(url,headers=hdrs)
        elif method.upper()=='POST':
            r=requests.post(url,headers=hdrs)
        elif method.upper()=='DELETE':
            r=requests.delete(url,headers=hdrs)
        else:
            print(f"Unsupported method: {method}", file=sys.stderr)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"API Error {method} {endpoint}: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f" Resp: {e.response.text}", file=sys.stderr)
        return None

def get_current_price(symbol):
    endpoint = "/fapi/v1/ticker/price"
    params = {'symbol': symbol}
    try:
        r=requests.get(f"{BASE_URL}{endpoint}",params=params)
        r.raise_for_status()
        return decimal.Decimal(r.json()['price'])
    except Exception as e:
        print(f"Error getting price {symbol}: {e}", file=sys.stderr)
        return None

def place_limit_order(symbol, side, quantity, price):
    endpoint = "/fapi/v1/order"
    try:
        fp=price.quantize(PRICE_PRECISION, decimal.ROUND_DOWN)
        fq=quantity.quantize(QUANTITY_PRECISION, decimal.ROUND_DOWN)
    except Exception as e:
        print(f"[ERROR] Format Price/Qty: {e}", file=sys.stderr)
        return None
    params = {'symbol':symbol, 'side':side.upper(), 'type':'LIMIT', 'quantity':str(fq), 'price':str(fp), 'timeInForce':'GTC'}
    print(f"Placing LIMIT {side} {fq} {symbol.replace('USDT','')}@{fp}...")
    return make_signed_request('POST', endpoint, params)

def get_open_orders(symbol):
    endpoint = "/fapi/v1/openOrders"
    params = {'symbol': symbol}
    r = make_signed_request('GET', endpoint, params)
    return r if isinstance(r, list) else []

def cancel_order(symbol, order_id):
    endpoint = "/fapi/v1/order"
    params={'symbol':symbol,'orderId':str(order_id)}
    print(f"Cancelling order {order_id}...")
    return make_signed_request('DELETE', endpoint, params)

def cancel_all_open_orders(symbol):
    endpoint = "/fapi/v1/allOpenOrders"
    params = {'symbol': symbol}
    print(f"Cancelling ALL orders for {symbol}...")
    r = make_signed_request('DELETE', endpoint, params)
    if r and isinstance(r, dict) and r.get('code')==200:
        print(" Cancel success (code 200).")
        return True
    elif isinstance(r, list) and len(r)==0:
        print(" Cancel success (empty list or no orders).")
        return True
    else:
        print(f" Cancel failed: {r}", file=sys.stderr)
        return False

# --- Grid Calculation Logic (Logarithmic - Keep as before) ---
def calculate_grid_levels(upper_price, lower_price, num_grids):
    if not isinstance(upper_price, decimal.Decimal) or not isinstance(lower_price, decimal.Decimal): print("[ERROR] Prices must be Decimal.", file=sys.stderr); return []
    if upper_price <= lower_price or num_grids < 1: print(f"[ERROR] Invalid log grid params {upper_price}/{lower_price}/{num_grids}", file=sys.stderr); return []
    if lower_price <= 0: print("[ERROR] Lower price must be positive.", file=sys.stderr); return []
    try:
        ratio = (upper_price / lower_price) ** (decimal.Decimal(1) / decimal.Decimal(str(num_grids)))
        if ratio <= 1: print(f"[ERROR] Grid ratio <= 1 ({ratio}).", file=sys.stderr); return []
        lvls = []; cur = lower_price
        for i in range(num_grids + 1):
            lvls.append(cur)
            if i < num_grids:
                cur *= ratio
    except Exception as e: print(f"[ERROR] Log grid calc: {e}", file=sys.stderr); return []
    try:
        fmt_lvls = [lvl.quantize(PRICE_PRECISION, decimal.ROUND_DOWN) for lvl in lvls]
    except Exception as e:
        print(f"[ERROR] Log grid quantize: {e}", file=sys.stderr); return []
    unique = []; tol = PRICE_PRECISION / 2
    temp_sorted = sorted(list(set(fmt_lvls)), reverse=True)
    if temp_sorted:
        unique.append(temp_sorted[0])
        for i in range(1, len(temp_sorted)):
            if abs(temp_sorted[i] - temp_sorted[i-1]) > tol:
                 if temp_sorted[i] >= lower_price.quantize(PRICE_PRECISION, decimal.ROUND_DOWN) - tol:
                      unique.append(temp_sorted[i])
    if len(unique) < 2: print("[WARN] Too few unique log levels.", file=sys.stderr)
    elif len(unique) <= num_grids: print(f"[WARN] Log levels reduced {len(unique)} <= {num_grids}", file=sys.stderr)
    return sorted(unique, reverse=True)


# --- Main Execution ---
if __name__ == "__main__":
    
    # --- Use Internal Default Parameters --- 
    upper_p = DEFAULT_UPPER_PRICE
    lower_p = DEFAULT_LOWER_PRICE
    num_g = DEFAULT_NUM_GRIDS
    check_interval = DEFAULT_CHECK_INTERVAL_SECONDS
    # --------------------------------------
    
    # --- Calculate Order Quantity based on USDT_AMOUNT --- 
    # Use the same logic as normal grid (average price based)
    avg_price = (lower_p + upper_p) / 2
    if avg_price <= 0 or num_g <= 0:
        print(f"[ERROR] Cannot calculate order quantity: Invalid avg price ({avg_price}) or num grids ({num_g}).", file=sys.stderr)
        sys.exit(1)
    estimated_qty_per_grid = (USDT_AMOUNT / decimal.Decimal(num_g)) / avg_price
    calculated_order_qty = estimated_qty_per_grid.quantize(QUANTITY_PRECISION, decimal.ROUND_DOWN)
    print(f"[INFO] Based on USDT_AMOUNT={USDT_AMOUNT}, AvgPrice={avg_price:.4f}, NumGrids={num_g} -> Calculated QtyPerGrid={calculated_order_qty}")
    if calculated_order_qty <= 0:
         print(f"[ERROR] Calculated order quantity is zero or negative ({calculated_order_qty}).", file=sys.stderr)
         sys.exit(1)
    min_notional_check = calculated_order_qty * lower_p
    if min_notional_check < MIN_NOTIONAL_VALUE:
        print(f"[ERROR] Calculated order notional value ({min_notional_check:.4f} USDT at lowest price) below minimum ({MIN_NOTIONAL_VALUE} USDT).", file=sys.stderr)
        sys.exit(1)
    ORDER_QTY_PER_GRID = calculated_order_qty
    # -------------------------------------------
    
    # --- Initial Setup Output --- 
    print("--- Logarithmic Grid Strategy Initializing (from Vault) ---")
    print(f"Symbol: {TARGET_SYMBOL}")
    print(f"USDT Amount: {USDT_AMOUNT}")
    print(f"Using Default Grid Range: {lower_p} - {upper_p}")
    print(f"Using Default Number of Grids: {num_g}")
    print(f"==> Calculated Order Quantity per Grid: {ORDER_QTY_PER_GRID} {TARGET_SYMBOL.replace('USDT','')}")
    print(f"Using Precisions: Price={PRICE_PRECISION}, Qty={QUANTITY_PRECISION}, Tick={TICK_SIZE}")

    # --- Calculate Grid Levels --- 
    print("Calculating Logarithmic Grid Levels...")
    grid_levels = calculate_grid_levels(upper_p, lower_p, num_g)
    if not grid_levels:
        print("[ERROR] Failed to calculate grid levels. Exiting.", file=sys.stderr)
        sys.exit(1)
    print("Calculated Grid Levels:", [float(lvl) for lvl in grid_levels])
    print(f"Number of levels generated: {len(grid_levels)}")

    # --- Main Loop (Identical logic to normal grid) ---
    print("--- Starting Grid Maintenance Loop ---")
    while True:
        try:
            print(f"--- Checking Grid ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---")
            current_price = get_current_price(TARGET_SYMBOL)
            if current_price is None: print("Failed price..."); time.sleep(check_interval); continue
            if not isinstance(current_price, decimal.Decimal): print("Price not Dec..."); time.sleep(check_interval); continue
            
            # Calculate the number of decimal places from PRICE_PRECISION
            num_decimal_places = abs(PRICE_PRECISION.as_tuple().exponent)
            print(f"Price: {current_price:.{num_decimal_places}f}")
            
            open_orders = get_open_orders(TARGET_SYMBOL)
            open_buys = set(); open_sells = set()
            if isinstance(open_orders, list):
                 for o in open_orders:
                     try:
                         p = decimal.Decimal(o.get('price')).quantize(PRICE_PRECISION); s = o.get('side').upper()
                         if s == 'BUY': open_buys.add(p)
                         elif s == 'SELL': open_sells.add(p)
                     except Exception: pass 
            print(f"Open Orders: BUYs@{sorted([float(p) for p in open_buys])}, SELLs@{sorted([float(p) for p in open_sells])}")
            placed = 0
            for lvl in grid_levels:
                 if not isinstance(lvl, decimal.Decimal): continue
                 lq = lvl.quantize(PRICE_PRECISION)
                 if lq < current_price and lq not in open_buys:
                     print(f"Missing BUY @{lq}. Placing...")
                     res = place_limit_order(TARGET_SYMBOL, 'BUY', ORDER_QTY_PER_GRID, lq)
                     if res and isinstance(res, dict) and 'orderId' in res: print(f" Placed BUY {res['orderId']}@{lq}"); placed+=1; time.sleep(0.2)
                     else: print(f" Failed BUY @{lq}: {res}", file=sys.stderr)
                 elif lq > current_price and lq not in open_sells:
                      print(f"Missing SELL @{lq}. Placing...")
                      res = place_limit_order(TARGET_SYMBOL, 'SELL', ORDER_QTY_PER_GRID, lq)
                      if res and isinstance(res, dict) and 'orderId' in res: print(f" Placed SELL {res['orderId']}@{lq}"); placed+=1; time.sleep(0.2)
                      else: print(f" Failed SELL @{lq}: {res}", file=sys.stderr)
            print(f"Grid check complete. Placed {placed} new orders.")
        except KeyboardInterrupt: print("Stopping..."); break
        except Exception as e: print(f"Main loop error: {e}", file=sys.stderr); import traceback; traceback.print_exc()
        time.sleep(check_interval)
    print("Strategy finished.") 