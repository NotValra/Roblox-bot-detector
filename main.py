
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
import threading
import math
import re  

# --- CONFIGURATION --- #

# IF YOU WANT TO STOP THE SCRIPT EARLY, PRESS CTRL+C
PLACE_ID = input("Place id here: ")
ROBLOSECURITY_COOKIE = "YOUR_COOKIE_HERE"  # Replace with your .ROBLOSECURITY cookie

# *** Choose Hashing Algorithm ***
HASH_ALGORITHM = "dhash"
# Main threshold for similarity checks (pairwise and global matches)
SIMILARITY_DIFFERENCE_THRESHOLD = 10  # Default: 12

# *** Global DB Seeding Threshold (Idea F refinement) ***
# Only add hashes to the global DB if a server contained at least
# one internal pair with a difference <= this value.
MIN_DIFF_TO_CONFIRM_BOT = 6  # Default: 6 (Lower means stricter confirmation, basically this means, what hashes to add to the DB depending of what the mindiff is, so if its lower than the `x` value, then it adds it (to add every value, just set it the same as your SIMILARITY_DIFFERENCE_THRESHOLD))

# *** Performance Tuning ***
MAX_IMAGE_DOWNLOAD_WORKERS = 75  # Default: 75
MAX_SERVER_PROCESS_WORKERS = 8  # Default: 8 
THUMBNAIL_BATCH_SIZE = 100  # Default: 100
SLEEP_INTERVAL_FOR_PAGE_COUNTING_IN_THE_BACKGROUND = 2.0  # Default: 2.0 (The page counter thing)
MIN_PLAYERS_TO_PROCESS = 3  # Default: 3 (Skip servers with fewer players than this)

# *** Network Settings ***
MAX_RETRIES = 3
RETRY_DELAY_BASE = 1.0
JITTER_MAX = 0.5
DEFAULT_429_NO_HEADER_WAIT = 30
REQUEST_TIMEOUT = 20

# *** Constants ***
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
BASE_URL_GAMES = "https://games.roblox.com/v1/games"
BASE_URL_THUMBNAILS = "https://thumbnails.roblox.com/v1"
BASE_URL_USERS = "https://users.roblox.com/v1/users"
# --- END OF CONFIGURATION --- #

### BELOW IS JUST CODE ###

# --- Global State & Counters ---
rate_limit_active = False
rate_limit_end_time = 0
stop_requested = False
session = requests.Session()
total_thumbnails_processed_global = 0
global_total_bot_instance_count_global = 0
total_servers_scanned_global = 0
total_players_in_processed_servers_global = 0
suspicious_servers_found_global = 0
previously_flagged_servers = set()
total_page_count = -1
page_counter_thread_running = False
page_counter_lock = threading.Lock()
globally_confirmed_bot_hashes = set() 


# --- Signal Handler ---
def signal_handler(sig, frame):
    """Handles Ctrl+C interruption."""
    global stop_requested
    if not stop_requested:
        print("\nCtrl+C detected! Attempting graceful shutdown...")
        stop_requested = True


signal.signal(signal.SIGINT, signal_handler)


# --- Rate Limit Handling ---
def wait_for_rate_limit():
    """Checks if rate limit is active and waits if necessary."""
    global rate_limit_active, rate_limit_end_time, stop_requested
    if rate_limit_active:
        wait_time = rate_limit_end_time - time.monotonic()
        if wait_time > 0:
            print(f"[RateLimit] API limited (429). Waiting {wait_time:.2f}s...")
            wait_interval = 0.5
            while wait_time > 0 and not stop_requested:
                time.sleep(min(wait_interval, wait_time))
                wait_time -= wait_interval
            if stop_requested:
                print("[RateLimit] Stop requested during wait.")
                rate_limit_active = False
                return
        rate_limit_active = False


def handle_rate_limit_response(response_headers, url_limited):
    """Processes a 429 response and sets the rate limit wait time."""
    global rate_limit_active, rate_limit_end_time, stop_requested
    if stop_requested:
        return
    short_url = url_limited.split("?")[0]
    print(f"WARN: Received 429 Too Many Requests from API: {short_url}")
    retry_after_header = response_headers.get("Retry-After")
    delay = DEFAULT_429_NO_HEADER_WAIT
    if retry_after_header:
        try:
            delay_seconds = int(retry_after_header)
            if delay_seconds > 0:
                delay = delay_seconds
            else:
                print(
                    f"[RateLimit] Ignored non-positive Retry-After "
                    f"('{retry_after_header}') for {short_url}."
                )
        except Exception as e:
            print(
                f"[RateLimit] Error processing Retry-After header "
                f"('{retry_after_header}') for {short_url}: {e}."
            )
    else:
        print(
            f"[RateLimit] No Retry-After header for {short_url}. "
            f"Using default {DEFAULT_429_NO_HEADER_WAIT}s."
        )
    sleep_duration = max(0, delay) + random.uniform(0, JITTER_MAX)
    print(f"[RateLimit] API limit hit ({short_url}). Waiting {sleep_duration:.2f}s...")
    rate_limit_active = True
    rate_limit_end_time = time.monotonic() + sleep_duration


