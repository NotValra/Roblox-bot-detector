import requests
import time
import json
import io
import signal
import itertools
import random
from collections import deque 
from PIL import Image, UnidentifiedImageError
import imagehash 
from concurrent.futures import ThreadPoolExecutor, as_completed 
import traceback 

# --- Configuration ---

# IF YOU WANT TO STOP THE SCRIPT EARLY, PRESS CTRL+C
PLACE_ID = input("Place id here: ") 
ROBLOSECURITY_COOKIE = "YOUR_COOKIE_HERE" # <--- PUT YOUR .ROBLOSECURITY COOKIE HERE

# *** Choose Hashing Algorithm ***
# Options: 'phash', 'dhash', 'ahash', 'whash', 'colorhash'
# dhash is the best one in my opinion, but you can try others
HASH_ALGORITHM = 'dhash'
SIMILARITY_DIFFERENCE_THRESHOLD = 10 # Adjust this to tune sensitivity of how close the hashes / thumbnails has to be need to be

MAX_IMAGE_DOWNLOAD_WORKERS = 30  
MAX_THUMBNAIL_BATCH_SIZE = 250   

MAX_RETRIES = 3 # Max retries for failed requests
RETRY_DELAY_BASE = 1.0  # Retry delay if it fails
JITTER_MAX = 0      # Jitter, use it if u want.
THUMBNAIL_BATCH_SIZE = MAX_THUMBNAIL_BATCH_SIZE
DEFAULT_429_NO_HEADER_WAIT = 30  
REQUEST_TIMEOUT = 15  

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'
BASE_URL_GAMES = "https://games.roblox.com/v1/games"
BASE_URL_THUMBNAILS = "https://thumbnails.roblox.com/v1"

rate_limit_active = False
rate_limit_end_time = 0
stop_requested = False
session = requests.Session()
total_thumbnails_processed = 0
global_unique_bot_hashes = set()
global_total_bot_instance_count = 0

def signal_handler(sig, frame):
    global stop_requested
    if not stop_requested:
        print("\nCtrl+C detected! Attempting graceful shutdown... Finishing current operations might take a moment.")
        stop_requested = True
signal.signal(signal.SIGINT, signal_handler)

def wait_for_rate_limit():
    global rate_limit_active, rate_limit_end_time
    if rate_limit_active:
        wait_time = rate_limit_end_time - time.monotonic()
        if wait_time > 0:
            print(f"[RateLimit] API limited (429). Waiting {wait_time:.2f}s...")
            wait_interval = 1.0
            while wait_time > 0 and not stop_requested:
                 time.sleep(min(wait_interval, wait_time))
                 wait_time -= wait_interval
            if stop_requested:
                print("[RateLimit] Stop requested during wait.")
                return
        rate_limit_active = False

def handle_rate_limit_response(response_headers, url_limited):
    global rate_limit_active, rate_limit_end_time
    short_url = url_limited.split('?')[0]
    print(f"WARN: Received 429 Too Many Requests from API: {short_url}")
    retry_after_header = response_headers.get('Retry-After')
    delay = DEFAULT_429_NO_HEADER_WAIT
    if retry_after_header:
        try:
            delay_seconds = int(retry_after_header)
            if delay_seconds > 0: delay = delay_seconds
            else: print(f"[RateLimit] API provided non-positive Retry-After ('{retry_after_header}') for {short_url}. Using default wait ({DEFAULT_429_NO_HEADER_WAIT}s).")
        except Exception as e: print(f"[RateLimit] Error processing Retry-After header ('{retry_after_header}') for {short_url}: {e}. Using default wait ({DEFAULT_429_NO_HEADER_WAIT}s).")
    else: print(f"[RateLimit] No Retry-After header found for {short_url}. Using default wait of {DEFAULT_429_NO_HEADER_WAIT}s.")
    sleep_duration = max(0, delay) + random.uniform(0, JITTER_MAX)
    print(f"[RateLimit] API limit hit ({short_url}). Waiting {sleep_duration:.2f}s (Includes jitter)...")
    rate_limit_active = True
    rate_limit_end_time = time.monotonic() + sleep_duration


