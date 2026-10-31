// Restock Monitor Helper
//
// Asks the monitor running on this computer (http://localhost:8080) what to look at and
// checks it inside *your* Chrome:
//   - quick checks: asks Target's stock service from a pinned target.com tab, the same
//     request target.com itself makes, one request every few seconds;
//   - page checks: loads the product page in a pinned tab and reads the buy box. Every
//     in-stock reading is confirmed this way before the monitor alerts you.
// It never solves bot checks and never places an order. When you place one yourself, it
// spots the confirmation page and reports it so you get a Discord message.

const MONITOR = "http://localhost:8080";
const PAGE_TIMEOUT = 25000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let busy = false;
let fastIndex = 0;
let fastErrors = 0;
let fastPausedUntil = 0;

function start() {
  chrome.alarms.create("tick", { periodInMinutes: 0.5 });
  loop();
}
chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === "tick") loop(); });
chrome.action.onClicked.addListener(() => chrome.tabs.create({ url: MONITOR }));

async function api(path, body) {
  const res = await fetch(MONITOR + path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

function setBadge(text, color = "#d97706") {
  chrome.action.setBadgeText({ text });
  if (text) chrome.action.setBadgeBackgroundColor({ color });
}

async function loop() {
  if (busy) return;
  busy = true;
  try {
    while (true) {
      let jobs;
      try {
        jobs = await api("/api/ext/jobs?max=1&v=" + chrome.runtime.getManifest().version);
      } catch (e) {
        setBadge("off", "#6b7280"); // monitor isn't running
        return;
      }
      if (jobs.ext_version && jobs.ext_version !== chrome.runtime.getManifest().version) {
        chrome.runtime.reload(); // the monitor updated our files on disk
        return;
      }
      for (const job of jobs.open || []) await openForCheckout(job, jobs.auto_cart);

      let didWork = false;
      const fast = jobs.fast || { items: [] };
      if (fast.items.length && Date.now() > fastPausedUntil) {
        didWork = true;
        const item = fast.items[fastIndex++ % fast.items.length];
        const result = await runInTab("fastTabId", "https://www.target.com/", targetStock,
          [item.tcin, fast.store_id, fast.zip, fast.key], false);
        await api("/api/ext/report", { id: item.id, method: "api", ...result });
        if (result.error) {
          if (++fastErrors >= 3) fastPausedUntil = Date.now() + 10 * 60 * 1000; // fall back to page checks
        } else {
          fastErrors = 0;
        }
      }

      const job = (jobs.checks || [])[0] ? { ...jobs.checks[0], kind: "check" }
        : (jobs.searches || [])[0] ? { ...jobs.searches[0], kind: "search" } : null;
      if (job) {
        didWork = true;
        const result = job.kind === "check"
          ? await runInTab("watcherTabId", job.url, extractProduct, [job.retailer], true)
          : await runInTab("watcherTabId", job.url, extractSearch, [job.site], true);
        if (job.kind === "check") await api("/api/ext/report", { id: job.id, method: "page", ...result });
        else await api("/api/ext/discover", { key: job.key, ...result });
        if (result.captcha) {
          setBadge("!"); // wait for you to complete the check in the pinned tab
          return;
        }
      }
      setBadge(Date.now() < fastPausedUntil ? "slow" : "", "#6b7280");
      if (!didWork) {
        // Nothing due: ask again shortly so a Discord-feed alert is picked up within seconds.
        // (Touching extension storage keeps Chrome from putting the helper to sleep.)
        await chrome.storage.session.get("watcherTabId");
        await sleep(1500);
        continue;
      }
      await sleep(fast.items.length ? fast.gap * 1000 : 1500 + Math.random() * 1500);
    }
  } catch (e) {
    console.warn("Restock helper:", e);
  } finally {
    busy = false;
  }
}

// Pinned tabs we reuse: one that loads product pages, one that stays on target.com for quick checks.
async function pinnedTab(name) {
  const stored = (await chrome.storage.session.get(name))[name];
  if (stored) {
    const tab = await chrome.tabs.get(stored).catch(() => null);
    if (tab) return tab;
  }
  const tab = await chrome.tabs.create({ url: "about:blank", active: false, pinned: true });
  await chrome.storage.session.set({ [name]: tab.id });
  return tab;
}

async function waitForLoad(tabId) {
  const end = Date.now() + PAGE_TIMEOUT;
  await sleep(800);
  while (Date.now() < end) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return false;
    if (tab.status === "complete" && tab.url && !tab.url.startsWith("about:")) return true;
    await sleep(300);
  }
  return false;
}

async function runInTab(name, url, func, args, navigate) {
  try {
    const tab = await pinnedTab(name);
    if (navigate || !(tab.url || "").startsWith(url)) {
      await chrome.tabs.update(tab.id, { url });
      await waitForLoad(tab.id);
    }
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
    return (res && res.result) || { error: "The page didn't return anything" };
  } catch (e) {
    return { error: "Couldn't read the page in Chrome: " + e.message };
  }
}

const checkoutTabs = new Map(); // tab id -> watched item id, for orders the bot placed

async function openForCheckout(job, autoCart) {
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  chrome.windows.update(tab.windowId, { focused: true });
  await sleep(300);
  await chrome.tabs.update(tab.id, { url: job.url });
  const auto = job.auto_checkout === "on" || job.auto_checkout === "test";
  if ((!autoCart && !auto) || !job.checkout_url) return;
  await waitForLoad(tab.id);
  const report = (outcome, message, extra = {}) =>
    api("/api/ext/checkout", { item_id: job.item_id, url: job.url, outcome, message, ...extra }).catch(() => {});
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: addToCart });
    if (!(res && res.result && res.result.added)) {
      if (auto) await report("failed", "Couldn't find an Add to cart button for this item");
      return;
    }
    await chrome.tabs.update(tab.id, { url: job.checkout_url });
    if (!auto) return; // stop at checkout; you press "Place order"
    await waitForLoad(tab.id);
    const [step] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: placeOrder,
      args: [job.max_total, job.auto_checkout === "test"] });
    const result = (step && step.result) || { outcome: "failed", message: "The checkout page didn't respond" };
    if (result.outcome === "placed") checkoutTabs.set(tab.id, job.item_id);
    await report(result.outcome, result.message, { total: result.total });
  } catch (e) {
    if (auto) await report("failed", "Checkout error: " + e.message);
  }
}

