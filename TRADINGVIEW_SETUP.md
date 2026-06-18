# IADSS UltiTrader Scalper — TradingView Alert Setup Guide

## Overview

The bot works by receiving TradingView alerts (webhooks) whenever an IADSS signal fires.
It only enters a trade when **all three** IADSS models agree within a 5-minute window:

| # | Model | Role | Alert triggers on |
|---|-------|------|--------------------|
| 1 | **Confluence Model** | Primary signal | Confirmed Buy / Confirmed Sell |
| 2 | **Mean Reversion** | Secondary confirmation | Buy / Sell |
| 3 | **Trend Model** | Mandatory last gate | Buy (flip blue) / Sell (flip orange) |
| 4 | **Optimized Trend** (15-min chart) | Macro bias filter | Up / Down |

**Entry rule:** Confluence BUY + Mean Reversion BUY + Trend BUY — all within 5 minutes  
**Exit rule:** Trend SELL alone (fastest) OR Confluence SELL + MR SELL together

---

## Step 1 — IADSS Indicator Settings for Scalping

On every chart where you create alerts, apply these settings (gear icon on each indicator):

### Confluence Model
| Setting | Value | Why |
|---------|-------|-----|
| Bias | Neutral | No directional lean |
| Entry type | % of current capital | |
| % | 10 | Matches 10% lot size |
| Order type | LIFO | Matches bot logic |
| Noise suppression | 0 | Maximum signals for scalping |
| Trading style | Very Aggressive | More signals on short TFs |
| Show backtest | On | Verify settings |

### Trend Model
| Setting | Value | Why |
|---------|-------|-----|
| Tightness | **Very Tight** | 98% win rate per docs |
| Order type | LIFO | |
| Noise suppression | 0 | |
| Trading style | Very Aggressive | |

### Mean Reversion
| Setting | Value | Why |
|---------|-------|-----|
| Order type | LIFO | |
| Noise suppression | **0** | Maximum signals |
| Trading style | **Very Aggressive** | More green dots |

---

## Step 2 — Timeframe Strategy

Use a **multi-timeframe** approach:

| Chart | Timeframe | Indicator | Purpose |
|-------|-----------|-----------|---------|
| Chart A | **15-minute** | Optimized Trend only | Macro bias (OT blue = trade longs) |
| Chart B | **5-minute** | All 4 IADSS | Main signal chart — create alerts here |
| Chart C | **1-minute** | Confluence + Trend only | Fast confirmation visual (optional) |

> **Rule**: Only take LONG entries when the 15-min OT is BLUE. If OT is ORANGE, the bot ignores buy signals automatically.

---

## Step 3 — Webhook URL

Your webhook URL follows this pattern:

```
https://YOUR-RAILWAY-URL/webhook?secret=iadss-ultitrader-2024
```

**For local testing** (using ngrok):
```
https://XXXX.ngrok.app/webhook?secret=iadss-ultitrader-2024
```

**For Railway production (UltiTrader service):**
```
https://ultitrader-iadss-production.up.railway.app/webhook?secret=iadss-ultitrader-2024
```

---

## Step 4 — Create TradingView Alerts

For each symbol, open TradingView → that symbol → 5-minute chart → all IADSS indicators loaded.

Click the **Alerts** bell (right sidebar) → **Create Alert (+)**

### Alert Message Format (JSON)

Copy the exact JSON below into the **"Message"** box for each alert.

---

### 4A — Confluence: Confirmed Buy

- **Condition:** `IA-Confluence-Model` → `Confirmed Buy`
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"conf","sig":"buy","str":"confirmed","px":{{close}}}
```

### 4B — Confluence: Confirmed Sell

- **Condition:** `IA-Confluence-Model` → `Confirmed Sell`
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"conf","sig":"sell","str":"confirmed","px":{{close}}}
```

### 4C — Mean Reversion: Buy

- **Condition:** `IA-Mean-Reversion` → `Buy`
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"mr","sig":"buy","str":"confirmed","px":{{close}}}
```

### 4D — Mean Reversion: Sell

- **Condition:** `IA-Mean-Reversion` → `Sell`
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"mr","sig":"sell","str":"confirmed","px":{{close}}}
```

### 4E — Trend Model: Buy (flip blue)

- **Condition:** `IA-Trend-Model` → `Buy`
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"trend","sig":"buy","str":"confirmed","px":{{close}}}
```

### 4F — Trend Model: Sell (flip orange)

- **Condition:** `IA-Trend-Model` → `Sell`
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"trend","sig":"sell","str":"confirmed","px":{{close}}}
```

### 4G — Optimized Trend: Up (15-min macro chart only)