# --- Fetch with Retry ---
def fetch_with_retry(
    url, method="GET", is_image_download=False, is_page_counter=False, **kwargs
):
    """Fetches a URL with retries, backoff, and rate limit handling."""
    global stop_requested, session
    retries = 0
    last_exception = None
    response = None
    local_max_retries = 1 if is_page_counter else MAX_RETRIES
    local_timeout = 10 if is_page_counter else REQUEST_TIMEOUT

    while retries <= local_max_retries:
        if stop_requested:
            return None
        wait_for_rate_limit()
        if stop_requested:
            return None

        try:
            kwargs.setdefault("timeout", local_timeout)
            _start_time = time.monotonic()
            response = session.request(method, url, headers=session.headers, **kwargs)

            if response.status_code == 429:
                handle_rate_limit_response(response.headers, url)
                last_exception = requests.exceptions.RequestException("429")
                response = None
                continue

            response.raise_for_status()

            if is_image_download:
                fetch_duration = time.monotonic() - _start_time
                response._fetch_duration = fetch_duration
            return response

        except requests.exceptions.Timeout as e:
            last_exception = e
            log_prefix = "WARN" if retries < local_max_retries else "ERROR"
            if not is_page_counter:
                print(
                    f"{log_prefix}: Request timed out ({url.split('?')[0]}...). "
                    f"Retrying ({retries+1}/{local_max_retries})..."
                )
        except requests.exceptions.ConnectionError as e:
            last_exception = e
            log_prefix = "WARN" if retries < local_max_retries else "ERROR"
            if not is_page_counter:
                print(
                    f"{log_prefix}: Connection error ({url.split('?')[0]}...): {e}. "
                    f"Retrying ({retries+1}/{local_max_retries})..."
                )
        except requests.exceptions.ChunkedEncodingError as e:
            last_exception = e
            log_prefix = "WARN" if retries < local_max_retries else "ERROR"
            if not is_page_counter:
                print(
                    f"{log_prefix}: Incomplete read ({url.split('?')[0]}...): {e}. "
                    f"Retrying ({retries+1}/{local_max_retries})..."
                )
        except requests.exceptions.RequestException as e:
            last_exception = e
            status_code = getattr(response, "status_code", "N/A")
            if status_code == 401 or status_code == 403:
                print(
                    f"CRITICAL ERROR: {status_code} from {url.split('?')[0]}. Cookie invalid?"
                )
                stop_requested = True
                return None
            log_prefix = "WARN" if retries < local_max_retries else "ERROR"
            if not is_page_counter or log_prefix == "ERROR":
                print(
                    f"{log_prefix}: Request failed ({url.split('?')[0]}... Status={status_code}): {e}. "
                    f"Retrying ({retries+1}/{local_max_retries})..."
                )
            response = None
        except Exception as e:
            last_exception = e
            log_prefix = "WARN" if retries < local_max_retries else "ERROR"
            if not is_page_counter:
                print(
                    f"{log_prefix}: Unexpected fetch error ({url.split('?')[0]}...): {e}. "
                    f"Retrying ({retries+1}/{local_max_retries})..."
                )
            response = None

        # Retry Logic
        retries += 1
        if retries <= local_max_retries and not stop_requested:
            delay = RETRY_DELAY_BASE * (2 ** (retries - 1)) + random.uniform(
                0, JITTER_MAX
            )
            sleep_duration = 2.0 if is_page_counter else delay
            sleep_end_time = time.monotonic() + sleep_duration
            while time.monotonic() < sleep_end_time and not stop_requested:
                time.sleep(0.1)
            if stop_requested:
                return None

    if not is_page_counter and not stop_requested:
        print(
            f"ERROR: Request failed permanently for {url.split('?')[0]}. "
            f"Last exception: {last_exception}"
        )

    return None


# --- Image Processing ---
def _fetch_and_hash_image(img_url, server_id):
    """Fetches, processes, and calculates the primary hash."""
    global stop_requested, HASH_ALGORITHM
    if stop_requested:
        return None
    img, img_data = None, None
    img_hash = None
    try:
        img_response = fetch_with_retry(img_url, is_image_download=True)
        if not (img_response and img_response.content):
            return None
        try:
            img_data = io.BytesIO(img_response.content)
            img = Image.open(img_data)
            img.load()
        except (UnidentifiedImageError, OSError):
            return None
        except Exception as e:
            print(f"WARN: Error processing image data {img_url[:75]}: {e}")
            return None
        try:
            if HASH_ALGORITHM == "phash":
                img_hash = imagehash.phash(img)
            elif HASH_ALGORITHM == "dhash":
                img_hash = imagehash.dhash(img)
            elif HASH_ALGORITHM == "ahash":
                img_hash = imagehash.average_hash(img)
            elif HASH_ALGORITHM == "whash":
                img_hash = imagehash.whash(img)
            elif HASH_ALGORITHM == "colorhash":
                with img.convert("RGBA") as img_rgba:
                    img_hash = imagehash.colorhash(img_rgba)
            else:
                print(f"ERROR: Unknown HASH_ALGORITHM '{HASH_ALGORITHM}'.")
                return None
        except Exception as e:
            print(f"ERROR: Hashing failed {img_url[:75]}: {e}")
            return None
        return {"hash": img_hash} if img_hash else None
    except Exception as e:
        print(f"ERROR: Unexpected error in _fetch_and_hash_image {img_url[:75]}: {e}")
        traceback.print_exc(limit=1)
    finally:
        if img:
            img.close()
        if img_data:
            img_data.close()
    return None


