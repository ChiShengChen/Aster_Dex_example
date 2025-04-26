import requests
import hmac
import hashlib
import time
import urllib.parse
import decimal
import sys
import argparse # To parse command-line arguments

# --- Configuration ---
# Consistent with other strategy scripts
BASE_URL = "https://fapi.asterdex.com"

# --- API Interaction Functions (Self-contained) ---

def get_server_time():
    """Gets server time from API."""
    try:
        response = requests.get(f"{BASE_URL}/fapi/v1/time", timeout=5) # Added timeout
        response.raise_for_status()
        return response.json()['serverTime']
    except requests.exceptions.RequestException as e:
        print(f"[CancelScript ERROR] Error fetching server time: {e}", file=sys.stderr)
        return None
    except Exception as e:
         print(f"[CancelScript ERROR] Unexpected error getting server time: {e}", file=sys.stderr)
         return None

def generate_signature(secret_key, params_str):
    """Generates HMAC SHA256 signature using the provided secret key."""
    if not secret_key:
        print("[CancelScript ERROR] Secret key is missing for signature generation.", file=sys.stderr)
        return None
    try:
        return hmac.new(secret_key.encode('utf-8'), params_str.encode('utf-8'), hashlib.sha256).hexdigest()
    except Exception as e:
        print(f"[CancelScript ERROR] Error generating signature: {e}", file=sys.stderr)
        return None

def make_signed_request(api_key, secret_key, method, endpoint, params=None):
    """Makes a signed API request using provided keys."""
    if not api_key or not secret_key:
         print("[CancelScript ERROR] API Key or Secret Key missing for signed request.", file=sys.stderr)
         return None
         
    if params is None:
        params = {}

    api_server_time_ms = get_server_time()
    if api_server_time_ms is None:
        return None # Error already printed by get_server_time

    params_for_signing = params.copy()
    params_for_signing['timestamp'] = int(api_server_time_ms)
    params_for_signing['recvWindow'] = 5000

    query_string_to_sign = urllib.parse.urlencode(sorted(params_for_signing.items()))
    
    signature = generate_signature(secret_key, query_string_to_sign)
    if signature is None:
        return None # Error already printed

    final_query_string = f"{query_string_to_sign}&signature={signature}"
    full_url = f"{BASE_URL}{endpoint}?{final_query_string}"
    
    headers = {'X-MBX-APIKEY': api_key}

    try:
        # Only DELETE is needed for cancel_all_open_orders
        if method.upper() == 'DELETE':
            response = requests.delete(full_url, headers=headers, timeout=10) # Added timeout
        else:
            print(f"[CancelScript ERROR] Unsupported method: {method}", file=sys.stderr)
            return None

        # Check for specific API error codes indicating failure vs success
        if response.status_code >= 400:
             print(f"[CancelScript WARN] API returned error status {response.status_code} for {method} {endpoint}.", file=sys.stderr)
             print(f"  Response: {response.text}", file=sys.stderr)
             # Even with error status, raise_for_status might not be needed if we check content
             # Let's return the JSON content if possible, or None otherwise
             try:
                 return response.json() 
             except requests.exceptions.JSONDecodeError:
                 return {"error": True, "status_code": response.status_code, "text": response.text} # Return error dict

        response.raise_for_status() # Check non-4xx/5xx HTTP issues
        return response.json()
        
    except requests.exceptions.RequestException as e:
        print(f"[CancelScript ERROR] API Request Error ({method} {endpoint}): {e}", file=sys.stderr)
        if e.response is not None:
            print(f"  Response Status: {e.response.status_code}, Body: {e.response.text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[CancelScript ERROR] Unexpected error during signed request: {e}", file=sys.stderr)
        return None

def cancel_all_open_orders(api_key, secret_key, symbol):
    """Cancels all open orders for a symbol using provided keys."""
    endpoint = "/fapi/v1/allOpenOrders"
    params = {'symbol': symbol}
    print(f"[CancelScript INFO] Attempting to cancel ALL open orders for {symbol}...")
    result = make_signed_request(api_key, secret_key, 'DELETE', endpoint, params)

    # Check result carefully based on expected AsterDex API responses
    if result is None:
        print(f"[CancelScript ERROR] Failed to send cancel request for {symbol}.", file=sys.stderr)
        return False
        
    # Example success checks (modify based on actual AsterDex responses):
    # Case 1: Successful cancellation returns a specific success code
    if isinstance(result, dict) and result.get('code') == 200:
        print(f"[CancelScript INFO] Successfully requested cancellation of all open orders for {symbol}.")
        return True
    # Case 2: Successful cancellation returns an empty list (if no orders existed or they were cancelled)
    elif isinstance(result, list) and len(result) == 0:
         print(f"[CancelScript INFO] Successfully cancelled all open orders for {symbol} (or no orders found).")
         return True
    # Case 3: Maybe success is just a 2xx status code with some other body
    # Add more checks if needed based on observing the API behavior
    
    # If none of the success conditions match, assume failure
    print(f"[CancelScript ERROR] Failed to cancel all open orders for {symbol}. Response: {result}", file=sys.stderr)
    return False

# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cancel all open orders for a specific symbol on AsterDex.")
    parser.add_argument("api_key", help="Your AsterDex API Key")
    parser.add_argument("secret_key", help="Your AsterDex Secret Key")
    parser.add_argument("symbol", help="The trading symbol (e.g., CRVUSDT)")

    # Check if running with arguments (e.g., from subprocess)
    if len(sys.argv) > 1:
        try:
            args = parser.parse_args()
            
            print(f"[CancelScript INFO] Received request to cancel orders for symbol: {args.symbol}")
            
            # Execute cancellation
            success = cancel_all_open_orders(args.api_key, args.secret_key, args.symbol)
            
            if success:
                print(f"[CancelScript INFO] Cancellation process completed successfully for {args.symbol}.")
                sys.exit(0) # Exit with success code 0
            else:
                print(f"[CancelScript ERROR] Cancellation process failed for {args.symbol}.", file=sys.stderr)
                sys.exit(1) # Exit with error code 1
                
        except SystemExit as e:
             # Allow SystemExit to propagate (e.g., from argparse help or sys.exit)
             sys.exit(e.code)
        except Exception as e:
             print(f"[CancelScript CRITICAL] An unexpected error occurred: {e}", file=sys.stderr)
             import traceback
             traceback.print_exc()
             sys.exit(2) # Exit with a different error code
    else:
        # If run directly without arguments, print help
        parser.print_help()
        sys.exit(0) 