- **Condition:** `IA-Optimized-Trend` → `Up` (on the 15-min chart)
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"ot","sig":"up","str":"confirmed","px":{{close}}}
```

### 4H — Optimized Trend: Down (15-min macro chart only)

- **Condition:** `IA-Optimized-Trend` → `Down` (on the 15-min chart)
- **Message:**
```json
{"sym":"{{ticker}}","mdl":"ot","sig":"down","str":"confirmed","px":{{close}}}
```

---

## Step 5 — Alert Settings (same for all)

| Field | Value |
|-------|-------|
| Trigger | **Once Per Bar Close** ← CRITICAL (prevents repainting) |
| Expiration | Set to max (1 month or more) |
| Webhook URL | Your webhook URL from Step 3 |
| Alert name | e.g. `NVDA-5m-conf-buy` |

> **IMPORTANT:** Always use "Once Per Bar Close" — firing on bar close eliminates false signals from repainting, which is explicitly acknowledged in the IADSS FAQ.

---

## Step 6 — Priority Alert Schedule

TradingView limits alerts by plan tier. Start with these priority symbols:

### TradingView Pro (~20 alerts) — Start with 2 symbols

| Priority | Symbols | Alerts needed |
|----------|---------|---------------|
| Tier 1 | NVDA, BTC/USD | 2 × 8 = 16 alerts |

### TradingView Pro+ (~100 alerts) — All target symbols

8 alerts per symbol × 12 symbols = **96 alerts total**

The 8 alerts per symbol are:
- conf buy, conf sell (5-min chart)
- mr buy, mr sell (5-min chart)
- trend buy, trend sell (5-min chart)
- ot up, ot down (15-min chart)

**Recommended symbol priority order:**
1. NVDA (highest volatility, best for scalping)
2. TSLA (very high volatility)
3. BTC/USD (24/7, no market hours constraint)
4. ETH/USD (24/7)
5. AMD (follows NVDA well)
6. SOL/USD (high crypto volatility)
7. META, AAPL, MSFT, AMZN, GOOGL, AVGO (less volatile, lower priority)

---

## Step 7 — TradingView Alert Naming Convention

Use this naming convention to stay organised across 84 alerts:

```
{SYMBOL}-{TF}-{model}-{signal}
```

Examples:
- `NVDA-5m-conf-buy`
- `NVDA-5m-conf-sell`
- `NVDA-5m-mr-buy`
- `NVDA-5m-trend-buy`
- `NVDA-15m-ot-up`   ← macro filter only

---

## Step 8 — Verify the Webhook

1. Start the bot: `python bot.py`
2. Go to TradingView → Alerts → click any active alert → **Trigger alert manually**
3. Check bot terminal — you should see:
   ```
   2024-xx-xx 10:00:00 INFO  | Signal: NVDA conf buy (confirmed) @ $875.00
   ```
4. Check `/status` endpoint: `http://localhost:3000/status`

---

## Step 9 — Find Your Telegram Chat ID

1. Open Telegram → search for your bot (`@YourBotName`)
2. Send any message (e.g. `/start`)
3. Start the bot — it will print your chat_id in the console:
   ```
   Discovered TELEGRAM_CHAT_ID=123456789
   ```
4. Add `TELEGRAM_CHAT_ID=123456789` to your `.env` file
5. Restart the bot

---

## Full Strategy Reference

### Entry Conditions (all must be true)
- [ ] Confluence Model: Confirmed Buy signal (solid green triangle) — fired within 5 min
- [ ] Mean Reversion: Buy signal (green dots turning up) — fired within 5 min
- [ ] Trend Model: Buy signal (flipped to BLUE) — fired within 5 min
- [ ] Macro OT (15-min): BLUE (uptrend) — macro bias filter
- [ ] Not already at max lots (3 per symbol)
- [ ] Daily loss limit not reached (3 losses or -$300)
- [ ] Stock market is open (stocks only; crypto is 24/7)
- [ ] Entry cooldown expired (2 min since last entry on same symbol)

### Exit Conditions (first one triggered wins)
- Trend Model flips SELL (orange) → exit ALL lots immediately
- Confluence SELL + Mean Reversion SELL → exit ALL lots
- Price drops 1.5% below lot entry → exit ALL lots (stop-loss)
- Price rises 2.5% above lot entry → exit newest lot (LIFO take-profit)

### IADSS Settings Matrix (from Excel template)
| Model | Tightness | Style | Noise | Order |
|-------|-----------|-------|-------|-------|
| Confluence | N/A | Very Aggressive | 0 | LIFO 10% |
| Trend | **Very Tight** | Very Aggressive | 0 | LIFO 10% |
| Mean Reversion | N/A | **Very Aggressive** | **0** | LIFO 10% |

---

## Deployment on Railway

1. Push UltiTrader folder to a GitHub repo
2. Railway → New Project → Deploy from GitHub
3. Add all `.env` variables in Railway → Variables tab
4. Set `PORT=3000` in Railway environment
5. Your webhook URL: `https://YOUR-APP.up.railway.app/webhook?secret=iadss-ultitrader-2024`

---

## Quick Reference — Webhook JSON Examples

| Signal | JSON payload |
|--------|-------------|
| NVDA Confluence Buy | `{"sym":"NVDA","mdl":"conf","sig":"buy","str":"confirmed","px":875.50}` |
| NVDA MR Buy | `{"sym":"NVDA","mdl":"mr","sig":"buy","str":"confirmed","px":876.00}` |
| NVDA Trend Buy | `{"sym":"NVDA","mdl":"trend","sig":"buy","str":"confirmed","px":877.00}` |
| NVDA Trend Sell | `{"sym":"NVDA","mdl":"trend","sig":"sell","str":"confirmed","px":860.00}` |
| BTC/USD conf buy | `{"sym":"BTCUSD","mdl":"conf","sig":"buy","str":"confirmed","px":95000}` |
| 15-min OT Up | `{"sym":"NVDA","mdl":"ot","sig":"up","str":"confirmed","px":875.00}` |