// Spot order-confirmation pages on Target / Pokemon Center and report the order.
const reportedOrders = new Set();
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  if (!/^https:\/\/www\.(target|pokemoncenter)\.com\/.*(confirm|thank|receipt)/i.test(tab.url)) return;
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: readOrder });
    const order = res && res.result;
    if (!order || !order.confirmed) return;
    const key = order.order_number || tab.url;
    if (reportedOrders.has(key)) return;
    reportedOrders.add(key);
    await api("/api/ext/order", { url: tab.url, item_id: checkoutTabs.get(tabId), ...order });
  } catch (e) {
    console.warn("Restock helper: couldn't read order page", e);
  }
});

// ---------------------------------------------------------------------------
// The functions below run inside the store's page, so each must be self-contained.

async function targetStock(tcin, storeId, zip, key) {
  const IN = ["IN_STOCK", "LIMITED_STOCK", "PRE_ORDER_SELLABLE", "BACKORDER_SELLABLE"];
  const OUT = ["OUT_OF_STOCK", "UNAVAILABLE", "PRE_ORDER_UNSELLABLE", "DISCONTINUED", "NOT_SOLD_IN_STORE"];
  const find = (data, name) => {
    const stack = [data];
    while (stack.length) {
      const node = stack.pop();
      if (node && typeof node === "object") {
        if (!Array.isArray(node) && name in node) return node[name];
        stack.push(...Object.values(node));
      }
    }
    return undefined;
  };
  const store = storeId || "3991";
  const params = new URLSearchParams({ key, tcin, channel: "WEB", store_id: store, pricing_store_id: store,
    scheduled_delivery_store_id: store, required_store_id: store, has_required_store_id: "true" });
  if (zip) params.set("zip", zip);
  try {
    const res = await fetch("https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1?" + params,
      { credentials: "include" });
    if (!res.ok) return { error: "Target's stock service answered HTTP " + res.status };
    const fulfillment = find(await res.json(), "fulfillment") || {};
    const shipping = (fulfillment.shipping_options || {}).availability_status;
    const pickup = storeId ? ((fulfillment.store_options || [])[0]?.order_pickup || {}).availability_status : undefined;
    const statuses = [shipping, pickup].filter(Boolean);
    if (!statuses.length) return { status: "unknown", detail: "Target didn't report availability" };
    const status = statuses.some((s) => IN.includes(s)) ? "in_stock"
      : statuses.every((s) => OUT.includes(s)) ? "out_of_stock" : "unknown";
    return { status, detail: "Quick check - shipping: " + (shipping || "n/a") + (storeId ? ", pickup: " + (pickup || "n/a") : "") };
  } catch (e) {
    return { error: "Quick check failed: " + e.message };
  }
}

