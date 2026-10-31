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
    "retail_only": "1",
    "target_via_chrome": "1",
    "pc_via_chrome": "0",
    "auto_cart": "0",
    "discover_minutes": "15",
    "discord_extras": "0",
    "target_fast": "1",
    "target_fast_gap": "3",
    "auto_checkout": "off",          # off / test / on
    "auto_checkout_daily_limit": "250",
    "feed_enabled": "0",
    "feed_bot_token": "",
    "feed_channel_id": "",
    "feed_pokemon_only": "1",
}

IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"
UNKNOWN = "unknown"
QUEUE = "queue"
BLOCKED = "blocked"
ERROR = "error"
MARKETPLACE = "marketplace"   # in stock, but only from a third-party seller
CONFIRMING = "confirming"     # one reading said in stock; double-checking before alerting

START_TIME = time.time()

VERSION = "1.5.0"
UPDATE_BASE = "https://raw.githubusercontent.com/dukearktouros/pokemon-restock-monitor/main/"
UPDATE_FILES = ["app.py", "dashboard.html", "README.md", "Start (Windows).bat", "Start (Mac).command",
                "chrome-helper/manifest.json", "chrome-helper/background.js"]
HELPER_MANIFEST = os.path.join(HERE, "chrome-helper", "manifest.json")


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
    """CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        keywords TEXT,
        max_price REAL
    )""",
    """CREATE TABLE IF NOT EXISTS discoveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL UNIQUE,
        category_id INTEGER,
        site TEXT NOT NULL,
        url TEXT NOT NULL,
        name TEXT,
        price REAL,
        image TEXT,
        seller TEXT,
        flag TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        found_at REAL NOT NULL
    )""",
]

SCHEMA.append("""CREATE TABLE IF NOT EXISTS checkouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        item_id INTEGER
    )""")
