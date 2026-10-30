# Pokémon Restock Monitor

Watches Target and Pokémon Center product pages (and most other stores) and pings
your phone on Discord the second something is back in stock. It has a dashboard in
your browser where you add items, set max prices and see what's happening.

---

## One-time setup (about 10 minutes)

### 1. Install Python
- Go to <https://www.python.org/downloads/> and click the big yellow **Download** button.
- **Windows:** run the installer, and on the first screen **tick "Add python.exe to PATH"** before clicking Install.
- **Mac:** run the installer. When it finishes, open **Applications → Python 3.x** and
  double-click **Install Certificates.command**. Without this step, the monitor can't connect to stores.

### 2. Make a Discord webhook (this is how alerts reach your phone)
1. In Discord, create your own server (the **+** button on the left, then "Create My Own"). You can also use one you already own.
2. Right-click a channel → **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook**.
3. Click **Copy Webhook URL**. You'll paste it into the dashboard in a minute.
4. On your phone, make sure Discord notifications are on for that server
   (long-press the server → Notification Settings → **All Messages**).

### 3. Start the monitor
- **Windows:** double-click **`Start (Windows).bat`**.
- **Mac:** double-click **`Start (Mac).command`**. If macOS blocks it, right-click it → **Open** → **Open**.

A black window opens (leave it open; that's the monitor running) and your browser
opens the dashboard at **http://localhost:8080**.

### 4. In the dashboard
1. Scroll to **Settings**, paste your webhook link, click **Save**, then **Send test alert**.
   Your phone should buzz.
2. Paste a product link at the top and click **Add**. Examples:
   - Target: `https://www.target.com/p/pokemon-.../-/A-12345678`
   - Pokémon Center: `https://www.pokemoncenter.com/product/...`
3. Optional: set a **max price** so you aren't alerted about an overpriced listing.

That's it. Next time, just double-click the Start file again. Your watchlist is saved.

---

## Chrome helper (for Target, and for Discover)

Target blocks automated checks, so Target items are checked by a small Chrome extension
inside **your own Chrome**. It opens your watched pages one at a time in a pinned tab and
reads the stock, price and seller, the same as you hitting refresh. It also runs **Discover**.

**Install it once:**
1. Open Chrome and type `chrome://extensions` in the address bar.
2. Turn on **Developer mode** (switch in the top-right corner).
3. Click **Load unpacked** and pick the **`chrome-helper`** folder inside your monitor folder.
4. Pin it: click the puzzle-piece icon in Chrome's toolbar, then the pin next to "Restock Monitor Helper".

The dashboard chip should change to **Chrome helper connected** within 30 seconds.
Keep Chrome open (it can be minimized). The helper updates itself along with the monitor.

- **Badge "off":** the monitor isn't running. Start it.
- **Badge "!":** Target or Pokémon Center wants a human check. Click the pinned tab and do it.
  You'll also get a Discord message. The helper never solves these for you.
- **Auto add-to-cart (optional, in Settings):** when something comes in stock, the helper
  opens it, clicks Add to cart and takes you to checkout. **It never presses Place order.** You do.

## Categories, Discover and retail-only buying

- **Categories** (e.g. Ascended Heroes, Prismatic Evolutions, 30th Celebration) group your
  watchlist. Each has **search words**, which Discover uses, and a **Max $**.
- **Discover** searches Target and Pokémon Center for each category's search words every
  15 minutes and alerts you on Discord when a **new** product listing appears. Click **Watch**
  to add it. The first search of each category only fills the list, without alerts.
- **Retail only:** with "Only alert when sold by the store itself" on (the default), items
  sold by **third-party sellers** on Target (marked "Sold and shipped by ...") never alert you.
  They show as **3rd-party only**.
- **Max $** is your second safety net. Set it close to retail. Rough US retail prices
  (check the current price for each product): Elite Trainer Box about $50-60,
  Booster Bundle about $27-30, Pokémon Center ETB about $60. A $150 "ETB" is a reseller.

- **Your computer must be on and awake.** Turn off sleep mode while you're monitoring
  (Windows: Settings → System → Power; Mac: System Settings → Battery/Energy → prevent sleep).
- **Be logged in ahead of time** on Target and Pokémon Center with your address and card
  saved, in the browser and on your phone. When the alert comes, tap the link and check out.
- **Target in-store pickup:** enter your store ID in Settings. To find it, open your store on
  target.com (the "My Store" page). The number at the end of the page link is the ID.
- **"Queue live" alert:** Pokémon Center puts up a waiting room during big drops. When the
  monitor sees one, it alerts you. Join the queue right away.
- **Don't set the check times too low.** 30 to 60 seconds is the sweet spot. Checking too
  often gets your home internet temporarily blocked by the store, and then you see nothing.

## What the statuses mean
| Status | Meaning |
|---|---|
| **In stock** | Buy it! You got a Discord alert. |
| **3rd-party only** | In stock, but only from a reseller. No alert. |
| **Sold out** | Watching. You'll be alerted when it changes. |
| **Queue live** | Pokémon Center's waiting room is up. Get in line. |
| **Blocked** | The store showed a bot check. The monitor slows down on its own and retries. |
| **Error** | Something else went wrong. The message under the item says what. |

## Files
- `app.py`: the monitor and the dashboard server
- `dashboard.html`: the dashboard page
- `chrome-helper/`: the Chrome extension
- `monitor.db`: your saved watchlist, settings and history (created automatically).
  Delete it to start fresh.