# --- Thumbnail Fetching ---
def get_thumbnail_hashes_for_server(player_tokens, server_id):
    """Fetches thumbnail URLs, then fetches/hashes images concurrently."""
    global stop_requested, BASE_URL_THUMBNAILS, THUMBNAIL_BATCH_SIZE, MAX_IMAGE_DOWNLOAD_WORKERS
    if not player_tokens or stop_requested:
        return [], 0
    image_urls_to_fetch, processed_count_for_server = [], 0
    payload_batch = [
        {
            "requestId": f"{token[:10]}",
            "token": token,
            "type": "AvatarHeadshot",
            "size": "150x150",
            "format": "Png",
            "isCircular": False,
        }
        for token in player_tokens
    ]
    # Fetch metadata in batches
    for i in range(0, len(payload_batch), THUMBNAIL_BATCH_SIZE):
        if stop_requested:
            break
        batch = payload_batch[i : i + THUMBNAIL_BATCH_SIZE]
        url = f"{BASE_URL_THUMBNAILS}/batch"
        response = fetch_with_retry(url, method="POST", json=batch)
        if not response:
            print(f"WARN: Skipping thumbnail metadata batch for {server_id}.")
            continue
        try:
            data = response.json().get("data", [])
            for item in data:
                if item.get("state") == "Completed" and item.get("imageUrl"):
                    image_urls_to_fetch.append(item["imageUrl"])
        except Exception as e:
            print(f"ERROR: Processing thumbnail metadata {server_id}: {e}")
            traceback.print_exc(limit=1)
        if stop_requested:
            break
    # Early exit if stopped or no URLs found
    if stop_requested or not image_urls_to_fetch:
        return [], 0
    # Fetch and hash images concurrently
    hashes_list = []
    num_urls = len(image_urls_to_fetch)
    num_workers = min(MAX_IMAGE_DOWNLOAD_WORKERS, num_urls) if num_urls > 0 else 0
    if num_workers <= 0:
        return [], 0
    futures = []
    try:
        with ThreadPoolExecutor(
            max_workers=num_workers, thread_name_prefix=f"ImgHash_{server_id[:6]}"
        ) as executor:
            for img_url in image_urls_to_fetch:
                if stop_requested:
                    break
                futures.append(
                    executor.submit(_fetch_and_hash_image, img_url, server_id)
                )
            if stop_requested:
                for f in futures:
                    if not f.done():
                        f.cancel()
            for future in as_completed(futures):
                if stop_requested or future.cancelled():
                    continue
                try:
                    result_data = future.result()
                    if result_data and result_data.get("hash"):
                        hashes_list.append(result_data["hash"])
                        processed_count_for_server += 1
                except Exception:
                    pass  # Ignore single image failures
    except Exception as e:
        print(f"ERROR: ThreadPoolExecutor issue {server_id}: {e}")
        traceback.print_exc(limit=1)
    return hashes_list, processed_count_for_server