async function extractProduct(retailer) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const BUY = /^(add to cart|add to bag|ship it|pick it up|deliver it|pre-?order( now)?|add for (shipping|pickup|delivery))\b/i;
  // Target's own buy-box buttons. Anything else that says "Add to cart" (recommended products,
  // "frequently bought together" carousels...) belongs to a different product and is ignored.
  const TARGET_BUY = "[data-test='shippingButton'], [data-test='orderPickupButton'], [data-test='scheduledDeliveryButton'], " +
    "[data-test='preorderButton'], [data-test='preorderButtonShipping'], [data-test='preorderButtonDisabled']";
  const NOT_BUY_BOX = "[data-test*='carousel' i], [data-test*='recommend' i], [data-test*='bundle' i], [class*='carousel' i], " +
    "[class*='Carousel'], [aria-roledescription='carousel'], [data-component-type*='recommend' i]";
  const text = () => (document.body && document.body.innerText) || "";
  const isCaptcha = () =>
    !!document.querySelector("#px-captcha, iframe[src*='captcha'], iframe[src*='_Incapsula_Resource']") ||
    /press\s*(&|and)\s*hold|verify you are (a )?human|pardon our interruption/i.test(text().slice(0, 3000));
  const isQueue = () => /queue-it|you are now in line|virtual queue|waiting room/i.test(location.href + " " + text().slice(0, 3000));
  const title = () => document.querySelector("h1[data-test='product-title'], h1");
  const usable = (b) => !b.disabled && b.getAttribute("aria-disabled") !== "true" && b.offsetParent !== null;

  function buyButtons() {
    const h1 = title();
    if (!h1) return [];
    // Only trust buttons in the product's own buy box: near the title, above the fold, and
    // not inside a carousel of other products.
    const top = h1.getBoundingClientRect().top + window.scrollY;
    const inBuyBox = (b) => {
      if (b.closest(NOT_BUY_BOX)) return false;
      const y = b.getBoundingClientRect().top + window.scrollY;
      return y > top - 200 && y < top + 1100;
    };
    const tagged = [...document.querySelectorAll(TARGET_BUY)];
    if (tagged.length) return tagged.filter((b) => usable(b) && !/disabled/i.test(b.getAttribute("data-test")) && inBuyBox(b));
    return [...document.querySelectorAll("button")].filter((b) =>
      BUY.test((b.innerText || "").trim()) && usable(b) && inBuyBox(b));
  }
  const soldOut = () => {
    const h1 = title();
    let box = h1;
    for (let i = 0; box && i < 6; i++) box = box.parentElement;
    return /\b(sold out|out of stock|currently unavailable|not available|coming soon)\b/i.test((box || document.body).innerText || "");
  };

  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (isCaptcha()) return { captcha: true };
    if (isQueue()) return { queue: true };
    if (title() && (buyButtons().length || soldOut())) break;
    await sleep(400);
  }
  await sleep(800); // let the buy box finish rendering

  let ld = {};
  for (const s of document.querySelectorAll("script[type='application/ld+json']")) {
    try {
      const stack = [JSON.parse(s.textContent)];
      while (stack.length) {
        const node = stack.pop();
        if (Array.isArray(node)) { stack.push(...node); continue; }
        if (!node || typeof node !== "object") continue;
        if ([].concat(node["@type"]).includes("Product")) {
          const offers = [].concat(node.offers || []).flatMap((o) => [].concat(o.offers || o));
          const offer = offers[0] || {};
          ld = {
            name: node.name, image: [].concat(node.image || [])[0],
            availability: String(offer.availability || "").split("/").pop(),
            price: offer.price || offer.lowPrice, seller: offer.seller && offer.seller.name,
          };
        }
        if (node["@graph"]) stack.push(node["@graph"]);
      }
    } catch (e) { /* ignore broken JSON-LD */ }
  }

  const meta = (p) => (document.querySelector(`meta[property='${p}'], meta[name='${p}']`) || {}).content;
  const buttons = buyButtons();
  const priceEl = document.querySelector("[data-test='product-price']");
  const priceText = (priceEl && priceEl.innerText) || ld.price || meta("product:price:amount") || null;
  const sellerMatch = text().match(/sold (?:and|&) shipped by\s*:?\s*([^\n|]{2,60})/i);
  const seller = (sellerMatch && sellerMatch[1].trim()) || ld.seller || null;

  // In stock only when the product's own buy button is there and clickable. Structured data
  // alone is not enough (it is often stale), unless the page has no buttons we recognise at all.
  let status = "unknown";
  if (buttons.length) status = "in_stock";
  else if (title()) status = "out_of_stock";
  if (!buttons.length && !document.querySelector(TARGET_BUY) && retailer !== "target" &&
      /^(InStock|LimitedAvailability|PreOrder|OnlineOnly)$/i.test(ld.availability || "") && !soldOut()) status = "in_stock";

  return {
    status,
    name: (title() && title().innerText.trim()) || ld.name || meta("og:title") || null,
    image: meta("og:image") || ld.image || null,
    price: priceText ? String(priceText) : null,
    seller,
    detail: status === "in_stock" ? "Page check - buy button: " + (buttons.map((b) => b.innerText.trim()).slice(0, 2).join(", ") || "listed in stock") : null,
  };
}