def fetch_with_retry(url, method="GET", is_image_download=False, **kwargs):
    global stop_requested
    retries = 0
    last_exception = None
    response = None
    while retries <= MAX_RETRIES:
        if stop_requested:
            if not is_image_download: print(f"INFO: Stopping request to {url.split('?')[0]} due to user interrupt.")
            return None
        wait_for_rate_limit()
        if stop_requested:
             if not is_image_download: print(f"INFO: Stopping request to {url.split('?')[0]} due to user interrupt after rate limit wait.")
             return None
        try:
            kwargs.setdefault('timeout', REQUEST_TIMEOUT)
            fetch_start_time = time.monotonic()
            response = session.request(method, url, headers=session.headers, **kwargs)
            fetch_duration = time.monotonic() - fetch_start_time

            if response.status_code == 429:
                 handle_rate_limit_response(response.headers, url)
                 last_exception = requests.exceptions.RequestException("Received 429")
                 response = None 
                 continue 

            response.raise_for_status() 

            if is_image_download:
                response._fetch_duration = fetch_duration 

            return response 

        except requests.exceptions.Timeout as e: last_exception = e; log_prefix = "WARN" if retries < MAX_RETRIES else "ERROR"; print(f"{log_prefix}: Request timed out ({url.split('?')[0]}...). Retrying ({retries+1}/{MAX_RETRIES})...")
        except requests.exceptions.ConnectionError as e: last_exception = e; log_prefix = "WARN" if retries < MAX_RETRIES else "ERROR"; print(f"{log_prefix}: Connection error ({url.split('?')[0]}...): {e}. Check network. Retrying ({retries+1}/{MAX_RETRIES})...")
        except requests.exceptions.ChunkedEncodingError as e: last_exception = e; log_prefix = "WARN" if retries < MAX_RETRIES else "ERROR"; print(f"{log_prefix}: Incomplete read/ChunkedEncodingError ({url.split('?')[0]}...): {e}. Retrying ({retries+1}/{MAX_RETRIES})...")
        except requests.exceptions.RequestException as e:
            last_exception = e
            status_code = getattr(response, 'status_code', 'N/A')
            if status_code == 401 or status_code == 403: print(f"CRITICAL ERROR: Received {status_code} from {url.split('?')[0]}. Your .ROBLOSECURITY cookie is likely invalid or expired. Stopping scan."); stop_requested = True; return None
            log_prefix = "WARN" if retries < MAX_RETRIES else "ERROR"
            if not is_image_download or log_prefix == "ERROR":
                 print(f"{log_prefix}: Request failed for {url.split('?')[0]}... Status={status_code}. Error: {e}. Retrying ({retries+1}/{MAX_RETRIES})...")
            response = None 
        except Exception as e: last_exception = e; log_prefix = "WARN" if retries < MAX_RETRIES else "ERROR"; print(f"{log_prefix}: An unexpected error occurred during fetch ({url.split('?')[0]}...): {e}. Retrying ({retries+1}/{MAX_RETRIES})..."); response = None # Clear response after error

        retries += 1
        if retries <= MAX_RETRIES and not stop_requested:
            delay = RETRY_DELAY_BASE * (2 ** (retries - 1)) + random.uniform(0, JITTER_MAX)
            sleep_end_time = time.monotonic() + delay
            while time.monotonic() < sleep_end_time and not stop_requested: time.sleep(0.1) 
            if stop_requested:
                if not is_image_download: print(f"INFO: Stop requested during retry delay for {url.split('?')[0]}.")
                return None

    if not is_image_download:
        is_auth_error_during_stop = stop_requested and isinstance(last_exception, requests.exceptions.RequestException) and getattr(last_exception.response, 'status_code', 0) in [401, 403]
        if not is_auth_error_during_stop:
             print(f"ERROR: Request ultimately failed after {MAX_RETRIES+1} attempts for {url.split('?')[0]}. Last exception: {last_exception}")

    return None 


