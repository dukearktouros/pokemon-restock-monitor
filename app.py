#!/usr/bin/env python3
"""
Pokemon restock monitor.

Watches product pages on Target, Pokemon Center (and most other stores), sends a
Discord alert the moment something comes back in stock, and serves a dashboard
at http://localhost:8080. Uses only Python's standard library - nothing to install.
"""

import html
import json
import os
import random
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "monitor.db")
DASHBOARD_PATH = os.path.join(HERE, "dashboard.html")
PORT = int(os.environ.get("PORT", "8080"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Public key that Target's own website uses for its product API.
TARGET_API_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
TARGET_API = "https://redsky.target.com/redsky_aggregations/v1/web/"

MIN_INTERVAL = 15        # never check one item more often than this (seconds)
MAX_BACKOFF = 15 * 60    # when a site errors or blocks us, slow down up to this
HOST_GAP = 2.0           # minimum seconds between two requests to the same site

DEFAULT_SETTINGS = {
    "discord_webhook": "",
    "ping_everyone": "1",
    "interval_target": "30",
    "interval_pokemoncenter": "60",
    "interval_other": "60",
    "target_zip": "",
    "target_store_id": "",
}

IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"
UNKNOWN = "unknown"
QUEUE = "queue"
BLOCKED = "blocked"
ERROR = "error"

START_TIME = time.time()

VERSION = "1.1.0"
UPDATE_BASE = "https://raw.githubusercontent.com/dukearktouros/pokemon-restock-monitor/main/"
UPDATE_FILES = ["app.py", "dashboard.html", "README.md", "Start (Windows).bat", "Start (Mac).command"]


# ---------------------------------------------------------------- database

_db_lock = threading.Lock()

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        retailer TEXT NOT NULL,
        name TEXT,
        image TEXT,
        max_price REAL,
        enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'unknown',
        stock TEXT NOT NULL DEFAULT 'unknown',
        detail TEXT,
        price REAL,
        last_checked REAL,
        last_error TEXT,
        last_in_stock REAL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        message TEXT NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
]


def _run(sql, args=(), fetch=False):
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql, args)
            result = [dict(r) for r in cur.fetchall()] if fetch else cur.lastrowid
            conn.commit()
            return result
        finally:
            conn.close()


def query(sql, args=()):
    return _run(sql, args, fetch=True)


def execute(sql, args=()):
    return _run(sql, args)


def init_db():
    for statement in SCHEMA:
        execute(statement)


def get_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in query("SELECT key, value FROM settings")})
    return settings


def save_settings(new):
    for key, value in new.items():
        if key in DEFAULT_SETTINGS:
            execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value).strip()))


def get_item(item_id):
    rows = query("SELECT * FROM items WHERE id = ?", (item_id,))
    return rows[0] if rows else None


def log_event(item_id, kind, message):
    execute("INSERT INTO events(item_id, ts, kind, message) VALUES (?, ?, ?, ?)",
            (item_id, time.time(), kind, message))
    execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)")


# ---------------------------------------------------------------- fetching

class Blocked(Exception):
    """The site refused the request (rate limit / bot protection). We back off."""


class CheckError(Exception):
    """Anything else that went wrong while checking an item."""


_host_locks = {}
_host_last = {}
_host_guard = threading.Lock()


def _wait_for_host(host):
    """Keep requests to the same site at least HOST_GAP seconds apart."""
    with _host_guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _host_last.get(host, 0) + HOST_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.time()


