import os
import csv
import multiprocessing
import signal
import time
import sys
import subprocess
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import decimal # Added for usdt_amount validation

load_dotenv() # Load .env for Flask app specific settings like SECRET_KEY

app = Flask(__name__)
# WARNING: Change this to a strong, random secret key in production, ideally from .env
app.config['SECRET_KEY'] = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

# --- Configuration ---
STRATEGIES_DIR = os.path.join(os.path.dirname(__file__), 'strategies')
USER_DATA_FILE = os.path.join(os.path.dirname(__file__), 'data', 'user_api_keys.csv')
STRATEGY_SCRIPTS = {
    "normal_grid": "aster_normal_grid_strategy.py",
    "log_grid": "aster_log_grid_strategy.py",
    "volume": "aster_volume_strategy.py",
    # Add other strategies here by name
}

# --- Process Management (In-memory, simple approach) ---
# Stores { "wallet_address": (process_object, strategy_name, symbol) }
# WARNING: This state is lost if the Flask app restarts.
# A more robust solution would use a database or external process manager.
running_strategies = {}

# --- Helper Functions ---

def load_user_api_keys():
    """Loads API keys from CSV. Returns dict {wallet: {'api_key': k, 'secret_key': s}}"""
    keys = {}
    if not os.path.exists(USER_DATA_FILE):
        print(f"[Warning] User data file not found: {USER_DATA_FILE}")
        return keys
    try:
        with open(USER_DATA_FILE, mode='r', newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            if reader.fieldnames != ['wallet_address', 'api_key', 'secret_key']:
                print(f"[Warning] CSV header mismatch in {USER_DATA_FILE}. Expected ['wallet_address', 'api_key', 'secret_key']")
                # Attempt to read anyway, but might fail if structure is different
            for row in reader:
                # Basic validation
                if row.get('wallet_address') and row.get('api_key') and row.get('secret_key'):
                     # WARNING: Keys stored in plain text! VERY INSECURE.
                     keys[row['wallet_address']] = {
                         'api_key': row['api_key'],
                         'secret_key': row['secret_key']
                     }
                else:
                    # Avoid printing potentially sensitive partial data
                    print(f"[Warning] Skipping invalid/incomplete row in CSV.") 
    except FileNotFoundError:
         print(f"[Warning] User data file not found: {USER_DATA_FILE}")
    except Exception as e:
        print(f"Error loading user data from {USER_DATA_FILE}: {e}")
    return keys

def save_user_api_key(wallet_address, api_key, secret_key):
    """Adds or updates a user's API key in the CSV."""
    # WARNING: Insecure storage method. Consider encryption.
    users = load_user_api_keys()
    users[wallet_address] = {'api_key': api_key, 'secret_key': secret_key}
    try:
        # Ensure the directory exists before writing
        os.makedirs(os.path.dirname(USER_DATA_FILE), exist_ok=True)
        with open(USER_DATA_FILE, mode='w', newline='') as csvfile:
            fieldnames = ['wallet_address', 'api_key', 'secret_key']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for addr, keys in users.items():
                writer.writerow({
                    'wallet_address': addr,
                    'api_key': keys['api_key'],
                    'secret_key': keys['secret_key']
                })
        return True
    except Exception as e:
        print(f"Error saving user data to {USER_DATA_FILE}: {e}")
        return False

def get_strategy_script_path(strategy_name):
    """Gets the full path to a strategy script."""
    script_filename = STRATEGY_SCRIPTS.get(strategy_name)
    if not script_filename:
        print(f"[Error] Unknown strategy name: {strategy_name}")
        return None
    path = os.path.join(STRATEGIES_DIR, script_filename)
    if not os.path.exists(path):
        print(f"[Error] Strategy script file not found: {path}")
        return None
    return path

def start_strategy_process(wallet_address, strategy_name, symbol, usdt_amount, api_keys):
    """Starts a strategy script, passing only essential info + USDT amount."""
    script_path = get_strategy_script_path(strategy_name)
    if not script_path:
        return None, f"Strategy '{strategy_name}' is not configured or script file not found."

    # Prepare environment variables for the child process
    env = os.environ.copy()
    env["VAULT_API_KEY"] = api_keys['api_key']
    env["VAULT_SECRET_KEY"] = api_keys['secret_key']
    env["VAULT_SYMBOL"] = symbol
    # Pass the USDT amount
    env["VAULT_USDT_AMOUNT"] = str(usdt_amount)
    # --- DO NOT pass other detailed params like upper_price, num_grids etc. --- 
    # The strategy script will use its internal defaults and calculate based on USDT_AMOUNT.

    print(f"[DEBUG] Preparing env for {strategy_name}: VAULT_SYMBOL={symbol}, VAULT_USDT_AMOUNT={usdt_amount}")

    try:
        def run_script(script_path, env_vars):
             os.environ.update(env_vars)
             try:
                 import runpy
                 runpy.run_path(script_path, run_name='__main__')
             except Exception as e:
                 print(f"[ERROR] Exception in strategy process (PID {os.getpid()}) for {wallet_address}: {e}")
                 import traceback
                 traceback.print_exc()

        process = multiprocessing.Process(
            target=run_script,
            args=(script_path, env),
            daemon=True
        )
        process.start()
        print(f"Started strategy '{strategy_name}' for {wallet_address} on {symbol} (PID: {process.pid}) with USDT amount {usdt_amount}")
        return process, None
    except Exception as e:
        print(f"Failed to start strategy process for {wallet_address}: {e}")
        return None, f"Failed to start process: {e}"

def stop_strategy_process(wallet_address, cancel_orders=False):
    """Stops a running strategy process, optionally cancelling orders first."""
    if wallet_address not in running_strategies:
        return False, "No strategy running for this wallet address.", "na", "na"

    process, strategy_name, symbol = running_strategies[wallet_address]
    pid = process.pid
    termination_status = "unknown"
    cancel_status = "not_attempted"

    # --- Step 1: Cancel Orders (if requested) --- 
    if cancel_orders:
        print(f"Cancelling orders for {wallet_address} on {symbol}...")
        user_keys = load_user_api_keys().get(wallet_address)
        if not user_keys:
            print(f"[ERROR] Cannot cancel orders: API keys not found for {wallet_address}. Proceeding to stop process only.")
            cancel_status = "failed_no_keys"
        else:
            cancel_script_path = os.path.join(STRATEGIES_DIR, "cancel_orders_script.py")
            if not os.path.exists(cancel_script_path):
                 print(f"[ERROR] Cannot cancel orders: Script not found at {cancel_script_path}. Proceeding to stop process only.")
                 cancel_status = "failed_no_script"
            else:
                try:
                    # Run the cancellation script as a separate, short-lived process
                    # Pass keys and symbol as command-line arguments
                    cmd = [
                        sys.executable, # Use the same python interpreter
                        cancel_script_path,
                        user_keys['api_key'],
                        user_keys['secret_key'],
                        symbol
                    ]
                    print(f"[Subprocess] Running command: {' '.join(cmd[:2])} <api_key> <secret_key> {symbol}") # Hide keys in log
                    # Use timeout to prevent hanging, capture output for logging
                    result = subprocess.run(
                        cmd, 
                        capture_output=True, 
                        text=True, 
                        timeout=30 # Set a reasonable timeout (e.g., 30 seconds)
                    )
                    
                    print(f"[CancelScript Output STDOUT] for {wallet_address}:")
                    print(result.stdout)
                    if result.stderr:
                         print(f"[CancelScript Output STDERR] for {wallet_address}:")
                         print(result.stderr)

                    if result.returncode == 0:
                        print(f"Order cancellation script completed successfully for {wallet_address} on {symbol}.")
                        cancel_status = "success"
                    else:
                        print(f"[ERROR] Order cancellation script failed for {wallet_address} on {symbol} (Return Code: {result.returncode}).")
                        cancel_status = f"failed_script_error_{result.returncode}"
                        
                except subprocess.TimeoutExpired:
                     print(f"[ERROR] Order cancellation script timed out for {wallet_address} on {symbol}.")
                     cancel_status = "failed_timeout"
                except Exception as e:
                    print(f"[ERROR] Failed to execute order cancellation script for {wallet_address}: {e}")
                    cancel_status = "failed_execution_error"
        # --- End Order Cancellation --- 
    
    # --- Step 2: Terminate Strategy Process --- 
    process_terminated = False
    if pid is None:
        print(f"[Warning] Process object for {wallet_address} has no PID. Cannot terminate. Removing from tracking.")
        termination_status = "failed_no_pid"
    elif process.is_alive():
        print(f"Attempting to stop strategy process '{strategy_name}' for {wallet_address} (PID: {pid})... Sending SIGTERM.")
        try:
            os.kill(pid, signal.SIGTERM)
            process.join(timeout=10)
            if process.is_alive():
                print(f"Process {pid} did not terminate gracefully after SIGTERM. Sending SIGKILL.")
                os.kill(pid, signal.SIGKILL)
                process.join(timeout=2)
            
            if not process.is_alive():
                 print(f"Process {pid} terminated.")
                 process_terminated = True
                 termination_status = "success"
            else:
                 print(f"[ERROR] Failed to terminate process {pid} for {wallet_address} after SIGKILL.")
                 termination_status = "failed_unkillable"
                 # Removing from tracking anyway to avoid repeated attempts
        except ProcessLookupError:
            print(f"Process {pid} for {wallet_address} already exited (ProcessLookupError)." )
            process_terminated = True # It's already stopped
            termination_status = "already_exited"
        except Exception as e:
            print(f"Error stopping process {pid} for {wallet_address}: {e}")
            termination_status = f"failed_exception:_{e}"
            # Fall through to remove from tracking
    else:
        print(f"Process {pid} for {wallet_address} was already stopped or finished.")
        process_terminated = True # It's already stopped
        termination_status = "already_stopped"

    # --- Step 3: Remove from Tracking --- 
    if wallet_address in running_strategies:
        del running_strategies[wallet_address]
        print(f"Removed {wallet_address} from running strategies tracking.")
    
    # --- Step 4: Determine Overall Success and Message --- 
    final_success = False
    message_parts = []
    
    # Cancellation outcome
    if cancel_orders:
        if cancel_status == "success":
            message_parts.append("Order cancellation successful.")
            final_success = True # Consider success if cancel works & process stops
        elif cancel_status == "not_attempted":
             message_parts.append("Order cancellation not attempted.")
             # Don't set final_success based on this alone
        else:
             message_parts.append(f"Order cancellation failed ({cancel_status}).")
             final_success = False # If cancellation fails, overall withdraw fails
             
    # Process termination outcome
    if process_terminated:
         message_parts.append(f"Strategy process {pid if pid else 'N/A'} terminated or already stopped ({termination_status}).")
         if not cancel_orders: # If only stopping, termination success means overall success
             final_success = True 
         elif not final_success: # If cancelling orders failed, still report process stop
              pass # Keep final_success as False
    else:
         message_parts.append(f"Failed to confirm strategy process termination ({termination_status}).")
         final_success = False # If process termination fails, overall fails
         
    final_message = " ".join(message_parts)
    return final_success, final_message, cancel_status, termination_status

# --- API Endpoints ---

@app.route('/register', methods=['POST'])
def register_keys():
    """Registers or updates API keys for a wallet address."""
    data = request.get_json()
    if not data or 'wallet_address' not in data or 'api_key' not in data or 'secret_key' not in data:
        return jsonify({"error": "Missing wallet_address, api_key, or secret_key"}), 400

    wallet_address = data['wallet_address']
    api_key = data['api_key']
    secret_key = data['secret_key']

    # Basic validation (add more robust checks, e.g., length, format)
    if not isinstance(wallet_address, str) or len(wallet_address) < 10: 
         return jsonify({"error": "Invalid wallet_address format"}), 400
    if not isinstance(api_key, str) or len(api_key) < 10:
         return jsonify({"error": "Invalid api_key format"}), 400
    if not isinstance(secret_key, str) or len(secret_key) < 10:
         return jsonify({"error": "Invalid secret_key format"}), 400

    print(f"Registering/Updating keys for wallet: {wallet_address[:5]}...{wallet_address[-5:]}")
    # WARNING: Insecure key handling!
    success = save_user_api_key(wallet_address, api_key, secret_key)

    if success:
        return jsonify({"message": "API keys registered successfully."}), 201
    else:
        return jsonify({"error": "Failed to save API keys to storage."}), 500

@app.route('/start_strategy', methods=['POST'])
def start_strategy():
    """Starts a chosen strategy using internal defaults + user's USDT amount."""
    data = request.get_json()
    # Expecting wallet_address, strategy_name, symbol, usdt_amount
    required_fields = ['wallet_address', 'strategy_name', 'symbol', 'usdt_amount']
    if not data or not all(field in data for field in required_fields):
        return jsonify({"error": f"Missing one or more required fields: {required_fields}"}), 400

    wallet_address = data['wallet_address']
    strategy_name = data['strategy_name']
    symbol = data['symbol']
    usdt_amount_str = str(data['usdt_amount']) # Ensure it's a string for Decimal

    # Validate usdt_amount
    try:
        usdt_amount = decimal.Decimal(usdt_amount_str)
        if usdt_amount <= 0:
            raise ValueError("usdt_amount must be positive")
        # Add a reasonable upper limit check? e.g., 1,000,000 USDT?
        # if usdt_amount > 1000000: 
        #     raise ValueError("usdt_amount exceeds maximum limit")
    except (ValueError, decimal.InvalidOperation, TypeError) as e:
        return jsonify({"error": f"Invalid usdt_amount: {e}. Must be a positive number."}), 400

    if strategy_name not in STRATEGY_SCRIPTS:
        return jsonify({"error": f"Invalid strategy_name: '{strategy_name}'. Available: {list(STRATEGY_SCRIPTS.keys())}"}), 400

    # Check if already running
    if wallet_address in running_strategies:
         process, running_strat, running_sym = running_strategies[wallet_address]
         if process.is_alive(): return jsonify({"error": f"Strategy '{running_strat}' on '{running_sym}' already running."}), 409
         else: print(f"[Warning] Cleaning dead process for {wallet_address}."); del running_strategies[wallet_address]

    # Get API Keys
    user_keys = load_user_api_keys().get(wallet_address)
    if not user_keys: return jsonify({"error": "Wallet address not registered."}), 404

    # --- Parameter validation is now simpler, just checking usdt_amount (done above) --- 
    # No need to validate specific strategy params like upper_price etc.

    # Start the process using the modified function
    print(f"Attempting to start strategy '{strategy_name}' for {wallet_address} with {usdt_amount} USDT...")
    process, error_msg = start_strategy_process(wallet_address, strategy_name, symbol, usdt_amount, user_keys)

    if process:
        running_strategies[wallet_address] = (process, strategy_name, symbol)
        return jsonify({
            "message": f"Strategy '{strategy_name}' initiated successfully for {wallet_address} on {symbol} with {usdt_amount} USDT.",
            "status": "starting",
            "pid": process.pid
        }), 202 # Accepted
    else:
        return jsonify({"error": f"Failed to start strategy: {error_msg}"}), 500

@app.route('/stop_strategy', methods=['POST'])
def stop_strategy():
    """Stops the running strategy WITHOUT cancelling orders."""
    data = request.get_json()
    if not data or 'wallet_address' not in data:
        return jsonify({"error": "Missing wallet_address"}), 400

    wallet_address = data['wallet_address']
    print(f"Received stop request (NO order cancellation) for wallet: {wallet_address}")
    
    # Call stop_strategy_process with cancel_orders=False
    success, message, _, termination_status = stop_strategy_process(wallet_address, cancel_orders=False)

    if success:
        return jsonify({"message": message, "status": "stopped", "termination_status": termination_status}), 200
    else:
        status_code = 404 if "No strategy running" in message or "already stopped" in message or "already exited" in message else 500
        return jsonify({"error": message, "status": "error_stopping", "termination_status": termination_status}), status_code

@app.route('/withdraw', methods=['POST'])
def withdraw_strategy():
    """Stops the running strategy AND cancels all its open orders."""
    data = request.get_json()
    if not data or 'wallet_address' not in data:
        return jsonify({"error": "Missing wallet_address"}), 400

    wallet_address = data['wallet_address']
    print(f"Received withdraw request (cancel orders AND stop) for wallet: {wallet_address}")

    # Call stop_strategy_process with cancel_orders=True
    success, message, cancel_status, termination_status = stop_strategy_process(wallet_address, cancel_orders=True)

    if success:
        # Overall success means cancellation (if attempted) was okay AND process stopped
        return jsonify({
            "message": message, 
            "status": "stopped", 
            "cancellation_status": cancel_status, 
            "termination_status": termination_status
        }), 200
    else:
        # Determine appropriate status code based on failure reason
        status_code = 500 # Default to internal server error
        response_status = "error_stopping_or_cancelling"
        if "No strategy running" in message or "already stopped" in message or "already exited" in message:
            status_code = 404
            response_status = "stopped" # Already stopped
        elif "failed_no_keys" in cancel_status or "failed_no_script" in cancel_status:
             status_code = 400 # Bad request / configuration error
             response_status = "error_cancelling_setup"
        elif "failed_script_error" in cancel_status or "failed_execution_error" in cancel_status or "failed_timeout" in cancel_status:
             status_code = 500 # Internal error during cancellation
             response_status = "error_cancelling_execution"
        elif "failed_unkillable" in termination_status:
             status_code = 500 # Internal error during termination
             response_status = "error_terminating_process"
             
        return jsonify({
            "error": message, 
            "status": response_status, 
            "cancellation_status": cancel_status, 
            "termination_status": termination_status
        }), status_code

@app.route('/status/<wallet_address>', methods=['GET'])
def get_strategy_status(wallet_address):
    """Checks the status of the strategy for a given wallet address."""
    if wallet_address in running_strategies:
        process, strategy_name, symbol = running_strategies[wallet_address]
        pid = process.pid
        if process.is_alive():
            return jsonify({
                "status": "running",
                "strategy": strategy_name,
                "symbol": symbol,
                "pid": pid
            }), 200
        else:
            # Process is tracked but not alive - potential zombie or finished uncleanly
            print(f"Process for {wallet_address} (PID: {pid}) is tracked but not alive. Cleaning up tracking entry.")
            # Clean up the entry
            del running_strategies[wallet_address]
            return jsonify({
                "status": "stopped", 
                "message": f"Process (PID: {pid}) for strategy '{strategy_name}' was tracked but found dead/finished.",
                "strategy": strategy_name,
                "symbol": symbol
                }), 200 # Still return 200, but indicate stopped state
    else:
        return jsonify({"status": "stopped", "message": "No strategy actively tracked for this wallet address."}), 200 # Consistent 200 for stopped status

@app.route('/status', methods=['GET'])
def get_all_statuses():
    """Returns the status of all tracked strategies."""
    statuses = {}
    # Iterate over a copy of keys in case cleanup happens during iteration
    tracked_wallets = list(running_strategies.keys())
    
    for wallet_address in tracked_wallets:
        if wallet_address in running_strategies: # Check again in case deleted during loop
            process, strategy_name, symbol = running_strategies[wallet_address]
            pid = process.pid
            if process.is_alive():
                statuses[wallet_address] = {
                    "status": "running",
                    "strategy": strategy_name,
                    "symbol": symbol,
                    "pid": pid
                }
            else:
                # Clean up dead process entry
                print(f"Process for {wallet_address} (PID: {pid}) is tracked but not alive during full status check. Cleaning up.")
                del running_strategies[wallet_address]
                statuses[wallet_address] = {
                    "status": "stopped", 
                    "message": f"Process (PID: {pid}) found dead/finished.",
                    "strategy": strategy_name,
                    "symbol": symbol
                }
                
    # Add wallets from CSV that are not currently running
    all_registered_users = load_user_api_keys()
    for wallet_address in all_registered_users:
        if wallet_address not in statuses:
             statuses[wallet_address] = {"status": "stopped", "message": "Registered, no strategy running."}

    return jsonify(statuses), 200


if __name__ == '__main__':
    # Ensure data directory exists
    os.makedirs(os.path.dirname(USER_DATA_FILE), exist_ok=True)
    # Initialize CSV if it doesn't exist or is empty/invalid
    try:
        file_exists = os.path.exists(USER_DATA_FILE)
        is_empty = file_exists and os.path.getsize(USER_DATA_FILE) == 0
        header_ok = False
        if file_exists and not is_empty:
             with open(USER_DATA_FILE, mode='r', newline='') as f_check:
                  reader = csv.reader(f_check)
                  header = next(reader, None)
                  if header == ['wallet_address', 'api_key', 'secret_key']:
                      header_ok = True
        
        if not file_exists or is_empty or not header_ok:
            print(f"Initializing or correcting user data file: {USER_DATA_FILE}")
            with open(USER_DATA_FILE, mode='w', newline='') as f_init:
                writer = csv.writer(f_init)
                writer.writerow(['wallet_address', 'api_key', 'secret_key'])
            print(f"User data file ensured.")
            
    except Exception as e:
         print(f"[ERROR] Could not initialize user data file {USER_DATA_FILE}: {e}")
         print("Please ensure the directory is writable.")
         # Decide if the app should exit if storage is unavailable
         # exit(1)

    # Run Flask dev server (use a proper WSGI server like Gunicorn for production)
    # Use host='0.0.0.0' to make it accessible on the network
    # Use debug=True only for development, False for production
    flask_debug = os.getenv('FLASK_DEBUG', 'True').lower() in ('true', '1', 't')
    print(f"Starting Flask app (Debug Mode: {flask_debug})... Access it at http://localhost:5000 or http://<your-ip>:5000")
    app.run(debug=flask_debug, host='0.0.0.0', port=5000) 