# --- Helper function for concurrent image processing ---
def _fetch_and_hash_image(img_url, server_id):
    """Fetches, processes, and hashes an image, returning timings."""
    global stop_requested, HASH_ALGORITHM
    if stop_requested: return None

    fetch_time = 0.0
    load_time = 0.0
    hash_time = 0.0
    img_hash = None
    img = None 
    img_data = None 

    try:
        img_response = fetch_with_retry(img_url, is_image_download=True)

        if img_response and img_response.content:
            fetch_time = getattr(img_response, '_fetch_duration', 0.0) # Retrieve fetch time

            # --- Load/Decode Timing ---
            load_start_time = time.monotonic()
            try:
                img_data = io.BytesIO(img_response.content)
                img = Image.open(img_data)
                # No explicit resize here, but opening includes decoding
            except UnidentifiedImageError:
                # print(f"DEBUG: Unidentified image format: {img_url}") # Optional debug
                return None # Cannot proceed
            except OSError as e:
                # print(f"DEBUG: OSError opening image {img_url}: {e}") # Optional debug
                return None # Cannot proceed
            load_time = time.monotonic() - load_start_time
            # --- End Load/Decode Timing ---

            # --- Hashing Timing ---
            hash_start_time = time.monotonic()
            try:
                if HASH_ALGORITHM == 'phash': img_hash = imagehash.phash(img)
                elif HASH_ALGORITHM == 'dhash': img_hash = imagehash.dhash(img)
                elif HASH_ALGORITHM == 'ahash': img_hash = imagehash.average_hash(img)
                elif HASH_ALGORITHM == 'whash': img_hash = imagehash.whash(img)
                elif HASH_ALGORITHM == 'colorhash':
                     img_rgba = img.convert("RGBA"); img_hash = imagehash.colorhash(img_rgba); img_rgba.close() 
                else:
                    print(f"ERROR: Unknown HASH_ALGORITHM '{HASH_ALGORITHM}'. Defaulting to phash.")
                    img_hash = imagehash.phash(img)
            except Exception as e:
                print(f"ERROR: Failed hashing image from {img_url[:75]}... (Server: {server_id}, Algo: {HASH_ALGORITHM}): {e}")
                return None 
            hash_time = time.monotonic() - hash_start_time
            # --- End Hashing Timing ---

            return {
                'hash': img_hash,
                'fetch_time': fetch_time,
                'load_time': load_time,
                'hash_time': hash_time
            }
        else:
            return None

    except Exception as e:
        print(f"ERROR: Unexpected issue processing image {img_url[:75]}... in _fetch_and_hash_image: {e}")
        return None
    finally:
        if img: img.close()
        if img_data: img_data.close()