async function extractSearch(site) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pattern = site === "target" ? /\/-\/A-(\d+)/ : /\/product\/([\w-]+)/;
  const text = () => (document.body && document.body.innerText) || "";
  const isCaptcha = () =>
    !!document.querySelector("#px-captcha, iframe[src*='captcha'], iframe[src*='_Incapsula_Resource']") ||
    /press\s*(&|and)\s*hold|verify you are (a )?human|pardon our interruption/i.test(text().slice(0, 3000));
  const links = () => [...document.querySelectorAll("a[href]")].filter((a) => pattern.test(a.href));

  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (isCaptcha()) return { captcha: true };
    if (links().length) break;
    await sleep(500);
  }
  for (let i = 0; i < 5; i++) { window.scrollBy(0, 1400); await sleep(700); } // load lazy product cards

  const idsIn = (el) => new Set([...el.querySelectorAll("a[href]")].map((a) => (a.href.match(pattern) || [])[1]).filter(Boolean));
  const found = new Map();
  for (const a of links()) {
    const id = a.href.match(pattern)[1];
    // Climb to the product card: the biggest element that still holds only this one product.
    let card = a;
    while (card.parentElement && idsIn(card.parentElement).size <= 1 && card.parentElement !== document.body) card = card.parentElement;
    const cardText = card.innerText || "";
    const img = card.querySelector("img");
    const entry = found.get(id) || { url: a.href.split("?")[0] };
    entry.name = entry.name || (a.innerText || "").trim() || a.getAttribute("aria-label") || (img && img.alt) || "";
    entry.image = entry.image || (img && (img.currentSrc || img.src)) || null;
    entry.price = entry.price || ((cardText.match(/\$\s?\d[\d,]*\.\d{2}/) || [])[0]) || null;
    const seller = cardText.match(/sold (?:and|&) shipped by\s*:?\s*([^\n|]{2,60})/i);
    entry.seller = entry.seller || (seller && seller[1].trim()) || null;
    found.set(id, entry);
  }
  return { results: [...found.values()] };
}