# --- Single Server Processing ---
def process_single_server(server_data, similarity_threshold, hash_algorithm):
    """Gets hashes, compares (pairwise & globally), returns results."""
    global stop_requested, globally_confirmed_bot_hashes, MIN_PLAYERS_TO_PROCESS
    server_id = server_data.get("id")
    player_tokens = server_data.get("playerTokens", [])
    player_count = server_data.get("playing", 0)
    server_result = {
        "server_id": server_id,
        "processed": False,
        "flagged": False,
        "flagged_by_global": False,
        "similar_pairs": 0,
        "unique_bot_hashes_in_server": set(),
        "min_diff": float("inf"),
        "thumbnails_processed_count": 0,
        "player_count": 0,
        "error": None,
    }

    if stop_requested:
        return server_result
    # Skip servers below minimum player count
    if player_count < MIN_PLAYERS_TO_PROCESS:
        server_result["processed"] = True
        return server_result
    # Skip servers with invalid data or too few tokens for comparison
    if not server_id or not player_tokens or len(player_tokens) < 2:
        server_result["processed"] = True
        return server_result

    try:
        server_hashes, thumbnails_processed = get_thumbnail_hashes_for_server(
            player_tokens, server_id
        )
        server_result["thumbnails_processed_count"] = thumbnails_processed

        if stop_requested:
            return server_result
        # Need >= 1 hash for global check, >= 2 for pair check
        if len(server_hashes) < 1:
            server_result["processed"] = True
            server_result["player_count"] = player_count
            return server_result

        # --- Global DB Check ---
        hashes_involved_this_server = set()
        found_match_in_global_db = False
        for h in server_hashes:
            if h in globally_confirmed_bot_hashes:
                hashes_involved_this_server.add(h)
                found_match_in_global_db = True
        if found_match_in_global_db:
            server_result["flagged_by_global"] = True

        # --- Internal Pair Check ---
        similar_pairs_found = 0
        min_diff_found = float("inf")
        internal_threshold = similarity_threshold  # Use main threshold

        if len(server_hashes) >= 2:
            for hash1, hash2 in itertools.combinations(server_hashes, 2):
                try:
                    hash_diff = hash1 - hash2
                    if hash_diff <= internal_threshold:
                        similar_pairs_found += 1
                        hashes_involved_this_server.add(hash1)
                        hashes_involved_this_server.add(hash2)
                        min_diff_found = min(min_diff_found, hash_diff)
                except Exception as e:
                    print(f"WARN: Comparing hashes {hash1} & {hash2}: {e}.")

        server_result["similar_pairs"] = similar_pairs_found
        if min_diff_found != float("inf"):
            server_result["min_diff"] = min_diff_found

        # --- Final Flagging Decision ---
        server_flagged = found_match_in_global_db or (similar_pairs_found > 0)
        server_result["flagged"] = server_flagged
        # Store all implicated hashes (primary) for this server
        server_result["unique_bot_hashes_in_server"] = hashes_involved_this_server

        # Mark as processed and record player count
        server_result["processed"] = True
        server_result["player_count"] = player_count

    except Exception as e:
        print(f"ERROR: Failed processing server {server_id}: {e}")
        traceback.print_exc(limit=1)
        server_result["error"] = str(e)

    return server_result


# --- ETA Formatting ---
def format_eta(seconds):
    """Formats seconds into a readable HH:MM:SS string."""
    if seconds < 0 or seconds == float("inf") or math.isnan(seconds):
        return "N/A"
    seconds = math.ceil(seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes:02d}:{secs:02d}"


# --- Background Page Counting ---
def count_total_pages_background(place_id):
    """Background thread function to count total server pages slowly."""
    global stop_requested, total_page_count, page_counter_thread_running
    global page_counter_lock, BASE_URL_GAMES

    print("INFO: Background page counter thread started.")
    current_cursor = None
    page_count = 0
    error_count = 0
    max_page_errors = 3

    while not stop_requested and error_count < max_page_errors:
        page_count += 1
        url = f"{BASE_URL_GAMES}/{place_id}/servers/Public?limit=100"
        if current_cursor:
            url += f"&cursor={current_cursor}"

        response = fetch_with_retry(url, is_page_counter=True)

        if response:
            error_count = 0
            try:
                data = response.json()
                next_cursor = data.get("nextPageCursor")
                if not next_cursor:
                    # Reached the end
                    with page_counter_lock:
                        total_page_count = page_count
                    print(
                        f"INFO: Background page counter finished. Total pages: {page_count}"
                    )
                    break  # Exit loop successfully
                else:
                    # Continue to the next page
                    current_cursor = next_cursor
            except json.JSONDecodeError:
                print("WARN: Background page counter JSON decode error.")
                error_count += 1
            except Exception as e:
                print(f"WARN: Background page counter error: {e}.")
                error_count += 1
        else:
            print("WARN: Background page counter fetch failed.")
            error_count += 1
            # Loop will check stop_requested

        # Sleep between requests
        if not stop_requested:
            sleep_interval = SLEEP_INTERVAL_FOR_PAGE_COUNTING_IN_THE_BACKGROUND
            sleep_end = time.monotonic() + sleep_interval
            while time.monotonic() < sleep_end and not stop_requested:
                time.sleep(0.2)

    if error_count >= max_page_errors:
        print("ERROR: Background page counter stopped due to errors.")

    # Signal completion/exit
    with page_counter_lock:
        page_counter_thread_running = False


