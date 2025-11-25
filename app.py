import asyncio
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)

# Streamrip library imports
from streamrip.client import DeezerClient, QobuzClient, SoundcloudClient, TidalClient
from streamrip.config import Config

# new logging config
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

STREAMRIP_CONFIG = os.environ.get("STREAMRIP_CONFIG", "/config/config.toml")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/music")
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))

download_queue = queue.Queue()
active_downloads = {}
download_history = []
sse_clients = []
album_art_cache = {}
cache_lock = threading.Lock()

# Initialize streamrip config and clients
streamrip_config = None
streamrip_clients = {}

# Global event loop for async operations
async_loop = None
async_thread = None


def run_event_loop(loop):
    """Run the event loop in a separate thread"""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def init_async_loop():
    """Initialize a persistent event loop in a separate thread"""
    global async_loop, async_thread

    async_loop = asyncio.new_event_loop()
    async_thread = threading.Thread(
        target=run_event_loop, args=(async_loop,), daemon=True
    )
    async_thread.start()
    logger.info("Async event loop initialized")


def run_async(coro):
    """Run a coroutine in the global event loop and wait for result"""
    global async_loop

    if async_loop is None:
        init_async_loop()

    # async_loop is guaranteed to be initialized here
    assert async_loop is not None
    future = asyncio.run_coroutine_threadsafe(coro, async_loop)
    return future.result(timeout=60)  # 60 second timeout


def init_streamrip():
    """Initialize streamrip config and clients"""
    global streamrip_config, streamrip_clients

    try:
        if os.path.exists(STREAMRIP_CONFIG):
            streamrip_config = Config(STREAMRIP_CONFIG)
            logger.info(f"Loaded streamrip config from {STREAMRIP_CONFIG}")
        else:
            streamrip_config = Config.defaults()
            logger.warning(f"Config not found at {STREAMRIP_CONFIG}, using defaults")

        # Initialize clients (will login on first use)
        # Clients expect Config object, which has .session attribute internally
        streamrip_clients = {
            "qobuz": QobuzClient(streamrip_config),
            "deezer": DeezerClient(streamrip_config),
            "tidal": TidalClient(streamrip_config),
            "soundcloud": SoundcloudClient(streamrip_config),
        }
        logger.info("Streamrip clients initialized")
    except Exception as e:
        logger.error(f"Failed to initialize streamrip: {e}")
        streamrip_config = Config.defaults()
        streamrip_clients = {}


# Initialize on startup
init_async_loop()
init_streamrip()


async def get_client(source):
    """Get and login to a streamrip client"""
    client = streamrip_clients.get(source)
    if not client:
        raise ValueError(f"Client not available for source: {source}")

    if not client.logged_in:
        try:
            await client.login()
            logger.info(f"Logged in to {source}")
        except Exception as e:
            logger.error(f"Failed to login to {source}: {e}")
            raise
    return client


class DownloadWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.current_process = None

    def run(self):
        while True:
            task = download_queue.get()
            if task is None:
                break

            task_id = task["id"]
            url = task["url"]
            quality = task.get("quality", 3)
            metadata = task.get("metadata", {})

            active_downloads[task_id] = {
                "status": "downloading",
                "url": url,
                "metadata": metadata,
                "started": time.time(),
            }

            broadcast_sse(
                {
                    "type": "download_started",
                    "id": task_id,
                    "metadata": metadata,
                    "status": "downloading",
                }
            )

            output_lines = []
            process = None

            try:
                cmd = ["rip"]
                if os.path.exists(STREAMRIP_CONFIG):
                    cmd.extend(["--config-path", STREAMRIP_CONFIG])
                cmd.extend(["-q", str(quality)])
                cmd.extend(["url", url])

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )

                self.current_process = process

                for line in process.stdout:
                    line = line.strip()
                    if line:
                        output_lines.append(line)
                        if len(output_lines) % 10 == 0:
                            broadcast_sse(
                                {
                                    "type": "download_progress",
                                    "id": task_id,
                                    "output": "\n".join(output_lines[-5:]),
                                    "progress": {"raw_output": True},
                                }
                            )

                process.wait()

                broadcast_sse(
                    {
                        "type": "download_completed",
                        "id": task_id,
                        "status": "completed" if process.returncode == 0 else "failed",
                        "metadata": metadata,
                        "output": "\n".join(output_lines),
                    }
                )

            except Exception as e:
                broadcast_sse(
                    {
                        "type": "download_error",
                        "id": task_id,
                        "error": str(e),
                        "output": "\n".join(output_lines) if output_lines else str(e),
                    }
                )

            finally:
                self.current_process = None
                if task_id in active_downloads:
                    del active_downloads[task_id]
                if process and process.poll() is None:
                    process.terminate()

            download_queue.task_done()


