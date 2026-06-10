# World Cup 2026 - Daily Score Predictions

A bot that every day at noon sends you the most probable scoreline for every World Cup game, powered by live Polymarket betting odds.

**What you get:**
- A **Telegram message** with today's and tomorrow's games + predicted scores (with country flags)
- A **Google Sheet** with all 72 games, predicted scores, and actual results as they come in

**How it works:**  
The script pulls live betting odds from [Polymarket](https://polymarket.com), uses a statistical model (independent Poisson distribution) to compute the most likely scoreline, and delivers predictions to your phone and a shared Sheet. No manual input needed once set up.

---

## What you need

- A **Mac** (the daily schedule uses a macOS-only feature)
- A **Telegram** account (free, works on any phone)
- A personal **Google** account for the Sheet
- The **Terminal** app (comes with every Mac, found in Applications > Utilities)

No programming experience required. Every step uses copy-paste commands.

---

## Setup

### Step 1 - Download the code

Open Terminal and run these three commands one at a time:

```bash
git clone https://github.com/YG15/wc-score-bot.git
```
```bash
mkdir -p ~/wc-scores
```
```bash
cp wc-score-bot/daily_scores.py wc-score-bot/com.wcscores.plist ~/wc-scores/
```

### Step 2 - Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send the message `/newbot`
3. Give it any name you like (e.g. `WC Scores`)
4. Give it a username ending in `bot` (e.g. `mywcscores_bot`) - must be unique
5. BotFather replies with a **token** that looks like `1234567890:ABCDEFGxxx` - copy it

### Step 3 - Get your Telegram chat ID

1. Search for your new bot in Telegram and press **Start**
2. Send it any message (e.g. `hello`)
3. Open this URL in a browser, replacing `YOUR_TOKEN` with the token from Step 2:
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
4. Find the number after `"chat":{"id":` - that's your chat ID (e.g. `683221070`)

### Step 4 - Save your Telegram credentials

In Terminal, run the two lines below. Replace `YOUR_TOKEN` and `YOUR_CHAT_ID` with the values from Steps 2-3:

```bash
echo "YOUR_TOKEN" > ~/wc-scores/telegram.txt
echo "YOUR_CHAT_ID" >> ~/wc-scores/telegram.txt
chmod 600 ~/wc-scores/telegram.txt
```

To verify it looks right (should show two lines, your token and your chat ID):
```bash
cat ~/wc-scores/telegram.txt
```

### Step 5 - Set up the Google Sheet

The Sheet updates automatically via a small script you paste into it. This takes about 5 minutes.

**Create a new Sheet:**
1. Make sure you are logged in to your personal Google account (not work)
2. Go to [sheets.new](https://sheets.new) in your browser - a blank Sheet opens

**Add the script:**
1. In the Sheet, click **Extensions** in the top menu bar
2. Click **Apps Script**
3. A new tab opens with a code editor. Delete everything in it.
4. Open the file `apps_script.js` from this repo and copy-paste its entire contents into the editor
5. Click the **Save** button (floppy disk icon, or Cmd+S)

**Deploy the script as a web app:**
1. Click **Deploy** in the top right > **New deployment**
2. Click the gear icon next to "Select type" > choose **Web app**
3. Under "Who has access", select **Anyone**
4. Click **Deploy**
5. A permissions screen may appear - click through to approve (this lets your Mac send data to the Sheet)
6. Copy the **Web app URL** - it looks like `https://script.google.com/macros/s/AKfy.../exec`

**Save the URL:**
```bash
echo "PASTE_YOUR_WEB_APP_URL_HERE" > ~/wc-scores/sheet_webhook.txt
chmod 600 ~/wc-scores/sheet_webhook.txt
```

### Step 6 - Set up the daily schedule

This tells your Mac to run the script every day at noon. Run these two commands:

```bash
sed "s/YOUR_USERNAME/$(whoami)/g" ~/wc-scores/com.wcscores.plist \
  > ~/Library/LaunchAgents/com.wcscores.plist
```
```bash
launchctl load -w ~/Library/LaunchAgents/com.wcscores.plist
```

Confirm it's loaded (you should see `com.wcscores` in the output):
```bash
launchctl list | grep wcscores
```

### Step 7 - Test it

First, do a dry run that prints output but doesn't send anything:
```bash
/usr/bin/python3 ~/wc-scores/daily_scores.py --print
```

If the output looks good (you see game names and predicted scores), do a real run:
```bash
/usr/bin/python3 ~/wc-scores/daily_scores.py
```

Check your Telegram - you should have received a message. The Google Sheet should also have updated (look for it in your Google Drive recents, it will be named "World Cup 2026 - Most-Probable Scores").

---

## Managing the bot

**Run it right now** (without waiting for noon):
```bash
launchctl kickstart -k gui/$(id -u)/com.wcscores
```

**View logs** (useful if something seems broken):
```bash
tail -50 ~/wc-scores/launchd.log
```

**Change the run time** (e.g. to 9am):  
Open `~/Library/LaunchAgents/com.wcscores.plist` in a text editor, change the `12` under `Hour` to `9`, then reload:
```bash
launchctl unload ~/Library/LaunchAgents/com.wcscores.plist
launchctl load -w ~/Library/LaunchAgents/com.wcscores.plist
```

**Stop it permanently:**
```bash
launchctl unload ~/Library/LaunchAgents/com.wcscores.plist
rm ~/Library/LaunchAgents/com.wcscores.plist
```

---

## Important notes

- **Your Mac must be awake at noon** for the job to fire. If it was sleeping, run it manually with the kickstart command above.
- **Internet is required** at run time to fetch live odds from Polymarket.
- The bot **automatically stops after 20 July 2026** (day after the WC final). You don't need to clean anything up.
- Predictions are based on market odds, not inside information. Use for fun, not gambling.

---

## Files in this repo

| File | What it is |
|------|-----------|
| `daily_scores.py` | The main script. Uses only Python's built-in libraries - no installs needed. |
| `apps_script.js` | The code you paste into your Google Sheet's Apps Script editor. |
| `com.wcscores.plist` | Template for the macOS daily scheduler. |
| `telegram.txt.example` | Shows the expected format for your credentials file (do not edit - create your own `telegram.txt`). |
| `sheet_webhook.txt.example` | Shows the expected format for the webhook URL file. |

---

## Troubleshooting

**No Telegram message received:**
- Run `tail -20 ~/wc-scores/launchd.log` and look for error messages
- Check your `telegram.txt` has exactly two lines (token on line 1, chat ID on line 2)
- Make sure you sent a message to the bot first (bots can't message you until you start them)

**Sheet is not updating:**
- Check that your `sheet_webhook.txt` contains the exact URL from the Apps Script deployment
- Make sure the Apps Script was deployed from *inside* the Sheet (Extensions > Apps Script), not as a standalone script

**`command not found: git`:**
- Install Xcode Command Line Tools: run `xcode-select --install` in Terminal

**The launchctl command fails:**
- Check the plist installed correctly: `cat ~/Library/LaunchAgents/com.wcscores.plist`
- Make sure `YOUR_USERNAME` was replaced - it should show your actual Mac username