def fetch(url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"):
    host = urllib.parse.urlparse(url).netloc
    _wait_for_host(host)
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        snippet = e.read()[:2000].decode("utf-8", errors="replace").strip()
        if "px-cdn" in snippet or "perimeterx" in snippet.lower() or "blockScript" in snippet:
            raise Blocked("%s's bot protection (PerimeterX) blocked the check - slowing down and retrying later"
                          % host)
        if e.code in (401, 403, 429, 435):
            raise Blocked("%s answered HTTP %d (rate-limited or blocked) - slowing down" % (host, e.code))
        snippet = snippet[:150]
        raise CheckError("HTTP %d from %s %s" % (e.code, host, snippet))
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            raise CheckError("SSL certificates aren't installed for Python. On a Mac, open your "
                             "Applications > Python folder and double-click 'Install Certificates.command'.")
        raise CheckError("Network error: %s" % reason)
    except (socket.timeout, TimeoutError):
        raise CheckError("%s took too long to answer" % host)


def fetch_json(url):
    text = fetch(url, accept="application/json")
    try:
        return json.loads(text)
    except ValueError:
        raise CheckError("Expected JSON but got something else from %s" % urllib.parse.urlparse(url).netloc)


def find_key(data, key):
    """Return the first value stored under `key` anywhere in nested JSON."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        values = data.values()
    elif isinstance(data, list):
        values = data
    else:
        return None
    for value in values:
        found = find_key(value, key)
        if found is not None:
            return found
    return None


def parse_price(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    return float(match.group(0).replace(",", "")) if match else None


# ---------------------------------------------------------------- retailers

def detect_retailer(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    for domain, name in (("target.com", "target"), ("pokemoncenter.com", "pokemoncenter"),
                         ("walmart.com", "walmart"), ("bestbuy.com", "bestbuy"),
                         ("gamestop.com", "gamestop"), ("amazon.com", "amazon")):
        if host == domain or host.endswith("." + domain):
            return name
    return "other"


def target_tcin(url):
    match = re.search(r"/A-(\d+)", url) or re.search(r"[?&](?:tcin|preselect)=(\d+)", url)
    return match.group(1) if match else None


TARGET_IN = {"IN_STOCK", "LIMITED_STOCK", "PRE_ORDER_SELLABLE", "BACKORDER_SELLABLE"}


def _pretty(status):
    return status.replace("_", " ").capitalize() if status else "n/a"


def check_target(item, settings):
    tcin = target_tcin(item["url"])
    if not tcin:
        raise CheckError("Couldn't find the item number (the 'A-12345678' part) in this Target link")
    my_store = settings["target_store_id"].strip()
    store = my_store or "3991"  # any store works for prices; set yours to also watch pickup stock
    base = {"key": TARGET_API_KEY, "tcin": tcin, "channel": "WEB",
            "store_id": store, "pricing_store_id": store}
    result = {"status": UNKNOWN}

    info = fetch_json(TARGET_API + "pdp_client_v1?" + urllib.parse.urlencode(base))
    product = find_key(info, "product") or {}
    title = find_key(product.get("item") or {}, "title")
    result["name"] = html.unescape(title) if title else None
    result["image"] = find_key(product, "primary_image_url")
    price = product.get("price") or {}
    result["price"] = parse_price(price.get("current_retail") or price.get("formatted_current_price"))

    params = dict(base, scheduled_delivery_store_id=store, required_store_id=store,
                  has_required_store_id="true")
    if settings["target_zip"].strip():
        params["zip"] = settings["target_zip"].strip()
    stock = fetch_json(TARGET_API + "pdp_fulfillment_v1?" + urllib.parse.urlencode(params))
    fulfillment = find_key(stock, "fulfillment") or {}
    shipping = (fulfillment.get("shipping_options") or {}).get("availability_status")
    pickup = None
    if my_store:
        for option in fulfillment.get("store_options") or []:
            pickup = (option.get("order_pickup") or {}).get("availability_status") or pickup

    statuses = [s for s in (shipping, pickup) if s]
    if not statuses:
        result["detail"] = "Target didn't report availability for this item"
        return result
    result["status"] = IN_STOCK if any(s in TARGET_IN for s in statuses) else OUT_OF_STOCK
    parts = ["Shipping: " + _pretty(shipping)]
    if my_store:
        parts.append("Pickup at store %s: %s" % (my_store, _pretty(pickup)))
    result["detail"] = " | ".join(parts)
    return result


SCHEMA_IN = {"instock", "instoreonly", "limitedavailability", "onlineonly", "preorder", "presale", "backorder"}
SCHEMA_OUT = {"outofstock", "soldout", "discontinued"}


def _iter_jsonld(text):
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                kind = node.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if "Product" in kinds or "ProductGroup" in kinds:
                    yield node
                stack.extend(v for k, v in node.items() if k in ("@graph", "hasVariant", "mainEntity"))


def _availability(value):
    word = str(value or "").rstrip("/").split("/")[-1].lower().replace("_", "").replace(" ", "")
    if word in SCHEMA_IN:
        return IN_STOCK
    if word in SCHEMA_OUT:
        return OUT_OF_STOCK
    return None


def _meta(text, prop):
    match = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)' % re.escape(prop), text, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop), text, re.I))
    return html.unescape(match.group(1)) if match else None


def looks_like_queue(text):
    lower = text.lower()
    return "queue-it" in lower or "you are now in line" in lower or "virtual queue" in lower or "waiting room" in lower


def looks_blocked(text):
    lower = text[:20000].lower()
    return ("_incapsula_resource" in lower or "incapsula incident" in lower
            or "pardon our interruption" in lower or "request unsuccessful" in lower
            or ("access denied" in lower and len(text) < 5000)
            or ("captcha" in lower and len(text) < 20000))


def parse_product_page(text):
    """Read stock, price, name and image from a product page's structured data."""
    result = {"status": UNKNOWN}
    for product in _iter_jsonld(text):
        result.setdefault("name", product.get("name"))
        image = product.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        result.setdefault("image", image)
        offers = product.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            nested = offer.get("offers")
            candidates = (nested if isinstance(nested, list) else [nested]) if nested else [offer]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                status = _availability(candidate.get("availability"))
                price = parse_price(candidate.get("price") or candidate.get("lowPrice"))
                if price is not None and result.get("price") is None:
                    result["price"] = price
                if status == IN_STOCK or (status and result["status"] == UNKNOWN):
                    result["status"] = status
        if result["status"] == IN_STOCK:
            break

    if result["status"] == UNKNOWN:
        match = re.search(r'"availability"\s*:\s*"([^"]+)"', text)
        if match:
            result["status"] = _availability(match.group(1)) or UNKNOWN
    if result["status"] == UNKNOWN:
        match = re.search(r'"(?:inStock|isInStock|in_stock)"\s*:\s*(true|false)', text)
        if match:
            result["status"] = IN_STOCK if match.group(1) == "true" else OUT_OF_STOCK

    result["name"] = result.get("name") or _meta(text, "og:title")
    result["image"] = result.get("image") or _meta(text, "og:image")
    if result.get("price") is None:
        result["price"] = parse_price(_meta(text, "product:price:amount") or _meta(text, "og:price:amount"))
    if result["name"]:
        result["name"] = html.unescape(result["name"]).strip()
    result["detail"] = None if result["status"] != UNKNOWN else "Couldn't find stock info on this page"
    return result


def check_page(item, settings):
    text = fetch(item["url"])
    if looks_like_queue(text):
        return {"status": QUEUE, "detail": "A waiting-room queue is up - a drop is probably live!"}
    if looks_blocked(text):
        raise Blocked("The site showed a bot-check page instead of the product - slowing down")
    return parse_product_page(text)


CHECKERS = {"target": check_target}


def fill_details(item_id):
    """Grab the product's name and main picture from its page (once, not on every check)."""
    item = get_item(item_id)
    if not item or (item["image"] and item["name"]):
        return
    try:
        text = fetch(item["url"])
    except Exception:
        return
    if looks_blocked(text) or looks_like_queue(text):
        return
    page = parse_product_page(text)
    name = page.get("name")
    if name:
        name = re.sub(r"\s*[:|-]\s*(Target|Pok.mon Center.*)$", "", name).strip() or None
    execute("UPDATE items SET name = COALESCE(name, ?), image = COALESCE(image, ?) WHERE id = ?",
            (name, page.get("image"), item_id))


def fill_missing_details():
    for row in query("SELECT id FROM items WHERE image IS NULL OR name IS NULL"):
        fill_details(row["id"])


# ---------------------------------------------------------------- alerts

def send_discord(settings, item, title, description, color):
    hook = settings["discord_webhook"].strip()
    if not hook:
        return False, "No Discord webhook set"
    if not re.match(r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/", hook):
        return False, "That doesn't look like a Discord webhook link"
    embed = {
        "title": title[:250],
        "url": item.get("url"),
        "description": (description or "")[:2000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [
            {"name": "Store", "value": RETAILER_NAMES.get(item.get("retailer"), "Other"), "inline": True},
            {"name": "Price", "value": "$%.2f" % item["price"] if item.get("price") is not None else "?", "inline": True},
        ],
    }
    if item.get("image"):
        embed["thumbnail"] = {"url": item["image"]}
    ping = settings["ping_everyone"] == "1"
    payload = {"content": "@everyone" if ping else "", "embeds": [embed],
               "allowed_mentions": {"parse": ["everyone"] if ping else []}}
    request = urllib.request.Request(hook, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        urllib.request.urlopen(request, timeout=15).close()
        return True, "Discord alert sent"
    except urllib.error.HTTPError as e:
        return False, "Discord rejected the alert (HTTP %d)" % e.code
    except Exception as e:  # network trouble shouldn't kill the monitor
        return False, "Couldn't reach Discord: %s" % e


RETAILER_NAMES = {"target": "Target", "pokemoncenter": "Pokemon Center", "walmart": "Walmart",
                  "bestbuy": "Best Buy", "gamestop": "GameStop", "amazon": "Amazon", "other": "Other"}


# ---------------------------------------------------------------- monitor loop

_next_due = {}
_failures = {}
_inflight = set()
_inflight_lock = threading.Lock()


def interval_for(item, settings):
    key = "interval_" + item["retailer"]
    raw = settings.get(key, settings["interval_other"])
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 60
    return max(MIN_INTERVAL, seconds)


def run_check(item_id):
    try:
        item = get_item(item_id)
        if not item:
            return
        settings = get_settings()
        checker = CHECKERS.get(item["retailer"], check_page)
        now = time.time()
        try:
            result = checker(item, settings)
        except (Blocked, CheckError) as e:
            blocked = isinstance(e, Blocked)
            _failures[item_id] = _failures.get(item_id, 0) + 1
            message = str(e)
            if message != item["last_error"]:
                log_event(item_id, "error", message)
            execute("UPDATE items SET status = ?, last_error = ?, last_checked = ? WHERE id = ?",
                    (BLOCKED if blocked else ERROR, message, now, item_id))
            return
        except Exception as e:
            _failures[item_id] = _failures.get(item_id, 0) + 1
            message = "Unexpected problem: %s" % e
            if message != item["last_error"]:
                log_event(item_id, "error", message)
            execute("UPDATE items SET status = ?, last_error = ?, last_checked = ? WHERE id = ?",
                    (ERROR, message, now, item_id))
            return

        _failures[item_id] = 0
        status = result["status"]
        price = result.get("price") if result.get("price") is not None else item["price"]
        name = result.get("name") or item["name"]
        image = result.get("image") or item["image"]
        stock = status if status in (IN_STOCK, OUT_OF_STOCK) else item["stock"]
        execute("""UPDATE items SET status = ?, stock = ?, detail = ?, price = ?, name = ?, image = ?,
                   last_checked = ?, last_error = NULL,
                   last_in_stock = CASE WHEN ? = 'in_stock' THEN ? ELSE last_in_stock END
                   WHERE id = ?""",
                (status, stock, result.get("detail"), price, name, image, now, status, now, item_id))
        updated = get_item(item_id)
        label = name or item["url"]

        max_price = item["max_price"]
        was_too_pricey = max_price is not None and item["price"] is not None and item["price"] > max_price
        if status == IN_STOCK and (item["stock"] != IN_STOCK or (was_too_pricey and price <= max_price)):
            if max_price is not None and price is not None and price > max_price:
                log_event(item_id, "in_stock", "%s is in stock but $%.2f is above your max of $%.2f - no alert"
                          % (label, price, item["max_price"]))
            else:
                log_event(item_id, "in_stock", "IN STOCK: %s" % label)
                ok, message = send_discord(settings, updated, "IN STOCK: %s" % label,
                                           result.get("detail") or "Go go go!", 0x22C55E)
                log_event(item_id, "alert" if ok else "error", message)
        elif status == OUT_OF_STOCK and item["stock"] == IN_STOCK:
            log_event(item_id, "out_of_stock", "Sold out: %s" % label)
        elif status == QUEUE and item["status"] != QUEUE:
            log_event(item_id, "queue", "Queue is up: %s" % label)
            ok, message = send_discord(settings, updated, "QUEUE IS LIVE: %s" % label,
                                       "Pokemon Center's waiting room is up - get in line now.", 0xF59E0B)
            log_event(item_id, "alert" if ok else "error", message)
    finally:
        item = get_item(item_id)
        if item:
            settings = get_settings()
            backoff = min(2 ** _failures.get(item_id, 0), MAX_BACKOFF / interval_for(item, settings))
            _next_due[item_id] = time.time() + interval_for(item, settings) * backoff * random.uniform(0.85, 1.15)
        with _inflight_lock:
            _inflight.discard(item_id)


def scheduler():
    pool = ThreadPoolExecutor(max_workers=4)
    while True:
        now = time.time()
        for row in query("SELECT id FROM items WHERE enabled = 1"):
            item_id = row["id"]
            with _inflight_lock:
                if item_id in _inflight or _next_due.get(item_id, 0) > now:
                    continue
                _inflight.add(item_id)
            pool.submit(run_check, item_id)
        time.sleep(1)


# ---------------------------------------------------------------- web dashboard

def state():
    events = query("""SELECT e.*, i.name AS item_name FROM events e
                      LEFT JOIN items i ON i.id = e.item_id ORDER BY e.id DESC LIMIT 80""")
    items = query("SELECT * FROM items ORDER BY created_at DESC")
    for item in items:
        item["next_check"] = _next_due.get(item["id"])
    return {"items": items, "events": events, "settings": get_settings(),
            "now": time.time(), "started": START_TIME,
            "version": VERSION, "update_available": _update["latest"]}


def add_item(body):
    url = str(body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("Paste a full product link starting with https://")
    retailer = detect_retailer(url)
    if retailer == "target" and not target_tcin(url):
        raise ValueError("That Target link has no item number - open the product page and copy its link")
    max_price = parse_price(body.get("max_price")) if str(body.get("max_price") or "").strip() else None
    item_id = execute("INSERT INTO items(url, retailer, max_price, created_at) VALUES (?, ?, ?, ?)",
                      (url, retailer, max_price, time.time()))
    log_event(item_id, "added", "Now watching %s" % url)
    _next_due[item_id] = 0
    threading.Thread(target=fill_details, args=(item_id,), daemon=True).start()
    return item_id


# ---------------------------------------------------------------- self-update

_update = {"latest": None}
_restart_after_shutdown = False
_server = None


def _version_tuple(version):
    return tuple(int(n) for n in re.findall(r"\d+", version))


def check_for_update():
    """Return the newer version number published on GitHub, or None."""
    try:
        request = urllib.request.Request(UPDATE_BASE + "version.txt", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=10) as response:
            latest = response.read().decode("utf-8").strip()
    except Exception:
        return None
    newer = latest if _version_tuple(latest) > _version_tuple(VERSION) else None
    _update["latest"] = newer
    return newer


def install_update():
    downloaded = {}
    for name in UPDATE_FILES:
        request = urllib.request.Request(UPDATE_BASE + urllib.parse.quote(name), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            downloaded[name] = response.read()
    compile(downloaded["app.py"], "app.py", "exec")  # never install a broken download
    for name, data in downloaded.items():
        path = os.path.join(HERE, name)
        temp = path + ".new"
        with open(temp, "wb") as f:
            f.write(data)
        if os.path.exists(path):
            os.chmod(temp, os.stat(path).st_mode)
        os.replace(temp, path)


def relaunch(extra_args):
    """Start the freshly installed version in this same window."""
    args = [a for a in sys.argv[1:] if a not in extra_args] + extra_args
    sys.exit(subprocess.call([sys.executable, os.path.join(HERE, "app.py")] + args))


def update_checker():
    while True:
        check_for_update()
        time.sleep(6 * 3600)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def _local_only(self):
        # Refuse requests from other websites (DNS rebinding / cross-site form posts).
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1"):
            self._json({"error": "forbidden"}, 403)
            return False
        if self.command != "GET" and self.headers.get("X-Dashboard") != "1":
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(DASHBOARD_PATH, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._local_only():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/items":
                self._json({"id": add_item(body)})
                return
            if path == "/api/settings":
                save_settings(body)
                self._json({"ok": True})
                return
            if path == "/api/update":
                global _restart_after_shutdown
                try:
                    install_update()
                except Exception as e:
                    self._json({"error": "Update failed: %s" % e}, 500)
                    return
                _restart_after_shutdown = True
                self._json({"ok": True})
                threading.Thread(target=_server.shutdown, daemon=True).start()
                return
            if path == "/api/test-alert":
                fake = {"url": "https://www.pokemoncenter.com/", "retailer": "pokemoncenter",
                        "price": 49.99, "image": None}
                ok, message = send_discord(get_settings(), fake, "Test alert - your monitor is connected!",
                                           "This is what a restock alert will look like.", 0x3B82F6)
                self._json({"ok": ok, "message": message}, 200 if ok else 400)
                return
            match = re.match(r"^/api/items/(\d+)/(check|toggle|edit)$", path)
            if match:
                item_id, action = int(match.group(1)), match.group(2)
                if not get_item(item_id):
                    self._json({"error": "Item not found"}, 404)
                    return
                if action == "check":
                    _failures[item_id] = 0
                    _next_due[item_id] = 0
                elif action == "toggle":
                    execute("UPDATE items SET enabled = 1 - enabled WHERE id = ?", (item_id,))
                    _next_due[item_id] = 0
                else:
                    if "max_price" in body:
                        raw = str(body.get("max_price") or "").strip()
                        execute("UPDATE items SET max_price = ? WHERE id = ?",
                                (parse_price(raw) if raw else None, item_id))
                    if "image" in body:
                        image = str(body.get("image") or "").strip()
                        if image and not re.match(r"^https?://", image):
                            raise ValueError("Picture link must start with https://")
                        execute("UPDATE items SET image = ? WHERE id = ?", (image or None, item_id))
                self._json({"ok": True})
                return
            self._json({"error": "not found"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)

    def do_DELETE(self):
        if not self._local_only():
            return
        match = re.match(r"^/api/items/(\d+)$", urllib.parse.urlparse(self.path).path)
        if not match:
            self._json({"error": "not found"}, 404)
            return
        item_id = int(match.group(1))
        execute("DELETE FROM items WHERE id = ?", (item_id,))
        execute("DELETE FROM events WHERE item_id = ?", (item_id,))
        _next_due.pop(item_id, None)
        self._json({"ok": True})


def main():
    global _server
    if "--no-update" not in sys.argv:
        latest = check_for_update()
        if latest:
            print("Updating to version %s..." % latest)
            try:
                install_update()
                print("Updated! Restarting...")
                relaunch(["--no-update"])
            except Exception as e:
                print("Update failed (%s) - starting the current version." % e)
    init_db()
    url = "http://localhost:%d" % PORT
    try:
        server = _server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("Port %d is busy - the monitor is probably already running. Opening the dashboard..." % PORT)
        webbrowser.open(url)
        sys.exit(1)
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=fill_missing_details, daemon=True).start()
    threading.Thread(target=update_checker, daemon=True).start()
    print("=" * 56)
    print("  Pokemon Restock Monitor v%s is running" % VERSION)
    print("  Dashboard: %s" % url)
    print("  Keep this window open. Close it (or press Ctrl+C) to stop.")
    print("=" * 56)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        return
    server.server_close()
    if _restart_after_shutdown:
        print("Update installed - restarting...")
        relaunch(["--no-update", "--no-browser"])


if __name__ == "__main__":
    main()