async function addToCart() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const BUY = /^(add to cart|add to bag|ship it|pre-?order( now)?|add for shipping)\b/i;
  const TARGET_BUY = "[data-test='shippingButton'], [data-test='preorderButton'], [data-test='preorderButtonShipping']";
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const usable = (b) => !b.disabled && b.getAttribute("aria-disabled") !== "true";
    const button = [...document.querySelectorAll(TARGET_BUY)].find(usable) ||
      [...document.querySelectorAll("button")].find((b) => BUY.test((b.innerText || "").trim()) && usable(b) &&
        !b.closest("[data-test*='carousel' i], [class*='carousel' i], [aria-roledescription='carousel']"));
    if (button) {
      button.click();
      await sleep(2500);
      return { added: true };
    }
    await sleep(400);
  }
  return { added: false };
}

// Runs on Target's checkout page. Presses "Place order" only when everything checks out;
// otherwise it stops and says why, leaving the page open for you.
async function placeOrder(maxTotal, testOnly) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const text = () => (document.body && document.body.innerText) || "";
  const visible = (el) => el && el.offsetParent !== null;
  const placeButton = () => [...document.querySelectorAll("button")].find((b) =>
    /^place (your )?order$/i.test((b.innerText || "").trim()) && !b.disabled && b.getAttribute("aria-disabled") !== "true");
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline && !placeButton()) {
    if (/\/login|signin/i.test(location.href) || document.querySelector("input[type='password']")) {
      return { outcome: "failed", message: "Target wants you to sign in. Sign in to Target in this Chrome so the bot can check out." };
    }
    if (document.querySelector("#px-captcha") || /press\s*(&|and)\s*hold|verify you are (a )?human/i.test(text().slice(0, 3000))) {
      return { outcome: "failed", message: "Target is asking for a human check on the checkout page." };
    }
    await sleep(500);
  }
  await sleep(1200); // let totals settle
  const button = placeButton();
  if (!button) return { outcome: "failed", message: "Couldn't find the Place order button (maybe a payment or address step needs you)." };

  const cvv = [...document.querySelectorAll("input")].find((i) => visible(i) && !i.value &&
    /cvv|cvc|security ?code/i.test([i.name, i.id, i.getAttribute("aria-label"), i.placeholder, i.autocomplete].join(" ")));
  if (cvv) return { outcome: "failed", message: "Target is asking for your card's security code (CVV). Enter it and press Place order." };

  const totals = [...text().matchAll(/total[^\n$]{0,30}\$\s?(\d[\d,]*\.\d{2})/gi)].map((m) => parseFloat(m[1].replace(/,/g, "")));
  if (!totals.length) return { outcome: "failed", message: "Couldn't read the order total, so the bot didn't buy." };
  const total = Math.max(...totals);
  if (total > maxTotal) {
    return { outcome: "failed", total, message: `The total is $${total.toFixed(2)}, above the limit of $${maxTotal.toFixed(2)} (your max price plus tax and shipping). Is something else in the cart?` };
  }
  if (testOnly) return { outcome: "test_ok", total, message: "Test mode: stopped before Place order." };
  button.click();
  return { outcome: "placed", total, message: "Place order clicked." };
}

function readOrder() {
  const text = (document.body && document.body.innerText) || "";
  const confirmed = /thank you for (your|the) order|thanks for (your|the) order|order (has been )?(placed|confirmed|received)|we('ve| have) (got|received) your order/i.test(text);
  const number = text.match(/order\s*(?:number|#|no\.?)\s*:?\s*#?\s*([A-Z0-9][A-Z0-9-]{5,})/i);
  const total = text.match(/(?:order\s+)?total\s*:?\s*\$\s?(\d[\d,]*\.\d{2})/i);
  return {
    confirmed,
    order_number: number ? number[1] : null,
    total: total ? total[1] : null,
    text: text.slice(0, 6000), // stays on your computer; used to match which watched items you bought
  };
}
