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
| **In stock** | Buy it! You got a Discord alert. |
| **Sold out** | Watching. You'll be alerted when it changes. |
| **Queue live** | Pokémon Center's waiting room is up. Get in line. |
| **Blocked** | The store showed a bot check. The monitor slows down on its own and retries. |
| **Error** | Something else went wrong. The message under the item says what. |

## Files
- `app.py`: the monitor and the dashboard server
- `dashboard.html`: the dashboard page
- `monitor.db`: your saved watchlist, settings and history (created automatically).
  Delete it to start fresh.
