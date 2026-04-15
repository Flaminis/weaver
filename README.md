# Oracle-LoL

Automated League of Legends trading bot for Polymarket. Monitors live LoL matches via PandaScore LLF (Live Low-Frequency) WebSocket, detects in-game events (kills, drakes, barons, inhibitors), computes expected value using a trained LightGBM win-probability model, and places FAK buy orders on the Polymarket CLOB when edge exceeds fees + spread.

Holds to resolution — no active selling. Winning shares pay $1.00.

## How It Works

```
PandaScore LLF ──→ Event Detection ──→ ML Model Impact ──→ Edge Calc ──→ Kelly Sizing ──→ FAK Buy
  (live game state)   (kill/baron/drake)   (ΔP win)         (p_fair - ask*1.02)  (quarter Kelly)   (Polymarket CLOB)
```

1. **LLF WebSocket** streams game state (kills, towers, drakes, barons, inhibitors) per team every few seconds
2. **Event detection** diffs consecutive states to identify what just happened
3. **LightGBM model** predicts how much the event shifts win probability, given game minute + full scoreboard context
4. **Edge calculation** combines market prior with model delta: `p_fair = pre_event_mid + model_impact`, then `edge = p_fair - ask * (1 + 0.02 taker fee)`
5. **Kelly sizing** computes optimal bet size: `quarter_kelly * $5000 bankroll`, clamped to [$5, min($250, 30% of book depth)]
6. **Execution** sends a Fill-and-Kill order with edge-proportional slippage budget: `limit = ask + min(edge * 0.25, 2c)`

Signals that don't clear the 2c minimum edge gate are skipped — this single check replaces priced-in detection, late-game fade, and directional locks.

## Architecture

```
scripts/
├── lol_trader.py          # Main bot — asyncio event loop, LLF connection, order execution
├── lol_signal.py          # Signal model — loads ML model, computes impact per event
├── lol_trader_config.py   # All constants: edge thresholds, Kelly params, sizing limits
├── lol_risk.py            # Position tracking, PnL, circuit breaker, exposure limits
├── audit_trades.py        # Read-only trade auditor — pulls /api/state and reviews PnL
├── polymarket/
│   ├── client.py          # CLOB wrapper (buy_fak, sell_limit, sell_fak, fill verification)
│   ├── ws_prices.py       # Polymarket WebSocket — real-time orderbook streaming
│   ├── config.py          # Polymarket credentials (from .env)
│   └── logger.py          # Structured logging
└── training/
    ├── event_impact.py    # EventImpactModel — wraps LightGBM for runtime inference
    ├── fetch_training_data.py   # Fetches historical match frames/events from PandaScore API
    ├── build_dataset.py   # Transforms raw JSON into training_rows.parquet
    └── train_model.py     # Optuna hyperparameter search + LightGBM training

dashboard/                 # React + shadcn/ui monitoring dashboard
├── src/
│   ├── App.tsx
│   ├── components/        # Match cards, events panel, positions, PnL
│   └── lib/types.ts       # TypeScript types matching /api/state JSON
└── vite.config.ts

data/
├── models/
│   ├── winprob_lgbm.joblib      # Trained LightGBM model (~7.5k games)
│   └── study.json               # Optuna best params + metrics
├── processed/
│   └── training_rows.parquet    # Feature matrix for training
└── raw/                         # Raw PandaScore frames/events JSON

logs/
└── trade_tapes/           # Per-signal book recordings (-3s to +60s at 1Hz)
```

## The ML Model

LightGBM classifier trained on ~7,500 professional LoL games from PandaScore. Predicts P(blue team wins) given 9 features:

| Feature | Description |
|---------|-------------|
| `game_minute` | Current game time |
| `kill_diff` | Blue kills - Red kills |
| `tower_diff` | Blue towers - Red towers |
| `drake_diff` | Blue drakes - Red drakes |
| `baron_diff` | Blue barons - Red barons |
| `inhib_diff` | Blue inhibitors - Red inhibitors |
| `herald_diff` | Blue heralds - Red heralds |
| `total_kills` | Blue kills + Red kills |
| `gold_diff_approx` | Estimated gold difference from objectives |

At runtime, `EventImpactModel` computes the model's probability **before** and **after** the event to get a calibrated delta. This delta is what drives the edge calculation.

**Metrics** (5-fold GroupKFold, grouped by game_id):
- Log-loss: ~0.38
- AUC: ~0.88

## Trade Tapes

Every signal that passes the edge gate triggers a 63-second recording of the orderbook:

- **-3s to 0s**: All raw ticks from the book buffer (sub-second granularity)
- **0s to +60s**: Full book snapshot every 1 second (top-5 levels, depth, bid/ask/spread)

Saved as JSON in `logs/trade_tapes/`. Each file contains the signal parameters, game state, execution outcome, and the full price time-series. Use these to measure whether the bot is betting ahead of or behind the market.

## Setup

### Requirements

```
# Runtime
pip install httpx websockets python-dotenv aiohttp py-clob-client

# Training (optional)
pip install lightgbm optuna scikit-learn pandas pyarrow joblib
```

### Environment Variables

Create `.env` in the project root:

```env
PANDASCORE_API_KEY=...       # PandaScore API key (LLF access required)
POLY_PRIVATE_KEY=...         # Polymarket wallet private key
POLY_API_KEY=...             # Polymarket CLOB API key
POLY_API_SECRET=...          # Polymarket CLOB API secret
POLY_API_PASSPHRASE=...     # Polymarket CLOB API passphrase
```

### Running

```bash
# Dry run (paper trading, no real orders)
python3 scripts/lol_trader.py

# Live trading
python3 scripts/lol_trader.py --live

# Audit recent trades
python3 scripts/audit_trades.py -n 10

# Dashboard (local dev)
cd dashboard && npm install && npm run dev
```

The bot exposes an HTTP API at `:8430` with `/api/state` returning full JSON state (matches, positions, events, PnL).

## Production

Runs on VPS at `38.180.152.197:8430` as a systemd service (`lol-trade`).

```bash
# Deploy
git push origin main
ssh -i ~/.ssh/ishosting_ie root@38.180.152.197 \
  "cd /opt/lol-trade && git pull origin main && systemctl restart lol-trade"

# Verify
curl -sS http://38.180.152.197:8430/api/state | python3 -m json.tool | head -20
ssh -i ~/.ssh/ishosting_ie root@38.180.152.197 "journalctl -u lol-trade -n 30 --no-pager"
```

## Training a New Model

```bash
# 1. Fetch data (resumes from checkpoint)
python3 -u scripts/training/fetch_training_data.py

# 2. Build feature matrix
python3 scripts/training/build_dataset.py

# 3. Train with Optuna hyperparameter search
python3 scripts/training/train_model.py

# Model saved to data/models/winprob_lgbm.joblib
# SCP to VPS after training
```

## Key Config (lol_trader_config.py)

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `TAKER_FEE` | 0.02 | Polymarket taker fee on FAK buys |
| `MIN_EDGE` | 0.02 | Minimum edge after fees to take a trade |
| `KELLY_FRACTION` | 0.25 | Quarter Kelly for conservative sizing |
| `BANKROLL` | $5,000 | Total capital for Kelly formula |
| `MIN_BET` / `MAX_SINGLE_BET` | $5 / $250 | Per-trade size bounds |
| `MAX_SPREAD` | 0.02 | Skip if spread > 2c |
| `MAX_BOOK_PARTICIPATION` | 0.30 | Never take > 30% of available depth |
| `TRADE_MAX_PRICE` | 0.85 | Don't buy above 85c (not enough upside) |
