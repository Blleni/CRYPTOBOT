# Hyperliquid Whale Mirror — Beginner Guide

This bot watches the whale wallet you chose:

```
0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36
```

…and copies its trades in real time, scaled to your money. If the whale
puts 5% of their account into a trade, the bot puts 5% of yours.

It starts in **paper mode**: fake money, real whale, real prices, zero risk.
You don't need a Hyperliquid account, a wallet, or any money to run it.
**Run it in paper mode for at least a few weeks before even thinking about
live mode.** That is the whole point: it shows you what copying this whale
would actually have done to your money.

## ⚠ Know who you're copying

This wallet has been publicly identified as belonging to **Abraxas Capital**,
an institutional trader. Two things matter for you:

1. **They use ~10× leverage on positions worth hundreds of millions.** Their
   shorts have been $20M+ underwater before turning profitable. They can
   survive drawdowns that would wipe out a small account. The bot's 25%
   per-trade cap protects you somewhat, but a string of losing copied trades
   will still hurt.
2. **Their Hyperliquid shorts are believed to be *hedges* against large spot
   holdings.** That means when their short loses money, they may be perfectly
   happy — their other (invisible to you) holdings gained. You'd be copying
   half of a strategy. Their "win" and your "win" are not the same thing.

Paper mode exists so you can see this play out with fake money first.

## Step 1 — Install Python

- **Windows:** download Python 3.11+ from https://python.org/downloads —
  during install, tick the box **"Add Python to PATH"**.
- **Mac:** open Terminal and run `python3 --version`. If it's 3.10 or newer
  you're done; otherwise install from python.org.

## Step 2 — Open a terminal in this folder

- **Windows:** open the unzipped `hl-mirror` folder, click the address bar,
  type `cmd`, press Enter.
- **Mac:** open Terminal, type `cd ` (with a space), drag the `hl-mirror`
  folder into the window, press Enter.

## Step 3 — Install the two libraries the bot needs

```
pip install -r requirements.txt
```

(If `pip` isn't found on Mac, use `pip3`. Same for `python` → `python3`.)

## Step 4 — Run it

```
python mirror.py
```

That's it. You'll see a live dashboard:

- **header** — whale account value, your equity, your total P&L
- **Open Positions** — what you're currently copying, with live unrealized P&L
- **Recent Closed Trades** — each finished trade: entry, exit, profit/loss, W/L
- **Cumulative Stats** — total trades, win rate, average win, average loss

Leave the window open. The bot only "sees" whale trades while it's running.
Press `Ctrl+C` to stop; your history is saved in `state/` and continues when
you restart. Every event is also written to `logs/trades.jsonl` with
timestamps — that's your audit trail.

**Be patient**: this whale may trade a few times a day or go quiet for days.
"waiting for whale activity…" is normal.

## Step 5 — Adjust settings (optional)

Open `config.json` in any text editor. The `_help` section explains every
field. The ones you might change:

- `my_capital_usd` — your pretend starting money (default $1,000)
- `max_position_fraction` — per-trade cap (default 0.25 = 25%)
- `min_viable_balance_usd` — the bot pauses new trades below this and shows
  a red PAUSED alert

## Step 6 — Live mode (only after weeks of good paper results)

Honestly: most people should stop at paper mode. If you go further:

1. Practice on Hyperliquid's **testnet** first: set `"network": "testnet"`
   and `"mode": "live"`, and get free test funds at
   https://app.hyperliquid-testnet.xyz — this is live-mode plumbing with
   zero real money.
2. For real mainnet trading you need a funded Hyperliquid account and an
   **API wallet** (create one at https://app.hyperliquid.xyz/API — it can
   trade but cannot withdraw, which is exactly what you want to give a bot).
3. Put the API wallet's private key in an environment variable — never in
   any file:
   - Mac/Linux: `export HL_PRIVATE_KEY=0x...`
   - Windows: `set HL_PRIVATE_KEY=0x...`
4. Set `"mode": "live"`, `"my_address"` to your main wallet address, and run.
   The bot demands typed confirmation before placing anything.
5. Start with a small amount you can genuinely afford to lose entirely.

## Safety rails built in

- never risks a bigger *fraction* than the whale risked of their own account
- hard 25% cap per trade no matter what the whale does
- never trades more than your available balance
- pauses new entries + alerts when balance drops below your minimum
  (exits stay enabled so you can still get flat with the whale)
- ignores signals that arrive late (stale price)
- ignores the whale's *past* trades on startup (only mirrors new ones)
- full timestamped audit log of every action and every skip

## What it doesn't do

- It can't copy the whale's leverage advantage or their off-exchange hedges.
- Paper fills assume you get the whale's price; in live trading your fill is
  usually slightly worse (you trade *after* them, and they move the market).
- It copies perp trades only, not the whale's spot trades.

None of this is financial advice — it's a tool for observing and, if you
choose, following another trader's decisions with your own money at risk.
