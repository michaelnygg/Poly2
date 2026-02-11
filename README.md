# Polymarket Arbitrage Bot

24/7 autonomous scanner + executor on Railway (~$5/month).

## How Authentication Works

Per [Polymarket docs](https://docs.polymarket.com/developers/CLOB/authentication):

1. **L1 Auth**: Your private key signs an EIP-712 message
2. The CLOB derives your API key/secret/passphrase deterministically
3. **L2 Auth**: All subsequent requests use HMAC signatures

**You only need your wallet private key.** The bot derives everything else.

## Deploy to Railway

### 1. Push to GitHub (PRIVATE repo)

```bash
git init && git add . && git commit -m "polybot"
git remote add origin https://github.com/YOU/polybot.git
git push -u origin main
```

### 2. Railway: New Project → Deploy from GitHub

### 3. Set Environment Variables in Railway

**Which wallet type are you?**

**Option A — Direct MetaMask/EOA** (you sent crypto directly):
```
POLY_PRIVATE_KEY  = (your private key, no 0x prefix)
SIGNATURE_TYPE    = 0
```

**Option B — Email/Magic login** (you signed up with email):
```
POLY_PRIVATE_KEY  = (export from https://reveal.magic.link/polymarket)
SIGNATURE_TYPE    = 1
POLY_FUNDER       = (your Polymarket profile address from polymarket.com/settings)
```

**Option C — MetaMask through polymarket.com** (browser wallet proxy):
```
POLY_PRIVATE_KEY  = (your MetaMask private key, no 0x prefix)
SIGNATURE_TYPE    = 2
POLY_FUNDER       = (your Polymarket profile address from polymarket.com/settings)
```

**Plus these for all options:**
```
BANKROLL          = 500
DRY_RUN           = true
DRY_RUN_HOURS     = 24
MIN_PROFIT        = 0.03
MAX_POS_PCT       = 0.10
SCAN_INTERVAL     = 30
MAX_TRADES_HR     = 3
DRAWDOWN_LIMIT    = 0.15
```

### 4. Deploy

Watch Railway logs:
```
╔══════════════════════════════════════════════╗
║  POLYMARKET ARBITRAGE BOT — FULL AUTO        ║
╚══════════════════════════════════════════════╝
Deriving API credentials from private key (L1 auth)...
CLOB client initialized with L1+L2 auth
🚀 Bot started. DRY RUN for first 24h
```

### 5. Go Live

After 24h dry run, set `DRY_RUN=false` in Railway variables.

## Safety Rails

| Protection | Default | Purpose |
|---|---|---|
| Dry run period | 24 hours | Prove system works before risking money |
| Max per trade | 10% of bankroll | No single trade wipes you |
| Max trades/hour | 3 | Prevents runaway execution |
| Drawdown halt | 15% | Stops bot if losing |
| Min profit | $0.03/dollar | Ignores dust-level arb |
| Kelly sizing | Automatic | Math-optimal position sizing |

## Important: Token Allowances

For **EOA wallets (SIGNATURE_TYPE=0)**, you must set token allowances before the bot can trade.
See: https://gist.github.com/poly-rodr/44313920481de58d5a3f6d1f8226bd5e

Email/Magic wallets (type 1) typically have allowances set automatically.

## Cost

- Railway: ~$5/month
- Polygon gas: ~$0.005/trade
- Total: ~$5-8/month