# === [MAIN SCANNING LOGIC - Uses MIN_DIFF_TO_CONFIRM_BOT] ===
def scan_place_for_similarities(place_id, cookie, similarity_threshold, hash_algorithm):
    global stop_requested, total_thumbnails_processed_global
    global global_total_bot_instance_count_global, total_servers_scanned_global
    global suspicious_servers_found_global, previously_flagged_servers, session
    global total_players_in_processed_servers_global, globally_confirmed_bot_hashes
    global total_page_count, page_counter_thread_running, page_counter_lock
    global MIN_DIFF_TO_CONFIRM_BOT  # Access the new config

    # Input Validation & Session Setup
    valid_algorithms = ["phash", "dhash", "ahash", "whash", "colorhash"]
    if hash_algorithm not in valid_algorithms:
        print("! ERROR: Invalid HASH_ALGORITHM !")
        return
    if not place_id or not isinstance(place_id, str) or not place_id.isdigit():
        print("! ERROR: Invalid PLACE_ID !")
        return
    if (
        not cookie
        or not isinstance(cookie, str)
        or len(cookie) < 100
        or "YOUR_COOKIE_HERE" in cookie
    ):
        print("! ERROR: Invalid .ROBLOSECURITY cookie!")
        return
    try:
        clean_cookie = cookie.replace(
            "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_",
            "",
        ).strip()
        if not clean_cookie:
            raise ValueError("Cookie empty")
        session.cookies.set(
            ".ROBLOSECURITY", clean_cookie, domain=".roblox.com", path="/"
        )
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        print("INFO: Session configured.")
    except Exception as e:
        print(f"ERROR: Failed session setup: {e}")
        return

    # Initial Fetch
    universe_id = None
    total_players_in_game = 0
    print("\nFetching game details...")
    try:
        details_url = f"{BASE_URL_GAMES}/multiget-place-details?placeIds={place_id}"
        details_response = fetch_with_retry(details_url)
        if details_response:
            details_data = details_response.json()
            if (
                details_data
                and isinstance(details_data, list)
                and len(details_data) > 0
            ):
                universe_id = details_data[0].get("universeId")
                if universe_id:
                    print(f"  > Universe ID: {universe_id}")
                    game_info_url = f"{BASE_URL_GAMES}?universeIds={universe_id}"
                    game_info_response = fetch_with_retry(game_info_url)
                    if game_info_response:
                        game_info_data = game_info_response.json()
                        if (
                            game_info_data
                            and game_info_data.get("data")
                            and isinstance(game_info_data["data"], list)
                            and len(game_info_data["data"]) > 0
                        ):
                            total_players_in_game = game_info_data["data"][0].get(
                                "playing", 0
                            )
                            print(f"  > Players in Game: {total_players_in_game}")
                        else:
                            print("  > WARN: Could not parse player count.")
                    else:
                        print("  > WARN: Failed to fetch game info.")
                else:
                    print("  > WARN: Universe ID not found.")
            else:
                print("  > WARN: Unexpected place details format.")
        else:
            print("  > WARN: Failed to fetch place details.")
    except json.JSONDecodeError as e:
        print(f"  > ERROR decoding details JSON: {e}")
    except Exception as e:
        print(f"  > ERROR fetching details: {e}")
        traceback.print_exc(limit=1)
    if total_players_in_game <= 0:
        print("WARN: Player count unknown.")
        total_players_in_game = 0

    # Reset Counters & Start Background Thread
    total_thumbnails_processed_global, global_total_bot_instance_count_global = 0, 0
    previously_flagged_servers = set()
    total_servers_scanned_global, total_players_in_processed_servers_global = 0, 0
    suspicious_servers_found_global = 0
    globally_confirmed_bot_hashes = set()
    start_time = time.time()
    with page_counter_lock:
        total_page_count = -1
        page_counter_thread_running = True
    page_thread = threading.Thread(
        target=count_total_pages_background, args=(place_id,), daemon=True
    )
    page_thread.start()

    # Print Config
    print(f"\n--- Starting Server Scan ---")
    print(
        f"Place ID: {place_id} | Algorithm: {hash_algorithm.upper()} | Threshold: {similarity_threshold}"
    )
    print(
        f"Server Workers: {MAX_SERVER_PROCESS_WORKERS} | Image Workers: {MAX_IMAGE_DOWNLOAD_WORKERS}"
    )
    print(f"Batch Size: {THUMBNAIL_BATCH_SIZE} | Retries: {MAX_RETRIES}")
    print(
        f"Skip Servers < {MIN_PLAYERS_TO_PROCESS} players: {'Yes' if MIN_PLAYERS_TO_PROCESS >= 2 else 'No'}"
    )
    print(f"MinDiff to Add to Global DB: {MIN_DIFF_TO_CONFIRM_BOT}")
    print("-" * 30)

    # Main Loop
    next_page_cursor = None
    page_number = 0
    server_executor = ThreadPoolExecutor(
        max_workers=MAX_SERVER_PROCESS_WORKERS, thread_name_prefix="ServerProc"
    )
    try:
        while not stop_requested:
            current_page_number_display = page_number + 1
            current_page_start_time = time.time()
            server_list_url = f"{BASE_URL_GAMES}/{place_id}/servers/Public?limit=100"
            if next_page_cursor:
                server_list_url += f"&cursor={next_page_cursor}"
            response = fetch_with_retry(server_list_url)
            page_fetch_time = time.time() - current_page_start_time
            if stop_requested or not response:
                if not response and not stop_requested:
                    print(
                        f"ERROR: Failed server list fetch page {current_page_number_display}."
                    )
                break
            try:
                parse_start_time = time.time()
                data = response.json()
                servers_on_page = data.get("data", [])
                current_page_cursor = next_page_cursor
                next_page_cursor = data.get("nextPageCursor")
                parse_time = time.time() - parse_start_time
                if (
                    not servers_on_page
                    and current_page_cursor is None
                    and page_number == 0
                ):
                    print("INFO: Page 1 empty.")
                    next_page_cursor = None
                    break
                elif not servers_on_page:
                    if next_page_cursor:
                        print(
                            f"INFO: Page {current_page_number_display} empty, continuing..."
                        )
                        page_number += 1
                        continue
                    else:
                        print(
                            f"INFO: Page {current_page_number_display} - No more servers found."
                        )
                        break

                # Page Header
                num_servers_fetched = len(servers_on_page)
                current_total_processed = total_servers_scanned_global
                with page_counter_lock:
                    known_total_pages = total_page_count
                    counter_running = page_counter_thread_running
                page_display = f"{current_page_number_display}" + (
                    f" / {known_total_pages}"
                    if known_total_pages > 0
                    else (
                        " / ? (Counting...)"
                        if counter_running
                        else " / ? (Count failed)"
                    )
                )
                print(f"\n--- Page {page_display} ---")
                print(
                    f"Received {num_servers_fetched} servers (Fetch: {page_fetch_time:.2f}s, Parse: {parse_time:.3f}s). Total Processed: {current_total_processed}. Submitting..."
                )

                # Submit Tasks
                page_futures = {}
                for server_data in servers_on_page:
                    if stop_requested:
                        break
                    server_id = server_data.get("id", "UNKNOWN")
                    future = server_executor.submit(
                        process_single_server,
                        server_data,
                        similarity_threshold,
                        hash_algorithm,
                    )
                    page_futures[future] = server_id
                if stop_requested:
                    print("INFO: Stop requested during submission.")
                    for f in page_futures:
                        if not f.done():
                            f.cancel()

                # Process Results
                page_completed = 0
                page_flagged = 0
                page_thumbs = 0
                page_players = 0
                page_processed_srv = 0
                for future in as_completed(page_futures):
                    server_id = page_futures[future]
                    page_completed += 1
                    if stop_requested or future.cancelled():
                        continue
                    try:
                        result = future.result()
                        if result["processed"]:
                            page_processed_srv += 1
                            total_servers_scanned_global += 1
                            player_count = result.get("player_count", 0)
                            if (
                                player_count >= MIN_PLAYERS_TO_PROCESS
                                or player_count == 0
                            ):
                                total_players_in_processed_servers_global += (
                                    player_count
                                )
                                page_players += player_count
                        total_thumbnails_processed_global += result[
                            "thumbnails_processed_count"
                        ]
                        page_thumbs += result["thumbnails_processed_count"]

                        # <<< Modified Global DB Update Logic >>>
                        if result["flagged"]:
                            instances = len(result["unique_bot_hashes_in_server"])
                            reason = (
                                "(Global DB)"
                                if result.get("flagged_by_global")
                                and result.get("similar_pairs", 0) == 0
                                else ""
                            )
                            min_diff_this_server = result.get("min_diff", float("inf"))

                            if server_id not in previously_flagged_servers:
                                suspicious_servers_found_global += 1
                                page_flagged += 1
                                previously_flagged_servers.add(server_id)
                                print(
                                    f"  >>> FLAGGED Server {server_id[-12:]}: Found {instances} bots (Pairs: {result.get('similar_pairs',0)}) {reason}. MinDiff: {min_diff_this_server if min_diff_this_server != float('inf') else 'N/A'}."
                                )

                            # Only update global DB if min_diff meets the stricter threshold
                            if min_diff_this_server <= MIN_DIFF_TO_CONFIRM_BOT:
                                globally_confirmed_bot_hashes.update(
                                    result["unique_bot_hashes_in_server"]
                                )

                            # Accumulate total instances found in this server
                            global_total_bot_instance_count_global += instances

                        if result["error"]:
                            print(
                                f"WARN: Server {server_id[-12:]} task error: {result['error']}"
                            )
                    except Exception as exc:
                        print(f"ERROR: Retrieving result for server {server_id}: {exc}")
                        traceback.print_exc(limit=1)

                # Page Summary
                page_number += 1
                if stop_requested:
                    break
                duration = time.time() - current_page_start_time
                with page_counter_lock:
                    known_total_pages = total_page_count
                page_disp = f"{current_page_number_display}" + (
                    f" / {known_total_pages}" if known_total_pages > 0 else ""
                )
                print(f"--- Page {page_disp} Summary ---")
                print(
                    f"Finished {page_completed}/{num_servers_fetched} tasks. Processed/Skipped: {page_processed_srv} servers in {duration:.2f}s."
                )
                print(f"Flagged {page_flagged} new servers.")
                print(
                    f"Processed {page_thumbs} thumbs & {page_players} players this page."
                )

                # Progress Rate & ETA
                elapsed = time.time() - start_time
                rate_str = "Rate: ..."
                eta_str = "ETA: ..."
                if elapsed > 1:
                    srv_per_m = (
                        (total_servers_scanned_global / (elapsed / 60))
                        if total_servers_scanned_global > 0
                        else 0
                    )
                    plyr_per_s = (
                        (total_players_in_processed_servers_global / elapsed)
                        if total_players_in_processed_servers_global > 0
                        else 0
                    )
                    sec_per_pg = (elapsed / page_number) if page_number > 0 else 0
                    rate_str = f"Rate: ~{plyr_per_s:.1f} players/sec | ~{srv_per_m:.1f} servers/min | ~{sec_per_pg:.2f} sec/page"
                    with page_counter_lock:
                        known_pages = total_page_count
                    if known_pages > 0:
                        if page_number > 0 and sec_per_pg > 0:
                            remaining = known_pages - page_number
                            if remaining >= 0:
                                eta_s = sec_per_pg * remaining
                                eta_str = f"ETA: ~{format_eta(eta_s)} ({remaining} pages left)"
                            else:
                                eta_str = "ETA: Finishing..."
                        else:
                            eta_str = (
                                f"ETA: Calculating... (0/{known_pages} pages done)"
                            )
                    elif page_counter_thread_running:
                        eta_str = "ETA: Calculating... (Waiting for page count)"
                    else:
                        eta_str = "ETA: N/A (Page count failed)"
                print(rate_str)
                print(eta_str)

                # Running Totals
                pct_str = (
                    f"({(total_players_in_processed_servers_global / total_players_in_game * 100):.1f}%)"
                    if total_players_in_game > 0
                    else ""
                )
                print(
                    f"Totals: Servers Processed={total_servers_scanned_global}, Players Scanned={total_players_in_processed_servers_global}{pct_str}, Servers Flagged={suspicious_servers_found_global}, Thumbsnails={total_thumbnails_processed_global}, Global DB={len(globally_confirmed_bot_hashes)}, Bot Instances={global_total_bot_instance_count_global}"
                )

                # Check for End of List
                if not next_page_cursor:
                    print("\nReached end of server list.")
                    if page_counter_thread_running:
                        print("INFO: Signalling background page counter to stop.")
                        stop_requested = True
                    break
            # Page Level Exceptions
            except json.JSONDecodeError as e:
                print(f"CRITICAL: Failed JSON decode. {e}")
                traceback.print_exc()
                stop_requested = True
                break
            except Exception as e:
                print(f"\nCRITICAL: Error processing page. {e}")
                traceback.print_exc()
                stop_requested = True
                break
        # End While Loop
    finally:  # Shutdown
        print("\nRequesting background page counter stop (if running)...")
        stop_requested = True
        if "page_thread" in locals() and page_thread.is_alive():
            page_thread.join(timeout=5.0)
        if "page_thread" in locals() and page_thread.is_alive():
            print("WARN: Page counter thread timed out.")
        print("Shutting down server pool...")
        server_executor.shutdown(wait=False, cancel_futures=True)
        print("Server pool shut down.")

    # === [FINAL REPORTING] ===
    end_time = time.time()
    elapsed_time = end_time - start_time
    print("\n--- Scan Finished ---")
    scan_completed_normally = not next_page_cursor and not stop_requested
    if scan_completed_normally:
        print("Scan completed normally.")
    elif stop_requested and not scan_completed_normally:
        print("Scan interrupted or stopped early.")
    else:
        print("Scan finished (Reason unclear).")
    print(f"\n--- Final Results ---")
    print(f"Place ID: {place_id}")
    print(f"Algorithm: {hash_algorithm.upper()}")
    print(f"Threshold: {similarity_threshold}")
    print(f"Min Players Skipped: {MIN_PLAYERS_TO_PROCESS}")
    print(f"MinDiff to Confirm Bot: {MIN_DIFF_TO_CONFIRM_BOT}")  # Show new config
    print(
        f"\nServers Processed (>= {MIN_PLAYERS_TO_PROCESS} players): {total_servers_scanned_global}"
    )
    print(
        f"Players Scanned (in processed servers): {total_players_in_processed_servers_global}"
    )
    print(f"Suspicious Servers Found: {suspicious_servers_found_global}")
    print(f"Thumbnails Processed: {total_thumbnails_processed_global}")
    print(
        f"\nUnique Bot Appearance Hashes (Global DB Size): {len(globally_confirmed_bot_hashes)}"
    )
    print(
        f"   (Based on hashes from servers with internal MinDiff <= {MIN_DIFF_TO_CONFIRM_BOT})"
    )
    bot_instances = global_total_bot_instance_count_global
    print(f"Total Bot Instances Seen (Sum across flagged servers): {bot_instances}")
    print(f"   (Counts unique similar players within each flagged server)")
    if total_thumbnails_processed_global > 0 and total_servers_scanned_global > 0:
        print(f"\n--- Rough Estimates Based on Scanned Sample ---")
        print(f" Caveats: Settings-dependent, sample bias.")
        print(
            f" Based on {total_servers_scanned_global} servers (>= {MIN_PLAYERS_TO_PROCESS}p), {total_players_in_processed_servers_global} players, {total_thumbnails_processed_global} thumbs."
        )
        print("-" * 20)
        percent_suspicious_servers = (
            (suspicious_servers_found_global / total_servers_scanned_global) * 100
            if total_servers_scanned_global > 0
            else 0
        )
        print(f"1. % Scanned Servers Flagged: {percent_suspicious_servers:.2f}%")
        percent_bot_thumbs = (
            (global_total_bot_instance_count_global / total_thumbnails_processed_global)
            * 100
            if total_thumbnails_processed_global > 0
            else 0
        )
        print(f"2. % Thumbs as Bot Instances: {percent_bot_thumbs:.2f}%")
        if total_players_in_processed_servers_global > 0:
            percent_bot_players = (
                global_total_bot_instance_count_global
                / total_players_in_processed_servers_global
            ) * 100
            print(f"3. Est. % Players as Bot Instances: {percent_bot_players:.2f}%")
        print(f"\n--- Extrapolation Warning ---")
        print(f"Extrapolating these percentages is highly speculative.")
    else:
        print("\nInsufficient data processed for estimates.")
    print(f"\n--- Performance & Settings ---")
    print(f"Server Workers: {MAX_SERVER_PROCESS_WORKERS}")
    print(f"Image Workers: {MAX_IMAGE_DOWNLOAD_WORKERS}")
    print(f"Algorithm: {hash_algorithm.upper()} | Threshold: {similarity_threshold}")
    print(f"Elapsed Time: {elapsed_time:.2f}s (~{elapsed_time / 60:.2f}m)")