# --- Thumbnail Fetching ---
def get_thumbnail_hashes_for_server(player_tokens, server_id):
    """Fetches thumbnails, hashes them, and reports average processing times."""
    global stop_requested, total_thumbnails_processed
    if not player_tokens or stop_requested: return []

    image_urls_to_fetch = []
    processed_count_for_server = 0
    metadata_fetch_start = time.monotonic()
    payload_batch = [{"requestId": f"{token[:10]}", "token": token, "type": "AvatarHeadshot", "size": "150x150", "format": "Png", "isCircular": False} for token in player_tokens]
    for i in range(0, len(payload_batch), THUMBNAIL_BATCH_SIZE):
        if stop_requested: break
        batch = payload_batch[i:i + THUMBNAIL_BATCH_SIZE]
        url = f"{BASE_URL_THUMBNAILS}/batch"
        response = fetch_with_retry(url, method="POST", json=batch)
        if not response: continue 

        try:
            data = response.json().get("data", [])
            for item in data:
                 if item.get("state") == "Completed" and item.get("imageUrl"):
                     image_urls_to_fetch.append(item["imageUrl"])
                 elif item.get("state") not in ["Completed", "Pending", "Error", "Blocked", "Rejected"]:
                      print(f"WARN: Unexpected thumbnail state '{item.get('state')}' for request ID {item.get('requestId', 'N/A')} in server {server_id}.")
        except json.JSONDecodeError:
            print(f"ERROR: Failed to decode JSON from thumbnail batch API response for server {server_id}. URL: {url}")
        except Exception as e:
            print(f"ERROR: Unexpected error processing thumbnail batch metadata for server {server_id}: {e}")
        if stop_requested: break 

    metadata_fetch_duration = time.monotonic() - metadata_fetch_start
    # --- End batch fetch ---

    if stop_requested: return []
    if not image_urls_to_fetch: return []

    hashes = []
    num_urls = len(image_urls_to_fetch)
    num_workers = min(MAX_IMAGE_DOWNLOAD_WORKERS, num_urls) if num_urls > 0 else 0

    if num_workers <= 0: return []

    # --- Initialize timing accumulators for this server ---
    total_fetch_time = 0.0
    total_load_time = 0.0
    total_hash_time = 0.0
    futures = []
    image_processing_start_time = time.monotonic()

    try:
        with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix=f'ImgHash_{server_id[:6]}') as executor:
            for img_url in image_urls_to_fetch:
                if stop_requested: break
                futures.append(executor.submit(_fetch_and_hash_image, img_url, server_id))

            if stop_requested:
                 for f in futures:
                     if not f.done(): f.cancel()

            for future in as_completed(futures):
                 if future.cancelled(): continue

                 try:
                    result_data = future.result() 
                    if result_data and result_data.get('hash'):
                        hashes.append(result_data['hash'])
                        processed_count_for_server += 1
                        total_fetch_time += result_data.get('fetch_time', 0.0)
                        total_load_time += result_data.get('load_time', 0.0)
                        total_hash_time += result_data.get('hash_time', 0.0)
                 except Exception as exc:
                     # Log errors from the worker thread if needed (can be noisy)
                     # print(f"DEBUG: Worker thread exception for server {server_id}: {exc}")
                     pass 

    except Exception as e:
        print(f"ERROR: ThreadPoolExecutor issue for server {server_id}: {e}")
        if not stop_requested:
            print("WARN: Setting stop_requested due to ThreadPoolExecutor error.")
            stop_requested = True


    image_processing_duration = time.monotonic() - image_processing_start_time
    total_thumbnails_processed += processed_count_for_server 

    # --- Calculate and Print Average Timings for this Server ---
    if processed_count_for_server > 0:
        avg_fetch = total_fetch_time / processed_count_for_server
        avg_load = total_load_time / processed_count_for_server
        avg_hash = total_hash_time / processed_count_for_server
        # --- Optional: Print detailed timing info ---
        #print(f"    [Timing Srv {server_id[:8]}] Processed {processed_count_for_server}/{num_urls} images in {image_processing_duration:.2f}s. Avg Times: Fetch={avg_fetch:.3f}s, Load={avg_load:.3f}s, Hash={avg_hash:.3f}s")
    elif num_urls > 0:
        print(f"    [Timing Srv {server_id[:8]}] Failed to process any of the {num_urls} images.")
    # else: No URLs to process initially

    # --- Optional: Print metadata fetch time ---
    # print(f"    [Timing Srv {server_id[:8]}] Metadata fetch took {metadata_fetch_duration:.2f}s")

    if stop_requested: return [] 
    return hashes


