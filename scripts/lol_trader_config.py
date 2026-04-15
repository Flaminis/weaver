"""
All constants and thresholds for the LoL Polymarket trading bot.

Entry: FAK buy (taker)
Exit: GTC limit sell (maker) — no slippage, earn spread
Fallback exit: FAK sell after timeout
"""

# ── Entry gates ─────────────────────────────────────────────────────────

TRADE_MIN_PRICE = 0.02          # Don't buy below 2c
TRADE_MAX_PRICE = 0.85          # Don't buy above 85c
MAX_SPREAD = 0.02               # Max 2c spread
MIN_EDGE = 0.02                 # v2: impact_prior - spread must exceed this
MIN_BOOK_DEPTH = 30             # $ depth within 3c of best (buy side)
PRICED_IN_WINDOW_SEC = 2.0      # Look-back window for priced-in check
PRICED_IN_THRESHOLD = 0.045     # v2: stricter on noisy legs (was 5c flat)
NEAR_RESOLVED_FLOOR = 0.03      # Skip markets priced below 3c
NEAR_RESOLVED_CEIL = 0.97       # Skip markets priced above 97c

# ── Exit ────────────────────────────────────────────────────────────────

HOLD_SECONDS = 30               # Auto-sell after 30s
SELL_PRICE_OFFSET = 0.01        # Sell limit at mid or best_ask - tick (maker)
SELL_TIMEOUT_SEC = 25           # If GTC not filled after 25s, cancel and FAK
SELL_FAK_SLIPPAGE = 0.03        # Emergency FAK sell: 3c below bid
MAX_SELL_RETRIES = 2            # Emergency sell retries with increasing aggression

# ── Sizing ──────────────────────────────────────────────────────────────

BET_SIZE_BASE = 10.0            # $10 base per trade (prod)
MAX_SINGLE_BET = 20.0           # $20 max single trade
MAX_TOTAL_EXPOSURE = 100.0      # $100 max across all open positions

# ── Cooldowns ───────────────────────────────────────────────────────────

TOKEN_COOLDOWN_SEC = 30         # Don't re-enter same token for 30s
MATCH_COOLDOWN_SEC = 5          # Min 5s between trades on same match

# ── Circuit breaker ─────────────────────────────────────────────────────

MAX_CONSECUTIVE_LOSSES = 5      # 5 losses in a row → pause
CIRCUIT_BREAKER_MINUTES = 30    # Pause for 30 min
DAILY_LOSS_LIMIT = 50.0         # Stop trading if daily loss exceeds $50

# ── PandaScore ──────────────────────────────────────────────────────────

PS_BASE = "https://api.pandascore.co"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ESPORTS_TAG_ID = 64

# ── LLF ─────────────────────────────────────────────────────────────────

LLF_RECONNECT_DELAY = 5
LLF_NOT_OPEN_DELAY = 30
LLF_RECV_TIMEOUT = 300

# ── Combo detection (signal v2) ─────────────────────────────────────────

COMBO_WINDOW_SEC = 30           # Events within 30s are part of same combo
TEAMFIGHT_KILL_THRESHOLD = 3    # Label “teamfight” + larger size when >= this in window
TEAMFIGHT_WINDOW_SEC = 15       # Kill burst window (stacked kills for same team)
MIN_STACKED_KILLS = 2           # v2: do not trade KILL unless >= this many kills in window (or post-obj)
POST_OBJECTIVE_KILL_WINDOW_SEC = 45.0  # KILL right after baron/inhib/drake counts as “follow-up”

# ── Logging ─────────────────────────────────────────────────────────────

LOG_TRADES_DIR = "logs"