# === [MAIN EXECUTION BLOCK - Added Validation] ===
if __name__ == "__main__":
    run_scan = True
    # Validation checks
    if not PLACE_ID or not isinstance(PLACE_ID, str) or not PLACE_ID.isdigit():
        print("! ERROR: Invalid PLACE_ID")
        run_scan = False
    if (
        not ROBLOSECURITY_COOKIE
        or not isinstance(ROBLOSECURITY_COOKIE, str)
        or len(ROBLOSECURITY_COOKIE) < 100
    ):
        print("! ERROR: Invalid Cookie format/length.")
        run_scan = False
    elif (
        "_|WARNING:-DO-NOT-SHARE-THIS" in ROBLOSECURITY_COOKIE
        and "YOUR_COOKIE_HERE" in ROBLOSECURITY_COOKIE
    ):
        print("! ERROR: Placeholder Cookie detected!")
        run_scan = False
    if HASH_ALGORITHM not in ["phash", "dhash", "ahash", "whash", "colorhash"]:
        print(f"! ERROR: Invalid HASH_ALGORITHM: {HASH_ALGORITHM}")
        run_scan = False
    if (
        not isinstance(SIMILARITY_DIFFERENCE_THRESHOLD, int)
        or SIMILARITY_DIFFERENCE_THRESHOLD < 0
    ):
        print(
            f"! ERROR: Invalid SIMILARITY_DIFFERENCE_THRESHOLD: {SIMILARITY_DIFFERENCE_THRESHOLD}"
        )
        run_scan = False
    # <<< Add validation for MIN_DIFF_TO_CONFIRM_BOT >>>
    if not isinstance(MIN_DIFF_TO_CONFIRM_BOT, int) or MIN_DIFF_TO_CONFIRM_BOT < 0:
        print(f"! ERROR: Invalid MIN_DIFF_TO_CONFIRM_BOT: {MIN_DIFF_TO_CONFIRM_BOT}")
        run_scan = False
    if (
        not isinstance(MAX_IMAGE_DOWNLOAD_WORKERS, int)
        or MAX_IMAGE_DOWNLOAD_WORKERS <= 0
    ):
        print(
            f"! ERROR: Invalid MAX_IMAGE_DOWNLOAD_WORKERS: {MAX_IMAGE_DOWNLOAD_WORKERS}"
        )
        run_scan = False
    if (
        not isinstance(MAX_SERVER_PROCESS_WORKERS, int)
        or MAX_SERVER_PROCESS_WORKERS <= 0
    ):
        print(
            f"! ERROR: Invalid MAX_SERVER_PROCESS_WORKERS: {MAX_SERVER_PROCESS_WORKERS}"
        )
        run_scan = False
    if not isinstance(THUMBNAIL_BATCH_SIZE, int) or THUMBNAIL_BATCH_SIZE <= 0:
        print(f"! ERROR: Invalid THUMBNAIL_BATCH_SIZE: {THUMBNAIL_BATCH_SIZE}")
        run_scan = False
    if not isinstance(MIN_PLAYERS_TO_PROCESS, int) or MIN_PLAYERS_TO_PROCESS < 2:
        print(f"! ERROR: Invalid MIN_PLAYERS_TO_PROCESS: {MIN_PLAYERS_TO_PROCESS}")
        run_scan = False

    if run_scan:
        print("Config checks passed. Starting scan...")
        try:
            scan_place_for_similarities(
                PLACE_ID,
                ROBLOSECURITY_COOKIE,
                SIMILARITY_DIFFERENCE_THRESHOLD,
                HASH_ALGORITHM,
            )
        except KeyboardInterrupt:
            if not stop_requested:
                print("\nKeyboardInterrupt caught.")
                stop_requested = True
        except Exception as e:
            print(f"\n--- FATAL UNHANDLED ERROR ---")
            print(f"Type: {type(e).__name__}, Details: {e}")
            print("-" * 30)
            traceback.print_exc()
            print("-" * 30)
            print("Script stopped due to critical error.")
        finally:
            print("\nScript execution finished or terminated.")
            if session:
                session.close()
                print("HTTP session closed.")
    else:
        print("\nScript not run due to config errors.")

    input("\nPress Enter to close window...")