SCHEMA.append("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        retailer TEXT NOT NULL,
        order_number TEXT,
        total REAL,
        items TEXT,
        url TEXT
    )""")

MIGRATIONS = [("items", "category_id", "INTEGER"), ("items", "seller", "TEXT"), ("orders", "item_id", "INTEGER")]

STARTER_CATEGORIES = [
    ("Ascended Heroes", "pokemon ascended heroes"),
    ("Prismatic Evolutions", "pokemon prismatic evolutions"),
    ("30th Celebration", "pokemon 30th celebration"),
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
    for table, column, kind in MIGRATIONS:
        if column not in {r["name"] for r in query("PRAGMA table_info(%s)" % table)}:
            execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, kind))
    if not query("SELECT 1 FROM settings WHERE key = 'categories_seeded'"):
        for name, keywords in STARTER_CATEGORIES:
            execute("INSERT INTO categories(name, keywords) VALUES (?, ?)", (name, keywords))
        execute("INSERT INTO settings(key, value) VALUES ('categories_seeded', '1')")


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
                seller = candidate.get("seller")
                if isinstance(seller, dict) and seller.get("name"):
                    result.setdefault("seller", str(seller["name"]).strip())
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

def send_discord(settings, item, title, description, color, ping=None):
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
    if ping is None:
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

# A seller counts as the store itself when its name contains this.
FIRST_PARTY = {"target": "target", "pokemoncenter": "pok", "walmart": "walmart", "bestbuy": "best buy",
               "gamestop": "gamestop", "amazon": "amazon"}

CHECKOUT_URLS = {"target": "https://www.target.com/checkout", "pokemoncenter": "https://www.pokemoncenter.com/cart"}


def first_party(retailer, seller):
    key = FIRST_PARTY.get(retailer)
    return key is None or key in seller.lower()


def effective_max(item):
    """The item's own max price, or else its category's."""
    if item["max_price"] is not None:
        return item["max_price"]
    if item.get("category_id"):
        rows = query("SELECT max_price FROM categories WHERE id = ?", (item["category_id"],))
        if rows:
            return rows[0]["max_price"]
    return None


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


def record_failure(item, message, blocked):
    _failures[item["id"]] = _failures.get(item["id"], 0) + 1
    if message != item["last_error"]:
        log_event(item["id"], "error", message)
    execute("UPDATE items SET status = ?, last_error = ?, last_checked = ? WHERE id = ?",
            (BLOCKED if blocked else ERROR, message, time.time(), item["id"]))


_pending_confirm = {}   # item id -> how the first in-stock reading was made
_quick_ignored_until = {}  # item id -> time; quick checks proven wrong are ignored until then
_last_ignored_log = {}


def apply_result(item, result, settings, method="page"):
    """Save a successful check and send whatever alert it calls for.

    A new in-stock reading is never trusted on its own: it has to be confirmed by a second
    reading - for Target, by actually loading the product page - before anything alerts.
    """
    item_id = item["id"]
    _failures[item_id] = 0
    now = time.time()
    status = result["status"]
    seller = result.get("seller") or None
    if method == "api" and status == IN_STOCK and item["stock"] != IN_STOCK \
            and _quick_ignored_until.get(item_id, 0) > now:
        execute("UPDATE items SET last_checked = ? WHERE id = ?", (now, item_id))
        return
    first = _pending_confirm.pop(item_id, None)
    if status == IN_STOCK and item["stock"] != IN_STOCK:
        needs_page = item["retailer"] == "target" and chrome_handles(item, settings)
        if first is None or (needs_page and method != "page"):
            _pending_confirm[item_id] = method
            execute("UPDATE items SET status = ?, last_checked = ?, last_error = NULL WHERE id = ?",
                    (CONFIRMING, now, item_id))
            return
    elif first is not None:
        if first == "api":
            _quick_ignored_until[item_id] = now + 60
        if _last_ignored_log.get(item_id, 0) < now - 600:
            _last_ignored_log[item_id] = now
            log_event(item_id, "skipped", "Ignored an in-stock reading for %s that didn't hold up on the re-check"
                      % (item["name"] or item["url"]))
    third_party = (status == IN_STOCK and seller and not first_party(item["retailer"], seller)
                   and settings["retail_only"] == "1")
    if third_party:
        status = MARKETPLACE
    price = result.get("price") if result.get("price") is not None else item["price"]
    name = result.get("name") or item["name"]
    image = result.get("image") or item["image"]
    if status in (IN_STOCK, OUT_OF_STOCK):
        stock = status
    else:
        stock = OUT_OF_STOCK if status == MARKETPLACE else item["stock"]
    detail = result.get("detail")
    if seller:
        detail = (detail + " | " if detail else "") + "Sold by " + seller
    execute("""UPDATE items SET status = ?, stock = ?, detail = ?, price = ?, name = ?, image = ?, seller = ?,
               last_checked = ?, last_error = NULL,
               last_in_stock = CASE WHEN ? = 'in_stock' THEN ? ELSE last_in_stock END
               WHERE id = ?""",
            (status, stock, detail, price, name, image, seller, now, status, now, item_id))
    updated = get_item(item_id)
    label = name or item["url"]

    max_price = effective_max(item)
    was_too_pricey = max_price is not None and item["price"] is not None and item["price"] > max_price
    already_known = _quick_ignored_until.get(item_id, 0) > now or item["status"] == MARKETPLACE
    if third_party:
        _quick_ignored_until[item_id] = now + 600  # the quick check can't see sellers
    if third_party and not already_known:
        log_event(item_id, "skipped", "%s is only available from 3rd-party seller %s - no alert" % (label, seller))
    elif status == IN_STOCK and (item["stock"] != IN_STOCK or (was_too_pricey and price <= max_price)):
        if max_price is not None and price is not None and price > max_price:
            log_event(item_id, "skipped", "%s is in stock but $%.2f is above your max of $%.2f - no alert"
                      % (label, price, max_price))
        else:
            log_event(item_id, "in_stock", "IN STOCK: %s" % label)
            ok, message = send_discord(settings, updated, "IN STOCK: %s" % label,
                                       "Confirmed by two checks. " + (detail or ""), 0x22C55E)
            log_event(item_id, "alert" if ok else "error", message)
            open_in_chrome(updated)
    elif status == OUT_OF_STOCK and item["stock"] == IN_STOCK:
        log_event(item_id, "out_of_stock", "Sold out: %s" % label)
    elif status == QUEUE and item["status"] != QUEUE:
        log_event(item_id, "queue", "Queue is up: %s" % label)
        if settings["discord_extras"] == "1":
            ok, message = send_discord(settings, updated, "QUEUE IS LIVE: %s" % label,
                                       "Pokemon Center's waiting room is up - get in line now.", 0xF59E0B)
            log_event(item_id, "alert" if ok else "error", message)
        open_in_chrome(updated)


def schedule_next(item_id):
    if item_id in _pending_confirm:
        _next_due[item_id] = time.time() + 1  # re-check right away
        return
    item = get_item(item_id)
    if item:
        interval = interval_for(item, get_settings())
        backoff = min(2 ** _failures.get(item_id, 0), MAX_BACKOFF / interval)
        _next_due[item_id] = time.time() + interval * backoff * random.uniform(0.85, 1.15)


def run_check(item_id):
    try:
        item = get_item(item_id)
        if not item:
            return
        settings = get_settings()
        checker = CHECKERS.get(item["retailer"], check_page)
        try:
            result = checker(item, settings)
        except (Blocked, CheckError) as e:
            record_failure(item, str(e), isinstance(e, Blocked))
            return
        except Exception as e:
            record_failure(item, "Unexpected problem: %s" % e, False)
            return
        apply_result(item, result, settings)
    finally:
        schedule_next(item_id)
        with _inflight_lock:
            _inflight.discard(item_id)


def chrome_handles(item, settings):
    """True when this item is checked by the Chrome helper instead of from Python."""
    return ((item["retailer"] == "target" and settings["target_via_chrome"] == "1")
            or (item["retailer"] == "pokemoncenter" and settings["pc_via_chrome"] == "1"))


def scheduler():
    pool = ThreadPoolExecutor(max_workers=4)
    while True:
        now = time.time()
        settings = get_settings()
        for row in query("SELECT id, retailer FROM items WHERE enabled = 1"):
            if chrome_handles(row, settings):
                continue
            item_id = row["id"]
            with _inflight_lock:
                if item_id in _inflight or _next_due.get(item_id, 0) > now:
                    continue
                _inflight.add(item_id)
            pool.submit(run_check, item_id)
        time.sleep(1)


# ---------------------------------------------------------------- chrome helper

_ext = {"last_seen": 0, "captcha_alerted": False, "open": []}
_search_due = {}

SEARCH_URLS = {"target": "https://www.target.com/s?searchTerm=%s&sortBy=newest",
               "pokemoncenter": "https://www.pokemoncenter.com/search/%s"}


def ext_connected():
    return time.time() - _ext["last_seen"] < 75


def helper_version():
    try:
        with open(HELPER_MANIFEST) as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


TAX_SHIPPING_ALLOWANCE = (1.12, 10.0)   # checkout total may be up to max price x 1.12 + $10


def spent_today():
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return query("SELECT COALESCE(SUM(total), 0) AS spent FROM orders WHERE ts >= ?", (midnight,))[0]["spent"]


def auto_checkout_plan(item, settings):
    """Return (max checkout total, None) if the bot may buy this item by itself, else (None, reason)."""
    mode = settings["auto_checkout"]
    if mode not in ("test", "on"):
        return None, "auto checkout is off"
    if item["retailer"] != "target":
        return None, "auto checkout only works on Target"
    max_price = effective_max(item)
    if max_price is None:
        return None, "set a max price for this item or its category to let the bot buy it"
    if query("SELECT 1 FROM orders WHERE item_id = ?", (item["id"],)):
        return None, "already bought once"
    if query("SELECT 1 FROM checkouts WHERE item_id = ? AND ts > ?", (item["id"], time.time() - 600)):
        return None, "a checkout for this item was already tried in the last 10 minutes"
    max_total = round(max_price * TAX_SHIPPING_ALLOWANCE[0] + TAX_SHIPPING_ALLOWANCE[1], 2)
    try:
        limit = float(settings["auto_checkout_daily_limit"])
    except ValueError:
        limit = 0
    if mode == "on" and spent_today() + max_total > limit:
        return None, "it would go over your daily limit of $%.2f" % limit
    return max_total, None


def open_in_chrome(item):
    """Ask the Chrome helper to open an item that just came in stock (and buy it, if allowed)."""
    if not ext_connected():
        return
    settings = get_settings()
    job = {"item_id": item["id"], "url": item["url"], "checkout_url": CHECKOUT_URLS.get(item["retailer"])}
    max_total, reason = auto_checkout_plan(item, settings)
    if max_total is not None:
        job.update(auto_checkout=settings["auto_checkout"], max_total=max_total)
        execute("INSERT INTO checkouts(ts, item_id) VALUES (?, ?)", (time.time(), item["id"]))
        log_event(item["id"], "checkout", "%s checkout started for %s (total limit $%.2f)"
                  % ("Test" if settings["auto_checkout"] == "test" else "Auto", item["name"] or item["url"], max_total))
    elif settings["auto_checkout"] != "off":
        log_event(item["id"], "skipped", "Not buying automatically: %s" % reason)
    _ext["open"].append(job)


def ext_checkout(body):
    """The helper finished (or gave up on) an automatic checkout."""
    item = get_item(int(body.get("item_id") or 0))
    label = (item["name"] or item["url"]) if item else "an item"
    outcome = str(body.get("outcome") or "")
    message = str(body.get("message") or "")[:300]
    total = parse_price(body.get("total"))
    if outcome == "placed":
        log_event(item and item["id"], "checkout", "Place order clicked for %s - waiting for the confirmation page" % label)
        return
    if outcome == "test_ok":
        log_event(item and item["id"], "checkout", "Test checkout for %s worked: total $%s, stopped before Place "
                  "order. Remove it from your Target cart now so a real checkout starts from an empty cart."
                  % (label, "%.2f" % total if total is not None else "?"))
        return
    log_event(item and item["id"], "error", "Checkout stopped for %s: %s" % (label, message))
    send_discord(get_settings(), {"url": body.get("url") or (item and item["url"]), "retailer": "target",
                                  "price": total, "image": item and item["image"]},
                 "CHECKOUT NEEDS YOU: %s" % label, message + "\nThe checkout page is open in Chrome.", 0xEF4444)


def due_searches(settings, now, limit):
    try:
        minutes = float(settings["discover_minutes"])
    except ValueError:
        minutes = 15
    if minutes <= 0:
        return []
    jobs = []
    for category in query("SELECT * FROM categories ORDER BY id"):
        for keyword in [k.strip() for k in (category["keywords"] or "").split(",") if k.strip()]:
            for site, pattern in SEARCH_URLS.items():
                key = "%d|%s|%s" % (category["id"], site, keyword)
                if len(jobs) >= limit or _search_due.get(key, 0) > now:
                    continue
                _search_due[key] = now + max(minutes, 5) * 60 * random.uniform(0.9, 1.1)
                jobs.append({"key": key, "site": site, "url": pattern % urllib.parse.quote(keyword)})
    return jobs


def ext_jobs(limit=4):
    _ext["last_seen"] = now = time.time()
    settings = get_settings()
    checks, fast = [], []
    items = query("SELECT id, url, retailer FROM items WHERE enabled = 1 ORDER BY id")
    # Re-checks that confirm an in-stock reading jump the queue.
    items.sort(key=lambda i: i["id"] not in _pending_confirm)
    for item in items:
        if not chrome_handles(item, settings):
            continue
        if item["retailer"] == "target" and settings["target_fast"] == "1" and item["id"] not in _pending_confirm:
            fast.append({"id": item["id"], "tcin": target_tcin(item["url"])})
        if len(checks) < limit and _next_due.get(item["id"], 0) <= now:
            checks.append(item)
            _next_due[item["id"]] = now + 60  # reserved; the report schedules the real next check
    searches = due_searches(settings, now, 1 if checks else limit)
    opens, _ext["open"] = _ext["open"], []
    try:
        gap = max(2.0, float(settings["target_fast_gap"]))
    except ValueError:
        gap = 3.0
    return {"checks": checks, "searches": searches, "open": opens,
            "fast": {"items": fast, "gap": gap, "store_id": settings["target_store_id"].strip(),
                     "zip": settings["target_zip"].strip(), "key": TARGET_API_KEY},
            "auto_cart": settings["auto_cart"] == "1", "ext_version": helper_version()}


def ext_report(body):
    item = get_item(int(body.get("id") or 0))
    if not item:
        return
    settings = get_settings()
    try:
        if body.get("captcha"):
            record_failure(item, "%s wants to know you're human - open Chrome, click the pinned tab and complete "
                                 "the check" % RETAILER_NAMES.get(item["retailer"], "The store"), True)
            if not _ext["captcha_alerted"] and settings["discord_extras"] == "1":
                _ext["captcha_alerted"] = True
                send_discord(settings, item, "Human check needed in Chrome",
                             "Open Chrome on your computer, click the pinned tab and complete the check so "
                             "monitoring can continue.", 0xF59E0B)
            return
        _ext["captcha_alerted"] = False
        method = "api" if body.get("method") == "api" else "page"
        if body.get("error"):
            if method == "api":
                return  # the quick check failed; the regular page check still covers this item
            record_failure(item, str(body["error"])[:300], False)
            return
        if body.get("queue"):
            result = {"status": QUEUE, "detail": "A waiting-room queue is up - a drop is probably live!"}
        else:
            status = body.get("status")
            result = {"status": status if status in (IN_STOCK, OUT_OF_STOCK) else UNKNOWN,
                      "price": parse_price(body.get("price")),
                      "name": (str(body.get("name") or "").strip()[:300] or None),
                      "image": body.get("image") if re.match(r"^https?://", str(body.get("image") or "")) else None,
                      "seller": (str(body.get("seller") or "").strip()[:80] or None),
                      "detail": body.get("detail")}
        apply_result(item, result, settings, method)
    finally:
        schedule_next(item["id"])


def listing_key(url):
    if detect_retailer(url) == "target":
        tcin = target_tcin(url)
        return "target:" + tcin if tcin else None
    match = re.search(r"pokemoncenter\.com/product/([\w-]+)", url)
    return "pokemoncenter:" + match.group(1) if match else None


def ext_discover(body):
    parts = str(body.get("key") or "").split("|", 2)
    if len(parts) != 3:
        return
    category_id, site, keyword = int(parts[0]), parts[1], parts[2]
    categories = query("SELECT * FROM categories WHERE id = ?", (category_id,))
    if not categories:
        return
    category = categories[0]
    if body.get("captcha"):
        log_event(None, "error", "Discover: %s wants a human check - open Chrome's pinned tab"
                  % RETAILER_NAMES.get(site, site))
        return
    if body.get("error"):
        return
    seeded_key = "seeded|" + body["key"]
    first_run = not query("SELECT 1 FROM settings WHERE key = ?", (seeded_key,))
    watching = {listing_key(row["url"]) for row in query("SELECT url FROM items")}
    settings = get_settings()
    found = 0
    for r in (body.get("results") or [])[:80]:
        url = str(r.get("url") or "").split("?")[0]
        key = listing_key(url)
        name = str(r.get("name") or "").strip()[:300]
        if not key or "pok" not in name.lower():
            continue
        found += 1
        price = parse_price(r.get("price"))
        seller = str(r.get("seller") or "").strip()[:80] or None
        image = r.get("image") if re.match(r"^https?://", str(r.get("image") or "")) else None
        flag = None
        if seller and not first_party(site, seller):
            flag = "3rd-party seller: %s" % seller
        elif category["max_price"] is not None and price is not None and price > category["max_price"]:
            flag = "Above your max price of $%.2f" % category["max_price"]
        if query("SELECT 1 FROM discoveries WHERE key = ?", (key,)):
            execute("UPDATE discoveries SET price = COALESCE(?, price), image = COALESCE(image, ?), flag = ? "
                    "WHERE key = ?", (price, image, flag, key))
            continue
        status = "added" if key in watching else "new"
        execute("""INSERT INTO discoveries(key, category_id, site, url, name, price, image, seller, flag, status,
                   found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, category_id, site, url, name, price, image, seller, flag, status, time.time()))
        if status == "new" and not first_run and not flag:
            log_event(None, "discover", "New listing (%s): %s" % (category["name"], name))
            if settings["discord_extras"] == "1":
                send_discord(settings, {"url": url, "retailer": site, "price": price, "image": image},
                             "NEW LISTING: %s" % name,
                             'Found on %s while searching "%s" (%s). Add it to your watchlist from the dashboard.'
                             % (RETAILER_NAMES.get(site, site), keyword, category["name"]), 0x8B5CF6, ping=False)
    if first_run:
        execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, '1')", (seeded_key,))
        log_event(None, "discover", 'Discover: first search of %s for "%s" found %d listings (no alerts the '
                  'first time)' % (RETAILER_NAMES.get(site, site), keyword, found))