# --- Main Scanning Logic ---
def scan_place_for_similarities(place_id, cookie, similarity_threshold, hash_algorithm):
    """
    Scans a place ID for servers containing multiple similar thumbnails.
    """
    global stop_requested, total_thumbnails_processed, global_unique_bot_hashes, global_total_bot_instance_count

    valid_algorithms = ['phash', 'dhash', 'ahash', 'whash', 'colorhash']
    if hash_algorithm not in valid_algorithms: print("! ERROR: Invalid HASH_ALGORITHM !"); return
    if not PLACE_ID or not isinstance(PLACE_ID, str) or not PLACE_ID.isdigit(): print("! ERROR: Invalid PLACE_ID !"); return
    if not cookie or not isinstance(cookie, str) or len(cookie) < 1 or "YOUR_COOKIE_HERE" in cookie: print("! ERROR: Invalid .ROBLOSECURITY cookie !"); return
    try:
        clean_cookie = cookie.replace("_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_", "").strip()
        if not clean_cookie: raise ValueError("Cookie is empty after removing warning placeholder.")
        session.cookies.set('.ROBLOSECURITY', clean_cookie, domain='.roblox.com', path='/')
        session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    except Exception as e: print(f"ERROR: Failed to set up session cookies/headers: {e}"); return

    # --- Scan Initialization ---
    next_page_cursor = None
    total_servers_scanned = 0
    suspicious_servers_found = 0
    previously_flagged_servers = set()
    total_thumbnails_processed = 0 
    global_unique_bot_hashes = set() 
    global_total_bot_instance_count = 0 
    start_time = time.time()

    # --- Print Initial Config ---
    print(f"\n--- Starting Scan ---")
    print(f"Place ID: {place_id} | Algorithm: {hash_algorithm.upper()} | Threshold: {similarity_threshold}")
    print(f" *** Monitor '[Debug] Pair Diff:' output to tune threshold! ***")
    print(f"Workers: {MAX_IMAGE_DOWNLOAD_WORKERS} | Batch Size: {THUMBNAIL_BATCH_SIZE} | Retries: {MAX_RETRIES}")
    print("-" * 30)
    print("INFO: Cookie validity checked by first API call.")
    print("-" * 30)


    # --- Main Server Loop ---
    while not stop_requested:
        current_page_start_time = time.time()
        server_list_url = f"{BASE_URL_GAMES}/{place_id}/servers/Public?limit=100"
        if next_page_cursor: server_list_url += f"&cursor={next_page_cursor}"

        response = fetch_with_retry(server_list_url)
        page_fetch_time = time.time() - current_page_start_time

        if stop_requested: break
        if not response: print(f"ERROR: Failed to fetch server list page. Stopping scan."); break

        try:
            parse_start_time = time.time()
            data = response.json()
            servers = data.get("data", [])
            next_page_cursor = data.get("nextPageCursor")
            parse_time = time.time() - parse_start_time

            if not servers:
                if next_page_cursor: continue
                else: print(f"INFO: No more servers found."); break 

            num_servers_on_page = len(servers)
            print(f"\nProcessing {num_servers_on_page} servers from page (Fetch: {page_fetch_time:.2f}s, Parse: {parse_time:.3f}s). Total Scanned: {total_servers_scanned}...")

            page_suspicious_servers = 0

            # --- Process Each Server on the Page ---
            for server_index, server in enumerate(servers):
                if stop_requested: break
                server_processing_start_time = time.time() 

                server_id = server.get("id")
                player_tokens = server.get("playerTokens", [])
                player_count = server.get("playing", 0)
                max_players = server.get("maxPlayers", "?")

                if not server_id or not player_tokens or len(player_tokens) < 2:
                    if server_id: total_servers_scanned += 1
                    continue 

                thumbnail_processing_start_time = time.time()
                server_hashes = get_thumbnail_hashes_for_server(player_tokens, server_id)
                thumbnail_processing_time = time.time() - thumbnail_processing_start_time
                num_hashes_processed = len(server_hashes) 
                total_servers_scanned += 1 

                if stop_requested: break
                if num_hashes_processed < 2: 
                     # Optionally log time even if skipping comparison
                     # print(f"    [Timing Srv {server_id[:8]}] Thumbnail processing took {thumbnail_processing_time:.2f}s (skipped comparison)")
                     continue

                # --- Compare Hashes within the Server ---
                compare_start_time = time.time()
                similar_pairs_found_in_server = 0
                hashes_involved_in_pairs_this_server = set() 
                first_pair_logged = False
                min_diff_found = float('inf') 

                for hash1, hash2 in itertools.combinations(server_hashes, 2):
                    try:
                        hash_diff = hash1 - hash2
                        # --- DEBUG PRINT (Optional - can be verbose) ---
                        # debug_check_threshold = similarity_threshold + 10 # Show comparisons near the threshold
                        # if hash_diff < debug_check_threshold:
                        #     h1_str = str(hash1)[:16]; h2_str = str(hash2)[:16]
                        #     comparison_symbol = "<=" if hash_diff <= similarity_threshold else "> "
                        #     print(f"    [Debug] Srv {server_id[:8]}.. Pair Diff: {hash_diff:<3} {comparison_symbol} {similarity_threshold} ({HASH_ALGORITHM.upper()}) | H: {h1_str} vs {h2_str}")
                        # --- END DEBUG ---

                        if hash_diff <= similarity_threshold:
                            similar_pairs_found_in_server += 1
                            hashes_involved_in_pairs_this_server.add(hash1)
                            hashes_involved_in_pairs_this_server.add(hash2)
                            global_unique_bot_hashes.add(hash1)
                            global_unique_bot_hashes.add(hash2)
                            min_diff_found = min(min_diff_found, hash_diff) 

                            if not first_pair_logged: 
                                print(f"  >>> FOUND SIMILAR PAIR in Server {server_id} ({player_count}/{max_players})! First Diff: {hash_diff} <= {similarity_threshold}.")
                                first_pair_logged = True 

                    except Exception as e:
                        print(f"ERROR: Comparing hashes {hash1} & {hash2} in server {server_id}: {e}. Skipping pair.")
                        continue

                compare_time = time.time() - compare_start_time

                # --- Server Summary & Accumulate Total Instances ---
                if similar_pairs_found_in_server > 0:
                    num_unique_avatars_in_server = len(hashes_involved_in_pairs_this_server)
                    print(f"    FLAGGED Server {server_id}: Found {similar_pairs_found_in_server} pair(s) involving {num_unique_avatars_in_server} suspected bot instance(s) ({num_hashes_processed} thumbs). MinDiff: {min_diff_found}. (Compare: {compare_time:.3f}s)")

                    if server_id not in previously_flagged_servers:
                        suspicious_servers_found += 1
                        page_suspicious_servers += 1
                        previously_flagged_servers.add(server_id)

                    global_total_bot_instance_count += num_unique_avatars_in_server
                # else: # Optionally log time for non-flagged servers
                #     server_total_time = time.time() - server_processing_start_time
                #     print(f"    [Info] Server {server_id} ({player_count}/{max_players}) - No similar pairs found. (Thumbs: {thumbnail_processing_time:.2f}s, Compare: {compare_time:.3f}s, Total: {server_total_time:.2f}s)")


            # --- End of server loop for the page ---
            if stop_requested: break

            # --- Page Summary ---
            page_processing_duration = time.time() - current_page_start_time 
            print(f"--- Page Summary (Scanned {total_servers_scanned} total) ---")
            print(f"Processed page in {page_processing_duration:.2f} seconds.")
            print(f"Found {page_suspicious_servers} new suspicious servers this page.")
            print(f"Totals: Suspicious Servers={suspicious_servers_found}, Processed Thumbs={total_thumbnails_processed}, Unique Bot Hashes={len(global_unique_bot_hashes)}, Total Bot Instances={global_total_bot_instance_count}")

            if not next_page_cursor:
                print("\nReached end of server list.")
                break 

        except json.JSONDecodeError as e:
            print(f"CRITICAL ERROR: Failed to decode JSON response for server list. Stopping scan. Response text (first 500 chars): {response.text[:500] if response else 'No response object'}")
            traceback.print_exc()
            stop_requested = True 
            break
        except Exception as e:
            print(f"\nCRITICAL ERROR processing server page: {e}")
            traceback.print_exc()
            stop_requested = True 
            break


    # --- End of While Loop (Scan Finished or Stopped) ---
    end_time = time.time()
    elapsed_time = end_time - start_time
    print("\n--- Scan Finished ---")
    if stop_requested:
        last_exception = None  # Ensure last_exception is initialized
        is_auth_error_stop = isinstance(last_exception, requests.exceptions.RequestException) and getattr(last_exception.response, 'status_code', 0) in [401, 403]
        if total_servers_scanned == 0 and is_auth_error_stop:
             print("Scan stopped early due to Authentication Error (401/403). Check your .ROBLOSECURITY cookie.")
        elif total_servers_scanned == 0 and not response:
             print("Scan stopped early, likely failed to fetch the first server page. Check network/Place ID.")
        elif not next_page_cursor and total_servers_scanned > 0 and not is_auth_error_stop: 
             print("Scan reached end of servers but was interrupted during final page processing.")
        else: 
             print("Scan interrupted or stopped due to error/user request (Ctrl+C).")
    else:
        print("Scan completed normally (processed all available server pages).")


    # --- Final Statistics ---
    print(f"\n--- Final Results ---")
    print(f"Place ID Scanned: {place_id}")
    print(f"Hashing Algorithm Used: {hash_algorithm.upper()}")
    print(f"Similarity Threshold Used: {similarity_threshold}")

    print(f"\nTotal Servers Scanned: {total_servers_scanned}")
    print(f"Total Suspicious Servers Found (>=1 similar pair): {suspicious_servers_found}")
    print(f"Total Thumbnails Successfully Processed & Hashed: {total_thumbnails_processed}")

    num_unique_suspected_bot_hashes = len(global_unique_bot_hashes)
    print(f"\nTotal Unique Suspected Bot Hashes Found: {num_unique_suspected_bot_hashes}")
    print(f"  (Represents the *variety* of distinct bot appearances involved in similar pairs)")
    print(f"Total Suspected Bot Instances Seen: {global_total_bot_instance_count}")
    print(f"  (Sum of unique bot instances found *within each* flagged server; counts frequency)")

    # --- Percentage Estimates ---
    if total_thumbnails_processed > 0 and total_servers_scanned > 0:
        print(f"\n--- Rough Estimates Based on Scanned Sample & Settings ---")
        print(f" Caveats: Accuracy depends heavily on ALGORITHM ('{hash_algorithm.upper()}') and THRESHOLD ({similarity_threshold}). Tune carefully!")
        print(f"          Based ONLY on the {total_servers_scanned} scanned servers and {total_thumbnails_processed} processed thumbnails.")
        print("-" * 20)

        percent_suspicious_servers = (suspicious_servers_found / total_servers_scanned) * 100 if total_servers_scanned > 0 else 0
        print(f"1. Percentage of servers with at least 2 bots: {percent_suspicious_servers:.2f}% ({suspicious_servers_found}/{total_servers_scanned} scanned servers had >=1 similar pair)")

        percent_bot_instances_of_total_processed = (global_total_bot_instance_count / total_thumbnails_processed) * 100
        print(f"2. Bot thumbnails Percentage: {percent_bot_instances_of_total_processed:.2f}% ({global_total_bot_instance_count} total instances / {total_thumbnails_processed} total processed hashes)")
        print(f"   (Suggests roughly {percent_bot_instances_of_total_processed:.1f}% of successfully processed avatars were flagged as a bot instance in a suspicious server)")

        print(f"\n--- Extrapolation Warning ---")
        print(f"Extrapolating these percentages to the *entire game* population is highly speculative.")

    else:
        print("\nInsufficient data processed. Cannot calculate meaningful percentages or ratios.")

    print(f"\n--- Performance & Settings Summary ---")
    print(f"Max Concurrent Image Downloads Used: {MAX_IMAGE_DOWNLOAD_WORKERS}")
    print(f"Hashing Algorithm: {hash_algorithm.upper()} | Threshold: {similarity_threshold}")
    print(f"Elapsed Time: {elapsed_time:.2f} seconds ({elapsed_time / 60:.2f} minutes)")


