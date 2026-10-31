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
  opens it, clicks Add to cart and takes you to checkout. See **Automatic checkout** below to let it press Place order too.

## How a restock gets confirmed (no false alarms)

1. **Quick check:** every few seconds the helper asks Target's stock service, from a pinned
   target.com tab in your Chrome, whether each item is in stock. This is the same request
   target.com itself makes.
2. **Page check:** when a quick check says "in stock", the helper immediately loads the real
   product page and looks only at **that product's own buy button**. "Add to cart" buttons on
   recommended-product carousels are ignored, since those caused the false alarms. It also reads
   who the seller is.
3. Only when both agree, and the seller is Target and the price is under your max, does
   Discord get pinged. A reading that doesn't hold up is ignored and noted in Activity.

Quick checks share one timer: with "2 seconds between quick checks" and 4 Target items,
each item is checked about every 8 seconds. Fewer items means faster checks per item. Going below 2
seconds makes Target more likely to show a human check.

## Discord: only what matters

By default Discord gets just two kinds of messages: **confirmed in stock** (with @everyone)
and **order placed**. Queues, human checks and new Discover listings stay on the dashboard,
unless you tick "Also post queues, human checks and new listings to Discord" in Settings.

## Automatic checkout (Target)

In Settings, **Automatic checkout on Target** has three modes:
- **Off:** you press Place order (default).
- **Test:** the bot adds to cart and fills checkout, then stops **before** Place order and
  notes the total in Activity. **Try this first on a real item** to make sure it works with your account.
- **On:** the bot presses Place order itself.

**Nothing is bought unless you tick Auto-buy**, either on an item (checkbox next to it in
the watchlist) or on a whole category (the **Buy** box in Categories). Everything else just pings you.

Safety rules, always on:
- Only confirmed restocks (two checks), only sold by Target, only on Target.
- Only items with a **max price** (on the item or its category).
- Stops if the checkout total is over max price x 1.12 + $10 (tax and shipping). This also
  catches other things sitting in your cart, so keep your Target cart empty.
- **One order per item:** after an order, that item is paused.
- **Daily spending limit** ($250 by default).
- If Target wants you to sign in, asks for your card's **CVV**, or shows a human check, the
  bot stops and Discord pings you: **CHECKOUT NEEDS YOU**. It never stores card details.

Before a drop: be signed in to Target in this Chrome with your address and card saved.

**Try it before a drop:** every Target item has a **Test checkout** button. It adds the item
to your cart, goes through checkout and stops before Place order, then reports the total in
Activity. Afterwards, **remove the item from your Target cart**, so a real checkout starts empty.

## Discord feed: use alerts from a server you follow

If another server's alert channel has a **Follow** button, its alerts can feed your bot.
Each alert is double-checked in Chrome before you're pinged or anything gets bought.

1. **Follow their channel:** in their server, open the alert channel, click **Follow**, and
   pick your server and a channel, for example a new `#restock-feed`.
2. **Create your own bot:** go to <https://discord.com/developers/applications>, then
   **New Application** → **Bot** → **Reset Token** → copy the token. On the same page,
   turn on **Message Content Intent**.
3. **Invite it to your server:** **OAuth2** → **URL Generator** → tick **bot**, then the permissions
   **View Channels** and **Read Message History**. Open the link it makes and pick your server.
4. **Copy the channel ID:** in Discord, go to **Settings → Advanced → Developer Mode** (on), then right-click
   `#restock-feed` → **Copy Channel ID**.
5. **In the dashboard Settings:** tick **Read alerts from my followed channel**, paste the
   channel ID and the bot token, and click **Save**. The chip at the top says **Feed connected**.

**Only your sets:** with "Only sets in my Categories" ticked (the default), the feed only
acts on alerts whose name or link matches one of your categories, for example "Ascended Heroes".
Every other set is ignored: no watchlist entry, no ping, no purchase. Matching alerts go straight
into that category. To catch shorter names like "Prismatic ETB", add `prismatic` to that category's
search words.

With that box unticked, other Pokémon items from the feed land in the **Discord feed** category. Its **Max $** (60 to start)
decides what automatic checkout may buy, so adjust it. The bot token is like a password:
it stays on your computer, and you shouldn't share it.

## Order confirmations

When an order is placed, by you or by automatic checkout, and the store shows its
"Thank you for your order" page in Chrome, the helper reads the order number and total and
you get an **ORDER PLACED** message on Discord. It also shows under **Orders placed** on the
dashboard. If you check out on your phone instead, look for the store's confirmation email.

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

## Tips
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
| **Confirming…** | One check said in stock. Double-checking the real page before alerting. |
| **In stock** | Confirmed by two checks. Buy it! You got a Discord alert. |
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