def broadcast_sse(data):
    message = f"data: {json.dumps(data)}\n\n"
    dead_clients = []

    for client in sse_clients:
        try:
            client.put(message)
        except:
            dead_clients.append(client)

    for client in dead_clients:
        sse_clients.remove(client)


@app.route("/api/events")
def sse_events():
    def generate():
        q = queue.Queue()
        sse_clients.append(q)

        try:
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"

            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    continue  # previous heartbeat check
        finally:
            sse_clients.remove(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


workers = []
for _ in range(MAX_CONCURRENT_DOWNLOADS):
    worker = DownloadWorker()
    worker.start()
    workers.append(worker)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url")
    quality = data.get("quality", 3)

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Validate URL (basic check)
    # youtube-dl for later
    valid_services = [
        "spotify.com",
        "deezer.com",
        "tidal.com",
        "qobuz.com",
        "soundcloud.com",
        "youtube.com",
    ]
    if not any(service in url.lower() for service in valid_services):
        return jsonify({"error": "Unsupported service URL"}), 400

    metadata = extract_metadata_from_url(url)

    task_id = f"dl_{int(time.time() * 1000)}"
    task = {"id": task_id, "url": url, "quality": quality, "metadata": metadata}

    download_queue.put(task)

    return jsonify({"task_id": task_id, "status": "queued"})


@app.route("/api/status")
def get_all_status():
    return jsonify(
        {
            "active": active_downloads,
            "history": download_history[-20:],
            "queue_size": download_queue.qsize(),
        }
    )


@app.route("/api/config", methods=["GET", "POST"])
def config():
    if request.method == "GET":
        if os.path.exists(STREAMRIP_CONFIG):
            with open(STREAMRIP_CONFIG, "r") as f:
                return jsonify({"config": f.read()})
        return jsonify({"config": ""})

    elif request.method == "POST":
        data = request.json
        config_content = data.get("config", "")

        try:
            if os.path.exists(STREAMRIP_CONFIG):
                shutil.copy2(STREAMRIP_CONFIG, f"{STREAMRIP_CONFIG}.bak")

            os.makedirs(os.path.dirname(STREAMRIP_CONFIG), exist_ok=True)
            with open(STREAMRIP_CONFIG, "w") as f:
                f.write(config_content)

            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


async def search_music_async(query, search_type, source):
    """Async function to search using streamrip library"""
    logger.info("=" * 60)
    logger.info("SEARCH REQUEST (using streamrip library)")
    logger.info(f"Query: '{query}'")
    logger.info(f"Type: {search_type}")
    logger.info(f"Source: {source}")
    logger.info("=" * 60)

    try:
        # Get the client for this source
        client = await get_client(source)

        # Perform search using the client - returns list of page dicts
        pages = await client.search(search_type, query, limit=100)

        logger.info(f"Search returned {len(pages)} pages")

        # Extract items from pages
        # Page structure: {"albums": {"items": [...], "total": 123}, ...}
        # The key is the pluralized media type
        items_key = f"{search_type}s"
        search_results = []
        for page in pages:
            items = page.get(items_key, {}).get("items", [])
            search_results.extend(items)

        logger.info(f"Extracted {len(search_results)} items from pages")

        # Debug: log first result to see structure
        if search_results:
            logger.debug(f"First result structure: {search_results[0]}")
            logger.debug(
                f"First result keys: {search_results[0].keys() if isinstance(search_results[0], dict) else 'Not a dict'}"
            )
            # Log all fields to see what's available for year/date
            if isinstance(search_results[0], dict):
                logger.debug(f"All fields in first result:")
                for key, value in search_results[0].items():
                    logger.debug(f"  {key}: {value}")

        results = []
        for idx, item in enumerate(search_results):
            # Extract metadata based on type
            item_id = str(item.get("id", ""))

            # Build result with more fields available from library
            if search_type == "album":
                artist = (
                    item.get("artist", {}).get("name", "")
                    if isinstance(item.get("artist"), dict)
                    else item.get("artist", "")
                )
                title = item.get("title", "")
                desc = f"{title} by {artist}" if artist else title

                # Try multiple fields for year
                year = (
                    item.get("year", "")
                    or item.get("released_at", "")
                    or item.get("release_date_original", "")
                    or item.get("release_date", "")
                    or ""
                )
                # If year is a timestamp, extract just the year
                if year and isinstance(year, (int, float)):
                    from datetime import datetime

                    year = datetime.fromtimestamp(year).year
                elif year and isinstance(year, str) and len(year) >= 4:
                    year = year[:4]  # Extract first 4 chars (YYYY)

                result_item = {
                    "id": item_id,
                    "service": source,
                    "type": "album",
                    "artist": artist,
                    "title": title,
                    "desc": desc,
                    "url": construct_url(source, "album", item_id),
                    "album_art": item.get("cover", ""),
                    "year": str(year) if year else "",
                    "label": item.get("label", {}).get("name", "")
                    if isinstance(item.get("label"), dict)
                    else item.get("label", ""),
                    "track_count": item.get(
                        "tracks_count", item.get("track_count", "")
                    ),
                }
            elif search_type == "track":
                artist = (
                    item.get("artist", {}).get("name", "")
                    if isinstance(item.get("artist"), dict)
                    else item.get("artist", "")
                )
                title = item.get("title", "")
                album = (
                    item.get("album", {}).get("title", "")
                    if isinstance(item.get("album"), dict)
                    else ""
                )
                desc = f"{title} by {artist}" if artist else title

                result_item = {
                    "id": item_id,
                    "service": source,
                    "type": "track",
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "desc": desc,
                    "url": construct_url(source, "track", item_id),
                    "album_art": item.get("cover", ""),
                    "duration": item.get("duration", ""),
                }
            elif search_type == "artist":
                name = item.get("name", "")

                result_item = {
                    "id": item_id,
                    "service": source,
                    "type": "artist",
                    "artist": name,
                    "title": "",
                    "desc": name,
                    "url": construct_url(source, "artist", item_id),
                    "album_art": item.get("picture", item.get("image", "")),
                }
            elif search_type == "playlist":
                name = item.get("name", item.get("title", ""))
                creator = (
                    item.get("creator", {}).get("name", "")
                    if isinstance(item.get("creator"), dict)
                    else ""
                )
                desc = f"{name} by {creator}" if creator else name

                result_item = {
                    "id": item_id,
                    "service": source,
                    "type": "playlist",
                    "artist": creator,
                    "title": name,
                    "desc": desc,
                    "url": construct_url(source, "playlist", item_id),
                    "album_art": item.get("image", ""),
                    "track_count": item.get("tracks_count", ""),
                }
            else:
                # Fallback for unknown types
                result_item = {
                    "id": item_id,
                    "service": source,
                    "type": search_type,
                    "artist": "",
                    "title": str(item),
                    "desc": str(item),
                    "url": construct_url(source, search_type, item_id),
                    "album_art": "",
                }

            results.append(result_item)

            if idx < 3:  # Log first 3 results
                logger.debug(f"Result {idx + 1}: {result_item}")

        logger.info(f"Returning {len(results)} results")
        return results

    except Exception as e:
        logger.exception(f"Search error: {e}")
        raise


@app.route("/api/search", methods=["POST"])
def search_music():
    data = request.json
    query = data.get("query")
    search_type = data.get("type", "album")
    source = data.get("source", "qobuz")

    if not query:
        logger.warning("No query provided")
        return jsonify({"error": "Query required"}), 400

    if source == "soundcloud" and search_type in ["album", "artist"]:
        logger.debug(f"SoundCloud doesn't support {search_type} search")
        return jsonify(
            {
                "results": [],
                "query": query,
                "source": source,
                "total_count": 0,
                "message": f"SoundCloud does not support {search_type} searches. Try searching for tracks or playlists instead.",
            }
        )

    try:
        # Run the async search function
        results = run_async(search_music_async(query, search_type, source))

        return jsonify(
            {
                "results": results,
                "query": query,
                "source": source,
                "total_count": len(results),
            }
        )

    except Exception as e:
        logger.exception(f"Unexpected error during search: {e}")
        error_msg = str(e)

        # Provide helpful error messages
        if "login" in error_msg.lower() or "auth" in error_msg.lower():
            error_msg = f"Authentication failed for {source}. Check your credentials in the config."
        elif "secret" in error_msg.lower():
            error_msg = "Invalid app secrets. Update your config."

        return jsonify(
            {"error": error_msg, "debug_info": {"exception_type": type(e).__name__}}
        ), 500


@app.route("/api/album-art", methods=["GET"])
def get_album_art():
    source = request.args.get("source")
    media_type = request.args.get("type")
    item_id = request.args.get("id")

    if not all([source, media_type, item_id]):
        return jsonify({"error": "Missing parameters"}), 400

    # Todo: handle SoundCloud special case and get correct albums if possible
    if source == "soundcloud":
        if "|" in item_id:
            item_id = item_id.split("|")[0]
        elif "soundcloud:tracks:" in item_id:
            match = re.search(r"soundcloud:tracks:(\d+)", item_id)
            if match:
                item_id = match.group(1)

    cache_key = f"{source}_{media_type}_{item_id}"
    if cache_key in album_art_cache:
        return jsonify({"album_art": album_art_cache[cache_key]})

    try:
        if source == "qobuz":
            app_id = get_qobuz_app_id()
            album_art = fetch_single_album_art(item_id, media_type, app_id)
            if album_art:
                album_art_cache[cache_key] = album_art
                return jsonify({"album_art": album_art})
            return jsonify({"album_art": ""})

        elif source == "tidal":
            if media_type == "artist":
                album_art = f"https://resources.tidal.com/images/{item_id}/750x750.jpg"
            else:
                album_art = f"https://resources.tidal.com/images/{item_id}/320x320.jpg"

            if album_art:
                album_art_cache[cache_key] = album_art
                return jsonify({"album_art": album_art})
            return jsonify({"album_art": ""})

        elif source == "deezer":
            if media_type == "artist":
                try:
                    response = requests.get(
                        f"https://api.deezer.com/artist/{item_id}", timeout=3
                    )
                    if response.status_code == 200:
                        data = response.json()
                        album_art = data.get("picture_medium", data.get("picture", ""))
                        if album_art:
                            album_art_cache[cache_key] = album_art
                            return jsonify({"album_art": album_art})
                except:
                    pass
                return jsonify({"album_art": ""})
            else:
                album_art = f"https://api.deezer.com/{media_type}/{item_id}/image"
                if album_art:
                    album_art_cache[cache_key] = album_art
                    return jsonify({"album_art": album_art})
                return jsonify({"album_art": ""})

        elif source == "soundcloud":
            # SoundCloud doesn't provide easy access to artwork
            # Just return empty and let the frontend handle placeholders
            return jsonify({"album_art": ""})

        # Default return for unknown sources
        return jsonify({"album_art": ""})

    except Exception as e:
        logger.error(
            f"Error fetching album art for {source}/{media_type}/{item_id}: {e}"
        )
        return jsonify({"album_art": ""})


@app.route("/api/browse")
def browse_downloads():
    try:
        files = []
        for root, dirs, filenames in os.walk(DOWNLOAD_DIR):
            for filename in filenames:
                if filename.endswith((".mp3", ".flac", ".m4a", ".opus")):
                    filepath = os.path.join(root, filename)
                    rel_path = os.path.relpath(filepath, DOWNLOAD_DIR)
                    files.append(
                        {
                            "name": rel_path,
                            "size": os.path.getsize(filepath),
                            "modified": os.path.getmtime(filepath),
                        }
                    )

        files.sort(key=lambda x: x["modified"], reverse=True)
        return jsonify(files[:100])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def fetch_single_album_art(item_id, media_type, app_id):
    """Fetch album art for a single Qobuz item"""
    try:
        api_base = "https://www.qobuz.com/api.json/0.2"
        endpoints = {
            "album": f"{api_base}/album/get",
            "track": f"{api_base}/track/get",
            "artist": f"{api_base}/artist/get",
        }

        if media_type not in endpoints:
            return None

        params = {"app_id": app_id}
        if media_type == "album":
            params["album_id"] = item_id
        elif media_type == "track":
            params["track_id"] = item_id
        elif media_type == "artist":
            params["artist_id"] = item_id

        response = requests.get(endpoints[media_type], params=params, timeout=3)
        if response.status_code != 200:
            return None

        data = response.json()

        # Check if we got a minimal response (no actual data)
        if len(data) <= 2:
            logger.debug(f"Minimal data for {media_type} {item_id}: {data}")
            return None

        image_data = None

        if media_type == "album" and "image" in data:
            image_data = data["image"]
        elif media_type == "track" and "album" in data and "image" in data["album"]:
            image_data = data["album"]["image"]
        elif media_type == "artist":
            for field in ["image", "picture", "photo", "images", "thumbnail", "avatar"]:
                if field in data:
                    image_data = data[field]
                    break

            if not image_data:
                logger.debug(f"No image field found for artist {item_id}")
                return None

        if not image_data:
            return None

        if isinstance(image_data, dict):
            for size in ["extralarge", "large", "medium", "small", "thumbnail"]:
                if size in image_data and image_data[size]:
                    url = image_data[size]
                    if url and isinstance(url, str) and url.startswith("http"):
                        return url
        elif isinstance(image_data, str) and image_data.startswith("http"):
            return image_data
        elif isinstance(image_data, list) and len(image_data) > 0:
            for item in image_data:
                if isinstance(item, str) and item.startswith("http"):
                    return item

        return None

    except Exception as e:
        logger.error(f"Error fetching album art for {media_type} {item_id}: {e}")
        return None


def get_qobuz_app_id():
    try:
        if os.path.exists(STREAMRIP_CONFIG):
            with open(STREAMRIP_CONFIG, "r") as f:
                config_content = f.read()
                # logger.debug(f"Config file content: {config_content[:200]}...")  # First 200 chars

            app_id_match = re.search(
                r'app_id\s*=\s*["\']?([^"\'\n]+)["\']?', config_content
            )

            if app_id_match:
                app_id = app_id_match.group(1).strip()
                logger.debug(f"Found app_id in config: {app_id}")
                return app_id
            else:
                logger.debug("No app_id found in config, using fallback")

        # Return a known working app_id as fallback
        fallback_app_id = "950096963"
        logger.debug(f"Using fallback app_id: {fallback_app_id}")
        return fallback_app_id

    except Exception as e:
        logger.error(f"Error extracting app_id: {e}")
        return "950096963"


def construct_url(source, media_type, item_id):
    if not item_id:
        return ""

    url_patterns = {
        "qobuz": {
            "album": f"https://open.qobuz.com/album/{item_id}",
            "track": f"https://open.qobuz.com/track/{item_id}",
            "artist": f"https://open.qobuz.com/artist/{item_id}",
            "playlist": f"https://open.qobuz.com/playlist/{item_id}",
        },
        "tidal": {
            "album": f"https://tidal.com/browse/album/{item_id}",
            "track": f"https://tidal.com/browse/track/{item_id}",
            "artist": f"https://tidal.com/browse/artist/{item_id}",
            "playlist": f"https://tidal.com/browse/playlist/{item_id}",
        },
        "deezer": {
            "album": f"https://www.deezer.com/album/{item_id}",
            "track": f"https://www.deezer.com/track/{item_id}",
            "artist": f"https://www.deezer.com/artist/{item_id}",
            "playlist": f"https://www.deezer.com/playlist/{item_id}",
        },
        "soundcloud": {
            "track": f"https://soundcloud.com/{item_id}",
            "album": f"https://soundcloud.com/{item_id}",
            "playlist": f"https://soundcloud.com/{item_id}",
        },
    }

    if source in url_patterns and media_type in url_patterns[source]:
        return url_patterns[source][media_type]

    return f"https://open.{source}.com/{media_type}/{item_id}"


def extract_metadata_from_url(url):
    metadata = {
        "service": None,
        "type": None,
        "id": None,
        "title": None,
        "artist": None,
        "album_art": None,
    }

    try:
        if "spotify.com" in url:
            metadata["service"] = "spotify"
            match = re.search(r"/(album|track|playlist|artist)/([a-zA-Z0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                # Note: Spotify requires OAuth for metadata, so we can't easily fetch it

        elif "qobuz.com" in url:
            metadata["service"] = "qobuz"
            match = re.search(r"/(album|track|playlist|artist)/([0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                metadata.update(fetch_qobuz_metadata(metadata["id"], metadata["type"]))

        elif "tidal.com" in url:
            metadata["service"] = "tidal"
            match = re.search(r"/(album|track|playlist|artist)/([0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                metadata["album_art"] = (
                    f"https://resources.tidal.com/images/{metadata['id']}/320x320.jpg"
                )

        elif "deezer.com" in url:
            metadata["service"] = "deezer"
            match = re.search(r"/(album|track|playlist|artist)/([0-9]+)", url)
            if match:
                metadata["type"] = match.group(1)
                metadata["id"] = match.group(2)
                metadata.update(fetch_deezer_metadata(metadata["id"], metadata["type"]))

    except Exception as e:
        logger.error(f"Error extracting metadata from URL: {e}")

    return metadata


def fetch_qobuz_metadata(item_id, item_type):
    metadata = {}
    try:
        app_id = get_qobuz_app_id()
        api_base = "https://www.qobuz.com/api.json/0.2"

        if item_type == "album":
            response = requests.get(
                f"{api_base}/album/get",
                params={"album_id": item_id, "app_id": app_id},
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("artist", {}).get("name", "")
                if "image" in data:
                    for size in ["small", "medium", "large", "thumbnail"]:
                        if size in data["image"]:
                            metadata["album_art"] = data["image"][size]
                            break

        elif item_type == "track":
            response = requests.get(
                f"{api_base}/track/get",
                params={"track_id": item_id, "app_id": app_id},
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("performer", {}).get("name", "")
                album = data.get("album", {})
                if "image" in album:
                    for size in ["small", "medium", "large", "thumbnail"]:
                        if size in album["image"]:
                            metadata["album_art"] = album["image"][size]
                            break

    except Exception as e:
        logger.error(f"Error fetching Qobuz metadata: {e}")

    return metadata


def fetch_deezer_metadata(item_id, item_type):
    metadata = {}
    try:
        api_base = "https://api.deezer.com"

        if item_type == "album":
            response = requests.get(f"{api_base}/album/{item_id}", timeout=5)
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("artist", {}).get("name", "")
                metadata["album_art"] = data.get("cover_medium", "")

        elif item_type == "track":
            response = requests.get(f"{api_base}/track/{item_id}", timeout=5)
            if response.status_code == 200:
                data = response.json()
                metadata["title"] = data.get("title", "")
                metadata["artist"] = data.get("artist", {}).get("name", "")
                album = data.get("album", {})
                metadata["album_art"] = album.get("cover_medium", "")

    except Exception as e:
        logger.error(f"Error fetching Deezer metadata: {e}")

    return metadata


@app.route("/api/download-from-url", methods=["POST"])
def download_from_url():
    data = request.json
    url = data.get("url")
    quality = data.get("quality", 3)

    title = data.get("title")
    artist = data.get("artist")
    album_art = data.get("album_art")
    service = data.get("service")

    if not url:
        return jsonify({"error": "URL required"}), 400

    if title and artist and service:
        metadata = {
            "title": title,
            "artist": artist,
            "album_art": album_art,
            "service": service,
        }
    else:
        metadata = extract_metadata_from_url(url)

    task_id = f"dl_{int(time.time() * 1000)}"
    task = {"id": task_id, "url": url, "quality": quality, "metadata": metadata}

    download_queue.put(task)

    return jsonify({"task_id": task_id, "status": "queued", "metadata": metadata})


if __name__ == "__main__":
    logger.info("Starting Streamrip Web application...")
    logger.info(f"Config path: {STREAMRIP_CONFIG}")
    logger.info(f"Download directory: {DOWNLOAD_DIR}")
    logger.info(f"Max concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}")
    app.run(host="0.0.0.0", port=5000, debug=False)

if __name__ == "__main__":
    logger.info("Starting Streamrip Web application...")
    logger.info(f"Config path: {STREAMRIP_CONFIG}")
    logger.info(f"Download directory: {DOWNLOAD_DIR}")
    logger.info(f"Max concurrent downloads: {MAX_CONCURRENT_DOWNLOADS}")
    app.run(host="0.0.0.0", port=5000, debug=False)
    app.run(host="0.0.0.0", port=5000, debug=False)