if __name__ == "__main__":
    run_scan = True
    if not PLACE_ID or not isinstance(PLACE_ID, str) or not PLACE_ID.isdigit(): print("! ERROR: Invalid PLACE_ID !"); run_scan = False
    if not ROBLOSECURITY_COOKIE or not isinstance(ROBLOSECURITY_COOKIE, str) or len(ROBLOSECURITY_COOKIE) < 100 or "_|WARNING:-DO-NOT-SHARE-THIS" in ROBLOSECURITY_COOKIE and "YOUR_COOKIE_HERE" in ROBLOSECURITY_COOKIE: print("! ERROR: Invalid .ROBLOSECURITY cookie - Placeholder detected! Replace 'YOUR_COOKIE_HERE'."); run_scan = False
    elif not ROBLOSECURITY_COOKIE or not isinstance(ROBLOSECURITY_COOKIE, str) or len(ROBLOSECURITY_COOKIE) < 100 : print("! ERROR: Invalid .ROBLOSECURITY cookie - Too short or invalid format."); run_scan = False
    if HASH_ALGORITHM not in ['phash', 'dhash', 'ahash', 'whash', 'colorhash']: print(f"! ERROR: Invalid HASH_ALGORITHM: {HASH_ALGORITHM} !"); run_scan = False
    if not isinstance(SIMILARITY_DIFFERENCE_THRESHOLD, int) or SIMILARITY_DIFFERENCE_THRESHOLD < 0: print(f"! ERROR: Invalid SIMILARITY_DIFFERENCE_THRESHOLD: {SIMILARITY_DIFFERENCE_THRESHOLD} !"); run_scan = False

    if run_scan:
       try:
           scan_place_for_similarities(
               PLACE_ID,
               ROBLOSECURITY_COOKIE,
               SIMILARITY_DIFFERENCE_THRESHOLD,
               HASH_ALGORITHM
           )
       except KeyboardInterrupt:
           if not stop_requested:
                print("\nKeyboardInterrupt caught directly. Shutting down.")
                stop_requested = True
       except Exception as e:
            print(f"\n--- FATAL UNHANDLED ERROR ---")
            print(f"Error Type: {type(e).__name__}")
            print(f"Error Details: {e}")
            print("-" * 30)
            traceback.print_exc()
            print("-" * 30)
            print("The script encountered a critical error and had to stop.")
       finally:
           print("\nScript execution finished or terminated.")
           if session:
               session.close()
               print("HTTP session closed.")
    else:
        print("\nScript did not run due to configuration errors. Please fix the issues listed above.")