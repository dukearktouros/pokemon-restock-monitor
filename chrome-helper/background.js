// Restock Monitor Helper
//
// Asks the monitor running on this computer (http://localhost:8080) what to look at,
// opens those pages one at a time in a pinned background tab of *your* Chrome, reads
// stock / price / seller from the page and reports back. It never solves bot checks
// and never places an order: if a store asks "are you human", you answer it yourself.

const MONITOR = "http://localhost:8080";
const PAGE_TIMEOUT = 25000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let busy = false;

function start() {
  chrome.alarms.create("tick", { periodInMinutes: 0.5 });
  tick();
}
chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === "tick") tick(); });
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

async function tick() {
  if (busy) return;
  busy = true;
  try {
    for (let round = 0; round < 6; round++) {
      let jobs;
      try {
        jobs = await api("/api/ext/jobs?v=" + chrome.runtime.getManifest().version);
      } catch (e) {
        setBadge("off", "#6b7280"); // monitor isn't running
        return;
      }
      if (jobs.ext_version && jobs.ext_version !== chrome.runtime.getManifest().version) {
        chrome.runtime.reload(); // the monitor updated our files on disk
        return;
      }
      setBadge("");
      for (const job of jobs.open || []) await openForCheckout(job, jobs.auto_cart);

      const work = [
        ...(jobs.checks || []).map((j) => ({ ...j, kind: "check" })),
        ...(jobs.searches || []).map((j) => ({ ...j, kind: "search" })),
      ];
      if (!work.length) return;
      for (const job of work) {
        const result = job.kind === "check"
          ? await runInWatcher(job.url, extractProduct, [job.retailer])
          : await runInWatcher(job.url, extractSearch, [job.site]);
        if (job.kind === "check") await api("/api/ext/report", { id: job.id, ...result });
        else await api("/api/ext/discover", { key: job.key, ...result });
        if (result.captcha) {
          setBadge("!"); // wait for you to complete the check in the pinned tab
          return;
        }
        await sleep(3000 + Math.random() * 3000);
      }
    }
  } catch (e) {
    console.warn("Restock helper:", e);
  } finally {
    busy = false;
  }
}

async function watcherTab() {
  const { watcherTabId } = await chrome.storage.session.get("watcherTabId");
  if (watcherTabId) {
    const tab = await chrome.tabs.get(watcherTabId).catch(() => null);
    if (tab) return tab.id;
  }
  const tab = await chrome.tabs.create({ url: "about:blank", active: false, pinned: true });
  await chrome.storage.session.set({ watcherTabId: tab.id });
  return tab.id;
}

async function waitForLoad(tabId) {
  const end = Date.now() + PAGE_TIMEOUT;
  await sleep(800);
  while (Date.now() < end) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return false;
    if (tab.status === "complete" && tab.url && !tab.url.startsWith("about:")) return true;
    await sleep(400);
  }
  return false;
}

async function runInWatcher(url, func, args) {
  try {
    const tabId = await watcherTab();
    await chrome.tabs.update(tabId, { url });
    await waitForLoad(tabId);
    const [res] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
    return (res && res.result) || { error: "The page didn't return anything" };
  } catch (e) {
    return { error: "Couldn't read the page in Chrome: " + e.message };
  }
}

async function openForCheckout(job, autoCart) {
  const tab = await chrome.tabs.create({ url: job.url, active: true });
  chrome.windows.update(tab.windowId, { focused: true });
  if (!autoCart || !job.checkout_url) return;
  await waitForLoad(tab.id);
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: addToCart });
    // Go to checkout and stop there. You review it and press "Place order" yourself.
    if (res && res.result && res.result.added) await chrome.tabs.update(tab.id, { url: job.checkout_url });
  } catch (e) {
    console.warn("Restock helper: add to cart failed", e);
  }
}

// ---------------------------------------------------------------------------
// The functions below run inside the store's page, so each must be self-contained.

async function extractProduct(retailer) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const BUY = /^(add to cart|add to bag|ship it|pick it up|deliver it|pre-?order( now)?|add for (shipping|pickup|delivery))\b/i;
  const text = () => (document.body && document.body.innerText) || "";
  const isCaptcha = () =>
    !!document.querySelector("#px-captcha, iframe[src*='captcha'], iframe[src*='_Incapsula_Resource']") ||
    /press\s*(&|and)\s*hold|verify you are (a )?human|pardon our interruption/i.test(text().slice(0, 3000));
  const isQueue = () => /queue-it|you are now in line|virtual queue|waiting room/i.test(location.href + " " + text().slice(0, 3000));
  const title = () => document.querySelector("h1[data-test='product-title'], h1");
  const buyButtons = () => [...document.querySelectorAll("button")].filter((b) =>
    BUY.test((b.innerText || "").trim()) && !b.disabled && b.getAttribute("aria-disabled") !== "true" && b.offsetParent !== null);
  const soldOutText = () => /\b(sold out|out of stock|currently unavailable|not available|coming soon)\b/i.test(text());

  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (isCaptcha()) return { captcha: true };
    if (isQueue()) return { queue: true };
    if (title() && (buyButtons().length || soldOutText())) break;
    await sleep(500);
  }
  await sleep(1000); // let the buy box finish rendering

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
  const priceText = (priceEl && priceEl.innerText) || ld.price || meta("product:price:amount") ||
    ((text().match(/\$\s?\d[\d,]*\.\d{2}/) || [])[0]);
  const sellerMatch = text().match(/sold (?:and|&) shipped by\s*:?\s*([^\n|]{2,60})/i);
  const seller = (sellerMatch && sellerMatch[1].trim()) || ld.seller || null;

  let status = "unknown";
  if (buttons.length) status = "in_stock";
  else if (/^(InStock|LimitedAvailability|PreOrder|OnlineOnly)$/i.test(ld.availability || "")) status = "in_stock";
  else if (title()) status = "out_of_stock";

  const how = buttons.map((b) => b.innerText.trim()).slice(0, 3).join(", ");
  return {
    status,
    name: (title() && title().innerText.trim()) || ld.name || meta("og:title") || null,
    image: meta("og:image") || ld.image || null,
    price: priceText ? String(priceText) : null,
    seller,
    detail: status === "in_stock" ? (how ? "Buttons: " + how : "Listed as in stock") : null,
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
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const button = [...document.querySelectorAll("button")].find((b) =>
      BUY.test((b.innerText || "").trim()) && !b.disabled && b.getAttribute("aria-disabled") !== "true");
    if (button) {
      button.click();
      await sleep(2500);
      return { added: true };
    }
    await sleep(400);
  }
  return { added: false };
}