def ext_order(body):
    """The helper saw an order-confirmation page: record it and tell Discord."""
    retailer = detect_retailer(str(body.get("url") or ""))
    number = re.sub(r"[^A-Za-z0-9-]", "", str(body.get("order_number") or ""))[:40] or None
    if number and query("SELECT 1 FROM orders WHERE order_number = ? AND retailer = ?", (number, retailer)):
        return
    text = str(body.get("text") or "").lower()
    bought = [i for i in query("SELECT * FROM items")
              if i["name"] and i["name"].lower()[:30] in text]
    by_bot = get_item(int(body.get("item_id") or 0))
    if by_bot and all(i["id"] != by_bot["id"] for i in bought):
        bought.insert(0, by_bot)
    names = ", ".join(i["name"] for i in bought)[:500] or None
    total = parse_price(body.get("total"))
    execute("INSERT INTO orders(ts, retailer, order_number, total, items, url, item_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), retailer, number, total, names, str(body.get("url") or "")[:300],
             bought[0]["id"] if bought else None))
    for i in bought:  # bought it - stop watching so it's never bought twice
        execute("UPDATE items SET enabled = 0 WHERE id = ?", (i["id"],))
    label = RETAILER_NAMES.get(retailer, "Store")
    log_event(bought[0]["id"] if bought else None, "order",
              "ORDER PLACED at %s%s" % (label, " - order #%s" % number if number else ""))
    lines = ["Order #: %s" % (number or "see your email")]
    if total is not None:
        lines.append("Total: $%.2f" % total)
    lines.append("Items: %s" % (names or "check the order page"))
    send_discord(get_settings(), {"url": body.get("url"), "retailer": retailer, "price": total,
                                  "image": bought[0]["image"] if bought else None},
                 "ORDER PLACED at %s" % label, "\n".join(lines), 0x10B981)


# ---------------------------------------------------------------- categories

def save_category(body, category_id=None):
    name = str(body.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Give the category a name")
    keywords = ", ".join(k.strip() for k in str(body.get("keywords") or "").split(",") if k.strip())[:300]
    raw = str(body.get("max_price") or "").strip()
    max_price = parse_price(raw) if raw else None
    if category_id is None:
        return execute("INSERT INTO categories(name, keywords, max_price) VALUES (?, ?, ?)", (name, keywords, max_price))
    execute("UPDATE categories SET name = ?, keywords = ?, max_price = ? WHERE id = ?",
            (name, keywords, max_price, category_id))
    return category_id


def delete_category(category_id):
    execute("UPDATE items SET category_id = NULL WHERE category_id = ?", (category_id,))
    execute("DELETE FROM discoveries WHERE category_id = ? AND status != 'added'", (category_id,))
    execute("DELETE FROM categories WHERE id = ?", (category_id,))


def _category_id(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if query("SELECT 1 FROM categories WHERE id = ?", (value,)) else None


# ---------------------------------------------------------------- web dashboard

def state():
    events = query("""SELECT e.*, i.name AS item_name FROM events e
                      LEFT JOIN items i ON i.id = e.item_id ORDER BY e.id DESC LIMIT 80""")
    items = query("SELECT * FROM items ORDER BY created_at DESC")
    settings = get_settings()
    for item in items:
        item["next_check"] = _next_due.get(item["id"])
        item["via_chrome"] = chrome_handles(item, settings)
    return {"items": items, "events": events, "settings": settings,
            "categories": query("SELECT * FROM categories ORDER BY id"),
            "discoveries": query("SELECT * FROM discoveries WHERE status = 'new' ORDER BY found_at DESC LIMIT 60"),
            "orders": query("SELECT * FROM orders ORDER BY ts DESC LIMIT 30"),
            "chrome": {"connected": ext_connected(), "last_seen": _ext["last_seen"] or None},
            "feed": {"status": _feed["status"]},
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
    key = listing_key(url)
    if key and any(listing_key(row["url"]) == key for row in query("SELECT url FROM items")):
        raise ValueError("You're already watching that product")
    item_id = execute("INSERT INTO items(url, retailer, max_price, category_id, created_at) VALUES (?, ?, ?, ?, ?)",
                      (url, retailer, max_price, _category_id(body.get("category_id")), time.time()))
    if key:
        execute("UPDATE discoveries SET status = 'added' WHERE key = ?", (key,))
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
        path = os.path.join(HERE, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = path + ".new"
        with open(temp, "wb") as f:
            f.write(data)
        if os.path.exists(path):
            os.chmod(temp, os.stat(path).st_mode)
        os.replace(temp, path)


def download_missing_files():
    """Fetch files added in newer versions that an older updater didn't know to download."""
    for name in UPDATE_FILES:
        path = os.path.join(HERE, *name.split("/"))
        if os.path.exists(path):
            continue
        try:
            request = urllib.request.Request(UPDATE_BASE + urllib.parse.quote(name), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:
            print("Couldn't download %s: %s" % (name, e))


def relaunch(extra_args):
    """Start the freshly installed version in this same window."""
    args = [a for a in sys.argv[1:] if a not in extra_args] + extra_args
    sys.exit(subprocess.call([sys.executable, os.path.join(HERE, "app.py")] + args))


# ---------------------------------------------------------------- discord feed
#
# Reads a channel in YOUR server that follows another server's alert channel (Discord's
# "Follow" button copies their alerts into your channel). A bot you create in your own server
# reads it; every product link is treated like a first "in stock" reading and confirmed by a
# real check before anything alerts or gets bought.

FEED_CATEGORY = "Discord feed"
_feed = {"status": "off", "last_id": None, "seen": {}, "empty": 0}


class FeedWait(Exception):
    def __init__(self, seconds):
        Exception.__init__(self, "rate limited")
        self.seconds = seconds


def discord_api(token, path):
    request = urllib.request.Request("https://discord.com/api/v10" + path, headers={
        "Authorization": "Bot " + token, "User-Agent": "RestockMonitor (local, %s)" % VERSION})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                raise FeedWait(float(json.loads(e.read()).get("retry_after", 5)))
            except ValueError:
                raise FeedWait(5)
        reasons = {401: "the bot token is wrong",
                   403: "the bot can't read that channel - check it was invited with Read Message History",
                   404: "that channel ID wasn't found"}
        raise RuntimeError(reasons.get(e.code, "Discord answered HTTP %d" % e.code))


def feed_links(message):
    """Pull Target / Pokemon Center product links out of a message, its embeds and buttons."""
    texts = [message.get("content") or ""]
    for embed in message.get("embeds") or []:
        texts += [embed.get("title") or "", embed.get("url") or "", embed.get("description") or ""]
        texts += ["%s %s" % (f.get("name", ""), f.get("value", "")) for f in embed.get("fields") or []]
    for row in message.get("components") or []:
        texts += [c.get("url") or "" for c in row.get("components") or []]
    blob = "\n".join(texts)
    urls = []
    for url in re.findall(r"https?://[^\s)>\]\"'|]+", blob):
        if listing_key(url):
            urls.append(url.split("?")[0])
    for tcin in re.findall(r"\bTCIN\W{0,4}(\d{6,10})\b", blob, re.I):
        urls.append("https://www.target.com/p/-/A-%s" % tcin)
    return blob, urls


def feed_category_id():
    rows = query("SELECT id FROM categories WHERE name = ?", (FEED_CATEGORY,))
    if rows:
        return rows[0]["id"]
    return execute("INSERT INTO categories(name, keywords, max_price) VALUES (?, '', 60)", (FEED_CATEGORY,))


def handle_feed_message(message, settings):
    blob, urls = feed_links(message)
    if not blob.strip():
        _feed["empty"] += 1
    if not urls or (settings["feed_pokemon_only"] == "1" and "pok" not in blob.lower()):
        return
    now = time.time()
    for url in urls:
        key = listing_key(url)
        if _feed["seen"].get(key, 0) > now - 120:
            continue
        _feed["seen"][key] = now
        item = next((i for i in query("SELECT * FROM items") if listing_key(i["url"]) == key), None)
        if item is None:
            item = get_item(add_item({"url": url, "category_id": feed_category_id()}))
        if not item["enabled"]:
            continue  # paused (for example, already bought)
        if item["stock"] == IN_STOCK:
            continue
        _pending_confirm[item["id"]] = "feed"
        execute("UPDATE items SET status = ? WHERE id = ?", (CONFIRMING, item["id"]))
        _next_due[item["id"]] = 0
        log_event(item["id"], "feed", "Discord feed says %s is live - checking it now" % (item["name"] or url))


def poll_feed(token, channel, settings):
    if _feed["last_id"] is None:  # start from the newest message; don't replay old alerts
        latest = discord_api(token, "/channels/%s/messages?limit=1" % channel)
        _feed["last_id"] = latest[0]["id"] if latest else "0"
    else:
        messages = discord_api(token, "/channels/%s/messages?after=%s&limit=50" % (channel, _feed["last_id"]))
        for message in sorted(messages, key=lambda m: int(m["id"])):
            _feed["last_id"] = message["id"]
            handle_feed_message(message, settings)
    _feed["status"] = ("connected, but messages arrive empty - turn on Message Content Intent for your bot"
                       if _feed["empty"] >= 3 else "connected")


def feed_poller():
    while True:
        settings = get_settings()
        token, channel = settings["feed_bot_token"].strip(), settings["feed_channel_id"].strip()
        if settings["feed_enabled"] != "1" or not token or not channel:
            _feed.update(status="off", last_id=None, empty=0)
            time.sleep(3)
            continue
        delay = 2
        try:
            poll_feed(token, channel, settings)
        except FeedWait as wait:
            delay = wait.seconds + 0.5
        except Exception as e:
            _feed["status"] = "error: %s" % e
            delay = 15
        time.sleep(delay)


def start_test_checkout(item):
    """Run a test checkout right now (stops before Place order), so you can try it before a drop."""
    if item["retailer"] != "target":
        raise ValueError("Test checkout works on Target items")
    if not ext_connected():
        raise ValueError("The Chrome helper isn't connected - open Chrome")
    max_price = effective_max(item) if effective_max(item) is not None else item["price"]
    if max_price is None:
        raise ValueError("Set a max price for this item first")
    max_total = round(max_price * TAX_SHIPPING_ALLOWANCE[0] + TAX_SHIPPING_ALLOWANCE[1], 2)
    _ext["open"].append({"item_id": item["id"], "url": item["url"], "checkout_url": CHECKOUT_URLS["target"],
                         "auto_checkout": "test", "max_total": max_total})
    log_event(item["id"], "checkout", "Test checkout started for %s - watch Chrome" % (item["name"] or item["url"]))


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
        elif path == "/api/ext/jobs":
            query_args = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                limit = max(1, min(4, int(query_args.get("max", ["4"])[0])))
            except ValueError:
                limit = 4
            self._json(ext_jobs(limit))
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
            if path == "/api/ext/report":
                ext_report(body)
                self._json({"ok": True})
                return
            if path == "/api/ext/checkout":
                ext_checkout(body)
                self._json({"ok": True})
                return
            if path == "/api/ext/order":
                ext_order(body)
                self._json({"ok": True})
                return
            if path == "/api/ext/discover":
                ext_discover(body)
                self._json({"ok": True})
                return
            if path == "/api/categories":
                self._json({"id": save_category(body)})
                return
            match = re.match(r"^/api/categories/(\d+)$", path)
            if match:
                self._json({"id": save_category(body, int(match.group(1)))})
                return
            if path == "/api/discoveries/dismiss-all":
                execute("UPDATE discoveries SET status = 'dismissed' WHERE status = 'new'")
                self._json({"ok": True})
                return
            match = re.match(r"^/api/discoveries/(\d+)/(add|dismiss)$", path)
            if match:
                rows = query("SELECT * FROM discoveries WHERE id = ?", (int(match.group(1)),))
                if not rows:
                    self._json({"error": "Not found"}, 404)
                    return
                found = rows[0]
                if match.group(2) == "add":
                    add_item({"url": found["url"], "category_id": found["category_id"],
                              "max_price": body.get("max_price")})
                    execute("UPDATE items SET name = COALESCE(name, ?), image = COALESCE(image, ?) WHERE url = ?",
                            (found["name"], found["image"], found["url"]))
                execute("UPDATE discoveries SET status = ? WHERE id = ?",
                        ("added" if match.group(2) == "add" else "dismissed", found["id"]))
                self._json({"ok": True})
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
            match = re.match(r"^/api/items/(\d+)/(check|toggle|edit|test-checkout)$", path)
            if match:
                item_id, action = int(match.group(1)), match.group(2)
                if not get_item(item_id):
                    self._json({"error": "Item not found"}, 404)
                    return
                if action == "test-checkout":
                    start_test_checkout(get_item(item_id))
                elif action == "check":
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
                    if "category_id" in body:
                        execute("UPDATE items SET category_id = ? WHERE id = ?",
                                (_category_id(body.get("category_id")), item_id))
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
        path = urllib.parse.urlparse(self.path).path
        match = re.match(r"^/api/categories/(\d+)$", path)
        if match:
            delete_category(int(match.group(1)))
            self._json({"ok": True})
            return
        match = re.match(r"^/api/items/(\d+)$", path)
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
    download_missing_files()
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
    threading.Thread(target=feed_poller, daemon=True).start()
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
